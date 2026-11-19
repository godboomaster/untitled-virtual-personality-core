"""Краткое описание персоны для модели телефона (app/features/short_prompt.py)
и окно «Краткое описание»: своё описание, промпт для своей нейросети,
«Составить заново», карточка, составленная на телефоне без ядра.

  A. Промпт для своей нейросети — тот же запрос, что уходит модели персоны.
  B. Своё описание в YAML (settings_api.set_persona_short_prompt): правка
     одного блока — комментарии и остальные ключи на месте; пустое — ключ
     убран; живой бот получает его сразу; необычная разметка — файл заново.
  C. Состояния: своё → yaml; «Составить заново» — прежняя карточка видна,
     composing, потом новая; не вышло — прежняя + error.
  D. Карточка с телефона: crc32 характера тот же — принята без вызова
     модели; другой — None; не карточка — ValueError.
  E. Маршруты PUT/POST short-prompt.

Бот подставной: модели и сеть не вызываются; данные — во временной папке.
Запуск: python3 -m scripts.test_short_prompt
"""

import os
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="short_prompt_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

import yaml  # noqa: E402

from app.api import settings_api  # noqa: E402
from app.features import short_prompt as sp  # noqa: E402

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


CHARACTER = (
    "Ты — Вега, штурман дальнего корабля. Говоришь коротко и по делу, с лёгкой иронией.\n\n"
    "Как ты говоришь: короткие фразы, морские словечки, никаких канцеляризмов.\n"
    "Нельзя: грубить, выдумывать факты о собеседнике."
)
CARD = (
    "Ты — Вега, штурман дальнего корабля, спокойная и ироничная.\n\n"
    "Как ты говоришь:\n- коротко и по делу\n- с морскими словечками\n- с лёгкой иронией\n\n"
    "Никогда:\n- не грубишь\n- не выдумываешь факты о собеседнике\n\n"
    "Примеры ответов:\n(привет) Свистать всех наверх — здравствуй!\n(как дела?) Курс ровный, ветер попутный."
)
NEW_CARD = CARD.replace("спокойная и ироничная", "собранная и насмешливая")

YAML_TEXT = """# Персона для проверки окна краткого описания
id: vega
name: Вега
# характер — главное
system_prompt: |
  Ты — Вега, штурман дальнего корабля. Говоришь коротко и по делу, с лёгкой иронией.

  Как ты говоришь: короткие фразы, морские словечки, никаких канцеляризмов.
  Нельзя: грубить, выдумывать факты о собеседнике.

# фичи
features:
  reminder: true
"""


class FakeRouter:
    def __init__(self):
        self.calls = []
        self.answer = CARD
        self.gate = None  # threading.Event — ответ только после него
        self.fail = False

    def get_response(self, messages, **kw):
        self.calls.append((messages, kw))
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError("провайдеры не ответили")
        return self.answer


def make_bot(context="api_vega", character=CHARACTER, data=None):
    persona = SimpleNamespace(system_prompt=character, persona_data=data if data is not None else {}, modes={})
    return SimpleNamespace(context=context, persona=persona, router=FakeRouter())


def wait_idle(job, timeout=5.0):
    until = time.time() + timeout
    while time.time() < until:
        with sp._state_lock:
            if job not in sp._running:
                return True
        time.sleep(0.01)
    return False


def test_manual_prompt():
    section("A. Промпт для своей нейросети")
    text = sp.manual_prompt(CHARACTER)
    msgs = sp.card_messages(CHARACTER)
    check("системная часть — CARD_PROMPT и язык описания",
          msgs[0]["content"].startswith(sp.CARD_PROMPT) and "Write the card in Russian" in msgs[0]["content"])
    check("одним текстом: системная часть, разделитель, характер",
          text == msgs[0]["content"] + "\n\n---\n\n" + CHARACTER)
    check("без характера — пусто", sp.manual_prompt("") == "")
    check("crc32 характера — как у телефона (zlib, UTF-8)",
          sp.character_crc(CHARACTER) == zlib.crc32(CHARACTER.encode("utf-8")))


def test_yaml_own():
    section("B. Своё описание в YAML")
    from app.api.runtime import registry
    tmp = Path(tempfile.mkdtemp(prefix="short_personas_"))
    path = tmp / "vega.yaml"
    path.write_text(YAML_TEXT, encoding="utf-8")
    orig_dir = settings_api._PERSONAS_DIR
    settings_api._PERSONAS_DIR = tmp
    bot = make_bot(data=yaml.safe_load(YAML_TEXT))
    registry._bots["vega"] = bot
    try:
        r = settings_api.set_persona_short_prompt("vega", "  " + CARD + "  \n")
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        check("записано — ok", r == {"ok": True})
        check("поле short_prompt — текст без пробелов по краям", data.get("short_prompt") == CARD)
        check("комментарии и остальные ключи на месте",
              raw.startswith(YAML_TEXT) and "# характер — главное" in raw and "# фичи" in raw
              and data["features"] == {"reminder": True} and data["system_prompt"].startswith("Ты — Вега"))
        check("живой бот получил описание сразу", bot.persona.persona_data.get("short_prompt") == CARD)
        check("get_short_prompt — своё (yaml), без вызова модели",
              sp.get_short_prompt(bot)["source"] == "yaml" and not bot.router.calls)

        settings_api.set_persona_short_prompt("vega", NEW_CARD)
        raw2 = path.read_text(encoding="utf-8")
        check("замена — один блок, файл тот же, кроме него",
              yaml.safe_load(raw2)["short_prompt"] == NEW_CARD and raw2.startswith(YAML_TEXT)
              and raw2.count("short_prompt:") == 1)

        # Своё описание посреди файла — правится на месте, соседи не задеты
        mid = YAML_TEXT.replace("# фичи\n", "short_prompt: старое\n\n# фичи\n")
        path.write_text(mid, encoding="utf-8")
        settings_api.set_persona_short_prompt("vega", "новое\nв две строки")
        raw3 = path.read_text(encoding="utf-8")
        d3 = yaml.safe_load(raw3)
        check("посреди файла — заменено на месте, комментарий и features после него целы",
              d3["short_prompt"] == "новое\nв две строки" and "\n\n# фичи\nfeatures:" in raw3
              and d3["features"] == {"reminder": True})

        settings_api.set_persona_short_prompt("vega", "   ")
        raw4 = path.read_text(encoding="utf-8")
        check("пустое — ключ убран, остальное как было",
              "short_prompt" not in yaml.safe_load(raw4) and raw4 == YAML_TEXT)
        check("…и у живого бота", "short_prompt" not in bot.persona.persona_data)

        # Необычная разметка (поток) — файл переписывается целиком, данные верны
        path.write_text("{id: vega, name: Вега, system_prompt: Ты Вега, short_prompt: x}\n", encoding="utf-8")
        settings_api.set_persona_short_prompt("vega", CARD)
        d5 = yaml.safe_load(path.read_text(encoding="utf-8"))
        check("поточный YAML — переписан целиком, данные верны",
              d5 == {"id": "vega", "name": "Вега", "system_prompt": "Ты Вега", "short_prompt": CARD})
        check("персоны нет — None", settings_api.set_persona_short_prompt("nope", CARD) is None)
    finally:
        settings_api._PERSONAS_DIR = orig_dir
        registry._bots.pop("vega", None)


def test_states():
    section("C. Состояния и «Составить заново»")
    bot = make_bot(context="api_c1")
    st = sp.get_short_prompt(bot)
    check("нет карточки — pending, составляется", st["status"] == "pending" and st["composing"] is True)
    check("составлено", wait_idle("api_c1") and sp.get_short_prompt(bot)["text"] == CARD)
    ready = sp.get_short_prompt(bot, with_prompt=True)
    check("готово — auto, composing false, промпт в ответе",
          ready["source"] == "auto" and ready["composing"] is False and ready["prompt"] == sp.manual_prompt(CHARACTER))
    check("модель — без локальной", bot.router.calls[0][1].get("exclude_provider") == "local")

    bot.router.answer = NEW_CARD
    bot.router.gate = threading.Event()
    st = sp.compose(bot)
    check("заново: прежняя карточка видна, composing true",
          st["status"] == "ready" and st["text"] == CARD and st["composing"] is True)
    bot.router.gate.set()
    wait_idle("api_c1")
    st = sp.get_short_prompt(bot)
    check("заново: новая карточка", st["text"] == NEW_CARD and st["composing"] is False and "error" not in st)

    bot.router.gate = None
    bot.router.fail = True
    sp.compose(bot)
    wait_idle("api_c1")
    st = sp.get_short_prompt(bot)
    check("заново не вышло — прежняя карточка и error",
          st["status"] == "ready" and st["text"] == NEW_CARD and "провайдеры" in st.get("error", ""))
    calls = len(bot.router.calls)
    sp.get_short_prompt(bot)
    check("после неудачи готовая не составляется сама снова", len(bot.router.calls) == calls)

    own = make_bot(context="api_c2", data={"short_prompt": CARD})
    st = sp.compose(own)
    check("своё задано — compose модель не зовёт (своё убирает вызывающий)",
          st["source"] == "yaml" and not own.router.calls)


def test_import():
    section("D. Карточка с телефона")
    bot = make_bot(context="api_d1")
    crc = zlib.crc32(CHARACTER.encode("utf-8"))
    check("характер другой — None", sp.import_card(bot, CARD, crc ^ 1) is None)
    try:
        sp.import_card(bot, "коротко", crc)
        check("не карточка — ValueError", False)
    except ValueError:
        check("не карточка — ValueError", True)
    st = sp.import_card(bot, "```\n" + CARD + "\n```", crc)
    check("принята — auto, ready, очищена от ```",
          st and st["source"] == "auto" and st["status"] == "ready" and st["text"] == CARD)
    check("модель не звали", not bot.router.calls)
    check("и дальше — она, без составления", sp.get_short_prompt(bot)["text"] == CARD and not bot.router.calls)


def test_routes():
    section("E. Маршруты PUT/POST short-prompt")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    from app.api.runtime import registry
    tmp = Path(tempfile.mkdtemp(prefix="short_routes_"))
    (tmp / "vega.yaml").write_text(YAML_TEXT, encoding="utf-8")
    bot = make_bot(context="api_e1", data=yaml.safe_load(YAML_TEXT))

    async def fake_get_bot(persona):
        return bot

    orig = server_mod._get_bot, server_mod._api_token, settings_api._PERSONAS_DIR
    server_mod._get_bot = fake_get_bot
    server_mod._api_token = ""
    settings_api._PERSONAS_DIR = tmp
    registry._bots["vega"] = bot
    base = "/api/personas/vega/offline/short-prompt"
    try:
        c = TestClient(server_mod.app, base_url="http://127.0.0.1")
        r = c.put(base, json={"text": CARD})
        check("PUT своё — 200, yaml, промпт в ответе",
              r.status_code == 200 and r.json()["source"] == "yaml" and r.json()["text"] == CARD
              and r.json()["prompt"].endswith(CHARACTER))
        check("…в YAML", yaml.safe_load((tmp / "vega.yaml").read_text(encoding="utf-8"))["short_prompt"] == CARD)
        r = c.put(base, json={"text": "x" * 4001})
        check("длиннее 4000 — 422", r.status_code == 422)
        r = c.put(base, json={"text": CARD, "source": "auto"})
        check("auto без crc — 422", r.status_code == 422)
        r = c.put(base, json={"text": CARD, "source": "auto", "crc": 1})
        check("auto с чужим crc — 409", r.status_code == 409)
        crc = zlib.crc32(CHARACTER.encode("utf-8"))
        r = c.put(base, json={"text": NEW_CARD, "source": "auto", "crc": crc})
        check("auto с crc характера — 200, своё убрано, карточка телефона",
              r.status_code == 200 and r.json()["source"] == "auto" and r.json()["text"] == NEW_CARD
              and "short_prompt" not in yaml.safe_load((tmp / "vega.yaml").read_text(encoding="utf-8"))
              and not bot.router.calls)
        c.put(base, json={"text": CARD})
        r = c.post(f"{base}/compose")
        check("compose — своё убрано, модель составляет, карточка телефона видна",
              r.status_code == 200 and r.json()["source"] == "auto" and r.json()["text"] == NEW_CARD
              and r.json()["composing"] is True
              and "short_prompt" not in yaml.safe_load((tmp / "vega.yaml").read_text(encoding="utf-8")))
        wait_idle("api_e1")
        r = c.get(base)
        check("GET после compose — новая карточка модели, промпт",
              r.json()["text"] == CARD and r.json()["composing"] is False and r.json()["prompt"])
        r = c.put(base, json={"text": ""})
        check("PUT пустое — 200, своё не задано", r.status_code == 200 and r.json()["source"] == "auto")
        server_mod._api_token = "s3cr3t"
        lan = TestClient(server_mod.app, base_url="http://127.0.0.1", client=("192.168.1.20", 50000))
        check("с API_TOKEN без токена — 401", lan.put(base, json={"text": CARD}).status_code == 401
              and lan.post(f"{base}/compose").status_code == 401)
    finally:
        server_mod._get_bot, server_mod._api_token, settings_api._PERSONAS_DIR = orig
        registry._bots.pop("vega", None)


def main():
    print("Краткое описание персоны: своё, промпт, «Составить заново», с телефона")
    test_manual_prompt()
    test_yaml_own()
    test_states()
    test_import()
    test_routes()
    print(f"\n{ok - failures}/{ok} OK" + (f", FAIL: {failures}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

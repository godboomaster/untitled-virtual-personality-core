"""Ответы без ноутбука (app/api/offline_api.py и маршруты /offline/*):

  - персона-кит: характер персоны, правила офлайна, правило вопросов,
    факты (с учётом приватности чата), хвост переписки, язык; секретов нет;
  - ключи: API-провайдеры персоны в её порядке, с моделью персоны; без
    модели или ключа и исключённые персоной — не отдаются;
  - приём офлайн-переписки: по времени, внутри хода пользователя, обычным
    memory.add_message (с извлечением фактов); повторы по id не пишутся;
  - маршруты: ключи с Cache-Control: no-store, проверка входа (роль, id,
    размер пачки).

Бот подставной: модели, Chroma и сеть не вызываются; данные — во временной
папке.
Запуск: PYTHONPATH=. python3 -m scripts.test_offline_api
"""

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="offline_api_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

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


class FakeRouter:
    def __init__(self):
        self.available = {
            "deepseek": {"base_url": "https://api.deepseek.com/v1/", "model": "deepseek-chat",
                         "api_keys": ["sk-ds-1", "sk-ds-2"]},
            "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "",
                     "api_keys": ["gsk-1"]},
            "kimi": {"base_url": "https://api.moonshot.ai/v1", "model": "kimi-k2",
                     "api_keys": ["sk-kimi"]},
            "zai": {"base_url": "https://api.z.ai/v1", "model": "glm", "api_keys": []},
            "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt", "api_keys": ["sk-oa"]},
        }
        self.overrides = {"groq": "llama-3.1-8b-instant"}
        self.excluded = {"openai"}

    def _get_provider_order(self):
        return ["kimi", "groq", "deepseek", "zai", "openai"]

    def _is_excluded(self, name):
        return name in self.excluded

    def model_for(self, name):
        return self.overrides.get(name) or self.available[name].get("model", "")


class FakeBot:
    context = "api_connor"

    def __init__(self):
        self.persona = SimpleNamespace(system_prompt="Ты Коннор, андроид-детектив.",
                                       persona_data={"name": "Коннор"})
        self.conversation_style = SimpleNamespace(frequency="rare")
        self.router = FakeRouter()
        self.calls = []
        self.turns = []
        stm = [
            {"role": "user", "content": "привет", "timestamp": 10.0},
            {"role": "system", "content": "служебное", "timestamp": 11.0},
            {"role": "assistant", "content": "Здравствуйте.", "timestamp": 12.0},
            {"role": "assistant", "content": "", "timestamp": 13.0},
        ]
        bot = self

        class Ltm:
            def get_all_facts(self, user_id="default", chat_id=None):
                bot.calls.append(("facts", user_id, chat_id))
                return ["Имя: Хэнк", "  ", "Работа: полиция"]

        class Stm:
            def get_last(self, n, chat_id=None):
                bot.calls.append(("tail", n, chat_id))
                return list(stm)

        def add_message(role, content, user_id="default", chat_id=None, user_name=None, timestamp=None):
            bot.calls.append(("add", role, content, user_id, chat_id, user_name, bool(bot.turns), timestamp))

        self.memory = SimpleNamespace(ltm=Ltm(), stm=Stm(), add_message=add_message)

    @staticmethod
    def stm_key(chat_id, user_id):
        return str(chat_id if chat_id is not None else user_id)

    def chat_user_language(self, key):
        return "ru"

    @contextmanager
    def user_turn(self, key):
        self.turns.append(key)
        try:
            yield {}
        finally:
            self.turns.pop()


def test_kit():
    section("A. Персона-кит")
    from app.api import offline_api
    bot = FakeBot()
    kit = offline_api.build_kit(bot, "connor", "web_user", "web_user")
    check("версия, id и имя персоны", kit["version"] == offline_api.KIT_VERSION
          and kit["persona"] == "connor" and kit["name"] == "Коннор")
    system = kit["system"]
    check("в системном тексте — характер персоны и правила офлайна",
          system.startswith("Ты Коннор") and offline_api.OFFLINE_RULES in system)
    check("…правило вопросов персоны (conversation_style)", "Do not end your reply with a question" in system)
    check("…факты о собеседнике списком", "What you know about the user:\n- Имя: Хэнк\n- Работа: полиция" in system)
    check("…и язык собеседника последним", system.rstrip().endswith(
        __import__("app.core.language", fromlist=["x"]).user_language_line("ru").rstrip()))
    check("факты — без пустых строк", kit["facts"] == ["Имя: Хэнк", "Работа: полиция"])
    parts = kit["parts"]
    check("части промпта для модели телефона: характер, правила, правило вопросов, язык",
          parts["character"].startswith("Ты Коннор") and parts["rules"] == offline_api.OFFLINE_RULES
          and "Do not end your reply with a question" in parts["style"] and parts["language"] in system)
    check("факты — с учётом приватности этого чата", ("facts", "web_user", "web_user") in bot.calls)
    check("хвост переписки: только user/assistant с текстом, по порядку",
          [(m["role"], m["text"]) for m in kit["history"]] == [("user", "привет"), ("assistant", "Здравствуйте.")])
    check(f"хвост — последние {offline_api.HISTORY_TAIL}", ("tail", offline_api.HISTORY_TAIL, "web_user") in bot.calls)
    flat = repr(kit)
    check("в ките нет ключей", "sk-" not in flat and "gsk" not in flat)


def test_providers():
    section("B. Ключи провайдеров")
    from app.api import offline_api
    creds = offline_api.provider_creds(FakeBot())
    names = [c["name"] for c in creds]
    check("порядок персоны", names == ["kimi", "groq", "deepseek"])
    check("модель персоны (llm.models) вместо общей", creds[1]["model"] == "llama-3.1-8b-instant")
    check("все ключи провайдера", creds[2]["api_keys"] == ["sk-ds-1", "sk-ds-2"])
    check("адрес без завершающего /", creds[2]["base_url"] == "https://api.deepseek.com/v1")
    check("без ключей (zai) и исключённые персоной (openai) — не отдаются",
          "zai" not in names and "openai" not in names)


def test_import():
    section("C. Приём офлайн-переписки")
    from app.api import offline_api
    bot = FakeBot()
    msgs = [
        {"id": "b", "role": "assistant", "text": "Я на связи.", "ts": 20.0, "source": "cloud"},
        {"id": "a", "role": "user", "text": "ты тут?", "ts": 19.0},
        {"id": "c", "role": "user", "text": "   ", "ts": 21.0},
    ]
    res = offline_api.import_messages(bot, "web_user", "web_user", msgs, user_name="Хэнк")
    adds = [c for c in bot.calls if c[0] == "add"]
    check("принято 3, повторов 0", res == {"imported": 3, "skipped": 0})
    check("по времени: сначала реплика пользователя, потом ответ",
          [(c[1], c[2]) for c in adds] == [("user", "ты тут?"), ("assistant", "Я на связи.")])
    check("пустой текст не пишется", len(adds) == 2)
    check("имя пользователя — у его реплик", adds[0][5] == "Хэнк" and adds[1][5] is None)
    check("запись — внутри хода пользователя", all(c[6] for c in adds))
    check("в историю этого чата", all(c[3] == "web_user" and c[4] == "web_user" for c in adds))
    check("время — с телефона", [c[7] for c in adds] == [19.0, 20.0])

    bot.calls.clear()
    res = offline_api.import_messages(bot, "web_user", "web_user",
                                      msgs + [{"id": "d", "role": "user", "text": "ещё", "ts": 30.0}])
    adds = [c for c in bot.calls if c[0] == "add"]
    check("повтор той же пачки — только новое", res == {"imported": 1, "skipped": 3}
          and [c[2] for c in adds] == ["ещё"])
    seen = offline_api._seen_path(bot)
    check("принятые id сохранены в папке данных контекста", seen.exists()
          and seen.parent.name == "api_connor")

    seen.write_text("[1, 2]")
    bot.calls.clear()
    res = offline_api.import_messages(bot, "web_user", "web_user",
                                      [{"id": "z", "role": "user", "text": "после порчи", "ts": 40.0}])
    check("битый файл id — не падает, приём идёт", res["imported"] == 1)

    bot.calls.clear()
    future = time.time() + 86400
    offline_api.import_messages(bot, "web_user", "web_user",
                                [{"id": "f", "role": "user", "text": "часы спешат", "ts": future}])
    adds = [c for c in bot.calls if c[0] == "add"]
    check("время из будущего (часы телефона спешат) — не позже сейчас",
          adds and adds[0][7] <= time.time())


def test_stm_timestamp():
    section("D. Время из прошлого в настоящей STM")
    from app.core.memory import ShortTermMemory
    from app.core.paths import data_dir
    tmp = Path(tempfile.mkdtemp(prefix="offline_stm_"))
    stm = ShortTermMemory(max_messages=10, db_path=str(tmp / "stm"), context="offline_stm")
    stm.add_message("user", "после возвращения", user_id="u", chat_id="c")
    now = time.time()
    stm.add_message("user", "из метро", user_id="u", chat_id="c", timestamp=now - 3600)
    stm.add_message("assistant", "ответ облака", user_id="u", chat_id="c", timestamp=now - 3590)
    stm.add_message("user", "в ту же мс", user_id="u", chat_id="c", timestamp=now - 3600)
    order = [m["content"] for m in stm.get_last(10, chat_id="c")]
    check("буфер — по времени, а не по порядку записи",
          order == ["из метро", "в ту же мс", "ответ облака", "после возвращения"])
    got = stm.collection.get(include=["metadatas"])
    stamps = sorted(m["timestamp"] for m in got["metadatas"])
    check("в базе — время из прошлого, занятая миллисекунда — следующая",
          len(set(stamps)) == 4 and stamps[0] == int((now - 3600) * 1000)
          and stamps[1] == stamps[0] + 1)
    again = ShortTermMemory(max_messages=10, db_path=str(tmp / "stm"), context="offline_stm")
    check("после перезапуска порядок тот же",
          [m["content"] for m in again.get_last(10, chat_id="c")] == order)
    last = json.loads((data_dir() / "offline_stm" / "last_message.json").read_text())
    check("метка последнего сообщения не уехала в прошлое", last["c"] >= now - 1)


def test_routes():
    section("D. Маршруты /offline/*")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    bot = FakeBot()

    async def fake_get_bot(persona):
        return bot

    orig_get, orig_token = server_mod._get_bot, server_mod._api_token
    server_mod._get_bot = fake_get_bot
    server_mod._api_token = ""
    try:
        c = TestClient(server_mod.app, base_url="http://127.0.0.1")
        r = c.get("/api/personas/connor/offline/kit")
        check("кит — 200, с текстом персоны", r.status_code == 200 and r.json()["name"] == "Коннор")
        r = c.get("/api/personas/connor/offline/providers")
        check("ключи — 200 и Cache-Control: no-store",
              r.status_code == 200 and r.headers.get("cache-control") == "no-store"
              and [p["name"] for p in r.json()["providers"]] == ["kimi", "groq", "deepseek"])
        body = {"messages": [{"id": "r1", "role": "user", "text": "привет из метро", "ts": 1.0}]}
        r = c.post("/api/personas/connor/offline/import", json=body)
        check("приём — 200, одно сообщение", r.status_code == 200 and r.json() == {"imported": 1, "skipped": 0})
        r = c.post("/api/personas/connor/offline/import", json=body)
        check("повтор — пропущен", r.status_code == 200 and r.json() == {"imported": 0, "skipped": 1})
        bad = [
            ({"messages": [{"id": "x", "role": "system", "text": "t", "ts": 1}]}, "роль system"),
            ({"messages": [{"id": "x y", "role": "user", "text": "t", "ts": 1}]}, "id с пробелом"),
            ({"messages": [{"id": "x", "role": "user", "text": "t" * 8001, "ts": 1}]}, "текст длиннее 8000"),
            ({"messages": [{"id": f"m{i}", "role": "user", "text": "t", "ts": i} for i in range(201)]}, "больше 200 сообщений"),
        ]
        for payload, name in bad:
            r = c.post("/api/personas/connor/offline/import", json=payload)
            check(f"{name} — 422", r.status_code == 422)
        server_mod._api_token = "s3cr3t"
        r = c.get("/api/personas/connor/offline/providers")
        check("с API_TOKEN без токена — 401 (ключи не отдаются)", r.status_code == 401)
    finally:
        server_mod._get_bot, server_mod._api_token = orig_get, orig_token


def main():
    test_kit()
    test_providers()
    test_import()
    test_stm_timestamp()
    test_routes()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

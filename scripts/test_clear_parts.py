"""«Очистить диалог» по частям (опасная зона досье):

  1. memory_wipe.collect_stores / wipe_stores с parts трогают только эти
     срезы (файловый фолбэк: бот без менеджеров), без parts — все;
  2. POST /api/chat/clear с parts: стираются только выбранные части
     (STM/LTM/дневник/инициативы/веб-чаты/срезы), в корзину уходят только
     они и список частей; без parts — полная очистка как раньше;
     неизвестная часть и пустой список — 422;
  3. backup_info отдаёт список частей последнего снапшота.

Данные и корзина — во временной папке, настоящий data/ не трогается
(clear_backup._DATA_DIR подменяется: он не смотрит на VPC_DATA_DIR).
Запуск: python3 -m scripts.test_clear_parts
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

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


PERSONA = "connor"  # существующая персона: эндпоинт проверяет id по реестру
CK = "web_user"


def seed(base: Path):
    ctx = base / f"api_{PERSONA}"
    (ctx / "todo" / CK).mkdir(parents=True, exist_ok=True)
    (ctx / "todo" / CK / "todo.txt").write_text("- купить хлеб\n", encoding="utf-8")
    (ctx / "reminders").mkdir(parents=True, exist_ok=True)
    (ctx / "reminders" / "reminders.json").write_text(json.dumps([
        {"chat_id": CK, "task": "полить цветы", "trigger_at": 9e9},
        {"chat_id": "other", "task": "чужое", "trigger_at": 9e9},
    ]), encoding="utf-8")
    return ctx


def test_memory_wipe(base: Path):
    section("1. memory_wipe: только выбранные срезы")
    from app.api import memory_wipe as mw
    ctx = seed(base)
    bot = SimpleNamespace()  # без менеджеров — файловый фолбэк
    check("ALL_PARTS — все 12 частей", len(mw.ALL_PARTS) == 12 and "control" in mw.ALL_PARTS)
    only_todo = mw.collect_stores(bot, PERSONA, CK, {"todo"})
    check("сбор parts={todo} — только todo", set(only_todo) == {"todo"})
    both = mw.collect_stores(bot, PERSONA, CK)
    check("сбор без parts — и todo, и напоминания", {"todo", "reminders"} <= set(both))
    mw.wipe_stores(bot, PERSONA, CK, {"todo"})
    rem = json.loads((ctx / "reminders" / "reminders.json").read_text(encoding="utf-8"))
    check("очистка parts={todo}: дела стёрты", not (ctx / "todo" / CK / "todo.txt").exists())
    check("очистка parts={todo}: напоминания целы", len(rem) == 2)
    mw.wipe_stores(bot, PERSONA, CK, {"reminders"})
    rem = json.loads((ctx / "reminders" / "reminders.json").read_text(encoding="utf-8"))
    check("очистка parts={reminders}: стёрты только напоминания этого чата",
          [r["task"] for r in rem] == ["чужое"])


class FakeBot:
    def __init__(self):
        self.calls = []
        self.memory = SimpleNamespace(
            stm=SimpleNamespace(get_messages=lambda u, c: [{"role": "user", "content": "привет"}]),
            ltm=SimpleNamespace(get_all_facts_with_meta=lambda u: [{"fact": "любит чай"}]),
            clear_stm=lambda c: self.calls.append("stm"),
            clear_ltm=lambda u: self.calls.append("ltm"),
        )
        self.self_memory = SimpleNamespace(export_state=lambda: {"episodes": 1},
                                           clear_all=lambda: self.calls.append("diary"))


def test_endpoint(base: Path):
    section("2. POST /api/chat/clear с parts")
    from fastapi.testclient import TestClient
    import app.api.server as server_mod
    from app.api import clear_backup, memory_wipe
    from app.features import web_llm as wl

    clear_backup._DATA_DIR = base  # корзина — во временной папке
    bot = FakeBot()
    wiped, collected = [], []

    async def fake_get_bot(persona):
        return bot

    server_mod._get_bot = fake_get_bot
    server_mod._pop_initiative_history = lambda b, p, c: (bot.calls.append("init"), [{"m": 1}])[1]
    server_mod._pop_daily_stats = lambda b, p, c: {"count": 2}
    server_mod._pop_last_activity = lambda b, p, c: 123.0
    wl.collect_chat_urls = lambda ctx: {"deepseek#chat": "https://x"}
    wl.clear_chat_urls = lambda ctx: bot.calls.append("webchat")
    memory_wipe.collect_stores = lambda b, p, c, parts=None: (collected.append(parts), {"todo": "- хлеб"})[1]
    memory_wipe.wipe_stores = lambda b, p, c, parts=None: wiped.append(parts)

    c = TestClient(server_mod.app, base_url="http://127.0.0.1")

    def clear(parts=None):
        bot.calls.clear(); wiped.clear(); collected.clear()
        body = {"persona": PERSONA, "user_id": CK, "chat_id": CK}
        if parts is not None:
            body["parts"] = parts
        return c.post("/api/chat/clear", json=body)

    def latest():
        return clear_backup.pop_latest(PERSONA, CK) or {}

    r = clear(["todo"])
    snap = latest()
    check("parts=[todo]: 200", r.status_code == 200)
    check("parts=[todo]: переписка, факты, дневник, веб-чаты, инициативы не тронуты", bot.calls == [])
    check("parts=[todo]: срезы собраны и стёрты только для todo",
          collected == [{"todo"}] and wiped == [{"todo"}])
    check("parts=[todo]: в корзине только срез и список частей",
          snap.get("parts") == ["todo"] and snap.get("stm") == [] and snap.get("ltm") == []
          and snap.get("diary") is None and snap.get("initiatives") == [] and snap.get("chat_urls") == {})

    r = clear(["ltm", "stm"])
    snap = latest()
    check("parts=[ltm, stm]: стёрты переписка и факты, остальное нет",
          sorted(bot.calls) == ["ltm", "stm"] and wiped == [{"ltm", "stm"}])
    check("parts=[ltm, stm]: в корзине переписка и факты, части по порядку",
          snap.get("parts") == ["stm", "ltm"] and len(snap.get("stm") or []) == 1
          and len(snap.get("ltm") or []) == 1 and snap.get("diary") is None)

    r = clear(["initiatives", "webchat", "diary"])
    snap = latest()
    check("parts=[initiatives, webchat, diary]: история инициатив, веб-чаты, дневник",
          sorted(bot.calls) == ["diary", "init", "webchat"])
    check("…в корзине их снапшот", snap.get("initiatives") == [{"m": 1}]
          and snap.get("daily_stats") == {"count": 2} and snap.get("last_activity") == 123.0
          and snap.get("chat_urls") and snap.get("diary") == {"episodes": 1})

    r = clear()
    snap = latest()
    check("без parts — полная очистка: всё стёрто",
          sorted(bot.calls) == ["diary", "init", "ltm", "stm", "webchat"] and wiped == [None])
    check("без parts — в корзине parts=None (полный снапшот)", "parts" in snap and snap["parts"] is None)
    check("ответ полной очистки — parts: all", r.json().get("parts") == "all")

    check("неизвестная часть — 422", clear(["everything"]).status_code == 422)
    check("пустой список — 422", clear([]).status_code == 422)

    section("3. backup_info: части последнего снапшота")
    clear(["reminders", "todo"])
    info = clear_backup.backup_info(PERSONA, CK)
    check("backup_info.parts — стёртые части", info.get("parts") == ["todo", "reminders"])
    latest()
    clear()
    check("после полной очистки backup_info.parts = None",
          clear_backup.backup_info(PERSONA, CK).get("parts") is None)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="clear_parts_"))
    os.environ["VPC_DATA_DIR"] = str(tmp)
    test_memory_wipe(tmp)
    test_endpoint(tmp)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

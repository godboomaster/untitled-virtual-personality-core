"""Тест паузы напоминаний (reminder_manager + веб-API).

  - напоминание на паузе не срабатывает, когда наступило его время
    (проход планировщика _check_due на поддельных часах), и не отправляется,
    если его поставили на паузу посреди хода (_still_due);
  - продолжение разового, чьё время прошло на паузе, — уходит ближайшим тиком;
  - продолжение повторяющегося — следующее время по расписанию после «сейчас»
    (пропущенные на паузе не догоняются); ещё не наступившее trigger_at
    короткая пауза не сбрасывает;
  - флаг паузы переживает перечитывание файла; старые записи без флага —
    активны и срабатывают;
  - PUT /reminders/{id} {active} и поле active в GET; напоминание на паузе
    остаётся в списке и после своего времени; /reminders помечает его;
    календарь и перенос по чату его не берут.

Всё на временной VPC_DATA_DIR — настоящая data/ не трогается.

Запуск: /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m scripts.test_reminder_pause
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, *, topic_id=None, parse_mode=None):
        self.sent.append((chat_id, text))
        return True


class FakeClock:
    """Подменяет модуль time внутри reminder_manager: планировщик видит
    заданное «сейчас», остальной процесс — настоящее время."""

    def __init__(self, now: float):
        self.now = now

    def time(self):
        return self.now


def main():
    tmp = Path(tempfile.mkdtemp(prefix="reminder_pause_test_"))
    old_env = os.environ.get("VPC_DATA_DIR")
    os.environ["VPC_DATA_DIR"] = str(tmp)
    try:
        run_scheduler()
        run_persistence(tmp)
        run_api(tmp)
    finally:
        if old_env is None:
            os.environ.pop("VPC_DATA_DIR", None)
        else:
            os.environ["VPC_DATA_DIR"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


def run_scheduler():
    from app.core import timeutil
    from app.features import reminder_manager as rm_mod
    from app.features.reminder_manager import ReminderManager

    section("1. Планировщик не берёт напоминание на паузе")
    clock = FakeClock(time.time())
    with mock.patch.object(rm_mod, "time", clock):
        rm = ReminderManager(context="pause_sched")
        sender = FakeSender()
        rm.set_sender(sender)

        once = rm.add_reminder("c1", "User", "выпить воды", 600)
        other = rm.add_reminder("c1", "User", "позвонить", 600)
        upd = rm.update_by_id("c1", once["id"], active=False)
        check("update_by_id(active=False): запись на паузе",
              upd is not None and upd.get("paused") is True)
        check("пауза не трогает trigger_at", abs(upd["trigger_at"] - once["trigger_at"]) < 1e-6)

        clock.now += 3600  # оба «наступили»
        asyncio.run(rm._check_due())
        sent_tasks = [t for _, t in sender.sent]
        check("наступившее на паузе не отправлено, соседнее — отправлено",
              len(sender.sent) == 1 and "позвонить" in sent_tasks[0])
        check("на паузе: не погашено (fired нет)", not once.get("fired"))
        check("соседнее погашено", other.get("fired") is True)
        asyncio.run(rm._check_due())
        check("повторный тик — по-прежнему не отправлено", len(sender.sent) == 1)
        listed = rm.get_active("c1")
        check("на паузе видно в списке и после своего времени",
              [r["id"] for r in listed] == [once["id"]])
        check("_still_due: на паузе — не «в силе» (ход не отправит)",
              rm._still_due(once) is False)

        section("2. Продолжение разового после его времени — ближайшим тиком")
        upd = rm.update_by_id("c1", once["id"], active=True)
        check("update_by_id(active=True): флаг снят, trigger_at прежний (в прошлом)",
              upd is not None and not upd.get("paused")
              and upd["trigger_at"] == once["trigger_at"] < clock.now)
        asyncio.run(rm._check_due())
        check("продолженное просроченное ушло ближайшим тиком",
              len(sender.sent) == 2 and "выпить воды" in sender.sent[-1][1]
              and once.get("fired") is True)
        check("сработавшее больше не адресуется (404-путь)",
              rm.update_by_id("c1", once["id"], active=False) is None)

        section("3. Повторяющееся: пауза, пропуск, продолжение")
        sched = {"type": "daily", "hour": 9, "minute": 15}
        daily = rm.add_reminder("c1", "User", "зарядка", 0, schedule=sched)
        first_at = daily["trigger_at"]
        rm.update_by_id("c1", daily["id"], active=False)
        clock.now = first_at + 3 * 86400 + 60  # три срабатывания прошли на паузе
        sender.sent.clear()
        asyncio.run(rm._check_due())
        check("повторяющееся на паузе не срабатывает и не перепланируется",
              sender.sent == [] and daily["trigger_at"] == first_at)
        upd = rm.update_by_id("c1", daily["id"], active=True)
        nxt = upd["trigger_at"]
        dt = timeutil.from_ts(nxt)
        check("продолжение: следующее время по расписанию в будущем",
              clock.now < nxt <= clock.now + 86400 and (dt.hour, dt.minute) == (9, 15))
        check("расписание не изменилось", upd["recurrence"] == {"type": "daily", "hour": 9, "minute": 15})
        asyncio.run(rm._check_due())
        check("сразу после продолжения пропущенное не догоняется", sender.sent == [])
        clock.now = nxt + 1
        asyncio.run(rm._check_due())
        check("в своё время срабатывает и переносится на сутки вперёд",
              len(sender.sent) == 1 and abs(daily["trigger_at"] - (nxt + 86400)) < 3700)

        # Короткая пауза до срабатывания: ранее перенесённое время не сбрасывается
        weekly = rm.add_reminder("c1", "User", "отчёт", 0,
                                 schedule={"type": "weekly", "hour": 18, "minute": 0, "weekday": 4})
        moved = weekly["trigger_at"] + 1800  # «перенеси на полчаса»
        weekly["trigger_at"] = moved
        rm.update_by_id("c1", weekly["id"], active=False)
        upd = rm.update_by_id("c1", weekly["id"], active=True)
        check("пауза до срабатывания: trigger_at остаётся прежним", upd["trigger_at"] == moved)

        section("4. Пауза и перенос по чату")
        rm2 = ReminderManager(context="pause_postpone")
        a = rm2.add_reminder("c2", "User", "чай", 600)
        b = rm2.add_reminder("c2", "User", "кофе", 1200)
        rm2.update_by_id("c2", b["id"], active=False)
        res = rm2.postpone_reminder("c2", seconds=300)
        check("перенос без подсказки: единственное не на паузе, без «какое?»",
              res and res.get("task") == "чай" and not res.get("ambiguous"))
        res = rm2.postpone_reminder("c2", seconds=300, task_hint="кофе")
        check("перенос по подсказке не находит напоминание на паузе",
              res and res.get("not_found") is True)
        check("postpone_by_id на паузе — None",
              rm2.postpone_by_id("c2", b["id"], seconds=60) is None)
        check("пауза с новым временем в прошлом — ValueError",
              _raises(lambda: rm2.update_by_id("c2", a["id"], trigger_at=clock.now - 5,
                                                active=False)))


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


def run_persistence(tmp: Path):
    from app.features import reminder_manager as rm_mod
    from app.features.reminder_manager import ReminderManager

    section("5. Хранение: флаг паузы в файле и старый формат")
    rm = ReminderManager(context="pause_persist")
    r = rm.add_reminder("c3", "User", "полить цветы", 600)
    rm.update_by_id("c3", r["id"], active=False)
    raw = json.loads((tmp / "pause_persist" / "reminders" / "reminders.json").read_text("utf-8"))
    check("в файле — \"paused\": true", raw[0].get("paused") is True)
    again = ReminderManager(context="pause_persist")
    check("после перечитывания — всё ещё на паузе",
          again.get_active("c3") and again.get_active("c3")[0].get("paused") is True)
    again.update_by_id("c3", r["id"], active=True)
    raw = json.loads((tmp / "pause_persist" / "reminders" / "reminders.json").read_text("utf-8"))
    check("после продолжения флага в файле нет", "paused" not in raw[0])

    # Файл старого формата: без id и без флага паузы
    legacy_dir = tmp / "pause_legacy" / "reminders"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (legacy_dir / "reminders.json").write_text(json.dumps([
        {"chat_id": "c4", "user_name": "User", "task": "старое", "created_at": now - 100,
         "trigger_at": now - 5, "topic_id": None, "fired": False},
        {"chat_id": "c4", "user_name": "User", "task": "будущее", "created_at": now - 100,
         "trigger_at": now + 600, "topic_id": None, "fired": False},
    ], ensure_ascii=False), encoding="utf-8")
    legacy = ReminderManager(context="pause_legacy")
    check("старые записи без флага — активны (в списке будущее, active)",
          [x["task"] for x in legacy.get_active("c4")] == ["будущее"]
          and not rm_mod.is_paused(legacy.get_active("c4")[0]))
    sender = FakeSender()
    legacy.set_sender(sender)
    asyncio.run(legacy._check_due())
    check("старое просроченное без флага срабатывает как раньше",
          len(sender.sent) == 1 and "старое" in sender.sent[0][1])


def run_api(tmp: Path):
    from fastapi.testclient import TestClient
    import app.api.server as server_mod
    from app.api.schemas import ChatRequest
    from app.core.config import Config
    from app.features.reminder_manager import ReminderManager

    section("6. Веб-API: PUT active, GET active, /reminders, календарь")
    rm = ReminderManager(context="api_pauseapi")
    # chat_user_language — язык служебных текстов (/reminders); у заглушки — русский
    bot = SimpleNamespace(reminder_manager=rm, chat_user_language=lambda chat_id: None)

    async def fake_get_bot(persona):
        return bot

    orig_token = server_mod._api_token
    server_mod._api_token = ""
    try:
        with mock.patch.object(server_mod, "_get_bot", fake_get_bot):
            client = TestClient(server_mod.app, base_url="http://127.0.0.1")
            base = "/api/personas/pauseapi/reminders"
            client.post(base, json={"task": "первое", "delay_seconds": 600})
            client.post(base, json={"task": "второе", "delay_seconds": 1200})
            items = client.get(base).json()["items"]
            check("GET: у всех active=true", len(items) == 2 and all(i["active"] is True for i in items))
            first = next(i for i in items if i["task"] == "первое")
            r = client.put(f"{base}/{first['id']}", json={"active": False})
            after = {i["id"]: i for i in r.json()["items"]} if r.status_code == 200 else {}
            check("PUT active=false: 200, active=false, время прежнее",
                  r.status_code == 200 and after[first["id"]]["active"] is False
                  and abs(after[first["id"]]["trigger_at"] - first["trigger_at"]) < 0.01)

            # Время напоминания на паузе прошло — оно всё ещё в списке
            rec = next(x for x in rm._reminders if x["id"] == first["id"])
            rec["trigger_at"] = time.time() - 30
            items = client.get(base).json()["items"]
            check("GET: на паузе и после своего времени — в списке",
                  any(i["id"] == first["id"] and i["active"] is False for i in items))

            reply, _ = server_mod._try_slash_command(
                bot, ChatRequest(persona="pauseapi", message="/reminders", chat_id="web_user"))
            check("/reminders: помечает паузу, без отрицательного остатка",
                  "на паузе" in reply and "через -" not in reply)

            with mock.patch.object(server_mod, "list_personas", lambda: ["pauseapi"]), \
                    mock.patch.object(Config, "DATA_DIR", str(tmp)):
                second = next(i for i in items if i["task"] == "второе")
                client.put(f"{base}/{second['id']}", json={"active": False})
                cal = server_mod._reminder_calendar_items(None, None, {})
                check("календарь не показывает напоминания на паузе", cal == [])
                client.put(f"{base}/{second['id']}", json={"active": True})
                cal = server_mod._reminder_calendar_items(None, None, {})
                check("продолженное — снова в календаре",
                      [c["title"] for c in cal] == ["второе"])

            r = client.put(f"{base}/{first['id']}", json={"active": True})
            check("PUT active=true: 200", r.status_code == 200)
            check("продолженное просроченное разовое уходит ближайшим тиком (из списка — до отправки)",
                  all(i["id"] != first["id"] for i in r.json()["items"]))
            r = client.put(f"{base}/{second['id']}", json={"active": "maybe"})
            check("active не bool: 422", r.status_code == 422)
            r = client.put(f"{base}/rffff0", json={"active": False})
            check("чужой id: 404", r.status_code == 404)
    finally:
        server_mod._api_token = orig_token


if __name__ == "__main__":
    sys.exit(main())

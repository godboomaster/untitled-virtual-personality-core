"""Напоминания глазами пользователя: что видно в списке и что ставится.

  A. Часть суток у времени: повтор «каждый день в 10 вечера» — 22:00, а не
     10:00, и «вечера» не остаётся в тексте задачи; «at 7 in the evening» —
     19:00; разовое «в 10 вечера выпить таблетку» — без лишнего «в» в задаче;
     «good night» в задаче час не трогает.
  B. Описание расписания и срока: по-русски для русского чата («каждый день в
     10:00», «по будням», «по понедельникам и пятницам»), по-английски для
     английского; дальше часа — временем («в 18:30», «завтра в 10:00»), а не
     «через 1380 мин»; пауза и прошедшее время.
  C. Номер в отмене — строка показанного списка: после показа одно сработало
     и выпало — «/cancel_reminder 2» всё равно отменяет показанное вторым, а
     не съехавшее на его место; показанного уже нет — None, а не соседнее; без показанного списка — по
     текущему порядку, как раньше; «последнее» — последнее показанное; id
     по-прежнему принимается.
  D. Списки без служебных кодов: /reminders в веб-чате — номер, задача и
     срок, без id; по-английски для английского чата; список для модели —
     без id и без telegram-id автора.
  E. /remind с повтором отвечает по-русски целиком («каждый день в 22:00»).

Всё на временной VPC_DATA_DIR — настоящая data/ не трогается. Без LLM и сети.
Запуск: python3 -m scripts.test_reminder_display
"""

import os
import shutil
import sys
import tempfile
import time
from datetime import timedelta
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


def run_parsing():
    from app.features import reminder_manager as rm

    section("A. Часть суток у времени")
    cases = [
        ("напоминай каждый день в 10 вечера выпить таблетку", "выпить таблетку", 22, 0),
        ("напоминай каждый день в 7 утра зарядка", "зарядка", 7, 0),
        ("напоминай каждый день в 10 утра просыпаться", "просыпаться", 10, 0),
        ("напоминай по пятницам в 6 вечера", None, 18, 0),
        ("напоминай каждый день в 8:30 вечера гулять", "гулять", 20, 30),
        ("напоминай каждый день в 12 дня обед", "обед", 12, 0),
        ("напоминай каждый день к 9 вечера закрыть ноутбук", "закрыть ноутбук", 21, 0),
        ("напоминай каждый день в 12:30", None, 12, 30),
        ("remind me every day at 7 in the morning to stretch", "stretch", 7, 0),
        ("remind me every day at 7 in the evening to stretch", "stretch", 19, 0),
        ("remind me every day at 9 to say good night", "say good night", 9, 0),
        ("remind me every Friday at 6 pm to call mom", "call mom", 18, 0),
    ]
    for text, task, hour, minute in cases:
        got = rm.parse_recurring(text)
        check(f"повтор {text!r} → {task!r} {hour:02d}:{minute:02d} (получено {got})",
              got is not None and got[0] == task
              and got[1]["hour"] == hour and got[1]["minute"] == minute)

    for text, task in [
        ("напомни в 10 вечера выпить таблетку", "выпить таблетку"),
        ("напомни в 7 утра зарядка", "зарядка"),
        ("напомни завтра в 9 вечера позвонить", "позвонить"),
        ("напомни завтра утром купить хлеб", "купить хлеб"),
        ("напомни через 2 часа позвонить", "позвонить"),
    ]:
        got = rm.parse_reminder(text)
        check(f"разовое {text!r} → задача {task!r} (получено {got and got[0]!r})",
              got is not None and got[0] == task)


def run_format():
    from app.core import timeutil
    from app.features import reminder_manager as rm

    section("B. Расписание и срок — на языке чата")
    fs = rm.format_schedule
    daily = {"type": "daily", "weekday": None, "hour": 10, "minute": 0}
    check("каждый день — ru", fs(daily, "ru") == "каждый день в 10:00")
    check("каждый день — en (по умолчанию, для модели)", fs(daily) == "every day at 10:00")
    check("по будням", fs({"type": "weekly", "weekdays": [0, 1, 2, 3, 4], "weekday": 0,
                          "hour": 9, "minute": 0}, "ru") == "по будням в 09:00")
    check("по выходным", fs({"type": "weekly", "weekdays": [5, 6], "weekday": 5,
                            "hour": 11, "minute": 0}, "ru") == "по выходным в 11:00")
    check("один день", fs({"type": "weekly", "weekday": 4, "hour": 18, "minute": 0}, "ru")
          == "по пятницам в 18:00")
    check("несколько дней", fs({"type": "weekly", "weekdays": [0, 2, 4], "weekday": 0,
                               "hour": 18, "minute": 0}, "ru")
          == "по понедельникам, средам и пятницам в 18:00")

    when = rm.format_reminder_when
    now = time.time()
    near = {"trigger_at": now + 9 * 60 + 30}
    check("ближе часа — остатком, ru", when(near) == "через 9 мин")
    check("ближе часа — остатком, en", when(near, "en") == "in 9 min")
    check("меньше минуты — секундами", when({"trigger_at": now + 40}).startswith("через "))
    # Дальше часа — временем. Опорные метки — по часам пользователя
    base = timeutil.from_ts(now).replace(second=0, microsecond=0)
    tomorrow = timeutil.to_ts((base + timedelta(days=1)).replace(hour=10, minute=0))
    check("завтра — «завтра в 10:00»", when({"trigger_at": tomorrow}) == "завтра в 10:00")
    check("завтра — en", when({"trigger_at": tomorrow}, "en") == "tomorrow at 10:00")
    later = timeutil.to_ts((base + timedelta(days=3)).replace(hour=8, minute=5))
    d = timeutil.from_ts(later).strftime("%d.%m")
    check("через 3 дня — дата и время", when({"trigger_at": later}) == f"{d} в 08:05")
    two_h = now + 2 * 3600
    dt = timeutil.from_ts(two_h)
    if dt.date() == timeutil.today():
        check("сегодня дальше часа — «в ЧЧ:ММ»", when({"trigger_at": two_h}) == f"в {dt.strftime('%H:%M')}")
    check("нигде не «через 1380 мин»", "мин" not in when({"trigger_at": tomorrow}))
    check("повтор — расписанием по-русски",
          when({"trigger_at": tomorrow, "recurrence": daily}) == "каждый день в 10:00")
    check("повтор — en", when({"trigger_at": tomorrow, "recurrence": daily}, "en") == "every day at 10:00")
    check("на паузе — пометка", when({"trigger_at": tomorrow, "paused": True}).endswith("(на паузе)"))
    check("прошедшее на паузе — без отрицательного остатка",
          when({"trigger_at": now - 30, "paused": True}) == "время прошло (на паузе)")


def run_numbers(tmp):
    from app.features.reminder_manager import ReminderManager

    section("C. Номер — строка показанного списка")
    mgr = ReminderManager(context="disp_numbers")
    a = mgr.add_reminder("c1", "User", "выключить плиту", 60)
    b = mgr.add_reminder("c1", "User", "полить цветы", 3600)
    c = mgr.add_reminder("c1", "User", "позвонить маме", 7200)
    mgr.note_listed("c1", mgr.get_active("c1"))  # пользователь видит 1, 2, 3
    # Пока он читает, первое сработало и выпало из активных
    mgr.cancel_by_ref("c1", a["id"])
    check("в текущем порядке на месте №2 теперь третье",
          [r["id"] for r in mgr.get_active("c1")] == [b["id"], c["id"]])
    removed = mgr.cancel_by_ref("c1", 1)
    check("«/cancel_reminder 2» отменяет показанное вторым, а не съехавшее",
          removed is not None and removed["id"] == b["id"])
    check("третье не тронуто", [r["id"] for r in mgr.get_active("c1")] == [c["id"]])
    check("показанного №1 уже нет — None, а не соседнее", mgr.cancel_by_ref("c1", 0) is None)
    check("номер вне показанного списка — None", mgr.cancel_by_ref("c1", 5) is None)
    mgr.add_reminder("c1", "User", "новое после показа", 600)
    res = mgr.cancel_request("c1", {"all": False, "ref": ("index", -1), "hint": None})
    check("«отмени последнее» — последнее показанное, а не добавленное после",
          [r["id"] for r in res.get("cancelled") or []] == [c["id"]])

    mgr2 = ReminderManager(context="disp_numbers2")
    x = mgr2.add_reminder("c2", "User", "первое", 600)
    mgr2.add_reminder("c2", "User", "второе", 1200)
    check("без показанного списка — по текущему порядку, как раньше",
          (mgr2.cancel_by_ref("c2", 0) or {}).get("id") == x["id"])
    y = mgr2.add_reminder("c2", "User", "третье", 1800)
    check("id по-прежнему принимается", (mgr2.cancel_by_ref("c2", y["id"]) or {}).get("id") == y["id"])


def run_lists(tmp):
    from app.api import server as server_mod
    from app.api.schemas import ChatRequest
    from app.features.reminder_manager import ReminderManager
    import app.bot_instance as bi

    section("D. Списки без служебных кодов")
    mgr = ReminderManager(context="disp_lists")
    r1 = mgr.add_reminder("w1", "Аня (100000001)", "полить цветы", 9 * 60 + 30)
    r2 = mgr.add_reminder("w1", "Аня (100000001)", "зарядка", 0,
                          schedule={"type": "daily", "weekday": None, "hour": 7, "minute": 0})
    lang = {"value": None}
    bot = SimpleNamespace(reminder_manager=mgr, chat_user_language=lambda chat_id: lang["value"])
    reply, _ = server_mod._try_slash_command(
        bot, ChatRequest(persona="p", message="/reminders", chat_id="w1"))
    check("веб /reminders: номера и задачи", "1. полить цветы" in reply and "2. зарядка" in reply)
    check("веб /reminders: без служебного id",
          r1["id"] not in reply and r2["id"] not in reply and "[id" not in reply)
    check("веб /reminders: повтор по-русски", "каждый день в 07:00" in reply and "every" not in reply)
    check("веб /reminders: подсказка — по номеру", "/cancel_reminder N" in reply)
    reply, _ = server_mod._try_slash_command(
        bot, ChatRequest(persona="p", message="/cancel_reminder 2", chat_id="w1"))
    check("веб /cancel_reminder 2 — вторая строка показанного списка, без id в ответе",
          "зарядка" in reply and r2["id"] not in reply)
    lang["value"] = "en"
    reply, _ = server_mod._try_slash_command(
        bot, ChatRequest(persona="p", message="/reminders", chat_id="w1"))
    check("английский чат — список по-английски",
          reply.startswith("Active reminders:") and "in 9 min" in reply)

    listing = bi._fmt_reminder_list(mgr.get_active("w1") + [r2])
    check("список для модели — без id", r1["id"] not in listing and "[" not in listing)
    check("список для модели — автор без telegram-id",
          "(by Аня)" in listing and "100000001" not in listing)
    choices = bi._fmt_reminder_choices(mgr.get_active("w1"))
    check("выбор для переноса — без id", r1["id"] not in choices)


def run_dispatch():
    import app.bot_instance as bi
    from app.features.reminder_manager import ReminderManager

    section("E. /remind с повтором — ответ по-русски")
    bot = bi.BotInstance.__new__(bi.BotInstance)
    bot.reminder_manager = ReminderManager(context="disp_dispatch")
    bot._pending_question_kind = {}
    bot._take_list_offer = lambda chat_id, user_id: None
    bot._reformulate_task = lambda t: t
    bot._activity_tracker = None
    reply = bot._dispatch_command("remind", "каждый день в 10 вечера выпить таблетку",
                                  "d1", "u1", "User")
    check(f"«каждый день в 22:00», без английского ({reply!r})",
          "каждый день в 22:00" in reply and "every" not in reply)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="reminder_display_"))
    os.environ["VPC_DATA_DIR"] = str(tmp)
    os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")
    try:
        run_parsing()
        run_format()
        run_numbers(tmp)
        run_lists(tmp)
        run_dispatch()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

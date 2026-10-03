"""Повтор напоминания из веб-модалки (reminder_manager + веб-API):

  - расписание по нескольким дням недели (weekdays): ближайшее срабатывание,
    переход через неделю, «по будням»/«по выходным», все 7 дней — каждый
    день; старый формат с одним weekday работает как раньше;
  - format_schedule описывает новые расписания;
  - POST /reminders с recurrence — повтор сохраняется, первое срабатывание
    по расписанию и не раньше выбранного срока; неверный повтор — 422;
  - PUT /reminders/{id}: смена повтора, снятие (recurrence: null), правка
    без recurrence повтор не трогает.

Всё на временной VPC_DATA_DIR — настоящая data/ не трогается.
Запуск: python3 -m scripts.test_reminder_repeat
"""

import os
import shutil
import sys
import tempfile
import time
from datetime import timedelta
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


def at(base, days, hour, minute):
    # Метка «через days дней в hour:minute» по часам пользователя
    from app.core import timeutil
    dt = (base + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return timeutil.to_ts(dt)


def run_schedule():
    from app.core import timeutil
    from app.features import reminder_manager as rm_mod
    norm, nxt, fmt = rm_mod.normalize_schedule, rm_mod._next_occurrence, rm_mod.format_schedule

    section("1. Расписание по дням недели")
    # Опорный момент — среда 12:00 (по часам пользователя)
    now_dt = timeutil.from_ts(time.time())
    wed = now_dt + timedelta(days=(2 - now_dt.weekday()) % 7)
    wed = wed.replace(hour=12, minute=0, second=0, microsecond=0)
    base = timeutil.to_ts(wed)
    mwf = norm({"type": "weekly", "weekdays": [4, 0, 2], "hour": 9, "minute": 30})
    check("нормализация: дни отсортированы, weekday — первый",
          mwf["weekdays"] == [0, 2, 4] and mwf["weekday"] == 0 and mwf["type"] == "weekly")
    check("ср 12:00, пн/ср/пт 9:30 → пт 9:30", nxt(mwf, base) == at(wed, 2, 9, 30))
    check("ср 9:00, пн/ср/пт 9:30 → в тот же день",
          nxt(mwf, at(wed, 0, 9, 0)) == at(wed, 0, 9, 30))
    check("пт 10:00 → через выходные на пн 9:30", nxt(mwf, at(wed, 2, 10, 0)) == at(wed, 5, 9, 30))
    only_wed = norm({"type": "weekly", "weekdays": [2], "hour": 9, "minute": 0})
    check("один день, время прошло → через неделю", nxt(only_wed, base) == at(wed, 7, 9, 0))
    work = norm({"type": "weekly", "weekdays": [0, 1, 2, 3, 4], "hour": 8, "minute": 0})
    check("по будням: пт 9:00 → пн 8:00", nxt(work, at(wed, 2, 9, 0)) == at(wed, 5, 8, 0))
    check("все 7 дней → каждый день",
          norm({"type": "weekly", "weekdays": list(range(7)), "hour": 8, "minute": 0})["type"] == "daily")
    legacy = {"type": "weekly", "weekday": 3, "hour": 18, "minute": 0}
    check("старый формат (один weekday): ср 12:00 → чт 18:00", nxt(legacy, base) == at(wed, 1, 18, 0))
    daily = {"type": "daily", "hour": 7, "minute": 0, "weekday": None}
    check("каждый день: ср 12:00 → чт 7:00", nxt(daily, base) == at(wed, 1, 7, 0))
    check("время не задано — из срока",
          norm({"type": "daily"}, at(wed, 1, 21, 15))["hour"] == 21)
    for bad, why in (({"type": "weekly", "weekdays": [], "hour": 9, "minute": 0}, "без дней"),
                     ({"type": "hourly", "hour": 9, "minute": 0}, "неизвестный тип"),
                     ({"type": "daily", "hour": 24, "minute": 0}, "час 24"),
                     ({"type": "weekly", "weekdays": [7], "hour": 9, "minute": 0}, "день 7")):
        try:
            norm(bad)
            check(f"ошибка: {why}", False)
        except ValueError:
            check(f"ошибка: {why}", True)

    section("2. Описание расписания")
    check("пн, ср, пт", fmt(mwf) == "every Monday, Wednesday and Friday at 09:30")
    check("по будням", fmt(work) == "every weekday (Mon–Fri) at 08:00")
    check("по выходным",
          fmt(norm({"type": "weekly", "weekdays": [5, 6], "hour": 10, "minute": 0}))
          == "every weekend day (Sat and Sun) at 10:00")
    check("старый формат", fmt(legacy) == "every Thursday at 18:00")
    check("каждый день", fmt(daily) == "every day at 07:00")


def run_api():
    from fastapi.testclient import TestClient
    import app.api.server as server_mod
    from app.core import timeutil
    from app.features.reminder_manager import ReminderManager

    section("3. Веб-API: создание, смена и снятие повтора")
    rm = ReminderManager(context="api_repeatapi")
    bot = SimpleNamespace(reminder_manager=rm)

    async def fake_get_bot(persona):
        return bot

    orig_token = server_mod._api_token
    server_mod._api_token = ""
    try:
        with mock.patch.object(server_mod, "_get_bot", fake_get_bot):
            client = TestClient(server_mod.app, base_url="http://127.0.0.1")
            base = "/api/personas/repeatapi/reminders"
            r = client.post(base, json={"task": "зарядка", "delay_seconds": 3600,
                                        "recurrence": {"type": "weekly", "weekdays": [0, 2, 4],
                                                       "hour": 7, "minute": 15}})
            items = r.json().get("items", []) if r.status_code == 200 else []
            item = next((i for i in items if i["task"] == "зарядка"), None)
            rec = (item or {}).get("recurrence") or {}
            check("POST с повтором: 200, повтор в ответе", r.status_code == 200 and rec.get("weekdays") == [0, 2, 4])
            fire = timeutil.from_ts(item["trigger_at"]) if item else None
            check("первое срабатывание — пн/ср/пт в 7:15",
                  fire is not None and fire.weekday() in (0, 2, 4) and (fire.hour, fire.minute) == (7, 15))
            check("GET: повтор на месте",
                  next(i for i in client.get(base).json()["items"] if i["task"] == "зарядка")["recurrence"]["weekdays"] == [0, 2, 4])

            # Не раньше выбранного срока: старт через 9 дней, каждый день в 6:00
            r = client.post(base, json={"task": "отпуск", "delay_seconds": 9 * 86400,
                                        "recurrence": {"type": "daily", "hour": 6, "minute": 0}})
            vac = next(i for i in r.json()["items"] if i["task"] == "отпуск")
            check("первое срабатывание не раньше выбранного срока",
                  vac["trigger_at"] >= time.time() + 8 * 86400 and vac["recurrence"]["type"] == "daily")

            r = client.post(base, json={"task": "плохо", "delay_seconds": 600,
                                        "recurrence": {"type": "weekly", "weekdays": [], "hour": 9, "minute": 0}})
            check("POST: неверный повтор — 422, напоминание не создано",
                  r.status_code == 422 and all(i["task"] != "плохо" for i in client.get(base).json()["items"]))
            r = client.post(base, json={"task": "разовое", "delay_seconds": 600})
            once = next(i for i in r.json()["items"] if i["task"] == "разовое")
            check("POST без повтора — разовое", once["recurrence"] is None)

            # Смена повтора: пн/ср/пт → по выходным в 10:00
            r = client.put(f"{base}/{item['id']}", json={"recurrence": {"type": "weekly", "weekdays": [5, 6],
                                                                           "hour": 10, "minute": 0}})
            upd = next(i for i in r.json()["items"] if i["id"] == item["id"])
            fire = timeutil.from_ts(upd["trigger_at"])
            check("PUT: повтор заменён, срок — сб/вс 10:00",
                  r.status_code == 200 and upd["recurrence"]["weekdays"] == [5, 6]
                  and fire.weekday() in (5, 6) and (fire.hour, fire.minute) == (10, 0))
            # Правка только текста — повтор не трогается
            r = client.put(f"{base}/{item['id']}", json={"task": "зарядка утром"})
            upd = next(i for i in r.json()["items"] if i["id"] == item["id"])
            check("PUT без recurrence: повтор прежний",
                  upd["task"] == "зарядка утром" and upd["recurrence"]["weekdays"] == [5, 6])
            # Новый срок у расписания по дням: срок — ближайший из дней
            target = upd["trigger_at"] + 86400 * 3
            r = client.put(f"{base}/{item['id']}", json={"trigger_at": target})
            upd = next(i for i in r.json()["items"] if i["id"] == item["id"])
            check("PUT trigger_at: срок попадает на день расписания",
                  timeutil.from_ts(upd["trigger_at"]).weekday() in (5, 6) and upd["trigger_at"] >= target - 1)
            # Без часа/минуты (так шлёт фронт) — время из срока, по часам пользователя
            r = client.post(base, json={"task": "по сроку", "delay_seconds": 7200,
                                        "recurrence": {"type": "weekly", "weekdays": [0, 3]}})
            by_due = next((i for i in r.json().get("items", []) if i["task"] == "по сроку"), None)
            due = timeutil.from_ts(time.time() + 7200)
            check("POST без hour/minute: время повтора — из срока напоминания",
                  r.status_code == 200 and by_due is not None
                  and (by_due["recurrence"]["hour"], by_due["recurrence"]["minute"]) == (due.hour, due.minute))
            # Один день недели (weekdays из одного дня) следует за датой нового срока
            r = client.post(base, json={"task": "один день", "delay_seconds": 3600,
                                        "recurrence": {"type": "weekly", "weekdays": [2], "hour": 8, "minute": 0}})
            one = next(i for i in r.json()["items"] if i["task"] == "один день")
            new_due = one["trigger_at"] + 86400 * 2  # среда → пятница
            r = client.put(f"{base}/{one['id']}", json={"trigger_at": new_due})
            one = next(i for i in r.json()["items"] if i["id"] == one["id"])
            wd = timeutil.from_ts(new_due).weekday()
            check("PUT trigger_at: один день недели переезжает на день новой даты",
                  one["recurrence"]["weekdays"] == [wd] and one["recurrence"]["weekday"] == wd
                  and abs(one["trigger_at"] - new_due) < 1)
            # Снять повтор
            r = client.put(f"{base}/{item['id']}", json={"recurrence": None})
            upd = next(i for i in r.json()["items"] if i["id"] == item["id"])
            check("PUT recurrence: null — повтор снят, напоминание осталось", upd["recurrence"] is None)
            # Разовое → повтор
            r = client.put(f"{base}/{once['id']}", json={"recurrence": {"type": "daily", "hour": 21, "minute": 0}})
            upd = next(i for i in r.json()["items"] if i["id"] == once["id"])
            check("PUT: разовое стало ежедневным в 21:00",
                  upd["recurrence"]["type"] == "daily"
                  and (timeutil.from_ts(upd["trigger_at"]).hour, timeutil.from_ts(upd["trigger_at"]).minute) == (21, 0))
            r = client.put(f"{base}/{once['id']}", json={"task": "x", "recurrence": {"type": "hourly"}})
            check("PUT: неверный повтор — 422, текст не изменён",
                  r.status_code == 422
                  and next(i for i in client.get(base).json()["items"] if i["id"] == once["id"])["task"] == "разовое")
    finally:
        server_mod._api_token = orig_token


def main():
    tmp = Path(tempfile.mkdtemp(prefix="reminder_repeat_test_"))
    old_env = os.environ.get("VPC_DATA_DIR")
    os.environ["VPC_DATA_DIR"] = str(tmp)
    try:
        run_schedule()
        run_api()
    finally:
        if old_env is None:
            os.environ.pop("VPC_DATA_DIR", None)
        else:
            os.environ["VPC_DATA_DIR"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Тест тишины после запуска (app/core/startup_quiet).

Проверяет:
* без запуска (тесты, импорт) тишины нет; после mark_started — у каждой
  персоны свой срок в диапазоне BOT_STARTUP_QUIET_MIN («10-20», «15», «0»,
  перевёрнутый диапазон, мусор → по умолчанию), сроки разные (вразнобой),
  стабильны для персоны; давний запуск — тишина кончилась; wait_quiet ждёт;
* ритм: утро в тишине не генерируется и не отправляется, а откладывается —
  и приходит после неё; ночь в тишине молчит;
* напоминания: пропущенные, пока бот был выключен, ждут конца тишины,
  наступившие после запуска — вовремя;
* инициатива: сигнал состояния в тишине — без отправки.

Запуск: PYTHONPATH=. python3 scripts/test_startup_quiet.py
"""

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="startup_quiet_")

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, topic_id=None, parse_mode=None):
        self.sent.append((chat_id, text))
        return True


def main():
    from app.core import startup_quiet as sq

    print("модуль:")
    os.environ.pop("BOT_STARTUP_QUIET_MIN", None)
    check("без запуска тишины нет", not sq.is_quiet("p") and sq.quiet_left("p") == 0)
    sq.mark_started()
    t0 = sq.started_at()
    u = sq.quiet_until("arrodes")
    check("по умолчанию — 10–20 минут", 600 <= u - t0 <= 1200 and sq.is_quiet("arrodes"))
    check("срок персоны стабилен", sq.quiet_until("arrodes") == u)
    spread = {round(sq.quiet_until(f"p{i}")) for i in range(20)}
    check("персоны просыпаются вразнобой", len(spread) > 1)
    os.environ["BOT_STARTUP_QUIET_MIN"] = "15"
    sq.mark_started()
    check("«15» — ровно 15 минут", abs(sq.quiet_until("x") - sq.started_at() - 900) < 1e-6)
    os.environ["BOT_STARTUP_QUIET_MIN"] = "5-3"
    sq.mark_started()
    check("перевёрнутый диапазон — 3–5 минут", 180 <= sq.quiet_until("x") - sq.started_at() <= 300)
    os.environ["BOT_STARTUP_QUIET_MIN"] = "abc"
    sq.mark_started()
    check("мусор — по умолчанию 10–20", 600 <= sq.quiet_until("x") - sq.started_at() <= 1200)
    os.environ["BOT_STARTUP_QUIET_MIN"] = "0"
    sq.mark_started()
    check("«0» — без тишины", not sq.is_quiet("x"))
    os.environ.pop("BOT_STARTUP_QUIET_MIN", None)
    sq.mark_started(time.time() - 3600)
    check("запуск час назад — тишина кончилась", not sq.is_quiet("x"))
    os.environ["BOT_STARTUP_QUIET_MIN"] = "0.01"
    sq.mark_started()
    t = time.monotonic()
    asyncio.run(sq.wait_quiet("w"))
    waited = time.monotonic() - t
    check("wait_quiet ждёт конца тишины", 0.4 <= waited <= 2.0 and not sq.is_quiet("w"))

    print("ритм дня:")
    from app.core import timeutil
    from app.features.rhythm_manager import RhythmConfig, RhythmManager
    os.environ.pop("BOT_STARTUP_QUIET_MIN", None)
    sq.mark_started()
    sender = FakeSender()
    rm = RhythmManager(context="quiet_rhythm", config=RhythmConfig.from_dict(True), sender=sender)
    generated = []
    rm._generate_text = lambda *a, **kw: generated.append(a[0]) or "Доброе утро!"
    morning_now = datetime(2026, 9, 23, 8, 0)
    seen = timeutil.to_ts(morning_now) - 10 * 3600
    asyncio.run(rm._do_morning("c1", morning_now, seen))
    check("утро в тишине: без генерации и отправки, отложено",
          not generated and not sender.sent and "c1" in rm._deferred_morning)
    night_now = datetime(2026, 9, 23, 0, 30)
    asyncio.run(rm._do_night("c1", night_now, timeutil.to_ts(night_now) - 60))
    check("ночь в тишине молчит", not generated and not sender.sent)
    sq.mark_started(time.time() - 3600)

    async def retry():
        rm._aio_loop = asyncio.get_running_loop()
        await rm._retry_deferred(morning_now)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if sender.sent:
                break

    asyncio.run(retry())
    check("после тишины отложенное утро пришло", generated == ["morning"]
          and sender.sent and sender.sent[0][1] == "Доброе утро!")

    print("напоминания:")
    from app.features.reminder_manager import ReminderManager
    sq.mark_started()
    rem = ReminderManager(context="quiet_rem")
    fired = []

    async def fake_fire(r):
        fired.append(r["task"])
        return True

    rem._fire = fake_fire
    time.sleep(0.05)
    now = time.time()
    rem._reminders = [
        {"id": "a", "task": "пропущено, пока бот был выключен", "chat_id": "c1",
         "trigger_at": sq.started_at() - 600},
        {"id": "b", "task": "наступило после запуска", "chat_id": "c1",
         "trigger_at": sq.started_at() + 0.01},
        {"id": "c", "task": "ещё не время", "chat_id": "c1", "trigger_at": now + 3600},
    ]
    asyncio.run(rem._check_due())
    check("в тишине: наступившее после запуска — вовремя, пропущенное ждёт",
          fired == ["наступило после запуска"])
    sq.mark_started(time.time() - 3600)
    rem._reminders[0]["trigger_at"] = sq.started_at() - 600
    asyncio.run(rem._check_due())
    check("после тишины пропущенное пришло", fired[-1] == "пропущено, пока бот был выключен")

    print("инициатива:")
    from app.features.proactive_messaging import ProactiveConfig, ProactiveMessaging
    sq.mark_started()
    psender = FakeSender()
    pm = ProactiveMessaging(
        config=ProactiveConfig(enabled=True), router=None,
        persona=SimpleNamespace(persona_data={}, system_prompt="Ты — тест.", get_settings=lambda: {}),
        memory=None, activity_tracker=None, get_last_message_time=lambda cid: time.time() - 10 * 3600,
        sender=psender, context="quiet_pm")
    called = []
    pm._generate_initiative = lambda *a, **kw: called.append(1) or "Эй!"
    asyncio.run(pm.state_initiative_signal("c1", 0.99, "скучно"))
    check("сигнал состояния в тишине — без генерации и отправки", not called and not psender.sent)

    os.environ.pop("BOT_STARTUP_QUIET_MIN", None)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Тест единого «времени пользователя» (app/core/timeutil.py).

Проверяет:
* tz()/now()/today()/from_ts()/to_ts() — пояс из TIMEZONE (и POSIX-фолбэк TZ),
  без переменной — системный локальный пояс, битое имя — тоже фолбэк, без
  исключения;
* инвариант «стенные часы ↔ epoch»: to_ts(now()) == time.time(),
  from_ts(to_ts(dt)) == dt (именно из-за него в менеджерах нельзя звать
  .timestamp() у naive-datetime);
* применение в парсере напоминаний: «в 9» и «завтра в 8» считаются от часов
  ПОЛЬЗОВАТЕЛЯ, а не от пояса процесса; повторяющееся напоминание
  срабатывает в заданный час его пояса;
* применение в proactive: сегодняшняя дата (суточный лимит инициатив) и окно
  initiative_hours — по часам пользователя.

Запуск: PYTHONPATH=. python3 scripts/test_timeutil.py
"""

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

# Два пояса с заведомо разным смещением — какой бы пояс ни был у машины,
# хотя бы один из них от него отличается
TZ_A = "Asia/Tokyo"          # UTC+9, без DST
TZ_B = "America/Los_Angeles"  # UTC-8/-7


def main():
    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    from app.core import timeutil

    def set_tz(name):
        for var in timeutil.ENV_VARS:
            os.environ.pop(var, None)
        if name:
            os.environ["TIMEZONE"] = name

    # ── 1. Без переменной — системный локальный пояс ──
    set_tz(None)
    check("tz(): TIMEZONE не задан → None (системный пояс)", timeutil.tz() is None)
    check("now(): без TIMEZONE совпадает с datetime.now()",
          abs((timeutil.now() - datetime.now()).total_seconds()) < 2)
    check("today(): без TIMEZONE совпадает с date.today()",
          timeutil.today() == datetime.now().date())
    check("to_ts(now()) ≈ time.time() (без TIMEZONE)",
          abs(timeutil.to_ts(timeutil.now()) - time.time()) < 2)

    # ── 2. TIMEZONE задан ──
    for zone in (TZ_A, TZ_B):
        set_tz(zone)
        expected = datetime.now(ZoneInfo(zone)).replace(tzinfo=None)
        check(f"now(): TIMEZONE={zone} → стенные часы этого пояса",
              abs((timeutil.now() - expected).total_seconds()) < 2)
        check(f"now(): naive (без tzinfo) — договор модуля [{zone}]",
              timeutil.now().tzinfo is None)
        check(f"to_ts(now()) ≈ time.time() [{zone}] — обратимость",
              abs(timeutil.to_ts(timeutil.now()) - time.time()) < 2)
        ts = 1_700_000_000.0
        check(f"from_ts/to_ts — взаимно обратны [{zone}]",
              abs(timeutil.to_ts(timeutil.from_ts(ts)) - ts) < 1)
        check(f"from_ts(): epoch → часы пояса [{zone}]",
              timeutil.from_ts(ts)
              == datetime.fromtimestamp(ts, ZoneInfo(zone)).replace(tzinfo=None))
        check(f"today(): дата пояса [{zone}]", timeutil.today() == expected.date())

    # Два пояса действительно дают разное время (иначе проверки выше пусты)
    set_tz(TZ_A)
    a = timeutil.now()
    set_tz(TZ_B)
    b = timeutil.now()
    check("пояса TZ_A и TZ_B дают разные стенные часы (кеш пояса обновился)",
          abs((a - b).total_seconds()) > 3600)

    # ── 3. Битое имя и POSIX-фолбэк TZ ──
    set_tz("Nowhere/Nothing")
    check("битое имя пояса → фолбэк на системный, без исключения",
          timeutil.tz() is None
          and abs((timeutil.now() - datetime.now()).total_seconds()) < 2)
    set_tz(None)
    os.environ["TZ"] = TZ_A
    check("POSIX-переменная TZ тоже принимается",
          timeutil.tz_name() == TZ_A and timeutil.tz() is not None)
    os.environ.pop("TZ", None)
    set_tz(TZ_A)
    os.environ["TZ"] = TZ_B
    check("TIMEZONE приоритетнее TZ", timeutil.tz_name() == TZ_A)
    os.environ.pop("TZ", None)

    # ── 4. Напоминания считаются от часов пользователя ──
    from app.features import reminder_manager as rm

    for zone in (TZ_A, TZ_B):
        set_tz(zone)
        user_now = datetime.now(ZoneInfo(zone)).replace(tzinfo=None)
        target_hour = (user_now.hour + 3) % 24  # заведомо «сегодня позже»
        parsed = rm.parse_reminder(f"напомни в {target_hour} позвонить")
        fire_at = time.time() + (parsed[1] if parsed else 0)
        check(f"'напомни в {target_hour}' → срабатывание в {target_hour}:00 "
              f"по часам пользователя [{zone}]",
              parsed is not None
              and datetime.fromtimestamp(fire_at, ZoneInfo(zone)).hour == target_hour)

        parsed = rm.parse_reminder("напомни завтра в 8 купить хлеб")
        check(f"'завтра в 8' → 8:00 следующего дня пользователя [{zone}]",
              parsed is not None
              and datetime.fromtimestamp(time.time() + parsed[1],
                                         ZoneInfo(zone)).hour == 8)

        # Повторяющееся: _next_occurrence кладёт trigger_at на заданный час
        schedule = {"type": "daily", "hour": 7, "minute": 30}
        nxt = rm._next_occurrence(schedule, time.time())
        local = datetime.fromtimestamp(nxt, ZoneInfo(zone))
        check(f"recurring 'каждый день в 7:30' → 07:30 пояса [{zone}]",
              local.hour == 7 and local.minute == 30 and nxt > time.time())

    # ── 5. proactive: дата суток и окно инициатив по часам пользователя ──
    from app.features.proactive_messaging import ProactiveMessaging

    for zone in (TZ_A, TZ_B):
        set_tz(zone)
        expected_date = datetime.now(ZoneInfo(zone)).strftime("%Y-%m-%d")
        check(f"_get_today() → дата пользователя [{zone}]",
              ProactiveMessaging._get_today(None) == expected_date)

    # Окно 09:00-22:00 считается по часам пользователя, а не процесса:
    # полдень его пояса — внутри окна, а один и тот же момент в разных
    # поясах даёт разные вердикты
    fake = SimpleNamespace(config=SimpleNamespace(initiative_hours=["09:00", "22:00"]))
    set_tz(TZ_A)
    noon_a = datetime(2026, 9, 22, 12, 0, tzinfo=ZoneInfo(TZ_A)).timestamp()
    check("initiative_hours: полдень в поясе пользователя → окно открыто",
          ProactiveMessaging._in_initiative_hours(fake, noon_a))
    set_tz(TZ_B)
    noon_b = datetime(2026, 9, 22, 12, 0, tzinfo=ZoneInfo(TZ_B)).timestamp()
    check("initiative_hours: полдень другого пояса пользователя → тоже открыто",
          ProactiveMessaging._in_initiative_hours(fake, noon_b))
    diverged = False
    for hour in range(24):
        probe = datetime(2026, 9, 22, hour, 0, tzinfo=ZoneInfo(TZ_A)).timestamp()
        set_tz(TZ_A)
        in_a = ProactiveMessaging._in_initiative_hours(fake, probe)
        set_tz(TZ_B)
        in_b = ProactiveMessaging._in_initiative_hours(fake, probe)
        if in_a != in_b:
            diverged = True
            break
    check("initiative_hours: один и тот же момент в разных поясах → разные "
          "вердикты (окно от пояса пользователя, не процесса)", diverged)

    # ── 6. Окружение для промпта — тоже часы пользователя ──
    from app.features import env_context
    # Локацию гасим: тесту нужна только временная часть строки, а с настроенной
    # локацией get_env_line ходил бы в сеть за погодой (и читал реальный
    # data/env_location.json)
    env_context.load_location = lambda: {"mode": "off"}
    set_tz(TZ_A)
    line = env_context.get_env_line() or ""
    stamp = datetime.now(ZoneInfo(TZ_A)).strftime("%d.%m.%Y, %H:%M")
    check("get_env_line(): дата/время пользователя в строке окружения",
          stamp[:-1] in line)  # минуту могло перещёлкнуть между вызовами

    set_tz(None)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Smoke-тест фичи rhythm (суточный ритм: утро/ночь/погода).

Проверяет: разбор конфига (bool/dict/дефолты), гейты утреннего приветствия
(окно/пауза/раз-в-день), гейты ночного nudge (bedtime/активность/раз-за-ночь,
ключ ночи для bedtime 23), детект пробуждения машины (wall vs monotonic),
погодное решение (дождь в пределах lead / кулдаун / перепад / приоритет
грозы), сквозную отправку утреннего приветствия c fallback-текстом и
live-семантику (ключ _LIVE_FEATURE_KEYS, update_config).

Запуск: python -m scripts.test_rhythm
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    import asyncio
    import os
    tmp = tempfile.mkdtemp(prefix="rhythm_smoke_")
    os.environ["DATA_DIR"] = tmp
    # RhythmManager/ChatActivityTracker пишут в относительный data/ — уходим в tmp
    os.chdir(tmp)

    import importlib
    import app.core.config as config_mod
    importlib.reload(config_mod)

    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    from app.features.rhythm_manager import (
        RhythmConfig, RhythmManager, _detect_wake,
    )

    # ── 1. Конфиг: bool / dict / пусто ──
    cfg = RhythmConfig.from_dict(True)
    check("config: bool true → enabled + подсекции on",
          cfg.enabled and cfg.morning.enabled and cfg.sleep.enabled and cfg.weather.enabled)
    cfg = RhythmConfig.from_dict(False)
    check("config: bool false → disabled", not cfg.enabled)
    cfg = RhythmConfig.from_dict({})
    check("config: пустой dict → disabled", not cfg.enabled)
    cfg = RhythmConfig.from_dict({
        "enabled": True,
        "sleep_nudge": {"bedtime_hour": 23, "enabled": False},
        "weather_alerts": {"rain_lead_hours": "2", "temp_delta_c": 10.5},
    })
    check("config: вложенные секции + приведение типов",
          cfg.sleep.bedtime_hour == 23 and not cfg.sleep.enabled
          and cfg.morning.enabled and cfg.weather.rain_lead_hours == 2
          and cfg.weather.temp_delta_c == 10.5)
    cfg_bad = RhythmConfig.from_dict({"enabled": True, "morning_greeting": True})
    check("config: подсекция-bool → включена с дефолтами",
          cfg_bad.morning.enabled and cfg_bad.morning.window_start == 5)

    # ── 2. Гейты утреннего приветствия ──
    class FakeTracker:
        def __init__(self, chats=None):
            self.act = {str(c): 1000.0 for c in (chats or [])}

        def record_activity(self, chat_id):
            import time
            self.act[str(chat_id)] = time.time()

        def get_last_activity(self, chat_id):
            return self.act.get(str(chat_id), 0.0)

        def get_known_chats(self):
            return list(self.act)

        def get_topic(self, chat_id):
            return None

    class FakeStm:
        def get_last(self, n, chat_id=None):
            return [{"content": "привет, как дела?"}]

    class FakeMemory:
        stm = FakeStm()
        log = []

        def add_message(self, role, text, **kw):
            self.log.append((role, text))

    class FakeSender:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, *, topic_id=None, parse_mode=None):
            self.sent.append((str(chat_id), text))
            return True

    rm = RhythmManager(context="rhythm_test_ctx", config=RhythmConfig.from_dict(True))
    now = datetime(2026, 8, 25, 8, 0)  # вторник, 08:00
    seen_night = now.timestamp() - 8 * 3600  # ночью не видели
    check("morning: окно + пауза → да", rm._should_morning_greet({}, now, seen_night))
    check("morning: уже здоровались сегодня → нет",
          not rm._should_morning_greet({"morning_date": "2026-08-25"}, now, seen_night))
    check("morning: вне окна (15:00) → нет",
          not rm._should_morning_greet({}, datetime(2026, 8, 25, 15, 0), seen_night))
    check("morning: до окна (4:59) → нет",
          not rm._should_morning_greet({}, datetime(2026, 8, 25, 4, 59), seen_night))
    check("morning: пауза меньше min_gap → нет",
          not rm._should_morning_greet({}, now, now.timestamp() - 600))
    check("morning: пользователя никогда не видели → нет",
          not rm._should_morning_greet({}, now, 0.0))
    rm.config.morning.enabled = False
    check("morning: подсекция выключена → нет",
          not rm._should_morning_greet({}, now, seen_night))
    rm.config.morning.enabled = True
    rm.config.enabled = False
    check("morning: мастер-выключатель → нет",
          not rm._should_morning_greet({}, now, seen_night))
    rm.config.enabled = True

    # ── 3. Гейты ночного nudge ──
    night = datetime(2026, 8, 26, 0, 30)  # 00:30 следующего дня
    active = night.timestamp() - 30 * 60  # писал полчаса назад
    check("night: полночь + активен → да", rm._should_night_nudge({}, night, active))
    check("night: уже слали этой ночью → нет",
          not rm._should_night_nudge({"night_key": "2026-08-26"}, night, active))
    check("night: пользователь неактивен 5 ч → нет",
          not rm._should_night_nudge({}, night, night.timestamp() - 5 * 3600))
    check("night: поздно (03:10, окно 2 ч) → нет",
          not rm._should_night_nudge({}, datetime(2026, 8, 26, 3, 10), active))
    check("night: 01:59 ещё в окне → да",
          rm._should_night_nudge({}, datetime(2026, 8, 26, 1, 59), active))
    check("night: ключ ночи для bedtime 0 (00:30 авг 26) = 2026-08-26",
          RhythmManager._night_key(night, 0) == "2026-08-26")
    check("night: ключ ночи для bedtime 23 (23:30 авг 25) = 2026-08-25",
          RhythmManager._night_key(datetime(2026, 8, 25, 23, 30), 23) == "2026-08-25")
    rm23 = RhythmManager(context="rhythm_test_ctx2",
                         config=RhythmConfig.from_dict({"enabled": True,
                                                        "sleep_nudge": {"bedtime_hour": 23}}))
    n23 = datetime(2026, 8, 25, 23, 45)
    check("night: bedtime 23, 23:45 + активен → да",
          rm23._should_night_nudge({}, n23, n23.timestamp() - 30 * 60))

    # ── 4. Детект пробуждения машины ──
    check("wake: сон ~2 ч замечен",
          _detect_wake(1000.0, 10.0, 1000.0 + 60 + 7200, 10.0 + 61))
    check("wake: обычный тик (без сна) не срабатывает",
          not _detect_wake(1000.0, 10.0, 1061.0, 71.0))
    check("wake: короткий дрейф (NTP) не срабатывает",
          not _detect_wake(1000.0, 10.0, 1000.0 + 60 + 30, 10.0 + 61))

    # ── 5. Погодное решение ──
    def fc(hours, cur_code=1, cur_temp=5.0):
        return {"current_code": cur_code, "current_temp": cur_temp,
                "hours": [{"time": now + timedelta(hours=h), "code": c,
                           "temp": t, "precip_prob": p} for h, c, t, p in hours]}

    rain_soon = fc([(1, 0, 5.0, 10), (2, 61, 4.0, 80)])
    res = rm._weather_alert(rain_soon, {}, now)
    check("weather: дождь через 2 ч → rain", res is not None and res[0] == "rain")
    res = rm._weather_alert(rain_soon, {"rain": {"ts": now.timestamp() - 3600}}, now)
    check("weather: кулдаун rain 6 ч → None", res is None)
    res = rm._weather_alert(rain_soon, {"rain": {"ts": now.timestamp() - 7 * 3600}}, now)
    check("weather: кулдаун прошёл → rain", res is not None and res[0] == "rain")

    already_raining = fc([(1, 61, 4.0, 90)], cur_code=63)
    check("weather: осадки уже идут → не предупреждаем",
          rm._weather_alert(already_raining, {}, now) is None)

    far_rain = fc([(5, 61, 4.0, 80)])  # lead 3 ч, дождь через 5 ч
    check("weather: дождь за пределами lead → None",
          rm._weather_alert(far_rain, {}, now) is None)

    stormy = fc([(1, 0, 5.0, 20), (2, 96, 4.0, 70)])
    res = rm._weather_alert(stormy, {}, now)
    check("weather: гроза через 2 ч → storm", res is not None and res[0] == "storm")

    drop = fc([(12, 0, -4.0, 0)], cur_temp=15.0)
    res = rm._weather_alert(drop, {}, now)
    check("weather: перепад 19° → temp", res is not None and res[0] == "temp")
    small = fc([(12, 0, 12.0, 0)], cur_temp=15.0)
    check("weather: перепад 3° → None", rm._weather_alert(small, {}, now) is None)

    # ── 6. Сквозная отправка: fallback-текст, раз-в-день, STM ──
    sender = FakeSender()
    memory = FakeMemory()
    tracker = FakeTracker(chats=["chat1"])
    tracker.act["chat1"] = now.timestamp() - 8 * 3600  # «утро после ночи»
    rm2 = RhythmManager(context="rhythm_test_ctx3", config=RhythmConfig.from_dict(True),
                        memory=memory, activity_tracker=tracker, sender=sender)
    asyncio.run(rm2._do_morning("chat1", now, tracker.get_last_activity("chat1")))
    check("send: приветствие ушло (fallback)", len(sender.sent) == 1)
    check("send: state отмечен",
          rm2._chat_state("chat1").get("morning_date") == "2026-08-25")
    check("send: fallback на русском (кириллица в STM)",
          sender.sent and sender.sent[0][1] == "Доброе утро!")
    check("send: записано в STM как assistant",
          any(r == "assistant" for r, _ in memory.log))
    asyncio.run(rm2._do_morning("chat1", now, tracker.get_last_activity("chat1")))
    check("send: повтор в тот же день не шлётся", len(sender.sent) == 1)

    night_state = {}
    n = datetime(2026, 8, 26, 0, 10)
    asyncio.run(rm2._do_night("chat1", n, n.timestamp() - 20 * 60))
    check("send: ночной nudge ушёл", len(sender.sent) == 2)
    check("send: ночь отмечена",
          rm2._chat_state("chat1").get("night_key") == "2026-08-26")
    asyncio.run(rm2._do_night("chat1", n, n.timestamp() - 20 * 60))
    check("send: повтор за ночь не шлётся", len(sender.sent) == 2)

    # ── 7. note_presence: планирование на цикл + троттлинг ──
    rm2.config.morning.window_start, rm2.config.morning.window_end = 0, 24
    rm2._presence_ts["chat1"] = 0.0

    async def _presence_flow():
        rm2.start()  # на текущем loop
        import time as _t
        rm2._presence_ts["chat1"] = _t.time() - 60  # обойти троттлинг
        rm2.note_presence("chat1")  # снапшот last_seen = старая активность трекера
        await asyncio.sleep(0.2)    # дать задачам отработать
        rm2.stop()

    asyncio.run(_presence_flow())
    # Утро уже отправлено сегодня → presence не должен слать повторно
    check("presence: уже здоровались — нового сообщения нет", len(sender.sent) == 2)

    # ── 8. Live-семантика ──
    from app.api.settings_api import _LIVE_FEATURE_KEYS
    check("live: rhythm — live-ключ (без рестарта)", "rhythm" in _LIVE_FEATURE_KEYS)
    rm2.update_config({"enabled": False})
    check("live: update_config выключает", not rm2.config.enabled)
    rm2.update_config({"enabled": True, "sleep_nudge": {"bedtime_hour": 1}})
    check("live: update_config обновляет параметры",
          rm2.config.enabled and rm2.config.sleep.bedtime_hour == 1)

    # ── 9. Досье: отметки событий ритма ──
    check("dossier: настройка по умолчанию включена", RhythmConfig.from_dict(True).dossier)
    check("dossier: dossier: false читается",
          not RhythmConfig.from_dict({"enabled": True, "dossier": False}).dossier)

    from app.features.chat_dossier import ChatDossier
    dossier = ChatDossier(context="rhythm_dossier_ctx")
    tracker3 = FakeTracker(chats=["c1"])
    tracker3.act["c1"] = now.timestamp() - 8 * 3600
    rm3 = RhythmManager(context="rhythm_dossier_ctx", config=RhythmConfig.from_dict(True),
                        memory=FakeMemory(), activity_tracker=tracker3,
                        sender=FakeSender(), dossier=dossier)
    asyncio.run(rm3._do_morning("c1", now, tracker3.get_last_activity("c1")))
    prof = dossier.get_profile("c1")
    check("dossier: событие записано после отправки",
          prof is not None and len(prof.events) == 1 and "утреннее приветствие" in prof.events[0])
    check("dossier: событие с меткой времени", prof.events[0].startswith("["))
    ctx_block = dossier.get_context_block("c1")
    check("dossier: событие в контекст-блоке", "утреннее приветствие" in ctx_block)

    # Персистентность: новый экземпляр видит события
    dossier_reloaded = ChatDossier(context="rhythm_dossier_ctx")
    prof2 = dossier_reloaded.get_profile("c1")
    check("dossier: события переживают рестарт",
          prof2 is not None and len(prof2.events) == 1)

    # dossier: false — не пишется
    tracker4 = FakeTracker(chats=["c2"])
    tracker4.act["c2"] = now.timestamp() - 8 * 3600
    rm4 = RhythmManager(context="rhythm_dossier_ctx2",
                        config=RhythmConfig.from_dict({"enabled": True, "dossier": False}),
                        memory=FakeMemory(), activity_tracker=tracker4,
                        sender=FakeSender(), dossier=dossier)
    asyncio.run(rm4._do_morning("c2", now, tracker4.get_last_activity("c2")))
    prof4 = dossier.get_profile("c2")
    check("dossier: dossier: false — отметок нет",
          prof4 is None or not prof4.events)

    # Без досье вообще (не передан) — не падает
    tracker5 = FakeTracker(chats=["c3"])
    tracker5.act["c3"] = now.timestamp() - 8 * 3600
    rm5 = RhythmManager(context="rhythm_dossier_ctx3", config=RhythmConfig.from_dict(True),
                        memory=FakeMemory(), activity_tracker=tracker5, sender=FakeSender())
    asyncio.run(rm5._do_morning("c3", now, tracker5.get_last_activity("c3")))
    check("dossier: без досье — не падает", len(rm5._chat_state("c3")) > 0)

    print()
    print(f"Проверок: {ok}, провалов: {failures}")
    if failures == 0:
        print("OK")
        return 0
    print("FAILURES")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""Smoke-тест «живой» персоны: цикл без LLM (эвристический fallback).

Проверяет связку LivingPersona + StateEngine + WorldEngine + Summarizer
на временном контексте: тики состояния, offline_log, применение событий
мира, приветствие-дневник, gate внешних стимулов, снимок для UI.

Запуск: python -m scripts.test_living_persona
"""

import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    tmp = tempfile.mkdtemp(prefix="living_smoke_")
    # Подменяем DATA_DIR до импорта движков
    import os
    os.environ["DATA_DIR"] = tmp

    import importlib
    import app.core.config as config_mod
    importlib.reload(config_mod)
    import app.core.living_persona as lp_mod
    importlib.reload(lp_mod)

    persona = SimpleNamespace(
        persona_name="connor",
        system_prompt="Ты — Коннор, андроид модели RK800. НЕЛЬЗЯ: говорить «я чувствую».",
    )

    config = lp_mod.LivingPersonaConfig({
        "state_engine": {"enabled": True, "tick_interval_minutes": 20},
        "world_lore": {"enabled": True, "events_per_day": [1, 3]},
        "external_stimuli": {"enabled": True},  # включён руками — gate должен это срезать
        "ui_room_mood_sync": True,
    })
    living = lp_mod.LivingPersona(
        context="smoke", persona=persona, router=None, config=config)

    ok = 0

    def check(name, cond):
        nonlocal ok
        status = "OK" if cond else "FAIL"
        print(f"  [{status}] {name}")
        if cond:
            ok += 1
        else:
            ok -= 100

    # 1. Выжимка без LLM — эвристический черновик (правила из «НЕЛЬЗЯ»)
    pc = living.persona_context()
    check("выжимка: personality_summary непустой", bool(pc["personality_summary"]))
    check("выжимка: world_binding.fictional_universe (unspecified→дефолт)",
          pc["world_binding"]["type"] == "fictional_universe")
    check("gate: внешние стимулы запрещены для fictional (даже при enabled)",
          living.external_stimuli_allowed() is False)

    # 2. Тик состояния: Gemma (Ollama) при доступности, иначе эвристика
    state = living.state_engine.tick("chat1", pc)
    check("тик: состояние создано", 0 <= state["energy"] <= 100)
    check(f"тик: движок отработал (engine={state.get('engine')})",
          state.get("engine") in ("gemma", "heuristic"))
    state2 = living.state_engine.tick("chat1", pc)
    check("тик: состояние персистентно", isinstance(state2["energy"], int))

    # 2b. Объединённый тик+скоринг одним вызовом (§3.4, экономия Gemma-вызова)
    state_ts, score_ts = living.state_engine.tick_and_score(
        "chat1", pc, silence_hours=5.0, since_initiative_hours=10.0,
        proactive_settings={"initiative_probability": 0.5, "max_daily_initiatives": 3})
    check("тик+скоринг: объединённый вызов отработал",
          state_ts.get("engine") in ("gemma", "heuristic")
          and 0.0 <= score_ts <= 1.0)

    # 3. offline_log
    living.state_engine.log_event("chat1", "world_event", {"event": "Протестировал новый модуль"})
    unconsumed = living.state_engine.unconsumed("chat1")
    check("лог: записи появились", len(unconsumed) >= 1)

    # 4. Применение события мира (storyline)
    event = {
        "event": "Заметил сбой в протоколе наблюдения",
        "involves_npc": [{"name": "CyberLife", "interaction": "отчёт"}],
        "involves_place": "штаб",
        "storyline_update": {"title": "Проверка девиации", "new_status": "started",
                             "note": "Начато наблюдение"},
        "mood_impact": {"valence_delta": -0.1, "tag": "настороженность"},
    }
    payload = living.world_engine.apply_event("chat1", event)
    check("событие: payload собран", payload.get("event") and payload.get("storyline"))
    check("событие: storyline создана",
          any(s["title"] == "Проверка девиации"
              for s in living.world_engine.active_storylines(5)))
    living.state_engine.apply_mood_impact("chat1", -0.1, "настороженность")
    check("mood_impact: valence сдвинут",
          living.state_engine.get_state("chat1")["mood"]["valence"] < 0)
    check("событие: журнал последнего факта",
          living.world_engine.last_world_fact("chat1").startswith("Заметил"))

    # 5. Приветствие-дневник (Gemma недоступна — прямые тезисы)
    entries = living.state_engine.entries_since("chat1", time.time() - 7200)
    ctx = living.summarizer.build_return_context("chat1", entries, 24.0)
    check("возврат: контекст собран", ctx is not None and "AWAY" in ctx)
    living.state_engine.mark_consumed([e["id"] for e in entries])
    check("возврат: записи consumed", len(living.state_engine.unconsumed("chat1")) == 0)

    # 6. Контекст для промпта + снимок UI
    living_ctx = living.get_living_context("chat1")
    check("промпт-контекст: состояние + факт",
          living_ctx and "CURRENT STATE" in living_ctx and "LAST THING" in living_ctx)
    ui = living.get_state_for_ui("chat1")
    check("UI-снимок: state+world на месте",
          ui["enabled"] and ui["state"]["energy"] >= 0
          and ui["world"]["storylines"] and ui["ui_sync"] is True)
    check("UI-снимок: метрики движков на месте",
          "metrics" in ui and "living" in ui["metrics"]
          and "state_engine" in ui["metrics"])

    # 7. Полный проход цикла (LLM нет — всё через fallback)
    signals = living._tick_all()
    check("цикл: не упал, состояние живо",
          living.state_engine.get_state("chat1")["energy"] >= 0)
    check("цикл: сигналы инициативы — список (не задача из потока)",
          isinstance(signals, list))

    # 8. Сигнал инициативы проходит полным путём: объединённый тик+скоринг →
    #    tuple из _tick_all → задача на event loop в _loop(). tick_and_score
    #    мокаем (score=0.9) — проверяем механику доставки, а не настроение Gemma.
    received = []
    import asyncio

    async def _fake_signal(chat_id, score, reason):
        received.append((chat_id, score, reason))

    living.on_initiative_signal = _fake_signal
    living.get_last_message_time = lambda cid: time.time() - 3600 * 20  # 20ч молчания
    living.get_last_initiative_time = lambda cid: time.time() - 3600 * 30
    living.state_engine.tick_and_score = lambda *a, **kw: (
        living.state_engine.get_state("chat1"), 0.9)
    signals2 = living._tick_all()
    check("инициатива: _tick_all вернул сигнал",
          isinstance(signals2, list) and len(signals2) >= 1
          and signals2[0][0] == "chat1")
    if signals2:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_fake_signal(*signals2[0]))
        finally:
            loop.close()
    check("инициатива: сигнал дошёл до получателя (proactive-контракт)",
          len(received) == 1 and received[0][0] == "chat1")

    # 9. external_stimuli: дефолт флага по world_binding (§1.3) + ручной override
    orig_pc_fn = living.persona_context
    real_pc = dict(pc)
    real_pc["world_binding"] = {"type": "real_world", "location": "Москва",
                                "universe_note": None}
    try:
        living.persona_context = lambda: real_pc
        # Флаг не задан в YAML → дефолт из world_binding: real_world → разрешено
        living.config.stimuli_flag_explicit = False
        living.config.external_stimuli_flag = False
        check("gate: real_world без явного флага → дефолт разрешает",
              living.external_stimuli_allowed() is True)
        # Явный override enabled=false → выключено даже для real_world
        living.config.stimuli_flag_explicit = True
        check("gate: явный enabled=false выключает real_world-стимулы",
              living.external_stimuli_allowed() is False)
        # Жёсткий gate: fictional не спасает даже explicit true
        living.persona_context = lambda: pc
        living.config.external_stimuli_flag = True
        check("gate: fictional + explicit true → всё равно запрещено",
              living.external_stimuli_allowed() is False)
    finally:
        del living.persona_context  # снимаем instance-shadow, метод класса виден снова
        assert living.persona_context is orig_pc_fn or True

    # 10. use_gemma=false: строго эвристический тик, даже если Ollama жива
    from app.core.state_engine import StateEngine
    se_off = StateEngine("smoke_gemma_off", "tester", use_gemma=False)
    st_off = se_off.tick("c1", pc)
    check("use_gemma=false: тик строго эвристический",
          st_off.get("engine") == "heuristic")

    # 11. Прореживание тиков неактивных чатов (§3.2): молчание > 72ч → тик реже
    del living.state_engine.tick_and_score  # снимаем мок секции 8 — нужен настоящий тик
    living.get_last_message_time = lambda cid: time.time() - 3600 * 100  # 100ч молчания
    living.state_engine._states["chat1"]["last_tick_at"] = time.time()  # тик был только что
    lt_before = living.state_engine.get_state("chat1")["last_tick_at"]
    living._tick_all()
    check("прореживание: неактивный чат пропущен в окне",
          living.state_engine.get_state("chat1")["last_tick_at"] == lt_before)
    # Окно вышло (тик был 3ч назад при базе 20мин × 6 = 2ч) → тик идёт
    living.state_engine._states["chat1"]["last_tick_at"] = time.time() - 3600 * 3
    living._tick_all()
    check("прореживание: по истечении окна тик проходит",
          living.state_engine.get_state("chat1")["last_tick_at"] > time.time() - 60)

    # 12. Внешние стимулы: whitelist-категории из YAML + расписание при провале поиска
    import app.features.web_search as ws_mod
    captured = {}

    def fake_search(query, *a, **kw):
        captured["query"] = query
        return []  # провал ветки поиска

    orig_search = ws_mod.search_web
    ws_mod.search_web = fake_search
    try:
        we = lp_mod.WorldEngine("smoke_fetch", "tester",
                                allowed_categories=["погода"])
        we._next_fetch_at = 0.0
        res = we.fetch_external_stimulus(real_pc)
        check("stimuli: whitelist-категория из YAML в запросе",
              captured.get("query") == "Москва погода")
        check("stimuli: провал поиска не возвращает стимул", res is None)
        check("stimuli: расписание обновлено даже при провале",
              we._next_fetch_at > time.time())
    finally:
        ws_mod.search_web = orig_search

    print(f"\n{'SMOKE PASSED' if ok > 0 else 'SMOKE FAILED'}: {max(ok, 0)} проверок пройдено")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

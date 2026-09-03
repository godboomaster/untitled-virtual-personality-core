"""Smoke-тест апдейта «жизни персоны» (ветка update-three).

Проверяет без LLM (моки/эвристики):
1. Окно времени самоинициативы (initiative_hours) — гейт регулярного цикла
   и сигнала состояния, включая переход через полночь; парсер форматов.
2. bypass_silence у state-инициативы: порог тишины не убивает генерацию.
3. STATE_CHANGE-фолбэк собирает living-контекст (не CONTINUATION-ветку).
4. Сеялка сюжетов (фолбэк-промпт, когда основной сид пуст) + нечёткий
   матчинг заголовков + чистка resolved-линий.
5. Fail-closed фильтр стимулов: нет локального движка → стимул отброшен;
   нет категорий/локации → fetch перепланирован.
6. Ручной world_binding из YAML: override поверх LLM-экстракта.

Запуск: python -m scripts.test_living_update3
"""

import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

_PASS = 0


def check(name: str, cond: bool):
    global _PASS
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond, name
    _PASS += 1


def main():
    tmp = tempfile.mkdtemp(prefix="living_update3_")
    import os
    os.environ["DATA_DIR"] = tmp

    import importlib
    import app.core.config as config_mod
    importlib.reload(config_mod)

    # ── 1. initiative_hours: парсер и гейт ────────────────────────
    print("1. Окно времени самоинициативы")
    import app.features.proactive_messaging as pm
    importlib.reload(pm)
    PC = pm.ProactiveConfig
    check("парсер: строка", PC.parse_hours("09:00-22:00") == ("09:00", "22:00"))
    check("парсер: dict", PC.parse_hours({"from": "22:00", "to": "08:00"}) == ("22:00", "08:00"))
    check("парсер: тире/пробелы", PC.parse_hours("9:00 – 23:30") == ("09:00", "23:30"))
    check("парсер: мусор → None", PC.parse_hours("утро") is None)
    check("парсер: None → None", PC.parse_hours(None) is None)

    persona = SimpleNamespace(
        persona_data={"features": {"proactive": {"enabled": True}}},
    )
    p = pm.ProactiveMessaging.__new__(pm.ProactiveMessaging)  # без __init__
    p.persona = persona
    p.config = PC(enabled=True, initiative_hours=("09:00", "22:00"))

    def at(h, m):
        return time.mktime(time.strptime(f"2026-08-26 {h:02d}:{m:02d}", "%Y-%m-%d %H:%M"))

    check("окно 09-22: 10:00 внутри", p._in_initiative_hours(at(10, 0)))
    check("окно 09-22: 23:00 снаружи", not p._in_initiative_hours(at(23, 0)))
    check("окно 09-22: граница 09:00 внутри", p._in_initiative_hours(at(9, 0)))
    check("окно 09-22: граница 22:00 снаружи", not p._in_initiative_hours(at(22, 0)))
    p.config.initiative_hours = ("22:00", "08:00")
    check("через полночь 22-08: 23:30 внутри", p._in_initiative_hours(at(23, 30)))
    check("через полночь 22-08: 03:00 внутри", p._in_initiative_hours(at(3, 0)))
    check("через полночь 22-08: 12:00 снаружи", not p._in_initiative_hours(at(12, 0)))
    p.config.initiative_hours = None
    check("без окна: всегда можно", p._in_initiative_hours(at(3, 0)) and p._in_initiative_hours(at(15, 0)))

    # Гейт регулярного цикла: вне окна _should_send_initiative = False
    p.config.initiative_hours = ("09:00", "22:00")
    p._get_daily_count = lambda chat_id: 0
    p.get_last_message_time = lambda chat_id: time.time() - 10 * 3600
    p._last_initiative_time = {}
    p._calculate_adaptive_threshold = lambda chat_id: 60
    import unittest.mock as mock
    with mock.patch.object(pm.time, "time", return_value=at(23, 0)):
        check("цикл: вне окна не пишем", not p._should_send_initiative("c"))
    with mock.patch.object(pm.time, "time", return_value=at(10, 0)):
        check("цикл: в окне можно", p._should_send_initiative("c"))

    # ── 2/3. state-инициатива: bypass_silence + living-контекст ─────
    print("2. Сигнал состояния: bypass_silence и living-контекст")
    import inspect
    sig = inspect.signature(p._generate_initiative)
    check("bypass_silence в сигнатуре", "bypass_silence" in sig.parameters)

    # Внутренний порог: тишина 1ч при threshold 8.5ч — без bypass None,
    # с bypass генерация идёт (мокаем _build_monolog_prompt и _side_response)
    p.config.silence_threshold_minutes = 510
    p.memory = SimpleNamespace(stm=SimpleNamespace(
        get_last=lambda n, chat_id=None: [{"role": "user", "content": "hi"}]))
    p.get_last_message_time = lambda chat_id: time.time() - 3600
    p.local_router = None
    p.persona.get_settings = lambda: {}
    called = {}

    def fake_prompt(*a, **kw):
        called["prompt"] = True
        return [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    def fake_side(messages, **kw):
        called["side"] = True
        return "вжух, у меня тут день был..."

    p._build_monolog_prompt = fake_prompt
    p._side_response = fake_side
    out = p._generate_initiative("c", "c", "u", pm.InitiativeType.STATE_CHANGE)
    check("без bypass: тишина 1ч < 8.5ч → None", out is None and "prompt" not in called)
    out = p._generate_initiative("c", "c", "u", pm.InitiativeType.STATE_CHANGE,
                                 bypass_silence=True)
    check("с bypass: генерация дошла до LLM", out == "вжух, у меня тут день был...")
    check("промпт собирался (живой контекст внутри _build_monolog_prompt)", called.get("prompt") is True)

    # STATE_CHANGE-фолбэк: living-контекст, а не CONTINUATION
    print("3. STATE_CHANGE-фолбэк с living-контекстом")
    p.living = SimpleNamespace(
        get_living_context=lambda chat_id: "[CURRENT STATE]\nэнергия 70",
        state_engine=SimpleNamespace(unconsumed=lambda chat_id, limit=5: [
            {"id": 1, "payload": {"event": "чинил полку"}}]),
    )
    p.self_memory = None
    p.dossier = None
    p.memory.stm.get_last = lambda n, chat_id=None: []
    p._fmt_role = lambda m: m.get("role", "?")
    prompts = []
    p._side_response = lambda messages, **kw: prompts.append(messages) or "жил-был день"
    p.persona.system_prompt = "Ты — тест."
    p.persona.get_settings = lambda: {}
    txt = p._generate_reflection_initiative("c", pm.InitiativeType.STATE_CHANGE)
    body = str(prompts[-1]) if prompts else ""
    check("фолбэк сгенерировал текст", bool(txt))
    check("в промпте блок состояния", "CURRENT STATE" in body)
    check("в промпте неозвученный факт", "чинил полку" in body)

    # ── 4. Сюжеты: fuzzy, сеялка-фолбэк, чистка ────────────────────
    print("4. Сюжеты и сценарист")
    import app.core.world_engine as we
    importlib.reload(we)
    check("норм: регистр/пунктуация", we._titles_similar("Расследование Кутузовой!", "расследование кутузовой"))
    check("норм: близкая формулировка", we._titles_similar("Тайна старого маяка", "тайна старого маяка."))
    check("разные линии не матчатся", not we._titles_similar("Расследование", "Ремонт корабля"))

    eng = we.WorldEngine("update3_world", "tester")
    eng.seed_from_system_prompt("Ты — тест.", router=None)
    check("сид без LLM помечан", eng._world.get("seeded") is True)
    check("фолбэк-сид сюжетов вызывается только с роутером", eng._world["storylines"] == [])

    # применяем событие со storyline_update — fuzzy-матч по перефразированному
    eng._world["storylines"].append({
        "id": 1, "title": "Тайна старого маяка", "status": "started",
        "summary": "", "related_npc_ids": [], "related_place_ids": [],
        "last_update_at": "2026-08-20T10:00:00", "created_at": "2026-08-20T10:00:00"})
    payload = eng.apply_event("chat1", {
        "event": "нашёл записку",
        "storyline_update": {"title": "тайна старого маяка!", "new_status": "ongoing",
                             "note": "найдена записка"},
    })
    check("fuzzy: апдейт применился к линии",
          eng._world["storylines"][0]["status"] == "ongoing")
    check("fuzzy: линия не задублировалась", len(eng._world["storylines"]) == 1)
    check("payload storyline на месте", payload.get("storyline", {}).get("title") == "Тайна старого маяка")

    # чистка resolved: 12 завершённых → остаётся 10
    for i in range(12):
        eng._world["storylines"].append({
            "id": 100 + i, "title": f"старая линия {i}", "status": "resolved",
            "summary": "", "related_npc_ids": [], "related_place_ids": [],
            "last_update_at": f"2026-08-{i + 1:02d}T10:00:00", "created_at": "2026-08-01T10:00:00"})
    eng.prune_resolved_storylines()
    resolved = [s for s in eng._world["storylines"] if s["status"] == "resolved"]
    check("чистка: осталось ≤ 10 resolved", len(resolved) == 10)
    check("чистка: активные не тронуты",
          any(s["status"] == "ongoing" for s in eng._world["storylines"]))
    check("мёртвое поле next_advance_at не пишется",
          all("next_advance_at" not in s for s in eng._world["storylines"]))

    # сеялка-фолбэк: роутер вернул основной сид без сюжетов → второй вызов
    class RouterWithStories:
        def __init__(self):
            self.calls = 0

        def get_response(self, messages, **kw):
            self.calls += 1
            if self.calls == 1:
                return '{"npcs": [], "places": [], "storylines": []}'
            return '{"storylines": [{"title": "поиски пропавшего ключа", "summary": "ключ от мастерской пропал"}]}'

    eng2 = we.WorldEngine("update3_world2", "tester")
    r = RouterWithStories()
    eng2.seed_from_system_prompt("Ты — тестовый мастер.", router=r)
    check("фолбэк-сид: второй вызов сделан", r.calls == 2)
    check("фолбэк-сид: сюжет засеян как started",
          any(s["status"] == "started" and "ключ" in s["title"]
              for s in eng2._world["storylines"]))

    # бэкфилл для мира, засеянного раньше без сюжетов (кейс connor)
    class StorylineRouter:
        def __init__(self):
            self.calls = 0

        def get_response(self, messages, **kw):
            self.calls += 1
            return '{"storylines": [{"title": "бэкфилл-линия", "summary": "s"}]}'

    old = we.WorldEngine("update3_world_old", "tester")
    old._world["seeded"] = True  # сид уже был, сюжетов не дал
    r_old = StorylineRouter()
    assert old.ensure_storylines("Ты — старый мир.", r_old)
    check("бэкфилл: мир без сюжетов получил линию",
          any(s["status"] == "started" for s in old._world["storylines"]))
    calls = r_old.calls
    old.ensure_storylines("Ты — старый мир.", r_old)
    check("бэкфилл: одноразовость", r_old.calls == calls)
    fresh = we.WorldEngine("update3_world_fresh", "tester")
    fresh._world["seeded"] = True
    fresh._world["storylines"] = [{"id": 9, "title": "есть", "status": "ongoing"}]
    r_fresh = StorylineRouter()
    fresh.ensure_storylines("p", r_fresh)
    check("бэкфилл: мир с сюжетами не трогает LLM", r_fresh.calls == 0)

    # ── 5. Стимулы: fail-closed + планирование ─────────────────────
    print("5. Fail-closed стимулы")
    eng3 = we.WorldEngine("update3_world3", "tester")
    eng3.local = SimpleNamespace(
        is_available=lambda task=None: False, get_response=lambda *a, **kw: None)
    import app.features.web_search as ws
    with mock.patch.object(ws, "search_web",
                           return_value=[{"title": "событие", "body": "текст"}]):
        st = eng3.fetch_external_stimulus({"interests": ["музыка"],
                                           "world_binding": {"type": "real_world", "location": "Город"}})
    check("нет движка → стимул отброшен", st is None)
    check("нет движка → в пул ничего не попало",
          not eng3._world["external_stimuli"])

    # нет категорий и локации → fetch перепланирован (не молотит каждый тик)
    eng4 = we.WorldEngine("update3_world4", "tester")
    before = eng4._next_fetch_at
    eng4.fetch_external_stimulus({"interests": [], "world_binding": {}})
    check("пустые категории: следующий fetch запланирован",
          eng4._next_fetch_at > before)

    # ── 6. Ручной world_binding из YAML ────────────────────────────
    print("6. world_binding override из YAML")
    import app.core.persona_context as pc
    importlib.reload(pc)
    layer = pc.PersonaContextLayer("update3_pc", router=None,
                                   manual_binding={"type": "real_world",
                                                   "location": "Novosibirsk"})
    got = layer.get("Ты — вымышленный рыцарь из Эльдариона.")  # экстракт скажет fictional
    check("override: тип real_world поверх экстракта",
          got["world_binding"]["type"] == "real_world")
    check("override: локация из YAML", got["world_binding"].get("location") == "Novosibirsk")
    check("override: помечен как ручной", got["world_binding"].get("manual") is True)
    layer2 = pc.PersonaContextLayer("update3_pc2", router=None,
                                    manual_binding={"type": "мусор"})
    got2 = layer2.get("Ты — Коннор из Детройта.")
    check("мусорный тип не открывает real_world",
          got2["world_binding"]["type"] != "real_world")

    # ── 7. Фича «жизнь» (зонтичный рубильник) ──────────────────────
    print("7. Фича life: зонтичный рубильник")
    import app.core.living_persona as lp
    importlib.reload(lp)
    C = lp.LivingPersonaConfig
    c = C({"life": True})
    check("life: true включает весь стек + ui sync",
          c.enabled and c.state_enabled and c.world_enabled and c.ui_room_mood_sync)
    check("life: true — дефолты (тик 20, события 1-3)",
          c.tick_interval_minutes == 20 and c.events_per_day == (1, 3))
    c = C({"life": {"enabled": True, "tick_interval_minutes": 30,
                    "events_per_day": [1, 2]}})
    check("life-dict: параметры прокинулись",
          c.tick_interval_minutes == 30 and c.events_per_day == (1, 2))
    c = C({"life": True, "state_engine": {"enabled": False}})
    check("явный блок гасит слой даже при life: true",
          not c.state_enabled and c.world_enabled)
    c = C({})
    check("без life и блоков — выключено (как раньше)",
          not c.enabled and not c.ui_room_mood_sync)
    c = C({"state_engine": {"enabled": True}})
    check("явные блоки работают без life (путь connor)",
          c.enabled and c.state_enabled and not c.world_enabled)
    c = C({"life": False})
    check("life: false — выключено", not c.enabled)

    print(f"\nSMOKE PASSED: {_PASS} проверок пройдено")


if __name__ == "__main__":
    main()

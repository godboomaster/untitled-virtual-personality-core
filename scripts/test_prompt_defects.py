"""Тест дефектов промптов (без LLM, фейковый локальный роутер).

Проверяет:
1. Промпт извлечения фактов (memory_config.build_extraction_prompt):
   в отрендеренном тексте нет висящих кавычек от строковых литералов
   внутри f-строки, блоки WRONG/CORRECT и примеры на месте.
2. Мёртвые шаблоны удалены: _MOMENTS_PROMPT/extract_moments (отношения) и
   _DIALOGUE_DETECT_PROMPT/detect_from_dialogue (мир) — их работу делает
   общий урожай диалога; ссылок на них в app/ и scripts/ не осталось.
3. Живой путь — LivingPersona._harvest_dialogue: один вызов
   dialogue_harvest, промпт по-английски с user_language_line в конце,
   NPC/места → миру, моменты/темы/позиции → отношениям (и дальше в
   контекст ответа и снимок UI); primitive — без NPC/мест и моментов.

Запуск: python -m scripts.test_prompt_defects
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    if cond:
        ok += 1
    else:
        failures += 1


# ── 1. Промпт извлечения фактов ──────────────────────────────

def test_extraction_prompt():
    print("1. Промпт извлечения фактов")
    from app.core.memory_config import (
        build_extraction_prompt, POSITIVE_EXAMPLES, NEGATIVE_EXAMPLES)

    for label, stm in (("без контекста", None),
                       ("с контекстом STM", "User: привет\nAssistant: привет!")):
        prompt = build_extraction_prompt("Меня зовут Ваня, живу в Казани", stm)
        lines = prompt.splitlines()
        check(f"{label}: нет строк из одной кавычки",
              not any(l.strip() == '"' for l in lines))
        check(f"{label}: нет хвостов строковых литералов (\\n\" / начало с \")",
              '\\n"' not in prompt
              and not any(l.lstrip().startswith('"WRONG') for l in lines))
        check(f"{label}: в каждой строке кавычки парные",
              all(l.count('"') % 2 == 0 for l in lines))
        check(f"{label}: шаблон отрендерен целиком (нет {{...}})",
              not re.search(r"\{[a-z_]+\}", prompt))

        try:
            i = lines.index("WRONG output (never do this):")
        except ValueError:
            i = -1
        check(f"{label}: блок WRONG — заголовок отдельной строкой", i >= 0)
        if i >= 0:
            check(f"{label}: блок WRONG — оба плохих примера сразу за заголовком",
                  lines[i + 1] == "  Name: Ivan, Pets: No_pets, Music: not mentioned, Goals: unknown"
                  and lines[i + 2] == "  Hobby: guitar, reading, yoga"
                  and lines[i + 3] == "")
            check(f"{label}: блок CORRECT следует за WRONG с верным примером",
                  lines[i + 4].startswith("CORRECT output for same input")
                  and lines[i + 5] == "  Name: Ivan, Hobby_music: guitar, "
                                      "Hobby_reading: reading, Hobby_fitness: yoga")

        pos_at = prompt.find("EXAMPLES — extract facts:")
        neg_at = prompt.find("EXAMPLES — no facts:")
        check(f"{label}: блоки примеров на месте и по порядку",
              0 < pos_at < neg_at)
        check(f"{label}: все позитивные примеры в своём блоке",
              all(f'  "{m}" → {f}' in prompt[pos_at:neg_at]
                  for m, f in POSITIVE_EXAMPLES.items()))
        check(f"{label}: все негативные примеры в своём блоке",
              all(f'  "{m}" → {f}' in prompt[neg_at:]
                  for m, f in NEGATIVE_EXAMPLES.items()))
        check(f"{label}: реплика пользователя в кавычках перед Answer:",
              prompt.endswith('"Меня зовут Ваня, живу в Казани"\nAnswer:'))
        check(f"{label}: блок контекста только когда он передан",
              ("RECENT CONVERSATION CONTEXT" in prompt) == bool(stm))


# ── 2. Мёртвые шаблоны удалены ───────────────────────────────

_DEAD_NAMES = ("_MOMENTS_PROMPT", "extract_moments",
               "_DIALOGUE_DETECT_PROMPT", "detect_from_dialogue",
               "DETECT_THROTTLE_SEC")


def test_dead_templates_removed():
    print("2. Мёртвые шаблоны удалены")
    import app.core.relationship as rel_mod
    import app.core.world_engine as we_mod
    check("relationship: нет _MOMENTS_PROMPT и extract_moments",
          not hasattr(rel_mod, "_MOMENTS_PROMPT")
          and not hasattr(rel_mod.RelationshipMemory, "extract_moments"))
    check("world_engine: нет _DIALOGUE_DETECT_PROMPT и detect_from_dialogue",
          not hasattr(we_mod, "_DIALOGUE_DETECT_PROMPT")
          and not hasattr(we_mod.WorldEngine, "detect_from_dialogue"))

    me = Path(__file__).resolve()
    hits = []
    for base in (ROOT / "app", ROOT / "scripts"):
        for path in base.rglob("*.py"):
            if path.resolve() == me:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name in _DEAD_NAMES:
                if name in text:
                    hits.append(f"{path.relative_to(ROOT)}: {name}")
    check("в app/ и scripts/ не осталось ссылок на удалённое"
          + (f" ({'; '.join(hits)})" if hits else ""), not hits)

    import app.core.living_persona as lp_mod
    tpl = lp_mod._HARVEST_PROMPT
    check("урожай диалога покрывает обе задачи (NPC/места + моменты/темы/позиции)",
          all(f'"{k}"' in tpl for k in ("new_npcs", "new_places", "moments",
                                         "topics", "stance_changes")))
    check("урожай диалога: шаблон по-английски, в конце {language_line}",
          not re.search(r"[А-Яа-яЁё]", tpl)
          and tpl.rstrip().endswith("{language_line}"))


# ── 3. Живой путь: урожай диалога ────────────────────────────

_HARVEST_PAYLOAD = {
    "new_npcs": [{"name": "Ваня", "role": "друг пользователя",
                  "context": "ходили вместе в кафе"}],
    "new_places": [{"name": "Кафе Пушкин", "type": "кафе",
                    "context": "там спорили о музыке"}],
    "mood_impact": {"valence_delta": 0.1, "tag": "тепло"},
    "moments": ["Вечный спор Битлз против Квин"],
    "topics": ["музыка семидесятых"],
    "stance_changes": [{"topic": "Битлз или Квин", "position": "за Квин"}],
}

_DIALOG = [
    {"role": "user", "content": "Вчера с другом Ваней ходили в кафе Пушкин"},
    {"role": "assistant", "content": "О, и как там? Опять спорили?"},
    {"role": "user", "content": "Да, опять — кто лучше, Битлз или Квин"},
    {"role": "assistant", "content": "Я всё ещё за Квин, ты же знаешь"},
]


class _HarvestLocal:
    # Фейковый локальный роутер: ответ урожая, запись вызовов
    def __init__(self):
        self.calls = []

    def is_available(self, *a, **kw):
        return True

    def get_response(self, messages=None, **kw):
        self.calls.append((kw.get("task"), messages))
        if kw.get("task") == "dialogue_harvest":
            return json.dumps(_HARVEST_PAYLOAD, ensure_ascii=False)
        return None


def _stub_local_router(stub):
    # get_local_router → stub во всех модулях app.* (как в test_living_persona)
    import app.core.local_router as lr_mod
    orig = lr_mod.get_local_router

    def fake(context=None):
        return stub

    def swap(old, new):
        for name, mod in list(sys.modules.items()):
            if ((name == "app" or name.startswith("app."))
                    and getattr(mod, "get_local_router", None) is old):
                mod.get_local_router = new

    swap(orig, fake)
    return lambda: swap(fake, orig)


def _make_living(lp_mod, context, intellect=None):
    persona = SimpleNamespace(persona_name="Коннор",
                              system_prompt="Ты — Коннор, андроид.")
    cfg = lp_mod.LivingPersonaConfig({
        "state_engine": {"enabled": True, "tick_interval_minutes": 20},
        "world_lore": {"enabled": True, "events_per_day": [1, 3]},
    })
    living = lp_mod.LivingPersona(context=context, persona=persona,
                                  router=None, config=cfg, intellect=intellect)
    # Выжимка персоны не нужна урожаю — без похода в движки
    living.persona_context = lambda: {"personality_summary": "андроид-детектив"}
    return living


def test_harvest_live_path():
    print("3. Живой путь: урожай диалога")
    import app.core.living_persona as lp_mod
    from app.core.intellect import IntellectConfig
    from app.core.language import user_language_line

    fake = _HarvestLocal()
    restore = _stub_local_router(fake)
    try:
        living = _make_living(lp_mod, "prompt_defects_norm")
        living._harvest_dialogue("c1", _DIALOG)
        harvest_calls = [c for c in fake.calls if c[0] == "dialogue_harvest"]
        check("один вызов LLM на урожай (task=dialogue_harvest), других нет",
              len(fake.calls) == 1 and len(harvest_calls) == 1)
        prompt = harvest_calls[0][1][-1]["content"] if harvest_calls else ""
        check("промпт урожая кончается user_language_line языка диалога (ru)",
              prompt.rstrip().splitlines()[-1] == user_language_line("ru")
              if prompt else False)

        world = living.world_engine.get_world_snapshot()
        check("мир: NPC из диалога заведён (origin=detected_from_dialogue)",
              any(n["name"] == "Ваня" and n.get("origin") == "detected_from_dialogue"
                  for n in world["npcs"]))
        check("мир: место из диалога заведено",
              any(p["name"] == "Кафе Пушкин" for p in world["places"]))

        rel = living.relationship.get_snapshot("c1") or {}
        check("отношения: момент/тема/позиция записаны",
              "Вечный спор Битлз против Квин" in rel.get("shared_moments", [])
              and "музыка семидесятых" in rel.get("shared_topics", [])
              and any(s["topic"] == "Битлз или Квин" for s in rel.get("stances", [])))
        block = living.relationship.get_context_block("c1") or ""
        check("отношения: момент доходит до контекста ответа",
              "Вечный спор Битлз против Квин" in block)
        ui = living.get_state_for_ui("c1")
        check("отношения: момент виден в снимке для UI",
              "Вечный спор Битлз против Квин"
              in ((ui.get("relationship") or {}).get("shared_moments") or []))

        # primitive: урожай идёт (mood), но NPC/места и моменты не заводятся
        fake.calls.clear()
        prim = IntellectConfig({"features": {"self_memory": True},
                                "intellect": {"tier": "primitive"}})
        living_p = _make_living(lp_mod, "prompt_defects_prim", intellect=prim)
        check("primitive: слой мира включён частично (проверка осмысленна)",
              living_p.primitive and living_p.config.world_enabled)
        living_p._harvest_dialogue("c1", _DIALOG)
        world_p = living_p.world_engine.get_world_snapshot()
        check("primitive: NPC/места из урожая не заводятся",
              world_p["npcs"] == [] and world_p["places"] == [])
        rel_p = living_p.relationship.get_snapshot("c1") or {}
        check("primitive: моменты/темы из урожая не заводятся",
              not rel_p.get("shared_moments") and not rel_p.get("shared_topics"))
    finally:
        restore()


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="prompt_defects_")
    import importlib
    import app.core.config as config_mod
    importlib.reload(config_mod)

    test_extraction_prompt()
    test_dead_templates_removed()
    test_harvest_live_path()

    print(f"\nИтого: OK={ok}, FAIL={failures}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()

"""Smoke-тест системы уровней интеллекта (intellect tiers).

Проверяет: IntellectConfig (дефолты/overrides/legacy), стилевые модификаторы
помощи + Gemma-детекцию, примитивный режим self_memory / world / state /
proactive / reminder, применение inventory_action офлайн-событием.

Запуск: python -m scripts.test_intellect
"""

import sys
import tempfile
import os
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="intellect_smoke_")
    import importlib
    import app.core.config as config_mod
    importlib.reload(config_mod)

    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    # ── 1. IntellectConfig: legacy / дефолты / overrides (§2) ──
    from app.core.intellect import IntellectConfig

    legacy = IntellectConfig({"features": {"self_memory": True}})
    check("legacy: без блока уровни не активны",
          legacy.active is False and legacy.help_response_style is None
          and legacy.self_memory_mode == "full")

    prim = IntellectConfig({"features": {"self_memory": True},
                            "intellect": {"tier": "primitive"}})
    check("primitive: дефолты (self_memory=primitive, style=action_only)",
          prim.self_memory_mode == "primitive"
          and prim.help_response_style == "action_only")

    norm = IntellectConfig({"features": {"self_memory": True},
                            "intellect": {"tier": "normal"}})
    check("normal: дефолты (full, casual_human)",
          norm.self_memory_mode == "full"
          and norm.help_response_style == "casual_human")

    bot = IntellectConfig({"features": {"self_memory": True},
                           "intellect": {"tier": "bot"}})
    check("bot: дефолты (full, full_assistant)",
          bot.self_memory_mode == "full"
          and bot.help_response_style == "full_assistant")

    ov = IntellectConfig({
        "features": {"self_memory": True},
        "intellect": {"tier": "normal",
                      "overrides": {"self_memory_mode": "none",
                                    "help_response_style": "full_assistant"}}})
    check("overrides: перекрывают дефолты tier",
          ov.self_memory_mode == "none"
          and ov.help_response_style == "full_assistant")

    off = IntellectConfig({"features": {"self_memory": False},
                           "intellect": {"tier": "bot"}})
    check("features.self_memory=false → модуля нет при любом tier",
          off.self_memory_mode == "none")

    # world_lore: full/partial (§3.3)
    check("world: primitive → partial, не full",
          prim.world_lore_full(True) is False and prim.world_lore_partial(True) is True)
    check("world: normal/bot → full",
          norm.world_lore_full(True) is True and bot.world_lore_full(True) is True)
    prim_off = IntellectConfig({
        "features": {}, "intellect": {
            "tier": "primitive",
            "overrides": {"world_lore_enabled": False}}})
    check("world: override false у primitive гасит всё",
          prim_off.world_lore_partial(True) is False
          and prim_off.world_lore_full(True) is False)

    # ── 2. Стилевые модификаторы (§4.2-4.3) ──
    from app.features import help_style

    for style, marker in (("action_only", "НЕ можешь объяснять"),
                          ("casual_human", "как обычный человек"),
                          ("full_assistant", "досконально")):
        block = help_style.build_style_block(style, "готовка")
        check(f"стиль {style}: фрагмент + приоритет + домен",
              block and marker in block and "OVERRIDES" in block and "готовка" in block)

    check("стиль: неизвестный → None", help_style.build_style_block("nope") is None)
    check("пайплайн: legacy-персона → блок не нужен",
          help_style.build_block_for_message("как приготовить пасту?", legacy, None) is None)

    # Gemma-детекция (Ollama доступен в этом окружении; иначе — пропуск)
    from app.core.local_router import get_local_router
    local = get_local_router()
    if local.is_available():
        verdict_help = help_style.detect_help_request(
            "Объясни, чем отличается List от Tuple в питоне", local)
        verdict_chat = help_style.detect_help_request(
            "привет, я сегодня весь день гулял по парку", local)
        check("детекция Gemma: техвопрос = help",
              verdict_help is not None and verdict_help["is_help_request"] is True)
        check("детекция Gemma: болтовня = не help",
              verdict_chat is not None and verdict_chat["is_help_request"] is False)

        block = help_style.build_block_for_message(
            "Как сварить кофе в турке?", prim, local)
        no_block = help_style.build_block_for_message(
            "смотри какую смешную картинку нашёл", prim, local)
        check("пайплайн: help-запрос primitive → action_only подключён",
              block is not None and "action" not in block.lower() or "НЕ можешь объяснять" in (block or ""))
        check("пайплайн: болтовня → модификатор не влезает",
              no_block is None)
    else:
        print("  [SKIP] Gemma недоступна — детекция пропущена")

    # Fallback: Gemma недоступна → ограничивающие стили всё равно применяются
    check("fallback: без Gemma casual_human всё равно применяется",
          help_style.build_block_for_message("как испечь хлеб?", norm, None) is not None)
    check("fallback: без Gemma full_assistant не подставляется зря",
          help_style.build_block_for_message("как испечь хлеб?", bot, None) is None)

    # ── 3. self_memory primitive (§3.1) ──
    import app.core.self_memory as sm_mod
    importlib.reload(sm_mod)

    captured = []

    class CaptureRouter:
        # self_memory зовет LLM через _side_response → нужен active_provider
        active_provider = None

        def get_response(self, messages, **kw):
            captured.append(messages[1]["content"])
            return "Тепло. Дремал у батареи."

    tmp_ctx = tempfile.mkdtemp()
    sm_prim = sm_mod.BotSelfMemory(tmp_ctx, "Мурзик", CaptureRouter(), mode="primitive")
    sm_prim._write_episode([{"role": "user", "content": "привет, я принёс тебе еду",
                             "user_name": "Хозяин"}])
    check("self_memory: primitive-промпт использован",
          captured and "впечатление-вспышку" in captured[-1])
    check("self_memory: эпизод короткий записан",
          sm_prim._episodes["active"] and sm_prim._episodes["active"][-1]["text"] == "Тепло. Дремал у батареи.")

    # Заметки в primitive не пишутся
    sm_prim._notes["notes"] = []
    sm_prim._msg_since_last_note = 100
    sm_prim.tick([{"role": "user", "content": "мне кажется ты меня чувствуешь и скучаешь"}] * 30,
                 "u1", "мне кажется ты меня чувствуешь и скучаешь")
    check("self_memory: заметки-рефлексии в primitive отключены",
          len(sm_prim._notes["notes"]) == 0)

    # life_summary primitive — паттерны
    sm_prim._episodes["archive"] = [{"text": t} for t in
                                    ("Тепло. Дремал.", "Блестит. Хочу.",
                                     "Громко. Спрятался.", "Тепло. Спал.")]
    captured.clear()

    class PatternRouter:
        active_provider = None

        def get_response(self, messages, **kw):
            return '{"patterns": ["любит тепло", "боится громких звуков"]}'

    sm_prim.router = PatternRouter()
    sm_prim._summarize_archive()
    check("self_memory: life_summary primitive = паттерны",
          "любит тепло" in sm_prim._episodes["life_summary"]
          and sm_prim._episodes["archive"] == [])

    # ── 4. world_engine primitive (§3.3-3.4) ──
    import app.core.world_engine as we_mod
    importlib.reload(we_mod)

    class NeverRouter:
        def get_response(self, **kw):
            raise AssertionError("primitive не должен звать основную LLM на засев")

    we_prim = we_mod.WorldEngine("smoke_we", "Мурзик", primitive=True)
    we_prim.seed_from_system_prompt("Ты — кот", NeverRouter())
    check("world: primitive — засев пропущен, без LLM",
          we_prim.seed_from_system_prompt("Ты — кот", NeverRouter()) is False
          and we_prim.get_world_snapshot()["npcs"] == [])
    check("world: primitive — детекция из диалога выключена",
          we_prim.detect_from_dialogue(
              [{"role": "user", "content": "мой друг Ваня зашёл"}]) == 0)

    pc_prim = {"personality_summary": "Домашний кот, любит тепло и блестящее.",
               "behavioral_rules": [], "interests": [],
               "world_binding": {"type": "fictional_universe"}}
    state_prim = {"energy": 60, "mood": {"valence": 0.2, "arousal": 0.4, "tag": "довольно"},
                  "pastime": "грызёт игрушку", "location": "дом"}
    if local.is_available():
        ev = we_prim.generate_offline_event(
            "c1", pc_prim, state_prim, None,
            inventory_items=["Клубок пряжи", "Колокольчик"])
        check("world: primitive-событие сгенерировано",
              ev is not None and ev.get("event"))
        payload = we_prim.apply_event("c1", ev or {})
        has_inv = "inventory_action" in payload
        check(f"world: событие несёт inventory_action={has_inv} (Gemma решает)",
              isinstance(payload, dict) and payload.get("event"))
        print(f"        событие: {payload.get('event', '')[:100]}")
    else:
        print("  [SKIP] Gemma недоступна — генерация события пропущена")

    # ── 5. LivingPersona primitive: применение inventory_action ──
    import app.core.living_persona as lp_mod
    importlib.reload(lp_mod)

    inv_calls = []

    class FakeInventory:
        def get_items(self):
            return [SimpleNamespace(name="Клубок пряжи"), SimpleNamespace(name="Колокольчик")]

        def add_item(self, name, desc="", source=None, expires=None):
            inv_calls.append(("add", name))

        def use_item(self, name):
            inv_calls.append(("use", name))

        def remove_item(self, name):
            inv_calls.append(("remove", name))

        def has_item(self, name):
            return any(i.name == name for i in self.get_items())

    persona = SimpleNamespace(persona_name="Мурзик",
                              system_prompt="Ты — кот Мурзик. Мяукаешь.")
    cfg = lp_mod.LivingPersonaConfig({
        "state_engine": {"enabled": True, "tick_interval_minutes": 20},
        "world_lore": {"enabled": True, "events_per_day": [1, 3]},
    })
    living = lp_mod.LivingPersona(
        context="smoke_lp", persona=persona, router=None, config=cfg,
        intellect=prim, inventory_manager=FakeInventory())
    check("living: primitive-флаги разнесены по движкам",
          living.state_engine.primitive and living.world_engine.primitive
          and living.summarizer.primitive)

    living._apply_inventory_action({"action": "add", "item": "Блестящий ключик",
                                    "description": "блестит"})
    living._apply_inventory_action({"action": "use", "item": "клубок"})
    living._apply_inventory_action({"action": "remove", "item": "Колокольчик"})
    check("living: inventory_action исполняется (add/use/remove)",
          ("add", "Блестящий ключик") in inv_calls
          and ("use", "Клубок пряжи") in inv_calls
          and ("remove", "Колокольчик") in inv_calls)

    # Контекст состояния: primitive не вербализуется рефлексией
    block = living.state_engine.get_state_context_block("c1")
    check("state: контекст primitive запрещает человеческую рефлексию",
          "CANNOT discuss" in block and "PHYSICAL STATE" in block)

    # ── 6. proactive: ограничение типов (§3.2) ──
    from app.features.proactive_messaging import (
        ProactiveMessaging, ProactiveConfig, InitiativeType)
    pm = ProactiveMessaging(
        config=ProactiveConfig(enabled=True),
        router=None, persona=persona, memory=None, activity_tracker=None,
        get_last_message_time=lambda cid: 0, sender=None,
        context="smoke_pm", intellect=prim)
    allowed_types = {InitiativeType.TODO_REFLECTION, InitiativeType.INVENTORY_REFLECTION,
                     InitiativeType.STATE_CHANGE}
    picked = {pm._select_initiative_type("c1") for _ in range(60)}
    check("proactive: primitive выбирает только практические типы",
          picked <= allowed_types and len(picked) >= 1)

    # Монолог содержит примитивную инструкцию
    msgs = pm._build_monolog_prompt(
        [{"role": "user", "content": "привет"}], [], "U", 3.0, "c1",
        InitiativeType.INVENTORY_REFLECTION)
    check("proactive: монолог с PRIMITIVE CREATURE MODE",
          any("PRIMITIVE CREATURE MODE" in m["content"] for m in msgs))

    # Служебный JSON (профиль досье и т.п.) не уходит пользователю как инициатива
    from app.features.proactive_messaging import _looks_like_payload
    check("proactive: JSON-пейлоад отфильтрован",
          _looks_like_payload('{"interests": ["tea"], "topics": []}')
          and _looks_like_payload('["a", "b"]'))
    check("proactive: обычный текст и скобка не режутся",
          not _looks_like_payload("привет, как дела?")
          and not _looks_like_payload("{ну как там дела"))

    # Веб-чат инициатив — свой канал (не общий «side» со строго-форматными задачами)
    class FakeRouter:
        active_provider = "kimi"

        def get_response(self, messages, **kw):
            self.last_kw = kw
            return "ок"

    fr = FakeRouter()
    pm.router = fr
    pm._side_response([{"role": "user", "content": "x"}])
    check("proactive: веб-чат — канал «proactive»",
          fr.last_kw.get("webchat_channel") == "proactive")

    # ── 7. reminder: primitive → без LLM-текста (§3.5) ──
    from app.features.reminder_manager import ReminderManager
    rm = ReminderManager(context="smoke_rm")
    rm.set_intellect_tier("primitive")
    check("reminder: primitive-флаг поднят", rm._primitive is True)
    rm2 = ReminderManager(context="smoke_rm2")
    rm2.set_intellect_tier("bot")
    check("reminder: bot — обычная генерация", rm2._primitive is False)

    print(f"\n{'SMOKE PASSED' if ok > 0 else 'SMOKE FAILED'}: {max(ok, 0)} проверок пройдено")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

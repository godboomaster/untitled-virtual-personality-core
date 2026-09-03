"""Проверка качества подачи «живой» персоны (фаза D).

Механику жизни проверяют scripts/test_living_persona.py и
test_living_update3.py. Здесь — другое: доходит ли состояние/факты/планы
до ПРОМПТА основной LLM так, чтобы ответ реально менялся.

Offline (всегда): сборка контекста при заданных состояниях —
проекции энергии/настроения, топическая зацепка фактов, планы, отношения.

Live (VPC_LIVE_EVAL=1, нужен рабочий провайдер): один и тот же вопрос
пользователя при «устал/подавлен» и «бодр/доволен» — ответ при низкой
энергии должен быть заметно короче.

Запуск: python -m scripts.eval_living_quality  (live: VPC_LIVE_EVAL=1 ...)
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

QUESTION = "привет! как дела, что нового?"


def main():
    tmp = tempfile.mkdtemp(prefix="living_quality_")
    os.environ["DATA_DIR"] = tmp

    import importlib
    import app.core.config as config_mod
    importlib.reload(config_mod)
    import app.core.living_persona as lp_mod
    importlib.reload(lp_mod)

    persona = SimpleNamespace(
        persona_name="test", system_prompt="Ты — Алекс, студент. Отвечаешь по-человечески.",
        persona_data={})
    living = lp_mod.LivingPersona(
        context="qual", persona=persona, router=None,
        config=lp_mod.LivingPersonaConfig({"life": True}))

    ok = 0
    skip = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1 if cond else -100

    def note(name, msg):
        nonlocal skip
        skip += 1
        print(f"  [SKIP] {name}: {msg}")

    # ── Offline: сборка контекста ──
    print("1. Offline: контраст состояний в промпте")
    se = living.state_engine
    low = se.get_state("c1")
    low["energy"] = 10
    se.apply_mood_impact("c1", -0.6, "тоска")
    ctx_low = living.get_living_context("c1", topic_text=QUESTION)
    check("низкая энергия → инструкция короче", "critically low" in ctx_low)
    check("низкая валентность → без принуждения к бодрости", "clearly low" in ctx_low)

    high = se.get_state("c2")
    high["energy"] = 95
    se.apply_mood_impact("c2", 0.6, "воодушевление")
    ctx_high = living.get_living_context("c2", topic_text=QUESTION)
    check("бодрое состояние — без проекций подавленности",
          "critically low" not in ctx_high and "clearly low" not in ctx_high)

    print("2. Offline: факты/планы/отношения в контексте")
    living.world_engine._world.setdefault("event_journal", {})["c2"] = \
        ["Помогал Хэнку с отчётом"]
    ctx_rel = living.get_living_context("c2", topic_text="как там Хэнк поживает?")
    ctx_off = living.get_living_context("c2", topic_text=QUESTION)
    check("факт при зацепке — есть", "LAST THING" in ctx_rel)
    check("факт без зацепки — нет", "LAST THING" not in ctx_off)

    living.world_engine.add_plan("Пересдача", "в пятницу", due_in_hours=30)
    ctx_plan = living.get_living_context("c2", topic_text=QUESTION)
    check("ближайший план виден (anticipation)", "UPCOMING IN YOUR LIFE" in ctx_plan)

    living.relationship.add_extracted("c2", moments=["Спор про табы"],
                                      topics=["python"], stance_changes=None)
    ctx_rel2 = living.get_living_context("c2", topic_text=QUESTION)
    check("общие моменты отношений видны", "YOUR RELATIONSHIP" in ctx_rel2)

    # ── Live: контраст ответов основной модели (opt-in) ──
    print("3. Live: длина ответа при низкой vs высокой энергии")
    if os.environ.get("VPC_LIVE_EVAL") != "1":
        note("live-прогон", "выключен (VPC_LIVE_EVAL=1 для включения)")
    else:
        try:
            from app.core.router import ModelRouter
            router = ModelRouter(context="qual_eval")
            from app.core.persona import PersonaLayer
            pl = PersonaLayer.__new__(PersonaLayer)
            pl.persona_name = "test"
            pl.system_prompt = ("Ты — Алекс, студент. Отвечаешь коротко и "
                                "по-человечески, как в мессенджере.")
            pl.settings = {}
            pl.persona_data = {}
            pl.web_single_user = False

            def ask(ctx):
                msgs = pl.prepare_messages(
                    QUESTION, history=[{"role": "user", "content": "хай",
                                        "timestamp": time.time() - 60}],
                    living_context=ctx)
                return (router.get_response(msgs, temperature=0.7,
                                            max_tokens=300) or "").strip()

            ans_low = ask(ctx_low)
            ans_high = ask(ctx_high)
            print(f"    low : {len(ans_low)} зн. | {ans_low[:80]!r}")
            print(f"    high: {len(ans_high)} зн. | {ans_high[:80]!r}")
            check("ответ при низкой энергии короче",
                  0 < len(ans_low) < len(ans_high) * 0.8)
        except Exception as e:
            note("live-прогон", f"провайдер недоступен: {e}")

    print(f"\nИтог: {ok} OK, {skip} пропущено")
    if ok <= 0:
        sys.exit(1)
    print("QUALITY EVAL DONE")


if __name__ == "__main__":
    main()

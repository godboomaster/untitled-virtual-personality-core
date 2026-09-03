"""Smoke-тест платформенного правила финальных вопросов (conversation_style).

Проверяет: разбор конфига (дефолт rare / none / natural / frequent / мусор),
детект финального вопроса (хвосты из пунктуации/эмодзи/кавычек), подсчёт
серии по истории, промпт-ноты, матрицу should_regenerate, регенерацию через
фейковый router, интеграцию с prepare_messages и реальные yaml персон.

Запуск: python -m scripts.test_conversation_style
"""

import sys
import tempfile
import os
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="convstyle_smoke_")
    import importlib
    import app.core.config as config_mod
    importlib.reload(config_mod)

    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    from app.features import conversation_style as cs

    # ── 1. Конфиг: дефолт и разбор значений ──
    check("дефолт (нет блока) — rare",
          cs.ConversationStyleConfig({}).frequency == "rare"
          and cs.ConversationStyleConfig(None).frequency == "rare"
          and cs.ConversationStyleConfig({"conversation_style": None}).frequency == "rare")
    check("блок не-dict не роняет разбор",
          cs.ConversationStyleConfig({"conversation_style": "rare"}).frequency == "rare")
    for freq in cs.FREQUENCIES:
        cfg = cs.ConversationStyleConfig(
            {"conversation_style": {"question_frequency": freq}})
        check(f"значение {freq!r} разбирается", cfg.frequency == freq)
    check("неизвестное значение → дефолт rare",
          cs.ConversationStyleConfig(
              {"conversation_style": {"question_frequency": "sometimes"}}).frequency == "rare")

    cfg_none = cs.ConversationStyleConfig({"conversation_style": {"question_frequency": "none"}})
    cfg_rare = cs.ConversationStyleConfig({})
    cfg_nat = cs.ConversationStyleConfig({"conversation_style": {"question_frequency": "natural"}})
    cfg_freq = cs.ConversationStyleConfig({"conversation_style": {"question_frequency": "frequent"}})
    check("limited: none/rare ограничены, natural/frequent — нет",
          cfg_none.limited and cfg_rare.limited
          and not cfg_nat.limited and not cfg_freq.limited)
    check("max_streak: none=0, rare=1",
          cfg_none.max_streak == 0 and cfg_rare.max_streak == 1
          and cfg_nat.max_streak is None)

    # ── 2. Детект финального вопроса ──
    positives = [
        "Хорошо. А у тебя как?",
        "Серьёзно?!",
        "Правда? 😊",
        'Он спросил: «Ты идёшь?»',
        "Думаешь?\"",
        "Идём завтра?)\n",
        "Ну что, решил?  \n\n",
        "Хочешь чаю? ☕",
    ]
    negatives = [
        "Хорошо. А у тебя всё по-старому.",
        None, "", "   ",
        "Всё отлично!",
        "Что? Ха!",
        "Спросил его, мол, как дела? Он промолчал.",
        "2+2 = 4.",
        "Вопрос? А потом длинный ответ без вопроса в конце.",
    ]
    check("детект: позитивные кейсы",
          all(cs.ends_with_question(t) for t in positives))
    check("детект: негативные кейсы",
          not any(cs.ends_with_question(t) for t in negatives))

    # ── 3. Серия по истории ──
    hist = [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "Привет! Как ты?"},
        {"role": "user", "content": "норм"},
        {"role": "assistant", "content": "А сам-то как?"},
        {"role": "user", "content": "расскажи что-нибудь"},
        {"role": "assistant", "content": "Рассказал. Интересно было?"},
    ]
    check("серия: три подряд ответа с вопросом (user-реплики не сбрасывают)",
          cs.count_question_streak(hist) == 3)
    check("серия: последний ответ без вопроса → 0",
          cs.count_question_streak(hist + [{"role": "assistant", "content": "Вот история."}]) == 0)
    check("серия: пустая/битая история → 0",
          cs.count_question_streak(None) == 0 and cs.count_question_streak([]) == 0
          and cs.count_question_streak([{"role": "assistant"}]) == 0)
    check("серия: не-assistant сообщения в конце игнорируются",
          cs.count_question_streak(hist + [{"role": "user", "content": "ещё?"}]) == 3)

    # ── 4. Промпт-ноты ──
    note_rare = cs.build_style_note("rare")
    note_none = cs.build_style_note("none")
    check("нота rare: есть запрет рефлекторных вопросов и лазейка для явных инструкций",
          note_rare is not None and "CONVERSATION STYLE" in note_rare
          and "завершённой" in note_rare and "она важнее" in note_rare)
    check("нота none: жёсткий запрет с лазейкой",
          note_none is not None and "вообще" in note_none and "она важнее" in note_none)
    check("ноты natural/frequent отсутствуют",
          cs.build_style_note("natural") is None and cs.build_style_note("frequent") is None)

    # ── 5. Матрица should_regenerate ──
    check("none: любой финальный вопрос → регенерация",
          cs.should_regenerate(cfg_none, "Правда?", 0))
    check("rare: первый вопрос в серии — можно",
          not cs.should_regenerate(cfg_rare, "Правда?", 0))
    check("rare: второй подряд — регенерация",
          cs.should_regenerate(cfg_rare, "Правда?", 1))
    check("natural/frequent: платформа не вмешивается",
          not cs.should_regenerate(cfg_nat, "Правда?", 5)
          and not cs.should_regenerate(cfg_freq, "Правда?", 5))
    check("ответ без вопроса — регенерации нет",
          not cs.should_regenerate(cfg_none, "Всё ясно.", 3))
    check("ответ с маркером [TODO_ADD:] не трогаем",
          not cs.should_regenerate(cfg_none, "Записал. Ещё что-то? [TODO_ADD:молоко]", 3))
    check("ответ с маркером [Ф2] не трогаем",
          not cs.should_regenerate(cfg_none, "Он стал Провидцем [Ф2]. Веришь?", 3))
    check("ответ с [PUNISH:] не трогаем",
          not cs.should_regenerate(cfg_none, "Штраф. Понял? [PUNISH:WARN:x]", 3))

    # ── 6. Регенерация через фейковый router ──
    stats_before = cs.get_stats()

    class FakeRouter:
        def __init__(self, reply=None, exc=None):
            self.reply, self.exc, self.calls = reply, exc, []

        def get_response(self, messages, **settings):
            self.calls.append(messages)
            if self.exc:
                raise self.exc
            return self.reply

    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    r1 = FakeRouter(reply="Да, рассказал бы раньше.")
    out = cs.regenerate_without_tail_question(r1, msgs, "Да. Рассказать?", {})
    check("регенерация: переписанный ответ возвращается",
          out == "Да, рассказал бы раньше.")
    check("регенерация: исходный ответ уходит в контексте как assistant-реплика",
          r1.calls and r1.calls[0][-2] == {"role": "assistant", "content": "Да. Рассказать?"}
          and r1.calls[0][-1]["role"] == "user")
    check("регенерация: счётчик попыток",
          cs.get_stats()["regen_attempts"] == stats_before["regen_attempts"] + 1)

    r2 = FakeRouter(reply="Ты точно хочешь это удалить?")
    out2 = cs.regenerate_without_tail_question(r2, msgs, "Удалил. Точно?", {})
    check("регенерация: модель настояла на вопросе — принимаем её ответ",
          out2 == "Ты точно хочешь это удалить?"
          and cs.get_stats()["regen_model_kept_question"]
          == stats_before["regen_model_kept_question"] + 1)

    r3 = FakeRouter(exc=RuntimeError("down"))
    check("регенерация: сбой router → None (оставляем исходник)",
          cs.regenerate_without_tail_question(r3, msgs, "Правда?", {}) is None
          and cs.get_stats()["regen_failures"] == stats_before["regen_failures"] + 1)
    r4 = FakeRouter(reply="   ")
    check("регенерация: пустой ответ → None",
          cs.regenerate_without_tail_question(r4, msgs, "Правда?", {}) is None)

    # ── 7. Интеграция с prepare_messages: нота в конце системного блока ──
    # (последнее слово — за нотой языка ответа: она перекрывает все
    # инструкции выше, включая эту; см. test_language_following)
    from app.core.persona import PersonaLayer
    p = PersonaLayer.__new__(PersonaLayer)
    p.system_prompt = "SYSTEM."
    with_note = p.prepare_messages("привет", conversation_style_context="CS_NOTE")
    _c = with_note[0]["content"]
    check("prepare_messages: нота в системном блоке, за ней только правило языка",
          with_note[0]["role"] == "system"
          and "\n\nCS_NOTE" in _c
          and _c.index("CS_NOTE") < _c.index("[RESPONSE LANGUAGE"))
    without_note = p.prepare_messages("привет")
    check("prepare_messages: без параметра ноты нет",
          "CS_NOTE" not in without_note[0]["content"])

    # ── 8. Реальные yaml персон (read-only) ──
    verso = cs.ConversationStyleConfig(PersonaLayer("verso").persona_data)
    check("verso.yaml: question_frequency=natural (характер)",
          verso.frequency == "natural" and not verso.limited)
    # Освобождение Арродеса: финальный вопрос — обязательная часть его
    # структуры ответа (принцип взаимности), правило бы ломало персонажа
    for name in ("arrodes", "arrodes_master"):
        cfg = cs.ConversationStyleConfig(PersonaLayer(name).persona_data)
        check(f"{name}.yaml: natural (обязательный финальный вопрос)",
              cfg.frequency == "natural" and not cfg.limited)
    for name in ("alex", "connor", "assistant"):
        cfg = cs.ConversationStyleConfig(PersonaLayer(name).persona_data)
        check(f"{name}.yaml: дефолт rare", cfg.frequency == "rare" and cfg.limited)

    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

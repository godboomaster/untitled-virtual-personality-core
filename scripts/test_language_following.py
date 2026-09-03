"""Smoke-тест правила языка ответа (бот отвечает на языке пользователя).

Проверяет: детект языка сообщения (detect_language), детект по диалогу
с фолбэком на историю этого же отправителя, ноту [RESPONSE LANGUAGE]
в prepare_messages (правильный язык, позиция последней в системном
блоке, отсутствие при неопределённом языке), правило книжного RAG,
yaml персон Арродеса, передачу языка в изолированные реплики обучения
и язык побочных фич: todo-список, напоминания, rhythm, самоинициатива,
дневник (self_memory, offline_summarizer).

Запуск: python -m scripts.test_language_following
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


class _FakeRouter:
    """Ловит сообщения последнего вызова, отвечает заготовкой."""

    def __init__(self, reply="Fine, proceeding!"):
        self.reply = reply
        self.calls = []
        self.active_provider = None

    def get_response(self, messages, **kw):
        self.calls.append(messages)
        return self.reply


def main():
    tmpdir = tempfile.mkdtemp(prefix="lang_smoke_")
    os.environ["DATA_DIR"] = tmpdir
    # learning_manager пишет в относительный data/ — уводим из репозитория
    os.chdir(tmpdir)
    import importlib
    import app.core.config as config_mod
    importlib.reload(config_mod)

    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    from app.core.language import (
        detect_language, detect_dialogue_language, response_language_note)

    # ── 1. detect_language: один текст ──
    print("detect_language:")
    check("чистый русский → ru", detect_language("Привет, как дела?") == "ru")
    check("чистый английский → en", detect_language("Hey, how are you doing?") == "en")
    check("смешанный, перевес ру → ru", detect_language("окей, let's do it по-быстрому") == "ru")
    check("смешанный, перевес en → en", detect_language("давай maybe later, I'm busy now") == "en")
    check("пустая строка → None", detect_language("") is None)
    check("без букв (эмодзи/цифры) → None", detect_language("123 🚀))") is None)
    check("синтетика файла → None", detect_language("The user sent a file: report.pdf") is None)
    check("синтетика картинки → None",
          detect_language("The user sent an image. Its contents according to the vision model: a cat.") is None)

    # ── 2. detect_dialogue_language: текущее сообщение + фолбэк на историю ──
    print("detect_dialogue_language:")
    check("текущее сообщение важнее истории",
          detect_dialogue_language("Hello!", [{"role": "user", "content": "Привет"}]) == "en")
    check("фолбэк на последний язык из истории",
          detect_dialogue_language("🚀🚀", [
              {"role": "assistant", "content": "Hi there"},
              {"role": "user", "content": "Привет, кто ты?"},
          ]) == "ru")
    check("реплики ассистента не считаются",
          detect_dialogue_language("🚀", [{"role": "assistant", "content": "Привет"}]) is None)
    check("в группе — язык того же отправителя",
          detect_dialogue_language("🚀", [
              {"role": "user", "sender_id": 7, "content": "Hello there"},
              {"role": "user", "sender_id": 5, "content": "Привет"},
          ], sender_id=5) == "ru")
    check("синтетика в истории не переключает язык",
          detect_dialogue_language("🚀", [
              {"role": "user", "content": "The user sent a file: x.pdf"},
          ]) is None)
    check("пустой ввод без истории → None", detect_dialogue_language("🚀") is None)

    # ── 3. response_language_note ──
    print("response_language_note:")
    check("ru → нота с Russian", "Russian" in (response_language_note("ru") or ""))
    check("en → нота с English", "English" in (response_language_note("en") or ""))
    check("None → ноты нет", response_language_note(None) is None)

    # ── 4. prepare_messages: нота в системном промпте ──
    print("prepare_messages:")
    from app.core.persona import PersonaLayer
    pl = PersonaLayer("assistant")

    msgs = pl.prepare_messages("Привет, кто ты?", user_id="u1", user_name="Alice")
    sysc = msgs[0]["content"]
    check("нота добавлена для русского сообщения",
          "[RESPONSE LANGUAGE — highest priority rule]" in sysc and "Russian" in sysc)
    check("нота стоит ПОСЛЕДНЕЙ в системном блоке",
          sysc.rstrip().endswith("Do not mix languages in one reply."))
    sysc_style = pl.prepare_messages(
        "Привет", conversation_style_context="[CONVERSATION STYLE — system rule]\nНе задавай вопросов."
    )[0]["content"]
    check("нота перекрывает conversation_style (идёт после него)",
          sysc_style.index("CONVERSATION STYLE") < sysc_style.index("[RESPONSE LANGUAGE"))

    sysc_en = pl.prepare_messages("Hello, who are you?")[0]["content"]
    check("нота с English для английского сообщения",
          "[RESPONSE LANGUAGE" in sysc_en and "English" in sysc_en)

    sysc_none = pl.prepare_messages("🚀🚀")[0]["content"]
    check("неопределённый язык — ноты нет", "[RESPONSE LANGUAGE" not in sysc_none)

    sysc_fb = pl.prepare_messages(
        "🚀🚀", history=[{"role": "user", "content": "Привет, кто ты?", "sender_id": "u1"}],
        user_id="u1")[0]["content"]
    check("фолбэк на историю через prepare_messages", "Russian" in sysc_fb)

    sysc_book = pl.prepare_messages("Привет, что там по ритуалу?", book_context="[КОНТЕКСТ:BOOK]\n...")[0]["content"]
    check("правило книжного RAG больше не требует русского",
          "отвечай на русском" not in sysc_book
          and "Язык ответа — язык сообщения пользователя." in sysc_book)

    # ── 5. Персоны Арродеса без привязки к русскому ──
    print("yaml Арродеса:")
    personas = Path(__file__).parent.parent / "app" / "personas"
    for fname in ("arrodes.yaml", "arrodes_master.yaml"):
        text = (personas / fname).read_text(encoding="utf-8")
        check(f"{fname}: нет «Язык: русский»", "Язык: русский" not in text)
        check(f"{fname}: стиль сохранён", "Речь строгая, лаконичная" in text)

    # ── 6. Изолированные реплики обучения получают язык пользователя ──
    print("learning_manager:")
    from app.features.learning_manager import LearningManager
    lm = LearningManager(context="lang_smoke")
    lm._router = _FakeRouter()

    lm.render_setup_reply("история", "confirmed", "раз в день", user_language="ru")
    setup_sys = lm._router.calls[-1][0]["content"]
    check("render_setup_reply: язык из параметра в промпте",
          "The user's language is Russian." in setup_sys and "Reply ONLY in Russian." in setup_sys)

    lm.render_setup_reply("history", "reask")
    setup_sys_fb = lm._router.calls[-1][0]["content"]
    check("render_setup_reply без языка — старая строка-инструкция",
          "Reply in the language of the user's messages." in setup_sys_fb)

    lm.render_continue_reply("c1", "YES", user_language="en")
    cont_sys = lm._router.calls[-1][0]["content"]
    check("render_continue_reply: язык из параметра в промпте",
          "The user's language is English." in cont_sys and "Reply ONLY in English." in cont_sys)

    # ── 7. Инструкции перегенерации держат язык ──
    print("инструкции перегенерации:")
    from app.features import conversation_style as cs
    check("_REGEN_INSTRUCTION требует сохранить язык", "language" in cs._REGEN_INSTRUCTION)
    import app.bot_instance as bi
    src = Path(bi.__file__).read_text(encoding="utf-8")
    check("continuation требует тот же язык",
          "Continue in the same language as the reply." in src)

    # ── 8. Todo-список: заголовки на языке записей/пользователя ──
    print("todo_manager:")
    from app.features.todo_manager import TodoManager
    tm = TodoManager(context="lang_smoke")
    check("ru-записи → русский заголовок",
          tm._render_list([("Аня", "купить хлеб")]).startswith("Список дел:"))
    check("en-записи → английский заголовок",
          tm._render_list([("Ann", "buy bread")]).startswith("Todo list:"))
    check("явный lang перекрывает язык записей",
          tm._render_list([("Аня", "купить хлеб")], lang="en").startswith("Todo list:"))
    check("пустой список + en → английская заглушка",
          tm._render_list([], lang="en") == "The todo list is empty.")
    check("пустой список без языка → русская заглушка",
          tm._render_list([]) == "Список дел пуст.")

    # ── 9. Напоминания и rhythm: единый детект по репликам пользователя ──
    print("reminder/rhythm:")

    class FakeSTM:
        def __init__(self, msgs):
            self.msgs = msgs

        def get_last(self, n, chat_id=None):
            return self.msgs

    class FakeMemory:
        def __init__(self, msgs):
            self.stm = FakeSTM(msgs)

    from app.features.reminder_manager import ReminderManager
    rm = ReminderManager(context="lang_smoke")
    rm._memory = FakeMemory([{"role": "user", "content": "Hey, remind me later"}])
    check("reminder: язык задачи ru → Russian", rm._reminder_lang("c1", "позвонить маме") == "Russian")
    check("reminder: язык задачи en → English", rm._reminder_lang("c1", "call mom") == "English")
    check("reminder: без задачи — язык истории (en)", rm._reminder_lang("c1", None) == "English")
    rm_syn = ReminderManager(context="lang_smoke")
    rm_syn._memory = FakeMemory([{"role": "assistant", "content": "The user sent a file: x"}])
    check("reminder: синтетика/ассистент не считаются → Russian",
          rm_syn._reminder_lang("c1", None) == "Russian")
    check("reminder: без памяти → Russian",
          ReminderManager(context="lang_smoke")._reminder_lang("c1", None) == "Russian")

    from app.features.rhythm_manager import RhythmManager, RhythmConfig
    ry = RhythmManager(context="lang_smoke", config=RhythmConfig.from_dict({}),
                       memory=FakeMemory([{"role": "user", "content": "Привет, чем занимаешься?"}]))
    check("rhythm: пользователь ru → Russian", ry._lang("c1") == "Russian")
    ry_en = RhythmManager(context="lang_smoke", config=RhythmConfig.from_dict({}),
                          memory=FakeMemory([{"role": "user", "content": "Hey, what's up?"}]))
    check("rhythm: пользователь en → English", ry_en._lang("c1") == "English")
    check("rhythm: без памяти → Russian",
          RhythmManager(context="lang_smoke", config=RhythmConfig.from_dict({}))._lang("c1") == "Russian")

    # ── 10. Самоинициатива: явный язык в промпте монолога ──
    print("proactive:")
    from app.features.proactive_messaging import ProactiveMessaging
    pm = ProactiveMessaging.__new__(ProactiveMessaging)
    pm.persona = SimpleNamespace(system_prompt="SYSTEM")
    pm.self_memory = None
    pm.dossier = None
    pm.living = None
    pm._primitive = False
    pm._get_recent_initiatives_text = lambda chat_id: ""
    pm._get_forbidden_topics_text = lambda chat_id: ""
    pm._get_emotional_state = lambda chat_id: ""
    pm._get_ignore_context = lambda chat_id: ""
    msgs_pm = pm._build_monolog_prompt(
        [{"role": "user", "content": "Hey, what's up?"}],
        [{"role": "user", "content": "Hey, what's up?"}], "Ann", 6.0, "c1", None)
    check("монолог: явный язык пользователя (en)",
          "The user's language is English. Write ONLY in English" in msgs_pm[0]["content"])
    msgs_pm_ru = pm._build_monolog_prompt(
        [{"role": "user", "content": "Привет, как дела?"}], [], "Аня", 6.0, "c1", None)
    check("монолог: явный язык пользователя (ru)",
          "The user's language is Russian. Write ONLY in Russian" in msgs_pm_ru[0]["content"])

    # ── 11. Дневник: self_memory и offline_summarizer ──
    print("дневник:")
    from app.core import self_memory as sm_mod
    captured_sm = []

    class CapRouterSM:
        active_provider = None

        def get_response(self, messages, **kw):
            captured_sm.append(messages[0]["content"])
            return "Warm. Slept by the battery."

    sm_full = sm_mod.BotSelfMemory(tempfile.mkdtemp(), "Connor", CapRouterSM(), mode="full")
    sm_full._write_episode([{"role": "user", "content": "Hey, how was your day?", "user_name": "Ann"}])
    check("эпизод self_memory: явный язык в системном сообщении",
          captured_sm and "The user's language is English." in captured_sm[-1])

    from app.core.offline_summarizer import OfflineSummarizer
    cap_sum = []

    class CapRouterSum:
        active_provider = None

        def get_response(self, messages, **kw):
            cap_sum.append(messages)
            return "Walked. Slept."

    os_sum = OfflineSummarizer(context="lang_smoke_sum", persona_name="Connor",
                               router=CapRouterSum())
    persona_stub = SimpleNamespace(system_prompt="You are Connor.")
    os_sum._theses_to_episode(["walked", "slept"], persona_stub, user_language="en")
    check("офлайн-эпизод: явный язык в промпте (en)",
          cap_sum and "Язык записи — английский." in cap_sum[-1][1]["content"])
    os_sum._theses_to_episode(["гулял"], persona_stub, user_language="ru")
    check("офлайн-эпизод: явный язык в промпте (ru)",
          "Язык записи — русский." in cap_sum[-1][1]["content"])

    print(f"\n{'PASS' if ok > 0 else 'FAIL'}: {ok} проверок")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

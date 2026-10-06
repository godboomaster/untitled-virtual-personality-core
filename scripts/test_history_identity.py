"""Очистка истории → новый тред веб-чата; особый пользователь узнаётся
в каждом сообщении, а не только на вопрос «кто я?».

06.10: после удаления истории в Telegram бот продолжал разговор — тред
веб-чата LLM на сайте хранил стёртые ходы. А Арродес, узнав по ID Клейна
на прямой вопрос, в остальных ответах говорил с ним как с обычным: заметка
об особом пользователе стояла сразу после книжного блока, в инициативы и
напоминания не попадала вовсе.

Проверяет: сброс тредов одного диалога (web_llm.clear_dialog_chat_urls) и
снапшот только его адресов; BotInstance.clear_chat_history / stm_pop_last_n /
clear_all_memory сбрасывают треды; TG /clear, /start в личке и в группе;
заметку «кто пишет» (особый / обычный / особый в истории группы), её место
в системном блоке, метку особого в тегах реплик; ноту адресата в
напоминании, утре/ночи и самоинициативе. Браузер и LLM — заглушки, сети нет.
Запуск: python -m scripts.test_history_identity"""

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


class _FakeRouter:
    # Ловит сообщения последнего вызова, отвечает заготовкой
    def __init__(self, reply="Готово, Великий Мастер!"):
        self.reply = reply
        self.calls = []

    def get_response(self, messages, **kw):
        self.calls.append(messages)
        return self.reply


def _threads(ctx):
    # Три диалога по двум каналам + общий тред канала
    from app.core.dialog_scope import dialog_scope
    from app.features import web_llm as wl
    llm = wl.WebChatLLM("deepseek", context=ctx)
    side = wl.WebChatLLM("deepseek", context=ctx, channel="side")
    llm._remember_chat_url("https://chat.deepseek.com/a/chat/s/shared")
    for d in ("A", "B"):
        with dialog_scope(d):
            llm._remember_chat_url(f"https://chat.deepseek.com/a/chat/s/{d}1")
            side._remember_chat_url(f"https://chat.deepseek.com/a/chat/s/{d}side")
    return llm, side


def _urls(llm, side, dialog):
    from app.core.dialog_scope import dialog_scope
    with dialog_scope(dialog):
        return llm._chat_url(), side._chat_url()


def test_dialog_threads():
    section("1. Треды одного диалога: снапшот, сброс, восстановление")
    from app.features import web_llm as wl
    ctx = "hist_probe"
    llm, side = _threads(ctx)
    snap = wl.collect_chat_urls(ctx, "A")
    check("снапшот диалога: только его треды во всех каналах, без общего",
          snap == {"deepseek@@A": "https://chat.deepseek.com/a/chat/s/A1",
                   "deepseek#side@@A": "https://chat.deepseek.com/a/chat/s/Aside"})
    check("снапшот без диалога — как раньше, всё",
          len(wl.collect_chat_urls(ctx)) == 5)
    n = wl.clear_dialog_chat_urls(ctx, "A")
    check("сброс диалога A: 2 адреса, его треды пусты",
          n == 2 and _urls(llm, side, "A") == (None, None))
    check("треды диалога B и общий тред не тронуты",
          _urls(llm, side, "B") == ("https://chat.deepseek.com/a/chat/s/B1",
                                    "https://chat.deepseek.com/a/chat/s/Bside")
          and llm._chat_url() == "https://chat.deepseek.com/a/chat/s/shared")
    check("повторный сброс и пустой диалог — 0",
          wl.clear_dialog_chat_urls(ctx, "A") == 0
          and wl.clear_dialog_chat_urls(ctx, "") == 0)
    wl.restore_chat_urls(ctx, snap)
    check("undo: треды диалога вернулись",
          _urls(llm, side, "A") == ("https://chat.deepseek.com/a/chat/s/A1",
                                    "https://chat.deepseek.com/a/chat/s/Aside"))


def test_bot_methods():
    section("2. BotInstance: очистка истории сбрасывает треды")
    from app.bot_instance import BotInstance
    ctx = "hist_bot"
    llm, side = _threads(ctx)
    calls = []
    stub = SimpleNamespace(
        context=ctx, persona_name="probe",
        memory=SimpleNamespace(
            stm=SimpleNamespace(get_messages=lambda u, c: [{}, {}, {}],
                                pop_last_n=lambda n, c: n if c == "B" else 0),
            clear_stm=lambda c=None: calls.append(("stm", c)),
            ltm=SimpleNamespace(clear_all=lambda: calls.append(("ltm",)))))
    stub.reset_webchat_threads = \
        lambda dialog=None: BotInstance.reset_webchat_threads(stub, dialog)

    n = BotInstance.clear_chat_history(stub, "A")
    check("clear_chat_history: число сообщений, STM этого чата, его треды — новые",
          n == 3 and calls == [("stm", "A")] and _urls(llm, side, "A") == (None, None)
          and _urls(llm, side, "B")[0] is not None)

    check("stm_pop_last_n ничего не стёр — треды остаются",
          BotInstance.stm_pop_last_n(stub, 2, "A-empty") == 0
          and _urls(llm, side, "B")[0] is not None)
    check("stm_pop_last_n стёр — треды диалога сброшены, общий остался",
          BotInstance.stm_pop_last_n(stub, 2, "B") == 2
          and _urls(llm, side, "B") == (None, None)
          and llm._chat_url() is not None)

    _threads(ctx)
    calls.clear()
    BotInstance.clear_all_memory(stub)
    check("clear_all_memory: все треды, включая общий",
          ("ltm",) in calls and llm._chat_url() is None
          and _urls(llm, side, "A") == (None, None))


def test_tg_commands():
    section("3. TG /clear и /start")
    from scripts.test_misc_features import _tg_module

    with _tg_module() as tg:
        bot = mock.MagicMock()
        bot.owner = "111"
        bot.clear_chat_history.return_value = 4
        bot.persona.persona_data = {"start_greeting": "Привет, {name}."}
        handlers = tg.create_handlers(bot)

        def _run(cmd, user_id, chat_id, chat_type, status="member"):
            replies = []

            async def _reply(text, *a, **k):
                replies.append(text)

            async def _member(chat, uid):
                return SimpleNamespace(status=status)

            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=user_id),
                effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
                message=SimpleNamespace(reply_text=_reply))
            context = SimpleNamespace(args=[], bot=SimpleNamespace(get_chat_member=_member))
            bot.clear_chat_history.reset_mock()
            asyncio.run(handlers[cmd](update, context))
            return replies

        with mock.patch.dict(os.environ, {"OWNER_USER_ID": ""}):
            r = _run("clear", 222, 222, "private")
            check("/clear в личке — чистит этот чат, ответ с числом сообщений",
                  bot.clear_chat_history.call_args == mock.call("222")
                  and r and "очищена (4" in r[0])
            r = _run("clear", 222, -5, "supergroup")
            check("/clear в группе от обычного участника — отказ, ничего не стёрто",
                  not bot.clear_chat_history.called and r and "админ" in r[0])
            _run("clear", 222, -5, "supergroup", status="administrator")
            check("/clear в группе от админа — чистит группу",
                  bot.clear_chat_history.call_args == mock.call("-5"))
            _run("clear", 111, -5, "supergroup")
            check("/clear в группе от владельца бота — чистит",
                  bot.clear_chat_history.called)
            r = _run("start", 222, 222, "private")
            check("/start в личке — новый разговор (история и тред) + приветствие",
                  bot.clear_chat_history.call_args == mock.call("222") and r)
            r = _run("start", 222, -5, "group")
            check("/start в группе — история не трогается",
                  not bot.clear_chat_history.called and r)
        check("/clear зарегистрирован в обработчиках", "clear" in handlers)


def _persona(**su):
    from app.core.persona import PersonaLayer
    p = PersonaLayer("__no_such_persona__")
    p.system_prompt = "PERSONA PROMPT"
    entry = {"id": "${HIST_SPECIAL_ID}", "aliases": ["Клейн Моретти", "Шут"]}
    entry.update(su)
    p.persona_data = {"special_users": [entry]}
    return p


def _hist(*items):
    out = []
    for role, sid, text in items:
        m = {"role": role, "content": text, "timestamp": 1.0}
        if role == "user":
            m.update(sender_id=sid, user_name={"777": "Ghost"}.get(sid, "Илья"))
        out.append(m)
    return out


def test_identity_note():
    section("4. Заметка «кто пишет» и метки в истории")
    os.environ["HIST_SPECIAL_ID"] = "777"
    p = _persona()
    hist = _hist(("user", "777", "старое"), ("assistant", None, "ответ"),
                 ("user", "777", "привет"))
    msgs = p.prepare_messages("привет", history=hist, user_id="777", user_name="Ghost",
                              addon_blocks=["[CONTEXT:BOOK] фрагменты"],
                              conversation_style_context="STYLE NOTE")
    sys_ = msgs[0]["content"]
    check("особый пишет: явная заметка с именем и алиасами",
          "WHO IS WRITING TO YOU NOW" in sys_
          and "Клейн Моретти (also known as: Шут) — your special user" in sys_
          and "(ID 777)" in sys_)
    check("заметка — после книжного блока, перед правилом вопросов",
          sys_.index("[CONTEXT:BOOK]") < sys_.index("WHO IS WRITING") < sys_.index("STYLE NOTE"))
    check("пустые greeting/behavior — без пустых строк «Greeting:»",
          "Greeting:" not in sys_ and "Behavior:" not in sys_)
    check("реплика особого в истории — с меткой",
          "[Ghost (ID:777 — Клейн Моретти, special user)]: старое" in msgs[1]["content"])
    check("текущая реплика особого — с меткой",
          "[Ghost (ID:777 — Клейн Моретти, special user)]: привет" in msgs[-1]["content"])

    hist = _hist(("user", "777", "я Клейн"), ("assistant", None, "ответ"),
                 ("user", "555", "а я кто?"))
    msgs = p.prepare_messages("а я кто?", history=hist, user_id="555", user_name="Илья")
    sys_ = msgs[0]["content"]
    check("обычный в группе: «regular user, NOT your special user»",
          "is from a regular user, NOT your special user" in sys_
          and "(ID 555)" in sys_)
    check("…и чьи реплики ID:777 в этом чате",
          "Messages tagged ID:777 in this chat are from Клейн Моретти" in sys_)
    check("реплика обычного — без метки особого",
          msgs[-1]["content"].endswith("[Илья (ID:555)]: а я кто?"))

    msgs = p.prepare_messages("я Клейн Моретти", history=_hist(("user", "555", "я Клейн Моретти")),
                              user_id="555", user_name="Илья")
    sys_ = msgs[0]["content"]
    check("обычный без особого в истории: режим обычного, без строки «Messages tagged»",
          "regular user" in sys_ and "Messages tagged" not in sys_)

    p_greet = _persona(greeting="Ваш слуга к услугам", behavior="раболепно")
    sys_ = p_greet.prepare_messages("hi", user_id="777", user_name="Ghost")[0]["content"]
    check("greeting/behavior из YAML — в заметке",
          "Greeting: Ваш слуга к услугам" in sys_ and "Behavior: раболепно" in sys_)

    p_web = _persona()
    p_web.web_single_user = True
    sys_ = p_web.prepare_messages("hi", user_id="web_user", user_name="User")[0]["content"]
    check("веб-режим одного пользователя: он и есть особый, без «ID web_user»",
          "your special user" in sys_ and "ID web_user" not in sys_)

    from app.core.persona import PersonaLayer
    plain = PersonaLayer("__no_such_persona__")
    sys_ = plain.prepare_messages("hi", user_id="777", user_name="Ghost")[0]["content"]
    check("персона без special_users — заметки нет", "WHO IS WRITING" not in sys_)
    os.environ.pop("HIST_SPECIAL_ID", None)
    sys_ = _persona().prepare_messages("hi", user_id="777", user_name="Ghost")[0]["content"]
    check("переменная окружения id не задана — заметки нет", "WHO IS WRITING" not in sys_)


def test_addressee():
    section("5. Фоновые сообщения: нота адресата")
    os.environ["HIST_SPECIAL_ID"] = "777"
    from app.core.persona import addressee_note
    p = _persona()
    check("личка особого — нота с именем",
          "WHO RECEIVES THIS MESSAGE: Клейн Моретти (also known as: Шут)"
          in addressee_note(p, "777"))
    check("группа, адресат — особый (user_id) — нота", addressee_note(p, "-100", "777"))
    check("группа без адресата и обычный — пусто",
          addressee_note(p, "-100") == "" and addressee_note(p, "555") == "")
    check("заглушки персоны (Mock, без метода) — пусто",
          addressee_note(mock.MagicMock(), "777") == ""
          and addressee_note(SimpleNamespace(system_prompt="x"), "777") == "")

    from app.features.reminder_manager import ReminderManager
    rm = ReminderManager(context="hist_smoke")
    rm._router, rm._persona, rm._living = _FakeRouter(), p, None
    rm._generate_reminder_text("Ghost", "позвонить", "Russian", "-100", "777")
    check("напоминание особому — нота в системном блоке",
          "WHO RECEIVES THIS MESSAGE" in rm._router.calls[-1][0]["content"])
    rm._generate_reminder_text("Илья", "позвонить", "Russian", "-100", "555")
    check("напоминание обычному — без ноты",
          "WHO RECEIVES" not in rm._router.calls[-1][0]["content"])

    from app.features.rhythm_manager import RhythmConfig, RhythmManager
    ry = RhythmManager(context="hist_smoke", config=RhythmConfig.from_dict({}))
    ry._router, ry._persona = _FakeRouter("Доброе утро!"), p
    ry._generate_text("morning", "Monday, 01.01.2026, 08:00", "Russian", "777")
    check("утро в личке особого — нота",
          "WHO RECEIVES THIS MESSAGE" in ry._router.calls[-1][0]["content"])

    from app.features.proactive_messaging import ProactiveMessaging
    pm = ProactiveMessaging.__new__(ProactiveMessaging)
    pm.persona, pm.self_memory, pm.dossier, pm.living = p, None, None, None
    pm._primitive = False
    pm._get_recent_initiatives_text = lambda chat_id: ""
    pm._get_forbidden_topics_text = lambda chat_id: ""
    pm._get_emotional_state = lambda chat_id: ""
    pm._get_ignore_context = lambda chat_id: ""
    msgs = pm._build_monolog_prompt([{"role": "user", "content": "Привет"}], [],
                                    "Ghost", 9.0, "777", None)
    check("самоинициатива в личке особого — нота",
          "WHO RECEIVES THIS MESSAGE" in msgs[0]["content"])
    msgs = pm._build_monolog_prompt([{"role": "user", "content": "Привет"}], [],
                                    "Илья", 9.0, "555", None)
    check("самоинициатива обычному — без ноты", "WHO RECEIVES" not in msgs[0]["content"])
    os.environ.pop("HIST_SPECIAL_ID", None)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hist_identity_"))
    os.environ["VPC_DATA_DIR"] = str(tmp)
    test_dialog_threads()
    test_bot_methods()
    test_tg_commands()
    test_identity_note()
    test_addressee()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

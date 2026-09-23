"""Присутствие в веб-вкладке (app/core/presence.py) + разбор «да/нет» обучения
+ обработка сбоя пайплайна bot_instance.process_message.

Что проверяем:
  1. presence ключуется парой (контекст персоны, chat_id): одна открытая
     вкладка НЕ морозит фон других чатов, других персон и Telegram-чатов той
     же персоны; отметки протухают по TTL и ограничены по числу ключей;
  2. гейты на местах вызова: proactive (инициатива), living (_tick_all —
     по чатам, any_active только для операций уровня персоны), memory
     (батч-экстракция и консолидация LTM);
  3. bot_instance: у основного пайплайна есть except (не только finally),
     а реплика-ошибка попадает в STM один раз и на языке пользователя;
  4. learning_manager.classify_continue_answer — через общий классификатор
     подтверждений (клаузный разбор, «не, давай не будем продолжать» = NO);
  5. learning_manager.clear_chat — публичная очистка обучения чата, и
     memory_wipe._wipe_learning ходит через неё;
  6. per-chat словари движка жизни ограничены (BoundedCache).

Запуск: PYTHONPATH=. python3 scripts/test_presence.py
"""

import ast
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ok = 0


def check(name, cond):
    global ok
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok = ok + 1 if cond else ok - 100


def main():
    from app.core.bounded_cache import BoundedCache
    from app.core.presence import WebPresence, web_context, web_presence

    print("\n── 1. Ключ присутствия: (контекст, чат) ──")
    p = WebPresence(ttl=30.0)
    p.note("api_alex", "web_user", True)
    check("свой чат своей персоны — активен", p.is_active("api_alex", "web_user"))
    check("другой чат той же персоны — НЕ активен",
          not p.is_active("api_alex", "chat2"))
    check("другая персона — НЕ активна", not p.is_active("api_bob", "web_user"))
    check("Telegram-контекст той же персоны — НЕ активен "
          "(веб-вкладки у него нет)", not p.is_active("alex", "web_user"))
    check("any_active по персоне — есть", p.any_active("api_alex"))
    check("any_active по другой персоне — нет", not p.any_active("api_bob"))
    check("any_active по Telegram-контексту — нет", not p.any_active("alex"))
    p.note("api_alex", "web_user", False)
    check("active=false снимает отметку", not p.is_active("api_alex", "web_user")
          and not p.any_active("api_alex"))

    check("web_context: id персоны → контекст бота registry",
          web_context("alex") == "api_alex")

    p_ttl = WebPresence(ttl=0.05)
    p_ttl.note("api_alex", "web_user", True)
    time.sleep(0.12)
    check("отметка протухает по TTL", not p_ttl.is_active("api_alex", "web_user")
          and not p_ttl.any_active("api_alex"))

    p_lim = WebPresence(ttl=30.0, max_keys=4)
    for i in range(20):
        p_lim.note("api_alex", f"chat{i}", True)
    check("число ключей ограничено (BoundedCache, без утечки)",
          isinstance(p_lim._marks, BoundedCache) and len(p_lim._marks) <= 4)
    check("глобальный синглтон — WebPresence с новым API",
          hasattr(web_presence, "any_active")
          and web_presence.is_active("api_nobody", "web_user") is False)

    print("\n── 2. Гейты: proactive / living / memory ──")
    from app.features.proactive_messaging import ProactiveMessaging

    def make_pm(context):
        pm = ProactiveMessaging.__new__(ProactiveMessaging)
        pm.context = context
        pm.config = types.SimpleNamespace(
            enabled=True, max_daily_initiatives=5, check_interval_minutes=1,
            silence_threshold_minutes=60)
        pm.persona = types.SimpleNamespace(persona_data={})
        pm._in_initiative_hours = lambda: True
        pm._get_daily_count = lambda cid: 0
        pm.get_last_message_time = lambda cid: time.time() - 7200
        pm._get_ignore_streak = lambda cid: 0
        pm._get_last_initiative_time = lambda cid: 0
        return pm

    web_presence.clear()
    web_presence.note("api_alex", "web_user", True)
    pm_web = make_pm("api_alex")
    check("proactive: инициатива в чате с открытой вкладкой — стоп",
          pm_web.initiative_cheaply_possible("web_user") is False)
    check("proactive: инициатива в ДРУГОМ чате той же персоны — идёт",
          pm_web.initiative_cheaply_possible("group42") is True)
    pm_tg = make_pm("alex")
    check("proactive: Telegram-чат той же персоны не заморожен веб-вкладкой",
          pm_tg.initiative_cheaply_possible("web_user") is True)
    pm_other = make_pm("api_bob")
    check("proactive: другая персона не заморожена",
          pm_other.initiative_cheaply_possible("web_user") is True)

    from app.core.living_persona import LivingPersona

    def make_living(world_enabled=False, screenwriter=False):
        lp = LivingPersona.__new__(LivingPersona)
        lp.context = "api_alex"
        lp.metrics = {"ticks_throttled": 0, "episodes_written": 0,
                      "screenwriter_runs": 0}
        lp.ticked = []
        lp.stimuli = []
        lp.screen = []
        lp.persona_context = lambda: {}
        lp._known_chats = lambda: ["web_user", "group42"]
        lp._persist_metrics_daily = lambda: None
        lp._chat_throttled = lambda cid: False
        lp._tick_chat = lambda cid, pc, fs: (lp.ticked.append(cid),
                                             lp.stimuli.append(fs), None)[2]
        lp.config = types.SimpleNamespace(world_enabled=world_enabled)
        lp.state_engine = types.SimpleNamespace(
            unconsumed=lambda cid, limit=40: [])
        lp.summarizer = types.SimpleNamespace(
            should_run_daily=lambda cid, n: False,
            should_run_screenwriter=lambda we: screenwriter,
            advance_storylines=lambda persona, we: lp.screen.append(1) or True)
        lp.world_engine = types.SimpleNamespace(
            should_fetch_stimuli=lambda: True)
        lp.external_stimuli_allowed = lambda: True
        lp.persona = None
        return lp

    lp = make_living()
    lp._tick_all()
    check("living: тикают все чаты, кроме чата с открытой вкладкой",
          lp.ticked == ["group42"])
    web_presence.clear()
    lp2 = make_living()
    lp2._tick_all()
    check("living: вкладок нет — тикают все чаты",
          lp2.ticked == ["web_user", "group42"])

    web_presence.note("api_alex", "web_user", True)
    lp3 = make_living(world_enabled=True, screenwriter=True)
    lp3._tick_all()
    check("living: сценарист/стимулы (уровень персоны) ждут по any_active",
          lp3.screen == [] and lp3.stimuli == [False])
    web_presence.clear()
    lp4 = make_living(world_enabled=True, screenwriter=True)
    lp4._tick_all()
    check("living: без вкладок сценарист и стимулы работают",
          lp4.screen == [1] and lp4.stimuli == [True, True])

    from app.core.memory import MemoryManager

    def make_mm():
        mm = MemoryManager.__new__(MemoryManager)
        mm.context = "api_alex"
        mm.enable_ltm_extraction = True
        mm._counter_lock = threading.Lock()
        mm._extract_counters = {}
        mm._user_msg_counters = {}
        mm.extracted = []
        mm.summarized = []
        mm.stm = types.SimpleNamespace(
            add_message=lambda *a, **kw: None,
            get_last=lambda n, chat_id=None: [
                {"role": "user", "content": "привет", "sender_id": None}])
        mm.ltm = types.SimpleNamespace(
            main_router=None,
            extract_facts_async=lambda *a, **kw: mm.extracted.append(1))
        mm._run_summarize_async = lambda uid: (mm.summarized.append(uid), True)[1]
        return mm

    def feed(mm, chat_id, n=40):
        for _ in range(n):
            mm.add_message("user", "текст", "u1", chat_id, None, light_mode=True)

    web_presence.note("api_alex", "web_user", True)
    mm = make_mm()
    feed(mm, "web_user")
    check("memory: экстракция и консолидация в чате с вкладкой — стоят",
          mm.extracted == [] and mm.summarized == [])
    mm2 = make_mm()
    feed(mm2, "group42")
    check("memory: в другом чате той же персоны — работают",
          mm2.extracted and mm2.summarized)
    web_presence.clear()
    mm3 = make_mm()
    feed(mm3, "web_user")
    check("memory: вкладку закрыли — накопленное поехало",
          mm3.extracted and mm3.summarized)

    print("\n── 3. API: persona + chat_id в /api/presence ──")
    from app.api.schemas import PresenceRequest
    req = PresenceRequest(active=True, persona="alex")
    check("PresenceRequest: chat_id по умолчанию web_user",
          req.chat_id == "web_user" and req.persona == "alex")
    bad = False
    try:
        PresenceRequest(active=True, persona="../../etc/passwd")
    except Exception:
        bad = True
    check("PresenceRequest: имя персоны валидируется (PersonaId)", bad)
    src = Path("app/api/server.py").read_text(encoding="utf-8")
    check("server: и эндпоинт, и heartbeat инбокса шлют ключ",
          'web_presence.note(web_context(req.persona), req.chat_id, req.active)' in src
          and 'web_presence.note(web_context(persona), chat_id, True)' in src)

    # Старый фронт мог слать /api/presence вовсе без persona (до того, как
    # гейт стали ключевать парой персона+чат) — раньше это 422 без внятного
    # объяснения на стороне фронта. persona теперь Optional: запрос
    # принимается, отметка просто не ставится (ключ без персоны не собрать).
    req_no_persona = PresenceRequest(active=True)
    check("PresenceRequest: без persona — не бросает (Optional, default None)",
          req_no_persona.persona is None and req_no_persona.chat_id == "web_user")

    try:
        import fastapi  # noqa: F401
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi недоступен: {e})")
    else:
        import logging as _logging
        import app.api.server as server_mod

        orig_token = server_mod._api_token
        server_mod._api_token = ""  # детерминированно: не зависим от .env
        server_mod._presence_no_persona_warned = False
        web_presence.clear()
        warned = []

        class _Capture(_logging.Handler):
            def emit(self, record):
                warned.append(record.getMessage())

        handler = _Capture(level=_logging.WARNING)
        server_mod.logger.addHandler(handler)
        try:
            client = TestClient(server_mod.app)
            r1 = client.post("/api/presence", json={"active": True, "chat_id": "c1"})
            check("POST /api/presence без persona: 200, а не 422 (старый фронт)",
                  r1.status_code == 200)
            r2 = client.post("/api/presence", json={"active": True, "chat_id": "c2"})
            check("POST /api/presence без persona (второй запрос): тоже 200",
                  r2.status_code == 200)
            check("без persona: warning в лог — один раз на процесс, не на каждый запрос",
                  sum(1 for w in warned if "без persona" in w) == 1)
            check("без persona: отметка присутствия не поставлена (ключ не собрать)",
                  not web_presence.any_active(web_context("alex")))

            r3 = client.post("/api/presence",
                             json={"active": True, "persona": "alex", "chat_id": "c3"})
            check("POST /api/presence с persona: 200 (штатное поведение не сломано)",
                  r3.status_code == 200)
            check("с persona: отметка присутствия поставлена",
                  web_presence.is_active(web_context("alex"), "c3"))
        finally:
            server_mod.logger.removeHandler(handler)
            server_mod._api_token = orig_token
            web_presence.clear()

    print("\n── 4. Сбой пайплайна process_message ──")
    bot_src = Path("app/bot_instance.py").read_text(encoding="utf-8")
    tree = ast.parse(bot_src)
    # process_message — тонкая обёртка (лог START/END), сам пайплайн — в
    # _process_message_impl: проверяем обе
    fns = [n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef)
           and n.name in ("process_message", "_process_message_impl")]
    check("пайплайн process_message найден", len(fns) == 2)
    naked = [t for fn in fns for t in ast.walk(fn)
             if isinstance(t, ast.Try) and t.finalbody and not t.handlers]
    check("у пайплайна нет try/finally без except", not naked)

    from app.bot_instance import BotInstance

    def make_bot(last_role):
        bot = BotInstance.__new__(BotInstance)
        bot.context = "api_alex"
        bot.saved = []
        bot.persona = types.SimpleNamespace(settings={})
        bot._pending_split_messages = {}
        bot.memory = types.SimpleNamespace(
            add_message=lambda role, text, uid, cid: bot.saved.append((role, text)),
            stm=types.SimpleNamespace(
                get_last=lambda n, chat_id=None: [{"role": last_role}]))
        return bot

    bot = make_bot("user")
    bot.chat_user_language = lambda cid: "ru"
    reply = bot._pipeline_failure_reply("u1", "web_user")
    check("сбой: понятная реплика по-русски вернулась пользователю",
          isinstance(reply, str) and "сломалось" in reply)
    check("сбой: реплика легла в STM (в истории нет вопроса без ответа)",
          bot.saved == [("assistant", reply)])
    bot_en = make_bot("user")
    bot_en.chat_user_language = lambda cid: "en"
    check("сбой: язык пользователя учитывается",
          "Sorry" in bot_en._pipeline_failure_reply("u1", "web_user"))
    bot_done = make_bot("assistant")
    bot_done.chat_user_language = lambda cid: "ru"
    bot_done._pipeline_failure_reply("u1", "web_user")
    check("сбой ПОСЛЕ сохранения ответа: второй записи в STM нет",
          bot_done.saved == [])
    bot_broken = make_bot("user")
    bot_broken.chat_user_language = lambda cid: 1 / 0
    bot_broken.memory.stm.get_last = lambda n, chat_id=None: 1 / 0
    check("сбой внутри самого обработчика не бросает наружу",
          isinstance(bot_broken._pipeline_failure_reply("u1", "web_user"), str))

    print("\n── 5. «Да/нет» обучения через общий классификатор ──")
    from app.features import learning_manager as lm
    from app.features.computer_control import classify_confirmation
    cases = [
        ("да", "YES"), ("давай", "YES"), ("продолжаем", "YES"),
        ("продолжай пожалуйста", "YES"), ("хочу", "YES"), ("ага", "YES"),
        ("ok", "YES"), ("конечно", "YES"),
        ("нет", "NO"), ("не надо", "NO"), ("хватит", "NO"),
        ("отстань", "NO"), ("стоп", "NO"), ("надоело", "NO"),
        ("останови курс", "NO"), ("не хочу", "NO"),
        # Корень дефекта: раньше два re.search по всему тексту давали YES
        ("не, давай не будем продолжать", "NO"),
        ("давай не будем", "NO"),
        ("подожди, не продолжай", "NO"),
        ("я не хочу продолжать", "NO"),
        ("наверное", "UNKNOWN"), ("", "UNKNOWN"),
        ("продолжим?", "UNKNOWN"),  # вопрос, а не ответ
    ]
    bad_cases = [(t, lm.classify_continue_answer(t), exp)
                 for t, exp in cases if lm.classify_continue_answer(t) != exp]
    check(f"классификация ответов на «продолжаем?» ({len(cases)} кейсов)"
          + (f" — промахи: {bad_cases}" if bad_cases else ""), not bad_cases)
    check("второй копии клаузной логики нет — словари-заплатки убраны",
          not hasattr(lm, "_POSITIVE_RE") and not hasattr(lm, "_NEGATIVE_RE")
          and lm.classify_confirmation is classify_confirmation)

    print("\n── 6. learning_manager.clear_chat + memory_wipe ──")
    from app.api import memory_wipe as mw
    from app.features.learning_manager import LearningManager
    cwd = os.getcwd()
    tmp = Path(tempfile.mkdtemp(prefix="presence_learn_"))
    try:
        os.chdir(tmp)
        mgr = LearningManager(context="api_ctx_test")
        # active=True — иначе _save() (он же чистильщик старых сессий)
        # выкинет фикстуры без created_at вместе с проверкой
        mgr._sessions = [{"chat_id": "web_user", "subject": "английский",
                          "active": True},
                         {"chat_id": "other", "subject": "чужое",
                          "active": True}]
        mgr.begin_setup("web_user", "физика", "u1", "Аня")
        mgr.begin_setup("web_user", "химия", "u2", "Боря")
        mgr.begin_setup("other", "чужое", "u3", "Вася")
        mgr.register_question_message("web_user", 111)
        removed = mgr.clear_chat("web_user")
        check("clear_chat: сессии чата удалены, чужие целы",
              removed == 1 and mgr._sessions == [{"chat_id": "other",
                                                  "subject": "чужое",
                                                  "active": True}])
        check("clear_chat: сняты ВСЕ ожидающие setup чата (групповой случай)",
              mgr.get_setup_state("web_user") is None
              and mgr.get_setup_state("web_user", "u2") is None
              and mgr.get_setup_state("other", "u3") is not None)
        check("clear_chat: реестр вопросов уроков чата очищен",
              not mgr.is_reply_to_question("web_user", 111))

        mgr2 = LearningManager(context="api_ctx_test2")
        mgr2.begin_setup("c1", "физика", "u1", "Аня")
        mgr2.begin_setup("c1", "химия", "u2", "Боря")
        mgr2.clear_setup("c1", "u1")
        check("clear_setup(user_id): чужой setup в чате не тронут",
              mgr2.get_setup_state("c1", "u1") is None
              and mgr2.get_setup_state("c1", "u2") is not None)
        mgr2.clear_setup("c1", all_users=True)
        check("clear_setup(all_users=True): чат вычищен целиком",
              mgr2.get_setup_state("c1", "u2") is None)

        mgr3 = LearningManager(context="api_ctx_test3")
        mgr3._sessions = [{"chat_id": "web_user", "subject": "английский",
                           "active": True}]
        mgr3.begin_setup("web_user", "физика", "u1", "Аня")
        fake_bot = types.SimpleNamespace(learning_manager=mgr3)
        mw._wipe_learning(fake_bot, "api_ctx_test3", "web_user")
        check("memory_wipe._wipe_learning ходит через публичный clear_chat",
              mgr3._sessions == []
              and mgr3.get_setup_state("web_user") is None)
    finally:
        os.chdir(cwd)

    print("\n── 7. Per-chat состояние движка жизни ограничено ──")
    living_src = Path("app/core/living_persona.py").read_text(encoding="utf-8")
    check("_chat_user_lang / _harvest_* — BoundedCache, а не вечный dict",
          living_src.count("BoundedCache(max_entries=MAX_CHAT_KEYS)") == 3)
    cache = BoundedCache(max_entries=3)
    for i in range(10):
        cache[f"c{i}"] = i
    check("вытеснение работает (проверка контракта кеша)", len(cache) == 3)

    print("\n── 8. Время пользователя вместо пояса машины (timeutil) ──")
    mem_src = Path("app/core/memory.py").read_text(encoding="utf-8")
    check("bot_instance: нет datetime.now()/date.today()/time.localtime()",
          "datetime.now()" not in bot_src and "date.today()" not in bot_src
          and "time.localtime(" not in bot_src)
    check("memory: датировка реплик — общий persona._format_msg_ts "
          "(второй копии формата на системном поясе нет)",
          "from app.core.persona import _format_msg_ts" in mem_src
          and "datetime.now()" not in mem_src
          and "timeutil.from_ts" not in mem_src.split("get_last_display")[1][:900])
    check("living: граница суток метрик — через timeutil",
          "timeutil.now()" in living_src
          and "datetime.now()" not in living_src)

    print(f"\nИтог: {ok} проверок")
    sys.exit(0)


if __name__ == "__main__":
    main()

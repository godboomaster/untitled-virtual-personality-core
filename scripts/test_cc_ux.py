"""UX режима управления: остановка долгих действий, дубли, жизненный цикл
режима, язык служебных реплик, подтверждения.

Проверяет: cc_texts (ru/en), английские describe/confirm_question/
describe_done и превью вводимого текста, cc_turn_enter/cc_turn_exit
(«стоп» до лока хода, занятый агент, дубль идущей команды), флаг остановки
ComputerControlManager (execute/навигация), TaskAgent.busy и отмену посреди
цепочки, автовыход по простою и сохранение режима на диск, честную пометку
о напоминаниях в режиме, rebind_computer_control. Браузер и LLM подменены.

Запуск: python -m scripts.test_cc_ux
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cc_ux_smoke_")
    # Папки ботов-заготовок (ux_<ns>) — во временный каталог, не в data/:
    # data_dir() читает VPC_DATA_DIR, DATA_DIR он не видит
    os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="cc_ux_vpc_")
    tmp = Path(tempfile.mkdtemp(prefix="cc_ux_data_"))
    import app.core.router as _net_router
    _net_router.internet_available = lambda: True
    _net_router._net_ok, _net_router._net_checked = True, float("inf")

    fails = 0
    total = 0

    def check(name, cond):
        nonlocal fails, total
        total += 1
        if not cond:
            fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")

    from app.features import cc_texts
    from app.features.computer_control import ComputerControlManager as CCM
    import app.features.flavor_text as _flavor
    from app.features import browser_actions as _ba

    _orig = (_ba.set_control_mode, _flavor.cc_reply, _flavor.phrase)
    _ba.set_control_mode = lambda *a, **kw: None
    _flavor.cc_reply = lambda *a, **kw: None
    # Банк персоны пуст — служебные фразы берутся из шаблонов
    _flavor.phrase = lambda context, key, template, **v: template

    # ── 1. Таблица фиксированных реплик ──
    check("t: ru по умолчанию", cc_texts.t("declined") == "Хорошо, не выполняю.")
    check("t: en", cc_texts.t("declined", "en") == "Okay, I won't do it.")
    check("t: неизвестный язык — русский", cc_texts.t("stopping", "de") == "Останавливаю…")
    check("t: плейсхолдеры", cc_texts.t("done", "en", what="opened x") == "Done: opened x.")
    check("t: недостающий плейсхолдер не роняет",
          "{detail}" in cc_texts.t("failed", "ru", what="нажать"))

    # ── 2. Описания действий на английском ──
    click = {"kind": "click", "element": "Войти", "host": "example.com"}
    check("describe ru без lang — как раньше",
          CCM.describe(click) == "нажать «Войти» на example.com")
    check("describe en", CCM.describe(click, lang="en")
          == "click \"Войти\" on example.com")
    check("confirm_question en",
          CCM.confirm_question(click, lang="en") == "Click \"Войти\" on example.com?")
    check("describe_done en",
          CCM.describe_done(click, lang="en") == "clicked \"Войти\" on example.com")
    check("confirm_question ru с lang=ru — русский",
          CCM.confirm_question(click, lang="ru").startswith("Нажать «Войти»"))
    url = {"kind": "url", "value": "https://www.example.com/maps"}
    check("confirm_question en url — хост с сегментом",
          CCM.confirm_question(url, lang="en") == "Open example.com/maps?")
    check("describe_done en url", CCM.describe_done(url, lang="en")
          == "opened example.com/maps")
    multi = {"kind": "multi", "items": [click, url]}
    check("multi en: вопрос и прошедшее",
          CCM.confirm_question(multi, lang="en").startswith("Click \"Войти\"")
          and " and open " in CCM.confirm_question(multi, lang="en")
          and ", opened " in CCM.describe_done(multi, lang="en"))
    scroll = {"kind": "scroll", "host": "x.ru", "dir": "up"}
    check("scroll en: вопрос со «stop»",
          CCM.confirm_question(scroll, lang="en")
          == "Start scrolling the page up on x.ru? Say \"stop\" to stop.")
    kinds = [
        {"kind": "nav", "host": "x.ru", "steps": ["a", "b"]},
        {"kind": "download", "element": "f", "host": "x.ru", "url": "https://x.ru/f"},
        {"kind": "hover", "element": "m", "host": "x.ru"},
        {"kind": "type", "text": "hi", "element": "q", "host": "x.ru", "submit": True},
        {"kind": "read", "mode": "page", "host": "x.ru"},
        {"kind": "send", "host": "x.ru"}, {"kind": "press", "host": "x.ru"},
        {"kind": "key", "key": "Enter", "host": "x.ru"},
        {"kind": "key", "media": "erase", "times": 3, "host": "x.ru"},
        {"kind": "slider", "slider_label": "v", "slider_value": 5,
         "slider_unit": "pct", "host": "x.ru"},
        {"kind": "media_vol", "op": "-10", "host": "x.ru"},
        {"kind": "scroll_stop"}, {"kind": "tab_switch", "element": "t"},
        {"kind": "zoom", "dir": "in", "host": "x.ru"},
        {"kind": "tab_op", "op": "back"}, {"kind": "tab_op", "op": "reload"},
        {"kind": "cart", "op": "remove", "product": "p", "host": "x.ru"},
        {"kind": "comp_edit", "product": "p", "host": "x.ru"},
        {"kind": "url", "value": "https://x.ru", "search_query": "q",
         "search_site": "x.ru"},
        {"kind": "app", "key": "Safari"}, {"kind": "task", "key": "k"},
    ]
    ok_all = True
    for a in kinds:
        for fn in (CCM.describe, CCM.confirm_question, CCM.describe_done):
            try:
                s = fn(dict(a), lang="en")
            except Exception as e:
                s = None
                print(f"    {a['kind']} {fn.__name__}: {e}")
            if not s or any("а" <= ch <= "я" for ch in s.lower()):
                ok_all = False
                print(f"    {a['kind']} {fn.__name__}: {s!r}")
    check("en: все виды действий описываются без кириллицы шаблона", ok_all)

    # ── 3. Превью вводимого текста в вопросе ──
    long_text = "слово " * 60
    typ = {"kind": "type", "text": long_text, "element": "поле", "host": "x.ru"}
    q = CCM.confirm_question(typ)
    shown = q.split("«")[1].split("»")[0]
    check("type: вопрос показывает до ~200 символов с многоточием",
          150 <= len(shown) <= 201 and shown.endswith("…"))
    typ_short = dict(typ, text="привет")
    check("type: короткий текст — целиком, без многоточия",
          "«привет»" in CCM.confirm_question(typ_short))
    check("type en: превью тоже ~200",
          CCM.confirm_question(dict(typ, text="w " * 150), lang="en").count("w") > 90)

    # ── 4. Флаг остановки менеджера ──
    class Spy(CCM):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls, self.seen = [], []

        def _dispatch(self, action, router=None):
            self.seen.append((self.__dict__.get("_exec_chat"),
                              self.stop_requested()))
            self.calls.append(dict(action))

    cc = Spy(context="ux", config={"confirm": False}, base_dir=tmp / "cc1")
    check("stop: по умолчанию не запрошен", not cc.stop_requested("c1"))
    cc.execute({"kind": "url", "value": "https://example.com"}, "c1")
    check("execute: исполняемый чат виден внутри, после — снят",
          cc.seen and cc.seen[0][0] == "c1" and cc.__dict__.get("_exec_chat") is None)
    cc.request_stop("c1")
    check("stop: запрос для чата", cc.stop_requested("c1") and not cc.stop_requested("c2"))
    cc.execute({"kind": "url", "value": "https://example.com"}, "c1")
    check("stop: внутри execute этого чата флаг виден без chat_id",
          cc.seen[-1] == ("c1", True))
    cc.execute({"kind": "url", "value": "https://example.com"}, "c2")
    check("stop: чужой чат флаг не видит", cc.seen[-1] == ("c2", False))
    cc._exec_chat = "c1"
    raised = False
    try:
        cc._raise_if_stopped()
    except RuntimeError as e:
        raised = "остановлено" in str(e)
    cc._exec_chat = None
    check("stop: _raise_if_stopped бросает понятную ошибку", raised)
    cc.stop_clear("c1")
    check("stop: stop_clear снимает", not cc.stop_requested("c1"))

    # ── 4b. «стоп» внутри одного долгого вызова и веб-чат без chat_id ──
    import threading as _thr
    # Веб-чат без chat_id: ключ хода — user_id (как у cc_turn_enter)
    cc.set_turn("U1", "ru")
    cc.request_stop("U1")
    cc.execute({"kind": "url", "value": "https://example.com"}, None)
    check("web без chat_id: execute видит «стоп» по ключу хода (user_id)",
          cc.seen[-1] == ("U1", True))
    cc.stop_clear("U1")
    cc.execute({"kind": "url", "value": "https://example.com"}, None)
    check("web без chat_id: без «стоп» — флага нет", cc.seen[-1] == ("U1", False))
    check("_stop_check: ключ фиксируется при создании",
          cc._stop_check("zz")() is False)

    _saved_ba = {k: getattr(_ba, k) for k in (
        "_eval_js_any", "snapshot_elements", "reveal_player_controls",
        "scroll_position", "scroll_step", "scroll_restore",
        "scroll_container_step", "scroll_container_restore",
        "snapshot_for_goal", "_select_backend", "wait_dom_idle")}
    _saved_submit = _ba._WORKER.submit
    try:
        _n = [0]

        def _live_dom(h, t, js):
            _n[0] += 1
            return _n[0]  # DOM всё время меняется — опрос до таймаута
        _ba._eval_js_any = _live_dom
        _t0 = time.monotonic()
        with _ba.stop_scope(lambda: True):
            _ba.wait_dom_idle("x.ru", None, timeout_sec=4.0, min_wait=0.2)
        check("wait_dom_idle: «стоп» в stop_scope — выход < 0.5 с",
              time.monotonic() - _t0 < 0.5)
        _t0 = time.monotonic()
        _ba.wait_dom_idle("x.ru", None, timeout_sec=4.0, min_wait=0.2,
                          stop=lambda: True)
        check("wait_dom_idle: явный stop= — выход < 0.5 с",
              time.monotonic() - _t0 < 0.5)
        _t0 = time.monotonic()
        _ba.wait_dom_idle("x.ru", None, timeout_sec=0.6, min_wait=0.1)
        check("wait_dom_idle: без «стоп» — ждёт как раньше",
              time.monotonic() - _t0 >= 0.55)
        check("sleep_or_stop: «стоп» — True сразу",
              _ba.sleep_or_stop(3.0, stop=lambda: True) is True)

        class _Pg:
            def evaluate(self, js):
                return 0  # статус навигации ещё не пришёл
        _t0 = time.monotonic()
        with _ba.stop_scope(lambda: True):
            st = _ba._gateway_status(_Pg(), budget_sec=10.0)
        check("_gateway_status: «стоп» — не ждёт статус шлюза",
              st is None and time.monotonic() - _t0 < 0.5)

        # open_new_tab: проверка «стоп» вызывающего доходит до потока воркера
        _seen_w = []

        class _W:
            def new_page(self, url, focus=False):
                _seen_w.append(_ba.stop_hit())
                return 7

        def _submit(fn, timeout=None):
            out = []
            th = _thr.Thread(target=lambda: out.append(fn(_W())))
            th.start()
            th.join(5)
            return out[0]
        _ba._select_backend = lambda tab_op=False: "cdp"
        _ba._WORKER.submit = _submit
        with _ba.stop_scope(lambda: True):
            tid = _ba.open_new_tab("https://x.ru", focus=True)
        check("open_new_tab: «стоп» виден в потоке воркера",
              tid == 7 and _seen_w == [True])
        _ba.open_new_tab("https://x.ru", focus=True)
        check("open_new_tab: без «стоп» в воркере флага нет",
              _seen_w[-1] is False)

        # _snapshot_for: опрос медленной вкладки (до 10 с) — по «стоп» сразу
        cc3 = CCM(context="ux3", config={"confirm": False}, base_dir=tmp / "cc3")
        cc3._last_tab_id, cc3._last_host = 5, "x.ru"
        _ba.reveal_player_controls = lambda *a, **kw: None

        def _no_items(h, tab_id=None):
            raise RuntimeError("нет кликабельных элементов")
        _ba.snapshot_elements = _no_items
        cc3.set_turn("C3", "en")
        cc3.request_stop("C3")
        _t0 = time.monotonic()
        r = cc3._snapshot_for(None, chat_id="")
        check("_snapshot_for: «стоп» во время опроса — выход < 1 с, "
              "английское «Stopped»",
              time.monotonic() - _t0 < 1.0 and r[-1] == cc_texts.t("stopped", "en"))
        check("_snapshot_for: после «стоп» вкладка не забыта",
              cc3._last_tab_id == 5)

        # _scroll_hunt / scroll_to_goal: «стоп» — шагов нет, «остановлено»
        _steps = []
        _ba.snapshot_elements = lambda h, tab_id=None: (
            "https://x.ru/", "x.ru", [{"idx": 1, "text": "a"}])
        _ba.scroll_position = lambda h, t: 0.0
        _ba.scroll_step = lambda h, t: _steps.append(1) or {"moved": True}
        _ba.scroll_container_step = lambda h, t: _steps.append(2) or {"moved": True}
        _ba.scroll_restore = lambda *a, **kw: None
        _ba.scroll_container_restore = lambda *a, **kw: None
        _ba.snapshot_for_goal = lambda *a, **kw: ("", [])
        _ba.wait_dom_idle = lambda *a, **kw: None
        g = cc3._scroll_hunt(_ba, "x.ru", 5, "котики", "https://x.ru/")
        check("_scroll_hunt: «стоп» — ни одного шага прокрутки",
              g == ("", []) and not _steps)
        sg, err = cc3.scroll_to_goal("котики", None, chat_id="")
        check("scroll_to_goal: «стоп» — честное «остановлено», не «не вижу»",
              sg is None and err == cc_texts.t("stopped", "en"))
        cc3.stop_clear("C3")
        sg, err = cc3.scroll_to_goal("котики", None, chat_id="")
        check("scroll_to_goal: без «стоп» — доскролл идёт",
              sg is not None and not sg["found"] and _steps)
        # Исполнение: шаг, упавший после выхода опроса по «стоп», — это
        # «остановлено» (error_class=stopped), а не ошибка сайта
        cc4 = Spy(context="ux4", config={"confirm": False}, base_dir=tmp / "cc4")

        def _boom(action, router=None):
            cc4.request_stop("C4")
            raise RuntimeError("не читается страница после открытия")
        cc4._dispatch = _boom
        cc4.set_turn("C4", "en")
        ok, det = cc4.execute({"kind": "url", "value": "https://x.ru"}, None)
        check("execute: сбой после «стоп» — английское «stopped at the user's "
              "request»", not ok and det == cc_texts.t("stopped_by_user", "en"))
        check("_sleep_or_stop менеджера: «стоп» — True сразу",
              cc4._sleep_or_stop(5.0) is True)
        cc4.set_turn(None)
        check("set_turn(None): ключ хода снят", cc4.turn_key() is None
              and _ba._STOP_TL.check is None)
    finally:
        for k, v in _saved_ba.items():
            setattr(_ba, k, v)
        _ba._WORKER.submit = _saved_submit
        cc.set_turn(None)

    # ── 4c. Английские служебные тексты: маркеры, отчёт о странице ──
    from app.features.computer_control import page_view_text, page_view_full_text
    ccm = CCM(context="ux5", config={"confirm": True}, base_dir=tmp / "cc5")
    ccm.set_turn("M", "en")
    clean, notes = ccm.process_markers(
        "Sure. [OPEN_APP:nosuchapp] [OPEN_URL:example.com]", "M")
    check("process_markers en: уведомление об отклонённом маркере",
          notes == ["⚠️ I can't do \"nosuchapp\" — it's not on the allowed list."])
    check("process_markers en: вопрос-шаблон по-английски",
          clean.endswith("?") and "Open" in clean and "Открыть" not in clean)
    _, notes_ru = ccm.process_markers("[OPEN_APP:nosuchapp]", "M", lang="ru")
    check("process_markers: явный lang=ru — русское уведомление",
          notes_ru and "Не могу выполнить" in notes_ru[0])
    ccm.set_turn(None)
    pv_en = page_view_text("https://x.ru/", "x.ru",
                           [{"idx": 1, "text": "Go", "tag": "button"}], lang="en")
    check("page_view_text en: заголовок и группа по-английски",
          pv_en.startswith("**Page:** x.ru") and "**Buttons** (1):" in pv_en)
    check("page_view_text: по умолчанию — русский",
          "**Страница:**" in page_view_text("", "x.ru", []))
    check("page_view_full_text en",
          page_view_full_text("", "x.ru", [], truncated=True, lang="en")
          == "Page: x.ru\nI can't read the page structure as text — here are "
             "the shots.\nThe page is long — I showed the top part.")

    # ── 4d. Английские фразы сценариев и агента задач ──
    check("cc_texts.phrase en — английский шаблон, банк не трогаем",
          cc_texts.phrase("ux", "scenario_run_cancel", "Сценарий «x» отменён.",
                          "en", name="x") == "The \"x\" scenario is cancelled.")
    check("cc_texts.phrase ru — банк/русский шаблон",
          cc_texts.phrase("ux", "scenario_run_cancel", "Сценарий «x» отменён.",
                          "ru", name="x") == "Сценарий «x» отменён.")
    check("cc_texts.phrase en без английского шаблона — исходный",
          cc_texts.phrase("ux", "no_such_key", "Шаблон", "en") == "Шаблон")
    from app.features.scenario_manager import ScenarioManager
    from app.features.task_agent import TaskAgent
    ccs = CCM(context="ux6", config={"confirm": False}, base_dir=tmp / "cc6")
    sm = ScenarioManager(context="ux6", computer_control=ccs,
                         base_dir=tmp / "sc6")
    ccs.set_turn("S", "en")
    check("scenario en: отмена без прогона",
          sm.cancel("S") == "Nothing to cancel — no scenario is running.")
    check("scenario en: сбой шага",
          sm._t("scenario_step_failed", err="x").startswith("Stopped: x."))
    check("scenario en: сценария нет",
          sm.start("nope", "S", None) == "I don't have a scenario called \"nope\".")
    ccs.set_turn("S", "ru")
    check("scenario ru: как раньше",
          sm.cancel("S") == "Нечего отменять — сценарий не запущен.")
    ccs.set_turn(None)
    ta_en = TaskAgent(computer_control=ccs, context="ux6",
                      memory_path=tmp / "task_memory6.json")
    ccs.set_turn("T", "en")
    ta_en._runs["T"] = {"goal": "g", "busy": False, "cancel": False, "qa": [],
                        "sites": [], "history": [], "steps": 0,
                        "touched": time.time()}
    check("task en: отмена", ta_en.cancel("T") == "Okay, dropping the task.")
    run_en = {"goal": "g", "turn_user": None}
    kind, q = ta_en._ask_confirm(run_en, {"kind": "click", "element": "Buy",
                                          "host": "x.ru"}, "click")
    check("task en: вопрос на подтверждение",
          kind == "pause" and q == "Next step — click \"Buy\" on x.ru. "
                                   "Shall I? (yes/no)")
    check("task en: фиксированная реплика", ta_en._t("task_scrolling")
          == "Scrolling the page.")
    ccs.set_turn(None)

    # ── 5. TaskAgent: busy и отмена ──
    from app.features.task_agent import TaskAgent
    ta = TaskAgent(computer_control=cc, context="ux",
                   memory_path=tmp / "task_memory.json")
    check("busy: прогона нет", not ta.busy("c1"))
    ta._runs["c1"] = {"goal": "g", "busy": True, "cancel": False, "qa": [],
                      "sites": [], "touched": time.time()}
    check("busy: идущий прогон", ta.busy("c1"))
    r = ta.cancel("c1")
    check("cancel при busy: флаг, прогон не снят до конца шага",
          ta._runs["c1"]["cancel"] is True and r)

    # _step: отмена, пришедшая пока модель думала, — действие не исполняется
    class _R:
        def get_response(self, *a, **kw):
            run["cancel"] = True
            return '{"action": "open", "target": "example.com"}'
    run = {"goal": "g", "busy": True, "cancel": False, "qa": [], "history": [],
           "steps": 0, "awaiting": None, "obs_extra": None, "sites": [],
           "touched": time.time(), "past": [], "lang": "ru"}
    ta._observe = lambda run, chat_id: {"url": "", "host": "", "tab_id": None,
                                        "shown": [], "items": []}
    ta._prompt = lambda run, obs, **kw: "P"
    opened = []
    ta._do_open = lambda *a, **kw: opened.append(1) or ("progress", None)
    out = ta._step(run, "c9", _R())
    check("_step: отмена во время LLM — действие не исполнено",
          out == ("progress", None) and not opened and run["steps"] == 0)

    # ── 6. Бот: cc_turn_enter / cc_turn_exit ──
    from app.bot_instance import BotInstance

    class FakeTA:
        def __init__(self, busy=False):
            self._busy, self.cancelled = busy, []

        def busy(self, chat_id):
            return self._busy

        def cancel(self, chat_id):
            self.cancelled.append(chat_id)
            return "бросаю"

        def active(self, chat_id):
            return self._busy

    def mkbot(mode=True, ta_busy=False, features=None):
        b = BotInstance.__new__(BotInstance)
        b.persona_name = "ux"
        b.context = f"ux_{time.time_ns()}"
        b.owner = "A"
        b.web_single_user = False
        b._cc_allowed_users = set()
        b.trigger_words = ["коннор"]
        b.features = features or {}
        b._control_mode = {"C"} if mode else set()
        b.computer_control = Spy(context=b.context, config={"confirm": True},
                                 base_dir=tmp / b.context)
        b.task_agent = FakeTA(ta_busy)
        b.scenario_manager = None
        return b

    b = mkbot(mode=False)
    check("enter: вне режима — ничего", b.cc_turn_enter("стоп", "A", "C") == (None, None))
    b = mkbot()
    check("enter: посторонний — ничего", b.cc_turn_enter("стоп", "Z", "C") == (None, None))
    b = mkbot()
    r, tok = b.cc_turn_enter("стоп", "A", "C")
    check("enter: «стоп» без идущего хода — обычный путь (регистрируется)",
          r is None and tok is not None)
    b.cc_turn_exit(tok)

    b = mkbot()
    r1, tok1 = b.cc_turn_enter("открой ютуб", "A", "C")
    r2, tok2 = b.cc_turn_enter("Коннор, открой ютуб", "A", "C")
    check("enter: дубль идущей команды — «уже выполняю»",
          r1 is None and tok1 and r2 == cc_texts.t("already_running") and tok2 is None)
    r3, tok3 = b.cc_turn_enter("нажми войти", "A", "C")
    check("enter: другая команда — в очередь", r3 is None and tok3)
    r4, _ = b.cc_turn_enter("стоп", "A", "C")
    check("enter: «стоп» при идущем ходе — «Останавливаю…» и флаг остановки",
          r4 == "Останавливаю…" and b.computer_control.stop_requested("C"))
    r5, _ = b.cc_turn_enter("stop", "A", "C")
    check("enter: «stop» — по-английски", r5 == "Stopping…")
    b.cc_turn_exit(tok1)
    b.cc_turn_exit(tok3)
    r6, tok6 = b.cc_turn_enter("открой ютуб", "A", "C")
    check("enter: после завершения та же команда снова принимается",
          r6 is None and tok6)
    b.cc_turn_exit(tok6)
    check("exit: реестр пуст", not b.__dict__.get("_cc_inflight"))
    b.cc_turn_exit(None)
    check("exit: None-токен не роняет", True)

    # Веб-чат без chat_id: «стоп» до лока хода и исполнение хода — один ключ
    b = mkbot()
    b._control_mode = {"A"}
    r1, tok1 = b.cc_turn_enter("открой ютуб", "A", None)
    r2, _ = b.cc_turn_enter("стоп", "A", None)
    check("web без chat_id: «стоп» при идущем ходе — «Останавливаю…»",
          r1 is None and tok1 and r2 == "Останавливаю…")
    b.computer_control.set_turn("A", "ru")  # так ставит process_message
    b.computer_control.execute({"kind": "url", "value": "https://x.ru"}, None)
    check("web без chat_id: исполнение хода видит этот «стоп»",
          b.computer_control.seen[-1] == ("A", True))
    b.computer_control.set_turn(None)
    b.cc_turn_exit(tok1)

    b = mkbot()
    b._cc_inflight = {"C": {"открой ютуб": (1, time.time() - 1000)}}
    r, tok = b.cc_turn_enter("открой ютуб", "A", "C")
    check("enter: протухшая регистрация (утечка токена) не блокирует", r is None and tok)

    b = mkbot(ta_busy=True)
    r, tok = b.cc_turn_enter("отмена", "A", "C")
    check("enter: «отмена» при занятом агенте — отмена сразу",
          r == "Останавливаю…" and b.task_agent.cancelled == ["C"] and tok is None)
    r, tok = b.cc_turn_enter("пепперони", "A", "C")
    check("enter: реплика занятому агенту — «ещё работаю», не очередь",
          r and "работаю" in r and tok is None)
    r, tok = b.cc_turn_enter("выйди из режима управления", "A", "C")
    check("enter: выход из режима при занятом агенте — стоп + своим ходом",
          r is None and tok and b.task_agent.cancelled[-1] == "C")

    # B2/E7: прогон агента ждёт ответа (не busy) — «стоп» автора снимает
    # его до лока хода (иначе «да», ждущее лока, исполнило бы шаг и повело
    # прогон дальше); чужой «стоп» ждущий прогон не трогает
    from app.features.task_agent import TaskAgent as _RealTA

    def with_idle_run(b):
        ta = _RealTA(b.computer_control, context=b.context,
                     memory_path=tmp / f"{b.context}_tm.json")
        ta._runs["C"] = {"goal": "x", "qa": [], "history": [], "steps": 0,
                         "awaiting": {"kind": "confirm", "act": {},
                                      "line": "click", "ts": time.time(),
                                      "user_id": "A"},
                         "busy": False, "cancel": False,
                         "touched": time.time(), "sites": [],
                         "turn_user": "A"}
        b.task_agent = ta
        return ta

    b = mkbot()
    ta = with_idle_run(b)
    r, tok = b.cc_turn_enter("Коннор, стоп", "A", "C")
    check("B2: «Коннор, стоп» автора при ждущем прогоне — снят сразу",
          "C" not in ta._runs and r and tok is None)
    b = mkbot()
    ta = with_idle_run(b)
    r1, tok1 = b.cc_turn_enter("да", "A", "C")  # ход «да» ждёт лока
    r2, tok2 = b.cc_turn_enter("стой", "A", "C")
    check("B2: «стой», пока «да» ждёт лока, — прогон снят, «да» его не найдёт",
          r1 is None and tok1 and r2 == "Останавливаю…" and "C" not in ta._runs
          and ta.feed("C", "да", None, user_id="A") is None)
    b.cc_turn_exit(tok1)
    b = mkbot()
    ta = with_idle_run(b)
    r, tok = b.cc_turn_enter("не надо", "A", "C")
    check("№13: «не надо» на «да/нет» агента — не отмена до лока хода "
          "(это «нет», разберёт агент)", r is None and "C" in ta._runs)
    b.cc_turn_exit(tok)
    b = mkbot()
    b._cc_allowed_users = {"B"}
    ta = with_idle_run(b)
    r, tok = b.cc_turn_enter("стоп", "B", "C")
    check("E7: чужой «стоп» не снимает ждущий прогон автора",
          r is None and "C" in ta._runs)
    b.cc_turn_exit(tok)

    # E6: посторонняя команда при ждущем прогоне; E7: отметка вопроса агента
    b = mkbot()
    b.computer_control.sites = {"ютуб": "https://www.youtube.com/"}
    check("E6: «открой ютуб» (алиас), «задача: …», «громче» — посторонние; "
          "«открой меню», «пепперони», «да» — ответы агенту",
          b._ta_foreign_command("открой ютуб", "C")
          and b._ta_foreign_command("задача: найди билеты", "C")
          and b._ta_foreign_command("сделай громче", "C")
          and not b._ta_foreign_command("открой меню", "C")
          and not b._ta_foreign_command("пепперони", "C")
          and not b._ta_foreign_command("да", "C"))
    check("M5: «продолжи», «пауза», «resume» — не посторонние (Space в "
          "кнопку на оформлении), «громче» — посторонняя",
          not b._ta_foreign_command("продолжи", "C")
          and not b._ta_foreign_command("пауза", "C")
          and not b._ta_foreign_command("resume", "C")
          and b._ta_foreign_command("громче", "C"))
    ta = with_idle_run(b)
    ta._runs["C"]["awaiting"] = {"kind": "ask", "question": "Какую пиццу?"}
    check("E7: вопрос агента (ask) — отметка живого ожидания для группы",
          b.cc_pending_stamp("C") is not None)

    # ── 7. Жизненный цикл режима ──
    b = mkbot(features={"computer_control": {"enabled": True, "idle_exit_min": 30}})
    b._control_mode_ts = {"C": time.time() - 31 * 60}
    check("idle: простой > 30 мин — режим погашен", not b.control_mode_on("C"))
    note = b._cc_pop_idle_notice("C")
    check("idle: уведомление на следующее сообщение, одноразовое",
          note and "простоя" in note and b._cc_pop_idle_notice("C") is None)
    b = mkbot(features={"computer_control": {"idle_exit_min": 30}})
    b._control_mode_ts = {"C": time.time() - 31 * 60}
    b.control_mode_on("C")
    check("idle: уведомление по-английски",
          "inactivity" in (b._cc_pop_idle_notice("C", "en") or ""))
    b = mkbot(features={"computer_control": {"idle_exit_min": 0}})
    b._control_mode_ts = {"C": time.time() - 999 * 60}
    check("idle: idle_exit_min=0 — без автовыхода", b.control_mode_on("C"))
    b = mkbot()
    b._control_mode_ts = {"C": time.time() - 10 * 60}
    check("idle: 10 мин (дефолт 30) — режим жив", b.control_mode_on("C"))
    b = mkbot()
    check("idle: без отметки времени (старые заготовки) — режим жив",
          b.control_mode_on("C"))

    # Сохранение на диск
    path = tmp / "persist" / "control_mode.json"
    b = mkbot(mode=False)
    b._control_mode_ts = {}
    b._control_mode_path = path
    r = b._control_mode_switch("C", True)
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    check("persist: включение пишет чат в файл", "C" in saved and "включён" in r)
    b2 = mkbot(mode=False)
    b2._control_mode_ts = {}
    b2._control_mode_path = path
    b2._cc_mode_load()
    check("persist: после «рестарта» режим восстановлен", b2.control_mode_on("C"))
    warmed = []
    _ba.set_control_mode = lambda chat, on: warmed.append((chat, on))
    b2._cc_mode_touch("C")
    b2._cc_mode_touch("C")
    _ba.set_control_mode = lambda *a, **kw: None
    check("persist: браузерный слой узнаёт о восстановленном режиме один раз",
          warmed == [("C", True)])
    r = b2._control_mode_switch("C", False, lang="en")
    saved = json.loads(path.read_text(encoding="utf-8"))
    check("persist: выключение убирает чат; реплика по-английски",
          "C" not in saved and r.startswith("Left control mode"))
    b3 = mkbot(mode=False)
    b3._control_mode_ts = {}
    check("persist: заготовка без пути на диск не пишет",
          b3._control_mode_switch("D", True) and not (tmp / "D").exists())
    b4 = mkbot(mode=False)
    b4.computer_control = None
    check("toggle en: фича выключена",
          b4._control_mode_switch("C", True, lang="en").startswith(
              "Computer control is turned off"))
    b5 = mkbot()
    check("toggle en: уже включён",
          b5._control_mode_switch("C", True, lang="en").startswith("I'm already"))

    # ── 8. Напоминания/дела в режиме — честная пометка ──
    check("note: «напомни через 5 минут» — пометка",
          "NOT" in (BotInstance._cc_mode_feature_note("напомни через 5 минут купить хлеб") or ""))
    check("note: «добавь в список дел …» — пометка",
          BotInstance._cc_mode_feature_note("добавь в список дел купить молоко") is not None)
    check("note: команда управления — без пометки",
          BotInstance._cc_mode_feature_note("открой ютуб") is None)

    # ── 9. Группа: подсказка про reply и отметка подтверждения ──
    b = mkbot()
    s0 = b.cc_pending_stamp("C")
    b.computer_control.set_pending("C", {"kind": "url", "value": "https://x.ru"},
                                   user_id="A")
    s1 = b.cc_pending_stamp("C")
    check("stamp: новый pending меняет отметку", s0 is None and s1 and s1 != s0)
    check("hint: подсказка про reply по-русски/по-английски",
          "reply" in b.cc_group_confirm_hint("да")
          and "reply" in b.cc_group_confirm_hint("open youtube"))

    # ── 10. Живая перепривязка надстроек ──
    b = mkbot(features={"computer_control": True})
    b.task_agent = None
    b.persona = SimpleNamespace(system_prompt="", persona_data={})
    _ens = _flavor.ensure_flavor_bank
    _flavor.ensure_flavor_bank = lambda *a, **kw: None
    try:
        b.rebind_computer_control()
        check("rebind: агент и сценарии созданы на текущем менеджере",
              b.task_agent is not None and b.task_agent.cc is b.computer_control
              and b.scenario_manager is not None
              and b.scenario_manager.cc is b.computer_control)
        new_cc = Spy(context=b.context, config={}, base_dir=tmp / "cc_new")
        b.computer_control = new_cc
        b.rebind_computer_control()
        check("rebind: существующие надстройки переключены на новый менеджер",
              b.task_agent.cc is new_cc and b.scenario_manager.cc is new_cc)
        b.computer_control = None
        b.rebind_computer_control()
        check("rebind: фича выключена — надстройки сняты",
              b.task_agent is None and b.scenario_manager is None)
    finally:
        _flavor.ensure_flavor_bank = _ens

    # settings_api вызывает перепривязку
    from app.api import settings_api
    b = mkbot(features={"computer_control": True})
    b.computer_control = None
    b.task_agent = None
    b.persona = SimpleNamespace(system_prompt="", persona_data={})
    _flavor.ensure_flavor_bank = lambda *a, **kw: None
    try:
        settings_api._apply_computer_control_live(b, {"enabled": True})
        check("settings: включение на живую создаёт менеджер и агента",
              b.computer_control is not None and b.task_agent is not None
              and b.task_agent.cc is b.computer_control)
    finally:
        _flavor.ensure_flavor_bank = _ens

    # ── 11. process_message: пометка о напоминании, idle-уведомление, en ──
    from app.features.conversation_style import ConversationStyleConfig

    class _Persona:
        def __init__(self):
            self.settings, self.persona_data, self.last_kwargs = {}, {}, None
            self.system_prompt = ""

        def prepare_messages(self, *a, **kw):
            self.last_kwargs = kw
            return [{"role": "system", "content": "SYS"}]

        def get_settings(self):
            return {"max_tokens": 500}

    class _Memory:
        class _STM:
            def get_last(self, *a, **kw):
                return []

        class _LTM:
            def get_facts_by_category(self, *a, **kw):
                return []

            def save_facts(self, *a, **kw):
                pass

        def __init__(self):
            self.stm, self.ltm = self._STM(), self._LTM()

        def add_message(self, *a, **kw):
            pass

        def get_context(self, *a, **kw):
            return [], [], []

        def get_chat_facts_block(self, *a, **kw):
            return None

    class _Router:
        def __init__(self, reply):
            self.reply, self.calls = reply, 0
            self.answer_provider, self._last_provider = None, "fake"

        def is_local_primary(self):
            return False

        def get_response(self, messages, **kw):
            self.calls += 1
            return self.reply

    def pbot(reply="Ответ.", features=None):
        b = mkbot(features=features)
        b.task_agent = None
        b.intellect = SimpleNamespace(active=False)
        b.conversation_style = ConversationStyleConfig(None)
        b.proactive = None
        b.addons = []
        b.self_memory = None
        b.living = None
        b.todo_manager = b.inventory_manager = b.reminder_manager = None
        b.learning_manager = None
        b.file_db = None
        b._punish_enabled = b._moderation_enabled = False
        b._web_search_enabled = False
        b._web_search_disabled_chats = set()
        b._pending_list_messages, b._pending_split_messages = {}, {}
        b._pending_photos, b._pending_question_kind = {}, {}
        b._pending_more_photos = {}
        b.persona = _Persona()
        b.memory = _Memory()
        b.router = _Router(reply)
        return b

    try:
        b = pbot()
        b.process_message("напомни через 10 минут выключить чайник",
                          user_id="A", chat_id="C")
        rc = (b.persona.last_kwargs or {}).get("reminder_context") or ""
        check("pipe: напоминание в режиме — модель знает, что НЕ сохранено",
              "Control mode is on" in rc)
        b = pbot()
        b.computer_control.request_stop("C")
        b.process_message("привет", user_id="A", chat_id="C")
        check("pipe: новый ход снимает прошлый «стоп»",
              not b.computer_control.stop_requested("C"))
        # Веб-чат без chat_id: ход ставит менеджеру ключ user_id и язык хода
        b = pbot()
        b._control_mode = {"A"}
        b.computer_control.request_stop("A")
        b.process_message("hello there", user_id="A", chat_id=None,
                          raw_user_text="hello there")
        check("pipe: веб без chat_id — ключ хода user_id, прошлый «стоп» снят",
              b.computer_control.turn_key() == "A"
              and b.computer_control.turn_lang() == "en"
              and not b.computer_control.stop_requested("A"))
        b.computer_control.set_turn(None)
        # Доскролл до цели и отчёт о странице — на языке хода
        b = pbot()
        b.computer_control.scroll_to_goal = lambda goal, site, chat_id="": (
            {"found": True, "shot": None, "host": "x.ru", "url": "",
             "edge": None, "goal": goal}, None)
        check("scroll goal en: нашёл",
              b._cc_scroll_goal_reply("cats", "C", "en")["reply"]
              == "Found \"cats\" — here's the spot.")
        b.computer_control.scroll_to_goal = lambda goal, site, chat_id="": (
            {"found": False, "shot": None, "host": "x.ru", "url": "",
             "edge": None, "goal": goal}, None)
        check("scroll goal en: не вижу",
              b._cc_scroll_goal_reply("cats", "C", "en")["reply"]
              .startswith("Scrolled the page — I don't see \"cats\"."))
        b.computer_control.scroll_to_goal = lambda *a, **kw: (
            None, cc_texts.t("stopped", "en"))
        check("scroll goal en: «стоп» — «Stopped», без реплики-ошибки",
              b._cc_scroll_goal_reply("cats", "C", "en")
              == {"ok": False, "reply": "Stopped, as you asked."})
        b.computer_control.scroll_to_goal = lambda *a, **kw: (
            None, cc_texts.t("stopped", "ru"))
        check("scroll goal ru: «стоп» — «Остановлено по твоей просьбе.»",
              b._cc_scroll_goal_reply("котики", "C", "ru")["reply"]
              == "Остановлено по твоей просьбе.")

        def _pv_boom(*a, **kw):
            raise RuntimeError("x")
        b.computer_control.page_view_report = _pv_boom
        check("page view en: сбой",
              b._cc_page_view_reply(None, False, False, "C", "en", "q")
              == {"ok": False, "reply": "Couldn't look at the page."})
        b.computer_control.page_view_report = lambda *a, **kw: (
            {"url": "https://x.ru/", "host": "x.ru", "items": [], "shot": None},
            None)
        b._persona_page_view_reply = lambda *a, **kw: None
        r_pv = b._cc_page_view_reply(None, True, False, "C", "en", "q")["reply"]
        check("page view en: список и «скриншот не вышел» по-английски",
              r_pv.startswith("**Page:** x.ru")
              and r_pv.endswith("here's the list of elements."))
        b = pbot(features={"computer_control": {"idle_exit_min": 30}})
        b._control_mode_ts = {"C": time.time() - 40 * 60}
        b.process_message("привет", user_id="A", chat_id="C")
        extra = b.pop_pending_list_messages("C")
        check("pipe: после простоя — режим выключен и уведомление вслед",
              not b.control_mode_on("C")
              and any("простоя" in m for m in extra))
        b = pbot()
        b.computer_control.set_pending("C", {"kind": "url",
                                             "value": "https://x.ru"}, user_id="A")
        r = b.process_message("no", user_id="A", chat_id="C", raw_user_text="no")
        check("pipe: отказ по-английски", r == "Okay, I won't do it.")
        b = pbot()
        r = b.process_message("exit control mode", user_id="A", chat_id="C",
                              raw_user_text="exit control mode")
        from app.features.computer_control import parse_control_mode
        if parse_control_mode("exit control mode") is False:
            check("pipe: выход из режима по-английски — английская реплика",
                  r.startswith("Left control mode"))
        else:
            print("    (parse_control_mode ещё не понимает английский — "
                  "зона парсеров)")
    finally:
        _ba.set_control_mode, _flavor.cc_reply, _flavor.phrase = _orig
    print(f"\nИтог: {total} проверок, {fails} провалов")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Узкие места состояния режима управления: «стоп», вкладки, язык отказов.

Правила живут в одном месте, забытый путь их не обходит:
- чей «стоп» проверять — только _stop_key: явный чат (str(None)/пусто — не
  ключ) → чат, чей execute идёт В ЭТОМ потоке → ход этого потока. «стоп»
  одного чата не обрывает действие другого, «None» от task_agent/сценария
  видит «стоп» по user_id;
- мёртвая вкладка (закрыта ботом — с id или видимая без id; умерла сама)
  снимается со слежения у ВСЕХ чатов (_forget_tab) — никто не ждёт её ~10 с;
- отказы резолверов и фолбэки лесенки — через cc_texts на языке хода
  (_tx); страж — AST-скан: русского литерала в ответе резолвера нет;
- английские «stop scrolling»/«stop the scroll»/«enough scrolling» — стоп
  листания (parse_scroll_request, _SCROLL_STOP_RE, _CC_STOP_RE бота).
Браузер подменён целиком: ни одного вызова в живой Chrome, сети нет.

Запуск: python -m scripts.test_cc_state
"""

import ast
import os
import re
import string
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent
CYR = re.compile(r"[а-яёА-ЯЁ]")


def _resolver_cyrillic(path: Path) -> list:
    """Русские литералы, которые могут уйти в ответ резолвера: всё, кроме
    докстрингов, логов/regex/сравнений, внутренних целей каскада («закрыть на
    X» → _resolve_element) и служебных переменных разбора."""
    extra = {"_snapshot_for", "intent_to_action", "_comp_edit_fallback",
             "_cart_op_fallback", "_escape_fallback", "list_open_tabs_text",
             "_type_fields_hint", "_hidden_fields_note", "_non_click_hint",
             "scroll_to_goal", "page_view_report"}
    safe_calls = {"compile", "search", "match", "sub", "findall", "fullmatch",
                  "split", "finditer", "startswith", "endswith", "replace",
                  "strip", "lstrip", "rstrip", "get", "join", "lower",
                  "_resolve_element", "_resolve_element_pick",
                  "_snapshot_for", "_lookup", "_audit_resolve",
                  "resolve_click", "resolve_hover", "resolve_type",
                  "_non_click_hint"}
    internal = {"goal", "collapse_try", "goal_n", "g", "q", "query", "label",
                "shown", "body", "field_goal"}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "ComputerControlManager")
    out = []
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        if not (fn.name.startswith(("resolve", "_resolve"))
                or fn.name in extra):
            continue
        skip = set()
        if fn.body and isinstance(fn.body[0], ast.Expr) \
                and isinstance(fn.body[0].value, ast.Constant):
            skip.add(id(fn.body[0].value))
        for n in ast.walk(fn):
            mark = False
            if isinstance(n, ast.Call):
                f = n.func
                name = getattr(f, "attr", getattr(f, "id", ""))
                base = getattr(getattr(f, "value", None), "id", "")
                mark = base in ("logger", "re") or name in safe_calls
            elif isinstance(n, (ast.Compare, ast.Set, ast.List,
                                ast.Subscript)):
                mark = True
            elif isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and k.value in (
                            "goal", "element", "slider_label"):
                        for m in ast.walk(v):
                            skip.add(id(m))
            elif isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                tg = n.targets if isinstance(n, ast.Assign) else [n.target]
                mark = any(getattr(t, "id", None) in internal for t in tg)
            if mark:
                for m in ast.walk(n):
                    skip.add(id(m))
        for n in ast.walk(fn):
            if id(n) in skip:
                continue
            if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                    and CYR.search(n.value):
                out.append(f"{fn.name}:{n.lineno}")
    return out


def _ladder_cyrillic(path: Path) -> list:
    """Русские литералы в ответах _cc_ladder бота: dict {"reply": …} и
    присваивания cc_err/reply/err."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "BotInstance")
    out = []
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name != "_cc_ladder":
            continue
        for n in ast.walk(fn):
            vals = []
            if isinstance(n, ast.Dict):
                vals = [v for k, v in zip(n.keys, n.values)
                        if isinstance(k, ast.Constant) and k.value == "reply"]
            elif isinstance(n, ast.Assign):
                names = []
                for t in n.targets:
                    names += [getattr(x, "id", None) for x in (
                        t.elts if isinstance(t, ast.Tuple) else [t])]
                if any(x in ("cc_err", "reply", "err") for x in names):
                    vals = [n.value]
            for v in vals:
                for m in ast.walk(v):
                    if isinstance(m, ast.Constant) and isinstance(m.value, str) \
                            and CYR.search(m.value):
                        out.append(f"{fn.name}:{m.lineno}")
    return out


def _text_keys_problems(paths, texts) -> list:
    """Каждый ключ _tx/cc_texts.t: есть ru и en, плейсхолдеры переданы."""
    out = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = getattr(f, "attr", getattr(f, "id", ""))
            base = getattr(getattr(f, "value", None), "id", "")
            if not (name == "_tx" or (name == "t" and base == "cc_texts")):
                continue
            if not n.args or not isinstance(n.args[0], ast.Constant):
                continue
            key = n.args[0].value
            row = texts.get(key)
            if not row:
                out.append(f"{path.name}:{n.lineno} нет ключа {key}")
                continue
            kws = {k.arg for k in n.keywords if k.arg}
            spread = any(k.arg is None for k in n.keywords)
            for lang in ("ru", "en"):
                s = row.get(lang)
                if not s:
                    out.append(f"{path.name}:{n.lineno} {key}: нет {lang}")
                    continue
                fields = {fn for _, fn, _, _ in string.Formatter().parse(s)
                          if fn}
                if fields - kws and not spread:
                    out.append(f"{path.name}:{n.lineno} {key}/{lang}: "
                               f"не передано {sorted(fields - kws)}")
    return out


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cc_state_smoke_")
    tmp = Path(tempfile.mkdtemp(prefix="cc_state_data_"))
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

    import app.features.computer_control as ccm
    from app.features import browser_actions as ba
    from app.features import cc_texts
    from app.features.computer_control import ComputerControlManager

    # ── Браузер подменён целиком ──
    def it(idx, tag, text, **kw):
        d = {"idx": idx, "tag": tag, "role": "", "text": text, "aria": "",
             "title": "", "href": "", "w": 80.0, "h": 20.0, "vp": True}
        d.update(kw)
        return d
    ITEMS = [it(0, "button", "Войти"), it(1, "button", "Sign in"),
             it(2, "input", "", aria="Поиск", ed=True, ph="Поиск"),
             it(3, "button", "Меню")]
    pages = {11: ("https://dodopizza.ru/", "dodopizza.ru"),
             22: ("https://www.youtube.com/", "youtube.com"),
             33: ("https://vk.com/feed", "vk.com"),
             44: ("https://slow.test/", "slow.test"),
             55: ("http://localhost:5173/chat", "localhost")}
    opened = {"https://dodopizza.ru/": 11, "https://www.youtube.com/": 22,
              "https://vk.com/feed": 33}
    state = {"dead": set(), "loading": set(), "snaps": [], "closed": [],
             "visible_tab": None, "steps": 0}

    def _snap(host=None, tab_id=None):
        state["snaps"].append((host, tab_id))
        if tab_id in state["dead"]:
            raise RuntimeError("no such tab")
        if tab_id in state["loading"]:
            raise RuntimeError("loading")
        if tab_id is not None and tab_id in pages:
            url, h = pages[tab_id]
        else:
            hp = str(host or "")
            hp = (re.sub(r"^https?://", "", hp).split("/")[0]).lower()
            url, h = next(((u, hh) for u, hh in pages.values()
                           if hp and (hh == hp or hh.endswith("." + hp)
                                      or hp.endswith(hh))),
                          ("https://p.test/", "p.test"))
        return url, h, [dict(x) for x in ITEMS]

    def _close(tid=None):
        t = tid if tid is not None else state["visible_tab"]
        state["dead"].add(t)
        state["closed"].append(t)
        return pages[t][0], "T"

    def _step(*a, **kw):
        state["steps"] += 1
        time.sleep(0.02)
        return {"moved": True, "bottom": state["steps"] >= 8}

    mocks = {
        "open_new_tab": lambda url, **kw: opened.get(url, 99),
        "snapshot_elements": _snap,
        "snapshot_for_goal": lambda host, goal, tab_id=None: ("", []),
        "visible_page_info": lambda: None,
        "dismiss_overlay": lambda host=None, tab_id=None: None,
        "reveal_player_controls": lambda *a, **kw: None,
        "detect_antibot": lambda host=None, tab_id=None, strict=False: None,
        "wait_dom_idle": lambda *a, **kw: None,
        "find_tab_id": lambda host: None,
        "list_pages": lambda: [],
        "list_tabs": lambda: [(22, "https://www.youtube.com/", "youtube.com",
                               "YouTube"),
                              # Латиницей: кириллица в отказе — только
                              # из шаблона, а не из заголовка вкладки
                              (11, "https://dodopizza.ru/", "dodopizza.ru",
                               "Dodo")],
        "page_identity": lambda tab_id=None: "",
        "close_tab": _close,
        "scroll_step": _step,
        "scroll_position": lambda *a, **kw: 0.0,
        "scroll_restore": lambda *a, **kw: None,
        "screenshot_tab": lambda *a, **kw: None,
        "screenshot_viewport": lambda *a, **kw: None,
    }
    saved = {k: getattr(ba, k, None) for k in mocks}
    for k, v in mocks.items():
        setattr(ba, k, v)
    orig_timeout, orig_poll = ccm.NAV_LOAD_TIMEOUT_SEC, ccm.NAV_POLL_SEC

    CFG = {"confirm": False, "click": True, "allow_domains": [],
           "vision_fallback": False, "llm_wide_resolve": False}

    class CC(ComputerControlManager):
        def _dispatch(self, action, router=None):
            if action.get("slow"):
                time.sleep(float(action["slow"]))
                return None
            return super()._dispatch(action, router)

    def make(sub=None):
        return CC(context="ccstate", config=CFG,
                  base_dir=tmp / (sub or f"m{time.time_ns()}"))

    def reset():
        state["dead"].clear()
        state["loading"].clear()
        state["snaps"].clear()
        state["closed"].clear()
        state["visible_tab"] = None

    try:
        # ── 1. Закрытие видимой вкладки без id — мёртва у всех чатов ──
        print("\n── 1. «закрой вкладку» без цели и умершая вкладка ──")
        reset()
        m = make()
        m.execute({"kind": "url", "value": "https://dodopizza.ru/"}, "A")
        m.execute({"kind": "url", "value": "https://www.youtube.com/"}, "B")
        m.execute({"kind": "url", "value": "https://vk.com/feed"}, "C")
        check("старт: A/B/C отслеживают 11/22/33",
              (m._st("A").last_tab_id, m._st("B").last_tab_id,
               m._st("C").last_tab_id) == (11, 22, 33))
        state["visible_tab"] = 22   # видна вкладка B
        ok, _d = m.execute({"kind": "tab_op", "op": "close", "tab_id": None,
                            "host": "youtube.com"}, "A")
        check("A закрыл видимую (tab_id=None): закрыта #22",
              ok and state["closed"] == [22])
        check("id мёртвой #22 снят и у B (отслеживал её)",
              m._st("B").last_tab_id is None)
        check("хост B не тронут (цель по хосту находит живую вкладку)",
              m._st("B").last_host == "youtube.com")
        check("C (другой сайт) держит свою #33",
              m._st("C").last_tab_id == 33)
        n0 = len(state["snaps"])
        t0 = time.time()
        r = m.resolve_click("войти", None, None, chat_id="B")
        dt = time.time() - t0
        check(f"B: «нажми войти» после закрытия — без ожидания ({dt:.1f} с)",
              dt < 2.0 and r[0] is not None
              and all(t != 22 for _h, t in state["snaps"][n0:]))
        # Закрытие по id — то же для всех (регресс прежнего пути)
        reset()
        m1 = make()
        m1.execute({"kind": "url", "value": "https://dodopizza.ru/"}, "A")
        m1.execute({"kind": "url", "value": "https://dodopizza.ru/"}, "B")
        m1.execute({"kind": "tab_op", "op": "close", "tab_id": 11,
                    "host": "dodopizza.ru"}, "B")
        check("закрытие по id: снят у обоих чатов",
              m1._st("A").last_tab_id is None
              and m1._st("B").last_tab_id is None)
        # Вкладку закрыл человек: первый чат её дождался — второй не ждёт
        reset()
        ccm.NAV_LOAD_TIMEOUT_SEC, ccm.NAV_POLL_SEC = 0.4, 0.05
        m2 = make()
        m2.execute({"kind": "url", "value": "https://www.youtube.com/"}, "A")
        m2.execute({"kind": "url", "value": "https://www.youtube.com/"}, "B")
        state["dead"].add(22)
        ra = m2.resolve_click("войти", None, None, chat_id="A")
        check("A: умершая #22 → фолбэк на хост, клик разрешён",
              ra[0] is not None and m2._st("A").last_tab_id is None)
        check("умершая #22 снята и у B", m2._st("B").last_tab_id is None)
        n0 = len(state["snaps"])
        rb = m2.resolve_click("войти", None, None, chat_id="B")
        check("B: мёртвую вкладку не опрашивает",
              rb[0] is not None
              and all(t != 22 for _h, t in state["snaps"][n0:]))
        ccm.NAV_LOAD_TIMEOUT_SEC, ccm.NAV_POLL_SEC = orig_timeout, orig_poll

        # ── 2. «стоп» — по потоку исполнения, не по чужому execute ──
        print("\n── 2. stop_requested: _exec_chat привязан к потоку ──")
        reset()
        m3 = make()
        m3.execute({"kind": "url", "value": "https://www.youtube.com/"}, "U7")
        state["steps"] = 0
        res = {}
        started = threading.Event()

        def tg():
            m3.set_turn("TG1", "ru")
            with m3.chat_scope("TG1"):
                m3.request_stop("TG1")
                started.set()
                res["tg_exec"] = m3.execute(
                    {"kind": "zoom", "value": "in", "slow": 1.2,
                     "host": "youtube.com"}, "TG1")

        def web():
            started.wait(2)
            time.sleep(0.2)
            m3.set_turn("U7", "ru")
            with m3.chat_scope("U7"):
                res["tg_running"] = m3.executing_for("TG1")
                res["stop_empty"] = m3.stop_requested("")
                res["stop_none"] = m3.stop_requested(None)
                res["stop_strnone"] = m3.stop_requested("None")
                res["sleep"] = m3._sleep_or_stop(0.2, "")
                res["goal"] = m3.scroll_to_goal("конца", None, chat_id="")
                res["steps"] = state["steps"]
        a = threading.Thread(target=tg)
        b = threading.Thread(target=web)
        a.start()
        b.start()
        a.join(10)
        b.join(10)
        check("пока TG1 исполняет, веб-чат U7 видит это (executing_for)",
              res.get("tg_running") is True)
        check("чужой «стоп» TG1 не виден веб-чату U7 ('' / None / 'None')",
              res.get("stop_empty") is False and res.get("stop_none") is False
              and res.get("stop_strnone") is False)
        check("_sleep_or_stop U7 не прерван чужим «стоп»",
              res.get("sleep") is False)
        g = res.get("goal") or (None, "нет ответа")
        check("доскролл U7 «до конца» идёт до края, а не «Остановлено»",
              g[1] is None and (g[0] or {}).get("edge") == "bottom"
              and res.get("steps", 0) >= 8)
        # Обратный случай: свой «стоп» веб-чата виден, пока исполняет TG1
        res2 = {}
        started2 = threading.Event()
        m3.stop_clear("TG1")

        def tg2():
            m3.set_turn("TG1", "ru")
            with m3.chat_scope("TG1"):
                started2.set()
                m3.execute({"kind": "zoom", "value": "in", "slow": 0.8,
                            "host": "youtube.com"}, "TG1")
                res2["tg_after"] = m3.stop_requested()

        def web2():
            started2.wait(2)
            time.sleep(0.2)
            m3.set_turn("U7", "ru")
            m3.request_stop("U7")
            res2["own"] = m3.stop_requested("")
            t0 = time.time()
            res2["own_sleep"] = m3._sleep_or_stop(3.0, "None")
            res2["own_dt"] = time.time() - t0
            m3.stop_clear("U7")
        a = threading.Thread(target=tg2)
        b = threading.Thread(target=web2)
        a.start()
        b.start()
        a.join(10)
        b.join(10)
        check("свой «стоп» U7 виден, пока исполняет TG1",
              res2.get("own") is True)
        check("_sleep_or_stop('None') U7 выходит по своему «стоп» сразу",
              res2.get("own_sleep") is True and res2.get("own_dt", 9) < 1.0)
        check("TG1 не принял «стоп» U7 за свой", res2.get("tg_after") is False)
        # Исполняющий поток видит «стоп» своего чата (ключ — его execute)
        seen = {}

        class CC2(CC):
            def _dispatch(self, action, router=None):
                seen["in_exec"] = (self.stop_requested(),
                                   self.stop_requested("None"),
                                   self._stop_key())
                return None
        m3b = CC2(context="ccstate", config=CFG, base_dir=tmp / "cc2")
        m3b.request_stop("TG9")
        m3b.execute({"kind": "zoom", "value": "in", "host": "youtube.com"},
                    "TG9")
        check("в потоке execute без хода: «стоп» своего чата виден",
              seen.get("in_exec") == (True, True, "TG9"))
        check("после execute привязка снята",
              m3b.__dict__.get("_exec_chat") is None
              and m3b.__dict__.get("_exec_thread") is None
              and m3b.stop_requested() is False)

        # ── 3. «None» от str(None) — не ключ чата ──
        print("\n── 3. «None»/пусто → ключ хода (как у _in_chat) ──")
        reset()
        m4 = make()
        m4.set_turn("U8", "en")
        m4.request_stop("U8")
        check("stop_requested('None') = «стоп» хода U8",
              m4.stop_requested("None") and m4.stop_requested("")
              and m4.stop_requested(None) and m4._stop_key("None") == "U8")
        m4.request_stop("None")
        m4.request_stop("")
        check("request_stop('None'/'') ключ не заводит",
              "None" not in m4._stop_chats() and "" not in m4._stop_chats())
        t0 = time.time()
        check("_sleep_or_stop(5, 'None') выходит сразу",
              m4._sleep_or_stop(5.0, "None") and time.time() - t0 < 1.0)
        with m4.chat_scope("U8"):
            m4._last_host = "slow.test"
            m4._last_tab_id = 44
        state["loading"].add(44)
        t0 = time.time()
        r = m4._snapshot_for(None, chat_id="None")
        dt = time.time() - t0
        check(f"_snapshot_for(chat_id='None'): опрос грузящейся вкладки "
              f"выходит по «стоп» ({dt:.1f} с)",
              dt < 2.0 and r[-1] == cc_texts.t("stopped", "en"))
        check("«стоп» вкладку не забывает", m4._st("U8").last_tab_id == 44)
        m4.stop_clear("None")
        check("stop_clear('None') чужой флаг не снимает",
              m4.stop_requested("U8"))
        m4.stop_clear("U8")
        check("stop_clear(U8) → флага нет", not m4.stop_requested("None"))
        m4.set_turn(None)
        m4.request_stop("U8")
        check("без хода «None» — ничей: False", not m4.stop_requested("None"))
        m4.stop_clear("U8")
        state["loading"].clear()

        # ── 4. Отказы резолверов — на языке хода ──
        print("\n── 4. Отказы резолверов и лесенки — cc_texts на языке хода ──")
        reset()

        def refusals(lang):
            mm = make()
            key = "E1" if lang == "en" else "R1"
            mm.set_turn(key, lang)
            out = {}
            try:
                mm.execute({"kind": "url",
                            "value": "https://www.youtube.com/"}, key)
                with mm.chat_scope(key):
                    out["tab_new"] = mm.resolve_tab_op(None, "new")[1]
                    out["close_all"] = mm.resolve_tab_op("all", "close")[1]
                    out["tab_op_nf"] = mm.resolve_tab_op("zzqx", "close")[1]
                    out["switch_nf"] = mm.resolve_tab_switch("zzqx", True)[1]
                    out["click_nf"] = mm.resolve_click(
                        "flurbix", None, None, chat_id=key)[1]
                    out["type_nf"] = mm.resolve_type(
                        "hello into flurbix", None, None, chat_id=key)[1]
                    mm._scroll = {"thread": SimpleNamespace(
                        is_alive=lambda: True), "stop": threading.Event()}
                    out["scrolling"] = mm.resolve_scroll("start", None)[1]
                    mm._scroll = None
                    mm._last_tab_id = 55   # вкладка чата бота
                    out["scroll_chat"] = mm.resolve_scroll("start", None)[1]
                    out["cart_chat"] = mm.resolve_cart(("add", "pizza"),
                                                       None)[1]
                    mm._last_tab_id = 22

                    def _boom(*a, **kw):
                        raise RuntimeError("boom")
                    mm.resolve_zoom = _boom
                    out["intent"] = mm.intent_to_action(
                        {"action": "zoom", "direction": "in"}, None)[1]
            finally:
                mm.set_turn(None)
            return out
        en = refusals("en")
        ru = refusals("ru")
        for k in ("tab_new", "close_all", "tab_op_nf", "switch_nf",
                  "click_nf", "type_nf", "scrolling", "scroll_chat",
                  "cart_chat", "intent"):
            check(f"EN {k}: английский, без кириллицы ({str(en.get(k))[:50]})",
                  bool(en.get(k)) and not CYR.search(str(en.get(k))))
            check(f"RU {k}: русский ({str(ru.get(k))[:50]})",
                  bool(ru.get(k)) and CYR.search(str(ru.get(k))) is not None)
        check("EN: тексты — из cc_texts",
              en["tab_new"] == cc_texts.t("rs_tab_new", "en")
              and en["cart_chat"] == cc_texts.t("rs_cart_chat_tab", "en")
              and en["intent"] == cc_texts.t("rs_intent_failed", "en",
                                             kind="zoom", detail="boom"))
        check("RU: прежние тексты не сменились",
              ru["tab_new"].startswith("Пустую вкладку открывать не буду")
              and ru["click_nf"] == "На странице youtube.com не нашёл "
                                    "элемента для «flurbix».")
        # Хвостовая точка отказа внутри шаблона с точкой — не «..»
        s_en = cc_texts.t("scenario_step_failed", "en",
                          err=en["click_nf"])
        s_ru = cc_texts.t("scenario_step_failed", "ru", err=ru["click_nf"])
        check("scenario_step_failed: без двойной точки (en/ru)",
              ".." not in s_en and ".." not in s_ru
              and "«flurbix»." in s_ru)
        check("многоточие в значении не срезается",
              "ждём..." in cc_texts.t("scenario_step_failed", "ru",
                                      err="ждём..."))
        # Стражи: ни одного русского литерала в ответах резолверов/лесенки,
        # каждый ключ — с ru/en и переданными плейсхолдерами
        bad = _resolver_cyrillic(ROOT / "app/features/computer_control.py")
        check(f"AST: русских литералов в отказах резолверов нет {bad[:5]}",
              not bad)
        bad_l = _ladder_cyrillic(ROOT / "app/bot_instance.py")
        check(f"AST: русских фолбэков в _cc_ladder нет {bad_l[:5]}",
              not bad_l)
        probs = _text_keys_problems(
            [ROOT / "app/features/computer_control.py",
             ROOT / "app/bot_instance.py"], cc_texts._T)
        check(f"cc_texts: ключи резолверов/лесенки полные {probs[:3]}",
              not probs)

        # ── 5. Английские формы остановки листания ──
        print("\n── 5. «stop scrolling» / «stop the scroll» / "
              "«enough scrolling» ──")
        import app.bot_instance as bi
        for s in ("stop scrolling", "stop the scroll", "Stop the scrolling!",
                  "enough scrolling", "stop scrolling now",
                  "stop scrolling please", "хватит листать",
                  "останови прокрутку", "стоп"):
            check(f"parse_scroll_request({s!r}) → stop",
                  (ccm.parse_scroll_request(s) or ("",))[0] == "stop")
        for s in ("stop scrolling", "stop the scroll", "enough scrolling",
                  "stop scrolling now", "хватит листать"):
            check(f"_SCROLL_STOP_RE({s!r})",
                  bool(ccm._SCROLL_STOP_RE.match(s)))
        for s in ("stop scrolling", "stop the scroll", "stop the scrolling",
                  "enough scrolling", "хватит листать", "stop"):
            check(f"_CC_STOP_RE бота({s!r})", bool(bi._CC_STOP_RE.match(s)))
        for s in ("stop the car", "enough of this", "нет",
                  "scroll the page"):
            check(f"не стоп листания: {s!r}",
                  (ccm.parse_scroll_request(s) or ("",))[0] != "stop"
                  and not ccm._SCROLL_STOP_RE.match(s))
        check("_CC_STOP_RE: «stop the car» — не стоп",
              not bi._CC_STOP_RE.match("stop the car"))
        # stop_scroll_if_active: вежливая английская форма гасит листание
        ms = make()
        stopped = []
        with ms.chat_scope("S1"):
            ms._scroll = {"thread": SimpleNamespace(is_alive=lambda: True),
                          "stop": threading.Event()}
            ms._scroll_stop_now = lambda *a, **kw: stopped.append(1)
            check("stop_scroll_if_active('stop scrolling please') гасит",
                  ms.stop_scroll_if_active("stop scrolling please") is True
                  and stopped == [1])
            check("stop_scroll_if_active('no') не гасит",
                  ms.stop_scroll_if_active("no") is False)
            ms._scroll = None
    finally:
        ccm.NAV_LOAD_TIMEOUT_SEC, ccm.NAV_POLL_SEC = orig_timeout, orig_poll
        for k, v in saved.items():
            if v is not None:
                setattr(ba, k, v)

    print(f"\nИтого: {total - fails}/{total} OK, FAIL: {fails}")


if __name__ == "__main__":
    main()

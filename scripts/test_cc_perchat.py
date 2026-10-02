"""Состояние браузера режима управления — по чатам (ChatBrowserState).

Проверяет: два чата с разными отслеживаемыми вкладками резолвят команду без
сайта каждый в свою (явный chat_id, ключ хода set_turn, chat_scope, «None» от
str(None)); новый чат без контекста не берёт чужую вкладку; авто-листание —
одно на чат: «стоп» в чате B не гасит листание A (stop_scroll_if_active,
resolve_scroll, cc_turn_enter бота), самозавершение пишется в свой чат;
last_tab.json по чатам (URL без токенов, служебный хост не пишется, обрезка),
старый формат — умолчание для всех чатов; база видимой вкладки ставится при
открытии/переключении вкладки ботом (всем чатам), переживает перезапуск и
замечает ручное переключение до первой команды; process_message держит чат
хода на весь ход. Браузер подменён целиком.

Запуск: python -m scripts.test_cc_perchat
"""

import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cc_perchat_smoke_")
    tmp = Path(tempfile.mkdtemp(prefix="cc_perchat_data_"))
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
    from app.features.computer_control import (ComputerControlManager,
                                               ChatBrowserState, _CC_CHAT)

    # ── Браузер подменён целиком: ни одного вызова в живой Chrome ──
    ITEM = {"idx": 0, "tag": "button", "role": "", "text": "Войти",
            "aria": "", "title": "", "href": "", "w": 40.0, "h": 20.0,
            "vp": True}
    state = {"visible": None, "snaps": [], "stops": [], "status": {},
             "tabs": {"https://dodopizza.ru/": 11,
                      "https://www.youtube.com/": 22}}

    # Страница вкладки по её id (отслеживаемая вкладка следует за живым URL —
    # _refresh_tracked_page); без id — страница по хосту
    pages = {11: ("https://dodopizza.ru/", "dodopizza.ru"),
             22: ("https://www.youtube.com/", "youtube.com"),
             7: ("https://mail.example.com/in", "mail.example.com")}

    def _snap(host=None, tab_id=None):
        state["snaps"].append((host, tab_id))
        url, h = pages.get(tab_id, ("https://p.test/", "p.test"))
        return url, h, [dict(ITEM)]

    saved = {}
    mocks = {
        "open_new_tab": lambda url, **kw: state["tabs"].get(url, 33),
        "snapshot_elements": _snap,
        "snapshot_for_goal": lambda host, goal, tab_id=None: ("", []),
        "visible_page_info": lambda: state["visible"],
        "dismiss_overlay": lambda host=None, tab_id=None: None,
        "reveal_player_controls": lambda *a, **kw: None,
        "detect_antibot": lambda host=None, tab_id=None, strict=False: None,
        "wait_dom_idle": lambda *a, **kw: None,
        "find_tab_id": lambda host: None,
        "list_pages": lambda: [],
        "page_identity": lambda tab_id=None: "",
        "scroll_start": lambda host, tab_id=None, **kw: {"ok": True},
        "scroll_status": lambda host, tab_id=None: dict(
            state["status"].get(tab_id) or {"active": True}),
        "scroll_stop": lambda host, tab_id=None: state["stops"].append(
            (host, tab_id)),
        "activate_tab": lambda tid: ({7: ("https://mail.example.com/in",
                                          "Почта")}.get(tid, ("", ""))),
    }
    for k, v in mocks.items():
        saved[k] = getattr(ba, k, None)
        setattr(ba, k, v)

    CFG = {"confirm": False, "allow_domains": [], "vision_fallback": False,
           "llm_wide_resolve": False}

    def make(sub=None):
        return ComputerControlManager(
            context="cpc", config=CFG,
            base_dir=tmp / (sub or f"m{time.time_ns()}"))

    def target(m, chat=None, **kw):
        # Куда ушёл бы снапшот команды «нажми войти» без сайта
        state["snaps"].clear()
        if chat is None:
            m.resolve_click("войти", None, None, **kw)
        else:
            m.resolve_click("войти", None, None, chat_id=chat, **kw)
        return state["snaps"][0] if state["snaps"] else None

    orig_poll = ccm._SCROLL_POLL_SEC
    try:
        # ── 1. Две вкладки двух чатов ──
        print("\n── 1. Отслеживаемая вкладка — своя у чата ──")
        m = make()
        m.execute({"kind": "url", "value": "https://dodopizza.ru/"}, "WEB")
        m.execute({"kind": "url", "value": "https://www.youtube.com/"}, "TG")
        check("два чата: WEB без сайта → своя вкладка (додо, #11)",
              target(m, "WEB")[1] == 11)
        check("два чата: TG без сайта → своя вкладка (ютуб, #22)",
              target(m, "TG")[1] == 22)
        check("два чата: состояния раздельны",
              m._st("WEB").last_host == "dodopizza.ru"
              and m._st("TG").last_host == "youtube.com"
              and m._st("WEB").last_tab_id == 11
              and m._st("TG").last_tab_id == 22)
        # Ключ хода (set_turn) без явного chat_id — как у process_message
        m.set_turn("WEB", "ru")
        try:
            check("ключ хода: резолв без chat_id → вкладка чата хода",
                  target(m)[1] == 11)
            check("ключ хода: атрибут менеджера — состояние чата хода",
                  m._last_tab_id == 11)
            with m.chat_scope("TG"):
                check("chat_scope важнее ключа хода", m._last_tab_id == 22)
            check("chat_scope снят — снова чат хода", m._last_tab_id == 11)
            # str(None) из сценариев/агента у веб-чата — не ключ
            check("chat_id «None» — чат хода, а не отдельный чат",
                  target(m, "None")[1] == 11
                  and "None" not in m._chat_states())
        finally:
            m.set_turn(None)
        check("вне вызова контекст чата не залипает", _CC_CHAT.get() is None)
        # Новый чат без контекста — видимая вкладка, не чужая отслеживаемая
        state["visible"] = None
        check("новый чат: чужая вкладка не цель (снапшот без вкладки)",
              target(m, "NEW") == (None, None))
        state["visible"] = ("https://e.mail.ru/inbox", "e.mail.ru")
        check("новый чат: цель — видимая вкладка",
              target(m, "NEW2")[0] == "https://e.mail.ru/inbox"
              and m._st("NEW2").last_host == "e.mail.ru"
              and m._st("WEB").last_host == "dodopizza.ru")
        state["visible"] = None
        # Исполнение пишет состояние своего чата
        m.execute({"kind": "tab_switch", "tab_id": 7,
                   "host": "mail.example.com"}, "TG")
        check("tab_switch: отслеживаемая вкладка сменилась только у TG",
              m._st("TG").last_tab_id == 7
              and m._st("WEB").last_tab_id == 11)
        # Список вкладок — в кэш своего чата
        orig_lt = getattr(ba, "list_tabs", None)
        ba.list_tabs = lambda: [(7, "https://mail.example.com/in",
                                 "mail.example.com", "Почта")]
        try:
            with m.chat_scope("TG"):
                txt = m.list_open_tabs_text()
            check("вкладки: «← текущая» — по вкладке этого чата",
                  "← текущая" in txt and len(m._st("TG").known_tabs) == 1
                  and m._st("WEB").known_tabs == [])
            with m.chat_scope("WEB"):
                txt_w = m.list_open_tabs_text()
            check("вкладки: у другого чата пометки нет",
                  "← текущая" not in txt_w)
        finally:
            if orig_lt is not None:
                ba.list_tabs = orig_lt
        # Закрытая ботом вкладка мертва для всех чатов
        mcl = make()
        mcl.execute({"kind": "url", "value": "https://dodopizza.ru/"}, "A")
        mcl.execute({"kind": "url", "value": "https://dodopizza.ru/"}, "B")
        mcl.execute({"kind": "url", "value": "https://www.youtube.com/"}, "C")
        orig_ct = getattr(ba, "close_tab", None)
        ba.close_tab = lambda tid: ("https://dodopizza.ru/", "Додо")
        try:
            mcl.execute({"kind": "tab_op", "op": "close", "tab_id": 11,
                         "host": "dodopizza.ru"}, "A")
        finally:
            if orig_ct is not None:
                ba.close_tab = orig_ct
        check("закрытие вкладки: id снят у всех, кто её отслеживал",
              mcl._st("A").last_tab_id is None
              and mcl._st("B").last_tab_id is None
              and mcl._st("C").last_tab_id == 22)
        check("закрытие вкладки: хост забыт только у закрывшего чата",
              mcl._st("A").last_host is None
              and mcl._st("B").last_host == "dodopizza.ru")

        # ── 2. Авто-листание — одно на чат ──
        print("\n── 2. Листание: «стоп» другого чата не гасит ──")
        ms = make()
        ms.execute({"kind": "scroll", "host": "youtube.com",
                    "value": "https://youtube.com/", "tab_id": 22}, "A")
        check("листание A идёт", ms._st("A").scroll is not None)
        with ms.chat_scope("A"):
            check("листание A: _scroll_active в A", ms._scroll_active())
        with ms.chat_scope("B"):
            check("листание A: в B не видно", not ms._scroll_active())
        check("«стоп» в B: листание A не гасится",
              ms.stop_scroll_if_active("стоп", chat_id="B") is False
              and ms._st("A").scroll is not None and not state["stops"])
        check("resolve_scroll(stop) в B — не команда (нечего гасить)",
              ms.resolve_scroll("stop", None, chat_id="B") == (None, None))
        act_b, err_b = ms.resolve_scroll("start", None, chat_id="B")
        check("B может листать свою страницу, пока листает A",
              act_b is not None and err_b is None)
        act_a, err_a = ms.resolve_scroll("start", None, chat_id="A")
        check("A: второй старт — «я уже листаю»",
              act_a is None and err_a and "уже листаю" in err_a)
        ms.execute({"kind": "scroll", "host": "p.test",
                    "value": "https://p.test/", "tab_id": 33}, "B")
        a_stop, _ = ms.resolve_scroll("stop", None, chat_id="A")
        check("resolve_scroll(stop) в A — scroll_stop",
              a_stop == {"kind": "scroll_stop"})
        ok_s, _ = ms.execute(a_stop, "A")
        check("«стоп» в A гасит только A (страница A, B листает дальше)",
              ok_s and state["stops"] == [("youtube.com", 22)]
              and ms._st("A").scroll is None
              and ms._st("B").scroll is not None)
        check("stop_scroll_if_active(chat_id=B) гасит листание B",
              ms.stop_scroll_if_active("хватит", chat_id="B") is True
              and ms._st("B").scroll is None
              and state["stops"][-1] == ("p.test", 33))
        # Самозавершение — в состояние своего чата (поток дозорного)
        ccm._SCROLL_POLL_SEC = 0.02
        state["stops"].clear()
        ms.execute({"kind": "scroll", "host": "youtube.com",
                    "value": "https://youtube.com/", "tab_id": 22}, "A")
        ms.execute({"kind": "scroll", "host": "p.test",
                    "value": "https://p.test/", "tab_id": 33}, "B")
        state["status"][22] = {"done": True, "active": False}
        deadline = time.time() + 3
        while ms._st("A").scroll is not None and time.time() < deadline:
            time.sleep(0.02)
        check("самозавершение A: сеанс снят и причина — у A",
              ms._st("A").scroll is None
              and (ms._st("A").scroll_ended or (0, ""))[1] == "bottom")
        check("самозавершение A: B листает, причины конца у B нет",
              ms._st("B").scroll is not None
              and ms._st("B").scroll_ended is None)
        with ms.chat_scope("A"):
            check("«стоп» вдогонку в A — честная причина",
                  ms.resolve_scroll("stop", None) == (
                      {"kind": "scroll_stop"}, None))
        state["status"].clear()
        ms.stop_scroll_if_active(chat_id="B")
        ccm._SCROLL_POLL_SEC = orig_poll

        # Бот: ранний «стоп» (cc_turn_enter) — листание ЭТОГО чата
        from app.bot_instance import BotInstance
        b = BotInstance.__new__(BotInstance)
        b.persona_name = "pc"
        b.context = f"pc_{time.time_ns()}"
        b.owner = "A"
        b.web_single_user = False
        b._cc_allowed_users = set()
        b.trigger_words = ["коннор"]
        b.features = {}
        b._control_mode = {"CA", "CB"}
        b.computer_control = make()
        b.task_agent = None
        b.scenario_manager = None
        cc = b.computer_control
        state["stops"].clear()
        cc.execute({"kind": "scroll", "host": "youtube.com",
                    "value": "https://youtube.com/", "tab_id": 22}, "CA")
        _r, tok_b = b.cc_turn_enter("открой ютуб", "A", "CB")
        r_b, _ = b.cc_turn_enter("стоп", "A", "CB")
        check("бот: «стоп» в чате B при его ходе — «Останавливаю…», "
              "листание A идёт",
              r_b == "Останавливаю…" and cc._st("CA").scroll is not None
              and not state["stops"] and cc.stop_requested("CB")
              and not cc.stop_requested("CA"))
        b.cc_turn_exit(tok_b)
        _r, tok_a = b.cc_turn_enter("нажми войти", "A", "CA")
        r_a, _ = b.cc_turn_enter("стоп", "A", "CA")
        check("бот: «стоп» в чате A гасит листание A",
              r_a == "Останавливаю…" and cc._st("CA").scroll is None
              and state["stops"] == [("youtube.com", 22)])
        b.cc_turn_exit(tok_a)
        # process_message: чат хода — на весь ход, после — снят
        seen = []
        b.user_turn = lambda key: contextlib.nullcontext()
        b._process_message_impl = lambda *a, **kw: (
            seen.append((cc._cur_chat(), _CC_CHAT.get())), "ок")[1]
        b.process_message("привет", user_id="U1", chat_id="C9")
        b.process_message("привет", user_id="U7", chat_id=None)
        check("process_message: контекст браузера — чат хода "
              "(веб без chat_id — user_id)",
              seen == [("C9", "C9"), ("U7", "U7")])
        check("process_message: после хода контекст снят",
              _CC_CHAT.get() is None)

        # ── 3. last_tab.json по чатам ──
        print("\n── 3. last_tab.json ──")
        d1 = tmp / "persist"
        mp = make("persist")
        mp.execute({"kind": "url", "value": "https://dodopizza.ru/"}, "WEB")
        mp.execute({"kind": "url",
                    "value": "https://example.com/cb?access_token=SECRET1"
                             "&page=2#frag"}, "TG")
        data = json.loads((d1 / "last_tab.json").read_text(encoding="utf-8"))
        chats = data.get("chats") or {}
        check("файл: запись по чатам",
              set(chats) == {"WEB", "TG"}
              and chats["WEB"]["host"] == "dodopizza.ru"
              and chats["TG"]["host"].startswith("example.com"))
        raw = (d1 / "last_tab.json").read_text(encoding="utf-8")
        check("файл: URL без токена и фрагмента (scrub_url)",
              "SECRET1" not in raw and "#frag" not in raw)
        check("файл: база видимой вкладки на диске",
              chats["TG"]["vis"] == "example.com")
        mp2 = make("persist")
        check("рестарт: каждый чат помнит свой сайт",
              mp2._st("WEB").last_host == "dodopizza.ru"
              and mp2._st("TG").last_host.startswith("example.com")
              and mp2._st("WEB").last_tab_id is None)
        check("рестарт: новый чат не наследует чужой сайт (новый формат)",
              mp2._st("OTHER").last_host is None)
        check("рестарт: вызов вне хода — свежий чат (совместимость)",
              mp2._last_host.startswith("example.com"))
        # Служебный хост не пишется и не восстанавливается
        with mp2.chat_scope("SVC"):
            mp2._last_host = "chat.deepseek.com"
            mp2._save_last_page("https://chat.deepseek.com/x")
        check("файл: служебный хост чата не записан",
              "SVC" not in json.loads((d1 / "last_tab.json").read_text(
                  encoding="utf-8"))["chats"])
        # Старый формат — умолчание для всех чатов
        d_old = tmp / "old"
        d_old.mkdir()
        (d_old / "last_tab.json").write_text(json.dumps(
            {"host": "dodopizza.ru", "url": "https://dodopizza.ru/menu",
             "ts": 1}), encoding="utf-8")
        mo = make("old")
        check("старый формат: умолчание для любого чата",
              mo._st("X").last_host == "dodopizza.ru"
              and mo._st("Y").last_url == "https://dodopizza.ru/menu")
        mo.execute({"kind": "url", "value": "https://www.youtube.com/"}, "X")
        check("старый формат: чат X ушёл на свой сайт, Y — прежнее умолчание",
              mo._st("X").last_host == "youtube.com"
              and mo._st("Y").last_host == "dodopizza.ru"
              and mo._st("Z").last_host == "dodopizza.ru")
        data_o = json.loads((d_old / "last_tab.json").read_text(
            encoding="utf-8"))
        check("старый формат: перезаписан новым (по чатам)",
              "X" in (data_o.get("chats") or {}))
        # Старый файл со служебным хостом — не умолчание
        d_os = tmp / "old_svc"
        d_os.mkdir()
        (d_os / "last_tab.json").write_text(json.dumps(
            {"host": "chat.deepseek.com", "url": "https://chat.deepseek.com/",
             "ts": 1}), encoding="utf-8")
        check("старый формат: служебный хост не восстанавливается",
              make("old_svc")._st("X").last_host is None)
        # Битый файл — пустой контекст, без исключения
        d_bad = tmp / "bad"
        d_bad.mkdir()
        (d_bad / "last_tab.json").write_text("{не json", encoding="utf-8")
        check("битый last_tab.json — пустой контекст",
              make("bad")._st("X").last_host is None)
        # Обрезка по числу чатов
        mc = make("cap")
        for i in range(ccm.LAST_TAB_MAX_CHATS + 5):
            with mc.chat_scope(f"c{i}"):
                mc._last_host = "dodopizza.ru"
                mc._save_last_page("https://dodopizza.ru/")
        data_c = json.loads((tmp / "cap" / "last_tab.json").read_text(
            encoding="utf-8"))
        check("файл: не больше LAST_TAB_MAX_CHATS свежих чатов",
              len(data_c["chats"]) == ccm.LAST_TAB_MAX_CHATS
              and f"c{ccm.LAST_TAB_MAX_CHATS + 4}" in data_c["chats"]
              and "c0" not in data_c["chats"])

        # ── 4. База видимой вкладки ──
        print("\n── 4. База видимой вкладки ──")
        mv = make("vis")
        state["visible"] = None
        mv.execute({"kind": "url", "value": "https://dodopizza.ru/"}, "A")
        check("открытие: база ставится сразу (без резолва)",
              mv._st("A").vis_baseline == "dodopizza.ru")
        # Ручное переключение до первой команды — замечено
        state["visible"] = ("https://e.mail.ru/inbox", "e.mail.ru")
        check("ручное переключение до первой команды → цель видимая",
              target(mv, "A")[0] == "e.mail.ru"
              and mv._st("A").last_tab_id is None)
        # Бот сам открыл вкладку другому чату — это не ручное переключение
        mv2 = make("vis2")
        mv2.execute({"kind": "url", "value": "https://dodopizza.ru/"}, "A")
        mv2.execute({"kind": "url", "value": "https://www.youtube.com/"}, "B")
        check("открытие в B: база обновлена всем чатам",
              mv2._st("A").vis_baseline == "www.youtube.com"
              and mv2._st("B").vis_baseline == "www.youtube.com")
        state["visible"] = ("https://www.youtube.com/", "www.youtube.com")
        check("видна вкладка, открытая ботом для B — команда A идёт в свою",
              target(mv2, "A")[1] == 11 and mv2._st("A").last_tab_id == 11)
        check("…а команда B — в свою", target(mv2, "B")[1] == 22)
        # Переключение вкладки ботом — тоже база
        mv2.execute({"kind": "tab_switch", "tab_id": 7,
                     "host": "mail.example.com"}, "B")
        check("tab_switch: база — активированная вкладка",
              mv2._st("B").vis_baseline == "mail.example.com"
              and mv2._st("A").vis_baseline == "mail.example.com")
        # После перезапуска: база с диска, ручное переключение замечено
        state["visible"] = None
        mr = make("vis3")
        mr.execute({"kind": "url", "value": "https://dodopizza.ru/"}, "A")
        mr2 = make("vis3")
        check("рестарт: база видимой вкладки восстановлена",
              mr2._st("A").vis_baseline == "dodopizza.ru")
        state["visible"] = ("https://e.mail.ru/inbox", "e.mail.ru")
        check("рестарт: ручное переключение до первой команды → видимая",
              target(mr2, "A")[0] == "e.mail.ru")
        # Видимая не менялась после рестарта — цель прежняя (по хосту)
        state["visible"] = ("https://dodopizza.ru/", "dodopizza.ru")
        mr3 = make("vis3")
        mr3._st("A").last_host = "dodopizza.ru"
        check("рестарт: видна та же страница — цель прежняя",
              target(mr3, "A")[0] == "dodopizza.ru")
        state["visible"] = None
        # Служебная вкладка базой не становится
        mz = make("vis4")
        with mz.chat_scope("A"):
            mz._init_vis_baseline("http://localhost:5173/chat")
        check("служебная вкладка — не база",
              mz._st("A").vis_baseline is None)
        check("ChatBrowserState — по умолчанию пуст",
              ChatBrowserState().last_host is None
              and ChatBrowserState().known_tabs == [])
    finally:
        ccm._SCROLL_POLL_SEC = orig_poll
        for k, v in saved.items():
            if v is not None:
                setattr(ba, k, v)

    print(f"\nИтого: {total - fails}/{total} OK, FAIL: {fails}")


if __name__ == "__main__":
    main()

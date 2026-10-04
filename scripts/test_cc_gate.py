"""Инвариант подтверждения режима управления — единая точка в execute().

Проверяет: рискованное действие (подпись оплаты/коммита/удаления с aria/title,
force_confirm, непроверенная подпись, клик по точке vision, ввод в
чувствительное поле, маркер модели, адрес из поиска) без токена «да» не
исполняется, а возвращается отказом с confirm_required; токен — объект
(строкой/словарём не подделать), ставит его только grant_confirmation;
nav-маршрут сверяет КАЖДЫЙ найденный элемент (стоп на «Продолжить и оплатить»,
продолжение на той же вкладке после «да»); LLM-восстановление шага не жмёт
рискованное; сценарий ставит шаг на «да/нет» (оплата — человеку), «стоп»
между шагами; бот: pending «да» даёт токен, отказ гейта → вопрос; агент задач
после своего «да»; вопрос по URL с параметрами; маркер с query без команды.

Запуск: python -m scripts.test_cc_gate
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cc_gate_smoke_")
    tmp = Path(tempfile.mkdtemp(prefix="cc_gate_data_"))
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

    import json
    import app.features.computer_control as ccm
    from app.features.computer_control import (
        ComputerControlManager, NeedsConfirm)
    from app.features import browser_actions as ba
    from app.features import cc_texts

    class Spy(ComputerControlManager):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls = []

        def _dispatch(self, action, router=None):
            self.calls.append(dict(action))

    n = {"i": 0}

    def make(cfg=None, cls=Spy):
        n["i"] += 1
        return cls(context="gate", config=cfg or {"confirm": True,
                                                   "click": True},
                   base_dir=tmp / f"m{n['i']}")

    def last_audit(m):
        p = m.base_dir / "audit.jsonl"
        lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
        return json.loads(lines[-1]) if lines else {}

    CR = ComputerControlManager.confirm_reason

    # ── 1. Причины гейта ──
    print("\n1. confirm_reason")
    check("клик «Оформить заказ» → commit",
          CR({"kind": "click", "element": "Оформить заказ"}) == "commit")
    check("иконка с aria «Оплатить» → payment",
          CR({"kind": "click", "element": "#4", "aria": "Оплатить"})
          == "payment")
    check("title «Удалить аккаунт» → destructive",
          CR({"kind": "click", "element": "#4", "title": "Удалить аккаунт"})
          == "destructive")
    check("force_confirm → force_confirm",
          CR({"kind": "click", "element": "Меню", "force_confirm": True})
          == "force_confirm")
    check("choose.label_unverified → label_unverified",
          CR({"kind": "click", "element": "Меню",
              "choose": {"label_unverified": True}}) == "label_unverified")
    check("клик по точке vision → point",
          CR({"kind": "click", "point": {"x": 1, "y": 2}}) == "point")
    check("hover по точке — не гейт (ничего не активирует)",
          CR({"kind": "hover", "point": {"x": 1, "y": 2}}) is None)
    check("ввод в чувствительное поле → sensitive_field",
          CR({"kind": "type", "element": "Пароль", "text": "x",
              "field_sensitive": True}) == "sensitive_field")
    check("маркер модели → marker",
          CR({"kind": "url", "value": "https://x.ru", "origin": "marker"})
          == "marker")
    check("pending_from=marker (после «да») → marker",
          CR({"kind": "url", "value": "https://x.ru", "origin": "pending",
              "pending_from": "marker"}) == "marker")
    check("адрес из поиска → via_search",
          CR({"kind": "url", "value": "https://x.ru", "via_search": True})
          == "via_search")
    check("обычный клик «Меню» → None",
          CR({"kind": "click", "element": "Меню"}) is None)
    check("nav — None (шаги сверяет _nav_gate)",
          CR({"kind": "nav", "steps": ["Оформить заказ"]}) is None)
    check("multi с рискованным пунктом → причина пункта",
          CR({"kind": "multi", "items": [
              {"kind": "url", "value": "https://x.ru"},
              {"kind": "click", "element": "Удалить аккаунт"}]})
          == "destructive")

    # ── 2. execute без токена — отказ, с токеном — исполнение ──
    print("\n2. execute: гейт")
    m = make()
    act = {"kind": "click", "idx": 3, "element": "Оформить заказ",
           "host": "dodopizza.ru", "origin": "fast"}
    ok, det = m.execute(act, "c1")
    check("без токена: не исполнено, отказ",
          ok is False and not m.calls
          and act.get("confirm_required", {}).get("reason") == "commit")
    check("отказ — понятный текст (без «Не удалось …»)",
          "подтверждения" in det)
    rec = last_audit(m)
    check("аудит: error_class=needs_confirm, gate=commit",
          rec.get("error_class") == "needs_confirm"
          and rec.get("gate") == "commit")
    for forged in ("pending", True, {"via": "pending"}, 1):
        a2 = dict(act, confirmed=forged)
        a2.pop("confirm_required", None)
        ok2, _ = m.execute(a2, "c1")
        check(f"поддельный токен {forged!r} — отказ", ok2 is False
              and not m.calls)
    got = m.gate_followup(act)
    check("gate_followup: pending-копия без служебных полей + вопрос",
          got is not None and "confirm_required" not in got[0]
          and got[1] == m.confirm_question(got[0]))
    ComputerControlManager.grant_confirmation(act, "pending", by="A")
    ok3, _ = m.execute(act, "c1")
    check("с токеном «да» — исполнено", ok3 is True and len(m.calls) == 1
          and "confirm_required" not in act)
    check("аудит: confirmed=pending", last_audit(m).get("confirmed")
          == "pending")
    check("gate_followup после успеха — None", m.gate_followup(act) is None)
    mm = make()
    multi = {"kind": "multi", "items": [
        {"kind": "url", "value": "https://youtube.com"},
        {"kind": "click", "idx": 1, "element": "Удалить аккаунт",
         "host": "youtube.com"}]}
    okm, _ = mm.execute(multi, "c2")
    check("multi с рискованным пунктом без токена — ничего не исполнено",
          okm is False and not mm.calls
          and multi["confirm_required"]["label"] == "Удалить аккаунт")
    ComputerControlManager.grant_confirmation(multi, "pending")
    okm2, _ = mm.execute(multi, "c2")
    check("multi с токеном — исполнен целиком", okm2 and len(mm.calls) == 1)
    mp = make()
    okp, _ = mp.execute({"kind": "click", "point": {"x": 5, "y": 5},
                         "host": "x.ru"}, "c3")
    check("клик по точке без «да» — отказ", okp is False and not mp.calls)
    okh, _ = mp.execute({"kind": "hover", "point": {"x": 5, "y": 5},
                         "host": "x.ru"}, "c3")
    check("hover по точке — исполняется", okh and len(mp.calls) == 1)
    okn, _ = mp.execute({"kind": "click", "idx": 1, "element": "Меню",
                         "host": "x.ru"}, "c3")
    check("обычный клик — исполняется без токена", okn and len(mp.calls) == 2)

    # ── 3. nav: гейт по фактически найденному элементу ──
    print("\n3. nav-маршрут")
    saved = {k: getattr(ba, k) for k in (
        "open_new_tab", "wait_dom_idle", "snapshot_elements",
        "dismiss_overlay", "page_urls", "follow_popup", "click_tagged",
        "snapshot_for_goal", "find_tab_id", "visible_page_info")}
    st = {"p": 0, "clicked": [], "opened": [], "pages": None}

    def _snap(host=None, tab_id=None):
        pages = st["pages"]
        return ("https://shop.ru/cart", "shop.ru",
                pages[min(st["p"], len(pages) - 1)])

    def _click(host, idx, tab_id=None):
        st["clicked"].append((idx, tab_id))
        st["p"] += 1

    ba.open_new_tab = lambda url, focus=True: (st["opened"].append(url), 7)[1]
    ba.wait_dom_idle = lambda *a, **k: None
    ba.snapshot_elements = _snap
    ba.dismiss_overlay = lambda *a, **k: None
    ba.page_urls = lambda: []
    ba.follow_popup = lambda pre, **k: None
    ba.click_tagged = _click
    ba.snapshot_for_goal = lambda host, goal, tab_id=None: ("", [])
    ba.find_tab_id = lambda *a, **k: None
    ba.visible_page_info = lambda: None

    def reset(pages):
        st.update(p=0, clicked=[], opened=[], pages=pages)

    PAY = [[{"idx": 1, "tag": "a", "text": "Корзина"},
            {"idx": 2, "tag": "a", "text": "Меню"}],
           [{"idx": 5, "tag": "button", "text": "Продолжить и оплатить 1 290 ₽"},
            {"idx": 6, "tag": "a", "text": "Назад"}],
           [{"idx": 8, "tag": "a", "text": "Спасибо"}]]
    try:
        cc = make(cls=ComputerControlManager)
        reset(PAY)
        nav = cc.resolve_nav("shop.ru", ["Корзина", "Продолжить"])
        check("needs_confirm(nav) — по домену (шаги не рискованные)",
              cc.needs_confirm(nav))
        ComputerControlManager.grant_confirmation(nav, "pending", by="A")
        okv, detv = cc.execute(nav, "n1")
        info = nav.get("confirm_required") or {}
        check("маршрут остановлен на «Продолжить и оплатить» (оплата)",
              okv is False and info.get("reason") == "payment"
              and info.get("label") == "Продолжить и оплатить 1 290 ₽")
        check("нажато только «Корзина», рискованное — нет",
              st["clicked"] == [(1, 7)])
        check("пройденное и остаток — в отказе",
              info.get("done") == ["Корзина"]
              and info.get("rest") == ["Продолжить"]
              and info.get("tab_id") == 7)
        got = cc.gate_followup(nav)
        cont, q = got if got else ({}, "")
        check("продолжение: nav на той же вкладке с подписью из вопроса",
              cont.get("kind") == "nav" and cont.get("resume_tab") == 7
              and cont.get("steps") == ["Продолжить"]
              and cont.get("gate_label") == "Продолжить и оплатить 1 290 ₽")
        check("вопрос: что пройдено, реальная подпись и риск",
              "Прошёл: Корзина" in q and "Продолжить и оплатить 1 290 ₽" in q
              and "оплата" in q)
        check("вопрос по-английски",
              "Click it?" in cc.gate_followup(nav, lang="en")[1])
        # «нет»/без токена продолжение не исполняется
        okc0, _ = cc.execute(dict(cont), "n1")
        check("продолжение без токена — снова стоп, клика нет",
              okc0 is False and st["clicked"] == [(1, 7)])
        ComputerControlManager.grant_confirmation(cont, "pending", by="A")
        okc, _ = cc.execute(cont, "n1")
        check("«да» на продолжение — клик на той же вкладке, новой нет",
              okc is True and st["clicked"] == [(1, 7), (5, 7)]
              and st["opened"] == ["https://shop.ru"])
        check("отчёт продолжения",
              cc.describe_done(cont) == "прошёл до «Продолжить» на shop.ru"
              and "went through" in cc.describe_done(cont, lang="en"))
        # Подпись на странице сменилась после вопроса — новый стоп
        reset([[{"idx": 9, "tag": "button",
                 "text": "Продолжить и оплатить 5 000 ₽"}]])
        cont2 = dict(cont)
        cont2.pop("confirmed")
        ComputerControlManager.grant_confirmation(cont2, "pending")
        okc2, _ = cc.execute(cont2, "n1")
        check("продолжение: другая подпись, чем в вопросе, — стоп",
              okc2 is False and not st["clicked"]
              and (cont2.get("confirm_required") or {}).get("label")
              == "Продолжить и оплатить 5 000 ₽")
        # Без токена вообще (confirm:false, известный домен)
        lax = make({"confirm": False, "click": True,
                    "allow_domains": ["shop.ru"]}, cls=ComputerControlManager)
        reset(PAY)
        nav2 = lax.resolve_nav("shop.ru", ["Корзина", "Продолжить"])
        check("confirm:false, известный домен, мирные шаги — без вопроса",
              not lax.needs_confirm(nav2))
        ok4, _ = lax.execute(nav2, "n2")
        check("исполнение без «да» — стоп перед оплатой",
              ok4 is False and st["clicked"] == [(1, 7)])
        # Шаг назван рискованным — вопрос до открытия; «да» его покрывает
        COMMIT = [[{"idx": 1, "tag": "a", "text": "Корзина"}],
                  [{"idx": 4, "tag": "button", "text": "Оформить заказ"}],
                  [{"idx": 8, "tag": "a", "text": "Спасибо"}]]
        reset(COMMIT)
        nav3 = lax.resolve_nav("shop.ru", ["Корзина", "Оформить заказ"])
        check("шаг «Оформить заказ» — вопрос даже при confirm:false",
              lax.needs_confirm(nav3))
        ok5, _ = lax.execute(nav3, "n3")
        check("без «да» — стоп перед «Оформить заказ»",
              ok5 is False and st["clicked"] == [(1, 7)]
              and nav3["confirm_required"]["reason"] == "commit")
        reset(COMMIT)
        nav3b = lax.resolve_nav("shop.ru", ["Корзина", "Оформить заказ"])
        ComputerControlManager.grant_confirmation(nav3b, "pending")
        ok6, _ = lax.execute(nav3b, "n3")
        check("«да» на маршрут с «Оформить заказ» — нажато (класс совпал)",
              ok6 is True and st["clicked"] == [(1, 7), (4, 7)])
        # Шаг «Оформить заказ», а нашлась оплата — другой класс, стоп
        reset([[{"idx": 1, "tag": "a", "text": "Корзина"}],
               [{"idx": 4, "tag": "button",
                 "text": "Оформить заказ и оплатить 990 ₽"}]])
        nav3c = lax.resolve_nav("shop.ru", ["Корзина", "Оформить заказ"])
        ComputerControlManager.grant_confirmation(nav3c, "pending")
        ok7, _ = lax.execute(nav3c, "n3")
        check("«да» на «Оформить заказ» не покрывает оплату — стоп",
              ok7 is False and st["clicked"] == [(1, 7)]
              and nav3c["confirm_required"]["reason"] == "payment")
        # Сомнительный выбор шага (force_confirm из резолва) — стоп
        reset(PAY)
        _orig_choose = lax._choose_element
        lax._choose_element = lambda goal, items, router, host=None: (
            (1, {"path": "llm", "force_confirm": True,
                 "label_unverified": True}))
        nav4 = lax.resolve_nav("shop.ru", ["Корзина"])
        ok8, _ = lax.execute(nav4, "n4")
        check("шаг с непроверенной подписью — стоп до «да»",
              ok8 is False and not st["clicked"]
              and nav4["confirm_required"]["reason"] == "label_unverified")
        lax._choose_element = _orig_choose
        # Эскалация шага (целевой снапшот): подпись из picked_item
        reset([[{"idx": 1, "tag": "a", "text": "Меню"}]])
        ba.snapshot_for_goal = lambda host, goal, tab_id=None: (
            "https://shop.ru/", [{"idx": 44, "tag": "button",
                                  "text": "Подтвердить заказ"}])
        nav5 = lax.resolve_nav("shop.ru", ["подтвердить"])
        try:
            lax._navigate(nav5)
            err5 = None
        except NeedsConfirm as e:
            err5 = e
        check("шаг из целевого снапшота с рискованной подписью — стоп",
              err5 is not None and not st["clicked"]
              and err5.info.get("label") == "Подтвердить заказ")
        ba.snapshot_for_goal = lambda host, goal, tab_id=None: ("", [])
        # LLM-восстановление шага не жмёт рискованное
        rec_m = make({"confirm": True, "click": True})

        class _R:
            def get_response(self, messages, **kw):
                return "1"
        for step, lab in (("Продолжить", "Продолжить и оплатить 1 290 ₽"),
                          ("Подтвердить", "Подтвердить заказ"),
                          ("Отправить", "Отправить заявку")):
            reset([[{"idx": 2, "tag": "button", "text": lab}]])
            r = rec_m._nav_step_recover(
                step, ["Корзина", step], 1, ["Корзина"], "shop.ru",
                [{"idx": 2, "tag": "button", "text": lab}], None, _R())
            check(f"восстановление не жмёт «{lab}»",
                  r is False and not st["clicked"])
        reset([[{"idx": 2, "tag": "button", "text": "Меню"}]])
        r = rec_m._nav_step_recover(
            "Цель", ["Цель"], 0, [], "shop.ru",
            [{"idx": 2, "tag": "button", "text": "Меню"}], None, _R())
        check("восстановление жмёт мирное «Меню»",
              r is True and st["clicked"] == [(2, None)])

        # ── 4. Бот: fast path и pending «да» ──
        print("\n4. бот: fast path / pending")
        from app.bot_instance import BotInstance
        from app.features.conversation_style import ConversationStyleConfig
        import app.features.flavor_text as _flavor
        ba.set_control_mode = lambda *a, **kw: None
        _flavor.cc_reply = lambda *a, **kw: None

        class _Persona:
            def __init__(self):
                self.settings, self.persona_data = {}, {"name": "Connor"}

            def prepare_messages(self, *a, **kw):
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
                self.log = []

            def add_message(self, role, content, *a, **kw):
                self.log.append((role, content))

            def get_context(self, *a, **kw):
                return [], [], []

            def get_chat_facts_block(self, *a, **kw):
                return None

        class _Router:
            def __init__(self, reply="Обычный ответ."):
                self.reply = reply
                self.answer_provider, self._last_provider = None, "fake"

            def is_local_primary(self):
                return False

            def supports_vision(self):
                return False

            def get_response(self, messages, **kw):
                return self.reply

        CHAT = "g1"

        def bot(cfg, cls=ComputerControlManager, sm=None):
            b = BotInstance.__new__(BotInstance)
            b.persona_name = "gate"
            b.context = f"gate_{time.time_ns()}"
            b.owner = "A"
            b.web_single_user = False
            b._cc_allowed_users = set()
            b.features = {}
            b.trigger_words = ["коннор"]
            b.intellect = SimpleNamespace(active=False)
            b.conversation_style = ConversationStyleConfig(None)
            b._control_mode = {CHAT}
            b.computer_control = cls(context=b.context, config=cfg,
                                     base_dir=tmp / b.context)
            b.scenario_manager = sm
            b.task_agent = None
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
            b.router = _Router()
            return b

        reset(PAY)
        b = bot({"confirm": True, "click": True})
        r1 = b.process_message("открой shop.ru - Корзина - Продолжить",
                               user_id="A", chat_id=CHAT)
        check("маршрут: вопрос до открытия", "shop.ru" in (r1 or "")
              and "?" in (r1 or "") and not st["clicked"])
        r2 = b.process_message("да", user_id="A", chat_id=CHAT)
        pend = b.computer_control.get_pending(CHAT, user_id="A") or {}
        check("«да» → маршрут встал перед оплатой, новый вопрос с подписью",
              st["clicked"] == [(1, 7)]
              and "Продолжить и оплатить 1 290 ₽" in (r2 or "")
              and pend.get("gate_label") == "Продолжить и оплатить 1 290 ₽")
        r3b = b.process_message("да", user_id="B", chat_id=CHAT)
        check("чужое «да» в группе продолжение не исполняет",
              st["clicked"] == [(1, 7)] and "Продолжить и оплатить" not in
              str([c for c in st["clicked"]]) and r3b is not None)
        # (чужая реплика снимает свой, а не чужой pending — продолжаем)
        r3 = b.process_message("да", user_id="A", chat_id=CHAT)
        check("«да» владельца → нажато на той же вкладке, отчёт",
              st["clicked"] == [(1, 7), (5, 7)] and "Продолжить" in (r3 or "")
              and len(st["opened"]) == 1)
        reset(PAY)
        b2 = bot({"confirm": False, "click": True,
                  "allow_domains": ["shop.ru"]})
        r4 = b2.process_message("открой shop.ru - Корзина - Продолжить",
                                user_id="A", chat_id=CHAT)
        check("confirm:false: маршрут исполняется, но встаёт перед оплатой "
              "с вопросом", st["clicked"] == [(1, 7)]
              and "Нажать?" in (r4 or "")
              and b2.computer_control.get_pending(CHAT, user_id="A"))
        r5 = b2.process_message("нет", user_id="A", chat_id=CHAT)
        check("«нет» — ничего не нажато, pending снят",
              st["clicked"] == [(1, 7)]
              and not b2.computer_control.get_pending(CHAT, user_id="A"))
        # pending «да» на клик «Оформить заказ» — токен, исполнение
        b3 = bot({"confirm": True, "click": True}, cls=Spy)
        b3.computer_control.set_pending(CHAT, {
            "kind": "click", "idx": 4, "element": "Оформить заказ",
            "host": "shop.ru", "origin": "fast"}, user_id="A")
        b3.process_message("да", user_id="A", chat_id=CHAT)
        calls = b3.computer_control.calls
        check("pending «да» → рискованный клик исполнен (токен от «да»)",
              len(calls) == 1 and calls[0].get("element") == "Оформить заказ"
              and ComputerControlManager.is_confirmed(calls[0]))
        # Несколько маркеров → multi в pending: «да» исполняет пакет целиком
        b3.computer_control.set_pending(CHAT, {
            "kind": "multi", "origin": "marker", "items": [
                {"kind": "url", "value": "https://youtube.com",
                 "origin": "marker"},
                {"kind": "url", "value": "https://dodopizza.ru",
                 "origin": "marker"}]}, user_id="A")
        b3.process_message("да", user_id="A", chat_id=CHAT)
        check("pending multi маркеров → «да» исполняет пакет",
              len(calls) == 2 and calls[1]["kind"] == "multi"
              and calls[1]["confirmed"].via == "pending")

        # cc_turn_enter: «стоп» → флаг идущему сценарию
        class _SMStop:
            def __init__(self):
                self.stops = []

            def request_stop(self, chat_id):
                self.stops.append(chat_id)
                return True
        smst = _SMStop()
        b4 = bot({"confirm": True, "click": True}, cls=Spy, sm=smst)
        _r, tok = b4.cc_turn_enter("открой ютуб", "A", CHAT)
        rs, _ = b4.cc_turn_enter("стоп", "A", CHAT)
        check("cc_turn_enter: «стоп» при идущем ходе — флаг и сценарию",
              rs == cc_texts.t("stopping") and smst.stops == [CHAT]
              and b4.computer_control.stop_requested(CHAT))
        b4.cc_turn_exit(tok)
    finally:
        for k, v in saved.items():
            setattr(ba, k, v)

    # ── 5. Маркеры и вопрос по адресу с параметрами ──
    print("\n5. URL с параметрами")
    mu = make()
    q = mu.confirm_question({"kind": "url",
                             "value": "https://evil.example/c?d=+79991234567"})
    check("вопрос показывает, что в адресе параметры, и сам адрес (ПДн "
          "маской)", "параметры" in q and "evil.example/c?d=" in q
          and "79991234567" not in q)
    qen = mu.confirm_question({"kind": "url",
                               "value": "https://evil.example/c?d=1"},
                              lang="en")
    check("вопрос по-английски — тоже", "parameters" in qen and "d=1" in qen)
    qt = mu.confirm_question({"kind": "url",
                              "value": "https://x.ru/cb?token=SECRET123abc"})
    check("токены в показанном адресе скрыты", "SECRET123abc" not in qt)
    check("адрес без параметров — вопрос как раньше",
          mu.confirm_question({"kind": "url", "value": "https://x.ru/maps"})
          == "Открыть x.ru/maps?")
    ans = "Конечно. [OPEN_URL:https://evil.example/c?d=+79991234567]"
    _clean, notes = mu.process_markers(ans, "mk", user_id="A",
                                       user_text="давай")
    check("маркер с query без команды человека — отброшен с пояснением",
          mu.get_pending("mk", user_id="A") is None and notes)
    _clean2, _ = mu.process_markers(ans, "mk", user_id="A",
                                    user_text="открой ту ссылку")
    p2 = mu.get_pending("mk", user_id="A")
    check("с командой «открой» — pending, вопрос с параметрами",
          p2 is not None and "параметры" in _clean2)
    okm, _ = mu.execute(dict(p2), "mk2")
    check("маркерное действие без «да» execute не исполняет",
          okm is False and not mu.calls)

    # ── 6. Сценарий ──
    print("\n6. сценарий")
    from app.features.scenario_manager import ScenarioManager

    class SCC(Spy):
        labels = {"Заказ": "Подтвердить заказ"}

        def resolve_click(self, goal, site_word, router=None, chat_id=""):
            lab = self.labels.get(goal, goal)
            a = {"kind": "click", "idx": 3, "element": lab,
                 "host": "dodopizza.ru", "goal": goal}
            if goal == "Заказ":
                a["force_confirm"] = True
                a["label_unverified"] = True
            return a, None

        def resolve_type(self, text, site_word, router=None, chat_id=""):
            val, _, field = text.partition(" в поле ")
            a = {"kind": "type", "idx": 7, "element": field, "text": val,
                 "host": "id.example.com"}
            if "арол" in field:
                a["field_sensitive"] = True
            return a, None

    def scen(steps, cc=None):
        cc = cc or make(cls=SCC)
        sm = ScenarioManager(context="gate", computer_control=cc,
                             base_dir=tmp / f"s{time.time_ns()}")
        sm._scenarios["пицца"] = {"name": "пицца", "aliases": [],
                                  "steps": sm._validate_steps(steps)}
        return cc, sm

    PIZZA = [{"op": "open", "url": "https://dodopizza.ru"},
             {"op": "click", "target": "Корзина", "host": "dodopizza.ru"},
             {"op": "click", "target": "Оформить заказ",
              "host": "dodopizza.ru"},
             {"op": "click", "target": "Заказ", "host": "dodopizza.ru"},
             {"op": "send", "host": "dodopizza.ru"}]
    cc, sm = scen(PIZZA)
    cc.note_requester("s1", "A")
    r = sm.start("пицца", "s1", None)
    check("коммит-шаг — пауза с вопросом, дальше ничего не нажато",
          [c.get("element") for c in cc.calls] == [None, "Корзина"]
          and "Оформить заказ" in r and "(да/нет)" in r and sm.active("s1"))
    cc.note_requester("s1", "B")
    rf = sm.feed("s1", "да", None)
    check("чужое «да» шаг не подтверждает",
          rf == cc_texts.t("scenario_confirm_foreign") and len(cc.calls) == 2)
    cc.note_requester("s1", "A")
    r2 = sm.feed("s1", "да", None)
    check("«да» владельца — шаг исполнен с токеном сценария",
          cc.calls[2].get("element") == "Оформить заказ"
          and cc.calls[2]["confirmed"].via == "scenario")
    check("следующий шаг с непроверенной подписью — снова вопрос",
          len(cc.calls) == 3 and "Подтвердить заказ" in r2
          and "(да/нет)" in r2)
    r3 = sm.feed("s1", "нет", None)
    check("«нет» — сценарий остановлен, клика нет",
          len(cc.calls) == 3 and not sm.active("s1") and "остановлен" in r3)
    # Протухшее «да» — шаг резолвится заново и спрашивает снова
    cc, sm = scen(PIZZA)
    sm.start("пицца", "s2", None)
    sm._runs["s2"]["confirm"]["ts"] = 0
    r4 = sm.feed("s2", "да", None)
    check("протухшее «да» — не исполнено, вопрос заново",
          len(cc.calls) == 2 and "истекло" in r4 and "(да/нет)" in r4)
    # Непонятный ответ дважды — прогон снят
    r5 = sm.feed("s2", "а что это", None)
    r6 = sm.feed("s2", "ну не знаю", None)
    check("антизалипание на вопросе шага",
          r5 == cc_texts.t("scenario_confirm_wait") and r6 is None
          and not sm.active("s2") and len(cc.calls) == 2)
    # Срок «да»: шаг-клик — PENDING_TTL_SEC (5 минут), клавишный шаг
    # («отправь» — Enter в то, что в фокусе) — KEY_CONFIRM_TTL_SEC (минута)
    cc, sm = scen(PIZZA)
    cc.note_requester("s5", "A")
    sm.start("пицца", "s5", None)
    sm._runs["s5"]["confirm"]["ts"] = time.time() - 120
    sm.feed("s5", "да", None)
    check("«да» на клик шага через 2 минуты — исполнено",
          len(cc.calls) == 3
          and cc.calls[2].get("element") == "Оформить заказ")
    cc, sm = scen([{"op": "open", "url": "https://dodopizza.ru"},
                   {"op": "send", "host": "dodopizza.ru"}])
    cc.note_requester("s6", "A")
    sm.start("пицца", "s6", None)
    pend = sm._runs["s6"]["confirm"]
    check("шаг «отправь» ждёт «да»",
          bool(pend) and pend["act"]["kind"] == "send")
    pend["ts"] = time.time() - 120
    r8 = sm.feed("s6", "да", None)
    check("«да» на «отправь» через 2 минуты — истекло, не нажато",
          "истекло" in (r8 or "")
          and not any(c.get("kind") == "send" for c in cc.calls))
    sm._runs["s6"]["confirm"]["ts"] = time.time() - 30
    sm.feed("s6", "да", None)
    check("«да» на «отправь» через 30 с — нажато",
          any(c.get("kind") == "send" for c in cc.calls))
    # Оплата на живой странице — передача человеку
    cc = make(cls=SCC)
    cc.labels = {"Далее": "Оплатить 1 290 ₽"}
    cc, sm = scen([{"op": "open", "url": "https://dodopizza.ru"},
                   {"op": "click", "target": "Далее",
                    "host": "dodopizza.ru"},
                   {"op": "click", "target": "Корзина",
                    "host": "dodopizza.ru"}], cc)
    r7 = sm.start("пицца", "s3", None)
    check("оплата при воспроизведении — handoff, не нажато, прогон завершён",
          len(cc.calls) == 1 and "оплата" in r7.lower()
          and not sm.active("s3"))
    # Сбой гейта в execute (шаг забыл спросить) → та же пауза
    cc, sm = scen(PIZZA)
    run = {"name": "пицца", "steps": [], "pos": 0, "slots": {},
           "awaiting": None, "failed": False, "confirm": None,
           "user_id": "A"}
    res = sm._run_act({"kind": "click", "idx": 1,
                       "element": "Удалить аккаунт", "host": "x.ru"},
                      run, "s4", None)
    check("execute отказал → сценарий ставит паузу, а не «сбой»",
          res == (False, None, None) and run["confirm"]
          and not cc.calls)
    # Ввод в чувствительное поле: ответ на слот — без паузы; литерал — пауза
    cc, sm = scen([{"op": "open", "url": "https://id.example.com"},
                   {"op": "ask", "slot": "секрет1",
                    "question": "Что ввести в поле «Пароль»?"},
                   {"op": "type", "field": "Пароль", "value": "{секрет1}",
                    "host": "id.example.com"}])
    sm.start("пицца", "s5", None)
    sm.feed("s5", "Kotik2019!", None)
    check("ответ на слот в чувствительное поле — ввод без второго вопроса",
          len(cc.calls) == 2 and cc.calls[1].get("text") == "Kotik2019!"
          and not sm.active("s5"))
    cc, sm = scen([{"op": "open", "url": "https://id.example.com"},
                   {"op": "type", "field": "Пароль", "value": "hunter2",
                    "host": "id.example.com"}])
    r8 = sm.start("пицца", "s6", None)
    check("литерал в чувствительное поле — пауза, в вопросе маска",
          len(cc.calls) == 1 and "(да/нет)" in r8 and "hunter2" not in r8)
    # «стоп» между шагами (флаг менеджера управления)
    STEPS4 = [{"op": "open", "url": "https://dodopizza.ru"},
              {"op": "click", "target": "Меню", "host": "dodopizza.ru"},
              {"op": "click", "target": "Пепперони", "host": "dodopizza.ru"},
              {"op": "click", "target": "Корзина", "host": "dodopizza.ru"}]

    class StopCC(SCC):
        def _dispatch(self, action, router=None):
            super()._dispatch(action, router)
            if action.get("element") == "Меню":
                self.request_stop("TG1")
    cc, sm = scen(STEPS4, make(cls=StopCC))
    r9 = sm.start("пицца", "TG1", None)
    check("«стоп» во время шага 2 — шаги 3–4 не исполнены",
          [c.get("element") for c in cc.calls] == [None, "Меню"]
          and "Остановлено" in r9 and not sm.active("TG1"))
    cc.stop_clear("TG1")

    # «стоп» флагом прогона (cc_turn_enter → request_stop)
    class FlagCC(SCC):
        def _dispatch(self, action, router=None):
            super()._dispatch(action, router)
            if action.get("element") == "Пепперони":
                self.sm_ref.request_stop("F1")
    fcc = make(cls=FlagCC)
    cc, sm = scen(STEPS4, fcc)
    fcc.sm_ref = sm
    check("request_stop без идущего прогона — False",
          sm.request_stop("F1") is False)
    r10 = sm.start("пицца", "F1", None)
    check("флаг прогона — стоп перед следующим шагом",
          [c.get("element") for c in cc.calls] == [None, "Меню", "Пепперони"]
          and "Остановлено" in r10 and not sm.active("F1"))
    check("голое «стоп» при ждущем прогоне — отмена",
          ScenarioManager.parse_cancel("стоп")
          and ScenarioManager.parse_cancel("stop"))
    # _strip_payment: «Перевод» на банке — handoff, коммит остаётся
    sp = ScenarioManager._strip_payment([
        {"op": "open", "url": "https://online.sberbank.ru"},
        {"op": "click", "target": "Оформить заказ", "host": "shop.ru"},
        {"op": "click", "target": "Перевод", "host": "online.sberbank.ru"},
        {"op": "click", "target": "Готово", "host": "online.sberbank.ru"}])
    check("_strip_payment: коммит остаётся, банковский «Перевод» — handoff",
          [s["op"] for s in sp] == ["open", "click", "handoff"])

    # ── 7. Агент задач: токен только после своего «да» ──
    print("\n7. агент задач")
    from app.features.task_agent import TaskAgent

    class ACC(Spy):
        page = [{"idx": 22, "tag": "button", "role": "button",
                 "text": "Оформить заказ", "vp": True},
                {"idx": 23, "tag": "button", "role": "button",
                 "text": "Меню", "vp": True}]

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            return "https://pizza.test/menu", "pizza.test", list(self.page), \
                1, None

    class SR:
        def __init__(self, replies):
            self.replies = list(replies)

        def get_response(self, messages, **kw):
            r = self.replies.pop(0) if self.replies else {
                "action": "fail", "message": "script over"}
            return json.dumps(r, ensure_ascii=False)

    ba_wait = ba.wait_dom_idle
    ba.wait_dom_idle = lambda *a, **k: None
    try:
        acc = make(cls=ACC)
        ag = TaskAgent(acc)
        rt = SR([{"action": "click", "n": 1},
                 {"action": "done", "message": "Готово."}])
        rq = ag.start("t1", "закажи пиццу на pizza.test", rt, user_id="A")
        # Коммит заказа: код читает факты страницы (read) и спрашивает
        clicks = lambda: [c for c in acc.calls if c.get("kind") == "click"]
        check("агент: «Оформить заказ» — вопрос, клика нет",
              "(да/нет)" in rq and not clicks())
        ag.feed("t1", "да", rt, user_id="A")
        check("агент: «да» автора → токен task, клик исполнен",
              len(clicks()) == 1 and clicks()[0]["idx"] == 22
              and clicks()[0]["confirmed"].via == "task")
        # Шаг, который агент не распознал как риск, — гейт execute → вопрос
        acc2 = make(cls=ACC)
        ag2 = TaskAgent(acc2)
        run2 = {"history": [], "turn_user": "A", "awaiting": None}
        kind, q2 = ag2._execute(run2, "t2", None,
                                {"kind": "click", "idx": 9, "element": "#9",
                                 "title": "Удалить аккаунт",
                                 "host": "pizza.test"}, "click \"#9\"")
        check("агент: отказ гейта → вопрос (_ask_confirm), клика нет",
              kind == "pause" and not acc2.calls
              and (run2["awaiting"] or {}).get("kind") == "confirm")
        kind3, msg3 = ag2._execute({"history": [], "turn_user": "A",
                                    "awaiting": None}, "t2", None,
                                   {"kind": "click", "idx": 9,
                                    "element": "#9", "aria": "Оплатить",
                                    "host": "pizza.test"}, "click \"#9\"")
        check("агент: оплата от гейта — передача человеку",
              kind3 == "finish" and not acc2.calls)
    finally:
        ba.wait_dom_idle = ba_wait

    # ── 8. Словарь риска: реальные подписи (red-team 1) ──
    print("\n8. словарь риска: подписи RU/EN")
    RL = ComputerControlManager.risky_label
    SHY = "­"
    POS = [
        # покупка/подписка/продление: цена + глагол, платный объект
        ("Купить за 299 ₽", "payment"), ("Купить за 1 990 ₽", "payment"),
        ("Buy for $4.99", "payment"), ("Buy now for $4.99", "payment"),
        ("Subscribe — $9.99/mo", "payment"), ("Subscribe for $9.99", "payment"),
        ("Продлить за 1 990 ₽", "payment"), ("Rent $3.99", "payment"),
        ("Rent", "payment"), ("Buy", "payment"), ("Purchase", "payment"),
        ("Get Premium — $9.99", "payment"), ("Upgrade to Pro $12", "payment"),
        ("Upgrade to Premium", "payment"), ("Купить подписку", "payment"),
        ("Купить билет", "payment"), ("Renew subscription", "payment"),
        ("Renew my membership", "payment"), ("Продлить подписку", "payment"),
        ("Продлить тариф", "payment"), ("Арендовать", "payment"),
        ("Взять в аренду", "payment"), ("Start free trial", "payment"),
        ("Buy tickets", "payment"), ("Buy credits", "payment"),
        ("Pre-order", "payment"), ("Оформить премиум", "payment"),
        ("Подключить тариф", "payment"),
        ("Подписаться за 199 ₽/мес", "payment"),
        ("Заказать за 2 500 руб.", "payment"), ("Get it for €2.99", "payment"),
        ("Unlock for 99 ₽", "payment"), ("Приобрести за 500 руб", "payment"),
        (f"Купи{SHY}ть за 299 ₽", "payment"),
        # удаление/выход/блокировка: префикс подтверждения, глагол в середине
        ("Да, удалить", "destructive"), ("Yes, delete", "destructive"),
        ("OK, delete", "destructive"), ("Навсегда удалить", "destructive"),
        ("Permanently delete", "destructive"), ("Empty trash", "destructive"),
        ("Empty the bin", "destructive"), ("Delete forever", "destructive"),
        ("Удалить навсегда", "destructive"),
        ("I understand, delete this repository", "destructive"),
        ("I understand the consequences, delete this repository",
         "destructive"),
        ("Leave server", "destructive"), ("Leave group", "destructive"),
        ("Leave workspace", "destructive"), ("Leave the channel", "destructive"),
        ("Leave", "destructive"), ("Block user", "destructive"),
        ("Block", "destructive"), ("Report and block", "destructive"),
        ("Report spam", "destructive"), ("Покинуть беседу", "destructive"),
        ("Покинуть группу", "destructive"), ("Выйти из группы", "destructive"),
        ("Да, выйти", "destructive"), ("Yes, log out", "destructive"),
        ("Заблокировать пользователя", "destructive"),
        ("Пожаловаться и заблокировать", "destructive"),
        ("Отписаться", "destructive"), ("Unsubscribe", "destructive"),
        ("Yes, unsubscribe", "destructive"),
        ("Clear all history", "destructive"),
        ("Да, очистить историю", "destructive"),
        ("Delete account", "destructive"), ("Move to trash", "destructive"),
        ("Remove from group", "destructive"),
        (f"Уда{SHY}лить аккаунт", "destructive"),
        ("Удaлить аккаунт", "destructive"),  # латинская «a»
        ("Dеlete account", "destructive"),  # кириллическая «е»
        ("Sign out of all devices", "destructive"),
        ("Uninstall", "destructive"), ("Wipe device", "destructive"),
        ("Terminate instance", "destructive"),
        ("Отменить подписку", "destructive"),
        ("Cancel membership", "destructive"),
        # отправка/оформление
        ("Да, отправить", "commit"), ("Yes, send", "commit"),
        ("OK, publish", "commit"), ("Confirm delete", "commit"),
        (f"Офор{SHY}мить заказ", "commit"), ("Place order", "commit"),
    ]
    NEG = ["Купить", "В корзину", "Add to cart", "Add to cart — $4.99",
           "В корзину 1 290 ₽", "Кофе в зёрнах 1 кг — 1 290 ₽", "Удалённые",
           "Удалённые (3)", "Deleted items", "Recently deleted", "Trash",
           "Корзина", "Недавно удалённые", "Hair remover",
           "Makeup remover — 490 ₽", "Как удалить аккаунт в Telegram?",
           "How to delete your account", "Подписаться", "Subscribe",
           "Leave a review", "Leave a comment", "Leave feedback", "Add block",
           "Blockchain", "Reports", "Мои заказы", "Заказ №123 на 1 500 ₽",
           "Получено 500 ₽", "Закрыть", "Close", "Меню", "Profile", "Да",
           "Yes", "OK", "Удалённая работа", "Quite interesting",
           "Report a problem", "Подписки", "Price: $9.99",
           "$9.99/mo", "Продлить сессию", "Продолжить", "Continue", "Upgrade",
           "Get started", "Показать удалённые", "Try it free"]
    pos_bad = [(lab, want, RL({"kind": "click", "element": lab}))
               for lab, want in POS
               if RL({"kind": "click", "element": lab}) != want]
    neg_bad = [(lab, RL({"kind": "click", "element": lab})) for lab in NEG
               if RL({"kind": "click", "element": lab}) is not None]
    check(f"рискованные подписи ({len(POS)} шт.) — нужный класс: {pos_bad}",
          len(POS) >= 60 and not pos_bad)
    check(f"мирные подписи ({len(NEG)} шт.) — None: {neg_bad}",
          len(NEG) >= 30 and not neg_bad)
    check("подпись в aria/title — тот же словарь",
          RL({"kind": "click", "element": "#3", "aria": "Yes, delete"})
          == "destructive"
          and RL({"kind": "click", "element": "#3",
                  "title": "Buy for $4.99"}) == "payment")
    check("цена у поля ввода — не покупка (ввод — имя поля)",
          RL({"kind": "type", "element": "Купить за 299 ₽", "text": "x"})
          is None)
    check("_is_payment: поисковый запрос с ценой — не оплата",
          not ccm._is_payment("купить айфон за 50 000 ₽"))
    mg = make()
    for lab in ("Да, удалить", "Buy for $4.99", "Leave server"):
        a8 = {"kind": "click", "idx": 5, "element": lab, "host": "x.example",
              "origin": "fast"}
        ok8, _ = mg.execute(a8, "c8")
        check(f"execute «{lab}» без токена — не нажато", ok8 is False
              and not mg.calls)
    mf = make({"confirm": False, "click": True})
    check("confirm:false — «Buy for $4.99» всё равно спрашивает",
          mf.needs_confirm({"kind": "click", "element": "Buy for $4.99",
                            "origin": "fast"}))

    # ── 9. Отправка (send / type+submit): явная команда — политика ──
    print("\n9. отправка")
    SEND = {"kind": "send", "host": "web.telegram.org"}
    for org in ("scenario", "task", "intent_llm", None):
        a9 = dict(SEND, origin=org) if org else dict(SEND)
        check(f"send origin={org} → commit", CR(a9) == "commit")
    check("send от маркера → marker", CR(dict(SEND, origin="marker"))
          == "marker")
    check("send fast (явное «отправь») → гейт не требует, решает политика",
          CR(dict(SEND, origin="fast")) is None)
    TS = {"kind": "type", "idx": 4, "element": "Сообщение", "text": "привет",
          "submit": True}
    check("ввод+submit от сценария/агента → commit",
          CR(dict(TS, origin="scenario")) == "commit"
          and CR(dict(TS, origin="task")) == "commit")
    check("ввод+submit в поисковое поле (field_safe) → None",
          CR(dict(TS, origin="task", field_safe=True)) is None)
    check("ввод без submit → None",
          CR({"kind": "type", "element": "Сообщение", "text": "x",
              "origin": "scenario"}) is None)
    mp = make({"confirm": False, "click": True})
    check("confirm:false: send fast — без вопроса (политика)",
          mp.needs_confirm(dict(SEND, origin="fast")) is False)
    ok9, _ = mp.execute(dict(SEND, origin="fast"), "c9")
    check("confirm:false: send fast исполнен", ok9 and len(mp.calls) == 1)
    a9s = dict(SEND, origin="scenario")
    ok9s, _ = mp.execute(a9s, "c9")
    check("send сценария без токена — не исполнен, confirm_required=commit",
          ok9s is False and len(mp.calls) == 1
          and a9s.get("confirm_required", {}).get("reason") == "commit")
    a9t = ComputerControlManager.grant_confirmation(
        dict(SEND, origin="scenario"), "scenario", by="A")
    ok9t, _ = mp.execute(a9t, "c9")
    check("send сценария с токеном — исполнен", ok9t and len(mp.calls) == 2)
    # Сценарий мессенджера: ввод ответа на слот, send — пауза, «да» — отправка
    cc9, sm9 = scen([{"op": "open", "url": "https://web.telegram.org/k/"},
                     {"op": "ask", "slot": "t", "question": "Что написать?"},
                     {"op": "type", "field": "Сообщение", "value": "{t}",
                      "host": "web.telegram.org"},
                     {"op": "send", "host": "web.telegram.org"}],
                    make({"confirm": False, "click": True}, cls=SCC))
    cc9.note_requester("s9", "A")
    sm9.start("пицца", "s9", None)
    r9a = sm9.feed("s9", "я задержусь", None)
    check("сценарий: send — пауза «да/нет», отправки нет",
          [c["kind"] for c in cc9.calls] == ["url", "type"]
          and "(да/нет)" in (r9a or "") and sm9.active("s9"))
    sm9.feed("s9", "да", None)
    check("сценарий: «да» — send исполнен с токеном сценария",
          [c["kind"] for c in cc9.calls] == ["url", "type", "send"]
          and cc9.calls[2]["confirmed"].via == "scenario")

    # ── 10. LLM-ярус: open с адресом от модели ──
    print("\n10. LLM-ярус: адрес от модели")
    mark = BotInstance._cc_mark_model_url
    mk = make({"confirm": False, "click": True,
               "sites": {"ютуб": "https://www.youtube.com"}})
    u1 = {"kind": "url",
          "value": "https://sber-online-lk.example/login?d=79991234567"}
    mark(mk, u1, "зайди в личный кабинет моего банка")
    check("выдуманный домен с query → via_search", u1.get("via_search"))
    u2 = {"kind": "url", "value": "https://bank.example/"}
    mark(mk, u2, "открой мой банк")
    check("домен не из sites/allow_domains → via_search",
          u2.get("via_search"))
    u3 = {"kind": "url", "value": "https://www.youtube.com"}
    mark(mk, u3, "включи-ка ютубчик")
    check("адрес алиаса из sites — без пометки", not u3.get("via_search"))
    u4 = {"kind": "url", "value": "https://example.org/"}
    mark(mk, u4, "загляни на example.org пожалуйста")
    check("домен, названный пользователем, без параметров — без пометки",
          not u4.get("via_search"))
    u5 = {"kind": "url", "value": "https://example.org/?d=79991234567"}
    mark(mk, u5, "загляни на example.org пожалуйста")
    check("названный домен, но с query от модели → via_search",
          u5.get("via_search"))
    u6 = {"kind": "multi", "items": [
        {"kind": "url", "value": "https://www.youtube.com"},
        {"kind": "url", "value": "https://evil.example/#tok"}]}
    mark(mk, u6, "открой ютуб и почту")
    check("multi: пометка пункта и всего действия",
          u6.get("via_search") and u6["items"][1].get("via_search")
          and not u6["items"][0].get("via_search"))
    u7 = {"kind": "app", "value": "Music"}
    mark(mk, u7, "включи музыку")
    check("приложение — без пометки", not u7.get("via_search"))
    check("помеченный адрес: needs_confirm при confirm:false и гейт",
          mk.needs_confirm(dict(u1, origin="intent_llm"))
          and CR(dict(u1, origin="intent_llm")) == "via_search")
    # Сквозной ход: бот с confirm:false, модель вернула open с ПДн в query
    import app.features.web_search as _ws
    import app.features.browser_history as _bh
    _fs, _fh = _ws.find_site_url, _bh.find_in_history
    _ws.find_site_url = lambda name: None
    _bh.find_in_history = lambda name: None
    try:
        b10 = bot({"confirm": False, "click": True}, cls=Spy)
        b10.router = _Router(
            '{"action":"open","target":'
            '"sber-online-lk.example/login?d=79991234567"}')
        b10.process_message("зайди в личный кабинет моего банка",
                            user_id="A", chat_id=CHAT)
        pend = b10.computer_control.get_pending(CHAT) or {}
        check("бот: open от LLM-яруса — вопрос, ничего не открыто",
              not b10.computer_control.calls and pend.get("via_search")
              and pend.get("origin") == "intent_llm")
    finally:
        _ws.find_site_url, _bh.find_in_history = _fs, _fh

    print(f"\n{total - fails}/{total} OK, FAIL: {fails}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

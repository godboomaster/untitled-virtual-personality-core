"""Тест хода задачи без лишних вызовов модели и честного конца на оплате
(task_agent: _done_at_payment, _replay_step, ветка «где?» в _feed_busy,
_open_needs_confirm). Ошибки нашёл прогон на стенде 20.10.

Проверяет:
* done модели на оформлении с платёжной кнопкой («Оплатить 599 ₽») — та же
  передача оплаты, что у границы оплаты кода: тот же итог, статус payment,
  план не отмечает оформление сделанным, ничего не нажато, в память — ok;
  несходство корзины (C3) после отбоя — к передаче пометка «посмотри
  корзину»;
* done на оформлении без платёжной кнопки и done не в заказе — как раньше;
* голый выбор сайта на вопрос кода «на каком сайте?» — сайт открывает код,
  без вызова модели (выбран другой сайт — его ссылка из выдачи); ответ с
  подробностями — ход модели;
* отложенный клик «В корзину» после вопросов о товаре: «как есть», «без
  добавок», «традиционное» при выбранном традиционном, «0,5» при 0,5 л —
  клик кодом без вызова модели; ответ с изменением («тонкое», «с
  моцареллой», «1 л», «без лука»), голое «да», кнопка сменилась — ход
  модели;
* адрес из цели или ответа — без «Делаю?»; чужой адрес, данные в query
  поверх названного хоста — с ним; вызов бота (пустой прогон) — как раньше.

Браузер — настоящий гейт ComputerControlManager с заглушкой _dispatch
(real_cc), модель — ScriptedRouter (test_task_agent): живых браузеров,
веб-чатов и LLM нет.

Запуск: PYTHONPATH=. python3 scripts/test_task_step_replay.py
"""

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.test_task_agent import ScriptedRouter, real_cc  # noqa: E402

FAILS = 0
OK = 0


def check(name, cond, detail=""):
    global FAILS, OK
    OK += 1
    if cond:
        print(f"  [OK] {name}")
    else:
        FAILS += 1
        print(f"  [FAIL] {name} {detail}")


class SlotRouter:
    """slot_router: разбор ответа — JSON по ответу человека (нет в slots —
    {}), «что спросить перед шагом?» — ask, прочее — NONE. brief — промпты
    разбора ответа моделью."""

    def __init__(self, slots=None, ask="NONE"):
        self.slots, self.ask = dict(slots or {}), ask
        self.prompts, self.brief = [], []

    def get_response(self, messages, **kw):
        p = messages[-1]["content"]
        self.prompts.append(p)
        if "You keep the brief" in p:
            self.brief.append(p)
            m = re.search(r'User\'s answer: "(.*)"\n', p)
            return json.dumps(self.slots.get(m.group(1) if m else "", {}),
                              ensure_ascii=False)
        if "What do you need to ask the user before this step?" in p:
            return self.ask
        return "NONE"


def main():
    from app.features import browser_actions as _ba
    from app.features import cc_privacy as _cp
    from app.features import task_agent as ta
    from app.features.task_agent import TaskAgent

    # Браузера нет: разметка разделов, листание, целевой снимок — заглушки
    _ba.wait_dom_idle = lambda *a, **k: None
    _ba.section_items = lambda *a, **k: None
    _ba.section_names = lambda *a, **k: None
    _ba.scroll_step = lambda *a, **k: {"moved": True, "bottom": False}
    _ba.scroll_container_step = lambda *a, **k: {"moved": True,
                                                  "bottom": False, "y0": 0}
    _ba.snapshot_for_goal = lambda host, text, tab_id=None: ("", [])
    # «Локальная модель» приватной страницы — тот же скриптованный роутер
    # (Ollama не зовётся)
    _cp.PrivateRouter.get_response = (
        lambda self, msgs, **kw: self._router.get_response(msgs, **kw))
    tmp = Path(tempfile.mkdtemp(prefix="step_replay_"))
    n_mem = [0]

    def agent(cc, slot=None):
        n_mem[0] += 1
        ag = TaskAgent(cc, memory_path=tmp / f"m{n_mem[0]}.json")
        ag.slot_router = slot
        return ag

    def urls(cc):
        return [d.get("value") for d in cc.dispatched
                if d.get("kind") == "url"]

    def clicks(cc):
        return [d.get("idx") for d in cc.dispatched
                if d.get("kind") == "click"]

    def finished(ag, chat):
        return (ag.__dict__.get("_finished", {}).get(chat) or {}).get("run") \
            or ag._runs.get(chat) or {}

    # ── 1. done на оформлении ──
    print("done на оформлении:")
    CO = "https://pizzatest.test/checkout"
    hdr = {"idx": 2, "tag": "button", "role": "button", "text": "Корзина 1",
           "aria": "Корзина"}
    pay_page = [
        {"idx": 1, "tag": "input", "role": "textbox", "text": "Адрес доставки",
         "ed": True},
        hdr,
        {"idx": 3, "tag": "label", "role": "radio", "text": "Картой онлайн",
         "on": 1},
        {"idx": 4, "tag": "button", "role": "button",
         "text": "Оплатить 599 ₽"},
        {"idx": 5, "tag": "a", "role": "link", "text": "Доставка и оплата"}]
    commit_page = [x for x in pay_page if x["idx"] != 4] + [
        {"idx": 6, "tag": "button", "role": "button",
         "text": "Оформить заказ"}]
    done_msg = ("Заказ собран: пепперони. Дальше оплата — её нужно сделать "
                "вам самим.")
    goal = "закажи пепперони на pizzatest.test"
    goal_slot = {goal: {"site": "pizzatest.test",
                        "items": [{"name": "Пепперони"}]}}

    def at_checkout(chat, page, replies, goal=goal, verified=True):
        # Задача дошла до оформления: позиция в корзине (проверена
        # счётчиком), «что-нибудь ещё?» — «нет», корзина открыта
        cc = real_cc({CO: [dict(x) for x in page]}, url=CO, base_dir=tmp)
        ag = agent(cc, SlotRouter(goal_slot))
        rt = ScriptedRouter([{"action": "ask", "question": "Куда доставить?"}]
                            + replies)
        ag.start(chat, goal, rt, user_id="A")
        run = ag._runs[chat]
        run.update(cart_adds=[{"key": "Пепперони", "verified": verified}],
                   more_no=True, more_n=1, cart_checked=True)
        r = ag.feed(chat, "Ленина 5", rt, user_id="A")
        return cc, ag, rt, r

    cc, ag, rt, r = at_checkout("d1", pay_page,
                                [{"action": "done", "message": done_msg}])
    card = ag.task_card("d1") or {}
    plan = card.get("plan") or []
    check("done при видимой «Оплатить 599 ₽» — передача оплаты, а не «готово»",
          "Дошёл до оплаты («Оплатить 599 ₽»)" in r and "дальше сам" in r
          and done_msg not in r, r)
    check("статус карточки — payment, как у границы оплаты",
          card.get("status") == "payment", f"{card}")
    check("план: оформление не отмечено сделанным (текущий шаг)",
          plan and plan[-1]["text"].startswith("оформить заказ")
          and plan[-1]["state"] == "current", f"{plan}")
    check("ничего не нажато (платёжный элемент — за человеком)",
          not clicks(cc))
    check("передача оплаты — без «да/продолжай» (reopen не ждёт)",
          "d1" not in ag.__dict__.get("_finished", {})
          and "d1" not in ag._runs)
    mem = json.loads((ag._memory_path).read_text(encoding="utf-8"))
    check("память задач — успешный исход, как у границы оплаты",
          mem["d1"][-1]["ok"] is True)
    # Граница оплаты кода: модель жмёт «Оплатить 599 ₽» — тот же итог
    cc_b, ag_b, _rt, r_b = at_checkout("d1b", pay_page, [
        {"action": "click", "n": 3, "label": "Оплатить 599 ₽"}])
    card_b = ag_b.task_card("d1b") or {}
    check("тот же итог и статус, что при остановке кода перед «Оплатить»",
          r_b.splitlines()[-1] == r.splitlines()[-1]
          and card_b.get("status") == card.get("status")
          and card_b.get("plan") == plan and not clicks(cc_b),
          f"{r_b!r} / {r!r}")
    # Корзина не подтверждена счётчиком — раз назад модели (C3), потом
    # передача с пометкой
    cc, ag, rt, r = at_checkout("d2", pay_page, [
        {"action": "done", "message": done_msg},
        {"action": "done", "message": done_msg}], verified=False)
    check("C3 не сошлось: раз назад модели, затем передача оплаты с «посмотри "
          "корзину»",
          any("NOT finished" in p for p in rt.prompts)
          and "Дошёл до оплаты" in r and "проверить не смог" in r
          and (ag.task_card("d2") or {}).get("status") == "payment", r)
    # Без платёжной кнопки — как раньше
    cc, ag, rt, r = at_checkout("d3", commit_page,
                                [{"action": "done", "message": done_msg}])
    card = ag.task_card("d3") or {}
    check("оформление без платёжной кнопки — done как раньше",
          done_msg in r and "Дошёл до оплаты" not in r
          and card.get("status") == "done", f"{r} {card.get('status')}")
    # Цель не заказ — как раньше
    cc = real_cc({CO: [dict(x) for x in pay_page]}, url=CO, base_dir=tmp)
    ag = agent(cc)
    r = ag.start("d4", "посмотри, что лежит в корзине на pizzatest.test",
                 ScriptedRouter([{"action": "done", "message":
                                  "В корзине пепперони."}]), user_id="A")
    check("цель не заказ — done как раньше, даже при «Оплатить»",
          "В корзине пепперони." in r and "Дошёл до оплаты" not in r
          and (ag.task_card("d4") or {}).get("status") == "done", r)
    # Футер «Доставка и оплата» и способ оплаты — не платёжный элемент
    TA = ta.TaskAgent
    check("ссылка «Доставка и оплата», переключатель «Картой онлайн», поле "
          "«Способ оплаты», «К оформлению заказа» — не платёжный элемент; "
          "«Перейти к оплате» и поле номера карты — да",
          TA._pay_element({"shown": commit_page, "host": "x.test"}) is None
          and TA._pay_element({"shown": [
              {"idx": 1, "tag": "button", "text": "Перейти к оплате"}]})
          == "Перейти к оплате"
          and TA._pay_element({"shown": [
              {"idx": 1, "tag": "input", "text": "Номер карты", "ed": True,
               "sn": True}]}) == "Номер карты"
          and TA._pay_element({"shown": [
              {"idx": 1, "tag": "select", "text": "Способ оплаты",
               "ed": True}]}) is None
          and TA._pay_element({"shown": [
              {"idx": 1, "tag": "button", "text": "К оформлению заказа"}]})
          is None)

    # ── 2. Выбор сайта → открытие кодом ──
    print("выбор сайта:")
    A, B = "https://alfa-pizza.test/", "https://beta-pizza.test/menu"
    found = [{"title": "Альфа Пицца", "snippet": "от 399 ₽", "url": A},
             {"title": "Бета Пицца", "snippet": "от 459 ₽", "url": B}]
    real_search = ta.web_search_links
    ta.web_search_links = lambda q, **kw: (found, None)
    try:
        def where(chat):
            cc = real_cc({A: [{"idx": 1, "tag": "button", "role": "button",
                               "text": "Войти"}],
                          B: [{"idx": 1, "tag": "button", "role": "button",
                               "text": "Войти"}]}, base_dir=tmp)
            ag = agent(cc)
            rt = ScriptedRouter([
                {"action": "search", "query": "пицца доставка"},
                {"action": "open", "target": A},
                {"action": "fail", "message": "стоп"}])
            q = ag.start(chat, "закажи пиццу", rt, user_id="A")
            return cc, ag, rt, q
        cc, ag, rt, q = where("s1")
        check("до выбора — вопрос «где?» кода, магазин не открыт",
              "На каком сайте" in q and not urls(cc)
              and ag._runs["s1"]["awaiting"].get("site_open") == A)
        n0 = len(rt.prompts)
        r = ag.feed("s1", "1", rt, user_id="A")
        check("«1» — открыт выбранный сайт без вызова модели (тот же open, "
              "на котором спросили)",
              urls(cc) == [A] and len(rt.prompts) == n0 + 1
              and f"Current page: {A}" in rt.prompts[n0], f"{urls(cc)}")
        cc, ag, rt, q = where("s2")
        n0 = len(rt.prompts)
        ag.feed("s2", "2", rt, user_id="A")
        check("«2» (другой сайт) — открыта его ссылка из выдачи, без вызова "
              "модели и без «Делаю?»",
              urls(cc) == [B] and len(rt.prompts) == n0 + 1
              and f"Current page: {B}" in rt.prompts[n0], f"{urls(cc)}")
        cc, ag, rt, q = where("s3")
        n0 = len(rt.prompts)
        ag.feed("s3", "бета пицца", rt, user_id="A")
        check("название варианта («бета пицца») — тоже голый выбор: открыт "
              "кодом",
              urls(cc) == [B] and len(rt.prompts) == n0 + 1, f"{urls(cc)}")
        cc, ag, rt, q = where("s4")
        n0 = len(rt.prompts)
        ag.feed("s4", "2, но без грибов", rt, user_id="A")
        check("ответ с подробностями — сайт не открыт кодом, ход модели",
              not urls(cc) and len(rt.prompts) == n0 + 1
              and f"Current page: {B}" not in rt.prompts[n0], f"{urls(cc)}")
    finally:
        ta.web_search_links = real_search

    # ── 3. Отложенный клик «В корзину» ──
    print("отложенный клик:")
    SHOP, ADDED = "https://pizzatest.test/", "https://pizzatest.test/?added=1"
    pctx = "Пепперони 30 см, традиционное тесто"
    # Счётчик корзины в шапке (под окном): по нему засчитывается добавление
    cart0 = {"idx": 9, "tag": "button", "role": "button", "text": "Корзина",
             "aria": "Корзина"}

    def pizza(price="599"):
        md = lambda i, t, on=None: dict(
            {"idx": i, "tag": "button", "role": "radio" if on is not None
             else "button", "text": t, "md": 1, "ctx": pctx},
            **({"on": on} if on is not None else {}))
        return [cart0, md(1, f"В корзину · {price} ₽"),
                md(2, "Маленькая 25 см", 0),
                md(3, "Средняя 30 см", 1), md(4, "Большая 35 см", 0),
                md(5, "Традиционное", 1), md(6, "Тонкое", 0),
                md(7, "Моцарелла 79 ₽", 0), md(8, "Халапеньо 59 ₽", 0)]
    added = [{"idx": 9, "tag": "button", "role": "button",
              "text": "Корзина 1 · 599 ₽", "aria": "Корзина"},
             {"idx": 10, "tag": "a", "role": "link", "text": "Меню"}]
    pgoal = "закажи пепперони 30 см на pizzatest.test"

    def gate(chat, answer, ch=None, page=None, after=None):
        cc = real_cc({SHOP: page or pizza(), ADDED: added}, url=SHOP,
                     clicks={1: ADDED}, base_dir=tmp)
        slot = SlotRouter({pgoal: {"site": "pizzatest.test", "items": [
            {"name": "Пепперони", "size": "30 см"}]},
            **({answer: ch} if ch is not None else {})},
            ask="Тесто: традиционное или тонкое? Сейчас выбрано традиционное.")
        ag = agent(cc, slot)
        rt = ScriptedRouter([
            {"action": "click", "n": 1, "label": "В корзину · 599 ₽",
             "expect": "the cart count grows"},
            {"action": "fail", "message": "стоп"}])
        q = ag.start(chat, pgoal, rt, user_id="A")
        if after:
            after(cc)
        n0 = len(rt.prompts)
        r = ag.feed(chat, answer, rt, user_id="A")
        return cc, ag, rt, q, r, len(rt.prompts) - n0
    cc, ag, rt, q, r, n = gate("r1", "традиционное, без добавок", {
        "items": [{"name": "Пепперони", "options": ["традиционное тесто"]}]})
    check("до клика — вопросы о товаре, «В корзину» не нажата",
          "Перед тем как положить «Пепперони» в корзину" in q
          and "Моцарелла" in q, q)
    check("«традиционное, без добавок» при выбранном традиционном — клик "
          "кодом без вызова модели, дальше «что-нибудь ещё?»",
          clicks(cc) == [1] and n == 0 and "Добавить что-нибудь ещё?" in r,
          f"{clicks(cc)} {n} {r!r}")
    hist = finished(ag, "r1").get("history") or []
    check("повтор — обычный шаг: в истории клик «→ ok», ожидание модели "
          "проверено, добавление засчитано",
          any(h.startswith('click "В корзину · 599 ₽" → ok') for h in hist)
          and any(x.get("verified") for x in finished(ag, "r1").get(
              "cart_adds") or ()), f"{hist}")
    for ans in ("как есть", "без добавок", "оставь как есть",
                "средняя, традиционное", "тесто любое, без добавок"):
        cc, ag, rt, q, r, n = gate(f"r-{ans}", ans)
        check(f"«{ans}» — выбор не меняет: клик кодом, без вызова модели",
              clicks(cc) == [1] and n == 0, f"{clicks(cc)} {n}")
    for ans, ch in (
            ("тонкое", {"items": [{"name": "Пепперони",
                                   "options": ["тонкое тесто"]}]}),
            ("с моцареллой", {"items": [{"name": "Пепперони",
                                         "options": ["моцарелла"]}]}),
            ("большую", {"items": [{"name": "Пепперони", "size": "35 см"}]}),
            ("без лука", {}), ("да", {}), ("давай", {}),
            ("как есть, две штуки", {"items": [{"name": "Пепперони",
                                               "qty": 2}]}),
            # Разбор модели вразрез с выбранным — ход модели. Голое
            # «традиционное» и «как есть» теперь разбирает код (_gate_fast,
            # test_task_brief_fast_more): к модели — ответы с припиской
            ("оставь традиционное", {"items": [{"name": "Пепперони",
                                                "options": ["тонкое тесто"]}]}),
            ("любое", {"address": "Ленина 5"})):
        cc, ag, rt, q, r, n = gate(f"m-{ans}-{len(ch)}", ans, ch)
        check(f"«{ans}» (разбор {json.dumps(ch, ensure_ascii=False)[:40]}) — "
              "что-то меняет или сомнение: ход модели, кодом не нажато",
              not clicks(cc) and n == 1, f"{clicks(cc)} {n}")
    # Кнопка сменилась, пока ждали ответа (цена другая — выбор поменяли в
    # браузере) — ход модели
    cc, ag, rt, q, r, n = gate(
        "g1", "как есть",
        after=lambda cc: cc.pages.__setitem__(SHOP, pizza("648")))
    check("кнопка «В корзину» сменилась (другая цена) — повтора нет, ход "
          "модели", not clicks(cc) and n == 1, f"{clicks(cc)} {n}")
    cc, ag, rt, q, r, n = gate(
        "g2", "как есть",
        after=lambda cc: setattr(cc, "cur", ADDED))
    check("страница другая — повтора нет, ход модели",
          not clicks(cc) and n == 1, f"{clicks(cc)} {n}")
    # Объём колы: «0,5» при выбранных 0,5 л — кодом; «1 л» — модели
    cctx = "Кола 0,5 л 1 л"
    cola = [cart0, {"idx": 1, "tag": "button", "role": "button",
             "text": "В корзину · 119 ₽", "md": 1, "ctx": cctx},
            {"idx": 2, "tag": "button", "role": "radio", "text": "0,5 л",
             "md": 1, "on": 1, "ctx": cctx},
            {"idx": 3, "tag": "button", "role": "radio", "text": "1 л",
             "md": 1, "on": 0, "ctx": cctx}]
    cgoal = "закажи колу на pizzatest.test"
    # «0,5»/«0.5» разбирает код (_gate_fast: 0,5 л) — клик кодом; разбор
    # модели вразрез с ответом («0,5, как есть» → 1 л) — ход модели
    for ans, ch, by_code in (
            ("0,5", {"items": [{"name": "кола", "size": "0,5 л"}]}, True),
            ("1 л", {"items": [{"name": "кола", "size": "1 л"}]}, False),
            ("0.5", {"items": [{"name": "кола", "size": "0,5 л"}]}, True),
            ("0,5, как есть", {"items": [{"name": "кола", "size": "1 л"}]},
             False)):
        cc = real_cc({SHOP: [dict(x) for x in cola], ADDED: added}, url=SHOP,
                     clicks={1: ADDED}, base_dir=tmp)
        ag = agent(cc, SlotRouter({cgoal: {"site": "pizzatest.test", "items": [
            {"name": "Кола"}]}, ans: ch},
            ask="Объём колы: 0,5 л или 1 л? Сейчас выбрано 0,5 л."))
        rt = ScriptedRouter([
            {"action": "click", "n": 1, "label": "В корзину · 119 ₽"},
            {"action": "fail", "message": "стоп"}])
        ag.start(f"c-{ans}", cgoal, rt, user_id="A")
        n0 = len(rt.prompts)
        ag.feed(f"c-{ans}", ans, rt, user_id="A")
        n = len(rt.prompts) - n0
        check(f"кола: «{ans}» (разбор {ch['items'][0]['size']}) — "
              + ("клик кодом" if by_code else "ход модели"),
              (clicks(cc) == [1] and n == 0) if by_code
              else (not clicks(cc) and n == 1), f"{clicks(cc)} {n}")

    # ── 4. Адрес из цели — без «Делаю?» ──
    print("адрес из слов человека:")
    DL = "https://pizzatest.test/delivery"
    dgoal = "открой pizzatest.test/delivery и скажи, до скольки они работают"

    def opener(chat, goal, targets, pages=None):
        cc = real_cc(pages or {DL: [{"idx": 1, "tag": "a", "role": "link",
                                     "text": "Меню"}]}, base_dir=tmp)
        ag = agent(cc)
        rt = ScriptedRouter([{"action": "open", "target": t} for t in targets]
                            + [{"action": "fail", "message": "стоп"}])
        return cc, ag, ag.start(chat, goal, rt, user_id="A")
    cc, ag, r = opener("o1", dgoal, ["pizzatest.test/delivery"])
    check("адрес из цели (стенд, вызов 33) — открыт без «Делаю?»",
          urls(cc) == [DL] and "Делаю?" not in r, f"{urls(cc)} {r!r}")
    cc, ag, r = opener("o2", dgoal, ["https://www.pizzatest.test/"])
    check("другой адрес того же названного хоста — без «Делаю?»",
          urls(cc) == ["https://www.pizzatest.test/"] and "Делаю?" not in r,
          f"{urls(cc)}")
    cc, ag, r = opener("o3", dgoal, ["https://evil.test/delivery"] * 2)
    check("чужой адрес — как раньше: не открыт, «Делаю?»",
          not urls(cc) and "Делаю?" in r
          and ag._runs["o3"]["awaiting"]["kind"] == "confirm", r)
    cc, ag, r = opener("o4", dgoal, [DL + "?ref=79991234567"])
    check("данные в query поверх названного хоста — «Делаю?»",
          not urls(cc) and "Делаю?" in r, r)
    QU = "https://shop.test/search?q=pizza"
    cc, ag, r = opener("o5", f"открой {QU} и скажи, что нашлось", [QU],
                       pages={QU: []})
    check("адрес с query, который человек написал сам, — дословно без "
          "«Делаю?»", urls(cc) == [QU] and "Делаю?" not in r, f"{urls(cc)}")
    # Хост из ответа человека
    OT = "https://other.test/"
    cc = real_cc({OT: []}, base_dir=tmp)
    ag = agent(cc)
    rt = ScriptedRouter([{"action": "ask",
                          "question": "На каком сайте искать?"},
                         {"action": "open", "target": OT},
                         {"action": "fail", "message": "стоп"}])
    ag.start("o6", "найди скидки на наушники", rt, user_id="A")
    r = ag.feed("o6", "other.test", rt, user_id="A")
    check("хост из ответа человека — без «Делаю?»",
          urls(cc) == [OT] and "Делаю?" not in r, f"{urls(cc)} {r!r}")
    check("вызов бота (пустой прогон) — как раньше: новый домен с «да»",
          ta._open_needs_confirm(cc, {}, {"kind": "url", "value": DL}, DL))
    run_s = {"goal": "войди", "qa": [("Какой пароль?", "secret.example")]}
    check("хост из ответа-секрета — не «назван человеком»",
          not ta._user_named(run_s, "https://secret.example/"))

    print(f"\nИтого: {OK} проверок, {FAILS} провалов")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

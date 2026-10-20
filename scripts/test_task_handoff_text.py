"""Тест текстов агента задач человеку: вопросы перед шагом без ложных
отсечений и сводка заказа при передаче оплаты (task_agent: _PAY_Q_RE,
_drop_system_qs, _question_lines, _handoff_summary, _payment_handoff).
Ошибки нашёл прогон на стенде 20.10.

Проверяет:
* «за доплату», «без доплаты», «доплатить» — платная добавка, не вопрос об
  оплате; «предоплата», «оплатить», «заплатить», «платёж», «картой» —
  оплата; «pay extra» — добавка, «pay by card» — оплата; «входит в комбо»,
  «войдёт» — не вход, «почти» — не почта;
* run2, вызов 8: строка «Тесто: …? … за доплату: …?» — человеку целиком,
  код не добавляет второй список добавок;
* вопрос-нарушитель в строке выпадает один: соседи и пояснение прошлого
  вопроса остаются, пояснение нарушителя уходит с ним, варианты под
  строкой — её последнего вопроса, сокращения («ул.») обрывков не дают;
* передача оплаты — со сводкой кодом (состав из корзины задачи с размером
  и добавками из брифа, сумма с кнопки, адрес, телефон маской), а не с
  отчётом модели: и из done на оформлении, и на границе оплаты кода; ru и
  en; статус карточки — payment, память — успешный исход;
* варианты позиции («традиционное») — в скобках без «+»: сводка оплаты,
  «В корзине: …», состав подтверждения, план человеку (ru, en) и модели;
* маски: телефон, номер карты в слоте оплаты, email; в группе значения с
  приватной страницы не цитируются, в личке — заметка в историю бота
  заглушкой; цель не заказ — без сводки;
* ключи текстов сводки — ru и en с теми же плейсхолдерами.

Браузер — настоящий гейт ComputerControlManager с заглушкой _dispatch
(real_cc), модель — ScriptedRouter/SlotRouter: живых браузеров, веб-чатов
и LLM нет.

Запуск: python3 -m scripts.test_task_handoff_text
"""

import json
import re
import string
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.test_task_agent import ScriptedRouter, real_cc  # noqa: E402
from scripts.test_task_step_replay import SlotRouter  # noqa: E402

FAILS = 0
OK = 0
CYR = re.compile(r"[а-яё]", re.IGNORECASE)


def check(name, cond, detail=""):
    global FAILS, OK
    OK += 1
    if cond:
        print(f"  [OK] {name}")
    else:
        FAILS += 1
        print(f"  [FAIL] {name} {detail}")


def main():
    from app.features import browser_actions as _ba
    from app.features import cc_privacy as _cp
    from app.features import cc_texts
    from app.features import task_agent as ta
    from app.features.task_agent import TaskAgent

    _ba.wait_dom_idle = lambda *a, **k: None
    _ba.section_items = lambda *a, **k: None
    _ba.section_names = lambda *a, **k: None
    _ba.scroll_step = lambda *a, **k: {"moved": True, "bottom": False}
    _ba.scroll_container_step = lambda *a, **k: {"moved": True,
                                                  "bottom": False, "y0": 0}
    _ba.snapshot_for_goal = lambda host, text, tab_id=None: ("", [])
    # «Локальная модель» приватной страницы — тот же скриптованный роутер
    _cp.PrivateRouter.get_response = (
        lambda self, msgs, **kw: self._router.get_response(msgs, **kw))
    tmp = Path(tempfile.mkdtemp(prefix="handoff_text_"))
    n_mem = [0]

    def agent(cc, slot=None):
        n_mem[0] += 1
        ag = TaskAgent(cc, memory_path=tmp / f"m{n_mem[0]}.json")
        ag.slot_router = slot
        return ag

    def clicks(cc):
        return [d.get("idx") for d in cc.dispatched
                if d.get("kind") == "click"]

    # ── 1. «доплата» и соседи по корню ──
    print("слова об оплате:")
    pay = ta._PAY_Q_RE
    for s in ("Добавить что-то за доплату: сырный бортик 169 ₽?",
              "Сырный бортик — с доплатой 169 ₽?", "Соус без доплаты?",
              "Доплатить за бортик 169 ₽?", "Доплата 59 ₽ — берём?",
              "Want to pay extra for a cheese crust?",
              "Pay more for the large size?", "Бесплатный соус добавить?",
              "Платье?", "Вес — платформа?"):
        check(f"не оплата: {s!r}", not pay.search(s))
    for s in ("Оплатить картой?", "Предоплата картой или наличными?",
              "Внести предоплату?", "Как будете оплачивать?",
              "Оплачу сразу?", "Как будете платить?", "Заплатить сейчас?",
              "Способ платежа?", "Платёжные данные?", "Картой или наличными?",
              "Pay by card or cash?", "Payment method?",
              "Prepayment required?", "Enter the CVV?"):
        check(f"оплата: {s!r}", bool(pay.search(s)))
    data, contact = ta._DATA_QUESTION_RE, ta._CONTACT_Q_RE
    for s in ("Что входит в комбо?", "Соусы входят в стоимость?",
              "Сколько войдёт в коробку?"):
        check(f"не вход: {s!r}", not data.search(s))
    for s in ("Данные для входа?", "Войдите в аккаунт?", "Как войти?",
              "Вход через Госуслуги?", "Логин?"):
        check(f"вход: {s!r}", bool(data.search(s)))
    check("«почти» — не почта (вопрос и чувствительная подпись), «почта», "
          "«почтой», «почтовый» — почта",
          not contact.search("Почти всё выбрано — соус?")
          and not _cp.is_sensitive_label("Почти готово")
          and all(contact.search(s) for s in ("Почта?", "Прислать почтой?",
                                              "Почтовый адрес?"))
          and _cp.is_sensitive_label("Почта"))
    check("вопрос с «почти» — ответ не секрет (модель видит выбор)",
          not ta._secret_answer("Почти всё выбрано — какой соус?", "сырный"))
    check("показ раздела моделью с «за доплату» — не отбит",
          ta.TaskAgent._section_text_ok(
              "- Пепперони — 599 ₽ (сырный бортик за доплату)",
              ["Пепперони 599 ₽"]))

    # ── 2. Вопрос-нарушитель — без соседей ──
    print("вопрос-нарушитель:")
    call8 = ("Тесто: традиционное или тонкое? Сейчас выбрано традиционное. "
             "Добавить что-то за доплату: сырный бортик 169 ₽, моцарелла "
             "79 ₽, халапеньо 59 ₽, шампиньоны 59 ₽, бекон 89 ₽, красный лук "
             "39 ₽ — или без добавок?")
    check("run2, вызов 8: строка про тесто и добавки «за доплату» — целиком",
          ta._question_lines(call8) == [call8])
    ql = ta._question_lines
    check("«Тесто: …? Сейчас выбрано традиционное. Оплатить картой?» — тесто "
          "с пояснением остаётся, варианты нарушителя — мимо",
          ql("Тесто: традиционное или тонкое? Сейчас выбрано традиционное. "
             "Оплатить картой?\n- Да\n- Нет")
          == ["Тесто: традиционное или тонкое? Сейчас выбрано традиционное."])
    check("нарушитель первым — следующий вопрос и его варианты остаются",
          ql("Оплатить картой? Тесто: традиционное или тонкое?\n"
             "- Традиционное\n- Тонкое")
          == ["Тесто: традиционное или тонкое?", "- Традиционное",
              "- Тонкое"])
    check("пояснение нарушителя уходит с ним, а не губит следующий вопрос",
          ql("Оплатить картой? Сейчас выбрана оплата картой. Тесто?\n"
             "- Тонкое") == ["Тесто?", "- Тонкое"])
    check("пояснение-нарушитель после чистого вопроса — мимо, вопрос остаётся",
          ql("Тесто? Оплата — картой.") == ["Тесто?"])
    check("сокращение «ул.» не даёт обрывка («Доставить на ул.»)",
          ql("Размер: 25 или 30 см? Доставить на ул. Тестовая, оплатить "
             "картой?") == ["Размер: 25 или 30 см?"])
    check("телефон и код в одной строке с размером — размер остаётся",
          ql("Size: 25 or 30 cm? 30 cm is selected now. Your phone number? "
             "Code from SMS?")
          == ["Size: 25 or 30 cm? 30 cm is selected now."])
    check("одни нарушители — строка мимо вместе с вариантами",
          ql("Введите номер карты?\n- Visa\n- Мир\nТесто?")
          == ["Тесто?"])
    check("строки по отдельности — как раньше",
          ql("Размер?\nВведите номер карты?\n- Visa\nВаш телефон?\n"
             "Код из СМС?\nТесто?") == ["Размер?", "Тесто?"]
          and ql("Вступление:\n- Где?\n- А\n* Б\nтекст")
          == ["Где?", "- А", "- Б"])

    # Вопросы перед «В корзину» по ответу модели вызова 8: тесто спрошено,
    # второй список добавок код не добавляет
    SHOP = "https://pizzatest.test/"
    pctx = "Пепперони 30 см, традиционное тесто"
    md = lambda i, t, on=None: dict(
        {"idx": i, "tag": "button", "role": "radio" if on is not None
         else "button", "text": t, "md": 1, "ctx": pctx},
        **({"on": on} if on is not None else {}))
    window = [{"idx": 9, "tag": "button", "role": "button", "text": "Корзина",
               "aria": "Корзина"},
              md(1, "В корзину · 599 ₽"), md(2, "Маленькая 25 см", 0),
              md(3, "Средняя 30 см", 1), md(4, "Большая 35 см", 0),
              md(5, "Традиционное", 1), md(6, "Тонкое", 0),
              md(7, "Сырный бортик 169 ₽", 0), md(8, "Моцарелла 79 ₽", 0)]
    pgoal = "закажи пепперони 30 см на pizzatest.test"
    cc = real_cc({SHOP: window}, url=SHOP, base_dir=tmp)
    ag = agent(cc, SlotRouter({pgoal: {"site": "pizzatest.test", "items": [
        {"name": "Пепперони", "size": "30 см"}]}}, ask=call8))
    q = ag.start("q1", pgoal, ScriptedRouter([
        {"action": "click", "n": 1, "label": "В корзину · 599 ₽"},
        {"action": "fail", "message": "стоп"}]), user_id="A")
    check("перед «В корзину» человеку — вопрос о тесте и добавках модели, "
          "без второго списка добавок кода, ничего не нажато",
          "Тесто: традиционное или тонкое?" in q and "за доплату" in q
          and "что-нибудь из этого" not in q and not clicks(cc), q)

    # ── 3. Сводка при передаче оплаты ──
    print("передача оплаты:")
    CO = "https://pizzatest.test/checkout"
    pay_page = [
        {"idx": 1, "tag": "input", "role": "textbox", "text": "Адрес доставки",
         "ed": True},
        {"idx": 2, "tag": "button", "role": "button", "text": "Корзина 2",
         "aria": "Корзина"},
        {"idx": 3, "tag": "label", "role": "radio", "text": "Картой онлайн",
         "on": 1},
        {"idx": 4, "tag": "button", "role": "button",
         "text": "Оплатить 867 ₽"}]
    model_msg = ("Заказ собран на pizzatest.test: пепперони 30 см и кола "
                 "0,5 л. Скидка 50% применена.")
    ITEMS = [{"name": "пепперони", "size": "30 см",
              "options": ["традиционное тесто"]},
             {"name": "кола", "size": "0,5 л"}]
    PHONE = "+7 900 000-00-00"

    def at_checkout(chat, replies, lang=None, goal=None, adds=None,
                    slots=None, group=False):
        # Задача на оформлении: обе позиции в корзине, «что-нибудь ещё?» —
        # «нет»; адрес и телефон — ответы на приватной странице
        goal = goal or ("order a pepperoni and a cola on pizzatest.test"
                        if lang == "en"
                        else "закажи пепперони и колу на pizzatest.test")
        cc = real_cc({CO: [dict(x) for x in pay_page]}, url=CO, base_dir=tmp)
        if lang:
            cc.set_turn(chat, lang)
        ag = agent(cc, SlotRouter({goal: {"site": "pizzatest.test",
                                          "items": ITEMS}}))
        notes = []
        ag.on_private_text = lambda text, host, what: notes.append(text)
        ag.is_group = lambda c: group
        rt = ScriptedRouter([{"action": "ask", "question": "Куда доставить?"}]
                            + replies)
        ag.start(chat, goal, rt, user_id="A", lang=lang)
        run = ag._runs[chat]
        run.update(cart_adds=adds if adds is not None else [
            {"key": "Пепперони", "verified": True},
            {"key": "Кола", "verified": True}],
            more_no=True, more_n=2, cart_checked=True)
        for k, v in (slots if slots is not None else {
                "address": {"value": "ул. Тестовая, 1, кв. 2",
                            "src": "private"},
                "phone": {"value": PHONE, "src": "private"}}).items():
            run["brief"][k] = v
        r = ag.feed(chat, "ул. Тестовая, 1, кв. 2", rt, user_id="A")
        return cc, ag, r, notes

    cc, ag, r, notes = at_checkout("h1", [{"action": "done",
                                           "message": model_msg}])
    lines = r.splitlines()
    want_ru = ("Заказ на pizzatest.test: пепперони 30 см (традиционное "
               "тесто), кола 0,5 л; к оплате 867 ₽; адрес: ул. Тестовая, 1, "
               "кв. 2; телефон: ***(16).")
    check("done на оформлении: «Дошёл до оплаты», под ним сводка кодом",
          len(lines) >= 2
          and lines[-2].startswith("Дошёл до оплаты («Оплатить 867 ₽»)")
          and lines[-1] == want_ru, r)
    check("отчёт модели (и выдуманная «скидка») человеку не ушёл",
          "Скидка" not in r and model_msg not in r)
    check("телефон — маской, цифр номера в ответе нет",
          "000-00-00" not in r and "***(16)" in r)
    check("статус карточки — payment, ничего не нажато",
          (ag.task_card("h1") or {}).get("status") == "payment"
          and not clicks(cc))
    mem = json.loads(ag._memory_path.read_text(encoding="utf-8"))
    check("память: успешный исход; итог с приватной страницы — заглушкой "
          "(сводка с адресом в память не ушла)",
          mem["h1"][-1]["ok"] is True
          and "Тестовая" not in mem["h1"][-1]["result"])
    check("сводка с адресом/телефоном с приватной страницы — в историю бота "
          "заглушкой", any(want_ru in n for n in notes), f"{notes}")
    # Граница оплаты кода: модель жмёт «Оплатить 867 ₽» — та же сводка
    cc_b, ag_b, r_b, _n = at_checkout("h2", [
        {"action": "click", "n": 4, "label": "Оплатить 867 ₽"}])
    check("граница оплаты кода — та же сводка, ничего не нажато",
          r_b.splitlines()[-1] == want_ru and not clicks(cc_b)
          and (ag_b.task_card("h2") or {}).get("status") == "payment", r_b)
    # Английский ход
    _cc, ag_en, r_en, _n = at_checkout("h3", [{"action": "done",
                                               "message": model_msg}],
                                       lang="en")
    want_en = ("The order on pizzatest.test: пепперони 30 см (традиционное "
               "тесто), кола 0,5 л; to pay: 867 ₽; address: ул. Тестовая, 1, "
               "кв. 2; phone: ***(16).")
    check("en: передача и сводка по-английски (названия — как у человека)",
          r_en.splitlines()[-2].startswith("I've reached the payment")
          and r_en.splitlines()[-1] == want_en, r_en)
    # C3 не сошлось: пометка «посмотри корзину» — после сводки
    _cc, _ag, r_c3, _n = at_checkout("h4", [
        {"action": "done", "message": model_msg},
        {"action": "done", "message": model_msg}],
        adds=[{"key": "Пепперони", "verified": False},
              {"key": "Кола", "verified": False}])
    check("C3: сводка, затем «посмотри корзину»",
          r_c3.splitlines()[-2] == want_ru
          and "проверить не смог" in r_c3.splitlines()[-1], r_c3)
    # Недобавленное — в сводке
    _cc, _ag, r_m, _n = at_checkout("h5", [
        {"action": "click", "n": 4, "label": "Оплатить 867 ₽"}],
        adds=[{"key": "Пепперони", "verified": True}])
    check("в корзину попало не всё — сводка называет недостающее",
          "не положил в корзину: кола 0,5 л" in r_m.splitlines()[-1]
          and "пепперони 30 см" in r_m.splitlines()[-1], r_m)

    # Сводка напрямую: маски, группа, без суммы на кнопке, не заказ
    def run_of(**kw):
        run = {"goal": "закажи пиццу", "chat_id": "u1",
               "page_host": "www.pizzatest.test",
               "cart_adds": [{"key": "Пепперони", "verified": True}],
               "brief": {"items": [{"name": "Пепперони", "size": "30 см",
                                    "options": [], "qty": 2}]},
               "page_state": {"cart": {"count": 2, "sum": 1198.0,
                                       "cur": "₽"}}}
        run.update(kw)
        return run

    cc = real_cc({CO: pay_page}, url=CO, base_dir=tmp)
    ag = agent(cc)
    notes = []
    ag.on_private_text = lambda text, host, what: notes.append(text)
    s = ag._handoff_summary(run_of(brief={
        "items": [{"name": "Пепперони", "size": "30 см", "options": [],
                   "qty": 2}],
        "payment": {"value": "карта 4111 1111 1111 1111", "src": "user"},
        "address": {"value": "Ленина 5, ivan@example.com", "src": "user"},
        "time": {"value": "к 19:00", "src": "user"}}), "Перейти к оплате")
    check("номер карты и email в значениях — маской, адрес и время — как есть",
          s and "4111" not in s and "ivan@" not in s
          and "оплата: карта ***(19)" in s and "Ленина 5, ***(16)" in s
          and "время: к 19:00" in s, s)
    check("без суммы на кнопке — корзина из шапки; ×2 из брифа; www. снят",
          s and s.startswith("Заказ на pizzatest.test: Пепперони 30 см ×2;")
          and "в корзине на сайте: 2 шт., 1 198 ₽" in s, s)
    check("значения не с приватной страницы — без заметки в историю",
          not notes, f"{notes}")
    ag.is_group = lambda c: True
    s = ag._handoff_summary(run_of(brief={
        "items": [], "address": {"value": "ул. Тестовая, 1", "src": "private"},
        "phone": {"value": PHONE, "src": "private"},
        "time": {"value": "к 19:00", "src": "user"}}), "Оплатить 599 ₽")
    check("группа: адрес и телефон с приватной страницы не цитируются, "
          "остальное — да",
          s and "Тестовая" not in s and "телефон" not in s
          and "время: к 19:00" in s and "к оплате 599 ₽" in s and not notes,
          s)
    ag.is_group = lambda c: False
    check("цель не заказ — без сводки",
          ag._handoff_summary(run_of(goal="посмотри, что в корзине"),
                              "Оплатить 599 ₽") is None)
    check("задача ничего не положила и корзина пуста — без сводки (сумма и "
          "так в строке передачи)",
          ag._handoff_summary(run_of(cart_adds=[], brief={"items": []},
                                     page_state={}), "Оплатить 599 ₽")
          is None)
    check("лежавшее в корзине до задачи — в сводке",
          "лежало там до задачи" in (ag._handoff_summary(run_of(
              cart_pre={"count": 1, "sum": 399.0}), "Оплатить 998 ₽") or ""))
    check("сумма с кнопки «Pay $12.50» — целиком",
          "к оплате $12.50" in (ag._handoff_summary(run_of(), "Pay $12.50")
                                or ""))

    # ── 3б. Варианты позиции — нейтрально, без «+» ──
    # Стенд 21.10 (run3): «пепперони 30 см (+ традиционное)» в сводке
    # оплаты, плане и «В корзине: …» читалось как платная добавка, хотя
    # это выбранное тесто
    print("варианты позиции:")
    opt_brief = {"items": [
        {"name": "пепперони", "size": "30 см", "options": ["традиционное"],
         "qty": 1},
        {"name": "кола", "size": "0,5 л", "options": [], "qty": 1},
        {"name": "терияки", "options": ["тонкое", "моцарелла"], "qty": 2}]}
    opt_adds = [{"key": "Пепперони", "verified": True},
                {"key": "Кола", "verified": True}]
    check("позиция: варианты в скобках без «+», количество после",
          TaskAgent._item_text(opt_brief["items"][2])
          == "терияки (тонкое, моцарелла) ×2"
          and TaskAgent._item_text(opt_brief["items"][0])
          == "пепперони 30 см (традиционное)")
    s = ag._handoff_summary(run_of(brief=opt_brief, cart_adds=opt_adds),
                            "Оплатить 867 ₽")
    check("сводка оплаты: «пепперони 30 см (традиционное), кола 0,5 л», "
          "недостающее — так же",
          s and s.startswith("Заказ на pizzatest.test: пепперони 30 см "
                             "(традиционное), кола 0,5 л;")
          and "не положил в корзину: терияки (тонкое, моцарелла) ×2" in s
          and "(+" not in s, s)
    what, _missing = ag._added_items(run_of(brief=opt_brief,
                                            cart_adds=opt_adds))
    check("«В корзине: …» и состав подтверждения — та же запись",
          what == ["пепперони 30 см (традиционное)", "кола 0,5 л"]
          and ag._order_items(run_of(brief=opt_brief))[0]
          == "пепперони 30 см (традиционное)", f"{what}")
    for lang, want in (("ru", "положить «пепперони 30 см (традиционное)» в "
                              "корзину"),
                       ("en", "put “пепперони 30 см (традиционное)” in the "
                              "cart")):
        texts = [p["text"] for p in ag._plan_view(
            run_of(brief=opt_brief, cart_adds=opt_adds, lang=lang),
            "working")]
        check(f"план человеку ({lang}): «{want}», «+» нигде",
              want in texts and not any("(+" in t for t in texts), texts)
    lines = ag._plan_lines(run_of(brief=opt_brief, cart_adds=opt_adds,
                                  lang="ru"))
    check("план модели: put \"пепперони 30 см (традиционное)\" into the cart",
          any('put "пепперони 30 см (традиционное)" into the cart' in x
              for x in lines) and not any("(+" in x for x in lines), lines)

    # ── 4. Ключи текстов сводки ──
    print("ключи текстов:")
    for key in ("task_handoff_order", "task_handoff_order_any",
                "task_fact_missing", "task_fact_to_pay", "task_fact_phone"):
        row = cc_texts._T.get(key) or {}
        f = {lang: {n for _, n, _, _ in string.Formatter().parse(
            row.get(lang) or "") if n} for lang in ("ru", "en")}
        check(f"{key}: ru и en, те же плейсхолдеры, en без кириллицы",
              row.get("ru") and row.get("en") and f["ru"] == f["en"]
              and CYR.search(row["ru"]) and not CYR.search(row["en"]), row)

    print(f"\nИтого: {OK} проверок, {FAILS} провалов")
    return FAILS


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

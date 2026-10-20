"""Справочные ссылки про оплату и чтение страницы (стенд агента задач 20.10).

Проверяет:
* граница оплаты: ссылка подвала «Доставка и оплата» (и ru/en родственные
  заголовки справочных страниц) на этот же сайт — не оплата: risky_label,
  гейт execute (confirm_reason), needs_confirm, гейт маршрута (_nav_gate),
  агент задач нажимает её, а не передаёт «оплату» человеку;
* оплатой остаётся всё остальное: «Оплатить», «Оплатить 867 ₽», «Перейти к
  оплате», «Pay now», выбор способа оплаты, поля карты, платёжные бренды;
* пограничные случаи: «Доставка и оплата» кнопкой и <a role=button>, ссылка
  без адреса/javascript:, «Оплата» на /checkout/payment, адрес корзины/
  оформления/платёжной страницы/шлюза/другого сайта, aria «Оплатить»,
  действие без фактов ссылки (шаг сценария, фраза человека);
* чтение (_READ_PAGE_JS/_READ_TASK_JS): скрытый или пустой article/main —
  видимый непустой контейнер или body (headless Chromium Playwright,
  set_content без сети; нет Playwright — SKIP этой части).

Запуск: python3 -m scripts.test_payment_info_links
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _item(text, href, tag="a", role="", aria=None, idx=7):
    it = {"idx": idx, "tag": tag, "role": role, "text": text, "href": href,
          "vp": True}
    if aria:
        it["aria"] = aria
    return it


def _click(item, host, origin=None):
    # Действие клика так, как его собирают резолвер (_with_labels) и агент
    from app.features.computer_control import ComputerControlManager as M
    a = {"kind": "click", "idx": item["idx"], "element": item["text"],
         "host": host, "value": f"https://{host}/"}
    if origin:
        a["origin"] = origin
    return M._with_labels(a, item)


def _browser_checks(check):
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"SKIP чтения: нет playwright ({e})")
        return
    from app.features import browser_actions as ba
    pw = sync_playwright().start()
    browser = None
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception as e:
        # Версия пакета и скачанного браузера разошлись — любой headless-shell
        # из кэша Playwright (как в test_ba_sandbox)
        import glob
        exes = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright"
                                    / "chromium_headless_shell-*/*/"
                                    "chrome-headless-shell"))
                      + glob.glob(str(Path.home() / ".cache/ms-playwright"
                                      / "chromium_headless_shell-*/*/"
                                      "chrome-headless-shell")))
        for exe in reversed(exes):
            try:
                browser = pw.chromium.launch(headless=True,
                                             executable_path=exe)
                break
            except Exception:
                continue
        if browser is None:
            pw.stop()
            print(f"SKIP чтения: headless Chromium не запускается "
                  f"({str(e)[:120]})")
            return
    try:
        page = browser.new_page(viewport={"width": 1000, "height": 700})

        def both(html):
            page.set_content(html)
            return (page.evaluate(ba._READ_PAGE_JS),
                    page.evaluate(ba._READ_TASK_JS))

        info = ("<header><a href='/'>Пицца Тест</a></header><div class='page'>"
                "<h1>Доставка и оплата</h1><h3>Условия доставки</h3><ul><li>"
                "Бесплатно от 799 ₽</li></ul><h3>Время работы</h3><p>Принимаем "
                "заказы с 10:00 до 23:00.</p></div><footer><a href='/delivery'>"
                "Доставка и оплата</a></footer>")
        # Стенд 20.10: скрытый пустой <article> на служебной странице
        for name, hidden in (
                ("display:none пустой", "<article style='display:none'>"
                                        "</article>"),
                ("hidden пустой", "<article hidden></article>"),
                ("видимый пустой", "<article></article>"),
                ("видимый из пробелов", "<article>  \n </article>"),
                ("пустой main", "<main><div></div></main>")):
            p, t = both(hidden + info)
            check(f"чтение: {name} article/main перед текстом — body "
                  "(текст страницы есть, не «нет текста»)",
                  all("Бесплатно от 799" in x and "23:00" in x
                      for x in (p, t)))
        # Скрытый article с текстом: innerText скрытого — его textContent,
        # раньше read отдавал невидимый старый текст
        p, t = both("<article style='display:none'>СТАРЫЙ-СКРЫТЫЙ</article>"
                    + info)
        check("чтение: скрытый article с текстом — не он, а body",
              all("СТАРЫЙ-СКРЫТЫЙ" not in x and "Бесплатно от 799" in x
                  for x in (p, t)))
        p, t = both("<div style='display:none'><main>В-СКРЫТОМ-ПРЕДКЕ</main>"
                    "</div>" + info)
        check("чтение: main внутри скрытого предка — не он, а body",
              all("В-СКРЫТОМ-ПРЕДКЕ" not in x and "23:00" in x
                  for x in (p, t)))
        p, t = both("<main style='visibility:hidden'>НЕВИДИМЫЙ</main>" + info)
        check("чтение: main с visibility:hidden — body",
              all("НЕВИДИМЫЙ" not in x and "23:00" in x for x in (p, t)))
        # Несколько кандидатов — лучший видимый непустой
        p, t = both("<main hidden>МОБИЛЬНЫЙ-СКРЫТЫЙ</main><main><h1>Меню</h1>"
                    "<p>Маргарита 499 ₽</p></main><footer>ПОДВАЛ</footer>")
        check("чтение: из двух main — видимый (скрытый и подвал не читаются)",
              all("Маргарита 499" in x and "МОБИЛЬНЫЙ" not in x
                  and "ПОДВАЛ" not in x for x in (p, t)))
        p, t = both("<article><p>Коротко</p></article><article><h1>Статья"
                    "</h1><p>Длинный основной текст статьи про доставку."
                    "</p></article><footer>ПОДВАЛ</footer>")
        check("чтение: без main из нескольких article — с самым длинным "
              "текстом", all("Длинный основной текст" in x
                             and "ПОДВАЛ" not in x for x in (p, t)))
        p, t = both("<nav>НАВИГАЦИЯ</nav><main><h1>Каталог</h1><article>"
                    "Пепперони 599 ₽</article><article>Маргарита 499 ₽"
                    "</article></main><footer>ПОДВАЛ</footer>")
        check("чтение: каталог — main целиком (все карточки article), как "
              "раньше", all("Пепперони 599" in x and "Маргарита 499" in x
                            and "НАВИГАЦИЯ" not in x and "ПОДВАЛ" not in x
                            for x in (p, t)))
        p, t = both("<main><p>Основное</p></main><article>" + "Сео-текст " * 50
                    + "</article>")
        check("чтение: видимый непустой main — первым, даже если article "
              "вне его длиннее", all(x.strip() == "Основное" for x in (p, t)))
        p, t = both("<div role='main'><p>Роль main</p></div><footer>ПОДВАЛ"
                    "</footer>")
        check("чтение: [role=main] — как main",
              all(x.strip() == "Роль main" for x in (p, t)))
        p, t = both("<p>Голая страница без контейнеров</p>")
        check("чтение: контейнеров нет — body",
              all("Голая страница" in x for x in (p, t)))
        # Агент: открытое окно по-прежнему первым
        page.set_content(
            "<article style='display:none'></article>" + info
            + "<div role=dialog style='position:fixed;top:0;left:0;"
            "width:400px;height:300px;background:#fff'>Окно: Пепперони 30 см"
            "</div>")
        t = page.evaluate(ba._READ_TASK_JS)
        check("чтение агента: открытое окно — первым (скрытый article не "
              "мешает)", t.startswith("Окно: Пепперони"))
    finally:
        browser.close()
        pw.stop()


def main():
    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    from app.features.computer_control import (ComputerControlManager as M,
                                                NeedsConfirm, _is_payment,
                                                link_facts, payment_info_link)

    # ── 1. Справочные ссылки — не оплата ──
    info_links = [
        ("Доставка и оплата", "https://pizzatest.ru/delivery", "pizzatest.ru"),
        ("Оплата и доставка", "https://shop.ru/oplata-i-dostavka/", "shop.ru"),
        ("Способы оплаты", "https://shop.ru/payment-methods", "shop.ru"),
        ("Условия оплаты", "https://shop.ru/info/oplata", "shop.ru"),
        ("Условия доставки и оплаты", "https://www.shop.ru/usloviya/",
         "shop.ru"),
        ("Информация об оплате", "https://shop.ru/pages/oplata", "shop.ru"),
        ("Delivery & payment", "https://shop.com/delivery-and-payment",
         "shop.com"),
        ("Payment methods", "https://shop.com/pages/payment-methods",
         "shop.com"),
        ("Shipping and payment", "https://shop.com/shipping-payment",
         "shop.com"),
        ("Payment information", "https://shop.com/help/payment-information",
         "shop.com"),
        # подпись с хвостом-стрелкой, капсом, кириллица вперемешку с латиницей
        ("Доставка и оплата →", "https://help.shop.ru/dostavka", "www.shop.ru"),
        ("ДОСТАВКА И ОПЛАТА", "https://shop.ru/%D0%B4%D0%BE%D1%81%D1%82%D0%B0"
         "%D0%B2%D0%BA%D0%B0", "shop.ru"),
        ("Дoстaвкa и оплата", "https://shop.ru/delivery", "shop.ru"),
    ]
    for lab, href, host in info_links:
        a = _click(_item(lab, href), host)
        check(f"справочная ссылка «{lab}» → {href.split('://')[1][:40]}: "
              "не оплата", payment_info_link(a)
              and M.risky_label(a) is None
              and M.confirm_reason(dict(a, origin="task")) is None)
    for role in ("link", "menuitem"):
        a = _click(_item("Доставка и оплата", "https://shop.ru/delivery",
                         role=role), "shop.ru")
        check(f"<a role={role}> «Доставка и оплата» — не оплата",
              M.risky_label(a) is None)
    check("ссылка без оплаты в подписи — действие не меняется (фактов "
          "ссылки нет)",
          link_facts(_item("Меню", "https://shop.ru/menu")) is None
          and "link" not in _click(_item("Меню", "https://shop.ru/menu"),
                                   "shop.ru"))

    # ── 2. Оплата остаётся оплатой ──
    pay_buttons = ["Оплатить", "Оплатить 867 ₽", "Перейти к оплате",
                   "Pay now", "Оплатить картой", "Картой онлайн", "СБП",
                   "Visa", "Mir Pay", "Proceed to payment", "Способ оплаты"]
    for lab in pay_buttons:
        check(f"кнопка «{lab}» — оплата",
              M.risky_label(_click(_item(lab, "", tag="button",
                                         role="button"), "shop.ru"))
              == "payment")
        check(f"ссылка «{lab}» (адрес справочный) — оплата: подпись не "
              "заголовок справки",
              M.risky_label(_click(_item(lab, "https://shop.ru/delivery"),
                                   "shop.ru")) == "payment")
    for field in ("Номер карты", "CVV", "Card number", "Срок действия карты"):
        check(f"поле «{field}» — оплата",
              M.risky_label({"kind": "type", "element": field, "text": "1",
                             "host": "shop.ru"}) == "payment")
    check("выбор способа оплаты на оформлении (радио «Картой курьеру») — "
          "оплата", M.risky_label(_click(_item(
              "Картой курьеру", "", tag="label"), "shop.ru")) == "payment")

    # ── 3. Пограничные случаи ──
    edge = [
        ("«Доставка и оплата» кнопкой",
         _item("Доставка и оплата", "", tag="button", role="button")),
        ("«Доставка и оплата» <a role=button>",
         _item("Доставка и оплата", "https://shop.ru/delivery", role="button")),
        ("«Доставка и оплата» вкладкой (role=tab)",
         _item("Доставка и оплата", "https://shop.ru/delivery", role="tab")),
        ("«Доставка и оплата» <a> без адреса (JS-обработчик)",
         _item("Доставка и оплата", "")),
        ("«Доставка и оплата» href=javascript:",
         _item("Доставка и оплата", "javascript:void(0)")),
        ("ссылка «Оплата» на /checkout/payment",
         _item("Оплата", "https://shop.ru/checkout/payment")),
        ("«Доставка и оплата» на шаг оформления /checkout?step=delivery",
         _item("Доставка и оплата", "https://shop.ru/checkout?step=delivery")),
        ("«Доставка и оплата» на /cart",
         _item("Доставка и оплата", "https://shop.ru/cart")),
        ("«Доставка и оплата» на /personal/order/make (Битрикс)",
         _item("Доставка и оплата", "https://shop.ru/personal/order/make/")),
        ("«Способы оплаты» на платёжную страницу /payment",
         _item("Способы оплаты", "https://shop.ru/payment")),
        ("«Способы оплаты» на /oplata без слова справки",
         _item("Способы оплаты", "https://shop.ru/oplata")),
        ("«Способы оплаты» на /order/pay",
         _item("Способы оплаты", "https://shop.ru/order/pay")),
        ("«Payment information» на маршрут SPA #payment",
         _item("Payment information", "https://shop.ru/faq#payment")),
        ("«Способы оплаты» на платёжный шлюз (другой сайт)",
         _item("Способы оплаты", "https://yookassa.ru/methods")),
        ("«Доставка и оплата» на другой сайт",
         _item("Доставка и оплата", "https://other.ru/delivery")),
        ("«Способы оплаты» на платёжный поддомен pay.",
         _item("Способы оплаты", "https://pay.shop.ru/methods")),
        ("«Доставка и оплата» с aria «Оплатить заказ»",
         _item("Доставка и оплата", "https://shop.ru/delivery",
               aria="Оплатить заказ")),
        ("«Доставка и оплата и Оплатить» — подпись не целиком",
         _item("Доставка и оплата и Оплатить", "https://shop.ru/delivery")),
    ]
    for name, it in edge:
        check(f"пограничный: {name} — оплата",
              M.risky_label(_click(it, "shop.ru")) == "payment"
              and M.confirm_reason(_click(it, "shop.ru", origin="task"))
              == "payment")
    a = _click(_item("Доставка и оплата", "https://shop.ru/delivery"),
               "shop.ru")
    check("пограничный: страница без хоста — оплата",
          M.risky_label(dict(a, host=None)) == "payment")
    check("пограничный: координатный клик (vision) — оплата",
          M.risky_label(dict(a, point={"x": 1, "y": 2})) == "payment")
    check("без фактов ссылки (шаг сценария, фраза человека) — оплата",
          M.risky_label({"kind": "click", "element": "Доставка и оплата",
                         "host": "shop.ru"}) == "payment"
          and _is_payment("Доставка и оплата"))
    from app.features.scenario_manager import ScenarioManager
    cut = ScenarioManager._strip_payment([
        {"op": "open", "url": "https://shop.ru/"},
        {"op": "click", "target": "Доставка и оплата", "host": "shop.ru"}])
    check("записанный шаг сценария «Доставка и оплата» (без адреса ссылки) "
          "по-прежнему отрезается", cut[-1]["op"] == "handoff")

    # ── 4. Гейты режима управления ──
    m = M(context="pil", config={"confirm": False},
          base_dir=Path(tempfile.mkdtemp(prefix="pil_")))
    info_it = _item("Доставка и оплата", "https://shop.ru/delivery")
    btn_it = _item("Оплатить", "", tag="button", role="button")
    check("needs_confirm: справочная ссылка — по политике кликов (без «да»), "
          "«Оплатить» — с «да»",
          m.needs_confirm(_click(info_it, "shop.ru")) is False
          and m.needs_confirm(_click(btn_it, "shop.ru")) is True)
    ctx = {"host": "shop.ru", "done": [], "tab_id": None,
           "url": "https://shop.ru/", "idx": 7}
    nav = {"kind": "nav", "value": "https://shop.ru/", "host": "shop.ru",
           "steps": ["Доставка и оплата"]}

    def gate(it):
        try:
            m._nav_gate(dict(nav), 0, "Доставка и оплата", it, None, ctx)
            return None
        except NeedsConfirm as e:
            return e.info.get("reason")
    check("гейт маршрута: найденная справочная ссылка — проходит, та же "
          "подпись кнопкой — стоп (оплата)",
          gate(info_it) is None
          and gate(_item("Доставка и оплата", "", tag="button",
                         role="button")) == "payment")

    # ── 5. Агент задач на настоящем гейте ──
    from scripts.test_task_agent import ScriptedRouter, real_cc
    from app.features import browser_actions as _ba
    _ba.wait_dom_idle = lambda *a, **k: None
    _ba.section_items = lambda *a, **k: None
    _ba.section_names = lambda *a, **k: None
    _ba.scroll_step = lambda *a, **k: {"moved": True, "bottom": False}
    from app.features import cc_privacy as _cp
    _cp.PrivateRouter.get_response = lambda self, msgs, **kw: None
    from app.features.task_agent import TaskAgent

    def run_agent(it, url="https://shop.test/", after=None):
        pages = {url: [dict(it)], "https://shop.test/delivery": [
            {"idx": 3, "tag": "a", "role": "", "text": "Меню",
             "href": "https://shop.test/menu"}]}
        cc = real_cc(pages, url=url, base_dir=Path(tempfile.mkdtemp(
            prefix="pil_ta_")), clicks={it["idx"]: after} if after else None)
        agent = TaskAgent(cc)
        agent.base_dir = cc.base_dir
        reply = agent.start("pil", "x", ScriptedRouter([
            {"action": "click", "n": 1, "label": it["text"]},
            {"action": "fail", "message": "стоп"}]), user_id="A")
        clicks = [d for d in cc.dispatched if d.get("kind") == "click"]
        return reply, clicks, cc
    reply, clicks, cc = run_agent(
        _item("Доставка и оплата", "https://shop.test/delivery"),
        after="https://shop.test/delivery")
    check("агент: «Доставка и оплата» ссылкой подвала — нажата, задача не "
          "снята «оплатой»",
          len(clicks) == 1 and clicks[0]["idx"] == 7
          and "дальше сам" not in reply and cc.cur == "https://shop.test/delivery")
    for name, it in (
            ("«Доставка и оплата» кнопкой",
             _item("Доставка и оплата", "", tag="button", role="button")),
            ("ссылка «Оплата» на /checkout/payment",
             _item("Оплата", "https://shop.test/checkout/payment")),
            ("«Оплатить 867 ₽»",
             _item("Оплатить 867 ₽", "", tag="button", role="button")),
            ("«Перейти к оплате» ссылкой",
             _item("Перейти к оплате", "https://shop.test/delivery"))):
        reply, clicks, _ = run_agent(it)
        check(f"агент: {name} — граница оплаты, клика нет",
              not clicks and "дальше сам" in reply)

    _browser_checks(check)

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

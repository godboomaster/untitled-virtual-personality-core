"""JS браузерного слоя на настоящем движке: собственный headless-Chromium
Playwright с локальными страницами (set_content) — без сети и без браузеров
пользователя (CDP :9222/:9223 не трогаем). Нет Playwright/Chromium — SKIP.

Проверяет (починка агента задач по аудиту 29.09):
* A1 — авто-закрытие оверлея: строгий режим (агент) жмёт только cookie/
  consent/gdpr; общий режим не жмёт «ОК» в диалоге заказа/удаления;
* D1/A3/A5/D6/D7 — флаги снимка on/sub/fm/dis/qs/sn доходят до _parse_snapshot;
* A6 — подпись сверяется в момент клика/ввода (переиспользованный узел не
  нажимается); D4 — force-клик сквозь чужой слой не делается;
* D5 — клик-пустышка не засчитывается по одному фокусу;
* D6 — маска телефона «+7 (___)»: 10 цифр — «filled», +7… — честная ошибка;
* D8 — чтение для агента: шторка корзины первой, итог внизу не обрезан;
* страж Enter фоновой вкладки (03.10, цикл клавиши headless Chrome): страж
  ставится до Enter и снимается после (и при упавшем dispatch), сбои
  установки/снятия отправку не роняют; на настоящей странице — кто поглотил
  Enter (сайт/страж/поле), Enter с символом в поле чата не отменяется
  (keypress duck.ai доходит), вне поля, в readOnly и при stopPropagation
  сайта — отменяется; после снятия следующий Enter не трогается.

Запуск: python -m scripts.test_ba_sandbox
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class _W:
    # Минимальный воркер для _click_cdp/_fill_cdp: одна страница песочницы
    def __init__(self, page):
        self.page = page

    def page_for(self, host, tab_id=None):
        return self.page

    def _all_pages(self):
        return [self.page]


class _LogRec(logging.Handler):
    # Сообщения логгера browser_actions — проверка диагностики стража
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.msgs = []

    def emit(self, record):
        self.msgs.append((record.levelno, record.getMessage()))


def _guard_checks(ba, page, cdp, check):
    """Страж Enter фоновой вкладки (см. _ENTER_GUARD_JS): порядок вызовов на
    подменах, затем НАСТОЯЩИЕ _raw_enter/_raw_eval/_raw_chat_fill_send на
    странице песочницы — подменён только транспорт _raw_tab_call (CDP-сессия
    Playwright вместо сокета пула). Сам цикл клавиш headless-shell Playwright
    не воспроизводит (он только у Chrome --headless=new на macOS — проверка
    на отдельном Chrome), здесь — логика стража в движке Blink."""
    blog = logging.getLogger(ba.__name__)
    rec = _LogRec()
    blog.addHandler(rec)
    old_level = blog.level
    blog.setLevel(logging.DEBUG)
    _rtc, _rev = ba._raw_tab_call, ba._raw_eval
    try:
        # ── порядок вызовов на подменах ──
        calls = []
        cfg = {"fail": None, "off": "guard"}

        def fake_eval(tab, js, timeout_sec=None):
            kind = ("on" if "'armed'" in js
                    else "off" if js == ba._ENTER_GUARD_OFF_JS else "js")
            calls.append(kind if kind != "on" else ("on", js))
            if cfg["fail"] == kind:
                raise ba.BrowserUnavailable(f"имитация сбоя {kind}")
            return "armed" if kind == "on" else cfg["off"]

        def fake_call(tab, method, params=None, timeout=None):
            calls.append(params["type"])
            if cfg["fail"] == "dispatch":
                raise ba.RawCallTimeout("имитация: dispatch не ответил")
            return {}

        def kinds():
            return [c[0] if isinstance(c, tuple) else c for c in calls]

        ba._raw_eval, ba._raw_tab_call = fake_eval, fake_call
        ba._raw_enter(1)
        raw_order, raw_js = kinds(), calls[0][1]
        calls.clear()
        ba._raw_enter(1, with_text=True, input_sel="textarea[name=user-prompt]")
        txt_order, txt_js = kinds(), calls[0][1]
        check("страж Enter: установка ДО Enter, снятие ПОСЛЕ keyUp (rawKeyDown "
              "и keyDown с символом); в страж уходят with_text и селектор поля",
              raw_order == ["on", "rawKeyDown", "keyUp", "off"]
              and txt_order == ["on", "keyDown", "keyUp", "off"]
              and raw_js.endswith("(false,\"\")")
              and txt_js.endswith("(true,\"textarea[name=user-prompt]\")"))
        calls.clear()
        cfg["fail"] = "dispatch"
        raised = None
        try:
            ba._raw_enter(1)
        except ba.RawCallTimeout as e:
            raised = e
        check("страж Enter: dispatch упал — страж всё равно снят, исключение "
              "dispatch'а уходит наверх",
              kinds() == ["on", "rawKeyDown", "off"] and raised is not None)
        calls.clear()
        cfg["fail"] = "on"
        try:
            ba._raw_enter(1)
            on_fail_ok = True
        except Exception:
            on_fail_ok = False
        check("страж Enter: сбой установки — Enter всё равно отправлен (как до "
              "стража), снятие всё равно пробуется",
              on_fail_ok and kinds() == ["on", "rawKeyDown", "keyUp", "off"])
        calls.clear()
        cfg["fail"] = "off"
        try:
            ba._raw_enter(1)
            off_fail_ok = True
        except Exception:
            off_fail_ok = False
        check("страж Enter: сбой снятия отправку не роняет",
              off_fail_ok and kinds() == ["on", "rawKeyDown", "keyUp", "off"])
        cfg["fail"] = None
        infos = []
        for res in ("guard", "site", "field", "unfired", ""):
            cfg["off"] = res
            rec.msgs.clear()
            ba._raw_enter(1)
            infos.append(any(lv == logging.INFO for lv, _m in rec.msgs))
        check("страж Enter: в лог INFO — только «поглотил страж» и «не дошёл "
              "до стража» (сайт/поле/смена документа — DEBUG)",
              infos == [True, False, False, True, False])
        cfg["off"] = "guard"
        rec.msgs.clear()
        ba._raw_enter(1)
        check("страж Enter: «поглотил страж» — с номером вкладки",
              any(lv == logging.INFO and "#1" in m and "страж" in m
                  for lv, m in rec.msgs))

        # ── настоящий JS стража на странице песочницы ──
        ba._raw_tab_call = (lambda tab, method, params=None, timeout=None:
                            cdp.send(method, params or {}))
        ba._RAW_TABS[1] = {"targetId": "t", "sessionId": "s", "pool": "h"}
        offs = []

        def rec_eval(tab, js, timeout_sec=None):
            r = _rev(tab, js, timeout_sec=timeout_sec)
            if js == ba._ENTER_GUARD_OFF_JS:
                offs.append(r)
            return r
        ba._raw_eval = rec_eval

        def run(html, with_text, sel, prep="t.focus();t.value='hello'"):
            # Глобалы окна переживают set_content (document.open оставляет
            # тот же Window) — счётчик прошлой страницы обнуляем заранее
            page.evaluate("()=>{window.sent=null;}")
            page.set_content(html)
            page.evaluate("()=>{var t=document.getElementById('t');" + prep + "}")
            offs.clear()
            ba._raw_enter(1, with_text=with_text, input_sel=sel)
            page.wait_for_timeout(100)
            st = page.evaluate(
                "()=>[document.getElementById('t').value, window.sent,"
                "window.__vpcEnterGuard||null]")
            return (offs[-1] if offs else None), st[0], st[1], st[2]

        bare = "<textarea id='t'></textarea>"
        r = run(bare, False, "#t")
        check("страж (Blink): rawKeyDown в поле без обработчика сайта — "
              "поглотил страж, страж снят", r == ("guard", "hello", None, None))
        r = run(bare, True, "#t")
        check("страж (Blink): Enter с символом в поле чата не отменён — "
              "перевод строки вставлен (поле поглотило), итог field",
              r == ("field", "hello\n", None, None))
        r = run(bare, True, "#other")
        check("страж (Blink): Enter с символом, фокус НЕ в поле чата — "
              "отменён на keydown (символ не вставлен), итог guard",
              r == ("guard", "hello", None, None))
        r = run("<textarea id='t' readonly>hello</textarea>", True, "#t",
                prep="t.focus()")
        check("страж (Blink): readOnly-поле — не «рабочее», Enter отменён",
              r == ("guard", "hello", None, None))
        r = run("<p>нет поля</p><textarea id='t' style='display:none'>"
                "hello</textarea>", True, "#t", prep="document.body.focus()")
        check("страж (Blink): фокус на body, Enter с символом — отменён",
              r[0] == "guard")
        site_kd = ("<textarea id='t'></textarea><script>var sent=0;"
                   "document.getElementById('t').addEventListener('keydown',"
                   "function(e){if(e.key==='Enter'){e.preventDefault();sent++;"
                   "this.value='';}});</script>")
        r = run(site_kd, False, "#t")
        check("страж (Blink): сайт отправил по keydown с preventDefault — "
              "обработчик сайта сработал, итог site",
              r == ("site", "", 1, None))
        stop = ("<textarea id='t'></textarea><script>var sent=0;"
                "var t=document.getElementById('t');"
                "t.addEventListener('keydown',function(e){if(e.key==='Enter')"
                "e.stopPropagation();});"
                "t.addEventListener('keypress',function(e){if(e.key==='Enter')"
                "{e.preventDefault();sent++;this.value='';}});</script>")
        r = run(stop, True, "#other")
        check("страж (Blink): сайт остановил всплытие keydown без "
              "preventDefault — страж отменил его микрозадачей (keypress не "
              "дошёл), итог guard", r == ("guard", "hello", 0, None))
        r = run(stop, True, "#t")
        check("страж (Blink): та же страница, фокус в поле чата — keypress "
              "сайта дошёл (отправка duck.ai не сломана), итог field",
              r == ("field", "", 1, None))
        # Синтетический keydown сайта стража не расходует (isTrusted)
        page.set_content(bare)
        _rev(1, ba._ENTER_GUARD_JS % ("false", "\"#t\""))
        page.evaluate("()=>window.dispatchEvent(new KeyboardEvent('keydown',"
                      "{key:'Enter',bubbles:true,cancelable:true}))")
        armed = page.evaluate("()=>typeof window.__vpcEnterGuard")
        off = _rev(1, ba._ENTER_GUARD_OFF_JS)
        check("страж (Blink): синтетический keydown сайта стража не "
              "расходует; снятие несработавшего — unfired",
              armed == "function" and off == "unfired"
              and page.evaluate("()=>window.__vpcEnterGuard") is None)
        # После упавшего dispatch страж снят: следующий Enter (как печатает
        # человек в видимом окне rescue) не отменяется
        page.set_content(bare)
        page.evaluate("()=>{var t=document.getElementById('t');t.focus();"
                      "t.value='a';window.late=[];}")
        ba._raw_tab_call = (lambda tab, method, params=None, timeout=None:
                            (_ for _ in ()).throw(ba.RawCallTimeout("имитация"))
                            if method == "Input.dispatchKeyEvent"
                            else cdp.send(method, params or {}))
        try:
            ba._raw_enter(1, input_sel="#t")
        except ba.RawCallTimeout:
            pass
        ba._raw_tab_call = (lambda tab, method, params=None, timeout=None:
                            cdp.send(method, params or {}))
        page.evaluate("()=>addEventListener('keydown',function(e){"
                      "if(e.key==='Enter')late.push(e.defaultPrevented);})")
        for ev in ({"type": "keyDown", "key": "Enter", "code": "Enter",
                    "windowsVirtualKeyCode": 13, "text": "\r"},
                   {"type": "keyUp", "key": "Enter", "code": "Enter",
                    "windowsVirtualKeyCode": 13}):
            cdp.send("Input.dispatchKeyEvent", ev)
        page.wait_for_timeout(100)
        check("страж (Blink): dispatch упал — страж снят, следующий Enter "
              "не отменён (перевод строки вставлен)",
              offs[-1:] == ["unfired"]
              and page.evaluate("()=>[late, document.getElementById('t')"
                                ".value, window.__vpcEnterGuard||null]")
              == [[False], "a\n", None])
        # Настоящий _raw_chat_fill_send (duck.ai-подобная страница: отправка
        # по keypress) — селектор поля доходит до стража, отправка проходит
        page.set_content(
            "<textarea id='t'></textarea><script>var sent=0;"
            "document.getElementById('t').addEventListener('keypress',"
            "function(e){if(e.key==='Enter'){e.preventDefault();sent++;"
            "this.value='';}});</script>")
        seen = {}
        _re = ba._raw_enter

        def spy_enter(tab, with_text=False, input_sel=None):
            seen.update(with_text=with_text, input_sel=input_sel)
            return _re(tab, with_text=with_text, input_sel=input_sel)
        ba._raw_enter = spy_enter
        offs.clear()
        try:
            res = ba._raw_chat_fill_send(1, "#t", "привет", enter_text=True)
        except Exception as e:
            res = f"{type(e).__name__}: {e}"
        finally:
            ba._raw_enter = _re
        check("страж: _raw_chat_fill_send передаёт селектор поля в _raw_enter; "
              "duck.ai-подобная отправка по keypress — sent",
              res == "sent" and seen == {"with_text": True, "input_sel": "#t"}
              and offs == ["field"] and page.evaluate("()=>sent") == 1)
    finally:
        ba._raw_tab_call, ba._raw_eval = _rtc, _rev
        ba._RAW_TABS.pop(1, None)
        blog.removeHandler(rec)
        blog.setLevel(old_level)


def main():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"SKIP: нет playwright ({e})")
        return 0
    from app.features import browser_actions as ba
    ba.CLICK_VERIFY_SEC = 1.0

    ok = fails = 0

    def check(name, cond):
        nonlocal ok, fails
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            fails += 1

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception as e:
        # Версия пакета и скачанного браузера разошлись — любой headless-shell
        # из кэша Playwright
        import glob
        exes = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright"
                                    / "chromium_headless_shell-*/*/"
                                    "chrome-headless-shell"))
                      + glob.glob(str(Path.home() / ".cache/ms-playwright"
                                      / "chromium_headless_shell-*/*/"
                                      "chrome-headless-shell")))
        browser = None
        for exe in reversed(exes):
            try:
                browser = pw.chromium.launch(headless=True, executable_path=exe)
                break
            except Exception:
                continue
        if browser is None:
            pw.stop()
            print(f"SKIP: headless Chromium не запускается ({str(e)[:120]})")
            return 0
    try:
        page = browser.new_page(viewport={"width": 1000, "height": 700})
        w = _W(page)

        def parsed():
            raw = str(page.evaluate(ba._js_fill(
                ba._SNAPSHOT_JS, BASE=ba._mark_base(ba.SNAPSHOT_MAX))) or "")
            return ba._parse_snapshot(raw)[1]

        def by_text(items, t):
            return next(i for i in items if i["text"].startswith(t))

        # ── A1: авто-закрытие оверлея ──
        dialogs = {
            "заказ": "<div role=dialog style='position:fixed;top:0;width:300px;"
                     "height:200px'><p>Подтвердите заказ на 1 299 ₽</p>"
                     "<button onclick=\"window.hit=1\">ОК</button></div>",
            "удаление": "<div role=dialog style='position:fixed;top:0;"
                        "width:300px;height:200px'><p>Удалить товар?</p>"
                        "<button onclick=\"window.hit=1\">OK</button></div>",
            "город": "<div role=dialog style='position:fixed;top:0;width:300px;"
                     "height:200px'><p>Ваш город Москва?</p>"
                     "<button onclick=\"window.hit=1\">Хорошо</button></div>",
            "cookie": "<div class='cookie-banner' style='position:fixed;"
                      "bottom:0;width:600px;height:80px'><p>Мы используем "
                      "cookies</p><button onclick=\"window.hit=1\">Принять"
                      "</button></div>",
        }
        res = {}
        for name, html in dialogs.items():
            for strict in (False, True):
                page.set_content(html)
                page.evaluate("window.hit=0")
                page.evaluate(ba._js_fill(ba._DISMISS_OVERLAY_JS,
                                          STRICT=strict))
                res[(name, strict)] = page.evaluate("window.hit") == 1
        check("A1: агент (строгий режим) жмёт только cookie-баннер",
              res[("cookie", True)] and not res[("город", True)]
              and not res[("заказ", True)] and not res[("удаление", True)])
        check("A1: общий режим не жмёт «ОК» в диалоге заказа/удаления",
              not res[("заказ", False)] and not res[("удаление", False)]
              and res[("город", False)] and res[("cookie", False)])
        # Красная команда: «consent»/«banner» в классе, а внутри — деньги
        money_boxes = {
            "autopay-consent": "<div class='autopay-consent' style='position:"
                               "fixed;bottom:0;width:600px;height:80px'>"
                               "Подключить автоплатёж 299 ₽ в месяц "
                               "<button onclick=\"window.hit=1\">Согласен"
                               "</button></div>",
            "order-banner": "<div class='order-banner' style='position:fixed;"
                            "top:0;width:600px;height:120px'>Подтвердите "
                            "заказ на 1 299 ₽ <button onclick=\"window.hit=1"
                            "\">ОК</button></div>",
            "subscribe-modal": "<div class='subscribe-modal' role=dialog "
                               "style='position:fixed;top:0;width:400px;"
                               "height:200px'>Premium 299 ₽/мес <button "
                               "onclick=\"window.hit=1\">Хорошо</button></div>"}
        clicked = []
        for name, html in money_boxes.items():
            for strict in (False, True):
                page.set_content(html)
                page.evaluate("window.hit=0")
                page.evaluate(ba._js_fill(ba._DISMISS_OVERLAY_JS,
                                          STRICT=strict))
                if page.evaluate("window.hit") == 1:
                    clicked.append((name, strict))
        check("A1: окно «consent/banner» про деньги/заказ/подписку — согласие "
              "не жмётся ни в каком режиме", clicked == [])

        # Финальная проверка C1/M2: смысл — в тексте всего окна; кнопка
        # формы не жмётся; cookie-баннер с «in order to» по-прежнему да
        FIX = ("position:fixed;top:0;left:0;width:500px;height:300px;"
               "background:#fff")
        c1 = {
            "bootstrap-заказ": f"<div class='modal' role=dialog style='{FIX}'>"
                               "<div class='modal-body'><p>Подтвердите заказ на "
                               "1 299 ₽</p></div><div class='modal-footer' "
                               "style='height:40px'><button onclick=\"window."
                               "hit=1\">ОК</button><button>Отмена</button>"
                               "</div></div>",
            "письмо": f"<div role=dialog style='{FIX}'><p>Отправить письмо 120 "
                      "получателям?</p><button onclick=\"window.hit=1\">ОК"
                      "</button></div>",
            "очистить": f"<div role=dialog style='{FIX}'><p>Очистить корзину?"
                        "</p><button onclick=\"window.hit=1\">ОК</button>"
                        "</div>",
            "согласие-в-форме": "<form onsubmit=\"window.hit=1;return false\">"
                                "<div class='consent-block' style='width:500px;"
                                "height:60px'>Даю согласие на обработку "
                                "персональных данных <button type=submit>"
                                "Согласен</button></div></form>"}
        pressed = []
        for name, html in c1.items():
            for strict in (False, True):
                page.set_content(html)
                page.evaluate("window.hit=0")
                page.evaluate(ba._js_fill(ba._DISMISS_OVERLAY_JS,
                                          STRICT=strict))
                if page.evaluate("window.hit") == 1:
                    pressed.append((name, strict))
        check("C1/M2: «ОК» в окне заказа/отправки/очистки и кнопка формы "
              "согласия — не жмутся ни в каком режиме", pressed == [])
        # N1: «персональные данные»/«конфиденциальность» в окне удаления/
        # заказа/выхода — не cookie-баннер
        n1 = [f"<div role=dialog style='{FIX}'><p>Удалить аккаунт? Ваши "
              "персональные данные будут удалены.</p><button onclick=\"window."
              "hit=1\">ОК</button></div>",
              f"<div class='modal' role=dialog style='{FIX}'><p>Подтвердите "
              "заказ. Согласие на обработку персональных данных.</p><div "
              "class='consent-footer'><button onclick=\"window.hit=1\">ОК"
              "</button></div></div>",
              f"<div role=dialog style='{FIX}'><p>Выйти из аккаунта? Политика "
              "конфиденциальности</p><button onclick=\"window.hit=1\">Хорошо"
              "</button></div>"]
        leaked = 0
        for html in n1:
            for strict in (False, True):
                page.set_content(html)
                page.evaluate("window.hit=0")
                page.evaluate(ba._js_fill(ba._DISMISS_OVERLAY_JS,
                                          STRICT=strict))
                leaked += page.evaluate("window.hit") == 1
        check("N1: слова про ПДн/конфиденциальность не отключают проверку "
              "опасного окна", leaked == 0)
        page.set_content(f"<div class='cookie-consent' style='{FIX}'><form "
                         "onsubmit=\"window.hit=1;return false\">We use "
                         "cookies <button>Accept all</button></form></div>")
        page.evaluate("window.hit=0")
        page.evaluate(ba._js_fill(ba._DISMISS_OVERLAY_JS, STRICT=True))
        check("N6: cookie-баннер с формой (consent.google-вид) — принят",
              page.evaluate("window.hit") == 1)
        # recheck2: окно без role=dialog (absolute) — футер с кнопками не
        # «отдельное окно»: текст «Удалить аккаунт?» виден
        ABS = ("position:absolute;top:50px;left:50px;width:500px;"
               "height:300px;background:#fff")
        pages = [f"<div class='modal' style='{ABS}'><p>Удалить аккаунт?</p>"
                 "<div class='modal-footer' style='height:50px'><button "
                 "onclick=\"window.hit=1\">ОК</button></div></div>",
                 "<div class='overlay' style='position:fixed;inset:0'></div>"
                 f"<div class='popup' style='{ABS}'><p>Подтвердите заказ "
                 "№4471</p><div class='popup__buttons' style='height:50px'>"
                 "<button onclick=\"window.hit=1\">ОК</button></div></div>"]
        leaked = 0
        for html in pages:
            for strict in (False, True):
                page.set_content(html)
                page.evaluate("window.hit=0")
                page.evaluate(ba._js_fill(ba._DISMISS_OVERLAY_JS,
                                          STRICT=strict))
                leaked += page.evaluate("window.hit") == 1
        check("recheck2: окно удаления/заказа без role=dialog — не жмётся",
              leaked == 0)
        # cookie-баннер со словами «удалить cookie», «отправки уведомлений»,
        # «Accept All Cookies» — принимается
        banners = [
            ("Принять", "Мы используем cookie. Вы можете удалить cookie в "
                        "настройках браузера."),
            ("ОК", "Сайт использует куки для оформления заказов и отправки "
                   "уведомлений."),
            ("Accept All Cookies", "We use cookies to send you relevant "
                                   "offers. You can delete or block them.")]
        accepted = 0
        for btn, text in banners:
            page.set_content(f"<div class='cookie-banner' style='{FIX}'><p>"
                             f"{text}</p><button onclick=\"window.hit=1\">"
                             f"{btn}</button></div>")
            page.evaluate("window.hit=0")
            page.evaluate(ba._js_fill(ba._DISMISS_OVERLAY_JS, STRICT=True))
            accepted += page.evaluate("window.hit") == 1
        check("recheck2: cookie-баннер со словами удаления/отправки — принят",
              accepted == len(banners))
        # Шторка корзины глубже двух уровней под fixed-обёрткой — читается
        page.set_content(
            "<main style='width:900px'><h1>Меню</h1><p>Маргарита от 499 ₽"
            "</p></main><div style='position:fixed;right:0;top:0;width:400px;"
            "height:600px'><div><div><section class='basket' style='height:"
            "560px'>Ваш заказ: Пепперони 599 ₽. Итого 599 ₽</section></div>"
            "</div></div>")
        got = page.evaluate(ba._READ_TASK_JS)
        check("recheck2: шторка корзины на глубине 3 — читается она",
              "Итого 599" in got and "Маргарита" not in got)
        page.set_content(f"<div class='cookie-notice' style='{FIX}'>We use "
                         "cookies in order to process payments. <button "
                         "onclick=\"window.hit=1\">Accept</button></div>")
        page.evaluate("window.hit=0")
        page.evaluate(ba._js_fill(ba._DISMISS_OVERLAY_JS, STRICT=True))
        check("L13: cookie-баннер с «in order to … payments» — принят",
              page.evaluate("window.hit") == 1)

        # ── Флаги снимка ──
        page.set_content("""
<button aria-pressed="true">Халапеньо</button>
<button aria-pressed="false">Сыр</button>
<button disabled>Недоступно</button>
<button aria-disabled="true">Арийно</button>
<fieldset disabled><button>В наборе</button></fieldset>
<form><input type=text aria-label="Телефон" inputmode="tel">
<input type=text aria-label="Поиск адреса" name="address-search">
<input type=text aria-label="Почта" autocomplete="email">
<button>Далее</button><button type=button>Изменить</button></form>
<form role=search><input type=text aria-label="Найти на сайте" name=q></form>
<input type=search aria-label="Поиск по меню">""")
        items = parsed()
        f = {i["text"]: i for i in items}
        check("D1: on 1/0 у переключателей, -1 у прочих",
              f["Халапеньо"]["on"] == 1 and f["Сыр"]["on"] == 0
              and f["Изменить"]["on"] == -1)
        check("D7: dis у disabled, aria-disabled, fieldset[disabled]",
              f["Недоступно"]["dis"] and f["Арийно"]["dis"]
              and f["В наборе"]["dis"] and not f["Сыр"]["dis"])
        check("A3: sub у submit-кнопки формы, не у type=button и не вне формы",
              f["Далее"]["sub"] and f["Далее"]["fm"]
              and not f["Изменить"]["sub"] and not f["Сыр"]["sub"])
        check("A5: qs — searchbox/role=search/type=search; «Поиск адреса» в "
              "форме с личными полями — нет",
              f["Найти на сайте"]["qs"] and f["Поиск по меню"]["qs"]
              and not f["Поиск адреса"]["qs"])
        check("D6: sn по inputmode=tel и autocomplete=email",
              f["Телефон"]["sn"] and f["Почта"]["sn"])
        # H2: нативный <dialog> — окно; M1: поиск в форме заказа — не qs
        page.set_content(
            "<dialog id=d><p>Подтвердите заказ на 1 299 ₽</p><form "
            "method=dialog><button>ОК</button></form></dialog>"
            "<form><input type=search aria-label='Улица и дом'>"
            "<input name=flat aria-label='Кв'><input name=cmt aria-label='Ком'>"
            "<button type=submit>Оформить заказ</button></form>"
            "<form><input type=search aria-label='Искать пиццу'>"
            "<button>Найти</button></form>"
            "<script>document.getElementById('d').showModal()</script>")
        f = {i["text"]: i for i in parsed()}
        check("H2: кнопка нативного <dialog> (showModal) — md",
              f["ОК"]["md"])
        check("M1: type=search в форме заказа — не строго поисковое; поиск с "
              "«Найти» — поисковое", not f["Улица и дом"]["qs"]
              and f["Искать пиццу"]["qs"])

        # ── A6/D4: сверка подписи и перекрытие ──
        # Обработчики меняют DOM: клик засчитывается по эффекту, не по фокусу
        page.set_content("""<script>function mark(t){window.hit=(window.hit||'')
+t+';';var s=document.createElement('span');s.innerText=t;
document.body.appendChild(s);}</script>
<button id=next onclick="mark('next')">Далее</button>
<div class=row><span>Сырный</span>
<button onclick="mark('pill')">49 ₽</button></div>
<input id=cm type=text placeholder="Комментарий">
<button onclick="window.hit=(window.hit||'')+'under;'"
 style="position:absolute;top:300px;left:10px;width:120px;height:40px">
Под слоем</button>""")
        items = parsed()
        nxt, pill = by_text(items, "Далее"), by_text(items, "Сырный")
        cm, under = by_text(items, "Комментарий"), by_text(items, "Под слоем")

        def click(it, exp=None):
            try:
                return ba._click_cdp(w, None, it["idx"], None, exp)
            except Exception as e:
                return f"ERR {e}"
        check("A6: подпись совпала — клик", click(nxt, "Далее") == "clicked")
        check("A6: пилюля-цена с подписью ряда — клик",
              click(pill, pill["text"]) == "clicked")
        page.evaluate("document.getElementById('next').innerText="
                      "'Подтвердить заказ'")
        r = click(nxt, "Далее")
        check("A6: узел переиспользован («Далее» → «Подтвердить заказ») — "
              "не нажат", ba.LABEL_CHANGED in r
              and "next;next" not in str(page.evaluate("window.hit")))
        page.evaluate("document.getElementById('cm').placeholder="
                      "'Номер карты'")
        try:
            r = ba._fill_cdp(w, None, cm["idx"], "x", None, False, "Комментарий")
        except Exception as e:
            r = str(e)
        check("A6: поле сменилось («Номер карты») — ввода нет",
              ba.LABEL_CHANGED in r)
        page.evaluate(
            "var o=document.createElement('div');o.innerText='Выберите город';"
            "o.style='position:fixed;top:0;left:0;width:100%;height:100%;"
            "background:rgba(0,0,0,.3)';o.onclick=function(){"
            "window.hit=(window.hit||'')+'OVERLAY;'};document.body.append(o)")
        r = click(under, "Под слоем")
        check("D4: элемент под чужим слоем — отказ, слой не нажат",
              "перекрыт" in r and "OVERLAY" not in str(page.evaluate(
                  "window.hit")))
        # Красная команда: дописанная подпись — уже другая кнопка
        page.set_content("<script>function mark(t){var s=document."
                         "createElement('span');s.innerText=t;document.body."
                         "appendChild(s);}</script><span>Отмена</span>"
                         "<button onclick=\"mark('x')\">Далее</button>")
        items = parsed()
        b = by_text(items, "Далее")
        page.evaluate("document.querySelector('button').innerText="
                      "'Далее — оплатить 1 299 ₽'")
        r = click(b, "Далее")
        check("A6: «Далее» → «Далее — оплатить 1 299 ₽» — не нажато",
              ba.LABEL_CHANGED in r)
        page.evaluate("document.querySelector('button').innerText="
                      "'Удалить аккаунт'")
        r = click(b, "Отмена")
        check("A6: подпись соседа («Отмена») кнопку «Удалить аккаунт» не "
              "подтверждает", ba.LABEL_CHANGED in r)

        # M6: подписи, которые снимок сочинил сам, — не «подпись сменилась»
        page.set_content(
            "<div class=row><span>Сырный</span> <button onclick=\"mark('s')\">"
            "49 ₽</button></div><div><label>Телефон</label><div><input "
            "id=ph></div></div><button class='icon-cart' onclick=\"mark('c')"
            "\"><svg width=20 height=20></svg></button><script>function "
            "mark(t){var s=document.createElement('span');s.innerText=t;"
            "document.body.appendChild(s);}</script>")
        el = page.query_selector("button")
        ok_pill = el.evaluate("(e, exp) => (" + ba._LABEL_MATCH_FN
                              + ")(e, exp)", "Сырный · 49 ₽")
        ok_ph = page.query_selector("#ph").evaluate(
            "(e, exp) => (" + ba._LABEL_MATCH_FN + ")(e, exp)", "Телефон")
        ok_icon = page.query_selector(".icon-cart").evaluate(
            "(e, exp) => (" + ba._LABEL_MATCH_FN + ")(e, exp)", "корзина")
        check("M6: пилюля «Сырный · 49 ₽», поле с подписью у родителя, иконка "
              "без текста — совпадают", ok_pill and ok_ph and ok_icon)

        # ── D5: клик-пустышка ──
        page.set_content(
            "<button>Пустышка</button>"
            "<button aria-pressed=false onclick=\"this.setAttribute("
            "'aria-pressed','true')\">Тоггл</button>")
        items = parsed()
        # Шаг агента (expect): отпечаток клика без фокуса
        r1, r2 = click(items[0], "Пустышка"), click(items[1], "Тоггл")
        check("D5: агент — пустышка «не уверен» (фокус — не эффект); тоггл — "
              "клик", "не уверен" in r1 and r2 == "clicked")

        # ── D6: маска телефона ──
        mask_html = """<label>Телефон <input id=ph type=tel></label><script>
var el=document.getElementById('ph');el.addEventListener('input',function(){
var d=el.value.replace(/\\D/g,'');if(d[0]!=='7')d='7'+d;d=d.slice(0,11);
var f='+7 ('+d.slice(1,4);if(d.length>4)f+=') '+d.slice(4,7);
if(d.length>7)f+='-'+d.slice(7,9);if(d.length>9)f+='-'+d.slice(9,11);
el.value=f;});</script>"""
        out = {}
        for val in ("9991234567", "+79991234567"):
            page.set_content(mask_html)
            it = parsed()[0]
            try:
                out[val] = ba._fill_cdp(w, None, it["idx"], val, None, False)
            except Exception as e:
                out[val] = str(e)
        check("D6: 10 цифр в маску «+7» — filled (сравнение по цифрам)",
              out["9991234567"] == "filled")
        check("D6: «+7…» в маску — честное «не совпало», без самих цифр",
              "значение не совпало" in out["+79991234567"]
              and "7999" not in out["+79991234567"])

        # ── D8: чтение для агента ──
        page.set_content(
            "<main><h1>Меню</h1>" + "<p>Пицца строка</p>" * 400
            + "<p>ИТОГО-ВНИЗУ 816 ₽</p></main><div class='cart-drawer' "
            "style='position:fixed;right:0;top:0;width:300px;height:400px;"
            "background:#fff'>Корзина: Пепперони ×2 Итого 816 ₽</div>")
        t_drawer = page.evaluate(ba._READ_TASK_JS)
        page.evaluate("document.querySelector('.cart-drawer').remove()")
        t_page = page.evaluate(ba._READ_TASK_JS)
        check("D8: открыта шторка корзины — читается она",
              t_drawer.startswith("Корзина: Пепперони"))
        check("D8: длинная страница — итог внизу не обрезан",
              "ИТОГО-ВНИЗУ" in t_page
              and "ИТОГО-ВНИЗУ" not in page.evaluate(ba._READ_PAGE_JS))
        # M7: страница корзины — весь состав с итогом, не последняя строка
        page.set_content(
            "<main class='cart-page'><h1>Корзина</h1><div class='cart-item'>"
            "Пепперони 599 ₽ − 1 +</div><div class='cart-item'>Маргарита 499 ₽"
            " − 1 +</div><div>Итого: 1 098 ₽</div></main>")
        t_cart = page.evaluate(ba._READ_TASK_JS)
        check("M7: страница корзины — весь состав и итог",
              "Пепперони" in t_cart and "Итого: 1 098 ₽" in t_cart)
        # Раздел одностраничного меню по разметке (как у Додо): закреплённая
        # панель ссылок разделов, блоки с заголовком h2 и карточками article
        card = ("<article style='cursor:pointer'><img alt=''><b>{0}</b> "
                "<button>от {1} ₽</button></article>")
        page.set_content(
            "<nav style='position:sticky;top:0'><a>Пиццы</a><a>Напитки</a>"
            "</nav><main><section><div><h2>Пиццы</h2></div>"
            + card.format("Пепперони", 349) + card.format("Сырная", 279)
            + "</section><section><h2>Напитки</h2>"
            + card.format("Кола 0,5 л", 135) + card.format("Морс", 99)
            + card.format("Кола 0,5 л", 135)
            + "</section><section><h2>Десерты</h2>"
            + card.format("Чизкейк", 199) + "</section></main>")
        st = page.evaluate("[scrollY, document.querySelectorAll("
                           "'[data-vpc-idx],[data-vpc-gidx]').length]")
        got = json.loads(page.evaluate(ba._js_fill(ba._SECTION_ITEMS_JS,
                                                   NAME="Напитки")))
        check("раздел по разметке: «Напитки» — только его карточки (внешний "
              "элемент, без дублей), не ссылка панели и не соседние разделы",
              got.get("found") and got.get("items") == [
                  "Кола 0,5 л от 135 ₽", "Морс от 99 ₽"])
        check("раздел по разметке: заголовок в обёртке («Пиццы») — блок "
              "раздела всё равно найден",
              json.loads(page.evaluate(ba._js_fill(
                  ba._SECTION_ITEMS_JS, NAME="Пиццы"))).get("items")
              == ["Пепперони от 349 ₽", "Сырная от 279 ₽"])
        check("раздел по разметке: нет такого заголовка — found=false; "
              "страница не прокручена и не размечена",
              json.loads(page.evaluate(ba._js_fill(
                  ba._SECTION_ITEMS_JS, NAME="Соусы"))).get("found") is False
              and page.evaluate("[scrollY, document.querySelectorAll("
                                "'[data-vpc-idx],[data-vpc-gidx]').length]")
              == st)
        # duck.ai (01.10): JS модели и селекторы ответа — на синтетической
        # копии разметки сайта (меню рисуется по клику, как на живом)
        from app.features import web_llm as wl
        page.set_content(
            "<div data-testid='duckai-chat-input'>"
            "<button data-testid='duckai-tools-button'>Tools</button>"
            "<span id='chip'>Web Search</span>"
            "<button data-testid='model-picker-button'>5.6 Luna</button></div>"
            "<div id='menu'></div>"
            "<div id='u1-assistant-message-0-1'><h3>Duck.ai said</h3>"
            "<div><span><strong>Gemma 4</strong> 31B</span></div>"
            "<div><div><div><div><div><div class='space-y-4 whitespace-normal'>"
            "<p>Да</p></div></div></div></div></div></div>"
            "<div><div><button aria-label='Copy to clipboard'>c</button></div>"
            "<button aria-label='2nd opinion'>2nd opinion</button></div></div>"
            "<script>"
            "var menu=document.getElementById('menu'),ws=true,model='gpt-5.6-luna';"
            "function row(t,id,on){var b=document.createElement('button');"
            "b.setAttribute('role','menuitemradio');b.innerText=t;"
            "if(id)b.setAttribute('data-testid','model-picker-row-'+id);"
            "b.setAttribute('aria-checked',on?'true':'false');menu.appendChild(b);return b;}"
            "document.querySelector('[data-testid=duckai-tools-button]').onclick=function(){"
            "menu.innerHTML='';row('Web Search Source answers from the web','',ws).onclick="
            "function(){ws=false;document.getElementById('chip').innerText='';menu.innerHTML='';};};"
            "document.querySelector('[data-testid=model-picker-button]').onclick=function(){"
            "menu.innerHTML='';['gpt-5.6-luna','tinfoil/gemma4-31b'].forEach(function(id){"
            "row(id,id,id===model).onclick=function(){model=id;menu.innerHTML='';};});};"
            "document.body.addEventListener('keydown',function(e){if(e.key==='Escape')"
            "menu.innerHTML='';});"
            "</script>")
        js = wl._DUCKAI_MODE_JS % json.dumps("tinfoil/gemma4-31b")
        first = page.evaluate(js)
        again = page.evaluate(js)
        check("duck.ai: JS выключает веб-поиск и выбирает Gemma 4; повтор — "
              "без клика (идемпотентно)",
              first == "search-off,clicked:tinfoil/gemma4-31b"
              and again == "ok:tinfoil/gemma4-31b")
        ans = page.evaluate(
            "(sels)=>{var e=[...document.querySelectorAll(sels[0])].pop();"
            "if(!e)return null;var p=e,done=false;"
            "for(var u=0;u<6&&p;u++,p=p.parentElement){"
            "if(p.querySelector(\"button[aria-label*='copy' i]\")){done=true;break;}}"
            "return [e.innerText.trim(),done];}",
            wl.ADAPTERS["duckai"]["answer"])
        check("duck.ai: ответ — только тело («Да», без «Duck.ai said» и имени "
              "модели), кнопка копирования — в пределах 6 предков (hasDone)",
              ans == ["Да", True])
        # Enter фоновой вкладки: duck.ai отправляет только по keyDown с
        # символом «\r» (keypress), rawKeyDown без символа — нет (01.10)
        sent_evs = []
        _rtc, _rev = ba._raw_tab_call, ba._raw_eval
        ba._raw_tab_call = lambda tab, method, params: sent_evs.append(params)
        ba._raw_eval = lambda tab, js, timeout_sec=None: ""  # страж Enter
        try:
            ba._raw_enter(1, with_text=True)
            with_txt = list(sent_evs)
            sent_evs.clear()
            ba._raw_enter(1)
            plain = list(sent_evs)
        finally:
            ba._raw_tab_call, ba._raw_eval = _rtc, _rev
        page.set_content(
            "<textarea id='t'></textarea><script>var sent=0;"
            "document.getElementById('t').addEventListener('keypress',"
            "function(e){if(e.key==='Enter'){sent++;e.preventDefault();}});"
            "</script>")
        cdp = page.context.new_cdp_session(page)

        def _fired(evs):
            page.evaluate("sent=0;document.getElementById('t').focus()")
            for ev in evs:
                cdp.send("Input.dispatchKeyEvent", ev)
            page.wait_for_timeout(200)
            return page.evaluate("sent")
        check("Enter с символом (enter_text) срабатывает на keypress, "
              "rawKeyDown без символа — нет (как было для прочих сайтов)",
              _fired(with_txt) == 1 and _fired(plain) == 0
              and with_txt[0]["type"] == "keyDown"
              and plain[0]["type"] == "rawKeyDown")
        _guard_checks(ba, page, cdp, check)
        page.set_content(
            "<nav style='position:sticky;top:0'><a>Пиццы</a><a>Напитки</a>"
            "</nav><main><section><div><h2>Пиццы</h2></div>"
            + card.format("Пепперони", 349) + card.format("Сырная", 279)
            + "</section><section><h2>Напитки</h2>"
            + card.format("Кола 0,5 л", 135) + card.format("Морс", 99)
            + card.format("Кола 0,5 л", 135)
            + "</section><section><h2>Десерты</h2>"
            + card.format("Чизкейк", 199) + "</section></main>")
        check("названия разделов по разметке: заголовки с двумя и более "
              "позициями с ценой («Десерты» с одной — нет, панель ссылок — "
              "не заголовки); страница не прокручена и не размечена",
              json.loads(page.evaluate(ba._SECTION_NAMES_JS))
              == ["Пиццы", "Напитки"]
              and page.evaluate("[scrollY, document.querySelectorAll("
                                "'[data-vpc-idx],[data-vpc-gidx]').length]")
              == st)
    finally:
        browser.close()
        pw.stop()

    print(f"\nИтого: {ok} проверок, {fails} провалов")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

"""Регрессия: доставленный клик не рапортуется как проваленный.

playwright по умолчанию считает клик незавершённым, пока не закоммитится
вызванный им переход («waiting for scheduled navigations to finish»): при
медленном переходе таймаут CLICK_TIMEOUT_MS срабатывает ПОСЛЕ отправки мыши.
Такой клик должен считаться доставленным — без ответа «клик не выполнен» и
без повторного force-клика.

Живого браузера нет: locator/страница — фейки; тексты ошибок — дословно
снятые с playwright 1.62 + Chrome (медленная ссылка, timeout=2500).

Запуск: /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
        -m scripts.test_click_delivery
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Дословно с playwright 1.62: клик ушёл («click action done»), таймаут —
# в ожидании перехода
POST_DISPATCH = (
    'Locator.click: Timeout 2500ms exceeded.\nCall log:\n'
    '  - waiting for locator("#a")\n'
    '    - locator resolved to <a id="a" href="/slow">Sign in</a>\n'
    '  - attempting click action\n'
    '    - waiting for element to be visible, enabled and stable\n'
    '    - element is visible, enabled and stable\n'
    '    - scrolling into view if needed\n'
    '    - done scrolling\n'
    '    - performing click action\n'
    '    - click action done\n'
    '    - waiting for scheduled navigations to finish\n')
# Таймаут actionability: мышь НЕ отправлялась
PRE_DISPATCH = (
    'Locator.click: Timeout 2500ms exceeded.\nCall log:\n'
    '  - waiting for locator("[data-vpc-idx=\\"3\\"]")\n'
    '    - locator resolved to <button data-vpc-idx="3">Войти</button>\n'
    '  - attempting click action\n'
    '    2 × waiting for element to be visible, enabled and stable\n'
    '      - element is not visible\n'
    '    - retrying click action\n')


class PwTimeout(Exception):
    # как playwright TimeoutError: текст с call log
    pass


def main():
    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    import app.features.browser_actions as ba

    class Req:
        def __init__(self, frame, nav=True):
            self.frame, self._nav = frame, nav

        def is_navigation_request(self):
            return self._nav

    class Page:
        # evaluate отдаёт отпечаток (fp крутится scroll'ом), on/remove_listener
        # — события запросов, как у playwright.

        def __init__(self):
            self.url = "https://lk.example.ru/"
            self.main_frame = self
            self.frames = [self]
            self.doc = "d1"
            self.fp = 1
            self.handlers = []
            self.pending = []     # события, доставляемые на ближайшем вызове
            self.evals = 0
            self.pumps = 0

        def on(self, ev, fn):
            assert ev == "request"
            self.handlers.append(fn)

        def remove_listener(self, ev, fn):
            self.handlers.remove(fn)

        def _deliver(self):
            evs, self.pending = self.pending, []
            for r in evs:
                for h in list(self.handlers):
                    h(r)

        def wait_for_timeout(self, ms):
            self.pumps += 1
            self._deliver()

        def evaluate(self, js, *a):
            self.evals += 1
            self._deliver()
            return f"{self.doc}|{self.url}|complete|{self.fp}"

    class Loc:
        # locator.first: по сценарию — поведение каждой попытки клика.

        def __init__(self, page, script):
            self.page, self.script = page, list(script)
            self.calls = []

        @property
        def first(self):
            return self

        def click(self, **kw):
            self.calls.append(kw)
            step = self.script.pop(0) if self.script else None
            if callable(step):
                step(self.page)
            elif isinstance(step, BaseException):
                raise step

    class W:
        def __init__(self, page):
            self.page = page

        def page_for(self, host, tab_id):
            return self.page

        def _all_pages(self):
            return [self.page]

    def run(script, fast_verify=True):
        # → (результат|исключение, loc, page)
        page = Page()
        loc = Loc(page, script)
        saved = (ba._locator_any_frame, ba.CLICK_VERIFY_SEC,
                 ba._label_toggle_js)
        ba._locator_any_frame = lambda pg, idx: (loc, pg)
        ba._label_toggle_js = lambda scope, idx: None
        if fast_verify:
            ba.CLICK_VERIFY_SEC = 0.2
        try:
            try:
                res = ba._click_cdp(W(page), "lk.example.ru", 3, None)
            except Exception as e:
                res = e
        finally:
            (ba._locator_any_frame, ba.CLICK_VERIFY_SEC,
             ba._label_toggle_js) = saved
        return res, loc, page

    def nav_then(exc=None):
        # Клик доставлен: страница ставит навигационный запрос (виден на
        # ближайшем вызове playwright), затем — опционально исключение.
        def step(pg):
            pg.pending.append(Req(pg))
            if exc is not None:
                raise exc
        return step

    # 1. Клик ушёл, переход медленный, playwright
    # бросил таймаут ПОСЛЕ отправки (если бы no_wait_after не действовал)
    res, loc, page = run([nav_then(PwTimeout(POST_DISPATCH))])
    check("таймаут после «click action done» — клик доставлен, «clicked»",
          res == "clicked")
    check("…и НЕТ второго (force) клика после доставленного",
          len(loc.calls) == 1 and not loc.calls[0].get("force"))
    check("клик идёт с no_wait_after=True (не ждём коммита перехода)",
          loc.calls[0].get("no_wait_after") is True
          and loc.calls[0].get("timeout") == ba.CLICK_TIMEOUT_MS)
    check("слушатель запросов снят после клика", page.handlers == [])

    # 2. Тот же call log без навигации (клик по кнопке, эффект — DOM)
    def dom_change(pg):
        pg.fp += 1
        raise PwTimeout(POST_DISPATCH)
    res, loc, _ = run([dom_change])
    check("post-dispatch таймаут без перехода: DOM изменился → «clicked», "
          "без force", res == "clicked" and len(loc.calls) == 1)

    # 3. Медленный переход, клик вернулся штатно (no_wait_after), DOM и URL
    # старые весь бюджет проверки — начатый переход и есть эффект:
    # не ClickUncertain (он в computer_control ведёт к ПОВТОРНОМУ клику)
    res, loc, page = run([nav_then()])
    check("медленный переход: начатая навигация = эффект, «clicked», а не "
          "«не уверен»", res == "clicked")
    check("…замер DOM старого документа во время перехода не делается "
          "(evaluate висел бы до коммита)",
          page.evals == 1 and page.pumps >= 1)

    # 4. Неоднозначное исключение без call log, но переход уже начат →
    # доставлен, force не нужен
    res, loc, _ = run([nav_then(RuntimeError("Target page, context or "
                                             "browser has been closed"))])
    check("исключение без call log, но переход начат — доставлен, без force",
          res == "clicked" and len(loc.calls) == 1)

    # 5. Честный отказ до отправки: actionability-таймаут → force-клик
    # (мышь ещё не отправлялась) → успех
    def force_ok(pg):
        pg.fp += 1
    res, loc, _ = run([PwTimeout(PRE_DISPATCH), force_ok])
    check("таймаут ДО отправки → один force-клик (с no_wait_after) → «clicked»",
          res == "clicked" and len(loc.calls) == 2
          and loc.calls[1].get("force") is True
          and loc.calls[1].get("no_wait_after") is True)

    # 6. Прокрутка при неудачных попытках меняет DOM-отпечаток, но это НЕ
    # доставка: иначе недоставленный клик сошёл бы за «сработал»
    def scrolled_fail(pg):
        pg.fp += 7   # повторы playwright скроллят элемент block:end/center
        raise PwTimeout(PRE_DISPATCH)
    res, loc, _ = run([scrolled_fail, force_ok])
    check("сдвиг отпечатка прокруткой при неудачной попытке — не доставка, "
          "force всё равно делается", len(loc.calls) == 2
          and loc.calls[1].get("force") is True)

    # 7. Обе попытки не отправили мышь → честный отказ, человеческим текстом
    res, loc, page = run([PwTimeout(PRE_DISPATCH), PwTimeout(PRE_DISPATCH)])
    msg = str(res)
    check("обе попытки до отправки → BrowserUnavailable (не ClickUncertain)",
          isinstance(res, ba.BrowserUnavailable)
          and not isinstance(res, ba.ClickUncertain))
    check("текст отказа по-русски, без «Locator.click:»/«Timeout …ms "
          "exceeded»: " + msg,
          msg.startswith("клик не выполнен: элемент за 2.5 с")
          and "Locator.click" not in msg and "exceeded" not in msg)
    check("текст отказа без хвостовой точки (вызывающий ставит свою)",
          not msg.endswith("."))
    check("слушатель снят и после отказа", page.handlers == [])

    # 8. Навигационные запросы чужих фреймов и не-документные — не признак
    def foreign(pg):
        pg.pending.append(Req(object()))          # другой фрейм
        pg.pending.append(Req(pg, nav=False))     # XHR/картинка
    res, loc, _ = run([foreign])
    check("XHR и переход чужого фрейма — не эффект клика (честное «не "
          "уверен»)", isinstance(res, ba.ClickUncertain))

    # 9. Разбор ошибок playwright
    check("_pw_click_dispatched: post-dispatch лог → True, pre → False, "
          "без лога → False",
          ba._pw_click_dispatched(PwTimeout(POST_DISPATCH))
          and not ba._pw_click_dispatched(PwTimeout(PRE_DISPATCH))
          and not ba._pw_click_dispatched(RuntimeError("boom")))
    check("_pw_error_text: не-таймаут — первая строка без префикса метода и "
          "точки", ba._pw_error_text(RuntimeError(
              "Locator.click: Element is not attached to the DOM.\nCall log:"))
          == "Element is not attached to the DOM")

    # 10. Enter (отправка поиска/формы) — та же ловушка ожидания перехода
    class EnterLoc:
        def __init__(self, exc=None):
            self.kw, self.exc = None, exc

        def press(self, key, **kw):
            self.kw = (key, kw)
            if self.exc:
                raise self.exc

    pg = Page()
    el = EnterLoc()
    pre = ba._page_state(pg)
    check("Enter идёт с no_wait_after=True",
          ba._pw_press_enter(el, pg, pre) is None
          and el.kw == ("Enter", {"timeout": ba.CLICK_TIMEOUT_MS,
                                  "no_wait_after": True}))
    pg2 = Page()
    pre2 = ba._page_state(pg2)
    el2 = EnterLoc(PwTimeout("Locator.press: Timeout 2500ms exceeded.\n"
                             "Call log:\n"))
    pg2.url = "https://lk.example.ru/search?q=1"   # поиск уже ушёл
    check("таймаут Enter при уже сменившемся URL — нажато (None)",
          ba._pw_press_enter(el2, pg2, pre2) is None)
    pg2.url = pre2.url
    why = ba._pw_press_enter(el2, pg2, pre2)
    check("таймаут Enter без перехода — честная причина по-русски: "
          f"{why}", why is not None and why.startswith("поле за 2.5 с")
          and not why.endswith("."))

    # 11. «exceeded..»: detail ошибки в ответе бота — без хвостовой точки
    import tempfile
    from app.features.computer_control import ComputerControlManager

    class M(ComputerControlManager):
        def __init__(self, *a, err=None, **kw):
            super().__init__(*a, **kw)
            self.err = err

        def _dispatch(self, action, router=None):
            raise self.err

    tmp = Path(tempfile.mkdtemp())
    m = M(context="t", config=True, base_dir=tmp,
          err=ba.BrowserUnavailable("клик не выполнен: Timeout 2500ms "
                                    "exceeded."))
    okk, detail = m.execute({"kind": "click", "idx": 3}, "c1")
    reply = f"Не удалось нажать: {detail}."
    check("execute: detail без хвостовой точки → в ответе нет «..»: "
          + reply, not okk and ".." not in reply
          and detail.endswith("exceeded"))
    m.err = RuntimeError("страница думает...")
    _o, detail2 = m.execute({"kind": "click", "idx": 3}, "c1")
    check("execute: многоточие в detail сохраняется",
          detail2.endswith("думает..."))
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

"""Тест исполнения действий режима управления: без повторного клика после
ClickUncertain, перенахождение подтверждённого элемента после «элемент
потерян», отпечаток точки координатного клика, label-фолбэк без двойного
щелчка, опрос корзины, слайдер (синонимы, повтор, громкость через <video>),
nav (ожидание готовности, след шагов в аудите на осечке).

Браузер и LLM подменены — живой Chrome и сеть не трогаются. JS-проверки
идут в node (если он есть) на поддельном DOM.

Запуск: python -m scripts.test_cc_execution
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cc_execution_")
    tmp = Path(tempfile.mkdtemp(prefix="cc_execution_data_"))

    ok = 0
    fail = 0

    def check(name, cond):
        nonlocal ok, fail
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        if cond:
            ok += 1
        else:
            fail += 1

    from app.features import browser_actions as ba
    from app.features import computer_control as cc
    from app.features.computer_control import ComputerControlManager

    saved = {}

    def patch(obj, name, value):
        key = (id(obj), name)
        if key not in saved:
            saved[key] = (obj, name, getattr(obj, name, None))
        setattr(obj, name, value)

    def restore():
        for obj, name, val in saved.values():
            setattr(obj, name, val)
        saved.clear()

    def boom(*a, **kw):
        raise AssertionError("живой браузер не трогаем")

    def base_mocks():
        # Всё, что могло бы дотянуться до браузера, — заглушки
        patch(ba, "_select_backend", boom)
        patch(ba, "_run_js", boom)
        patch(ba, "page_urls", lambda: [])
        patch(ba, "follow_popup", lambda pre: None)
        patch(ba, "find_tab_id", lambda host: None)

    def last_audit(sub):
        p = tmp / sub / "audit.jsonl"
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def mgr(sub):
        return ComputerControlManager(context="t", base_dir=tmp / sub,
                                      config={"confirm": True})

    # ── 1. ClickUncertain: клик доставлен — второго клика нет ──
    try:
        base_mocks()
        clicks = []
        snaps = []

        def _ct_unc(host, idx, tab_id=None):
            clicks.append(idx)
            raise ba.ClickUncertain("клик отправлен, но видимого эффекта нет")

        patch(ba, "click_tagged", _ct_unc)
        patch(ba, "snapshot_elements",
              lambda host=None, tab_id=None: (snaps.append(1), boom())[1])
        m = mgr("unc")
        act = {"kind": "click", "idx": 7, "element": "Меню", "host": "x.ru",
               "goal": "бургер-меню", "value": "https://x.ru/"}
        ok1, det1 = m.execute(act, "c1")
        rec = last_audit("unc")
        check("ClickUncertain: ровно один клик, без переснятия страницы",
              clicks == [7] and not snaps)
        check("ClickUncertain: честное «не уверен» (ok=False, класс uncertain)",
              ok1 is False and "эффекта нет" in det1
              and rec.get("error_class") == "uncertain"
              and rec.get("verify") == "uncertain"
              and not act.get("retried"))
    finally:
        restore()

    # ── 2. «элемент потерян»: перенаходим ТОТ ЖЕ элемент по подписи ──
    try:
        base_mocks()
        clicks = []

        def _ct_lost_once(host, idx, tab_id=None):
            clicks.append(idx)
            if len(clicks) == 1:
                raise ba.BrowserUnavailable(
                    "элемент потерян — страница изменилась")
            return "clicked"

        patch(ba, "click_tagged", _ct_lost_once)
        patch(ba, "snapshot_elements", lambda host=None, tab_id=None: (
            "https://x.ru/", "x.ru",
            [{"idx": 101, "tag": "a", "text": "Войти"},
             {"idx": 102, "tag": "a", "text": "Каталог товаров"}]))
        m = mgr("lost")
        m._choose_element = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("точное совпадение подписи — без выбора по цели"))
        act = {"kind": "click", "idx": 5, "element": "Каталог товаров",
               "host": "x.ru", "goal": "каталог", "value": "https://x.ru/"}
        ok2, det2 = m.execute(act, "c2")
        rec = last_audit("lost")
        check("потерян: клик по элементу с той же подписью, один повтор",
              ok2 is True and clicks == [5, 102] and act["idx"] == 102)
        check("потерян: retried=True и в действии, и в аудите",
              act.get("retried") is True and rec.get("retried") is True)
    finally:
        restore()

    # ── 3. «элемент потерян», а вкладка ушла на другую страницу — отказ ──
    try:
        base_mocks()
        clicks = []

        def _ct_lost(host, idx, tab_id=None):
            clicks.append(idx)
            raise ba.BrowserUnavailable("элемент потерян — страница изменилась")

        patch(ba, "click_tagged", _ct_lost)
        patch(ba, "snapshot_elements", lambda host=None, tab_id=None: (
            "https://x.ru/other", "x.ru",
            [{"idx": 201, "tag": "a", "text": "Каталог товаров"}]))
        m = mgr("moved")
        act = {"kind": "click", "idx": 5, "element": "Каталог товаров",
               "host": "x.ru", "goal": "каталог", "value": "https://x.ru/"}
        ok3, det3 = m.execute(act, "c3")
        check("потерян + страница сменилась: отказ без второго клика",
              ok3 is False and clicks == [5] and "страница уже сменилась" in det3)
    finally:
        restore()

    # ── 4. Перевыбор по цели нашёл ДРУГОЙ элемент — не нажимаем ──
    try:
        base_mocks()
        clicks = []
        patch(ba, "click_tagged", _ct_lost)
        patch(ba, "snapshot_elements", lambda host=None, tab_id=None: (
            "https://x.ru/", "x.ru",
            [{"idx": 301, "tag": "a", "text": "Каталог акций"}]))
        m = mgr("other")
        m._choose_element = lambda goal, items, router, **kw: (
            301, {"path": "score"})
        act = {"kind": "click", "idx": 5, "element": "Каталог товаров",
               "host": "x.ru", "goal": "каталог", "value": "https://x.ru/"}
        ok4, det4 = m.execute(act, "c4")
        check("потерян + на месте цели другой элемент: отказ с обеими подписями",
              ok4 is False and clicks == [5] and "Каталог акций" in det4
              and "не нажимаю" in det4)
    finally:
        restore()

    # ── 5. Наведение: «не уверен» не повторяется, «потерян» — перенаходим ──
    try:
        base_mocks()
        hovers = []

        def _hv_lost_once(host, idx, tab_id=None):
            hovers.append(idx)
            if len(hovers) == 1:
                raise ba.BrowserUnavailable("элемент потерян — страница изменилась")
            return "hovered"

        patch(ba, "hover_tagged", _hv_lost_once)
        patch(ba, "snapshot_elements", lambda host=None, tab_id=None: (
            "https://x.ru/", "x.ru",
            [{"idx": 401, "tag": "a", "text": "Профиль"}]))
        m = mgr("hover")
        act = {"kind": "hover", "idx": 9, "element": "Профиль",
               "host": "x.ru", "goal": "профиль", "value": "https://x.ru/"}
        ok5, _ = m.execute(act, "c5")
        check("наведение: потерян → тот же элемент, один повтор, retried",
              ok5 is True and hovers == [9, 401] and act.get("retried") is True)
    finally:
        restore()

    # ── 6. Координатный клик: отпечаток точки сверяется до клика ──
    try:
        base_mocks()
        got_kw = {}

        def _cap(host, x, y, tab_id=None, **kw):
            got_kw.update(kw)
            return "clicked"

        patch(ba, "click_at_point", _cap)
        m = mgr("point")
        sig = {"sx": 0, "sy": 640, "el": "canvas#game|"}
        act = {"kind": "click", "host": "x.ru", "element": "зона 3",
               "point": {"x": 100.0, "y": 200.0, "label": "зона 3",
                         "zone": 3, "sig": sig}}
        # Клик по точке vision исполняется только после «да» (гейт execute)
        m.grant_confirmation(act, "pending")
        m.execute(act, "c6")
        check("координатный клик: отпечаток из point уходит в click_at_point",
              got_kw.get("expect") == sig)
    finally:
        restore()

    class _SigPage:
        def __init__(self, sig):
            self.sig = sig
            self.clicked = 0
            self.mouse = self

        def click(self, x, y):
            self.clicked += 1

        def evaluate(self, js, *a):
            if js.startswith("innerWidth"):
                return "1280x800"
            if js == ba._POINT_SIG_JS:
                return json.dumps(self.sig)
            return "doc|https://x.ru/|complete|1"

    exp = {"sx": 0, "sy": 640, "el": "button#buy|Купить"}
    same = _SigPage(dict(exp))
    err_same = None
    try:
        ba._check_point_sig(same, 10, 10, exp)
    except ba.BrowserUnavailable as e:
        err_same = str(e)
    check("отпечаток совпал — клик разрешён", err_same is None)
    scrolled = _SigPage({"sx": 0, "sy": 900, "el": "button#buy|Купить"})
    w = type("W", (), {"page_for": lambda self, h, t: scrolled})()
    err_scr = ""
    try:
        ba._click_point_cdp(w, "x.ru", 10, 10, None, exp)
    except ba.BrowserUnavailable as e:
        err_scr = str(e)
    check("страница прокрутилась после выбора точки — отказ ДО клика",
          "прокрутилась" in err_scr and scrolled.clicked == 0)
    other = _SigPage({"sx": 0, "sy": 641, "el": "div#banner|Реклама"})
    w2 = type("W", (), {"page_for": lambda self, h, t: other})()
    err_oth = ""
    try:
        ba._click_point_cdp(w2, "x.ru", 10, 10, None, exp)
    except ba.BrowserUnavailable as e:
        err_oth = str(e)
    check("под точкой другой элемент — отказ ДО клика (дрейф 1px — не прокрутка)",
          "другой элемент" in err_oth and other.clicked == 0)
    err_none = None
    try:
        ba._check_point_sig(_SigPage(exp), 10, 10, None)
    except ba.BrowserUnavailable as e:
        err_none = str(e)
    check("без отпечатка (старые действия) — сверки нет", err_none is None)
    check("зоны: отпечаток центра снимается вместе с зонами (psig, формат "
          "_POINT_SIG_JS)",
          "sig:psig(" in ba._ALL_CLICKABLE_BOXES_JS
          and "e.tagName.toLowerCase()+'#'+(e.id||'')+'|'" in
          ba._ALL_CLICKABLE_BOXES_JS
          and "e.tagName.toLowerCase()+'#'+(e.id||'')+'|'" in ba._POINT_SIG_JS)
    import inspect
    check("наведение по координатам тоже принимает отпечаток",
          "expect" in inspect.signature(ba.hover_at_point).parameters)

    node = shutil.which("node")

    def run_node(script: str) -> str:
        r = subprocess.run([node, "-e", script], capture_output=True,
                           text=True, timeout=20)
        return (r.stdout or "").strip() + (
            ("\nERR:" + r.stderr.strip()[:300]) if r.returncode else "")

    # ── 7. Label-фолбэк: сам label второй раз не кликается ──
    if node:
        # Поддельный DOM: e (цель) внутри label; у label — чекбокс (или нет).
        # pre — замер «до клика» (_LABEL_PRE_JS), клик пользователя
        # эмулируется сменой checked, затем фолбэк (_LABEL_TOGGLE_BODY_JS)
        lbl_js = (
            "function mk(hasInput,checked){"
            "var inp=hasInput?{type:'checkbox',checked:checked,clicks:0,"
            "click:function(){this.clicks++;this.checked=!this.checked;}}:null;"
            "var lab={clicks:0,control:inp,click:function(){this.clicks++;},"
            "querySelector:function(){return inp;}};"
            "var e={closest:function(){return lab;}};"
            "return {e:e,lab:lab,inp:inp};}"
            "function pre(d){var vpcSe=d.e;" + ba._LABEL_PRE_JS + "}"
            "function fb(d){var e=d.e;" + ba._LABEL_TOGGLE_BODY_JS + "}"
            "var window=globalThis,out=[];"
            # a) наш клик уже перещёлкнул контрол → flipped, без клика
            "var a=mk(true,false);pre(a);a.inp.checked=true;"
            "out.push(fb(a)+':'+a.inp.clicks+':'+a.lab.clicks);"
            # b) клик прошёл мимо label-механики → один input.click()
            "var b=mk(true,false);pre(b);"
            "out.push(fb(b)+':'+b.inp.clicks+':'+b.lab.clicks);"
            # c) был включён, наш клик выключил → flipped, обратно не щёлкаем
            "var c=mk(true,true);pre(c);c.inp.checked=false;"
            "out.push(fb(c)+':'+c.inp.clicks+':'+c.lab.clicks);"
            # d) label без чекбокса → '' и label не кликается
            "var d=mk(false,false);pre(d);"
            "out.push(fb(d)+':-:'+d.lab.clicks);"
            # e) состояние «до» неизвестно → unknown, без клика
            "var f=mk(true,false);window.__vpcChk=undefined;"
            "out.push(fb(f)+':'+f.inp.clicks+':'+f.lab.clicks);"
            "console.log(out.join('|'));")
        res = run_node(lbl_js).split("|")
        check("label: наш клик уже перещёлкнул контрол — второго щелчка нет",
              res[0:1] == ["flipped:0:0"])
        check("label: клик прошёл мимо label-механики — один input.click()",
              res[1:2] == ["flipped:1:0"])
        check("label: включённый контрол наш клик выключил — обратно не щёлкаем",
              res[2:3] == ["flipped:0:0"])
        check("label без чекбокса: сам label повторно не кликается",
              res[3:4] == [":-:0"])
        check("label: состояние «до» неизвестно — без повторного щелчка",
              res[4:5] == ["unknown:0:0"])
        # Отпечаток зоны (psig в _ALL_CLICKABLE_BOXES_JS) и отпечаток перед
        # кликом (_POINT_SIG_JS) обязаны совпадать на одной и той же странице
        _bx = ba._js_fill(ba._ALL_CLICKABLE_BOXES_JS, MIN=8)
        _psig = _bx[_bx.index("function psig("):_bx.index("all.forEach")]
        sig_js = (
            "var el={tagName:'BUTTON',id:'buy',title:'',innerText:'Купить  сейчас',"
            "getAttribute:function(){return null;}};"
            "global.document={elementFromPoint:function(){return el;}};"
            "global.window={scrollX:0,scrollY:640.4};" + _psig +
            "var a=JSON.stringify(psig(5,5));"
            "var b=(" + ba._POINT_SIG_JS + ")([5,5]);"
            "console.log(a===b?'same':a+' vs '+b);")
        check("отпечаток зоны при резолве == отпечаток перед кликом (формат)",
              run_node(sig_js) == "same")
    else:
        print("  [SKIP] node не найден — JS-проверки label пропущены")
    check("label-фолбэк: pre-замер встроен в замер «до» (ноль лишних вызовов)",
          ba._LABEL_PRE_JS in ba._state_js(5)
          and ba._LABEL_PRE_JS not in ba._state_js())
    import re as _re
    _lclick = _re.compile(r"(?<![\w.])l\.click\(\)")
    check("label-фолбэк: ни в CDP, ни в AppleScript нет l.click()",
          not _lclick.search(inspect.getsource(ba._click_applescript))
          and not _lclick.search(inspect.getsource(ba._label_toggle_js))
          and not _lclick.search(ba._LABEL_TOGGLE_BODY_JS))

    # ── 8. Корзина: опрос эффекта вместо одного замера через 0.6 с ──
    try:
        patch(ba, "CART_VERIFY_SEC", 0.5)
        patch(ba, "CART_POLL_SEC", 0.01)
        states = []

        def _cart_js(host, js, tab_id=None, front=False, **kw):
            if "return 'ok:clicked'" in js:
                return "ok:clicked"
            return json.dumps(states.pop(0) if len(states) > 1 else states[0])

        patch(ba, "_run_js", _cart_js)
        states[:] = [{"present": True, "qty": 1}, {"present": True, "qty": 1},
                     {"present": False, "qty": None}]
        r1 = ba.cart_op("x.ru", "пицца", "remove")
        check("корзина remove: товар исчез не сразу — опрос дождался",
              r1.get("status") == "ok")
        states[:] = [{"present": True, "qty": 1}]
        err_c = ""
        try:
            ba.cart_op("x.ru", "пицца", "remove")
        except ba.BrowserUnavailable as e:
            err_c = str(e)
        check("корзина remove: товар так и остался — честное «не сработал»",
              "всё ещё в корзине" in err_c)
        # increase: замер «до» (qty 2), потом 2, 2, 3 — ждём смены числа
        states[:] = [{"present": True, "qty": 2}, {"present": True, "qty": 2},
                     {"present": True, "qty": 2}, {"present": True, "qty": 3}]
        r3 = ba.cart_op("x.ru", "пицца", "increase")
        check("корзина increase: новое количество после перерисовки",
              r3.get("qty") == 3)
        states[:] = [{"present": True, "qty": 1}, {"present": False,
                                                    "qty": None}]
        r4 = ba.cart_op("x.ru", "пицца", "decrease")
        check("корзина decrease при 1 шт — товар ушёл, qty=0", r4.get("qty") == 0)
    finally:
        restore()
    try:
        base_mocks()
        patch(ba, "cart_op", lambda host, product, op, tab_id=None: (
            _ for _ in ()).throw(ba.BrowserUnavailable(
                f"«{product}» всё ещё в корзине — клик не сработал")))
        m = mgr("cart")
        act = {"kind": "cart", "op": "remove", "product": "Пепперони фреш",
               "host": "x.ru"}
        m.execute(act, "c8")
        rec = last_audit("cart")
        check("аудит корзины: товар и операция в записи",
              rec.get("product") == "Пепперони фреш"
              and rec.get("op") == "remove")
    finally:
        restore()

    # ── 9. Слайдер ──
    class _SlPage:
        def __init__(self, verify_seq):
            self.seq = list(verify_seq)
            self.applies = 0

        def evaluate(self, js, *a):
            if js.startswith("(function(label"):
                self.applies += 1
                return json.dumps({"st": "range", "v": 40})
            if js == ba._SLIDER_VERIFY_JS:
                return self.seq.pop(0) if self.seq else ""
            return ""

    try:
        patch(ba, "_select_backend", lambda tab_op=True: "cdp")
        patch(ba, "SLIDER_SETTLE_SEC", 0.0)
        patch(ba, "SLIDER_RETRY_SETTLE_SEC", 0.0)
        pg = _SlPage(["10", "40"])
        patch(ba._WORKER, "submit", lambda fn, timeout=None: fn(
            type("W", (), {"page_for": lambda self, h, t: pg})()))
        got = ba.set_slider("x.ru", "яркость", 40)
        check("слайдер: значение не принято — один повтор, принято со 2-го",
              got == "40" and pg.applies == 2)
        pg2 = _SlPage(["10", "10"])
        patch(ba._WORKER, "submit", lambda fn, timeout=None: fn(
            type("W", (), {"page_for": lambda self, h, t: pg2})()))
        err_s = ""
        try:
            ba.set_slider("x.ru", "яркость", 40)
        except ba.BrowserUnavailable as e:
            err_s = str(e)
        check("слайдер: и повтор не принят — честный отказ, повторов не больше",
              "не принял значение" in err_s and pg2.applies == 2)
    finally:
        restore()

    if node:
        # Подбор слайдера в JS на поддельном DOM: aria-label плеера
        sl_js = (
            "function El(id,aria){this.id=id;this.a={'aria-label':aria};"
            "this.tagName='DIV';this.parentElement=null;this.innerText='';"
            "this.children=[];this.title='';}"
            "El.prototype.getBoundingClientRect=function(){"
            "return {left:0,top:0,width:100,height:10};};"
            "El.prototype.getAttribute=function(n){"
            "return this.a[n]===undefined?null:this.a[n];};"
            "El.prototype.setAttribute=function(n,v){this.a[n]=v;};"
            "El.prototype.closest=function(){return null;};"
            "var els=[];var document={querySelectorAll:function(){return els;},"
            "documentElement:{children:[]},getElementById:function(){return null;}};"
            "var window={scrollX:0,scrollY:0,HTMLInputElement:{prototype:{}}};"
            "function run(list,label){els=list;var r=JSON.parse(eval("
            "SRC.replace('__L__',JSON.stringify(label))));"
            "return r.st==='custom'?r.dbg:r.st;}"
            "var SRC=" + json.dumps(ba._SET_SLIDER_JS % ("__L__", 40, '""'))
            + ";"
            "var two=function(){return [new El('seek','Seek slider'),"
            "new El('vol','Громкость')];};"
            "console.log([run(two(),'звук'),run(two(),'перемотка'),"
            "run(two(),'volume'),run([new El('only','Яркость')],'слайдер'),"
            "run(two(),'слайдер'),run(two(),'ползунок')].join('|'));")
        res = run_node(sl_js).split("|")
        check("слайдер: «звук» → ползунок «Громкость» (синоним)",
              res[0:1] == ["div#vol"])
        check("слайдер: «перемотка» → «Seek slider» (синоним)",
              res[1:2] == ["div#seek"])
        check("слайдер: «volume» → «Громкость» (кросс-язык)",
              res[2:3] == ["div#vol"])
        check("слайдер: родовое «слайдер» при одном ползунке — берём его",
              res[3:4] == ["div#only"])
        check("слайдер: родовое «слайдер»/«ползунок» при двух — no-match "
              "(«ползунок» не липнет к «Seek slider»)",
              res[4:6] == ["no-match", "no-match"])
    else:
        print("  [SKIP] node не найден — JS-проверки слайдера пропущены")

    try:
        base_mocks()
        mv_calls, ss_calls = [], []

        def _mv(host, op, tab_id=None):
            mv_calls.append(op)
            return "vol:" + str(int(round(float(op[1:]) * 100)))

        patch(ba, "media_volume_op", _mv)
        patch(ba, "set_slider", lambda *a, **kw: (ss_calls.append(a), "0")[1])
        m = mgr("vol")
        act = {"kind": "slider", "host": "youtube.com", "slider_label": "громкость",
               "slider_value": 40, "slider_unit": "pct", "element": "громкость"}
        okv, _ = m.execute(act, "c9")
        rec = last_audit("vol")
        check("громкость при живом видео — напрямую у <video>, без слайдера",
              okv and mv_calls == ["=0.4"] and not ss_calls
              and act.get("slider_done") == "40%"
              and rec.get("slider_path") == "media_vol")
        check("громкость через <video>: отчёт с процентом",
              "40%" in ComputerControlManager.describe_done(act))

        def _no_video(host, op, tab_id=None):
            raise ba.BrowserUnavailable("нет видео на странице")

        patch(ba, "media_volume_op", _no_video)
        act2 = dict(act)
        act2.pop("slider_done", None)
        okv2, _ = m.execute(act2, "c9")
        check("громкость без видео — обычный слайдер",
              okv2 and len(ss_calls) == 1)
        mv_calls.clear()
        patch(ba, "media_volume_op", _mv)
        act3 = {"kind": "slider", "host": "x.ru", "slider_label": "яркость",
                "slider_value": 40, "slider_unit": "pct", "element": "яркость"}
        m.execute(act3, "c9")
        check("не громкость — <video> не трогаем", not mv_calls)
    finally:
        restore()
    if node:
        mv_js = ("var v={volume:0.9,muted:true,paused:false,"
                 "getBoundingClientRect:function(){return {width:10,height:10};}};"
                 "var document={querySelectorAll:function(){return [v];}};"
                 "var r=eval(" + json.dumps(
                     ba._js_fill(ba._MEDIA_VOLUME_JS, OP="=0.25")) + ");"
                 "console.log(r+'|'+v.volume+'|'+v.muted);")
        check("media_volume_op «=0.25»: абсолютная громкость и снятие мьюта",
              run_node(mv_js) == "vol:25|0.25|false")

    # ── 10. nav: готовность до первого шага, след шагов на осечке ──
    try:
        base_mocks()
        order = []
        patch(cc, "NAV_LOAD_TIMEOUT_SEC", 0.2)
        patch(cc, "NAV_POLL_SEC", 0.02)
        patch(ba, "open_new_tab", lambda url, focus=True: (
            order.append("open"), 5)[1])
        patch(ba, "backend_forced", lambda: True)
        patch(ba, "wait_dom_idle", lambda *a, **kw: order.append("idle"))

        def _snap_empty(host=None, tab_id=None):
            order.append("snap")
            raise ba.BrowserUnavailable("нет кликабельных элементов")

        patch(ba, "snapshot_elements", _snap_empty)
        m = mgr("nav")
        act = {"kind": "nav", "value": "https://example.edu/",
               "host": "example.edu", "steps": ["Студентам", "Расписание"],
               "element": "Студентам → Расписание"}
        okn, detn = m.execute(act, "c10")
        rec = last_audit("nav")
        check("nav: ждём готовности страницы ДО первого снапшота",
              order[:3] == ["open", "idle", "snap"])
        check("nav: пустая страница — честная осечка с именем шага",
              okn is False and "Студентам" in detn)
        check("nav: след шагов (no_items) — в аудите и на осечке",
              "no_items" in str(rec.get("path") or ""))
    finally:
        restore()

    print(f"\n{ok} OK, {fail} FAIL")


if __name__ == "__main__":
    main()

"""Тест снимка: обёртка кнопки карточки — одна строка с кнопкой (стенд
20.10, «закажи пиццу» на вымышленной пиццерии: после названий карточек
список элементов вырос с 57 до 75 строк — подвал карточки «[div] от 459 ₽
Выбрать» и кнопка «[button] Выбрать» внутри него шли двумя строками).

Проверяет:
* дедуп снимка (_merge_card_wraps): подвал карточки и кнопка в нём — одна
  строка, кнопка; её text прежний («Выбрать» — сверка с узлом при клике),
  текст обёртки — в wrap; в строке модели шагов (_elem_line) и в показе
  каталога (_show_label) — название, цена и действие; span цены с
  поднятым от подвала текстом — туда же; текст, поднятый от карточки
  («Вода негазированная 0,5 л 79 ₽ Выбрать»), — в кнопку своей карточки
  (по названию рядом), не соседней;
* разные действия не сводятся: «Выбрать» и «В избранное» в одной обёртке
  (текстом и иконкой), «−» и «+», ссылка товара и «В корзину», поле в
  обёртке, окно товара (cn там — заголовок окна у всех), та же карточка
  далеко, ссылка-обёртка (href — сама цель), кнопка перекрыта чужим слоем;
* блок-раздел в строке модели: у «Выбрать» без цены — только название
  карточки, если в блоке названия других карточек (чужая цена «от 119 ₽»
  у сока); свой блок, где название не первым, — как был;
* настоящий движок (headless-Chromium Playwright, главная стенда —
  scripts/fixtures/stand_pizzatest_home.html — перехватом запроса, без
  сети; нет Playwright/Chromium — SKIP): одна строка на карточку, разделы
  и названия на месте; клик по номеру строки — в кнопку (метка снимка на
  button, сверка подписи _LABEL_MATCH_FN, клик открывает окно товара); ход
  агента «click n» с подписью строки доходит до диспетчера номером
  кнопки и подписью «Выбрать»; разделы по разметке — без модели; размер
  списка до правки и после: не больше прогона 1 (57 строк, 4490 символов).

Запуск: python3 -m scripts.test_snapshot_card_dedup
"""

import glob
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.features import browser_actions as _ba  # noqa: E402
from app.features import cc_privacy as _cp  # noqa: E402
from app.features import task_agent as ta  # noqa: E402
from app.features.computer_control import _active_layer  # noqa: E402
from app.features.task_agent import TaskAgent  # noqa: E402
from scripts.test_task_agent import ScriptedRouter, real_cc  # noqa: E402

FAILS = 0
OKS = 0


def check(name, cond, detail=""):
    global FAILS, OKS
    OKS += 1
    if cond:
        print(f"  [OK] {name}")
    else:
        FAILS += 1
        print(f"  [FAIL] {name} {detail}")


# Браузера бота в тестах нет: ожидание, разметка разделов, листание,
# целевой снимок — заглушки (часть с движком подменяет разделы своими)
_ba.wait_dom_idle = lambda *a, **k: None
_ba.section_items = lambda *a, **k: None
_ba.section_names = lambda *a, **k: None
_ba.scroll_step = lambda *a, **k: {"moved": True, "bottom": False}
_ba.scroll_container_step = lambda *a, **k: {"moved": True, "bottom": False,
                                             "y0": 0}
_ba.snapshot_for_goal = lambda host, text, tab_id=None: ("", [])
# Приватная страница не зовёт живую локальную модель (Ollama)
_cp.PrivateRouter.get_response = lambda self, msgs, **kw: None
TMP = Path(tempfile.mkdtemp(prefix="snap_card_dedup_"))
el = TaskAgent._elem_line


def it(idx, tag, text, x, y, w, h, cn="", ctx="", **kw):
    return {"idx": idx, "tag": tag, "role": kw.pop("role", ""), "text": text,
            "href": kw.pop("href", ""), "x": float(x), "y": float(y),
            "w": float(w), "h": float(h), "cn": cn, "ctx": ctx, "vp": True,
            **kw}


def ids(items):
    return [i["idx"] for i in items]


def dedup(items):
    return _ba._dedup_snapshot_items([dict(x) for x in items],
                                     "https://pizzatest.test/")


# ── 1. Подвал карточки и кнопка в нём — одна строка ──
print("Обёртка кнопки карточки — в кнопку")
CTX_P = "Пепперони Пикантная пепперони, моцарелла, томатный соус от 459 ₽ Выбрать"
CTX_M = "Маргарита Моцарелла, томаты, итальянские травы от 399 ₽ Выбрать"
pair = [
    # Подвал карточки (pointer унаследован), span цены с поднятым текстом
    # подвала, кнопка внутри подвала; так же — соседняя карточка
    it(1, "div", "от 459 ₽ Выбрать", 32, 643, 283, 36, "Пепперони", CTX_P),
    it(2, "span", "от 459 ₽ Выбрать", 32, 651, 61, 20, "Пепперони", CTX_P),
    it(3, "div", "от 399 ₽ Выбрать", 343, 643, 283, 36, "Маргарита", CTX_M),
    it(4, "span", "от 399 ₽ Выбрать", 343, 651, 61, 20, "Маргарита", CTX_M),
    it(30, "button", "Выбрать", 214, 643, 101, 36, "Пепперони", CTX_P),
    it(31, "button", "Выбрать", 525, 643, 101, 36, "Маргарита", CTX_M)]
out = dedup(pair)
by = {i["idx"]: i for i in out}
check("подвал + span цены + кнопка — остаётся только кнопка, у каждой "
      "карточки своя", ids(out) == [30, 31], ids(out))
check("у кнопки text прежний («Выбрать» — сверка с узлом при клике), "
      "текст обёртки — в wrap",
      by.get(30, {}).get("text") == "Выбрать"
      and by.get(30, {}).get("wrap") == "от 459 ₽ Выбрать"
      and by.get(31, {}).get("wrap") == "от 399 ₽ Выбрать", out)
check("подпись действия (_action_label) — «Выбрать», подпись для модели "
      "(_item_label) — «от 459 ₽ Выбрать»",
      ta._action_label(by.get(30, {})) == "Выбрать"
      and ta._item_label(by.get(30, {})) == "от 459 ₽ Выбрать")
line = el(1, by.get(30, {}))
check("строка модели шагов: «[button/-] от 459 ₽ Выбрать (block: "
      "Пепперони)» — название, цена, действие",
      line == "1) [button/-] от 459 ₽ Выбрать (block: Пепперони)", line)
check("показ каталога: «Пепперони — от 459 ₽»",
      ta._show_label(by.get(30, {})) == "Пепперони — от 459 ₽",
      ta._show_label(by.get(30, {})))
check("подпись модели «от 459 ₽ Выбрать» и «Выбрать» сходятся с кнопкой "
      "(_label_fits)",
      ta._label_fits("от 459 ₽ Выбрать", ta._label_of(by.get(30, {})))
      and ta._label_fits("Выбрать", ta._label_of(by.get(30, {}))))
check("_pick_shown: две карточки — две строки",
      ids(ta._pick_shown(out)) == [30, 31], ids(ta._pick_shown(out)))

# Текст, поднятый от карточки («0,5 л» — служебный, взят текст карточки):
# фрагмент не содержит кнопку, но он той же карточки — тот же cn рядом
SEC = ("Напитки Кола Газированный напиток от 119 ₽ Выбрать Кола Зеро "
       "Газированный напиток без сахара от 119 ₽ Выбрать Вода")
lifted = [
    it(5, "p", "Вода негазированная 0,5 л 79 ₽ Выбрать", 32, 2938, 283, 16,
       "Вода негазированная", SEC),
    it(6, "div", "79 ₽ Выбрать", 32, 2964, 283, 36, "Вода негазированная",
       SEC),
    it(7, "p", "Холодный чай лимон 0,5 л 129 ₽ Выбрать", 343, 2938, 283, 16,
       "Холодный чай лимон", SEC),
    it(8, "div", "Холодный чай лимон 0,5 л 129 ₽ Выбрать", 343, 2964, 283,
       36, "Холодный чай лимон", SEC),
    it(40, "button", "Выбрать", 214, 2964, 101, 36, "Вода негазированная",
       SEC),
    it(41, "button", "Выбрать", 525, 2964, 101, 36, "Холодный чай лимон",
       SEC)]
out = dedup(lifted)
by = {i["idx"]: i for i in out}
check("текст карточки, поднятый на «0,5 л», — в кнопку своей карточки: "
      "самый полный («Вода негазированная 0,5 л 79 ₽ Выбрать», не «79 ₽ "
      "Выбрать»), соседняя — своё",
      ids(out) == [40, 41]
      and by[40].get("wrap") == "Вода негазированная 0,5 л 79 ₽ Выбрать"
      and by[41].get("wrap") == "Холодный чай лимон 0,5 л 129 ₽ Выбрать",
      out)
check("строка — с названием из самого текста, без блока-раздела",
      el(1, by.get(40, {}), {"Вода негазированная", "Холодный чай лимон"})
      == "1) [button/-] Вода негазированная 0,5 л 79 ₽ Выбрать",
      el(1, by.get(40, {})))

# ── 2. Разные действия — не сводим ──
print("Разные действия — не сводятся")
fav_text = [
    it(1, "div", "от 459 ₽ В избранное Выбрать", 32, 643, 283, 36,
       "Пепперони", CTX_P),
    it(30, "button", "В избранное", 120, 643, 90, 36, "Пепперони", CTX_P),
    it(31, "button", "Выбрать", 214, 643, 101, 36, "Пепперони", CTX_P)]
out = dedup(fav_text)
check("«Выбрать» и «В избранное» в одной обёртке — все три строки, без wrap",
      ids(out) == [1, 30, 31] and not any(i.get("wrap") for i in out),
      out)
fav_icon = [
    it(1, "div", "от 459 ₽ Выбрать", 32, 643, 283, 36, "Пепперони", CTX_P),
    it(30, "button", "", 180, 647, 28, 28, "Пепперони", CTX_P,
       aria="В избранное"),
    it(31, "button", "Выбрать", 214, 643, 101, 36, "Пепперони", CTX_P)]
out = dedup(fav_icon)
check("иконка «В избранное» без текста в той же обёртке — не сводим",
      ids(out) == [1, 30, 31] and not any(i.get("wrap") for i in out), out)
qty = [
    it(1, "div", "− 1 +", 20, 300, 120, 30, "Пепперони", "Пепперони − 1 +"),
    it(30, "button", "−", 20, 300, 28, 28, "Пепперони", "Пепперони − 1 +"),
    it(31, "button", "+", 100, 300, 28, 28, "Пепперони", "Пепперони − 1 +")]
out = dedup(qty)
check("«−» и «+» количества — не сводим", ids(out) == [1, 30, 31]
      and not any(i.get("wrap") for i in out), out)
link_cart = [
    it(1, "div", "Пепперони от 459 ₽ В корзину", 32, 595, 283, 90,
       "Пепперони", CTX_P),
    it(2, "a", "Пепперони", 32, 600, 150, 24, "Пепперони", CTX_P,
       href="https://pizzatest.test/p/pepperoni"),
    it(30, "button", "В корзину", 214, 643, 101, 36, "Пепперони", CTX_P)]
out = dedup(link_cart)
check("ссылка на страницу товара и «В корзину» в одной обёртке: все три "
      "строки, обёртка — не в одну из них",
      ids(out) == [1, 2, 30] and not any(i.get("wrap") for i in out), out)
lifted2 = [
    it(1, "span", "Пепперони 459 ₽ В корзину", 32, 651, 61, 20, "Пепперони",
       CTX_P),
    it(2, "a", "Пепперони", 32, 600, 150, 24, "Пепперони", CTX_P,
       href="https://pizzatest.test/p/pepperoni"),
    it(30, "button", "В корзину", 214, 643, 101, 36, "Пепперони", CTX_P)]
out = dedup(lifted2)
check("поднятый текст с подписями ссылки и «В корзину» (та же карточка) — "
      "не в одну из них", ids(out) == [1, 2, 30]
      and not any(i.get("wrap") for i in out), out)
link_wrap = [
    it(1, "a", "от 459 ₽ Выбрать", 32, 643, 283, 36, "Пепперони", CTX_P,
       href="https://pizzatest.test/p/pepperoni"),
    it(30, "button", "Выбрать", 214, 643, 101, 36, "Пепперони", CTX_P)]
out = dedup(link_wrap)
check("ссылка-обёртка (href — своя цель) — не фрагмент, не сводим",
      ids(out) == [1, 30] and not any(i.get("wrap") for i in out), out)
field = [
    it(1, "div", "Количество Добавить", 20, 300, 260, 40, "Пепперони",
       CTX_P),
    it(30, "input", "Количество", 30, 305, 60, 30, "", CTX_P, ed=True),
    it(31, "button", "Добавить", 120, 305, 100, 30, "Пепперони", CTX_P)]
out = dedup(field)
check("поле внутри обёртки — не сводим", ids(out) == [1, 30, 31]
      and not any(i.get("wrap") for i in out), out)
modal = [
    it(1, "div", "Выбрать размер", 300, 300, 120, 30, "Пепперони",
       "× Пепперони 30 см", md=True),
    it(30, "button", "Выбрать", 300, 400, 120, 30, "Пепперони",
       "× Пепперони 30 см", md=True)]
out = dedup(modal)
check("окно товара: «Выбрать размер» рядом с «Выбрать» (cn — заголовок "
      "окна у обоих) — не сводим", ids(out) == [1, 30]
      and not any(i.get("wrap") for i in out), out)
far = [
    it(1, "div", "от 459 ₽ Выбрать", 32, 5000, 283, 36, "Пепперони", CTX_P),
    it(30, "button", "Выбрать", 214, 643, 101, 36, "Пепперони", CTX_P)]
out = dedup(far)
check("та же карточка далеко (другой раздел) — не сводим",
      ids(out) == [1, 30] and not any(i.get("wrap") for i in out), out)
two = [
    it(1, "div", "от 459 ₽ Выбрать", 32, 643, 283, 36, "", CTX_P),
    it(30, "button", "Выбрать", 214, 643, 101, 36, "", CTX_P),
    it(31, "button", "Выбрать", 214, 700, 101, 36, "", CTX_P)]
out = dedup(two)
check("без названия карточки: кнопка внутри — сводим, соседняя «Выбрать» "
      "вне обёртки — не мешает и не трогается",
      ids(out) == [30, 31] and out[0].get("wrap") == "от 459 ₽ Выбрать"
      and not out[1].get("wrap"), out)
covered = [
    it(1, "div", "от 459 ₽ Выбрать", 32, 643, 283, 36, "Пепперони", CTX_P),
    it(30, "button", "Выбрать", 214, 643, 101, 36, "Пепперони", CTX_P,
       cov=True)]
out = dedup(covered)
check("кнопка перекрыта чужим слоем, обёртка — нет: не сводим (строка "
      "карточки не пропадает с обёрткой)", ids(out) == [1, 30]
      and not any(i.get("wrap") for i in out), out)

# ── 3. Блок-раздел в строке модели шагов ──
print("Строка модели: блок-раздел — только название")
names = {"Кола", "Кола Зеро", "Вода негазированная", "Комбо на двоих"}
l_zero = el(1, it(34, "button", "Выбрать", 0, 0, 10, 10, "Кола Зеро", SEC),
            names)
check("«Выбрать» колы зеро в блоке-разделе («Напитки Кола … от 119 ₽») — "
      "«(block: Кола Зеро)»", l_zero == "1) [button/-] Выбрать (block: "
      "Кола Зеро)", l_zero)
l_old = el(1, it(34, "button", "Выбрать", 0, 0, 10, 10, "Кола Зеро", SEC))
check("без списка названий — как было («Кола Зеро · Напитки…»)",
      "(block: Кола Зеро · Напитки Кола" in l_old, l_old)
own = "Хит Кола Зеро Газированный напиток без сахара от 119 ₽ Выбрать"
l_own = el(1, it(34, "button", "Выбрать", 0, 0, 10, 10, "Кола Зеро", own),
           names)
check("свой блок, где название не первым («Хит Кола Зеро …»): «Кола» "
      "внутри «Кола Зеро» — не чужое название, блок как был",
      "(block: Кола Зеро · Хит Кола Зеро" in l_own, l_own)
l_named = el(1, it(35, "p", "Газированный напиток без сахара", 0, 0, 10, 10,
                   "Кола Зеро", SEC), names)
check("подпись с названием — без блока (как было)",
      l_named == "1) [p/-] Газированный напиток без сахара", l_named)


# ── 4. Настоящий движок: главная стенда ──
print("Настоящий движок (Playwright): главная стенда")
FIXTURE = Path(__file__).parent / "fixtures" / "stand_pizzatest_home.html"
# Прогон 1 стенда (до названий карточек): столько строк и символов в списке
# элементов главной — потолок для снимка с разделами и названиями
RUN1_LINES, RUN1_CHARS = 57, 4490


def _browser(pw):
    try:
        return pw.chromium.launch(headless=True)
    except Exception as e:
        # Версия пакета и скачанного браузера разошлись — любой
        # headless-shell из кэша Playwright (как в test_ba_sandbox)
        exes = sorted(glob.glob(str(
            Path.home() / "Library/Caches/ms-playwright"
            / "chromium_headless_shell-*/*/chrome-headless-shell"))
            + glob.glob(str(Path.home() / ".cache/ms-playwright"
                            / "chromium_headless_shell-*/*/"
                            "chrome-headless-shell")))
        for exe in reversed(exes):
            try:
                return pw.chromium.launch(headless=True, executable_path=exe)
            except Exception:
                continue
        print(f"  SKIP: headless Chromium не запускается ({str(e)[:120]})")
        return None


def _lines(items, merge=True):
    """Список элементов промпта модели шагов: как в _prompt. merge=False —
    как до правки: без свода обёрток и без списка названий."""
    saved = _ba._merge_card_wraps
    try:
        if not merge:
            _ba._merge_card_wraps = lambda its: its
        shown = ta._pick_shown(_active_layer(list(items)))
    finally:
        _ba._merge_card_wraps = saved
    nm = {ta._card_name(i) for i in shown} - {""} if merge else ()
    return shown, [el(n, i, nm) for n, i in enumerate(shown, 1)]


def engine_checks():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"  SKIP: нет playwright ({e})")
        return
    url0 = "https://pizzatest.ru/"
    html = FIXTURE.read_text(encoding="utf-8")
    pw = sync_playwright().start()
    br = _browser(pw)
    if br is None:
        pw.stop()
        return
    saved = _ba.section_names
    try:
        pg = br.new_page(viewport={"width": 1280, "height": 800})
        # Страница — перехватом запроса: адрес настоящий (якоря «/#pizza»,
        # location.href — как на стенде), сети нет
        pg.route("https://pizzatest.ru/**", lambda rt: rt.fulfill(
            body=html, content_type="text/html; charset=utf-8"))
        pg.goto(url0)
        # Как на стенде: баннер cookie закрыт, «Ваш город — Москва?» висит
        pg.evaluate("acceptCookie()")

        def raw_snap():
            return str(pg.evaluate(_ba._js_fill(
                _ba._SNAPSHOT_JS, BASE=_ba._mark_base(_ba.SNAPSHOT_MAX))) or "")

        raw = raw_snap()
        # До правки: тот же сырой снимок без свода обёрток
        saved_merge = _ba._merge_card_wraps
        _ba._merge_card_wraps = lambda its: its
        try:
            _u, items_old = _ba._parse_snapshot(raw)
        finally:
            _ba._merge_card_wraps = saved_merge
        _s_old, lines_old = _lines(items_old, merge=False)
        url, items = _ba._parse_snapshot(raw)
        shown, lines = _lines(items)
        block_old, block = "\n".join(lines_old), "\n".join(lines)
        print(f"    размер списка: до — {len(lines_old)} строк, "
              f"{len(block_old)} символов; после — {len(lines)} строк, "
              f"{len(block)} символов (прогон 1 — {RUN1_LINES}/{RUN1_CHARS})")
        # Строк — около прогона 1: в нём не было 5 ссылок разделов, а
        # «Выбрать» карточек комбо, закусок, напитков и десертов сливались
        # в одну строку на раздел (16 кнопок без своей строки)
        check(f"размер списка — не больше прогона 1 ({RUN1_CHARS} символов), "
              f"строк — не больше {RUN1_LINES + 3} и меньше, чем до правки",
              len(block) <= RUN1_CHARS and len(lines) <= RUN1_LINES + 3
              and len(lines) < len(lines_old)
              and len(block) < len(block_old),
              (len(lines), len(block), len(lines_old), len(block_old)))
        check("до правки — пары «[div] от 459 ₽ Выбрать» + «[button] "
              "Выбрать» (воспроизведение стенда)",
              any(x.startswith("1) [div/-] от 459 ₽ Выбрать")
                  for x in lines_old), lines_old[:3])
        check("ни одного фрагмента-подвала с «Выбрать»: «Выбрать» — только "
              "кнопки", not any(re.match(r"\d+\) \[(div|span|p)/", x)
                                and "Выбрать" in x for x in lines),
              [x for x in lines if "Выбрать" in x and "[button" not in x])
        for name, price in (("Пепперони", "от 459 ₽"), ("Цыплёнок ранч",
                                                       "от 499 ₽"),
                            ("Кола", "от 119 ₽"), ("Кола Зеро", "от 119 ₽"),
                            ("Картофель из печи", "от 189 ₽")):
            hit = [x for x in lines if f"(block: {name})" in x]
            check(f"«{name}»: одна строка — кнопка с ценой «{price}» и "
                  "названием", len(hit) == 1 and "[button/-] " + price
                  + " Выбрать" in hit[0], hit)
        hit = [x for x in lines if "Вода негазированная" in x]
        check("вода: одна строка — кнопка «Вода негазированная 0,5 л 79 ₽ "
              "Выбрать»", len(hit) == 1 and "[button/-] Вода негазированная "
              "0,5 л 79 ₽ Выбрать" in hit[0], hit)
        check("«Выбрать» комбо — с названием карточки, без блока-раздела "
              "(первая карточка раздела — не эта)",
              any(x.endswith("[button/-] Выбрать (block: Комбо для "
                             "компании) — below the visible area")
                  for x in lines)
              and not any("Комбо для компании · " in x for x in lines),
              [x for x in lines if "Комбо для" in x])
        check("разделы «Пицца», «Комбо», «Закуски», «Напитки», «Десерты» — "
              "в списке ссылками", all(any(f"[a/-] {s} (block:" in x
                                           for x in lines)
                                       for s in ("Пицца", "Комбо", "Закуски",
                                                 "Напитки", "Десерты")))

        # Клик по номеру строки — в кнопку
        n = next((k for k, x in enumerate(lines, 1)
                  if "(block: Пепперони)" in x), None)
        item = shown[n - 1] if n else {}
        check("номер строки «Пепперони» — кнопка «Выбрать» снимка",
              item.get("tag") == "button" and item.get("text") == "Выбрать",
              item)
        loc = pg.locator(_ba._mark_sel(int(item.get("idx") or 0)))
        tag = pg.evaluate("(s)=>{var e=document.querySelector(s);"
                          "return e?e.tagName+'|'+e.innerText:''}",
                          _ba._mark_sel(int(item.get("idx") or 0)))
        check("метка снимка под этим номером — на <button> «Выбрать»",
              tag == "BUTTON|Выбрать", tag)
        check("сверка подписи при клике (_LABEL_MATCH_FN, ожидание "
              "_action_label) — сходится",
              bool(_ba._eval_arg(loc, _ba._LABEL_MATCH_FN,
                                 ta._action_label(item))))
        loc.click()
        opened = pg.evaluate(
            "()=>document.getElementById('prodOv').classList.contains('open')"
            "&&(document.querySelector('#prodModal h2')||{}).textContent")
        check("клик по кнопке открыл окно «Пепперони» — то же, что сделал бы "
              "человек", opened == "Пепперони", opened)
        pg.evaluate("closeOv('prodOv')")

        # Ход агента: модель называет номер строки и её подпись
        cc = real_cc({url0: [dict(x) for x in items]}, url=url0,
                     base_dir=TMP)
        ag = TaskAgent(cc, memory_path=TMP / "click.json")
        goal = "закажи пепперони на pizzatest.ru"

        class Slots:
            def get_response(self, messages, **kw):
                p = messages[-1]["content"]
                if "You keep the brief" in p:
                    return json.dumps({"site": "pizzatest.ru", "items": [
                        {"name": "пепперони"}]}, ensure_ascii=False)
                return "NONE"
        ag.slot_router = Slots()
        rt = ScriptedRouter([{"action": "click", "n": n,
                              "label": "от 459 ₽ Выбрать",
                              "expect": "a dialog opens"},
                             {"action": "fail", "message": "стоп"}])
        ag.start("k1", goal, rt, user_id="A")
        step_p = next((p for p in rt.prompts if "Interactive elements" in p),
                      "")
        check("промпт шага: «[button/-] от 459 ₽ Выбрать (block: Пепперони)»"
              " — одной строкой, номер тот же",
              f"\n{n}) [button/-] от 459 ₽ Выбрать (block: Пепперони)"
              in step_p and step_p.count("от 459 ₽") == 1,
              re.findall(r"\n\d+\) [^\n]*459[^\n]*", step_p))
        cl = [d for d in cc.dispatched if d.get("kind") == "click"]
        check("клик агента — номер кнопки и подпись «Выбрать» (ожидание "
              "сверки с узлом)", len(cl) == 1
              and cl[0].get("idx") == item.get("idx")
              and cl[0].get("element") == "Выбрать",
              cl or cc.dispatched)

        # Разделы по разметке — на этой странице, без модели
        _ba.section_names = lambda host, tab_id=None: json.loads(
            pg.evaluate(_ba._SECTION_NAMES_JS))
        obs = {"host": "pizzatest.ru", "tab_id": 1, "url": url,
               "shown": shown}
        secs = TaskAgent._markup_sections(obs)
        check("_markup_sections по снимку после свода — пять разделов",
              secs == ["Пицца", "Комбо", "Закуски", "Напитки", "Десерты"],
              secs)

        # Показ каталога человеку: разделов нет, обзор модели — с чужим
        # названием → список кодом по снимку после свода
        class Show:
            def get_response(self, messages, **kw):
                p = messages[-1]["content"]
                if "You keep the brief" in p:
                    return "{}"
                if "Present it:" in p:
                    return "- Тирамису — 350 ₽\nКакую?"
                return "NONE"
        _ba.section_names = lambda *a, **k: []

        def show(its, chat):
            cc = real_cc({url0: [dict(x) for x in its]}, url=url0,
                         base_dir=TMP)
            ag = TaskAgent(cc, memory_path=TMP / f"{chat}.json")
            ag.slot_router = Show()
            r = ag.start(chat, "закажи пиццу на pizzatest.ru", ScriptedRouter(
                [{"action": "fail", "message": "стоп"}]), user_id="A")
            return r, [x for x in r.splitlines() if x.startswith("- ")]
        r, rows = show(items, "k2")
        r_old, rows_old = show(items_old, "k3")
        check("список кодом: пиццы с названиями («Пепперони — от 459 ₽»), "
              "без «- от …» и голого «Выбрать», без повторов",
              rows[:2] == ["- Пепперони — от 459 ₽", "- Маргарита — от 399 ₽"]
              and not any(x.startswith("- от ") or x == "- Выбрать"
                          for x in rows) and len(rows) == len(set(rows)), r)
        check("список кодом — тот же, что до правки (порядок карточек "
              "прежний: свод встаёт на место подвала)", rows == rows_old,
              (rows, rows_old))
    finally:
        _ba.section_names = saved
        br.close()
        pw.stop()


engine_checks()

print(f"\nИтог: {OKS} проверок, провалов: {FAILS}")
sys.exit(1 if FAILS else 0)

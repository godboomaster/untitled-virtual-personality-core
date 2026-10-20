"""Тест показа каталога агента задач: названия товаров и разделов (стенд
20.10, «закажи пиццу» на вымышленной пиццерии).

Проверяет:
* дедуп снапшота (_dedup_snapshot_items/_snap_link_key): якоря разделов
  этой же страницы (/#pizza, /#combo) не сливаются ни между собой, ни с
  логотипом «/»; настоящие дубли (одна ссылка товара дважды в карточке)
  сливаются; «#»-пустышки JS-кнопок, адрес неизвестен, ссылка на другую
  страницу — как раньше (_link_key);
* название карточки (поле cn снимка, _CARD_NAME_FN_JS) на настоящем
  движке (headless-Chromium Playwright, страница отдаётся перехватом —
  без сети): заголовок карточки, без бейджа «Новинка»; блок — весь раздел
  (колы), а название у каждой своё; подпись картинки без заголовка;
  бейдж — не название раздела; без названия — пусто; целевой снимок —
  тоже с cn. Нет Playwright/Chromium — SKIP этой части;
* разделы по разметке на странице как у стенда — без вызова модели;
* показ раздела: подпись-цена без названия («от 459 ₽ Выбрать») — с
  названием из блока карточки («Пепперони — от 459 ₽»); названия нет и
  там — не строка товара; одна карточка — одна строка; обзор модели
  сверяется по названиям (с названиями страницы — человеку, с чужими —
  список кодом); названия видит и модель шагов (_elem_line);
* вопрос о товаре: название из заголовка окна — без «×» и описания
  («× Кола Газированный напиток» → «Кола»); без заголовка — без глифов;
* быстрый разбор выбора из нового показа (_brief_fast): номер и название
  — позиция без цены, без вызова модели; из обзора модели — тоже.

Запуск: python3 -m scripts.test_task_browse_names
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


class SlotRouter:
    """slot_router по виду промпта: разделы, показ, «что спросить?» —
    текст; разбор ответа — JSON (нет в slots — {}). brief — промпты
    разбора: столько раз ответу понадобилась модель."""

    def __init__(self, sections="NONE", show="", ask="NONE", slots=None):
        self.sections, self.show, self.ask = sections, show, ask
        self.slots = dict(slots or {})
        self.prompts, self.brief = [], []

    def get_response(self, messages, **kw):
        p = messages[-1]["content"]
        self.prompts.append(p)
        if "You keep the brief" in p:
            self.brief.append(p)
            m = re.search(r'User\'s answer: "(.*)"\n', p)
            return json.dumps(self.slots.get(m.group(1) if m else "", {}),
                              ensure_ascii=False)
        if "sections of the site's catalog" in p:
            return self.sections
        if "Present it:" in p:
            return self.show
        if "What do you need to ask the user before this step?" in p:
            return self.ask
        return "{}"

    def calls(self, mark):
        return sum(mark in p for p in self.prompts)


# Браузера бота в тестах нет: разметка, листание, целевой снимок — заглушки
# (часть с настоящим движком подменяет их своими)
_ba.wait_dom_idle = lambda *a, **k: None
_ba.section_items = lambda *a, **k: None
_ba.section_names = lambda *a, **k: None
_ba.scroll_step = lambda *a, **k: {"moved": True, "bottom": False}
_ba.scroll_container_step = lambda *a, **k: {"moved": True, "bottom": False,
                                             "y0": 0}
_ba.snapshot_for_goal = lambda host, text, tab_id=None: ("", [])
# Приватная страница не зовёт живую локальную модель (Ollama)
_cp.PrivateRouter.get_response = lambda self, msgs, **kw: None
TMP = Path(tempfile.mkdtemp(prefix="task_browse_names_"))


def it(idx, tag, text, href="", x=0.0, y=0.0, w=80.0, h=20.0, **kw):
    return {"idx": idx, "tag": tag, "role": kw.pop("role", ""), "text": text,
            "href": href, "x": x, "y": y, "w": w, "h": h, **kw}


def ids(items):
    return [i["idx"] for i in items]


# ── 1. Дедуп снапшота: якоря разделов этой страницы ──
print("Дедуп: якоря разделов не сливаются, дубли — сливаются")
HOME = "https://pizzatest.test/"
nav = [it(1, "a", "Пицца Тест Доставка пиццы за 35 минут", HOME, x=20, y=10,
          w=200, h=40),
       it(2, "a", "Пицца", HOME + "#pizza", x=20, y=70, w=50),
       it(3, "a", "Комбо", HOME + "#combo", x=90, y=70, w=50),
       it(4, "a", "Напитки", HOME + "#drinks", x=160, y=70, w=60),
       it(5, "a", "Десерты", HOME + "#desserts", x=240, y=70, w=60)]
dd = _ba._dedup_snapshot_items(nav, HOME)
check("стенд 20.10: «/», «/#pizza», «/#combo»… рядом — все живы (разные "
      "цели одной страницы)", ids(dd) == [1, 2, 3, 4, 5], ids(dd))
check("тот же якорь дважды рядом (меню сверху и подписью) — один",
      ids(_ba._dedup_snapshot_items(
          nav[1:2] + [it(6, "a", "Пиццы и закуски", HOME + "#pizza", x=24,
                         y=74, w=120)], HOME)) == [6])
card = [it(10, "a", "1:03", "https://pizzatest.test/p/pepperoni?utm_source=x",
           x=10, y=300, w=200, h=150),
        it(11, "a", "Пепперони — пикантная классика",
           "https://pizzatest.test/p/pepperoni", x=10, y=455, w=200, h=24)]
check("настоящий дубль: ссылка товара дважды в карточке — одна, с "
      "информативной подписью", ids(_ba._dedup_snapshot_items(card, HOME))
      == [11])
rev = [it(12, "a", "Пепперони — пикантная классика",
          "https://pizzatest.test/p/pepperoni", x=10, y=455, w=200),
       it(13, "a", "12 отзывов", "https://pizzatest.test/p/pepperoni#reviews",
          x=10, y=480, w=80)]
check("ссылка на другую страницу с фрагментом («#отзывы» той же карточки) "
      "— как раньше, одна цель", ids(_ba._dedup_snapshot_items(rev, HOME))
      == [12])
check("адрес страницы неизвестен — как раньше (_link_key без фрагмента)",
      len(_ba._dedup_snapshot_items(nav)) < len(nav))
js_btns = [it(20, "a", "Пепперони", HOME + "#", x=10, y=600, w=90),
           it(21, "a", "Пепперони — пикантная классика", HOME + "#!", x=10,
              y=640, w=200)]
check("«#»/«#!»-пустышки JS-кнопок одной карточки — как раньше, одна цель",
      ids(_ba._dedup_snapshot_items(js_btns, HOME)) == [21])
pk = _ba._link_key(HOME)
check("_snap_link_key: якорь этой страницы — свой ключ, «/» и «#!» — ключ "
      "страницы, другая страница — _link_key",
      _ba._snap_link_key(HOME + "#pizza", pk) != _ba._snap_link_key(
          HOME + "#combo", pk)
      and _ba._snap_link_key(HOME + "#pizza", pk) != pk
      and _ba._snap_link_key(HOME, pk) == pk
      and _ba._snap_link_key(HOME + "#!", pk) == pk
      and _ba._snap_link_key(HOME + "p/1#reviews", pk)
      == _ba._link_key(HOME + "p/1"))

# ── 2. Показ без браузера: подписи стенда с названием карточки (cn) ──
print("Показ: названия из блока карточки")
SEC_DRINKS = ("Напитки Кола Газированный напиток от 119 ₽ Выбрать Кола Зеро "
              "Газированный напиток без сахара от 119 ₽ Выбрать Вода")
PZ = [("Пепперони", 459, "Пикантная пепперони, моцарелла, томатный соус"),
      ("Маргарита", 399, "Моцарелла, томаты, итальянские травы"),
      ("Четыре сыра", 519, "Сыр блю чиз, чеддер, пармезан")]


def row(idx, tag, text, cn, ctx, **kw):
    return {"idx": idx, "tag": tag, "role": "", "text": text, "cn": cn,
            "ctx": ctx, "vp": True, **kw}


page = [row(1, "a", "Пицца Тест Доставка пиццы за 35 минут", "",
            "Пицца Тест Доставка пиццы за 35 минут Корзина 0"),
        row(2, "button", "Корзина 0", "",
            "Пицца Тест Доставка пиццы за 35 минут Корзина 0")]
for i, (n, p, d) in enumerate(PZ):
    ctx = f"{n} {d} от {p} ₽ Выбрать"
    page += [row(10 + i, "div", f"от {p} ₽ Выбрать", n, ctx),
             row(20 + i, "button", "Выбрать", n, ctx)]
page += [row(30, "div", "от 119 ₽ Выбрать", "Кола", SEC_DRINKS),
         row(31, "div", "от 119 ₽ Выбрать", "Кола Зеро", SEC_DRINKS),
         row(32, "p", "Вода негазированная 0,5 л 79 ₽ Выбрать",
             "Вода негазированная", SEC_DRINKS),
         row(33, "div", "79 ₽ Выбрать", "Вода негазированная", SEC_DRINKS),
         row(34, "button", "Выбрать", "Кола Зеро", SEC_DRINKS),
         # Карточка без заголовка и подписи картинки — названия нет нигде
         row(40, "div", "от 249 ₽ Выбрать", "",
             "Хит Сырники 4 шт., со сгущенным молоком от 249 ₽ Выбрать")]
CODE_LIST = ("На pizzatest.test:\n- Пепперони — от 459 ₽\n- Маргарита — от "
             "399 ₽\n- Четыре сыра — от 519 ₽\n- Кола — от 119 ₽\n- Кола Зеро "
             "— от 119 ₽\n- Вода негазированная 0,5 л 79 ₽ Выбрать\n"
             "Что заказываем?")


def fresh(name, show, slots=None):
    cc = real_cc({HOME: page}, url=HOME, base_dir=TMP)
    ag = TaskAgent(cc, memory_path=TMP / f"{name}.json")
    ag.slot_router = SlotRouter(show=show, slots=slots)
    return ag


def stop():
    return ScriptedRouter([{"action": "fail", "message": "стоп"}])


# Обзор модели с позицией не со страницы — список кодом
ag = fresh("s1", "- Гавайская — 500 ₽\n- Пепперони — от 459 ₽\nКакую?")
r = ag.start("s1", "закажи пиццу на pizzatest.test", stop(), user_id="A")
check("обзор модели с чужим названием («Гавайская») — человеку список кода "
      "с названиями карточек, а не «от 459 ₽ Выбрать»",
      CODE_LIST in r and "Гавайская" not in r and "- от " not in r, r)
check("подпись без названия и в блоке («от 249 ₽ Выбрать» без cn) — не "
      "строка товара; голое «Выбрать» — тоже", "249" not in r
      and "\n- Выбрать" not in r, r)
check("одна карточка — одна строка: «79 ₽ Выбрать» воды не дала вторую "
      "«Вода негазированная — 79 ₽»", "Вода негазированная — 79" not in r, r)
show_p = next((p for p in ag.slot_router.prompts if "Present it:" in p), "")
check("модели показа ушли подписи с названиями («Пепперони — от 459 ₽», "
      "«Кола Зеро — от 119 ₽»), не голые цены",
      "- Пепперони — от 459 ₽\n" in show_p and "- Кола Зеро — от 119 ₽" in show_p
      and "- от 459 ₽ Выбрать" not in show_p, show_p[-1500:])
aw = dict(ag._runs["s1"]["awaiting"])
check("варианты показа — товары с названиями (pick item, listed)",
      aw.get("pick") == "item" and aw.get("listed")
      and aw.get("items", [])[:2] == ["Пепперони — от 459 ₽",
                                       "Маргарита — от 399 ₽"], aw)

# Быстрый разбор выбора из списка кода: позиция — без цены, без модели
for ans, want in (("2", "Маргарита"), ("пепперони", "Пепперони"),
                  ("четыре сыра", "Четыре сыра"), ("кола зеро", None)):
    ag = fresh(f"f-{want}", "- Гавайская — 500 ₽\nКакую?")
    ag.start("f", "закажи пиццу на pizzatest.test", stop(), user_id="A")
    aw = dict(ag._runs["f"]["awaiting"])
    run = ag._runs["f"]
    run["qa"].append((aw["question"], ans))
    n0 = len(ag.slot_router.brief)
    ch = ag._update_brief(run, aw["question"], ans, private=False, aw=aw)
    if want is None:
        # «Кола» ⊂ «Кола Зеро»: какой вариант назван, код не решает — модель
        check(f"список кода, ответ «{ans}» при «Кола» и «Кола Зеро» — "
              "разбор модели, не угадывание",
              len(ag.slot_router.brief) == n0 + 1, ch)
        continue
    check(f"список кода, ответ «{ans}» — позиция «{want}» без цены, без "
          "вызова модели", ch == {"items": [{"name": want}]}
          and len(ag.slot_router.brief) == n0, (ch, len(ag.slot_router.brief)))
check("_option_item строки нового показа — название без цены",
      ta._option_item("Пепперони — от 459 ₽") == "Пепперони"
      and ta._option_item("Кола Зеро — от 119 ₽") == "Кола Зеро")

# Обзор модели с названиями страницы — человеку он, и выбор из него — тоже
# без модели
MODEL_LIST = ("Пицца Тест, пиццы:\n- Пепперони — от 459 ₽\n- Маргарита — от "
              "399 ₽\n- Четыре сыра — от 519 ₽\nЧто закажем?")
ag = fresh("s2", MODEL_LIST)
r = ag.start("s2", "закажи пиццу на pizzatest.test", stop(), user_id="A")
check("обзор модели с названиями карточек — принят (сверка по названиям, "
      "не по «Выбрать»)", r.endswith(MODEL_LIST), r)
aw = dict(ag._runs["s2"]["awaiting"])
run = ag._runs["s2"]
run["qa"].append((aw["question"], "четыре сыра"))
n0 = len(ag.slot_router.brief)
ch = ag._update_brief(run, aw["question"], "четыре сыра", private=False, aw=aw)
check("выбор из обзора модели («четыре сыра») — позиция без цены, без "
      "модели", ch == {"items": [{"name": "Четыре сыра"}]}
      and len(ag.slot_router.brief) == n0, ch)

# Модель шагов: название карточки у подписей без названия
el = TaskAgent._elem_line
l_price = el(1, row(10, "div", "от 459 ₽ Выбрать", "Пепперони",
                    "Пепперони Пикантная пепперони от 459 ₽ Выбрать"))
l_zero = el(2, row(34, "button", "Выбрать", "Кола Зеро", SEC_DRINKS))
l_pz = el(3, row(20, "button", "Выбрать", "Пепперони",
                 "Пепперони Пикантная пепперони от 459 ₽ Выбрать"))
l_named = el(4, row(32, "p", "Вода негазированная 0,5 л 79 ₽ Выбрать",
                    "Вода негазированная", SEC_DRINKS))
check("строка модели шагов: «от 459 ₽ Выбрать (block: Пепперони)»; «Выбрать»"
      " колы зеро — «(block: Кола Зеро · Напитки…)»; блок, что уже начинается "
      "с названия, и подпись с названием — как были",
      "от 459 ₽ Выбрать (block: Пепперони)" in l_price
      and "Выбрать (block: Кола Зеро · Напитки Кола" in l_zero
      and "Выбрать (block: Пепперони Пикантная" in l_pz
      and l_named.endswith("Вода негазированная 0,5 л 79 ₽ Выбрать"),
      (l_price, l_zero, l_pz, l_named))
picked = ta._pick_shown([row(30, "div", "от 119 ₽ Выбрать", "Кола", SEC_DRINKS),
                         row(31, "div", "от 119 ₽ Выбрать", "Кола Зеро",
                             SEC_DRINKS),
                         row(35, "span", "от 119 ₽ Выбрать", "Кола Зеро",
                             SEC_DRINKS)])
check("_pick_shown: одинаковые подписи колы и колы зеро в одном блоке — "
      "оба товара, кусок той же карточки — схлопнут", ids(picked) == [30, 31],
      ids(picked))

# ── 3. Вопрос о товаре: название без «×» и описания ──
print("Вопрос о товаре: чистое название")
pk_ = TaskAgent._product_key
add = {"idx": 50, "tag": "button", "text": "В корзину · 119 ₽", "md": True,
       "ctx": "× Кола Газированный напиток 0,5 л 1 л В корзину · 119 ₽"}
obs0 = {"url": HOME}
check("стенд 20.10: «× Кола Газированный напиток» → заголовок окна «Кола»",
      pk_(dict(add, cn="Кола"), obs0) == "Кола")
check("заголовка нет — без глифа «×» (описание — как было)",
      pk_(add, obs0) == "Кола Газированный напиток")
check("чужой заголовок окна («Добавить по вкусу») ключ не подменяет",
      pk_(dict(add, cn="Добавить по вкусу"), obs0)
      == "Кола Газированный напиток")
check("кнопка закрытия с текстом «Закрыть» — тоже не в названии",
      pk_(dict(add, ctx="Закрыть Пепперони 30 см, традиционное тесто "
               "В корзину · 599 ₽"), obs0) == "Пепперони")
card_add = {"idx": 51, "tag": "button", "text": "В корзину", "md": False,
            "ctx": SEC_DRINKS.replace("Выбрать", "В корзину")}
check("«В корзину» в карточке раздела (блок — весь раздел «Напитки Кола…»):"
      " ключ — заголовок карточки, у колы и колы зеро разный",
      pk_(dict(card_add, cn="Кола Зеро"), obs0) == "Кола Зеро"
      and pk_(dict(card_add, cn="Кола"), obs0) == "Кола",
      (pk_(dict(card_add, cn="Кола Зеро"), obs0), pk_(card_add, obs0)))
ag = fresh("q1", "x", slots=None)
ag.slot_router.ask = "Тесто: традиционное или тонкое? Сейчас выбрано традиционное."
ag.start("q1", "закажи пепперони на pizzatest.test", stop(), user_id="A")
run = ag._runs.get("q1") or ag.__dict__.get("_finished", {}).get("q1", {}).get(
    "run") or {}
win = [{"idx": 60, "tag": "button", "text": "×", "aria": "Закрыть",
        "md": True, "cn": "Пепперони",
        "ctx": "× Пепперони 30 см, традиционное тесто, 610 г Пикантная "
               "пепперони Средняя 30 см Традиционное Тонкое В корзину · 599 ₽"},
       {"idx": 61, "tag": "button", "text": "Традиционное", "md": True,
        "on": 1, "cn": "Пепперони", "ctx": ""},
       {"idx": 62, "tag": "button", "text": "Тонкое", "md": True, "on": 0,
        "cn": "Пепперони", "ctx": ""}]
btn = {"idx": 63, "tag": "button", "text": "В корзину · 599 ₽", "md": True,
       "cn": "Пепперони", "ctx": win[0]["ctx"]}
run.setdefault("item_asked", [])
q = ag._item_questions(run, btn, {"url": HOME, "host": "pizzatest.test",
                                  "shown": win + [btn]})
check("вопрос о товаре: «Перед тем как положить «Пепперони» в корзину» — "
      "без «×»", bool(q) and "«Пепперони» в корзину" in q and "×" not in q, q)

# ── 4. Настоящий движок: страница как у стенда ──
print("Настоящий движок (Playwright): разметка стенда")


def _card(name, desc, price, tag="", alt=None, head=True):
    img = (f"<img alt='{alt}' width=40 height=40 src='data:image/gif;base64,"
           "R0lGODlhAQABAAAAACw='>" if alt is not None else
           "<img width=40 height=40 src='data:image/gif;base64,"
           "R0lGODlhAQABAAAAACw='>")
    tg = f"<span class='tag'>{tag}</span>" if tag else ""
    nm = f"<h3>{name}{tg}</h3>" if head else f"<div class='nm'>{name}</div>"
    return (f"<article class='card' onclick='openProd(\"{name}\")'>{img}{nm}"
            f"<p>{desc}</p><div class='cfoot'><span class='cprice'>{price}"
            "</span><button class='btn ghost' onclick='event.stopPropagation();"
            f"openProd(\"{name}\")'>Выбрать</button></div></article>")


def _sec(sid, title, cards):
    return (f"<section class='menu' id='{sid}'><h2>{title}</h2>"
            f"<div class='grid'>{''.join(cards)}</div></section>")


SHOP_HTML = (
    "<!doctype html><html lang='ru'><head><meta charset='utf-8'><style>"
    "body{margin:0;font-family:sans-serif}a{color:inherit}"
    "header{display:flex;gap:16px;padding:10px}nav{display:flex;gap:18px;"
    "padding:10px}.grid{display:grid;grid-template-columns:repeat(4,1fr);"
    "gap:20px}article.card{cursor:pointer;display:flex;flex-direction:column}"
    ".cfoot{display:flex;justify-content:space-between}"
    ".tag{background:#f60;color:#fff;font-size:11px}"
    ".overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;"
    "z-index:50}.overlay.open{display:flex}.modal{background:#fff;width:600px;"
    "position:relative;display:flex}"
    "</style></head><body>"
    "<header><a class='logo' href='/'>Пицца Тест<small> Доставка пиццы за 35 "
    "минут</small></a><button id='cart'>Корзина 0</button></header>"
    "<nav><a href='#pizza'>Пицца</a><a href='/#combo'>Комбо</a>"
    "<a href='/#drinks'>Напитки</a><a href='/#desserts'>Десерты</a>"
    "<a href='/promo'>Акции</a><a href='/contacts'>Контакты</a></nav><main>"
    + _sec("pizza", "Пицца", [
        _card("Пепперони", "Пикантная пепперони, моцарелла, томатный соус",
              "от 459 ₽", alt="Пепперони"),
        _card("Маргарита", "Моцарелла, томаты, итальянские травы", "от 399 ₽"),
        _card("Цыплёнок ранч", "Цыпленок, ветчина, соус ранч", "от 499 ₽",
              tag="Новинка")])
    + _sec("combo", "Комбо", [
        _card("Комбо на двоих", "2 пиццы 25 см и 2 напитка", "1 199 ₽"),
        _card("Обед на одного", "Пицца 25 см, закуска и напиток", "649 ₽")])
    + _sec("drinks", "Напитки", [
        _card("Кола", "Газированный напиток", "от 119 ₽"),
        _card("Кола Зеро", "Газированный напиток без сахара", "от 119 ₽")])
    + _sec("desserts", "Десерты", [
        _card("Чизкейк", "Классический творожный", "от 189 ₽",
              alt="Чизкейк", head=False),
        _card("Сырники", "4 шт., со сгущенным молоком", "от 219 ₽",
              head=False)])
    + "</main>"
    "<div class='overlay' id='prodOv'><div class='modal' role='dialog' "
    "aria-modal='true' id='prodModal'></div></div>"
    "<script>function openProd(n){var drink=/Кола/.test(n),h='';"
    "h+='<button class=\"x\" aria-label=\"Закрыть\" onclick=\"document."
    "getElementById(\\'prodOv\\').classList.remove(\\'open\\')\">×</button>';"
    "h+='<div class=\"mright\"><h2>'+n+'</h2>';"
    "if(drink){h+='<p>Газированный напиток</p><div role=\"radiogroup\">"
    "<button role=\"radio\" aria-checked=\"true\">0,5 л</button>"
    "<button role=\"radio\" aria-checked=\"false\">1 л</button></div>"
    "<button class=\"btn add\">В корзину · 119 ₽</button>';}"
    "else{h+='<p>30 см, традиционное тесто, 610 г</p><p>Пикантная пепперони, "
    "моцарелла</p><div role=\"radiogroup\"><button role=\"radio\" "
    "aria-checked=\"true\">Средняя 30 см</button><button role=\"radio\" "
    "aria-checked=\"false\">Большая 35 см</button></div><h4>Добавить по вкусу"
    "</h4><div class=\"tops\"><button class=\"top\">Сырный бортик 169 ₽"
    "</button><button class=\"top\">Халапеньо 59 ₽</button></div>"
    "<button class=\"btn add\">В корзину · 599 ₽</button>';}"
    "document.getElementById('prodModal').innerHTML=h+'</div>';"
    "document.getElementById('prodOv').classList.add('open');}</script>"
    "</body></html>")


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


def engine_checks():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"  SKIP: нет playwright ({e})")
        return
    URL = "https://shop.test/"
    pw = sync_playwright().start()
    br = _browser(pw)
    if br is None:
        pw.stop()
        return
    saved = (_ba.section_names, _ba.section_items)
    try:
        pg = br.new_page(viewport={"width": 1280, "height": 800})
        # Страница — перехватом запроса: адрес настоящий (якоря «/#pizza»
        # и location.href — как на сайте), сети нет
        pg.route("https://shop.test/**", lambda rt: rt.fulfill(
            body=SHOP_HTML, content_type="text/html; charset=utf-8"))
        pg.goto(URL)

        def snap():
            raw = str(pg.evaluate(_ba._js_fill(
                _ba._SNAPSHOT_JS, BASE=_ba._mark_base(_ba.SNAPSHOT_MAX))) or "")
            return _ba._parse_snapshot(raw)

        url, items = snap()
        labs = [i["text"] for i in items]
        check("снимок: ссылки разделов «Пицца», «Комбо», «Напитки», «Десерты» "
              "живы рядом с логотипом «/»", url == URL and all(
                  x in labs for x in ("Пицца", "Комбо", "Напитки", "Десерты"))
              and any(x.startswith("Пицца Тест") for x in labs), labs)

        def cn_of(text, after=None):
            # Подвал карточки «от 459 ₽ Выбрать» сведён в кнопку «Выбрать»
            # (поле wrap, _merge_card_wraps) — ищем и по нему
            hits = [i for i in items if (i.get("wrap") or i["text"]) == text
                    and (after is None or after in i["ctx"])]
            return [i.get("cn") for i in hits]

        check("cn: «от 459 ₽ Выбрать» — «Пепперони» (заголовок карточки)",
              set(cn_of("от 459 ₽ Выбрать")) == {"Пепперони"},
              cn_of("от 459 ₽ Выбрать"))
        check("cn: «от 119 ₽ Выбрать» колы и колы зеро (блок — весь раздел) — "
              "у каждой своё", set(cn_of("от 119 ₽ Выбрать"))
              == {"Кола", "Кола Зеро"}, cn_of("от 119 ₽ Выбрать"))
        check("cn: «Цыплёнок ранч» — без бейджа «Новинка»",
              set(cn_of("от 499 ₽ Выбрать")) == {"Цыплёнок ранч"},
              cn_of("от 499 ₽ Выбрать"))
        check("cn бейджа «Новинка» — не название раздела «Пицца»",
              all(c != "Пицца" for c in cn_of("Новинка")), cn_of("Новинка"))
        check("cn: карточка без заголовка — подпись картинки, если это строка "
              "карточки («Чизкейк»); без неё — пусто («Сырники»)",
              set(cn_of("от 189 ₽ Выбрать")) == {"Чизкейк"}
              and set(cn_of("от 219 ₽ Выбрать")) == {""},
              (cn_of("от 189 ₽ Выбрать"), cn_of("от 219 ₽ Выбрать")))
        check("cn у шапки и навигации — пусто",
              not any(i.get("cn") for i in items
                      if i["text"] in ("Пицца", "Корзина 0", "Контакты")))

        # Разделы по разметке: section_names — на этой странице
        _ba.section_names = lambda host, tab_id=None: json.loads(
            pg.evaluate(_ba._SECTION_NAMES_JS))
        obs = {"host": "shop.test", "tab_id": 1, "url": URL,
               "shown": ta._pick_shown(items)}
        check("_markup_sections на странице как у стенда — разделы без "
              "модели", TaskAgent._markup_sections(obs)
              == ["Пицца", "Комбо", "Напитки", "Десерты"],
              TaskAgent._markup_sections(obs))
        cc = real_cc({URL: items}, url=URL, base_dir=TMP)
        ag = TaskAgent(cc, memory_path=TMP / "eng.json")
        ag.slot_router = SlotRouter(sections="NONE")
        r = ag.start("e1", "закажи пиццу на shop.test", stop(), user_id="A")
        check("вопрос о разделах — по разметке, вызова модели о разделах нет",
              "На shop.test есть разделы:\n- Пицца\n- Комбо\n- Напитки\n"
              "- Десерты\n" in r and ag.slot_router.calls(
                  "sections of the site's catalog") == 0, r)
        _ba.section_names = lambda *a, **k: []
        cc = real_cc({URL: items}, url=URL, base_dir=TMP)
        ag = TaskAgent(cc, memory_path=TMP / "eng2.json")
        ag.slot_router = SlotRouter(show="- Гавайская — 500 ₽\nКакую?")
        r = ag.start("e2", "закажи пиццу на shop.test", stop(), user_id="A")
        check("разделов нет — список кода по настоящему снимку с названиями: "
              "«Пепперони — от 459 ₽», «Кола Зеро — от 119 ₽», «Чизкейк — от 189 "
              "₽»;"
              " «Сырники» без названия — мимо",
              "- Пепперони — от 459 ₽\n" in r and "- Кола Зеро — от 119 ₽" in r
              and "- Чизкейк — от 189 ₽" in r and "219" not in r, r)

        # Целевой снимок (find) — тоже с cn
        raw = str(pg.evaluate(_ba._js_fill(
            _ba._GOAL_SNAPSHOT_JS, GOAL="Кола Зеро",
            BASE=_ba._mark_base(_ba.GOAL_SNAPSHOT_MAX))) or "")
        g = json.loads(raw).get("items") or []
        check("целевой снимок: у «Выбрать» колы зеро cn «Кола Зеро»",
              any(x.get("text") == "Выбрать" and x.get("cn") == "Кола Зеро"
                  for x in g), [(x.get("text"), x.get("cn")) for x in g])

        # Окно товара: заголовок окна — название в вопросе о товаре
        for prod, label, want in (("Пепперони", "В корзину · 599 ₽",
                                   "Пепперони"),
                                  ("Кола", "В корзину · 119 ₽", "Кола")):
            pg.evaluate(f"openProd({json.dumps(prod)})")
            _u, its = snap()
            btn = next((i for i in its if i["text"] == label and i["md"]),
                       None)
            key = TaskAgent._product_key(btn or {}, {"url": URL})
            check(f"окно «{prod}»: cn кнопки «{label}» — «{want}», ключ товара "
                  f"«{want}» (без «×» и описания)",
                  btn is not None and btn.get("cn") == want and key == want,
                  (btn and btn.get("cn"), btn and btn.get("ctx"), key))
            pg.evaluate("document.getElementById('prodOv').classList"
                        ".remove('open')")
        # Шторка корзины: «+»/«−» позиции и подзаголовок допродажи — «+» не
        # получает чужое название «Добавить к заказу?»
        pg.evaluate(
            "var d=document.createElement('div');d.setAttribute('role',"
            "'dialog');d.style.cssText='position:fixed;top:0;right:0;"
            "width:400px;height:100%;background:#fff;z-index:60';"
            "d.innerHTML='<div class=\"dbody\"><div class=\"citem\"><b>"
            "Пепперони</b><div class=\"qty\"><button>−</button><span>1</span>"
            "<button>+</button></div></div><h4>Добавить к заказу?</h4><div "
            "class=\"up\">Соус сырный 49 ₽ <button aria-label=\"Добавить соус\""
            ">+</button></div></div>';document.body.appendChild(d);")
        _u, its = snap()
        plus = [i for i in its if i["text"] == "+" and i["md"]]
        check("шторка корзины: у «+» позиции нет чужого названия "
              "(«Добавить к заказу?» — подзаголовок шторки)",
              plus and all(i.get("cn") != "Добавить к заказу?" for i in plus),
              [(i["text"], i.get("aria"), i.get("cn")) for i in plus])
    finally:
        _ba.section_names, _ba.section_items = saved
        br.close()
        pw.stop()


engine_checks()

print(f"\nИтог: {OKS} проверок, провалов: {FAILS}")
sys.exit(1 if FAILS else 0)

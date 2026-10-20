"""Тест разбора без модели голых ответов на вопрос МОДЕЛИ шагов с
вариантами-товарами и на вопросы о товаре перед «В корзину» (TaskAgent:
_ask_pick, _gate_pick, _gate_fast → _brief_fast). Лишние вызовы нашёл третий
прогон стенда 21.10 (разборы брифа 13, 31, 35).

Проверяет:
* вопрос модели «Какой напиток? - Кола — от 119 ₽ - Кола Зеро — …» в задаче
  заказа (каждый вариант — товар снимка: название и цена): номер и точное
  название → позиция без вызова модели — как вернула бы модель; позиция с
  объёмом в названии, уже названная, неоднозначный номер, номер вне
  диапазона, приписка — модели;
* варианты не товары (адрес, время, размер, магазин), вопрос о замене или с
  отрицанием, открытое окно товара, цель-справка, приватная страница, цена
  не та — без пометки, разбор модели; варианты — разделы магазина → бриф как
  был, без модели;
* вопросы о товаре перед «В корзину» (гейт): «как есть»/«оставь»/«так и
  клади»/«as is»/«keep it» — позиция как выбрано (объём — выбранный в окне),
  «0,5» ↔ «0,5 л», «тонкое» — без модели; многосоставное, отрицание, вопрос,
  количество, номер при вариантах списком, вариант не из вопроса,
  неоднозначный — модели;
* повтор «В корзину» кодом после ответа (_replay_step) — тот же, что при
  разборе моделью: «как есть»/«0,5» — клик кодом, «1 л»/«тонкое» — ход
  модели;
* EN-формы; лог — одна строка без значений; в run["traffic"] разбора нет;
* run3: сколько разборов брифа ушло бы к модели.

Браузер — настоящий гейт ComputerControlManager с заглушкой _dispatch
(real_cc), модели — скриптованные: живых браузеров, веб-чатов и LLM нет.

Запуск: PYTHONPATH=. python3 scripts/test_task_brief_fast_more.py
"""

import json
import logging
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
    """slot_router по виду промпта: разбор ответа — JSON по ответу человека
    (нет в slots — {}), «что спросить перед шагом?» — ask, разделы и показ
    раздела — NONE. brief — промпты разбора: столько раз ответу
    понадобилась модель."""

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


class LogLines(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


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
    # Приватная страница не зовёт живую локальную модель (Ollama): вызовы
    # считаем, ответа нет
    local_calls = []
    _cp.PrivateRouter.get_response = (
        lambda self, msgs, **kw: local_calls.append(msgs[-1]["content"]))
    tmp = Path(tempfile.mkdtemp(prefix="brief_fast_more_"))
    log = LogLines()
    logging.getLogger("app.features.task_agent").addHandler(log)
    logging.getLogger("app.features.task_agent").setLevel(logging.INFO)
    n_mem = [0]

    def agent(cc, slot):
        n_mem[0] += 1
        ag = TaskAgent(cc, memory_path=tmp / f"m{n_mem[0]}.json")
        ag.slot_router = slot
        return ag

    def clicks(cc):
        return [d.get("idx") for d in cc.dispatched
                if d.get("kind") == "click"]

    def finished(ag, chat):
        return (ag.__dict__.get("_finished", {}).get(chat) or {}).get("run") \
            or ag._runs.get(chat) or {}

    def brief_calls(run):
        return ((run.get("traffic") or {}).get("brief") or [0])[0]

    SHOP = "https://pizzatest.test/"
    DL, ADDED = SHOP + "delivery", SHOP + "?added=1"

    def card(i, name, price):
        # Карточка каталога: подпись-цена без названия, название — заголовок
        # карточки (cn), как у pizzatest на стенде
        return {"idx": i, "tag": "button", "role": "button",
                "text": f"от {price} ₽ Выбрать", "cn": name, "ctx": name}
    menu = [card(1, "Пепперони", 459), card(2, "Маргарита", 399),
            card(3, "Кола", 119), card(4, "Кола Зеро", 119),
            {"idx": 5, "tag": "button", "role": "button",
             "text": "Морс клюквенный 0,5 л 139 ₽ Выбрать"},
            {"idx": 6, "tag": "button", "role": "button",
             "text": "Сок апельсиновый 1 л 199 ₽ Выбрать"},
            {"idx": 7, "tag": "a", "role": "link", "text": "Пицца"},
            {"idx": 8, "tag": "a", "role": "link", "text": "Напитки"},
            {"idx": 9, "tag": "a", "role": "link",
             "text": "Кола в подарок к заказу от 1 500 ₽"},
            {"idx": 10, "tag": "button", "role": "button", "text": "Корзина",
             "aria": "Корзина"}]
    drinks_q = ("Какой напиток заказать?\n- Кола — от 119 ₽\n- Кола Зеро — от "
                "119 ₽\n- Морс клюквенный 0,5 л — 139 ₽\n- Сок апельсиновый 1 л "
                "— 199 ₽")
    dgoal = "закажи напиток в pizzatest.test"

    def new_run(**kw):
        return dict({"goal": dgoal, "qa": [], "history": [], "lang": "ru",
                     "chat_id": "u", "steps": 0, "sites": []}, **kw)

    def ask_aw(ag, question, run=None, shown=None, private=False,
               kind="ask"):
        # Вопрос модели шагов (ask или done с вопросом) — через _act, как в
        # ходе: пометка вариантов ставится там
        run = run or new_run()
        obs = {"host": "pizzatest.test", "url": SHOP, "private": private,
               "shown": [dict(x) for x in (shown or menu)]}
        TaskAgent._note_seen(run, obs["shown"])
        act = ({"action": "ask", "question": question} if kind == "ask"
               else {"action": "done", "message": question})
        ag._act(run, "u", ScriptedRouter([]), act, obs)
        return run, run.get("awaiting") or {}

    def parse(ag, run, aw, answer):
        # Ответ на ожидание aw — как в feed: пара в qa, затем разбор
        run["qa"].append((aw.get("question"), answer))
        n0 = len(ag.slot_router.brief)
        ch = ag._update_brief(run, aw.get("question"), answer,
                              private=bool(aw.get("private")), aw=aw)
        return ch, len(ag.slot_router.brief) - n0

    def items(run):
        return [(x["name"], x.get("size"), x.get("options"), x["qty"],
                 x["src"]) for x in (run.get("brief") or {}).get("items") or ()]

    # ── 1. Вопрос модели с вариантами-товарами ──
    print("вопрос модели — товары страницы:")
    ag = agent(real_cc({}, base_dir=tmp), SlotRouter())
    run, aw = ask_aw(ag, drinks_q)
    check("вопрос модели «Какой напиток? - Кола — от 119 ₽ …» в задаче заказа "
          "— с пометкой выбора позиции (вопрос модели) и товарами снимка",
          aw.get("pick") == "item" and aw.get("offer") and not aw.get("listed")
          and "Кола Зеро — от 119 ₽" in (aw.get("items") or ())
          and aw["question"] == drinks_q, f"{aw}")
    for ans, name in (("2", "Кола Зеро"), ("1", "Кола"),
                      ("кола", "Кола"), ("Кола!", "Кола")):
        run, aw = ask_aw(ag, drinks_q)
        ch, n = parse(ag, run, aw, ans)
        check(f"«{ans}» → без модели, позиция «{name}» (как вызов 31 стенда)",
              n == 0 and ch == {"items": [{"name": name}]}
              and items(run) == [(name, None, [], 1, "user")], f"{ch} {n}")
    for ans, why in (("морс", "название с объёмом — позиция или размер?"),
                     ("3", "название с объёмом"),
                     ("7", "номер вне диапазона"), ("0", "номер вне диапазона"),
                     ("1 или 2", "два варианта"), ("вторую и третью", "два"),
                     ("2 колы зеро", "количество"),
                     ("давай колу зеро", "другая форма слова — неточно"),
                     ("Кола Зеро", "«Кола» ⊂ «Кола Зеро» — как в "
                      "test_task_browse_names, код не решает"),
                     ("кола зеро, но холодную", "приписка"),
                     ("кола без сахара", "отрицание/свое слово"),
                     ("на твой вкус", "выбор отдан агенту"),
                     ("что-нибудь", "свободный текст")):
        run, aw = ask_aw(ag, drinks_q)
        _ch, n = parse(ag, run, aw, ans)
        check(f"«{ans}» ({why}) — разбор модели", n == 1, f"{_ch}")
    # Уже названа в брифе — ещё одна (qty) или та же: решит модель
    run = new_run(brief=dict(ta._new_brief(), items=[
        {"name": "кола", "size": None, "options": [], "qty": 1,
         "src": "user"}]))
    run, aw = ask_aw(ag, drinks_q, run=run)
    _ch, n = parse(ag, run, aw, "2")
    check("позиция, уже названная в брифе («кола» ~ «Кола Зеро»): разбор "
          "модели", n == 1)
    # «done» с вопросом — тот же вопрос модели
    run, aw = ask_aw(ag, drinks_q, kind="done")
    ch, n = parse(ag, run, aw, "2")
    check("done модели с вопросом и вариантами-товарами — тоже без модели",
          aw.get("pick") == "item" and n == 0
          and ch == {"items": [{"name": "Кола Зеро"}]}, f"{aw} {ch}")
    # Одна карточка двумя подписями (ссылка с названием и кнопка-цена) —
    # тот же товар; два товара одного названия и цены, но разного объёма —
    # какой назван, код не знает
    twice = menu + [{"idx": 30, "tag": "a", "role": "link",
                     "text": "Кола Зеро от 119 ₽"}]
    run, aw = ask_aw(ag, drinks_q, shown=twice)
    ch, n = parse(ag, run, aw, "2")
    check("карточка двумя подписями («Кола Зеро от 119 ₽» и «Кола Зеро — от "
          "119 ₽») — тот же товар: без модели",
          aw.get("pick") == "item" and n == 0
          and ch == {"items": [{"name": "Кола Зеро"}]}, f"{aw.get('pick')}")
    mors = menu + [{"idx": 31, "tag": "button", "role": "button",
                    "text": "Морс клюквенный 1 л 139 ₽ Выбрать"}]
    run, aw = ask_aw(ag, "Какой напиток?\n- Кола — от 119 ₽\n- Морс "
                         "клюквенный — 139 ₽", shown=mors)
    check("«Морс клюквенный — 139 ₽» при морсе 0,5 л и 1 л по 139 ₽ — какой "
          "товар, код не знает: без пометки", "pick" not in aw, f"{aw}")
    # Цена варианта — не та, что у товара страницы (модель перепутала)
    run, aw = ask_aw(ag, "Какой напиток?\n- Кола — от 129 ₽\n- Кола Зеро — "
                         "от 119 ₽")
    _ch, n = parse(ag, run, aw, "2")
    check("цена варианта не у товара страницы — без пометки, разбор модели",
          "pick" not in aw and n == 1, f"{aw}")

    print("вопрос модели — не товары:")
    for q, ans, why in (
            ("Когда доставить?\n- Как можно скорее\n- К 19:00\n- К 20:00",
             "2", "время"),
            ("Куда доставить?\n- ул. Ленина, 5\n- ул. Тестовая, 1, кв. 2",
             "1", "адрес"),
            ("Как получить заказ?\n- Доставка — 149 ₽\n- Самовывоз — 0 ₽",
             "2", "способ получения"),
            ("Какой размер пепперони?\n- 25 см — 459 ₽\n- 30 см — 599 ₽",
             "2", "размер"),
            ("Где заказать?\n- Пицца Тест — от 459 ₽\n- Пицца Плюс — от 499 ₽",
             "1", "магазин"),
            ("Каких пицц хочется?\n- Мясные — Пепперони, Мясная\n- Сырные — "
             "Четыре сыра", "1", "обзор видов"),
            ("Пепперони закончилась. Что взять вместо?\n- Маргарита — от 399 "
             "₽\n- Кола — от 119 ₽", "1", "замена"),
            ("Что убрать из заказа?\n- Кола — от 119 ₽\n- Кола Зеро — от 119 "
             "₽", "1", "удаление"),
            ("Колы без сахара нет. Какую взять?\n- Кола — от 119 ₽\n- "
             "Маргарита — от 399 ₽", "1", "отрицание в вопросе")):
        run, aw = ask_aw(ag, q)
        _ch, n = parse(ag, run, aw, ans)
        check(f"варианты — {why}: без пометки, «{ans}» — разбор модели "
              "(в items — только от модели)",
              "pick" not in aw and n == 1 and not items(run), f"{aw}")
    # Открыто окно товара: варианты бывают его добавками
    win = menu + [{"idx": 20, "tag": "button", "role": "button",
                   "text": "В корзину · 119 ₽", "md": 1, "ctx": "Кола"}]
    run, aw = ask_aw(ag, drinks_q, shown=win)
    check("открыто окно товара — без пометки", "pick" not in aw, f"{aw}")
    run, aw = ask_aw(ag, drinks_q, private=True)
    check("приватная страница — без пометки (разбор — локальной модели)",
          "pick" not in aw, f"{aw}")
    run, aw = ask_aw(ag, drinks_q, run=new_run(
        goal="узнай, сколько стоит кола в pizzatest.test"))
    check("цель-справка (не покупка) — без пометки", "pick" not in aw,
          f"{aw}")
    # Варианты — разделы магазина (известные по _browse_sections): бриф как
    # был, раздел откроет модель шагов по паре вопрос–ответ
    secs = ["Пицца", "Напитки", "Десерты"]
    run, aw = ask_aw(ag, "Что посмотрим?\n- Пицца\n- Напитки\n- Десерты",
                     run=new_run(browse={"stage": "done", "sections": secs}))
    ch, n = parse(ag, run, aw, "напитки")
    check("варианты — разделы магазина: пометка раздела, «напитки» — без "
          "модели, бриф как был",
          aw.get("pick") == "section" and n == 0 and ch == {}
          and not items(run), f"{aw} {ch}")
    run, aw = ask_aw(ag, "Что-нибудь ещё?\n- Пицца\n- Напитки\n- Десерты",
                     run=new_run(browse={"stage": "done", "sections": secs},
                                 cart_adds=[{"key": "Кола", "verified": True}]))
    check("«что-нибудь ещё?» модели с разделами — без пометки (вид просьбы "
          "вынимает модель)", aw.get("more") and "pick" not in aw, f"{aw}")
    # EN: вопрос модели на английском сайте
    en_menu = [{"idx": 1, "tag": "button", "role": "button",
                "text": "from $1.19 Select", "cn": "Cola", "ctx": "Cola"},
               {"idx": 2, "tag": "button", "role": "button",
                "text": "from $1.19 Select", "cn": "Cola Zero",
                "ctx": "Cola Zero"},
               {"idx": 3, "tag": "button", "role": "button",
                "text": "Orange juice 1 l $1.99 Select"}]
    en_q = ("Which drink should I order?\n- Cola — from $1.19\n- Cola Zero — "
            "from $1.19\n- Orange juice 1 l — $1.99")
    for ans, name in (("2", "Cola Zero"), ("1", "Cola"), ("cola", "Cola")):
        run, aw = ask_aw(ag, en_q, shown=en_menu, run=new_run(
            goal="order a drink on pizzatest.test", lang="en"))
        ch, n = parse(ag, run, aw, ans)
        check(f"EN: «{ans}» → без модели, позиция «{name}»",
              aw.get("pick") == "item" and n == 0
              and ch == {"items": [{"name": name}]}, f"{aw.get('pick')} {ch}")
    run, aw = ask_aw(ag, en_q, shown=en_menu, run=new_run(
        goal="order a drink on pizzatest.test", lang="en"))
    _ch, n = parse(ag, run, aw, "cola zero, but cold")
    check("EN: «cola zero, but cold» — разбор модели", n == 1)

    # ── 2. Сквозной: как сценарий 3 стенда (вызовы 30–31) ──
    print("сквозной — вопрос модели:")
    dl_page = [{"idx": 1, "tag": "a", "role": "link",
                "text": "Пицца Тест Доставка пиццы за 35 минут"},
               {"idx": 2, "tag": "a", "role": "link", "text": "Контакты"}]
    cc = real_cc({DL: dl_page, SHOP: [dict(x) for x in menu]}, url=DL,
                 clicks={1: SHOP}, base_dir=tmp)
    slot = SlotRouter({dgoal: {"site": "pizzatest.test"}})
    ag = agent(cc, slot)
    rt = ScriptedRouter([
        {"action": "click", "n": 1,
         "label": "Пицца Тест Доставка пиццы за 35 минут"},
        {"action": "ask", "question": drinks_q},
        {"action": "fail", "message": "стоп"}])
    q = ag.start("e1", dgoal, rt, user_id="A")
    aw = dict((ag._runs.get("e1") or {}).get("awaiting") or {})
    nb, n0 = len(slot.brief), len(rt.prompts)
    log.lines.clear()
    ag.feed("e1", "2", rt, user_id="A")
    run_e = finished(ag, "e1")
    fast = [x for x in log.lines if "без модели" in x]
    check("сквозной: вопрос модели со списком напитков ушёл человеку с "
          "пометкой", "Кола Зеро — от 119 ₽" in q and aw.get("pick") == "item",
          f"{q!r} {aw.get('pick')}")
    check("сквозной: «2» — разбора моделью нет, позиция «Кола Зеро» в брифе",
          len(slot.brief) == nb and items(run_e) == [
              ("Кола Зеро", None, [], 1, "user")], f"{items(run_e)}")
    check("сквозной: ход модели шагов после ответа видит выбор (Q/A в "
          "промпте)", len(rt.prompts) == n0 + 1
          and "Кола Зеро" in rt.prompts[n0], f"{len(rt.prompts) - n0}")
    check("лог: одна строка «без модели (выбор позиции (вопрос модели))», "
          "без значений; в счёте трафика — только разбор цели",
          len(fast) == 1 and "вопрос модели" in fast[0]
          and fast[0].endswith(": items") and "Кола" not in fast[0]
          and brief_calls(run_e) == 1, f"{fast} {brief_calls(run_e)}")

    # ── 3. Вопросы о товаре перед «В корзину» (гейт) ──
    print("гейт корзины — кола:")
    cart0 = {"idx": 9, "tag": "button", "role": "button", "text": "Корзина",
             "aria": "Корзина"}
    added = [{"idx": 9, "tag": "button", "role": "button",
              "text": "Корзина 1 · 119 ₽", "aria": "Корзина"},
             {"idx": 10, "tag": "a", "role": "link", "text": "Меню"}]

    def cola_page(name="Кола Зеро", btn="В корзину · 119 ₽",
                  sizes=("0,5 л", "1 л")):
        ctx = f"{name} {' '.join(sizes)}"
        return [cart0, {"idx": 1, "tag": "button", "role": "button",
                        "text": btn, "md": 1, "ctx": ctx}] + [
            {"idx": 2 + i, "tag": "button", "role": "radio", "text": s,
             "md": 1, "on": int(i == 0), "ctx": ctx}
            for i, s in enumerate(sizes)]
    cgoal = "закажи колу зеро на pizzatest.test"
    cola_ask = "Объём Колы Зеро: 0,5 л или 1 л? Сейчас выбрано 0,5 л."

    def gate(chat, answer, goal=cgoal, page=None, ask=cola_ask,
             goal_items=None, lang=None, ch=None):
        page = page or cola_page()
        cc = real_cc({SHOP: [dict(x) for x in page], ADDED: added}, url=SHOP,
                     clicks={1: ADDED}, base_dir=tmp)
        if lang == "en":
            # Ход на английском (язык хода бот ставит менеджеру управления)
            cc.turn_lang = lambda: "en"
        slot = SlotRouter({goal: {"site": "pizzatest.test", "items": (
            goal_items or [{"name": "Кола Зеро"}])},
            **({answer: ch} if ch is not None else {})}, ask=ask)
        ag = agent(cc, slot)
        rt = ScriptedRouter([
            {"action": "click", "n": 1, "label": page[1]["text"],
             "expect": "the cart count grows"},
            {"action": "fail", "message": "stop"}])
        q = ag.start(chat, goal, rt, user_id="A", lang=lang)
        aw = dict((ag._runs.get(chat) or {}).get("awaiting") or {})
        nb, n0 = len(slot.brief), len(rt.prompts)
        log.lines.clear()
        r = ag.feed(chat, answer, rt, user_id="A")
        run = finished(ag, chat)
        return {"q": q, "aw": aw, "r": r, "model": len(slot.brief) - nb,
                "steps": len(rt.prompts) - n0, "clicks": clicks(cc),
                "items": items(run), "log": list(log.lines), "run": run}
    g = gate("k0", "как есть")
    check("до клика — вопрос о товаре с пометкой гейта: позиция брифа и "
          "переключатели окна",
          "Перед тем как положить «Кола Зеро» в корзину" in g["q"]
          and g["aw"].get("pick") == "gate"
          and g["aw"]["gate"]["name"] == "Кола Зеро"
          and g["aw"]["gate"]["opts"] == [["0,5 л", 1], ["1 л", 0]]
          and g["aw"].get("replay"), f"{g['aw']}")
    for ans in ("как есть", "Как есть.", "оставь", "оставь как есть",
                "так и клади", "пусть будет так", "ничего не меняй",
                "как есть, пожалуйста", "давай как есть"):
        g = gate(f"k-{ans}", ans)
        check(f"«{ans}» — без модели: позиция с выбранным объёмом «0,5 л» "
              "(как вызов 35), «В корзину» — кодом (повтор), без хода модели",
              g["model"] == 0 and g["items"] == [
                  ("Кола Зеро", "0,5 л", [], 1, "user")]
              and g["clicks"] == [1] and g["steps"] == 0,
              f"{g['model']} {g['items']} {g['clicks']} {g['steps']}")
    for ans in ("0,5", "0.5", "0,5 л", "объём 0,5"):
        g = gate(f"v-{ans}", ans)
        check(f"«{ans}» ↔ «0,5 л» — без модели, размер «0,5 л» (как вызов "
              "13), клик кодом",
              g["model"] == 0 and g["items"] == [
                  ("Кола Зеро", "0,5 л", [], 1, "user")]
              and g["clicks"] == [1] and g["steps"] == 0,
              f"{g['model']} {g['items']} {g['clicks']} {g['steps']}")
    for ans in ("1 л", "1", "давай 1 л"):
        g = gate(f"w-{ans}", ans)
        check(f"«{ans}» — без модели размер «1 л»; выбор меняется — клик не "
              "кодом, ход модели (как при разборе моделью)",
              g["model"] == 0 and g["items"] == [
                  ("Кола Зеро", "1 л", [], 1, "user")]
              and not g["clicks"] and g["steps"] == 1,
              f"{g['model']} {g['items']} {g['clicks']} {g['steps']}")
    for ans, why in (("л", "подходят оба"), ("литр", "нет такой подписи"),
                     ("2", "такого объёма нет"), ("1,5", "такого объёма нет"),
                     ("0,5 и 1 л", "два"), ("как есть, две штуки",
                                           "количество"),
                     ("как есть, но 1 л", "приписка"), ("не 0,5", "отрицание"),
                     ("да", "голое «да»"), ("давай", "служебное"),
                     ("на твой вкус", "выбор отдан агенту")):
        g = gate(f"m-{ans}", ans)
        check(f"«{ans}» ({why}) — разбор модели", g["model"] == 1,
              f"{g['model']} {g['items']}")
    # Названный человеком размер — другой, чем выбран в окне: «как есть»
    # неоднозначно — модели
    g = gate("s1", "как есть", goal_items=[{"name": "Кола Зеро",
                                            "size": "1 л"}])
    check("«как есть» при названном человеком «1 л» и выбранном 0,5 л — "
          "разбор модели", g["model"] == 1, f"{g['model']}")
    g = gate("s2", "как есть", goal_items=[{"name": "Кола Зеро",
                                            "size": "0,5 л"}])
    check("«как есть» при названном «0,5 л» = выбранном — без модели, бриф "
          "как был", g["model"] == 0 and g["items"] == [
              ("Кола Зеро", "0,5 л", [], 1, "user")] and g["clicks"] == [1],
          f"{g['model']} {g['items']}")
    # Вопрос модели об объёме без вариантов окна в тексте: «как есть» — бриф
    # как был, без модели; голый вариант — не из вопроса: модели
    g = gate("s3", "как есть", ask="Нужен лёд?")
    check("«как есть», вопрос не называл объёмы — без модели, позиция без "
          "изменений", g["model"] == 0 and g["items"] == [
              ("Кола Зеро", None, [], 1, "user")] and g["clicks"] == [1],
          f"{g['model']} {g['items']}")
    g = gate("s4", "0,5", ask="Нужен лёд?")
    check("«0,5», а вопрос об объёме не спрашивал — разбор модели",
          g["model"] == 1, f"{g['model']}")
    # Две позиции в брифе: позиция гейта — по блоку товара
    g = gate("s5", "0,5", goal="закажи пепперони и колу зеро на "
             "pizzatest.test", goal_items=[{"name": "пепперони"},
                                           {"name": "кола зеро"}])
    check("две позиции в брифе — размер у той, чей товар в окне (кола зеро)",
          g["model"] == 0 and g["items"] == [
              ("пепперони", None, [], 1, "user"),
              ("кола зеро", "0,5 л", [], 1, "user")], f"{g['items']}")
    # Позиции этого товара в брифе нет — без пометки, модели
    g = gate("s6", "как есть", goal_items=[{"name": "Маргарита"}])
    check("позиции товара окна нет в брифе — без пометки, разбор модели",
          "pick" not in g["aw"] and g["model"] == 1, f"{g['aw'].get('pick')}")
    # Лог — одна строка без значений
    g = gate("l1", "как есть")
    fast = [x for x in g["log"] if "без модели" in x]
    check("лог: одна строка «разбор ответа без модели (вопрос о товаре: как "
          "есть): items», без значений",
          len(fast) == 1 and "вопрос о товаре: как есть" in fast[0]
          and fast[0].endswith(": items") and "0,5" not in fast[0]
          and "Кола" not in fast[0], f"{fast}")

    print("гейт корзины — пицца:")
    pctx = "Пепперони 30 см, традиционное тесто"

    def pizza():
        md = lambda i, t, on=None: dict(
            {"idx": i, "tag": "button", "role": "radio" if on is not None
             else "button", "text": t, "md": 1, "ctx": pctx},
            **({"on": on} if on is not None else {}))
        return [cart0, md(1, "В корзину · 599 ₽"),
                md(2, "Маленькая 25 см", 0),
                md(3, "Средняя 30 см", 1), md(4, "Большая 35 см", 0),
                md(5, "Традиционное", 1), md(6, "Тонкое", 0),
                md(7, "Моцарелла 79 ₽", 0), md(8, "Халапеньо 59 ₽", 0)]
    pgoal = "закажи пепперони 30 см на pizzatest.test"
    pask = "Тесто: традиционное или тонкое? Сейчас выбрано традиционное."
    pitems = [{"name": "Пепперони", "size": "30 см"}]

    def pgate(chat, ans, ch=None, ask=pask):
        return gate(chat, ans, goal=pgoal, page=pizza(), ask=ask,
                    goal_items=pitems, ch=ch)
    g = pgate("p0", "как есть")
    check("пицца: вопрос — тесто (модель) и добавки списком (код)",
          "традиционное или тонкое" in g["q"] and "- Моцарелла — 79 ₽" in
          g["q"], g["q"])
    check("пицца «как есть» — без модели: выбранное тесто из вопроса — в "
          "опции, размер человека как был; клик кодом",
          g["model"] == 0 and g["items"] == [
              ("Пепперони", "30 см", ["Традиционное"], 1, "user")]
          and g["clicks"] == [1] and g["steps"] == 0,
          f"{g['model']} {g['items']} {g['clicks']}")
    g = pgate("p1", "традиционное")
    check("пицца «традиционное» (выбрано) — без модели, опция, клик кодом",
          g["model"] == 0 and g["items"] == [
              ("Пепперони", "30 см", ["Традиционное"], 1, "user")]
          and g["clicks"] == [1], f"{g['model']} {g['items']} {g['clicks']}")
    for ans, opt in (("тонкое", "Тонкое"), ("давай тонкое", "Тонкое"),
                     ("моцарелла", "Моцарелла")):
        g = pgate(f"p-{ans}", ans)
        check(f"пицца «{ans}» — без модели опция «{opt}»; выбор меняется — "
              "ход модели, кодом не нажато",
              g["model"] == 0 and g["items"] == [
                  ("Пепперони", "30 см", [opt], 1, "user")]
              and not g["clicks"] and g["steps"] == 1,
              f"{g['model']} {g['items']} {g['clicks']}")
    for ans, why in (("традиционное, без добавок", "многосоставной"),
                     ("тонкое и бекон", "многосоставной"),
                     ("тонкое, моцарелла", "многосоставной"),
                     ("а есть без лука?", "вопрос"),
                     ("без лука", "отрицание"),
                     ("2", "номер при добавках списком"),
                     ("большую", "размер, о котором вопрос не спрашивал"),
                     ("бортик", "нет в окне"),
                     ("как есть, но тонкое", "приписка")):
        g = pgate(f"pm-{ans}", ans)
        check(f"пицца «{ans}» ({why}) — разбор модели", g["model"] == 1,
              f"{g['model']} {g['items']}")
    # Опция уже названа человеком: другая опция может её заменять — модели
    g = gate("p9", "тонкое", goal=pgoal, page=pizza(), ask=pask,
             goal_items=[{"name": "Пепперони", "size": "30 см",
                          "options": ["традиционное"]}])
    check("пицца «тонкое» при названном «традиционное» — разбор модели",
          g["model"] == 1, f"{g['model']}")
    # Размер — в вопросе кода (человек размер не назвал): «маленькую» —
    # размер позиции
    g = gate("p10", "маленькую", goal="закажи пепперони на pizzatest.test",
             page=pizza(), ask="NONE", goal_items=[{"name": "Пепперони"}])
    check("пицца без размера: вопрос кода о размере, «маленькую» — без "
          "модели размер «Маленькая 25 см», ход модели (выбор меняется)",
          "Маленькая 25 см" in g["q"] and g["model"] == 0
          and g["items"] == [("Пепперони", "Маленькая 25 см", [], 1, "user")]
          and not g["clicks"], f"{g['q']!r} {g['items']}")

    print("гейт корзины — EN:")
    en_ask = "Size of Cola Zero: 0.5 l or 1 l? 0.5 l is selected now."
    egoal = "order cola zero on pizzatest.test"
    epage = cola_page(name="Cola Zero", btn="Add to cart · $1.19",
                      sizes=("0.5 l", "1 l"))
    for ans in ("as is", "keep it", "leave it", "keep it as is",
                "as is, please", "no changes", "don't change", "0.5",
                "0,5 l"):
        g = gate(f"e-{ans}", ans, goal=egoal, page=epage, ask=en_ask,
                 goal_items=[{"name": "Cola Zero"}], lang="en")
        check(f"EN «{ans}» — без модели, размер «0.5 l», клик кодом",
              "Before I put \"Cola Zero\" in the cart" in g["q"]
              and g["model"] == 0 and g["items"] == [
                  ("Cola Zero", "0.5 l", [], 1, "user")]
              and g["clicks"] == [1], f"{g['model']} {g['items']}")
    for ans in ("as is, but 1 l", "not 0.5", "2 of them", "l"):
        g = gate(f"em-{ans}", ans, goal=egoal, page=epage, ask=en_ask,
                 goal_items=[{"name": "Cola Zero"}], lang="en")
        check(f"EN «{ans}» — разбор модели", g["model"] == 1, f"{g['model']}")

    # ── 4. Приватный гейт: без модели совсем ──
    print("приватная страница:")
    ag = agent(real_cc({}, base_dir=tmp), SlotRouter())
    run = new_run(brief=dict(ta._new_brief(), items=[
        {"name": "Кола Зеро", "size": None, "options": [], "qty": 1,
         "src": "user"}]))
    aw_p = {"kind": "ask", "private": True, "pick": "gate",
            "question": "Перед тем как положить «Кола Зеро» в корзину:\n"
                        + cola_ask, "gate": {"name": "Кола Зеро",
                                             "opts": [["0,5 л", 1],
                                                      ["1 л", 0]]}}
    local0 = len(local_calls)
    ch, n = parse(ag, run, aw_p, "как есть")
    check("приватный гейт «как есть» — ни облачной, ни локальной модели, "
          "значение с пометкой private",
          n == 0 and len(local_calls) == local0
          and items(run) == [("Кола Зеро", "0,5 л", [], 1, "private")],
          f"{items(run)}")

    # ── 5. run3: разборы брифа, которые теперь без модели ──
    print("run3:")
    # 13: «0,5» на объём колы (2 позиции в брифе); 31: «2» на список
    # напитков модели; 35: «как есть» на объём колы зеро. Остальные разборы
    # — цель (1, 23, 27), свободный текст (5), «традиционное, без добавок»
    # (9, многосоставной), приватные адрес и телефон (17, 20) — модели
    g13 = gate("r13", "0,5", goal="закажи пиццу", page=cola_page(name="Кола"),
               ask="Объём колы: 0,5 л или 1 л? Сейчас выбрано 0,5 л.",
               goal_items=[{"name": "пепперони", "size": "30 см",
                            "options": ["традиционное"]}, {"name": "кола"}])
    g35 = gate("r35", "как есть")
    run31, aw31 = ask_aw(agent(real_cc({}, base_dir=tmp), SlotRouter()),
                         drinks_q)
    ch31, n31 = parse(ag, run31, aw31, "2")
    saved = [k for k, ok in (("13", g13["model"] == 0 and g13["items"][-1]
                              == ("кола", "0,5 л", [], 1, "user")),
                             ("31", n31 == 0 and ch31 == {"items": [
                                 {"name": "Кола Зеро"}]}),
                             ("35", g35["model"] == 0)) if ok]
    check("run3: разборы 13, 31, 35 — без модели, бриф тот же, что вернула "
          "модель", saved == ["13", "31", "35"], f"{saved}")
    print(f"  run3: без модели разборы {', '.join(saved)} — "
          f"{len(saved)} из 10 вызовов brief (≈5950 симв. облаку)")

    print(f"\nИтого: {OK} проверок, {FAILS} провалов")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

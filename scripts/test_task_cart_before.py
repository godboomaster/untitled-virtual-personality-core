"""Тест заказа при корзине, собранной до задачи (task_agent: _more_due,
_ask_more, _cart_look, _cart_look_step, _cart_lines, _cart_line_fits).
Ошибки нашёл прогон run4 (сценарий 4, «закажи пиццу как в прошлый раз»):
в корзине pizzatest лежали Пепперони 30 см, Кола 0,5 л и Кола Зеро от
прошлых задач; модель сама открыла и прочитала корзину (вызовы 36–37),
спросила о лишнем и сама нажала «К оформлению заказа» (вызов 40) — без
«что-нибудь ещё?»: гейт оформления ждал добавлений задачи, а их не было;
план до конца требовал «положить Пепперони».

Проверяет:
* разбор текста корзины: строки «название / подробности / − N + / цена»,
  шум (заголовок, «Удалить», допродажа без счётчика), сверка со счётчиком
  шапки или итогом; не узнано — None;
* строгая сверка строки с позицией брифа: «Кола» ≠ «Кола Зеро», 0,5 л ≠
  1 л, лишняя добавка, тесто брифа; две подходящие строки — сомнение;
* «пора спросить» перед оформлением, когда задача ничего не положила, а
  корзина сайта не пуста, — один раз; пустая/неизвестная корзина, «нет»
  — нет;
* повтор заказа: код сам открывает корзину кнопкой шапки и читает её,
  лежавшее из брифа — ✓ «уже в корзине», сразу «что-нибудь ещё?» с её
  строками — без вызовов модели шагов; после «нет» — оформление;
  повтор «В корзину» лежавшего — назад модели; клик корзину не открыл
  или в брифе нет сайта — ход модели, как раньше;
* не повтор: модель открыла корзину и жмёт «К оформлению» — не нажато,
  вопрос кода со строками корзины, лежавшее до задачи — и в вопросе о
  заказе; после «нет» — нажато; то же для ссылки «Оформление заказа»;
* корзина не разобрана — вопрос с числом со шапки, «не проверял»;
  похожее, но не то («Кола» без размера) — «не уверен», а не «ещё нет»;
* английский ход — английские фразы; строки с приватной страницы —
  вопрос приватный.

Браузер — настоящий гейт ComputerControlManager с заглушкой _dispatch
(real_cc), модель — ScriptedRouter (test_task_agent): живых браузеров,
веб-чатов и LLM нет.

Запуск: PYTHONPATH=. python3 scripts/test_task_cart_before.py
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
    {}), «что спросить перед шагом?» — NONE. brief — промпты разбора."""

    def __init__(self, slots=None):
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
        return "NONE"


# Шторка корзины pizzatest из run4 (вызов 38: текст чтения для агента)
DRAWER_TEXT = """3 товара на 837 ₽
×
Пепперони
30 см, традиционное тесто
−
1
+
599 ₽
Удалить
Кола
0,5 л
−
1
+
119 ₽
Удалить
Кола Зеро
0,5 л
−
1
+
119 ₽
Удалить
Добавить к заказу?
Соус сырный
49 ₽ +
Соус барбекю
49 ₽ +
Кола 0,5 л
119 ₽ +
Сумма заказа
837 ₽
К оформлению заказа"""

SITE = "pizzatest.test"
MAIN = f"https://{SITE}/"
CO = f"https://{SITE}/checkout"
PEPPERONI = {"name": "Пепперони", "size": "30 см",
             "options": ["Традиционное тесто", "без добавок"], "qty": 1}
COLA = {"name": "Кола", "size": "0,5 л", "options": [], "qty": 1}

MAIN_ITEMS = [
    {"idx": 2, "tag": "button", "role": "button", "text": "от 119 ₽ Выбрать",
     "ctx": "Кола Газированный напиток от 119 ₽ Выбрать", "cn": "Кола"},
    {"idx": 4, "tag": "button", "role": "button", "text": "от 119 ₽ Выбрать",
     "ctx": "Кола Зеро Газированный напиток без сахара от 119 ₽ Выбрать",
     "cn": "Кола Зеро"},
    {"idx": 10, "tag": "button", "role": "button", "text": "от 459 ₽ Выбрать",
     "ctx": "Пепперони Пикантная пепперони, моцарелла от 459 ₽ Выбрать",
     "cn": "Пепперони"},
    {"idx": 38, "tag": "button", "role": "button", "text": "Войти"},
    {"idx": 39, "tag": "button", "role": "button", "text": "Корзина 3"},
]
DRAWER_ITEMS = [
    {"idx": 101, "tag": "button", "role": "button", "text": "+",
     "aria": "Добавить Соус сырный", "md": 1},
    {"idx": 104, "tag": "button", "role": "button",
     "text": "К оформлению заказа", "md": 1},
    {"idx": 105, "tag": "button", "role": "button", "text": "×",
     "aria": "Закрыть корзину", "md": 1},
    {"idx": 106, "tag": "button", "role": "button", "text": "−",
     "aria": "Уменьшить", "md": 1,
     "ctx": "3 товара на 837 ₽ × Пепперони 30 см, традиционное тесто − 1 +"},
    {"idx": 108, "tag": "button", "role": "button", "text": "Удалить",
     "md": 1, "ctx": "Пепперони 30 см, традиционное тесто − 1 + 599 ₽"},
]
# Окно товара «Пепперони» (повтор «В корзину» лежавшего)
PIZZA_WINDOW = [
    {"idx": 301, "tag": "button", "role": "button",
     "text": "В корзину · 599 ₽", "md": 1,
     "ctx": "Пепперони 30 см, традиционное тесто В корзину · 599 ₽"},
    {"idx": 302, "tag": "button", "role": "button", "text": "Закрыть",
     "md": 1},
]
CO_ITEMS = [
    {"idx": 201, "tag": "input", "role": "textbox", "text": "Адрес доставки",
     "ed": True},
    {"idx": 202, "tag": "button", "role": "button", "text": "Оформить заказ"}]


def shop_cc(base_dir, text=DRAWER_TEXT, drawer=False, badge="Корзина 3",
            co_label=None):
    """Магазин из run4: главная, шторка корзины по клику на «Корзина 3»
    (шапка остаётся в снимке под окном), «К оформлению заказа» → /checkout,
    «Выбрать» у пепперони → окно товара. Чтение — текст шторки."""
    cc = real_cc({}, url=MAIN, base_dir=base_dir)
    cc.drawer, cc.window, cc.reads = drawer, False, 0
    items = [dict(x, text=badge) if x["idx"] == 39 else x for x in MAIN_ITEMS]
    drawer_items = [dict(x, text=co_label, tag="a", role="link")
                    if co_label and x["idx"] == 104 else x
                    for x in DRAWER_ITEMS]

    def _snap(site_word, chat_id="", auto_dismiss=False):
        if cc.cur == CO:
            page = CO_ITEMS
        else:
            page = items + (drawer_items if cc.drawer else []) \
                + (PIZZA_WINDOW if cc.window else [])
        return cc.cur, SITE, [dict(x) for x in page], 1, None

    def _disp(action, router=None):
        if action["kind"] == "read":
            cc.reads += 1
            action["_result"] = text if cc.drawer else "Пицца Тест меню"
            return
        cc.dispatched.append(dict(action))
        if action["kind"] == "click":
            if action.get("idx") == 39:
                cc.drawer = True
            elif action.get("idx") == 104:
                cc.cur, cc.drawer = CO, False
            elif action.get("idx") == 10:
                cc.window = True
    cc._snapshot_for, cc._dispatch = _snap, _disp
    return cc


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
    # Приватная страница (/checkout) не зовёт живую локальную модель
    _cp.PrivateRouter.get_response = lambda self, msgs, **kw: None
    tmp = Path(tempfile.mkdtemp(prefix="cart_before_"))
    n_mem = [0]

    def agent(cc, slot=None, past=None, chat="c"):
        n_mem[0] += 1
        path = tmp / f"m{n_mem[0]}.json"
        if past:
            path.write_text(json.dumps({chat: past}, ensure_ascii=False),
                            encoding="utf-8")
        ag = TaskAgent(cc, memory_path=path)
        ag.slot_router = slot or SlotRouter()
        return ag

    def clicks(cc):
        return [d.get("idx") for d in cc.dispatched
                if d.get("kind") == "click"]

    past_order = [{"ts": 1, "goal": "закажи пиццу", "sites": [SITE],
                   "ok": True, "qa": [], "result": "Дошёл до оплаты",
                   "brief": {"site": SITE, "items": [PEPPERONI, COLA]}}]

    # ── 1. Разбор текста корзины ──
    print("разбор корзины:")
    lines = ta._cart_lines(DRAWER_TEXT)
    check("шторка run4 — три строки с подробностями, штуками и ценами; "
          "заголовок, «Удалить», допродажа и итог — мимо",
          [(x["name"], x["detail"], x["qty"], x["price"]) for x in lines or ()]
          == [("Пепперони", "30 см, традиционное тесто", 1, 599.0),
              ("Кола", "0,5 л", 1, 119.0), ("Кола Зеро", "0,5 л", 1, 119.0)],
          f"{lines}")
    check("сверка: счётчик шапки 3 (штуки) — да; 2 — нет; без счётчика — "
          "итог текста 837 ₽ = сумма строк",
          ta._cart_lines_fit(lines, {"count": 3}, DRAWER_TEXT)
          and not ta._cart_lines_fit(lines, {"count": 2}, DRAWER_TEXT)
          and ta._cart_lines_fit(lines, None, DRAWER_TEXT)
          and not ta._cart_lines_fit(lines, {"sum": 900.0}, ""))
    one = ta._cart_lines("Корзина\nПепперони 30 см\n599 ₽\n− 2 +\nУдалить")
    check("строка одной строкой («Пепперони 30 см», «− 2 +»), цена до "
          "счётчика, заголовок «Корзина» — шум",
          [(x["name"], x["qty"], x["price"]) for x in one or ()]
          == [("Пепперони 30 см", 2, 599.0)], f"{one}")
    check("не узнано: счётчиков нет; три строки текста перед счётчиком — None",
          ta._cart_lines("Пепперони\n599 ₽\nКола\n119 ₽") is None
          and ta._cart_lines("Доставка 35 минут\nПепперони\n30 см\n−\n1\n+")
          is None)
    check("«− 0 +» у допродажи — не строка корзины",
          [x["name"] for x in ta._cart_lines(
              "Соус\n−\n0\n+\nКола\n0,5 л\n−\n1\n+") or ()] == ["Кола"])

    # ── 2. Строгая сверка с брифом ──
    print("сверка с брифом:")
    fits = ta._cart_line_fits
    cola, zero = lines[1], lines[2]
    check("«Кола 0,5 л» — строка «Кола / 0,5 л», не «Кола Зеро / 0,5 л»",
          fits(COLA, cola) and not fits(COLA, zero))
    check("«Кола Зеро 0,5 л» — своя строка; «Кола 1 л» и «Кола» без размера "
          "— ни одна",
          fits({"name": "Кола Зеро", "size": "0,5 л"}, zero)
          and not fits({"name": "Кола Зеро", "size": "0,5 л"}, cola)
          and not fits({"name": "Кола", "size": "1 л"}, cola)
          and not fits({"name": "Кола"}, cola))
    check("Пепперони 30 см, традиционное, без добавок — строка «30 см, "
          "традиционное тесто»; тонкое, 25 см, с беконом — нет",
          fits(PEPPERONI, lines[0])
          and not fits(dict(PEPPERONI, options=["Тонкое тесто"]), lines[0])
          and not fits(dict(PEPPERONI, size="25 см"), lines[0])
          and not fits(PEPPERONI, dict(lines[0], detail="30 см, традиционное "
                                       "тесто + бекон")))
    check("бриф без теста при тесте в строке — сомнение (не она)",
          not fits({"name": "Пепперони", "size": "30 см"}, lines[0]))
    check("_cart_had: пары только однозначные",
          ta._cart_had([PEPPERONI, COLA], lines) == {0: 0, 1: 1}
          and ta._cart_had([COLA], [cola, dict(cola)]) == {})

    # ── 3. «Пора спросить» без добавлений задачи ──
    print("гейт без добавлений задачи:")
    ag = agent(shop_cc(tmp))
    base = {"goal": "закажи пиццу", "qa": [], "history": [],
            "page_host": SITE}
    full = dict(base, page_state={"cart": {"found": True, "count": 3}})
    check("корзина не пуста, задача ничего не клала — перед оформлением "
          "пора, до хода модели — нет",
          ag._more_due(dict(full), before_checkout=True)
          and not ag._more_due(dict(full)))
    check("один раз (more_n), «нет» — не пора; корзина пуста или не видна — "
          "не пора",
          not ag._more_due(dict(full, more_n=0), before_checkout=True)
          and not ag._more_due(dict(full, more_no=True), before_checkout=True)
          and not ag._more_due(dict(base, page_state={"cart": {
              "found": True, "count": 0}}), before_checkout=True)
          and not ag._more_due(dict(base, page_state={"cart": {
              "found": True, "count": None, "sum": None}}),
              before_checkout=True))
    check("шапка под шторкой не видна — по последнему снимку того же сайта",
          ag._more_due(dict(base, page_state={"cart": {"found": True}},
                            cart_seen={"host": SITE, "cart": {
                                "found": True, "count": 3}}),
                       before_checkout=True)
          and not ag._more_due(dict(base, page_state={"cart": {}},
                                    cart_seen={"host": "other.test", "cart": {
                                        "found": True, "count": 3}}),
                               before_checkout=True))

    # ── 4. Повтор заказа: корзину открывает и читает код ──
    print("повтор заказа (run4, сц. 4):")
    cc = shop_cc(tmp)
    slot = SlotRouter()
    ag = agent(cc, slot, past=past_order)
    rt = ScriptedRouter([
        {"action": "click", "n": 2, "label": "К оформлению заказа"},
        {"action": "fail", "message": "стоп"}])
    q = ag.start("c", "закажи пиццу как в прошлый раз", rt, user_id="A") or ""
    run = ag._runs.get("c") or {}
    check("до хода модели: клик «Корзина 3» кодом, чтение кодом, вызовов "
          "модели шагов нет", clicks(cc) == [39] and cc.reads == 1
          and not rt.prompts, f"{clicks(cc)} reads={cc.reads} "
          f"prompts={len(rt.prompts)}")
    check("вопрос кода: строки корзины, что лежало до задачи и что из "
          "заказа уже там",
          "В корзине на сайте сейчас: Пепперони 30 см, традиционное тесто "
          "— 599 ₽; Кола 0,5 л — 119 ₽; Кола Зеро 0,5 л — 119 ₽ — это "
          "лежало там ещё до этой задачи." in q
          and "Из заказа там уже есть: Пепперони 30 см (Традиционное тесто, "
          "без добавок), Кола 0,5 л — второй раз не кладу." in q
          and "Добавить что-нибудь ещё?" in q and "Ещё не в корзине" not in q,
          q)
    check("ждёт ответа на «что-нибудь ещё?» (разбор моделью: «нет» может "
          "значить «убрать»)",
          (run.get("awaiting") or {}).get("more") is True
          and "pick" not in (run.get("awaiting") or {})
          and not (run.get("awaiting") or {}).get("private"))
    plan = ag._plan_lines(run)
    check("план модели: позиции ✓ «лежало до задачи», ▶ — «что-нибудь ещё?»",
          any(p.startswith("✓") and "Пепперони" in p and "before this task"
              in p for p in plan)
          and any(p.startswith("✓") and "Кола 0,5 л" in p for p in plan)
          and any(p.startswith("▶") and "anything else" in p for p in plan),
          f"{plan}")
    card = ag.task_card("c") or {}
    check("план человеку: «уже лежало в корзине», текущий — «что-нибудь ещё?»",
          [s["text"] for s in card.get("plan") or ()
           if "уже лежало в корзине" in s["text"]]
          == ["«Пепперони 30 см (Традиционное тесто, без добавок)» — уже "
              "лежало в корзине", "«Кола 0,5 л» — уже лежало в корзине"]
          and any(s["state"] == "current" and "что-нибудь ещё" in s["text"]
                  for s in card.get("plan") or ()), f"{card.get('plan')}")
    hist = " ".join(run.get("history") or ())
    check("история модели: строки корзины и «не класть повторно»",
          "read the cart (done by the system" in hist
          and "never add them again" in hist and "Кола Зеро 0,5 л" in hist)
    r = ag.feed("c", "нет", rt, user_id="A") or ""
    check("«нет» — модель жмёт «К оформлению заказа», гейт пропускает",
          clicks(cc) == [39, 104] and len(rt.prompts) == 1,
          f"{clicks(cc)} {r}")
    p0 = rt.prompts[0] if rt.prompts else ""
    check("промпт модели: ✓ позиции, ✓ «нет», ✓ корзина открыта, ▶ оформление",
          "it was in the cart before this task" in p0
          and "the user said no" in p0
          and re.search(r"✓ \d+\. open the cart", p0)
          and re.search(r"▶ \d+\. check out", p0), p0[-1500:])
    run = ag._runs.get("c") or ag.__dict__.get("_finished", {}).get(
        "c", {}).get("run") or {}
    check("корзина до задачи — в фактах оформления (cart_pre 3 шт.)",
          (run.get("cart_pre") or {}).get("count") == 3)

    # Повтор «В корзину» лежавшего — назад модели, без клика
    cc = shop_cc(tmp)
    ag = agent(cc, SlotRouter(), past=past_order)
    rt = ScriptedRouter([{"action": "click", "n": 3, "label": "Закрыть"}])
    ag.start("c", "закажи пиццу как в прошлый раз", rt, user_id="A")
    run = ag._runs["c"]
    cc.drawer, cc.window = False, True
    obs = ag._observe(run, "c")
    n = next(i for i, it in enumerate(obs["shown"], 1)
             if it["text"].startswith("В корзину"))
    ag._act(run, "c", None, {"action": "click", "n": n}, obs)
    check("«В корзину» пепперони, лежавшей до задачи, — не нажато, назад "
          "модели: «было в корзине до задачи»",
          clicks(cc) == [39]
          and "was in the cart before this task" in run["history"][-1],
          run["history"][-1])

    # Кнопка корзины кодом корзину не открыла — дальше модель
    cc = shop_cc(tmp)
    orig = cc._dispatch

    def _no_drawer(action, router=None):
        orig(action, router)
        cc.drawer = False
    cc._dispatch = _no_drawer
    ag = agent(cc, SlotRouter(), past=past_order)
    rt = ScriptedRouter([{"action": "fail", "message": "стоп"}])
    ag.start("c", "закажи пиццу как в прошлый раз", rt, user_id="A")
    run = ag._runs.get("c") or ag.__dict__.get("_finished", {}).get(
        "c", {}).get("run") or {}
    check("корзина по клику кода не открылась — ход модели, чтения нет",
          clicks(cc) == [39] and len(rt.prompts) == 1 and not cc.reads
          and run.get("cart_read") is False, f"{clicks(cc)} {len(rt.prompts)}")
    # В брифе прошлого заказа нет сайта — чужую корзину код не открывает
    cc = shop_cc(tmp)
    ag = agent(cc, SlotRouter(), past=[dict(
        past_order[0], brief={"items": [PEPPERONI, COLA]})])
    rt = ScriptedRouter([{"action": "fail", "message": "стоп"}])
    ag.start("c", "закажи пиццу как в прошлый раз", rt, user_id="A")
    check("сайта в брифе нет — код корзину не открывает, ход модели",
          clicks(cc) == [] and len(rt.prompts) == 1)

    # ── 5. Не повтор: модель открыла корзину и жмёт «К оформлению» ──
    print("гейт оформления (модель открыла корзину сама):")
    goal = f"закажи пепперони 30 см и колу 0,5 на {SITE}"
    cc = shop_cc(tmp)
    slot = SlotRouter({goal: {"site": SITE, "items": [
        dict(PEPPERONI), dict(COLA)]}})
    ag = agent(cc, slot)
    rt = ScriptedRouter([
        {"action": "click", "n": 5, "label": "Корзина 3"},
        {"action": "click", "n": 2, "label": "К оформлению заказа"},
        {"action": "click", "n": 2, "label": "К оформлению заказа"},
        {"action": "fail", "message": "стоп"}])
    q = ag.start("g", goal, rt, user_id="A") or ""
    check("не повтор: корзину открыла модель (код сам не открывает)",
          len(rt.prompts) == 2 and clicks(cc) == [39], f"{len(rt.prompts)}")
    check("«К оформлению заказа» без «что-нибудь ещё?» — не нажато",
          clicks(cc) == [39], f"{clicks(cc)}")
    check("вместо него вопрос кода: строки корзины (прочитаны кодом) и что "
          "из заказа там уже есть",
          "В корзине на сайте сейчас: Пепперони 30 см, традиционное тесто"
          in q and "Из заказа там уже есть: Пепперони 30 см" in q
          and "Добавить что-нибудь ещё?" in q and cc.reads == 1, q)
    run = ag._runs["g"]
    facts = ag._checkout_facts(run, "g", {"private": False})
    check("лежавшее до задачи — и в вопросе о самом заказе",
          "лежало там до задачи" in ag._commit_question(
              run, facts, "Оформить заказ"))
    r = ag.feed("g", "нет", rt, user_id="A") or ""
    check("после «нет» — «К оформлению заказа» нажато",
          clicks(cc) == [39, 104], f"{clicks(cc)} {r}")

    # Ссылка «Оформление заказа» (не checkout_step_label, не коммит) — тот же
    # гейт «что-нибудь ещё?»
    check("подписи перехода к оформлению вне словаря гейта: «Оформление "
          "заказа», «Check out», «Kasse»; «Условия оформления заказа» — нет",
          all(ta._CHECKOUT_GO_RE.fullmatch(x) for x in (
              "Оформление заказа", "Check out", "Kasse"))
          and not ta._CHECKOUT_GO_RE.fullmatch("Условия оформления заказа"))
    cc = shop_cc(tmp, co_label="Оформление заказа")
    ag = agent(cc, SlotRouter({goal: {"site": SITE, "items": [
        dict(PEPPERONI), dict(COLA)]}}))
    rt = ScriptedRouter([
        {"action": "click", "n": 5, "label": "Корзина 3"},
        {"action": "click", "n": 2, "label": "Оформление заказа"},
        {"action": "click", "n": 2, "label": "Оформление заказа"},
        {"action": "fail", "message": "стоп"}])
    q = ag.start("l", goal, rt, user_id="A") or ""
    check("ссылка «Оформление заказа» без «что-нибудь ещё?» — не нажата, "
          "вопрос кода", clicks(cc) == [39]
          and "Добавить что-нибудь ещё?" in q, f"{clicks(cc)} {q}")
    ag.feed("l", "нет", rt, user_id="A")
    check("после «нет» — нажата без «да»", clicks(cc) == [39, 104],
          f"{clicks(cc)}")

    # Корзина не разобрана — число со шапки, «не проверял»
    cc = shop_cc(tmp, text="Ваша корзина\nчто-то непонятное\nК оформлению")
    ag = agent(cc, SlotRouter({goal: {"site": SITE, "items": [
        dict(PEPPERONI), dict(COLA)]}}))
    rt = ScriptedRouter([
        {"action": "click", "n": 5, "label": "Корзина 3"},
        {"action": "click", "n": 2, "label": "К оформлению заказа"},
        {"action": "fail", "message": "стоп"}])
    q = ag.start("u", goal, rt, user_id="A") or ""
    run = ag._runs.get("u") or {}
    check("корзина не разобрана: не нажато, вопрос с числом со шапки и «не "
          "проверял», план не тронут",
          clicks(cc) == [39]
          and "В корзине на сайте сейчас: 3 шт — это лежало там ещё до этой "
          "задачи." in q and "Что именно там лежит, я не проверял; заказ: "
          "Пепперони 30 см" in q and not run.get("cart_adds"), q)

    # ── 6. Похожее, но не то; английский ход; приватная корзина ──
    print("сомнение, язык, приватность:")
    ag = agent(shop_cc(tmp))
    run = {"goal": "закажи колу", "qa": [], "history": [], "page_host": SITE,
           "brief": {"items": [{"name": "Кола", "qty": 1}]},
           "page_state": {"cart": {"found": True, "count": 3}},
           "cart_first": {SITE: {"found": True, "count": 3}}}
    obs = {"private": False, "url": MAIN, "host": SITE,
           "shown": [dict(x) for x in DRAWER_ITEMS]}
    ag.cc.drawer = True
    _k, q = ag._ask_more(run, "s", obs)
    check("«Кола» без размера при «Кола 0,5 л» и «Кола Зеро» — «не уверен», "
          "не «ещё не в корзине» и не ✓",
          "Похожее на Кола в корзине уже есть" in q
          and "Ещё не в корзине" not in q and not run.get("cart_adds")
          and run.get("cart_unsure") == ["Кола"], q)

    cc = shop_cc(tmp, drawer=True)
    cc.turn_lang = lambda: "en"
    ag = agent(cc)
    run = {"goal": "order a pizza", "qa": [], "history": [], "page_host": SITE,
           "brief": {"items": [dict(PEPPERONI)]},
           "page_state": {"cart": {"found": True, "count": 3}},
           "cart_first": {SITE: {"found": True, "count": 3}}}
    _k, q = ag._ask_more(run, "e", obs)
    check("английский ход — английский вопрос",
          q.startswith("The cart on the site has now: Пепперони 30 см")
          and "it was there before this task." in q
          and "From the order, it already has:" in q
          and "Anything else?" in q, q)
    cc = shop_cc(tmp, drawer=True)
    ag = agent(cc)
    run = {"goal": "закажи пиццу", "qa": [], "history": [], "page_host": SITE,
           "page_private": True, "brief": {"items": [dict(PEPPERONI)]},
           "page_state": {"cart": {"found": True, "count": 3}}}
    looked = ag._cart_look(run, "p", dict(obs, private=True,
                                           url=f"https://{SITE}/cart"))
    run["page_private"] = False
    _k, q = ag._ask_more(run, "p", obs)
    check("корзина прочитана на приватной странице: вопрос со строками — "
          "приватный и с обычной страницы, строка истории облаку — заглушкой",
          looked and "Пепперони" in q
          and run["awaiting"].get("private") is True
          and "Пепперони" not in " ".join(
              (run.get("hist_public") or {}).values()),
          f"{run.get('awaiting')}")

    # Обычный заказ с добавлением задачи — как раньше (строк нет)
    cc = shop_cc(tmp, drawer=True)
    ag = agent(cc)
    run = {"goal": "закажи пиццу", "qa": [], "history": [],
           "brief": {"items": [dict(PEPPERONI)]},
           "cart_adds": [{"key": "Пепперони", "verified": True}],
           "cart_pre": {"count": 2}, "page_state": {"cart": {"count": 3}}}
    _k, q = ag._ask_more(run, "n", obs)
    check("задача сама положила — прежний вопрос «В корзине: …» с «до этой "
          "задачи что-то лежало (2 шт.)»",
          q.startswith("В корзине: Пепперони 30 см") and "(2 шт.)" in q
          and not cc.reads, q)

    print(f"\nИтого: {OK - FAILS} OK, {FAILS} FAIL")
    return FAILS


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

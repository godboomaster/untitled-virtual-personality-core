"""Тест агента задач: цель-вопрос о сайте — не заказ.

Стенд 20.10: «узнай в Пицца Тест, сколько стоит доставка и до скольки
работают» по слову «доставк» шла заказом — план корзины в карточке, лишний
вызов модели о разделах магазина, верный «done» с ответом отбит
_not_finished («в корзине пусто»), и человеку ушло «Не получилось: <ответ>».

Проверяет:
* разбор цели (_info_goal поверх _ORDER_GOAL_RE/_BUY_GOAL_RE): вопрос и
  справка («узнай/посмотри/проверь/скажи…», «сколько стоит доставка», «до
  скольки работают», «есть ли доставка в …», «мой заказ», en «find out…»,
  «check delivery hours») — не заказ; «закажи с доставкой», «оформи
  доставку», «доставь пиццу», «order pizza with delivery», «узнай цену и
  закажи», «можно заказать?», «can I order?» — заказ; пограничные случаи;
* вопрос: ни плана заказа (карточка, промпт), ни разделов магазина (ни
  разметки, ни модели), «done» с ответом принят с первого раза, без
  пометки «корзину проверить не смог»; сайт вопроса «как в прошлый раз»
  не подставляет;
* заказ — как раньше: разделы, план, «done» при пустой корзине — назад
  модели, бриф для повтора в памяти.

Запуск: python3 -m scripts.test_task_info_goal
"""

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.features import browser_actions as _ba  # noqa: E402
from app.features import cc_privacy as _cp  # noqa: E402
from app.features import task_agent as ta  # noqa: E402
from app.features.task_agent import TaskAgent  # noqa: E402
from scripts.test_task_agent import (  # noqa: E402
    FakeCC, ScriptedRouter, real_cc)

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


# Браузера в тестах нет: разметка разделов — подмена со счётчиком
_ba.wait_dom_idle = lambda *a, **k: None
_ba.section_items = lambda *a, **k: None
_ba.scroll_step = lambda *a, **k: {"moved": True, "bottom": False}
_ba.scroll_container_step = lambda *a, **k: {"moved": True, "bottom": False,
                                             "y0": 0}
# Приватная страница не зовёт живую локальную модель (Ollama)
_cp.PrivateRouter.get_response = lambda self, msgs, **kw: None
NAMES_CALLS = []
# Что вернёт разметка: None — «не разобрать» (тогда разделы — модель)
HEADS = [["Пиццы", "Комбо", "Закуски", "Напитки"]]


def _names(*a, **k):
    NAMES_CALLS.append(a)
    return HEADS[0]


_ba.section_names = _names

TMP = Path(tempfile.mkdtemp(prefix="task_info_goal_"))
FakeCC.base_dir = TMP / "fake"


class SlotRouter:
    # slot_router: разделы / показ раздела — текст, разбор ответа — JSON
    SEC = "sections of the site's catalog"

    def __init__(self, slots=()):
        self.slots, self.prompts = list(slots), []

    def get_response(self, messages, **kw):
        p = messages[-1]["content"]
        self.prompts.append(p)
        if self.SEC in p:
            return "- Пиццы\n- Комбо"
        if "Present it:" in p:
            return "- Пепперони — от 349 ₽\nКакую заказываем?"
        return json.dumps(self.slots.pop(0) if self.slots else {},
                          ensure_ascii=False)

    def sec_calls(self):
        return sum(self.SEC in p for p in self.prompts)


def is_order(g):
    return bool(ta._ORDER_GOAL_RE.search(g))


def is_buy(g):
    return bool(ta._BUY_GOAL_RE.search(g))


print("Разбор цели: вопрос-справка — не заказ")
INFO = [
    # Стенд 20.10 — дословно
    "узнай в Пицца Тест, сколько стоит доставка и до скольки работают",
    "узнай, сколько стоит доставка",
    "посмотри, какие условия доставки в Додо",
    "проверь, есть ли доставка в Химки",
    "скажи, сколько идёт доставка",
    "подскажи стоимость доставки на pizza.test",
    "сравни цены доставки в Додо и Пицца Тест",
    # Без глагола справки — вопросительное слово, «ли», «?»
    "сколько стоит доставка в Пицца Тест",
    "до скольки работает доставка",
    "какие условия доставки",
    "есть ли доставка в Химки",
    "сколько идёт доставка",
    "есть доставка в Химки?",
    "когда привезут заказ",
    # Свой заказ, статус, «о заказе» — справка, не новый заказ
    "проверь мой заказ",
    "где мой заказ",
    "статус доставки",
    "узнай статус моего заказа",
    "найди в почте письмо о заказе",
    # Инфинитив в вопросе — не действие
    "выясни, можно ли заказать пиццу на завтра",
    "сколько стоит купить подписку",
    "хочу узнать, сколько стоит доставка",
    "не надо заказывать, просто узнай цену доставки",
    "Коннор, узнай, до скольки работает доставка",
    # en
    "Find out how much delivery costs",
    "check delivery hours",
    "find out if I can order pizza after midnight",
    "check my order status",
    "how much does delivery cost if I order two pizzas",
    "what are the delivery terms on pizza.test",
]
for g in INFO:
    check(f"вопрос: «{g}»",
          ta._info_goal(g) and not is_order(g) and not is_buy(g),
          (ta._info_goal(g), is_order(g), is_buy(g)))

print("Разбор цели: заказ остаётся заказом")
ORDER = [
    "закажи с доставкой",
    "оформи доставку",
    "доставь пиццу",
    "доставьте роллы к 8",
    "order pizza with delivery",
    "закажи пиццу",
    "купи чехол",
    "оформи доставку роллов",
    "order a pizza",
    "хочу пиццу с доставкой",
    "доставка суши на дом",
    "заказ пиццы на дом",
    "сделай заказ, какой обычно",
    "закажи пепперони 30 см на додо",
    "Коннор, закажи пиццу",
    "please buy a phone case",
    "закажи как в прошлый раз",
    "повтори заказ",
    "оформи заказ как есть",
    "order it as is",
    # Вопрос + действие заказа — заказ
    "узнай цену и закажи",
    "посмотри, сколько стоит пепперони, и закажи две",
    "хочу заказать пиццу — сколько стоит доставка?",
    "закажи пиццу и проверь, сколько идёт доставка",
    "посмотри, что я заказывал в прошлый раз, и повтори",
    "хочу посмотреть меню и заказать пиццу",
    "order pizza and check how long delivery takes",
    "find out the price and order it",
    "I want to order pizza, how much is delivery?",
    "place an order for pepperoni",
    # Вопрос-просьба: как раньше — заказ
    "можешь заказать пиццу?",
    "можно заказать пиццу с доставкой?",
    "закажешь пиццу?",
    "давай закажем пиццу",
    "can I order a pizza on dodo?",
    # «какой-нибудь», «когда» в заказе — не вопрос
    "закажи какую-нибудь пиццу",
    "закажи пиццу, какую посоветуешь",
    "купи билет, когда начнутся продажи",
    # Другие языки — как раньше
    "bestelle eine Pizza",
]
for g in ORDER:
    check(f"заказ: «{g}»", is_order(g) and not ta._info_goal(g),
          (is_order(g), ta._info_goal(g)))
check("покупка (_BUY_GOAL_RE): «доставь пиццу», «закажи с доставкой», "
      "«оформи доставку», «order pizza with delivery», «узнай цену и закажи»",
      all(is_buy(g) for g in ("доставь пиццу", "закажи с доставкой",
                              "оформи доставку", "order pizza with delivery",
                              "узнай цену и закажи")))
check("«предоставь доступ», «оставь как есть» — не заказ (слово "
      "«доставь» — целиком)",
      not any(is_order(g) or is_buy(g) for g in ("предоставь доступ",
                                                 "оставь как есть")))
check("«какой товар» — вопрос (не «какой-то»): «узнай, какой товар "
      "дешевле с доставкой»", ta._info_goal("какой товар дешевле с доставкой"))

print("Вопрос на сайте с разделами: ни плана, ни разделов, done принят")
HOME, PIZ = "https://pizza.test/", "https://pizza.test/pizzas"
NAV_CTX = "Пиццы Комбо Закуски Напитки Доставка и оплата Корзина"


def link(idx, text, ctx=NAV_CTX):
    return {"idx": idx, "tag": "a", "role": "link", "text": text,
            "ctx": ctx, "vp": True}


nav = [link(1, "Пиццы"), link(2, "Комбо"), link(3, "Закуски"),
       link(4, "Напитки"), link(5, "Доставка и оплата")]
ANSWER = ("Доставка — 149 ₽, бесплатно от 799 ₽. Работают с 10:00 до "
          "23:00.")


def fresh(name, slots=()):
    cc = real_cc({HOME: nav, PIZ: nav}, url=HOME, clicks={1: PIZ},
                 base_dir=TMP / f"cc_{name}")
    agent = TaskAgent(cc, memory_path=TMP / f"{name}.json")
    agent.slot_router = SlotRouter(slots)
    return cc, agent


NAMES_CALLS.clear()
cc, agent = fresh("i1", slots=[{"site": "pizza.test"}])
rt = ScriptedRouter([{"action": "done", "message": ANSWER}])
GOAL_I = "узнай на pizza.test, сколько стоит доставка и до скольки работают"
r = agent.start("i1", GOAL_I, rt, user_id="A")
check("ответ модели — человеку как есть: без «Не получилось», без «корзину "
      "проверить не смог»",
      ANSWER in r and "Не получилось" not in r and "проверить не смог"
      not in r and "Корзина на сайте" not in r, r)
check("done принят с первого раза: модель шагов позвана один раз, «NOT "
      "finished» нет", len(rt.prompts) == 1
      and not any("NOT finished" in p for p in rt.prompts), rt.prompts)
check("разделов магазина нет: ни вопроса, ни вызова модели о разделах, ни "
      "чтения разметки", "есть разделы" not in r
      and agent.slot_router.sec_calls() == 0 and not NAMES_CALLS,
      (agent.slot_router.prompts, NAMES_CALLS))
check("промпт шага — без плана заказа и без правила разделов",
      rt.prompts and "The plan of the order" not in rt.prompts[0]
      and "which section of the site" not in rt.prompts[0])
card = agent.task_card("i1")
check("карточка: итог done, плана заказа нет",
      card and card["status"] == "done" and card["plan"] == [], card)
check("модель разбора за весь вопрос — один вызов (бриф цели), как на "
      "стенде без вызова 41",
      len(agent.slot_router.prompts) == 1
      and "(the task goal)" in agent.slot_router.prompts[0],
      agent.slot_router.prompts)

# Разметка не разобрана (как на стенде) — модель о разделах не зовётся
HEADS[0] = None
cc, agent = fresh("i2")
rt = ScriptedRouter([{"action": "done", "message": ANSWER}])
r = agent.start("i2", "скажи, сколько стоит доставка на pizza.test", rt,
                user_id="A")
check("разметки нет — вызова модели о разделах (стенд: 34, 41) тоже нет, "
      "done принят", agent.slot_router.sec_calls() == 0 and ANSWER in r
      and len(rt.prompts) == 1, (agent.slot_router.prompts, r))
HEADS[0] = ["Пиццы", "Комбо", "Закуски", "Напитки"]

# Тот же сайт, заказ — разделы и план, как раньше
NAMES_CALLS.clear()
cc, agent = fresh("o1")
rt = ScriptedRouter([{"action": "fail", "message": "стоп"}])
r = agent.start("o1", "закажи пиццу на pizza.test", rt, user_id="A")
card = agent.task_card("o1")
check("заказ: вопрос о разделах по разметке, как раньше",
      "есть разделы:\n- Пиццы\n- Комбо\n- Закуски\n- Напитки\n" in r
      and len(NAMES_CALLS) == 1, (r, NAMES_CALLS))
check("заказ: в карточке план заказа с шагом «выбрать, что заказать»",
      card and len(card["plan"]) >= 4
      and any(p["text"] == "выбрать, что заказать" for p in card["plan"]),
      card and card["plan"])

print("done/fail на фейковом браузере: вопрос и заказ")


def run_fake(chat, goal, replies):
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc, memory_path=TMP / f"{chat}.json")
    router = ScriptedRouter(replies)
    return agent, router, agent.start(chat, goal, router, user_id="A")


for chat, goal in (("f1", "проверь на pizza.test, есть ли доставка в Химки"),
                   ("f2", "find out how much delivery costs on pizza.test"),
                   ("f3", "проверь мой заказ на pizza.test"),
                   ("f4", "сколько идёт доставка на pizza.test?")):
    agent, router, r = run_fake(chat, goal, [
        {"action": "done", "message": "Да, доставляют, 149 ₽."},
        {"action": "fail", "message": "лишний ход"}])
    check(f"«{goal}» — done принят сразу, без отбоя и пометок",
          len(router.prompts) == 1 and r.endswith("Да, доставляют, 149 ₽.")
          and "проверить не смог" not in r
          and "couldn't verify" not in r and not agent.active(chat),
          (r, len(router.prompts)))
    check(f"«{goal}» — план заказа в промпте не строится",
          "The plan of the order" not in router.prompts[0])

for chat, goal in (("g1", "закажи пиццу на pizza.test"),
                   ("g2", "узнай цену доставки и закажи пиццу на "
                          "pizza.test"),
                   ("g3", "order pizza with delivery on pizza.test")):
    agent, router, r = run_fake(chat, goal, [
        {"action": "done", "message": "Готово."},
        {"action": "done", "message": "Готово."}])
    check(f"заказ «{goal}»: done при пустой корзине — назад модели, "
          "повторный — с честной пометкой (как раньше)",
          len(router.prompts) == 2
          and "NOT finished — nothing was added to the cart"
          in router.prompts[1]
          and ("проверить не смог" in r or "couldn't verify" in r), r)
    check(f"заказ «{goal}»: план заказа в промпте",
          "The plan of the order" in router.prompts[0])

print("План и память — прямые проверки")
bare = TaskAgent(SimpleNamespace(base_dir=str(TMP / "bare")),
                 memory_path=TMP / "bare.json")
for g, want in ((GOAL_I, False), ("закажи пиццу", True)):
    run = {"goal": g, "lang": "ru"}
    check(f"«{g}»: план (карточка и промпт) — "
          + ("есть" if want else "пусто"),
          bool(bare._plan_view(run, "working")) == want
          and bool(bare._plan_lines(run)) == want)


def rec_for(g):
    run = {"goal": g, "lang": "ru", "qa": [], "history": [],
           "sites": ["pizza.test"], "outcome": "done", "traffic_logged": True,
           "brief": {"items": [], "exclude": [],
                     "site": {"value": "Пицца Тест", "src": "user"}}}
    bare._remember("mem", run, "ok")
    data = json.loads((TMP / "bare.json").read_text(encoding="utf-8"))
    return data["mem"][-1]


check("память: заказ — бриф с сайтом для «как в прошлый раз» (как раньше)",
      (rec_for("закажи пиццу в Пицца Тест").get("brief") or {}).get("site")
      == "Пицца Тест")
check("память: вопрос «сколько стоит доставка в Пицца Тест» — без брифа",
      "brief" not in rec_for("сколько стоит доставка в Пицца Тест"))

print(f"\nИтого: {OKS} проверок, {FAILS} провалов")
sys.exit(1 if FAILS else 0)

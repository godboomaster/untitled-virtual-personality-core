"""Тест позиций брифа агента задач (task_agent: _apply_brief, _no_extras,
_answer_keeps, _same_item strict, _brief_pick). Ошибки нашёл четвёртый
прогон симуляции (run4, 21.10).

Проверяет:
* отказ от добавок значением опции («без добавок», «ничего», «как есть»,
  «no extras», «none») — не опция позиции: в бриф не кладётся (разбор
  ответа и память задач), список из одних отказов опции не меняет;
  «без лука», «без глютена» — опции, остаются; промпт разбора говорит
  модели, что отказ — не опция;
* повтор «В корзину» кодом после «традиционное, без добавок», когда разбор
  вернул опции [«Традиционное тесто», «без добавок»] (run4 №10–11): клик
  кодом без шага модели, в плане и брифе «без добавок» нет;
* строка прошлой задачи из записи старого кода — без «без добавок»;
* «Кола» и «Кола Зеро» — разные позиции (run4 №39–40: бриф после «оставь»
  слил «Колу Зеро» с «Колой»): _same_item strict (лишнее отличительное
  слово — другая позиция; вид, размер, мера — та же), слияние брифа,
  remove_items/exclude, правило промпта; корзина и бриф — каждое
  добавление своей позиции (_brief_pick): недобавленное, «готово» (C3),
  «В корзине: …», карточка положенного, окно товара (_brief_item,
  _gate_pick), повтор кодом (_answer_keeps), «уже добавлено»
  (_cart_add_gate); без брифа — как раньше.

Браузер — настоящий гейт ComputerControlManager с заглушкой _dispatch
(real_cc), модель — ScriptedRouter (test_task_agent): живых браузеров,
веб-чатов и LLM нет.

Запуск: PYTHONPATH=. python3 scripts/test_task_brief_items.py
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
    {}), «что спросить перед шагом?» — ask, прочее — NONE. brief — промпты
    разбора ответа моделью."""

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
    _cp.PrivateRouter.get_response = (
        lambda self, msgs, **kw: self._router.get_response(msgs, **kw))
    tmp = Path(tempfile.mkdtemp(prefix="brief_items_"))
    n_mem = [0]

    def agent(cc, slot=None):
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

    def items(brief):
        return [(b["name"], b.get("size"), b.get("options"), b.get("qty"))
                for b in brief["items"]]

    # ── 1. Отказ от добавок — не опция ──
    print("отказ от добавок — не опция:")
    refusals = ("без добавок", "Без добавок.", "без дополнений", "без допов",
                "без доп. ингредиентов", "без каких-либо добавок",
                "никаких добавок", "добавки не нужны", "ничего",
                "ничего не надо", "больше ничего", "без всего", "нет",
                "как есть", "no extras", "No add-ons", "without toppings",
                "nothing else", "none", "as is")
    real = ("без лука", "без глютена", "Без сахара", "Традиционное тесто",
            "тонкое", "без доп. сыра", "без майонеза", "no onions",
            "extra cheese", "Сырный бортик", "без добавок и соусов")
    check("отказы — _no_extras", all(ta._no_extras(o) for o in refusals),
          [o for o in refusals if not ta._no_extras(o)])
    check("настоящие опции («без лука» — что убрать, «без доп. сыра», "
          "тесто, добавка) — не отказ",
          not any(ta._no_extras(o) for o in real),
          [o for o in real if ta._no_extras(o)])
    b = ta._new_brief()
    ta._apply_brief(b, {"items": [{"name": "Пепперони", "size": "30 см"}]})
    ta._apply_brief(b, {"items": [{"name": "Пепперони", "options": [
        "Традиционное тесто", "без добавок"]}]})
    check("run4 №10: [«Традиционное тесто», «без добавок»] → в брифе только "
          "тесто", items(b) == [("Пепперони", "30 см", ["Традиционное тесто"],
                                 1)], items(b))
    changed = ta._apply_brief(b, {"items": [{"name": "Пепперони",
                                             "options": ["без добавок"]}]})
    check("список из одних отказов опции не меняет (тесто осталось), бриф "
          "не изменился", not changed and b["items"][0]["options"]
          == ["Традиционное тесто"], items(b))
    ta._apply_brief(b, {"items": [{"name": "Пепперони", "options": [
        "без лука", "nothing else"]}]})
    check("«без лука» — опция (выбор), «nothing else» — мимо",
          b["items"][0]["options"] == ["без лука"], items(b))
    ta._apply_brief(b, {"items": [{"name": "Пепперони", "options": []}]})
    check("явный пустой список, как раньше, опции очищает",
          b["items"][0]["options"] == [], items(b))
    m = ta._new_brief()
    ta._apply_brief(m, {"site": "pizzatest.ru", "items": [
        {"name": "Пепперони", "size": "30 см", "options": [
            "Традиционное тесто", "без добавок"], "qty": 1},
        {"name": "Кола", "size": "0,5 л", "options": [], "qty": 1}]},
        src="memory")
    check("«как в прошлый раз» из записи старого кода (бриф из памяти) — "
          "без «без добавок»",
          m["items"][0]["options"] == ["Традиционное тесто"]
          and m["items"][0]["src"] == "memory", items(m))
    rec = {"brief": {"items": [{"name": "Пепперони", "size": "30 см",
                                "options": ["Традиционное тесто",
                                            "без добавок"]}]}}
    check("строка прошлой задачи из такой записи — «(Традиционное тесто)»",
          ta._past_items(rec) == "Пепперони 30 см (Традиционное тесто)",
          ta._past_items(rec))

    # _answer_keeps на снимке run4 (офлайн-проба probe4/keeps.py)
    add = {"text": "В корзину · 599 ₽", "md": True}
    shown = [add,
             {"text": "25 см", "on": 0, "md": True},
             {"text": "30 см", "on": 1, "md": True},
             {"text": "35 см", "on": 0, "md": True},
             {"text": "Традиционное", "on": 1, "md": True},
             {"text": "Тонкое", "on": 0, "md": True},
             {"text": "Сырный бортик 169 ₽", "on": 0, "md": True},
             {"text": "Моцарелла 79 ₽", "on": 0, "md": True}]

    def keeps(answer, opts, size=None):
        it = {"name": "Пепперони", "options": opts}
        if size:
            it["size"] = size
        rep = {"answer": answer, "key": "Пепперони 30 см",
               "ch": {"items": [it]}}
        return TaskAgent._answer_keeps(None, {}, rep, add, {"shown": shown})
    check("_answer_keeps: «традиционное, без добавок» с опцией «без добавок» "
          "разбора — выбор не меняет (run4 №11 — повтор кодом)",
          keeps("традиционное, без добавок",
                ["Традиционное тесто", "без добавок"]))
    check("_answer_keeps: опции «no extras»/«ничего» — тоже",
          keeps("без добавок", ["no extras"]) and keeps("как есть",
                                                         ["ничего"]))
    check("_answer_keeps: невыбранный вариант рядом с отказом — модели "
          "(«тонкое, без добавок»)",
          not keeps("тонкое, без добавок", ["Тонкое тесто", "без добавок"]))
    check("_answer_keeps: «без лука» опцией — не отказ, модели",
          not keeps("без добавок", ["без лука"]))

    # Промпт разбора: отказ от добавок — не опция
    cc0 = real_cc({"https://x.test/": []}, url="https://x.test/",
                  base_dir=tmp)
    slot0 = SlotRouter()
    ag0 = agent(cc0, slot0)
    run0 = {"goal": "закажи пиццу", "brief": ta._new_brief(), "qa": [],
            "lang": "ru", "chat_id": "p0", "traffic": {}}
    ag0._update_brief(run0, "Добавки?", "без добавок")
    check("промпт разбора: «declining extras … is not an option»",
          slot0.brief and "declining extras" in slot0.brief[-1]
          and "is not an option" in slot0.brief[-1],
          slot0.brief[-1][-900:] if slot0.brief else "")

    # ── 2. Повтор «В корзину» кодом после «традиционное, без добавок» ──
    print("повтор «В корзину» после отказа от добавок:")
    SHOP, ADDED = "https://pizzatest.test/", "https://pizzatest.test/?added=1"
    pctx = "Пепперони 30 см, традиционное тесто"
    cart0 = {"idx": 9, "tag": "button", "role": "button", "text": "Корзина",
             "aria": "Корзина"}

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
    added = [{"idx": 9, "tag": "button", "role": "button",
              "text": "Корзина 1 · 599 ₽", "aria": "Корзина"},
             {"idx": 10, "tag": "a", "role": "link", "text": "Меню"}]
    pgoal = "закажи пепперони 30 см на pizzatest.test"
    ans = "традиционное, без добавок"
    cc = real_cc({SHOP: pizza(), ADDED: added}, url=SHOP, clicks={1: ADDED},
                 base_dir=tmp)
    slot = SlotRouter({pgoal: {"site": "pizzatest.test", "items": [
        {"name": "Пепперони", "size": "30 см"}]},
        ans: {"items": [{"name": "Пепперони", "options": [
            "Традиционное тесто", "без добавок"]}]}},
        ask="Тесто: традиционное или тонкое? Сейчас выбрано традиционное.")
    ag = agent(cc, slot)
    rt = ScriptedRouter([
        {"action": "click", "n": 1, "label": "В корзину · 599 ₽",
         "expect": "the cart count grows"},
        {"action": "fail", "message": "стоп"}])
    q = ag.start("r1", pgoal, rt, user_id="A")
    n0, nb = len(rt.prompts), len(slot.brief)
    r = ag.feed("r1", ans, rt, user_id="A")
    check("до клика — вопросы о товаре", "Перед тем как положить «Пепперони»"
          in q, q)
    check("разбор ответа — модель (многосоставной), опции с «без добавок»",
          len(slot.brief) == nb + 1)
    check("«В корзину» — кодом, без шага модели (run4 №11 был лишним)",
          clicks(cc) == [1] and len(rt.prompts) == n0
          and "Добавить что-нибудь ещё?" in r,
          f"{clicks(cc)} {len(rt.prompts) - n0} {r!r}")
    run = finished(ag, "r1")
    bi = (run.get("brief") or {}).get("items") or [{}]
    check("в брифе — «Традиционное тесто» без «без добавок»",
          bi[0].get("options") == ["Традиционное тесто"], f"{bi}")
    plan = " ".join(x["text"] for x in ag.task_card("r1").get("plan") or ())
    check("план и «В корзине: …» — без «без добавок»",
          "без добавок" not in plan and "без добавок" not in r
          and "Традиционное тесто" in plan, f"{plan} / {r}")

    # ── 3. «Кола» и «Кола Зеро» — разные позиции ──
    print("разные позиции с общим словом:")
    S, L = ta._same_item, (lambda a, b: ta._same_item(a, b, strict=True))
    check("_same_item по умолчанию — как раньше («Кола» ~ «Кола Зеро»)",
          S("Кола", "Кола Зеро") and S("Пепперони", "пицца пепперони")
          and not S("Четыре сыра", "Четыре сезона"))
    check("strict: лишнее отличительное слово — другая позиция («Кола Зеро», "
          "«Пепперони Плюс», «Соус барбекю», «Кока-Кола», «Сет …»)",
          not L("Кола", "Кола Зеро") and not L("Пепперони", "Пепперони Плюс")
          and not L("Барбекю", "Соус барбекю") and not L("Кола", "Кока-Кола")
          and not L("Филадельфия", "Сет Филадельфия"))
    check("strict: вид, размер, мера — та же («Пицца Пепперони», «Пепперони "
          "30 см», «Большая пепперони», «Напиток Кола 0,5 литра», «Ролл …»)",
          L("Пепперони", "Пицца Пепперони") and L("Пепперони", "Пепперони 30 см")
          and L("Пепперони", "Большая пепперони")
          and L("Кола", "Напиток Кола 0,5 литра")
          and L("Филадельфия", "Ролл Филадельфия") and L("Кола 0,5 л", "кола"))
    # run4 №39: бриф «как в прошлый раз» (память) + «оставь» с Колой Зеро
    b = ta._new_brief()
    ta._apply_brief(b, {"site": "pizzatest.ru", "items": [
        {"name": "Пепперони", "size": "30 см", "options": [
            "Традиционное тесто"]}, {"name": "Кола", "size": "0,5 л"}]},
        src="memory")
    ta._apply_brief(b, {"items": [{"name": "Пепперони"}, {"name": "Кола"}, {
        "name": "Кола Зеро", "size": "0,5 л", "qty": 1}]})
    check("run4 №39: «Пепперони, Кола, Кола Зеро» — три позиции, «Кола Зеро» "
          "не слита с «Колой»",
          [(n, s) for n, s, _o, _q in items(b)] == [
              ("Пепперони", "30 см"), ("Кола", "0,5 л"),
              ("Кола Зеро", "0,5 л")], items(b))
    ta._apply_brief(b, {"items": [{"name": "Кола", "size": "1 л"},
                                  {"name": "Пицца Пепперони", "qty": 2}]})
    check("обновление по имени брифа — только своя позиция; «Пицца "
          "Пепперони» — та же «Пепперони»",
          [(n, s, q) for n, s, _o, q in items(b)] == [
              ("Пепперони", "30 см", 2), ("Кола", "1 л", 1),
              ("Кола Зеро", "0,5 л", 1)], items(b))
    b2 = json.loads(json.dumps(b))
    ta._apply_brief(b2, {"remove_items": ["Кола"]})
    check("remove_items «Кола» — только «Кола», «Кола Зеро» осталась",
          [x[0] for x in items(b2)] == ["Пепперони", "Кола Зеро"], items(b2))
    b2 = json.loads(json.dumps(b))
    ta._apply_brief(b2, {"exclude": ["Кола"]})
    check("exclude «Кола» — убрана «Кола», «Кола Зеро» осталась",
          [x[0] for x in items(b2)] == ["Пепперони", "Кола Зеро"]
          and b2["exclude"] == ["Кола"], items(b2))
    ta._apply_brief(b2, {"items": [{"name": "Кола Зеро", "qty": 2}]})
    check("«Кола Зеро» снова — исключение «Кола» не снято",
          b2["exclude"] == ["Кола"], b2["exclude"])
    ta._apply_brief(b2, {"items": [{"name": "Кола"}]})
    check("«Кола» снова — исключение снято, позиция вернулась",
          b2["exclude"] == [] and [x[0] for x in items(b2)] == [
              "Пепперони", "Кола Зеро", "Кола"], f"{b2['exclude']} {items(b2)}")
    b3 = ta._new_brief()
    ta._apply_brief(b3, {"items": [{"name": "Кола Зеро"}]})
    ta._apply_brief(b3, {"remove_items": ["кола"]})
    check("remove_items «кола» при одной «Кола Зеро» — убрана (единственная "
          "похожая)", b3["items"] == [], items(b3))
    check("промпт разбора: правило «Cola Zero» и «Cola» — разные, замена — "
          "через remove_items",
          slot0.brief and '"Cola Zero" and "Cola" are different items'
          in slot0.brief[-1] and "into remove_items" in slot0.brief[-1])

    # Корзина задачи и позиции брифа: каждое добавление — своей позиции
    two = [{"name": "Кола", "size": None, "options": [], "qty": 1},
           {"name": "Кола Зеро", "size": None, "options": [], "qty": 1}]
    zero = [{"key": "Кола Зеро", "verified": True}]
    check("_brief_pick: «Кола Зеро» корзины — «Кола Зеро» брифа; «Кока-Кола» "
          "при одной «Коле» — она; «Кола» при одной «Кола Зеро» — ничья",
          ta._brief_pick("Кола Зеро", two) is two[1]
          and ta._brief_pick("Кола", two) is two[0]
          and ta._brief_pick("Кока-Кола", two[:1]) is two[0]
          and ta._brief_pick("Кола", two[1:]) is None
          and ta._brief_pick("Пицца Пепперони 30 см", [
              {"name": "Пепперони"}]) is not None)
    cc1 = real_cc({SHOP: [cart0]}, url=SHOP, base_dir=tmp)
    ag1 = agent(cc1)

    def cart_run(adds, brief_items=two):
        return {"goal": "закажи колу и колу зеро на pizzatest.test",
                "brief": dict(ta._new_brief(), items=json.loads(json.dumps(
                    brief_items))), "cart_adds": [dict(x) for x in adds],
                "qa": [], "history": [], "lang": "ru"}
    run1 = cart_run(zero)
    check("в корзине «Кола Зеро» — «Кола» ещё не положена (_pending_items)",
          [b["name"] for b in ag1._pending_items(run1)] == ["Кола"],
          ag1._pending_items(run1))
    why = ag1._not_finished(run1, {"url": SHOP})
    check("«готово» при одной «Коле Зеро» — «Кола» не добавлена (C3)",
          bool(why) and "the user asked for Кола," in why, why)
    what, missing = ag1._added_items(run1)
    check("«В корзине: …» — «Кола Зеро», не добавлено — «Кола»",
          what == ["Кола Зеро"] and missing == ["Кола"], (what, missing))
    run2 = cart_run(zero + [{"key": "Кола", "verified": True}])
    what, missing = ag1._added_items(run2)
    check("обе в корзине — обе в составе, ничего не пропало, «готово» "
          "сходится",
          what == ["Кола Зеро", "Кола"] and missing == []
          and not ag1._pending_items(run2)
          and ag1._not_finished(run2, {"url": SHOP}) is None,
          (what, missing, ag1._not_finished(run2, {"url": SHOP})))
    run3 = cart_run([{"key": "Кола", "verified": True}], two[1:])
    check("в брифе «Кола Зеро», в корзине «Кола» — не та позиция: C3 "
          "называет и недобавленную, и лишнюю",
          "Кола Зеро" in (ag1._not_finished(run3, {"url": SHOP}) or ""),
          ag1._not_finished(run3, {"url": SHOP}))
    card = lambda t: {"idx": 5, "tag": "button", "role": "button",
                      "text": f"{t} от 119 ₽ Выбрать"}
    obs1 = {"url": SHOP, "shown": [cart0]}
    check("_done_item: карточку «Колы» (ещё не положена) открыть можно, "
          "«Колы Зеро» (положена) — нет",
          ag1._done_item(run1, card("Кола"), obs1) is None
          and ag1._done_item(run1, card("Кола Зеро"), obs1) == "Кола Зеро",
          (ag1._done_item(run1, card("Кола"), obs1),
           ag1._done_item(run1, card("Кола Зеро"), obs1)))
    run4 = cart_run(zero, [])
    check("_done_item без брифа: «Кола» — не положенная «Кола Зеро»",
          ag1._done_item(run4, card("Кола"), obs1) is None)

    # Окно товара: позиция брифа — по названию товара окна
    def win(name):
        return {"idx": 1, "tag": "button", "role": "button",
                "text": "В корзину · 119 ₽", "md": 1,
                "ctx": f"{name} 0,5 л 1 л В корзину · 119 ₽"}
    run5 = cart_run([])
    check("_brief_item: окно «Кола Зеро» при «Коле» и «Коле Зеро» брифа — "
          "«Кола Зеро» (раньше — никакая), окно «Кола» — «Кола»",
          (TaskAgent._brief_item(run5, win("Кола Зеро")) or {}).get("name")
          == "Кола Зеро"
          and (TaskAgent._brief_item(run5, win("Кола")) or {}).get("name")
          == "Кола")
    obs_z = {"url": SHOP, "shown": [win("Кола Зеро"), {
        "idx": 2, "tag": "button", "role": "radio", "text": "0,5 л", "on": 1,
        "md": 1}, {"idx": 3, "tag": "button", "role": "radio", "text": "1 л",
                   "on": 0, "md": 1}]}
    g = ag1._gate_pick(run5, obs_z["shown"][0], obs_z).get("gate") or {}
    check("_gate_pick окна «Кола Зеро» — позиция «Кола Зеро»",
          g.get("name") == "Кола Зеро", g)
    run6 = cart_run([], two[:1])
    g = ag1._gate_pick(run6, obs_z["shown"][0], obs_z).get("gate") or {}
    check("_gate_pick: одна «Кола» в брифе, окно «Кола Зеро» — как раньше, "
          "«Кола» (общее название)", g.get("name") == "Кола", g)
    rep = {"answer": "0,5", "key": "Кола Зеро",
           "ch": {"items": [{"name": "Кола", "size": "0,5 л"}]}}
    check("_answer_keeps: разбор про «Колу», окно «Кола Зеро», в брифе обе — "
          "не повтор кодом",
          not TaskAgent._answer_keeps(None, run5, rep, obs_z["shown"][0],
                                      obs_z))
    rep_z = dict(rep, ch={"items": [{"name": "Кола Зеро", "size": "0,5 л"}]})
    check("_answer_keeps: разбор про «Колу Зеро» — повтор кодом, как раньше",
          TaskAgent._answer_keeps(None, run5, rep_z, obs_z["shown"][0], obs_z))
    # Повтор того же товара (_cart_add_gate): «Кола» после «Колы Зеро» при
    # обеих в брифе — не повтор; без брифа — как раньше, повтор
    check("ключ окна «Кола» — «Кола» (_product_key)",
          TaskAgent._product_key(win("Кола"), obs1) == "Кола")
    run7 = cart_run(zero)
    run7.update(chat_id="g7", awaiting=None, steps=0, sigs=[])
    for b in run7["brief"]["items"]:
        b["src"] = "memory"  # без вопросов о товаре — сразу проверка повтора
    obs_c = {"url": SHOP, "host": "pizzatest.test", "shown": [win("Кола")]}
    r7 = ag1._cart_add_gate(run7, {"kind": "click"}, 'click "В корзину"',
                            "В корзину · 119 ₽", obs_c["shown"][0], obs_c)
    check("_cart_add_gate: «Кола» после положенной «Колы Зеро», в брифе обе — "
          "клик идёт (не «уже добавлено»)",
          r7 is None and not any("already added" in h
                                 for h in run7["history"]), (r7, run7["history"]))
    run8 = cart_run(zero, [])
    run8.update(chat_id="g8", awaiting=None, steps=0, sigs=[])
    r8 = ag1._cart_add_gate(run8, {"kind": "click"}, 'click "В корзину"',
                            "В корзину · 119 ₽", obs_c["shown"][0], obs_c)
    check("_cart_add_gate без брифа — как раньше: «Кола» ~ «Кола Зеро» — "
          "назад модели «уже добавлено»",
          r8 == ("progress", None) and any("already added" in h
                                           for h in run8["history"]),
          (r8, run8["history"]))

    print(f"\nИтого: {OK} проверок, {FAILS} провалов")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

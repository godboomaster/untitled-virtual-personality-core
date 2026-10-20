"""Тест разбора ответа по брифу без вызова модели (TaskAgent._brief_fast):
вопрос сформировал код, ответ — голый точный выбор из предложенного.

Проверяет:
* точный выбор — разбора моделью нет, изменения (ch) и бриф — как вернула
  бы модель:
  - «как в прошлый раз — на X?» / «„Якитория“ — это X?»: «да» → сайт X,
    «нет» / «другой сайт» → бриф как был;
  - «на каком сайте?» с вариантами из выдачи: номер, название, адрес →
    сайт варианта;
  - разделы магазина: номер или название раздела → бриф как был;
  - показ раздела (текст модели и список кодом): номер или название
    товара → позиция без цены;
  - «что-нибудь ещё?» кода: «нет», «всё, оформляй», «да» → бриф как был;
* сложный ответ («сырная, но 30 см», «да, но на другом сайте»), неточный
  выбор, свободный текст, вопрос модели, обзор видов вместо списка,
  «что-нибудь ещё?» с припиской, уже названная позиция — разбор моделью;
* приватный вопрос: без модели совсем (ни облака, ни локальной), значения —
  с пометкой private; сложный ответ — только локальной модели;
* скрытое ({{secretN}}) в варианте — разбор модели, значение заглушкой;
* лог: одна строка «без модели», слоты без значений; в run["traffic"]
  разбора без модели нет;
* сквозной заказ пиццы: сколько разборов ответа ушло к модели.

Запуск: PYTHONPATH=. python3 scripts/test_task_brief_fast.py
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
    """slot_router по виду промпта (как BRouter/PlanRouter в
    test_task_agent): разделы, показ раздела, «что спросить?» — текст;
    разбор ответа — JSON по ответу человека (нет в slots — {}). brief —
    промпты разбора: столько раз ответу понадобилась модель."""

    def __init__(self, sections="NONE", show="", slots=None, ask="NONE"):
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


class LogLines(logging.Handler):
    # Строки лога агента задач — проверить, что ушло в лог
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

    # Браузера в тестах нет: разметка разделов, листание, целевой снимок —
    # заглушки (как в test_task_agent)
    _ba.wait_dom_idle = lambda *a, **k: None
    _ba.section_items = lambda *a, **k: None
    _ba.section_names = lambda *a, **k: None
    _ba.scroll_step = lambda *a, **k: {"moved": True, "bottom": False}
    _ba.scroll_container_step = lambda *a, **k: {"moved": True,
                                                  "bottom": False, "y0": 0}
    # Приватная страница не зовёт живую локальную модель (Ollama): «локальной
    # нет»; вызовы считаем
    local_calls = []
    _cp.PrivateRouter.get_response = (
        lambda self, msgs, **kw: local_calls.append(msgs[-1]["content"]))
    tmp = Path(tempfile.mkdtemp(prefix="brief_fast_"))
    log = LogLines()
    logging.getLogger("app.features.task_agent").addHandler(log)
    logging.getLogger("app.features.task_agent").setLevel(logging.INFO)

    SHOP, OTHER = "https://dodopizza.test/", "https://pizzasinizza.test/"
    hdr = lambda s: {"idx": 7, "tag": "button", "role": "button", "text": s,
                     "aria": "Корзина"}
    nav = lambda cart: [
        {"idx": 1, "tag": "a", "role": "link", "text": "Пиццы"},
        {"idx": 2, "tag": "a", "role": "link", "text": "Комбо"},
        {"idx": 3, "tag": "a", "role": "link", "text": "Напитки"},
        hdr(cart)]
    pz = [{"idx": 10 + i, "tag": "a", "role": "link", "text": f"{n} от {p} ₽"}
          for i, (n, p) in enumerate([("Пепперони", 349), ("Сырная", 279),
                                      ("Терияки", 359)])]
    found = [{"title": "Додо Пицца — доставка", "snippet": "от 279 ₽",
              "url": SHOP},
             {"title": "Пицца Синица", "snippet": "от 299 ₽", "url": OTHER}]
    ta.web_search_links = lambda q, **kw: (found, None)
    _ba.snapshot_for_goal = lambda host, text, tab_id=None: (SHOP, [])

    def agent_with(slot, cc=None, mem="m.json"):
        ag = TaskAgent(cc or real_cc({SHOP: nav("0 ₽")}, base_dir=tmp),
                       memory_path=tmp / mem)
        ag.slot_router = slot
        return ag

    def new_run(**kw):
        return dict({"goal": "закажи пиццу", "qa": [], "history": [],
                     "lang": "ru", "chat_id": "u", "steps": 0, "sites": []},
                    **kw)

    def parse(ag, run, aw, answer, names=()):
        # Ответ на ожидание aw — как в feed: пара в qa, затем разбор
        run["qa"].append((aw["question"], answer))
        n0 = len(ag.slot_router.brief)
        ch = ag._update_brief(run, aw["question"], answer,
                              private=bool(aw.get("private")), aw=aw,
                              names=names)
        return ch, len(ag.slot_router.brief) - n0

    def brief_calls(run):
        return ((run.get("traffic") or {}).get("brief") or [0])[0]

    # ── «Как в прошлый раз — на X?» (вопрос кода до любого действия) ──
    print("сайт из прошлой задачи:")
    mem = tmp / "past.json"
    mem.write_text(json.dumps({"p1": [
        {"ts": 1, "goal": "закажи пиццу", "sites": ["dodopizza.test"],
         "qa": [], "result": "Дошёл до оплаты", "ok": True,
         "brief": {"site": "dodopizza.test", "items": []}}]},
        ensure_ascii=False), encoding="utf-8")
    slot = SlotRouter()
    cc = real_cc({SHOP: nav("0 ₽")}, base_dir=tmp)
    ag = TaskAgent(cc, memory_path=mem)
    ag.slot_router = slot
    r = ag.start("p1", "закажи пиццу", ScriptedRouter([
        {"action": "open", "target": SHOP},
        {"action": "fail", "message": "стоп"}]), user_id="A")
    aw = dict(ag._runs["p1"]["awaiting"])
    n0 = len(slot.brief)
    ag.feed("p1", "да", ScriptedRouter([{"action": "fail", "message": "x"}]),
            user_id="A")
    run_p = (ag.__dict__.get("_finished", {}).get("p1") or {}).get("run") \
        or ag._runs.get("p1") or {}
    site = (run_p.get("brief") or {}).get("site") or {}
    check("«как в прошлый раз — на X?» → «да»: разбора моделью нет, сайт X в "
          "брифе от человека",
          "Как в прошлый раз — на dodopizza.test?" in r
          and len(slot.brief) == n0 and site.get("value") == "dodopizza.test"
          and site.get("src") == "user", f"{site} {len(slot.brief) - n0}")
    run = new_run()
    ch, n = parse(ag, run, aw, "да")
    check("site_offer «да»: ch — {site: X}", n == 0
          and ch == {"site": "dodopizza.test"})
    for ans in ("нет", "другой сайт", "нет, лучше на другом сайте", "не здесь"):
        run = new_run()
        ch, n = parse(ag, run, aw, ans)
        check(f"site_offer «{ans}»: без модели, бриф как был",
              n == 0 and ch == {} and not run["brief"]["site"])
    run = new_run()
    ch, n = parse(ag, run, aw, "Коннор, да", names=("Коннор",))
    check("site_offer «Коннор, да» (обращение к персоне): без модели",
          n == 0 and ch == {"site": "dodopizza.test"})
    for ans in ("да, но на другом сайте", "нет, давай папа джонс",
                "да?", "ок, и пепперони сразу"):
        run = new_run()
        _ch, n = parse(ag, run, aw, ans)
        check(f"site_offer «{ans}» — сложный ответ, разбор модели", n == 1)
    # «„Якитория“ — это X? Заказываем здесь?» — тот же вид (site_offer)
    run = new_run(brief=dict(ta._new_brief(),
                             site={"value": "Якитория", "src": "user"}))
    ag._site_is_q(run, "u", "yakitoria.test")
    ch, n = parse(ag, run, run["awaiting"], "да")
    check("«„Якитория“ — это X?» → «да»: без модели, сайт — адрес",
          n == 0 and run["brief"]["site"]["value"] == "yakitoria.test")

    # ── «На каком сайте?» с вариантами из выдачи (_site_which) ──
    print("выбор сайта:")
    slot = SlotRouter()
    ag = agent_with(slot)

    def which_aw():
        run = new_run(search={"results": found})
        ag._site_which(run, "u", SHOP, {})
        return run, run["awaiting"]
    run, aw = which_aw()
    check("вопрос «где?» кода — с пометкой выбора сайта",
          aw.get("pick") == "site" and "- dodopizza.test — Додо Пицца" in
          aw["question"])
    for ans, host in (("1", "dodopizza.test"), ("2", "pizzasinizza.test"),
                      ("додо пицца", "dodopizza.test"),
                      ("давай додо", "dodopizza.test"),
                      ("dodopizza.test", "dodopizza.test"),
                      ("papajohns.test", "papajohns.test")):
        run, aw = which_aw()
        ch, n = parse(ag, run, aw, ans)
        check(f"«где?» → «{ans}»: без модели, сайт {host}",
              n == 0 and ch == {"site": host}
              and run["brief"]["site"] == {"value": host, "src": "user"},
              f"{ch} {n}")
    # «синица» с адресом pizzasinizza.test код не сверил (транслит «sinica»)
    # — не выбор для _site_pick: модели, как раньше
    for ans in ("додо, но без грибов", "не додо", "папа джонс", "пицца",
                "додо на ленина 5", "1 или 2", "первый", "синица"):
        run, aw = which_aw()
        _ch, n = parse(ag, run, aw, ans)
        check(f"«где?» → «{ans}»: не голый выбор — разбор модели", n == 1)
    # Вопрос «где?» написала модель по выдаче (адреса из неё): название —
    # в начале варианта
    ag.slot_router = SlotRouter(ask=(
        "На каком сайте заказать пиццу?\n- Додо Пицца (dodopizza.test) — от "
        "279 ₽\n- Пицца Синица (pizzasinizza.test) — от 299 ₽"))

    def which_model_aw():
        run = new_run(search={"results": found},
                      search_hosts={"dodopizza.test", "pizzasinizza.test"})
        ag._site_which(run, "u", SHOP, {})
        return run, run["awaiting"]
    run, aw = which_model_aw()
    check("вопрос «где?» модели по выдаче — тоже с пометкой выбора сайта",
          aw.get("pick") == "site"
          and aw["question"].startswith("На каком сайте заказать пиццу?"))
    for ans, host in (("синица", "pizzasinizza.test"),
                      ("2", "pizzasinizza.test"),
                      ("Додо Пицца", "dodopizza.test")):
        run, aw = which_model_aw()
        ch, n = parse(ag, run, aw, ans)
        check(f"«где?» (модель) → «{ans}»: без модели, сайт {host}",
              n == 0 and ch == {"site": host}, f"{ch} {n}")
    run, aw = which_model_aw()
    _ch, n = parse(ag, run, aw, "синица, но подешевле")
    check("«где?» (модель) → «синица, но подешевле»: разбор модели", n == 1)
    ag.slot_router = SlotRouter()

    # ── Разделы магазина (_browse_sections) ──
    print("разделы:")
    slot = SlotRouter(sections="- Пиццы\n- Комбо\n- Напитки")
    ag = agent_with(slot)
    obs_home = {"host": "dodopizza.test", "url": SHOP, "shown": nav("0 ₽"),
                "tab_id": 1}

    def sections_aw():
        run = new_run()
        b = run.setdefault("browse", {})
        ag._browse_sections(run, "u", obs_home, b, "dodopizza.test")
        return run, run["awaiting"]
    run, aw = sections_aw()
    check("вопрос о разделах — с пометкой выбора раздела",
          aw.get("pick") == "section" and aw.get("browse")
          and "- Пиццы\n- Комбо\n- Напитки" in aw["question"])
    for ans in ("пиццы", "Пиццы!", "2", "давай напитки"):
        run, aw = sections_aw()
        ch, n = parse(ag, run, aw, ans)
        check(f"разделы → «{ans}»: без модели, бриф как был",
              n == 0 and ch == {} and not run["brief"]["items"])
    run, aw = sections_aw()
    parse(ag, run, aw, "пиццы")
    ag._browse_answer(run, "пиццы")
    check("разделы → «пиццы»: раздел выбран как раньше (_browse_answer)",
          run["browse"].get("stage") == "chosen"
          and run["browse"].get("section") == "Пиццы")
    for ans in ("пиццы и напитки", "хочу пепперони", "пиццы, но без мяса",
                "не пиццы", "пиццу", "7"):
        run, aw = sections_aw()
        _ch, n = parse(ag, run, aw, ans)
        check(f"разделы → «{ans}»: разбор модели", n == 1)

    # ── Показ раздела (_browse_show): текст модели и список кодом ──
    print("показ раздела:")
    obs_pz = {"host": "dodopizza.test", "url": SHOP + "pizzas",
              "shown": nav("0 ₽") + pz, "tab_id": 1}
    model_show = ("В разделе три пиццы:\n- Пепперони — от 349 ₽\n- Сырная — "
                  "от 279 ₽\n- Терияки — от 359 ₽\nКакую заказываем?")

    def show_aw(show, run=None):
        ag.slot_router = SlotRouter(show=show)
        run = run or new_run()
        b = run.setdefault("browse", {})
        b.update(stage="opened", section="Пиццы",
                 sections=["Пиццы", "Комбо", "Напитки"])
        run["history"].append("click \"Пиццы\" → ok | the address changed")
        ag._browse_show(run, "u", obs_pz, b)
        return run, run["awaiting"]
    run, aw = show_aw(model_show)
    check("показ раздела (текст модели) — с пометкой выбора позиции и "
          "товарами страницы",
          aw.get("pick") == "item" and not aw.get("listed")
          and aw["question"] == model_show
          and aw.get("items") == [x["text"] for x in pz])
    for ans, name in (("сырная", "Сырная"), ("3", "Терияки"),
                      ("Пепперони", "Пепперони"), ("давай терияки", "Терияки")):
        run, aw = show_aw(model_show)
        ch, n = parse(ag, run, aw, ans)
        check(f"показ (модель) → «{ans}»: без модели, позиция «{name}»",
              n == 0 and ch == {"items": [{"name": name}]}
              and [(x["name"], x["qty"], x["src"]) for x in
                   run["brief"]["items"]] == [(name, 1, "user")], f"{ch}")
    for ans in ("сырную", "сырная, но 30 см", "2 сырных", "сырная и терияки",
                "без лука", "покажи все", "на твой вкус"):
        run, aw = show_aw(model_show)
        _ch, n = parse(ag, run, aw, ans)
        check(f"показ (модель) → «{ans}»: разбор модели", n == 1)
    # Модель выдумала позицию — список кодом (подписи страницы как есть)
    run, aw = show_aw("- Гавайская — 500 ₽\nКакую?")
    check("показ списком кодом — с пометкой «список кода»",
          aw.get("listed") and "- Терияки от 359 ₽" in aw["question"])
    ch, n = parse(ag, run, aw, "терияки")
    check("показ (список кодом) → «терияки»: без модели, позиция без цены и "
          "предлога («Терияки», не «Терияки от 359 ₽»)",
          n == 0 and ch == {"items": [{"name": "Терияки"}]})
    run, aw = show_aw("- Гавайская — 500 ₽\nКакую?")
    ch, n = parse(ag, run, aw, "1")
    check("показ (список кодом) → «1»: без модели, позиция «Пепперони»",
          n == 0 and ch == {"items": [{"name": "Пепперони"}]})
    # Обзор видов вместо списка (больше 10 позиций) — что в варианте вид, а
    # что товар, код не знает
    run, aw = show_aw("- Мясные — Пепперони, Терияки\n- Сырные — Сырная\n"
                      "Какую?")
    _ch, n = parse(ag, run, aw, "мясные")
    check("показ — обзор видов («Мясные — Пепперони, Терияки») → «мясные»: "
          "разбор модели", n == 1)
    # Цены модели со страницы, но не те у товаров (перепутала) — вариант не
    # сверяется с товаром: к модели
    run, aw = show_aw("- Пепперони — от 279 ₽\n- Сырная — от 349 ₽\nКакую?")
    _ch, n = parse(ag, run, aw, "пепперони")
    check("показ — цена варианта модели не у этого товара страницы — разбор "
          "модели", not aw.get("listed") and n == 1)
    # Позиция уже в брифе: ещё одна (qty) или та же — решит модель
    run = new_run(brief=dict(ta._new_brief(), items=[
        {"name": "Сырная", "size": None, "options": [], "qty": 1,
         "src": "user"}]))
    run, aw = show_aw(model_show, run)
    _ch, n = parse(ag, run, aw, "сырная")
    check("показ → позиция, уже названная в брифе: разбор модели", n == 1)

    # ── «Что-нибудь ещё?» кода (_ask_more) ──
    print("что-нибудь ещё:")
    ag = agent_with(SlotRouter())
    obs_cart = {"host": "dodopizza.test", "url": SHOP, "shown": nav("349 ₽")}

    def more_aw(private=False, missing=False):
        items = [{"name": "Пепперони", "size": None, "options": [], "qty": 1,
                  "src": "private" if private else "user"}]
        if missing:
            items.append({"name": "Маргарита", "size": None, "options": [],
                          "qty": 1, "src": "user"})
        run = new_run(brief=dict(ta._new_brief(), items=items),
                      cart_adds=[{"key": "Пепперони", "verified": True}])
        ag._ask_more(run, "u", obs_cart)
        return run, run["awaiting"]
    run, aw = more_aw()
    check("«что-нибудь ещё?» без приписок — с пометкой",
          aw.get("pick") == "more" and aw.get("more"))
    for ans in ("нет", "нет, спасибо", "всё, оформляй", "больше ничего",
                "это всё", "Нет.", "да", "давай"):
        run, aw = more_aw()
        ch, n = parse(ag, run, aw, ans)
        check(f"«что-нибудь ещё?» → «{ans}»: без модели, бриф как был",
              n == 0 and ch == {} and len(run["brief"]["items"]) == 1)
    run, aw = more_aw()
    parse(ag, run, aw, "нет")
    ag._more_answer(run, "нет", (), {})
    check("«что-нибудь ещё?» → «нет»: дальше корзина, как раньше",
          run.get("more_no") is True)
    for ans in ("нет, но убери колу", "да, ещё колу", "добавь напиток",
                "нет, но пепперони большую"):
        run, aw = more_aw()
        _ch, n = parse(ag, run, aw, ans)
        check(f"«что-нибудь ещё?» → «{ans}»: разбор модели", n == 1)
    run, aw = more_aw(missing=True)
    _ch, n = parse(ag, run, aw, "нет")
    check("«что-нибудь ещё?» с припиской «ещё не в корзине: …» → «нет» "
          "(может значить и «это убрать»): разбор модели",
          "pick" not in aw and "Ещё не в корзине" in aw["question"] and n == 1)

    # ── Приватный вопрос ──
    print("приватный вопрос:")
    run, aw = more_aw(private=True)
    local0 = len(local_calls)
    ch, n = parse(ag, run, aw, "нет")
    check("приватный «что-нибудь ещё?» → «нет»: без модели — ни облачной, ни "
          "локальной, в счёте трафика разбора нет",
          aw.get("private") and n == 0 and ch == {}
          and len(local_calls) == local0 and brief_calls(run) == 0)
    run, aw = more_aw(private=True)
    _ch, n = parse(ag, run, aw, "нет, но убери колу")
    check("приватный вопрос, сложный ответ — разбор только локальной модели "
          "(облачный slot_router не звался)",
          n == 0 and len(local_calls) == local0 + 1 and brief_calls(run) == 1)
    # Значение из ответа на приватной странице — с пометкой private:
    # облачному промпту — заглушкой (как у разбора моделью)
    run = new_run()
    aw_p = {"kind": "ask", "question": "Заказываем здесь — на shop.test? "
            "(да / другой сайт)", "private": True, "site_offer": "shop.test"}
    ch, n = parse(ag, run, aw_p, "да")
    view = ta._brief_view(run["brief"])
    check("приватный вопрос: значение без модели — src private, облаку "
          "заглушкой",
          n == 0 and run["brief"]["site"] == {"value": "shop.test",
                                              "src": "private"}
          and "shop.test" not in json.dumps(view, ensure_ascii=False))

    # ── Скрытое ({{secretN}}) в варианте ──
    print("скрытое:")
    ag = agent_with(SlotRouter())
    ag.known_secrets = lambda chat: ["Сырная"]
    run, aw = show_aw(model_show)
    _ch, n = parse(ag, run, aw, "сырная")
    p = ag.slot_router.brief[-1] if ag.slot_router.brief else ""
    check("вариант с известным секретом чата — разбор модели, значение "
          "заглушкой {{secretN}}, не открытым текстом",
          n == 1 and "{{secret" in p and "Сырная" not in p
          and "сырная" not in p)
    ag.known_secrets = None

    # ── Вопрос модели и прочие ожидания — разбор модели как раньше ──
    print("вопрос модели:")
    ag = agent_with(SlotRouter())
    aw_m = {"kind": "ask", "question": "Какую пиццу?\n- Пепперони — 349 ₽\n"
            "- Сырная — 279 ₽", "private": False, "more": False}
    run = new_run()
    _ch, n = parse(ag, run, aw_m, "1")
    check("вопрос модели с вариантами → «1»: разбор модели (что в её "
          "вариантах — позиция или размер, код не знает)", n == 1)
    aw_mm = {"kind": "ask", "question": "Что-нибудь ещё добавить в заказ?",
             "private": False, "more": True}
    run = new_run()
    _ch, n = parse(ag, run, aw_mm, "нет")
    check("«что-нибудь ещё?» модели (не кода) → «нет»: разбор модели", n == 1)
    run = new_run()
    n0 = len(ag.slot_router.brief)
    ag._update_brief(run, "(the task goal)", "закажи пепперони на додо")
    check("цель задачи — разбор модели, как раньше",
          len(ag.slot_router.brief) == n0 + 1)

    # ── Лог и счёт трафика ──
    print("лог:")
    log.lines.clear()
    ag = agent_with(SlotRouter())
    run, aw = which_aw()
    parse(ag, run, aw, "додо пицца")
    run2, aw2 = show_aw(model_show)
    parse(ag, run2, aw2, "сырная")
    fast = [x for x in log.lines if "без модели" in x]
    check("лог: на разбор без модели — одна строка [TaskAgent], слоты без "
          "значений",
          len(fast) == 2 and all(x.startswith("[TaskAgent] разбор ответа без "
                                              "модели") for x in fast)
          and fast[0].endswith(": site") and fast[1].endswith(": items")
          and not any(w in x for x in fast for w in (
              "dodopizza", "додо", "Сырная", "сырная")), f"{fast}")
    check("счёт трафика: разбора без модели в run[\"traffic\"] нет",
          brief_calls(run) == 0 and brief_calls(run2) == 0
          and "brief" not in (run.get("traffic") or {}))

    # ── Сквозной заказ пиццы: цель → «где?» → разделы → показ → вопрос о
    # товаре → «что-нибудь ещё?» → адрес ──
    print("сквозной заказ:")
    PZ, PP, PZ2 = SHOP + "pizzas", SHOP + "p/pepperoni", SHOP + "pizzas?a"
    pp = nav("0 ₽") + pz + [
        {"idx": 40, "tag": "button", "role": "button",
         "text": "Моцарелла 69 ₽", "md": 1},
        {"idx": 41, "tag": "button", "role": "button",
         "text": "В корзину за 349 ₽", "md": 1, "ctx": "Пепперони 25 см"}]
    cc = real_cc({SHOP: nav("0 ₽"), PZ: nav("0 ₽") + pz, PP: pp,
                  PZ2: nav("3 4 9 ₽") + pz},
                 clicks={1: PZ, 10: PP, 41: PZ2}, base_dir=tmp)
    slot = SlotRouter(sections="- Пиццы\n- Комбо\n- Напитки", show=model_show,
                      slots={"Ленина 5": {"address": "Ленина 5"}})
    ag = agent_with(slot, cc, mem="e2e.json")
    # Повторы шага после ответа (open после «1», «В корзину» после «без
    # добавок») делает код без модели (test_task_step_replay)
    rt = ScriptedRouter([
        {"action": "search", "query": "пицца доставка"},
        {"action": "open", "target": SHOP},
        {"action": "click", "n": 1, "label": "Пепперони от 349 ₽"},
        {"action": "click", "n": 1, "label": "В корзину за 349 ₽"},
        {"action": "ask", "question": "Куда доставить? Назови адрес."},
        {"action": "fail", "message": "стоп"}])
    steps = [ag.start("e2e", "закажи пиццу", rt, user_id="A")]
    for ans in ("1", "пиццы", "пепперони", "без добавок", "нет", "Ленина 5"):
        steps.append(ag.feed("e2e", ans, rt, user_id="A"))
    run_e = (ag.__dict__.get("_finished", {}).get("e2e") or {}).get("run") \
        or ag._runs.get("e2e") or {}
    br = run_e.get("brief") or {}
    want_qs = ("На каком сайте это сделать?", "есть разделы", "Какую "
               "заказываем?", "Перед тем как положить «Пепперони»",
               "Добавить что-нибудь ещё?", "Куда доставить?")
    check("сквозной: вопросы по порядку — сайт, разделы, показ, товар, "
          "«что-нибудь ещё?», адрес",
          all(q in s for q, s in zip(want_qs, steps)),
          f"{[s[-60:] for s in steps]}")
    check("сквозной: бриф — сайт, позиция и адрес, как при разборе моделью",
          (br.get("site") or {}).get("value") == "dodopizza.test"
          and [x["name"] for x in br.get("items") or ()] == ["Пепперони"]
          and (br.get("address") or {}).get("value") == "Ленина 5", f"{br}")
    answers = len(want_qs)
    model = len(slot.brief) - 1  # без разбора цели
    check(f"сквозной: из {answers} ответов к модели — {model} (вопрос о "
          "товаре и адрес), разбор цели — тоже модели",
          model == 2 and brief_calls(run_e) == 3
          and "без добавок" in slot.brief[1] and "Ленина 5" in slot.brief[2])
    print(f"  сквозной заказ: разборов моделью {len(slot.brief)} из "
          f"{answers + 1} (раньше — все {answers + 1})")

    print(f"\nИтого: {OK} проверок, {FAILS} провалов")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

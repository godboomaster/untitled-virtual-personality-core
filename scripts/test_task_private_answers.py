"""Тест: ответ на вопрос с приватной страницы не уходит облаку.

Стенд 20.10: на /checkout (приватная — только локальная модель) агент
спросил адрес, человек ответил «ул. Тестовая, 1, кв. 2». Вопрос в облачных
промптах был заглушкой, а ответ — нет: в той же задаче (блок «вопросы и
ответы») и в следующей задаче чата (блок прошлых задач на /delivery).

Проверяет:
* та же задача: облачный промпт шага и вызова «по странице» — заглушка
  вопроса и ответа, бриф — заглушка слота; локальный промпт — значение;
* память задач: на диске значение как есть и пометка qa_private;
  следующая задача — облаку заглушка, локальной модели (снова на
  оформлении) — значение;
* старая запись без пометки: вопрос сохранён заглушкой — ответ тоже скрыт;
* «как в прошлый раз» по неприватным данным: позиции и сайт из памяти,
  ответы с обычных страниц — видны облаку, сайт предлагается;
* секреты (телефон) — маской и на диске, и в промптах;
* исключение брифа с приватной страницы — облаку заглушкой; заглушку,
  возвращённую моделью, бриф не берёт значением;
* приписка во время паузы на приватной странице — как ответ оттуда;
* веб-поиск с ответом с приватной страницы в запросе — запрос и выдача
  облаку не показываются.

Без браузера, веб-чатов и настоящих LLM: локальная модель — подмена
PrivateRouter.get_response, облако — ScriptedRouter.

Запуск: python3 -m scripts.test_task_private_answers
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.test_task_agent import FakeCC, ScriptedRouter  # noqa: E402

FAILS = 0
OK = 0

ADDR = "ул. Тестовая, 1, кв. 2"
PHONE = "+7 999 123-45-67"


def check(name, cond, detail=""):
    global FAILS, OK
    OK += 1
    if cond:
        print(f"  [OK] {name}")
    else:
        FAILS += 1
        print(f"  [FAIL] {name} {detail}")


class ShopCC(FakeCC):
    """Магазин из двух страниц: /checkout (приватная по встроенным
    признакам) и /delivery (обычная). «Зоны доставки» уводит на /delivery."""

    PAGES = {
        "https://pizza.test/checkout": [
            {"idx": 40, "tag": "input", "role": "textbox",
             "text": "Адрес доставки", "ed": True, "vp": True},
            {"idx": 41, "tag": "a", "role": "link", "text": "Зоны доставки",
             "vp": True},
        ],
        "https://pizza.test/delivery": [
            {"idx": 50, "tag": "div", "text": "Доставка за 35 минут",
             "vp": True},
            {"idx": 51, "tag": "a", "role": "link", "text": "Меню",
             "vp": True},
        ],
    }

    def __init__(self, url):
        super().__init__()
        self.url = url

    def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
        return self.url, "pizza.test", [dict(x) for x in self.PAGES[self.url]], \
            1, None

    def execute(self, act, chat_id="", router=None):
        self.executed.append(dict(act))
        if act.get("kind") == "click" and act.get("idx") == 41:
            self.url = "https://pizza.test/delivery"
        return True, ""


class SlotRouter:
    """slot_router: облачные вызовы разбора/«по странице» — запоминает."""

    def __init__(self):
        self.prompts = []

    def get_response(self, messages, **kw):
        self.prompts.append(messages[-1]["content"])
        return "{}"


class Local:
    """Подмена PrivateRouter.get_response: разбор ответа — адрес в слот,
    шаги — по сценарию; промпты — в prompts."""

    def __init__(self, steps=(), slots=None):
        self.steps = list(steps)
        self.slots = slots
        self.prompts = []

    def __call__(self, _router, messages, **kw):
        p = messages[-1]["content"]
        self.prompts.append(p)
        if "You keep the brief" in p:
            return json.dumps(self.slots or {}, ensure_ascii=False)
        if "autopilot" not in p:
            return None
        if not self.steps:
            return None
        s = self.steps.pop(0)
        return s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)


def _step_prompts(prompts):
    return [p for p in prompts if p.startswith("You are an autopilot")]


def _obs(url, run):
    return {"url": url, "host": "pizza.test", "tab_id": 1,
            "shown": [dict(x) for x in ShopCC.PAGES[url]], "note": None,
            "text": None, "error": None, "search": run.get("search"),
            "private": "checkout" in url}


def main():
    from app.features import cc_privacy as _P
    from app.features import task_agent as ta
    from app.features.task_agent import TaskAgent

    tmp = Path(tempfile.mkdtemp(prefix="ta_privans_"))
    orig_local = _P.PrivateRouter.get_response

    # ── 1. Блок «вопросы и ответы» прогона ──
    print("вопросы и ответы прогона")
    qa = [("Какую пиццу?", "пепперони"), ("Куда доставить?", ADDR)]
    cloud = "\n".join(ta._qa_lines(qa, {1}))
    local = "\n".join(ta._qa_lines(qa, set()))
    check("облаку: ответ с приватной страницы — заглушкой, вопрос тоже",
          ADDR not in cloud and ta._PRIVATE_ANSWER in cloud
          and ta._PRIVATE_QUESTION in cloud and "Куда доставить" not in cloud)
    check("облаку: ответ с обычной страницы — как есть",
          "пепперони" in cloud and "Какую пиццу?" in cloud)
    check("локальной модели — значение", ADDR in local)

    try:
        # ── 2. Та же задача: вопрос на /checkout, следующий шаг на /delivery ──
        print("та же задача: облако после ответа с приватной страницы")
        mem = tmp / "mem.json"
        cc = ShopCC("https://pizza.test/checkout")
        agent = TaskAgent(cc, memory_path=mem)
        slot = SlotRouter()
        agent.slot_router = slot
        loc = Local(steps=[{"action": "ask",
                            "question": "Куда доставить заказ?"},
                           {"action": "click", "n": 2}],
                    slots={"address": ADDR})
        _P.PrivateRouter.get_response = (
            lambda self, messages, **kw: loc(self, messages, **kw))
        router = ScriptedRouter([{"action": "fail", "message": "стоп"}])
        r1 = agent.start("c1", "заполни форму на pizza.test", router,
                         user_id="A")
        run = agent._runs.get("c1") or {}
        check("вопрос задан на приватной странице и помечен",
              "Куда доставить" in r1
              and (run.get("awaiting") or {}).get("private") is True)
        n_slot = len(slot.prompts)
        agent.feed("c1", ADDR, router, user_id="A")
        steps_local = _step_prompts(loc.prompts)
        check("локальный промпт шага после ответа — значение видно",
              steps_local and ADDR in steps_local[-1], f"{len(steps_local)}")
        check("разбор ответа — локальной модели, облаку нет",
              any("You keep the brief" in p and ADDR in p
                  for p in loc.prompts)
              and not any(ADDR in p for p in slot.prompts[n_slot:]))
        cloud_steps = _step_prompts(router.prompts)
        check("облачный шаг на /delivery был", bool(cloud_steps)
              and "pizza.test/delivery" in cloud_steps[-1],
              f"{len(router.prompts)}")
        cp = cloud_steps[-1] if cloud_steps else ""
        check("облачный промпт: ни адреса, ни его частей",
              ADDR not in cp and "Тестовая" not in cp, cp[:600])
        check("облачный промпт: заглушки вопроса и ответа",
              ta._PRIVATE_ANSWER in cp and ta._PRIVATE_QUESTION in cp)
        check("облачный промпт: слот брифа — заглушкой",
              ta._PRIVATE_SLOT in cp)

        # Вызов «по странице» (slot_router) с обычной страницы — облако
        run_l = (agent.__dict__.get("_finished", {}).get("c1") or {}) \
            .get("run") or agent._runs.get("c1") or run
        slot.prompts.clear()
        agent._pre_llm(run_l, _obs("https://pizza.test/delivery", run_l),
                       "open the menu", "Page:\n- Меню\n", "\nWhat to ask?\n")
        check("вызов «по странице» в облако: ответ заглушкой",
              slot.prompts and ADDR not in slot.prompts[-1]
              and "Тестовая" not in slot.prompts[-1]
              and ta._PRIVATE_ANSWER in slot.prompts[-1])
        loc.prompts.clear()
        agent._pre_llm(run_l, _obs("https://pizza.test/checkout", run_l),
                       "fill in the form", "Page:\n- Адрес\n",
                       "\nWhat to ask?\n")
        check("вызов «по странице» локальной модели: значение видно",
              loc.prompts and ADDR in loc.prompts[-1])

        # ── 3. Память задач ──
        print("память задач и следующая задача")
        data = json.loads(mem.read_text(encoding="utf-8"))
        rec = (data.get("c1") or [{}])[-1]
        check("на диске: значение как есть, вопрос заглушкой, пометка",
              rec.get("qa") and rec["qa"][0][1] == ADDR
              and rec["qa"][0][0] == ta._PRIVATE_QUESTION
              and rec.get("qa_private") == [0], f"{rec}")
        check("на диске: итог прогона с приватной страницей — заглушкой",
              rec.get("result") == ta._PRIVATE_RESULT)

        cc2 = ShopCC("https://pizza.test/delivery")
        r2 = ScriptedRouter([{"action": "fail", "message": "стоп"}])
        TaskAgent(cc2, memory_path=mem).start(
            "c1", "заполни форму на pizza.test снова", r2, user_id="A")
        cp2 = (_step_prompts(r2.prompts) or [""])[0]
        check("следующая задача: блок прошлых задач в облачном промпте есть",
              "earlier tasks" in cp2, cp2[:300])
        check("следующая задача: ответ прошлой — облаку заглушкой",
              ADDR not in cp2 and "Тестовая" not in cp2
              and ta._PRIVATE_ANSWER in cp2)

        loc3 = Local(steps=[{"action": "fail", "message": "стоп"}])
        _P.PrivateRouter.get_response = (
            lambda self, messages, **kw: loc3(self, messages, **kw))
        cc3 = ShopCC("https://pizza.test/checkout")
        r3 = ScriptedRouter([])
        TaskAgent(cc3, memory_path=mem).start(
            "c1", "заполни форму на pizza.test ещё", r3, user_id="A")
        lp3 = (_step_prompts(loc3.prompts) or [""])[0]
        check("снова на оформлении: локальной модели адрес прошлой задачи "
              "виден", "earlier tasks" in lp3 and ADDR in lp3, lp3[:400])
        check("снова на оформлении: облако не спрошено", not r3.prompts)
        data = json.loads(mem.read_text(encoding="utf-8"))
        check("записи следующих задач: старая пометка на месте",
              (data["c1"][0].get("qa_private") == [0]))
    finally:
        _P.PrivateRouter.get_response = orig_local

    # ── 4. Старая запись без пометки ──
    print("старая запись памяти")
    old = {"ts": 1, "goal": "закажи пиццу", "sites": ["pizza.test"],
           "qa": [["Какую пиццу?", "пепперони"],
                  [ta._PRIVATE_QUESTION, ADDR],
                  [ta._PRIVATE_QUESTION, "***(16), имя Тест"]],
           "result": ta._PRIVATE_RESULT}
    line = TaskAgent._past_line(old)
    check("облаку: ответы под заглушкой вопроса — скрыты",
          ADDR not in line and "имя Тест" not in line
          and line.count(ta._PRIVATE_ANSWER) == 2)
    check("облаку: ответ с обычной страницы — виден", "пепперони" in line)
    line_l = TaskAgent._past_line(old, local=True)
    check("локальной модели — значения", ADDR in line_l
          and "имя Тест" in line_l)
    bad = dict(old, qa_private="x")
    check("испорченная пометка — не падает, заглушка по вопросу",
          ADDR not in TaskAgent._past_line(bad))
    marked = {"ts": 1, "goal": "g", "qa": [["Куда?", ADDR]],
              "qa_private": [0]}
    check("пометка без заглушки вопроса — тоже скрыто",
          ADDR not in TaskAgent._past_line(marked)
          and "Куда?" not in TaskAgent._past_line(marked))

    # ── 5. «Как в прошлый раз» по неприватным данным ──
    print("«как в прошлый раз»")
    mem5 = tmp / "mem5.json"
    rec5 = {"ts": int(time.time()), "goal": "закажи пиццу на pizza.test",
            "sites": ["pizza.test"], "ok": True,
            "qa": [["Какую пиццу?", "пепперони 30 см"],
                   [ta._PRIVATE_QUESTION, ADDR]],
            "qa_private": [1],
            "brief": {"site": "pizza.test",
                      "items": [{"name": "Пепперони", "size": "30 см",
                                 "options": [], "qty": 1}]},
            "result": ta._PRIVATE_RESULT}
    mem5.write_text(json.dumps({"c5": [rec5]}, ensure_ascii=False),
                    encoding="utf-8")
    ag5 = TaskAgent(ShopCC("https://pizza.test/delivery"), memory_path=mem5)
    goal = "закажи пиццу как в прошлый раз"
    run5 = {"goal": goal, "lang": "ru", "qa": [], "history": [], "steps": 0,
            "sites": [], "past": ag5._past_tasks("c5", goal),
            "brief": ta._new_brief(), "chat_id": "c5"}
    last = next((r for r in run5["past"] if r.get("ok")), None)
    check("прошлый успешный заказ найден", last is not None)
    ta._apply_brief(run5["brief"], (last or {}).get("brief"), src="memory")
    p5 = ag5._prompt(run5, _obs("https://pizza.test/delivery", run5))
    check("облаку: позиция и сайт из памяти, ответ с обычной страницы",
          "Пепперони" in p5 and "from memory" in p5
          and "пепперони 30 см" in p5 and "pizza.test" in p5)
    check("облаку: адрес прошлого заказа — нет", ADDR not in p5
          and "Тестовая" not in p5)
    run5b = {"goal": "закажи пиццу", "lang": "ru", "qa": [],
             "past": ag5._past_tasks("c5", "закажи пиццу"),
             "brief": ta._new_brief()}
    check("сайт прошлого заказа предлагается («как в прошлый раз — на X?»)",
          ag5._site_offer(run5b) == "pizza.test")

    # Запись прогона: позиции с обычных страниц — для повтора, с приватной —
    # нет; адрес — не в бриф записи
    mem6 = tmp / "mem6.json"
    ag6 = TaskAgent(ShopCC("https://pizza.test/delivery"), memory_path=mem6)
    br6 = ta._new_brief()
    ta._apply_brief(br6, {"site": "pizza.test",
                          "items": [{"name": "Пепперони", "size": "30 см"}]})
    ta._apply_brief(br6, {"address": ADDR,
                          "items": [{"name": "Подарок для Маши"}]},
                    src="private")
    run6 = {"goal": "закажи пиццу на pizza.test", "lang": "ru",
            "qa": [("Какую пиццу?", "пепперони 30 см"),
                   ("Куда доставить?", ADDR),
                   ("Какой у вас телефон?", PHONE)],
            "qa_private": {1}, "history": [], "sites": ["pizza.test"],
            "brief": br6, "outcome": "payment", "private_seen": True,
            "chat_id": "c6"}
    ag6._remember("c6", run6, "дошёл до оплаты")
    rec6 = json.loads(mem6.read_text(encoding="utf-8"))["c6"][-1]
    check("запись: бриф для повтора — сайт и позиция с обычной страницы",
          (rec6.get("brief") or {}).get("site") == "pizza.test"
          and [x["name"] for x in rec6["brief"]["items"]] == ["Пепперони"],
          f"{rec6.get('brief')}")
    check("запись: адрес — не в брифе записи, пометка у его пары",
          ADDR not in json.dumps(rec6.get("brief"), ensure_ascii=False)
          and rec6.get("qa_private") == [1])

    # ── 6. Секреты по-прежнему маской ──
    print("секреты")
    disk6 = mem6.read_text(encoding="utf-8")
    check("на диске: телефон-ответ — маской", PHONE not in disk6
          and "999" not in disk6)
    line6 = TaskAgent._past_line(rec6)
    check("прошлая задача облаку: телефон маской, адрес заглушкой",
          PHONE not in line6 and "***(" in line6 and ADDR not in line6)
    check("прошлая задача локальной модели: телефон всё равно маской",
          PHONE not in TaskAgent._past_line(rec6, local=True))
    rec_old = {"ts": 1, "goal": "g",
               "qa": [["Какой у вас телефон?", PHONE]]}
    check("старая запись с открытым телефоном — маской при показе",
          PHONE not in TaskAgent._past_line(rec_old))
    run7 = {"goal": "заполни форму на pizza.test", "lang": "ru",
            "qa": [("Куда доставить?", f"{ADDR}, {PHONE}")],
            "qa_private": {0}, "history": [], "steps": 0, "sites": [],
            "chat_id": "c7"}
    p7l = ag6._prompt(run7, _obs("https://pizza.test/checkout", run7),
                      local=True)
    p7c = ag6._prompt(run7, _obs("https://pizza.test/delivery", run7))
    check("локальной модели: адрес виден, телефон — {{secretN}}",
          ADDR in p7l and PHONE not in p7l and "{{secret" in p7l)
    check("облаку: ни адреса, ни телефона", ADDR not in p7c
          and PHONE not in p7c and ta._PRIVATE_ANSWER in p7c)

    # ── 7. Бриф: исключения и заглушки ──
    print("бриф")
    b = ta._new_brief()
    ta._apply_brief(b, {"exclude": ["ананасы"]})
    ta._apply_brief(b, {"exclude": ["подарок для Маши"]}, src="private")
    cl = "\n".join(ta._brief_lines(b))
    ll = "\n".join(ta._brief_lines(b, local=True))
    check("облаку: исключение с приватной страницы — заглушкой, обычное — "
          "как есть", "Маши" not in cl and ta._PRIVATE_SLOT in cl
          and "ананасы" in cl)
    check("локальной модели: исключение видно", "Маши" in ll)
    check("служебная пометка — ни в один вид брифа",
          "exclude_private" not in json.dumps(ta._brief_view(b))
          and "exclude_private" not in json.dumps(ta._brief_view(b, True)))
    was = json.dumps(b, sort_keys=True, ensure_ascii=False)
    ta._apply_brief(b, {"items": [{"name": ta._PRIVATE_SLOT, "qty": 2}],
                        "exclude": [ta._PRIVATE_SLOT]})
    check("заглушка от модели — не позиция и не исключение",
          json.dumps(b, sort_keys=True, ensure_ascii=False) == was)

    # ── 8. Приписка во время паузы на приватной странице ──
    print("приписка во время паузы")
    try:
        _P.PrivateRouter.get_response = lambda self, messages, **kw: None
        cc8 = ShopCC("https://pizza.test/checkout")
        ag8 = TaskAgent(cc8, memory_path=tmp / "mem8.json")
        ag8.start("c8", "заполни форму на pizza.test", ScriptedRouter([]),
                  user_id="A")
        run8 = ag8._runs.get("c8") or {}
        check("пауза на приватной странице без локальной модели",
              (run8.get("awaiting") or {}).get("kind") == "continue")
        cc8.url = "https://pizza.test/delivery"
        r8 = ScriptedRouter([{"action": "fail", "message": "стоп"}])
        ag8.feed("c8", f"я сам ввёл адрес {ADDR}", r8, user_id="A")
        cp8 = (_step_prompts(r8.prompts) or [""])[0]
        check("облаку: приписка с паузы на оформлении — заглушкой",
              cp8 and ADDR not in cp8 and ta._PRIVATE_ANSWER in cp8,
              cp8[:300])
    finally:
        _P.PrivateRouter.get_response = orig_local

    # ── 9. Веб-поиск с ответом с приватной страницы ──
    print("веб-поиск")
    orig_search = ta.web_search_links
    try:
        ta.web_search_links = lambda q, engine="google": ([
            {"title": "Тестовая улица, 1 — индекс 101000",
             "url": "https://map.test/testovaya-1",
             "snippet": "Тестовая ул., 1: индекс, как добраться"}], None)
        ag9 = TaskAgent(ShopCC("https://pizza.test/checkout"),
                        memory_path=tmp / "mem9.json")
        run9 = {"goal": "заполни форму на pizza.test", "lang": "ru",
                "qa": [("Куда доставить?", ADDR)], "qa_private": {0},
                "history": [], "steps": 3, "sites": [], "chat_id": "c9",
                "page_private": True}
        ag9._do_search(run9, "ул. Тестовая индекс", chat_id="c9")
        check("поиск с ответом с приватной страницы — помечен",
              (run9.get("search") or {}).get("private") is True)
        p9c = ag9._prompt(run9, _obs("https://pizza.test/delivery", run9))
        p9l = ag9._prompt(run9, _obs("https://pizza.test/checkout", run9),
                          local=True)
        check("облаку: ни запроса, ни выдачи", "Тестовая" not in p9c
              and "map.test" not in p9c and "search again" in p9c, p9c[:500])
        check("локальной модели: выдача видна", "map.test" in p9l)
        run9b = dict(run9, search=None, history=[], page_private=False)
        ag9._do_search(run9b, "пицца доставка", chat_id="c9")
        p9b = ag9._prompt(run9b, _obs("https://pizza.test/delivery", run9b))
        check("обычный поиск — выдача облаку как раньше",
              not (run9b.get("search") or {}).get("private")
              and "map.test" in p9b)
    finally:
        ta.web_search_links = orig_search

    print(f"\nИтого: {OK} проверок, {FAILS} провалов")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

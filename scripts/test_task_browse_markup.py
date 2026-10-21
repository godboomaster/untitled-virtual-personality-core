"""Тест показа раздела магазина по разметке (_browse_show / _browse_open /
_browse_unclicked, _SECTION_ITEMS_JS).

Run4 21.10 (стенд pizzatest.ru): №5 — раздел «Пицца» по разметке, а список
«- название — цена» писала модель (~1,7 тыс. симв., ~10 с); №31 — клик
«Напитки» кодом перекрыт окном «Ваш город — Москва?» (в снимке окном не
помечено), показ ушёл модели шагов (~8,4 тыс. симв.). Проверяет:
* позиции раздела по разметке целиком (у каждой название и цена), их
  2..SECTION_LIST_MAX — список кодом без модели (тот же текст и пометка
  listed, что у запасного списка), по-русски и по-английски; кнопка
  карточки после цены («Выбрать») — мимо; голый выбор из списка — позиция
  без модели. Больше 10, позиция без названия (список неполон), позиции
  из ленты снимка (разметки нет) — модель, как раньше;
* клик раздела перекрыт / не прошёл / подписи нет в списке, а меню
  одностраничное (ссылки разделов — якоря этой страницы) — раздел по
  разметке без клика, без модели; строка истории модели шагов: показан без
  клика, слой на месте (закрывать ли — решает она). Ссылка раздела ведёт
  на другую страницу, позиций по разметке меньше двух — как раньше,
  модель шагов; больше 10 — показ моделью по разметке;
* _SECTION_ITEMS_JS на настоящем движке (headless-Chromium Playwright,
  страница стенда — перехватом, без сети): «название — цена» (все 9 пицц,
  длинная карточка, без бейджа «Новинка»), напитки при открытом окне
  города; окно города — не md, центр «Напитки» им перекрыт; две цены,
  обёртка с несколькими заголовками, подпись картинки, «from $1.19», «от»
  не внутри слова. Нет Playwright/Chromium — SKIP этой части.

Браузер — real_cc (test_task_agent): настоящий ComputerControlManager,
заглушены браузер и снимок; модель — скрипт. Живых браузеров, веб-чатов и
LLM нет.

Запуск: python3 -m scripts.test_task_browse_markup
"""

import glob
import json
import logging
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


class SecRouter:
    """slot_router по виду промпта: разделы, показ раздела — текст; разбор
    цели/ответа — JSON по очереди slots (нет — {})."""
    SEC = "sections of the site's catalog"

    def __init__(self, show="", slots=()):
        self.show, self.slots, self.prompts = show, list(slots), []

    def get_response(self, messages, **kw):
        p = messages[-1]["content"]
        self.prompts.append(p)
        if self.SEC in p:
            return "NONE"
        if "Present it:" in p:
            return self.show
        return json.dumps(self.slots.pop(0) if self.slots else {},
                          ensure_ascii=False)

    def shows(self):
        return [p for p in self.prompts if "Present it:" in p]


class _Log(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


# Браузера в тестах нет: разделы и позиции по разметке — подмена
HEADS = []
ITEMS = {}
_ba.section_names = lambda host, tab_id=None: list(HEADS) or None
_ba.section_items = lambda host, sec, tab_id=None: (
    list(ITEMS[sec]) if ITEMS.get(sec) is not None else None)
_ba.wait_dom_idle = lambda *a, **k: None
_ba.scroll_step = lambda *a, **k: {"moved": True, "bottom": False}
_ba.scroll_container_step = lambda *a, **k: {"moved": True, "bottom": False,
                                             "y0": 0}
_ba.snapshot_for_goal = lambda host, text, tab_id=None: (None, [])
# Приватная страница не зовёт живую локальную модель (Ollama)
_cp.PrivateRouter.get_response = lambda self, msgs, **kw: None

log = _Log()
_tl = logging.getLogger(ta.__name__)
_tl.addHandler(log)
_tl.setLevel(logging.INFO)

TMP = Path(tempfile.mkdtemp(prefix="task_browse_mk_"))
SITE = "pizzatest.test"
HOME = f"https://{SITE}/"
NAV = "Пицца Комбо Закуски Напитки Акции Контакты"
SECTIONS = ["Пицца", "Комбо", "Закуски", "Напитки"]
CITY = "Ваш город — Москва? Да Выбрать другой"
COVERED = ("клик не выполнен: элемент перекрыт другим слоем (окном или "
           "баннером)")


def link(idx, text, ctx="", **kw):
    return {"idx": idx, "tag": "a", "role": "link", "text": text,
            "ctx": ctx, "vp": True, **kw}


def page(hrefs=("#pizza", "#combo", "#snacks", "#drinks"), labels=SECTIONS,
         drinks=None):
    # Главная стенда: шапка, панель разделов (якоря этой страницы), окно
    # города (не md — как в снимке run4), карточки «от … ₽ Выбрать»
    head = [link(1, "Пицца Тест Доставка пиццы за 35 минут", "Пицца Тест"),
            {"idx": 2, "tag": "button", "role": "button", "text": "Войти",
             "vp": True},
            {"idx": 3, "tag": "button", "role": "button", "text": "Корзина 0",
             "vp": True}]
    nav = [link(20 + i, x, NAV, href=(HOME + h if h.startswith("#") else h))
           for i, (x, h) in enumerate(zip(labels, hrefs))]
    city = [{"idx": 36, "tag": "button", "role": "button", "text": "Да",
             "ctx": CITY, "vp": True},
            {"idx": 37, "tag": "button", "role": "button",
             "text": "Выбрать другой", "ctx": CITY, "vp": True}]
    cards = [{"idx": 50 + i, "tag": "button", "role": "button",
              "text": f"от {p} ₽ Выбрать", "cn": n, "ctx": n, "vp": False}
             for i, (n, p) in enumerate(drinks or (("Пепперони", 459),
                                                   ("Кола", 119),
                                                   ("Кола Зеро", 119)))]
    return head + nav + city + cards


DRINKS = ["Кола — от 119 ₽", "Кола Зеро — от 119 ₽",
          "Морс клюквенный — 139 ₽", "Сок апельсиновый — 199 ₽",
          "Вода негазированная — 79 ₽", "Холодный чай лимон — 129 ₽"]
CODE_LIST = ("В разделе «Напитки»:\n- Кола — от 119 ₽\n- Кола Зеро — от 119 ₽"
             "\n- Морс клюквенный — 139 ₽\n- Сок апельсиновый — 199 ₽\n- Вода "
             "негазированная — 79 ₽\n- Холодный чай лимон — 129 ₽\nЧто "
             "заказываем?")


def fresh(name, pages=None, items=None, show="", slots=None, covered=False,
          heads=SECTIONS):
    HEADS[:] = list(heads)
    ITEMS.clear()
    ITEMS["Напитки"] = list(DRINKS) if items is None else items
    cc = real_cc(pages or {HOME: page()}, url=HOME, base_dir=TMP)
    if covered:
        # Клик «Напитки» (23) перекрыт окном города: клика не было
        orig = cc._dispatch

        def disp(action, router=None):
            if action.get("kind") == "click" and action.get("idx") == 23:
                cc.dispatched.append(dict(action))
                raise RuntimeError(COVERED)
            return orig(action, router)
        cc._dispatch = disp
    agent = TaskAgent(cc, memory_path=TMP / f"{name}.json")
    agent.slot_router = SecRouter(show, [{"kinds": ["напиток"]}]
                                  if slots is None else slots)
    return cc, agent


def calls(agent, chat, kind):
    run = agent._runs.get(chat) or {}
    return (run.get("traffic") or {}).get(kind, [0])[0]


def clicked(cc):
    return [d.get("idx") for d in cc.dispatched if d.get("kind") == "click"]


def stop():
    return ScriptedRouter([{"action": "fail", "message": "стоп"}])


GOAL = f"закажи напиток на {SITE}"

print("Раздел по разметке целиком, до 10 позиций — список кодом")
log.msgs.clear()
cc, agent = fresh("e1")
rt = stop()
r = agent.start("e1", GOAL, rt, user_id="A")
run = agent._runs.get("e1") or {}
aw = run.get("awaiting") or {}
check("«Напитки» нажаты кодом, человеку — список кода «название — цена»",
      clicked(cc) == [23] and r.endswith(CODE_LIST), (r, cc.dispatched))
check("ни вызова «по странице» (показ), ни хода модели шагов",
      calls(agent, "e1", "page") == 0 and not agent.slot_router.shows()
      and not rt.prompts, (agent.slot_router.prompts, rt.prompts))
check("пометка — как у запасного списка кодом: pick item, listed, позиции",
      aw.get("pick") == "item" and aw.get("listed") and aw.get("browse")
      and aw.get("items") == DRINKS
      and (run.get("browse") or {}).get("stage") == "presented", aw)
check("лог: «список кодом, без модели»",
      any("раздел «Напитки» по разметке: 6 — список кодом" in m
          for m in log.msgs), log.msgs)
n_brief = calls(agent, "e1", "brief")
rt2 = stop()
agent.feed("e1", "2", rt2, user_id="A")
brief = run.get("brief") or {}
check("голый выбор «2» из списка кода — позиция «Кола Зеро» без вызова "
      "разбора", [x["name"] for x in brief.get("items") or ()] == ["Кола Зеро"]
      and n_brief == 1
      and (run.get("traffic") or {}).get("brief", [0])[0] == n_brief,
      (brief.get("items"), n_brief, run.get("traffic")))

# Кнопка карточки после цены (текст карточки без заголовка) — мимо
cc, agent = fresh("e2", items=["Кола 0,5 л 119 ₽ Выбрать",
                               "Морс 0,5 л 139 ₽ Выбрать"])
r = agent.start("e2", GOAL, stop(), user_id="A")
check("«Кола 0,5 л 119 ₽ Выбрать» — в списке без «Выбрать», модели нет",
      r.endswith("В разделе «Напитки»:\n- Кола 0,5 л 119 ₽\n- Морс 0,5 л "
                 "139 ₽\nЧто заказываем?")
      and calls(agent, "e2", "page") == 0, r)

# По-английски — та же фраза банка на языке хода
EN_NAV = "Pizza Combos Drinks Contacts"
en_page = ([link(20 + i, x, EN_NAV, href=HOME + "#" + x.lower())
            for i, x in enumerate(["Pizza", "Combos", "Drinks"])]
           + [{"idx": 50, "tag": "button", "role": "button",
               "text": "from $1.19 Select", "cn": "Cola", "ctx": "Cola",
               "vp": False}])
cc, agent = fresh("e3", pages={HOME: en_page}, heads=["Pizza", "Combos",
                                                      "Drinks"],
                  slots=[{"kinds": ["drink"]}])
ITEMS.clear()
ITEMS["Drinks"] = ["Cola — from $1.19", "Lemonade — $2.49 Add to cart"]
cc.turn_lang = lambda: "en"
r = agent.start("e3", f"order a drink on {SITE}", stop(), user_id="A")
if "has these sections" in r:
    r = agent.feed("e3", "Drinks", stop(), user_id="A")
check("EN: «In \"Drinks\": … What shall I order?», кнопка «Add to cart» "
      "после цены — мимо, модели нет",
      r.endswith("In \"Drinks\":\n- Cola — from $1.19\n- Lemonade — $2.49\n"
                 "What shall I order?")
      and calls(agent, "e3", "page") == 0, r)

print("Показ моделью, как раньше — больше 10, список неполон, лента снимка")
many = [f"Напиток{chr(0x430 + i)} — {100 + i} ₽" for i in range(12)]
cc, agent = fresh("e4", items=many,
                  show="В разделе 12 напитков, от 100 до 111 ₽. Какой?")
r = agent.start("e4", GOAL, stop(), user_id="A")
shows = agent.slot_router.shows()
check("12 позиций по разметке — обзор модели: модели — только позиции "
      "разметки (не карточки снимка)",
      calls(agent, "e4", "page") == 1 and len(shows) == 1
      and "found by the page's markup" in shows[0]
      and "- Напитока — 100 ₽" in shows[0] and "Пепперони" not in shows[0]
      and r.endswith("В разделе 12 напитков, от 100 до 111 ₽. Какой?"),
      (r, shows))
cc, agent = fresh("e5", items=DRINKS[:5] + ["от 429 ₽ Выбрать"],
                  show="- Кола — от 119 ₽\n- Морс клюквенный — 139 ₽\nКакой?")
r = agent.start("e5", GOAL, stop(), user_id="A")
check("позиция разметки без названия («от 429 ₽ Выбрать») — список неполон: "
      "показ модели", calls(agent, "e5", "page") == 1
      and r.endswith("Какой?"), r)
cc, agent = fresh("e6", items=None,
                  show="- Кола — от 119 ₽\n- Кола Зеро — от 119 ₽\nКакую?")
ITEMS.pop("Напитки")
r = agent.start("e6", GOAL, stop(), user_id="A")
check("разметки раздела нет — позиции из ленты снимка: показ модели (там "
      "бывает чужое)", calls(agent, "e6", "page") == 1
      and "found by the page's markup" not in agent.slot_router.shows()[0]
      and r.endswith("Какую?"), r)

print("Клик раздела перекрыт — раздел по разметке без клика")
log.msgs.clear()
cc, agent = fresh("d1", covered=True)
rt = stop()
r = agent.start("d1", GOAL, rt, user_id="A")
run = agent._runs.get("d1") or {}
hist = run.get("history") or []
check("клик «Напитки» перекрыт (окно города) — человеку список кода",
      clicked(cc) == [23] and r.endswith(CODE_LIST), (r, cc.dispatched))
check("ни показа моделью, ни хода модели шагов",
      calls(agent, "d1", "page") == 0 and not rt.prompts
      and calls(agent, "d1", "step") == 0, (agent.slot_router.prompts,
                                            rt.prompts))
check("история модели шагов: клик не выполнен (слой), раздел показан без "
      "клика, слой на месте; текста окна (город) в строке нет",
      any(h.startswith("click \"Напитки\" → NOT performed — the element is "
                       "covered by another layer") for h in hist)
      and hist[-1] == ("show the section \"Напитки\" → shown to the user "
                       "from the page's markup, without the click (the layer "
                       "over the page is still there)")
      and "Москва" not in hist[-1], hist)
check("лог: «клик перекрыт — показ по разметке без клика»; пометка listed",
      any("раздел «Напитки»: клик перекрыт — показ по разметке без клика"
          in m for m in log.msgs)
      and (run.get("awaiting") or {}).get("listed"), log.msgs)
rt2 = stop()
agent.feed("d1", "2", rt2, user_id="A")
step_p = rt2.prompts[0] if rt2.prompts else ""
check("после выбора — ход модели шагов: в истории оба шага, окно — её "
      "решение (код его не закрывал)",
      "covered by another layer" in step_p
      and "without the click (the layer over the page is still there)"
      in step_p and clicked(cc) == [23], step_p[:3000])

# Клик доставлен, а видимой реакции нет («не уверен») — тоже по разметке
cc, agent = fresh("d1b")
_orig = cc._dispatch


def _uncertain(action, router=None):
    if action.get("kind") == "click":
        cc.dispatched.append(dict(action))
        raise _ba.ClickUncertain("клик отправлен, страница не изменилась — "
                                 "не уверен, что сработало")
    return _orig(action, router)


cc._dispatch = _uncertain
r = agent.start("d1b", GOAL, stop(), user_id="A")
hist = (agent._runs.get("d1b") or {}).get("history") or []
check("клик без видимой реакции — раздел по разметке, строка без «слоя»",
      r.endswith(CODE_LIST) and any(
          "no visible change was detected" in h for h in hist)
      and hist[-1].endswith("from the page's markup, without the click"),
      (r, hist))

print("Клик не прошёл, а показа нет, как раньше — раздел отдельной "
      "страницей, разметки мало")
cc, agent = fresh("d2", pages={HOME: page(hrefs=(
    "/pizza", "/combo", "/snacks", "/drinks"))}, covered=True)
rt = stop()
r = agent.start("d2", GOAL, rt, user_id="A")
check("ссылка раздела — другая страница (блок здесь бывает витриной) — "
      "показа нет, ход модели шагов",
      "В разделе" not in r and len(rt.prompts) == 1
      and calls(agent, "d2", "page") == 0, (r, rt.prompts[:1]))
cc, agent = fresh("d3", items=["Кола — от 119 ₽"], covered=True)
rt = stop()
r = agent.start("d3", GOAL, rt, user_id="A")
check("по разметке одна позиция — как раньше, ход модели шагов",
      "В разделе" not in r and len(rt.prompts) == 1, r)
cc, agent = fresh("d4", items=many, covered=True,
                  show="В разделе 12 напитков, от 100 до 111 ₽. Какой?")
rt = stop()
r = agent.start("d4", GOAL, rt, user_id="A")
check("перекрыт, а позиций по разметке 12 — обзор модели по разметке, без "
      "хода модели шагов", calls(agent, "d4", "page") == 1
      and "found by the page's markup" in agent.slot_router.shows()[0]
      and r.endswith("Какой?") and not rt.prompts, r)

print("Подписи раздела нет в списке")
for name, hrefs, want in (
        ("d5", ("#pizza", "#combo", "#snacks", "#drinks"), True),
        ("d6", ("/pizza", "/combo", "/snacks", "/drinks"), False)):
    cc, agent = fresh(name)
    run = {"goal": GOAL, "history": [], "steps": 0, "qa": [], "lang": "ru",
           "sites": [SITE]}
    b = {"stage": "chosen", "section": "Напитки", "sections": SECTIONS,
         "kind": "напиток"}
    shown = [x for x in page(hrefs=hrefs) if x["text"] != "Напитки"]
    obs = {"host": SITE, "tab_id": 1, "url": HOME, "shown": shown}
    res = agent._browse_open(run, name, None, obs, b)
    if want:
        check("соседние разделы — якоря этой страницы: показ по разметке, "
              "без клика", res and res[0] == "pause" and res[1] == CODE_LIST
              and not clicked(cc) and run["history"][-1].endswith(
                  "from the page's markup, without the click"), (res, run))
    else:
        check("соседние разделы — другие страницы: как раньше, «NOT found» "
              "модели шагов", res == ("progress", None)
              and b.get("stage") == "done"
              and "NOT found in the list" in run["history"][-1], (res, run))

print("Ссылка на эту же страницу (_same_page_link)")
U = "https://pizzatest.test/"
check("якорь, относительный якорь, кнопка без адреса, www — та же; другой "
      "путь, запрос, сайт — нет",
      all(ta._same_page_link({"href": h}, U) for h in (
          U + "#drinks", "#drinks", "", "javascript:void(0)",
          "https://www.pizzatest.test/#a"))
      and not any(ta._same_page_link({"href": h}, U) for h in (
          U + "drinks", U + "?cat=2", "https://other.test/#a"))
      and not ta._same_page_link({"href": U}, U + "menu"))


def _browser(pw):
    try:
        return pw.chromium.launch(headless=True)
    except Exception as e:
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


def js_checks():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  SKIP: нет Playwright — JS раздела не проверен")
        return
    html = (Path(__file__).parent / "fixtures"
            / "stand_pizzatest_home.html").read_text(encoding="utf-8")
    pw = sync_playwright().start()
    br = _browser(pw)
    if br is None:
        pw.stop()
        return

    def items(pg, name):
        return json.loads(pg.evaluate(_ba._js_fill(
            _ba._SECTION_ITEMS_JS, NAME=_ba._clean_goal_text(name, 60))))
    try:
        pg = br.new_page(viewport={"width": 1280, "height": 800})
        # Страница стенда — перехватом запроса (адрес настоящий), сети нет
        pg.route("https://pizzatest.ru/**", lambda rt: rt.fulfill(
            body=html, content_type="text/html; charset=utf-8"))
        pg.goto("https://pizzatest.ru/")
        # Как на стенде: cookie закрыт, «Ваш город — Москва?» висит
        pg.evaluate("acceptCookie()")
        got = items(pg, "Пицца").get("items")
        check("JS: «Пицца» — все 9 пицц «название — цена» (раньше 4: цена "
              "срезалась на 80 знаках, длинная карточка — без названия), "
              "без бейджа «Новинка»",
              got == ["Пепперони — от 459 ₽", "Маргарита — от 399 ₽",
                      "Четыре сыра — от 519 ₽", "Ветчина и грибы — от 449 ₽",
                      "Гавайская — от 469 ₽", "Барбекю — от 489 ₽",
                      "Мясная — от 529 ₽", "Овощи и грибы — от 429 ₽",
                      "Цыплёнок ранч — от 499 ₽"], got)
        got = items(pg, "Напитки").get("items")
        check("JS: «Напитки» при открытом окне города — 6 напитков",
              got == ["Кола — от 119 ₽", "Кола Зеро — от 119 ₽",
                      "Морс клюквенный — 139 ₽", "Сок апельсиновый — 199 ₽",
                      "Вода негазированная — 79 ₽",
                      "Холодный чай лимон — 129 ₽"], got)
        _u, snap = _ba._parse_snapshot(str(pg.evaluate(_ba._js_fill(
            _ba._SNAPSHOT_JS, BASE=_ba._mark_base(_ba.SNAPSHOT_MAX))) or ""))
        city = [i for i in snap if i["text"] in ("Да", "Выбрать другой")]
        check("снимок: кнопки окна города — не md (absolute-пузырь без "
              "role=dialog и «popup» в классе), ссылка «Напитки» — якорь",
              len(city) == 2 and not any(i["md"] for i in city)
              and any(i["text"] == "Напитки"
                      and i["href"] == "https://pizzatest.ru/#drinks"
                      for i in snap), city)
        cover = pg.locator("nav.cats a", has_text="Напитки").first.evaluate(
            _ba._COVER_FN)
        free = pg.locator("nav.cats a", has_text="Пицца").first.evaluate(
            _ba._COVER_FN)
        check("центр «Напитки» перекрыт окном города (клик откажет), "
              "«Пицца» — нет", bool(cover) and not free, (cover, free))
        # Синтетика: две цены, обёртка, подпись картинки, EN, «от» в слове
        card = ("<article style='cursor:pointer'>{0}<p>{1}</p>"
                "<button>Выбрать</button></article>")
        pg.set_content(
            "<main><section><h2>Пиццы</h2>"
            + card.format("<h3>Пепперони</h3>", "Острая 599 ₽ 459 ₽")
            + card.format("<h3>Сырная</h3>", "Сыр от 399 ₽")
            + "<div style='cursor:pointer'><h3>Хиты</h3>"
            + card.format("<h4>Мясная</h4>", "от 529 ₽")
            + card.format("<h4>Гавайская</h4>", "от 469 ₽") + "</div>"
            + card.format("<img alt='Чизкейк'><div>Чизкейк</div>", "189 ₽")
            + card.format("<h3>Торт</h3>", "Кот 250 ₽")
            + "</section><section><h2>Drinks</h2>"
            + card.format("<h3>Cola</h3>", "Classic from $1.19")
            + card.format("<h3>Lemonade</h3>", "$2.49")
            + "</section></main>")
        got = items(pg, "Пиццы").get("items")
        check("JS: две цены — текст как есть; обёртка с двумя заголовками — "
              "не позиция (карточки внутри — свои); подпись картинки; «от» "
              "внутри «Кот» — не предлог",
              got == ["Пепперони Острая 599 ₽ 459 ₽ Выбрать",
                      "Сырная — от 399 ₽", "Мясная — от 529 ₽",
                      "Гавайская — от 469 ₽", "Чизкейк — 189 ₽",
                      "Торт — 250 ₽"], got)
        got = items(pg, "Drinks").get("items")
        check("JS: EN — «Cola — from $1.19», «Lemonade — $2.49»",
              got == ["Cola — from $1.19", "Lemonade — $2.49"], got)
    finally:
        br.close()
        pw.stop()


print("_SECTION_ITEMS_JS на настоящем движке")
js_checks()

print(f"\nИтого: {OKS} проверок, {FAILS} провалов")
sys.exit(1 if FAILS else 0)

"""Тест разделов магазина, когда задача началась на внутренней странице
того же сайта (_browse_sections / _service_page / _browse_later /
_browse_home): одна попытка при первом приходе больше не ставит крест.

Run3 21.10, сценарий 3: вкладка осталась на /delivery от прошлой задачи,
«закажи напиток в Пицца Тест» — разметка там пуста, модель о разделах
ответила NONE (№28), stage=done навсегда; дальше модель шагов кликнула
логотип (№29) и сама перечислила напитки (№30). Проверяет:
* старт на служебной странице того же сайта (доставка, без товаров) —
  модель о разделах не спрашивали, главная открыта кодом (тот же хост,
  «/»), прогресс человеку понятный; на главной — разделы по разметке, без
  вызова модели и без хода модели шагов;
* вид назван в цели («напиток») — раздел «Напитки» выбран, открыт кликом и
  показан кодом (позиции раздела по разметке целиком — список кодом, без
  вызова «по странице»), без вопроса о разделах;
  то же, когда сайт назван названием («в Пицца Тест»), а вкладка уже на нём;
* вкладка человека на чужом сайте (служебный путь, сайт назван другим) или
  сайт не выбран — ни разделов, ни разметки, ни главной кодом;
  приватная страница (оформление) — тоже;
* повторов не больше SECTIONS_RETRY_MAX: та же страница — без новой попытки
  и без чтения разметки, другая служебная — следующая, за лимитом — stage
  done без вызова модели; главная кодом — один раз за выбор;
* модель о разделах зовётся, только когда разметка не годится: на главной
  без разметки (после перехода — один вызов, на главной, а не на /delivery),
  на странице с товарами (категория; «служебный» путь с ценами) — как
  раньше, без перехода на главную;
* _service_page: путь служебный словом (латиница, транслит, кириллица в
  адресе) и ни одной позиции с ценой; «/menu», «/catalog/drinks»,
  «/apple-juice», главная — нет.

Браузер — real_cc (test_task_agent): настоящий ComputerControlManager,
заглушены только браузер и снимок; модель — скрипт. Живых браузеров,
веб-чатов и LLM нет.

Запуск: python3 -m scripts.test_task_sections_retry
"""

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.features import browser_actions as _ba  # noqa: E402
from app.features import cc_privacy as _cp  # noqa: E402
from app.features import cc_texts  # noqa: E402
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

    def __init__(self, sections="NONE", show="", slots=()):
        self.sections, self.show = sections, show
        self.slots, self.prompts = list(slots), []

    def get_response(self, messages, **kw):
        p = messages[-1]["content"]
        self.prompts.append(p)
        if self.SEC in p:
            return self.sections
        if "Present it:" in p:
            return self.show
        return json.dumps(self.slots.pop(0) if self.slots else {},
                          ensure_ascii=False)

    def sec_prompts(self):
        return [p for p in self.prompts if self.SEC in p]


# Браузера в тестах нет: разметка — по адресу текущей страницы фейка
CUR = []        # [cc] — чья страница сейчас открыта
HEADS = {}      # адрес → заголовки разделов по разметке
ITEMS = {}      # раздел → позиции по разметке
NAMES_CALLS = []


def _names(host, tab_id=None):
    url = CUR[0].cur if CUR else None
    NAMES_CALLS.append(url)
    return HEADS.get(url)


_ba.section_names = _names
_ba.section_items = lambda host, sec, tab_id=None: ITEMS.get(sec)
_ba.wait_dom_idle = lambda *a, **k: None
_ba.scroll_step = lambda *a, **k: {"moved": True, "bottom": False}
_ba.scroll_container_step = lambda *a, **k: {"moved": True, "bottom": False,
                                             "y0": 0}
_ba.snapshot_for_goal = lambda host, text, tab_id=None: (None, [])
# Приватная страница не зовёт живую локальную модель (Ollama)
_cp.PrivateRouter.get_response = lambda self, msgs, **kw: None

TMP = Path(tempfile.mkdtemp(prefix="task_sec_retry_"))
SITE = "pizzatest.test"
HOME = f"https://{SITE}/"
DELIV, CONTACTS, ABOUT = HOME + "delivery", HOME + "contacts", HOME + "o-nas"
DRINKS = HOME + "#drinks"
NAV = "Пицца Комбо Закуски Напитки Акции Контакты"
FOOT = "О нас Акции Вакансии Доставка и оплата Контакты"


def link(idx, text, ctx="", **kw):
    return {"idx": idx, "tag": "a", "role": "link", "text": text,
            "ctx": ctx, "vp": True, **kw}


# Шапка и подвал любой страницы сайта (как /delivery стенда run3: каталога
# в шапке нет — только логотип, вход, корзина и служебные ссылки)
head = [link(1, "Пицца Тест Доставка пиццы за 35 минут", "Пицца Тест"),
        {"idx": 2, "tag": "button", "role": "button", "text": "Войти",
         "vp": True},
        {"idx": 3, "tag": "button", "role": "button", "text": "Корзина 2",
         "vp": True}]
foot = [link(40 + i, x, FOOT) for i, x in enumerate(
    ["О нас", "Акции", "Вакансии", "Доставка и оплата", "Контакты"])]
menu = [link(20 + i, x, NAV) for i, x in enumerate(
    ["Пицца", "Комбо", "Закуски", "Напитки"])]
drinks = [link(30 + i, f"{n} {p} ₽ Выбрать", f"{n} {p} ₽ Выбрать")
          for i, (n, p) in enumerate([("Кола 0,5 л", 119),
                                      ("Морс клюквенный 0,5 л", 139),
                                      ("Вода 0,5 л", 79)])]
pizzas = [link(50 + i, f"{n} от {p} ₽", f"{n} сыр, соус от {p} ₽ Выбрать")
          for i, (n, p) in enumerate([("Пепперони", 459), ("Сырная", 399)])]
PAGES = {HOME: head + menu + pizzas + drinks + foot,
         DRINKS: head + menu + pizzas + drinks + foot,
         DELIV: head + foot, CONTACTS: head + foot, ABOUT: head + foot,
         HOME + "pizzas": head + pizzas + foot,
         HOME + "promo": head + [link(60, "Комбо Дуэт 999 ₽", "Комбо"),
                                 link(61, "Комбо Трио 1 299 ₽", "Комбо")]
         + foot,
         HOME + "checkout": head + [
             {"idx": 70, "tag": "input", "role": "textbox", "text": "Адрес",
              "ed": True, "vp": True}],
         "https://news.test/about": [link(1, "Новости"), link(2, "О проекте"),
                                     link(3, "Контакты")]}
SECTIONS = ["Пицца", "Комбо", "Закуски", "Напитки"]
SHOW = ("- Кола 0,5 л — 119 ₽\n- Морс клюквенный 0,5 л — 139 ₽\n"
        "- Вода 0,5 л — 79 ₽\nКакой напиток заказываем?")


def fresh(name, url, slots=(), sections="NONE", show=SHOW, heads=None):
    HEADS.clear()
    HEADS.update({HOME: SECTIONS} if heads is None else heads)
    ITEMS.clear()
    ITEMS["Напитки"] = [x["text"] for x in drinks]
    NAMES_CALLS.clear()
    cc = real_cc(PAGES, url=url, clicks={23: DRINKS})
    CUR[:] = [cc]
    agent = TaskAgent(cc, memory_path=TMP / f"{name}.json")
    agent.slot_router = SecRouter(sections, show, slots)
    return cc, agent


def calls(agent, chat, kind):
    # Вызовы модели этого прогона по виду (run["traffic"])
    run = agent._runs.get(chat) or {}
    return (run.get("traffic") or {}).get(kind, [0])[0]


def opened(cc):
    return [d.get("value") for d in cc.dispatched if d.get("kind") == "url"]


def clicked(cc):
    return [d.get("idx") for d in cc.dispatched if d.get("kind") == "click"]


def stop():
    return ScriptedRouter([{"action": "fail", "message": "стоп"}])


HOME_LINE = cc_texts.t("task_sections_home", site=SITE)

print("Старт на служебной странице того же сайта — главная кодом, разделы "
      "по разметке")
cc, agent = fresh("s1", DELIV)
rt = stop()
r = agent.start("s1", f"закажи поесть на {SITE}", rt, user_id="A")
run = agent._runs.get("s1") or {}
b = run.get("browse") or {}
check("главная открыта кодом (тот же хост, «/»), прогресс человеку — "
      "«Здесь товаров нет — открыл главную …»",
      opened(cc) == [HOME] and HOME_LINE in r
      and HOME_LINE == f"Здесь товаров нет — открыл главную {SITE}.",
      (r, opened(cc)))
check("на главной — вопрос о разделах по разметке",
      f"На {SITE} есть разделы:\n- Пицца\n- Комбо\n- Закуски\n- Напитки\n"
      in r and b.get("stage") == "asked", (r, b))
check("модель о разделах не звалась ни на /delivery, ни на главной; модель "
      "шагов тоже; вызовов «по странице» нет",
      not agent.slot_router.sec_prompts() and not rt.prompts
      and calls(agent, "s1", "page") == 0 and calls(agent, "s1", "step") == 0,
      (agent.slot_router.prompts, rt.prompts))
check("история для модели: переход сделал код, «→ ok»; разметку прочли на "
      "/delivery и на главной — по разу",
      any(h.startswith(f"open the home page \"{HOME}\"") and " → ok" in h
          for h in run.get("history") or ())
      and NAMES_CALLS == [DELIV, HOME], (run.get("history"), NAMES_CALLS))
check("главная — сайт задачи (пришли действием агента)",
      SITE in (run.get("sites") or ()), run.get("sites"))
check("фраза на английском",
      cc_texts.t("task_sections_home", "en", site=SITE)
      == f"No products on this page — opened the {SITE} home page.")

print("Вид назван в цели — раздел открыт и показан кодом")
cc, agent = fresh("k1", DELIV, slots=[{"kinds": ["напиток"]}])
rt = stop()
r = agent.start("k1", f"закажи напиток на {SITE}", rt, user_id="A")
run = agent._runs.get("k1") or {}
b = run.get("browse") or {}
aw = run.get("awaiting") or {}
# Позиции раздела по разметке целиком (3, с названием и ценой) — список
# кодом, кнопка карточки «Выбрать» — мимо (run4 №5: показ моделью — лишний)
check("главная кодом → «Напитки» нажаты кодом → показ раздела списком кода",
      opened(cc) == [HOME] and clicked(cc) == [23]
      and r.endswith("В разделе «Напитки»:\n- Кола 0,5 л 119 ₽\n- Морс "
                     "клюквенный 0,5 л 139 ₽\n- Вода 0,5 л 79 ₽\nЧто "
                     "заказываем?"), (r, cc.dispatched))
check("вопроса о разделах не было; вид «напиток» → раздел «Напитки»",
      "есть разделы" not in r and b.get("kind") == "напиток"
      and b.get("section") == "Напитки" and b.get("stage") == "presented",
      b)
check("ни одного вызова «по странице» (ни показа, ни разделов), модели "
      "шагов не было",
      calls(agent, "k1", "page") == 0 and not agent.slot_router.sec_prompts()
      and not rt.prompts, (agent.slot_router.prompts, rt.prompts))
check("варианты показа — товары раздела (pick=item, listed: голый выбор — "
      "без модели)", aw.get("pick") == "item" and aw.get("browse")
      and aw.get("listed") and len(aw.get("items") or ()) == 3, aw)
# Сайт назван названием, вкладка уже на нём (сценарий 3 run3)
cc, agent = fresh("k2", DELIV,
                  slots=[{"site": "Пицца Тест", "kinds": ["напиток"]}])
rt = stop()
r = agent.start("k2", "закажи напиток в Пицца Тест", rt, user_id="A")
run = agent._runs.get("k2") or {}
check("«в Пицца Тест» (названием), вкладка на /delivery этого сайта — "
      "сайт сверен, главная и раздел кодом, без модели шагов",
      agent._site_choice(run, SITE) == "ok" and opened(cc) == [HOME]
      and clicked(cc) == [23] and r.endswith("- Вода 0,5 л 79 ₽\nЧто "
                                              "заказываем?")
      and not agent.slot_router.sec_prompts() and not rt.prompts,
      (r, cc.dispatched))
# Вид из цели, а на главной своего раздела нет — вопрос о разделах
cc, agent = fresh("k3", DELIV, slots=[{"kinds": ["суши"]}])
r = agent.start("k3", f"закажи суши на {SITE}", stop(), user_id="A")
check("вид «суши» без своего раздела — вопрос о разделах, как раньше",
      f"На {SITE} есть разделы:" in r and clicked(cc) == []
      and not agent.slot_router.sec_prompts(), r)
# Товар назван — разделы ни к чему, главную не открываем
cc, agent = fresh("k4", DELIV, slots=[{"items": [{"name": "Кола 0,5 л"}]}])
rt = stop()
r = agent.start("k4", f"закажи колу 0,5 л на {SITE}", rt, user_id="A")
check("товар назван — ни главной кодом, ни разметки, ход модели шагов",
      opened(cc) == [] and not NAMES_CALLS and len(rt.prompts) == 1, r)

print("Чужой сайт человека, сайт не выбран, приватная страница — без "
      "разделов и без главной")
for name, url, goal, why in (
        ("f1", "https://news.test/about", "закажи напиток в Пицца Тест",
         "вкладка новостей человека (служебный путь), сайт назван другим"),
        ("f2", "https://news.test/about", "закажи напиток",
         "сайт не выбран"),
        ("f3", HOME + "checkout", f"закажи напиток на {SITE}",
         "приватная страница (оформление) того же сайта")):
    cc, agent = fresh(name, url, slots=[
        {"site": "Пицца Тест", "kinds": ["напиток"]} if "Пицца Тест" in goal
        else {"kinds": ["напиток"]}])
    r = agent.start(name, goal, stop(), user_id="A")
    check(f"{why} — разметку не читали, главную не открывали, разделов нет",
          not NAMES_CALLS and opened(cc) == [] and HOME_LINE not in r
          and "есть разделы" not in r
          and not agent.slot_router.sec_prompts(), (r, NAMES_CALLS,
                                                    cc.dispatched))

print("Повторов не больше лимита")
cc, agent = fresh("l1", DELIV)
run = {"goal": f"закажи поесть на {SITE}", "history": [], "steps": 0,
       "qa": [], "lang": "ru", "sites": [SITE]}
b = {"stage": None}


def obs_of(url):
    cc.cur = url
    return {"host": SITE, "tab_id": 1, "url": url,
            "shown": [dict(x) for x in PAGES[url]]}


res = agent._browse_sections(run, "l1", obs_of(DELIV), b, SITE)
check("без роутера (главную открыть нечем) — попытка отложена: stage None, "
      "1 из лимита", res is None and b.get("stage") is None
      and b.get("tries") == 1 and b.get("retry_url") == DELIV, b)
n_names = len(NAMES_CALLS)
res = agent._browse_sections(run, "l1", obs_of(DELIV), b, SITE)
check("та же страница снова — без попытки и без чтения разметки",
      res is None and b.get("tries") == 1 and len(NAMES_CALLS) == n_names, b)
res = agent._browse_sections(run, "l1", obs_of(CONTACTS), b, SITE)
check("другая служебная страница — вторая попытка",
      res is None and b.get("stage") is None and b.get("tries") == 2, b)
res = agent._browse_sections(run, "l1", obs_of(ABOUT), b, SITE)
check(f"за лимитом ({ta.SECTIONS_RETRY_MAX}) — stage done, как после NONE, "
      "без вызова модели",
      res is None and b.get("stage") == "done" and b.get("tries") == 2
      and not agent.slot_router.sec_prompts(), b)
res = agent._browse_sections(run, "l1", obs_of(HOME), {"stage": None,
                                                       "tries": 1}, SITE)
check("после отложенной попытки — на главной разделы по разметке",
      res and res[0] == "pause" and "есть разделы" in res[1], res)
# Главная кодом — один раз за выбор: вторая служебная страница — только
# отложить
b = {"stage": None}
res = agent._browse_sections(run, "l1", obs_of(DELIV), b, SITE,
                             router=stop())
check("с роутером — главная кодом (раз)", opened(cc) == [HOME]
      and b.get("home") and res and res[0] == "progress", (res, b))
res = agent._browse_sections(run, "l1", obs_of(CONTACTS), b, SITE,
                             router=stop())
check("вторая служебная страница — главную снова не открываем, отложено",
      opened(cc) == [HOME] and res is None and b.get("stage") is None
      and b.get("tries") == 2, (res, b))
check("за всё время модель о разделах не звалась",
      not agent.slot_router.sec_prompts())

print("Модель о разделах — только когда разметка не годится")
cc, agent = fresh("m1", DELIV, heads={}, sections="- Пицца\n- Напитки")
r = agent.start("m1", f"закажи поесть на {SITE}", stop(), user_id="A")
sp = agent.slot_router.sec_prompts()
check("главная без разметки — модель позвана один раз, на главной (не на "
      "/delivery)", opened(cc) == [HOME] and len(sp) == 1
      and f"The page {HOME} —" in sp[0] and "/delivery" not in sp[0]
      and f"На {SITE} есть разделы:\n- Пицца\n- Напитки\n" in r, (r, sp))
for name, url, why in (("m2", HOME + "pizzas", "категория с товарами"),
                       ("m3", HOME + "promo", "«служебный» путь, но с "
                                              "ценами")):
    cc, agent = fresh(name, url, heads={}, sections="NONE")
    r = agent.start(name, f"закажи поесть на {SITE}", stop(), user_id="A")
    sp = agent.slot_router.sec_prompts()
    # NONE модели — показ самой страницы тем же правилом (как раньше)
    check(f"{why} — как раньше: модель о разделах на этой странице, без "
          "перехода на главную; NONE — показ страницы",
          opened(cc) == [] and len(sp) == 1 and f"The page {url} —" in sp[0]
          and f"На {SITE}:\n- " in r and r.endswith("Что заказываем?"),
          (r, sp))

print("_service_page")
blank = [link(1, "О нас"), link(2, "Корзина 459 ₽"),
         link(3, "Доставка 149 ₽")]
for url, want in (("/delivery", True), ("/info/delivery-terms", True),
                  ("/o-nas", True), ("/moscow/contacts", True),
                  ("/%D0%B4%D0%BE%D1%81%D1%82%D0%B0%D0%B2%D0%BA%D0%B0", True),
                  ("/", False), ("", False), ("/menu", False),
                  ("/catalog/drinks", False), ("/apple-juice", False),
                  ("/helpful", False)):
    got = TaskAgent._service_page({"url": "https://shop.test" + url,
                                   "shown": blank})
    check(f"«{url or '(пусто)'}» — {'служебная' if want else 'нет'} "
          "(корзина и доставка с ценой — не товар)", got == want, got)
check("служебный путь, но позиция с ценой — не служебная",
      not TaskAgent._service_page({"url": "https://shop.test/promo",
                                   "shown": [link(1, "Комбо Дуэт 999 ₽")]}))
check("путь сравнивается словом: «/news» — да, «/newsletter-pizza» — нет",
      ta._SERVICE_PATH_RE.search("/news")
      and not ta._SERVICE_PATH_RE.search("/newsletter-pizza")
      and not re.search(ta._SERVICE_PATH_RE.pattern, "/applepie",
                        re.IGNORECASE))

print(f"\nИтого: {OKS} проверок, {FAILS} провалов")
sys.exit(1 if FAILS else 0)

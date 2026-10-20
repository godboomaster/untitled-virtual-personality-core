"""Тест разделов магазина агента задач: сначала разметка, модель — запасом.

Проверяет (_browse_sections / _markup_sections):
* разметка дала ≥2 раздела с подписью на странице — модель разделов не
  звалась (минус вызов веб-чата на заход в магазин), вопрос — с этими
  разделами, дальше как раньше: ответ открывает раздел, показ, лог «по
  разметке»; больше 10 — первые 10; вызов модели падает — вопрос всё
  равно; модели разбора нет вовсе — как раньше, разметку не читают;
* «как в прошлый раз» — подсказка к вопросу; вид назван («напиток») —
  раздел выбран без вопроса;
* разметка пуста/мусор — модель позвана, как раньше: служебные и акции
  (вход, корзина, контакты, акции, приложение), заголовков нет на
  странице, подписи каждая в своей карточке (товары), список неполон
  (соседей по панели больше найденных); один раздел по разметке — к
  названному моделью;
* цели каталог не нужен («оформи корзину как есть», «оформи заказ как
  есть», «order it as is») — ни разметки, ни модели разделов, дальше
  модель шагов; «доставь» не сходит за «оставь»;
* _SECTION_NAMES_JS на настоящем движке (headless-Chromium Playwright,
  set_content — без сети): заголовок без ровни (h1 товара, одинокий h4
  подвала) — не раздел. Нет Playwright/Chromium — SKIP этой части.

Запуск: python3 -m scripts.test_task_sections_markup
"""

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
    # slot_router: разделы / показ раздела — текст, разбор ответа — JSON
    # (как BRouter в test_task_agent)
    SEC = "sections of the site's catalog"

    def __init__(self, sections, show="x", slots=()):
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

    def sec_calls(self):
        return sum(self.SEC in p for p in self.prompts)


class _Log(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


# Браузера в тестах нет: разметка — подмена, позиции раздела — из снимка
_ba.wait_dom_idle = lambda *a, **k: None
_ba.section_items = lambda *a, **k: None
_ba.scroll_step = lambda *a, **k: {"moved": True, "bottom": False}
_ba.scroll_container_step = lambda *a, **k: {"moved": True, "bottom": False,
                                             "y0": 0}
_ba.snapshot_for_goal = lambda host, text, tab_id=None: (PIZ, [])
# Приватная страница не зовёт живую локальную модель (Ollama)
_cp.PrivateRouter.get_response = lambda self, msgs, **kw: None

HEADS = []      # что вернёт разметка (None — «не разобрать»)
NAMES_CALLS = []


def _names(*a, **k):
    NAMES_CALLS.append(a)
    return HEADS[0] if HEADS else None


_ba.section_names = _names


def set_heads(x):
    HEADS[:] = [x]
    NAMES_CALLS.clear()


log = _Log()
_tl = logging.getLogger(ta.__name__)
_tl.addHandler(log)
_tl.setLevel(logging.INFO)

TMP = Path(tempfile.mkdtemp(prefix="task_sec_mk_"))
HOME, PIZ = "https://pizza.test/", "https://pizza.test/pizzas"
NAV_CTX = "Пиццы Комбо Закуски Напитки Войти Корзина"


def link(idx, text, ctx="", **kw):
    return {"idx": idx, "tag": "a", "role": "link", "text": text,
            "ctx": ctx, "vp": True, **kw}


nav = [link(1, "Пиццы", NAV_CTX), link(2, "Комбо", NAV_CTX),
       link(3, "Закуски", NAV_CTX), link(4, "Напитки", NAV_CTX),
       {"idx": 5, "tag": "button", "role": "button", "text": "Войти",
        "ctx": NAV_CTX, "vp": True},
       {"idx": 7, "tag": "button", "role": "button", "text": "0 ₽",
        "aria": "Корзина", "ctx": NAV_CTX, "vp": True}]
pizzas = [link(10 + i, f"{n} от {p} ₽", f"{n} сыр, соус от {p} ₽ Выбрать")
          for i, (n, p) in enumerate([("Пепперони", 349), ("Сырная", 279),
                                      ("Терияки", 359)])]
SHOW = ("- Пепперони — от 349 ₽\n- Сырная — от 279 ₽\n- Терияки — от 359 ₽"
        "\nКакую заказываем?")


def fresh(name, pages=None, sections="NONE", show=SHOW, slots=(),
          clicks=None, mem=None):
    cc = real_cc(pages or {HOME: nav, PIZ: nav + pizzas}, url=HOME,
                 clicks={1: PIZ} if clicks is None else clicks)
    path = TMP / f"{name}.json"
    if mem is not None:
        path.write_text(json.dumps(mem, ensure_ascii=False), encoding="utf-8")
    agent = TaskAgent(cc, memory_path=path)
    agent.slot_router = SecRouter(sections, show, slots)
    return cc, agent


def page_calls(agent, chat):
    # Вызовы модели «по странице» (_pre_llm) этого прогона
    run = agent._runs.get(chat) or {}
    return (run.get("traffic") or {}).get("page", [0])[0]


def stop():
    return ScriptedRouter([{"action": "fail", "message": "стоп"}])


print("Разметка дала ≥2 раздела — модель разделов не зовётся")
set_heads(["Пиццы", "Комбо", "Закуски", "Часто заказывают"])
log.msgs.clear()
cc, agent = fresh("m1")
rt = stop()
r = agent.start("m1", "закажи пиццу на pizza.test", rt, user_id="A")
run = agent._runs.get("m1") or {}
b = run.get("browse") or {}
check("вопрос с разделами разметки (с подписью на странице; «Часто "
      "заказывают» — заголовок без ссылки — мимо)",
      "На pizza.test есть разделы:\n- Пиццы\n- Комбо\n- Закуски\nЧто "
      "посмотрим?" in r and "Часто" not in r, r)
check("модель разделов не звалась, модель шагов тоже; разметку спросили "
      "один раз", agent.slot_router.sec_calls() == 0 and not rt.prompts
      and page_calls(agent, "m1") == 0 and len(NAMES_CALLS) == 1,
      (agent.slot_router.prompts, rt.prompts, NAMES_CALLS))
check("стадия «asked», разделы и вопрос — в browse",
      b.get("stage") == "asked"
      and b.get("sections") == ["Пиццы", "Комбо", "Закуски"]
      and b.get("sections_q") and b["sections_q"] in r, b)
check("лог: откуда разделы — «по разметке»",
      any("разделы по разметке: 3" in m for m in log.msgs), log.msgs)
r = agent.feed("m1", "пиццы", rt, user_id="A")
check("ответ «пиццы» — раздел открыт кликом, показ от модели, как раньше",
      [d.get("idx") for d in cc.dispatched if d.get("kind") == "click"]
      == [1] and r.endswith("Какую заказываем?")
      and "- Сырная — от 279 ₽" in r and not rt.prompts, r)
check("за весь заход — один вызов «по странице» (показ раздела), без "
      "вызова о разделах", page_calls(agent, "m1") == 1
      and agent.slot_router.sec_calls() == 0)

# Больше 10 разделов — первые 10, как было
many = [f"Раздел{chr(0x430 + i)}" for i in range(12)]
mctx = " ".join(many)
set_heads(many)
cc, agent = fresh("m2", pages={HOME: [link(i + 1, x, mctx)
                                      for i, x in enumerate(many)]})
r = agent.start("m2", "закажи поесть на pizza.test", stop(), user_id="A")
check("12 разделов по разметке — в вопросе первые 10, модель не звалась",
      r.count("\n- ") == 10 and f"- {many[9]}\n" in r and many[10] not in r
      and agent.slot_router.sec_calls() == 0, r)

# Веб-чат модели недоступен (вызов падает) — разделы по разметке всё равно
class DownRouter(SecRouter):
    def get_response(self, messages, **kw):
        self.prompts.append(messages[-1]["content"])
        raise RuntimeError("webchat down")


set_heads(["Пиццы", "Комбо"])
cc, agent = fresh("m3")
agent.slot_router = DownRouter("NONE")
r = agent.start("m3", "закажи пиццу на pizza.test", stop(), user_id="A")
check("вызов модели падает — вопрос о разделах по разметке, модель о "
      "разделах не спрашивали",
      "На pizza.test есть разделы:\n- Пиццы\n- Комбо\n" in r
      and agent.slot_router.sec_calls() == 0, r)
# Модели разбора нет вовсе (тесты без slot_router) — как раньше: разметку
# не читают (браузер не трогают), решает модель шагов
set_heads(["Пиццы", "Комбо"])
cc, agent = fresh("m3b")
agent.slot_router = None
rt = stop()
r = agent.start("m3b", "закажи пиццу на pizza.test", rt, user_id="A")
check("модели разбора нет — без разделов и без чтения разметки, как раньше",
      "есть разделы" not in r and not NAMES_CALLS and len(rt.prompts) == 1,
      (r, NAMES_CALLS))

# Прошлый заказ той же темы — подсказка; вид назван — раздел без вопроса
set_heads(["Пиццы", "Комбо", "Напитки"])
cc, agent = fresh("m4", mem={"m4": [
    {"ts": 1, "goal": "закажи пиццу", "sites": ["pizza.test"], "qa": [],
     "result": "Дошёл до оплаты", "ok": True,
     "brief": {"site": "pizza.test",
               "items": [{"name": "Пепперони", "size": "30 см"}]}}]})
r = agent.start("m4", "закажи пиццу на pizza.test", stop(), user_id="A")
check("прошлый заказ — «В прошлый раз: Пепперони 30 см.» к вопросу",
      "- Напитки\nЧто посмотрим?" in r
      and "В прошлый раз: Пепперони 30 см." in r
      and agent.slot_router.sec_calls() == 0, r)
run = agent._runs["m4"]
b = {"stage": None, "again": True, "kind": "напиток"}
obs = {"host": "pizza.test", "tab_id": 1, "url": HOME, "shown": nav}
res = agent._browse_sections(run, "m4", obs, b, "pizza.test")
check("вид «напиток» — раздел «Напитки» выбран без вопроса (stage chosen)",
      res == ("progress", None) and b.get("stage") == "chosen"
      and b.get("section") == "Напитки"
      and b.get("sections") == ["Пиццы", "Комбо", "Напитки"]
      and agent.slot_router.sec_calls() == 0, (res, b))

print("Разметка пуста или мусор — модель, как раньше")
for heads, why in ((None, "разметку не разобрать"),
                   ([], "заголовков нет"),
                   (["Пиццы", "Акции", "Контакты", "Войти", "Корзина",
                     "Скачать приложение"],
                    "служебные и акции — мимо, остался один"),
                   (["Римские пиццы", "Десерты"], "подписей нет на "
                                                  "странице")):
    set_heads(heads)
    log.msgs.clear()
    extra = [link(30, "Акции", NAV_CTX), link(31, "Контакты", NAV_CTX),
             link(32, "Скачать приложение", NAV_CTX)]
    cc, agent = fresh("e1", pages={HOME: nav + extra, PIZ: nav + pizzas},
                      sections="- Пиццы\n- Комбо\n- Напитки")
    r = agent.start("e1", "закажи пиццу на pizza.test", stop(), user_id="A")
    check(f"{why} — модель позвана один раз, вопрос с её разделами, лог "
          "«от модели»",
          agent.slot_router.sec_calls() == 1
          and "есть разделы:\n- Пиццы\n- Комбо\n- Напитки\n" in r
          and page_calls(agent, "e1") == 1
          and any("разделы от модели: 3" in m for m in log.msgs),
          (r, log.msgs))

check("служебные ссылки — словом: «Контакты», «Скачать приложение», "
      "«Вакансии», «Contact us», «Jobs» — да; «Контактные линзы», "
      "«Appetizers», «Contact lenses» — разделы",
      all(ta._SERVICE_LINK_RE.search(x) for x in (
          "Контакты", "Скачать приложение", "Вакансии", "Contact us", "Jobs"))
      and not any(ta._SERVICE_LINK_RE.search(x) for x in (
          "Контактные линзы", "Appetizers", "Contact lenses")))
# Подписи каждая в своём блоке — карточки товаров, не панель разделов
set_heads(["Пепперони", "Сырная"])
cards = [link(40, "Пепперони", "Пепперони сыр, соус от 349 ₽ Выбрать"),
         link(41, "Сырная", "Сырная сыр, соус от 279 ₽ Выбрать")]
cc, agent = fresh("e2", pages={HOME: nav + cards, PIZ: nav + pizzas},
                  sections="- Пиццы\n- Комбо")
r = agent.start("e2", "закажи пиццу на pizza.test", stop(), user_id="A")
check("заголовки — подписи товаров в своих карточках — не разделы: "
      "модель", agent.slot_router.sec_calls() == 1
      and "- Пиццы\n- Комбо\n" in r and "Пепперони" not in r, r)
# Та же панель плюс товар со своим ctx — товар отброшен, модели нет
set_heads(["Пиццы", "Пепперони", "Комбо", "Закуски", "Напитки"])
cc, agent = fresh("e3", pages={HOME: nav + cards, PIZ: nav + pizzas})
r = agent.start("e3", "закажи пиццу на pizza.test", stop(), user_id="A")
check("панель меню + товар в карточке — разделы панели, товар мимо, "
      "модель не звалась",
      "есть разделы:\n- Пиццы\n- Комбо\n- Закуски\n- Напитки\n" in r
      and "Пепперони" not in r and agent.slot_router.sec_calls() == 0, r)
# Список неполон: в меню 7 разделов, блоки с ценами — у двух («Новинки»,
# «Хиты» на главной; каталог — по страницам)
menu = ["Новинки", "Хиты", "Пиццы", "Роллы", "Суши", "Сеты", "Напитки"]
mctx = " ".join(menu) + " Войти"
set_heads(["Новинки", "Хиты"])
cc, agent = fresh("e4", pages={HOME: [link(50 + i, x, mctx)
                                      for i, x in enumerate(menu)]},
                  sections="- Пиццы\n- Роллы\n- Суши")
r = agent.start("e4", "закажи поесть на pizza.test", stop(), user_id="A")
check("по разметке 2 из 7 подписей меню — список неполон, модель",
      agent.slot_router.sec_calls() == 1 and "- Пиццы\n- Роллы\n- Суши\n" in r
      and "Новинки" not in r, r)
# Найдено больше, чем соседей без блока, — разметка годится
set_heads(["Пиццы", "Роллы", "Суши", "Сеты"])
cc, agent = fresh("e5", pages={HOME: [link(50 + i, x, mctx)
                                      for i, x in enumerate(menu)]})
r = agent.start("e5", "закажи поесть на pizza.test", stop(), user_id="A")
check("по разметке 4 из 7 (соседей без блока 3) — разметка, без модели",
      "есть разделы:\n- Пиццы\n- Роллы\n- Суши\n- Сеты\n" in r
      and agent.slot_router.sec_calls() == 0, r)
# Один раздел по разметке, модель — NONE: этот один к названному моделью;
# меньше двух — показ страницы (_browse_show)
set_heads(["Пиццы"])
log.msgs.clear()
cc, agent = fresh("e6", pages={HOME: nav + pizzas}, sections="NONE")
r = agent.start("e6", "закажи пиццу на pizza.test", stop(), user_id="A")
b = (agent._runs.get("e6") or {}).get("browse") or {}
check("один по разметке + NONE модели — раздел один: показ страницы тем же "
      "правилом, лог «от модели и по разметке: 1»",
      agent.slot_router.sec_calls() == 1 and r.endswith("Какую заказываем?")
      and b.get("stage") == "presented"
      and any("разделы от модели и по разметке: 1" in m for m in log.msgs),
      (r, b, log.msgs))
# Разметка пуста, вызов модели падает — как раньше: решает модель шагов
set_heads(None)
cc, agent = fresh("e7")
agent.slot_router = DownRouter("NONE")
rt = stop()
r = agent.start("e7", "закажи пиццу на pizza.test", rt, user_id="A")
check("разметка пуста, вызов модели упал — без вопроса, модель шагов",
      "есть разделы" not in r and agent.slot_router.sec_calls() == 1
      and len(rt.prompts) == 1, r)

print("Цели каталог не нужен — ни разметки, ни модели разделов")
for goal in ("оформи корзину как есть на pizza.test",
             "оформи заказ как есть на pizza.test",
             "order it as is on pizza.test"):
    set_heads(["Пиццы", "Комбо"])
    cc, agent = fresh("n1")
    rt = stop()
    r = agent.start("n1", goal, rt, user_id="A")
    check(f"«{goal}» — без вопроса о разделах, дальше модель шагов",
          "есть разделы" not in r and not NAMES_CALLS
          and agent.slot_router.sec_calls() == 0 and len(rt.prompts) == 1,
          (r, NAMES_CALLS))
# Та же цель без «как есть» — разделы есть: отсекла именно проверка цели
set_heads(["Пиццы", "Комбо"])
cc, agent = fresh("n1b")
r = agent.start("n1b", "оформи заказ на pizza.test", stop(), user_id="A")
check("«оформи заказ на pizza.test» (без «как есть») — разделы по разметке",
      "На pizza.test есть разделы:\n- Пиццы\n- Комбо\n" in r, r)
check("«доставь»/«поставь» — не «как есть» (широкий _AS_IS_RE их ловит)",
      ta._AS_IS_RE.search("доставь пиццу")
      and not ta._AS_IS_GOAL_RE.search("закажи пиццу, доставь к семи")
      and not ta._AS_IS_GOAL_RE.search("поставь лайк")
      and not ta._AS_IS_GOAL_RE.search("this is it")
      and ta._AS_IS_GOAL_RE.search("checkout as it is"))
set_heads(["Пиццы", "Комбо"])
cc, agent = fresh("n2")
r = agent.start("n2", "закажи пиццу и доставь к семи на pizza.test", stop(),
                user_id="A")
check("«закажи пиццу и доставь…» — разделы на месте",
      "На pizza.test есть разделы:\n- Пиццы\n- Комбо\n" in r, r)


def js_checks():
    """_SECTION_NAMES_JS на headless-Chromium (как test_ba_sandbox)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: нет Playwright — JS разделов не проверен")
        return
    import glob
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception as e:
        browser = None
        for exe in reversed(sorted(glob.glob(str(
                Path.home() / "Library/Caches/ms-playwright"
                / "chromium_headless_shell-*/*/chrome-headless-shell")))):
            try:
                browser = pw.chromium.launch(headless=True,
                                             executable_path=exe)
                break
            except Exception:
                continue
        if browser is None:
            pw.stop()
            print(f"SKIP: headless Chromium не запускается ({str(e)[:120]})")
            return
    try:
        page = browser.new_page(viewport={"width": 1000, "height": 700})
        card = ("<article style='cursor:pointer'><img alt=''><b>{0}</b> "
                "<button>от {1} ₽</button></article>")
        # Страница товара: единственный h1 — без ровни, его блок — вся
        # страница со всеми ценами; одинокий h4 подвала — так же
        page.set_content(
            "<div id='root'><nav style='position:sticky;top:0'><a>Пиццы</a>"
            "<a>Напитки</a><a>Контакты</a></nav><main><h1>Пепперони</h1>"
            "<section><h2>Пиццы</h2>" + card.format("Пепперони", 349)
            + card.format("Сырная", 279) + "</section><section><h2>Напитки"
            "</h2>" + card.format("Кола", 135) + card.format("Морс", 99)
            + "</section></main><footer><h4>Контакты</h4><p>8 800</p>"
            "</footer></div>")
        got = json.loads(page.evaluate(_ba._SECTION_NAMES_JS))
        check("JS: h2 с ровней — разделы; h1 товара и одинокий h4 подвала "
              "(блок — вся страница) — нет", got == ["Пиццы", "Напитки"], got)
        # Один раздел с позициями среди заголовков того же уровня — ровня
        # есть (у «Десертов» одна позиция), раздел засчитан
        page.set_content(
            "<main><section><h2>Пиццы</h2>" + card.format("Пепперони", 349)
            + card.format("Сырная", 279) + "</section><section><h2>Десерты"
            "</h2>" + card.format("Чизкейк", 199) + "</section></main>")
        got = json.loads(page.evaluate(_ba._SECTION_NAMES_JS))
        check("JS: ровня без позиций — ровня («Пиццы» засчитаны, «Десерты» "
              "с одной позицией — нет)", got == ["Пиццы"], got)
    finally:
        browser.close()
        pw.stop()


print("_SECTION_NAMES_JS на настоящем движке")
js_checks()

print(f"\nИтого: {OKS} проверок, {FAILS} провалов")
sys.exit(1 if FAILS else 0)

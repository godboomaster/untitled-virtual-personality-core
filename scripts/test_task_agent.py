"""Тест агента-автопилота (task_agent): цикл «снапшот → LLM → действие».

Проверяет:
* parse_task_request — явный запуск «задача: …» / «сделай за меня …»;
* parse_agent_action — строгий разбор JSON-действия модели;
* интент "task" LLM-яруса разбора команды (parse_intent_action/resolve_intent_llm);
* полный прогон на фейковом браузере: открыть сайт → вопрос пользователю →
  ответ возвращается в промпт → клик по выбранному товару идёт по idx
  РЕАЛЬНОГО элемента → клик по оплате не исполняется, прогон завершается
  передачей человеку;
* подтверждение «оформить заказ»: «да» исполняет, «нет» уходит модели;
* ввод в чувствительное поле — подтверждение;
* номер вне списка → повторный запрос, потом честный стоп;
* зацикливание, бюджет хода («продолжать?»), отмена.

Запуск: PYTHONPATH=. python3 scripts/test_task_agent.py
"""

import json
import sys
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class FakeCC:
    """Браузер из двух страниц: главная пиццерии и меню."""

    PAGES = {
        "https://pizza.test/": [
            {"idx": 10, "tag": "a", "role": "link", "text": "Меню", "vp": True},
            {"idx": 11, "tag": "input", "role": "textbox", "text": "Телефон",
             "ed": True, "sn": True, "vp": True},
        ],
        "https://pizza.test/menu": [
            {"idx": 20, "tag": "button", "role": "button", "text": "Пепперони", "vp": True},
            {"idx": 21, "tag": "button", "role": "button", "text": "Маргарита", "vp": True},
            {"idx": 22, "tag": "button", "role": "button", "text": "Оформить заказ", "vp": True},
            {"idx": 23, "tag": "button", "role": "button", "text": "Оплатить картой", "vp": True},
        ],
    }

    # Алиас из конфига: открытие pizza.test — известный сайт, без «да»
    sites = {"пиццерия": "https://pizza.test/"}

    def __init__(self):
        self.url = None
        self.executed = []

    def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
        if self.url is None:
            return None, None, None, None, "в браузере ничего не открыто"
        return self.url, "pizza.test", list(self.PAGES[self.url]), 1, None

    def resolve_url(self, token):
        return ({"kind": "url", "value": "https://pizza.test/"}
                if "pizza.test" in token else None)

    def resolve(self, name, web_search=True):
        return None

    def execute(self, act, chat_id="", router=None):
        self.executed.append(dict(act))
        if act["kind"] == "url":
            self.url = act["value"]
        elif act["kind"] == "click" and act["idx"] == 10:
            self.url = "https://pizza.test/menu"
        return True, ""

    @staticmethod
    def describe(act):
        return f"{act['kind']} «{act.get('element') or act.get('value')}»"

    @staticmethod
    def describe_done(act):
        return f"сделал {act['kind']} «{act.get('element') or act.get('value')}»"

    def resolve_key(self, key, site, router, chat_id=""):
        return {"kind": "key", "key": key, "host": "pizza.test"}, None

    def resolve_tab_op(self, goal, op, router, chat_id=""):
        return {"kind": "tab_op", "op": op}, None

    def resolve_read(self, mode, site, chat_id=""):
        return {"kind": "read", "mode": mode}, None


def real_cc(pages: dict, url=None, base_dir=None, clicks=None):
    """Настоящий ComputerControlManager: execute → _confirm_gate → _dispatch;
    заглушены только браузер (_dispatch пишет в dispatched) и снимок
    (_snapshot_for из pages). clicks — {idx: адрес после клика}. Гейт,
    токены, аудит, приватность — настоящие (FakeCC гейта не моделирует)."""
    import tempfile
    from urllib.parse import urlsplit
    from app.features.computer_control import ComputerControlManager

    class RealCC(ComputerControlManager):
        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            self.dismiss_calls.append(auto_dismiss)
            if self.cur is None:
                return None, None, None, None, "в браузере ничего не открыто"
            return (self.cur, urlsplit(self.cur).hostname,
                    [dict(x) for x in self.pages[self.cur]], 1, None)

        def _dispatch(self, action, router=None):
            self.dispatched.append(dict(action))
            if action["kind"] == "url":
                self.cur = action["value"]
            elif action["kind"] == "click" and action.get("idx") in self.clicks:
                self.cur = self.clicks[action["idx"]]

        def _init_vis_baseline(self, *a, **k):
            pass

        def _save_last_page(self, *a, **k):
            pass

    cc = RealCC(context="ta_real", config={"confirm": False, "click": True},
                base_dir=base_dir or Path(tempfile.mkdtemp(prefix="ta_real_")))
    cc.pages, cc.cur, cc.clicks = pages, url, dict(clicks or {})
    cc.dispatched, cc.dismiss_calls = [], []
    return cc


class ScriptedRouter:
    """Отдаёт заранее заданные ответы по очереди и запоминает промпты."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def get_response(self, messages, **kw):
        self.prompts.append(messages[-1]["content"])
        r = self.replies.pop(0) if self.replies else '{"action":"fail","message":"script over"}'
        return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)


def main():
    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    import tempfile
    # Память задач агента — во временную папку, не в data/ персоны
    FakeCC.base_dir = Path(tempfile.mkdtemp(prefix="task_agent_"))
    from app.features import browser_actions as _ba
    _ba.wait_dom_idle = lambda *a, **k: None
    # Позиции раздела по разметке — из браузера; в тестах его нет (тесты
    # раздела подменяют сами)
    _ba.section_items = lambda *a, **k: None
    _ba.section_names = lambda *a, **k: None
    _ba.scroll_step = lambda *a, **k: {"moved": True, "bottom": False}
    # Браузер в тестах не трогаем: листание контейнера — заглушка
    container_scrolls = []
    _ba.scroll_container_step = lambda *a, **k: (
        container_scrolls.append(a), {"moved": True, "bottom": False,
                                      "y0": 0})[1]
    # Приватная страница в тестах не зовёт живую локальную модель (Ollama):
    # по умолчанию «локальной нет»; тесты, которым она нужна, подменяют сами
    from app.features import cc_privacy as _cp_guard
    _cp_guard.PrivateRouter.get_response = lambda self, msgs, **kw: None
    from app.features import task_agent as ta
    from app.features.task_agent import (TaskAgent, parse_agent_action,
                                         parse_agent_actions,
                                         parse_task_request)
    from app.features.computer_control import parse_intent_action

    # ── 1. Явный запуск ──
    check("«задача: закажи пиццу» → цель",
          parse_task_request("задача: закажи пиццу") == "закажи пиццу")
    check("«сделай за меня скачай отчёт» → цель",
          parse_task_request("сделай за меня скачай отчёт")
          == "скачай отчёт")
    check("«task: order a pizza» → цель",
          parse_task_request("task: order a pizza") == "order a pizza")
    for t in ("закажи пиццу", "какая у тебя задача?", "задача", "сделай"):
        check(f"не запуск: «{t}»", parse_task_request(t) is None)

    # ── 2. Разбор действия модели ──
    check("click с номером", parse_agent_action('ok {"action":"click","n":"3"}')
          == {"action": "click", "n": 3})
    check("type без текста → None",
          parse_agent_action('{"action":"type","n":1}') is None)
    check("неизвестное действие → None",
          parse_agent_action('{"action":"pay","n":1}') is None)
    check("key нормализуется по регистру",
          parse_agent_action('{"action":"key","key":"enter"}')
          == {"action": "key", "key": "Enter"})
    check("key вне списка → None",
          parse_agent_action('{"action":"key","key":"F12"}') is None)
    check("ask без вопроса → None",
          parse_agent_action('{"action":"ask"}') is None)
    check("мусор → None", parse_agent_action("нажми на меню") is None)
    # B1: ответы моделей в том виде, в каком они приходят на деле
    check("type с {{secret1}} — разобран (формат из промпта)",
          parse_agent_actions('{"action":"type","n":3,"text":"{{secret1}}"}')
          == [{"action": "type", "n": 3, "text": "{{secret1}}",
               "submit": False}])
    check("текст и {скобки} до JSON — действие найдено",
          parse_agent_actions('Выберу {вариант 2}: {"action":"click","n":2,'
                              '"label":"Меню"}')
          == [{"action": "click", "n": 2, "label": "Меню"}])
    check("<think>…</think> — JSON рассуждения не исполняется",
          parse_agent_actions('<think>{"action":"done","message":"x"}'
                              '</think>\n{"action":"click","n":2}')
          == [{"action": "click", "n": 2}])
    check("незакрытый <think> — пусто",
          parse_agent_actions('<think>{"action":"done","message":"x"}') == [])
    check("n: true / 2.7 / null → None, «3» → 3",
          parse_agent_action('{"action":"click","n":true}') is None
          and parse_agent_action('{"action":"click","n":2.7}') is None
          and parse_agent_action('{"action":"click","n":null}') is None
          and parse_agent_action('{"action":"click","n":"3"}')
          == {"action": "click", "n": 3})
    check("цепочка в ```json``` с мусором между объектами — оба звена",
          parse_agent_actions('```json\n{"action":"click","n":1}\n``` затем '
                              '{"action":"click","n":2}')
          == [{"action": "click", "n": 1}, {"action": "click", "n": 2}])
    check("не-действие после действия обрывает цепочку",
          parse_agent_actions('{"action":"click","n":1}\n{"action":"pay"}\n'
                              '{"action":"click","n":2}')
          == [{"action": "click", "n": 1}])

    # ── 3. Интент task в LLM-ярусе ──
    check("intent task разобран",
          parse_intent_action('{"action":"task","goal":"закажи пиццу"}')
          == {"action": "task", "goal": "закажи пиццу"})
    check("intent task без цели → None",
          parse_intent_action('{"action":"task"}') is None)
    from app.features.computer_control import ComputerControlManager
    act, err = ComputerControlManager.resolve_intent_llm(
        SimpleNamespace(stats={}), "закажи мне пиццу",
        ScriptedRouter(['{"action":"task","goal":"закажи мне пиццу"}']))
    check("resolve_intent_llm → task-действие для агента",
          act == {"kind": "task", "goal": "закажи мне пиццу"} and err is None)

    # ── 4. Полный прогон: вопрос → ответ → выбор → граница оплаты ──
    cc = FakeCC()
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        {"action": "open", "target": "pizza.test"},
        {"action": "click", "n": 1},                     # Меню
        {"action": "ask", "question": "Какую пиццу: Пепперони или Маргариту?"},
        {"action": "click", "n": 1},                     # Пепперони
        {"action": "click", "n": 4},                     # Оплатить картой
    ])
    notes = []
    reply = agent.start("c1", "закажи пиццу на pizza.test", router,
                        notify=notes.append)
    check("прогон встал на вопросе", reply.rstrip().endswith("Маргариту?")
          and agent.active("c1"))
    check("старт — первой строкой хода (не отдельным сообщением после "
          "вопроса), шаги и вопрос — после него",
          not any("Беру:" in n for n in notes) and reply.startswith("Беру:")
          and reply.index("Сделал url") < reply.index("Какую пиццу")
          if "Сделал url" in reply else False)
    check("первый промпт: страницы нет — открыть сайт",
          "Current page: none" in router.prompts[0])
    check("клик «Меню» — по idx реального элемента",
          cc.executed[1]["kind"] == "click" and cc.executed[1]["idx"] == 10)
    reply = agent.feed("c1", "пепперони", router, notify=notes.append)
    check("ответ пользователя попал в промпт",
          "A: пепперони" in router.prompts[3])
    check("клик по выбранной пицце исполнен (idx 20)",
          cc.executed[-1]["idx"] == 20)
    check("оплата не исполнена — передача человеку",
          all(a.get("idx") != 23 for a in cc.executed)
          and "оплат" in reply.lower())
    check("прогон завершён", not agent.active("c1"))

    # ── 5. «Оформить заказ» — подтверждение ──
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        {"action": "click", "n": 3},                     # Оформить заказ
        {"action": "done", "message": "Заказ оформлен."},
        {"action": "done", "message": "Заказ оформлен."},
    ])
    reply = agent.start("c2", "закажи пиццу на pizza.test", router)
    clicks = lambda: [a for a in cc.executed if a.get("kind") == "click"]
    # П.4: коммит заказа — один вопрос с фактами (страница прочитана кодом)
    check("«Оформить заказ» → вопрос да/нет с фактами заказа, клика нет",
          "Оформляю заказ:" in reply and "Нажать «Оформить заказ»? (да/нет)"
          in reply and not clicks())
    reply = agent.feed("c2", "да", router)
    # C3: корзина пуста, страницы «заказ принят» нет — первый done назад
    # модели, повторный принят с честной пометкой человеку
    check("«да» → клик исполнен, затем done",
          clicks() and clicks()[0]["idx"] == 22
          and "Заказ оформлен." in reply and not agent.active("c2"))
    check("C3: done заказа при пустой корзине — модели «NOT finished», "
          "человеку — «проверить не смог»",
          "NOT finished — nothing was added to the cart" in router.prompts[2]
          and "проверить не смог" in reply)

    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        {"action": "click", "n": 3},
        {"action": "done", "message": "Ок, не оформляю."},
    ])
    agent.start("c3", "закажи пиццу на pizza.test", router)
    agent.feed("c3", "нет, сначала маргариту добавь", router)
    check("«нет …» → клик не исполнен, реплика ушла модели в историю",
          not [a for a in cc.executed if a.get("kind") == "click"]
          and "маргариту добавь" in router.prompts[-1])

    # ── 6. Ввод в чувствительное поле — подтверждение ──
    cc = FakeCC()
    cc.url = "https://pizza.test/"
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        {"action": "type", "n": 2, "text": "+7 900 000-00-00"},
    ])
    reply = agent.start("c4", "закажи пиццу на pizza.test", router)
    check("телефон → подтверждение, ввода нет",
          "Делаю?" in reply and not cc.executed)
    agent.cancel("c4")
    check("отмена снимает прогон", not agent.active("c4"))

    # ── 7. Номер вне списка → повтор → честный стоп ──
    cc = FakeCC()
    cc.url = "https://pizza.test/"
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 9},
                             {"action": "click", "n": 9}])
    reply = agent.start("c5", "закажи пиццу", router)
    check("невалидный номер: два запроса и стоп без клика",
          len(router.prompts) == 2 and not cc.executed
          and "not a valid action" in router.prompts[1]
          and not agent.active("c5"))

    # ── 8. Зацикливание ──
    cc = FakeCC()
    cc.url = "https://pizza.test/"
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "key", "key": "Escape"}] * 5)
    reply = agent.start("c6", "закажи пиццу на pizza.test", router)
    check("одно и то же действие ×3 → стоп",
          "по кругу" in reply and len(cc.executed) == 2)

    # ── 9. Бюджет хода → «продолжать?» → «да» ──
    cc = FakeCC()
    cc.url = "https://pizza.test/"
    agent = TaskAgent(cc)
    old = ta.MAX_STEPS_PER_TURN
    ta.MAX_STEPS_PER_TURN = 2
    router = ScriptedRouter([{"action": "scroll"}, {"action": "read"},
                             {"action": "done", "message": "Всё."}])
    try:
        reply = agent.start("c7", "найди что-нибудь", router)
        check("бюджет хода → вопрос «продолжать?»",
              "Продолжать?" in reply and agent.active("c7"))
        reply = agent.feed("c7", "да", router)
        check("«да» → прогон продолжен до done",
              reply.endswith("Всё.") and not agent.active("c7"))
    finally:
        ta.MAX_STEPS_PER_TURN = old

    # ── 10. Эффект действия «до/после» и устаревший клик ──
    class CartCC(FakeCC):
        # Окно товара: «В корзину» закрывает окно, в шапке растёт корзина;
        # второй клик по той же кнопке — «элемент потерян»
        def __init__(self):
            super().__init__()
            self.url = "https://pizza.test/menu"
            self.added = False

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            items = [{"idx": 30, "tag": "a", "role": "link",
                      "text": "Корзина 1" if self.added else "Корзина"}]
            if not self.added:
                items.append({"idx": 31, "tag": "button", "role": "button",
                              "text": "В корзину за 408 ₽", "md": True})
            return self.url, "pizza.test", items, 1, None

        def execute(self, act, chat_id="", router=None):
            self.executed.append(dict(act))
            if act.get("idx") == 31:
                if self.added:
                    return False, "элемент потерян — страница изменилась"
                self.added = True
            return True, ""

    cc = CartCC()
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 2},
                             {"action": "done", "message": "Всё."}])
    r = agent.start("c8", "закажи пиццу на pizza.test", router)
    # Положено — план: сначала «что-нибудь ещё?» (до хода модели)
    check("план: товар в корзине — сразу «что-нибудь ещё?», модель не звалась",
          "Добавить что-нибудь ещё?" in r and len(router.prompts) == 1)
    agent.feed("c8", "нет", router)
    p2 = router.prompts[1]
    check("эффект: окно закрылось, кнопка пропала, корзина выросла",
          "the open dialog closed" in p2
          and "В корзину за 408 ₽" in p2.split("disappeared:")[1].split("\n")[0]
          and "appeared: Корзина 1" in p2)

    run = {"history": [], "page_state": None}
    agent2 = TaskAgent(CartCC())
    agent2.cc.added = True
    agent2._execute(run, "c9", None, {"kind": "click", "idx": 31,
                                      "host": "pizza.test"}, 'click "В корзину"')
    check("«элемент потерян» — записано как «не выполнено», не как провал",
          "NOT performed" in run["history"][-1]
          and "failed" not in run["history"][-1])

    # D11: метка протухла между снимком и кликом (SPA дорисовалась) — одна
    # пересъёмка, тот же элемент с новым номером нажат без лишнего раунда
    class RerenderCC(FakeCC):
        def __init__(self):
            super().__init__()
            self.url, self.gen = "https://pizza.test/menu", 1

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            return self.url, "pizza.test", [
                {"idx": 100 * self.gen, "tag": "button", "role": "button",
                 "text": "Пепперони"}], 1, None

        def execute(self, act, chat_id="", router=None):
            self.executed.append(dict(act))
            if len(self.executed) == 1:
                self.gen += 1  # пока думала модель, SPA перерисовала узлы
            if act.get("idx") != 100 * self.gen:
                return False, "элемент потерян — страница изменилась"
            return True, ""
    cc = RerenderCC()
    router = ScriptedRouter([{"action": "click", "n": 1},
                             {"action": "fail", "message": "стоп"}])
    TaskAgent(cc).start("d11", "x", router)
    check("D11: элемент потерян — пересъёмка и клик по свежему номеру, "
          "модель не тратит раунд", [a["idx"] for a in cc.executed] == [100, 200]
          and 'click "Пепперони" → ok' in router.prompts[1]
          and "NOT performed" not in router.prompts[1])

    # ── 11. Длинный вопрос списком — не режется, переносы сохраняются ──
    long_q = "Какую пиццу?\n" + "\n".join(f"- Пицца {i} от 300 ₽" for i in range(40))
    a = parse_agent_action(json.dumps({"action": "ask", "question": long_q},
                                      ensure_ascii=False))
    check("вопрос длиннее 400 символов не обрезан, список построчно",
          a and len(a["question"]) > 400 and "\n- Пицца 39 от 300 ₽" in a["question"])

    # ── 12. Цепочка действий за один ответ модели ──
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        '{"action":"click","n":1}\n{"action":"click","n":2}',
        {"action": "done", "message": "Всё."},
        {"action": "done", "message": "Всё."}])
    agent.start("c10", "закажи пепперони и маргариту на pizza.test", router)
    check("цепочка: два клика за один запрос к модели",
          [a["idx"] for a in cc.executed] == [20, 21] and len(router.prompts) == 3)

    class StaleCC(FakeCC):
        def execute(self, act, chat_id="", router=None):
            self.executed.append(dict(act))
            if act.get("idx") == 20:
                return False, "элемент потерян — страница изменилась"
            return True, ""
    cc = StaleCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        '{"action":"click","n":1}\n{"action":"click","n":2}',
        {"action": "done", "message": "Всё."}])
    agent.start("c11", "x", router)
    # D11: «элемент потерян» — клика не было; одна пересъёмка и попытка по
    # тому же элементу, дальше цепочка рвётся
    check("цепочка рвётся на устаревшем элементе — второй клик не исполнен",
          [a["idx"] for a in cc.executed] == [20, 20])
    check("цепочка из ask после клика — ask отброшен, клик исполнен",
          TaskAgent._valid_chain(parse_agent_actions(
              '{"action":"click","n":1}{"action":"ask","question":"?"}'), 4)
          == [{"action": "click", "n": 1}])

    # ── 13. Веб-поиск: страница внутри сайта ищется, а не угадывается ──
    check("search с запросом",
          parse_agent_action('{"action":"search","query":"преподавателяин вуза"}')
          == {"action": "search", "query": "преподавателяин вуза"})
    check("search без запроса → None",
          parse_agent_action('{"action":"search"}') is None)
    results = [
        {"title": "вуза - СТАСЫШИН В. М. - Технологии баз данных",
         "url": "https://example.edu/kaf/persons/827/Students/DataBases",
         "snippet": "Курс"},
        {"title": "вуза - СТАСЫШИНА Т. Л. - Общая информация",
         "url": "https://example.edu/kaf/persons/1914/", "snippet": ""}]
    queries = []
    real_search = ta.web_search_links
    ta.web_search_links = lambda q, **kw: (queries.append(q) or (results, None))
    try:
        cc = FakeCC()
        agent = TaskAgent(cc)
        router = ScriptedRouter([
            {"action": "search", "query": "преподавателяин вуза базы данных"},
            {"action": "ask", "question": "преподавателяин В. М. или преподавателяина Т. Л.?"}])
        reply = agent.start("c12", "открой курс базы данных на странице "
                            "стасышина вуза", router)
        check("первый промпт: действие search и правило про поиск/тёзок",
              '"action":"search"' in router.prompts[0]
              and "search first" in router.prompts[0]
              and "namesakes" in router.prompts[0])
        check("поиск ушёл с запросом модели, браузер не трогали",
              queries == ["преподавателяин вуза базы данных"] and not cc.executed)
        check("второй промпт: ссылки результатов с URL",
              "https://example.edu/kaf/persons/1914/" in router.prompts[1]
              and "СТАСЫШИН В. М." in router.prompts[1])
        check("прогон встал на вопросе о выборе",
              reply.rstrip().endswith("преподавателяина Т. Л.?") and agent.active("c12"))
        # Результаты остаются в промпте и после ответа пользователя
        router.replies = [{"action": "done", "message": "ok"}]
        agent.feed("c12", "Т. Л.", router)
        check("результаты поиска видны и на следующем шаге",
              "persons/1914" in router.prompts[-1])
        ta.web_search_links = lambda q, **kw: ([], "no internet connection")
        cc = FakeCC()
        agent = TaskAgent(cc)
        router = ScriptedRouter([{"action": "search", "query": "x"},
                                 {"action": "fail", "message": "нет сети"}])
        agent.start("c13", "x", router)
        check("провал поиска записан в историю для модели",
              'search "x" → failed: no internet connection' in router.prompts[1])
    finally:
        ta.web_search_links = real_search
    check("склонение шагов",
          [ta._steps_ru(n) for n in (1, 4, 5, 11, 12, 21, 22, 25)]
          == ["1 шаг", "4 шага", "5 шагов", "11 шагов", "12 шагов",
              "21 шаг", "22 шага", "25 шагов"])

    # ── 14. Память задач: прошлый сайт и выбор предлагаются вопросом ──
    mem_dir = Path(tempfile.mkdtemp(prefix="task_mem_"))
    cc = FakeCC()
    agent = TaskAgent(cc, memory_path=mem_dir / "task_memory.json")
    # Успешный исход (E5: в «как в прошлый раз» — только такие): заказ
    # дошёл до оплаты, дальше человек
    router = ScriptedRouter([
        {"action": "open", "target": "pizza.test"},
        {"action": "ask", "question": "Какую пиццу?"},
        {"action": "click", "n": 1},                     # Меню
        {"action": "click", "n": 4}])                    # Оплатить картой
    agent.start("m1", "закажи мне пиццу на pizza.test", router)
    agent.feed("m1", "Маргариту, телефон +7 913 123-45-67", router)
    mem = json.loads((mem_dir / "task_memory.json").read_text(encoding="utf-8"))
    rec = (mem.get("m1") or [{}])[-1]
    check("память: цель, сайт, ответ и итог записаны",
          rec.get("goal") == "закажи мне пиццу на pizza.test"
          and rec.get("sites") == ["pizza.test"]
          and rec["qa"][0][0] == "Какую пиццу?"
          and rec.get("result", "").startswith("Дошёл до оплаты")
          and rec.get("ok") is True)
    check("память: телефон из ответа не сохранён",
          "913" not in json.dumps(mem, ensure_ascii=False)
          and rec["qa"][0][1] in ("Маргариту, телефон [hidden]",
                                  "Маргариту, телефон ***(16)"))
    cc = FakeCC()
    agent2 = TaskAgent(cc, memory_path=mem_dir / "task_memory.json")
    router = ScriptedRouter([{"action": "ask",
                              "question": "Заказать на pizza.test, как в прошлый раз?"}])
    agent2.start("m1", "закажи пиццу", router)
    p0 = router.prompts[0]
    check("память: новая задача той же темы видит прошлую — сайт и выбор",
          "earlier tasks on the same topic" in p0 and "sites: pizza.test" in p0
          and "A: Маргариту" in p0 and "like last time" in p0)
    router = ScriptedRouter([{"action": "fail", "message": "x"}])
    TaskAgent(FakeCC(), memory_path=mem_dir / "task_memory.json").start(
        "m1", "закажи суши", router)
    check("память: другая тема («суши») прошлую пиццу не видит",
          "earlier tasks on the same topic" not in router.prompts[0])
    router = ScriptedRouter([{"action": "fail", "message": "x"}])
    TaskAgent(FakeCC(), memory_path=mem_dir / "task_memory.json").start(
        "m2", "закажи пиццу", router)
    check("память: другой чат прошлую задачу не видит",
          "earlier tasks on the same topic" not in router.prompts[0])
    agent2.cancel("m1")
    mem = json.loads((mem_dir / "task_memory.json").read_text(encoding="utf-8"))
    check("память: прогоны без сайта и ответов (отмена на вопросе, провал) не пишутся",
          len(mem["m1"]) == 1 and "m2" not in mem)
    check("вопрос с вариантами: правило «каждый вариант с новой строки»",
          "each option on its own line" in p0)

    # ── 15. Общая политика риска: отправка/коммит/Enter — только после «да» ──
    import logging
    import time as _time
    from app.features import cc_privacy as _ccp
    from app.features.computer_control import ComputerControlManager as _CCM

    class FormCC(FakeCC):
        PAGES = {"https://shop.test/": [
            {"idx": 40, "tag": "button", "role": "button", "text": "Отправить"},
            {"idx": 41, "tag": "button", "role": "button", "text": "Place your order"},
            {"idx": 42, "tag": "button", "role": "button", "text": "",
             "aria": "Оплатить заказ"},
            {"idx": 43, "tag": "textarea", "role": "textbox", "text": "Сообщение",
             "ed": True},
            {"idx": 44, "tag": "input", "role": "searchbox", "text": "Поиск",
             "ed": True, "q": True, "qs": True},
            {"idx": 45, "tag": "button", "role": "button", "text": "Опубликовать"},
            {"idx": 46, "tag": "button", "role": "button", "text": "Подтвердить"},
            {"idx": 47, "tag": "input", "role": "textbox", "text": "Пароль",
             "ed": True, "sn": True},
        ]}
        allow_domains = []
        private_hosts = ()
        _known_domain = _CCM._known_domain
        _privacy_router = _CCM._privacy_router

        def __init__(self):
            super().__init__()
            self.url = "https://shop.test/"

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            return self.url, "shop.test", list(self.PAGES[self.url]), 1, None

        def resolve_url(self, token):
            return ({"kind": "url", "value": token}
                    if token.startswith("https://") else None)

    def solo(reply_obj, chat):
        cc = FormCC()
        agent = TaskAgent(cc)
        router = ScriptedRouter([reply_obj])
        reply = agent.start(chat, "x", router, user_id="A")
        return cc, agent, reply

    for n, lab in ((1, "Отправить"), (6, "Опубликовать"), (7, "Подтвердить")):
        cc, agent, reply = solo({"action": "click", "n": n}, f"r{n}")
        check(f"клик «{lab}» → вопрос да/нет, клика нет",
              "Делаю?" in reply and not cc.executed
              and agent._runs[f"r{n}"]["awaiting"]["kind"] == "confirm")
    # A4: мгновенная покупка (Place your order — заказ со списанием с
    # сохранённой карты) для агента — оплата, её делает человек
    cc, agent, reply = solo({"action": "click", "n": 2}, "r2")
    check("A4: клик «Place your order» → передача человеку, клика нет",
          not cc.executed and "дальше сам" in reply and "r2" not in agent._runs)
    cc, agent, reply = solo({"action": "click", "n": 3}, "r3")
    check("иконка с aria «Оплатить заказ» → передача человеку, клика нет",
          not cc.executed and "оплат" in reply.lower()
          and not agent.active("r3"))
    cc, agent, reply = solo({"action": "type", "n": 4, "text": "привет",
                             "submit": True}, "r4")
    check("ввод + submit в обычное поле → вопрос, ввода нет",
          "Делаю?" in reply and not cc.executed)
    cc, agent, reply = solo({"action": "key", "key": "Enter"}, "r5")
    check("Enter без поиска → вопрос, нажатия нет",
          "Делаю?" in reply and not cc.executed)
    cc, agent, reply = solo({"action": "key", "key": "Tab"}, "r5t")
    check("Tab → вопрос, нажатия нет", "Делаю?" in reply and not cc.executed)
    cc, agent, reply = solo({"action": "key", "key": "Space"}, "r5s")
    check("Space (нажимает кнопку в фокусе) → вопрос, нажатия нет",
          "Делаю?" in reply and not cc.executed
          and agent._runs["r5s"]["awaiting"]["kind"] == "confirm")
    agent.feed("r5s", "да", ScriptedRouter([{"action": "done", "message": "ок"}]),
               user_id="A")
    check("Space после «да» автора — нажат",
          [a.get("key") for a in cc.executed] == ["Space"])

    class YtCC(FormCC):
        def resolve_key(self, key, site, router, chat_id=""):
            # resolve_key на YouTube меняет Space на шорткат k
            return {"kind": "key", "key": "k", "host": "youtube.com"}, None
    cc = YtCC()
    reply = TaskAgent(cc).start("r5y", "x", ScriptedRouter([
        {"action": "key", "key": "Space"}, {"action": "done", "message": "ок"}]),
        user_id="A")
    check("Space, который resolve_key заменил на k (YouTube), — без вопроса",
          [a.get("key") for a in cc.executed] == ["k"])

    # Свой веб-поиск агента: ПДн в запросе — только после «да»
    real_search_links = ta.web_search_links
    sent_q = []
    ta.web_search_links = lambda q, **kw: (sent_q.append(q) or (
        [{"title": "t", "snippet": "", "url": "https://found.test/"}], None))
    try:
        pii_q = "доставка пиццы +7 913 123-45-67 ivan.petrov@mail.ru"
        cc, agent, reply = solo({"action": "search", "query": pii_q}, "sq1")
        aw = agent._runs["sq1"]["awaiting"]
        check("поиск с телефоном/email → вопрос, запрос не ушёл",
              sent_q == [] and aw["kind"] == "confirm" and "(да/нет)" in reply)
        check("вопрос и история — без телефона и email",
              "123-45-67" not in reply and "petrov" not in reply
              and "123-45-67" not in aw["line"] and "petrov" not in aw["line"])
        r = agent.feed("sq1", "да", ScriptedRouter([
            {"action": "done", "message": "ок"}]), user_id="B")
        check("«да» чужого на поиск с ПДн — запрос не ушёл", sent_q == [])
        router = ScriptedRouter([{"action": "done", "message": "ок"}])
        r = agent.feed("sq1", "да", router, user_id="A")
        check("«да» автора — запрос ушёл как есть, в промпте и ответе без ПДн",
              sent_q == [pii_q] and "petrov" not in router.prompts[0]
              and "123-45-67" not in router.prompts[0] and "petrov" not in r)
        sent_q.clear()
        cc, agent, reply = solo({"action": "search", "query": pii_q}, "sq2")
        router = ScriptedRouter([{"action": "done", "message": "ок"}])
        agent.feed("sq2", "нет", router, user_id="A")
        check("«нет» на поиск с ПДн — запроса нет, модель узнала об отказе",
              sent_q == [] and "declined" in router.prompts[0])
        sent_q.clear()
        cc, agent, reply = solo({"action": "search", "query": "пицца пепперони"},
                                "sq3")
        check("обычный поисковый запрос — без вопроса",
              sent_q == ["пицца пепперони"])
        # Пароль без вида токена: ответ на вопрос о пароле и известные
        # секреты чата (хук бота) — запрос только после «да», в вопросе маской
        sent_q.clear()
        cc = FormCC()
        agent = TaskAgent(cc)
        agent.start("sq4", "x", ScriptedRouter([
            {"action": "ask", "question": "Какой пароль от кабинета?"}]),
            user_id="A")
        reply = agent.feed("sq4", "Kotik2019!", ScriptedRouter([
            {"action": "search", "query": "вход kotik2019! личный кабинет"}]),
            user_id="A")
        aw = agent._runs["sq4"]["awaiting"]
        check("поиск с паролем из ответа агенту → вопрос, запрос не ушёл",
              sent_q == [] and aw["kind"] == "confirm" and "(да/нет)" in reply)
        check("вопрос/история поиска — пароль маской",
              "otik2019" not in reply.lower() and "otik2019" not in aw["line"]
              and "***(10)" in reply)
        agent.feed("sq4", "да", ScriptedRouter([
            {"action": "done", "message": "ок"}]), user_id="A")
        check("«да» автора — запрос с паролем ушёл как есть",
              sent_q == ["вход kotik2019! личный кабинет"])
        sent_q.clear()
        agent = TaskAgent(FormCC())
        agent.known_secrets = lambda chat: (["Murzik77"] if chat == "sq5"
                                            else [])
        reply = agent.start("sq5", "x", ScriptedRouter([
            {"action": "search", "query": "murzik77 форум"}]), user_id="A")
        check("поиск с известным секретом чата (хук) → вопрос, маской",
              sent_q == [] and "Murzik77" not in reply
              and "murzik77" not in reply and "***(8)" in reply)
        agent = TaskAgent(FormCC())
        agent.known_secrets = lambda chat: ["Murzik77"]
        agent.start("sq6", "x", ScriptedRouter([
            {"action": "search", "query": "murzik777 и murzik"}]), user_id="A")
        check("известный секрет только отдельным словом — иначе без вопроса",
              sent_q == ["murzik777 и murzik"])
    finally:
        ta.web_search_links = real_search_links

    # Прочитанный текст обычной страницы — в облачный промпт без ПДн
    class ReadCC(FormCC):
        def resolve_read(self, mode, site, chat_id=""):
            return {"kind": "read", "mode": mode}, None

        def execute(self, act, chat_id="", router=None):
            self.executed.append(dict(act))
            if act["kind"] == "read":
                return True, ("Пицца 500 ₽. Пишите ivan.petrov@mail.ru, "
                              "+7 913 123-45-67, карта 4111 1111 1111 1111, "
                              "ключ sk9Fq2LxZ7pW3mB8vT4nR6yH1cJ5dK0a")
            return True, ""
    router = ScriptedRouter([{"action": "read"},
                             {"action": "done", "message": "ок"}])
    TaskAgent(ReadCC()).start("rd1", "x", router, user_id="A")
    pt = router.prompts[-1]
    check("read обычной страницы: в промпте без email/телефона/карты/токена",
          "Page text" in pt and "petrov" not in pt and "123-45-67" not in pt
          and "4111" not in pt and "sk9Fq2" not in pt)
    check("read обычной страницы: цены и слова как есть",
          "Пицца 500 ₽" in pt)

    class PrivReadCC(ReadCC):
        private_hosts = ("shop.test",)
    local_pt = []
    orig_get = _ccp.PrivateRouter.get_response
    try:
        _ccp.PrivateRouter.get_response = lambda self, msgs, **kw: (
            local_pt.append(msgs[-1]["content"])
            or ('{"action":"read"}' if len(local_pt) == 1
                else '{"action":"done","message":"ок"}'))
        TaskAgent(PrivReadCC()).start("rd2", "x", ScriptedRouter([]),
                                      user_id="A")
    finally:
        _ccp.PrivateRouter.get_response = orig_get
    check("read приватной страницы: локальной модели — как есть",
          len(local_pt) == 2 and "ivan.petrov@mail.ru" in local_pt[-1])

    # Лог резолва адреса модели — без токенов и ПДн
    class BoomCC(FormCC):
        def resolve_url(self, token):
            raise RuntimeError(f"bad {token}")
    dbg = []

    class _DH(logging.Handler):
        def emit(self, rec):
            dbg.append(rec.getMessage())
    dh = _DH()
    lg_ta = logging.getLogger("app.features.task_agent")
    old_lvl = lg_ta.level
    lg_ta.addHandler(dh)
    lg_ta.setLevel(logging.DEBUG)
    try:
        TaskAgent(BoomCC())._do_open(
            {"history": [], "qa": []}, "b1", None,
            "https://x.test/cb?access_token=TOKSECRET123&mail=ivan.petrov@mail.ru")
    finally:
        lg_ta.removeHandler(dh)
        lg_ta.setLevel(old_lvl)
    check("лог резолва адреса модели — без токена и email",
          dbg and not any("TOKSECRET123" in m or "petrov" in m for m in dbg))

    # Хук приватности истории: ввод агента — до исполнения и до вопроса
    typed = []
    cc = FormCC()
    agent = TaskAgent(cc)
    agent.on_typed = lambda a: typed.append((a.get("text"), list(cc.executed)))
    agent.start("h1", "x", ScriptedRouter([
        {"action": "type", "n": 8, "text": "Kotik2019!"}]), user_id="A")
    check("on_typed: ввод в поле пароля отмечен до вопроса",
          typed and typed[0] == ("Kotik2019!", []))
    typed.clear()
    agent.feed("h1", "да", ScriptedRouter([{"action": "done", "message": "ок"}]),
               user_id="A")
    check("on_typed: и перед исполнением после «да»",
          typed and typed[0] == ("Kotik2019!", []) and cc.executed)
    agent = TaskAgent(FormCC())
    agent.start("h2", "x", ScriptedRouter([
        {"action": "ask", "question": "Какой пароль?"}]), user_id="A")
    check("awaiting_question: вопрос агента виден боту до записи ответа",
          agent.awaiting_question("h2") == "Какой пароль?"
          and agent.awaiting_question("nope") is None)
    cc = FormCC()
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        '{"action":"type","n":5,"text":"пицца","submit":true}',
        '{"action":"type","n":5,"text":"суши"}\n{"action":"key","key":"Enter"}',
        {"action": "key", "key": "Escape"},
        {"action": "done", "message": "ок"}])
    agent.start("r6", "x", router, user_id="A")
    check("поиск: submit и Enter сразу после ввода в поисковое поле — без «да»",
          [a["kind"] for a in cc.executed] == ["type", "type", "key", "key"]
          and cc.executed[0].get("field_safe"))

    # ── 16. Подтверждение агента: владелец и TTL ──
    cc, agent, reply = solo({"action": "click", "n": 1}, "g1")
    aw = agent._runs["g1"]["awaiting"]
    check("подтверждение подписано автором хода и временем",
          aw["user_id"] == "A" and abs(aw["ts"] - _time.time()) < 5)
    r = agent.feed("g1", "да", ScriptedRouter([]), user_id="B")
    check("«да» чужого участника — шаг не исполнен, ожидание осталось",
          not cc.executed and agent._runs["g1"]["awaiting"] is aw
          and "поставил задачу" in r)
    aw["ts"] = _time.time() - ta.CONFIRM_TTL_SEC - 1
    router = ScriptedRouter([{"action": "fail", "message": "стоп"}])
    r = agent.feed("g1", "да", router, user_id="A")
    check("протухшее подтверждение — «да» не исполняет, модель узнаёт об этом",
          not cc.executed and "confirmation expired" in router.prompts[0])
    check("B7: протухшее «да» — человеку объяснено, почему шаг не сделан",
          "больше 10 минут" in r)
    cc, agent, reply = solo({"action": "click", "n": 1}, "g2")
    # B7: люди отвечают через 2–5 минут — «да» через 4 минуты ещё в срок
    agent._runs["g2"]["awaiting"]["ts"] = _time.time() - 240
    agent.feed("g2", "да", ScriptedRouter([{"action": "done", "message": "ок"}]),
               user_id="A")
    check("«да» автора через 4 минуты — шаг исполнен",
          cc.executed and cc.executed[0]["idx"] == 40)
    # Перехват владения: чужой ответ на вопрос агента раньше делал автора
    # ответа владельцем, и его следующее «да» исполняло рискованный шаг
    cc, agent, reply = solo({"action": "ask", "question": "Какой адрес?"}, "g3")
    router = ScriptedRouter([{"action": "click", "n": 1}])
    r = agent.feed("g3", "Ленина 1", router, user_id="B")
    run = agent._runs["g3"]
    check("чужой ответ на вопрос агента — не принят, владелец прежний",
          run["turn_user"] == "A" and run["awaiting"]["kind"] == "ask"
          and not router.prompts and "поставил" in r)
    agent.feed("g3", "Ленина 1", router, user_id="A")
    r = agent.feed("g3", "да", ScriptedRouter([]), user_id="B")
    check("после ответа владельца чужое «да» на рискованный шаг не исполняет",
          not cc.executed and agent._runs["g3"]["awaiting"]["kind"] == "confirm")
    r = agent.feed("g3", "отмена", ScriptedRouter([]), user_id="B")
    check("чужая «отмена» не снимает задачу", "g3" in agent._runs)

    # ── 17. Открытие адреса, выбранного моделью ──
    def solo_twice(reply_obj, chat):
        # Адрес не из выдачи/страниц — сначала назад модели («NOT opened»);
        # тот же open повтором — обычный путь (вопрос «да/нет»)
        cc = FormCC()
        agent = TaskAgent(cc)
        router = ScriptedRouter([reply_obj, reply_obj])
        reply = agent.start(chat, "x", router, user_id="A")
        return cc, agent, reply, router

    cc, agent, reply, router = solo_twice(
        {"action": "open",
         "target": "https://attacker.example/log?phone=79991234567"}, "o1")
    check("адрес-догадка модели — сначала не открыт, модели объяснено",
          "NOT opened" in router.prompts[1]
          and "attacker.example" in router.prompts[1])
    aw = agent._runs["o1"]["awaiting"]
    check("чужой домен с данными в query → вопрос, via_search, не открыт",
          not cc.executed and aw["kind"] == "confirm"
          and aw["act"].get("via_search") and "attacker.example" in reply)
    check("история агента — без телефона из адреса",
          "79991234567" not in aw["line"])
    cc, agent, reply = solo({"action": "open", "target":
                             "https://pizza.test/menu#x"}, "o2")
    check("известный домен, но с fragment → вопрос", not cc.executed
          and "Делаю?" in reply)
    cc, agent, reply, _r = solo_twice(
        {"action": "open", "target": "https://other.test/"}, "o3")
    check("домен не из sites/allow_domains → вопрос", not cc.executed
          and agent._runs["o3"]["awaiting"]["kind"] == "confirm")

    # Сайты по памяти модели («Додо Пицца — dodo.ru»): вопрос с такими
    # адресами — назад модели; после поиска адреса из выдачи — спрошено
    pizza_q = ("На каком сайте заказать пиццу?\n- Додо Пицца — dodo.ru\n"
               "- Папа Джонс — papajohns.ru")
    found = [{"title": "Додо Пицца", "snippet": "",
              "url": "https://dodopizza.ru/city"},
             {"title": "Папа Джонс", "snippet": "",
              "url": "https://papajohns.ru/city"}]
    ta.web_search_links = lambda q, **kw: (found, None)
    try:
        cc = FormCC()
        agent = TaskAgent(cc)
        router = ScriptedRouter([
            {"action": "ask", "question": pizza_q},
            {"action": "search", "query": "заказать пиццу"},
            {"action": "ask", "question":
             "На каком сайте?\n- Додо Пицца — dodopizza.ru\n"
             "- Папа Джонс — papajohns.ru"}])
        reply = agent.start("s1", "закажи пиццу", router, user_id="A")
        check("сайты-догадки в вопросе — не спрошено, модели велено искать",
              "NOT asked" in router.prompts[1] and "dodo.ru" in router.prompts[1]
              and "Search the web first" in router.prompts[1])
        check("сайты из выдачи поиска — спрошено",
              "dodopizza.ru" in reply and agent._runs["s1"]["awaiting"]
              and agent._runs["s1"]["awaiting"]["kind"] == "ask")
        # Ответ «додо пицца», модель открывает домен из головы — не открыт;
        # домен из выдачи (поддомен совпадает) — открыт
        router = ScriptedRouter([
            {"action": "open", "target": "https://dodo.ru"},
            {"action": "open", "target": "https://dodopizza.ru/"},
            {"action": "done", "message": "ок"}])
        agent.feed("s1", "додо пицца", router, user_id="A")
        check("«dodo.ru» по памяти после выбора — не открыт",
              "NOT opened" in router.prompts[1]
              and all("dodo.ru" != (a.get("value") or "").split("/")[2]
                      for a in cc.executed if a.get("kind") == "url"))
        aw = (agent._runs.get("s1") or {}).get("awaiting") or {}
        opened = [a["value"] for a in cc.executed if a.get("kind") == "url"]
        check("dodopizza.ru из выдачи — не отбит как догадка",
              opened == ["https://dodopizza.ru/"]
              or "dodopizza.ru" in str((aw.get("act") or {}).get("value")))
    finally:
        ta.web_search_links = real_search
    # Сайт, названный человеком в цели, — не догадка
    cc = FormCC()
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "open", "target": "https://other.test/"}])
    agent.start("s2", "открой other.test и найди скидки", router, user_id="A")
    check("домен из слов человека — без «NOT opened»",
          not any("NOT opened" in p for p in router.prompts[1:])
          and agent._runs["s2"]["awaiting"]["kind"] == "confirm")
    from app.features.task_agent import _site_hosts
    check("адреса в тексте: сайты да; почта, файлы, числа — нет",
          _site_hosts("dodo.ru, https://www.dominos.ru/menu, a@b.ru, "
                      "config.py, 3.5, т.д., вуза.рф")
          == ["dodo.ru", "dominos.ru", "вуза.рф"])
    ta.web_search_links = lambda q, **kw: ([{"title": "t", "snippet": "",
                                       "url": "https://found.test/p?id=5"}], None)
    try:
        # B6: настоящий гейт — раньше FakeCC без гейта не видел, что после
        # «можно без да» гейт по тому же флагу via_search всё равно спрашивал
        cc = real_cc({"https://found.test/p?id=5": [
            {"idx": 1, "tag": "a", "role": "link", "text": "Меню"}]})
        agent = TaskAgent(cc)
        router = ScriptedRouter([
            {"action": "search", "query": "x"},
            {"action": "open", "target": "https://found.test/p?id=5"},
            {"action": "done", "message": "ок"}])
        reply = agent.start("o4", "x", router, user_id="A")
        check("B6: ссылка из выдачи своего поиска — открыта без «да», "
              "в аудите from_search",
              [a.get("value") for a in cc.dispatched]
              == ["https://found.test/p?id=5"]
              and cc.dispatched[0].get("from_search")
              and not cc.dispatched[0].get("via_search")
              and "Делаю?" not in reply and "o4" not in agent._runs)
    finally:
        ta.web_search_links = real_search

    # ── 18. Приватность промпта ──
    cc = FormCC()
    cc.PAGES = dict(FormCC.PAGES)
    secret_url = ("https://shop.test/cb?code=SECRETCODE123"
                  "#access_token=TOKVALUE")
    cc.PAGES[secret_url] = FormCC.PAGES["https://shop.test/"]
    cc.url = secret_url
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 1}])
    agent.start("p1", "x", router, user_id="A")
    check("URL страницы в промпте — без code/access_token",
          "SECRETCODE123" not in router.prompts[0]
          and "TOKVALUE" not in router.prompts[0]
          and "Current page: https://shop.test/cb" in router.prompts[0])
    run = {"history": ["click → ok"],
           "effect_base": {"url": "https://shop.test/", "labels": [],
                           "dialog": False}}
    agent._note_effect(run, secret_url, [])
    check("«the address changed to» — без токенов",
          "SECRETCODE123" not in run["history"][-1]
          and "the address changed to https://shop.test/cb" in run["history"][-1])

    class PrivCC(FormCC):
        private_hosts = ("shop.test",)
    orig_get = _ccp.PrivateRouter.get_response
    try:
        _ccp.PrivateRouter.get_response = lambda self, *a, **kw: None
        cc = PrivCC()
        agent = TaskAgent(cc)
        router = ScriptedRouter([{"action": "click", "n": 1}])
        reply = agent.start("p2", "x", router, user_id="A")
        check("приватная страница без локальной модели — облако не спрошено, "
              "пауза и передача человеку",
              router.prompts == [] and not cc.executed
              and agent._runs["p2"]["awaiting"]["kind"] == "continue"
              and agent._runs["p2"]["awaiting"]["user_id"] == "A"
              and "дальше сам" in reply)
        # «да» на передачу человеку — только от автора задачи и в срок
        aw = agent._runs["p2"]["awaiting"]
        router = ScriptedRouter([])
        r = agent.feed("p2", "да", router, user_id="B")
        check("«продолжай» после передачи человеку от чужого — прогон стоит",
              router.prompts == [] and agent._runs["p2"]["awaiting"] is aw
              and "только тот, кто её поставил" in r)
        aw["ts"] = _time.time() - ta.CONTINUE_TTL_SEC - 1
        r = agent.feed("p2", "да", router, user_id="A")
        aw2 = agent._runs["p2"]["awaiting"]
        check("протухшее «продолжать?» — «да» не возобновляет, вопрос заново",
              router.prompts == [] and not cc.executed
              and aw2["kind"] == "continue" and aw2["ts"] > aw["ts"]
              and "Продолжать задачу?" in r)
        r = agent.feed("p2", "да", router, user_id="A")
        check("свежее «да» автора — прогон продолжен (страница всё ещё "
              "приватная — снова передача)", "дальше сам" in r)
        local_prompts = []
        _ccp.PrivateRouter.get_response = lambda self, msgs, **kw: (
            local_prompts.append(msgs[-1]["content"])
            or '{"action":"done","message":"ок"}')
        router = ScriptedRouter([])
        reply = TaskAgent(PrivCC()).start("p3", "x", router, user_id="A")
        check("приватная страница с локальной моделью — решает она, облако нет",
              router.prompts == [] and local_prompts and reply.endswith("ок"))
    finally:
        _ccp.PrivateRouter.get_response = orig_get

    # ── 19. Лог шага и история: введённый текст не открытым текстом ──
    seen_logs = []

    class _H(logging.Handler):
        def emit(self, rec):
            seen_logs.append(rec.getMessage())
    h = _H()
    lg = logging.getLogger("app.features.task_agent")
    old_level = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        cc = FormCC()
        agent = TaskAgent(cc)
        router = ScriptedRouter([
            {"action": "type", "n": 8, "text": "Kotik2019!"},
            {"action": "done", "message": "ок"}])
        agent.start("l1", "x", router, user_id="A")
        q = agent._runs["l1"]["awaiting"]
        agent.feed("l1", "да", router, user_id="A")
    finally:
        lg.removeHandler(h)
        lg.setLevel(old_level)
    check("лог шага агента — без пароля",
          seen_logs and not any("Kotik2019" in m for m in seen_logs))
    check("история/промпт — ввод в поле пароля маской",
          "Kotik2019" not in router.prompts[-1] and "Kotik2019" not in q["line"])
    check("ввод в поле пароля после «да» исполнен с настоящим текстом",
          cc.executed and cc.executed[0]["text"] == "Kotik2019!")

    # ── Приватная страница: история, память задач, ответы-секреты ──
    print("приватная страница и ответы-секреты агента")
    from app.features import cc_privacy as _P

    class LkCC(FakeCC):
        PAGES = {
            "https://shop.test/menu": [
                {"idx": 10, "tag": "a", "text": "Профиль", "vp": True},
                {"idx": 11, "tag": "button", "text": "Пепперони — 599 ₽",
                 "vp": True}],
            "https://shop.test/lk": [
                {"idx": 30, "tag": "div", "text": "Иван Петров", "vp": True},
                {"idx": 31, "tag": "div", "text": "ivan.petrov@mail.ru",
                 "vp": True},
                {"idx": 33, "tag": "div", "text": "Заказ: тест на ВИЧ",
                 "vp": True}],
        }
        base_dir = Path(tempfile.mkdtemp(prefix="task_lk_"))

        def __init__(self):
            super().__init__()
            self.url = "https://shop.test/menu"

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            return self.url, "shop.test", list(self.PAGES[self.url]), 1, None

        def execute(self, act, chat_id="", router=None):
            self.executed.append(dict(act))
            if act.get("kind") == "click" and act.get("idx") == 10:
                self.url = "https://shop.test/lk"
            return True, ""

    _PRIV_LEAKS = ("ivan.petrov@mail.ru", "тест на ВИЧ", "Иван Петров")
    orig_local = _P.PrivateRouter.get_response
    local_prompts = []
    try:
        # Локальной модели нет: на /lk — пауза, человек возвращает на /menu
        _P.PrivateRouter.get_response = (
            lambda self, messages, **kw:
            (local_prompts.append(messages[-1]["content"]), None)[1])
        cc = LkCC()
        agent = TaskAgent(cc)
        router = ScriptedRouter([{"action": "click", "n": 1},
                                 {"action": "fail", "message": "стоп"}])
        agent.start("p1", "закажи пиццу на shop.test", router, user_id="A")
        run = agent._runs.get("p1") or {}
        check("приватная страница: _observe пометил прогон",
              run.get("private_seen") is True)
        check("эффект перехода на /lk — в истории полный (для локальной)",
              "Иван Петров" in (run.get("history") or [""])[-1])
        cc.url = "https://shop.test/menu"
        agent.feed("p1", "да", router, user_id="A")
        cloud = router.prompts[-1]
        check("облачный промпт после приватной страницы — без её подписей",
              not any(s in cloud for s in _PRIV_LEAKS))
        check("облачный промпт: строка шага с заглушкой приватной страницы",
              "private page" in cloud and 'click "Профиль"' in cloud)
        mem = (LkCC.base_dir / "task_memory.json").read_text(encoding="utf-8")
        check("память задач: итог прогона с приватной страницей — заглушкой",
              "private page" in mem and "стоп" not in mem)

        # Локальная модель есть: отчёт по кабинету — не в task_memory.json
        _P.PrivateRouter.get_response = (
            lambda self, messages, **kw: json.dumps(
                {"action": "done",
                 "message": "В профиле: Иван Петров, заказ «тест на ВИЧ»"},
                ensure_ascii=False))
        cc = LkCC()
        mem_path = LkCC.base_dir / "task_memory_local.json"
        agent = TaskAgent(cc, memory_path=mem_path)
        noted = []
        agent.on_private_text = lambda t, h, what="overview": noted.append(t)
        r = agent.start("p2", "проверь мой заказ на shop.test",
                        ScriptedRouter([{"action": "click", "n": 1}]),
                        user_id="A")
        check("отчёт локальной модели — пользователю в чат",
              "тест на ВИЧ" in r)
        check("отчёт локальной модели — в хук приватной истории",
              any("тест на ВИЧ" in t for t in noted))
        mem = mem_path.read_text(encoding="utf-8")
        check("task_memory.json: отчёт с приватной страницы не сохранён",
              not any(s in mem for s in _PRIV_LEAKS))
        cc.url = "https://shop.test/menu"
        r2 = ScriptedRouter([{"action": "fail", "message": "x"}])
        TaskAgent(cc, memory_path=mem_path).start(
            "p2", "проверь мой заказ на shop.test ещё раз", r2, user_id="A")
        check("следующая задача: облако не видит итог приватной страницы",
              r2.prompts and not any(s in r2.prompts[0] for s in _PRIV_LEAKS))

        # find/read с приватной страницы, а страница стала обычной — не в облако
        agent = TaskAgent(LkCC())
        run = {"goal": "g", "lang": "ru", "qa": [], "history": ["x → ok"],
               "steps": 0, "awaiting": None, "busy": False, "cancel": False,
               "sites": [], "obs_extra": {"text": "Мама: анализы плохие",
                                          "private": True}}
        obs = agent._observe(run, "p3")
        check("obs_extra с приватной страницы на обычной — отброшен",
              obs["text"] is None and not obs["private"])
    finally:
        _P.PrivateRouter.get_response = orig_local

    # Ответ на вопрос о данных — {{secretN}} независимо от языка вопроса
    for q in ("Какие данные для входа на shop.test?",
              "What credentials should I sign in with?",
              "Contraseña para shop.test?", "Какую пиццу взять?"):
        check(f"ответ-пароль на «{q}» — секрет",
              ta._secret_answer(q, "Kotik2019!"))
    check("ответ «да» на вопрос о входе — не секрет",
          not ta._secret_answer("Какие данные для входа?", "да"))
    check("выбор из предложенных вариантов — не секрет",
          not ta._secret_answer(
              "Какую?\n- Пепперони — 599 ₽\n- Маргарита — 499 ₽", "пепперони"))
    check("обычный ответ на вопрос о предпочтении — не секрет",
          not ta._secret_answer("Какую пиццу взять?", "маргариту большую")
          and not ta._secret_answer("Какую видеокарту?", "RTX4090"))
    check("«ivan / Kotik2019!» — слово-пароль скрыто",
          ta._pw_shaped("Kotik2019!") and not ta._pw_shaped("shop.test"))
    # B5: обычные ответы — не секреты; секреты внутри ответа — по отдельности
    check("B5: «Пепперони» на «Доступны: …» — не секрет",
          not ta._secret_answer("Доступны: Пепперони, Маргарита. Какую?",
                                "Пепперони")
          and not ta._secret_answer("Карта или наличные?", "картой"))
    run_b5 = {"qa": [("Доступны: Пепперони, Маргарита. Какую?", "Пепперони"),
                     ("Адрес и телефон?", "Ленина 5, 8 913 123-45-67")],
              "goal": "закажи пиццу"}
    hid = TaskAgent(FakeCC())._hidden(run_b5)
    check("B5: «Ленина 5, 8 913…» — телефон своим плейсхолдером, адрес виден",
          hid == {"8 913 123-45-67": "{{secret1}}"})
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        {"action": "ask", "question": "Доступны: Пепперони, Маргарита. Какую?"},
        {"action": "fail", "message": "стоп"}])
    agent.start("b5", "закажи пиццу", router, user_id="A")
    agent.feed("b5", "Пепперони", router, user_id="A")
    check("B5: подписи каталога и ответ в промпте — как есть, без {{secret}}",
          "{{secret" not in router.prompts[-1]
          and "] Пепперони" in router.prompts[-1]
          and "A: Пепперони" in router.prompts[-1])
    run_b5 = {"qa": [("Какой пароль?", "Маргарита")], "goal": "x",
              "history": [], "steps": 0}
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    p_b5 = agent._prompt(dict(run_b5, lang="ru"), agent._observe(
        dict(run_b5, lang="ru"), "b5b"))
    check("B5: секрет прячется в ответах, но не в подписях элементов",
          "{{secret1}}" in p_b5 and "] Маргарита" in p_b5
          and "A: {{secret1}}" in p_b5)
    cc = FakeCC()
    cc.url = "https://pizza.test/"
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        {"action": "ask", "question": "Contraseña para pizza.test?"},
        {"action": "fail", "message": "стоп"}])
    typed_hook = []
    agent.on_typed = lambda a: typed_hook.append(a.get("text"))
    agent.start("s1", "войди в мой аккаунт на pizza.test", router, user_id="A")
    check("answer_is_secret: ответ на вопрос агента по форме",
          agent.answer_is_secret("s1", "Kotik2019!"))
    agent.feed("s1", "Kotik2019!", router, user_id="A")
    check("Contraseña: пароль не уходит в облачный промпт",
          "Kotik2019" not in router.prompts[-1]
          and "{{secret1}}" in router.prompts[-1])
    check("ответ-секрет — в хук маски истории бота",
          "Kotik2019!" in typed_hook)
    mem = (FakeCC.base_dir / "task_memory.json").read_text(encoding="utf-8")
    check("ответ-секрет в task_memory.json — маской", "Kotik2019" not in mem)

    # ── Варианты вопроса — не слова человека (живой прогон dodopizza:
    # «Да» на «- Да, на сайт / - Нет, другой сайт» модель прочла как «нет») ──
    q_site = ("Заказать на pizza.test, как в прошлый раз?\n"
              "- Да, на pizza.test\n- Нет, выбрать другой сайт")
    cc = FakeCC()
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "ask", "question": q_site},
                             {"action": "fail", "message": "стоп"}])
    agent.start("o1", "закажи пиццу", router, user_id="A")
    agent.feed("o1", "Да", router, user_id="A")
    p = router.prompts[-1]
    check("«Да» → явно выбран вариант (1)",
          "A: Да  → the user picked option (1) Да, на pizza.test" in p)
    check("варианты не стоят пунктами «- …» рядом с ответами",
          "\n- Нет, выбрать другой сайт" not in p
          and "options you offered: (1) Да, на pizza.test; (2) Нет" in p)
    q_menu = ("Какую пиццу?\n- Гавайская, 20 см — как в прошлый раз\n"
              "- Другая пицца\n- Открыть меню и показать варианты")
    check("«открой меню» — ближе всего к варианту «Открыть меню»",
          ta._chosen_option(q_menu, "открой меню") == (3, False))
    check("«2» — вариант 2 точно", ta._chosen_option(q_menu, "2") == (2, True))
    check("ответ вне вариантов — без привязки",
          ta._chosen_option(q_menu, "пепперони 30 см") == (None, False))
    check("вопрос без вариантов — формат ответа прежний",
          ta._qa_text("Какую пиццу?", "Маргариту")
          == "- Q: Какую пиццу?\n  A: Маргариту")

    # ── «В корзину»: не в хвосте цепочки и не второй раз без «да» ──
    class CartCC(FakeCC):
        # Окно товара остаётся открытым, добавление видно по счётчику
        # корзины; works=False — клик «проходит», но товар не добавляется
        works = True

        def __init__(self):
            super().__init__()
            self.n_added = 0

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            cart = f"Корзина {self.n_added}" if self.n_added else "Корзина"
            return self.url, "pizza.test", [
                {"idx": 30, "tag": "button", "role": "button", "text": "20 см",
                 "vp": True, "md": True},
                {"idx": 31, "tag": "button", "role": "button",
                 "text": "В корзину за 408 ₽", "vp": True, "md": True},
                {"idx": 32, "tag": "button", "role": "button",
                 "text": "Закрыть", "vp": True, "md": True},
                {"idx": 33, "tag": "a", "role": "link", "text": cart,
                 "vp": True}], 1, None

        def execute(self, act, chat_id="", router=None):
            self.executed.append(dict(act))
            if act.get("idx") == 31 and self.works:
                self.n_added += 1
            return True, ""

    cc = CartCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        '{"action":"click","n":1}\n{"action":"click","n":2}',
        {"action": "click", "n": 2},
        {"action": "click", "n": 2},
        {"action": "click", "n": 2},
        {"action": "fail", "message": "стоп"}])
    reply = agent.start("k1", "закажи пиццу на pizza.test", router, user_id="A")
    # План: положено — «что-нибудь ещё?»; «нет» — дальше модель (повторы
    # «В корзину» ниже — после этого ответа)
    check("план: после первого «В корзину» — «что-нибудь ещё?»",
          "Добавить что-нибудь ещё?" in reply)
    reply = agent.feed("k1", "нет", router, user_id="A")
    check("«В корзину» после выбора размера — не в той же цепочке",
          [a["idx"] for a in cc.executed][:2] == [30, 31]
          and len(router.prompts) >= 2)
    check("первое «В корзину» — исполнено, засчитано по счётчику корзины",
          "added: the cart went from empty to 1 item(s)" in router.prompts[2])
    check("первый повтор «В корзину» — не нажат, назад модели без паузы",
          "NOT performed — this task already added" in router.prompts[3])
    check("второй повтор — не нажат, вопрос человеку (не стоп «зациклился»)",
          [a["idx"] for a in cc.executed].count(31) == 1
          and "уже добавлено" in reply and "(да/нет)" in reply)
    check("C1: окно товара открыто, счётчик +1 — ровно один клик, без "
          "«не сработала»", [a["idx"] for a in cc.executed].count(31) == 1
          and "не сработала" not in reply)
    agent.feed("k1", "да", router, user_id="A")
    check("«да» на повторное «В корзину» — нажато",
          [a["idx"] for a in cc.executed].count(31) == 2)

    # C1: счётчик виден и не вырос — НЕ добавлено; повтор вслепую — только с
    # «да» (раньше: «Press it once more» и повтор сразу — дубли в корзине)
    class SameCC(CartCC):
        works = False

        def __init__(self):
            super().__init__()
            self.n_added = 1  # в корзине уже что-то есть: «Корзина 1»
    cc = SameCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 2},
                             {"action": "click", "n": 2}])
    reply = agent.start("k2", "закажи пиццу на pizza.test", router, user_id="A")
    check("C1: счётчик не вырос — модели «NOT added», не «нажми ещё»",
          "NOT added: the cart stayed at 1 item(s)" in router.prompts[1]
          and "once more" not in router.prompts[1])
    check("C1: повтор без нового действия — не нажат, вопрос человеку",
          [a["idx"] for a in cc.executed].count(31) == 1
          and "не сработала" in reply and "(да/нет)" in reply)
    cc = SameCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 2},
                             {"action": "click", "n": 1},
                             {"action": "click", "n": 2},
                             {"action": "fail", "message": "стоп"}])
    agent.start("k3", "закажи пиццу на pizza.test", router, user_id="A")
    check("C1: после нового действия (выбрана опция) повтор — не вслепую, "
          "нажат", [a["idx"] for a in cc.executed] == [31, 30, 31])

    # Корзины на странице нет (голая цифра без слова «корзина») — не
    # проверено: модели «открой корзину», повтор до проверки не нажат
    class DigitCC(CartCC):
        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            url, host, items, tab, err = super()._snapshot_for(site_word)
            items[-1] = dict(items[-1], text=str(self.n_added))
            return url, host, items, tab, err
    cc = DigitCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 2},
                             {"action": "click", "n": 2},
                             {"action": "fail", "message": "стоп"}])
    agent.start("k5", "купи чехол на pizza.test", router, user_id="A")
    check("C1: корзины нет на странице — «NOT verified: open the cart»",
          "NOT verified" in router.prompts[1]
          and "open the cart" in router.prompts[1])
    check("C1: до проверки корзины повтор не нажат",
          [a["idx"] for a in cc.executed].count(31) == 1)
    cc = DigitCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 2}, {"action": "read"},
                             {"action": "click", "n": 2},
                             {"action": "fail", "message": "стоп"}])
    agent.start("k5b", "купи чехол на pizza.test", router, user_id="A")
    check("C1: после чтения корзины повтор разрешён (решение не вслепую)",
          [a["idx"] for a in cc.executed if a.get("idx")].count(31) == 2)

    # Не корзина: браузер видел реакцию, подписи те же (лайк/подписка) —
    # модели «скорее всего сработало, не повторяй вслепую»
    cc = CartCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 1},
                             {"action": "fail", "message": "стоп"}])
    agent.start("k6", "поставь лайк", router, user_id="A")
    check("переключатель без видимых изменений — «не повторяй вслепую»",
          "do not repeat it blindly" in router.prompts[1]
          and "the page looks unchanged" not in router.prompts[1])

    # Запуск после «да» на «Берусь за задачу?» — без второго «Берусь»
    cc = FakeCC()
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "ask", "question": "Какой сайт?"}])
    reply = agent.start("r1", "закажи пиццу", router, announce=False)
    check("announce=False — ответ только вопрос агента",
          reply == "Какой сайт?")

    # «done» с вопросом — задача не закрывается, ответ идёт в неё
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        {"action": "done", "message": "Открылась не та пицца. Продолжить?"},
        {"action": "fail", "message": "стоп"}])
    agent.start("r2", "закажи пиццу", router, announce=False, user_id="A")
    check("done с вопросом — прогон жив и ждёт ответа",
          agent.active("r2")
          and agent.awaiting_question("r2") == "Открылась не та пицца. Продолжить?")
    agent.feed("r2", "да", router, user_id="A")
    check("ответ на такой вопрос — в промпте модели",
          "A: да" in router.prompts[-1])

    # Опечатка в «да» на подтверждение — переспрос, а не отказ
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 3},   # Оформить заказ
                             {"action": "fail", "message": "стоп"}])
    agent.start("r3", "закажи пиццу на pizza.test", router, user_id="A")
    reply = agent.feed("r3", "а", router, user_id="A")
    check("«а» на «да/нет» — переспрос, шаг не исполнен, вопрос жив",
          "Не понял" in reply and len(router.prompts) == 1
          and not any(a.get("idx") == 22 for a in cc.executed))
    agent.feed("r3", "lf", router, user_id="A")
    check("«lf» (раскладка) — «да»: шаг исполнен",
          any(a.get("idx") == 22 for a in cc.executed))

    # Итог задачи → «да» следующей репликой — прогон возобновлён
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        {"action": "fail", "message": "Не нашёл Гавайскую."},
        {"action": "fail", "message": "стоп"}])
    agent.start("r4", "закажи пиццу", router, user_id="A")
    check("после итога прогона нет", not agent.active("r4"))
    check("чужое «да» не возобновляет",
          not agent.reopen("r4", "да", user_id="B"))
    check("«да» автора — возобновлено", agent.reopen("r4", "да", user_id="A")
          and agent.active("r4"))
    agent.feed("r4", "да", router, user_id="A")
    check("возобновлённый прогон видит итог и ответ",
          "Не нашёл Гавайскую" in router.prompts[-1]
          and "A: да" in router.prompts[-1])
    router = ScriptedRouter([{"action": "fail", "message": "Не вышло."}])
    agent.start("r5", "закажи пиццу", router, user_id="A")
    check("другая реплика после итога снимает возобновление",
          not agent.reopen("r5", "какая погода?", user_id="A")
          and not agent.reopen("r5", "да", user_id="A"))
    router = ScriptedRouter([{"action": "ask", "question": "Какую?"}])
    agent.start("r6", "закажи пиццу", router, user_id="A")
    agent.feed("r6", "отмена", router, user_id="A")
    check("отменённый человеком прогон не возобновляется",
          not agent.reopen("r6", "да", user_id="A"))

    # Номер и подпись клика расходятся — берётся элемент с подписью
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 1, "label": "Маргарита"},
                             {"action": "click", "n": 1, "label": "Гавайская"},
                             {"action": "fail", "message": "стоп"}])
    agent.start("r7", "закажи маргариту на pizza.test", router, user_id="A")
    check("n=1 «Пепперони», label «Маргарита» — нажата Маргарита",
          [a["idx"] for a in cc.executed] == [21])
    check("подписи нет в списке — не нажато, модели объяснено",
          "NOT performed — element 1 is" in router.prompts[-1])

    # «Закрыть» окно товара — рутина, без «да/нет»
    cc = CartCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 3},
                             {"action": "fail", "message": "стоп"}])
    reply = agent.start("k3", "закажи пиццу на pizza.test", router, user_id="A")
    check("«Закрыть» — нажато без подтверждения",
          [a["idx"] for a in cc.executed] == [32] and "(да/нет)" not in reply)

    # ── Повторный клик по сработавшей опции/переключателю ──
    class ToggleCC(FakeCC):
        """Окно товара: опция-переключатель меняет состояние и цену на
        кнопке корзины; report_on=False — сайт без признаков состояния."""

        def __init__(self, report_on=True, sticky=False):
            super().__init__()
            self.url = "https://pizza.test/menu"
            self.on = {"jal": False}
            self.report_on, self.sticky, self.plus = report_on, sticky, 0

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            jal = {"idx": 41, "tag": "button", "role": "", "vp": True,
                   "md": True, "text": "Острый перец халапеньо 49 ₽"}
            if self.report_on:
                jal["on"] = 1 if self.on["jal"] else 0
            price = 769 + (49 if self.on["jal"] else 0)
            return self.url, "pizza.test", [
                {"idx": 40, "tag": "label", "text": "20 см", "vp": True,
                 "md": True, "on": 1},
                jal,
                {"idx": 42, "tag": "button", "text": "+", "vp": True,
                 "md": True, "ctx": f"Количество {self.plus}"},
                {"idx": 43, "tag": "button", "text": f"В корзину за {price} ₽",
                 "vp": True, "md": True}], 1, None

        def execute(self, act, chat_id="", router=None):
            self.executed.append(dict(act))
            if act.get("idx") == 41 and not self.sticky:
                self.on["jal"] = not self.on["jal"]
            if act.get("idx") == 42:
                self.plus += 1
            return True, ""

    def jal_clicks(cc):
        return sum(1 for a in cc.executed if a.get("idx") == 41)

    cc = ToggleCC()
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        '{"action":"click","n":1}\n{"action":"click","n":2}\n'
        '{"action":"click","n":4}',
        {"action": "click", "n": 2},
        {"action": "click", "n": 2},
        {"action": "fail", "message": "стоп"}])
    reply = agent.start("t1", "гавайская 20 см с халапеньо", router,
                        user_id="A")
    def jal_line(prompt):
        return next((ln for ln in prompt.splitlines()
                     if "халапеньо 49 ₽" in ln and ln.startswith("2)")), "")

    check("опция: состояние в строке элемента",
          jal_line(router.prompts[0]).endswith("— not selected/off")
          and jal_line(router.prompts[1]).endswith("— selected/on"))
    check("опция: смена состояния — в заметке «after it»",
          'is now selected/on' in router.prompts[1])
    check("опция: повтор клика — не нажато, модели объяснено",
          jal_clicks(cc) == 1 and cc.on["jal"]
          and "clicking it again would most likely undo it"
          in router.prompts[2])
    check("опция: настойчивый повтор — вопрос человеку, не «хожу по кругу»",
          jal_clicks(cc) == 1 and "уже нажата" in reply
          and "по кругу" not in reply)
    agent.feed("t1", "да", router, user_id="A")
    check("опция: «да» на повтор — нажато", jal_clicks(cc) == 2)

    # Сайт без признаков состояния: повтор сразу после сработавшего клика
    cc = ToggleCC(report_on=False)
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 2},
                             {"action": "click", "n": 2},
                             {"action": "fail", "message": "стоп"}])
    agent.start("t2", "гавайская с халапеньо", router, user_id="A")
    check("опция без состояния: повтор сразу после клика — не нажат",
          jal_clicks(cc) == 1 and "NOT performed — you already clicked"
          in router.prompts[-1])

    # Явно невыбрана после клика (клик не включил) — повтор разрешён
    cc = ToggleCC(sticky=True)
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 2},
                             {"action": "click", "n": 2},
                             {"action": "fail", "message": "стоп"}])
    agent.start("t3", "гавайская с халапеньо", router, user_id="A")
    check("опция не включилась (on=0) — повтор нажат", jal_clicks(cc) == 2)

    # Счётчик «+» — повторяемый контрол, жмётся подряд
    cc = ToggleCC()
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 3},
                             {"action": "click", "n": 3},
                             {"action": "fail", "message": "стоп"}])
    agent.start("t4", "две гавайские", router, user_id="A")
    check("«+» дважды подряд — оба нажаты", cc.plus == 2)

    # ── Список элементов: дубли карточек и внеэкранный каталог не вытесняют
    # шапку (корзина — кнопка с ценой, «Корзина» только в aria) ──
    from app.features.task_agent import _pick_shown, _chosen_option
    cards = []
    for k in range(40):
        for _ in range(3):  # вложенные куски одной карточки
            cards.append({"idx": 100 + len(cards), "tag": "div",
                          "text": f"Пицца {k} от {300 + k} ₽",
                          "ctx": f"Пицца {k} от {300 + k} ₽ Выбрать",
                          "vp": k < 5})
    cart = {"idx": 999, "tag": "button", "text": "4 0 8 ₽",
            "aria": "Корзина - 408 ₽", "vp": True}
    shown = _pick_shown(cards + [cart])
    check("список: дубли карточек схлопнуты, видимая кнопка шапки не "
          "срезана лимитом", cart in shown and len(shown) == 41
          and sum(1 for it in shown if it["text"] == "Пицца 1 от 301 ₽") == 1)
    many = [{"idx": 2000 + k, "tag": "a", "text": f"Товар {k}", "vp": False}
            for k in range(ta.ELEMENTS_MAX + 20)]
    shown = _pick_shown(many + [cart])
    check("список: сверх лимита — сначала видимые, порядок страницы",
          len(shown) == ta.ELEMENTS_MAX and shown[-1] is cart
          and shown[0]["idx"] == 2000)
    line = TaskAgent._elem_line(1, cart)
    check("подпись-цена с aria — модель видит «Корзина»",
          "Корзина - 408 ₽" in line and "4 0 8 ₽" in line)

    # ── Ответ — название варианта без лишних слов ──
    q = ("Какой напиток?\n- Добрый Кола 0,5 л — 135 ₽\n"
         "- Добрый Кола без сахара 0,5 л — 135 ₽\n- Другая (напишите)")
    check("«Добрый Кола» — вариант без «без сахара», точно",
          _chosen_option(q, "Добрый Кола") == (1, True))
    check("«Добрый Кола без сахара» — второй вариант",
          _chosen_option(q, "добрый кола без сахара") == (2, True))

    # ── Варианты-товары с ценой, которых не было на страницах, — назад
    # модели; повтор того же вопроса — человеку ──
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    invented = ("Какой напиток?\n- Апельсиновый сок 0,3 л — ~129 ₽\n"
                "- Морс клюквенный 0,5 л — ~149 ₽\n- Перейти в корзину")
    router = ScriptedRouter([{"action": "ask", "question": invented},
                             {"action": "ask", "question": invented}])
    reply = agent.start("g1", "закажи пиццу и напиток", router, user_id="A",
                        announce=False)
    check("придуманные варианты с ценой — не спрошено, модели объяснено",
          "NOT asked" in router.prompts[1]
          and "Апельсиновый сок" in router.prompts[1])
    check("повтор того же вопроса — уходит человеку",
          agent._runs["g1"]["awaiting"]["kind"] == "ask"
          and "Апельсиновый сок" in reply)
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    real = "Какую?\n- Пепперони — 599 ₽\n- Маргарита — 549 ₽"
    router = ScriptedRouter([{"action": "ask", "question": real}])
    reply = agent.start("g2", "закажи пиццу", router, user_id="A",
                        announce=False)
    check("варианты со страницы — спрошено сразу",
          len(router.prompts) == 1 and "Пепперони" in reply)

    # ── Цепочка: следующее звено — по свежему снимку ──
    class SizeCC(FakeCC):
        """Окно товара: выбор размера меняет цены опций (и их разметку)."""

        def __init__(self):
            super().__init__()
            self.url = "https://pizza.test/menu"
            self.small, self.gen = False, 0

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            self.gen += 1
            b = 1000 * self.gen
            jal = 49 if self.small else 79
            return self.url, "pizza.test", [
                {"idx": b + 1, "tag": "label", "text": "20 см", "vp": True,
                 "md": True},
                {"idx": b + 2, "tag": "button", "vp": True, "md": True,
                 "text": f"Халапеньо {jal} ₽"},
                {"idx": b + 3, "tag": "button", "vp": True, "md": True,
                 "text": "Сырный бортик"}], 1, None

        def execute(self, act, chat_id="", router=None):
            self.executed.append(dict(act))
            if act.get("idx") and act["idx"] % 1000 == 1:
                self.small = True
            return True, ""

    cc = SizeCC()
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        '{"action":"click","n":1,"label":"20 см"}\n'
        '{"action":"click","n":2,"label":"Халапеньо 79 ₽"}',
        {"action": "fail", "message": "стоп"}])
    agent.start("c1", "гавайская 20 см с халапеньо", router, user_id="A")
    check("цепочка: подпись звена сменилась (новая цена) — звено не нажато",
          [a.get("element") for a in cc.executed] == ["20 см"]
          and "its label changed" in router.prompts[1]
          and "Халапеньо 49 ₽" in router.prompts[1])
    cc = SizeCC()
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        '{"action":"click","n":1,"label":"20 см"}\n'
        '{"action":"click","n":3,"label":"Сырный бортик"}',
        {"action": "fail", "message": "стоп"}])
    agent.start("c2", "гавайская 20 см с бортиком", router, user_id="A")
    check("цепочка: подпись та же — звено нажато по номеру свежего снимка",
          [a.get("element") for a in cc.executed] == ["20 см", "Сырный бортик"]
          and cc.executed[1]["idx"] == 2003)

    # ── «продолжать?» не протухает за минуту ──
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "scroll"}, {"action": "read"}]
                            * (ta.MAX_STEPS_PER_TURN // 2)
                            + [{"action": "done", "message": "готово"}])
    reply = agent.start("k1", "найди пиццу", router, user_id="A")
    aw = agent._runs["k1"]["awaiting"]
    aw["ts"] = _time.time() - 180
    reply = agent.feed("k1", "да", router, user_id="A")
    check("«да» через 3 минуты на «продолжать?» — прогон продолжен",
          aw["kind"] == "continue" and "Пауза затянулась" not in reply
          and "готово" in reply)

    # ── A1: авто-закрытие оверлея в пути агента ──
    pages_a1 = {"https://pizza.test/": [
        {"idx": 1, "tag": "a", "role": "link", "text": "Меню"}]}
    cc = real_cc(pages_a1, url="https://user.test/")
    cc.pages["https://user.test/"] = [
        {"idx": 1, "tag": "button", "role": "button", "text": "ОК"}]
    agent = TaskAgent(cc)
    agent.start("a1", "закажи пиццу на pizza.test", ScriptedRouter([
        {"action": "open", "target": "https://pizza.test/"}]), user_id="A")
    agent.feed("a1", "да", ScriptedRouter([{"action": "fail", "message": "стоп"}]),
               user_id="A")
    check("A1: до первого open (вкладка человека) — без авто-закрытия; "
          "после — только cookie/consent",
          [a["value"] for a in cc.dispatched] == ["https://pizza.test/"]
          and cc.dismiss_calls[0] is False
          and cc.dismiss_calls[-1] == "consent"
          and True not in cc.dismiss_calls)
    from app.features import browser_actions as _ba_a1
    from app.features.computer_control import ComputerControlManager as _M
    saved = (_ba_a1.dismiss_overlay, _ba_a1.snapshot_elements,
             _ba_a1.reveal_player_controls)
    seen_a1 = []
    try:
        _ba_a1.dismiss_overlay = lambda h=None, tab_id=None, consent_only=False: (
            seen_a1.append(consent_only) or "Принять")
        _ba_a1.snapshot_elements = lambda h, tab_id=None: (
            "https://pizza.test/", "pizza.test", [dict(pages_a1[
                "https://pizza.test/"][0])])
        _ba_a1.reveal_player_controls = lambda *a, **k: None
        m = _M(context="a1", config={"confirm": False},
               base_dir=Path(tempfile.mkdtemp(prefix="ta_a1_")))
        m._snapshot_for("pizza.test", chat_id="a1c", auto_dismiss="consent")
        check("A1: _snapshot_for(auto_dismiss=\"consent\") → только "
              "cookie/consent, нажатое — агенту (take_dismissed)",
              seen_a1 == [True] and m.take_dismissed("a1c") == "Принять"
              and m.take_dismissed("a1c") is None)

        class DismissCC(FakeCC):
            def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
                self.auto = auto_dismiss
                return _M._snapshot_for(m, "pizza.test", chat_id=chat_id,
                                        auto_dismiss=auto_dismiss)

            def take_dismissed(self, chat_id):
                return m.take_dismissed(chat_id)

        cc = DismissCC()
        agent = TaskAgent(cc)
        router = ScriptedRouter([{"action": "scroll"},
                                 {"action": "fail", "message": "стоп"}])
        run_a1 = {"goal": "x", "lang": "ru", "qa": [], "history": [],
                  "steps": 0, "awaiting": None, "obs_extra": None,
                  "busy": False, "cancel": False, "touched": 0, "sites": [],
                  "turn_user": "A", "past": [], "opened": True}
        agent._observe(run_a1, "a1c")
        check("A1: автонажатие — в истории шага",
              run_a1["history"][-1]
              == 'auto: closed a cookie/consent banner → clicked "Принять"')
    finally:
        (_ba_a1.dismiss_overlay, _ba_a1.snapshot_elements,
         _ba_a1.reveal_player_controls) = saved
    from app.features.browser_actions import _DISMISS_OVERLAY_JS as _dj
    check("A1: JS — строгий режим только cookie/consent/gdpr, «ОК» в диалоге "
          "заказа/оплаты/удаления не жмётся и в общем режиме",
          "__STRICT__" in _dj and "if(strict&&!/cookie|consent|gdpr/i" in _dj
          and "if(strict&&danger)continue;" in _dj
          and "var cookie=/cookie|gdpr|consent/i.test(cls);" in _dj
          and "var danger=money||kill||(!cookie&&act);" in _dj
          and "function outer(b)" in _dj)

    # ── A2/A4: многоязычные подписи коммита и оплаты — настоящий гейт ──
    labels_a2 = [("Jetzt bezahlen", "pay"), ("Zahlungspflichtig bestellen", "pay"),
                 ("Commander", "ask"), ("Valider la commande", "ask"),
                 ("Замовити", "ask"), ("Enviar", "ask"), ("PayPal", "pay"),
                 ("SberPay", "pay"), ("ЮMoney", "pay"), ("Оплатить долями", "pay"),
                 ("Сплит", "pay"), ("Siparişi onayla", "ask"),
                 ("Zamawiam i płacę", "pay"), ("Записаться", "ask"),
                 ("Ответить", "ask"), ("Buy now", "pay"),
                 ("Купить в 1 клик", "pay"), ("Place your order", "pay"),
                 ("Оформить заказ", "ask")]
    for lab, want in labels_a2:
        cc = real_cc({"https://shop.test/": [
            {"idx": 7, "tag": "button", "role": "button", "text": lab}]},
            url="https://shop.test/")
        agent = TaskAgent(cc)
        reply = agent.start(f"a2-{lab}", "x", ScriptedRouter([
            {"action": "click", "n": 1, "label": lab}]), user_id="A")
        got = ("pay" if "дальше сам" in reply else
               "ask" if "Делаю?" in reply else "click")
        check(f"A2/A4: «{lab}» → {want} (без клика)",
              got == want and not cc.dispatched)
    from app.features.computer_control import ComputerControlManager as _CM
    check("A4: мгновенная покупка — оплата только для агента (у команды "
          "человека — коммит с «да»)",
          _CM.risky_label({"kind": "click", "element": "Buy now"}) == "commit"
          and _CM.risky_label({"kind": "click", "element": "Buy now",
                               "origin": "task"}) == "payment")
    check("A2: словари не ловят обычные подписи",
          all(_CM.risky_label({"kind": "click", "element": x}) is None
              for x in ("Replace", "Placeholder", "Pagination", "Réglages",
                        "Сплит-система LG", "Купить", "В корзину", "Далее")))

    # ── C: корзина по счётчику, товар — ключ, done сверяется с брифом ──
    class ShopCC(FakeCC):
        """Меню с двумя товарами (у каждого своя кнопка «В корзину за 408 ₽»),
        окно товара не закрывается, счётчик корзины в шапке растёт."""
        def __init__(self, works=True):
            super().__init__()
            self.url = "https://pizza.test/menu"
            self.n_added, self.works = 0, works
            self.sizes = None  # [(подпись, on)] — окно размеров

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            cart = (f"Корзина {self.n_added} · {408 * self.n_added} ₽"
                    if self.n_added else "Корзина")
            items = [
                {"idx": 1, "tag": "button", "role": "button",
                 "text": "В корзину за 408 ₽",
                 "ctx": "Пепперони 30 см, традиционное тесто В корзину за 408 ₽"},
                {"idx": 2, "tag": "button", "role": "button",
                 "text": "В корзину за 408 ₽",
                 "ctx": "Маргарита 30 см, традиционное тесто В корзину за 408 ₽"},
                {"idx": 3, "tag": "a", "role": "link", "text": cart}]
            for i, (lab, on) in enumerate(self.sizes or (), 10):
                items.append({"idx": i, "tag": "button", "role": "button",
                              "text": lab, "on": on})
            return self.url, "pizza.test", items, 1, None

        def execute(self, act, chat_id="", router=None):
            self.executed.append(dict(act))
            if act.get("idx") in (1, 2) and self.works:
                self.n_added += 1
            elif act.get("idx", 0) >= 10 and self.sizes:
                k = act["idx"] - 10
                self.sizes = [(lab, 1 if j == k else 0)
                              for j, (lab, _on) in enumerate(self.sizes)]
            return True, ""

    class SlotRouter:
        # Разбор ответа по слотам (отдельный вызов): скриптованные JSON
        def __init__(self, replies):
            self.replies, self.prompts = list(replies), []

        def get_response(self, messages, **kw):
            self.prompts.append(messages[-1]["content"])
            r = self.replies.pop(0) if self.replies else {}
            return json.dumps(r, ensure_ascii=False)

    two = {"items": [{"name": "Пепперони"}, {"name": "Маргарита"}]}
    cc = ShopCC()
    agent = TaskAgent(cc)
    agent.slot_router = SlotRouter([two])
    router = ScriptedRouter([{"action": "click", "n": 1},
                             {"action": "click", "n": 2},
                             {"action": "done", "message": "Добавил обе."}])
    reply = agent.start("c2", "закажи пепперони и маргариту на pizza.test", router,
                        user_id="A")
    check("C2: «пепперони и маргарита» — одинаковая кнопка у двух товаров, "
          "обе добавлены (ключ — товар, не текст кнопки)",
          [a["idx"] for a in cc.executed] == [1, 2]
          and "already added" not in "".join(router.prompts))
    # План: обе в корзине — сначала «что-нибудь ещё?», «нет» — done модели
    check("план: обе позиции в корзине — «что-нибудь ещё?» до хода модели",
          "Добавить что-нибудь ещё?" in reply and "Добавил обе." not in reply)
    reply = agent.feed("c2", "нет", router, user_id="A")
    check("C3: корзина сходится с брифом — done принят, к итогу факт корзины",
          "Добавил обе." in reply and "Корзина на сайте: 2 шт., 816" in reply
          and "проверить не смог" not in reply)
    cc = ShopCC()
    agent = TaskAgent(cc)
    agent.slot_router = SlotRouter([{"items": [{"name": "Пепперони"}]}])
    router = ScriptedRouter([{"action": "click", "n": 2},
                             {"action": "done", "message": "Добавил Том ям."},
                             {"action": "done", "message": "Добавил Том ям."}])
    reply = agent.start("c3", "закажи пепперони на pizza.test", router, user_id="A")
    check("C3: в корзине не то, что просили — модели «NOT finished», "
          "человеку — пометка и факт корзины",
          "NOT finished — the user asked for Пепперони" in router.prompts[2]
          and "проверить не смог" in reply and "Корзина на сайте: 1 шт." in reply)

    # C4: размер не назван — не выбирать за человека; назван — выбран он.
    # Вопрос о размере задаёт код (вопросы о товаре до «В корзину» одним
    # сообщением, _item_questions), а не модель по отбою: модель
    # спрашивала размер, потом код — добавки, по одному
    cc = ShopCC()
    cc.sizes = [("25 см", 1), ("30 см", 0), ("35 см", 0)]
    agent = TaskAgent(cc)
    agent.slot_router = SlotRouter([
        {"items": [{"name": "Пепперони"}]}, {},
        {"items": [{"name": "Пепперони", "size": "30 см"}]}])
    router = ScriptedRouter([{"action": "click", "n": 1},
                             {"action": "click", "n": 5},
                             {"action": "click", "n": 1},
                             {"action": "done", "message": "Готово."}])
    reply = agent.start("c4a", "закажи пепперони на pizza.test", router, user_id="A")
    check("C4: размер не назван — «В корзину» не нажато, человеку — вопрос "
          "о товаре с размерами со страницы",
          not cc.executed and len(router.prompts) == 1
          and "Перед тем как положить «Пепперони» в корзину" in reply
          and "Какой размер: 25 см, 30 см, 35 см? Сейчас выбран 25 см." in reply)
    reply = agent.feed("c4a", "30 см", router, user_id="A")
    check("C4: ответ «30 см» — выбран 30, потом «В корзину» (вопрос о товаре "
          "второй раз не задан), затем «что-нибудь ещё?»",
          [a["idx"] for a in cc.executed] == [11, 1]
          and "Добавить что-нибудь ещё?" in reply
          and "Перед тем как положить" not in reply)
    reply = agent.feed("c4a", "нет", router, user_id="A")
    check("C4: «нет» на «что-нибудь ещё?» — done модели", "Готово." in reply)
    cc = ShopCC()
    cc.sizes = [("25 см", 1), ("30 см", 0), ("35 см", 0)]
    agent = TaskAgent(cc)
    agent.slot_router = SlotRouter([{"items": [{"name": "Пепперони"}]}])
    router = ScriptedRouter([{"action": "click", "n": 1},
                             {"action": "click", "n": 1},
                             {"action": "done", "message": "Готово."}])
    agent.start("c4c", "закажи пепперони на pizza.test", router, user_id="A")
    agent.feed("c4c", "как есть", router, user_id="A")
    reply = agent.feed("c4c", "нет", router, user_id="A")
    check("C4: «как есть» на вопрос с размерами — выбранный (25 см), без "
          "второго вопроса о размере",
          [a["idx"] for a in cc.executed] == [1] and "Готово." in reply
          and "ask the user which size" not in "".join(router.prompts))
    cc = ShopCC()
    cc.sizes = [("25 см", 1), ("30 см", 0), ("35 см", 0)]
    agent = TaskAgent(cc)
    agent.slot_router = SlotRouter([{"items": [{"name": "Пепперони",
                                                "size": "30 см"}]}])
    router = ScriptedRouter([{"action": "click", "n": 1},
                             {"action": "click", "n": 5},
                             {"action": "click", "n": 1},
                             {"action": "done", "message": "Готово."}])
    agent.start("c4b", "закажи пепперони 30 см на pizza.test", router, user_id="A")
    reply = agent.feed("c4b", "нет", router, user_id="A")
    check("C4: выбран не тот размер — сначала выбрать названный, потом "
          "«В корзину»", "select 30 см first" in router.prompts[1]
          and [a["idx"] for a in cc.executed] == [11, 1] and "Готово." in reply)

    # E9: «Додо, но не пепперони» — по слотам: сайт тот же, пепперони — в
    # исключения, адрес тот же
    cc = FakeCC()
    agent = TaskAgent(cc)
    slots = SlotRouter([
        {"site": "Додо", "items": [{"name": "Пепперони", "size": "30 см"}],
         "address": "Ленина 5"},
        {"site": "Додо", "remove_items": ["Пепперони"],
         "exclude": ["пепперони"]}])
    agent.slot_router = slots
    router = ScriptedRouter([
        {"action": "ask", "question": "Как в прошлый раз: пепперони 30 см на "
                                       "Додо?"},
        {"action": "fail", "message": "стоп"}])
    agent.start("e9", "закажи пепперони 30 см в додо на Ленина 5", router,
                user_id="A")
    agent.feed("e9", "Додо, но не пепперони", router, user_id="A")
    br = agent._runs.get("e9", {}).get("brief") or agent.__dict__.get(
        "_finished", {}).get("e9", {}).get("run", {}).get("brief")
    check("E9: «Додо, но не пепперони» — сайт и адрес остались, пепперони "
          "с размером — в исключениях",
          br and br["site"]["value"] == "Додо"
          and br["address"]["value"] == "Ленина 5" and not br["items"]
          and br["exclude"] == ["пепперони"])
    check("E9: разбор — отдельный вызов: вопрос, ответ и бриф на входе",
          "Додо, но не пепперони" in slots.prompts[1]
          and "Как в прошлый раз" in slots.prompts[1]
          and "Ленина 5" in slots.prompts[1]
          and "the brief" in router.prompts[-1] and "not wanted: пепперони"
          in router.prompts[-1])

    # E5: «как в прошлый раз» — последний УСПЕШНЫЙ заказ, бриф из памяти
    mem_e5 = Path(tempfile.mkdtemp(prefix="task_mem_e5_")) / "task_memory.json"
    mem_e5.write_text(json.dumps({"e5": [
        {"ts": 1, "goal": "закажи пиццу", "sites": ["dodopizza.ru"], "qa": [],
         "result": "Дошёл до оплаты", "ok": True,
         "brief": {"site": "dodopizza.ru",
                   "items": [{"name": "Пепперони", "size": "30 см",
                              "options": [], "qty": 1}]}},
        {"ts": 2, "goal": "закажи роллы", "sites": ["sushi.test"], "qa": [],
         "result": "cancelled by the user", "ok": False}]},
        ensure_ascii=False), encoding="utf-8")
    agent = TaskAgent(FakeCC(), memory_path=mem_e5)
    router = ScriptedRouter([{"action": "fail", "message": "стоп"}])
    agent.start("e5", "закажи как в прошлый раз", router, user_id="A")
    p5 = router.prompts[0]
    check("E5: «как в прошлый раз» без предмета — последний успешный заказ, "
          "отменённый не предлагается",
          "dodopizza.ru" in p5 and "sushi.test" not in p5
          and "item: Пепперони, size 30 см" in p5 and "from memory" in p5)
    from app.features.task_agent import _goal_stems as _gs
    check("E5: тема — по предмету: «заказать пиццу» ~ «закажи пиццу», "
          "«как в прошлый раз» темы не делает",
          _gs("заказать пиццу") == _gs("закажи пиццу") == {"пицц"}
          and not _gs("закажи как в прошлый раз"))

    # expect: ожидание модели проверяет код
    cc = ShopCC(works=False)
    cc.n_added = 1
    agent = TaskAgent(cc)
    router = ScriptedRouter([
        {"action": "click", "n": 1, "expect": "the cart count grows"},
        {"action": "fail", "message": "стоп"}])
    agent.start("ex", "закажи пепперони на pizza.test", router, user_id="A")
    check("F: expect — код сверил ожидание «корзина вырастет» со страницей",
          'expected "the cart count grows": NOT seen' in router.prompts[1])

    # ── D2: find → click по номеру СВЕЖЕЙ разметки; D3: back во вкладке агента
    goal_calls = []

    def _fake_goal(host, text, tab_id=None):
        # Каждая съёмка ставит новые метки (общий снимок их стирает)
        base = 500 + 100 * len(goal_calls)
        goal_calls.append(text)
        return "https://pizza.test/menu", [
            {"idx": base, "tag": "button", "role": "button",
             "text": "Добавить соус"}]
    saved_goal = _ba.snapshot_for_goal
    _ba.snapshot_for_goal = _fake_goal
    try:
        cc = FakeCC()
        cc.url = "https://pizza.test/menu"
        agent = TaskAgent(cc)
        router = ScriptedRouter([{"action": "find", "text": "соус"},
                                 {"action": "click", "n": 1},
                                 {"action": "back"},
                                 {"action": "fail", "message": "стоп"}])
        agent.start("d2", "добавь соус", router, user_id="A")
        clicks = [a for a in cc.executed if a.get("kind") == "click"]
        check("D2: find → click — номер из пересъёмки места (метки живые)",
              len(goal_calls) >= 2 and clicks and clicks[0]["idx"] == 600
              and clicks[0]["element"] == "Добавить соус")
        backs = [a for a in cc.executed if a.get("kind") == "tab_op"]
        check("D3: back — во вкладке агента (tab_id снимка), не в видимой",
              backs and backs[0].get("tab_id") == 1
              and backs[0].get("origin") == "task")
    finally:
        _ba.snapshot_for_goal = saved_goal

    # ── D1/D7/A3/A5: настоящий _parse_snapshot сохраняет флаги JS ──
    raw_snap = json.dumps({"url": "https://pizza.test/menu", "vw": 1000,
                           "items": [
        {"idx": 1, "tag": "button", "role": "button", "text": "30 см",
         "on": 1},
        {"idx": 2, "tag": "button", "role": "button", "text": "35 см",
         "on": 0},
        {"idx": 3, "tag": "button", "role": "button", "text": "Далее",
         "on": -1, "sub": 1, "fm": 1},
        {"idx": 4, "tag": "button", "role": "button", "text": "Оформить",
         "dis": 1},
        {"idx": 5, "tag": "input", "role": "searchbox", "text": "Поиск",
         "ed": 1, "q": 1, "qs": 1}]}, ensure_ascii=False)
    _u, parsed = _ba._parse_snapshot(raw_snap)
    by = {it["idx"]: it for it in parsed}
    check("D1: _parse_snapshot сохраняет on (1/0/-1), а не теряет",
          by[1]["on"] == 1 and by[2]["on"] == 0 and by[3]["on"] == -1)
    check("A3/D7/A5: sub, fm, dis, qs доходят до агента",
          by[3]["sub"] and by[3]["fm"] and by[4]["dis"] and by[5]["qs"]
          and not by[1]["sub"])
    saved_run_js = _ba._run_js
    _ba._run_js = lambda host, js, tab_id=None: json.dumps(
        {"url": "https://pizza.test/menu", "items": [
            {"idx": 900, "tag": "button", "role": "button",
             "text": "Халапеньо", "on": 1}]}, ensure_ascii=False)
    try:
        _u, goal_items = _ba.snapshot_for_goal("pizza.test", "халапеньо")
    finally:
        _ba._run_js = saved_run_js
    check("D1: snapshot_for_goal тоже сохраняет on",
          goal_items and goal_items[0]["on"] == 1)
    cc = FakeCC()
    agent = TaskAgent(cc)
    p_on = agent._elem_line(1, by[1]) + agent._elem_line(2, by[2])
    check("D1: модель видит выбранность опции", "selected/on" in p_on
          and "not selected/off" in p_on)

    # ── D6: маска телефона — сравнение по цифрам, ошибка не «ок» ──
    check("D6: телефон сравнивается по последним 10 цифрам",
          _ba._phone_same("9991234567", "+7 (999) 123-45-67") is True
          and _ba._phone_same("+79991234567", "+7 (799) 912-34-56") is False
          and _ba._phone_same("Ленина 5", "Ленина 5") is None)

    class MaskCC(FakeCC):
        def execute(self, act, chat_id="", router=None):
            self.executed.append(dict(act))
            return False, ("значение не совпало: в поле 11 цифр, а вводилось "
                           "11 — введи номер без +7/8")
    run_d6 = {"history": [], "page_state": None}
    TaskAgent(MaskCC())._execute(run_d6, "d6", None, {
        "kind": "type", "idx": 11, "text": "+79991234567",
        "host": "pizza.test"}, 'type "***(12)" into "Телефон"')
    check("D6: не совпало значение поля — модели «failed», а не «ok»/«done»",
          run_d6["history"][-1].startswith('type "***(12)" into "Телефон" → '
                                           "failed: the field now holds")
          and "7999" not in run_d6["history"][-1])

    # ── D10: окно открыто — листается окно, а не страница под ним ──
    class ModalCC(FakeCC):
        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            return "https://pizza.test/menu", "pizza.test", [
                {"idx": 1, "tag": "button", "role": "button", "text": "30 см",
                 "md": True}], 1, None
    container_scrolls.clear()
    router = ScriptedRouter([{"action": "scroll"},
                             {"action": "fail", "message": "стоп"}])
    TaskAgent(ModalCC()).start("d10", "x", router, user_id="A")
    check("D10: открыто окно — листание окна (контейнер), модели — "
          "«scroll down (the open dialog)»",
          len(container_scrolls) == 1
          and "scroll down (the open dialog) → ok" in router.prompts[1])

    # ── E2: явный запуск — только с разделителем ──
    for t in ("задача по математике на завтра", "агент 007 снова на связи",
              "task manager", "задание было сложное"):
        check(f"E2: не запуск: «{t}»", parse_task_request(t) is None)
    check("E2: «задача:закажи пиццу» (без пробела) и «Коннор, задача: …» — "
          "запуск", parse_task_request("задача:закажи пиццу") == "закажи пиццу"
          and parse_task_request("Коннор, задача: закажи пиццу",
                                 {"Коннор"}) == "закажи пиццу")

    # ── E4: «ок» после успешного итога/оплаты задачу не возобновляет ──
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent = TaskAgent(cc)
    agent.start("e4", "закажи пиццу на pizza.test", ScriptedRouter([
        {"action": "click", "n": 4}]), user_id="A")      # оплата → человеку
    check("E4: «ок» после передачи оплаты — не возобновление",
          not agent.reopen("e4", "ок", user_id="A"))
    agent = TaskAgent(FakeCC())
    agent.start("e4b", "найди скидки", ScriptedRouter([
        {"action": "fail", "message": "сайт не открылся"}]), user_id="A")
    agent.cancel("e4b")
    check("E4: после отмены (выход из режима) «да» старую задачу не "
          "возобновляет", not agent.reopen("e4b", "да", user_id="A"))
    agent = TaskAgent(FakeCC())
    agent.start("e4c", "найди скидки", ScriptedRouter([
        {"action": "fail", "message": "сайт не открылся"}]), user_id="A")
    check("E4: после провала «да» — возобновление (как раньше)",
          agent.reopen("e4c", "да", user_id="A"))

    # ── E6: явная посторонняя команда при ждущем прогоне ──
    cc = FakeCC()
    agent = TaskAgent(cc)
    agent.start("e6", "закажи пиццу", ScriptedRouter([
        {"action": "ask", "question": "Какую пиццу?"}]), user_id="A")
    q6 = agent.ask_switch("e6", "открой ютуб", user_id="A")
    check("E6: «открой ютуб» при вопросе агента — «бросить задачу?»",
          q6 and "Бросить её" in q6 and "открой ютуб" in q6)
    r6 = agent.feed("e6", "нет", ScriptedRouter([]), user_id="A")
    check("E6: «нет» — задача продолжается, прежний вопрос снова ждёт ответа",
          "e6" in agent._runs and agent._runs["e6"]["awaiting"]["kind"] == "ask"
          and "Какую пиццу?" in r6 and agent.pop_switch("e6") is None)
    agent.ask_switch("e6", "открой ютуб", user_id="A")
    agent.feed("e6", "да", ScriptedRouter([]), user_id="A")
    check("E6: «да» — задача снята, команда отдана боту (pop_switch)",
          "e6" not in agent._runs and agent.pop_switch("e6") == "открой ютуб")
    check("E6: чужой участник «бросить задачу?» не вызывает",
          (lambda ag: (ag.start("e6b", "x", ScriptedRouter([
              {"action": "ask", "question": "Какую?"}]), user_id="A"),
              ag.ask_switch("e6b", "открой ютуб", user_id="B"))[1])(
                  TaskAgent(FakeCC())) is None)

    # ── A3: корзина/оформление — submit формы, кнопка без подписи, цена ──
    from app.features.computer_control import ComputerControlManager as _CM3
    # Фаза оформления по адресу, но не приватная страница (иначе промпт ушёл
    # бы локальной модели — тест от Ollama не зависит)
    co_url = "https://shop.test/order/delivery"
    co_items = [
        {"idx": 1, "tag": "button", "role": "button", "text": "Далее",
         "sub": True, "fm": True},
        {"idx": 2, "tag": "button", "role": "button", "text": ""},
        {"idx": 3, "tag": "div", "role": "button", "text": "1 299 ₽"},
        {"idx": 4, "tag": "button", "role": "button", "text": "Оформить",
         "dis": True},
        {"idx": 5, "tag": "button", "role": "button", "text": "Изменить"}]
    for n, what in ((1, "submit формы «Далее»"), (2, "иконка без подписи"),
                    (3, "подпись-цена «1 299 ₽»")):
        cc = real_cc({co_url: [dict(x) for x in co_items]}, url=co_url)
        agent = TaskAgent(cc)
        reply = agent.start(f"a3-{n}", "x", ScriptedRouter([
            {"action": "click", "n": n}]), user_id="A")
        aw = (agent._runs.get(f"a3-{n}") or {}).get("awaiting") or {}
        check(f"A3: оформление, {what} — вопрос «да/нет», клика нет, "
              "гейт требует токен",
              "Делаю?" in reply and not cc.dispatched
              and aw.get("kind") == "confirm"
              and _CM3.confirm_reason(aw["act"]) == "force_confirm")
        agent.feed(f"a3-{n}", "да", ScriptedRouter([
            {"action": "fail", "message": "стоп"}]), user_id="A")
        check(f"A3: {what} после «да» — нажато (токен гейта)",
              [a["idx"] for a in cc.dispatched] == [n])
    cc = real_cc({"https://shop.test/menu": [dict(co_items[0])]},
                 url="https://shop.test/menu")
    agent = TaskAgent(cc)
    agent.start("a3m", "x", ScriptedRouter([{"action": "click", "n": 1}]),
                user_id="A")
    check("A3: вне корзины/оформления submit «Далее» — без вопроса",
          [a["idx"] for a in cc.dispatched] == [1])
    # D7: неактивный контрол не нажимается
    cc = real_cc({co_url: [dict(x) for x in co_items]}, url=co_url)
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 4},
                             {"action": "fail", "message": "стоп"}])
    agent.start("d7", "x", router, user_id="A")
    check("D7: disabled — не нажат, модели «disabled», в списке пометка",
          not cc.dispatched and "is disabled" in router.prompts[1]
          and "Оформить — disabled" in router.prompts[0])

    # ── A5: клавиши в гейте, строго поисковое поле, search_typed ──
    CR5 = _CM3.confirm_reason
    check("A5: гейт — Enter/Space/Tab не от команды человека — «да»",
          all(CR5({"kind": "key", "key": k, "origin": o}) == "commit"
              for k in ("Enter", "Space", "Tab")
              for o in ("task", "scenario", "intent_llm", None)))
    check("A5: гейт — команда человека (fast), Escape/стрелки, "
          "Enter агента после ввода в поиск (search_enter) — без токена",
          CR5({"kind": "key", "key": "Enter", "origin": "fast"}) is None
          and CR5({"kind": "key", "key": "Escape", "origin": "task"}) is None
          and CR5({"kind": "key", "key": "Enter", "origin": "task",
                   "search_enter": True}) is None)
    s_url = "https://shop.test/"
    s_items = [
        {"idx": 1, "tag": "input", "role": "searchbox", "text": "Поиск",
         "ed": True, "q": True, "qs": True},
        {"idx": 2, "tag": "input", "role": "textbox", "text": "Поиск адреса",
         "ed": True, "q": True, "qs": False, "fm": True}]

    def a5(reply_objs, chat, fail_type=False):
        class A5CC(type(real_cc({}))):
            pass
        cc = real_cc({s_url: [dict(x) for x in s_items]}, url=s_url)
        if fail_type:
            orig = cc._dispatch

            def _d(action, router=None):
                if action["kind"] == "type":
                    raise RuntimeError("ввод не выполнен: поле пропало")
                return orig(action, router)
            cc._dispatch = _d
        agent = TaskAgent(cc)
        reply = agent.start(chat, "x", ScriptedRouter(reply_objs), user_id="A")
        return cc, agent, reply

    cc, agent, reply = a5([{"action": "type", "n": 1, "text": "пицца",
                            "submit": True},
                           {"action": "fail", "message": "стоп"}], "a5a")
    check("A5: ввод+Enter в строго поисковое поле — без «да»",
          [a["kind"] for a in cc.dispatched] == ["type"]
          and cc.dispatched[0].get("field_safe"))
    cc, agent, reply = a5([{"action": "type", "n": 2, "text": "Ленина",
                            "submit": True}], "a5b")
    check("A5: «Поиск адреса» (q по подписи, в форме) + Enter — «да»",
          not cc.dispatched and "Делаю?" in reply)
    cc, agent, reply = a5([{"action": "type", "n": 1, "text": "пицца"},
                           {"action": "key", "key": "Enter"}], "a5c",
                          fail_type=True)
    check("A5: ввод не удался — следующий Enter не считается поиском (вопрос)",
          not cc.dispatched and "Делаю?" in reply)
    cc, agent, reply = a5([{"action": "type", "n": 1, "text": "пицца"},
                           {"action": "key", "key": "Enter"},
                           {"action": "fail", "message": "стоп"}], "a5d")
    check("A5: Enter сразу после удачного ввода в поиск — без «да», "
          "гейт пропускает по search_enter",
          [a["kind"] for a in cc.dispatched] == ["type", "key"]
          and cc.dispatched[1].get("search_enter"))

    # ── A6: подпись сверяется в момент клика ──
    check("A6: шагу агента — expect подписи, команде человека — нет",
          _CM3._expect_label({"origin": "task", "element": "Далее"})
          == {"expect": "Далее"}
          and _CM3._expect_label({"origin": "pending", "pending_from": "task",
                                  "element": "Далее"}) == {"expect": "Далее"}
          and _CM3._expect_label({"origin": "fast", "element": "Далее"}) == {})
    from app.features import browser_actions as _ba6
    saved6 = (_ba6.click_tagged, _ba6.page_urls, _ba6.follow_popup)
    got6 = []
    try:
        _ba6.click_tagged = lambda h, i, tab_id=None, **kw: got6.append(kw)
        _ba6.page_urls = lambda *a, **k: []
        _ba6.follow_popup = lambda *a, **k: None
        m6 = _CM3(context="a6", config={"confirm": False},
                  base_dir=Path(tempfile.mkdtemp(prefix="ta_a6_")))
        m6._remember_tab = lambda *a, **k: None
        m6._dispatch({"kind": "click", "idx": 5, "element": "Далее",
                      "host": "shop.test", "origin": "task"})
        m6._dispatch({"kind": "click", "idx": 5, "element": "Далее",
                      "host": "shop.test", "origin": "fast"})
        check("A6: _dispatch передаёт браузеру ожидаемую подпись (агент)",
              got6 == [{"expect": "Далее"}, {}])
    finally:
        _ba6.click_tagged, _ba6.page_urls, _ba6.follow_popup = saved6
    cc = real_cc({s_url: [{"idx": 7, "tag": "button", "role": "button",
                           "text": "Далее"}]}, url=s_url)

    def _changed(action, router=None):
        raise _ba6.BrowserUnavailable(_ba6.LABEL_CHANGED)
    cc._dispatch = _changed
    agent = TaskAgent(cc)
    router = ScriptedRouter([{"action": "click", "n": 1},
                             {"action": "fail", "message": "стоп"}])
    agent.start("a6", "x", router, user_id="A")
    check("A6: подпись сменилась в момент клика — модели «NOT performed: "
          "label changed», не «ok»",
          "label changed before the click" in router.prompts[1]
          and "→ ok" not in router.prompts[1])

    # ── A7/A8: open алиаса приложения/задачи; open по имени без поисковика ──
    class AppCC(FakeCC):
        def resolve_url(self, token):
            return None

        def resolve(self, name, web_search=True):
            self.web_search = web_search
            return ({"kind": "app", "key": "терминал", "value": "Terminal"}
                    if name == "терминал" else None)
    cc = AppCC()
    agent = TaskAgent(cc)
    reply = agent.start("a7", "x", ScriptedRouter([
        {"action": "open", "target": "терминал"}]), user_id="A")
    aw = agent._runs["a7"]["awaiting"]
    check("A7: open алиаса приложения — вопрос «да/нет», не запущено; гейт "
          "требует токен", not cc.executed and aw["kind"] == "confirm"
          and _CM3.confirm_reason(dict(aw["act"])) == "force_confirm"
          and _CM3.confirm_reason({"kind": "app", "value": "Terminal",
                                   "origin": "task"}) == "force_confirm")
    check("A8: open по имени — resolve без поисковика (web_search=False)",
          cc.web_search is False)
    router = ScriptedRouter([{"action": "open", "target": "додо пицца"},
                             {"action": "fail", "message": "стоп"}])
    agent = TaskAgent(AppCC())
    agent.start("a8", "x", router, user_id="A")
    check("A8: неизвестное имя — модели «search for it first», "
          "в Google не ушло", "search for it first" in router.prompts[1])

    # ── A9: приватная страница — carry, логи, аудит, группа ──
    import logging as _lg9

    class _Cap(_lg9.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, rec):
            self.lines.append(rec.getMessage())
    cap = _Cap()
    _lg9.getLogger().addHandler(cap)
    old_level = _lg9.getLogger().level
    _lg9.getLogger().setLevel(_lg9.INFO)
    try:
        priv_url = "https://lk.clinic.test/account/appointments"
        cc = real_cc({priv_url: [{"idx": 1, "tag": "button", "role": "button",
                                  "text": "Записаться к Иванову 15:30"}]},
                     url=priv_url)
        cc.is_private_page = lambda h: "lk.clinic" in str(h)
        agent = TaskAgent(cc)
        notes9 = []
        agent.on_private_text = lambda t, h, w: notes9.append(t)
        agent.is_group = lambda chat: chat.startswith("-")
        loc9 = ScriptedRouter([{"action": "click", "n": 1}])
        loc9.local_ok = True
        _P9 = __import__("app.features.cc_privacy", fromlist=["x"])
        orig9 = _P9.PrivateRouter.get_response
        _P9.PrivateRouter.get_response = lambda self, msgs, **kw: \
            loc9.get_response(msgs, **kw)
        try:
            r1 = agent.start("-100", "x", ScriptedRouter([]), user_id="A")
            check("A9d: группа — вопрос с приватной страницы заглушкой",
                  "Иванову" not in r1 and "приватной странице" in r1)
            loc9.replies = [{"action": "fail", "message": "стоп"}]
            agent.feed("-100", "да", ScriptedRouter([]), user_id="A")
            check("A9d: группа — «да» вслепую на скрытый шаг не исполняет его "
                  "(шаг делает человек)", not cc.dispatched)
            # Личный чат: шаг после «да» исполнен, строка — заглушкой в историю
            loc9.replies = [{"action": "click", "n": 1}]
            agent.start("p9", "x", ScriptedRouter([]), user_id="A")
            loc9.replies = [{"action": "fail", "message": "стоп"}]
            r2 = agent.feed("p9", "да", ScriptedRouter([]), user_id="A")
            check("A9a: строка шага после «да» на приватной странице — в "
                  "историю заглушкой (on_private_text)",
                  [a["idx"] for a in cc.dispatched] == [1]
                  and any("Иванову" in t for t in notes9))
        finally:
            _P9.PrivateRouter.get_response = orig9
        log9 = "\n".join(cap.lines)
        check("A9b: лог «Выполнено/шаг» без подписи приватной страницы",
              "Иванову" not in log9 and "Выполнено" in log9)
        aud9 = (cc.base_dir / "audit.jsonl").read_text(encoding="utf-8")
        check("A9c: в аудите подпись приватной страницы — маской",
              "Иванову" not in aud9 and "***(" in aud9)
    finally:
        _lg9.getLogger().removeHandler(cap)
        _lg9.getLogger().setLevel(old_level)

    # ── B2–B4: отмена, «не надо», «Коннор, да» в каждом виде ожидания ──
    names = {"коннор", "Connor"}

    def waiting(kind, chat):
        # Прогон в нужном ожидании: ask — вопрос модели, confirm — клик
        # «Отправить», continue — пауза бюджета хода
        cc = FormCC()
        agent = TaskAgent(cc)
        first = ({"action": "ask", "question": "Какой адрес?"} if kind == "ask"
                 else {"action": "click", "n": 1})
        agent.start(chat, "x", ScriptedRouter([first]), user_id="A")
        if kind == "continue":
            agent._runs[chat]["awaiting"] = agent._await_continue(
                agent._runs[chat])
        return cc, agent

    for kind in ("ask", "confirm", "continue"):
        for word in ("стой", "abort", "останови", "отмени всё", "Коннор, стоп"):
            cc, agent = waiting(kind, f"s-{kind}")
            agent.feed(f"s-{kind}", word, ScriptedRouter([]), user_id="A",
                       names=names)
            check(f"B3: «{word}» при ожидании {kind} — задача отменена",
                  f"s-{kind}" not in agent._runs and not cc.executed)
        cc, agent = waiting(kind, f"n-{kind}")
        router = ScriptedRouter([{"action": "fail", "message": "стоп"}])
        agent.feed(f"n-{kind}", "не надо", router, user_id="A", names=names)
        if kind == "continue":
            check("B3: «не надо» на «продолжать?» — отмена",
                  "n-continue" not in agent._runs)
        elif kind == "confirm":
            check("B3: «не надо» на «да/нет» — отказ от шага, задача жива",
                  not cc.executed and router.prompts
                  and "the user declined" in router.prompts[0])
        else:
            check("B3: «не надо» на вопрос — ответ «нет», задача не отменена",
                  router.prompts and "A: не надо" in router.prompts[0])
        cc, agent = waiting(kind, f"y-{kind}")
        router = ScriptedRouter([{"action": "fail", "message": "стоп"}])
        agent.feed(f"y-{kind}", "Коннор, да", router, user_id="A", names=names)
        if kind == "confirm":
            check("B4: «Коннор, да» на «да/нет» — шаг исполнен",
                  [a.get("element") for a in cc.executed] == ["Отправить"])
        else:
            check(f"B4: «Коннор, да» при ожидании {kind} — прогон продолжен",
                  bool(router.prompts) and not cc.executed)
    check("B4: reopen по «Коннор, да» на итог",
          (lambda ag: (ag.__dict__.setdefault("_finished", {}).update(
              {"f1": {"run": {"goal": "x", "turn_user": "A", "qa": [],
                              "history": []}, "ts": _time.time(),
                      "text": "Дошёл до корзины. Продолжить?"}}),
              ag.reopen("f1", "Коннор, да", user_id="A", names=names))[1])(
                  TaskAgent(FormCC())))

    # B2: «стоп» (бот: cc_turn_enter → cancel), пока исполняется шаг,
    # подтверждённый «да», — прогон занят, дальше не идёт
    class StopDuringCC(FormCC):
        seen_busy = None

        def execute(self, act, chat_id="", router=None):
            StopDuringCC.seen_busy = agent.busy(chat_id)
            if agent.busy(chat_id):
                agent.cancel(chat_id)  # то, что делает cc_turn_enter
            return super().execute(act, chat_id, router)

    cc = StopDuringCC()
    agent = TaskAgent(cc)
    agent.start("b2", "x", ScriptedRouter([{"action": "click", "n": 1}]),
                user_id="A")
    router = ScriptedRouter([{"action": "click", "n": 2}])
    r = agent.feed("b2", "да", router, user_id="A")
    check("B2: во время подтверждённого шага прогон занят (busy)",
          StopDuringCC.seen_busy is True)
    check("B2: «стоп» во время подтверждённого шага — дальше ни шага, "
          "ответ — отмена", not router.prompts and "b2" not in agent._runs
          and "бросаю задачу" in r and len(cc.executed) == 1)
    check("B2: отменённый прогон не возобновляется «да» на итог",
          "b2" not in agent.__dict__.get("_finished", {})
          and not agent.reopen("b2", "да", user_id="A"))

    # B2: «стоп» во время последнего шага хода, который кончился вопросом, —
    # ответ отменой, а не вопросом
    class StopOnStep(FormCC):
        def execute(self, act, chat_id="", router=None):
            agent._runs[chat_id]["cancel"] = True
            return super().execute(act, chat_id, router)

    cc = StopOnStep()
    cc.url = "https://shop.test/"
    agent = TaskAgent(cc)
    r = agent.start("b2b", "x", ScriptedRouter([
        '{"action":"type","n":5,"text":"пицца"}']), user_id="A")
    check("B2: отмена во время шага — ответ «бросаю», не вопрос модели",
          "бросаю задачу" in r and "b2b" not in agent._runs)

    # ── П.4: одно подтверждение коммита — факты со страницы, итог перечитан ──
    class CheckoutCC(FakeCC):
        total = "816"

        def __init__(self):
            super().__init__()
            self.url = "https://shop.test/order/confirm"

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            return self.url, "shop.test", [
                {"idx": 70, "tag": "button", "role": "button",
                 "text": "Оформить заказ"}], 1, None

        def execute(self, act, chat_id="", router=None):
            self.executed.append(dict(act))
            if act.get("kind") == "read":
                return True, (f"Ваш заказ\nПепперони 30 см 408 ₽\n"
                              f"Итого: {self.total} ₽")
            return True, ""
    cc = CheckoutCC()
    agent = TaskAgent(cc)
    agent.slot_router = SlotRouter([{
        "items": [{"name": "Пепперони", "size": "30 см", "qty": 2}],
        "address": "Ленина 5", "payment": "наличными"}])
    q4 = agent.start("p4", "закажи 2 пепперони 30 см на shop.test", ScriptedRouter([
        {"action": "click", "n": 1}]), user_id="A")
    check("П.4: вопрос о коммите — состав, итог со страницы, адрес, оплата",
          "Пепперони 30 см ×2" in q4 and "итого 816" in q4
          and "адрес: Ленина 5" in q4 and "оплата: наличными" in q4
          and "Нажать «Оформить заказ»?" in q4)
    cc.total = "1 632"
    r4 = agent.feed("p4", "да", ScriptedRouter([]), user_id="A")
    check("П.4: сумма изменилась до клика — подтверждение аннулировано, "
          "вопрос заново с новой суммой, клика нет",
          "Сумма на странице изменилась" in r4 and "итого 1 632" in r4
          and not [a for a in cc.executed if a.get("kind") == "click"])
    agent.feed("p4", "да", ScriptedRouter([
        {"action": "done", "message": "Заказ оформлен"}]), user_id="A")
    check("П.4: «да» на новую сумму — нажато",
          [a["idx"] for a in cc.executed if a.get("kind") == "click"] == [70])

    # ── Мелочи: аудит — номер прогона, вопросы и поиск ──
    cc = real_cc({"https://shop.test/": [
        {"idx": 1, "tag": "a", "role": "link", "text": "Меню"}]},
        url="https://shop.test/")
    agent = TaskAgent(cc)
    agent.start("au", "x", ScriptedRouter([
        {"action": "click", "n": 1},
        {"action": "ask", "question": "Какую пиццу? Мой номер +7 913 123-45-67"}]),
        user_id="A")
    recs = [json.loads(x) for x in (cc.base_dir / "audit.jsonl").read_text(
        encoding="utf-8").splitlines()]
    runs_au = {r.get("task_run") for r in recs}
    check("Мелочи: аудит — клик и вопрос агента с одним номером прогона, "
          "вопрос без телефона",
          len(runs_au) == 1 and None not in runs_au
          and [r["kind"] for r in recs] == ["click", "task_ask"]
          and "913" not in json.dumps(recs, ensure_ascii=False))

    # ── Находки красной команды по A+B ──
    # №1: «ОК»/«Да»/«Продолжить» в окне сайта — по тексту окна
    for lab, ctx, want in (
            ("ОК", "Подтвердите заказ на 1 299 ₽ ОК Отмена", "ask"),
            ("Да", "Удалить аккаунт навсегда? Да Нет", "ask"),
            ("Продолжить", "Списать 1 299 ₽ с карты *4242? Продолжить", "pay"),
            ("Да", "Ваш город Москва? Да Выбрать другой", "click")):
        cc = real_cc({"https://shop.test/": [
            {"idx": 7, "tag": "button", "role": "button", "text": lab,
             "ctx": ctx, "md": True}]}, url="https://shop.test/")
        agent = TaskAgent(cc)
        reply = agent.start(f"rt1-{want}-{lab}", "x", ScriptedRouter([
            {"action": "click", "n": 1}]), user_id="A")
        got = ("pay" if "дальше сам" in reply else
               "ask" if "Делаю?" in reply else "click")
        check(f"№1: «{lab}» в окне «{ctx[:30]}…» → {want}",
              got == want and (bool(cc.dispatched) == (want == "click"))
              and (want != "ask" or ctx[:20] in reply))
    from app.features.computer_control import ComputerControlManager as _CMr
    check("№1: гейт — «ОК» агента с текстом окна про заказ требует «да»",
          _CMr.confirm_reason({"kind": "click", "element": "ОК",
                               "origin": "task", "context":
                               "Подтвердите заказ на 1 299 ₽"}) == "commit")

    # №2: подтверждённый «Удалить» у Маргариты не переходит на Пепперони
    del_items = [
        {"idx": 1, "tag": "button", "role": "button", "text": "Удалить",
         "ctx": "Маргарита 30 см 408 ₽"},
        {"idx": 2, "tag": "button", "role": "button", "text": "Удалить",
         "ctx": "Пепперони 30 см 408 ₽"}]
    cc = real_cc({"https://shop.test/order/items": [dict(x) for x in del_items]},
                 url="https://shop.test/order/items")
    agent = TaskAgent(cc)
    q2 = agent.start("rt2", "x", ScriptedRouter([{"action": "click", "n": 1}]),
                     user_id="A")
    check("№2: в вопросе назван блок («Маргарита…»)", "Маргарита" in q2)
    cc.pages["https://shop.test/order/items"] = [dict(del_items[1], idx=5)]
    agent.feed("rt2", "да", ScriptedRouter([
        {"action": "fail", "message": "стоп"}]), user_id="A")
    check("№2: своя кнопка пропала — «да» не переходит на чужой блок",
          not cc.dispatched)

    # №3: паспорт, кодовое слово — секрет целиком; «картой» — нет
    check("№3: ответы о паспорте/кодовом слове — секрет целиком",
          ta._secret_answer("Серия и номер паспорта?", "4510 123456")
          and ta._secret_answer("Кодовое слово (секретный вопрос)?",
                                "Сидорова")
          and not ta._secret_answer("Карта или наличные?", "картой"))
    # №4: телефон человека на странице — в подписях маской
    run4 = {"qa": [("Какой телефон?", "8 913 123-45-67")], "goal": "x",
            "history": [], "steps": 0, "lang": "ru"}
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    cc.PAGES = dict(FakeCC.PAGES)
    cc.PAGES["https://pizza.test/menu"] = [
        {"idx": 5, "tag": "a", "role": "link", "text": "8 913 123-45-67"},
        {"idx": 6, "tag": "button", "role": "button", "text": "Маргарита"}]
    agent = TaskAgent(cc)
    p4 = agent._prompt(run4, agent._observe(run4, "rt4"))
    check("№4: телефон, показанный страницей, в подписях — {{secretN}}",
          "913" not in p4 and "] {{secret1}}" in p4 and "] Маргарита" in p4)

    # №7: «да?» — не согласие; «давай не будем» на «продолжать?» — отказ
    cc, agent = waiting("confirm", "rt7")
    agent.feed("rt7", "да?", ScriptedRouter([]), user_id="A")
    check("№7: «да?» на «Делаю?» — переспрос, шаг не исполнен",
          not cc.executed and agent._runs["rt7"]["awaiting"]["kind"]
          == "confirm")
    cc, agent = waiting("continue", "rt7b")
    agent.feed("rt7b", "давай не будем", ScriptedRouter([]), user_id="A")
    check("№7: «давай не будем» на «продолжать?» — задача снята",
          "rt7b" not in agent._runs)

    # №8: клавиша по «да» через 9 минут — не исполняется (фокус другой)
    cc = FormCC()
    agent = TaskAgent(cc)
    agent.start("rt8", "x", ScriptedRouter([{"action": "key", "key": "Enter"}]),
                user_id="A")
    agent._runs["rt8"]["awaiting"]["ts"] = _time.time() - 540
    agent.feed("rt8", "да", ScriptedRouter([
        {"action": "fail", "message": "стоп"}]), user_id="A")
    check("№8: Enter по «да» через 9 минут — не нажат",
          not any(a.get("kind") == "key" for a in cc.executed))

    # №9: «стоп», пока переснимали страницу для подтверждённого шага
    class StopObsCC(FormCC):
        armed = False

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            run = agent._runs.get(chat_id)
            if self.armed and run and run.get("busy"):
                agent.cancel(chat_id)  # cc_turn_enter при busy
            return super()._snapshot_for(site_word, chat_id, auto_dismiss)
    cc = StopObsCC()
    agent = TaskAgent(cc)
    agent.start("rt9", "x", ScriptedRouter([{"action": "click", "n": 1}]),
                user_id="A")
    cc.armed = True
    r9 = agent.feed("rt9", "да", ScriptedRouter([]), user_id="A") or ""
    check("№9: «стоп» во время пересъёмки — «Отправить» не нажат",
          not cc.executed and "бросаю" in r9)

    # №12: иконка без подписи на оформлении — после «да» находится заново
    icon_url = "https://shop.test/order/delivery"
    cc = real_cc({icon_url: [
        {"idx": 3, "tag": "button", "role": "button", "text": "",
         "ctx": "Адрес доставки Ленина 5"}]}, url=icon_url)
    agent = TaskAgent(cc)
    agent.start("rt12", "x", ScriptedRouter([{"action": "click", "n": 1}]),
                user_id="A")
    cc.pages[icon_url] = [{"idx": 44, "tag": "button", "role": "button",
                           "text": "", "ctx": "Адрес доставки Ленина 5"}]
    agent.feed("rt12", "да", ScriptedRouter([
        {"action": "fail", "message": "стоп"}]), user_id="A")
    check("№12: безымянная иконка после «да» — нажата по свежему номеру",
          [a["idx"] for a in cc.dispatched] == [44])

    # ── Финальная проверка: H1–H5, M3/M4/M9/M10, мелочи ──
    # Номера разметки меняются при КАЖДОМ снимке (как в браузере), клик по
    # номеру старого снимка — «элемент потерян»
    def renum_cc(pages, url):
        cc = real_cc({}, url=url)
        cc.gen = 0

        def _snap(site_word, chat_id="", auto_dismiss=False):
            cc.gen += 1
            return (cc.cur, "shop.test",
                    [dict(x, idx=x["idx"] + 1000 * cc.gen)
                     for x in pages[cc.cur]], 1, None)

        def _disp(action, router=None):
            if action["kind"] == "read":
                action["_result"] = cc.read_text
                return
            if action["kind"] in ("click", "type") \
                    and action["idx"] // 1000 != cc.gen:
                raise _ba6.BrowserUnavailable(
                    "элемент потерян — страница изменилась")
            cc.dispatched.append(dict(action))
        cc._snapshot_for, cc._dispatch = _snap, _disp
        cc.read_text = "Итого: 816 ₽"
        return cc

    # H5: итог перечитан — клик по свежему номеру (не «элемент потерян»)
    cc = renum_cc({"https://shop.test/order/confirm": [
        {"idx": 1, "tag": "button", "role": "button",
         "text": "Оформить заказ"}]}, "https://shop.test/order/confirm")
    agent = TaskAgent(cc)
    agent.start("h5", "закажи пиццу на shop.test", ScriptedRouter([
        {"action": "click", "n": 1}]), user_id="A")
    agent.feed("h5", "да", ScriptedRouter([
        {"action": "done", "message": "Заказ оформлен"}]), user_id="A")
    check("H5: коммит после «да» нажат (перечитывание итога не сбило номер)",
          [a.get("element") for a in cc.dispatched] == ["Оформить заказ"])
    # M3: «стоп» во время перечитывания итога — клика нет
    cc = renum_cc({"https://shop.test/order/confirm": [
        {"idx": 1, "tag": "button", "role": "button",
         "text": "Оформить заказ"}]}, "https://shop.test/order/confirm")
    agent = TaskAgent(cc)
    agent.start("m3", "закажи пиццу на shop.test", ScriptedRouter([
        {"action": "click", "n": 1}]), user_id="A")
    orig_disp = cc._dispatch

    def _disp_stop(action, router=None):
        if action["kind"] == "read":
            agent.cancel("m3")  # cc_turn_enter при busy
        return orig_disp(action, router)
    cc._dispatch = _disp_stop
    r = agent.feed("m3", "да", ScriptedRouter([]), user_id="A") or ""
    check("M3: «стоп» во время перечитывания итога — заказ не оформлен",
          not cc.dispatched and "бросаю" in r)

    # H1: «элемент потерян» → пересъёмка; под той же подписью теперь окно
    # списания — передача человеку, не клик
    class LostCC(FakeCC):
        def __init__(self):
            super().__init__()
            self.url, self.n = "https://shop.test/", 0

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            self.n += 1
            if self.n == 1:
                return self.url, "shop.test", [
                    {"idx": 7, "tag": "button", "role": "button",
                     "text": "Продолжить", "ctx": ""}], 1, None
            return self.url, "shop.test", [
                {"idx": 70, "tag": "button", "role": "button",
                 "text": "Продолжить", "md": True,
                 "ctx": "Списать 1 299 ₽ с карты *4242 за подписку Плюс?"}
            ], 1, None

        def execute(self, act, chat_id="", router=None):
            self.executed.append(dict(act))
            if act.get("idx") == 7:
                return False, "элемент потерян — страница изменилась"
            return True, ""
    cc = LostCC()
    r = TaskAgent(cc).start("h1", "x", ScriptedRouter([
        {"action": "click", "n": 1}]), user_id="A")
    check("H1: после «элемент потерян» под той же подписью другое окно "
          "(списание) — повторного клика нет",
          [a.get("idx") for a in cc.executed] == [7])

    # Встроенный блок «Удалить аккаунт навсегда? [Да]» (не окно) — «да»
    cc = real_cc({"https://shop.test/": [
        {"idx": 1, "tag": "button", "role": "button", "text": "Да",
         "ctx": "Удалить аккаунт навсегда? Да Нет"}]}, url="https://shop.test/")
    r = TaskAgent(cc).start("h2", "x", ScriptedRouter([
        {"action": "click", "n": 1}]), user_id="A")
    check("H2: «Да» в блоке «Удалить аккаунт?» вне окна — вопрос, клика нет",
          "Делаю?" in r and not cc.dispatched)

    # H3: ответ на вопрос с приватной страницы — в облако заглушкой
    agent = TaskAgent(FakeCC())
    agent.slot_router = SlotRouter([{}])
    run_h3 = {"goal": "закажи пиццу", "qa": [], "chat_id": "h3", "lang": "ru",
              "history": [], "steps": 0}
    agent._update_brief(run_h3, "(the task goal)", "закажи пиццу")
    saved_pr = _cp_guard.PrivateRouter.get_response
    _cp_guard.PrivateRouter.get_response = lambda self, msgs, **kw: json.dumps(
        {"address": "ул. Ленина 5, кв. 12"}, ensure_ascii=False)
    try:
        agent._update_brief(run_h3, "В профиле адрес … Доставить туда?", "да",
                            private=True)
    finally:
        _cp_guard.PrivateRouter.get_response = saved_pr
    lines_cloud = "\n".join(ta._brief_lines(run_h3["brief"]))
    lines_local = "\n".join(ta._brief_lines(run_h3["brief"], local=True))
    check("H3: слот из ответа на приватной странице — облаку заглушкой, "
          "локальной модели — как есть",
          "Ленина" not in lines_cloud and "hidden" in lines_cloud
          and "Ленина" in lines_local)

    # H4: «бросить задачу?» — пароль из команды не цитируется
    agent = TaskAgent(FakeCC())
    agent.start("h4", "закажи пиццу", ScriptedRouter([
        {"action": "ask", "question": "Какую пиццу?"}]), user_id="A")
    q_h4 = agent.ask_switch(
        "h4", "задача: войди в почту, логин ivan@mail.ru пароль Kotik2019!",
        user_id="A")
    check("H4: вопрос «бросить задачу?» — без пароля и почты",
          "Kotik2019" not in q_h4 and "ivan@mail.ru" not in q_h4)
    # M9: пока ждём ответа на «бросить?», прежний вопрос — для маски ответа
    check("M9: awaiting_question смотрит сквозь «бросить задачу?»",
          agent.awaiting_question("h4") == "Какую пиццу?")
    # L8: «не надо» на «бросить задачу?» — «нет», задача продолжается
    r = agent.feed("h4", "не надо", ScriptedRouter([]), user_id="A")
    check("L8/L9: «не надо» на «бросить задачу?» — задача жива, прежний "
          "вопрос повторён", "h4" in agent._runs and "Какую пиццу?" in r)
    # L10: протухший вопрос «бросить?» — «да» не бросает задачу
    agent.ask_switch("h4", "открой ютуб", user_id="A")
    agent._runs["h4"]["awaiting"]["ts"] = _time.time() - 3600
    agent.feed("h4", "да", ScriptedRouter([
        {"action": "fail", "message": "стоп"}]), user_id="A")
    check("L10: «да» на протухший «бросить задачу?» — команда не исполнена",
          agent.pop_switch("h4") is None)

    # M4: «стоп» во время разбора цели по слотам — прогон не действует
    class StopSlot:
        def get_response(self, messages, **kw):
            agent_m4.cancel("m4")  # cc_turn_enter
            return "{}"
    cc = FakeCC()
    cc.url = "https://pizza.test/menu"
    agent_m4 = TaskAgent(cc)
    agent_m4.slot_router = StopSlot()
    r = agent_m4.start("m4", "закажи пиццу", ScriptedRouter([
        {"action": "click", "n": 1}]), user_id="A")
    check("M4: «стоп» во время разбора цели — ни одного клика",
          not cc.executed and "бросаю" in r and "m4" not in agent_m4._runs)

    # ── Перепроверка (rereview.md): N2–N5, R1, R4 ──
    # N2: «В корзину» — submit на оформлении (вопрос A3): подтверждённое
    # добавление засчитано товару, а не тексту кнопки
    cc = ShopCC()
    cc.url = "https://pizza.test/order/menu"
    agent = TaskAgent(cc)
    agent.start("n2", "закажи маргариту на pizza.test", ScriptedRouter([
        {"action": "click", "n": 2}]), user_id="A")
    ShopCC_items = cc._snapshot_for
    agent.feed("n2", "да", ScriptedRouter([
        {"action": "fail", "message": "стоп"}]), user_id="A")
    fin = (agent.__dict__.get("_finished", {}).get("n2") or {}).get("run") \
        or agent._runs.get("n2") or {}
    check("N2: подтверждённое «В корзину» — ключ товара «Маргарита»",
          [x["key"] for x in fin.get("cart_adds") or []] == ["Маргарита"])
    # N3: в группе «нет» на «бросить задачу?» повторяет вопрос с приватной
    # страницы заглушкой
    agent = TaskAgent(FakeCC())
    agent.is_group = lambda chat: chat.startswith("-")
    agent.start("-n3", "x", ScriptedRouter([
        {"action": "ask", "question": "Какую пиццу?"}]), user_id="A")
    agent._runs["-n3"]["awaiting"].update(
        private=True, question="В профиле: ул. Ленина 5. Доставить туда?")
    agent.ask_switch("-n3", "открой ютуб", user_id="A")
    r = agent.feed("-n3", "нет", ScriptedRouter([]), user_id="A")
    check("N3: группа — прежний вопрос с приватной страницы заглушкой",
          "Ленина" not in r and "приватной странице" in r)
    # N4: сбой разбора цели не оставляет прогон «занятым»
    class BoomSlot:
        def get_response(self, messages, **kw):
            raise KeyError("boom")
    agent = TaskAgent(FakeCC())
    agent.slot_router = BoomSlot()
    orig_apply = ta._apply_brief
    ta._apply_brief = lambda *a, **k: (_ for _ in ()).throw(TypeError("x"))
    try:
        agent.start("n4", "закажи как в прошлый раз", ScriptedRouter([
            {"action": "ask", "question": "Что заказать?"}]), user_id="A")
    finally:
        ta._apply_brief = orig_apply
    check("N4: исключение в разборе цели — прогон жив и не занят",
          "n4" in agent._runs and not agent._runs["n4"]["busy"])
    # R1: «Продолжить» рядом со «Списать … с карты» вне окна — человеку
    cc = real_cc({"https://shop.test/": [
        {"idx": 1, "tag": "button", "role": "button", "text": "Продолжить",
         "ctx": "Списать 1 299 ₽ с карты *4242 за подписку Плюс Продолжить"}]},
        url="https://shop.test/")
    r = TaskAgent(cc).start("r1", "x", ScriptedRouter([
        {"action": "click", "n": 1}]), user_id="A")
    check("R1: «Продолжить» у «Списать … с карты» вне окна — передача "
          "человеку, клика нет", "дальше сам" in r and not cc.dispatched)

    # recheck2: «списа…» без суммы/карты рядом — не оплата; cookie-текст со
    # словами удаления/отправки — не опасное окно
    from app.features.computer_control import dialog_risk as _dr
    calm = [("Далее", "Список товаров (2) Пепперони 599 ₽ Маргарита 499 ₽"),
            ("Продолжить", "Итого 1 098 ₽ Списать 120 бонусов"),
            ("Далее", "Способ оплаты: списание при получении заказа"),
            ("Continue", "Shipping: free of charge"),
            ("Next", "Delivery charge: $0"),
            ("Хорошо", "Мы используем файлы cookie. Вы можете удалить cookie"),
            ("ОК", "Сайт использует куки для оформления заказов и отправки "
                   "уведомлений")]
    bad = [(l, c, _dr(l, c, in_dialog=False)) for l, c in calm
           if _dr(l, c, in_dialog=False) is not None]
    check(f"recheck2: мирные «Далее/Продолжить» и cookie-тексты — не риск "
          f"{bad}", not bad)
    risky = [("Продолжить", "Списать 1 299 ₽ с карты *4242", "payment"),
             ("Далее", "С вашей карты будет списано 499 ₽", "payment"),
             ("Continue", "We will charge $9.99 to your card", "payment"),
             ("ОК", "Подтвердите заказ №4471. Мы используем cookie", "commit"),
             ("Да", "Удалить аккаунт? Мы используем cookie", "destructive")]
    bad = [(l, c, _dr(l, c, in_dialog=True)) for l, c, want in risky
           if _dr(l, c, in_dialog=True) != want]
    check(f"recheck2: списание/заказ/удаление рядом с cookie — риск {bad}",
          not bad)

    # ── Живой прогон 30.09 (dodopizza): сайт из памяти до ответа, добавки,
    # «что-нибудь ещё?», чужое в корзине, двойное «да» на переход ──
    mem_live = Path(tempfile.mkdtemp(prefix="task_mem_live_")) / "m.json"
    mem_live.write_text(json.dumps({"lv": [
        {"ts": 1, "goal": "закажи пиццу", "sites": ["pizza.test"], "qa": [],
         "result": "Дошёл до оплаты", "ok": True,
         "brief": {"site": "pizza.test", "items": []}}]},
        ensure_ascii=False), encoding="utf-8")
    cc = real_cc({"https://pizza.test/": [
        {"idx": 1, "tag": "a", "role": "link", "text": "Пиццы"}]})
    agent = TaskAgent(cc, memory_path=mem_live)
    router = ScriptedRouter([{"action": "open", "target": "https://pizza.test/"},
                             {"action": "open", "target": "https://pizza.test/"},
                             {"action": "fail", "message": "стоп"}])
    r = agent.start("lv", "закажи пиццу", router, user_id="A")
    check("живой 30.09: сайт из памяти — сначала вопрос, сайт не открыт",
          "Как в прошлый раз — на pizza.test?" in r and not cc.dispatched)
    agent.feed("lv", "да", router, user_id="A")
    check("живой 30.09: «да» — сайт в брифе от человека, дальше открыт",
          ((agent._runs.get("lv") or {}).get("brief") or {}).get("site", {})
          .get("src") == "user"
          or any(d.get("kind") == "url" for d in cc.dispatched))
    check("живой 30.09: вопрос о сайте задаётся один раз",
          sum("Как в прошлый раз" in q for q, _a in
              ((agent.__dict__.get("_finished", {}).get("lv") or {})
               .get("run") or agent._runs.get("lv") or {}).get("qa", []))
          == 1)
    # Модель сама спросила про сайт — код второй раз не спрашивает
    agent = TaskAgent(real_cc({"https://pizza.test/": [
        {"idx": 1, "tag": "a", "role": "link", "text": "Пиццы"}]}),
        memory_path=mem_live)
    # Разбор ответа «да» на вопрос модели (slot_router) — сайт в бриф
    agent.slot_router = SlotRouter([{}, {"site": "pizza.test"}])
    router = ScriptedRouter([
        {"action": "ask", "question": "Заказать на pizza.test, как в прошлый раз?"},
        {"action": "open", "target": "https://pizza.test/"},
        {"action": "fail", "message": "стоп"}])
    agent.start("lv", "закажи пиццу", router, user_id="A")
    r = agent.feed("lv", "да", router, user_id="A")
    check("живой 30.09: модель спросила о сайте сама — код свой вопрос не "
          "повторяет; незнакомый домен из её вопроса — по-прежнему с «да» "
          "(вопрос модели мог подсказать текст страницы)",
          "Как в прошлый раз —" not in r and "Делаю?" in r
          and not any(d.get("kind") == "url" for d in agent.cc.dispatched))
    # M2: принятый на вопрос кода сайт — главная без «открыть?», адрес с
    # путём (данные в пути) — только с «да»
    mem_m2 = mem_live.parent / "m_m2.json"
    mem_m2.write_text(json.dumps({"lv2": [
        {"ts": 1, "goal": "закажи пиццу", "sites": ["pizza.test"], "qa": [],
         "result": "Дошёл до оплаты", "ok": True,
         "brief": {"site": "pizza.test", "items": []}}]},
        ensure_ascii=False), encoding="utf-8")
    cc = real_cc({"https://pizza.test/": [
        {"idx": 1, "tag": "a", "role": "link", "text": "Пиццы"}]})
    agent = TaskAgent(cc, memory_path=mem_m2)
    router = ScriptedRouter([
        {"action": "open", "target": "https://pizza.test/"},
        {"action": "open", "target": "https://pizza.test/"},
        {"action": "open", "target": "https://pizza.test/order/79991234567"},
        {"action": "fail", "message": "стоп"}])
    r0 = agent.start("lv2", "закажи пиццу", router, user_id="A")
    r = agent.feed("lv2", "да", router, user_id="A")
    check("M2: сайт, принятый на вопрос кода, — главная открыта без "
          "«открыть?», адрес с путём — вопрос, не открыт",
          "Как в прошлый раз — на pizza.test?" in r0
          and [d.get("value") for d in cc.dispatched if d.get("kind") == "url"]
          == ["https://pizza.test/"] and "Делаю?" in r
          and "открыть https://pizza.test/order" in r)
    # M1: в памяти без сайта в брифе (только «sites» — там бывает вкладка
    # человека) — код сайт не предлагает; приватный хост — тоже
    mem_m1 = mem_live.parent / "m_m1.json"
    mem_m1.write_text(json.dumps({"z": [
        {"ts": 1, "goal": "закажи пиццу", "sites": ["www.youtube.com",
                                                    "pizza.test"],
         "qa": [], "result": "x", "ok": True,
         "brief": {"site": None, "items": []}}]}), encoding="utf-8")
    r = TaskAgent(real_cc({"https://pizza.test/": []}),
                  memory_path=mem_m1).start("z", "закажи пиццу", ScriptedRouter([
                      {"action": "fail", "message": "стоп"}]), user_id="A")
    check("M1: без сайта в брифе прошлой записи — вопроса «как в прошлый "
          "раз — на www.youtube.com?» нет", "Как в прошлый раз" not in r)
    # Вкладка человека (видео) открыта до задачи; агент только читает её —
    # это не сайт задачи (в память «sites» не попадает)
    cc = real_cc({"https://www.youtube.com/watch": [
        {"idx": 1, "tag": "a", "role": "link", "text": "Видео"}]},
        url="https://www.youtube.com/watch")
    agent = TaskAgent(cc, memory_path=mem_live.parent / "m_m1b.json")
    agent.start("m1b", "закажи пиццу", ScriptedRouter([
        {"action": "read"}, {"action": "fail", "message": "стоп"}]),
        user_id="A")
    fin = ((agent.__dict__.get("_finished") or {}).get("m1b") or {}).get(
        "run") or {}
    check("M1: только чтение вкладки человека — её хост не сайт задачи",
          fin.get("sites") == [])
    # Сквозной путь: успешный заказ (сайт не называли — модель открыла сама)
    # → в памяти сайт, где выросла корзина → следующая задача: «как в
    # прошлый раз — на pizza.test?» до любого действия
    mem_e2e = mem_live.parent / "m_e2e.json"
    Q1, Q2 = "https://pizza.test/menu", "https://pizza.test/?cart"
    cart_btn = lambda t: {"idx": 7, "tag": "button", "role": "button",
                          "text": t, "aria": "Корзина"}
    cc = real_cc({
        Q1: [cart_btn("0 ₽"), {"idx": 6, "tag": "button", "role": "button",
                               "text": "В корзину за 359 ₽",
                               "ctx": "Терияки 20 см 359 ₽"}],
        Q2: [cart_btn("3 5 9 ₽"), {"idx": 9, "tag": "button",
                                   "role": "button",
                                   "text": "Оплатить картой"}]},
        url=Q1, clicks={6: Q2})
    agent = TaskAgent(cc, memory_path=mem_e2e)
    router = ScriptedRouter([{"action": "click", "n": 2},
                             {"action": "click", "n": 2},   # оплата — ещё?
                             {"action": "click", "n": 2},   # передача
                             {"action": "fail", "message": "x"}])
    agent.start("e2e", "закажи пиццу терияки на pizza.test", router, user_id="A")
    r = agent.feed("e2e", "нет", router, user_id="A")
    rec = (json.loads(mem_e2e.read_text(encoding="utf-8")).get("e2e")
           or [{}])[-1]
    check("сквозной: заказ дошёл до оплаты — в памяти сайт, где выросла "
          "корзина", "дальше сам" in r and rec.get("ok") is True
          and (rec.get("brief") or {}).get("site") == "pizza.test")
    cc2 = real_cc({"https://pizza.test/": []})
    r = TaskAgent(cc2, memory_path=mem_e2e).start(
        "e2e", "закажи пиццу", ScriptedRouter([
            {"action": "open", "target": "https://pizza.test/"}]),
        user_id="A")
    check("сквозной: следующая задача — «как в прошлый раз — на pizza.test?» "
          "до открытия", "Как в прошлый раз — на pizza.test?" in r
          and not cc2.dispatched)

    # L4: в память — адрес сайта, а не название («додо»): где выросла
    # корзина, иначе где кончилась задача (без счётчика в шапке)
    mem_l4 = mem_live.parent / "m_l4.json"
    ag = TaskAgent(FakeCC(), memory_path=mem_l4)
    base_run = {"goal": "закажи пиццу", "qa": [("Где?", "на додо")],
                "sites": ["www.pizza.test"], "outcome": "payment",
                "cancel": False, "history": []}
    ag._remember("l4a", dict(base_run, cart_host="www.pizza.test", brief={
        "items": [], "exclude": [], "site": {"value": "додо", "src": "user"}}),
        "Дошёл до оплаты")
    ag._remember("l4b", dict(base_run, page_host="shop2.test", brief={
        "items": [], "exclude": [], "site": None}), "Дошёл до оплаты")
    mem = json.loads(mem_l4.read_text(encoding="utf-8"))
    check("L4: сайт в памяти — адрес (где выросла корзина / где кончилась "
          "задача), без www", mem["l4a"][-1]["brief"]["site"] == "pizza.test"
          and mem["l4b"][-1]["brief"]["site"] == "shop2.test")

    # M3: «что-нибудь ещё?» — по корзине, заказанное не добавленное отдельно
    ag = TaskAgent(FakeCC())
    run = {"goal": "закажи терияки и колу", "qa": [], "history": [],
           "brief": {"items": [{"name": "Терияки", "size": "20 см",
                                "options": [], "qty": 1},
                               {"name": "Кока-кола", "size": "0,5 л",
                                "options": [], "qty": 1}]},
           "cart_adds": [{"key": "Терияки", "verified": True}]}
    kind, q = ag._ask_more(run, "c", {"private": False})
    check("M3: «В корзине» — только добавленное, остальное — «ещё не в "
          "корзине»", "В корзине: Терияки 20 см." in q
          and "Ещё не в корзине: Кока-кола 0,5 л." in q)
    q_more = "Хотите заказать что-нибудь еще?"
    q_addr = "Что-то ещё указать в адресе?"
    check("L2: «что-нибудь еще» без ё и с «заказ» — вопрос о дозаказе; "
          "«что-то ещё указать в адресе?» — нет",
          bool(ta._ELSE_Q_RE.search(q_more) and ta._ORDER_WORD_RE.search(q_more))
          and not ta._ORDER_WORD_RE.search(q_addr))
    # Голое «Что-нибудь ещё?» модели, когда в корзине уже есть добавленное
    # задачей, — тоже дозаказ: код не спрашивает второй раз
    ag = TaskAgent(FakeCC())
    run_l2 = {"goal": "закажи пиццу", "qa": [], "history": [], "sigs": [],
              "cart_adds": [{"key": "Терияки", "verified": True}],
              "awaiting": None}
    ag._act(run_l2, "l2", None, {"action": "ask",
                                 "question": "Что-нибудь ещё?"},
            {"private": False, "url": "", "host": "", "shown": []})
    check("L2: голое «Что-нибудь ещё?» после добавления — код не повторит",
          run_l2.get("more_asked") is True)
    long_lab = "К оформлению " + "— " * 45 + "и оплате"
    cc = real_cc({"https://pizza.test/": [
        {"idx": 1, "tag": "button", "role": "button", "text": long_lab}]},
        url="https://pizza.test/")
    r = TaskAgent(cc, memory_path=mem_live.parent / "m_l6.json").start(
        "l6", "закажи пиццу на pizza.test", ScriptedRouter([
            {"action": "click", "n": 1}, {"action": "fail", "message": "x"}]),
        user_id="A")
    check("L6: длинная подпись «К оформлению ——… и оплате» (в действии "
          "обрезана до 80) — не переход: вопрос, клика нет",
          "(да/нет)" in r and not [d for d in cc.dispatched
                                   if d.get("kind") == "click"])
    check("деньги: 12345.67 → «12 345.67»", ta._fmt_money(12345.67)
          == "12 345.67")

    P1 = "https://pizza.test/product/teriyaki"
    P2 = "https://pizza.test/?cart"
    P3 = "https://pizza.test/step2"
    dlg = [{"idx": 1, "tag": "button", "role": "button", "text": "Закрыть",
            "md": 1},
           {"idx": 2, "tag": "button", "role": "button",
            "text": "Моцарелла 69 ₽", "md": 1},
           {"idx": 3, "tag": "button", "role": "button", "text": "Бекон 59 ₽",
            "md": 1},
           {"idx": 4, "tag": "label", "role": "", "text": "20 см", "md": 1,
            "on": 1, "ctx": "20 см 25 см"},
           {"idx": 5, "tag": "label", "role": "", "text": "25 см", "md": 1,
            "on": 0, "ctx": "20 см 25 см"},
           {"idx": 6, "tag": "button", "role": "button",
            "text": "В корзину за 359 ₽", "md": 1,
            "ctx": "Терияки 20 см, традиционное тесто"}]
    hdr = lambda s: {"idx": 7, "tag": "button", "role": "button", "text": s,
                     "aria": "Корзина"}
    cc = real_cc({
        P1: dlg + [hdr("8 4 9 ₽")],
        P2: [hdr("1 2 0 8 ₽"),
             {"idx": 8, "tag": "button", "role": "button",
              "text": "К оформлению заказа", "md": 1,
              "ctx": "Терияки 20 см 359 ₽ Итого 1 208 ₽"}],
        P3: [{"idx": 9, "tag": "button", "role": "button",
              "text": "Оформить заказ"}]},
        url=P1, clicks={6: P2, 8: P3})
    agent = TaskAgent(cc, memory_path=mem_live.parent / "m2.json")
    agent.slot_router = SlotRouter([
        {"items": [{"name": "Терияки", "size": "20 см"}]}])
    router = ScriptedRouter([
        {"action": "click", "n": 6},                  # В корзину — добавки?
        {"action": "click", "n": 6},                  # после «нет»
        {"action": "click", "n": 2},                  # К оформлению — ещё?
        {"action": "click", "n": 2},                  # после «нет» — без «да»
        {"action": "click", "n": 1},                  # Оформить заказ
        {"action": "fail", "message": "стоп"}])
    r = agent.start("ad", "закажи терияки 20 см на pizza.test", router, user_id="A")
    check("добавки: окно товара с платными добавками — вопрос с ценами со "
          "страницы, в корзину не нажато",
          "Перед тем как положить «Терияки» в корзину" in r
          and "Добавить к «Терияки» что-нибудь из этого?" in r
          and "- Моцарелла — 69 ₽" in r
          and "- Бекон — 59 ₽" in r and "20 см" not in r.split("\n", 1)[1]
          and not [d for d in cc.dispatched if d.get("kind") == "click"])
    r = agent.feed("ad", "нет", router, user_id="A")
    check("добавки: «нет» — товар в корзину одним кликом",
          [d.get("idx") for d in cc.dispatched if d.get("kind") == "click"] == [6])
    check("ещё: перед переходом к оформлению — «что-нибудь ещё?» с составом "
          "и чужим в корзине (было 849 ₽, стало 1 208 ₽)",
          "В корзине: Терияки 20 см." in r and "Добавить что-нибудь ещё?" in r
          and "849 ₽" in r and "1 208 ₽" in r
          and [d.get("idx") for d in cc.dispatched if d.get("kind") == "click"] == [6])
    r = agent.feed("ad", "нет", router, user_id="A")
    check("переход «К оформлению заказа» — без «да» (настоящий гейт); «да» — "
          "только на сам заказ, с фактами и чужим в корзине",
          [d.get("idx") for d in cc.dispatched if d.get("kind") == "click"] == [6, 8]
          and "Нажать «Оформить заказ»?" in r
          and "лежало там до задачи" in r)
    check("гейт: «К оформлению заказа» от человека (fast) — по-прежнему с «да»",
          ComputerControlManager.confirm_reason(
              {"kind": "click", "element": "К оформлению заказа",
               "origin": "fast"}) == "commit")
    check("корзина человеку — с валютой: «1 208 ₽»",
          agent._cart_str({"count": None, "sum": 1208.0, "cur": "₽"})
          == "1 208 ₽")
    # Добавки названы в цели — вопроса нет
    cc = real_cc({P1: dlg + [hdr("0 ₽")], P2: [hdr("4 2 8 ₽")]},
                 url=P1, clicks={6: P2})
    agent = TaskAgent(cc, memory_path=mem_live.parent / "m3.json")
    agent.slot_router = SlotRouter([
        {"items": [{"name": "Терияки", "size": "20 см",
                    "options": ["моцарелла"]}]}])
    agent.start("ad2", "закажи терияки 20 см с моцареллой на pizza.test", ScriptedRouter([
        {"action": "click", "n": 6}, {"action": "fail", "message": "стоп"}]),
        user_id="A")
    check("добавки: названы человеком — без вопроса, в корзину",
          [d.get("idx") for d in cc.dispatched if d.get("kind") == "click"] == [6])
    # Шторка корзины с «+» у соуса — не окно товара: вопроса о добавках нет
    cc = real_cc({P2: [hdr("3 5 9 ₽"),
                       {"idx": 10, "tag": "button", "role": "button",
                        "text": "В корзину", "md": 1,
                        "ctx": "Сырный соус 69 ₽"},
                       {"idx": 11, "tag": "button", "role": "button",
                        "text": "Чесночный соус 69 ₽", "md": 1},
                       {"idx": 8, "tag": "button", "role": "button",
                        "text": "К оформлению заказа", "md": 1}]},
                 url=P2)
    r = TaskAgent(cc, memory_path=mem_live.parent / "m4.json").start(
        "ad3", "закажи сырный соус на pizza.test", ScriptedRouter([
            {"action": "click", "n": 2}, {"action": "fail", "message": "стоп"}]),
        user_id="A")
    check("добавки: «В корзину» в шторке корзины — без вопроса о добавках",
          "Перед тем как положить" not in r
          and [d.get("idx") for d in cc.dispatched if d.get("kind") == "click"]
          == [10])

    # ── Живой прогон 30.09, 15:43: сайт открыт до вопроса «на каком
    # сайте?» (поиск → первый результат → вопрос); вопросы о товаре — по
    # одному. Что спросить перед шагом — модель (_pre_questions), когда —
    # код ──
    class QRouter:
        # slot_router: на «что спросить?» — текст вопросов, на разбор
        # ответа — JSON слотов; оба по очереди
        def __init__(self, questions, slots=()):
            self.questions, self.slots = list(questions), list(slots)
            self.q_prompts, self.prompts = [], []

        def get_response(self, messages, **kw):
            p = messages[-1]["content"]
            self.prompts.append(p)
            if "What do you need to ask the user before this step?" in p:
                self.q_prompts.append(p)
                return self.questions.pop(0) if self.questions else "NONE"
            return json.dumps(self.slots.pop(0) if self.slots else {},
                              ensure_ascii=False)

    DODO = "https://dodopizza.ru/city"
    found_live = [
        {"title": "Додо Пицца �город — доставка пиццы", "snippet":
         "Пиццы от 279 ₽", "url": DODO},
        {"title": "Папа Джонс", "snippet": "от 429 ₽",
         "url": "https://www.papajohns.ru/city"}]
    ta.web_search_links = lambda q, **kw: (found_live, None)
    try:
        cc = real_cc({DODO: [{"idx": 1, "tag": "a", "role": "link",
                              "text": "Пиццы"}]})
        agent = TaskAgent(cc, memory_path=mem_live.parent / "w1.json")
        router = ScriptedRouter([
            {"action": "search", "query": "заказать пиццу доставка"},
            {"action": "open", "target": DODO},
            {"action": "open", "target": DODO},
            {"action": "fail", "message": "стоп"}])
        r = agent.start("w1", "закажи пиццу", router, user_id="A")
        check("живой 15:43: заказ без сайта — поиск можно, первый результат "
              "не открыт до вопроса; варианты из выдачи (без модели — кодом)",
              "На каком сайте это сделать?" in r
              and "- dodopizza.ru — Додо Пицца" in r
              and "- papajohns.ru — Папа Джонс" in r
              and not [d for d in cc.dispatched if d.get("kind") == "url"])
        r = agent.feed("w1", "додо", router, user_id="A")
        check("живой 15:43: после ответа — открыт результат поиска (без "
              "второго «открыть?»), вопрос о сайте один",
              [d.get("value") for d in cc.dispatched
               if d.get("kind") == "url"] == [DODO]
              and "На каком сайте" not in r)
        # Вопрос пишет модель по выдаче; адреса — из выдачи
        cc = real_cc({DODO: []})
        agent = TaskAgent(cc, memory_path=mem_live.parent / "w2.json")
        qr = QRouter(["Исходя из выдачи:\n**На каком сайте заказать пиццу?**\n"
                      "- Додо Пицца (dodopizza.ru) — от 279 ₽\n"
                      "- Папа Джонс (papajohns.ru) — от 429 ₽"])
        agent.slot_router = qr
        r = agent.start("w2", "закажи пиццу", ScriptedRouter([
            {"action": "search", "query": "пицца"},
            {"action": "open", "target": DODO}]), user_id="A")
        check("вопрос о сайте — от модели: вступление и разметка убраны, "
              "варианты под вопросом",
              r.endswith("На каком сайте заказать пиццу?\n- Додо Пицца "
                         "(dodopizza.ru) — от 279 ₽\n- Папа Джонс "
                         "(papajohns.ru) — от 429 ₽")
              and "Исходя" not in r and "**" not in r)
        check("вопрос о сайте: модели — цель, выдача и шаг",
              qr.q_prompts and "закажи пиццу" in qr.q_prompts[0]
              and "papajohns.ru/city" in qr.q_prompts[0]
              and "Next step: open a website" in qr.q_prompts[0])
        # Модель вписала адрес не из выдачи — варианты кодом
        agent = TaskAgent(real_cc({DODO: []}),
                          memory_path=mem_live.parent / "w3.json")
        agent.slot_router = QRouter(["Где заказать?\n- Додо (dodo.ru)"])
        r = agent.start("w3", "закажи пиццу", ScriptedRouter([
            {"action": "search", "query": "пицца"},
            {"action": "open", "target": DODO}]), user_id="A")
        check("вопрос о сайте: адрес не из выдачи (dodo.ru) — варианты кодом",
              "dodo.ru)" not in r and "- dodopizza.ru" in r)
        # Поиска ещё не было — назад модели: сначала поиск
        cc = real_cc({DODO: []})
        router = ScriptedRouter([{"action": "open", "target": DODO},
                                 {"action": "fail", "message": "стоп"}])
        r = TaskAgent(cc, memory_path=mem_live.parent / "w4.json").start(
            "w4", "закажи пиццу", router, user_id="A")
        check("заказ без сайта и без поиска — не открыт, модели «сначала "
              "поиск», человеку вопроса нет",
              "NOT opened — the user has not chosen where" in router.prompts[1]
              and not cc.dispatched and "На каком сайте" not in r)
        # «Как в прошлый раз — на X?» → «другой сайт»: тот самый вопрос «где»
        # не считается — перед открытием спрошено с выдачей
        mem_w5 = mem_live.parent / "w5.json"
        mem_w5.write_text(json.dumps({"w5": [
            {"ts": 1, "goal": "закажи пиццу", "sites": ["pizza.test"],
             "qa": [], "result": "Дошёл до оплаты", "ok": True,
             "brief": {"site": "pizza.test", "items": []}}]},
            ensure_ascii=False), encoding="utf-8")
        cc = real_cc({DODO: []})
        agent = TaskAgent(cc, memory_path=mem_w5)
        router = ScriptedRouter([
            {"action": "open", "target": "pizza.test"},
            {"action": "search", "query": "пицца"},
            {"action": "open", "target": DODO}])
        agent.start("w5", "закажи пиццу", router, user_id="A")
        r = agent.feed("w5", "другой сайт", router, user_id="A")
        check("«как в прошлый раз?» → «другой сайт» — перед открытием вопрос "
              "с выдачей", "На каком сайте это сделать?" in r
              and not cc.dispatched)
        # Не заказ — открывает как раньше
        cc = real_cc({DODO: []})
        TaskAgent(cc, memory_path=mem_live.parent / "w6.json").start(
            "w6", "найди меню пиццерии", ScriptedRouter([
                {"action": "search", "query": "пицца"},
                {"action": "open", "target": DODO},
                {"action": "fail", "message": "стоп"}]), user_id="A")
        check("не заказ — результат поиска открыт без вопроса о сайте",
              [d.get("value") for d in cc.dispatched
               if d.get("kind") == "url"] == [DODO])
    finally:
        ta.web_search_links = real_search

    # Вопросы о товаре: все сразу, от модели по окну товара
    dlg2 = dlg[:5] + [
        {"idx": 12, "tag": "label", "role": "", "text": "Традиционное",
         "md": 1, "on": 1},
        {"idx": 13, "tag": "label", "role": "", "text": "Тонкое", "md": 1,
         "on": 0}] + dlg[5:]
    # Стенд состояния не переключает: после клика «Тонкое» — страница, где
    # оно выбрано
    P1t = P1 + "?thin"
    dlg2t = [dict(it, on={12: 0, 13: 1}.get(it["idx"], it.get("on")))
             if it["idx"] in (12, 13) else it for it in dlg2]
    cc = real_cc({P1: dlg2 + [hdr("0 ₽")], P1t: dlg2t + [hdr("0 ₽")],
                  P2: [hdr("4 2 8 ₽")]}, url=P1, clicks={13: P1t, 6: P2})
    agent = TaskAgent(cc, memory_path=mem_live.parent / "iq1.json")
    qr = QRouter(["Исходя из того, что уже выбраны 20 см и традиционное "
                  "тесто, нужно уточнить:\n"
                  "1. Оставляем 20 см или 25 см?\n"
                  "2. Тесто: традиционное или тонкое?\n"
                  "3. Добавить что-то из добавок (Моцарелла 69 ₽, Бекон "
                  "59 ₽)?"],
                 slots=[{"items": [{"name": "Терияки"}]},
                        {"items": [{"name": "Терияки", "size": "20 см",
                                    "options": ["тонкое", "моцарелла"]}]}])
    agent.slot_router = qr
    router = ScriptedRouter([{"action": "click", "n": 8},     # В корзину
                             {"action": "click", "n": 7},     # Тонкое
                             {"action": "click", "n": 2},     # Моцарелла
                             {"action": "click", "n": 8},     # В корзину
                             {"action": "fail", "message": "стоп"}])
    r = agent.start("iq1", "закажи терияки на pizza.test", router, user_id="A")
    check("вопросы о товаре — одним сообщением, от модели (нумерация и "
          "вступление убраны); код размер/добавки не дублирует",
          "Перед тем как положить «Терияки» в корзину:\nОставляем 20 см или "
          "25 см?\nТесто: традиционное или тонкое?\nДобавить что-то из "
          "добавок (Моцарелла 69 ₽, Бекон 59 ₽)?\nОтветь одним сообщением" in r
          and "Какой размер:" not in r and "что-нибудь из этого" not in r
          and not [d for d in cc.dispatched if d.get("kind") == "click"])
    qp = qr.q_prompts[0] if qr.q_prompts else ""
    check("вопросы о товаре: модели — окно товара с выбранным, кнопка, шаг",
          "- 20 см (selected)" in qp and "- Традиционное (selected)" in qp
          and "- Моцарелла 69 ₽" in qp and "- Тонкое" in qp
          and "The add-to-cart button: \"В корзину за 359 ₽\"" in qp
          and "Next step: put \"Терияки\" into the cart" in qp
          and "nothing about delivery" in qp and "anything else" in qp)
    r = agent.feed("iq1", "20, тонкое, с моцареллой", router, user_id="A")
    check("вопросы о товаре: ответ — опции выбраны, товар в корзине, "
          "второго вопроса нет",
          [d.get("idx") for d in cc.dispatched if d.get("kind") == "click"]
          == [13, 2, 6] and "Перед тем как положить" not in r)
    # Модель промолчала о размере и добавках — код добавил их сам
    cc = real_cc({P1: dlg + [hdr("0 ₽")]}, url=P1)
    agent = TaskAgent(cc, memory_path=mem_live.parent / "iq2.json")
    agent.slot_router = QRouter(["NONE"], slots=[{"items": [{"name": "Терияки"}]}])
    r = agent.start("iq2", "закажи терияки на pizza.test", ScriptedRouter([
        {"action": "click", "n": 6}, {"action": "fail", "message": "стоп"}]),
        user_id="A")
    check("вопросы о товаре: модель — NONE, а размер не выбран и есть "
          "платные добавки — спрошено кодом",
          "Какой размер: 20 см, 25 см? Сейчас выбран 20 см." in r
          and "- Моцарелла — 69 ₽" in r
          and not [d for d in cc.dispatched if d.get("kind") == "click"])
    # Всё названо — вопроса нет
    cc = real_cc({P1: dlg + [hdr("0 ₽")], P2: [hdr("3 5 9 ₽")]},
                 url=P1, clicks={6: P2})
    agent = TaskAgent(cc, memory_path=mem_live.parent / "iq3.json")
    agent.slot_router = QRouter(["NONE"], slots=[
        {"items": [{"name": "Терияки", "size": "20 см"}]}])
    agent.start("iq3", "закажи терияки 20 см без добавок на pizza.test", ScriptedRouter([
        {"action": "click", "n": 6}, {"action": "fail", "message": "стоп"}]),
        user_id="A")
    check("вопросы о товаре: размер и «без добавок» названы, модель — NONE "
          "— в корзину сразу",
          [d.get("idx") for d in cc.dispatched if d.get("kind") == "click"]
          == [6])
    check("разбор «что спросить?»: пусто — сбой, NONE — нечего, не больше "
          f"{ta.PRE_QUESTIONS_MAX} вопросов, варианты — под вопросом",
          ta._question_lines("") is None and ta._question_lines("NONE") == []
          and len(ta._question_lines("\n".join(f"В{i}?" for i in range(9))))
          == ta.PRE_QUESTIONS_MAX
          and ta._question_lines("Вступление:\n- Где?\n- А\n* Б\nтекст")
          == ["Где?", "- А", "- Б"])

    # Живой 15:49: окно товара вытеснило из снимка шапку со счётчиком —
    # «В корзину» сверяется с корзиной последнего снимка, где она была
    # видна (было «NOT verified» → чтение страницы по кругу → стоп)
    M0, M1, M2 = ("https://pizza.test/menu", "https://pizza.test/product/s",
                  "https://pizza.test/menu?added")
    card = {"idx": 20, "tag": "a", "role": "link", "text": "Сырная от 279 ₽"}
    cc = real_cc({
        M0: [hdr("1 2 0 8 ₽"), card],
        M1: [{"idx": 1, "tag": "button", "role": "button", "text": "Закрыть",
              "md": 1},
             {"idx": 6, "tag": "button", "role": "button",
              "text": "В корзину за 328 ₽", "md": 1, "ctx": "Сырная 20 см"}],
        M2: [hdr("1 5 3 6 ₽"), card]}, url=M0, clicks={20: M1, 6: M2})
    agent = TaskAgent(cc, memory_path=mem_live.parent / "cs1.json")
    router = ScriptedRouter([
        {"action": "click", "n": 2, "label": "Сырная от 279 ₽"},
        {"action": "click", "n": 2, "label": "В корзину за 328 ₽"},
        {"action": "fail", "message": "стоп"}])
    agent.start("cs1", "закажи сырную пиццу на pizza.test", router,
                user_id="A")
    run_cs = (agent.__dict__.get("_finished", {}).get("cs1") or {}).get(
        "run") or agent._runs.get("cs1") or {}
    # Сверено — план сразу спрашивает «что-нибудь ещё?» (модель не звалась)
    hist_cs = " ".join(run_cs.get("history") or ())
    check("живой 15:49: шапки нет в снимке окна — добавление сверено с "
          "корзиной прошлого снимка (1208 → 1536), чужое в корзине замечено",
          len(router.prompts) == 2
          and "added: the cart went from 1208 to 1536" in hist_cs
          and "NOT verified" not in hist_cs
          and (run_cs.get("cart_pre") or {}).get("sum") == 1208
          and (run_cs.get("awaiting") or {}).get("more"))

    class ReadCC(FakeCC):
        def execute(self, act, chat_id="", router=None):
            if act["kind"] == "read":
                self.executed.append(dict(act))
                return True, "Меню: Пепперони 408 ₽"
            return super().execute(act, chat_id, router)
    cc = ReadCC()
    cc.url = "https://pizza.test/menu"
    router = ScriptedRouter([{"action": "read"}, {"action": "read"},
                             {"action": "fail", "message": "стоп"}])
    TaskAgent(cc, memory_path=mem_live.parent / "rd.json").start(
        "rd", "найди пепперони", router, user_id="A")
    check("чтение: тот же текст второй раз — модели «тот же текст, ищи в "
          "списке элементов / find»",
          "the SAME text as the previous read" in router.prompts[2]
          and "the SAME text" not in router.prompts[1])

    # ── Независимая проверка review-live-3009b: вопрос о сайте ──
    PJ = "https://www.papajohns.ru/city"
    ta.web_search_links = lambda q, **kw: (found_live, None)
    try:
        # Вопрос модели со словом «магазин» — не вопрос «где заказать?»
        cc = real_cc({DODO: []})
        agent = TaskAgent(cc, memory_path=mem_live.parent / "v1.json")
        router = ScriptedRouter([
            {"action": "search", "query": "пицца"},
            {"action": "ask", "question": "В каком городе доставить? От этого "
                                          "зависит магазин"},
            {"action": "open", "target": DODO}])
        agent.start("v1", "закажи пиццу", router, user_id="A")
        r = agent.feed("v1", "�город", router, user_id="A")
        check("review: вопрос о городе со словом «магазин» — не выбор сайта: "
              "перед открытием вопрос с выдачей",
              "На каком сайте это сделать?" in r and not cc.dispatched)
        # Ответ на вопрос «где?» привязан к адресу
        router = ScriptedRouter([{"action": "open", "target": DODO},
                                 {"action": "open", "target": PJ},
                                 {"action": "fail", "message": "стоп"}])
        r = agent.feed("v1", "папа джонс", router, user_id="A")
        check("review: ответ «папа джонс» — первый результат (Додо) не открыт, "
              "открыт выбранный",
              "NOT opened — the user chose papajohns.ru" in router.prompts[1]
              and [d.get("value") for d in cc.dispatched
                   if d.get("kind") == "url"] == [PJ])
        # «да» на «как в прошлый раз — на pizza.test?» — другой магазин молча
        # не открывается
        cc = real_cc({DODO: [], "https://pizza.test/": []})
        agent = TaskAgent(cc, memory_path=mem_w5)
        router = ScriptedRouter([
            {"action": "open", "target": "https://pizza.test/"},
            {"action": "search", "query": "пицца"},
            {"action": "open", "target": DODO},
            {"action": "fail", "message": "стоп"}])
        agent.start("w5", "закажи пиццу", router, user_id="A")
        agent.feed("w5", "да", router, user_id="A")
        check("review: «да» на pizza.test — dodopizza из выдачи не открыт",
              "NOT opened — the user chose pizza.test" in router.prompts[-1]
              and not any("dodopizza" in str(d.get("value"))
                          for d in cc.dispatched))
        # Ссылка на магазин с адресом в снимке — до клика тот же вопрос;
        # без адреса — «заказываем здесь?» до первого действия на нём
        Y = "https://ya.test/search?text=pizza"
        cc = real_cc({Y: [{"idx": 5, "tag": "a", "role": "link",
                           "text": "Додо Пицца", "href": DODO}]}, url=Y)
        router = ScriptedRouter([{"action": "click", "n": 1},
                                 {"action": "fail", "message": "стоп"}])
        TaskAgent(cc, memory_path=mem_live.parent / "v4.json").start(
            "v4", "закажи пиццу", router, user_id="A")
        check("review: ссылка на магазин (href) — не нажата до выбора сайта",
              not cc.dispatched
              and "NOT opened — the user has not chosen" in router.prompts[1])
        cc = real_cc({Y: [{"idx": 5, "tag": "a", "role": "link",
                           "text": "Додо Пицца"}],
                      DODO: [{"idx": 7, "tag": "a", "role": "link",
                              "text": "Пиццы"}]}, url=Y, clicks={5: DODO})
        agent = TaskAgent(cc, memory_path=mem_live.parent / "v5.json")
        router = ScriptedRouter([{"action": "click", "n": 1},
                                 {"action": "click", "n": 1},
                                 {"action": "click", "n": 1},
                                 {"action": "fail", "message": "стоп"}])
        r = agent.start("v5", "закажи пиццу", router, user_id="A")
        check("review: магазин открыт кликом без адреса — до действия на нём "
              "«Заказываем здесь — на dodopizza.ru?»",
              "Заказываем здесь — на dodopizza.ru?" in r
              and [d.get("idx") for d in cc.dispatched] == [5])
        agent.feed("v5", "да", router, user_id="A")
        check("review: «да» — дальше на нём без вопросов",
              [d.get("idx") for d in cc.dispatched] == [5, 7])
    finally:
        ta.web_search_links = real_search
    check("review: покупка — «закажи/купи/order a», не «письмо о заказе», "
          "«мой заказ», «order status»",
          all(ta._BUY_GOAL_RE.search(g) for g in (
              "закажи пиццу", "купи чехол", "оформи доставку роллов",
              "order a pizza"))
          and not any(ta._BUY_GOAL_RE.search(g) for g in (
              "найди в почте письмо о заказе", "проверь мой заказ",
              "check my order status", "где мой заказ")))

    # ── review: вопросы о товаре ──
    # Ответ с размером, который разбор не вынул, — не «как есть»
    for ans in ("35, тонкое", "XL", "тонкое тесто"):
        cc = ShopCC()
        cc.sizes = [("25 см", 1), ("30 см", 0), ("35 см", 0)]
        agent = TaskAgent(cc)
        agent.slot_router = SlotRouter([{"items": [{"name": "Пепперони"}]}])
        router = ScriptedRouter([{"action": "click", "n": 1},
                                 {"action": "click", "n": 1},
                                 {"action": "fail", "message": "стоп"}])
        agent.start("sz", "закажи пепперони на pizza.test", router, user_id="A")
        agent.feed("sz", ans, router, user_id="A")
        check(f"review: ответ «{ans}» на вопрос с размерами — выбранный "
              "(25 см) не кладётся, модели «спроси размер»",
              not cc.executed and len(router.prompts) == 3
              and "ask the user which size" in router.prompts[2])
    # «В корзину» на карточке каталога (спрашивать нечего) ключ не тратит:
    # окно товара потом спрашивает о добавках
    CAT = "https://pizza.test/catalog"
    cc = real_cc({CAT: [{"idx": 30, "tag": "button", "role": "button",
                         "text": "В корзину",
                         "ctx": "Терияки 20 см, традиционное тесто"}],
                  P1: dlg + [hdr("0 ₽")]}, url=CAT, clicks={30: P1})
    agent = TaskAgent(cc, memory_path=mem_live.parent / "cm.json")
    agent.slot_router = QRouter(["NONE", "NONE"], slots=[
        {"items": [{"name": "Терияки", "size": "20 см"}]}])
    router = ScriptedRouter([{"action": "click", "n": 1},
                             {"action": "click", "n": 6},
                             {"action": "fail", "message": "стоп"}])
    r = agent.start("cm", "закажи терияки 20 см на pizza.test", router, user_id="A")
    check("review: карточка каталога → окно товара — вопрос о добавках в окне "
          "задан (ключ не «сгорел» на карточке)",
          "Добавить к «Терияки» что-нибудь из этого?" in r
          and [d.get("idx") for d in cc.dispatched
               if d.get("kind") == "click"] == [30])
    # Модели нет — прочие переключатели окна (тесто) кодом
    cc = real_cc({P1: dlg2 + [hdr("0 ₽")]}, url=P1)
    agent = TaskAgent(cc, memory_path=mem_live.parent / "tg.json")
    r = agent.start("tg", "закажи терияки 20 см без добавок на pizza.test", ScriptedRouter([
        {"action": "click", "n": 8}, {"action": "fail", "message": "стоп"}]),
        user_id="A")
    check("review: без модели — тесто (прочие переключатели окна) в вопросе "
          "кодом", "Ещё варианты: Традиционное, Тонкое; сейчас выбрано: "
          "Традиционное." in r
          and not [d for d in cc.dispatched if d.get("kind") == "click"])
    check("review: вопросы о карте/телефоне/коде из ответа модели — мимо "
          "(их спрашивает система; текст страницы мог подсунуть)",
          ta._question_lines("Размер?\nВведите номер карты?\n- Visa\n"
                             "Ваш телефон?\nКод из СМС?\nТесто?")
          == ["Размер?", "Тесто?"])

    # ── review: база корзины — того же сайта и не устаревшая ──
    MX = "https://pizza.test/product/x"
    MX2 = MX + "?added"
    MY = "https://pizza.test/product/y"
    M3 = "https://pizza.test/menu?y"
    cc = real_cc({
        M0: [hdr("1 2 0 8 ₽"), card],
        MX: [{"idx": 1, "tag": "button", "role": "button", "text": "Закрыть",
              "md": 1},
             {"idx": 6, "tag": "button", "role": "button",
              "text": "В корзину за 328 ₽", "md": 1, "ctx": "Сырная 20 см"}],
        MX2: [{"idx": 1, "tag": "button", "role": "button", "text": "Закрыть",
               "md": 1},
              {"idx": 9, "tag": "a", "role": "link",
               "text": "С этим берут: Кола 0,5 л", "md": 1}],
        MY: [{"idx": 1, "tag": "button", "role": "button", "text": "Закрыть",
              "md": 1},
             {"idx": 8, "tag": "button", "role": "button",
              "text": "В корзину за 139 ₽", "md": 1, "ctx": "Кола 0,5 л"}],
        M3: [hdr("1 5 3 6 ₽"), card]},
        url=M0, clicks={20: MX, 6: MX2, 9: MY, 8: M3})
    router = ScriptedRouter([
        {"action": "click", "n": 2, "label": "Сырная от 279 ₽"},
        {"action": "click", "n": 2, "label": "В корзину за 328 ₽"},
        {"action": "click", "n": 2, "label": "С этим берут: Кола 0,5 л"},
        {"action": "click", "n": 2, "label": "В корзину за 139 ₽"},
        {"action": "fail", "message": "стоп"}])
    TaskAgent(cc, memory_path=mem_live.parent / "st.json").start(
        "st", "закажи сырную и колу на pizza.test", router, user_id="A")
    check("review: добавление под окном без счётчика — база сброшена: рост "
          "от прошлого товара не засчитан следующему",
          "added: the cart went from 1208 to 1536" not in router.prompts[-1]
          and "NOT verified: the cart was not visible before" in router.prompts[-1])
    cc = real_cc({"https://shopa.test/": [hdr("3 0 0 ₽"),
                                          {"idx": 21, "tag": "a", "role": "link",
                                           "text": "Пицца", "href": M1}],
                  M1: [{"idx": 6, "tag": "button", "role": "button",
                        "text": "В корзину за 328 ₽", "md": 1,
                        "ctx": "Сырная 20 см"}],
                  M2: [hdr("1 5 3 6 ₽"), card]},
                 url="https://shopa.test/", clicks={21: M1, 6: M2})
    router = ScriptedRouter([
        {"action": "click", "n": 2, "label": "Пицца"},
        {"action": "click", "n": 1, "label": "В корзину за 328 ₽"},
        {"action": "fail", "message": "стоп"}])
    TaskAgent(cc, memory_path=mem_live.parent / "xh.json").start(
        "xh", "закажи сырную на pizza.test", router, user_id="A")
    check("review: корзина другого сайта — не база («300 → 1536» не "
          "засчитано)", "added: the cart went from 300" not in router.prompts[-1]
          and "NOT verified" in router.prompts[-1])

    # ── Перепроверка review-live-3009b: N1–N7 ──
    ta.web_search_links = lambda q, **kw: (found_live, None)
    try:
        # N1: «да» на вопрос модели «заказать на papajohns.ru?» — выбор
        cc = real_cc({DODO: [], PJ: []})
        agent = TaskAgent(cc, memory_path=mem_live.parent / "n1.json")
        router = ScriptedRouter([
            {"action": "search", "query": "пицца"},
            {"action": "open", "target": DODO}])
        agent.start("n1", "закажи пиццу", router, user_id="A")
        router = ScriptedRouter([
            {"action": "open", "target": DODO},
            {"action": "ask", "question": "Додо не доставляет на твой адрес. "
                                          "Заказать на papajohns.ru?"},
            {"action": "open", "target": PJ},
            {"action": "fail", "message": "стоп"}])
        agent.feed("n1", "dodopizza.ru", router, user_id="A")
        agent.feed("n1", "да", router, user_id="A")
        check("N1: «да» на «заказать на papajohns.ru?» — выбор сменён, "
              "papajohns открыт (без блока «user chose dodopizza»)",
              [d.get("value") for d in cc.dispatched
               if d.get("kind") == "url"] == [DODO, PJ])
        # N2: «другой сайт» на «заказываем здесь?» — на нём не действуем
        Y = "https://ya.test/search?text=pizza"
        cc = real_cc({Y: [{"idx": 5, "tag": "a", "role": "link",
                           "text": "Додо Пицца"}],
                      DODO: [{"idx": 7, "tag": "a", "role": "link",
                              "text": "Пиццы"}]}, url=Y, clicks={5: DODO})
        agent = TaskAgent(cc, memory_path=mem_live.parent / "n2.json")
        router = ScriptedRouter([{"action": "click", "n": 1},
                                 {"action": "click", "n": 1},
                                 {"action": "click", "n": 1},
                                 {"action": "fail", "message": "стоп"}])
        agent.start("n2", "закажи пиццу", router, user_id="A")
        agent.feed("n2", "другой сайт", router, user_id="A")
        check("N2: «другой сайт» на «здесь?» — на dodopizza не нажато, модели "
              "«пользователь отказался»",
              [d.get("idx") for d in cc.dispatched] == [5]
              and "the user declined ordering on dodopizza.ru"
              in router.prompts[-1])
        # N3: сайт выбран, а после клика — другой магазин: вопрос
        G = "https://google.test/url?q=pj"
        cc = real_cc({G: [{"idx": 5, "tag": "a", "role": "link",
                           "text": "Папа Джонс"}],
                      PJ: [{"idx": 7, "tag": "a", "role": "link",
                            "text": "Пиццы"}]}, url=G, clicks={5: PJ})
        r = TaskAgent(cc, memory_path=mem_live.parent / "n3.json").start(
            "n3", "закажи пиццу на dodopizza.ru", ScriptedRouter([
                {"action": "click", "n": 1}, {"action": "click", "n": 1},
                {"action": "fail", "message": "стоп"}]), user_id="A")
        check("N3: выбран dodopizza.ru, кликом открыт papajohns — «Сейчас "
              "открыт papajohns.ru, а выбран dodopizza.ru. Заказываем здесь?»",
              "Сейчас открыт papajohns.ru, а выбран dodopizza.ru" in r
              and [d.get("idx") for d in cc.dispatched] == [5])
        # N6: вкладка человека — сам магазин: его страницы — без вопроса о
        # сайте и без «адрес — догадка»
        cc = real_cc({DODO: [{"idx": 7, "tag": "a", "role": "link",
                              "text": "Пиццы"}],
                      DODO + "/menu": []}, url=DODO)
        router = ScriptedRouter([{"action": "open", "target": DODO + "/menu"},
                                 {"action": "fail", "message": "стоп"}])
        r = TaskAgent(cc, memory_path=mem_live.parent / "n6.json").start(
            "n6", "закажи пиццу", router, user_id="A")
        check("N6/18:11: во вкладке уже магазин (мог остаться от прошлой "
              "задачи), сайт не выбран — «Заказываем здесь?», а не «сначала "
              "поиск» и не «адрес — догадка»; не открыто",
              "Заказываем здесь — на dodopizza.ru?" in r and not cc.dispatched
              and "NOT opened" not in "".join(router.prompts))
        # N6: алиас конфига — выбор человека заранее
        cc = FakeCC()
        cc.url = "https://pizza.test/"
        agent = TaskAgent(cc, memory_path=mem_live.parent / "n6b.json")
        r = agent.start("n6b", "закажи пиццу", ScriptedRouter([
            {"action": "click", "n": 1}, {"action": "fail", "message": "стоп"}]),
            user_id="A")
        check("18:11: сайт из алиаса конфига («пицца: dodopizza.ru» — для "
              "«открой пиццу») — не выбор магазина: «Заказываем здесь?»",
              "Заказываем здесь — на pizza.test?" in r and not cc.executed)
    finally:
        ta.web_search_links = real_search
    # N5: модель сама спросила размер, «оставь как есть» — выбранный
    cc = ShopCC()
    cc.sizes = [("25 см", 1), ("30 см", 0), ("35 см", 0)]
    agent = TaskAgent(cc)
    agent.slot_router = SlotRouter([{"items": [{"name": "Пепперони"}]}])
    router = ScriptedRouter([
        {"action": "ask", "question": "Какой размер: 25, 30 или 35 см?"},
        {"action": "click", "n": 1},
        {"action": "fail", "message": "стоп"}])
    agent.start("n5", "закажи пепперони на pizza.test", router, user_id="A")
    r = agent.feed("n5", "оставь как есть, 2 штуки", router, user_id="A")
    check("N5: ответ «как есть, 2 штуки» на вопрос модели о размерах — "
          "выбранный, без отбоя «спроси размер»",
          "ask the user which size" not in "".join(router.prompts))
    check("N7: «хочу пиццу с доставкой» — покупка; «письмо о доставке», "
          "«статус доставки» — нет; «where do you live?» — не вопрос «где "
          "заказать»",
          ta._BUY_GOAL_RE.search("хочу пиццу с доставкой")
          and ta._BUY_GOAL_RE.search("доставка суши на дом")
          and not ta._BUY_GOAL_RE.search("найди письмо о доставке")
          and not ta._BUY_GOAL_RE.search("статус доставки")
          and not ta._is_where_q("Where do you live?")
          and not ta._is_where_q("Каким сервисом оплатишь?")
          and ta._is_where_q("На каком сайте заказать пиццу?")
          and ta._is_where_q("Where should I order it?"))

    # ── Перепроверка 2: алиас/вкладка не перекрывают выбор и отказ; «да»
    # на уточнение модели — не «как есть» ──
    cc = real_cc({DODO: [{"idx": 7, "tag": "a", "role": "link",
                          "text": "Пиццы"}], PJ: []})
    cc.sites = {"пицца": DODO}
    router = ScriptedRouter([{"action": "open", "target": "пицца"},
                             {"action": "fail", "message": "стоп"}])
    TaskAgent(cc, memory_path=mem_live.parent / "o1.json").start(
        "o1", "закажи пепперони на papajohns.ru", router, user_id="A")
    check("M1: алиас конфига (dodopizza) при выбранном papajohns — не открыт",
          not cc.dispatched and "the user chose papajohns.ru"
          in router.prompts[1])
    cc = real_cc({DODO: [{"idx": 7, "tag": "button", "role": "button",
                          "text": "В корзину за 408 ₽",
                          "ctx": "Пепперони 30 см"}]}, url=DODO)
    r = TaskAgent(cc, memory_path=mem_live.parent / "o2.json").start(
        "o2", "закажи пиццу на papajohns.ru", ScriptedRouter([
            {"action": "click", "n": 1}, {"action": "fail", "message": "x"}]),
        user_id="A")
    check("M1: вкладка человека — dodopizza, цель — papajohns: «В корзину» "
          "на dodopizza не нажато, вопрос о смене сайта",
          not cc.dispatched and "Сейчас открыт dodopizza.ru, а выбран "
          "papajohns.ru" in r)
    mem_o3 = mem_live.parent / "o3.json"
    mem_o3.write_text(json.dumps({"o3": [
        {"ts": 1, "goal": "закажи пиццу", "sites": ["dodopizza.ru"], "qa": [],
         "result": "Дошёл до оплаты", "ok": True,
         "brief": {"site": "dodopizza.ru", "items": []}}]},
        ensure_ascii=False), encoding="utf-8")
    cc = real_cc({DODO: [{"idx": 7, "tag": "a", "role": "link",
                          "text": "Пиццы"}]}, url=DODO)
    agent = TaskAgent(cc, memory_path=mem_o3)
    router = ScriptedRouter([{"action": "click", "n": 1},
                             {"action": "click", "n": 1},
                             {"action": "fail", "message": "стоп"}])
    agent.start("o3", "закажи пиццу", router, user_id="A")
    agent.feed("o3", "другой сайт", router, user_id="A")
    check("M1: «другой сайт» на «как в прошлый раз — на dodopizza.ru?» — "
          "на вкладке dodopizza не действует",
          not cc.dispatched and "the user declined ordering on dodopizza.ru"
          in router.prompts[-1])
    cc = ShopCC()
    cc.sizes = [("25 см", 1), ("30 см", 0), ("35 см", 0)]
    agent = TaskAgent(cc)
    agent.slot_router = SlotRouter([{"items": [{"name": "Пепперони"}]}])
    router = ScriptedRouter([
        {"action": "click", "n": 1},
        {"action": "ask", "question": "Правильно понял: 35 см (сейчас выбран "
                                      "25 см), тонкое?"},
        {"action": "click", "n": 1},
        {"action": "fail", "message": "стоп"}])
    agent.start("o4", "закажи пепперони на pizza.test", router, user_id="A")
    agent.feed("o4", "35, тонкое", router, user_id="A")
    agent.feed("o4", "да", router, user_id="A")
    check("M2: «да» на уточнение модели «35 см?» — не «как есть»: 25 см не "
          "положен", not cc.executed)

    # ── Перепроверка 3: вкладка человека при выбранном другом сайте — без
    # вопроса только ссылки; сайт — в вопросе о заказе ──
    SH = "https://shop.test/"
    cc = real_cc({SH: [{"idx": 3, "tag": "button", "role": "button",
                        "text": "Добавить", "ctx": "Пепперони 30 см 408 ₽"},
                       {"idx": 4, "tag": "a", "role": "link",
                        "text": "Папа Джонс", "href": PJ}],
                  PJ: []}, url=SH, clicks={4: PJ})
    r = TaskAgent(cc, memory_path=mem_live.parent / "k1.json").start(
        "k1", "закажи пиццу на papajohns.ru", ScriptedRouter([
            {"action": "click", "n": 1}, {"action": "fail", "message": "x"}]),
        user_id="A")
    check("K1: вкладка shop.test, выбран papajohns — «Добавить» не нажато, "
          "вопрос о смене сайта",
          not cc.dispatched and "Сейчас открыт shop.test, а выбран "
          "papajohns.ru" in r)
    ag_k2 = TaskAgent(FakeCC())
    q_k2 = ag_k2._commit_question({"page_host": "www.shop.test",
                                   "chat_id": "k2"},
                                  {"items": ["Пепперони"], "total": 408.0},
                                  "Оформить заказ")
    check("K2: вопрос о заказе называет сайт",
          q_k2.startswith("Оформляю заказ: сайт shop.test; Пепперони"))

    # ── Живой прогон 18:11: вкладка осталась на Додо от прошлой задачи, в
    # конфиге алиас «пицца: dodopizza.ru» — открыл Додо без вопроса;
    # модель сама перещёлкала размеры и тесто до вопроса о товаре ──
    ta.web_search_links = lambda q, **kw: (found_live, None)
    try:
        LEFT = DODO + "/geodezicheskaya41/product/chiken-bomboni"
        cc = real_cc({LEFT: [{"idx": 7, "tag": "a", "role": "link",
                              "text": "Пиццы"}], DODO: []}, url=LEFT)
        cc.sites = {"пицца": "dodopizza.ru", "додо пицца": "dodopizza.ru"}
        router = ScriptedRouter([
            {"action": "search", "query": "заказать пиццу с доставкой"},
            {"action": "open", "target": DODO},
            {"action": "fail", "message": "стоп"}])
        r = TaskAgent(cc, memory_path=mem_live.parent / "lv18.json").start(
            "lv18", "закажи пиццу", router, user_id="A")
        check("живой 18:11: алиас и оставшаяся вкладка — не выбор: результат "
              "поиска не открыт, вопрос «на каком сайте?» с выдачей",
              "На каком сайте это сделать?" in r and "- dodopizza.ru" in r
              and not [d for d in cc.dispatched if d.get("kind") == "url"])
    finally:
        ta.web_search_links = real_search
    win = [{"idx": 1, "tag": "button", "role": "button", "text": "Закрыть",
            "md": 1},
           {"idx": 2, "tag": "label", "role": "", "text": "20 см", "md": 1,
            "on": 0},
           {"idx": 3, "tag": "label", "role": "", "text": "30 см", "md": 1,
            "on": 1},
           {"idx": 4, "tag": "label", "role": "", "text": "Тонкое", "md": 1,
            "on": 0},
           {"idx": 5, "tag": "button", "role": "button",
            "text": "В корзину за 579 ₽", "md": 1,
            "ctx": "Чикен бомбони 30 см, традиционное тесто"}]
    cc = real_cc({P1: win + [hdr("0 ₽")]}, url=P1)
    agent = TaskAgent(cc, memory_path=mem_live.parent / "lv18b.json")
    router = ScriptedRouter([
        '{"action":"click","n":2}\n{"action":"click","n":4}',
        {"action": "click", "n": 5},
        {"action": "click", "n": 2},
        {"action": "fail", "message": "стоп"}])
    r = agent.start("lv18b", "закажи чикен бомбони на pizza.test", router,
                    user_id="A")
    check("живой 18:11: размер и тесто до вопроса о товаре не щёлкаются — "
          "модели «нажми В корзину, система спросит»; вопрос — с размером "
          "сайта (30 см), а не выбранным моделью",
          not [d for d in cc.dispatched if d.get("kind") == "click"]
          and "do not pick sizes or options before the user" in router.prompts[1]
          and "Сейчас выбран 30 см." in r)
    agent.feed("lv18b", "20 см", router, user_id="A")
    check("живой 18:11: после ответа «20 см» — размер выбран",
          [d.get("idx") for d in cc.dispatched if d.get("kind") == "click"]
          == [2])

    # ── Разделы магазина: что заказать, не сказано — разделы сайта, затем
    # выбранный раздел (до 10 — списком, больше — коротко; «покажи все») ──
    class BRouter:
        # slot_router: разделы / показ раздела — текст, разбор ответа — JSON
        def __init__(self, sections, show, slots=()):
            self.sections, self.show = sections, show
            self.slots, self.prompts = list(slots), []

        def get_response(self, messages, **kw):
            p = messages[-1]["content"]
            self.prompts.append(p)
            if "sections of the site's catalog" in p:
                return self.sections
            if "Present it:" in p:
                return self.show
            return json.dumps(self.slots.pop(0) if self.slots else {},
                              ensure_ascii=False)

    HOME, PIZ = "https://pizza.test/", "https://pizza.test/pizzas"
    nav = [{"idx": 1, "tag": "a", "role": "link", "text": "Пиццы"},
           {"idx": 2, "tag": "a", "role": "link", "text": "Комбо"},
           {"idx": 3, "tag": "a", "role": "link", "text": "Закуски"},
           {"idx": 4, "tag": "a", "role": "link", "text": "Напитки"},
           {"idx": 5, "tag": "button", "role": "button", "text": "Войти"},
           hdr("0 ₽")]
    pizzas = [{"idx": 10 + i, "tag": "a", "role": "link",
               "text": f"{n} от {p} ₽"} for i, (n, p) in enumerate(
                   [("Пепперони", 349), ("Сырная", 279), ("Терияки", 359)])]
    saved_goal_b = _ba.snapshot_for_goal
    _ba.snapshot_for_goal = lambda host, text, tab_id=None: (PIZ, [])
    try:
        cc = real_cc({HOME: nav, PIZ: nav + pizzas}, url=HOME,
                     clicks={1: PIZ})
        agent = TaskAgent(cc, memory_path=mem_live.parent / "br1.json")
        br = BRouter("Что посмотрим?\n- Пиццы\n- Комбо\n- Закуски\n- Напитки\n"
                     "- Десерты\n- Войти",
                     "В разделе три пиццы:\n- Пепперони — от 349 ₽\n- Сырная "
                     "— от 279 ₽\n- Терияки — от 359 ₽\nКакую заказываем?")
        agent.slot_router = br
        router = ScriptedRouter([{"action": "fail", "message": "стоп"}])
        r = agent.start("br1", "закажи пиццу на pizza.test", router,
                        user_id="A")
        check("разделы: на сайте, товар не назван — разделы со страницы "
              "(«Десерты» не со страницы и «Войти» — мимо), модель шагов не "
              "звалась",
              "На pizza.test есть разделы:\n- Пиццы\n- Комбо\n- Закуски\n"
              "- Напитки\nЧто посмотрим?" in r and "Десерты" not in r
              and "Войти" not in r.split("\n", 1)[1] and not router.prompts)
        r = agent.feed("br1", "пиццы", router, user_id="A")
        check("разделы: «пиццы» — раздел открыт кодом, показан списком от "
              "модели (до 10 позиций)",
              [d.get("idx") for d in cc.dispatched
               if d.get("kind") == "click"] == [1]
              and "- Пепперони — от 349 ₽" in r and r.endswith(
                  "Какую заказываем?") and not router.prompts)
        # «покажи все» после показа, где модель выдумала позицию, — список
        # кодом (позиции страницы)
        cc = real_cc({HOME: nav, PIZ: nav + pizzas}, url=HOME,
                     clicks={1: PIZ})
        agent = TaskAgent(cc, memory_path=mem_live.parent / "br2.json")
        agent.slot_router = BRouter(
            "- Пиццы\n- Комбо", "- Гавайская — 500 ₽\nКакую?")
        agent.start("br2", "закажи пиццу на pizza.test", router, user_id="A")
        r = agent.feed("br2", "1", router, user_id="A")
        check("разделы: номер раздела; показ с выдуманной позицией "
              "(«Гавайская») — список кодом со страницы",
              "Гавайская" not in r and "В разделе «Пиццы»:\n- Пепперони от "
              "349 ₽\n- Сырная от 279 ₽\n- Терияки от 359 ₽\nЧто "
              "заказываем?" in r)
        r = agent.feed("br2", "покажи все", router, user_id="A")
        check("разделы: «покажи все» — весь раздел списком",
              "- Терияки от 359 ₽" in r and "Что заказываем?" in r)
        # Товар назван в ответе на разделы — дальше модель шагов
        cc = real_cc({HOME: nav, PIZ: nav + pizzas}, url=HOME)
        agent = TaskAgent(cc, memory_path=mem_live.parent / "br3.json")
        agent.slot_router = BRouter("- Пиццы\n- Комбо", "x", slots=[
            {}, {"items": [{"name": "Пепперони"}]}])
        router = ScriptedRouter([{"action": "fail", "message": "стоп"}])
        agent.start("br3", "закажи пиццу на pizza.test", router, user_id="A")
        agent.feed("br3", "хочу пепперони", router, user_id="A")
        check("разделы: в ответе назван товар — раздел не открывается, "
              "дальше модель шагов",
              len(router.prompts) == 1 and not cc.dispatched)
        # Товар назван в цели — разделов нет
        agent = TaskAgent(real_cc({HOME: nav}, url=HOME),
                          memory_path=mem_live.parent / "br4.json")
        br4 = BRouter("- Пиццы\n- Комбо", "x",
                      slots=[{"items": [{"name": "Пепперони"}]}])
        agent.slot_router = br4
        router = ScriptedRouter([{"action": "fail", "message": "стоп"}])
        r = agent.start("br4", "закажи пепперони на pizza.test", router,
                        user_id="A")
        check("разделы: товар назван в цели — без вопроса о разделах",
              "есть разделы" not in r and router.prompts
              and not any("sections of the site's catalog" in p
                          for p in br4.prompts))
        # Большой раздел — коротко от модели; «покажи все» — список; ответ
        # «пиццы», записанный разбором в позиции, — всё равно раздел
        many = [{"idx": 30 + i, "tag": "a", "role": "link",
                 "text": f"Пицца {i} от {300 + i} ₽"} for i in range(14)]
        cc = real_cc({HOME: nav, PIZ: nav + many}, url=HOME, clicks={1: PIZ})
        agent = TaskAgent(cc, memory_path=mem_live.parent / "br5.json")
        summary = ("В разделе 14 пицц, от 300 до 313 ₽: например, Пицца 1, "
                   "Пицца 7. Назови, какую, или скажи «покажи все».")
        agent.slot_router = BRouter("- Пиццы\n- Комбо", summary, slots=[
            {}, {"items": [{"name": "Пиццы"}]}])
        agent.start("br5", "закажи пиццу на pizza.test", router, user_id="A")
        r = agent.feed("br5", "пиццы", router, user_id="A")
        check("разделы: «пиццы» (разбор записал как позицию) — раздел; "
              "больше 10 позиций — коротко от модели",
              r.endswith(summary) and [d.get("idx") for d in cc.dispatched
                                       if d.get("kind") == "click"] == [1])
        r = agent.feed("br5", "покажи все", router, user_id="A")
        check("разделы: «покажи все» — все 14 позиций со страницы",
              "- Пицца 0 от 300 ₽" in r and "- Пицца 13 от 313 ₽" in r
              and "Что заказываем?" in r)
        # ── Проверка review-browse ──
        # 1: сайт назван словом («в Додо») — не разделы вкладки новостей
        NEWS = "https://news.test/"
        news = [{"idx": i, "tag": "a", "role": "link", "text": x}
                for i, x in enumerate(["Россия", "Мир", "Спорт"], 1)]
        cc = real_cc({NEWS: news}, url=NEWS)
        agent = TaskAgent(cc, memory_path=mem_live.parent / "rb1.json")
        agent.slot_router = BRouter("- Россия\n- Мир\n- Спорт", "x",
                                    slots=[{"site": "Додо Пицца"}])
        r = agent.start("rb1", "закажи пиццу в додо", ScriptedRouter([
            {"action": "fail", "message": "стоп"}]), user_id="A")
        check("review-browse 1: сайт назван словом — разделов вкладки "
              "новостей нет", "есть разделы" not in r and not cc.dispatched)
        # 2, 3: отказ («без комбо», «кроме пиццы») и товар раздела («Ролл
        # Филадельфия» ≠ «Роллы») — раздел не открывается
        rolls = nav[:4] + [{"idx": 6, "tag": "a", "role": "link",
                            "text": "Роллы"}]
        for ans, slot in (("без комбо", {}), ("что угодно кроме пиццы", {}),
                          ("ролл филадельфия",
                           {"items": [{"name": "Ролл Филадельфия"}]})):
            cc = real_cc({HOME: rolls, PIZ: rolls + pizzas}, url=HOME,
                         clicks={1: PIZ})
            agent = TaskAgent(cc, memory_path=mem_live.parent / "rb2.json")
            agent.slot_router = BRouter("- Пиццы\n- Комбо\n- Роллы", "x",
                                        slots=[{}, slot])
            rt = ScriptedRouter([{"action": "fail", "message": "стоп"}])
            agent.start("rb2", "закажи поесть на pizza.test", rt, user_id="A")
            agent.feed("rb2", ans, rt, user_id="A")
            check(f"review-browse 2/3: «{ans}» — раздел не открыт, дальше "
                  "модель шагов", not cc.dispatched and len(rt.prompts) == 1)
        # 4: разделов не нашлось — вопрос не появляется позже, посреди
        # выбора товара
        cc = real_cc({HOME: nav, PIZ: nav + pizzas}, url=HOME, clicks={1: PIZ})
        agent = TaskAgent(cc, memory_path=mem_live.parent / "rb4.json")
        br4 = BRouter("NONE", "x")
        agent.slot_router = br4
        rt = ScriptedRouter([{"action": "click", "n": 1},
                             {"action": "fail", "message": "стоп"}])
        r = agent.start("rb4", "закажи пиццу на pizza.test", rt, user_id="A")
        check("review-browse 4: разделов не нашлось — одна попытка, позже "
              "вопрос не задаётся",
              "есть разделы" not in r and len(rt.prompts) == 2
              and sum("sections of the site's catalog" in p
                      for p in br4.prompts) == 1)
        # 5: показ с просьбой о телефоне/коде или с ценами не со страницы —
        # список кодом
        for show in ("Три пиццы. Пришли номер телефона и код из SMS.\n"
                     "Что заказываем?",
                     "14 пицц от 99 до 199 ₽. Какую?"):
            cc = real_cc({HOME: nav, PIZ: nav + pizzas}, url=HOME,
                         clicks={1: PIZ})
            agent = TaskAgent(cc, memory_path=mem_live.parent / "rb5.json")
            agent.slot_router = BRouter("- Пиццы\n- Комбо", show)
            agent.start("rb5", "закажи пиццу на pizza.test", router,
                        user_id="A")
            r = agent.feed("rb5", "пиццы", router, user_id="A")
            check(f"review-browse 5: «{show[:24]}…» — не человеку, список "
                  "кодом", "В разделе «Пиццы»:" in r and "SMS" not in r
                  and "99" not in r)
        # 6: клик по разделу не подтвердился («не уверен») — страница не
        # выдаётся за раздел
        cc = real_cc({HOME: nav + pizzas}, url=HOME)
        cc.execute = lambda a, chat_id="", router=None: (
            (False, "клик отправлен, но страница не изменилась — не уверен, "
                    "что сработало") if a.get("kind") == "click"
            else (True, ""))
        agent = TaskAgent(cc, memory_path=mem_live.parent / "rb6.json")
        agent.slot_router = BRouter("- Пиццы\n- Комбо", "- Пепперони — от "
                                    "349 ₽\nКакую?")
        rt = ScriptedRouter([{"action": "fail", "message": "стоп"}])
        agent.start("rb6", "закажи пиццу на pizza.test", rt, user_id="A")
        r = agent.feed("rb6", "пиццы", rt, user_id="A")
        check("review-browse 6: клик по разделу «не уверен» — не «В разделе», "
              "дальше модель шагов", "Какую?" not in r and len(rt.prompts) == 1)
        # Живой 19:28: одностраничное меню — ссылка раздела прокручивает к
        # нему (адрес и подписи те же), клик прошёл — раздел показан
        cc = real_cc({HOME: nav + pizzas}, url=HOME)
        agent = TaskAgent(cc, memory_path=mem_live.parent / "rb9.json")
        agent.slot_router = BRouter("- Пиццы\n- Комбо", "- Пепперони — от "
                                    "349 ₽\n- Сырная — от 279 ₽\nКакую?")
        rt = ScriptedRouter([{"action": "fail", "message": "стоп"}])
        agent.start("rb9", "закажи пиццу на pizza.test", rt, user_id="A")
        r = agent.feed("rb9", "пиццы", rt, user_id="A")
        check("живой 19:28: одностраничное меню (прокрутка к разделу) — раздел "
              "показан, модель шагов не звалась",
              r.endswith("Какую?") and not rt.prompts
              and [d.get("idx") for d in cc.dispatched
                   if d.get("kind") == "click"] == [1])
        # Живой 19:28: товар не назван — карточку («Пепперони от 349 ₽»)
        # модель сама не открывает; названный — можно
        rt = ScriptedRouter([{"action": "click", "n": 7},
                             {"action": "fail", "message": "стоп"}])
        r = agent.feed("rb9", "на твой вкус", rt, user_id="A")
        check("живой 19:28: «на твой вкус» — карточка товара не нажата, модели "
              "«спроси, какой, со списком»",
              [d.get("idx") for d in cc.dispatched
               if d.get("kind") == "click"] == [1]
              and "the user has not chosen an item yet" in rt.prompts[1])
        cc = real_cc({HOME: nav + pizzas}, url=HOME)
        rt = ScriptedRouter([{"action": "click", "n": 7},
                             {"action": "fail", "message": "стоп"}])
        TaskAgent(cc, memory_path=mem_live.parent / "rb10.json").start(
            "rb10", "закажи пепперони на pizza.test", rt, user_id="A")
        check("живой 19:28: товар назван человеком — его карточка нажимается",
              [d.get("idx") for d in cc.dispatched
               if d.get("kind") == "click"] == [10])
        # 7: цель — оформить собранную корзину — разделов нет
        cc = real_cc({HOME: nav}, url=HOME)
        agent = TaskAgent(cc, memory_path=mem_live.parent / "rb7.json")
        br7 = BRouter("- Пиццы\n- Комбо", "x")
        agent.slot_router = br7
        r = agent.start("rb7", "оформи заказ на pizza.test, корзина уже "
                        "собрана", ScriptedRouter([
                            {"action": "fail", "message": "стоп"}]),
                        user_id="A")
        check("review-browse 7: «корзина уже собрана» — без разделов",
              "есть разделы" not in r and not any(
                  "sections of the site's catalog" in p for p in br7.prompts))
        # 8: правило промпта о разделах — только пока они в ходу
        check("review-browse 8: правило «система спросила о разделах» — "
              "только пока разделы в ходу",
              "asked which section" not in agent._prompt(
                  {"goal": "x", "qa": [], "history": [], "lang": "ru",
                   "browse": {"stage": "done"}},
                  {"url": "", "host": None, "shown": [], "note": None,
                   "text": None, "error": "none", "search": None})
              and "asked which section" in agent._prompt(
                  {"goal": "x", "qa": [], "history": [], "lang": "ru",
                   "browse": {"stage": "presented"}},
                  {"url": "", "host": None, "shown": [], "note": None,
                   "text": None, "error": "none", "search": None}))
        # Живой 20:18: вкладка осталась на Додо от прошлой задачи, сайт
        # выбран названием («Додо пицца», без адреса) — разделы не
        # спрашивались; название → адрес: алиас конфига, транслит
        DD = "https://dodopizza.ru"
        LEFT = DD + "/city/geodezicheskaya41/product/pepperoni-tomat"
        cc = real_cc({LEFT: [{"idx": 9, "tag": "button", "role": "button",
                              "text": "Закрыть", "md": 1}], DD: nav},
                     url=LEFT)
        cc.sites = {"пицца": "dodopizza.ru", "додо пицца": "dodopizza.ru"}
        agent = TaskAgent(cc, memory_path=mem_live.parent / "d2018.json")
        agent.slot_router = BRouter("- Пиццы\n- Комбо\n- Закуски", "x",
                                    slots=[{}, {"site": "Додо Пицца"}])
        rt = ScriptedRouter([
            {"action": "ask", "question": "На каком сайте заказать пиццу?\n"
                                          "- Додо Пицца\n- Папа Джонс"},
            {"action": "open", "target": DD},
            {"action": "fail", "message": "стоп"}])
        agent.start("d2018", "закажи пиццу", rt, user_id="A")
        r = agent.feed("d2018", "Додо пицца", rt, user_id="A")
        if "Делаю?" in r:
            r = agent.feed("d2018", "да", rt, user_id="A")
        check("живой 20:18: сайт выбран названием, вкладка осталась на нём — "
              "после открытия вопрос о разделах",
              "На dodopizza.ru есть разделы:" in r)
        # Живой 23:52: «где?» без вариантов, ответ «додо пицца», модель
        # открыла сайт названием — название шло за адрес: «„Додо Пицца“ —
        # это додо пицца?», «да» записало его выбранным сайтом, на
        # dodopizza.ru разделов не было, дальше «открыт dodopizza.ru, а
        # выбран додо пицца»
        cc = real_cc({DD: nav})
        cc.sites = {"пицца": DD, "додо пицца": DD}
        agent = TaskAgent(cc, memory_path=mem_live.parent / "d2352.json")
        agent.slot_router = BRouter("- Пиццы\n- Комбо\n- Закуски", "x",
                                    slots=[{}, {"site": "Додо Пицца"}])
        rt = ScriptedRouter([
            {"action": "ask", "question": "Где заказать пиццу? Могу "
                                          "поискать варианты — назовите "
                                          "город или предпочтения"},
            {"action": "open", "target": "додо пицца"},
            {"action": "fail", "message": "стоп"}])
        agent.start("d2352", "закажи пиццу", rt, user_id="A")
        r = agent.feed("d2352", "додо пицца", rt, user_id="A")
        check("живой 23:52: открытие названием — без «это додо пицца?», "
              "открыт dodopizza.ru, сразу разделы",
              "это додо пицца" not in r and "На dodopizza.ru есть разделы:"
              in r and [d.get("value") for d in cc.dispatched
                        if d.get("kind") == "url"] == [DD])
        if "Заказываем здесь?" in r:
            agent.feed("d2352", "да", rt, user_id="A")  # как в живом
        run_d = agent._runs.get("d2352") or {}
        # Словарь — до _site_choice (она сама дописывает сверенное)
        names_d = dict(run_d.get("site_names") or {})
        check("живой 23:52: dodopizza.ru — выбранный сайт (название сверено "
              "с адресом), не «а выбран додо пицца»",
              names_d.get("додо пицца") == "dodopizza.ru"
              and agent._site_choice(run_d, "dodopizza.ru") == "ok")
        # Название, которое резолв ведёт на другой адрес, — «это X?» с
        # адресом, не с названием
        cc = real_cc({"https://rolls.test": nav})
        cc.sites = {"роллы": "https://rolls.test"}
        agent = TaskAgent(cc, memory_path=mem_live.parent / "d2352b.json")
        agent.slot_router = SlotRouter([{}, {"site": "Якитория"}])
        rt = ScriptedRouter([
            {"action": "ask", "question": "Где заказать роллы?"},
            {"action": "open", "target": "роллы"},
            {"action": "fail", "message": "стоп"}])
        agent.start("d2352b", "закажи роллы", rt, user_id="A")
        r = agent.feed("d2352b", "якитория", rt, user_id="A")
        check("живой 23:52: название ведёт на другой адрес — «„Якитория“ — "
              "это rolls.test?», не открыт",
              "«Якитория» — это rolls.test? Заказываем здесь?" in r
              and not cc.dispatched)
        al = {"пицца": "dodopizza.ru", "додо пицца": "dodopizza.ru"}
        check("название → адрес: алиас, транслит; чужое и общее — нет",
              ta._name_fits_host("Додо Пицца", "dodopizza.ru", al)
              and ta._name_fits_host("Папа Джонс", "papajohns.ru")
              and ta._name_fits_host("Pizza Hut", "www.pizzahut.ru")
              and not ta._name_fits_host("Папа Джонс", "dodopizza.ru", al)
              and not ta._name_fits_host("Пиццерия", "pizzeria-best.ru"))
        # Чётких разделов нет, а товары на странице есть — показ страницы
        # тем же правилом (до 10 — списком)
        cc = real_cc({HOME: pizzas}, url=HOME)
        agent = TaskAgent(cc, memory_path=mem_live.parent / "np.json")
        agent.slot_router = BRouter("NONE", "- Пепперони — от 349 ₽\n- "
                                    "Сырная — от 279 ₽\n- Терияки — от 359 "
                                    "₽\nКакую заказываем?")
        rt = ScriptedRouter([{"action": "fail", "message": "стоп"}])
        r = agent.start("np", "закажи пиццу на pizza.test", rt, user_id="A")
        check("разделов нет — страница показана тем же правилом (список от "
              "модели), модель шагов не звалась",
              r.endswith("Какую заказываем?") and "- Сырная — от 279 ₽" in r
              and not rt.prompts)
        cc = real_cc({HOME: pizzas}, url=HOME)
        agent = TaskAgent(cc, memory_path=mem_live.parent / "np2.json")
        agent.slot_router = BRouter("NONE", "- Гавайская — 500 ₽\nКакую?")
        r = agent.start("np2", "закажи пиццу на pizza.test", rt, user_id="A")
        check("разделов нет, показ модели не со страницы — список кодом «На "
              "pizza.test:»", "На pizza.test:\n- Пепперони от 349 ₽" in r
              and "Гавайская" not in r)
        # Живой 00:16: ссылки разделов Додо стояли в начале списка, а модель
        # разделов не назвала — показан обзор всей страницы. Запас —
        # заголовки разделов по разметке с такой же подписью на странице
        _ba.section_names = lambda *a, **k: ["Пиццы", "Комбо", "Соусы"]
        try:
            for reply in ("NONE", "Разделы: пиццы, комбо и напитки."):
                cc = real_cc({HOME: nav + pizzas}, url=HOME)
                agent = TaskAgent(cc, memory_path=mem_live.parent / "s16.json")
                agent.slot_router = BRouter(reply, "x")
                rt = ScriptedRouter([{"action": "fail", "message": "стоп"}])
                r = agent.start("s16", "закажи пиццу на pizza.test", rt,
                                user_id="A")
                check(f"живой 00:16 («{reply[:12]}»): модель разделов не "
                      "назвала — разделы по разметке, только те, что есть "
                      "на странице",
                      "На pizza.test есть разделы:\n- Пиццы\n- Комбо\nЧто "
                      "посмотрим?" in r and "Соусы" not in r
                      and not rt.prompts)
        finally:
            _ba.section_names = lambda *a, **k: None
        # Тест gemma 01.10: ответ «Пиццы» при разделах «Пиццы» и «Римские
        # пиццы» (Додо) не выбирал раздел — подходили оба
        bp = {"stage": "asked", "sections": ["Пиццы", "Комбо",
                                             "Римские пиццы"]}
        check("gemma 01.10: «Пиццы» — раздел «Пиццы», хоть слово есть и в "
              "«Римские пиццы»; «римские пиццы» — свой; номер — как был",
              TaskAgent._picked_section(bp, "Пиццы") == "Пиццы"
              and TaskAgent._picked_section(bp, "давай пиццы") == "Пиццы"
              and TaskAgent._picked_section(bp, "римские пиццы")
              == "Римские пиццы"
              and TaskAgent._picked_section(bp, "3") == "Римские пиццы")
        # gemma3 записала позицией слово цели («закажи пиццу» → «пицца») —
        # вопрос о разделах пропадал: позиция из родовых слов — вид
        cc = real_cc({HOME: nav + pizzas}, url=HOME)
        agent = TaskAgent(cc, memory_path=mem_live.parent / "gm1.json")
        agent.slot_router = BRouter("- Пиццы\n- Комбо", "x", slots=[
            {"items": [{"name": "пицца", "size": None, "qty": 1}]}])
        r = agent.start("gm1", "закажи пиццу на pizza.test", ScriptedRouter(
            [{"action": "fail", "message": "стоп"}]), user_id="A")
        run_g = agent._runs.get("gm1") or {}
        check("gemma 01.10: позиция «пицца» из цели — вид, не товар: бриф "
              "пуст, вопрос о разделах на месте",
              "На pizza.test есть разделы:" in r
              and not (run_g.get("brief") or {}).get("items"))
        agent.slot_router = SlotRouter([{"items": [
            {"name": "Пицца Пепперони"}, {"name": "суши"}]}])
        ch_g = agent._update_brief(run_g, "Что заказать?", "пепперони и суши")
        check("gemma 01.10: «Пицца Пепперони» — товар, «суши» — вид",
              [x["name"] for x in run_g["brief"]["items"]]
              == ["Пицца Пепперони"] and ch_g.get("kinds") == ["суши"])
    finally:
        _ba.snapshot_for_goal = saved_goal_b

    # ── Названия сайтов человека: «якитория» = открытый yakitoria.test —
    # сверено (алиас, транслит, заголовок выдачи) или «да» на «это X?»;
    # запоминается для следующих задач ──
    YAK = "https://yakitoria.test/"
    ta.web_search_links = lambda q, **kw: ([
        {"title": "Доставка роллов в �городе", "snippet": "",
         "url": YAK}], None)
    try:
        mem_n = mem_live.parent / "names.json"
        cc = real_cc({YAK: [{"idx": 1, "tag": "a", "role": "link",
                             "text": "Роллы"}]})
        agent = TaskAgent(cc, memory_path=mem_n)
        agent.slot_router = SlotRouter([{}, {"site": "Якитория"}])
        rt = ScriptedRouter([
            {"action": "search", "query": "роллы доставка"},
            {"action": "ask", "question": "Где заказать?\n- Якитория\n"
                                          "- Тануки"},
            {"action": "open", "target": YAK},
            {"action": "fail", "message": "стоп"}])
        agent.start("nm", "закажи роллы", rt, user_id="A")
        r = agent.feed("nm", "якитория", rt, user_id="A")
        check("названия: «Якитория» с адресом не сверить — до открытия "
              "«„Якитория“ — это yakitoria.test?», не открыт",
              "«Якитория» — это yakitoria.test? Заказываем здесь?" in r
              and not cc.dispatched)
        agent.feed("nm", "да", rt, user_id="A")
        memj = json.loads(mem_n.read_text(encoding="utf-8"))
        check("названия: «да» — открыт и запомнен «якитория» = "
              "yakitoria.test",
              [d.get("value") for d in cc.dispatched
               if d.get("kind") == "url"] == [YAK]
              and memj["nm"][-1].get("site_names") == {
                  "якитория": "yakitoria.test"})
        cc = real_cc({YAK: [{"idx": 1, "tag": "a", "role": "link",
                             "text": "Роллы"}]})
        agent = TaskAgent(cc, memory_path=mem_n)
        agent.slot_router = SlotRouter([{"site": "Якитория"}])
        r = agent.start("nm", "закажи роллы в якитории", ScriptedRouter([
            {"action": "search", "query": "роллы"},
            {"action": "open", "target": YAK},
            {"action": "fail", "message": "стоп"}]), user_id="A")
        check("названия: следующая задача — «якитория» известна, открыт без "
              "вопросов", "это yakitoria.test" not in r
              and [d.get("value") for d in cc.dispatched
                   if d.get("kind") == "url"] == [YAK])
    finally:
        ta.web_search_links = real_search
    check("названия: служебные слова ответа — не часть названия",
          ta._norm_name("Давай Додо пиццу!") == "додо пиццу")

    # ── Живой прогон 21:05: на ответ «25, традиционное, бекон» модель одной
    # цепочкой нажала «25 см» и «Тонкое» — опцию, которой в ответе нет ──
    win5 = [{"idx": 1, "tag": "button", "role": "button", "text": "Закрыть",
             "md": 1},
            {"idx": 2, "tag": "label", "role": "", "text": "25 см", "md": 1,
             "on": 0},
            {"idx": 3, "tag": "label", "role": "", "text": "30 см", "md": 1,
             "on": 1},
            {"idx": 4, "tag": "label", "role": "", "text": "Традиционное",
             "md": 1, "on": 1},
            {"idx": 5, "tag": "label", "role": "", "text": "Тонкое", "md": 1,
             "on": 0},
            {"idx": 6, "tag": "button", "role": "button", "text": "Бекон 99 ₽",
             "md": 1, "on": 0},
            {"idx": 7, "tag": "button", "role": "button",
             "text": "В корзину за 579 ₽", "md": 1,
             "ctx": "Чикен бомбони 30 см, традиционное тесто"}]
    qs5 = ("Размер: 25 или 30 см? Сейчас выбрано 30 см\nТесто: Традиционное "
           "или Тонкое? Сейчас выбрано Традиционное\nДобавить бекон 99 ₽?")
    cc = real_cc({P1: win5 + [hdr("0 ₽")]}, url=P1)
    agent = TaskAgent(cc, memory_path=mem_live.parent / "lv21.json")
    agent.slot_router = QRouter([qs5], slots=[
        {"items": [{"name": "Чикен бомбони"}]},
        {"items": [{"name": "Чикен бомбони", "size": "25 см"}]}])
    router = ScriptedRouter([
        {"action": "click", "n": 7},
        '{"action":"click","n":2}\n{"action":"click","n":5}\n'
        '{"action":"click","n":6}',
        {"action": "click", "n": 6},
        {"action": "click", "n": 5},
        {"action": "fail", "message": "стоп"}])
    r = agent.start("lv21", "закажи чикен бомбони на pizza.test", router,
                    user_id="A")
    r = agent.feed("lv21", "25, традиционное, бекон", router, user_id="A")
    clicks = [d.get("idx") for d in cc.dispatched if d.get("kind") == "click"]
    check("живой 21:05: «25 см» и «Бекон» нажаты, «Тонкое» — нет: модели — "
          "«не выбрано человеком», настаивает — вопрос человеку",
          clicks == [2, 6]
          and 'the user did not choose "Тонкое"' in router.prompts[2]
          and "Нажать «Тонкое»? В твоём ответе этого не было." in r)
    agent.feed("lv21", "нет", router, user_id="A")
    check("живой 21:05: «нет» — «Тонкое» так и не нажато",
          [d.get("idx") for d in cc.dispatched
           if d.get("kind") == "click"] == [2, 6])
    # Выбор отдан агенту — опция не из ответа нажимается без вопроса
    cc = real_cc({P1: win5 + [hdr("0 ₽")]}, url=P1)
    agent = TaskAgent(cc, memory_path=mem_live.parent / "lv21b.json")
    agent.slot_router = QRouter([qs5], slots=[
        {"items": [{"name": "Чикен бомбони"}]},
        {"items": [{"name": "Чикен бомбони", "size": "25 см"}]}])
    router = ScriptedRouter([
        {"action": "click", "n": 7},
        '{"action":"click","n":2}\n{"action":"click","n":5}',
        {"action": "fail", "message": "стоп"}])
    agent.start("lv21b", "закажи чикен бомбони на pizza.test", router,
                user_id="A")
    agent.feed("lv21b", "25, тесто на твой вкус", router, user_id="A")
    check("живой 21:05: «тесто на твой вкус» — «Тонкое» нажато без вопроса",
          [d.get("idx") for d in cc.dispatched
           if d.get("kind") == "click"] == [2, 5])

    # ── План заказа (живой прогон 21:02–21:16): позиция → «что-нибудь
    # ещё?» → просьба «какой-нибудь напиток» (снова раздел и выбор) → «нет»
    # → корзина → оформление; положенное снова не открывается ──
    class PlanRouter:
        # slot_router: разделы, показы разделов по очереди, «что спросить?»
        # — NONE, разбор ответов — JSON по очереди
        def __init__(self, sections, shows, slots):
            self.sections, self.shows = sections, list(shows)
            self.slots, self.prompts = list(slots), []

        def get_response(self, messages, **kw):
            p = messages[-1]["content"]
            self.prompts.append(p)
            if "sections of the site's catalog" in p:
                return self.sections
            if "Present it:" in p:
                return self.shows.pop(0) if self.shows else ""
            if "What do you need to ask the user before this step?" in p:
                return "NONE"
            return json.dumps(self.slots.pop(0) if self.slots else {},
                              ensure_ascii=False)

    H, PZ, PP = ("https://shop.test/", "https://shop.test/pizzas",
                 "https://shop.test/p/pepperoni")
    PZ2, DR, CW = ("https://shop.test/pizzas?a", "https://shop.test/drinks",
                   "https://shop.test/p/cola")
    DR2, DRW = "https://shop.test/drinks?a", "https://shop.test/drinks?cart"
    navs = lambda s: [dict(x) for x in nav[:5]] + [hdr(s)]
    pz = [{"idx": 10, "tag": "a", "role": "link", "text": "Пепперони от 349 ₽"},
          {"idx": 11, "tag": "a", "role": "link", "text": "Сырная от 279 ₽"}]
    dr = [{"idx": 20, "tag": "a", "role": "link",
           "text": "Кола 0,5 л от 135 ₽"},
          {"idx": 21, "tag": "a", "role": "link", "text": "Морс от 99 ₽"}]
    add = lambda i, p, ctx: {"idx": i, "tag": "button", "role": "button",
                             "text": f"В корзину за {p} ₽", "md": 1,
                             "ctx": ctx}
    saved_goal_p = _ba.snapshot_for_goal
    _ba.snapshot_for_goal = lambda host, text, tab_id=None: (H, [])
    try:
        cc = real_cc({
            H: navs("0 ₽"), PZ: navs("0 ₽") + pz,
            PP: navs("0 ₽") + pz + [add(41, 349, "Пепперони 30 см")],
            PZ2: navs("3 4 9 ₽") + pz, DR: navs("3 4 9 ₽") + pz + dr,
            CW: navs("3 4 9 ₽") + pz + dr + [add(42, 135, "Кола 0,5 л")],
            DR2: navs("4 8 4 ₽") + pz + dr,
            DRW: navs("4 8 4 ₽") + pz + dr + [
                {"idx": 50, "tag": "button", "role": "button",
                 "text": "К оформлению заказа", "md": 1}]},
            url=H, clicks={1: PZ, 10: PP, 41: PZ2, 4: DR, 20: CW, 42: DR2,
                           7: DRW})
        agent = TaskAgent(cc, memory_path=mem_live.parent / "pl1.json")
        agent.slot_router = PlanRouter(
            "- Пиццы\n- Комбо\n- Закуски\n- Напитки",
            ["- Пепперони — от 349 ₽\n- Сырная — от 279 ₽\nКакую?",
             "- Кола 0,5 л — от 135 ₽\n- Морс — от 99 ₽\nКакой напиток?"],
            [{}, {}, {"items": [{"name": "Пепперони"}]}, {"kinds": ["напиток"]},
             {"items": [{"name": "Кола 0,5 л"}]}, {}])
        router = ScriptedRouter([
            {"action": "click", "n": 1, "label": "Пепперони от 349 ₽"},
            {"action": "click", "n": 1, "label": "В корзину за 349 ₽"},
            {"action": "click", "n": 1, "label": "Пепперони от 349 ₽"},
            {"action": "click", "n": 1, "label": "Кола 0,5 л от 135 ₽"},
            {"action": "click", "n": 1, "label": "В корзину за 135 ₽"},
            {"action": "click", "n": 1, "label": "Корзина"},
            {"action": "fail", "message": "стоп"}])
        agent.start("pl1", "закажи пиццу на shop.test", router, user_id="A")
        agent.feed("pl1", "пиццы", router, user_id="A")
        r = agent.feed("pl1", "пепперони", router, user_id="A")
        clicks = lambda: [d.get("idx") for d in cc.dispatched
                          if d.get("kind") == "click"]
        check("план: пицца в корзине — сразу «что-нибудь ещё?», модель "
              "дальше не звалась (не уходит в корзину и разделы сама)",
              "В корзине: Пепперони. Добавить что-нибудь ещё?" in r
              and clicks() == [1, 10, 41] and len(router.prompts) == 2)
        r = agent.feed("pl1", "добавь какой-нибудь напиток", router,
                       user_id="A")
        check("план: «какой-нибудь напиток» — раздел «Напитки» открыт кодом "
              "без вопроса о разделах, показан, выбор за человеком",
              clicks() == [1, 10, 41, 4] and "- Кола 0,5 л — от 135 ₽" in r
              and "есть разделы" not in r and len(router.prompts) == 2)
        r = agent.feed("pl1", "колу", router, user_id="A")
        check("план: в промпте — положенное ✓, текущий шаг ▶ — кола",
              "✓ 2. put \"Пепперони\" into the cart — it is in the cart"
              in router.prompts[2]
              and "▶ 3. put \"Кола 0,5 л\" into the cart" in router.prompts[2])
        check("план: уже положенную пиццу модель снова не открывает",
              "\"Пепперони\" is already in the cart" in router.prompts[3]
              and clicks().count(10) == 1)
        check("план: кола в корзине — снова «что-нибудь ещё?» с обеими",
              clicks() == [1, 10, 41, 4, 20, 42]
              and "В корзине: Пепперони, Кола 0,5 л." in r
              and "Добавить что-нибудь ещё?" in r)
        agent.feed("pl1", "нет", router, user_id="A")
        check("план: «нет» — текущий шаг «открыть корзину», затем, когда "
              "корзина открыта, — оформление",
              "▶ 5. open the cart" in router.prompts[5]
              and "✓ 5. open the cart" in router.prompts[6]
              and "▶ 6. check out" in router.prompts[6]
              and clicks()[-1] == 7)
    finally:
        _ba.snapshot_for_goal = saved_goal_p
    # Выбор вида идёт — карточку другого товара модель за человека не
    # открывает; «да» без подробностей — снова разделы; «всё» — корзина
    ag = TaskAgent(FakeCC())
    run_k = {"goal": "закажи пиццу", "qa": [], "history": [],
             "kinds": ["напиток"],
             "brief": {"items": [{"name": "Пепперони", "qty": 1}]},
             "cart_adds": [{"key": "Пепперони", "verified": True}]}
    check("план: пока выбирают «напиток» — карточка колы не открывается",
          ag._early_item(run_k, {"text": "Кола 0,5 л от 135 ₽", "tag": "a"},
                         {"shown": [], "url": ""}))
    run_y = {"goal": "закажи пиццу", "qa": [("q", "да")], "history": [],
             "browse": {"stage": "done", "sections": ["Пиццы", "Напитки"]},
             "brief": {"items": []}, "cart_adds": []}
    ag._more_answer(run_y, "да", (), {})
    run_n = {"goal": "закажи пиццу", "qa": [("q", "всё")], "history": []}
    ag._more_answer(run_n, "всё, оформляй", (), {})
    check("план: «да» на «что-нибудь ещё?» — снова вопрос о разделах; "
          "«всё, оформляй» — больше не спрашивать",
          run_y["browse"].get("again") and run_y["browse"]["stage"] is None
          and run_y["browse"]["sections"] == ["Пиццы", "Напитки"]
          and run_n.get("more_no") is True and not ag._more_due(
              dict(run_n, cart_adds=[{"key": "x", "verified": True}])))
    run_s = {"goal": "закажи пиццу", "qa": [("q", "и что-нибудь сладкое")],
             "history": [], "kinds": [],
             "browse": {"stage": "done", "again": True, "closed": True,
                        "kind": "напиток",
                        "sections": ["Пиццы", "Напитки", "Десерты"]},
             "brief": {"items": [{"name": "Пепперони"},
                                 {"name": "Кола 0,5 л"}]}}
    ag._more_answer(run_s, "и что-нибудь сладкое", (), {"kinds": ["сладкое"]})
    check("план: вторая просьба после законченного выбора («напиток») — "
          "снова раздел и выбор для «сладкое»",
          run_s["browse"].get("kind") == "сладкое"
          and not run_s["browse"].get("closed")
          and run_s["browse"]["stage"] is None and run_s["kinds"] == ["сладкое"])
    # Карточка, которая кладёт товар сама (без «В корзину»): корзина
    # выросла — добавлено, план идёт дальше
    UP, UP2 = "https://shop.test/menu", "https://shop.test/menu?morse"
    cc = real_cc({UP: navs("3 4 9 ₽") + dr, UP2: navs("4 4 8 ₽") + dr},
                 url=UP, clicks={21: UP2})
    agent = TaskAgent(cc, memory_path=mem_live.parent / "pl2.json")
    agent.slot_router = SlotRouter([{"items": [{"name": "Морс"}]}])
    r = agent.start("pl2", "закажи морс на shop.test", ScriptedRouter([
        {"action": "click", "n": 1, "label": "Морс от 99 ₽"},
        {"action": "fail", "message": "стоп"}]), user_id="A")
    check("план: клик по карточке положил товар (корзина 349 → 448) — "
          "добавлено, сразу «что-нибудь ещё?»",
          "В корзине: Морс." in r and "Добавить что-нибудь ещё?" in r)
    # Клик по карточке, открывший окно (корзина не выросла), — не добавление
    cc = real_cc({UP: navs("3 4 9 ₽") + dr, UP2: navs("3 4 9 ₽") + dr},
                 url=UP, clicks={21: UP2})
    agent = TaskAgent(cc, memory_path=mem_live.parent / "pl3.json")
    agent.slot_router = SlotRouter([{"items": [{"name": "Морс"}]}])
    r = agent.start("pl3", "закажи морс на shop.test", ScriptedRouter([
        {"action": "click", "n": 1, "label": "Морс от 99 ₽"},
        {"action": "fail", "message": "стоп"}]), user_id="A")
    check("план: карточка открыла окно, корзина та же — не добавление",
          "Добавить что-нибудь ещё?" not in r
          and not ((agent.__dict__.get("_finished", {}).get("pl3") or {})
                   .get("run") or {}).get("cart_adds"))

    # ── Живой 22:49: «Напитки» на одностраничном меню показались пиццами —
    # список шёл в порядке страницы. Раздел на той же странице: видимое
    # после прокрутки — первым, список кодом — только из видимого; позиций
    # раздела нет — показ отдаётся модели шагов, вид остаётся в плане ──
    M, MW = "https://shop2.test/", "https://shop2.test/p/pep"
    ice = {"idx": 12, "tag": "a", "role": "link",
           "text": "новинка Айс-ти инжир-бузина от 179 ₽"}

    def menu(drinks_on_screen: bool, cart: str):
        top = not drinks_on_screen
        return (navs(cart) + [dict(ice, vp=top)]
                + [dict(x, vp=top) for x in pz] + [dict(x, vp=not top)
                                                   for x in dr])

    def scroll_hook(cc_, url):
        # Клик по «Напитки» не меняет адрес — только прокрутку (vp)
        orig = cc_._dispatch

        def disp(action, router=None):
            orig(action, router)
            if action.get("kind") == "click" and action.get("idx") == 4:
                cc_.pages[url] = menu(True, "3 4 9 ₽" if "?a" in url
                                      else "0 ₽")
        cc_._dispatch = disp
    saved_goal_m = _ba.snapshot_for_goal
    _ba.snapshot_for_goal = lambda host, text, tab_id=None: (M, [])
    try:
        for show, name in (("- Кола — 999 ₽\nКакой?", "показ не со страницы"),
                           ("", "модели нет")):
            cc = real_cc({M: menu(False, "0 ₽")}, url=M)
            scroll_hook(cc, M)
            agent = TaskAgent(cc, memory_path=mem_live.parent / "sp1.json")
            br_sp = BRouter("- Пиццы\n- Напитки", show)
            agent.slot_router = br_sp
            agent.start("sp1", "закажи что-нибудь на shop2.test",
                        ScriptedRouter([{"action": "fail", "message": "x"}]),
                        user_id="A")
            r = agent.feed("sp1", "напитки", ScriptedRouter(
                [{"action": "fail", "message": "x"}]), user_id="A")
            check(f"живой 22:49 ({name}): раздел на той же странице — "
                  "список кодом только из видимого (напитки), без пицц",
                  "В разделе «Напитки»:\n- Кола 0,5 л от 135 ₽\n- Морс от 99 ₽"
                  in r and "Пепперони" not in r and "Айс-ти" not in r)
        sp_show = next(p for p in br_sp.prompts if "Present it:" in p)
        check("живой 22:49: модели — видимое после прокрутки первым и с "
              "пометкой, «только то, что относится к разделу, иначе NONE»",
              sp_show.index("- Кола 0,5 л от 135 ₽ (on screen)")
              < sp_show.index("- Пепперони от 349 ₽")
              and "reply NONE" in sp_show)
        # Модель: позиций раздела нет (NONE) — показ модели шагов; вид
        # «напиток» в плане, карточку за человека не открыть; позиция
        # названа — вид выбран
        MA = "https://shop2.test/?a"
        cc = real_cc({M: menu(False, "0 ₽"),
                      MW: menu(False, "0 ₽") + [add(41, 349,
                                                    "Пепперони 30 см")],
                      MA: menu(False, "3 4 9 ₽")},
                     url=M, clicks={10: MW, 41: MA})
        scroll_hook(cc, MA)
        agent = TaskAgent(cc, memory_path=mem_live.parent / "sp2.json")
        agent.slot_router = PlanRouter("- Пиццы\n- Напитки", ["NONE"], [
            {"items": [{"name": "Пепперони"}]}, {"kinds": ["напиток"]},
            {"items": [{"name": "Кола 0,5 л"}]}])
        router = ScriptedRouter([
            {"action": "click", "n": 1, "label": "Пепперони от 349 ₽"},
            {"action": "click", "n": 1, "label": "В корзину за 349 ₽"},
            {"action": "click", "n": 1, "label": "Кола 0,5 л от 135 ₽"},
            {"action": "ask", "question": "Какой напиток?\n- Кола 0,5 л — "
                                          "135 ₽\n- Морс — 99 ₽"},
            {"action": "click", "n": 1, "label": "Кола 0,5 л от 135 ₽"},
            {"action": "fail", "message": "стоп"}])
        agent.start("sp2", "закажи пепперони на shop2.test", router,
                    user_id="A")
        r = agent.feed("sp2", "добавь напиток", router, user_id="A")
        clicks = [d.get("idx") for d in cc.dispatched
                  if d.get("kind") == "click"]
        check("живой 22:49: позиций раздела нет — не показано чужое, модели "
              "«докрути и спроси», карточку колы за человека не открыть",
              clicks == [10, 41, 4] and "В разделе" not in r
              and "NOT shown — the items of \"Напитки\"" in router.prompts[2]
              and "the user has not chosen an item yet" in router.prompts[3]
              and "Какой напиток?" in r)
        agent.feed("sp2", "колу", router, user_id="A")
        run_sp = (agent.__dict__.get("_finished", {}).get("sp2") or {}).get(
            "run") or agent._runs.get("sp2") or {}
        check("живой 22:49: человек назвал колу — вид выбран, карточка "
              "открыта", [d.get("idx") for d in cc.dispatched
                          if d.get("kind") == "click"][-1] == 20
              and run_sp.get("kinds") == [])
        # Раздел по разметке (живая вкладка Додо: блок с заголовком h2
        # «Напитки», 21 позиция) — показаны его позиции, хотя на экране кофе
        coffee = [{"idx": 60 + i, "tag": "a", "role": "link", "text": t,
                   "vp": True} for i, t in enumerate(
                       ["Айс капучино от 239 ₽", "Кофе Капучино от 159 ₽"])]
        cc = real_cc({M: navs("0 ₽") + coffee + [dict(x, vp=False)
                                                 for x in pz + dr]}, url=M)
        _ba.section_items = lambda host, name, tab_id=None: (
            ["Кола 0,5 л от 135 ₽", "Морс от 99 ₽"] if name == "Напитки"
            else None)
        agent = TaskAgent(cc, memory_path=mem_live.parent / "sp3.json")
        br_mk = BRouter("- Пиццы\n- Напитки", "")
        agent.slot_router = br_mk
        agent.start("sp3", "закажи что-нибудь на shop2.test",
                    ScriptedRouter([{"action": "fail", "message": "x"}]),
                    user_id="A")
        r = agent.feed("sp3", "напитки", ScriptedRouter(
            [{"action": "fail", "message": "x"}]), user_id="A")
        mk_show = next(p for p in br_mk.prompts if "Present it:" in p)
        check("раздел по разметке: показаны его позиции (кола, морс), не "
              "кофе на экране и не пиццы; модели — только они",
              "В разделе «Напитки»:\n- Кола 0,5 л от 135 ₽\n- Морс от 99 ₽"
              in r and "Капучино" not in r and "Пепперони" not in r
              and "found by the page's markup" in mk_show
              and "Капучино" not in mk_show)
    finally:
        _ba.snapshot_for_goal = saved_goal_m
        _ba.section_items = lambda *a, **k: None

    # ── Живой 23:35: на «где заказать?» с выдачей ответ «додо пицца» открыл
    # pizzasinizza.ru — вариант выбирался по числу общих слов, а общим было
    # лишь «пицца» («Dodo Pizza» — латиницей) ──
    q_nsk = ("Где заказать пиццу? Вот результаты поиска:\n"
             "- Dodo Pizza (�город) — dodopizza.ru/city\n"
             "- Papa John's (�город) — papajohns.ru/city\n"
             "- Пицца Синица (�город) — pizzasinizza.ru/city\n"
             "- ST Pizza (�город) — stapizza.ru\n"
             "- Доставьевский (�город) — nsk.dostaevsky.ru/pizza\n"
             "- 888 PIZZA (�город) — 888pizza.ru")
    picks = {a: ta._site_pick(q_nsk, a) for a in (
        "додо пицца", "Доодо пицца", "додо", "синица", "папа джонс", "2",
        "пицца", "�город", "888 пицца")}
    check("живой 23:35: «додо пицца» (и с опечаткой) — dodopizza.ru, не "
          "«Пицца Синица»; «синица», «папа джонс», номер — свои",
          picks["додо пицца"] == picks["Доодо пицца"] == picks["додо"]
          == ["dodopizza.ru"] and picks["синица"] == ["pizzasinizza.ru"]
          and picks["папа джонс"] == picks["2"] == ["papajohns.ru"])
    check("живой 23:35: ответ из общих слов («пицца», город) — не выбор",
          picks["пицца"] == picks["�город"] == picks["888 пицца"] == [])
    NSK = [("Dodo Pizza (�город)", "https://dodopizza.ru/city"),
           ("Пицца Синица (�город)",
            "https://pizzasinizza.ru/city")]
    ta.web_search_links = lambda q, **kw: ([
        {"title": t, "snippet": "", "url": u} for t, u in NSK], None)
    try:
        for target, name in ((NSK[0][1], "Додо открыт"),
                             (NSK[1][1], "Синица — не открыта")):
            cc = real_cc({u: [{"idx": 1, "tag": "a", "role": "link",
                               "text": "Пиццы"}] for _t, u in NSK})
            agent = TaskAgent(cc, memory_path=mem_live.parent / "nsk.json")
            rt = ScriptedRouter([
                {"action": "search", "query": "заказать пиццу доставка"},
                {"action": "ask", "question": q_nsk.split("\n- Papa")[0]
                 + "\n- Пицца Синица (�город) — "
                   "pizzasinizza.ru/city"},
                {"action": "open", "target": target},
                {"action": "fail", "message": "стоп"}])
            agent.start("nsk", "закажи пиццу", rt, user_id="A")
            agent.feed("nsk", "додо пицца", rt, user_id="A")
            run_n = (agent.__dict__.get("_finished", {}).get("nsk") or {}
                     ).get("run") or agent._runs.get("nsk") or {}
            opened = [d.get("value") for d in cc.dispatched
                      if d.get("kind") == "url"]
            if target == NSK[0][1]:
                check(f"живой 23:35 ({name}): «додо пицца» — выбран "
                      "dodopizza.ru, открыт, в словаре «додо пицца» = он",
                      opened == [NSK[0][1]]
                      and run_n.get("site_pick") == ["dodopizza.ru"]
                      and (run_n.get("site_names") or {}).get("додо пицца")
                      == "dodopizza.ru")
            else:
                check(f"живой 23:35 ({name}): после «додо пицца» модель "
                      "открывает pizzasinizza.ru — не открыт, модели «выбран "
                      "dodopizza.ru»", not opened
                      and "the user chose dodopizza.ru" in rt.prompts[-1])
    finally:
        ta.web_search_links = real_search

    # L18: мусор от модели разбора — не падаем
    b18 = ta._new_brief()
    check("L18: «clear: true», «items: 2», «exclude» строкой — без исключения",
          ta._apply_brief(b18, {"clear": True, "items": 2,
                                "exclude": "пепперони"})
          and b18["exclude"] == ["пепперони"])

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

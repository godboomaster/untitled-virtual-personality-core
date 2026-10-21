"""Тест: блок прошлых задач в промпте шага и итог задачи в памяти.

Стенд 21.10 (run3): блок «The user's earlier tasks on the same topic» шёл в
КАЖДЫЙ шаг целиком — вопросы прошлого заказа с вариантами и ценами, ответы,
«Ended with: …» — 1,4–1,6 тыс. симв. на шаг облаку. И итог вопроса-справки
(«узнай в Пицца Тест, сколько стоит доставка»), начатого на вкладке
/checkout, в память не попал: прогон бывал на приватной странице — итог
заглушкой, хотя ответ дал облачный шаг на обычной /delivery.

Проверяет:
* похожая задача и та же тема заказа: облаку — коротко (цель, сайты, что
  заказали; без вопросов прошлой задачи и заглушки итога), в разы короче;
* «как в прошлый раз»: повторяемая запись — целиком (ответы с обычных
  страниц видны), позиции — в бриф «из памяти»;
* приватное: ответы с оформления облаку — заглушкой и в коротком, и в
  полном виде; локальной модели — как есть; секреты — маской;
* блок прошлых задач — повторяемый (StickyBlock) кусок промпта: во втором
  шаге того же треда веб-чата — ссылкой; короткий — целиком;
* итог: справка, начатая на приватной странице, с ответом облачного шага
  на обычной — в памяти и в следующей задаче; отчёт локальной модели на
  приватной странице, передача оплаты, итог с данными с приватной
  страницы — по-прежнему _PRIVATE_RESULT;
* сколько символов это сэкономило бы на облачных шагах run3.

Без браузера, веб-чатов и настоящих LLM: локальная модель — подмена
PrivateRouter.get_response, облако — ScriptedRouter, тред веб-чата —
WebChatLLM с подменённой вкладкой.

Запуск: python3 -m scripts.test_task_past_block
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.test_task_agent import FakeCC, ScriptedRouter  # noqa: E402

FAILS = 0
OK = 0

ADDR = "ул. Тестовая, 1, кв. 2"
PHONE = "+7 999 123-45-67"
PAST_HEAD = "The user's earlier tasks on the same topic"

# Запись заказа из памяти стенда run3 (вымышленный магазин): вопросы с
# вариантами и ценами, ответы, две пары с /checkout, бриф для повтора
ORDER_REC = {
    "ts": 1792519323, "goal": "закажи пиццу", "sites": ["pizzatest.ru"],
    "ok": True,
    "qa": [
        ["Где заказать пиццу? - Пицца Тест (https://pizzatest.ru/) — "
         "пепперони от 459 ₽ - Пицца Плюс (https://pizzaplus-test.ru/) — "
         "Пепперони Плюс от 499 ₽", "1"],
        ["На pizzatest.ru есть разделы: - Пицца - Комбо - Закуски - Напитки "
         "- Десерты Что посмотрим? Можно и сразу назвать, что хочешь.",
         "пепперони 30 см и колу"],
        ["Перед тем как положить «Пепперони» в корзину: Тесто: традиционное "
         "или тонкое? Сейчас выбрано традиционное. Добавки за доплату: "
         "сырный бортик 169 ₽, моцарелла 79 ₽, халапеньо 59 ₽, шампиньоны "
         "59 ₽, бекон 89 ₽, красный лук 39 ₽ — нужны какие-нибудь? Ответь "
         "одним сообщением; «как есть» — положу как выб",
         "традиционное, без добавок"],
        ["Перед тем как положить «Кола» в корзину: Объём колы: 0,5 л или "
         "1 л? Сейчас выбрано 0,5 л. Ответь одним сообщением; «как есть» — "
         "положу как выбрано.", "0,5"],
        ["В корзине: пепперони 30 см (+ традиционное), кола 0,5 л. Добавить "
         "что-нибудь ещё? Если нет — «нет», открою корзину и перейду к "
         "оформлению.", "нет, всё"],
        ["(a question asked on a private page — hidden)", ADDR],
        ["(a question asked on a private page — hidden)",
         "***(16), имя Тест"]],
    "qa_private": [5, 6],
    "brief": {"site": "pizzatest.ru", "items": [
        {"name": "пепперони", "size": "30 см", "options": ["традиционное"],
         "qty": 1},
        {"name": "кола", "size": "0,5 л", "options": [], "qty": 1}]},
}
# Ответ облачного шага run3 (№26) на вопрос о доставке
INFO_ANSWER = ("Пицца Тест: доставка бесплатна при заказе от 799 ₽, при "
               "меньшей сумме стоит 149 ₽ (минимальный заказ — 499 ₽). За "
               "МКАД в пределах 5 км доставка 249 ₽. Заказы принимают "
               "ежедневно с 10:00 до 23:00, в пятницу и субботу — до 01:00.")


def check(name, cond, detail=""):
    global FAILS, OK
    OK += 1
    if cond:
        print(f"  [OK] {name}")
    else:
        FAILS += 1
        print(f"  [FAIL] {name} {detail}")


class ShopCC(FakeCC):
    """Магазин: /checkout и /account (приватные по встроенным признакам),
    /delivery и /menu (обычные). «Доставка и оплата» уводит на /delivery."""

    PAGES = {
        "https://pizza.test/checkout": [
            {"idx": 40, "tag": "input", "role": "textbox",
             "text": "Адрес доставки", "ed": True, "vp": True},
            {"idx": 41, "tag": "a", "role": "link",
             "text": "Доставка и оплата",
             "href": "https://pizza.test/delivery", "vp": True},
        ],
        "https://pizza.test/account": [
            {"idx": 60, "tag": "div", "text": "Бонусы: 1 234", "vp": True},
            {"idx": 61, "tag": "a", "role": "link", "text": "Меню",
             "vp": True},
        ],
        "https://pizza.test/delivery": [
            {"idx": 50, "tag": "div", "text": "Доставка 149 ₽, до 23:00",
             "vp": True},
            {"idx": 51, "tag": "a", "role": "link", "text": "Меню",
             "vp": True},
        ],
        "https://pizza.test/menu": [
            {"idx": 20, "tag": "button", "role": "button",
             "text": "Кола — 119 ₽", "vp": True},
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


class Local:
    """Подмена PrivateRouter.get_response: шаги — по сценарию."""

    def __init__(self, steps=()):
        self.steps = list(steps)
        self.prompts = []

    def __call__(self, _router, messages, **kw):
        p = messages[-1]["content"]
        self.prompts.append(p)
        if "autopilot" not in p or not self.steps:
            return None
        s = self.steps.pop(0)
        return s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)


def _obs(url, run):
    return {"url": url, "host": "pizza.test", "tab_id": 1,
            "shown": [dict(x) for x in ShopCC.PAGES[url]], "note": None,
            "text": None, "error": None, "search": run.get("search"),
            "private": "checkout" in url or "account" in url}


def _block(prompt: str) -> str:
    # Блок прошлых задач в промпте шага (до следующего блока)
    i = prompt.find(PAST_HEAD)
    if i < 0:
        return ""
    j = prompt.find("\n\n", i)
    return prompt[i:j if j >= 0 else len(prompt)]


def _run(agent, chat, goal):
    from app.features import task_agent as ta
    run = {"goal": goal, "lang": "ru", "qa": [], "history": [], "steps": 0,
           "sites": [], "past": agent._past_tasks(chat, goal),
           "brief": ta._new_brief(), "chat_id": chat}
    if ta._REPEAT_RE.search(goal):
        last = next((r for r in run["past"] if r.get("ok")
                     and isinstance(r.get("brief"), dict)), None)
        if last:
            ta._apply_brief(run["brief"], last["brief"], src="memory")
    return run


def main():
    from app.features import cc_privacy as _P
    from app.features import task_agent as ta
    from app.features.task_agent import TaskAgent

    tmp = Path(tempfile.mkdtemp(prefix="ta_pastblock_"))
    orig_local = _P.PrivateRouter.get_response
    # Итог заказа run3 — заглушкой: прогон кончился передачей оплаты на
    # /checkout
    ORDER_REC["result"] = ta._PRIVATE_RESULT

    # ── 1. Размер блока: похожая задача, та же тема заказа, повтор ──
    print("блок прошлых задач в промпте шага")
    mem = tmp / "mem.json"
    mem.write_text(json.dumps({"c1": [ORDER_REC]}, ensure_ascii=False),
                   encoding="utf-8")
    agent = TaskAgent(ShopCC("https://pizza.test/delivery"), memory_path=mem)
    url = "https://pizza.test/delivery"
    full_old = (f"{PAST_HEAD} (newest first):\n"
                + TaskAgent._past_line(ORDER_REC))

    run_i = _run(agent, "c1", "узнай в Пицца Тест, сколько стоит доставка")
    p_i = agent._prompt(run_i, _obs(url, run_i))
    b_i = _block(p_i)
    check("похожая задача (справка): блок есть, что заказали — одной строкой",
          b_i and "ordered: пепперони 30 см (традиционное), кола 0,5 л" in b_i,
          b_i)
    check("похожая задача: без вопросов прошлой задачи и заглушки итога",
          "Q:" not in b_i and "Где заказать" not in b_i
          and ta._PRIVATE_RESULT not in b_i and "Ended with" not in b_i, b_i)
    check(f"похожая задача: блок {len(b_i)} симв. вместо {len(full_old)} "
          "(меньше четверти)", 0 < len(b_i) < len(full_old) / 4)

    run_o = _run(agent, "c1", "закажи пиццу")
    p_o = agent._prompt(run_o, _obs(url, run_o))
    b_o = _block(p_o)
    check("та же тема заказа: коротко, прошлый выбор — позициями",
          "ordered: пепперони 30 см (традиционное), кола 0,5 л" in b_o
          and "Q:" not in b_o and len(b_o) < len(full_old) / 4, b_o)
    check("та же тема заказа: сайт прошлого заказа предлагается кодом",
          agent._site_offer(run_o) == "pizzatest.ru")

    run_r = _run(agent, "c1", "закажи пиццу как в прошлый раз")
    p_r = agent._prompt(run_r, _obs(url, run_r))
    b_r = _block(p_r)
    check("«как в прошлый раз»: запись целиком — ответы с обычных страниц",
          "A: пепперони 30 см и колу" in b_r
          and "A: традиционное, без добавок" in b_r and "Где заказать" in b_r,
          b_r[:400])
    check("«как в прошлый раз»: позиции — в бриф «из памяти»",
          "from memory" in p_r and "item: пепперони" in p_r)
    check("«как в прошлый раз»: ответы с оформления — заглушкой",
          ADDR not in p_r and "Тестовая" not in p_r and "имя Тест" not in p_r
          and b_r.count(ta._PRIVATE_ANSWER) == 2)

    # Повтор: целиком — только повторяемая запись, остальные коротко
    older = dict(ORDER_REC, ts=ORDER_REC["ts"] - 86400,
                 brief={"site": "pizzaplus-test.ru", "items": [
                     {"name": "Маргарита", "size": "25 см", "qty": 2}]})
    mem2 = tmp / "mem2.json"
    mem2.write_text(json.dumps({"c2": [older, ORDER_REC]},
                               ensure_ascii=False), encoding="utf-8")
    ag2 = TaskAgent(ShopCC(url), memory_path=mem2)
    run_r2 = _run(ag2, "c2", "повтори заказ")
    b_r2 = _block(ag2._prompt(run_r2, _obs(url, run_r2)))
    check("повтор: старшая запись — одной строкой, повторяемая — целиком",
          "ordered: Маргарита 25 см ×2 on pizzaplus-test.ru" in b_r2
          and b_r2.count("Где заказать") == 1, b_r2[-400:])

    # Локальная модель (снова на оформлении) — целиком, значения видны
    p_l = agent._prompt(run_i, _obs("https://pizza.test/checkout", run_i),
                        local=True)
    b_l = _block(p_l)
    check("локальной модели — запись целиком, ответ с оформления виден",
          ADDR in b_l and "Где заказать" in b_l and "A: 0,5" in b_l, b_l[:300])

    # ── 2. Запись без позиций: первые пары коротко, приватное заглушкой ──
    print("запись без позиций заказа")
    form = {"ts": 1, "goal": "заполни анкету доставки на pizza.test",
            "sites": ["pizza.test"], "ok": True,
            "qa": [["Какое имя указать в анкете? Варианты:\n- Иван\n- Пётр",
                    "Иван"],
                   ["Какой у вас телефон?", PHONE],
                   [ta._PRIVATE_QUESTION, ADDR],
                   ["Подписаться на рассылку?", "нет"]],
            "qa_private": [2], "result": "Анкета отправлена " + "x" * 300}
    short = TaskAgent._past_line(form, short=True)
    check("короткая строка: первые пары, вопрос без вариантов",
          "Q: Какое имя указать в анкете? Варианты:" in short
          and "A: Иван" in short and "- Пётр" not in short
          and "Подписаться" not in short and "(+1 more questions)" in short,
          short)
    check("короткая строка: ответ с приватной страницы — заглушкой",
          ADDR not in short and ta._PRIVATE_ANSWER in short
          and ta._PRIVATE_QUESTION in short)
    check("короткая строка: телефон-ответ — маской", PHONE not in short
          and "999" not in short)
    check("короткая строка: итог обрезан",
          "Анкета отправлена" in short
          and len(short.split("Ended with: ")[1]) <= ta.PAST_RESULT_SHORT + 1)
    check("локальной модели: значение с приватной страницы — как есть",
          ADDR in TaskAgent._past_line(form, local=True, short=True))
    pw = {"ts": 1, "goal": "войди с паролем Qwerty12 на shop.ru",
          "sites": ["shop.ru"], "qa": [["Какой пароль?", "Kotik2020!"]],
          "result": "ок"}
    line_pw = TaskAgent._past_line(pw, short=True)
    check("короткая строка: пароль цели и ответа — маской",
          "Qwerty12" not in line_pw and "Kotik2020!" not in line_pw)
    bad = {"ts": 1, "goal": "g", "qa": [["только вопрос"], "x", None],
           "brief": {"items": ["x", {"name": ""}, {"name": "Кола",
                                                   "options": "x"}]}}
    check("испорченная запись — не падает",
          "ordered: Кола" in TaskAgent._past_line(bad, short=True))

    # ── 3. Блок — повторяемый кусок промпта (StickyBlock) ──
    print("повторяемый блок")
    # _sticky_for — по последнему промпту прогона (в шаге — сразу за ним)
    p_r = agent._prompt(run_r, _obs(url, run_r))
    p_i = agent._prompt(run_i, _obs(url, run_i))
    blocks = agent._sticky_for(run_r)
    past_b = [b for b in blocks if b.ref == ta._STEP_PAST_REF]
    check("повтор (длинный блок): блок прошлых задач — повторяемый",
          len(past_b) == 1 and past_b[0].text == b_r and past_b[0].text in p_r
          and past_b[0].window == ta.STICKY_PAST_WINDOW)
    check("короткий блок — целиком, без ссылки",
          len(b_i) < ta.STICKY_PAST_MIN and run_i.get("past_block") == b_i
          and not any(
              b.ref == ta._STEP_PAST_REF for b in agent._sticky_for(run_i)))
    # Секрет человека в блоке — {{secretN}}: блок — тот же кусок промпта
    # Секрет чата (ответ на «пароль?» в этой задаче) — в ответе прошлой
    # задачи без слова «пароль»: его скрывает только {{secretN}} промпта
    sec = dict(ORDER_REC, qa=[["Что передать курьеру?",
                               "скажи слово лунакотик"]]
               + ORDER_REC["qa"][1:])
    mem3 = tmp / "mem3.json"
    mem3.write_text(json.dumps({"c3": [sec]}, ensure_ascii=False),
                    encoding="utf-8")
    ag3 = TaskAgent(ShopCC(url), memory_path=mem3)
    run_s = _run(ag3, "c3", "закажи пиццу как в прошлый раз")
    run_s["qa"] = [("Какой пароль от кабинета?", "лунакотик")]
    p_s = ag3._prompt(run_s, _obs(url, run_s))
    blk = run_s.get("past_block") or ""
    check("секрет в блоке — {{secretN}}, блок — точный кусок промпта",
          "лунакотик" not in p_s and "{{secret" in blk and blk in p_s
          and any(b.text == blk for b in ag3._sticky_for(run_s)), blk[:200])

    # Тред веб-чата: второй шаг подряд — блок ссылкой
    from app.features import browser_actions as ba
    from app.features import web_llm as wl
    env_keys = ("WEBCHAT_AUTO_GAP_SEC", "WEBCHAT_AUTO_JITTER_SEC",
                "WEBCHAT_AUTO_PER_HOUR", "WEBCHAT_NEW_CHATS_PER_HOUR",
                "WEBCHAT_NEW_CHATS_PER_DAY")
    env_old = {k: os.environ.get(k) for k in env_keys}
    for k in env_keys:
        os.environ[k] = "0"
    paths = (wl._pace_path, wl._suspended_path)
    blocks_fn = (ba.count_blocks, ba.last_block_text)
    wl._pace_path = lambda: tmp / "pacing.json"
    wl._suspended_path = lambda: tmp / "suspended.json"
    ba.count_blocks = lambda *a, **kw: 0
    ba.last_block_text = lambda *a, **kw: ""

    class FakeChat(wl.WebChatLLM):
        def __init__(self):
            super().__init__("deepseek", base_dir=tmp / "wc", channel="cc")
            self.sent = []
            self.nav = True

        def _ensure_chat(self, fresh=False):
            if fresh or self.nav:
                self._new_thread()
                self.nav = False
            self._tab_id = 1
            return 1

        def _send_verified(self, ba_, host, tab_id, prompt,
                           wait_upload=False):
            self.sent.append(prompt)

        def _wait_answer(self, *a, **kw):
            return '{"action":"scroll"}'

        def _capture_chat_url(self, *a, **kw):
            pass

        def _quarantine_skip(self, ba_=None):
            return False

        def _snap_for(self, ba_, tab_id):
            return None

    class Via:
        # Роутер поверх веб-чата: канал/провайдер режима — не его забота
        def __init__(self, chat):
            self.chat = chat

        def get_response(self, messages, **kw):
            return self.chat.get_response(messages)

    try:
        chat = FakeChat()
        for _ in range(2):
            agent._ask_llm(run_r, "step", Via(chat), p_r,
                           sticky=agent._sticky_for(run_r))
        check("тред веб-чата: 1-й шаг — блок целиком, 2-й — ссылкой",
              len(chat.sent) == 2 and b_r in chat.sent[0]
              and b_r not in chat.sent[1]
              and ta._STEP_PAST_REF in chat.sent[1])
        chat2 = FakeChat()
        for _ in range(2):
            agent._ask_llm(run_i, "step", Via(chat2), p_i,
                           sticky=agent._sticky_for(run_i))
        check("тред веб-чата: короткий блок — целиком в обоих шагах",
              len(chat2.sent) == 2 and all(b_i in s for s in chat2.sent))
    finally:
        wl._pace_path, wl._suspended_path = paths
        ba.count_blocks, ba.last_block_text = blocks_fn
        for k, v in env_old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ── 4. Итог справки, начатой на приватной странице ──
    print("итог задачи в памяти")
    try:
        mem4 = tmp / "mem4.json"
        cc = ShopCC("https://pizza.test/checkout")
        loc = Local(steps=[{"action": "click", "n": 2,
                            "label": "Доставка и оплата"}])
        _P.PrivateRouter.get_response = (
            lambda self, messages, **kw: loc(self, messages, **kw))
        router = ScriptedRouter([{"action": "done", "message": INFO_ANSWER}])
        out = TaskAgent(cc, memory_path=mem4).start(
            "c4", "узнай на pizza.test, сколько стоит доставка и до скольки "
            "работают", router, user_id="A")
        check("справка: первый шаг — локальной модели на /checkout, ответ — "
              "облачного шага на /delivery",
              len([p for p in loc.prompts if "autopilot" in p]) == 1
              and router.prompts and "pizza.test/delivery" in router.prompts[-1]
              and "149 ₽" in out, f"{len(loc.prompts)} {out[:200]}")
        rec4 = json.loads(mem4.read_text(encoding="utf-8"))["c4"][-1]
        check("справка, начатая на приватной странице: итог — в памяти",
              rec4.get("result") != ta._PRIVATE_RESULT
              and "149 ₽" in str(rec4.get("result"))
              and "01:00" in str(rec4.get("result")), f"{rec4.get('result')}")
        cc5 = ShopCC("https://pizza.test/menu")
        r5 = ScriptedRouter([{"action": "fail", "message": "стоп"}])
        TaskAgent(cc5, memory_path=mem4).start(
            "c4", "узнай на pizza.test, сколько стоит доставка в выходные",
            r5, user_id="A")
        b5 = _block(r5.prompts[0] if r5.prompts else "")
        check("следующая задача: итог справки виден облаку (коротко)",
              "Ended with: Пицца Тест: доставка бесплатна" in b5
              and ta._PRIVATE_RESULT not in b5, b5)

        # Отчёт локальной модели на приватной странице — заглушкой.
        # Вопрос оттуда — чтобы прогон было что записать (пустой не пишется)
        mem6 = tmp / "mem6.json"
        loc6 = Local(steps=[{"action": "ask",
                             "question": "За какой период показать?"},
                            {"action": "done",
                             "message": "На счету 1 234 бонуса, адрес "
                             + ADDR}])
        _P.PrivateRouter.get_response = (
            lambda self, messages, **kw: loc6(self, messages, **kw))
        r6 = ScriptedRouter([])
        ag6 = TaskAgent(ShopCC("https://pizza.test/account"),
                        memory_path=mem6)
        ag6.start("c6", "узнай, сколько бонусов на pizza.test", r6,
                  user_id="A")
        out6 = ag6.feed("c6", "за месяц", r6, user_id="A")
        rec6 = json.loads(mem6.read_text(encoding="utf-8"))["c6"][-1]
        check("отчёт локальной модели по кабинету — человеку в чат",
              "1 234 бонуса" in out6 and not r6.prompts)
        check("отчёт локальной модели по кабинету — в памяти заглушкой",
              rec6.get("result") == ta._PRIVATE_RESULT
              and "бонус" not in json.dumps(rec6, ensure_ascii=False)
              .replace(rec6["goal"], ""), f"{rec6}")
    finally:
        _P.PrivateRouter.get_response = orig_local

    # Решение _result_private по состоянию прогона
    base = {"goal": "узнай про доставку на pizza.test", "lang": "ru",
            "history": [], "sites": ["pizza.test"], "private_seen": True,
            "step_private": False, "page_private": False, "outcome": "done",
            "qa": [("Куда доставить?", ADDR)], "qa_private": {0}}
    rp = TaskAgent._result_private
    check("облачный шаг на обычной странице — не приватно",
          not rp(base, "Доставка 149 ₽"))
    check("в итоге ответ с приватной страницы — приватно",
          rp(base, f"Доставим на {ADDR} за 149 ₽"))
    br = ta._new_brief()
    ta._apply_brief(br, {"address": "ул. Лесная, 7"}, src="private")
    ta._apply_brief(br, {"items": [{"name": "Подарок для Маши"}]},
                    src="private")
    check("в итоге слот/позиция брифа с приватной страницы — приватно",
          rp(dict(base, qa=[], brief=br), "Привезём на ул. Лесная, 7")
          and rp(dict(base, qa=[], brief=br), "Положил Подарок для Маши"))
    check("короткий ответ с приватной страницы («да») — не повод",
          not rp(dict(base, qa=[("Подтвердить?", "да")]), "Да, доставка есть"))
    check("последний шаг на приватной странице / локальной моделью — "
          "приватно", rp(dict(base, step_private=True), "Доставка 149 ₽"))
    check("браузер стоит на приватной странице — приватно",
          rp(dict(base, page_private=True), "Сделал 40 шагов"))
    check("передача оплаты после оформления — приватно",
          rp(dict(base, outcome="payment"), "Дошёл до оплаты"))
    old = {k: v for k, v in base.items() if k != "step_private"}
    check("прогон без отметки шага (старый) — приватно, как раньше",
          rp(old, "Доставка 149 ₽"))
    check("прогон без приватных страниц — не приватно",
          not rp(dict(base, private_seen=False, step_private=True),
                 "Доставка 149 ₽"))
    mem7 = tmp / "mem7.json"
    ag7 = TaskAgent(ShopCC(url), memory_path=mem7)
    ag7._remember("c7", dict(base), "cancelled by the user")
    rec7 = json.loads(mem7.read_text(encoding="utf-8"))["c7"][-1]
    check("системный итог — как есть", rec7["result"] == "cancelled by the user")

    # ── 5. Run3: сколько символов облаку сэкономили бы шаги ──
    print("run3: облачные шаги сценариев 2–3")
    info = {"ts": ORDER_REC["ts"] + 51,
            "goal": "узнай в Пицца Тест, сколько стоит доставка и до скольки "
            "работают", "sites": ["pizzatest.ru"], "ok": True, "qa": [],
            "result": ta._PRIVATE_RESULT}
    head = f"{PAST_HEAD} (newest first):\n"
    old2 = head + TaskAgent._past_line(ORDER_REC)
    new2 = head + TaskAgent._past_line(ORDER_REC, short=True)
    old3 = head + "\n".join(TaskAgent._past_line(r) for r in (info, ORDER_REC))
    new3 = head + "\n".join(TaskAgent._past_line(r, short=True) for r in (
        dict(info, result=INFO_ANSWER), ORDER_REC))
    # №25, №26 — сценарий 2; №29, 30, 32, 33 — сценарий 3. Ссылка вместо
    # блока — со второго шага подряд в треде (№30, 32, 33), если блок длинный
    ref3 = len(ta._STEP_PAST_REF) if len(new3) >= ta.STICKY_PAST_MIN \
        else len(new3)
    saved = 2 * (len(old2) - len(new2)) + (len(old3) - len(new3)) \
        + 3 * (len(old3) - ref3)
    was = 2 * len(old2) + 4 * len(old3)
    print(f"    блок: сценарий 2 — {len(old2)} → {len(new2)} симв., "
          f"сценарий 3 — {len(old3)} → {len(new3)} (ссылкой {ref3})")
    print(f"    6 облачных шагов: {was} → {was - saved} симв. блока "
          f"(−{saved})")
    check("run3: блок прошлых задач облаку — меньше пятой части",
          was - saved < was / 5)

    print(f"\nИтого: {OK} проверок, {FAILS} провалов")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

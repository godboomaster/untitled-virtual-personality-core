"""Тест бюджета задачи в вызовах модели (task_agent, MAX_CALLS_PER_TURN).

Проверяет:
* пауза «продолжать?» ровно на потолке вызовов за ход, до следующего шага;
  в тексте — сколько запросов к модели заняла задача (ru и en);
* «да» начинает новый ход — счёт с нуля; ответ на вопрос агента — нет:
  разбор ответа (бриф) до входа в цикл идёт в счёт хода;
* пауза по вызовам не ломает «стоп»/«отмена»/«нет», протухшее «да»
  (task_continue_expired), чужую реплику (task_continue_foreign) и паузу
  при сбое модели (task_llm_down);
* TASK_MAX_CALLS_PER_TURN из окружения, 0 — без потолка; мусор — по
  умолчанию;
* поле calls в карточке задачи (живой прогон и итог).

Браузер — FakeCC, модель — ScriptedRouter (test_task_agent): живых
браузеров, веб-чатов и LLM нет.

Запуск: PYTHONPATH=. python3 scripts/test_task_call_budget.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.features import task_agent as ta  # noqa: E402
from app.features.task_agent import TaskAgent  # noqa: E402
from scripts.test_task_agent import FakeCC, ScriptedRouter  # noqa: E402

ENV = "TASK_MAX_CALLS_PER_TURN"
OK = 0
FAILS = 0


def check(name, cond, detail=""):
    global OK, FAILS
    if cond:
        OK += 1
        print(f"  [OK] {name}")
    else:
        FAILS += 1
        print(f"  [FAIL] {name} {detail}")


class SlotRouter:
    # Разбор ответа по слотам (отдельный вызов): скриптованные JSON
    def __init__(self, replies=()):
        self.replies, self.prompts = list(replies), []

    def get_response(self, messages, **kw):
        self.prompts.append(messages[-1]["content"])
        r = self.replies.pop(0) if self.replies else {}
        return json.dumps(r, ensure_ascii=False)


class EnCC(FakeCC):
    # Ход на английском (язык хода бот ставит менеджеру управления)
    @staticmethod
    def turn_lang():
        return "en"


class DownOnceRouter(ScriptedRouter):
    # Первый вызов — сбой модели (исключение), дальше — по сценарию
    def __init__(self, replies):
        super().__init__(replies)
        self.failed = False

    def get_response(self, messages, **kw):
        if not self.failed:
            self.failed = True
            self.prompts.append(messages[-1]["content"])
            raise RuntimeError("model down")
        return super().get_response(messages, **kw)


def walk(n, end="Всё."):
    # n шагов без вопросов (листание/чтение по очереди — не «по кругу») и итог
    return ([{"action": "scroll"}, {"action": "read"}] * n)[:n] + [
        {"action": "done", "message": end}]


def set_cap(value):
    if value is None:
        os.environ.pop(ENV, None)
    else:
        os.environ[ENV] = str(value)


def agent_at(url="https://pizza.test/", cc_cls=FakeCC):
    cc = cc_cls()
    cc.url = url
    return TaskAgent(cc), cc


def main():
    # Память задач — во временную папку, не в data/ персоны
    FakeCC.base_dir = Path(tempfile.mkdtemp(prefix="task_budget_"))
    from app.features import browser_actions as _ba
    _ba.wait_dom_idle = lambda *a, **k: None
    _ba.section_items = lambda *a, **k: None
    _ba.section_names = lambda *a, **k: None
    _ba.scroll_step = lambda *a, **k: {"moved": True, "bottom": False}
    _ba.scroll_container_step = lambda *a, **k: {"moved": True,
                                                 "bottom": False, "y0": 0}
    # Приватная страница не зовёт живую локальную модель (Ollama)
    from app.features import cc_privacy as _cp
    _cp.PrivateRouter.get_response = lambda self, msgs, **kw: None
    saved_env = os.environ.get(ENV)
    saved_steps = ta.MAX_STEPS_PER_TURN
    try:
        _run()
    finally:
        set_cap(saved_env)
        ta.MAX_STEPS_PER_TURN = saved_steps
    print(f"\nИтого: {OK + FAILS} проверок, {FAILS} провалов")
    return 1 if FAILS else 0


def _run():
    print("Склонение и потолок из окружения")
    check("1 запрос / 22 запроса / 30 запросов / 11 запросов",
          [ta._calls_ru(n) for n in (1, 22, 30, 11)]
          == ["1 запрос", "22 запроса", "30 запросов", "11 запросов"])
    check("шаги склоняются как раньше",
          [ta._steps_ru(n) for n in (1, 3, 12, 21)]
          == ["1 шаг", "3 шага", "12 шагов", "21 шаг"])
    set_cap(None)
    check("без переменной — MAX_CALLS_PER_TURN (около 30)",
          ta._calls_cap() == ta.MAX_CALLS_PER_TURN and 20 <= ta.MAX_CALLS_PER_TURN <= 40)
    set_cap(7)
    check("TASK_MAX_CALLS_PER_TURN=7 → 7", ta._calls_cap() == 7)
    set_cap("0")
    check("TASK_MAX_CALLS_PER_TURN=0 → 0 (без потолка)", ta._calls_cap() == 0)
    set_cap("-3")
    check("отрицательное → 0", ta._calls_cap() == 0)
    set_cap("много")
    check("не число → по умолчанию", ta._calls_cap() == ta.MAX_CALLS_PER_TURN)

    print("Пауза ровно на потолке")
    set_cap(4)
    agent, cc = agent_at()
    router = ScriptedRouter(walk(10))
    reply = agent.start("b1", "найди что-нибудь", router, user_id="A")
    run = agent._runs.get("b1") or {}
    check("4 вызова → «продолжать?» с числом запросов",
          "Задача заняла уже 4 запроса к модели" in reply
          and "Продолжать?" in reply, reply)
    check("пятого шага нет: модель спрошена ровно 4 раза, 4 шага",
          len(router.prompts) == 4 and run.get("steps") == 4,
          (len(router.prompts), run.get("steps")))
    check("ждёт «продолжать?» от автора задачи",
          (run.get("awaiting") or {}).get("kind") == "continue"
          and run["awaiting"].get("user_id") == "A")
    check("текст бюджета шагов не подменён", "Сделал уже" not in reply)
    card = agent.task_card("b1")
    check("карточка: calls = 4, статус «пауза»",
          card and card.get("calls") == 4 and card["status"] == "paused", card)

    print("Сброс на «да»")
    reply = agent.feed("b1", "да", router, user_id="A")
    run = agent._runs.get("b1") or {}
    check("«да» → новый ход ещё на 4 вызова, в тексте — всего 8",
          len(router.prompts) == 8 and "8 запросов к модели" in reply, reply)
    check("отметка хода — на 4", run.get("calls_mark") == 4, run.get("calls_mark"))
    reply = agent.feed("b1", "да", router, user_id="A")
    check("третий ход короче потолка — до done",
          reply.endswith("Всё.") and not agent.active("b1")
          and len(router.prompts) == 11, (reply, len(router.prompts)))
    card = agent.task_card("b1")
    check("итоговая карточка: calls = 11 за все три хода, статус «готово»",
          card and card.get("calls") == 11 and card["status"] == "done", card)

    print("Ответ без «да» на «продолжать?» — тоже продолжение")
    agent, cc = agent_at()
    router = ScriptedRouter(walk(10))
    agent.start("b2", "найди что-нибудь", router, user_id="A")
    reply = agent.feed("b2", "смотри ниже", router, user_id="A")
    check("уточнение на паузе продолжает задачу новым ходом",
          len(router.prompts) == 8 and "8 запросов" in reply, reply)

    print("Ответ на вопрос агента не сбрасывает счёт хода")
    set_cap(3)
    agent, cc = agent_at()
    slots = SlotRouter()
    agent.slot_router = slots
    router = ScriptedRouter([{"action": "ask", "question": "Что искать?"},
                             {"action": "scroll"}, {"action": "read"},
                             {"action": "done", "message": "Нашёл."}])
    reply = agent.start("b3", "найди что-нибудь", router, user_id="A")
    check("бриф цели + шаг-вопрос = 2 вызова, ждёт ответа",
          len(slots.prompts) + len(router.prompts) == 2
          and agent._runs["b3"]["awaiting"]["kind"] == "ask", reply)
    reply = agent.feed("b3", "пиццу", router, user_id="A")
    check("разбор ответа — третий вызов: пауза до первого шага хода",
          len(slots.prompts) == 2 and len(router.prompts) == 1
          and "3 запроса к модели" in reply
          and agent._runs["b3"]["awaiting"]["kind"] == "continue", reply)
    reply = agent.feed("b3", "да", router, user_id="A")
    check("«да» → дальше до итога", reply.endswith("Нашёл.")
          and not agent.active("b3"), reply)

    print("Стоп, отмена, «нет», протухшее «да», чужая реплика")
    set_cap(2)
    for i, word in enumerate(("стоп", "отмена", "нет")):
        agent, cc = agent_at()
        router = ScriptedRouter(walk(6))
        agent.start(f"s{i}", "найди что-нибудь", router, user_id="A")
        n0 = len(router.prompts)
        reply = agent.feed(f"s{i}", word, router, user_id="A")
        check(f"«{word}» на паузе по вызовам — задача снята",
              "бросаю задачу" in reply and not agent.active(f"s{i}")
              and len(router.prompts) == n0, reply)

    agent, cc = agent_at()
    router = ScriptedRouter(walk(6))
    agent.start("e1", "найди что-нибудь", router, user_id="A")
    aw = agent._runs["e1"]["awaiting"]
    reply = agent.feed("e1", "да", router, user_id="B")
    check("чужое «да» — прогон стоит, счёт не сброшен",
          "только тот, кто её поставил" in reply and len(router.prompts) == 2
          and agent._runs["e1"]["awaiting"] is aw
          and "calls_mark" not in agent._runs["e1"], reply)
    aw["ts"] = time.time() - ta.CONTINUE_TTL_SEC - 1
    reply = agent.feed("e1", "да", router, user_id="A")
    aw2 = agent._runs["e1"]["awaiting"]
    check("протухшее «да» — вопрос заново, шагов и сброса нет",
          "Пауза затянулась" in reply and len(router.prompts) == 2
          and aw2["kind"] == "continue" and aw2["ts"] > aw["ts"]
          and "calls_mark" not in agent._runs["e1"], reply)
    reply = agent.feed("e1", "да", router, user_id="A")
    check("свежее «да» автора — новый ход на 2 вызова",
          len(router.prompts) == 4 and "4 запроса к модели" in reply, reply)

    print("Пауза при сбое модели")
    set_cap(3)
    agent, cc = agent_at()
    router = DownOnceRouter(walk(6))
    reply = agent.start("d1", "найди что-нибудь", router, user_id="A")
    check("сбой модели — своя пауза, вызов посчитан",
          "Модель сейчас не отвечает" in reply
          and agent.task_card("d1").get("calls") == 1, reply)
    reply = agent.feed("d1", "да", router, user_id="A")
    check("«да» на неё — новый ход: ещё 3 вызова до паузы по вызовам",
          len(router.prompts) == 4 and "4 запроса к модели" in reply, reply)

    print("0 — без потолка")
    ta.MAX_STEPS_PER_TURN = 100
    try:
        set_cap(0)
        agent, cc = agent_at()
        router = ScriptedRouter(walk(34))
        reply = agent.start("z1", "найди что-нибудь", router, user_id="A")
        check("TASK_MAX_CALLS_PER_TURN=0: 35 вызовов за ход без паузы",
              reply.endswith("Всё.") and len(router.prompts) == 35
              and not agent.active("z1"), (reply[-80:], len(router.prompts)))
        set_cap(None)
        saved = ta.MAX_CALLS_PER_TURN
        ta.MAX_CALLS_PER_TURN = 0
        try:
            agent, cc = agent_at()
            router = ScriptedRouter(walk(34))
            reply = agent.start("z2", "найди что-нибудь", router, user_id="A")
            check("MAX_CALLS_PER_TURN = 0 — тоже без потолка",
                  reply.endswith("Всё.") and len(router.prompts) == 35)
        finally:
            ta.MAX_CALLS_PER_TURN = saved
        agent, cc = agent_at()
        router = ScriptedRouter(walk(34))
        reply = agent.start("z3", "найди что-нибудь", router, user_id="A")
        check(f"по умолчанию — пауза на {ta.MAX_CALLS_PER_TURN} вызовах",
              len(router.prompts) == ta.MAX_CALLS_PER_TURN
              and f"{ta._calls_ru(ta.MAX_CALLS_PER_TURN)} к модели" in reply,
              (len(router.prompts), reply[-120:]))
    finally:
        ta.MAX_STEPS_PER_TURN = 12

    print("Бюджет шагов раньше потолка вызовов — прежний текст")
    set_cap(30)
    ta.MAX_STEPS_PER_TURN = 2
    try:
        agent, cc = agent_at()
        router = ScriptedRouter(walk(4))
        reply = agent.start("w1", "найди что-нибудь", router, user_id="A")
        check("2 шага → «Сделал уже 2 шага», без текста про запросы",
              "Сделал уже 2 шага" in reply and "запрос" not in reply, reply)
    finally:
        ta.MAX_STEPS_PER_TURN = 12

    print("Английский ход")
    set_cap(3)
    agent, cc = agent_at(cc_cls=EnCC)
    router = ScriptedRouter(walk(6))
    reply = agent.start("en1", "find something", router, user_id="A")
    check("en: «The task has taken 3 model requests already… Continue?»",
          "The task has taken 3 model requests already and isn't finished. "
          "Continue? (\"yes\" / \"cancel\")" in reply, reply)
    from app.features import cc_texts
    check("cc_texts: английский шаблон рядом с task_continue",
          "task_continue_calls" in cc_texts._EN_PHRASES
          and cc_texts.phrase("x", "task_continue_calls", "ru", "en", calls=5)
          == "The task has taken 5 model requests already and isn't "
             "finished. Continue? (\"yes\" / \"cancel\")")
    check("cc_texts: не en — русский шаблон места вызова",
          cc_texts.phrase("x", "task_continue_calls", "Задача заняла уже "
                          "5 запросов", "ru", calls=5).startswith(
                              "Задача заняла уже 5 запросов"))

    print("Карточка")
    agent = TaskAgent(FakeCC())
    agent._runs["c"] = {"goal": "найди что-нибудь", "lang": "ru", "qa": [],
                        "history": [], "steps": 3, "awaiting": None,
                        "busy": False, "cancel": False, "touched": time.time(),
                        "started": time.time() - 30, "ui_log": [], "id": "r1",
                        "turn_user": "A",
                        "traffic": {"step": [3, 9000], "brief": [2, 800],
                                    "page": [1, 400]}}
    check("calls — сумма всех видов вызовов (шаги, бриф, страница)",
          agent.task_card("c").get("calls") == 6)
    agent._runs["c"].pop("traffic")
    check("без вызовов — calls = 0", agent.task_card("c").get("calls") == 0)


if __name__ == "__main__":
    sys.exit(main())

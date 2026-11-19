"""Кнопки ответа у портрета: вопрос в ленте реплик идёт с вариантами.

Портрет на рабочем столе показывал вопрос режима управления («Делаю?
(да/нет)», «Какой открыть?» со списком) только текстом — отвечать
приходилось в чате. Теперь реплика хода пользователя несёт options — те же
варианты, что у кнопок веба (BotInstance.cc_answer_options).

  A. said: options у реплики отдаются клиенту, у событий паузы их нет; без
     вариантов — поля нет; в пересказе после перезапуска ядра — нет; тот же текст после вашей новой реплики (heard) —
     не повтор (вопрос с кнопками встаёт снова), без неё — повтор;
  B. server._on_stm_message: варианты — только посреди хода пользователя в
     этом чате (_generating), пустые — без поля, сбой бота — реплика всё
     равно в ленте; ответ из одних строк хода — бота не спрашиваем;
  C. живой прогон агента задач: вопрос со списком → kind options, подписи
     и номера; подтверждение шага → yesno; ответ в ленте — без строк хода.

LLM, Ollama и сеть не вызываются, data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_said_answer_options
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="said_answer_options_")

ok = 0
failures = 0

YESNO = {"kind": "yesno", "options": [{"role": "yes"}, {"role": "no"}]}


def check(name, cond, detail=""):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + ("" if cond or detail == "" else f" — {detail}"))
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


def _reset():
    from app.api import said
    said._recent.clear()


def _items(said, start, chat="web_user"):
    items, _ = said.take(start, chat)
    return items


# ════════════ A. Лента ════════════

def test_said():
    section("A. Лента реплик: варианты у вопроса")
    from app.api import said
    _reset()
    start = said.cursor()
    said.note("don", "web_user", "Делаю? (да/нет)", options=YESNO)
    said.note("don", "web_user", "Привет")
    said.note("don", "web_user", "", kind="settled", options=YESNO)
    items = _items(said, start)
    check("у вопроса — варианты как есть", items[0].get("options") == YESNO, items)
    check("без вариантов — поля нет", "options" not in items[1], items)
    check("у события паузы вариантов не бывает", items[2]["kind"] == "settled" and "options" not in items[2], items)
    items, _ = said.take(0, "web_user", fresh_sec=said.REPLAY_SEC)
    check("после перезапуска ядра вопрос пересказан без кнопок (ожидание ушло с прежним процессом)",
          any(m["text"] == "Делаю? (да/нет)" for m in items) and not any("options" in m for m in items), items)

    _reset()
    start = said.cursor()
    said.note("don", "web_user", "Делаю? (да/нет)", options=YESNO)
    said.note("don", "web_user", "Делаю? (да/нет)", options=YESNO)
    check("тот же вопрос без вашей реплики между — повтор", len(_items(said, start)) == 1)
    said.note("don", "web_user", "что?", kind="heard")
    said.note("don", "web_user", "Делаю? (да/нет)", options=YESNO)
    items = _items(said, start)
    check("после вашей реплики — снова с кнопками",
          [(m["kind"], "options" in m) for m in items] == [("reply", True), ("heard", False), ("reply", True)], items)
    said.note("kara", "web_user", "x", kind="heard")
    said.note("don", "web_user", "Делаю? (да/нет)")
    check("чужая персона своей репликой повтор не снимает",
          len([m for m in _items(said, start) if m["persona"] == "don"]) == 3)


# ════════════ B. Запись в историю ════════════

class Bot:
    def __init__(self, opts=None, lines=frozenset(), boom=False):
        self.opts, self.lines, self.boom = opts, lines, boom
        self.asked = []

    def cc_task_lines(self, chat):
        return self.lines

    def cc_answer_options(self, chat, user=None):
        self.asked.append((chat, user))
        if self.boom:
            raise RuntimeError("boom")
        return self.opts


def test_hook():
    section("B. Реплика хода — с вариантами, фоновая — без")
    import app.api.server as server_mod
    from app.api import runtime, said
    saved = dict(runtime.registry._bots)
    try:
        bot = Bot(YESNO)
        runtime.registry._bots["don"] = bot
        _reset()
        start = said.cursor()
        server_mod._on_stm_message("api_don", "web_user", "assistant", "Напоминаю: чай.")
        check("вне хода (напоминание посреди ожидания) — без вариантов и бота не спрашиваем",
              "options" not in _items(said, start)[-1] and bot.asked == [])
        server_mod._generating["don:web_user"] = 1
        server_mod._on_stm_message("api_don", "web_user", "assistant", "Делаю? (да/нет)")
        check("посреди хода — варианты бота", _items(said, start)[-1].get("options") == YESNO)
        check("бот спрошен о том же чате", bot.asked[-1] == ("web_user", "web_user"), bot.asked)
        server_mod._on_stm_message("api_don", "web_user", "user", "да")
        check("реплика пользователя в ленту не идёт", len(_items(said, start)) == 2)
        server_mod._on_stm_message("api_don", "tg_1", "assistant", "Чужой чат")
        check("другой чат без хода — без вариантов", "options" not in _items(said, start, "tg_1")[-1])

        bot.opts = {"kind": "options", "options": []}
        server_mod._on_stm_message("api_don", "web_user", "assistant", "Какой открыть?")
        check("пустой список — без поля", "options" not in _items(said, start)[-1])
        bot.opts = None
        server_mod._on_stm_message("api_don", "web_user", "assistant", "Готово.")
        check("вопроса нет — без поля", "options" not in _items(said, start)[-1])

        runtime.registry._bots["don"] = Bot(boom=True)
        server_mod._on_stm_message("api_don", "web_user", "assistant", "Ладно.")
        check("сбой бота — реплика всё равно в ленте, без вариантов",
              _items(said, start)[-1]["text"] == "Ладно." and "options" not in _items(said, start)[-1])

        lined = Bot(YESNO, lines=frozenset({"Листаю страницу."}))
        runtime.registry._bots["don"] = lined
        server_mod._on_stm_message("api_don", "web_user", "assistant", "Листаю страницу.")
        check("ответ из одних строк хода — облачка нет и бота не спрашиваем",
              _items(said, start)[-1]["text"] == "Ладно." and lined.asked == [])
    finally:
        server_mod._generating.pop("don:web_user", None)
        runtime.registry._bots.clear()
        runtime.registry._bots.update(saved)


# ════════════ C. Живой прогон агента задач ════════════

def test_agent():
    section("C. Агент задач: вопрос со списком и подтверждение шага")
    import app.api.server as server_mod
    from app.api import runtime, said
    from app.features.task_agent import TaskAgent
    from scripts.test_task_agent import FakeCC, ScriptedRouter

    FakeCC.base_dir = Path(tempfile.mkdtemp(prefix="said_answers_cc_"))
    agent = TaskAgent(FakeCC())
    question = "Какую пиццу?\n- Пепперони — 549 ₽\n- Маргарита — 499 ₽"
    router = ScriptedRouter([
        {"action": "open", "target": "pizza.test"},
        {"action": "click", "n": 1},
        {"action": "ask", "question": question},
    ])
    reply = agent.start("web_user", "закажи пиццу на pizza.test", router)

    class AgentBot:
        def cc_task_lines(self, chat):
            return agent.log_lines(chat)

        def cc_answer_options(self, chat, user=None):
            return agent.answer_options(chat)

    saved = dict(runtime.registry._bots)
    runtime.registry._bots["don"] = AgentBot()
    server_mod._generating["don:web_user"] = 1
    try:
        _reset()
        start = said.cursor()
        server_mod._on_stm_message("api_don", "web_user", "assistant", reply)
        item = _items(said, start)[-1]
        opts = item.get("options") or {}
        check("вопрос со списком — kind options", opts.get("kind") == "options", opts)
        check("подписи и номера вариантов",
              [(o.get("label"), o.get("send")) for o in opts.get("options", [])]
              == [("Пепперони — 549 ₽", "1"), ("Маргарита — 499 ₽", "2")], opts)
        check("в облачке — без строк хода, вопрос со списком (строки-варианты убирает портрет)",
              item["text"].startswith("Какую пиццу?") and "549" in item["text"], item["text"])

        run = agent._runs["web_user"]
        run["awaiting"] = {"kind": "confirm", "question": "Делаю?"}
        server_mod._on_stm_message("api_don", "web_user", "assistant", "Следующий шаг — нажать «Оплатить». Делаю? (да/нет)")
        check("подтверждение шага — да/нет", _items(said, start)[-1].get("options") == YESNO)
        run["busy"] = True
        server_mod._on_stm_message("api_don", "web_user", "assistant", "Ещё работаю.")
        check("агент занят — кнопок нет", "options" not in _items(said, start)[-1])
    finally:
        server_mod._generating.pop("don:web_user", None)
        runtime.registry._bots.clear()
        runtime.registry._bots.update(saved)


def main():
    test_said()
    test_hook()
    test_agent()
    print(f"\nИтого: {ok - failures}/{ok} OK, FAIL: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

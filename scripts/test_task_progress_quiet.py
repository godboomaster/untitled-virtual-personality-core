"""Строки хода агента задач — без уведомлений и облачков; ваше сообщение у
портрета в режиме управления не висит весь ход.

Режим управления: агент шлёт строки хода («Нажал …», «Листаю страницу.»)
промежуточными сообщениями, а итоговый ответ хода начинается последней
пачкой таких строк. Веб прячет их в карточку задачи, но inbox и лента
реплик портрета видели их как реплики: системные уведомления с шагами,
облачко портрета 1/4 со строками хода. А облачко вашего ответа агенту
(«да», «2») висело у портрета до конца хода — минутами.

Строки хода в очередь входящих не кладутся вовсе (а не с меткой): вкладка
со старым кодом (vite перезагружает скрытую вкладку, только когда её
покажут) и APK прежней версии метку не знают и показали бы уведомление.

  A. inbox: WebInboxSender(kind=KIND_PROGRESS) — в очередь ничего, прочие
     метки — как раньше; уведомитель хода (BotInstance._cc_notifier) метит
     строки хода, в историю пишет как раньше; отправителю без поддержки
     меток (Telegram) метку не передаёт — там строки хода идут сообщениями;
  B. лента said: запись строк хода в историю облачком не встаёт (вырезана
     журналом карточки); обычная реплика следом — да;
  C. ответ хода в ленте — без строк хода из журнала карточки
     (TaskAgent.log_lines: живой прогон, итог ещё CARD_TTL_SEC); без
     карточки и без бота — как есть;
  D. ход в режиме управления (_run_generation acting): settled сразу после
     «прочтения» (_read_sec), не дожидаясь конца хода; ход короче —
     лишнего settled нет; вне режима — как раньше; _acting_turn.

LLM, Ollama и сеть не вызываются, data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_task_progress_quiet
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="task_progress_quiet_")

ok = 0
failures = 0


def check(name, cond, detail=""):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + ("" if cond or detail == "" else f" — {detail}"))
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


def _reset():
    from app.api import inbox, said
    said._recent.clear()
    inbox._inbox.clear()


def _queued(persona, chat="web_user"):
    from app.api import inbox
    return [(m["kind"], m["text"]) for m in inbox._inbox.get((persona, chat), ())]


# ════════════ A. inbox и уведомитель хода ════════════

def test_inbox():
    section("A. inbox: метка строк хода")
    from app.api import inbox
    from app.bot_instance import BotInstance
    _reset()
    loop = inbox.background_loop()

    def send(sender, text, **kw):
        asyncio.run_coroutine_threadsafe(sender.send_message("web_user", text, **kw), loop).result(5)

    send(inbox.WebInboxSender("don"), "Напоминаю: чай.")
    send(inbox.WebInboxSender("don"), "Нажал «Меню».", kind=inbox.KIND_PROGRESS)
    send(inbox.WebInboxSender("don", kind=inbox.KIND_INITIATIVE), "Листаю страницу.",
         kind=inbox.KIND_PROGRESS)
    send(inbox.WebInboxSender("don"), "Файл готов.", kind="document")
    check("строки хода в очередь не попадают (и у отправителя инициативы), прочее — как раньше",
          _queued("don") == [("message", "Напоминаю: чай."), ("document", "Файл готов.")], _queued("don"))

    # Уведомитель хода: отправитель и loop — у фонового менеджера бота
    _reset()
    stm = []
    task = SimpleNamespace(done=lambda: False, get_loop=lambda: loop)
    fake = SimpleNamespace(
        reminder_manager=SimpleNamespace(_sender=inbox.WebInboxSender("don"), _task=task),
        proactive=None,
        memory=SimpleNamespace(add_message=lambda role, text, uid, chat: stm.append((role, text, chat))),
        get_chat_topic=lambda chat: None)
    notify = BotInstance._cc_notifier(fake, "web_user", "web_user")
    notify("Нажал «Меню» на pizza.test.\nЛистаю страницу.")
    check("строки хода во входящие не попадают", _queued("don") == [], _queued("don"))
    check("и в историю — как раньше", stm == [("assistant", "Нажал «Меню» на pizza.test.\nЛистаю страницу.",
                                               "web_user")], stm)

    class TgSender:  # без supports_kinds: лишнего аргумента не принимает
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, *, topic_id=None, parse_mode=None):
            self.sent.append((chat_id, text, topic_id))
            return True

    tg = TgSender()
    fake.reminder_manager = SimpleNamespace(_sender=tg, _task=task)
    fake.get_chat_topic = lambda chat: 7
    BotInstance._cc_notifier(fake, "-100", "u1")("Нажал «Меню».")
    check("Telegram: метку не передаём, тема — как раньше", tg.sent == [("-100", "Нажал «Меню».", 7)], tg.sent)


# ════════════ B. Лента said ════════════

def test_said():
    section("B. Лента реплик: строки хода облачком не встают")
    import app.api.server as server_mod
    from app.api import inbox, runtime, said
    _reset()
    lines = frozenset({"Нажал «Меню».", "Листаю страницу."})

    class Bot:
        def cc_task_lines(self, chat):
            return lines

        def cc_answer_options(self, chat, user=None):
            return None

    saved = dict(runtime.registry._bots)
    runtime.registry._bots["don"] = Bot()
    try:
        start = said.cursor()
        # Уведомитель хода: во входящие — ничего, в историю — строки хода
        asyncio.run_coroutine_threadsafe(inbox.WebInboxSender("don").send_message(
            "web_user", "Нажал «Меню».\nЛистаю страницу.", kind=inbox.KIND_PROGRESS), inbox.background_loop()).result(5)
        server_mod._on_stm_message("api_don", "web_user", "assistant", "Нажал «Меню».\nЛистаю страницу.")
        items, cur = said.take(start, "web_user")
        check("строки хода и их запись в историю — не реплики", items == [], items)
        check("номер ленты не сдвинут (будить клиентов нечем)", cur == start)
        server_mod._on_stm_message("api_don", "web_user", "assistant", "Какую пиццу: Пепперони или Маргариту?")
        items, _ = said.take(start, "web_user")
        check("обычная реплика следом — в ленте",
              [(m["kind"], m["text"]) for m in items] == [("reply", "Какую пиццу: Пепперони или Маргариту?")], items)
    finally:
        runtime.registry._bots.clear()
        runtime.registry._bots.update(saved)


# ════════════ C. Ответ хода без строк хода ════════════

def test_strip():
    section("C. Ответ хода в ленте — без строк хода")
    import app.api.server as server_mod
    from app.api import runtime, said
    from app.features.task_agent import CARD_TTL_SEC, RUN_TTL_SEC, TaskAgent
    from scripts.test_task_agent import FakeCC, ScriptedRouter

    FakeCC.base_dir = Path(tempfile.mkdtemp(prefix="task_progress_cc_"))
    agent = TaskAgent(FakeCC())
    router = ScriptedRouter([
        {"action": "open", "target": "pizza.test"},
        {"action": "click", "n": 1},
        {"action": "ask", "question": "Какую пиццу: Пепперони или Маргариту?"},
    ])
    # Без notify — весь ход строками в ответе (как последняя пачка с notify)
    reply = agent.start("web_user", "закажи пиццу на pizza.test", router)
    lines = agent.log_lines("web_user")
    check("журнал живого прогона — строки хода", len(lines) >= 2, lines)
    check("ответ начинается строками хода", reply.splitlines()[0].strip() in lines, reply)

    class Bot:
        def cc_task_lines(self, chat):
            return agent.log_lines(chat)

    _reset()
    saved = dict(runtime.registry._bots)
    runtime.registry._bots["don"] = Bot()
    try:
        start = said.cursor()
        server_mod._on_stm_message("api_don", "web_user", "assistant", reply)
        items, _ = said.take(start, "web_user")
        check("в облачке только вопрос", [m["text"] for m in items] == ["Какую пиццу: Пепперони или Маргариту?"],
              items)
        server_mod._on_stm_message("api_don", "web_user", "assistant", "\n".join(sorted(lines)))
        items, _ = said.take(start, "web_user")
        check("ответ из одних строк хода — облачка нет", len(items) == 1, items)
        server_mod._on_stm_message("api_don", "other", "assistant", reply)
        items, _ = said.take(start, "other")
        check("другой чат (своей карточки нет) — как есть", [m["text"] for m in items] == [reply], items)
        server_mod._on_stm_message("api_kara", "web_user", "assistant", reply)
        check("бот не поднят — как есть", said._recent[-1]["text"] == reply)

        class Broken:
            def cc_task_lines(self, chat):
                raise RuntimeError("boom")

        runtime.registry._bots["don"] = Broken()
        server_mod._on_stm_message("api_don", "web_user", "assistant", "Готово.\nВсё.")
        check("сбой журнала — реплика всё равно в ленте", said._recent[-1]["text"] == "Готово.\nВсё.")
    finally:
        runtime.registry._bots.clear()
        runtime.registry._bots.update(saved)

    # Журнал — живой прогон, потом итог ещё CARD_TTL_SEC
    run = agent._runs["web_user"]
    run["touched"] = time.time() - RUN_TTL_SEC - 5
    check("протухший прогон без итога — пусто", agent.log_lines("web_user") == frozenset())
    run["touched"] = time.time()
    agent._keep_card("web_user", run, "done")
    agent._runs.pop("web_user")
    check("итог прогона — журнал карточки", agent.log_lines("web_user") == lines)
    agent._cards["web_user"]["ts"] = time.time() - CARD_TTL_SEC - 1
    check("после CARD_TTL_SEC — пусто", agent.log_lines("web_user") == frozenset())
    check("чужой чат — пусто", agent.log_lines("nobody") == frozenset())


# ════════════ D. Ход в режиме управления ════════════

def _events(said, persona="don"):
    return [(m["kind"], m["text"]) for m in said._recent if m["persona"] == persona]


def _pending(text):
    return {"text": text, "ts": time.time(), "client": "", "image": False}


def test_acting():
    section("D. Режим управления: облачко уходит после прочтения")
    import app.api.server as server_mod
    from app.api import said

    orig = server_mod._READ_SEC, server_mod._READ_PAD_SEC
    server_mod._READ_SEC, server_mod._READ_PAD_SEC = (0.0, 0.05, 0.05), 0.0
    try:
        def slow():
            time.sleep(0.3)
            said.note("don", "web_user", "Какую открыть?")
            return "Какую открыть?"

        _reset()
        asyncio.run(server_mod._run_generation("don:web_user", slow, pending=_pending("да"), acting=True))
        check("heard → settled (прочитала) → ответ → settled (конец хода)",
              [k for k, _ in _events(said)] == ["heard", "settled", "reply", "settled"], _events(said))

        _reset()
        asyncio.run(server_mod._run_generation("don:web_user", slow, pending=_pending("да")))
        check("вне режима — до конца хода", [k for k, _ in _events(said)] == ["heard", "reply", "settled"],
              _events(said))

        async def quick():
            await server_mod._run_generation("don:web_user", lambda: "ок", pending=_pending("да"), acting=True)
            await asyncio.sleep(0.15)  # таймер «прочитала» уже снят

        _reset()
        asyncio.run(quick())
        check("ход короче чтения — лишнего settled нет", [k for k, _ in _events(said)] == ["heard", "settled"],
              _events(said))

        _reset()
        asyncio.run(server_mod._run_generation("don:web_user", lambda: "ок", pending=None, acting=True))
        check("слэш-команда (pending нет) — ничего", _events(said) == [])
    finally:
        server_mod._READ_SEC, server_mod._READ_PAD_SEC = orig

    check("время чтения: короткое — не меньше минимума", server_mod._read_sec(_pending("да"))
          == server_mod._READ_SEC[1] + server_mod._READ_PAD_SEC)
    check("длинное — не больше максимума", server_mod._read_sec(_pending("я" * 400))
          == server_mod._READ_SEC[2] + server_mod._READ_PAD_SEC)

    def req(**kw):
        base = {"from_skin": False, "chat_id": None, "user_id": "web_user"}
        base.update(kw)
        return SimpleNamespace(**base)

    seen = []

    class Bot:
        def __init__(self, on):
            self.on = on

        def control_mode_on(self, key):
            seen.append(key)
            if isinstance(self.on, Exception):
                raise self.on
            return self.on

    check("режим включён — действует", server_mod._acting_turn(Bot(True), req()) is True)
    check("чат — chat_id или user_id", seen[-1] == "web_user"
          and server_mod._acting_turn(Bot(True), req(chat_id="c9")) and seen[-1] == "c9")
    check("режим выключен — думает", server_mod._acting_turn(Bot(False), req()) is False)
    check("из скина — не режим управления", server_mod._acting_turn(Bot(True), req(from_skin=True)) is False)
    check("сбой — как раньше", server_mod._acting_turn(Bot(RuntimeError("x")), req()) is False)


def main():
    test_inbox()
    test_said()
    test_strip()
    test_acting()
    print(f"\nИтого: {ok - failures}/{ok} OK, FAIL: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

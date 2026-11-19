"""Отмена хода, который уже генерируется, и длина ответа.

  A. app/core/turn_cancel: флажок хода (персона:чат), отменяется только
     начатая генерация, проверка в потоке хода, BaseException;
  B. app/core/reply_length: бюджет токенов → «≤ N слов, A–B предложений»
     для веб-чатов (полосы, язык), обрезка оборванного ответа до фразы;
  C. роутер: перед вызовом модели — проверка, голосом — ответ потоком и
     обрыв посреди него (соединение закрыто), фолбэк по цепочке не идёт;
     веб-чат получает подсказку длины в инструкциях;
  D. ядро: /api/chat/cancel, supersede, настоящий _chat_in_turn — ход
     отменили посреди генерации → cancelled, лок чата свободен сразу;
  E. ход персоны (настоящий _process_message_impl на заглушках): голосом —
     потолок max_tokens, без «продолжи», оборванное — до фразы; отменили,
     пока модель писала, — ответ не лёг в историю;
  F. кнопка «Стоп» веб-чата: /api/chat/stream кончается cancelled сразу
     (и когда поток ещё дописывает — веб-чат на сайте), «печатает» гаснет,
     следующая реплика идёт; работу агента — его «стоп», не обрыв.

Сеть, LLM и браузер не используются.
Запуск: PYTHONPATH=. python3 -m scripts.test_turn_cancel
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="turn_cancel_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"
os.environ.pop("WAKE_LISTEN", None)
os.environ.pop("WEBCHAT_LENGTH_HINT", None)

from app.core import reply_length, turn_cancel  # noqa: E402

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n{title}")


def raises_cancel(fn) -> bool:
    try:
        fn()
    except turn_cancel.TurnCancelled:
        return True
    return False


# ===== A =====

def test_tokens():
    section("A. Флажок хода")
    tok = turn_cancel.open_turn("connor:web_user")
    check("ход в очереди (лок не взят) — не отменяется: реплику ещё не записали",
          turn_cancel.cancel("connor:web_user") == 0 and not tok.cancelled)
    with turn_cancel.scope(tok):
        check("в потоке хода он виден", turn_cancel.current() is tok and tok.started)
        check("пока не отменён — проверка проходит", not raises_cancel(turn_cancel.check))
        check("начатый — отменяется", turn_cancel.cancel("connor:web_user", "новая реплика") == 1)
        check("…проверка — TurnCancelled с причиной", raises_cancel(turn_cancel.check) and tok.reason == "новая реплика")
        check("повторная отмена — ноль (уже)", turn_cancel.cancel("connor:web_user") == 0)
    check("вне хода — ничего не видно", turn_cancel.current() is None and not raises_cancel(turn_cancel.check))
    check("другой чат не задет", turn_cancel.cancel("connor:other") == 0)
    turn_cancel.close_turn(tok)
    check("ход закрыт — в реестре пусто", not turn_cancel.busy("connor:web_user")
          and "connor:web_user" not in turn_cancel._open)
    check("TurnCancelled — не Exception (except Exception её не глотает)",
          not issubclass(turn_cancel.TurnCancelled, Exception))
    seen = []

    def worker(t):
        with turn_cancel.scope(t):
            seen.append(turn_cancel.current() is t)

    t2 = turn_cancel.open_turn("k")
    th = threading.Thread(target=worker, args=(t2,))
    th.start()
    th.join()
    check("свой поток — свой ход (ContextVar)", seen == [True] and turn_cancel.current() is None)
    turn_cancel.close_turn(t2)


# ===== B =====

def test_length():
    section("B. Длина ответа")
    ru = [{"role": "user", "content": "почему они меня не слушают"}]
    en = [{"role": "user", "content": "why don't they listen to me"}]
    check("400 токенов по-русски — ~200 слов, 8–17 предложений", reply_length.budget_words(400, "ru") == (200, 8, 17))
    check("100 — ~50 слов, 2–4", reply_length.budget_words(100, "ru") == (50, 2, 4))
    check("400 по-английски — ~300 слов", reply_length.budget_words(400, "en")[0] == 300)
    note = reply_length.webchat_note(400, ru)
    check(f"подсказка веб-чату: {note}", note is not None and "at most about 200 words" in note
          and "8–17 sentences" in note and "keep any format" in note)
    check("язык — по последней реплике пользователя", "about 300 words" in reply_length.webchat_note(400, en))
    check("обычная реплика (1500 токенов) — без подсказки: число слов подталкивало бы растянуть",
          reply_length.webchat_note(1500, ru) is None)
    check("классификатор (10 токенов) — без подсказки", reply_length.webchat_note(10, ru) is None)
    check("нижняя полоса (40 токенов) — ~20 слов, 1–2 предложения",
          "about 20 words (roughly 1–2 sentences)" in reply_length.webchat_note(40, ru))
    check("мусор вместо числа — без подсказки", reply_length.webchat_note("много", ru) is None)
    os.environ["WEBCHAT_LENGTH_HINT"] = "0"
    check("WEBCHAT_LENGTH_HINT=0 — выключено", reply_length.webchat_note(400, ru) is None)
    os.environ.pop("WEBCHAT_LENGTH_HINT")
    cut = "Ох, ну спрашиваешь. Сегодня четвёртое ноября, среда! А вечером я бы сходила в"
    check("оборванное — до последней законченной фразы",
          reply_length.trim_to_sentence(cut) == "Ох, ну спрашиваешь. Сегодня четвёртое ноября, среда!")
    check("законченное — как есть", reply_length.trim_to_sentence("Да. Конечно!") == "Да. Конечно!")
    check("законченная часть крошечная — обрывок оставляем",
          reply_length.trim_to_sentence("Ох. " + "очень длинная фраза без конца " * 5) .startswith("Ох. очень"))
    check("кавычка после точки — фраза закончена",
          reply_length.trim_to_sentence("Он сказал: «Иди.» А потом") == "Он сказал: «Иди.»")


# ===== C =====

class FakeStream:
    def __init__(self, parts, on_part=None):
        self.parts, self.on_part, self.closed, self.sent = parts, on_part, False, 0

    def __iter__(self):
        for p in self.parts:
            if self.on_part:
                self.on_part(self.sent)
            self.sent += 1
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=p))])

    async def __aiter__(self):
        for chunk in self:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.close()

    def close(self):
        self.closed = True


def test_router():
    section("C. Роутер")
    from app.core import router as rmod
    calls = []
    streams = []

    class FakeOpenAI:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create(self, **kw):
            calls.append(kw)
            if kw.get("stream"):
                s = FakeStream(["Ох, ", "ну ", "спрашиваешь", "… ", "дорогой"] * 20, on_part=FakeOpenAI.on_part)
                streams.append(s)
                return s
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Целиком."))])

        on_part = None

    real = rmod.AsyncOpenAI
    rmod.AsyncOpenAI = FakeOpenAI
    r = rmod.ModelRouter.__new__(rmod.ModelRouter)
    r.model_overrides, r._last_key_index, r._provider_sems = {}, {}, {}
    cfg = {"api_keys": ["k1", "k2"], "base_url": "http://x", "model": "m"}
    try:
        check("без хода — обычный вызов одним куском",
              r._call_with_keys("mimo", cfg, [], 0.7, 400, 0.9, 30) == "Целиком." and not calls[-1].get("stream"))
        tok = turn_cancel.open_turn("rodion:web_user")
        with turn_cancel.scope(tok):
            check("ход текстом (без stream_abort) — тоже одним куском",
                  r._call_with_keys("mimo", cfg, [], 0.7, 400, 0.9, 30) == "Целиком." and not calls[-1].get("stream"))
        turn_cancel.close_turn(tok)
        tok = turn_cancel.open_turn("rodion:web_user", stream_abort=True)
        with turn_cancel.scope(tok):
            ans = r._call_with_keys("mimo", cfg, [], 0.7, 400, 0.9, 30)
            check("голосом — потоком, ответ собран целиком", calls[-1].get("stream") is True
                  and ans.startswith("Ох, ну спрашиваешь") and len(ans) > 100 and streams[-1].closed)
            FakeOpenAI.on_part = lambda i: i == 3 and turn_cancel.cancel("rodion:web_user", "новая реплика")
            n = len(calls)
            got = raises_cancel(lambda: r._call_with_keys("mimo", cfg, [], 0.7, 400, 0.9, 30))
            check("отменили посреди ответа — TurnCancelled, соединение закрыто",
                  got and streams[-1].closed and streams[-1].sent <= 5)
            check("…второй ключ не пробовали (фолбэк не глотает отмену)", len(calls) == n + 1)
            FakeOpenAI.on_part = None
            n = len(calls)
            check("уже отменён — модель не зовут вовсе",
                  raises_cancel(lambda: r._call_with_keys("mimo", cfg, [], 0.7, 400, 0.9, 30)) and len(calls) == n)
            check("…и веб-чат тоже не зовут",
                  raises_cancel(lambda: r._try_webchat([], 0.7, 400, 0.9, 30, sites=["deepseek"])))
        turn_cancel.close_turn(tok)
        # Поток с on_token (/api/chat/stream): отмена посреди
        tok = turn_cancel.open_turn("rodion:web_user")
        got = []
        with turn_cancel.scope(tok):
            FakeOpenAI.on_part = lambda i: i == 2 and turn_cancel.cancel("rodion:web_user")
            check("потоковый ответ (on_token) — тоже обрывается",
                  raises_cancel(lambda: r._stream_with_keys("mimo", cfg, [], got.append, 0.7, 400, 0.9, 30))
                  and streams[-1].closed and len(got) <= 3)
            FakeOpenAI.on_part = None
        turn_cancel.close_turn(tok)
    finally:
        rmod.AsyncOpenAI = real

    from app.features.web_llm import WebChatLLM
    msgs = [{"role": "system", "content": "PERSONA"}, {"role": "user", "content": "почему они меня не слушают"}]
    note = reply_length.webchat_note(400, msgs)
    joined = WebChatLLM._join_messages(msgs + [{"role": "system", "content": note}])
    check("подсказка длины — в блоке инструкций веб-чату, реплика после",
          joined.startswith("Instructions") and note in joined.split("User:")[0])
    src = (ROOT / "app" / "features" / "web_llm.py").read_text("utf-8")
    check("веб-чат: подсказка из max_tokens и проверка отмены до отправки",
          "reply_length.webchat_note(max_tokens, messages)" in src and "turn_cancel.check()" in src)


# ===== D =====

class FakeBot:
    features = {}
    router = SimpleNamespace(_last_provider="mimo", active_provider="mimo", model_for=lambda p: "m")

    def __init__(self):
        self.started = threading.Event()
        self.calls = []

    def control_mode_on(self, chat_id):
        return False

    def process_message(self, text, **kw):
        self.calls.append((text, kw))
        if text == "долго":
            self.started.set()
            # Как роутер: модель пишет, между кусками — проверка отмены
            for _ in range(500):
                turn_cancel.check()
                time.sleep(0.01)
            return "Длинное эссе."
        return f"Ответ на «{text}»."

    def pop_pending_split_messages(self, chat_id):
        return []

    def pop_pending_list_messages(self, chat_id):
        return []

    def pop_pending_question_kind(self, chat_id):
        return None

    def pop_pending_photos(self, chat_id):
        return []

    def cc_answer_options(self, *a):
        return None

    def cc_task_card(self, *a):
        return None


def test_core():
    section("D. Ядро: /api/chat/cancel, supersede, отменённый ход")
    from fastapi.testclient import TestClient
    from app.api import server
    from app.api.schemas import ChatRequest
    os.environ.pop("WAKE_LISTEN", None)
    c = TestClient(server.app)
    tok_ = getattr(server, "_api_token", "")
    hdr = {"Authorization": f"Bearer {tok_}"} if tok_ else {}

    tok = turn_cancel.open_turn("connor:web_user")
    with turn_cancel.scope(tok):
        r = c.post("/api/chat/cancel", json={"persona": "connor", "chat_id": "web_user"}, headers=hdr)
    check(f"POST /api/chat/cancel — начатый ход отменён: {r.json()}", r.status_code == 200
          and r.json() == {"cancelled": 1} and tok.cancelled)
    turn_cancel.close_turn(tok)
    r = c.post("/api/chat/cancel", json={"persona": "connor"}, headers=hdr)
    check("нечего отменять — 0", r.json() == {"cancelled": 0})
    check("чужое имя персоны — 422", c.post("/api/chat/cancel", json={"persona": "../x"}, headers=hdr).status_code == 422)

    # supersede: новая реплика обрывает начатый ход этого чата
    calls = []

    async def fake_turn(bot, req, request):
        calls.append(req.message)
        return server.ChatResponse(reply="ок", persona=req.persona, chat_id="web_user")

    async def fake_get_bot(name):
        return FakeBot()

    orig = server._chat_turn, server._get_bot
    server._chat_turn, server._get_bot = fake_turn, fake_get_bot
    try:
        tok = turn_cancel.open_turn("connor:web_user")
        with turn_cancel.scope(tok):
            r = c.post("/api/chat", json={"persona": "connor", "message": "ну ты где", "chat_id": "web_user"}, headers=hdr)
            check("без supersede — прежний ход не тронут (новая встаёт в очередь)", not tok.cancelled and calls == ["ну ты где"])
            r = c.post("/api/chat", json={"persona": "connor", "message": "ну ты где", "chat_id": "web_user",
                                          "supersede": True}, headers=hdr)
            check("supersede — прежний ход отменён, новая реплика пошла", tok.cancelled and r.json()["reply"] == "ок")
        turn_cancel.close_turn(tok)
    finally:
        server._chat_turn, server._get_bot = orig

    # Настоящий _chat_in_turn: ход «долго» отменили посреди генерации
    bot = FakeBot()

    async def scenario():
        req = ChatRequest(persona="rodion", message="долго", chat_id="web_user", voice=True)
        key = "rodion:web_user"
        first = asyncio.create_task(server._chat_in_turn(bot, req, key, "web_user", None))
        await asyncio.to_thread(bot.started.wait, 5)
        t0 = time.monotonic()
        n = turn_cancel.cancel(key, "новая реплика")
        res = await first
        dt = time.monotonic() - t0
        req2 = ChatRequest(persona="rodion", message="ну ты где", chat_id="web_user", voice=True)
        res2 = await asyncio.wait_for(server._chat_in_turn(bot, req2, key, "web_user", None), 5)
        return n, res, dt, res2

    n, res, dt, res2 = asyncio.run(scenario())
    check(f"отменили посреди генерации — ответ cancelled, пустой (за {dt:.2f} с, а не 5 с)",
          n == 1 and res.cancelled and res.reply == "" and dt < 1.0)
    check("голосом — в ход ушла пометка spoken (короткий ответ, потолок)",
          bot.calls[0][1].get("spoken") is True)
    check("следующая реплика — сразу, лок чата свободен", res2.reply == "Ответ на «ну ты где»." and not res2.cancelled)
    check("флажки закрыты — реестр пуст", not turn_cancel.busy("rodion:web_user"))
    check("генерация чата не висит", server._generating.get("rodion:web_user", 0) == 0)


# ===== E =====

def make_bot(answer, on_call=None):
    from app.bot_instance import BotInstance
    from app.features.conversation_style import ConversationStyleConfig
    from app.features.list_offers import ListOffers
    saved = []
    calls = []

    class Persona:
        settings, persona_data = {}, {"name": "Rodion"}

        def prepare_messages(self, *a, **kw):
            return [{"role": "system", "content": "SYS"}]

        def get_settings(self):
            return {"max_tokens": 1500}

    class Memory:
        class _STM:
            def get_last(self, *a, **kw):
                return []

        class _LTM:
            def get_facts_by_category(self, *a, **kw):
                return []

            def save_facts(self, *a, **kw):
                pass

        def __init__(self):
            self.stm, self.ltm = self._STM(), self._LTM()

        def add_message(self, role, content, *a, **kw):
            saved.append((role, content))

        def get_context(self, *a, **kw):
            return [], [], []

        def get_chat_facts_block(self, *a, **kw):
            return ""

    class Router:
        answer_provider, _last_provider = None, "mimo"

        def is_local_primary(self):
            return False

        def supports_vision(self):
            return False

        def get_response(self, messages, **kw):
            calls.append(kw)
            if on_call:
                on_call(len(calls))
            return answer if len(calls) == 1 else " и дальше."

    b = BotInstance.__new__(BotInstance)
    b.persona_name, b.context, b.owner = "rodion", f"tc_{time.time_ns()}", "web_user"
    b.web_single_user, b._cc_allowed_users = True, set()
    b.features = {"file_access": False}
    b.trigger_words = ["родион"]
    b.intellect = SimpleNamespace(active=False)
    b.conversation_style = ConversationStyleConfig(None)
    b._control_mode = set()
    b.computer_control = b.scenario_manager = b.task_agent = b.proactive = None
    b.addons, b.self_memory, b.living = [], None, None
    b.todo_manager = b.inventory_manager = b.reminder_manager = None
    b.learning_manager = b.file_db = None
    b._punish_enabled = b._moderation_enabled = False
    b._web_search_enabled, b._web_search_disabled_chats = False, set()
    b._pending_list_messages, b._pending_split_messages = {}, {}
    b._pending_photos, b._pending_question_kind, b._pending_more_photos = {}, {}, {}
    b.persona, b.memory, b.router, b.list_offers = Persona(), Memory(), Router(), ListOffers()
    return b, calls, saved


def test_turn():
    section("E. Ход персоны: потолок голоса, без «продолжи», отмена до записи")
    from app.bot_instance import SPOKEN_MAX_TOKENS
    long_cut = ("Ох, ну спрашиваешь. " * 40) + "А вечером я бы сходила в"
    b, calls, saved = make_bot(long_cut)
    reply = b.process_message("почему они меня не слушают", user_id="web_user", chat_id="web_user", spoken=True)
    check(f"голосом — max_tokens не больше {SPOKEN_MAX_TOKENS} (у персоны 1500)",
          calls and calls[0].get("max_tokens") == SPOKEN_MAX_TOKENS)
    check("оборванный ответ — без «продолжи» (один вызов модели)", len(calls) == 1)
    check("…срезан до законченной фразы", reply.rstrip().endswith("спрашиваешь.") and "сходила в" not in reply)
    b, calls, saved = make_bot(long_cut)
    reply = b.process_message("почему они меня не слушают", user_id="web_user", chat_id="web_user")
    check("текстом — как было: лимит персоны и догенерация", calls[0].get("max_tokens") == 1500 and len(calls) >= 2)
    b, calls, saved = make_bot("Коротко, без точки в конце")
    reply = b.process_message("ну как", user_id="web_user", chat_id="web_user", spoken=True)
    check("короткий ответ без точки голосом — не трогаем (не упёрся в потолок)", reply == "Коротко, без точки в конце")

    # Отменили, пока модель писала (веб-чат дописал после отмены)
    tok = turn_cancel.open_turn("rodion:web_user", stream_abort=True)
    b, calls, saved = make_bot("Длинный ответ.", on_call=lambda n: turn_cancel.cancel("rodion:web_user", "новая реплика"))
    with turn_cancel.scope(tok):
        got = raises_cancel(lambda: b.process_message("почему", user_id="web_user", chat_id="web_user", spoken=True))
    turn_cancel.close_turn(tok)
    check("отменили, пока модель писала, — TurnCancelled из хода", got)
    check(f"…ответ в историю не лёг (реплика пользователя — да): {saved}",
          not any(role == "assistant" for role, _ in saved))


# ===== F =====

class StreamBot(FakeBot):
    """Заглушка для /api/chat/stream: «долго» — модель пишет и проверяет
    отмену; «сайт» — веб-чат: дописывает, не глядя на флажок (1 с), отмена
    видна только после ответа; «агент» — прогон агента задач."""

    def __init__(self):
        super().__init__()
        self.site_done = threading.Event()
        self.stream_abort = []
        self.ta_cancelled = []
        bot = self

        class TA:
            busy_chats = set()

            def busy(self, chat):
                return chat in self.busy_chats

            def cancel(self, chat):
                bot.ta_cancelled.append(chat)
                self.busy_chats.discard(chat)
                return "Отменяю."
        self.task_agent = TA()

    def process_message(self, text, **kw):
        tok = turn_cancel.current()
        self.stream_abort.append(bool(tok and tok.stream_abort))
        if text == "сайт":
            self.started.set()
            time.sleep(1.0)
            self.site_done.set()
            turn_cancel.check()
            return "Ответ сайта."
        if text == "агент":
            self.task_agent.busy_chats.add("web_user")
            self.started.set()
            for _ in range(300):
                if "web_user" not in self.task_agent.busy_chats:
                    return "Остановилась на шаге 2."
                time.sleep(0.01)
            return "Готово."
        return super().process_message(text, **kw)


def _sse(body: str) -> list:
    import json
    return [json.loads(ln[6:]) for ln in body.split("\n") if ln.startswith("data: ")]


def test_stop_button():
    section("F. Кнопка «Стоп»: /api/chat/stream и /api/chat/cancel")
    import httpx
    from app.api import server
    tok_ = getattr(server, "_api_token", "")
    hdr = {"Authorization": f"Bearer {tok_}"} if tok_ else {}
    bot = StreamBot()

    async def fake_get_bot(name):
        return bot

    orig = server._get_bot, server.runtime.registry.peek
    server._get_bot = fake_get_bot
    server.runtime.registry.peek = lambda p: bot
    key = "connor:web_user"
    msg = lambda m: {"persona": "connor", "message": m, "chat_id": "web_user"}  # noqa: E731

    async def scenario():
        out = {}
        tr = httpx.ASGITransport(app=server.app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=tr, base_url="http://127.0.0.1:8000", headers=hdr) as c:
            # 1. Модель пишет — «Стоп» обрывает её, стрим кончается cancelled
            bot.started.clear()
            first = asyncio.create_task(c.post("/api/chat/stream", json=msg("долго")))
            await asyncio.to_thread(bot.started.wait, 5)
            out["gen_before"] = server._chat_generating(key)
            t0 = time.monotonic()
            r = await c.post("/api/chat/cancel", json={"persona": "connor", "chat_id": "web_user"})
            out["cancel1"] = r.json()
            out["events1"] = _sse((await first).text)
            out["dt1"] = time.monotonic() - t0
            out["gen_after1"] = server._chat_generating(key)
            r = await asyncio.wait_for(c.post("/api/chat/stream", json=msg("ну ты где")), 5)
            out["events2"] = _sse(r.text)
            # 2. Веб-чат: поток дописывает, стрим — сразу
            bot.started.clear()
            third = asyncio.create_task(c.post("/api/chat/stream", json=msg("сайт")))
            await asyncio.to_thread(bot.started.wait, 5)
            t0 = time.monotonic()
            await c.post("/api/chat/cancel", json={"persona": "connor", "chat_id": "web_user"})
            out["events3"] = _sse((await third).text)
            out["dt3"] = time.monotonic() - t0
            out["site_running"] = not bot.site_done.is_set()
            out["count_mid"] = server._generating.get(key, 0)
            out["gen_mid"] = server._chat_generating(key)
            await asyncio.to_thread(bot.site_done.wait, 5)
            for _ in range(100):
                if not server._generating.get(key) and not turn_cancel.busy(key) \
                        and not turn_cancel.cancelled_count(key):
                    break
                await asyncio.sleep(0.02)
            out["count_end"] = server._generating.get(key, 0)
            out["open_end"] = turn_cancel.cancelled_count(key)
            # 3. Агент: «Стоп» — его стоп, ход отвечает итогом
            bot.started.clear()
            fourth = asyncio.create_task(c.post("/api/chat/stream", json=msg("агент")))
            await asyncio.to_thread(bot.started.wait, 5)
            # supersede-реплика ждёт лока за агентом — в фоне
            sup = asyncio.create_task(c.post("/api/chat", json={**msg("ну ты где"), "supersede": True}))
            await asyncio.sleep(0.2)
            out["supersede_agent"] = list(bot.ta_cancelled)
            r = await c.post("/api/chat/cancel", json={"persona": "connor", "chat_id": "web_user"})
            out["cancel4"] = r.json()
            out["events4"] = _sse((await fourth).text)
            out["sup"] = (await sup).json()
        return out

    try:
        out = asyncio.run(scenario())
    finally:
        server._get_bot = orig[0]
        server.runtime.registry.peek = orig[1]
        if "peek" in server.runtime.registry.__dict__:
            del server.runtime.registry.__dict__["peek"]

    ev1 = out["events1"]
    check(f"«печатает», пока модель пишет: {out['gen_before']}", out["gen_before"])
    check(f"POST /api/chat/cancel — {out['cancel1']}", out["cancel1"] == {"cancelled": 1})
    check(f"стрим кончился done+cancelled без текста (за {out['dt1']:.2f} с, а не 5 с)",
          ev1 and ev1[-1].get("done") and ev1[-1].get("cancelled") is True
          and ev1[-1].get("reply") == "" and not any("token" in e for e in ev1) and out["dt1"] < 1.0)
    check("«печатает» погас", not out["gen_after1"])
    check("API-провайдер веб-чата зовётся потоком (обрыв посреди ответа)", bot.stream_abort[:1] == [True])
    ev2 = out["events2"]
    check("следующая реплика — обычный ответ", ev2 and ev2[-1].get("reply") == "Ответ на «ну ты где»."
          and not ev2[-1].get("cancelled"))
    ev3 = out["events3"]
    check(f"веб-чат дописывает — стрим всё равно кончился сразу ({out['dt3']:.2f} с, сайт пишет: {out['site_running']})",
          ev3 and ev3[-1].get("cancelled") is True and out["dt3"] < 0.6 and out["site_running"])
    check(f"…лок и счётчик держатся, пока поток не вышел ({out['count_mid']}), а «печатает» не горит",
          out["count_mid"] == 1 and not out["gen_mid"])
    check(f"поток вышел — счётчик и флажки сняты ({out['count_end']}, {out['open_end']})",
          out["count_end"] == 0 and out["open_end"] == 0)
    check(f"supersede работу агента не обрывает: {out['supersede_agent']}", out["supersede_agent"] == [])
    check(f"«Стоп» агенту — его стоп: {out['cancel4']}", out["cancel4"] == {"cancelled": 0, "stopping": True}
          and bot.ta_cancelled == ["web_user"])
    ev4 = out["events4"]
    check("ход агента ответил итогом (не обрыв)", ev4 and ev4[-1].get("reply") == "Остановилась на шаге 2."
          and not ev4[-1].get("cancelled"))
    check("реплика за ним — своим ходом", out["sup"].get("reply") == "Ответ на «ну ты где»."
          and not out["sup"].get("cancelled"))


def main():
    test_tokens()
    test_length()
    test_router()
    test_core()
    test_turn()
    test_stop_button()
    print(f"\n{ok - failures}/{ok} OK" + (f", FAIL: {failures}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

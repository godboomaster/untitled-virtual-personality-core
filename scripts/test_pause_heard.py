"""Тест паузы до ответа в ленте реплик (server._pause_heard, app/api/said):
пользователь написал — heard с текстом его сообщения (портрет на столе,
виджет и PiP показывают его облачком сбоку, персона «читает» и думает), ход
закончен — settled. Фраз паузы и вызовов модели на паузу нет.

  - ход (_run_generation): heard с текстом сразу, settled в конце, ответ
    между ними; длинный текст обрезан; картинка без подписи — heard без
    текста; слэш-команда (pending нет) — ничего; две реплики в один чат —
    у каждой свой heard; чат и персона — из ключа хода;
  - SSE /api/chat/stream: событий filler нет, финал — ответ;
  - лента said: heard с текстом и settled без него, повтором не считаются
    (ответ с тем же текстом, что сообщение, не съеден); после перезапуска
    ядра события паузы не повторяются.

LLM, Ollama и сеть не вызываются, data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_pause_heard
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="pause_heard_")

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


def _reset():
    from app.api import said
    said._recent.clear()


def _events(said, persona="don"):
    return [(m["kind"], m["text"]) for m in said._recent if m["persona"] == persona]


async def _turn(server_mod, gen_key, delay, pending, mid=None):
    def fn():
        if mid:
            time.sleep(delay / 2)
            mid()
            time.sleep(delay / 2)
        else:
            time.sleep(delay)
        return "ответ"

    return await server_mod._run_generation(gen_key, fn, pending=pending)


def _pending(text, image=False):
    return {"text": text, "ts": time.time(), "client": "", "image": image}


# ════════════ A. Ход ════════════

def test_turn():
    section("A. Ход: heard с текстом → settled")
    import app.api.server as server_mod
    from app.api import said

    _reset()
    res = asyncio.run(_turn(server_mod, "don:web_user", 0.1, _pending("  почему небо голубое?  "),
                            mid=lambda: said.note("don", "web_user", "Потому что рассеяние.")))
    ev = _events(said)
    check("ответ отдан", res == "ответ")
    check("heard → reply → settled", [k for k, _ in ev] == ["heard", "reply", "settled"])
    check("heard — текст сообщения (без пробелов по краям)", ev[0][1] == "почему небо голубое?")
    check("settled — без текста", ev[-1][1] == "")

    _reset()
    asyncio.run(_turn(server_mod, "don:web_user", 0.01, _pending("я" * 2000)))
    check("длинное сообщение — обрезано", len(_events(said)[0][1]) == server_mod.HEARD_CHARS)

    _reset()
    asyncio.run(_turn(server_mod, "don:web_user", 0.01, _pending("", image=True)))
    check("картинка без подписи — heard без текста", _events(said) == [("heard", ""), ("settled", "")])

    _reset()
    asyncio.run(_turn(server_mod, "don:web_user", 0.01, None))
    check("слэш-команда (pending нет) — ничего", _events(said) == [])

    _reset()

    async def two():
        return await asyncio.gather(_turn(server_mod, "don:web_user", 0.15, _pending("первое")),
                                    _turn(server_mod, "don:web_user", 0.05, _pending("второе")))
    asyncio.run(two())
    ev = _events(said)
    check("две реплики в один чат — у каждой свой heard и settled",
          [t for k, t in ev if k == "heard"] == ["первое", "второе"]
          and [k for k, _ in ev].count("settled") == 2)

    _reset()
    asyncio.run(_turn(server_mod, "kara:tg_42", 0.01, _pending("привет")))
    check("чат и персона — из ключа хода", [m["chat_id"] for m in said._recent] == ["tg_42"] * 2
          and all(m["persona"] == "kara" for m in said._recent))


# ════════════ B. SSE ════════════

class StreamBot:
    """Бот для /api/chat/stream: ровно то, что трогает потоковый ход."""
    features = {}
    living = None
    router = SimpleNamespace(_last_provider=None, active_provider=None)
    persona = SimpleNamespace(system_prompt="Ты — Дон Кихот.")

    def __init__(self, delay):
        self.delay = delay
        self.stm = []
        self._local_router = None
        self.memory = SimpleNamespace(stm=SimpleNamespace(
            get_messages=lambda **kw: list(self.stm),
            pop_last_n=lambda n, key: [self.stm.pop() for _ in range(min(n, len(self.stm)))],
            add_message=lambda role, text, uid, key: self.stm.append(
                {"role": role, "content": text, "timestamp": time.time()})))

    def process_message(self, text, chat_id=None, **kw):
        time.sleep(self.delay)
        reply = "Поезжай, друг мой."
        self.stm = [{"role": "user", "content": text, "timestamp": time.time()},
                    {"role": "assistant", "content": reply, "timestamp": time.time()}]
        return reply

    def pop_pending_photos(self, chat_id):
        return []

    def _turn_stm_tail(self, key):
        return self.stm

    def chat_user_language(self, key):
        return "ru"

    def control_mode_on(self, key):
        return False

    def pop_pending_split_messages(self, chat_id):
        return []

    def pop_pending_list_messages(self, chat_id):
        return []

    def pop_pending_question_kind(self, chat_id):
        return None

    def pop_pending_files(self, chat_id):
        return []

    def pop_reply_history_text(self, chat_id):
        return None

    def cc_answer_options(self, *a):
        return None

    def cc_task_card(self, *a):
        return None


def test_stream():
    section("B. Потоковый чат: без фраз паузы")
    import app.api.server as server_mod
    from app.api import said
    from app.api.schemas import ChatRequest
    _reset()
    bot = StreamBot(0.3)

    async def fake_get_bot(persona):
        return bot

    async def fake_touch(persona):
        return None

    async def run(req):
        resp = await server_mod.chat_stream(req, SimpleNamespace(
            headers={"host": "127.0.0.1:8000"}, client=SimpleNamespace(host="127.0.0.1")))
        events = []
        async for chunk in resp.body_iterator:
            for frame in chunk.split("\n\n"):
                if frame.startswith("data: "):
                    events.append(json.loads(frame[6:]))
        return events

    orig = server_mod._get_bot, server_mod._touch_persona
    server_mod._get_bot, server_mod._touch_persona = fake_get_bot, fake_touch
    try:
        events = asyncio.run(run(ChatRequest(persona="don", message="стоит ли ехать?", chat_id="web_user")))
    finally:
        server_mod._get_bot, server_mod._touch_persona = orig
    check("событий filler нет", not any("filler" in e for e in events))
    done = events[-1] if events else {}
    check("финал — ответ", done.get("done") and done.get("reply") == "Поезжай, друг мой.")
    kinds = [k for k, _ in _events(said)]
    check("лента: heard с текстом в начале хода, settled в конце",
          kinds[:1] == ["heard"] and kinds[-1:] == ["settled"]
          and _events(said)[0][1] == "стоит ли ехать?")


# ════════════ C. Лента said ════════════

def test_said():
    section("C. Лента реплик: события паузы")
    from app.api import said
    _reset()
    start = said.cursor()
    said.note("don", "web_user", "Привет", kind="heard")
    said.note("don", "web_user", "Привет", kind="heard")
    said.note("don", "web_user", "", kind="reply")
    check("heard с текстом без проверки повтора, пустая реплика — нет",
          [m["kind"] for m in said._recent] == ["heard", "heard"])
    said.note("don", "web_user", "Привет")
    check("ответ с тем же текстом, что сообщение, — не съеден", said._recent[-1]["kind"] == "reply")
    said.note("don", "web_user", "", kind="settled")
    check("settled без текста встаёт", said._recent[-1]["kind"] == "settled")
    items, _ = said.take(0, "web_user", fresh_sec=said.REPLAY_SEC)
    check("после перезапуска ядра (fresh_sec) — только реплики", [m["kind"] for m in items] == ["reply"])
    items, cur = said.take(start, "web_user")
    check("свой запуск — все события по порядку, heard с текстом",
          [(m["kind"], m["text"]) for m in items]
          == [("heard", "Привет"), ("heard", "Привет"), ("reply", "Привет"), ("settled", "")]
          and cur == said.cursor())
    check("filler — больше не событие паузы", "filler" not in said.PAUSE_KINDS)


def main():
    test_turn()
    test_stream()
    test_said()
    print(f"\nИтого: {ok - failures}/{ok} OK, FAIL: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

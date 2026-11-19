"""Тест ленты реплик персон (app/api/said, GET /api/said) — облачко портрета
на рабочем столе (desktop/src-tauri/src/portrait.rs).

  - note/take: реплики по номеру, только своего чата; пустые и повтор той же
    реплики в тот же чат за DEDUP_SEC не встают; номер больше текущего
    (прошлый запуск ядра) — только номер; fresh_sec — не старше;
  - откуда реплики: запись ответа в историю веб-персоны (STM api_<персона>,
    роль assistant — в ленту; реплика пользователя, контекст Telegram и
    переписка задним числом — нет) и фоновое сообщение (inbox_push); фоновое,
    записанное и в историю тем же текстом, — один раз;
  - долгий опрос: пустой ответ ждёт реплики, будится из другого потока, не
    отдаёт реплики другого чата, отвечает по таймауту и при остановке ядра;
  - маршрут: первый запрос (без epoch) — сразу и только номер; свой epoch —
    реплики после since с именем персоны; чужой epoch — реплики этого
    запуска за последние REPLAY_SEC; из сети без токена — 401.

LLM, Ollama и сеть не вызываются, data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_said
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="said_")

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


def _reset(said):
    said._recent.clear()
    said._expected_kinds.clear()


def _texts(items):
    return [(m["persona"], m["text"]) for m in items]


# ════════════ A. note/take ════════════

def test_note_take():
    section("A. note/take")
    from app.api import said

    _reset(said)
    items, start = said.take(None)
    check("since=None — только номер", items == [] and start == said._seq)

    said.note("don", "web_user", "  Привет  ")
    said.note("don", "web_user", "")
    said.note("don", "web_user", "   ")
    said.note("verso", "web_user", "Bonjour")
    said.note("don", "tg_42", "в другой чат")
    items, cur = said.take(start)
    check("реплики по порядку, без пустых и чужого чата, текст обрезан",
          _texts(items) == [("don", "Привет"), ("verso", "Bonjour")])
    check("номер ответа — последний", cur == said._seq)
    check("в реплике persona, text, kind, ts",
          set(items[0]) == {"persona", "text", "kind", "ts"} and items[0]["kind"] == "reply")
    check("другой чат — по своему chat_id", _texts(said.take(start, "tg_42")[0]) == [("don", "в другой чат")])

    said.note("don", "web_user", "Привет")
    check("повтор той же реплики в тот же чат — не встаёт", said.take(cur)[0] == [])
    said.note("verso", "web_user", "Привет")
    check("тот же текст у другой персоны — встаёт", _texts(said.take(cur)[0]) == [("verso", "Привет")])
    cur = said._seq

    # Повтор позже DEDUP_SEC — снова реплика
    said._recent[-1]["ts"] -= said.DEDUP_SEC + 1
    said.note("verso", "web_user", "Привет")
    check("тот же текст позже DEDUP_SEC — встаёт", _texts(said.take(cur)[0]) == [("verso", "Привет")])

    items, c = said.take(said._seq + 5)
    check("номер больше текущего (прошлый запуск) — только номер", items == [] and c == said._seq)

    _reset(said)
    said.note("don", "web_user", "давно")
    said._recent[-1]["ts"] -= 600
    said.note("don", "web_user", "только что")
    check("fresh_sec — только не старше", _texts(said.take(0, fresh_sec=120)[0]) == [("don", "только что")])

    for i in range(said._recent.maxlen + 10):
        said.note("don", "web_user", f"n{i}")
    check("лента ограничена", len(said._recent) == said._recent.maxlen)


# ════════════ B. Откуда реплики ════════════

def _fake_stm(context):
    from app.core.memory import ShortTermMemory
    stm = ShortTermMemory.__new__(ShortTermMemory)
    stm.context = context
    stm._lock = threading.RLock()
    bufs = {}
    stm._get_buffer = lambda cid: bufs.setdefault(cid, deque(maxlen=50))
    stm._save_to_db = lambda *a, at=None, **k: at if at is not None else time.time()
    stm._stamp_last_message = lambda *a, **k: None
    return stm


def test_sources():
    section("B. Ответы из истории и фоновые сообщения")
    import app.api.server  # noqa: F401 — подключает слушателя записи в историю
    from app.api import inbox, said

    _reset(said)
    start = said._seq
    stm = _fake_stm("api_don")
    stm.add_message("user", "как дела?", "web_user")
    stm.add_message("assistant", "Отлично, сеньор!", "web_user")
    stm.add_message("assistant", "задним числом", "web_user", timestamp=time.time() - 3600)
    _fake_stm("tg").add_message("assistant", "из Telegram", "42")
    _fake_stm("api_verso").add_message("assistant", "C'est parti", "web_user")
    items, cur = said.take(start)
    check("ответ веб-персоны — в ленте; реплика пользователя, задним числом и Telegram — нет",
          _texts(items) == [("don", "Отлично, сеньор!"), ("verso", "C'est parti")])

    inbox.inbox_push("don", "web_user", "Напоминаю: обед", kind="message")
    inbox.inbox_push("don", "web_user", "Как прошёл день?", kind=inbox.KIND_INITIATIVE)
    items, cur2 = said.take(cur)
    check("фоновые сообщения — в ленте со своим kind",
          [(m["text"], m["kind"]) for m in items]
          == [("Напоминаю: обед", "message"), ("Как прошёл день?", inbox.KIND_INITIATIVE)])
    stm.add_message("assistant", "Как прошёл день?", "web_user")
    check("фоновое, записанное и в историю тем же текстом, — один раз", said.take(cur2)[0] == [])


# ════════════ C. Долгий опрос ════════════

def test_initiative_delivery():
    section("Инициатива: STM, inbox и ночное сообщение ритма")
    import app.api.server  # noqa: F401 — реальный обработчик записи STM
    from types import SimpleNamespace
    from app.api import inbox, said
    from app.core.turn_gate import ChatTurnGate
    from app.features.rhythm_manager import RhythmConfig, RhythmManager

    _reset(said)
    start = said.cursor()
    said.expect_kind("don", "web_user", "Спокойной ночи", "initiative")
    said.note("don", "web_user", "Спокойной ночи")
    said.note("don", "web_user", "Спокойной ночи", "initiative")
    said.note("don", "web_user", "Спокойной ночи")
    items, _ = said.take(start)
    check("STM и inbox дают только одну инициативу без обычной реплики",
          [(m["text"], m["kind"]) for m in items] == [("Спокойной ночи", "initiative")])

    _reset(said)
    said.expect_kind("don", "web_user", "Первое", "initiative")
    said.expect_kind("don", "web_user", "Второе", "initiative")
    start = said.cursor()
    said.note("don", "web_user", "Первое")
    said.note("don", "web_user", "Второе")
    check("параллельные ожидания одного чата сохраняют тип каждого текста",
          [m["kind"] for m in said.take(start)[0]] == ["initiative", "initiative"])

    _reset(said)
    said.note("don", "web_user", "Ранняя копия")
    consumed = said.cursor()
    said.note("don", "web_user", "Ранняя копия", "initiative")
    check("исправленный тип получает новый номер для уже прочитавшего клиента",
          [m["kind"] for m in said.take(consumed)[0]] == ["initiative"])
    consumed = said.cursor()
    said.note("don", "web_user", "Ранняя копия")
    said.note("don", "web_user", "Ранняя копия", "initiative")
    check("повтор после исправления не выдаётся снова", said.take(consumed)[0] == [])

    for gated in (False, True):
        _reset(said)
        start = said.cursor()
        before_send = []

        class Sender(inbox.WebInboxSender):
            async def send_message(self, *args, **kwargs):
                before_send.extend(said.take(start)[0])
                return await super().send_message(*args, **kwargs)

        stm = _fake_stm("api_don")
        memory = SimpleNamespace(stm=stm, add_message=stm.add_message)
        manager = RhythmManager(
            context="api_don", config=RhythmConfig.from_dict(True), memory=memory,
            sender=Sender("don", kind=inbox.KIND_INITIATIVE),
            turn_gate=ChatTurnGate() if gated else None,
        )
        sent = asyncio.run(manager._send("web_user", "Пора отдохнуть", "night"))
        items, _ = said.take(start)
        check(f"ритм {'с гейтом' if gated else 'без гейта'}: только одна инициатива",
              sent is True and [m["kind"] for m in items] == ["initiative"])
        if gated:
            check("тип инициативы выставлен уже при записи STM до доставки",
                  [m["kind"] for m in before_send] == ["initiative"])


def test_reminder_delivery():
    section("Напоминания: индикатор, чат и метка доставки")
    import app.api.server  # noqa: F401 — реальный обработчик записи STM
    from types import SimpleNamespace
    from app.api import inbox, said
    from app.core.turn_gate import ChatTurnGate
    from app.features.reminder_manager import ReminderManager, reminder_ref

    for gated in (False, True):
        _reset(said)
        start = said.cursor()
        before_send = []
        delivered = []

        class Sender(inbox.WebInboxSender):
            async def send_message(self, chat_id, text, **kwargs):
                before_send.extend(said.take(start)[0])
                delivered.append((text, kwargs))
                return await super().send_message(chat_id, text, **kwargs)

        stm = _fake_stm("api_don")
        manager = ReminderManager(context="api_don")
        manager.set_sender(Sender("don"))
        manager.set_memory(SimpleNamespace(stm=stm, add_message=stm.add_message))
        if gated:
            manager.set_turn_gate(ChatTurnGate())
        manager.add_reminder("web_user", "", "полить цветы", 0, topic_id=17)
        reminder = manager._reminders[-1]
        sent = asyncio.run(manager._fire(reminder))
        items, consumed = said.take(start)
        check(f"напоминание {'с гейтом' if gated else 'без гейта'} — одно событие reminder",
              sent is True and len(items) == 1 and items[0]["kind"] == said.REMINDER_KIND)
        check("сохранены текст, топик и ref для подавления повтора на телефоне",
              len(delivered) == 1 and "полить цветы" in delivered[0][0]
              and delivered[0][1] == {"topic_id": 17, "kind": inbox.KIND_REMINDER, "ref": reminder_ref(reminder)})
        if gated:
            check("до отправки запись STM уже имеет тип reminder",
                  [m["kind"] for m in before_send] == [said.REMINDER_KIND])
        said.note("don", "web_user", items[0]["text"])
        said.note("don", "web_user", items[0]["text"], kind=said.REMINDER_KIND)
        check("копии STM и inbox не повторяют напоминание", said.take(consumed)[0] == [])


def test_wait():
    section("C. Долгий опрос")
    from app.api import inbox, said

    _reset(said)
    cur = said._seq

    def push_later(delay, *args):
        def run():
            time.sleep(delay)
            said.note(*args)
        threading.Thread(target=run, daemon=True).start()

    push_later(0.3, "don", "web_user", "проснись")
    t0 = time.monotonic()
    items, cur2 = asyncio.run(said.wait(cur, 10))
    dt = time.monotonic() - t0
    check(f"реплика из другого потока будит ожидание ({dt:.2f} с)",
          _texts(items) == [("don", "проснись")] and 0.2 < dt < 3)

    push_later(0.2, "don", "tg_1", "чужой чат")
    push_later(0.5, "don", "web_user", "своему")
    items, cur3 = asyncio.run(said.wait(cur2, 10))
    check("реплика другого чата не отдаётся, ждём свою", _texts(items) == [("don", "своему")])

    t0 = time.monotonic()
    items, c = asyncio.run(said.wait(cur3, 0.4))
    dt = time.monotonic() - t0
    check(f"таймаут — пусто ({dt:.2f} с)", items == [] and c == said._seq and dt >= 0.35)

    t0 = time.monotonic()
    items, _ = asyncio.run(said.wait(None, 5))
    check("since=None — сразу", items == [] and time.monotonic() - t0 < 0.5)

    inbox.set_stop_check(lambda: True)
    try:
        t0 = time.monotonic()
        items, _ = asyncio.run(said.wait(said._seq, 30))
        dt = time.monotonic() - t0
        check(f"ядро останавливается — отвечает сразу ({dt:.2f} с)", items == [] and dt < 2.5)
    finally:
        inbox.set_stop_check(None)

    async def gone():
        return True
    t0 = time.monotonic()
    items, _ = asyncio.run(said.wait(said._seq, 30, gone=gone))
    check("клиент ушёл — не ждём", items == [] and time.monotonic() - t0 < 2.5)
    check("ожидающих не осталось", not said._waiters)


# ════════════ D. Маршрут ════════════

def test_route():
    section("D. GET /api/said")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    from app.api import said

    orig = server_mod._api_token
    server_mod._api_token = "s3cr3t"
    try:
        client = TestClient(server_mod.app, base_url="http://127.0.0.1")
        _reset(said)
        said.note("don", "web_user", "до портрета")

        t0 = time.monotonic()
        r = client.get("/api/said", params={"wait": 5})
        data = r.json() if r.status_code == 200 else {}
        check("первый запрос (без epoch) — сразу и только номер",
              r.status_code == 200 and data.get("messages") == [] and data.get("cursor") == said._seq
              and data.get("epoch") == said.EPOCH and time.monotonic() - t0 < 1)
        cur = data.get("cursor", 0)

        box = {}

        def long_get():
            box["r"] = client.get("/api/said", params={"wait": 20, "since": cur, "epoch": said.EPOCH})
        th = threading.Thread(target=long_get)
        th.start()
        time.sleep(0.3)
        said.note("don", "web_user", "Ответ")
        th.join(10)
        data = box["r"].json() if "r" in box and box["r"].status_code == 200 else {}
        msgs = data.get("messages") or []
        check("свой epoch — реплика после since, с именем персоны",
              len(msgs) == 1 and msgs[0]["text"] == "Ответ" and msgs[0]["persona"] == "don"
              and msgs[0].get("name") and data.get("cursor") == said._seq)

        said._recent[0]["ts"] -= said.REPLAY_SEC + 5
        r = client.get("/api/said", params={"wait": 0, "since": 999, "epoch": "old000000000"})
        data = r.json() if r.status_code == 200 else {}
        check("чужой epoch — реплики этого запуска за последние REPLAY_SEC",
              [m["text"] for m in data.get("messages", [])] == ["Ответ"])

        r = client.get("/api/said", params={"wait": -1})
        check("wait < 0 — 422", r.status_code == 422)

        lan = TestClient(server_mod.app, base_url="http://127.0.0.1", client=("192.168.1.20", 50000))
        check("из сети без токена — 401", lan.get("/api/said").status_code == 401)
        check("из сети с токеном — 200",
              lan.get("/api/said", headers={"Authorization": "Bearer s3cr3t"}).status_code == 200)
    finally:
        server_mod._api_token = orig


def main():
    test_note_take()
    test_sources()
    test_initiative_delivery()
    test_reminder_delivery()
    test_wait()
    test_route()
    print(f"\nИтого: {ok - failures}/{ok} OK, FAIL: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

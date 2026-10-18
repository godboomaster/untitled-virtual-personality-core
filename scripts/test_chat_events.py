"""Тест: сообщение с одного устройства сразу видно на другом.

Ответ ложится в историю в конце хода, а веб опрашивает inbox раз в 15 с.
Поэтому реплика, отправленная с компьютера, появлялась на телефоне с
опозданием, вместе с ответом или ещё позже. Теперь:

  A. chat_events: номер изменений по персонам; changed_since отдаёт, у кого
     что менялось, None — номер не из окна; wait_changes ждёт, будится
     толчком из другого потока, отвечает по таймауту и при остановке ядра;
  B. ход (_run_generation): реплика видна другим устройствам с начала хода
     (и пока ждёт лока чата за предыдущей) до записи ответа; отправившему —
     нет; начало и конец хода — толчок chat_events; слэш-команды не видны;
  C. запись в историю веб-персоны (STM, контекст api_<персона>) — толчок;
     чужой контекст (Telegram) — нет; фоновое сообщение (inbox_push) — толчок;
  D. маршруты: GET /api/changes (первый — сразу с номером, ожидание
     будится ходом, чужой epoch — «опросить всех»); inbox персоны отдаёт
     pending всем, кроме отправившего.

LLM, Ollama и сеть не вызываются, data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_chat_events
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="chat_events_")

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


# ════════════ A. chat_events ════════════

def test_events():
    section("A. chat_events: номер, окно, ожидание")
    from app.api import chat_events as ce
    from app.api import inbox

    v0, names = ce.changed_since(None)
    check("без номера — «опросить всех»", names is None)
    check("свой номер — пусто", ce.changed_since(v0) == (v0, []))
    ce.bump("connor")
    ce.bump("arrodes")
    ce.bump("connor")
    v1, names = ce.changed_since(v0)
    check("после толчков — номер вырос на 3", v1 == v0 + 3)
    check("персоны с изменениями — без повторов", names == ["arrodes", "connor"])
    check("номер больше текущего (чужой процесс) — опросить всех",
          ce.changed_since(v1 + 10)[1] is None)
    for _ in range(ce._recent.maxlen + 5):
        ce.bump("x")
    check("номер старше окна — опросить всех", ce.changed_since(v0)[1] is None)

    async def scenario():
        v, _ = ce.changed_since(None)
        t0 = time.monotonic()
        got = await ce.wait_changes(v, 0.3)
        check("пусто — ждёт до таймаута и отвечает пустым списком",
              got == (v, []) and time.monotonic() - t0 >= 0.25)

        threading.Timer(0.3, ce.bump, args=("connor",)).start()
        t0 = time.monotonic()
        v2, names = await ce.wait_changes(v, 10)
        dt = time.monotonic() - t0
        check("толчок из другого потока будит ожидание", names == ["connor"] and v2 == v + 1)
        check("ответ сразу (с паузой на пачку), а не по таймауту", 0.25 <= dt < 1.5)

        # Пачка: три толчка подряд — один ответ со всеми
        def burst():
            ce.bump("a")
            time.sleep(0.02)
            ce.bump("b")
        threading.Timer(0.2, burst).start()
        _, names = await ce.wait_changes(v2, 10)
        check("пачка изменений — одним ответом", names == ["a", "b"])

        v3, _ = ce.changed_since(None)
        inbox.set_stop_check(lambda: True)
        try:
            t0 = time.monotonic()
            got = await ce.wait_changes(v3, 30)
            check("ядро останавливается — ожидание отпускается сразу",
                  got == (v3, []) and time.monotonic() - t0 < 2)
        finally:
            inbox.set_stop_check(None)

        async def gone():
            return True
        t0 = time.monotonic()
        await ce.wait_changes(v3, 30, gone=gone)
        check("клиент ушёл — ожидание отпускается", time.monotonic() - t0 < 2)
        check("ожидающих не осталось", not ce._waiters)

    asyncio.run(scenario())


# ════════════ B. ход: реплика в обработке ════════════

class _Req:
    def __init__(self, message, client_id=None, image=None):
        self.message = message
        self.client_id = client_id
        self.image = image


def test_turn():
    section("B. ход: реплика видна другим устройствам до ответа")
    import app.api.server as server
    from app.api import chat_events as ce

    key = "connor:web_user"
    check("слэш-команда — не показывается", server._pending_entry(_Req("/clear")) is None)
    check("пустая без картинки — нет", server._pending_entry(_Req("   ")) is None)
    e = server._pending_entry(_Req("", image="data:image/png;base64,AA"))
    check("картинка без текста — есть, с пометкой image", e is not None and e["image"] is True)

    async def scenario():
        first_go = threading.Event()
        second_seen = {}

        def fn_first():
            first_go.wait(5)
            return "ответ 1"

        def fn_second():
            return "ответ 2"

        v0, _ = ce.changed_since(None)
        t1 = asyncio.create_task(server._run_generation(
            key, fn_first, pending=server._pending_entry(_Req("привет с компа", client_id="laptop1"))))
        await asyncio.sleep(0.1)
        _, names = ce.changed_since(v0)
        check("начало хода — толчок персоне", names == ["connor"])
        check("телефон видит реплику компьютера",
              [p["text"] for p in server._pending_public(key, "phone1")] == ["привет с компа"])
        check("у реплики — время отправки", abs(server._pending_public(key, "phone1")[0]["ts"]
                                               - time.time()) < 5)
        check("компьютер свою реплику не получает", server._pending_public(key, "laptop1") == [])
        check("клиент без id видит всё", len(server._pending_public(key, "")) == 1)

        # Вторая реплика с телефона ждёт лока чата за первой — уже видна
        t2 = asyncio.create_task(server._run_generation(
            key, fn_second, pending=server._pending_entry(_Req("и ещё", client_id="phone1"))))
        await asyncio.sleep(0.1)
        second_seen["laptop"] = [p["text"] for p in server._pending_public(key, "laptop1")]
        check("реплика, ждущая лока, видна компьютеру", second_seen["laptop"] == ["и ещё"])
        check("третьему устройству видны обе по порядку",
              [p["text"] for p in server._pending_public(key, "tablet")] == ["привет с компа", "и ещё"])

        v1, _ = ce.changed_since(None)
        first_go.set()
        r1, r2 = await asyncio.gather(t1, t2)
        check("ответы на месте", (r1, r2) == ("ответ 1", "ответ 2"))
        check("после ответа реплик в обработке нет", server._pending_public(key, "tablet") == []
              and key not in server._pending_user)
        check("флаг «генерирует» снят", key not in server._generating)
        _, names = ce.changed_since(v1)
        check("конец хода — толчок персоне", names == ["connor"])

        def boom():
            raise RuntimeError("сбой")
        try:
            await server._run_generation(key, boom, pending=server._pending_entry(_Req("упадёт")))
        except RuntimeError:
            pass
        check("ошибка хода — реплика снята", key not in server._pending_user)

    asyncio.run(scenario())


# ════════════ C. запись в историю и фоновые сообщения ════════════

def test_stm_hook():
    section("C. запись в историю, фоновое сообщение — толчок")
    import app.api.server  # noqa: F401 — регистрирует слушателя записи
    from app.api import chat_events as ce
    from app.api import inbox
    from app.core import memory

    v0, _ = ce.changed_since(None)
    memory._notify_stm_write("api_connor", "web_user")
    _, names = ce.changed_since(v0)
    check("история веб-персоны — толчок ей", names == ["connor"])
    v1, _ = ce.changed_since(None)
    memory._notify_stm_write("connor", "12345")
    check("чужой контекст (Telegram) — без толчка", ce.changed_since(v1) == (v1, []))
    inbox.inbox_push("arrodes", "web_user", "напоминание")
    _, names = ce.changed_since(v1)
    check("фоновое сообщение — толчок персоне", names == ["arrodes"])

    class _Stm(memory.ShortTermMemory):
        def __init__(self):  # без Chroma: только то, что зовёт add_message
            self.context = "api_connor"

        def _get_buffer(self, chat_id):
            from collections import deque
            return deque(maxlen=10)

        def _save_to_db(self, *a, **kw):
            return time.time()

        def _stamp_last_message(self, *a, **kw):
            pass
    v2, _ = ce.changed_since(None)
    _Stm().add_message("user", "привет", "web_user", "web_user")
    _, names = ce.changed_since(v2)
    check("add_message в STM будит открытый веб", names == ["connor"])


# ════════════ D. маршруты ════════════

def test_routes():
    section("D. GET /api/changes и pending в inbox")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server
    from app.api import chat_events as ce

    orig = server._api_token
    server._api_token = ""
    try:
        client = TestClient(server.app, base_url="http://127.0.0.1")
        t0 = time.monotonic()
        r = client.get("/api/changes")
        data = r.json() if r.status_code == 200 else {}
        check("первый запрос — сразу, номер и epoch",
              set(data) == {"version", "epoch", "personas"} and data["epoch"] == ce.EPOCH
              and time.monotonic() - t0 < 1)
        v = data.get("version", 0)
        r = client.get("/api/changes", params={"since": v, "epoch": "old", "wait": 20})
        check("номер прошлого запуска — сразу и «опросить всех»",
              r.status_code == 200 and r.json()["personas"] is None)
        r = client.get("/api/changes", params={"since": -1})
        check("отрицательный номер — 422", r.status_code == 422)

        box = {}

        def long_get():
            box["r"] = client.get("/api/changes", params={"since": v, "epoch": ce.EPOCH, "wait": 20})
            box["t"] = time.monotonic()
        th = threading.Thread(target=long_get)
        t0 = time.monotonic()
        th.start()
        time.sleep(0.4)
        # Ход на другом устройстве: реплика в обработке
        key = "connor:web_user"
        entry = server._pending_entry(_Req("с компьютера", client_id="laptop1"))
        server._generating_enter(key, entry)
        try:
            th.join(10)
            r = box.get("r")
            data = r.json() if r is not None and r.status_code == 200 else {}
            check("ожидание будится началом хода, ответ — персона",
                  data.get("personas") == ["connor"] and box["t"] - t0 < 3)

            r = client.get("/api/personas/connor/inbox", params={"client_id": "phone1"})
            data = r.json() if r.status_code == 200 else {}
            check("inbox телефона: генерация и реплика компьютера",
                  data.get("generating") is True
                  and [p.get("text") for p in data.get("pending", [])] == ["с компьютера"])
            check("поля реплики — text и ts", bool(data.get("pending"))
                  and set(data["pending"][0]) == {"text", "ts"})
            r = client.get("/api/personas/connor/inbox", params={"client_id": "laptop1"})
            check("inbox компьютера: своей реплики нет",
                  r.status_code == 200 and r.json().get("pending") == [])
        finally:
            server._generating_exit(key, entry)
        r = client.get("/api/personas/connor/inbox", params={"client_id": "phone1"})
        check("после хода — pending пуст", r.status_code == 200 and r.json().get("pending") == [])

        r = client.post("/api/chat", json={"persona": "connor", "message": "x", "client_id": "bad id!"})
        check("client_id с недопустимыми символами в /api/chat — 422", r.status_code == 422)
    finally:
        server._api_token = orig


def main():
    test_events()
    test_turn()
    test_stm_hook()
    test_routes()

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

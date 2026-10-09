"""Тест общего inbox (GET /api/inbox): новые фоновые сообщения сразу по всем
персонам одним запросом — его опрашивает фоновая служба приложения на
телефоне, чтобы показать уведомление, пока приложение свёрнуто или закрыто.

  - inbox_pop_all: каждый клиент получает свою копию каждого сообщения, по
    всем персонам чата и в порядке появления; повторный опрос пуст; другой
    chat_id не смешивается; курсор общий с inbox_pop (по персоне) — одно и
    то же сообщение клиент получает один раз, каким бы путём ни спросил;
  - маршрут: имя персоны из её YAML (нет персоны — id), без токена при
    заданном API_TOKEN — 401, неверный или пустой client_id — 422;
  - долгий опрос (wait/since/epoch): пустой ответ ждёт сообщения, будится
    толчком из другого потока, отвечает по таймауту и при остановке ядра;
    курсор клиента (since) возвращает потерянный ответ ещё раз, курсор
    чужого процесса (другой epoch) не применяется; без новых параметров
    ответ прежний — список.

LLM, Ollama и сеть не вызываются, data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_inbox_all
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="inbox_all_")

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


def _reset_inbox(inbox):
    inbox._inbox.clear()
    inbox._delivered.clear()
    inbox._cursors.clear()
    inbox._client_seen.clear()
    inbox._all_seen.clear()


def _pairs(items):
    return [(m["persona"], m["text"]) for m in items]


# ════════════ A. inbox_pop_all ════════════

def test_pop_all():
    section("A. inbox_pop_all: все персоны одним опросом")
    from app.api import inbox

    _reset_inbox(inbox)
    # Оба клиента уже опрашивали (курсоры есть) — каждый получает всё новое
    check("первый опрос пустой очереди — пусто", inbox.inbox_pop_all("web_user", "phone-bg") == [])
    inbox.inbox_pop_all("web_user", "laptop")

    inbox.inbox_push("p", "web_user", "раз")
    inbox.inbox_push("q", "web_user", "два")
    inbox.inbox_push("p", "web_user", "три")
    inbox.inbox_push("p", "tg_chat", "чужой чат")

    got = inbox.inbox_pop_all("web_user", "phone-bg")
    check("служба получает сообщения всех персон в порядке появления",
          _pairs(got) == [("p", "раз"), ("q", "два"), ("p", "три")])
    check("поля — persona/text/kind/ts", got and set(got[0]) == {"persona", "text", "kind", "ts"})
    check("повторный опрос — пусто", inbox.inbox_pop_all("web_user", "phone-bg") == [])
    check("второй клиент получает свою копию того же",
          _pairs(inbox.inbox_pop_all("web_user", "laptop")) == [("p", "раз"), ("q", "два"), ("p", "три")])
    check("второй клиент повторно — пусто", inbox.inbox_pop_all("web_user", "laptop") == [])

    check("чужой chat_id не смешивается: сообщение другого чата — только по своему chat_id",
          _pairs(inbox.inbox_pop_all("tg_chat", "phone-bg")) == [("p", "чужой чат")])
    check("…и по web_user его нет", inbox.inbox_pop_all("web_user", "phone-bg") == [])

    # Курсор общий с inbox_pop: забранное по персоне не приходит повторно
    inbox.inbox_push("p", "web_user", "четыре")
    inbox.inbox_push("q", "web_user", "пять")
    check("по персоне клиент забрал своё",
          [m["text"] for m in inbox.inbox_pop("p", "web_user", "phone-bg")] == ["четыре"])
    check("общий опрос того же клиента — только остальное",
          _pairs(inbox.inbox_pop_all("web_user", "phone-bg")) == [("q", "пять")])
    # Новый клиент без курсора — только ещё никому не доставленное (как у inbox_pop)
    check("новый клиент: уже доставленное не получает",
          inbox.inbox_pop_all("web_user", "fresh") == [])

    # Служба (свой client_id) и веб приложения (свой) — не крадут друг у друга
    _reset_inbox(inbox)
    inbox.inbox_pop("p", "web_user", "app")
    inbox.inbox_pop_all("web_user", "app-bg")
    inbox.inbox_push("p", "web_user", "напоминание")
    check("служба получила напоминание",
          _pairs(inbox.inbox_pop_all("web_user", "app-bg")) == [("p", "напоминание")])
    check("веб того же телефона получает его своим опросом",
          [m["text"] for m in inbox.inbox_pop("p", "web_user", "app")] == ["напоминание"])

    # Новая персона появилась после первого опроса — её сообщения приходят
    inbox.inbox_push("new", "web_user", "привет от новой")
    check("сообщение персоны без курсора у клиента приходит",
          _pairs(inbox.inbox_pop_all("web_user", "app-bg")) == [("new", "привет от новой")])
    _reset_inbox(inbox)


# ════════════ B. Маршрут GET /api/inbox ════════════

def test_route():
    section("B. GET /api/inbox")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    from app.api import inbox

    orig = server_mod._api_token
    server_mod._api_token = "s3cr3t"
    try:
        # С API_TOKEN токен нужен и по localhost (адрес в сети отсекает ещё
        # и список разрешённых хостов API_ALLOWED_HOSTS — здесь он не проверяется)
        client = TestClient(server_mod.app, base_url="http://127.0.0.1")
        auth = {"Authorization": "Bearer s3cr3t"}
        _reset_inbox(inbox)

        r = client.get("/api/inbox", params={"client_id": "phone1234-bg"})
        check("без токена при заданном API_TOKEN — 401", r.status_code == 401)
        r = client.get("/api/inbox", params={"client_id": "phone1234-bg"},
                       headers={"Authorization": "Bearer nope"})
        check("неверный токен — 401", r.status_code == 401)
        r = client.get("/api/inbox", params={"client_id": "bad id!"}, headers=auth)
        check("client_id с недопустимыми символами — 422", r.status_code == 422)
        r = client.get("/api/inbox", params={"client_id": "x" * 65}, headers=auth)
        check("client_id длиннее 64 — 422", r.status_code == 422)
        r = client.get("/api/inbox", params={"client_id": ""}, headers=auth)
        check("пустой client_id — 422", r.status_code == 422)
        r = client.get("/api/inbox", headers=auth)
        check("без client_id — 422", r.status_code == 422)

        r = client.get("/api/inbox", params={"client_id": "phone1234-bg"}, headers=auth)
        check("первый опрос — 200 и пустой список", r.status_code == 200 and r.json() == [])
        client.get("/api/inbox", params={"client_id": "laptop5678"}, headers=auth)

        inbox.inbox_push("connor", "web_user", "Напоминаю: созвон в 15:00", kind="reminder")
        inbox.inbox_push("gone_persona", "web_user", "от удалённой")
        inbox.inbox_push("connor", "other_chat", "чужой чат")

        r = client.get("/api/inbox", params={"client_id": "phone1234-bg"}, headers=auth)
        data = r.json() if r.status_code == 200 else None
        check("200 и список", isinstance(data, list))
        data = data or []
        check("два сообщения web_user (чужой чат не смешан)", len(data) == 2)
        first = data[0] if data else {}
        check("поля persona/name/text/kind/ts",
              set(first) == {"persona", "name", "text", "kind", "ts"})
        check("имя персоны — из YAML", first.get("persona") == "connor" and first.get("name") == "Коннор")
        check("kind и текст на месте",
              first.get("kind") == "reminder" and first.get("text") == "Напоминаю: созвон в 15:00")
        check("нет персоны — вместо имени id",
              len(data) > 1 and data[1].get("name") == "gone_persona")

        r = client.get("/api/inbox", params={"client_id": "phone1234-bg"}, headers=auth)
        check("повторный опрос — пусто", r.status_code == 200 and r.json() == [])
        r = client.get("/api/inbox", params={"client_id": "laptop5678"}, headers=auth)
        check("второй клиент получает свои копии",
              r.status_code == 200 and [m["text"] for m in r.json()]
              == ["Напоминаю: созвон в 15:00", "от удалённой"])
        r = client.get("/api/inbox", params={"client_id": "phone1234-bg", "chat_id": "other_chat"},
                       headers=auth)
        check("chat_id=other_chat — только сообщения своего чата",
              r.status_code == 200 and [m["text"] for m in r.json()] == ["чужой чат"])
        _reset_inbox(inbox)
    finally:
        server_mod._api_token = orig


# ════════════ C. Долгий опрос: курсор клиента и ожидание ════════════

def test_long_poll():
    section("C. Долгий опрос: since, ожидание, таймаут, остановка")
    from app.api import inbox

    _reset_inbox(inbox)
    items, c0 = inbox.inbox_take("web_user", "svc-bg")
    check("первый take — пусто, курсор = текущий номер", items == [] and c0 == inbox._seq)
    inbox.inbox_push("connor", "web_user", "раз")
    items, c1 = inbox.inbox_take("web_user", "svc-bg", c0)
    check("since: новое после курсора", [m["text"] for m in items] == ["раз"] and c1 > c0)
    items, _ = inbox.inbox_take("web_user", "svc-bg", c0)
    check("since: ответ потерялся — тот же курсор отдаёт сообщение снова",
          [m["text"] for m in items] == ["раз"])
    items, c2 = inbox.inbox_take("web_user", "svc-bg", c1)
    check("since: с нового курсора — пусто", items == [] and c2 == c1)
    inbox.inbox_push("connor", "web_user", "два")
    items, _ = inbox.inbox_take("web_user", "svc-bg", 10 ** 9)
    check("since больше текущего номера (чужой процесс) — как без since",
          [m["text"] for m in items] == ["два"])
    items, _ = inbox.inbox_take("web_user", "svc-bg")
    check("…и курсоры ядра сдвинулись вместе с ним", items == [])
    # Курсор клиента не отбирает сообщения у других клиентов
    inbox.inbox_pop_all("web_user", "laptop")
    inbox.inbox_push("lena", "web_user", "три")
    items, c3 = inbox.inbox_take("web_user", "svc-bg", inbox._seq - 1)
    check("since: сообщение получено", [m["text"] for m in items] == ["три"])
    check("…ноутбук получает свою копию",
          [m["text"] for m in inbox.inbox_pop_all("web_user", "laptop")] == ["три"])

    async def run_wait(since, timeout):
        t0 = time.monotonic()
        res = await inbox.inbox_wait("web_user", "svc-bg", since, timeout)
        return res, time.monotonic() - t0

    inbox.inbox_push("connor", "web_user", "уже есть")
    (items, c4), dt = asyncio.run(run_wait(c3, 5))
    check("ожидание: сообщение уже есть — ответ сразу",
          [m["text"] for m in items] == ["уже есть"] and dt < 0.5)

    (items, c5), dt = asyncio.run(run_wait(c4, 0.4))
    check("ожидание: таймаут — пусто, примерно через wait",
          items == [] and 0.35 <= dt < 1.5 and c5 == c4)

    def push_later():
        time.sleep(0.3)
        inbox.inbox_push("lena", "web_user", "из другого потока")
    threading.Thread(target=push_later).start()
    (items, c6), dt = asyncio.run(run_wait(c5, 10))
    check("ожидание: толчок из другого потока будит сразу",
          [m["text"] for m in items] == ["из другого потока"] and dt < 1.5)

    def push_other_chat():
        time.sleep(0.2)
        inbox.inbox_push("lena", "other_chat", "чужой чат")
    threading.Thread(target=push_other_chat).start()
    (items, _), dt = asyncio.run(run_wait(c6, 0.8))
    check("ожидание: сообщение другого чата не отдаётся, ждём дальше",
          items == [] and dt >= 0.7)

    stop = {"on": False}
    inbox.set_stop_check(lambda: stop["on"])
    try:
        def stop_later():
            time.sleep(0.3)
            stop["on"] = True
        threading.Thread(target=stop_later).start()
        (items, _), dt = asyncio.run(run_wait(c6, 30))
        check("ожидание: остановка ядра — ответ в пределах секунды-двух",
              items == [] and dt < 2.5)
    finally:
        inbox.set_stop_check(None)
    gone = {"on": False}

    async def is_gone():
        return gone["on"]

    async def run_wait_gone():
        t0 = time.monotonic()
        res = await inbox.inbox_wait("web_user", "svc-bg", c6, 30, gone=is_gone)
        return res, time.monotonic() - t0

    def leave_later():
        time.sleep(0.3)
        gone["on"] = True
    threading.Thread(target=leave_later).start()
    (items, _), dt = asyncio.run(run_wait_gone())
    check("ожидание: клиент закрыл соединение — запрос отпускается",
          items == [] and dt < 2.5)
    check("после ожиданий не осталось ждущих", not inbox._waiters)
    _reset_inbox(inbox)


def test_route_long_poll():
    section("D. GET /api/inbox: долгий опрос")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    from app.api import inbox

    orig = server_mod._api_token
    server_mod._api_token = ""
    try:
        client = TestClient(server_mod.app, base_url="http://127.0.0.1")
        _reset_inbox(inbox)
        base = {"client_id": "svc-bg"}

        r = client.get("/api/inbox", params=base)
        check("без wait/since — прежний ответ: список", r.status_code == 200 and r.json() == [])
        r = client.get("/api/inbox", params={**base, "wait": -1})
        check("wait < 0 — 422", r.status_code == 422)
        t0 = time.monotonic()
        r = client.get("/api/inbox", params={**base, "epoch": ""})
        data = r.json() if r.status_code == 200 else {}
        check("первый запрос службы (пустой epoch, без wait) — сразу, с курсором",
              set(data) == {"messages", "cursor", "epoch"} and time.monotonic() - t0 < 1)

        t0 = time.monotonic()
        r = client.get("/api/inbox", params={**base, "wait": 0.3})
        data = r.json() if r.status_code == 200 else {}
        check("wait: пусто — объект messages/cursor/epoch после ожидания",
              set(data) == {"messages", "cursor", "epoch"} and data["messages"] == []
              and data["epoch"] == inbox.EPOCH and time.monotonic() - t0 >= 0.25)
        cursor = data.get("cursor", 0)

        box = {}

        def long_get():
            box["r"] = client.get("/api/inbox", params={**base, "wait": 20, "since": cursor,
                                                        "epoch": inbox.EPOCH})
            box["t"] = time.monotonic()
        th = threading.Thread(target=long_get)
        t0 = time.monotonic()
        th.start()
        time.sleep(0.4)
        inbox.inbox_push("connor", "web_user", "Пора!", kind="reminder")
        th.join(10)
        r = box.get("r")
        data = r.json() if r is not None and r.status_code == 200 else {}
        msgs = data.get("messages") or []
        check("ожидающий запрос отвечает, как только пришло сообщение",
              [m.get("text") for m in msgs] == ["Пора!"] and box["t"] - t0 < 3)
        check("поля сообщения — как в прежнем ответе",
              bool(msgs) and set(msgs[0]) == {"persona", "name", "text", "kind", "ts"}
              and msgs[0]["name"] == "Коннор")

        r = client.get("/api/inbox", params={**base, "since": cursor, "epoch": inbox.EPOCH})
        check("тот же since — ответ повторяется (потерянный по дороге не пропадёт)",
              r.status_code == 200 and [m["text"] for m in r.json()["messages"]] == ["Пора!"])
        r = client.get("/api/inbox", params={**base, "since": data.get("cursor"), "epoch": inbox.EPOCH})
        check("новый since — пусто, сразу (wait не задан)",
              r.status_code == 200 and r.json()["messages"] == [])
        r = client.get("/api/inbox", params={**base, "since": cursor, "epoch": "old-process"})
        check("курсор прошлого запуска ядра (другой epoch) не применяется",
              r.status_code == 200 and r.json()["messages"] == [])
        _reset_inbox(inbox)
    finally:
        server_mod._api_token = orig


def main():
    test_pop_all()
    test_route()
    test_long_poll()
    test_route_long_poll()

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Тест доступа с телефона (приложение на Android, ядро на ноутбуке):

  - без API_TOKEN ядро отвечает только запросам по localhost: запрос по
    другому имени хоста (обратный прокси, адрес в сети) — 403 с подсказкой, а не
    открытый доступ; с токеном — обычная проверка Bearer;
  - фоновые сообщения (inbox) получает каждое устройство: у клиента свой
    курсор, новый клиент получает только то, что ещё никому не доставлено;
    смена id персоны переносит и курсоры, архив id — снимает очередь;
  - токен API для приложения (GET /api/token) получает только тот, кто его
    уже знает (или эта машина, если токена нет); телефону по VPC Link — 403.

Запуск: PYTHONPATH=. python3 scripts/test_mobile_access.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

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


def _request(host: str, extra: dict | None = None):
    from starlette.requests import Request
    headers = [(b"host", host.encode())]
    headers += [(k.lower().encode(), v.encode()) for k, v in (extra or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/api/personas",
                    "headers": headers})


def _auth(server_mod, host: str, token: str | None = None, extra: dict | None = None):
    # → HTTP-статус отказа require_auth или 200, если пропустил
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token) if token else None
    try:
        asyncio.run(server_mod.require_auth(_request(host, extra), creds))
        return 200
    except HTTPException as e:
        return e.status_code


# ════════════ A. Токен для удалённого доступа ════════════

def test_remote_needs_token():
    section("A. Без API_TOKEN — только localhost")
    import app.api.server as server_mod

    orig = server_mod._api_token
    try:
        server_mod._api_token = ""
        check("localhost:8000 без токена — пропускает", _auth(server_mod, "localhost:8000") == 200)
        check("127.0.0.1:8000 без токена — пропускает", _auth(server_mod, "127.0.0.1:8000") == 200)
        check("[::1]:8000 без токена — пропускает", _auth(server_mod, "[::1]:8000") == 200)
        check("имя через прокси без токена — 403",
              _auth(server_mod, "my-mac.example.net") == 403)
        check("адрес в сети без токена — 403", _auth(server_mod, "192.168.1.5:8000") == 403)
        check("пустой Host без токена — 403", _auth(server_mod, "") == 403)
        # Обратный прокси мог оставить Host = 127.0.0.1 — запрос
        # всё равно удалённый: его выдаёт X-Forwarded-For / Forwarded
        check("через прокси (X-Forwarded-For) по 127.0.0.1 — 403",
              _auth(server_mod, "127.0.0.1:8000", extra={"X-Forwarded-For": "100.101.102.103"}) == 403)
        check("через прокси (Forwarded) по localhost — 403",
              _auth(server_mod, "localhost:8000", extra={"Forwarded": "for=100.101.102.103"}) == 403)

        server_mod._api_token = "s3cr3t"
        check("с API_TOKEN: имя через прокси и верный токен — пропускает",
              _auth(server_mod, "my-mac.example.net", "s3cr3t") == 200)
        check("с API_TOKEN: без токена — 401",
              _auth(server_mod, "my-mac.example.net") == 401)
        check("с API_TOKEN: неверный — 401",
              _auth(server_mod, "my-mac.example.net", "nope") == 401)
        check("с API_TOKEN: localhost без токена — тоже 401",
              _auth(server_mod, "localhost:8000") == 401)
        check("с API_TOKEN: через прокси и верный токен — пропускает",
              _auth(server_mod, "127.0.0.1:8000", "s3cr3t",
                    extra={"X-Forwarded-For": "100.101.102.103"}) == 200)
    finally:
        server_mod._api_token = orig

    # Через приложение целиком: отказ 403 приходит телом {"detail": ...}
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    orig = server_mod._api_token
    server_mod._api_token = ""
    try:
        client = TestClient(server_mod.app, base_url="http://127.0.0.1")
        r = client.get("/api/health")
        check("health по localhost — 200", r.status_code == 200)
        r = client.get("/api/personas/connor/inbox", params={"client_id": "bad id!"})
        check("inbox: client_id с недопустимыми символами — 422", r.status_code == 422)
        r = client.get("/api/personas/connor/inbox", params={"client_id": "x" * 65})
        check("inbox: client_id длиннее 64 — 422", r.status_code == 422)
        r = client.get("/api/personas/connor/inbox", params={"client_id": "phone_1-a"})
        check("inbox: допустимый client_id — 200", r.status_code == 200)
    finally:
        server_mod._api_token = orig


def test_api_token_endpoint():
    section("A2. Токен API для приложения (GET /api/token)")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod

    orig = server_mod._api_token
    try:
        client = TestClient(server_mod.app, base_url="http://127.0.0.1")
        remote = TestClient(server_mod.app, base_url="http://192.168.1.5:8000")

        server_mod._api_token = ""
        r = client.get("/api/token")
        check("без API_TOKEN, localhost — 200 и token: null",
              r.status_code == 200 and r.json() == {"token": None})
        check("ответ не кэшируется (Cache-Control: no-store)",
              r.headers.get("cache-control") == "no-store")
        check("без API_TOKEN, адрес в сети — 403", remote.get("/api/token").status_code == 403)

        server_mod._api_token = "s3cr3t"
        good = {"Authorization": "Bearer s3cr3t"}
        check("с API_TOKEN: без токена — 401", client.get("/api/token").status_code == 401)
        check("с API_TOKEN: неверный — 401",
              client.get("/api/token", headers={"Authorization": "Bearer nope"}).status_code == 401)
        r = client.get("/api/token", headers=good)
        check("с API_TOKEN и верным токеном — 200 и сам токен",
              r.status_code == 200 and r.json() == {"token": "s3cr3t"}
              and r.headers.get("cache-control") == "no-store")
        r = remote.get("/api/token", headers=good)
        check("с API_TOKEN: адрес в сети с верным токеном — 200",
              r.status_code == 200 and r.json() == {"token": "s3cr3t"})
        # Телефон по каналу: Authorization подставил ноутбук, сам телефон
        # токена не знает — и не должен его получить
        r = client.get("/api/token", headers={**good, "X-VPC-Link": "0123456789ab"})
        check("по VPC Link — 403, токена в ответе нет",
              r.status_code == 403 and "s3cr3t" not in r.text)
    finally:
        server_mod._api_token = orig


# ════════════ B. Inbox: курсор на устройство ════════════

def _reset_inbox(inbox):
    inbox._inbox.clear()
    inbox._delivered.clear()
    inbox._cursors.clear()
    inbox._client_seen.clear()


def _texts(items):
    return [m["text"] for m in items]


def test_inbox_cursors():
    section("B. Inbox: каждое устройство получает каждое сообщение")
    from app.api import inbox

    _reset_inbox(inbox)
    inbox.inbox_push("p", "web_user", "раз")
    got = inbox.inbox_pop("p", "web_user", "laptop")
    check("ноутбук получает сообщение", _texts(got) == ["раз"])
    check("поля сообщения — text/kind/ts (без seq)",
          set(got[0]) == {"text", "kind", "ts"})
    check("телефон, опросивший впервые, старое (уже доставленное) не получает",
          inbox.inbox_pop("p", "web_user", "phone") == [])

    inbox.inbox_push("p", "web_user", "два")
    check("новое — ноутбуку", _texts(inbox.inbox_pop("p", "web_user", "laptop")) == ["два"])
    check("новое — и телефону", _texts(inbox.inbox_pop("p", "web_user", "phone")) == ["два"])
    check("повторный опрос — пусто", inbox.inbox_pop("p", "web_user", "phone") == [])

    inbox.inbox_push("p", "web_user", "три")
    inbox.inbox_push("p", "web_user", "четыре")
    check("пачка — по порядку",
          _texts(inbox.inbox_pop("p", "web_user", "phone")) == ["три", "четыре"])
    check("ноутбук получает ту же пачку позже",
          _texts(inbox.inbox_pop("p", "web_user", "laptop")) == ["три", "четыре"])

    # Никто не опрашивал — первый пришедший клиент получает всё накопленное
    inbox.inbox_push("p", "web_user", "пять")
    check("недоставленное получает и новый клиент",
          _texts(inbox.inbox_pop("p", "web_user", "tablet")) == ["пять"])
    check("…и остальные знакомые клиенты",
          _texts(inbox.inbox_pop("p", "web_user", "laptop")) == ["пять"])

    # Чаты и персоны не смешиваются
    inbox.inbox_push("p", "other_chat", "чужой чат")
    inbox.inbox_push("q", "web_user", "чужая персона")
    check("другой чат той же персоны — отдельно",
          inbox.inbox_pop("p", "web_user", "laptop") == []
          and _texts(inbox.inbox_pop("p", "other_chat", "laptop")) == ["чужой чат"])
    check("другая персона — отдельно",
          _texts(inbox.inbox_pop("q", "web_user", "laptop")) == ["чужая персона"])

    # Старый фронт (без client_id) — как прежний pop
    _reset_inbox(inbox)
    inbox.inbox_push("p", "web_user", "a")
    check("без client_id: получает", _texts(inbox.inbox_pop("p", "web_user")) == ["a"])
    check("без client_id: повторно — пусто", inbox.inbox_pop("p", "web_user") == [])


def test_inbox_rename_drop():
    section("C. Inbox: смена id, архив, лимит клиентов")
    from app.api import inbox

    _reset_inbox(inbox)
    inbox.inbox_push("old", "web_user", "доставлено")
    inbox.inbox_pop("old", "web_user", "laptop")
    inbox.inbox_push("old", "web_user", "ещё не забрано")
    inbox.inbox_rename("old", "new")
    check("после смены id старый ключ пуст", inbox.inbox_pop("old", "web_user", "laptop") == [])
    check("ноутбук получает под новым id только незабранное",
          _texts(inbox.inbox_pop("new", "web_user", "laptop")) == ["ещё не забрано"])
    check("новый клиент под новым id — уже доставленное не получает",
          inbox.inbox_pop("new", "web_user", "phone") == [])

    inbox.inbox_push("old2", "web_user", "x")
    inbox.inbox_push("new2", "web_user", "y")
    inbox.inbox_rename("old2", "new2")
    check("слияние очередей при смене id — по порядку",
          _texts(inbox.inbox_pop("new2", "web_user", "laptop")) == ["x", "y"])

    inbox.inbox_push("gone", "web_user", "прежняя персона")
    inbox.inbox_drop("gone")
    check("архив id — очередь снята", inbox.inbox_pop("gone", "web_user", "laptop") == [])

    _reset_inbox(inbox)
    for i in range(inbox._MAX_CLIENTS + 5):
        inbox.inbox_pop("p", "web_user", f"c{i}")
    check("курсоров не больше лимита клиентов",
          len(inbox._cursors) <= inbox._MAX_CLIENTS and len(inbox._client_seen) <= inbox._MAX_CLIENTS)
    check("забываются самые давние", "c0" not in inbox._cursors
          and f"c{inbox._MAX_CLIENTS + 4}" in inbox._cursors)

    inbox._client_seen["stale"] = 0.0
    inbox._cursors["stale"] = {}
    inbox.inbox_pop("p", "web_user", "fresh")
    check("клиент, молчавший дольше недели, забыт", "stale" not in inbox._cursors)
    _reset_inbox(inbox)


def main():
    test_remote_needs_token()
    test_api_token_endpoint()
    test_inbox_cursors()
    test_inbox_rename_drop()

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

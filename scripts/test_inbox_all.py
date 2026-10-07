"""Тест общего inbox (GET /api/inbox): новые фоновые сообщения сразу по всем
персонам одним запросом — его опрашивает фоновая служба приложения на
телефоне, чтобы показать уведомление, пока приложение свёрнуто или закрыто.

  - inbox_pop_all: каждый клиент получает свою копию каждого сообщения, по
    всем персонам чата и в порядке появления; повторный опрос пуст; другой
    chat_id не смешивается; курсор общий с inbox_pop (по персоне) — одно и
    то же сообщение клиент получает один раз, каким бы путём ни спросил;
  - маршрут: имя персоны из её YAML (нет персоны — id), без токена при
    заданном API_TOKEN — 401, неверный или пустой client_id — 422.

LLM, Ollama и сеть не вызываются, data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_inbox_all
"""

import os
import sys
import tempfile
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


def main():
    test_pop_all()
    test_route()

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Тест темы оформления для портрета на рабочем столе (app/core/ui_look):
веб присылает выбор скина, портрет рисует голову рисовкой скина.

  A. Модуль: без файла — обычный вид; выбор целиком ложится в
     data/ui_look.json, повтор того же — «не изменилось»; негодный id скина
     или персоны, «default» общим скином, чужая схема, чужой ключ —
     ValueError, файл не трогается; битый файл — обычный вид, негодные
     пункты из файла пропускаются.
  B. Лента реплик: событие look без персоны и текста — во все чаты, только
     клиентам с acts; старый клиент (без acts) его не видит.
  C. Маршруты: GET/PUT /api/settings/ui-look (422 на негодное), выбор — в
     лёгком списке портретов (GET /api/portraits, поле look); смена будит
     ленту (look), повтор того же — нет; из сети без токена — 401.

LLM, Ollama и сеть не вызываются, data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_ui_look
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="ui_look_")

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


def raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


# ════════════ A. Модуль ════════════

def test_module():
    section("A. app/core/ui_look")
    from app.core import ui_look as ul

    path = ul._path()
    path.unlink(missing_ok=True)
    check("без файла — обычный вид", ul.get() == {"skin": None, "personas": {}, "theme": None} and not path.exists())

    look = {"skin": "clean-neutral", "personas": {"kara": "default", "don": "clean-neutral"}, "theme": "dark"}
    saved, changed = ul.update(look)
    check("выбор целиком — сохранён, изменилось", saved == look and changed)
    check("в файле — то же", json.loads(path.read_text()) == look)
    check("get() — то же", ul.get() == look)
    _, changed = ul.update(dict(look))
    check("повтор того же — не изменилось", not changed)
    saved, changed = ul.update({"skin": None, "personas": {}, "theme": "light"})
    check("обычный вид, светлая — изменилось", changed and ul.get() == {"skin": None, "personas": {}, "theme": "light"})

    before = path.read_text()
    bad = [
        ({"skin": "Clean Neutral"}, "id скина с пробелом и заглавными"),
        ({"skin": "../etc"}, "путь вместо id"),
        ({"skin": "default"}, "«default» общим скином"),
        ({"skin": 5}, "не строка"),
        ({"personas": {"../x": "clean-neutral"}}, "негодный id персоны"),
        ({"personas": {"kara": "Bad Id"}}, "негодный id скина у персоны"),
        ({"personas": ["kara"]}, "список вместо словаря"),
        ({"theme": "sepia"}, "чужая схема"),
        ({"color": "red"}, "чужой ключ"),
    ]
    for patch, label in bad:
        check(f"{label} — ValueError", raises(lambda p=patch: ul.update(p)))
    check("после отказов файл не тронут", path.read_text() == before)
    too_many = {f"p{i}": "clean-neutral" for i in range(ul.MAX_PERSONAS + 1)}
    check("слишком много персон — ValueError", raises(lambda: ul.update({"personas": too_many})))

    path.write_text("{битый json")
    check("битый файл — обычный вид", ul.get() == {"skin": None, "personas": {}, "theme": None})
    path.write_text(json.dumps({"skin": "../x", "personas": {"kara": "clean-neutral", "../y": "a", "don": 3},
                                "theme": "sepia"}))
    check("негодное из файла пропущено, годное — осталось",
          ul.get() == {"skin": None, "personas": {"kara": "clean-neutral"}, "theme": None})
    path.unlink(missing_ok=True)


# ════════════ B. Лента реплик ════════════

def test_said():
    section("B. Лента реплик: событие look")
    from app.api import said

    start = said.cursor()
    said.look_changed()
    items, _ = said.take(start, "web_user", acts=True)
    check("портрету (acts) — look без персоны и текста",
          [(m["kind"], m["persona"], m["text"]) for m in items] == [("look", "", "")])
    items, _ = said.take(start, "phone_chat", acts=True)
    check("клиенту с acts в другом чате — тоже", [m["kind"] for m in items] == ["look"])
    items, _ = said.take(start, "web_user", acts=False)
    check("старому клиенту (без acts) — нет", items == [])
    said.look_changed()
    items, _ = said.take(start, "web_user", acts=True)
    check("второй раз подряд — тоже приходит (не повтор реплики)", [m["kind"] for m in items] == ["look", "look"])


# ════════════ C. Маршруты ════════════

def test_routes():
    section("C. Маршруты")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    from app.api import said
    from app.core import ui_look as ul

    ul._path().unlink(missing_ok=True)
    orig = server_mod._api_token
    server_mod._api_token = "s3cr3t"
    try:
        client = TestClient(server_mod.app, base_url="http://127.0.0.1")
        r = client.get("/api/settings/ui-look")
        check("GET — обычный вид", r.status_code == 200 and r.json() == {"skin": None, "personas": {}, "theme": None})

        look = {"skin": "clean-neutral", "personas": {"kara": "default"}, "theme": "dark"}
        start = said.cursor()
        r = client.put("/api/settings/ui-look", json=look)
        check("PUT — 200, ответ — сохранённое", r.status_code == 200 and r.json() == look)
        items, _ = said.take(start, "web_user", acts=True)
        check("смена будит ленту (look)", [m["kind"] for m in items] == ["look"])
        start = said.cursor()
        client.put("/api/settings/ui-look", json=look)
        items, _ = said.take(start, "web_user", acts=True)
        check("повтор того же — лента молчит", items == [])

        r = client.put("/api/settings/ui-look", json={"skin": "../x"})
        check("негодный id — 422", r.status_code == 422)
        r = client.put("/api/settings/ui-look", json={"theme": "sepia"})
        check("чужая схема — 422", r.status_code == 422)
        r = client.put("/api/settings/ui-look", json={"skin": 5})
        check("не строка — 422", r.status_code == 422)
        check("после отказов — прежнее", ul.get() == look)

        r = client.get("/api/portraits")
        check("список портретов — с look", r.status_code == 200 and r.json().get("look") == look)

        lan = TestClient(server_mod.app, base_url="http://127.0.0.1", client=("192.168.1.20", 50000))
        check("из сети без токена GET — 401", lan.get("/api/settings/ui-look").status_code == 401)
        check("из сети без токена PUT — 401", lan.put("/api/settings/ui-look", json=look).status_code == 401)
        r = lan.put("/api/settings/ui-look", json={"skin": None, "personas": {}, "theme": "light"},
                    headers={"Authorization": "Bearer s3cr3t"})
        check("из сети с токеном — 200", r.status_code == 200 and r.json()["theme"] == "light")
    finally:
        server_mod._api_token = orig
        ul._path().unlink(missing_ok=True)


def main():
    test_module()
    test_said()
    test_routes()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

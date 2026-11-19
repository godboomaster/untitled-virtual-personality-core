"""Тест скорости текста у аватара (app/core/avatar_text) — облачко портрета
на рабочем столе и виджета «Живой портрет» на телефоне.

  A. Модуль: без файла — умолчания (5 слов/с, 6,5 с на часть); частичная
     правка ложится в data/avatar_text.json и видна сразу; 0 слов/с —
     «сразу»; вне пределов, не число, bool, NaN, чужой ключ — ValueError, файл
     не трогается; битый файл — умолчания; правка файла в обход update —
     видна после сверки (не чаще раза в секунду).
  B. Маршруты: GET/PUT /api/settings/avatar-text (422 вне пределов и для
     строки), из сети без токена — 401; скорость едет вместе с репликами
     (GET /api/said) и в лёгком списке портретов (GET /api/portraits).

LLM, Ollama и сеть не вызываются, data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_avatar_text
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="avatar_text_")

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
    section("A. app/core/avatar_text")
    from app.core import avatar_text as at

    at._reset_cache()
    path = at._path()
    check("без файла — умолчания", at.get() == {"words_per_sec": 5.0, "part_sec": 6.5} and not path.exists())

    s = at.update({"words_per_sec": 8})
    check("правка слов — в ответе и сразу в get()", s["words_per_sec"] == 8 and at.get()["words_per_sec"] == 8)
    check("вторая величина не тронута", at.get()["part_sec"] == 6.5)
    on_disk = json.loads(path.read_text())
    check("файл — оба значения", on_disk == {"words_per_sec": 8.0, "part_sec": 6.5})
    check("ответ — с умолчаниями и пределами",
          s["defaults"] == {"words_per_sec": 5.0, "part_sec": 6.5}
          and s["limits"] == {"words_per_sec": [1.0, 20.0], "part_sec": [1.5, 30.0]})

    at.update({"words_per_sec": 0, "part_sec": 3.5})
    check("0 слов/с — «сразу», часть 3,5 с", at.get() == {"words_per_sec": 0.0, "part_sec": 3.5})

    before = path.read_text()
    bad = [
        ({"words_per_sec": 0.5}, "слов меньше 1 (и не 0)"),
        ({"words_per_sec": 21}, "слов больше 20"),
        ({"part_sec": 1}, "часть короче 1,5 с"),
        ({"part_sec": 31}, "часть длиннее 30 с"),
        ({"part_sec": "5"}, "строка"),
        ({"words_per_sec": True}, "bool"),
        ({"part_sec": float("nan")}, "NaN"),
        ({"speed": 2}, "чужой ключ"),
        ({"words_per_sec": 8, "part_sec": 100}, "одно из двух вне пределов"),
    ]
    for patch, name in bad:
        check(f"{name} — ValueError", raises(lambda p=patch: at.update(p)))
    check("после отказов файл не тронут", path.read_text() == before)

    path.write_text("{битый")
    at._reset_cache()
    check("битый файл — умолчания", at.get() == {"words_per_sec": 5.0, "part_sec": 6.5})
    path.write_text(json.dumps({"words_per_sec": 99, "part_sec": 4}))
    at._reset_cache()
    check("значение вне пределов в файле — его умолчание, другое — как есть",
          at.get() == {"words_per_sec": 5.0, "part_sec": 4.0})

    path.write_text(json.dumps({"words_per_sec": 3, "part_sec": 10}))
    os.utime(path, None)
    time.sleep(at.RECHECK_SEC + 0.1)
    check("правка файла в обход update — видна после сверки", at.get() == {"words_per_sec": 3.0, "part_sec": 10.0})

    path.unlink()
    at._reset_cache()


# ════════════ B. Маршруты ════════════

def test_routes():
    section("B. Маршруты")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    from app.core import avatar_text as at

    at._reset_cache()
    orig = server_mod._api_token
    server_mod._api_token = "s3cr3t"
    try:
        client = TestClient(server_mod.app, base_url="http://127.0.0.1")
        r = client.get("/api/settings/avatar-text")
        check("GET — умолчания", r.status_code == 200 and r.json()["words_per_sec"] == 5.0
              and r.json()["part_sec"] == 6.5)
        r = client.put("/api/settings/avatar-text", json={"part_sec": 4})
        check("PUT части — 200, слова не тронуты",
              r.status_code == 200 and r.json()["part_sec"] == 4.0 and r.json()["words_per_sec"] == 5.0)
        r = client.put("/api/settings/avatar-text", json={"words_per_sec": 12.5})
        check("PUT слов (дробное) — 200", r.status_code == 200 and r.json()["words_per_sec"] == 12.5)
        r = client.put("/api/settings/avatar-text", json={"words_per_sec": 50})
        check("вне пределов — 422", r.status_code == 422)
        r = client.put("/api/settings/avatar-text", json={"part_sec": "6"})
        check("строка — 422", r.status_code == 422)
        check("после отказов — прежнее", at.get() == {"words_per_sec": 12.5, "part_sec": 4.0})

        r = client.get("/api/said", params={"wait": 0})
        check("лента реплик — со скоростью текста",
              r.status_code == 200 and r.json().get("text") == {"words_per_sec": 12.5, "part_sec": 4.0})
        r = client.get("/api/portraits")
        check("список портретов — со скоростью текста",
              r.status_code == 200 and r.json().get("text") == {"words_per_sec": 12.5, "part_sec": 4.0})

        lan = TestClient(server_mod.app, base_url="http://127.0.0.1", client=("192.168.1.20", 50000))
        check("из сети без токена — 401", lan.get("/api/settings/avatar-text").status_code == 401)
        check("из сети без токена PUT — 401",
              lan.put("/api/settings/avatar-text", json={"part_sec": 5}).status_code == 401)
        r = lan.put("/api/settings/avatar-text", json={"part_sec": 5}, headers={"Authorization": "Bearer s3cr3t"})
        check("из сети с токеном — 200", r.status_code == 200 and r.json()["part_sec"] == 5.0)
    finally:
        server_mod._api_token = orig
        at._path().unlink(missing_ok=True)
        at._reset_cache()


def main():
    test_module()
    test_routes()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

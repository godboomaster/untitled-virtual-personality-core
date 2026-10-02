"""Тест библиотеки скинов (app/api/skins_api + эндпоинты /api/skins*).

  - создание скина: один комбинированный файл на три экрана хранится один
    раз, список отдаёт метаданные без HTML;
  - чтение скина целиком, правка имени/цветов/экранов, сборка мусора файлов;
  - назначения персонам, удаление скина снимает назначения;
  - лимиты (3 МБ на файл), валидация цветов и id (traversal → 422/404);
  - смена id / удаление персоны держат назначения согласованными;
  - лимит тела POST/PUT /api/skins и /api/skins/generate: 413 по
    Content-Length до чтения тела и по факту для chunked (BodySizeLimit).

Всё на временной VPC_DATA_DIR — настоящая data/ не трогается.

Запуск: /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m scripts.test_skins_api
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

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


COMBINED = (
    '<!DOCTYPE html><html><head><style>:root{--bg:#112233;}</style></head><body>'
    '<div data-vpc-screen="chat"></div><div data-vpc-screen="dossier"></div>'
    '<div data-vpc-screen="room"></div></body></html>'
)
CHAT_ONLY = '<!DOCTYPE html><html><body><div data-vpc-screen="chat">v2</div></body></html>'


def main():
    tmp = Path(tempfile.mkdtemp(prefix="skins_test_"))
    old_env = os.environ.get("VPC_DATA_DIR")
    os.environ["VPC_DATA_DIR"] = str(tmp)
    try:
        run(tmp)
    finally:
        if old_env is None:
            os.environ.pop("VPC_DATA_DIR", None)
        else:
            os.environ["VPC_DATA_DIR"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


def run(tmp: Path):
    from fastapi.testclient import TestClient
    import app.api.server as server_mod
    from app.api import skins_api

    orig_token = server_mod._api_token
    server_mod._api_token = ""
    personas = ["alex", "mira"]
    try:
        with mock.patch.object(server_mod, "list_personas", lambda: list(personas)):
            client = TestClient(server_mod.app)
            run_endpoints(client, tmp)
            run_body_limit(client)
        run_persona_hooks()
        run_body_limit_asgi()
    finally:
        server_mod._api_token = orig_token


def run_endpoints(client, tmp: Path):
    section("1. Создание и список")
    r = client.post("/api/skins", json={
        "name": "Роща", "author": "me", "version": "1.0", "contract": 2,
        "files": [COMBINED], "screens": {"chat": 0, "dossier": 0, "room": 0},
        "colors": {"--bg": "#445566"}, "hue_shift": 30,
    })
    check("POST /api/skins: 200", r.status_code == 200)
    skin = r.json()["skin"]
    sid = skin["id"]
    check("id скина безопасный и не builtin", sid.startswith("sk_"))
    check("три экрана ссылаются на один хэш", len(set(skin["screens"].values())) == 1
          and set(skin["screens"]) == {"chat", "dossier", "room"})
    files = list((tmp / "skins" / "files").glob("*.html"))
    check("комбинированный файл хранится один раз", len(files) == 1)
    check("метаданные: имя, контракт, цвета, сдвиг тона",
          skin["name"] == "Роща" and skin["contract"] == 2
          and skin["colors"] == {"--bg": "#445566"} and skin["hue_shift"] == 30)

    lst = client.get("/api/skins").json()["skins"]
    check("GET /api/skins: один скин", len(lst) == 1 and lst[0]["id"] == sid)
    check("список без HTML", "files" not in lst[0] and "<html" not in str(lst[0]))
    check("размеры экранов в списке", lst[0]["sizes"]["chat"] == len(COMBINED.encode()))

    full = client.get(f"/api/skins/{sid}").json()
    check("GET /api/skins/{id}: файл по хэшу",
          full["files"].get(skin["screens"]["chat"]) == COMBINED)

    section("2. Правка")
    r = client.put(f"/api/skins/{sid}", json={
        "name": "Роща 2", "files": [CHAT_ONLY], "screens": {"chat": 0, "room": None},
    })
    check("PUT: 200", r.status_code == 200)
    upd = r.json()["skin"]
    check("имя обновлено", upd["name"] == "Роща 2")
    check("room убран, chat заменён, dossier на старом файле",
          set(upd["screens"]) == {"chat", "dossier"}
          and upd["screens"]["chat"] != upd["screens"]["dossier"])
    check("цвета не тронуты без поля colors", upd["colors"] == {"--bg": "#445566"})
    check("файлов теперь два", len(list((tmp / "skins" / "files").glob("*.html"))) == 2)
    r = client.put(f"/api/skins/{sid}", json={"screens": {"chat": None, "dossier": None}})
    check("PUT, убирающий все экраны: 400", r.status_code == 400)
    r = client.put(f"/api/skins/{sid}", json={"colors": {"--accent": "rgba(1, 2, 3, 0.5)"}, "hue_shift": -40})
    check("PUT цветов: 200 и значения сохранены", r.status_code == 200
          and r.json()["skin"]["colors"] == {"--accent": "rgba(1, 2, 3, 0.5)"}
          and r.json()["skin"]["hue_shift"] == -40)

    section("3. Валидация")
    r = client.put(f"/api/skins/{sid}", json={"colors": {"--x": "red;} body{display:none"}})
    check("цвет с CSS-инъекцией: 400", r.status_code == 400)
    r = client.put(f"/api/skins/{sid}", json={"colors": {"x": "#fff"}})
    check("имя переменной без --: 400", r.status_code == 400)
    big = "<html>" + "a" * (3 * 1024 * 1024) + "</html>"
    r = client.post("/api/skins", json={"name": "big", "files": [big], "screens": {"chat": 0}})
    check("файл > 3 МБ: 413", r.status_code == 413)
    r = client.post("/api/skins", json={"name": "x", "files": [CHAT_ONLY], "screens": {"lobby": 0}})
    check("неизвестный экран: 400", r.status_code == 400)
    r = client.post("/api/skins", json={"name": "x", "files": [CHAT_ONLY], "screens": {"chat": 3}})
    check("ссылка на несуществующий файл: 400", r.status_code == 400)
    r = client.post("/api/skins", json={"name": "  ", "files": [CHAT_ONLY], "screens": {"chat": 0}})
    check("пустое имя: 400", r.status_code == 400)
    r = client.get("/api/skins/..%2F..%2Fetc")
    check("traversal в id: не 200", r.status_code in (404, 422))
    r = client.get("/api/skins/sk_missing")
    check("несуществующий скин: 404", r.status_code == 404)
    r = client.get("/api/skins/builtin-sylvan-grove")
    check("builtin на сервере не читается: 404", r.status_code == 404)

    section("4. Назначения")
    r = client.put("/api/personas/alex/skin", json={"skin_id": sid})
    check("назначить скин alex: 200", r.status_code == 200)
    r = client.put("/api/personas/mira/skin", json={"skin_id": "builtin-sylvan-grove"})
    check("назначить builtin mira: 200", r.status_code == 200)
    r = client.put("/api/personas/ghost/skin", json={"skin_id": sid})
    check("несуществующая персона: 404", r.status_code == 404)
    r = client.put("/api/personas/alex/skin", json={"skin_id": "sk_nope"})
    check("несуществующий скин: 404", r.status_code == 404)
    a = client.get("/api/skin-assignments").json()["assignments"]
    check("GET назначений", a == {"alex": sid, "mira": "builtin-sylvan-grove"})
    r = client.put("/api/personas/mira/skin", json={"skin_id": None})
    check("снять скин: 200", r.status_code == 200
          and client.get("/api/skin-assignments").json()["assignments"] == {"alex": sid})

    section("5. Удаление")
    r2 = client.post("/api/skins", json={"name": "Копия", "files": [CHAT_ONLY], "screens": {"chat": 0}})
    sid2 = r2.json()["skin"]["id"]
    r = client.delete(f"/api/skins/{sid}")
    check("DELETE: 200 и снят с alex", r.status_code == 200 and r.json()["unassigned"] == ["alex"])
    check("назначения пусты", client.get("/api/skin-assignments").json()["assignments"] == {})
    left = list((tmp / "skins" / "files").glob("*.html"))
    check("файл, общий со вторым скином, остался; остальные удалены", len(left) == 1)
    check("второй скин читается целиком",
          CHAT_ONLY in client.get(f"/api/skins/{sid2}").json()["files"].values())
    r = client.delete(f"/api/skins/{sid}")
    check("повторный DELETE: 404", r.status_code == 404)

    section("5c. Встроенный скин: удаление скрывает, восстановление возвращает")
    check("по умолчанию скрытых нет", client.get("/api/skins").json().get("hidden_builtins") == [])
    client.put("/api/personas/mira/skin", json={"skin_id": "builtin-sylvan-grove"})
    r = client.delete("/api/skins/builtin-sylvan-grove")
    check("DELETE builtin: 200 и снят с mira",
          r.status_code == 200 and r.json()["unassigned"] == ["mira"])
    check("builtin в hidden_builtins",
          client.get("/api/skins").json()["hidden_builtins"] == ["builtin-sylvan-grove"])
    check("назначение builtin снято",
          "mira" not in client.get("/api/skin-assignments").json()["assignments"])
    r = client.delete("/api/skins/builtin-sylvan-grove")
    check("повторное скрытие не дублирует",
          r.status_code == 200 and client.get("/api/skins").json()["hidden_builtins"] == ["builtin-sylvan-grove"])
    r = client.delete("/api/skins/builtin-Bad_ID")
    check("кривой builtin-id: 404", r.status_code in (404, 422))
    check("файл скрытых не принимается за скин",
          all(s["id"].startswith("sk_") for s in client.get("/api/skins").json()["skins"]))
    r = client.post("/api/skins/builtins/restore")
    check("restore: вернул скрытые", r.status_code == 200 and r.json()["restored"] == ["builtin-sylvan-grove"])
    check("после restore скрытых нет", client.get("/api/skins").json()["hidden_builtins"] == [])
    r = client.post("/api/skins/builtins/restore")
    check("повторный restore: пусто", r.status_code == 200 and r.json()["restored"] == [])

    section("5a. id «assignments» и прочие не-скины")
    client.put("/api/personas/alex/skin", json={"skin_id": sid2})
    assign_file = tmp / "skins" / "assignments.json"
    before = assign_file.read_text(encoding="utf-8")
    r = client.delete("/api/skins/assignments")
    check("DELETE /api/skins/assignments: 404", r.status_code == 404)
    check("файл назначений цел", assign_file.is_file()
          and assign_file.read_text(encoding="utf-8") == before)
    r = client.put("/api/skins/assignments", json={"name": "x"})
    check("PUT /api/skins/assignments: 404", r.status_code == 404)
    check("файл назначений не перезаписан", assign_file.read_text(encoding="utf-8") == before)
    r = client.get("/api/skins/assignments")
    check("GET /api/skins/assignments: 404", r.status_code == 404)
    r = client.put("/api/personas/mira/skin", json={"skin_id": "assignments"})
    check("назначить «assignments»: 404", r.status_code == 404)
    r = client.put("/api/personas/mira/skin", json={"skin_id": "builtin-Bad_Id"})
    check("builtin с недопустимым id: 400", r.status_code == 400)
    check("назначения не изменились",
          client.get("/api/skin-assignments").json()["assignments"] == {"alex": sid2})
    check("список скинов не видит assignments.json",
          [s["id"] for s in client.get("/api/skins").json()["skins"]] == [sid2])

    section("5b. Битый JSON скина не теряет HTML при сборке мусора")
    files_dir = tmp / "skins" / "files"
    r3 = client.post("/api/skins", json={"name": "Битый", "files": ["<html>keep-me</html>"],
                                         "screens": {"chat": 0}})
    sid3 = r3.json()["skin"]["id"]
    (tmp / "skins" / f"{sid3}.json").write_text("{not json", encoding="utf-8")
    client.delete(f"/api/skins/{sid2}")  # удаление запускает сборку мусора
    check("HTML скина с битым JSON не удалён сборкой мусора",
          any("keep-me" in p.read_text(encoding="utf-8") for p in files_dir.glob("*.html")))
    (tmp / "skins" / f"{sid3}.json").unlink()


def run_body_limit(client):
    section("7. Лимит тела запроса: 413 до разбора JSON")
    from app.api import skin_gen_api, skins_api
    check("лимиты: скины ≥ 3 файла по 3 МБ, генерация ≥ 2 файла по 3 МБ",
          skins_api.MAX_BODY_BYTES >= 3 * skins_api.MAX_FILE_BYTES
          and skin_gen_api.MAX_BODY_BYTES >= 2 * skin_gen_api.MAX_HTML_BYTES)
    over = str(skins_api.MAX_BODY_BYTES + 1)
    with mock.patch.object(skins_api, "create_skin") as create, \
            mock.patch.object(skins_api, "update_skin") as update, \
            mock.patch.object(skin_gen_api, "prepare") as prepare:
        # Content-Length больше лимита: отказ по заголовку, тело не читается
        r = client.post("/api/skins", content=b"{}",
                        headers={"content-type": "application/json", "content-length": over,
                                 "origin": "http://example.test"})
        check("POST /api/skins: Content-Length сверх лимита → 413",
              r.status_code == 413 and "МБ" in r.json().get("detail", ""))
        check("413 уходит с CORS-заголовком (middleware внутри CORS)",
              r.headers.get("access-control-allow-origin") is not None)
        r = client.put("/api/skins/sk_000000000000", content=b"{}",
                       headers={"content-type": "application/json", "content-length": over})
        check("PUT /api/skins/{id}: Content-Length сверх лимита → 413", r.status_code == 413)
        r = client.post("/api/skins/generate", content=b"{}", headers={
            "content-type": "application/json",
            "content-length": str(skin_gen_api.MAX_BODY_BYTES + 1)})
        check("POST /api/skins/generate: Content-Length сверх лимита → 413", r.status_code == 413)
        r = client.post("/api/skins", content=b"{}",
                        headers={"content-type": "application/json", "content-length": "abc"})
        check("кривой Content-Length → 400", r.status_code == 400)

        # Тело без длины (chunked): читается с потолком, превышение — тот же 413
        def chunks(total, size=1024 * 1024):
            sent = 0
            while sent < total:
                n = min(size, total - sent)
                sent += n
                yield b" " * n

        r = client.post("/api/skins", content=chunks(skins_api.MAX_BODY_BYTES + 10),
                        headers={"content-type": "application/json"})
        check("chunked сверх лимита → 413", r.status_code == 413)
        check("до обработчиков дело не дошло",
              not create.called and not update.called and not prepare.called)
    # Уложившееся chunked-тело доходит до обработчика целиком
    r = client.post("/api/skins", content=iter([b'{"name": "chunked", "files": ["<html>c</html>"],',
                                                b' "screens": {"chat": 0}}']),
                    headers={"content-type": "application/json"})
    check("chunked в пределах лимита: скин создан", r.status_code == 200
          and r.json()["skin"]["name"] == "chunked")
    client.delete(f"/api/skins/{r.json()['skin']['id']}")
    r = client.get("/api/skins", headers={"content-length": over})
    check("другие методы/маршруты лимит не трогает", r.status_code == 200)


def run_body_limit_asgi():
    section("8. BodySizeLimit на уровне ASGI: тело по частям")
    import asyncio
    from app.api.security import BodySizeLimit

    seen = []

    async def inner(scope, receive, send):
        msg = await receive()
        seen.append(msg["body"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = BodySizeLimit(inner, [("POST", r"/x", 10)])

    def call(parts, method="POST", path="/x"):
        queue = [{"type": "http.request", "body": p, "more_body": i < len(parts) - 1}
                 for i, p in enumerate(parts)]
        out = []

        async def receive():
            return queue.pop(0) if queue else {"type": "http.disconnect"}

        async def send(message):
            out.append(message)

        asyncio.run(mw({"type": "http", "method": method, "path": path, "headers": []},
                       receive, send))
        return next((m["status"] for m in out if m["type"] == "http.response.start"), None)

    seen.clear()
    check("части в сумме ≤ лимита — склеены в одно тело",
          call([b"abc", b"def", b"gh"]) == 200 and seen == [b"abcdefgh"])
    seen.clear()
    check("части в сумме > лимита — 413, приложение не вызвано",
          call([b"abcdef", b"ghijk"]) == 413 and seen == [])
    seen.clear()
    check("чужой путь — без лимита", call([b"x" * 50], path="/y") == 200 and seen == [b"x" * 50])


def run_persona_hooks():
    section("6. Смена id и удаление персоны")
    from app.api import skins_api
    meta = skins_api.create_skin(skins_api.SkinCreate(
        name="hook", files=[CHAT_ONLY], screens={"chat": 0}))
    skins_api.assign_skin("alex", meta["id"])
    skins_api.rename_persona("alex", "alexander")
    check("смена id: назначение переехало",
          skins_api.get_assignments() == {"alexander": meta["id"]})
    skins_api.forget_persona("alexander")
    check("удаление персоны: назначение снято", skins_api.get_assignments() == {})
    check("сам скин остался в библиотеке",
          any(s["id"] == meta["id"] for s in skins_api.list_skins()))


if __name__ == "__main__":
    sys.exit(main())

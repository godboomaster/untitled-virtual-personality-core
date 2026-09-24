"""Smoke-тест защиты API от небезопасного ввода:

  - id персоны (и вообще «безопасный id») из тела/query/path не должен
    доходить до файловой системы как есть — единая точка проверки в
    app/api/security, применённая в runtime.get_persona_info (последний
    рубеж), pydantic-схемах и settings_api (конфиг/YAML персоны);
  - .env пишется атомарно, под локом, и не принимает значения, которые
    могли бы дописать в файл постороннюю строку (\\r/\\n/NUL);
  - запись YAML персоны переживает исключение посреди записи (не остаётся
    усечённой);
  - 1-2 эндпоинта целиком (через FastAPI TestClient, на временных путях —
    НАСТОЯЩИЕ .env и data/ не трогаются).

Запуск: PYTHONPATH=. python3 scripts/test_api_security.py
"""

import asyncio
import shutil
import sys
import tempfile
import threading
import time
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


# ════════════ A. app.api.security: is_safe_id / safe_join ════════════

def test_security_primitives():
    section("A. security.is_safe_id / safe_join")
    from app.api.security import is_safe_id, safe_join

    bad_ids = [
        "../x", "..%2f", "a/b", "/etc/passwd", "", "a" * 65,
        "a\x00b", "..", ".", "café", "a‥b", "。。", "a\nb", "a\rb",
        "../../persona_template", None, 123,
    ]
    for v in bad_ids:
        check(f"is_safe_id отклоняет {v!r}", not is_safe_id(v))

    good_ids = ["alex", "a_b-9", "A" * 64, "verso_ru_group", "x"]
    for v in good_ids:
        check(f"is_safe_id принимает {v!r}", is_safe_id(v))

    # safe_join: базовый traversal-сценарий — victim-файл ЛЕЖИТ там, куда
    # целится ".." (иначе resolve()-проверка ничего бы не решала: цель
    # просто не существовала бы, что не то же самое, что «отвергнут»)
    tmp = Path(tempfile.mkdtemp(prefix="safejoin_"))
    try:
        base = tmp / "personas"
        base.mkdir()
        (tmp / "victim.yaml").write_text("system_prompt: hacked\n", encoding="utf-8")
        check("safe_join: '../victim' → None (traversal)",
              safe_join(base, "../victim", ".yaml") is None)
        check("safe_join: 'a/b' → None",
              safe_join(base, "a/b", ".yaml") is None)
        p = safe_join(base, "alex", ".yaml")
        check("safe_join: валидное имя → путь внутри base",
              p is not None and p.parent.resolve() == base.resolve())
        # prefix (контекст data/api_{persona}) не должен красть символы из
        # лимита длины имени персоны
        long_name = "a" * 60  # + "api_" = 64, ровно на границе
        check("safe_join: длинное имя + prefix не отваливается зря",
              safe_join(base, long_name, prefix="api_") is not None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════ B. runtime.get_persona_info / list_personas (реальные персоны, read-only) ═══

def test_runtime_last_line_of_defense():
    section("B. runtime.get_persona_info — последний рубеж")
    from app.api import runtime

    real_personas = runtime.list_personas()
    check("list_personas: реальные персоны на диске найдены", len(real_personas) > 0)
    for name in real_personas:
        info = runtime.get_persona_info(name)
        check(f"get_persona_info: валидная персона {name!r} проходит",
              info is not None and info["id"] == name)

    traversal_names = [
        "../../persona_template",  # файл СУЩЕСТВУЕТ на диске
        "..", "../x", "a/b", "/etc/passwd", "", "a" * 100,
        "a\x00b", "café", "pip_the_sprite/../../persona_template",
    ]
    for name in traversal_names:
        info = runtime.get_persona_info(name)
        check(f"get_persona_info: {name!r} отклонено (None), не читает файл вне personas/",
              info is None)


# ════════════ C. pydantic-схемы: PersonaId/SafeId ════════════

def test_schemas():
    section("C. schemas.py — PersonaId в теле запроса")
    from pydantic import ValidationError

    from app.api.schemas import CalendarEntryCreate, ChatRequest, PersonaDraftSave

    try:
        ChatRequest(persona="../evil", message="hi")
        check("ChatRequest: persona с traversal → ValidationError", False)
    except ValidationError:
        check("ChatRequest: persona с traversal → ValidationError", True)

    req = ChatRequest(persona="alex", message="hi")
    check("ChatRequest: валидная persona принята", req.persona == "alex")

    try:
        CalendarEntryCreate(title="t", date="2026-01-01", persona="../evil")
        check("CalendarEntryCreate: persona с traversal → ValidationError", False)
    except ValidationError:
        check("CalendarEntryCreate: persona с traversal → ValidationError", True)

    entry = CalendarEntryCreate(title="t", date="2026-01-01")
    check("CalendarEntryCreate: persona=None (опционально) проходит", entry.persona is None)

    try:
        PersonaDraftSave(id="../evil")
        check("PersonaDraftSave: id с traversal → ValidationError", False)
    except ValidationError:
        check("PersonaDraftSave: id с traversal → ValidationError", True)


# ════════════ D. settings_api: конфиг/YAML персоны на временном каталоге ═══

def test_settings_api_traversal():
    section("D. settings_api — YAML персоны, временный _PERSONAS_DIR")
    from app.api import settings_api

    tmp = Path(tempfile.mkdtemp(prefix="settings_api_"))
    personas_dir = tmp / "personas"
    personas_dir.mkdir()
    victim = tmp / "victim.yaml"
    victim_original = "id: victim\nsystem_prompt: original\n"
    victim.write_text(victim_original, encoding="utf-8")
    good = personas_dir / "goodp.yaml"
    good.write_text("id: goodp\nsystem_prompt: hello\nsettings: {}\n", encoding="utf-8")

    orig_dir = settings_api._PERSONAS_DIR
    settings_api._PERSONAS_DIR = personas_dir
    try:
        traversal = "../victim"

        check("get_persona_config: traversal → None",
              settings_api.get_persona_config(traversal) is None)
        check("victim.yaml не тронут после get_persona_config",
              victim.read_text(encoding="utf-8") == victim_original)

        r = settings_api.update_persona_config(traversal, {"pwned": True}, None, None)
        check("update_persona_config: traversal → None (не читает/не пишет вне personas/)",
              r is None)
        check("victim.yaml не тронут после update_persona_config",
              victim.read_text(encoding="utf-8") == victim_original)

        r2 = settings_api.update_persona_proactive(traversal, {"enabled": True})
        check("update_persona_proactive: traversal → None", r2 is None)
        check("victim.yaml не тронут после update_persona_proactive",
              victim.read_text(encoding="utf-8") == victim_original)

        r3 = settings_api.save_persona_yaml(traversal, "id: x\nsystem_prompt: hacked\n")
        check("save_persona_yaml: traversal → None", r3 is None)
        check("victim.yaml не тронут после save_persona_yaml",
              victim.read_text(encoding="utf-8") == victim_original)

        r4 = settings_api.create_persona("id: ../victim\nsystem_prompt: x\n")
        check("create_persona: id с traversal отклонён", r4["ok"] is False)
        check("victim.yaml не тронут после create_persona",
              victim.read_text(encoding="utf-8") == victim_original)

        r5 = settings_api.duplicate_persona(traversal)
        check("duplicate_persona: traversal-источник → None", r5 is None)
        check("вне personas_dir новых файлов не появилось",
              sorted(p.name for p in tmp.iterdir()) == ["personas", "victim.yaml"])

        # ── позитив: валидное имя действительно работает ──
        ok_result = settings_api.update_persona_config("goodp", {"answered": True}, None, None)
        check("update_persona_config: валидное имя проходит", ok_result is not None)
        import yaml as _yaml
        data = _yaml.safe_load(good.read_text(encoding="utf-8"))
        check("update_persona_config: настройки реально записались",
              data.get("settings", {}).get("answered") is True)

        dup = settings_api.duplicate_persona("goodp")
        check("duplicate_persona: валидное имя создаёт копию внутри personas_dir",
              dup is not None and (personas_dir / f"{dup['persona']}.yaml").is_file())
    finally:
        settings_api._PERSONAS_DIR = orig_dir
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════ E. .env: round-trip, отказ на \n/NUL, атомарность, лок ════════════

def test_env_safety():
    section("E. security.persist_env/remove_env — .env")
    from app.api import security

    tmp = Path(tempfile.mkdtemp(prefix="envsafety_"))
    env_path = tmp / ".env"
    try:
        # ── отказ на переносы строк / NUL / плохое имя переменной ──
        try:
            security.persist_env(env_path, "GROQ_API_KEY", "sk-x\nAPI_TOKEN=evil")
            check("persist_env: значение с \\n отклонено", False)
        except ValueError:
            check("persist_env: значение с \\n отклонено", True)
        check(".env не создан отклонённой записью", not env_path.exists())

        try:
            security.persist_env(env_path, "X", "a\x00b")
            check("persist_env: значение с NUL отклонено", False)
        except ValueError:
            check("persist_env: значение с NUL отклонено", True)

        try:
            security.persist_env(env_path, "lower_case", "v")
            check("persist_env: имя переменной не из [A-Z0-9_] отклонено", False)
        except ValueError:
            check("persist_env: имя переменной не из [A-Z0-9_] отклонено", True)

        # ── round-trip значений, которые легко сломать наивной записью ──
        from dotenv import dotenv_values
        tricky = {
            "K_SPACE": "hello world",
            "K_HASH": "a#b",
            "K_DQUOTE": 'a"b',
            "K_SQUOTE": "a'b",
            "K_EQ": "a=b",
            "K_PAD": "  spaced  ",
        }
        for var, value in tricky.items():
            security.persist_env(env_path, var, value)
        values = dotenv_values(env_path)
        for var, value in tricky.items():
            check(f".env round-trip: {var}={value!r}", values.get(var) == value)

        # ── удаление ──
        security.remove_env(env_path, "K_SPACE")
        check("remove_env: переменная удалена",
              "K_SPACE" not in dotenv_values(env_path))
        check("remove_env: остальные переменные целы",
              dotenv_values(env_path).get("K_HASH") == "a#b")

        # ── конкурентная запись (лок сериализует, файл не бьётся) ──
        def _writer(i):
            security.persist_env(env_path, f"CONC_{i}", f"val{i}")

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        values = dotenv_values(env_path)
        check("конкурентная запись: все 20 переменных на месте",
              all(values.get(f"CONC_{i}") == f"val{i}" for i in range(20)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════ F. атомарность записи (обрыв посреди — файл не усечён) ════════════

def test_atomic_write():
    section("F. атомарность записи (симуляция обрыва)")
    from app.api import security

    tmp = Path(tempfile.mkdtemp(prefix="atomic_"))
    try:
        target = tmp / "persona.yaml"
        target.write_text("ORIGINAL-CONTENT", encoding="utf-8")
        with mock.patch("os.replace", side_effect=OSError("disk full (симуляция)")):
            try:
                security.atomic_write_text(target, "NEW-CONTENT-THAT-SHOULD-NOT-LAND")
                check("atomic_write_text: исключение при os.replace всплывает", False)
            except OSError:
                check("atomic_write_text: исключение при os.replace всплывает", True)
        check("atomic_write_text: файл не усечён/не заменён при обрыве",
              target.read_text(encoding="utf-8") == "ORIGINAL-CONTENT")

        # тот же сценарий для .env (dotenv.rewrite — тот же tmp+os.replace)
        env_path = tmp / ".env"
        security.persist_env(env_path, "FOO", "old-value")
        with mock.patch("os.replace", side_effect=OSError("disk full (симуляция)")):
            try:
                security.persist_env(env_path, "FOO", "new-value")
                check("persist_env: исключение при os.replace всплывает", False)
            except OSError:
                check("persist_env: исключение при os.replace всплывает", True)
        from dotenv import dotenv_values
        check(".env не повреждён/не усечён при обрыве записи",
              dotenv_values(env_path).get("FOO") == "old-value")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════ D2. settings_api: отказ на \n в ключе/модели (реальные вызовы) ════════════

def test_settings_api_env_rejection():
    section("D2. settings_api.add_provider_key / set_provider_model — отказ на \\n")
    from app.api import settings_api

    tmp = Path(tempfile.mkdtemp(prefix="settings_env_"))
    fake_env = tmp / ".env"
    orig_env_path = settings_api._ENV_PATH
    settings_api._ENV_PATH = fake_env
    try:
        res = settings_api.add_provider_key("groq", "sk-ok\nEVIL=1")
        check("add_provider_key: ключ с \\n отклонён (ok=False, не 500)", res["ok"] is False)
        check(".env не создан отклонённым ключом", not fake_env.exists())

        res2 = settings_api.set_provider_model("groq", "model\nEVIL=1")
        check("set_provider_model: имя модели с \\n отклонено (ok=False)", res2["ok"] is False)
        check(".env всё ещё не создан", not fake_env.exists())
    finally:
        settings_api._ENV_PATH = orig_env_path
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════ G. FastAPI TestClient — 1-2 эндпоинта целиком ════════════

def test_fastapi_endpoints():
    section("G. FastAPI TestClient")
    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return

    import app.api.server as server_mod

    orig_token = server_mod._api_token
    server_mod._api_token = ""  # детерминированно: не зависим от реального .env
    try:
        client = TestClient(server_mod.app)

        r = client.get("/api/health")
        check("GET /api/health: 200 без авторизации", r.status_code == 200)

        r = client.get("/api/personas")
        check("GET /api/personas: 200, читает реальные YAML (read-only)", r.status_code == 200)
        names = {p["id"] for p in r.json()}
        check("GET /api/personas: список не пуст", len(names) > 0)

        # Path-параметр: невалидное имя не долетает до settings_api вообще —
        # 422 от pydantic/FastAPI Path(pattern=...), эндпоинт не выполняется
        r = client.get("/api/personas/a.b/config")
        check("GET /api/personas/a.b/config: 422 (точка не в алфавите id)",
              r.status_code == 422)

        r = client.get(f"/api/personas/{'a' * 100}/config")
        check("GET /api/personas/<100 символов>/config: 422 (превышена длина)",
              r.status_code == 422)

        # Query-параметр: /api/chat/history?persona=
        r = client.get("/api/chat/history", params={"persona": "../evil"})
        check("GET /api/chat/history?persona=../evil: 422", r.status_code == 422)

        r = client.get("/api/chat/history", params={"persona": ""})
        check("GET /api/chat/history?persona=: 422 (пусто)", r.status_code == 422)

        # Валидный ФОРМАТ, но персоны с таким именем нет — уже не 422, а 404
        # (get_persona_config сходил на диск и не нашёл файл), не 500
        r = client.get("/api/personas/no_such_persona_xyz/config")
        check("GET /api/personas/no_such_persona_xyz/config: 404, не 500/422",
              r.status_code == 404)

        # ── авторизация: hmac.compare_digest вместо == ──
        server_mod._api_token = "s3cr3t-token"
        r = client.get("/api/personas")
        check("auth: без токена при заданном API_TOKEN → 401", r.status_code == 401)
        r = client.get("/api/personas", headers={"Authorization": "Bearer wrong"})
        check("auth: неверный токен → 401", r.status_code == 401)
        r = client.get("/api/personas", headers={"Authorization": "Bearer s3cr3t-token"})
        check("auth: верный токен → 200", r.status_code == 200)
    finally:
        server_mod._api_token = orig_token


# ════════════ H. chat_stream: «печать» не блокирует поток пула ════════════

def test_typed_chunks_nonblocking():
    section("H. server._typed_chunks — пейсинг через asyncio.sleep, не time.sleep")
    import app.api.server as server_mod

    orig_sleep = time.sleep

    def _boom(*a, **kw):
        raise AssertionError("time.sleep() вызван из «печати» — блокирует поток пула")

    async def _collect(text):
        return [ev async for ev in server_mod._typed_chunks(text)]

    time.sleep = _boom
    try:
        events = asyncio.run(_collect("привет мир!"))
        empty_events = asyncio.run(_collect(""))
    finally:
        time.sleep = orig_sleep
    check("_typed_chunks: ни разу не вызвал синхронный time.sleep", True)
    rebuilt = "".join(e["token"] for e in events)
    check("_typed_chunks: склеенные токены равны исходному тексту", rebuilt == "привет мир!")
    check("_typed_chunks: пустой текст → без событий", empty_events == [])


# ════════════ I. persona_yaml: sync read вынесен в поток ══════════════════

def test_persona_yaml_nonblocking():
    section("I. GET /api/personas/{persona}/yaml — не блокирует event loop")
    try:
        import fastapi  # noqa: F401
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi недоступен: {e})")
        return

    import app.api.server as server_mod
    from app.api.runtime import list_personas

    personas = list_personas()
    if not personas:
        print("  (пропущено — нет ни одной персоны)")
        return
    persona = personas[0]

    orig_token = server_mod._api_token
    server_mod._api_token = ""
    orig_read_text = Path.read_text
    entered = threading.Event()
    release = threading.Event()

    def _slow_read_text(self, *a, **kw):
        if self.name == f"{persona}.yaml":
            entered.set()
            release.wait(timeout=5)
        return orig_read_text(self, *a, **kw)

    try:
        client = TestClient(server_mod.app)
        with mock.patch.object(Path, "read_text", _slow_read_text):
            result = {}

            def _call_yaml():
                result["r"] = client.get(f"/api/personas/{persona}/yaml")

            t = threading.Thread(target=_call_yaml)
            t.start()
            got_entered = entered.wait(timeout=5)
            check("persona_yaml: запрос дошёл до чтения файла", got_entered)

            t0 = time.time()
            r_health = client.get("/api/health")
            dt = time.time() - t0
            check("persona_yaml: /api/health отвечает быстро, пока yaml «читается» "
                  "(event loop не заблокирован синхронным read_text)",
                  r_health.status_code == 200 and dt < 2.0)

            release.set()
            t.join(timeout=5)
        check("persona_yaml: сам запрос успешно завершился, пока файл вернулся",
              result.get("r") is not None and result["r"].status_code == 200)
    finally:
        server_mod._api_token = orig_token
        release.set()


def main():
    test_security_primitives()
    test_runtime_last_line_of_defense()
    test_schemas()
    test_settings_api_traversal()
    test_env_safety()
    test_atomic_write()
    test_settings_api_env_rejection()
    test_fastapi_endpoints()
    test_typed_chunks_nonblocking()
    test_persona_yaml_nonblocking()

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

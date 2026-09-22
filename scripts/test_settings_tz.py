"""Тест настройки часового пояса пользователя (веб-настройки → TIMEZONE, см.
app/core/timeutil и app/api/settings_api.get_timezone/set_timezone).

Проверяет бизнес-логику settings_api.get_timezone/set_timezone напрямую (как
секция D2/F в test_api_security.py — settings_api._ENV_PATH подменяется на
временный файл, реальный .env не трогается). HTTP-эндпоинты
GET/PUT /api/settings/timezone заводятся отдельно в app/api/server.py (вне
области этого агента) по образцу /api/settings/location — здесь фиксируется
контракт, который они должны выполнять, вызывая эти же функции:

  GET  -> settings_api.get_timezone()
  PUT  -> settings_api.set_timezone(req.timezone); result["ok"] is False -> 422

Запуск: PYTHONPATH=. python3 scripts/test_settings_tz.py
"""

import os
import shutil
import sys
import tempfile
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


def _use_temp_env():
    """Подменить settings_api._ENV_PATH на пустой временный файл; вернуть
    (fake_env, restore) — restore возвращает _ENV_PATH и окружение обратно."""
    from app.api import settings_api

    tmp = Path(tempfile.mkdtemp(prefix="settings_tz_"))
    fake_env = tmp / ".env"
    orig_env_path = settings_api._ENV_PATH
    orig_timezone = os.environ.get("TIMEZONE")
    orig_tz = os.environ.get("TZ")
    settings_api._ENV_PATH = fake_env
    os.environ.pop("TIMEZONE", None)
    os.environ.pop("TZ", None)

    def restore():
        settings_api._ENV_PATH = orig_env_path
        if orig_timezone is None:
            os.environ.pop("TIMEZONE", None)
        else:
            os.environ["TIMEZONE"] = orig_timezone
        if orig_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = orig_tz
        shutil.rmtree(tmp, ignore_errors=True)

    return fake_env, restore


def test_get_default():
    section("A. get_timezone() без TIMEZONE — системный пояс")
    from app.api import settings_api
    from app.core import timeutil

    fake_env, restore = _use_temp_env()
    try:
        timeutil._cache = ("", None)  # чистим кеш имени между тестами
        res = settings_api.get_timezone()
        check("get_timezone: timezone == '' (не задан)", res["timezone"] == "")
        check("get_timezone: source == 'system'", res["source"] == "system")
        check("get_timezone: effective не пуст", bool(res.get("effective")))
    finally:
        restore()


def test_put_valid_roundtrip():
    section("B. set_timezone(валидное) — .env + os.environ + timeutil сразу")
    from app.api import settings_api
    from app.core import timeutil
    from dotenv import dotenv_values

    fake_env, restore = _use_temp_env()
    try:
        timeutil._cache = ("", None)
        # Два пояса с заведомо разным смещением — годится любой, если он
        # отличается от системного в момент теста
        tz_name = "Asia/Tokyo"
        res = settings_api.set_timezone(tz_name)
        check("set_timezone(валидное): ok=True", res.get("ok") is True)
        check("set_timezone(валидное): timezone в ответе", res.get("timezone") == tz_name)
        check("set_timezone(валидное): source == 'env'", res.get("source") == "env")
        check("set_timezone(валидное): effective == имя", res.get("effective") == tz_name)

        check(".env содержит TIMEZONE=", dotenv_values(fake_env).get("TIMEZONE") == tz_name)
        check("os.environ обновлён сразу (для живого процесса)",
              os.environ.get("TIMEZONE") == tz_name)
        check("timeutil.tz_name() сразу отдаёт новое значение",
              timeutil.tz_name() == tz_name)
        check("timeutil.tz() резолвится в валидный пояс", timeutil.tz() is not None)

        # Повторный GET видит то же самое, что вернул PUT
        res2 = settings_api.get_timezone()
        check("get_timezone() после PUT совпадает", res2 == {
            "timezone": tz_name, "effective": tz_name, "source": "env",
        })
    finally:
        restore()


def test_put_invalid_rejected():
    section("C. set_timezone(невалидное) — .env не меняется, ok=False")
    from app.api import settings_api
    from app.core import timeutil
    from dotenv import dotenv_values

    fake_env, restore = _use_temp_env()
    try:
        timeutil._cache = ("", None)
        for bad in ("Not/A_Zone", "../etc/passwd", "москва", "UTC+99", "a\nEVIL=1"):
            res = settings_api.set_timezone(bad)
            check(f"set_timezone({bad!r}): ok=False", res.get("ok") is False)
            check(f"set_timezone({bad!r}): есть detail", bool(res.get("detail")))
        check(".env не создан невалидным значением", not fake_env.exists())
        check("os.environ не тронут", "TIMEZONE" not in os.environ)
        check("timeutil.tz_name() по-прежнему пуст", timeutil.tz_name() == "")
    finally:
        restore()


def test_put_empty_resets():
    section("D. set_timezone('') — сброс на системный (ключ удалён)")
    from app.api import settings_api
    from app.core import timeutil
    from dotenv import dotenv_values

    fake_env, restore = _use_temp_env()
    try:
        timeutil._cache = ("", None)
        settings_api.set_timezone("Europe/Moscow")
        check("подготовка: TIMEZONE выставлен", dotenv_values(fake_env).get("TIMEZONE") == "Europe/Moscow")

        res = settings_api.set_timezone("")
        check("set_timezone(''): ok=True", res.get("ok") is True)
        check("set_timezone(''): source == 'system'", res.get("source") == "system")
        check("set_timezone(''): timezone == ''", res.get("timezone") == "")

        values = dotenv_values(fake_env)
        check("TIMEZONE удалён из .env", "TIMEZONE" not in values or not values.get("TIMEZONE"))
        check("TIMEZONE удалён из os.environ", "TIMEZONE" not in os.environ)
        check("timeutil.tz() снова None (системный)", timeutil.tz() is None)
    finally:
        restore()


def test_http_endpoints():
    section("E. HTTP: GET/PUT /api/settings/timezone (TestClient)")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi недоступен: {e})")
        return
    import app.api.server as server_mod
    from app.core import timeutil
    from dotenv import dotenv_values

    fake_env, restore = _use_temp_env()
    orig_token = server_mod._api_token
    server_mod._api_token = ""
    try:
        timeutil._cache = ("", None)
        client = TestClient(server_mod.app)
        r = client.get("/api/settings/timezone")
        check("GET: 200 и поля timezone/effective/source",
              r.status_code == 200 and {"timezone", "effective", "source"} <= set(r.json()))
        r = client.put("/api/settings/timezone", json={"timezone": "Asia/Tokyo"})
        check("PUT валидного: 200, source=env, .env обновлён",
              r.status_code == 200 and r.json().get("source") == "env"
              and dotenv_values(fake_env).get("TIMEZONE") == "Asia/Tokyo")
        r = client.put("/api/settings/timezone", json={"timezone": "Not/A_Zone"})
        check("PUT невалидного: 422, .env не изменился",
              r.status_code == 422 and dotenv_values(fake_env).get("TIMEZONE") == "Asia/Tokyo")
        r = client.put("/api/settings/timezone", json={"timezone": ""})
        check("PUT пустого: 200, source=system, ключ удалён",
              r.status_code == 200 and r.json().get("source") == "system"
              and not dotenv_values(fake_env).get("TIMEZONE"))
    finally:
        server_mod._api_token = orig_token
        restore()


def main():
    test_get_default()
    test_put_valid_roundtrip()
    test_put_invalid_rejected()
    test_put_empty_resets()
    test_http_endpoints()

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

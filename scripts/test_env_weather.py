"""Тест погоды окружения: Open-Meteo с запасным MET Norway (app/features/env_context.py).

Проверяет (сеть не трогает — httpx.Client подменён MockTransport):
* символы MET Norway → WMO-коды (суффиксы _day/_night, все «…andthunder»,
  опечатка API «lightssnowshowers…»), у каждого кода есть описание;
* Open-Meteo не подключился → строка погоды из MET Norway (ощущается, ветер
  м/с, координаты до 4 знаков, User-Agent с контактом); полчаса Open-Meteo не
  трогаем, потом снова он; 502 — тоже повод уйти в MET Norway;
* у Open-Meteo просим ветер в м/с (по умолчанию он отдаёт км/ч);
* оба недоступны → get_env_line отдаёт только время, fetch_forecast бросает;
* прогноз из MET Norway: UTC → часы пользователя, горизонт, коды, дождь со
  снегом считается осадками.

Запуск: PYTHONPATH=. python3 scripts/test_env_weather.py
"""

import logging
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

# Своя папка данных — чтобы случайно не прочитать настоящий env_location.json
os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="env_weather_")

import httpx  # noqa: E402

CFG = {"mode": "geo", "city": "Новосибирск",
       "lat": 55.01255898989714, "lon": 82.93703820509}


def _metno_payload():
    # Ряд как у api.met.no/…/complete: часы в UTC, первая точка — текущий час
    def point(time, temp, feels, wind, symbol):
        return {"time": time, "data": {
            "instant": {"details": {"air_temperature": temp,
                                    "apparent_air_temperature": feels,
                                    "wind_speed": wind}},
            "next_1_hours": {"summary": {"symbol_code": symbol},
                             "details": {"precipitation_amount": 0.0}},
        }}
    series = [
        point("2026-10-27T19:00:00Z", 3.4, -1.1, 5.9, "cloudy"),
        point("2026-10-27T20:00:00Z", 3.5, -1.0, 5.9, "cloudy"),
        point("2026-10-27T21:00:00Z", 2.0, -2.0, 6.5, "lightsleet"),
        point("2026-10-28T07:00:00Z", 0.5, -4.0, 7.0, "snow"),
        point("2026-10-28T08:00:00Z", 0.0, -4.5, 7.0, "snow"),  # за горизонтом 12 ч
    ]
    return {"type": "Feature", "properties": {"timeseries": series}}


def main():
    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    from app.core import timeutil
    from app.features import env_context as ec

    for var in timeutil.ENV_VARS:
        os.environ.pop(var, None)
    os.environ["TIMEZONE"] = "Asia/Novosibirsk"  # UTC+7, без DST

    warnings = []

    class _Catch(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                warnings.append(record.getMessage())

    ec.logger.addHandler(_Catch())

    # ── 1. Символы MET Norway → WMO ──
    cases = {
        "clearsky_day": 0, "fair_night": 1, "partlycloudy_polartwilight": 2,
        "cloudy": 3, "fog": 45, "lightrain": 61, "heavyrain": 65,
        "rainshowers_day": 81, "lightsleet": 68, "sleetshowers_night": 84,
        "snow": 73, "heavysnowshowers_day": 86,
        "rainandthunder": 95, "heavysleetshowersandthunder_night": 95,
        "lightssnowshowersandthunder_day": 95,  # опечатка в самом API
    }
    wrong = {s: ec._metno_code(s) for s, code in cases.items() if ec._metno_code(s) != code}
    check(f"metno: символы → WMO ({len(cases)} шт.){' ' + str(wrong) if wrong else ''}",
          not wrong)
    check("metno: неизвестный/пустой символ → None",
          ec._metno_code("sunnywithunicorns") is None and ec._metno_code(None) is None
          and ec._metno_code("") is None)
    check("metno: у каждого кода перевода есть описание",
          all(code in ec._WMO_DESC for code in ec._METNO_WMO.values()))
    precip_bases = [b for b in ec._METNO_WMO if any(w in b for w in ("rain", "sleet", "snow"))]
    check("metno: дождь/снег/дождь со снегом → коды осадков",
          all(ec.is_precip_code(ec._METNO_WMO[b]) for b in precip_bases))

    # ── Подмена сети ──
    calls = []
    mode = {"open_meteo": "timeout", "metno": "ok"}

    def handler(request: httpx.Request):
        calls.append(request)
        host = request.url.host
        if host == "api.open-meteo.com":
            if mode["open_meteo"] == "timeout":
                raise httpx.ConnectTimeout("timed out", request=request)
            if mode["open_meteo"] == "502":
                return httpx.Response(502, request=request)
            return httpx.Response(200, request=request, json={"current": {
                "time": "2026-10-28T02:00", "temperature_2m": 10.0,
                "apparent_temperature": 9.0, "weather_code": 0, "wind_speed_10m": 2.0}})
        if host == "api.met.no":
            if mode["metno"] == "timeout":
                raise httpx.ConnectTimeout("timed out", request=request)
            return httpx.Response(200, request=request, json=_metno_payload())
        raise AssertionError(f"неожиданный запрос в сеть: {request.url}")

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    def hosts():
        return [r.url.host for r in calls]

    ec.load_location = lambda: dict(CFG)

    def fresh_line():
        ec._cache.update(key=None, ts=0.0, line=None)
        calls.clear()
        return ec.get_env_line() or ""

    with mock.patch.object(httpx, "Client", fake_client):
        # ── 2. Open-Meteo не подключился → MET Norway ──
        ec._open_meteo_down_until = 0.0
        line = fresh_line()
        check(f"fallback: строка из MET Norway ({line!r})",
              line.startswith("Новосибирск: +3°C (feels like -1°C), overcast, wind 6 m/s | "))
        check("fallback: сначала Open-Meteo, потом MET Norway",
              hosts() == ["api.open-meteo.com", "api.met.no"])
        metno_req = calls[-1] if calls else None
        check("fallback: координаты до 4 знаков",
              metno_req is not None and metno_req.url.params.get("lat") == "55.0126"
              and metno_req.url.params.get("lon") == "82.937")
        ua = metno_req.headers.get("user-agent", "") if metno_req else ""
        check("fallback: User-Agent с названием и контактом",
              "virtual-persona-core" in ua and "github.com/" in ua)
        check("fallback: одно предупреждение о переключении",
              sum("Open-Meteo недоступен" in w for w in warnings) == 1
              and not any("Погода недоступна" in w for w in warnings))

        # ── 3. Полчаса Open-Meteo не трогаем ──
        line = fresh_line()
        check("sticky: повторно — сразу MET Norway, без ожидания Open-Meteo",
              hosts() == ["api.met.no"] and "overcast" in line)
        check("sticky: повторного предупреждения нет",
              sum("Open-Meteo недоступен" in w for w in warnings) == 1)

        # ── 4. Окно прошло, Open-Meteo ожил → снова он, ветер в м/с ──
        ec._open_meteo_down_until = 0.0
        mode["open_meteo"] = "ok"
        line = fresh_line()
        check(f"primary: Open-Meteo снова основной ({line!r})",
              hosts() == ["api.open-meteo.com"] and line.startswith("Новосибирск: +10°C, clear | "))
        om_req = calls[0] if calls else None
        check("primary: ветер просим в м/с",
              om_req is not None and om_req.url.params.get("wind_speed_unit") == "ms")

        # ── 5. 502 от Open-Meteo — тоже в MET Norway ──
        ec._open_meteo_down_until = 0.0
        mode["open_meteo"] = "502"
        line = fresh_line()
        check("502: строка из MET Norway",
              hosts() == ["api.open-meteo.com", "api.met.no"] and "overcast" in line)

        # ── 6. Оба недоступны ──
        ec._open_meteo_down_until = 0.0
        mode["open_meteo"] = "timeout"
        mode["metno"] = "timeout"
        warnings.clear()
        line = fresh_line()
        check(f"оба лежат: get_env_line — только время ({line!r})",
              "|" not in line and "°C" not in line and line.count(",") == 2)
        check("оба лежат: в логе «Погода недоступна»",
              any("Погода недоступна" in w for w in warnings))
        try:
            ec.fetch_forecast(CFG)
            raised = False
        except httpx.HTTPError:
            raised = True
        check("оба лежат: fetch_forecast бросает (rhythm пишет «Прогноз недоступен»)", raised)

        # ── 7. Прогноз из MET Norway ──
        mode["metno"] = "ok"
        ec._open_meteo_down_until = 0.0
        mode["open_meteo"] = "timeout"
        calls.clear()
        fc = ec.fetch_forecast(CFG, hours=12) or {}
        hours = fc.get("hours") or []
        check("forecast: текущие код и температура — из первой точки",
              fc.get("current_code") == 3 and fc.get("current_temp") == 3.4)
        check("forecast: UTC → часы пользователя (19:00Z → 02:00 по Новосибирску)",
              bool(hours) and hours[0]["time"] == datetime(2026, 10, 28, 3, 0))
        check("forecast: горизонт 12 ч (08:00Z уже за ним)",
              [h["time"].hour for h in hours] == [3, 4, 14])
        check("forecast: дождь со снегом через 2 ч — осадки, с описанием",
              len(hours) > 1 and hours[1]["code"] == 68 and ec.is_precip_code(hours[1]["code"])
              and ec._WMO_DESC.get(hours[1]["code"]) == "light sleet")
        check("forecast: вероятности осадков нет → None",
              all(h["precip_prob"] is None for h in hours))

    ec._open_meteo_down_until = 0.0
    os.environ.pop("TIMEZONE", None)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

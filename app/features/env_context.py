"""Окружение пользователя (местоположение + погода) для контекста ответов.

Конфиг — data/env_location.json:
  {"mode": "off"} — выключено (по умолчанию)
  {"mode": "manual" | "geo", "city": str, "lat": float, "lon": float}

Погода — Open-Meteo (бесплатно, без ключа); если он не отвечает (часть
провайдеров РФ режет его адрес на Hetzner) — MET Norway (api.met.no, тоже без
ключа), его символы погоды переводятся в WMO-коды. Геокодинг города — Open-Meteo
Geocoding, обратный геокодинг (geo-режим) — Nominatim (OSM). Строка окружения
кешируется на 30 минут: get_env_line() вызывается на каждое сообщение.
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from app.core import timeutil
from app.core.atomic_io import atomic_write_text, load_json_safe
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

def _config_path() -> Path:
    # Общая папка данных (VPC_DATA_DIR), а не data/ рядом с кодом
    return data_dir() / "env_location.json"
_CACHE_TTL = 30 * 60  # погода обновляется не чаще раза в полчаса
_cache: dict = {"key": None, "ts": 0.0, "line": None}
# Файл общий на процесс (не per-context) — сериализуем чтение/запись, иначе
# одновременные set_manual_city/set_geo/set_off (веб-настройки) чередуют
# запись и теряют друг друга.
_LOCATION_LOCK = threading.Lock()

_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
# Open-Meteo обычно подключается за доли секунды; не ждём его 5 с, когда есть
# запасной источник — строка погоды собирается прямо перед ответом персоны
_OPEN_METEO_TIMEOUT = httpx.Timeout(8.0, connect=2.0)
# После сбоя сети Open-Meteo пропускаем на полчаса и сразу идём в MET Norway
_OPEN_METEO_RETRY = 30 * 60
_open_meteo_down_until = 0.0

# MET Norway требует User-Agent с названием приложения и контактом, а
# координаты — не точнее 4 знаков (иначе 403)
_METNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
_METNO_HEADERS = {
    "User-Agent": "virtual-persona-core/1.0 "
                  "github.com/godboomaster/untitled-virtual-personality-core",
}

# WMO Weather interpretation codes → short English description
_WMO_DESC = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    # 68/69/83/84 — дождь со снегом (WMO 4677); Open-Meteo их не отдаёт,
    # приходят из перевода символов MET Norway
    68: "light sleet", 69: "sleet",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    83: "light sleet showers", 84: "sleet showers",
    85: "light snowfall", 86: "snowfall",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}

# Символ MET Norway (без суффикса _day/_night/_polartwilight) → WMO-код.
# Все варианты «…andthunder» — гроза (95), см. _metno_code
_METNO_WMO = {
    "clearsky": 0, "fair": 1, "partlycloudy": 2, "cloudy": 3, "fog": 45,
    "lightrain": 61, "rain": 63, "heavyrain": 65,
    "lightrainshowers": 80, "rainshowers": 81, "heavyrainshowers": 82,
    "lightsleet": 68, "sleet": 69, "heavysleet": 69,
    "lightsleetshowers": 83, "sleetshowers": 84, "heavysleetshowers": 84,
    "lightsnow": 71, "snow": 73, "heavysnow": 75,
    "lightsnowshowers": 85, "snowshowers": 85, "heavysnowshowers": 86,
}

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def load_location() -> dict:
    # Текущий конфиг местоположения. По умолчанию — выключено.
    with _LOCATION_LOCK:
        data = load_json_safe(_config_path(), default={"mode": "off"}, label="Env")
    return data if isinstance(data, dict) else {"mode": "off"}


def save_location(cfg: dict) -> dict:
    with _LOCATION_LOCK:
        atomic_write_text(_config_path(), json.dumps(cfg, ensure_ascii=False, indent=2))
    _cache.update(key=None, ts=0.0, line=None)  # сброс кеша — конфиг изменился
    logger.info(f"[Env] Местоположение сохранено: {cfg}")
    return cfg


def set_off() -> dict:
    return save_location({"mode": "off"})


def set_manual_city(city: str) -> dict | None:
    # Режим 'manual': город → координаты через Open-Meteo Geocoding.
    city = (city or "").strip()
    if not city:
        return None
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "ru", "format": "json"},
            )
            r.raise_for_status()
            results = (r.json() or {}).get("results") or []
            if not results:
                return None
            top = results[0]
            name = top.get("name") or city
            country = top.get("country")
            label = f"{name}, {country}" if country else name
            return save_location({
                "mode": "manual", "city": label,
                "lat": float(top["latitude"]), "lon": float(top["longitude"]),
            })
    except Exception as e:
        logger.warning(f"[Env] Геокодинг города '{city}' не удался: {e}")
        return None


def set_geo(lat: float, lon: float) -> dict | None:
    # Режим 'geo': координаты от браузера → название места через Nominatim.
    city = f"{lat:.4f},{lon:.4f}"  # fallback, если обратный геокодинг недоступен
    try:
        with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": "virtual-persona-core/1.0"}) as client:
            r = client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "accept-language": "ru"},
            )
            r.raise_for_status()
            addr = (r.json() or {}).get("address") or {}
            place = (
                addr.get("city") or addr.get("town") or addr.get("village")
                or addr.get("municipality") or addr.get("county")
            )
            if place:
                city = place
    except Exception as e:
        logger.warning(f"[Env] Обратный геокодинг {lat},{lon} не удался: {e}")
    return save_location({"mode": "geo", "city": city, "lat": float(lat), "lon": float(lon)})


def _from_open_meteo_or_metno(open_meteo, metno, what: str):
    """open_meteo(), а при сбое сети или ответа — metno(). После сбоя Open-Meteo
    полчаса не трогаем; ошибка MET Norway уходит вызывающему."""
    global _open_meteo_down_until
    if time.time() >= _open_meteo_down_until:
        try:
            result = open_meteo()
        except (httpx.HTTPError, ValueError) as e:
            _open_meteo_down_until = time.time() + _OPEN_METEO_RETRY
            logger.warning(f"[Env] Open-Meteo недоступен ({str(e) or type(e).__name__}) — "
                           f"{what} берём у MET Norway, Open-Meteo снова через "
                           f"{_OPEN_METEO_RETRY // 60} мин")
        else:
            if result is not None:
                return result
    return metno()


def _metno_code(symbol) -> int | None:
    # Символ MET Norway ("lightrain", "partlycloudy_night", …) → WMO-код
    if not isinstance(symbol, str) or not symbol:
        return None
    base = symbol.split("_", 1)[0]
    if "thunder" in base:
        return 95
    return _METNO_WMO.get(base)


def _metno_points(cfg: dict) -> list[dict]:
    """Ряд MET Norway: [{"time", "temp", "feels", "wind", "code", "prob"}],
    первая точка — текущий час. time — стенные часы пользователя (naive, как
    timeutil.now(); API отдаёт UTC), wind — м/с, code — WMO ближайшего часа,
    prob — вероятность осадков (обычно есть только в Скандинавии)."""
    with httpx.Client(timeout=_TIMEOUT, headers=_METNO_HEADERS) as client:
        r = client.get(_METNO_URL, params={
            "lat": round(float(cfg["lat"]), 4), "lon": round(float(cfg["lon"]), 4),
        })
        r.raise_for_status()
        series = ((r.json() or {}).get("properties") or {}).get("timeseries") or []
    points = []
    for entry in series:
        if not isinstance(entry, dict):
            continue
        try:
            moment = datetime.fromisoformat(str(entry.get("time")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            continue
        data = entry.get("data") or {}
        inst = (data.get("instant") or {}).get("details") or {}
        next_hour = data.get("next_1_hours") or {}
        points.append({
            "time": timeutil.from_ts(moment.timestamp()),
            "temp": inst.get("air_temperature"),
            "feels": inst.get("apparent_air_temperature"),
            "wind": inst.get("wind_speed"),
            "code": _metno_code((next_hour.get("summary") or {}).get("symbol_code")),
            "prob": (next_hour.get("details") or {}).get("probability_of_precipitation"),
        })
    return points


def _current_open_meteo(cfg: dict) -> dict | None:
    with httpx.Client(timeout=_OPEN_METEO_TIMEOUT) as client:
        r = client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": cfg["lat"], "longitude": cfg["lon"],
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "wind_speed_unit": "ms",  # по умолчанию км/ч
                "timezone": "auto",
            },
        )
        r.raise_for_status()
        cur = (r.json() or {}).get("current") or {}
    if not cur:
        return None
    return {
        "temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
        "code": cur.get("weather_code"), "wind": cur.get("wind_speed_10m"),
    }


def _current_metno(cfg: dict) -> dict | None:
    points = _metno_points(cfg)
    return points[0] if points else None


def _fetch_weather_line(cfg: dict) -> str | None:
    """Строка 'Город: +7°C (ощущается +5°C), дождь, ветер 4 м/с' (без времени —
    время/дата добавляются отдельно в get_env_line)."""
    cur = _from_open_meteo_or_metno(
        lambda: _current_open_meteo(cfg), lambda: _current_metno(cfg), "погоду")
    if not cur:
        return None

    parts = []
    temp = cur.get("temp")
    if temp is not None:
        t_str = f"{temp:+.0f}°C"
        feels = cur.get("feels")
        if feels is not None and abs(feels - temp) >= 2:
            t_str += f" (feels like {feels:+.0f}°C)"
        parts.append(t_str)
    weather = _WMO_DESC.get(cur.get("code"))
    if weather:
        parts.append(weather)
    wind = cur.get("wind")
    if wind is not None and wind >= 3:  # м/с; слабый ветер не упоминаем
        parts.append(f"wind {wind:.0f} m/s")
    if not parts:
        return None
    return f"{cfg.get('city', '?')}: {', '.join(parts)}"


# WMO-коды осадков (морось/дождь/ливни/снег) — для погодных предупреждений rhythm
_PRECIP_CODES = {
    51, 53, 55, 56, 57,                      # морось
    61, 63, 65, 66, 67,                      # дождь
    68, 69, 83, 84,                          # дождь со снегом
    71, 73, 75, 77,                          # снег
    80, 81, 82, 85, 86,                      # ливни
}


def is_precip_code(code) -> bool:
    # Является ли WMO-код осадками (дождь/снег/морось/ливни).
    try:
        return int(code) in _PRECIP_CODES
    except (TypeError, ValueError):
        return False


def fetch_forecast(cfg: dict, hours: int = 12) -> dict | None:
    """Прогноз на ближайшие часы — для погодных предупреждений (rhythm_manager):
      {"current_code": int|None, "current_temp": float|None,
       "hours": [{"time": datetime, "code": int|None,
                  "temp": float|None, "precip_prob": int|None}]}
    Времена — локальные naive datetime (Open-Meteo с timezone=auto; запуск
    локальный, часовой пояс машины совпадает с местом пользователя; у MET
    Norway — UTC, переведённые в пояс пользователя).
    None — прогноз недоступен (ответ без почасовых данных); сбой сети у обоих
    источников — исключение."""
    return _from_open_meteo_or_metno(
        lambda: _forecast_open_meteo(cfg, hours), lambda: _forecast_metno(cfg, hours),
        "прогноз")


def _forecast_metno(cfg: dict, hours: int) -> dict | None:
    points = _metno_points(cfg)
    if not points:
        return None
    cur = points[0]
    horizon = cur["time"] + timedelta(hours=hours)
    out_hours = [
        {"time": p["time"], "code": p["code"], "temp": p["temp"], "precip_prob": p["prob"]}
        for p in points[1:] if cur["time"] < p["time"] <= horizon
    ]
    if not out_hours:
        return None
    return {"current_code": cur["code"], "current_temp": cur["temp"], "hours": out_hours}


def _forecast_open_meteo(cfg: dict, hours: int) -> dict | None:
    with httpx.Client(timeout=_OPEN_METEO_TIMEOUT) as client:
        r = client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": cfg["lat"], "longitude": cfg["lon"],
                "current": "temperature_2m,weather_code",
                "hourly": "temperature_2m,weather_code,precipitation_probability",
                "forecast_days": 2,
                "timezone": "auto",
            },
        )
        r.raise_for_status()
        data = r.json() or {}
    cur = data.get("current") or {}
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return None
    try:
        cur_time = datetime.fromisoformat(cur.get("time"))
    except (TypeError, ValueError):
        return None

    def _at(arr, i):
        return arr[i] if isinstance(arr, list) and i < len(arr) else None

    horizon = cur_time + timedelta(hours=hours)
    out_hours = []
    for i, ts in enumerate(times):
        try:
            t = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            continue
        if cur_time < t <= horizon:
            out_hours.append({
                "time": t,
                "code": _at(hourly.get("weather_code"), i),
                "temp": _at(hourly.get("temperature_2m"), i),
                "precip_prob": _at(hourly.get("precipitation_probability"), i),
            })
    if not out_hours:
        return None
    return {
        "current_code": cur.get("weather_code"),
        "current_temp": cur.get("temperature_2m"),
        "hours": out_hours,
    }


def get_env_line() -> str | None:
    """Строка окружения для системного промпта.

    Местоположение выключено — всё равно отдаём текущие дату/время пользователя
    (app.core.timeutil: пояс TIMEZONE, без него — системный локальный): без
    этого персоны не знают, который час, и отвечают «не знаю».
    С местоположением — добавляется погода (сеть не чаще раза в 30 минут).
    """
    cfg = load_location()
    # Дата/время пользователя (пояс TIMEZONE, app.core.timeutil), а не
    # системный пояс процесса: эта строка идёт в промпт как «который час»
    now = timeutil.now()
    time_line = f"{_WEEKDAYS[now.weekday()]}, {now:%d.%m.%Y, %H:%M}"
    if cfg.get("mode") not in ("manual", "geo") or "lat" not in cfg:
        return time_line
    key = (cfg.get("mode"), cfg.get("city"), cfg.get("lat"), cfg.get("lon"))
    if _cache["key"] == key and time.time() - _cache["ts"] < _CACHE_TTL:
        weather = _cache["line"]
        return f"{weather} | {time_line}" if weather else time_line
    try:
        line = _fetch_weather_line(cfg)
    except Exception as e:
        logger.warning(f"[Env] Погода недоступна: {e}")
        line = None
    # Кешируем даже неудачу, но с коротким сроком — повторная попытка через 2 минуты
    _cache.update(key=key, ts=time.time() if line else time.time() - (_CACHE_TTL - 120), line=line)
    if line:
        logger.info(f"[Env] Окружение: {line}")
    return f"{line} | {time_line}" if line else time_line

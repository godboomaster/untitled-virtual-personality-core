"""Окружение пользователя (местоположение + погода) для контекста ответов.

Конфиг — data/env_location.json:
  {"mode": "off"} — выключено (по умолчанию)
  {"mode": "manual" | "geo", "city": str, "lat": float, "lon": float}

Погода — Open-Meteo (бесплатно, без ключа), геокодинг города — Open-Meteo
Geocoding, обратный геокодинг (geo-режим) — Nominatim (OSM). Строка окружения
кешируется на 30 минут: get_env_line() вызывается на каждое сообщение.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent.parent / "data" / "env_location.json"
_CACHE_TTL = 30 * 60  # погода обновляется не чаще раза в полчаса
_cache: dict = {"key": None, "ts": 0.0, "line": None}

_TIMEOUT = httpx.Timeout(8.0, connect=5.0)

# WMO Weather interpretation codes → short English description
_WMO_DESC = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    85: "light snowfall", 86: "snowfall",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def load_location() -> dict:
    """Текущий конфиг местоположения. По умолчанию — выключено."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"mode": "off"}


def save_location(cfg: dict) -> dict:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    _cache.update(key=None, ts=0.0, line=None)  # сброс кеша — конфиг изменился
    logger.info(f"[Env] Местоположение сохранено: {cfg}")
    return cfg


def set_off() -> dict:
    return save_location({"mode": "off"})


def set_manual_city(city: str) -> dict | None:
    """Режим 'manual': город → координаты через Open-Meteo Geocoding."""
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
    """Режим 'geo': координаты от браузера → название места через Nominatim."""
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


def _fetch_weather_line(cfg: dict) -> str | None:
    """Строка 'Город: +7°C (ощущается +5°C), дождь, ветер 4 м/с' (без времени —
    время/дата добавляются отдельно в get_env_line)."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        r = client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": cfg["lat"], "longitude": cfg["lon"],
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "timezone": "auto",
            },
        )
        r.raise_for_status()
        cur = (r.json() or {}).get("current") or {}
    if not cur:
        return None

    parts = []
    temp = cur.get("temperature_2m")
    if temp is not None:
        t_str = f"{temp:+.0f}°C"
        app = cur.get("apparent_temperature")
        if app is not None and abs(app - temp) >= 2:
            t_str += f" (feels like {app:+.0f}°C)"
        parts.append(t_str)
    weather = _WMO_DESC.get(cur.get("weather_code"))
    if weather:
        parts.append(weather)
    wind = cur.get("wind_speed_10m")
    if wind is not None and wind >= 3:  # м/с; слабый ветер не упоминаем
        parts.append(f"wind {wind:.0f} m/s")
    if not parts:
        return None
    return f"{cfg.get('city', '?')}: {', '.join(parts)}"


# WMO-коды осадков (морось/дождь/ливни/снег) — для погодных предупреждений rhythm
_PRECIP_CODES = {
    51, 53, 55, 56, 57,                      # морось
    61, 63, 65, 66, 67,                      # дождь
    71, 73, 75, 77,                          # снег
    80, 81, 82, 85, 86,                      # ливни
}


def is_precip_code(code) -> bool:
    """Является ли WMO-код осадками (дождь/снег/морось/ливни)."""
    try:
        return int(code) in _PRECIP_CODES
    except (TypeError, ValueError):
        return False


def fetch_forecast(cfg: dict, hours: int = 12) -> dict | None:
    """Прогноз на ближайшие часы — для погодных предупреждений (features.rhythm):
      {"current_code": int|None, "current_temp": float|None,
       "hours": [{"time": datetime, "code": int|None,
                  "temp": float|None, "precip_prob": int|None}]}
    Времена — локальные naive datetime (Open-Meteo с timezone=auto; запуск
    локальный, часовой пояс машины совпадает с местом пользователя).
    None — прогноз недоступен (сеть/ответ без почасовых данных)."""
    with httpx.Client(timeout=_TIMEOUT) as client:
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

    Местоположение выключено — всё равно отдаём текущие дату/время сервера
    (запуск локальный, время сервера = время устройства пользователя): без этого
    персоны не знают который час и отвечают «не знаю». С местоположением —
    добавляется погода (сеть не чаще раза в 30 минут).
    """
    cfg = load_location()
    now = datetime.now()
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

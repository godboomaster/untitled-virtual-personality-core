"""Прогрев ботов при старте API: персоны, с которыми пользователь недавно
общался, поднимаются в фоне сразу после запуска — история и настройки уже
загружены к моменту, когда пользователь открывает чат, и фоновые циклы
(инициативы, напоминания, ритм) идут без ожидания первого захода в чат.

«Недавно» — пользователь писал персоне или открывал её чат в последние
API_WARMUP_HOURS часов (по умолчанию 48; 0 — прогрев выключен). Персоны,
к которым давно не заходили, остаются ленивыми (BotRegistry.get поднимет их
при первом обращении), замороженные (features.muted) не греются вовсе.

Метки захода — data/api_last_seen.json ({персона: unix-ts}), их ставит
touch() из эндпоинтов чата. Для сообщений, написанных до появления этого
файла, дополнительно берётся время последней реплики пользователя из STM.
"""

import logging
import os
import threading
import time

from app.api.runtime import get_persona_info, list_personas, registry
from app.core.atomic_io import atomic_write_json, load_json_safe
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

_TOUCH_THROTTLE_SEC = 60  # чаще раза в минуту метку на диск не пишем

_lock = threading.Lock()
_last_written: dict[str, float] = {}


def _last_seen_path():
    return data_dir() / "api_last_seen.json"


def _warmup_hours() -> float:
    try:
        return max(0.0, float(os.getenv("API_WARMUP_HOURS", "48")))
    except ValueError:
        return 48.0


def touch(persona: str) -> None:
    """Отметка «пользователь заходил к персоне» (открыл чат или написал).
    Синхронная запись файла — вызывать через asyncio.to_thread."""
    now = time.time()
    with _lock:
        if now - _last_written.get(persona, 0) < _TOUCH_THROTTLE_SEC:
            return
        _last_written[persona] = now
        path = _last_seen_path()
        data = load_json_safe(path, default={}, label="warmup")
        if not isinstance(data, dict):
            data = {}
        data[persona] = now
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, data)
        except Exception as e:
            logger.warning(f"[Warmup] Не удалось сохранить {path}: {e}")


def _last_seen(persona: str, marks: dict) -> float:
    # Позднейшая из меток: заход/сообщение через API и реплика пользователя в STM
    from app.api.home_api import _last_user_ts
    try:
        mark = float(marks.get(persona) or 0)
    except (TypeError, ValueError):
        mark = 0.0
    return max(mark, _last_user_ts(persona, "web_user") or 0.0)


def recent_personas(hours: float) -> list[str]:
    """Персоны с активностью пользователя за последние hours часов, свежие
    первыми. Замороженные пропускаются."""
    marks = load_json_safe(_last_seen_path(), default={}, label="warmup")
    if not isinstance(marks, dict):
        marks = {}
    cutoff = time.time() - hours * 3600
    recent = []
    for persona in list_personas():
        info = get_persona_info(persona)
        if info is None or (info.get("features") or {}).get("muted"):
            continue
        ts = _last_seen(persona, marks)
        if ts >= cutoff:
            recent.append((ts, persona))
    recent.sort(reverse=True)
    return [persona for _, persona in recent]


def _warm() -> None:
    hours = _warmup_hours()
    try:
        personas = recent_personas(hours)
    except Exception as e:
        logger.warning(f"[Warmup] Не удалось выбрать персоны: {e}")
        return
    if not personas:
        logger.info(f"[Warmup] Нет персон с активностью за {hours:g} ч — прогрев пропущен")
        return
    logger.info(f"[Warmup] Активность за {hours:g} ч: {', '.join(personas)} — загружаю")
    # По одной: BotRegistry создаёт инстансы под общим локом, а параллельный
    # подъём нескольких Chroma/эмбеддингов только умножил бы пик памяти
    for persona in personas:
        started = time.monotonic()
        try:
            registry.get(persona)
        except Exception as e:
            logger.warning(f"[Warmup] {persona}: не удалось загрузить: {e}")
            continue
        logger.info(f"[Warmup] {persona} загружена за {time.monotonic() - started:.1f} с")


def start_warmup() -> None:
    # Фоновый поток: сервер начинает отвечать сразу, не дожидаясь загрузки
    if _warmup_hours() <= 0:
        logger.info("[Warmup] API_WARMUP_HOURS=0 — прогрев ботов выключен")
        return
    threading.Thread(target=_warm, name="api-warmup", daemon=True).start()

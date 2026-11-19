"""Скорость текста у аватара — облачко портрета на рабочем столе (панель,
desktop/src/portrait) и виджета «Живой портрет» на телефоне (PortraitWidget):
реплика идёт слово за словом, длинная — частями под облачко.

    words_per_sec — слов в секунду (слово средней длины; длинное — дольше,
                    короткое — быстрее), 0 — сразу всей частью;
    part_sec      — сколько висит часть средней длины (≈100 символов),
                    прежде чем смениться следующей; короче — меньше, длиннее
                    — больше, в тех же пропорциях.

Значения по умолчанию (5 слов/с, 6,5 с) — те, что были зашиты в портрете
до настройки. Клиенты получают их вместе с репликами (GET /api/said) и в
лёгком списке портретов (GET /api/portraits): новая скорость — уже со
следующей реплики, отдельно ничего опрашивать не надо.

Хранение: data/avatar_text.json {"words_per_sec": …, "part_sec": …}. Читается
на каждый ответ ленты реплик — кеш в памяти, файл сверяется по (mtime,
size) не чаще раза в секунду. Запись — атомарно, под межпроцессным локом.
"""

import logging
import math
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from app.core.atomic_io import atomic_write_json, file_lock, load_json_safe
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

DEFAULTS: Dict[str, float] = {"words_per_sec": 5.0, "part_sec": 6.5}
# Слов в секунду: 0 — сразу, иначе от и до
WORDS_MIN, WORDS_MAX = 1.0, 20.0
PART_MIN, PART_MAX = 1.5, 30.0
RECHECK_SEC = 1.0

_lock = threading.Lock()
_cache: Dict[str, object] = {"values": None, "stat": None, "checked": 0.0}


def _path() -> Path:
    return data_dir() / "avatar_text.json"


def _stat_key(path: Path) -> tuple:
    try:
        st = path.stat()
    except OSError:
        return (str(path), None, None)
    return (str(path), st.st_mtime_ns, st.st_size)


def _valid(key: str, value) -> Optional[float]:
    """Число в допустимых пределах — оно же (float); нет — None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    if not math.isfinite(v):
        return None
    if key == "words_per_sec":
        return v if v == 0 or WORDS_MIN <= v <= WORDS_MAX else None
    if key == "part_sec":
        return v if PART_MIN <= v <= PART_MAX else None
    return None


def _parse(data) -> Dict[str, float]:
    # Битое или чужое значение — значение по умолчанию
    values = dict(DEFAULTS)
    if isinstance(data, dict):
        for k in DEFAULTS:
            v = _valid(k, data.get(k))
            if v is not None:
                values[k] = v
    return values


def get() -> Dict[str, float]:
    """Текущие значения (копия)."""
    now = time.monotonic()
    path = _path()
    with _lock:
        values = _cache["values"]
        stat = _cache["stat"]
        if (values is not None and stat and stat[0] == str(path)
                and now - float(_cache["checked"]) < RECHECK_SEC):
            return dict(values)
    key = _stat_key(path)
    with _lock:
        if _cache["values"] is not None and key == _cache["stat"]:
            _cache["checked"] = now
            return dict(_cache["values"])
    data = load_json_safe(path, default={}, label="AvatarText") if key[2] is not None else {}
    values = _parse(data)
    with _lock:
        _cache.update(values=values, stat=key, checked=now)
    return dict(values)


def get_settings() -> dict:
    """Для GET /api/settings/avatar-text: значения, умолчания и пределы."""
    return {**get(), "defaults": dict(DEFAULTS),
            "limits": {"words_per_sec": [WORDS_MIN, WORDS_MAX], "part_sec": [PART_MIN, PART_MAX]}}


def update(patch: Dict[str, float]) -> dict:
    """Частичная правка (PUT). Неизвестный ключ или значение вне пределов —
    ValueError, файл не трогается."""
    patch = dict(patch or {})
    bad = [k for k in patch if k not in DEFAULTS]
    if bad:
        raise ValueError(f"Неизвестные пункты: {', '.join(sorted(bad))}")
    clean = {}
    for k, v in patch.items():
        ok = _valid(k, v)
        if ok is None:
            raise ValueError(f"{k}: вне пределов")
        clean[k] = ok
    path = _path()
    with file_lock(path):
        values = _parse(load_json_safe(path, default={}, label="AvatarText"))
        values.update(clean)
        atomic_write_json(path, values)
        key = _stat_key(path)
    with _lock:
        _cache.update(values=values, stat=key, checked=time.monotonic())
    if clean:
        logger.info("[AvatarText] " + ", ".join(f"{k}={v:g}" for k, v in sorted(clean.items())))
    return get_settings()


def _reset_cache() -> None:
    # Тесты: смена папки данных / правка файла в обход update()
    with _lock:
        _cache.update(values=None, stat=None, checked=0.0)

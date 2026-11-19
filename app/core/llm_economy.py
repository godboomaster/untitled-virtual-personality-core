"""«Экономия нейронки»: где работу модели делает код — глобальная настройка
ядра (Настройки → «Экономия нейронки»), общая для всех персон.

Каждый пункт — переключатель: true — как раньше, работает модель; false
(дефолт, ключа нет) — то же делает код без вызова модели. Пункты:

  Фон и простой
    proactive_reflection         — вторая попытка инициативы (рефлексия), когда
                                   монолог модели промолчал; код — не пишем
                                   (рефлексия до порога монолога, где она —
                                   единственный вызов, остаётся всегда)
    life_inactive_chats          — жизнь персоны (тики, события мира, дневник)
                                   и в чатах, где пользователь давно не писал;
                                   код — только чаты с репликой за 3 дня
    harvest_timer                — урожай диалога и по таймеру (почти каждое
                                   сообщение в медленном чате); код — раз в
                                   HARVEST_MIN_MESSAGES реплик пользователя
    state_model_tick             — тик состояния моделью (у персон, где он
                                   разрешён use_gemma); код — эвристика
    room_llm_placement           — новый предмет в комнате расставляет модель
                                   (у персон с room_llm_placement); код — эвристика
    return_summary_llm           — «пока тебя не было» на пути ответа сжимает
                                   модель; код — последние записи как есть
  Путь ответа
    search_every_message         — веб-поиск на каждое сообщение, кроме связок
                                   (как раньше); код — только явная просьба
                                   найти или вопрос о свежих/внешних фактах
    question_regen               — финальный вопрос сверх лимита переписывает
                                   модель (второй полный вызов); код — срезает
                                   вопрос в конце

Хранение: data/llm_economy.json {"items": {key: bool}}. Читается на горячих
путях (ответ, тики жизни, цикл инициатив) — кеш в памяти, файл сверяется по
(mtime, size) не чаще раза в секунду: правка из веба доходит и до
Telegram-процесса ядра. Запись — атомарно, под межпроцессным локом, сразу в
кеш своего процесса.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from app.core.atomic_io import atomic_write_json, file_lock, load_json_safe
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

# Порядок — как в настройках: группа → пункты
GROUPS: Dict[str, Tuple[str, ...]] = {
    "background": (
        "proactive_reflection",
        "life_inactive_chats",
        "harvest_timer",
        "state_model_tick",
        "room_llm_placement",
        "return_summary_llm",
    ),
    "reply": (
        "search_every_message",
        "question_regen",
    ),
}
KEYS: Tuple[str, ...] = tuple(k for keys in GROUPS.values() for k in keys)
# Дефолт каждого пункта — код (модель выключена)
DEFAULTS: Dict[str, bool] = {k: False for k in KEYS}

# Файл сверяется с диском не чаще раза в RECHECK_SEC
RECHECK_SEC = 1.0

_lock = threading.Lock()
# items — текущие значения; stat — (путь, mtime_ns, size) файла, из которого
# они прочитаны (размер None — файла нет); checked — monotonic последней сверки
_cache: Dict[str, object] = {"items": None, "stat": None, "checked": 0.0}


def _path() -> Path:
    return data_dir() / "llm_economy.json"


def _stat_key(path: Path) -> tuple:
    # Путь — в ключе: папка данных могла смениться (VPC_DATA_DIR стенда)
    try:
        st = path.stat()
    except OSError:
        return (str(path), None, None)
    return (str(path), st.st_mtime_ns, st.st_size)


def _parse(data) -> Dict[str, bool]:
    # Только известные ключи и только bool: битое значение — дефолт пункта
    items = dict(DEFAULTS)
    raw = data.get("items") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        for k in KEYS:
            if isinstance(raw.get(k), bool):
                items[k] = raw[k]
    return items


def _items() -> Dict[str, bool]:
    now = time.monotonic()
    path = _path()
    with _lock:
        items = _cache["items"]
        stat = _cache["stat"]
        if (items is not None and stat and stat[0] == str(path)
                and now - float(_cache["checked"]) < RECHECK_SEC):
            return items
    key = _stat_key(path)
    with _lock:
        if _cache["items"] is not None and key == _cache["stat"]:
            _cache["checked"] = now
            return _cache["items"]
    data = load_json_safe(path, default={}, label="LLMEconomy") if key[2] is not None else {}
    items = _parse(data)
    with _lock:
        _cache.update(items=items, stat=key, checked=now)
    return items


def enabled(key: str) -> bool:
    """Пункт отдан модели (как раньше)? False — работает код (дефолт).
    Неизвестный ключ — False: опечатка в вызове не включает модель."""
    return bool(_items().get(key, False))


def get_settings() -> dict:
    """Для GET /api/settings/llm-economy: значения, дефолты и группы."""
    return {"items": dict(_items()), "defaults": dict(DEFAULTS),
            "groups": {g: list(keys) for g, keys in GROUPS.items()}}


def update(patch: Dict[str, bool]) -> dict:
    """Частичная правка (PUT): {key: bool}. Неизвестный ключ или не bool —
    ValueError, файл не трогается."""
    patch = dict(patch or {})
    bad = [k for k in patch if k not in DEFAULTS]
    if bad:
        raise ValueError(f"Неизвестные пункты: {', '.join(sorted(bad))}")
    wrong = [k for k, v in patch.items() if not isinstance(v, bool)]
    if wrong:
        raise ValueError(f"Нужно true/false: {', '.join(sorted(wrong))}")
    path = _path()
    with file_lock(path):
        items = _parse(load_json_safe(path, default={}, label="LLMEconomy"))
        items.update(patch)
        atomic_write_json(path, {"items": items})
        key = _stat_key(path)
    with _lock:
        _cache.update(items=items, stat=key, checked=time.monotonic())
    changed = ", ".join(f"{k}={'модель' if v else 'код'}" for k, v in sorted(patch.items()))
    if changed:
        logger.info(f"[LLMEconomy] {changed}")
    return get_settings()


def _reset_cache() -> None:
    # Тесты: смена папки данных / правка файла в обход update()
    with _lock:
        _cache.update(items=None, stat=None, checked=0.0)

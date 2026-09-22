"""Ограниченный кеш по ключу (chat_id / user_id) — один helper на проект.

Корень группы дефектов (аудит, п.7): состояние «на каждый чат/пользователя»
держалось в обычных ``dict``, которые только растут. У бота в группах
chat_id — это каждый чат, куда его когда-либо добавляли, а значение —
не счётчик, а буфер на ``max_messages`` сообщений (``ShortTermMemory.buffers``):
процесс живёт неделями и утекает ровно на объём всей переписки. Такие же
вечные словари были у счётчиков экстракции/консолидации
(``MemoryManager._extract_counters`` / ``_user_msg_counters``) и у
``FileVectorDB._loaded_docs``. Заплатка «чистить в одном месте» не лечит —
каждое новое такое состояние заводит утечку заново, поэтому здесь один
ограниченный контейнер, который ведёт себя как dict.

:class:`BoundedCache` — LRU по последнему обращению (+ необязательный TTL):
при переполнении вытесняется самый давно не использованный ключ. Важное
следствие для данных, которые нельзя терять: вытеснение допустимо только
если значение можно восстановить (STM-буфер перечитывается из ChromaDB —
см. ``ShortTermMemory._load_chat_from_db``) или его потеря безобидна
(счётчик обнулится, батч-экстракция просто произойдёт чуть позже).

Потокобезопасен: собственный ``RLock``, вложение в другие локи допустимо
(внутри лока кеша никогда не вызывается чужой код).
"""

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Iterator, Optional

_MISSING = object()


class BoundedCache:
    """dict-подобный кеш с ограничением по числу ключей (LRU) и TTL.

    Args:
        max_entries: максимум ключей; при превышении вытесняется LRU.
        ttl: время жизни записи в секундах (None — без TTL).
        on_evict: колбэк ``(key, value)`` при вытеснении/истечении —
            например, чтобы залогировать потерю.
    """

    def __init__(self, max_entries: int = 200, ttl: Optional[float] = None,
                 on_evict: Optional[Callable[[Any, Any], None]] = None):
        self.max_entries = max(1, int(max_entries))
        self.ttl = ttl
        self._on_evict = on_evict
        self._data: "OrderedDict[Any, Any]" = OrderedDict()
        self._stamps: dict = {}
        self._lock = threading.RLock()
        self.evictions = 0  # для тестов/диагностики

    # ─── Внутреннее ──────────────────────────────────────

    def _expired(self, key) -> bool:
        if self.ttl is None:
            return False
        return (time.time() - self._stamps.get(key, 0)) > self.ttl

    def _drop(self, key, evicted: bool):
        value = self._data.pop(key, None)
        self._stamps.pop(key, None)
        if evicted:
            self.evictions += 1
        if self._on_evict is not None:
            try:
                self._on_evict(key, value)
            except Exception:
                pass

    def _purge_expired(self):
        if self.ttl is None:
            return
        for key in [k for k in self._data if self._expired(k)]:
            self._drop(key, evicted=True)

    def _touch(self, key):
        self._data.move_to_end(key)
        self._stamps[key] = time.time()

    def _trim(self):
        while len(self._data) > self.max_entries:
            oldest = next(iter(self._data))
            self._drop(oldest, evicted=True)

    # ─── dict-подобный API ───────────────────────────────

    def __setitem__(self, key, value):
        with self._lock:
            self._purge_expired()
            self._data[key] = value
            self._touch(key)
            self._trim()

    def __getitem__(self, key):
        with self._lock:
            if key not in self._data or self._expired(key):
                if key in self._data:
                    self._drop(key, evicted=True)
                raise KeyError(key)
            self._touch(key)
            return self._data[key]

    def __delitem__(self, key):
        with self._lock:
            if key not in self._data:
                raise KeyError(key)
            self._drop(key, evicted=False)

    def __contains__(self, key) -> bool:
        with self._lock:
            if key not in self._data:
                return False
            if self._expired(key):
                self._drop(key, evicted=True)
                return False
            return True

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired()
            return len(self._data)

    def __iter__(self) -> Iterator:
        return iter(self.keys())

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def pop(self, key, default=_MISSING):
        with self._lock:
            if key in self._data and not self._expired(key):
                value = self._data[key]
                self._drop(key, evicted=False)
                return value
            self._data.pop(key, None)
            self._stamps.pop(key, None)
            if default is _MISSING:
                raise KeyError(key)
            return default

    def setdefault(self, key, default):
        with self._lock:
            try:
                return self[key]
            except KeyError:
                self[key] = default
                return default

    def keys(self) -> list:
        with self._lock:
            self._purge_expired()
            # Копия: вызывающий код итерирует её вне лока, и параллельный
            # add_message не должен ронять его "dictionary changed size"
            return list(self._data.keys())

    def values(self) -> list:
        with self._lock:
            self._purge_expired()
            return list(self._data.values())

    def items(self) -> list:
        with self._lock:
            self._purge_expired()
            return list(self._data.items())

    def clear(self):
        with self._lock:
            self._data.clear()
            self._stamps.clear()

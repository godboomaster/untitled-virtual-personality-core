"""Сигнал «у персоны что-то изменилось» для открытого веба (долгий опрос).

Веб опрашивает inbox каждой персоны раз в 15 с. Поэтому сообщение,
отправленное с другого устройства, «печатает» и готовый ответ доходили до
второго устройства с опозданием до тика. Теперь веб держит открытым
GET /api/changes: ответ приходит, как только у персоны началась или
кончилась генерация, что-то легло в историю или пришло фоновое сообщение.
Тогда веб сразу опрашивает inbox этих персон. Сами данные по-прежнему
отдаёт inbox, здесь только номер изменения и чьё оно.

Номера живут, пока жив процесс: EPOCH отличает этот процесс от прежнего,
и номер клиента из прошлого запуска не применяется.
"""

import asyncio
import threading
import uuid
from collections import deque
from typing import Awaitable, Callable, Optional

from app.api import inbox as _inbox

EPOCH = uuid.uuid4().hex[:12]
MAX_WAIT_SEC = 60
# Изменения идут пачками (реплика и ответ в историю, затем конец генерации):
# проснувшийся запрос чуть ждёт, чтобы ответить на пачку одним разом
_SETTLE_SEC = 0.15
_STOP_POLL_SEC = 1.0

_lock = threading.Lock()
_version = 0
# Последние изменения: (номер, персона). Номер клиента старше окна — ответ
# None: что менялось, неизвестно, клиенту опросить всех
_recent: deque = deque(maxlen=512)
# Ожидающие запросы: (их event loop, событие). bump зовут из разных потоков
# (генерация в пуле, фоновый loop менеджеров) — будим через call_soon_threadsafe
_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()


def bump(persona: str) -> None:
    """У персоны что-то изменилось (из любого потока)."""
    global _version
    with _lock:
        _version += 1
        _recent.append((_version, persona))
        waiters = list(_waiters)
    for loop, event in waiters:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # loop уже закрыт — ядро останавливается


def changed_since(since: Optional[int]) -> tuple[int, Optional[list[str]]]:
    """→ (текущий номер, персоны с изменениями после since). None вместо
    списка — since не из этого окна (None, больше текущего, старше хранимых
    изменений): клиенту опросить всех."""
    with _lock:
        if since is None or since > _version:
            return _version, None
        if since == _version:
            return _version, []
        if not _recent or since < _recent[0][0] - 1:
            return _version, None
        return _version, sorted({p for v, p in _recent if v > since})


async def wait_changes(since: Optional[int], timeout: float,
                       gone: Optional[Callable[[], Awaitable[bool]]] = None
                       ) -> tuple[int, Optional[list[str]]]:
    """Как changed_since, но пустой ответ ждёт до timeout секунд (не больше
    MAX_WAIT_SEC): отвечает, как только у кого-то что-то изменилось, по
    истечении времени, при остановке ядра или когда клиент ушёл (gone)."""
    version, names = changed_since(since)
    timeout = min(max(timeout, 0.0), MAX_WAIT_SEC)
    if names != [] or timeout <= 0:
        return version, names
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    event = asyncio.Event()
    waiter = (loop, event)
    with _lock:
        _waiters.add(waiter)
    try:
        # Изменение между первой проверкой и регистрацией ожидания не
        # потеряется: проверяем ещё раз уже под ожиданием
        version, names = changed_since(since)
        while names == []:
            left = deadline - loop.time()
            if left <= 0 or _inbox._stopping() or (gone is not None and await gone()):
                return version, names
            try:
                await asyncio.wait_for(event.wait(), min(left, _STOP_POLL_SEC))
            except (asyncio.TimeoutError, TimeoutError):
                pass
            if event.is_set():
                event.clear()
                await asyncio.sleep(_SETTLE_SEC)
            version, names = changed_since(since)
        return version, names
    finally:
        with _lock:
            _waiters.discard(waiter)

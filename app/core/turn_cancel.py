"""Отмена хода, который уже генерируется.

Поток генерации нельзя убить снаружи — он сам проверяет флажок своего хода
и выходит (TurnCancelled):
  - роутер — перед каждым вызовом модели и между кусками потокового ответа
    (API-провайдеры при отмене ведутся потоком: закрытое соединение — и
    провайдер перестаёт писать, платим за написанное);
  - ход персоны — до догенерации и до записи ответа в историю.

Ход — запрос в чат (персона:чат, как лок чата в app/api/server.py). Отменяют:
новая реплика с supersede («заменяет прежнюю» — голосовая активация, когда
человек заговорил, пока персона думала) и POST /api/chat/cancel. Отменяется
только начатая генерация (поток уже взял лок чата): реплика в очереди за ней
ещё не записана в историю — её отмена потеряла бы сказанное.

Веб-чаты в браузере посреди ответа не обрываются: сайт допишет, ответ
выбросится (новая реплика в тот же тред не уйдёт, пока сайт пишет). Короткий
ответ им задаёт подсказка длины (app/core/reply_length).

TurnCancelled — BaseException: десятки «except Exception» на пути ответа
(аддоны, поиск, маркеры) её не глотают; ловит её только тот, кто открыл ход."""
from __future__ import annotations

import contextlib
import contextvars
import threading
from typing import Iterator, Optional


class TurnCancelled(BaseException):
    """Ход отменён: выйти, не записывая ответ."""


class Token:
    def __init__(self, key: str, stream_abort: bool = False):
        self.key = key
        self.event = threading.Event()
        self.started = False
        self.reason = ""
        # API-провайдера звать потоком, чтобы оборвать посреди ответа (ходы
        # голосом); без флага — отмена только между вызовами модели
        self.stream_abort = stream_abort
        self._callbacks: list = []

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def cancel(self, reason: str = "") -> None:
        self.reason = reason or self.reason
        with _lock:
            first = not self.event.is_set()
            self.event.set()
            cbs, self._callbacks = (self._callbacks, []) if first else ([], self._callbacks)
        for cb in cbs:
            try:
                cb()
            except Exception:
                pass

    def on_cancel(self, cb) -> None:
        """cb() — когда ход отменят (уже отменён — сразу). Зовётся в потоке
        того, кто отменил: в event loop — через call_soon_threadsafe."""
        with _lock:
            if not self.event.is_set():
                self._callbacks.append(cb)
                return
        cb()


_lock = threading.Lock()
_open: dict[str, list[Token]] = {}
_current: contextvars.ContextVar[Optional[Token]] = contextvars.ContextVar("turn_cancel", default=None)


def open_turn(key: str, stream_abort: bool = False) -> Token:
    """Ход в чате key: с этого момента его можно отменить."""
    tok = Token(key, stream_abort)
    with _lock:
        _open.setdefault(key, []).append(tok)
    return tok


def close_turn(tok: Token) -> None:
    with _lock:
        lst = _open.get(tok.key)
        if lst and tok in lst:
            lst.remove(tok)
            if not lst:
                _open.pop(tok.key, None)


def cancel(key: str, reason: str = "") -> int:
    """Отменить начатую генерацию в чате key. → сколько ходов отменено."""
    with _lock:
        toks = [t for t in _open.get(key, ()) if t.started and not t.cancelled]
    for t in toks:
        t.cancel(reason)
    return len(toks)


def cancelled_count(key: str) -> int:
    """Отменённые, но ещё не вышедшие ходы чата (веб-чат дописывает на
    сайте): «печатает» по ним уже не показываем — ответа не будет."""
    with _lock:
        return sum(1 for t in _open.get(key, ()) if t.cancelled)


def busy(key: str) -> bool:
    with _lock:
        return any(t.started and not t.cancelled for t in _open.get(key, ()))


@contextlib.contextmanager
def scope(tok: Optional[Token]) -> Iterator[Optional[Token]]:
    """Поток генерации хода tok: роутер и ход видят его флажок. Генерация
    началась (лок чата взят) — отсюда отмена действует."""
    if tok is not None:
        tok.started = True
    ctx = _current.set(tok)
    try:
        yield tok
    finally:
        _current.reset(ctx)


def current() -> Optional[Token]:
    return _current.get()


def cancelled() -> bool:
    tok = _current.get()
    return tok is not None and tok.cancelled


def check() -> None:
    """Ход этого потока отменён — TurnCancelled."""
    tok = _current.get()
    if tok is not None and tok.cancelled:
        raise TurnCancelled(tok.reason or "отменён")

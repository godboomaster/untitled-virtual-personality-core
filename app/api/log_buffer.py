"""Кольцевой буфер логов для режима разработчика в веб-UI.

Хендлер вешается на root-логгер при старте API-сервера; записи доступны
через GET /api/logs?since=N (инкрементальная выборка по seq). В буфер не
попадают шумные инфраструктурные логгеры (uvicorn access, httpx и т.п.).
"""

import itertools
import logging
import time
from collections import deque

_MAX_ENTRIES = 1500
_entries: deque = deque(maxlen=_MAX_ENTRIES)
_seq = itertools.count(1)

# Инфраструктурный шум, не интересный при отладке ответов
_NOISY_LOGGERS = ("uvicorn", "httpx", "httpcore", "chromadb", "sentence_transformers")


class _RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        try:
            if record.name.startswith(_NOISY_LOGGERS):
                return
            msg = record.getMessage()
        except Exception:
            return
        # Прогресс-бары и переносы строк в панели не нужны
        msg = " ".join(str(msg).split())[:400]
        if not msg:
            return
        _entries.append({
            "seq": next(_seq),
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        })


def install():
    """Повесить хендлер на root-логгер (идемпотентно)."""
    root = logging.getLogger()
    if any(isinstance(h, _RingBufferHandler) for h in root.handlers):
        return
    root.addHandler(_RingBufferHandler())
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)


def since(seq: int, limit: int = 500) -> dict:
    """Записи с seq > переданного + маркер последнего seq (для следующего опроса)."""
    out = [e for e in _entries if e["seq"] > seq]
    if limit and len(out) > limit:
        out = out[-limit:]
    latest = _entries[-1]["seq"] if _entries else 0
    return {"entries": out, "latest": latest}

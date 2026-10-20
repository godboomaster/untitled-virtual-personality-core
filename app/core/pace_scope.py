"""Автоматические вызовы веб-чата: темп отправок (app/features/web_llm.py).

Канал сам по себе не говорит, кто ждёт ответа: «cc» — это и команда
человека («открой ютуб», ответ на его реплику), и цикл агента задач, который
шлёт шаг за шагом без человека. Ждать паузу между отправками должна только
автоматика, поэтому цикл агента помечает свои вызовы этой областью
(фоновые каналы side/proactive/cc_gen — автоматика и без неё).

Ключ области — одна задача (прогон агента): запасной веб-чат, который роутер
выбрал, когда назначенный не ответил, держится за ней до конца
(app/core/router.py, _auto_fallback).

ContextVar — по той же причине, что и dialog_scope.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

_AUTO: ContextVar[bool] = ContextVar("webchat_automated", default=False)
_KEY: ContextVar[Optional[str]] = ContextVar("webchat_automated_key",
                                             default=None)


def is_automated() -> bool:
    return _AUTO.get()


def automation_key() -> Optional[str]:
    """Ключ текущей области автоматики (None — вне области или без ключа)."""
    return _KEY.get() if _AUTO.get() else None


@contextmanager
def automated_calls(key: Optional[str] = None):
    """Вызовы модели внутри — автоматика (темп сайта, часовой потолок).
    key — задача, за которой держится выбранный запасной веб-чат."""
    token = _AUTO.set(True)
    ktoken = _KEY.set(key)
    try:
        yield
    finally:
        _KEY.reset(ktoken)
        _AUTO.reset(token)

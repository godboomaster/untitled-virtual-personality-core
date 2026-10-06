"""Автоматические вызовы веб-чата: темп отправок (app/features/web_llm.py).

Канал сам по себе не говорит, кто ждёт ответа: «cc» — это и команда
человека («открой ютуб», ответ на его реплику), и цикл агента задач, который
шлёт шаг за шагом без человека. Ждать паузу между отправками должна только
автоматика, поэтому цикл агента помечает свои вызовы этой областью
(фоновые каналы side/proactive/cc_gen — автоматика и без неё).

ContextVar — по той же причине, что и dialog_scope.
"""

from contextlib import contextmanager
from contextvars import ContextVar

_AUTO: ContextVar[bool] = ContextVar("webchat_automated", default=False)


def is_automated() -> bool:
    return _AUTO.get()


@contextmanager
def automated_calls():
    """Вызовы модели внутри — автоматика (темп сайта, часовой потолок)."""
    token = _AUTO.set(True)
    try:
        yield
    finally:
        _AUTO.reset(token)

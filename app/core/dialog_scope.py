"""Область диалога: какой чат (Telegram-чат, веб-чат) обслуживает текущий код.

Зачем: веб-чат-провайдер (app/features/web_llm.py) держит на сайте постоянный
тред, и сайт помнит все прошлые ходы этого треда. Один тред на персону —
значит, модель видит промпты ДРУГИХ чатов персоны (05.10: Арродес в одной
группе назвал по имени человека из другой группы). С областью у каждого чата
свой тред на сайте; вне области — общий тред персоны, как раньше (дневник,
мир, служебные вызовы без чата).

ContextVar, а не threading.local: asyncio.to_thread копирует контекст в
поток, поэтому область, поставленная в корутине чата (инициатива, напоминание,
урок), доходит до синхронного LLM-вызова в потоке. threading.Thread и
executor.submit контекст НЕ копируют — там область ставится явно
(dialog_scope) или переносится через contextvars.copy_context().
"""

import functools
import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Optional

_DIALOG: ContextVar[Optional[str]] = ContextVar("dialog_scope", default=None)


def current_dialog() -> Optional[str]:
    # Ключ текущего чата; None — вне области (общий тред персоны)
    return _DIALOG.get()


@contextmanager
def dialog_scope(key):
    """Код внутри — работа с чатом key (тот же ключ, что у STM: chat_id,
    иначе user_id). Пустой key — область не меняется."""
    if key is None or str(key) == "":
        yield
        return
    token = _DIALOG.set(str(key))
    try:
        yield
    finally:
        _DIALOG.reset(token)


def scoped_by(get_key: Callable):
    """Декоратор функции/корутины одного чата: область диалога — по
    аргументам вызова, get_key(*args, **kwargs) → ключ (ошибка — без
    области: декоратор не должен ломать сам вызов)."""
    def _key(args, kwargs):
        try:
            return get_key(*args, **kwargs)
        except Exception:
            return None

    def deco(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def _async(*args, **kwargs):
                with dialog_scope(_key(args, kwargs)):
                    return await fn(*args, **kwargs)
            return _async

        @functools.wraps(fn)
        def _sync(*args, **kwargs):
            with dialog_scope(_key(args, kwargs)):
                return fn(*args, **kwargs)
        return _sync
    return deco

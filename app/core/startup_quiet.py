"""Тишина после запуска: первые 10–20 минут боты сами не пишут.

После старта (API или Telegram) все персоны разом просыпаются: инициатива,
утреннее приветствие, «жизнь» персоны, уроки, пропущенные напоминания — и
всё это сообщения в чаты плюс запросы к моделям, в том числе к веб-чатам
(DeepSeek и др.), которые блокируют аккаунт за всплески. Поэтому фоновая
работа каждой персоны начинается не сразу: у каждой свой случайный срок в
пределах BOT_STARTUP_QUIET_MIN (по умолчанию «10-20» минут) — персоны
просыпаются вразнобой, а не все в одну минуту.

На ответы человеку тишина не действует. Напоминания, чьё время наступает
после запуска, приходят вовремя; ждут только пропущенные, пока бот был
выключен.

BOT_STARTUP_QUIET_MIN: «10-20» — случайно в диапазоне, «15» — ровно, «0» —
без тишины.

Отсчёт включает только настоящий запуск (app.main → mark_started): тесты и
прямой импорт модулей тишины не получают.
"""

import asyncio
import logging
import os
import random
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_RANGE = (10.0, 20.0)

_lock = threading.Lock()
_started: float | None = None  # None — запуска не было (тесты, импорт): тишины нет
_until: dict = {}


def mark_started(now: float | None = None) -> None:
    """Точка отсчёта — запуск процесса (app.main); сроки персон заново."""
    global _started
    with _lock:
        _started = time.time() if now is None else now
        _until.clear()


def started_at() -> float:
    return _started or 0.0


def _range() -> tuple:
    raw = (os.getenv("BOT_STARTUP_QUIET_MIN") or "").strip()
    if not raw:
        return DEFAULT_RANGE
    try:
        if "-" in raw:
            lo, hi = (float(x) for x in raw.split("-", 1))
        else:
            lo = hi = float(raw)
    except ValueError:
        logger.warning(f"[Quiet] BOT_STARTUP_QUIET_MIN={raw!r} — не число, беру 10-20")
        return DEFAULT_RANGE
    lo, hi = max(0.0, lo), max(0.0, hi)
    return (min(lo, hi), max(lo, hi))


def quiet_until(key: str) -> float:
    """До какого момента персона key молчит (свой случайный срок на процесс)."""
    key = str(key)
    with _lock:
        if _started is None:
            return 0.0
        if key not in _until:
            lo, hi = _range()
            _until[key] = _started + random.uniform(lo, hi) * 60.0
        return _until[key]


def quiet_left(key: str) -> float:
    """Сколько секунд персоне ещё молчать (0 — тишина кончилась)."""
    return max(0.0, quiet_until(key) - time.time())


def is_quiet(key: str) -> bool:
    return quiet_left(key) > 0


async def wait_quiet(key: str, label: str = "") -> None:
    """Фоновый цикл персоны ждёт конца её тишины."""
    left = quiet_left(key)
    if left <= 0:
        return
    logger.info(f"{label or '[Quiet]'} {key}: тишина после запуска — "
                f"начну через {left / 60:.0f} мин")
    await asyncio.sleep(left)

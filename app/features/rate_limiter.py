"""
Rate limiter — ограничение сообщений на пользователя.
Используется персонами с features.rate_limit: true
"""

import os
import time
from collections import defaultdict


RATE_LIMIT_DEFAULT = int(os.getenv("RATE_LIMIT_DEFAULT", "6"))
RATE_WINDOW = int(os.getenv("RATE_WINDOW", "3600"))

_user_requests: dict[str, list[float]] = defaultdict(list)
_punish_blocked: dict[str, float] = {}  # user_id -> block_until timestamp


def block_user(user_id: str, duration: int = None):
    # Заблокировать пользователя на duration секунд
    _punish_blocked[user_id] = time.time() + (duration or RATE_WINDOW)


def is_blocked(user_id: str) -> bool:
    # Проверить, заблокирован ли пользователь (punish block)
    if user_id not in _punish_blocked:
        return False
    if time.time() > _punish_blocked[user_id]:
        del _punish_blocked[user_id]
        return False
    return True


def get_rate_limit(user_id: str, individual_limits: dict = None) -> int:
    """
    Получить лимит для пользователя.
    individual_limits: {user_id: limit, ...}, 0 = без лимита
    """
    if individual_limits:
        uid = str(user_id)
        if uid in individual_limits:
            return individual_limits[uid]
    return RATE_LIMIT_DEFAULT


def check_rate_limit(user_id: str, individual_limits: dict = None) -> bool:
    # Возвращает True, если пользователь НЕ превысил лимит
    limit = get_rate_limit(user_id, individual_limits)
    if limit == 0:
        return True
    if is_blocked(user_id):
        return False
    now = time.time()
    timestamps = _user_requests[user_id]
    _user_requests[user_id] = [t for t in timestamps if now - t < RATE_WINDOW]
    if len(_user_requests[user_id]) >= limit:
        return False
    _user_requests[user_id].append(now)
    return True


def get_status_text(individual_limits: dict = None) -> str:
    # Текст для команды /ratelimits
    now = time.time()
    lines = []
    for uid, timestamps in _user_requests.items():
        active = [t for t in timestamps if now - t < RATE_WINDOW]
        _user_requests[uid] = active
        if not active:
            continue
        user_limit = get_rate_limit(uid, individual_limits)
        remaining = user_limit - len(active)
        oldest = active[0]
        mins_left = int((oldest + RATE_WINDOW - now) // 60)
        secs_left = int((oldest + RATE_WINDOW - now) % 60)
        if remaining <= 0:
            lines.append(f"{uid} — лимит исчерпан (сброс через {mins_left}м {secs_left}с)")
        else:
            lines.append(f"{uid} — {remaining}/{user_limit} осталось (сброс через {mins_left}м {secs_left}с)")
    return "\n".join(lines) if lines else "Нет пользователей с активным лимитом."

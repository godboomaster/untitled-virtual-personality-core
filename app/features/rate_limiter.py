"""
Rate limiter — ограничение сообщений на пользователя.
Используется персонами с features.rate_limit: true
"""

import os
import time

from app.core.bounded_cache import BoundedCache


RATE_LIMIT_DEFAULT = int(os.getenv("RATE_LIMIT_DEFAULT", "6"))
RATE_WINDOW = int(os.getenv("RATE_WINDOW", "3600"))

# BoundedCache, а не обычный dict: словарь рос бы бессрочно на каждого
# нового user_id (групповой чат, веб) — процесс живёт неделями и не
# подчищает ни разбаненных, ни просто неактивных пользователей.
# Вытеснение LRU безобидно: у _user_requests это просто сброс счётчика
# (лимит отмерится заново), у _punish_blocked — досрочное снятие бана, что
# допустимо: для вытеснения нужны 5000 более свежих пользователей.
_MAX_TRACKED_USERS = 5000

_user_requests = BoundedCache(max_entries=_MAX_TRACKED_USERS)  # user_id -> [timestamp, ...]
_punish_blocked = BoundedCache(max_entries=_MAX_TRACKED_USERS)  # user_id -> block_until timestamp


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
    timestamps = [t for t in _user_requests.get(user_id, []) if now - t < RATE_WINDOW]
    if len(timestamps) >= limit:
        _user_requests[user_id] = timestamps
        return False
    timestamps.append(now)
    _user_requests[user_id] = timestamps
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

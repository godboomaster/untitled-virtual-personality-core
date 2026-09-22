"""Общая политика ретенции персистентных словарей по chat_id (аудит: задача
«ретенция»).

Корень дефекта: несколько структур растут по одному чату на каждую запись и
никогда не уменьшаются — proactive_messaging.ChatActivityTracker
(_known_chats/_last_activity/_chat_topics), state_engine._states,
relationship._chats. Разовый чат (человек написал один раз и больше не
вернулся) остаётся в файле и в памяти бессрочно, и тиков/сохранений на такой
чат со временем не становится меньше — политики «чат давно неактивен, запись
не нужна» не было вовсе. Один helper вместо трёх похожих циклов «найти
старое — удалить», разбросанных по каждому менеджеру.

Политика: запись, у которой ЕСТЬ метка последней активности и она старше
CHAT_RETENTION_DAYS дней — удаляется. Запись БЕЗ метки (легаси — создана до
того, как метка появилась именно в этой структуре) НЕ удаляется: иначе
первый же прогон после обновления кода стёр бы разом всю накопленную
историю без разбора свежести. Такая запись начнёт пруниться сама, как
только получит метку на следующей записи/тике.
"""

import logging
import os
import threading
import time
from typing import Callable, Dict, Hashable, List, Optional

logger = logging.getLogger(__name__)

# Сколько дней без активности запись чата считается ещё актуальной. Env —
# чтобы можно было ужесточить/ослабить без релиза, тем же приёмом, что и
# остальные TTL/лимиты в проекте (см. os.getenv(...) в app/core/config.py).
CHAT_RETENTION_DAYS = float(os.getenv("CHAT_RETENTION_DAYS", "180"))

# Как часто на живущем процессе допустимо перезапускать prune_stale поверх
# уже прогруженных структур (аудит: раньше прореживание было только при
# загрузке — процесс месяцами не перезапускается, разовые чаты копятся
# между рестартами бессрочно). Env — тем же приёмом, что и CHAT_RETENTION_DAYS.
RETENTION_TICK_HOURS = float(os.getenv("RETENTION_TICK_HOURS", "6"))


class RetentionTimer:
    """Дозор для периодического прореживания: «не чаще раза в interval_sec».

    Каждый из трёх менеджеров (ChatActivityTracker, StateEngine,
    RelationshipMemory) зовёт свой ``_prune_stale_*`` из уже существующего
    периодического пути (тик/цикл инициатив — НЕ на каждое сообщение), но
    сам прогон ``prune_stale`` — это проход по всему словарю: незачем
    делать его на каждый такой вызов, если тик может случаться раз в
    минуты, а актуальность ретенции меряется днями. ``due()`` возвращает
    True не чаще раза в ``interval_sec`` и сама взводит следующий срок —
    вызывающая сторона не должна отдельно запоминать «уже сработало».
    Потокобезопасен — тик может прийти из разных потоков (фоновый цикл vs
    вызов из обработчика сообщения)."""

    def __init__(self, interval_sec: float = RETENTION_TICK_HOURS * 3600):
        self._interval = interval_sec
        self._last_fire = 0.0
        self._lock = threading.Lock()

    def due(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            if now - self._last_fire < self._interval:
                return False
            self._last_fire = now
            return True


def prune_stale(records: Dict[Hashable, object],
                last_seen: Callable[[Hashable, object], Optional[float]],
                max_age_days: float = CHAT_RETENTION_DAYS, *,
                keep_min: int = 0,
                label: str = "") -> List[Hashable]:
    """Удаляет из ``records`` (мутирует словарь на месте) ключи, чья
    ``last_seen(key, value)`` старше ``max_age_days`` дней.

    ``last_seen`` вернул None (или бросил исключение) — метки у записи нет,
    это легаси — запись НЕ трогаем, вне зависимости от возраста.

    ``keep_min`` — не опускаться ниже этого числа записей в ``records``,
    даже если формально устарело больше (совсем маленькая база — рано
    подчищать под ноль на старте); удаляются самые старые сверх этого
    минимума, а не случайные.

    Возвращает список удалённых ключей — вызывающая сторона логирует под
    своей меткой (разные структуры, разный смысл «сколько удалено» и что
    делать с ключом дальше — например, proactive_messaging синхронизирует
    по нему ещё два словаря)."""
    if not records:
        return []
    cutoff = time.time() - max_age_days * 86400
    stale = []
    for key, value in list(records.items()):
        try:
            ts = last_seen(key, value)
        except Exception as e:
            logger.debug(f"[retention] last_seen сорвался на ключе {key!r}: {e}")
            ts = None
        if ts is not None and ts < cutoff:
            stale.append((key, ts))
    if not stale:
        return []
    if keep_min > 0:
        removable = max(0, len(records) - keep_min)
        if removable < len(stale):
            stale.sort(key=lambda kv: kv[1])  # старые — первыми на удаление
            stale = stale[:removable]
    removed = []
    for key, _ in stale:
        records.pop(key, None)
        removed.append(key)
    if removed and label:
        logger.info(f"[{label}] Ретенция: удалено {len(removed)} неактивных "
                    f"записей чатов (старше {max_age_days:.0f} дн. без активности)")
    return removed

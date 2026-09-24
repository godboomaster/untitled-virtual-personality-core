"""Присутствие пользователя в веб-вкладке чата — по (контекст, чат).

Фронт сообщает состояние «вкладка видима и в фокусе, открыт чат X с персоной P»
двумя путями: мгновенно — POST /api/presence по visibilitychange/focus/blur,
и heartbeat'ом — параметром focused=1 в поллинге GET
/api/personas/{p}/inbox (каждые 15 с, только для персоны открытого чата).

Пока отметка по чату свежа, бот не делает по ЭТОМУ чату побочных дел:
самоинициатива, батч-экстракция и консолидация LTM, анализ досье,
self-memory, урожай диалога и living-тик чата молчат. Вкладка скрыта/закрыта
или пользователь ушёл в другой чат — отметка снимается явным active=false
или протухает по TTL.

Ключ — пара (контекст BotInstance, chat_id), а не глобальный флаг: контекст
у веб-персоны ``api_{persona}`` (см. :func:`web_context` и registry в
app/api/runtime.py), у Telegram-персоны — её собственный. Так одна открытая
веб-вкладка не замораживает фоновую жизнь других персон и чатов, включая
Telegram-чаты той же персоны (у них веб-вкладки нет вовсе).

Ключи ограничены и протухают (BoundedCache: LRU + TTL) — чатов за время
жизни процесса может быть сколько угодно, вечный dict здесь был бы утечкой.

В Telegram-режиме сигналов нет — is_active() для его контекста всегда False.
"""

import threading
import time

from app.core.bounded_cache import BoundedCache


def web_context(persona: str) -> str:
    """Контекст BotInstance веб-персоны по её id — тот же ключ, которым бота
    создаёт registry (``BotInstance(persona_name=p, context=f"api_{p}")``).
    Единственное место соответствия «id персоны в API ↔ контекст» для
    presence: фронт знает только id, а ключ должен совпасть с тем, что
    спрашивают bot_instance/memory/living/proactive по self.context."""
    return f"api_{persona}"


class WebPresence:
    """Отметки «веб-вкладка чата активна» по (контекст, chat_id) с TTL:
    heartbeat поллинга держит отметку живой, явный active=false или тишина
    дольше TTL — снимает.

    Значение записи — момент heartbeat'а (monotonic), свежесть считается по
    нему: TTL самого кеша здесь только сборщик мусора для ключей, к которым
    больше не обращаются (чтение через get() обновляет его отметку LRU, но
    на ответ is_active это не влияет — решает сохранённый момент)."""

    def __init__(self, ttl: float = 45.0, max_keys: int = 256):
        self._ttl = ttl
        # TTL кеша с запасом к TTL присутствия: запись, которую перестали и
        # обновлять, и спрашивать, уходит сама, не занимая место в LRU
        self._marks = BoundedCache(max_entries=max_keys, ttl=ttl * 10)
        self._lock = threading.Lock()

    @staticmethod
    def _key(context: str, chat_id) -> tuple:
        return (str(context or "default"), str(chat_id))

    def note(self, context: str, chat_id, active: bool):
        # Отметка о состоянии вкладки конкретного чата конкретной персоны.
        key = self._key(context, chat_id)
        with self._lock:
            if active:
                self._marks[key] = time.monotonic()
            else:
                self._marks.pop(key, None)

    def is_active(self, context: str, chat_id) -> bool:
        # Открыта ли прямо сейчас веб-вкладка ЭТОГО чата этой персоны.
        key = self._key(context, chat_id)
        with self._lock:
            return self._fresh(self._marks.get(key))

    def any_active(self, context: str) -> bool:
        """Открыта ли веб-вкладка хоть одного чата этой персоны. Нужно для
        операций уровня персоны, которые нельзя разделить по чатам
        (living_persona._tick_all: сценарист и внешние стимулы)."""
        ctx = str(context or "default")
        with self._lock:
            return any(self._fresh(stamp) for (c, _cid), stamp
                       in self._marks.items() if c == ctx)

    def _fresh(self, stamp) -> bool:
        return bool(stamp) and (time.monotonic() - stamp) < self._ttl

    def clear(self):
        # Сброс всех отметок (тесты, перезапуск).
        with self._lock:
            self._marks.clear()


web_presence = WebPresence()

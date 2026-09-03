"""Доставка фоновых сообщений (напоминания, уроки, инициативы) в веб.

В Telegram-режиме менеджеры шлют через TelegramMessageSender. В API-режиме
транспорт — WebInboxSender: сообщение кладётся в очередь (persona, chat_id),
а фронт забирает её polling'ом GET /api/personas/{p}/inbox.

Здесь же — фоновый event loop API-режима: reminder/learning/proactive
запускаются на нём (как на loop'е бота в main.py).
"""

import asyncio
import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

# Очередь входящих: (persona, chat_id) → сообщения. In-memory: переживает
# только жизнь процесса — при рестарте недоставленное теряется (приемлемо:
# напоминание/урок повторится по расписанию, если не сработало).
_inbox: dict[tuple[str, str], deque] = {}
_inbox_lock = threading.Lock()
_MAX_QUEUED = 100


def inbox_push(persona: str, chat_id: str, text: str, kind: str = "message"):
    key = (persona, str(chat_id))
    with _inbox_lock:
        q = _inbox.setdefault(key, deque(maxlen=_MAX_QUEUED))
        q.append({"text": text, "kind": kind, "ts": time.time()})


def inbox_pop(persona: str, chat_id: str) -> list[dict]:
    """Забрать все накопленные сообщения (pop-семантика)."""
    key = (persona, str(chat_id))
    with _inbox_lock:
        q = _inbox.get(key)
        if not q:
            return []
        items = list(q)
        q.clear()
        return items


class WebInboxSender:
    """MessageSender-совместимый транспорт: кладёт сообщения в inbox веб-чата."""

    def __init__(self, persona: str):
        self._persona = persona

    async def send_message(self, chat_id: str, text: str, *,
                           topic_id=None, parse_mode=None) -> bool:
        inbox_push(self._persona, chat_id, text)
        return True

    async def send_document(self, chat_id: str, file_path: str, filename: str, *,
                            caption=None, topic_id=None, parse_mode=None) -> bool:
        # Файл через inbox не доставить — шлём уведомление с именем и подписью
        text = f"📎 {filename}"
        if caption:
            text += f"\n{caption}"
        inbox_push(self._persona, chat_id, text, kind="document")
        return True

    def get_last_sent_message_id(self, chat_id: str):
        return None  # у веб-сообщений нет id — reply-to-логика обучения не используется


# ── Фоновый event loop для reminder/learning/proactive ────────────────

_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_lock = threading.Lock()


def _run_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def background_loop() -> asyncio.AbstractEventLoop:
    """Общий фоновый loop API-процесса (создаётся при первом боте с фичами)."""
    global _bg_loop
    with _bg_lock:
        if _bg_loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=_run_loop, args=(loop,), daemon=True, name="api-bg").start()
            _bg_loop = loop
    return _bg_loop


def wire_reminder_for_api(persona: str, bot, sender=None) -> None:
    """Подключает reminder-менеджер бота к веб-inbox (sender/память/LLM-текст/
    заморозка) и запускает его фоновый цикл. Используется при старте бота и при
    живом включении фичи reminder через настройки (без рестарта сервера)."""
    if bot.reminder_manager is None:
        return
    sender = sender or getattr(bot, "_api_inbox_sender", None) or WebInboxSender(persona)
    bot._api_inbox_sender = sender
    rm = bot.reminder_manager
    rm.set_sender(sender)
    rm.set_memory(bot.memory)
    # Текст напоминаний генерируется LLM в характере персоны (как в TG)
    rm.set_router_persona(bot.router, bot.persona)
    # Заморозка персоны: напоминания молчат (флаг читается живьём из bot.features)
    rm.set_muted_check(lambda: bool((bot.features or {}).get("muted")))
    loop = background_loop()
    loop.call_soon_threadsafe(rm.start, loop)


def wire_rhythm_for_api(persona: str, bot, sender=None) -> None:
    """Подключает rhythm-менеджер (утро/ночь/погода) к веб-inbox и запускает
    его фоновый цикл. Используется при старте бота и при живом включении
    features.rhythm через настройки (без рестарта сервера)."""
    sender = sender or getattr(bot, "_api_inbox_sender", None) or WebInboxSender(persona)
    bot._api_inbox_sender = sender
    if getattr(bot, "rhythm", None) is None:
        bot.setup_rhythm(sender)  # no-op, если фича выключена в YAML
    rm = getattr(bot, "rhythm", None)
    if rm is None:
        return
    rm.set_sender(sender)
    loop = background_loop()
    loop.call_soon_threadsafe(rm.start, loop)


def start_bot_features(persona: str, bot):
    """Подключает inbox-sender и запускает фоновые циклы бота (идемпотентно)."""
    if getattr(bot, "_api_features_started", False):
        return
    bot._api_features_started = True

    sender = WebInboxSender(persona)
    bot._api_inbox_sender = sender
    loop = background_loop()

    if bot._activity_tracker is not None:
        bot.setup_proactive(sender)
    wire_reminder_for_api(persona, bot, sender=sender)
    if bot.learning_manager is not None:
        bot.setup_learning(sender)
    wire_rhythm_for_api(persona, bot, sender=sender)

    def _start_all():
        if bot.proactive is not None:
            bot.proactive.start(loop=loop)
        if bot.learning_manager is not None:
            bot.learning_manager.start(loop=loop)
        if bot.living is not None:
            bot.living.start(loop=loop)

    loop.call_soon_threadsafe(_start_all)
    logger.info(f"[api] Фоновые циклы запущены для {persona}")

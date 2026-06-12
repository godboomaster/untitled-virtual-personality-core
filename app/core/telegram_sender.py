"""
Реализация MessageSender для Telegram Bot API.
"""

import logging
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from app.core.interfaces import MessageSender

logger = logging.getLogger(__name__)


class TelegramMessageSender:
    """Отправка сообщений через Telegram Bot API."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        topic_id: Optional[int] = None,
    ) -> bool:
        try:
            # Длинные сообщения (>100 символов) отправляем в сворачиваемой цитате
            if len(text) > 100:
                # Экранируем HTML внутри текста перед оборачиванием
                safe_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                text = f"<blockquote expandable>{safe_text}</blockquote>"
                parse_mode = ParseMode.HTML
            else:
                parse_mode = None

            kwargs = {
                "chat_id": int(chat_id),
                "text": text,
                "parse_mode": parse_mode,
            }
            if topic_id:
                kwargs["message_thread_id"] = topic_id

            await self._bot.send_message(**kwargs)
            return True
        except TelegramError as e:
            logger.error(f"[TelegramSender] Ошибка отправки в {chat_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"[TelegramSender] Неожиданная ошибка при отправке в {chat_id}: {e}")
            return False

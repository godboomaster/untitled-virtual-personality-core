"""
Реализация MessageSender для Telegram Bot API.
"""

import html
import logging
import re
from typing import Optional

from telegram import Bot, InputFile
from telegram.constants import ParseMode
from telegram.error import TelegramError

from app.core.interfaces import MessageSender

logger = logging.getLogger(__name__)

# ── Конвертация Markdown персоны → HTML для Telegram ──
#
# Персона генерирует обычный GFM-подобный Markdown (**bold**, *italic*, `code`, ```блоки```,
# [текст](url)). Родные Markdown-парсеры Telegram сюда не годятся:
#   - legacy "Markdown": жирный — ОДНА звёздочка, а не две — синтаксис персоны не совпадает;
#   - "MarkdownV2": требует ручного экранирования кучи спецсимволов, которые LLM никогда
#     не экранирует, из-за чего Telegram падает с "can't parse entities" на любой скобке/точке.
# Поэтому конвертируем в HTML сами — это единственный режим, где можно сначала безопасно
# экранировать текст, а потом расставить теги, не рискуя парс-ошибками.
_CODE_BLOCK_RE = re.compile(r"```(?:\w+\n)?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+?)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.DOTALL)


def markdown_to_telegram_html(text: str) -> str:
    """Конвертирует Markdown персоны в HTML, понятный Telegram (parse_mode=HTML)."""
    if not text:
        return text

    # 1. Код прячем ДО общего экранирования — иначе спецсимволы внутри кода
    #    заэкранируются, а сам код ещё и попадёт под правила bold/italic ниже.
    placeholders = []

    def _stash(html_fragment: str) -> str:
        placeholders.append(html_fragment)
        return f"\x00{len(placeholders) - 1}\x00"

    text = _CODE_BLOCK_RE.sub(lambda m: _stash(f"<pre>{html.escape(m.group(1).strip())}</pre>"), text)
    text = _INLINE_CODE_RE.sub(lambda m: _stash(f"<code>{html.escape(m.group(1))}</code>"), text)

    # 2. Экранируем HTML-спецсимволы в остальном тексте
    text = html.escape(text, quote=False)

    # 3. Ссылки, жирный (сначала **, иначе одиночные * растащат пару на части), курсив
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)

    # 4. Возвращаем код-блоки на место
    for i, fragment in enumerate(placeholders):
        text = text.replace(f"\x00{i}\x00", fragment)

    return text


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
        parse_mode: Optional[str] = None,
    ) -> bool:
        """
        Отправка сообщений через Telegram Bot API.

        Параметр parse_mode позволяет указать форматирование:
        - None — обычный текст (экранирование HTML)
        - "HTML" — HTML-форматирование
        - "Markdown" — Markdown-форматирование

        Когда python-telegram-bot обновится до поддержки Bot API 10.1,
        можно будет использовать send_rich_message() с InputRichMessage.
        """
        try:
            # Если parse_mode не указан явно — считаем, что text написан персоной в Markdown,
            # и конвертируем его в HTML сами (см. markdown_to_telegram_html выше).
            if parse_mode is None:
                html_text = markdown_to_telegram_html(text)
                if len(text) > 100:
                    html_text = f"<blockquote expandable>{html_text}</blockquote>"
                text = html_text
                parse_mode = ParseMode.HTML

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

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        filename: str,
        *,
        caption: Optional[str] = None,
        topic_id: Optional[int] = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        """Отправляет файл (документ) в чат через Telegram Bot API."""
        try:
            kwargs = {"chat_id": int(chat_id)}
            if topic_id:
                kwargs["message_thread_id"] = topic_id
            if caption:
                # Как и в send_message: без явного parse_mode считаем caption Markdown'ом
                # персоны и конвертируем в HTML — иначе разметка в подписи к файлу
                # (что и было в баг-репорте) уходит буквальными звёздочками.
                if parse_mode is None:
                    kwargs["caption"] = markdown_to_telegram_html(caption)
                    kwargs["parse_mode"] = ParseMode.HTML
                else:
                    kwargs["caption"] = caption
                    kwargs["parse_mode"] = parse_mode

            with open(file_path, "rb") as f:
                kwargs["document"] = InputFile(f, filename=filename)
                await self._bot.send_document(**kwargs)
            return True
        except TelegramError as e:
            logger.error(f"[TelegramSender] Ошибка отправки файла в {chat_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"[TelegramSender] Неожиданная ошибка при отправке файла в {chat_id}: {e}")
            return False

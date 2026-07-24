"""
Реализация MessageSender для Telegram Bot API.
"""

import html
import logging
import re
from typing import Dict, List, Optional

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

# Лимит подписи (caption) к документу/медиа в Telegram Bot API.
_CAPTION_LIMIT = 1024

# Лимит текста сообщения в Telegram Bot API.
_MESSAGE_LIMIT = 4096

# Размер части при разбиении длинных сообщений — с запасом под HTML-экранирование
# (& → &amp; и т.п.) и обёртку-blockquote при конвертации Markdown → HTML.
_TELEGRAM_TEXT_LIMIT = 3500


def _truncate_plain(text: str, limit: int) -> str:
    """Обрезает текст по лимиту с многоточием (для plain-text деградации)."""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _split_for_telegram(text: str, limit: int = _TELEGRAM_TEXT_LIMIT) -> List[str]:
    """Делит длинный текст на части ≤ limit, предпочитая границу абзаца, затем строки,
    затем слова. Режется ИСХОДНЫЙ текст (не HTML — иначе порвём теги)."""
    parts: List[str] = []
    rest = (text or "").strip()
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


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
        # message_id последнего успешно отправленного сообщения ПО ЧАТАМ (для опционального
        # чтения вызывающим кодом — например, чтобы зарегистрировать его как «вопрос бота»).
        # Per-chat: отправки идут конкурентно из разных чатов, и общий атрибут отдавал
        # чужой message_id — reply-to-логика обучения регистрировала бы не то сообщение.
        self._last_sent_message_ids: Dict[str, int] = {}

    def get_last_sent_message_id(self, chat_id: str) -> Optional[int]:
        """message_id последнего успешно отправленного сообщения в этот чат (или None)."""
        return self._last_sent_message_ids.get(str(chat_id))

    async def _send_one(self, chat_id: str, text: str, parse_mode: Optional[str],
                        topic_id: Optional[int]) -> None:
        """Одна отправка bot.send_message + фиксация message_id последнего сообщения."""
        kwargs = {
            "chat_id": int(chat_id),
            "text": text,
            "parse_mode": parse_mode,
        }
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        sent = await self._bot.send_message(**kwargs)
        # Сохраняем message_id для вызывающего кода (опционально) — по чату.
        msg_id = getattr(sent, "message_id", None)
        if msg_id is not None:
            self._last_sent_message_ids[str(chat_id)] = msg_id

    async def _send_long_html(self, chat_id: str, text: str, topic_id: Optional[int],
                              limit: int) -> None:
        """Отправляет длинный markdown-текст частями под лимит Telegram.
        Режем ИСХОДНЫЙ текст (не HTML — порвём теги), каждую часть конвертируем
        отдельно. Запаса лимита может не хватить: плотная разметка (code/bold-спаны)
        раздувает текст тегами сильнее, чем на треть — тогда часть рекурсивно режется
        мельче. Совсем патологические куски (сплошные спецсимволы) уходят plain text'ом:
        потерять форматирование лучше, чем потерять сообщение."""
        for part in _split_for_telegram(text, limit):
            part_html = markdown_to_telegram_html(part)
            if len(part) > 100:
                part_html = f"<blockquote expandable>{part_html}</blockquote>"
            if len(part_html) <= _MESSAGE_LIMIT:
                await self._send_one(chat_id, part_html, ParseMode.HTML, topic_id)
            elif limit > 800:
                await self._send_long_html(chat_id, part, topic_id, limit // 2)
            else:
                await self._send_one(chat_id, part, None, topic_id)

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

        Текст, не влезающий в лимит Telegram (4096 после конвертации в HTML),
        разбивается на части и отправляется несколькими сообщениями — раньше такой
        текст Telegram просто отклонял, и он молча терялся.

        После успешной отправки сохраняет message_id последней части
        (доступен через get_last_sent_message_id(chat_id)).
        """
        try:
            # Явный parse_mode — одна отправка, контроль длины на вызывающем коде.
            if parse_mode is not None:
                await self._send_one(chat_id, text, parse_mode, topic_id)
                return True

            # Без явного parse_mode считаем text Markdown'ом персоны и конвертируем
            # в HTML сами (см. markdown_to_telegram_html выше).
            html_text = markdown_to_telegram_html(text)
            if len(text) > 100:
                html_text = f"<blockquote expandable>{html_text}</blockquote>"
            if len(html_text) <= _MESSAGE_LIMIT:
                await self._send_one(chat_id, html_text, ParseMode.HTML, topic_id)
                return True

            # Не влезает в лимит — режем исходный текст (не HTML, чтобы не порвать
            # теги) и шлём частями.
            logger.info(
                f"[TelegramSender] Длинное сообщение ({len(text)} симв.) — "
                f"отправляю частями в {chat_id}"
            )
            await self._send_long_html(chat_id, text, topic_id, _TELEGRAM_TEXT_LIMIT)
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
                    html_caption = markdown_to_telegram_html(caption)
                    if len(html_caption) <= _CAPTION_LIMIT:
                        kwargs["caption"] = html_caption
                        kwargs["parse_mode"] = ParseMode.HTML
                    else:
                        # Подпись после HTML-экранирования не влезает в лимит Telegram
                        # (1024) — длинная подпись урока с контрольными вопросами легко
                        # за него выходит. Обрезать HTML нельзя (битые теги → ошибка
                        # парсинга), поэтому деградируем до plain text по лимиту:
                        # подпись без форматирования лучше, чем недоставленный файл.
                        kwargs["caption"] = _truncate_plain(caption, _CAPTION_LIMIT)
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

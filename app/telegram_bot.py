"""
Единый Telegram-бот — создаёт handlers для одного BotInstance.
main.py запускает два Telegram Application, каждый со своими handlers.
"""

import asyncio
import logging
import re
import time
from typing import Optional
from telegram import Update, BotCommand, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from app.bot_instance import BotInstance
from app.core.users import register_user, get_user_display, get_user_tag
from app.core.telegram_sender import TelegramMessageSender
from app.features.file_sender import prepare_response, cleanup_files
from app.features.reply_context import extract_reply_context

logger = logging.getLogger(__name__)


# ─── Markdown в HTML ──────────────────────────────────────

from app.core.rich_message_formatter import RichMessageFormatter

_rich_formatter = RichMessageFormatter()

def _md_to_html(text: str) -> str:
    """
    Конвертирует Markdown в HTML для Telegram.
    Использует RichMessageFormatter для поддержки новых тегов:
    - <tg-spoiler> — спойлеры
    - <u> — подчеркивание
    - <sub>, <sup> — индексы
    - <mark> — выделение
    """
    return _rich_formatter.to_current_html(text)


async def _reply_ai(message, text: str):
    # Анализируем ответ — нужны ли файлы
    try:
        msg_parts, files = prepare_response(text)
    except Exception as e:
        logger.error(f"Ошибка в prepare_response: {e}", exc_info=True)
        msg_parts = [text[:3900] + "\n\n[ Ответ слишком длинный — ошибка при обработке]"]
        files = None

    # Отправляем текстовые сообщения (может быть несколько частей)
    for part in msg_parts:
        if not part or not part.strip():
            continue
        try:
            html = _md_to_html(part)
            # Сворачиваем в expandable blockquote для единообразия
            html = f"<blockquote expandable>{html}</blockquote>"
            await message.reply_text(html, parse_mode="HTML")
        except Exception:
            try:
                await message.reply_text(part)
            except Exception:
                pass

    # Отправляем файлы (код)
    if files:
        for filepath, filename in files:
            try:
                with open(filepath, 'rb') as f:
                    await message.reply_document(
                        document=InputFile(f, filename=filename),
                        caption=f"{filename}"
                    )
            except Exception as e:
                logger.error(f"Ошибка отправки файла {filename}: {e}")
        cleanup_files(files)


# ─── Создание handlers для конкретного BotInstance ────────

def create_handlers(bot: BotInstance) -> dict:
    """
    Возвращает словарь handler-функций для данного BotInstance.
    Каждая функция — замыкание над bot.
    """
    persona_name = bot.persona_name
    features = bot.features

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = str(user.id)
        display_name = get_user_display(user_id)
        logger.info(f"[{persona_name}] /start от {user_id} ({display_name})")

        if persona_name == "connor":
            greeting = f"Привет, {display_name}. Я — Коннор, андроид модели RK800.\nОбратись ко мне по имени — и я помогу."
        elif persona_name == "arrodes":
            greeting = f"Привет, {display_name}. Я — Великий Арродес, древнее зеркало с Моря Хаоса.\nОбратись ко мне по имени — и я отвечу, если сочту нужным."
        else:
            greeting = f"Привет, {display_name}. Я — {bot.persona.persona_data.get('name', persona_name)}."
        await update.message.reply_text(greeting)

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        lines = [
            "Доступные команды:",
            "/start — начать диалог",
            "/help — эта справка",
            "/stats — статистика памяти",
            "/last N — последние N сообщений (10 по умолчанию)",
            "/erase N — удалить последние N из STM",
            "/reset — сбросить память текущего пользователя",
            "/resetall — сбросить память всех пользователей",
        ]
        if bot.file_db:
            lines.append("/files — список загруженных файлов")
            lines.append("/reset_files — сбросить файловую базу")
            lines.append("\nОтправьте файл — я прочитаю и сохраню!")
        if bot.todo_manager:
            lines.append("/todo — показать список дел чата")
            lines.append("/reminders — активные напоминания")
            lines.append("/cancel_reminder N — отменить напоминание №N")
        if bot.inventory_manager:
            lines.append("/inventory — показать инвентарь бота")
        await update.message.reply_text("\n".join(lines))

    async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        user_id = str(update.effective_user.id)
        s = bot.get_memory_stats(user_id=user_id, chat_id=chat_id)
        text = (
            f"Статистика памяти:\n"
            f"  Краткосрочная: {s['stm_count']}/{s['stm_max']} сообщений\n"
            f"  Долгосрочная: {s['ltm_count']} фактов"
        )
        await update.message.reply_text(text)

    async def erase_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Удалить последние N сообщений из STM (deque + ChromaDB)."""
        import os
        owner_id = os.getenv("OWNER_USER_ID", "")
        if str(update.effective_user.id) != owner_id:
            return

        chat_id = str(update.effective_chat.id)

        if not context.args:
            await update.message.reply_text("Использование: /erase N (число)")
            return

        try:
            n = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Использование: /erase N (число)")
            return
        n = max(1, min(n, 500))

        deleted = bot.stm_pop_last_n(n, chat_id)
        await update.message.reply_text(f"Удалено {deleted} сообщений из STM.")

    async def last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать последние n сообщений из STM (первое предложение)."""
        import os
        owner_id = os.getenv("OWNER_USER_ID", "")
        if str(update.effective_user.id) != owner_id:
            return

        chat_id = str(update.effective_chat.id)
        n = 10
        if context.args:
            try:
                n = int(context.args[0])
            except ValueError:
                await update.message.reply_text("Использование: /last N (число)")
                return
        n = max(1, min(n, 100))

        messages = bot.get_stm_last_display(n, chat_id)
        if not messages:
            await update.message.reply_text("STM пуст.")
            return

        lines = []
        for i, m in enumerate(messages, 1):
            role = m["role"]
            name = m.get("user_name") or ("User" if role == "user" else "Bot")
            content = m["content"]
            tag = f"[{name}]" if role == "user" else "[Bot]"
            lines.append(f"{i}. {tag} {content}")

        await update.message.reply_text("\n".join(lines))

    async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        logger.info(f"[{persona_name}] Reset LTM для {user_id}")
        bot.clear_ltm_only(user_id=user_id)
        s = bot.get_memory_stats(user_id=user_id)
        await update.message.reply_text(f"Факты сброшены.\nLTM: {s['ltm_count']} фактов")

    async def resetall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        if bot.allowed_dm_users and user_id not in bot.allowed_dm_users:
            return
        bot.clear_all_memory()
        await update.message.reply_text("Вся память сброшена.")

    async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not bot.file_db:
            return
        user_id = str(update.effective_user.id)
        files = bot.file_db.get_loaded_files(user_id)
        if files:
            text = "Загруженные файлы:\n" + "\n".join(f"- {f}" for f in files)
        else:
            text = "Нет загруженных файлов."
        await update.message.reply_text(text)

    async def reset_files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not bot.file_db:
            return
        user_id = str(update.effective_user.id)
        bot.file_db.reset(user_id=user_id)
        await update.message.reply_text("Файловая база очищена.")

    async def ratelimits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not bot._rate_limit_enabled:
            return
        import os
        owner_id = os.getenv("OWNER_USER_ID", "")
        if str(update.effective_user.id) != owner_id:
            return
        text = bot.get_rate_limit_status()
        await update.message.reply_text(text)

    async def web_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        enabled = bot.toggle_web_search(chat_id)
        if enabled:
            await update.message.reply_text("Веб-поиск включён.")
        else:
            await update.message.reply_text("Веб-поиск выключен.")

    async def todo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not bot.todo_manager:
            await update.message.reply_text("Список дел не активен для этой персоны.")
            return
        chat_id = str(update.effective_chat.id)
        todo_list = bot.todo_manager.get_list(chat_id)
        await update.message.reply_text(todo_list or "Список дел пуст.")

    async def reminders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not bot.reminder_manager:
            await update.message.reply_text("Напоминания не активны для этой персоны.")
            return
        chat_id = str(update.effective_chat.id)
        active = bot.reminder_manager.get_active(chat_id)
        if not active:
            await update.message.reply_text("Активных напоминаний нет.")
            return
        from datetime import datetime
        lines = ["Активные напоминания:"]
        for i, r in enumerate(active):
            remain = r["trigger_at"] - time.time()
            task = r.get("task") or "(без описания)"
            mins = int(remain / 60)
            if mins > 0:
                when = f"через {mins} мин"
            else:
                when = f"через {int(remain)} сек"
            lines.append(f"{i + 1}. {task} — {when}")
        lines.append("")
        lines.append("Чтобы отменить: /cancel_reminder N")
        await update.message.reply_text("\n".join(lines))

    async def cancel_reminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not bot.reminder_manager:
            return
        chat_id = str(update.effective_chat.id)
        if not context.args:
            await update.message.reply_text("Использование: /cancel_reminder N (номер из /reminders)")
            return
        try:
            idx = int(context.args[0]) - 1
        except ValueError:
            await update.message.reply_text("Нужно число — номер напоминания из /reminders.")
            return
        if bot.reminder_manager.cancel_reminder(chat_id, idx):
            await update.message.reply_text("Напоминание отменено.")
        else:
            await update.message.reply_text("Напоминание с таким номером не найдено.")

    async def inventory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not bot.inventory_manager:
            await update.message.reply_text("Инвентарь не активен для этой персоны.")
            return
        inv_list = bot.inventory_manager.get_list_text()
        await update.message.reply_text(inv_list)

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = str(user.id)
        chat_id = str(update.effective_chat.id)
        text = update.message.text

        # Регистрируем пользователя
        register_user(user_id, user.first_name or user.username or f"User_{user_id}", user.username)

        logger.info(f"[{persona_name}] [MSG] chat={chat_id} user={user_id} text='{text[:80]}'")

        # Логируем информацию о топике (для отладки)
        message_thread_id = getattr(update.message, "message_thread_id", None)
        is_topic_message = getattr(update.message, "is_topic_message", False)
        if message_thread_id:
            logger.info(f"[{persona_name}] [TOPIC] chat={chat_id} thread_id={message_thread_id} is_topic={is_topic_message}")
            # Сохраняем топик для proactive messaging
            bot.record_topic(chat_id, message_thread_id)

        if text.startswith("/"):
            return

        # Reply to bot?
        is_reply_to_bot = False
        reply_ctx = None
        if update.message.reply_to_message:
            replied = update.message.reply_to_message
            if replied.from_user and replied.from_user.id == context.bot.id:
                is_reply_to_bot = True
            else:
                reply_ctx = extract_reply_context(update, context.bot.id)

        # Записываем активность ТОЛЬКО если сообщение адресовано боту
        # (reply боту, trigger word, или личный чат)
        is_private = update.effective_chat.type == "private"
        is_addressed_to_bot = is_reply_to_bot or bot.should_respond(text) or is_private
        if is_addressed_to_bot:
            bot.record_activity(chat_id)

        # Trigger
        if not bot.should_respond(text) and not is_reply_to_bot:
            return

        # Pre-check (rate limit, moderation, punish)
        is_private = update.effective_chat.type == "private"
        check = bot.pre_check(user_id, text, is_private)
        if check:
            if check == "MODERATION_BLOCKED":
                await _reply_ai(update.message, "*Удар молнии.* Сеанс окончен.")
            return

        # Strip trigger
        clean_text = bot.strip_trigger(text)
        if not clean_text or not clean_text.strip():
            clean_text = text

        logger.info(f"[{persona_name}] Обработка от {user_id}: {clean_text[:60]}...")

        # Предварительное сообщение (для Арродеса)
        if persona_name == "arrodes":
            try:
                await update.message.reply_text("Поверхность зеркала потемнела...")
            except Exception:
                return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        try:
            user_name = user.first_name or user.username or f"User_{user_id}"
            user_tag = get_user_tag(user_id)
            response = await asyncio.to_thread(
                bot.process_message, clean_text,
                user_id=user_id, chat_id=chat_id,
                user_name=user_tag if chat_id != user_id else user_name,
                reply_context=reply_ctx
            )
            logger.info(f"[{bot.router.get_provider_model_info()}] [{persona_name}] Ответ получен ({len(response)} символов)")
            await _reply_ai(update.message, response)

            # Отправляем списки дел/инвентарь отдельными сообщениями
            pending = bot._pending_list_messages
            for msg in pending:
                await _reply_ai(update.message, msg)
        except Exception as e:
            logger.error(f"[{persona_name}] Ошибка: {e}", exc_info=True)
            try:
                await update.message.reply_text("Произошла ошибка. Попробуйте позже.")
            except Exception:
                pass

    async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not bot.file_db:
            return

        caption = update.message.caption or ""

        # Reply на сообщение бота с файлом — тоже обрабатываем
        is_reply_to_bot = False
        if update.message.reply_to_message:
            replied = update.message.reply_to_message
            if replied.from_user and replied.from_user.id == context.bot.id:
                is_reply_to_bot = True

        if not bot.should_respond(caption) and not is_reply_to_bot:
            return

        user = update.effective_user
        user_id = str(user.id)
        chat_id = str(update.effective_chat.id)
        document = update.message.document

        caption_clean = bot.strip_trigger(caption)

        if document.file_size and document.file_size > bot.max_file_size:
            await update.message.reply_text(f"Файл слишком большой (макс. {bot.max_file_size // 1024 // 1024} МБ)")
            return

        logger.info(f"[{persona_name}] Файл от {user_id}: {document.file_name}")

        file = await context.bot.get_file(document.file_id)
        file_bytes = await file.download_as_bytearray()

        await update.message.reply_text("Читаю файл...")

        from app.core.file_reader import extract_text
        text = extract_text(bytes(file_bytes), document.file_name)

        if text.startswith(("Ошибка", "Формат", "Не удалось", "Библиотека")):
            await update.message.reply_text(text)
            return

        bot.file_db.add_file(user_id=user_id, filename=document.file_name, content=text)

        loaded_files = bot.file_db.get_loaded_files(user_id)
        files_note = f"Загружено файлов: {len(loaded_files)}/3"
        message_with_file = f"Пользователь отправил файл '{document.file_name}'. {files_note}:\n\n{text}"
        if caption_clean:
            message_with_file = f"{caption_clean}\n\n{message_with_file}"

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        try:
            user_tag = get_user_tag(user_id)
            response = await asyncio.to_thread(
                bot.process_message, message_with_file,
                user_id=user_id, chat_id=chat_id,
                user_name=user_tag
            )
            logger.info(f"[{bot.router.get_provider_model_info()}] [{persona_name}] Ответ получен ({len(response)} символов)")
            await _reply_ai(update.message, response)
        except Exception as e:
            logger.error(f"[{persona_name}] Ошибка файла: {e}", exc_info=True)
            await update.message.reply_text("Произошла ошибка при обработке файла.")

    async def reset_diary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not bot.self_memory:
            await update.message.reply_text("Личный дневник не активен для этой персоны.")
            return
        user_id = str(update.effective_user.id)
        if bot.allowed_dm_users and user_id not in bot.allowed_dm_users:
            return
        bot.self_memory.clear_all()
        logger.info(f"[{persona_name}] /reset_diary от {user_id}")
        await update.message.reply_text("Дневник полностью очищен. Эпизоды, архив и наблюдения удалены.")

    # Собираем все handlers
    return {
        "start": start,
        "help": help_cmd,
        "stats": stats_cmd,
        "erase": erase_cmd,
        "last": last_cmd,
        "reset": reset_cmd,
        "resetall": resetall_cmd,
        "reset_diary": reset_diary_cmd if bot.self_memory else None,
        "files": files_cmd if bot.file_db else None,
        "reset_files": reset_files_cmd if bot.file_db else None,
        "ratelimits": ratelimits_cmd if bot._rate_limit_enabled else None,
        "web": web_cmd if bot._web_search_enabled else None,
        "todo": todo_cmd if bot.todo_manager else None,
        "reminders": reminders_cmd if bot.reminder_manager else None,
        "cancel_reminder": cancel_reminder_cmd if bot.reminder_manager else None,
        "inventory": inventory_cmd if bot.inventory_manager else None,
        "handle_message": handle_message,
        "handle_document": handle_document if bot.file_db else None,
    }


def register_handlers(app: Application, bot: BotInstance):
    # Регистрирует все handlers для данного Application.
    h = create_handlers(bot)
    persona_name = bot.persona_name

    # Создаем sender и инициализируем proactive messaging
    if bot._activity_tracker:
        sender = TelegramMessageSender(bot=app.bot)
        bot.setup_proactive(sender)

    # Reminder manager — sender нужен независимо от proactive
    if bot.reminder_manager:
        sender = TelegramMessageSender(bot=app.bot)
        bot.reminder_manager.set_sender(sender)
        bot.reminder_manager.set_router_persona(bot.router, bot.persona)

    app.add_handler(CommandHandler("start", h["start"]))
    app.add_handler(CommandHandler("help", h["help"]))
    app.add_handler(CommandHandler("stats", h["stats"]))
    app.add_handler(CommandHandler("erase", h["erase"]))
    app.add_handler(CommandHandler("last", h["last"]))
    app.add_handler(CommandHandler("reset", h["reset"]))
    app.add_handler(CommandHandler("resetall", h["resetall"]))

    if h.get("reset_diary"):
        app.add_handler(CommandHandler("reset_diary", h["reset_diary"]))

    if h.get("files"):
        app.add_handler(CommandHandler("files", h["files"]))
    if h.get("reset_files"):
        app.add_handler(CommandHandler("reset_files", h["reset_files"]))
    if h.get("ratelimits"):
        app.add_handler(CommandHandler("ratelimits", h["ratelimits"]))
    if h.get("web"):
        app.add_handler(CommandHandler("web", h["web"]))
    if h.get("todo"):
        app.add_handler(CommandHandler("todo", h["todo"]))
    if h.get("reminders"):
        app.add_handler(CommandHandler("reminders", h["reminders"]))
    if h.get("cancel_reminder"):
        app.add_handler(CommandHandler("cancel_reminder", h["cancel_reminder"]))
    if h.get("inventory"):
        app.add_handler(CommandHandler("inventory", h["inventory"]))

    # Debug
    async def debug_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        if msg:
            logger.info(
                f"[{persona_name}] [DEBUG] msg_id={msg.message_id} "
                f"chat={msg.chat.type} text={repr(getattr(msg, 'text', None))}"
            )
    app.add_handler(MessageHandler(filters.ALL, debug_all), group=-1)

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h["handle_message"]))

    # Documents
    if h.get("handle_document"):
        app.add_handler(MessageHandler(filters.Document.ALL, h["handle_document"]))

    return app

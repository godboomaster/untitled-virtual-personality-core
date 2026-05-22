"""
Единый Telegram-бот — создаёт handlers для одного BotInstance.
main.py запускает два Telegram Application, каждый со своими handlers.
"""

import asyncio
import logging
import re
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
from app.features.file_sender import prepare_response, cleanup_files

logger = logging.getLogger(__name__)


# ─── Markdown → HTML ──────────────────────────────────────

def _md_to_html(text: str) -> str:
    code_blocks = []

    def _save_block(m):
        lang = m.group(1)
        code = m.group(2).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        placeholder = f'\x00CODEBLOCK{len(code_blocks)}\x00'
        if lang:
            code_blocks.append(f'<pre><code class="language-{lang}">{code}</code></pre>')
        else:
            code_blocks.append(f'<pre>{code}</pre>')
        return placeholder

    text = re.sub(r'```(\w*)\n?(.*?)```', _save_block, text, flags=re.DOTALL)

    def _save_inline(m):
        code = m.group(1).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        placeholder = f'\x00CODEBLOCK{len(code_blocks)}\x00'
        code_blocks.append(f'<code>{code}</code>')
        return placeholder

    text = re.sub(r'`([^`]+)`', _save_inline, text)
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    text = re.sub(r'^&gt;\s?(.*)$', r'<blockquote>\1</blockquote>', text, flags=re.MULTILINE)
    text = re.sub(r'</blockquote>\n<blockquote>', '\n', text)
    text = re.sub(r'^### (.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^---+$', '───────────', text, flags=re.MULTILINE)

    for i, block in enumerate(code_blocks):
        text = text.replace(f'\x00CODEBLOCK{i}\x00', block)
    return text


async def _reply_ai(message, text: str):
    # Анализируем ответ — нужен ли файл
    try:
        text_to_send, files = prepare_response(text)
    except Exception as e:
        logger.error(f"Ошибка в prepare_response: {e}", exc_info=True)
        # Fallback: отправляем как есть, но обрезаем до лимита
        text_to_send = text[:3900] + "\n\n[⚠️ Ответ слишком длинный — произошла ошибка при создании файла]"
        files = None

    # Отправляем текстовое сообщение (или краткое описание)
    if text_to_send:
        try:
            html = _md_to_html(text_to_send)
            await message.reply_text(html, parse_mode="HTML")
        except Exception:
            try:
                await message.reply_text(text_to_send)
            except Exception:
                pass

    # Отправляем файлы
    if files:
        for filepath, filename in files:
            try:
                with open(filepath, 'rb') as f:
                    await message.reply_document(
                        document=InputFile(f, filename=filename),
                        caption=f"📄 {filename}"
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
            "/clear — очистить память (устарело)",
            "/reset — сбросить память текущего пользователя",
            "/resetall — сбросить память всех пользователей",
        ]
        if bot.file_db:
            lines.append("/files — список загруженных файлов")
            lines.append("/reset_files — сбросить файловую базу")
            lines.append("\nОтправьте файл — я прочитаю и сохраню!")
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

    async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        user_id = str(update.effective_user.id)
        bot.clear_memory(user_id=user_id, chat_id=chat_id)
        await update.message.reply_text("Память очищена.")

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
        # Только для владельца
        if str(update.effective_user.id) != "734961317":
            return
        text = bot.get_rate_limit_status()
        await update.message.reply_text(text)

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = str(user.id)
        chat_id = str(update.effective_chat.id)
        text = update.message.text

        # Регистрируем пользователя
        register_user(user_id, user.first_name or user.username or f"User_{user_id}", user.username)

        logger.info(f"[{persona_name}] [MSG] chat={chat_id} user={user_id} text='{text[:80]}'")

        if text.startswith("/"):
            return

        # Reply to bot?
        is_reply_to_bot = False
        if update.message.reply_to_message:
            replied = update.message.reply_to_message
            if replied.from_user and replied.from_user.id == context.bot.id:
                is_reply_to_bot = True

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
                user_name=user_tag if chat_id != user_id else user_name
            )
            logger.info(f"[{persona_name}] Ответ получен ({len(response)} символов)")
            await _reply_ai(update.message, response)
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
        if not bot.should_respond(caption):
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
            logger.info(f"[{persona_name}] Ответ получен ({len(response)} символов)")
            await _reply_ai(update.message, response)
        except Exception as e:
            logger.error(f"[{persona_name}] Ошибка файла: {e}", exc_info=True)
            await update.message.reply_text("Произошла ошибка при обработке файла.")

    # Собираем все handlers
    return {
        "start": start,
        "help": help_cmd,
        "stats": stats_cmd,
        "clear": clear_cmd,
        "reset": reset_cmd,
        "resetall": resetall_cmd,
        "files": files_cmd if bot.file_db else None,
        "reset_files": reset_files_cmd if bot.file_db else None,
        "ratelimits": ratelimits_cmd if bot._rate_limit_enabled else None,
        "handle_message": handle_message,
        "handle_document": handle_document if bot.file_db else None,
    }


def register_handlers(app: Application, bot: BotInstance):
    """Регистрирует все handlers для данного Application."""
    h = create_handlers(bot)
    persona_name = bot.persona_name

    app.add_handler(CommandHandler("start", h["start"]))
    app.add_handler(CommandHandler("help", h["help"]))
    app.add_handler(CommandHandler("stats", h["stats"]))
    app.add_handler(CommandHandler("clear", h["clear"]))
    app.add_handler(CommandHandler("reset", h["reset"]))
    app.add_handler(CommandHandler("resetall", h["resetall"]))

    if h.get("files"):
        app.add_handler(CommandHandler("files", h["files"]))
    if h.get("reset_files"):
        app.add_handler(CommandHandler("reset_files", h["reset_files"]))
    if h.get("ratelimits"):
        app.add_handler(CommandHandler("ratelimits", h["ratelimits"]))

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

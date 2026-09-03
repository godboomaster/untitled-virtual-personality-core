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
from app.core.message_pacing import send_delay as _send_delay
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
    """Отправляет ответ бота. Возвращает список message_id отправленных текстовых сообщений
    (нужно, чтобы регистрировать сообщения-вопросы для reply-to-логики обучения)."""
    sent_message_ids = []
    # Анализируем ответ — нужны ли файлы
    try:
        msg_parts, files = prepare_response(text)
    except Exception as e:
        logger.error(f"Ошибка в prepare_response: {e}", exc_info=True)
        msg_parts = [text[:3900] + "\n\n[ Ответ слишком длинный — ошибка при обработке]"]
        files = None

    # Отправляем текстовые сообщения (может быть несколько частей)
    for i, part in enumerate(msg_parts):
        if not part or not part.strip():
            continue
        if i > 0:
            # Между частями — пауза «набора»: растёт с длиной следующей части
            try:
                await message.chat.send_action("typing")
            except Exception:
                pass
            await asyncio.sleep(_send_delay(part))
        try:
            html = _md_to_html(part)
            # Сворачиваем в expandable blockquote для единообразия
            html = f"<blockquote expandable>{html}</blockquote>"
            sent = await message.reply_text(html, parse_mode="HTML")
            if getattr(sent, "message_id", None):
                sent_message_ids.append(sent.message_id)
        except Exception:
            try:
                sent = await message.reply_text(part)
                if getattr(sent, "message_id", None):
                    sent_message_ids.append(sent.message_id)
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

    return sent_message_ids


async def _send_split_parts(bot, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: str) -> list:
    """Досылка расщеплённого ответа (settings.split_messages): хвост частей,
    оставшийся от process_message/command_reply в pending-бакете, уходит
    отдельными сообщениями — с «печатает…» и паузой между ними, как человек,
    шлющий несколько сообщений подряд. Возвращает list отправленных message_id
    (регистрируются как обычные реплики бота — для reply-to-логики обучения)."""
    sent_ids = []
    for part in bot.pop_pending_split_messages(chat_id):
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            await asyncio.sleep(_send_delay(part))
            sent_ids.extend(await _reply_ai(update.message, part))
        except Exception as e:
            logger.error(f"Ошибка досылки части расщеплённого ответа: {e}")
    return sent_ids


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
        import os
        user_id = str(update.effective_user.id)
        is_owner = user_id in {bot.owner, os.getenv("OWNER_USER_ID", "")}

        lines = [
            "═══ ЧТО Я УМЕЮ ═══",
            "",
            "💬 Общение",
            "— Отвечаю на trigger-слово, reply на моё сообщение или в личке.",
            "— Помню контекст разговора (краткосрочная память) и факты о тебе (долгосрочная).",
            "— В группе вижу факты участников, сказанные публично в этом чате; личное из ЛС туда не попадает.",
            "— «Зови меня X» — буду обращаться как просишь.",
            "— Поправь меня («запомни: …», «не так…») — сохраню как правило и буду соблюдать всегда.",
            "— Могу написать сам, если ты долго молчишь (проактивные сообщения).",
            "— Веду личный дневник наблюдений и использую его в разговоре.",
            "— Reply на конкретное моё сообщение — вижу, на какое именно.",
            "— Работаю даже без облачных LLM — на локальной модели.",
        ]
        if bot._web_search_enabled:
            lines.append("— Ищу в интернете, когда ответа нет в памяти (/web — вкл/выкл).")
        if bot.file_db:
            lines.append(f"— Читаю файлы (до {bot.file_db.max_docs} шт): пересказ, поиск по содержимому.")
        lines.append("— Понимаю изображения: вытащу текст и опишу, что на картинке.")

        lines += [
            "",
            "═══ КОМАНДЫ ═══",
            "",
            "🧠 Память",
            "/stats — статистика памяти",
            "/reset — сбросить мои факты о тебе",
            "/forget <что> — забыть конкретный факт",
            "/ltm_privacy [smart|strict] — приватность памяти: smart — публичный профиль доступен везде, strict — в каждом чате с нуля",
            "/ltm_export — выгрузить твою память файлом (в личку)",
            "/relations — связи участников чата",
            "/last N — последние N сообщений этого чата",
            "/context — какой контекст уходит в промпт",
        ]
        if bot._rate_limit_enabled:
            lines.append("/ratelimits — статистика лимитов")
        if bot.todo_manager:
            lines += [
                "",
                "📝 Дела",
                "/add_todo <задача> — добавить дело",
                "/todo — список дел чата",
                "Или просто: «запиши …», «надо сделать …», «готово, вычеркни N».",
            ]
        if bot.reminder_manager:
            lines += [
                "",
                "⏰ Напоминания",
                "/remind <что> [через N …] — напомнить",
                "/reminders — активные напоминания",
                "/cancel_reminder N — отменить №N",
                "Или просто: «напомни через час …».",
            ]
        if bot.inventory_manager:
            lines += [
                "",
                "🎒 Инвентарь",
                "/add_inventory <предмет>[: описание] — дать мне предмет",
                "/inventory — что у меня есть",
                "Или просто: «держи кофе», «возьми ключ» — описание и срок годности придумаю сам.",
            ]
        if bot.learning_manager:
            lines += [
                "",
                "🎓 Обучение",
                "/learn <тема> — курс с уроками по расписанию и тестами",
                "Или просто: «научи меня …», «хочу выучить …».",
            ]
        if bot.file_db:
            lines += [
                "",
                "📎 Файлы",
                "/files — загруженные файлы",
                "/reset_files — очистить файловую базу",
            ]
        if bot._web_search_enabled:
            lines.append("/web — вкл/выкл веб-поиск в этом чате")
        if bot.self_memory and is_owner:
            lines.append("/reset_diary — очистить мой дневник (owner)")

        if is_owner:
            lines += [
                "",
                "👑 Owner",
                "/erase N — удалить последние N сообщений STM",
                "/resetall — стереть ВСЮ память бота",
            ]

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
            time_str = f" {m['time']}" if m.get("time") else ""
            lines.append(f"{i}.{time_str} {tag} {content}")

        await update.message.reply_text("\n".join(lines))

    async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        logger.info(f"[{persona_name}] Reset LTM для {user_id}")
        bot.clear_ltm_only(user_id=user_id)
        s = bot.get_memory_stats(user_id=user_id)
        await update.message.reply_text(f"Факты сброшены.\nLTM: {s['ltm_count']} фактов")

    async def forget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Точечное забывание: /forget <что забыть> — удаляет самый похожий факт."""
        user_id = str(update.effective_user.id)
        raw = update.message.text or ""
        args = raw.split(" ", 1)[1].strip() if " " in raw else ""
        if not args:
            await update.message.reply_text("Использование: /forget <что забыть, например: сон>")
            return
        forgotten = await asyncio.to_thread(bot.forget_fact, args, user_id)
        if forgotten:
            await update.message.reply_text(f"Забыл: «{forgotten}»")
        else:
            await update.message.reply_text("Не нашёл похожего факта в памяти.")

    async def relations_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Социальный граф: связи участников чата."""
        user_id = str(update.effective_user.id)
        chat_id = str(update.effective_chat.id)
        text = await asyncio.to_thread(bot.get_relations_text, user_id, chat_id)
        await update.message.reply_text(text)

    async def context_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает, какой контекст ушёл бы в промпт — файлом."""
        import os
        import tempfile
        import shutil
        user_id = str(update.effective_user.id)
        chat_id = str(update.effective_chat.id)
        text = await asyncio.to_thread(bot.debug_context, user_id, chat_id)
        tmp_dir = tempfile.mkdtemp(prefix="ctx_")
        path = os.path.join(tmp_dir, "context.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            with open(path, "rb") as f:
                await update.message.reply_document(
                    document=InputFile(f, filename="context.txt"),
                    caption="Контекст, который уходит в промпт.",
                )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def resetall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        import os
        user_id = str(update.effective_user.id)
        if user_id not in {bot.owner, os.getenv("OWNER_USER_ID", "")}:
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
            await update.message.reply_text("Ограничение частоты сообщений выключено для этой персоны.")
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
        lang = bot.chat_user_language(chat_id)
        todo_list = bot.todo_manager.get_list(chat_id, lang=lang)
        empty = "The todo list is empty." if lang == "en" else "Список дел пуст."
        await update.message.reply_text(todo_list or empty)

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
        from app.features.reminder_manager import format_schedule
        lines = ["Активные напоминания:"]
        for i, r in enumerate(active):
            task = r.get("task") or "(без описания)"
            author = r.get("user_name") or ""
            author_text = f" (от {author})" if author else ""
            if r.get("recurrence"):
                when = format_schedule(r["recurrence"])
            else:
                remain = r["trigger_at"] - time.time()
                mins = int(remain / 60)
                when = f"через {mins} мин" if mins > 0 else f"через {int(remain)} сек"
            lines.append(f"{i + 1}. {task}{author_text} — {when}")
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

    # ── слэш-команды как второй способ записи (ответ через LLM в образе персоны) ──

    async def _run_command(update: Update, kind: str, usage: str, manager_attr: str):
        """Общий каркас: проверяет менеджера, парсит аргументы, вызывает _dispatch_command, отвечает."""
        if not getattr(bot, manager_attr):
            await update.message.reply_text("Эта функция не активна для данной персоны.")
            return
        user = update.effective_user
        chat_id = str(update.effective_chat.id)
        user_id = str(user.id)
        user_name = user.first_name or user.username or f"User_{user_id}"
        # Сырой текст после имени команды
        raw = update.message.text or ""
        args = raw.split(" ", 1)[1].strip() if " " in raw else ""
        if not args:
            await update.message.reply_text(usage)
            return
        try:
            response = await asyncio.to_thread(
                bot._dispatch_command, kind, args, chat_id, user_id, user_name
            )
        except Exception as e:
            logger.error(f"[{persona_name}] Ошибка команды /{kind}: {e}", exc_info=True)
            response = "Произошла ошибка. Попробуйте позже."
        sent_ids = await _reply_ai(update.message, response)
        sent_ids += await _send_split_parts(bot, update, context, chat_id)

        # Если команда завершилась вопросом бота (у /learn — «как часто присылать
        # уроки?»), регистрируем его message_id, чтобы reply пользователя на него
        # распознавался как ответ на вопрос — та же логика, что в handle_message.
        question_kind = bot.pop_pending_question_kind(chat_id)
        if sent_ids and bot.learning_manager and question_kind:
            for mid in sent_ids:
                bot.learning_manager.register_question_message(chat_id, mid)

    async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _run_command(update, "remind",
                           "Использование: /remind <что напомнить> [через N ...]", "reminder_manager")

    async def add_todo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _run_command(update, "todo",
                           "Использование: /add_todo <задача>", "todo_manager")

    async def add_inventory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _run_command(update, "inventory",
                           "Использование: /add_inventory <название предмета>[: описание]", "inventory_manager")

    async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _run_command(update, "learn",
                           "Использование: /learn <тема>", "learning_manager")

    # Per-chat блокировки: сообщения ОДНОГО чата обрабатываются последовательно,
    # но разные чаты и slash-команды — параллельно (приложение запущено с
    # concurrent_updates=True). Без этого два быстрых сообщения из одного чата
    # гнались между собой за pending-флаги и порядок в STM.
    _chat_locks: dict = {}

    def _chat_lock(chat_id: str) -> asyncio.Lock:
        lock = _chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            _chat_locks[chat_id] = lock
        return lock

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
        reply_to_bot_message_id = None
        if update.message.reply_to_message:
            replied = update.message.reply_to_message
            if replied.from_user and replied.from_user.id == context.bot.id:
                is_reply_to_bot = True
                reply_to_bot_message_id = replied.message_id
                # Передаём текст СВОЕГО сообщения, на которое ответили, —
                # иначе LLM видела только факт reply, но не на какую реплику
                replied_text = replied.text or replied.caption
                if replied_text:
                    reply_ctx = f"[{persona_name}]: {replied_text[:500]}"
                elif replied.document:
                    reply_ctx = f"[{persona_name}]: [File: {replied.document.file_name or 'unnamed'}]"
            else:
                reply_ctx = extract_reply_context(update, context.bot.id)

        # Записываем активность ТОЛЬКО если сообщение адресовано боту
        # (reply боту, trigger word, или личный чат)
        is_private = update.effective_chat.type == "private"
        is_addressed_to_bot = is_reply_to_bot or bot.should_respond(text) or is_private
        if is_addressed_to_bot:
            bot.record_activity(chat_id)
            # Утреннее приветствие rhythm: первое появление пользователя днём
            bot.note_presence(chat_id)

        # Trigger
        if not bot.should_respond(text) and not is_reply_to_bot:
            return

        # Дальше — конвейер ответа. Сериализуем по чату: slash-команды и другие
        # чаты не ждут LLM, но два сообщения одного чата не перехлёстываются.
        async with _chat_lock(chat_id):
            # Pre-check (rate limit, moderation, punish) — в потоке, т.к. модерация делает синхронный HTTP-запрос
            check = await asyncio.to_thread(bot.pre_check, user_id, text, is_private)
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
                    reply_context=reply_ctx,
                    reply_to_bot_message_id=reply_to_bot_message_id
                )
                logger.info(f"[{bot.router.get_provider_model_info()}] [{persona_name}] Ответ получен ({len(response)} символов)")
                sent_ids = await _reply_ai(update.message, response)
                # Хвост расщеплённого ответа — отдельными сообщениями следом
                sent_ids += await _send_split_parts(bot, update, context, chat_id)

                # Если этот ответ — бот-вопрос (частота уроков/«продолжаем?»), регистрируем его
                # message_id, чтобы потом понять, ответил ли пользователь reply-ом именно на него.
                # Флаг per-chat и одноразовый (pop) — при конкурентных чатах чужой флаг
                # сюда не протечёт.
                question_kind = bot.pop_pending_question_kind(chat_id)
                if sent_ids and bot.learning_manager and question_kind:
                    for mid in sent_ids:
                        bot.learning_manager.register_question_message(chat_id, mid)

                # Отправляем списки дел/инвентарь отдельными сообщениями (per-chat бакет)
                pending = bot.pop_pending_list_messages(chat_id)
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
        # Telegram не гарантирует имя файла у документа
        filename = document.file_name or f"document_{document.file_unique_id}"

        caption_clean = bot.strip_trigger(caption)

        if document.file_size and document.file_size > bot.max_file_size:
            await update.message.reply_text(f"Файл слишком большой (макс. {bot.max_file_size // 1024 // 1024} МБ)")
            return

        logger.info(f"[{persona_name}] Файл от {user_id}: {filename}")

        # Та же per-chat сериализация, что и для текстовых сообщений
        async with _chat_lock(chat_id):
            file = await context.bot.get_file(document.file_id)
            file_bytes = await file.download_as_bytearray()

            await update.message.reply_text("Читаю файл...")

            # markitdown и ChromaDB+эмбеддинги — тяжёлые синхронные вызовы, в поток
            from app.core.file_reader import extract_text
            text = await asyncio.to_thread(extract_text, bytes(file_bytes), filename)

            if text.startswith(("Ошибка", "Формат", "Не удалось", "Библиотека")):
                await update.message.reply_text(text)
                return

            await asyncio.to_thread(bot.file_db.add_file, user_id, filename, text)

            loaded_files = await asyncio.to_thread(bot.file_db.get_loaded_files, user_id)
            files_note = f"Files loaded: {len(loaded_files)}/{bot.file_db.max_docs}"
            message_with_file = f"The user sent a file '{filename}'. {files_note}:\n\n{text}"
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
                logger.info(f"[{bot.router.get_provider_model_info()}] [{persona_name}] Ответ на файл получен ({len(response)} символов)")
                await _reply_ai(update.message, response)
                await _send_split_parts(bot, update, context, chat_id)
            except Exception as e:
                logger.error(f"[{persona_name}] Ошибка файла: {e}", exc_info=True)
                await update.message.reply_text("Произошла ошибка при обработке файла.")

    async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """OCR/описание изображения через локальную vision-модель (gemma в Ollama)."""
        caption = update.message.caption or ""

        # Reply на сообщение бота — тоже обрабатываем
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

        photo = update.message.photo[-1]  # самый большой из предложенных размеров
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())

        caption_clean = bot.strip_trigger(caption)

        await update.message.reply_text("Смотрю на изображение...")

        # Та же per-chat сериализация, что и для текстовых сообщений
        async with _chat_lock(chat_id):
            # Каскад: vision-провайдер основного роутера → локальная gemma
            ocr_text = await asyncio.to_thread(bot.describe_image, image_bytes, caption_clean)
            if not ocr_text:
                if not bot._local_router or not bot._local_router.is_available():
                    await update.message.reply_text(
                        "Сейчас не могу обработать изображение — ни одна vision-модель недоступна."
                    )
                    return
                ocr_text = await asyncio.to_thread(bot._local_router.ocr_image, image_bytes, caption_clean)
            if not ocr_text:
                await update.message.reply_text("Не удалось прочитать изображение.")
                return

            message_with_image = (
                "The user sent an image. Its contents according to the "
                f"vision model:\n{ocr_text}"
            )
            if caption_clean:
                message_with_image = f"{caption_clean}\n\n{message_with_image}"

            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

            try:
                user_tag = get_user_tag(user_id)
                response = await asyncio.to_thread(
                    bot.process_message, message_with_image,
                    user_id=user_id, chat_id=chat_id,
                    user_name=user_tag
                )
                logger.info(f"[{bot.router.get_provider_model_info()}] [{persona_name}] Ответ на изображение получен ({len(response)} символов)")
                await _reply_ai(update.message, response)
                await _send_split_parts(bot, update, context, chat_id)
            except Exception as e:
                logger.error(f"[{persona_name}] Ошибка обработки изображения: {e}", exc_info=True)
                await update.message.reply_text("Произошла ошибка при обработке изображения.")

    async def reset_diary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not bot.self_memory:
            await update.message.reply_text("Личный дневник не активен для этой персоны.")
            return
        user_id = str(update.effective_user.id)
        import os
        if user_id not in {bot.owner, os.getenv("OWNER_USER_ID", "")}:
            return
        bot.self_memory.clear_all()
        logger.info(f"[{persona_name}] /reset_diary от {user_id}")
        await update.message.reply_text("Дневник полностью очищен. Эпизоды, архив и наблюдения удалены.")

    async def ltm_privacy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Переключение режима приватности долгосрочной памяти."""
        user_id = str(update.effective_user.id)
        arg = (context.args[0].lower().strip() if context.args else "")

        if arg in ("smart", "strict"):
            mode = bot.set_ltm_privacy(user_id, arg)
        elif arg:
            await update.message.reply_text("Использование: /ltm_privacy [smart|strict]")
            return
        else:
            mode = bot.get_ltm_privacy(user_id)

        descriptions = {
            "smart": (
                "УМНЫЙ (по умолчанию): публичный профиль (имя, город, хобби, "
                "питомцы...) бот помнит в любом чате; личные темы — только там, "
                "где ты о них рассказал."
            ),
            "strict": (
                "СТРОГИЙ: в каждом чате бот помнит о тебе только то, что было "
                "сказано в этом чате. В новом чате — с чистого листа."
            ),
        }
        prefix = "Режим установлен" if arg else "Текущий режим"
        await update.message.reply_text(
            f"{prefix}: {mode.upper()}\n\n{descriptions[mode]}\n\n"
            "Сменить: /ltm_privacy smart или /ltm_privacy strict"
        )

    async def ltm_export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Высылает пользователю файл с его долгосрочной памятью — строго в личку."""
        import os
        import shutil
        from telegram.error import Forbidden

        user_id = str(update.effective_user.id)
        is_private = update.effective_chat.type == "private"

        path = bot.export_ltm_file(user_id)
        if not path:
            await update.message.reply_text("В долгосрочной памяти пока нет фактов о тебе.")
            return

        try:
            with open(path, "rb") as f:
                await context.bot.send_document(
                    chat_id=user_id,  # всегда в личные сообщения, даже из группы
                    document=InputFile(f, filename=os.path.basename(path)),
                    caption="Твоя долгосрочная память (LTM).",
                )
            if not is_private:
                await update.message.reply_text("Отправил файл тебе в личные сообщения.")
        except Forbidden:
            await update.message.reply_text(
                "Не могу написать первым — начни диалог со мной в личных сообщениях (/start) и повтори команду."
            )
        except Exception as e:
            logger.error(f"[{persona_name}] Ошибка экспорта LTM: {e}", exc_info=True)
            await update.message.reply_text("Не удалось отправить файл, попробуй позже.")
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    # Собираем все handlers
    return {
        "start": start,
        "help": help_cmd,
        "stats": stats_cmd,
        "erase": erase_cmd,
        "last": last_cmd,
        "reset": reset_cmd,
        "forget": forget_cmd,
        "context": context_cmd,
        "relations": relations_cmd,
        "resetall": resetall_cmd,
        "ltm_privacy": ltm_privacy_cmd,
        "ltm_export": ltm_export_cmd,
        "reset_diary": reset_diary_cmd if bot.self_memory else None,
        "files": files_cmd if bot.file_db else None,
        "reset_files": reset_files_cmd if bot.file_db else None,
        "ratelimits": ratelimits_cmd,
        "web": web_cmd if bot._web_search_enabled else None,
        "todo": todo_cmd if bot.todo_manager else None,
        "reminders": reminders_cmd if bot.reminder_manager else None,
        "cancel_reminder": cancel_reminder_cmd if bot.reminder_manager else None,
        "inventory": inventory_cmd if bot.inventory_manager else None,
        "remind": remind_cmd if bot.reminder_manager else None,
        "add_todo": add_todo_cmd if bot.todo_manager else None,
        "add_inventory": add_inventory_cmd if bot.inventory_manager else None,
        "learn": learn_cmd if bot.learning_manager else None,
        "handle_message": handle_message,
        "handle_document": handle_document if bot.file_db else None,
        "handle_photo": handle_photo,
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
        bot.reminder_manager.set_memory(bot.memory)

    # Суточный ритм (утро/ночь/погода): setup_rhythm сам проверит enabled
    bot.setup_rhythm(TelegramMessageSender(bot=app.bot))

    # Learning manager — sender и роутеры для генерации уроков
    if bot.learning_manager:
        sender = TelegramMessageSender(bot=app.bot)
        bot.setup_learning(sender)

    app.add_handler(CommandHandler("start", h["start"]))
    app.add_handler(CommandHandler("help", h["help"]))
    app.add_handler(CommandHandler("stats", h["stats"]))
    app.add_handler(CommandHandler("erase", h["erase"]))
    app.add_handler(CommandHandler("last", h["last"]))
    app.add_handler(CommandHandler("reset", h["reset"]))
    app.add_handler(CommandHandler("forget", h["forget"]))
    app.add_handler(CommandHandler("context", h["context"]))
    app.add_handler(CommandHandler("relations", h["relations"]))
    app.add_handler(CommandHandler("resetall", h["resetall"]))
    app.add_handler(CommandHandler("ltm_privacy", h["ltm_privacy"]))
    app.add_handler(CommandHandler("ltm_export", h["ltm_export"]))

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
    if h.get("remind"):
        app.add_handler(CommandHandler("remind", h["remind"]))
    if h.get("add_todo"):
        app.add_handler(CommandHandler("add_todo", h["add_todo"]))
    if h.get("add_inventory"):
        app.add_handler(CommandHandler("add_inventory", h["add_inventory"]))
    if h.get("learn"):
        app.add_handler(CommandHandler("learn", h["learn"]))

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

    # Photos (OCR через локальную vision-модель)
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, h["handle_photo"]))

    return app

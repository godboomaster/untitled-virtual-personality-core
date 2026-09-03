"""
Reply context — извлекает текст сообщения-ответа для передачи в модель.
Когда пользователь отвечает на чужое сообщение и призывает бота,
бот видит оригинальное сообщение и может на него отреагировать.
"""

from telegram import Update


def extract_reply_context(update: Update, bot_id: int) -> str | None:
    """
    Извлекает контекст сообщения, на которое ответил пользователь.
    Возвращает строку вида '[Имя Автора]: текст сообщения' или None.
    """
    msg = update.message
    if not msg or not msg.reply_to_message:
        return None

    replied = msg.reply_to_message

    # Не подтягиваем контекст из собственных ответов бота
    if replied.from_user and replied.from_user.id == bot_id:
        return None

    # Текст
    replied_text = replied.text or replied.caption or None
    if not replied_text:
        # Если это документ без текста — берём имя файла
        if replied.document:
            replied_text = f"[File: {replied.document.file_name}]" if replied.document.file_name else "[File]"
        else:
            return None

    # Автор
    author = "Unknown"
    if replied.from_user:
        author = replied.from_user.first_name or replied.from_user.username or f"User_{replied.from_user.id}"

    return f"[{author}]: {replied_text}"

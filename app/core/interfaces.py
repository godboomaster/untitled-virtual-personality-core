"""
Единый интерфейс отправки сообщений.
Любой транспорт (мессенджер, веб-чат) реализует MessageSender.
ProactiveMessaging зависит только от интерфейса, не от конкретного транспорта.
"""

from typing import Protocol, Optional


class MessageSender(Protocol):
    # Контракт отправки сообщений.

    # Транспорт доставляет сам файл (иначе вызывающий шлёт содержимое текстом)
    supports_documents: bool
    # У отправленных сообщений есть id, на которые пользователь отвечает reply
    # (иначе reply-логика — «ответ именно на этот вопрос» — недоступна)
    supports_replies: bool

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        topic_id: Optional[int] = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        """
        Отправляет сообщение в чат.

        Args:
            chat_id: ID чата (строкой, даже если транспорт использует числовые ID)
            text: Текст сообщения
            topic_id: ID топика/треда (опционально)
            parse_mode: Режим форматирования (None, "HTML", "Markdown")

        Returns:
            True если отправка успешна, False если нет.
        """
        ...

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
        """
        Отправляет файл (документ) в чат.

        Args:
            chat_id: ID чата.
            file_path: Путь к файлу на диске.
            filename: Имя файла, которое увидит пользователь.
            caption: Подпись к файлу (опционально).
            topic_id: ID топика/треда (опционально).

        Returns:
            True если отправка успешна, False если нет.
        """
        ...

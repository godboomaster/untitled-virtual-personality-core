"""
Единый интерфейс отправки сообщений.
Любой транспорт (Telegram, Discord, VK) реализует MessageSender.
ProactiveMessaging зависит только от интерфейса, не от конкретного транспорта.
"""

from typing import Protocol, Optional


class MessageSender(Protocol):
    """Контракт отправки сообщений."""

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        topic_id: Optional[int] = None,
    ) -> bool:
        """
        Отправляет сообщение в чат.

        Args:
            chat_id: ID чата (строка, т.к. Telegram использует int но часто передает как str)
            text: Текст сообщения
            topic_id: ID топика/треда (опционально)

        Returns:
            True если отправка успешна, False если нет.
        """
        ...

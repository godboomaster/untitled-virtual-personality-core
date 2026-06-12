"""
Простой менеджер списка дел для чата.
Хранит один файл todo.txt на чат в data/{context}/todo/{chat_id}/todo.txt.
"""

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class TodoManager:
    """
    Управляет списком дел чата.
    Файл один на весь чат, пункты привязаны к имени пользователя.
    """

    def __init__(self, context: str = "default"):
        self.context = context
        self.base_dir = Path(f"data/{context}/todo")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _todo_path(self, chat_id: str) -> Path:
        # chat_id может содержать символы, которые не любит файловая система — чистим
        safe_chat_id = re.sub(r"[^\w\-]", "_", str(chat_id))
        chat_dir = self.base_dir / safe_chat_id
        chat_dir.mkdir(parents=True, exist_ok=True)
        return chat_dir / "todo.txt"

    def _parse_items(self, text: str) -> List[tuple]:
        """Парсит пункты из текста файла. Возвращает [(user_name, task)]."""
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Формат: '- Имя: задача' или '- задача'
            if line.startswith("-"):
                content = line[1:].strip()
                if ":" in content:
                    user_name, task = content.split(":", 1)
                    items.append((user_name.strip(), task.strip()))
                else:
                    items.append(("", content))
        return items

    def _format_items(self, items: List[tuple], chat_id: str) -> str:
        """Форматирует пункты в текст файла."""
        lines = [
            f"# Список дел чата {chat_id}",
            f"# Обновлен: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
        ]
        if not items:
            lines.append("# Пока нет записанных дел.")
        else:
            for user_name, task in items:
                name = user_name or "Неизвестный"
                lines.append(f"- {name}: {task}")
        return "\n".join(lines) + "\n"

    def add_item(self, chat_id: str, user_name: str, task: str) -> str:
        """
        Добавляет пункт в список дел чата.
        Возвращает отформатированный список дел.
        """
        task = task.strip()
        if not task:
            return self.get_list(chat_id) or "Список дел пуст."

        with self._lock:
            path = self._todo_path(chat_id)
            items = []
            if path.exists():
                try:
                    items = self._parse_items(path.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning(f"[Todo] Не удалось прочитать {path}: {e}")

            items.append((user_name.strip() or "Пользователь", task))

            try:
                path.write_text(self._format_items(items, chat_id), encoding="utf-8")
            except Exception as e:
                logger.warning(f"[Todo] Не удалось записать {path}: {e}")

        return self._render_list(items)

    def get_list(self, chat_id: str) -> Optional[str]:
        """Возвращает отформатированный список дел или None если файла нет."""
        path = self._todo_path(chat_id)
        if not path.exists():
            return None
        try:
            items = self._parse_items(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[Todo] Не удалось прочитать {path}: {e}")
            return None
        return self._render_list(items)

    def _render_list(self, items: List[tuple]) -> str:
        if not items:
            return "Список дел пуст."
        lines = ["Список дел:"]
        for i, (user_name, task) in enumerate(items, 1):
            name = user_name or "Пользователь"
            lines.append(f"{i}. {name}: {task}")
        return "\n".join(lines)

    def clear(self, chat_id: str) -> bool:
        """Очищает список дел чата. Возвращает True если файл был удален."""
        path = self._todo_path(chat_id)
        if path.exists():
            try:
                path.unlink()
                return True
            except Exception as e:
                logger.warning(f"[Todo] Не удалось удалить {path}: {e}")
        return False


# Эвристика для определения todo-запросов
_TODO_TRIGGERS = [
    "запиши", "добавь", "напомни", "список дел", "to-do", "todo",
]

_TODO_EXTRACT_PATTERNS = [
    re.compile(r"запиши[\s,]*(?:что)?\s*(?:мне|нам|ему|ей|им)?\s*(?:надо|нужно)?\s*[\s,:\-]*(.+)", re.IGNORECASE),
    re.compile(r"добавь(?:\s+в\s+список)?\s*[\s,:\-]*(.+)", re.IGNORECASE),
    re.compile(r"напомни(?:\s+мне)?\s*[\s,:\-]*(.+)", re.IGNORECASE),
    re.compile(r"(?:надо|нужно)\s+(?:мне|нам|ему|ей|им)?\s*[\s,:\-]*(.+)", re.IGNORECASE),
]


def is_todo_request(text: str) -> bool:
    """Определяет, является ли запрос просьбой записать дело."""
    lower = text.lower()
    return any(trigger in lower for trigger in _TODO_TRIGGERS)


def extract_task(text: str) -> Optional[str]:
    """Пытается извлечь текст задачи из запроса. Возвращает None если не удалось."""
    for pattern in _TODO_EXTRACT_PATTERNS:
        match = pattern.search(text)
        if match:
            task = match.group(1).strip()
            # Убираем завершающие частицы "пожалуйста" и знаки
            task = re.sub(r"[.!?\s]*пожалуйста[.!?\s]*$", "", task, flags=re.IGNORECASE).strip()
            if task:
                return task
    # Fallback: если триггер есть, но паттерн не сработал — возвращаем весь текст
    if is_todo_request(text):
        # Убираем обращение к боту
        cleaned = re.sub(r"^(?:(?:коннор|жабка|arrodes|connor)[,\s]+)+", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"[,\s]+пожалуйста\s*$", "", cleaned, flags=re.IGNORECASE).strip()
        if cleaned:
            return cleaned
    return None

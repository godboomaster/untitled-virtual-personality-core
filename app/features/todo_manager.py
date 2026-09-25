"""
Простой менеджер списка дел для чата.
Хранит один файл todo.txt на чат в data/{context}/todo/{chat_id}/todo.txt.
"""

import logging
import re
import threading
import time
from pathlib import Path
from typing import List, Optional

from app.core import timeutil
from app.core.atomic_io import atomic_write_text
from app.core.language import detect_language
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

# Перенос строки внутри задачи ломает построчный формат файла (каждая
# многострочная задача плодит "лишние" строки без "- Имя:", а _parse_items
# либо теряет хвост задачи после \n, либо читает его как отдельный пункт без
# автора). Экранируем реальные \n/\r в один печатный маркер на запись и
# разворачиваем обратно на чтение — файлы без маркера (без многострочных
# задач) читаются как есть, экранировать в них нечего.
_NL_ESCAPE = "\\n"
_BACKSLASH_ESCAPE = "\\\\"

# Имена бота для обрезки обращения в начале фразы («коннор, запиши…»).
# Если персона не передала свои trigger_words — используем этот список
# (текущее поведение до введения персон-специфичных имён).
_DEFAULT_TRIGGER_NAMES = ("коннор", "жабка", "arrodes", "connor", "арродес")


def _trigger_names_alt(trigger_words: Optional[List[str]]) -> str:
    # Имена для regex-альтернации |, экранированные под re.
    names = trigger_words if trigger_words else _DEFAULT_TRIGGER_NAMES
    return "|".join(re.escape(w) for w in names)


def _escape_task(task: str) -> str:
    return task.replace("\\", _BACKSLASH_ESCAPE).replace("\r\n", "\n").replace("\n", _NL_ESCAPE)


def _unescape_task(task: str) -> str:
    out = []
    i = 0
    while i < len(task):
        ch = task[i]
        if ch == "\\" and i + 1 < len(task):
            nxt = task[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)

# Заголовки списка для пользователя: язык — язык записей (записи всегда
# на языке того, кто их диктовал), иначе — язык, переданный вызовом
_LIST_TITLE = {"ru": "Список дел:", "en": "Todo list:"}
_LIST_EMPTY = {"ru": "Список дел пуст.", "en": "The todo list is empty."}


class TodoManager:
    """
    Управляет списком дел чата.
    Файл один на весь чат, пункты привязаны к имени пользователя.
    """

    def __init__(self, context: str = "default"):
        self.context = context
        self.base_dir = data_dir() / context / "todo"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _todo_path(self, chat_id: str) -> Path:
        # chat_id может содержать символы, которые не любит файловая система — чистим
        safe_chat_id = re.sub(r"[^\w\-]", "_", str(chat_id))
        chat_dir = self.base_dir / safe_chat_id
        chat_dir.mkdir(parents=True, exist_ok=True)
        return chat_dir / "todo.txt"

    def _parse_items(self, text: str) -> List[tuple]:
        # Парсит пункты из текста файла. Возвращает [(user_name, task)].
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
                    items.append((user_name.strip(), _unescape_task(task.strip())))
                else:
                    items.append(("", _unescape_task(content)))
        return items

    def _format_items(self, items: List[tuple], chat_id: str) -> str:
        # Форматирует пункты в текст файла.
        lines = [
            f"# Список дел чата {chat_id}",
            f"# Обновлен: {timeutil.now().strftime('%Y-%m-%d %H:%M')}",
            "",
        ]
        if not items:
            lines.append("# Пока нет записанных дел.")
        else:
            for user_name, task in items:
                name = user_name or "Unknown"
                lines.append(f"- {name}: {_escape_task(task)}")
        return "\n".join(lines) + "\n"

    def add_item(self, chat_id: str, user_name: str, task: str, lang: str = None) -> str:
        """
        Добавляет пункт в список дел чата.
        Возвращает отформатированный список дел.
        lang ('ru'/'en') — язык заголовков списка (по умолчанию — язык записей).
        """
        task = task.strip()
        if not task:
            return self.get_list(chat_id, lang=lang) or _LIST_EMPTY.get(lang or "ru", _LIST_EMPTY["ru"])

        with self._lock:
            path = self._todo_path(chat_id)
            items = []
            if path.exists():
                try:
                    items = self._parse_items(path.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning(f"[Todo] Не удалось прочитать {path}: {e}")

            items.append((user_name.strip() or "User", task))

            try:
                atomic_write_text(path, self._format_items(items, chat_id))
            except Exception as e:
                logger.warning(f"[Todo] Не удалось записать {path}: {e}")

        return self._render_list(items, lang=lang)

    def get_list(self, chat_id: str, lang: str = None) -> Optional[str]:
        """Возвращает отформатированный список дел или None если файла нет.

        Под тем же локом, что и мутации: без него чтение могло застать файл
        ровно в момент записи add_item/remove_item из другого потока
        (стресс-тестом это ловилось как пустые/битые чтения даже при
        атомарной записи — race именно на уровне «файл ещё не появился /
        уже удалён между exists() и read_text()», а не на уровне повреждения
        содержимого)."""
        with self._lock:
            path = self._todo_path(chat_id)
            if not path.exists():
                return None
            try:
                items = self._parse_items(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[Todo] Не удалось прочитать {path}: {e}")
                return None
        return self._render_list(items, lang=lang)

    def _render_list(self, items: List[tuple], lang: str = None) -> str:
        # Язык заголовков: явный → язык записей → русский
        if lang not in _LIST_TITLE:
            lang = detect_language(" ".join(task for _, task in items)) or "ru"
        if not items:
            return _LIST_EMPTY[lang]
        lines = [_LIST_TITLE[lang]]
        for i, (user_name, task) in enumerate(items, 1):
            name = user_name or "User"
            lines.append(f"{i}. {name}: {task}")
        return "\n".join(lines)

    def remove_item(self, chat_id: str, index: int, lang: str = None) -> Optional[str]:
        """
        Удаляет пункт по номеру (1-based).
        Возвращает отформатированный список или None если индекс невалиден.
        """
        with self._lock:
            path = self._todo_path(chat_id)
            if not path.exists():
                return None
            try:
                items = self._parse_items(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[Todo] Не удалось прочитать {path}: {e}")
                return None

            if index < 1 or index > len(items):
                return None

            removed = items.pop(index - 1)
            logger.info(f"[Todo] Удалён пункт {index}: {removed}")

            try:
                atomic_write_text(path, self._format_items(items, chat_id))
            except Exception as e:
                logger.warning(f"[Todo] Не удалось записать {path}: {e}")

        return self._render_list(items, lang=lang)

    def clear(self, chat_id: str) -> bool:
        # Очищает список дел чата. Возвращает True если файл был удален.
        with self._lock:
            path = self._todo_path(chat_id)
            if path.exists():
                try:
                    path.unlink()
                    return True
                except Exception as e:
                    logger.warning(f"[Todo] Не удалось удалить {path}: {e}")
        return False


# Эвристика для определения todo-запросов.
# "напомни" сюда намеренно не входит: любой текст с "напом" уходит в путь
# напоминаний (reminder_manager), который перехватывает его раньше todo.
_TODO_TRIGGERS = [
    "запиши", "добавь", "список дел", "to-do", "todo",
]

_TODO_EXTRACT_PATTERNS = [
    re.compile(r"запиши[\s,]*(?:что)?\s*(?:мне|нам|ему|ей|им)?\s*(?:надо|нужно)?\s*[\s,:\-]*(.+)", re.IGNORECASE),
    re.compile(r"добавь(?:\s+в\s+список)?\s*[\s,:\-]*(.+)", re.IGNORECASE),
    re.compile(r"(?:надо|нужно)\s+(?:мне|нам|ему|ей|им)?\s*[\s,:\-]*(.+)", re.IGNORECASE),
]


# Границы слов: «добавь» не ловит «добавьте», «сделать» не ловит «сделал»
# (это завершение дела). «сделать/доделать» — инфинитивы: «надо сделать отчёт».
_TODO_TRIGGER_RE = re.compile(
    r"\b(?:запиши|добавь|список\s+дел|to-do|todo|доделать|сделать)\b",
    re.IGNORECASE,
)


def is_todo_request(text: str) -> bool:
    # Определяет, является ли запрос просьбой записать дело.
    return bool(_TODO_TRIGGER_RE.search(text))


# Просьба ПОКАЗАТЬ список: текст заканчивается на «список/списка/списке дел»
# (опционально «на сегодня/завтра» и знаки): «дай мой список дел», «что в списке дел?».
_TODO_LIST_REQUEST_RE = re.compile(
    r"\bспис\w*\s+дел\b(?:\s+на\s+(?:сегодня|завтра|неделю))?\s*[?.!]*$",
    re.IGNORECASE,
)

# Явные команды изменения списка — отменяют показ
# («запиши в список дел», «удали пункт из списка дел» — не просьба показать).
_TODO_LIST_EXCLUDE_RE = re.compile(
    r"\b(?:запиши|записать|добавь|добавить|доделать|удали|удалить|убери|убрать|вычеркни|зачеркни)\b",
    re.IGNORECASE,
)


def is_todo_list_request(text: str) -> bool:
    # Просьба показать список дел (а не добавить/убрать пункт).
    if not _TODO_LIST_REQUEST_RE.search(text):
        return False
    return not _TODO_LIST_EXCLUDE_RE.search(text)


# Просьба отметить дело выполненным (по границам слов).
_TODO_DONE_TRIGGER_RE = re.compile(
    r"\b(?:сделал|сделано|готово|вычеркни|вычеркнуть|убери\s+из|убрать\s+из|"
    r"удали\s+из|удалить\s+из|выполнил|выполнено|закрыл\s+дело|зачеркни)\b",
    re.IGNORECASE,
)


def is_todo_done_request(text: str) -> bool:
    # Определяет, просит ли пользователь убрать дело (сделано/вычеркни/убери).
    return bool(_TODO_DONE_TRIGGER_RE.search(text))


_TODO_DONE_TRIGGERS = [
    "сделал", "сделано", "готово", "вычеркни", "вычеркнуть",
    "убери из", "убрать из", "удали из", "удалить из",
    "выполнил", "выполнено", "закрыл дело", "зачеркни",
]

_TODO_DONE_NUMBER_RE = re.compile(
    r"(?:пункт\s+)?(\d+)",
)


def extract_todo_done_index(text: str) -> Optional[int]:
    # Номер пункта для удаления (1-based) или None.
    match = _TODO_DONE_NUMBER_RE.search(text)
    if match:
        return int(match.group(1))
    return None


def extract_task(text: str, trigger_words: Optional[List[str]] = None) -> Optional[str]:
    # Пытается извлечь текст задачи из запроса. Возвращает None если не удалось.
    # trigger_words — имена персоны, чтобы обрезать обращение в начале фразы
    # («коннор, запиши…»); без них берётся дефолтный список имён.
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
        alt = _trigger_names_alt(trigger_words)
        cleaned = re.sub(rf"^(?:(?:{alt})[,\s]+)+", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"[,\s]+пожалуйста\s*$", "", cleaned, flags=re.IGNORECASE).strip()
        if cleaned:
            return cleaned
    return None

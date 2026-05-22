"""
Автоматическое создание файлов из ответов LLM.

Логика:
- Если в ответе есть блок кода с языком (```python, ```js и т.д.) — код отправляется файлом
- Если ответ длинный (> FILE_THRESHOLD символов) — весь ответ отправляется как .md
- Короткий текст без кода отправляется обычным сообщением
"""

import re
import os
import tempfile
import logging
from typing import Optional, Tuple, List

logger = logging.getLogger(__name__)

# Порог длины — если ответ длиннее, упаковываем в файл
FILE_THRESHOLD = 3500  # символов

# Telegram лимит — если ответ длиннее, отправляем summary + файл
TELEGRAM_LIMIT = 4000  # символов

# Маппинг маркеров языка → расширение файла
LANG_EXTENSIONS = {
    "python": ".py", "py": ".py",
    "javascript": ".js", "js": ".js",
    "typescript": ".ts", "ts": ".ts",
    "java": ".java",
    "c": ".c", "cpp": ".cpp", "c++": ".cpp",
    "csharp": ".cs", "cs": ".cs", "c#": ".cs",
    "go": ".go",
    "rust": ".rs", "rs": ".rs",
    "ruby": ".rb", "rb": ".rb",
    "php": ".php",
    "swift": ".swift",
    "kotlin": ".kt", "kt": ".kt",
    "sql": ".sql",
    "html": ".html",
    "css": ".css",
    "scss": ".scss",
    "json": ".json",
    "yaml": ".yaml", "yml": ".yml",
    "xml": ".xml",
    "bash": ".sh", "shell": ".sh", "sh": ".sh",
    "powershell": ".ps1",
    "dockerfile": ".dockerfile",
    "docker": ".dockerfile",
    "lua": ".lua",
    "r": ".r",
    "matlab": ".m",
    "dart": ".dart",
    "vue": ".vue",
    "react": ".jsx",
    "tsx": ".tsx",
    "toml": ".toml",
    "ini": ".ini",
    "cfg": ".cfg",
    "conf": ".conf",
    "nginx": ".conf",
    "graphql": ".graphql",
    "proto": ".proto",
}


def _extract_code_blocks(text: str) -> List[Tuple[str, str]]:
    """
    Извлекает блоки кода с указанным языком.
    Возвращает список (язык, код).
    """
    pattern = r'```(\w+)\n(.*?)```'
    matches = re.findall(pattern, text, flags=re.DOTALL)
    return [(lang.strip(), code.strip()) for lang, code in matches]


def _get_extension(lang: str) -> str:
    """Возвращает расширение файла для языка."""
    return LANG_EXTENSIONS.get(lang.lower(), f".{lang.lower()}")


def _is_mostly_code(text: str) -> bool:
    """
    Определяет, состоит ли ответ преимущественно из кода.
    Если есть блок кода с языком и он занимает >60% ответа — это код-ответ.
    """
    blocks = _extract_code_blocks(text)
    if not blocks:
        return False

    total_code_len = sum(len(code) for _, code in blocks)
    total_text_len = len(text)

    return total_code_len > total_text_len * 0.5


def _has_code_blocks(text: str) -> bool:
    """Есть ли в тексте блоки кода с указанным языком."""
    return bool(_extract_code_blocks(text))


def prepare_response(text: str) -> Tuple[str, Optional[list]]:
    """
    Анализирует ответ LLM и решает, нужно ли создавать файл(ы).

    Возвращает:
        (text_to_send, files_or_none)
        - text_to_send: текст для обычного сообщения (может быть сокращён)
        - files: список (filepath, filename) или None

    Стратегия:
        1. Есть код-блоки с языком И ответ = преимущественно код →
           файл с кодом, текст = краткое описание
        2. Есть код-блоки с языком, но ответ смешанный →
           файл с кодом + .md с полным ответом
        3. Нет код-блоков, но ответ длинный → .md файл
        4. Короткий ответ без кода → обычное сообщение (files=None)
    """
    if not text or len(text.strip()) == 0:
        return text, None

    code_blocks = _extract_code_blocks(text)
    mostly_code = _is_mostly_code(text)
    is_long = len(text) > FILE_THRESHOLD

    # --- Случай 1: преимущественно код ---
    if code_blocks and mostly_code:
        # Берём самый большой блок кода как основной файл
        main_block = max(code_blocks, key=lambda b: len(b[1]))
        lang, code = main_block

        ext = _get_extension(lang)
        filename = f"code{ext}"
        filepath = _write_temp_file(code, filename)

        # Текст без кода — как описание
        description = _strip_code_blocks(text).strip()

        if not description:
            description = f"Вот код ({lang}):"

        # Если несколько блоков кода — доп. файлы
        extra_files = []
        for i, (l, c) in enumerate(code_blocks):
            if (l, c) == main_block:
                continue
            ext2 = _get_extension(l)
            fn2 = f"code_{i + 1}{ext2}"
            extra_files.append(_write_temp_file(c, fn2))

        all_files = [(filepath, filename)] + extra_files
        return description, all_files

    # --- Случай 2: есть код, но ответ смешанный (объяснение + код) ---
    if code_blocks and is_long:
        # Основной файл — весь ответ как .md
        md_filename = "response.md"
        md_filepath = _write_temp_file(text, md_filename)

        # Для каждого блока кода — отдельный файл
        code_files = []
        for i, (lang, code) in enumerate(code_blocks):
            ext = _get_extension(lang)
            fn = f"code_{i + 1}{ext}"
            code_files.append(_write_temp_file(code, fn))

        all_files = [(md_filepath, md_filename)] + code_files

        # Короткое текстовое сообщение
        description = _strip_code_blocks(text).strip()
        if len(description) > 500:
            description = description[:500] + "...\n\n📄 Полный ответ и код — в файлах ниже."
        else:
            description += "\n\n📄 Полный ответ и код — в файлах ниже."

        return description, all_files

    # --- Случай 3: длинный ответ без кода → .md ---
    if is_long and not code_blocks:
        md_filename = "response.md"
        md_filepath = _write_temp_file(text, md_filename)

        description = text[:500] + "...\n\n📄 Продолжение — в файле."
        return description, [(md_filepath, md_filename)]

    # --- Случай 4: ответ превышает Telegram лимит → summary + .md ---
    if len(text) > TELEGRAM_LIMIT:
        md_filename = "response.md"
        md_filepath = _write_temp_file(text, md_filename)

        # Берём первые ~500 символов как summary
        summary = text[:500].strip()
        # Обрезаем по последнему переносу строки, чтобы не рвать слова
        last_break = summary.rfind('\n')
        if last_break > 300:
            summary = summary[:last_break]
        description = summary + "\n\n📄 Полный ответ — в файле."
        return description, [(md_filepath, md_filename)]

    # --- Случай 5: обычный ответ ---
    return text, None


def _strip_code_blocks(text: str) -> str:
    """Удаляет блоки кода из текста, оставляя только текст."""
    result = re.sub(r'```\w*\n.*?```', '', text, flags=re.DOTALL)
    # Убираем пустые строки подряд
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result


def _write_temp_file(content: str, filename: str) -> str:
    """
    Записывает контент во временный файл.
    Возвращает абсолютный путь.
    """
    tmp_dir = tempfile.mkdtemp(prefix="virtp_")
    filepath = os.path.join(tmp_dir, filename)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

    logger.info(f"Создан файл: {filepath} ({len(content)} символов)")
    return filepath


def cleanup_files(files: List[Tuple[str, str]]):
    """Удаляет временные файлы после отправки."""
    for filepath, _ in files:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
            # Удаляем пустую temp-директорию
            dirpath = os.path.dirname(filepath)
            if os.path.exists(dirpath) and not os.listdir(dirpath):
                os.rmdir(dirpath)
        except Exception as e:
            logger.warning(f"Не удалось удалить {filepath}: {e}")

"""
Автоматическое создание файлов из ответов LLM.

Логика:
- Если ответ = преимущественно код (код-блоки занимают >50%) — код отправляется файлом,
  пояснение текстом
- Если есть код-блоки в длинном ответе — код файлом, весь текст несколькими сообщениями
- Длинный текст без кода — отправляется несколькими сообщениями целиком
- Короткий ответ — одно сообщение
"""

import re
import os
import tempfile
import logging
from typing import Optional, Tuple, List

logger = logging.getLogger(__name__)

# Telegram лимит на одно сообщение
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
    """Извлекает блоки кода с указанным языком."""
    pattern = r'```(\w+)\n(.*?)```'
    matches = re.findall(pattern, text, flags=re.DOTALL)
    return [(lang.strip(), code.strip()) for lang, code in matches]


def _get_extension(lang: str) -> str:
    """Возвращает расширение файла для языка."""
    return LANG_EXTENSIONS.get(lang.lower(), f".{lang.lower()}")


def _is_mostly_code(text: str) -> bool:
    """Если код занимает >50% ответа — это код-ответ."""
    blocks = _extract_code_blocks(text)
    if not blocks:
        return False
    total_code_len = sum(len(code) for _, code in blocks)
    return total_code_len > len(text) * 0.5


def _strip_code_blocks(text: str) -> str:
    """Удаляет блоки кода из текста."""
    result = re.sub(r'```\w*\n.*?```', '', text, flags=re.DOTALL)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result


def _write_temp_file(content: str, filename: str) -> str:
    """Записывает контент во временный файл."""
    tmp_dir = tempfile.mkdtemp(prefix="virtp_")
    filepath = os.path.join(tmp_dir, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    logger.info(f"Создан файл: {filepath} ({len(content)} символов)")
    return filepath


def _split_text(text: str, limit: int = TELEGRAM_LIMIT) -> List[str]:
    """Разбивает длинный текст на части по абзацам с лимитом символов."""
    if len(text) <= limit:
        return [text]

    parts = []
    current = ""

    # Сначала пробуем разбить по двойным переносам (абзацам)
    paragraphs = text.split('\n\n')

    for para in paragraphs:
        if not para.strip():
            continue
        # Если один абзац длиннее лимита — режем по строкам
        if len(para) > limit:
            lines = para.split('\n')
            for line in lines:
                if len(current) + len(line) + 2 > limit and current:
                    parts.append(current.strip())
                    current = line
                else:
                    current = current + '\n' + line if current else line
        elif len(current) + len(para) + 2 > limit:
            parts.append(current.strip())
            current = para
        else:
            current = current + '\n\n' + para if current else para

    if current.strip():
        parts.append(current.strip())

    return parts


def prepare_response(text: str) -> Tuple[List[str], Optional[list]]:
    """
    Анализирует ответ LLM и решает формат отправки.

    Возвращает:
        (messages, files_or_none)
        - messages: список текстов для отправки (1 или несколько сообщений)
        - files: список (filepath, filename) или None

    Логика:
        1. Ответ = преимущественно код → файл с кодом, пояснение текстом
        2. Есть код-блоки в смешанном ответе → код файлом, текст несколькими сообщениями
        3. Длинный текст без кода → несколько сообщений целиком
        4. Короткий текст → одно сообщение
    """
    if not text or len(text.strip()) == 0:
        return [text], None

    code_blocks = _extract_code_blocks(text)
    mostly_code = _is_mostly_code(text)

    # --- Случай 1: преимущественно код → файл ---
    if code_blocks and mostly_code:
        main_block = max(code_blocks, key=lambda b: len(b[1]))
        lang, code = main_block

        ext = _get_extension(lang)
        filename = f"code{ext}"
        filepath = _write_temp_file(code, filename)

        description = _strip_code_blocks(text).strip()
        if not description:
            description = f"Вот код ({lang}):"

        # Дополнительные файлы если несколько блоков
        extra_files = []
        for i, (l, c) in enumerate(code_blocks):
            if (l, c) == main_block:
                continue
            ext2 = _get_extension(l)
            fn2 = f"code_{i + 1}{ext2}"
            extra_files.append(_write_temp_file(c, fn2))

        all_files = [(filepath, filename)] + extra_files
        return [description], all_files

    # --- Случай 2: есть код в смешанном ответе → код файлом, текст полностью ---
    if code_blocks:
        # Код — в файлы
        code_files = []
        for i, (lang, code) in enumerate(code_blocks):
            ext = _get_extension(lang)
            fn = f"code_{i + 1}{ext}"
            code_files.append(_write_temp_file(code, fn))

        # Текст без кода — несколькими сообщениями
        text_only = _strip_code_blocks(text).strip()
        messages = _split_text(text_only)

        return messages, code_files

    # --- Случай 3: текст без кода → разбиваем на части если длинный ---
    messages = _split_text(text)
    return messages, None


def cleanup_files(files: List[Tuple[str, str]]):
    """Удаляет временные файлы после отправки."""
    for filepath, _ in files:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
            dirpath = os.path.dirname(filepath)
            if os.path.exists(dirpath) and not os.listdir(dirpath):
                os.rmdir(dirpath)
        except Exception as e:
            logger.warning(f"Не удалось удалить {filepath}: {e}")

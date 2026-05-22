"""
Модуль для извлечения текста из различных файлов.
Использует markitdown (Microsoft) — единая библиотека для всех форматов.
Поддерживаемые форматы: PDF, DOCX, PPTX, XLSX, TXT, MD, CSV, JSON, HTML, XML,
RTF, ODT, EPUB, IPYNB, изображения, ZIP и другие.
"""

import io
import logging
from markitdown import MarkItDown

logger = logging.getLogger(__name__)

# Максимальный размер файла (10 МБ)
MAX_FILE_SIZE_DEFAULT = 10 * 1024 * 1024

_md = MarkItDown()

SUPPORTED_EXTENSIONS = {
    'txt', 'md', 'py', 'js', 'ts', 'json', 'csv', 'html', 'xml',
    'yaml', 'yml', 'log', 'cfg', 'ini', 'sh', 'bat', 'rtf', 'odt',
    'epub', 'pdf', 'docx', 'doc', 'pptx', 'xlsx', 'xls', 'ipynb',
    'png', 'jpg', 'jpeg', 'gif', 'bmp', 'tiff', 'webp',
}


def extract_text(file_bytes: bytes, filename: str) -> str:
    """
    Извлекает текст из файла через markitdown.

    Args:
        file_bytes: Содержимое файла в байтах
        filename: Имя файла (для определения формата)

    Returns:
        Извлечённый текст в формате Markdown
    """
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

    if ext not in SUPPORTED_EXTENSIONS:
        return (
            f"Формат .{ext} не поддерживается.\n\n"
            f"Поддерживаемые: PDF, DOCX, PPTX, XLSX, TXT, MD, CSV, JSON, "
            f"HTML, XML, PY, JS, TS, LOG, YAML, RTF, ODT, EPUB, IPYNB, "
            f"PNG, JPG, GIF и другие."
        )

    try:
        stream = io.BytesIO(file_bytes)
        result = _md.convert(stream, file_extension=f'.{ext}')
        text = result.text_content

        if not text or not text.strip():
            return "Не удалось извлечь текст (возможно, файл пуст или содержит только изображения)"

        return text

    except Exception as e:
        logger.warning(f"markitdown не смог обработать {filename}: {e}")
        return f"Ошибка чтения файла: {e}"


"""Лор персоны из веба: загрузка, список, удаление и проверка запроса.

Сам лор и поиск — app/core/lore.py. Здесь — разбор файла: txt и md читаются
напрямую, остальное — через extract_text (markitdown). У него ошибка приходит
строкой вместо текста, поэтому файл, который просто начинается со слова
«Ошибка», для txt не должен считаться отказом.
"""

from app.core import lore
from app.core.file_reader import extract_text
from app.core.lore import LoreError

MAX_BODY_BYTES = lore.MAX_FILE_BYTES + 64 * 1024

# Как extract_text сообщает об отказе (см. _EXTRACT_ERROR_PREFIXES в server.py)
_EXTRACT_ERRORS = ("Ошибка чтения", "Формат .", "Не удалось извлечь", "Библиотека")


def _decode(data: bytes) -> str:
    # Текстовый файл: BOM, затем UTF-8, затем определение кодировки (cp1251
    # старых русских txt, UTF-16 без BOM)
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(enc, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    except ImportError:
        pass
    return data.decode("cp1251", errors="replace")


def read_text(filename: str, data: bytes) -> str:
    # Текст файла или LoreError с понятной причиной
    name = lore.clean_filename(filename)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in lore.EXTENSIONS:
        raise LoreError("Лор — это текст: txt, md, docx, pdf, odt, rtf, epub или html")
    if len(data) > lore.MAX_FILE_BYTES:
        raise LoreError(f"Файл больше {lore.MAX_FILE_BYTES // (1024 * 1024)} МБ")
    if ext in ("txt", "md"):
        return _decode(data)
    text = extract_text(data, name)
    if text.startswith(_EXTRACT_ERRORS):
        raise LoreError(text)
    return text


def info(persona: str) -> dict:
    return lore.store_for(persona).info()


def upload(persona: str, filename: str, data: bytes) -> dict:
    store = lore.store_for(persona)
    store.add_file(filename, read_text(filename, data), original=data)
    return store.info()


# Как отдавать оригинал: PDF браузер показывает сам (во вкладке «Лор» — во
# встроенном просмотре), остальное — только скачать: html или svg, открытые
# со страницы веба, исполнили бы чужие скрипты
_MEDIA = {
    "pdf": "application/pdf", "txt": "text/plain; charset=utf-8", "md": "text/markdown; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword", "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf", "epub": "application/epub+zip", "html": "application/octet-stream",
}


def view(persona: str, filename: str) -> dict | None:
    """Файл лора для просмотра: текст с заголовками (как его разобрала
    нарезка) и сами куски — ровно то, что ищет поиск."""
    store = lore.store_for(persona)
    entry = store.file_entry(filename)
    text = store.source_text(filename) if entry else None
    if entry is None or text is None:
        return None
    name = entry["name"]
    return {
        "name": name, "chars": entry["chars"], "chunks": entry["chunks"], "ts": entry["ts"],
        "ext": lore.file_ext(name),
        "original": store.original_path(name) is not None,
        "blocks": lore.outline(text),
        "pieces": store.file_chunks(name),
    }


def original(persona: str, filename: str) -> tuple | None:
    # (путь, тип, имя для скачивания) или None: файл загружен до того, как
    # оригиналы стали сохраняться
    store = lore.store_for(persona)
    entry = store.file_entry(filename)
    path = store.original_path(filename) if entry else None
    if path is None:
        return None
    return path, _MEDIA.get(lore.file_ext(entry["name"]), "application/octet-stream"), entry["name"]


def remove(persona: str, filename: str) -> dict | None:
    store = lore.store_for(persona)
    return store.info() if store.remove_file(filename) else None


def search(bot, query: str, previous: str = "") -> dict:
    result = bot.lore_search(query, previous)
    return {
        "hits": [{"file": h.file, "section": h.section, "text": h.text,
                  "similarity": round(h.similarity, 3), "words": h.words}
                 for h in result.hits],
        "weak": result.weak,
        "skipped": result.skipped,
    }

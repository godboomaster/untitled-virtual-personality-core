"""
Веб-поиск через DuckDuckGo.
Используется когда в контексте разговора/памяти нет ответа на вопрос.
"""

import re
import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Сколько результатов брать
MAX_RESULTS = 5
# Максимальная длина сниппета (символов)
MAX_SNIPPET_LEN = 300
# Максимальная длина загруженного текста страницы
MAX_PAGE_TEXT_LEN = 3000
# Таймаут загрузки страницы (секунды)
PAGE_FETCH_TIMEOUT = 10
# Сколько страниц загружать полностью (из top результатов)
FETCH_TOP_N = 2

# Lazy import — не ломает старт бота если пакет не установлен
_DDGS = None


def _get_ddgs():
    global _DDGS
    if _DDGS is not None:
        return _DDGS
    try:
        from ddgs import DDGS
        _DDGS = DDGS
        return _DDGS
    except ImportError:
        logger.error("[WEB_SEARCH] Пакет ddgs не установлен: pip install ddgs")
        return None


def fetch_page_text(url: str, max_len: int = MAX_PAGE_TEXT_LEN) -> str:
    """
    Загружает страницу и извлекает основной текст (без HTML-тегов, скриптов, стилей).

    Возвращает чистый текст длиной до max_len символов, или пустую строку при ошибке.
    """
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=PAGE_FETCH_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru,en;q=0.9",
            },
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()

        html = resp.text

        # Удаляем скрипты, стили, head, nav, footer, header
        for tag in ["script", "style", "head", "nav", "footer", "header", "aside", "noscript"]:
            html = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", html, flags=re.DOTALL | re.IGNORECASE)

        # Удаляем все HTML-теги
        text = re.sub(r"<[^>]+>", " ", html)

        # Декодируем HTML-сущности
        text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#39;", "'")

        # Сжимаем пробелы и пустые строки
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n", "\n\n", text)

        text = text.strip()

        # Обрезаем до лимита
        if len(text) > max_len:
            # Режем по последнему предложению в пределах лимита
            cut = text[:max_len]
            last_dot = cut.rfind(".")
            last_newline = cut.rfind("\n")
            boundary = max(last_dot, last_newline)
            if boundary > max_len // 2:
                text = cut[:boundary + 1].strip()
            else:
                text = cut.strip()

        return text

    except Exception as e:
        logger.debug(f"[WEB_SEARCH] Не удалось загрузить {url}: {e}")
        return ""


def _is_fetchable_url(url: str) -> bool:
    # Проверяет, имеет ли смысл загружать страницу (пропускает PDF, видео, etc)
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        skip_exts = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mp3",
                     ".zip", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")
        if path.endswith(skip_exts):
            return False
        # Пропускаем известные не-текстовые хосты
        skip_hosts = ("youtube.com", "youtu.be", "vimeo.com", "tiktok.com",
                      "instagram.com", "twitter.com", "x.com", "facebook.com")
        if parsed.hostname and any(h in parsed.hostname for h in skip_hosts):
            return False
        return True
    except Exception:
        return False


def search_web(query: str, max_results: int = MAX_RESULTS) -> list[dict]:
    """
    Ищет запрос в DuckDuckGo и возвращает список результатов.
    Для топ-результатов загружает полный текст страницы.

    Returns:
        [{"title": ..., "body": ..., "href": ..., "full_text": ...}, ...]
    """
    DDGS = _get_ddgs()
    if DDGS is None:
        logger.error("[WEB_SEARCH] Пакет ddgs не установлен")
        return []

    try:
        ddgs = DDGS()
        results = list(ddgs.text(query, max_results=max_results))
        if not results:
            logger.info(f"[WEB_SEARCH] Нет результатов для: '{query[:60]}'")
            return []

        # Обрезаем длинные сниппеты
        for r in results:
            if len(r.get("body", "")) > MAX_SNIPPET_LEN:
                r["body"] = r["body"][:MAX_SNIPPET_LEN] + "..."
            r["full_text"] = ""

        # Загружаем полный текст для топ-результатов
        for r in results[:FETCH_TOP_N]:
            url = r.get("href", "")
            if url and _is_fetchable_url(url):
                full = fetch_page_text(url)
                if full:
                    r["full_text"] = full
                    logger.info(f"[WEB_SEARCH] Загружен текст: {url[:60]} ({len(full)} символов)")

        logger.info(f"[WEB_SEARCH] Найдено {len(results)} результатов для: '{query[:60]}'")
        return results

    except Exception as e:
        logger.error(f"[WEB_SEARCH] Ошибка поиска: {e}")
        return []


def format_web_results(results: list[dict]) -> str:
    """
    Форматирует результаты в текст для вставки в промпт.
    Если есть full_text — использует его вместо сниппета.
    """
    if not results:
        return ""

    parts = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "Без заголовка")
        href = r.get("href", "")

        # Полный текст приоритетнее сниппета
        body = r.get("full_text") or r.get("body", "")
        if not body:
            continue

        parts.append(f"{i}. {title}\n   {body}\n   Источник: {href}")

    return "\n\n".join(parts)

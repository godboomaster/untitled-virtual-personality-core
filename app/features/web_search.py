"""
Веб-поиск через DuckDuckGo.
Используется когда в контексте разговора/памяти нет ответа на вопрос.
"""

import logging

logger = logging.getLogger(__name__)

# Сколько результатов брать
MAX_RESULTS = 5
# Максимальная длина сниппета (символов)
MAX_SNIPPET_LEN = 300

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


def search_web(query: str, max_results: int = MAX_RESULTS) -> list[dict]:
    """
    Ищет запрос в DuckDuckGo и возвращает список результатов.

    Returns:
        [{"title": ..., "body": ..., "href": ...}, ...]
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

        logger.info(f"[WEB_SEARCH] Найдено {len(results)} результатов для: '{query[:60]}'")
        return results

    except Exception as e:
        logger.error(f"[WEB_SEARCH] Ошибка поиска: {e}")
        return []


def format_web_results(results: list[dict]) -> str:
    """
    Форматирует результаты в текст для вставки в промпт.
    """
    if not results:
        return ""

    parts = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "Без заголовка")
        body = r.get("body", "")
        href = r.get("href", "")
        parts.append(f"{i}. {title}\n   {body}\n   Источник: {href}")

    return "\n\n".join(parts)

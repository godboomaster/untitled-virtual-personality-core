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


# Чёрный список доменов — соцсети, развлекательные, ненадёжные источники
BLACKLIST_DOMAINS = {
    # Соцсети
    "instagram.com", "instagr.am",
    "tiktok.com", "tiktokv.com",
    "twitter.com", "x.com", "t.co",
    "facebook.com", "fb.com", "fb.me",
    "vk.com", "vk.me", "vkontakte.ru",
    "ok.ru", "odnoklassniki.ru",
    "telegram.org", "t.me", "telegra.ph",
    "linkedin.com", "lnkd.in",
    "pinterest.com", "pin.it",
    "snapchat.com", "snap.com",
    "reddit.com", "redd.it",
    "tumblr.com",
    "discord.com", "discord.gg", "discordapp.com",
    # Видео
    "youtube.com", "youtu.be",
    "vimeo.com",
    "twitch.tv",
    # Развлекательные / ненадёжные
    "9gag.com", "9gag.ru",
    "buzzfeed.com", "buzzfeednews.com",
    "memepedia.ru", "knowyourmeme.com",
    "giphy.com", "tenor.com",
    # Форумы с низким качеством
    "pikabu.ru", "joyreactor.cc",
    # Короткие ссылки (непредсказуемы)
    "bit.ly", "tinyurl.com", "goo.gl", "ow.ly", "short.link",
    # AI-генераторы контента (можут быть галлюцинации)
    "chatgpt.com", "openai.com",
}


def _is_blacklisted(url: str) -> bool:
    """Проверяет URL по чёрному списку доменов."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return True
        hostname = hostname.lower()
        # Проверяем точное совпадение и поддомены
        for blocked in BLACKLIST_DOMAINS:
            if hostname == blocked or hostname.endswith("." + blocked):
                return True
        return False
    except Exception:
        return True  # При ошибке — блокируем


def _is_fetchable_url(url: str) -> bool:
    # Проверяет, имеет ли смысл загружать страницу (пропускает PDF, видео, etc)
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        skip_exts = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mp3",
                     ".zip", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")
        if path.endswith(skip_exts):
            return False
        # Проверяем чёрный список
        if _is_blacklisted(url):
            return False
        return True
    except Exception:
        return False


def _enhance_query(query: str) -> tuple[str, str | None]:
    """
    Улучшает поисковый запрос через локальную LLM:
    - Перефразирует под поиск (убирает разговорные обороты)
    - Переводит на английский если тема техническая/международная

    Возвращает (ru_query, en_query | None).
    en_query = None если тема сугубо русскоязычная (локальные новости, люди, события).
    """
    try:
        from app.core.local_router import get_local_router
        router = get_local_router()
        if not router or not router.is_available():
            return query, None

        prompt = (
            "You improve search queries for a search engine. Your task:\n"
            "1. Rephrase the query for better search results (remove conversational filler, make it concise and specific)\n"
            "2. For technical/international topics (docker, kubernetes, python, AI, programming, software) — provide English translation\n"
            "   For purely Russian topics (Russian news, Russian people, local events, weather in Russian cities) — English is not needed (null)\n\n"
            "Reply STRICTLY in JSON format (no explanations, only JSON):\n"
            '{"ru": "rephrased query in Russian", "en": "english query or null"}\n\n'
            f'Query: "{query}"'
        )

        response = router.get_response(
            messages=[
                {"role": "system", "content": "You improve search queries. Reply ONLY JSON, no markdown code blocks."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=100,
        )

        if not response:
            return query, None

        # Парсим JSON
        import json as _json
        response = response.strip()

        # Убираем markdown code blocks если есть
        response = re.sub(r"^```(?:json)?\s*", "", response)
        response = re.sub(r"\s*```$", "", response)

        json_start = response.find("{")
        json_end = response.rfind("}") + 1
        if json_start == -1 or json_end == 0:
            return query, None

        data = _json.loads(response[json_start:json_end])
        ru = (data.get("ru") or "").strip() or query
        en = (data.get("en") or "").strip() or None
        if en and en.lower() in ("null", "none", "-", ""):
            en = None

        logger.info(f"[WEB_SEARCH] Запрос улучшен: '{query[:50]}' → ru='{ru[:50]}' en='{en or 'нет'}'")
        return ru, en

    except Exception as e:
        logger.debug(f"[WEB_SEARCH] Ошибка улучшения запроса: {e}")
        return query, None


def search_web(
    query: str,
    max_results: int = MAX_RESULTS,
    enhance: bool = True,
    en_query_override: str | None = None,
) -> list[dict]:
    """
    Ищет запрос в DuckDuckGo и возвращает список результатов.
    Если enhance=True — улучшает запрос через LLM и делает дополнительный поиск на английском.
    Если en_query_override задан — использует его вместо LLM-перевода (для rewriter'а).
    Для топ-результатов загружает полный текст страницы.

    Returns:
        [{"title": ..., "body": ..., "href": ..., "full_text": ...}, ...]
    """
    DDGS = _get_ddgs()
    if DDGS is None:
        logger.error("[WEB_SEARCH] Пакет ddgs не установлен")
        return []

    # Улучшаем запрос через LLM или используем готовый перевод от rewriter'а
    ru_query = query
    en_query = None
    if en_query_override:
        en_query = en_query_override
    elif enhance:
        ru_query, en_query = _enhance_query(query)

    def _run_search(q: str, limit: int) -> list[dict]:
        """Один поиск с фильтрацией по блэклисту."""
        try:
            ddgs = DDGS()
            raw = list(ddgs.text(q, max_results=limit))
            filtered = []
            for r in raw:
                url = r.get("href", "")
                if not _is_blacklisted(url):
                    filtered.append(r)
                else:
                    logger.debug(f"[WEB_SEARCH] Пропущен (blacklist): {url[:60]}")
            return filtered
        except Exception as e:
            logger.error(f"[WEB_SEARCH] Ошибка поиска '{q[:50]}': {e}")
            return []

    # Русскоязычный поиск
    ru_results = _run_search(ru_query, max_results)

    # Англоязычный поиск (если есть перевод)
    en_results = []
    if en_query:
        en_results = _run_search(en_query, max_results)

    # Мержим: дедупликация по URL, ru-результаты приоритетнее
    seen_urls: set[str] = set()
    merged: list[dict] = []

    for r in ru_results:
        url = r.get("href", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            r["_lang"] = "ru"
            merged.append(r)

    for r in en_results:
        url = r.get("href", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            r["_lang"] = "en"
            merged.append(r)

    if not merged:
        logger.info(f"[WEB_SEARCH] Нет результатов для: '{query[:60]}'")
        return []

    # Ограничиваем общее количество
    merged = merged[:max_results * 2]

    # Обрезаем длинные сниппеты
    for r in merged:
        if len(r.get("body", "")) > MAX_SNIPPET_LEN:
            r["body"] = r["body"][:MAX_SNIPPET_LEN] + "..."
        r["full_text"] = ""

    # Загружаем полный текст для топ-результатов
    for r in merged[:FETCH_TOP_N]:
        url = r.get("href", "")
        if url and _is_fetchable_url(url):
            full = fetch_page_text(url)
            if full:
                r["full_text"] = full
                logger.info(f"[WEB_SEARCH] Загружен текст: {url[:60]} ({len(full)} символов)")

    logger.info(
        f"[WEB_SEARCH] Найдено {len(merged)} результатов "
        f"(ru={len(ru_results)}, en={len(en_results)}) для: '{query[:60]}'"
    )
    return merged


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

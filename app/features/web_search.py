"""
Веб-поиск через DuckDuckGo.
Используется когда в контексте разговора/памяти нет ответа на вопрос.
"""

import re
import logging
from urllib.parse import urlparse

import httpx

from app.core.local_router import get_local_router

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


def _google_translate(text: str) -> str | None:
    """
    Переводит текст через Google Translate (deep_translator).
    Возвращает перевод или None при ошибке/недоступности.
    """
    try:
        from deep_translator import GoogleTranslator
        translator = GoogleTranslator(source="auto", target="en")
        result = translator.translate(text)
        if result and result.lower() != text.lower():
            return result
    except Exception as e:
        logger.debug(f"[WEB_SEARCH] Google Translate недоступен: {e}")
    return None


def _verify_translation(original: str, translated: str, router) -> bool:
    """
    Локальная LLM проверяет пару оригинал/перевод.
    Возвращает True если перевод корректен (Google не подменил термины синонимами).
    """
    if not router or not router.is_available():
        return True  # нет возможности проверить — считаем ок

    verify_prompt = (
        "Проверь перевод с русского на английский.\n"
        "Задача: убедись что Google Translate не заменил специфические термины "
        "(имена персонажей, названия игр, незнакомые слова) синонимами или дословным переводом.\n"
        "Ответь ТОЛЬКО 'OK' если перевод корректен, или 'FAIL' если есть подмена терминов.\n\n"
        f"Оригинал: {original}\n"
        f"Перевод: {translated}"
    )
    try:
        response = router.get_response(
            messages=[{"role": "user", "content": verify_prompt}],
            temperature=0.1,
            max_tokens=10,
        )
        if response:
            verdict = response.strip().upper()
            ok = verdict.startswith("OK")
            logger.info(f"[WEB_SEARCH] Верификация перевода: {verdict} | '{original[:40]}' -> '{translated[:40]}'")
            return ok
    except Exception as e:
        logger.debug(f"[WEB_SEARCH] Ошибка верификации перевода: {e}")
    return True  # при ошибке — считаем ок


def _enhance_query(query: str, history: list[dict] | None = None, persona_context: str | None = None) -> tuple[str, str | None]:
    """
    Улучшает поисковый запрос через локальную LLM.
    Учитывает историю диалога и контекст персоны.
    Возвращает (ru_query, en_query или None).
    """
    try:
        from app.core.query_enhancer import QueryEnhancer
        enhancer = QueryEnhancer()
        logger.info(f"[WEB_SEARCH] Улучшение запроса: '{query[:50]}' (history={len(history) if history else 0}, persona={'yes' if persona_context else 'no'})")
        enhanced = enhancer.enhance(query, history=history, persona_context=persona_context)
        logger.info(f"[WEB_SEARCH] Результат улучшения: '{query[:50]}' -> '{enhanced[:50]}'")
    except Exception as e:
        logger.debug(f"[WEB_SEARCH] Ошибка улучшения запроса: {e}")
        return query, None

    # Переводим на английский для расширения поиска
    # Схема: enhanced -> Google Translate -> LLM верификация -> en_query
    if not all(ord(c) < 128 for c in enhanced.replace(" ", "")):
        en_translated = _google_translate(enhanced)
        if en_translated:
            router = get_local_router()
            if _verify_translation(enhanced, en_translated, router):
                logger.info(f"[WEB_SEARCH] Перевод (Google+verify): '{enhanced[:50]}' -> en='{en_translated[:50]}'")
                return enhanced, en_translated
            else:
                logger.warning(f"[WEB_SEARCH] Перевод отклонён верификатором: '{en_translated[:50]}'")
        else:
            logger.info("[WEB_SEARCH] Google Translate недоступен, пропускаем английский поиск")
    else:
        logger.info("[WEB_SEARCH] Запрос уже на английском, перевод не нужен")

    return enhanced, None


def search_web(
    query: str,
    max_results: int = MAX_RESULTS,
    enhance: bool = True,
    en_query_override: str | None = None,
    history: list[dict] | None = None,
    persona_context: str | None = None,
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
        ru_query, en_query = _enhance_query(query, history=history, persona_context=persona_context)

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

    # Англоязычный поиск: 5 результатов (приоритет)
    en_results = []
    if en_query:
        en_results = _run_search(en_query, 5)

    # Русскоязычный поиск: 2 результата
    ru_results = _run_search(ru_query, 2)

    # Мержим: дедупликация по URL, en-результаты приоритетнее
    seen_urls: set[str] = set()
    merged: list[dict] = []

    for r in en_results:
        url = r.get("href", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            r["_lang"] = "en"
            merged.append(r)

    for r in ru_results:
        url = r.get("href", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            r["_lang"] = "ru"
            merged.append(r)

    if not merged:
        logger.info(f"[WEB_SEARCH] Нет результатов для: '{query[:60]}'")
        return []

    # Ограничиваем общее количество до 7
    merged = merged[:7]

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
        f"(en={len(en_results)}, ru={len(ru_results)}) для: '{query[:60]}'"
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

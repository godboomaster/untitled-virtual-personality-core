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
        "Check the translation from Russian to English.\n"
        "Task: make sure Google Translate did not replace specific terms "
        "(character names, game titles, unfamiliar words) with synonyms or a literal translation.\n"
        "Answer ONLY 'OK' if the translation is correct, or 'FAIL' if terms were substituted.\n\n"
        f"Original: {original}\n"
        f"Translation: {translated}"
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


# «Не тот сайт» для резолва «открой X»: соцсети, энциклопедии, магазины
# приложений. По запросу «сайт нгту» нужен nstu.ru, а не группа ВК или статья
# Википедии. Если запрос сам называет платформу («открой вк», «открой ютуб») —
# она и есть цель, не фильтруем (сверка по слагу названия/перевода).
_PLATFORM_DOMAINS = {
    "wikipedia.org": "wikipedia", "wikimedia.org": "wikimedia",
    "vk.ru": "vk", "vk.com": "vk", "ok.ru": "ok",
    "facebook.com": "facebook", "instagram.com": "instagram",
    "twitter.com": "twitter", "x.com": "x",
    "t.me": "telegram", "telegram.me": "telegram", "tiktok.com": "tiktok",
    "reddit.com": "reddit", "youtube.com": "youtube", "youtu.be": "youtube",
    "play.google.com": "googleplay", "apps.apple.com": "appstore",
    # контентные площадки: статьи/подборки О запрошенном, а не само запрошенное
    "grokipedia.com": "grokipedia", "pinterest.com": "pinterest",
}

# Падежные окончания для матчинга в резолве сайтов: «кутузовой» (запрос) и
# «КУТУЗОВА» (заголовок страницы) должны совпадать. Срезаем окончание,
# оставляя основу ≥ 4 символов — короче даёт слишком шумные совпадения.
_WORD_ENDINGS = (
    "ого", "его", "ому", "ему", "ыми", "ими", "ами", "ями",
    "ая", "яя", "ое", "ее", "ой", "ей", "ый", "ий", "ых", "их",
    "ом", "ем", "ам", "ям", "ах", "ях", "ов", "ев", "ью",
    "а", "я", "у", "ю", "о", "е", "ы", "и", "ь", "s",
)


def _stem(word: str) -> str:
    """Срез падежного окончания («кутузовой» → «кутузов»)."""
    for e in _WORD_ENDINGS:
        if word.endswith(e) and len(word) - len(e) >= 4:
            return word[:-len(e)]
    return word


def _match_word(qw: str, tw: str, text_slug: str) -> bool:
    """Слово запроса есть в слаге: само, его перевод или их основы."""
    for w in (qw, tw):
        if w and (w in text_slug or _stem(w) in text_slug):
            return True
    return False


def find_site_url(name: str, max_results: int = 10) -> str | None:
    """Лёгкий резолв «название сайта» → корневой URL сайта по выдаче DDG.

    Для fast-path «открой X» (computer_control): один поисковый вызов,
    БЕЗ LLM-улучшения запроса и БЕЗ загрузки полных текстов страниц —
    иначе быстрый путь перестаёт быть быстрым.

    Выбор результата:
    1. первый, чей домен содержит название (для кириллицы — через перевод:
       «ютуб» → «youtube» матчит www.youtube.com);
    2. для мультисловных названий — первый, у кого все слова запроса есть
       в домене+пути (сами или через перевод: «гугл карты» → google.com/maps —
       конкатенированный слаг ломался бы о порядок слов и смешанные языки);
    3. первый, чей заголовок содержит название/все его слова («НГТУ. …» →
       nstu.ru: домен вуза аббревиатуру не содержит, доменный матч не сработал бы);
    4. иначе None (запрос уходит в LLM-путь): открыть не тот сайт по первому
       результату хуже, чем спросить уточнение.
    Итоговый URL: доменный матч и однословный запрос — «корень + путь до первого
    сегмента со словом запроса» (nstu.ru, а не подстраница приёмной кампании;
    discord.com, а не чужой сервер; но google.de/intl/ru/maps для «гугл карт»);
    мультисловный матч по заголовку — страница целиком («кутузова нгту» →
    ciu.nstu.ru/kaf/persons/98849 — искомое как раз на ней).
    Домены из _PLATFORM_DOMAINS пропускаем, если запрос их самих не называет."""
    from urllib.parse import urlparse
    DDGS = _get_ddgs()
    if DDGS is None:
        logger.error("[WEB_SEARCH] Пакет ddgs не установлен")
        return None
    try:
        raw = list(DDGS().text(name, max_results=max_results))
    except Exception as e:
        logger.error(f"[WEB_SEARCH] Резолв сайта '{name[:50]}' не удался: {e}")
        return None

    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9а-яё]+", "", (s or "").lower())

    slugs = {_slug(name)}
    translated = _google_translate(name)
    if translated:
        slugs.add(_slug(translated))

    # Мультисловные названия («гугл карты» → maps.google.com): конкатенированный
    # слаг ломается о порядок слов в домене и о смешанные языки («Google Карты»).
    # Поэтому дополнительно матчим по словам: каждое значимое слово запроса должно
    # найтись в домене/заголовке — само или через перевод (пары слово↔перевод
    # строятся при совпадении числа слов, иначе — только слова запроса).
    q_words = [w for w in re.findall(r"[a-z0-9а-яё]+", name.lower()) if len(w) >= 3]
    t_words = [w for w in re.findall(r"[a-z0-9а-яё]+", (translated or "").lower()) if len(w) >= 3]
    pairs = (list(zip(q_words, t_words)) if len(q_words) == len(t_words)
             else [(w, w) for w in q_words])

    def _all_words_in(text_slug: str) -> bool:
        return bool(pairs) and all(
            _match_word(qw, tw, text_slug) for qw, tw in pairs)

    candidates = []
    for r in raw:
        url = r.get("href", "")
        # _is_blacklisted тут НЕ применяем: тот список — про нечитаемые для
        # текста страницы (youtube, соцсети), а резолву нужны именно они
        if not url or urlparse(url).scheme not in ("http", "https"):
            continue
        host = (urlparse(url).hostname or "").lower()
        platform = next((p for d, p in _PLATFORM_DOMAINS.items()
                         if host == d or host.endswith("." + d)), None)
        if platform and platform not in slugs:
            continue  # статья/группа О сайте, а не сам сайт
        candidates.append((url, r.get("title") or ""))
    if not candidates:
        logger.info(f"[WEB_SEARCH] Резолв '{name[:40]}': выдача пуста после фильтра — отказ")
        return None

    def _word_in(text_slug: str) -> bool:
        """Хотя бы одно слово запроса (или его перевод) есть в слаге."""
        return any(_match_word(qw, tw, text_slug) for qw, tw in pairs)

    multi = len(pairs) >= 2

    def _site_url(url: str) -> str:
        """Корень + путь до первого сегмента со словом запроса:
        google.de/intl/ru/maps/about → google.de/intl/ru/maps,
        nstu.ru/entrance/… → nstu.ru, чужой discord-сервер → discord.com."""
        p = urlparse(url)
        kept = []
        for seg in [s for s in p.path.split("/") if s]:
            kept.append(seg)
            if _word_in(_slug(seg)):
                break
        else:
            kept = []  # ни один сегмент не совпал — только корень
        return f"{p.scheme}://{p.netloc}" + ("/" + "/".join(kept) if kept else "/")

    for url, _title in candidates:
        p = urlparse(url)
        host = p.hostname or ""
        hosts = [host]
        try:  # кириллические домены: xn--c1atqe.xn--p1ai → «нгту.рф»
            hosts.append(host.encode("ascii").decode("idna"))
        except Exception:
            pass
        domain = " ".join(_slug(h) for h in hosts)
        if any(s and s in domain for s in slugs):
            logger.info(f"[WEB_SEARCH] Резолв '{name[:40]}' → {_site_url(url)[:60]} (домен)")
            return _site_url(url)
        # Мультисловный запрос: слова могут лежать в пути (google.com/maps)
        if multi and _all_words_in(domain + " " + _slug(p.path)):
            logger.info(f"[WEB_SEARCH] Резолв '{name[:40]}' → {_site_url(url)[:60]} (домен+путь)")
            return _site_url(url)
    for url, title in candidates:
        title_slug = _slug(title)
        if (any(s and len(s) >= 3 and s in title_slug for s in slugs)
                or _all_words_in(title_slug)):
            logger.info(f"[WEB_SEARCH] Резолв '{name[:40]}' → {url[:60]} (заголовок)")
            if multi:
                # Мультисловный запрос по заголовку — ищут СТРАНИЦУ
                # («кутузова нгту» → …/persons/98849): путь сохраняем целиком
                p = urlparse(url)
                return f"{p.scheme}://{p.netloc}{p.path or '/'}"
            return _site_url(url)
    logger.info(f"[WEB_SEARCH] Резолв '{name[:40]}': совпадения ни по домену, ни по заголовку — отказ")
    return None


def format_web_results(results: list[dict]) -> str:
    """
    Форматирует результаты в текст для вставки в промпт.
    Если есть full_text — использует его вместо сниппета.
    """
    if not results:
        return ""

    parts = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "No title")
        href = r.get("href", "")

        # Полный текст приоритетнее сниппета
        body = r.get("full_text") or r.get("body", "")
        if not body:
            continue

        parts.append(f"{i}. {title}\n   {body}\n   Source: {href}")

    return "\n\n".join(parts)

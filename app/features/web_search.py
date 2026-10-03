"""
Веб-поиск через DuckDuckGo (резолв сайтов — также через веб-Google в пуле H).
Используется когда в контексте разговора/памяти нет ответа на вопрос.
"""

import inspect
import ipaddress
import json
import re
import socket
import threading
import time
import logging
from urllib.parse import urlparse

import httpx

from app.core.language import detect_language, user_language_line
from app.core.local_router import get_local_router
from app.core.router import internet_available
from app.core.word_stem import WORD_ENDINGS as _WORD_ENDINGS
from app.core.word_stem import stem as _stem

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
# Редиректов у одной загрузки — не больше (иначе можно зациклиться/затянуть запрос)
MAX_REDIRECTS = 5


# Последний резолв _resolve_checked на ЭТОМ потоке (host -> ip). Позволяет
# get_with_safe_redirects взять для соединения ровно тот адрес, что уже
# прошёл проверку в is_safe_public_url — без второго getaddrinfo. Под
# конкурентными потоками бота гонки нет: запись и чтение — на одном потоке,
# между ними нет операций ввода-вывода (см. _pinned_get).
_last_resolved = threading.local()


def _resolve_checked(host: str) -> "ipaddress._BaseAddress | None":
    """Резолвит host, проверяет ВСЕ адреса на публичность и возвращает
    первый (для соединения) — или None, если резолв не удался или среди
    адресов есть непубличный (приватный/loopback/link-local/reserved/
    multicast/unspecified, включая IPv4-in-IPv6 вида ``::ffff:127.0.0.1``).

    Единственное место, которое реально резолвит имя для SSRF-проверки —
    результат кладётся в _last_resolved, чтобы соединение (_pinned_get)
    использовало тот же адрес, а не резолвило host заново (иначе между
    проверкой и коннектом DNS мог бы отдать другой ответ — rebinding)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        infos = None
    ip = None
    if infos:
        ips = []
        for info in infos:
            raw_ip = info[4][0]
            try:
                ip_i = ipaddress.ip_address(raw_ip.split("%", 1)[0])  # без zone id у IPv6 link-local
            except ValueError:
                ips = None
                break
            candidates = [ip_i]
            mapped = getattr(ip_i, "ipv4_mapped", None)  # ::ffff:10.0.0.1 → 10.0.0.1
            if mapped is not None:
                candidates.append(mapped)
            if any(c.is_private or c.is_loopback or c.is_link_local
                   or c.is_reserved or c.is_multicast or c.is_unspecified
                   for c in candidates):
                ips = None
                break
            ips.append(ip_i)
        if ips:
            ip = ips[0]
    _last_resolved.host = host
    _last_resolved.ip = ip
    return ip


def _cached_resolved_ip(host: str):
    """IP из ПОСЛЕДНЕГО _resolve_checked(host) на этом потоке — или None,
    если такого резолва не было (например, is_safe_public_url подменена
    в тестах и реальный резолв не выполнялся)."""
    if getattr(_last_resolved, "host", None) == host:
        return getattr(_last_resolved, "ip", None)
    return None


def is_safe_public_url(url: str) -> bool:
    """SSRF-фильтр: True — только для http(s)-URL, чей
    хост резолвится ИСКЛЮЧИТЕЛЬНО в публичные адреса.

    URL страниц из выдачи поиска фактически приходит от третьей стороны
    (DuckDuckGo, и содержимое найденных страниц) — без проверки
    ``fetch_page_text`` был бы SSRF: страница/редирект на
    ``http://169.254.169.254/...`` (облачные метаданные), ``http://localhost:PORT``
    или адрес внутри локальной сети ушли бы обычным GET с сервера.

    Резолвим ВСЕ адреса имени (getaddrinfo может вернуть и публичный, и
    приватный A/AAAA для одного домена — «безопасно» = ни одного
    непубличного среди всех вариантов, не «хотя бы один публичный»).
    Покрывает IPv4 и IPv6, включая IPv4-in-IPv6 (``::ffff:127.0.0.1``).

    DNS rebinding (резолв здесь и резолв на момент реального коннекта у
    httpx расходятся) закрыт на стороне соединения: get_with_safe_redirects
    подключается по адресу, резолвленному ЗДЕСЬ же (_resolve_checked), а не
    резолвит host заново — второго resolve между проверкой и GET просто нет.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    return _resolve_checked(host) is not None

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


def _pinned_get(client, url: str, host: str | None, ip):
    """GET по адресу, уже прошедшему проверку в is_safe_public_url в ЭТОМ же
    хопе (ip — из _cached_resolved_ip, тот же резолв, что и для проверки):
    URL для соединения строится с хостом-IP, поэтому httpx второй раз имя не
    резолвит — разрыв «проверили → пошли по сети другим адресом» (DNS
    rebinding) закрыт тем, что резолв и соединение используют один ответ.
    Host-заголовок и extensions["sni_hostname"] сохраняют для сервера и TLS
    исходное имя — httpcore использует sni_hostname как server_hostname и
    для SNI, и для проверки сертификата (httpcore._sync.connection.
    HTTPConnection._connect, httpx/httpcore ≥0.24), так что сертификат
    по-прежнему сверяется с именем хоста, а не с IP.

    Если ip не резолвлен (is_safe_public_url подменена — тесты, реального
    резолва не было) или client — тестовый фейк без keyword-параметров
    headers/extensions (например ``def get(self, url)``) — коннект идёт
    обычным способом, по имени хоста: пиннинг не применяется, эти параметры
    нужны только для него."""
    if ip is None or host is None:
        return client.get(url)
    try:
        params = inspect.signature(client.get).parameters
    except (TypeError, ValueError):
        params = {}
    has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    if not has_kwargs and not ("headers" in params and "extensions" in params):
        return client.get(url)
    pinned = httpx.URL(url).copy_with(host=str(ip))
    return client.get(
        str(pinned),
        headers={"Host": host},
        extensions={"sni_hostname": host},
    )


def get_with_safe_redirects(client, url: str):
    """GET стороннего URL с SSRF-фильтром (is_safe_public_url) на исходном
    адресе И на КАЖДОМ хопе редиректа — единственный обход цепочки в
    проекте (fetch_page_text, computer_control._first_result_url).
    Клиент обязан быть с follow_redirects=False — иначе httpx пройдёт
    цепочку сам, без проверки хопов.

    Каждый хоп резолвится РОВНО один раз (is_safe_public_url →
    _resolve_checked): соединение (_pinned_get) идёт по адресу из этого же
    резолва, а не по новому — см. _pinned_get про DNS rebinding.
    → (ответ, финальный URL) или (None, причина отказа)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not is_safe_public_url(current):
            if current == url:
                return None, f"адрес недоступен для загрузки: {current[:80]}"
            return None, f"редирект на недоступный адрес: {current[:80]}"
        host = urlparse(current).hostname
        ip = _cached_resolved_ip(host) if host else None
        resp = _pinned_get(client, current, host, ip)
        if not getattr(resp, "is_redirect", False):
            return resp, current
        location = resp.headers.get("location")
        if not location:
            return resp, current  # редирект без Location — читаем как есть
        current = str(httpx.URL(current).join(location))
    return None, f"слишком много редиректов (>{MAX_REDIRECTS}): {url[:80]}"


def fetch_page_text(url: str, max_len: int = MAX_PAGE_TEXT_LEN) -> str:
    """
    Загружает страницу и извлекает основной текст (без HTML-тегов, скриптов, стилей).

    Возвращает чистый текст длиной до max_len символов, или пустую строку при ошибке.

    SSRF-фильтр (is_safe_public_url) — на исходном URL И на каждом хопе
    редиректа: follow_redirects=True сам по себе не проверяет, куда сервер
    уводит запрос, а публичная страница может 302-нуть на
    http://169.254.169.254/latest/meta-data/ или localhost:6379.
    """
    if not is_safe_public_url(url):
        logger.debug(f"[WEB_SEARCH] SSRF-фильтр: адрес недоступен для загрузки: {url[:80]}")
        return ""
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=PAGE_FETCH_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru,en;q=0.9",
            },
        ) as client:
            resp, why = get_with_safe_redirects(client, url)
            if resp is None:
                logger.debug(f"[WEB_SEARCH] SSRF-фильтр: {why}")
                return ""
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
    # AI-генераторы контента (могут быть галлюцинации)
    "chatgpt.com", "openai.com",
}


def _is_blacklisted(url: str) -> bool:
    # Проверяет URL по чёрному списку доменов.
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
        f"Translation: {translated}\n\n"
        f"{user_language_line(detect_language(original))} "
        "The verdict itself is always exactly OK or FAIL."
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


def _enhance_query(query: str, history: list[dict] | None = None, persona_context: str | None = None,
                   verify_translation: bool = False,
                   local_router=None) -> tuple[str, str | None]:
    """
    Улучшает поисковый запрос через локальную LLM.
    Учитывает историю диалога и контекст персоны.
    Возвращает (ru_query, en_query или None).
    verify_translation=False — перевод Google принимается без LLM-проверки
    (экономит один вызов локальной модели на каждый поиск).
    """
    try:
        from app.core.query_enhancer import QueryEnhancer
        enhancer = QueryEnhancer(router=local_router)
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
            if not verify_translation:
                logger.info(f"[WEB_SEARCH] Перевод (Google): '{enhanced[:50]}' -> en='{en_translated[:50]}'")
                return enhanced, en_translated
            router = local_router or get_local_router()
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
    verify_translation: bool = False,
    fetch_pages: bool = True,
    local_router=None,
) -> list[dict]:
    """
    Ищет запрос в DuckDuckGo и возвращает список результатов.
    fetch_pages=False — только сниппеты, без загрузки страниц (до 2×10 с):
    так идёт нога DDG в гонке с AI Mode (web_search_race).
    Если enhance=True — улучшает запрос через LLM и делает дополнительный поиск на английском.
    Если en_query_override задан — использует его вместо LLM-перевода (для rewriter'а).
    verify_translation=True — дополнительно проверяет Google-перевод локальной LLM.
    local_router — локальный роутер персоны (движки её служебных задач).
    Для топ-результатов загружает полный текст страницы.

    Returns:
        [{"title": ..., "body": ..., "href": ..., "full_text": ...}, ...]
    """
    # Офлайн: оба поиска + загрузка страниц заведомо мертвы — пропускаем сразу,
    # иначе это до 25 с ожидания ответа и ERROR-спам от ddgs
    if not internet_available():
        logger.info(f"[WEB_SEARCH] Нет интернета — поиск пропущен: '{query[:60]}'")
        return []

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
        ru_query, en_query = _enhance_query(
            query, history=history, persona_context=persona_context,
            verify_translation=verify_translation, local_router=local_router)

    def _run_search(q: str, limit: int) -> list[dict]:
        # Один поиск с фильтрацией по блэклисту.
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
    for r in (merged[:FETCH_TOP_N] if fetch_pages else []):
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
# приложений. По запросу «сайт вуза» нужен официальный домен вуза, а не его
# группа в соцсети или статья в энциклопедии. Если запрос сам называет
# платформу («открой вк», «открой ютуб») — она и есть цель, не фильтруем
# (сверка по слагу названия/перевода).
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

# _WORD_ENDINGS/_stem — алиасы на app.core.word_stem: таблица окончаний и
# сама функция стемминга живут в отдельном stdlib-модуле (его же, без
# httpx/openai, импортирует browser_actions). Имена оставлены прежними,
# чтобы остальной проект (scenario_manager, computer_control,
# browser_history), делающий `from app.features.web_search import _stem`,
# продолжал работать без изменений.


def _match_word(qw: str, tw: str, text_slug: str) -> bool:
    # Слово запроса есть в слаге: само, его перевод или их основы.
    for w in (qw, tw):
        if w and (w in text_slug or _stem(w) in text_slug):
            return True
    return False


# ── Выдача веб-Google (headless пул H) ──
# Google точнее DDG на узких запросах («человек + организация»: страница
# преподавателя на сайте вуза), но у него нет бесплатного API выдачи — читаем
# страницу поиска во вкладке уже поднятого пула H (того же, где живёт AI Mode).
# udm=14 — фильтр «Веб»: только обычные ссылки, без ИИ-обзора и каруселей;
# селектор «#rso a:has(h3)» и без него отсекает рекламу (#tads) и ИИ-обзор
# (над #rso). Капча не обходится: сайт уходит в карантин, резолв — в DDG.
GOOGLE_SEARCH_URL = "https://www.google.com/search?udm=14&q="
# Ключ карантина общий с веб-чатом AI Mode: тот же профиль и IP — капча
# у одного означает капчу у другого
GOOGLE_QUARANTINE_SITE = "google"
GOOGLE_WAIT_SEC = 8.0      # потолок ожидания выдачи во вкладке

# Ждёт выдачу (или страницу капчи) и отдаёт JSON: sorry — антибот-стена
# Google (/sorry/, форма капчи), rso — контейнер органики есть (его пропажа
# без капчи — смена разметки), links — [заголовок, href, сниппет] органических
# ссылок (сниппета нет — пустая строка: его разметка меняется чаще ссылок)
_GOOGLE_RESULTS_JS = (
    "(async()=>{const t0=Date.now();"
    "const sorry=()=>/^\\/sorry\\//.test(location.pathname)"
    "||!!document.querySelector('#captcha-form,form[action*=\"/sorry/\"]');"
    "while(Date.now()-t0<" + str(int(GOOGLE_WAIT_SEC * 1000)) + "){"
    "if(document.querySelector('#rso')||sorry())break;"
    "if(document.readyState==='complete'&&Date.now()-t0>2500)break;"
    "await new Promise(r=>setTimeout(r,150));}"
    "const links=[...document.querySelectorAll('#rso a:has(h3)')].map(a=>{"
    "const box=a.closest('[data-hveid][data-ved],.MjjYud,.g');"
    "const sn=box&&box.querySelector('[data-sncf],.VwiC3b,"
    "[style*=\"-webkit-line-clamp\"]');"
    "return [a.querySelector('h3').innerText.trim(),a.href,"
    "sn?sn.innerText.trim():''];});"
    "return JSON.stringify({sorry:sorry(),"
    "rso:!!document.querySelector('#rso'),links});})()"
)


def _google_unwrap(href: str) -> str:
    """Редирект-обёртка Google (/url?q=…) → целевой адрес; прочее как есть."""
    from urllib.parse import parse_qs
    p = urlparse(href)
    if re.match(r"^(www\.)?google\.", p.hostname or "") and p.path == "/url":
        qs = parse_qs(p.query)
        return (qs.get("q") or qs.get("url") or [""])[0]
    return href


# ── Проба капчи поиска в rescue пула H ──
# Капча поиска ставит общий с AI Mode карантин «google» (вид challenge, пул
# H), и rescue пула H (Chrome бота видимый, человек проходит капчи руками)
# ждёт его снятия (web_llm._rescue_pending_sites). Снять его было некому:
# поиск в карантине Google сразу пропускает, open_headless_tab в rescue
# отказывает (иначе окно выдачи выскакивало бы на каждый фоновый поиск), а
# веб-чат google, чей _challenge_check снимает карантин по чистой странице,
# есть не у всех персон. Rescue держался до конца срока (15 мин), каждая
# новая вкладка веб-чатов всё это время выскакивала окном, а окна с капчей
# поиска у человека не было вовсе — решить её было негде.
# Поэтому в rescue поиск сам — проба своей капчи: держит ОДНО окно выдачи
# (его открытие — тот же единственный запрос к Google, что сделал бы
# обычный поиск), человек решает в нём капчу, а следующие поиски только
# смотрят на окно — БЕЗ навигации: она сорвала бы решение посреди капчи и
# была бы лишним запросом к Google (за частые запросы уже банили). Чисто —
# карантин снят, окно закрыто, rescue завершается по общему правилу
# (_finish_rescue_if_done). Вне rescue окно не держим.
# Лок — только неблокирующий: поиски идут из разных потоков, замер пробы
# длится до ~12 с, и сосед не должен ни ждать его, ни открыть вторую пробу
# (он просто уходит в DDG, как и без пробы).
_GOOGLE_PROBE_LOCK = threading.Lock()
_GOOGLE_PROBE_TAB: int | None = None  # фоновая вкладка пула H (окно rescue)
# Хост Google (www./consent./ccTLD) — проба, ушедшая с него, капчу поиска
# уже не показывает
_GOOGLE_HOST_RE = re.compile(r"(^|\.)google\.[a-z]{2,3}(\.[a-z]{2})?$")
# Замер пробы — тот же _GOOGLE_RESULTS_JS плюс адрес страницы, на которой он
# сделан. Адрес отдельным вызовом (tab_url) мог оказаться уже от другой
# страницы: свежая проба, прочитанная ещё на about:blank (навигация не
# дошла), с адресом, снятым мигом позже уже на Google, сошла бы за «чисто»
_GOOGLE_PROBE_JS = ("(async()=>{const r=JSON.parse(await " + _GOOGLE_RESULTS_JS
                    + ");r.href=location.href;return JSON.stringify(r);})()")


def _drop_google_probe(ba, why: str) -> None:
    """Закрыть и забыть окно-пробу. Только под _GOOGLE_PROBE_LOCK. Закрытие
    best effort: вкладки умершего Chrome уже нет в реестре (close — no-op)."""
    global _GOOGLE_PROBE_TAB
    tab, _GOOGLE_PROBE_TAB = _GOOGLE_PROBE_TAB, None
    if tab is None:
        return
    try:
        ba.close_background_tab(tab)
    except Exception as e:
        logger.debug(f"[WEB_SEARCH] Google: окно-проба #{tab} не "
                     f"закрылось: {e}")
    logger.info(f"[WEB_SEARCH] Google: окно-проба капчи #{tab} "
                f"отпущено ({why})")


def _release_google_probe(ba, why: str) -> None:
    # Проба больше не нужна (rescue окончен, карантина капчи нет). Лок занят —
    # пробу прямо сейчас смотрит сосед, и он же её отпустит
    if _GOOGLE_PROBE_TAB is None or not _GOOGLE_PROBE_LOCK.acquire(
            blocking=False):
        return
    try:
        _drop_google_probe(ba, why)
    finally:
        _GOOGLE_PROBE_LOCK.release()


def _check_google_probe(ba, tab: int) -> tuple[str, dict, str]:
    """Состояние окна-пробы БЕЗ навигации (только чтение DOM: человек может
    решать в нём капчу прямо сейчас) → (вердикт, выдача, url):
    dead — вкладки нет (Chrome пула перезапущен: rescue окончен или «почини
    браузер» ещё раз; окно закрыли) или в ней уже не Google (человек ушёл
    на другой сайт — капчу поиска там не пройти);
    challenge — капча на месте;
    unknown — замер не удался или страница ещё не дошла до адреса. Это не
    «чисто»: карантин по нему не снимается (как в web_llm._challenge_check —
    иначе сбой детектора снимал бы карантин вслепую);
    clear — Google без капчи."""
    # Вкладка выпала из реестра — её Chrome сброшен. Проверка ДО вызовов:
    # чужой (не фоновый) tab_id ушёл бы в playwright-воркер пула V
    if not ba.is_raw_tab(tab):
        return "dead", {}, ""
    try:
        data = json.loads(ba.eval_js(None, tab, _GOOGLE_PROBE_JS,
                                     timeout_sec=GOOGLE_WAIT_SEC + 4) or "{}")
        if not isinstance(data, dict):
            raise ValueError("выдача пробы — не объект")
        label = ba.detect_antibot(None, tab, strict=True)
    except Exception as e:
        # Вызов сам выбросил вкладку из реестра — таргета больше нет
        # (_raw_eval: закрыта, Chrome умер). Иначе вкладка жива, а не удался
        # только замер (таймаут, навигация посреди чтения)
        if not ba.is_raw_tab(tab):
            return "dead", {}, ""
        logger.info(f"[WEB_SEARCH] Google: окно-пробу капчи проверить не "
                    f"удалось ({str(e)[:160]}) — состояние неизвестно, "
                    "карантин не трогаю")
        return "unknown", {}, ""
    url = str(data.get("href") or "")
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "unknown", data, url  # about:blank — навигация ещё не дошла
    if not _GOOGLE_HOST_RE.search(host):
        return "dead", data, url
    # Метка антибота при ОТРИСОВАННОЙ выдаче (#rso) — ложная: detect_antibot
    # ловит и заголовок, а у выдачи он — сам запрос («captcha …», «доступ
    # запрещён 403 …»). Стена Google — это /sorry/ (признак sorry), выдачу
    # она не показывает; без исключения проба с таким запросом не снялась
    # бы никогда, и rescue держался бы весь срок
    if data.get("sorry") or (label and not data.get("rso")):
        return "challenge", data, url
    return "clear", data, url


def _probe_on_query(url: str, query: str) -> bool:
    """Проба стоит на выдаче ЭТОГО запроса (после капчи Google возвращает
    на исходный адрес пробы) — её выдачу можно взять вместо нового запроса."""
    from urllib.parse import parse_qs
    p = urlparse(url)
    qs = parse_qs(p.query)
    return (p.path == "/search" and (qs.get("udm") or [""])[0] == "14"
            and (qs.get("q") or [""])[0].strip() == query.strip())


def _google_rescue_probe(ba, query: str) -> tuple[bool, dict | None]:
    """Google в карантине — проба его капчи в rescue пула H → (снят, выдача).
    снят=False — карантин держится (или rescue нет): поиск отдаёт None, как и
    раньше. снят=True — капча пройдена, карантин снят; выдача — прочитанная
    прямо из пробы, если та стоит на этом же запросе (второй запрос к Google
    не нужен), иначе None — обычный путь поиска (rescue ещё идёт из-за других
    сайтов — open_headless_tab откажет, будет DDG).
    Вне rescue ничего не открывает и не читает — только отпускает
    оставшуюся пробу. Запросов к Google — не больше одного за вызов (открытие
    пробы, когда её нет), повторов нет."""
    global _GOOGLE_PROBE_TAB
    from urllib.parse import quote_plus
    from app.features.web_llm import (
        _finish_rescue_if_done, clear_quarantine, quarantine_kind)
    try:
        rescue = bool(ba.pool_h_rescue_active())
    except Exception:
        rescue = False
    if not rescue:
        _release_google_probe(ba, "rescue окончен")
        return False, None
    if quarantine_kind(GOOGLE_QUARANTINE_SITE) != "challenge":
        # Лимит/отказ руками не снять, а чистая выдача их не опровергает
        # (так же их не пробует в rescue и web_llm._quarantine_skip)
        _release_google_probe(ba, "карантин не капча")
        return False, None
    if not _GOOGLE_PROBE_LOCK.acquire(blocking=False):
        return False, None  # пробу сейчас смотрит/открывает соседний поиск
    try:
        tab = _GOOGLE_PROBE_TAB
        verdict, data, url = ("dead", {}, "") if tab is None \
            else _check_google_probe(ba, tab)
        if verdict == "dead":
            if tab is not None:
                _drop_google_probe(ba, "вкладка пробы умерла или ушла "
                                       "с Google")
            try:
                tab = ba.open_headless_tab(
                    GOOGLE_SEARCH_URL + quote_plus(query), rescue_probe=True)
            except Exception as e:
                logger.info(f"[WEB_SEARCH] Google: окно-проба капчи не "
                            f"открылось ({e})")
                return False, None
            _GOOGLE_PROBE_TAB = tab
            logger.info(f"[WEB_SEARCH] Google в карантине, rescue пула H — "
                        f"окно-проба капчи #{tab} открыто")
            verdict, data, url = _check_google_probe(ba, tab)
            if verdict == "dead":
                # Свежая проба уже не на Google (или умерла): новую — только
                # следующим поиском, повторов в одном вызове нет
                _drop_google_probe(ba, "свежая проба не на Google")
                return False, None
        if verdict != "clear":
            return False, None
        clear_quarantine(GOOGLE_QUARANTINE_SITE)
        logger.info("[WEB_SEARCH] Google: капча поиска пройдена — карантин "
                    "снят")
        _drop_google_probe(ba, "капча пройдена")
    finally:
        _GOOGLE_PROBE_LOCK.release()
    try:
        # Rescue завершится, только если капч/входов больше не ждём
        _finish_rescue_if_done(ba, cleared=GOOGLE_QUARANTINE_SITE)
    except Exception as e:
        logger.debug(f"[WEB_SEARCH] Google: проверка конца rescue: {e}")
    return True, (data if data.get("rso") and _probe_on_query(url, query)
                  else None)


def _google_parse_links(data: dict, max_results: int) -> list[dict]:
    """Ссылки выдачи (JSON _GOOGLE_RESULTS_JS) → [{"href", "title", "body"}]:
    без служебных ссылок Google и дублей, не больше max_results."""
    out, seen = [], set()
    for title, href, *rest in data.get("links") or []:
        url = _google_unwrap(str(href or ""))
        p = urlparse(url)
        if p.scheme not in ("http", "https") or re.match(
                r"^(www\.)?google\.", p.hostname or ""):
            continue  # служебные ссылки самого Google (картинки, «ещё»)
        key = ((p.hostname or "").lower(), p.path.rstrip("/"), p.query)
        if key in seen:
            continue
        seen.add(key)
        out.append({"href": url, "title": str(title or ""),
                    "body": str(rest[0] if rest else "")})
        if len(out) >= max_results:
            break
    return out


def google_web_links(query: str, max_results: int = 10) -> list[dict] | None:
    """Органическая выдача веб-Google → [{"href", "title", "body"}] (формат
    DDGS.text; body — сниппет, может быть пустым).

    None — Google недоступен и вызывающему нужен другой поисковик: нет
    интернета, пул H не поднят (ради одного поиска браузер не запускаем),
    Google в карантине, капча (→ карантин), разметка не распознана
    (warning в лог — иначе поломка выглядела бы как «ничего не нашлось»).
    В rescue пула H карантин капчи проверяет окно-проба
    (_google_rescue_probe): капча пройдена — карантин снят, поиск идёт
    дальше."""
    from urllib.parse import quote_plus
    from app.features import browser_actions as ba
    from app.features.web_llm import quarantine_site, site_quarantined
    if not internet_available():
        return None
    if site_quarantined(GOOGLE_QUARANTINE_SITE):
        cleared, probe_data = _google_rescue_probe(ba, query)
        if not cleared:
            logger.info("[WEB_SEARCH] Google в карантине — выдача не читается")
            return None
        if probe_data is not None:
            return _google_parse_links(probe_data, max_results)
    else:
        # Карантин снят не пробой (веб-чат google, срок) — окно не держим
        _release_google_probe(ba, "карантина Google нет")
    try:
        tab_id = ba.open_headless_tab(GOOGLE_SEARCH_URL + quote_plus(query))
    except Exception as e:
        logger.info(f"[WEB_SEARCH] Google: вкладка не открылась ({e})")
        return None
    try:
        raw = ba.eval_js(None, tab_id, _GOOGLE_RESULTS_JS,
                         timeout_sec=GOOGLE_WAIT_SEC + 4)
        data = json.loads(raw or "{}")
        if data.get("sorry") or ba.detect_antibot(None, tab_id):
            quarantine_site(GOOGLE_QUARANTINE_SITE, "капча в поиске Google")
            return None
        if not data.get("rso"):
            logger.warning(f"[WEB_SEARCH] Google: нет блока выдачи (#rso) по "
                           f"'{query[:50]}' — разметка изменилась?")
            return None
    except Exception as e:
        logger.info(f"[WEB_SEARCH] Google: выдача не прочитана ({e})")
        return None
    finally:
        ba.close_background_tab(tab_id)
    return _google_parse_links(data, max_results)


def _ddg_links(query: str, max_results: int) -> list[dict] | None:
    """Выдача DDG → [{"href", "title", …}]; None — пакет/сеть недоступны."""
    DDGS = _get_ddgs()
    if DDGS is None:
        logger.error("[WEB_SEARCH] Пакет ddgs не установлен")
        return None
    try:
        return list(DDGS().text(query, max_results=max_results))
    except Exception as e:
        logger.error(f"[WEB_SEARCH] DDG по '{query[:50]}' не удался: {e}")
        return None


def search_links(query: str, max_results: int = 10,
                 engine: str = "google") -> tuple[list[dict], str]:
    """Ссылки выдачи → ([{"href", "title", "body"}], движок). engine="google" —
    веб-Google, при его недоступности DDG (движок в ответе — фактический)."""
    if engine == "google":
        links = google_web_links(query, max_results)
        if links is not None:
            return links, "google"
        logger.info(f"[WEB_SEARCH] Google недоступен — DDG для '{query[:50]}'")
    return _ddg_links(query, max_results) or [], "ddg"


# Выдача последних резолвов (после фильтра платформ) — варианты для списка
# «какой сайт открыть?»: computer_control берёт их сразу после find_site_url,
# второй запрос к поисковику не нужен
SITE_CHOICES_TTL_SEC = 120.0
_SITE_CHOICES_LOCK = threading.Lock()
_SITE_CHOICES: dict[str, tuple[float, list]] = {}


def _choices_key(name: str) -> str:
    return " ".join(str(name or "").lower().split())


def _remember_site_choices(name: str, candidates: list) -> None:
    now = time.time()
    with _SITE_CHOICES_LOCK:
        for k in [k for k, (ts, _c) in _SITE_CHOICES.items()
                  if now - ts > SITE_CHOICES_TTL_SEC]:
            del _SITE_CHOICES[k]
        _SITE_CHOICES[_choices_key(name)] = (now, list(candidates))


def site_choices(name: str) -> list[tuple[str, str]]:
    """Варианты (url, заголовок) из выдачи последнего find_site_url по этому
    имени, в порядке выдачи; пусто — резолва не было или он устарел."""
    with _SITE_CHOICES_LOCK:
        ts, cands = _SITE_CHOICES.get(_choices_key(name), (0.0, []))
    return list(cands) if time.time() - ts <= SITE_CHOICES_TTL_SEC else []


def find_site_url(name: str, max_results: int = 10,
                  engine: str = "google") -> str | None:
    """Лёгкий резолв «название сайта» → корневой URL сайта по выдаче поисковика.

    Для fast-path «открой X» (computer_control): один поисковый вызов,
    БЕЗ LLM-улучшения запроса и БЕЗ загрузки полных текстов страниц —
    иначе быстрый путь перестаёт быть быстрым.

    Выбор результата:
    1. первый, чей домен содержит название (для кириллицы — через перевод:
       «ютуб» → «youtube» матчит www.youtube.com);
    2. для мультисловных названий — первый, у кого все слова запроса есть
       в домене+пути (сами или через перевод) — конкатенированный слаг
       ломался бы о порядок слов и смешанные языки;
    3. первый, чей заголовок содержит название/все его слова (например,
       аббревиатура учреждения есть в заголовке, но не в домене — доменный
       матч тогда не сработал бы);
    4. иначе None (запрос уходит в LLM-путь): открыть не тот сайт по первому
       результату хуже, чем спросить уточнение.
    Итоговый URL: доменный матч и однословный запрос — «корень + путь до
    первого сегмента со словом запроса» (домен учреждения, а не подстраница
    приёмной кампании; корень сервиса, а не чужая страница на нём); мультисловный матч по
    заголовку — страница целиком (например, запрос «человек + организация»
    может резолвиться прямо на страницу этого человека).
    Домены из _PLATFORM_DOMAINS пропускаем, если запрос их самих не называет.
    engine — поисковик выдачи: "google" (веб-Google в пуле H, при его
    недоступности — DDG; см. search_links) или "ddg"."""
    from urllib.parse import urlparse
    # Офлайн: резолв через поисковик заведомо мёртв — сразу None (запрос
    # уходит в LLM-путь, как при отсутствии результата)
    if not internet_available():
        logger.info(f"[WEB_SEARCH] Нет интернета — резолв сайта пропущен: '{name[:60]}'")
        return None
    raw, _engine = search_links(name, max_results, engine=engine)
    if not raw:
        return None

    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9а-яё]+", "", (s or "").lower())

    slugs = {_slug(name)}
    translated = _google_translate(name)
    if translated:
        slugs.add(_slug(translated))

    # Мультисловные названия: конкатенированный слаг ломается о порядок слов
    # в домене и о смешанные языки (слова запроса на разных языках).
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
    _remember_site_choices(name, candidates)
    if not candidates:
        logger.info(f"[WEB_SEARCH] Резолв '{name[:40]}': выдача пуста после фильтра — отказ")
        return None

    def _word_in(text_slug: str) -> bool:
        # Хотя бы одно слово запроса (или его перевод) есть в слаге.
        return any(_match_word(qw, tw, text_slug) for qw, tw in pairs)

    multi = len(pairs) >= 2

    def _site_url(url: str) -> str:
        """Корень + путь до первого сегмента со словом запроса: длинный путь
        сворачивается до найденного раздела (например,
        example.com/intl/ru/maps/about → example.com/intl/ru/maps); ни один
        сегмент не совпал (например, ссылка на чужую страницу внутри сервиса) —
        только корень домена."""
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
        try:  # кириллические домены: punycode → читаемый вид (например, «сайт.рф»)
            hosts.append(host.encode("ascii").decode("idna"))
        except Exception:
            pass
        domain = " ".join(_slug(h) for h in hosts)
        if any(s and s in domain for s in slugs):
            logger.info(f"[WEB_SEARCH] Резолв '{name[:40]}' → {_site_url(url)[:60]} (домен)")
            return _site_url(url)
        # Мультисловный запрос: слова могут лежать в пути, а не только в домене
        if multi and _all_words_in(domain + " " + _slug(p.path)):
            logger.info(f"[WEB_SEARCH] Резолв '{name[:40]}' → {_site_url(url)[:60]} (домен+путь)")
            return _site_url(url)
    for url, title in candidates:
        title_slug = _slug(title)
        if (any(s and len(s) >= 3 and s in title_slug for s in slugs)
                or _all_words_in(title_slug)):
            logger.info(f"[WEB_SEARCH] Резолв '{name[:40]}' → {url[:60]} (заголовок)")
            if multi:
                # Мультисловный запрос по заголовку — ищут конкретную
                # страницу (например, «человек + организация»): путь
                # сохраняем целиком
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

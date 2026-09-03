"""Локальная история браузера — персональный сигнал релевантности сайтов.

Chrome/Edge/Chromium и Firefox хранят историю в SQLite — читаем напрямую
(файл сначала копируется во временный: работающий браузер держит свою БД
залоченной). Safari на macOS закрыт TCC (Full Disk Access) — прочитается,
только если права уже выданы, иначе тихо пропускаем.

Всё read-only и не покидает машину: слова запроса сравниваются с доменами и
заголовками локально, в логи история не пишется. Результат кэшируется (TTL):
история нужна резолву «открой X» (computer_control) как подсказка «что
пользователь имеет в виду», а не как постоянный мониторинг.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 600  # история меняется непрерывно, резолву хватает 10 мин свежести
_MIN_VISITS = 3       # одиночные случайные визиты — не «частые сайты»
_MAX_ENTRIES = 500    # топ по посещаемости с каждого браузера

# url, title, visits, last_visit_unix
Entry = Tuple[str, str, int, float]

_CHROME_TS_OFFSET = 11644473600000000  # микросекунды между 1601-01-01 и epoch
_SAFARI_TS_OFFSET = 978307200          # секунды между 2001-01-01 и epoch

_CHROME_SQL = """
    SELECT url, title, visit_count, last_visit_time FROM urls
    WHERE visit_count >= ? AND url LIKE 'http%'
    ORDER BY visit_count DESC LIMIT ?
"""
_FIREFOX_SQL = """
    SELECT url, title, visit_count, last_visit_date FROM moz_places
    WHERE visit_count >= ? AND url LIKE 'http%'
    ORDER BY visit_count DESC LIMIT ?
"""
_SAFARI_SQL = """
    SELECT i.url, i.title, COUNT(v.id), MAX(v.visit_time)
    FROM history_items i JOIN history_visits v ON v.history_item = i.id
    WHERE i.url LIKE 'http%'
    GROUP BY i.id HAVING COUNT(v.id) >= ?
    ORDER BY 3 DESC LIMIT ?
"""


def _history_files() -> List[Tuple[str, Path]]:
    """(движок, путь к БД истории) для текущей ОС; несуществующее отбрасываем."""
    home = Path.home()
    candidates: List[Tuple[str, Path]] = []
    if sys.platform == "darwin":
        candidates += [
            ("chrome", home / "Library/Application Support/Google/Chrome/Default/History"),
            ("chrome", home / "Library/Application Support/Microsoft Edge/Default/History"),
            ("chrome", home / "Library/Application Support/Chromium/Default/History"),
            ("safari", home / "Library/Safari/History.db"),
        ]
        candidates += [("firefox", Path(p)) for p in glob.glob(
            str(home / "Library/Application Support/Firefox/Profiles/*/places.sqlite"))]
    elif sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        candidates += [
            ("chrome", local / "Google/Chrome/User Data/Default/History"),
            ("chrome", local / "Microsoft/Edge/User Data/Default/History"),
        ]
        candidates += [("firefox", Path(p)) for p in glob.glob(
            str(roaming / "Mozilla/Firefox/Profiles/*/places.sqlite"))]
    else:  # linux и прочие unix
        candidates += [
            ("chrome", home / ".config/google-chrome/Default/History"),
            ("chrome", home / ".config/chromium/Default/History"),
        ]
        candidates += [("firefox", Path(p)) for p in glob.glob(
            str(home / ".mozilla/firefox/*/places.sqlite"))]
    return [(kind, p) for kind, p in candidates if p.exists()]


def _read_sqlite_copy(path: Path, sql: str, params: tuple) -> list:
    """Запрос к КОПИИ БД (оригинал залочен браузером). Любая ошибка — пустой
    список: TCC-запрет, битая БД, другая схема — не повод ронять резолв."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        shutil.copy2(path, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()
    except Exception as e:
        logger.debug(f"[BrowserHistory] {path.name}: чтение не удалось: {e}")
        return []
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


_cache: dict = {"ts": 0.0, "entries": []}


def _load_entries() -> List[Entry]:
    """Объединённый топ посещаемых адресов всех найденных браузеров (кэш TTL)."""
    now = time.time()
    if now - float(_cache["ts"]) < _CACHE_TTL_SEC:
        return _cache["entries"]
    entries: List[Entry] = []
    for kind, path in _history_files():
        try:
            if kind == "chrome":
                rows = _read_sqlite_copy(path, _CHROME_SQL, (_MIN_VISITS, _MAX_ENTRIES))
                entries += [(u, t or "", int(v), (ts - _CHROME_TS_OFFSET) / 1e6)
                            for u, t, v, ts in rows]
            elif kind == "firefox":
                rows = _read_sqlite_copy(path, _FIREFOX_SQL, (_MIN_VISITS, _MAX_ENTRIES))
                entries += [(u, t or "", int(v), (ts or 0) / 1e6)
                            for u, t, v, ts in rows]
            elif kind == "safari":
                rows = _read_sqlite_copy(path, _SAFARI_SQL, (_MIN_VISITS, _MAX_ENTRIES))
                entries += [(u, t or "", int(v), (ts or 0) + _SAFARI_TS_OFFSET)
                            for u, t, v, ts in rows]
        except Exception as e:
            logger.debug(f"[BrowserHistory] {kind}:{path.name}: {e}")
    _cache["ts"] = now
    _cache["entries"] = entries
    if entries:
        logger.info(f"[BrowserHistory] Частых адресов загружено: {len(entries)}")
    return entries


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9а-яё]+", "", (s or "").lower())


# Хосты, чьи заголовки — контент ленты/писем, а не «имя сайта»: письмо с темой
# «платформа» не делает gmail платформой. Для них матчимся только по домену.
_NOISY_TITLE_HOSTS = ("mail.", "gmail.", "x.com", "twitter.com", "facebook.com",
                      "instagram.com", "vk.com", "ok.ru", "t.me",
                      "web.telegram.org", "pinterest.")


def find_in_history(name: str) -> Optional[str]:
    """«платформа» → «https://platform.21-school.ru/»: самый посещаемый адрес,
    где ВСЕ слова запроса (или их основы — «диспейса» ≈ «диспейс») нашлись
    в домене или заголовке. Однословный запрос → корень хоста (ищут сайт),
    мультисловный → сама страница из истории (ищут страницу).
    None — в истории ничего подходящего (резолв идёт дальше, в поиск)."""
    from app.features.web_search import _stem  # лениво: во избежание циклов импорта
    words = [w for w in re.findall(r"[a-z0-9а-яё]+", name.lower()) if len(w) >= 3]
    if not words:
        return None
    best: Optional[Entry] = None
    for url, title, visits, _last in _load_entries():
        host = urlparse(url).hostname or ""
        if "localhost" in host or host.startswith("127."):
            continue  # дев-серверы — не «сайты» для команды «открой»
        target = _slug(host)
        if not any(n in host for n in _NOISY_TITLE_HOSTS):
            target += " " + _slug(title)
        if not all(w in target or _stem(w) in target for w in words):
            continue
        if best is None or visits > best[2]:
            best = (url, title, visits, _last)
    if best is None:
        return None
    if len(words) >= 2:
        return best[0]
    p = urlparse(best[0])
    return f"{p.scheme}://{p.netloc}/"

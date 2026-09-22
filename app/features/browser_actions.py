"""Действия в живом браузере (computer_control, этап 3b): клики/клавиши внутри
уже открытых вкладок. LLM сюда не лезет: исполняются только рецепты из
реестра RECIPES, на которые ссылается allowlist tasks персоны ("recipe:<id>"),
и агентные клики/скачивания по снапшоту страницы.

Бэкенд — единый на обеих ОС (macOS/Windows): любой Chromium-браузер
(Chrome, Edge, Opera, Яндекс, Brave, Vivaldi) с `--remote-debugging-port`
+ Playwright `connect_over_cdp`. Браузер бот умеет
запускать сам (см. `_CdpWorker._launch_chrome`): профиль и бинарь — в конфиге
`features.computer_control.browser` персоны. Отличий в логике
snapshot/click/wait по ОС нет — различия только в запуске процесса браузера.

AppleScript (macOS) оставлен как fallback на случай, когда CDP недоступен
(Chrome уже открыт без отладки, политика безопасности и т.п.): JS в реальной
вкладке через Apple Events (`execute tab javascript`), разовые разрешения —
Chrome → Вид → Разработчикам → «Разрешить JavaScript из событий Apple» +
согласие на автоматизацию (TCC).

ВНИМАНИЕ про профили: с Chrome 136+ `--remote-debugging-port` игнорируется
для профиля по умолчанию — поэтому дефолтный `user_data_dir` здесь
выделенный automation-профиль. На чужой (основной) профиль можно переключить
конфигом (`browser.user_data_dir`), но тогда Chrome должен быть полностью
закрыт (SingletonLock) и быть старше 136 — иначе порт молча не поднимется.

Рецепт — это JS-сниппет, выполняемый во вкладке, чей URL содержит домен.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import weakref
from collections import Counter
from typing import Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

from app.core.word_stem import WORD_ENDINGS as _STEM_ENDINGS

logger = logging.getLogger(__name__)

CDP_URL = "http://127.0.0.1:9222"

# Таймауты/бюджеты (п.2 плана: суммарное ожидание готовности 5–8 сек,
# не блокируем бота бесконечно)
CDP_CONNECT_TIMEOUT_MS = 2500
BROWSER_LAUNCH_TIMEOUT_SEC = 12.0
READY_TIMEOUT_SEC = 6.0          # общий бюджет ожидания готовности страницы
NETWORKIDLE_BUDGET_MS = 3000     # networkidle — best effort внутри бюджета
DOM_POLL_MS = 250                # шаг опроса DOM-хэша
DOM_STABLE_POLLS = 2             # столько одинаковых опросов подряд = «стабильно»
CLICK_TIMEOUT_MS = 2500          # actionability-таймаут playwright-клика
CLICK_VERIFY_SEC = 3.0           # closed-loop: ждём видимого эффекта клика
POPUP_WAIT_SEC = 0.6             # доп. ожидание нового окна после клика
POPUP_NAV_WAIT_SEC = 3.0         # новая вкладка ещё about:blank — ждём навигацию
SUBMIT_VERIFY_SEC = 2.0          # closed-loop отправки: поле очистилось/стр. изменилась
DOWNLOAD_VERIFY_SEC = 5.0        # closed-loop: ждём события скачивания
SCROLL_MAX_MS = 600000           # потолок одного сеанса авто-листания (10 мин)
POINT_MIN_PX = 8                 # меньший видимый остаток зоны не адресуем
# Потолок нажатий в ОДНОЙ серии (press_key times) — единственный источник:
# им же ограничивает разбор «удали N символов» в computer_control
# (_ERASE_MAX). Раньше потолков было два: разбор обещал до 100 Backspace, а
# press_key молча клампил до 10 — «удали 50 символов» отчитывалось о 50
# удалённых, а стирало 10
PRESS_TIMES_MAX = 100

# Бюджеты ОДНОЙ операции: у каждого вызова свой потолок, иначе одна зависшая
# вкладка (alert(), бесконечный JS) держит лок воркера навсегда — вместе с
# restart_browser/shutdown_browser/watchdog, которые идут через тот же submit
SUBMIT_TIMEOUT_SEC = 45.0        # дефолт playwright-операции
SUBMIT_PROBE_TIMEOUT_SEC = 15.0  # пробник подключения (connect без запуска)
SUBMIT_LAUNCH_TIMEOUT_SEC = BROWSER_LAUNCH_TIMEOUT_SEC + 20.0  # запуск браузера
SUBMIT_KILL_TIMEOUT_SEC = 40.0   # завершение процесса браузера (grace + SIGKILL)
NAV_GOTO_TIMEOUT_SEC = 20.0      # page.goto одной вкладки (_new_page_quiet)
# Опрос HTTP-статуса навигации (заглушка шлюза 502/503/504): статус приходит
# только С ОТВЕТОМ сервера, поэтому первый замер ждёт долго (dodo отдала 502 за
# 10.5с), а перепроверка после переоткрытия нужна лишь для лога — держать из-за
# неё лок воркера ещё 15с нельзя (кейс: открытие занимало воркер до 30с)
GATEWAY_PROBE_SEC = 15.0
GATEWAY_RECHECK_SEC = 3.0
# Бюджет submit'а открытия/навигации СЧИТАЕТСЯ из шагов внутри, а не задан
# «на глаз»: два goto + два опроса шлюза + запас
SUBMIT_NAV_TIMEOUT_SEC = (2 * NAV_GOTO_TIMEOUT_SEC + GATEWAY_PROBE_SEC
                          + GATEWAY_RECHECK_SEC + 20.0)
SUBMIT_CAPTURE_TIMEOUT_SEC = 120.0  # полностраничный захват (лестница кадров)
SUBMIT_MARGIN_SEC = 20.0         # запас поверх бюджета ожидания в странице

# Сырой CDP (фоновые вкладки веб-чатов)
RAW_CONNECT_TIMEOUT_SEC = 10.0   # установка websocket-соединения
RAW_CALL_TIMEOUT_SEC = 20.0      # дефолтный потолок ОТВЕТА на один вызов
RAW_MARGIN_SEC = 10.0            # запас поверх бюджета awaitPromise-ожидания

# Флаги экономии ресурсов для автоматизационного профиля: фоновая сеть,
# синхронизация, переводчик, каст и скачивание моделей «подсказок» — лишняя
# нагрузка на железо (фризы при работе бота); профиль учётками веб-чатов это
# не ломает. Применяются на свежем запуске браузера (перезапуск Chrome).
CHROME_THRIFT_FLAGS = (
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--metrics-recording-only",
    # Без --mute-audio: в этом браузере пользователь реально смотрит/слушает
    # («включи музыку на ютубе» — звук нужен). Тишина была «экономией
    # ресурсов» автоматизационного профиля, но медиа-сценарии важнее
    "--disable-features=Translate,MediaRouter,OptimizationHints",
    "--force-color-profile=srgb",
)


# Флаги против троттлинга фоновых страниц: без них Chrome душит скрытые/
# фоновые вкладки (setTimeout 100мс → ~1с, кейс web_extended: отправка веб-чата
# уходила в дебаунс и «не появлялась в ленте»). Для автоматизации это критично
# — JS вкладок должен работать в реальном темпе. Цена — чуть больше CPU у
# фоновых SPA, что компенсируется freeze вкладок между вызовами.
CHROME_NO_THROTTLE_FLAGS = (
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)


class BrowserUnavailable(RuntimeError):
    """Браузер недоступен/не настроен: текст — человеческий, его видит бот."""

    error_class = "unavailable"


class ClickUncertain(BrowserUnavailable):
    """Клик/скачивание отправлены, но видимого эффекта нет (closed-loop
    проверка, п.6): честное «не уверен, что сработало», а не тихий успех."""

    error_class = "uncertain"


class FillUncertain(BrowserUnavailable):
    """Текст отправлен в поле, но его значение не совпало с введённым
    (closed-loop, как у клика): честное «не уверен», а не тихое «Введено»."""

    error_class = "uncertain"


class RawTabUnsupported(BrowserUnavailable):
    """Операция принципиально недоступна фоновой (raw-CDP) вкладке веб-чата:
    клики, снапшоты, скриншоты, история — это playwright, а background-
    таргеты он в Page не заворачивает. Отдельный класс, чтобы «не смогли
    проверить» нельзя было принять за «проверили, чисто» (detect_antibot и
    его вызывающие в web_llm)."""

    error_class = "unsupported"


class RawCallTimeout(BrowserUnavailable):
    """Ответ CDP не пришёл в бюджет ЭТОГО вызова. Соединение и соседние
    вкладки при этом живы: рвать сокет из-за одного долгого ожидания
    нельзя — вместе с ним умирали все фоновые вкладки пула."""

    error_class = "timeout"


class BackendUnsupported(BrowserUnavailable):
    """Операция недоступна АКТИВНОМУ бэкенду: нужен CDP (playwright-
    примитивы — доверенный ввод, мышь, зум, вкладки), а выбран/доступен
    только AppleScript или Safari. Отдельный класс, потому что «транспорта
    для проверки нет» — не то же самое, что «проверили и не вышло»:
    вызывающие различали это по ПОДСТРОКЕ текста ошибки (chat_wait_uploaded:
    «требует бэкенд» in str(e)), и любая правка формулировки молча
    превращала «не смогли проверить» в «сбой»."""

    error_class = "backend"


def _no_backend(what: str, active: Optional[str] = None,
                need: Tuple[str, ...] = ("cdp",)) -> BackendUnsupported:
    """ЕДИНЫЙ отказ «активный бэкенд эту операцию не умеет» (тип + текст):
    раньше каждое место писало свою формулировку, а различать их
    вызывающим приходилось по подстроке."""
    tail = f" (нужен {'/'.join(need)}"
    tail += f", активен «{active}»)" if active else ")"
    return BackendUnsupported(
        f"{what} работает только в браузере бота (Chrome с отладкой){tail}")


# ── Рецепты: id → (домен вкладки | None = активная, JS) ──
# JS одной строкой, ASCII, без кавычек-ловушек. Успех — строка с префиксом
# «ok:» (остальное идёт в лог); любой другой текст — причина неудачи,
# её покажет бот как «Не удалось …: <текст>».

RECIPES: Dict[str, Tuple[Optional[str], str]] = {
    "youtube_toggle": (
        "youtube.com",
        "var v=document.querySelector('video');"
        "if(v){v.paused?v.play():v.pause();v.paused?'ok:paused':'ok:playing'}"
        "else{'во вкладке ютуба нет видео'}",
    ),
    "youtube_next": (
        "youtube.com",
        "var b=document.querySelector('.ytp-next-button');"
        "if(b){b.click();'ok:next'}else{'нет кнопки следующего видео'}",
    ),
    "youtube_mute": (
        "youtube.com",
        "var v=document.querySelector('video');"
        "if(v){v.muted=!v.muted;v.muted?'ok:muted':'ok:unmuted'}"
        "else{'во вкладке ютуба нет видео'}",
    ),
    # В живом браузере (с куками) антибот не мешает — в отличие от серверного
    # fetch: открываем первый фильм/сериал со страницы поиска
    "kinopoisk_first": (
        "kinopoisk.ru",
        # первый link на КОРЕНЬ карточки (/film/123/), а не на разделы cast/reviews
        "var as=[].slice.call(document.querySelectorAll('a[href^=\"/film/\"],a[href^=\"/series/\"]'));"
        "var a=as.find(function(x){return /^\\/(film|series)\\/\\d+\\/?$/.test(new URL(x.href).pathname)});"
        "if(a){a.click();'ok:opened'}else{'на странице нет ссылок на фильмы'}",
    ),
    # N-й результат выдачи («запусти третий результат/видео»): номер
    # подставляется в {N} из аргумента recipe:search_pick:N; вкладка — как у
    # search_first (активная, иначе фоновая страница поиска). Отсчёт — от
    # первого ВИДИМОГО во вьюпорте («первое видео» — первое на экране, а не
    # в DOM докрученной страницы); за видимыми — остальные в DOM-порядке,
    # чтобы N за пределами экрана продолжал список ниже сгиба
    "search_pick": (
        None,
        "var N={N};"
        "var h=location.hostname,p=location.pathname,a=null;"
        "var vf=function(l){var v=[],r=[];l.forEach(function(x){"
        "var b=x.getBoundingClientRect();"
        "(b.width>0&&b.height>0&&b.bottom>0&&b.top<innerHeight"
        "&&b.right>0&&b.left<innerWidth?v:r).push(x);});return v.concat(r);};"
        # youtube: заголовочные ссылки видео по всем раскладкам — выдача
        # (a#video-title), старый фид (a#video-title-link), up-next
        # (a#wc-endpoint), lockup-главная 2025 (a.ytLockupMetadataViewModelTitle);
        # селектор-список отдаёт всё в DOM-порядке, по одной ссылке на видео
        "if(h.indexOf('youtube.com')>=0){"
        "  var vs=vf([].slice.call(document.querySelectorAll('a#video-title-link,a#video-title,a#wc-endpoint,a.ytLockupMetadataViewModelTitle')));a=vs[N-1]||null;"
        "}else if(h.indexOf('kinopoisk.ru')>=0){"
        "  var as=[].slice.call(document.querySelectorAll('a[href^=\"/film/\"],a[href^=\"/series/\"]'));"
        "  var rs=vf(as.filter(function(x){return /^\\/(film|series)\\/\\d+\\/?$/.test(new URL(x.href).pathname)}));"
        "  a=rs[N-1]||null;"
        "}else if(h.indexOf('google.')>=0){"
        "  var hs=vf([].slice.call(document.querySelectorAll('#search h3')));var h3=hs[N-1];a=h3?h3.closest('a'):null;"
        "}"
        "if(a){a.click();'ok:opened'}else{'нет результата номер '+N}",
    ),
    # N-е видео в плейлисте («открой третье видео в плейлисте»): панель
    # плейлиста на странице видео / страница плейлиста; нет панели — общий
    # список видео страницы
    "playlist_pick": (
        None,
        "var N={N};"
        "var vs=document.querySelectorAll("
        "'ytd-playlist-panel-renderer a#wc-endpoint,"
        "ytd-playlist-panel-renderer a#video-title,"
        "ytd-playlist-video-list-renderer a.ytLockupMetadataViewModelTitle,"
        "ytd-playlist-video-list-renderer a#video-title,"
        "ytd-playlist-video-renderer a#video-title');"
        "if(!vs.length){vs=document.querySelectorAll('a#video-title-link,a#video-title,a#wc-endpoint,a.ytLockupMetadataViewModelTitle');}"
        "var a=vs[N-1]||null;"
        "if(a){a.click();'ok:opened'}else{'нет видео номер '+N}",
    ),
    # N-е видео полки shorts на странице («первое видео в shorts», «первый
    # шортс»): полок может быть несколько — берётся первая ВИДИМАЯ (её и
    # имеет в виду пользователь), иначе первая в DOM. Локаль не нужна:
    # полка определяется по ссылкам /shorts/, а не по заголовку
    "shorts_pick": (
        None,
        "var N={N};"
        "var shelves=[].slice.call(document.querySelectorAll("
        "'ytd-rich-shelf-renderer,ytd-reel-shelf-renderer,"
        "ytd-rich-section-renderer,[is-shorts]'));"
        "var sh=shelves.filter(function(s){"
        "return s.querySelector('a[href*=\"/shorts/\"]');});"
        "if(!sh.length){'на странице нет полки shorts'}"
        "else{"
        "var shelf=sh[0];"
        "for(var i=0;i<sh.length;i++){var r=sh[i].getBoundingClientRect();"
        "if(r.bottom>0&&r.top<innerHeight){shelf=sh[i];break;}}"
        # По одной ссылке на шортс (у пункта их две: миниатюра и заголовок)
        "var seen={},links=[];"
        "shelf.querySelectorAll('a[href*=\"/shorts/\"]').forEach(function(a){"
        "var h=a.href.split('?')[0];if(!seen[h]){seen[h]=1;links.push(a);}});"
        "var a=links[N-1]||null;"
        "if(a){a.click();'ok:opened'}else{'в полке shorts нет видео номер '+N}}",
    ),
    # Первый результат выдачи на АКТИВНОЙ вкладке — сайт определяется по хосту
    "search_first": (
        None,
        "var h=location.hostname,p=location.pathname,a=null;"
        "if(h.indexOf('youtube.com')>=0){"
        "  a=document.querySelector('a#video-title-link,a#video-title,a#wc-endpoint,a.ytLockupMetadataViewModelTitle');"
        "}else if(h.indexOf('kinopoisk.ru')>=0){"
        "  var as=[].slice.call(document.querySelectorAll('a[href^=\"/film/\"],a[href^=\"/series/\"]'));"
        "  a=as.find(function(x){return /^\\/(film|series)\\/\\d+\\/?$/.test(new URL(x.href).pathname)});"
        "}else if(h.indexOf('google.')>=0){"
        "  var h3=document.querySelector('#search h3');a=h3?h3.closest('a'):null;"
        "}"
        "if(a){a.click();'ok:opened'}else{'ни на одной вкладке нет страницы поиска с результатами'}",
    ),
}


# ── Конфиг браузера (features.computer_control.browser) ──

# Дефолтный профиль — ВЫДЕЛЕННЫЙ automation-профиль: с Chrome 136+ отладочный
# порт на профиле по умолчанию игнорируется, а основной профиль ещё и бывает
# занят запущенным Chrome. Путь к основному профилю задаётся конфигом явно.
_DEFAULT_PROFILES = {
    "darwin": "~/Library/Application Support/vpc-browser-profile",
    "win32": "%LOCALAPPDATA%\\vpc-browser-profile",
    "linux": "~/.cache/vpc-browser-profile",
}

# Пул V (headed): профиль — КОПИЯ automation-профиля пула H (создаётся при
# первом запуске V; два Chrome не могут делить один профиль — SingletonLock)
_DEFAULT_V_PROFILES = {
    "darwin": "~/Library/Application Support/vpc-browser-profile-headed",
    "win32": "%LOCALAPPDATA%\\vpc-browser-profile-headed",
    "linux": "~/.cache/vpc-browser-profile-headed",
}
_DEFAULT_H_CDP_URL = "http://127.0.0.1:9223"  # пул H (headless): свой порт

_BCFG_DEFAULTS = {
    "backend": "auto",        # auto | cdp | applescript (последний — только macOS)
    "channel": "chrome",      # приоритетный Chromium-браузер при авто-детекте
    "cdp_url": CDP_URL,       # пул V (headed): видимые команды + headed-сайты
    "launch": True,           # боту можно самому запускать браузер с отладкой
    "user_data_dir": None,    # пул H (headless): str или per-OS dict; None — выделенный
    "executable": None,       # путь к бинарю; None — авто-детект Chromium-браузеров
    # ── Пулы (web_extended) ──
    "headless_url": _DEFAULT_H_CDP_URL,  # пул H: свой порт
    "visible_user_data_dir": None,  # пул V: None — дефолтный *-headed (копия H при 1-м запуске)
    "pool_h_mode": "headless",      # headless | hidden (headed со скрытым окном — запас против антибота)
    "v_idle_shutdown_min": 20,      # пул V гасится после N мин без активности
    "headed_fallback_without_control": False,  # headed-сайты webchat без режима управления
}
_BCFG: Dict[str, object] = dict(_BCFG_DEFAULTS)


def set_browser_config(cfg: Optional[dict]):
    """Применить блок `browser:` из конфига computer_control. Неизвестные ключи
    игнорируются, отсутствующие — сбрасываются на дефолты. Смена конфига роняет
    текущее CDP-подключение и реестр вкладок (бэкенд/профиль могли измениться)."""
    global _BCFG
    new = dict(_BCFG_DEFAULTS)
    if isinstance(cfg, dict):
        for k in new:
            if k in cfg:
                new[k] = cfg[k]
    backend = str(new.get("backend") or "auto").lower()
    if backend not in ("auto", "cdp", "applescript", "safari"):
        logger.warning(f"[BrowserActions] Неизвестный browser.backend {backend!r} — auto")
        backend = "auto"
    new["backend"] = backend
    if new == _BCFG:
        return
    _BCFG = new
    logger.info(f"[BrowserActions] Конфиг браузера: backend={backend}, "
                f"channel={new.get('channel')}, launch={new.get('launch')}, "
                f"profile={new.get('user_data_dir') or 'выделенный'}")
    if _WORKER._thread is not None:  # воркер ещё не стартовал — сбрасывать нечего
        try:
            _WORKER.submit(lambda w: w._drop_connection())
        except Exception as e:
            logger.debug(f"[BrowserActions] Сброс CDP-подключения не удался: {e}")
    # Пулы raw-CDP: порты/профили могли измениться — сокеты переустановятся
    # лениво на следующих вызовах
    for _pool in (_POOL_H, _POOL_V):
        try:
            _reset_raw_pool(_pool)
        except Exception:
            pass


def _prefs_path(udd: str) -> str:
    return os.path.join(udd, "Default", "Preferences")


def _prefs_editable(udd: str) -> bool:
    """Preferences профиля можно править ТОЛЬКО когда Chrome на нём закрыт:
    живой Chrome держит настройки в памяти и перезапишет файл при выходе
    (наша правка потеряется, а чужие свежие ключи затрутся). posix:
    закрытость подтверждает отсутствие/протухлость SingletonLock (его же
    смотрит _check_profile_lock перед запуском). win32: лока нет — проверить
    нечем, поэтому профиль не трогаем вовсе; раньше правились Preferences
    ЖИВОГО профиля."""
    if sys.platform == "win32":
        return _win_profile_closed(udd)
    lock = os.path.join(udd, "SingletonLock")
    if not os.path.islink(lock):
        return True
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[-1])
    except (ValueError, OSError):
        return True  # мусорный лок — держателя нет
    if _pid_alive(pid):
        logger.debug(f"[BrowserActions] Профиль {udd} занят Chrome "
                     f"(pid {pid}) — Preferences не правим")
        return False
    return True


def _win_profile_closed(udd: str) -> bool:
    """(win32) Профиль НЕ занят запущенным браузером. Символьного
    SingletonLock на Windows нет, зато работающий Chrome держит
    `<User Data>\\lockfile` открытым эксклюзивно — пробуем взять его сами:
    открыть на чтение-запись (без усечения) и поставить неблокирующий
    msvcrt-лок на один байт. Получилось — браузер закрыт, Preferences править
    можно; отказ — профиль живой, не трогаем (иначе Chrome перезапишет файл
    своей памятью при выходе, и правка потеряется). Файла нет — профиль ни
    разу не запускался, править можно."""
    lock = os.path.join(udd, "lockfile")
    if not os.path.exists(lock):
        return True
    fd = None
    try:
        fd = os.open(lock, os.O_RDWR)
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return True
    except Exception as e:
        logger.debug(f"[BrowserActions] Профиль {udd} занят запущенным "
                     f"браузером ({e}) — Preferences не правим")
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _load_prefs(udd: str) -> Optional[dict]:
    """Preferences профиля → dict; {} — файла ещё нет (новый профиль).
    None — файл ЕСТЬ, но не читается/не разбирается: правку обязан отменить
    вызывающий. Раньше ошибка чтения давала `prefs = {}`, и наша правка
    писала поверх настроек пользователя пустой объект — профиль сбрасывался
    целиком (логины веб-чатов в нём же)."""
    path = _prefs_path(udd)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"[BrowserActions] Preferences профиля не прочитались "
                       f"({e}) — настройки не трогаю")
        return None


def _save_prefs(udd: str, prefs: dict) -> bool:
    """Атомарная запись Preferences (tmp + os.replace) — единственное место
    записи настроек профиля."""
    path = _prefs_path(udd)
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.debug(f"[BrowserActions] Preferences профиля не записались: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _enable_memory_saver(udd: str):
    """Включить Memory Saver в профиле бота (best effort, до запуска Chrome):
    фоновые вкладки веб-чатов выгружаются из RAM — меньше давление на своп.
    Правим Preferences напрямую: флагом командной строки Memory Saver не
    включается, а профиль целиком наш."""
    if not _prefs_editable(udd):
        return
    prefs = _load_prefs(udd)
    if prefs is None:
        return  # нечитаемый файл: писать поверх нельзя
    hem = prefs.get("high_efficiency_mode")
    if isinstance(hem, dict) and hem.get("enabled") is True:
        return
    if not isinstance(hem, dict):
        hem = {}
    hem["enabled"] = True
    prefs["high_efficiency_mode"] = hem
    if _save_prefs(udd, prefs):
        logger.info("[BrowserActions] Memory Saver включён в профиле бота")


def _wipe_saved_zoom_levels(udd: str):
    """Сбросить сохранённые per-host зумы сайтов в профиле пула V (best effort,
    до запуска Chrome — тот же момент, что _enable_memory_saver). Случайный
    ⌘−/щипок запоминается Chrome навсегда (Preferences → partition.
    per_host_zoom_levels) и ломает и вёрстку, и скриншоты (кейс 19.09, dodo:
    зум 50% — innerWidth 1614 при окне 807, контент колонкой в пол-окна,
    кадр 3228×3440), а CDP-override'ы его не перебивают. Профиль
    автоматизационный — чужих настроек зума в нём не бывает, чистим всю
    карту. Основной профиль пользователя (конфигом можно нацелить и на
    него) не трогаем — там зумы человека."""
    if _is_default_browser_profile(udd) or not _prefs_editable(udd):
        return
    prefs = _load_prefs(udd)
    if not prefs:  # None (нечитаемо) или {} (файла нет) — чистить нечего
        return
    part = prefs.get("partition")
    phzl = part.get("per_host_zoom_levels") if isinstance(part, dict) else None
    x = phzl.get("x") if isinstance(phzl, dict) else None
    if not isinstance(x, dict) or not x:
        return
    n = len(x)
    phzl["x"] = {}
    if _save_prefs(udd, prefs):
        logger.info(f"[BrowserActions] Сброшены сохранённые зумы сайтов "
                    f"({n} шт.) в профиле пула V")


def _resolve_user_data_dir() -> str:
    """Каталог профиля для запуска браузера: конфиг (str или per-OS dict) или
    выделенный automation-профиль по умолчанию."""
    udd = _BCFG.get("user_data_dir")
    val = None
    if isinstance(udd, dict):
        val = udd.get(sys.platform) or udd.get("other")
    elif isinstance(udd, str) and udd.strip():
        val = udd.strip()
    if not val or not str(val).strip():
        val = _DEFAULT_PROFILES.get(sys.platform, _DEFAULT_PROFILES["linux"])
    path = os.path.expandvars(os.path.expanduser(str(val).strip()))
    os.makedirs(path, exist_ok=True)
    return path


def _resolve_executable() -> Optional[str]:
    """Бинарь браузера: конфиг → типовые пути Chromium-браузеров ОС → PATH.

    CDP одинаков у всех Chromium (Chrome, Edge, Opera, Яндекс Браузер, Brave,
    Vivaldi, Chromium) — детектим их все: у пользователя может не быть
    именно Chrome. Порядок перебора: явный browser.channel → chrome → edge →
    остальные. Ничего не нашлось — конфиг browser.executable."""
    exe = _BCFG.get("executable")
    if isinstance(exe, str) and exe.strip():
        exe = os.path.expandvars(os.path.expanduser(exe.strip()))
        return exe if os.path.exists(exe) else None
    channel = str(_BCFG.get("channel") or "chrome").lower()

    mac_apps = {
        "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "edge": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "opera": "/Applications/Opera.app/Contents/MacOS/Opera",
        "yandex": "/Applications/Yandex.app/Contents/MacOS/Yandex",
        "brave": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "vivaldi": "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
        "chromium": "/Applications/Chromium.app/Contents/MacOS/Chromium",
    }
    win_paths = {
        "chrome": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                   r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                   r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"],
        "edge": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                 r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
        "opera": [r"%LOCALAPPDATA%\Programs\Opera\opera.exe",
                  r"%ProgramFiles%\Opera\opera.exe"],
        "yandex": [r"%LOCALAPPDATA%\Yandex\YandexBrowser\Application\browser.exe"],
        "brave": [r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
                  r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"],
        "vivaldi": [r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe",
                    r"%ProgramFiles%\Vivaldi\Application\vivaldi.exe"],
    }
    path_names = {
        "chrome": ["google-chrome", "google-chrome-stable", "chrome"],
        "edge": ["microsoft-edge", "microsoft-edge-stable"],
        "opera": ["opera"],
        "yandex": ["yandex-browser", "yandex-browser-stable"],
        "brave": ["brave-browser", "brave"],
        "vivaldi": ["vivaldi", "vivaldi-stable"],
        "chromium": ["chromium", "chromium-browser"],
    }
    order = [channel] + [b for b in
                         ("chrome", "edge", "opera", "yandex", "brave",
                          "vivaldi", "chromium") if b != channel]

    cands: List[str] = []
    names: List[str] = []
    for b in order:
        if sys.platform == "darwin":
            if mac_apps.get(b):
                cands.append(mac_apps[b])
        elif sys.platform == "win32":
            cands += [os.path.expandvars(p) for p in win_paths.get(b, [])]
        names += path_names.get(b, [])
    for c in cands:
        if os.path.exists(c):
            return c
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def _is_default_browser_profile(path: str) -> bool:
    """path — основной профиль Chromium-браузера текущей ОС (Chrome, Edge,
    Opera, Яндекс, Brave, Vivaldi). Его процесс убивать нельзя никогда:
    там вкладки и сессии пользователя."""
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    cands = {
        "darwin": [f"{home}/Library/Application Support/Google/Chrome",
                   f"{home}/Library/Application Support/Microsoft Edge",
                   f"{home}/Library/Application Support/com.operasoftware.Opera",
                   f"{home}/Library/Application Support/Yandex/YandexBrowser",
                   f"{home}/Library/Application Support/BraveSoftware/Brave-Browser",
                   f"{home}/Library/Application Support/Vivaldi"],
        "win32": [f"{local}\\Google\\Chrome\\User Data",
                  f"{local}\\Microsoft\\Edge\\User Data",
                  f"{roaming}\\Opera Software\\Opera Stable",
                  f"{local}\\Yandex\\YandexBrowser\\User Data",
                  f"{local}\\BraveSoftware\\Brave-Browser\\User Data",
                  f"{local}\\Vivaldi\\User Data"],
        "linux": [f"{home}/.config/google-chrome",
                  f"{home}/.config/chromium",
                  f"{home}/.config/microsoft-edge",
                  f"{home}/.config/opera",
                  f"{home}/.config/yandex-browser",
                  f"{home}/.config/BraveSoftware/Brave-Browser",
                  f"{home}/.config/vivaldi"],
    }
    norm = os.path.normcase(os.path.abspath(path))
    return any(norm == os.path.normcase(os.path.abspath(c))
               for c in cands.get(sys.platform, []) if c)


def _try_reclaim_profile(pid: int, user_data_dir: str) -> bool:
    """Мягкое освобождение лока: Chrome, держащий ВЫДЕЛЕННЫЙ automation-профиль
    без отладочного порта (перезапущен вручную/системой — флаги потерялись),
    завершаем по SIGTERM и отдаём профиль боту. Иначе вся браузерная
    автоматизация молча разваливается на неуправляемых вкладках (кейс 22.08:
    веб-чаты падали с «отслеживаемая вкладка закрыта»). Сессии/куки в профиле
    сохраняются — гасим только процесс. Не трогаем: основной профиль
    пользователя, чужие процессы, Chrome С отладкой (странное состояние —
    разбираться руками). → True, если лок освобождён и можно запускаться."""
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    if "--remote-debugging-port" in cmd:
        return False
    if f"--user-data-dir={user_data_dir}" not in cmd:
        return False  # лок держит не наш запуск — не трогаем
    if _is_default_browser_profile(user_data_dir):
        return False
    logger.warning(
        f"[BrowserActions] Профиль {user_data_dir} занят Chrome (pid {pid}) "
        f"без отладки — завершаю его, чтобы перезапустить с CDP")
    _proc_terminate(pid)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break  # процесс завершился
        time.sleep(0.3)
    else:
        logger.warning("[BrowserActions] Chrome не завершился за 10с "
                       "— профиль не освобождён")
        return False
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.unlink(os.path.join(user_data_dir, name))
        except OSError:
            pass
    logger.info("[BrowserActions] Профиль освобождён — продолжаю запуск с CDP")
    return True


def _check_profile_lock(user_data_dir: str):
    """SingletonLock: профиль уже занят запущенным Chrome — понятная ошибка
    (п.1 плана). posix: лок — симлинк «host-pid»; мёртвый pid — протухший лок
    от упавшего Chrome, снимаем. Живой держатель без отладки на выделенном
    профиле — мягко забираем профиль обратно (_try_reclaim_profile).
    На Windows лока-файла нет — занятый профиль детектируется по мгновенному
    выходу запущенного процесса (_launch_chrome)."""
    if sys.platform == "win32":
        return
    lock = os.path.join(user_data_dir, "SingletonLock")
    if not os.path.islink(lock):
        return
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[-1])
    except (ValueError, OSError):
        return
    if not _pid_alive(pid):  # общая проверка живости, см. _pid_alive
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.unlink(os.path.join(user_data_dir, name))
            except OSError:
                pass
        logger.info(f"[BrowserActions] Снят протухший SingletonLock (pid {pid})")
        return
    if _try_reclaim_profile(pid, user_data_dir):
        return
    raise BrowserUnavailable(
        "профиль браузера занят: Chrome уже запущен с этим профилем без "
        "отладки. Закрой его полностью и повтори — тогда бот запустит браузер "
        "сам, либо запусти scripts/chrome_debug.sh")


# ── CDP-воркер: единый бэкенд для обеих ОС ───────────────
# sync_playwright привязан к создавшему его потоку, поэтому экземпляр живёт в
# выделенном потоке, а все операции сериализуются через очередь. Заодно это
# даёт постоянное подключение (без переподключения на каждый вызов) и реестр
# отслеживаемых вкладок (tab_id → Page) — аналог стабильных AppleScript-id.


def _pid_alive(pid: int) -> bool:
    """Процесс жив. ЕДИНАЯ проверка на все платформы: на Windows
    os.kill(pid, 0) процесс не опрашивает, а ЗАВЕРШАЕТ его (os.kill там —
    TerminateProcess, сигнал игнорируется), поэтому проверка живости
    Chrome сама его и убивала; там спрашиваем ядро через
    OpenProcess+WaitForSingleObject. На posix — сигнал 0, причём чужой
    живой процесс (PermissionError) считается ЖИВЫМ: прежний
    `except OSError: return False` выдавал его за мёртвый, и
    _kill_chrome_on_profile рапортовал «браузер завершён», пока тот работал.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        try:
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            SYNCHRONIZE, WAIT_TIMEOUT, ERROR_ACCESS_DENIED = 0x100000, 0x102, 5
            h = k32.OpenProcess(SYNCHRONIZE, False, pid)
            if not h:
                # Доступ запрещён — процесс существует (чужой сеанс/права)
                return k32.GetLastError() == ERROR_ACCESS_DENIED
            try:
                return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
            finally:
                k32.CloseHandle(h)
        except Exception as e:  # ctypes недоступен — не угадываем
            logger.debug(f"[BrowserActions] Проверка процесса {pid}: {e}")
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # чужой процесс, но ЖИВОЙ
    except OSError:
        return False


def _proc_terminate(pid: int):
    """ВЕЖЛИВОЕ завершение процесса — платформенно, одно место. posix:
    SIGTERM. win32: `taskkill /PID` (окну уходит WM_CLOSE, Chrome успевает
    записать куки/сессии) — os.kill там превращает ЛЮБОЙ сигнал в
    TerminateProcess, то есть «вежливая» фаза была жёстким убийством, и
    grace-ожидание после неё не имело смысла."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(int(pid))],
                           capture_output=True, timeout=10)
        except Exception as e:
            logger.debug(f"[BrowserActions] taskkill {pid}: {e}")
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _proc_kill(pid: int):
    """ЖЁСТКОЕ завершение: posix SIGKILL, win32 `taskkill /F /T` (вместе с
    дочерними рендерерами — они держат профиль)."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                           capture_output=True, timeout=10)
        except Exception as e:
            logger.debug(f"[BrowserActions] taskkill /F {pid}: {e}")
        return
    if not hasattr(signal, "SIGKILL"):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _kill_chrome_on_profile(proc: Optional[subprocess.Popen],
                            user_data_dir: str,
                            grace_sec: float = 10.0) -> bool:
    """Завершить Chrome на профиле бота (общая логика пулов H и V).
    Кандидаты: запущенный нами Popen → pid из SingletonLock (после проверки
    cmdline, что держит именно наш профиль — Chrome с чужим профилем не
    трогаем). SIGTERM, до grace_sec, затем SIGKILL (posix). grace_sec короче
    на путях рестарта: там браузер wedged по определению, долгое вежливое
    ожидание — просто фриз. → True, если мёртв."""
    pids: List[int] = []
    if proc is not None and proc.poll() is None:
        pids.append(proc.pid)
    if sys.platform != "win32":
        lock = os.path.join(user_data_dir, "SingletonLock")
        if os.path.islink(lock):
            try:
                lock_pid = int(os.readlink(lock).rsplit("-", 1)[-1])
            except (ValueError, OSError):
                lock_pid = None
            if lock_pid and lock_pid not in pids:
                try:
                    cmd = subprocess.run(
                        ["ps", "-p", str(lock_pid), "-o", "command="],
                        capture_output=True, text=True, timeout=5).stdout
                except Exception:
                    cmd = ""
                if f"--user-data-dir={user_data_dir}" in cmd:
                    pids.append(lock_pid)
    if not pids:
        logger.info("[BrowserActions] Процесс браузера бота не найден — "
                    "нечего завершать")
        return True
    for pid in pids:
        _proc_terminate(pid)  # платформенно вежливо (см. _proc_terminate)
    alive = list(pids)
    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline and alive:
        alive = [pid for pid in pids if _pid_alive(pid)]
        if alive:
            time.sleep(0.3)
    if alive:
        logger.warning(f"[BrowserActions] Браузер не завершился за "
                       f"{int(grace_sec)}с — убиваю принудительно")
        for pid in alive:
            _proc_kill(pid)
        time.sleep(0.5)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.unlink(os.path.join(user_data_dir, name))
        except OSError:
            pass
    died = not any(_pid_alive(pid) for pid in pids)
    logger.info("[BrowserActions] Браузер бота завершён"
                if died else
                "[BrowserActions] Браузер бота не удалось завершить")
    return died


def _abort_launch(proc: Optional[subprocess.Popen], user_data_dir: str,
                  what: str):
    """Общая уборка после ЛЮБОГО сбоя запуска браузера (таймаут отладочного
    порта, исключение по пути, отказ профиля): только что запущенный процесс
    нельзя оставлять жить. Он держит SingletonLock профиля, а ссылку на него
    вызывающий уже потерял — следующий запуск видит «профиль занят Chrome без
    отладки» и пул не поднимается больше никогда (кейс пула V после таймаута
    запуска). Grace короткий: браузер только что стартовал, сохранять ему
    нечего."""
    if proc is None or proc.poll() is not None:
        return
    logger.warning(f"[BrowserActions] Запуск {what} не удался — добиваю "
                   f"осиротевший процесс (pid {proc.pid})")
    if _is_default_browser_profile(user_data_dir):
        # Основной профиль пользователя: чужие процессы на нём не трогаем
        # никогда (там его вкладки и сессии) — гасим ТОЛЬКО свой запуск
        try:
            proc.terminate()
        except Exception as e:
            logger.debug(f"[BrowserActions] Свой процесс не завершён: {e}")
        return
    _kill_chrome_on_profile(proc, user_data_dir, grace_sec=3.0)


def _chat_or_service_url(url: str) -> bool:
    """Вкладка служебная (web_llm-хосты: динамический реестр + статические
    адаптеры) или это чат самого бота (localhost:5173/8000) — не цель
    пользовательских команд и не «видимая страница» при выборе вкладки
    (пользователь печатает в чат — его цель всё равно не он)."""
    try:
        u = urlparse(url)
        hn = (u.hostname or "").lower()
        if not hn or is_service_host(hn):
            return True
        return hn in ("localhost", "127.0.0.1") and u.port in (5173, 8000)
    except Exception:
        return False


def _origin_of(host_part: Optional[str]) -> Optional[str]:
    """scheme://host из полного URL (для фолбэка матча вкладки: полный URL
    с одноразовым query — auth?state=…&nonce=… — устаревает мгновенно).
    None — host_part не URL, а обычный хост-фрагмент («youtube.com»)."""
    s = str(host_part or "")
    if not s.startswith(("http://", "https://")):
        return None
    try:
        p = urlparse(s)
        return f"{p.scheme}://{p.netloc}" if p.netloc else None
    except Exception:
        return None


def _redirect_origins_of(host_part: Optional[str]) -> List[str]:
    """Origin'ы URL'ов, спрятанных в query записанного URL (OAuth
    ?redirect_uri=…, ?continue=…, ?returnUrl=… — универсально, без списка
    имён: любое значение-param'а, само являющееся http(s)-URL). Вкладка
    после логина уходит именно туда, и ни сам записанный URL, ни его origin
    (auth.…) больше ни одной живой вкладке не соответствуют."""
    s = str(host_part or "")
    if not s.startswith(("http://", "https://")):
        return []
    try:
        pairs = parse_qsl(urlparse(s).query)
    except Exception:
        return []
    out: List[str] = []
    for _k, v in pairs:
        for cand in (v, unquote(v)):  # значение бывает закодировано дважды
            o = _origin_of(cand)
            if o and o not in out:
                out.append(o)
    return out


def _site_key(host_part: Optional[str]) -> Optional[str]:
    """«Семейство сайта» — два последних лейбла хоста (auth.school.example.com и
    school.example.com — один сайт). Принимает и полный URL, и голый хост
    («school.example.com» — снапшоты отдают host без схемы). Последний
    фолбэк матча вкладки: записанный auth-URL устарел, вкладка вернулась на
    основной хост."""
    s = str(host_part or "").strip()
    if s and not s.startswith(("http://", "https://")):
        s = "https://" + s
    o = _origin_of(s)
    if not o:
        return None
    hn = (urlparse(o).hostname or "").lower()
    parts = hn.split(".")
    if len(parts) < 2 or parts[-1].isdigit():  # один лейбл / IP — не сайт
        return None
    return ".".join(parts[-2:])


def _host_matches_site(url: str, site_key: str) -> bool:
    try:
        hn = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return hn == site_key or hn.endswith("." + site_key)


def _register_page(w, page) -> int:
    """tab_id страницы в реестре воркера: уже зарегистрированная отдаёт свой
    (а не новый — follow_popup и tab_id_for_host плодили по id на каждый
    вызов, реестр пух мёртвыми записями), новая получает следующий номер.
    ЕДИНСТВЕННОЕ место выдачи id playwright-вкладок."""
    for tid, pg in list(w._pages.items()):
        if pg is page:
            return tid
    tid = w._next_tab_id
    w._next_tab_id += 1
    w._pages[tid] = page
    return tid


class _CdpWorker:
    def __init__(self):
        self._req: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._op_lock = threading.Lock()  # одна CDP-операция за раз
        self._pw = None
        self._browser = None
        # Реестры страниц/историй — ПО ПОКОЛЕНИЯМ воркера. Зависший поток
        # брошенного поколения возвращается из playwright-вызова уже после
        # _poison и дописывает в реестр свои Page — они принадлежат мёртвому
        # соединению, и новое поколение видело их как «отслеживаемые вкладки».
        # Поток пишет в словарь СВОЕГО поколения (_gen_of_thread), а живой
        # код — в словарь текущего; доступ снаружи прежний (_pages/_url_hist —
        # свойства, отдающие словарь поколения вызывающего потока)
        self._pages_by_gen: Dict[int, Dict[int, object]] = {}
        self._hist_by_gen: Dict[int, Dict[int, List[str]]] = {}
        self._owner_by_gen: Dict[int, "weakref.WeakValueDictionary"] = {}
        self._local = threading.local()  # _local.gen — поколение потока
        self._next_tab_id = 1  # общий на все поколения: id не переиспользуются
        # Поколение воркера: зависший поток убить нельзя, поэтому он
        # бросается вместе со своей очередью и соединением (см. _poison),
        # а работу продолжает новое поколение
        self._gen = 0
        # Соединения БРОШЕННЫХ поколений (gen → (pw, browser)): закрыть их
        # из другого потока нельзя (playwright-объекты принадлежат своему),
        # поэтому зависший поток закрывает своё сам, когда вызов наконец
        # вернётся (_loop). Иначе сокет к Chrome и процесс playwright-
        # драйвера жили до конца процесса бота.
        self._abandoned: Dict[int, Tuple[object, object]] = {}
        # История URL'ов каждой живой вкладки (id(page) → список, свежий —
        # последний): записанный кем-то URL вкладки устаревает (OAuth-
        # редиректы), а вкладка живёт дальше — матчимся по её прошлым URL.
        # Рядом — ВЛАДЕЛЕЦ записи под тем же ключом, слабой ссылкой:
        # числовой id после сборки мусора ПЕРЕИСПОЛЬЗУЕТСЯ, и новая вкладка
        # наследовала историю чужой (матч по «прошлому URL» уводил команду
        # в другую вкладку). Идентичность сверяется там же, где история и
        # пишется, — в _all_pages.
        self._proc: Optional[subprocess.Popen] = None  # браузер, запущенный нами

    def _gen_of_thread(self) -> int:
        """Поколение, от имени которого работает ВЫЗЫВАЮЩИЙ поток: у потока
        воркера — то, с которым он стартовал (в т.ч. уже брошенное), у любого
        другого (тесты, пробники) — текущее."""
        return int(getattr(self._local, "gen", self._gen))

    @property
    def _pages(self) -> Dict[int, object]:
        """tab_id → Page ТЕКУЩЕГО поколения (см. _pages_by_gen)."""
        return self._pages_by_gen.setdefault(self._gen_of_thread(), {})

    @property
    def _url_hist(self) -> Dict[int, List[str]]:
        """История URL вкладок текущего поколения."""
        return self._hist_by_gen.setdefault(self._gen_of_thread(), {})

    @property
    def _hist_owner(self) -> "weakref.WeakValueDictionary":
        """Владельцы записей истории (слабые ссылки) текущего поколения."""
        return self._owner_by_gen.setdefault(
            self._gen_of_thread(), weakref.WeakValueDictionary())

    # Транспорт (вызывается из любого потока)
    def submit(self, fn, timeout: Optional[float] = None):
        """Операция в потоке воркера. timeout — бюджет ИМЕННО этой операции
        (дефолт SUBMIT_TIMEOUT_SEC; заведомо долгим — навигация, захват
        страницы, завершение браузера — вызывающий задаёт свой). По
        истечении бюджета вызывающий получает BrowserUnavailable, лок
        освобождается, а застрявший поток бросается вместе с его
        playwright-соединением: иначе одна вкладка с alert() или бесконечным
        JS навсегда блокировала ВСЕ браузерные операции процесса, включая
        restart_browser/shutdown_browser/watchdog — они идут сюда же."""
        note_pool_v_activity()  # воркер обслуживает только пул V (headed)
        budget = float(SUBMIT_TIMEOUT_SEC if timeout is None else timeout)
        with self._op_lock:
            req = self._ensure_loop()
            box: Dict[str, object] = {}
            done = threading.Event()
            req.put((fn, box, done))
            if not done.wait(budget):
                self._poison()
                raise BrowserUnavailable(
                    f"браузер не ответил за {int(budget)}с — операция "
                    "брошена, воркер пересоздан")
            if "err" in box:
                raise box["err"]
            return box.get("res")

    def _ensure_loop(self) -> "queue.Queue":
        """Живой поток текущего поколения (под _op_lock) → его очередь."""
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._loop, args=(self._gen, self._req), daemon=True,
                name=f"vpc-cdp-{self._gen}")
            self._thread.start()
        return self._req

    def _poison(self):
        """Поток воркера застрял внутри playwright-вызова. Убить его нельзя
        (в python нет прерывания потока), закрыть его соединение отсюда —
        тоже: playwright-объекты принадлежат своему потоку. Поэтому
        поколение инкрементируется, очередь и соединение заводятся заново, а
        старый поток допишет результат в никому не нужный box и закроет
        СВОЁ соединение сам (_loop: соединение брошенного поколения лежит в
        _abandoned до возврата зависшего вызова). Процесс браузера
        (self._proc) остаётся — его ещё гасить _kill_browser'у нового
        поколения."""
        logger.warning("[BrowserActions] Воркер браузера завис — поток и его "
                       "CDP-соединение брошены, следующая операция поднимет "
                       "новые")
        if self._pw is not None or self._browser is not None:
            self._abandoned[self._gen] = (self._pw, self._browser)
        # Реестры брошенного поколения НЕ чистим и не переиспользуем: в них
        # допишет зависший поток, когда вернётся, — и выбросит их сам
        # (_release_abandoned). Новое поколение начинает с пустых (setdefault)
        self._gen += 1
        self._thread = None
        self._req = queue.Queue()
        self._pw = None
        self._browser = None

    def _loop(self, gen: int, req: "queue.Queue"):
        self._local.gen = gen  # все реестры этого потока — его поколения
        while True:
            if gen != self._gen:
                # Поколение брошено (_poison) ДО получения новой задачи:
                # закрываем своё соединение и уходим — держать сокет к
                # Chrome и процесс playwright-драйвера незачем
                self._release_abandoned(gen)
                return
            fn, box, done = req.get()
            if gen != self._gen:
                # Поколение брошено, пока задача лежала в очереди: за браузер
                # отвечает другой поток — двум живым воркерам на одном
                # браузере не бывать
                box["err"] = BrowserUnavailable("воркер браузера заменён")
                done.set()
                self._release_abandoned(gen)
                return
            try:
                box["res"] = fn(self)
            except BaseException as e:
                box["err"] = e
            finally:
                done.set()
            if gen != self._gen:
                # Зависший вызов ВЕРНУЛСЯ уже после _poison: единственный
                # поток, которому playwright разрешает закрыть это
                # соединение, — этот. Закрываем и умираем.
                self._release_abandoned(gen)
                return

    def _release_abandoned(self, gen: int):
        """(в потоке воркера) Закрыть соединение своего брошенного поколения.
        Хвост №5 аудита: пока зависший вызов не вернулся, сокет к Chrome и
        процесс playwright-драйвера держались до выхода бота — новое
        поколение поднимало ВТОРОЕ соединение поверх живого первого."""
        # Реестры брошенного поколения больше никому не нужны
        self._pages_by_gen.pop(gen, None)
        self._hist_by_gen.pop(gen, None)
        self._owner_by_gen.pop(gen, None)
        pw, browser = self._abandoned.pop(gen, (None, None))
        # Только pw.stop() — как в _drop_connection: browser.close() у
        # connect_over_cdp ГАСИТ Chrome (этим пользуется _kill_browser), а
        # брошенное поколение обязано отпустить соединение, не убивая
        # браузер с вкладками пользователя
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        if pw is not None or browser is not None:
            logger.info(f"[BrowserActions] Соединение брошенного поколения "
                        f"воркера #{gen} освобождено")

    # ── Ниже — только в потоке воркера ──

    def _drop_connection(self):
        self._pages.clear()
        self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    def _connect(self, timeout_ms: int = CDP_CONNECT_TIMEOUT_MS):
        from playwright.sync_api import sync_playwright
        if self._pw is None:
            self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(
                str(_BCFG.get("cdp_url") or CDP_URL), timeout=timeout_ms)
        except Exception as e:
            detail = str(e).split("Call log")[0].strip().split("\n")[0]
            raise BrowserUnavailable(
                f"браузер с отладкой ({_BCFG.get('cdp_url')}) недоступен: {detail}")

    def ensure_browser(self, allow_launch: bool):
        """Живое CDP-подключение: переподключение при обрыве; при отсутствии —
        запуск браузера, если разрешён."""
        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    return
            except Exception:
                pass
            self._drop_connection()
        try:
            self._connect()
            return
        except BrowserUnavailable:
            self._drop_connection()
            if not allow_launch:
                raise
        self._launch_chrome()

    def _launch_chrome(self):
        exe = _resolve_executable()
        if not exe:
            raise BrowserUnavailable(
                "не найден ни один Chromium-браузер (Chrome, Edge, Opera, "
                "Яндекс, Brave, Vivaldi) — укажи browser.executable в конфиге "
                "computer_control или установи один из них")
        udd = _pool_v_profile()
        _check_profile_lock(udd)
        _ensure_v_profile_copy()
        _enable_memory_saver(udd)
        _wipe_saved_zoom_levels(udd)
        port = urlparse(str(_BCFG.get("cdp_url") or CDP_URL)).port or 9222
        cmd = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={udd}",
               "--no-first-run", "--no-default-browser-check",
               "--disable-session-crashed-bubble",
               # Экономия ресурсов: профиль — автоматизационный, фоновая
               # синхронизация/телеметрия/переводчик/каст не нужны, а их
               # фоновая активность добавляет нагрузку на слабом железе
               *CHROME_THRIFT_FLAGS, *CHROME_NO_THROTTLE_FLAGS, "about:blank"]
        # Пониженный приоритет: Chrome бота уступает CPU приложениям
        # пользователя — иначе его всплески давали системные микрофризы
        # (курсор/ввод замирали на секунды)
        popen_kw: Dict[str, object] = dict(
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if sys.platform == "win32":
            popen_kw["creationflags"] = getattr(
                subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        else:
            cmd = ["nice", "-n", "10"] + cmd
        try:
            proc = subprocess.Popen(  # noqa: S603 — путь/флаги из нашего конфига
                cmd, **popen_kw)
        except OSError as e:
            raise BrowserUnavailable(f"не удалось запустить браузер: {e}")
        self._proc = proc
        logger.info(f"[BrowserActions] Запускаю браузер с отладкой: {exe} "
                    f"(профиль {udd}, порт {port})")
        deadline = time.monotonic() + BROWSER_LAUNCH_TIMEOUT_SEC
        last_err: Optional[Exception] = None
        # Любой выход из запуска НЕ через return — осиротевший Chrome на
        # профиле пула V: добиваем его (иначе SingletonLock держится, и пул V
        # не поднимется уже никогда), см. _abort_launch
        try:
            while time.monotonic() < deadline:
                try:
                    self._connect(timeout_ms=1500)
                    logger.info("[BrowserActions] Браузер запущен, CDP подключён")
                    note_pool_v_activity()
                    # Режим управления нигде не включён — окно пула V
                    # показывать незачем (запустили ради hidden-сайта/rescue)
                    if not control_mode_any():
                        _hide_pool_window(proc.pid)
                    return
                except BrowserUnavailable as e:
                    last_err = e
                    if proc.poll() is not None:
                        # Процесс умер сразу — типично при занятом профиле:
                        # запуск просто открыл окно в уже работающем Chrome
                        # без отладки
                        raise BrowserUnavailable(
                            "браузер завершился сразу после запуска — похоже, "
                            "этот профиль уже занят запущенным Chrome без "
                            "отладки. Закрой его полностью и повтори (или "
                            "смени browser.user_data_dir)")
                    time.sleep(0.3)
            raise BrowserUnavailable(
                f"не дождался отладочного порта за "
                f"{int(BROWSER_LAUNCH_TIMEOUT_SEC)}с"
                f"{': ' + str(last_err) if last_err else ''}. Если профиль — "
                "основной профиль Chrome 136+, порт для него игнорируется: "
                "задай отдельный browser.user_data_dir в конфиге")
        except BaseException:
            _abort_launch(proc, udd, "браузера пула V")
            if self._proc is proc:
                self._proc = None
            self._drop_connection()
            raise

    def _kill_browser(self, user_data_dir: str, grace_sec: float = 10.0) -> bool:
        """(в потоке воркера) Завершить процесс браузера на профиле пула V.
        Сначала грациозный browser.close() по CDP — Chrome пишет куки/сессии
        на диск и уходит за ~1с (голый SIGTERM он игнорировал 10с до SIGKILL,
        теряя свежие куки — кейс 14.09). Остатки — _kill_chrome_on_profile."""
        try:
            if self._browser is not None and self._browser.is_connected():
                self._browser.close()
                t0 = time.monotonic()
                while time.monotonic() - t0 < 5:
                    if self._proc is None or self._proc.poll() is not None:
                        break
                    time.sleep(0.2)
        except Exception:
            pass
        self._drop_connection()
        died = _kill_chrome_on_profile(self._proc, user_data_dir,
                                       grace_sec=grace_sec)
        self._proc = None
        return died

    _URL_HIST_MAX = 30  # сколько прошлых URL помним на вкладку

    def _all_pages(self) -> List:
        """Живые страницы браузера — ЕДИНОЕ место учёта: гарантирует
        соединение (иначе первый же вызов на непрогретом воркере падал
        AttributeError на self._browser=None — кейс page_for_user_visible),
        ведёт историю URL и вычищает закрытые вкладки из реестра _pages.
        Раньше чистка реестра жила в трёх функциях (tab_id_for_host,
        list_tabs_detailed, close_tab), и мимо них он копил мёртвые
        страницы, в т.ч. от брошенного поколения воркера."""
        if self._browser is None:
            self.ensure_browser(allow_launch=False)
        pages = [p for ctx in self._browser.contexts for p in ctx.pages
                 if not p.is_closed()]
        alive = set()
        for p in pages:
            k = id(p)
            alive.add(k)
            if self._hist_owner.get(k) is not p:
                # Ключ занят ДРУГОЙ (уже собранной) вкладкой: id
                # переиспользован — чужую историю не наследуем
                self._url_hist.pop(k, None)
                try:
                    self._hist_owner[k] = p
                except TypeError:  # объект без слабых ссылок — как раньше
                    pass
            u = p.url or ""
            h = self._url_hist.setdefault(k, [])
            if u and (not h or h[-1] != u):
                h.append(u)
                del h[:-self._URL_HIST_MAX]
        for k in list(self._url_hist):  # закрытые вкладки — из памяти
            if k not in alive:
                del self._url_hist[k]
                self._hist_owner.pop(k, None)
        self._purge_pages()
        return pages

    def _purge_pages(self):
        """Выбросить из реестра tab_id → Page закрытые и чужие страницы.
        «Чужая» — та, чей is_closed() падает: так выглядят страницы
        БРОШЕННОГО поколения воркера (их playwright-соединение уже
        остановлено), если зависший вызов успел их зарегистрировать."""
        for tid, pg in list(self._pages.items()):
            try:
                dead = pg.is_closed()
            except Exception:
                dead = True
            if dead:
                self._pages.pop(tid, None)

    def register_page(self, page) -> int:
        return _register_page(self, page)

    @staticmethod
    def _visible_of(candidates) -> Optional[object]:
        """Видимая (активная в своём окне) вкладка из кандидатов:
        document.visibilityState === 'visible' (фоновые вкладки Chrome —
        hidden). «Активную вкладку» CDP не сообщает, но видимость — это и
        есть вкладка, на которую смотрит пользователь. При нескольких окнах
        видимых несколько — предпочитаем вкладку окна с ФОКУСОМ (туда
        пользователь кликал последний раз), иначе последнюю (свежее окно).
        Фокуса нет ни у кого (пользователь печатает в мессенджере) — тоже
        последняя."""
        vis = []
        for p in reversed(candidates):
            try:
                if p.evaluate("document.visibilityState") == "visible":
                    vis.append(p)
            except Exception:
                continue
        if len(vis) <= 1:
            return vis[0] if vis else None
        for p in vis:
            try:
                if p.evaluate("document.hasFocus()"):
                    return p
            except Exception:
                continue
        return vis[0]

    def page_for_user_visible(self):
        """Вкладка, на которую смотрит пользователь, — ТЕМ ЖЕ источником, что
        visible_page_info() (macOS: активная вкладка переднего окна через
        AppleScript): tab_op без явной цели подписывается по нему на резолве,
        и исполнение обязано совпасть с подписью (кейс 10.09: «закрой
        вкладку» подписалось «YouTube», а закрыло платформу — CDP
        visibilityState у Chrome 152 'visible' у всех вкладок окна, и
        _visible_of брал просто последнюю). None — источник молчит (не
        macOS, нет прав) или вкладки нет среди страниц: вызывающий падает
        на прежнюю CDP-эвристику page_for(None, None); проще —
        current_user_page(), она и есть «вкладка по умолчанию»."""
        url = _front_window_url()
        if not url or _chat_or_service_url(url):
            return None
        pages = [p for p in self._all_pages()
                 if not _chat_or_service_url(p.url)]
        # Точный URL; SPA успел сменить query за доли секунды — по origin;
        # дублей берём последнюю (свежую), как page_for
        exact = [p for p in pages if (p.url or "") == url]
        if exact:
            return exact[-1]
        origin = _origin_of(url)
        if origin:
            same = [p for p in pages if (p.url or "").startswith(origin)]
            if same:
                return same[-1]
        return None

    def current_user_page(self):
        """ЕДИНОЕ понятие «текущая пользовательская вкладка» — вкладка, в
        которую уходит любая команда без явной цели: сначала та, на которую
        человек реально смотрит (page_for_user_visible — один источник с
        подписью действия на резолве), иначе CDP-эвристика page_for(None,
        None). Раньше эта пара была расписана по месту у reload/close/
        history, а scan_search (номерные рецепты «третье видео») вообще брал
        ПЕРВУЮ вкладку из списка — «включи третье видео» уезжало на чужую
        страницу того же сайта."""
        return self.page_for_user_visible() or self.page_for(None, None)

    def page_for(self, host_part: Optional[str], tab_id: Optional[int] = None):
        """Вкладка по tab_id (реестр отслеживаемых) или по подстроке URL
        (видимая из подходящих, иначе последняя — свежая); host_part=None —
        видимая пользовательская, иначе крайняя открытая."""
        self.ensure_browser(allow_launch=False)
        if tab_id is not None:
            if is_raw_tab(tab_id):
                # Отдельный класс, а не общий BrowserUnavailable: «playwright
                # эту вкладку не видит» должно быть отличимо от «проверили и
                # чисто» у обёрток вроде detect_antibot
                raise RawTabUnsupported(
                    "фоновая вкладка: доступны только чтение и ввод в чат")
            pg = self._pages.get(tab_id)
            if pg is None or pg.is_closed():
                self._pages.pop(tab_id, None)
                raise BrowserUnavailable("отслеживаемая вкладка закрыта")
            return pg
        pages = self._all_pages()
        if host_part is None:
            if not pages:
                raise BrowserUnavailable("нет открытого окна браузера")
            # Служебные вкладки веб-чатов и вкладка чата бота — не
            # «крайняя страница» для команд (см. также _snapshot_for)
            nonsvc = [p for p in pages
                      if not _chat_or_service_url(p.url)]
            vis = self._visible_of(nonsvc)
            if vis is not None:
                return vis
            return (nonsvc or pages)[-1]
        matches = [p for p in pages if host_part in (p.url or "")]
        if not matches:
            # Вкладка уже ушла дальше по цепочке редиректов с того URL, что
            # успели записать (OAuth: platform → auth?state=… → platform):
            # ищем вкладку, в истории URL'ов которой записанный встречался
            matches = [p for p in pages
                       if host_part in self._url_hist.get(id(p), ())]
        if not matches:
            # Полный URL видимой вкладки мог устареть за миллисекунды:
            # OAuth-редиректы (…/auth?...state=…&nonce=…) одноразовые и
            # сменяются сразу после чтения, SPA переписывает query (кейс
            # 10.09: auth.school.example.com → «нет открытой вкладки» при живой
            # вкладке). Фолбэк на origin (scheme://host)
            origin = _origin_of(host_part)
            if origin:
                matches = [p for p in pages
                           if (p.url or "").startswith(origin)]
        if not matches:
            # Auth-вкладка уже вернулась на redirect_uri (platform…):
            # origin'ы URL'ов из query записанного (?redirect_uri=… и т.п.)
            redir = _redirect_origins_of(host_part)
            if redir:
                matches = [p for p in pages
                           if any((p.url or "").startswith(o) for o in redir)]
        if not matches:
            # Последний фолбэк — семейство сайта (auth.school.example.com →
            # school.example.com): редирект мог быть без ?redirect_uri
            site = _site_key(host_part)
            if site:
                matches = [p for p in pages
                           if _host_matches_site(p.url or "", site)]
        if not matches:
            raise BrowserUnavailable(f"нет открытой вкладки {host_part}")
        # Дубли сайта: действуем на вкладке, на которую смотрит пользователь
        vis = self._visible_of(matches)
        if vis is not None:
            return vis
        return matches[-1]

    def new_page(self, url: str, focus: bool = False) -> int:
        self.ensure_browser(allow_launch=True)
        ctx = self._browser.contexts[0] if self._browser.contexts \
            else self._browser.new_context()
        page, quiet = _open_page_gateway_retry(self, ctx, url)
        tid = self._next_tab_id
        self._next_tab_id += 1
        self._pages[tid] = page
        logger.info(f"[BrowserActions] Открыта вкладка #{tid}: {url[:80]}")
        if focus:
            # Команда «открой сайт» от пользователя: переключаем на страницу —
            # вкладка И окно на передний план (CDP bring_to_front; на macOS
            # ещё AppleScript activate — CDP окно поднимает не всегда)
            try:
                page.bring_to_front()
            except Exception as e:
                logger.debug(f"[BrowserActions] bring_to_front не удался: {e}")
            try:
                final_url = (page.url or "").strip() or url
            except Exception:
                final_url = url
            if final_url and final_url != "about:blank":
                _focus_browser_tab(final_url)
        elif quiet:
            self._activate_tab_quietly(page, url)
        self._ensure_zoom_normal(page)
        return tid

    def _new_page_quiet(self, ctx, url: str):
        """Вкладка БЕЗ выдёргивания окна на передний план:
        Target.createTarget(background:true) через browser-level CDP-сессию,
        playwright-обёртку ждём событием контекста (expect_page — оно же
        качает события соединения; пассивный опрос _all_pages() события не
        прокачивает и вкладку «не видит»). Любая неудача — обычный
        ctx.new_page(): окно всплывёт, но команда выполнится.
        → (page, quiet): quiet=True — вкладка создана фоновой (не активная)."""
        quiet = False
        try:
            session = self._browser.new_browser_cdp_session()
            try:
                with ctx.expect_page(timeout=5000) as ev:
                    session.send("Target.createTarget",
                                 {"url": "about:blank", "background": True})
                page = ev.value
                quiet = True
            finally:
                try:
                    session.detach()
                except Exception:
                    pass
        except Exception as e:
            logger.info(f"[BrowserActions] Фоновое создание вкладки не "
                        f"сработало ({e}) — обычное открытие")
            page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=int(NAV_GOTO_TIMEOUT_SEC * 1000))
        except Exception:
            pass  # страница догружается в фоне — готовность ждёт снапшот
        return page, quiet

    def _activate_tab_quietly(self, page, url: str):
        """Только что открытая фоновая вкладка → АКТИВНАЯ в своём окне, при
        этом окно браузера НЕ всплывает поверх занятий пользователя:
        «открой сайт» из чата означает «подготовь вкладку», а не «дёрни меня
        в браузер». Фоновая вкладка не видна, пока не кликнешь её — поэтому
        переключаем тихо (macOS: AppleScript без activate, см.
        _select_browser_tab_quietly). Не macOS / неудача — вкладка остаётся
        фоновой: тишина важнее переключения (CDP-пути Page.bringToFront и
        Target.activateTarget активируют приложение целиком — это всплытие)."""
        if sys.platform != "darwin":
            return
        try:
            final_url = (page.url or "").strip() or url
        except Exception:
            final_url = url
        if not final_url or final_url == "about:blank":
            return
        if _select_browser_tab_quietly(final_url):
            logger.info(f"[BrowserActions] Вкладка сделана активной без "
                        f"подъёма окна: {final_url[:80]}")
        else:
            logger.info(f"[BrowserActions] Тихо выбрать вкладку не удалось — "
                        f"осталась фоновой: {final_url[:80]}")

    _ZOOM_RATIO_TOL = 0.05  # допуск inner/outer около 1.0: один шаг зума
    # Chrome (90% → 1.111, 110% → 0.909) обязан детектиться, а легальные
    # состояния 100% дают ровно 1.000 (замер 19.09)

    @staticmethod
    def _zoom_ratio(page) -> float:
        """innerWidth/outerWidth: 1.0 при зуме 100% (оба в CSS px, Retina/dpr
        не влияет), 2.0 при зуме 50% и т.д. outerWidth=0 (странный таргет)
        считаем нормой — не мешаем."""
        try:
            return float(page.evaluate(
                "window.outerWidth ? window.innerWidth/window.outerWidth : 1"
            ) or 1)
        except Exception:
            return 1.0

    def _ensure_zoom_normal(self, page):
        """Сбросить чужой зум вкладки к 100%. Per-host зум Chrome переживает
        рестарт и ломает вёрстку/скриншоты (кейс 19.09, dodo: зум 50% —
        контент колонкой в пол-окна, кадр 3228×3440), CDP-override'ы его не
        перебивают (мультиплицируются им). Просим как пользователь — ⌘0 (на
        Win/Linux акселератор доходит; на macOS браузерные акселераторы из
        CDP молча не срабатывают — там лечение это wipe карты зумов на
        старте пула, а точечно — команда «сбрось масштаб», zoom_page).
        Зум, выставленный самим пользователем командой (_INTENTIONAL_ZOOM),
        не трогаем."""
        r = self._zoom_ratio(page)
        if abs(r - 1.0) <= self._ZOOM_RATIO_TOL:
            return
        try:
            where = urlparse(page.url or "").hostname or page.url or ""
        except Exception:
            where = ""
        with _INTENTIONAL_ZOOM_LOCK:
            if (where or "").lower().removeprefix("www.") in _INTENTIONAL_ZOOM:
                return  # зум выставлен командой пользователя — не откатываем
        logger.warning(f"[BrowserActions] Зум вкладки ≠100% "
                       f"(~{round(100 / max(r, 0.05))}%, {where}) — сбрасываю")
        try:
            page.keyboard.press(
                "Meta+0" if sys.platform == "darwin" else "Control+0")
            time.sleep(0.4)
        except Exception as e:
            logger.debug(f"[BrowserActions] Клавиша сброса зума не ушла: {e}")
            return
        r2 = self._zoom_ratio(page)
        if abs(r2 - 1.0) <= self._ZOOM_RATIO_TOL:
            logger.info("[BrowserActions] Зум вкладки сброшен до 100%")
        else:
            logger.warning(f"[BrowserActions] Зум не сбросился "
                           f"(inner/outer={r2:.2f}) — станет 100% при "
                           f"перезапуске браузера бота (карта зумов чистится "
                           f"на старте); срочно — ⌘0 в окне бота вручную")

    def tab_id_for_host(self, host_part: str) -> Optional[int]:
        self.ensure_browser(allow_launch=False)
        self._purge_pages()
        for tid, pg in list(self._pages.items()):
            if host_part in (pg.url or ""):
                return tid
        matches = [p for p in self._all_pages() if host_part in (p.url or "")]
        if not matches:
            return None
        return self.register_page(matches[-1])

    def list_tabs_detailed(self) -> List[Tuple[int, str, str, str]]:
        """(tab_id, url, host, title) живых страниц, кроме служебных
        (web_llm) и вкладки чата — для «перейди на вкладку X» и «какие
        вкладки открыты». Страницы регистрируются в _pages, так что id
        стабильны для activate_tab и последующих команд."""
        self.ensure_browser(allow_launch=False)
        out: List[Tuple[int, str, str, str]] = []
        for p in self._all_pages():
            host = (urlparse(p.url).hostname or "").lower()
            # Служебная вкладка веб-чата / вкладка чата бота — не цель
            # переключения. Фильтр ОДИН на весь файл (_chat_or_service_url →
            # is_service_host): своя копия здесь знала только про
            # _SERVICE_HOSTS и пропускала статические адаптеры web_llm
            if not host or _chat_or_service_url(p.url):
                continue
            tid = self.register_page(p)
            try:
                title = str(p.title() or "")
            except Exception:
                title = ""
            out.append((tid, p.url, host, title))
        return out

    def _scan_order(self) -> List:
        """Порядок обхода вкладок для рецептов выдачи (scan_search): первой
        — ТЕКУЩАЯ пользовательская (current_user_page), затем остальные в
        порядке браузера. AppleScript-ветка всегда так и делала (активная
        вкладка переднего окна → поиск по остальным), а CDP-ветка брала
        просто первую страницу списка: «включи третье видео» при двух
        открытых ютубах жало в невидимой вкладке (кейс номерных рецептов)."""
        pages = self._all_pages()
        try:
            first = self.current_user_page()
        except BrowserUnavailable:
            first = None
        if first is None:
            return pages
        return [first] + [p for p in pages if p is not first]

    def eval_js(self, host_part: Optional[str], js: str,
                scan_search: bool = False, tab_id: Optional[int] = None,
                front: bool = False) -> str:
        """JS во вкладке. scan_search (рецепты выдачи): пробуем все вкладки,
        возвращаем первый «ok:…» — JS сам проверяет сайт/страницу.
        front=True — выдёргивать вкладку на передний план: только по явной
        команде пользователя («перейди на вкладку»); рабочие действия бота
        (снапшоты, клики, ввод) окно НЕ поднимают — bring_to_front на каждый
        чих всплывал поверх окон пользователя."""
        self.ensure_browser(allow_launch=False)
        if scan_search and host_part is None and tab_id is None:
            last: Optional[str] = None
            for p in self._scan_order():
                try:
                    r = str(p.evaluate(js) or "")
                except Exception:
                    continue
                if r.startswith("ok:"):
                    return r
                last = r
            if last is not None:
                return last
            raise BrowserUnavailable("нет открытой вкладки с результатами поиска")

        page = self.page_for(host_part, tab_id)
        if front:
            try:
                page.bring_to_front()
            except Exception:
                pass
        return str(page.evaluate(js) or "")


_WORKER = _CdpWorker()


def _cdp_available() -> bool:
    """Быстрый пробник: CDP-подключение живо или устанавливается сходу
    (connection refused — мгновенно, таймаута нет)."""
    try:
        _WORKER.submit(lambda w: w.ensure_browser(allow_launch=False),
                       timeout=SUBMIT_PROBE_TIMEOUT_SEC)
        return True
    except Exception:
        return False


# Перезапуск браузера: не чаще раза в RESTART_COOLDOWN_SEC — серия неудачных
# отправок веб-чата иначе устраивала бы restart-шторм (каждый ~10с)
RESTART_COOLDOWN_SEC = 60.0
_RESTART_LOCK = threading.Lock()
_LAST_RESTART_TS = 0.0


def _close_pool_h_graceful():
    """Грациозное закрытие пула H по CDP (Browser.close): куки/сессии пишутся
    на диск (cf_clearance после rescue!), процесс уходит за ~1с вместо
    10-секундного SIGTERM-таймаута под SIGKILL. Best effort."""
    try:
        if _RAW_CLIENTS[_POOL_H] is None:
            return
        _raw_call("Browser.close", pool=_POOL_H)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5:
            if _POOL_H_PROC is not None and _POOL_H_PROC.poll() is not None:
                return
            if not _pool_h_alive():
                return
            time.sleep(0.2)
    except Exception:
        pass


def _reset_raw_pool(pool: str):
    """Сбросить вкладки и сокет пула (они умирают вместе с его Chrome) —
    следующие вызовы не должны стрелять в мёртвые сессии."""
    with _RAW_LOCKS[pool]:
        with _RAW_TABS_LOCK:
            for tid in [t for t, tab in _RAW_TABS.items()
                        if _pool_of_tab(tab) == pool]:
                _RAW_TABS.pop(tid, None)
        cl = _RAW_CLIENTS[pool]
        if cl is not None:
            try:
                cl.close()
            except Exception:
                pass
            _RAW_CLIENTS[pool] = None


def shutdown_browser(reason: str = "выход бота"):
    """Завершить ОБА Chrome бота при остановке бота: раньше браузер оставался
    жить после выхода (окно + вкладки веб-чатов грузили систему бессрочно).
    Трогаем только профили бота — Chrome с основным профилем пользователя
    сюда не попадает и не убивается никогда. Безопасно при незапущенных
    браузерах и не-CDP бэкенде."""
    if str(_BCFG.get("backend") or "auto") == "safari":
        return
    try:
        if _WORKER._thread is not None:
            udd_v = _pool_v_profile()
            if not _is_default_browser_profile(udd_v):
                logger.info(f"[BrowserActions] Завершение пула V ({reason})")
                try:
                    _WORKER.submit(lambda w: w._kill_browser(udd_v),
                                   timeout=SUBMIT_KILL_TIMEOUT_SEC)
                except BrowserUnavailable as e:
                    # Воркер завис/пересоздан — выход бота не ждёт его:
                    # процесс браузера гасим напрямую
                    logger.info(f"[BrowserActions] Пул V через воркер не "
                                f"закрылся ({e}) — гашу процесс напрямую")
                    _kill_chrome_on_profile(_WORKER._proc, udd_v)
        _reset_raw_pool(_POOL_V)
    except Exception as e:
        logger.debug(f"[BrowserActions] Завершение пула V не удалось: {e}")
    try:
        _shutdown_pool_h(reason)
    except Exception as e:
        logger.debug(f"[BrowserActions] Завершение пула H не удалось: {e}")


def _shutdown_pool_h(reason: str):
    """Пул H: гасим только если он реально запускался (процесс наш или живой
    SingletonLock его профиля) — лишний Chrome не поднимаем ради выключения."""
    global _POOL_H_PROC
    udd = _pool_h_profile()
    if _is_default_browser_profile(udd):
        return
    if _POOL_H_PROC is None and not os.path.islink(
            os.path.join(udd, "SingletonLock")):
        return
    logger.info(f"[BrowserActions] Завершение пула H ({reason})")
    _close_pool_h_graceful()
    _reset_raw_pool(_POOL_H)
    _kill_chrome_on_profile(_POOL_H_PROC, udd)
    _POOL_H_PROC = None


def restart_browser(reason: str = "",
                    cooldown_sec: float = RESTART_COOLDOWN_SEC,
                    pool: Optional[str] = None) -> bool:
    """Перезапустить браузер бота: закрыть процесс и открыть заново с CDP.
    Лечит залипшие состояния страницы, которые не чинятся навигацией
    (кейс 26.08: промпт введён, Enter нажат — поле не очистилось, сообщение
    не появилось в ленте; закрытый цикл отправки дважды не прошёл).
    Убивается ТОЛЬКО Chrome на профиле бота — браузер с основным профилем
    пользователя не трогаем никогда (там вкладки и сессии человека).
    pool: V (дефолт) — видимый браузер команд; H — headless веб-чатов
    (перезапуск = убить процесс и сбросить вкладки, поднимется лениво при
    следующем обращении). → True, если браузер после этого доступен по CDP."""
    # Safari — браузер пользователя без отдельного профиля: процесс не
    # перезапускаем никогда (убивать чужой браузер нельзя)
    if str(_BCFG.get("backend") or "auto") == "safari":
        logger.info("[BrowserActions] Перезапуск неприменим к Safari")
        return False
    global _LAST_RESTART_TS
    with _RESTART_LOCK:
        if time.time() - _LAST_RESTART_TS < cooldown_sec:
            logger.info(f"[BrowserActions] Перезапуск браузера пропущен — "
                        f"уже перезапускали <{int(cooldown_sec)}с назад")
            return False
        _LAST_RESTART_TS = time.time()
    pool = pool or _POOL_V
    logger.warning(f"[BrowserActions] Перезапуск браузера бота, пул "
                   f"{pool.upper()}{f' ({reason})' if reason else ''}")
    if pool == _POOL_H:
        global _POOL_H_PROC
        _close_pool_h_graceful()
        _reset_raw_pool(_POOL_H)
        udd_h = _pool_h_profile()
        if not _is_default_browser_profile(udd_h):
            _kill_chrome_on_profile(_POOL_H_PROC, udd_h, grace_sec=3.0)
        _POOL_H_PROC = None
        return True  # поднимется лениво на следующем вызове
    _reset_raw_pool(_POOL_V)
    udd = _pool_v_profile()
    if not _is_default_browser_profile(udd):
        try:
            _WORKER.submit(lambda w: w._kill_browser(udd, grace_sec=3.0),
                           timeout=SUBMIT_KILL_TIMEOUT_SEC)
        except BrowserUnavailable as e:
            # Перезапуск — лечение ЗАВИСШЕГО браузера, поэтому он обязан
            # работать и через зависший воркер (тот уже пересоздан submit'ом)
            logger.info(f"[BrowserActions] Закрытие пула V через воркер не "
                        f"удалось ({e}) — гашу процесс напрямую")
            _kill_chrome_on_profile(_WORKER._proc, udd, grace_sec=3.0)
    else:
        logger.warning("[BrowserActions] Профиль — основной профиль "
                       "пользователя: процесс не убиваю, только "
                       "переподключаюсь")
    try:
        _WORKER.submit(lambda w: w.ensure_browser(
            allow_launch=bool(_BCFG.get("launch", True))),
            timeout=SUBMIT_LAUNCH_TIMEOUT_SEC)
        logger.info("[BrowserActions] Браузер бота перезапущен, CDP подключён")
        return True
    except BrowserUnavailable as e:
        logger.warning(f"[BrowserActions] Браузер не поднялся после "
                       f"перезапуска: {e}")
        return False


def backend_forced() -> bool:
    """backend != auto: бэкенд выбран явно — ошибки не маскируем фолбэками."""
    return str(_BCFG.get("backend") or "auto") != "auto"


def _select_backend(tab_op: bool) -> str:
    """→ 'cdp' | 'applescript' | 'safari'. tab_op=True — операция над уже
    открытой вкладкой: запускать новый (пустой) браузер бессмысленно,
    фолбэк/ошибка важнее. tab_op=False (открытие URL) — браузер можно
    запустить."""
    backend = str(_BCFG.get("backend") or "auto")
    if backend == "safari":
        if sys.platform != "darwin":
            raise BrowserUnavailable("safari-бэкенд доступен только на macOS")
        return "safari"
    if backend == "applescript":
        if sys.platform != "darwin":
            raise BrowserUnavailable("applescript-бэкенд доступен только на macOS")
        return "applescript"
    if backend == "cdp":
        _WORKER.submit(lambda w: w.ensure_browser(
            allow_launch=bool(_BCFG.get("launch", True))),
            timeout=SUBMIT_LAUNCH_TIMEOUT_SEC)
        return "cdp"
    # auto: CDP — основной путь; AppleScript — fallback (только macOS)
    if _cdp_available():
        return "cdp"
    if not tab_op and _BCFG.get("launch", True):
        try:
            _WORKER.submit(lambda w: w.ensure_browser(allow_launch=True),
                           timeout=SUBMIT_LAUNCH_TIMEOUT_SEC)
            return "cdp"
        except BrowserUnavailable as e:
            if sys.platform != "darwin":
                raise
            logger.info(f"[BrowserActions] CDP-запуск не удался, "
                        f"фолбэк на AppleScript: {e}")
    if sys.platform == "darwin":
        # Chromium не установлен вовсе — но есть Safari: его диалект
        if not _chrome_present() and _safari_present():
            return "safari"
        return "applescript"
    raise BrowserUnavailable(
        "браузер с отладкой недоступен. Закрой Chrome и запусти его с "
        "--remote-debugging-port=9222, либо разреши browser.launch в конфиге "
        "computer_control — бот запустит браузер сам")


# ── macOS fallback: Apple Events ─────────────────────────

def _as_lit(s: Optional[str]) -> str:
    """ЕДИНСТВЕННОЕ место экранирования строки для вставки в двойные кавычки
    AppleScript — что для строкового литерала самого AppleScript (URL,
    host_part в `contains "…"`), что для JS-текста, который передаётся как
    `execute … javascript "…"`/`do JavaScript "…" in t`: экранируются те же
    два символа (`\\` и `"`), и AppleScript сам вернёт их JS-движку как есть.
    Раньше часть сборщиков (_find_tab_applescript: host_part/_origin в
    `contains "…"`) вставляла значение вообще без экранирования — там, где
    источник значения был не свободным пользовательским текстом, это не
    стреляло, но было местной копией одной и той же логики без общей точки."""
    return str(s or "").replace("\\", "\\\\").replace('"', '\\"')


# ── Адресация приложения в AppleScript (единая точка) ──
# Apple Events адресуют приложение ПО ИМЕНИ (или bundle id): pid Chrome-suite
# не понимает — по pid адресуется только System Events (_as_proc_ref, им уже
# пользуется _ax_zoom_click). Отсюда два правила, собранные здесь и больше
# нигде: имя приложения берётся из конфига (канал/бинарь браузера бота, а не
# зашитый «Google Chrome» — на Edge/Brave/Яндексе мосты просто не работали),
# а если экземпляров этого браузера запущено несколько (Chrome пула V + личный
# Chrome пользователя — одно имя, один bundle id), мост по имени отказывается
# работать: попасть можно в любой, а трогать личный браузер нельзя.
AS_NO_APP = "__no_app__"          # сентинел: приложение не запущено
_SAFARI_APP = "Safari"
_SYS_EVENTS_APP = "System Events"


def _as_app_name() -> str:
    """AppleScript-имя браузера бота: browser.executable (…/Brave Browser.app/
    … → «Brave Browser») → browser.channel → «Google Chrome»."""
    exe = _BCFG.get("executable")
    if isinstance(exe, str) and exe.strip():
        for part in os.path.normpath(exe.strip()).split(os.sep):
            if part.lower().endswith(".app"):
                return part[:-4]
    channel = str(_BCFG.get("channel") or "chrome").lower()
    return _QUIET_TAB_APPS.get(channel) or "Google Chrome"


def _as_tell(body: str, app: Optional[str] = None, guard: bool = True,
             sentinel: str = AS_NO_APP) -> str:
    """`tell application "<app>" … end tell` — ЕДИНСТВЕННОЕ место, где скрипт
    адресует приложение. app=None — браузер бота (_as_app_name).
    guard=True добавляет проверку «приложение запущено»: Apple Events сами
    ЗАПУСКАЮТ адресата, и мост «прочитай переднее окно» на маке без этого
    браузера молча поднимал пустое окно вместо честного отказа; не запущено
    — скрипт возвращает sentinel. guard=False — запуск уместен («открой
    сайт»)."""
    name = _as_lit(app or _as_app_name())
    head = (f'if application "{name}" is not running then return "{sentinel}"\n'
            if guard else "")
    return f'{head}tell application "{name}"\n{body}end tell\n'


def _as_tell_to(cmd: str, app: Optional[str] = None) -> str:
    """Однострочный `tell application "<app>" to <cmd>` (activate, keystroke)."""
    return f'tell application "{_as_lit(app or _as_app_name())}" to {cmd}\n'


def _as_proc_ref(pid: int) -> str:
    """Ссылка на процесс ПО PID для System Events — единственный способ
    адресовать именно наш экземпляр браузера (имя у бота и у личного Chrome
    одно): скрытие окна пула, клик по меню «Вид», подсчёт экземпляров."""
    return f"(first process whose unix id is {int(pid)})"


_AS_PIDS_TTL = 2.0
_AS_PIDS_CACHE: Dict[str, Tuple[float, Optional[List[int]]]] = {}


def _as_browser_pids(app: Optional[str] = None) -> Optional[List[int]]:
    """PID'ы запущенных процессов приложения (System Events). None — спросить
    не удалось (не macOS, нет прав Accessibility): тогда ведём себя как
    раньше. Результат кэшируется на _AS_PIDS_TTL — вызов идёт на каждом
    резолве команды."""
    name = app or _as_app_name()
    now = time.monotonic()
    hit = _AS_PIDS_CACHE.get(name)
    if hit is not None and now - hit[0] < _AS_PIDS_TTL:
        return hit[1]
    pids: Optional[List[int]] = None
    if sys.platform == "darwin":
        script = _as_tell(
            f'  set out to ""\n'
            f'  repeat with pr in (every process whose name is '
            f'"{_as_lit(name)}")\n'
            "    set out to out & (unix id of pr) & \" \"\n"
            "  end repeat\n"
            "  return out\n", app=_SYS_EVENTS_APP, guard=False)
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                pids = [int(t) for t in (r.stdout or "").split() if t.isdigit()]
        except Exception as e:
            logger.debug(f"[BrowserActions] Экземпляры {name} не посчитаны: {e}")
    _AS_PIDS_CACHE[name] = (now, pids)
    return pids


_AS_BOT_URLS_TTL = 1.5
_AS_BOT_URLS_CACHE: Tuple[float, Tuple[str, ...]] = (0.0, ())
_AS_BOT_URLS_LOCK = threading.Lock()


def _bot_page_urls() -> Tuple[str, ...]:
    """URL всех вкладок браузеров БОТА (пулы V и H) — через HTTP /json/list
    отладочного порта, а НЕ через воркер: проверку зовут из его же потока
    (page_for_user_visible → _front_window_url), и submit оттуда — вечная
    самоблокировка на _op_lock. Кэш на _AS_BOT_URLS_TTL: на один резолв
    команды проверка приходит несколько раз."""
    global _AS_BOT_URLS_CACHE
    now = time.monotonic()
    with _AS_BOT_URLS_LOCK:
        ts, urls = _AS_BOT_URLS_CACHE
        if now - ts < _AS_BOT_URLS_TTL:
            return urls
    import urllib.request
    out: List[str] = []
    for base in (str(_BCFG.get("cdp_url") or CDP_URL), _pool_h_cdp_url()):
        try:
            with urllib.request.urlopen(
                    base.rstrip("/") + "/json/list", timeout=2) as r:
                for t in json.loads(r.read().decode("utf-8", "replace")) or []:
                    u = str((t or {}).get("url") or "")
                    if u:
                        out.append(u)
        except Exception as e:
            logger.debug(f"[BrowserActions] Список вкладок {base} не получен: {e}")
    res = tuple(out)
    with _AS_BOT_URLS_LOCK:
        _AS_BOT_URLS_CACHE = (now, res)
    return res


def _page_key(url: str) -> str:
    """scheme://host/path без query и #fragment — «та же страница» для сверки
    переднего окна с вкладками пула (SPA переписывает query за миллисекунды,
    а вот путь у чужой страницы другой: personal /watch vs бот /results)."""
    try:
        u = urlparse(url or "")
    except Exception:
        return ""
    if not u.hostname:
        return ""
    host = u.hostname.lower().removeprefix("www.")
    return f"{(u.scheme or '').lower()}://{host}{(u.path or '').rstrip('/')}"


def _as_url_is_bot_page(url: str) -> bool:
    """URL открыт в браузере бота (вкладка пула V или H) — доказательство,
    что Apple Event ушёл НАШЕМУ процессу, а не личному Chrome
    пользователя."""
    if not url:
        return False
    ours = _bot_page_urls()
    if url in ours:
        return True
    key = _page_key(url)
    return bool(key) and any(_page_key(u) == key for u in ours)


def _as_foreign_instance(url: str) -> bool:
    """Ответ моста пришёл от ЧУЖОГО экземпляра браузера — ЕДИНАЯ точка
    решения. Да, если одновременно: экземпляров с этим именем больше одного,
    у бота есть живой отладочный порт со списком вкладок, и переднего окна
    (url) в этом списке нет. Нет отладочного порта вовсе (чистый
    applescript-бэкенд: браузер пользователя и есть браузер бота) — сверять
    не с чем, мосту не мешаем."""
    pids = _as_browser_pids()
    if pids is None or len(pids) <= 1:
        return False
    if not _bot_page_urls():
        return False
    if _as_url_is_bot_page(url):
        return False
    where = _page_key(url) or url[:60] or "—"
    logger.info(f"[BrowserActions] Экземпляров «{_as_app_name()}» {len(pids)}, "
                f"а переднее окно ответившего ({where}) не из вкладок пула: "
                f"это личный браузер, мост молчит")
    return True


def _as_single_target(app: Optional[str] = None) -> bool:
    """Мост по ИМЕНИ приложения адресует ИМЕННО браузер бота.

    Один запущенный экземпляр — да, сомнений нет. Несколько (Chrome пула V +
    личный Chrome пользователя, или V + hidden-пул H: имя и bundle id у них
    одни, а выбрать экземпляр Apple Event'ом нельзя — pid понимает только
    System Events, _as_proc_ref) — да ТОЛЬКО если переднее окно, которое
    вернул мост, показывает страницу из реестра вкладок пула
    (_as_url_is_bot_page). Это единственная надёжная проверка «нам ответил
    наш процесс»; не совпало или ответа нет — отказ, как раньше (вызывающий
    падает на CDP-путь, который всегда про браузер бота).

    ЕДИНАЯ точка решения: мосты, возвращающие URL (_front_window_url),
    сверяют свой ответ, остальные (JS во вкладке, тихий выбор вкладки, поиск
    вкладки по URL) спрашивают здесь — им проверка делается тем же чтением
    переднего окна."""
    pids = _as_browser_pids(app)
    if pids is None or len(pids) <= 1:
        return True
    if app and app != _as_app_name():
        # Чужой браузер (перебор имён в тихом выборе вкладки): вкладками пула
        # его экземпляр не подтвердить — остаётся отказ
        logger.info(f"[BrowserActions] Экземпляров «{app}» {len(pids)}, "
                    f"это не браузер бота — мост не трогает их")
        return False
    if not _bot_page_urls():
        return True  # отладочного порта нет — сверять не с чем (см. выше)
    # _front_window_url сам сверяет свой ответ со вкладками пула:
    # непустая строка = событие ушло браузеру бота
    return bool(_front_window_url())


def _as_run(script: str, browser: str = "chrome",
            app: Optional[str] = None) -> str:
    """_osascript + единая обработка сентинела «приложение не запущено»
    (_as_tell(guard=True))."""
    out = _osascript(script, browser=browser)
    if out == AS_NO_APP:
        raise BrowserUnavailable(f"{app or _as_app_name()} не запущен")
    return out


def _osascript(script: str, browser: str = "chrome") -> str:
    """Прогон AppleScript через временный файл → stdout. Ошибки —
    BrowserUnavailable с подсказкой по настройке (browser='safari' —
    подсказки для Safari-диалекта)."""
    tmp = None
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".scpt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        r = subprocess.run(["osascript", tmp], capture_output=True,
                           text=True, timeout=15)
    except FileNotFoundError:
        raise BrowserUnavailable("osascript недоступен (не macOS?)")
    except subprocess.TimeoutExpired:
        raise BrowserUnavailable("osascript не ответил (таймаут)")
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        if "JavaScript" in err and ("отключено" in err or "turned off" in err.lower()
                                    or "Apple" in err or "allow" in err.lower()):
            if browser == "safari":
                raise BrowserUnavailable(
                    "в Safari выключен JavaScript из событий Apple: включи "
                    "«Develop → Allow JavaScript from Apple Events»")
            raise BrowserUnavailable(
                "в Chrome выключен JavaScript из событий Apple: включи "
                "«Вид → Разработчикам → Разрешить JavaScript из событий Apple»")
        if "-1743" in err or "автоматизац" in err.lower() or "not authorized" in err.lower():
            raise BrowserUnavailable(
                f"нет разрешения на автоматизацию: разреши управление "
                f"{'Safari' if browser == 'safari' else 'Chrome'} в "
                "System Settings → Privacy & Security → Automation")
        raise BrowserUnavailable(
            f"{'Safari' if browser == 'safari' else 'Chrome'}/AppleScript: {err[:200]}")
    return (r.stdout or "").strip()


def _open_tab_applescript(url: str, focus: bool = False) -> int:
    """macOS: новая вкладка Chrome с url → её стабильный AppleScript-id.
    id переживает переходы страницы и не зависит от порядка окон/вкладок —
    в отличие от поиска по подстроке URL, который путается в старых вкладках
    того же сайта. focus=True — окно всплывает (команда «открой сайт»)."""
    safe = _as_lit(url)
    # guard=False: приложение можно и запустить — это команда «открой сайт»
    script = _as_tell(
        "  if (count of windows) = 0 then make new window\n"
        "  set w to front window\n"
        f'  tell w to make new tab with properties {{URL:"{safe}"}}\n'
        + ("  activate\n" if focus else "") +
        "  return id of active tab of w\n", guard=False)
    out = _osascript(script)
    try:
        return int(out)
    except ValueError:
        raise BrowserUnavailable(f"не удалось открыть вкладку: {out[:120] or 'пустой ответ Chrome'}")


def _find_tab_applescript(host_part: str) -> Optional[int]:
    """macOS: id ПОСЛЕДНЕЙ вкладки, чей URL содержит host_part (сравнение
    текстом — `t's id is <int>` у Chrome молча не матчится). None — такой
    вкладки нет / Chrome не ответил."""
    if sys.platform != "darwin" or not host_part:
        return None
    if not _as_single_target():
        return None  # несколько экземпляров браузера — адресовать нечем
    safe = _as_lit(host_part)
    script = _as_tell(
        "  set found to missing value\n"
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      try\n"
        "        with timeout of 5 seconds\n"
        f'          if (t\'s URL) contains "{safe}" then set found to (t\'s id) as text\n'
        "        end timeout\n"
        "      end try\n"
        "    end repeat\n"
        "  end repeat\n"
        "  if found is not missing value then return found\n")
    try:
        return int(_as_run(script))
    except (BrowserUnavailable, TypeError, ValueError):
        return None


# AppleScript-имена Chromium-браузеров для тихого выбора вкладки; первым
# пробуем канал из конфига — обычно это и есть браузер бота
_QUIET_TAB_APPS = {
    "chrome": "Google Chrome", "edge": "Microsoft Edge", "opera": "Opera",
    "yandex": "Yandex", "brave": "Brave Browser", "vivaldi": "Vivaldi",
}


def _as_app_order() -> List[str]:
    """Имена Chromium-браузеров для перебора: сначала браузер бота
    (_as_app_name — конфиг executable/channel), потом остальные. Один
    порядок для тихого выбора вкладки и для подъёма окна."""
    ordered = [_as_app_name()]
    ordered += [n for n in _QUIET_TAB_APPS.values() if n not in ordered]
    return ordered


def _quiet_tab_select_script(app: str, url: str, host: str,
                             raise_window: bool = False) -> str:
    safe = _as_lit(url)
    safe_h = _as_lit(host or "")
    # raise_window=True — команда «открой сайт» от пользователя: вкладка
    # активируется и окно браузера всплывает поверх текущего (activate);
    # захваченный фокус НЕ возвращаем — переключение и есть цель команды.
    # Проверка «запущено» — тем же способом, что и у остальных мостов
    # (_as_tell(guard=...)), но со своим сентинелом: вызывающий перебирает
    # несколько имён браузеров
    head = (
        _as_tell("", app=app, guard=True, sentinel="__skip__").split("\n")[0]
        + "\n"
        + ("" if raise_window else
           _as_tell(
               "    set prevBundle to bundle identifier of first application "
               "process whose frontmost is true\n"
               "    set prevName to name of first application process whose "
               "frontmost is true\n",
               app=_SYS_EVENTS_APP, guard=False)))
    # Хвост скрипта: quiet — вернуть перехваченный страницей фокус прежнему
    # frontmost-приложению; raise — activate браузера (окно всплывает поверх
    # текущего — переключение и есть цель команды «открой сайт»).
    if raise_window:
        tail = _as_tell_to("activate", app=app) + 'return "ok"\n'
    else:
        tail = (
            # Смена активной вкладки не активирует приложение сама, но
            # страница (чаты/карты с обработчиками фокуса) может перехватить
            # его — тогда браузер вылезет вперёд. Проверяем и возвращаем
            # фокус прежнему frontmost-приложению.
            "delay 0.25\n"
            + _as_tell_to("set nowName to name of first application process "
                          "whose frontmost is true", app=_SYS_EVENTS_APP)
            + "if nowName is not prevName then\n"
            "    try\n"
            "        tell application id prevBundle to activate\n"
            "    on error\n"
            "        try\n"
            "            tell application prevName to activate\n"
            "        end try\n"
            "    end try\n"
            '    return "restored"\n'
            "end if\n"
            'return "ok"\n'
        )
    body = _as_tell(
        f'    set theURL to "{safe}"\n'
        f'    set theHost to "{safe_h}"\n'
        "    set hitWin to 0\n"
        "    set hitTab to 0\n"
        # Проход 1 — точный URL, 2 — contains (лишние query-параметры),
        # 3 — только хост: сайт между делом редиректил (youtube.com →
        # www.youtube.com/?themeRefresh=1) или подменил URL replaceState'ом
        # после domcontentloaded — финальный page.url не совпадает с адресом
        # вкладки в момент выбора, а направление contains может быть обратным
        "    repeat with passNum from 1 to 3\n"
        "        set wIdx to 0\n"
        "        repeat with w in windows\n"
        "            set wIdx to wIdx + 1\n"
        "            set i to 1\n"
        "            repeat with t in tabs of w\n"
        "                try\n"
        "                    if passNum is 1 then\n"
        "                        if (t's URL) is theURL then\n"
        "                            if hitWin is 0 then set hitWin to wIdx\n"
        "                            if hitWin is wIdx then set hitTab to i\n"
        "                        end if\n"
        "                    else if passNum is 2 then\n"
        "                        if (t's URL) contains theURL then\n"
        "                            if hitWin is 0 then set hitWin to wIdx\n"
        "                            if hitWin is wIdx then set hitTab to i\n"
        "                        end if\n"
        "                    else\n"
        "                        if (t's URL) contains theHost then\n"
        "                            if hitWin is 0 then set hitWin to wIdx\n"
        "                            if hitWin is wIdx then set hitTab to i\n"
        "                        end if\n"
        "                    end if\n"
        "                end try\n"
        "                set i to i + 1\n"
        "            end repeat\n"
        "        end repeat\n"
        "        if hitTab > 0 then exit repeat\n"
        "    end repeat\n"
        "    if hitTab is 0 then return \"__nomatch__\"\n"
        "    set active tab index of window hitWin to hitTab\n",
        app=app, guard=False)
    return head + body + tail


def _select_browser_tab_quietly(url: str) -> bool:
    """macOS: сделать вкладку с этим URL активной в её окне, НЕ поднимая окно
    браузера поверх окон пользователя (set active tab index без activate;
    перехваченный страницей фокус сразу возвращается прежнему приложению).
    Совпадение: точный URL → contains (лишние query-параметры) → хост (сайт
    редиректил/подменил URL replaceState'ом — youtube.com →
    www.youtube.com/?themeRefresh=1); окно — первое от переднего с
    совпадением, вкладка в нём — последняя подходящая (новые — правее).
    → True, если вкладка выбрана. Первый запущенный Chromium решает: nomatch
    у него — другие не пробуем (это вкладка чужого браузера, трогать нельзя)."""
    if sys.platform != "darwin" or not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        host = ""
    for app in _as_app_order():
        if not _as_single_target(app):
            return False  # экземпляров несколько — можно попасть в личный браузер
        try:
            out = _osascript(_quiet_tab_select_script(app, url, host))
        except BrowserUnavailable as e:
            logger.debug(f"[BrowserActions] Тихий выбор вкладки, {app}: {e}")
            return False  # нет разрешения/приложение недоступно — другие имена не спасут
        if out in ("ok", "restored"):
            if out == "restored":
                logger.info("[BrowserActions] Страница перехватила фокус — "
                            "вернул прежнему приложению")
            return True
        if out == "__nomatch__":
            return False
        # __skip__: это приложение не запущено — пробуем следующего кандидата
    return False


def _focus_browser_tab(url: str) -> bool:
    """macOS: переключить ПОЛЬЗОВАТЕЛЯ на вкладку с этим URL — вкладка
    активируется и окно браузера всплывает поверх текущего (activate).
    Команда «открой сайт» — пользователь просил страницу, переключение и есть
    смысл; обратный возврат фокуса не делаем. Совпадение — то же, что у
    _select_browser_tab_quietly (точный URL → contains → хост)."""
    if sys.platform != "darwin" or not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        host = ""
    for app in _as_app_order():
        if not _as_single_target(app):
            return False  # экземпляров несколько — можно попасть в личный браузер
        try:
            out = _osascript(_quiet_tab_select_script(app, url, host,
                                                      raise_window=True))
        except BrowserUnavailable as e:
            logger.debug(f"[BrowserActions] Переключение на вкладку, {app}: {e}")
            return False
        if out == "ok":
            return True
        if out == "__nomatch__":
            return False
    return False


def _run_apple_events(host_part: Optional[str], js: str,
                      scan_search: bool = False,
                      tab_id: Optional[int] = None) -> str:
    """JS во вкладке Chromium-браузера (имя приложения — из конфига, см.
    _as_app_name): по tab_id (точно, для навигации), иначе чей URL содержит
    host_part; при None — в активной вкладке переднего окна.
    scan_search=True (рецепты поисковой выдачи): при неудаче на активной —
    фоновый поиск вкладок со страницей поиска (команда может быть дана из
    чата — активная вкладка тогда сам чат).
    Экземпляров браузера больше одного (Chrome бота + личный Chrome) —
    честный отказ: Apple Event уйдёт в произвольный (_as_single_target).
    Ошибки — BrowserUnavailable с подсказкой по настройке."""
    if not _as_single_target():
        raise BrowserUnavailable(
            f"запущено несколько экземпляров «{_as_app_name()}» — "
            "AppleScript не выбирает экземпляр; закрой лишний браузер "
            "или включи CDP (browser.launch)")
    esc = _as_lit(js)
    if tab_id is not None:
        # Точная вкладка по стабильному id (многошаговая навигация).
        # Сравнение текстом: `t's id is <int>` у Chrome молча не матчится
        script = _as_tell(
            "  repeat with w in windows\n"
            "    repeat with t in tabs of w\n"
            f"      if (t's id as text) is \"{int(tab_id)}\" then\n"
            "        try\n"
            "          with timeout of 5 seconds\n"
            f'            return execute t javascript "{esc}"\n'
            "          end timeout\n"
            "        on error errMsg number errNum\n"
            '          if errNum is 12 or errMsg contains "JavaScript" then return "__js_disabled__"\n'
            "        end try\n"
            "      end if\n"
            "    end repeat\n"
            "  end repeat\n") + 'return "__no_tab__"\n'
    elif host_part is None and not scan_search:
        # Активная вкладка переднего окна. Если это наш чат — НЕ ищем другую
        # вкладку сами: клик по первой попавшейся странице (чужой сайт) хуже,
        # чем честный отказ; распознаёт чат и просит назвать сайт resolve_click
        script = _as_tell(
            "  with timeout of 5 seconds\n"
            f'    return execute (active tab of front window) javascript "{esc}"\n'
            "  end timeout\n")
    elif host_part is None:
        # Рецепт «здесь и сейчас»: сначала активная вкладка (пользователь может
        # смотреть на выдачу), затем — фоновый поиск среди вкладок со СТРАНИЦЕЙ
        # ПОИСКА (когда команда дана из чата, активная вкладка — сам чат).
        # JS сам проверяет сайт/страницу и отвечает «ok:…» только при клике.
        # err 12 («JavaScript через AppleScript отключено») пробрасываем
        # сентинелом — иначе try-глушилка выдаёт его за «нет вкладки»
        script = _as_tell(
            "  try\n"
            "    with timeout of 5 seconds\n"
            f'      set r to execute (active tab of front window) javascript "{esc}"\n'
            '      if r starts with "ok:" then return r\n'
            "    end timeout\n"
            "  on error errMsg number errNum\n"
            '    if errNum is 12 or errMsg contains "JavaScript" then return "__js_disabled__"\n'
            "  end try\n"
            "  repeat with w in windows\n"
            "    repeat with t in tabs of w\n"
            "      try\n"
            "        with timeout of 5 seconds\n"
            "          set u to t's URL\n"
            '          if u contains "youtube.com/results" or u contains "kp_query" or u contains "/search?" then\n'
            f'            set r to execute t javascript "{esc}"\n'
            '            if r starts with "ok:" then return r\n'
            "          end if\n"
            "        end timeout\n"
            "      on error errMsg number errNum\n"
            '        if errNum is 12 or errMsg contains "JavaScript" then return "__js_disabled__"\n'
            "      end try\n"
            "    end repeat\n"
            "  end repeat\n") + 'return "__no_tab__"\n'
    else:
        # Обход вкладок — под try + timeout на КАЖДОЙ: одна повисшая/модальная
        # вкладка иначе вешает всю AppleEvent-очередь Chrome (-1712).
        # Берём ПОСЛЕДНЮЮ подходящую вкладку: свежеоткрытая правее всех, —
        # навигация должна вести свежую, а не залипшую старую того же сайта.
        # Полный URL с одноразовым query (OAuth state/nonce) устаревает за
        # миллисекунды — второй проход по origin (scheme://host), кейс 10.09
        _origin = _origin_of(host_part) or ""
        # host_part/_origin — раньше вставлялись в `contains "…"` совсем без
        # экранирования (в отличие от соседних сборщиков этого же файла,
        # где та же операция всегда шла через safe/safe_h): страница/URL с
        # кавычкой или бэкслешем в хосте ломала бы AppleScript-скрипт целиком
        safe_hp = _as_lit(host_part)
        safe_or = _as_lit(_origin)
        script = _as_tell(
            "  set found to missing value\n"
            "  repeat with w in windows\n"
            "    repeat with t in tabs of w\n"
            "      try\n"
            "        with timeout of 5 seconds\n"
            f'          if (t\'s URL) contains "{safe_hp}" then set found to t\n'
            "        end timeout\n"
            "      end try\n"
            "    end repeat\n"
            "  end repeat\n"
            + ("" if not _origin else
               "  if found is missing value then\n"
               "    repeat with w in windows\n"
               "      repeat with t in tabs of w\n"
               "        try\n"
               "          with timeout of 5 seconds\n"
               f'            if (t\'s URL) contains "{safe_or}" then set found to t\n'
               "          end timeout\n"
               "        end try\n"
               "      end repeat\n"
               "    end repeat\n"
               "  end if\n")
            + "  if found is not missing value then\n"
            "    try\n"
            "      with timeout of 5 seconds\n"
            f'        return execute found javascript "{esc}"\n'
            "      end timeout\n"
            "    on error errMsg number errNum\n"
            '      if errNum is 12 or errMsg contains "JavaScript" then return "__js_disabled__"\n'
            "    end try\n"
            "  end if\n") + 'return "__no_tab__"\n'
    out = _as_run(script)
    if out == "__js_disabled__":
        raise BrowserUnavailable(
            "в Chrome выключен JavaScript из событий Apple: включи "
            "«Вид → Разработчикам → Разрешить JavaScript из событий Apple»")
    if out == "__no_tab__":
        # host_part=None — рецепты выдачи (search_first/search_pick): вкладку
        # с результатами поиска не нашли; иначе — вкладку конкретного сайта
        raise BrowserUnavailable(
            "нет открытой вкладки с результатами поиска" if host_part is None
            else f"нет открытой вкладки {host_part}")
    return out


# ── Safari-бэкенд (macOS): do JavaScript через Apple Events ──
# У Safari нет CDP и стабильных id вкладок: вкладки адресуем по URL
# (реестр tab_id → последний известный URL ведём сами). Доверенный Enter
# (отправка в веб-чатах) — System Events keystroke: вкладка на мгновение
# выходит на передний план — цена отсутствия Input-домена.
# Требует от пользователя: Develop → «Allow JavaScript from Apple Events»
# + согласие на автоматизацию (TCC). Запускать Safari с другим профилем
# нельзя — работаем в браузере пользователя, процесс его никогда не убиваем.

_SAFARI_TABS: Dict[int, str] = {}   # tab_id → последний известный URL
_SAFARI_LOCK = threading.Lock()
_SAFARI_NEXT_ID = 500_000           # не пересекается с Chrome AS-id и CDP-реестрами


def _safari_present() -> bool:
    return sys.platform == "darwin" and os.path.isdir("/Applications/Safari.app")


def _chrome_present() -> bool:
    """Chrome установлен (нужен AppleScript-фолбэку auto-режима на macOS)."""
    return sys.platform != "darwin" or os.path.isdir(
        "/Applications/Google Chrome.app")


def _safari_exec(host_part: Optional[str], js: str,
                 tab_id: Optional[int] = None) -> str:
    """JS во вкладке Safari: tab_id (URL из реестра) → по подстроке URL →
    передний документ. «missing value» (JS вернул undefined) — пустая строка."""
    esc = _as_lit(js)
    if tab_id is not None:
        with _SAFARI_LOCK:
            host_part = _SAFARI_TABS.get(tab_id) or host_part
    if host_part:
        safe = _as_lit(host_part)
        script = _as_tell(
            "  repeat with w in windows\n"
            "    repeat with t in tabs of w\n"
            "      try\n"
            f'        if (URL of t) contains "{safe}" then return do JavaScript "{esc}" in t\n'
            "      end try\n"
            "    end repeat\n"
            "  end repeat\n", app=_SAFARI_APP) + 'return "__no_tab__"\n'
    else:
        script = _as_tell(
            '  if (count of documents) = 0 then return "__no_tab__"\n'
            f'  return do JavaScript "{esc}" in front document\n',
            app=_SAFARI_APP)
    out = _as_run(script, browser="safari", app=_SAFARI_APP)
    if out == "__no_tab__":
        raise BrowserUnavailable(
            "нет открытой вкладки Safari" + (f" {host_part}" if host_part else ""))
    return "" if out == "missing value" else out


def _run_safari_events(host_part: Optional[str], js: str,
                       scan_search: bool = False,
                       tab_id: Optional[int] = None) -> str:
    """Аналог _run_apple_events для Safari. scan_search: обход всех вкладок,
    первый ответ «ok:…» (рецепты поисковой выдачи)."""
    if scan_search and host_part is None and tab_id is None:
        esc = _as_lit(js)
        script = _as_tell(
            "  repeat with w in windows\n"
            "    repeat with t in tabs of w\n"
            "      try\n"
            f'        set r to do JavaScript "{esc}" in t\n'
            '        if r starts with "ok:" then return r\n'
            "      end try\n"
            "    end repeat\n"
            "  end repeat\n", app=_SAFARI_APP) + 'return "__no_tab__"\n'
        out = _as_run(script, browser="safari", app=_SAFARI_APP)
        if out == "__no_tab__":
            raise BrowserUnavailable("нет открытой вкладки с результатами поиска")
        return out
    return _safari_exec(host_part, js, tab_id)


def _safari_open_tab(url: str, focus: bool = False) -> int:
    """Новая вкладка Safari → наш tab_id (реестр по URL; стабильных id у
    вкладок Safari нет). focus=True — Safari выходит на передний план
    (команда «открой сайт» от пользователя)."""
    global _SAFARI_NEXT_ID
    safe = _as_lit(url)
    script = _as_tell(
        "  if (count of windows) = 0 then make new document\n"
        f'  tell front window to make new tab with properties {{URL:"{safe}"}}\n'
        + ("  activate\n" if focus else ""),
        app=_SAFARI_APP, guard=False) + 'return "ok"\n'
    _osascript(script, browser="safari")
    with _SAFARI_LOCK:
        tid = _SAFARI_NEXT_ID
        _SAFARI_NEXT_ID += 1
        _SAFARI_TABS[tid] = url
    return tid


def _safari_tab_url(tab_id: Optional[int], host_part: Optional[str]) -> str:
    """Текущий URL вкладки; обновляет реестр (после первого сообщения чат
    получает постоянный адрес)."""
    url = _safari_exec(host_part, "location.href", tab_id).strip()
    if tab_id is not None and url:
        with _SAFARI_LOCK:
            _SAFARI_TABS[tab_id] = url
    return url


def _safari_navigate(tab_id: Optional[int], host_part: Optional[str], url: str):
    """Навигация уже открытой вкладки (web_llm: свежий чат — возврат на home)."""
    if tab_id is not None:
        with _SAFARI_LOCK:
            host_part = _SAFARI_TABS.get(tab_id) or host_part
    if not host_part:
        raise BrowserUnavailable("Safari: неизвестная вкладка для навигации")
    safe_h = _as_lit(host_part)
    safe_u = _as_lit(url)
    script = _as_tell(
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      try\n"
        f'        if (URL of t) contains "{safe_h}" then\n'
        f'          set URL of t to "{safe_u}"\n'
        '          return "ok"\n'
        "        end if\n"
        "      end try\n"
        "    end repeat\n"
        "  end repeat\n", app=_SAFARI_APP) + 'return "__no_tab__"\n'
    if _as_run(script, browser="safari", app=_SAFARI_APP) == "__no_tab__":
        raise BrowserUnavailable(f"нет открытой вкладки Safari {host_part}")
    if tab_id is not None:
        with _SAFARI_LOCK:
            _SAFARI_TABS[tab_id] = url


def _safari_focus_tab(tab_id: Optional[int], host_part: Optional[str]):
    """Вкладка Safari на передний план: System Events (keystroke/key code)
    работает только с фокусом ОС. Требует Accessibility-разрешение (TCC)
    вдобавок к Automation."""
    if tab_id is not None:
        with _SAFARI_LOCK:
            host_part = _SAFARI_TABS.get(tab_id) or host_part
    if host_part:
        safe = _as_lit(host_part)
        script = _as_tell(
            "  repeat with w in windows\n"
            "    repeat with t in tabs of w\n"
            "      try\n"
            f'        if (URL of t) contains "{safe}" then\n'
            "          set current tab of w to t\n"
            "          set index of w to 1\n"
            "          activate\n"
            '          return "ok"\n'
            "        end if\n"
            "      end try\n"
            "    end repeat\n"
            "  end repeat\n", app=_SAFARI_APP) + 'return "__no_tab__"\n'
        if _as_run(script, browser="safari",
                   app=_SAFARI_APP) == "__no_tab__":
            raise BrowserUnavailable(
                f"нет открытой вкладки Safari {host_part}")
    else:
        _osascript(_as_tell_to("activate", app=_SAFARI_APP), browser="safari")
    time.sleep(0.3)  # дать фокусу перейти


def _safari_enter(tab_id: Optional[int], host_part: Optional[str]):
    """Доверенный Enter через System Events (как keyboard.press у playwright)."""
    _safari_focus_tab(tab_id, host_part)
    try:
        _osascript(_as_tell_to("keystroke return", app=_SYS_EVENTS_APP),
                   browser="safari")
    except BrowserUnavailable as e:
        raise BrowserUnavailable(
            f"{e} (для Enter нужно ещё Accessibility: System Settings → "
            "Privacy & Security → Accessibility)")


def _safari_escape(tab_id: Optional[int], host_part: Optional[str]):
    """Доверенный Escape (key code 53) через System Events."""
    _safari_focus_tab(tab_id, host_part)
    try:
        _osascript(_as_tell_to("key code 53", app=_SYS_EVENTS_APP),
                   browser="safari")
    except BrowserUnavailable as e:
        raise BrowserUnavailable(
            f"{e} (для Escape нужно ещё Accessibility: System Settings → "
            "Privacy & Security → Accessibility)")


def _safari_chat_fill_send(host_part: Optional[str], tab_id: Optional[int],
                           input_sel: str, text: str) -> str:
    """Ввод+отправка в веб-чате Safari: JS-fill (управляемые редакторы типа
    Lexical могут не принять — честная ошибка) + доверенный Enter через
    System Events (вкладка на мгновение выходит на передний план)."""
    sel = json.dumps(input_sel, ensure_ascii=False)
    if _safari_exec(host_part,
                    _CHAT_FILL_JS % (sel, json.dumps(text, ensure_ascii=False)),
                    tab_id) != "ok":
        raise BrowserUnavailable("поле чата не приняло ввод")
    got = _safari_exec(host_part, _CHAT_FIELD_JS % sel, tab_id)
    if _norm_ws(text[:200]) not in _norm_ws(got):
        raise BrowserUnavailable(
            "поле чата не приняло текст (управляемый редактор без CDP)")
    def _st() -> _Probe:
        try:
            return _probe_of(_safari_exec(host_part, _DOM_STATE_JS, tab_id))
        except BrowserUnavailable:
            return _Probe(False, "", "", "")

    pre = _st()
    _safari_enter(tab_id, host_part)
    deadline = time.time() + SUBMIT_VERIFY_SEC
    while time.time() < deadline:
        try:
            cur = _safari_exec(host_part, _CHAT_FIELD_JS % sel, tab_id)
        except BrowserUnavailable:
            cur = ""
        if not cur.strip():
            return "sent"
        if _effect_verdict(pre, _st()) == EFFECT_CHANGED:
            return "sent"
        time.sleep(0.25)
    raise FillUncertain(
        "Enter нажат, но поле не очистилось и страница не изменилась — "
        "не уверен, что отправилось")


def _safari_wait_input(host_part: Optional[str], tab_id: Optional[int],
                       selector: str, timeout_sec: float) -> bool:
    """Опрос наличия поля ввода (первая загрузка чата рендерится не сразу)."""
    js = ("(function(){var e=document.querySelector("
          + json.dumps(selector) + ");return e?'yes':'no';})()")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            if _safari_exec(host_part, js, tab_id) == "yes":
                return True
        except BrowserUnavailable:
            pass
        time.sleep(0.5)
    return False


def _safari_page_urls() -> List[str]:
    """URL всех вкладок Safari (снимок для детекта попапов)."""
    script = _as_tell(
        '  set out to ""\n'
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      try\n"
        "        set out to out & (URL of t) & linefeed\n"
        "      end try\n"
        "    end repeat\n"
        "  end repeat\n"
        "  return out\n", app=_SAFARI_APP)
    try:
        out = _osascript(script, browser="safari")
    except BrowserUnavailable:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


# ── Общий вход для JS-сниппетов (рецепты, href) ──────────

def _eval_in_tab(host_part: Optional[str], tab_id: Optional[int], js: str,
                 front: bool = False, scan_search: bool = False,
                 timeout_sec: Optional[float] = None,
                 backends: Optional[Tuple[str, ...]] = None) -> str:
    """ЕДИНАЯ точка «выполнить JS во вкладке (host_part, tab_id)»: транспорт
    выбирается здесь и только здесь — сырой CDP для фоновых вкладок веб-
    чатов, иначе playwright-воркер / Safari / AppleScript. Все мосты
    (_run_js, _eval_js_any, читатели чата) идут сюда, чтобы новая операция
    не могла «забыть» raw-ветку: её забыл _eval_js_any, и detect_antibot на
    фоновой вкладке молча отдавал «чисто» вместо капчи.
    timeout_sec — бюджет JS, который сам ждёт в странице (raw-транспорт
    выводит из него потолок ответа CDP, playwright-воркер — бюджет submit).
    backends — чем разрешено исполнять помимо raw (None — чем угодно):
    операциям, которым нужны playwright-примитивы, отказ должен быть ЯВНЫМ.
    front=True — выдёргивать окно на передний план (только явные команды
    пользователя); фоновой вкладке неприменимо."""
    if is_raw_tab(tab_id):
        return _raw_eval(int(tab_id), js, timeout_sec=timeout_sec)
    backend = _select_backend(tab_op=True)
    if backends is not None and backend not in backends:
        raise _no_backend("операция во вкладке", active=backend,
                          need=tuple(backends))
    if backend == "cdp":
        budget = None if timeout_sec is None \
            else float(timeout_sec) + SUBMIT_MARGIN_SEC
        return str(_WORKER.submit(
            lambda w: w.eval_js(host_part, js, scan_search=scan_search,
                                tab_id=tab_id, front=front),
            timeout=budget) or "")
    if backend == "safari":
        return str(_run_safari_events(host_part, js, scan_search, tab_id) or "")
    return str(_run_apple_events(host_part, js, scan_search,
                                 tab_id=tab_id) or "")


def _run_js(host_part: Optional[str], js: str, scan_search: bool = False,
            tab_id: Optional[int] = None, front: bool = False) -> str:
    """Бэкенд по конфигу: CDP (единый на обеих ОС), AppleScript или Safari
    (macOS fallback). front=True — выдёргивать окно браузера на передний
    план; зарезервировано за явными командами пользователя (activate_tab),
    по умолчанию действия бота окно не поднимают."""
    return _eval_in_tab(host_part, tab_id, js, front=front,
                        scan_search=scan_search)


def run_recipe(recipe_id: str) -> str:
    """Выполнить рецепт по id. Часть после двоеточия — числовой аргумент
    («search_pick:3» → третий результат выдачи; без аргумента — первый).
    Неудачи — BrowserUnavailable с человеческим текстом."""
    arg = None
    if ":" in recipe_id:
        recipe_id, arg = recipe_id.split(":", 1)
    entry = RECIPES.get(recipe_id)
    if entry is None:
        raise BrowserUnavailable(f"неизвестный рецепт «{recipe_id}»")
    host, js = entry
    if "{N}" in js:
        n = 1
        if arg is not None:
            if not arg.isdigit() or not 1 <= int(arg) <= 20:
                raise BrowserUnavailable(f"некорректный номер результата: «{arg}»")
            n = int(arg)
        js = js.replace("{N}", str(n))
    elif arg is not None:
        raise BrowserUnavailable(f"рецепт «{recipe_id}» не принимает аргумент")
    out = _run_js(host, js, scan_search=(host is None))
    logger.info(f"[BrowserActions] Рецепт «{recipe_id}» → {out[:60]}")
    if not out.startswith("ok:"):
        raise BrowserUnavailable(out or "рецепт не сработал")
    return out[3:]


# ── Готовность страницы и состояние DOM (п.2 и п.6) ──────

# Отпечаток страницы для closed-loop: ОГРАНИЧЕННОЙ стоимости, без
# сериализации разметки. Сериализация innerHTML (и рекурсивная — всех
# shadowRoot) стоила на тяжёлой странице больше самого действия, тормозила
# страницу пользователя на каждом опросе (раз в 250 мс) и на глубоком DOM
# падала с RangeError — а падение замера closed-loop засчитывал как
# «страница изменилась».
# Что входит в отпечаток и зачем:
#   • токен документа window.__vpcDoc — переживает перерисовку, но НЕ смену
#     документа: надёжный признак навигации/перезагрузки (см. _effect_verdict);
#   • href + readyState — навигация и стадия загрузки;
#   • title, общее число элементов, число детей body — структурные правки;
#   • счётчики состояний контролов: aria-expanded/checked/pressed/selected,
#     :checked, details/dialog[open], aria-current — «меню раскрылось»,
#     «переключатель щёлкнул», «пункт выбран» без единого нового узла;
#   • число диалогов/меню/листбоксов/тултипов/алертов — «появился попап»;
#   • активный элемент (+ длина его value) — фокус переехал в поле;
#   • позиция прокрутки — клик увёл к якорю;
#   • 5 hit-тестов вьюпорта (центр и крест) — поверх контента лёг оверлей;
#   • ограниченный обход DOM (до 1500 узлов, глубина до 20, shadowRoot
#     внутри того же бюджета): тег, ключевые атрибуты, текст листьев —
#     «текст сменился», «класс стал open». Лимиты дают и защиту от
#     RangeError, и постоянную цену замера на любой странице.
_DOM_STATE_JS = (
    "(function(){try{"
    "var d=document,h=2166136261;"
    # FNV-1a, 32 бита; строки режем до 160 символов — цена замера не должна
    # зависеть от длины текста узла
    "function mix(s){s=''+s;var n=s.length>160?160:s.length;"
    "for(var i=0;i<n;i++){h^=s.charCodeAt(i);"
    "h=(h+((h<<1)+(h<<4)+(h<<7)+(h<<8)+(h<<24)))>>>0;}h=(h^n)>>>0;}"
    "function cnt(q){try{return d.querySelectorAll(q).length;}"
    "catch(x){return -1;}}"
    "if(!window.__vpcDoc){window.__vpcDoc='d'+Math.random().toString(36)"
    ".slice(2);}"
    "mix(d.title);"
    "mix(d.getElementsByTagName('*').length);"
    "mix(d.body?d.body.childElementCount:0);"
    "mix(cnt('[aria-expanded=\"true\"]'));"
    "mix(cnt('[aria-checked=\"true\"],[aria-pressed=\"true\","
    "[aria-selected=\"true\"]'));"
    "mix(cnt(':checked'));"
    "mix(cnt('details[open],dialog[open]'));"
    "mix(cnt('[role=dialog],[role=alertdialog],[aria-modal=\"true\"],"
    "[role=menu],[role=listbox],[role=tooltip],[role=alert]'));"
    "mix(cnt('[aria-current]'));"
    "var a=d.activeElement;"
    "mix(a?(a.tagName+'#'+(a.id||'')+'.'"
    "+((a.className||'')+'').slice(0,40)):'-');"
    "try{mix(a&&a.value!=null?(''+a.value).length:-1);}catch(x1){}"
    "mix(Math.round(window.scrollY||0)+','+Math.round(window.scrollX||0));"
    "try{var pt=[[0.5,0.5],[0.5,0.15],[0.5,0.85],[0.15,0.5],[0.85,0.5]];"
    "for(var p=0;p<pt.length;p++){var t=d.elementFromPoint("
    "Math.round(innerWidth*pt[p][0]),Math.round(innerHeight*pt[p][1]));"
    "mix(t?(t.tagName+'#'+(t.id||'')+'.'"
    "+((t.className||'')+'').slice(0,40)):'-');}}catch(x2){}"
    "var N=1500,D=20;"
    "function walk(n,dep){if(N<=0||dep>D||!n)return;"
    "var c=n.children;if(!c)return;"
    "for(var i=0;i<c.length&&N>0;i++){var e=c[i];N--;"
    "mix(e.tagName);var at=e.attributes;"
    "if(at){mix(at.length);"
    "for(var j=0;j<at.length&&j<14;j++){var an=at[j].name;"
    # style пропускаем: анимации/transition дёргают его каждый кадр и любой
    # замер выглядел бы как «страница изменилась»
    "if(an==='style')continue;"
    "if(an==='id'||an==='class'||an==='role'||an==='value'||an==='open'"
    "||an==='checked'||an==='hidden'||an==='disabled'||an==='selected'"
    "||an==='href'||an==='src'||an.indexOf('aria-')===0){"
    "mix(an);mix(at[j].value);}}}"
    "if(!e.firstElementChild){mix(e.textContent);}"
    "if(e.shadowRoot)walk(e.shadowRoot,dep+1);"
    "walk(e,dep+1);}}"
    # Обходим body, а не documentElement: head (скрипты/стили) съел бы бюджет
    "walk(d.body||d.documentElement,0);"
    "return window.__vpcDoc+'|'+location.href+'|'+d.readyState+'|'+(h>>>0);"
    "}catch(e2){return '';}})()"
)

# Трёхзначный результат замера эффекта: изменилось / не изменилось /
# замер не удался. «Замер не удался» никогда не равен «сработало» —
# раньше любая ошибка evaluate отдавалась уникальным сентинелом и первый же
# опрос признавал клик успешным (при систематическом сбое замера —
# всегда).
EFFECT_CHANGED = "changed"
EFFECT_SAME = "same"
EFFECT_FAILED = "failed"


class _Probe(NamedTuple):
    """Один замер состояния страницы/фрейма.
    ok — замер удался; url/aux снимаются БЕЗ JS (переживают неработающий
    evaluate и потому годятся в положительный признак навигации);
    doc — токен документа, fp — отпечаток."""

    ok: bool
    url: str
    doc: str
    fp: str
    aux: str = ""


def _probe_of(raw: str, url: str = "", aux: str = "") -> _Probe:
    """Разбор ответа _DOM_STATE_JS («док|href|readyState|хэш»); пустой/битый
    ответ — замер не удался (ok=False), а не «новое состояние»."""
    doc, _sep, rest = str(raw or "").partition("|")
    if not doc or not rest:
        return _Probe(False, url, "", "", aux)
    return _Probe(True, url, doc, rest, aux)


def _page_state(scope, aux: str = "") -> _Probe:
    """Замер страницы/фрейма через evaluate. Падение evaluate — это
    «замер не удался», а НЕ признак изменения: URL берём отдельно (playwright
    отдаёт его без JS), и только он с токеном документа решают, была ли
    навигация."""
    url = ""
    try:
        url = str(getattr(scope, "url", "") or "")
    except Exception:
        url = ""
    try:
        raw = str(scope.evaluate(_DOM_STATE_JS) or "")
    except Exception:
        return _Probe(False, url, "", "", aux)
    return _probe_of(raw, url, aux)


def _effect_verdict(pre: _Probe, cur: _Probe) -> str:
    """Сравнение двух замеров → EFFECT_CHANGED/SAME/FAILED. Навигация —
    отдельный положительный признак по надёжным приметам (URL, токен
    документа, внешний aux вроде числа вкладок), а не «любое исключение»."""
    if pre.aux != cur.aux:
        return EFFECT_CHANGED
    if pre.url and cur.url and pre.url != cur.url:
        return EFFECT_CHANGED
    if not (pre.ok and cur.ok):
        return EFFECT_FAILED
    if pre.doc != cur.doc:
        return EFFECT_CHANGED   # документ заменён: переход/перезагрузка
    return EFFECT_CHANGED if pre.fp != cur.fp else EFFECT_SAME


def _wait_effect(state_fn, pre: _Probe, timeout: Optional[float] = None,
                 interval: float = 0.3) -> str:
    """Общая closed-loop проверка эффекта действия — одна на клик,
    наведение, ввод и клик по координатам. Ждём изменения до timeout
    (None — общий бюджет CLICK_VERIFY_SEC).
    → EFFECT_CHANGED; EFFECT_SAME (замеры шли, изменений нет);
    EFFECT_FAILED (ни один замер не удался — эффект неизвестен)."""
    budget = CLICK_VERIFY_SEC if timeout is None else float(timeout)
    interval = min(interval, max(0.02, budget / 4))
    deadline = time.monotonic() + budget
    measured = False
    while True:
        time.sleep(interval)
        verdict = _effect_verdict(pre, state_fn())
        if verdict == EFFECT_CHANGED:
            return EFFECT_CHANGED
        if verdict == EFFECT_SAME:
            measured = True
        if time.monotonic() >= deadline:
            return EFFECT_SAME if measured else EFFECT_FAILED


def _uncertain(verdict: str, sent_ru: str) -> ClickUncertain:
    """Честный текст «не уверен»: «эффекта не видно» и «проверить не удалось»
    — разные причины, и пользователю они говорят разное."""
    tail = ("но проверить эффект не удалось: страница не отвечает на замер"
            if verdict == EFFECT_FAILED else "но страница не изменилась")
    return ClickUncertain(f"{sent_ru}, {tail} — не уверен, что сработало")


def _verify_effect(state_fn, pre: _Probe, sent_ru: str,
                   timeout: Optional[float] = None) -> str:
    """_wait_effect + честный ClickUncertain, когда эффекта не видно или
    замер не удался. Единая точка для исполнителей без своих фолбэков."""
    verdict = _wait_effect(state_fn, pre, timeout)
    if verdict != EFFECT_CHANGED:
        raise _uncertain(verdict, sent_ru)
    return verdict


# evaluate(js, arg): playwright передаёт аргумент, ТОЛЬКО если выражение
# вычисляется в функцию. У IIFE (`(function(a){…})()`) выражение — уже
# результат вызова, аргумент молча теряется (у hover-проверки селектор
# приезжал undefined и ветка «навёлся» была недостижима). _js_takes_arg —
# признак «шаблон примет аргумент», им же проверяется весь файл в тесте.
_JS_IIFE_TAIL_RE = re.compile(r"\}\s*\)\s*\([^()]*\)\s*;?\s*$")


def _js_takes_arg(js: str) -> bool:
    """True — выражение вычисляется в ФУНКЦИЮ и получит arg из evaluate."""
    s = str(js or "").strip()
    if _JS_IIFE_TAIL_RE.search(s):
        return False
    head = s.split("=>", 1)[0]
    if "=>" in s and len(head) <= 120 and "{" not in head:
        return True
    return s.startswith("function") or s.startswith("(function")


def _eval_arg(scope, fn_js: str, arg):
    """evaluate с аргументом — только через этот помощник: шаблон обязан
    быть функцией, иначе аргумент потерялся бы молча."""
    if not _js_takes_arg(fn_js):
        raise BrowserUnavailable(
            "JS-шаблон не принимает аргумент (нужна функция, не IIFE)")
    return scope.evaluate(fn_js, arg)


def wait_page_ready(page, timeout_sec: float = READY_TIMEOUT_SEC):
    """Ждём готовности перед снапшотом: load → networkidle (best effort) →
    стабильность DOM (DOM_STABLE_POLLS одинаковых опроса хэша подряд с шагом
    DOM_POLL_MS — SPA дорисовывается после load). Общий бюджет timeout_sec:
    по его исчерпании работаем с тем, что есть (бота не блокируем)."""
    deadline = time.monotonic() + timeout_sec
    try:
        page.wait_for_load_state(
            "load", timeout=max(1, int((deadline - time.monotonic()) * 1000)))
    except Exception:
        pass
    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=max(1, min(NETWORKIDLE_BUDGET_MS,
                               int((deadline - time.monotonic()) * 1000))))
    except Exception:
        pass
    prev: Optional[_Probe] = None
    stable = 0
    while time.monotonic() < deadline and stable < DOM_STABLE_POLLS:
        cur = _page_state(page)
        # Неудавшийся замер стабильностью не считается: он ничего не говорит
        # о странице (и раньше выглядел вечно «новым» состоянием)
        if cur.ok and prev is not None and cur == prev:
            stable += 1
        else:
            prev = cur if cur.ok else None
            stable = 0
        page.wait_for_timeout(DOM_POLL_MS)


def _eval_js_any(host_part: Optional[str], tab_id: Optional[int],
                 js: str) -> str:
    """JS во вкладке на любом транспорте, БЕЗ выдёргивания на передний план
    (фоновые проверки перед снапшотом) — тонкая обёртка над _eval_in_tab."""
    return _eval_in_tab(host_part, tab_id, js, front=False)


# Авто-закрытие оверлеев-блокеров (куки-баннеры, подписка на рассылку,
# geo-попап) перед снапшотом: они перекрывают контент, съедают бюджет
# снапшота и ловят клик вместо цели. Консервативно: кликаем ТОЛЬКО контрол
# внутри видимого «всплывающего» контейнера (role=dialog/aria-modal или
# fixed/absolute + класс/id вида cookie/consent/popup/modal/…), чей текст
# ЦЕЛИКОМ — типовое согласие/отказ/закрытие; случайный «ОК» в контенте
# страницы не трогаем. Не больше одного клика за вызов (следующий оверлей —
# следующим снапшотом). Контейнеры перебираем от конца DOM: порталы
# рендерятся последними и лежат поверх.
_DISMISS_OVERLAY_JS = (
    "(function(){"
    "function vis(e){var s=getComputedStyle(e);"
    "return s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';}"
    "function fixedish(e){var p=e,d=0;"
    "while(p&&p.tagName!=='BODY'&&d<10){var s=getComputedStyle(p).position;"
    "if(s==='fixed'||s==='absolute')return true;p=p.parentElement;d++;}"
    "return false;}"
    "var ac=/^(принять|принимаю|принять все|принять всё|согласен|согласна|"
    "соглашаюсь|ок|ok|okay|accept|accept all|agree|i agree|понятно|хорошо|"
    "разрешаю|разрешить|allow|allow all|отклонить|отклонить все|отклоняю|"
    "reject|reject all|decline|got it)[!.…]*$/i;"
    # Отмашки («закрыть», «позже», крестик) — только внутри ЯВНЫХ блокеров
    # (класс/id с cookie|consent|gdpr|newsletter|subscribe|banner). На
    # контентной модалке (карточка товара dodo — тоже role=dialog с
    # aria-label «Закрыть» на крестике) такой клик закрыл бы страницу,
    # которую пользователь открыл, — регрессия «просил добавку, выкинуло
    # на главную»
    "var dis=/^(закрыть|not now|позже|не сейчас|нет,? спасибо|спасибо,? нет|"
    "пропустить|skip)[!.…]*$/i;"
    "var boxes=document.querySelectorAll('[role=dialog],[aria-modal=true],"
    "[class*=cookie],[class*=Cookie],[class*=consent],[class*=Consent],"
    "[id*=cookie],[id*=consent],[class*=gdpr],[class*=popup],[class*=Popup],"
    "[class*=modal],[class*=Modal],[class*=overlay],[class*=banner],"
    "[class*=newsletter],[class*=subscribe]');"
    "for(var i=boxes.length-1;i>=0;i--){var box=boxes[i];"
    "var r=box.getBoundingClientRect();"
    "if(r.width<40||r.height<30)continue;"
    "if(!vis(box))continue;"
    "var blocker=/cookie|consent|gdpr|newsletter|subscribe|banner/i.test("
    "(box.getAttribute('class')||'')+' '+(box.getAttribute('id')||''));"
    "var modal=box.getAttribute('role')==='dialog'||box.hasAttribute('aria-modal');"
    "if(!blocker&&!modal&&!fixedish(box))continue;"
    "var bs=box.querySelectorAll('button,a,[role=button],"
    "input[type=button],input[type=submit]');"
    # Утвердительное согласие — на любом модале; отмашка — только на блокере
    "var hit=null,ds=null;"
    "for(var j=0;j<bs.length;j++){var b=bs[j];"
    "var t=((b.innerText||b.value||'')+'').replace(/\\s+/g,' ').trim();"
    "if(!t)t=(b.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim();"
    "if(!t||t.length>40)continue;"
    "var br=b.getBoundingClientRect();"
    "if(br.width<2||br.height<2||!vis(b))continue;"
    "if(ac.test(t)){hit={e:b,t:t};break;}"
    "if(!ds&&dis.test(t))ds={e:b,t:t};}"
    "if(!hit&&blocker)hit=ds;"
    "if(!hit&&blocker){"
    "var cs=box.querySelectorAll('[class*=close],[class*=Close],"
    "[aria-label*=закры i],[aria-label*=close i],[aria-label*=dismiss i]');"
    "for(var k=0;k<cs.length;k++){var c=cs[k];"
    "if(!/^(BUTTON|A)$/.test(c.tagName)&&c.getAttribute('role')!=='button'){"
    "var cc=c.closest('button,a,[role=button]');if(cc)c=cc;}"
    "var cr=c.getBoundingClientRect();"
    "if(cr.width<2||cr.height<2||!vis(c))continue;"
    "hit={e:c,t:'закрыть'};break;}}"
    "if(hit){try{hit.e.click();}catch(x){}"
    "return JSON.stringify({text:hit.t.slice(0,60)});}}"
    "return '';})()"
)


def dismiss_overlay(host_part: Optional[str] = None,
                    tab_id: Optional[int] = None) -> Optional[str]:
    """Закрыть типовой оверлей-блокер, если он сейчас на странице (один
    консервативный клик — см. _DISMISS_OVERLAY_JS). → текст нажатого
    контрола; None — оверлея нет, кликнуть не удалось или бэкенд недоступен
    (это не ошибка: вызывается best effort перед снапшотом)."""
    try:
        raw = _eval_js_any(host_part, tab_id, _DISMISS_OVERLAY_JS)
    except Exception as e:
        logger.debug(f"[BrowserActions] Детект оверлеев недоступен: {e}")
        return None
    if not raw:
        return None
    try:
        text = str(json.loads(raw).get("text") or "").strip()
    except (TypeError, ValueError, AttributeError):
        return None
    if not text:
        return None
    logger.info(f"[BrowserActions] Оверлей закрыт автоматически: «{text[:60]}»")
    return text


# Показать панель управления плеера YouTube: она прячется автохайдом
# (класс ytp-autohide на контейнере) через ~3с без движения мыши — и снапшот
# не видит кнопок паузы/звука/настроек, а клики по ним резолвятся вслепую.
# Решение: инжект ПЕРСИСТЕНТНОГО style-оверрайда (живёт до навигации) +
# mousemove + снятие класса — панель остаётся видимой и для бота, и для
# пользователя («открой нижнюю панель видео»). Только youtube-хосты; на
# остальных страницах JS — тихий no-op
_REVEAL_PLAYER_JS = (
    "(function(){try{"
    "if(location.hostname.indexOf('youtube')<0)return '';"
    "var pl=document.querySelector('.html5-video-player');"
    "if(!pl)return '';"
    "if(!document.getElementById('vpc-player-reveal')){"
    "var st=document.createElement('style');st.id='vpc-player-reveal';"
    "st.textContent='.ytp-autohide .ytp-chrome-bottom,.ytp-autohide "
    ".ytp-chrome-top{opacity:1!important;visibility:visible!important}';"
    "(document.head||document.documentElement).appendChild(st);}"
    "pl.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,"
    "clientX:200,clientY:300}));"
    "pl.classList.remove('ytp-autohide');"
    "return 'ok';}catch(e){return '';}})()"
)


def reveal_player_controls(host_part: Optional[str] = None,
                           tab_id: Optional[int] = None) -> bool:
    """Раскрыть панель плеера YouTube (пауза/звук/настройки) — best effort,
    вызывается перед снапшотом: кнопки становятся видимыми для скоринга и
    кликов. → True, если на странице есть плеер и он раскрыт."""
    try:
        raw = _eval_js_any(host_part, tab_id, _REVEAL_PLAYER_JS)
    except Exception as e:
        logger.debug(f"[BrowserActions] Раскрытие плеера недоступно: {e}")
        return False
    if str(raw or "").strip() == "ok":
        logger.info(f"[BrowserActions] Панель плеера раскрыта "
                    f"({host_part or f'вкладка #{tab_id}'})")
        return True
    return False


# Детект антибот-стены (CAPTCHA / Cloudflare challenge): с ней ретраи
# бессмысленны — нужен честный отказ. Сигналы: заголовок challenge-страницы
# (там мало чего ещё есть) или КРУПНЫЙ видимый виджет капчи. Мелкий бейдж
# reCAPTCHA v3 (есть на куче обычных сайтов и ничего не блокирует) —
# сознательно отсекаем по площади.
_ANTIBOT_JS = (
    "(function(){"
    "var t=(document.title||'');"
    "if(/just a moment|attention required|access denied|captcha|"
    "are you a robot|robot check|доступ запрещ|не робот|"
    "проверка безопасности/i.test(t))return 'title: '+t.slice(0,60);"
    "var sels=['iframe[src*=recaptcha]','iframe[src*=hcaptcha]',"
    "'iframe[src*=challenges.cloudflare]','iframe[src*=smartcaptcha]',"
    "'iframe[src*=captcha]','#challenge-form','#challenge-stage',"
    "'[class*=CheckboxCaptcha]','[class*=SmartCaptcha]'];"
    "for(var i=0;i<sels.length;i++){var els=document.querySelectorAll(sels[i]);"
    "for(var j=0;j<els.length;j++){var e=els[j];"
    "var r=e.getBoundingClientRect();"
    "if(r.width*r.height<30000)continue;"
    "var s=getComputedStyle(e);"
    "if(s.display==='none'||s.visibility==='hidden')continue;"
    "return 'widget: '+sels[i];}}"
    "return '';})()"
)


def detect_antibot(host_part: Optional[str] = None,
                   tab_id: Optional[int] = None,
                   strict: bool = False) -> Optional[str]:
    """Признак антибот-проверки на странице → короткая метка
    («title: …»/«widget: …»); None — страница чиста.
    strict=True — сбой САМОГО ЗАМЕРА (вкладка не отвечает, транспорт не
    поддержан) поднимается исключением, а не превращается в None: «не
    смогли посмотреть» ≠ «посмотрели, чисто», и вызывающий (web_llm) не
    должен снимать карантин вслепую. strict=False — прежний best effort."""
    try:
        label = str(_eval_js_any(host_part, tab_id, _ANTIBOT_JS) or "").strip()
    except Exception as e:
        if strict:
            raise BrowserUnavailable(
                f"антибот-проверка не выполнена: {str(e)[:120]}")
        return None
    return label or None


# Позиция чекбокса антибот-виджета (Cloudflare Turnstile «Verify you are
# human», hCaptcha, Yandex SmartCaptcha): первый видимый challenge-iframe
# → его rect (чекбокс в таких виджетах слева по центру)
_CHALLENGE_BOX_JS = (
    "(function(){var sels=['iframe[src*=challenges.cloudflare]',"
    "'iframe[src*=hcaptcha]','iframe[src*=recaptcha]',"
    "'#challenge-stage iframe','iframe[src*=smartcaptcha]'];"
    "for(var i=0;i<sels.length;i++){var els=document.querySelectorAll(sels[i]);"
    "for(var j=0;j<els.length;j++){var e=els[j];"
    "var r=e.getBoundingClientRect();"
    "if(r.width<20||r.height<20)continue;"
    "var s=getComputedStyle(e);"
    "if(s.display==='none'||s.visibility==='hidden')continue;"
    "return JSON.stringify({x:r.left,y:r.top,w:r.width,h:r.height});}}"
    "return '';})()"
)


def try_challenge_autoclick(host_part: Optional[str] = None,
                            tab_id: Optional[int] = None) -> bool:
    """Одна автопопытка пройти простой чекбокс-челлендж («Verify you are
    human»): доверенный CDP-клик по позиции чекбокса в виджете. Повторных
    попыток НЕ делаем — повторы ухудшают поведенческий фингерпринт.
    True — клик отправлен (пройдёт ли — проверит повторный detect_antibot);
    False — виджета нет или клик не удался."""
    try:
        raw = str(_eval_js_any(host_part, tab_id, _CHALLENGE_BOX_JS) or "")
        box = json.loads(raw) if raw else None
    except Exception:
        return False
    if not box:
        return False
    x = float(box["x"]) + min(30.0, float(box["w"]) * 0.15)
    y = float(box["y"]) + float(box["h"]) / 2.0
    try:
        if is_raw_tab(tab_id):
            for ev_type in ("mousePressed", "mouseReleased"):
                _raw_tab_call(int(tab_id), "Input.dispatchMouseEvent",
                              {"type": ev_type, "x": x, "y": y,
                               "button": "left", "clickCount": 1})
        else:
            _WORKER.submit(lambda w: w.page_for(host_part, tab_id)
                           .mouse.click(x, y))
        logger.info(f"[BrowserActions] Антибот-чекбокс: автоклик ({x:.0f},{y:.0f})")
        return True
    except Exception as e:
        logger.debug(f"[BrowserActions] Автоклик по челленджу не удался: {e}")
        return False


def wait_dom_idle(host_part: Optional[str] = None, tab_id: Optional[int] = None,
                  timeout_sec: float = 2.0, min_wait: float = 0.3) -> None:
    """Пауза после действия ВМЕСТО фиксированного слипа: ждём, пока
    DOM-отпечаток перестанет меняться (DOM_STABLE_POLLS одинаковых замеров
    подряд с шагом DOM_POLL_MS), в границах [min_wait, timeout_sec]. Живая
    страница (дорендер SPA) даёт подождать дольше слепого слипа, статичная —
    выйти раньше. Бэкенд без eval — прежний фиксированный слип."""
    try:
        state = _eval_js_any(host_part, tab_id, _DOM_STATE_JS)
    except Exception:
        time.sleep(max(min_wait, timeout_sec / 2))
        return
    t0 = time.monotonic()
    stable = 0
    while True:
        elapsed = time.monotonic() - t0
        if elapsed >= timeout_sec:
            return
        if stable >= DOM_STABLE_POLLS and elapsed >= min_wait:
            return
        time.sleep(DOM_POLL_MS / 1000)
        try:
            cur = _eval_js_any(host_part, tab_id, _DOM_STATE_JS)
        except Exception:
            return  # страница в переходе между документами/вкладка умерла
        if cur != state:
            state = cur
            stable = 0
        else:
            stable += 1


# Доскролл-поиск цели (виртуализированные списки, react-window, бесконечные
# ленты): текста цели нет в отрендеренном DOM, пока её не доскроллили —
# выглядит как «не нашлось на странице». Примитивы для _resolve_element:
# позиция → шаг на экран вниз → восстановление при промахе.
_SCROLL_POS_JS = "String(window.scrollY||0)"

# scroll-behavior:smooth в CSS сайта делает scrollBy/scrollTop асинхронным:
# позиция сразу после вызова ещё старая, и шаг читался как «не сдвинулось» —
# доскролл-поиск обрывался на первом шаге, а цель ниже сгиба объявлялась
# «не нашлась на странице». На время шага поведение принудительно мгновенное
# (inline !important перебивает стиль сайта), затем стиль возвращаем как был.
_INSTANT_SCROLL_FN_JS = (
    "function __vpcInst(e){if(!e||!e.style)return function(){};"
    "var s=e.style,v=s.getPropertyValue('scroll-behavior'),"
    "p=s.getPropertyPriority('scroll-behavior');"
    "s.setProperty('scroll-behavior','auto','important');"
    "return function(){if(v)s.setProperty('scroll-behavior',v,p);"
    "else s.removeProperty('scroll-behavior');};}"
)

_SCROLL_STEP_JS = (
    "(function(){" + _INSTANT_SCROLL_FN_JS +
    "var de=document.documentElement,bd=document.body;"
    "var r1=__vpcInst(de),r2=__vpcInst(bd);"
    "var y0=window.scrollY||0;"
    "window.scrollBy(0,Math.round(window.innerHeight*0.9));"
    "var y1=window.scrollY||0;"
    "var lim=Math.max(de.scrollHeight,bd?bd.scrollHeight:0)"
    "-window.innerHeight;"
    "r1();r2();"
    "return JSON.stringify({moved:y1>y0+10,bottom:y1>=lim-2});})()"
)

# Шаг прокрутки КРУПНЕЙШЕГО внутреннего скроллящегося контейнера (очередь
# YouTube #items, внутренние ленты): окно стоит на месте, а виртуализированный
# список внутри контейнера подгружает пункты только при его прокрутке.
# y0 в ответе — исходная позиция контейнера для возврата
_CONTAINER_SCROLL_STEP_JS = (
    "(function(){" + _INSTANT_SCROLL_FN_JS +
    "var els=document.querySelectorAll('*'),best=null,bm=0;"
    "for(var i=0;i<els.length;i++){var e=els[i];"
    "if(e===document.body||e===document.documentElement)continue;"
    "if(e.scrollHeight<=e.clientHeight+40||e.clientHeight<100"
    "||e.clientWidth<150)continue;"
    "var st=getComputedStyle(e);"
    "if(st.overflowY!=='auto'&&st.overflowY!=='scroll')continue;"
    "var r=e.getBoundingClientRect();"
    "if(r.bottom<0||r.top>window.innerHeight)continue;"
    "var a=e.clientWidth*e.clientHeight;"
    "if(a>bm){bm=a;best=e;}}"
    "if(!best)return JSON.stringify({moved:false,bottom:false});"
    "var rst=__vpcInst(best);"
    "var y0=best.scrollTop;"
    "best.scrollTop+=Math.round(best.clientHeight*0.9);"
    "var y1=best.scrollTop;"
    "var lim=best.scrollHeight-best.clientHeight;"
    "rst();"
    "return JSON.stringify({moved:y1>y0+10,"
    "bottom:y1>=lim-2,y0:y0});})()")

_CONTAINER_SCROLL_RESTORE_JS = (
    "(function(){" + _INSTANT_SCROLL_FN_JS +
    "var els=document.querySelectorAll('*'),best=null,bm=0;"
    "for(var i=0;i<els.length;i++){var e=els[i];"
    "if(e===document.body||e===document.documentElement)continue;"
    "if(e.scrollHeight<=e.clientHeight+40||e.clientHeight<100"
    "||e.clientWidth<150)continue;"
    "var st=getComputedStyle(e);"
    "if(st.overflowY!=='auto'&&st.overflowY!=='scroll')continue;"
    "var r=e.getBoundingClientRect();"
    "if(r.bottom<0||r.top>window.innerHeight)continue;"
    "var a=e.clientWidth*e.clientHeight;"
    "if(a>bm){bm=a;best=e;}}"
    "if(best){var rst=__vpcInst(best);best.scrollTop=__Y__;rst();}"
    "return 'ok';})()")


def scroll_container_step(host_part: Optional[str] = None,
                          tab_id: Optional[int] = None) -> dict:
    """Один шаг вниз крупнейшего внутреннего скроллящегося контейнера →
    {"moved", "bottom", "y0"} (y0 — исходная позиция, для возврата)."""
    try:
        data = json.loads(_eval_js_any(host_part, tab_id,
                                       _CONTAINER_SCROLL_STEP_JS))
        return {"moved": bool(data.get("moved")),
                "bottom": bool(data.get("bottom")),
                "y0": data.get("y0")}
    except Exception:
        return {"moved": False, "bottom": False, "y0": None}


def scroll_container_restore(host_part: Optional[str] = None,
                             tab_id: Optional[int] = None,
                             y0: float = 0.0) -> None:
    """Вернуть контейнер в исходную позицию после доскролл-поиска."""
    try:
        _eval_js_any(host_part, tab_id,
                     _js_fill(_CONTAINER_SCROLL_RESTORE_JS,
                             Y=int(float(y0 or 0))))
    except Exception:
        pass


def scroll_position(host_part: Optional[str] = None,
                    tab_id: Optional[int] = None) -> Optional[float]:
    """Текущий scrollY вкладки; None — недоступно."""
    try:
        return float(_eval_js_any(host_part, tab_id, _SCROLL_POS_JS))
    except Exception:
        return None


def scroll_step(host_part: Optional[str] = None,
                tab_id: Optional[int] = None) -> dict:
    """Один экран вниз → {"moved": bool, "bottom": bool}; недоступно —
    {"moved": False, "bottom": False}."""
    try:
        raw = _eval_js_any(host_part, tab_id, _SCROLL_STEP_JS)
        data = json.loads(raw)
        return {"moved": bool(data.get("moved")),
                "bottom": bool(data.get("bottom"))}
    except Exception:
        return {"moved": False, "bottom": False}


def scroll_restore(host_part: Optional[str] = None, tab_id: Optional[int] = None,
                   y: float = 0.0) -> None:
    """Вернуть прокрутку на место после неудачного доскролл-поиска
    (пользователь не должен обнаружить страницу уехавшей). Мгновенно —
    при scroll-behavior:smooth возврат иначе доезжал бы уже после
    следующего снапшота."""
    try:
        _eval_js_any(host_part, tab_id,
                     "(function(){" + _INSTANT_SCROLL_FN_JS +
                     "var r1=__vpcInst(document.documentElement),"
                     "r2=__vpcInst(document.body);"
                     f"window.scrollTo(0,{float(y)});"
                     "r1();r2();return 'ok';})()")
    except Exception:
        pass


_PAGE_IDENTITY_JS = (
    "(document.title||'')+'|'+(function(){"
    "var m=document.querySelector('meta[property=\"og:site_name\"]');"
    "return m?(m.content||''):'';})()"
)


def page_identity(tab_id: Optional[int] = None,
                  host_part: Optional[str] = None) -> str:
    """«document.title|og:site_name» вкладки — мягкая верификация «тот ли
    сайт открыли» после навигации (резолв поиском, а не алиасом/историей)."""
    return _eval_js_any(host_part, tab_id, _PAGE_IDENTITY_JS)


def _shot_blank(shot: bytes) -> bool:
    """Кадр почти однотонный → «пустой» скриншот: фоновая вкладка могла
    ни разу не отрендериться (Chrome не композитит невидимое, а lazy-
    контент SPA ждёт IntersectionObserver — кейс 09.09, dodo: рамки
    разметки есть, а страница белая). Пороги: один тон ≥98.5% (чистая
    заглушка — замер 18.09: 100% пикселей тон 243, #f3f3f3) или top-2
    тона ≥99.5% (заглушка + широкая кайма/полоса второго тона). У реальной
    страницы даже на белом фоне текст/лого размазывают гистограмму. Ошибка
    разбора (нет Pillow и т.п.) — считаем кадр нормальным, не мешаем."""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(shot)).convert("L")
        hist = img.histogram()
        total = sum(hist) or 1
        top = sorted(hist, reverse=True)
        if top[0] / total >= 0.985:
            return True
        return (top[0] + top[1]) / total >= 0.995
    except Exception:
        return False


def _wait_first_frames(page, timeout_ms: int = 2500) -> None:
    """Первые кадры repaint после активации скрытой вкладки: 2×
    requestAnimationFrame (первый кадр видимости + отложенные
    resize/relayout — Chrome откладывает перерисовку скрытых вкладок,
    кейс 18.09: dodo-вкладка после ресайва окна отдала старый рендер
    в углу большого кадра, остальное белое). RAF в троттлируемой вкладке
    может не прийти вовсе — внешний setTimeout как страховка."""
    try:
        page.evaluate(
            "(ms) => new Promise(res => {"
            " let n = 0;"
            " const t = setTimeout(res, ms);"
            " const step = () => (++n >= 2)"
            "   ? (clearTimeout(t), res()) : requestAnimationFrame(step);"
            " requestAnimationFrame(step);"
            "})", timeout_ms)
    except Exception:
        time.sleep(1.0)


def _screenshot_os_level(page) -> Optional[bytes]:
    """Page.captureScreenshot с fromSurface:false — съёмка через оконный
    сервер ОС, МИМО композитора вкладки. Спасает, когда Chrome окно не
    композитит (окно скрыто/перекрыто/на другом Space): playwright-скриншот
    (fromSurface:true) при этом возвращает белый кадр, хотя DOM жив, а
    visibilityState с Chrome 152+ врёт «visible» всем вкладкам окна (кейс
    18.09, dodo: 92 элемента в снапшоте — и чисто белые кадры). scale =
    1/devicePixelRatio — картинка в CSS-пикселях, как у playwright
    scale='css' (рамки кандидатов считаются в CSS-координатах). У окна без
    экранной поверхности (свёрнуто в Dock) съёмка не выйдет — None/ошибка,
    caller идёт в активацию."""
    try:
        import base64
        try:
            dpr = float(page.evaluate("devicePixelRatio") or 1)
        except Exception:
            dpr = 1.0
        sess = page.context.new_cdp_session(page)
        try:
            res = sess.send("Page.captureScreenshot",
                            {"format": "jpeg", "quality": 80,
                             "fromSurface": False,
                             "scale": 1.0 / max(dpr, 0.1)})
        finally:
            try:
                sess.detach()
            except Exception:
                pass
        data = res.get("data") if isinstance(res, dict) else None
        return base64.b64decode(data) if data else None
    except Exception as e:
        logger.debug(f"[BrowserActions] OS-уровневый скриншот не удался: {e}")
        return None


# ── Зум страницы (команда «увеличь/уменьши/сбрось масштаб») ───────────
# CDP-клавиши браузерные акселераторы на macOS НЕ триггерят (замер 19.09:
# Meta+−/+/0 через Input.dispatchKeyEvent — innerWidth/outerWidth не
# меняется); работает только доверенный ввод — клик System Events по меню
# «Вид» процесса, и тот требует frontmost (там же замер). Поэтому зум —
# только macOS и только по ЯВНОЙ команде (окно поднять уместно). Тихий
# auto-fix — _ensure_zoom_normal; хосты, где зум выставлен этой командой,
# он не откатывает (_INTENTIONAL_ZOOM).
_INTENTIONAL_ZOOM: set = set()  # хосты с зумом, выставленным командой
_INTENTIONAL_ZOOM_LOCK = threading.Lock()

_ZOOM_MENU_ITEMS = {
    "in": ("Увеличить", "Zoom In"),
    "out": ("Уменьшить", "Zoom Out"),
    "reset": ("Фактический размер", "Actual Size"),
}


def _browser_pid(w) -> Optional[int]:
    """PID Chrome пула V: наш процесс, иначе SystemInfo.getProcessInfo по
    browser-сессии (Chrome мог быть поднят не нами — тогда _proc пуст).
    PID обязателен: меню жмём ТОЛЬКО процессу пула, личный Chrome
    пользователя — ниже по иерархии System Events, не наш."""
    if w._proc is not None and w._proc.poll() is None:
        return w._proc.pid
    try:
        sess = w._browser.new_browser_cdp_session()
        try:
            info = sess.send("SystemInfo.getProcessInfo")
        finally:
            try:
                sess.detach()
            except Exception:
                pass
        for p in (info or {}).get("processInfo") or []:
            if p.get("type") == "browser":
                # поле pid в ответе SystemInfo.getProcessInfo называется "id"
                return int(p.get("pid") or p.get("id") or 0) or None
    except Exception as e:
        logger.debug(f"[BrowserActions] PID браузера не получен: {e}")
    return None


def _ax_zoom_click(pid: int, direction: str) -> bool:
    """Клик по пункту меню «Вид»/«View» Chrome-процесса pid (RU→EN локаль).
    True — клик ушёл."""
    item_ru, item_en = _ZOOM_MENU_ITEMS[direction]
    script = _as_tell(
        f'    tell {_as_proc_ref(pid)}\n'
        '        set frontmost to true\n'
        '        delay 0.2\n'
        '        try\n'
        f'            click menu item "{item_ru}" of menu 1 of '
        'menu bar item "Вид" of menu bar 1\n'
        '        on error\n'
        f'            click menu item "{item_en}" of menu 1 of '
        'menu bar item "View" of menu bar 1\n'
        '        end try\n'
        '    end tell\n', app=_SYS_EVENTS_APP, guard=False)
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            logger.info(f"[BrowserActions] AX-зум не удался: "
                        f"{r.stderr.strip()[:120]}")
            return False
        return True
    except Exception as e:
        logger.info(f"[BrowserActions] AX-зум не удался: {e}")
        return False


def zoom_page(direction: str, host_part: Optional[str] = None,
              tab_id: Optional[int] = None) -> dict:
    """Шаг зума страницы («in»/«out») или сброс («reset») — как ⌘+/⌘−/⌘0
    у пользователя. Зум Chrome per-host, шаги его собственные (100→110→125…).
    → {"zoom": % после, "changed": bool, "host": ст}. Ошибки —
    BrowserUnavailable (caller отвечает честным отказом)."""
    if direction not in _ZOOM_MENU_ITEMS:
        raise BrowserUnavailable(f"неизвестное направление зума: {direction}")
    if _select_backend(tab_op=True) != "cdp":
        raise _no_backend("зум страницы")
    if sys.platform != "darwin":
        raise BrowserUnavailable("зум страницы пока управляется только на "
                                 "macOS — здесь вручную: Ctrl и +/−, "
                                 "сброс — Ctrl+0")

    def _op(w):
        page = w.page_for(host_part, tab_id)
        before = _CdpWorker._zoom_ratio(page)
        pid = _browser_pid(w)
        if not pid:
            raise BrowserUnavailable("не нашёл процесс браузера")
        try:
            page.bring_to_front()  # зум применяется к активной вкладке окна
        except Exception:
            pass
        if not _ax_zoom_click(pid, direction):
            raise BrowserUnavailable("меню зума не сработало — нет прав "
                                     "автоматизации?")
        time.sleep(0.5)
        after = _CdpWorker._zoom_ratio(page)
        try:
            host = (urlparse(page.url or "").hostname or "").lower()
            host = host.removeprefix("www.")
        except Exception:
            host = ""
        zoom = int(round(100 / max(after, 0.05)))
        if host:
            with _INTENTIONAL_ZOOM_LOCK:
                if abs(after - 1.0) <= _CdpWorker._ZOOM_RATIO_TOL:
                    _INTENTIONAL_ZOOM.discard(host)
                else:
                    _INTENTIONAL_ZOOM.add(host)
        logger.info(f"[BrowserActions] Зум ({direction}) "
                    f"{host or page.url}: "
                    f"{round(100 / max(before, 0.05))}% → {zoom}%")
        return {"zoom": zoom, "host": host,
                "changed": abs(after - before) > 0.02}
    return _WORKER.submit(_op)


def screenshot_viewport(host_part: Optional[str] = None,
                        tab_id: Optional[int] = None,
                        allow_focus: bool = False) -> Optional[bytes]:
    """JPEG-скриншот вьюпорта вкладки — для визуального фолбэка резолва
    (иконочные UI без accessible name, п.4) и отчёта «покажи страницу».
    scale='css': 1 CSS-пиксель = 1 пиксель картинки (на Retina device-scale
    даёт ×2 по каждой стороне — в 4 раза больше пикселей на кодирование/
    пересылку, а рамки кандидатов всё равно считаются в CSS-координатах);
    jpeg q80 заметно легче png и для кодирования, и для аплоада в веб-чат.
    Пустой (однотонный) кадр — лечим лестницей: пауза + пересъёмка (страница
    могла быть в середине рендера) → OS-уровневая съёмка окна
    (_screenshot_os_level, fromSurface:false — мимо композитора, который
    белый при скрытом/перекрытом окне, кейс 18.09 dodo) → тихая активация
    вкладки (_activate_tab_quietly, окно не всплывает) → при allow_focus
    жёсткая эскалация (bring_to_front + подъём окна — «покажи страницу»,
    всплытие уместно; тихие vision-фолбэки идут с allow_focus=False).
    document.visibilityState как критерий скрытости НЕ используется: с
    Chrome 152+ он врёт «visible» всем вкладкам окна (кейс 10.09).
    Кадр пуст после всех ступеней — None: белый кадр вызывающим не отдаём,
    vision по нему ткнул бы вслепую. None также — бэкенд не даёт скриншота/
    ошибка (фолбэк не обязателен)."""
    try:
        if _select_backend(tab_op=True) != "cdp":
            return None

        def _op(w):
            page = w.page_for(host_part, tab_id)
            w._ensure_zoom_normal(page)

            def _shoot() -> bytes:
                return page.screenshot(type="jpeg", quality=80, scale="css")

            def _os_shot() -> Optional[bytes]:
                s = _screenshot_os_level(page)
                return s if (s and not _shot_blank(s)) else None

            shot = _shoot()
            if not _shot_blank(shot):
                return shot
            # Середина рендера/навигации — пауза и один повтор
            time.sleep(0.8)
            shot = _shoot()
            if not _shot_blank(shot):
                return shot
            # Композитор кадр не отдаёт (окно скрыто/перекрыто/на другом
            # Space — кейс 18.09, dodo): съёмка через оконный сервер ОС,
            # вкладку не переключаем
            shot = _os_shot()
            if shot:
                return shot
            # Экранной поверхности нет (окно свёрнуто в Dock и т.п.) —
            # тихая активация вкладки, ожидание первых кадров, пересъёмка
            w._activate_tab_quietly(page, page.url)
            _wait_first_frames(page)
            shot = _os_shot()
            if shot:
                return shot
            shot = _shoot()
            if not _shot_blank(shot):
                return shot
            if allow_focus:
                # «Покажи страницу» — всплытие окна уместно: жёсткая
                # активация вкладки + подъём окна (CDP окно поднимает
                # не всегда — дублируем AppleScript на macOS)
                logger.info("[BrowserActions] Тихая активация не сработала "
                            "— поднимаю окно (запрошен показ страницы)")
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                try:
                    _focus_browser_tab(page.url)
                except Exception:
                    pass
                time.sleep(0.3)
                _wait_first_frames(page)
                shot = _os_shot()
                if shot:
                    return shot
                shot = _shoot()
                if not _shot_blank(shot):
                    return shot
            logger.info("[BrowserActions] Скриншот пуст после всех "
                        "попыток — кадр не отдан")
            return None
        return _WORKER.submit(_op, timeout=SUBMIT_CAPTURE_TIMEOUT_SEC)
    except Exception as e:
        logger.debug(f"[BrowserActions] Скриншот вьюпорта не удался: {e}")
        return None


# ── Полностраничный захват: скролл-стичинг ─────────────────
# CDP captureBeyondViewport на фоновой/скрытой вкладке бессилен (clip за
# пределы вьюпорта игнорируется — кадр остаётся вьюпортным, замер 19.09,
# Chrome 153), а Emulation-override на всю высоту даёт холст в сотни
# мегапикселей, на котором Chrome падает целиком (тот же замер: 3228×47170
# ≈ 152 Мпикс — краш пула). Поэтому полная страница собирается из
# вьюпортных кадров той же лестницей съёмки, что у screenshot_viewport
# (OS-уровень пробивает некомпозитимые окна): листаем с шагом
# vh − липкая_панель и клеим по факту сдвига scrollTop. Плавающие
# fixed-виджеты (чат-кнопка, куки-бар) на время прячем — иначе
# дублируются в каждом кадре; верхняя липкая панель остаётся, её полоса
# срезается со всех кадров, кроме первого, шагом прокрутки и кропом.
_FULLPAGE_MAX_HEIGHT_PX = 24000   # потолок захвата, CSS-пикселей
_FULLPAGE_PAUSE_SEC = 0.55        # догрузка ленивого контента между шагами
_FULLPAGE_SLICE_H = 1600          # высота куска в CSS px (лимит TG 2560)

_FULLPAGE_METRICS_JS = (
    "(function(){var se=document.scrollingElement||document.documentElement;"
    "return {sh: se.scrollHeight, vh: window.innerHeight,"
    " vw: window.innerWidth, y: se.scrollTop};})")
# Высота верхней липкой панели (шапка/навбар): full-width, прижата к верху
_FULLPAGE_STICKY_JS = (
    "(function(){var top=0,els=document.querySelectorAll('body *'),i,r,cs;"
    "for(i=0;i<els.length;i++){cs=getComputedStyle(els[i]);"
    "if(cs.position!=='sticky'&&cs.position!=='fixed')continue;"
    "r=els[i].getBoundingClientRect();"
    "if(r.width>=window.innerWidth*0.9&&r.top<=2&&r.height>=30"
    "&&r.height<=window.innerHeight*0.4)top=Math.max(top,r.bottom);}"
    "return Math.round(top);})")
# Прячем плавающие fixed/sticky-виджеты (чат-кнопки, куки-бары, плавающие
# TOC-кнопки wikipedia — иначе повторяются в каждом кадре, кейс 19.09),
# КРОМЕ верхней full-width панели — она нужна в первом кадре, а из
# остальных срезается кропом
_FULLPAGE_HIDE_FIXED_JS = (
    "(function(){var n=0,els=document.querySelectorAll('body *'),i,r,cs;"
    "for(i=0;i<els.length;i++){cs=getComputedStyle(els[i]);"
    "if(cs.position!=='fixed'&&cs.position!=='sticky')continue;"
    "r=els[i].getBoundingClientRect();"
    "if(r.width>=window.innerWidth*0.9&&r.top<=2"
    "&&r.height<=window.innerHeight*0.4)continue;"
    "if(r.width<2||r.height<2)continue;"
    # В метку кладём ИСХОДНЫЙ инлайновый display: `style.display=''` при
    # восстановлении затирал его (элемент со style="display:flex" оставался
    # без своего display и ломал вёрстку страницы после захвата)
    "els[i].setAttribute('data-vpc-fp-hide',els[i].style.display||'');"
    "els[i].style.display='none';n++;}"
    "return n;})")
_FULLPAGE_UNHIDE_JS = (
    "(function(){document.querySelectorAll('[data-vpc-fp-hide]').forEach("
    "function(e){var d=e.getAttribute('data-vpc-fp-hide')||'';"
    "if(d){e.style.display=d;}else{e.style.removeProperty('display');}"
    "e.removeAttribute('data-vpc-fp-hide');});"
    "document.querySelectorAll('[data-vpc-anchor]').forEach("
    "function(e){e.removeAttribute('data-vpc-anchor');});"
    "return 'ok';})")
# Оглавление страницы для текстовой сводки: разделы (h1-h3) и их позиции
# (ссылки/кнопки/цены) в порядке DOM. Лимиты: 12 разделов, 8 позиций в
# разделе, 60 позиций всего; дедуп по тексту
_FULLPAGE_OUTLINE_JS = (
    "(function(){var out=[],cur=null,seen={},total=0,i;"
    "var els=document.querySelectorAll("
    "'h1,h2,h3,[role=heading],a[href],button,[role=button],[class*=price]');"
    "function tx(e){return (e.innerText||e.textContent||'')"
    ".replace(/\\s+/g,' ').trim();}"
    "function vis(e){var s=getComputedStyle(e),r=e.getBoundingClientRect();"
    "return s.display!=='none'&&s.visibility!=='hidden'"
    "&&r.width>0&&r.height>0;}"
    "for(i=0;i<els.length&&total<60;i++){var el=els[i];"
    "if(!vis(el))continue;"
    "var t=tx(el);if(t.length<2||t.length>60)continue;"
    "var head=/^h[123]$/.test(el.tagName.toLowerCase())"
    "||el.getAttribute('role')==='heading';"
    "if(head){if(out.length>=12)continue;"
    "cur={head:t,items:[]};out.push(cur);continue;}"
    "if(!cur){cur={head:null,items:[]};out.push(cur);}"
    "if(cur.items.length>=8)continue;"
    "var k=t.toLowerCase();if(seen[k])continue;seen[k]=1;"
    "cur.items.push(t);total++;}"
    "return JSON.stringify(out);})")


# Якорь для точной стыковки кадров: reflow страницы между кадрами (догрузка
# картинок/шрифтов сдвигает контент) ломает стыковку «по scrollTop» (кейс
# 19.09: wikipedia, на шве полстроки пропало). Вместо координат страницы
# меряем ОДИН И ТОТ ЖЕ элемент в соседних кадрах: сдвиг его viewport-
# позиции между кадрами — точная величина перекрытия, при любых reflow.
# Якорь — самый нижний текстовый блок вьюпорта (запас 80px от низа)
_FULLPAGE_ANCHOR_MARK_JS = (
    "(function(){var els=document.querySelectorAll("
    "'p,li,h1,h2,h3,img,td,blockquote,pre');"
    "for(var i=els.length-1;i>=0;i--){var r=els[i].getBoundingClientRect();"
    "if(r.top>innerHeight*0.5&&r.top<innerHeight-80"
    "&&r.height>10&&r.width>100){"
    "els[i].setAttribute('data-vpc-anchor','1');return Math.round(r.top);}}"
    "return -1;})()")
_FULLPAGE_ANCHOR_READ_JS = (
    "(function(){var e=document.querySelector('[data-vpc-anchor]');"
    "if(!e)return -1;var r=Math.round(e.getBoundingClientRect().top);"
    "e.removeAttribute('data-vpc-anchor');return r;})()")


def _stitch_slices(frames: List[bytes], offs: List[float],
                   crops: List[float], vh_css: float,
                   slice_h: int = _FULLPAGE_SLICE_H) -> List[bytes]:
    """Склейка вьюпортных кадров в полную ленту и нарезка на куски.
    offs[i] — canvas-позиция i-го кадра, crops[i] — сколько срезать сверху
    (липкая панель), оба в CSS-пикселях; пересчитываются в масштаб кадра
    (источники дают разный: playwright scale='css' — 1:1, OS-уровень на
    Retina — ×dpr, замер 19.09). Соседние кадры внахлёст — поздний кадр
    перезаписывает перекрытие (это его область, свежий рендер).
    Высота куска ≤2560 image-пикселей (лимит длинной стороны фото
    Telegram). → список jpeg-кусков."""
    import io
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None  # лента за десятки мегапикселей — штатно
    imgs = [Image.open(io.BytesIO(f)).convert("RGB") for f in frames]
    if not imgs:
        return []
    size0 = imgs[0].size
    scale0 = size0[1] / max(float(vh_css), 1.0)
    total_h = int(round(offs[-1] * scale0)) + size0[1] \
        - int(round(crops[-1] * scale0))
    canvas = Image.new("RGB", (size0[0], max(1, total_h)))
    for im, off, cr in zip(imgs, offs, crops):
        if im.size != size0:
            im = im.resize(size0, Image.LANCZOS)
        c = int(round(cr * scale0))
        if c:
            im = im.crop((0, c, size0[0], im.height))
        canvas.paste(im, (0, int(round(off * scale0))))
    slice_h_img = max(1, min(round(slice_h * scale0), 2560))
    shots = []
    for top in range(0, total_h, slice_h_img):
        buf = io.BytesIO()
        canvas.crop((0, top, size0[0], min(top + slice_h_img,
                                           total_h))).save(
            buf, format="JPEG", quality=80)
        shots.append(buf.getvalue())
    return shots


def full_page_capture(host_part: Optional[str] = None,
                      tab_id: Optional[int] = None,
                      allow_focus: bool = True) -> Optional[dict]:
    """Полностраничный скриншот вкладки скролл-стичингом (см. блок выше):
    прогон сверху вниз вьюпортными кадрами, склейка, нарезка на куски
    ≤_FULLPAGE_SLICE_H для отправки альбомом. Позиция прокрутки и
    спрятанные на время fixed-виджеты восстанавливаются.
    → {"shots": [jpeg...], "outline": [{head, items}...], "captured": px,
       "total": px, "truncated": bool, "url": str}
    None — бэкенд не CDP или ни одного кадра не добыто (caller отдаёт
    обычный отчёт вьюпорта)."""
    try:
        if _select_backend(tab_op=True) != "cdp":
            return None

        def _op(w):
            page = w.page_for(host_part, tab_id)
            w._ensure_zoom_normal(page)
            wait_page_ready(page)

            def _metrics() -> dict:
                try:
                    m = page.evaluate(_FULLPAGE_METRICS_JS)
                    return m if isinstance(m, dict) else {}
                except Exception:
                    return {}

            m = _metrics()
            sh, vh = int(m.get("sh") or 0), int(m.get("vh") or 0)
            y0 = float(m.get("y") or 0)
            if vh < 150 or sh < 150:
                return None
            cap_h = min(sh, _FULLPAGE_MAX_HEIGHT_PX)

            def _frame() -> Optional[bytes]:
                try:
                    s = page.screenshot(type="jpeg", quality=80, scale="css")
                    if not _shot_blank(s):
                        return s
                except Exception:
                    pass
                s = _screenshot_os_level(page)
                return s if (s and not _shot_blank(s)) else None

            # Липкая верхняя панель: замер на месте и «в движении» — у части
            # сайтов панель проявляется только после начала прокрутки
            try:
                page.evaluate("window.scrollTo(0,0);'ok'")
            except Exception:
                pass
            time.sleep(0.35)
            sticky0 = 0
            try:
                sticky0 = int(page.evaluate(_FULLPAGE_STICKY_JS) or 0)
                if sh > vh:
                    page.evaluate(
                        f"window.scrollTo(0,{min(400, sh - vh)});'ok'")
                    time.sleep(0.3)
                    sticky0 = max(sticky0, int(
                        page.evaluate(_FULLPAGE_STICKY_JS) or 0))
                    page.evaluate("window.scrollTo(0,0);'ok'")
                    time.sleep(0.35)
            except Exception:
                sticky0 = 0
            sticky0 = max(0, min(sticky0, int(vh * 0.4)))

            def _sticky_here() -> int:
                # Панель на ТЕКУЩЕЙ позиции (могла появиться/исчезнуть
                # по ходу страницы) — её полоса срезается кропом кадра
                try:
                    return max(0, min(int(page.evaluate(
                        _FULLPAGE_STICKY_JS) or 0), int(vh * 0.4)))
                except Exception:
                    return sticky0

            step0 = max(200, vh - sticky0)
            # Потолок кадров выводится из высотных лимитов: на узком окне
            # (vh 860) 24000px — это ~30 кадров, фиксированные 18 резали бы
            # страницу раньше времени (замер 19.09, wikipedia)
            max_frames = min(40, max(12, int(cap_h / step0) + 2))
            try:
                page.evaluate(_FULLPAGE_HIDE_FIXED_JS)
            except Exception:
                pass

            frames: List[bytes] = []
            offs: List[float] = []    # canvas-позиция кадра, CSS px
            crops: List[float] = []   # срез сверху (липкая панель), CSS px
            escalated = False
            pending = None  # (r_old, prev_off, y_prev, prev_crop) — якорь
            try:
                y = 0.0
                while True:
                    # Плавающие виджеты прячем перед КАЖДЫМ кадром: часть
                    # становится fixed/sticky только после начала прокрутки
                    # (TOC-кнопка wikipedia — на старте она static, кейс 19.09)
                    try:
                        page.evaluate(_FULLPAGE_HIDE_FIXED_JS)
                    except Exception:
                        pass
                    st = _sticky_here()
                    crop = 0.0 if not frames else float(st)
                    if pending is None:
                        off = 0.0
                    else:
                        r_old, prev_off, y_prev, prev_crop = pending
                        pending = None
                        r_new = -1.0
                        try:
                            r_new = float(page.evaluate(
                                _FULLPAGE_ANCHOR_READ_JS) or -1)
                        except Exception:
                            pass
                        if (r_old >= 0 and r_new >= 0
                                # якорь врёт (элемент пересоздан) — откат
                                # на сдвиг по scrollTop
                                and abs((r_old - r_new) - (y - y_prev))
                                <= vh * 0.75):
                            off = (prev_off + (r_old - prev_crop)
                                   - (r_new - crop))
                        else:
                            off = (prev_off + (y - y_prev)
                                   - prev_crop + crop)
                    shot = _frame()
                    if shot is None and not frames and not escalated:
                        # Первый кадр не даётся — та же эскалация, что в
                        # screenshot_viewport: тихая активация, затем при
                        # allow_focus подъём окна («покажи страницу» —
                        # всплытие уместно)
                        escalated = True
                        try:
                            w._activate_tab_quietly(page, page.url)
                            _wait_first_frames(page)
                            shot = _frame()
                        except Exception:
                            shot = None
                        if shot is None and allow_focus:
                            logger.info("[BrowserActions] Полный захват: "
                                        "тихая активация не сработала — "
                                        "поднимаю окно")
                            try:
                                page.bring_to_front()
                            except Exception:
                                pass
                            try:
                                _focus_browser_tab(page.url)
                            except Exception:
                                pass
                            time.sleep(0.3)
                            _wait_first_frames(page)
                            shot = _frame()
                    if shot is None:
                        break
                    frames.append(shot)
                    offs.append(off)
                    crops.append(crop)
                    if (len(frames) >= max_frames
                            or y + vh >= sh - 2 or y + vh >= cap_h):
                        break
                    # Якорь в нижней части кадра ДО прокрутки — его сдвиг
                    # между кадрами даст точное перекрытие при склейке
                    r_old = -1.0
                    try:
                        r_old = float(page.evaluate(
                            _FULLPAGE_ANCHOR_MARK_JS) or -1)
                    except Exception:
                        pass
                    try:
                        page.evaluate(
                            f"window.scrollTo(0,"
                            f"{min(y + max(200, vh - st), cap_h)});'ok'")
                    except Exception:
                        break
                    time.sleep(_FULLPAGE_PAUSE_SEC)
                    nm = _metrics()
                    ny = float(nm.get("y") or y)
                    if ny <= y + 2:
                        nsh = int(nm.get("sh") or sh)
                        if nsh > sh:
                            # Лента догрузила контент — идём дальше
                            sh = nsh
                            cap_h = min(sh, _FULLPAGE_MAX_HEIGHT_PX)
                            continue
                        break  # низ страницы
                    pending = (r_old, off, y, crop)
                    y = ny
                    sh = int(nm.get("sh") or sh)
                    cap_h = min(sh, _FULLPAGE_MAX_HEIGHT_PX)
            finally:
                try:
                    page.evaluate(_FULLPAGE_UNHIDE_JS)
                except Exception:
                    pass
                try:
                    page.evaluate(f"window.scrollTo(0,{y0});'ok'")
                except Exception:
                    pass
            if not frames:
                return None
            shots = _stitch_slices(frames, offs, crops, vh)
            outline = []
            try:
                outline = json.loads(
                    str(page.evaluate(_FULLPAGE_OUTLINE_JS) or "[]"))
                if not isinstance(outline, list):
                    outline = []
            except Exception:
                outline = []
            captured = int(offs[-1] + vh - crops[-1]) if offs else 0
            return {"shots": shots, "outline": outline,
                    "captured": captured, "total": int(sh),
                    "truncated": captured < int(sh) - 2,
                    "url": str(page.url or "")}
        return _WORKER.submit(_op, timeout=SUBMIT_CAPTURE_TIMEOUT_SEC)
    except Exception as e:
        logger.info(f"[BrowserActions] Полностраничный захват не удался: {e}")
        return None


# ── Дедуп дублей карточки / служебный текст метаданных ────

# Центры ближе этого — кандидаты с одним ключом считаются дублями одной
# карточки. Тюнинг под плотность разметки: плотная лента — меньше,
# растянутые карточки — больше
_SAME_CARD_DIST_PX = 300

# Трекер-параметры запроса, не различающие цель ссылки
_LINK_KEY_DROP_PARAMS = {"fbclid", "gclid", "yclid", "dclid", "si", "pp"}


def _link_key(href: str) -> Optional[str]:
    """Нормализованный ключ ссылки для дедупа: lowercase, без #fragment и
    трекер-параметров (utm_*/fbclid/gclid/yclid/dclid/si/pp). None — ключ
    не считается (пустой href, '#', javascript:) и по ссылке не дедупим."""
    href = str(href or "").strip()
    if (not href or href.startswith("#")
            or href.lower().startswith("javascript:")):
        return None
    try:
        p = urlparse(href)
        qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
              if not k.lower().startswith("utm_")
              and k.lower() not in _LINK_KEY_DROP_PARAMS]
        query = urlencode(sorted(qs))
        return f"{p.scheme}|{p.netloc}|{p.path}|{query}".lower()
    except Exception:
        return href.lower()


def _card_text_key(it: dict) -> Optional[str]:
    """Ключ текста для дедупа: нормализованная подпись от 12 символов
    (короткие «Войти»/«OK» — слишком общие, их не дедупим)."""
    t = " ".join(str(it.get("text") or it.get("aria") or "").lower().split())
    return t if len(t) >= 12 else None


def _texts_dup(a: str, b: str) -> bool:
    """Дубль по тексту: равенство или префикс после среза хвостового
    «…»/«...» (обрезанный второй заголовок той же карточки)."""
    if not a or not b:
        return False
    if a == b:
        return True
    a2 = re.sub(r"(?:…|\.{3})+$", "", a).rstrip()
    b2 = re.sub(r"(?:…|\.{3})+$", "", b).rstrip()
    return (len(a2) >= 12 and len(b2) >= 12
            and (a2.startswith(b2) or b2.startswith(a2)))


def _rects_close(a: dict, b: dict) -> bool:
    """Рядом геометрически: центры в пределах _SAME_CARD_DIST_PX или один
    rect содержит ≥80% площади другого (тонкая строка метаданных внутри
    обёртки карточки)."""
    ax, ay = float(a.get("x") or 0), float(a.get("y") or 0)
    aw, ah = float(a.get("w") or 0), float(a.get("h") or 0)
    bx, by = float(b.get("x") or 0), float(b.get("y") or 0)
    bw, bh = float(b.get("w") or 0), float(b.get("h") or 0)
    dx = (ax + aw / 2) - (bx + bw / 2)
    dy = (ay + ah / 2) - (by + bh / 2)
    if dx * dx + dy * dy <= _SAME_CARD_DIST_PX * _SAME_CARD_DIST_PX:
        return True
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    small = min(aw * ah, bw * bh)
    return small > 0 and ix * iy >= 0.8 * small


# «Служебный» текст метаданных (просмотры/подписчики/даты/лайки) — общий
# JS-признак для зонального vision (_ALL_CLICKABLE_BOXES_JS) и
# псевдокликабельного прохода снапшота (_SNAPSHOT_JS): одна точка
# расширения словаря. Все ветки требуют цифру (у метаданных она почти
# всегда есть — счётчики/даты; защита от ложняков на навигации вроде «Мои
# подписки»); словарная ветка мультиязычна (RU/EN/PT/ES/FR/DE — расширять
# тут), остальные языконезависимы: короткая строка + цифра + разделитель
# метаданных •/·/| или высокая доля цифр
_SVC_FN_JS = (
    "function svc(t){t=(t||'');"
    "if(!/[0-9]/.test(t)||t.length>48)return false;"
    "if(/просмотр|подписчик|лайк|views?|subscribers?|likes?|ago|назад|"
    "atr[aá]s|hace|il y a|vor\\s|visualiza|inscrit/i.test(t))return true;"
    "if(/[•·|]/.test(t))return true;"
    "var d=t.replace(/[^0-9]/g,'').length,ns=t.replace(/\\s/g,'').length;"
    "return ns>0&&d>=Math.max(2,ns*0.25);}"
)

# Python-зеркало svc (_SVC_FN_JS) — для дедупа текстового снапшота ниже;
# при правке словаря держать обе формы синхронными
_SERVICE_WORDS_RE = re.compile(
    r"просмотр|подписчик|лайк|views?|subscribers?|likes?|ago|назад|"
    r"atr[aá]s|hace|il y a|vor\s|visualiza|inscrit", re.IGNORECASE)
# Подпись-бейдж длительности («1:03», «1:24:55» на ссылке-превью карточки)
_DURATION_TEXT_RE = re.compile(r"^\d{1,3}:\d{2}(:\d{2})?$")


def _service_text(t: str) -> bool:
    """«Служебная» строка метаданных: цифра + (словарь | разделитель •/·/| |
    доля цифр ≥25%), до 48 символов. Зеркало _SVC_FN_JS."""
    t = str(t or "")
    if not re.search(r"[0-9]", t) or len(t) > 48:
        return False
    if _SERVICE_WORDS_RE.search(t) or re.search(r"[•·|]", t):
        return True
    d = len(re.sub(r"[^0-9]", "", t))
    ns = len(re.sub(r"\s", "", t))
    return ns > 0 and d >= max(2, ns * 0.25)


# Теги псевдокликабельных фрагментов (pdiv-проход _SNAPSHOT_JS): span/div/
# li/p без href — «кликабельность» может быть унаследована (css-свойство
# cursor наследуется от кликабельной карточки-контейнера)
_SNAP_FRAG_TAGS = {"span", "div", "li", "p"}

# Интерактивные ARIA-роли: элемент с такой ролью — настоящий контрол,
# даже при «неинтерактивном» теге (div role=button)
_INTERACTIVE_ROLES = {"button", "link", "tab", "menuitem", "option",
                      "switch", "checkbox", "radio", "combobox",
                      "textbox", "searchbox", "slider"}


def _snap_frag(it: dict) -> bool:
    """Псевдокликабельный фрагмент снапшота: span/div/li/p без href, без
    интерактивной роли и не поле ввода — «кликабельность» обычно
    унаследована от карточки (css cursor наследуется). В дедупе и широком
    LLM-списке такие уступают настоящим контролам."""
    return (it.get("tag") in _SNAP_FRAG_TAGS and not it.get("href")
            and not it.get("ed")
            and str(it.get("role") or "") not in _INTERACTIVE_ROLES)


def _snap_label(it: dict) -> str:
    return str(it.get("text") or it.get("aria") or it.get("title") or "")


def _snap_label_quality(it: dict) -> Tuple[int, int, float]:
    """Качество подписи при дедупе: информативная (не служебная и не бейдж
    длительности) важнее всего, затем длиннее, затем больше площадь."""
    lab = _snap_label(it)
    informative = (bool(lab) and not _service_text(lab)
                   and not _DURATION_TEXT_RE.match(lab))
    return (1 if informative else 0, len(lab),
            float(it.get("w") or 0) * float(it.get("h") or 0))


def _dedup_snapshot_items(items: List[dict]) -> List[dict]:
    """Схлопывание дублей одной карточки в текстовом снапшоте (кейс 09.09,
    ютуб: 5 из 30 строк списка для LLM — фрагменты одной карточки: ссылка-
    превью «1:03», заголовок, строка «N просмотров • дата», имя канала).
    Оба правила — только для геометрически близких пар (одна карточка):
    дубли href в другом конце страницы (пагинация сверху и снизу) не
    трогаем.
    1) ссылки с одинаковым ключом href — остаётся одна, с более
       информативной подписью (_snap_label_quality);
    2) фрагмент (_snap_frag: span/div/li/p без href и интерактивной
       роли) с тем же текстом (от 3 символов, сравнение — общим _texts_dup:
       равенство или префикс после обрезанного «…»), что у рядом стоящего
       настоящего контрола, — выкидываем фрагмент: его pointer-курсор
       унаследован от карточки, а клик покрывает сам контрол. Разные
       href — разные цели, не дедупим.
    Правила проверяются ОБА на каждую пару: раньше совпадение href при
    далёких rect'ах уводило пару в `continue`, и текстовое правило для неё
    вообще не считалось."""
    lks = [_link_key(it.get("href")) for it in items]
    labs = [" ".join(_snap_label(it).lower().split()) for it in items]
    frags = [_snap_frag(it) for it in items]
    kept: List[int] = []
    for i, it in enumerate(items):
        dup_j = None
        for j in kept:
            same_link = lks[i] is not None and lks[i] == lks[j]
            # Текст сравнивается ОДНИМ определением на весь проект
            # (_texts_dup — им же дедупит карточки vision в computer_control)
            same_text = (frags[i] != frags[j] and len(labs[i]) >= 3
                         and _texts_dup(labs[i], labs[j]))
            if (same_link or same_text) and _rects_close(it, items[j]):
                dup_j = j
                break
        if dup_j is None:
            kept.append(i)
            continue
        if frags[i] != frags[dup_j]:
            # Фрагмент против настоящего контрола — остаётся контрол
            if not frags[i]:
                kept[kept.index(dup_j)] = i
        elif _snap_label_quality(it) > _snap_label_quality(items[dup_j]):
            kept[kept.index(dup_j)] = i
    if len(kept) < len(items):
        logger.debug(f"[BrowserActions] Дедуп снапшота: "
                     f"{len(items)} → {len(kept)} элементов")
    # Победитель занимал слот проигравшего — возвращаем DOM-порядок
    return [items[i] for i in sorted(kept)]


# ── DOM-снапшот и агентный клик («нажми X») ──────────────

# Номера разметки (data-vpc-idx общего снапшота и data-vpc-gidx целевого) —
# из ОДНОГО сквозного пространства: каждый прогон разметки (главный документ,
# каждый обойдённый iframe, целевой снапшот) берёт здесь свой блок номеров, и
# номера не переиспользуются. Отсюда три свойства, на которых держится
# адресация: номер главного документа не может совпасть с номером фрейма
# (коллизия при дедупе, когда метки выброшенных элементов остаются на
# странице); номер целевого снапшота не может совпасть с номером общего, и
# потребителю («нажми», «наведи», «скачай», ввод) не нужно знать, какой
# снапшот пометил элемент — номер ищется в обеих разметках сразу; метка
# прошлого снапшота (в том числе оставшаяся в необойдённом фрейме — чистка
# туда не заходит) никогда не совпадёт с номером текущего выбора, поэтому
# отложенный клик по протухшей метке честно не находит элемент, а не жмёт
# чужой. Счётчик стартует от часов: после перезапуска бота на живых страницах
# остаются метки прошлого процесса, и нумерация с нуля их бы догнала.
# Бюджеты — ЕДИНСТВЕННЫЙ источник: шаблоны снапшотов подставляют их в свой JS
# (var M=…) прямо отсюда, а не держат рядом свою копию числа. Раньше связь
# была только комментарием: правка константы оставляла в JS прежние B+100/B+25,
# и снапшот молча собирал не тот бюджет, что обещал питон.
SNAPSHOT_MAX = 100        # бюджет элементов общего снапшота
GOAL_SNAPSHOT_MAX = 25    # бюджет целевого снапшота
_MARK_LOCK = threading.Lock()
_MARK_NEXT = int(time.time() * 1000) % 1_000_000_000


def _mark_base(reserve: int) -> int:
    """Начало блока номеров для одного прогона разметки; reserve — потолок
    элементов этого прогона (блок выдаётся целиком, сколько бы JS ни
    разметил на самом деле)."""
    global _MARK_NEXT
    with _MARK_LOCK:
        base = _MARK_NEXT
        _MARK_NEXT = base + int(reserve)
        return base


def _mark_sel(idx: int) -> str:
    """CSS-ссылка на элемент снапшота: номер ищется и в общей разметке, и в
    целевой — номера сквозные, совпадение может быть только одно."""
    n = int(idx)
    return f'[data-vpc-idx="{n}"],[data-vpc-gidx="{n}"]'


# ── Единая подстановка значений в JS-шаблоны ──────────────
# Раньше плейсхолдеры (__GOAL__, __PROD__, __Q__, __NAME__, __SIDE__, __DIR__,
# __OP__, __BASE__…) жили ВНУТРИ кавычек шаблона ("...('__PROD__')") и
# подставлялись сырой строковой заменой — кавычки/бэкслеши пользовательского
# текста либо вырезались заранее (re.sub(r"[\"'\\]", "", …) — «L'Oréal»/
# «Papa John's» после такой чистки не совпадали с текстом на странице), либо
# не экранировались вовсе и держались только тем, что вызывающий код сам
# ограничивал значение (op из фиксированного набора и т.п.). Единый путь:
# шаблон использует плейсхолдер как ЗНАЧЕНИЕ (var goal=__GOAL__;, без кавычек
# вокруг него в тексте шаблона), а _js_fill сам решает, кодировать строкой,
# числом или булевым.
def _js_value(v) -> str:
    """Питоновское значение → JS-литерал, безопасный сразу для всех мостов
    (Playwright evaluate, сырой CDP Runtime.evaluate, AppleScript
    `execute … javascript "…"`/`do JavaScript "…" in t`).
    json.dumps(ensure_ascii=True) даёт JS-совместимый литерал: управляющие
    символы (включая перевод строки и U+2028/U+2029 — до ES2019 они ломали
    JS-строковые литералы, а нам важно быть безопасными и на старых WebKit)
    уходят в \\-escape, а не остаются сырыми символами; весь не-ASCII текст —
    в \\uXXXX (то же самое приводит к тому, что в результирующем JS вообще
    не остаётся «живых» кавычек/бэкслешей текста — экранирование
    AppleScript-моста (_as_lit) применяется поверх уже готового JS и не
    видит разницы между этим и константой в шаблоне).
    Числа/bool/None остаются JS-примитивами (не строкой в кавычках) —
    шаблоны сравнивают их без parseInt/приведения типов.
    "</script>" защищаем отдельно (json.dumps его не трогает) — на случай,
    если строка когда-нибудь окажется внутри HTML, а не только в evaluate."""
    if v is None or isinstance(v, (bool, int, float)):
        return json.dumps(v)
    if isinstance(v, (list, tuple)):
        # Таблица-константа (падежные окончания стема) — JS-массив строк, а
        # не str(list): подстановка таблиц идёт тем же единственным путём
        return json.dumps([str(x) for x in v],
                          ensure_ascii=True).replace("</", "<\\/")
    return json.dumps(str(v), ensure_ascii=True).replace("</", "<\\/")


def _clean_goal_text(s, limit: int = 80) -> str:
    """Текст цели/товара/запроса пользователя перед подстановкой в JS-шаблон:
    только сжатие пробелов и обрезка длины. Кавычки/бэкслеши раньше вырезались
    регуляркой начисто — «L'Oréal»/«Papa John's» после такой чистки переставали
    совпадать с текстом на странице; экранирование теперь на стороне
    JS-литерала (_js_fill/_js_value), резать сам текст не нужно."""
    return " ".join(str(s or "").split())[:limit].strip()


_JS_PLACEHOLDER_RE = re.compile(r"__([A-Z][A-Z0-9]*)__")


def _js_fill(template: str, **values) -> str:
    """Подставить значения в JS-шаблон с плейсхолдерами __NAME__ — ЕДИНСТВЕННЫЙ
    путь подстановки пользовательских/страничных значений в JS (взамен
    точечных вызовов .replace по имени плейсхолдера, разбросанных по всему
    файлу). Каждое имя из values обязано встретиться в шаблоне и наоборот —
    рассинхрон (опечатка в имени, забытое значение) падает сразу, а не тихо
    оставляет неподставленный плейсхолдер в JS-тексте."""
    seen: set = set()

    def _sub(m: "re.Match") -> str:
        name = m.group(1)
        seen.add(name)
        if name not in values:
            raise KeyError(f"_js_fill: нет значения для __{name}__ в шаблоне")
        return _js_value(values[name])

    out = _JS_PLACEHOLDER_RE.sub(_sub, template)
    extra = set(values) - seen
    if extra:
        raise KeyError(
            f"_js_fill: шаблон не содержит плейсхолдеров для {sorted(extra)}")
    return out


# ── Единая JS-нормализация текста для матчинга ────────────
# Зеркалит computer_control._norm_match/_fold_diacritics (комментарий там —
# источник истины про «почему»): регистр, ё→е, диакритика ЛАТИНИЦЫ через
# NFKD (кириллицу не трогаем — «й» разложился бы в «и»+бреве, и склейка
# слила бы «действие»/«действий»), дефисы/тире → пробел, апострофы разных
# начертаний → ASCII. Раньше в каждом шаблоне, сравнивающем текст страницы с
# целью пользователя, была своя копия этой нормализации (только дефисы —
# _GOAL_SNAPSHOT_JS; только ё→е — _SET_SLIDER_JS; кавычки вместо апострофов —
# _READ_SECTION_JS; ни диакритики, ни апострофов — _VPC_NORM_JS корзины) —
# «нажми елка» не находило «Ёлка», «lumieres» не находило Lumière. Теперь
# один фрагмент, подключаемый во все такие шаблоны.
_VPC_NORM_CORE_JS = (
    "function __vpcFold(ch){"
    "if(ch==='ё')return 'е';"
    "var d=ch.normalize?ch.normalize('NFKD'):ch;"
    "if(d.length>=2){var c0=d.charCodeAt(0);"
    "if(c0>=97&&c0<=122){var ok=true;"
    "for(var fi=1;fi<d.length;fi++){var cc=d.charCodeAt(fi);"
    "if(!(cc>=0x0300&&cc<=0x036f)){ok=false;break;}}"
    "if(ok)return d[0];}}"
    "return ch;}"
    "function __vpcN(s){"
    "var t=(s||'').toLowerCase()"
    ".replace(/[-‑–—]/g,' ').replace(/[’ʼ]/g,\"'\");"
    "var cs=Array.from(t),out='';"
    "for(var ci=0;ci<cs.length;ci++){out+=__vpcFold(cs[ci]);}"
    "return out.replace(/\\s+/g,' ').trim();}"
)

# Таблица падежных окончаний — ОДНА на проект: живёт в app.core.word_stem
# (stdlib-модуль, импорт вверху файла), рядом с питоновским `stem`, которым
# матчится текст в питоне (web_search._stem — алиас на него же). Отсюда же
# собирается JS-стем (__vpcStem ниже). Так «основа слова» в питоне и в JS —
# одно понятие, а не две похожие формулы (JS раньше усекал по ДЛИНЕ:
# «додстер» → «додс», питон по таблице → «додстер»).
#
# Модуль — не web_search, потому что browser_actions обязан подниматься на
# голой стандартной библиотеке: его импортирует scripts/chrome_debug.sh
# (ручной запуск браузера, когда бот сломан), а web_search тянет
# httpx/openai и чтение .env через app.core.router.

# Стем и поиск слова «с начала слова» поверх общей нормализации (__vpcN).
# Алгоритм — ОДИН В ОДИН с web_search._stem: первое подходящее окончание из
# таблицы _STEM_ENDINGS (она же у питона), основа не короче 4 символов, иначе
# слово как есть. Раньше здесь была своя формула (усечение по длине), а в
# _GOAL_SNAPSHOT_JS — её копия: «основ слова» в проекте было три.
# Подключается всем шаблонам, которые сравнивают слова цели с текстом
# страницы: целевой снапшот, корзина, состав продукта.
_VPC_NORM_JS = _js_fill(
    _VPC_NORM_CORE_JS +
    "var __vpcEnds=__ENDINGS__;"
    "function __vpcStem(w){"
    "for(var si=0;si<__vpcEnds.length;si++){var e=__vpcEnds[si];"
    "if(w.length-e.length>=4&&w.slice(w.length-e.length)===e)"
    "return w.slice(0,w.length-e.length);}"
    "return w;}"
    "function __vpcWIn(hay,w){var st=__vpcStem(w);var ws=hay.split(' ');"
    "for(var i=0;i<ws.length;i++){if(ws[i].indexOf(st)===0)return true;}"
    "return false;}",
    ENDINGS=_STEM_ENDINGS)




def _mark_find_js(idx: int, var: str = "el") -> str:
    """То же для мостов без querySelector со строкой '[attr=N]' (через
    AppleScript «=» внутри селектора ломает мост Chrome): цикл по обеим
    разметкам. NodeList-индекс нельзя — его порядок (document order)
    расходится с порядком присвоения номеров в снапшоте."""
    n = str(int(idx))
    return ("var vpcEls=document.querySelectorAll('[data-vpc-idx],[data-vpc-gidx]'),"
            + var + "=null,vpcI;"
            "for(vpcI=0;vpcI<vpcEls.length;vpcI++){var vpcE=vpcEls[vpcI];"
            "if(vpcE.getAttribute('data-vpc-idx')==='" + n + "'"
            "||vpcE.getAttribute('data-vpc-gidx')==='" + n + "'){"
            + var + "=vpcE;break;}}")


# Однострочный JS (только одинарные кавычки — через AppleScript идёт как есть):
# помечает видимые кликабельные элементы атрибутом data-vpc-idx (номера — от
# __BASE__, блок сквозного пространства; бюджеты проходов тоже от базы) и возвращает
# JSON {url, items:[{idx,tag,role,text,aria,title,href,w,h,vp}]}.
# Помимо текста собираем aria-label/title/alt/placeholder и роль (п.3) —
# подписи есть даже у иконок без текста. Видимость — по реальному рендеру:
# размер rect + getComputedStyle (display/visibility/opacity), vp — во вьюпорте.
# Проходы в порядке приоритета (общий бюджет 100 элементов): 1) пункты
# открытых попапов/меню; 2) кликабельные и label-переключатели открытой
# модалки (порталы в конце body иначе не влезают в бюджет); 3) стандартные
# ссылки/кнопки/поля (сначала во вьюпорте, потом остальные в DOM-порядке)
# + label с radio/checkbox; 3б) кнопки-иконки без текста
# (бургер-меню, крестик, корзина) — подпись синтезируется из class/id;
# 4) иконки-раскрыватели JS-меню
# (img/svg/i с class/src вида menu-open, nav-js, chevron…) — клик по иконке,
# подпись — текст родительского пункта меню (так устроены древовидные меню:
# обработчик висит на иконке, а не на пункте); 5) текстовые пункты JS-меню.
# NB: page.accessibility.snapshot() сознательно НЕ используем — в современном
# Playwright этот API deprecated; роли/лейблы собираем здесь сами, заодно с
# привязкой к реальным элементам для последующего клика.
_SNAPSHOT_JS = (
    # IIFE-обёртка — ОБЯЗАТЕЛЬНА: топ-левел var/function голого скрипта
    # ложатся в глобальный скоуп страницы, и evaluate падает с SyntaxError,
    # если сайт сам объявил то же имя лексически (global lexical environment
    # общий для всех скриптов страницы). Кейс 10.09: на странице с
    # «let inner» наш «var inner» убивал снапшот целиком («нажми master» →
    # Page.evaluate: Identifier 'inner' has already been declared)
    "(function(){"
    "var sel='a[href],button,[role=button],input[type=button],input[type=submit],summary,[role=link],"
    "[role=tab],[role=option],[role=menuitem],[role=switch]';"
    # Поля ввода — тоже элементы снапшота (флаг ed): команда «введи X в поле Y»
    # целится в них; клик по ним безвреден (фокус)
    "var edsel='textarea,input:not([type]),input[type=text],input[type=search],input[type=email],"
    "input[type=tel],input[type=url],input[type=number],input[type=password],"
    "[role=textbox],[role=searchbox],[role=combobox],"
    "[contenteditable]:not([contenteditable=false])';"
    "sel=sel+','+edsel;"
    "document.querySelectorAll('[data-vpc-idx],[data-vpc-host],[data-vpc-gidx]').forEach(function(e){e.removeAttribute('data-vpc-idx');e.removeAttribute('data-vpc-host');e.removeAttribute('data-vpc-gidx')});"
    "var B=__BASE__;var out=[],idx=B,i,el,r;"
    # Общий бюджет элементов — из SNAPSHOT_MAX (одно место), этапные бюджеты
    # проходов — его доли: пункты открытых меню 25%, модалка 50%, «голая»
    # навигация 60%, псевдокликабельные 70%, остальное — до полного бюджета.
    # Пропорции держатся сами при смене SNAPSHOT_MAX
    "var M=" + str(SNAPSHOT_MAX) + ";"
    "var M25=Math.round(M*0.25),M50=Math.round(M*0.5),"
    "M60=Math.round(M*0.6),M70=Math.round(M*0.7);"
    # Активный слой поверх затемнённого фона (панель комментариев ютуба,
    # модалка с бэкдропом, корзина-шторка dodo): ищем в стеке элементов в
    # центре вьюпорта бэкдроп-подобный (fixed/absolute, ≥60% вьюпорта,
    # полупрозрачная заливка 0<α<0.98; прозрачные fixed-обёртки виджетов —
    # не в счёт). Найден — sc каждого элемента решает ПОКРЫТИЕ его центра
    # (elementFromPoint, как cov в _GOAL_SNAPSHOT_JS): верхний в точке —
    # сам элемент/потомок/предок → элемент доступен (sc:1), иначе он под
    # бэкдропом (sc:0). Покрытие, а не «элемент стека над бэкдропом»:
    # слои бывают вложенными (кейс 11.09: шторка соусов dodo поверх
    # корзины — у неё свой оверлей rgba(0,0,0,0.64) поверх ВСЕГО, и
    # содержимое корзины тоже затемнено) и боковыми (корзина центр не
    # перекрывает — верхний стека там сам бэкдроп, кейс 10.09). Вне
    # вьюпорта при активном бэкдропе → sc:0 (внеэкранное под затемнением
    # недоступно; иначе весь внеэкранный каталог считался «в слое»).
    # Элементы вне слоя (sc:0) не попадают в текстовый выбор
    # (_choose_element/_llm_wide_pick), vision-рамки и собираются последними
    "var bdEl=null;try{"
    "var stk=document.elementsFromPoint(window.innerWidth/2,"
    "window.innerHeight/2);"
    "for(var bi=0;bi<stk.length;bi++){var bd=stk[bi];"
    "if(bd===document.body||bd===document.documentElement)break;"
    "var br=bd.getBoundingClientRect();"
    "if(br.width<window.innerWidth*0.6||br.height<window.innerHeight*0.6)"
    "continue;"
    "var bs2=getComputedStyle(bd);"
    "if(bs2.position!=='fixed'&&bs2.position!=='absolute')continue;"
    "var bm=(bs2.backgroundColor||'').match(/rgba\\(([^)]+)\\)/);"
    "if(!bm)continue;"
    "var bal=parseFloat(bm[1].split(',')[3]);"
    "if(!(bal>0&&bal<0.98))continue;"
    "bdEl=bd;break;}"
    "}catch(x){}"
    "function vpcSc(e){"
    "if(!bdEl)return 1;"
    "var scr=e.getBoundingClientRect();"
    "var scx=scr.left+scr.width/2,scy=scr.top+scr.height/2;"
    "if(scx<0||scy<0||scx>=window.innerWidth||scy>=window.innerHeight)return 0;"
    "var sct=null;try{sct=document.elementFromPoint(scx,scy);}catch(x2){}"
    "return (sct&&(e===sct||e.contains(sct)||sct.contains(e)))?1:0;}"
    "function vpcVis(e){var s=getComputedStyle(e);return s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';}"
    # Подпись поля ввода: aria-label → aria-labelledby → связанный <label> →
    # placeholder → name; по ней команда ввода находит поле («Выберите город»)
    "function vpcLabel(e){var t=e.getAttribute('aria-label')||'';"
    "if(!t){var lb=e.getAttribute('aria-labelledby');if(lb){var lo=document.getElementById(lb);if(lo)t=lo.innerText||'';}}"
    "if(!t&&e.labels&&e.labels.length)t=e.labels[0].innerText||'';"
    "if(!t)t=e.getAttribute('placeholder')||'';"
    # Плавающая подпись (анкета dodocontrol): <span>Имя</span><input> —
    # текстовый сосед ПЕРЕД полем; затем короткий текст родителя, если в нём
    # единственное поле (обёртка label-less форм)
    "if(!t){var sib=e.previousElementSibling;if(sib){var st=(sib.innerText||'').replace(/\\s+/g,' ').trim();if(st&&st.length<=40)t=st;}}"
    "if(!t){var pp=e.parentElement;if(pp&&pp.querySelectorAll('input,textarea,[contenteditable]').length===1){var pt=(pp.innerText||'').replace(/\\s+/g,' ').trim();if(pt&&pt.length<=40)t=pt;}}"
    "if(!t)t=e.getAttribute('name')||'';"
    "return t;}"
    # Модальный контекст: предок с role=dialog/aria-modal, классом
    # popup/modal/…, или фиксированный слой с высоким z-index (карточка
    # товара dodo — div.popup-inner в fixed-портале, без role=dialog)
    "function vpcMd(e){var p=e,d=0;"
    "while(p&&p!==document.body&&d<20){"
    "if(p.getAttribute){"
    "if(p.getAttribute('role')==='dialog'||p.hasAttribute('aria-modal'))return 1;"
    "var cl=(p.getAttribute('class')||'').toString();"
    "if(/popup|modal|dialog|overlay|sheet|lightbox/i.test(cl))return 1;"
    "var s=getComputedStyle(p);"
    "if(s.position==='fixed'&&parseFloat(s.zIndex||'0')>=10)return 1;}"
    "p=p.parentElement;d++;}"
    "return 0;}"
    # Виджет выбора (multiselect/v-select/combobox): его чип/поле — рабочий
    # контрол («нажми пиццамейкер» = открыть список вакансий), а одноимённые
    # карточки страницы кликаются впустую — бонус контролу в скоринге
    "function vpcSf(e){if(e.tagName==='SELECT')return 1;"
    "var p=e,d=0;"
    "while(p&&p!==document.body&&d<12){"
    "if(p.getAttribute){"
    "if(p.getAttribute('role')==='combobox')return 1;"
    "var cl=(p.getAttribute('class')||'').toString();"
    "if(/(^|[_\\s-])(multiselect|v-select)|select-trigger|select__control|"
    "combobox/i.test(cl))return 1;}"
    "p=p.parentElement;d++;}"
    "return 0;}"
    # Открытый выпадающий список (listbox/menu/option, vue-select и т.п.):
    # его пункты видны «прямо сейчас» и исчезнут при клике мимо — бонус
    # в скоринге против одноимённого фона страницы (пункт «Пиццамейкер»
    # списка vs карточка вакансии «Пиццамейкер» в разделе ниже)
    "function vpcDd(e){var r=e.getAttribute&&e.getAttribute('role');"
    "if(r==='option'||r==='menuitem')return 1;"
    "var p=e,d=0;"
    "while(p&&p!==document.body&&d<20){"
    "if(p.getAttribute){"
    "var pr=p.getAttribute('role');"
    "if(pr==='listbox'||pr==='menu'||pr==='tree')return 1;"
    "var cl=(p.getAttribute('class')||'').toString();"
    "if(/dropdown-menu|dropdown-list|dropdown-content|listbox|"
    "select-dropdown|options-list|suggest|autocomplete/i.test(cl))return 1;}"
    "p=p.parentElement;d++;}"
    "return 0;}"
    # Внешняя ссылка: уводит со страницы (футер dodo «Калорийность и состав»
    # → drive.google.com) — штраф в скоринге против on-page контролов
    "function vpcExt(e){try{return e.href&&(new URL(e.href)).host!==location.host?1:0;}catch(x){return 0;}}"
    "function vpcInfo(e,tg){var b=e.getBoundingClientRect();"
    "var ed=0;try{ed=e.matches(edsel)?1:0;}catch(x){}"
    "var t=(ed?(vpcLabel(e)||e.value||e.innerText||e.title||''):"
    "(e.innerText||e.value||e.getAttribute('aria-label')||e.title||e.getAttribute('alt')||e.getAttribute('placeholder')||'')).replace(/\\s+/g,' ').trim();"
    # Подпись-бейдж длительности («1:03» на ссылке-превью карточки ютуба):
    # innerText побеждает aria/title, и обёртка подписывается таймером
    # вместо названия (кейс 09.09: «[a] 1:03» в списке вместо заголовка
    # видео) — переподписываем из aria-label/title, если там длиннее
    "if(!ed&&/^\\d{1,3}:\\d{2}(:\\d{2})?$/.test(t)){"
    "var dtl=(e.getAttribute('aria-label')||e.title||'')"
    ".replace(/\\s+/g,' ').trim();"
    "if(dtl.length>t.length)t=dtl;}"
    # Голая цена/служебная строка — неуникальная подпись контрола (шторка
    # соусов dodo: шесть одинаковых пилюль-кнопок «49 ₽», скорингу и LLM их
    # не различить — кейс 11.09). Переподписываем из ближайшего предка-ряда
    # с коротким осмысленным текстом («Сырный 49 ₽») — как fb-подпись
    # «Сырный · 49 ₽» в целевом снапшоте. svc объявлен ниже (hoisting)
    "if(svc(t)){var rp=e.parentElement,rd=0;"
    "while(rp&&rp.tagName!=='BODY'&&rd<3){"
    "var rt=(rp.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(rt&&rt.length<=40&&!svc(rt)){t=rt;break;}"
    "rp=rp.parentElement;rd++;}}"
    # Контекст предка: НАИБОЛЬШИЙ предок в пределах 400 символов (первый
    # более крупный — лишь ряд кнопок «Заменить Изменить состав», а нужна
    # вся карточка «Кофе Капучино …» — иначе скоуп-клик «заменить в кофе
    # капучино» мимо); до 400 не нашлось — первый более крупный, как раньше
    "var ctx='',p=e.parentElement,d=0;"
    "while(p&&d<6){var pt=(p.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(pt.length>t.length+8){"
    "if(pt.length>400){if(!ctx)ctx=pt;break;}"
    "ctx=pt;}"
    "p=p.parentElement;d++;}"
    "ctx=ctx.slice(0,160);"
    "return {tag:tg,role:e.getAttribute('role')||'',text:t.slice(0,80),ctx:ctx,"
    "aria:(e.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim().slice(0,80),"
    "title:(e.title||'').replace(/\\s+/g,' ').trim().slice(0,80),"
    # data-testid — семантический крюк безтекстовых иконок: бургер платформы
    # School 21 — <button data-testid="MobileHeader.BurgerButton"> без
    # aria/текста, скорингу не за что зацепиться (кейс 10.09: «три полоски»
    # не находились на странице вообще). Скоринг матчит tid в общий hay
    "tid:(e.getAttribute('data-testid')||e.getAttribute('data-test-id')||'')"
    ".replace(/\\s+/g,' ').trim().slice(0,80),"
    "href:e.href||'',w:Math.round(b.width),h:Math.round(b.height),ed:ed,"
    # Элемент внутри открытой модалки/диалога (vpcMd): модальный контекст —
    # то, что пользователь сейчас видит; бонус в скоринге против одноимённых
    # ссылок футера («состав» на странице товара). sc — активный слой поверх
    # затемнённого фона (текстовый выбор и vision-рамки режутся до него)
    "md:vpcMd(e),dd:vpcDd(e),sf:vpcSf(e),ext:vpcExt(e),sc:vpcSc(e),"
    "q:(ed&&(e.type==='search'||/search|поиск/i.test((e.id||'')+' '+"
    "(e.getAttribute('class')||'')+' '+(e.getAttribute('name')||'')+' '+"
    "(e.getAttribute('placeholder')||'')))?1:0),"
    # Чувствительное поле (пароль/email/tel): ввод туда подтверждается
    # всегда (needs_confirm), «безопасное поле» по одной подписи не считаем
    "sn:(ed&&/^(password|email|tel)$/.test(e.type))?1:0,"
    "x:Math.round(b.left),y:Math.round(b.top),"
    "vp:(b.bottom>0&&b.right>0&&b.top<window.innerHeight&&b.left<window.innerWidth)?1:0};}"
    # Пилюли-переключатели вида «30 см / Тонкое тесто» (dodo), оценки,
    # согласия: это <label> с radio/checkbox внутри (инпут скрыт стилями,
    # кликабельна сама подпись). Собираем только label, обслуживающий
    # radio/checkbox (сам инпут в sel не входит и не размечается) — подписи
    # текстовых полей сюда не попадают, дублей с полями нет. Клик по label
    # переключает инпут нативно. lim — бюджет idx вызова (модалка/страница)
    "function vpcLabels(root,lim){var lbs=root.querySelectorAll('label');"
    "for(var li=0;li<lbs.length&&idx<lim;li++){var lb=lbs[li];"
    "if(lb.hasAttribute('data-vpc-idx')||lb.querySelector('[data-vpc-idx]'))continue;"
    "if(!lb.querySelector('input[type=radio],input[type=checkbox]')){"
    "var lf=lb.getAttribute('for');if(!lf)continue;"
    "var lo2=document.getElementById(lf);"
    "if(!lo2||(lo2.type!=='radio'&&lo2.type!=='checkbox'))continue;}"
    "r=lb.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(lb))continue;"
    "var infl=vpcInfo(lb,'label');"
    "if(!infl.text)continue;"
    "lb.setAttribute('data-vpc-idx',idx);infl.idx=idx;out.push(infl);idx++;}}"
    # Открытые попапы/выпадашки: в DOM идут последними, а бюджет в 100
    # элементов главная лента съедает раньше (youtube) — пункты открытого
    # меню собираем ПЕРВЫМИ (deepest-only внутри попапа: кликабельный ряд,
    # а не секция). «Мёртвый» якорь (<a id=endpoint href=""> у polymer-меню)
    # — не ссылка, пункт из-за него не выкидываем
    "var mh=/menu|item|link|folder|tab|btn|nav/i;"
    "var mtag=/menu|item|link|tab|btn|nav/i;"
    "var pops=[];"
    "var mns=document.querySelectorAll('*');"
    "for(i=0;i<mns.length&&pops.length<200;i++){el=mns[i];"
    "if(!mh.test(el.getAttribute('class')||'')&&!mtag.test(el.tagName.toLowerCase()))continue;"
    "if(!el.closest('[class*=popup],[class*=dropdown],[class*=dialog],[class*=overlay],[role=menu],[role=dialog],[role=listbox]'))continue;"
    "var inns=el.querySelectorAll(sel),live=false;"
    "for(var ii=0;ii<inns.length;ii++){var ie=inns[ii];"
    "if(ie.tagName!=='A'||ie.getAttribute('href')){live=true;break;}}"
    "if(live)continue;"
    "var mt=(el.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(!mt||mt.length>60)continue;"
    "r=el.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(el))continue;"
    "pops.push({e:el,t:mt});}"
    "for(i=0;i<pops.length&&idx<B+M25;i++){"
    "var deep=true;"
    "for(var j=0;j<pops.length;j++){if(i!==j&&pops[i].e.contains(pops[j].e)){deep=false;break;}}"
    "if(!deep)continue;"
    "el=pops[i].e;"
    "el.setAttribute('data-vpc-idx',idx);"
    "var inf0=vpcInfo(el,el.tagName.toLowerCase());inf0.text=pops[i].t.slice(0,80);"
    "inf0.idx=idx;out.push(inf0);idx++;}"
    # Открытая модалка поверх страницы (карточка товара, логин): её DOM
    # рендерится порталом в КОНЕЦ body, и при богатой странице за ним (dodo:
    # 340+ ссылок каталога) кнопки модалки не влезают в бюджет 100 — «В
    # корзину» не находилась вообще. Кликабельные внутри видимого диалога
    # собираем СРАЗУ после пунктов попапов: открытая модалка — почти всегда
    # то, что пользователь имеет в виду. Детект: role=dialog/aria-modal или
    # класс popup/modal/dialog/overlay у контейнера во «всплывающем слое»
    # (fixed/absolute у самого элемента ИЛИ предка — у dodo fixed-корень
    # портала носит сгенерированный класс, а popup-* внутри него static).
    # Бюджет — до idx 50, остаток — ленте.
    "var dlgs=document.querySelectorAll('[role=dialog],[aria-modal=true],[class*=popup],[class*=modal],[class*=Modal],[class*=dialog],[class*=overlay]');"
    "for(i=0;i<dlgs.length&&idx<B+M50;i++){el=dlgs[i];"
    "if(!el.getAttribute('role')&&!el.hasAttribute('aria-modal')){"
    "var fl=false,pa=el,up=0;"
    "while(pa&&pa.tagName!=='BODY'&&up<7){"
    "var pps=getComputedStyle(pa).position;"
    "if(pps==='fixed'||pps==='absolute'){fl=true;break;}"
    "pa=pa.parentElement;up++;}"
    "if(!fl)continue;}"
    "r=el.getBoundingClientRect();if(r.width<100||r.height<60)continue;"
    "if(!vpcVis(el))continue;"
    "var ins=el.querySelectorAll(sel);"
    "for(var i3=0;i3<ins.length&&idx<B+M50;i3++){var ie3=ins[i3];"
    "if(ie3.hasAttribute('data-vpc-idx'))continue;"
    "r=ie3.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(ie3))continue;"
    "var inf4=vpcInfo(ie3,ie3.tagName.toLowerCase());"
    "if(!inf4.text)continue;"
    "ie3.setAttribute('data-vpc-idx',idx);inf4.idx=idx;out.push(inf4);idx++;}"
    "vpcLabels(el,B+M50);}"
    # Кнопки закрытия окон/попапов («card-close» у dodo — без текста и
    # aria-label, портал модалки в КОНЦЕ body): поздние проходы до них не
    # добираются — бюджет съедает лента, и «закрой окно» не находит ничего.
    # Собираем рано, вслед за диалогами; безтекстовым подпись «закрыть»
    "var cls2=document.querySelectorAll('[class*=close],[class*=Close],"
    "[aria-label*=закры i],[aria-label*=close i],[aria-label*=dismiss i]');"
    "var cn=0;"
    "for(i=0;i<cls2.length&&cn<8&&idx<B+M50;i++){el=cls2[i];"
    "if(!/^(BUTTON|A)$/.test(el.tagName)&&el.getAttribute('role')!=='button'){"
    "var cc=el.closest('button,a,[role=button]');"
    "if(cc){el=cc;}else if(getComputedStyle(el).cursor!=='pointer'){"
    "var pc=el.parentElement;"
    "if(pc&&getComputedStyle(pc).cursor==='pointer'){el=pc;}else continue;}}"
    "if(el.hasAttribute('data-vpc-idx')||el.querySelector('[data-vpc-idx]'))continue;"
    "r=el.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(el))continue;"
    "var infc=vpcInfo(el,el.tagName.toLowerCase());"
    "if(!infc.text)infc.text='закрыть';"
    "el.setAttribute('data-vpc-idx',idx);infc.idx=idx;out.push(infc);"
    "idx++;cn++;}"
    # Якоря без href с JS-обработчиком (меню категорий dodo — «Кофе и чай»:
    # <a> в ul.links, клик обрабатывает React onClick, href нет — в общий
    # селектор a[href] такие не попадают, и пункты меню невидимы). Признак
    # кликабельности — cursor:pointer (мёртвые якоря-метки его не имеют)
    "var nah=document.querySelectorAll('a:not([href])');"
    "for(i=0;i<nah.length&&idx<B+M60;i++){el=nah[i];"
    "if(el.hasAttribute('data-vpc-idx')||el.querySelector('[data-vpc-idx]'))continue;"
    "if(getComputedStyle(el).cursor!=='pointer')continue;"
    "r=el.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(el))continue;"
    "var infa=vpcInfo(el,'a');"
    "if(!infa.text)continue;"
    "el.setAttribute('data-vpc-idx',idx);infa.idx=idx;out.push(infa);idx++;}"
    # Кликабельные div/span/li с коротким текстом (пункт «Ещё» в меню dodo —
    # div с cursor:pointer внутри <li>, ни ссылки, ни кнопки): React вешает
    # onClick на произвольный элемент. Признак тот же, что у псевдо-якорей —
    # cursor:pointer; из вложенных дублей с одним текстом берём глубочайший,
    # внутри/снаружи уже размеченного не повторяемся. ДВЕ ФАЗЫ: сначала
    # элементы во вьюпорте (богатые страницы dodo — 1400+ pointer-элементов
    # каталога до панели выбора в DOM, иначе бюджет кончается до неё).
    # NB: css-свойство cursor НАСЛЕДУЕТСЯ — внутри кликабельной карточки
    # pointer имеет каждый span метаданных (кейс 09.09, ютуб: 5 из 30 строк
    # списка для LLM — фрагменты одной карточки «1,7 млн просмотров», «•»,
    # «1 месяц назад»; они же ели бюджет idx). Срезаем такие до расхода
    # бюджета: явно неинтерактивные ARIA-роли, глифы без единой буквы/цифры
    # («•», «→» — их размечают icon-проход и vision) и служебные строки
    # метаданных (svc — общий с зональным vision признак, _SVC_FN_JS)
    + _SVC_FN_JS +
    "var pdiv=document.querySelectorAll('div,span,li,p');"
    "var pv=[],pvx=[],po=[],pox=[];"
    "for(i=0;i<pdiv.length;i++){el=pdiv[i];"
    "if(el.hasAttribute('data-vpc-idx')||el.querySelector('[data-vpc-idx]'))continue;"
    "if(el.closest('[data-vpc-idx],button,a'))continue;"
    "if(getComputedStyle(el).cursor!=='pointer')continue;"
    "var pdt=(el.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(!pdt||pdt.length>40)continue;"
    "var pdr=(el.getAttribute('role')||'');"
    "if(/^(text|presentation|none|separator|img)$/.test(pdr))continue;"
    "if(!/[\\p{L}\\p{N}]/u.test(pdt))continue;"
    # Служебная подпись у pointer-элемента (пилюля-цена «49 ₽»): не
    # выкидываем, если ряд подписывает её осмысленно (≤40, не svc) — шторка
    # соусов dodo держится на таких пилюлях; без ряда — режем, как раньше
    "if(svc(pdt)){var ap=el.parentElement,ad=0,at='';"
    "while(ap&&ap.tagName!=='BODY'&&ad<3){"
    "at=(ap.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(at&&at.length<=40&&!svc(at))break;at='';"
    "ap=ap.parentElement;ad++;}"
    "if(!at)continue;pdt=at;}"
    "r=el.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(el))continue;"
    "var pds=el.querySelectorAll('div,span,li,p'),deeper=false;"
    "for(var pj=0;pj<pds.length;pj++){var pe2=pds[pj];"
    "if(getComputedStyle(pe2).cursor!=='pointer')continue;"
    "if(((pe2.innerText||'').replace(/\\s+/g,' ').trim())===pdt){deeper=true;break;}}"
    "if(deeper)continue;"
    # При активном бэкдропе элементы его слоя — первыми: порталы шторок в
    # DOM последние, и без приоритета бюджет съедает затемнённый каталог
    "var ivp2=(r.bottom>0&&r.right>0&&r.top<window.innerHeight&&r.left<window.innerWidth);"
    "var isc2=bdEl?vpcSc(el):1;"
    "if(ivp2){(isc2?pv:pvx).push({e:el,t:pdt});}"
    "else{(isc2?po:pox).push({e:el,t:pdt});}}"
    "var pall=pv.concat(pvx,po,pox);"
    "for(i=0;i<pall.length&&idx<B+M70;i++){el=pall[i].e;"
    "el.setAttribute('data-vpc-idx',idx);"
    "var infp=vpcInfo(el,el.tagName.toLowerCase());infp.text=pall[i].t.slice(0,80);"
    "infp.idx=idx;out.push(infp);idx++;}"
    # Основной проход (ссылки/кнопки/поля) — ДВЕ ФАЗЫ, как у pdiv выше:
    # сначала элементы во вьюпорте, затем остальные в DOM-порядке. Иначе на
    # длинной странице бюджет 100 съедают шапка/сайдбар/футер, идущие в DOM
    # раньше видимых карточек (кейс 09.09, ютуб: ссылка-заголовок видимой
    # карточки не влезла в снапшот, и «видео с японскими символами» нажало
    # span имени канала из pdiv-прохода)
    "var els=document.querySelectorAll(sel);"
    "var evp=[],eoff=[],evx=[],eox=[];"
    "for(i=0;i<els.length;i++){el=els[i];r=el.getBoundingClientRect();"
    "if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(el))continue;"
    "if(el.hasAttribute('data-vpc-idx')||el.querySelector('[data-vpc-idx]'))continue;"
    # При активном бэкдропе — сначала элементы его слоя (внутри вьюпорта и
    # вне его): порталы шторок идут в DOM последними, и без приоритета их
    # кнопки не влезают в бюджет 100 (кейс 11.09: пилюли «49 ₽» шторки
    # соусов dodo потерялись за затемнённым каталогом)
    "var ivp3=(r.bottom>0&&r.right>0&&r.top<window.innerHeight&&r.left<window.innerWidth);"
    "var isc3=bdEl?vpcSc(el):1;"
    "if(ivp3){(isc3?evp:evx).push(el);}else{(isc3?eoff:eox).push(el);}}"
    "var eall=evp.concat(evx,eoff,eox);"
    "for(i=0;i<eall.length&&idx<B+M;i++){el=eall[i];"
    "var inf=vpcInfo(el,el.tagName.toLowerCase());"
    "if(!inf.text)continue;"
    "el.setAttribute('data-vpc-idx',idx);inf.idx=idx;out.push(inf);idx++;}"
    "vpcLabels(document,B+M);"
    # Кнопки-иконки БЕЗ текста (бургер-меню, крестик, корзина): у
    # «header-mobile__burger» ни innerText, ни aria-label — без этого прохода
    # элемент невидим, и нажать его нельзя никак. Подпись синтезируем из
    # class/id по словарю («нажми бургер» → «бургер-меню»). Элементы с
    # текстом уже собраны выше — их пропускаем
    "var blbl={burger:'бургер-меню',hamburger:'бургер-меню',menu:'меню',"
    "close:'закрыть',search:'поиск',cart:'корзина',basket:'корзина',"
    "profile:'профиль',account:'профиль',login:'войти',bell:'уведомления',"
    "notif:'уведомления',filter:'фильтры',setting:'настройки',gear:'настройки'};"
    "var btns=document.querySelectorAll('button,[role=button]');"
    "var nb5=0;"
    "for(i=0;i<btns.length&&idx<B+M;i++){el=btns[i];"
    "if(el.hasAttribute('data-vpc-idx')||el.querySelector('[data-vpc-idx]'))continue;"
    "r=el.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(el))continue;"
    "var inf5=vpcInfo(el,el.tagName.toLowerCase());"
    "if(inf5.text)continue;"
    "var cls=((el.getAttribute('class')||'')+' '+(el.getAttribute('id')||'')).toLowerCase();"
    "var lab='';for(var bk in blbl){if(cls.indexOf(bk)>=0){lab=blbl[bk];break;}}"
    # Совсем безымянные (иконка-SVG без текста/aria/словарного класса) тоже
    # берём, но с отдельной квотой: текстовый скоринг по ним бессилен, а
    # vision-фолбэку нужны кандидаты с рамками — иначе иконочная кнопка
    # невидима для всего контура
    "if(!lab){if(nb5>=12)continue;nb5++;}"
    "el.setAttribute('data-vpc-idx',idx);inf5.text=lab;inf5.idx=idx;"
    "out.push(inf5);idx++;}"
    "var ico=document.querySelectorAll('img,svg,i');"
    "for(i=0;i<ico.length&&idx<B+M;i++){el=ico[i];"
    "var hint=(el.getAttribute('class')||'')+' '+(el.getAttribute('src')||'')+' '+(el.getAttribute('alt')||'');"
    "if(!/open|clos|expand|toggle|nav|arrow|plus|minus|chevron|caret/i.test(hint))continue;"
    "if(el.closest('[data-vpc-idx]'))continue;"
    "r=el.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(el))continue;"
    "var host=el.closest('div,li,td,span');if(!host)continue;"
    "if(host.hasAttribute('data-vpc-host'))continue;"
    "var ht=(host.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(!ht||ht.length>80)continue;"
    "var inner=host.querySelector(sel),vis=false;"
    "if(inner){var ir=inner.getBoundingClientRect();if(ir.width>=2&&ir.height>=2)vis=true;}"
    "if(vis)continue;"
    "host.setAttribute('data-vpc-host','1');"
    "el.setAttribute('data-vpc-idx',idx);"
    "var inf2=vpcInfo(el,el.tagName.toLowerCase());inf2.text=ht.slice(0,80);inf2.idx=idx;"
    "out.push(inf2);idx++;}"
    # Остальные текстовые пункты JS-меню (сайдбары, папки меню вне попапов —
    # «Расписание» на ciu): menu-похожий класс ИЛИ кастомный тег, deepest-only.
    # Попапы уже собраны первым проходом — их сюда не тащим
    "var cand=[];"
    "for(i=0;i<mns.length&&cand.length<600;i++){el=mns[i];"
    "if(!mh.test(el.getAttribute('class')||'')&&!mtag.test(el.tagName.toLowerCase()))continue;"
    "if(el.closest('[data-vpc-idx],[data-vpc-host]'))continue;"
    "var inns2=el.querySelectorAll(sel),live2=false;"
    "for(var i2=0;i2<inns2.length;i2++){var ie2=inns2[i2];"
    "if(ie2.tagName!=='A'||ie2.getAttribute('href')){live2=true;break;}}"
    "if(live2)continue;"
    "var mt2=(el.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(!mt2||mt2.length>60)continue;"
    "r=el.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(el))continue;"
    "cand.push({e:el,t:mt2});}"
    "for(i=0;i<cand.length&&idx<B+M;i++){"
    "var deep2=true;"
    "for(var j2=0;j2<cand.length;j2++){if(i!==j2&&cand[i].e.contains(cand[j2].e)){deep2=false;break;}}"
    "if(!deep2)continue;"
    "el=cand[i].e;"
    "el.setAttribute('data-vpc-idx',idx);"
    "var inf3=vpcInfo(el,el.tagName.toLowerCase());inf3.text=cand[i].t.slice(0,80);inf3.idx=idx;"
    "out.push(inf3);idx++;}"
    # Открытые shadow root'ы (веб-компоненты — Salesforce, SPA-виджеты):
    # querySelectorAll главного документа в них не заходит — обходим
    # отдельно. Протухшие метки внутри shadow root главная чистка не снимает
    # — снимаем здесь сами. Клик по ним работает на CDP (playwright пронзает
    # open shadow DOM), на AppleScript — невидимы
    "var shroots=[];var wlk=function(n){if(n.shadowRoot)shroots.push(n.shadowRoot);"
    "var ch=n.children||[];for(var q=0;q<ch.length;q++){wlk(ch[q]);}};"
    "wlk(document.documentElement);"
    "for(var s2=0;s2<shroots.length;s2++){"
    "shroots[s2].querySelectorAll('[data-vpc-idx],[data-vpc-host]').forEach(function(e){"
    "e.removeAttribute('data-vpc-idx');e.removeAttribute('data-vpc-host')});}"
    "for(var s3=0;s3<shroots.length&&idx<B+M;s3++){"
    "var shes=shroots[s3].querySelectorAll(sel);"
    "for(var i4=0;i4<shes.length&&idx<B+M;i4++){var e4=shes[i4];"
    "if(e4.hasAttribute('data-vpc-idx'))continue;"
    "r=e4.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(e4))continue;"
    "var infs=vpcInfo(e4,e4.tagName.toLowerCase());"
    "if(!infs.text)continue;"
    "e4.setAttribute('data-vpc-idx',idx);infs.idx=idx;out.push(infs);idx++;}}"
    "return out.length?JSON.stringify({url:location.href,vw:window.innerWidth,items:out}):'__empty__';"
    "})()"
)


def _parse_snapshot(raw: str) -> Tuple[str, List[dict]]:
    """JSON снапшота → (url, нормализованные items). Любая неразбериха —
    BrowserUnavailable с человеческим текстом."""
    if raw == "__empty__":
        raise BrowserUnavailable("на странице нет кликабельных элементов")
    try:
        data = json.loads(raw)
        url = str(data.get("url") or "")
        vw = float(data.get("vw") or 0)
        raw_items = data.get("items") or []
    except (AttributeError, TypeError, ValueError):
        raise BrowserUnavailable(f"не разобрался снапшот страницы: {str(raw)[:80]}")
    items: List[dict] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        try:
            items.append({
                "idx": int(it.get("idx")),
                "tag": str(it.get("tag") or ""),
                "role": str(it.get("role") or ""),
                "text": str(it.get("text") or ""),
                "ctx": str(it.get("ctx") or ""),
                "aria": str(it.get("aria") or ""),
                "title": str(it.get("title") or ""),
                # data-testid — крюк безтекстовых иконок (бургер платформы)
                "tid": str(it.get("tid") or ""),
                "href": str(it.get("href") or ""),
                "w": float(it.get("w") or 0),
                "h": float(it.get("h") or 0),
                "x": float(it.get("x") or 0),
                "y": float(it.get("y") or 0),
                "vw": vw,
                "vp": bool(it.get("vp")),
                "ed": bool(it.get("ed")),
                "q": bool(it.get("q")),
                "sn": bool(it.get("sn")),
                "md": bool(it.get("md")),
                "dd": bool(it.get("dd")),
                "sf": bool(it.get("sf")),
                "ext": bool(it.get("ext")),
                # Элемент активного слоя (поверх затемнённого фона); у
                # фреймов/целевого снапшота флага нет — там по умолчанию
                "sc": bool(it.get("sc", 1)),
            })
        except (TypeError, ValueError):
            continue
    # Дедуп дублей одной карточки (несколько a[href] на одну ссылку;
    # фрагмент-спан, повторяющий соседний контрол) — кейс 09.09: список для
    # LLM забит фрагментами одной карточки ютуба. idx не перенумеровываем:
    # метки data-vpc-idx на странице соответствуют оставшимся элементам
    items = _dedup_snapshot_items(items)
    if not items:
        raise BrowserUnavailable("на странице нет кликабельных элементов")
    return url, items


# Снапшот обходит и видимые iframe'ы (чаты, виджеты оплаты, встроенные карты):
# JS главного фрейма их DOM не видит. Только CDP (frame.evaluate работает и
# для cross-origin фреймов); AppleScript-фолбэк остаётся без фреймов — там
# same-origin политика. Компактная версия снапшота: только стандартные
# кликабельные/поля, без приоритетных проходов попапов/иконок. Номера — свой
# блок сквозного пространства на каждый фрейм (_mark_base): клик ищет метку
# по всем фреймам, и номер фрейма не может совпасть с номером главного
# документа или соседнего фрейма.
FRAME_SNAPSHOT_MAX = 3     # столько видимых фреймов обходим за снапшот
FRAME_SNAPSHOT_ITEMS = 25  # бюджет элементов на фрейм (у главного — 100)

_FRAME_SNAPSHOT_JS = (
    "(function(base,lim){"
    "document.querySelectorAll('[data-vpc-idx],[data-vpc-gidx]').forEach(function(e){"
    "e.removeAttribute('data-vpc-idx');e.removeAttribute('data-vpc-gidx')});"
    "var sel='a[href],button,[role=button],input[type=button],input[type=submit],"
    "summary,[role=link],[role=tab],[role=option],[role=menuitem],[role=switch]';"
    "var edsel='textarea,input:not([type]),input[type=text],input[type=search],"
    "input[type=email],input[type=tel],input[type=url],input[type=number],"
    "input[type=password],[role=textbox],[role=searchbox],[role=combobox],"
    "[contenteditable]:not([contenteditable=false])';"
    "sel=sel+','+edsel;"
    "var out=[],idx=base;"
    "var els=document.querySelectorAll(sel);"
    "for(var i=0;i<els.length&&out.length<lim;i++){var e=els[i];"
    "var r=e.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "var s=getComputedStyle(e);"
    "if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')continue;"
    "var ed=0;try{ed=e.matches(edsel)?1:0;}catch(x){}"
    "var t=(e.innerText||e.value||e.getAttribute('aria-label')||e.title||"
    "e.getAttribute('placeholder')||'').replace(/\\s+/g,' ').trim();"
    # Плавающая подпись поля (текстовый сосед перед input) — как vpcLabel
    "if(!t&&ed){var sib=e.previousElementSibling;if(sib){t=(sib.innerText||'').replace(/\\s+/g,' ').trim();if(t.length>40)t='';}}"
    "if(!t)continue;"
    "e.setAttribute('data-vpc-idx',idx);"
    "out.push({idx:idx,tag:e.tagName.toLowerCase(),"
    "role:e.getAttribute('role')||'',text:t.slice(0,80),"
    "aria:(e.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim().slice(0,80),"
    "title:(e.title||'').replace(/\\s+/g,' ').trim().slice(0,80),"
    "href:e.href||'',w:Math.round(r.width),h:Math.round(r.height),"
    "q:(ed&&(e.type==='search'||/search|поиск/i.test((e.id||'')+' '+"
    "(e.getAttribute('class')||'')+' '+(e.getAttribute('name')||'')+' '+"
    "(e.getAttribute('placeholder')||'')))?1:0),"
    # Чувствительное поле (пароль/email/tel): ввод туда подтверждается
    # всегда (needs_confirm), «безопасное поле» по одной подписи не считаем
    "sn:(ed&&/^(password|email|tel)$/.test(e.type))?1:0,"
    "x:Math.round(r.left),y:Math.round(r.top),ed:ed,"
    "md:(e.closest('[role=dialog],[aria-modal=true],[class*=popup],"
    "[class*=modal],[class*=Modal],[class*=dialog],[class*=overlay]')?1:0),"
    "dd:(e.closest('[role=listbox],[role=menu],[role=tree],[role=option],"
    "[role=menuitem],[class*=dropdown-menu],[class*=listbox],"
    "[class*=select-dropdown],[class*=options-list],[class*=suggest],"
    "[class*=autocomplete]')?1:0),"
    "sf:(e.closest('select,[role=combobox],.multiselect,.v-select,"
    "[class*=select-trigger],[class*=select__control]')?1:0),"
    "ext:(function(){try{return e.href&&(new URL(e.href)).host!==location.host?1:0;}catch(x){return 0;}})(),"
    "vp:(r.bottom>0&&r.right>0&&r.top<window.innerHeight&&r.left<window.innerWidth)?1:0});"
    "idx++;}"
    "return JSON.stringify({items:out});})(__BASE__,__LIM__)"
)


def _merge_frame_items(page, items: List[dict]) -> List[dict]:
    """Добавить к снапшоту элементы видимых iframe'ов страницы (CDP, поток
    воркера). Пропускаем: мелкие фреймы (<100×60 — трекеры/пиксели) и
    about:blank. У фрейм-элементов: fr — хост фрейма, x — в координатах
    страницы (rect фрейма + сдвиг iframe во вьюпорте), ctx пуст (чужой мир,
    контекст предка бесполезен), vp — «во вьюпорте фрейма И фрейм на
    экране». Номера каждого фрейма — свой блок сквозного пространства
    (_mark_base): считать базу от номеров оставшихся элементов нельзя —
    дедуп снапшота выбрасывает элементы из списка, а их метки остаются на
    странице, и база «max(idx)+1» налезала бы на выброшенный дубль."""
    try:
        frames = [f for f in page.frames if f != page.main_frame]
    except Exception:
        return items
    if not frames:
        return items
    try:
        vwsz = str(page.evaluate("window.innerWidth+'x'+window.innerHeight"))
        vw_p, vh_p = (float(v) for v in vwsz.split("x"))
    except Exception:
        vw_p = vh_p = 0.0
    used = 0
    for fr in frames:
        if used >= FRAME_SNAPSHOT_MAX:
            break
        try:
            furl = str(fr.url or "")
        except Exception:
            furl = ""
        if not furl or furl.startswith("about:"):
            continue
        try:
            fe = fr.frame_element()
            box = fe.bounding_box() if fe is not None else None
        except Exception:
            box = None
        if not box or box.get("width", 0) < 100 or box.get("height", 0) < 60:
            continue
        try:
            raw = str(fr.evaluate(
                _js_fill(_FRAME_SNAPSHOT_JS,
                        BASE=_mark_base(FRAME_SNAPSHOT_ITEMS),
                        LIM=FRAME_SNAPSHOT_ITEMS)) or "")
            fitems = json.loads(raw).get("items") or []
        except Exception:
            continue  # фрейм в переходе между документами — пропускаем
        fhost = urlparse(furl).hostname or furl
        box_vp = bool(vw_p) and (box["x"] < vw_p
                                 and box["x"] + box["width"] > 0
                                 and box["y"] < vh_p
                                 and box["y"] + box["height"] > 0)
        added = 0
        for it in fitems:
            if not isinstance(it, dict):
                continue
            try:
                items.append({
                    "idx": int(it.get("idx")),
                    "tag": str(it.get("tag") or ""),
                    "role": str(it.get("role") or ""),
                    "text": str(it.get("text") or ""),
                    "ctx": "",
                    "aria": str(it.get("aria") or ""),
                    "title": str(it.get("title") or ""),
                    "tid": str(it.get("tid") or ""),
                    "href": str(it.get("href") or ""),
                    "w": float(it.get("w") or 0),
                    "h": float(it.get("h") or 0),
                    "x": float(it.get("x") or 0) + float(box.get("x") or 0),
                    "y": float(it.get("y") or 0) + float(box.get("y") or 0),
                    "vw": 0.0,
                    "vp": bool(it.get("vp")) and box_vp,
                    "ed": bool(it.get("ed")),
                    "q": bool(it.get("q")),
                    "sn": bool(it.get("sn")),
                    "md": bool(it.get("md")),
                    "dd": bool(it.get("dd")),
                    "sf": bool(it.get("sf")),
                    "ext": bool(it.get("ext")),
                    "fr": fhost,
                })
                added += 1
            except (TypeError, ValueError):
                continue
        if added:
            used += 1
            logger.info(f"[BrowserActions] iframe {fhost}: +{added} элементов "
                        f"в снапшот")
    return items


def snapshot_elements(host_part: Optional[str] = None,
                      tab_id: Optional[int] = None) -> Tuple[str, str, List[dict]]:
    """(url, host, items) видимых кликабельных элементов вкладки. Перед
    снапшотом на CDP ждём готовности страницы (п.2). host_part=None —
    активная/крайняя вкладка; tab_id — точная отслеживаемая вкладка.
    На CDP к элементам главного фрейма добавляются элементы видимых
    iframe'ов (_merge_frame_items). Номера разметки — блок сквозного
    пространства (_mark_base): у каждого снапшота они свои, метки прошлого
    не матчатся."""
    if _select_backend(tab_op=True) == "cdp":
        def _op(w):
            page = w.page_for(host_part, tab_id)
            w._ensure_zoom_normal(page)
            wait_page_ready(page)
            raw = str(page.evaluate(
                _js_fill(_SNAPSHOT_JS, BASE=_mark_base(SNAPSHOT_MAX))) or "")
            if raw == "__empty__":
                # Главный фрейм пуст — весь контент может жить в iframe
                url, items = str(page.url or ""), []
            else:
                url, items = _parse_snapshot(raw)
            items = _merge_frame_items(page, items)
            if not items:
                raise BrowserUnavailable("на странице нет кликабельных элементов")
            return url, items
        url, items = _WORKER.submit(_op)
    else:
        url, items = _parse_snapshot(_run_apple_events(
            host_part,
            _js_fill(_SNAPSHOT_JS, BASE=_mark_base(SNAPSHOT_MAX)),
            tab_id=tab_id))
    host = urlparse(url).hostname or url
    return url, host, items


# Целевой снапшот «места»: общий снапшот режется бюджетом 100 (dodo: 350+
# кликабельных, «Додстер» из раздела закусок туда не влезает) — здесь ищем
# по ВСЕМУ DOM элементы, чей собственный текст совпадает с целью, и размечаем
# не только их, но и ВСЕ контролы найденного места (карточка товара: кнопка
# цены, пилюли размера/теста) — дальше обычный скоринг/LLM работает уже по
# локальному окружению, а «сделай тонкое у додстера» не тонет за бюджетом.
# Совпадение: фраза целиком ИЛИ слова по отдельности (от 3 букв; слова от 6
# букв — по усечённому началу: «додстера» → «додстер…» — бедный стемминг
# русской морфологии без словаря). Кандидаты ранжируются по числу совпавших
# слов, первое совпадение прокручивается во вьюпорт (scrollIntoView — и
# визуальный отклик «вот где оно», и честный флаг vp).
# Цель клика для совпадения: сам элемент, если он кликабелен; иначе
# кликабельный предок (на dodo заголовок товара — <span> внутри <a>); иначе
# первая видимая кнопка/ссылка внутри карточки-предка («Выбрать»).
# Место — ближайший предок цели с ≥2 видимыми интерактивными элементами
# (потолок 500 символов текста): селекторы и объём текста ненадёжны — у
# dodo карточка это «Додстер от 169 ₽» (16 символов), а [class*=product]
# цепляет h3.product-title с одним заголовком. Внутрь места входят и
# псевдокнопки: короткий текст + cursor:pointer (цена «от 385 ₽» на dodo —
# span с React-обработчиком, не button и не ссылка). Разметка — ОТДЕЛЬНЫЙ
# атрибут data-vpc-gidx, чтобы не затирать метки общего снапшота (выбор
# может остаться за ним), а номера — из общего сквозного пространства
# (_mark_base): потребителю не нужно знать, какой снапшот пометил элемент.
_GOAL_SNAPSHOT_JS = (
    "(function(goal){"
    + _VPC_NORM_JS +
    "var sel='a[href],button,[role=button],input[type=button],input[type=submit],"
    "summary,[role=link],[role=tab],[role=option],[role=menuitem],[role=switch]';"
    "document.querySelectorAll('[data-vpc-gidx]').forEach(function(e){"
    "e.removeAttribute('data-vpc-gidx')});"
    "var B=__BASE__;var out=[],idx=B,i;"
    "var M=" + str(GOAL_SNAPSHOT_MAX) + ";"  # бюджет — из константы, не литерал
    "function vpcVis(e){var s=getComputedStyle(e);"
    "return s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';}"
    # Модальный контекст — как vpcMd в _SNAPSHOT_JS (role=dialog у карточки
    # dodo нет, только класс popup-inner в fixed-портале)
    "function vpcMd(e){var p=e,d=0;"
    "while(p&&p!==document.body&&d<20){"
    "if(p.getAttribute){"
    "if(p.getAttribute('role')==='dialog'||p.hasAttribute('aria-modal'))return 1;"
    "var cl=(p.getAttribute('class')||'').toString();"
    "if(/popup|modal|dialog|overlay|sheet|lightbox/i.test(cl))return 1;"
    "var s=getComputedStyle(p);"
    "if(s.position==='fixed'&&parseFloat(s.zIndex||'0')>=10)return 1;}"
    "p=p.parentElement;d++;}"
    "return 0;}"
    # Открытый выпадающий список — как vpcDd в _SNAPSHOT_JS
    "function vpcDd(e){var r=e.getAttribute&&e.getAttribute('role');"
    "if(r==='option'||r==='menuitem')return 1;"
    "var p=e,d=0;"
    "while(p&&p!==document.body&&d<20){"
    "if(p.getAttribute){"
    "var pr=p.getAttribute('role');"
    "if(pr==='listbox'||pr==='menu'||pr==='tree')return 1;"
    "var cl=(p.getAttribute('class')||'').toString();"
    "if(/dropdown-menu|dropdown-list|dropdown-content|listbox|"
    "select-dropdown|options-list|suggest|autocomplete/i.test(cl))return 1;}"
    "p=p.parentElement;d++;}"
    "return 0;}"
    # Виджет выбора — как vpcSf в _SNAPSHOT_JS
    "function vpcSf(e){if(e.tagName==='SELECT')return 1;"
    "var p=e,d=0;"
    "while(p&&p!==document.body&&d<12){"
    "if(p.getAttribute){"
    "if(p.getAttribute('role')==='combobox')return 1;"
    "var cl=(p.getAttribute('class')||'').toString();"
    "if(/(^|[_\\s-])(multiselect|v-select)|select-trigger|select__control|"
    "combobox/i.test(cl))return 1;}"
    "p=p.parentElement;d++;}"
    "return 0;}"
    "function vpcExt(e){try{return e.href&&(new URL(e.href)).host!==location.host?1:0;}catch(x){return 0;}}"
    "function info(e,tg){var b=e.getBoundingClientRect();"
    "var t=(e.innerText||e.value||e.getAttribute('aria-label')||e.title||'')"
    ".replace(/\\s+/g,' ').trim();"
    "var ctx='',p=e.parentElement,d=0;"
    "while(p&&d<6){var pt=(p.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(pt.length>t.length+8){"
    "if(pt.length>400){if(!ctx)ctx=pt;break;}"
    "ctx=pt;}"
    "p=p.parentElement;d++;}"
    "ctx=ctx.slice(0,160);"
    "return {tag:tg,role:e.getAttribute('role')||'',text:t.slice(0,80),ctx:ctx,"
    "aria:(e.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim().slice(0,80),"
    "title:(e.title||'').replace(/\\s+/g,' ').trim().slice(0,80),"
    "tid:(e.getAttribute('data-testid')||e.getAttribute('data-test-id')||'')"
    ".replace(/\\s+/g,' ').trim().slice(0,80),"
    "href:e.href||'',w:Math.round(b.width),h:Math.round(b.height),ed:0,"
    "md:vpcMd(e),dd:vpcDd(e),sf:vpcSf(e),ext:vpcExt(e),"
    "x:Math.round(b.left),"
    "vp:(b.bottom>0&&b.right>0&&b.top<window.innerHeight&&b.left<window.innerWidth)?1:0};}"
    # Общая нормализация цели (__vpcN — регистр/дефисы/апострофы/диакритика/
    # ё=е): «айс-ти»≈«Айс ти», «lumieres»≈«Lumière», «елка»≈«Ёлка»
    "goal=__vpcN(goal);"
    "var words=goal.split(' ').filter(function(w){return w.length>=3;});"
    # Слово совпадает с НАЧАЛА слова текста («айс» ≠ «гавАЙСкая»); длинные
    # слова — с усечением падежного окончания («додстера» → «додстер»).
    # Стем — ОДИН на проект: __vpcStem/__vpcWIn из _VPC_NORM_JS, собранные из
    # той же таблицы окончаний, что у питоновского web_search._stem (своя
    # копия wmatch жила здесь и расходилась с корзиной и с питоном)
    "function hits(own){if(goal.length>=3&&own.indexOf(goal)>=0)return 99;"
    "var h=0;for(var wi=0;wi<words.length;wi++){"
    "if(__vpcWIn(own,words[wi]))h++;}"
    "return h;}"
    "var mts=[];"
    "var all=document.querySelectorAll('*');"
    "for(i=0;i<all.length;i++){var e=all[i];"
    "var own='';"
    "for(var n=0;n<e.childNodes.length;n++){var c=e.childNodes[n];"
    "if(c.nodeType===3)own+=c.textContent;}"
    # Длинные подписи (название видео в плейлисте YouTube — 60+ символов)
    # дублируются в title-атрибуте: сумма текста и атрибутов улетала за
    # лимит 120, и строка плейлиста выпадала из кандидатов («троеточие в
    # sirene boss» не находило ряд вообще). Лимит — только на собственный
    # текст (отсекает огромные текстовые блоки страницы); aria/title
    # добавляем поверх, без ограничения суммы — на них живут подписи
    # иконочных кнопок («i» состава у dodo — чистый svg)
    "own=own.replace(/\\s+/g,' ').trim();"
    "if(own.length>160)continue;"
    # data-testid — крюк безтекстовых иконок (бургер «MobileHeader.Burger-
    # Button»): точки/подчёркивания → пробелы, иначе prefix-матч слова
    # («burger») внутрь dotted-токена не попадает
    "var dtid=(e.getAttribute('data-testid')||e.getAttribute('data-test-id')||'')"
    ".replace(/[-_.]/g,' ');"
    "own=__vpcN(own+' '+(e.getAttribute('aria-label')||'')+' '+(e.title||'')"
    "+' '+dtid);"
    "if(own.length<2)continue;"
    "var h=hits(own);if(h<1)continue;"
    "var r=e.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(e))continue;"
    # vp — во вьюпорте ли: у dodo каталог идёт в DOM РАНЬШЕ панели товара, и
    # без приоритета видимого «омлет сырный» цеплял карточку рекомендаций
    # (а не панель выбора, которую пользователь смотрит)
    "var ivp=(r.bottom>0&&r.right>0&&r.top<window.innerHeight&&r.left<window.innerWidth)?1:0;"
    # cov — перекрыт ли элемент другим в точке центра (открытая модалка
    # поверх ленты): портал модалки рендерится в КОНЕЦ body, и без этого
    # признака бюджет разметки съедали карточки каталога ПОД попапом (соусы
    # dodo) — «сырный соус» резолвился в основную ленту
    "var cov=0;"
    "if(ivp){var tp=null;try{tp=document.elementFromPoint("
    "r.left+r.width/2,r.top+r.height/2);}catch(x3){}"
    "if(tp&&tp!==e&&!e.contains(tp)&&!tp.contains(e))cov=1;}"
    "mts.push({e:e,h:h,vp:ivp,cov:cov,ow:own,md:vpcMd(e),dd:vpcDd(e)});}"
    "if(!mts.length)return '__none__';"
    # При равных хитах: модальный контекст и открытый список раньше (открытая
    # карточка товара / выпадашка — то, что пользователь видит; иначе «инфо»
    # уходило в футер «Правовая информация»); видимый неперекрытый раньше;
    # короткий текст раньше («Сырный» конкретнее заголовка модалки «Соусы к
    # бортикам и закускам»)
    "mts.sort(function(a,b){return b.h-a.h||a.cov-b.cov||b.md-a.md||b.dd-a.dd"
    "||b.vp-a.vp||a.ow.length-b.ow.length;});"
    # Точное фразовое совпадение (h=99) обесценивает словесные: «sirene boss»
    # даёт строку плейлиста фразой, а рекомендация «Sirene's theme» — одним
    # словом; без отсева размечались контролы ОБОИХ мест, и скоуп-клик
    # («троеточие в sirene boss») уходил в чужую карточку рекомендаций
    "if(mts[0].h>=99){mts=mts.filter(function(mm){return mm.h>=99;});}"
    "function mark(t,fb){"
    "if(!t||t.hasAttribute('data-vpc-gidx')||idx>=B+M)return;"
    "var inf=info(t,t.tagName.toLowerCase());"
    # Контрол, найденный от совпавшего текста (кнопка «49 ₽» рядом с
    # «Сырный»), подписываем самим совпадением — иначе шесть одинаковых
    # «49 ₽» модалки не различить ни скорингом, ни LLM
    "if(fb&&inf.text.toLowerCase().indexOf(fb.toLowerCase())<0){"
    "inf.text=(fb.slice(0,40)+(inf.text?' · '+inf.text:'')).slice(0,80);}"
    "if(!inf.text)return;"
    "t.setAttribute('data-vpc-gidx',idx);inf.idx=idx;out.push(inf);idx++;}"
    "var first=null;"
    "for(i=0;i<mts.length&&idx<B+M;i++){var e2=mts[i].e;"
    "var own2='';"
    "for(var n2=0;n2<e2.childNodes.length;n2++){var c2=e2.childNodes[n2];"
    "if(c2.nodeType===3)own2+=c2.textContent;}"
    "own2=own2.replace(/\\s+/g,' ').trim().slice(0,80);"
    "var t=null;"
    "try{if(e2.matches(sel))t=e2;}catch(x){}"
    "if(!t){try{t=e2.closest(sel);}catch(x2){}}"
    # Цель сама — кликабельный якорь без href (меню категорий dodo — «Кофе
    # и чай»): matches/closest(sel) его не видят (селектор требует a[href])
    "if(!t){var pa2=e2,up2=0;"
    "while(pa2&&up2<4){"
    "if(pa2.tagName==='A'&&getComputedStyle(pa2).cursor==='pointer'){t=pa2;break;}"
    "pa2=pa2.parentElement;up2++;}}"
    # Цель — кликабельный div/li/span с коротким текстом (пункт «Ещё» в меню
    # dodo — div с cursor:pointer, ни ссылки, ни кнопки): поднимаемся от
    # текстового элемента до pointer-предка. Срабатывает после селекторов,
    # так что настоящие ссылки/кнопки не перехватываются
    "if(!t){var pa3=e2,up3=0;"
    "while(pa3&&pa3.tagName!=='BODY'&&up3<4){"
    "if(getComputedStyle(pa3).cursor==='pointer'){"
    "var pt5=(pa3.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(pt5&&pt5.length<=60){t=pa3;break;}}"
    "pa3=pa3.parentElement;up3++;}}"
    "if(!t){var p=e2.parentElement,d2=0;"
    "while(p&&d2<6){var q=p.querySelector(sel);"
    "if(q){var qr=q.getBoundingClientRect();"
    "if(qr.width>=2&&qr.height>=2&&vpcVis(q)){t=q;break;}}"
    "p=p.parentElement;d2++;}}"
    "if(!t||t.hasAttribute('data-vpc-gidx'))continue;"
    "if(!first)first=t;"
    "mark(t,own2);"
    # Соседство: остальные контролы того же места (кнопка цены, пилюли
    # размера/теста — label с radio/checkbox внутри, клик нативный).
    # «Место» — НАИБОЛЬШИЙ предок цели с текстом ≤500 символов (выше уже
    # секция, а не место; минимум 10, чтобы не застрять на строке
    # заголовка). Ни селекторы карточек, ни число интерактивных не надёжны:
    # [class*=product] цепляет h3.product-title, счётчик ≥2 — строку-ряд с
    # иконками, а у dodo карточка это «Додстер от 169 ₽» (16 символов)
    "var cont=null,pa=(t||e2).parentElement,up=0;"
    "while(pa&&pa.tagName!=='BODY'&&up<8){"
    "var ct2=(pa.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(ct2.length>500)break;"
    "if(ct2.length>=10)cont=pa;"
    "pa=pa.parentElement;up++;}"
    "if(!cont)continue;"
    "var nb=cont.querySelectorAll(sel);"
    "for(var k=0;k<nb.length&&idx<B+M;k++){var ne=nb[k];"
    "var nr=ne.getBoundingClientRect();if(nr.width<2||nr.height<2)continue;"
    "if(!vpcVis(ne))continue;"
    "mark(ne);}"
    "var nl=cont.querySelectorAll('label');"
    "for(var k2=0;k2<nl.length&&idx<B+M;k2++){var lb=nl[k2];"
    "if(!lb.querySelector('input[type=radio],input[type=checkbox]'))continue;"
    "var lr=lb.getBoundingClientRect();if(lr.width<2||lr.height<2)continue;"
    "if(!vpcVis(lb))continue;"
    "mark(lb);}"
    # Псевдокнопки места: короткий текст + cursor:pointer (цена «от 385 ₽»
    # на dodo — span с React-обработчиком, не button и не ссылка; пункты
    # меню — <a> без href с тем же признаком)
    "var np=cont.querySelectorAll('span,div,a:not([href])');"
    "for(var k3=0;k3<np.length&&idx<B+M;k3++){var pe=np[k3];"
    "if(pe.closest('[data-vpc-gidx]'))continue;"
    # ...и предки уже размеченного (ряд-обёртка заголовка дублирует ссылку)
    "if(pe.querySelector('[data-vpc-gidx]'))continue;"
    "if(getComputedStyle(pe).cursor!=='pointer')continue;"
    "var pt3=(pe.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(!pt3||pt3.length>40)continue;"
    "var pr=pe.getBoundingClientRect();if(pr.width<2||pr.height<2)continue;"
    "if(!vpcVis(pe))continue;"
    "mark(pe);}}"
    "if(first){try{first.scrollIntoView({block:'center'});}catch(x4){}}"
    "return out.length?JSON.stringify({url:location.href,vw:window.innerWidth,items:out}):'__none__';"
    "})(__GOAL__)"
)


def snapshot_for_goal(host_part: Optional[str], goal: str,
                      tab_id: Optional[int] = None) -> Tuple[str, List[dict]]:
    """Целевой снапшот «места» под конкретную цель: элементы, чей текст
    совпадает с goal, плюс все контролы найденного места (карточка: кнопка
    цены, пилюли размера/теста) — где бы они ни были в DOM (общий снапшот
    режется бюджетом 100). Первое совпадение прокручивается во вьюпорт.
    → (url, items); совпадений нет — (url, []), это не ошибка.
    Пустая/мусорная цель — ('', []) без вызова страницы.
    Кавычки/бэкслеши цели больше не вырезаются — раньше это ломало матчинг
    товаров вроде «L'Oréal»/«Papa John's» (текст в JS переставал совпадать с
    текстом на странице); безопасность подстановки в JS теперь на _js_fill."""
    safe = _clean_goal_text(goal, 60)
    if not safe:
        return "", []
    raw = _run_js(host_part,
                  _js_fill(_GOAL_SNAPSHOT_JS, GOAL=safe,
                          BASE=_mark_base(GOAL_SNAPSHOT_MAX)),
                  tab_id=tab_id)
    if raw == "__none__":
        return "", []
    try:
        data = json.loads(raw)
        url = str(data.get("url") or "")
        raw_items = data.get("items") or []
    except (AttributeError, TypeError, ValueError):
        raise BrowserUnavailable(
            f"не разобрался целевой снапшот страницы: {str(raw)[:80]}")
    items: List[dict] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        try:
            items.append({
                "idx": int(it.get("idx")),
                "tag": str(it.get("tag") or ""),
                "role": str(it.get("role") or ""),
                "text": str(it.get("text") or ""),
                "ctx": str(it.get("ctx") or ""),
                "aria": str(it.get("aria") or ""),
                "title": str(it.get("title") or ""),
                "tid": str(it.get("tid") or ""),
                "href": str(it.get("href") or ""),
                "w": float(it.get("w") or 0),
                "h": float(it.get("h") or 0),
                "vp": bool(it.get("vp")),
                "ed": False,
                "md": bool(it.get("md")),
                "dd": bool(it.get("dd")),
                "sf": bool(it.get("sf")),
                "ext": bool(it.get("ext")),
            })
        except (TypeError, ValueError):
            continue
    return url, items


def snapshot_clickables(host_part: Optional[str] = None,
                        tab_id: Optional[int] = None) -> Tuple[str, str, str]:
    """Совместимая текстовая форма снапшота: (url, host, «idx|тег|текст»)."""
    url, host, items = snapshot_elements(host_part, tab_id=tab_id)
    return url, host, "\n".join(
        f"{it['idx']}|{it['tag']}|{it['text']}" for it in items)


# Подписи полей ввода, которые сейчас НЕ видимы (свёрнутое меню, закрытый
# попап): снапшот их отбрасывает по нулевому rect, и «введи X в поиск» честно
# отвечал «нет поля», хотя оно есть — просто спрятано. Для подсказки
# «поле есть, но скрыто — открой меню».
# IIFE-обёртка — как у всех прочих шаблонов (см. комментарий у _SNAPSHOT_JS):
# без неё var edsel/els/out объявлялись в ГЛОБАЛЬНОМ скоупе страницы, и на
# сайте с одноимённым `let` (или просто со своим `var out`/`var els` в другом
# скрипте) evaluate падал SyntaxError'ом — hidden_editable_labels молча ловил
# исключение и возвращал [] вместо честного списка скрытых полей.
_HIDDEN_EDITABLES_JS = (
    "(function(){"
    "var edsel='textarea,input:not([type]),input[type=text],input[type=search],"
    "input[type=email],input[type=tel],input[type=url],input[type=number],"
    "input[type=password],[role=textbox],[role=searchbox],[role=combobox],"
    "[contenteditable]:not([contenteditable=false])';"
    "var els=document.querySelectorAll(edsel),out=[];"
    "for(var i=0;i<els.length&&out.length<8;i++){var e=els[i];"
    "var r=e.getBoundingClientRect();"
    "var s=getComputedStyle(e);"
    "if(r.width>=2&&r.height>=2&&s.display!=='none'&&s.visibility!=='hidden'"
    "&&s.opacity!=='0')continue;"
    "var t=e.getAttribute('aria-label')||'';"
    "if(!t&&e.labels&&e.labels.length)t=e.labels[0].innerText||'';"
    "if(!t)t=e.getAttribute('placeholder')||'';"
    "if(!t)t=e.getAttribute('name')||'';"
    "t=t.replace(/\\s+/g,' ').trim().slice(0,60);"
    # Поисковость — отдельным флагом: placeholder скрытого поля может не
    # содержать слова «поиск» («Пишите полное название…» на ranobes)
    "var q=(e.type==='search'||/search|поиск/i.test("
    "(e.id||'')+' '+(e.getAttribute('class')||'')+' '+(e.getAttribute('name')||''))"
    ")?1:0;"
    "if(t)out.push({t:t,q:q});}"
    "return JSON.stringify(out);"
    "})()"
)


def hidden_editable_labels(host_part: Optional[str] = None,
                           tab_id: Optional[int] = None) -> List[dict]:
    """Скрытые поля ввода вкладки: [{t: подпись, q: 1 если поисковое}].
    Пустой список — нет скрытых полей или бэкенд недоступен (не исключение:
    это вспомогательная подсказка к честному отказу). eval_js — без
    выдёргивания вкладки на передний план."""
    try:
        raw = eval_js(host_part, tab_id, _HIDDEN_EDITABLES_JS)
        data = json.loads(raw or "[]")
        return [x for x in data if isinstance(x, dict) and x.get("t")][:8]
    except Exception:
        return []


# Авто-листание («промотай страницу»): плавная прокрутка анимацией внутри
# самой страницы (requestAnimationFrame), а не дискретные рывки извне.
# Запуск — один CDP-вызов, дальше страница крутится сама с постоянной
# скоростью, питон лишь изредка опрашивает состояние (scroll_status).
# «Стоп» — ещё один CDP-вызов, гасящий анимацию: страница замирает ровно
# там, где пользователь сказал «стоп», без долёта накопленных шагов.
# Скорость — от высоты окна (~1 экран за 9 сек: текст успевают читать),
# разгон плавный (~0.5 с), без резкого старта. Окно не скроллится (фиды
# ВК/чатов крутят внутренний контейнер) — крутим самый большой видимый
# скролл-контейнер. На бесконечных лентах (youtube) у дна ждём подгрузки
# до 2.5 с, лента подросла — крутим дальше, нет — done (конец листания).
_SCROLL_START_JS = (
    "(function(side,dir,name){"
    + _VPC_NORM_CORE_JS +
    "var up=dir==='up';"
    "var prev=window.__vpcScroll;"
    "if(prev&&prev.raf){cancelAnimationFrame(prev.raf);}"
    "var de=document.documentElement;"
    "var wmax=Math.max(de.scrollHeight,document.body?document.body.scrollHeight:0)"
    "-window.innerHeight;"
    "var target=null;"
    # «раздел слева/справа»: внутренняя прокручиваемая панель соответствующей
    # половины вьюпорта (у карточки товара dodo левая колонка — отдельный
    # скролл, окно её не крутит). Центр контейнера строго в своей половине —
    # BODY/ленту на всю ширину это отсекает
    "if(side){"
    "var half=window.innerWidth/2,sb=null,sm=0;"
    "var se=document.querySelectorAll('*');"
    "for(var si=0;si<se.length;si++){var e0=se[si];"
    "if(e0.scrollHeight<=e0.clientHeight+60||e0.clientHeight<200)continue;"
    # Обёртки-ленты на всю ширину — не боковая панель; и контейнер должен
    # реально скроллиться (overflow auto/scroll — иначе scrollTop молча не
    # двигается, как у div-ленты dodo, что объедает выбор по площади)
    "var r0=e0.getBoundingClientRect();"
    "if(r0.width>=window.innerWidth*0.9)continue;"
    "var ov0=getComputedStyle(e0).overflowY;"
    "if(ov0!=='auto'&&ov0!=='scroll')continue;"
    "if(r0.bottom<0||r0.top>window.innerHeight)continue;"
    "var cx=(r0.left+r0.right)/2;"
    "if(side==='left'&&cx>=half)continue;"
    "if(side==='right'&&cx<half)continue;"
    "var a0=e0.clientWidth*e0.clientHeight;"
    "if(a0>sm){sm=a0;sb=e0;}}"
    "if(!sb)return JSON.stringify({ok:false,bottom:false,side_missed:true});"
    "target=sb;}"
    # Именованный контейнер («пролистай комментарии»): прокручиваемый блок,
    # чьё имя совпадает по id/class/aria-label самого блока, предков
    # (ytd-comments#comments) или заголовку внутри («Комментарии» в шапке
    # панели). Приоритетнее дефолтного окна: пользователь назвал его явно
    "if(!side&&name){"
    "var names=name.split('|').map(function(x){return __vpcN(x);});"
    "var nm=null,nmScore=0;"
    "var ne=document.querySelectorAll('*');"
    "for(var ni=0;ni<ne.length;ni++){var e1=ne[ni];"
    "if(e1===document.body||e1===document.documentElement)continue;"
    "if(e1.scrollHeight<=e1.clientHeight+60||e1.clientHeight<150)continue;"
    "var ov1=getComputedStyle(e1).overflowY;"
    "if(ov1!=='auto'&&ov1!=='scroll')continue;"
    "var r1=e1.getBoundingClientRect();"
    "if(r1.bottom<0||r1.top>window.innerHeight)continue;"
    "var hay=(e1.tagName||'')+' '+(e1.id||'')+' '+"
    "(e1.getAttribute('aria-label')||'')+' '+(e1.getAttribute('class')||'');"
    # Предки: ytd-comments — тег, а не id/class; берём tagName тоже.
    # Заголовок панели («Комментарии») ищем от самого дальнего предка —
    # в самом скроллере его нет (он в шапке engagement-панели)
    "var p1=e1.parentElement,pd=0,top1=e1;"
    "while(p1&&pd<6){hay+=' '+p1.tagName+' '+(p1.id||'')+' '+"
    "(p1.getAttribute('aria-label')||'');top1=p1;p1=p1.parentElement;pd++;}"
    "var hd=top1.querySelector('#header h2,h2#title,[role=heading],"
    "h1,h2,h3,#title,#header');"
    "if(hd)hay+=' '+(hd.innerText||'');"
    "hay=__vpcN(hay);"
    "var hit=0;for(var nj=0;nj<names.length;nj++){"
    "if(names[nj]&&hay.indexOf(names[nj])>=0)hit++;}"
    "if(!hit)continue;"
    "var a1=e1.clientWidth*e1.clientHeight*(1+hit);"
    "if(a1>nmScore){nmScore=a1;nm=e1;}}"
    "if(!nm)return JSON.stringify({ok:false,bottom:false,name_missed:true});"
    "target=nm;}"
    # Без стороны: открытое всплывающее меню (настройки плеера YouTube,
    # dropdown) приоритетнее окна — «промотай» при открытом меню крутит
    # ЕГО, иначе меню настроек видео не листается вообще (страница под ним
    # скроллится, меню — нет)
    "if(!side&&!target){"
    "var pm=document.querySelectorAll('.ytp-popup,[role=menu],"
    "[role=listbox],.ytmusic-menu,[class*=popup-menu]');"
    "for(var pi=0;pi<pm.length;pi++){var pe=pm[pi];"
    "if(pe.scrollHeight<=pe.clientHeight+20)continue;"
    "var pr=pe.getBoundingClientRect();"
    "if(pr.width<40||pr.height<40)continue;"
    "if(pr.bottom<0||pr.top>window.innerHeight)continue;"
    "var ps=getComputedStyle(pe);"
    "if(ps.display==='none'||ps.visibility==='hidden'||ps.opacity==='0')"
    "continue;"
    "target=pe;break;}}"
    "if(!side&&!target&&wmax<=2){"
    "var best=null,bm=0,els=document.querySelectorAll('*');"
    "for(var i=0;i<els.length;i++){var e=els[i];"
    "if(e.scrollHeight>e.clientHeight+60&&e.clientHeight>200){"
    "var r=e.getBoundingClientRect();"
    "if(r.bottom<0||r.top>window.innerHeight)continue;"
    "var a=e.clientWidth*e.clientHeight;"
    "if(a>bm){bm=a;best=e;}}}"
    "if(!best)return JSON.stringify({ok:false,bottom:true});"
    "target=best;}"
    "function cur(){return target?target.scrollTop:window.scrollY;}"
    "function lim(){return target?target.scrollHeight-target.clientHeight:"
    "Math.max(document.documentElement.scrollHeight,"
    "document.body?document.body.scrollHeight:0)-window.innerHeight;}"
    "if(up){if(cur()<=2)return JSON.stringify({ok:false,bottom:true});}"
    "else if(lim()>0&&cur()>=lim()-2){"
    "return JSON.stringify({ok:false,bottom:true});}"
    "var S={raf:0,target:target,done:false,end:'',stall:0,vel:0,last:0,"
    "py:cur(),t0:0,speed:Math.max(60,window.innerHeight/9),up:up,acc:0};"
    "window.__vpcScroll=S;"
    "function tick(now){"
    "if(window.__vpcScroll!==S){return;}"
    "if(!S.t0)S.t0=now;"
    "if(!S.last){S.last=now;S.raf=requestAnimationFrame(tick);return;}"
    "var dt=Math.min(0.1,(now-S.last)/1000);S.last=now;"
    # Абсолютный потолок длительности: листание не должно жить вечно ни при
    # каком поведении страницы
    "if(now-S.t0>" + str(SCROLL_MAX_MS) + "){"
    "S.done=true;S.end='cap';S.raf=0;return;}"
    "S.vel=Math.min(S.speed,S.vel+S.speed*dt*2);"
    "S.acc+=(S.up?-1:1)*S.vel*dt;"
    # Перерисовка страницы — самая дорогая часть листания: scrollTo на каждый
    # rAF-кадр (60 репейнтов/с на тяжёлой странице вроде dodo грузил GPU и
    # ронял fps всей системы). Квантуем: двигаем скролл ступенями ~56px
    # (~2-3 репейнта/с) — видно то же листание, нагрузка на порядок ниже.
    "if(S.acc<56&&S.acc>-56){S.raf=requestAnimationFrame(tick);return;}"
    "var y=cur()+S.acc;S.acc=0;"
    "if(S.up&&y<0)y=0;"
    "if(S.target){S.target.scrollTop=y;}else{window.scrollTo(0,y);}"
    "if(S.up&&cur()<=0.5){S.done=true;S.end='edge';S.raf=0;return;}"
    # Завершение — по ЗАСТОЮ позиции, а не только по «легли на дно»:
    # контейнер мог перестать быть прокручиваемым (lim()<=0 — панель
    # свернули/перерисовали) или страница замерла. Прежняя ветка «дно»
    # сбрасывала застой в этом случае, и листание не кончалось никогда.
    # Застой меряем по часам кадра: dt-суммой 2.5 с набирались бы минуты
    "var L=lim();"
    "var stuck=Math.abs(cur()-S.py)<1||L<=0||(!S.up&&cur()>=L-2);"
    "S.py=cur();"
    "if(stuck){if(!S.stall)S.stall=now;"
    # Бесконечная лента у дна догружается — ждём 2.5 с, подросла → крутим
    "if(now-S.stall>2500){S.done=true;S.end='edge';S.raf=0;return;}}"
    "else{S.stall=0;}"
    "S.raf=requestAnimationFrame(tick);}"
    "S.raf=requestAnimationFrame(tick);"
    "return JSON.stringify({ok:true,bottom:false});"
    "})(__SIDE__,__DIR__,__NAME__)"
)
# «Стоп»: гасим анимацию немедленно — страница замирает на текущем месте.
_SCROLL_STOP_JS = (
    "(function(){"
    "var S=window.__vpcScroll;"
    "if(S&&S.raf){cancelAnimationFrame(S.raf);}"
    "window.__vpcScroll=null;"
    "return JSON.stringify({ok:true});"
    "})()"
)
# Опрос дозорным потоком: active — крутится, done — само дошло до конца.
# Ни того ни другого (страница ушла навигацией/перезагрузкой) — active:false.
_SCROLL_STATUS_JS = (
    "(function(){"
    "var S=window.__vpcScroll;"
    "if(!S){return JSON.stringify({active:false,done:false,end:''});}"
    "return JSON.stringify({active:!S.done,done:!!S.done,end:S.end||''});"
    "})()"
)


def scroll_start(host_part: Optional[str] = None,
                 tab_id: Optional[int] = None,
                 side: Optional[str] = None,
                 direction: Optional[str] = None,
                 name: Optional[str] = None) -> dict:
    """Запустить авто-листание страницы (анимация живёт в самой вкладке).
    Скролл движется ступенями ~56px, а не на каждый rAF-кадр — 60
    репейнтов/с «плавного» варианта грузили GPU и роняли fps всей системы.
    Окно браузера НЕ выдёргивается на передний план (front=False): листание
    идёт в фоне, иначе периодический опрос статуса вечно перехватывал бы
    фокус у чата («стоп» некуда написать). side='left'/'right' — листается
  внутренняя панель этой половины вьюпорта, а не окно. name — именованный
    контейнер («комментарии|comment»): листается названный блок (панель
    комментариев, чат), а не страница. → {"ok", "bottom",
    "side_missed", "name_missed"}; ok=False/bottom — страница уже у края
    (низ/верх) или скроллить нечего; side_missed — прокручиваемого раздела
    на этой стороне нет; name_missed — названного контейнера нет.
    direction='up' — листать вверх (по умолчанию вниз)."""
    raw = _run_js(host_part,
                  _js_fill(_SCROLL_START_JS, SIDE=side or "",
                          DIR=direction or "", NAME=name or ""),
                  tab_id=tab_id, front=False)
    try:
        res = json.loads(raw)
        return {"ok": bool(res.get("ok")), "bottom": bool(res.get("bottom")),
                "side_missed": bool(res.get("side_missed")),
                "name_missed": bool(res.get("name_missed"))}
    except (TypeError, ValueError, AttributeError):
        raise BrowserUnavailable(f"не разобрался ответ прокрутки: {raw[:80]}")


def scroll_stop(host_part: Optional[str] = None,
                tab_id: Optional[int] = None) -> None:
    """Мгновенно остановить листание (best effort: вкладка могла умереть)."""
    try:
        _run_js(host_part, _SCROLL_STOP_JS, tab_id=tab_id, front=False)
    except Exception:
        pass


def scroll_status(host_part: Optional[str] = None,
                  tab_id: Optional[int] = None) -> dict:
    """Состояние листания: {"active", "done", "end"}. done — само долистало
    до конца; end — чем именно кончилось ('edge' — край/застой позиции,
    'cap' — упёрлось в потолок длительности); active=False и done=False —
    страница ушла (навигация/перезагрузка).
    Опрашивается дозорным раз в ~секунду — строго без фронтинга окна."""
    raw = _run_js(host_part, _SCROLL_STATUS_JS, tab_id=tab_id, front=False)
    try:
        res = json.loads(raw)
        return {"active": bool(res.get("active")),
                "done": bool(res.get("done")),
                "end": str(res.get("end") or "")}
    except (TypeError, ValueError, AttributeError):
        raise BrowserUnavailable(f"не разобрался статус прокрутки: {raw[:80]}")


# ── Операции с корзиной сайта ─────────────────────────────
# «убери гавайскую из корзины», «убавь додстер», «прибавь колу»,
# «измени песто в корзине». Кнопки корзины (у dodo и подобных) — без текста
# и aria-label (× и пара −/+ в ряду с количеством), поэтому карточку товара
# находим по названию, а контрол выбираем по ПОЗИЦИИ: × — верх карточки,
# − и + — нижний ряд (левая/правая). Общий искатель карточки — _CART_FIND_JS;
# клик и closed-loop проверка — разными вызовами (между ними пауза на
# ре-рендер корзины).
_CART_FIND_JS = (
    _VPC_NORM_JS
    # Карточки товара: заголовок (свой текст с первым словом названия, с
    # начала слова) → ближайший предок с кнопкой и
    # текстом ≤500 (выше уже вся панель корзины); все слова названия должны
    # читаться в тексте карточки (уточнение «гавайскую 20 см»). Из найденных
    # выигрывают карточки, где все слова читаются в ЗАГОЛОВКЕ (первой строке):
    # «двойная пепперони» — название и одиночной пиццы, и строка в описании
    # состава комбо «3 пиццы …» — совпадение по описанию без приоритета
    # заголовка давало ложную неоднозначность из 3 карточек (кейс 07.09)
    + "function __vpcCards(prod){"
    "var words=__vpcN(prod).split(' ').filter(function(w){return w.length>=2;});"
    "if(!words.length)return [];"
    "var first=__vpcStem(words[0]);"
    "var out=[],all=document.querySelectorAll('*'),i,k;"
    "for(i=0;i<all.length;i++){var e=all[i];"
    "var own='';"
    "for(k=0;k<e.childNodes.length;k++){var c=e.childNodes[k];"
    "if(c.nodeType===3)own+=c.textContent;}"
    "own=__vpcN(own);"
    "if(own.length<2||own.length>60)continue;"
    "var ws=own.split(' '),hit=false;"
    "for(k=0;k<ws.length;k++){if(ws[k].indexOf(first)===0){hit=true;break;}}"
    "if(!hit)continue;"
    "var r=e.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "var p=e,card=null,up=0;"
    "while(p&&p.tagName!=='BODY'&&up<10){"
    "if(p.querySelector&&p.querySelector('button')){card=p;break;}"
    "p=p.parentElement;up++;}"
    "if(!card)continue;"
    # Подпись корзинной карточки: ≥2 кнопок (× и −/+) И ряд количества
    # «− N +» — у каталожных карточек и промо «Добавить к заказу?» его нет
    "if(card.querySelectorAll('button').length<2)continue;"
    "if(__vpcQty(card,__vpcCtrls(card))===null)continue;"
    "var ct=__vpcN(card.innerText);"
    "if(ct.length>500)continue;"
    "var ok=true;"
    "for(k=0;k<words.length;k++){if(!__vpcWIn(ct,words[k])){ok=false;break;}}"
    "if(!ok)continue;"
    "var dup=false;"
    "for(k=0;k<out.length;k++){if(out[k]===card){dup=true;break;}}"
    "if(!dup)out.push(card);}"
    "var titled=[],j,fl,ok2;"
    "for(i=0;i<out.length;i++){"
    "var lines=(out[i].innerText||'').split('\\n');fl='';"
    "for(j=0;j<lines.length;j++){fl=__vpcN(lines[j]);if(fl.length>=2)break;}"
    "if(fl.length<2)continue;"
    "ok2=true;"
    "for(j=0;j<words.length;j++){if(!__vpcWIn(fl,words[j])){ok2=false;break;}}"
    "if(ok2)titled.push(out[i]);}"
    "return titled.length?titled:out;}"
    # Контролы карточки: edit — «Изменить»; remove — кнопка верхнего ряда
    # (× в правом верхнем углу); qty — нижний ряд из 2+ кнопок: левая −, правая +
    "function __vpcCtrls(card){"
    "var edit=null,als=card.querySelectorAll('a,button'),i;"
    "for(i=0;i<als.length;i++){if(__vpcN(als[i].innerText)==='изменить'){edit=als[i];break;}}"
    "var list=[],btns=card.querySelectorAll('button');"
    "for(i=0;i<btns.length;i++){var b=btns[i];var r=b.getBoundingClientRect();"
    "if(r.width<2||r.height<2)continue;"
    "list.push({b:b,top:r.top,left:r.left});}"
    "var rem=null;"
    "for(i=0;i<list.length;i++){var it=list[i];"
    "if(edit&&it.b===edit)continue;"
    "if(!rem||it.top<rem.top-2||(Math.abs(it.top-rem.top)<=2&&it.left>rem.left))rem=it;}"
    "var pairs=[];"
    "for(i=0;i<list.length;i++){var it2=list[i];"
    "if(edit&&it2.b===edit)continue;pairs.push(it2);}"
    "pairs.sort(function(a,b2){return a.top-b2.top||a.left-b2.left;});"
    "var group=[];"
    "for(i=0;i<pairs.length;i++){"
    "if(group.length&&Math.abs(pairs[i].top-group[0].top)>4){"
    "if(group.length>=2)break;group=[];}"
    "group.push(pairs[i]);}"
    "if(group.length<2&&pairs.length>=2)group=pairs.slice(-2);"
    "group.sort(function(a,b2){return a.left-b2.left;});"
    "return {edit:edit,remove:rem?rem.b:null,dec:group.length>=2?group[0].b:null,"
    "inc:group.length>=2?group[group.length-1].b:null};}"
    # Ряд количества «− N +»: чисто-числовой элемент МЕЖДУ двумя кнопками
    # одного ряда. Это сигнатура позиции корзины — у каталожной карточки
    # и промо-ряда такого нет, по нему корзину отличаем от каталога
    "function __vpcQty(card,c){"
    "if(!c.dec||!c.inc)return null;"
    "var dr=c.dec.getBoundingClientRect(),ir=c.inc.getBoundingClientRect();"
    "var els=card.querySelectorAll('span,div');"
    "for(var i=0;i<els.length;i++){var e=els[i];"
    "var t=__vpcN(e.innerText);"
    "if(!/^\\d{1,2}$/.test(t))continue;"
    "var r=e.getBoundingClientRect();"
    "if(Math.abs(r.top-dr.top)>6)continue;"
    "if(r.left>dr.left&&r.left<ir.left)return parseInt(t,10);}"
    "return null;}"
)

_CART_CLICK_JS = (
    "(function(prod,op){"
    + _CART_FIND_JS +
    "var cards=__vpcCards(prod);"
    "if(!cards.length)return 'err:не вижу в корзине «'+prod+'» на этой странице';"
    "if(cards.length>1){var vs=[];"
    "for(var v=0;v<cards.length&&v<4;v++){"
    "vs.push(__vpcN(cards[v].innerText).slice(0,40));}"
    # JSON, а не vs.join('|')/split('|') на стороне питона — текст карточки
    # («О! 1+1=3») сам может содержать «|», и склейка/резка тогда рвёт список
    "return 'amb:'+JSON.stringify(vs);}"
    "var c=__vpcCtrls(cards[0]);"
    "var t=(op==='edit')?c.edit:(op==='remove')?c.remove:"
    "(op==='decrease')?c.dec:c.inc;"
    "if(!t)return 'err:у «'+prod+'» нет такой кнопки в корзине';"
    "t.click();return 'ok:clicked';"
    "})(__PROD__,__OP__)"
)

_CART_VERIFY_JS = (
    "(function(prod){"
    + _CART_FIND_JS +
    "var cards=__vpcCards(prod);"
    "if(!cards.length)return JSON.stringify({present:false,qty:null});"
    "var qty=__vpcQty(cards[0],__vpcCtrls(cards[0]));"
    "return JSON.stringify({present:true,qty:qty});"
    "})(__PROD__)"
)


def cart_op(host_part: Optional[str], product: str, op: str,
            tab_id: Optional[int] = None) -> dict:
    """Операция с корзиной сайта: op ∈ remove|decrease|increase|edit.
    Клик детерминированный (позиция контрола в карточке товара), затем
    closed-loop проверка эффекта: remove — товар исчез из корзины;
    decrease/increase — новое количество (decrease при 1 шт убирает товар —
    qty=0). → {"status": "ok", "qty": int|None}; неоднозначность (несколько
    похожих карточек) и промах — BrowserUnavailable с человеческим текстом."""
    prod = _clean_goal_text(product, 80)
    if not prod:
        raise BrowserUnavailable("пустое название товара")
    if op not in ("remove", "decrease", "increase", "edit"):
        raise BrowserUnavailable(f"неизвестная операция с корзиной: {op}")
    raw = _run_js(host_part,
                  _js_fill(_CART_CLICK_JS, PROD=prod, OP=op),
                  tab_id=tab_id, front=False)
    if raw.startswith("amb:"):
        try:
            variants = [str(v) for v in json.loads(raw[4:]) if v]
        except (ValueError, TypeError):
            variants = []
        raise BrowserUnavailable(
            "в корзине несколько похожих: "
            + "; ".join(variants) + " — уточни, какую именно")
    if raw.startswith("err:"):
        raise BrowserUnavailable(raw[4:])
    if not raw.startswith("ok:"):
        raise BrowserUnavailable(f"не разобрался ответ корзины: {raw[:80]}")
    time.sleep(0.6)  # ре-рендер корзины после клика
    qty = None
    try:
        ver = json.loads(_run_js(
            host_part, _js_fill(_CART_VERIFY_JS, PROD=prod),
            tab_id=tab_id, front=False) or "{}")
        if op == "remove" and ver.get("present"):
            raise BrowserUnavailable(
                f"«{prod}» всё ещё в корзине — клик не сработал")
        if op in ("decrease", "increase"):
            qty = ver.get("qty")
            if op == "decrease" and not ver.get("present"):
                qty = 0  # минус при количестве 1 убрал товар совсем
    except BrowserUnavailable:
        raise
    except Exception:
        pass  # клик уже сработал — отчёт без числа не страшен
    return {"status": "ok", "qty": qty}


def cart_item_present(host_part: Optional[str], product: str,
                      tab_id: Optional[int] = None) -> bool:
    """Товар виден в открытой корзине страницы (тот же искатель карточки,
    что у cart_op — по названию и сигнатуре ряда количества). Только проба
    наличия, кликов не делает: резолвер ею решает, уводить ли голую фразу
    «удали X»/«закрой X» в корзинное удаление или отвечать «не нашёл»."""
    prod = _clean_goal_text(product, 80)
    if not prod:
        return False
    try:
        ver = json.loads(_run_js(
            host_part, _js_fill(_CART_VERIFY_JS, PROD=prod),
            tab_id=tab_id, front=False) or "{}")
        return bool(ver.get("present"))
    except Exception:
        return False


# ── Редактор состава на странице/модалке продукта ──
# Комбо-страницы dodo: у каждого выбранного товара своя ссылка «Изменить
# состав», а имя товара живёт соседним блоком — ctx снапшота его не
# захватывает, и скоуп-скоринг «изменить состав в гавайская» промахивался
# в инфо-иконку (кейс 07.09). Контрол находим по тексту «изменить состав»,
# товар — по контексту предка (до 8 уровней, первый содержательный текст).
_COMP_EDIT_CORE_JS = (
    "var cand=[],els=document.querySelectorAll('a,button,[role=button]'),i;"
    "for(i=0;i<els.length;i++){"
    "var t=__vpcN(els[i].innerText);"
    "if(t.indexOf('изменить состав')!==0)continue;"
    "var r=els[i].getBoundingClientRect();"
    "if(r.width<2||r.height<2)continue;cand.push(els[i]);}"
    "function cx(el){var p=el,up=0;while(p&&p.tagName!=='BODY'&&up<8){"
    "var t=__vpcN(p.innerText);if(t.length>25&&t.length<=400)return t;"
    "p=p.parentElement;up++;}return __vpcN(el.innerText);}"
    "function hits(pr){var words=__vpcN(pr).split(' ').filter(function(w){"
    "return w.length>=2;});var out=[],k,j;"
    "for(k=0;k<cand.length;k++){var ct=cx(cand[k]),ok=true;"
    "for(j=0;j<words.length;j++){if(!__vpcWIn(ct,words[j])){ok=false;break;}}"
    "if(ok)out.push(k);}return out;}"
    "function vlist(ix){var vs=[];for(var k=0;k<ix.length&&k<4;k++){"
    "vs.push(cx(cand[ix[k]]).slice(0,40));}return vs;}"
)

_COMP_EDIT_FIND_JS = (
    "(function(prod){"
    + _VPC_NORM_JS + _COMP_EDIT_CORE_JS +
    "if(!cand.length)return '{\"status\":\"none\"}';"
    "if(!prod){return cand.length===1?'{\"status\":\"unique\"}':"
    "JSON.stringify({status:'multi',variants:vlist("
    "cand.map(function(_,k){return k;}))});}"
    "var h=hits(prod);"
    "if(h.length===1)return '{\"status\":\"unique\"}';"
    "if(!h.length)return '{\"status\":\"none\"}';"
    "return JSON.stringify({status:'multi',variants:vlist(h)});"
    "})(__PROD__)"
)

_COMP_EDIT_CLICK_JS = (
    "(function(prod){"
    + _VPC_NORM_JS + _COMP_EDIT_CORE_JS +
    "var el=null;"
    "if(!prod){el=cand.length===1?cand[0]:null;}"
    "else{var h=hits(prod);el=h.length===1?cand[h[0]]:null;}"
    "if(!el)return 'err:нет единственной «Изменить состав» для «'+prod+'»';"
    "el.click();return 'ok:clicked';"
    "})(__PROD__)"
)


def _comp_edit_prod(product: str) -> str:
    return _clean_goal_text(product, 80)


def edit_composition_find(host_part: Optional[str], product: str,
                          tab_id: Optional[int] = None) -> dict:
    """Проба резолвера: есть ли на странице/в модалке продукта ровно один
    подходящий контрол «Изменить состав» (для названного товара — в его
    слоте). → {"status": "unique"|"none"|"multi", "variants": [...]};
    {} — страница недоступна."""
    try:
        return json.loads(_run_js(
            host_part,
            _js_fill(_COMP_EDIT_FIND_JS, PROD=_comp_edit_prod(product)),
            tab_id=tab_id, front=False) or "{}")
    except Exception:
        return {}


def edit_composition_op(host_part: Optional[str], product: str,
                        tab_id: Optional[int] = None) -> None:
    """Клик по единственной подходящей ссылке «Изменить состав» (слот товара
    на странице/в модалке продукта). Промах/неоднозначность —
    BrowserUnavailable с человеческим текстом."""
    raw = _run_js(host_part,
                  _js_fill(_COMP_EDIT_CLICK_JS,
                          PROD=_comp_edit_prod(product)),
                  tab_id=tab_id, front=False)
    if raw.startswith("err:"):
        raise BrowserUnavailable(raw[4:])
    if not raw.startswith("ok:"):
        raise BrowserUnavailable(f"не разобрался ответ страницы: {raw[:80]}")


def click_tagged(host_part: Optional[str], idx: int,
                 tab_id: Optional[int] = None) -> str:
    """Клик по элементу с номером разметки из снапшота этой вкладки (общего
    или целевого — номера сквозные, знать какого не нужно).
    CDP: настоящий playwright-клик (скролл, actionability) с фолбэком на
    force; затем closed-loop проверка эффекта (п.6) — нет изменений за
    CLICK_VERIFY_SEC → ClickUncertain («не уверен, что сработало»), это
    отдельный класс ошибок от «элемент не найден»."""
    if _select_backend(tab_op=True) == "cdp":
        return _WORKER.submit(
            lambda w: _click_cdp(w, host_part, idx, tab_id))
    return _click_applescript(host_part, idx, tab_id)


def _locator_any_frame(page, idx: int):
    """Локатор элемента по номеру разметки: главный фрейм, затем видимые
    iframe'ы (снапшот обходит их, у каждого свой блок номеров). Номер
    сквозной — совпасть может только один элемент, где бы он ни жил и каким
    бы снапшотом (общим/целевым) ни был помечен.
    → (locator, scope) где scope — page или frame (для closed-loop
    отпечатка); (None, None) — элемент не нашёлся нигде."""
    sel = _mark_sel(idx)
    try:
        loc = page.locator(sel)
        if loc.count() > 0:
            return loc, page
    except Exception:
        pass
    try:
        frames = [f for f in page.frames if f != page.main_frame]
    except Exception:
        frames = []
    for fr in frames:
        try:
            loc = fr.locator(sel)
            if loc.count() > 0:
                return loc, fr
        except Exception:
            continue  # фрейм в переходе между документами
    return None, None


def _click_cdp(w: _CdpWorker, host_part: Optional[str], idx: int,
               tab_id: Optional[int]) -> str:
    page = w.page_for(host_part, tab_id)
    loc, scope = _locator_any_frame(page, idx)
    if loc is None:
        raise BrowserUnavailable("элемент потерян — страница изменилась")
    # Попап (новое окно/вкладка от клика — вход в аккаунт и т.п.) саму страницу
    # не меняет: счётчик страниц в отпечатке, иначе честный клик по «Войти»
    # выглядел бы как «не сработало». Отпечаток — по ТОМУ фрейму, где жил
    # элемент: клик внутри iframe не меняет DOM главного фрейма
    def _st() -> _Probe:
        return _page_state(scope, aux=f"tabs:{len(w._all_pages())}")

    pre = _st()
    try:
        loc.first.click(timeout=CLICK_TIMEOUT_MS)
    except Exception:
        # Элемент перекрыт/не стабилен — кликаем принудительно (без
        # actionability-проверок playwright)
        try:
            loc.first.click(force=True, timeout=CLICK_TIMEOUT_MS)
        except Exception as e:
            detail = str(e).split("Call log")[0].strip().split("\n")[0]
            raise BrowserUnavailable(f"клик не выполнен: {detail[:120]}")
    logger.info(f"[BrowserActions] Клик idx={idx} "
                f"({host_part or f'вкладка #{tab_id}' if tab_id else 'активная'})")
    verdict = _wait_effect(_st, pre)
    if verdict == EFFECT_CHANGED:
        return "clicked"
    # FAQ-аккордеоны/тогглы — label+checkbox (dodo): клик по внутреннему div
    # заголовка проходит мимо label-механики (событие не активирует контрол).
    # Перещёлкиваем сам input: аккордеон раскрывается CSS :checked — DOM
    # не меняется, поэтому доказательство — сам факт перещёлкивания
    toggled = _label_toggle_js(scope, idx)
    if toggled in ("flipped", "already"):
        logger.info(f"[BrowserActions] Клик idx={idx} — через label/input "
                    f"({toggled})")
        return "clicked"
    if toggled == "label":
        verdict = _wait_effect(_st, pre)
        if verdict == EFFECT_CHANGED:
            logger.info(f"[BrowserActions] Клик idx={idx} — через label")
            return "clicked"
    raise _uncertain(verdict, "клик отправлен")


def _label_toggle_js(scope, idx: int) -> Optional[str]:
    """Фолбэк клика для label-обёрток: элемент с номером разметки
    внутри <label> с checkbox/radio — дёргаем сам контрол (input.click() —
    trusted-семантика перещёлкивания + события для React). → 'flipped'
    (checked перещёлкнулся — само по себе доказательство: аккордеон
    открывается CSS :checked и DOM-отпечаток не меняется), 'already' (уже
    был включён — обратно не перещёлкиваем: «нажми вопрос» ≠ «закрой его»),
    'label' (input нет, кликнули label — проверять отпечатком вызывающему),
    None/'stuck' — не label-конструкция или контрол не поддался."""
    js = ("(function(){var e=document.querySelector('" + _mark_sel(idx)
          + "');if(!e)return '';"
          "var l=e.closest?e.closest('label'):null;if(!l)return '';"
          "var i=l.querySelector('input[type=checkbox],input[type=radio]');"
          "if(i){var b=!!i.checked;"
          # Уже открыт/включён — не перещёлкиваем обратно: «нажми вопрос
          # аккордеона» значит «хочу видеть раскрытым», а не тоггл туда-сюда
          "if(b)return 'already';"
          "i.click();return i.checked!==b?'flipped':'stuck';}"
          "l.click();return 'label';})()")
    try:
        return str(scope.evaluate(js) or "") or None
    except Exception:
        return None


def _click_applescript(host_part: Optional[str], idx: int,
                       tab_id: Optional[int]) -> str:
    """JS-клик по номеру разметки (_mark_find_js — общая и целевая разметка
    разом) + та же closed-loop проверка состояния (та же трёхзначная модель:
    неудавшийся Apple-Events замер — не подтверждение клика)."""
    def _state() -> _Probe:
        try:
            return _probe_of(
                _run_apple_events(host_part, _DOM_STATE_JS, tab_id=tab_id))
        except BrowserUnavailable:
            return _Probe(False, "", "", "")

    js = ("var d=document.documentElement;"
          + _mark_find_js(idx) +
          "if(el){el.scrollIntoView({block:'center'});el.click();d.setAttribute('data-vpc-res','ok:clicked');}"
          "else{d.setAttribute('data-vpc-res','элемент потерян — страница изменилась');}"
          "d.getAttribute('data-vpc-res')")
    pre = _state()
    out = _run_apple_events(host_part, js, tab_id=tab_id)
    if not out.startswith("ok:"):
        raise BrowserUnavailable(out or "клик не выполнен")
    logger.info(f"[BrowserActions] Клик idx={idx} "
                f"({host_part or f'вкладка #{tab_id}' if tab_id else 'активная'})")
    verdict = _wait_effect(_state, pre)
    if verdict == EFFECT_CHANGED:
        return out[3:]
    # label+checkbox/radio (FAQ-аккордеоны dodo): клик по внутреннему div мимо
    # label-механики — перещёлкиваем сам контрол; 'flipped' — checked
    # перещёлкнулся, это само по себе доказательство (CSS :checked без
    # изменения DOM-отпечатка)
    toggle = (_mark_find_js(idx, var="e") +
              "if(!e){''}else{var l=e.closest?e.closest('label'):null;"
              "if(!l){''}else{var inp=l.querySelector('input[type=checkbox],input[type=radio]');"
              "if(inp){var b=!!inp.checked;if(b){'already'}else{inp.click();inp.checked!==b?'flipped':'stuck'}}"
              "else{l.click();'label'}}}")
    tres = _run_apple_events(host_part, toggle, tab_id=tab_id)
    if tres in ("flipped", "already"):
        return out[3:]
    if tres == "label":
        verdict = _wait_effect(_state, pre)
        if verdict == EFFECT_CHANGED:
            return out[3:]
    raise _uncertain(verdict, "клик отправлен")


# Зональный обзор для vision-фолбэка (когда DOM-снапшот пуст/беден:
# canvas/WebGL, ARIA-скрытая разметка): ВСЕ визуально кликабельные зоны
# вьюпорта, включая безымянные. Крупный canvas (>40% вьюпорта) режется
# сеткой 3×3 — иначе «кликни по врагу» в игре не адресуемо. Зоны без
# DOM-метки кликаются по координатам (click_at_point).
# Подпись зоны — цепочка: свой aria/текст → alt картинки → aria потомка →
# title/placeholder → текст РОДИТЕЛЯ (безымянные иконки ×/−/+ в карточке
# товара, миниатюры shorts: название — соседний блок в том же контейнере);
# собственный текст-бейдж вида «0:28» уступает подписи родителя. Близнецы
# с совпадающим rect (два button-слайда баннера) схлопываются в одну зону,
# мелочь <8px (точки слайдера) не даёт зон — по ней не попасть координатой
_ALL_CLICKABLE_BOXES_JS = (
    "(function(){"
    "var sel='button,a,[role=button],[role=link],input,textarea,select,"
    "summary,[onclick],[tabindex]:not([tabindex=\"-1\"]),canvas,"
    "[contenteditable]:not([contenteditable=false])';"
    # Открытые попапы/меню/диалоги: их пункты в DOM идут ПОСЛЕДНИМИ (портал в
    # конец body) и бюджет 40 зон съедает лента раньше — меню «Ещё» на ютубе
    # оставалось без разметки. А это то, что пользователь видит поверх прямо
    # сейчас — собираем их первыми (стабильная сортировка: DOM-порядок внутри
    # групп сохраняется)
    "var popSel='[role=menu],[role=listbox],[role=dialog],[aria-modal=true],"
    "[class*=popup],[class*=Popup],[class*=dropdown],[class*=Dropdown]';"
    "function vis(e,r){var s=getComputedStyle(e);"
    "return s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'"
    "&&r.width>=8&&r.height>=8&&r.bottom>0&&r.right>0"
    "&&r.top<innerHeight&&r.left<innerWidth;}"
    "function norm(s){return (s||'').replace(/\\s+/g,' ').trim();}"
    "function nm(e){"
    "var t=norm(e.getAttribute('aria-label'))||norm(e.innerText)||"
    "norm(e.value);"
    "if(/^\\d{1,2}:\\d{2}$/.test(t))t='';"
    "if(!t){var im=e.querySelector('img');if(im)t=norm(im.alt);}"
    "if(!t){var ca=e.querySelector('[aria-label]');"
    "if(ca)t=norm(ca.getAttribute('aria-label'));}"
    "if(!t)t=norm(e.title)||norm(e.getAttribute('placeholder'));"
    "if(!t){var p=e.parentElement,up=0;"
    "while(p&&up<4){var pt=norm(p.innerText);"
    "if(pt.length>=3&&pt.length<=400){t=pt;break;}"
    "p=p.parentElement;up++;}}"
    "return t;}"
    "var out=[];"
    "var all=[].slice.call(document.querySelectorAll(sel));"
    # Активный слой поверх затемнённого фона (панель комментариев, модалка
    # с бэкдропом, шторка поверх корзины dodo): бэкдроп-подобный элемент в
    # стеке центра вьюпорта (fixed/absolute, ≥60% вьюпорта, полупрозрачная
    # заливка 0<α<0.98; прозрачные fixed-обёртки виджетов — не в счёт) →
    # размечаем только элементы, не покрытые в своём центре (кейс 08.09: 33
    # зоны размазаны по затемнённой странице, на комментарии не хватало
    # бюджета 40). Покрытие, а не вложенность в «слой над бэкдропом»: слои
    # бывают вложенными (у шторки соусов свой оверлей поверх корзины) и
    # боковыми (корзина центр не перекрывает). Canvas не режем — игровые
    # оверлеи не должны прятать поле. Слой применяем, только если он
    # реально сужает разметку и в нём ≥2 кликабельных (страховка от ложного
    # детекта)
    "var bdEl=null;try{"
    "var stk=document.elementsFromPoint(innerWidth/2,innerHeight/2);"
    "for(var bi=0;bi<stk.length;bi++){var bd=stk[bi];"
    "if(bd===document.body||bd===document.documentElement)break;"
    "var br=bd.getBoundingClientRect();"
    "if(br.width<innerWidth*0.6||br.height<innerHeight*0.6)continue;"
    "var bs2=getComputedStyle(bd);"
    "if(bs2.position!=='fixed'&&bs2.position!=='absolute')continue;"
    "var bm=(bs2.backgroundColor||'').match(/rgba\\(([^)]+)\\)/);"
    "if(!bm)continue;"
    "var bal=parseFloat(bm[1].split(',')[3]);"
    "if(!(bal>0&&bal<0.98))continue;"
    "bdEl=bd;break;}"
    "}catch(x){}"
    "if(bdEl){var bdIn=all.filter(function(e){"
    "if(e.tagName==='CANVAS')return true;"
    "var cr=e.getBoundingClientRect();"
    "var cx=cr.left+cr.width/2,cy=cr.top+cr.height/2;"
    "if(cx<0||cy<0||cx>=innerWidth||cy>=innerHeight)return false;"
    "var tp=null;try{tp=document.elementFromPoint(cx,cy);}catch(x3){}"
    "return tp&&(e===tp||e.contains(tp)||tp.contains(e));});"
    "if(bdIn.length>=2&&bdIn.length<all.length)all=bdIn;}"
    "all.sort(function(a,b){"
    "return (a.closest(popSel)?0:1)-(b.closest(popSel)?0:1);});"
    # Зона отдаётся ОБРЕЗАННОЙ по вьюпорту: кликаем мы её геометрический
    # центр, а у наполовину уехавшего за край элемента центр сырого rect
    # оказывается за экраном (mouse.click(-90, y) — мимо страницы). Видимый
    # остаток меньше __MIN__ px по стороне — зоны нет: по ней не попасть
    "function put(x,y,w,h,text){"
    "var L=Math.max(0,x),T=Math.max(0,y),"
    "R=Math.min(innerWidth,x+w),B=Math.min(innerHeight,y+h);"
    "if(R-L<__MIN__||B-T<__MIN__)return;"
    "out.push({x:L,y:T,w:R-L,h:B-T,text:text});}"
    "all.forEach(function(e){"
    "var r=e.getBoundingClientRect();if(!vis(e,r))return;"
    "var t=nm(e);"
    "if(e.tagName==='CANVAS'&&r.width*r.height>"
    "innerWidth*innerHeight*0.4){"
    # Крупный canvas — сетка 3×3: зона = ячейка (клик по координатам центра)
    "for(var gy=0;gy<3;gy++)for(var gx=0;gx<3;gx++){"
    "put(r.left+gx*r.width/3,r.top+gy*r.height/3,"
    "r.width/3,r.height/3,"
    "(t||'canvas')+' — сектор '+(gy*3+gx+1));}"
    "return;}"
    "put(r.left,r.top,r.width,r.height,t.slice(0,40));"
    "});"
    "var ded=[];"
    "for(var i=0;i<out.length;i++){var b=out[i],dup=false;"
    "for(var j=0;j<ded.length;j++){var c=ded[j];"
    "if(Math.abs(b.x-c.x)<=4&&Math.abs(b.y-c.y)<=4&&"
    "Math.abs(b.w-c.w)<=6&&Math.abs(b.h-c.h)<=6){dup=true;break;}}"
    "if(!dup)ded.push(b);}"
    # «Служебные» строки метаданных (просмотры/подписчики/даты/лайки) почти
    # целиком внутри бо́льшей рамки карточки — шум: на одну карточку приходится
    # обёртка + заголовок + такие строки, и они конкурируют за номера рамок
    # (кейс 08.09: vision на ютубе получал тонкие «N просмотров» вместо
    # заголовка). Срезаем ДО бюджета 40 зон. svc — общий признак со
    # снапшотом (_SVC_FN_JS, словарь расширять там)
    + _SVC_FN_JS +
    # Зона выкидывается, если «служебная» И ≥85% её площади внутри зоны
    # вдвое больше (0.85/2 — пороги вложенности, тюнинг под плотность
    # разметки; самостоятельная кнопка-цена вне кликабельной карточки
    # выживает)
    "var ded2=[];"
    "for(var i2=0;i2<ded.length;i2++){var b2=ded[i2],buried=false;"
    "if(svc(b2.text)){var ba2=b2.w*b2.h;"
    "for(var j2=0;j2<ded.length;j2++){if(i2===j2)continue;var c2=ded[j2];"
    "if(c2.w*c2.h<ba2*2)continue;"
    "var ix=Math.max(0,Math.min(b2.x+b2.w,c2.x+c2.w)-Math.max(b2.x,c2.x)),"
    "iy=Math.max(0,Math.min(b2.y+b2.h,c2.y+c2.h)-Math.max(b2.y,c2.y));"
    "if(ba2>0&&ix*iy>=ba2*0.85){buried=true;break;}}}"
    "if(!buried)ded2.push(b2);}"
    "return JSON.stringify(ded2.slice(0,40));})()"
)


def all_clickable_boxes(host_part: Optional[str] = None,
                        tab_id: Optional[int] = None) -> List[dict]:
    """Все визуально кликабельные зоны вьюпорта (для зонального
    vision-фолбэка, когда текстовый скоринг не нашёл ничего): x/y/w/h в
    CSS-пикселях вьюпорта + короткая подпись. Пусто — бэкенд недоступен."""
    try:
        raw = _eval_js_any(host_part, tab_id,
                           _js_fill(_ALL_CLICKABLE_BOXES_JS, MIN=POINT_MIN_PX))
        return [b for b in json.loads(raw or "[]") if isinstance(b, dict)]
    except Exception:
        return []


def _viewport_size(page) -> Tuple[float, float]:
    """Размер вьюпорта в CSS-пикселях; (0, 0) — не удалось узнать."""
    try:
        raw = str(page.evaluate("innerWidth+'x'+innerHeight") or "")
        w_s, _sep, h_s = raw.partition("x")
        return float(w_s), float(h_s)
    except Exception:
        return 0.0, 0.0


def _check_point(page, x, y) -> Tuple[float, float]:
    """Валидация координат клика/наведения: точка обязана лежать во
    вьюпорте. Центр частично видимой зоны мог оказаться за краем экрана
    (mouse.click(-90, y) уходил в никуда, а closed-loop потом рапортовал
    «не уверен» вместо честного «по этой точке не попасть»)."""
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        raise BrowserUnavailable("координаты клика не разобрались")
    if x != x or y != y or x in (float("inf"), float("-inf")) \
            or y in (float("inf"), float("-inf")):
        raise BrowserUnavailable("координаты клика не разобрались")
    vw, vh = _viewport_size(page)
    if vw > 0 and vh > 0 and not (0 <= x <= vw - 1 and 0 <= y <= vh - 1):
        raise BrowserUnavailable(
            f"точка ({int(x)}, {int(y)}) вне видимой области экрана "
            f"({int(vw)}×{int(vh)}) — по ней не попасть")
    return x, y


def _click_point_cdp(w: "_CdpWorker", host_part: Optional[str],
                     x: float, y: float, tab_id: Optional[int]) -> str:
    page = w.page_for(host_part, tab_id)
    x, y = _check_point(page, x, y)
    pre = _page_state(page)
    page.mouse.click(x, y)
    _verify_effect(lambda: _page_state(page), pre,
                   "клик по координатам отправлен")
    return "clicked"


def click_at_point(host_part: Optional[str], x: float, y: float,
                   tab_id: Optional[int] = None) -> str:
    """Клик по координатам вьюпорта (CSS px) — зона vision-фолбэка без
    DOM-метки (canvas/WebGL). Только CDP (AppleScript-мост координатный
    клик не умеет). Closed-loop обязателен: текстового подтверждения,
    что нажали именно цель, у координатного клика нет."""
    if _select_backend(tab_op=True) != "cdp":
        raise _no_backend("клик по координатам")
    return _WORKER.submit(
        lambda w: _click_point_cdp(w, host_part, x, y, tab_id))


# «наведи (курсор) на X» — hover без клика: раскрыть hover-меню, hover-
# кнопки карточки (очередь/«посмотреть позже»), свёрнутый слайдер
# громкости. Только реальное движение мыши (playwright hover/mouse.move →
# CDP-ввод): синтетический dispatchEvent CSS :hover не включает, поэтому
# AppleScript-ветки нет — там честный BrowserUnavailable
# Шаблон — ФУНКЦИЯ (не IIFE): evaluate доставляет селектор аргументом
# только функции; у вызванного выражения аргумент теряется молча, селектор
# приезжал undefined и проверка всегда говорила 'gone'
_HOVER_CHECK_JS = (
    "(sel)=>{"
    "var e=document.querySelector(sel);"
    "if(!e)return 'gone';"  # DOM перерисовался от наведения (метка снялась)
    "try{if(e.matches(':hover'))return 'hover';}catch(x){}"
    "return '';}"
)


def _hover_cdp(w: "_CdpWorker", host_part: Optional[str], idx: int,
               tab_id: Optional[int]) -> str:
    page = w.page_for(host_part, tab_id)
    loc, scope = _locator_any_frame(page, idx)
    if loc is None:
        raise BrowserUnavailable("элемент потерян — страница изменилась")
    pre = _page_state(scope)
    try:
        loc.first.hover(timeout=CLICK_TIMEOUT_MS)
    except Exception:
        # Перекрыт/нестабилен — движение принудительно (без actionability)
        try:
            loc.first.hover(force=True, timeout=CLICK_TIMEOUT_MS)
        except Exception as e:
            detail = str(e).split("Call log")[0].strip().split("\n")[0]
            raise BrowserUnavailable(f"наведение не выполнено: {detail[:120]}")
    logger.info(f"[BrowserActions] Наведение idx={idx} "
                f"({host_part or f'вкладка #{tab_id}' if tab_id else 'активная'})")
    try:
        hov = str(_eval_arg(scope, _HOVER_CHECK_JS, _mark_sel(idx)) or "")
    except Exception:
        hov = ""
    if hov == "hover":
        return "hovered"
    # Меню/подсветка раскрылась и перерисовала DOM (метка могла пропасть) —
    # это и есть видимый эффект наведения
    if hov == "gone":
        return "hovered"
    _verify_effect(lambda: _page_state(scope), pre, "наведение отправлено")
    return "hovered"


def hover_tagged(host_part: Optional[str], idx: int,
                 tab_id: Optional[int] = None) -> str:
    """Наведение курсора на элемент с номером разметки из снапшота вкладки
    (общего или целевого — как у click_tagged). Только CDP."""
    if _select_backend(tab_op=True) != "cdp":
        raise _no_backend("наведение курсора")
    return _WORKER.submit(
        lambda w: _hover_cdp(w, host_part, idx, tab_id))


def _hover_point_cdp(w: "_CdpWorker", host_part: Optional[str],
                     x: float, y: float, tab_id: Optional[int]) -> str:
    page = w.page_for(host_part, tab_id)
    x, y = _check_point(page, x, y)
    pre = _page_state(page)
    page.mouse.move(x, y)
    # Меню раскрывается с transition — короткое окно на DOM-реакцию; без
    # неё честно рапортуем само движение (наведение без эффекта — тоже
    # валидный исход: «подержи курсор над X»)
    if _wait_effect(lambda: _page_state(page), pre) == EFFECT_CHANGED:
        return "hovered-react"
    return "hovered"


def hover_at_point(host_part: Optional[str], x: float, y: float,
                   tab_id: Optional[int] = None) -> str:
    """Наведение по координатам вьюпорта (CSS px) — зона vision-фолбэка
    без DOM-метки. Только CDP."""
    if _select_backend(tab_op=True) != "cdp":
        raise _no_backend("наведение по координатам")
    return _WORKER.submit(
        lambda w: _hover_point_cdp(w, host_part, x, y, tab_id))


def _norm_ws(s: str) -> str:
    return " ".join(str(s or "").lower().split())



def fill_tagged(host_part: Optional[str], idx: int, text: str,
                tab_id: Optional[int] = None, submit: bool = False) -> str:
    """Ввод текста в поле с номером разметки из снапшота вкладки.
    CDP: фокус кликом → очистка → посимвольный ввод (реальные key-события,
    их ждут suggest-виджеты вроде выбора города), фолбэк на fill().
    submit=True — после ввода Enter в том же поле («…и отправь»: чаты,
    где кнопка отправки — безымянная иконка). Closed-loop, как у клика
    (п.6): значение поля читается обратно, несовпадение → FillUncertain
    («не уверен»), а не тихое «Введено»; для submit — поле очистилось
    или страница изменилась, иначе тоже FillUncertain."""
    text = str(text or "").strip()
    if not text:
        raise BrowserUnavailable("пустой текст — нечего вводить")
    if _select_backend(tab_op=True) == "cdp":
        return _WORKER.submit(
            lambda w: _fill_cdp(w, host_part, idx, text, tab_id, submit))
    return _fill_applescript(host_part, idx, text, tab_id, submit)


def _fill_cdp(w: _CdpWorker, host_part: Optional[str], idx: int,
              text: str, tab_id: Optional[int], submit: bool = False) -> str:
    page = w.page_for(host_part, tab_id)
    loc, scope = _locator_any_frame(page, idx)
    if loc is None:
        raise BrowserUnavailable("элемент потерян — страница изменилась")
    el = loc.first
    try:
        el.click(timeout=CLICK_TIMEOUT_MS)   # фокус: suggest слушает focus
        el.fill("", timeout=CLICK_TIMEOUT_MS)  # сброс старого значения + input
        el.press_sequentially(text, delay=25)
    except Exception:
        # Поле не приняло посимвольный ввод (readonly/перерисовка) —
        # мгновенная установка значения с input-событием
        try:
            el.fill(text, timeout=CLICK_TIMEOUT_MS)
        except Exception as e:
            # Виджет ЗАМЕНИЛ поле при фокусе (Vue-поиск википедии подменяет
            # input): метка умерла вместе со старым элементом, но фокус
            # остался в новом поле — печатаем в активный элемент клавиатурой
            typed = False
            try:
                editable = scope.evaluate(
                    "(function(){var a=document.activeElement;return !!a&&"
                    "(a.isContentEditable||/^(INPUT|TEXTAREA)$/.test(a.tagName))"
                    "})()")
                if editable:
                    page.keyboard.type(text, delay=25)
                    typed = True
            except Exception:
                typed = False
            if not typed:
                detail = str(e).split("Call log")[0].strip().split("\n")[0]
                raise BrowserUnavailable(f"ввод не выполнен: {detail[:120]}")
    logger.info(f"[BrowserActions] Ввод idx={idx} ({len(text)} симв.) "
                f"({host_part or f'вкладка #{tab_id}' if tab_id else 'активная'})")
    try:
        got = str(el.evaluate(
            "e => e.isContentEditable ? e.innerText : e.value") or "")
    except Exception:
        got = ""
    if not got.strip():
        # Поле могло быть ЗАМЕНЕНО виджетом при фокусе (Vue-поиск википедии
        # подменяет input при вводе, метка data-vpc-idx уходит с ним) —
        # читаем значение активного элемента: фокус после ввода остаётся
        # в (заменённом) поле
        try:
            got = str(scope.evaluate(
                "(document.activeElement&&"
                "(document.activeElement.isContentEditable"
                "?document.activeElement.innerText"
                ":document.activeElement.value))||''") or "")
        except Exception:
            pass
    # «Содержит», а не «равно»: виджет может дописать своё («�город, …»)
    if _norm_ws(text) in _norm_ws(got):
        if not submit:
            return "filled"
        pre = _page_state(scope)
        try:
            el.press("Enter", timeout=CLICK_TIMEOUT_MS)
        except Exception as e:
            detail = str(e).split("Call log")[0].strip().split("\n")[0]
            raise BrowserUnavailable(
                f"текст введён, но Enter не нажался: {detail[:100]}")
        # Отправка подтверждается фактом: поле очистилось (чат) или
        # страница изменилась (поиск ушёл в навигацию)
        deadline = time.time() + SUBMIT_VERIFY_SEC
        while time.time() < deadline:
            try:
                cur = str(el.evaluate(
                    "e => e.isContentEditable ? e.innerText : e.value") or "")
            except Exception:
                cur = ""  # элемент ушёл из DOM — страница перерисовалась
            if not cur.strip() or _effect_verdict(
                    pre, _page_state(scope)) == EFFECT_CHANGED:
                logger.info(f"[BrowserActions] Ввод+Enter idx={idx} — отправлено")
                return "submitted"
            time.sleep(0.25)
        raise FillUncertain(
            "текст введён, Enter нажат, но поле не очистилось и страница "
            "не изменилась — не уверен, что сообщение отправилось")
    raise FillUncertain(
        "текст отправлен в поле, но его значение не совпало — "
        "не уверен, что ввод сработал")


def _fill_applescript(host_part: Optional[str], idx: int, text: str,
                      tab_id: Optional[int], submit: bool = False) -> str:
    """JS-ввод по номеру разметки: native setter (React-совместимо) + события
    input/change, значение читается обратно — то же closed-loop правило.
    Элемент ищем циклом (_mark_find_js), как в _click_applescript."""
    if submit:
        raise _no_backend("отправка по Enter («…и отправь»)")
    js = ("var d=document.documentElement;"
          + _mark_find_js(idx) +
          "if(!el){d.setAttribute('data-vpc-res','элемент потерян — страница изменилась');}"
          "else{el.scrollIntoView({block:'center'});el.focus();"
          "var txt=" + json.dumps(text, ensure_ascii=False) + ";"
          "if(el.isContentEditable){el.innerText=txt;}"
          "else{var proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
          "var desc=Object.getOwnPropertyDescriptor(proto,'value');"
          "if(desc&&desc.set){desc.set.call(el,txt);}else{el.value=txt;}}"
          "el.dispatchEvent(new Event('input',{bubbles:true}));"
          "el.dispatchEvent(new Event('change',{bubbles:true}));"
          "var got=el.isContentEditable?el.innerText:el.value;"
          "function vn(s){return (s||'').replace(/\\s+/g,' ').trim().toLowerCase();}"
          "d.setAttribute('data-vpc-res',vn(got).indexOf(vn(txt))>=0?'ok:filled':'uncertain');}"
          "d.getAttribute('data-vpc-res')")
    out = _run_apple_events(host_part, js, tab_id=tab_id)
    if out.startswith("ok:"):
        logger.info(f"[BrowserActions] Ввод idx={idx} ({len(text)} симв.) "
                    f"({host_part or f'вкладка #{tab_id}' if tab_id else 'активная'})")
        return out[3:]
    if out == "uncertain":
        raise FillUncertain(
            "текст отправлен в поле, но его значение не совпало — "
            "не уверен, что ввод сработал")
    raise BrowserUnavailable(out or "ввод не выполнен")


def href_of_tagged(host_part: Optional[str], idx: int,
                   tab_id: Optional[int] = None) -> str:
    """Абсолютный href элемента с номером разметки из снапшота вкладки —
    общего или целевого (скачивание «нашлось целевым снапшотом» брало бы
    href чужого элемента, если бы номер искался только в общей разметке).
    Пустая строка — элемент потерян или это не ссылка (иконки меню без href).
    CDP ищет метку и в iframe'ах (как снапшот), AppleScript — только главный
    фрейм."""
    if _select_backend(tab_op=True) == "cdp":
        def _op(w):
            page = w.page_for(host_part, tab_id)
            loc, _scope = _locator_any_frame(page, idx)
            if loc is None:
                return ""
            try:
                return str(loc.first.evaluate("e => e.href || ''") or "")
            except Exception:
                return ""
        return str(_WORKER.submit(_op) or "").strip()
    js = ("var d=document.documentElement;"
          + _mark_find_js(idx) +
          "d.setAttribute('data-vpc-res', el ? (el.href||'') : '');"
          "d.getAttribute('data-vpc-res')")
    return _run_js(host_part, js, tab_id=tab_id).strip()


def download_in_tab(host_part: Optional[str], url: str,
                    tab_id: Optional[int] = None) -> str:
    """Принудительное скачивание url из контекста вкладки: клик синтетического
    <a download> внутри страницы. Прямой клик по ссылке с target=_blank
    (file_get-ссылки) открывал бы мёртвую вкладку вместо скачивания.
    CDP: ждём события download (closed-loop, п.6); нет события за
    DOWNLOAD_VERIFY_SEC → ClickUncertain. AppleScript-фолбэк событий не даёт —
    там без проверки."""
    # Раньше url экранировался вручную (\ и ' — ровно то, во что сам его тут
    # оборачивали) и не в IIFE: `\n`/`\r`/U+2028/U+2029 в url проходили как
    # есть и ломали строковый литерал JS; _js_fill/_js_value кодируют url
    # json.dumps'ом (управляющие символы и весь не-ASCII — в \-escape) и не
    # оставляют url в кавычках шаблона — сравнивать/резать вручную не нужно
    js = _js_fill(
        "(function(){var a=document.createElement('a');"
        "a.href=__URL__;a.download='';"
        "document.body.appendChild(a);a.click();a.remove();"
        "return 'ok:download';})()", URL=url)
    if _select_backend(tab_op=True) == "cdp":
        def _op(w):
            page = w.page_for(host_part, tab_id)
            from playwright.sync_api import TimeoutError as PwTimeout
            try:
                with page.expect_download(timeout=DOWNLOAD_VERIFY_SEC * 1000) as dl_info:
                    page.evaluate(js)
                dl = dl_info.value
                return f"download:{dl.suggested_filename or ''}"
            except PwTimeout:
                raise ClickUncertain(
                    "скачивание не подтвердилось за несколько секунд — "
                    "не уверен, что началось")
        out = _WORKER.submit(_op, timeout=DOWNLOAD_VERIFY_SEC + SUBMIT_MARGIN_SEC)
        logger.info(f"[BrowserActions] Скачивание {url[:60]} → {out[:40]}")
        return out
    out = _run_js(host_part, js, tab_id=tab_id)
    if not out.startswith("ok:"):
        raise BrowserUnavailable(out or "скачивание не запустилось")
    logger.info(f"[BrowserActions] Скачивание {url[:60]} "
                f"({host_part or 'активная'}, без подтверждения — fallback)")
    return out[3:]


# ── Открытие/поиск вкладок (унифицировано) ───────────────

# ── Фоновые вкладки (сырой CDP, без выдёргивания окна) ──────────
# Playwright оборачивает в Page только таргеты, созданные им самим, а его
# new_page() АКТИВИРУЕТ вкладку — macOS тут же поднимает окно Chrome на
# передний план (проверено замером frontmost-процесса). Target.createTarget
# (background:true) вкладку не активирует — фокус остаётся у пользователя,
# но playwright такой таргет в Page не превращает. Поэтому служебные
# вкладки (web_llm) открываем через собственный минимальный CDP-клиент по
# browser-websocket и работаем через flat-сессию (sessionId в конверте).
# На фоновых вкладках доступны только eval/навигация/ввод в чат —
# интерактивные действия (клики, снапшоты) на них не поддержаны.

_RAW_ID_OFFSET = 1_000_000  # id фоновых вкладок не пересекаются с playwright-реестром
# Единый реестр фоновых вкладок обоих пулов: call-site'ы (`is_raw_tab`) не
# зависят от пула; пул хранится в записи вкладки
_RAW_TABS: Dict[int, dict] = {}  # tab_id → {"targetId", "sessionId", "pool"}
# Реестр правят потоки разных WebChat (у каждого свой лок), поэтому все его
# чтения/записи — под общим локом, а id выдаёт МОНОТОННЫЙ счётчик и никогда
# не переиспользует освободившийся: «max(_RAW_TABS)+1» отдавал одинаковый id
# двум параллельным открытиям (промпт одного сайта уходил во вкладку
# другого), а после дропа — прежнему владельцу, который кэширует _tab_id и
# продолжал слать/навигировать уже чужую вкладку
_RAW_TABS_LOCK = threading.RLock()
_RAW_NEXT_ID = _RAW_ID_OFFSET + 1
_RAW_SWEPT: set = set()  # пулы, где уборка осиротевших вкладок уже прошла


def is_raw_tab(tab_id: Optional[int]) -> bool:
    """Вкладка — фоновая (raw-CDP, реестр _RAW_TABS). ЕДИНСТВЕННАЯ проверка
    транспорта: развилки по `tab_id in _RAW_TABS` расползались по call-
    site'ам, и новая операция (_eval_js_any) молча оставалась без raw-ветки."""
    if tab_id is None:
        return False
    with _RAW_TABS_LOCK:
        return tab_id in _RAW_TABS


def _refuse_raw_tab(tab_id: Optional[int], what: str):
    """Операция принципиально невозможна на фоновой вкладке — отказываем
    ЯВНО и отличимо (RawTabUnsupported), а не «не получилось, наверное
    чисто»: молчаливый промах уводил действие на чужую вкладку."""
    if is_raw_tab(tab_id):
        raise RawTabUnsupported(
            f"{what} недоступно для фоновой вкладки веб-чата — playwright "
            "её не видит (окна у неё нет)")

# ── Пулы браузеров (web_extended) ──
# H — headless Chrome: постоянные фоновые веб-чаты (deepseek/qwen/...),
# окна нет вообще → нет композитинга WindowServer и GPU-нагрузки.
# V — headed Chrome: видимые команды управления + headed/hidden веб-чаты
# (chatgpt/claude); по умолчанию выключен, живёт по требованию с idle-таймером.
# Пулы — разные процессы Chrome с разными профилями (один профиль нельзя
# держать двум процессам): H наследует существующий automation-профиль с
# логинами, V при первом запуске получает его копию.
_POOL_H = "h"
_POOL_V = "v"
# RLock, а не Lock: ленивый старт пула H идёт ИЗНУТРИ _raw_call (под этим
# же локом) и при смене режима (rescue on/off) дёргает _reset_raw_pool и
# Browser.close — на обычном Lock это вешало поток намертво
_RAW_LOCKS = {_POOL_H: threading.RLock(), _POOL_V: threading.RLock()}
_RAW_CLIENTS: Dict[str, object] = {_POOL_H: None, _POOL_V: None}
_POOL_H_PROC: Optional[subprocess.Popen] = None  # Chrome пула H (V гоняет _WORKER)


def _pool_h_cdp_url() -> str:
    return str(_BCFG.get("headless_url") or _DEFAULT_H_CDP_URL)


def _pool_h_profile() -> str:
    """Профиль пула H — существующий automation-профиль (там логины чатов)."""
    return _resolve_user_data_dir()


def _pool_v_profile() -> str:
    """Профиль пула V: конфиг visible_user_data_dir → иначе дефолт per-OS
    (сосед automation-профиля). При первом запуске создаётся копией профиля H."""
    val = _BCFG.get("visible_user_data_dir")
    if isinstance(val, dict):
        val = val.get(sys.platform) or val.get("other")
    if isinstance(val, str) and val.strip():
        path = os.path.expandvars(os.path.expanduser(val.strip()))
    else:
        path = os.path.expandvars(os.path.expanduser(
            _DEFAULT_V_PROFILES.get(sys.platform, _DEFAULT_V_PROFILES["linux"])))
    os.makedirs(path, exist_ok=True)
    return path


def _ensure_v_profile_copy():
    """Первый запуск пула V: клонируем профиль H (логины веб-чатов, куки —
    на macOS/Linux ключ шифрования кук общий на машину, в копии они
    расшифруются). Кэши/локи не копируем. Дальше профили живут независимо:
    разлогин в одном — перелогин руками в нём же."""
    dst = _pool_v_profile()
    # Маркер живого профиля: появился Default/Preferences или Local State
    if os.path.exists(os.path.join(dst, "Default", "Preferences")) or \
            os.path.exists(os.path.join(dst, "Local State")):
        return
    src = _pool_h_profile()
    if not os.path.isdir(src) or os.path.realpath(src) == os.path.realpath(dst):
        return
    _SKIP = {"Cache", "Code Cache", "GPUCache", "Service Worker",
             "SingletonLock", "SingletonCookie", "SingletonSocket",
             "Crashpad", "BrowserMetrics", "lockfile", ".org.chromium.Chromium.*"}
    import fnmatch
    def _ignore(_dir, names):
        return [n for n in names
                if n in _SKIP or any(fnmatch.fnmatch(n, p) for p in _SKIP)]
    try:
        if os.path.isdir(dst) and not os.listdir(dst):
            os.rmdir(dst)  # пустой каталог от makedirs — copytree мешает
        shutil.copytree(src, dst, ignore=_ignore, dirs_exist_ok=True)
        logger.info(f"[BrowserActions] Профиль пула V создан копией профиля H "
                    f"({dst})")
    except Exception as e:
        logger.warning(f"[BrowserActions] Копия профиля для пула V не "
                       f"удалась ({e}) — стартуем с пустым")


def _ws_timeout(e: BaseException) -> bool:
    """Исключение — это «сокет молчит», а не обрыв: websocket-client кидает
    свой WebSocketTimeoutException, голый сокет — socket.timeout."""
    import socket
    return isinstance(e, socket.timeout) or type(e).__name__ in (
        "WebSocketTimeoutException", "timeout", "TimeoutError")


class _RawCdp:
    """Минимальный sync CDP-клиент поверх browser-websocket: один сокет,
    flat-сессии через sessionId в конверте, события пропускаем. Нужен для
    background-таргетов, которых playwright не видит."""

    def __init__(self, cdp_url: Optional[str] = None):
        import urllib.request
        import websocket  # websocket-client
        base = str(cdp_url or _BCFG.get("cdp_url") or CDP_URL).rstrip("/")
        with urllib.request.urlopen(f"{base}/json/version", timeout=5) as r:
            ws_url = json.loads(r.read().decode())["webSocketDebuggerUrl"]
        # suppress_origin: Chrome отвергает websocket с Origin-заголовком
        # (403 «--remote-allow-origins»); без заголовка пускает
        self._ws = websocket.create_connection(
            ws_url, timeout=RAW_CONNECT_TIMEOUT_SEC, suppress_origin=True)

    def call(self, method: str, params: Optional[dict] = None,
             session_id: Optional[str] = None,
             timeout: Optional[float] = None) -> dict:
        """timeout — бюджет ответа ИМЕННО этого вызова. Таймаут сокета для
        этого не годится: он общий на соединение, а ожидания в странице
        (awaitPromise — аплоад картинки до 25с) длятся дольше любой разумной
        константы, и recv падал раньше ответа ровно на долгих операциях."""
        budget = float(RAW_CALL_TIMEOUT_SEC if timeout is None else timeout)
        self._next = getattr(self, "_next", 0) + 1
        msg: Dict[str, object] = {"id": self._next, "method": method,
                                  "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self._ws.send(json.dumps(msg))
        deadline = time.monotonic() + budget
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RawCallTimeout(
                    f"CDP {method}: ответа нет за {int(budget)}с")
            try:
                # Шаг recv'а мелкий: сокет-таймаут только нарезает ожидание,
                # потолок держит бюджет вызова
                self._ws.settimeout(max(0.5, min(left, 5.0)))
                data = json.loads(self._ws.recv())
            except Exception as e:
                if _ws_timeout(e):
                    continue  # ждём дальше в границах бюджета
                raise
            if data.get("id") != msg["id"]:
                # События и ответы брошенных по таймауту вызовов — мимо
                # (id монотонный, чужой ответ не может сойти за наш)
                continue
            if "error" in data:
                raise BrowserUnavailable(
                    f"CDP {method}: {(data['error'] or {}).get('message')}")
            return data.get("result") or {}

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def _session_gone(e: BaseException) -> bool:
    """Протокольный отказ «сессии с таким id нет»: таргет жив, но attach
    протух (Chrome пересоздал сессию, соединение переподнималось)."""
    return "Session with given id not found" in str(e)


def _raw_call(method: str, params: Optional[dict] = None,
              session_id: Optional[str] = None, pool: str = _POOL_V,
              tab_id: Optional[int] = None, timeout: Optional[float] = None,
              _retried: bool = False) -> dict:
    """Вызов CDP: ленивое подключение, свой бюджет ответа у каждого вызова и
    ОДНО восстановление на ошибку транспорта.
    pool — какой Chrome (H — headless веб-чаты / V — headed): у пулов свои
    сокет и лок, они не блокируют друг друга. Для пула H перед подключением
    гарантируется запуск его Chrome (ленивый старт).
    tab_id — адресация вкладкой вместо голого sessionId: он резолвится из
    реестра НА КАЖДОЙ попытке, поэтому повтор после переподключения уходит
    уже в новую сессию. С зафиксированным sessionId повтор в новом
    соединении был невалиден всегда — один обрыв убивал разом все вкладки.
    timeout — потолок ожидания ответа; его истечение соединение НЕ рвёт."""
    lock = _RAW_LOCKS[pool]
    if pool == _POOL_V:
        note_pool_v_activity()
    reattach = ""  # "pool" — пересобрали клиент, "tab" — протухла одна сессия
    with lock:
        if _RAW_CLIENTS[pool] is None:
            if pool == _POOL_H:
                _ensure_pool_h_browser()
            _RAW_CLIENTS[pool] = _RawCdp(_pool_h_cdp_url()
                                         if pool == _POOL_H else None)
            _sweep_orphan_tabs(_RAW_CLIENTS[pool], pool)
        sid = _raw_session(tab_id) if tab_id is not None else session_id
        try:
            return _RAW_CLIENTS[pool].call(method, params, sid, timeout=timeout)
        except RawCallTimeout:
            raise  # бюджет одного вызова: соединение и соседние вкладки живы
        except BrowserUnavailable as e:
            if _retried or tab_id is None or not _session_gone(e):
                raise
            reattach = "tab"
        except Exception:
            if _retried:
                raise BrowserUnavailable(f"CDP-соединение оборвано на {method}")
            try:
                _RAW_CLIENTS[pool].close()
            except Exception:
                pass
            _RAW_CLIENTS[pool] = None
            reattach = "pool"
    # Переattach — ВНЕ лока: он сам ходит через _raw_call
    if reattach == "pool":
        _raw_reattach_pool(pool)
    else:
        _raw_reattach_tab(int(tab_id))
    return _raw_call(method, params, session_id, pool=pool, tab_id=tab_id,
                     timeout=timeout, _retried=True)


def _raw_session(tab_id: int) -> str:
    with _RAW_TABS_LOCK:
        tab = _RAW_TABS.get(tab_id)
        if tab is None:
            raise BrowserUnavailable("фоновая вкладка закрыта")
        return str(tab["sessionId"])


def _raw_tab_call(tab_id: int, method: str, params: Optional[dict] = None,
                  timeout: Optional[float] = None) -> dict:
    """CDP-вызов, адресованный фоновой вкладке: пул и sessionId берутся из
    реестра (sessionId — на каждой попытке, см. _raw_call)."""
    with _RAW_TABS_LOCK:
        tab = _RAW_TABS.get(tab_id)
        if tab is None:
            raise BrowserUnavailable("фоновая вкладка закрыта")
        pool = _pool_of_tab(tab)
    return _raw_call(method, params, pool=pool, tab_id=tab_id, timeout=timeout)


def _raw_reattach_tab(tab_id: int) -> bool:
    """Новый sessionId для живого таргета вкладки. False — таргета больше
    нет (вкладку закрыли снаружи): запись выбрасываем, и вызывающий получит
    честное «фоновая вкладка закрыта» вместо вечных «session not found»."""
    with _RAW_TABS_LOCK:
        tab = _RAW_TABS.get(tab_id)
        if tab is None:
            return False
        target_id, pool = str(tab["targetId"]), _pool_of_tab(tab)
    try:
        sid = str(_raw_call("Target.attachToTarget",
                            {"targetId": target_id, "flatten": True},
                            pool=pool, _retried=True)["sessionId"])
    except Exception as e:
        logger.info(f"[BrowserActions] Фоновая вкладка #{tab_id} не "
                    f"переattach'илась ({e}) — считаем её мёртвой")
        _raw_forget(tab_id)
        return False
    with _RAW_TABS_LOCK:
        if tab_id in _RAW_TABS:
            _RAW_TABS[tab_id]["sessionId"] = sid
    return True


def _raw_reattach_pool(pool: str):
    """После пересоздания клиента все прежние sessionId невалидны —
    переattach живых таргетов пула; мёртвые вылетают из реестра."""
    with _RAW_TABS_LOCK:
        ids = [t for t, tab in _RAW_TABS.items() if _pool_of_tab(tab) == pool]
    if not ids:
        return
    alive = sum(1 for t in ids if _raw_reattach_tab(t))
    logger.info(f"[BrowserActions] Пул {pool.upper()}: соединение пересобрано, "
                f"живых фоновых вкладок {alive} из {len(ids)}")


def _sweep_orphan_tabs(client, pool: str):
    """Уборка осиротевших фоновых вкладок при ПЕРВОМ подключении к пулу H:
    его Chrome переживает перезапуск бота, а реестр — нет, и страницы
    веб-чатов прошлых запусков копились в нём бессрочно. Закрываем только
    page-таргеты служебных хостов веб-чатов (is_service_host), которых нет
    в реестре. Пул V не трогаем вовсе — там вкладки пользователя; в
    rescue-режиме пропускаем и H: в его окне человек решает капчу.
    Best effort, раз за процесс."""
    if pool in _RAW_SWEPT:
        return
    _RAW_SWEPT.add(pool)
    if pool != _POOL_H:
        return
    # Только по явному browser.sweep_orphan_tabs: Chrome пула H общий для всех
    # процессов бота (персоны можно запускать раздельно), и «нет в реестре
    # этого процесса» тогда не значит «сирота» — это живой чат соседа
    if not _BCFG.get("sweep_orphan_tabs"):
        return
    try:
        if pool_h_rescue_active():
            return
        with _RAW_TABS_LOCK:
            known = {str(t["targetId"]) for t in _RAW_TABS.values()}
        infos = (client.call("Target.getTargets") or {}).get("targetInfos") or []
        for info in infos:
            if str(info.get("type")) != "page":
                continue
            target_id = str(info.get("targetId") or "")
            if not target_id or target_id in known:
                continue
            host = (urlparse(str(info.get("url") or "")).hostname or "").lower()
            if not host or not is_service_host(host):
                continue
            client.call("Target.closeTarget", {"targetId": target_id})
            logger.info(f"[BrowserActions] Пул H: закрыта осиротевшая "
                        f"вкладка веб-чата ({host})")
    except Exception as e:
        logger.debug(f"[BrowserActions] Уборка сирот пула {pool}: {e}")


def _pool_of_tab(tab: dict) -> str:
    return str(tab.get("pool") or _POOL_V)


def _pool_h_alive() -> bool:
    """Пробник: Chrome пула H отвечает на /json/version."""
    import urllib.request
    try:
        with urllib.request.urlopen(
                _pool_h_cdp_url().rstrip("/") + "/json/version", timeout=2) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def _chrome_version(exe: str) -> Optional[str]:
    """Версия браузера для маскировочного UA (HeadlessChrome → Chrome)."""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=5).stdout
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out or "")
        return m.group(1) if m else None
    except Exception:
        return None


def _headless_mask_flags(exe: str) -> List[str]:
    """Флаги headless=new + маскировка под headed (web_extended): детект
    сводится к WebGL/поведенческим сигналам, UA и размер окна закрываем.
    Флаги платформонезависимы, кроме ANGLE-бэкенда (реальный GPU вместо
    SwiftShader — WebGL-фингерпринт настоящий)."""
    flags = ["--headless=new", "--window-size=1920,1080", "--hide-scrollbars",
             *CHROME_NO_THROTTLE_FLAGS]
    ua_platform = {
        "darwin": "Macintosh; Intel Mac OS X 10_15_7",
        "win32": "Windows NT 10.0; Win64; x64",
    }.get(sys.platform, "X11; Linux x86_64")
    ver = _chrome_version(exe)
    if ver:
        flags.append(f"--user-agent=Mozilla/5.0 ({ua_platform}) "
                     f"AppleWebKit/537.36 (KHTML, like Gecko) "
                     f"Chrome/{ver} Safari/537.36")
    angle = {"darwin": "metal", "win32": "d3d11"}.get(sys.platform)
    if angle:  # реальный GPU; не взлетит — Chrome сам уйдёт в SwiftShader
        flags.append(f"--use-angle={angle}")
    return flags


def _ensure_pool_h_browser():
    """Chrome пула H жив и в нужном режиме (ленивый старт при первом
    обращении; rescue: видимый режим ≠ штатный → перезапуск)."""
    if _pool_h_alive():
        if _POOL_H_RUNNING_MODE is None or \
                _POOL_H_RUNNING_MODE == _pool_h_desired_mode():
            return
        # Режим поменялся (rescue on/off) — перезапускаем в нужном
        global _POOL_H_PROC
        logger.info("[BrowserActions] Пул H: смена режима — перезапуск")
        _close_pool_h_graceful()
        _reset_raw_pool(_POOL_H)
        _kill_chrome_on_profile(_POOL_H_PROC, _pool_h_profile(), grace_sec=3.0)
        _POOL_H_PROC = None
    if not _BCFG.get("launch", True):
        raise BrowserUnavailable(
            f"headless-браузер бота ({_pool_h_cdp_url()}) недоступен")
    _launch_pool_h_chrome()


def _launch_pool_h_chrome():
    """Запуск Chrome пула H в нужном режиме: headless=new (штатно),
    hidden (headed со скрытым окном — запас против антибот-детекта) или
    headed (rescue: пользователь решает капчу). Пониженный приоритет,
    Memory Saver, свой порт/профиль."""
    global _POOL_H_PROC, _POOL_H_RUNNING_MODE
    exe = _resolve_executable()
    if not exe:
        raise BrowserUnavailable(
            "не найден ни один Chromium-браузер (Chrome, Edge, Opera, "
            "Яндекс, Brave, Vivaldi) — укажи browser.executable в конфиге "
            "computer_control или установи один из них")
    udd = _pool_h_profile()
    _check_profile_lock(udd)
    _enable_memory_saver(udd)
    port = urlparse(_pool_h_cdp_url()).port or 9223
    mode = _pool_h_desired_mode()
    if mode not in ("headless", "hidden", "headed"):
        logger.warning(f"[BrowserActions] pool_h_mode {mode!r} неизвестен — headless")
        mode = "headless"
    cmd = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={udd}",
           "--no-first-run", "--no-default-browser-check",
           "--disable-session-crashed-bubble", *CHROME_THRIFT_FLAGS,
           *CHROME_NO_THROTTLE_FLAGS]
    if mode == "headless":
        cmd.extend(_headless_mask_flags(exe))
    else:
        cmd.append("--window-size=1920,1080")
    cmd.append("about:blank")
    popen_kw: Dict[str, object] = dict(
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == "win32":
        popen_kw["creationflags"] = getattr(
            subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    else:
        cmd = ["nice", "-n", "10"] + cmd
    try:
        proc = subprocess.Popen(  # noqa: S603 — путь/флаги из нашего конфига
            cmd, **popen_kw)
    except OSError as e:
        raise BrowserUnavailable(f"не удалось запустить headless-браузер: {e}")
    _POOL_H_PROC = proc
    logger.info(f"[BrowserActions] Запускаю пул H (режим {mode}): "
                f"{exe} (профиль {udd}, порт {port})")
    deadline = time.monotonic() + BROWSER_LAUNCH_TIMEOUT_SEC
    # Как и у пула V: сбой запуска не оставляет осиротевший Chrome на профиле
    try:
        while time.monotonic() < deadline:
            if _pool_h_alive():
                _POOL_H_RUNNING_MODE = mode
                logger.info("[BrowserActions] Пул H запущен, CDP доступен")
                if mode == "hidden":
                    _hide_pool_window(proc.pid)
                return
            if proc.poll() is not None:
                raise BrowserUnavailable(
                    "headless-браузер завершился сразу после запуска — похоже, "
                    "его профиль занят другим Chrome. Закрой его и повтори")
            time.sleep(0.3)
        raise BrowserUnavailable(
            f"headless-браузер не поднял порт {port} за "
            f"{int(BROWSER_LAUNCH_TIMEOUT_SEC)}с")
    except BaseException:
        _abort_launch(proc, udd, "пула H")
        if _POOL_H_PROC is proc:
            _POOL_H_PROC = None
        raise


def _hide_pool_window(pid: Optional[int] = None):
    """Скрыть окно headed-Chrome пула (macOS: hide приложения по PID — чужие
    Chrome пользователя не затрагиваются). Windows/Linux: окно остаётся
    свёрнутым вручную (best effort, без зависимостей)."""
    if sys.platform != "darwin" or not pid:
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             _as_tell_to(f"set visible of {_as_proc_ref(pid)} to false",
                         app=_SYS_EVENTS_APP)],
            capture_output=True, timeout=10)
    except Exception as e:
        logger.debug(f"[BrowserActions] Скрыть окно пула не удалось: {e}")


# ── Жизненный цикл пула V (headed) ──
# Пул V выключен по умолчанию. Поднимается: команда управления (режим
# управления включён), headed/hidden webchat-сайт (если разрешено без режима
# управления), rescue. Гасится после v_idle_shutdown_min минут без CDP-
# активности (с проверкой играющего медиа — YouTube не убить посреди
# просмотра) и при выключении последнего чата режима управления (через тот
# же idle-путь). Режим управления сетит bot_instance (set_control_mode).
_CONTROL_MODE_CHATS: set = set()
_POOL_V_TS = time.time()          # последняя активность пула V
_POOL_V_WATCHDOG: Optional[threading.Thread] = None


def set_control_mode(chat_id: str, on: bool):
    """bot_instance сообщает о переключении режима управления чата.
    Включение: пул V поднимаем заранее (пользователь ждёт готовый браузер)
    и показываем окно. Выключение: пул догорит по idle-таймеру."""
    if on:
        _CONTROL_MODE_CHATS.add(str(chat_id))
        note_pool_v_activity()
        try:
            if _cdp_available():
                _set_pool_v_window_visible(True)
            elif _BCFG.get("launch", True):
                _WORKER.submit(lambda w: w.ensure_browser(allow_launch=True))
        except Exception as e:
            logger.debug(f"[BrowserActions] Прогрев пула V не удался: {e}")
    else:
        _CONTROL_MODE_CHATS.discard(str(chat_id))


def control_mode_any() -> bool:
    return bool(_CONTROL_MODE_CHATS)


def pool_v_webchat_allowed() -> bool:
    """headed-сайты webchat без режима управления — только по конфигу
    headed_fallback_without_control (дефолт False: устройство свободно от
    видимого Chrome, chatgpt/claude пропускаются в цепочке fallback)."""
    return control_mode_any() or bool(_BCFG.get("headed_fallback_without_control"))


def note_pool_v_activity():
    global _POOL_V_TS
    _POOL_V_TS = time.time()
    _start_pool_v_watchdog()


def _start_pool_v_watchdog():
    global _POOL_V_WATCHDOG
    if _POOL_V_WATCHDOG is not None:
        return
    _POOL_V_WATCHDOG = threading.Thread(
        target=_pool_v_watchdog, daemon=True, name="vpc-pool-v")
    _POOL_V_WATCHDOG.start()


def _pool_v_watchdog():
    while True:
        time.sleep(30)
        try:
            _pool_v_idle_check()
        except Exception:
            pass


def _pool_v_idle_check():
    idle_min = float(_BCFG.get("v_idle_shutdown_min") or 20)
    if idle_min <= 0:
        return
    if time.time() - _POOL_V_TS < idle_min * 60:
        return
    if _WORKER._thread is None:
        return  # бот к пулу V не подключался — нечего гасить
    if _pool_v_media_playing():
        note_pool_v_activity()  # пользователь смотрит/слушает — не трогаем
        return
    logger.info("[BrowserActions] Пул V простаивает "
                f">{int(idle_min)} мин — гасим Chrome")
    try:
        udd = _pool_v_profile()
        if not _is_default_browser_profile(udd):
            _WORKER.submit(lambda w: w._kill_browser(udd),
                           timeout=SUBMIT_KILL_TIMEOUT_SEC)
        _reset_raw_pool(_POOL_V)
    except Exception as e:
        logger.debug(f"[BrowserActions] Остановка пула V по простою: {e}")


def _pool_v_media_playing() -> bool:
    """Во вкладках пула V играет видео/аудио (best effort)."""
    if _WORKER._thread is None:
        return False

    def _op(w):
        if w._browser is None:
            return False
        for p in w._all_pages():
            try:
                if p.evaluate(
                        "Array.from(document.querySelectorAll('video,audio'))"
                        ".some(function(m){return !m.paused && !m.ended;})"):
                    return True
            except Exception:
                continue
        return False
    try:
        return bool(_WORKER.submit(_op))
    except Exception:
        return False


def _pool_v_pid() -> Optional[int]:
    if _WORKER._proc is not None and _WORKER._proc.poll() is None:
        return _WORKER._proc.pid
    if sys.platform != "win32":
        try:
            lock = os.path.join(_pool_v_profile(), "SingletonLock")
            return int(os.readlink(lock).rsplit("-", 1)[-1])
        except Exception:
            return None
    return None


def _set_pool_v_window_visible(visible: bool):
    """macOS: показать/скрыть приложение Chrome пула V (по PID, чужие Chrome
    не затрагиваются). Остальные ОС — no-op."""
    if sys.platform != "darwin":
        return
    pid = _pool_v_pid()
    if not pid:
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             _as_tell_to(
                 f"set visible of {_as_proc_ref(pid)} to "
                 f"{'true' if visible else 'false'}", app=_SYS_EVENTS_APP)],
            capture_output=True, timeout=10)
    except Exception as e:
        logger.debug(f"[BrowserActions] Видимость окна пула V не переключена: {e}")


# ── Rescue-режим пула H (ручное решение капчи) ──
# Антибот-челлендж не прошёл автокликом → сайт в карантине, пользователь
# пишет «почини браузер» → пул H перезапускается ВИДИМЫМ, пользователь решает
# капчу, следующее обращение к сайту видит чистую страницу → карантин снимается
# (web_llm._challenge_check), rescue завершается → пул H возвращается в
# headless. Rescue истекает сам (если пользователь так и не пришёл).
_POOL_H_RUNNING_MODE: Optional[str] = None  # режим, в котором H реально запущен
_POOL_H_MODE_OVERRIDE: Optional[str] = None  # "headed" на время rescue
_POOL_H_RESCUE_UNTIL = 0.0
POOL_H_RESCUE_MIN = 15.0


def _pool_h_desired_mode() -> str:
    global _POOL_H_MODE_OVERRIDE, _POOL_H_RESCUE_UNTIL
    if _POOL_H_MODE_OVERRIDE and time.time() < _POOL_H_RESCUE_UNTIL:
        return _POOL_H_MODE_OVERRIDE
    _POOL_H_MODE_OVERRIDE = None  # rescue истёк
    return str(_BCFG.get("pool_h_mode") or "headless").lower()


def pool_h_rescue_active() -> bool:
    return bool(_POOL_H_MODE_OVERRIDE) and time.time() < _POOL_H_RESCUE_UNTIL


def rescue_pool_h(duration_min: float = POOL_H_RESCUE_MIN) -> bool:
    """Перезапустить пул H ВИДИМЫМ (rescue: пользователь решает капчу руками).
    Вкладки веб-чатов умирают — web_llm переоткроет их по URL (self-healing).
    → True, если видимый браузер поднялся."""
    global _POOL_H_MODE_OVERRIDE, _POOL_H_RESCUE_UNTIL, _POOL_H_PROC
    _POOL_H_MODE_OVERRIDE = "headed"
    _POOL_H_RESCUE_UNTIL = time.time() + duration_min * 60
    logger.warning(f"[BrowserActions] Rescue пула H на {int(duration_min)} мин — "
                   "перезапуск в видимом режиме")
    try:
        _close_pool_h_graceful()
        _reset_raw_pool(_POOL_H)
        udd = _pool_h_profile()
        if not _is_default_browser_profile(udd):
            _kill_chrome_on_profile(_POOL_H_PROC, udd, grace_sec=3.0)
        _POOL_H_PROC = None
        _launch_pool_h_chrome()
        return True
    except Exception as e:
        logger.warning(f"[BrowserActions] Rescue-перезапуск пула H не удался: {e}")
        _POOL_H_MODE_OVERRIDE = None
        _POOL_H_RESCUE_UNTIL = 0.0
        return False


def end_rescue_pool_h():
    """Капча пройдена (web_llm увидел чистую страницу): пул H возвращается
    в штатный режим — лениво, при следующем обращении (mode-mismatch в
    _ensure_pool_h_browser перезапустит)."""
    global _POOL_H_MODE_OVERRIDE, _POOL_H_RESCUE_UNTIL
    _POOL_H_MODE_OVERRIDE = None
    _POOL_H_RESCUE_UNTIL = 0.0
    logger.info("[BrowserActions] Rescue пула H завершён — возврат в штатный режим")


def pool_status() -> dict:
    """Состояние пулов для API/devlog: живость, режим, простой пула V."""
    try:
        v_alive = _WORKER._browser is not None and _WORKER._browser.is_connected()
    except Exception:
        v_alive = False
    return {
        "h": {"alive": _pool_h_alive(), "mode": _POOL_H_RUNNING_MODE,
              "rescue": pool_h_rescue_active()},
        "v": {"alive": bool(v_alive),
              "idle_sec": int(time.time() - _POOL_V_TS)},
    }


def _raw_tab(tab_id: int) -> dict:
    """Снимок записи вкладки (targetId/pool). sessionId из снимка НЕ
    используем для вызовов — его резолвит _raw_call по tab_id, иначе повтор
    после переattach'а уходил бы в протухшую сессию."""
    with _RAW_TABS_LOCK:
        tab = _RAW_TABS.get(tab_id)
        if tab is None:
            raise BrowserUnavailable("фоновая вкладка закрыта")
        return dict(tab)


def _raw_open(url: str, pool: str = _POOL_V) -> int:
    """Фоновая вкладка: createTarget(background:true) + flat-сессия → tab_id.
    pool — в каком Chrome открывать (H — headless веб-чаты, V — headed).
    Пул H: вкладка создаётся АКТИВНОЙ в ОТДЕЛЬНОМ виртуальном окне
    (background:False + newWindow:True) — иначе headless-страница рождается
    скрытой: rAF не тикает, React-лента чата не рендерится, и отправленное
    сообщение «не появляется в ленте» (кейс 14.09). В пуле V (headed) —
    background:True: фокус/окно пользователя не трогаем."""
    if pool == _POOL_H:
        tid = _raw_call("Target.createTarget",
                        {"url": "about:blank", "background": False,
                         "newWindow": True},
                        pool=pool)["targetId"]
    else:
        tid = _raw_call("Target.createTarget",
                        {"url": "about:blank", "background": True},
                        pool=pool)["targetId"]
    sid = _raw_call("Target.attachToTarget",
                    {"targetId": tid, "flatten": True}, pool=pool)["sessionId"]
    global _RAW_NEXT_ID
    with _RAW_TABS_LOCK:
        tab_id = _RAW_NEXT_ID
        _RAW_NEXT_ID += 1
        _RAW_TABS[tab_id] = {"targetId": tid, "sessionId": sid, "pool": pool}
    if url and url != "about:blank":
        _raw_call("Page.navigate", {"url": url}, pool=pool, tab_id=tab_id)
    logger.info(f"[BrowserActions] Открыта фоновая вкладка #{tab_id} "
                f"(пул {pool.upper()}): {url[:80]}")
    return tab_id


def _raw_forget(tab_id: int) -> Optional[dict]:
    """Выбросить вкладку из реестра (страницу не трогаем — она считается
    мёртвой) → её прежняя запись или None."""
    with _RAW_TABS_LOCK:
        return _RAW_TABS.pop(tab_id, None)


def _raw_drop(tab_id: int):
    _raw_forget(tab_id)


def close_background_tab(tab_id: Optional[int]) -> bool:
    """Закрыть фоновую вкладку веб-чата (Target.closeTarget) и выбросить её
    из реестра. Без явного закрытия брошенные вкладки SPA-чатов копились в
    Chrome пула H: реестр про них забывал, а страницы жили и ели память.
    False — вкладка не наша/уже закрыта или браузер не ответил (не ошибка:
    вызывается на пути лечения)."""
    if tab_id is None:
        return False
    tab = _raw_forget(int(tab_id))
    if tab is None:
        return False
    try:
        _raw_call("Target.closeTarget", {"targetId": tab["targetId"]},
                  pool=_pool_of_tab(tab))
        logger.info(f"[BrowserActions] Фоновая вкладка #{tab_id} закрыта")
        return True
    except Exception as e:
        logger.debug(f"[BrowserActions] Закрытие фоновой вкладки "
                     f"#{tab_id}: {e}")
        return False


def _raw_eval(tab_id: int, js: str,
              timeout_sec: Optional[float] = None) -> str:
    """JS во фоновой вкладке. timeout_sec — бюджет ожидания ОТВЕТА: у JS,
    который сам ждёт в странице (awaitPromise — аплоад аттача, подтверждение
    вставки картинки), потолок задаёт вызывающий; константа сокета меньше
    бюджета страницы рвала соединение ровно на долгих ожиданиях."""
    _raw_session(tab_id)  # вкладка наша и жива
    for attempt in (1, 2):
        try:
            res = _raw_tab_call(tab_id, "Runtime.evaluate",
                                {"expression": js, "returnByValue": True,
                                 "awaitPromise": True},
                                timeout=timeout_sec)
        except RawCallTimeout:
            raise  # вкладку НЕ роняем: не ответил один вызов, а не страница
        except BrowserUnavailable as e:
            # «Cannot find context» — контекст пересоздаётся (навигация SPA
            # на свежей вкладке): транзиент, вкладку НЕ роняем — ретрай
            if attempt == 1 and "Cannot find context" in str(e):
                time.sleep(0.7)
                continue
            _raw_drop(tab_id)
            raise
        if res.get("exceptionDetails"):
            # Холодная страница (свежий Chrome, дорендер SPA, навигация)
            # может кидать разовые исключения — одна повторная попытка после
            # паузы; детерминированная ошибка просто упадёт повторно
            # (кейс kimi 17.09: первое сообщение на холодной вкладке падало
            # «JS во вкладке упал», дальше всё уезжало в 150-с таймауты).
            if attempt == 1:
                time.sleep(0.7)
                continue
            raise BrowserUnavailable("JS во вкладке упал")
        return str((res.get("result") or {}).get("value") or "")


def _raw_url(tab_id: int) -> str:
    tab = _raw_tab(tab_id)
    try:
        info = _raw_call("Target.getTargetInfo", {"targetId": tab["targetId"]},
                         pool=_pool_of_tab(tab))
    except RawCallTimeout:
        raise
    except BrowserUnavailable:
        _raw_drop(tab_id)
        raise BrowserUnavailable("фоновая вкладка закрыта")
    return str((info.get("targetInfo") or {}).get("url") or "")


def _raw_enter(tab_id: int):
    """Доверенный Enter (Input-домен) — как keyboard.press у playwright."""
    for ev_type in ("rawKeyDown", "keyUp"):
        _raw_tab_call(tab_id, "Input.dispatchKeyEvent",
                      {"type": ev_type, "key": "Enter", "code": "Enter",
                       "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})


def _raw_insert_text(tab_id: int, text: str):
    """Вставка текста IME-путём (Input.insertText) — для управляемых
    редакторов (Lexical на kimi.ai), откатывающих программный JS-fill."""
    _raw_tab_call(tab_id, "Input.insertText", {"text": text})


# Поле чата: значение (contenteditable → innerText)
_CHAT_FIELD_JS = (
    "(function(){var e=document.querySelector(%s);"
    "return e?(e.isContentEditable?e.innerText:e.value):'';})()"
)

# Ввод текста: native setter (React-совместимо) + input/change события
_CHAT_FILL_JS = (
    "(function(sel,text){"
    "var e=document.querySelector(sel);if(!e)return 'no-field';"
    "e.focus();"
    "if(e.isContentEditable){e.innerText=text;}"
    "else{var proto=(e instanceof HTMLTextAreaElement)?"
    "HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
    "Object.getOwnPropertyDescriptor(proto,'value').set.call(e,text);}"
    "e.dispatchEvent(new Event('input',{bubbles:true}));"
    "e.dispatchEvent(new Event('change',{bubbles:true}));"
    "return 'ok';})(%s,%s)"
)


def _raw_state(tab_id: int) -> _Probe:
    """Замер фоновой вкладки (аналог _page_state): ok=False — замер не
    удался, подтверждением эффекта это не считается."""
    try:
        return _probe_of(_raw_eval(tab_id, _DOM_STATE_JS))
    except Exception:
        return _Probe(False, "", "", "")


def _raw_chat_fill_send(tab_id: int, input_sel: str, text: str) -> str:
    """Ввод+отправка в фоновой вкладке: JS-fill + Enter, closed-loop
    подтверждение (поле очистилось/страница изменилась) как у playwright."""
    sel = json.dumps(input_sel, ensure_ascii=False)
    if _raw_eval(tab_id, _CHAT_FILL_JS % (sel, json.dumps(text, ensure_ascii=False))) != "ok":
        raise BrowserUnavailable("поле чата не приняло ввод")
    got = _raw_eval(tab_id, _CHAT_FIELD_JS % sel)
    if _norm_ws(text[:200]) in _norm_ws(got):
        # Откат управляемого редактора асинхронен: сразу после fill DOM
        # читается как новый текст, через доли секунды reconcile его стирает —
        # перепроверка после паузы ловит такой откат (кейс kimi 17.09: fill
        # «успешен», а Enter уходил в уже пустое поле — closed-loop «поле
        # очистилось» ложно подтверждал отправку).
        time.sleep(0.35)
        got = _raw_eval(tab_id, _CHAT_FIELD_JS % sel)
    if _norm_ws(text[:200]) not in _norm_ws(got):
        # Управляемые редакторы (Lexical — kimi.ai) откатывают JS-fill:
        # состояние редактора не из DOM, записанное стирается при reconcile.
        # Обход: выделение содержимого редактора (Selection API; execCommand —
        # страховка для textarea) + IME-вставка — она идёт через
        # editing-пайплайн редактора. Применяется асинхронно — короткий опрос.
        _raw_eval(tab_id,
                  "(function(){var e=document.querySelector(" + sel + ");"
                  "if(e){e.focus();"
                  "try{window.getSelection().selectAllChildren(e);}catch(x){}"
                  "document.execCommand('selectAll');}})()")
        _raw_insert_text(tab_id, text)
        ins_deadline = time.time() + 2.0
        while time.time() < ins_deadline:
            got = _raw_eval(tab_id, _CHAT_FIELD_JS % sel)
            if _norm_ws(text[:200]) in _norm_ws(got):
                break
            time.sleep(0.2)
    if _norm_ws(text[:200]) not in _norm_ws(got):
        raise BrowserUnavailable("поле чата не приняло текст")
    pre = _raw_state(tab_id)
    _raw_enter(tab_id)
    deadline = time.time() + SUBMIT_VERIFY_SEC
    while time.time() < deadline:
        try:
            cur = _raw_eval(tab_id, _CHAT_FIELD_JS % sel)
        except BrowserUnavailable:
            cur = ""
        if not cur.strip() or _effect_verdict(
                pre, _raw_state(tab_id)) == EFFECT_CHANGED:
            return "sent"
        time.sleep(0.25)
    raise FillUncertain("промпт введён, Enter нажат, но поле не "
                        "очистилось — не уверен, что ушло")


def _raw_wait_input(tab_id: int, selector: str, timeout_sec: float) -> bool:
    js = ("(function(){var e=document.querySelector(" + json.dumps(selector) + ");"
          "if(!e)return false;var s=getComputedStyle(e);"
          "var r=e.getBoundingClientRect();"
          "return s.display!=='none'&&s.visibility!=='hidden'"
          "&&r.width>2&&r.height>2;})()")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            if _raw_eval(tab_id, js) == "True":
                return True
        except BrowserUnavailable:
            return False
        time.sleep(0.3)
    return False


# Вставка картинки в композер чата синтетическим paste-событием: DataTransfer
# с File конструируется в странице — системный буфер обмена пользователя не
# трогаем, а «настоящий» Cmd+V фоновой вкладке недоступен (нет фокуса).
# Подтверждение — по ПРИРОСТУ признаков аттача после события (у qwen чип
# <img class=vision-item-image> в композере появляется за ~1-3с аплоада);
# baseline-дифф, чтобы существующая разметка не давала ложное срабатывание.
_CHAT_PASTE_IMAGE_JS = (
    "(function(sel,b64,mime,fname){"
    "var e=document.querySelector(sel);if(!e)return 'no-field';"
    "var box=e.closest('form,[class*=composer],[class*=Composer],"
    "[class*=input-wrapper],[class*=Input],[class*=editor],[class*=Editor]')"
    "||e.parentElement;"
    "function sig(){var n=0;"
    "var fis=document.querySelectorAll('input[type=file]');"
    "for(var i=0;i<fis.length;i++){if(fis[i].files&&fis[i].files.length)"
    "n+=fis[i].files.length;}"
    "n+=document.querySelectorAll('img[src^=\"blob:\"]').length;"
    "n+=document.querySelectorAll('[class*=attach] img,[class*=Attach] img,"
    "[class*=preview] img,[class*=Preview] img,[class*=file-card] img,"
    "[class*=FileCard] img,[class*=vision-item]').length;"
    "if(box)n+=box.querySelectorAll('img,canvas').length;"
    "return n;}"
    "var base=sig();"
    "e.focus();"
    "var bin=atob(b64),bytes=new Uint8Array(bin.length);"
    "for(var i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);"
    "var file=new File([bytes],fname,{type:mime});"
    "var dt=new DataTransfer();dt.items.add(file);"
    "var ev;"
    "try{ev=new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,"
    "cancelable:true});}catch(x){"
    "ev=new Event('paste',{bubbles:true,cancelable:true});"
    "try{Object.defineProperty(ev,'clipboardData',{value:dt});}catch(x2){}}"
    "e.dispatchEvent(ev);"
    "return new Promise(function(res){var t0=Date.now();"
    "(function poll(){"
    "if(sig()>base){res('attached');return;}"
    "if(Date.now()-t0>6000){res('no-attach');return;}"
    "setTimeout(poll,300);})();});"
    "})(%s,%s,%s,%s)"
)
# Сколько ждёт сам JS вставки (poll до 6с) — из него выводится потолок
# ответа CDP: константа сокета меньше бюджета страницы рвала соединение
CHAT_PASTE_WAIT_SEC = 6.0


def chat_paste_image(host_part: Optional[str], tab_id: Optional[int],
                     input_sel: str, image_bytes: bytes,
                     mime: str = "image/png") -> bool:
    """Вставить картинку в поле чата веб-LLM (синтетический paste — см. JS
    выше). mime — реальный тип байтов: имя и тип File в paste-событии
    ставятся по нему (jpeg легче для аплоада). True — сайт подтвердил
    аттач; False — поле не найдено или сайт paste проигнорировал (тогда
    вызывающий честно падает в фолбэк)."""
    if not image_bytes:
        return False
    import base64
    fname = "image.jpg" if mime == "image/jpeg" else "image.png"
    b64 = base64.b64encode(image_bytes).decode()
    js = _CHAT_PASTE_IMAGE_JS % (json.dumps(input_sel, ensure_ascii=False),
                                 json.dumps(b64),
                                 json.dumps(mime),
                                 json.dumps(fname))

    try:
        out = _eval_in_tab(host_part, tab_id, js,
                           timeout_sec=CHAT_PASTE_WAIT_SEC,
                           backends=("cdp",))
    except Exception as e:
        logger.info(f"[BrowserActions] Вставка картинки в чат не удалась: {e}")
        return False
    return str(out) == "attached"


# Ожидание окончания аплоада аттачей: у qwen и др. между появлением аттача
# в композере и концом загрузки на сервер проходит заметное время; «отправить»
# в это окно даёт только тост «files still uploading», а сообщение теряется.
# Маркеры «загрузка идёт»: класс *uploading* (qwen: vision-item-container-
# uploading), прогресс-бар. Маркера нет в первые ~2.5с — сайт без явного
# индикатора, не блокируем отправку
_CHAT_WAIT_UPLOADED_JS = (
    "(function(sel,timeoutMs){"
    "var e=document.querySelector(sel);if(!e)return 'no-field';"
    "var BUSY='[class*=\"uploading\" i],[class*=\"upload-progress\" i],"
    "[role=\"progressbar\"]';"
    "var t0=Date.now(),seen=false,clean=0;"
    "return new Promise(function(res){"
    "(function poll(){"
    "if(document.querySelector(BUSY)){seen=true;clean=0;}"
    "else{clean++;"
    "if(seen&&clean>=2){res('ready');return;}"
    "if(!seen&&Date.now()-t0>2500){res('ready');return;}}"
    "if(Date.now()-t0>timeoutMs){res(seen?'timeout':'ready');return;}"
    "setTimeout(poll,300);})();});"
    "})(%s,%d)"
)


def chat_wait_uploaded(host_part: Optional[str], tab_id: Optional[int],
                       input_sel: str, timeout: float = 25.0) -> bool:
    """Ждать окончания аплоада аттачей перед отправкой (см. JS выше).
    True — можно слать; False — поле не найдено или аплоад висит дольше
    timeout (вызывающий решает, слать ли с риском тоста)."""
    js = _CHAT_WAIT_UPLOADED_JS % (json.dumps(input_sel, ensure_ascii=False),
                                   int(timeout * 1000))

    try:
        # Потолок ответа CDP выводится из бюджета ожидания В СТРАНИЦЕ: ждём
        # там до timeout, и сокет не должен падать раньше (кейс: на долгих
        # аплоадах обрыв рвал соединение и терял сообщение)
        out = _eval_in_tab(host_part, tab_id, js, timeout_sec=float(timeout),
                           backends=("cdp",))
    except BackendUnsupported as e:
        # Транспорта для проверки нет (AppleScript/Safari) — это не «аплоад
        # висит»: отправку не блокируем. Раньше отличалось по подстроке
        # текста ошибки, теперь по типу (см. BackendUnsupported)
        if not is_raw_tab(tab_id):
            logger.debug(f"[BrowserActions] Аплоад не проверить: {e}")
            return True
        return False
    except BrowserUnavailable as e:
        logger.info(f"[BrowserActions] Ожидание аплоада не удалось: {e}")
        return False
    except Exception as e:
        logger.info(f"[BrowserActions] Ожидание аплоада не удалось: {e}")
        return False
    return str(out) not in ("no-field", "timeout")


# Транзитные ошибки шлюза/CDN: страница — заглушка «Bad Gateway», а свежее
# открытие (новое соединение) лечит. 500 не входит: это ошибка приложения,
# переоткрытием не чинится
_GATEWAY_STATUSES = (502, 503, 504)


def _gateway_status(page, budget_sec: float = GATEWAY_PROBE_SEC) -> Optional[int]:
    """HTTP-статус основной навигации страницы (PerformanceNavigationTiming.
    responseStatus — Chrome 100+). Возвращает 502/503/504, если навигация
    ответила ошибкой шлюза; None — статус здоровый или не прочитался
    (старый браузер, eval недоступен, SPA-переход). Статус появляется
    только С ОТВЕТОМ сервера — опрашиваем до budget_sec (кейс 19.09: dodo
    отдала 502 за 10.5с — двухсекундное окно закрывалось задолго до ответа, и
    переоткрытие не срабатывало). На здоровых страницах статус известен
    <1с — опрос завершается сразу, общий путь не тормозится. Бюджет ЯВНЫЙ:
    опрос идёт в потоке воркера и держит его лок (см. GATEWAY_*)."""
    deadline = time.monotonic() + float(budget_sec)
    while True:
        try:
            st = page.evaluate(
                "(() => { const n = performance.getEntriesByType"
                "('navigation')[0]; return n && n.responseStatus || 0; })()")
        except Exception:
            return None  # eval недоступен — не мешаем открытию
        if st:
            st = int(st)
            return st if st in _GATEWAY_STATUSES else None
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def _open_page_gateway_retry(worker, ctx, url: str):
    """Открыть страницу через worker._new_page_quiet; если первая навигация
    ответила ошибкой шлюза 502/503/504 (заглушка CDN вместо сайта — кейс
    18.09: dodo открывалась в 502, а «закрыть и открыть заново» лечило) —
    закрыть вкладку и открыть заново, ОДИН раз. Повторная ошибка —
    оставляем как есть (это уже состояние сайта, а не транзит).
    → (page, quiet) как у _new_page_quiet."""
    page, quiet = worker._new_page_quiet(ctx, url)
    st = _gateway_status(page)
    if not st:
        return page, quiet
    logger.info(f"[BrowserActions] {url[:60]} ответил {st} — "
                "переоткрываю вкладку заново")
    try:
        page.close()
    except Exception:
        pass
    page, quiet = worker._new_page_quiet(ctx, url)
    # Перепроверка — только для лога (второй раз не переоткрываем), поэтому
    # короткий бюджет: держать воркер ещё 15с незачем
    st2 = _gateway_status(page, budget_sec=GATEWAY_RECHECK_SEC)
    if st2:
        logger.warning(f"[BrowserActions] {url[:60]} снова {st2} после "
                       "переоткрытия — оставляю как есть")
    return page, quiet


def open_new_tab(url: str, background: bool = False, pool: str = _POOL_V,
                 focus: bool = False) -> int:
    """Новая вкладка с url → стабильный id для адресации следующих команд
    («на этой странице …»). CDP: id из реестра воркера; macOS fallback:
    AppleScript-id (Chrome) или наш реестр по URL (Safari).
    Операция открытия — браузер разрешено запустить. Обычное открытие
    (background=False) на CDP/macOS делает вкладку АКТИВНОЙ в её окне,
    но окно не всплывает: «открой сайт» готовит вкладку, не дёргая
    пользователя (см. _select_browser_tab_quietly). focus=True — наоборот:
    вкладка И окно выходят на передний план (пользователь просил страницу —
    переключение и есть смысл команды; см. _focus_browser_tab). Для
    background-служебных вкладок focus игнорируется.
    background=True — вкладка в фоне БЕЗ выдёргивания окна (сырой CDP;
    только eval/навигация/ввод в чат) и без переключения на неё. Для
    служебных вкладок web_llm. pool: H — headless Chrome веб-чатов
    (дефолт у web_llm), V — headed (команды управления, headed-сайты).
    AppleScript-фолбэка для background нет намеренно: открытая им вкладка
    неуправляема (JS/ввод недоступны) и веб-чат всё равно упадёт — лучше
    сразу понятная ошибка, чем мусорная вкладка (кейс 22.08). Safari:
    фоновых вкладок нет — открываем обычную видимую."""
    if background:
        # Бэкенд выбираем БЕЗ запуска пула V (старое _select_backend(
        # tab_op=False) поднимало headed Chrome даже для вкладок пула H —
        # лишний видимый браузер, кейс 14.09). Каждый пул поднимает себя
        # сам: H — в _ensure_pool_h_browser, V — в fallback'е ниже.
        backend = str(_BCFG.get("backend") or "auto").lower()
        if backend == "safari":
            logger.info("[BrowserActions] Safari: фоновых вкладок нет — "
                        "служебная вкладка будет видимой")
            return _safari_open_tab(url)
        if backend not in ("auto", "cdp"):
            raise BrowserUnavailable(
                "веб-чат требует CDP: браузер с отладкой недоступен, "
                "а открытая через AppleScript вкладка неуправляема")
        try:
            return _raw_open(url, pool=pool)
        except Exception as e:
            if pool == _POOL_H:
                if not pool_v_webchat_allowed():
                    # Режим управления выключен — headed Chrome не поднимаем
                    # даже ради fallback'а: честная ошибка, цепочка идёт дальше
                    logger.info(f"[BrowserActions] пул H недоступен ({e}), "
                                "пул V без режима управления выключен")
                    raise BrowserUnavailable(
                        f"headless-браузер бота недоступен: {e}")
                # Пул H не поднялся (не найден браузер и т.п.) — деградируем
                # в пул V, чтобы веб-чат продолжил работать как раньше
                logger.info(f"[BrowserActions] пул H недоступен ({e}) — "
                            "вкладка в пуле V")
                try:
                    return _raw_open(url, pool=_POOL_V)
                except Exception as e2:
                    e = e2
            logger.info(f"[BrowserActions] фоновое открытие не сработало "
                        f"({e}) — обычная вкладка")
        # Fallback: видимая вкладка в пуле V — его Chrome worker запустит сам,
        # и только когда он действительно нужен (headed-сайт)
        return _WORKER.submit(lambda w: w.new_page(url),
                              timeout=SUBMIT_NAV_TIMEOUT_SEC)
    backend = _select_backend(tab_op=False)
    if backend == "cdp":
        return _WORKER.submit(lambda w: w.new_page(url, focus=focus),
                              timeout=SUBMIT_NAV_TIMEOUT_SEC)
    if backend == "safari":
        return _safari_open_tab(url, focus=focus)
    return _open_tab_applescript(url, focus=focus)


def page_urls() -> List[str]:
    """URL всех живых страниц — снимок «до клика» для детекта попапа.
    Не-CDP бэкенд (Chrome): пустой список (попапы там не отслеживаем);
    Safari: список вкладок через Apple Events."""
    backend = _select_backend(tab_op=True)
    if backend == "cdp":
        return _WORKER.submit(lambda w: [p.url for p in w._all_pages()])
    if backend == "safari":
        return _safari_page_urls()
    return []


def _front_window_url() -> str:
    """URL активной вкладки переднего окна Chrome (macOS, AppleScript) —
    ровно то, на что смотрит пользователь. ЕДИНЫЙ источник «видимой»
    вкладки: им пользуются и visible_page_info() (подпись действия на
    резолве), и _CdpWorker.page_for_user_visible() (исполнение) — два
    разных источника давали рассинхрон (кейс 10.09: «закрой вкладку»
    подписалось «YouTube», а закрыло платформу — CDP visibilityState у
    Chrome 152 'visible' у ВСЕХ вкладок окна). Пустая строка — не macOS,
    нет прав автоматизации или окна, ЛИБО ответ пришёл не от браузера бота
    (см. ниже)."""
    if sys.platform != "darwin":
        return ""
    try:
        url = _as_run(_as_tell(
            "  try\n"
            "    return URL of active tab of front window\n"
            "  end try\n")).strip()
    except Exception:
        return ""
    if not url or _as_foreign_instance(url):
        # Ответ личного браузера пользователя (см. _as_foreign_instance):
        # выдавать его вкладку за «видимую страницу» бота нельзя. Молчим —
        # вызывающий падает на CDP-эвристику, она всегда про браузер бота
        return ""
    return url


def visible_page_info() -> Optional[Tuple[str, str]]:
    """(url, host) вкладки, на которую смотрит пользователь.
    Источник истины на macOS — активная вкладка переднего окна через
    AppleScript: CDP-эвристика document.visibilityState === 'visible'
    ненадёжна — Chrome отдаёт 'visible' ВСЕМ вкладкам окна (замерено на
    Chrome 152: 9 вкладок, одно окно, visible у всех; кейс 10.09 — «нажми
    три полоски» на платформе уходило на youtube, т.к. «видимой» считалась
    последняя вкладка). На не-macOS — visibilityState-эвристика CDP (при
    нескольких окнах — окно с фокусом, иначе свежее).
    Служебные веб-чаты и вкладка чата бота — не кандидаты. None — браузер
    недоступен, активная вкладка служебная или окна нет."""
    try:
        backend = _select_backend(tab_op=True)
    except Exception:
        return None
    if sys.platform == "darwin" and backend in ("cdp", "applescript"):
        # Активная вкладка переднего окна — ровно то, на что смотрит
        # пользователь; без этого выбора команда без сайта («нажми три
        # полоски» на платформе при открытом ранее ютубе, кейс 09.09)
        # уходила в ОТСЛЕЖИВАЕМУЮ вкладку (_last_tab_id) вместо видимой.
        # Работает и при CDP-подключении (тот же Chrome), поэтому идёт
        # первым, а visibilityState — только как запасной путь ниже
        url = _front_window_url()
        if url:
            if _chat_or_service_url(url):
                return None
            return url, (urlparse(url).hostname or "")
        if backend != "cdp":
            return None
        # AppleScript молчит (нет прав автоматизации?) — фолбэк на CDP ниже
    elif backend != "cdp":
        return None

    def _op(w):
        cands = [p for p in w._all_pages()
                 if not _chat_or_service_url(p.url)]
        vis = w._visible_of(cands)
        if vis is None:
            return None
        return vis.url, (urlparse(vis.url).hostname or "")

    try:
        return _WORKER.submit(_op)
    except Exception:
        return None


# ── Чтение текста со страницы («прочитай последнее сообщение») ──

_READ_LAST_JS = (
    # Последнее сообщение чата: claude.ai (строки group/message-row: ответ —
    # p.font-claude-response-body, своё — [data-testid=user-message]),
    # chatgpt ([data-message-author-role]), иначе последний видимый блок
    # [class*=message]. Формат ответа: «роль|текст»
    "(function(){"
    "function cl(e){return (e.innerText||'').replace(/\\s+/g,' ').trim();}"
    "function vis(e){var s=getComputedStyle(e);var r=e.getBoundingClientRect();"
    "return s.display!=='none'&&s.visibility!=='hidden'&&r.width>=2&&r.height>=8;}"
    "var rows=document.querySelectorAll('div.group\\\\/message-row'),i,t,parts;"
    "for(i=rows.length-1;i>=0;i--){"
    "  if(!vis(rows[i]))continue;"
    "  var um=rows[i].querySelector('[data-testid=user-message]');"
    "  if(um){t=cl(um);if(t)return 'пользователь|'+t;}"
    "  var ps=rows[i].querySelectorAll('.font-claude-response-body');"
    "  parts=[];for(var k=0;k<ps.length;k++){var pt=cl(ps[k]);if(pt)parts.push(pt);}"
    "  t=parts.join(' ');if(t)return 'assistant|'+t;"
    "}"
    "var cg=document.querySelectorAll('[data-message-author-role]');"
    "for(i=cg.length-1;i>=0;i--){if(!vis(cg[i]))continue;t=cl(cg[i]);"
    "  if(t)return (cg[i].getAttribute('data-message-author-role')||'assistant')+'|'+t;}"
    "var ms=document.querySelectorAll('[class*=message],[class*=Message]'),last='';"
    "for(i=0;i<ms.length;i++){var e=ms[i];if(!vis(e))continue;"
    "  if(e.querySelector('[class*=message],[class*=Message]'))continue;"
    "  t=cl(e);if(t.length>=2)last=t;}"
    "return last?'|'+last:'';})()"
)

_READ_PAGE_JS = (
    # IIFE — как у _SNAPSHOT_JS: голый топ-левел «var e» падает с SyntaxError
    # на страницах, где сайт сам объявил «let e» (минифицированные бандлы)
    "(function(){"
    "var e=document.querySelector('main,article,[role=main]')||document.body;"
    "return (e?(e.innerText||''):'').replace(/\\n{3,}/g,'\\n\\n').trim()"
    ".slice(0,3000);"
    "})()"
)


_ENTER_TARGET_JS = (
    # Цель для Enter («отправь» без «введи»): единственное видимое НЕПУСТОЕ
    # поле (туда уже что-то ввели); иначе поле в фокусе; иначе единственное
    # поле страницы. Метим атрибутом — сам Enter жмёт playwright (доверенное
    # key-событие; синтетический KeyboardEvent фреймворки игнорируют)
    "(function(){"
    "var edsel='textarea,input:not([type]),input[type=text],input[type=search],input[type=email],"
    "input[type=tel],input[type=url],input[type=number],input[type=password],"
    "[role=textbox],[role=searchbox],[role=combobox],"
    "[contenteditable]:not([contenteditable=false])';"
    "document.querySelectorAll('[data-vpc-enter]').forEach(function(e){e.removeAttribute('data-vpc-enter')});"
    "function vis(e){var s=getComputedStyle(e);var r=e.getBoundingClientRect();"
    "return s.display!=='none'&&s.visibility!=='hidden'&&r.width>=2&&r.height>=2;}"
    "function val(e){return (e.isContentEditable?e.innerText:(e.value||'')).trim();}"
    "var els=[].slice.call(document.querySelectorAll(edsel)).filter(vis);"
    "if(!els.length)return 'нет полей ввода';"
    "var ne=els.filter(function(e){return val(e)!=='';});"
    "var t=null;"
    "if(ne.length===1)t=ne[0];"
    "if(!t&&document.activeElement&&els.indexOf(document.activeElement)>=0)t=document.activeElement;"
    "if(!t&&els.length===1)t=els[0];"
    "if(!t)return 'ambiguous:'+els.length;"
    "t.setAttribute('data-vpc-enter','1');return 'ok';})()"
)


def navigate_tab(url: str, host_part: Optional[str] = None,
                 tab_id: Optional[int] = None) -> None:
    """Навигация УЖЕ открытой вкладки на url (без выдёргивания на передний
    план). Для web_llm: свежий чат — это возврат служебной вкладки на home."""
    if is_raw_tab(tab_id):
        _raw_tab_call(int(tab_id), "Page.navigate", {"url": url})
        return
    backend = _select_backend(tab_op=True)
    if backend == "safari":
        _safari_navigate(tab_id, host_part, url)
        return
    if backend != "cdp":
        raise _no_backend("навигация вкладки")

    def _op(w):
        page = w.page_for(host_part, tab_id)
        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=int(NAV_GOTO_TIMEOUT_SEC * 1000))
        except Exception:
            pass  # догружается в фоне — готовность ждёт читающая сторона

    return _WORKER.submit(_op, timeout=SUBMIT_NAV_TIMEOUT_SEC)


def _chat_fill_send_loc(page, loc, text: str) -> str:
    """Ввод в поле чата по готовому locator'у + Enter: fill (мгновенно,
    посимвольный набор длинных промптов не тянет) с обходом управляемых
    редакторов (Lexical откатывает fill — вставка через IME-путь), затем
    Enter и closed-loop (поле очистилось/страница изменилась)."""
    try:
        loc.click(timeout=CLICK_TIMEOUT_MS)
        loc.fill(text, timeout=CLICK_TIMEOUT_MS)
    except Exception as e:
        detail = str(e).split("Call log")[0].strip().split("\n")[0]
        raise BrowserUnavailable(f"поле чата не приняло ввод: {detail[:100]}")
    got = str(loc.evaluate(
        "e => e.isContentEditable ? e.innerText : e.value") or "")
    if _norm_ws(text[:200]) not in _norm_ws(got):
        # Управляемые редакторы (Lexical — kimi.ai) откатывают fill():
        # их состояние не из DOM, заполненное содержимое стирается
        # при reconcile. Обход — вставка через IME-путь
        # (Input.insertText): проходит через editing-пайплайн редактора.
        # selectAll перед вставкой — заменить возможный частичный fill.
        try:
            loc.evaluate("e => { e.focus();"
                         " document.execCommand('selectAll'); }")
            page.keyboard.insert_text(text)
            ins_deadline = time.time() + 2.0
            while time.time() < ins_deadline:
                got = str(loc.evaluate(
                    "e => e.isContentEditable ? e.innerText : e.value") or "")
                if _norm_ws(text[:200]) in _norm_ws(got):
                    break
                time.sleep(0.2)  # редактор применяет вставку асинхронно
        except Exception as e:
            detail = str(e).split("Call log")[0].strip().split("\n")[0]
            raise BrowserUnavailable(
                f"поле чата не приняло текст: {detail[:100]}")
    # fill() может обрезать под лимит поля — сверяем по началу текста
    if _norm_ws(text[:200]) not in _norm_ws(got):
        raise BrowserUnavailable("поле чата не приняло текст")
    pre = _page_state(page)
    try:
        loc.press("Enter", timeout=CLICK_TIMEOUT_MS)
    except Exception as e:
        detail = str(e).split("Call log")[0].strip().split("\n")[0]
        raise BrowserUnavailable(f"Enter не нажался: {detail[:100]}")
    deadline = time.time() + SUBMIT_VERIFY_SEC
    while time.time() < deadline:
        try:
            cur = str(loc.evaluate(
                "e => e.isContentEditable ? e.innerText : e.value") or "")
        except Exception:
            cur = ""
        if not cur.strip() or _effect_verdict(
                pre, _page_state(page)) == EFFECT_CHANGED:
            return "sent"
        time.sleep(0.25)
    raise FillUncertain("промпт введён, Enter нажат, но поле не "
                        "очистилось — не уверен, что ушло")


def chat_fill_send(host_part: Optional[str], tab_id: Optional[int],
                   input_sel: str, text: str) -> str:
    """Быстрый ввод в поле чата по селектору адаптера (fill — мгновенно,
    посимвольный набор для длинных промптов не годится) + Enter. Отправка
    подтверждается: поле очистилось или страница изменилась."""
    text = str(text or "").strip()
    if not text:
        raise BrowserUnavailable("пустой текст — нечего отправлять")
    if is_raw_tab(tab_id):
        return _raw_chat_fill_send(int(tab_id), input_sel, text)
    backend = _select_backend(tab_op=True)
    if backend == "safari":
        return _safari_chat_fill_send(host_part, tab_id, input_sel, text)
    if backend != "cdp":
        raise _no_backend("chat-ввод")

    def _op(w):
        page = w.page_for(host_part, tab_id)
        loc = page.locator(input_sel).first
        return _chat_fill_send_loc(page, loc, text)

    return _WORKER.submit(_op)


def chat_fill_send_tagged(host_part: Optional[str], tab_id: Optional[int],
                          idx: int, text: str) -> str:
    """То же, но по номеру разметки снапшота вместо CSS-селектора —
    goal-фолбэк web_llm, когда селектор адаптера протух после редизайна.
    Только CDP (метку ставит snapshot_elements, сырой/сафари-путь её не
    имеет)."""
    text = str(text or "").strip()
    if not text:
        raise BrowserUnavailable("пустой текст — нечего отправлять")
    if is_raw_tab(tab_id):
        # Метка снапшота — playwright-примитив; отказ должен быть ЯВНЫМ,
        # а не тихим промахом по полю на чужой вкладке
        raise RawTabUnsupported(
            "ввод по метке снапшота на фоновой вкладке недоступен — "
            "разметку ставит playwright, а он её не видит")
    if _select_backend(tab_op=True) != "cdp":
        raise _no_backend("chat-ввод по метке снапшота")

    def _op(w):
        page = w.page_for(host_part, tab_id)
        loc, _scope = _locator_any_frame(page, idx)
        if loc is None:
            raise BrowserUnavailable(
                "поле чата по метке снапшота не нашлось")
        return _chat_fill_send_loc(page, loc, text)

    return _WORKER.submit(_op)


# JS-фрагменты чтения блоков веб-чатов: видимость элемента и снятие текста
# с сохранением markdown (innerText теряет **жирный**/*курсив*/списки).
# Общие для last_block_text и answer_blocks_after.
_VIS_JS = ("function vis(e){var st=getComputedStyle(e);var r=e.getBoundingClientRect();"
           "return st.display!=='none'&&st.visibility!=='hidden'&&r.width>=2&&r.height>=2;}")
_MD_JS = ("function md(node,pre){var out='';"
          "for(var i=0;i<node.childNodes.length;i++){var ch=node.childNodes[i];"
          "if(ch.nodeType===3){out+=pre?ch.nodeValue:ch.nodeValue.replace(/\\s+/g,' ');continue;}"
          "if(ch.nodeType!==1)continue;var tag=ch.tagName.toLowerCase();"
          "if(tag==='script'||tag==='style'||tag==='button'||tag==='svg')continue;"
          "if(tag==='br'){out+='\\n';continue;}"
          "var inner=md(ch,pre||tag==='pre');var t;"
          "if(tag==='pre'){out+='\\n```\\n'+inner.replace(/\\n+$/,'')+'\\n```\\n';continue;}"
          "if(tag==='strong'||tag==='b'){t=inner.trim();out+=t?'**'+t+'**':'';continue;}"
          "if(tag==='em'||tag==='i'){t=inner.trim();out+=t?'*'+t+'*':'';continue;}"
          "if(tag==='code'&&!pre){t=inner.trim();out+=t?'`'+t+'`':'';continue;}"
          "if(tag==='li'){out+='\\n- '+inner.trim();continue;}"
          "if(tag==='ul'||tag==='ol'){out+='\\n'+inner+'\\n';continue;}"
          "if(/^h[1-6]$/.test(tag)){t=inner.trim();out+=t?'\\n**'+t+'**\\n\\n':'';continue;}"
          "if(tag==='blockquote'){out+='\\n> '+inner.trim()+'\\n\\n';continue;}"
          "if(tag==='p'||tag==='div'){out+=inner+'\\n\\n';continue;}"
          "out+=inner;}"
          "return out;}")
# Хвосты нормализации снятого текста (общие у обоих читателей)
_MD_TAIL_JS = ".replace(/[ \\t]+\\n/g,'\\n').replace(/\\n{3,}/g,'\\n\\n').trim()"
_TEXT_TAIL_JS = ".replace(/\\n{3,}/g,'\\n\\n').trim()"
# Клон с вырезанными exclude-узлами: читаем текст без служебных блоков
# (цепочки рассуждений z.ai и т.п. — md() не смотрит на видимость узлов,
# поэтому свёрнутый блок всё равно попадал бы в текст). Текст снимается с
# отсоединённого клона: markdown-разбор структурный и не пострадает, а вот
# plain-innerText на клоне теряет layout-переносы — exclude рассчитан на
# markdown-чтение (web_llm читает ответы с markdown=True).
_EXCL_JS = ("if(excl){var c=e.cloneNode(true);var xs=c.querySelectorAll(excl);"
            "for(var j=0;j<xs.length;j++)xs[j].remove();}else{var c=e;}")


def last_block_text(host_part: Optional[str], tab_id: Optional[int],
                    selectors: List[str], markdown: bool = False,
                    exclude: Optional[str] = None) -> str:
    """Текст последнего видимого блока по первому непустому селектору из
    списка (ответ ассистента в веб-чате). Без усечения — лимит решает caller.
    markdown=True — сохранить форматирование как markdown (**жирный**,
    *курсив*, `код`, списки, преформат): innerText его безвозвратно теряет.
    Пайплайн markdown пропускает (_strip_markdown снимает только
    заголовки/картинки), фронт и TG его рендерят.
    exclude — CSS-селектор служебных подузлов, вырезаемых из текста
    (цепочки рассуждений и т.п.)."""
    js = ("(function(){var sels=" + json.dumps(selectors) + ";"
          "var useMd=" + ("true" if markdown else "false") + ";"
          "var excl=" + json.dumps(exclude) + ";"
          + _VIS_JS + _MD_JS +
          "for(var s=0;s<sels.length;s++){var els=document.querySelectorAll(sels[s]);"
          "for(var i=els.length-1;i>=0;i--){var e=els[i];if(!vis(e))continue;"
          + _EXCL_JS +
          "var t=useMd?md(c,false)" + _MD_TAIL_JS +
          ":(c.innerText||'')" + _TEXT_TAIL_JS + ";"
          "if(t)return t;}}"
          "return '';})()")
    return _eval_in_tab(host_part, tab_id, js)


def answer_blocks_after(host_part: Optional[str], tab_id: Optional[int],
                        user_selectors: List[str], answer_selectors: List[str],
                        marker: str, markdown: bool = False,
                        exclude: Optional[str] = None,
                        done_selector: Optional[str] = None):
    """Блоки ответа ПОСЛЕ конкретного нашего сообщения (якорь web_llm).

    marker — нормализованное начало отправленного промпта; якорь — последний
    видимый user-блок, его содержащий. Возвращает (count, text_last, done):
    count=None — якорь не найден (лента виртуализована?) — caller уходит на
    baseline-путь; count=0 — наше сообщение есть, ответ ещё не начался.
    done — у последнего блока есть маркер завершения генерации
    (done_selector — напр. ряд кнопок copy у kimi; None — всегда True):
    без него плейсхолдер «-» на сбойной генерации залипал как «стабильный
    ответ». Без якоря при медленном рендере истории (SPA грузит ленту после
    поля ввода) старый завершённый ответ выглядел «новым» и возвращался
    вместо настоящего (кейс 22.08: реформулировка coref вместо ответа)."""
    js = ("(function(){var userSels=" + json.dumps(user_selectors) + ";"
          "var ansSels=" + json.dumps(answer_selectors) + ";"
          "var marker=" + json.dumps(marker) + ";"
          "var useMd=" + ("true" if markdown else "false") + ";"
          "var excl=" + json.dumps(exclude) + ";"
          "var doneSel=" + json.dumps(done_selector) + ";"
          + _VIS_JS + _MD_JS +
          "function norm(s){return (s||'').replace(/\\s+/g,' ').toLowerCase();}"
          # Маркер завершения: ищем видимый doneSel среди предков блока
          # (ряд действий ответа лежит в контейнере сообщения)
          "function hasDone(el){if(!doneSel)return true;var p=el;"
          "for(var u=0;u<6&&p;u++,p=p.parentElement){"
          "var ds=p.querySelector?p.querySelector(doneSel):null;"
          "if(ds&&vis(ds))return true;}return false;}"
          "var anchor=null;"
          "for(var s=0;s<userSels.length&&!anchor;s++){"
          "var us=document.querySelectorAll(userSels[s]);"
          "for(var i=us.length-1;i>=0;i--){var e=us[i];if(!vis(e))continue;"
          "if(norm(e.innerText).indexOf(marker)>=0){anchor=e;break;}}}"
          "if(!anchor)return JSON.stringify({found:false,count:0,text:'',done:false});"
          "for(var s=0;s<ansSels.length;s++){"
          "var els=document.querySelectorAll(ansSels[s]);var n=0,txt='',done=false;"
          "for(var i=0;i<els.length;i++){var e=els[i];"
          # Node.DOCUMENT_POSITION_PRECEDING (2): anchor предшествует e
          "if(!(e.compareDocumentPosition(anchor)&2))continue;"
          "if(!vis(e))continue;if(!((e.innerText||'').trim()))continue;n++;"
          + _EXCL_JS +
          "var t=useMd?md(c,false)" + _MD_TAIL_JS +
          ":(c.innerText||'')" + _TEXT_TAIL_JS + ";"
          "if(t){txt=t;done=hasDone(e);}}"
          "if(n)return JSON.stringify({found:true,count:n,text:txt,done:done});}"
          "return JSON.stringify({found:true,count:0,text:'',done:false});})()")
    try:
        data = json.loads(_eval_in_tab(host_part, tab_id, js) or "{}")
    except (TypeError, ValueError):
        return None, "", False
    if not data.get("found"):
        return None, "", False
    return (int(data.get("count") or 0), str(data.get("text") or ""),
            bool(data.get("done")))


def tab_url(host_part: Optional[str] = None, tab_id: Optional[int] = None) -> str:
    """Текущий URL вкладки. Для web_llm: после первого сообщения чат
    получает постоянный адрес — запоминаем его как «наш чат»."""
    if is_raw_tab(tab_id):
        return _raw_url(int(tab_id))
    backend = _select_backend(tab_op=True)
    if backend == "safari":
        try:
            return _safari_tab_url(tab_id, host_part)
        except BrowserUnavailable:
            return ""
    if backend != "cdp":
        return ""
    return _WORKER.submit(lambda w: str(w.page_for(host_part, tab_id).url))


def eval_js(host_part: Optional[str], tab_id: Optional[int], js: str,
            timeout_sec: Optional[float] = None) -> str:
    """Произвольный JS во вкладке (служебные UI-операции web_llm вроде
    переключения режима чата). CDP или Safari, без выдёргивания на передний
    план. evaluate ждёт промисы — асинхронные сценарии возвращают Promise;
    ждущим в странице сценариям бюджет задаётся timeout_sec (из него
    выводится потолок ответа транспорта)."""
    return _eval_in_tab(host_part, tab_id, js, timeout_sec=timeout_sec,
                        backends=("cdp", "safari"))


def wait_input(host_part: Optional[str], tab_id: Optional[int],
               selector: str, timeout_sec: float = 8.0) -> bool:
    """Дождаться видимого поля ввода (web_llm: первая загрузка чата может
    рендериться дольше пары секунд). False по таймауту — не исключение:
    дальше отработает closed-loop отправки."""
    if is_raw_tab(tab_id):
        return _raw_wait_input(int(tab_id), selector, timeout_sec)
    backend = _select_backend(tab_op=True)
    if backend == "safari":
        return _safari_wait_input(host_part, tab_id, selector, timeout_sec)
    if backend != "cdp":
        return False

    def _op(w):
        page = w.page_for(host_part, tab_id)
        try:
            page.wait_for_selector(selector, state="visible",
                                   timeout=int(timeout_sec * 1000))
            return True
        except Exception:
            return False

    # Бюджет submit'а выводится из ожидания в странице, а не из константы
    return _WORKER.submit(_op, timeout=float(timeout_sec) + SUBMIT_MARGIN_SEC)


def count_blocks(host_part: Optional[str], tab_id: Optional[int],
                 selectors: List[str]) -> int:
    """Число видимых непустых блоков по первому совпавшему селектору (та же
    логика видимости, что у last_block_text). Для web_llm: счётчик ДО
    отправки — в непрерывном чате ждём ПОЯВЛЕНИЯ нового блока ответа."""
    js = ("(function(){var sels=" + json.dumps(selectors) + ";"
          "for(var s=0;s<sels.length;s++){var els=document.querySelectorAll(sels[s]);"
          "var n=0;for(var i=0;i<els.length;i++){var e=els[i];"
          "var r=e.getBoundingClientRect();var st=getComputedStyle(e);"
          "if(st.display==='none'||st.visibility==='hidden'||r.width<2||r.height<2)continue;"
          "if(!((e.innerText||'').trim()))continue;n++;}"
          "if(n)return n;}"
          "return 0;})()")
    try:
        return int(_eval_in_tab(host_part, tab_id, js) or 0)
    except (TypeError, ValueError):
        return 0


def press_enter(host_part: Optional[str], tab_id: Optional[int] = None) -> str:
    """Enter в поле ввода — standalone «отправь»/«send». Цель выбирает JS
    (непустое поле / фокусное / единственное), нажатие — playwright (реальное
    key-событие). Closed-loop отправки — как у «введи … и отправь»: поле
    очистилось или страница изменилась, иначе FillUncertain."""
    backend = _select_backend(tab_op=True)
    if backend == "safari":
        # Цель — тот же JS; нажатие — доверенный Enter через System Events
        out = _safari_exec(host_part, _ENTER_TARGET_JS, tab_id)
        if out != "ok":
            raise BrowserUnavailable(
                "не понял, в какое поле жать Enter — несколько полей, "
                "кликни нужное и повтори" if out.startswith("ambiguous:")
                else "на странице нет полей ввода")
        pre = _safari_exec(
            host_part,
            "(function(){var e=document.querySelector('[data-vpc-enter]');"
            "return e?(e.isContentEditable?e.innerText:e.value):'';})()",
            tab_id)
        _safari_enter(tab_id, host_part)
        deadline = time.time() + SUBMIT_VERIFY_SEC
        while time.time() < deadline:
            cur = _safari_exec(
                host_part,
                "(function(){var e=document.querySelector('[data-vpc-enter]');"
                "return e?(e.isContentEditable?e.innerText:e.value):'';})()",
                tab_id)
            if not cur.strip() or _norm_ws(cur) != _norm_ws(pre):
                return "sent"
            time.sleep(0.25)
        raise FillUncertain(
            "Enter нажат, но поле не очистилось — не уверен, что отправилось")
    if backend != "cdp":
        raise _no_backend("«отправь» (Enter)")

    def _op(w):
        page = w.page_for(host_part, tab_id)
        out = str(page.evaluate(_ENTER_TARGET_JS) or "")
        if out != "ok":
            raise BrowserUnavailable(
                "не понял, в какое поле жать Enter — несколько полей, "
                "кликни нужное и повтори" if out.startswith("ambiguous:")
                else "на странице нет полей ввода")
        loc = page.locator("[data-vpc-enter='1']")
        pre = _page_state(page)
        try:
            loc.first.press("Enter", timeout=CLICK_TIMEOUT_MS)
        except Exception as e:
            detail = str(e).split("Call log")[0].strip().split("\n")[0]
            raise BrowserUnavailable(f"Enter не нажался: {detail[:100]}")
        deadline = time.time() + SUBMIT_VERIFY_SEC
        while time.time() < deadline:
            try:
                cur = str(loc.first.evaluate(
                    "e => e.isContentEditable ? e.innerText : e.value") or "")
            except Exception:
                cur = ""  # элемент ушёл из DOM — страница перерисовалась
            if not cur.strip() or _effect_verdict(
                    pre, _page_state(page)) == EFFECT_CHANGED:
                logger.info(f"[BrowserActions] Enter "
                            f"({host_part or f'вкладка #{tab_id}'}) — отправлено")
                return "sent"
            time.sleep(0.25)
        raise FillUncertain(
            "Enter нажат, но поле не очистилось и страница не изменилась — "
            "не уверен, что отправилось")

    return _WORKER.submit(_op)


# macOS key codes для System Events (Safari-бэкенд: доверенное нажатие
# клавиши требует фокуса окна, см. _safari_focus_tab)
_MAC_KEYCODES = {"Space": 49, "Enter": 36, "Escape": 53, "Tab": 48,
                 "Backspace": 51, "m": 46, "k": 40,
                 "ArrowDown": 125, "ArrowUp": 126,
                 "ArrowLeft": 123, "ArrowRight": 124}


def press_key(host_part: Optional[str], key: str,
              tab_id: Optional[int] = None, times: int = 1) -> str:
    """Специальная клавиша (Space/Enter/Escape/Tab/Backspace/m/стрелки) в
    страницу — БЕЗ выбора элемента: клавиша уходит в активный фокус или
    документ (плеер: play/pause, громкость стрелками; игра, модалка).
    times — сколько раз нажать (громкость: стрелка = ~10% YouTube); потолок
    серии — PRESS_TIMES_MAX, он же потолок разбора «удали N символов».
    Best effort без closed-loop проверки эффекта: клавиша может не менять
    видимый DOM (canvas-рендер) — обещаем только нажатие. CDP — playwright
    (доверенное key-событие), Safari — key code через System Events;
    AppleScript-фолбэк Chrome не умеет фокусить окно для System Events —
    честный отказ."""
    backend = _select_backend(tab_op=True)
    n = max(1, min(int(times or 1), PRESS_TIMES_MAX))
    if backend == "safari":
        code = _MAC_KEYCODES.get(key)
        if code is None:
            raise BrowserUnavailable(f"клавиша {key} не поддерживается")
        _safari_focus_tab(tab_id, host_part)
        try:
            for _ in range(n):
                _osascript(
                    _as_tell_to(f"key code {code}", app=_SYS_EVENTS_APP),
                    browser="safari")
        except BrowserUnavailable as e:
            raise BrowserUnavailable(
                f"{key} не нажался: {e}")
        logger.info(f"[BrowserActions] {key}×{n} (Safari) — "
                    f"{host_part or f'вкладка #{tab_id}'}")
        return "ok"
    if backend != "cdp":
        raise _no_backend(f"клавиша {key}")

    def _op(w):
        page = w.page_for(host_part, tab_id)
        # Space/Enter на сфокусированной кнопке «нажимают» САМУ кнопку
        # (нативное поведение), а не доходят до обработчика страницы: после
        # клика по «троеточию» фокус сидит на нём — «пауза» открывала меню.
        # Снимаем фокус с активируемых элементов перед нажатием
        if key in ("Space", "Enter"):
            try:
                page.evaluate(
                    "var a=document.activeElement;"
                    "if(a&&a!==document.body&&a.matches&&a.matches("
                    "'button,a,[role=button],summary,[role=tab],"
                    "[role=menuitem],[role=option]'))a.blur();'ok'")
            except Exception:
                pass
        # Стрелки громкости YouTube работают только с фокусом на плеере
        # (иначе они листают ленту): жмём на сам <video> — playwright сам
        # фокусит элемент. Space так НЕЛЬЗЯ: фокус на <video> глотает пробел
        # (пауза перестаёт срабатывать) — Space уходит на уровень документа.
        # Нет видео/не нажалось — обычное нажатие в документ
        done = 0  # фактически ушедших нажатий (серия могла оборваться)
        if key.startswith("Arrow"):
            try:
                loc = page.locator("video").first
                for _ in range(n):
                    loc.press(key, timeout=2000)
                    done += 1
                logger.info(f"[BrowserActions] {key}" +
                            (f"×{n}" if n > 1 else "") +
                            f" на видео ({host_part or f'вкладка #{tab_id}'})")
                return "ok"
            except Exception as e:
                # Сбой В СЕРЕДИНЕ серии: часть нажатий уже дошла до плеера —
                # добиваем только ОСТАТОК. Раньше фолбэк жал заново все n, и
                # «громче на 4 шага» после сбоя на третьем давало 4+2=6
                # шагов (громкость уезжала мимо просьбы)
                if done:
                    logger.info(f"[BrowserActions] {key}: серия оборвалась на "
                                f"{done}/{n} ({e}) — добиваю остаток "
                                f"{n - done} в документ")
        for _ in range(max(0, n - done)):
            page.keyboard.press(key)
        logger.info(f"[BrowserActions] {key}" + (f"×{n}" if n > 1 else "") +
                    f" ({host_part or f'вкладка #{tab_id}'})")
        return "ok"

    return _WORKER.submit(_op)
# /aria-modal/dialog[open] или fixed-перекрытие ≥15% вьюпорта с z-index≥10
# и «модальным» классом. Детект для Escape-фолбэка «закрой окно»
_MODAL_VISIBLE_JS = r"""
(function(){
  function vis(e){var r=e.getBoundingClientRect();var s=getComputedStyle(e);
    return r.width>40&&r.height>40&&s.visibility!=='hidden'&&s.display!=='none'
      &&parseFloat(s.opacity||'1')>0.05;}
  var dl=document.querySelectorAll('[role="dialog"],[aria-modal="true"],dialog[open]');
  for(var i=0;i<dl.length;i++){if(vis(dl[i]))return "1";}
  var all=document.querySelectorAll('div,section,aside,form');
  var vw=window.innerWidth||1,vh=window.innerHeight||1;
  for(var j=0;j<all.length;j++){var e=all[j];if(!vis(e))continue;
    var s=getComputedStyle(e);
    if(s.position!=='fixed'&&s.position!=='absolute')continue;
    var z=parseInt(s.zIndex,10)||0;if(z<10)continue;
    var r=e.getBoundingClientRect();
    if(r.width*r.height<vw*vh*0.15)continue;
    var cl=String(e.className||'');
    // Хром плеера (ytp-overlays-container и родня) — не модалка: класс
    // содержит «overlay», элемент вечно на странице, и Escape-фолбэк
    // «закрой окно» вечно «не закрывал» его
    if(/^ytp-|html5-video-player|ytm-|video-ads/.test(cl))continue;
    if(/popup|modal|dialog|overlay|sheet|lightbox|drawer/i.test(cl))
      return "1";}
  return "0";
})()
"""


def modal_visible(host_part: Optional[str], tab_id: Optional[int] = None) -> bool:
    """Есть ли на странице видимый диалог/оверлей. Ошибка доступа — False
    (не выдумываем модалку там, где не смогли посмотреть)."""
    try:
        out = _eval_in_tab(host_part, tab_id, _MODAL_VISIBLE_JS)
    except Exception:
        return False
    return str(out or "").strip() == "1"


# Виден ли раскрытый выпадающий список (listbox/menu, vue-select/multiselect):
# у него нет крестика, «закрой окно» про него — тоже про это. Высота ≥50:
# YouTube держит на странице ПОСТОЯННО видимые триггеры yt-dropdown-menu
# (класс матчится, высота ~24px) — без порога детектор вечно «видит список»,
# и Escape-фолбэк «закрой» срабатывает из ниоткуда и «не закрывается»
_OPEN_LIST_VISIBLE_JS = r"""
(function(){
  var l=document.querySelectorAll('[role="listbox"],[role="menu"],'
    +'.vs__dropdown-menu,.multiselect__content-wrapper,'
    +'[class*=dropdown-menu],[class*=select-dropdown],[class*=options-list]');
  for(var i=0;i<l.length;i++){var e=l[i];var r=e.getBoundingClientRect();
    var s=getComputedStyle(e);
    if(r.width>40&&r.height>50&&s.display!=='none'&&s.visibility!=='hidden'
      &&parseFloat(s.opacity||'1')>0.05)return "1";}
  return "0";
})()
"""
# «Что-то временное поверх страницы» — модалка ИЛИ открытый список
_TRANSIENT_VISIBLE_JS = (
    "(function(){var m=" + _MODAL_VISIBLE_JS + ";if(m==='1')return '1';"
    "var l=" + _OPEN_LIST_VISIBLE_JS + ";return l==='1'?'1':'0';})()"
)


def open_list_visible(host_part: Optional[str], tab_id: Optional[int] = None) -> bool:
    """Есть ли на странице раскрытый выпадающий список. Ошибка доступа — False."""
    try:
        out = _eval_in_tab(host_part, tab_id, _OPEN_LIST_VISIBLE_JS)
    except Exception:
        return False
    return str(out or "").strip() == "1"


def press_escape(host_part: Optional[str], tab_id: Optional[int] = None) -> str:
    """Escape на странице — закрытие модалки/оверлея без крестика или
    открытого выпадающего списка. Closed-loop: если до нажатия что-то такое
    было видно и после осталось — BrowserUnavailable (Escape игнорируется)."""
    backend = _select_backend(tab_op=True)
    if backend == "safari":
        pre = str(_safari_exec(host_part, _TRANSIENT_VISIBLE_JS, tab_id) or "")
        _safari_escape(tab_id, host_part)
        deadline = time.time() + 1.5
        while time.time() < deadline:
            cur = str(_safari_exec(host_part, _TRANSIENT_VISIBLE_JS, tab_id) or "")
            if pre.strip() != "1" or cur.strip() != "1":
                return "ok"
            time.sleep(0.25)
        raise BrowserUnavailable("окно не закрылось — оно игнорирует Escape")
    if backend != "cdp":
        raise _no_backend("Escape")

    def _op(w):
        page = w.page_for(host_part, tab_id)
        before = str(page.evaluate(_TRANSIENT_VISIBLE_JS) or "").strip()
        page.keyboard.press("Escape")
        # Меню закрывается с АНИМАЦИЕЙ затухания: одна проверка через 0.4с
        # видела ещё живый попап и честно отказывала, хотя Escape сработал —
        # опрашиваем как Safari-путь, до 1.5с
        deadline = time.time() + 1.5
        while time.time() < deadline:
            time.sleep(0.25)
            try:
                after = str(page.evaluate(_TRANSIENT_VISIBLE_JS) or "").strip()
            except Exception:
                after = ""  # страница перерисовалась/ушла — считаем закрытым
            if before.strip() != "1" or after.strip() != "1":
                logger.info(f"[BrowserActions] Escape "
                            f"({host_part or f'вкладка #{tab_id}'}) — окно закрыто")
                return "ok"
        raise BrowserUnavailable(
            "окно не закрылось — оно игнорирует Escape")

    return _WORKER.submit(_op)


# Слайдер: input[type=range] (нативный value-сеттер + input/change — так
# принимает и React) или кастомный [role=slider] (координаты для клика по
# треку). Подпись ищем по словам в СОБСТВЕННЫХ aria-label/title/
# aria-labelledby элемента (у иконочных слайдеров вроде громкости/прогресса
# ютуба текст живёт там, а не в innerText предков — кейс 08.09 «перетащи
# громкость на 0» не находил ничего), затем в <label> и внутритексте +
# aria-label предков: ключ = слова*10 − глубина, своя подпись сильнее всего
# (+5). Слова от 6 букв — по усечённому началу («громкости» → «громкост…»,
# как в целевом снапшоте — бедный стемминг русской морфологии). Свёрнутые
# до наведения слайдеры (<10px) — второй шанс: range сеттится и скрытым,
# кастомный помечается tiny — CDP раскроет реальным наведением мыши.
# При промахе возвращаем have — подписи реально найденных слайдеров
# (честный ответ «что есть на странице» вместо отказа вслепую).
# Открытые shadow root'ы обходим отдельно (tp-yt-paper-slider и т.п.).
_SET_SLIDER_JS = (
    "(function(label, value, unit){"
    + _VPC_NORM_CORE_JS +
    "function norm(s){return __vpcN(s);}"
    "var els=[].slice.call(document.querySelectorAll("
    "'input[type=range],[role=slider]'));"
    "try{var sh=[];var wlk2=function(n){if(n.shadowRoot)sh.push(n.shadowRoot);"
    "var ch=n.children||[];for(var q2=0;q2<ch.length;q2++){wlk2(ch[q2]);}};"
    "wlk2(document.documentElement);"
    "for(var s9=0;s9<sh.length;s9++){"
    "var shs=sh[s9].querySelectorAll('input[type=range],[role=slider]');"
    "for(var s8=0;s8<shs.length;s8++){if(els.indexOf(shs[s8])<0)"
    "els.push(shs[s8]);}}}catch(x){}"
    "var live=[],tiny=[];"
    "for(var i=0;i<els.length;i++){var r0=els[i].getBoundingClientRect();"
    "if(r0.width>=10&&r0.height>=4)live.push(els[i]);else tiny.push(els[i]);}"
    "if(!live.length&&!tiny.length)return '{\"st\":\"none\"}';"
    "var words=norm(label).split(' ').filter(function(w){return w.length>=3;})"
    ".map(function(w){return w.length>=6?w.slice(0,w.length-1):w;});"
    "function ownLab(el){var t=norm(el.getAttribute('aria-label'))||"
    "norm(el.title);"
    "if(!t){var lb=el.getAttribute('aria-labelledby');"
    "if(lb){var lo=document.getElementById(lb);if(lo)t=norm(lo.innerText);}}"
    "return t;}"
    "function pick(list){var best=null,bestKey=-1;"
    "for(var j=0;j<list.length;j++){var el=list[j];var key=-1;"
    "var own=ownLab(el);"
    "if(own){var s0=0;for(var k0=0;k0<words.length;k0++){"
    "if(own.indexOf(words[k0])>=0)s0++;}"
    "if(s0>0)key=s0*10+5;}"
    "var chain=[];var lab=el.closest('label');if(lab)chain.push(lab);"
    "var p=el;for(var i2=0;i2<4&&p;i2++){p=p.parentElement;"
    "if(p)chain.push(p);}"
    "for(var d=0;d<chain.length;d++){"
    "var ctx=norm((chain[d].innerText||'').slice(0,300))+' '+"
    "norm(chain[d].getAttribute('aria-label'));"
    "var s=0;for(var k=0;k<words.length;k++){"
    "if(ctx.indexOf(words[k])>=0)s++;}"
    "if(s>0){var kk=s*10-d;if(kk>key)key=kk;}}"
    "if(key>bestKey){bestKey=key;best=el;}}"
    "return best;}"
    "var best=pick(live),tinyF=false;"
    "if(!best&&tiny.length){best=pick(tiny);tinyF=!!best;}"
    "if(!best){"
    "if(words.length&&(live.length+tiny.length)>1){"
    "var have=[],all2=live.concat(tiny);"
    "for(var h2=0;h2<all2.length&&have.length<6;h2++){"
    "var hl=ownLab(all2[h2]);"
    "if(!hl){var pp=all2[h2].parentElement,up2=0;"
    "while(pp&&up2<3){var pt2=norm((pp.innerText||'').slice(0,60));"
    "if(pt2){hl=pt2;break;}pp=pp.parentElement;up2++;}}"
    "if(hl&&have.indexOf(hl.slice(0,30))<0)have.push(hl.slice(0,30));}"
    "return JSON.stringify({st:'no-match',have:have});}"
    "best=live[0]||tiny[0];tinyF=live.length===0;}"
    "var min=parseFloat(best.min||best.getAttribute('aria-valuemin')||'0');"
    "var max=parseFloat(best.max||best.getAttribute('aria-valuemax')||'100');"
    # Единицы: pct — доля шкалы (50% громкости 0..1 → 0.5, а не максимум);
    # min — минуты → секунды (шкала медиа-прогресса в секундах); без
    # единицы/секунды — значение как есть
    "var v=value;"
    "if(unit==='pct'){v=Math.round((min+(max-min)*value/100)*100)/100;}"
    "else if(unit==='min'){v=value*60;}"
    "v=Math.max(min,Math.min(max,v));"
    "best.setAttribute('data-vpc-slider','1');"
    "if(best.tagName==='INPUT'){"
    "var setter=Object.getOwnPropertyDescriptor("
    "window.HTMLInputElement.prototype,'value').set;"
    "setter.call(best,String(v));"
    "best.dispatchEvent(new Event('input',{bubbles:true}));"
    "best.dispatchEvent(new Event('change',{bubbles:true}));"
    "return JSON.stringify({st:'range',v:v});}"
    "var r=best.getBoundingClientRect();"
    "var ratio=max>min?(v-min)/(max-min):0.5;"
    "return JSON.stringify({st:'custom',v:v,"
    "x:Math.round(r.left+r.width*ratio),y:Math.round(r.top+r.height/2),"
    "tiny:(tinyF||r.width<10||r.height<4)?1:0,"
    "dbg:(best.tagName+'#'+(best.id||'')).toLowerCase()});"
    "})(%s,%s,%s)"
)
# Прочитать фактическое значение помеченного слайдера и снять метку
_SLIDER_VERIFY_JS = (
    "(function(){var e=document.querySelector('[data-vpc-slider]');"
    "if(!e)return '';"
    "var v=(e.value!==undefined)?e.value:e.getAttribute('aria-valuenow');"
    "e.removeAttribute('data-vpc-slider');"
    "return String(v);})()"
)


# Громкость <video> напрямую (shorts: стрелки клавиатуры — листание видео,
# а не громкость; «m» там не работает). Цель — играющее/крупнейшее видео.
_MEDIA_VOLUME_JS = (
    "(function(){"
    "var vs=[].slice.call(document.querySelectorAll('video'));"
    "if(!vs.length)return 'нет видео на странице';"
    "var v=vs[0],best=0;"
    "vs.forEach(function(e){var r=e.getBoundingClientRect();"
    "var a=r.width*r.height;if(!e.paused&&a>best){best=a;v=e;}});"
    "var op=__OP__;"
    "if(op==='toggle'){v.paused?v.play():v.pause();"
    "return v.paused?'paused':'playing';}"
    # Звук — НАПРАВЛЕННО: «включи звук» и «выключи звук» — разные команды, а
    # не одно переключение. Раньше 'mute' переключал (просьба «включи звук» у
    # незаглушённого видео его ГЛУШИЛА), а 'unmute' в шаблон не был заведён
    # вовсе — падал в числовую ветку (d=0) и отвечал 'vol:NN', из-за чего
    # отчёт говорил «выставил громкость N%» вместо «включил звук».
    # toggle_mute оставлен как явное переключение (клавиша «m»)
    "if(op==='mute'){v.muted=true;return 'muted';}"
    "if(op==='unmute'){v.muted=false;return 'unmuted';}"
    "if(op==='toggle_mute'){v.muted=!v.muted;"
    "return v.muted?'muted':'unmuted';}"
    # Остальное — шаг громкости («-0.2»/«0.2»); нечисловая операция не
    # «тихо делаем ничего», а честная причина: молчаливый d=0 маскировал
    # незаведённые операции (так и потерялся unmute)
    "var d=parseFloat(op);"
    "if(isNaN(d))return 'неизвестная операция со звуком: '+op;"
    "v.muted=false;"
    "var nv=Math.min(1,Math.max(0,v.volume+d));"
    "v.volume=nv;"
    "return 'vol:'+Math.round(nv*100);})()"
)


def media_volume_op(host_part: Optional[str], op: str,
                    tab_id: Optional[int] = None) -> str:
    """Громкость/звук видео напрямую у <video> (на shorts стрелки клавиатуры —
    листание видео, а не громкость). op: «-0.2»/«0.2» — шаг громкости;
    «mute»/«unmute» — НАПРАВЛЕННО выключить/включить звук; «toggle_mute» —
    переключить; «toggle» — пауза/продолжение.
    → 'vol:NN' | 'muted' | 'unmuted' | 'paused' | 'playing'; по 'muted'/
    'unmuted' отчёт говорит «выключил/включил звук», а не «выставил
    громкость N%». Причина отказа из JS («нет видео…», «неизвестная
    операция…») — BrowserUnavailable, как у остальных мостов: строкой она
    доходила до отчёта как «изменил громкость» (любая строка = успех)."""
    got = str(_eval_js_any(host_part, tab_id,
                           _js_fill(_MEDIA_VOLUME_JS, OP=op)))
    if got.startswith("vol:") or got in _MEDIA_VOLUME_OK:
        return got
    raise BrowserUnavailable(got or "звук: пустой ответ страницы")


# Успешные ответы _MEDIA_VOLUME_JS (кроме 'vol:NN'); всё остальное — причина
_MEDIA_VOLUME_OK = frozenset({"muted", "unmuted", "paused", "playing"})


# Точки наведения для раскрытия свёрнутого слайдера: сначала левый край
# самого слайдера (у свёрнутой громкости ютуба там иконка динамика), затем
# центры видимых предков — мелкий → крупный; parentElement упирается в
# shadow-границу (tp-yt-paper-slider) — пересекаем через getRootNode().host
_SLIDER_HOVER_ANCHOR_JS = (
    "(function(){var e=document.querySelector('[data-vpc-slider]');"
    "if(!e)return '[]';"
    "var pts=[];var r0=e.getBoundingClientRect();"
    "if(r0.height>=4)pts.push({x:r0.left+2,y:r0.top+r0.height/2});"
    "var p=e,d=0;"
    "while(p&&d<8&&pts.length<3){"
    "p=p.parentElement||(p.getRootNode?p.getRootNode().host:null);"
    "if(!p||!p.getBoundingClientRect)break;"
    "var r=p.getBoundingClientRect();"
    "if(r.width>=10&&r.height>=4){"
    "pts.push({x:r.left+r.width/2,y:r.top+r.height/2});}"
    "d++;}"
    "return JSON.stringify(pts);})()"
)
# Перемер помеченного слайдера после наведения: свежая точка клика под
# целевое значение (rect до наведения мог быть нулевым)
_SLIDER_MEASURE_JS = (
    "(function(value){var e=document.querySelector('[data-vpc-slider]');"
    "if(!e)return '';"
    "var r=e.getBoundingClientRect();"
    "if(r.width<10||r.height<4)return '';"
    "var min=parseFloat(e.min||e.getAttribute('aria-valuemin')||'0');"
    "var max=parseFloat(e.max||e.getAttribute('aria-valuemax')||'100');"
    "var v=Math.max(min,Math.min(max,value));"
    "var ratio=max>min?(v-min)/(max-min):0.5;"
    "return JSON.stringify({v:v,"
    "x:r.left+r.width*ratio,y:r.top+r.height/2});})(%s)"
)


_SLIDER_UNMARK_JS = (
    "(function(){var e=document.querySelector('[data-vpc-slider]');"
    "if(e)e.removeAttribute('data-vpc-slider');return 'ok';})()"
)


def _slider_unmark(page_eval):
    """Снять метку [data-vpc-slider] — ЕДИНСТВЕННОЕ место и обязательный шаг
    на ЛЮБОМ выходе. Оставшаяся на странице метка перехватывает
    querySelector следующей команды слайдера: та меряет и тянет ЧУЖОЙ
    элемент. Раньше снятие было расписано по ветвям и терялось на отказах
    (нечитаемый ответ JS, «на странице нет слайдеров», исключение мыши или
    клавиатуры, успех Safari-ветки)."""
    try:
        page_eval(_SLIDER_UNMARK_JS)
    except Exception as e:
        logger.debug(f"[BrowserActions] Метка слайдера не снята: {e}")


def _slider_accepted(got, want) -> bool:
    """Виджет принял значение: |факт − цель| < 0.51 (шкалы целочисленные).
    ОДНО определение для обоих бэкендов: нечисловой ответ («», «on»,
    локализованное число) — «не принял», а не ValueError наружу (в
    Safari-ветке голый float(got) вылетал мимо BrowserUnavailable)."""
    try:
        return abs(float(str(got).strip()) - float(want)) < 0.51
    except (TypeError, ValueError):
        return False


def _slider_no_match(label: str, st: dict) -> "BrowserUnavailable":
    """«Не нашёлся» + список реально найденных на странице слайдеров
    (have из _SET_SLIDER_JS) — вместо отказа вслепую пользователь/LLM
    может переформулировать подпись."""
    have = st.get("have") or []
    suffix = ""
    if have:
        suffix = ("; на странице есть: "
                  + ", ".join(f"«{str(h)[:30]}»" for h in have[:6]))
    return BrowserUnavailable(
        f"слайдер с подписью «{label[:40]}» не нашёлся{suffix}")


def _hover_reveal_slider(page, value: float) -> Optional[Tuple[float, float]]:
    """Раскрыть свёрнутый до наведения слайдер (громкость ютуба) РЕАЛЬНЫМ
    движением мыши (CSS :hover синтетическим событием не включается):
    пробуем точки-кандидаты (левый край слайдера → видимые предки через
    shadow-границы) и после каждой перемеряем. → свежая точка клика или
    None — не раскрылся (тогда клавиатурный фолбэк)."""
    try:
        raw_a = str(page.evaluate(_SLIDER_HOVER_ANCHOR_JS) or "")
        anchors = json.loads(raw_a) if raw_a else []
    except (ValueError, TypeError):
        anchors = []
    if not isinstance(anchors, list):
        return None
    for a in anchors[:3]:
        try:
            page.mouse.move(float(a["x"]), float(a["y"]))
        except (TypeError, KeyError, ValueError):
            continue
        time.sleep(0.35)  # transition раскрытия
        try:
            raw_m = str(page.evaluate(_SLIDER_MEASURE_JS % float(value)) or "")
            m2 = json.loads(raw_m) if raw_m else None
        except (ValueError, TypeError):
            m2 = None
        if isinstance(m2, dict):
            return float(m2["x"]), float(m2["y"])
    return None


# Чтение значения помеченного слайдера БЕЗ снятия метки (клавиатурный
# фолбэк измеряет шаг по ходу, метка нужна до конца)
_SLIDER_READ_JS = (
    "(function(){var e=document.querySelector('[data-vpc-slider]');"
    "if(!e)return '';"
    "var v=(e.value!==undefined&&e.value!==null)?e.value:"
    "e.getAttribute('aria-valuenow');"
    "return v===null||v===undefined?'':String(v);})()"
)


def _keyboard_set_slider(page, value: float) -> None:
    """Кастомный слайдер без рабочей геометрии (не раскрылся при наведении):
    фокус + клавиши по ARIA-контракту слайдера (Home → минимум, стрелка →
    шаг; шаг измеряем фактически — у громкости ютуба он 5). Итог проверяет
    обычный closed-loop вызывающего кода. BrowserUnavailable — слайдер
    клавиатуру не принимает или цель недостижима шагами."""
    page.evaluate(
        "(function(){var e=document.querySelector('[data-vpc-slider]');"
        "if(e)e.focus();})()")

    def _read() -> Optional[float]:
        try:
            raw = str(page.evaluate(_SLIDER_READ_JS) or "").strip()
            return float(raw) if raw else None
        except (ValueError, TypeError):
            return None

    def _fail(msg: str):
        _slider_unmark(page.evaluate)
        raise BrowserUnavailable(msg)

    page.keyboard.press("Home")
    cur = _read()
    if cur is None:
        _fail("слайдер скрыт и на клавиатуру не отвечает")
    if abs(cur - value) < 0.51:
        return
    pos = neg = None
    step = 0.0
    page.keyboard.press("ArrowRight")
    nxt = _read()
    if nxt is not None and nxt != cur:
        step, pos, neg = abs(nxt - cur), "ArrowRight", "ArrowLeft"
    else:
        page.keyboard.press("ArrowUp")  # вертикальный слайдер
        nxt = _read()
        if nxt is not None and nxt != cur:
            step, pos, neg = abs(nxt - cur), "ArrowUp", "ArrowDown"
    if not pos or not step or nxt is None:
        _fail("слайдер скрыт и на клавиатуру не отвечает")
    delta = value - nxt
    count = abs(int(round(delta / step)))
    if count > 60:
        _fail(f"слайдер клавишами не дотянуть (шаг {step:g}, цель {value:g})")
    key = pos if delta > 0 else neg
    for _ in range(count):
        page.keyboard.press(key)


def set_slider(host_part: Optional[str], label: str, value: int,
               tab_id: Optional[int] = None, unit: str = "") -> str:
    """Перетащить слайдер: «рабочие часы в день» → 8. Возвращает фактически
    установленное значение (строкой; клампится в min..max самим виджетом).
    unit: "" — значение как есть; "pct" — процент шкалы; "min"/"sec" —
    минуты/секунды медиа-прогресса (конвертация в JS). Подпись матчится
    по aria-label/title/aria-labelledby самого элемента, label и тексту
    предков; свёрнутый до наведения кастомный слайдер на CDP раскрывается
    реальным наведением мыши, без геометрии — клавиши по ARIA-контракту.
    Нет слайдеров / подпись не совпала (в ошибке — список имеющихся) /
    виджет значение не принял — BrowserUnavailable с честным текстом."""
    js = _SET_SLIDER_JS % (json.dumps(label or "", ensure_ascii=False),
                           int(value),
                           json.dumps(unit or "", ensure_ascii=False))
    backend = _select_backend(tab_op=True)

    def _verify(page_eval) -> str:
        time.sleep(0.35)  # фреймворк может перерисовать и сбросить значение
        got = str(page_eval(_SLIDER_VERIFY_JS) or "").strip()
        return got

    if backend == "safari":
        def _seval(j):
            return _safari_exec(host_part, j, tab_id)

        # finally — метка снимается на любом выходе (в т.ч. на отказах и
        # нечитаемом ответе JS), см. _slider_unmark
        try:
            raw = str(_seval(js) or "")
            try:
                st = json.loads(raw)
            except ValueError:
                st = {"st": ""}
            if st.get("st") == "range":
                got = _verify(_seval)
                if got and _slider_accepted(got, st.get("v")):
                    logger.info(f"[BrowserActions] Слайдер «{label[:30]}» → "
                                f"{got} "
                                f"({host_part or f'вкладка #{tab_id}'})")
                    return got
                raise BrowserUnavailable(
                    f"слайдер не принял значение (осталось {got or 'прежним'})")
            if st.get("st") == "custom":
                raise BrowserUnavailable(
                    "слайдер нестандартный (не input) — на Safari не потяну")
            if st.get("st") == "no-match":
                raise _slider_no_match(label, st)
            raise BrowserUnavailable("на странице нет слайдеров")
        finally:
            _slider_unmark(_seval)
    if backend != "cdp":
        raise _no_backend("слайдер")

    def _op(w):
        page = w.page_for(host_part, tab_id)
        try:
            return _op_marked(page)
        finally:
            _slider_unmark(page.evaluate)  # метка уходит на любом выходе

    def _op_marked(page):
        raw = str(page.evaluate(js) or "")
        try:
            st = json.loads(raw)
        except ValueError:
            st = {"st": ""}
        kind = st.get("st")
        if kind == "no-match":
            raise _slider_no_match(label, st)
        if kind not in ("range", "custom"):
            raise BrowserUnavailable("на странице нет слайдеров")
        if kind == "custom":
            x, y = float(st["x"]), float(st["y"])
            if st.get("tiny"):
                # Свёрнут до наведения (громкость ютуба): CSS :hover
                # синтетическим событием не включается — раскрываем
                # реальным движением мыши и перемеряем точку клика;
                # не раскрылся — клавиши по ARIA-контракту слайдера
                logger.info(f"[BrowserActions] Слайдер «{label[:30]}» "
                            f"свёрнут ({st.get('dbg') or '?'}) — раскрываем "
                            f"наведением")
                pt = _hover_reveal_slider(page, float(st["v"]))
                if pt is not None:
                    x, y = pt
                    page.mouse.click(x, y)
                else:
                    _keyboard_set_slider(page, float(st["v"]))
            else:
                # Кастомный слайдер: доверенный клик по точке трека
                # (большинство виджетов прыгают в позицию клика)
                page.mouse.click(x, y)
        got = _verify(page.evaluate)
        if got and _slider_accepted(got, st.get("v")):
            logger.info(f"[BrowserActions] Слайдер «{label[:30]}» → {got} "
                        f"({host_part or f'вкладка #{tab_id}'})")
            return got
        # Виджет значение не принял (вернул прежнее/пусто)
        raise BrowserUnavailable(
            f"слайдер не принял значение (осталось {got or 'прежним'})")

    return _WORKER.submit(_op)


def read_text(host_part: Optional[str], tab_id: Optional[int] = None,
              mode: str = "last") -> str:
    """Текст со страницы без побочек (front=False — вкладку не выдёргиваем).
    mode=last — последнее сообщение чата (роль-префикс), page — основной
    текст (main/article). Нечего читать — BrowserUnavailable с честным текстом."""
    js = _READ_PAGE_JS if mode == "page" else _READ_LAST_JS
    out = str(_eval_in_tab(host_part, tab_id, js) or "").strip()
    if not out:
        raise BrowserUnavailable(
            "на странице нет текста" if mode == "page"
            else "на странице не нашлось сообщений")
    if mode != "page":
        role, _, body = out.partition("|")
        prefix = {"assistant": "Ассистент", "пользователь": "Вы",
                  "user": "Вы"}.get(role, "")
        out = f"{prefix}: {body}" if prefix and body else body
        if len(out) > 2000:
            out = out[:2000] + "…"
    return out


_READ_SECTION_JS = (
    # Текст секции страницы по заголовку («что находится в "Добавить по вкусу"?»):
    # заголовок — короткий (≤80) элемент, чей текст покрывает все слова запроса
    # (с начала слова, со стеммингом — «в корзине» ≈ «Корзина»); из нескольких
    # кандидатов берём самый короткий (точнее всего). Контейнер — ближайший
    # section/article/aside или предок, чей текст заметно шире заголовка, но не
    # вся страница (BODY — промах, секцию выделить не удалось)
    "(function(q){"
    + _VPC_NORM_JS +
    # «»/" — кавычки цитирования названия секции в самом вопросе («что в
    # "Добавить по вкусу"?»), их режем в пробел отдельно от __vpcN (там
    # апостроф — часть текста, а не разделитель фразы)
    "function N(s){return __vpcN((s||'').replace(/[«»\"]/g,' '));}"
    # Стем и «слово с начала слова» — общие (__vpcStem/__vpcWIn из
    # _VPC_NORM_JS, таблица окончаний та же, что у питоновского
    # web_search._stem): здесь жила ТРЕТЬЯ копия формулы усечения
    "function allW(t,words){for(var i=0;i<words.length;i++){"
    "if(!__vpcWIn(t,words[i]))return false;}return true;}"
    # Ранг заголовка: h1 (товар/страница) > h2 > h3.. > прочие — иначе
    # побеждает h3 похожей карточки из рекомендаций вместо заголовка товара
    "function rank(e){var tg=e.tagName;"
    "if(tg==='H1')return 0;"
    "if(tg==='H2')return 1;"
    "if(/^H[3-6]$/.test(tg)||e.getAttribute('role')==='heading')return 2;"
    "return 3;}"
    "var words=N(q).split(' ').filter(function(w){return w.length>=2;});"
    "if(!words.length)return '';"
    "var els=document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading],"
    "legend,strong,b,a,div,span,p');"
    "var best=null,brank=99,bl=999,bt='';"
    "for(var i=0;i<els.length;i++){var e=els[i];"
    # Заголовок секции не живёт в шапке/навигации — иначе «что в корзине?»
    # цепляло бы кнопку «Корзина» из хедера и отдавало мусор навигации;
    # и не живёт внутри кнопки/ссылки — иначе ловил бы «В корзину за 769 ₽»
    "if(e.closest('header,nav,[role=banner],[role=navigation]'))continue;"
    "if(e.closest('button,a'))continue;"
    "var own='';"
    "for(var k2=0;k2<e.childNodes.length;k2++){var cn=e.childNodes[k2];"
    "if(cn.nodeType===3)own+=cn.textContent;}"
    "own=N(own);"
    "var t=own.length>=2?own:N(e.innerText);"
    "if(t.length<2||t.length>80)continue;"
    # CTA-кнопки («в корзину за 408 ₽») не заголовки — у заголовков секций
    # цены в тексте не бывает
    "if(/\\d\\s*(₽|руб|р\\.|\\$|€)/.test(t))continue;"
    "if(!allW(t,words))continue;"
    "var rk=rank(e);"
    "if(rk>brank||(rk===brank&&t.length>=bl))continue;"
    "var r=e.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "best=e;brank=rk;bl=t.length;bt=t;}"
    "if(!best)return '';"
    # Контейнер: поднимаемся от заголовка, пока текста мало (<300 — это лишь
    # заголовок с подписью-описанием), но не перескакиваем потолок 2200, если
    # текущий уже несёт больше заголовка (иначе берём большой, но срезанный)
    "var c=best,txt=bt,up=0;"
    "while(c&&up<8){"
    "if(txt.length>=300)break;"
    "var p=c.parentElement;"
    "if(!p||p.tagName==='BODY')break;"
    "var pt=(p.innerText||'').replace(/\\n{3,}/g,'\\n\\n').trim();"
    "if(pt.length>2200&&txt.length>=bt.length+25)break;"
    "c=p;txt=pt;up++;}"
    "if(txt.length<=bt.length+10)return '';"  # кроме заголовка ничего нет
    "return txt.slice(0,2200);})(__Q__)"
)


def read_section(host_part: Optional[str], query: str,
                 tab_id: Optional[int] = None) -> str:
    """Текст секции открытой страницы по её заголовку — для вопросов вида
    «что находится в X?»: вытащенное отдаётся LLM контекстом, список/ответ
    формулирует она. Секция не нашлась — пустая строка (не ошибка: вопрос
    уходит в обычный диалог). Без побочек (front=False)."""
    q = _clean_goal_text(query, 60)
    if not q:
        raise BrowserUnavailable("пустой запрос секции")
    raw = _run_js(host_part, _js_fill(_READ_SECTION_JS, Q=q),
                  tab_id=tab_id, front=False)
    return str(raw or "").strip()


def list_pages() -> List[Tuple[str, str]]:
    """(url, host) всех живых страниц (CDP) — для кросс-страничного поиска
    элемента, когда на целевой вкладке его нет (попап открыт раньше клика).
    Служебные вкладки веб-чатов не участвуют (кликать их командами нельзя)."""
    backend = _select_backend(tab_op=True)
    if backend == "cdp":
        def _op(w):
            return [(p.url, urlparse(p.url).hostname or "")
                    for p in w._all_pages()
                    if not _chat_or_service_url(p.url)]
        return _WORKER.submit(_op)
    if backend == "safari":
        return [(u, urlparse(u).hostname or "") for u in _safari_page_urls()
                if not _chat_or_service_url(u)]
    return []


def list_tabs() -> List[Tuple[int, str, str, str]]:
    """(tab_id, url, host, title) живых вкладок — для «перейди на вкладку X»
    и «какие вкладки открыты». Только CDP: нужны title и bring_to_front,
    на AppleScript/Safari — честный отказ."""
    if _select_backend(tab_op=True) != "cdp":
        raise _no_backend("переключение вкладок")
    return _WORKER.submit(lambda w: w.list_tabs_detailed())


def activate_tab(tab_id: int) -> Tuple[str, str]:
    """Вкладку на передний план → (url, title). BrowserUnavailable, если
    вкладка умерла или бэкенд не CDP; фоновая вкладка веб-чата — явный
    RawTabUnsupported (окна у неё нет)."""
    _refuse_raw_tab(tab_id, "переключение на вкладку")
    if _select_backend(tab_op=True) != "cdp":
        raise _no_backend("переключение вкладок")

    def _op(w):
        pg = w.page_for(None, tab_id)
        try:
            pg.bring_to_front()
        except Exception:
            pass
        try:
            title = str(pg.title() or "")
        except Exception:
            title = ""
        return pg.url, title
    return _WORKER.submit(_op)


def reload_tab(tab_id: Optional[int] = None) -> Tuple[str, str]:
    """Перезагрузить вкладку (tab_id=None — видимую пользовательскую)
    → (url, title). BrowserUnavailable при бэкенде не-CDP; вкладку чата
    бота не перезагружаем (себя же из-под пользователя дёргать нельзя).
    Фоновые вкладки (веб-чаты, реестр _RAW_TABS) playwright не видит —
    они перезагружаются напрямую по raw-CDP (залипшую вкладку чата лечим
    ею вместо перезапуска всего браузера)."""
    if is_raw_tab(tab_id):
        _raw_tab_call(int(tab_id), "Page.reload", {"ignoreCache": False})
        logger.info(f"[BrowserActions] Фоновая вкладка #{tab_id} "
                    "перезагружена")
        return "", ""
    if _select_backend(tab_op=True) != "cdp":
        raise _no_backend("управление вкладками")

    def _op(w):
        # Без явной цели — ТЕКУЩАЯ пользовательская вкладка (общее понятие,
        # тот же источник, что подпись действия на резолве)
        pg = w.current_user_page() if tab_id is None \
            else w.page_for(None, tab_id)
        if tab_id is None and _chat_or_service_url(pg.url):
            raise BrowserUnavailable(
                "видимая вкладка — служебная (чат/автоматика), "
                "её не перезагружаю")
        try:
            pg.reload()
            # Загрузку ждём недолго: факт перезагрузки — сам вызов reload,
            # а тяжёлая страница может собираться дольше любого таймаута
            pg.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        try:
            title = str(pg.title() or "")
        except Exception:
            title = ""
        return pg.url, title
    return _WORKER.submit(_op)


def close_tab(tab_id: Optional[int] = None) -> Tuple[str, str]:
    """Закрыть вкладку (tab_id=None — видимую пользовательскую)
    → (url, title) закрытой. Служебные вкладки (web_llm) и вкладку чата
    бота не закрываем: по tab_id они сюда не доходят (list_tabs их
    не отдаёт), а без tab_id — страхуемся явно. Фоновую вкладку веб-чата
    закрывает close_background_tab, а не эта команда."""
    _refuse_raw_tab(tab_id, "закрытие вкладки командой")
    if _select_backend(tab_op=True) != "cdp":
        raise _no_backend("управление вкладками")

    def _op(w):
        # Без явной цели — ТЕКУЩАЯ пользовательская вкладка (общее понятие,
        # тот же источник, что подпись действия на резолве)
        pg = w.current_user_page() if tab_id is None \
            else w.page_for(None, tab_id)
        if _chat_or_service_url(pg.url):
            raise BrowserUnavailable(
                "это служебная вкладка (чат/автоматика) — её не закрываю")
        url = pg.url
        try:
            title = str(pg.title() or "")
        except Exception:
            title = ""
        pg.close()
        w._purge_pages()  # общая чистка реестра, а не своя копия
        return url, title
    return _WORKER.submit(_op)


def history_nav_tab(tab_id: Optional[int], direction: str) -> Tuple[str, str]:
    """Назад/вперёд по истории вкладки (tab_id=None — видимой
    пользовательской) → (url, title) после перехода. BrowserUnavailable,
    если истории в ту сторону нет: go_back/go_forward отдают None и на
    отсутствие истории, и на SPA-переход без document load — различаем по
    смене URL, чтобы SPA не получала ложный отказ."""
    _refuse_raw_tab(tab_id, "листание истории вкладки")
    if _select_backend(tab_op=True) != "cdp":
        raise _no_backend("управление вкладками")
    if direction not in ("back", "forward"):
        raise BrowserUnavailable(f"неизвестное направление: {direction}")

    def _op(w):
        # Без явной цели — ТЕКУЩАЯ пользовательская вкладка (общее понятие,
        # тот же источник, что подпись действия на резолве)
        pg = w.current_user_page() if tab_id is None \
            else w.page_for(None, tab_id)
        if _chat_or_service_url(pg.url):
            raise BrowserUnavailable(
                "это служебная вкладка (чат/автоматика) — её не листаю")
        pre = pg.url
        resp = None
        try:
            resp = (pg.go_back(wait_until="commit", timeout=8000)
                    if direction == "back"
                    else pg.go_forward(wait_until="commit", timeout=8000))
        except Exception:
            # Навигация МОГЛА состояться: при восстановлении из bfcache
            # Chrome не шлёт load/commit-события и waiter падает по таймауту
            # (живой кейс 10.09: go_back на example.com — «Timeout…
            # navigated to …»). Судим по факту смены URL, а не по waiter'у
            pass
        deadline = time.time() + 2.0
        while not pg.is_closed() and pg.url == pre and time.time() < deadline:
            time.sleep(0.1)  # SPA: popstate применяется не мгновенно
        if pg.is_closed() or (pg.url == pre and resp is None):
            raise BrowserUnavailable(
                "некуда: истории назад у этой вкладки нет"
                if direction == "back" else
                "некуда: истории вперёд у этой вкладки нет")
        try:
            pg.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        try:
            title = str(pg.title() or "")
        except Exception:
            title = ""
        return pg.url, title
    return _WORKER.submit(_op)


# Служебные вкладки веб-чатов (web_llm): живут рядом со страницами
# пользователя и НЕ должны попадать в командную адресацию/попапы — web_llm
# навигирует свою вкладку конкурентно с кликами, и её новый URL раньше
# «угонял» отслеживание: после LLM-уточнения команды уезжали на чат
_SERVICE_HOSTS: set = set()


def register_service_host(host: Optional[str]):
    """Пометить хост как служебный (вкладки веб-чатов web_llm)."""
    h = (host or "").strip().lower()
    if h:
        _SERVICE_HOSTS.add(h)


def is_service_host(host: Optional[str]) -> bool:
    """Служебный хост веб-чата: динамический реестр + статический список
    адаптеров web_llm (на старте реестр ещё пуст, а last_tab.json могли
    записать грязным прошлым запуском)."""
    h = (host or "").strip().lower()
    if not h:
        return False
    if h in _SERVICE_HOSTS:
        return True
    try:
        from app.features.web_llm import ADAPTERS
        # service_host=False (google): хост — рабочая цель команд («открой
        # гугл»), глушить его нельзя; вкладка веб-чата живёт в пуле H, куда
        # команды не целятся
        return h in {str(a.get("host") or "").lower() for a in ADAPTERS.values()
                     if a.get("service_host", True)}
    except Exception:
        return False


def follow_popup(pre_urls: List[str],
                 timeout_sec: float = POPUP_WAIT_SEC
                 ) -> Optional[Tuple[int, str, str]]:
    """Страница, появившаяся после клика (окно входа Google, ссылка с
    target=_blank и т.п.) — регистрируется в реестре и возвращается как
    (tab_id, host, url), чтобы следующие команды целились в неё. None —
    новой страницы нет.
    «Новизна» — по СЧЁТЧИКУ URL, а не по множеству: вкладка, открывшаяся с
    адресом-дублем уже открытого (ссылка «в новой вкладке» на ту же
    страницу), — тоже попап; старый about:blank (стартовая вкладка браузера)
    новым не считается. Свежая вкладка, ещё сидящая на about:blank (навигация
    в полёте), дожидается URL до POPUP_NAV_WAIT_SEC — дольше общего бюджета.
    Служебные вкладки веб-чатов (_SERVICE_HOSTS) попапом НЕ считаются:
    web_llm навигирует свою вкладку конкурентно с кликом, и её новый URL
    раньше ложно «угонял» отслеживание на chat.deepseek.com и т.п."""
    if _select_backend(tab_op=True) != "cdp":
        return None

    def _op(w):
        deadline = time.time() + timeout_sec
        blank_deadline = None  # есть свежая about:blank — ждём её навигацию
        while True:
            counts = Counter(pre_urls)
            fresh_blank = False
            for p in w._all_pages():
                u = p.url or ""
                if counts.get(u, 0) > 0:
                    counts[u] -= 1
                    continue  # вкладка была до клика (и старый about:blank)
                if u.startswith("about:"):
                    fresh_blank = True  # новая вкладка клика, URL ещё нет
                    continue
                host = urlparse(u).hostname or ""
                if is_service_host(host):
                    continue  # служебная вкладка веб-чата, не попап клика
                tid = _register_page(w, p)
                logger.info(f"[BrowserActions] Попап после клика: "
                            f"вкладка #{tid} ({host})")
                return tid, host, u
            if fresh_blank and blank_deadline is None:
                blank_deadline = time.time() + POPUP_NAV_WAIT_SEC
            now = time.time()
            if now >= deadline \
                    and (blank_deadline is None or now >= blank_deadline):
                return None
            time.sleep(0.2)

    return _WORKER.submit(_op, timeout=float(timeout_sec)
                          + POPUP_NAV_WAIT_SEC + SUBMIT_MARGIN_SEC)


def find_tab_id(host_part: str) -> Optional[int]:
    """Стабильный id вкладки по подстроке URL (для _remember_tab). None —
    вкладки нет / бэкенд недоступен. Фоновые (raw) вкладки playwright не
    видит — сканируются отдельно."""
    if not host_part:
        return None
    try:
        if _select_backend(tab_op=True) == "cdp":
            tid = _WORKER.submit(lambda w: w.tab_id_for_host(host_part))
            if tid is not None:
                return tid
            with _RAW_TABS_LOCK:
                raw_ids = list(_RAW_TABS)
            for tab_id in raw_ids:
                try:
                    if host_part in _raw_url(tab_id):
                        return tab_id
                except RawCallTimeout:
                    continue  # не ответила на замер — не повод её забывать
                except BrowserUnavailable:
                    _raw_drop(tab_id)
            return None
        return _find_tab_applescript(host_part)
    except BrowserUnavailable:
        return None

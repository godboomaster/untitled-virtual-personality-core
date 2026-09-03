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
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

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
SUBMIT_VERIFY_SEC = 2.0          # closed-loop отправки: поле очистилось/стр. изменилась
DOWNLOAD_VERIFY_SEC = 5.0        # closed-loop: ждём события скачивания

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
    # search_first (активная, иначе фоновая страница поиска)
    "search_pick": (
        None,
        "var N={N};"
        "var h=location.hostname,p=location.pathname,a=null;"
        # youtube: заголовочные ссылки видео по всем раскладкам — выдача
        # (a#video-title), старый фид (a#video-title-link), up-next
        # (a#wc-endpoint), lockup-главная 2025 (a.ytLockupMetadataViewModelTitle);
        # селектор-список отдаёт всё в DOM-порядке, по одной ссылке на видео
        "if(h.indexOf('youtube.com')>=0){"
        "  var vs=document.querySelectorAll('a#video-title-link,a#video-title,a#wc-endpoint,a.ytLockupMetadataViewModelTitle');a=vs[N-1]||null;"
        "}else if(h.indexOf('kinopoisk.ru')>=0){"
        "  var as=[].slice.call(document.querySelectorAll('a[href^=\"/film/\"],a[href^=\"/series/\"]'));"
        "  var rs=as.filter(function(x){return /^\\/(film|series)\\/\\d+\\/?$/.test(new URL(x.href).pathname)});"
        "  a=rs[N-1]||null;"
        "}else if(h.indexOf('google.')>=0){"
        "  var hs=document.querySelectorAll('#search h3');var h3=hs[N-1];a=h3?h3.closest('a'):null;"
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

_BCFG_DEFAULTS = {
    "backend": "auto",        # auto | cdp | applescript (последний — только macOS)
    "channel": "chrome",      # приоритетный Chromium-браузер при авто-детекте
    "cdp_url": CDP_URL,
    "launch": True,           # боту можно самому запускать браузер с отладкой
    "user_data_dir": None,    # str или per-OS dict; None — выделенный профиль
    "executable": None,       # путь к бинарю; None — авто-детект Chromium-браузеров
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
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.unlink(os.path.join(user_data_dir, name))
            except OSError:
                pass
        logger.info(f"[BrowserActions] Снят протухший SingletonLock (pid {pid})")
        return
    except PermissionError:
        pass  # чужой, но живой процесс — профиль занят
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
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False



class _CdpWorker:
    def __init__(self):
        self._req: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._op_lock = threading.Lock()  # одна CDP-операция за раз
        self._pw = None
        self._browser = None
        self._pages: Dict[int, object] = {}
        self._next_tab_id = 1
        self._proc: Optional[subprocess.Popen] = None  # браузер, запущенный нами

    # Транспорт (вызывается из любого потока)
    def submit(self, fn):
        with self._op_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, daemon=True, name="vpc-cdp")
                self._thread.start()
            box: Dict[str, object] = {}
            done = threading.Event()
            self._req.put((fn, box, done))
            done.wait()
            if "err" in box:
                raise box["err"]
            return box.get("res")

    def _loop(self):
        while True:
            fn, box, done = self._req.get()
            try:
                box["res"] = fn(self)
            except BaseException as e:
                box["err"] = e
            finally:
                done.set()

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
        udd = _resolve_user_data_dir()
        _check_profile_lock(udd)
        port = urlparse(str(_BCFG.get("cdp_url") or CDP_URL)).port or 9222
        try:
            proc = subprocess.Popen(  # noqa: S603 — путь/флаги из нашего конфига
                [exe, f"--remote-debugging-port={port}", f"--user-data-dir={udd}",
                 "--no-first-run", "--no-default-browser-check",
                 "--disable-session-crashed-bubble",
                 # Экономия ресурсов: профиль — автоматизационный, фоновая
                 # синхронизация/телеметрия/переводчик/каст не нужны, а их
                 # фоновая активность добавляет нагрузку на слабом железе
                 *CHROME_THRIFT_FLAGS, "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise BrowserUnavailable(f"не удалось запустить браузер: {e}")
        self._proc = proc
        logger.info(f"[BrowserActions] Запускаю браузер с отладкой: {exe} "
                    f"(профиль {udd}, порт {port})")
        deadline = time.monotonic() + BROWSER_LAUNCH_TIMEOUT_SEC
        last_err: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                self._connect(timeout_ms=1500)
                logger.info("[BrowserActions] Браузер запущен, CDP подключён")
                return
            except BrowserUnavailable as e:
                last_err = e
                if proc.poll() is not None:
                    # Процесс умер сразу — типично при занятом профиле: запуск
                    # просто открыл окно в уже работающем Chrome без отладки
                    raise BrowserUnavailable(
                        "браузер завершился сразу после запуска — похоже, этот "
                        "профиль уже занят запущенным Chrome без отладки. Закрой "
                        "его полностью и повтори (или смени browser.user_data_dir)")
                time.sleep(0.3)
        raise BrowserUnavailable(
            f"не дождался отладочного порта за {int(BROWSER_LAUNCH_TIMEOUT_SEC)}с"
            f"{': ' + str(last_err) if last_err else ''}. Если профиль — "
            "основной профиль Chrome 136+, порт для него игнорируется: задай "
            "отдельный browser.user_data_dir в конфиге")

    def _kill_browser(self, user_data_dir: str) -> bool:
        """(в потоке воркера) Завершить процесс браузера на профиле бота.
        Кандидаты: запущенный нами Popen → pid из SingletonLock (после
        проверки cmdline, что держит именно наш профиль — Chrome с чужим
        профилем не трогаем). SIGTERM, до 10с, затем SIGKILL (posix).
        → True, если процесс мёртв (или его и не было)."""
        self._drop_connection()
        pids: List[int] = []
        if self._proc is not None and self._proc.poll() is None:
            pids.append(self._proc.pid)
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
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        alive = list(pids)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and alive:
            alive = [pid for pid in pids if _pid_alive(pid)]
            if alive:
                time.sleep(0.3)
        if alive and hasattr(signal, "SIGKILL"):
            logger.warning("[BrowserActions] Браузер не завершился за 10с — "
                           "SIGKILL")
            for pid in alive:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            time.sleep(0.5)
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.unlink(os.path.join(user_data_dir, name))
            except OSError:
                pass
        self._proc = None
        died = not any(_pid_alive(pid) for pid in pids)
        logger.info("[BrowserActions] Браузер бота завершён"
                    if died else
                    "[BrowserActions] Браузер бота не удалось завершить")
        return died

    def _all_pages(self) -> List:
        return [p for ctx in self._browser.contexts for p in ctx.pages
                if not p.is_closed()]

    def page_for(self, host_part: Optional[str], tab_id: Optional[int] = None):
        """Вкладка по tab_id (реестр отслеживаемых) или по подстроке URL
        (последняя подходящая — свежая); host_part=None — крайняя открытая."""
        self.ensure_browser(allow_launch=False)
        if tab_id is not None:
            if tab_id in _RAW_TABS:
                raise BrowserUnavailable(
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
            # Служебные вкладки веб-чатов — не «крайняя страница» для команд:
            # web_llm открывает их последними, и без фильтра «последняя
            # вкладка» указывала бы на chat.deepseek.com вместо страницы
            # пользователя
            nonsvc = [p for p in pages
                      if (urlparse(p.url).hostname or "").lower()
                      not in _SERVICE_HOSTS]
            return (nonsvc or pages)[-1]  # активную вкладку CDP не сообщает
        matches = [p for p in pages if host_part in (p.url or "")]
        if not matches:
            raise BrowserUnavailable(f"нет открытой вкладки {host_part}")
        return matches[-1]

    def new_page(self, url: str) -> int:
        self.ensure_browser(allow_launch=True)
        ctx = self._browser.contexts[0] if self._browser.contexts \
            else self._browser.new_context()
        page, quiet = self._new_page_quiet(ctx, url)
        tid = self._next_tab_id
        self._next_tab_id += 1
        self._pages[tid] = page
        logger.info(f"[BrowserActions] Открыта вкладка #{tid}: {url[:80]}")
        if quiet:
            self._activate_tab_quietly(page, url)
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
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
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

    def tab_id_for_host(self, host_part: str) -> Optional[int]:
        self.ensure_browser(allow_launch=False)
        for tid, pg in list(self._pages.items()):
            if pg.is_closed():
                self._pages.pop(tid, None)
            elif host_part in (pg.url or ""):
                return tid
        matches = [p for p in self._all_pages() if host_part in (p.url or "")]
        if not matches:
            return None
        tid = self._next_tab_id
        self._next_tab_id += 1
        self._pages[tid] = matches[-1]
        return tid

    def list_tabs_detailed(self) -> List[Tuple[int, str, str, str]]:
        """(tab_id, url, host, title) живых страниц, кроме служебных
        (web_llm) и вкладки чата — для «перейди на вкладку X» и «какие
        вкладки открыты». Страницы регистрируются в _pages, так что id
        стабильны для activate_tab и последующих команд."""
        self.ensure_browser(allow_launch=False)
        for tid, pg in list(self._pages.items()):
            if pg.is_closed():
                self._pages.pop(tid, None)
        out: List[Tuple[int, str, str, str]] = []
        for p in self._all_pages():
            host = (urlparse(p.url).hostname or "").lower()
            if not host or host in _SERVICE_HOSTS:
                continue
            if host in ("localhost", "127.0.0.1"):
                continue  # вкладка чата — не цель переключения
            tid = next((t for t, pg in self._pages.items() if pg is p), None)
            if tid is None:
                tid = self._next_tab_id
                self._next_tab_id += 1
                self._pages[tid] = p
            try:
                title = str(p.title() or "")
            except Exception:
                title = ""
            out.append((tid, p.url, host, title))
        return out

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
            for p in self._all_pages():
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
        _WORKER.submit(lambda w: w.ensure_browser(allow_launch=False))
        return True
    except Exception:
        return False


# Перезапуск браузера: не чаще раза в RESTART_COOLDOWN_SEC — серия неудачных
# отправок веб-чата иначе устраивала бы restart-шторм (каждый ~10с)
RESTART_COOLDOWN_SEC = 60.0
_RESTART_LOCK = threading.Lock()
_LAST_RESTART_TS = 0.0


def restart_browser(reason: str = "",
                    cooldown_sec: float = RESTART_COOLDOWN_SEC) -> bool:
    """Перезапустить браузер бота: закрыть процесс и открыть заново с CDP.
    Лечит залипшие состояния страницы, которые не чинятся навигацией
    (кейс 26.08: промпт введён, Enter нажат — поле не очистилось, сообщение
    не появилось в ленте; закрытый цикл отправки дважды не прошёл).
    Убивается ТОЛЬКО Chrome на выделенном профиле бота — браузер с основным
    профилем пользователя не трогаем никогда (там вкладки и сессии
    человека). Фоновые вкладки веб-чатов умирают вместе с процессом: их
    реестр сбрасываем, WebChatLLM откроет свежие вкладки сам (self-healing
    в _ensure_chat). → True, если браузер после этого доступен по CDP."""
    # Safari — браузер пользователя без отдельного профиля: процесс не
    # перезапускаем никогда (убивать чужой браузер нельзя)
    if str(_BCFG.get("backend") or "auto") == "safari":
        logger.info("[BrowserActions] Перезапуск неприменим к Safari")
        return False
    global _LAST_RESTART_TS, _RAW_CLIENT
    with _RESTART_LOCK:
        if time.time() - _LAST_RESTART_TS < cooldown_sec:
            logger.info(f"[BrowserActions] Перезапуск браузера пропущен — "
                        f"уже перезапускали <{int(cooldown_sec)}с назад")
            return False
        _LAST_RESTART_TS = time.time()
    logger.warning(f"[BrowserActions] Перезапуск браузера бота"
                   f"{f' ({reason})' if reason else ''}")
    # Фоновые вкладки и их сокет умирают вместе с браузером — сбрасываем,
    # чтобы следующие вызовы не стреляли в мёртвые сессии
    with _RAW_LOCK:
        _RAW_TABS.clear()
        if _RAW_CLIENT is not None:
            try:
                _RAW_CLIENT.close()
            except Exception:
                pass
            _RAW_CLIENT = None
    udd = _resolve_user_data_dir()
    if not _is_default_browser_profile(udd):
        _WORKER.submit(lambda w: w._kill_browser(udd))
    else:
        logger.warning("[BrowserActions] Профиль — основной профиль "
                       "пользователя: процесс не убиваю, только "
                       "переподключаюсь")
    try:
        _WORKER.submit(lambda w: w.ensure_browser(
            allow_launch=bool(_BCFG.get("launch", True))))
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
            allow_launch=bool(_BCFG.get("launch", True))))
        return "cdp"
    # auto: CDP — основной путь; AppleScript — fallback (только macOS)
    if _cdp_available():
        return "cdp"
    if not tab_op and _BCFG.get("launch", True):
        try:
            _WORKER.submit(lambda w: w.ensure_browser(allow_launch=True))
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


def _open_tab_applescript(url: str) -> int:
    """macOS: новая вкладка Chrome с url → её стабильный AppleScript-id.
    id переживает переходы страницы и не зависит от порядка окон/вкладок —
    в отличие от поиска по подстроке URL, который путается в старых вкладках
    того же сайта."""
    safe = url.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Google Chrome"\n'
        "  if (count of windows) = 0 then make new window\n"
        "  set w to front window\n"
        f'  tell w to make new tab with properties {{URL:"{safe}"}}\n'
        "  return id of active tab of w\n"
        "end tell\n"
    )
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
    safe = host_part.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Google Chrome"\n'
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
        "  if found is not missing value then return found\n"
        "end tell\n"
    )
    try:
        return int(_osascript(script))
    except (BrowserUnavailable, TypeError, ValueError):
        return None


# AppleScript-имена Chromium-браузеров для тихого выбора вкладки; первым
# пробуем канал из конфига — обычно это и есть браузер бота
_QUIET_TAB_APPS = {
    "chrome": "Google Chrome", "edge": "Microsoft Edge", "opera": "Opera",
    "yandex": "Yandex", "brave": "Brave Browser", "vivaldi": "Vivaldi",
}


def _quiet_tab_select_script(app: str, url: str, host: str) -> str:
    safe = url.replace("\\", "\\\\").replace('"', '\\"')
    safe_h = (host or "").replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'if application "{app}" is not running then return "__skip__"\n'
        'tell application "System Events"\n'
        "    set prevBundle to bundle identifier of first application "
        "process whose frontmost is true\n"
        "    set prevName to name of first application process whose "
        "frontmost is true\n"
        "end tell\n"
        f'tell application "{app}"\n'
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
        "    set active tab index of window hitWin to hitTab\n"
        "end tell\n"
        # Смена активной вкладки не активирует приложение сама, но страница
        # (чаты/карты с обработчиками фокуса) может перехватить его — тогда
        # браузер вылезет вперёд. Проверяем и возвращаем фокус прежнему
        # frontmost-приложению.
        "delay 0.25\n"
        'tell application "System Events" to set nowName to name of '
        "first application process whose frontmost is true\n"
        "if nowName is not prevName then\n"
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
    channel = str(_BCFG.get("channel") or "chrome").lower()
    ordered = [_QUIET_TAB_APPS[channel]] if channel in _QUIET_TAB_APPS else []
    ordered += [n for n in _QUIET_TAB_APPS.values() if n not in ordered]
    for app in ordered:
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


def _run_apple_events(host_part: Optional[str], js: str,
                      scan_search: bool = False,
                      tab_id: Optional[int] = None) -> str:
    """JS во вкладке Chrome: по tab_id (точно, для навигации), иначе чей URL
    содержит host_part; при None — в активной вкладке переднего окна.
    scan_search=True (рецепты поисковой выдачи): при неудаче на активной —
    фоновый поиск вкладок со страницей поиска (команда может быть дана из
    чата — активная вкладка тогда сам чат).
    Ошибки — BrowserUnavailable с подсказкой по настройке."""
    esc = js.replace("\\", "\\\\").replace('"', '\\"')
    if tab_id is not None:
        # Точная вкладка по стабильному id (многошаговая навигация).
        # Сравнение текстом: `t's id is <int>` у Chrome молча не матчится
        script = (
            'tell application "Google Chrome"\n'
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
            "  end repeat\n"
            "end tell\n"
            'return "__no_tab__"\n'
        )
    elif host_part is None and not scan_search:
        # Активная вкладка переднего окна. Если это наш чат — НЕ ищем другую
        # вкладку сами: клик по первой попавшейся странице (чужой сайт) хуже,
        # чем честный отказ; распознаёт чат и просит назвать сайт resolve_click
        script = (
            'tell application "Google Chrome"\n'
            "  with timeout of 5 seconds\n"
            f'    return execute (active tab of front window) javascript "{esc}"\n'
            "  end timeout\n"
            "end tell\n"
        )
    elif host_part is None:
        # Рецепт «здесь и сейчас»: сначала активная вкладка (пользователь может
        # смотреть на выдачу), затем — фоновый поиск среди вкладок со СТРАНИЦЕЙ
        # ПОИСКА (когда команда дана из чата, активная вкладка — сам чат).
        # JS сам проверяет сайт/страницу и отвечает «ok:…» только при клике.
        # err 12 («JavaScript через AppleScript отключено») пробрасываем
        # сентинелом — иначе try-глушилка выдаёт его за «нет вкладки»
        script = (
            'tell application "Google Chrome"\n'
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
            "  end repeat\n"
            "end tell\n"
            'return "__no_tab__"\n'
        )
    else:
        # Обход вкладок — под try + timeout на КАЖДОЙ: одна повисшая/модальная
        # вкладка иначе вешает всю AppleEvent-очередь Chrome (-1712).
        # Берём ПОСЛЕДНЮЮ подходящую вкладку: свежеоткрытая правее всех, —
        # навигация должна вести свежую, а не залипшую старую того же сайта
        script = (
            'tell application "Google Chrome"\n'
            "  set found to missing value\n"
            "  repeat with w in windows\n"
            "    repeat with t in tabs of w\n"
            "      try\n"
            "        with timeout of 5 seconds\n"
            f'          if (t\'s URL) contains "{host_part}" then set found to t\n'
            "        end timeout\n"
            "      end try\n"
            "    end repeat\n"
            "  end repeat\n"
            "  if found is not missing value then\n"
            "    try\n"
            "      with timeout of 5 seconds\n"
            f'        return execute found javascript "{esc}"\n'
            "      end timeout\n"
            "    on error errMsg number errNum\n"
            '      if errNum is 12 or errMsg contains "JavaScript" then return "__js_disabled__"\n'
            "    end try\n"
            "  end if\n"
            "end tell\n"
            'return "__no_tab__"\n'
        )
    out = _osascript(script)
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
    esc = js.replace("\\", "\\\\").replace('"', '\\"')
    if tab_id is not None:
        with _SAFARI_LOCK:
            host_part = _SAFARI_TABS.get(tab_id) or host_part
    if host_part:
        safe = host_part.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            'tell application "Safari"\n'
            "  repeat with w in windows\n"
            "    repeat with t in tabs of w\n"
            "      try\n"
            f'        if (URL of t) contains "{safe}" then return do JavaScript "{esc}" in t\n'
            "      end try\n"
            "    end repeat\n"
            "  end repeat\n"
            "end tell\n"
            'return "__no_tab__"\n'
        )
    else:
        script = (
            'tell application "Safari"\n'
            '  if (count of documents) = 0 then return "__no_tab__"\n'
            f'  return do JavaScript "{esc}" in front document\n'
            "end tell\n"
        )
    out = _osascript(script, browser="safari")
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
        esc = js.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            'tell application "Safari"\n'
            "  repeat with w in windows\n"
            "    repeat with t in tabs of w\n"
            "      try\n"
            f'        set r to do JavaScript "{esc}" in t\n'
            '        if r starts with "ok:" then return r\n'
            "      end try\n"
            "    end repeat\n"
            "  end repeat\n"
            "end tell\n"
            'return "__no_tab__"\n'
        )
        out = _osascript(script, browser="safari")
        if out == "__no_tab__":
            raise BrowserUnavailable("нет открытой вкладки с результатами поиска")
        return out
    return _safari_exec(host_part, js, tab_id)


def _safari_open_tab(url: str) -> int:
    """Новая вкладка Safari → наш tab_id (реестр по URL; стабильных id у
    вкладок Safari нет)."""
    global _SAFARI_NEXT_ID
    safe = url.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Safari"\n'
        "  if (count of windows) = 0 then make new document\n"
        f'  tell front window to make new tab with properties {{URL:"{safe}"}}\n'
        "end tell\n"
        'return "ok"\n'
    )
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
    safe_h = host_part.replace("\\", "\\\\").replace('"', '\\"')
    safe_u = url.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Safari"\n'
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      try\n"
        f'        if (URL of t) contains "{safe_h}" then\n'
        f'          set URL of t to "{safe_u}"\n'
        '          return "ok"\n'
        "        end if\n"
        "      end try\n"
        "    end repeat\n"
        "  end repeat\n"
        "end tell\n"
        'return "__no_tab__"\n'
    )
    if _osascript(script, browser="safari") == "__no_tab__":
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
        safe = host_part.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            'tell application "Safari"\n'
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
            "  end repeat\n"
            "end tell\n"
            'return "__no_tab__"\n'
        )
        if _osascript(script, browser="safari") == "__no_tab__":
            raise BrowserUnavailable(
                f"нет открытой вкладки Safari {host_part}")
    else:
        _osascript('tell application "Safari" to activate', browser="safari")
    time.sleep(0.3)  # дать фокусу перейти


def _safari_enter(tab_id: Optional[int], host_part: Optional[str]):
    """Доверенный Enter через System Events (как keyboard.press у playwright)."""
    _safari_focus_tab(tab_id, host_part)
    try:
        _osascript('tell application "System Events" to keystroke return',
                   browser="safari")
    except BrowserUnavailable as e:
        raise BrowserUnavailable(
            f"{e} (для Enter нужно ещё Accessibility: System Settings → "
            "Privacy & Security → Accessibility)")


def _safari_escape(tab_id: Optional[int], host_part: Optional[str]):
    """Доверенный Escape (key code 53) через System Events."""
    _safari_focus_tab(tab_id, host_part)
    try:
        _osascript('tell application "System Events" to key code 53',
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
    try:
        pre = _safari_exec(host_part, _DOM_STATE_JS, tab_id)
    except BrowserUnavailable:
        pre = f"<transition:{time.monotonic()}>"
    _safari_enter(tab_id, host_part)
    deadline = time.time() + SUBMIT_VERIFY_SEC
    while time.time() < deadline:
        try:
            cur = _safari_exec(host_part, _CHAT_FIELD_JS % sel, tab_id)
        except BrowserUnavailable:
            cur = ""
        if not cur.strip():
            return "sent"
        try:
            if _safari_exec(host_part, _DOM_STATE_JS, tab_id) != pre:
                return "sent"
        except BrowserUnavailable:
            return "sent"  # страница ушла — считаем отправленным
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
    script = (
        'tell application "Safari"\n'
        '  set out to ""\n'
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      try\n"
        "        set out to out & (URL of t) & linefeed\n"
        "      end try\n"
        "    end repeat\n"
        "  end repeat\n"
        "  return out\n"
        "end tell\n"
    )
    try:
        out = _osascript(script, browser="safari")
    except BrowserUnavailable:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


# ── Общий вход для JS-сниппетов (рецепты, href) ──────────

def _run_js(host_part: Optional[str], js: str, scan_search: bool = False,
            tab_id: Optional[int] = None, front: bool = False) -> str:
    """Бэкенд по конфигу: CDP (единый на обеих ОС), AppleScript или Safari
    (macOS fallback). front=True — выдёргивать окно браузера на передний
    план; зарезервировано за явными командами пользователя (activate_tab),
    по умолчанию действия бота окно не поднимают."""
    backend = _select_backend(tab_op=True)
    if backend == "cdp":
        return _WORKER.submit(
            lambda w: w.eval_js(host_part, js, scan_search=scan_search,
                                tab_id=tab_id, front=front))
    if backend == "safari":
        return _run_safari_events(host_part, js, scan_search, tab_id)
    return _run_apple_events(host_part, js, scan_search, tab_id=tab_id)


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

# Дешёвый «отпечаток» страницы: URL + readyState + размер DOM + размер
# shadow-DOM (клики по элементам внутри shadow root меняют только его —
# без этого слагаемого closed-loop их не видел). Смена любого — страница
# изменилась (навигация, SPA-перерисовка, открывшееся меню).
_DOM_STATE_JS = (
    "location.href+'|'+document.readyState+'|'+"
    "document.getElementsByTagName('*').length+'|'+"
    "(document.body?document.body.innerHTML.length:0)+'|'+"
    "(function(){var s=0;var w=function(n){"
    "if(n.shadowRoot)s+=n.shadowRoot.innerHTML.length;"
    "var c=n.children||[];for(var i=0;i<c.length;i++)w(c[i]);};"
    "w(document.documentElement);return s;})()"
)


def _page_state(page) -> str:
    """Отпечаток страницы; в переходе между документами evaluate падает —
    это тоже изменение, отдаём сентинел, отличный от любого прошлого состояния."""
    try:
        return str(page.evaluate(_DOM_STATE_JS))
    except Exception:
        return f"<transition:{time.monotonic()}>"


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
    prev = None
    stable = 0
    while time.monotonic() < deadline and stable < DOM_STABLE_POLLS:
        cur = _page_state(page)
        if "<transition:" not in cur and cur == prev:
            stable += 1
        else:
            prev = cur
            stable = 0
        page.wait_for_timeout(DOM_POLL_MS)


def _poll_state_change(state_fn, pre: str, timeout: float = CLICK_VERIFY_SEC,
                       interval: float = 0.3) -> bool:
    """Closed-loop (п.6): ждём, пока отпечаток страницы не изменится
    относительно pre. True — изменилась (клик сработал)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(interval)
        if state_fn() != pre:
            return True
    return False


def _eval_js_any(host_part: Optional[str], tab_id: Optional[int],
                 js: str) -> str:
    """JS во вкладке на любом бэкенде, БЕЗ выдёргивания на передний план
    (фоновые проверки перед снапшотом). CDP — через воркер (front=False),
    Safari/AppleScript — их мосты."""
    backend = _select_backend(tab_op=True)
    if backend == "cdp":
        return str(_WORKER.submit(
            lambda w: w.eval_js(host_part, js, tab_id=tab_id,
                                front=False)) or "")
    if backend == "safari":
        return str(_safari_exec(host_part, js, tab_id) or "")
    return str(_run_apple_events(host_part, js, tab_id=tab_id) or "")


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
                   tab_id: Optional[int] = None) -> Optional[str]:
    """Признак антибот-проверки на странице → короткая метка
    («title: …»/«widget: …»); None — не похоже на капчу или проверка
    недоступна (best effort, не ошибка)."""
    try:
        label = str(_eval_js_any(host_part, tab_id, _ANTIBOT_JS) or "").strip()
    except Exception:
        return None
    return label or None


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
_SCROLL_STEP_JS = (
    "(function(){"
    "var y0=window.scrollY||0;"
    "window.scrollBy(0,Math.round(window.innerHeight*0.9));"
    "var de=document.documentElement;"
    "var lim=Math.max(de.scrollHeight,document.body?document.body.scrollHeight:0)"
    "-window.innerHeight;"
    "var y1=window.scrollY||0;"
    "return JSON.stringify({moved:y1>y0+10,bottom:y1>=lim-2});})()"
)

# Шаг прокрутки КРУПНЕЙШЕГО внутреннего скроллящегося контейнера (очередь
# YouTube #items, внутренние ленты): окно стоит на месте, а виртуализированный
# список внутри контейнера подгружает пункты только при его прокрутке.
# y0 в ответе — исходная позиция контейнера для возврата
_CONTAINER_SCROLL_STEP_JS = (
    "(function(){"
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
    "var y0=best.scrollTop;"
    "best.scrollTop+=Math.round(best.clientHeight*0.9);"
    "var lim=best.scrollHeight-best.clientHeight;"
    "return JSON.stringify({moved:best.scrollTop>y0+10,"
    "bottom:best.scrollTop>=lim-2,y0:y0});})()")

_CONTAINER_SCROLL_RESTORE_JS = (
    "(function(){"
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
    "if(best)best.scrollTop=__Y__;"
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
                     _CONTAINER_SCROLL_RESTORE_JS.replace(
                         "__Y__", str(int(float(y0 or 0)))))
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
    (пользователь не должен обнаружить страницу уехавшей)."""
    try:
        _eval_js_any(host_part, tab_id,
                     f"window.scrollTo(0,{float(y)});'ok'")
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


def screenshot_viewport(host_part: Optional[str] = None,
                        tab_id: Optional[int] = None) -> Optional[bytes]:
    """JPEG-скриншот вьюпорта вкладки — для визуального фолбэка резолва
    (иконочные UI без accessible name, п.4). scale='css': 1 CSS-пиксель =
    1 пиксель картинки (на Retina device-scale даёт ×2 по каждой стороне —
    в 4 раза больше пикселей на кодирование/пересылку, а рамки кандидатов
    всё равно считаются в CSS-координатах); jpeg q80 заметно легче png и
    для кодирования, и для аплоада в веб-чат. Только CDP; None — бэкенд
    не даёт скриншота/ошибка (фолбэк не обязателен)."""
    try:
        if _select_backend(tab_op=True) != "cdp":
            return None
        def _op(w):
            page = w.page_for(host_part, tab_id)
            return page.screenshot(type="jpeg", quality=80, scale="css")
        return _WORKER.submit(_op)
    except Exception as e:
        logger.debug(f"[BrowserActions] Скриншот вьюпорта не удался: {e}")
        return None


# ── DOM-снапшот и агентный клик («нажми X») ──────────────

# Однострочный JS (только одинарные кавычки — через AppleScript идёт как есть):
# помечает видимые кликабельные элементы атрибутом data-vpc-idx и возвращает
# JSON {url, items:[{idx,tag,role,text,aria,title,href,w,h,vp}]}.
# Помимо текста собираем aria-label/title/alt/placeholder и роль (п.3) —
# подписи есть даже у иконок без текста. Видимость — по реальному рендеру:
# размер rect + getComputedStyle (display/visibility/opacity), vp — во вьюпорте.
# Проходы в порядке приоритета (общий бюджет 100 элементов): 1) пункты
# открытых попапов/меню; 2) кликабельные и label-переключатели открытой
# модалки (порталы в конце body иначе не влезают в бюджет); 3) стандартные
# ссылки/кнопки/поля + label с radio/checkbox; 3б) кнопки-иконки без текста
# (бургер-меню, крестик, корзина) — подпись синтезируется из class/id;
# 4) иконки-раскрыватели JS-меню
# (img/svg/i с class/src вида menu-open, nav-js, chevron…) — клик по иконке,
# подпись — текст родительского пункта меню (так устроены древовидные меню:
# обработчик висит на иконке, а не на пункте); 5) текстовые пункты JS-меню.
# NB: page.accessibility.snapshot() сознательно НЕ используем — в современном
# Playwright этот API deprecated; роли/лейблы собираем здесь сами, заодно с
# привязкой к реальным элементам для последующего клика.
_SNAPSHOT_JS = (
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
    "var out=[],idx=0,i,el,r;"
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
    "href:e.href||'',w:Math.round(b.width),h:Math.round(b.height),ed:ed,"
    # Элемент внутри открытой модалки/диалога (vpcMd): модальный контекст —
    # то, что пользователь сейчас видит; бонус в скоринге против одноимённых
    # ссылок футера («состав» на странице товара)
    "md:vpcMd(e),dd:vpcDd(e),sf:vpcSf(e),ext:vpcExt(e),"
    "q:(ed&&(e.type==='search'||/search|поиск/i.test((e.id||'')+' '+"
    "(e.getAttribute('class')||'')+' '+(e.getAttribute('name')||'')+' '+"
    "(e.getAttribute('placeholder')||'')))?1:0),"
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
    "for(i=0;i<pops.length&&idx<25;i++){"
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
    "for(i=0;i<dlgs.length&&idx<50;i++){el=dlgs[i];"
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
    "for(var i3=0;i3<ins.length&&idx<50;i3++){var ie3=ins[i3];"
    "if(ie3.hasAttribute('data-vpc-idx'))continue;"
    "r=ie3.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(ie3))continue;"
    "var inf4=vpcInfo(ie3,ie3.tagName.toLowerCase());"
    "if(!inf4.text)continue;"
    "ie3.setAttribute('data-vpc-idx',idx);inf4.idx=idx;out.push(inf4);idx++;}"
    "vpcLabels(el,50);}"
    # Кнопки закрытия окон/попапов («card-close» у dodo — без текста и
    # aria-label, портал модалки в КОНЦЕ body): поздние проходы до них не
    # добираются — бюджет съедает лента, и «закрой окно» не находит ничего.
    # Собираем рано, вслед за диалогами; безтекстовым подпись «закрыть»
    "var cls2=document.querySelectorAll('[class*=close],[class*=Close],"
    "[aria-label*=закры i],[aria-label*=close i],[aria-label*=dismiss i]');"
    "var cn=0;"
    "for(i=0;i<cls2.length&&cn<8&&idx<50;i++){el=cls2[i];"
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
    "for(i=0;i<nah.length&&idx<60;i++){el=nah[i];"
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
    # каталога до панели выбора в DOM, иначе бюджет кончается до неё)
    "var pdiv=document.querySelectorAll('div,span,li,p');"
    "var pv=[],po=[];"
    "for(i=0;i<pdiv.length;i++){el=pdiv[i];"
    "if(el.hasAttribute('data-vpc-idx')||el.querySelector('[data-vpc-idx]'))continue;"
    "if(el.closest('[data-vpc-idx],button,a'))continue;"
    "if(getComputedStyle(el).cursor!=='pointer')continue;"
    "var pdt=(el.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(!pdt||pdt.length>40)continue;"
    "r=el.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(el))continue;"
    "var pds=el.querySelectorAll('div,span,li,p'),deeper=false;"
    "for(var pj=0;pj<pds.length;pj++){var pe2=pds[pj];"
    "if(getComputedStyle(pe2).cursor!=='pointer')continue;"
    "if(((pe2.innerText||'').replace(/\\s+/g,' ').trim())===pdt){deeper=true;break;}}"
    "if(deeper)continue;"
    "if(r.bottom>0&&r.right>0&&r.top<window.innerHeight&&r.left<window.innerWidth){"
    "pv.push({e:el,t:pdt});}else{po.push({e:el,t:pdt});}}"
    "var pall=pv.concat(po);"
    "for(i=0;i<pall.length&&idx<70;i++){el=pall[i].e;"
    "el.setAttribute('data-vpc-idx',idx);"
    "var infp=vpcInfo(el,el.tagName.toLowerCase());infp.text=pall[i].t.slice(0,80);"
    "infp.idx=idx;out.push(infp);idx++;}"
    "var els=document.querySelectorAll(sel);"
    "for(i=0;i<els.length&&idx<100;i++){el=els[i];r=el.getBoundingClientRect();"
    "if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(el))continue;"
    "if(el.hasAttribute('data-vpc-idx')||el.querySelector('[data-vpc-idx]'))continue;"
    "var inf=vpcInfo(el,el.tagName.toLowerCase());"
    "if(!inf.text)continue;"
    "el.setAttribute('data-vpc-idx',idx);inf.idx=idx;out.push(inf);idx++;}"
    "vpcLabels(document,100);"
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
    "for(i=0;i<btns.length&&idx<100;i++){el=btns[i];"
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
    "for(i=0;i<ico.length&&idx<100;i++){el=ico[i];"
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
    "for(i=0;i<cand.length&&idx<100;i++){"
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
    "for(var s3=0;s3<shroots.length&&idx<100;s3++){"
    "var shes=shroots[s3].querySelectorAll(sel);"
    "for(var i4=0;i4<shes.length&&idx<100;i4++){var e4=shes[i4];"
    "if(e4.hasAttribute('data-vpc-idx'))continue;"
    "r=e4.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "if(!vpcVis(e4))continue;"
    "var infs=vpcInfo(e4,e4.tagName.toLowerCase());"
    "if(!infs.text)continue;"
    "e4.setAttribute('data-vpc-idx',idx);infs.idx=idx;out.push(infs);idx++;}}"
    "out.length?JSON.stringify({url:location.href,vw:window.innerWidth,items:out}):'__empty__'"
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
                "href": str(it.get("href") or ""),
                "w": float(it.get("w") or 0),
                "h": float(it.get("h") or 0),
                "x": float(it.get("x") or 0),
                "y": float(it.get("y") or 0),
                "vw": vw,
                "vp": bool(it.get("vp")),
                "ed": bool(it.get("ed")),
                "q": bool(it.get("q")),
                "md": bool(it.get("md")),
                "dd": bool(it.get("dd")),
                "sf": bool(it.get("sf")),
                "ext": bool(it.get("ext")),
            })
        except (TypeError, ValueError):
            continue
    if not items:
        raise BrowserUnavailable("на странице нет кликабельных элементов")
    return url, items


# Снапшот обходит и видимые iframe'ы (чаты, виджеты оплаты, встроенные карты):
# JS главного фрейма их DOM не видит. Только CDP (frame.evaluate работает и
# для cross-origin фреймов); AppleScript-фолбэк остаётся без фреймов — там
# same-origin политика. Компактная версия снапшота: только стандартные
# кликабельные/поля, без приоритетных проходов попапов/иконок. Нумерация
# data-vpc-idx продолжает главный снапшот (клик ищет метку по всем фреймам).
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
    экране»."""
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
    next_idx = max((int(it.get("idx", -1)) for it in items), default=-1) + 1
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
                _FRAME_SNAPSHOT_JS
                .replace("__BASE__", str(next_idx))
                .replace("__LIM__", str(FRAME_SNAPSHOT_ITEMS))) or "")
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
                    "href": str(it.get("href") or ""),
                    "w": float(it.get("w") or 0),
                    "h": float(it.get("h") or 0),
                    "x": float(it.get("x") or 0) + float(box.get("x") or 0),
                    "y": float(it.get("y") or 0) + float(box.get("y") or 0),
                    "vw": 0.0,
                    "vp": bool(it.get("vp")) and box_vp,
                    "ed": bool(it.get("ed")),
                    "q": bool(it.get("q")),
                    "md": bool(it.get("md")),
                    "dd": bool(it.get("dd")),
                    "sf": bool(it.get("sf")),
                    "ext": bool(it.get("ext")),
                    "fr": fhost,
                })
                added += 1
            except (TypeError, ValueError):
                continue
        next_idx += added
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
    iframe'ов (_merge_frame_items)."""
    if _select_backend(tab_op=True) == "cdp":
        def _op(w):
            page = w.page_for(host_part, tab_id)
            wait_page_ready(page)
            raw = str(page.evaluate(_SNAPSHOT_JS) or "")
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
        url, items = _parse_snapshot(
            _run_apple_events(host_part, _SNAPSHOT_JS, tab_id=tab_id))
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
# атрибут data-vpc-gidx: раньше целевой снапшот перезаписывал data-vpc-idx
# общего, и когда выбор оставался за общим, клик уходил по ЧУЖОЙ метке
# (idx совпал с другим элементом — «состав» открывал анкету из футера).
_GOAL_SNAPSHOT_JS = (
    "(function(goal){"
    "var sel='a[href],button,[role=button],input[type=button],input[type=submit],"
    "summary,[role=link],[role=tab],[role=option],[role=menuitem],[role=switch]';"
    "document.querySelectorAll('[data-vpc-gidx]').forEach(function(e){"
    "e.removeAttribute('data-vpc-gidx')});"
    "var out=[],idx=0,i;"
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
    "href:e.href||'',w:Math.round(b.width),h:Math.round(b.height),ed:0,"
    "md:vpcMd(e),dd:vpcDd(e),sf:vpcSf(e),ext:vpcExt(e),"
    "x:Math.round(b.left),"
    "vp:(b.bottom>0&&b.right>0&&b.top<window.innerHeight&&b.left<window.innerWidth)?1:0};}"
    # Дефисы/тире в цели — в пробелы: «айс-ти» и «Айс ти» на странице — одно
    "goal=goal.replace(/[-‐-―]/g,' ');"
    "var words=goal.split(' ').filter(function(w){return w.length>=3;});"
    # Слово совпадает с НАЧАЛА слова текста («айс» ≠ «гавАЙСкая»); длинные
    # слова — с усечением окончания («додстера» → «додстер»)
    "function wmatch(own,w){var pl=w.length>=7?w.length-3:(w.length>=6?w.length-2:0);"
    "var st=pl>=4?w.slice(0,pl):w;var ws=own.split(' ');"
    "for(var k=0;k<ws.length;k++){if(ws[k].indexOf(st)===0)return true;}"
    "return false;}"
    "function hits(own){if(goal.length>=3&&own.indexOf(goal)>=0)return 99;"
    "var h=0;for(var wi=0;wi<words.length;wi++){if(wmatch(own,words[wi]))h++;}"
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
    "own=(own+' '+(e.getAttribute('aria-label')||'')+' '+(e.title||''))"
    ".replace(/[-‐-―]/g,' ').replace(/\\s+/g,' ').trim().toLowerCase();"
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
    "if(!t||t.hasAttribute('data-vpc-gidx')||idx>=25)return;"
    "var inf=info(t,t.tagName.toLowerCase());"
    # Контрол, найденный от совпавшего текста (кнопка «49 ₽» рядом с
    # «Сырный»), подписываем самим совпадением — иначе шесть одинаковых
    # «49 ₽» модалки не различить ни скорингом, ни LLM
    "if(fb&&inf.text.toLowerCase().indexOf(fb.toLowerCase())<0){"
    "inf.text=(fb.slice(0,40)+(inf.text?' · '+inf.text:'')).slice(0,80);}"
    "if(!inf.text)return;"
    "t.setAttribute('data-vpc-gidx',idx);inf.idx=idx;out.push(inf);idx++;}"
    "var first=null;"
    "for(i=0;i<mts.length&&idx<25;i++){var e2=mts[i].e;"
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
    "for(var k=0;k<nb.length&&idx<25;k++){var ne=nb[k];"
    "var nr=ne.getBoundingClientRect();if(nr.width<2||nr.height<2)continue;"
    "if(!vpcVis(ne))continue;"
    "mark(ne);}"
    "var nl=cont.querySelectorAll('label');"
    "for(var k2=0;k2<nl.length&&idx<25;k2++){var lb=nl[k2];"
    "if(!lb.querySelector('input[type=radio],input[type=checkbox]'))continue;"
    "var lr=lb.getBoundingClientRect();if(lr.width<2||lr.height<2)continue;"
    "if(!vpcVis(lb))continue;"
    "mark(lb);}"
    # Псевдокнопки места: короткий текст + cursor:pointer (цена «от 385 ₽»
    # на dodo — span с React-обработчиком, не button и не ссылка; пункты
    # меню — <a> без href с тем же признаком)
    "var np=cont.querySelectorAll('span,div,a:not([href])');"
    "for(var k3=0;k3<np.length&&idx<25;k3++){var pe=np[k3];"
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
    "})('__GOAL__')"
)


def snapshot_for_goal(host_part: Optional[str], goal: str,
                      tab_id: Optional[int] = None) -> Tuple[str, List[dict]]:
    """Целевой снапшот «места» под конкретную цель: элементы, чей текст
    совпадает с goal, плюс все контролы найденного места (карточка: кнопка
    цены, пилюли размера/теста) — где бы они ни были в DOM (общий снапшот
    режется бюджетом 100). Первое совпадение прокручивается во вьюпорт.
    → (url, items); совпадений нет — (url, []), это не ошибка.
    Пустая/мусорная цель — ('', []) без вызова страницы."""
    safe = re.sub(r"[\"'\\]", "", " ".join(goal.lower().split()))[:60].strip()
    if not safe:
        return "", []
    raw = _run_js(host_part, _GOAL_SNAPSHOT_JS.replace("__GOAL__", safe),
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
_HIDDEN_EDITABLES_JS = (
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
    "JSON.stringify(out)"
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
    "(function(side,dir){"
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
    # Без стороны: открытое всплывающее меню (настройки плеера YouTube,
    # dropdown) приоритетнее окна — «промотай» при открытом меню крутит
    # ЕГО, иначе меню настроек видео не листается вообще (страница под ним
    # скроллится, меню — нет)
    "if(!side){"
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
    "var S={raf:0,target:target,done:false,stall:0,vel:0,last:0,"
    "speed:Math.max(60,window.innerHeight/9),up:up,acc:0};"
    "window.__vpcScroll=S;"
    "function tick(now){"
    "if(window.__vpcScroll!==S){return;}"
    "if(!S.last){S.last=now;S.raf=requestAnimationFrame(tick);return;}"
    "var dt=Math.min(0.1,(now-S.last)/1000);S.last=now;"
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
    "if(S.up){if(cur()<=0.5){S.done=true;S.raf=0;return;}}"
    "else if(lim()>0&&cur()>=lim()-2){"
    "S.stall+=dt;"
    "if(S.stall>2.5){S.done=true;S.raf=0;return;}"
    "}else{S.stall=0;}"
    "S.raf=requestAnimationFrame(tick);}"
    "S.raf=requestAnimationFrame(tick);"
    "return JSON.stringify({ok:true,bottom:false});"
    "})('__SIDE__','__DIR__')"
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
    "if(!S){return JSON.stringify({active:false,done:false});}"
    "return JSON.stringify({active:!S.done,done:!!S.done});"
    "})()"
)


def scroll_start(host_part: Optional[str] = None,
                 tab_id: Optional[int] = None,
                 side: Optional[str] = None,
                 direction: Optional[str] = None) -> dict:
    """Запустить авто-листание страницы (анимация живёт в самой вкладке).
    Скролл движется ступенями ~56px, а не на каждый rAF-кадр — 60
    репейнтов/с «плавного» варианта грузили GPU и роняли fps всей системы.
    Окно браузера НЕ выдёргивается на передний план (front=False): листание
    идёт в фоне, иначе периодический опрос статуса вечно перехватывал бы
    фокус у чата («стоп» некуда написать). side='left'/'right' — листается
  внутренняя панель этой половины вьюпорта, а не окно. → {"ok", "bottom",
    "side_missed"}; ok=False/bottom — страница уже у края (низ/верх) или
    скроллить нечего; side_missed — прокручиваемого раздела на этой стороне
    нет. direction='up' — листать вверх (по умолчанию вниз)."""
    raw = _run_js(host_part,
                  _SCROLL_START_JS.replace("__SIDE__", side or "")
                  .replace("__DIR__", direction or ""),
                  tab_id=tab_id, front=False)
    try:
        res = json.loads(raw)
        return {"ok": bool(res.get("ok")), "bottom": bool(res.get("bottom")),
                "side_missed": bool(res.get("side_missed"))}
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
    """Состояние листания: {"active", "done"}. done — само долистало до
    конца; active=False и done=False — страница ушла (навигация/перезагрузка).
    Опрашивается дозорным раз в ~секунду — строго без фронтинга окна."""
    raw = _run_js(host_part, _SCROLL_STATUS_JS, tab_id=tab_id, front=False)
    try:
        res = json.loads(raw)
        return {"active": bool(res.get("active")),
                "done": bool(res.get("done"))}
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
    "function __vpcN(s){return (s||'').toLowerCase().replace(/[-‐-―]/g,' ')"
    ".replace(/\\s+/g,' ').trim();}"
    "function __vpcStem(w){var pl=w.length>=7?w.length-3:(w.length>=6?w.length-2:0);"
    "return pl>=4?w.slice(0,pl):w;}"
    "function __vpcWIn(hay,w){var st=__vpcStem(w);var ws=hay.split(' ');"
    "for(var i=0;i<ws.length;i++){if(ws[i].indexOf(st)===0)return true;}return false;}"
    # Карточки товара: заголовок (свой текст с первым словом названия, с
    # начала слова — «айс» ≠ «гавАЙСкая») → ближайший предок с кнопкой и
    # текстом ≤500 (выше уже вся панель корзины); все слова названия должны
    # читаться в тексте карточки (уточнение «гавайскую 20 см»)
    "function __vpcCards(prod){"
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
    "return out;}"
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
    "return 'amb:'+vs.join('|');}"
    "var c=__vpcCtrls(cards[0]);"
    "var t=(op==='edit')?c.edit:(op==='remove')?c.remove:"
    "(op==='decrease')?c.dec:c.inc;"
    "if(!t)return 'err:у «'+prod+'» нет такой кнопки в корзине';"
    "t.click();return 'ok:clicked';"
    "})('__PROD__','__OP__')"
)

_CART_VERIFY_JS = (
    "(function(prod){"
    + _CART_FIND_JS +
    "var cards=__vpcCards(prod);"
    "if(!cards.length)return JSON.stringify({present:false,qty:null});"
    "var qty=__vpcQty(cards[0],__vpcCtrls(cards[0]));"
    "return JSON.stringify({present:true,qty:qty});"
    "})('__PROD__')"
)


def cart_op(host_part: Optional[str], product: str, op: str,
            tab_id: Optional[int] = None) -> dict:
    """Операция с корзиной сайта: op ∈ remove|decrease|increase|edit.
    Клик детерминированный (позиция контрола в карточке товара), затем
    closed-loop проверка эффекта: remove — товар исчез из корзины;
    decrease/increase — новое количество (decrease при 1 шт убирает товар —
    qty=0). → {"status": "ok", "qty": int|None}; неоднозначность (несколько
    похожих карточек) и промах — BrowserUnavailable с человеческим текстом."""
    prod = re.sub(r"[\"'\\]", "", " ".join(str(product or "").split()))[:80].strip()
    if not prod:
        raise BrowserUnavailable("пустое название товара")
    if op not in ("remove", "decrease", "increase", "edit"):
        raise BrowserUnavailable(f"неизвестная операция с корзиной: {op}")
    raw = _run_js(host_part,
                  _CART_CLICK_JS.replace("__PROD__", prod).replace("__OP__", op),
                  tab_id=tab_id, front=False)
    if raw.startswith("amb:"):
        variants = [v for v in raw[4:].split("|") if v]
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
            host_part, _CART_VERIFY_JS.replace("__PROD__", prod),
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


def click_tagged(host_part: Optional[str], idx: int,
                 tab_id: Optional[int] = None,
                 mark: str = "data-vpc-idx") -> str:
    """Клик по элементу с меткой из последнего снапшота этой вкладки.
    mark — атрибут разметки: data-vpc-idx (общий снапшот) или
    data-vpc-gidx (целевой — не затирает метки общего).
    CDP: настоящий playwright-клик (скролл, actionability) с фолбэком на
    force; затем closed-loop проверка эффекта (п.6) — нет изменений за
    CLICK_VERIFY_SEC → ClickUncertain («не уверен, что сработало»), это
    отдельный класс ошибок от «элемент не найден»."""
    if _select_backend(tab_op=True) == "cdp":
        return _WORKER.submit(
            lambda w: _click_cdp(w, host_part, idx, tab_id, attr=mark))
    return _click_applescript(host_part, idx, tab_id, attr=mark)


def _locator_any_frame(page, idx: int, attr: str = "data-vpc-idx"):
    """Локатор элемента с меткой снапшота: главный фрейм, затем видимые
    iframe'ы (снапшот обходит их, _merge_frame_items — нумерация единая).
    attr — какой снапшот пометил элемент: общий (data-vpc-idx) или целевой
    (data-vpc-gidx) — они независимы и не затирают друг друга.
    → (locator, scope) где scope — page или frame (для closed-loop
    отпечатка); (None, None) — элемент не нашёлся нигде."""
    sel = f"[{attr}='{int(idx)}']"
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
               tab_id: Optional[int], attr: str = "data-vpc-idx") -> str:
    page = w.page_for(host_part, tab_id)
    loc, scope = _locator_any_frame(page, idx, attr=attr)
    if loc is None:
        raise BrowserUnavailable("элемент потерян — страница изменилась")
    # Попап (новое окно/вкладка от клика — вход в аккаунт и т.п.) саму страницу
    # не меняет: счётчик страниц в отпечатке, иначе честный клик по «Войти»
    # выглядел бы как «не сработало». Отпечаток — по ТОМУ фрейму, где жил
    # элемент: клик внутри iframe не меняет DOM главного фрейма
    pre = _page_state(scope) + f"|tabs:{len(w._all_pages())}"
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
    if _poll_state_change(
            lambda: _page_state(scope) + f"|tabs:{len(w._all_pages())}", pre):
        return "clicked"
    # FAQ-аккордеоны/тогглы — label+checkbox (dodo): клик по внутреннему div
    # заголовка проходит мимо label-механики (событие не активирует контрол).
    # Перещёлкиваем сам input: аккордеон раскрывается CSS :checked — DOM
    # не меняется, поэтому доказательство — сам факт перещёлкивания
    toggled = _label_toggle_js(scope, idx, attr)
    if toggled in ("flipped", "already"):
        logger.info(f"[BrowserActions] Клик idx={idx} — через label/input "
                    f"({toggled})")
        return "clicked"
    if toggled == "label" and _poll_state_change(
            lambda: _page_state(scope) + f"|tabs:{len(w._all_pages())}", pre):
        logger.info(f"[BrowserActions] Клик idx={idx} — через label")
        return "clicked"
    raise ClickUncertain(
        "клик отправлен, но страница не изменилась — не уверен, что сработало")


def _label_toggle_js(scope, idx: int, attr: str = "data-vpc-idx") -> Optional[str]:
    """Фолбэк клика для label-обёрток: элемент с меткой снапшота (attr)
    внутри <label> с checkbox/radio — дёргаем сам контрол (input.click() —
    trusted-семантика перещёлкивания + события для React). → 'flipped'
    (checked перещёлкнулся — само по себе доказательство: аккордеон
    открывается CSS :checked и DOM-отпечаток не меняется), 'already' (уже
    был включён — обратно не перещёлкиваем: «нажми вопрос» ≠ «закрой его»),
    'label' (input нет, кликнули label — проверять отпечатком вызывающему),
    None/'stuck' — не label-конструкция или контрол не поддался."""
    js = ("(function(){var e=document.querySelector('[" + attr + "=\"" + str(int(idx))
          + "\"]');if(!e)return '';"
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
                       tab_id: Optional[int],
                       attr: str = "data-vpc-idx") -> str:
    """JS-клик по метке снапшота (attr: data-vpc-idx общий / data-vpc-gidx
    целевой) + та же closed-loop проверка состояния.
    Элемент ищем циклом по значению атрибута: querySelector со строкой вида
    '[attr=N]' (с «=») мост Chrome→AppleScript ломается, а NodeList-индекс
    нельзя — его порядок (document order) расходится с порядком присвоения
    idx в снапшоте (сначала ссылки/кнопки, затем иконки-раскрыватели)."""
    def _state() -> str:
        try:
            return _run_apple_events(host_part, _DOM_STATE_JS, tab_id=tab_id)
        except BrowserUnavailable:
            return f"<err:{time.monotonic()}>"

    js = ("var d=document.documentElement;"
          "var els=document.querySelectorAll('[" + attr + "]'),el=null,i;"
          "for(i=0;i<els.length;i++){if(els[i].getAttribute('" + attr + "')==='" + str(int(idx)) + "'){el=els[i];break;}}"
          "if(el){el.scrollIntoView({block:'center'});el.click();d.setAttribute('data-vpc-res','ok:clicked');}"
          "else{d.setAttribute('data-vpc-res','элемент потерян — страница изменилась');}"
          "d.getAttribute('data-vpc-res')")
    pre = _state()
    out = _run_apple_events(host_part, js, tab_id=tab_id)
    if not out.startswith("ok:"):
        raise BrowserUnavailable(out or "клик не выполнен")
    logger.info(f"[BrowserActions] Клик idx={idx} "
                f"({host_part or f'вкладка #{tab_id}' if tab_id else 'активная'})")
    if _poll_state_change(_state, pre):
        return out[3:]
    # label+checkbox/radio (FAQ-аккордеоны dodo): клик по внутреннему div мимо
    # label-механики — перещёлкиваем сам контрол; 'flipped' — checked
    # перещёлкнулся, это само по себе доказательство (CSS :checked без
    # изменения DOM-отпечатка)
    toggle = ("var e=null,els=document.querySelectorAll('[" + attr + "]'),i;"
              "for(i=0;i<els.length;i++){if(els[i].getAttribute('" + attr + "')==='"
              + str(int(idx)) + "'){e=els[i];break;}}"
              "if(!e){''}else{var l=e.closest?e.closest('label'):null;"
              "if(!l){''}else{var inp=l.querySelector('input[type=checkbox],input[type=radio]');"
              "if(inp){var b=!!inp.checked;if(b){'already'}else{inp.click();inp.checked!==b?'flipped':'stuck'}}"
              "else{l.click();'label'}}}")
    tres = _run_apple_events(host_part, toggle, tab_id=tab_id)
    if tres in ("flipped", "already") \
            or (tres == "label" and _poll_state_change(_state, pre)):
        return out[3:]
    raise ClickUncertain(
        "клик отправлен, но страница не изменилась — не уверен, что сработало")


def _norm_ws(s: str) -> str:
    return " ".join(str(s or "").lower().split())


def fill_tagged(host_part: Optional[str], idx: int, text: str,
                tab_id: Optional[int] = None, submit: bool = False) -> str:
    """Ввод текста в поле с data-vpc-idx из последнего снапшота вкладки.
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
    # «Содержит», а не «равно»: виджет может дописать своё («Новосибирск, …»)
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
            if not cur.strip() or _page_state(scope) != pre:
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
    """JS-ввод по data-vpc-idx: native setter (React-совместимо) + события
    input/change, значение читается обратно — то же closed-loop правило.
    Элемент ищем циклом, как в _click_applescript: querySelector со строкой
    '[attr=N]' (с «=») мост Chrome→AppleScript ломается."""
    if submit:
        raise BrowserUnavailable(
            "отправка по Enter («…и отправь») работает только с CDP-бэкендом")
    js = ("var d=document.documentElement;"
          "var els=document.querySelectorAll('[data-vpc-idx]'),el=null,i;"
          "for(i=0;i<els.length;i++){if(els[i].getAttribute('data-vpc-idx')==='" + str(int(idx)) + "'){el=els[i];break;}}"
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
    """Абсолютный href элемента с data-vpc-idx из последнего снапшота вкладки.
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
          "var els=document.querySelectorAll('[data-vpc-idx]'),i,el=null;"
          "for(i=0;i<els.length;i++){if(els[i].getAttribute('data-vpc-idx')==='" + str(int(idx)) + "'){el=els[i];break;}}"
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
    safe = url.replace("\\", "\\\\").replace("'", "\\'")
    js = ("var a=document.createElement('a');"
          "a.href='" + safe + "';a.download='';"
          "document.body.appendChild(a);a.click();a.remove();'ok:download'")
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
        out = _WORKER.submit(_op)
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
_RAW_TABS: Dict[int, dict] = {}  # tab_id → {"targetId", "sessionId"}
_RAW_LOCK = threading.Lock()   # сериализация вызовов по единому сокету
_RAW_CLIENT = None             # ленивый _RawCdp


class _RawCdp:
    """Минимальный sync CDP-клиент поверх browser-websocket: один сокет,
    flat-сессии через sessionId в конверте, события пропускаем. Нужен для
    background-таргетов, которых playwright не видит."""

    def __init__(self):
        import urllib.request
        import websocket  # websocket-client
        base = str(_BCFG.get("cdp_url") or CDP_URL).rstrip("/")
        with urllib.request.urlopen(f"{base}/json/version", timeout=5) as r:
            ws_url = json.loads(r.read().decode())["webSocketDebuggerUrl"]
        # suppress_origin: Chrome отвергает websocket с Origin-заголовком
        # (403 «--remote-allow-origins»); без заголовка пускает
        self._ws = websocket.create_connection(ws_url, timeout=20,
                                               suppress_origin=True)

    def call(self, method: str, params: Optional[dict] = None,
             session_id: Optional[str] = None) -> dict:
        self._next = getattr(self, "_next", 0) + 1
        msg: Dict[str, object] = {"id": self._next, "method": method,
                                  "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self._ws.send(json.dumps(msg))
        while True:
            data = json.loads(self._ws.recv())
            if data.get("id") != msg["id"]:
                continue  # события — мимо (лок сериализует вызовы, чужих ответов нет)
            if "error" in data:
                raise BrowserUnavailable(
                    f"CDP {method}: {(data['error'] or {}).get('message')}")
            return data.get("result") or {}

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def _raw_call(method: str, params: Optional[dict] = None,
              session_id: Optional[str] = None, _retried: bool = False) -> dict:
    """Вызов CDP с ленивым подключением и одним переподключением при обрыве."""
    global _RAW_CLIENT
    with _RAW_LOCK:
        if _RAW_CLIENT is None:
            _RAW_CLIENT = _RawCdp()
        try:
            return _RAW_CLIENT.call(method, params, session_id)
        except BrowserUnavailable:
            raise
        except Exception:
            if _retried:
                raise BrowserUnavailable(f"CDP-соединение оборвано на {method}")
            try:
                _RAW_CLIENT.close()
            except Exception:
                pass
            _RAW_CLIENT = None
    return _raw_call(method, params, session_id, _retried=True)


def _raw_tab(tab_id: int) -> dict:
    tab = _RAW_TABS.get(tab_id)
    if tab is None:
        raise BrowserUnavailable("фоновая вкладка закрыта")
    return tab


def _raw_open(url: str) -> int:
    """Фоновая вкладка: createTarget(background:true) + flat-сессия → tab_id."""
    tid = _raw_call("Target.createTarget",
                    {"url": "about:blank", "background": True})["targetId"]
    sid = _raw_call("Target.attachToTarget",
                    {"targetId": tid, "flatten": True})["sessionId"]
    tab_id = _RAW_ID_OFFSET + 1 + (max(_RAW_TABS.keys()) - _RAW_ID_OFFSET
                                   if _RAW_TABS else 0)
    _RAW_TABS[tab_id] = {"targetId": tid, "sessionId": sid}
    if url and url != "about:blank":
        _raw_call("Page.navigate", {"url": url}, session_id=sid)
    logger.info(f"[BrowserActions] Открыта фоновая вкладка #{tab_id}: {url[:80]}")
    return tab_id


def _raw_drop(tab_id: int):
    _RAW_TABS.pop(tab_id, None)


def _raw_eval(tab_id: int, js: str) -> str:
    tab = _raw_tab(tab_id)
    try:
        res = _raw_call("Runtime.evaluate",
                        {"expression": js, "returnByValue": True,
                         "awaitPromise": True},
                        session_id=tab["sessionId"])
    except BrowserUnavailable:
        _raw_drop(tab_id)
        raise
    if res.get("exceptionDetails"):
        raise BrowserUnavailable("JS во вкладке упал")
    return str((res.get("result") or {}).get("value") or "")


def _raw_url(tab_id: int) -> str:
    tab = _raw_tab(tab_id)
    try:
        info = _raw_call("Target.getTargetInfo", {"targetId": tab["targetId"]})
    except BrowserUnavailable:
        _raw_drop(tab_id)
        raise BrowserUnavailable("фоновая вкладка закрыта")
    return str((info.get("targetInfo") or {}).get("url") or "")


def _raw_enter(tab_id: int):
    """Доверенный Enter (Input-домен) — как keyboard.press у playwright."""
    tab = _raw_tab(tab_id)
    for ev_type in ("rawKeyDown", "keyUp"):
        _raw_call("Input.dispatchKeyEvent",
                  {"type": ev_type, "key": "Enter", "code": "Enter",
                   "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13},
                  session_id=tab["sessionId"])


def _raw_insert_text(tab_id: int, text: str):
    """Вставка текста IME-путём (Input.insertText) — для управляемых
    редакторов (Lexical на kimi.ai), откатывающих программный JS-fill."""
    tab = _raw_tab(tab_id)
    _raw_call("Input.insertText", {"text": text}, session_id=tab["sessionId"])


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


def _raw_state(tab_id: int) -> str:
    """Отпечаток страницы фоновой вкладки (аналог _page_state)."""
    try:
        return _raw_eval(tab_id, _DOM_STATE_JS)
    except Exception:
        return f"<transition:{time.monotonic()}>"


def _raw_chat_fill_send(tab_id: int, input_sel: str, text: str) -> str:
    """Ввод+отправка в фоновой вкладке: JS-fill + Enter, closed-loop
    подтверждение (поле очистилось/страница изменилась) как у playwright."""
    sel = json.dumps(input_sel, ensure_ascii=False)
    if _raw_eval(tab_id, _CHAT_FILL_JS % (sel, json.dumps(text, ensure_ascii=False))) != "ok":
        raise BrowserUnavailable("поле чата не приняло ввод")
    got = _raw_eval(tab_id, _CHAT_FIELD_JS % sel)
    if _norm_ws(text[:200]) not in _norm_ws(got):
        # Управляемые редакторы (Lexical — kimi.ai) откатывают JS-fill:
        # состояние редактора не из DOM, записанное стирается при reconcile.
        # Обход: selectAll (заменить возможный частичный fill) + IME-вставка —
        # она идёт через editing-пайплайн редактора. Применяется асинхронно —
        # короткий опрос.
        _raw_eval(tab_id,
                  "(function(){var e=document.querySelector(" + sel + ");"
                  "if(e){e.focus();document.execCommand('selectAll');}})()")
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
        if not cur.strip() or _raw_state(tab_id) != pre:
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

    def _do(page_eval) -> bool:
        try:
            return str(page_eval(js)) == "attached"
        except Exception as e:
            logger.info(f"[BrowserActions] Вставка картинки в чат не "
                        f"удалась: {e}")
            return False

    if tab_id is not None and tab_id in _RAW_TABS:
        return _do(lambda j: _raw_eval(tab_id, j))
    backend = _select_backend(tab_op=True)
    if backend != "cdp":
        return False

    def _op(w):
        page = w.page_for(host_part, tab_id)
        return _do(lambda j: page.evaluate(j))

    return bool(_WORKER.submit(_op))


def open_new_tab(url: str, background: bool = False) -> int:
    """Новая вкладка с url → стабильный id для адресации следующих команд
    («на этой странице …»). CDP: id из реестра воркера; macOS fallback:
    AppleScript-id (Chrome) или наш реестр по URL (Safari).
    Операция открытия — браузер разрешено запустить. Обычное открытие
    (background=False) на CDP/macOS делает вкладку АКТИВНОЙ в её окне,
    но окно не всплывает: «открой сайт» готовит вкладку, не дёргая
    пользователя (см. _select_browser_tab_quietly).
    background=True — вкладка в фоне БЕЗ выдёргивания окна (сырой CDP;
    только eval/навигация/ввод в чат) и без переключения на неё. Для
    служебных вкладок web_llm.
    AppleScript-фолбэка для background нет намеренно: открытая им вкладка
    неуправляема (JS/ввод недоступны) и веб-чат всё равно упадёт — лучше
    сразу понятная ошибка, чем мусорная вкладка (кейс 22.08). Safari:
    фоновых вкладок нет — открываем обычную видимую."""
    backend = _select_backend(tab_op=False)
    if background:
        if backend == "safari":
            logger.info("[BrowserActions] Safari: фоновых вкладок нет — "
                        "служебная вкладка будет видимой")
            return _safari_open_tab(url)
        if backend != "cdp":
            raise BrowserUnavailable(
                "веб-чат требует CDP: браузер с отладкой недоступен, "
                "а открытая через AppleScript вкладка неуправляема")
        try:
            return _raw_open(url)
        except Exception as e:
            logger.info(f"[BrowserActions] фоновое открытие не сработало "
                        f"({e}) — обычная вкладка")
    if backend == "cdp":
        return _WORKER.submit(lambda w: w.new_page(url))
    if backend == "safari":
        return _safari_open_tab(url)
    return _open_tab_applescript(url)


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
    "var e=document.querySelector('main,article,[role=main]')||document.body;"
    "(e?(e.innerText||''):'').replace(/\\n{3,}/g,'\\n\\n').trim().slice(0,3000)"
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
    if tab_id is not None and tab_id in _RAW_TABS:
        _raw_call("Page.navigate", {"url": url},
                  session_id=_raw_tab(tab_id)["sessionId"])
        return
    backend = _select_backend(tab_op=True)
    if backend == "safari":
        _safari_navigate(tab_id, host_part, url)
        return
    if backend != "cdp":
        raise BrowserUnavailable("навигация вкладки работает только с CDP")

    def _op(w):
        page = w.page_for(host_part, tab_id)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass  # догружается в фоне — готовность ждёт читающая сторона

    return _WORKER.submit(_op)


def chat_fill_send(host_part: Optional[str], tab_id: Optional[int],
                   input_sel: str, text: str) -> str:
    """Быстрый ввод в поле чата по селектору адаптера (fill — мгновенно,
    посимвольный набор для длинных промптов не годится) + Enter. Отправка
    подтверждается: поле очистилось или страница изменилась."""
    text = str(text or "").strip()
    if not text:
        raise BrowserUnavailable("пустой текст — нечего отправлять")
    if tab_id is not None and tab_id in _RAW_TABS:
        return _raw_chat_fill_send(tab_id, input_sel, text)
    backend = _select_backend(tab_op=True)
    if backend == "safari":
        return _safari_chat_fill_send(host_part, tab_id, input_sel, text)
    if backend != "cdp":
        raise BrowserUnavailable("chat-ввод работает только с CDP-бэкендом")

    def _op(w):
        page = w.page_for(host_part, tab_id)
        loc = page.locator(input_sel).first
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
            if not cur.strip() or _page_state(page) != pre:
                return "sent"
            time.sleep(0.25)
        raise FillUncertain("промпт введён, Enter нажат, но поле не "
                            "очистилось — не уверен, что ушло")

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
    if tab_id is not None and tab_id in _RAW_TABS:
        return _raw_eval(tab_id, js)
    backend = _select_backend(tab_op=True)
    if backend == "cdp":
        return _WORKER.submit(
            lambda w: w.eval_js(host_part, js, tab_id=tab_id, front=False))
    if backend == "safari":
        return _run_safari_events(host_part, js, tab_id=tab_id)
    return _run_apple_events(host_part, js, tab_id=tab_id)


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
        if tab_id is not None and tab_id in _RAW_TABS:
            raw = _raw_eval(tab_id, js)
        else:
            backend = _select_backend(tab_op=True)
            if backend == "cdp":
                raw = _WORKER.submit(
                    lambda w: w.eval_js(host_part, js, tab_id=tab_id, front=False))
            elif backend == "safari":
                raw = _run_safari_events(host_part, js, tab_id=tab_id)
            else:
                raw = _run_apple_events(host_part, js, tab_id=tab_id)
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None, "", False
    if not data.get("found"):
        return None, "", False
    return (int(data.get("count") or 0), str(data.get("text") or ""),
            bool(data.get("done")))


def tab_url(host_part: Optional[str] = None, tab_id: Optional[int] = None) -> str:
    """Текущий URL вкладки. Для web_llm: после первого сообщения чат
    получает постоянный адрес — запоминаем его как «наш чат»."""
    if tab_id is not None and tab_id in _RAW_TABS:
        return _raw_url(tab_id)
    backend = _select_backend(tab_op=True)
    if backend == "safari":
        try:
            return _safari_tab_url(tab_id, host_part)
        except BrowserUnavailable:
            return ""
    if backend != "cdp":
        return ""
    return _WORKER.submit(lambda w: str(w.page_for(host_part, tab_id).url))


def eval_js(host_part: Optional[str], tab_id: Optional[int], js: str) -> str:
    """Произвольный JS во вкладке (служебные UI-операции web_llm вроде
    переключения режима чата). CDP или Safari, без выдёргивания на передний
    план. evaluate ждёт промисы — асинхронные сценарии возвращают Promise."""
    if tab_id is not None and tab_id in _RAW_TABS:
        return _raw_eval(tab_id, js)
    backend = _select_backend(tab_op=True)
    if backend == "safari":
        return _safari_exec(host_part, js, tab_id)
    if backend != "cdp":
        raise BrowserUnavailable("eval_js работает только с CDP-бэкендом")
    return str(_WORKER.submit(
        lambda w: w.eval_js(host_part, js, tab_id=tab_id, front=False)) or "")


def wait_input(host_part: Optional[str], tab_id: Optional[int],
               selector: str, timeout_sec: float = 8.0) -> bool:
    """Дождаться видимого поля ввода (web_llm: первая загрузка чата может
    рендериться дольше пары секунд). False по таймауту — не исключение:
    дальше отработает closed-loop отправки."""
    if tab_id is not None and tab_id in _RAW_TABS:
        return _raw_wait_input(tab_id, selector, timeout_sec)
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

    return _WORKER.submit(_op)


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
        if tab_id is not None and tab_id in _RAW_TABS:
            return int(_raw_eval(tab_id, js) or 0)
        backend = _select_backend(tab_op=True)
        if backend == "cdp":
            return int(_WORKER.submit(
                lambda w: w.eval_js(host_part, js, tab_id=tab_id, front=False)) or 0)
        if backend == "safari":
            return int(_run_safari_events(host_part, js, tab_id=tab_id) or 0)
        return int(_run_apple_events(host_part, js, tab_id=tab_id) or 0)
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
        raise BrowserUnavailable(
            "«отправь» (Enter) работает только с CDP-бэкендом")

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
            if not cur.strip() or _page_state(page) != pre:
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
    times — сколько раз нажать (громкость: стрелка = ~10% YouTube).
    Best effort без closed-loop проверки эффекта: клавиша может не менять
    видимый DOM (canvas-рендер) — обещаем только нажатие. CDP — playwright
    (доверенное key-событие), Safari — key code через System Events;
    AppleScript-фолбэк Chrome не умеет фокусить окно для System Events —
    честный отказ."""
    backend = _select_backend(tab_op=True)
    n = max(1, min(int(times or 1), 10))
    if backend == "safari":
        code = _MAC_KEYCODES.get(key)
        if code is None:
            raise BrowserUnavailable(f"клавиша {key} не поддерживается")
        _safari_focus_tab(tab_id, host_part)
        try:
            for _ in range(n):
                _osascript(
                    f'tell application "System Events" to key code {code}',
                    browser="safari")
        except BrowserUnavailable as e:
            raise BrowserUnavailable(
                f"{key} не нажался: {e}")
        logger.info(f"[BrowserActions] {key}×{n} (Safari) — "
                    f"{host_part or f'вкладка #{tab_id}'}")
        return "ok"
    if backend != "cdp":
        raise BrowserUnavailable(
            f"клавиша {key} работает только с CDP-бэкендом")

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
        if key.startswith("Arrow"):
            try:
                loc = page.locator("video").first
                for _ in range(n):
                    loc.press(key, timeout=2000)
                logger.info(f"[BrowserActions] {key}" +
                            (f"×{n}" if n > 1 else "") +
                            f" на видео ({host_part or f'вкладка #{tab_id}'})")
                return "ok"
            except Exception:
                pass
        for _ in range(n):
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
    backend = _select_backend(tab_op=True)
    try:
        if backend == "cdp":
            out = _WORKER.submit(
                lambda w: w.eval_js(host_part, _MODAL_VISIBLE_JS,
                                    tab_id=tab_id, front=False))
        elif backend == "safari":
            out = _safari_exec(host_part, _MODAL_VISIBLE_JS, tab_id)
        else:
            out = _run_apple_events(host_part, _MODAL_VISIBLE_JS, tab_id=tab_id)
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
    backend = _select_backend(tab_op=True)
    try:
        if backend == "cdp":
            out = _WORKER.submit(
                lambda w: w.eval_js(host_part, _OPEN_LIST_VISIBLE_JS,
                                    tab_id=tab_id, front=False))
        elif backend == "safari":
            out = _safari_exec(host_part, _OPEN_LIST_VISIBLE_JS, tab_id)
        else:
            out = _run_apple_events(host_part, _OPEN_LIST_VISIBLE_JS, tab_id=tab_id)
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
        raise BrowserUnavailable("Escape работает только с CDP-бэкендом")

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
# треку). Выбор — по словам подписи в предках: ключ = слова*10 − глубина,
# своя строка (мелкий предок) бьёт общую секцию при равном числе слов
_SET_SLIDER_JS = (
    "(function(label, value){"
    "function norm(s){return (s||'').toLowerCase().replace(/ё/g,'е')"
    ".replace(/\\s+/g,' ').trim();}"
    "var els=document.querySelectorAll('input[type=range],[role=slider]');"
    "var live=[];"
    "for(var i=0;i<els.length;i++){var r0=els[i].getBoundingClientRect();"
    "if(r0.width>=10&&r0.height>=4)live.push(els[i]);}"
    "if(!live.length)return '{\"st\":\"none\"}';"
    "var words=norm(label).split(' ').filter(function(w){return w.length>=3;});"
    "var best=null,bestKey=-1;"
    "for(var j=0;j<live.length;j++){var el=live[j];"
    "var chain=[];var lab=el.closest('label');if(lab)chain.push(lab);"
    "var p=el;for(var i2=0;i2<4&&p;i2++){p=p.parentElement;"
    "if(p)chain.push(p);}"
    "var key=-1;"
    "for(var d=0;d<chain.length;d++){"
    "var ctx=norm((chain[d].innerText||'').slice(0,300));"
    "var s=0;for(var k=0;k<words.length;k++){"
    "if(ctx.indexOf(words[k])>=0)s++;}"
    "if(s>0){var kk=s*10-d;if(kk>key)key=kk;}}"
    "if(key>bestKey){bestKey=key;best=el;}}"
    "if(!best){"
    "if(words.length&&live.length>1)return '{\"st\":\"no-match\"}';"
    "best=live[0];}"
    "var min=parseFloat(best.min||best.getAttribute('aria-valuemin')||'0');"
    "var max=parseFloat(best.max||best.getAttribute('aria-valuemax')||'100');"
    "var v=Math.max(min,Math.min(max,value));"
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
    "x:Math.round(r.left+r.width*ratio),y:Math.round(r.top+r.height/2)});"
    "})(%s,%s)"
)
# Прочитать фактическое значение помеченного слайдера и снять метку
_SLIDER_VERIFY_JS = (
    "(function(){var e=document.querySelector('[data-vpc-slider]');"
    "if(!e)return '';"
    "var v=(e.value!==undefined)?e.value:e.getAttribute('aria-valuenow');"
    "e.removeAttribute('data-vpc-slider');"
    "return String(v);})()"
)


def set_slider(host_part: Optional[str], label: str, value: int,
               tab_id: Optional[int] = None) -> str:
    """Перетащить слайдер: «рабочие часы в день» → 8. Возвращает фактически
    установленное значение (строкой; клампится в min..max самим виджетом).
    Нет слайдеров / подпись не совпала / виджет значение не принял —
    BrowserUnavailable с честным текстом."""
    js = _SET_SLIDER_JS % (json.dumps(label or "", ensure_ascii=False),
                           int(value))
    backend = _select_backend(tab_op=True)

    def _verify(page_eval) -> str:
        time.sleep(0.35)  # фреймворк может перерисовать и сбросить значение
        got = str(page_eval(_SLIDER_VERIFY_JS) or "").strip()
        return got

    if backend == "safari":
        raw = str(_safari_exec(host_part, js, tab_id) or "")
        try:
            st = json.loads(raw)
        except ValueError:
            st = {"st": ""}
        if st.get("st") == "range":
            got = _verify(lambda j: _safari_exec(host_part, j, tab_id))
            if got and float(got) == float(st["v"]):
                logger.info(f"[BrowserActions] Слайдер «{label[:30]}» → {got} "
                            f"({host_part or f'вкладка #{tab_id}'})")
                return got
            _safari_exec(host_part,
                         "(function(){var e=document.querySelector("
                         "'[data-vpc-slider]');if(e)e.removeAttribute("
                         "'data-vpc-slider');})()", tab_id)
            raise BrowserUnavailable(
                f"слайдер не принял значение (осталось {got or 'прежним'})")
        if st.get("st") == "custom":
            _safari_exec(host_part,
                         "(function(){var e=document.querySelector("
                         "'[data-vpc-slider]');if(e)e.removeAttribute("
                         "'data-vpc-slider');})()", tab_id)
            raise BrowserUnavailable(
                "слайдер нестандартный (не input) — на Safari не потяну")
        if st.get("st") == "no-match":
            raise BrowserUnavailable(f"слайдер с подписью «{label[:40]}» "
                                     "не нашёлся")
        raise BrowserUnavailable("на странице нет слайдеров")
    if backend != "cdp":
        raise BrowserUnavailable("Слайдер работает только с CDP-бэкендом")

    def _op(w):
        page = w.page_for(host_part, tab_id)
        raw = str(page.evaluate(js) or "")
        try:
            st = json.loads(raw)
        except ValueError:
            st = {"st": ""}
        kind = st.get("st")
        if kind == "no-match":
            raise BrowserUnavailable(f"слайдер с подписью «{label[:40]}» "
                                     "не нашёлся")
        if kind not in ("range", "custom"):
            raise BrowserUnavailable("на странице нет слайдеров")
        if kind == "custom":
            # Кастомный слайдер: доверенный клик по точке трека (большинство
            # виджетов прыгают в позицию клика)
            page.mouse.click(float(st["x"]), float(st["y"]))
        got = _verify(page.evaluate)
        if got:
            try:
                ok = abs(float(got) - float(st["v"])) < 0.51
            except ValueError:
                ok = False
            if ok:
                logger.info(f"[BrowserActions] Слайдер «{label[:30]}» → {got} "
                            f"({host_part or f'вкладка #{tab_id}'})")
                return got
        # Виджет значение не принял (вернул прежнее/пусто)
        page.evaluate(
            "(function(){var e=document.querySelector('[data-vpc-slider]');"
            "if(e)e.removeAttribute('data-vpc-slider');})()")
        raise BrowserUnavailable(
            f"слайдер не принял значение (осталось {got or 'прежним'})")

    return _WORKER.submit(_op)


def read_text(host_part: Optional[str], tab_id: Optional[int] = None,
              mode: str = "last") -> str:
    """Текст со страницы без побочек (front=False — вкладку не выдёргиваем).
    mode=last — последнее сообщение чата (роль-префикс), page — основной
    текст (main/article). Нечего читать — BrowserUnavailable с честным текстом."""
    js = _READ_PAGE_JS if mode == "page" else _READ_LAST_JS
    backend = _select_backend(tab_op=True)
    if backend == "cdp":
        out = _WORKER.submit(
            lambda w: w.eval_js(host_part, js, tab_id=tab_id, front=False))
    elif backend == "safari":
        out = _run_safari_events(host_part, js, tab_id=tab_id)
    else:
        out = _run_apple_events(host_part, js, tab_id=tab_id)
    out = str(out or "").strip()
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
    "function N(s){return (s||'').toLowerCase().replace(/[-‐-―«»\"']/g,' ')"
    ".replace(/\\s+/g,' ').trim();}"
    "function stem(w){var pl=w.length>=7?w.length-3:(w.length>=6?w.length-2:0);"
    "return pl>=4?w.slice(0,pl):w;}"
    "function allW(t,words){var ws=t.split(' ');"
    "for(var i=0;i<words.length;i++){var st=stem(words[i]),hit=false;"
    "for(var k=0;k<ws.length;k++){if(ws[k].indexOf(st)===0){hit=true;break;}}"
    "if(!hit)return false;}return true;}"
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
    "return txt.slice(0,2200);})('__Q__')"
)


def read_section(host_part: Optional[str], query: str,
                 tab_id: Optional[int] = None) -> str:
    """Текст секции открытой страницы по её заголовку — для вопросов вида
    «что находится в X?»: вытащенное отдаётся LLM контекстом, список/ответ
    формулирует она. Секция не нашлась — пустая строка (не ошибка: вопрос
    уходит в обычный диалог). Без побочек (front=False)."""
    q = re.sub(r"[\"'\\]", "", " ".join(str(query or "").split()))[:60].strip()
    if not q:
        raise BrowserUnavailable("пустой запрос секции")
    raw = _run_js(host_part, _READ_SECTION_JS.replace("__Q__", q),
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
                    if (urlparse(p.url).hostname or "").lower()
                    not in _SERVICE_HOSTS]
        return _WORKER.submit(_op)
    if backend == "safari":
        return [(u, urlparse(u).hostname or "") for u in _safari_page_urls()
                if (urlparse(u).hostname or "").lower() not in _SERVICE_HOSTS]
    return []


def list_tabs() -> List[Tuple[int, str, str, str]]:
    """(tab_id, url, host, title) живых вкладок — для «перейди на вкладку X»
    и «какие вкладки открыты». Только CDP: нужны title и bring_to_front,
    на AppleScript/Safari — честный отказ."""
    if _select_backend(tab_op=True) != "cdp":
        raise BrowserUnavailable(
            "переключение вкладок работает только в браузере бота "
            "(Chrome с отладкой)")
    return _WORKER.submit(lambda w: w.list_tabs_detailed())


def activate_tab(tab_id: int) -> Tuple[str, str]:
    """Вкладку на передний план → (url, title). BrowserUnavailable, если
    вкладка умерла или бэкенд не CDP."""
    if _select_backend(tab_op=True) != "cdp":
        raise BrowserUnavailable(
            "переключение вкладок работает только в браузере бота "
            "(Chrome с отладкой)")

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
        return h in {str(a.get("host") or "").lower() for a in ADAPTERS.values()}
    except Exception:
        return False


def follow_popup(pre_urls: List[str],
                 timeout_sec: float = POPUP_WAIT_SEC
                 ) -> Optional[Tuple[int, str, str]]:
    """Страница, появившаяся после клика (окно входа Google и т.п.) —
    регистрируется в реестре и возвращается как (tab_id, host, url), чтобы
    следующие команды целились в неё. None — новой страницы нет. Страница,
    которая на момент проверки ещё about:blank, ждётся до навигации.
    Служебные вкладки веб-чатов (_SERVICE_HOSTS) попапом НЕ считаются:
    web_llm навигирует свою вкладку конкурентно с кликом, и её новый URL
    раньше ложно «угонял» отслеживание на chat.deepseek.com и т.п."""
    if _select_backend(tab_op=True) != "cdp":
        return None

    def _op(w):
        deadline = time.time() + timeout_sec
        while True:
            for p in w._all_pages():
                if p.url not in pre_urls and not p.url.startswith("about:"):
                    host = urlparse(p.url).hostname or ""
                    if host.lower() in _SERVICE_HOSTS:
                        continue  # служебная вкладка веб-чата, не попап клика
                    tid = w._next_tab_id
                    w._next_tab_id += 1
                    w._pages[tid] = p
                    logger.info(f"[BrowserActions] Попап после клика: "
                                f"вкладка #{tid} ({host})")
                    return tid, host, p.url
            if time.time() >= deadline:
                return None
            time.sleep(0.2)

    return _WORKER.submit(_op)


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
            for tab_id in list(_RAW_TABS):
                try:
                    if host_part in _raw_url(tab_id):
                        return tab_id
                except BrowserUnavailable:
                    _raw_drop(tab_id)
            return None
        return _find_tab_applescript(host_part)
    except BrowserUnavailable:
        return None

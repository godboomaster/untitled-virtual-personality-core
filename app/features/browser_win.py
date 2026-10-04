"""Окна Chrome бота на Windows: окна агента, скрытый пул H, тихое
переключение вкладок и явный подъём окна — monkey-patch'ем browser_actions.

Зачем отдельный модуль. browser_actions прячет/показывает окна бота и тихо
выбирает вкладку только на macOS (AppleScript); на Windows эти места —
no-op, и окна Chrome бота висят на экране и в панели задач, а фоновая
вкладка не становится активной. Основной проект трогать нельзя (macOS/Linux
обязаны вести себя ровно как раньше), поэтому вся Windows-логика здесь, а
подключает её только проверка ОС в app.main.main(): на Windows она зовёт
install(), на macOS/Linux этот модуль не импортируется вовсе.
Примитивы Win32 — в win_desktop (ctypes), здесь — только решения «какое
окно прятать/показывать и куда открывать вкладку».

Решения пользователя (не менять):
1. «Спрятать» окно = увести его ЗА ПРЕДЕЛЫ виртуального экрана без
   активации и убрать из панели задач/Alt+Tab. SW_HIDE постоянным
   состоянием — нельзя: Chrome перестаёт рисовать скрытое окно. «Показать» —
   вернуть сохранённое место и кнопку в панели задач, тоже без активации.
2. Окна пула V двух ролей. ПОЛЬЗОВАТЕЛЬСКИЕ — туда открываются вкладки по
   командам режима управления («открой ютуб»); их не прячем никогда. Окна
   АГЕНТА — служебные вкладки бота (фоновые вкладки пула V, фолбэк
   background-открытия, вкладки задачи агента «закажи пиццу»). Окно агента
   ВИДНО, пока пользователь в режиме управления (control_mode_any()), и
   стоит за экраном в остальное время. Пул V, поднятый вне режима
   управления, прячет своё первое окно при запуске (как на macOS) — это окно
   агента.
3. Пул H в режиме hidden: все его окна всегда за экраном (и новые окна-
   вкладки _raw_open). headless и headed (rescue: человек решает капчу) —
   не трогаем.
4. Тихое переключение вкладки: вкладка активна в своём окне, окно не
   всплывает; доля секунды мигания допустима — перехваченный фокус
   возвращается прежнему окну, мигание кнопки гасится.
5. Явный подъём (_focus_browser_tab): вкладка → её окно → на передний план.
   Скрытое окно агента вне режима управления НЕ показываем.

Отрисовка за экраном (проба CDP, Chrome 154): активная вкладка окна за
экраном — visibilityState "visible", rAF тикает; у СВЁРНУТОГО окна — "hidden".
Поэтому окна никогда не сворачиваем (hide_offscreen сперва разворачивает).
Учёт перекрытия окон Windows (CalculateNativeWinOcclusion) страницу не
прячет: --disable-backgrounding-occluded-windows (CHROME_NO_THROTTLE_FLAGS)
велит Chrome считать перекрытое окно видимым — флаги запуска не трогаем.

Что патчится (каждый патч: путь Windows в try, любой сбой → лог и ШТАТНОЕ
поведение; VPC_WIN_WINDOWS=0 — install() ничего не делает):
- _raw_call — только Target.createTarget: пул V → вкладка в окне агента;
  пул H в режиме hidden → новое окно сразу за экраном.
- open_new_tab — метка маршрута (агент/пользователь) для потока воркера.
- _CdpWorker._new_page_quiet — открытие вкладки по маршруту.
- _CdpWorker._activate_tab_quietly — тихое переключение (Windows).
- _focus_browser_tab — явный подъём окна (Windows).
- _hide_pool_window — скрытие окна при запуске (Windows).
- set_control_mode — показ/скрытие окон агента при входе/выходе.
- browser_actions.subprocess — прокси: учёт запусков Chrome наших пулов.
  Пул H hidden получает --window-position за экраном (без вспышки; флаг
  Chrome применяет к КАЖДОМУ своему окну до конца жизни процесса — пулу H
  это и нужно). Пулу V флаг НЕ даём: его окна пользователя, Ctrl+N, попапы
  сайтов рождались бы за экраном; первое окно скрытого запуска прячется
  опросом сразу после Popen (вспышка — доли секунды).
- TaskAgent._execute — пометка потока «исполняет шаг агента».
"""

from __future__ import annotations

import functools
import inspect
import itertools
import logging
import os
import subprocess as _real_subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from app.features import win_desktop as wd

logger = logging.getLogger(__name__)

# Выключатель: VPC_WIN_WINDOWS=0 — Windows-логика окон не ставится вовсе
ENV_SWITCH = "VPC_WIN_WINDOWS"

# Роли окон в реестре
_AGENT = "agent"    # окно агента пула V: видно только в режиме управления
_USER = "user"      # окно пользователя пула V: не прячем никогда
_HIDDEN = "hidden"  # окно пула H в режиме hidden: всегда за экраном

# Имена пулов — как в browser_actions (сверяются при install)
_POOL_H = "h"
_POOL_V = "v"

_SETTLE_SEC = 0.25        # страж фокуса: Chrome активирует окно асинхронно
_AUX_TIMEOUT_SEC = 5.0    # потолок вспомогательных CDP-вызовов (окна/таргеты)
_NEW_HWND_WAIT_SEC = 2.0  # окно новой цели появляется чуть позже ответа CDP
_SIZED_GRACE_SEC = 0.3    # окно, совпавшее с CDP только размером, — после паузы
_NEW_TARGET_WAIT_SEC = 3.0  # вкладка window.open появляется в getTargets не сразу
_LAUNCH_WAIT_SEC = 5.0    # окна только что запущенного Chrome
_EARLY_HIDE_SEC = 30.0    # опрос первого окна скрытого запуска (холодный старт)
_EARLY_POLL_SEC = 0.05
_LAUNCH_GRACE_SEC = 20.0  # после скрытого запуска подметальщик дежурит столько
_SWEEP_SEC = 1.5          # период подметальщика всплывающих окон
_EXPECT_PAGE_MS = 8000    # playwright: событие новой страницы
_USER_EXPECT_MS = 5000    # как у штатного _new_page_quiet
# Окно агента, созданное за экраном: его вёрстку видит агент (скриншоты,
# vision), размер — обычного ноутбучного окна
_AGENT_W, _AGENT_H = 1280, 900
# Пул H hidden запускается с --window-size=1920,1080 — новые окна того же размера
_POOL_H_W, _POOL_H_H = 1920, 1080
# Допуски сверки окна CDP (DIP) с HWND (физические px)
_SIZE_TOL_DIP = 24
_POS_TOL_DIP = 48

# Новая вкладка в окне-источнике: _blank с открывателем (по openerId её
# находим в Target.getTargets), userGesture в Runtime.evaluate — иначе
# блокировщик всплывающих окон вернёт null
_OPEN_JS = ("(() => { try { return !!window.open('about:blank', '_blank'); }"
            " catch (e) { return false; } })()")
_SEVER_JS = "(() => { try { window.opener = null; } catch (e) {} return true; })()"


# ── Состояние модуля ──

_STATE_LOCK = threading.RLock()  # install/uninstall
_INSTALLED = False
_BA: Any = None                  # app.features.browser_actions (после install)
_ORIG: Dict[str, Any] = {}       # оригиналы патчей по ключу
_ORIG_RAW_CALL: Any = None       # оригинальный _raw_call: внутри — только он
# (модуль, имя, оригинал) — копии патчимых функций в других модулях app.*
# («from app.features.browser_actions import X» держит свою ссылку)
_ALIASES: List[Tuple[Any, str, Any]] = []

_TLS = threading.local()         # .agent_depth — поток внутри TaskAgent._execute

# Метки маршрута: url → {токен: роль}. Ключ — URL, а не «глобал под локом
# на весь вызов»: open_new_tab(background=True) пула H идут параллельно из
# каналов веб-чата, и лок на всё время открытия (до десятков секунд
# навигации) выстроил бы их в очередь за чужой вкладкой. До воркера URL
# доходит тем же объектом (new_page(url) → _open_page_gateway_retry →
# _new_page_quiet(ctx, url)), так что метку по нему видно точно
_HINTS_LOCK = threading.Lock()
_HINTS: Dict[str, Dict[int, str]] = {}
_HINT_SEQ = itertools.count(1)

# Создание вкладок/окон по маршруту — по одному: поиск новой цели по
# «было/стало» (таргеты, HWND) не должен видеть соседнюю
_CREATE_LOCK = threading.RLock()
# Показ/скрытие окон пула (переходы режима управления, подметальщик,
# скрытие при запуске) — по одному. Под ним только Win32 и реестр, ни
# одного ожидания воркера/CDP: его берут и из потока воркера
_SYNC_LOCK = threading.RLock()

# Запуски Chrome наших пулов (прокси Popen): пул → (Popen, скрытый?,
# monotonic). Храним сам Popen, а не голый pid: живость — по его poll()
# (на Windows — по открытому хэндлу процесса, и пока он открыт, система этот
# pid никому не отдаст). Голый pid умершего Chrome Windows быстро
# переиспользует — и «окна пула» оказались бы окнами чужой программы
# (Slack, VS Code и прочие Electron — тоже класс Chrome_WidgetWin_*)
_LAUNCHED: Dict[str, Tuple[Any, bool, float]] = {}
# (пул, pid) → можно ли управлять окнами (не основной профиль пользователя)
_MANAGED_CACHE: Dict[Tuple[str, int], bool] = {}
# Последний известный pid браузера пула + соединение, по которому он узнан:
# подметальщик не должен каждые полторы секунды спрашивать его у Chrome по
# CDP. Кэш верен, пока это соединение — текущее у пула (перезапуск Chrome
# пересобирает соединение → кэш протух, даже если старый pid уже занял
# чужой процесс)
_PID_CACHE: Dict[str, Tuple[int, Any]] = {}
# windowId окон агента, чей HWND при создании не нашёлся (окно появилось
# позже ожидания): подметальщик опознаёт их по windowId → monotonic
_PENDING_AGENT_WIDS: Dict[int, float] = {}
_PENDING_TTL_SEC = 300.0

_SWEEP_LOCK = threading.Lock()
_SWEEP_STOP = threading.Event()
_SWEEPER: Optional[threading.Thread] = None
# Поколение подметальщика: uninstall его меняет. Поток, который в момент
# uninstall был внутри _sweep_once, флаг _SWEEP_STOP мог «проспать» (его уже
# сбросил следующий _ensure_sweeper) — по поколению он всё равно выходит, и
# двух подметальщиков после uninstall/install не бывает
_SWEEP_GEN = 0

_FAILS: Set[str] = set()


def _note_fail(where: str, e: BaseException) -> None:
    """Сбой пути Windows: первый раз — warning (видно, что логика окон не
    сработала и включилось штатное поведение), дальше — debug, без спама."""
    if where in _FAILS:
        logger.debug(f"[BrowserWin] {where}: {e}")
        return
    _FAILS.add(where)
    logger.warning(f"[BrowserWin] {where}: {e} — штатное поведение")


# ── Реестр окон ──

class _WinRec:
    """Окно Chrome, которым мы управляем. hidden — что сделали МЫ (истину
    «за экраном ли оно» проверяет win_desktop.is_offscreen)."""
    __slots__ = ("hwnd", "role", "window_id", "hidden", "ts")

    def __init__(self, hwnd: int, role: str, window_id: Optional[int] = None,
                 hidden: bool = False):
        self.hwnd = int(hwnd)
        self.role = role
        self.window_id = window_id  # windowId CDP (Browser.getWindowForTarget)
        self.hidden = hidden
        self.ts = time.monotonic()  # последнее использование: свежие — первыми


class _Registry:
    """Окна по пулам, ключ — HWND. Привязан к pid браузера пула: Chrome
    перезапущен (pid другой) — все HWND прежнего мертвы, реестр пула
    сбрасывается; мёртвые HWND (IsWindow) выбрасываются при каждом чтении."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._pid: Dict[str, Optional[int]] = {_POOL_H: None, _POOL_V: None}
        self._wins: Dict[str, Dict[int, _WinRec]] = {_POOL_H: {}, _POOL_V: {}}

    def pid(self, pool: str) -> Optional[int]:
        with self.lock:
            return self._pid.get(pool)

    def bind_pid(self, pool: str, pid: Optional[int]) -> None:
        if not pid:
            return
        with self.lock:
            old = self._pid.get(pool)
            if old == int(pid):
                return
            if old is not None and self._wins[pool]:
                logger.info(f"[BrowserWin] Пул {pool.upper()}: Chrome сменился "
                            f"(pid {old} → {pid}) — реестр окон сброшен")
            for h in self._wins[pool]:
                wd.forget(h)
            self._pid[pool] = int(pid)
            self._wins[pool] = {}
            if pool == _POOL_V and old is not None:
                _PENDING_AGENT_WIDS.clear()  # windowId прежнего Chrome

    def _prune(self, pool: str) -> None:
        pid = self._pid.get(pool)
        dead = [h for h in self._wins[pool]
                if not wd.is_window(h) or (pid and wd.window_pid(h) != pid)]
        for h in dead:
            self._wins[pool].pop(h, None)

    def recs(self, pool: str) -> List[_WinRec]:
        with self.lock:
            self._prune(pool)
            return list(self._wins[pool].values())

    def get(self, pool: str, hwnd: int) -> Optional[_WinRec]:
        with self.lock:
            return self._wins[pool].get(int(hwnd))

    def put(self, pool: str, hwnd: int, role: str,
            window_id: Optional[int] = None,
            hidden: Optional[bool] = None) -> _WinRec:
        with self.lock:
            rec = self._wins[pool].get(int(hwnd))
            if rec is None:
                rec = _WinRec(hwnd, role, window_id, bool(hidden))
                self._wins[pool][rec.hwnd] = rec
            else:
                rec.role = role
                if window_id is not None:
                    rec.window_id = window_id
                if hidden is not None:
                    rec.hidden = bool(hidden)
        if pool == _POOL_V and role == _AGENT:
            _claim(rec.hwnd)
        return rec

    def agents(self) -> List[_WinRec]:
        # Окна агента пула V, свежие первыми
        recs = [r for r in self.recs(_POOL_V) if r.role == _AGENT]
        return sorted(recs, key=lambda r: r.ts, reverse=True)

    def reset(self) -> None:
        with self.lock:
            self._pid = {_POOL_H: None, _POOL_V: None}
            self._wins = {_POOL_H: {}, _POOL_V: {}}


_REG = _Registry()


# ── Владелец окна (Chrome пула V общий для процессов бота) ──
# Режим управления у каждого процесса бота свой, а Chrome пула V — общий
# (процесс api и процессы персон подключаются к одному порту). Окно агента,
# спрятанное процессом A, для процесса B — «неучтённое окно за экраном»; без
# метки B присвоил бы его себе и прятал бы каждые полторы секунды, пока A в
# режиме управления его показывает. Метка — pid владельца в свойстве окна
# (win_desktop.set_owner_tag); живой чужой владелец — окно не наше.

def _claim(hwnd: int) -> None:
    # Окно агента — наше: метка с нашим pid (без лишней записи, если уже)
    try:
        me = os.getpid()
        if wd.owner_tag(hwnd) != me:
            wd.set_owner_tag(hwnd, me)
    except Exception as e:
        logger.debug(f"[BrowserWin] Метка владельца окна: {e}")


def _foreign(hwnd: int) -> bool:
    """Окно помечено ДРУГИМ живым процессом бота — не трогаем. Метки нет
    или владелец умер (бот перезапущен, Chrome жил) — окно ничьё, наше."""
    try:
        tag = wd.owner_tag(hwnd)
    except Exception:
        return False
    return bool(tag) and tag != os.getpid() and _pid_alive(tag)


# ── pid браузеров пулов ──

def _proc_pid(proc: Any) -> Optional[int]:
    # pid живого Popen (None — нет процесса или он уже вышел)
    try:
        if proc is not None and proc.poll() is None:
            return int(proc.pid)
    except Exception:
        pass
    return None


def _pid_alive(pid: Optional[int]) -> bool:
    try:
        return bool(pid) and bool(_BA._pid_alive(int(pid)))
    except Exception:
        return False


def _in_worker_thread() -> bool:
    """Поток CDP-воркера (имя «vpc-cdp-<поколение>», см.
    _CdpWorker._ensure_loop; брошенное поколение — тоже он). Отсюда нельзя
    звать _WORKER.submit/_cdp_available (взаимная блокировка на _op_lock),
    зато можно playwright."""
    return threading.current_thread().name.startswith("vpc-cdp-")


def _pid_from_info(info: Any) -> Optional[int]:
    # SystemInfo.getProcessInfo → pid браузерного процесса. Проба CDP
    # (Chrome 154): ровно одна запись type "browser", pid — в поле "id"
    for p in (info or {}).get("processInfo") or []:
        if p.get("type") == "browser":
            pid = int(p.get("pid") or p.get("id") or 0)
            return pid or None
    return None


def _conn_current(pool: str, conn: Any) -> bool:
    # Соединение, по которому узнан pid, всё ещё текущее у пула
    if conn is None or getattr(conn, "_dead", None) is not None:
        return False  # сырой сокет уже видел обрыв — Chrome мог смениться
    if conn is (_BA._RAW_CLIENTS or {}).get(pool):
        return True
    return pool == _POOL_V and conn is _BA._WORKER._browser


def _cached_pid(pool: str) -> Optional[int]:
    entry = _PID_CACHE.get(pool)
    if not entry:
        return None
    pid, conn = entry
    if _conn_current(pool, conn) and _pid_alive(pid):
        return pid
    _PID_CACHE.pop(pool, None)
    return None


def _remember_pid(pool: str, pid: Optional[int], conn: Any) -> Optional[int]:
    if pid and conn is not None:
        _PID_CACHE[pool] = (int(pid), conn)
    return pid


def _launched_pid(pool: str) -> Optional[int]:
    entry = _LAUNCHED.get(pool)
    return _proc_pid(entry[0]) if entry else None


def _pid_via_client(pool: str) -> Optional[int]:
    """pid по УЖЕ открытому сырому сокету пула — без _raw_call: тот ради
    одного вопроса подключался бы заново, для пула H — ещё и поднимал бы
    Chrome (_ensure_pool_h_browser), а для пула V отмечал бы активность и
    откладывал его гашение по простою."""
    cl = (_BA._RAW_CLIENTS or {}).get(pool)
    if cl is None:
        return None
    try:
        pid = _pid_from_info(cl.call("SystemInfo.getProcessInfo",
                                     timeout=_AUX_TIMEOUT_SEC))
    except Exception as e:
        logger.debug(f"[BrowserWin] pid пула {pool.upper()} по сокету: {e}")
        return None
    return _remember_pid(pool, pid, cl)


def _pool_v_pid(allow_connect: bool = False) -> Optional[int]:
    """pid Chrome пула V без лишних побочек: свой Popen → запуск, замеченный
    прокси → прежний (его соединение живо) → в потоке воркера playwright
    (_browser_pid) → открытый сырой сокет → (allow_connect) SystemInfo через
    оригинальный _raw_call. Последний — только там, где пользователь и так
    работает с пулом V (вход в режим управления, создание вкладки): он
    отмечает активность пула, а подметальщику её отмечать нельзя."""
    ba = _BA
    pid = (_proc_pid(ba._WORKER._proc) or _launched_pid(_POOL_V)
           or _cached_pid(_POOL_V))
    if pid is not None:
        return pid
    browser = ba._WORKER._browser
    if _in_worker_thread() and browser is not None:
        try:
            pid = _remember_pid(_POOL_V, ba._browser_pid(ba._WORKER), browser)
        except Exception as e:
            logger.debug(f"[BrowserWin] pid пула V через playwright: {e}")
    if pid is None:
        pid = _pid_via_client(_POOL_V)
    if pid is None and allow_connect:
        try:
            pid = _pid_from_info(_ORIG_RAW_CALL(
                "SystemInfo.getProcessInfo", None, None, _POOL_V, None,
                _AUX_TIMEOUT_SEC))
            _remember_pid(_POOL_V, pid, (ba._RAW_CLIENTS or {}).get(_POOL_V))
        except Exception as e:
            logger.debug(f"[BrowserWin] pid пула V через CDP: {e}")
    return pid


def _pool_h_pid() -> Optional[int]:
    """pid Chrome пула H. Никогда не через _raw_call: для пула H он лениво
    ЗАПУСКАЕТ Chrome — узнавать pid ценой запуска браузера нельзя."""
    pid = (_proc_pid(_BA._POOL_H_PROC) or _launched_pid(_POOL_H)
           or _cached_pid(_POOL_H))
    if pid is not None:
        return pid
    return _pid_via_client(_POOL_H)


def _pool_managed(pool: str, pid: Optional[int]) -> bool:
    """Окна этого Chrome — наши: запущен этим процессом или работает на
    выделенном автоматизационном профиле. Основной профиль пользователя
    (личный Chrome с отладочным портом) не трогаем никогда."""
    if not pid:
        return False
    ba = _BA
    proc = ba._WORKER._proc if pool == _POOL_V else ba._POOL_H_PROC
    if int(pid) == _proc_pid(proc):
        return True
    key = (pool, int(pid))
    hit = _MANAGED_CACHE.get(key)
    if hit is None:
        try:
            prof = ba._pool_v_profile() if pool == _POOL_V else ba._pool_h_profile()
            hit = not ba._is_default_browser_profile(prof)
        except Exception:
            hit = False
        _MANAGED_CACHE[key] = hit
        if not hit:
            logger.info(f"[BrowserWin] Пул {pool.upper()} (pid {pid}) — основной "
                        f"профиль браузера пользователя: его окна не трогаем")
    return hit


def _pool_h_hidden(pid: Optional[int] = None) -> bool:
    """Окна Chrome пула H (pid) надо держать за экраном.

    Сначала — НУЖНЫЙ режим (_pool_h_desired_mode, общий для процессов бота
    через файл rescue): не hidden (rescue, headless) — не прячем ничего. Без
    pid на этом и всё (дешёвая предпроверка до поиска pid).

    С pid — режим именно ЭТОГО Chrome. _POOL_H_RUNNING_MODE ему верить
    нельзя без сверки: rescue перезапускает пул H, и _POOL_H_PROC уже новый
    (видимый) Chrome, а _POOL_H_RUNNING_MODE ещё «hidden» от прежнего, пока
    новый не ответит по CDP, — подметальщик спрятал бы окно, где человек
    решает капчу. А _POOL_H_MODE_PID на Windows всегда None (pid из
    SingletonLock — только на POSIX). Поэтому по старшинству:
      1) свой запуск (прокси Popen): «скрытый» посчитан из командной строки
         в момент запуска этого самого Chrome;
      2) _POOL_H_RUNNING_MODE, если _POOL_H_MODE_PID — этот pid;
      3) иначе (Chrome соседа, режим неизвестен) — как _adopt_pool_h_mode на
         macOS: видимые hidden и headed по флагам не различить, нужен
         hidden — прячем. У headless окон нет — прятать там нечего."""
    try:
        if _BA._pool_h_desired_mode() != "hidden":
            return False
    except Exception:
        return False
    if pid is None:
        return True
    entry = _LAUNCHED.get(_POOL_H)
    if entry is not None and _proc_pid(entry[0]) == int(pid):
        return bool(entry[1])
    mode = _BA._POOL_H_RUNNING_MODE
    if mode is not None and getattr(_BA, "_POOL_H_MODE_PID", None) == int(pid):
        return mode == "hidden"
    return True


def _in_launch_grace(pool: str, pid: Optional[int] = None) -> bool:
    # Только что запущенный скрытым Chrome пула: его окна — «стартовые»
    entry = _LAUNCHED.get(pool)
    if not entry or not entry[1]:
        return False
    if time.monotonic() - entry[2] >= _LAUNCH_GRACE_SEC:
        return False
    live = _proc_pid(entry[0])
    return live is not None and (pid is None or live == pid)


# ── Скрыть/показать с учётом реестра ──

def _hide_rec(pool: str, rec: _WinRec) -> bool:
    ok = wd.hide_offscreen(rec.hwnd)
    if ok:
        rec.hidden = True
    return ok


def _show_rec(rec: _WinRec) -> bool:
    ok = wd.show_onscreen(rec.hwnd)
    if ok:
        rec.hidden = False
    return ok


def _enforce_agent(rec: _WinRec, hidden: bool) -> None:
    # Окно агента — в нужном состоянии (видно ⇔ режим управления)
    if hidden:
        if not rec.hidden or not wd.is_offscreen(rec.hwnd):
            _hide_rec(_POOL_V, rec)
    elif rec.hidden or wd.is_offscreen(rec.hwnd):
        _show_rec(rec)


def _reassert_hidden() -> None:
    """Спрятанные окна, которые Chrome вернул на экран (активация, смена
    мониторов), — снова за край. Окна агента — только вне режима управления."""
    with _SYNC_LOCK:
        hrecs = _REG.recs(_POOL_H)
        if hrecs and _pool_h_hidden(_REG.pid(_POOL_H)):
            for rec in hrecs:
                if rec.hidden and not wd.is_offscreen(rec.hwnd):
                    _hide_rec(_POOL_H, rec)
        if not _BA.control_mode_any():
            for rec in _REG.agents():
                if rec.hidden and not wd.is_offscreen(rec.hwnd):
                    _hide_rec(_POOL_V, rec)


def _hidden_rec(hwnd: int) -> Optional[_WinRec]:
    # Наше спрятанное окно (пул H hidden / окно агента вне режима управления)
    if not hwnd:
        return None
    rec = _REG.get(_POOL_H, hwnd)
    if rec is not None and rec.hidden and _pool_h_hidden(_REG.pid(_POOL_H)):
        return rec
    rec = _REG.get(_POOL_V, hwnd)
    if rec is not None and rec.role == _AGENT and rec.hidden \
            and not _BA.control_mode_any():
        return rec
    return None


def _any_hidden() -> bool:
    # Есть ли сейчас спрятанные нами окна (иначе ввод «в никуда» не уйдёт)
    if any(r.hidden for r in _REG.recs(_POOL_H)) \
            and _pool_h_hidden(_REG.pid(_POOL_H)):
        return True
    return (not _BA.control_mode_any()) and any(r.hidden for r in _REG.agents())


def _drop_hidden_focus(hwnd: Optional[int] = None) -> bool:
    """Активное окно — НАШЕ спрятанное (или hwnd, если передан) и оно за
    экраном → снять с него активность (повторное скрытие: цикл SW_HIDE →
    SW_SHOWNA отдаёт активность следующему окну, и окно снова на дне
    z-порядка). Для случаев, когда прежнего окна мы не знаем: активировал
    не наш вызов (bring_to_front штатного кода до нашего патча, сам Chrome).
    True — активность снята."""
    fg = wd.foreground_window()
    if not fg:
        return False
    if hwnd is not None:
        if fg != hwnd:
            return False
    elif _hidden_rec(fg) is None:
        return False
    if not wd.is_offscreen(fg):
        return False  # окно на экране — его видно, ввод не «в никуда»
    wd.hide_offscreen(fg)
    logger.info("[BrowserWin] Невидимое окно бота стало активным — снята активность")
    return True


def _protect_hidden_focus(prev_hwnd: int) -> bool:
    """Клавиатура не должна оставаться у НЕВИДИМОГО окна. Страж win_desktop
    возвращает фокус, только если до действия он был у чужого процесса; а
    пользователь мог работать в окне пользователя того же Chrome бота —
    тогда активированное окно агента за экраном молча забрало бы ввод.
    Возвращаем фокус прежнему окну; не вышло — повторное скрытие (цикл
    SW_HIDE → SW_SHOWNA снимает с окна активность). True — фокус вернули."""
    fg = wd.foreground_window()
    if not fg or fg == prev_hwnd or _hidden_rec(fg) is None:
        return False
    if not wd.is_offscreen(fg):
        return False  # окно уже на экране — его видно, ввод не «в никуда»
    if prev_hwnd and wd.is_window(prev_hwnd) and wd.force_foreground(prev_hwnd):
        logger.info("[BrowserWin] Невидимое окно бота забрало фокус — вернули")
        return True
    wd.hide_offscreen(fg)
    logger.info("[BrowserWin] Невидимое окно бота забрало фокус — снята активность")
    return False


class _Guard:
    """Страж фокуса вокруг действия, которое может активировать окно Chrome
    бота: win_desktop.FocusGuard (фокус ушёл из чужого процесса в Chrome
    бота — вернуть) + _protect_hidden_focus (ввод не остаётся у невидимого
    окна, даже если до действия пользователь был в том же Chrome)."""

    def __init__(self, pid: int):
        self._pid = int(pid)
        self._inner = wd.FocusGuard({self._pid}, settle_sec=_SETTLE_SEC)
        self.restored = False

    def __enter__(self) -> "_Guard":
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._inner.__exit__(exc_type, exc, tb)
        finally:
            self.restored = bool(self._inner.restored)
            try:
                if _any_hidden():
                    if self._inner.previous_pid == self._pid and _SETTLE_SEC:
                        # Внутренний страж в этом случае не ждал, а Chrome
                        # активирует окно асинхронно
                        time.sleep(_SETTLE_SEC)
                    if _protect_hidden_focus(self._inner.previous_hwnd):
                        self.restored = True
            except Exception as e:
                logger.debug(f"[BrowserWin] Страж невидимых окон: {e}")
        return False


# ── CDP: вкладки и окна (только оригинальный _raw_call — без рекурсии) ──

def _raw(method: str, params: Optional[dict] = None, pool: str = _POOL_V,
         session_id: Optional[str] = None,
         timeout: Optional[float] = _AUX_TIMEOUT_SEC) -> dict:
    return _ORIG_RAW_CALL(method, params, session_id, pool, None, timeout) or {}


def _page_targets(pool: str = _POOL_V) -> List[dict]:
    infos = _raw("Target.getTargets", pool=pool).get("targetInfos") or []
    return [t for t in infos if t.get("type") == "page" and t.get("targetId")]


def _window_of(target_id: str, pool: str = _POOL_V) -> Tuple[int, dict]:
    # windowId окна вкладки + его границы (DIP)
    r = _raw("Browser.getWindowForTarget", {"targetId": str(target_id)}, pool=pool)
    return int(r["windowId"]), dict(r.get("bounds") or {})


def _windows_of(targets: List[dict]) -> Dict[str, Tuple[int, dict]]:
    out: Dict[str, Tuple[int, dict]] = {}
    for t in targets:
        try:
            out[str(t["targetId"])] = _window_of(t["targetId"])
        except Exception:
            continue  # DevTools/служебные страницы окна браузера не имеют
    return out


def _close_target(target_id: Optional[str]) -> None:
    if not target_id:
        return
    try:
        _raw("Target.closeTarget", {"targetId": str(target_id)})
    except Exception as e:
        logger.debug(f"[BrowserWin] Закрыть вкладку {str(target_id)[:8]}…: {e}")


def _protocol_error(e: BaseException, method: str) -> bool:
    """Chrome ОТВЕТИЛ отказом (неизвестные параметры у старой версии и
    т.п.) — повтор штатными параметрами безопасен. Таймаут/обрыв — нет:
    цель могла уже создаться, повтор дал бы вторую."""
    ba = _BA
    return (isinstance(e, ba.BrowserUnavailable)
            and not isinstance(e, ba.RawCallTimeout)
            and str(e).startswith(f"CDP {method}:"))


def _pool_scale(pid: Optional[int]) -> float:
    """Масштаб (DPI/96) окон Chrome пула — по любому его окну; окон нет —
    1.0. Новое окно Chrome ставит на тот же монитор, что и прежние."""
    for h in (wd.browser_windows(pid) if pid else []):
        return max(0.5, float(wd.window_dpi(h) or 96) / 96.0)
    return 1.0


def _offscreen_dip(pid: Optional[int], w_dip: int, h_dip: int) -> Tuple[int, int]:
    """Угол окна w_dip×h_dip за экраном — в DIP (так считает границы CDP и
    --window-position), из физической точки win_desktop.offscreen_origin.
    Без перевода физические координаты, поданные как DIP, при масштабе 300%
    уезжали бы втрое дальше — к пределу 16-битных координат окна (−32768) и
    признаку свёрнутого окна (−32000)."""
    s = _pool_scale(pid)
    x, y = wd.offscreen_origin(int(w_dip * s), int(h_dip * s))
    return int(x / s), int(y / s)


def _bounds_fit(hwnd: int, b: dict) -> Tuple[bool, bool]:
    """(размер совпал, место совпало): окно CDP (DIP) против HWND
    (физические px; масштаб — DPI окна). Место сверяем и в DIP, и как есть:
    на нескольких мониторах начало координат DIP у Chrome — своё."""
    r = wd.window_rect(hwnd)
    if not r or not b:
        return False, False
    try:
        bl, bt = float(b.get("left") or 0), float(b.get("top") or 0)
        bw, bh = float(b.get("width") or 0), float(b.get("height") or 0)
    except (TypeError, ValueError):
        return False, False
    l, t, rr, bb = r
    s = max(0.5, float(wd.window_dpi(hwnd) or 96) / 96.0)
    size_ok = (abs((rr - l) / s - bw) <= _SIZE_TOL_DIP
               and abs((bb - t) / s - bh) <= _SIZE_TOL_DIP)
    pos_ok = ((abs(l / s - bl) <= _POS_TOL_DIP and abs(t / s - bt) <= _POS_TOL_DIP)
              or (abs(l - bl) <= _POS_TOL_DIP * s and abs(t - bt) <= _POS_TOL_DIP * s))
    return size_ok, pos_ok


def _pick_hwnd(cands: List[int], b: dict) -> Optional[int]:
    # Окно по границам: совпали размер и место → один кандидат; иначе один по размеру
    fits = [(h, *_bounds_fit(h, b)) for h in cands]
    both = [h for h, s, p in fits if s and p]
    if len(both) == 1:
        return both[0]
    sized = [h for h, s, _ in fits if s]
    if len(sized) == 1:
        return sized[0]
    return None


def _resolve_window_ids(pid: int) -> None:
    """windowId CDP для окон реестра пула V, у которых его нет (окно,
    спрятанное при запуске; окно, найденное подметальщиком). Единственная
    пара «неизвестное окно CDP ↔ неизвестный HWND» — сразу; иначе сверка
    границ."""
    recs = _REG.recs(_POOL_V)
    if not any(r.window_id is None for r in recs):
        return
    wins: Dict[int, dict] = {}
    for wid, b in _windows_of(_page_targets()).values():
        wins.setdefault(wid, b)
    known = {r.window_id for r in recs if r.window_id is not None}
    free = {wid: b for wid, b in wins.items() if wid not in known}
    if not free:
        return
    cands = []
    for h in wd.browser_windows(pid):
        rec = _REG.get(_POOL_V, h)
        if rec is None or rec.window_id is None:
            cands.append(h)
    if len(free) == 1 and len(cands) == 1:
        rec = _REG.get(_POOL_V, cands[0])
        if rec is not None:
            rec.window_id = next(iter(free))
        return
    for wid, b in free.items():
        h = _pick_hwnd(cands, b)
        if h is None:
            continue
        cands.remove(h)
        rec = _REG.get(_POOL_V, h)
        if rec is not None and rec.window_id is None:
            rec.window_id = wid


def _hwnd_for_window(pid: int, wid: int, b: dict) -> Optional[int]:
    # HWND окна CDP: реестр → дозаполнение windowId → сверка границ
    for _ in range(2):
        for rec in _REG.recs(_POOL_V):
            if rec.window_id == wid:
                return rec.hwnd
        try:
            _resolve_window_ids(pid)
        except Exception as e:
            logger.debug(f"[BrowserWin] windowId окон пула V: {e}")
    cands = []
    for h in wd.browser_windows(pid):
        rec = _REG.get(_POOL_V, h)
        if rec is None or rec.window_id is None:
            cands.append(h)
    if len(cands) == 1 and len(wd.browser_windows(pid)) == 1:
        return cands[0]
    return _pick_hwnd(cands, b)


def _wait_new_target(before: Set[str], opener: str,
                     budget: float = _NEW_TARGET_WAIT_SEC
                     ) -> Optional[Tuple[str, bool]]:
    """Новая вкладка window.open → (targetId, найдена по openerId?). Проба
    CDP (Chrome 154): openerId у вкладки window.open есть всегда — и с
    noopener, и после window.opener = null, — так что основной путь —
    он. Запасной (к концу ожидания) — единственная новая пустая вкладка:
    её могли открыть и не мы (штатная вкладка пользователя из воркера в ту
    же секунду), поэтому вызывающий чужую по виду вкладку НЕ закрывает."""
    deadline = time.monotonic() + budget
    while True:
        fresh = [t for t in _page_targets() if str(t["targetId"]) not in before]
        hit = [t for t in fresh if t.get("openerId") == opener]
        if hit:
            return str(hit[-1]["targetId"]), True
        if time.monotonic() >= deadline:
            blank = [t for t in fresh if str(t.get("url") or "") in ("", "about:blank")]
            return (str(blank[0]["targetId"]), False) if len(blank) == 1 else None
        time.sleep(0.1)


def _wait_new_hwnds(pid: int, before: Set[int],
                    budget: float = _NEW_HWND_WAIT_SEC) -> List[int]:
    # Окна pid, появившиеся после снимка before (окно новой цели — асинхронно)
    deadline = time.monotonic() + budget
    while True:
        fresh = [h for h in wd.browser_windows(pid) if h not in before]
        if fresh or time.monotonic() >= deadline:
            return fresh
        time.sleep(0.05)


def _wait_new_hwnd_for(pid: int, before: Set[int], b: dict,
                       budget: float = _NEW_HWND_WAIT_SEC
                       ) -> Tuple[Optional[int], List[int]]:
    """HWND нового окна с границами b (CDP, DIP) среди окон, появившихся
    после снимка before → (hwnd | None, новые окна). «Единственное новое
    окно» без сверки границ не берём: в ту же секунду пользователь мог
    нажать Ctrl+N или сайт в его вкладке открыть попап — и окно
    пользователя ушло бы за экран как окно агента. Границы неизвестны
    (getWindowForTarget не ответил) — единственное новое, как раньше.
    Совпал только размер (место по DIP на мониторах разного масштаба может
    не сойтись) — берём, если он единственный и продержался
    _SIZED_GRACE_SEC: окно того же размера, появившееся первым, может
    оказаться окном пользователя, а наше — вот-вот следом."""
    deadline = time.monotonic() + budget
    fresh: List[int] = []
    sized_since: Optional[float] = None
    while True:
        now = time.monotonic()
        fresh = [h for h in wd.browser_windows(pid) if h not in before]
        sized: List[int] = []
        if fresh and not b:
            if len(fresh) == 1:
                return fresh[0], fresh
        elif fresh:
            fits = [(h, *_bounds_fit(h, b)) for h in fresh]
            both = [h for h, s_ok, p_ok in fits if s_ok and p_ok]
            if len(both) == 1:
                return both[0], fresh
            sized = [h for h, s_ok, _p in fits if s_ok]
            if len(sized) == 1:
                if sized_since is None:
                    sized_since = now
                elif now - sized_since >= _SIZED_GRACE_SEC:
                    return sized[0], fresh
            else:
                sized_since = None
        if now >= deadline:
            return (sized[0] if len(sized) == 1 else None), fresh
        time.sleep(0.05)


def _sever_opener(target_id: str) -> None:
    """Связь новой вкладки с открывателем больше не нужна: страница-источник
    (любой сайт в окне агента) не должна управлять новой вкладкой."""
    try:
        sid = str(_raw("Target.attachToTarget",
                       {"targetId": target_id, "flatten": True})["sessionId"])
    except Exception as e:
        logger.debug(f"[BrowserWin] opener не разорван (attach): {e}")
        return
    try:
        _raw("Runtime.evaluate", {"expression": _SEVER_JS, "returnByValue": True},
             session_id=sid, timeout=3.0)
    except Exception as e:
        logger.debug(f"[BrowserWin] opener не разорван: {e}")
    finally:
        try:
            _raw("Target.detachFromTarget", {"sessionId": sid})
        except Exception:
            pass


def _open_in_window(pid: int, wid: int) -> Optional[str]:
    """Новая вкладка В УКАЗАННОМ окне. У Target.createTarget нет параметра
    «окно» (без newWindow вкладка уходит в последнее активное окно), поэтому
    — window.open из вкладки этого окна (временная flat-сессия). → targetId
    или None (окно без вкладок, всплывающее заблокировано, вкладка ушла не
    туда — наша тогда закрыта).

    Проба CDP (Chrome 154, headless): window.open('about:blank','_blank') с
    userGesture:true открывает вкладку в ТОМ ЖЕ окне, что и вкладка-
    источник, — даже если окно за экраном; без userGesture блокировщик
    всплывающих окон возвращает null. С features ('width=…') Chrome
    открывает ОТДЕЛЬНОЕ окно-попап, и на экране — поэтому без них."""
    targets = _page_targets()
    before = {str(t["targetId"]) for t in targets}
    # Источник — обычная страница или пустая вкладка: служебные chrome://,
    # devtools:// и т.п. для window.open ненадёжны
    def _plain(t: dict) -> bool:
        u = str(t.get("url") or "")
        return u in ("", "about:blank") or u.startswith(("http:", "https:"))
    src = None
    for t in sorted(targets, key=lambda t: not _plain(t)):
        try:
            if _window_of(t["targetId"])[0] == wid:
                src = str(t["targetId"])
                break
        except Exception:
            continue
    if src is None:
        return None
    # Страж: window.open может активировать окно (в том числе невидимое) —
    # ввод пользователя не должен уйти в браузер бота
    with _Guard(pid):
        sid = str(_raw("Target.attachToTarget",
                       {"targetId": src, "flatten": True})["sessionId"])
        try:
            r = _raw("Runtime.evaluate",
                     {"expression": _OPEN_JS, "userGesture": True,
                      "returnByValue": True}, session_id=sid)
        finally:
            try:
                _raw("Target.detachFromTarget", {"sessionId": sid})
            except Exception:
                pass
        if not (r.get("result") or {}).get("value"):
            logger.info("[BrowserWin] window.open в окне не сработал")
            return None
        found = _wait_new_target(before, src)
    if found is None:
        logger.info("[BrowserWin] Вкладка window.open не найдена среди целей")
        return None
    new, ours = found
    try:
        got = _window_of(new)[0]
    except Exception:
        got = None
    if got != wid:
        if ours:
            logger.info("[BrowserWin] Вкладка window.open ушла в другое окно — "
                        "закрыта, открываю отдельным окном")
            _close_target(new)
        else:
            # Найдена не по openerId — может быть чужой вкладкой: не трогаем
            logger.info("[BrowserWin] Новая вкладка не в окне назначения и не "
                        "наша по openerId — оставлена, открываю отдельным окном")
        return None
    _sever_opener(new)
    return new


def _create_target(params: dict, fallback: dict, pool: str,
                   timeout: Optional[float]) -> str:
    # createTarget с нашими параметрами; отказ Chrome — штатными
    try:
        return str(_ORIG_RAW_CALL("Target.createTarget", params, None, pool,
                                  None, timeout)["targetId"])
    except Exception as e:
        if params is fallback or not _protocol_error(e, "Target.createTarget"):
            raise
        logger.info(f"[BrowserWin] Chrome отверг границы нового окна ({e}) — "
                    "создаю без них, прячу после")
        return str(_ORIG_RAW_CALL("Target.createTarget", fallback, None, pool,
                                  None, timeout)["targetId"])


def _open_new_window(pid: int, url: str, role: str, hidden: bool,
                     timeout: Optional[float] = None) -> str:
    """Вкладка в НОВОМ окне пула V (role — агента/пользователя). Скрытое —
    сразу за экраном (left/top в createTarget), окно находим разницей HWND
    до/после и прячем ещё и из панели задач. Всё — под стражем фокуса:
    создание окна может увести передний план.

    Проба CDP (Chrome 154, headless): newWindow+background с left/top/
    width/height Chrome соблюдает в точности (-9000, 0, 1280×800). Headed
    Chrome на Windows или сама система могут вернуть окно на монитор,
    поэтому окно всё равно прячется Win32 (hide_offscreen) после создания:
    границы в createTarget — только чтобы окно не мелькнуло."""
    before = set(wd.browser_windows(pid))
    base = {"url": url or "about:blank", "newWindow": True, "background": True}
    params = dict(base)
    if hidden:
        x, y = _offscreen_dip(pid, _AGENT_W, _AGENT_H)
        params.update(left=x, top=y, width=_AGENT_W, height=_AGENT_H)
    with _Guard(pid):
        tid = _create_target(params, base, _POOL_V, timeout)
        # Вкладка уже есть: дальше ничего не роняем — её отдаём в любом случае
        try:
            try:
                wid, b = _window_of(tid)
            except Exception:
                wid, b = None, {}
            hwnd, fresh = _wait_new_hwnd_for(pid, before, b)
            if hwnd is None:
                # Окно появилось позже ожидания (медленная машина): его
                # опознает подметальщик по windowId — иначе окно агента
                # осталось бы на экране «окном пользователя»
                if wid is not None and role == _AGENT:
                    _PENDING_AGENT_WIDS[wid] = time.monotonic()
                    _ensure_sweeper()
                logger.warning(f"[BrowserWin] Окно новой вкладки не найдено "
                               f"(новых окон: {len(fresh)}) — "
                               f"{'ждёт подметальщика' if role == _AGENT else 'не учтено'}")
            else:
                with _SYNC_LOCK:
                    rec = _REG.put(_POOL_V, hwnd, role, window_id=wid)
                    if hidden:
                        _hide_rec(_POOL_V, rec)
                    elif wd.is_offscreen(hwnd):
                        _show_rec(rec)  # Chrome поставил его рядом со спрятанным
        except Exception as e:
            _note_fail("учёт нового окна", e)
    who = "агента" if role == _AGENT else "пользователя"
    logger.info(f"[BrowserWin] Новое окно {who}"
                f"{' (за экраном)' if hidden else ''}: вкладка {tid[:8]}…")
    if hidden:
        _ensure_sweeper()
    return tid


def _create_agent_target(pid: int, url: str = "about:blank",
                         timeout: Optional[float] = None) -> Optional[str]:
    """(сырой CDP, любой поток) Вкладка агента: в существующее окно агента
    (свежее первым), иначе — новое окно агента, видимое ⇔ режим управления.
    → targetId; None — не вышло ни так, ни так (вызывающий — штатно)."""
    hidden = not _BA.control_mode_any()
    with _CREATE_LOCK:
        _REG.bind_pid(_POOL_V, pid)
        for rec in _REG.agents():
            if rec.window_id is None:
                try:
                    _resolve_window_ids(pid)
                except Exception as e:
                    logger.debug(f"[BrowserWin] windowId окна агента: {e}")
            if rec.window_id is None:
                continue
            with _SYNC_LOCK:
                _enforce_agent(rec, hidden)
            try:
                tid = _open_in_window(pid, rec.window_id)
            except Exception as e:
                logger.debug(f"[BrowserWin] Вкладка в окне агента: {e}")
                tid = None
            if tid:
                rec.ts = time.monotonic()
                if hidden:
                    _reassert_hidden()
                logger.info(f"[BrowserWin] Вкладка агента в его окне"
                            f"{' (за экраном)' if hidden else ''}: {tid[:8]}…")
                return tid
        return _open_new_window(pid, url, _AGENT, hidden, timeout)


def _window_unusable(pid: int, wid: int, b: dict, agent_wids: Set[int]) -> bool:
    """Окно не годится для вкладки пользователя: окно агента или окно за
    экраном (в т.ч. спрятанное соседним процессом бота — в нашем реестре
    его может и не быть)."""
    if wid in agent_wids:
        return True
    hwnd = _hwnd_for_window(pid, wid, b)
    return bool(hwnd) and wd.is_offscreen(hwnd)


def _create_user_target(pid: int, timeout: Optional[float] = None) -> Optional[str]:
    """(сырой CDP) Вкладка в окне ПОЛЬЗОВАТЕЛЯ: window.open из вкладки окна,
    которое видно и не принадлежит агенту; такого нет — новое окно на
    экране."""
    with _CREATE_LOCK:
        agent_wids = {r.window_id for r in _REG.agents() if r.window_id is not None}
        targets = _page_targets()
        wins = _windows_of(targets)
        user_wid = None
        bad: Set[int] = set()
        for t in reversed(targets):
            w = wins.get(str(t["targetId"]))
            if not w or w[0] in bad:
                continue
            if _window_unusable(pid, w[0], w[1], agent_wids):
                bad.add(w[0])
                continue
            user_wid = w[0]
            break
        if user_wid is not None:
            try:
                tid = _open_in_window(pid, user_wid)
            except Exception as e:
                logger.debug(f"[BrowserWin] Вкладка в окне пользователя: {e}")
                tid = None
            if tid:
                return tid
        return _open_new_window(pid, "about:blank", _USER, False, timeout)


# ── Патч _raw_call: Target.createTarget ──

def _raw_create_v(params: Optional[dict], timeout: Optional[float]) -> Optional[dict]:
    """Сырая вкладка пула V (фоновые вкладки веб-чата, _raw_open) — всегда
    вкладка агента. None — путь неприменим (штатный вызов)."""
    p = params or {}
    if p.get("newWindow"):
        return None  # явное новое окно штатный код пулу V не заказывает
    pid = _pool_v_pid(allow_connect=True)
    if not pid or not _pool_managed(_POOL_V, pid):
        return None
    tid = _create_agent_target(pid, str(p.get("url") or "about:blank"), timeout)
    return {"targetId": tid} if tid else None


def _raw_create_h(params: Optional[dict], timeout: Optional[float]) -> Optional[dict]:
    """Окно-вкладка пула H в режиме hidden: сразу за экраном (left/top),
    под стражем фокуса, новое окно — вон из панели задач."""
    p = dict(params or {})
    if not p.get("newWindow") or not _pool_h_hidden():
        return None  # headless/headed/rescue — штатно, без единого лишнего вызова
    pid = _pool_h_pid()
    if pid is None:
        # Chrome H ещё не поднят/не подключён: штатный createTarget поднял бы
        # его лениво сам — делаем это дешёвым вызовом заранее, чтобы pid
        # (страж фокуса, разница окон) был известен ДО создания окна
        try:
            _ORIG_RAW_CALL("Browser.getVersion", None, None, _POOL_H, None, timeout)
        except Exception as e:
            logger.debug(f"[BrowserWin] Пул H не поднялся заранее: {e}")
            return None
        pid = _pool_h_pid()
    if not pid or not _pool_h_hidden(pid) or not _pool_managed(_POOL_H, pid):
        return None  # в т.ч. видимый rescue-Chrome, пока режим ещё прежний
    _REG.bind_pid(_POOL_H, pid)
    x, y = _offscreen_dip(pid, _POOL_H_W, _POOL_H_H)
    p.setdefault("left", x)
    p.setdefault("top", y)
    p.setdefault("width", _POOL_H_W)
    p.setdefault("height", _POOL_H_H)
    before = set(wd.browser_windows(pid))
    with _Guard(pid):
        tid = _create_target(p, dict(params or {}), _POOL_H, timeout)
        try:
            _wait_new_hwnds(pid, before, budget=1.0)
            _hide_all_of(_POOL_H, pid, _HIDDEN)
        except Exception as e:
            _note_fail("скрытие окна пула H", e)
    _ensure_sweeper()
    return {"targetId": tid}


def _raw_call_win(method: str, params: Optional[dict] = None,
                  session_id: Optional[str] = None, pool: str = _POOL_V,
                  tab_id: Optional[int] = None, timeout: Optional[float] = None,
                  _retried: bool = False) -> dict:
    """Перехват ТОЛЬКО Target.createTarget (вызов горячий — сначала
    сравнение строки). Повтор оригинала после обрыва (_retried) идёт мимо:
    параметры к нему уже наши."""
    if (method != "Target.createTarget" or _retried or session_id
            or tab_id is not None or not _INSTALLED):
        return _ORIG_RAW_CALL(method, params, session_id, pool, tab_id,
                              timeout, _retried)
    try:
        if pool == _POOL_V:
            res = _raw_create_v(params, timeout)
        elif pool == _POOL_H:
            res = _raw_create_h(params, timeout)
        else:
            res = None
        if res is not None:
            return res
    except Exception as e:
        if isinstance(e, _BA.RawCallTimeout):
            raise  # штатный вызов упал бы так же, а повтор мог бы задвоить вкладку
        _note_fail(f"создание вкладки пула {str(pool).upper()}", e)
    return _ORIG_RAW_CALL(method, params, session_id, pool, tab_id, timeout,
                          _retried)


# ── Метка маршрута: open_new_tab → поток воркера ──

def _in_task_agent() -> bool:
    return getattr(_TLS, "agent_depth", 0) > 0


def _push_hint(url: str, hint: str) -> int:
    token = next(_HINT_SEQ)
    with _HINTS_LOCK:
        _HINTS.setdefault(url, {})[token] = hint
    return token


def _pop_hint(url: str, token: int) -> None:
    with _HINTS_LOCK:
        d = _HINTS.get(url)
        if d is not None:
            d.pop(token, None)
            if not d:
                _HINTS.pop(url, None)


def _hint_for(url: str) -> str:
    """Маршрут вкладки для url. Нет метки или метки разошлись (два открытия
    одного URL разными маршрутами) — «пользователь»: это штатное поведение."""
    with _HINTS_LOCK:
        vals = set((_HINTS.get(url) or {}).values())
    return _AGENT if vals == {_AGENT} else _USER


def _open_new_tab_win(url: str, background: bool = False, pool: str = _POOL_V,
                      focus: bool = False) -> int:
    """Метка маршрута: «агент» — фоновая служебная вкладка (её фолбэк в
    воркер) или шаг задачи агента; иначе «пользователь» (команда режима
    управления). Сама логика открытия — штатная."""
    token: Optional[int] = None
    if _INSTALLED:
        try:
            hint = _AGENT if (background or _in_task_agent()) else _USER
            token = _push_hint(url, hint)
        except Exception as e:
            _note_fail("метка маршрута вкладки", e)
    try:
        return _ORIG["open_new_tab"](url, background=background, pool=pool, focus=focus)
    finally:
        if token is not None:
            _pop_hint(url, token)


def _task_execute_win(self, *args, **kwargs):
    """Шаг задачи агента: всё, что этот поток откроет через open_new_tab, —
    вкладки агента (computer_control зовёт браузер синхронно, в том же
    потоке)."""
    depth = getattr(_TLS, "agent_depth", 0)
    _TLS.agent_depth = depth + 1
    try:
        return _ORIG["TaskAgent._execute"](self, *args, **kwargs)
    finally:
        _TLS.agent_depth = depth


# ── Патчи воркера (поток воркера) ──

class _NoRoute(Exception):
    """Путь Windows неприменим — штатное создание вкладки."""


def _target_of_page(ctx: Any, page: Any) -> Optional[str]:
    s = ctx.new_cdp_session(page)
    try:
        info = (s.send("Target.getTargetInfo") or {}).get("targetInfo") or {}
        return str(info.get("targetId") or "") or None
    finally:
        try:
            s.detach()
        except Exception:
            pass


def _page_for_target(ctx: Any, cand: Any, tid: str) -> Any:
    """Playwright-Page вкладки tid: событие expect_page отдаёт первую новую
    страницу — проверяем, что это наша (рядом мог открыться попап)."""
    try:
        if cand is not None and _target_of_page(ctx, cand) == tid:
            return cand
    except Exception:
        pass
    for p in reversed(list(ctx.pages)):
        if p is cand:
            continue
        try:
            if _target_of_page(ctx, p) == tid:
                return p
        except Exception:
            continue
    raise RuntimeError(f"playwright не увидел вкладку {tid[:8]}…")


def _expect_created(ctx: Any, create: Callable[[], Optional[str]]) -> Any:
    """(поток воркера) Вкладка сырым CDP + её playwright-Page. Ожидание
    события — ДО создания (иначе событие пролетит мимо). None — путь
    неприменим; сбой после создания — вкладка закрывается (штатный путь
    откроет свою, двух не будет)."""
    tid: Optional[str] = None
    try:
        with ctx.expect_page(timeout=_EXPECT_PAGE_MS) as ev:
            tid = create()
            if not tid:
                raise _NoRoute()
        return _page_for_target(ctx, ev.value, tid)
    except _NoRoute:
        return None
    except Exception:
        _close_target(tid)
        raise


def _goto(page: Any, url: str) -> None:
    # Как в штатном _new_page_quiet: недогруз — не ошибка, готовность ждёт снапшот
    try:
        page.goto(url, wait_until="domcontentloaded",
                  timeout=int(float(_BA.NAV_GOTO_TIMEOUT_SEC) * 1000))
    except Exception:
        pass


def _offscreen_windows(pid: int) -> bool:
    # Есть ли у этого Chrome окна за экраном (дёшево: только Win32)
    return any(wd.is_offscreen(h) for h in wd.browser_windows(pid))


def _user_page(w: Any, ctx: Any, pid: int) -> Optional[Tuple[Any, bool]]:
    """Маршрут «пользователь» при живых окнах агента (или любых окнах этого
    Chrome за экраном — их мог спрятать соседний процесс бота): штатное
    фоновое создание (вкладка уходит в последнее активное окно), затем
    проверка окна — попала в окно агента (пользователь кликал в него в
    режиме управления) или в окно за экраном → закрыть и открыть в окне
    пользователя. До навигации: страница не грузится дважды.

    Проба CDP (Chrome 154): куда ляжет createTarget без newWindow, зависит
    от истории — фоновое новое окно «последнее активное» не меняет, окно на
    переднем плане меняет, activateTarget/bringToFront — нет, а после
    закрытия окна вкладки падали в окно за экраном. Поэтому место вкладки
    не предсказываем, а ПРОВЕРЯЕМ getWindowForTarget."""
    try:
        _resolve_window_ids(pid)
    except Exception as e:
        logger.debug(f"[BrowserWin] windowId окон агента: {e}")
    agent_wids = {r.window_id for r in _REG.agents() if r.window_id is not None}
    if not agent_wids and not _offscreen_windows(pid):
        return None  # ни окон агента, ни окон за экраном — штатно
    session = w._browser.new_browser_cdp_session()
    tid: Optional[str] = None
    b: dict = {}
    try:
        with ctx.expect_page(timeout=_USER_EXPECT_MS) as ev:
            tid = str(session.send("Target.createTarget",
                                   {"url": "about:blank", "background": True})["targetId"])
        page = _page_for_target(ctx, ev.value, tid)
        try:
            r = session.send("Browser.getWindowForTarget", {"targetId": tid})
            wid: Optional[int] = int(r["windowId"])
            b = dict(r.get("bounds") or {})
        except Exception:
            wid = None
    except Exception:
        _close_target(tid)
        raise
    finally:
        try:
            session.detach()
        except Exception:
            pass
    try:
        bad = wid is not None and _window_unusable(pid, wid, b, agent_wids)
    except Exception as e:
        # Вкладка уже создана: сбой проверки не должен отдать вызов штатному
        # пути (он открыл бы вторую, а эта осталась бы пустой)
        logger.debug(f"[BrowserWin] Окно вкладки пользователя не проверено: {e}")
        bad = False
    if not bad:
        return page, True  # штатно: фоновая вкладка в окне пользователя
    logger.info("[BrowserWin] Вкладка пользователя попала в окно агента или "
                "за экран — переоткрываю в окне пользователя")
    try:
        page.close()
    except Exception:
        _close_target(tid)
    page2 = _expect_created(ctx, lambda: _create_user_target(pid))
    if page2 is None:
        return None
    return page2, False  # window.open/новое окно — вкладка уже активная


def _new_page_quiet_win(self, ctx, url: str):
    """Вкладка по маршруту: агент → окно агента, пользователь → окно
    пользователя. Контракт штатный: (page, quiet), quiet=True — вкладка
    создана фоновой (её потом тихо активирует _activate_tab_quietly)."""
    if _INSTALLED:
        try:
            pid = _pool_v_pid()
            if pid and _pool_managed(_POOL_V, pid):
                _REG.bind_pid(_POOL_V, pid)
                res = None
                if _hint_for(url) == _AGENT:
                    page = _expect_created(
                        ctx, lambda: _create_agent_target(pid))
                    if page is not None:
                        res = (page, False)
                elif _REG.agents() or _offscreen_windows(pid):
                    res = _user_page(self, ctx, pid)
                if res is not None:
                    _goto(res[0], url)
                    return res
        except Exception as e:
            _note_fail("открытие вкладки по маршруту", e)
    return _ORIG["_new_page_quiet"](self, ctx, url)


def _activate_tab_quietly_win(self, page, url: str):
    """Windows: вкладку — активной в её окне, окно не всплывает. Win32 не
    умеет «выбрать вкладку без активации окна», поэтому: bring_to_front под
    стражем фокуса (перехваченный передний план возвращается прежнему окну,
    мигание кнопки гасится) → спрятанные окна снова за край."""
    if _INSTALLED:
        try:
            pid = _pool_v_pid()
            if pid and _pool_managed(_POOL_V, pid):
                # Проба CDP: в headless bringToFront окно за экраном не
                # двигает; headed Chrome на Windows при этом активирует окно
                # средствами ОС — отсюда страж и повторное скрытие ниже
                with _Guard(pid) as guard:
                    page.bring_to_front()
                for h in wd.browser_windows(pid):
                    wd.stop_flash(h)
                _reassert_hidden()
                try:
                    shown = (page.url or "").strip() or url
                except Exception:
                    shown = url
                logger.info(f"[BrowserWin] Вкладка сделана активной без "
                            f"подъёма окна{' (фокус возвращён)' if guard.restored else ''}"
                            f": {str(shown)[:80]}")
                return None
        except Exception as e:
            _note_fail("тихое переключение вкладки", e)
    return _ORIG["_activate_tab_quietly"](self, page, url)


# ── Явный подъём окна вкладки ──

def _match_targets(targets: List[dict], url: str) -> List[dict]:
    """Как у macOS-версии: точный URL → URL вкладки содержит url → хост
    (сайт редиректил/подменил URL — youtube.com → www.youtube.com/?…)."""
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        host = ""
    tests: List[Callable[[str], bool]] = [
        lambda u: u == url,
        lambda u: url in u,
        lambda u: bool(host) and host in u,
    ]
    for test in tests:
        hits = [t for t in targets if test(str(t.get("url") or ""))]
        if hits:
            return hits
    return []


def _raise_tab(url: str) -> Optional[bool]:
    """→ True/False — результат; None — путь неприменим (штатный)."""
    pid = _pool_v_pid(allow_connect=True)
    if not pid or not _pool_managed(_POOL_V, pid):
        return None
    _REG.bind_pid(_POOL_V, pid)
    hits = _match_targets(_page_targets(), url)
    if not hits:
        return False
    on = bool(_BA.control_mode_any())
    best = None
    # Новые вкладки — правее/позже: с конца; вкладка в окне, которое можно
    # показать, важнее вкладки в спрятанном окне агента
    for t in reversed(hits):
        try:
            wid, b = _window_of(t["targetId"])
        except Exception:
            continue
        hwnd = _hwnd_for_window(pid, wid, b)
        if hwnd is None:
            continue
        rec = _REG.get(_POOL_V, hwnd)
        offscreen = wd.is_offscreen(hwnd)
        agentish = (rec is not None and rec.role == _AGENT) or \
            (rec is None and offscreen)
        # Спрятанное окно соседнего процесса бота — его режим управления,
        # не наш: не показываем никогда
        foreign = offscreen and (rec is None or rec.role != _AGENT) \
            and _foreign(hwnd)
        cand = (t, wid, b, hwnd, rec, foreign or (agentish and not on), foreign)
        if not cand[5]:
            best = cand
            break
        if best is None:
            best = cand
    if best is None:
        _drop_hidden_focus()
        return False
    t, wid, b, hwnd, rec, blocked, foreign = best
    if blocked:
        logger.info("[BrowserWin] Вкладка в " + (
            "окне, спрятанном другим процессом бота" if foreign else
            "скрытом окне агента, режим управления выключен")
            + " — окно не показываю")
        # Штатный вызывающий уже сделал page.bring_to_front() БЕЗ стража
        # (new_page(focus=True), эскалации скриншота) — невидимое окно могло
        # стать активным: клавиатура не должна остаться у него
        _drop_hidden_focus(hwnd)
        _reassert_hidden()
        return False
    with _SYNC_LOCK:
        if rec is None and wd.is_offscreen(hwnd):
            # Спрятано при запуске, но не учтено (метки чужого живого
            # владельца нет — проверено выше) — это окно агента
            rec = _REG.put(_POOL_V, hwnd, _AGENT, window_id=wid, hidden=True)
        if rec is not None and (rec.hidden or wd.is_offscreen(hwnd)):
            _show_rec(rec)
    if str(b.get("windowState") or "") == "minimized":
        # Проба CDP: у свёрнутого окна setWindowBounds с left/top отвечает {}
        # и молча игнорируется — сначала только windowState:"normal" (место
        # вернёт show_onscreen/force_foreground уже средствами Win32)
        try:
            _raw("Browser.setWindowBounds",
                 {"windowId": wid, "bounds": {"windowState": "normal"}})
        except Exception as e:
            logger.debug(f"[BrowserWin] Развернуть окно через CDP: {e}")
    try:
        _raw("Target.activateTarget", {"targetId": t["targetId"]})
    except Exception as e:
        logger.debug(f"[BrowserWin] activateTarget: {e}")
    ok = bool(wd.force_foreground(hwnd))
    logger.info(f"[BrowserWin] Окно вкладки {'поднято' if ok else 'поднять не удалось'}: "
                f"{str(t.get('url') or url)[:80]}")
    return ok


def _focus_browser_tab_win(url: str) -> bool:
    if _INSTALLED and url:
        try:
            res = _raise_tab(url)
            if res is not None:
                return res
        except Exception as e:
            _note_fail("подъём окна вкладки", e)
    return _ORIG["_focus_browser_tab"](url)


# ── Скрытие при запуске и режим управления ──

def _hide_all_of(pool: str, pid: int, role: str, force_role: bool = False) -> int:
    """Спрятать окна pid с ролью role → сколько окон под ролью. Окна
    пользователя не трогаем (кроме force_role — стартовые окна только что
    запущенного Chrome: пользовательских там быть не может)."""
    n = 0
    with _SYNC_LOCK:
        _REG.bind_pid(pool, pid)
        for h in wd.browser_windows(pid):
            rec = _REG.get(pool, h)
            if rec is not None and rec.role == _USER and not force_role:
                continue
            rec = _REG.put(pool, h, role)
            if not rec.hidden or not wd.is_offscreen(h):
                _hide_rec(pool, rec)
            n += 1
    return n


def _pool_of_pid(pid: int) -> Optional[str]:
    if pid == _proc_pid(_BA._WORKER._proc) or pid == _launched_pid(_POOL_V):
        return _POOL_V
    if pid == _proc_pid(_BA._POOL_H_PROC) or pid == _launched_pid(_POOL_H):
        return _POOL_H
    return None


def _launch_hide(pool: str, pid: int) -> None:
    """Скрытие первого окна при запуске: пул V вне режима управления (окно —
    агента), пул H в режиме hidden. Окна может ещё не быть — дожидаемся в
    фоне (поток воркера не держим)."""
    if not _pool_managed(pool, pid):
        return
    role = _AGENT if pool == _POOL_V else _HIDDEN
    n = _hide_all_of(pool, pid, role, force_role=True)
    logger.info(f"[BrowserWin] Пул {pool.upper()}: окно Chrome спрятано за "
                f"экран при запуске ({n})")
    if n == 0:
        threading.Thread(target=_await_launch_windows, args=(pool, pid, role),
                         daemon=True, name="vpc-win-launch").start()
    _ensure_sweeper()
    if pool == _POOL_V and _BA.control_mode_any():
        _schedule_sync()  # режим управления включили посреди запуска


def _await_launch_windows(pool: str, pid: int, role: str) -> None:
    deadline = time.monotonic() + _LAUNCH_WAIT_SEC
    while _INSTALLED and time.monotonic() < deadline:
        time.sleep(0.25)
        try:
            if pool == _POOL_V and _BA.control_mode_any():
                _sync_visibility()
                return
            if _hide_all_of(pool, pid, role, force_role=True):
                if pool == _POOL_V and _BA.control_mode_any():
                    _sync_visibility()  # режим включили, пока прятали
                return
        except Exception as e:
            _note_fail("окна запуска", e)
            return


def _hide_pool_window_win(pid: Optional[int] = None):
    """Windows: окна только что запущенного Chrome пула — за экран и вон из
    панели задач. Чужой pid (не наш запуск) — штатно (на Windows no-op)."""
    if _INSTALLED and pid:
        try:
            pool = _pool_of_pid(int(pid))
            if pool is not None:
                _launch_hide(pool, int(pid))
                return None
        except Exception as e:
            _note_fail("скрытие окна при запуске", e)
    return _ORIG["_hide_pool_window"](pid)


def _sync_once(pid: int, on: bool) -> int:
    """Окна агента пула V — в состояние режима управления → сколько их.
    Неучтённое окно ЗА ЭКРАНОМ — наше (запущено скрытым, пока режим
    включали; спрятано прежним запуском бота) — тоже окно агента, если его
    не пометил другой живой процесс бота (_foreign); неучтённое на экране —
    не наше дело (его разберёт подметальщик)."""
    n = 0
    with _SYNC_LOCK:
        _REG.bind_pid(_POOL_V, pid)
        for h in wd.browser_windows(pid):
            rec = _REG.get(_POOL_V, h)
            if rec is None:
                if not wd.is_offscreen(h) or _foreign(h):
                    continue  # на экране — не наше дело; соседа — его
                rec = _REG.put(_POOL_V, h, _AGENT, hidden=True)
            if rec.role != _AGENT:
                continue
            n += 1
            _enforce_agent(rec, not on)
    return n


def _sync_visibility() -> None:
    """Режим управления включён → окна агента на экран; последний чат вышел
    → окна агента за экран (только их). Состояние читается в момент работы,
    а не вызова: быстрые вкл/выкл подряд сходятся к последнему."""
    on = bool(_BA.control_mode_any())
    pid = _pool_v_pid(allow_connect=on)
    if not pid or not _pool_managed(_POOL_V, pid):
        return
    # Скрытый запуск только что: его окно может появиться чуть позже
    wait_until = time.monotonic() + (_LAUNCH_WAIT_SEC if _in_launch_grace(_POOL_V, pid) else 0.0)
    while True:
        on = bool(_BA.control_mode_any())
        n = _sync_once(pid, on)
        if n or time.monotonic() >= wait_until:
            break
        time.sleep(0.25)
    if n:
        logger.info(f"[BrowserWin] Режим управления {'включён' if on else 'выключен'}"
                    f" — окна агента {'показаны' if on else 'спрятаны'} ({n})")
    if not on:
        _ensure_sweeper()


def _schedule_sync() -> None:
    # В отдельном потоке: set_control_mode зовут из цикла событий бота
    def _run() -> None:
        try:
            _sync_visibility()
        except Exception as e:
            _note_fail("показ/скрытие окон агента", e)
    threading.Thread(target=_run, daemon=True, name="vpc-win-sync").start()


def _set_control_mode_win(chat_id: str, on: bool):
    try:
        was: Optional[bool] = bool(_BA.control_mode_any())
    except Exception:
        was = None
    try:
        return _ORIG["set_control_mode"](chat_id, on)
    finally:
        if _INSTALLED:
            try:
                now = bool(_BA.control_mode_any())
                if was is None or now != was or (on and now):
                    _schedule_sync()
            except Exception as e:
                _note_fail("режим управления", e)


# ── Подметальщик всплывающих окон ──

def _pending_agent_wids() -> Set[int]:
    # windowId окон агента, ждущих опознания подметальщиком: не протухшие и
    # ещё не учтённые в реестре (окно мог опознать и другой путь)
    if not _PENDING_AGENT_WIDS:
        return set()
    now = time.monotonic()
    known = {r.window_id for r in _REG.recs(_POOL_V) if r.window_id is not None}
    for wid, ts in list(_PENDING_AGENT_WIDS.items()):
        if now - ts > _PENDING_TTL_SEC or wid in known:
            _PENDING_AGENT_WIDS.pop(wid, None)
    return set(_PENDING_AGENT_WIDS)


def _sweep_needed() -> bool:
    """Подметальщик нужен, пока есть что держать спрятанным: окна пула H в
    режиме hidden, окна агента вне режима управления (и ещё не опознанные),
    скрытый запуск."""
    if not _INSTALLED:
        return False
    if _in_launch_grace(_POOL_H) or _in_launch_grace(_POOL_V):
        return True
    if _REG.recs(_POOL_H) and _pool_h_hidden(_REG.pid(_POOL_H)):
        return True
    return (not _BA.control_mode_any()) and bool(_REG.agents()
                                                 or _pending_agent_wids())


def _classify_v(pid: int, unknown: List[int]) -> None:
    """Неучтённые окна пула V вне режима управления: окно, вкладка которого
    открыта из окна агента (попап сайта), — окно агента, прячем; остальное —
    окно пользователя (Ctrl+N, DevTools…), не трогаем. CDP — только здесь и
    только при появлении нового окна (вызов отмечает активность пула V).

    Проба CDP (Chrome 154): openerId у вкладки из window.open/target=_blank
    есть всегда, даже с noopener и после window.opener = null; window.open с
    features ('width=…') — отдельное окно-попап, и Chrome ставит его НА экран.
    Такое окно и ловим здесь по openerId. Окно агента, чей HWND при создании
    не нашёлся (_PENDING_AGENT_WIDS), опознаётся по своему windowId."""
    verdict: Dict[int, Tuple[str, Optional[int]]] = {}
    pending = _pending_agent_wids()
    try:
        agent_wids = {r.window_id for r in _REG.agents() if r.window_id is not None}
        if not agent_wids:
            _resolve_window_ids(pid)
            agent_wids = {r.window_id for r in _REG.agents()
                          if r.window_id is not None}
        targets = _page_targets()
        win_of = _windows_of(targets)
        known = {r.window_id for r in _REG.recs(_POOL_V) if r.window_id is not None}
        new_wins: Dict[int, dict] = {}
        for wid, b in win_of.values():
            if wid not in known:
                new_wins.setdefault(wid, b)
        left = list(unknown)
        for wid, b in new_wins.items():
            if len(new_wins) == 1 and len(left) == 1:
                hwnd: Optional[int] = left[0]
            else:
                hwnd = _pick_hwnd(left, b)
            if hwnd is None:
                continue
            left.remove(hwnd)
            openers = [str(t.get("openerId")) for t in targets
                       if t.get("openerId")
                       and win_of.get(str(t["targetId"]), (None,))[0] == wid]
            from_agent = wid in pending or any(
                win_of.get(o, (None,))[0] in agent_wids for o in openers)
            if wid in pending:
                _PENDING_AGENT_WIDS.pop(wid, None)
            verdict[hwnd] = (_AGENT if from_agent else _USER, wid)
    except Exception as e:
        logger.debug(f"[BrowserWin] Разбор нового окна пула V: {e}")
    with _SYNC_LOCK:
        for h in unknown:
            role, wid = verdict.get(h, (_USER, None))
            rec = _REG.put(_POOL_V, h, role, window_id=wid)
            if role == _AGENT and not _BA.control_mode_any():
                _hide_rec(_POOL_V, rec)
                logger.info("[BrowserWin] Окно, открытое из окна агента, "
                            "спрятано за экран")


def _sweep_once() -> None:
    # Невидимое окно бота не должно держать клавиатуру — даже если его
    # активировал не наш вызов (сам Chrome, система)
    _drop_hidden_focus()
    # Пул H в режиме hidden: за экраном — всё (headless/headed/rescue — мимо
    # сразу, без поиска pid; режим — именно этого Chrome, см. _pool_h_hidden)
    hpid = _pool_h_pid() if _pool_h_hidden() else None
    if hpid and _pool_h_hidden(hpid) and _pool_managed(_POOL_H, hpid):
        with _SYNC_LOCK:
            _REG.bind_pid(_POOL_H, hpid)
            for h in wd.browser_windows(hpid):
                rec = _REG.get(_POOL_H, h)
                if rec is None:
                    rec = _REG.put(_POOL_H, h, _HIDDEN)
                    logger.info("[BrowserWin] Пул H: новое окно — за экран")
                if not rec.hidden or not wd.is_offscreen(h):
                    _hide_rec(_POOL_H, rec)
    # Пул V вне режима управления: окна агента — за экраном
    if _BA.control_mode_any():
        return
    vpid = _pool_v_pid()
    if not vpid or not _pool_managed(_POOL_V, vpid):
        return
    unknown: List[int] = []
    with _SYNC_LOCK:
        _REG.bind_pid(_POOL_V, vpid)
        for h in wd.browser_windows(vpid):
            rec = _REG.get(_POOL_V, h)
            if rec is None:
                if wd.is_offscreen(h):
                    if _foreign(h):
                        continue  # окно агента соседнего процесса бота
                    # Спрятанное прежним запуском бота (Chrome пережил его)
                    rec = _REG.put(_POOL_V, h, _AGENT, hidden=True)
                    _hide_rec(_POOL_V, rec)
                else:
                    unknown.append(h)
                continue
            if rec.role == _AGENT and (not rec.hidden or not wd.is_offscreen(h)):
                _hide_rec(_POOL_V, rec)
    if unknown and not _in_launch_grace(_POOL_V, vpid):
        # Окна запуска прячет сам запуск (_hide_pool_window) — их не разбираем
        if _REG.agents() or _pending_agent_wids():
            _classify_v(vpid, unknown)
        else:
            with _SYNC_LOCK:
                for h in unknown:
                    _REG.put(_POOL_V, h, _USER)


def _sweep_loop(gen: int) -> None:
    global _SWEEPER
    while not _SWEEP_STOP.wait(_SWEEP_SEC):
        if gen != _SWEEP_GEN or not _INSTALLED:
            return
        try:
            _sweep_once()
        except Exception as e:
            _note_fail("подметальщик окон", e)
        with _SWEEP_LOCK:
            if gen != _SWEEP_GEN:
                return
            try:
                needed = _sweep_needed()
            except Exception:
                needed = False
            if not needed:
                if _SWEEPER is threading.current_thread():
                    _SWEEPER = None
                return


def _ensure_sweeper() -> None:
    global _SWEEPER
    with _SWEEP_LOCK:
        if not _INSTALLED:
            return
        if _SWEEPER is not None and _SWEEPER.is_alive():
            return
        _SWEEP_STOP.clear()
        _SWEEPER = threading.Thread(target=_sweep_loop, args=(_SWEEP_GEN,),
                                    daemon=True, name="vpc-win-sweep")
        _SWEEPER.start()


# ── Прокси subprocess: Chrome скрытых пулов стартует за экраном ──

def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(
        os.path.expandvars(str(p))))).rstrip("\\/")


def _pool_of_profile(udd: str) -> Optional[str]:
    try:
        n = _norm_path(udd)
        if n == _norm_path(_BA._pool_v_profile()):
            return _POOL_V
        if n == _norm_path(_BA._pool_h_profile()):
            return _POOL_H
    except Exception as e:
        logger.debug(f"[BrowserWin] Профиль запуска не сверен: {e}")
    return None


def _window_size(cmd: List[str]) -> Optional[Tuple[int, int]]:
    for a in cmd:
        if a.startswith("--window-size="):
            try:
                w, h = a.split("=", 1)[1].split(",", 1)
                return int(w), int(h)
            except ValueError:
                return None
    return None


def _launch_args(args: Any) -> Tuple[Any, Optional[Tuple[str, bool]]]:
    """Команда запуска Chrome НАШЕГО пула → (команда, (пул, скрытый?));
    чужая команда — (как есть, None). Пулу H в режиме hidden добавляется
    --window-position за экраном (перед завершающим URL).

    Пулу V — никогда: Chrome читает --window-position/--window-size из своей
    командной строки для КАЖДОГО нового окна (GetSavedWindowBoundsAndShowState
    → UpdateWindowBoundsAndShowStateFromCommandLine, поверх своей проверки
    «окно видно на мониторе»). Пул V, поднятый вне режима управления, потом
    рождал бы за экраном и окна пользователя (Ctrl+N, OAuth/3-D Secure
    попапы сайтов) — с фокусом клавиатуры, но невидимые. Его первое окно
    прячет опрос сразу после запуска (_early_launch_hide)."""
    if not _INSTALLED or not isinstance(args, (list, tuple)) or not args:
        return args, None
    cmd = [str(a) for a in args]
    if not any(a.startswith("--remote-debugging-port=") for a in cmd):
        return args, None
    udd = next((a.split("=", 1)[1] for a in cmd if a.startswith("--user-data-dir=")), None)
    if not udd:
        return args, None
    pool = _pool_of_profile(udd)
    if pool is None:
        return args, None
    if pool == _POOL_H:
        if any(a.startswith("--headless") for a in cmd):
            return args, (pool, False)
        hidden = _BA._pool_h_desired_mode() == "hidden"
    else:
        # Флаг пулу V не даём (см. выше) — только учёт запуска
        return args, (pool, not _BA.control_mode_any())
    if not hidden:
        return args, (pool, False)
    if any(a.startswith("--window-position=") for a in cmd):
        return args, (pool, True)
    # --window-position — в DIP; окон ещё нет, масштаб неизвестен (1.0):
    # при масштабе >100% окно встанет ещё дальше за край, а точное место
    # всё равно выставит hide_offscreen (Win32) при _hide_pool_window
    w, h = _window_size(cmd) or (_POOL_H_W, _POOL_H_H)
    x, y = _offscreen_dip(None, w, h)
    flag = f"--window-position={int(x)},{int(y)}"
    out = list(args)
    if not cmd[-1].startswith("-"):
        out.insert(len(out) - 1, flag)  # перед завершающим about:blank
    else:
        out.append(flag)
    logger.info(f"[BrowserWin] Пул {pool.upper()}: Chrome стартует за экраном ({flag})")
    return out, (pool, True)


def _early_launch_hide(pool: str, proc: Any,
                       budget: float = _EARLY_HIDE_SEC) -> None:
    """Первое окно скрытого запуска — за экран, как только оно показалось,
    не дожидаясь CDP (штатный _hide_pool_window зовётся только после
    подключения — секунда-другая окна на экране и в панели задач). Пулу V
    это заменяет --window-position (см. _launch_args), пулу H — убирает
    кнопку панели задач раньше. Режим управления включили посреди запуска —
    окно пула V не прячем (а уже спрятанное покажет _sync_visibility)."""
    role = _AGENT if pool == _POOL_V else _HIDDEN
    deadline = time.monotonic() + budget
    while _INSTALLED and time.monotonic() < deadline:
        pid = _proc_pid(proc)
        if pid is None:
            return  # Chrome вышел (профиль занят — окно ушло в чужой Chrome)
        try:
            if pool == _POOL_V and _BA.control_mode_any():
                return
            if wd.browser_windows(pid):
                if not _pool_managed(pool, pid):
                    return
                n = _hide_all_of(pool, pid, role, force_role=True)
                logger.info(f"[BrowserWin] Пул {pool.upper()}: окно запуска "
                            f"спрятано сразу ({n})")
                if pool == _POOL_V and _BA.control_mode_any():
                    # Режим включили, пока прятали: его переход мог пройти
                    # до регистрации окна — показываем сами
                    _sync_visibility()
                _ensure_sweeper()
                return
        except Exception as e:
            _note_fail("окно запуска (опрос)", e)
            return
        time.sleep(_EARLY_POLL_SEC)


class _HidingPopen(_real_subprocess.Popen):
    """Popen для browser_actions: учёт запусков Chrome наших пулов; Chrome
    пула H hidden рождается сразу за экраном, первое окно скрытого запуска
    любого пула прячется опросом сразу после старта (_early_launch_hide).
    Подкласс настоящего Popen: isinstance и весь интерфейс процесса —
    прежние."""

    def __init__(self, args, *a, **kw):
        launch = None
        try:
            args, launch = _launch_args(args)
        except Exception as e:
            _note_fail("флаги запуска Chrome", e)
        super().__init__(args, *a, **kw)
        if launch is not None:
            _LAUNCHED[launch[0]] = (self, bool(launch[1]), time.monotonic())
            if launch[1]:
                try:
                    threading.Thread(target=_early_launch_hide,
                                     args=(launch[0], self), daemon=True,
                                     name="vpc-win-launch").start()
                except Exception as e:
                    _note_fail("опрос окна запуска", e)
                _ensure_sweeper()


class _SubprocessProxy:
    """Замена атрибута browser_actions.subprocess: всё, кроме Popen, —
    настоящий модуль (чтение, запись и удаление атрибутов уходят в него —
    как если бы прокси не было)."""

    def __init__(self, real: Any):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "Popen", _HidingPopen)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_real"), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(object.__getattribute__(self, "_real"), name)

    def __repr__(self) -> str:
        return f"<win-proxy {object.__getattribute__(self, '_real')!r}>"


# ── install / uninstall ──

# Имя → ожидаемые параметры (по порядку). Не совпало — не ставим НИЧЕГО:
# патч поверх изменённой функции молча сломал бы её контракт
_MODULE_PATCHES: Dict[str, Tuple[Tuple[str, ...], Callable]] = {
    "_raw_call": (("method", "params", "session_id", "pool", "tab_id",
                   "timeout", "_retried"), _raw_call_win),
    "open_new_tab": (("url", "background", "pool", "focus"), _open_new_tab_win),
    "set_control_mode": (("chat_id", "on"), _set_control_mode_win),
    "_hide_pool_window": (("pid",), _hide_pool_window_win),
    "_focus_browser_tab": (("url",), _focus_browser_tab_win),
}
_WORKER_PATCHES: Dict[str, Tuple[Tuple[str, ...], Callable]] = {
    "_new_page_quiet": (("self", "ctx", "url"), _new_page_quiet_win),
    "_activate_tab_quietly": (("self", "page", "url"), _activate_tab_quietly_win),
}
_TASK_PARAMS = ("self", "run", "chat_id", "router", "a", "line")
# Внутренности browser_actions, на которые опирается путь Windows
_REQUIRED = ("_WORKER", "_CdpWorker", "_POOL_H", "_POOL_V", "_POOL_H_PROC",
             "_RAW_CLIENTS", "_POOL_H_RUNNING_MODE", "control_mode_any",
             "_pool_h_profile", "_pool_v_profile", "_pool_h_desired_mode",
             "_is_default_browser_profile", "_pid_alive", "_browser_pid",
             "NAV_GOTO_TIMEOUT_SEC", "BrowserUnavailable", "RawCallTimeout",
             "subprocess")


def _params(fn: Any) -> Tuple[str, ...]:
    return tuple(inspect.signature(fn).parameters)


def _validate(ba: Any, ta: Any) -> List[str]:
    problems: List[str] = []
    for name in _REQUIRED:
        if not hasattr(ba, name):
            problems.append(f"нет browser_actions.{name}")
    if problems:
        return problems
    if (ba._POOL_H, ba._POOL_V) != (_POOL_H, _POOL_V):
        problems.append("имена пулов не «h»/«v»")
    if not hasattr(ba._WORKER, "_proc") or not hasattr(ba._WORKER, "_browser"):
        problems.append("у _WORKER нет _proc/_browser")
    if getattr(ba.subprocess, "Popen", None) is not _real_subprocess.Popen:
        problems.append("browser_actions.subprocess — не модуль subprocess")
    for name, (want, _w) in _MODULE_PATCHES.items():
        fn = getattr(ba, name, None)
        if not callable(fn):
            problems.append(f"нет browser_actions.{name}")
        elif _params(fn) != want:
            problems.append(f"browser_actions.{name}{_params(fn)} ≠ {want}")
    for name in ("open_new_tab", "_raw_call"):
        fn = getattr(ba, name, None)
        if callable(fn):
            default = inspect.signature(fn).parameters.get("pool")
            if default is None or default.default != _POOL_V:
                problems.append(f"browser_actions.{name}: pool по умолчанию не «v»")
    for name, (want, _w) in _WORKER_PATCHES.items():
        fn = ba._CdpWorker.__dict__.get(name)
        if not callable(fn):
            problems.append(f"нет _CdpWorker.{name}")
        elif _params(fn) != want:
            problems.append(f"_CdpWorker.{name}{_params(fn)} ≠ {want}")
    task_cls = getattr(ta, "TaskAgent", None)
    fn = task_cls.__dict__.get("_execute") if task_cls is not None else None
    if not callable(fn):
        problems.append("нет TaskAgent._execute")
    elif _params(fn) != _TASK_PARAMS:
        problems.append(f"TaskAgent._execute{_params(fn)} ≠ {_TASK_PARAMS}")
    return problems


def _wrap(orig: Callable, wrapper: Callable) -> Callable:
    """Обёртка с именем/докстрингом/сигнатурой оригинала (__wrapped__):
    inspect и трассировки видят знакомую функцию."""
    @functools.wraps(orig)
    def patched(*args, **kwargs):
        return wrapper(*args, **kwargs)
    patched.__vpc_win__ = True  # type: ignore[attr-defined]
    return patched


def _apply(ba: Any, ta: Any) -> None:
    global _ORIG_RAW_CALL
    _ORIG.clear()
    _ALIASES.clear()
    originals: Dict[int, Tuple[str, Callable]] = {}
    for name, (_want, wrapper) in _MODULE_PATCHES.items():
        orig = getattr(ba, name)
        _ORIG[name] = orig
        if name == "_raw_call":
            # Горячий путь — без лишнего слоя: обёртка сама сверяет метод
            _ORIG_RAW_CALL = orig
            new = wrapper
        else:
            new = _wrap(orig, wrapper)
        setattr(ba, name, new)
        originals[id(orig)] = (name, new)
    for name, (_want, wrapper) in _WORKER_PATCHES.items():
        orig = ba._CdpWorker.__dict__[name]
        _ORIG[name] = orig
        setattr(ba._CdpWorker, name, _wrap(orig, wrapper))
    orig_exec = ta.TaskAgent.__dict__["_execute"]
    _ORIG["TaskAgent._execute"] = orig_exec
    ta.TaskAgent._execute = _wrap(orig_exec, _task_execute_win)
    _ORIG["subprocess"] = ba.subprocess
    ba.subprocess = _SubprocessProxy(ba.subprocess)
    # Копии патчимых функций в других модулях app.* («from
    # app.features.browser_actions import _raw_call» держит СВОЮ ссылку на
    # оригинал). Сейчас в app/ таких импортов нет — все зовут через атрибут
    # модуля (_ba.open_new_tab, ba.set_control_mode); проверка — на будущее
    me = sys.modules.get(__name__)
    for mname, mod in list(sys.modules.items()):
        # Свой модуль — мимо: _ORIG_RAW_CALL здесь оригинал НАМЕРЕННО
        if (mod is None or mod is ba or mod is me
                or not mname.startswith("app.")):
            continue
        try:
            d = vars(mod)
        except TypeError:
            continue
        for attr, val in list(d.items()):
            hit = originals.get(id(val))
            if hit is not None and val is _ORIG[hit[0]]:
                setattr(mod, attr, hit[1])
                _ALIASES.append((mod, attr, val))
                logger.info(f"[BrowserWin] Пропатчена копия {mname}.{attr}")


def install(force: bool = False) -> bool:
    """Поставить Windows-логику окон. Только win32 (force=True — тесты на
    любой ОС с фейковым win_desktop); повторный вызов — no-op;
    VPC_WIN_WINDOWS=0 — ничего не ставит. Любая точка патча не совпала с
    ожидаемой — НИЧЕГО не ставится (warning), поведение штатное.
    → True — патчи стоят."""
    global _INSTALLED, _BA
    with _STATE_LOCK:
        if _INSTALLED:
            return True
        if os.environ.get(ENV_SWITCH, "").strip() == "0":
            logger.info(f"[BrowserWin] {ENV_SWITCH}=0 — Windows-логика окон "
                        "Chrome бота отключена")
            return False
        if sys.platform != "win32" and not force:
            return False
        if not force and not wd.is_supported():
            logger.warning("[BrowserWin] Win32 API недоступен — окна Chrome "
                           "бота остаются штатными")
            return False
        try:
            from app.features import browser_actions as ba
            from app.features import task_agent as ta
        except Exception as e:
            logger.warning(f"[BrowserWin] Модули бота не импортировались ({e}) "
                           "— патчи не установлены")
            return False
        problems = _validate(ba, ta)
        if problems:
            logger.warning("[BrowserWin] Точки патча не совпали с ожидаемыми: "
                           + "; ".join(problems)
                           + " — патчи НЕ установлены, поведение штатное")
            return False
        _BA = ba
        _REG.reset()
        _HINTS.clear()
        _LAUNCHED.clear()
        _MANAGED_CACHE.clear()
        _PID_CACHE.clear()
        _PENDING_AGENT_WIDS.clear()
        _FAILS.clear()
        _apply(ba, ta)
        _INSTALLED = True
    logger.info("[BrowserWin] Windows-логика окон Chrome бота установлена "
                "(окна агента — только в режиме управления, пул H hidden — "
                "за экраном)")
    return True


def uninstall() -> None:
    """Снять все патчи (только те, что ещё наши: поверх мог встать чужой
    патч — его не затираем) и остановить подметальщика. Окна не трогаем:
    в работе бота uninstall не зовётся, это для тестов."""
    global _INSTALLED, _SWEEPER, _SWEEP_GEN
    with _STATE_LOCK:
        if not _INSTALLED:
            return
        _INSTALLED = False
        _SWEEP_STOP.set()
        with _SWEEP_LOCK:
            _SWEEP_GEN += 1
            _SWEEPER = None
        ba = _BA
        for name in _MODULE_PATCHES:
            cur = getattr(ba, name, None)
            if cur is _raw_call_win or getattr(cur, "__vpc_win__", False):
                setattr(ba, name, _ORIG[name])
        for name in _WORKER_PATCHES:
            cur = ba._CdpWorker.__dict__.get(name)
            if getattr(cur, "__vpc_win__", False):
                setattr(ba._CdpWorker, name, _ORIG[name])
        try:
            from app.features import task_agent as ta
            cur = ta.TaskAgent.__dict__.get("_execute")
            if getattr(cur, "__vpc_win__", False):
                ta.TaskAgent._execute = _ORIG["TaskAgent._execute"]
        except Exception as e:
            logger.debug(f"[BrowserWin] TaskAgent._execute не восстановлен: {e}")
        if isinstance(ba.subprocess, _SubprocessProxy):
            ba.subprocess = _ORIG["subprocess"]
        for mod, attr, val in _ALIASES:
            cur = vars(mod).get(attr)
            if cur is _raw_call_win or getattr(cur, "__vpc_win__", False):
                setattr(mod, attr, val)
        # _ORIG/_ORIG_RAW_CALL не чистим: вызов, начатый до снятия, и
        # ссылка на обёртку, сохранённая кем-то, уходят в оригинал
        # (обёртки без _INSTALLED — сквозные)
        _ALIASES.clear()
        _REG.reset()
        _HINTS.clear()
        _LAUNCHED.clear()
        _MANAGED_CACHE.clear()
        _PID_CACHE.clear()
        _PENDING_AGENT_WIDS.clear()
    logger.info("[BrowserWin] Windows-логика окон снята")


def is_installed() -> bool:
    return _INSTALLED

"""Окна бота на Windows: тонкий слой Win32 через ctypes (без сторонних пакетов).

Зачем отдельный модуль. На macOS окна пулов Chrome прячутся AppleScript'ом
(`_hide_pool_window` и соседи в browser_actions), на Windows те же функции —
no-op: окно агента и окна hidden-пула H висят на экране и в панели задач.
Логику для Windows держим ОТДЕЛЬНО, не трогая основной код: здесь — только
примитивы Win32, а решения «какое окно прятать/показывать» — в browser_win.

«Спрятать» на Windows = увести окно ЗА ПРЕДЕЛЫ виртуального экрана без
активации + убрать его из панели задач и Alt+Tab. SW_HIDE (или сворачивание)
как постоянное состояние не годится: такое окно Chrome считает невидимым и
перестаёт рисовать страницу (document.hidden, троттлинг таймеров), а агент и
веб-чаты работают именно с живой отрисовкой. Окно «за краем» Chrome рисует
дальше: --disable-backgrounding-occluded-windows (уже в
CHROME_NO_THROTTLE_FLAGS) отключает его собственную проверку перекрытия.

Панель задач и Alt+Tab — через WS_EX_TOOLWINDOW (и снятие WS_EX_APPWINDOW),
а НЕ через ITaskbarList::DeleteTab:
  * DeleteTab убирает только кнопку — Alt+Tab и Win+Tab окно показывать
    продолжают; стиль убирает из всех трёх разом;
  * кнопку, удалённую DeleteTab, Explorer возвращает при любом показе или
    активации окна и после перезапуска explorer.exe — пришлось бы следить;
    стиль — свойство самого окна, он всё это переживает;
  * COM потребовал бы CoInitialize в каждом вызывающем потоке (воркер
    Playwright, подметальщик browser_win) и ручную таблицу vtable в ctypes.
Минус стиля: панель задач перечитывает его только при ПОКАЗЕ окна, поэтому
после смены стиля делаем цикл SW_HIDE → SW_SHOWNA — и только когда окно уже
за краем (мигания никто не видит, SW_SHOWNA не активирует). Побочный плюс:
SW_HIDE снимает активность с окна, если оно было активным, — ввод с
клавиатуры не уходит в невидимое окно. Цена — пара событий visibilitychange
на странице в момент первого скрытия.

Спрятанное окно ещё и НЕАКТИВИРУЕМО: WS_EX_NOACTIVATE + дно z-порядка.
TOOLWINDOW убирает окно только из панели задач и Alt+Tab, а видимое и
доступное окно система сама делает активным, когда пользователь закрывает
или сворачивает своё активное окно (берёт следующее по z-порядку, а SW_SHOWNA
цикла выше как раз ставит окно наверх). Пользователь закрыл Блокнот — и его
следующие нажатия (и Enter) ушли бы в невидимое поле ввода веб-чата.
NOACTIVATE система при таком выборе пропускает (MSDN), программная
активация (SetForegroundWindow — сам Chrome, force_foreground) работает
по-прежнему. Показ возвращает исходные биты стиля.

Координаты — ФИЗИЧЕСКИЕ пиксели: вызовы геометрии идут под per-monitor
DPI-контекстом потока (_dpi_scope). python.exe сам DPI-unaware, и без этого
GetWindowRect окна Chrome (per-monitor-v2) вернул бы «виртуализированные»
координаты, а SetWindowPos промахивался бы на мониторах с масштабом ≠ 100%.

Импортируется на любой ОС: windll трогается только при первом обращении к
бэкенду на win32. Вне Windows (без подставленного фейка) каждая функция
возвращает безопасное значение по умолчанию.

Бэкенд подменяемый (set_api): тесты подставляют фейк с теми же именами
методов, что у _Win32Api.
  Обязательные:
    enum_windows() -> [hwnd]                      (верхний уровень, z-порядок)
    get_window_thread_process_id(h) -> (tid, pid)
    get_class_name(h) -> str
    get_owner(h) -> hwnd | 0                      (GetWindow GW_OWNER)
    is_window(h), is_window_visible(h), is_iconic(h), is_zoomed(h) -> bool
    get_window_rect(h) -> (l, t, r, b) | None
    get_ex_style(h) -> int; set_ex_style(h, value) -> bool
    set_window_pos(h, insert_after, x, y, cx, cy, flags) -> bool
    show_window(h, cmd) -> bool                   (как ShowWindow: «было видно»)
    get_virtual_screen() -> (left, top, width, height)
    get_foreground_window() -> hwnd | 0
    set_foreground_window(h) -> bool; bring_window_to_top(h) -> bool
    get_current_thread_id() -> int
    attach_thread_input(tid_from, tid_to, attach) -> bool
    flash_window_stop(h) -> bool
  Необязательные (нет метода или исключение — запасное поведение):
    enum_monitors() -> [(l, t, r, b)]
    get_work_area() -> (l, t, r, b) | None        (основной монитор)
    get_monitor_rects(h) -> ((монитор l,t,r,b), (рабочая область l,t,r,b)) | None
    get_window_placement(h) -> (show_cmd, (l, t, r, b) в координатах
                                рабочей области) | None
    get_dpi_for_window(h) -> int                  (0 = неизвестно)
    set_thread_dpi_awareness_context(ctx) -> int  (прежний контекст, 0 = не вышло)
    ensure_message_queue() -> None
    is_hung_app_window(h) -> bool
    is_responsive(h, timeout_ms) -> bool          (SendMessageTimeout WM_NULL)
    get_prop(h) -> int; set_prop(h, value) -> bool  (метка владельца окна)
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)

Rect = Tuple[int, int, int, int]

# ── Константы Win32 ──
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
# Биты стиля, которые меняет скрытие (и только их возвращает показ)
MANAGED_EXSTYLE = WS_EX_TOOLWINDOW | WS_EX_APPWINDOW | WS_EX_NOACTIVATE
GW_OWNER = 4

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOWNA = 8
SW_RESTORE = 9

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
HWND_TOP = 0
HWND_BOTTOM = 1

FLASHW_STOP = 0
PM_NOREMOVE = 0x0000
WM_NULL = 0x0000
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002
SPI_GETWORKAREA = 0x0030
MONITOR_DEFAULTTONEAREST = 2
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

# Псевдо-хэндлы DPI_AWARENESS_CONTEXT (winuser.h)
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = -3
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

# Рамки браузера у Chrome/Edge/прочих Chromium — этот класс окна
CHROME_WINDOW_CLASS_PREFIX = "Chrome_WidgetWin_"

# ── Геометрия ──
# Зазор между правым краем спрятанного окна и левым краем виртуального
# экрана: тень/невидимые рамки DWM и анимации не должны «подглядывать»
OFFSCREEN_GAP_PX = 2000
# Потолок модуля координат. Координаты в WM_MOVE/WM_WINDOWPOSCHANGED
# упакованы в 16 бит со знаком, а -32000 — признак свёрнутого окна:
# Windows и Chrome приняли бы окно на -32000 за свёрнутое
OFFSCREEN_LIMIT_PX = 30000
DEFAULT_WINDOW_W_DIP = 1280
DEFAULT_WINDOW_H_DIP = 900
DEFAULT_WORKAREA_FRACTION = 0.85
# Меньше — не рамка браузера (скрытые служебные окна Chrome нулевого размера)
FRAME_MIN_W_PX = 100
FRAME_MIN_H_PX = 60
# «Окно на экране» — видна полоса, за которую можно ухватить мышью
VISIBLE_MIN_W_PX = 100
VISIBLE_MIN_H_PX = 40
SIZE_TOLERANCE_PX = 2
FOCUS_SETTLE_SEC = 0.25
# Пинг чужого окна перед AttachThreadInput: дольше — поток занят/завис
RESPONSIVE_TIMEOUT_MS = 200
# Свойство окна (SetPropW) с pid процесса бота, который это окно спрятал
# или завёл как своё: Chrome пула V общий для процессов бота, а режим
# управления у каждого процесса свой
OWNER_PROP = "VPC.BotOwnerPid"
_FALLBACK_SCREEN: Tuple[int, int, int, int] = (0, 0, 0, 0)

# ── Состояние модуля ──
_API_LOCK = threading.Lock()
_INJECTED_API: Any = None
_REAL_API: Any = None
_REAL_API_TRIED = False

# Скрытие/показ одного окна из разных потоков (воркер Playwright,
# подметальщик, потоки бота) не должны переплетаться: сохранённый
# прямоугольник и стиль меняются парой
_OP_LOCK = threading.RLock()
# hwnd -> (pid, прямоугольник на экране до скрытия). pid рядом — защита от
# переиспользования HWND: закрытое окно отдаёт номер новому, и чужой
# прямоугольник/стиль без сверки применился бы не к тому окну
_SAVED_RECTS: Dict[int, Tuple[int, Rect]] = {}
# hwnd -> (pid, расширенный стиль до скрытия)
_ORIG_EXSTYLE: Dict[int, Tuple[int, int]] = {}
_PRUNE_THRESHOLD = 64
_WARNED: Set[str] = set()


# ── Бэкенд ──

def set_api(api: Any) -> None:
    """Подменить бэкенд (тесты — фейком); None — вернуть настоящий.
    Сохранённые прямоугольники/стили принадлежат прежнему бэкенду: их
    HWND в новом ничего не значат — сбрасываем."""
    global _INJECTED_API
    with _API_LOCK:
        _INJECTED_API = api
    forget()


def get_api() -> Any:
    """Текущий бэкенд: подставленный, иначе настоящий (только win32,
    создаётся лениво при первом обращении), иначе None."""
    global _REAL_API, _REAL_API_TRIED
    injected = _INJECTED_API
    if injected is not None:
        return injected
    if sys.platform != "win32":
        return None
    with _API_LOCK:
        if not _REAL_API_TRIED:
            _REAL_API_TRIED = True
            try:
                _REAL_API = _Win32Api()
            except Exception as e:
                logger.warning(f"[WinDesktop] Win32 API недоступен — окна бота "
                               f"не прячем: {e}")
                _REAL_API = None
        return _REAL_API


def is_supported() -> bool:
    return get_api() is not None


def _fail(where: str, e: BaseException) -> None:
    """Сбой Win32-пути: первый раз на функцию — warning (видно в логе, что
    логика окон не работает), дальше — debug, без спама на каждом вызове."""
    if where in _WARNED:
        logger.debug(f"[WinDesktop] {where}: {e}")
    else:
        _WARNED.add(where)
        logger.warning(f"[WinDesktop] {where} не удалось: {e}")


def _opt(api: Any, name: str, default: Any, *args: Any) -> Any:
    """Вызов НЕОБЯЗАТЕЛЬНОГО метода бэкенда: старая Windows (нет
    GetDpiForWindow/SetThreadDpiAwarenessContext) или урезанный фейк не
    должны ронять всю операцию — берётся запасное поведение."""
    fn = getattr(api, name, None)
    if fn is None:
        return default
    try:
        res = fn(*args)
    except Exception as e:
        logger.debug(f"[WinDesktop] {name}: {e}")
        return default
    return default if res is None else res


@contextmanager
def _dpi_scope(api: Any) -> Iterator[None]:
    """Per-monitor DPI-контекст ТОЛЬКО для текущего потока и только на время
    блока: процесс (и чужой код в этом потоке — Playwright, ctypes в
    browser_actions) остаётся в своём режиме. V2 — с Windows 10 1703,
    иначе V1 (1607); старше — без контекста (там и Chrome не per-monitor)."""
    prev = 0
    for ctx in (DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
                DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE):
        prev = _opt(api, "set_thread_dpi_awareness_context", 0, ctx)
        if prev:
            break
    try:
        yield
    finally:
        if prev:
            _opt(api, "set_thread_dpi_awareness_context", 0, prev)


# ── Геометрия (чистые помощники) ──

def _as_rect(value: Any) -> Optional[Rect]:
    try:
        l, t, r, b = (int(v) for v in value)
    except Exception:
        return None
    return (l, t, r, b)


def _size(rect: Optional[Rect]) -> Tuple[int, int]:
    if not rect:
        return (0, 0)
    return (rect[2] - rect[0], rect[3] - rect[1])


def _overlap(a: Rect, b: Rect) -> Tuple[int, int]:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return (max(0, w), max(0, h))


def _virtual(api: Any) -> Tuple[int, int, int, int]:
    vs = api.get_virtual_screen()
    try:
        l, t, w, h = (int(v) for v in vs)
    except Exception:
        return _FALLBACK_SCREEN
    return (l, t, w, h)


def _screen_rects(api: Any) -> List[Rect]:
    """Прямоугольники мониторов. Список мониторов точнее общей рамки
    виртуального экрана: при мониторах разной высоты в ней есть «дыры»,
    где окно формально внутри, но его никто не видит."""
    mons = _opt(api, "enum_monitors", None)
    if mons:
        rects = [r for r in (_as_rect(m) for m in mons) if r]
        if rects:
            return rects
    l, t, w, h = _virtual(api)
    return [(l, t, l + w, t + h)] if w > 0 and h > 0 else []


def _rect_visible(api: Any, rect: Optional[Rect]) -> bool:
    """На каком-то мониторе видна полоса окна, за которую можно ухватить."""
    if not rect:
        return False
    w, h = _size(rect)
    if w <= 0 or h <= 0:
        return False
    need_w, need_h = min(VISIBLE_MIN_W_PX, w), min(VISIBLE_MIN_H_PX, h)
    for mon in _screen_rects(api):
        ow, oh = _overlap(rect, mon)
        if ow >= need_w and oh >= need_h:
            return True
    return False


def _rect_offscreen(api: Any, rect: Optional[Rect]) -> bool:
    """Ни один пиксель окна не попадает ни на один монитор. Экран неизвестен
    (бэкенд ничего не вернул) — «за краем» не утверждаем."""
    if not rect:
        return False
    screens = _screen_rects(api)
    if not screens:
        return False
    for mon in screens:
        ow, oh = _overlap(rect, mon)
        if ow > 0 and oh > 0:
            return False
    return True


def _offscreen_origin_for(vs: Tuple[int, int, int, int], width: int,
                          height: int) -> Tuple[int, int]:
    """Левый верхний угол окна width×height целиком ЛЕВЕЕ виртуального
    экрана. Левый край у виртуального экрана всегда ≤ 0 (основной монитор
    начинается в 0,0), так что x отрицательный. Если левее не помещается в
    потолок ±OFFSCREEN_LIMIT_PX (огромная стена мониторов при 300%) — правее
    экрана; совсем никак — сам потолок (не -32000: признак свёрнутого)."""
    vl, vt, vw, vh = vs
    w = int(width) if width and width > 0 else DEFAULT_WINDOW_W_DIP
    h = int(height) if height and height > 0 else DEFAULT_WINDOW_H_DIP
    x = vl - w - OFFSCREEN_GAP_PX
    if x < -OFFSCREEN_LIMIT_PX:
        right = vl + vw + OFFSCREEN_GAP_PX
        x = right if right + w <= OFFSCREEN_LIMIT_PX else -OFFSCREEN_LIMIT_PX
    y = max(-OFFSCREEN_LIMIT_PX, min(vt, OFFSCREEN_LIMIT_PX - h))
    return (x, y)


def _offscreen_origin(api: Any, width: int, height: int) -> Tuple[int, int]:
    return _offscreen_origin_for(_virtual(api), width, height)


def _window_pid(api: Any, hwnd: int) -> int:
    try:
        return int(api.get_window_thread_process_id(hwnd)[1] or 0)
    except Exception:
        return 0


def _workspace_offset(api: Any, hwnd: int) -> Tuple[int, int]:
    """WINDOWPLACEMENT у окна без WS_EX_TOOLWINDOW — в координатах рабочей
    области: экранные = рабочие + (рабочая область − монитор). Сдвиг
    ненулевой, только когда панель задач слева/сверху."""
    try:
        if int(api.get_ex_style(hwnd)) & WS_EX_TOOLWINDOW:
            return (0, 0)
    except Exception:
        return (0, 0)
    rects = _opt(api, "get_monitor_rects", None, hwnd)
    try:
        mon, work = _as_rect(rects[0]), _as_rect(rects[1])
    except Exception:
        return (0, 0)
    if not mon or not work:
        return (0, 0)
    return (work[0] - mon[0], work[1] - mon[1])


def _restored_rect(api: Any, hwnd: int) -> Optional[Rect]:
    """Экранный прямоугольник окна; у свёрнутого — куда оно развернётся
    (GetWindowRect свёрнутого — плашка 160×28 на -32000, для сверки с
    границами из CDP и для запоминания места бесполезна)."""
    if api.is_iconic(hwnd):
        placement = _opt(api, "get_window_placement", None, hwnd)
        try:
            normal = _as_rect(placement[1])
        except Exception:
            normal = None
        if not normal:
            return None
        dx, dy = _workspace_offset(api, hwnd)
        return (normal[0] + dx, normal[1] + dy, normal[2] + dx, normal[3] + dy)
    return _as_rect(api.get_window_rect(hwnd))


def _primary_work_area(api: Any) -> Rect:
    wa = _as_rect(_opt(api, "get_work_area", None))
    if wa and wa[2] > wa[0] and wa[3] > wa[1]:
        return wa
    screens = _screen_rects(api)
    for mon in screens:
        if mon[0] <= 0 < mon[2] and mon[1] <= 0 < mon[3]:
            return mon  # основной монитор — тот, где точка (0,0)
    if screens:
        return screens[0]
    return (0, 0, DEFAULT_WINDOW_W_DIP, DEFAULT_WINDOW_H_DIP)


def _default_rect(api: Any, hwnd: int) -> Rect:
    """Место для окна, которому некуда вернуться: по центру рабочей области
    основного монитора, ~1280×900 DIP, но не больше 85% области."""
    wa = _primary_work_area(api)
    ww, wh = _size(wa)
    dpi = int(_opt(api, "get_dpi_for_window", 0, hwnd) or 0)
    scale = dpi / 96.0 if dpi > 0 else 1.0
    w = max(1, int(min(DEFAULT_WINDOW_W_DIP * scale, ww * DEFAULT_WORKAREA_FRACTION)))
    h = max(1, int(min(DEFAULT_WINDOW_H_DIP * scale, wh * DEFAULT_WORKAREA_FRACTION)))
    l = wa[0] + (ww - w) // 2
    t = wa[1] + (wh - h) // 2
    return (l, t, l + w, t + h)


def _prune_locked(api: Any) -> None:
    """Выкинуть записи мёртвых окон (и окон с чужим pid — HWND
    переиспользован). Дёшево: только когда словари разрослись."""
    if len(_SAVED_RECTS) + len(_ORIG_EXSTYLE) <= _PRUNE_THRESHOLD:
        return
    for store in (_SAVED_RECTS, _ORIG_EXSTYLE):
        for h, entry in list(store.items()):
            try:
                alive = bool(api.is_window(h)) and _window_pid(api, h) == entry[0]
            except Exception:
                alive = False
            if not alive:
                store.pop(h, None)


# ── Окна процесса ──

def browser_windows(pid: int, include_hidden: bool = False) -> List[int]:
    """Рамки браузера процесса pid: верхний уровень, без владельца
    (пузыри, меню, подсказки, омнибокс Chrome — «собственные» окна рамки),
    класс Chrome_WidgetWin_*, размер рамки. Видимые — и на экране, и за
    краем; свёрнутые тоже. include_hidden=True — ещё и без WS_VISIBLE
    (окно, которое Chrome только что создал и ещё не показал). Порядок —
    z-порядок, сверху вниз."""
    api = get_api()
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return []
    if api is None or pid <= 0:
        return []
    try:
        with _dpi_scope(api):
            found: List[int] = []
            for hwnd in api.enum_windows() or []:
                if not hwnd:
                    continue
                hwnd = int(hwnd)
                if _window_pid(api, hwnd) != pid:
                    continue
                if api.get_owner(hwnd):
                    continue
                if not str(api.get_class_name(hwnd) or "").startswith(
                        CHROME_WINDOW_CLASS_PREFIX):
                    continue
                if not include_hidden and not api.is_window_visible(hwnd):
                    continue
                if api.is_iconic(hwnd):
                    found.append(hwnd)
                    continue
                w, h = _size(_as_rect(api.get_window_rect(hwnd)))
                if w < FRAME_MIN_W_PX or h < FRAME_MIN_H_PX:
                    continue
                found.append(hwnd)
            return found
    except Exception as e:
        _fail("browser_windows", e)
        return []


def is_window(hwnd: int) -> bool:
    api = get_api()
    if api is None or not hwnd:
        return False
    try:
        return bool(api.is_window(int(hwnd)))
    except Exception as e:
        _fail("is_window", e)
        return False


def window_pid(hwnd: int) -> int:
    api = get_api()
    if api is None or not hwnd:
        return 0
    return _window_pid(api, int(hwnd))


def window_rect(hwnd: int) -> Optional[Rect]:
    """Экранный прямоугольник в физических пикселях; у свёрнутого окна —
    прямоугольник, в который оно развернётся."""
    api = get_api()
    if api is None or not hwnd:
        return None
    try:
        with _dpi_scope(api):
            if not api.is_window(int(hwnd)):
                return None
            return _restored_rect(api, int(hwnd))
    except Exception as e:
        _fail("window_rect", e)
        return None


def window_dpi(hwnd: int) -> int:
    """DPI окна (Chrome per-monitor: DPI его монитора); неизвестно — 96."""
    api = get_api()
    if api is None or not hwnd:
        return 96
    dpi = _opt(api, "get_dpi_for_window", 0, int(hwnd))
    try:
        dpi = int(dpi)
    except (TypeError, ValueError):
        dpi = 0
    return dpi if dpi > 0 else 96


def virtual_screen() -> Tuple[int, int, int, int]:
    """(left, top, width, height) виртуального экрана, физические пиксели."""
    api = get_api()
    if api is None:
        return _FALLBACK_SCREEN
    try:
        with _dpi_scope(api):
            return _virtual(api)
    except Exception as e:
        _fail("virtual_screen", e)
        return _FALLBACK_SCREEN


def offscreen_origin(width: int, height: int) -> Tuple[int, int]:
    """Куда ставить левый верхний угол окна width×height (физические
    пиксели), чтобы оно целиком было за пределами всех мониторов."""
    return _offscreen_origin_for(virtual_screen(), width, height)


def is_offscreen(hwnd: int) -> bool:
    """Окно целиком вне всех мониторов. Свёрнутое — НЕ «за краем» (это
    другое состояние, и рисовать Chrome его не будет)."""
    api = get_api()
    if api is None or not hwnd:
        return False
    try:
        with _dpi_scope(api):
            hwnd = int(hwnd)
            if not api.is_window(hwnd) or api.is_iconic(hwnd):
                return False
            return _rect_offscreen(api, _as_rect(api.get_window_rect(hwnd)))
    except Exception as e:
        _fail("is_offscreen", e)
        return False


# ── Скрыть / показать ──

def _set_taskbar_presence_locked(api: Any, hwnd: int, pid: int, present: bool,
                                 force_cycle: bool = False) -> bool:
    """Убрать/вернуть окно в панель задач и Alt+Tab (WS_EX_TOOLWINDOW) и
    сделать его неактивируемым/снова активируемым (WS_EX_NOACTIVATE), см.
    докстринг модуля. Возвращаем только свои биты (MANAGED_EXSTYLE):
    остальной стиль Chrome мог поменять, пока окно было спрятано (поверх
    всех — для картинки-в-картинке, слоистость), и затирать его снимком
    нельзя. → True — стиль сменился или был цикл показа (z-порядок окна
    после этого — верх)."""
    ex = int(api.get_ex_style(hwnd)) & 0xFFFFFFFF
    if present:
        entry = _ORIG_EXSTYLE.pop(hwnd, None)
        orig = entry[1] if entry and entry[0] == pid else None
        if orig is None:
            # Спрятано не нами в этом процессе (бот перезапускался, а Chrome
            # жил) — у рамки браузера своих TOOLWINDOW/NOACTIVATE не бывает
            new = ex & ~(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        else:
            new = (ex & ~MANAGED_EXSTYLE) | (orig & MANAGED_EXSTYLE)
    else:
        new = (ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW
        if new != ex:
            entry = _ORIG_EXSTYLE.get(hwnd)
            if not entry or entry[0] != pid:
                _ORIG_EXSTYLE[hwnd] = (pid, ex)
    changed = new != ex
    if changed and not api.set_ex_style(hwnd, new):
        logger.debug(f"[WinDesktop] Стиль окна {hwnd:#x} не сменился")
        changed = False
    cycled = False
    if (changed or force_cycle) and api.is_window_visible(hwnd):
        # Панель задач замечает стиль только при показе окна. Окно в этот
        # момент за краем (или свёрнуто) — цикл не виден; SW_SHOWNA
        # показывает «как было» и не активирует
        api.show_window(hwnd, SW_HIDE)
        api.show_window(hwnd, SW_SHOWNA)
        cycled = True
    return changed or cycled


def _push_bottom_locked(api: Any, hwnd: int) -> None:
    """Спрятанное окно — на дно z-порядка (без активации). SW_SHOWNA цикла
    панели задач и Activate() самого Chrome ставят верхнеуровневое окно
    наверх: следующим после закрытого пользователем окна оказалось бы оно.
    NOACTIVATE это уже исключает; дно — второй рубеж (старые сборки,
    оболочки, которые выбирают окно сами)."""
    api.set_window_pos(hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                       SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)


def _unminimize_quietly_locked(api: Any, hwnd: int) -> None:
    """Свёрнутое/развёрнутое → обычное БЕЗ активации. SetWindowPlacement
    сразу «за край» не годится: система сама возвращает полностью невидимое
    окно на монитор. Поэтому SW_SHOWNOACTIVATE (как SW_SHOWNORMAL, но без
    активации) — миг на экране; второй проход — окно, свёрнутое из
    развёрнутого, могло развернуться обратно в развёрнутое."""
    for _ in range(2):
        if not (api.is_iconic(hwnd) or api.is_zoomed(hwnd)):
            return
        api.show_window(hwnd, SW_SHOWNOACTIVATE)


def _hide_locked(api: Any, hwnd: int) -> bool:
    if not api.is_window(hwnd):
        return False
    if _opt(api, "is_hung_app_window", False, hwnd):
        # SetWindowPos/SetWindowLongPtr чужого окна — синхронные сообщения
        # его потоку: зависший UI-поток Chrome повесил бы и наш поток
        logger.debug(f"[WinDesktop] Окно {hwnd:#x} не отвечает — не прячем")
        return False
    pid = _window_pid(api, hwnd)
    _prune_locked(api)
    was_foreground = int(api.get_foreground_window() or 0) == hwnd
    iconic = bool(api.is_iconic(hwnd))
    rect = None if iconic else _as_rect(api.get_window_rect(hwnd))
    moved = False
    if iconic or not _rect_offscreen(api, rect):
        # Запоминаем место, только пока окно реально на экране: повторное
        # скрытие не должно затирать его координатами «за краем».
        # У развёрнутого — его полноэкранный прямоугольник
        keep = rect if _rect_visible(api, rect) else None
        _unminimize_quietly_locked(api, hwnd)
        cur = _as_rect(api.get_window_rect(hwnd))
        if keep is None and _rect_visible(api, cur):
            keep = cur  # свёрнутое: его место — куда оно развернулось
        saved = _SAVED_RECTS.get(hwnd)
        if keep is not None and not (saved and saved[0] == pid):
            # Запись уже есть — окно спрятано нами и с тех пор не
            # показывалось: на экран его вернул не пользователь (активация
            # Chrome, неудачный сдвиг — см. ниже), и его нынешнее место —
            # не то, куда его надо вернуть
            _SAVED_RECTS[hwnd] = (pid, keep)
        w, h = _size(keep or cur)
        x, y = _offscreen_origin(api, w, h)
        flags = SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER
        cw, ch = _size(cur)
        if keep and cur and (abs(cw - w) > SIZE_TOLERANCE_PX
                             or abs(ch - h) > SIZE_TOLERANCE_PX):
            # Из развёрнутого окно вернулось к «обычному» размеру — за
            # краем держим прежний: вёрстка страницы под агентом не прыгает
            api.set_window_pos(hwnd, HWND_TOP, x, y, w, h, flags)
        else:
            api.set_window_pos(hwnd, HWND_TOP, x, y, 0, 0, flags | SWP_NOSIZE)
        moved = True
        now = _as_rect(api.get_window_rect(hwnd))
        if not _rect_offscreen(api, now) and now:
            # Мониторы разного DPI: за краем «ближайшим» стал монитор с
            # другим масштабом, Chrome (per-monitor v2) принял предложенный
            # WM_DPICHANGED размер — окно выросло и краем вылезло на экран.
            # Угол — заново, от нынешнего размера (DPI у окна уже тот же)
            nx, ny = _offscreen_origin(api, *_size(now))
            api.set_window_pos(hwnd, HWND_TOP, nx, ny, 0, 0, flags | SWP_NOSIZE)
        if not _rect_offscreen(api, _as_rect(api.get_window_rect(hwnd))):
            # Сдвиг не удался — стиль не трогаем: окно на экране без кнопки
            # в панели задач потерялось бы для пользователя
            logger.debug(f"[WinDesktop] Окно {hwnd:#x} за край не ушло")
            return False
    # Окно уже за краем (запуск с --window-position, повторный вызов) —
    # остаётся только стиль. Активное спрятанное окно — цикл hide/show
    # обязателен даже без смены стиля: снимает с него активность
    touched = _set_taskbar_presence_locked(api, hwnd, pid, present=False,
                                           force_cycle=was_foreground)
    if moved or touched:
        _push_bottom_locked(api, hwnd)
    ok = (not api.is_iconic(hwnd)
          and _rect_offscreen(api, _as_rect(api.get_window_rect(hwnd))))
    if not ok:
        logger.debug(f"[WinDesktop] Окно {hwnd:#x} за край не ушло")
    return ok


def hide_offscreen(hwnd: int) -> bool:
    """Спрятать окно: за край виртуального экрана без активации + вон из
    панели задач и Alt+Tab + неактивируемо (WS_EX_NOACTIVATE) и на дно
    z-порядка. Прежнее место (если окно было на экране и ещё не запомнено
    нами) — для show_onscreen. Мониторы разного DPI: окно, выросшее за краем
    от WM_DPICHANGED, сдвигается повторно. True — окно за краем."""
    api = get_api()
    if api is None or not hwnd:
        return False
    try:
        with _OP_LOCK, _dpi_scope(api):
            return _hide_locked(api, int(hwnd))
    except Exception as e:
        _fail("hide_offscreen", e)
        return False


def _below_foreground(api: Any, hwnd: int) -> int:
    """Куда в z-порядке ставить возвращаемое окно: сразу ПОД активным окном
    пользователя. Спрятанное окно лежит на дне — «не трогать z-порядок»
    оставило бы его за всеми окнами; наверх — закрыло бы окно, в котором
    пользователь печатает (показ без активации — ввод остаётся там).
    Активное окно поверх всех (TOPMOST) — HWND_TOP: вставка под окно поверх
    всех сделала бы поверх всех и наше."""
    try:
        fg = int(api.get_foreground_window() or 0)
        if fg and fg != hwnd and api.is_window(fg) \
                and not int(api.get_ex_style(fg)) & WS_EX_TOPMOST:
            return fg
    except Exception as e:
        logger.debug(f"[WinDesktop] Активное окно для z-порядка: {e}")
    return HWND_TOP


def _show_locked(api: Any, hwnd: int, fallback_rect: Optional[Rect],
                 top: bool) -> bool:
    if not api.is_window(hwnd):
        return False
    if _opt(api, "is_hung_app_window", False, hwnd):
        logger.debug(f"[WinDesktop] Окно {hwnd:#x} не отвечает — не показываем")
        return False
    pid = _window_pid(api, hwnd)
    _prune_locked(api)
    iconic = bool(api.is_iconic(hwnd))
    cur = None if iconic else _as_rect(api.get_window_rect(hwnd))
    if not iconic and cur and not _rect_offscreen(api, cur):
        # Уже на экране — место не трогаем (пользователь мог развернуть или
        # подвинуть окно), только возвращаем в панель задач
        _SAVED_RECTS.pop(hwnd, None)
        _set_taskbar_presence_locked(api, hwnd, pid, present=True)
        return True
    entry = _SAVED_RECTS.pop(hwnd, None)
    target = entry[1] if entry and entry[0] == pid else None
    if target is None and fallback_rect is not None:
        target = _as_rect(fallback_rect)
    if target is None or not _rect_visible(api, target):
        # Сохранённое место пропало (монитор отключили) — по центру основного
        target = _default_rect(api, hwnd)
    # Стиль — пока окно ещё за краем/свёрнуто: цикл hide/show не виден
    _set_taskbar_presence_locked(api, hwnd, pid, present=True)
    if iconic or api.is_zoomed(hwnd):
        _unminimize_quietly_locked(api, hwnd)
        cur = _as_rect(api.get_window_rect(hwnd))
        if iconic and cur and not _rect_offscreen(api, cur):
            return True  # развернулось на своё место на экране
    l, t, r, b = target
    after = HWND_TOP if top else _below_foreground(api, hwnd)
    flags = SWP_NOACTIVATE | SWP_NOOWNERZORDER
    for _ in range(2):
        api.set_window_pos(hwnd, after, l, t, r - l, b - t, flags)
        cur = _as_rect(api.get_window_rect(hwnd))
        if cur and all(abs(a - c) <= SIZE_TOLERANCE_PX for a, c in zip(cur, target)):
            break
        # Переезд на монитор с другим DPI (WM_DPICHANGED): Chrome подгоняет
        # размер под «предложенный» системой — ставим прямоугольник ещё раз
    return bool(cur) and not _rect_offscreen(api, cur)


def show_onscreen(hwnd: int, fallback_rect: Optional[Rect] = None,
                  top: bool = False) -> bool:
    """Вернуть окно на экран: сохранённое место, иначе fallback_rect, иначе
    по центру рабочей области основного монитора (~1280×900 DIP, ≤85%);
    вернуть в панель задач/Alt+Tab и снова сделать активируемым. Без
    активации. Возвращённое из-за края окно встаёт в z-порядке сразу под
    активным окном пользователя (видно, но не закрывает то, где он
    печатает); top=True — наверх (тоже без активации). Окно, которое и так
    на экране, z-порядок не меняет. Развёрнутость не восстанавливается:
    «развернуть без активации» Win32 не умеет — окно встанет обычным, но тем
    же прямоугольником. True — окно на экране."""
    api = get_api()
    if api is None or not hwnd:
        return False
    try:
        with _OP_LOCK, _dpi_scope(api):
            return _show_locked(api, int(hwnd), fallback_rect, bool(top))
    except Exception as e:
        _fail("show_onscreen", e)
        return False


def saved_rect(hwnd: int) -> Optional[Rect]:
    """Запомненное место спрятанного окна (физические пиксели) или None."""
    with _OP_LOCK:
        entry = _SAVED_RECTS.get(int(hwnd or 0))
    return entry[1] if entry else None


def forget(hwnd: Optional[int] = None) -> None:
    """Забыть сохранённые место/стиль окна (None — всех окон): браузер
    перезапущен, HWND больше ничего не значат."""
    with _OP_LOCK:
        if hwnd is None:
            _SAVED_RECTS.clear()
            _ORIG_EXSTYLE.clear()
        else:
            _SAVED_RECTS.pop(int(hwnd), None)
            _ORIG_EXSTYLE.pop(int(hwnd), None)


# ── Метка владельца окна ──

def set_owner_tag(hwnd: int, pid: int) -> bool:
    """Пометить окно pid'ом процесса бота, который им управляет (свойство
    окна OWNER_PROP, SetPropW). Chrome пула V общий для процессов бота, а
    режим управления у каждого свой: по метке процесс не присваивает себе
    спрятанные окна соседа (иначе прятал бы их у того из-под режима
    управления). Нет поддержки у бэкенда — False (метки просто нет)."""
    api = get_api()
    if api is None or not hwnd or not pid:
        return False
    fn = getattr(api, "set_prop", None)
    if fn is None:
        return False
    try:
        return bool(fn(int(hwnd), int(pid)))
    except Exception as e:
        _fail("set_owner_tag", e)
        return False


def owner_tag(hwnd: int) -> int:
    """pid из метки владельца окна; 0 — метки нет (или не прочитать)."""
    api = get_api()
    if api is None or not hwnd:
        return 0
    try:
        return int(_opt(api, "get_prop", 0, int(hwnd)) or 0)
    except (TypeError, ValueError):
        return 0


# ── Фокус ──

def foreground_window() -> int:
    api = get_api()
    if api is None:
        return 0
    try:
        return int(api.get_foreground_window() or 0)
    except Exception as e:
        _fail("foreground_window", e)
        return 0


def force_foreground(hwnd: int) -> bool:
    """Поднять окно и отдать ему клавиатуру — по ЯВНОЙ просьбе пользователя
    («покажи страницу») или вернуть фокус, который у него украли.

    Фоновому процессу Windows SetForegroundWindow не даёт (блокировка
    переднего плана: окно только мигнёт в панели задач). Классический обход
    без синтетических нажатий клавиш — на время вызова присоединить ввод
    нашего потока к потоку текущего активного окна (и к потоку цели):
    тогда система считает нас «владельцем ввода». Подводные камни:
      * у потока должна быть очередь сообщений, иначе AttachThreadInput
        падает — PeekMessage(PM_NOREMOVE) её создаёт, ничего не забирая;
      * присоединять поток к самому себе нельзя (ошибка) — пропускаем;
      * отсоединяем в finally: забытое присоединение склеивает ввод нашего
        потока с чужим UI-потоком до конца жизни;
      * к потоку ЗАВИСШЕГО (или занятого) окна не присоединяемся — ни
        активного, ни целевого (FocusGuard возвращает фокус окну
        пользователя, а оно может как раз думать): в общей очереди ввода
        смена активного окна — синхронные WM_NCACTIVATE/WM_ACTIVATE этому
        потоку, и наш поток (часто — воркер CDP, за ним стоят все
        браузерные операции бота) ждал бы, пока «Не отвечает» не оживёт или
        не будет убито. Без присоединения та же смена для чужой очереди —
        асинхронная. Пинг — SendMessageTimeout WM_NULL с SMTO_ABORTIFHUNG
        (RESPONSIVE_TIMEOUT_MS); без присоединения к активному
        SetForegroundWindow может не сработать (блокировка) — это лучше
        зависания.
    Свёрнутое окно разворачиваем (SW_RESTORE) уже при присоединённом вводе.
    Окно «за краем» эта функция на экран НЕ возвращает — это решение
    вызывающего (show_onscreen перед ней). True — окно стало активным."""
    api = get_api()
    if api is None or not hwnd:
        return False
    try:
        hwnd = int(hwnd)
        if not api.is_window(hwnd):
            return False
        if _opt(api, "is_hung_app_window", False, hwnd):
            logger.debug(f"[WinDesktop] Окно {hwnd:#x} не отвечает — фокус не даём")
            return False
        fg = int(api.get_foreground_window() or 0)
        if fg == hwnd and not api.is_iconic(hwnd):
            api.bring_window_to_top(hwnd)
            return True
        _opt(api, "ensure_message_queue", None)
        cur_tid = int(api.get_current_thread_id() or 0)
        tids: List[int] = []
        for w in (fg, hwnd):
            if not w:
                continue
            if not _responsive(api, w):
                logger.debug(f"[WinDesktop] Окно {w:#x} не отвечает — ввод его "
                             f"потока не присоединяем")
                continue
            try:
                tid = int(api.get_window_thread_process_id(w)[0] or 0)
            except Exception:
                tid = 0
            if tid and tid != cur_tid and tid not in tids:
                tids.append(tid)
        attached: List[int] = []
        try:
            for tid in tids:
                if cur_tid and api.attach_thread_input(cur_tid, tid, True):
                    attached.append(tid)
            if api.is_iconic(hwnd):
                api.show_window(hwnd, SW_RESTORE)
            api.bring_window_to_top(hwnd)
            api.set_foreground_window(hwnd)
        finally:
            for tid in reversed(attached):
                try:
                    api.attach_thread_input(cur_tid, tid, False)
                except Exception as e:
                    logger.debug(f"[WinDesktop] Отсоединить ввод потока {tid}: {e}")
        ok = int(api.get_foreground_window() or 0) == hwnd
        if not ok:
            logger.debug(f"[WinDesktop] Окно {hwnd:#x} активным не стало "
                         f"(блокировка переднего плана)")
        return ok
    except Exception as e:
        _fail("force_foreground", e)
        return False


def _responsive(api: Any, hwnd: int) -> bool:
    """Поток окна отвечает: не «Не отвечает» (IsHungAppWindow — 5 с без
    сообщений) и обработал WM_NULL за RESPONSIVE_TIMEOUT_MS. Нет методов у
    бэкенда — считаем отвечающим (прежнее поведение)."""
    if _opt(api, "is_hung_app_window", False, hwnd):
        return False
    return bool(_opt(api, "is_responsive", True, hwnd, RESPONSIVE_TIMEOUT_MS))


def stop_flash(hwnd: int) -> None:
    """Погасить мигание кнопки окна в панели задач (FlashWindowEx
    FLASHW_STOP): Chrome мигает, когда система отказала ему в переднем
    плане, — после возврата фокуса это ложная тревога."""
    api = get_api()
    if api is None or not hwnd:
        return
    try:
        api.flash_window_stop(int(hwnd))
    except Exception as e:
        _fail("stop_flash", e)


PidSource = Union[Iterable[int], Callable[[], Iterable[int]]]


class FocusGuard:
    """Страж фокуса вокруг действия, которое может увести передний план в
    Chrome бота (новое окно/вкладка, bring_to_front): ввод пользователя не
    должен внезапно уходить в браузер бота — тем более в невидимое окно.

    Вход запоминает активное окно и его pid. Выход ждёт settle_sec (Chrome
    активирует окно асинхронно) и, если передний план теперь у процесса из
    watched_pids, а раньше был НЕ у него, — возвращает фокус прежнему окну
    и гасит мигание кнопок наблюдаемых окон. watched_pids — pid'ы или
    функция без аргументов, вызываемая на выходе (pid может появиться только
    внутри блока — например, при запуске браузера). Исключения из блока не
    глотает. .restored — фокус возвращали и это удалось."""

    def __init__(self, watched_pids: PidSource, settle_sec: float = FOCUS_SETTLE_SEC):
        self._watched = watched_pids
        self.settle_sec = max(0.0, float(settle_sec or 0.0))
        self.previous_hwnd = 0
        self.previous_pid = 0
        self.restored = False

    def _pids(self) -> Set[int]:
        src = self._watched
        try:
            items = src() if callable(src) else src
            return {int(p) for p in (items or []) if p and int(p) > 0}
        except Exception as e:
            logger.debug(f"[WinDesktop] FocusGuard: pid'ы не получены: {e}")
            return set()

    def __enter__(self) -> "FocusGuard":
        self.restored = False
        api = get_api()
        if api is None:
            return self
        try:
            self.previous_hwnd = int(api.get_foreground_window() or 0)
            self.previous_pid = _window_pid(api, self.previous_hwnd) if self.previous_hwnd else 0
        except Exception as e:
            logger.debug(f"[WinDesktop] FocusGuard: активное окно не прочитано: {e}")
            self.previous_hwnd = self.previous_pid = 0
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._settle()
        except Exception as e:
            logger.debug(f"[WinDesktop] FocusGuard: {e}")
        return False

    def _settle(self) -> None:
        api = get_api()
        if api is None or not self.previous_hwnd:
            return
        pids = self._pids()
        if not pids or self.previous_pid in pids:
            # Пользователь и так был в браузере бота — возвращать нечего
            return
        if self.settle_sec:
            time.sleep(self.settle_sec)
        fg = int(api.get_foreground_window() or 0)
        if fg and fg != self.previous_hwnd and _window_pid(api, fg) in pids \
                and api.is_window(self.previous_hwnd):
            self.restored = force_foreground(self.previous_hwnd)
            logger.info(f"[WinDesktop] Браузер бота перехватил фокус — "
                        f"{'вернули' if self.restored else 'вернуть не удалось'}")
        # Мигание гасим и без перехвата: отказ системы в переднем плане
        # Chrome тоже отмечает миганием кнопки
        for pid in pids:
            for w in browser_windows(pid):
                stop_flash(w)


# ── Настоящий бэкенд ──

class _Win32Api:
    """Тонкие обёртки над user32/kernel32: по методу на функцию Win32,
    без логики (она — в функциях модуля, где её видят тесты с фейком).

    Для КАЖДОЙ функции заданы argtypes/restype: без них ctypes передаёт
    int как 32-битный C int и обрезает 64-битные HWND/HANDLE, а результат
    тоже читает как int. HWND/HMONITOR/контексты DPI — указательного
    размера (c_void_p), GetWindowLongPtrW/SetWindowLongPtrW (в 32-битном
    user32 их нет — там это макросы на GetWindowLongW/SetWindowLongW).
    use_last_error=True — GetLastError, не затёртый самим Python между
    вызовом и чтением."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as W

        self._ct = ctypes
        self._W = W
        # Собственные экземпляры WinDLL, а не общие ctypes.windll.*: argtypes
        # вешаются на объект функции и иначе «протекли» бы в чужой код
        # (тот же _pid_alive в browser_actions зовёт windll.kernel32 без них)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ptr = ctypes.POINTER
        LONG_PTR = ctypes.c_ssize_t

        def bind(dll: Any, name: str, restype: Any, *argtypes: Any,
                 optional: bool = False) -> Any:
            fn = getattr(dll, name, None)
            if fn is None:
                if optional:
                    return None
                raise OSError(f"в {dll._name} нет {name}")
            fn.restype = restype
            fn.argtypes = list(argtypes)
            return fn

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", W.UINT), ("hwnd", W.HWND), ("dwFlags", W.DWORD),
                        ("uCount", W.UINT), ("dwTimeout", W.DWORD)]

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [("length", W.UINT), ("flags", W.UINT), ("showCmd", W.UINT),
                        ("ptMinPosition", W.POINT), ("ptMaxPosition", W.POINT),
                        ("rcNormalPosition", W.RECT)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT),
                        ("rcWork", W.RECT), ("dwFlags", W.DWORD)]

        class MSG(ctypes.Structure):
            # Свой, а не wintypes.MSG: в актуальном SDK в конце есть
            # lPrivate, которого нет в wintypes, — на x86 PeekMessage писал
            # бы за край буфера. Лишнее поле безвредно
            _fields_ = [("hwnd", W.HWND), ("message", W.UINT), ("wParam", W.WPARAM),
                        ("lParam", W.LPARAM), ("time", W.DWORD), ("pt", W.POINT),
                        ("lPrivate", W.DWORD)]

        self._FLASHWINFO = FLASHWINFO
        self._WINDOWPLACEMENT = WINDOWPLACEMENT
        self._MONITORINFO = MONITORINFO
        self._MSG = MSG
        # WINFUNCTYPE (stdcall), не CFUNCTYPE: на x86 соглашение о вызовах
        # колбэков user32 — stdcall; на x64 они совпадают
        self._WNDENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        self._MONITORENUMPROC = ctypes.WINFUNCTYPE(
            W.BOOL, W.HMONITOR, W.HDC, ptr(W.RECT), W.LPARAM)

        self._EnumWindows = bind(user32, "EnumWindows", W.BOOL,
                                 self._WNDENUMPROC, W.LPARAM)
        self._GetWindowThreadProcessId = bind(user32, "GetWindowThreadProcessId",
                                              W.DWORD, W.HWND, ptr(W.DWORD))
        self._GetClassNameW = bind(user32, "GetClassNameW", ctypes.c_int,
                                   W.HWND, W.LPWSTR, ctypes.c_int)
        self._GetWindow = bind(user32, "GetWindow", W.HWND, W.HWND, W.UINT)
        self._IsWindow = bind(user32, "IsWindow", W.BOOL, W.HWND)
        self._IsWindowVisible = bind(user32, "IsWindowVisible", W.BOOL, W.HWND)
        self._IsIconic = bind(user32, "IsIconic", W.BOOL, W.HWND)
        self._IsZoomed = bind(user32, "IsZoomed", W.BOOL, W.HWND)
        self._GetWindowRect = bind(user32, "GetWindowRect", W.BOOL,
                                   W.HWND, ptr(W.RECT))
        if getattr(user32, "GetWindowLongPtrW", None) is not None:
            self._GetWindowLong = bind(user32, "GetWindowLongPtrW", LONG_PTR,
                                       W.HWND, ctypes.c_int)
            self._SetWindowLong = bind(user32, "SetWindowLongPtrW", LONG_PTR,
                                       W.HWND, ctypes.c_int, LONG_PTR)
        else:  # 32-битный Python: *Ptr — макросы, экспорта нет
            self._GetWindowLong = bind(user32, "GetWindowLongW", W.LONG,
                                       W.HWND, ctypes.c_int)
            self._SetWindowLong = bind(user32, "SetWindowLongW", W.LONG,
                                       W.HWND, ctypes.c_int, W.LONG)
        self._SetWindowPos = bind(user32, "SetWindowPos", W.BOOL, W.HWND, W.HWND,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, W.UINT)
        self._ShowWindow = bind(user32, "ShowWindow", W.BOOL, W.HWND, ctypes.c_int)
        self._GetWindowPlacement = bind(user32, "GetWindowPlacement", W.BOOL,
                                        W.HWND, ptr(WINDOWPLACEMENT))
        self._GetSystemMetrics = bind(user32, "GetSystemMetrics", ctypes.c_int,
                                      ctypes.c_int)
        self._EnumDisplayMonitors = bind(user32, "EnumDisplayMonitors", W.BOOL,
                                         W.HDC, ptr(W.RECT), self._MONITORENUMPROC,
                                         W.LPARAM)
        self._SystemParametersInfoW = bind(user32, "SystemParametersInfoW", W.BOOL,
                                           W.UINT, W.UINT, W.LPVOID, W.UINT)
        self._MonitorFromWindow = bind(user32, "MonitorFromWindow", W.HMONITOR,
                                       W.HWND, W.DWORD)
        self._GetMonitorInfoW = bind(user32, "GetMonitorInfoW", W.BOOL,
                                     W.HMONITOR, ptr(MONITORINFO))
        # Windows 10 1607+; на старших — запасное поведение модуля
        self._GetDpiForWindow = bind(user32, "GetDpiForWindow", W.UINT, W.HWND,
                                     optional=True)
        self._SetThreadDpiAwarenessContext = bind(
            user32, "SetThreadDpiAwarenessContext", ctypes.c_void_p,
            ctypes.c_void_p, optional=True)
        self._IsHungAppWindow = bind(user32, "IsHungAppWindow", W.BOOL, W.HWND,
                                     optional=True)
        self._GetForegroundWindow = bind(user32, "GetForegroundWindow", W.HWND)
        self._SetForegroundWindow = bind(user32, "SetForegroundWindow", W.BOOL,
                                         W.HWND)
        self._BringWindowToTop = bind(user32, "BringWindowToTop", W.BOOL, W.HWND)
        self._AttachThreadInput = bind(user32, "AttachThreadInput", W.BOOL,
                                       W.DWORD, W.DWORD, W.BOOL)
        self._PeekMessageW = bind(user32, "PeekMessageW", W.BOOL, ptr(MSG),
                                  W.HWND, W.UINT, W.UINT, W.UINT)
        self._FlashWindowEx = bind(user32, "FlashWindowEx", W.BOOL, ptr(FLASHWINFO))
        self._SendMessageTimeoutW = bind(
            user32, "SendMessageTimeoutW", W.LPARAM, W.HWND, W.UINT, W.WPARAM,
            W.LPARAM, W.UINT, W.UINT, ptr(ctypes.c_size_t), optional=True)
        # Имя свойства — атомом (MAKEINTATOM): со строкой SetPropW каждый раз
        # прибавлял бы ссылку на глобальный атом, а снимать метку с окна
        # чужого процесса перед его уничтожением мы не можем. Атом глобальный
        # — у всех процессов бота один и тот же
        self._SetPropW = bind(user32, "SetPropW", W.BOOL, W.HWND, ctypes.c_void_p,
                              W.HANDLE, optional=True)
        self._GetPropW = bind(user32, "GetPropW", W.HANDLE, W.HWND, ctypes.c_void_p,
                              optional=True)
        self._GlobalAddAtomW = bind(kernel32, "GlobalAddAtomW", W.ATOM, W.LPCWSTR,
                                    optional=True)
        self._owner_atom = 0
        self._GetCurrentThreadId = bind(kernel32, "GetCurrentThreadId", W.DWORD)
        self._ptr_mask = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1

    @staticmethod
    def _h(hwnd: Any) -> Optional[int]:
        """int → аргумент HWND; 0 — NULL (None для c_void_p)."""
        return int(hwnd) if hwnd else None

    # ── Перечисление и свойства ──

    def enum_windows(self) -> List[int]:
        found: List[int] = []

        def _cb(hwnd: Any, _lparam: Any) -> bool:
            if hwnd:
                found.append(int(hwnd))
            return True

        proc = self._WNDENUMPROC(_cb)  # ссылка жива до конца вызова
        self._EnumWindows(proc, 0)
        return found

    def get_window_thread_process_id(self, hwnd: int) -> Tuple[int, int]:
        pid = self._W.DWORD(0)
        tid = self._GetWindowThreadProcessId(self._h(hwnd), self._ct.byref(pid))
        return int(tid or 0), int(pid.value or 0)

    def get_class_name(self, hwnd: int) -> str:
        buf = self._ct.create_unicode_buffer(256)
        n = self._GetClassNameW(self._h(hwnd), buf, len(buf))
        return buf.value if n > 0 else ""

    def get_owner(self, hwnd: int) -> int:
        return int(self._GetWindow(self._h(hwnd), GW_OWNER) or 0)

    def is_window(self, hwnd: int) -> bool:
        return bool(self._IsWindow(self._h(hwnd)))

    def is_window_visible(self, hwnd: int) -> bool:
        return bool(self._IsWindowVisible(self._h(hwnd)))

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self._IsIconic(self._h(hwnd)))

    def is_zoomed(self, hwnd: int) -> bool:
        return bool(self._IsZoomed(self._h(hwnd)))

    def is_hung_app_window(self, hwnd: int) -> bool:
        if self._IsHungAppWindow is None:
            return False
        return bool(self._IsHungAppWindow(self._h(hwnd)))

    def get_window_rect(self, hwnd: int) -> Optional[Rect]:
        r = self._W.RECT()
        if not self._GetWindowRect(self._h(hwnd), self._ct.byref(r)):
            return None
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))

    def get_window_placement(self, hwnd: int) -> Optional[Tuple[int, Rect]]:
        wp = self._WINDOWPLACEMENT()
        wp.length = self._ct.sizeof(wp)
        if not self._GetWindowPlacement(self._h(hwnd), self._ct.byref(wp)):
            return None
        r = wp.rcNormalPosition
        return int(wp.showCmd), (int(r.left), int(r.top), int(r.right), int(r.bottom))

    def get_ex_style(self, hwnd: int) -> int:
        return int(self._GetWindowLong(self._h(hwnd), GWL_EXSTYLE)) & 0xFFFFFFFF

    def set_ex_style(self, hwnd: int, value: int) -> bool:
        # Стиль — DWORD, а параметр — знаковый LONG(_PTR): старший бит
        # переводим в отрицательное число, иначе ctypes бросит OverflowError
        v = int(value) & 0xFFFFFFFF
        if v >= 0x80000000:
            v -= 0x100000000
        # 0 в ответ — и ошибка, и законный прежний стиль: различаем по
        # LastError, обнулив его перед вызовом (при use_last_error это
        # приватная копия ctypes, она подставляется в систему на время вызова)
        self._ct.set_last_error(0)
        prev = self._SetWindowLong(self._h(hwnd), GWL_EXSTYLE, v)
        return not (prev == 0 and self._ct.get_last_error() != 0)

    # ── Положение и показ ──

    def set_window_pos(self, hwnd: int, insert_after: int, x: int, y: int,
                       cx: int, cy: int, flags: int) -> bool:
        return bool(self._SetWindowPos(self._h(hwnd), self._h(insert_after),
                                       int(x), int(y), int(cx), int(cy), int(flags)))

    def show_window(self, hwnd: int, cmd: int) -> bool:
        return bool(self._ShowWindow(self._h(hwnd), int(cmd)))

    # ── Экран и DPI ──

    def enum_monitors(self) -> List[Rect]:
        rects: List[Rect] = []

        def _cb(_hmon: Any, _hdc: Any, lprect: Any, _lparam: Any) -> bool:
            try:
                r = lprect.contents
                rects.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
            except Exception:
                pass
            return True

        proc = self._MONITORENUMPROC(_cb)
        self._EnumDisplayMonitors(None, None, proc, 0)
        return rects

    def get_virtual_screen(self) -> Tuple[int, int, int, int]:
        # Объединение мониторов из EnumDisplayMonitors — в координатах
        # DPI-контекста вызывающего потока; GetSystemMetrics — запасной путь
        mons = self.enum_monitors()
        if mons:
            l = min(m[0] for m in mons)
            t = min(m[1] for m in mons)
            r = max(m[2] for m in mons)
            b = max(m[3] for m in mons)
            return (l, t, r - l, b - t)
        gsm = self._GetSystemMetrics
        return (int(gsm(SM_XVIRTUALSCREEN)), int(gsm(SM_YVIRTUALSCREEN)),
                int(gsm(SM_CXVIRTUALSCREEN)), int(gsm(SM_CYVIRTUALSCREEN)))

    def get_work_area(self) -> Optional[Rect]:
        r = self._W.RECT()
        if not self._SystemParametersInfoW(SPI_GETWORKAREA, 0, self._ct.byref(r), 0):
            return None
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))

    def get_monitor_rects(self, hwnd: int) -> Optional[Tuple[Rect, Rect]]:
        hmon = self._MonitorFromWindow(self._h(hwnd), MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None
        mi = self._MONITORINFO()
        mi.cbSize = self._ct.sizeof(mi)
        if not self._GetMonitorInfoW(hmon, self._ct.byref(mi)):
            return None
        m, w = mi.rcMonitor, mi.rcWork
        return ((int(m.left), int(m.top), int(m.right), int(m.bottom)),
                (int(w.left), int(w.top), int(w.right), int(w.bottom)))

    def get_dpi_for_window(self, hwnd: int) -> int:
        if self._GetDpiForWindow is None:
            return 0
        return int(self._GetDpiForWindow(self._h(hwnd)) or 0)

    def set_thread_dpi_awareness_context(self, ctx: int) -> int:
        if self._SetThreadDpiAwarenessContext is None:
            return 0
        # Псевдо-хэндлы отрицательные (-4 = PER_MONITOR_AWARE_V2): в
        # указатель — по маске его разрядности. Прежний контекст приходит
        # непрозрачным значением и так же возвращается для восстановления
        prev = self._SetThreadDpiAwarenessContext(int(ctx) & self._ptr_mask)
        return int(prev or 0)

    # ── Фокус ──

    def get_foreground_window(self) -> int:
        return int(self._GetForegroundWindow() or 0)

    def set_foreground_window(self, hwnd: int) -> bool:
        return bool(self._SetForegroundWindow(self._h(hwnd)))

    def bring_window_to_top(self, hwnd: int) -> bool:
        return bool(self._BringWindowToTop(self._h(hwnd)))

    def get_current_thread_id(self) -> int:
        return int(self._GetCurrentThreadId() or 0)

    def attach_thread_input(self, tid_from: int, tid_to: int, attach: bool) -> bool:
        return bool(self._AttachThreadInput(int(tid_from), int(tid_to),
                                            bool(attach)))

    def ensure_message_queue(self) -> None:
        # Первый вызов PeekMessage создаёт потоку очередь; PM_NOREMOVE —
        # ничего из неё не забираем
        msg = self._MSG()
        self._PeekMessageW(self._ct.byref(msg), None, 0, 0, PM_NOREMOVE)

    def is_responsive(self, hwnd: int, timeout_ms: int) -> bool:
        if self._SendMessageTimeoutW is None:
            return True
        res = self._ct.c_size_t(0)
        # 0 — таймаут или поток завис (SMTO_ABORTIFHUNG — не ждать вовсе);
        # SMTO_BLOCK — наш поток на это время чужих сообщений не разбирает
        return bool(self._SendMessageTimeoutW(
            self._h(hwnd), WM_NULL, 0, 0, SMTO_ABORTIFHUNG | SMTO_BLOCK,
            int(timeout_ms), self._ct.byref(res)))

    def _atom(self) -> int:
        if not self._owner_atom and self._GlobalAddAtomW is not None:
            self._owner_atom = int(self._GlobalAddAtomW(OWNER_PROP) or 0)
        return self._owner_atom

    def set_prop(self, hwnd: int, value: int) -> bool:
        atom = self._atom()
        if not atom or self._SetPropW is None:
            return False
        return bool(self._SetPropW(self._h(hwnd), atom, int(value) or None))

    def get_prop(self, hwnd: int) -> int:
        atom = self._atom()
        if not atom or self._GetPropW is None:
            return 0
        return int(self._GetPropW(self._h(hwnd), atom) or 0)

    def flash_window_stop(self, hwnd: int) -> bool:
        fi = self._FLASHWINFO()
        fi.cbSize = self._ct.sizeof(fi)
        fi.hwnd = self._h(hwnd)
        fi.dwFlags = FLASHW_STOP
        fi.uCount = 0
        fi.dwTimeout = 0
        # Ответ FlashWindowEx — «было ли окно активно», не успех
        self._FlashWindowEx(self._ct.byref(fi))
        return True


__all__ = [
    "set_api", "get_api", "is_supported",
    "browser_windows", "is_window", "window_pid", "window_rect", "window_dpi",
    "virtual_screen", "offscreen_origin", "is_offscreen",
    "hide_offscreen", "show_onscreen", "saved_rect", "forget",
    "set_owner_tag", "owner_tag",
    "foreground_window", "force_foreground", "stop_flash", "FocusGuard",
]

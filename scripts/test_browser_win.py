"""Тест Windows-логики окон Chrome бота: app/features/win_desktop.py (Win32
через ctypes) и app/features/browser_win.py (monkey-patch browser_actions).

Ни Windows, ни Chrome, ни Playwright, ни Ollama здесь нет: Win32 —
фейковый оконный менеджер (win_desktop.set_api), CDP — фейковый Chrome за
подменённым _raw_call, воркер и страницы playwright — фейки, оригиналы
патчимых функций подменены записывающими заглушками ДО install(force=True),
так что «штатное поведение» в тестах ничего настоящего не запускает
(AppleScript, Chrome на 9222/9223, ensure_browser).

  A. штатный путь импорта новых модулей не тянет; о них знает только
     проверка ОС в app.main (вне Windows — не импортирует, на Windows —
     install(), сбой install() бота не роняет);
  B. macOS/Linux: install() — no-op, атрибуты browser_actions те же объекты;
     win_desktop без бэкенда отдаёт безопасные значения;
  C. выключатель VPC_WIN_WINDOWS=0;
  D. install(force=True) патчит ровно намеченные точки, uninstall всё
     возвращает; несовпавшая сигнатура — не ставится НИЧЕГО;
  E. win_desktop: скрытие/показ, сохранённое место, стиль панели задач,
     никогда -32000, DPI, FocusGuard, force_foreground;
  F. метка маршрута open_new_tab → поток воркера → _new_page_quiet;
  G. _raw_call: перехват только Target.createTarget (пул V — окно агента,
     пул H hidden — за экраном, headless/headed — штатно);
  H. режим управления: вход показывает окна агента, выход последнего чата
     прячет только их;
  I. явный подъём вкладки (_focus_browser_tab);
  J. тихое переключение вкладки (_activate_tab_quietly);
  K. скрытие окна при запуске (_hide_pool_window);
  L. подметальщик всплывающих окон;
  M. прокси subprocess: --window-position только пулу H hidden, пул V —
     опрос первого окна сразу после запуска;
  P. rescue пула H: режим прежнего Chrome не прячет окно капчи;
  Q. общий Chrome пула V у нескольких процессов бота (метка владельца);
  N. сбой пути Windows → штатное поведение у каждого патча;
  O. подметальщик: настоящий цикл (один поток, сам выходит, поколения).

Запуск: python -m scripts.test_browser_win
"""

import contextlib
import inspect
import itertools
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


# ── Фейковый Win32 (бэкенд win_desktop) ──

SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
SWP_NOACTIVATE = 0x0010
HWND_TOP, HWND_BOTTOM = 0, 1
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE, SW_SHOWNA, SW_RESTORE = 0, 4, 8, 9


class Win:
    def __init__(self, pid, rect, cls="Chrome_WidgetWin_1", owner=0,
                 visible=True, ex=0x100, dpi=96, appear_in=0):
        self.pid, self.tid = pid, pid + 100000
        self.rect, self.normal = tuple(rect), tuple(rect)
        self.cls, self.owner, self.visible, self.ex = cls, owner, visible, ex
        self.iconic = self.zoomed = False
        self.dpi = dpi
        self.busy = self.hung = False
        self.appear_in = appear_in  # столько опросов видимости окно ещё не показано


class FakeWin32:
    """Оконный менеджер «на бумаге»: методы — как у win_desktop._Win32Api.
    z-порядок: self.z (сверху вниз); активация поднимает окно наверх; при
    скрытии/закрытии активного окна активным становится следующее по
    z-порядку видимое окно без WS_EX_NOACTIVATE (как пишет MSDN)."""

    def __init__(self):
        self.w = {}
        self.z = []
        self.seq = itertools.count(0x1000)
        self._fg = 0
        self.monitors = [(0, 0, 1920, 1080)]
        self.mon_dpi = None   # DPI мониторов: окно, сменившее монитор, — WM_DPICHANGED
        self.work = (0, 0, 1920, 1040)
        self.calls = []
        self.dpi_ctx = 111
        self.attached = set()
        self.attach_log = []
        self.flash_stopped = []
        self.block_fg = False
        self.activations = 0  # set_window_pos без SWP_NOACTIVATE
        self.xs = []          # все x, куда ставились окна
        self.props = {}

    @property
    def fg(self):
        return self._fg

    @fg.setter
    def fg(self, h):
        self._fg = h
        if h and h in self.z:
            self.z.remove(h)
            self.z.insert(0, h)

    def _next_active(self, skip):
        for x in self.z:
            win = self.w[x]
            if x != skip and win.visible and not win.iconic \
                    and not win.ex & WS_EX_NOACTIVATE:
                return x
        return 0

    def add(self, win):
        h = next(self.seq)
        self.w[h] = win
        self.z.insert(0, h)
        return h

    def close(self, h):
        # Пользователь закрыл окно: активным — следующее активируемое по z
        was = self._fg == h
        self.w.pop(h, None)
        if h in self.z:
            self.z.remove(h)
        if was:
            self._fg = self._next_active(h)

    def enum_windows(self):
        return list(self.z)

    def get_window_thread_process_id(self, h):
        x = self.w.get(h)
        return (x.tid, x.pid) if x else (0, 0)

    def get_class_name(self, h):
        return self.w[h].cls

    def get_owner(self, h):
        return self.w[h].owner

    def is_window(self, h):
        return h in self.w

    def is_window_visible(self, h):
        win = self.w[h]
        if win.appear_in > 0:
            win.appear_in -= 1
            return False
        return win.visible

    def is_iconic(self, h):
        return self.w[h].iconic

    def is_zoomed(self, h):
        return self.w[h].zoomed

    def get_window_rect(self, h):
        x = self.w.get(h)
        if not x:
            return None
        return (-32000, -32000, -31840, -31972) if x.iconic else x.rect

    def get_window_placement(self, h):
        return (2 if self.w[h].iconic else 1, self.w[h].normal)

    def get_ex_style(self, h):
        return self.w[h].ex

    def set_ex_style(self, h, v):
        self.calls.append(("ex", h, v))
        self.w[h].ex = v
        return True

    def _nearest_mon(self, rect):
        best, best_key = 0, None
        for i, m in enumerate(self.monitors):
            ow = max(0, min(rect[2], m[2]) - max(rect[0], m[0]))
            oh = max(0, min(rect[3], m[3]) - max(rect[1], m[1]))
            dx = max(m[0] - rect[2], rect[0] - m[2], 0)
            dy = max(m[1] - rect[3], rect[1] - m[3], 0)
            key = (-(ow * oh), dx * dx + dy * dy)
            if best_key is None or key < best_key:
                best, best_key = i, key
        return best

    def set_window_pos(self, h, after, x, y, cx, cy, flags):
        self.calls.append(("pos", h, after, x, y, cx, cy, flags))
        if not flags & SWP_NOACTIVATE:
            self.activations += 1
        win = self.w[h]
        if not flags & SWP_NOZORDER:
            self.z.remove(h)
            if after == HWND_TOP:
                self.z.insert(0, h)
            elif after == HWND_BOTTOM:
                self.z.append(h)
            else:
                self.z.insert(self.z.index(after) + 1 if after in self.z else 0, h)
        l, t, r, b = win.rect
        if flags & SWP_NOMOVE:
            x, y = l, t
        if flags & SWP_NOSIZE:
            cx, cy = r - l, b - t
        if flags & SWP_NOMOVE and flags & SWP_NOSIZE:
            return True
        rect = (x, y, x + cx, y + cy)
        if self.mon_dpi:
            # Per-monitor v2: окно на мониторе другого DPI — Chrome принимает
            # предложенный WM_DPICHANGED размер (угол остаётся)
            nd = self.mon_dpi[self._nearest_mon(rect)]
            if nd != win.dpi:
                k = nd / win.dpi
                rect = (x, y, x + int(cx * k), y + int(cy * k))
                win.dpi = nd
        win.rect = rect
        win.normal = win.rect
        self.xs.append(x)
        return True

    def show_window(self, h, cmd):
        self.calls.append(("show", h, cmd))
        win = self.w[h]
        was = win.visible
        if cmd == SW_HIDE:
            win.visible = False
            if self._fg == h:
                # Как Windows: активность — следующему активируемому по z
                self._fg = self._next_active(h)
        elif cmd == SW_SHOWNA:
            if not was:
                # Показ верхнеуровневого окна — наверх z-порядка, без активации
                self.z.remove(h)
                self.z.insert(0, h)
            win.visible = True
        elif cmd in (SW_SHOWNOACTIVATE, SW_RESTORE):
            win.visible = True
            if win.iconic or win.zoomed:
                win.iconic = win.zoomed = False
                win.rect = win.normal
            if cmd == SW_RESTORE and not self.block_fg:
                self.fg = h
        return was

    def get_virtual_screen(self):
        l = min(m[0] for m in self.monitors)
        t = min(m[1] for m in self.monitors)
        r = max(m[2] for m in self.monitors)
        b = max(m[3] for m in self.monitors)
        return (l, t, r - l, b - t)

    def enum_monitors(self):
        return list(self.monitors)

    def get_work_area(self):
        return self.work

    def is_hung_app_window(self, h):
        return h in self.w and self.w[h].hung

    def is_responsive(self, h, timeout_ms):
        return h in self.w and not (self.w[h].busy or self.w[h].hung)

    def get_prop(self, h):
        return self.props.get(h, 0)

    def set_prop(self, h, value):
        if h not in self.w:
            return False
        self.props[h] = value
        return True

    def get_dpi_for_window(self, h):
        return self.w[h].dpi if h in self.w else 0

    def set_thread_dpi_awareness_context(self, ctx):
        prev, self.dpi_ctx = self.dpi_ctx, ctx
        return prev

    def get_foreground_window(self):
        return self.fg

    def set_foreground_window(self, h):
        if self.block_fg and not self.attached:
            return False
        self.fg = h
        return True

    def bring_window_to_top(self, h):
        return True

    def get_current_thread_id(self):
        return 7

    def attach_thread_input(self, a, b, on):
        (self.attached.add if on else self.attached.discard)((a, b))
        if on:
            self.attach_log.append(b)
        return True

    def ensure_message_queue(self):
        self.calls.append(("queue",))

    def flash_window_stop(self, h):
        self.flash_stopped.append(h)
        return True


class ExplodingWin32:
    """Бэкенд, у которого падает всё: win_desktop обязан не уронить вызывающего."""

    def __getattr__(self, name):
        def boom(*a, **k):
            raise OSError(f"{name}: access denied")
        return boom


# ── Фейковый Chrome (CDP за подменённым _raw_call) ──

class FakeChrome:
    def __init__(self, win, pid, scale=1.0):
        self.win, self.pid, self.scale = win, pid, scale
        self.windows = {}   # windowId → hwnd
        self.targets = {}   # targetId → {url, wid, opener}
        self.sessions = {}  # sessionId → targetId
        self.calls = []
        self.last_active = None
        self.wseq = itertools.count(1)
        self.tseq = itertools.count(1)
        self.sseq = itertools.count(1)
        self.steal_focus = True   # новое окно забирает передний план
        self.clamp = False        # система возвращает новое окно на экран
        self.open_mode = "same"   # window.open: same | elsewhere | blocked
        self.reject_bounds = False
        self.activate_steals = False
        self.severed = []
        self.on_create = None     # вызывается перед созданием нового окна
        self.launch_pos = None    # --window-position: КАЖДОЕ новое окно — туда
        self.appear_in = 0        # новое окно показывается не сразу

    def add_window(self, rect=(100, 100, 1380, 1000), visible=True):
        h = self.win.add(Win(self.pid, rect, visible=visible,
                             dpi=int(96 * self.scale), appear_in=self.appear_in))
        wid = next(self.wseq)
        self.windows[wid] = h
        self.last_active = wid
        return wid, h

    def close_window(self, wid):
        # Окно закрыто: и в CDP, и в Win32
        self.win.w.pop(self.windows.pop(wid), None)
        for t in [t for t, d in self.targets.items() if d["wid"] == wid]:
            self.targets.pop(t)

    def close_hwnd(self, h):
        wid = self.wid_of_hwnd(h)
        if wid is not None:
            self.close_window(wid)

    def add_tab(self, wid, url="about:blank", opener=None):
        tid = f"T{next(self.tseq):04d}"
        self.targets[tid] = {"url": url, "wid": wid, "opener": opener}
        return tid

    def wid_of_hwnd(self, h):
        return next((w for w, x in self.windows.items() if x == h), None)

    def bounds(self, wid):
        win = self.win.w[self.windows[wid]]
        l, t, r, b = win.normal if win.iconic else win.rect
        s = self.scale
        return {"left": int(l / s), "top": int(t / s), "width": int((r - l) / s),
                "height": int((b - t) / s),
                "windowState": "minimized" if win.iconic else "normal"}

    def __call__(self, method, params=None, session_id=None):
        p = dict(params or {})
        self.calls.append((method, p, session_id))
        if method == "SystemInfo.getProcessInfo":
            return {"processInfo": [{"type": "renderer", "id": self.pid + 7},
                                    {"type": "browser", "id": self.pid}]}
        if method == "Browser.getVersion":
            return {"product": "Chrome/154"}
        if method == "Target.getTargets":
            out = []
            for t, d in self.targets.items():
                info = {"targetId": t, "type": "page", "url": d["url"]}
                if d["opener"]:
                    info["openerId"] = d["opener"]
                out.append(info)
            return {"targetInfos": out}
        if method == "Browser.getWindowForTarget":
            wid = self.targets[p["targetId"]]["wid"]
            return {"windowId": wid, "bounds": self.bounds(wid)}
        if method == "Target.createTarget":
            if p.get("newWindow"):
                if self.on_create:
                    self.on_create()
                if self.reject_bounds and "left" in p:
                    raise BA.BrowserUnavailable(
                        "CDP Target.createTarget: Invalid window bounds")
                if "left" in p:
                    s = self.scale
                    rect = (int(p["left"] * s), int(p["top"] * s),
                            int((p["left"] + p.get("width", 800)) * s),
                            int((p["top"] + p.get("height", 600)) * s))
                else:
                    rect = (200, 150, 1480, 1050)
                if self.clamp:
                    w, h = rect[2] - rect[0], rect[3] - rect[1]
                    rect = (100, 100, 100 + w, 100 + h)
                if self.launch_pos:
                    # Chrome: --window-position из командной строки — поверх
                    # границ из CDP, для каждого окна
                    w, h = rect[2] - rect[0], rect[3] - rect[1]
                    lx, ly = self.launch_pos
                    rect = (lx, ly, lx + w, ly + h)
                wid, h = self.add_window(rect)
                if self.steal_focus or not p.get("background"):
                    self.win.fg = h
            else:
                wid = self.last_active
            return {"targetId": self.add_tab(wid, p.get("url") or "about:blank")}
        if method == "Target.attachToTarget":
            sid = f"S{next(self.sseq)}"
            self.sessions[sid] = p["targetId"]
            return {"sessionId": sid}
        if method == "Target.detachFromTarget":
            self.sessions.pop(p.get("sessionId"), None)
            return {}
        if method == "Runtime.evaluate":
            src = self.sessions.get(session_id)
            expr = p.get("expression", "")
            if src and "window.open(" in expr:
                if not p.get("userGesture") or self.open_mode == "blocked":
                    return {"result": {"value": False}}
                wid = self.targets[src]["wid"]
                if self.open_mode in ("elsewhere", "foreign"):
                    wid = next(w for w in self.windows if w != wid)
                # foreign: вкладка без openerId — по виду не наша
                self.add_tab(wid, opener=None if self.open_mode == "foreign" else src)
                return {"result": {"value": True}}
            if src and "window.opener = null" in expr:
                self.severed.append(src)
            return {"result": {"value": True}}
        if method == "Target.closeTarget":
            self.targets.pop(p["targetId"], None)
            return {"success": True}
        if method == "Target.activateTarget":
            if self.activate_steals:
                self.win.fg = self.windows[self.targets[p["targetId"]]["wid"]]
            return {}
        if method == "Browser.setWindowBounds":
            st = (p.get("bounds") or {}).get("windowState")
            h = self.windows[p["windowId"]]
            if st == "normal":
                self.win.w[h].iconic = False
                self.win.w[h].rect = self.win.w[h].normal
            return {}
        raise RuntimeError(f"фейковый Chrome не знает {method}")


class FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.dead = False

    def poll(self):
        return 0 if self.dead else None


# ── Фейки playwright (поток воркера) ──

class Page:
    def __init__(self, chrome, tid):
        self.chrome, self.tid = chrome, tid
        self.url = "about:blank"
        self.closed = False
        self.on_front = None
        self.fronted = 0

    def goto(self, url, **kw):
        self.url = url
        if self.tid in self.chrome.targets:
            self.chrome.targets[self.tid]["url"] = url

    def close(self):
        self.closed = True
        self.chrome.targets.pop(self.tid, None)

    def bring_to_front(self):
        self.fronted += 1
        if self.on_front:
            self.on_front()


class Ctx:
    def __init__(self, chrome):
        self.chrome = chrome
        self.pages = []

    @contextlib.contextmanager
    def expect_page(self, timeout=0):
        before = set(self.chrome.targets)

        class Ev:
            value = None
        ev = Ev()
        yield ev
        new = [t for t in self.chrome.targets if t not in before]
        if not new:
            raise TimeoutError("нет новой страницы")
        pg = Page(self.chrome, new[-1])
        self.pages.append(pg)
        ev.value = pg

    def new_cdp_session(self, page):
        class S:
            def send(_s, m, p=None):
                return {"targetInfo": {"targetId": page.tid}}

            def detach(_s):
                pass
        return S()


class FakeWorker:
    def __init__(self, chrome):
        class B:
            def new_browser_cdp_session(_b):
                class S:
                    def send(_s, m, p=None):
                        return chrome(m, p)

                    def detach(_s):
                        pass
                return S()
        self._browser = B()


# ── Подмены оригиналов browser_actions (записывающие заглушки) ──

BA = None
CHROME = {"v": None, "h": None}
LOG = []          # (что, аргументы) — вызовы «штатного» поведения
ALIVE = set()
H_DESIRED = {"mode": "headless"}
WORKER_HINTS = []
OPEN_CALLS_WORKER = {"on": False, "ctx": None, "w": None}


def fake_raw_call(method, params=None, session_id=None, pool="v", tab_id=None,
                  timeout=None, _retried=False):
    LOG.append(("raw", method, dict(params or {}), session_id, pool, tab_id, _retried))
    chrome = CHROME.get(pool)
    if chrome is None:
        raise BA.BrowserUnavailable(f"пул {pool} недоступен")
    return chrome(method, params, session_id)


def fake_open_new_tab(url, background=False, pool="v", focus=False):
    # Как штатный: работа уходит в поток воркера (имя — как у настоящего)
    box = {}

    def _job():
        box["hint"] = BW._hint_for(url)
        if OPEN_CALLS_WORKER["on"]:
            box["res"] = BA._CdpWorker._new_page_quiet(
                OPEN_CALLS_WORKER["w"], OPEN_CALLS_WORKER["ctx"], url)
    t = threading.Thread(target=_job, name="vpc-cdp-99")
    t.start()
    t.join()
    WORKER_HINTS.append(box.get("hint"))
    LOG.append(("open_new_tab", url, background, pool, focus))
    box_res = box.get("res")
    return box_res if box_res is not None else 77


def fake_set_control_mode(chat_id, on):
    LOG.append(("set_control_mode", chat_id, on))
    if on:
        BA._CONTROL_MODE_CHATS.add(str(chat_id))
    else:
        BA._CONTROL_MODE_CHATS.discard(str(chat_id))


def fake_hide_pool_window(pid=None):
    LOG.append(("hide_pool_window", pid))


def fake_focus_browser_tab(url):
    LOG.append(("focus_browser_tab", url))
    return "orig-focus"


def fake_new_page_quiet(self, ctx, url):
    LOG.append(("new_page_quiet", url))
    return ("ORIG-PAGE", True)


def fake_activate_tab_quietly(self, page, url):
    LOG.append(("activate_tab_quietly", url))
    return "orig-activate"


def fake_execute(self, run, chat_id, router, a, line):
    LOG.append(("execute", BW._in_task_agent()))
    BA.open_new_tab(a.get("url", "https://pizza.example/"), focus=True)
    return ("ok", None)


def logged(kind):
    return [e for e in LOG if e[0] == kind]


BW = None
WD = None


def main():
    global BA, BW, WD
    # Предупреждения путей Windows ожидаемы (тесты сбоев) — не засоряем вывод
    logging.disable(logging.WARNING)
    from app.features import browser_actions as ba
    from app.features import browser_win as bw
    from app.features import task_agent as ta
    from app.features import win_desktop as wd
    BA, BW, WD = ba, bw, wd

    PATCHED_MOD = {"_raw_call", "open_new_tab", "set_control_mode",
                   "_hide_pool_window", "_focus_browser_tab", "subprocess"}
    PATCHED_WORKER = {"_new_page_quiet", "_activate_tab_quietly"}

    def snap():
        return (dict(vars(ba)), dict(ba._CdpWorker.__dict__),
                dict(ta.TaskAgent.__dict__))

    def changed(before):
        mod, wk, task = before
        return ({k for k, v in vars(ba).items() if mod.get(k) is not v},
                {k for k, v in ba._CdpWorker.__dict__.items() if wk.get(k) is not v},
                {k for k, v in ta.TaskAgent.__dict__.items() if task.get(k) is not v})

    # ── A ──
    section("A. Штатный путь импорта не знает о новых модулях")
    code = ("import sys, app.features.browser_actions, app.features.task_agent, "
            "app.features.computer_control, app.features.web_llm; "
            "print(sorted(m for m in ('app.features.browser_win', "
            "'app.features.win_desktop') if m in sys.modules))")
    env = dict(os.environ, OLLAMA_URL="http://127.0.0.1:9")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, timeout=180)
    check("импорт browser_actions/task_agent/computer_control/web_llm не тянет "
          "browser_win/win_desktop", r.stdout.strip().endswith("[]"))
    mine = {"app/features/browser_win.py", "app/features/win_desktop.py"}
    mentions = []
    for p in (ROOT / "app").rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel in mine:
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"\b(browser_win|win_desktop)\b", txt):
            mentions.append(rel)
    check("о новых модулях знает только app/main.py (проверка ОС)",
          mentions == ["app/main.py"])
    # Проверка ОС в app.main: вне Windows модуль не импортируется
    code = ("import sys, app.main as m; m._install_windows_browser_logic(); "
            "print('app.features.browser_win' in sys.modules)")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, timeout=180)
    check("app.main вне Windows: browser_win не импортируется",
          sys.platform == "win32" or r.stdout.strip().endswith("False"))
    # Windows (sys.platform подменён): install() зовётся, его сбой бота не
    # роняет
    code = "\n".join([
        "import sys, app.main as m",
        "from app.features import browser_win as bw",
        "calls = []",
        "bw.install = lambda *a, **k: calls.append(1) or True",
        "sys.platform = 'win32'",
        "m._install_windows_browser_logic()",
        "def boom(*a, **k): raise RuntimeError('boom')",
        "bw.install = boom",
        "m._install_windows_browser_logic()",
        "print(calls)",
    ])
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, timeout=180)
    check("app.main на Windows: install() зовётся, его сбой не роняет запуск",
          r.stdout.strip().endswith("[1]"))

    # ── B ──
    section("B. Не Windows: install() — no-op")
    before = snap()
    if sys.platform != "win32":
        check("install() вне Windows → False", bw.install() is False)
        check("is_installed() — False", bw.is_installed() is False)
        mod_c, wk_c, task_c = changed(before)
        check("атрибуты browser_actions — те же объекты", not mod_c)
        check("методы _CdpWorker и TaskAgent — те же объекты",
              not wk_c and not task_c)
        check("ba.subprocess — настоящий модуль", ba.subprocess is subprocess)
        wd.set_api(None)
        check("win_desktop без бэкенда: get_api() — None", wd.get_api() is None)
        check("безопасные значения по умолчанию",
              wd.hide_offscreen(123) is False and wd.show_onscreen(123) is False
              and wd.virtual_screen() == (0, 0, 0, 0) and wd.window_dpi(5) == 96
              and wd.browser_windows(42) == [] and wd.force_foreground(5) is False
              and wd.is_offscreen(5) is False and wd.foreground_window() == 0)
        with wd.FocusGuard({1}, settle_sec=0) as g:
            pass
        check("FocusGuard без бэкенда ничего не делает", g.restored is False)
    else:
        print("  (пропущено: запуск на Windows)")

    # ── C ──
    section("C. Выключатель VPC_WIN_WINDOWS=0")
    old_env = os.environ.get("VPC_WIN_WINDOWS")
    os.environ["VPC_WIN_WINDOWS"] = "0"
    try:
        before = snap()
        check("install(force=True) при VPC_WIN_WINDOWS=0 → False",
              bw.install(force=True) is False)
        check("ничего не пропатчено", changed(before) == (set(), set(), set()))
    finally:
        if old_env is None:
            os.environ.pop("VPC_WIN_WINDOWS", None)
        else:
            os.environ["VPC_WIN_WINDOWS"] = old_env

    # ── D ──
    section("D. install(force=True) / uninstall на настоящих функциях")
    before = snap()
    sigs = {n: inspect.signature(getattr(ba, n)) for n in PATCHED_MOD - {"subprocess"}}
    try:
        check("install(force=True) → True", bw.install(force=True) is True)
        mod_c, wk_c, task_c = changed(before)
        check(f"пропатчены ровно {sorted(PATCHED_MOD)}", mod_c == PATCHED_MOD)
        check("у _CdpWorker — ровно _new_page_quiet и _activate_tab_quietly",
              wk_c == PATCHED_WORKER)
        check("у TaskAgent — ровно _execute", task_c == {"_execute"})
        check("_raw_call — без лишней обёртки", ba._raw_call is bw._raw_call_win)
        check("сигнатуры патчей совпадают со штатными",
              all(inspect.signature(getattr(ba, n)) == s for n, s in sigs.items()))
        check("ba.subprocess — прокси, run/DEVNULL настоящие",
              isinstance(ba.subprocess, bw._SubprocessProxy)
              and ba.subprocess.run is subprocess.run
              and ba.subprocess.DEVNULL == subprocess.DEVNULL)
        check("Popen прокси — подкласс настоящего",
              issubclass(ba.subprocess.Popen, subprocess.Popen))
        snap2 = snap()
        check("повторный install — True и без второго слоя",
              bw.install(force=True) is True and changed(snap2) == (set(), set(), set()))
    finally:
        bw.uninstall()
    check("uninstall вернул всё как было", changed(before) == (set(), set(), set()))
    check("is_installed() после uninstall — False", bw.is_installed() is False)

    real_focus = ba._focus_browser_tab
    ba._focus_browser_tab = lambda url, extra=None: False
    try:
        before = snap()
        check("сигнатура точки патча изменилась → install False",
              bw.install(force=True) is False)
        check("…и не пропатчено НИЧЕГО", changed(before) == (set(), set(), set()))
    finally:
        ba._focus_browser_tab = real_focus
        bw.uninstall()

    # ── E ──
    section("E. win_desktop: геометрия, стиль, фокус (фейковый Win32)")
    api = FakeWin32()
    wd.set_api(api)
    CH = 500
    h_main = api.add(Win(CH, (100, 100, 1380, 1000), ex=0x100 | WS_EX_APPWINDOW))
    api.add(Win(CH, (0, 0, 0, 0), visible=False))                    # служебное
    api.add(Win(CH, (200, 200, 500, 400), owner=h_main))             # пузырь
    api.add(Win(CH, (200, 200, 900, 700), cls="Other"))
    h_foreign = api.add(Win(77, (50, 50, 900, 700), cls="Notepad"))
    h_small = api.add(Win(CH, (10, 10, 50, 30)))
    h_2 = api.add(Win(CH, (10, 10, 900, 700)))
    check("browser_windows: только рамки Chrome этого pid",
          sorted(wd.browser_windows(CH)) == [h_main, h_2]
          and h_small not in wd.browser_windows(CH))
    check("контекст DPI потока восстановлен", api.dpi_ctx == 111)

    api.fg = h_foreign
    check("hide_offscreen → True", wd.hide_offscreen(h_main) is True)
    r = api.w[h_main].rect
    vl, vt, _vw, _vh = wd.virtual_screen()
    check("за краем: x = левый край − ширина − 2000, y = верх, размер прежний",
          r == (vl - 1280 - 2000, vt, vl - 2000, vt + 900))
    check("is_offscreen", wd.is_offscreen(h_main) is True)
    check("место запомнено", wd.saved_rect(h_main) == (100, 100, 1380, 1000))
    ex = api.w[h_main].ex
    check("вон из панели задач: TOOLWINDOW есть, APPWINDOW снят",
          ex & WS_EX_TOOLWINDOW and not ex & WS_EX_APPWINDOW)
    shows = [c for c in api.calls if c[0] == "show" and c[1] == h_main]
    check("панель задач замечает стиль: цикл SW_HIDE → SW_SHOWNA",
          [c[2] for c in shows[-2:]] == [SW_HIDE, SW_SHOWNA])
    check("без активации: фокус у прежнего окна, все SetWindowPos с NOACTIVATE",
          api.fg == h_foreign and api.activations == 0)
    n_calls = len(api.calls)
    check("повторное скрытие — без лишних вызовов, место не затёрто",
          wd.hide_offscreen(h_main) is True and len(api.calls) == n_calls
          and wd.saved_rect(h_main) == (100, 100, 1380, 1000))

    check("show_onscreen → True", wd.show_onscreen(h_main) is True)
    check("место восстановлено", api.w[h_main].rect == (100, 100, 1380, 1000))
    check("стиль восстановлен в точности (и APPWINDOW вернулся)",
          api.w[h_main].ex == 0x100 | WS_EX_APPWINDOW)
    check("сохранённое место сброшено", wd.saved_rect(h_main) is None)
    check("показ без активации", api.fg == h_foreign and api.activations == 0)

    api.fg = h_2
    wd.hide_offscreen(h_2)
    check("скрытие активного окна снимает с него активность", api.fg != h_2)
    wd.show_onscreen(h_2)

    # Находка: спрятанное окно оставалось активируемым и наверху z-порядка —
    # закрыл пользователь своё окно, и система отдала клавиатуру невидимому
    api.fg = h_foreign
    wd.hide_offscreen(h_main)
    ex = api.w[h_main].ex
    check("спрятанное окно — WS_EX_NOACTIVATE (система не выберет его активным)",
          bool(ex & WS_EX_NOACTIVATE))
    check("спрятанное окно — на дне z-порядка", api.z[-1] == h_main)
    h_note = api.add(Win(77, (300, 300, 900, 700), cls="Notepad"))
    api.fg = h_note                    # пользователь кликнул в Блокнот
    # Chrome сам поднял спрятанное окно наверх (Activate без фокуса)
    api.z.remove(h_main)
    api.z.insert(1, h_main)
    api.close(h_note)                  # …и закрыл Блокнот
    check("закрыл своё окно → активным стало НЕ невидимое окно бота",
          api.fg != h_main and api.fg != 0)
    check("показ: NOACTIVATE снят, стиль в точности прежний",
          wd.show_onscreen(h_main) and api.w[h_main].ex == 0x100 | WS_EX_APPWINDOW)
    check("показ из-за края — сразу под активным окном (не на дне и не поверх)",
          api.z.index(h_main) == api.z.index(api.fg) + 1)
    api.w[h_foreign].ex |= WS_EX_TOPMOST
    api.fg = h_foreign
    wd.hide_offscreen(h_main)
    wd.show_onscreen(h_main)
    check("активное окно поверх всех → показ наверх, но не «поверх всех»",
          api.z[0] == h_main and not api.w[h_main].ex & WS_EX_TOPMOST)
    api.w[h_foreign].ex &= ~WS_EX_TOPMOST

    h_min = api.add(Win(CH, (300, 300, 1100, 900)))
    api.w[h_min].iconic = True
    check("свёрнутое: window_rect — куда развернётся",
          wd.window_rect(h_min) == (300, 300, 1100, 900))
    check("свёрнутое не считается «за краем»", wd.is_offscreen(h_min) is False)
    check("скрытие свёрнутого: развёрнуто и за краем",
          wd.hide_offscreen(h_min) is True and not api.w[h_min].iconic
          and wd.is_offscreen(h_min))
    check("…место — его обычный прямоугольник",
          wd.saved_rect(h_min) == (300, 300, 1100, 900))

    h_launch = api.add(Win(CH, (-6000, 0, -4720, 900)))  # стартовал за экраном
    wd.hide_offscreen(h_launch)
    check("окно, родившееся за экраном: места нет", wd.saved_rect(h_launch) is None)
    wd.show_onscreen(h_launch)
    r = api.w[h_launch].rect
    dw, dh = min(1280, int(1920 * 0.85)), min(900, int(1040 * 0.85))
    check("показ без места — по центру рабочей области (~1280×900, ≤85%)",
          r == ((1920 - dw) // 2, (1040 - dh) // 2,
                (1920 - dw) // 2 + dw, (1040 - dh) // 2 + dh))
    h_fb = api.add(Win(CH, (-6000, 0, -4720, 900)))
    check("fallback_rect", wd.show_onscreen(h_fb, fallback_rect=(10, 20, 810, 620))
          and api.w[h_fb].rect == (10, 20, 810, 620))
    h_hi = api.add(Win(CH, (-6000, 0, -4720, 900), dpi=144))
    wd.show_onscreen(h_hi)
    r = api.w[h_hi].rect
    check("по центру при 150%: 1920×1350 → не больше 85% области",
          (r[2] - r[0], r[3] - r[1]) == (min(1920, int(1920 * 0.85)),
                                         min(1350, int(1040 * 0.85))))
    check("window_dpi отдаёт DPI окна, неизвестно — 96",
          wd.window_dpi(h_hi) == 144 and wd.window_dpi(0xDEAD) == 96)

    check("offscreen_origin: один монитор",
          wd._offscreen_origin_for((0, 0, 1920, 1080), 1280, 900) == (-3280, 0))
    check("offscreen_origin: монитор слева",
          wd._offscreen_origin_for((-2560, -200, 4480, 1440), 1280, 900)
          == (-2560 - 1280 - 2000, -200))
    x, y = wd._offscreen_origin_for((-29000, 0, 31000, 1080), 3840, 2160)
    check("огромная стена мониторов: не -32000 и в пределах ±30000",
          x != -32000 and -30000 <= x <= 30000 and -30000 <= y <= 30000)
    check("…и окно целиком вне экрана",
          x >= -29000 + 31000 or x + 3840 <= -29000)
    api.monitors = [(-29000, 0, -26000, 1080), (0, 0, 2000, 1080)]
    h_wall = api.add(Win(CH, (100, 100, 3940, 2260)))
    wd.hide_offscreen(h_wall)
    check("скрытие при огромной стене: окно за краем",
          wd.is_offscreen(h_wall))
    check("ни одно окно не ставилось на -32000 (признак свёрнутого)",
          -32000 not in api.xs and all(-30000 <= v <= 30000 for v in api.xs))
    api.monitors = [(0, 0, 1920, 1080)]

    api.fg = h_foreign
    api.flash_stopped.clear()
    with wd.FocusGuard({CH}, settle_sec=0) as g:
        api.fg = h_2  # Chrome бота перехватил фокус
    check("FocusGuard: фокус ушёл в наблюдаемый pid → вернули",
          g.restored and api.fg == h_foreign)
    check("FocusGuard: мигание кнопок Chrome погашено", h_2 in api.flash_stopped)
    api.fg = h_main
    with wd.FocusGuard({CH}, settle_sec=0) as g:
        api.fg = h_2
    check("FocusGuard: пользователь и был в Chrome бота → не трогаем",
          not g.restored and api.fg == h_2)
    api.fg = h_foreign
    with wd.FocusGuard(lambda: [CH], settle_sec=0) as g:
        pass
    check("FocusGuard: фокус не двигался → ничего", not g.restored and api.fg == h_foreign)
    raised = False
    try:
        with wd.FocusGuard({CH}, settle_sec=0):
            api.fg = h_2
            raise ValueError("x")
    except ValueError:
        raised = True
    check("FocusGuard: исключение блока не глотает, фокус всё равно вернул",
          raised and api.fg == h_foreign)

    api.block_fg = True
    api.attached.clear()
    api.fg = h_foreign
    check("force_foreground: через AttachThreadInput", wd.force_foreground(h_2) is True
          and api.fg == h_2)
    check("…ввод отсоединён в finally", not api.attached)
    api.w[h_2].iconic = True
    api.fg = h_foreign
    check("force_foreground разворачивает свёрнутое",
          wd.force_foreground(h_2) and not api.w[h_2].iconic)
    # Находка: присоединение к потоку зависшего активного окна вешало наш поток
    api.fg = h_foreign
    api.attach_log.clear()
    api.w[h_foreign].busy = True
    wd.force_foreground(h_2)
    check("активное окно не отвечает → к его потоку НЕ присоединяемся",
          api.w[h_foreign].tid not in api.attach_log
          and api.w[h_2].tid in api.attach_log and not api.attached)
    api.w[h_foreign].busy = False
    api.attach_log.clear()
    api.fg = h_foreign
    wd.force_foreground(h_2)
    check("…отвечает → присоединяемся, как раньше",
          api.w[h_foreign].tid in api.attach_log)
    api.attach_log.clear()
    api.fg = h_2
    api.w[h_foreign].busy = True       # FocusGuard возвращает фокус занятому окну
    wd.force_foreground(h_foreign)
    check("целевое окно занято → к его потоку тоже не присоединяемся",
          api.w[h_foreign].tid not in api.attach_log and not api.attached)
    api.w[h_foreign].busy = False
    api.block_fg = False

    # Находка: мониторы разного DPI — за краем окно выросло (WM_DPICHANGED)
    # и краем вылезло на экран; повтор затирал запомненное место
    api.monitors = [(-2880, 0, 0, 1800), (0, 0, 1920, 1080)]   # ноутбук 250% слева
    api.mon_dpi = [240, 96]
    h_dpi = api.add(Win(CH, (0, 0, 1920, 1040)))
    check("скрытие на смешанном DPI: окно за краем целиком",
          wd.hide_offscreen(h_dpi) is True and wd.is_offscreen(h_dpi))
    check("…место — прежнее (до роста от DPI)",
          wd.saved_rect(h_dpi) == (0, 0, 1920, 1040))
    api.w[h_dpi].rect = (-1000, 0, 3800, 2600)    # система вернула часть на экран
    wd.hide_offscreen(h_dpi)
    check("повторное скрытие не затирает место окна, спрятанного нами",
          wd.saved_rect(h_dpi) == (0, 0, 1920, 1040) and wd.is_offscreen(h_dpi))
    check("показ — ровно на прежнее место (DPI пересчитан повтором)",
          wd.show_onscreen(h_dpi) and api.w[h_dpi].rect == (0, 0, 1920, 1040))
    api.monitors = [(0, 0, 1920, 1080)]
    api.mon_dpi = None

    # Метка владельца окна
    check("метка владельца: записать/прочитать",
          wd.set_owner_tag(h_dpi, 4321) and wd.owner_tag(h_dpi) == 4321
          and wd.owner_tag(h_2) == 0)

    wd.set_api(ExplodingWin32())
    try:
        res = (wd.hide_offscreen(5), wd.show_onscreen(5), wd.browser_windows(5),
               wd.is_offscreen(5), wd.force_foreground(5), wd.window_rect(5))
        with wd.FocusGuard({5}, settle_sec=0):
            pass
        check("бэкенд падает на всём → безопасные значения, без исключений",
              res == (False, False, [], False, False, None))
    except Exception as e:
        check(f"бэкенд падает на всём → без исключений ({e})", False)

    # ── F–N: install(force=True) поверх записывающих заглушек ──
    tmp = tempfile.mkdtemp(prefix="vpc-bw-")
    vprof, hprof = os.path.join(tmp, "v"), os.path.join(tmp, "h")
    saved = {
        "mod": {n: getattr(ba, n) for n in ("_raw_call", "open_new_tab",
                                            "set_control_mode", "_hide_pool_window",
                                            "_focus_browser_tab", "_pid_alive",
                                            "_pool_v_profile", "_pool_h_profile",
                                            "_pool_h_desired_mode")},
        "worker": {n: ba._CdpWorker.__dict__[n] for n in PATCHED_WORKER},
        "execute": ta.TaskAgent.__dict__["_execute"],
        "proc": ba._WORKER._proc,
        "hproc": ba._POOL_H_PROC,
        "hmode": ba._POOL_H_RUNNING_MODE,
        "hmodepid": ba._POOL_H_MODE_PID,
        "chats": set(ba._CONTROL_MODE_CHATS),
        "clients": dict(ba._RAW_CLIENTS),
        "bw": {n: getattr(bw, n) for n in ("_ensure_sweeper", "_schedule_sync",
                                           "_SETTLE_SEC", "_SWEEP_SEC")},
    }
    sweeper_starts = []
    try:
        ba._raw_call = fake_raw_call
        ba.open_new_tab = fake_open_new_tab
        ba.set_control_mode = fake_set_control_mode
        ba._hide_pool_window = fake_hide_pool_window
        ba._focus_browser_tab = fake_focus_browser_tab
        ba._CdpWorker._new_page_quiet = fake_new_page_quiet
        ba._CdpWorker._activate_tab_quietly = fake_activate_tab_quietly
        ta.TaskAgent._execute = fake_execute
        ba._pid_alive = lambda pid: int(pid or 0) in ALIVE
        ba._pool_v_profile = lambda: vprof
        ba._pool_h_profile = lambda: hprof
        ba._pool_h_desired_mode = lambda: H_DESIRED["mode"]
        ba._RAW_CLIENTS[ba._POOL_H] = None
        ba._RAW_CLIENTS[ba._POOL_V] = None
        ba._CONTROL_MODE_CHATS.clear()
        # Детерминизм: подметальщик и показ/скрытие — вручную и синхронно
        bw._ensure_sweeper = lambda: sweeper_starts.append(1)
        bw._schedule_sync = lambda: bw._sync_visibility()
        bw._SETTLE_SEC = 0

        api = FakeWin32()
        wd.set_api(api)
        PV, PH = 4242, 5151
        ALIVE.update({PV, PH})
        ba._WORKER._proc = FakeProc(PV)
        cv = FakeChrome(api, PV)
        CHROME["v"] = cv
        check("install(force=True) поверх заглушек", bw.install(force=True) is True)

        h_other = api.add(Win(999, (0, 0, 800, 600), cls="Notepad"))
        uw, uh = cv.add_window((50, 50, 1300, 900))   # окно пользователя
        t_yt = cv.add_tab(uw, "https://www.youtube.com/")
        api.fg = h_other

        # ── F ──
        section("F. Метка маршрута: open_new_tab → поток воркера")
        LOG.clear()
        WORKER_HINTS.clear()
        ba.open_new_tab("https://chat.example/", background=True)
        ba.open_new_tab("https://youtube.com/")
        ta.TaskAgent._execute(object(), {}, "c1", None,
                              {"url": "https://pizza.example/"}, "")
        check("background=True → «агент» в потоке воркера",
              WORKER_HINTS[0] == "agent")
        check("команда пользователя → «пользователь»", WORKER_HINTS[1] == "user")
        check("open_new_tab из шага TaskAgent._execute → «агент»",
              WORKER_HINTS[2] == "agent" and logged("execute") == [("execute", True)])
        check("штатный open_new_tab вызван с теми же аргументами",
              logged("open_new_tab")[0] == ("open_new_tab", "https://chat.example/",
                                            True, "v", False)
              and logged("open_new_tab")[2][4] is True)
        check("метки сняты, флаг агента снят", not bw._HINTS and not bw._in_task_agent())
        t1 = bw._push_hint("https://same/", "agent")
        t2 = bw._push_hint("https://same/", "user")
        check("разные маршруты одного URL → «пользователь» (штатно)",
              bw._hint_for("https://same/") == "user")
        bw._pop_hint("https://same/", t1)
        bw._pop_hint("https://same/", t2)

        ctx, fwk = Ctx(cv), FakeWorker(cv)
        OPEN_CALLS_WORKER.update(on=True, ctx=ctx, w=fwk)
        LOG.clear()
        page, quiet = ba.open_new_tab("https://youtube.com/watch")
        check("метка «пользователь», окон агента нет → штатный _new_page_quiet",
              (page, quiet) == ("ORIG-PAGE", True) and logged("new_page_quiet"))
        LOG.clear()
        ta.TaskAgent._execute(object(), {}, "c1", None,
                              {"url": "https://pizza.example/"}, "")
        res = [e for e in LOG if e[0] == "new_page_quiet"]
        agents = bw._REG.agents()
        check("метка «агент» дошла до _new_page_quiet: штатный не вызван",
              not res and len(agents) == 1)
        ap = ctx.pages[-1] if ctx.pages else None
        check("вкладка агента — в новом окне агента, навигация выполнена",
              ap is not None and ap.url == "https://pizza.example/"
              and cv.targets[ap.tid]["wid"] != uw)
        OPEN_CALLS_WORKER.update(on=False)

        # ── G ──
        section("G. _raw_call: перехват только Target.createTarget")
        LOG.clear()
        out = ba._raw_call("Runtime.evaluate", {"expression": "1+1"}, pool="v",
                           timeout=3.0)
        check("не createTarget → оригинал с теми же аргументами, один вызов",
              LOG == [("raw", "Runtime.evaluate", {"expression": "1+1"}, None, "v",
                       None, False)] and out == {"result": {"value": True}})
        LOG.clear()
        ba._raw_call("Target.createTarget", {"url": "about:blank"}, pool="v",
                     _retried=True)
        check("повтор после обрыва (_retried) — мимо перехвата",
              len(LOG) == 1 and LOG[0][1] == "Target.createTarget" and LOG[0][6])

        # Чистый лист: окно агента из F закрываем
        for wid in [w for w in cv.windows if w != uw]:
            cv.close_window(wid)
        api.fg = h_other
        LOG.clear()
        res1 = ba._raw_call("Target.createTarget",
                            {"url": "about:blank", "background": True}, pool="v")
        a1 = bw._REG.agents()
        ah = a1[0].hwnd if a1 else None
        cc = [e[2] for e in LOG if e[1] == "Target.createTarget"]
        check("пул V: первая служебная вкладка — новое окно агента",
              len(a1) == 1 and cv.targets[res1["targetId"]]["wid"] != uw)
        check("createTarget: newWindow+background и границы за экраном",
              cc and cc[-1].get("newWindow") and cc[-1].get("background")
              and cc[-1].get("left", 0) < 0)
        check("окно агента за экраном и вне панели задач (режим выключен)",
              ah and wd.is_offscreen(ah) and api.w[ah].ex & WS_EX_TOOLWINDOW)
        check("перехваченный фокус возвращён прежнему окну", api.fg == h_other)
        res2 = ba._raw_call("Target.createTarget",
                            {"url": "about:blank", "background": True}, pool="v")
        evals = [e for e in LOG if e[1] == "Runtime.evaluate"
                 and "window.open(" in e[2].get("expression", "")]
        check("вторая — в том же окне агента через window.open с userGesture",
              cv.targets[res2["targetId"]]["wid"] == cv.targets[res1["targetId"]]["wid"]
              and len(bw._REG.agents()) == 1 and evals
              and evals[-1][2].get("userGesture") is True)
        check("…связь с открывателем разорвана",
              res2["targetId"] in cv.severed)
        check("окно пользователя не тронуто",
              api.w[uh].rect == (50, 50, 1300, 900) and not wd.is_offscreen(uh))

        cv.open_mode = "elsewhere"
        n_before = len(cv.targets)
        res3 = ba._raw_call("Target.createTarget",
                            {"url": "about:blank", "background": True}, pool="v")
        check("window.open ушёл в чужое окно → та вкладка закрыта, новое окно агента",
              cv.targets[res3["targetId"]]["wid"] not in (uw,)
              and len(cv.targets) == n_before + 1
              and all(d["wid"] != uw or t == t_yt for t, d in cv.targets.items()))
        cv.open_mode = "foreign"
        n_before = len(cv.targets)
        n_agents = len(bw._REG.agents())   # window.open пробуется в каждом
        wait_defaults = bw._wait_new_target.__defaults__
        bw._wait_new_target.__defaults__ = (0.3,)  # запасной путь — по истечении ожидания
        try:
            res3b = ba._raw_call("Target.createTarget",
                                 {"url": "about:blank", "background": True}, pool="v")
        finally:
            bw._wait_new_target.__defaults__ = wait_defaults
        stray = [t for t, d in cv.targets.items() if d["wid"] == uw and t != t_yt]
        check("новая пустая вкладка не по openerId в чужом окне — НЕ закрыта "
              "(могла быть вкладкой пользователя), вкладка агента — новым окном",
              n_agents and len(stray) == n_agents
              and len(cv.targets) == n_before + n_agents + 1
              and cv.targets[res3b["targetId"]]["wid"] != uw)
        for t in stray:
            cv.targets.pop(t, None)
        cv.open_mode = "blocked"
        res4 = ba._raw_call("Target.createTarget",
                            {"url": "about:blank", "background": True}, pool="v")
        check("window.open заблокирован → запасной путь newWindow",
              res4.get("targetId") in cv.targets)
        cv.open_mode = "same"

        cv.clamp = True
        for rec in bw._REG.agents():          # окна агента закрыты
            cv.close_hwnd(rec.hwnd)
        res5 = ba._raw_call("Target.createTarget",
                            {"url": "about:blank", "background": True}, pool="v")
        h5 = cv.windows[cv.targets[res5["targetId"]]["wid"]]
        check("Chrome вернул новое окно на экран (проба: на Windows возможно) → "
              "всё равно за краем через Win32", wd.is_offscreen(h5))
        cv.clamp = False

        cv.reject_bounds = True
        for rec in bw._REG.agents():
            cv.close_hwnd(rec.hwnd)
        LOG.clear()
        res6 = ba._raw_call("Target.createTarget",
                            {"url": "about:blank", "background": True}, pool="v")
        cc = [e[2] for e in LOG if e[1] == "Target.createTarget"]
        h6 = cv.windows[cv.targets[res6["targetId"]]["wid"]]
        check("Chrome отверг границы → повтор без них, окно спрятано после",
              len(cc) == 2 and "left" in cc[0] and "left" not in cc[1]
              and wd.is_offscreen(h6))
        cv.reject_bounds = False

        # Находка: «единственное новое окно» бралось без сверки границ — Ctrl+N
        # пользователя в ту же секунду уходил за экран как окно агента
        for rec in bw._REG.agents():
            cv.close_hwnd(rec.hwnd)
        user_new = {}

        def _ctrl_n():
            cv.on_create = None
            w_, h_ = cv.add_window((400, 300, 1400, 1100))   # 1000×800, на экране
            cv.add_tab(w_, "about:blank")
            user_new["h"] = h_
            cv.appear_in = 3          # окно агента покажется чуть позже
        cv.on_create = _ctrl_n
        try:
            res7 = ba._raw_call("Target.createTarget",
                                {"url": "about:blank", "background": True}, pool="v")
        finally:
            cv.on_create, cv.appear_in = None, 0
        h7 = cv.windows[cv.targets[res7["targetId"]]["wid"]]
        uh7 = user_new.get("h")
        r7 = bw._REG.get("v", uh7) if uh7 else None
        check("Ctrl+N в ту же секунду: окно пользователя не принято за окно агента",
              uh7 is not None and (r7 is None or r7.role != "agent")
              and not wd.is_offscreen(uh7) and not api.w[uh7].ex & WS_EX_TOOLWINDOW)
        check("…окно агента опознано по границам и спрятано",
              wd.is_offscreen(h7) and bw._REG.get("v", h7) is not None
              and bw._REG.get("v", h7).role == "agent")
        cv.close_hwnd(uh7)

        # DPI: границы CDP — в DIP
        ch_hi = FakeChrome(api, 6262, scale=1.5)
        hw_hi, hh_hi = ch_hi.add_window((100, 100, 2020, 1450))
        x_dip, y_dip = bw._offscreen_dip(6262, 1280, 900)
        px, _py = wd.offscreen_origin(1920, 1350)
        check("DIP-угол за экраном при 150%: ×1.5 = физический угол",
              abs(x_dip * 1.5 - px) <= 2 and x_dip * 1.5 + 1920 <= 0)
        check("без окон пула масштаб 1.0 — DIP = физические",
              bw._offscreen_dip(None, 1280, 900) == wd.offscreen_origin(1280, 900))

        # Пул H
        ch = FakeChrome(api, PH)
        CHROME["h"] = ch
        ba._POOL_H_PROC = FakeProc(PH)
        hw0, hh0 = ch.add_window((0, 0, 1920, 1080))
        ch.add_tab(hw0)
        H_DESIRED["mode"] = "headless"
        ba._POOL_H_RUNNING_MODE = "headless"
        LOG.clear()
        params = {"url": "about:blank", "background": False, "newWindow": True}
        ba._raw_call("Target.createTarget", dict(params), pool="h")
        check("пул H headless: параметры штатные, ни одного лишнего вызова",
              LOG == [("raw", "Target.createTarget", params, None, "h", None, False)])
        H_DESIRED["mode"] = "headed"
        ba._POOL_H_RUNNING_MODE = "headed"
        LOG.clear()
        ba._raw_call("Target.createTarget", dict(params), pool="h")
        check("пул H headed (rescue): штатно, окно не прячется",
              LOG == [("raw", "Target.createTarget", params, None, "h", None, False)]
              and not any(wd.is_offscreen(h) for h in ch.windows.values()))
        H_DESIRED["mode"] = "hidden"
        ba._POOL_H_RUNNING_MODE = "hidden"
        api.fg = h_other
        LOG.clear()
        rh = ba._raw_call("Target.createTarget", dict(params), pool="h")
        cc = [e[2] for e in LOG if e[1] == "Target.createTarget"]
        nh = ch.windows[ch.targets[rh["targetId"]]["wid"]]
        check("пул H hidden: createTarget с границами за экраном",
              cc and cc[-1].get("left", 0) < 0 and cc[-1].get("width") == 1920
              and cc[-1].get("newWindow") is True)
        check("пул H hidden: новое окно за экраном и вне панели задач",
              wd.is_offscreen(nh) and api.w[nh].ex & WS_EX_TOOLWINDOW)
        check("пул H hidden: фокус возвращён", api.fg == h_other)
        check("пул H hidden: остальные окна пула тоже за экраном",
              wd.is_offscreen(hh0))
        ch.launch_pos = (-5000, 0)   # --window-position: Chrome ставит туда КАЖДОЕ окно
        rh2 = ba._raw_call("Target.createTarget", dict(params), pool="h")
        ch.launch_pos = None
        nh2 = ch.windows[ch.targets[rh2["targetId"]]["wid"]]
        check("пул H hidden: окно в --window-position (поверх границ CDP) — за "
              "экраном и вне панели задач",
              wd.is_offscreen(nh2) and api.w[nh2].ex & WS_EX_TOOLWINDOW)

        # ── H ──
        section("H. Режим управления: окна агента видны только в нём")
        agent = bw._REG.agents()[0]
        ah = agent.hwnd
        wd.hide_offscreen(ah)
        user_rect, user_ex = api.w[uh].rect, api.w[uh].ex
        h_orphan = api.add(Win(PV, (-7000, 0, -5720, 900)))  # скрыт при запуске, не учтён
        LOG.clear()
        ba.set_control_mode("c1", True)
        check("включение: штатный set_control_mode вызван",
              logged("set_control_mode") == [("set_control_mode", "c1", True)])
        check("включение: окно агента на экране и в панели задач",
              not wd.is_offscreen(ah) and not api.w[ah].ex & WS_EX_TOOLWINDOW)
        check("включение: неучтённое окно пула V за экраном — показано как окно агента",
              not wd.is_offscreen(h_orphan)
              and bw._REG.get("v", h_orphan).role == "agent")
        ba.set_control_mode("c2", True)
        ba.set_control_mode("c1", False)
        check("выключил один из двух чатов → окна агента ещё видны",
              not wd.is_offscreen(ah))
        ba.set_control_mode("c2", False)
        check("вышел последний чат → окна агента за экраном",
              wd.is_offscreen(ah) and wd.is_offscreen(h_orphan))
        check("окно пользователя не тронуто ни разу",
              api.w[uh].rect == user_rect and api.w[uh].ex == user_ex)
        check("окна пула H не тронуты режимом управления", wd.is_offscreen(nh))

        # ── I ──
        section("I. Явный подъём вкладки (_focus_browser_tab)")
        t_pz = bw._create_agent_target(PV)
        cv.targets[t_pz]["url"] = "https://pizza.example/menu"
        pz_h = cv.windows[cv.targets[t_pz]["wid"]]
        api.fg = h_other
        LOG.clear()
        check("вкладка в скрытом окне агента, режим выключен → False",
              ba._focus_browser_tab("https://pizza.example/menu") is False)
        check("…окно не показано, фокус не тронут, штатный не звался",
              wd.is_offscreen(pz_h) and api.fg == h_other
              and not logged("focus_browser_tab"))
        # Находка: штатный вызывающий уже сделал bring_to_front без стража —
        # невидимое окно агента стало активным
        api.fg = pz_h
        check("…и окно уже активно: False, активность снята, окно за экраном",
              ba._focus_browser_tab("https://pizza.example/menu") is False
              and api.fg != pz_h and api.fg != 0 and wd.is_offscreen(pz_h))
        api.fg = h_other
        check("вкладка пользователя (хост) → True, окно на переднем плане",
              ba._focus_browser_tab("https://youtube.com/") is True and api.fg == uh)
        api.w[uh].iconic = True
        api.fg = h_other
        LOG.clear()
        okf = ba._focus_browser_tab("https://www.youtube.com/")
        sb = [e for e in LOG if e[1] == "Browser.setWindowBounds"]
        check("свёрнутое окно: CDP windowState normal (без left/top) и подъём",
              okf is True and sb and sb[-1][2]["bounds"] == {"windowState": "normal"}
              and not api.w[uh].iconic and api.fg == uh)
        ba._CONTROL_MODE_CHATS.add("c1")
        check("в режиме управления: окно агента показано и поднято",
              ba._focus_browser_tab("https://pizza.example/menu") is True
              and not wd.is_offscreen(pz_h) and api.fg == pz_h)
        ba.set_control_mode("c1", False)
        check("нет такой вкладки → False",
              ba._focus_browser_tab("https://nothing.example/") is False)

        # ── J ──
        section("J. Тихое переключение вкладки (_activate_tab_quietly)")
        api.fg = h_other
        pg = Page(cv, t_pz)
        pg.on_front = lambda: setattr(api, "fg", pz_h)
        LOG.clear()
        ba._CdpWorker._activate_tab_quietly(FakeWorker(cv), pg, "https://pizza.example/")
        check("bring_to_front выполнен, фокус вернулся прежнему окну",
              pg.fronted == 1 and api.fg == h_other)
        check("окно агента осталось за экраном, штатный не звался",
              wd.is_offscreen(pz_h) and not logged("activate_tab_quietly"))
        api.fg = uh  # пользователь в окне пользователя того же Chrome
        ba._CdpWorker._activate_tab_quietly(FakeWorker(cv), pg, "https://pizza.example/")
        check("фокус из окна того же Chrome не остаётся у невидимого окна агента",
              api.fg == uh)

        # ── K ──
        section("K. Скрытие окна при запуске (_hide_pool_window)")
        PV2 = 7373
        ALIVE.add(PV2)
        ba._WORKER._proc = FakeProc(PV2)
        cv2 = FakeChrome(api, PV2)
        CHROME["v"] = cv2
        lw, lh = cv2.add_window((10, 10, 1290, 910))
        cv2.add_tab(lw)
        LOG.clear()
        ba._hide_pool_window(PV2)
        rec = bw._REG.get("v", lh)
        check("пул V вне режима: окно запуска — агента, за экраном",
              rec is not None and rec.role == "agent" and wd.is_offscreen(lh)
              and not logged("hide_pool_window"))
        check("смена Chrome пула V (новый pid) → реестр сброшен",
              bw._REG.pid("v") == PV2 and bw._REG.get("v", ah) is None)
        ba._hide_pool_window(31337)
        check("чужой pid → штатное поведение",
              logged("hide_pool_window") == [("hide_pool_window", 31337)])

        # ── L ──
        section("L. Подметальщик всплывающих окон")
        ctx2 = Ctx(cv2)
        tok = bw._push_hint("https://shop.example/", "agent")
        try:
            pshop, _q = ba._CdpWorker._new_page_quiet(FakeWorker(cv2), ctx2,
                                                       "https://shop.example/")
        finally:
            bw._pop_hint("https://shop.example/", tok)
        check("вкладка агента — в окне запуска (windowId дозаполнен)",
              cv2.targets[pshop.tid]["wid"] == lw)
        pw, ph = cv2.add_window((300, 300, 900, 900))          # попап сайта
        cv2.add_tab(pw, "https://pay.example/", opener=pshop.tid)
        xw, xh = cv2.add_window((400, 300, 1000, 900))         # Ctrl+N пользователя
        cv2.add_tab(xw, "about:blank")
        bw._LAUNCHED.pop("v", None)
        bw._sweep_once()
        check("попап из окна агента → окно агента, за экраном",
              wd.is_offscreen(ph) and bw._REG.get("v", ph).role == "agent")
        check("новое окно пользователя → «пользователь», не тронуто",
              not wd.is_offscreen(xh) and bw._REG.get("v", xh).role == "user")
        hh_new = api.add(Win(PH, (100, 100, 1000, 800)))
        bw._sweep_once()
        check("пул H hidden: новое окно — за экран", wd.is_offscreen(hh_new))
        api.w[lh].rect = (50, 50, 1330, 950)   # система вернула окно агента на экран
        bw._sweep_once()
        check("окно агента, вернувшееся на экран вне режима, — снова за краем",
              wd.is_offscreen(lh))
        api.fg = lh   # Chrome сам активировал спрятанное окно агента
        bw._sweep_once()
        check("подметальщик: невидимое окно агента стало активным → активность снята",
              api.fg != lh and wd.is_offscreen(lh))
        # HWND нового окна агента не нашёлся при создании (окно показалось позже)
        before_ids = set(cv2.windows)
        bw._PENDING_AGENT_WIDS.clear()
        orig_add = cv2.add_window
        cv2.add_window = lambda rect=(100, 100, 1380, 1000), visible=True: \
            orig_add((200, 150, 1480, 1050), visible=False)
        try:
            bw._open_new_window(PV2, "about:blank", "agent", hidden=False)
        finally:
            cv2.add_window = orig_add
        new_wid = next(w for w in cv2.windows if w not in before_ids)
        late_h = cv2.windows[new_wid]
        check("окно агента без HWND ждёт подметальщика по windowId",
              new_wid in bw._PENDING_AGENT_WIDS)
        api.w[late_h].visible = True
        bw._sweep_once()
        check("…подметальщик опознал его как окно агента и спрятал",
              bw._REG.get("v", late_h) is not None
              and bw._REG.get("v", late_h).role == "agent" and wd.is_offscreen(late_h)
              and new_wid not in bw._PENDING_AGENT_WIDS)

        # Переиспользованный pid: Chrome пула H умер, его pid занял чужой процесс
        dead_cl = type("Cl", (), {"_dead": None})()
        ba._RAW_CLIENTS[ba._POOL_H] = dead_cl
        bw._PID_CACHE["h"] = (PH, dead_cl)
        ba._POOL_H_PROC.dead = True
        ba._RAW_CLIENTS[ba._POOL_H] = None    # соединение пересобрано
        h_slack = api.add(Win(PH, (300, 200, 1300, 900)))  # «Slack» с тем же pid
        bw._LAUNCHED.pop("h", None)
        bw._sweep_once()
        check("pid умершего Chrome H занят чужим процессом → его окна не трогаем",
              not wd.is_offscreen(h_slack) and bw._pool_h_pid() is None)
        ba._POOL_H_PROC.dead = False

        # ── P ──
        section("P. Rescue пула H: режим прежнего Chrome не прячет окно капчи")
        PH2 = 6161
        ALIVE.add(PH2)
        ch2 = FakeChrome(api, PH2)
        old_hproc, old_ch = ba._POOL_H_PROC, CHROME["h"]
        rescue_proc = FakeProc(PH2)
        ba._POOL_H_PROC = rescue_proc          # Popen rescue-Chrome уже есть…
        ba._POOL_H_RUNNING_MODE = "hidden"     # …а режим ещё от прежнего Chrome
        ba._POOL_H_MODE_PID = None             # Windows: pid из SingletonLock не узнать
        bw._LAUNCHED["h"] = (rescue_proc, False, time.monotonic())  # запуск видимым
        CHROME["h"] = ch2
        rw_, rh_ = ch2.add_window((200, 100, 1400, 900))
        ch2.add_tab(rw_, "https://captcha.example/")
        H_DESIRED["mode"] = "headed"           # rescue (общий файл)
        bw._sweep_once()
        check("rescue: окно видимого Chrome подметальщик не прячет",
              not wd.is_offscreen(rh_) and bw._REG.get("h", rh_) is None)
        LOG.clear()
        hp = {"url": "about:blank", "background": False, "newWindow": True}
        ba._raw_call("Target.createTarget", dict(hp), pool="h")
        check("rescue: createTarget пула H — штатно, без границ за экраном",
              LOG == [("raw", "Target.createTarget", hp, None, "h", None, False)])
        H_DESIRED["mode"] = "hidden"   # rescue кончился, а Chrome ещё видимый
        bw._sweep_once()
        check("rescue кончился, Chrome запущен видимым → окно не прячем "
              "(перезапуск в hidden — штатный, лениво)",
              not wd.is_offscreen(rh_) and bw._REG.get("h", rh_) is None)
        bw._LAUNCHED.pop("h", None)
        ba._POOL_H_MODE_PID = PH2
        ba._POOL_H_RUNNING_MODE = "headed"     # режим, сверенный с ЭТИМ pid
        bw._sweep_once()
        check("режим, относящийся к этому pid (headed), — не прячем",
              not wd.is_offscreen(rh_))
        ba._POOL_H_MODE_PID = None
        ba._POOL_H_RUNNING_MODE = None         # Chrome соседа, режим неизвестен
        bw._sweep_once()
        check("Chrome соседа, режим неизвестен, нужен hidden → прячем (как на macOS)",
              wd.is_offscreen(rh_))
        ba._POOL_H_PROC, CHROME["h"] = old_hproc, old_ch
        ba._POOL_H_RUNNING_MODE = "hidden"

        # ── Q ──
        section("Q. Общий Chrome пула V: окна соседнего процесса бота")
        ba._WORKER._proc = FakeProc(PV2)
        CHROME["v"] = cv2
        OTHER = 8888
        ALIVE.add(OTHER)
        ow_, oh_ = cv2.add_window((-7000, 0, -6000, 700))    # спрятано соседом
        cv2.add_tab(ow_, "https://neighbor.example/")
        api.w[oh_].ex |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        api.props[oh_] = OTHER
        check("наши окна агента помечены нашим pid",
              bw._REG.agents() and all(api.props.get(r.hwnd) == os.getpid()
                                       for r in bw._REG.agents()))
        ba.set_control_mode("c1", True)
        check("наш режим управления: окно агента соседа не показано и не присвоено",
              wd.is_offscreen(oh_) and bw._REG.get("v", oh_) is None)
        check("…подъём вкладки в нём → False, окно не показано",
              ba._focus_browser_tab("https://neighbor.example/") is False
              and wd.is_offscreen(oh_))
        ba.set_control_mode("c1", False)
        bw._sweep_once()
        check("подметальщик вне режима: окно соседа не присвоено",
              bw._REG.get("v", oh_) is None and api.props[oh_] == OTHER)
        # Вкладка пользователя легла в спрятанное соседом окно (последнее активное)
        cv2.last_active = ow_
        uw2, _uh2 = cv2.add_window((500, 100, 1500, 800))
        cv2.add_tab(uw2, "https://mail.example/")
        cv2.last_active = ow_
        ctxq = Ctx(cv2)
        pq, _qq = ba._CdpWorker._new_page_quiet(FakeWorker(cv2), ctxq, "https://user.example/")
        wq = cv2.targets[pq.tid]["wid"] if getattr(pq, "tid", None) in cv2.targets else None
        check("вкладка пользователя не осталась в окне за экраном (соседа)",
              wq is not None and wq != ow_ and not wd.is_offscreen(cv2.windows[wq]))
        ALIVE.discard(OTHER)                   # сосед умер — окно ничьё
        bw._sweep_once()
        rq = bw._REG.get("v", oh_)
        check("владелец умер → окно присвоено как окно агента, метка — наша",
              rq is not None and rq.role == "agent" and api.props.get(oh_) == os.getpid())
        cv2.close_window(ow_)

        # ── M ──
        section("M. Прокси subprocess: --window-position только пулу H hidden")
        ba._CONTROL_MODE_CHATS.clear()
        cmd_v = ["chrome", "--remote-debugging-port=9222", f"--user-data-dir={vprof}",
                 "--no-first-run", "about:blank"]
        out, launch = bw._launch_args(cmd_v)
        check("пул V вне режима: команда БЕЗ --window-position (Chrome применил бы "
              "его ко всем своим окнам), запуск учтён скрытым",
              out == cmd_v and launch == ("v", True))
        ba._CONTROL_MODE_CHATS.add("c9")
        check("пул V в режиме управления: команда без изменений",
              bw._launch_args(cmd_v) == (cmd_v, ("v", False)))
        ba._CONTROL_MODE_CHATS.clear()
        cmd_h = ["chrome", "--remote-debugging-port=9223", f"--user-data-dir={hprof}",
                 "--window-size=1920,1080", "about:blank"]
        H_DESIRED["mode"] = "hidden"
        out, launch = bw._launch_args(cmd_h)
        check("пул H hidden: флаг добавлен", out[-2].startswith("--window-position=-")
              and launch == ("h", True))
        H_DESIRED["mode"] = "headed"
        check("пул H headed: без изменений", bw._launch_args(cmd_h)[0] == cmd_h)
        H_DESIRED["mode"] = "hidden"
        cmd_hl = cmd_h[:-1] + ["--headless=new", "about:blank"]
        check("пул H headless: без изменений", bw._launch_args(cmd_hl)[0] == cmd_hl)
        foreign = ["chrome", "--remote-debugging-port=9333",
                   f"--user-data-dir={os.path.join(tmp, 'personal')}", "about:blank"]
        check("чужой профиль: без изменений", bw._launch_args(foreign) == (foreign, None))
        plain = ["osascript", "-e", "beep"]
        check("не Chrome: без изменений", bw._launch_args(plain) == (plain, None))

        H_DESIRED["mode"] = "hidden"
        echo_h = [sys.executable, "-c", "import sys; print(repr(sys.argv[1:]))",
                  "--remote-debugging-port=9", f"--user-data-dir={hprof}", "about:blank"]
        p = ba.subprocess.Popen(echo_h, stdout=subprocess.PIPE, text=True)
        stdout, _ = p.communicate(timeout=60)
        argv = eval(stdout.strip())  # noqa: S307 — собственный repr списка строк
        check("настоящий Popen через прокси, пул H hidden: --window-position перед about:blank",
              argv[-1] == "about:blank" and argv[-2].startswith("--window-position="))
        check("isinstance(Popen) сохранён, запуск учтён",
              isinstance(p, subprocess.Popen) and bw._LAUNCHED["h"][0] is p)
        echo = [sys.executable, "-c", "import sys; print(repr(sys.argv[1:]))",
                "--remote-debugging-port=9", f"--user-data-dir={vprof}", "about:blank"]
        pv = ba.subprocess.Popen(echo, stdout=subprocess.PIPE, text=True)
        argv_v = eval(pv.communicate(timeout=60)[0].strip())  # noqa: S307
        check("настоящий Popen, пул V: аргументы как есть, запуск учтён",
              argv_v == echo[3:] and bw._LAUNCHED["v"][0] is pv)

        # Первое окно скрытого запуска пула V — опросом сразу после Popen
        ba._CONTROL_MODE_CHATS.clear()
        PV3, PV4 = 7474, 7575
        ALIVE.update({PV3, PV4})
        h3 = api.add(Win(PV3, (30, 30, 1310, 930), appear_in=2))  # показалось не сразу
        bw._early_launch_hide("v", FakeProc(PV3), budget=2.0)
        rec3 = bw._REG.get("v", h3)
        check("скрытый запуск пула V: первое окно за экраном сразу, как показалось "
              "(без CDP), — окно агента",
              rec3 is not None and rec3.role == "agent" and wd.is_offscreen(h3)
              and api.w[h3].ex & WS_EX_TOOLWINDOW)
        h4 = api.add(Win(PV4, (60, 60, 1340, 960)))
        ba._CONTROL_MODE_CHATS.add("c9")
        bw._early_launch_hide("v", FakeProc(PV4), budget=0.3)
        check("режим управления включили посреди запуска → окно не прячем",
              not wd.is_offscreen(h4))
        ba._CONTROL_MODE_CHATS.clear()
        dead = FakeProc(PV4)
        dead.dead = True
        t0 = time.monotonic()
        bw._early_launch_hide("v", dead, budget=5.0)
        check("Chrome вышел сразу (профиль занят) → опрос кончается сразу",
              time.monotonic() - t0 < 1.0 and not wd.is_offscreen(h4))
        bw._REG.bind_pid("v", PV2)
        real_la = bw._launch_args
        bw._launch_args = lambda args: (_ for _ in ()).throw(RuntimeError("сбой"))
        try:
            p2 = ba.subprocess.Popen(echo, stdout=subprocess.PIPE, text=True)
            argv2 = eval(p2.communicate(timeout=60)[0].strip())  # noqa: S307
        finally:
            bw._launch_args = real_la
        check("сбой разбора команды → Popen с исходными аргументами",
              argv2 == echo[3:])

        # ── N ──
        section("N. Сбой пути Windows → штатное поведение у каждого патча")
        LOG.clear()
        real = bw._create_agent_target
        bw._create_agent_target = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            ba._raw_call("Target.createTarget", {"url": "about:blank", "background": True},
                         pool="v")
        finally:
            bw._create_agent_target = real
        cc = [e for e in LOG if e[1] == "Target.createTarget"]
        check("_raw_call: сбой → оригинал с исходными параметрами, один раз",
              len(cc) == 1 and cc[0][2] == {"url": "about:blank", "background": True})
        LOG.clear()
        bw._create_agent_target = lambda *a, **k: (_ for _ in ()).throw(
            ba.RawCallTimeout("CDP Target.createTarget: ответа нет за 20с"))
        timed_out = False
        try:
            ba._raw_call("Target.createTarget", {"url": "about:blank", "background": True},
                         pool="v")
        except ba.RawCallTimeout:
            timed_out = True
        finally:
            bw._create_agent_target = real
        check("_raw_call: таймаут не повторяется (вкладка могла создаться)",
              timed_out and not [e for e in LOG if e[1] == "Target.createTarget"])

        LOG.clear()
        real = bw._push_hint
        bw._push_hint = lambda *a: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            r = ba.open_new_tab("https://x.example/", background=True)
        finally:
            bw._push_hint = real
        check("open_new_tab: сбой метки → штатный вызов и его результат",
              r == 77 and logged("open_new_tab"))

        LOG.clear()
        real = bw._pool_v_pid
        bw._pool_v_pid = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            r = ba._CdpWorker._new_page_quiet(FakeWorker(cv2), Ctx(cv2), "https://y/")
            r2 = ba._CdpWorker._activate_tab_quietly(FakeWorker(cv2), Page(cv2, "T"), "u")
        finally:
            bw._pool_v_pid = real
        check("_new_page_quiet: сбой → штатный", r == ("ORIG-PAGE", True)
              and logged("new_page_quiet"))
        check("_activate_tab_quietly: сбой → штатный", r2 == "orig-activate")

        LOG.clear()
        boom_page = Page(cv2, "T")
        boom_page.on_front = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        r3 = ba._CdpWorker._activate_tab_quietly(FakeWorker(cv2), boom_page, "u")
        check("_activate_tab_quietly: bring_to_front упал → штатный", r3 == "orig-activate")

        LOG.clear()
        real = bw._raise_tab
        bw._raise_tab = lambda url: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            r = ba._focus_browser_tab("https://z/")
        finally:
            bw._raise_tab = real
        check("_focus_browser_tab: сбой → штатный и его результат",
              r == "orig-focus" and logged("focus_browser_tab"))

        LOG.clear()
        real = bw._launch_hide
        bw._launch_hide = lambda *a: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            ba._hide_pool_window(PV2)
        finally:
            bw._launch_hide = real
        check("_hide_pool_window: сбой → штатный",
              logged("hide_pool_window") == [("hide_pool_window", PV2)])

        LOG.clear()
        bw._schedule_sync = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            ba.set_control_mode("c5", True)
            ba.set_control_mode("c5", False)
            no_exc = True
        except Exception:
            no_exc = False
        check("set_control_mode: сбой показа/скрытия не мешает штатному",
              no_exc and logged("set_control_mode") == [
                  ("set_control_mode", "c5", True), ("set_control_mode", "c5", False)])
        bw._schedule_sync = lambda: bw._sync_visibility()

        wd.set_api(ExplodingWin32())
        LOG.clear()
        try:
            r = ba._raw_call("Target.createTarget",
                             {"url": "about:blank", "background": True}, pool="v")
            check("Win32 падает на всём → вкладка всё равно создана",
                  r.get("targetId") in cv2.targets)
        except Exception as e:
            check(f"Win32 падает на всём → без исключений ({e})", False)
        wd.set_api(api)
        check("подметальщик заказан (через заглушку — тест детерминирован)",
              bool(sweeper_starts))

        # ── O ──
        section("O. Подметальщик: настоящий цикл")
        bw._ensure_sweeper = saved["bw"]["_ensure_sweeper"]
        bw._SWEEP_SEC = 0.02
        ba._CONTROL_MODE_CHATS.clear()
        H_DESIRED["mode"] = "hidden"
        bw._REG.bind_pid("v", PV2)
        _wo, hx = cv2.add_window((40, 40, 1320, 940))
        recx = bw._REG.put("v", hx, "agent")
        bw._hide_rec("v", recx)

        def sweepers():
            return [t for t in threading.enumerate()
                    if t.name == "vpc-win-sweep" and t.is_alive()]
        check("нужен: окно агента спрятано, режим выключен", bw._sweep_needed())
        bw._ensure_sweeper()
        bw._ensure_sweeper()
        first = sweepers()
        check("подметальщик один, сколько его ни заказывай", len(first) == 1)
        api.w[hx].rect = (40, 40, 1320, 940)   # система вернула окно на экран
        deadline = time.monotonic() + 3
        while not wd.is_offscreen(hx) and time.monotonic() < deadline:
            time.sleep(0.02)
        check("…и он работает: окно агента снова за экраном", wd.is_offscreen(hx))
        ba._CONTROL_MODE_CHATS.add("c1")       # больше не нужен
        H_DESIRED["mode"] = "headless"
        bw._LAUNCHED.clear()
        bw._PENDING_AGENT_WIDS.clear()
        first[0].join(timeout=3)
        check("не нужен → поток вышел сам и освободил место",
              not first[0].is_alive() and bw._SWEEPER is None)
        ba._CONTROL_MODE_CHATS.clear()
        H_DESIRED["mode"] = "hidden"
        bw._REG.put("v", hx, "agent", hidden=True)
        bw._ensure_sweeper()
        second = bw._SWEEPER
        check("снова нужен → новый поток",
              second is not None and second.is_alive() and second is not first[0])
        # Прежний поток — ВНУТРИ _sweep_once, пока идут uninstall/install и
        # новый _ensure_sweeper (тот сбрасывает _SWEEP_STOP): флаг остановки
        # прежний поток «просыпает», выйти он должен по поколению
        gate, entered = threading.Event(), threading.Event()
        real_sweep = bw._sweep_once

        def held_sweep():
            entered.set()
            gate.wait(3)
            real_sweep()
        bw._sweep_once = held_sweep
        try:
            entered.wait(3)
            bw.uninstall()
            bw.install(force=True)
            bw._REG.bind_pid("v", PV2)
            bw._REG.put("v", hx, "agent", hidden=True)
            bw._ensure_sweeper()
        finally:
            bw._sweep_once = real_sweep
            gate.set()
        second.join(timeout=3)
        time.sleep(0.1)
        check("uninstall/install посреди обхода: прежний поток вышел, работает ровно один",
              entered.is_set() and not second.is_alive() and len(sweepers()) == 1)
        bw.uninstall()
        time.sleep(0.1)
        check("uninstall останавливает подметальщика", not sweepers())
    finally:
        bw.uninstall()
        for n, v in saved["mod"].items():
            setattr(ba, n, v)
        for n, v in saved["worker"].items():
            setattr(ba._CdpWorker, n, v)
        ta.TaskAgent._execute = saved["execute"]
        ba._WORKER._proc = saved["proc"]
        ba._POOL_H_PROC = saved["hproc"]
        ba._POOL_H_RUNNING_MODE = saved["hmode"]
        ba._POOL_H_MODE_PID = saved["hmodepid"]
        ba._CONTROL_MODE_CHATS.clear()
        ba._CONTROL_MODE_CHATS.update(saved["chats"])
        ba._RAW_CLIENTS.clear()
        ba._RAW_CLIENTS.update(saved["clients"])
        for n, v in saved["bw"].items():
            setattr(bw, n, v)
        wd.set_api(None)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    section("Итог")
    check("после теста browser_actions — штатный",
          ba._raw_call is saved["mod"]["_raw_call"] and ba.subprocess is subprocess
          and not bw.is_installed())
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

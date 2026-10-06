"""Трансляция вкладки агента в веб (режим управления).

Отдельное CDP-подключение к странице пула V (Page.startScreencast) — мимо
исполнителя Playwright (_WORKER в browser_actions: одна операция за раз,
бюджеты 45/120 с): кадры не ждут кликов агента и не задерживают их.

Какую вкладку показывать — отслеживаемую вкладку чата
(ComputerControlManager.tracked_page): точное совпадение URL, затем хост;
агент перешёл на другую вкладку — трансляция переключается сама.

Кадры — JPEG base64 в том виде, в каком их прислал Chrome (без
перекодирования). Темп задаёт задержанный ack: Chrome не присылает
следующий кадр, пока не подтверждён прежний, поэтому лишнего он и не
рисует. Только чтение: ни кликов, ни ввода — смотреть, а не управлять.
"""

import json
import logging
import threading
import time
import urllib.request
from typing import Callable, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

FRAME_INTERVAL_SEC = 0.12   # не чаще ~8 кадров в секунду
# Вкладка не рисуется (фоновая, окно свёрнуто) — скринкаст молчит; тогда
# раз в столько секунд — снимок captureScreenshot, чтобы вид не застывал
STILL_FALLBACK_SEC = 2.0
TARGET_POLL_SEC = 1.5       # как часто сверять, та ли вкладка
MAX_WIDTH, MAX_HEIGHT, QUALITY = 1280, 800, 60
CONNECT_TIMEOUT_SEC = 5


def list_pages(cdp_url: str) -> Optional[List[dict]]:
    """Страницы браузера (/json/list): только вкладки с адресом подключения.
    None — браузер не отвечает (не запущен или порт занят не им)."""
    base = str(cdp_url).rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/json/list", timeout=2) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return None
    return [t for t in data if isinstance(t, dict) and t.get("type") == "page"
            and t.get("webSocketDebuggerUrl")]


def _host(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").removeprefix("www.")
    except Exception:
        return ""


def pick_target(pages: Optional[List[dict]], tracked: Optional[dict]) -> Optional[dict]:
    """Вкладка для трансляции: отслеживаемая чатом — по полному URL, затем
    по хосту; нет такой (закрыли, ещё не открывали) — первая обычная
    страница (служебные devtools://, chrome-extension:// — мимо)."""
    if not pages:
        return None
    url = str((tracked or {}).get("url") or "").rstrip("/")
    host = _host(url) or str((tracked or {}).get("host") or "").removeprefix("www.")
    if url:
        for p in pages:
            if str(p.get("url") or "").rstrip("/") == url:
                return p
    if host:
        for p in pages:
            if _host(p.get("url")) == host:
                return p
    normal = [p for p in pages if not str(p.get("url") or "").startswith(
        ("devtools://", "chrome-extension://"))]
    return normal[0] if normal else None


class ControlViewStream:
    """Трансляция для одного зрителя в своём потоке. emit(dict) получает
    кадры {"frame", "url", "title", "private"} и смену состояния
    {"status": "no_browser" | "no_tab"}. describe(url) → {"url", "private"}:
    адрес для показа (без токенов) и признак приватной страницы."""

    def __init__(self, cdp_url: str, tracked: Callable[[], Optional[dict]],
                 describe: Callable[[str], dict], emit: Callable[[dict], None]):
        self.cdp_url = cdp_url
        self.tracked = tracked
        self.describe = describe
        self.emit = emit
        self._stop = threading.Event()
        self._ws = None
        self._status: Optional[str] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="control-view",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    # ── внутреннее ─────────────────────────────────────────

    def _tracked(self) -> Optional[dict]:
        try:
            return self.tracked()
        except Exception:
            return None

    def _set_status(self, status: str) -> None:
        if self._status != status:
            self._status = status
            self.emit({"status": status})

    def _meta(self, target: dict) -> dict:
        url = str(target.get("url") or "")
        try:
            d = self.describe(url) or {}
        except Exception:
            d = {}
        return {"url": d.get("url", ""), "private": bool(d.get("private")),
                "title": str(target.get("title") or "")[:200]}

    def _run(self) -> None:
        while not self._stop.is_set():
            pages = list_pages(self.cdp_url)
            if pages is None:
                self._set_status("no_browser")
                self._stop.wait(TARGET_POLL_SEC)
                continue
            target = pick_target(pages, self._tracked())
            if target is None:
                self._set_status("no_tab")
                self._stop.wait(TARGET_POLL_SEC)
                continue
            try:
                self._session(target)
            except Exception as e:
                logger.debug(f"[ControlView] трансляция вкладки прервалась: {e}")
                self._stop.wait(1.0)

    def _session(self, target: dict) -> None:
        """Скринкаст одной вкладки — до смены вкладки агента, обрыва или
        stop(). Первый кадр — captureScreenshot: статичная страница сама
        кадров не шлёт, а зритель должен увидеть её сразу. Скринкаст молчит
        дольше STILL_FALLBACK_SEC — снова снимок (фоновая вкладка не
        рисуется); вкладку на передний план не выводим — это решает агент."""
        import websocket  # websocket-client
        # suppress_origin: Chrome отвергает websocket с Origin-заголовком
        ws = websocket.create_connection(target["webSocketDebuggerUrl"],
                                         timeout=CONNECT_TIMEOUT_SEC,
                                         suppress_origin=True)
        self._ws = ws
        meta = self._meta(target)
        self._status = "live"
        try:
            ws.send(json.dumps({"id": 1, "method": "Page.startScreencast",
                                "params": {"format": "jpeg", "quality": QUALITY,
                                           "maxWidth": MAX_WIDTH,
                                           "maxHeight": MAX_HEIGHT,
                                           "everyNthFrame": 1}}))
            ws.send(json.dumps({"id": 2, "method": "Page.captureScreenshot",
                                "params": {"format": "jpeg", "quality": QUALITY}}))
            last_check = time.monotonic()
            last_emit = 0.0
            shot_id, shot_pending, shot_ts = 2, True, time.monotonic()
            while not self._stop.is_set():
                ws.settimeout(0.5)
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    msg = None
                if isinstance(msg, dict):
                    if msg.get("method") == "Page.screencastFrame":
                        p = msg.get("params") or {}
                        wait = FRAME_INTERVAL_SEC - (time.monotonic() - last_emit)
                        if wait > 0:
                            # Задержанный ack — темп кадров у самого Chrome
                            self._stop.wait(wait)
                        self.emit({"frame": p.get("data", ""), **meta})
                        last_emit = time.monotonic()
                        ws.send(json.dumps({"id": 3, "method": "Page.screencastFrameAck",
                                            "params": {"sessionId": p.get("sessionId")}}))
                    elif msg.get("id") == shot_id and "method" not in msg:
                        shot_pending = False
                        data = (msg.get("result") or {}).get("data")
                        if data:
                            self.emit({"frame": data, **meta})
                            last_emit = time.monotonic()
                    elif msg.get("method") == "Inspector.detached":
                        return
                now = time.monotonic()
                if (not shot_pending and now - last_emit >= STILL_FALLBACK_SEC
                        and now - shot_ts >= STILL_FALLBACK_SEC):
                    shot_id += 10
                    shot_pending, shot_ts = True, now
                    ws.send(json.dumps({"id": shot_id, "method": "Page.captureScreenshot",
                                        "params": {"format": "jpeg", "quality": QUALITY}}))
                elif shot_pending and now - shot_ts > 10:
                    shot_pending = False  # ответ на снимок потерялся — не ждём вечно
                if time.monotonic() - last_check >= TARGET_POLL_SEC:
                    last_check = time.monotonic()
                    pages = list_pages(self.cdp_url)
                    if pages is None:
                        return
                    nxt = pick_target(pages, self._tracked())
                    if nxt is None or nxt.get("id") != target.get("id"):
                        return  # агент на другой вкладке — переподключаемся
                    meta = self._meta(nxt)  # адрес/заголовок после перехода
        finally:
            self._ws = None
            try:
                ws.send(json.dumps({"id": 4, "method": "Page.stopScreencast"}))
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass

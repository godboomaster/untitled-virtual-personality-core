"""Тест трансляции вкладки агента в веб (app/features/browser_stream.py).

Проверяет:
* pick_target — вкладка чата по полному URL, затем по хосту; закрыта —
  первая обычная страница (devtools:// мимо); страниц нет — None;
* вживую (нужен Chromium; путь — VPC_TEST_CHROME или стандартные места,
  иначе SKIP): свой headless Chrome на отдельном порту — кадры идут, темп
  ограничен (~8/с), адрес и признак приватности из describe; агент «перешёл»
  на другую вкладку — трансляция переключилась сама; браузер закрыт —
  статус no_browser; stop() завершает поток;
* эндпоинт /api/personas/{p}/control/view (тот же Chrome): кадры по SSE,
  режим управления погас — {"status": "off"} и конец потока; режим
  выключен с самого начала — 409.

Запуск: PYTHONPATH=. python3 scripts/test_browser_stream.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.features import browser_stream as bs  # noqa: E402

FAILS = 0


def free_port() -> int:
    # Свой порт на каждый прогон: осиротевший Chrome прошлого прогона не
    # подменит собой новый (на занятый порт новый Chrome не встаёт)
    import socket
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


def check(name, cond, detail=""):
    global FAILS
    if cond:
        print(f"  [OK] {name}")
    else:
        FAILS += 1
        print(f"  [FAIL] {name} {detail}")


print("pick_target")
pages = [
    {"id": "d", "url": "devtools://devtools/bundled/inspector.html", "webSocketDebuggerUrl": "ws://d"},
    {"id": "a", "url": "https://www.dodopizza.ru/moscow", "webSocketDebuggerUrl": "ws://a"},
    {"id": "b", "url": "https://www.youtube.com/watch?v=1", "webSocketDebuggerUrl": "ws://b"},
]
check("по полному URL", bs.pick_target(pages, {"url": "https://www.youtube.com/watch?v=1"})["id"] == "b")
check("по хосту (без www)", bs.pick_target(pages, {"url": "https://youtube.com/feed"})["id"] == "b")
check("по хосту из tracked.host", bs.pick_target(pages, {"host": "dodopizza.ru"})["id"] == "a")
check("вкладки чата нет — первая обычная, не devtools", bs.pick_target(pages, {"url": "https://x.test/"})["id"] == "a")
check("чат без вкладки — первая обычная", bs.pick_target(pages, None)["id"] == "a")
check("страниц нет — None", bs.pick_target([], {"url": "https://a.test"}) is None)
check("браузер не отвечает — list_pages None", bs.list_pages("http://127.0.0.1:9") is None)


def find_chrome():
    cands = [os.environ.get("VPC_TEST_CHROME")]
    cache = Path.home() / "Library/Caches/ms-playwright"
    if cache.is_dir():
        cands += [str(p) for p in sorted(cache.glob("chromium-*/chrome-mac*/*.app/Contents/MacOS/*"), reverse=True)]
        cands += [str(p) for p in sorted(cache.glob("chromium-*/chrome-linux*/chrome"), reverse=True)]
    cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              shutil.which("chromium") or "", shutil.which("google-chrome") or ""]
    return next((c for c in cands if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


PAGE = ("<!doctype html><meta charset=utf-8><title>{label}</title>"
        "<body style='margin:0;background:{color}'><div id=b style='width:80px;height:80px;background:#fff'></div>"
        "<script>let x=0;setInterval(()=>{{x=(x+7)%600;b.style.marginLeft=x+'px'}},30)</script>")


def serve_pages():
    # Две страницы с анимацией (кадры идут непрерывно) на своём порту
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            label, color = ("Вторая", "#604020") if self.path.startswith("/b") else ("Первая", "#204060")
            body = PAGE.format(label=label, color=color).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def cdp_json(base, path, method="GET"):
    req = urllib.request.Request(f"{base}{path}", method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode() or "null")


def activate(base, url):
    # Агент выводит свою вкладку вперёд (open_new_tab(focus=True)) — тест так же
    for p in bs.list_pages(base) or []:
        if p["url"] == url:
            urllib.request.urlopen(f"{base}/json/activate/{p['id']}", timeout=5).read()


chrome = find_chrome()
if not chrome:
    print("\n[SKIP] живой тест: Chromium не найден (VPC_TEST_CHROME)")
else:
    PORT = free_port()
    print(f"Живой тест на {Path(chrome).name}, порт {PORT}")
    web = serve_pages()
    wp = web.server_address[1]
    urls = [f"http://127.0.0.1:{wp}/a", f"http://localhost:{wp}/b"]
    prof = tempfile.mkdtemp(prefix="vpc_stream_")
    proc = subprocess.Popen([chrome, "--headless=new", f"--remote-debugging-port={PORT}",
                             f"--user-data-dir={prof}", "--no-first-run",
                             "--window-size=1280,800", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)  # закрываем всей группой
    base = f"http://127.0.0.1:{PORT}"
    srv_thread = None
    try:
        for _ in range(50):
            if bs.list_pages(base) is not None:
                break
            time.sleep(0.2)
        for u in urls:
            cdp_json(base, f"/json/new?{u}", method="PUT")
        time.sleep(1.5)
        listed = [p["url"] for p in bs.list_pages(base) or []]
        check("две страницы открыты", all(u in listed for u in urls), listed)
        activate(base, urls[0])

        tracked = {"url": urls[0]}
        got, lock = [], threading.Lock()

        def emit(item):
            with lock:
                got.append((time.monotonic(), item))

        def describe(url):
            return {"url": "shown:" + url, "private": url.endswith("/b")}

        st = bs.ControlViewStream(base, tracked=lambda: tracked, describe=describe, emit=emit)
        st.start()
        time.sleep(3.0)
        with lock:
            frames = [(t, i) for t, i in got if "frame" in i]
        check("кадры идут", len(frames) >= 8, len(frames))
        tail = frames[-8:]
        rate = (len(tail) - 1) / (tail[-1][0] - tail[0][0]) if len(tail) > 1 and tail[-1][0] > tail[0][0] else 99
        check("темп ограничен (≤ 10 кадров/с)", rate <= 10.0, f"{rate:.1f}/с")
        check("кадр — JPEG base64", bool(frames) and frames[-1][1]["frame"].startswith("/9j/"))
        check("адрес и приватность из describe", bool(frames) and frames[-1][1]["url"] == "shown:" + urls[0]
              and frames[-1][1]["private"] is False, frames[-1][1].get("url") if frames else "")
        check("заголовок вкладки", bool(frames) and frames[-1][1]["title"] == "Первая",
              frames[-1][1].get("title") if frames else "")

        tracked = {"url": urls[1]}
        activate(base, urls[1])
        time.sleep(3.5)
        with lock:
            last = [i for _t, i in got if "frame" in i][-1]
        check("агент перешёл на другую вкладку — трансляция за ним", last["title"] == "Вторая", last.get("title"))
        check("приватная страница отмечена", last["private"] is True)

        tracked = {"host": "127.0.0.1"}
        time.sleep(3.5)
        with lock:
            last = [i for _t, i in got if "frame" in i][-1]
        check("вкладка по хосту, хоть она и фоновая (запасной снимок)", last["title"] == "Первая", last.get("title"))

        # ── эндпоинт: настоящий uvicorn, SSE читаем потоком ──
        import uvicorn
        from app.api import server
        from app.api.runtime import registry
        from app.features import browser_actions as ba
        mode = {"on": True}
        registry._bots["connor"] = SimpleNamespace(
            control_mode_on=lambda c: mode["on"],
            cc_tracked_page=lambda c: {"url": urls[1]},
            cc_view_describe=lambda u: {"url": u, "private": False})
        orig_url = ba.pool_v_cdp_url
        ba.pool_v_cdp_url = lambda: base
        cfg = uvicorn.Config(server.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
        userver = uvicorn.Server(cfg)
        srv_thread = threading.Thread(target=userver.run, daemon=True)
        srv_thread.start()
        for _ in range(100):
            if userver.started:
                break
            time.sleep(0.1)
        api_port = userver.servers[0].sockets[0].getsockname()[1]
        api = f"http://127.0.0.1:{api_port}/api/personas/connor/control/view?chat_id=web_user"
        try:
            events, t0 = [], time.monotonic()
            with urllib.request.urlopen(api, timeout=20) as r:
                ctype = r.headers.get("content-type", "")
                check("эндпоинт отвечает SSE", r.status == 200 and ctype.startswith("text/event-stream"), ctype)
                for raw in r:
                    line = raw.decode().strip()
                    if line.startswith("data: "):
                        ev = json.loads(line[6:])
                        events.append(ev)
                        if sum(1 for e in events if "frame" in e) == 5:
                            mode["on"] = False  # режим погас — поток должен закрыться
                    if time.monotonic() - t0 > 20:
                        break
            check("кадры пришли по SSE", sum(1 for e in events if "frame" in e) >= 5)
            check("заголовок той вкладки, что у чата", any(e.get("title") == "Вторая" for e in events))
            check("режим погас → {status: off} и конец потока",
                  bool(events) and events[-1] == {"status": "off"} and time.monotonic() - t0 < 20, events[-1:])
            try:
                urllib.request.urlopen(api, timeout=5)
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
            check("режим выключен с начала → 409", code == 409, code)
        finally:
            ba.pool_v_cdp_url = orig_url
            registry._bots.pop("connor", None)
            userver.should_exit = True

        os.killpg(proc.pid, 15)
        proc.wait(timeout=10)
        statuses = []
        for _ in range(40):  # Chrome закрывается не мгновенно: до 8 с
            time.sleep(0.2)
            with lock:
                statuses = [i["status"] for _t, i in got if "status" in i]
            if "no_browser" in statuses:
                break
        check("браузер закрыт — статус no_browser", "no_browser" in statuses, statuses)
        st.stop()
        st._thread.join(timeout=5)
        check("stop() завершает поток", not st._thread.is_alive())
    finally:
        try:
            os.killpg(proc.pid, 9)  # и вся группа процессов Chrome
        except ProcessLookupError:
            pass
        web.shutdown()
        shutil.rmtree(prof, ignore_errors=True)

print(f"\n{'ALL OK' if FAILS == 0 else f'{FAILS} FAIL'}")
sys.exit(1 if FAILS else 0)

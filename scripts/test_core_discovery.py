"""Тест: «кто тут ядро?» — поиск ядра телефоном в локальной сети.

Ядро отвечает на широковещательный UDP-запрос (app/link/discovery.py),
телефон спрашивает классом CoreDiscovery (web/android/.../CoreDiscovery.java,
без android.* — собирается обычным javac, org.json — из кеша Gradle):

  1. ответ ядра: формат, nonce, имя, порт API только при API_HOST наружу,
     токен, порт VPC Link; молчание — короткий запрос, чужой пакет, не
     частный адрес, ядро только для этой машины; ответ короче запроса;
  2. когда поднимается: при старте — только если ядро видно из сети,
     DISCOVERY_ENABLED=0, повторный запуск, порт занят;
  3. живой сокет: запрос по UDP → ответ тому же отправителю;
  4. телефон (Java): запрос дополнен до 256 байт, три отправки, чужой nonce,
     мусор и ответ без портов отброшены, повторы склеены; против настоящего
     ответчика ядра;
  5. id ядра: постоянный (data/link/core_id), в ответе и в /api/health;
     /api/core/proof — HMAC токеном по nonce, id, адресам и порту, без
     авторизации, без токена — null, кривой nonce — 422;
  6. ядро сменило адрес (web/src/coreMove.ts в Node, заглушки модулей):
     переключение только на тот же id с верной подписью (подпись считает
     настоящий Python), не подходит токен / адрес / порт — остаёмся; адрес-имя
     и пустой токен не трогаем; VPC Link — новый адрес первым; смена сервера
     во время проверки; один поиск на два вызова.

Всё на 127.0.0.1, в сеть ничего не уходит; data/ — временный каталог.
Смена IP по-настоящему не делается: «новый адрес» — другой порт на 127.0.0.1.
Запуск: python3 -m scripts.test_core_discovery
"""

import asyncio
import glob
import hashlib
import hmac
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

_TMP = tempfile.mkdtemp(prefix="core_discovery_")
os.environ["VPC_DATA_DIR"] = _TMP
ROOT = Path(__file__).parent.parent
JAVA_SRC = ROOT / "web/android/app/src/main/java/io/vpcore/app/CoreDiscovery.java"
HARNESS = ROOT / "scripts/java/DiscoveryHarness.java"

ok = 0
failures = 0
skipped = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n{title}")


ENV_KEYS = ("API_HOST", "API_PORT", "API_TOKEN", "LINK_NAME", "DISCOVERY_ENABLED", "DISCOVERY_PORT",
            "API_ALLOWED_HOSTS")


def set_env(**kw):
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in kw.items():
        os.environ[k] = v


def request(nonce="abc123", size=256):
    body = b"VPC-DISCOVER/1 " + nonce.encode()
    return body + b" " * max(0, size - len(body))


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_answer():
    from app.link import discovery, host as link_host
    section("1. Ответ ядра")
    set_env(API_HOST="0.0.0.0", API_PORT="8123", API_TOKEN="secret", LINK_NAME="Стол Призрака")
    raw = discovery.answer(request("n0nce"), "192.168.1.20")
    reply = json.loads(raw) if raw else {}
    check("ответ — JSON протокола discover/1", reply.get("vpc") == "discover/1")
    check("nonce возвращается как был", reply.get("nonce") == "n0nce")
    check("имя — LINK_NAME", reply.get("name") == "Стол Призрака")
    check("порт API — API_PORT", reply.get("api") == 8123)
    check("токен задан — token: true", reply.get("token") is True)
    check("канала нет — link: null", reply.get("link") is None)
    check("в ответе постоянный id ядра", reply.get("id") == discovery.core_id())
    check("ответ короче запроса (не усиливает)", len(raw or b"x" * 999) < discovery.REQUEST_MIN)
    long_name = "Я" * 200
    os.environ["LINK_NAME"] = long_name
    raw_long = discovery.answer(request("x" * 32), "10.0.0.5")
    check("и с длинным именем и nonce — короче запроса", raw_long is not None and len(raw_long) < discovery.REQUEST_MIN)
    os.environ["LINK_NAME"] = "Стол Призрака"

    nonce = json.loads(discovery.answer(request("y" * 80), "10.0.0.5"))["nonce"]
    check("nonce обрезается до 32 символов", nonce == "y" * 32)
    os.environ.pop("API_TOKEN")
    check("без API_TOKEN — token: false", json.loads(discovery.answer(request(), "10.0.0.5"))["token"] is False)

    check("короткий запрос — молчание", discovery.answer(request(size=100), "192.168.1.20") is None)
    check("чужой пакет — молчание", discovery.answer(b"M-SEARCH * HTTP/1.1" + b" " * 300, "192.168.1.20") is None)
    check("публичный адрес — молчание", discovery.answer(request(), "8.8.8.8") is None)
    check("CGNAT 100.64/10 — молчание", discovery.answer(request(), "100.64.1.1") is None)
    check("не адрес — молчание", discovery.answer(request(), "garbage") is None)
    check("172.16/12 — отвечает", discovery.answer(request(), "172.20.0.3") is not None)
    check("loopback (эмулятор через 10.0.2.2) — отвечает", discovery.answer(request(), "127.0.0.1") is not None)

    set_env(API_HOST="127.0.0.1")
    check("ядро только для этой машины, канала нет — молчание", discovery.answer(request(), "192.168.1.20") is None)
    set_env(API_HOST="localhost")
    check("API_HOST=localhost — тоже молчание", discovery.answer(request(), "192.168.1.20") is None)

    link_host._host = SimpleNamespace(lan_port=8766, lan_error=None)
    try:
        reply = json.loads(discovery.answer(request(), "192.168.1.20") or b"{}")
        check("канал поднят, API закрыто — api: null, link: 8766", reply.get("api") is None and reply.get("link") == 8766)
        link_host._host = SimpleNamespace(lan_port=8766, lan_error="Address already in use")
        check("порт канала не открылся — молчание", discovery.answer(request(), "192.168.1.20") is None)
    finally:
        link_host._host = None


def test_lifecycle():
    from app.link import discovery, host as link_host
    section("2. Когда поднимается")
    calls = []
    real = discovery.ensure_started

    async def fake(bind="0.0.0.0"):
        calls.append(bind)
        return True

    discovery.ensure_started = fake
    try:
        set_env(API_HOST="127.0.0.1")
        asyncio.run(discovery.start_if_needed())
        check("ядро только для этой машины — сокет не открывается", calls == [])
        set_env(API_HOST="0.0.0.0")
        asyncio.run(discovery.start_if_needed())
        check("API_HOST=0.0.0.0 — открывается", calls == ["0.0.0.0"])
        set_env(API_HOST="127.0.0.1")
        link_host._host = SimpleNamespace(lan_port=8766, lan_error=None)
        asyncio.run(discovery.start_if_needed())
        check("канал VPC Link уже поднят — открывается", calls == ["0.0.0.0", "0.0.0.0"])
    finally:
        discovery.ensure_started = real
        link_host._host = None

    src = (ROOT / "app/link/host.py").read_text(encoding="utf-8")
    check("канал, поднятый позже (QR-код), включает ответчик", "discovery.ensure_started()" in src)
    srv = (ROOT / "app/api/server.py").read_text(encoding="utf-8")
    check("старт API зовёт start_if_needed, остановка — shutdown",
          "discovery.start_if_needed()" in srv and "discovery.shutdown()" in srv)

    async def scenario():
        port = free_udp_port()
        set_env(DISCOVERY_ENABLED="0", DISCOVERY_PORT=str(port))
        off = await discovery.ensure_started(bind="127.0.0.1")
        set_env(DISCOVERY_PORT=str(port))
        first = await discovery.ensure_started(bind="127.0.0.1")
        again = await discovery.ensure_started(bind="127.0.0.1")
        same = discovery._transport
        await discovery.shutdown()
        await asyncio.sleep(0.05)  # transport.close закрывает сокет на следующем шаге цикла
        # Порт занят чужим сокетом без SO_REUSEPORT — отказ без исключения
        busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        busy.bind(("127.0.0.1", port))
        try:
            taken = await discovery.ensure_started(bind="127.0.0.1")
        finally:
            busy.close()
            await discovery.shutdown()
        return off, first, again, same, taken

    off, first, again, same, taken = asyncio.run(scenario())
    check("DISCOVERY_ENABLED=0 — не поднимается", off is False)
    check("поднимается на заданном порту", first is True and same is not None)
    check("повторный запуск — тот же сокет", again is True)
    check("порт занят — False, без исключения", taken is False)


class Responder:
    """Настоящий ответчик ядра в своём цикле событий на 127.0.0.1."""

    def __init__(self, port):
        self.port = port
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(5)

    def _run(self):
        from app.link import discovery
        asyncio.set_event_loop(self.loop)
        self.started = self.loop.run_until_complete(discovery.ensure_started(bind="127.0.0.1"))
        self.ready.set()
        self.loop.run_forever()

    def stop(self):
        from app.link import discovery
        fut = asyncio.run_coroutine_threadsafe(discovery.shutdown(), self.loop)
        fut.result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def test_live(port):
    section("3. Живой сокет")
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.settimeout(2)
    try:
        c.sendto(request("live1"), ("127.0.0.1", port))
        data, addr = c.recvfrom(2048)
        reply = json.loads(data)
        check("ответ пришёл отправителю", addr[0] == "127.0.0.1" and reply.get("nonce") == "live1")
        check("в ответе порт API стенда", reply.get("api") == 8124)
        c.sendto(request("short", size=40), ("127.0.0.1", port))
        try:
            c.recvfrom(2048)
            silent = False
        except socket.timeout:
            silent = True
        check("короткий запрос — без ответа", silent)
    finally:
        c.close()


def json_jar():
    found = sorted(glob.glob(os.path.expanduser(
        "~/.gradle/caches/modules-2/files-2.1/org.json/json/*/*/json-*.jar")))
    return found[-1] if found else None


def build_java():
    jar = json_jar()
    if not jar or not shutil.which("javac"):
        return None, None
    out = Path(_TMP) / "classes"
    out.mkdir(exist_ok=True)
    r = subprocess.run(["javac", "-encoding", "UTF-8", "--release", "11", "-cp", jar, "-d", str(out),
                        str(JAVA_SRC), str(HARNESS)], capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        return False, jar
    return str(out), jar


def ask_java(classes, jar, port, wait_ms=900, hosts=("127.0.0.1",)):
    r = subprocess.run(["java", "-Dfile.encoding=UTF-8", "-cp", f"{classes}{os.pathsep}{jar}",
                        "io.vpcore.app.DiscoveryHarness", str(port), str(wait_ms), *hosts],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-2000:])
    return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]


class FakeCore:
    """Поддельный ответчик: записывает запросы, отвечает набором пакетов."""

    def __init__(self, replies):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.replies = replies
        self.requests = []
        self.alive = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while self.alive:
            try:
                data, addr = self.sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            self.requests.append(data)
            nonce = data[len(b"VPC-DISCOVER/1"):].strip().decode()
            for make in self.replies:
                self.sock.sendto(make(nonce), addr)

    def stop(self):
        self.alive = False
        self.thread.join(2)
        self.sock.close()


def test_phone(port):
    global skipped
    section("4. Телефон (CoreDiscovery.java)")
    classes, jar = build_java()
    if classes is None:
        print("  [SKIP] нет javac или org.json (соберите APK один раз — Gradle скачает org.json)")
        skipped += 1
        return
    check("CoreDiscovery.java собирается без Android", classes is not False)
    if classes is False:
        return

    def reply(**kw):
        return lambda nonce: json.dumps({"vpc": "discover/1", "nonce": nonce, **kw}).encode()

    fake = FakeCore([
        lambda nonce: json.dumps({"vpc": "discover/1", "nonce": "чужой", "name": "Чужой", "api": 1,
                                  "token": True, "link": None}).encode(),
        lambda nonce: b"\x00\xffnot json",
        lambda nonce: json.dumps({"vpc": "other/9", "nonce": nonce, "api": 1}).encode(),
        reply(name="Без портов", api=None, token=True, link=None),
        reply(name="Ноутбук", id="abc123def456abc123def456", api=8000, token=True, link=None),
        reply(name="", id="bad id; drop", api=None, token=False, link=8766),
    ])
    try:
        found = ask_java(classes, jar, fake.port)
    finally:
        fake.stop()
    reqs = fake.requests
    check("запрос ушёл три раза (UDP теряется)", len(reqs) == 3)
    check("запрос — 256 байт, начинается с VPC-DISCOVER/1",
          bool(reqs) and all(len(r) == 256 and r.startswith(b"VPC-DISCOVER/1 ") for r in reqs))
    nonces = {r[len(b"VPC-DISCOVER/1"):].strip() for r in reqs}
    check("nonce один на опрос, 16 hex", len(nonces) == 1 and all(len(n) == 16 for n in nonces))
    names = [f["name"] for f in found]
    check("чужой nonce, мусор, чужой протокол, ответ без портов — отброшены", "Чужой" not in names and "Без портов" not in names)
    check("повторы склеены: два ядра из трёх отправок", len(found) == 2)
    lap = next((f for f in found if f["name"] == "Ноутбук"), {})
    check("ядро: адрес отправителя, порт API, токен",
          lap.get("host") == "127.0.0.1" and lap.get("api") == 8000 and lap.get("token") is True and lap.get("link") == 0)
    check("id ядра из ответа", lap.get("id") == "abc123def456abc123def456")
    link_only = next((f for f in found if f["link"] == 8766), {})
    check("только канал: api 0, пустое имя — адрес", link_only.get("api") == 0 and link_only.get("name") == "127.0.0.1")
    check("id не из букв и цифр — пустой", link_only.get("id") == "")

    quiet = FakeCore([])
    try:
        none = ask_java(classes, jar, quiet.port, wait_ms=500)
    finally:
        quiet.stop()
    check("никто не ответил — пустой список", none == [])

    found = ask_java(classes, jar, port)
    from app.link import discovery
    check("настоящий ответчик ядра найден", len(found) == 1 and found[0].get("api") == 8124
          and found[0].get("name") == "Стенд" and found[0].get("token") is True
          and found[0].get("id") == discovery.core_id())


def test_core_id_and_proof():
    from app.link import discovery
    section("5. Id ядра и доказательство")
    first = discovery.core_id()
    path = Path(_TMP) / "link" / "core_id"
    check("id — 24 hex, записан в data/link/core_id",
          len(first) == 24 and path.read_text(encoding="utf-8") == first)
    discovery.reset_for_tests()
    check("после перезапуска — тот же id (из файла)", discovery.core_id() == first)
    path.write_text("испорчено!", encoding="utf-8")
    discovery.reset_for_tests()
    second = discovery.core_id()
    check("испорченный файл — новый id", second != first and len(second) == 24
          and path.read_text(encoding="utf-8") == second)

    set_env(API_PORT="8125", API_ALLOWED_HOSTS="Core.Lan, 10.0.2.2")
    os.environ["API_TOKEN"] = "proof-token"
    from fastapi.testclient import TestClient
    from app.api import server
    server._api_token = "proof-token"
    # Host — разрешённый: с API_ALLOWED_HOSTS ядро проверяет его (security.LocalOriginGuard)
    client = TestClient(server.app, base_url="http://127.0.0.1")
    r = client.get("/api/health")
    check("/api/health: status ok и id ядра", r.status_code == 200 and r.json() == {"status": "ok", "id": second})
    nonce = "a" * 32
    r = client.get(f"/api/core/proof?nonce={nonce}")
    body = r.json() if r.status_code == 200 else {}
    hosts = body.get("hosts") or []
    check("/api/core/proof без авторизации — 200", r.status_code == 200)
    check("адреса: интерфейсы и API_ALLOWED_HOSTS (в нижнем регистре), по порядку",
          "core.lan" in hosts and "10.0.2.2" in hosts and hosts == sorted(hosts))
    check("порт — API_PORT, id — свой", body.get("port") == 8125 and body.get("id") == second)
    msg = "|".join(["vpc-core-proof/1", second, nonce, ",".join(hosts), "8125"])
    good = hmac.new(b"proof-token", msg.encode(), hashlib.sha256).hexdigest()
    check("подпись — HMAC-SHA256 токеном по префиксу, id, nonce, адресам, порту", body.get("proof") == good)
    check("токен в ответ не попадает", "proof-token" not in r.text)
    for bad in ("short", "x" * 65, "a" * 20 + "!%3B"):
        if client.get(f"/api/core/proof?nonce={bad}").status_code != 422:
            check(f"кривой nonce {bad!r} — 422", False)
            break
    else:
        check("кривой nonce (короткий, длинный, не те символы) — 422", True)
    check("без nonce — 422", client.get("/api/core/proof").status_code == 422)
    server._api_token = ""
    check("на ядре нет токена — proof: null", client.get(f"/api/core/proof?nonce={nonce}").json().get("proof") is None)
    return second


class ProofServer:
    """Ядро на «новом адресе»: /api/core/proof считает настоящий discovery.proof."""

    def __init__(self, token, hosts, port_override=None):
        from app.link import discovery
        outer = self
        self.calls = 0

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.calls += 1
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                nonce = (q.get("nonce") or [""])[0]
                port = port_override or self.server.server_address[1]
                body = json.dumps({"id": discovery.core_id(), "hosts": hosts, "port": port,
                                   "proof": discovery.proof(token, nonce, hosts, port)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


MOVE_JS = r"""
import { registerHooks } from 'node:module';
const cfg = JSON.parse(process.env.MOVE_CFG);
const state = { url: cfg.url, token: cfg.token, link: cfg.link, setUrl: [], setLink: [], reconnect: 0,
  bg: 0, wake: 0, discover: 0, rechecks: 0, found: cfg.found, discoverFails: cfg.discoverFails };
globalThis.__s = state;
const stubs = {
  './api': `export function isNativeApp() { return true; }
    export function getApiUrl() { return globalThis.__s.url; }
    export function setApiUrl(u) { globalThis.__s.url = u; globalThis.__s.setUrl.push(u); }
    export function getApiToken() { return globalThis.__s.token; }`,
  './link/client.ts': `export function getLinkConfig() { return globalThis.__s.link; }
    export function setLinkConfig(c) { globalThis.__s.link = c; globalThis.__s.setLink.push(c); }
    export function reconnectLink() { globalThis.__s.reconnect += 1; }`,
  './apiData': `export function recheckCore() { globalThis.__s.rechecks += 1; } export function useCoreHealth() { return 'down'; }`,
  './standalone': 'export function isStandalone() { return false; }',
  './bgInbox': 'export function syncBgInbox() { globalThis.__s.bg += 1; }',
  './wakeWord': 'export function syncWakeWord() { globalThis.__s.wake += 1; }',
  './coreFinder': `export async function discoverCores() {
      globalThis.__s.discover += 1;
      await new Promise((r) => setTimeout(r, 30));
      if (globalThis.__s.discoverFails) { const e = new Error('no wifi'); e.code = 'no_wifi'; throw e; }
      if (globalThis.__s.changeUrlTo) globalThis.__s.url = globalThis.__s.changeUrlTo;
      return { found: globalThis.__s.found, nets: [] };
    }`,
  'react': 'export function useEffect() {}',
};
registerHooks({
  resolve(spec, ctx, next) {
    if (spec in stubs) return { url: 'data:text/javascript,' + encodeURIComponent(stubs[spec]), shortCircuit: true };
    if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(spec + '.ts', ctx);
    return next(spec, ctx);
  },
});
const ls = new Map(Object.entries(cfg.storage || {}));
globalThis.localStorage = { getItem: (k) => (ls.has(k) ? ls.get(k) : null), setItem: (k, v) => ls.set(k, String(v)),
  removeItem: (k) => ls.delete(k) };
const realFetch = globalThis.fetch;
state.fetches = [];
globalThis.fetch = (u, init) => { state.fetches.push(String(u)); return realFetch(u, init); };
state.changeUrlTo = cfg.changeUrlTo;
const m = await import(process.env.MOVE_MODULE);
const runs = cfg.twice ? await Promise.all([m.relocateCore(), m.relocateCore()]) : [await m.relocateCore()];
const { found: _f, ...out } = state;
console.log(JSON.stringify({ ...out, moved: runs }));
"""


def relocate(**cfg):
    env = dict(os.environ, MOVE_CFG=json.dumps(cfg), MOVE_MODULE=(ROOT / "web/src/coreMove.ts").as_uri())
    r = subprocess.run(["node", "--input-type=module", "-e", MOVE_JS], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(ROOT / "web"))
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-3000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_relocate(core_id):
    global skipped
    section("6. Ядро сменило адрес (coreMove.ts)")
    if not shutil.which("node"):
        print("  [SKIP] нет node")
        skipped += 1
        return
    old = "http://192.168.1.10:8000"
    store = {"vpc-core-id": core_id}
    good = ProofServer("tok", ["127.0.0.1", "192.168.1.10"])
    wrong_token = ProofServer("other", ["127.0.0.1"])
    relay = ProofServer("tok", ["192.168.1.77"])
    wrong_port = ProofServer("tok", ["127.0.0.1"], port_override=9)
    try:
        def me(srv, **kw):
            return {"host": "127.0.0.1", "id": core_id, "name": "Ноутбук", "api": srv.port, "token": True,
                    "link": 0, **kw}

        impostor = me(wrong_token, id="0" * 24)
        r = relocate(url=old, token="tok", storage=store, found=[impostor, me(good)])
        new = f"http://127.0.0.1:{good.port}"
        check("тот же id и верная подпись — адрес сменился", r["moved"] == [True] and r["url"] == new)
        check("чужой id даже не спрашивается", all(str(wrong_token.port) not in f for f in r["fetches"]))
        check("фоновая служба и голосовая активация — на новый адрес", r["bg"] == 1 and r["wake"] == 1)
        check("токен новому адресу не отправлялся (только nonce)",
              all("tok" not in f for f in r["fetches"]) and all("/api/core/proof?nonce=" in f for f in r["fetches"]))

        r = relocate(url=old, token="tok", storage=store, found=[me(wrong_token)])
        check("подпись не тем токеном — остаёмся", r["moved"] == [False] and r["url"] == old and not r["setUrl"])
        r = relocate(url=old, token="tok", storage=store, found=[me(relay)])
        check("подпись настоящего ядра к чужому адресу (пересылка) — остаёмся", r["moved"] == [False] and r["url"] == old)
        r = relocate(url=old, token="tok", storage=store, found=[me(wrong_port)])
        check("порт в подписи не тот — остаёмся", r["moved"] == [False] and r["url"] == old)
        r = relocate(url=f"http://127.0.0.1:{good.port}", token="tok", storage=store, found=[me(good)])
        check("ядро там же, где было, — без проверки и смены", r["moved"] == [False] and not r["fetches"])
        r = relocate(url=old, token="tok", storage={}, found=[me(good)])
        check("id ядра неизвестен — сеть не спрашивается", r["moved"] == [False] and r["discover"] == 0)
        r = relocate(url="https://core.example.com", token="tok", storage=store, found=[me(good)])
        check("адрес — имя (прокси, вне дома) — не подменяется", r["moved"] == [False] and r["url"] == "https://core.example.com")
        r = relocate(url=old, token="", storage=store, found=[me(good)])
        check("токена нет — проверить нечем, остаёмся", r["moved"] == [False] and not r["fetches"])
        r = relocate(url=old, token="tok", storage=store, found=[], discoverFails=True)
        check("телефон не в Wi-Fi — тихо, без смены", r["moved"] == [False] and r["url"] == old)
        r = relocate(url=old, token="tok", storage=store, found=[me(good)], changeUrlTo="http://192.168.1.50:8000")
        check("сервер сменили во время поиска — выбор пользователя не трогаем",
              r["moved"] == [False] and r["url"] == "http://192.168.1.50:8000")
        r = relocate(url=old, token="tok", storage=store, found=[me(good)], twice=True)
        check("два вызова подряд — один поиск", r["discover"] == 1 and r["moved"] == [True, True])

        link = {"v": 1, "k": "K", "r": "R", "n": "Ноутбук", "u": "", "l": ["192.168.1.10:8766"], "sk": "S"}
        r = relocate(url="", token="", storage=store, link=link,
                     found=[me(good, host="192.168.1.20", api=0, link=8766)])
        check("VPC Link: новый адрес первым, канал переподключается",
              r["moved"] == [True] and r["link"]["l"] == ["192.168.1.20:8766", "192.168.1.10:8766"] and r["reconnect"] == 1)
        check("VPC Link: без проверки подписью (Noise проверит ключ сам)", not r["fetches"])
        r = relocate(url="", token="", storage=store, link=link,
                     found=[me(good, host="192.168.1.10", api=0, link=8766)])
        check("VPC Link: адрес уже известен — без переподключения", r["moved"] == [False] and r["reconnect"] == 0)
        r = relocate(url="", token="", storage=store, link=link,
                     found=[me(good, id="1" * 24, host="192.168.1.20", api=0, link=8766)])
        check("VPC Link: чужой id — адрес не добавляется", r["moved"] == [False] and not r["setLink"])
    finally:
        for srv in (good, wrong_token, relay, wrong_port):
            srv.stop()


def main():
    try:
        test_answer()
        test_lifecycle()
        port = free_udp_port()
        set_env(API_HOST="0.0.0.0", API_PORT="8124", API_TOKEN="t", LINK_NAME="Стенд", DISCOVERY_PORT=str(port))
        responder = Responder(port)
        try:
            check("ответчик стенда поднялся", responder.started)
            test_live(port)
            test_phone(port)
        finally:
            responder.stop()
        core_id = test_core_id_and_proof()
        test_relocate(core_id)
    finally:
        set_env()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\nИтого: {ok - failures}/{ok} OK" + (f", пропущено {skipped}" if skipped else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

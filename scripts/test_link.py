"""Тест VPC Link — защищённого канала «телефон ↔ ноутбук»:

  A. Noise: Python-реализация (app/link/noise.py) совпадает с официальными
     тестовыми векторами (scripts/fixtures/noise_vectors.json);
  B. то же для телефона (web/src/link/noise.ts, под Node);
  C. сквозной сценарий в локальной сети: сопряжение по одноразовому секрету,
     отказ чужому ключу, устаревшему и повторному QR, неверному секрету;
     запросы к API через канал (заголовки ставит ноутбук, путь — только /api/),
     подмена кадра рвёт соединение, отвязка рвёт сессию;
  D. посредник: ноутбук только с паролем, телефон через комнату, ноутбук не в
     сети — отказ;
  E. настоящий клиент телефона (web/src/link/client.ts) под Node: сопряжение,
     JSON, загрузка FormData, стрим по частям, 1 МБ, отмена, 204, путь вне API —
     напрямую и через посредника.

Без сети: всё на 127.0.0.1, данные — во временной папке. Без Node части B и E
пропускаются.

Запуск: PYTHONPATH=. python3 scripts/test_link.py
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent
VECTORS = ROOT / "scripts" / "fixtures" / "noise_vectors.json"

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


def _node_ready() -> bool:
    return bool(shutil.which("node")) and (ROOT / "web" / "node_modules" / "@noble" / "curves").is_dir()


# ════════════ A, B. Тестовые векторы Noise ════════════

def _run_vector(v, HandshakeState):
    pattern = v["protocol_name"].split("_")[1]

    def hx(k):
        return bytes.fromhex(v[k]) if v.get(k) else None

    def psk(side):
        return bytes.fromhex(v[f"{side}_psks"][0]) if v.get(f"{side}_psks") else None

    i = HandshakeState(pattern, True, hx("init_prologue") or b"", s=hx("init_static"),
                       rs=hx("init_remote_static"), psk=psk("init"), e=hx("init_ephemeral"))
    r = HandshakeState(pattern, False, hx("resp_prologue") or b"", s=hx("resp_static"),
                       rs=hx("resp_remote_static"), psk=psk("resp"), e=hx("resp_ephemeral"))
    good, ti, tr = True, None, None
    for n, m in enumerate(v["messages"]):
        payload, ct = bytes.fromhex(m["payload"]), bytes.fromhex(m["ciphertext"])
        from_init = n % 2 == 0
        if not i.finished:
            w, rd = (i, r) if from_init else (r, i)
            got = w.write_message(payload)
            good &= got == ct and rd.read_message(got) == payload
            if i.finished:
                ti, tr = i.transport(), r.transport()
                good &= i.handshake_hash.hex() == v["handshake_hash"]
        else:
            w, rd = (ti, tr) if from_init else (tr, ti)
            got = w.encrypt(payload)
            good &= got == ct and rd.decrypt(got) == payload
    return good


def test_vectors():
    section("A. Noise (Python) — официальные тестовые векторы")
    from app.link.noise import HandshakeState, NoiseError
    vectors = json.loads(VECTORS.read_text())["vectors"]
    for v in vectors:
        check(v["protocol_name"], _run_vector(v, HandshakeState))
    # Подмена одного бита в кадре — отказ, а не мусор
    from app.link import noise
    a, b = noise.generate_private(), noise.generate_private()
    i = HandshakeState("IK", True, b"p", s=a, rs=noise.public_key(b))
    r = HandshakeState("IK", False, b"p", s=b)
    r.read_message(i.write_message(b""))
    i.read_message(r.write_message(b""))
    ti, tr = i.transport(), r.transport()
    ct = bytearray(ti.encrypt(b"hello"))
    ct[0] ^= 1
    try:
        tr.decrypt(bytes(ct))
        tampered = False
    except NoiseError:
        tampered = True
    check("подменённый кадр не расшифровывается", tampered)
    # Чужой пролог (режим «подключение» вместо «сопряжение») — рукопожатие не сходится
    i = HandshakeState("IK", True, b"vpc-link/1\x01", s=a, rs=noise.public_key(b))
    r = HandshakeState("IK", False, b"vpc-link/1\x02", s=b)
    try:
        r.read_message(i.write_message(b"x"))
        mismatch = False
    except NoiseError:
        mismatch = True
    check("разный пролог — рукопожатие отвергнуто", mismatch)


def test_vectors_js():
    section("B. Noise (телефон, noise.ts под Node) — те же векторы")
    if not _node_ready():
        print("  (пропущено — нет Node или web/node_modules)")
        return
    code = f"""
import fs from 'node:fs';
const {{ HandshakeState }} = await import({json.dumps(str(ROOT / 'web/src/link/noise.ts'))});
const V = JSON.parse(fs.readFileSync({json.dumps(str(VECTORS))}, 'utf8')).vectors;
const hx = (s) => (s ? Uint8Array.from(Buffer.from(s, 'hex')) : undefined);
const eq = (a, b) => Buffer.from(a).equals(Buffer.from(b));
for (const v of V) {{
  const p = v.protocol_name.split('_')[1];
  const i = new HandshakeState(p, true, hx(v.init_prologue) ?? new Uint8Array(), {{ s: hx(v.init_static), rs: hx(v.init_remote_static), psk: v.init_psks?.length ? hx(v.init_psks[0]) : undefined, e: hx(v.init_ephemeral) }});
  const r = new HandshakeState(p, false, hx(v.resp_prologue) ?? new Uint8Array(), {{ s: hx(v.resp_static), rs: hx(v.resp_remote_static), psk: v.resp_psks?.length ? hx(v.resp_psks[0]) : undefined, e: hx(v.resp_ephemeral) }});
  let ok = true, ti, tr;
  v.messages.forEach((m, n) => {{
    const pl = hx(m.payload) ?? new Uint8Array(), ct = hx(m.ciphertext); const fi = n % 2 === 0;
    if (!i.finished) {{ const [w, rd] = fi ? [i, r] : [r, i]; const g = w.writeMessage(pl); ok &&= eq(g, ct) && eq(rd.readMessage(g), pl);
      if (i.finished) {{ ti = i.transport(); tr = r.transport(); ok &&= Buffer.from(i.handshakeHash).toString('hex') === v.handshake_hash; }} }}
    else {{ const [w, rd] = fi ? [ti, tr] : [tr, ti]; const g = w.encrypt(pl); ok &&= eq(g, ct) && eq(rd.decrypt(g), pl); }}
  }});
  console.log(JSON.stringify({{ name: v.protocol_name, ok }}));
}}
"""
    res = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True,
                         timeout=120, cwd=ROOT / "web")
    lines = [json.loads(x) for x in res.stdout.splitlines() if x.startswith("{")]
    check("Node выполнил проверку векторов", len(lines) == 5 or print(res.stderr[-800:]))
    for x in lines:
        check(f"{x['name']} (JS)", x["ok"])


# ════════════ Тестовое API и «телефон» на Python ════════════

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


BIG = bytes((i * 7 + 3) % 251 for i in range(1_000_000))


def start_dummy_api():
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import Response, StreamingResponse

    app = FastAPI()
    state = {"slow_cancelled": 0}

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.api_route("/api/echo", methods=["GET", "POST", "PUT"])
    async def echo(request: Request):
        body = await request.body()
        return {"method": request.method, "headers": dict(request.headers), "len": len(body),
                "text": body[:200].decode("utf-8", "replace")}

    @app.get("/api/stream")
    async def stream():
        async def gen():
            for n in range(5):
                yield f"data: {n}\n\n".encode()
                await asyncio.sleep(0.2)
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/big")
    async def big():
        return Response(BIG, media_type="application/octet-stream")

    @app.get("/api/slow")
    async def slow():
        async def gen():
            try:
                yield b"start"
                await asyncio.sleep(10)
                yield b"end"
            finally:
                state["slow_cancelled"] += 1
        return StreamingResponse(gen(), media_type="text/plain")

    @app.get("/api/empty")
    async def empty():
        return Response(status_code=204)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return port, state, server


async def py_connect(url, laptop_pub, sk, mode, payload=b'{"v":1}', psk=None):
    """Телефон на Python: соединение + рукопожатие → (ws, transport, hello)."""
    from websockets.asyncio.client import connect
    from app.link import noise
    from app.link.tunnel import PROLOGUE, MODE_PAIR
    ws = await connect(url, compression=None, max_size=70_000)
    hs = noise.HandshakeState("IKpsk2" if mode == MODE_PAIR else "IK", True, PROLOGUE + bytes([mode]),
                              s=sk, rs=laptop_pub, psk=psk)
    await ws.send(bytes([mode]) + hs.write_message(payload))
    reply = await asyncio.wait_for(ws.recv(), 5)
    hello = json.loads(hs.read_message(reply) or b"{}")
    return ws, hs.transport(), hello


async def py_close_code(coro):
    """Код закрытия, если ноутбук отказал (None — не отказал)."""
    from websockets.exceptions import ConnectionClosed
    try:
        ws, _, _ = await coro
    except ConnectionClosed as e:
        return e.rcvd.code if e.rcvd else None
    try:
        await asyncio.wait_for(ws.recv(), 2)
    except ConnectionClosed as e:
        return e.rcvd.code if e.rcvd else None
    except asyncio.TimeoutError:
        return None
    return None


async def py_request(ws, t, sid, method, path, headers=None, body=b""):
    from app.link import tunnel as tn
    await ws.send(t.encrypt(tn.frame(tn.REQ, sid, json.dumps({"m": method, "p": path, "h": headers or {}}).encode())))
    if body:
        await ws.send(t.encrypt(tn.frame(tn.REQ_DATA, sid, body)))
    await ws.send(t.encrypt(tn.frame(tn.REQ_END, sid)))
    res = {"status": None, "headers": {}, "body": b"", "error": None}
    while True:
        data = t.decrypt(await asyncio.wait_for(ws.recv(), 5))
        kind, s, payload = data[0], int.from_bytes(data[1:5], "big"), data[5:]
        if s != sid:
            continue
        if kind == tn.RES:
            head = json.loads(payload)
            res["status"], res["headers"] = head["s"], head["h"]
        elif kind == tn.RES_DATA:
            res["body"] += payload
        elif kind == tn.RES_END:
            return res
        elif kind == tn.ERROR:
            res["error"] = json.loads(payload).get("e")
            return res


async def py_pair(url, offer_id, psk, laptop_pub, name="Py phone"):
    from app.link import noise
    from app.link import tunnel as tn
    sk = noise.generate_private()
    ws, t, hello = await py_connect(url, laptop_pub, sk, tn.MODE_PAIR,
                                    json.dumps({"v": 1, "offer": offer_id, "name": name}).encode(), psk)
    await ws.send(t.encrypt(tn.frame(tn.PAIR_CONFIRM, 0)))
    data = t.decrypt(await asyncio.wait_for(ws.recv(), 5))
    paired = json.loads(data[5:]) if data[0] == tn.PAIRED else {}
    return sk, ws, t, hello, paired


# ════════════ C, D, E. Сквозной сценарий ════════════

async def scenario(api_port: int, api_state: dict):
    from app.link import noise, relay, store
    from app.link import tunnel as tn
    from app.link.host import LinkHost

    ready = asyncio.Event()
    relay_task = asyncio.create_task(relay.run(0, bind="127.0.0.1", host_key="relay-pass", ready=ready))
    await asyncio.wait_for(ready.wait(), 5)
    relay_url = f"ws://127.0.0.1:{ready.port}"

    host = LinkHost(api_port=api_port, lan_port=0, relay_url=relay_url, relay_key="relay-pass",
                    api_token="tok-123")
    await host.start()
    for _ in range(100):
        if host.relay_connected:
            break
        await asyncio.sleep(0.05)
    ident = store.identity()
    laptop = ident["public"]
    lan = f"ws://127.0.0.1:{host.lan_port}/"
    peer = f"{relay_url}/v1/peer/{ident['room']}"

    section("C. Локальная сеть: сопряжение и отказы")
    offer, psk, _ = store.create_offer()
    sk, ws, t, hello, paired = await py_pair(lan, offer, psk, laptop)
    did = paired.get("id")
    check("сопряжение: ноутбук ответил PAIRED с id устройства", bool(did))
    check("устройство записано с именем", store.devices().get(did, {}).get("name") == "Py phone")
    check("приветствие ноутбука: имя, адреса, порт",
          hello.get("name") and isinstance(hello.get("lan"), list) and hello.get("port") == host.lan_port)
    r = await py_request(ws, t, 1, "GET", "/api/health")
    check("сразу после сопряжения — запросы идут", r["status"] == 200 and json.loads(r["body"]) == {"status": "ok"})
    await ws.close()

    code = await py_close_code(py_pair(lan, offer, psk, laptop))
    check("тот же QR второй раз — отказ 4410 (одноразовый)", code == 4410)

    offer2, psk2, _ = store.create_offer()
    n_before = len(store.devices())
    try:
        await py_pair(lan, offer2, os.urandom(32), laptop)
        wrong = False
    except noise.NoiseError:
        wrong = True
    except Exception:
        wrong = True
    check("неверный секрет — рукопожатие не сходится", wrong)
    check("…и устройство не записано", len(store.devices()) == n_before)

    offer3, psk3, _ = store.create_offer()
    store._offers[offer3] = (psk3, time.time() - 1)
    code = await py_close_code(py_pair(lan, offer3, psk3, laptop))
    check("истёкший QR — отказ 4410", code == 4410)

    stranger = noise.generate_private()
    code = await py_close_code(py_connect(lan, laptop, stranger, tn.MODE_CONNECT))
    check("чужой ключ без сопряжения — отказ 4403", code == 4403)

    section("C. Локальная сеть: запросы через канал")
    ws, t, hello = await py_connect(lan, laptop, sk, tn.MODE_CONNECT)
    r = await py_request(ws, t, 1, "POST", "/api/echo",
                         {"Content-Type": "application/json", "X-VPC-Link": "spoof", "Authorization": "Bearer evil",
                          "Origin": "https://evil.example", "X-Test": "1"}, b'{"a":1}')
    h = json.loads(r["body"])["headers"] if r["status"] == 200 else {}
    check("echo: 200 и тело дошло", r["status"] == 200 and json.loads(r["body"])["text"] == '{"a":1}')
    check("X-VPC-Link ставит ноутбук (подмена телефоном не проходит)", h.get("x-vpc-link") == did)
    check("Authorization — токен ядра от ноутбука, не от телефона", h.get("authorization") == "Bearer tok-123")
    check("Origin и посторонние заголовки не передаются", "origin" not in h and "x-test" not in h)
    check("Content-Type передаётся", h.get("content-type") == "application/json")
    r = await py_request(ws, t, 3, "GET", "/docs")
    check("путь вне /api/ — отказ", r["error"] == "path not allowed")
    r = await py_request(ws, t, 5, "GET", "/api/big")
    check("1 МБ по частям — байт в байт", r["status"] == 200 and r["body"] == BIG)
    r = await py_request(ws, t, 7, "POST", "/api/echo", {"Content-Type": "application/octet-stream"}, b"x" * 60_000)
    check("тело запроса 60 КБ дошло", r["status"] == 200 and json.loads(r["body"])["len"] == 60_000)
    check("ноутбук видит устройство в сети", host.online().get(did) == "lan")

    await ws.send(b"\x00" * 40)  # кадр без верного ключа
    try:
        await asyncio.wait_for(ws.recv(), 3)
        dropped = False
    except Exception:
        dropped = True
    check("мусорный кадр — ноутбук рвёт соединение", dropped)

    ws, t, _ = await py_connect(lan, laptop, sk, tn.MODE_CONNECT)
    await asyncio.sleep(0.1)
    # Как DELETE /api/link/devices/{id}: забыть ключ и порвать сессии
    store.remove_device(did)
    await host.revoke(did)
    code = await py_close_code(asyncio.sleep(0, (ws, t, None)))
    check("отвязка рвёт открытую сессию (4401)", code == 4401)

    section("D. Посредник")
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus
    try:
        async with connect(f"{relay_url}/v1/host/{ident['room']}x"):
            pass
        no_key = False
    except InvalidStatus as e:
        no_key = e.response.status_code == 401
    check("ноутбук без пароля посредника — 401", no_key)
    code = await py_close_code(py_connect(f"{relay_url}/v1/peer/NoSuchRoom0000000000", laptop, sk, tn.MODE_CONNECT))
    check("комната без ноутбука — 4404", code == 4404)
    code = await py_close_code(py_connect(peer, laptop, noise.generate_private(), tn.MODE_CONNECT))
    check("чужой ключ через посредника — тот же отказ 4403 (код доходит)", code == 4403)
    code = await py_close_code(py_connect(peer, laptop, sk, tn.MODE_CONNECT))
    check("отвязанный телефон через посредника — 4403", code == 4403)
    offer4, psk4, _ = store.create_offer()
    sk4, ws, t, hello, paired = await py_pair(peer, offer4, psk4, laptop, "Через посредника")
    check("сопряжение через посредника", bool(paired.get("id")))
    r = await py_request(ws, t, 1, "GET", "/api/big")
    check("1 МБ через посредника — байт в байт", r["status"] == 200 and r["body"] == BIG)
    check("ноутбук видит устройство через посредника", host.online().get(paired.get("id")) == "relay")
    await ws.close()

    section("E. Клиент телефона (client.ts) под Node")
    if not _node_ready():
        print("  (пропущено — нет Node или web/node_modules)")
    else:
        for mode in ("lan", "relay"):
            offer5, psk5, _ = store.create_offer()
            uri = store.pairing_uri(offer5, psk5, name="Test Mac", relay=relay_url,
                                    lan=[f"127.0.0.1:{host.lan_port}"])
            before = api_state["slow_cancelled"]
            proc = await asyncio.create_subprocess_exec(
                "node", str(ROOT / "scripts" / "link_node_client.mjs"), uri, mode,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(ROOT / "web"))
            out, err = await asyncio.wait_for(proc.communicate(), 120)
            steps = {}
            for line in out.decode().splitlines():
                if line.startswith("{"):
                    d = json.loads(line)
                    steps[d.pop("step")] = d
            if "done" not in steps:
                print(err.decode()[-1500:])
            tag = f"[{mode}]"
            p = steps.get("pair", {})
            # Имя — то, что ноутбук сообщил при подключении (важнее строки из QR)
            check(f"{tag} сопряжение по строке из QR", bool(p.get("id")) and p.get("name") == tn.laptop_name())
            hl = steps.get("health", {})
            check(f"{tag} /api/health через канал, путь — {mode}",
                  hl.get("status") == 200 and hl.get("json") == {"status": "ok"} and hl.get("state") == mode)
            e = steps.get("echo", {}).get("json", {})
            eh = e.get("headers", {})
            check(f"{tag} POST JSON: тело и заголовки", e.get("text") == '{"hello":"мир","n":1}'
                  and eh.get("x-vpc-link") == p.get("id") and "x-test" not in eh)
            u = steps.get("upload", {}).get("json", {})
            check(f"{tag} FormData: multipart с boundary, файл 200 КБ",
                  u.get("headers", {}).get("content-type", "").startswith("multipart/form-data; boundary=")
                  and u.get("len", 0) > 200_000)
            chunks = steps.get("stream", {}).get("chunks", [])
            text = "".join(c["text"] for c in chunks)
            check(f"{tag} стрим приходит по частям, а не разом",
                  text == "".join(f"data: {n}\n\n" for n in range(5)) and len(chunks) >= 3
                  and chunks[-1]["t"] - chunks[0]["t"] >= 500)
            b = steps.get("big", {})
            check(f"{tag} 1 МБ — длина и контрольная сумма", b.get("len") == len(BIG)
                  and b.get("sum") == sum(BIG) % 65521)
            a = steps.get("abort", {})
            check(f"{tag} отмена запроса — AbortError", a.get("name") == "AbortError")
            for _ in range(40):
                if api_state["slow_cancelled"] > before:
                    break
                await asyncio.sleep(0.05)
            check(f"{tag} отмена доходит до API (генератор закрыт)", api_state["slow_cancelled"] > before)
            em = steps.get("empty", {})
            check(f"{tag} 204 — без тела", em.get("status") == 204 and em.get("body") is True)
            check(f"{tag} путь вне API — ошибка, а не ответ", "error" in steps.get("outside", {}))

    await host.stop()
    relay_task.cancel()


def main():
    test_vectors()
    test_vectors_js()
    tmp = tempfile.mkdtemp(prefix="vpc-link-test-")
    os.environ["VPC_DATA_DIR"] = tmp
    from app.link import store
    store.reset_for_tests()
    port, state, server = start_dummy_api()
    try:
        asyncio.run(scenario(port, state))
    finally:
        server.should_exit = True
        store.reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

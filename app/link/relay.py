"""Посредник VPC Link: склеивает ноутбук и телефон, когда напрямую не достать.

Самодостаточный файл — на сервер копируется только он (нужен Python 3.10+ и
`pip install websockets`):

    python3 relay.py --port 8443 --host-key <пароль>          # за nginx/caddy с TLS
    python3 relay.py --port 443 --cert fullchain.pem --key privkey.pem --host-key <пароль>

Что он видит: «комнату» (случайный id ноутбука), время и размеры сообщений.
Чего не видит: содержимого — телефон и ноутбук шифруют всё сами (Noise), и
ключей у посредника нет. Подменить ноутбук он тоже не может: телефон узнаёт
ноутбук по ключу из QR-кода.

Протокол:
  /v1/host/<комната> — ноутбук (одно соединение на комнату; новое вытесняет
                       старое). Заголовок X-Relay-Key — если задан --host-key,
                       чтобы чужие не пользовались вашим сервером.
  /v1/peer/<комната> — телефон. Его сообщения уходят ноутбуку кадром
                       [DATA][sid][данные], ответы ноутбука [DATA][sid][…] —
                       телефону без заголовка. [OPEN]/[CLOSE] — подключился /
                       отключился телефон sid; [CLOSE][sid][код:2][причина]
                       от ноутбука — закрыть телефон с этим кодом (отказ в
                       сопряжении телефон должен отличать от обрыва).
  /health            — «ok» для проверки снаружи.
"""

import argparse
import asyncio
import hmac
import http
import logging
import re
import ssl

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

OPEN, DATA, CLOSE = 0x01, 0x02, 0x03
MAX_MESSAGE = 70_000
MAX_PEERS = 8
_ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_PATH_RE = re.compile(r"^/v1/(host|peer)/([^/?]+)")

logger = logging.getLogger("vpc-relay")


class Room:
    def __init__(self, host):
        self.host = host
        self.peers: dict[int, object] = {}
        self.next_sid = 1


class Relay:
    def __init__(self, host_key: str = ""):
        self.host_key = host_key
        self.rooms: dict[str, Room] = {}

    def process_request(self, connection, request):
        if request.path == "/health":
            return Response(http.HTTPStatus.OK, "OK", Headers({"Content-Type": "text/plain"}), b"ok\n")
        m = _PATH_RE.match(request.path)
        if not m or not _ROOM_RE.match(m.group(2)):
            return Response(http.HTTPStatus.NOT_FOUND, "Not Found", Headers(), b"")
        if m.group(1) == "host" and self.host_key:
            got = request.headers.get("X-Relay-Key", "")
            if not hmac.compare_digest(got.encode(), self.host_key.encode()):
                return Response(http.HTTPStatus.UNAUTHORIZED, "Unauthorized", Headers(), b"")
        return None

    async def handler(self, ws):
        role, room_id = _PATH_RE.match(ws.request.path).groups()
        if role == "host":
            await self._host(ws, room_id)
        else:
            await self._peer(ws, room_id)

    async def _host(self, ws, room_id: str):
        old = self.rooms.get(room_id)
        if old:
            await old.host.close(4409, "replaced")
        room = self.rooms[room_id] = Room(ws)
        try:
            async for msg in ws:
                if not isinstance(msg, bytes) or len(msg) < 5:
                    continue
                kind, sid = msg[0], int.from_bytes(msg[1:5], "big")
                peer = room.peers.get(sid)
                if peer is None:
                    continue
                if kind == DATA:
                    try:
                        await peer.send(msg[5:])
                    except Exception:
                        pass
                elif kind == CLOSE:
                    room.peers.pop(sid, None)
                    code, reason = 1000, "closed by host"
                    if len(msg) >= 7:
                        code = int.from_bytes(msg[5:7], "big")
                        reason = msg[7:130].decode("utf-8", "replace")
                    if not (code == 1000 or 4000 <= code < 5000):
                        code = 1000
                    await peer.close(code, reason)
        finally:
            if self.rooms.get(room_id) is room:
                self.rooms.pop(room_id, None)
            for peer in list(room.peers.values()):
                await peer.close(1001, "host offline")

    async def _peer(self, ws, room_id: str):
        room = self.rooms.get(room_id)
        if room is None:
            await ws.close(4404, "laptop offline")
            return
        if len(room.peers) >= MAX_PEERS:
            await ws.close(4429, "too many devices")
            return
        sid = room.next_sid
        room.next_sid += 1
        room.peers[sid] = ws
        tag = sid.to_bytes(4, "big")
        try:
            await room.host.send(bytes([OPEN]) + tag)
            async for msg in ws:
                if not isinstance(msg, bytes):
                    break
                await room.host.send(bytes([DATA]) + tag + msg)
        except Exception:
            pass
        finally:
            if room.peers.pop(sid, None) is not None:
                try:
                    await room.host.send(bytes([CLOSE]) + tag)
                except Exception:
                    pass


async def run(port: int, *, bind: str = "0.0.0.0", host_key: str = "", cert: str = "", key: str = "",
              ready: asyncio.Event | None = None):
    relay = Relay(host_key)
    ctx = None
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key or None)
    async with serve(relay.handler, bind, port, ssl=ctx, process_request=relay.process_request,
                     compression=None, max_size=MAX_MESSAGE + 5, ping_interval=20, ping_timeout=20) as server:
        logger.info(f"посредник VPC Link слушает {bind}:{port}{' (TLS)' if ctx else ''}")
        if ready is not None:
            ready.port = server.sockets[0].getsockname()[1]
            ready.set()
        await asyncio.Future()


def main():
    p = argparse.ArgumentParser(description="Посредник VPC Link (видит только шифртекст)")
    p.add_argument("--port", type=int, default=8443)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--host-key", default="", help="пароль для ноутбука (LINK_RELAY_KEY в .env ядра)")
    p.add_argument("--cert", default="", help="сертификат TLS (fullchain.pem)")
    p.add_argument("--key", default="", help="закрытый ключ TLS")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(run(a.port, bind=a.bind, host_key=a.host_key, cert=a.cert, key=a.key))


if __name__ == "__main__":
    main()

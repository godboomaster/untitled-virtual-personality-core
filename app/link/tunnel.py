"""Сессия VPC Link на ноутбуке: рукопожатие и HTTP API ядра поверх канала.

Соединение (WebSocket напрямую или через посредника) — это «труба» сообщений
(Pipe). Первое сообщение телефона — [режим][сообщение 1 Noise]:

  MODE_CONNECT — Noise_IK: телефон знает ключ ноутбука, ноутбук пускает только
                 сопряжённые ключи;
  MODE_PAIR    — Noise_IKpsk2: плюс одноразовый секрет из QR-кода; устройство
                 записывается, когда телефон подтвердил, что секрет знает
                 (первый кадр после рукопожатия — PAIR_CONFIRM).

Дальше каждое сообщение — зашифрованный кадр [тип][поток uint32][тело].
Телефон шлёт запрос (REQ + REQ_DATA… + REQ_END), ноутбук передаёт его в
API на 127.0.0.1 и стримит ответ (RES, RES_DATA…, RES_END или ERROR);
CANCEL — телефон больше не ждёт (обрыв стрима, AbortController).
Тот же протокол на телефоне — web/src/link/client.ts.
"""

import asyncio
import json
import logging
import os
import socket
from typing import Optional, Protocol

import httpx

from app.link import noise, store

logger = logging.getLogger(__name__)

PROLOGUE = b"vpc-link/1"
MODE_CONNECT = 0x01
MODE_PAIR = 0x02

REQ, REQ_DATA, REQ_END = 0x01, 0x02, 0x03
RES, RES_DATA, RES_END = 0x04, 0x05, 0x06
CANCEL, ERROR = 0x07, 0x08
PING, PONG = 0x09, 0x0A
PAIR_CONFIRM, PAIRED = 0x0B, 0x0C

CHUNK = 32 * 1024
MAX_BODY = 64 * 1024 * 1024  # картинки комнаты — до 40 МБ одним запросом
MAX_STREAMS = 64
HANDSHAKE_TIMEOUT = 15.0

# Заголовки запроса, которые телефон может передать API; остальные (Host,
# Origin, Cookie, Authorization…) ставит ноутбук сам
_REQ_HEADERS = {"content-type", "accept", "accept-language", "cache-control", "last-event-id",
                "if-none-match", "if-modified-since"}
_RES_HEADERS = {"content-type", "cache-control", "content-disposition", "etag", "last-modified"}

# Закрытие соединения: код → причина (видна телефону)
CLOSE_BAD = 4400
CLOSE_UNKNOWN = 4403
CLOSE_OFFER = 4410
CLOSE_REVOKED = 4401


class Pipe(Protocol):
    label: str

    async def recv(self) -> Optional[bytes]: ...  # None — соединение закрыто

    async def send(self, data: bytes) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


def frame(kind: int, stream: int, body: bytes = b"") -> bytes:
    return bytes([kind]) + stream.to_bytes(4, "big") + body


def laptop_name() -> str:
    name = os.getenv("LINK_NAME") or socket.gethostname()
    return store.clean_name(name.removesuffix(".local"))


class Session:
    """Одно подключённое устройство."""

    def __init__(self, pipe: Pipe, client: httpx.AsyncClient, *, api_token: str = "",
                 lan_port: int = 0, on_open=None, on_close=None):
        self.pipe = pipe
        self.client = client
        self.api_token = api_token
        self.lan_port = lan_port
        self.on_open = on_open
        self.on_close = on_close
        self.device: Optional[str] = None
        self.transport: Optional[noise.Transport] = None
        self._send_lock = asyncio.Lock()
        self._streams: dict[int, dict] = {}

    # ── рукопожатие ──

    async def _handshake(self) -> bool:
        first = await asyncio.wait_for(self.pipe.recv(), HANDSHAKE_TIMEOUT)
        if not first or len(first) < 2 or first[0] not in (MODE_CONNECT, MODE_PAIR):
            await self.pipe.close(CLOSE_BAD, "bad hello")
            return False
        mode = first[0]
        ident = store.identity()
        hs = noise.HandshakeState("IK" if mode == MODE_CONNECT else "IKpsk2", False,
                                  PROLOGUE + bytes([mode]), s=ident["private"])
        try:
            hello = json.loads(hs.read_message(first[1:]) or b"{}")
        except (noise.NoiseError, ValueError):
            await self.pipe.close(CLOSE_BAD, "handshake")
            return False
        phone = hs.rs
        offer = None
        if mode == MODE_CONNECT:
            self.device = store.find_device(phone)
            if not self.device:
                logger.info(f"[Link] {self.pipe.label}: ключ не сопряжён — отказ")
                await self.pipe.close(CLOSE_UNKNOWN, "not paired")
                return False
        else:
            offer = str(hello.get("offer") or "")
            psk = store.offer_psk(offer)
            if psk is None:
                await self.pipe.close(CLOSE_OFFER, "pairing code expired")
                return False
            hs.set_psk(psk)
        reply = {"v": 1, "name": laptop_name(), "lan": store.lan_addresses(), "port": self.lan_port}
        await self.pipe.send(hs.write_message(json.dumps(reply).encode()))
        self.transport = hs.transport()
        if mode == MODE_PAIR:
            # Секрет знает только тот, кто видел QR: первый кадр должен
            # расшифроваться и быть подтверждением
            data = await asyncio.wait_for(self.pipe.recv(), HANDSHAKE_TIMEOUT)
            try:
                body = self.transport.decrypt(data or b"")
            except noise.NoiseError:
                body = b""
            if not body or body[0] != PAIR_CONFIRM or not store.consume_offer(offer):
                await self.pipe.close(CLOSE_OFFER, "pairing failed")
                return False
            self.device = store.add_device(phone, hello.get("name"))
            await self._send(PAIRED, 0, json.dumps({"id": self.device}).encode())
        return True

    # ── основной цикл ──

    async def run(self) -> None:
        try:
            if not await self._handshake():
                return
        except (asyncio.TimeoutError, noise.NoiseError, ConnectionError) as e:
            logger.debug(f"[Link] {self.pipe.label}: рукопожатие не удалось: {e}")
            await self.pipe.close(CLOSE_BAD, "handshake")
            return
        logger.info(f"[Link] устройство {self.device} подключено ({self.pipe.label})")
        if self.on_open:
            self.on_open(self)
        try:
            while True:
                data = await self.pipe.recv()
                if data is None:
                    break
                try:
                    body = self.transport.decrypt(data)
                except noise.NoiseError:
                    logger.warning(f"[Link] {self.pipe.label}: кадр не расшифровался — разрыв")
                    break
                if len(body) < 5:
                    break
                await self._on_frame(body[0], int.from_bytes(body[1:5], "big"), body[5:])
        finally:
            for st in self._streams.values():
                task = st.get("task")
                if task:
                    task.cancel()
            self._streams.clear()
            await self.pipe.close()
            if self.on_close:
                self.on_close(self)
            logger.info(f"[Link] устройство {self.device} отключилось")

    async def _send(self, kind: int, stream: int, body: bytes = b"") -> None:
        # Шифрование и отправка — под одним замком: номер nonce должен
        # совпасть с порядком, в котором телефон получит кадры
        async with self._send_lock:
            await self.pipe.send(self.transport.encrypt(frame(kind, stream, body)))

    async def _on_frame(self, kind: int, stream: int, body: bytes) -> None:
        if kind == PING:
            await self._send(PONG, stream, body)
        elif kind == REQ:
            if stream in self._streams or len(self._streams) >= MAX_STREAMS:
                await self._send(ERROR, stream, b'{"e":"too many streams"}')
                return
            try:
                head = json.loads(body)
            except ValueError:
                await self._send(ERROR, stream, b'{"e":"bad request"}')
                return
            self._streams[stream] = {"head": head, "body": bytearray(), "task": None}
        elif kind == REQ_DATA:
            st = self._streams.get(stream)
            if st is None or st["task"]:
                return
            st["body"] += body
            if len(st["body"]) > MAX_BODY:
                self._streams.pop(stream, None)
                await self._send(ERROR, stream, b'{"e":"request body too large"}')
        elif kind == REQ_END:
            st = self._streams.get(stream)
            if st is not None and not st["task"]:
                st["task"] = asyncio.create_task(self._forward(stream, st))
        elif kind == CANCEL:
            st = self._streams.pop(stream, None)
            if st and st["task"]:
                st["task"].cancel()

    async def _forward(self, stream: int, st: dict) -> None:
        head = st["head"]
        method = str(head.get("m") or "GET").upper()
        path = str(head.get("p") or "")
        # Канал ведёт только в API ядра: путь от корня, без схемы и хоста
        if not path.startswith("/api/") or "\r" in path or "\n" in path or "#" in path:
            await self._send(ERROR, stream, b'{"e":"path not allowed"}')
            self._streams.pop(stream, None)
            return
        headers = {k: str(v) for k, v in (head.get("h") or {}).items() if k.lower() in _REQ_HEADERS}
        headers["X-VPC-Link"] = self.device or ""
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        try:
            async with self.client.stream(method, path, headers=headers,
                                          content=bytes(st["body"]) or None) as resp:
                res_headers = {k: v for k, v in resp.headers.items() if k.lower() in _RES_HEADERS}
                await self._send(RES, stream, json.dumps({"s": resp.status_code, "h": res_headers}).encode())
                async for chunk in resp.aiter_bytes():
                    for i in range(0, len(chunk), CHUNK):
                        await self._send(RES_DATA, stream, chunk[i:i + CHUNK])
            await self._send(RES_END, stream)
            store.touch_device(self.device)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[Link] {method} {path}: {e}")
            try:
                await self._send(ERROR, stream, json.dumps({"e": str(e)[:200]}).encode())
            except Exception:
                pass
        finally:
            self._streams.pop(stream, None)

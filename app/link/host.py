"""Сторона ноутбука VPC Link: принимает телефоны и передаёт их запросы в API.

Два входа, оба ведут в одну и ту же сессию (tunnel.Session):
  - локальная сеть: WebSocket на 0.0.0.0:LINK_PORT. Порт открыт в сеть, но
    без ключа из сопряжения дальше рукопожатия никто не пройдёт; само API по-
    прежнему слушает только 127.0.0.1;
  - посредник (LINK_RELAY_URL): ноутбук сам держит исходящее соединение,
    поэтому ни проброса портов, ни белого IP не нужно. Посредник склеивает его
    с телефоном по «комнате» и видит только шифртекст (app/link/relay.py).

Настройки (.env): LINK_ENABLED (1 — включить; по умолчанию включается сам,
когда есть сопряжённые устройства или открыт QR-код), LINK_PORT (8766),
LINK_RELAY_URL (wss://…), LINK_RELAY_KEY (пароль посредника для ноутбука),
LINK_NAME (имя ноутбука в приложении).
"""

import asyncio
import logging
import os
from typing import Optional

import httpx

from app.link import store
from app.link.tunnel import CLOSE_REVOKED, Session

logger = logging.getLogger(__name__)

# Кадры между ноутбуком и посредником (у телефона — сырые сообщения)
RELAY_OPEN, RELAY_DATA, RELAY_CLOSE = 0x01, 0x02, 0x03
MAX_WS_MESSAGE = 70_000


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


class _WsPipe:
    """Телефон напрямую: одно WebSocket-соединение."""

    def __init__(self, ws, label: str):
        self.ws = ws
        self.label = label

    async def recv(self) -> Optional[bytes]:
        try:
            msg = await self.ws.recv()
        except Exception:
            return None
        return msg if isinstance(msg, bytes) else None

    async def send(self, data: bytes) -> None:
        await self.ws.send(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self.ws.close(code, reason)
        except Exception:
            pass


class _RelayPipe:
    """Телефон через посредника: поток sid внутри соединения ноутбука."""

    def __init__(self, host: "LinkHost", ws, sid: int):
        self.host = host
        self.ws = ws
        self.sid = sid
        self.label = f"relay#{sid}"
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.closed = False

    async def recv(self) -> Optional[bytes]:
        if self.closed and self.queue.empty():
            return None
        return await self.queue.get()

    async def send(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionError("relay stream closed")
        await self.ws.send(bytes([RELAY_DATA]) + self.sid.to_bytes(4, "big") + data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        self.queue.put_nowait(None)
        try:
            # Код и причина доходят до телефона: «не сопряжён» ≠ обрыв связи
            await self.ws.send(bytes([RELAY_CLOSE]) + self.sid.to_bytes(4, "big")
                               + code.to_bytes(2, "big") + reason.encode()[:120])
        except Exception:
            pass


class LinkHost:
    def __init__(self, *, api_port: int, lan_port: int, relay_url: str = "", relay_key: str = "",
                 api_token: str = ""):
        self.api_port = api_port
        self.lan_port = lan_port
        self.relay_url = relay_url.rstrip("/")
        self.relay_key = relay_key
        self.api_token = api_token
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port}",
                                        timeout=httpx.Timeout(None, connect=5.0))
        self.sessions: set[Session] = set()
        self.lan_error: Optional[str] = None
        self.relay_connected = False
        self.relay_error: Optional[str] = None
        self._lan_server = None
        self._relay_task: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()

    # ── жизненный цикл ──

    async def start(self) -> None:
        from websockets.asyncio.server import serve
        try:
            self._lan_server = await serve(self._on_lan, "0.0.0.0", self.lan_port, compression=None,
                                           max_size=MAX_WS_MESSAGE, ping_interval=20, ping_timeout=20)
            self.lan_error = None
            if not self.lan_port:  # порт 0 — любой свободный (тесты)
                self.lan_port = self._lan_server.sockets[0].getsockname()[1]
            logger.info(f"[Link] жду телефон в локальной сети на порту {self.lan_port}")
        except OSError as e:
            self.lan_error = str(e)
            logger.warning(f"[Link] порт {self.lan_port} занят или недоступен: {e}")
        if self.relay_url:
            self._relay_task = asyncio.create_task(self._relay_loop())

    async def stop(self) -> None:
        if self._relay_task:
            self._relay_task.cancel()
        if self._lan_server:
            self._lan_server.close()
        for s in list(self.sessions):
            await s.pipe.close(1001, "shutdown")
        await self.client.aclose()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _session(self, pipe) -> Session:
        return Session(pipe, self.client, api_token=self.api_token, lan_port=self.lan_port,
                       on_open=self.sessions.add, on_close=self.sessions.discard)

    async def revoke(self, device: str) -> None:
        # Устройство отвязано — его открытые соединения рвутся сразу
        for s in [s for s in self.sessions if s.device == device]:
            await s.pipe.close(CLOSE_REVOKED, "unpaired")

    def online(self) -> dict[str, str]:
        """id устройства → через что подключено сейчас."""
        return {s.device: ("relay" if s.pipe.label.startswith("relay") else "lan")
                for s in self.sessions if s.device}

    # ── локальная сеть ──

    async def _on_lan(self, ws) -> None:
        addr = ws.remote_address[0] if ws.remote_address else "?"
        await self._session(_WsPipe(ws, f"lan {addr}")).run()

    # ── посредник ──

    async def _relay_loop(self) -> None:
        from websockets.asyncio.client import connect
        url = f"{self.relay_url}/v1/host/{store.identity()['room']}"
        headers = {"X-Relay-Key": self.relay_key} if self.relay_key else None
        backoff = 1.0
        while True:
            pipes: dict[int, _RelayPipe] = {}
            try:
                async with connect(url, additional_headers=headers, compression=None,
                                   max_size=MAX_WS_MESSAGE + 5, ping_interval=20,
                                   ping_timeout=20, open_timeout=15) as ws:
                    self.relay_connected, self.relay_error, backoff = True, None, 1.0
                    logger.info(f"[Link] на связи с посредником {self.relay_url}")
                    async for msg in ws:
                        if not isinstance(msg, bytes) or len(msg) < 5:
                            continue
                        kind, sid = msg[0], int.from_bytes(msg[1:5], "big")
                        if kind == RELAY_OPEN:
                            pipe = pipes[sid] = _RelayPipe(self, ws, sid)
                            self._spawn(self._session(pipe).run())
                        elif kind == RELAY_DATA and sid in pipes:
                            try:
                                pipes[sid].queue.put_nowait(msg[5:])
                            except asyncio.QueueFull:
                                await pipes.pop(sid).close()
                        elif kind == RELAY_CLOSE and sid in pipes:
                            p = pipes.pop(sid)
                            p.closed = True
                            p.queue.put_nowait(None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.relay_error = str(e)[:200]
                logger.info(f"[Link] посредник недоступен ({e}); повтор через {backoff:.0f} с")
            finally:
                self.relay_connected = False
                for p in pipes.values():
                    p.closed = True
                    p.queue.put_nowait(None)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ── Один хост на процесс API ─────────────────────────────────────────

_host: Optional[LinkHost] = None
_host_lock = asyncio.Lock()


def configured_relay() -> str:
    return (os.getenv("LINK_RELAY_URL") or "").strip()


def lan_port() -> int:
    return _env_int("LINK_PORT", 8766)


def current() -> Optional[LinkHost]:
    return _host


async def ensure_started() -> LinkHost:
    """Поднять хост, если ещё не поднят (QR-код открыт, есть устройства)."""
    global _host
    async with _host_lock:
        if _host is None:
            host = LinkHost(api_port=_env_int("API_PORT", 8000), lan_port=lan_port(),
                            relay_url=configured_relay(), relay_key=os.getenv("LINK_RELAY_KEY", ""),
                            api_token=os.getenv("API_TOKEN", ""))
            await host.start()
            _host = host
        return _host


async def start_if_needed() -> None:
    # При старте API: канал нужен, если включён явно или уже есть устройства
    flag = (os.getenv("LINK_ENABLED") or "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return
    if flag in ("1", "true", "yes", "on") or store.devices():
        await ensure_started()


async def shutdown() -> None:
    global _host
    if _host is not None:
        await _host.stop()
        _host = None

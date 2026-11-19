"""Поиск ядра в локальной сети: телефон спрашивает «кто тут ядро VPC?».

Приложение на телефоне шлёт широковещательный UDP-пакет «VPC-DISCOVER/1
<nonce>» на порт DISCOVERY_PORT (8767), ядро отвечает отправителю коротким
JSON: имя компьютера, порт API, если оно открыто в сеть (API_HOST не
loopback), задан ли API_TOKEN и порт VPC Link, если канал поднят. Так
телефон находит ядро за секунду на любом порту и в сети любой ширины — без
перебора адресов (CoreFinderPlugin.find — запасной путь для старых ядер).

Сокет поднимается, только когда ядро и так видно из сети (API_HOST наружу
или работает VPC Link): ядро «только для этой машины» в сети о себе не
объявляет. Отвечаем только частным адресам и только на запрос не короче
REQUEST_MIN байт — ответ короче запроса, так что порт не годится для
усиления атак. DISCOVERY_ENABLED=0 — выключить совсем; DISCOVERY_PORT —
другой порт (тесты; телефон спрашивает только 8767).

Ядро переехало (DHCP выдал компьютеру другой адрес) — телефон находит его тем
же запросом по постоянному id (core_id, он же в /api/health). Назваться чужим
id может кто угодно в сети, поэтому прежде чем слать новому адресу токен,
телефон просит доказательство (proof): HMAC токеном по своему nonce, id и
адресам, на которых ядро себя знает, — без токена его не подделать, а
пересланный от настоящего ядра не подойдёт к чужому адресу.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
from typing import Optional

from app.core.paths import data_dir

logger = logging.getLogger(__name__)

MAGIC = b"VPC-DISCOVER/1"
PROTO = "discover/1"
DEFAULT_PORT = 8767
# Запрос дополняется пробелами до этой длины — ответ (имя ≤ 40 символов,
# порты, флаги) заведомо короче
REQUEST_MIN = 256
NONCE_MAX = 32

PROOF_PREFIX = "vpc-core-proof/1"

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_id_lock = threading.Lock()
_core_id: Optional[str] = None


def core_id() -> str:
    """Постоянный id этого ядра (data/link/core_id): телефон узнаёт по нему своё
    ядро на новом адресе."""
    global _core_id
    with _id_lock:
        if _core_id:
            return _core_id
        path = data_dir() / "link" / "core_id"
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if not (16 <= len(text) <= 64 and text.isalnum()):
            text = secrets.token_hex(12)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            except OSError as e:
                logger.warning(f"[Discovery] id ядра не сохранён: {e}")
        _core_id = text
        return _core_id


def reset_for_tests() -> None:
    global _core_id
    _core_id = None


def known_hosts() -> list[str]:
    """Адреса, по которым телефон может видеть это ядро: IPv4 интерфейсов и
    имена из API_ALLOWED_HOSTS (обратный прокси, Docker, эмулятор)."""
    from app.link import store
    extra = [h.strip().lower() for h in (os.getenv("API_ALLOWED_HOSTS") or "").split(",") if h.strip()]
    return sorted(set(store.lan_addresses()) | set(extra))


def proof(token: str, nonce: str, hosts: list[str], port: int) -> Optional[str]:
    """HMAC-SHA256 токеном по «префикс|id|nonce|адреса|порт»; без токена — None."""
    if not token:
        return None
    msg = "|".join([PROOF_PREFIX, core_id(), nonce, ",".join(hosts), str(port)])
    return hmac.new(token.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def enabled() -> bool:
    return (os.getenv("DISCOVERY_ENABLED") or "").strip().lower() not in ("0", "false", "no", "off")


def port() -> int:
    return _env_int("DISCOVERY_PORT", DEFAULT_PORT)


def api_open() -> bool:
    """API слушает не только эту машину — телефон может прийти по адресу."""
    return (os.getenv("API_HOST") or "127.0.0.1").strip().lower() not in _LOOPBACK


def _link_port() -> Optional[int]:
    try:
        from app.link import host as link_host
        h = link_host.current()
    except Exception:
        return None
    return h.lan_port if h is not None and not h.lan_error else None


def answer(data: bytes, source_ip: str) -> Optional[bytes]:
    """Ответ на датаграмму; None — молчать (не запрос, чужая сеть, нечего сказать)."""
    if len(data) < REQUEST_MIN or not data.startswith(MAGIC):
        return None
    try:
        if not ipaddress.ip_address(source_ip.split("%")[0]).is_private:
            return None
    except ValueError:
        return None
    nonce = data[len(MAGIC):].strip()[:NONCE_MAX].decode("ascii", "replace")
    api = _env_int("API_PORT", 8000) if api_open() else None
    link = _link_port()
    if api is None and link is None:
        return None
    from app.link.tunnel import laptop_name
    reply = {"vpc": PROTO, "nonce": nonce, "id": core_id(), "name": laptop_name(), "api": api,
             "token": bool(os.getenv("API_TOKEN")), "link": link}
    return json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode()


class _Responder(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            reply = answer(data, addr[0])
        except Exception as e:
            logger.debug(f"[Discovery] ответ не собран: {e}")
            return
        if reply and self.transport is not None:
            self.transport.sendto(reply, addr)


_transport: Optional[asyncio.DatagramTransport] = None
_lock = asyncio.Lock()


async def ensure_started(bind: str = "0.0.0.0") -> bool:
    """Поднять ответчик (повторный вызов — ничего). False — выключен или порт занят."""
    global _transport
    if not enabled():
        return False
    async with _lock:
        if _transport is not None:
            return True
        loop = asyncio.get_running_loop()
        kwargs = {"local_addr": (bind, port()), "family": socket.AF_INET}
        try:
            # Два ядра на одной машине (стенд рядом с рабочим) — широковещательный
            # запрос получат оба; на Windows reuse_port нет
            try:
                _transport, _ = await loop.create_datagram_endpoint(_Responder, reuse_port=True, **kwargs)
            except (ValueError, AttributeError, NotImplementedError):
                _transport, _ = await loop.create_datagram_endpoint(_Responder, **kwargs)
        except OSError as e:
            logger.warning(f"[Discovery] порт {port()}/udp занят или недоступен: {e}")
            return False
        logger.info(f"[Discovery] отвечаю телефонам на «кто тут ядро?» на порту {port()}/udp")
        return True


async def start_if_needed() -> None:
    # При старте API: только если ядро и так открыто в сеть
    if api_open() or _link_port() is not None:
        await ensure_started()


async def shutdown() -> None:
    global _transport
    if _transport is not None:
        _transport.close()
        _transport = None

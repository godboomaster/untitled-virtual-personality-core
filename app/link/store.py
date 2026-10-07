"""Состояние VPC Link на ноутбуке (data/link/, файлы с правами 0600):

  identity.json — закрытый ключ ноутбука и «комната» у посредника;
  devices.json  — сопряжённые устройства: открытый ключ, имя, когда виделись.

Предложения сопряжения (одноразовый секрет из QR-кода) живут только в памяти
и истекают через OFFER_TTL_SEC: перезапуск ядра отменяет показанный QR.
"""

import base64
import hashlib
import json
import logging
import re
import secrets
import socket
import threading
import time

from app.core.atomic_io import atomic_write_json, load_json_safe
from app.core.paths import data_dir
from app.link import noise

logger = logging.getLogger(__name__)

OFFER_TTL_SEC = 600
_SEEN_WRITE_EVERY_SEC = 60  # last_seen на диск — не чаще (опрос идёт каждые секунды)
_NAME_MAX = 40

_lock = threading.Lock()
_offers: dict[str, tuple[bytes, float]] = {}
_identity: dict | None = None


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _dir():
    d = data_dir() / "link"
    d.mkdir(parents=True, exist_ok=True)
    return d


def identity() -> dict:
    """{"private": bytes, "public": bytes, "room": str} — создаётся при первом обращении."""
    global _identity
    with _lock:
        if _identity is not None:
            return _identity
        path = _dir() / "identity.json"
        raw = load_json_safe(path, default=None, label="Link identity") if path.exists() else None
        if not (isinstance(raw, dict) and raw.get("private") and raw.get("room")):
            raw = {"private": noise.generate_private().hex(), "room": secrets.token_urlsafe(16),
                   "created": time.time()}
            atomic_write_json(path, raw)
        private = bytes.fromhex(raw["private"])
        _identity = {"private": private, "public": noise.public_key(private), "room": raw["room"]}
        return _identity


def device_id(public: bytes) -> str:
    return hashlib.sha256(public).hexdigest()[:12]


def clean_name(name) -> str:
    text = "".join(ch for ch in str(name or "") if ch.isprintable()).strip()
    return text[:_NAME_MAX] or "Телефон"


def _devices_path():
    return _dir() / "devices.json"


def _load_devices() -> dict:
    path = _devices_path()
    data = load_json_safe(path, default={}, label="Link devices") if path.exists() else {}
    return data if isinstance(data, dict) else {}


def devices() -> dict:
    """id → {"public": hex, "name", "paired_at", "last_seen"}."""
    with _lock:
        return _load_devices()


def find_device(public: bytes) -> str | None:
    did = device_id(public)
    with _lock:
        d = _load_devices().get(did)
    return did if d and d.get("public") == public.hex() else None


def add_device(public: bytes, name: str) -> str:
    did = device_id(public)
    with _lock:
        data = _load_devices()
        now = time.time()
        data[did] = {"public": public.hex(), "name": clean_name(name), "paired_at": now, "last_seen": now}
        atomic_write_json(_devices_path(), data)
    logger.info(f"[Link] сопряжено устройство {did} ({clean_name(name)})")
    return did


def remove_device(did: str) -> bool:
    with _lock:
        data = _load_devices()
        if did not in data:
            return False
        data.pop(did)
        atomic_write_json(_devices_path(), data)
    logger.info(f"[Link] устройство {did} отвязано")
    return True


def touch_device(did: str) -> None:
    with _lock:
        data = _load_devices()
        d = data.get(did)
        if not d or time.time() - float(d.get("last_seen") or 0) < _SEEN_WRITE_EVERY_SEC:
            return
        d["last_seen"] = time.time()
        atomic_write_json(_devices_path(), data)


# ── Предложения сопряжения ──────────────────────────────────────────

def create_offer() -> tuple[str, bytes, float]:
    """Новое предложение: (id, секрет psk, когда истекает). Прежние — отменяются:
    на экране всегда один действующий QR-код."""
    offer_id, psk = secrets.token_urlsafe(9), secrets.token_bytes(32)
    expires = time.time() + OFFER_TTL_SEC
    with _lock:
        _offers.clear()
        _offers[offer_id] = (psk, expires)
    return offer_id, psk, expires


def offer_psk(offer_id: str) -> bytes | None:
    with _lock:
        item = _offers.get(str(offer_id))
        if not item:
            return None
        psk, expires = item
        if time.time() > expires:
            _offers.pop(str(offer_id), None)
            return None
        return psk


def consume_offer(offer_id: str) -> bool:
    # Одноразово: телефон, подтвердивший сопряжение, гасит QR-код
    with _lock:
        return _offers.pop(str(offer_id), None) is not None


def offer_active() -> float | None:
    """Когда истекает действующее предложение (None — нет)."""
    with _lock:
        now = time.time()
        for offer_id, (_, expires) in list(_offers.items()):
            if expires < now:
                _offers.pop(offer_id, None)
        return max((e for _, e in _offers.values()), default=None)


def cancel_offers() -> None:
    with _lock:
        _offers.clear()


# ── Адреса в локальной сети ─────────────────────────────────────────

# Туннели и виртуальные интерфейсы (VPN, контейнеры, виртуалки, AirDrop):
# с телефона в той же сети Wi-Fi до них не достать, а в QR-коде и на
# телефоне им делать нечего
_VIRTUAL_IF = re.compile(
    r"^(lo|utun|tun|tap|ppp|ipsec|wg|gif|stf|awdl|llw|bridge|vmnet|vboxnet|docker|veth|br-|virbr|zt|"
    r"tailscale|anpi|ap\d)|vpn|virtual|vethernet|loopback|hyper-v|wireguard|openvpn|wintun",
    re.IGNORECASE)


def lan_addresses() -> list[str]:
    """IPv4-адреса этой машины на физических интерфейсах (Wi-Fi, Ethernet)."""
    try:
        import psutil
        stats = psutil.net_if_stats()
        found = []
        for name, addrs in psutil.net_if_addrs().items():
            if _VIRTUAL_IF.search(name) or not (stats.get(name) and stats[name].isup):
                continue
            for a in addrs:
                if a.family == socket.AF_INET and not a.address.startswith(("127.", "0.", "169.254.")):
                    found.append(a.address)
        return found
    except Exception:
        pass
    # Без psutil — адреса по имени хоста (без разбора интерфейсов)
    try:
        ips = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        ips = []
    return [ip for ip in ips if not ip.startswith(("127.", "0.", "169.254."))]


def pairing_uri(offer_id: str, psk: bytes, *, name: str, relay: str, lan: list[str]) -> str:
    """Строка для QR-кода: всё, что нужно телефону, чтобы найти ноутбук и
    убедиться, что говорит именно с ним."""
    ident = identity()
    payload = {"v": 1, "k": b64u(ident["public"]), "r": ident["room"], "o": offer_id,
               "p": b64u(psk), "n": name, "u": relay, "l": lan}
    return "vpclink:" + b64u(json.dumps(payload, separators=(",", ":")).encode())


def reset_for_tests() -> None:
    global _identity
    with _lock:
        _identity = None
        _offers.clear()

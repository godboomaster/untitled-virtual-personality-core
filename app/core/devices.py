"""Устройства ядра: Raspberry Pi с камерой, потом что угодно со своими
возможностями (лампа, моторы, датчики).

Как Gemma на телефоне (app/core/phone_llm): ядро не может само обратиться к
устройству — устройство приходит к нему с API_TOKEN, как приложение:
POST /api/devices/hello — кто оно (id, имя, как к нему обращаются в чате) и
что умеет (capabilities: «camera.snapshot», …); дальше держит долгий запрос
GET /api/devices/{id}/commands и возвращает итог команды
POST /api/devices/{id}/result. Ход персоны зовёт command() из своего потока
и ждёт итог до таймаута.

Реестр (имя, имена, возможности) — в <data>/devices.json: после перезапуска
ядро узнаёт обращение к устройству и честно отвечает «не на связи», пока
оно не придёт снова. Очередь команд — в памяти процесса: команда, которую
устройство не взяло до таймаута вызывающего, снимается, итог снятой —
выбрасывается.
"""

import asyncio
import json
import logging
import threading
import time
import uuid
from collections import deque
from typing import Awaitable, Callable, Optional

from app.core.atomic_io import atomic_write_text
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

# Предел ожидания долгого опроса на ядре (устройство шлёт свой wait)
MAX_WAIT_SEC = 60.0
# Устройство опрашивало не раньше стольких секунд назад — на связи: оно шлёт
# wait=25, следующий запрос — сразу за ответом, плюс запас на сеть и команду
ONLINE_SEC = 40.0
_MAX_DEVICES = 16
# Проверка остановки ядра во время ожидания (как в inbox)
_STOP_POLL_SEC = 1.0
_FILE = "devices.json"


class DeviceError(Exception):
    """Команда не выполнена. code: unknown | unsupported | offline | timeout | failed;
    detail — что сообщило само устройство (для failed)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


_lock = threading.Lock()
# id → {id, name, names, capabilities, registered}; None — ещё не прочитан с диска
_devices: Optional[dict[str, dict]] = None
# id → {seen, waiting}: только в памяти, после перезапуска все не на связи
_seen: dict[str, dict] = {}
_queues: dict[str, deque] = {}
_cmds: dict[str, dict] = {}
# Ожидающие опросы устройства: id → {(их event loop, событие «пришла команда»)}
_waiters: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}
_stop_check: Optional[Callable[[], bool]] = None


def set_stop_check(fn: Optional[Callable[[], bool]]) -> None:
    """Признак «ядро останавливается» для долгого опроса (app/main.py)."""
    global _stop_check
    _stop_check = fn


def _stopping() -> bool:
    try:
        return bool(_stop_check and _stop_check())
    except Exception:
        return False


def _path():
    return data_dir() / _FILE


def _registry() -> dict[str, dict]:
    # Под _lock. Реестр читается с диска один раз за процесс
    global _devices
    if _devices is None:
        _devices = {}
        try:
            raw = json.loads(_path().read_text(encoding="utf-8"))
            for d in raw.get("devices", []):
                if isinstance(d, dict) and d.get("id"):
                    _devices[d["id"]] = d
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"[Devices] {_FILE} не прочитан: {e}")
    return _devices


def _save() -> None:
    # Под _lock
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(_path(), json.dumps(
            {"devices": list(_registry().values())}, ensure_ascii=False, indent=1))
    except Exception as e:
        logger.warning(f"[Devices] {_FILE} не записан: {e}")


def _online_locked(device_id: str, now: float) -> bool:
    s = _seen.get(device_id)
    return bool(s) and (s.get("waiting") or now - s["seen"] < ONLINE_SEC)


def _public(d: dict, now: float) -> dict:
    s = _seen.get(d["id"])
    return {"id": d["id"], "name": d["name"], "names": list(d.get("names") or []),
            "capabilities": list(d.get("capabilities") or []),
            "online": _online_locked(d["id"], now),
            "seen_sec": round(now - s["seen"]) if s else None}


def register(device_id: str, name: str, names: list[str], capabilities: list[str]) -> dict:
    """Устройство представилось (при каждом своём запуске и переподключении)."""
    now = time.time()
    clean_names = []
    for n in [name, *names]:
        n = " ".join(str(n).split()).lower()
        if n and n not in clean_names:
            clean_names.append(n)
    caps = sorted({c for c in capabilities if c})
    with _lock:
        reg = _registry()
        old = reg.get(device_id)
        rec = {"id": device_id, "name": name.strip(), "names": clean_names,
               "capabilities": caps,
               "registered": old.get("registered", now) if old else now}
        if old != rec:
            if old is None and len(reg) >= _MAX_DEVICES:
                oldest = min(reg.values(), key=lambda d: (_seen.get(d["id"]) or {}).get("seen", 0))
                reg.pop(oldest["id"], None)
            reg[device_id] = rec
            _save()
        _seen[device_id] = {"seen": now, "waiting": False}
        out = _public(rec, now)
    logger.info(f"[Devices] «{rec['name']}» ({device_id}) на связи: {', '.join(caps) or 'без возможностей'}")
    return out


def forget(device_id: str) -> bool:
    with _lock:
        if _registry().pop(device_id, None) is None:
            return False
        _save()
        _seen.pop(device_id, None)
        for cmd in _queues.pop(device_id, ()):
            _cmds.pop(cmd["id"], None)
    return True


def list_devices() -> list[dict]:
    now = time.time()
    with _lock:
        return [_public(d, now) for d in _registry().values()]


def with_capability(capability: str) -> list[dict]:
    """Устройства (и на связи, и нет), которые умеют capability."""
    return [d for d in list_devices() if capability in d["capabilities"]]


def online(device_id: str) -> bool:
    with _lock:
        return _online_locked(device_id, time.time())


def _wake(device_id: str) -> None:
    # Под _lock: команду кладут из потоков хода, ждут — на event loop API
    for loop, event in list(_waiters.get(device_id, ())):
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            _waiters[device_id].discard((loop, event))  # loop закрыт


def _drop(cmd: dict) -> None:
    with _lock:
        _cmds.pop(cmd["id"], None)
        try:
            _queues.get(cmd["device"], deque()).remove(cmd)
        except ValueError:
            pass  # уже взята устройством


def command(device_id: str, action: str, args: Optional[dict] = None,
            timeout: float = 20.0) -> dict:
    """Команда устройству → его итог {ok, data, image, error}. Ждёт в
    вызывающем потоке до timeout; не вышло — DeviceError."""
    now = time.time()
    with _lock:
        dev = _registry().get(device_id)
        if dev is None:
            raise DeviceError("unknown")
        if action not in (dev.get("capabilities") or []):
            raise DeviceError("unsupported")
        if not _online_locked(device_id, now):
            raise DeviceError("offline")
        cmd = {"id": uuid.uuid4().hex[:16], "device": device_id, "action": action,
               "args": dict(args or {}), "created": now, "taken": False,
               "done": threading.Event(), "result": None}
        _cmds[cmd["id"]] = cmd
        _queues.setdefault(device_id, deque()).append(cmd)
        _wake(device_id)
    t0 = time.time()
    if not cmd["done"].wait(timeout):
        _drop(cmd)
        logger.info(f"[Devices] {device_id}: {action} без итога за {timeout:.0f} с "
                    f"({'взята' if cmd['taken'] else 'не взята'})")
        raise DeviceError("timeout")
    _drop(cmd)
    res = cmd["result"] or {}
    logger.info(f"[Devices] {device_id}: {action} за {time.time() - t0:.1f} с, "
                f"{'ok' if res.get('ok') else 'ошибка'}")
    if not res.get("ok"):
        raise DeviceError("failed", res.get("error") or "")
    return res


def _take(device_id: str) -> list[dict]:
    with _lock:
        q = _queues.get(device_id)
        out = []
        while q:
            cmd = q.popleft()
            if cmd["id"] not in _cmds:
                continue  # снята вызывающим
            cmd["taken"] = True
            out.append({"id": cmd["id"], "action": cmd["action"], "args": cmd["args"]})
        return out


def _requeue(device_id: str, taken: list[dict]) -> None:
    # Команды ушли бы в оборванный запрос — обратно в начало очереди
    with _lock:
        q = _queues.setdefault(device_id, deque())
        for pub in reversed(taken):
            cmd = _cmds.get(pub["id"])
            if cmd is not None and not cmd["done"].is_set():
                cmd["taken"] = False
                q.appendleft(cmd)


def _note(device_id: str, waiting: bool, touch: bool = True) -> None:
    with _lock:
        s = _seen.setdefault(device_id, {"seen": time.time()})
        if touch:
            s["seen"] = time.time()
        s["waiting"] = waiting


async def next_commands(device_id: str, wait: float,
                        gone: Optional[Callable[[], Awaitable[bool]]] = None) -> Optional[list]:
    """Долгий опрос устройства: команды сразу, если есть, иначе ждём до wait
    секунд (не больше MAX_WAIT_SEC) — до команды, остановки ядра или обрыва
    соединения. None — устройство не представилось (нужен hello)."""
    with _lock:
        if device_id not in _registry():
            return None
    _note(device_id, waiting=False)
    taken = _take(device_id)
    wait = min(max(wait, 0.0), MAX_WAIT_SEC)
    if taken or wait <= 0:
        return taken
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    event = asyncio.Event()
    waiter = (loop, event)
    with _lock:
        _waiters.setdefault(device_id, set()).add(waiter)
    _note(device_id, waiting=True)
    try:
        while True:
            # Сброс до проверки: команда, пришедшая между ними, снова поднимет
            # событие (call_soon_threadsafe выполнится после await)
            event.clear()
            taken = _take(device_id)
            if taken:
                if gone is not None and await gone():
                    _requeue(device_id, taken)
                    return []
                return taken
            while not event.is_set():
                left = deadline - loop.time()
                if left <= 0 or _stopping() or (gone is not None and await gone()):
                    return []
                try:
                    await asyncio.wait_for(event.wait(), min(left, _STOP_POLL_SEC))
                except (asyncio.TimeoutError, TimeoutError):
                    pass
    finally:
        with _lock:
            _waiters.get(device_id, set()).discard(waiter)
        # Без отметки «был на связи»: ожидание кончилось на ядре, а не по
        # запросу устройства (см. phone_llm.next_job)
        _note(device_id, waiting=False, touch=False)


def complete(device_id: str, command_id: str, ok: bool, data: Optional[dict] = None,
             image: Optional[bytes] = None, error: Optional[str] = None) -> bool:
    """Итог команды от устройства. False — команду уже сняли или она не его."""
    _note(device_id, waiting=False)
    with _lock:
        cmd = _cmds.get(command_id)
        if cmd is None or cmd["device"] != device_id or cmd["done"].is_set():
            return False
        cmd["result"] = {"ok": bool(ok), "data": data or {}, "image": image, "error": error}
        cmd["done"].set()
    return True

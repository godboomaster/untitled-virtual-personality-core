"""
Входящий вебхук: POST /api/hooks/<токен> — любой сервис или скрипт
(Home Assistant, IFTTT, CI, cron, uptime-мониторинг) присылает событие,
и оно проходит тот же разбор, что письма и сообщения.

Тело — JSON {"title", "text"/"message", "url", "priority", "source",
"sender", "id"} (поля необязательны; незнакомый JSON сохраняется текстом),
форма или простой текст. priority: urgent | high | normal | low — без
неё уровень решает разбор. Токен в адресе — единственный ключ: адрес
держать в секрете, при утечке — «Новый адрес» в «Сервисах».
"""

import hashlib
import json
import re
import secrets
import threading
import time
from typing import Optional
from urllib.parse import parse_qs

from app.integrations import store
from app.integrations.feed import Event

RATE_PER_MIN = 30
MAX_BODY = 64 * 1024
_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")

_rate_lock = threading.Lock()
_hits: list = []


def token() -> str:
    return str(store.load_secrets("webhook").get("token") or "")


def regenerate() -> str:
    new = secrets.token_hex(16)
    store.update_secrets("webhook", {"token": new}, replace=True)
    return new


def token_ok(value: str) -> bool:
    cur = token()
    return bool(cur) and bool(_TOKEN_RE.match(value or "")) and secrets.compare_digest(value, cur)


def rate_ok(now: Optional[float] = None) -> bool:
    now = now if now is not None else time.time()
    with _rate_lock:
        while _hits and now - _hits[0] > 60:
            _hits.pop(0)
        if len(_hits) >= RATE_PER_MIN:
            return False
        _hits.append(now)
        return True


def _first(data: dict, *keys) -> str:
    for k in keys:
        v = data.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def parse(body: bytes, content_type: str = "") -> Event:
    raw = body[:MAX_BODY]
    text = raw.decode("utf-8", errors="replace")
    data: dict = {}
    ctype = (content_type or "").lower()
    if "json" in ctype or text.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(text)
            data = parsed if isinstance(parsed, dict) else {"text": json.dumps(parsed, ensure_ascii=False)}
        except ValueError:
            data = {"text": text}
    elif "x-www-form-urlencoded" in ctype:
        data = {k: v[0] for k, v in parse_qs(text).items() if v}
    else:
        data = {"text": text}
    title = _first(data, "title", "subject", "name", "event", "summary")
    msg = _first(data, "text", "message", "body", "description", "content")
    known = {"title", "subject", "name", "event", "summary", "text", "message", "body",
             "description", "content", "url", "link", "priority", "level", "source",
             "sender", "from", "id"}
    if not msg and set(data) - known:
        msg = json.dumps({k: v for k, v in data.items() if k not in known},
                         ensure_ascii=False)[:2000]
    source = _first(data, "source", "app", "service")[:80]
    ident = _first(data, "id") or hashlib.sha1(raw).hexdigest()[:16] + f":{int(time.time() // 60)}"
    return Event(source="webhook", key=ident[:120], kind="event",
                 title=title or source or "Webhook", sender=_first(data, "sender", "from") or source,
                 text=msg, url=_first(data, "url", "link"), ts=time.time(),
                 meta={"priority": _first(data, "priority", "level").lower(), "app": source},
                 group=source)

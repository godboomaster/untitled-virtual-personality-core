"""Ссылки на файлы, присланные в веб-чат («пришли файл X», file_access).

Ответ бота несёт не сам файл, а одноразово выданный случайный id:
GET /api/files/{id} (под тем же токеном API) отдаёт файл, пока ссылка жива.
Реестр — в памяти процесса: после перезапуска ядра ссылки пропадают, текст
ответа в истории остаётся. Метка ответа в STM (reply_ts) привязывает файлы
к сообщению — история после перезагрузки страницы показывает их снова.
"""

import secrets
import threading
import time
from typing import Dict, List, Optional

# Ссылка живёт сутки: успеть скачать на телефоне, вернувшись к чату
TTL_SEC = 24 * 3600
MAX_LINKS = 300

_lock = threading.Lock()
_links: Dict[str, dict] = {}


def _prune(now: float) -> None:
    for k in [k for k, v in _links.items() if v["expires"] <= now]:
        _links.pop(k, None)
    if len(_links) > MAX_LINKS:
        for k in sorted(_links, key=lambda k: _links[k]["expires"])[:len(_links) - MAX_LINKS]:
            _links.pop(k, None)


def public(link_id: str, entry: dict) -> dict:
    return {"id": link_id, "name": entry["name"], "size": entry["size"],
            "url": f"/api/files/{link_id}"}


def register(persona: str, chat_key: str, files: List[dict],
             reply_ts: Optional[float] = None) -> List[dict]:
    """Файлы хода → публичные записи {id, name, size, url} для ответа."""
    out = []
    now = time.time()
    with _lock:
        _prune(now)
        for f in files:
            link_id = secrets.token_urlsafe(24)
            entry = {"path": f["path"], "name": f["name"], "size": int(f.get("size") or 0),
                     "persona": persona, "chat": str(chat_key),
                     "reply_ts": reply_ts, "expires": now + TTL_SEC}
            _links[link_id] = entry
            out.append(public(link_id, entry))
    return out


def get(link_id: str) -> Optional[dict]:
    with _lock:
        e = _links.get(link_id)
        if not e or e["expires"] <= time.time():
            _links.pop(link_id, None)
            return None
        return dict(e)


def for_history(persona: str, chat_key: str) -> List[tuple]:
    """[(reply_ts, [публичные записи])] живых ссылок чата — для истории."""
    now = time.time()
    by_ts: Dict[float, List[dict]] = {}
    with _lock:
        for k, e in _links.items():
            if (e["persona"] == persona and e["chat"] == str(chat_key)
                    and e["reply_ts"] is not None and e["expires"] > now):
                by_ts.setdefault(e["reply_ts"], []).append(public(k, e))
    return sorted(by_ts.items())


def clear() -> None:
    with _lock:
        _links.clear()

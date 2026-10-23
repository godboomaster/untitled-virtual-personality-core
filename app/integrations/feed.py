"""
Лента событий сервисов: data/integrations/feed.json.

Событие приходит от источника (Event), получает id (хеш источника и ключа
— повтор того же письма или уведомления GitHub не создаёт второй записи),
потом разбор пишет в запись уровень, пересказ и причину, а уведомитель —
статус доставки:

    new      — ждёт разбора
    queued   — сообщить (ждёт окна склейки, тихих часов, лимита частоты)
    digest   — в сводку
    notified — сообщено; digested — ушло сводкой
    ignored  — не сообщать; skipped — неактуально (прочитано в самом
               сервисе, устарело)

Статусы переживают перезапуск: hub дочитывает new и queued.
"""

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from app.core.atomic_io import atomic_write_json, file_lock, load_json_safe
from app.integrations import store

MAX_ITEMS = 400
MAX_SEEN = 4000
MAX_TEXT = 2000
MAX_TITLE = 300

STATUSES = ("new", "queued", "digest", "notified", "digested", "ignored", "skipped")


def _clip(text, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def event_id(source: str, key: str) -> str:
    return hashlib.sha1(f"{source}:{key}".encode("utf-8")).hexdigest()[:16]


@dataclass
class Event:
    source: str          # gmail | drive | github | telegram | imap | rss | webhook
    key: str             # устойчивый ключ внутри источника
    kind: str            # message | mention | review_requested | shared | item …
    title: str
    sender: str = ""
    text: str = ""
    url: str = ""
    ts: float = 0.0      # когда случилось
    # Признаки источника для разбора: labels, muted, is_contact, reason …
    meta: dict = field(default_factory=dict)
    # Нить/чат/репозиторий — для склейки и правил
    group: str = ""

    @property
    def id(self) -> str:
        return event_id(self.source, self.key)

    def record(self) -> dict:
        now = time.time()
        return {
            "id": self.id, "source": self.source, "kind": self.kind,
            "title": _clip(self.title, MAX_TITLE), "sender": _clip(self.sender, 200),
            "text": _clip(self.text, MAX_TEXT), "url": str(self.url or "")[:1000],
            "ts": float(self.ts or now), "received": now,
            "meta": dict(self.meta or {}), "group": _clip(self.group, 200),
            "level": None, "summary": "", "reason": "", "by": "",
            "status": "new", "notified_at": None, "feedback": None,
        }


class Feed:
    """Лента с межпроцессным локом: Telegram-процесс читает её для ответов
    в чате («что пришло в почту?») и пишет оценки «важно/не важно»."""

    def __init__(self):
        self._lock = threading.Lock()

    @property
    def path(self):
        return store.root() / "feed.json"

    def _load(self) -> dict:
        data = load_json_safe(self.path, default={}, label="Feed")
        if not isinstance(data, dict):
            data = {}
        items = [x for x in data.get("items") or [] if isinstance(x, dict) and x.get("id")]
        seen = [x for x in data.get("seen") or [] if isinstance(x, str)]
        return {"items": items, "seen": seen}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Срезаем старое: сначала то, что уже не ждёт действий
        items = data["items"]
        if len(items) > MAX_ITEMS:
            waiting = {"new", "queued", "digest"}
            drop = len(items) - MAX_ITEMS
            keep = []
            for it in items:
                if drop > 0 and it.get("status") not in waiting:
                    drop -= 1
                    continue
                keep.append(it)
            items = keep[-MAX_ITEMS:]
        data["items"] = items
        data["seen"] = data["seen"][-MAX_SEEN:]
        atomic_write_json(self.path, data)

    def add(self, events: Iterable[Event]) -> List[dict]:
        """Новые записи (повторы по id отбрасываются)."""
        events = list(events)
        if not events:
            return []
        with self._lock, file_lock(self.path):
            data = self._load()
            known = set(data["seen"]) | {it["id"] for it in data["items"]}
            added = []
            for ev in events:
                rec = ev.record()
                if rec["id"] in known:
                    continue
                known.add(rec["id"])
                data["items"].append(rec)
                data["seen"].append(rec["id"])
                added.append(rec)
            if added:
                self._save(data)
            return added

    def update(self, ids: Iterable[str], **fields) -> List[dict]:
        ids = set(ids)
        if not ids:
            return []
        with self._lock, file_lock(self.path):
            data = self._load()
            changed = []
            for it in data["items"]:
                if it["id"] in ids:
                    it.update(fields)
                    changed.append(dict(it))
            if changed:
                self._save(data)
            return changed

    def apply(self, updates: dict) -> None:
        """Пачка правок {id: {поле: значение}} одной записью файла."""
        if not updates:
            return
        with self._lock, file_lock(self.path):
            data = self._load()
            for it in data["items"]:
                upd = updates.get(it["id"])
                if upd:
                    it.update(upd)
            self._save(data)

    def get(self, item_id: str) -> Optional[dict]:
        for it in self.items():
            if it["id"] == item_id:
                return it
        return None

    def items(self) -> List[dict]:
        with self._lock, file_lock(self.path):
            return self._load()["items"]

    def with_status(self, *statuses: str) -> List[dict]:
        return [it for it in self.items() if it.get("status") in statuses]

    def recent(self, limit: int = 50, sources: Optional[Iterable[str]] = None,
               since: float = 0.0, levels: Optional[Iterable[str]] = None) -> List[dict]:
        """Свежие сверху."""
        sources = set(sources) if sources else None
        levels = set(levels) if levels else None
        out = []
        for it in reversed(self.items()):
            if sources and it.get("source") not in sources:
                continue
            if levels and it.get("level") not in levels:
                continue
            if since and float(it.get("received") or 0) < since:
                continue
            out.append(it)
            if len(out) >= limit:
                break
        return out

    def clear(self) -> None:
        with self._lock, file_lock(self.path):
            data = self._load()
            data["items"] = []
            self._save(data)


feed = Feed()

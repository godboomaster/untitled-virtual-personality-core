"""
RSS и Atom: блоги, новости, релизы на GitHub (…/releases.atom), каналы
YouTube (feeds/videos.xml?channel_id=…) и всё, у чего есть лента.

Каждая лента — запрос с ETag/Last-Modified (без новостей — 304). Первый
опрос ленты только запоминает, что в ней уже есть. По умолчанию записи
идут в сводку; если в «что для меня важно» что-то написано — модель
разбора может поднять подходящую запись до уведомления.
"""

import hashlib
import logging
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime
from typing import List, Optional, Tuple

from app.integrations import store
from app.integrations.feed import Event
from app.integrations.sources import PollSource, SourceError, http
from app.integrations.sources.google import strip_html

logger = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
MAX_SEEN = 300
MAX_NEW_PER_FEED = 15


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(el, *names) -> Optional[ET.Element]:
    for ch in el:
        if _local(ch.tag) in names:
            return ch
    return None


def _text(el, *names) -> str:
    ch = _child(el, *names)
    return (ch.text or "").strip() if ch is not None and ch.text else ""


def _date(value: str) -> float:
    value = (value or "").strip()
    if not value:
        return 0.0
    try:
        return parsedate_to_datetime(value).timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_feed(content: bytes) -> Tuple[str, List[dict]]:
    """(название ленты, записи {id, title, link, summary, ts})."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise SourceError(f"rss: не XML ({e})")
    kind = _local(root.tag)
    items: List[dict] = []
    title = ""
    if kind == "feed":  # Atom
        title = _text(root, "title")
        for entry in root:
            if _local(entry.tag) != "entry":
                continue
            link = ""
            for ch in entry:
                if _local(ch.tag) == "link" and ch.get("rel", "alternate") == "alternate":
                    link = ch.get("href", "")
                    break
            summary = _text(entry, "summary") or _text(entry, "content")
            items.append({"id": _text(entry, "id") or link, "title": _text(entry, "title"),
                          "link": link, "summary": summary,
                          "ts": _date(_text(entry, "published") or _text(entry, "updated"))})
    else:  # RSS 2.0 / RDF
        channel = _child(root, "channel")
        title = _text(channel, "title") if channel is not None else ""
        parents = [channel, root] if channel is not None else [root]
        for parent in parents:
            for item in parent:
                if _local(item.tag) != "item":
                    continue
                link = _text(item, "link")
                items.append({"id": _text(item, "guid") or link or _text(item, "title"),
                              "title": _text(item, "title"), "link": link,
                              "summary": _text(item, "description", "encoded"),
                              "ts": _date(_text(item, "pubdate", "date"))})
    for it in items:
        it["summary"] = strip_html(it["summary"])[:800]
    return title, [it for it in items if it["id"]]


def fetch(url: str, etag: str = "", modified: str = "") -> Tuple[int, bytes, dict]:
    headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    with http(30.0) as c:
        with c.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 304:
                return 304, b"", dict(resp.headers)
            if resp.status_code >= 400:
                raise SourceError(f"rss: {url} — HTTP {resp.status_code}")
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise SourceError(f"rss: {url} — лента больше 5 МБ")
            return resp.status_code, body, dict(resp.headers)


def _key(url: str, item_id: str) -> str:
    return hashlib.sha1(f"{url}\n{item_id}".encode("utf-8")).hexdigest()[:20]


class RssSource(PollSource):
    name = "rss"

    def __init__(self, settings_getter=None):
        self._settings = settings_getter or store.load_settings

    def configured(self) -> bool:
        return bool(self._settings()["services"]["rss"]["feeds"])

    def check(self, url: Optional[str] = None) -> dict:
        feeds = [url] if url else [f["url"] for f in self._settings()["services"]["rss"]["feeds"]]
        out = []
        for u in feeds:
            try:
                _, body, _ = fetch(u)
                title, items = parse_feed(body)
                out.append({"url": u, "ok": True, "title": title, "items": len(items)})
            except SourceError as e:
                out.append({"url": u, "ok": False, "error": str(e)})
        return {"ok": all(f["ok"] for f in out) if out else False, "feeds": out}

    def poll(self) -> List[Event]:
        feeds = self._settings()["services"]["rss"]["feeds"]
        st = store.load_state("rss")
        per = dict(st.get("feeds") or {})
        events: List[Event] = []
        errors = []
        for f in feeds:
            url = f["url"]
            fs = dict(per.get(url) or {})
            try:
                code, body, headers = fetch(url, fs.get("etag", ""), fs.get("modified", ""))
            except SourceError as e:
                errors.append(str(e))
                continue
            except Exception as e:
                errors.append(f"rss: {url} — {e}")
                continue
            if code == 304:
                continue
            try:
                title, items = parse_feed(body)
            except SourceError as e:
                errors.append(str(e))
                continue
            seen = list(fs.get("seen") or [])
            first = not fs.get("seen_init")
            fresh = []
            for it in items:
                k = _key(url, it["id"])
                if k in seen:
                    continue
                seen.append(k)
                if not first:
                    fresh.append((k, it))
            for k, it in fresh[:MAX_NEW_PER_FEED]:
                name = f.get("title") or title or url
                events.append(Event(source="rss", key=k, kind="item", title=it["title"] or name,
                                    sender=name, text=it["summary"], url=it["link"],
                                    ts=it["ts"] or time.time(), meta={"feed": name},
                                    group=name))
            per[url] = {"etag": headers.get("etag", ""), "modified": headers.get("last-modified", ""),
                        "seen": seen[-MAX_SEEN:], "seen_init": True, "title": title}
        # Удалённые из настроек ленты — забываем
        per = {u: v for u, v in per.items() if u in {f["url"] for f in feeds}}
        store.update_state("rss", {"feeds": per})
        if errors and not events and len(errors) == len(feeds):
            raise SourceError("; ".join(errors[:3]))
        if errors:
            logger.info(f"[RSS] часть лент не ответила: {'; '.join(errors[:3])}")
        return events

"""
Уведомитель: когда и как сообщить о разобранных событиях.

- urgent — сразу (в тихие часы и при «не беспокоить» — если разрешено
  настройкой quiet.urgent);
- notify — после короткого окна склейки (события одной минуты уходят
  одним сообщением), вне тихих часов и «не беспокоить», не чаще
  max_per_hour сообщений в час — лишнее ждёт и уходит пачкой;
- digest — сводкой в digest_times (пропущенное время, пока ядро было
  выключено, — сводка при первом тике) и по просьбе в чате;
- ignore — только лента.

Тексты собирает код (style plain) — быстро и без лишних вызовов модели;
style persona — персона пересказывает своими словами, факты и ссылки те же.
Доставка (deliver) — снаружи: hub шлёт в чат персоны (веб и телефон) и
в Telegram от бота персоны.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from app.core import timeutil
from app.integrations import store
from app.integrations.feed import feed

logger = logging.getLogger(__name__)

COALESCE_SEC = 20.0
# Событие старше этого к моменту разбора (ядро было выключено) — не
# прерывать, а в сводку
STALE_SEC = 6 * 3600
MAX_LINES = 8
DIGEST_PER_SOURCE = 4

SOURCE_LABELS = {
    "ru": {"gmail": "Почта", "imap": "Почта", "drive": "Google Диск", "github": "GitHub",
           "telegram": "Telegram", "rss": "Ленты", "webhook": "Сервис"},
    "en": {"gmail": "Mail", "imap": "Mail", "drive": "Google Drive", "github": "GitHub",
           "telegram": "Telegram", "rss": "Feeds", "webhook": "Service"},
}
_T = {
    "ru": {"urgent": "Срочно", "new": "Новое ({n})", "more": "…и ещё {n}",
           "digest": "Сводка: {n} {events}", "digest_more": "…и ещё {n}",
           "feed": "Всё — в разделе «Сервисы».", "in": "в",
           "events": ("событие", "события", "событий"), "test": "Проверка уведомлений: всё работает."},
    "en": {"urgent": "Urgent", "new": "New ({n})", "more": "…and {n} more",
           "digest": "Digest: {n} {events}", "digest_more": "…and {n} more",
           "feed": "Everything is in Services.", "in": "in",
           "events": ("event", "events", "events"), "test": "Notification check: everything works."},
}


def _plural_ru(n: int, forms: Tuple[str, str, str]) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _events_word(n: int, lang: str) -> str:
    forms = _T[lang]["events"]
    return _plural_ru(n, forms) if lang == "ru" else (forms[0] if n == 1 else forms[1])


def tr(lang: Optional[str]) -> str:
    return lang if lang in _T else "ru"


# ── Время ──────────────────────────────────────────────────────────────

def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_quiet(settings: dict, now: Optional[float] = None) -> bool:
    q = settings["notify"]["quiet"]
    if not q.get("enabled"):
        return False
    dt = timeutil.from_ts(now if now is not None else time.time())
    cur = dt.hour * 60 + dt.minute
    start, end = _minutes(q["from"]), _minutes(q["to"])
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def dnd_active(settings: dict, now: Optional[float] = None) -> bool:
    return float(settings.get("dnd_until") or 0) > (now if now is not None else time.time())


def last_digest_slot(settings: dict, now: float) -> Optional[float]:
    """Последний момент сводки по расписанию ≤ now (сегодня или вчера)."""
    times = settings["notify"].get("digest_times") or []
    if not times:
        return None
    cur = timeutil.from_ts(now)
    best = None
    for day_shift in (0, 1):
        day = (cur - timedelta(days=day_shift)).date()
        for t in times:
            h, m = (int(x) for x in t.split(":"))
            slot = timeutil.to_ts(datetime(day.year, day.month, day.day, h, m))
            if slot <= now and (best is None or slot > best):
                best = slot
        if best is not None:
            break
    return best


# ── Тексты ─────────────────────────────────────────────────────────────

def _who(item: dict, lang: str) -> str:
    sender = str(item.get("sender") or "").strip()
    group = str(item.get("group") or "").strip()
    if item.get("source") == "telegram" and group and group != sender:
        return f"{sender} {_T[lang]['in']} «{group}»" if sender else f"«{group}»"
    if item.get("source") in ("github", "rss") and group:
        return group
    return sender or group


def line(item: dict, lang: str) -> str:
    label = SOURCE_LABELS[lang].get(item.get("source"), item.get("source"))
    who = _who(item, lang)
    summary = str(item.get("summary") or item.get("title") or "").strip()
    head = f"{label} · {who}" if who else label
    return f"{head}: {summary}" if summary else head


def compose_notification(items: List[dict], lang: Optional[str]) -> str:
    lang = tr(lang)
    t = _T[lang]
    urgent = any(it.get("level") == "urgent" for it in items)
    if len(items) == 1:
        it = items[0]
        text = line(it, lang)
        if urgent:
            text = f"{t['urgent']} — {text}"
        if it.get("url"):
            text += f"\n{it['url']}"
        return text
    # Срочные — первыми
    items = sorted(items, key=lambda it: 0 if it.get("level") == "urgent" else 1)
    head = t["new"].format(n=len(items))
    if urgent:
        head = f"{t['urgent']} — {head}"
    rows = []
    for it in items[:MAX_LINES]:
        mark = "! " if it.get("level") == "urgent" and len(items) > 1 else ""
        row = f"— {mark}{line(it, lang)}"
        if it.get("url") and len(items) <= 3:
            row += f"\n  {it['url']}"
        rows.append(row)
    if len(items) > MAX_LINES:
        rows.append(t["more"].format(n=len(items) - MAX_LINES))
    return head + "\n" + "\n".join(rows)


def compose_digest(items: List[dict], lang: Optional[str]) -> str:
    lang = tr(lang)
    t = _T[lang]
    labels = SOURCE_LABELS[lang]
    by_src: Dict[str, List[dict]] = {}
    for it in items:
        by_src.setdefault(labels.get(it.get("source"), it.get("source")), []).append(it)
    parts = [t["digest"].format(n=len(items), events=_events_word(len(items), lang))]
    for label, group in sorted(by_src.items(), key=lambda kv: -len(kv[1])):
        rows = []
        for it in group[-DIGEST_PER_SOURCE:][::-1]:
            who = _who(it, lang)
            summary = str(it.get("summary") or it.get("title") or "").strip()
            rows.append(f"— {who}: {summary}" if who else f"— {summary}")
        if len(group) > DIGEST_PER_SOURCE:
            rows.append(t["digest_more"].format(n=len(group) - DIGEST_PER_SOURCE))
        parts.append(f"{label} ({len(group)}):\n" + "\n".join(rows))
    parts.append(t["feed"])
    return "\n\n".join(parts)


def test_text(lang: Optional[str]) -> str:
    return _T[tr(lang)]["test"]


# ── Тик ────────────────────────────────────────────────────────────────

Deliver = Callable[[str, List[dict], str], Optional[bool]]


class Notifier:
    """tick() раз в несколько секунд из hub. deliver(text, items, kind) →
    True — доставлено, False — не удалось (повтор позже), None — занято
    (идёт ход пользователя, повтор на следующем тике).

    relevant(items) → id тех, о ком сообщать уже не нужно (прочитано в
    самом сервисе); lang() — язык текстов; restyle(text, items) — пересказ
    персоной (style persona) или None."""

    def __init__(self, deliver: Deliver,
                 relevant: Optional[Callable[[List[dict]], set]] = None,
                 lang: Optional[Callable[[], Optional[str]]] = None,
                 restyle: Optional[Callable[[str, List[dict], str], Optional[str]]] = None):
        self._deliver = deliver
        self._relevant = relevant
        self._lang = lang or (lambda: "ru")
        self._restyle = restyle

    def accept(self, decisions: dict, items: List[dict], now: Optional[float] = None) -> None:
        """Записать решения разбора в ленту и поставить в очередь."""
        now = now if now is not None else time.time()
        by_id = {it["id"]: it for it in items}
        updates = {}
        for item_id, d in decisions.items():
            item = by_id.get(item_id) or {}
            level = d["level"]
            reason = d.get("reason", "")
            # Догнали старое (ядро было выключено): не прерываем
            if level in ("urgent", "notify") and now - float(item.get("ts") or now) > STALE_SEC:
                level = "digest"
                reason = (reason + "; " if reason else "") + "stale"
            status = {"urgent": "queued", "notify": "queued",
                      "digest": "digest", "ignore": "ignored"}[level]
            updates[item_id] = {"level": level, "summary": d.get("summary", ""),
                                "reason": reason, "by": d.get("by", ""), "status": status}
        feed.apply(updates)

    def _send(self, text: str, items: List[dict], kind: str) -> Optional[bool]:
        if self._restyle is not None:
            try:
                styled = self._restyle(text, items, kind)
                if styled:
                    text = styled
            except Exception as e:
                logger.warning(f"[Integrations] пересказ персоной не удался: {e}")
        return self._deliver(text, items, kind)

    def tick(self, now: Optional[float] = None) -> dict:
        """Один проход: срочное, обычное, сводка. Возвращает, что сделано
        (для тестов и лога)."""
        now = now if now is not None else time.time()
        settings = store.load_settings()
        done = {"notified": [], "digested": [], "skipped": []}
        quiet = in_quiet(settings, now) or dnd_active(settings, now)
        urgent_ok = (not quiet) or settings["notify"]["quiet"].get("urgent", True)
        nstate = store.load_state("notifier")
        before = dict(nstate)
        sent = [t for t in nstate.get("sent") or [] if now - t < 3600]
        limit = settings["notify"]["max_per_hour"]

        queued = feed.with_status("queued")
        urgent = [q for q in queued if q.get("level") == "urgent"]
        normal = [q for q in queued if q.get("level") != "urgent"]
        batch: List[dict] = []
        if urgent and urgent_ok:
            batch.extend(urgent)
        if normal and not quiet and len(sent) < limit:
            newest = max(float(q.get("received") or 0) for q in normal)
            # Окно склейки: ждём, пока поток событий не утихнет (не дольше
            # минуты от самого старого), — пачка уходит одним сообщением
            oldest = min(float(q.get("received") or 0) for q in normal)
            if now - newest >= COALESCE_SEC or now - oldest >= 3 * COALESCE_SEC or batch:
                batch.extend(normal)
        # Прочитанное в самом сервисе (письмо открыто, чат Telegram прочитан)
        # — проверяем только то, что уходит сейчас: проверка стоит запроса
        if batch and self._relevant is not None:
            try:
                gone = self._relevant(batch)
            except Exception as e:
                logger.debug(f"[Integrations] проверка прочитанного: {e}")
                gone = set()
            if gone:
                feed.update(gone, status="skipped", reason="read elsewhere")
                done["skipped"] = sorted(gone)
                batch = [b for b in batch if b["id"] not in gone]
        if batch:
            batch.sort(key=lambda q: float(q.get("received") or 0))
            res = self._send(compose_notification(batch, self._lang()), batch, "notify")
            if res:
                feed.update([b["id"] for b in batch], status="notified", notified_at=now)
                sent.append(now)
                done["notified"] = [b["id"] for b in batch]

        slot = last_digest_slot(settings, now)
        if slot is not None and not quiet and float(nstate.get("last_digest") or 0) < slot:
            pending = feed.with_status("digest")
            if pending:
                res = self._send(compose_digest(pending, self._lang()), pending, "digest")
                if res:
                    feed.update([p["id"] for p in pending], status="digested", notified_at=now)
                    nstate["last_digest"] = now
                    done["digested"] = [p["id"] for p in pending]
            else:
                nstate["last_digest"] = now
        nstate["sent"] = sent
        if nstate != before:
            store.update_state("notifier", nstate, replace=True)
        return done

    def digest_now(self, mark: bool = True) -> Tuple[str, List[dict]]:
        """Сводка по просьбе («что я пропустил?»): текст и события; mark —
        считать их сообщёнными."""
        pending = feed.with_status("digest", "queued")
        if not pending:
            return "", []
        text = compose_digest(pending, self._lang())
        if mark:
            feed.update([p["id"] for p in pending], status="digested", notified_at=time.time())
        return text, pending

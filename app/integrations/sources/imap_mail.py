"""
Почта по IMAP: Яндекс, Mail.ru, Outlook, iCloud и любой другой ящик.

Вход — пароль приложения (у Яндекса и Mail.ru его создают в настройках
безопасности; обычный пароль с двухфакторной защитой не подойдёт). Ящик
открывается только на чтение (EXAMINE), письма берутся с BODY.PEEK — не
помечаются прочитанными. Курсор — UID последнего увиденного письма при
неизменном UIDVALIDITY; первый опрос только его запоминает.
"""

import email
import imaplib
import logging
import re
import socket
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import List, Optional

from app.integrations import store
from app.integrations.feed import Event
from app.integrations.sources import AuthError, PollSource, SourceError
from app.integrations.sources.google import strip_html

logger = logging.getLogger(__name__)

PRESETS = {
    "yandex": ("imap.yandex.ru", 993), "mailru": ("imap.mail.ru", 993),
    "gmail": ("imap.gmail.com", 993), "outlook": ("outlook.office365.com", 993),
    "icloud": ("imap.mail.me.com", 993), "rambler": ("imap.rambler.ru", 993),
}
MAX_NEW_PER_POLL = 30
_FETCH_BYTES = 65536
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{3,253}$")

# Тесты подменяют фабрику соединения
connect_factory = None


def preset_for(user: str) -> Optional[tuple]:
    domain = str(user or "").rsplit("@", 1)[-1].lower()
    return {
        "yandex.ru": PRESETS["yandex"], "ya.ru": PRESETS["yandex"], "yandex.com": PRESETS["yandex"],
        "mail.ru": PRESETS["mailru"], "bk.ru": PRESETS["mailru"], "inbox.ru": PRESETS["mailru"],
        "list.ru": PRESETS["mailru"], "internet.ru": PRESETS["mailru"],
        "gmail.com": PRESETS["gmail"], "outlook.com": PRESETS["outlook"],
        "hotmail.com": PRESETS["outlook"], "live.com": PRESETS["outlook"],
        "icloud.com": PRESETS["icloud"], "me.com": PRESETS["icloud"],
        "rambler.ru": PRESETS["rambler"],
    }.get(domain)


def normalize_account(data: dict) -> dict:
    user = str(data.get("user") or "").strip()
    password = str(data.get("password") or "")
    host = str(data.get("host") or "").strip().lower()
    port = int(data.get("port") or 0)
    if not host:
        preset = preset_for(user)
        if preset:
            host, port = preset
    if not user or not password or not _HOST_RE.match(host):
        raise ValueError("нужны адрес ящика, пароль приложения и сервер IMAP")
    port = port or 993
    if not 1 <= port <= 65535:
        raise ValueError("неверный порт")
    folder = str(data.get("folder") or "INBOX").strip()[:100] or "INBOX"
    return {"user": user, "password": password, "host": host, "port": port, "folder": folder}


def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def _connect(acc: dict):
    if connect_factory is not None:
        return connect_factory(acc)
    try:
        conn = imaplib.IMAP4_SSL(acc["host"], int(acc["port"]), timeout=30)
    except (OSError, socket.timeout) as e:
        raise SourceError(f"imap: нет соединения с {acc['host']}: {e}")
    try:
        conn.login(acc["user"], acc["password"])
    except imaplib.IMAP4.error as e:
        try:
            conn.logout()
        except Exception:
            pass
        raise AuthError(f"imap: вход не принят ({e})")
    return conn


def _body(msg) -> str:
    plain, rich = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if part.get_content_type() == "text/plain":
            plain.append(text)
        elif part.get_content_type() == "text/html":
            rich.append(text)
    text = "\n".join(plain).strip() or strip_html("\n".join(rich))
    return text[:4000].strip()


def parse_message(raw: bytes, key: str, flags: str = "") -> Optional[Event]:
    msg = email.message_from_bytes(raw)
    name, addr = parseaddr(_decode(msg.get("From")))
    try:
        ts = parsedate_to_datetime(msg.get("Date")).timestamp()
    except Exception:
        ts = 0.0
    return Event(
        source="imap", key=key, kind="message",
        title=_decode(msg.get("Subject")).strip() or "(без темы)",
        sender=name or addr, text=_body(msg), ts=ts,
        meta={"email": addr.lower(), "unread": "\\Seen" not in flags,
              "bulk": bool(msg.get("List-Unsubscribe")
                           or str(msg.get("Precedence") or "").lower() in ("bulk", "list"))},
        group=addr.lower())


class ImapSource(PollSource):
    name = "imap"

    def configured(self) -> bool:
        sec = store.load_secrets("imap")
        return bool(sec.get("user") and sec.get("password") and sec.get("host"))

    def check(self, acc: Optional[dict] = None) -> dict:
        acc = acc or store.load_secrets("imap")
        conn = _connect(acc)
        try:
            typ, _ = conn.select(acc.get("folder") or "INBOX", readonly=True)
            if typ != "OK":
                raise SourceError(f"imap: нет папки {acc.get('folder')}")
            return {"ok": True, "account": acc.get("user")}
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def poll(self) -> List[Event]:
        acc = store.load_secrets("imap")
        st = store.load_state("imap")
        conn = _connect(acc)
        try:
            typ, data = conn.select(acc.get("folder") or "INBOX", readonly=True)
            if typ != "OK":
                raise SourceError(f"imap: нет папки {acc.get('folder')}")
            validity = str((conn.response("UIDVALIDITY")[1] or [b""])[0] or b"",
                           "ascii", "replace") if hasattr(conn, "response") else ""
            typ, data = conn.uid("SEARCH", None, "ALL")
            uids = [int(x) for x in (data[0] or b"").split()] if typ == "OK" else []
            last = int(st.get("last_uid") or 0)
            if not st.get("validity") or st.get("validity") != validity:
                store.update_state("imap", {"validity": validity,
                                            "last_uid": max(uids) if uids else 0})
                return []
            new = [u for u in uids if u > last][-MAX_NEW_PER_POLL:]
            events = []
            for uid in new:
                typ, parts = conn.uid("FETCH", str(uid), f"(FLAGS BODY.PEEK[]<0.{_FETCH_BYTES}>)")
                if typ != "OK" or not parts:
                    continue
                raw, flags = b"", ""
                for part in parts:
                    if isinstance(part, tuple) and len(part) >= 2:
                        flags = part[0].decode("ascii", "replace")
                        raw = part[1]
                if not raw or "\\Seen" in flags:
                    continue
                ev = parse_message(raw, f"{validity}:{uid}", flags)
                if ev:
                    events.append(ev)
            if uids:
                store.update_state("imap", {"last_uid": max(max(uids), last)})
            return events
        except imaplib.IMAP4.error as e:
            raise SourceError(f"imap: {e}")
        finally:
            try:
                conn.logout()
            except Exception:
                pass

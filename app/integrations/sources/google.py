"""
Google: вход OAuth 2.0, Gmail и Google Диск/Документы — только чтение.

Вход. Пользователь создаёт в Google Cloud свой OAuth-клиент типа «Desktop
app» (ID и секрет — в «Сервисах»): своего общего клиента у проекта нет, и
приватные данные идут напрямую Google ↔ этот компьютер. Вход — PKCE через
loopback: на 127.0.0.1 на случайном порту поднимается одноразовый сервер,
Google возвращает туда код (адрес с портом для Desktop-клиентов Google
принимает без регистрации). Браузер на другом устройстве (телефон) до
127.0.0.1 ядра не достанет — тогда адрес из строки браузера после входа
вставляется в «Сервисах» вручную (finish_from_url).

Права: gmail.readonly и drive.readonly (+ email — показать, какой аккаунт
подключён). Писем не отправляем, файлов не меняем.

Gmail — история ящика (users.history) от запомненного historyId: новые
письма во «Входящих», ещё не прочитанные. Диск — лента изменений
(changes): файл, которым с вами поделились, и правка вашего файла другим
человеком. Комментарии в Документах Google присылает письмом — их видит
разбор почты.
"""

import base64
import hashlib
import html
import http.server
import json
import logging
import re
import secrets
import threading
import time
import urllib.parse
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import List, Optional

from app.integrations import store
from app.integrations.feed import Event
from app.integrations.sources import AuthError, PollSource, SourceError, raise_for
from app.integrations.sources import http as http_client

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
DRIVE = "https://www.googleapis.com/drive/v3"
SCOPES = ["openid", "email",
          "https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/drive.readonly"]

LOGIN_TTL = 600
MAX_NEW_PER_POLL = 30
_BODY_MAX = 4000
_READ_MAX = 12000

_CLIENT_ID_RE = re.compile(r"^[\w.-]+\.apps\.googleusercontent\.com$")


# ── Ключи клиента и токены ─────────────────────────────────────────────

def parse_client(payload) -> dict:
    """ID и секрет клиента из полей или из скачанного client_secret_*.json."""
    data = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except ValueError:
            data = {}
    if isinstance(data, dict):
        inner = data.get("installed") or data.get("web") or data
        cid = str(inner.get("client_id") or "").strip()
        secret = str(inner.get("client_secret") or "").strip()
    else:
        cid = secret = ""
    if not _CLIENT_ID_RE.match(cid) or not secret or len(secret) > 200:
        raise ValueError("нужны client_id (…apps.googleusercontent.com) и client_secret")
    return {"client_id": cid, "client_secret": secret}


def connected() -> bool:
    return bool(store.load_secrets("google").get("refresh_token"))


def account() -> str:
    return str(store.load_secrets("google").get("email") or "")


def _jwt_email(id_token: str) -> str:
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload)).get("email") or "")
    except Exception:
        return ""


def _token_request(data: dict) -> dict:
    with http_client() as c:
        resp = c.post(TOKEN_URL, data=data)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400:
        err = str(body.get("error") or resp.status_code)
        if err in ("invalid_grant", "unauthorized_client", "invalid_client"):
            raise AuthError(f"google: {err}")
        raise SourceError(f"google: вход не удался ({err})")
    return body


_token_lock = threading.Lock()


def access_token() -> str:
    """Действующий access_token; истёк — обновление по refresh_token."""
    with _token_lock:
        sec = store.load_secrets("google")
        if not sec.get("refresh_token"):
            raise AuthError("google: не подключён")
        if sec.get("access_token") and float(sec.get("expires_at") or 0) > time.time() + 60:
            return sec["access_token"]
        body = _token_request({
            "client_id": sec.get("client_id", ""), "client_secret": sec.get("client_secret", ""),
            "refresh_token": sec["refresh_token"], "grant_type": "refresh_token"})
        token = body.get("access_token")
        if not token:
            raise SourceError("google: нет access_token в ответе")
        store.update_secrets("google", {
            "access_token": token,
            "expires_at": time.time() + float(body.get("expires_in") or 3600)})
        return token


def _get(url: str, params=None, timeout: float = 20.0):
    with http_client(timeout) as c:
        resp = c.get(url, params=params, headers={"Authorization": f"Bearer {access_token()}"})
    raise_for(resp, "google")
    return resp


# ── Вход (OAuth, PKCE, loopback) ───────────────────────────────────────

class _Login:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending: Optional[dict] = None   # state, verifier, redirect, expires
        self.server: Optional[http.server.HTTPServer] = None
        self.result: Optional[dict] = None    # {"ok": bool, "error": str, "email": str}


_login = _Login()

_DONE_HTML = """<!doctype html><meta charset="utf-8"><title>{title}</title>
<body style="font:16px system-ui;margin:40px;max-width:520px">
<h2>{title}</h2><p>{text}</p></body>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — имя метода задаёт http.server
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" not in q and "error" not in q:
            self.send_response(404)
            self.end_headers()
            return
        res = _finish(q.get("code", [""])[0], q.get("state", [""])[0], q.get("error", [""])[0])
        ok = res.get("ok")
        title = "Google подключён" if ok else "Не получилось подключить Google"
        text = ("Вкладку можно закрыть и вернуться в «Сервисы»." if ok
                else html.escape(res.get("error") or "Попробуйте ещё раз из «Сервисов»."))
        body = _DONE_HTML.format(title=title, text=text).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        threading.Thread(target=_stop_server, daemon=True).start()

    def log_message(self, *args):  # без строк доступа в лог
        return


def _stop_server():
    with _login.lock:
        server, _login.server = _login.server, None
    if server is not None:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass


def start_login(serve: bool = True) -> str:
    """Ссылка на вход Google. serve — поднять одноразовый сервер для
    возврата (тесты обходятся без него)."""
    sec = store.load_secrets("google")
    if not sec.get("client_id") or not sec.get("client_secret"):
        raise ValueError("сначала укажите client_id и client_secret")
    _stop_server()
    port = 0
    if serve:
        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        server.timeout = 1
        with _login.lock:
            _login.server = server

        def run(srv=server):
            try:
                srv.serve_forever(poll_interval=0.5)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True, name="google-oauth").start()
        # Вход не завершили — сервер гаснет сам; демон: не держит остановку ядра
        timer = threading.Timer(LOGIN_TTL, _stop_server)
        timer.daemon = True
        timer.start()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()) \
        .decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    redirect = f"http://127.0.0.1:{port or 1}/"
    with _login.lock:
        _login.pending = {"state": state, "verifier": verifier, "redirect": redirect,
                          "expires": time.time() + LOGIN_TTL}
        _login.result = None
    params = {
        "client_id": sec["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _finish(code: str, state: str, error: str = "") -> dict:
    with _login.lock:
        pending = _login.pending
    if pending is None or time.time() > pending["expires"]:
        res = {"ok": False, "error": "вход устарел — начните заново"}
    elif not secrets.compare_digest((state or "").encode("utf-8"), pending["state"].encode("utf-8")):
        res = {"ok": False, "error": "чужой ответ входа (state не совпал)"}
    elif error:
        res = {"ok": False, "error": "доступ не выдан" if error == "access_denied" else error}
    elif not code:
        res = {"ok": False, "error": "в ответе нет кода"}
    else:
        sec = store.load_secrets("google")
        try:
            body = _token_request({
                "code": code, "client_id": sec.get("client_id", ""),
                "client_secret": sec.get("client_secret", ""),
                "redirect_uri": pending["redirect"], "grant_type": "authorization_code",
                "code_verifier": pending["verifier"]})
            if not body.get("refresh_token"):
                raise SourceError("Google не выдал refresh_token — уберите доступ приложения "
                                  "в аккаунте Google и подключите заново")
            granted = set(str(body.get("scope") or "").split())
            email = _jwt_email(body.get("id_token") or "")
            store.update_secrets("google", {
                "refresh_token": body["refresh_token"],
                "access_token": body.get("access_token", ""),
                "expires_at": time.time() + float(body.get("expires_in") or 3600),
                "email": email, "scopes": sorted(granted), "connected_at": time.time()})
            res = {"ok": True, "email": email,
                   "gmail": "https://www.googleapis.com/auth/gmail.readonly" in granted,
                   "drive": "https://www.googleapis.com/auth/drive.readonly" in granted}
            store.update_state("gmail", None)
            store.update_state("drive", None)
            store.update_state("google", {"auth_error": ""})
        except (SourceError, AuthError) as e:
            res = {"ok": False, "error": str(e)}
        with _login.lock:
            _login.pending = None
    with _login.lock:
        _login.result = res
    return res


def finish_from_url(pasted: str) -> dict:
    """Адрес из строки браузера после входа (или сам код)."""
    pasted = str(pasted or "").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query) if "?" in pasted else {}
    code = (q.get("code") or [""])[0]
    if not code and re.match(r"^[\w/.-]{10,}$", pasted):
        code = pasted
    with _login.lock:
        state = (_login.pending or {}).get("state", "")
    res = _finish(code, (q.get("state") or [state])[0], (q.get("error") or [""])[0])
    _stop_server()
    return res


def login_result() -> Optional[dict]:
    with _login.lock:
        return dict(_login.result) if _login.result else None


# ── Разбор письма ──────────────────────────────────────────────────────

def _b64(data: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def message_body(payload: dict) -> str:
    """Текст письма: text/plain, иначе text/html без разметки."""
    plain, rich = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            rich.append(_b64(data))
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload or {})
    text = "\n".join(plain).strip() or strip_html("\n".join(rich))
    # Цитата прошлых писем — не новость
    text = re.split(r"\n(?:>|On .{5,80} wrote:|\d{1,2}\.\d{1,2}\.\d{2,4}.{0,60}пишет:)", text)[0]
    return text[:_BODY_MAX].strip()


def _headers(payload: dict) -> dict:
    return {h.get("name", "").lower(): h.get("value", "")
            for h in (payload or {}).get("headers") or []}


def _mail_ts(value: str) -> float:
    try:
        return parsedate_to_datetime(value).timestamp()
    except Exception:
        return time.time()


def gmail_link(thread_id: str) -> str:
    email = account()
    auth = f"?authuser={urllib.parse.quote(email)}" if email else ""
    return f"https://mail.google.com/mail/u/0/{auth}#inbox/{thread_id}"


def message_event(msg: dict) -> Optional[Event]:
    labels = list(msg.get("labelIds") or [])
    if "INBOX" not in labels or {"SENT", "DRAFT"} & set(labels):
        return None
    payload = msg.get("payload") or {}
    h = _headers(payload)
    name, email = parseaddr(h.get("from", ""))
    return Event(
        source="gmail", key=str(msg.get("id")), kind="message",
        title=h.get("subject", "").strip() or "(без темы)",
        sender=name or email, text=message_body(payload) or msg.get("snippet", ""),
        url=gmail_link(str(msg.get("threadId") or msg.get("id"))),
        ts=float(msg.get("internalDate") or 0) / 1000 or _mail_ts(h.get("date", "")),
        meta={"labels": labels, "email": email.lower(), "unread": "UNREAD" in labels,
              "message_id": str(msg.get("id")),
              "bulk": bool(h.get("list-unsubscribe") or h.get("precedence") in ("bulk", "list")),
              "thread": msg.get("threadId")},
        group=email.lower(),
    )


# ── Gmail ──────────────────────────────────────────────────────────────

class GmailSource(PollSource):
    name = "gmail"

    def configured(self) -> bool:
        sec = store.load_secrets("google")
        return bool(sec.get("refresh_token")) and \
            "https://www.googleapis.com/auth/gmail.readonly" in (sec.get("scopes") or [])

    def check(self) -> dict:
        prof = _get(f"{GMAIL}/profile").json()
        return {"ok": True, "account": prof.get("emailAddress"),
                "messages": prof.get("messagesTotal")}

    def poll(self) -> List[Event]:
        st = store.load_state("gmail")
        hid = st.get("history_id")
        if not hid:
            prof = _get(f"{GMAIL}/profile").json()
            store.update_state("gmail", {"history_id": prof.get("historyId")})
            return []
        ids: List[str] = []
        page = None
        new_hid = hid
        for _ in range(5):
            params = {"startHistoryId": hid, "historyTypes": "messageAdded",
                      "labelId": "INBOX", "maxResults": 100}
            if page:
                params["pageToken"] = page
            with http_client() as c:
                resp = c.get(f"{GMAIL}/history", params=params,
                             headers={"Authorization": f"Bearer {access_token()}"})
            if resp.status_code == 404:
                # historyId слишком старый (ядро долго было выключено) —
                # начинаем с текущего, пропущенное не догоняем
                prof = _get(f"{GMAIL}/profile").json()
                store.update_state("gmail", {"history_id": prof.get("historyId")})
                return []
            raise_for(resp, "google")
            data = resp.json()
            new_hid = data.get("historyId") or new_hid
            for h in data.get("history") or []:
                for added in h.get("messagesAdded") or []:
                    mid = (added.get("message") or {}).get("id")
                    if mid and mid not in ids:
                        ids.append(mid)
            page = data.get("nextPageToken")
            if not page:
                break
        events = []
        for mid in ids[-MAX_NEW_PER_POLL:]:
            try:
                msg = _get(f"{GMAIL}/messages/{mid}", params={"format": "full"}).json()
            except SourceError as e:
                if "404" in str(e):
                    continue  # письмо уже удалено
                raise
            if "UNREAD" not in (msg.get("labelIds") or []):
                continue  # прочитано раньше, чем мы увидели
            ev = message_event(msg)
            if ev:
                events.append(ev)
        store.update_state("gmail", {"history_id": new_hid})
        return events

    def gone(self, items: List[dict]) -> set:
        out = set()
        for it in items:
            mid = (it.get("meta") or {}).get("message_id")
            if it.get("source") != "gmail" or not mid:
                continue
            try:
                msg = _get(f"{GMAIL}/messages/{mid}", params={"format": "minimal"}).json()
            except Exception:
                continue
            if "UNREAD" not in (msg.get("labelIds") or []):
                out.add(it["id"])
        return out


def gmail_search(query: str, limit: int = 8) -> List[dict]:
    """Письма по запросу Gmail (from:, subject:, is:unread …): кратко."""
    from concurrent.futures import ThreadPoolExecutor
    data = _get(f"{GMAIL}/messages", params={"q": query, "maxResults": limit}).json()
    ids = [m["id"] for m in data.get("messages") or []]

    def meta(mid):
        return _get(f"{GMAIL}/messages/{mid}", params={
            "format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]}).json()

    # Ответ ждут в чате: метаданные писем — параллельно, порядок — как в выдаче
    with ThreadPoolExecutor(max_workers=5) as pool:
        msgs = list(pool.map(meta, ids))
    out = []
    for m, msg in zip(data.get("messages") or [], msgs):
        h = _headers(msg.get("payload") or {})
        name, email = parseaddr(h.get("from", ""))
        out.append({"id": m["id"], "from": name or email, "email": email,
                    "subject": h.get("subject", ""), "snippet": html.unescape(msg.get("snippet", "")),
                    "date": _mail_ts(h.get("date", "")),
                    "unread": "UNREAD" in (msg.get("labelIds") or []),
                    "url": gmail_link(str(msg.get("threadId") or m["id"]))})
    return out


# ── Google Диск ────────────────────────────────────────────────────────

_FILE_FIELDS = ("id,name,mimeType,webViewLink,modifiedTime,trashed,sharedWithMeTime,ownedByMe,"
                "sharingUser(displayName,emailAddress,me),"
                "lastModifyingUser(displayName,emailAddress,me),owners(displayName,me)")


def _iso_ts(value: Optional[str]) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class DriveSource(PollSource):
    name = "drive"

    def __init__(self, settings_getter=None):
        self._settings = settings_getter or store.load_settings

    def configured(self) -> bool:
        sec = store.load_secrets("google")
        return bool(sec.get("refresh_token")) and \
            "https://www.googleapis.com/auth/drive.readonly" in (sec.get("scopes") or [])

    def check(self) -> dict:
        about = _get(f"{DRIVE}/about", params={"fields": "user(emailAddress)"}).json()
        return {"ok": True, "account": (about.get("user") or {}).get("emailAddress")}

    def poll(self) -> List[Event]:
        st = store.load_state("drive")
        token = st.get("page_token")
        if not token:
            data = _get(f"{DRIVE}/changes/startPageToken").json()
            store.update_state("drive", {"page_token": data.get("startPageToken"),
                                         "since": time.time()})
            return []
        since = float(st.get("since") or time.time())
        opts = self._settings()["services"]["drive"]
        events: List[Event] = []
        new_token = token
        for _ in range(10):
            data = _get(f"{DRIVE}/changes", params={
                "pageToken": token, "pageSize": 100, "spaces": "drive",
                "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,time,file({_FILE_FIELDS}))",
            }).json()
            for ch in data.get("changes") or []:
                ev = self._change_event(ch, since, opts)
                if ev:
                    events.append(ev)
            if data.get("newStartPageToken"):
                new_token = data["newStartPageToken"]
                break
            token = data.get("nextPageToken")
            if not token:
                break
            new_token = token
        store.update_state("drive", {"page_token": new_token, "since": time.time() - 120})
        return events

    @staticmethod
    def _change_event(ch: dict, since: float, opts: dict) -> Optional[Event]:
        f = ch.get("file") or {}
        if ch.get("removed") or not f or f.get("trashed"):
            return None
        name = f.get("name") or "(без названия)"
        shared_ts = _iso_ts(f.get("sharedWithMeTime"))
        if opts.get("shared", True) and shared_ts >= since and not f.get("ownedByMe"):
            who = f.get("sharingUser") or {}
            return Event(source="drive", key=f"shared:{f['id']}", kind="shared",
                         title=name, sender=who.get("displayName") or who.get("emailAddress") or "",
                         url=f.get("webViewLink", ""), ts=shared_ts,
                         meta={"email": (who.get("emailAddress") or "").lower(),
                               "mime": f.get("mimeType"), "file_id": f["id"]},
                         group=name)
        editor = f.get("lastModifyingUser") or {}
        mod_ts = _iso_ts(f.get("modifiedTime"))
        if opts.get("edits", True) and f.get("ownedByMe") and editor and not editor.get("me") \
                and mod_ts >= since:
            # Одна запись на файл в час: серия правок — одно событие
            bucket = int(mod_ts // 3600)
            return Event(source="drive", key=f"edit:{f['id']}:{bucket}", kind="edited",
                         title=name, sender=editor.get("displayName") or editor.get("emailAddress") or "",
                         url=f.get("webViewLink", ""), ts=mod_ts,
                         meta={"email": (editor.get("emailAddress") or "").lower(),
                               "mime": f.get("mimeType"), "file_id": f["id"]},
                         group=name)
        return None


def _q_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def drive_search(query: str, limit: int = 8) -> List[dict]:
    """Файлы Диска по названию или тексту, свежие сверху."""
    q = _q_escape(query.strip())
    data = _get(f"{DRIVE}/files", params={
        "q": f"(name contains '{q}' or fullText contains '{q}') and trashed = false",
        "pageSize": limit, "orderBy": "modifiedTime desc",
        "fields": "files(id,name,mimeType,modifiedTime,webViewLink,owners(displayName))",
    }).json()
    out = []
    for f in data.get("files") or []:
        out.append({"id": f["id"], "name": f.get("name", ""), "mime": f.get("mimeType", ""),
                    "modified": _iso_ts(f.get("modifiedTime")), "url": f.get("webViewLink", ""),
                    "owner": ((f.get("owners") or [{}])[0]).get("displayName", "")})
    # Совпадение в названии — выше совпадения в тексте
    low = query.strip().lower()
    out.sort(key=lambda f: 0 if low in f["name"].lower() else 1)
    return out


_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
_DOWNLOAD_MAX = 10 * 1024 * 1024


def drive_read(file_id: str, mime: str = "") -> str:
    """Текст файла Диска: Документы/Презентации — текстом, Таблицы — CSV
    (первый лист), PDF/Word и текстовые — по содержимому."""
    if not re.match(r"^[\w-]{10,200}$", file_id or ""):
        raise ValueError("bad file id")
    if not mime:
        mime = _get(f"{DRIVE}/files/{file_id}", params={"fields": "mimeType"}).json().get("mimeType", "")
    if mime in _EXPORT:
        resp = _get(f"{DRIVE}/files/{file_id}/export", params={"mimeType": _EXPORT[mime]}, timeout=60)
        return resp.text[:_READ_MAX]
    if mime.startswith("application/vnd.google-apps"):
        return ""
    resp = _get(f"{DRIVE}/files/{file_id}", params={"alt": "media"}, timeout=60)
    content = resp.content[:_DOWNLOAD_MAX]
    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        return content.decode("utf-8", errors="replace")[:_READ_MAX]
    # PDF, Word и прочее — через общий разбор файлов ядра
    from app.core.file_reader import extract_text
    ext = {"application/pdf": "pdf",
           "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
           "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx"
           }.get(mime)
    if not ext:
        return ""
    return (extract_text(content, f"file.{ext}") or "")[:_READ_MAX]

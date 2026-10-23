"""API раздела «Сервисы» (app/integrations): статус и настройки, вход в
Google, GitHub, Telegram и почту, лента событий с оценками, правила,
проверка уведомлений и приём вебхуков.

router — под авторизацией ядра (server.py подключает его с require_auth);
hooks_router — POST /api/hooks/<токен> без неё: внешний сервис токена API
не знает, ключ — токен в адресе. Секреты наружу не отдаются: только
признак «задан» и маска. Адрес вебхука по каналу VPC Link не показывается
(как токен API).
"""

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.integrations import store, triage
from app.integrations.feed import feed
from app.integrations.hub import hub
from app.integrations.sources import AuthError, SourceError
from app.integrations.sources import github as gh_src
from app.integrations.sources import google as google_src
from app.integrations.sources import imap_mail, rss as rss_src, telegram_user, webhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations")
hooks_router = APIRouter()

_ACCOUNTS = ("google", "github", "telegram", "imap", "rss", "webhook")
_RULE_ID_RE = r"^[a-f0-9]{8,32}$"
_ITEM_ID_RE = r"^[a-f0-9]{16}$"


def _via_link(request: Request) -> bool:
    return bool(request.headers.get("x-vpc-link"))


def _err(e: Exception) -> str:
    return str(e)[:300]


# ── Статус ──────────────────────────────────────────────────────────────

def _personas() -> List[dict]:
    from app.api import runtime
    out = []
    for pid in runtime.list_personas():
        info = runtime.get_persona_info(pid) or {}
        out.append({"id": pid, "name": info.get("name") or pid,
                    "muted": bool((info.get("features") or {}).get("muted"))})
    return out


def _service_state(name: str) -> dict:
    st = store.load_state(name)
    return {"last_ok": st.get("last_ok"), "last_error": st.get("last_error") or "",
            "auth_error": st.get("auth_error") or ""}


def status(request: Request) -> dict:
    settings = store.load_settings()
    g = store.load_secrets("google")
    gh = store.load_secrets("github")
    tg = store.load_secrets("telegram")
    im = store.load_secrets("imap")
    wh_token = webhook.token()
    persona = None
    try:
        persona = hub.persona()
    except Exception:
        pass
    token, owner = hub.telegram_target(persona) if persona else ("", "")
    scopes = g.get("scopes") or []
    items = feed.items()
    day_ago = time.time() - 86400
    return {
        "hub": {"started": hub.started, "active": hub.active},
        "settings": settings,
        "personas": _personas(),
        "persona": persona,
        "telegram_delivery": {"token": bool(token), "owner": bool(owner)},
        "accounts": {
            "google": {
                "client": bool(g.get("client_id") and g.get("client_secret")),
                "client_id": g.get("client_id", ""),
                "connected": bool(g.get("refresh_token")),
                "email": g.get("email", ""),
                "gmail": "https://www.googleapis.com/auth/gmail.readonly" in scopes,
                "drive": "https://www.googleapis.com/auth/drive.readonly" in scopes,
                "auth_error": store.load_state("google").get("auth_error") or "",
            },
            "github": {"connected": bool(gh.get("token")), "login": gh.get("login", ""),
                       "token_hint": store.mask(gh.get("token"))},
            "telegram": {"available": telegram_user.available(),
                         "connected": bool(tg.get("session")),
                         "user": tg.get("user") or None,
                         "api_id": bool(tg.get("api_id")),
                         "login_step": _tg_step(),
                         "running": bool(hub.telegram.client is not None) if hub.active else None},
            "imap": {"connected": bool(im.get("host") and im.get("password")),
                     "user": im.get("user", ""), "host": im.get("host", ""),
                     "port": im.get("port") or 993, "folder": im.get("folder", "INBOX")},
            "webhook": {"token": bool(wh_token),
                        "path": (f"/api/hooks/{wh_token}" if wh_token and not _via_link(request)
                                 else ("/api/hooks/" + store.mask(wh_token) if wh_token else ""))},
        },
        "status": {name: _service_state(name)
                   for name in ("gmail", "drive", "github", "telegram", "imap", "rss")},
        "counts": {
            "queued": sum(1 for i in items if i.get("status") == "queued"),
            "digest": sum(1 for i in items if i.get("status") == "digest"),
            "today": sum(1 for i in items if float(i.get("received") or 0) >= day_ago),
            "notified_today": sum(1 for i in items if i.get("status") == "notified"
                                  and float(i.get("notified_at") or 0) >= day_ago),
        },
        "now": time.time(),
    }


def _tg_step() -> Optional[str]:
    lg = hub.tg_login
    if lg.client is None or time.time() > lg.expires:
        return None
    return "code"


@router.get("")
async def get_status(request: Request):
    return await asyncio.to_thread(status, request)


# ── Настройки ───────────────────────────────────────────────────────────

class SettingsPatch(BaseModel):
    notify: Optional[Dict[str, Any]] = None
    services: Optional[Dict[str, Dict[str, Any]]] = None


def _overlay(target: dict, patch: dict) -> None:
    # Только ключи, которые есть в настройках; вложенные dict — рекурсивно.
    # Типы и границы доводит normalize_settings
    for key, val in patch.items():
        if key not in target:
            continue
        if isinstance(target[key], dict) and isinstance(val, dict):
            _overlay(target[key], val)
        else:
            target[key] = val


@router.put("/settings")
async def put_settings(patch: SettingsPatch):
    if patch.notify and "persona" in patch.notify and patch.notify["persona"]:
        pid = str(patch.notify["persona"])
        if pid not in {p["id"] for p in await asyncio.to_thread(_personas)}:
            raise HTTPException(404, f"Персона '{pid}' не найдена")

    def mutate(s):
        if patch.notify:
            _overlay(s["notify"], patch.notify)
        if patch.services:
            _overlay(s["services"], patch.services)

    s = await asyncio.to_thread(store.update_settings, mutate)
    return {"ok": True, "settings": s}


class RuleIn(BaseModel):
    action: Literal["urgent", "always", "digest", "never"]
    source: str = "any"
    match: str = Field(min_length=1, max_length=200)


@router.post("/rules")
async def add_rule(rule: RuleIn):
    norm = store.normalize_rule(rule.model_dump())
    if norm is None:
        raise HTTPException(422, "пустое правило")

    def mutate(s):
        s["rules"].insert(0, norm)

    await asyncio.to_thread(store.update_settings, mutate)
    return {"ok": True, "rule": norm}


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str):
    if not re.match(_RULE_ID_RE, rule_id):
        raise HTTPException(422, "bad id")
    removed = []

    def mutate(s):
        before = len(s["rules"])
        s["rules"] = [r for r in s["rules"] if r["id"] != rule_id]
        removed.append(before != len(s["rules"]))

    await asyncio.to_thread(store.update_settings, mutate)
    if not removed[0]:
        raise HTTPException(404, "правило не найдено")
    return {"ok": True}


@router.post("/examples/clear")
async def clear_examples():
    await asyncio.to_thread(store.update_settings, lambda s: s.update(examples=[]))
    return {"ok": True}


@router.post("/dnd")
async def set_dnd(minutes: int = Body(0, embed=True, ge=0, le=48 * 60)):
    until = time.time() + minutes * 60 if minutes else 0
    await asyncio.to_thread(store.update_settings, lambda s: s.update(dnd_until=until))
    return {"ok": True, "dnd_until": until}


# ── Лента ───────────────────────────────────────────────────────────────

def _public_item(it: dict) -> dict:
    out = {k: it.get(k) for k in ("id", "source", "kind", "title", "sender", "url", "ts",
                                  "received", "group", "level", "summary", "reason", "by",
                                  "status", "notified_at", "feedback")}
    out["text"] = str(it.get("text") or "")[:800]
    return out


@router.get("/feed")
async def get_feed(limit: int = Query(80, ge=1, le=400),
                   source: Optional[str] = Query(None, max_length=20)):
    sources = [source] if source else None
    items = await asyncio.to_thread(feed.recent, limit, sources)
    return {"items": [_public_item(i) for i in items]}


class FeedbackIn(BaseModel):
    level: Literal["urgent", "notify", "digest", "ignore"]


@router.post("/feed/{item_id}/feedback")
async def item_feedback(item_id: str, body: FeedbackIn):
    if not re.match(_ITEM_ID_RE, item_id):
        raise HTTPException(422, "bad id")
    item = await asyncio.to_thread(feed.get, item_id)
    if item is None:
        raise HTTPException(404, "событие не найдено")
    rule = await asyncio.to_thread(triage.record_feedback, item, body.level)
    await asyncio.to_thread(feed.update, [item_id], feedback=body.level)
    return {"ok": True, "rule": rule}


@router.post("/feed/clear")
async def clear_feed():
    await asyncio.to_thread(feed.clear)
    return {"ok": True}


@router.post("/test-notify")
async def test_notify():
    res = await asyncio.to_thread(hub.test_notify)
    if res is None:
        return {"ok": False, "detail": "busy"}
    return {"ok": bool(res)}


@router.post("/digest-now")
async def digest_now():
    def run():
        text, items = hub.notifier.digest_now(mark=False)
        if not items:
            return {"ok": True, "sent": 0}
        res = hub.deliver(text, items, "digest")
        if res:
            feed.update([i["id"] for i in items], status="digested", notified_at=time.time())
        return {"ok": bool(res), "sent": len(items) if res else 0}
    return await asyncio.to_thread(run)


# ── Проверка и опрос ────────────────────────────────────────────────────

_CHECKS = {
    "gmail": lambda: google_src.GmailSource().check(),
    "drive": lambda: google_src.DriveSource().check(),
    "github": lambda: gh_src.GitHubSource().check(),
    "imap": lambda: imap_mail.ImapSource().check(),
    "rss": lambda: rss_src.RssSource().check(),
}


@router.post("/{service}/check")
async def check_service(service: str):
    if service == "telegram":
        st = hub.telegram.status if hub.active else {}
        connected = telegram_user.configured()
        return {"ok": connected and bool(st.get("running", not hub.active)),
                "account": (store.load_secrets("telegram").get("user") or {}).get("name"),
                "error": st.get("error") or ("" if connected else "не подключён")}
    fn = _CHECKS.get(service)
    if fn is None:
        raise HTTPException(404, "нет такого сервиса")
    try:
        res = await asyncio.to_thread(fn)
        store.update_state(service, {"last_error": "", "auth_error": ""})
        return res
    except AuthError as e:
        return {"ok": False, "error": _err(e), "auth": True}
    except (SourceError, ValueError, OSError) as e:
        return {"ok": False, "error": _err(e)}


@router.post("/{service}/poll")
async def poll_service(service: str):
    if service not in hub.polls:
        raise HTTPException(404, "нет такого сервиса")
    hub.poll_now(service)
    return {"ok": True, "active": hub.active}


@router.delete("/{account}")
async def disconnect(account: str):
    if account not in _ACCOUNTS:
        raise HTTPException(404, "нет такого сервиса")
    if account == "telegram":
        await asyncio.to_thread(_telegram_logout)
    if account == "rss":
        await asyncio.to_thread(store.update_settings,
                                lambda s: s["services"]["rss"].update(feeds=[], enabled=False))
        await asyncio.to_thread(store.update_state, "rss", None)
        return {"ok": True}
    await asyncio.to_thread(store.forget_service, account)
    return {"ok": True}


def _telegram_logout() -> None:
    # Сеанс завершается и на стороне Telegram — не висит в «Активных сеансах»
    sec = store.load_secrets("telegram")
    if not sec.get("session") or hub.loop is None or not telegram_user.available():
        return

    async def run():
        await hub.telegram.stop()
        client = telegram_user._client(sec["session"], sec["api_id"], sec["api_hash"])
        try:
            await client.connect()
            if await client.is_user_authorized():
                await client.log_out()
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    try:
        hub.run(run(), timeout=30)
    except Exception as e:
        logger.info(f"[Integrations] выход из Telegram: {e}")


# ── Google ──────────────────────────────────────────────────────────────

class GoogleClientIn(BaseModel):
    client_id: str = Field("", max_length=300)
    client_secret: str = Field("", max_length=300)
    json_text: str = Field("", max_length=20000)


@router.post("/google/client")
async def google_client(body: GoogleClientIn):
    try:
        client = google_src.parse_client(body.json_text or {"client_id": body.client_id,
                                                           "client_secret": body.client_secret})
    except ValueError as e:
        raise HTTPException(422, str(e))
    old = store.load_secrets("google")
    if old.get("client_id") and old.get("client_id") != client["client_id"]:
        # Другой клиент — прежние токены от него не подойдут
        await asyncio.to_thread(store.forget_service, "google")
    await asyncio.to_thread(store.update_secrets, "google", client)
    return {"ok": True}


@router.post("/google/login")
async def google_login():
    try:
        url = await asyncio.to_thread(google_src.start_login)
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e))
    return {"url": url}


@router.get("/google/login")
async def google_login_result():
    return {"result": google_src.login_result(), "connected": google_src.connected(),
            "email": google_src.account()}


@router.post("/google/finish")
async def google_finish(url: str = Body(..., embed=True, max_length=4000)):
    res = await asyncio.to_thread(google_src.finish_from_url, url)
    return res


# ── GitHub ──────────────────────────────────────────────────────────────

@router.post("/github/token")
async def github_token(token: str = Body(..., embed=True, max_length=300)):
    token = token.strip()
    if not gh_src.valid_token(token):
        raise HTTPException(422, "не похоже на токен GitHub (ghp_…, github_pat_…)")
    try:
        info = await asyncio.to_thread(gh_src.GitHubSource().check, token)
    except (AuthError, SourceError) as e:
        return {"ok": False, "error": _err(e)}
    await asyncio.to_thread(store.update_secrets, "github",
                            {"token": token, "login": info.get("account") or ""}, True)
    await asyncio.to_thread(store.update_state, "github", None)

    def enable(s):
        s["services"]["github"]["enabled"] = True

    await asyncio.to_thread(store.update_settings, enable)
    return {"ok": True, "login": info.get("account")}


# ── Telegram ────────────────────────────────────────────────────────────

class TgLoginIn(BaseModel):
    api_id: int = Field(..., ge=1, le=2 ** 31)
    api_hash: str = Field(..., min_length=16, max_length=64, pattern=r"^[a-fA-F0-9]+$")
    phone: str = Field(..., min_length=6, max_length=20, pattern=r"^\+?[0-9 ()-]{6,20}$")


def _tg_ready():
    if not telegram_user.available():
        raise HTTPException(501, "нужен пакет telethon: pip install telethon")
    if hub.loop is None:
        raise HTTPException(503, "хаб сервисов не запущен")


@router.post("/telegram/login")
async def tg_login(body: TgLoginIn):
    _tg_ready()
    phone = re.sub(r"[^\d+]", "", body.phone)
    try:
        res = await asyncio.to_thread(hub.run, hub.tg_login.start(body.api_id, body.api_hash, phone), 60)
    except Exception as e:
        return {"ok": False, "error": _err(e)}
    return res


@router.post("/telegram/code")
async def tg_code(code: str = Body(..., embed=True, min_length=3, max_length=12)):
    _tg_ready()
    try:
        res = await asyncio.to_thread(hub.run, hub.tg_login.code(code), 60)
    except Exception as e:
        return {"ok": False, "error": _err(e)}
    if res.get("ok") and res.get("user"):
        await asyncio.to_thread(_enable, "telegram")
    return res


@router.post("/telegram/password")
async def tg_password(password: str = Body(..., embed=True, min_length=1, max_length=256)):
    _tg_ready()
    try:
        res = await asyncio.to_thread(hub.run, hub.tg_login.password(password), 60)
    except Exception as e:
        return {"ok": False, "error": _err(e)}
    if res.get("ok") and res.get("user"):
        await asyncio.to_thread(_enable, "telegram")
    return res


def _enable(service: str) -> None:
    store.update_settings(lambda s: s["services"][service].update(enabled=True))


# ── Почта IMAP ──────────────────────────────────────────────────────────

class ImapIn(BaseModel):
    user: str = Field(..., min_length=3, max_length=200)
    password: str = Field(..., min_length=1, max_length=300)
    host: str = Field("", max_length=253)
    port: int = Field(0, ge=0, le=65535)
    folder: str = Field("INBOX", max_length=100)


@router.post("/imap")
async def imap_connect(body: ImapIn):
    try:
        acc = imap_mail.normalize_account(body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e))
    try:
        await asyncio.to_thread(imap_mail.ImapSource().check, acc)
    except (AuthError, SourceError) as e:
        return {"ok": False, "error": _err(e)}
    await asyncio.to_thread(store.update_secrets, "imap", acc, True)
    await asyncio.to_thread(store.update_state, "imap", None)
    await asyncio.to_thread(_enable, "imap")
    return {"ok": True, "host": acc["host"]}


@router.get("/imap/preset")
async def imap_preset(user: str = Query(..., max_length=200)):
    preset = imap_mail.preset_for(user)
    return {"host": preset[0], "port": preset[1]} if preset else {"host": "", "port": 993}


# ── RSS и вебхук ────────────────────────────────────────────────────────

@router.post("/rss/check")
async def rss_check(url: str = Body(..., embed=True, max_length=500)):
    if not re.match(r"^https?://\S+$", url.strip()):
        raise HTTPException(422, "нужен адрес http(s)://")
    return await asyncio.to_thread(rss_src.RssSource().check, url.strip())


@router.post("/webhook/regenerate")
async def webhook_regenerate(request: Request):
    if _via_link(request):
        raise HTTPException(403, "Новый адрес вебхука — только с компьютера ядра")
    tok = await asyncio.to_thread(webhook.regenerate)
    await asyncio.to_thread(_enable, "webhook")
    return {"ok": True, "path": f"/api/hooks/{tok}"}


@hooks_router.post("/api/hooks/{token}")
async def incoming_hook(token: str, request: Request):
    if not webhook.token_ok(token):
        raise HTTPException(404, "Not Found")
    settings = await asyncio.to_thread(store.load_settings)
    if not settings["services"]["webhook"]["enabled"]:
        raise HTTPException(409, "вебхук выключен")
    if not webhook.rate_ok():
        raise HTTPException(429, "слишком часто")
    body = await request.body()
    if len(body) > webhook.MAX_BODY:
        raise HTTPException(413, "слишком большое тело")
    ev = webhook.parse(body, request.headers.get("content-type", ""))
    added = await asyncio.to_thread(hub.ingest, [ev])
    return {"ok": True, "accepted": bool(added)}

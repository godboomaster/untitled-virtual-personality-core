"""
Перенос сервисов с телефона на компьютер.

Приложение на телефоне без компьютера само проверяет ленты, GitHub, почту
по IMAP и Gmail (web/android/.../PhoneServices.java). Появился компьютер —
телефон один раз предлагает перенести их сюда (web/src/offline/phoneServices.ts,
POST /api/integrations/import): настройки, ключи доступа, курсоры источников
(«что уже видел»), правила и ленту. Дальше главный — компьютер: телефон
перенесённые источники больше не проверяет.

Компьютер главный и при слиянии:
  - что здесь уже подключено своим аккаунтом, не заменяется (skipped с
    причиной); тот же аккаунт — только курсор подтягивается вперёд;
  - ленты и правила складываются (повторы — один раз);
  - курсоры такие, чтобы старое не пришло второй раз: «уже видел» ленты
    складывается с тем, что видел компьютер; GitHub — с самого позднего
    момента; IMAP — с последнего UID, если ящик тот же (UIDVALIDITY);
    Gmail — с historyId телефона;
  - настройки уведомлений телефона ложатся только туда, где у компьютера
    ещё умолчание;
  - записи ленты телефона встают в ленту (ждущие сводки и отправки — как
    ждущие: дошлёт уже компьютер), их id — в «уже видел» ленты.

Ключи у телефона и ядра одни и те же: запись ленты RSS — sha1(url\\nid)[:20],
событие — sha1(source:key)[:16] (feed.event_id).
"""

import json
import logging
import re
import time
from typing import Iterable, List, Optional

from app.integrations import store
from app.integrations.feed import feed
from app.integrations.sources import github as gh_src
from app.integrations.sources import google as google_src
from app.integrations.sources import imap_mail
from app.integrations.sources import rss as rss_src

logger = logging.getLogger(__name__)

_RSS_KEY_RE = re.compile(r"^[a-f0-9]{20}$")
_NOTIFY_KEYS = ("quiet", "digest_times", "max_per_hour", "language")
_GOOGLE_SCOPES = {"openid", "email", "https://www.googleapis.com/auth/userinfo.email",
                  "https://www.googleapis.com/auth/gmail.readonly",
                  "https://www.googleapis.com/auth/drive.readonly"}

_SKIP = {
    "github_other": "на компьютере подключён другой токен GitHub",
    "github_bad": "не похоже на токен GitHub",
    "imap_other": "на компьютере подключён другой ящик",
    "imap_bad": "неполные данные ящика",
    "google_other": "на компьютере подключён другой аккаунт Google",
    "google_bad": "неверный ключ клиента Google",
}


def _enable(service: str) -> None:
    store.update_settings(lambda s: s["services"][service].update(enabled=True))


def _rss(rss: dict, moved: List[str]) -> None:
    feeds = [f for f in rss.get("feeds") or []
             if re.match(r"^https?://\S+$", str(f.get("url") or "").strip())]
    if not feeds:
        return

    def mutate(s):
        cur = s["services"]["rss"]["feeds"]
        have = {f["url"] for f in cur}
        for f in feeds:
            url = f["url"].strip()
            if url not in have:
                cur.append({"url": url, "title": str(f.get("title") or "")[:120]})
                have.add(url)
        if rss.get("enabled", True):
            s["services"]["rss"]["enabled"] = True

    store.update_settings(mutate)
    per = dict(store.load_state("rss").get("feeds") or {})
    for f in feeds:
        url = f["url"].strip()
        cur = dict(per.get(url) or {})
        seen = [k for k in cur.get("seen") or [] if isinstance(k, str)]
        have = set(seen)
        add = [k for k in f.get("seen") or [] if isinstance(k, str) and _RSS_KEY_RE.match(k) and k not in have]
        cur["seen"] = (seen + add)[-rss_src.MAX_SEEN:]
        # Телефон уже читал ленту — первый опрос компьютера не «запоминает», а сравнивает
        if f.get("seen_init") or cur.get("seen_init"):
            cur["seen_init"] = True
        if not cur.get("etag") and not cur.get("modified"):
            cur["etag"] = str(f.get("etag") or "")[:300]
            cur["modified"] = str(f.get("modified") or "")[:100]
        cur.setdefault("title", str(f.get("title") or "")[:200])
        per[url] = cur
    store.update_state("rss", {"feeds": per})
    moved.append("rss")


def _github(gh: dict, moved: List[str], skipped: dict) -> None:
    token = str(gh.get("token") or "").strip()
    if not gh_src.valid_token(token):
        skipped["github"] = _SKIP["github_bad"]
        return
    cur = store.load_secrets("github")
    if cur.get("token") and cur["token"] != token:
        skipped["github"] = _SKIP["github_other"]
        return
    since = float(gh.get("since") or 0)
    if cur.get("token"):
        # Тот же токен уже здесь — курсор только вперёд
        st = store.load_state("github")
        if since > float(st.get("since") or 0):
            store.update_state("github", {"since": since})
    else:
        store.update_secrets("github", {"token": token, "login": str(gh.get("login") or "")[:100]}, True)
        store.update_state("github", {"since": since, "last_modified": str(gh.get("last_modified") or "")[:100]}
                           if since else None, replace=True)
    if gh.get("enabled", True):
        _enable("github")
    moved.append("github")


def _imap(im: dict, moved: List[str], skipped: dict) -> None:
    try:
        acc = imap_mail.normalize_account(im)
    except (ValueError, TypeError):
        skipped["imap"] = _SKIP["imap_bad"]
        return
    cur = store.load_secrets("imap")
    connected = bool(cur.get("host") and cur.get("password"))
    same = connected and str(cur.get("user") or "").lower() == acc["user"].lower() \
        and str(cur.get("host") or "").lower() == acc["host"]
    if connected and not same:
        skipped["imap"] = _SKIP["imap_other"]
        return
    validity = str(im.get("validity") or "")
    last_uid = int(im.get("last_uid") or 0)
    if not connected:
        store.update_secrets("imap", acc, True)
        store.update_state("imap", {"validity": validity, "last_uid": last_uid} if validity else None,
                           replace=True)
    else:
        st = store.load_state("imap")
        if validity and st.get("validity") == validity and last_uid > int(st.get("last_uid") or 0):
            store.update_state("imap", {"last_uid": last_uid})
    if im.get("enabled", True):
        _enable("imap")
    moved.append("imap")


def _google(g: dict, moved: List[str], skipped: dict) -> None:
    try:
        client = google_src.parse_client({"client_id": g.get("client_id"),
                                          "client_secret": g.get("client_secret")})
    except ValueError:
        skipped["gmail"] = _SKIP["google_bad"]
        return
    refresh = str(g.get("refresh_token") or "").strip()
    email = str(g.get("email") or "")[:320]
    hid = str(g.get("history_id") or "")
    cur = store.load_secrets("google")
    if cur.get("refresh_token"):
        if str(cur.get("email") or "").lower() != email.lower() or not email:
            skipped["gmail"] = _SKIP["google_other"]
            return
        # Тот же аккаунт уже здесь — курсор Gmail только вперёд
        st = store.load_state("gmail")
        if hid and (not str(st.get("history_id") or "").isdigit() or int(hid) > int(st["history_id"])):
            store.update_state("gmail", {"history_id": hid})
        moved.append("gmail")
        return
    scopes = sorted({str(s) for s in g.get("scopes") or []} & _GOOGLE_SCOPES)
    store.update_secrets("google", {
        **client, "refresh_token": refresh, "access_token": "", "expires_at": 0,
        "email": email, "scopes": scopes, "connected_at": time.time()}, True)
    store.update_state("gmail", {"history_id": hid} if hid else None, replace=True)
    store.update_state("drive", None)
    store.update_state("google", {"auth_error": "", "auth_notified": False})
    if g.get("enabled", True) and "https://www.googleapis.com/auth/gmail.readonly" in scopes:
        _enable("gmail")
    moved.append("gmail")


def _rules(rules: Iterable[dict]) -> int:
    added = []

    def mutate(s):
        have = {(r["action"], r["source"], r["match"].lower()) for r in s["rules"]}
        for raw in rules:
            rule = store.normalize_rule(raw)
            if rule is None:
                continue
            key = (rule["action"], rule["source"], rule["match"].lower())
            if key in have:
                continue
            have.add(key)
            s["rules"].append(rule)
            added.append(rule)

    store.update_settings(mutate)
    return len(added)


def _notify(n: dict, personas: Iterable[str]) -> List[str]:
    """Настройки уведомлений телефона — туда, где у компьютера умолчание."""
    applied: List[str] = []
    defaults = store.DEFAULT_SETTINGS["notify"]
    personas = set(personas)

    def mutate(s):
        cur = s["notify"]
        for key in _NOTIFY_KEYS:
            if key in n and n[key] is not None and cur[key] == defaults[key] and n[key] != cur[key]:
                cur[key] = n[key]
                applied.append(key)
        pid = n.get("persona")
        if pid and not cur.get("persona") and pid in personas:
            cur["persona"] = pid
            applied.append("persona")

    store.update_settings(mutate)
    return applied


def import_from_phone(data: dict, personas: Optional[Iterable[str]] = None) -> dict:
    """Слить сервисы телефона в ядро. → {ok, moved, skipped, rules, items, notify}."""
    moved: List[str] = []
    skipped: dict = {}
    if data.get("rss"):
        _rss(data["rss"], moved)
    if data.get("github"):
        _github(data["github"], moved, skipped)
    if data.get("imap"):
        _imap(data["imap"], moved, skipped)
    if data.get("google"):
        _google(data["google"], moved, skipped)
    rules = _rules(data.get("rules") or [])
    applied = _notify(data.get("notify") or {}, personas or [])
    items = feed.import_records(data.get("items") or [], data.get("seen_ids") or [])
    logger.info(f"[Integrations] перенос с телефона: {moved or '—'}"
                + (f", пропущено {sorted(skipped)}" if skipped else "")
                + f", правил +{rules}, записей ленты +{items}")
    return {"ok": True, "moved": moved, "skipped": skipped, "rules": rules, "items": items,
            "notify": applied}


def clean_meta(meta) -> dict:
    """meta записи ленты с телефона: только JSON-простые значения, немного."""
    if not isinstance(meta, dict):
        return {}
    out = {}
    for k, v in list(meta.items())[:20]:
        if not isinstance(k, str) or len(k) > 40:
            continue
        if isinstance(v, list):
            v = [str(x)[:100] for x in v[:20]]
        elif v is not None and not isinstance(v, (str, int, float, bool)):
            continue
        elif isinstance(v, str):
            v = v[:300]
        out[k] = v
    return out if len(json.dumps(out, ensure_ascii=False)) <= 4000 else {}

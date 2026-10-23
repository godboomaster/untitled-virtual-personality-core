"""
GitHub: уведомления пользователя (GET /notifications) по личному токену.

Токен — fine-grained или classic с правом notifications (для приватных
репозиториев — и repo). Опрос с If-Modified-Since: без новостей GitHub
отвечает 304 и не тратит лимит; интервал не короче X-Poll-Interval.
Уведомления не помечаются прочитанными — это делает сам пользователь.

У уведомления есть причина (reason): review_requested, mention, assign,
ci_activity, security_alert, subscribed … — по ней разбор решает, стоит
ли прерывать. Для упоминаний и комментариев подтягивается текст
последнего комментария: модели разбора нужен смысл, а не только заголовок.
"""

import logging
import re
import time
from datetime import datetime
from typing import List, Optional

from app.integrations import store
from app.integrations.feed import Event
from app.integrations.sources import AuthError, PollSource, http, raise_for

logger = logging.getLogger(__name__)

API = "https://api.github.com"
_COMMENT_REASONS = {"mention", "team_mention", "comment", "author", "review_requested",
                    "assign", "manual"}
_TOKEN_RE = re.compile(r"^(gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})$")


def valid_token(token: str) -> bool:
    return bool(_TOKEN_RE.match(str(token or "").strip()))


def _headers(token: Optional[str] = None) -> dict:
    token = token or store.load_secrets("github").get("token", "")
    if not token:
        raise AuthError("github: нет токена")
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _iso_ts(value: Optional[str]) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def html_url(subject: dict, repo: str) -> str:
    """API-адрес предмета уведомления → страница на github.com."""
    url = str(subject.get("url") or "")
    m = re.match(r"^https://api\.github\.com/repos/([^/]+/[^/]+)/(pulls|issues|commits|releases)/([^/]+)$", url)
    if m:
        kind = {"pulls": "pull", "issues": "issues", "commits": "commit",
                "releases": "releases"}[m.group(2)]
        if kind == "releases":
            return f"https://github.com/{m.group(1)}/releases"
        return f"https://github.com/{m.group(1)}/{kind}/{m.group(3)}"
    if subject.get("type") == "CheckSuite":
        return f"https://github.com/{repo}/actions"
    if subject.get("type") == "RepositoryVulnerabilityAlert":
        return f"https://github.com/{repo}/security"
    if subject.get("type") == "Discussion":
        return f"https://github.com/{repo}/discussions"
    return f"https://github.com/{repo}" if repo else "https://github.com/notifications"


class GitHubSource(PollSource):
    name = "github"

    def __init__(self):
        self._poll_interval: Optional[float] = None

    def configured(self) -> bool:
        return bool(store.load_secrets("github").get("token"))

    def interval_hint(self) -> Optional[float]:
        return self._poll_interval

    def check(self, token: Optional[str] = None) -> dict:
        with http() as c:
            resp = c.get(f"{API}/user", headers=_headers(token))
        if resp.status_code == 401:
            raise AuthError("github: токен не принят")
        raise_for(resp, "github")
        user = resp.json()
        scopes = resp.headers.get("x-oauth-scopes")
        # Проба уведомлений: fine-grained токен без доступа к ним даёт 403
        with http() as c:
            probe = c.get(f"{API}/notifications", params={"per_page": 1}, headers=_headers(token))
        if probe.status_code in (401, 403):
            raise AuthError("github: у токена нет доступа к уведомлениям (notifications)")
        return {"ok": True, "account": user.get("login"), "name": user.get("name"),
                "scopes": scopes}

    def poll(self) -> List[Event]:
        st = store.load_state("github")
        headers = _headers()
        since = float(st.get("since") or 0)
        if not since:
            # Первый опрос: старые непрочитанные — не новость
            store.update_state("github", {"since": time.time()})
            return []
        if st.get("last_modified"):
            headers["If-Modified-Since"] = st["last_modified"]
        with http() as c:
            resp = c.get(f"{API}/notifications", params={"per_page": 50}, headers=headers)
        try:
            self._poll_interval = float(resp.headers.get("x-poll-interval") or 0) or None
        except ValueError:
            self._poll_interval = None
        if resp.status_code == 304:
            return []
        if resp.status_code == 401:
            raise AuthError("github: токен отозван или истёк")
        raise_for(resp, "github")
        threads = resp.json() if isinstance(resp.json(), list) else []
        events = []
        for th in threads:
            updated = _iso_ts(th.get("updated_at"))
            if updated <= since:
                continue
            ev = self._event(th)
            if ev:
                events.append(ev)
        newest = max([since] + [_iso_ts(t.get("updated_at")) for t in threads])
        store.update_state("github", {"since": newest,
                                      "last_modified": resp.headers.get("last-modified", "")})
        return events

    def _event(self, th: dict) -> Optional[Event]:
        subject = th.get("subject") or {}
        repo = (th.get("repository") or {}).get("full_name", "")
        reason = th.get("reason", "")
        sender, text, url = "", "", html_url(subject, repo)
        comment_url = subject.get("latest_comment_url")
        if comment_url and reason in _COMMENT_REASONS and comment_url != subject.get("url"):
            try:
                with http() as c:
                    r = c.get(comment_url, headers=_headers())
                if r.status_code < 400:
                    data = r.json()
                    sender = (data.get("user") or {}).get("login", "")
                    text = str(data.get("body") or "")[:1500]
                    url = data.get("html_url") or url
            except Exception as e:
                logger.debug(f"[GitHub] комментарий не получен: {e}")
        return Event(
            source="github", key=f"{th.get('id')}:{th.get('updated_at')}", kind=reason or "update",
            title=subject.get("title", ""), sender=sender, text=text, url=url,
            ts=_iso_ts(th.get("updated_at")),
            meta={"reason": reason, "type": subject.get("type"), "repo": repo,
                  "thread": str(th.get("id"))},
            group=repo)

    def gone(self, items: List[dict]) -> set:
        out = set()
        for it in items:
            thread = (it.get("meta") or {}).get("thread")
            if it.get("source") != "github" or not thread:
                continue
            try:
                with http() as c:
                    r = c.get(f"{API}/notifications/threads/{thread}", headers=_headers())
                if r.status_code < 400 and r.json().get("unread") is False:
                    out.add(it["id"])
            except Exception:
                continue
        return out


def overview(limit: int = 10) -> dict:
    """Для чата: непрочитанные уведомления и PR, где ждут вашего ревью."""
    headers = _headers()
    with http() as c:
        r = c.get(f"{API}/notifications", params={"per_page": limit}, headers=headers)
        raise_for(r, "github")
        notes = [{"repo": (t.get("repository") or {}).get("full_name", ""),
                  "title": (t.get("subject") or {}).get("title", ""),
                  "reason": t.get("reason", ""),
                  "url": html_url(t.get("subject") or {}, (t.get("repository") or {}).get("full_name", ""))}
                 for t in r.json()]
        reviews = []
        login = store.load_secrets("github").get("login")
        if login:
            r = c.get(f"{API}/search/issues", headers=headers, params={
                "q": f"is:open is:pr review-requested:{login} archived:false", "per_page": limit})
            if r.status_code < 400:
                reviews = [{"title": i.get("title", ""), "url": i.get("html_url", ""),
                            "repo": re.sub(r"^https://github\.com/([^/]+/[^/]+).*$", r"\1",
                                           i.get("html_url", ""))}
                           for i in r.json().get("items") or []]
    return {"notifications": notes, "reviews": reviews}

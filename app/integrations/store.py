"""
Хранилище сервисов: настройки, секреты и состояние источников.

Файлы в data/integrations/ читаются заново на каждую операцию под
межпроцессным локом (file_lock): настройки меняет веб (процесс API), а
читают и Telegram-боты в другом процессе (правила из чата, «не
беспокоить»). Копии в памяти нет — нечему расходиться.

secrets.json пишется с правами 0600. Наружу (API, логи) секреты не
уходят: для показа есть mask().
"""

import copy
import logging
import os
import re
import secrets as _secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.atomic_io import atomic_write_json, file_lock, load_json_safe
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

SERVICES = ("gmail", "drive", "github", "telegram", "imap", "rss", "webhook")
# Уровни разбора: от важного к неважному
LEVELS = ("urgent", "notify", "digest", "ignore")
RULE_ACTIONS = ("urgent", "always", "digest", "never")
ENGINES = ("local", "persona", "rules")
STYLES = ("plain", "persona")

MAX_RULES = 200
MAX_EXAMPLES = 40
MAX_INTERESTS = 2000
MAX_FEEDS = 50

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

DEFAULT_SETTINGS: dict = {
    "notify": {
        # Кто сообщает: id персоны; None — первая персона из списка
        "persona": None,
        # web — чат персоны в вебе и уведомление на телефоне (inbox);
        # telegram — сообщение от бота персоны владельцу (нужны
        # <ПЕРСОНА>_BOT_TOKEN и OWNER_USER_ID)
        "channels": {"web": True, "telegram": False},
        # plain — текст собирает код (быстро, приватно); persona — персона
        # пересказывает своими словами (её моделью)
        "style": "plain",
        # local — Ollama; persona — модель персоны (облако, без веб-чатов);
        # rules — без модели, только правила и признаки
        "engine": "local",
        # Свободный текст «что для меня важно» — уходит модели разбора
        "interests": "",
        "quiet": {"enabled": True, "from": "23:00", "to": "08:00", "urgent": True},
        "digest_times": ["09:00", "19:00"],
        "max_per_hour": 6,
        # auto — по языку чата персоны; ru | en
        "language": "auto",
    },
    "services": {
        "gmail": {"enabled": False, "interval": 90},
        "drive": {"enabled": False, "interval": 300, "shared": True, "edits": True},
        "github": {"enabled": False, "interval": 60},
        "telegram": {
            "enabled": False,
            "private": True,       # личные чаты
            "mentions": True,      # упоминания и ответы мне в группах
            "groups": False,       # все сообщения групп (шумно)
            "channels": False,     # посты каналов
            "muted": "digest",     # чаты «без звука»: digest | ignore | normal
            "skip_read": True,     # прочитал в Telegram до разбора — не сообщать
        },
        "imap": {"enabled": False, "interval": 120},
        "rss": {"enabled": False, "interval": 1800, "feeds": []},
        "webhook": {"enabled": False},
    },
    "rules": [],
    # Оценки пользователя («важно» / «не важно») — примеры для модели
    "examples": [],
    # «Не беспокой до …» (unix-время); 0 — выключено
    "dnd_until": 0,
}

# Границы интервалов опроса, секунды
_INTERVALS = {
    "gmail": (30, 3600), "drive": (60, 6 * 3600), "github": (60, 3600),
    "imap": (30, 3600), "rss": (300, 24 * 3600),
}

_lock = threading.Lock()


def root() -> Path:
    return data_dir() / "integrations"


def _path(name: str) -> Path:
    return root() / name


def settings_path() -> Path:
    return _path("settings.json")


def secrets_path() -> Path:
    return _path("secrets.json")


def state_path() -> Path:
    return _path("state.json")


# ── Настройки ───────────────────────────────────────────────────────────

def _merge(defaults: dict, data: Any) -> dict:
    # Недостающие ключи — из умолчаний, лишние отбрасываются, типы — как у
    # умолчания (битый файл или старая версия не роняют чтение)
    out = copy.deepcopy(defaults)
    if not isinstance(data, dict):
        return out
    for key, dval in defaults.items():
        if key not in data:
            continue
        val = data[key]
        if isinstance(dval, dict):
            out[key] = _merge(dval, val) if dval else (val if isinstance(val, dict) else {})
        elif isinstance(dval, bool):
            if isinstance(val, bool):
                out[key] = val
        elif isinstance(dval, (int, float)) and not isinstance(dval, bool):
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                out[key] = val
        elif isinstance(dval, list):
            if isinstance(val, list):
                out[key] = val
        elif dval is None or isinstance(dval, str):
            if val is None or isinstance(val, str):
                out[key] = val
    return out


def hhmm(value) -> Optional[str]:
    """«9:05» → «09:05»; не время — None."""
    m = _HHMM_RE.match(str(value or "").strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def normalize_settings(data: Any) -> dict:
    s = _merge(DEFAULT_SETTINGS, data)
    n = s["notify"]
    if n["engine"] not in ENGINES:
        n["engine"] = "local"
    if n["style"] not in STYLES:
        n["style"] = "plain"
    if n["language"] not in ("auto", "ru", "en"):
        n["language"] = "auto"
    n["interests"] = str(n["interests"] or "")[:MAX_INTERESTS]
    n["max_per_hour"] = max(1, min(60, int(n["max_per_hour"])))
    q = n["quiet"]
    q["from"] = hhmm(q["from"]) or "23:00"
    q["to"] = hhmm(q["to"]) or "08:00"
    n["digest_times"] = sorted({t for t in (hhmm(x) for x in n["digest_times"]) if t})[:6]
    for name, (lo, hi) in _INTERVALS.items():
        svc = s["services"][name]
        svc["interval"] = max(lo, min(hi, int(svc.get("interval") or lo)))
    tg = s["services"]["telegram"]
    if tg["muted"] not in ("digest", "ignore", "normal"):
        tg["muted"] = "digest"
    feeds = []
    for f in s["services"]["rss"]["feeds"]:
        url = str(f.get("url") if isinstance(f, dict) else f or "").strip()
        if re.match(r"^https?://\S+$", url) and url not in [x["url"] for x in feeds]:
            title = str(f.get("title") or "")[:120] if isinstance(f, dict) else ""
            feeds.append({"url": url[:500], "title": title})
    s["services"]["rss"]["feeds"] = feeds[:MAX_FEEDS]
    s["rules"] = [r for r in (normalize_rule(x) for x in s["rules"]) if r][:MAX_RULES]
    s["examples"] = [e for e in s["examples"] if isinstance(e, dict)][-MAX_EXAMPLES:]
    s["dnd_until"] = float(s["dnd_until"] or 0)
    return s


def load_settings() -> dict:
    with _lock, file_lock(settings_path()):
        return normalize_settings(load_json_safe(settings_path(), default={},
                                                 label="Integrations"))


def update_settings(mutate: Callable[[dict], Any]) -> dict:
    """Атомарная правка: mutate(settings) меняет словарь на месте."""
    path = settings_path()
    with _lock, file_lock(path):
        s = normalize_settings(load_json_safe(path, default={}, label="Integrations"))
        mutate(s)
        s = normalize_settings(s)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, s)
        return s


def normalize_rule(rule: Any) -> Optional[dict]:
    """Правило: {id, action, source, match, created_at}. match — подстрока
    (без регистра) в отправителе, чате, репозитории или заголовке; source
    — один из SERVICES или «any»."""
    if not isinstance(rule, dict):
        return None
    action = str(rule.get("action") or "")
    match = re.sub(r"\s+", " ", str(rule.get("match") or "")).strip()[:200]
    source = str(rule.get("source") or "any")
    if action not in RULE_ACTIONS or not match:
        return None
    if source not in SERVICES and source != "any":
        source = "any"
    rid = str(rule.get("id") or "")
    if not re.match(r"^[a-f0-9]{8,32}$", rid):
        rid = _secrets.token_hex(6)
    return {"id": rid, "action": action, "source": source, "match": match,
            "created_at": float(rule.get("created_at") or time.time())}


# ── Секреты ─────────────────────────────────────────────────────────────

def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows: права не в POSIX-смысле


def load_secrets(service: Optional[str] = None) -> dict:
    with _lock, file_lock(secrets_path()):
        data = load_json_safe(secrets_path(), default={}, label="Integrations")
    data = data if isinstance(data, dict) else {}
    if service is None:
        return data
    val = data.get(service)
    return dict(val) if isinstance(val, dict) else {}


def update_secrets(service: str, values: Optional[dict], replace: bool = False) -> dict:
    """Слить values в секреты сервиса (replace — заменить целиком; None —
    удалить сервис)."""
    path = secrets_path()
    with _lock, file_lock(path):
        data = load_json_safe(path, default={}, label="Integrations")
        data = data if isinstance(data, dict) else {}
        if values is None:
            data.pop(service, None)
        else:
            cur = {} if replace else dict(data.get(service) or {})
            cur.update(values)
            data[service] = cur
        _write_private(path, data)
        return dict(data.get(service) or {})


def mask(value: Any) -> str:
    """Секрет для показа: «ghp_…7f3a»."""
    s = str(value or "")
    if not s:
        return ""
    if len(s) <= 8:
        return "…" + s[-2:]
    return f"{s[:4]}…{s[-4:]}"


# ── Состояние источников (курсоры, ошибки) ─────────────────────────────

def load_state(service: str) -> dict:
    with _lock, file_lock(state_path()):
        data = load_json_safe(state_path(), default={}, label="Integrations")
    val = data.get(service) if isinstance(data, dict) else None
    return dict(val) if isinstance(val, dict) else {}


def update_state(service: str, values: Optional[dict], replace: bool = False) -> dict:
    path = state_path()
    with _lock, file_lock(path):
        data = load_json_safe(path, default={}, label="Integrations")
        data = data if isinstance(data, dict) else {}
        if values is None:
            data.pop(service, None)
        else:
            cur = {} if replace else dict(data.get(service) or {})
            cur.update(values)
            data[service] = cur
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data)
        return dict(data.get(service) or {})


def forget_service(service: str) -> None:
    """Отключение: секреты и курсоры сервиса стираются, флаг выключается.
    Gmail и Диск — один аккаунт Google: отключаются вместе."""
    if service in ("gmail", "drive"):
        service = "google"
    secret_key = service
    if service == "google":
        update_state("gmail", None)
        update_state("drive", None)
        update_settings(lambda s: (s["services"]["gmail"].update(enabled=False),
                                   s["services"]["drive"].update(enabled=False)))
    else:
        update_state(service, None)
        if service in SERVICES:
            update_settings(lambda s: s["services"][service].update(enabled=False))
    update_secrets(secret_key, None)

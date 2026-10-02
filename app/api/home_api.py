"""Сводка для главной страницы веба (GET /api/home): состояние персон, лента
«пока вас не было» и телеметрия — одним запросом.

Всё читается из файлов персон напрямую, БЕЗ создания BotInstance: главная
показывает сразу все персоны, а подъём бота (Chroma, фоновые циклы) ради
пары чисел на каждую персону был бы непозволительно тяжёлым (как и inbox,
см. server.inbox). Файлы только читаются; битый/отсутствующий файл — пустое
значение, а не ошибка всей сводки.
"""

import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from app.api.security import safe_join
from app.core.config import Config
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

_FEED_PER_KIND = 5  # последних событий каждого вида на персону
_TEXT_MAX = 280


def _ctx_dirs(persona: str) -> list[Path]:
    # Папка веб-контекста персоны: data_dir() и Config.DATA_DIR (Chroma,
    # living, self_memory) обычно совпадают, но env может их развести
    out = []
    for root in (data_dir(), Path(Config.DATA_DIR)):
        path = safe_join(root, persona, prefix="api_")
        if path is not None and path not in out:
            out.append(path)
    return out


def _find(persona: str, rel: str) -> Path | None:
    for base in _ctx_dirs(persona):
        path = base / rel
        if path.exists():
            return path
    return None


def _load_json(persona: str, rel: str):
    path = _find(persona, rel)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _sqlite_ro(path: Path) -> sqlite3.Connection:
    # Только чтение: база Chroma открыта живым ботом — не мешаем его записи
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)


def _last_user_ts(persona: str, chat_id: str) -> float | None:
    """Время последнего сообщения пользователя в чате — из STM (Chroma).
    last_message.json не годится: его штампуют и ответы/инициативы персоны."""
    path = _find(persona, "stm/chroma.sqlite3")
    if path is None:
        return None
    try:
        with _sqlite_ro(path) as db:
            row = db.execute(
                """SELECT MAX(COALESCE(t.float_value, t.int_value))
                   FROM embedding_metadata r
                   JOIN embedding_metadata c ON c.id = r.id AND c.key = 'chat_id' AND c.string_value = ?
                   JOIN embedding_metadata t ON t.id = r.id AND t.key = 'timestamp'
                   WHERE r.key = 'role' AND r.string_value = 'user'""",
                (chat_id,),
            ).fetchone()
    except sqlite3.Error as e:
        logger.debug(f"[home] STM {persona}: {e}")
        return None
    ts = row[0] if row else None
    if not ts:
        return None
    ts = float(ts)
    return ts / 1000 if ts > 1e11 else ts  # в метаданных — миллисекунды


def _ltm_count(persona: str) -> int:
    path = _find(persona, "ltm/chroma.sqlite3")
    if path is None:
        return 0
    try:
        with _sqlite_ro(path) as db:
            return int(db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])
    except sqlite3.Error:
        return 0


def _iso_ts(value) -> float | None:
    # Метки дневника — локальное время ISO без зоны
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _clip(text) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= _TEXT_MAX else text[: _TEXT_MAX - 1] + "…"


def _looks_like_json(text: str) -> bool:
    # Сбойные записи дневника — сырой JSON ответа модели, в ленте не нужны
    t = text.strip()
    if not t.startswith(("{", "[")):
        return False
    try:
        json.loads(t)
        return True
    except ValueError:
        return t.startswith("{ \"") or t.startswith("{\"")


def persona_overview(persona: str, config: dict, chat_id: str = "web_user") -> dict:
    features = config.get("features") or {}
    proactive_cfg = features.get("proactive") if isinstance(features.get("proactive"), dict) else {}

    # Живое состояние (living): срез чата пользователя
    state = None
    living = _load_json(persona, "living/state.json")
    if isinstance(living, dict):
        s = (living.get("chats") or {}).get(chat_id)
        if isinstance(s, dict):
            mood = s.get("mood") if isinstance(s.get("mood"), dict) else {}
            state = {
                "pastime": s.get("pastime") or "",
                "location": s.get("location") or "",
                "mood": mood.get("tag") or "",
                "energy": s.get("energy"),
                "updated_at": _iso_ts(s.get("updated_at")),
            }

    events = []
    history = _load_json(persona, "initiative_history.json")
    if isinstance(history, dict):
        for e in (history.get(chat_id) or [])[-_FEED_PER_KIND:]:
            if isinstance(e, dict) and e.get("message") and e.get("timestamp"):
                events.append({"kind": "initiative", "text": _clip(e["message"]),
                               "ts": float(e["timestamp"]), "type": e.get("type") or ""})

    episodes = _load_json(persona, "self_memory/episodes.json")
    if isinstance(episodes, dict):
        diary = [e for e in (episodes.get("archive") or []) + (episodes.get("active") or [])
                 if isinstance(e, dict) and e.get("text") and not _looks_like_json(str(e["text"]))]
        dated = [(ts, e) for e in diary if (ts := _iso_ts(e.get("timestamp"))) is not None]
        for ts, e in sorted(dated, key=lambda x: x[0])[-_FEED_PER_KIND:]:
            events.append({"kind": "diary", "text": _clip(e["text"]), "ts": ts})

    now = time.time()
    reminders = _load_json(persona, "reminders/reminders.json")
    active_reminders = []
    if isinstance(reminders, list):
        for r in reminders:
            if (isinstance(r, dict) and r.get("chat_id") == chat_id and not r.get("fired")
                    and not r.get("paused") and (r.get("trigger_at") or 0) > now):
                active_reminders.append(r)
    next_reminder = min(active_reminders, key=lambda r: r["trigger_at"], default=None)

    stats = _load_json(persona, "proactive_stats.json")
    today = datetime.now().strftime("%Y-%m-%d")
    chat_stats = stats.get(chat_id) if isinstance(stats, dict) else None
    initiatives_today = (int(chat_stats.get("count") or 0)
                         if isinstance(chat_stats, dict) and chat_stats.get("date") == today else 0)

    return {
        "last_user_ts": _last_user_ts(persona, chat_id),
        "state": state,
        "events": sorted(events, key=lambda e: e["ts"], reverse=True),
        "reminders_active": len(active_reminders),
        "next_reminder": ({"text": _clip(next_reminder.get("task")), "ts": next_reminder["trigger_at"]}
                          if next_reminder else None),
        "initiatives_today": initiatives_today,
        "initiatives_max": (int(proactive_cfg.get("max_daily_initiatives") or 0)
                            if proactive_cfg.get("enabled") else 0),
        "ltm_facts": _ltm_count(persona),
    }


def home_overview(personas: dict[str, dict], chat_id: str = "web_user") -> dict:
    """{"now", "personas": {id: overview}} — personas: {id: данные YAML}."""
    out = {}
    for persona, config in personas.items():
        try:
            out[persona] = persona_overview(persona, config, chat_id)
        except Exception:
            logger.exception(f"[home] Сводка персоны {persona} не собрана")
    return {"now": time.time(), "personas": out}

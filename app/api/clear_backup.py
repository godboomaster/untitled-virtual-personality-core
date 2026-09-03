"""Корзина очистки диалога: снапшот STM/LTM/дневника перед полным сбросом.

Хранение: data/api_{persona}/clear_backups/{timestamp}.json — по файлу на
каждую очистку, старше _RETENTION_DAYS удаляются при записи нового. После
успешного восстановления файл удаляется (повторный restore дал бы дубли).
"""

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_RETENTION_DAYS = 7


def _backup_dir(persona: str) -> Path:
    return _DATA_DIR / f"api_{persona}" / "clear_backups"


def make_backup(persona: str, user_id: str, chat_id: str,
                stm: list, ltm: list, diary: dict | None,
                initiatives: list | None = None,
                daily_stats: dict | None = None,
                last_activity: float = 0) -> Path | None:
    """Сохранить снапшот перед очисткой. Пустой снапшот не пишем."""
    if not stm and not ltm and not diary and not initiatives and not daily_stats and not last_activity:
        return None
    bdir = _backup_dir(persona)
    bdir.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    path = bdir / f"{ts:.0f}.json"
    payload = {
        "ts": ts, "persona": persona, "user_id": user_id, "chat_id": chat_id,
        "stm": stm, "ltm": ltm, "diary": diary,
        "initiatives": initiatives or [],
        "daily_stats": daily_stats,
        "last_activity": last_activity,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        f"[ClearBackup] {persona}: снапшот {path.name} "
        f"(stm={len(stm)}, ltm={len(ltm)}, diary={'да' if diary else 'нет'}, "
        f"init={len(initiatives or [])}, today={(daily_stats or {}).get('count', 0)}, "
        f"activity={'да' if last_activity else 'нет'})"
    )
    _prune(bdir)
    return path


def _prune(bdir: Path):
    """Удалить снапшоты старше _RETENTION_DAYS."""
    cutoff = time.time() - _RETENTION_DAYS * 86400
    for f in bdir.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                logger.info(f"[ClearBackup] Протухший снапшот удалён: {f.name}")
        except OSError:
            pass


def latest_backup(persona: str) -> dict | None:
    """Самый свежий снапшот персоны (None — корзина пуста)."""
    bdir = _backup_dir(persona)
    files = sorted(bdir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True) \
        if bdir.is_dir() else []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_file"] = f.name
            return data
        except (json.JSONDecodeError, OSError):
            continue
    return None


def backup_info(persona: str) -> dict:
    """Краткая информация для UI: есть ли бэкап и что в нём."""
    data = latest_backup(persona)
    if not data:
        return {"exists": False}
    return {
        "exists": True,
        "ts": data.get("ts"),
        "counts": {
            "stm": len(data.get("stm") or []),
            "ltm": len(data.get("ltm") or []),
            "diary": bool(data.get("diary")),
            "initiatives": len(data.get("initiatives") or []),
        },
    }


def pop_latest(persona: str) -> dict | None:
    """Забрать свежий снапшот и удалить его файл (для восстановления)."""
    data = latest_backup(persona)
    if not data:
        return None
    fname = data.pop("_file", None)
    if fname:
        try:
            (_backup_dir(persona) / fname).unlink()
        except OSError:
            pass
    return data

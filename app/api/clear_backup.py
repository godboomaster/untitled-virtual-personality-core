"""Корзина очистки диалога: снапшот STM/LTM/дневника/инициатив/адресов
веб-чатов LLM перед полным сбросом.

Хранение: data/api_{persona}/clear_backups/{chat_segment}/{timestamp}.json —
по файлу на каждую очистку ЭТОГО чата, старше _RETENTION_DAYS удаляются при
записи нового. После успешного восстановления файл удаляется (повторный
restore дал бы дубли).

Бэкап привязан к (persona, chat_id), а не к персоне целиком: иначе в
групповом использовании очистка/восстановление одного чата задевала бы
данные другого чата той же персоны. latest_backup дополнительно ищет
совпадение среди старых плоских файлов clear_backups/*.json без привязки
к чату — они больше не создаются, но не должны теряться из виду.
"""

import json
import logging
import time
from pathlib import Path

from app.api.security import safe_join, safe_segment

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_RETENTION_DAYS = 7


def _backup_root(persona: str) -> Path | None:
    # None — persona не прошла проверку формата (см. app/api/security):
    # вызывающие уже гейтят persona через list_personas()/_get_bot, это —
    # рубеж защиты в глубину на случай, если такого вызова где-то не будет.
    base = safe_join(_DATA_DIR, persona, prefix="api_")
    return base / "clear_backups" if base is not None else None


def _backup_dir(persona: str, chat_id: str) -> Path | None:
    # Каталог бэкапов конкретного чата персоны.
    root = _backup_root(persona)
    return root / safe_segment(chat_id) if root is not None else None


def make_backup(persona: str, user_id: str, chat_id: str,
                stm: list, ltm: list, diary: dict | None,
                initiatives: list | None = None,
                daily_stats: dict | None = None,
                last_activity: float = 0,
                chat_urls: dict | None = None,
                stores: dict | None = None,
                parts: list | None = None) -> Path | None:
    # Сохранить снапшот перед очисткой; пустой снапшот не пишем.
    if not stm and not ltm and not diary and not initiatives \
            and not daily_stats and not last_activity and not chat_urls \
            and not stores:
        return None
    bdir = _backup_dir(persona, chat_id)
    if bdir is None:
        logger.warning(f"[ClearBackup] недопустимое имя персоны: {persona!r}")
        return None
    bdir.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    path = bdir / f"{ts:.0f}.json"
    payload = {
        "ts": ts, "persona": persona, "user_id": user_id, "chat_id": chat_id,
        "stm": stm, "ltm": ltm, "diary": diary,
        "initiatives": initiatives or [],
        "daily_stats": daily_stats,
        "last_activity": last_activity,
        "chat_urls": chat_urls or {},
        "stores": stores or {},
        # Какие части стёрты (None — полная очистка): подпись корзины в досье
        "parts": parts,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        f"[ClearBackup] {persona}/{chat_id}: снапшот {path.name} "
        f"(stm={len(stm)}, ltm={len(ltm)}, diary={'да' if diary else 'нет'}, "
        f"init={len(initiatives or [])}, today={(daily_stats or {}).get('count', 0)}, "
        f"activity={'да' if last_activity else 'нет'}, "
        f"webchat={len(chat_urls or {})}, stores={len(stores or {})})"
    )
    _prune(bdir)
    # Заодно подчищаем протухшие бэкапы старого плоского формата (до
    # привязки к chat_id) — glob() нерекурсивен, чужие per-chat подкаталоги
    # не трогает
    root = _backup_root(persona)
    if root is not None and root != bdir:
        _prune(root)
    return path


def _prune(bdir: Path):
    # Удалить снапшоты старше _RETENTION_DAYS.
    cutoff = time.time() - _RETENTION_DAYS * 86400
    for f in bdir.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                logger.info(f"[ClearBackup] Протухший снапшот удалён: {f.name}")
        except OSError:
            pass


def _newest_in_dir(bdir: Path | None) -> dict | None:
    # Самый свежий валидный снапшот в каталоге (None — пусто/битые файлы).
    files = sorted(bdir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True) \
        if bdir is not None and bdir.is_dir() else []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_file"] = f.name
            return data
        except (json.JSONDecodeError, OSError):
            continue
    return None


def latest_backup(persona: str, chat_id: str) -> dict | None:
    """Самый свежий снапшот именно этого чата (None — корзина пуста).

    Если у чата нет своих бэкапов, ищем совпадение по chat_id/user_id среди
    старых плоских файлов clear_backups/*.json (бэкапы до привязки к чату
    хранились без разбивки по chat_id)."""
    data = _newest_in_dir(_backup_dir(persona, chat_id))
    if data is not None:
        return data
    root = _backup_root(persona)
    if root is None or not root.is_dir():
        return None
    legacy_files = sorted(
        (f for f in root.glob("*.json") if f.is_file()),
        key=lambda f: f.stat().st_mtime, reverse=True,
    )
    for f in legacy_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(data.get("chat_id") or data.get("user_id") or "") == str(chat_id):
            data["_file"] = f.name
            data["_legacy"] = True
            return data
    return None


def backup_info(persona: str, chat_id: str) -> dict:
    # Краткая информация для UI: есть ли бэкап этого чата и что в нём.
    data = latest_backup(persona, chat_id)
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
            "webchat": len(data.get("chat_urls") or {}),
            "stores": len(data.get("stores") or {}),
        },
        "parts": data.get("parts"),
    }


def pop_latest(persona: str, chat_id: str) -> dict | None:
    # Забрать свежий снапшот этого чата и удалить его файл (для восстановления).
    data = latest_backup(persona, chat_id)
    if not data:
        return None
    fname = data.pop("_file", None)
    is_legacy = data.pop("_legacy", False)
    bdir = _backup_root(persona) if is_legacy else _backup_dir(persona, chat_id)
    if fname and bdir is not None:
        try:
            (bdir / fname).unlink()
        except OSError:
            pass
    return data

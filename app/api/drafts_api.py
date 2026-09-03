"""Черновики новых персон (модалка создания на фронте).

Черновик — JSON-файл ``data/persona_drafts/{id}.json`` с непрозрачным
состоянием формы фронта + снапшотом сгенерированного YAML. Валидации
почти нет намеренно: черновик может быть недозаполнен — в этом его смысл.
В ``app/personas/`` черновики не попадают, реальные персоны они не затрагивают.
"""

import json
import re
import time
import uuid
from pathlib import Path

DRAFTS_DIR = Path(__file__).parent.parent.parent / "data" / "persona_drafts"

# Допустимый id черновика (защита от path traversal)
_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _path(draft_id: str) -> Path | None:
    if not _ID_RE.match(draft_id):
        return None
    return DRAFTS_DIR / f"{draft_id}.json"


def list_drafts() -> list[dict]:
    """Все черновики целиком (form + yaml), свежие сверху.

    Фронту удобно получать всё одним запросом — черновики маленькие,
    приложение однопользовательское.
    """
    out = []
    if DRAFTS_DIR.is_dir():
        for p in DRAFTS_DIR.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue  # битый файл не роняет список
            if isinstance(d, dict) and isinstance(d.get("id"), str):
                out.append(d)
    out.sort(key=lambda d: d.get("updated_at", 0), reverse=True)
    return out


def save_draft(draft_id: str | None, name: str, form: dict, yaml_text: str) -> dict | None:
    """Создать (id=None) или обновить черновик. None — невалидный id."""
    now = time.time()
    created = now
    if draft_id:
        path = _path(draft_id)
        if path is None:
            return None
        if path.is_file():
            try:
                created = float(json.loads(path.read_text(encoding="utf-8")).get("created_at", now))
            except Exception:
                pass
    else:
        draft_id = uuid.uuid4().hex[:12]
        path = DRAFTS_DIR / f"{draft_id}.json"
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    draft = {
        "id": draft_id,
        "name": name,
        "created_at": created,
        "updated_at": now,
        "form": form if isinstance(form, dict) else {},
        "yaml": yaml_text,
    }
    path.write_text(json.dumps(draft, ensure_ascii=False, indent=1), encoding="utf-8")
    return draft


def delete_draft(draft_id: str) -> bool:
    path = _path(draft_id)
    if path is None or not path.is_file():
        return False
    path.unlink()
    return True

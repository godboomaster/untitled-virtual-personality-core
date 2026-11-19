"""Как сейчас выглядит веб-интерфейс на компьютере: тема оформления (скин из
web/skins/) — общая и своя у отдельных персон — и схема (светлая/тёмная).

Скин выбирают в вебе, и живёт выбор в браузере (localStorage). Портрет на
рабочем столе (desktop/src/portrait) рисуется в окне панели, куда браузер не
дотянется, — веб присылает выбор сюда (PUT /api/settings/ui-look), панель
берёт его в лёгком списке портретов (GET /api/portraits, поле look), а без
ядра — прямо из файла. Смена будит портрет событием ленты реплик (kind look,
app/api/said): голова меняется сразу, а не к следующему опросу.

    skin     — id общего скина; null — обычный вид;
    personas — своя тема персоны: {id персоны: id скина или "default" —
               у этой персоны обычный вид при общем скине};
    theme    — "light" / "dark", выбор в шапке веба; null — неизвестно
               (портрет берёт схему системы).

Телефон сюда не пишет: в приложении свой выбор, а портрет на столе — у
компьютера. Хранение: data/ui_look.json, запись атомарно под межпроцессным
локом.
"""

import logging
import re
from pathlib import Path

from app.core.atomic_io import atomic_write_json, file_lock, load_json_safe
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

# id персоны — как SAFE_ID_RE ядра (app/api/security.py)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# id скина — имя папки в web/skins/
SKIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
PERSONA_DEFAULT = "default"
THEMES = ("light", "dark")
MAX_PERSONAS = 500
EMPTY = {"skin": None, "personas": {}, "theme": None}


def _path() -> Path:
    return data_dir() / "ui_look.json"


def _parse(data) -> dict:
    """Из файла — только годное: битое или чужое значение пропускается."""
    out = {"skin": None, "personas": {}, "theme": None}
    if not isinstance(data, dict):
        return out
    skin = data.get("skin")
    if isinstance(skin, str) and SKIN_RE.match(skin) and skin != PERSONA_DEFAULT:
        out["skin"] = skin
    personas = data.get("personas")
    if isinstance(personas, dict):
        for pid, sid in list(personas.items())[:MAX_PERSONAS]:
            if isinstance(pid, str) and SAFE_ID_RE.match(pid) and isinstance(sid, str) and SKIN_RE.match(sid):
                out["personas"][pid] = sid
    theme = data.get("theme")
    if theme in THEMES:
        out["theme"] = theme
    return out


def get() -> dict:
    """Текущий выбор (копия); файла нет — обычный вид."""
    path = _path()
    if not path.exists():
        return {**EMPTY, "personas": {}}
    return _parse(load_json_safe(path, default={}, label="UiLook"))


def update(look: dict) -> tuple[dict, bool]:
    """Выбор целиком (PUT) → (сохранённое, изменилось ли). Негодное значение —
    ValueError, файл не трогается."""
    look = dict(look or {})
    bad = [k for k in look if k not in EMPTY]
    if bad:
        raise ValueError(f"Неизвестные пункты: {', '.join(sorted(bad))}")
    skin = look.get("skin")
    if skin is not None and (not isinstance(skin, str) or not SKIN_RE.match(skin) or skin == PERSONA_DEFAULT):
        raise ValueError("skin: не id скина")
    personas = look.get("personas") or {}
    if not isinstance(personas, dict) or len(personas) > MAX_PERSONAS:
        raise ValueError("personas: не словарь или слишком много")
    for pid, sid in personas.items():
        if not isinstance(pid, str) or not SAFE_ID_RE.match(pid):
            raise ValueError("personas: не id персоны")
        if not isinstance(sid, str) or not SKIN_RE.match(sid):
            raise ValueError(f"personas.{pid}: не id скина")
    theme = look.get("theme")
    if theme is not None and theme not in THEMES:
        raise ValueError("theme: light или dark")
    clean = {"skin": skin, "personas": dict(personas), "theme": theme}
    path = _path()
    with file_lock(path):
        before = _parse(load_json_safe(path, default={}, label="UiLook")) if path.exists() else None
        changed = before != clean
        if changed:
            atomic_write_json(path, clean)
    if changed:
        logger.info(f"[UiLook] скин {skin or '—'}, своих у персон {len(personas)}, схема {theme or '—'}")
    return clean, changed

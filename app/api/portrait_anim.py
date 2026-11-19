"""Анимации портрета на рабочем столе: ест, читает, идёт… Пользователь сам
готовит лист кадров rows×cols (генератор картинок или рисунок), даёт ему
название и выбирает, когда играть (triggers): случайно в ротации, на
события (погладили, вы вернулись, засыпает…), шагами, пока портрет идёт по
экрану, или по выбору самой персоны — маркером [ACT:имя] в её ответе.
Играет их портрет на рабочем столе (desktop/src/portrait/), кадры рисует
web/src/portrait/AnimSprite.tsx.

Нарезка. Клетка — ровно по сетке (поля offset_x/offset_y — пустой край
листа). Фон — как у листов портрета (app/api/portrait_api.py): у листа с
прозрачностью он уже прозрачный, иначе сплошной цвет по краю листа
убирается заливкой от края клетки. В клетке остаётся крупнейшая фигура и
куски, которые края клетки не касаются (реквизит: книга, тарелка, крошка
печенья в воздухе); касаются края — это обрывки соседних кадров. Совсем
мелкие крошки (меньше SPECK площади клетки) — шум фона, их нет.

Масштаб один на анимацию, кадр — длинной стороной ANIM_PX, пропорции —
как у клетки листа. Выравнивание (align):
- grid — у всех кадров один и тот же сдвиг: все фигуры вместе — по середине
  кадра, нижней точкой к низу; движение внутри листа (прыжок, шаг вбок)
  сохраняется;
- feet — каждый кадр сам: низ фигуры к низу кадра, горизонтальный центр
  масс — к середине. Гасит дрожание генератора (фигура в каждой клетке
  стоит чуть по-своему), но прыжки пропадают.

Редактор кадров (вкладка «Кадры» раздела «Аватар»,
app/api/portrait_frames.py) добавляет поверх этого xs/ys — линии сетки,
подвинутые руками, fix — сдвиг, масштаб и поворот кадров (устройство то же,
что у листов портрета, app/api/portrait_api.py) и order — свой порядок
кадров: кадр j анимации — клетка листа order[j], клеток не из order в
анимации нет (кадр удалили), клетка может идти и не раз (1‑2‑3‑2: кадр с
листа добавили ещё раз). fix и holds — по кадрам анимации, а не по клеткам
листа: кадр переставили — его правка и время едут с ним (это делает
редактор). Кадров больше, чем клеток, — атлас растёт вниз: atlas_rows рядов
по cols кадров (клиенты читают атлас по нему), slots — сколько кадров. Фигуре с правками тесно — кадр
атласа растёт вверх и вбок, до FRAME_MAX. Пока длинная сторона остаётся
ANIM_PX, размер фигуры на экране (animBox) не меняется; дальше фигура на
экране мельче, потому что окно портрета на рабочем столе — квадрат со
стороной головы, и в него входит весь кадр: лучше мельче, чем обрезано.

Время. fps — шаг всей анимации, holds — сколько шагов стоит каждый кадр
(нет — все по одному), поэтому fps ускоряет и замедляет всё сразу, а полоска
времени в редакторе кадров двигает кадры относительно друг друга. loops —
сколько раз проиграть за раз, loop — по кругу, пока не случится что-то ещё
(что именно — desktop/src/portrait/useBody.ts). Новая картинка и другая
сетка сбрасывают holds и order: номера кадров уже про другое.

Звук. У анимации может быть свой звук (sound — MIME файла, нет — без
звука): начинается с первым кадром, громкость volume (0–100), sound_loop —
повторять, пока анимация на экране, иначе звучит один раз и доигрывает,
даже если анимация уже кончилась. Файл — по сигнатуре байтов (MP3, AAC,
WAV, OGG, FLAC, M4A, WebM), не больше SOUND_MAX_BYTES.

Край экрана. Анимация стоит на нижнем крае экрана (fix.floor — какой
линией фигуры) или подглядывает из-за другого края (fix.edge: left, right,
top и линия в кадре): тогда портрет на столе, пока она играет, встаёт к
этому краю, что за линией — за краем, потом возвращается. В записи edge —
линия уже в кадре атласа (portrait_api.edge_in_atlas).

Связки. after — после каких анимаций играет (любой из них: та кончилась —
эта), before — перед какими (сначала эта, потом та), chance — в скольких
процентах случаев связка срабатывает (100 — каждый раз). Цепочку собирает
портрет (web/src/portrait/animTypes.ts: leadIn, followUp); анимация в ней
не повторяется. Удалили анимацию — из связок других она пропадает.

Переход между режимами персоны (app/api/avatar_modes.py) — анимация с
поводом mode в аватаре режима, в который переходят: switch_from — из каких
режимов ("" — обычный характер; пусто — из любого). Портрет играет её, когда
режим сменился, а после неё — уже голову нового режима.

Хранение — data/api_<персона>/portrait/anims/: anims.json (настройки
ротации и анимации), <id>.webp (атлас cols×rows кадров fw×fh по порядку
чтения), <id>.raw.webp (атлас без правок кадров, есть только при правках),
<id>.src (исходный лист — переразметить без повторной загрузки), <id>.sound
(звук анимации, если есть). У аватара режима — то же в
data/api_<персона>/portrait_modes/<режим>/anims/.
"""

from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from app.api.portrait_api import (EDGE_SIDES, PortraitError, _cut_background, _has_transparency,
                                  _sheet_background, clean_fix, compose, edge_in_atlas, grid_lines,
                                  grown_frame, portrait_dir, reorder, store_atlas, whole_fix)
from app.api.room_api import validate_data_url
from app.core.atomic_io import atomic_write_json

ANIM_PX = 256
FRAME_MAX = 512  # кадр с правками растёт до этого размера
MAX_GRID = 12
MAX_ANIMS = 40
MAX_SLOTS = 144  # кадров в анимации, с повторами клеток
SHEET_MAX_BYTES = 16 * 1024 * 1024
FIT = 0.96  # фигуры в кадре — с небольшим запасом по краям
SPECK = 0.001  # кусок меньше этой доли клетки — шум
SOLID = 16  # альфа, с которой пиксель — уже фигура
NAME_MAX = 40
HOLD_MIN, HOLD_MAX = 0.05, 300.0  # сколько шагов fps стоит кадр
BG_MODES = ("auto", "keep")
ALIGNS = ("grid", "feet")
FACES = ("none", "left", "right")
TRIGGERS = ("idle", "walk", "llm", "pet", "hello", "message", "think", "drop", "grab",
            "fall", "land", "jump", "angry", "sleep", "wake", "break", "focus", "mode")
# Переход между режимами (повод mode): из каких режимов — id режима, "" —
# обычный характер; пусто — из любого
SWITCH_FROM_MAX = 12
_SWITCH_RE = re.compile(r"^[\w-]{0,40}$")
DEFAULT_SETTINGS = {"every_min": 4, "walk": True, "sleep_anim": None}
# Поля, от которых зависит нарезка: сменились — режем лист заново
_CUT_FIELDS = ("rows", "cols", "offset_x", "offset_y", "bg", "tolerance", "align", "xs", "ys", "fix", "order")
KEEP = object()  # put_cut: порядок кадров — как был
_ID_RE = re.compile(r"^[0-9a-f]{8}$")
# Клавиши и кнопки мыши, на которые анимация играет на рабочем столе (панель
# слушает их по всей системе, desktop/src-tauri/src/input_watch.rs). Сочетание
# — модификаторы в этом порядке и одна клавиша по KeyboardEvent.code
# (физическая: KeyA, Digit1, F5, ArrowLeft, ShiftLeft…) или кнопка мыши
# (MouseLeft, MouseRight, MouseMiddle, MouseBack, MouseForward)
KEYS_MAX = 8
_KEY_RE = re.compile(r"^(Ctrl\+)?(Alt\+)?(Shift\+)?(Meta\+)?[A-Z][A-Za-z0-9]{0,23}$")
LINKS_MAX = 12  # анимаций в связке «после» и «перед» у одной
SOUND_MAX_BYTES = 2 * 1024 * 1024
SOUND_NAME_MAX = 80
VOLUME_DEFAULT = 80
SOUND_MIMES = ("audio/mpeg", "audio/aac", "audio/wav", "audio/ogg", "audio/flac", "audio/mp4", "audio/webm")
_SOUND_URL_RE = re.compile(r"^data:([a-z0-9.+-]+/[a-z0-9.+-]+)?(?:;[^,;]*)*;base64,([A-Za-z0-9+/=\s]+)$", re.I)

_lock = threading.Lock()


def anims_dir(persona: str) -> Path:
    return portrait_dir(persona) / "anims"


def _clamp(v, lo, hi, default, kind=int):
    try:
        x = kind(v)
    except (TypeError, ValueError):
        return default
    if kind is float and not np.isfinite(x):
        return default
    return max(lo, min(hi, x))


def _settings(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    sleep = raw.get("sleep_anim")
    return {"every_min": _clamp(raw.get("every_min"), 0, 60, DEFAULT_SETTINGS["every_min"]),
            "walk": bool(raw.get("walk", DEFAULT_SETTINGS["walk"])),
            "sleep_anim": sleep if isinstance(sleep, str) and _ID_RE.fullmatch(sleep) else None}


def _line(raw, n: int) -> Optional[list]:
    """Линии сетки из записи: n+1 чисел по возрастанию, иначе их нет."""
    if not (isinstance(raw, list) and len(raw) == n + 1):
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in raw):
        return None
    return raw if all(a < b for a, b in zip(raw, raw[1:])) else None


def _holds(raw, cells: int) -> Optional[list]:
    """Сколько шагов стоит каждый кадр: числа в пределах, не длиннее листа.
    Битые или все единицы — None: кадры идут ровно."""
    if not (isinstance(raw, list) and 0 < len(raw) <= cells):
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in raw):
        return None
    out = [round(_clamp(v, HOLD_MIN, HOLD_MAX, 1.0, float), 3) for v in raw]
    return None if all(v == 1 for v in out) else out


def _order(raw, cells: int) -> Optional[list]:
    """Свой порядок кадров: номера клеток листа (хоть один, клетка может
    повторяться), не больше MAX_SLOTS. Битый или как на листе — None."""
    if not (isinstance(raw, list) and 0 < len(raw) <= MAX_SLOTS):
        return None
    if any(isinstance(v, bool) or not isinstance(v, int) for v in raw):
        return None
    if min(raw) < 0 or max(raw) >= cells:
        return None
    return None if raw == list(range(cells)) else list(raw)


def _keys(raw) -> list:
    """Клавиши анимации: сочетания по _KEY_RE, без повторов, не больше
    KEYS_MAX. Не список — нет клавиш."""
    if not isinstance(raw, list):
        return []
    out = []
    for k in raw:
        if isinstance(k, str) and _KEY_RE.match(k) and k not in out:
            out.append(k)
    return out[:KEYS_MAX]


def _links(raw) -> list:
    """Связка «после» или «перед»: id анимаций без повторов, не больше
    LINKS_MAX. Есть ли такие анимации — проверяет put_anim."""
    if not isinstance(raw, list):
        return []
    out = []
    for x in raw:
        if isinstance(x, str) and _ID_RE.match(x) and x not in out:
            out.append(x)
    return out[:LINKS_MAX]


def _switch_from(raw) -> list:
    """Из каких режимов переход (повод mode): id режимов и "" (обычный), без
    повторов, не больше SWITCH_FROM_MAX. Не список — из любого."""
    if not isinstance(raw, list):
        return []
    out = []
    for x in raw:
        if isinstance(x, str) and _SWITCH_RE.match(x) and x not in out:
            out.append(x)
    return out[:SWITCH_FROM_MAX]


def is_switch_only(anim: dict) -> bool:
    """Только переход между режимами (единственный повод — mode): в обычные
    анимации аватара не входит."""
    return anim.get("triggers") == ["mode"]


def sniff_sound(raw: bytes) -> Optional[str]:
    """MIME звука по первым байтам; не звук — None."""
    head = raw[:16]
    if head[:3] == b"ID3":
        return "audio/mpeg"
    if len(head) >= 2 and head[0] == 0xFF:
        if head[1] & 0xF6 == 0xF0:  # ADTS: AAC без контейнера
            return "audio/aac"
        if head[1] & 0xE0 == 0xE0 and head[1] & 0x06:  # кадр MPEG, слой задан
            return "audio/mpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[:4] == b"fLaC":
        return "audio/flac"
    if head[4:8] == b"ftyp":
        return "audio/mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm"
    return None


def decode_sound(value) -> tuple[bytes, str]:
    """data-URL звука → (байты, MIME по сигнатуре); не то — PortraitError."""
    m = _SOUND_URL_RE.match(value) if isinstance(value, str) else None
    if not m:
        raise PortraitError(400, "Звук: ожидается data-URL файла")
    b64 = re.sub(r"\s+", "", m.group(2))
    if len(b64) * 3 // 4 > SOUND_MAX_BYTES + 3:
        raise PortraitError(413, f"Звук: больше {SOUND_MAX_BYTES // (1024 * 1024)} МБ")
    try:
        raw = base64.b64decode(b64, validate=True)
    except ValueError:
        raise PortraitError(400, "Звук: файл повреждён (base64)")
    if not raw or len(raw) > SOUND_MAX_BYTES:
        raise PortraitError(413, f"Звук: больше {SOUND_MAX_BYTES // (1024 * 1024)} МБ")
    mime = sniff_sound(raw)
    if mime is None:
        raise PortraitError(400, "Звук: файл не похож на MP3, AAC, WAV, OGG, FLAC, M4A или WebM")
    return raw, mime


def _edge_rec(raw) -> Optional[dict]:
    if not (isinstance(raw, dict) and raw.get("side") in EDGE_SIDES):
        return None
    at = raw.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return {"side": raw["side"], "at": round(_clamp(at, 0, FRAME_MAX, 0, float), 1)}


def atlas_rows(rows: int, cols: int, slots: int) -> int:
    """Рядов в атласе клиентов: как у листа, а кадров больше — сколько нужно."""
    return max(rows, -(-slots // cols))


def slots_of(rec: dict) -> int:
    """Сколько кадров в анимации, считая те, что не играются."""
    return len(rec["order"]) if rec.get("order") else rec["rows"] * rec["cols"]


def _anim(raw: dict) -> Optional[dict]:
    """Запись анимации из anims.json — с полями в пределах (битое — None)."""
    if not isinstance(raw, dict) or not _ID_RE.match(str(raw.get("id") or "")):
        return None
    rows = _clamp(raw.get("rows"), 1, MAX_GRID, 1)
    cols = _clamp(raw.get("cols"), 1, MAX_GRID, 1)
    filled = _clamp(raw.get("filled"), 0, rows * cols, rows * cols)
    trig = raw.get("triggers") if isinstance(raw.get("triggers"), list) else []
    order = _order(raw.get("order"), rows * cols)
    slots = len(order) if order else rows * cols
    sound = raw.get("sound") if raw.get("sound") in SOUND_MIMES else None
    return {
        "id": raw["id"],
        "name": str(raw.get("name") or "")[:NAME_MAX],
        "rows": rows, "cols": cols,
        "frames": _clamp(raw.get("frames"), 1, slots, max(1, min(slots, filled))),
        "filled": filled,
        "order": order,
        "slots": slots,
        "atlas_rows": atlas_rows(rows, cols, slots),
        "fps": _clamp(raw.get("fps"), 1, 30, 8),
        "loops": _clamp(raw.get("loops"), 1, 20, 1),
        "loop": bool(raw.get("loop", False)),
        "holds": _holds(raw.get("holds"), slots),
        "scale": round(_clamp(raw.get("scale"), 0.3, 3.0, 1.0, float), 3),
        "align": raw.get("align") if raw.get("align") in ALIGNS else "grid",
        "bg": raw.get("bg") if raw.get("bg") in BG_MODES else "auto",
        "tolerance": _clamp(raw.get("tolerance"), 0, 120, 0),
        "offset_x": _clamp(raw.get("offset_x"), 0, 4000, 0),
        "offset_y": _clamp(raw.get("offset_y"), 0, 4000, 0),
        "xs": _line(raw.get("xs"), cols),
        "ys": _line(raw.get("ys"), rows),
        "fix": clean_fix(raw.get("fix"), slots),
        "faces": raw.get("faces") if raw.get("faces") in FACES else "none",
        "triggers": [t for t in TRIGGERS if t in trig],
        "keys": _keys(raw.get("keys")),
        "switch_from": _switch_from(raw.get("switch_from")),
        "after": [x for x in _links(raw.get("after")) if x != raw["id"]],
        "before": [x for x in _links(raw.get("before")) if x != raw["id"]],
        "chance": _clamp(raw.get("chance"), 1, 100, 100),
        "sound": sound,
        "sound_name": str(raw.get("sound_name") or "")[:SOUND_NAME_MAX] if sound else "",
        "sound_loop": bool(raw.get("sound_loop", False)),
        "volume": _clamp(raw.get("volume"), 0, 100, VOLUME_DEFAULT),
        "weight": _clamp(raw.get("weight"), 1, 10, 1),
        "enabled": bool(raw.get("enabled", True)),
        "fw": _clamp(raw.get("fw"), 1, FRAME_MAX, ANIM_PX),
        "fh": _clamp(raw.get("fh"), 1, FRAME_MAX, ANIM_PX),
        # Пол кадра — на столько px выше его низа (фигура уходит под пол, grown_frame)
        "sink": _clamp(raw.get("sink"), 0, FRAME_MAX, 0),
        # Середина пола — столько px от левого края кадра (своя рамка бывает
        # несимметричной); нет — посередине
        "ax": (round(_clamp(raw.get("ax"), 0, FRAME_MAX, 0, float), 1)
               if isinstance(raw.get("ax"), (int, float)) and not isinstance(raw.get("ax"), bool) else None),
        # Край экрана, кроме низа (fix.edge): линия в кадре атласа — px от его
        # левого (left, right) или верхнего (top) края; портрет на столе
        # встаёт к этому краю экрана, пока анимация играет
        "edge": _edge_rec(raw.get("edge")),
        "created_at": raw.get("created_at") if isinstance(raw.get("created_at"), (int, float)) else None,
        "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), (int, float)) else None,
    }


def _read(persona: str) -> dict:
    try:
        data = json.loads((anims_dir(persona) / "anims.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {"settings": dict(DEFAULT_SETTINGS), "anims": [], "updated_at": None}
    anims = [a for a in (_anim(x) for x in (data.get("anims") or [])) if a]
    ts = data.get("updated_at")
    return {"settings": _settings(data.get("settings")), "anims": anims,
            "updated_at": ts if isinstance(ts, (int, float)) else None}


def _write(persona: str, data: dict) -> None:
    d = anims_dir(persona)
    d.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = time.time()
    atomic_write_json(d / "anims.json", data)


def get_anims(persona: str, atlas: bool = True) -> dict:
    """{anims: [... + atlas (data-URL webp) и sound_url (data-URL звука, если
    он есть), если atlas], settings, updated_at}. Анимации без атласа на
    диске (файл потеряли) не отдаются; звук потеряли — без звука."""
    data = _read(persona)
    d = anims_dir(persona)
    out = []
    for a in data["anims"]:
        path = d / f"{a['id']}.webp"
        if not path.is_file():
            continue
        item = dict(a)
        sound = d / f"{a['id']}.sound"
        if item["sound"] and not sound.is_file():
            item.update(sound=None, sound_name="")
        if atlas:
            item["atlas"] = "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode()
            if item["sound"]:
                item["sound_url"] = f"data:{item['sound']};base64," + base64.b64encode(sound.read_bytes()).decode()
        out.append(item)
    return {"anims": out, "settings": data["settings"], "updated_at": data["updated_at"]}


def updated_at(persona: str) -> Optional[float]:
    """Когда анимации правили (без атласов); anims.json нет — None."""
    try:
        if not (anims_dir(persona) / "anims.json").is_file():
            return None
        return _read(persona)["updated_at"]
    except PortraitError:
        return None


def has_anims(persona: str) -> bool:
    try:
        return (anims_dir(persona) / "anims.json").is_file()
    except PortraitError:
        return False


def put_anim(persona: str, anim_id: str, upd: dict) -> dict:
    """Новая анимация (anim_id "new" — id выдаёт ядро) или правка: data_url —
    новый лист; без него лист режется заново, только если сменилась сетка,
    поля, фон или выравнивание, иначе меняются одни поля. Правки редактора
    кадров остаются, пока подходят: новая картинка сбрасывает всё, другая
    сетка — линии, правки и время отдельных кадров, другие поля — линии.
    loop, keys, after, before, chance, sound_loop и volume None — как было
    (вкладка старой версии их не присылает). sound_url: None — звук как
    был, "" — убрать, data-URL — новый (sound_name — имя файла)."""
    is_new = anim_id == "new"
    if not is_new and not _ID_RE.match(anim_id or ""):
        raise PortraitError(404, "Анимация не найдена")
    name = str(upd.get("name") or "").strip()[:NAME_MAX]
    if not name:
        raise PortraitError(400, "Нужно название анимации")
    rows, cols = upd.get("rows"), upd.get("cols")
    if not (isinstance(rows, int) and isinstance(cols, int) and 1 <= rows <= MAX_GRID and 1 <= cols <= MAX_GRID):
        raise PortraitError(400, f"Сетка — от 1×1 до {MAX_GRID}×{MAX_GRID}")
    if upd.get("bg", "auto") not in BG_MODES:
        raise PortraitError(400, "Фон — auto или keep")
    if upd.get("align", "grid") not in ALIGNS:
        raise PortraitError(400, "Выравнивание — grid или feet")
    if upd.get("faces", "none") not in FACES:
        raise PortraitError(400, "Куда смотрит фигура — none, left или right")
    trig = upd.get("triggers") or []
    if not isinstance(trig, list) or any(t not in TRIGGERS for t in trig):
        raise PortraitError(400, "Неизвестный повод для анимации")
    keys = upd.get("keys")
    if keys is not None and (not isinstance(keys, list) or len(keys) > KEYS_MAX
                             or any(not isinstance(k, str) or not _KEY_RE.match(k) for k in keys)):
        raise PortraitError(400, f"Клавиши — до {KEYS_MAX} сочетаний вида Ctrl+Shift+KeyK или MouseLeft")
    switch_from = upd.get("switch_from")
    if switch_from is not None and (not isinstance(switch_from, list) or len(switch_from) > SWITCH_FROM_MAX
                                    or any(not isinstance(x, str) or not _SWITCH_RE.match(x) for x in switch_from)):
        raise PortraitError(400, f"Переход — из режимов по id, не больше {SWITCH_FROM_MAX}")
    links = {k: upd.get(k) for k in ("after", "before")}
    for v in links.values():
        if v is not None and (not isinstance(v, list) or len(v) > LINKS_MAX
                              or any(not isinstance(x, str) or not _ID_RE.match(x) for x in v)):
            raise PortraitError(400, f"Связка — до {LINKS_MAX} анимаций")
    sound_url = upd.get("sound_url")
    sound_new = decode_sound(sound_url) if sound_url else None
    data_url = upd.get("data_url")
    raw_new = None
    if data_url is not None:
        url = validate_data_url(data_url, SHEET_MAX_BYTES, "Лист анимации")
        raw_new = base64.b64decode(url.split(",", 1)[1])
    d = anims_dir(persona)
    with _lock:
        data = _read(persona)
        anims = data["anims"]
        if is_new:
            if raw_new is None:
                raise PortraitError(400, "Нужна картинка листа кадров")
            if len(anims) >= MAX_ANIMS:
                raise PortraitError(400, f"Анимаций у персоны не больше {MAX_ANIMS}")
            anim_id = uuid.uuid4().hex[:8]
            prev = None
        else:
            prev = next((a for a in anims if a["id"] == anim_id), None)
            if prev is None:
                raise PortraitError(404, "Анимация не найдена")
        now = time.time()
        known = {a["id"] for a in anims} - {anim_id}
        old = prev or {}
        # Связки — только с теми, что есть (другую удалили, пока вкладка была открыта)
        linked = {k: old.get(k, []) if v is None else [x for x in v if x in known] for k, v in links.items()}
        keep = {k: old.get(k, d) if upd.get(k) is None else upd[k]
                for k, d in (("chance", 100), ("sound_loop", False), ("volume", VOLUME_DEFAULT))}
        rec = _anim({
            **(prev or {}),
            **linked, **keep,
            "id": anim_id, "name": name, "rows": rows, "cols": cols,
            "fps": upd.get("fps", 8), "loops": upd.get("loops", 1), "scale": upd.get("scale", 1.0),
            "loop": (prev or {}).get("loop", False) if upd.get("loop") is None else upd["loop"],
            "align": upd.get("align", "grid"), "bg": upd.get("bg", "auto"),
            "tolerance": upd.get("tolerance", 0), "offset_x": upd.get("offset_x", 0),
            "offset_y": upd.get("offset_y", 0), "faces": upd.get("faces", "none"),
            "triggers": trig, "weight": upd.get("weight", 1),
            "keys": (prev or {}).get("keys", []) if keys is None else keys, "enabled": upd.get("enabled", True),
            "switch_from": (prev or {}).get("switch_from", []) if switch_from is None else switch_from,
            "created_at": (prev or {}).get("created_at") or now, "updated_at": now,
        })
        if raw_new is not None:
            rec.update(xs=None, ys=None, fix={}, holds=None, order=None)
        elif prev is not None:
            if (prev["rows"], prev["cols"]) != (rows, cols):
                rec.update(xs=None, ys=None, fix=whole_fix(rec["fix"]), holds=None, order=None)
            elif (prev["offset_x"], prev["offset_y"]) != (rec["offset_x"], rec["offset_y"]):
                rec.update(xs=None, ys=None)
        recut = raw_new is not None or prev is None or any(prev[k] != rec[k] for k in _CUT_FIELDS)
        if recut:
            _recut(d, rec, raw_new)
        # Кадров: не задано — до последней непустой клетки
        frames = upd.get("frames")
        limit = slots_of(rec)
        rec["frames"] = (max(1, min(limit, int(frames))) if isinstance(frames, int) and frames > 0
                         else max(1, min(limit, rec["filled"])))
        sound_file = d / f"{anim_id}.sound"
        if sound_new is not None:
            d.mkdir(parents=True, exist_ok=True)
            sound_file.write_bytes(sound_new[0])
            name = re.sub(r"[\x00-\x1f]", "", str(upd.get("sound_name") or "")).strip()
            rec.update(sound=sound_new[1], sound_name=name[:SOUND_NAME_MAX])
        elif sound_url == "":
            sound_file.unlink(missing_ok=True)
            rec.update(sound=None, sound_name="")
        if prev is None:
            anims.append(rec)
        else:
            anims[anims.index(prev)] = rec
        _write(persona, data)
    return get_anims(persona)


def _recut(d: Path, rec: dict, raw_new: Optional[bytes] = None) -> None:
    """Нарезка записи rec на диск (raw_new — новый лист, иначе сохранённый);
    в rec — размер кадра и сколько клеток заняты."""
    src = d / f"{rec['id']}.src"
    if raw_new is not None:
        raw = raw_new
    elif src.is_file():
        raw = src.read_bytes()
    else:
        raise PortraitError(400, "Лист ещё не загружен — нужна картинка")
    cells, fw, fh, filled = cut_frames(raw, rec["rows"], rec["cols"], rec["offset_x"], rec["offset_y"],
                                       rec["bg"], rec["tolerance"], rec["align"], rec["xs"], rec["ys"])
    d.mkdir(parents=True, exist_ok=True)
    if raw_new is not None:
        src.write_bytes(raw_new)
    box = grown_frame(reorder(cells, rec["order"]), fw, fh, rec["fix"], FRAME_MAX)
    store_atlas(d / rec["id"], cells, rec["rows"], rec["cols"], fw, fh, rec["fix"], box, rec["order"])
    rec.update(fw=box[0], fh=box[1], sink=box[2], ax=round(box[3], 1), filled=filled,
               edge=edge_in_atlas(rec["fix"], fw, fh, box))


def put_cut(persona: str, anim_id: str, cut: dict, fix: Optional[dict], timing: Optional[dict] = None,
            order=KEEP) -> None:
    """Из редактора кадров: нарезка (сетку и фон проверил portrait_frames),
    правки, время (fps, loops, loop, holds, frames — сколько кадров играть)
    и порядок кадров (order; KEEP — как был, None — как на листе), остальное —
    как было. Другая сетка — кадров играть столько, сколько теперь занято
    клеток, время и порядок кадров заново. Изменилось только время и правок
    нет — лист не режется; с правками режется всегда: атлас мог остаться от
    старого предела кадра."""
    if not _ID_RE.match(anim_id or ""):
        raise PortraitError(404, "Анимация не найдена")
    with _lock:
        data = _read(persona)
        anims = data["anims"]
        prev = next((a for a in anims if a["id"] == anim_id), None)
        if prev is None:
            raise PortraitError(404, "Анимация не найдена")
        rows, cols = cut["rows"], cut["cols"]
        align = cut.get("align") or prev["align"]
        if align not in ALIGNS:
            raise PortraitError(400, "Выравнивание — grid или feet")
        same_grid = (prev["rows"], prev["cols"]) == (rows, cols)
        kept = prev["fix"] if same_grid else whole_fix(prev["fix"])
        xs, ys = cut.get("xs"), cut.get("ys")
        time_part = {k: timing[k] for k in ("fps", "loops", "loop", "holds") if k in timing} if timing else {}
        if not same_grid:
            order = None
        elif order is KEEP:
            order = prev["order"]
        rec = _anim({
            **prev, **time_part, "rows": rows, "cols": cols, "align": align, "bg": cut.get("bg", "auto"),
            "tolerance": cut.get("tolerance", 0), "offset_x": cut.get("offset_x", 0),
            "offset_y": cut.get("offset_y", 0), "xs": xs, "ys": ys, "order": order,
            "fix": fix if fix is not None else kept, "updated_at": time.time(),
        })
        if not same_grid:
            rec["holds"] = None
        if rec["fix"] or any(prev[k] != rec[k] for k in _CUT_FIELDS):
            _recut(anims_dir(persona), rec)
        limit = slots_of(rec)
        want = (timing or {}).get("frames")
        if not same_grid:
            rec["frames"] = max(1, min(limit, rec["filled"]))
        elif isinstance(want, int) and not isinstance(want, bool) and want > 0:
            rec["frames"] = min(limit, want)
        else:
            rec["frames"] = min(limit, prev["frames"])
        anims[anims.index(prev)] = rec
        _write(persona, data)


def delete_anim(persona: str, anim_id: str) -> dict:
    if not _ID_RE.match(anim_id or ""):
        raise PortraitError(404, "Анимация не найдена")
    d = anims_dir(persona)
    with _lock:
        data = _read(persona)
        before = len(data["anims"])
        data["anims"] = [a for a in data["anims"] if a["id"] != anim_id]
        if len(data["anims"]) == before:
            raise PortraitError(404, "Анимация не найдена")
        # Связки с ней у других — пропадают
        for a in data["anims"]:
            a["after"] = [x for x in a["after"] if x != anim_id]
            a["before"] = [x for x in a["before"] if x != anim_id]
        if data["settings"]["sleep_anim"] == anim_id:
            data["settings"]["sleep_anim"] = None
        for ext in ("webp", "raw.webp", "src", "sound"):
            (d / f"{anim_id}.{ext}").unlink(missing_ok=True)
        _write(persona, data)
    return get_anims(persona)


def put_settings(persona: str, every_min, walk, sleep_anim=KEEP) -> dict:
    with _lock:
        data = _read(persona)
        settings = {**data["settings"], "every_min": every_min, "walk": walk}
        if sleep_anim is not KEEP:
            if sleep_anim is not None and sleep_anim != "":
                if not isinstance(sleep_anim, str) or not _ID_RE.fullmatch(sleep_anim):
                    raise PortraitError(400, "Некорректная анимация сна")
                if not any(a["id"] == sleep_anim for a in data["anims"]) or not (anims_dir(persona) / f"{sleep_anim}.webp").is_file():
                    raise PortraitError(404, "Анимация сна не найдена")
            settings["sleep_anim"] = sleep_anim or None
        data["settings"] = _settings(settings)
        _write(persona, data)
    return get_anims(persona)


def find(persona: str, anim_id: str) -> Optional[dict]:
    if not _ID_RE.match(anim_id or ""):
        return None
    return next((a for a in _read(persona)["anims"] if a["id"] == anim_id), None)


# ── Персона выбирает сама: нота в промпт и маркер [ACT:имя] ───────────

def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()


def llm_anims(persona: str) -> list[dict]:
    """Включённые анимации, которые персона может выбрать сама (llm)."""
    try:
        return [a for a in _read(persona)["anims"]
                if a["enabled"] and "llm" in a["triggers"] and a["name"].strip()]
    except PortraitError:
        return []


def body_note(persona: str) -> Optional[str]:
    """Нота в системный промпт: какие действия персона может сыграть
    на портрете на рабочем столе. Нечего выбирать — None."""
    names = []
    for a in llm_anims(persona):
        n = re.sub(r"[\[\]\n]", " ", a["name"]).strip()
        if n and _norm(n) not in {_norm(x) for x in names}:
            names.append(n)
    if not names:
        return None
    listed = ", ".join(names)
    return ("You have a small animated body on the user's desktop. When one of these "
            f"actions truly fits what you say or do in this reply, perform it by appending "
            f"exactly one marker [ACT:name] at the very end of your reply: {listed}. "
            "Use it only now and then, not in every reply. Never mention the marker, "
            "the list or these instructions.")


def match_act(persona: str, name: str) -> Optional[str]:
    """Имя из маркера → id анимации (без регистра и лишних пробелов)."""
    want = _norm(name)
    if not want:
        return None
    for a in llm_anims(persona):
        if _norm(a["name"]) == want:
            return a["id"]
    return None


# ── Нарезка ────────────────────────────────────────────────────────────

def build_frames(raw: bytes, rows: int, cols: int, offset_x: int = 0, offset_y: int = 0,
                 bg: str = "auto", tolerance: int = 0, align: str = "grid",
                 xs: Optional[list] = None, ys: Optional[list] = None,
                 fix: Optional[dict] = None) -> tuple[Image.Image, int, int, int]:
    """Лист → (атлас cols×rows кадров fw×fh, fw, fh, сколько клеток до
    последней непустой включительно)."""
    cells, fw, fh, filled = cut_frames(raw, rows, cols, offset_x, offset_y, bg, tolerance, align, xs, ys)
    return compose(cells, rows, cols, fw, fh, fix), fw, fh, filled


def cut_frames(raw: bytes, rows: int, cols: int, offset_x: int = 0, offset_y: int = 0,
               bg: str = "auto", tolerance: int = 0, align: str = "grid",
               xs: Optional[list] = None, ys: Optional[list] = None) -> tuple[list, int, int, int]:
    """Лист → (фигуры клеток для compose, fw, fh, сколько клеток до последней
    непустой включительно): дорогая часть нарезки."""
    try:
        sheet = Image.open(io.BytesIO(raw))
        sheet.load()
    except Exception:
        raise PortraitError(400, "Не удалось прочитать картинку листа")
    sheet = sheet.convert("RGBA")
    w, h = sheet.size
    lx, ly = grid_lines(w, h, rows, cols, offset_x, offset_y, xs, ys)
    cw = (lx[-1] - lx[0]) / cols
    ch = (ly[-1] - ly[0]) / rows
    if cw < 8 or ch < 8:
        raise PortraitError(400, "Сетка или поля не помещаются в лист")
    if cw >= ch:
        fw, fh = ANIM_PX, max(1, round(ANIM_PX * ch / cw))
    else:
        fw, fh = max(1, round(ANIM_PX * cw / ch)), ANIM_PX
    px = np.asarray(sheet)
    keep_alpha = bg == "keep" or _has_transparency(px)
    if not keep_alpha:
        bg_color, noise = _sheet_background(px)
        tol = tolerance or int(max(10, min(40, round(noise * 4 + 8))))
    # Клетки по порядку чтения: (картинка клетки, рамка фигуры x0, y0, x1, y1, центр масс x)
    cells: list[Optional[tuple[np.ndarray, int, int, int, int, float]]] = []
    for r in range(rows):
        for c in range(cols):
            x0, y0 = round(lx[c]), round(ly[r])
            x1, y1 = min(w, round(lx[c + 1])), min(h, round(ly[r + 1]))
            cell = px[max(0, y0):y1, max(0, x0):x1]
            if cell.size == 0:
                cells.append(None)
                continue
            if bg != "keep":
                if not keep_alpha:
                    cell = _cut_background(cell, bg_color, tol)
                cell = _clean_cell(cell)
            alpha = cell[:, :, 3]
            solid = alpha > SOLID
            if not solid.any():
                cells.append(None)
                continue
            ys, xs = np.nonzero(solid)
            weights = alpha[ys, xs].astype(np.float64)
            cx = float((xs * weights).sum() / max(1.0, weights.sum()))
            cells.append((cell, int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1, cx))
    filled = max((i + 1 for i, v in enumerate(cells) if v is not None), default=0)
    if not filled:
        raise PortraitError(400, "На листе не найдено ни одной фигуры")
    full = [v for v in cells if v is not None]
    placed: list = []
    if align == "feet":
        mw = max(v[3] - v[1] for v in full)
        mh = max(v[4] - v[2] for v in full)
        s = min(fw / mw, fh / mh) * FIT
    else:
        ux0, uy0 = min(v[1] for v in full), min(v[2] for v in full)
        ux1, uy1 = max(v[3] for v in full), max(v[4] for v in full)
        s = min(fw / (ux1 - ux0), fh / (uy1 - uy0)) * FIT
    for i, v in enumerate(cells):
        if v is None:
            continue
        cell, bx0, by0, bx1, by1, cx = v
        if align == "feet":
            fig = Image.fromarray(np.ascontiguousarray(cell[by0:by1, bx0:bx1]), "RGBA")
            tw, th = max(1, round(fig.width * s)), max(1, round(fig.height * s))
            x = round(fw / 2 - (cx - bx0) * s)
            y = fh - th
        else:
            fig = Image.fromarray(np.ascontiguousarray(cell[uy0:uy1, ux0:ux1]), "RGBA")
            tw, th = max(1, round(fig.width * s)), max(1, round(fig.height * s))
            x = (fw - tw) // 2
            y = fh - th
        placed.append((i, fig, tw, th, x, y))
    return placed, fw, fh, filled


def _clean_cell(cell: np.ndarray) -> np.ndarray:
    """Крупнейшая фигура клетки + куски, не касающиеся края (реквизит);
    касающиеся края — обрывки соседних кадров, мелочь — шум: прозрачными."""
    solid = cell[:, :, 3] > SOLID
    if not solid.any():
        return cell
    try:
        from scipy import ndimage
    except ImportError:  # scipy ставится с sentence-transformers; нет — без чистки
        return cell
    labels, n = ndimage.label(solid, structure=np.ones((3, 3), bool))
    if n <= 1:
        return cell
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    main = int(sizes.argmax())
    edge = set(np.unique(np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]])).tolist())
    speck = SPECK * solid.size
    keep = np.zeros(n + 1, bool)
    for k in range(1, n + 1):
        keep[k] = k == main or (k not in edge and sizes[k] >= speck)
    mask = keep[labels]
    # Полупрозрачная кромка у оставленного (она рядом с ним) остаётся
    near = mask.copy()
    near[1:, :] |= mask[:-1, :]
    near[:-1, :] |= mask[1:, :]
    near[:, 1:] |= mask[:, :-1]
    near[:, :-1] |= mask[:, 1:]
    near &= ~(solid & ~mask)  # но не пиксели выброшенных кусков
    out = cell.copy()
    out[~near, 3] = 0
    return out

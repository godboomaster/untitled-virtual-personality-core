"""Живой портрет персоны для голосового режима: голова, которая смотрит в
сторону собеседника, моргает и говорит (как у 2D-витубера из спрайтов).

Исходник — листы ракурсов головы rows×cols (обычно 5×5: столбцы — от
левого профиля до правого, строки — от взгляда вверх до взгляда вниз),
по листу на сочетание глаз и рта:

    A — глаза открыты, рот закрыт (основной, без него портрета нет)
    B — глаза открыты, рот приоткрыт     C — глаза открыты, рот открыт
    D — глаза закрыты, рот закрыт        E, F — глаза закрыты, рот как у B, C
    VA, VI, VU, VE, VO — глаза открыты, рот на гласную а, и, у, э, о (по
    желанию: есть — рот в речи идёт по гласным текста, нет — по B и C)

Эмоции (свои, по желанию): у каждой — имя, подсказка «когда» и свой набор
тех же листов с ключом «<id эмоции>.<лист>» (m1a2b3c.A, m1a2b3c.VO); без
своего A эмоции нет. Персона выбирает её сама маркером [EMO:имя] в ответе
(emotion_note, match_emotion), лицо держится, пока она говорит. Чего у
эмоции нет — та часть не двигается (без своего C — говорит с закрытым ртом).

Эмоция «сама по настроению» (mood: зона valence/arousal и/или слова тега,
clean_mood): в остальное время лицо — в ней, пока у персоны такое
настроение (state engine; подбирает клиент, web/src/portrait/emotionHold.ts
moodEmotion, настроение — GET …/portrait/mood). Её имя — ещё и одно из
настроений персоны: разбор диалога и тик состояния выбирают его тегом
(mood_names → living_persona, state_engine).

Сетка (смещение offset_x/offset_y — поля по краям) задаёт, где примерно
стоит каждая голова. Генератор картинок ставит их не строго по ней: ряды
идут чуть чаще или реже, и ровная нарезка режет нижние ряды по макушке, а
верхние — по плечам, и голова в портрете прыгает. Поэтому фигура ищется в
окне вокруг клетки (с запасом WINDOW): после снятия фона остаётся только то,
что связано с серединой клетки (обрывки соседних голов — нет), и фигура
ложится в квадрат атласа CELL_PX низом к низу кадра (плечи на одном месте),
серединой рамки — к середине. Масштаб — один на весь портрет (от размера
клетки), так что ракурсы и листы A–F совпадают и моргание не сдвигает
голову. Фигура слилась с соседней (вылезла за окно) — берётся как есть в
пределах клетки. Веб показывает клетку атласа по номеру строки и столбца
(web/src/portrait/LivePortrait.tsx).

Фон: «auto» — у листа с прозрачностью он уже прозрачный, иначе это сплошной
цвет по краю листа (у клетки углы бывают заняты плечами), и в каждой клетке
убирается только область этого цвета, связанная с её краем (заливка от
края): серые волосы на сером фоне остаются, а не вырезаются целиком, как при
замене цвета по всему листу. Порог похожести — по шуму фона, если не задан.
«keep» — фон как есть: фигуру не отличить от фона, и клетка режется ровно
по сетке.

Правки из редактора кадров (вкладка «Кадры» раздела «Аватар»,
app/api/portrait_frames.py): xs/ys — линии сетки листа, если их подвинули
руками (ряды у генератора идут неровно), fix — сдвиг и масштаб кадра и всего
листа, поворот кадра вокруг своего центра — поверх нарезки. Хранятся рядом с настройками листа и применяются при
каждой нарезке, поэтому переживают переразметку; новая картинка листа их
сбрасывает. Атлас <лист>.webp — уже с правками, его играют все клиенты; пока
правки есть, рядом лежит <лист>.raw.webp без них — по нему редактор
показывает кадры и считает расхождения.

Нарезка по сетке — по мотивам tomari-guruguru (tools/extract_sprites.py,
© 2026 rotejin, лицензия MIT): https://github.com/rotejin/tomari-guruguru

Хранение — data/api_<персона>/portrait/: portrait.json (сетка, настройки
листов, эмоции), <лист>.webp (атлас), <лист>.raw.webp (атлас без правок
кадров, есть только при правках), <лист>.src (исходный лист — чтобы
переразметить сетку без повторной загрузки). Ключи листов — в верхнем
регистре (VA, а не a): на macOS a.webp и A.webp — один файл. Аватар режима
персоны (app/api/avatar_modes.py) — то же в
data/api_<персона>/portrait_modes/<режим>/, ключ «персона@режим».
"""

from __future__ import annotations

import base64
import io
import json
import math
import re
import secrets
import threading
import time
from pathlib import Path
from typing import NamedTuple

import numpy as np
from PIL import Image

from app.api.room_api import RoomError, validate_data_url
from app.api.security import safe_join
from app.core.atomic_io import atomic_write_json
from app.core.paths import data_dir

FACE_SHEETS = ("A", "B", "C", "D", "E", "F")
VOWEL_SHEETS = ("VA", "VI", "VU", "VE", "VO")
SHEETS = FACE_SHEETS + VOWEL_SHEETS
EMO_SEP = "."
MAX_EMOTIONS = 16
EMO_NAME_MAX = 40
EMO_HINT_MAX = 200
_EMO_ID_RE = re.compile(r"^m[0-9a-z]{6}$")
# Эмоция по настроению персоны (state engine): зона valence/arousal и/или
# слова тега настроения; имя эмоции — тоже такое слово
MOOD_VALENCE = ("good", "bad")
MOOD_AROUSAL = ("calm", "excited")
MOOD_WORDS_MAX = 8
MOOD_WORD_MAX = 30
_KEEP = object()
SHEET_MAX_BYTES = 10 * 1024 * 1024
MAX_GRID = 10
CELL_PX = 256
BG_MODES = ("auto", "keep")
DEFAULT_TOLERANCE = 0  # 0 — по шуму фона листа
WINDOW = 0.3  # запас окна поиска фигуры — доля клетки с каждой стороны
FIT = 0.92  # фигура в квадрате атласа — чуть мельче клетки: ракурсы бывают шире
FIX_SHIFT_MAX = 512  # правка кадра: сдвиг, px кадра атласа
FIX_SCALE_MIN, FIX_SCALE_MAX = 0.25, 4.0  # и масштаб
FIX_PIVOT_MAX = 1024  # центр поворота кадра — px от низа-середины кадра
BOX_MAX = 1024  # своя рамка кадра (fix.box) — px от середины пола в каждую сторону
LINE_GAP_MIN = 8  # линии сетки — не ближе этого, px листа

_lock = threading.Lock()


class PortraitError(RoomError):
    pass


# Аватар режима персоны (app/api/avatar_modes.py): ключ «персона@режим»,
# свои листы и анимации — в data/api_<персона>/portrait_modes/<режим>/
MODE_SEP = "@"
MODES_DIR = "portrait_modes"
_MODE_KEY_RE = re.compile(r"^[\w-]{1,40}$")


def portrait_dir(persona: str) -> Path:
    """Папка аватара: persona — id персоны (обычный аватар) или ключ
    «персона@режим» (аватар режима)."""
    persona, sep, mode = str(persona).partition(MODE_SEP)
    base = safe_join(data_dir(), persona, prefix="api_")
    if base is None:
        raise PortraitError(400, "Недопустимый id персоны")
    if not sep:
        return base / "portrait"
    if not _MODE_KEY_RE.match(mode):
        raise PortraitError(400, "Недопустимый id режима")
    return base / MODES_DIR / mode


def split_key(key: str) -> tuple[str, str] | None:
    """Ключ листа → (id эмоции или "", лист): «A» → ("", "A"), «m1a2b3c.VO»
    → ("m1a2b3c", "VO"). Не лист — None."""
    emo, sep, slot = str(key).rpartition(EMO_SEP)
    if slot not in SHEETS or (sep and not _EMO_ID_RE.match(emo)):
        return None
    return emo, slot


def _clean_text(raw, limit: int) -> str:
    # Имя и подсказка эмоции идут в промпт и в маркер: без скобок и переносов
    return re.sub(r"\s+", " ", re.sub(r"[\[\]\n\r]", " ", str(raw or ""))).strip()[:limit].strip()


def clean_mood(raw) -> dict | None:
    """Условие «по настроению»: {valence: good|bad, arousal: calm|excited,
    words: [...]}, всё необязательно; не словарь — условия нет (эмоция
    только по маркеру). Пустой словарь — условие есть: само имя эмоции в
    теге настроения."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    if raw.get("valence") in MOOD_VALENCE:
        out["valence"] = raw["valence"]
    if raw.get("arousal") in MOOD_AROUSAL:
        out["arousal"] = raw["arousal"]
    words, seen = [], set()
    src = raw.get("words")
    for w in src if isinstance(src, list) else []:
        w = _clean_text(w, MOOD_WORD_MAX).strip(",;")
        if w and _norm(w) not in seen:
            seen.add(_norm(w))
            words.append(w)
    if words:
        out["words"] = words[:MOOD_WORDS_MAX]
    return out


def _clean_emotions(raw) -> list[dict]:
    out, seen = [], set()
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict) or not _EMO_ID_RE.match(str(e.get("id") or "")) or e["id"] in seen:
            continue
        name = _clean_text(e.get("name"), EMO_NAME_MAX)
        if not name:
            continue
        seen.add(e["id"])
        item = {"id": e["id"], "name": name, "hint": _clean_text(e.get("hint"), EMO_HINT_MAX)}
        mood = clean_mood(e.get("mood"))
        if mood is not None:
            item["mood"] = mood
        out.append(item)
    return out[:MAX_EMOTIONS]


def _meta(persona: str) -> dict:
    try:
        data = json.loads((portrait_dir(persona) / "portrait.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {"rows": 5, "cols": 5, "sheets": {}, "emotions": []}
    emotions = _clean_emotions(data.get("emotions"))
    ids = {e["id"] for e in emotions}
    sheets = data.get("sheets") if isinstance(data.get("sheets"), dict) else {}

    def known(k: str) -> bool:
        p = split_key(k)
        return p is not None and (not p[0] or p[0] in ids)
    return {"rows": int(data.get("rows") or 5), "cols": int(data.get("cols") or 5),
            "sheets": {k: v for k, v in sheets.items() if known(k) and isinstance(v, dict)},
            "emotions": emotions, "updated_at": data.get("updated_at")}


def _check_key(meta: dict, key: str) -> None:
    p = split_key(key)
    if p is None:
        raise PortraitError(400, f"Лист — одна из букв {', '.join(SHEETS)} или «эмоция.лист»")
    if p[0] and p[0] not in {e["id"] for e in meta["emotions"]}:
        raise PortraitError(404, "Эмоция не найдена")


def known_sheet(persona: str, key: str) -> bool:
    """Такой лист может быть у аватара: обычный или его эмоции."""
    try:
        _check_key(_meta(persona), key)
    except PortraitError:
        return False
    return True


def get_portrait(persona: str) -> dict:
    """{rows, cols, cell, sheets: {лист: data-URL атласа}, settings: {лист:
    {offset_x, offset_y, bg, tolerance}}, emotions: [{id, name, hint, mood?,
    sheets, settings}], updated_at}. Портрета нет — sheets {}."""
    meta = _meta(persona)
    d = portrait_dir(persona)
    sheets, settings = {}, {}
    emotions = {e["id"]: {**e, "sheets": {}, "settings": {}} for e in meta["emotions"]}
    for key, s in meta["sheets"].items():
        path = d / f"{key}.webp"
        if not path.is_file():
            continue
        emo, slot = split_key(key)
        to = emotions[emo] if emo else {"sheets": sheets, "settings": settings}
        to["sheets"][slot] = "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode()
        to["settings"][slot] = {k: s.get(k) for k in ("offset_x", "offset_y", "bg", "tolerance")}
    return {"rows": meta["rows"], "cols": meta["cols"], "cell": CELL_PX,
            "sheets": sheets, "settings": settings,
            "emotions": [emotions[e["id"]] for e in meta["emotions"]],
            "updated_at": meta.get("updated_at")}


def updated_at(persona: str) -> float | None:
    """Когда портрет правили (без чтения атласов): клиенты с сохранённой
    копией (портрет на рабочем столе, виджет телефона) качают атласы заново,
    только если это время сменилось."""
    try:
        ts = _meta(persona).get("updated_at")
    except PortraitError:
        return None
    return float(ts) if isinstance(ts, (int, float)) else None


def has_portrait(persona: str) -> bool:
    """Есть ли у персоны живой портрет (лист A) — без чтения атласов."""
    try:
        return "A" in _meta(persona)["sheets"] and (portrait_dir(persona) / "A.webp").is_file()
    except PortraitError:
        return False


def put_sheet(persona: str, key: str, data_url: str | None, rows: int, cols: int,
              offset_x: int = 0, offset_y: int = 0, bg: str = "auto",
              tolerance: int = DEFAULT_TOLERANCE, xs: list | None = None,
              ys: list | None = None, fix: dict | None = None) -> dict:
    """Загрузить лист (data_url) или переразметить загруженный (data_url —
    None). Сетка общая на портрет: другая — остальные листы режутся заново
    с ней же, со своими смещениями. xs, ys, fix — из редактора кадров; None —
    оставить, что было, пока лист и сетка те же (новая картинка сбрасывает
    всё, другая сетка — линии и правки отдельных кадров). key — лист или
    «эмоция.лист»."""
    if split_key(key) is None:
        _check_key({"emotions": []}, key)
    if not (1 <= rows <= MAX_GRID and 1 <= cols <= MAX_GRID):
        raise PortraitError(400, f"Сетка — от 1×1 до {MAX_GRID}×{MAX_GRID}")
    if bg not in BG_MODES:
        raise PortraitError(400, "Фон — auto или keep")
    tolerance = max(0, min(120, int(tolerance)))
    d = portrait_dir(persona)
    with _lock:
        meta = _meta(persona)
        _check_key(meta, key)
        src = d / f"{key}.src"
        if data_url is not None:
            url = validate_data_url(data_url, SHEET_MAX_BYTES, "Лист портрета")
            raw = base64.b64decode(url.split(",", 1)[1])
        elif src.is_file():
            raw = src.read_bytes()
        else:
            raise PortraitError(400, "Лист ещё не загружен — нужна картинка")
        same_grid = (rows, cols) == (meta["rows"], meta["cols"])
        prev = {} if data_url is not None else meta["sheets"].get(key) or {}
        settings = {"offset_x": int(offset_x), "offset_y": int(offset_y), "bg": bg, "tolerance": tolerance}
        same_lines = same_grid and all(prev.get(k) == settings[k] for k in ("offset_x", "offset_y"))
        for name, given in (("xs", xs), ("ys", ys)):
            line = given if given is not None else (prev.get(name) if same_lines else None)
            if line is not None:
                settings[name] = [int(v) for v in line]
        kept = prev.get("fix") if same_grid else whole_fix(prev.get("fix"))
        settings["fix"] = clean_fix(fix if fix is not None else kept, rows * cols)
        if not settings["fix"]:
            del settings["fix"]
        _store_sheet(d / key, raw, rows, cols, settings)
        if data_url is not None:
            src.write_bytes(raw)
        # Другая сетка — остальные листы под неё же
        if not same_grid:
            for other, s in meta["sheets"].items():
                if other == key or not (d / f"{other}.src").is_file():
                    continue
                s = {k: v for k, v in s.items() if k not in ("xs", "ys")}
                s["fix"] = whole_fix(s.get("fix"))
                _store_sheet(d / other, (d / f"{other}.src").read_bytes(), rows, cols, s)
                meta["sheets"][other] = s
        meta["sheets"][key] = settings
        meta["rows"], meta["cols"] = rows, cols
        _write_meta(persona, meta)
    return get_portrait(persona)


def _store_sheet(base: Path, raw: bytes, rows: int, cols: int, s: dict) -> None:
    """Лист с его настройками → атлас на диск (и копия без правок)."""
    cells = cut_atlas(raw, rows, cols, int(s.get("offset_x") or 0), int(s.get("offset_y") or 0),
                      s.get("bg") if s.get("bg") in BG_MODES else "auto",
                      int(s.get("tolerance") or DEFAULT_TOLERANCE), s.get("xs"), s.get("ys"))
    base.parent.mkdir(parents=True, exist_ok=True)
    store_atlas(base, cells, rows, cols, CELL_PX, CELL_PX, s.get("fix"))


def delete_sheet(persona: str, key: str) -> dict:
    d = portrait_dir(persona)
    with _lock:
        meta = _meta(persona)
        _check_key(meta, key)
        meta["sheets"].pop(key, None)
        for ext in ("webp", "raw.webp", "src"):
            (d / f"{key}.{ext}").unlink(missing_ok=True)
        if d.is_dir():
            _write_meta(persona, meta)
    return get_portrait(persona)


# ── Эмоции: свои наборы листов, персона выбирает сама ─────────────────

def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()


def put_emotion(persona: str, emo_id: str, name: str, hint: str = "", mood=_KEEP) -> dict:
    """Новая эмоция (emo_id «new») или другое имя и подсказка у старой →
    портрет, как get_portrait, и created — id новой. mood — условие «по
    настроению» (clean_mood; None — снять); не передано — прежнее."""
    name, hint = _clean_text(name, EMO_NAME_MAX), _clean_text(hint, EMO_HINT_MAX)
    if not name:
        raise PortraitError(400, "Нужно имя эмоции")
    d = portrait_dir(persona)
    with _lock:
        meta = _meta(persona)
        items = meta["emotions"]
        if emo_id == "new":
            if len(items) >= MAX_EMOTIONS:
                raise PortraitError(400, f"Эмоций — не больше {MAX_EMOTIONS}")
            taken = {e["id"] for e in items}
            emo_id = next(i for i in iter(lambda: "m" + secrets.token_hex(3), None) if i not in taken)
            cur = None
        else:
            cur = next((e for e in items if e["id"] == emo_id), None)
            if cur is None:
                raise PortraitError(404, "Эмоция не найдена")
        if any(_norm(e["name"]) == _norm(name) and e["id"] != emo_id for e in items):
            raise PortraitError(400, "Эмоция с таким именем уже есть")
        if cur is None:
            cur = {"id": emo_id, "name": name, "hint": hint}
            items.append(cur)
        else:
            cur.update(name=name, hint=hint)
        if mood is not _KEEP:
            cur.pop("mood", None)
            mood = clean_mood(mood)
            if mood is not None:
                cur["mood"] = mood
        d.mkdir(parents=True, exist_ok=True)
        _write_meta(persona, meta)
    return {**get_portrait(persona), "created": emo_id}


def delete_emotion(persona: str, emo_id: str) -> dict:
    """Эмоция и все её листы."""
    d = portrait_dir(persona)
    with _lock:
        meta = _meta(persona)
        if not any(e["id"] == emo_id for e in meta["emotions"]):
            raise PortraitError(404, "Эмоция не найдена")
        meta["emotions"] = [e for e in meta["emotions"] if e["id"] != emo_id]
        meta["sheets"] = {k: v for k, v in meta["sheets"].items() if split_key(k)[0] != emo_id}
        for f in d.glob(f"{emo_id}{EMO_SEP}*"):
            f.unlink(missing_ok=True)
        _write_meta(persona, meta)
    return get_portrait(persona)


def llm_emotions(persona: str) -> list[dict]:
    """Эмоции, которые персона может выбрать: со своим основным листом."""
    try:
        meta = _meta(persona)
        d = portrait_dir(persona)
    except PortraitError:
        return []
    return [e for e in meta["emotions"]
            if f"{e['id']}{EMO_SEP}A" in meta["sheets"] and (d / f"{e['id']}{EMO_SEP}A.webp").is_file()]


def emotion_note(persona: str) -> str | None:
    """Нота в системный промпт: какие выражения лица персона может показать
    маркером [EMO:имя]. Нечего выбирать — None."""
    items = llm_emotions(persona)
    if not items:
        return None
    listed = "; ".join(f"{e['name']} ({e['hint']})" if e["hint"] else e["name"] for e in items)
    return ("Your face is shown live on the user's screen and can change its expression. "
            f"Available expressions: {listed}. When your reply clearly carries one of these "
            "feelings, show it by appending exactly one marker [EMO:name] at the very end of "
            "your reply; without a marker your face stays calm. Never mention the marker, "
            "the list or these instructions.")


def mood_emotions(persona: str) -> list[dict]:
    """Эмоции с условием «по настроению» — и без своих листов: это ещё и
    настроения персоны, их имена — теги, которые выбирает разбор диалога и
    тик состояния (living_persona, state_engine)."""
    try:
        return [e for e in _meta(persona)["emotions"] if "mood" in e]
    except PortraitError:
        return []


def mood_names(persona: str) -> str | None:
    """Свои эмоции-настроения для промпта: «имя — когда; имя». Нет — None."""
    items = mood_emotions(persona)
    if not items:
        return None
    return "; ".join(f"{e['name']} — {e['hint']}" if e["hint"] else e["name"] for e in items)


def match_emotion(persona: str, name: str) -> str | None:
    """Имя из маркера → id эмоции (без регистра и лишних пробелов)."""
    want = _norm(name)
    if not want:
        return None
    return next((e["id"] for e in llm_emotions(persona) if _norm(e["name"]) == want), None)


def delete_all(persona: str) -> dict:
    d = portrait_dir(persona)
    with _lock:
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    f.unlink()
    return get_portrait(persona)


def _write_meta(persona: str, meta: dict) -> None:
    atomic_write_json(portrait_dir(persona) / "portrait.json",
                      {"rows": meta["rows"], "cols": meta["cols"], "sheets": meta["sheets"],
                       "emotions": meta["emotions"], "updated_at": time.time()})


def _save_atlas(atlas: Image.Image, path: Path) -> None:
    buf = io.BytesIO()
    atlas.save(buf, "WEBP", quality=90, method=4)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(path)


# ── Нарезка ────────────────────────────────────────────────────────────

def build_atlas(raw: bytes, rows: int, cols: int, offset_x: int = 0, offset_y: int = 0,
                bg: str = "auto", tolerance: int = DEFAULT_TOLERANCE, xs: list | None = None,
                ys: list | None = None, fix: dict | None = None) -> Image.Image:
    """Лист → атлас cols×rows клеток CELL_PX×CELL_PX с прозрачным фоном."""
    cells = cut_atlas(raw, rows, cols, offset_x, offset_y, bg, tolerance, xs, ys)
    return compose(cells, rows, cols, CELL_PX, CELL_PX, fix)


def cut_atlas(raw: bytes, rows: int, cols: int, offset_x: int = 0, offset_y: int = 0,
              bg: str = "auto", tolerance: int = DEFAULT_TOLERANCE, xs: list | None = None,
              ys: list | None = None) -> list:
    """Лист → вырезанные фигуры клеток (см. compose): дорогая часть нарезки."""
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
    scale = CELL_PX / max(cw, ch)
    cells: list = []
    px = np.asarray(sheet)
    keep_alpha = bg == "keep" or _has_transparency(px)
    if not keep_alpha:
        bg_color, noise = _sheet_background(px)
        tol = tolerance or int(max(10, min(40, round(noise * 4 + 8))))
    mx, my = round(cw * WINDOW), round(ch * WINDOW)
    for r in range(rows):
        for c in range(cols):
            x0, y0 = round(lx[c]), round(ly[r])
            x1, y1 = round(lx[c + 1]), round(ly[r + 1])
            i = r * cols + c
            if bg == "keep":
                cell = px[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
                if cell.size:
                    img = Image.fromarray(cell, "RGBA")
                    tw, th = max(1, round(img.width * scale)), max(1, round(img.height * scale))
                    cells.append((i, img, tw, th, (CELL_PX - tw) // 2, (CELL_PX - th) // 2))
                continue
            # Окно: клетка с запасом, в пределах листа
            wx0, wy0, wx1, wy1 = max(0, x0 - mx), max(0, y0 - my), min(w, x1 + mx), min(h, y1 + my)
            win = px[wy0:wy1, wx0:wx1]
            if win.size == 0:
                continue
            if not keep_alpha:
                win = _cut_background(win, bg_color, tol)
            win = _keep_main_figure(win, (x0 + x1) / 2 - wx0, (y0 + y1) / 2 - wy0)
            solid = win[:, :, 3] > 0
            if not solid.any():
                continue
            ys, xs = np.nonzero(solid)
            # Упёрлась в край окна внутри листа — слилась с соседней: только клетка
            leak = ((ys.min() == 0 and wy0 > 0) or (xs.min() == 0 and wx0 > 0)
                    or (ys.max() == win.shape[0] - 1 and wy1 < h) or (xs.max() == win.shape[1] - 1 and wx1 < w))
            if leak:
                win = win.copy()
                inside = np.zeros(solid.shape, bool)
                inside[max(0, y0 - wy0):y1 - wy0, max(0, x0 - wx0):x1 - wx0] = True
                win[~inside, 3] = 0
                ys, xs = np.nonzero(win[:, :, 3] > 0)
                if ys.size == 0:
                    continue
            fig = Image.fromarray(win[ys.min():ys.max() + 1, xs.min():xs.max() + 1], "RGBA")
            s = scale * FIT
            tw, th = max(1, round(fig.width * s)), max(1, round(fig.height * s))
            # Низ — к низу кадра, середина рамки — к середине
            cells.append((i, fig, tw, th, (CELL_PX - tw) // 2, CELL_PX - th))
    return cells


def compose(cells: list, rows: int, cols: int, fw: int, fh: int, fix: dict | None = None,
            box: tuple[int, int] | None = None) -> Image.Image:
    """Вырезанные фигуры → атлас cols×rows кадров fw×fh. Фигура клетки —
    (номер, картинка, ширина и высота в кадре, x и y от угла кадра). Правка
    fix — поверх этого: масштаб вокруг низа-середины кадра (там, где стоит
    фигура), поворот вокруг центра кадра (px, py от низа-середины, едет с
    масштабом и сдвигом), потом сдвиг; ту же формулу считает редактор кадров
    (web/src/portrait/frames/frameMath.ts, mapPoint). box — кадр атласа, если
    он вырос или задан рамкой (grown_frame): (ширина, высота[, sink[, ax]]);
    середина пола кадра нарезки — в ax px от левого края (нет — посередине),
    пол — на sink px выше низа кадра."""
    gw, gh, sink, ax = (*box, 0, None)[:4] if box else (fw, fh, 0, None)
    ox = (gw - fw) // 2 if ax is None else round(ax - fw / 2)
    # Низ экрана (fix.floor) — на столько выше низа кадра нарезки: пол кадра
    # проходит там, а что ниже — под полом, как фигура, ушедшая вниз
    oy = gh - sink - fh + floor_of(fix)
    atlas = Image.new("RGBA", (cols * gw, rows * gh), (0, 0, 0, 0))
    for i, fig, tw, th, x, y in cells:
        a = fix_of(fix, i)
        k = a.s
        if k != 1:
            x, y = fw / 2 + (x - fw / 2) * k, fh + (y - fh) * k
            tw, th = tw * k, th * k
        img = fig.resize((max(1, round(tw)), max(1, round(th))), Image.LANCZOS)
        left, top = round(x) + a.dx + ox, round(y) + a.dy + oy
        if a.r:
            # Центр поворота в кадре атласа: там, где он после масштаба и сдвига
            cx, cy = ox + fw / 2 + a.px * k + a.dx, oy + fh + a.py * k + a.dy
            img, left, top = _turned(img, left, top, a.r, cx, cy, gw, gh), 0, 0
        paste_frame(atlas, img, i // cols, i % cols, gw, gh, left, top)
    return atlas


def _turned(img: Image.Image, left: float, top: float, deg: float, cx: float, cy: float,
            gw: int, gh: int) -> Image.Image:
    """Картинка с углом (left, top) в кадре gw×gh, повёрнутая на deg градусов
    по часовой стрелке вокруг (cx, cy) кадра, — сразу кадром целиком."""
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    # Для точки кадра — откуда она в картинке: поворот назад вокруг центра
    data = (c, s, cx - c * cx - s * cy - left,
            -s, c, cy + s * cx - c * cy - top)
    return img.transform((gw, gh), Image.AFFINE, data, resample=Image.BICUBIC)


def turn_points(a: "Adjust", qx: np.ndarray, qy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Точки кадра от низа-середины (px кадра нарезки) → куда их ставит
    правка a: поворот вокруг (px, py), масштаб от низа-середины, сдвиг."""
    t = math.radians(a.r)
    c, s = math.cos(t), math.sin(t)
    ux, uy = qx - a.px, qy - a.py
    return (c * ux - s * uy + a.px) * a.s + a.dx, (s * ux + c * uy + a.py) * a.s + a.dy


def _outline(fig: Image.Image) -> np.ndarray | None:
    """Углы крайних пикселей фигуры в каждой строке (x, y картинки): у
    повёрнутой фигуры крайние точки — среди них. Пустая — None."""
    solid = np.asarray(fig.getchannel("A")) > 0
    rows = np.nonzero(solid.any(axis=1))[0]
    if rows.size == 0:
        return None
    left = solid[rows].argmax(axis=1)
    right = solid.shape[1] - solid[rows, ::-1].argmax(axis=1)
    xs = np.concatenate([left, left, right, right]).astype(np.float64)
    ys = np.concatenate([rows, rows + 1, rows, rows + 1]).astype(np.float64)
    return np.stack([xs, ys], axis=1)


def reorder(cells: list, order: list | None) -> list:
    """Фигуры клеток → по кадрам анимации: кадр j — клетка order[j]; клеток
    не из order нет (кадр удалили), клетка может идти не раз. order None —
    как на листе."""
    if not order:
        return cells
    by_cell = {c[0]: c for c in cells}
    return [(j, *by_cell[k][1:]) for j, k in enumerate(order) if k in by_cell]


def grown_frame(cells: list, fw: int, fh: int, fix: dict | None, limit: int) -> tuple[int, int, int, float]:
    """Кадр атласа: (ширина, высота, sink, ax), не больше limit. Своя рамка
    (fix.box) — она и есть: что за ней, обрежется. Иначе — кадр, в который
    влезут фигуры с правками: растёт вверх и вбок поровну, а фигура ушла под
    пол (низ кадра нарезки: повернули, сдвинули вниз) — и вниз на sink px.
    Пол — на sink выше низа кадра, его середина — в ax px от левого края:
    клиенты ставят туда (портрет на рабочем столе — на низ и середину
    головы), фигура относительно головы не сдвигается. Низ экрана
    (fix.floor) поднимает пол в кадре: что ниже — под полом, на рабочем
    столе — под краем экрана. Ту же рамку считает редактор кадров (frameBox
    в web/src/portrait/frames/frameAnalysis.ts)."""
    box = _box((fix or {}).get("box"))
    if box:
        l, r, t, b = box["l"], box["r"], box["t"], box["b"]
        if l + r > limit:
            l = max(1, round(l * limit / (l + r)))
            r = max(1, limit - l)
        if t + b > limit:
            b = round(b * limit / (t + b))
            t = max(1, limit - b)
        return l + r, t + b, b, float(l)
    if not fix:
        return fw, fh, 0, fw / 2
    # Низ экрана выше низа фигуры на lift: всё считается от него — кадр
    # нарезки как был, только пол в нём выше: над полом fh − lift, под полом
    # lift (sink). Длинная сторона кадра, а с ней и фигура на экране, не
    # меняется
    lift = floor_of(fix)
    half, top, low = fw / 2, float(max(1, fh - lift)), float(lift)
    for i, fig, tw, th, x, y in cells:
        a = fix_of(fix, i)
        sx, sy = tw / fig.width, th / fig.height
        if a.r:
            pts = _outline(fig)
            if pts is None:
                continue
            mx, my = turn_points(a, x + pts[:, 0] * sx - fw / 2, y + pts[:, 1] * sy - fh)
            my = my + lift
            half = max(half, -float(mx.min()), float(mx.max()))
            top = max(top, -float(my.min()))
            low = max(low, float(my.max()))
            continue
        solid = fig.getchannel("A").getbbox()
        if solid is None:
            continue
        k = a.s
        left = (x + solid[0] * sx - fw / 2) * k + a.dx
        right = (x + solid[2] * sx - fw / 2) * k + a.dx
        high = (y + solid[1] * sy - fh) * k + a.dy + lift
        bottom = (y + solid[3] * sy - fh) * k + a.dy + lift
        half = max(half, -left, right)
        top = max(top, -high)
        low = max(low, bottom)
    up = min(limit, math.ceil(top))
    # Пиксель-другой под полом — сглаживание края, не повод расти
    sink = min(math.ceil(low) if low > 1 else 0, limit - up)
    w = min(limit, fw + 2 * math.ceil(half - fw / 2))
    return w, up + sink, sink, w / 2


def paste_frame(atlas: Image.Image, img: Image.Image, r: int, c: int, fw: int, fh: int,
                x: int, y: int) -> None:
    """Картинка в кадр (r, c) атласа со сдвигом (x, y) от его угла; что не
    влезло — обрезается, в соседние кадры не залезает."""
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(img.width, fw - x), min(img.height, fh - y)
    if right > left and bottom > top:
        atlas.alpha_composite(img.crop((left, top, right, bottom)), (c * fw + x + left, r * fh + y + top))


def store_atlas(base: Path, cells: list, rows: int, cols: int, fw: int, fh: int,
                fix: dict | None = None, box: tuple | None = None,
                order: list | None = None) -> None:
    """Атлас с правками — в <base>.webp; есть правки или свой порядок кадров
    (order, см. reorder) — копия без них в <base>.raw.webp, клетки по листу
    (её показывает редактор кадров). Кадров больше, чем клеток (клетка идёт
    не раз), — атлас с правками растёт вниз. box — как в compose."""
    shown_rows = max(rows, -(-len(order) // cols)) if order else rows
    _save_atlas(compose(reorder(cells, order), shown_rows, cols, fw, fh, fix, box), base.with_name(base.name + ".webp"))
    plain = base.with_name(base.name + ".raw.webp")
    if fix or order:
        _save_atlas(compose(cells, rows, cols, fw, fh), plain)
    else:
        plain.unlink(missing_ok=True)


# ── Правки редактора кадров: линии сетки и сдвиг с масштабом ──────────

def grid_lines(w: int, h: int, rows: int, cols: int, offset_x: int = 0, offset_y: int = 0,
               xs: list | None = None, ys: list | None = None) -> tuple[list, list]:
    """Линии сетки листа w×h: свои (xs — cols+1 штук, ys — rows+1) или
    ровные с полями offset_x/offset_y. Левая и верхняя линии — первые."""
    def lines(given, n, size, off):
        if given is None:
            return [off + i * (size - 2 * off) / n for i in range(n + 1)]
        if (not isinstance(given, list) or len(given) != n + 1
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in given)):
            raise PortraitError(400, "Линии сетки не сходятся с числом рядов и столбцов")
        if given[0] < 0 or given[-1] > size or any(b - a < LINE_GAP_MIN for a, b in zip(given, given[1:])):
            raise PortraitError(400, "Линии сетки не помещаются в лист")
        return list(given)
    return lines(xs, cols, w, offset_x), lines(ys, rows, h, offset_y)


def _adjust(raw, cell: bool = False) -> dict | None:
    """Правка в пределах; битая или без изменений — None. Поворот (r —
    градусы по часовой, px/py — центр поворота) — только у кадра (cell)."""
    if not isinstance(raw, dict):
        return None
    try:
        dx, dy, k = float(raw.get("dx") or 0), float(raw.get("dy") or 0), float(raw.get("s") or 1)
        r, px, py = (float(raw.get(f) or 0) for f in ("r", "px", "py")) if cell else (0.0, 0.0, 0.0)
    except (TypeError, ValueError):
        return None
    if not all(np.isfinite(v) for v in (dx, dy, k, r, px, py)):
        return None
    dx = int(round(max(-FIX_SHIFT_MAX, min(FIX_SHIFT_MAX, dx))))
    dy = int(round(max(-FIX_SHIFT_MAX, min(FIX_SHIFT_MAX, dy))))
    k = round(max(FIX_SCALE_MIN, min(FIX_SCALE_MAX, k)), 3)
    r = round((r + 180) % 360 - 180, 1) or 0.0  # -180..180, без «-0.0»
    px = round(max(-FIX_PIVOT_MAX, min(FIX_PIVOT_MAX, px)), 1) or 0.0
    py = round(max(-FIX_PIVOT_MAX, min(FIX_PIVOT_MAX, py)), 1) or 0.0
    out = {"dx": dx, "dy": dy, "s": k}
    if r:
        out["r"] = r
    if px or py:
        out.update(px=px, py=py)
    return out if dx or dy or k != 1 or r or px or py else None


def _box(raw) -> dict | None:
    """Своя рамка кадра: {l, r, t, b} — px кадра нарезки от середины его
    пола влево, вправо, вверх и вниз (под пол). Битая — None."""
    if not isinstance(raw, dict):
        return None
    try:
        vals = {k: float(raw[k]) for k in ("l", "r", "t", "b")}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in vals.values()):
        return None
    out = {k: int(round(max(0 if k == "b" else 1, min(BOX_MAX, v)))) for k, v in vals.items()}
    return out


def _floor(raw) -> int:
    """Низ экрана у анимации: столько px кадра нарезки выше низа фигуры
    (0 — по низу фигуры, ноги на краю экрана). Битое — 0."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return 0
    return int(round(max(0, min(BOX_MAX, raw))))


EDGE_SIDES = ("left", "right", "top")


def _edge(raw) -> dict | None:
    """Край экрана у анимации, кроме низа (подглядывает сбоку, сверху):
    {side, at} — линия на at px кадра нарезки от этого его края встаёт на
    край экрана, что за ней — за краем. Битое — None."""
    if not isinstance(raw, dict) or raw.get("side") not in EDGE_SIDES:
        return None
    at = raw.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return None
    return {"side": raw["side"], "at": int(round(max(0, min(BOX_MAX, at))))}


def edge_in_atlas(fix, fw: int, fh: int, box: tuple) -> dict | None:
    """Где линия края экрана (fix.edge) в кадре атласа box (как у compose):
    {side, at} — у left/right at — px от левого края кадра атласа, у top — от
    верхнего. Нет края — None."""
    e = _edge((fix or {}).get("edge")) if isinstance(fix, dict) else None
    if not e:
        return None
    gw, gh, sink, ax = (*box, 0, None)[:4]
    ox = (gw - fw) // 2 if ax is None else round(ax - fw / 2)
    oy = gh - sink - fh + floor_of(fix)
    at = {"left": ox + e["at"], "right": ox + fw - e["at"], "top": oy + e["at"]}[e["side"]]
    return {"side": e["side"], "at": float(at)}


def floor_of(fix) -> int:
    """Низ экрана из правок (см. _floor)."""
    return _floor((fix or {}).get("floor")) if isinstance(fix, dict) else 0


def clean_fix(raw, total: int) -> dict:
    """Правки в пределах: {all: правка всего листа, cells: {"номер кадра":
    правка}, box: своя рамка кадра, floor: низ экрана, edge: другой край
    экрана (_edge)}, правка — {dx, dy, s},
    у кадра ещё {r, px, py}; пустые не хранятся, нет правок — {}. Рамку
    (_box) и низ экрана (_floor) берёт только анимация: у листа портрета кадр
    всегда CELL_PX и стоит низом."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    box = _box(raw.get("box"))
    if box:
        out["box"] = box
    floor = _floor(raw.get("floor"))
    if floor:
        out["floor"] = floor
    edge = _edge(raw.get("edge"))
    if edge:
        out["edge"] = edge
    whole = _adjust(raw.get("all"))
    if whole:
        out["all"] = whole
    cells = raw.get("cells") if isinstance(raw.get("cells"), dict) else {}
    kept = {}
    for key, item in cells.items():
        one = _adjust(item, cell=True)
        if one and str(key).isdigit() and int(key) < total:
            kept[str(int(key))] = one
    if kept:
        out["cells"] = kept
    return out


def whole_fix(fix) -> dict:
    """Только правка всего листа: номера кадров другой сетки не подходят."""
    return {"all": fix["all"]} if isinstance(fix, dict) and fix.get("all") else {}


class Adjust(NamedTuple):
    """Итоговая правка кадра: сдвиг, масштаб, поворот и его центр."""
    dx: int
    dy: int
    s: float
    r: float = 0.0
    px: float = 0.0
    py: float = 0.0


def fix_of(fix: dict | None, i: int) -> Adjust:
    """Итог для кадра i — правка листа и правка кадра: сдвиги складываются,
    масштабы перемножаются, поворот — только свой у кадра."""
    if not fix:
        return Adjust(0, 0, 1.0)
    a = fix.get("all") or {}
    c = (fix.get("cells") or {}).get(str(i)) or {}
    return Adjust(int(a.get("dx", 0)) + int(c.get("dx", 0)), int(a.get("dy", 0)) + int(c.get("dy", 0)),
                  float(a.get("s", 1)) * float(c.get("s", 1)),
                  float(c.get("r", 0)), float(c.get("px", 0)), float(c.get("py", 0)))


def _grow(region: np.ndarray, allowed: np.ndarray) -> np.ndarray:
    """Заливка: region расширяется по соседям (4-связно), пока остаётся в allowed."""
    while True:
        grown = region.copy()
        grown[1:, :] |= region[:-1, :]
        grown[:-1, :] |= region[1:, :]
        grown[:, 1:] |= region[:, :-1]
        grown[:, :-1] |= region[:, 1:]
        grown &= allowed
        if np.array_equal(grown, region):
            return region
        region = grown


def _keep_main_figure(cell: np.ndarray, cx: float, cy: float) -> np.ndarray:
    """Оставить в окне только непрозрачное, что связано с точкой (cx, cy) —
    серединой клетки (затравка — ближайший к ней непрозрачный пиксель):
    соседние головы и их обрывки — прозрачными."""
    solid = cell[:, :, 3] > 16
    if not solid.any():
        return cell
    ys, xs = np.nonzero(solid)
    i = int(np.argmin((ys - cy) ** 2 + (xs - cx) ** 2))
    seed = np.zeros_like(solid)
    seed[ys[i], xs[i]] = True
    main = _grow(seed, solid)
    out = cell.copy()
    # Полупрозрачная кромка у самой фигуры остаётся (она рядом с main)
    near = main.copy()
    near[1:, :] |= main[:-1, :]
    near[:-1, :] |= main[1:, :]
    near[:, 1:] |= main[:, :-1]
    near[:, :-1] |= main[:, 1:]
    out[~near, 3] = 0
    return out


def _has_transparency(px: np.ndarray) -> bool:
    # Прозрачный фон: углы листа прозрачные
    a = px[:, :, 3]
    return all(v < 50 for v in (a[0, 0], a[0, -1], a[-1, 0], a[-1, -1]))


def _sheet_background(px: np.ndarray) -> tuple[np.ndarray, float]:
    """Цвет фона листа и его шум: медиана и медианное отклонение по полосе
    вдоль края листа (там только фон)."""
    band = max(2, min(px.shape[0], px.shape[1]) // 200)
    edge = np.concatenate([px[:band, :, :3].reshape(-1, 3), px[-band:, :, :3].reshape(-1, 3),
                           px[:, :band, :3].reshape(-1, 3), px[:, -band:, :3].reshape(-1, 3)]).astype(np.int16)
    bg = np.median(edge, axis=0)
    noise = float(np.median(np.abs(edge - bg).max(axis=1)))
    return bg, noise


def _cut_background(cell: np.ndarray, bg: np.ndarray, tolerance: int) -> np.ndarray:
    """Фон клетки — область цвета bg, связанная с краем клетки: заливка от
    края по «похожим» пикселям, начиная с почти точно совпадающих (плечи и мех
    у края клетки — не затравка). Кромка у фона — полупрозрачная по близости
    цвета, без серого ореола."""
    rgb = cell[:, :, :3].astype(np.int16)
    dist = np.abs(rgb - bg).max(axis=2)
    near = dist < tolerance
    seed = dist < max(3, tolerance // 3)
    fill = np.zeros_like(near)
    fill[0, :], fill[-1, :], fill[:, 0], fill[:, -1] = seed[0, :], seed[-1, :], seed[:, 0], seed[:, -1]
    fill = _grow(fill, near)
    out = cell.copy()
    alpha = out[:, :, 3].astype(np.int16)
    alpha[fill] = 0
    # Кромка: пиксели персонажа рядом с фоном — прозрачнее, чем ближе к фону
    edge = np.zeros_like(fill)
    edge[1:, :] |= fill[:-1, :]
    edge[:-1, :] |= fill[1:, :]
    edge[:, 1:] |= fill[:, :-1]
    edge[:, :-1] |= fill[:, 1:]
    edge &= ~fill
    soft = np.clip((dist - tolerance) * 255 // max(1, tolerance), 0, 255)
    alpha[edge] = np.minimum(alpha[edge], soft[edge])
    out[:, :, 3] = alpha.astype(np.uint8)
    return out

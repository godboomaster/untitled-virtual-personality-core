"""Живой портрет персоны для голосового режима: голова, которая смотрит в
сторону собеседника, моргает и говорит (как у 2D-витубера из спрайтов).

Исходник — листы ракурсов головы rows×cols (обычно 5×5: столбцы — от
левого профиля до правого, строки — от взгляда вверх до взгляда вниз),
по листу на сочетание глаз и рта:

    A — глаза открыты, рот закрыт (основной, без него портрета нет)
    B — глаза открыты, рот приоткрыт     C — глаза открыты, рот открыт
    D — глаза закрыты, рот закрыт        E, F — глаза закрыты, рот как у B, C

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

Нарезка по сетке — по мотивам tomari-guruguru (tools/extract_sprites.py,
© 2026 rotejin, лицензия MIT): https://github.com/rotejin/tomari-guruguru

Хранение — data/api_<персона>/portrait/: portrait.json (сетка, настройки
листов), <лист>.webp (атлас), <лист>.src (исходный лист — чтобы
переразметить сетку без повторной загрузки).
"""

from __future__ import annotations

import base64
import io
import json
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from app.api.room_api import RoomError, validate_data_url
from app.api.security import safe_join
from app.core.atomic_io import atomic_write_json
from app.core.paths import data_dir

SHEETS = ("A", "B", "C", "D", "E", "F")
SHEET_MAX_BYTES = 10 * 1024 * 1024
MAX_GRID = 10
CELL_PX = 256
BG_MODES = ("auto", "keep")
DEFAULT_TOLERANCE = 0  # 0 — по шуму фона листа
WINDOW = 0.3  # запас окна поиска фигуры — доля клетки с каждой стороны
FIT = 0.92  # фигура в квадрате атласа — чуть мельче клетки: ракурсы бывают шире

_lock = threading.Lock()


class PortraitError(RoomError):
    pass


def portrait_dir(persona: str) -> Path:
    base = safe_join(data_dir(), persona, prefix="api_")
    if base is None:
        raise PortraitError(400, "Недопустимый id персоны")
    return base / "portrait"


def _meta(persona: str) -> dict:
    try:
        data = json.loads((portrait_dir(persona) / "portrait.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {"rows": 5, "cols": 5, "sheets": {}}
    sheets = data.get("sheets") if isinstance(data.get("sheets"), dict) else {}
    return {"rows": int(data.get("rows") or 5), "cols": int(data.get("cols") or 5),
            "sheets": {k: v for k, v in sheets.items() if k in SHEETS and isinstance(v, dict)},
            "updated_at": data.get("updated_at")}


def get_portrait(persona: str) -> dict:
    """{rows, cols, cell, sheets: {лист: data-URL атласа}, settings: {лист:
    {offset_x, offset_y, bg, tolerance}}, updated_at}. Портрета нет — sheets {}."""
    meta = _meta(persona)
    d = portrait_dir(persona)
    sheets, settings = {}, {}
    for key, s in meta["sheets"].items():
        path = d / f"{key}.webp"
        if not path.is_file():
            continue
        sheets[key] = "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode()
        settings[key] = {k: s.get(k) for k in ("offset_x", "offset_y", "bg", "tolerance")}
    return {"rows": meta["rows"], "cols": meta["cols"], "cell": CELL_PX,
            "sheets": sheets, "settings": settings, "updated_at": meta.get("updated_at")}


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
              tolerance: int = DEFAULT_TOLERANCE) -> dict:
    """Загрузить лист (data_url) или переразметить загруженный (data_url —
    None). Сетка общая на портрет: другая — остальные листы режутся заново
    с ней же, со своими смещениями."""
    if key not in SHEETS:
        raise PortraitError(400, f"Лист — одна из букв {', '.join(SHEETS)}")
    if not (1 <= rows <= MAX_GRID and 1 <= cols <= MAX_GRID):
        raise PortraitError(400, f"Сетка — от 1×1 до {MAX_GRID}×{MAX_GRID}")
    if bg not in BG_MODES:
        raise PortraitError(400, "Фон — auto или keep")
    tolerance = max(0, min(120, int(tolerance)))
    d = portrait_dir(persona)
    with _lock:
        meta = _meta(persona)
        src = d / f"{key}.src"
        if data_url is not None:
            url = validate_data_url(data_url, SHEET_MAX_BYTES, "Лист портрета")
            raw = base64.b64decode(url.split(",", 1)[1])
        elif src.is_file():
            raw = src.read_bytes()
        else:
            raise PortraitError(400, "Лист ещё не загружен — нужна картинка")
        settings = {"offset_x": int(offset_x), "offset_y": int(offset_y), "bg": bg, "tolerance": tolerance}
        atlas = build_atlas(raw, rows, cols, **settings)
        d.mkdir(parents=True, exist_ok=True)
        if data_url is not None:
            src.write_bytes(raw)
        _save_atlas(atlas, d / f"{key}.webp")
        # Другая сетка — остальные листы под неё же
        if (rows, cols) != (meta["rows"], meta["cols"]):
            for other, s in meta["sheets"].items():
                if other == key or not (d / f"{other}.src").is_file():
                    continue
                _save_atlas(build_atlas((d / f"{other}.src").read_bytes(), rows, cols,
                                        int(s.get("offset_x") or 0), int(s.get("offset_y") or 0),
                                        s.get("bg") if s.get("bg") in BG_MODES else "auto",
                                        int(s.get("tolerance") or DEFAULT_TOLERANCE)),
                            d / f"{other}.webp")
        meta["sheets"][key] = settings
        _write_meta(persona, rows, cols, meta["sheets"])
    return get_portrait(persona)


def delete_sheet(persona: str, key: str) -> dict:
    if key not in SHEETS:
        raise PortraitError(400, f"Лист — одна из букв {', '.join(SHEETS)}")
    d = portrait_dir(persona)
    with _lock:
        meta = _meta(persona)
        meta["sheets"].pop(key, None)
        for ext in ("webp", "src"):
            (d / f"{key}.{ext}").unlink(missing_ok=True)
        if d.is_dir():
            _write_meta(persona, meta["rows"], meta["cols"], meta["sheets"])
    return get_portrait(persona)


def delete_all(persona: str) -> dict:
    d = portrait_dir(persona)
    with _lock:
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    f.unlink()
    return get_portrait(persona)


def _write_meta(persona: str, rows: int, cols: int, sheets: dict) -> None:
    atomic_write_json(portrait_dir(persona) / "portrait.json",
                      {"rows": rows, "cols": cols, "sheets": sheets, "updated_at": time.time()})


def _save_atlas(atlas: Image.Image, path: Path) -> None:
    buf = io.BytesIO()
    atlas.save(buf, "WEBP", quality=90, method=4)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(path)


# ── Нарезка ────────────────────────────────────────────────────────────

def build_atlas(raw: bytes, rows: int, cols: int, offset_x: int = 0, offset_y: int = 0,
                bg: str = "auto", tolerance: int = DEFAULT_TOLERANCE) -> Image.Image:
    """Лист → атлас cols×rows клеток CELL_PX×CELL_PX с прозрачным фоном."""
    try:
        sheet = Image.open(io.BytesIO(raw))
        sheet.load()
    except Exception:
        raise PortraitError(400, "Не удалось прочитать картинку листа")
    sheet = sheet.convert("RGBA")
    w, h = sheet.size
    cw = (w - 2 * offset_x) / cols
    ch = (h - 2 * offset_y) / rows
    if cw < 8 or ch < 8:
        raise PortraitError(400, "Сетка или поля не помещаются в лист")
    scale = CELL_PX / max(cw, ch)
    atlas = Image.new("RGBA", (cols * CELL_PX, rows * CELL_PX), (0, 0, 0, 0))
    px = np.asarray(sheet)
    keep_alpha = bg == "keep" or _has_transparency(px)
    if not keep_alpha:
        bg_color, noise = _sheet_background(px)
        tol = tolerance or int(max(10, min(40, round(noise * 4 + 8))))
    mx, my = round(cw * WINDOW), round(ch * WINDOW)
    for r in range(rows):
        for c in range(cols):
            x0, y0 = round(offset_x + c * cw), round(offset_y + r * ch)
            x1, y1 = round(offset_x + (c + 1) * cw), round(offset_y + (r + 1) * ch)
            if bg == "keep":
                cell = px[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
                if cell.size:
                    img = Image.fromarray(cell, "RGBA")
                    tw, th = max(1, round(img.width * scale)), max(1, round(img.height * scale))
                    _place(atlas, img.resize((tw, th), Image.LANCZOS), r, c,
                           (CELL_PX - tw) // 2, (CELL_PX - th) // 2)
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
            _place(atlas, fig.resize((tw, th), Image.LANCZOS), r, c, (CELL_PX - tw) // 2, CELL_PX - th)
    return atlas


def _place(atlas: Image.Image, img: Image.Image, r: int, c: int, x: int, y: int) -> None:
    """Картинка в клетку (r, c) атласа со сдвигом (x, y) от её угла; что не
    влезло — обрезается, в соседние клетки не залезает."""
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(img.width, CELL_PX - x), min(img.height, CELL_PX - y)
    if right > left and bottom > top:
        atlas.alpha_composite(img.crop((left, top, right, bottom)),
                              (c * CELL_PX + x + left, r * CELL_PX + y + top))


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

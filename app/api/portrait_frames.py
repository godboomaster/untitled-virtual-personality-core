"""Редактор кадров (вкладка «Кадры» раздела «Аватар»): слои из нарезанных
листов накладываются друг на друга, чтобы кадры совпадали. Источник — лист
портрета (kind "sheet", id — буква) или анимация (kind "anim", id — её id);
здесь у них одно лицо: нарезка, правки, атлас без правок и исходник. У
анимации ещё порядок кадров (order): кадры удаляют, переставляют и добавляют с
листа (клетка может идти не раз), а атлас без правок — клетки по листу,
порядок редактор кладёт сам.

Редактор рисует кадры сам: берёт атлас без правок (raw) и кладёт поверх
fix той же формулой, что portrait_api.compose. Поэтому отсюда идёт
<id>.raw.webp, а не атлас, который играют клиенты: иначе сдвиг посчитался
бы дважды. И размер кадра здесь — кадра нарезки, по атласу без правок: у
анимации с правками записан уже выросший кадр (portrait_api.grown_frame). Пробная нарезка (preview) ничего не пишет на диск, сохраняет
только save — у портрета тем же put_sheet, у анимации — put_cut.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

from app.api import portrait_anim, portrait_api
from app.api.portrait_api import PortraitError

KINDS = ("sheet", "anim")
_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def _base(persona: str, kind: str, item: str) -> Path:
    """Путь без расширения: base + ".src", ".webp", ".raw.webp"."""
    if kind == "sheet":
        if not portrait_api.known_sheet(persona, item):
            raise PortraitError(404, "Лист не найден")
        return portrait_api.portrait_dir(persona) / item
    if kind == "anim":
        if portrait_anim.find(persona, item) is None:
            raise PortraitError(404, "Анимация не найдена")
        return portrait_anim.anims_dir(persona) / item
    raise PortraitError(404, "Источник кадров — лист или анимация")


def _ext(base: Path, ext: str) -> Path:
    return base.with_name(f"{base.name}.{ext}")


def _sheet_size(base: Path) -> tuple[int, int]:
    """Размер исходника; его нет (старая запись) — 0×0: нарезку не поправить."""
    try:
        with Image.open(_ext(base, "src")) as im:
            return im.size
    except (OSError, ValueError):
        return 0, 0


def _url(path: Path) -> str:
    return "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode()


def describe(persona: str, kind: str, item: str) -> dict:
    """Всё, что нужно редактору об источнике: сетка и размер кадра, нарезка
    (линии — всегда готовым списком), правки, атлас без правок."""
    base = _base(persona, kind, item)
    plain = _ext(base, "raw.webp")
    atlas = plain if plain.is_file() else _ext(base, "webp")
    if not atlas.is_file():
        raise PortraitError(404, "Лист не найден")
    w, h = _sheet_size(base)
    if kind == "sheet":
        meta = portrait_api._meta(persona)
        s = meta["sheets"].get(item)
        if s is None:
            raise PortraitError(404, "Лист не найден")
        rows, cols = meta["rows"], meta["cols"]
        out = {"name": item, "fps": 8, "count": rows * cols, "frame_max": portrait_api.CELL_PX,
               "max_grid": portrait_api.MAX_GRID, "updated_at": meta.get("updated_at")}
    else:
        s = portrait_anim.find(persona, item)
        rows, cols = s["rows"], s["cols"]
        out = {"name": s["name"], "fps": s["fps"], "count": s["frames"], "frame_max": portrait_anim.FRAME_MAX,
               "max_grid": portrait_anim.MAX_GRID, "updated_at": s.get("updated_at"),
               "saved_w": s["fw"], "saved_h": s["fh"], "order": s["order"]}
    with Image.open(atlas) as im:
        out.update(fw=im.width // cols, fh=im.height // rows)
    off_x, off_y = int(s.get("offset_x") or 0), int(s.get("offset_y") or 0)
    if w and h:
        xs, ys = portrait_api.grid_lines(w, h, rows, cols, off_x, off_y, s.get("xs"), s.get("ys"))
        xs, ys = [round(v) for v in xs], [round(v) for v in ys]
    else:
        xs, ys = None, None
    cut = {"rows": rows, "cols": cols, "offset_x": off_x, "offset_y": off_y,
           "bg": s.get("bg") or "auto", "tolerance": int(s.get("tolerance") or 0),
           "xs": xs, "ys": ys}
    if kind == "anim":
        cut["align"] = s["align"]
    timing = {k: s[k] for k in ("fps", "loops", "loop", "holds", "frames")} if kind == "anim" else None
    slots = portrait_anim.slots_of(s) if kind == "anim" else rows * cols
    return {"order": None, **out, "kind": kind, "id": item, "rows": rows, "cols": cols, "cut": cut,
            "timing": timing, "fix": portrait_api.clean_fix(s.get("fix"), slots),
            "sheet_w": w, "sheet_h": h, "atlas": _url(atlas)}


def sheet_bytes(persona: str, kind: str, item: str) -> tuple[bytes, str]:
    """Исходный лист как есть: (байты, MIME). Нарезка рисуется поверх него."""
    path = _ext(_base(persona, kind, item), "src")
    if not path.is_file():
        raise PortraitError(404, "Исходного листа нет — загрузите его заново")
    raw = path.read_bytes()
    try:
        with Image.open(io.BytesIO(raw)) as im:
            mime = _MIME.get(im.format or "", "application/octet-stream")
    except (OSError, ValueError):
        raise PortraitError(400, "Не удалось прочитать картинку листа")
    return raw, mime


def _check_cut(cut: dict, limit: int) -> tuple[int, int, str]:
    rows, cols = cut.get("rows"), cut.get("cols")
    if not (isinstance(rows, int) and isinstance(cols, int) and 1 <= rows <= limit and 1 <= cols <= limit):
        raise PortraitError(400, f"Сетка — от 1×1 до {limit}×{limit}")
    bg = cut.get("bg", "auto")
    if bg not in portrait_api.BG_MODES:
        raise PortraitError(400, "Фон — auto или keep")
    return rows, cols, bg


def preview(persona: str, kind: str, item: str, cut: dict) -> dict:
    """Пробная нарезка: атлас без правок по этим настройкам, на диск не идёт."""
    path = _ext(_base(persona, kind, item), "src")
    if not path.is_file():
        raise PortraitError(400, "Исходного листа нет — загрузите его заново")
    raw = path.read_bytes()
    limit = portrait_api.MAX_GRID if kind == "sheet" else portrait_anim.MAX_GRID
    rows, cols, bg = _check_cut(cut, limit)
    off_x, off_y = int(cut.get("offset_x") or 0), int(cut.get("offset_y") or 0)
    tol = max(0, min(120, int(cut.get("tolerance") or 0)))
    xs, ys = cut.get("xs"), cut.get("ys")
    if kind == "sheet":
        cells = portrait_api.cut_atlas(raw, rows, cols, off_x, off_y, bg, tol, xs, ys)
        fw = fh = portrait_api.CELL_PX
        count = rows * cols
    else:
        align = cut.get("align") or "grid"
        if align not in portrait_anim.ALIGNS:
            raise PortraitError(400, "Выравнивание — grid или feet")
        cells, fw, fh, count = portrait_anim.cut_frames(raw, rows, cols, off_x, off_y, bg, tol, align, xs, ys)
    atlas = portrait_api.compose(cells, rows, cols, fw, fh)
    buf = io.BytesIO()
    atlas.save(buf, "WEBP", quality=90, method=2)
    return {"rows": rows, "cols": cols, "fw": fw, "fh": fh, "count": max(1, count),
            "atlas": "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()}


KEEP = portrait_anim.KEEP


def save(persona: str, kind: str, item: str, cut: dict, fix: dict | None, timing: dict | None = None,
         order=KEEP) -> dict:
    """Сохранить нарезку и правки: атлас для всех клиентов режется заново.
    timing — время кадров анимации, order — её порядок кадров (KEEP — как
    был); у листа портрета их нет: место кадра там — ракурс головы."""
    _base(persona, kind, item)
    limit = portrait_api.MAX_GRID if kind == "sheet" else portrait_anim.MAX_GRID
    rows, cols, bg = _check_cut(cut, limit)
    # Первая линия — и есть поле: карточки «Портрет» и «Анимации» показывают его
    xs, ys = cut.get("xs"), cut.get("ys")
    cut = {**cut, "offset_x": xs[0] if xs else int(cut.get("offset_x") or 0),
           "offset_y": ys[0] if ys else int(cut.get("offset_y") or 0)}
    if kind == "sheet":
        portrait_api.put_sheet(persona, item, None, rows, cols, cut["offset_x"], cut["offset_y"], bg,
                               int(cut.get("tolerance") or 0), xs=xs, ys=ys,
                               fix=fix if fix is not None else {})
    else:
        portrait_anim.put_cut(persona, item, cut, fix if fix is not None else {}, timing, order)
    return describe(persona, kind, item)

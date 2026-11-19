"""Редактор кадров: линии сетки, правки кадров (сдвиг, масштаб, поворот),
порядок кадров анимации, атлас без правок для редактора, что правки
переживают и что их сбрасывает. Папка
данных — временная, ядро и модели не нужны.

Запуск: python -m scripts.test_portrait_frames
"""

import base64
import io
import os
import sys
import tempfile

tmp = tempfile.mkdtemp(prefix="vpc-frames-")
os.environ["VPC_DATA_DIR"] = tmp
os.environ["DATA_DIR"] = tmp

import numpy as np
from PIL import Image, ImageDraw

from app.api import portrait_anim as A
from app.api import portrait_api as P
from app.api import portrait_frames as F
from app.api.portrait_api import PortraitError

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"[OK] {name}")
    else:
        fail += 1
        print(f"[FAIL] {name} {detail}")


def raises(fn, status=None):
    try:
        fn()
    except PortraitError as e:
        return status is None or e.status == status
    return False


def sheet_png(rows, cols, cell=200, bg=(120, 120, 120), jitter=None):
    """Лист: цветной круг на сером фоне в каждой клетке."""
    img = Image.new("RGB", (cols * cell, rows * cell), bg)
    d = ImageDraw.Draw(img)
    for r in range(rows):
        for c in range(cols):
            jx, jy = (jitter or {}).get(r * cols + c, (0, 0))
            x, y = c * cell + cell // 2 + jx, r * cell + cell // 2 + jy
            d.ellipse((x - 50, y - 70, x + 50, y + 70), fill=(220, 40 + 30 * c, 60 + 40 * r))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def data_url(raw):
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def atlas_of(url):
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGBA")


def cell_of(atlas, r, c, fw, fh):
    return np.asarray(atlas.crop((c * fw, r * fh, (c + 1) * fw, (r + 1) * fh)))


def box(cell):
    ys, xs = np.nonzero(cell[:, :, 3] > 128)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


# ── Правки: пределы ──────────────────────────────────────────────────
check("clean_fix: мусор — пусто", P.clean_fix("x", 4) == {} and P.clean_fix(None, 4) == {})
check("clean_fix: нулевая правка не хранится",
      P.clean_fix({"all": {"dx": 0, "dy": 0, "s": 1}, "cells": {"1": {"dx": 0}}}, 4) == {})
fx = P.clean_fix({"all": {"dx": 3.4, "dy": "x"}, "cells": {"1": {"dx": 9999, "s": 100}, "7": {"dx": 1},
                                                          "a": {"dx": 1}, "2": 5}}, 4)
check("clean_fix: прижимает к пределам", fx["cells"]["1"]["dx"] == P.FIX_SHIFT_MAX and fx["cells"]["1"]["s"] == P.FIX_SCALE_MAX)
check("clean_fix: кадры вне сетки и не числа — долой", set(fx["cells"]) == {"1"})
check("clean_fix: битое поле всей правки — вся правка долой", "all" not in fx)
check("fix_of: лист + кадр", P.fix_of({"all": {"dx": 2, "dy": 1, "s": 2}, "cells": {"3": {"dx": 5, "dy": 0, "s": 0.5}}}, 3)[:3] == (7, 1, 1.0))
fx = P.clean_fix({"all": {"dx": 1, "r": 30, "px": 5}, "cells": {"0": {"r": 190.04, "px": 3000, "py": -12.345},
                                                                "1": {"r": -0.0}, "2": {"px": 4}}}, 4)
check("поворот: только у кадра", fx["all"] == {"dx": 1, "dy": 0, "s": 1}, str(fx))
check("поворот: угол в −180..180, центр в пределах", fx["cells"]["0"] == {"dx": 0, "dy": 0, "s": 1, "r": -170.0,
                                                                          "px": P.FIX_PIVOT_MAX, "py": -12.3}, str(fx))
check("поворот: нулевой не хранится, центр без поворота — хранится",
      "1" not in fx["cells"] and fx["cells"]["2"] == {"dx": 0, "dy": 0, "s": 1, "px": 4.0, "py": 0.0}, str(fx))
check("поворот: битое число — правка долой", P.clean_fix({"cells": {"0": {"r": "x"}}}, 4) == {})
check("fix_of: поворот и центр кадра", P.fix_of({"all": {"dx": 2, "dy": 0, "s": 1}, "cells": {"1": {"r": 45, "px": 3, "py": -9}}}, 1)
      == (2, 0, 1.0, 45.0, 3.0, -9.0))
check("whole_fix: кадры долой", P.whole_fix({"all": {"dx": 1}, "cells": {"1": {"dx": 2}}}) == {"all": {"dx": 1}})

# ── Линии сетки ──────────────────────────────────────────────────────
xs, ys = P.grid_lines(1000, 500, 2, 4, 10, 20)
check("ровные линии с полями", xs == [10, 255, 500, 745, 990] and ys == [20, 250, 480], f"{xs} {ys}")
check("свои линии остаются", P.grid_lines(100, 100, 1, 2, 0, 0, [0, 30, 100], [0, 100])[0] == [0, 30, 100])
check("линий не столько", raises(lambda: P.grid_lines(100, 100, 1, 2, 0, 0, [0, 100], [0, 100]), 400))
check("линия за листом", raises(lambda: P.grid_lines(100, 100, 1, 2, 0, 0, [0, 50, 120], [0, 100]), 400))
check("линии слиплись", raises(lambda: P.grid_lines(100, 100, 1, 2, 0, 0, [0, 3, 100], [0, 100]), 400))
check("линии не по порядку", raises(lambda: P.grid_lines(100, 100, 1, 2, 0, 0, [0, 80, 60], [0, 100]), 400))

# Фигура пересекает ровную линию: своя линия спасает
raw = sheet_png(1, 2, jitter={0: (60, 0)})
atlas, fw, fh, _ = A.build_frames(raw, 1, 2, bg="auto", align="feet")
a0 = cell_of(atlas, 0, 0, fw, fh)
atlas2, *_ = A.build_frames(raw, 1, 2, bg="auto", align="feet", xs=[0, 330, 400], ys=[0, 200])
check("своя линия: фигура больше не разрезана", (a0[:, :, 3] > 128).sum() < (cell_of(atlas2, 0, 0, fw, fh)[:, :, 3] > 128).sum())

# ── compose: та же формула, что у редактора ──────────────────────────
raw = sheet_png(2, 2)
cells, fw, fh, filled = A.cut_frames(raw, 2, 2)
plain = P.compose(cells, 2, 2, fw, fh)
moved = P.compose(cells, 2, 2, fw, fh, {"cells": {"1": {"dx": 7, "dy": -6, "s": 1}}})
b0, b1 = box(cell_of(plain, 0, 1, fw, fh)), box(cell_of(moved, 0, 1, fw, fh))
check("сдвиг кадра: фигура на (7, −6)", (b1[0] - b0[0], b1[1] - b0[1]) == (7, -6), f"{b0} {b1}")
check("сдвиг кадра: соседний кадр не тронут",
      np.array_equal(cell_of(plain, 0, 0, fw, fh), cell_of(moved, 0, 0, fw, fh)))
big = P.compose(cells, 2, 2, fw, fh, {"cells": {"0": {"dx": 0, "dy": 0, "s": 0.5}}})
b0, b1 = box(cell_of(plain, 0, 0, fw, fh)), box(cell_of(big, 0, 0, fw, fh))
check("масштаб вокруг низа-середины: низ на месте", abs(b1[3] - b0[3]) <= 1 or b0[3] < fh, f"{b0} {b1}")
check("масштаб 0,5: фигура вдвое меньше", abs((b1[2] - b1[0]) * 2 - (b0[2] - b0[0])) <= 2, f"{b0} {b1}")
want_y = fh + (b0[1] - fh) * 0.5
check("масштаб: верх фигуры по формуле", abs(b1[1] - want_y) <= 1.5, f"{b1[1]} {want_y}")
out = P.compose(cells, 2, 2, fw, fh, {"cells": {"0": {"dx": 400, "dy": 0, "s": 1}}})
check("вынесли за кадр — обрезано, соседний чист",
      not cell_of(out, 0, 0, fw, fh)[:, :, 3].any() and np.array_equal(cell_of(plain, 0, 1, fw, fh), cell_of(out, 0, 1, fw, fh)))
check("build_frames без правки = compose без правки",
      np.array_equal(np.asarray(A.build_frames(raw, 2, 2)[0]), np.asarray(plain)))

# Поворот вокруг центра кадра: высокий овал ложится набок, центр на месте
b0 = box(cell_of(plain, 0, 0, fw, fh))
mid = ((b0[0] + b0[2]) / 2 - fw / 2, (b0[1] + b0[3]) / 2 - fh)
turned = P.compose(cells, 2, 2, fw, fh, {"cells": {"0": {"r": 90, "px": mid[0], "py": mid[1]}}})
b1 = box(cell_of(turned, 0, 0, fw, fh))
check("поворот 90°: ширина ↔ высота", abs((b1[2] - b1[0]) - (b0[3] - b0[1])) <= 3 and abs((b1[3] - b1[1]) - (b0[2] - b0[0])) <= 3, f"{b0} {b1}")
check("поворот 90°: центр фигуры на месте",
      abs((b1[0] + b1[2]) / 2 - (b0[0] + b0[2]) / 2) <= 1.5 and abs((b1[1] + b1[3]) / 2 - (b0[1] + b0[3]) / 2) <= 1.5, f"{b0} {b1}")
check("поворот: соседний кадр не тронут", np.array_equal(cell_of(plain, 0, 1, fw, fh), cell_of(turned, 0, 1, fw, fh)))
# По часовой стрелке: метка справа от центра уходит вниз
one = [(0, Image.new("RGBA", (20, 20), (0, 0, 0, 0)), 20, 20, 100, 100)]
ImageDraw.Draw(one[0][1]).rectangle((14, 8, 19, 11), fill=(255, 0, 0, 255))
cw_ = P.compose(one, 1, 1, 200, 200, {"cells": {"0": {"r": 90, "px": 10, "py": -90}}})
mark = box(np.asarray(cw_))
check("поворот +90° — по часовой (y вниз)", mark[1] >= 113 and abs((mark[0] + mark[2]) / 2 - 110) <= 2, str(mark))
# Центр поворота — по низу-середине, едет со сдвигом: тот же поворот со сдвигом = сдвиг повёрнутого
shifted = P.compose(cells, 2, 2, fw, fh, {"cells": {"0": {"dx": 6, "dy": -4, "r": 90, "px": mid[0], "py": mid[1]}}})
b2 = box(cell_of(shifted, 0, 0, fw, fh))
check("поворот + сдвиг: центр едет со сдвигом", (b2[0] - b1[0], b2[1] - b1[1]) == (6, -4), f"{b1} {b2}")
# Повернули около ступней — кадр анимации растёт вбок
tall = [(0, Image.new("RGBA", (40, 200), (200, 50, 50, 255)), 40, 200, 108, 56)]
gw, gh, sink, ax = P.grown_frame(tall, 256, 256, {"cells": {"0": {"r": 90}}}, 512)
# Столбик 40 px толщиной лёг набок вокруг середины пола: половина толщины — под полом
check("поворот набок у пола: кадр шире и ниже пола на полтолщины",
      gw >= 256 + 2 * 60 and sink == 20 and gh == 256 + 20, f"{gw} {gh} {sink}")
gw2, gh2, sink2, _ = P.grown_frame(tall, 256, 256, {"cells": {"0": {"r": 10, "px": 0, "py": -100}}}, 512)
check("лёгкий наклон вокруг середины: вбок и вверх не растёт, вниз — на опустившийся угол",
      gw2 == 256 and 0 < sink2 <= 4 and gh2 == 256 + sink2, f"{gw2} {gh2} {sink2}")
grown = P.compose(tall, 1, 1, 256, 256, {"cells": {"0": {"r": 90}}}, (gw, gh, sink, ax))
g = box(np.asarray(grown))
check("выросший кадр: повёрнутая фигура целиком, и под полом тоже",
      g[2] - g[0] >= 198 and g[0] >= 0 and g[2] <= gw and g[3] - g[1] >= 39 and g[3] <= gh, f"{g} {gw} {gh}")
check("пол на месте: верх лёгшей фигуры — на полтолщины выше пола", abs(g[1] - (256 - 20)) <= 1, str(g))
# Сдвинули вниз без поворота — кадр растёт вниз на сдвиг
gw3, gh3, sink3, _ = P.grown_frame(tall, 256, 256, {"cells": {"0": {"dy": 30}}}, 512)
check("сдвиг вниз на 30: sink 30", (gw3, gh3, sink3) == (256, 286, 30), f"{gw3} {gh3} {sink3}")
check("без правок — как было, sink 0, пол посередине", P.grown_frame(tall, 256, 256, None, 512) == (256, 256, 0, 128.0))
check("предел: вниз не больше, чем осталось до limit",
      P.grown_frame(tall, 256, 256, {"cells": {"0": {"dy": 400}}}, 300)[1:3] == (300, 44))
# Своя рамка: какая задана, такая и есть, фигура относительно пола не двигается
fx_box = P.clean_fix({"box": {"l": 50.4, "r": "200", "t": 300, "b": -5}, "cells": {"0": {"r": 90}}}, 1)
check("рамка: числа целые, низ не меньше 0", fx_box["box"] == {"l": 50, "r": 200, "t": 300, "b": 0}, str(fx_box))
check("рамка: битая — не хранится", "box" not in P.clean_fix({"box": {"l": 1, "r": 2}}, 1)
      and "box" not in P.clean_fix({"box": {"l": "x", "r": 1, "t": 1, "b": 0}}, 1))
check("правка сетки другой — рамка уходит вместе с кадрами", P.whole_fix({"all": {"dx": 1}, "box": fx_box["box"]}) == {"all": {"dx": 1}})
bw, bh, bs, bax = P.grown_frame(tall, 256, 256, {"box": {"l": 60, "r": 200, "t": 220, "b": 40}}, 512)
check("рамка → кадр 260×260, пол на 40 выше низа, середина пола в 60 от левого края", (bw, bh, bs, bax) == (260, 260, 40, 60.0))
check("рамка больше предела — ужата с сохранением долей",
      P.grown_frame(tall, 256, 256, {"box": {"l": 300, "r": 300, "t": 500, "b": 100}}, 512) == (512, 512, 85, 256.0))
plain_tall = box(np.asarray(P.compose(tall, 1, 1, 256, 256)))
boxed = box(np.asarray(P.compose(tall, 1, 1, 256, 256, None, (bw, bh, bs, bax))))
# Столбик стоял у середины пола (x 108..148 при середине 128): теперь середина пола — 60
check("в своей рамке фигура на том же месте от середины пола",
      boxed[0] - 60 == plain_tall[0] - 128 and boxed[3] == bh - bs, f"{plain_tall} {boxed}")
# Низ экрана (fix.floor): линия на столько px выше низа фигуры стоит на полу,
# что ниже — под полом; кадр той же высоты, фигура не мельчает
check("низ экрана: целое 0..BOX_MAX, 0 и мусор не хранятся",
      P.clean_fix({"floor": 99.6}, 1) == {"floor": 100} and P.clean_fix({"floor": -5}, 1) == {}
      and P.clean_fix({"floor": "x"}, 1) == {} and P.clean_fix({"floor": True}, 1) == {}
      and P.clean_fix({"floor": 5000}, 1)["floor"] == P.BOX_MAX)
check("другая сетка — низ экрана уходит с кадрами", P.whole_fix({"floor": 80, "all": {"dx": 1}}) == {"all": {"dx": 1}})
fw_, fh_, fs_, fax_ = P.grown_frame(tall, 256, 256, {"floor": 100}, 512)
check("низ экрана 100: кадр 256×256, пол на 100 выше низа (sink), середина та же",
      (fw_, fh_, fs_, fax_) == (256, 256, 100, 128.0), f"{fw_} {fh_} {fs_} {fax_}")
floored = box(np.asarray(P.compose(tall, 1, 1, 256, 256, {"floor": 100}, (fw_, fh_, fs_, fax_))))
check("фигура не сдвинута в кадре и того же размера: ниже пола — 100 px",
      floored == plain_tall and (fh_ - fs_) - floored[3] == -100, f"{plain_tall} {floored}")
gw4, gh4, sink4, _ = P.grown_frame(tall, 256, 256, {"floor": 100, "cells": {"0": {"dy": -60}}}, 512)
check("низ экрана + фигура поднята на 60: кадр как без линии (вырос на 4 вверх), пол на 100 выше низа",
      (gw4, gh4, sink4) == (256, 260, 100) and P.grown_frame(tall, 256, 256, {"cells": {"0": {"dy": -60}}}, 512)[1] == 260, f"{gw4} {gh4} {sink4}")
check("низ экрана со своей рамкой: рамка — от линии", P.grown_frame(tall, 256, 256, {"floor": 100, "box": {"l": 60, "r": 200, "t": 220, "b": 40}}, 512)
      == (260, 260, 40, 60.0))
fb = box(np.asarray(P.compose(tall, 1, 1, 256, 256, {"floor": 100, "box": {"l": 60, "r": 200, "t": 220, "b": 40}}, (260, 260, 40, 60.0))))
# Другой край экрана (fix.edge): сбоку и сверху — линия от края кадра нарезки
check("край экрана: только left/right/top, at целое ≥ 0",
      P.clean_fix({"edge": {"side": "left", "at": 40.4}}, 1) == {"edge": {"side": "left", "at": 40}}
      and P.clean_fix({"edge": {"side": "bottom", "at": 3}}, 1) == {}
      and P.clean_fix({"edge": {"side": "top", "at": -9}}, 1) == {"edge": {"side": "top", "at": 0}}
      and P.clean_fix({"edge": {"side": "left"}}, 1) == {})
check("другая сетка — край уходит с кадрами", P.whole_fix({"edge": {"side": "left", "at": 4}}) == {})
check("линия в кадре атласа: без роста — как в кадре нарезки",
      [P.edge_in_atlas({"edge": {"side": k, "at": 40}}, 256, 256, (256, 256, 0, 128.0)) for k in ("left", "right", "top")]
      == [{"side": "left", "at": 40.0}, {"side": "right", "at": 216.0}, {"side": "top", "at": 40.0}])
check("кадр вырос (на 30 влево-вправо, на 20 вверх): линия едет с кадром нарезки",
      [P.edge_in_atlas({"edge": {"side": k, "at": 40}}, 256, 256, (316, 276, 0, 158.0))["at"] for k in ("left", "right", "top")]
      == [70.0, 246.0, 60.0])
check("без края — None", P.edge_in_atlas({}, 256, 256, (256, 256, 0, 128.0)) is None)
# Столбик 200 px: от 100 над линией до 100 под ней; в рамке под линией — 40
check("в своей рамке с низом экрана: над линией 100 px фигуры, под ней видно 40",
      (fb[1], fb[3]) == (220 - 100, 260), str(fb))

# ── Лист портрета через редактор ─────────────────────────────────────
persona = "frames_t"
raw_a = sheet_png(2, 2, cell=240)
P.put_sheet(persona, "A", data_url(raw_a), 2, 2)
P.put_sheet(persona, "B", data_url(sheet_png(2, 2, cell=240, jitter={1: (8, 0)})), 2, 2)
d = F.describe(persona, "sheet", "A")
check("describe: сетка и размер кадра", (d["rows"], d["cols"], d["fw"], d["fh"], d["count"]) == (2, 2, 256, 256, 4))
check("describe: линии готовым списком", d["cut"]["xs"] == [0, 240, 480] and d["cut"]["ys"] == [0, 240, 480], str(d["cut"]))
check("describe: размер исходника", (d["sheet_w"], d["sheet_h"]) == (480, 480))
check("describe: правок нет", d["fix"] == {})
base = P.portrait_dir(persona)
raw_before = (base / "A.webp").read_bytes()
s = F.save(persona, "sheet", "A", d["cut"], {"cells": {"2": {"dx": 5, "dy": 3, "s": 1}}})
check("save: правка в ответе", s["fix"] == {"cells": {"2": {"dx": 5, "dy": 3, "s": 1}}})
check("save: появился атлас без правок", (base / "A.raw.webp").is_file())
check("save: атлас клиентов сменился", (base / "A.webp").read_bytes() != raw_before)
raw_atlas, played = atlas_of(s["atlas"]), Image.open(base / "A.webp").convert("RGBA")
b0, b1 = box(cell_of(raw_atlas, 1, 0, 256, 256)), box(cell_of(played, 1, 0, 256, 256))
check("редактор получает кадры без правки, клиенты — с ней", (b1[0] - b0[0], b1[1] - b0[1]) == (5, 3), f"{b0} {b1}")
check("сравнение с тем, что было до правки: кадр 3 не менялся",
      np.abs(cell_of(raw_atlas, 1, 1, 256, 256).astype(int) - cell_of(played, 1, 1, 256, 256).astype(int)).mean() < 1.5)
pub = P.get_portrait(persona)
check("GET /portrait не несёт правок", "fix" not in pub["settings"]["A"] and "xs" not in pub["settings"]["A"])

# Переразметка карточкой «Портрет» (те же поля) правки не трогает
P.put_sheet(persona, "A", None, 2, 2, 0, 0, "auto", 0)
check("переразметка тем же — правки на месте", F.describe(persona, "sheet", "A")["fix"].get("cells", {}).get("2", {}).get("dx") == 5)
# Свои линии: другие поля из карточки их снимают
cut = dict(F.describe(persona, "sheet", "A")["cut"])
cut["xs"] = [6, 250, 476]
s = F.save(persona, "sheet", "A", cut, s["fix"])
check("линии сохранились", s["cut"]["xs"] == [6, 250, 476])
check("первая линия — поле", s["cut"]["offset_x"] == 6)
P.put_sheet(persona, "A", None, 2, 2, 6, 0, "auto", 0)
check("те же поля — линии на месте", F.describe(persona, "sheet", "A")["cut"]["xs"] == [6, 250, 476])
P.put_sheet(persona, "A", None, 2, 2, 12, 0, "auto", 0)
check("другие поля — линии ровные", F.describe(persona, "sheet", "A")["cut"]["xs"] == [12, 240, 468])
# Другая сетка общая: номера кадров не подходят, правка листа остаётся
F.save(persona, "sheet", "B", F.describe(persona, "sheet", "B")["cut"],
       {"all": {"dx": 2, "dy": 0, "s": 1}, "cells": {"1": {"dx": 4, "dy": 0, "s": 1}}})
P.put_sheet(persona, "A", None, 1, 2, 0, 0, "auto", 0)
b = F.describe(persona, "sheet", "B")
check("другая сетка: B переразмечен", (b["rows"], b["cols"]) == (1, 2))
check("другая сетка: правки кадров B долой, правка листа осталась", b["fix"] == {"all": {"dx": 2, "dy": 0, "s": 1}}, str(b["fix"]))
a = F.describe(persona, "sheet", "A")
check("другая сетка: правки кадров A долой", a["fix"] == {}, str(a["fix"]))
check("правок нет — копия без правок удалена", not (base / "A.raw.webp").exists())
# Новая картинка
P.put_sheet(persona, "B", data_url(sheet_png(1, 2, cell=240)), 1, 2)
check("новая картинка: правки долой", F.describe(persona, "sheet", "B")["fix"] == {})
check("новая картинка: копия без правок удалена", not (base / "B.raw.webp").exists())

# ── Пробная нарезка: на диск не идёт ─────────────────────────────────
before = (base / "A.webp").read_bytes()
meta_before = (base / "portrait.json").read_bytes()
pv = F.preview(persona, "sheet", "A", {"rows": 2, "cols": 2, "bg": "auto"})
check("пробная нарезка: другая сетка в ответе", (pv["rows"], pv["cols"], pv["count"]) == (2, 2, 4))
check("пробная нарезка: атлас 2×2", atlas_of(pv["atlas"]).size == (512, 512))
check("пробная нарезка: диск не тронут",
      (base / "A.webp").read_bytes() == before and (base / "portrait.json").read_bytes() == meta_before)
check("пробная нарезка: сетка вне пределов", raises(lambda: F.preview(persona, "sheet", "A", {"rows": 99, "cols": 2}), 400))
check("пробная нарезка: чужой фон", raises(lambda: F.preview(persona, "sheet", "A", {"rows": 2, "cols": 2, "bg": "x"}), 400))

# ── Анимация ─────────────────────────────────────────────────────────
res = A.put_anim(persona, "new", {"name": "Шаги", "rows": 2, "cols": 3, "data_url": data_url(sheet_png(2, 3)), "fps": 12})
aid = res["anims"][0]["id"]
check("анимация: атласа без правок пока нет", not (A.anims_dir(persona) / f"{aid}.raw.webp").exists())
d = F.describe(persona, "anim", aid)
check("describe анимации", (d["name"], d["fps"], d["count"], d["cut"]["align"]) == ("Шаги", 12, 6, "grid"), str(d["name"]))
cut = dict(d["cut"])
cut["xs"] = [0, 190, 400, 600]
s = F.save(persona, "anim", aid, cut, {"cells": {"4": {"dx": -3, "dy": 2, "s": 1.05}}})
check("анимация: правка и линии сохранены", s["cut"]["xs"] == [0, 190, 400, 600] and s["fix"]["cells"]["4"]["s"] == 1.05)
check("анимация: копия без правок есть", (A.anims_dir(persona) / f"{aid}.raw.webp").is_file())
rec = A.find(persona, aid)
check("анимация: остальные поля целы", (rec["name"], rec["fps"]) == ("Шаги", 12))
# Правка карточкой «Анимации» — только поля: правки целы
body = {"name": "Шаги 2", "rows": 2, "cols": 3, "fps": 10, "offset_x": 0, "offset_y": 0, "frames": 5}
A.put_anim(persona, aid, body)
rec = A.find(persona, aid)
check("карточка: fps сменился, правки целы", rec["fps"] == 10 and rec["fix"].get("cells", {}).get("4", {}).get("dx") == -3)
check("карточка: линии целы", rec["xs"] == [0, 190, 400, 600])
# Кадров играется 5 — сохранение из редактора этого не сбрасывает
s = F.save(persona, "anim", aid, dict(F.describe(persona, "anim", aid)["cut"]), rec["fix"])
check("редактор: число кадров осталось", A.find(persona, aid)["frames"] == 5)
A.put_anim(persona, aid, {**body, "offset_x": 4})
rec = A.find(persona, aid)
check("карточка: другие поля — линии долой, правки целы", rec["xs"] is None and "cells" in rec["fix"])
A.put_anim(persona, aid, {**body, "offset_x": 4, "rows": 3, "cols": 2})
rec = A.find(persona, aid)
check("карточка: другая сетка — правки кадров долой", rec["fix"] == {})
F.save(persona, "anim", aid, dict(F.describe(persona, "anim", aid)["cut"]), {"all": {"dx": 1, "dy": 0, "s": 1}})
check("правка всего листа сохранена", A.find(persona, aid)["fix"] == {"all": {"dx": 1, "dy": 0, "s": 1}})
A.put_anim(persona, aid, {**body, "offset_x": 4, "rows": 3, "cols": 2, "data_url": data_url(sheet_png(3, 2))})
check("новый лист: правки долой", A.find(persona, aid)["fix"] == {})
F.save(persona, "anim", aid, dict(F.describe(persona, "anim", aid)["cut"]), {"all": {"dx": 1, "dy": 0, "s": 1}})
A.delete_anim(persona, aid)
left = [p.name for p in A.anims_dir(persona).iterdir() if aid in p.name]
check("удаление анимации: файлов не осталось", left == [], str(left))

# ── Кадр растёт под правки ───────────────────────────────────────────
def wide_png(cw, ch, cols=2, rows=2):
    """Лист с вытянутыми клетками: высокая фигура почти на всю клетку."""
    img = Image.new("RGB", (cols * cw, rows * ch), (120, 120, 120))
    d = ImageDraw.Draw(img)
    for i in range(rows * cols):
        x, y = (i % cols) * cw + cw // 2, (i // cols) * ch + ch // 2
        d.ellipse((x - ch // 3, y - int(ch * 0.46), x + ch // 3, y + int(ch * 0.46)), fill=(220, 90, 60))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


aid = A.put_anim(persona, "new", {"name": "Широкая", "rows": 2, "cols": 2, "data_url": data_url(wide_png(300, 200))})["anims"][0]["id"]
rec = A.find(persona, aid)
base_h = rec["fh"]
check("вытянутая клетка: кадр ниже 256", (rec["fw"], rec["fh"]) == (256, 171), f"{rec['fw']} {rec['fh']}")
cut = dict(F.describe(persona, "anim", aid)["cut"])
figure = box(cell_of(Image.open(A.anims_dir(persona) / f"{aid}.webp").convert("RGBA"), 0, 0, 256, 171))
s = F.save(persona, "anim", aid, cut, {"all": {"dx": 0, "dy": 0, "s": 1.3}})
rec = A.find(persona, aid)
check("масштаб 130%: кадр вырос вверх, ширина та же", rec["fw"] == 256 and 200 <= rec["fh"] <= 256, f"{rec['fw']} {rec['fh']}")
atlas = Image.open(A.anims_dir(persona) / f"{aid}.webp").convert("RGBA")
check("атлас под выросший кадр", atlas.size == (2 * rec["fw"], 2 * rec["fh"]), str(atlas.size))
grown = box(cell_of(atlas, 0, 0, rec["fw"], rec["fh"]))
want = (figure[3] - figure[1]) * 1.3
check("голова не обрезана: фигура выросла целиком", abs((grown[3] - grown[1]) - want) <= 3, f"{grown} {want}")
check("сверху остался запас: край фигуры не упёрся в кадр", grown[1] >= 0 and rec["fh"] - (grown[3] - grown[1]) >= 0)
check("низ фигуры — на полу", grown[3] == rec["fh"], f"{grown} {rec['fh']}")
check("редактору — кадр нарезки и атлас без правок", (s["fw"], s["fh"]) == (256, 171) and atlas_of(s["atlas"]).size == (512, 342), f"{s['fw']} {s['fh']}")
check("предел кадра — в ответе", s["frame_max"] == A.FRAME_MAX)
check("кадр атласа на ядре — в ответе", (s["saved_w"], s["saved_h"]) == (rec["fw"], rec["fh"]), f"{s['saved_w']} {s['saved_h']}")
s = F.save(persona, "anim", aid, cut, {"all": {"dx": 0, "dy": 0, "s": 1}, "cells": {"3": {"dx": 0, "dy": -50, "s": 1}}})
rec = A.find(persona, aid)
check("сдвиг вверх тоже растит кадр", rec["fh"] > base_h and rec["fw"] == 256, f"{rec['fw']} {rec['fh']}")
s = F.save(persona, "anim", aid, cut, {"all": {"dx": 0, "dy": 0, "s": 1}, "cells": {"3": {"dx": 0, "dy": 30, "s": 1}}})
rec = A.find(persona, aid)
check("сдвиг вниз под пол: кадр растёт вниз, пол помечен (sink)",
      rec["sink"] == 30 and rec["fh"] == base_h + 30, f"{rec['fh']} {rec['sink']}")
atlas = Image.open(A.anims_dir(persona) / f"{aid}.webp").convert("RGBA")
low = box(cell_of(atlas, 1, 1, rec["fw"], rec["fh"]))
still = box(cell_of(atlas, 0, 0, rec["fw"], rec["fh"]))
check("ушедшая под пол фигура целиком; остальные стоят на полу (sink выше низа)",
      low[3] == rec["fh"] and still[3] == rec["fh"] - 30, f"{low} {still}")
check("в записи sink по умолчанию 0", A._anim({"id": "0123abcd"})["sink"] == 0)
s = F.save(persona, "anim", aid, cut, {"box": {"l": 100, "r": 180, "t": 150, "b": 20}})
rec = A.find(persona, aid)
check("своя рамка: кадр, пол и его середина — в записи",
      (rec["fw"], rec["fh"], rec["sink"], rec["ax"]) == (280, 170, 20, 100.0), f"{rec['fw']} {rec['fh']} {rec['sink']} {rec['ax']}")
check("своя рамка — в правках редактору", s["fix"].get("box") == {"l": 100, "r": 180, "t": 150, "b": 20}, str(s["fix"]))
atlas = Image.open(A.anims_dir(persona) / f"{aid}.webp").convert("RGBA")
check("атлас — под свою рамку", atlas.size == (2 * 280, 2 * 170), str(atlas.size))
s = F.save(persona, "anim", aid, cut, {"edge": {"side": "left", "at": 50}})
rec = A.find(persona, aid)
check("край слева 50: в записи линия в кадре атласа", rec["edge"] == {"side": "left", "at": 50.0} and s["fix"].get("edge") == {"side": "left", "at": 50}, str(rec["edge"]))
check("в записи край по умолчанию None", A._anim({"id": "0123abcd"})["edge"] is None)
s = F.save(persona, "anim", aid, cut, {"floor": 60})
rec = A.find(persona, aid)
check("низ экрана 60: кадр той же высоты, пол на 60 выше низа",
      (rec["fw"], rec["fh"], rec["sink"]) == (256, base_h, 60) and s["fix"].get("floor") == 60, f"{rec['fw']} {rec['fh']} {rec['sink']} {s['fix']}")
atlas = Image.open(A.anims_dir(persona) / f"{aid}.webp").convert("RGBA")
check("в атласе фигура на месте: низ фигуры — у низа кадра", box(cell_of(atlas, 0, 0, rec["fw"], rec["fh"]))[3] == rec["fh"])
s = F.save(persona, "anim", aid, cut, {"all": {"dx": 0, "dy": 0, "s": 4}})
rec = A.find(persona, aid)
check("больше предела не растёт", rec["fh"] == A.FRAME_MAX and rec["fw"] <= A.FRAME_MAX, f"{rec['fw']} {rec['fh']}")
s = F.save(persona, "anim", aid, cut, {})
rec = A.find(persona, aid)
check("правки сняли — кадр прежний", (rec["fw"], rec["fh"], rec["sink"]) == (256, base_h, 0), f"{rec['fw']} {rec['fh']}")
A.delete_anim(persona, aid)
# Высокая клетка: растёт вбок и по-прежнему от середины
aid = A.put_anim(persona, "new", {"name": "Узкая", "rows": 1, "cols": 2, "data_url": data_url(wide_png(100, 300, cols=2, rows=1))})["anims"][0]["id"]
rec = A.find(persona, aid)
check("высокая клетка: кадр уже 256", rec["fh"] == 256 and rec["fw"] < 256, f"{rec['fw']} {rec['fh']}")
fw0 = rec["fw"]
cut = dict(F.describe(persona, "anim", aid)["cut"])
F.save(persona, "anim", aid, cut, {"cells": {"0": {"dx": 40, "dy": 0, "s": 1}}})
rec = A.find(persona, aid)
check("сдвиг вбок растит кадр на обе стороны поровну", rec["fw"] > fw0 and (rec["fw"] - fw0) % 2 == 0 and rec["fh"] == 256, f"{rec['fw']}")
atlas = Image.open(A.anims_dir(persona) / f"{aid}.webp").convert("RGBA")
b1 = box(cell_of(atlas, 0, 1, rec["fw"], rec["fh"]))
check("второй кадр остался по середине", abs((b1[0] + b1[2]) / 2 - rec["fw"] / 2) <= 2, f"{b1} {rec['fw']}")
A.delete_anim(persona, aid)
# Квадратная клетка: кадр уже 256×256, а фигуру увели за край
aid = A.put_anim(persona, "new", {"name": "Квадрат", "rows": 1, "cols": 3, "data_url": data_url(sheet_png(1, 3, cell=300))})["anims"][0]["id"]
rec = A.find(persona, aid)
check("квадратная клетка: кадр 256×256", (rec["fw"], rec["fh"]) == (256, 256), f"{rec['fw']} {rec['fh']}")
cut = dict(F.describe(persona, "anim", aid)["cut"])
plain = Image.open(A.anims_dir(persona) / f"{aid}.webp").convert("RGBA")
wide0 = box(cell_of(plain, 0, 0, 256, 256))
fix = {"cells": {"0": {"dx": -150, "dy": 0, "s": 1}, "2": {"dx": 150, "dy": 0, "s": 1}}}
s = F.save(persona, "anim", aid, cut, fix)
rec = A.find(persona, aid)
check("сдвиг за край: кадр шире 256", 256 < rec["fw"] <= A.FRAME_MAX and rec["fh"] == 256, f"{rec['fw']} {rec['fh']}")
atlas = Image.open(A.anims_dir(persona) / f"{aid}.webp").convert("RGBA")
check("атлас под широкий кадр", atlas.size == (3 * rec["fw"], 256), str(atlas.size))
left = box(cell_of(atlas, 0, 0, rec["fw"], 256))
right = box(cell_of(atlas, 0, 2, rec["fw"], 256))
check("фигура не обрезана слева: ширина та же", left[2] - left[0] == wide0[2] - wide0[0], f"{left} {wide0}")
check("фигура не обрезана справа: ширина та же", right[2] - right[0] == wide0[2] - wide0[0], f"{right} {wide0}")
check("широкий кадр читается обратно", A.get_anims(persona, False)["anims"][-1]["fw"] == rec["fw"])
check("редактору — кадр нарезки и широкий на ядре", (s["fw"], s["saved_w"]) == (256, rec["fw"]), f"{s['fw']} {s['saved_w']}")
stamp = (A.anims_dir(persona) / f"{aid}.webp").stat().st_mtime_ns
F.save(persona, "anim", aid, cut, fix, {"fps": 5, "loops": 1, "loop": False, "holds": None})
check("с правками лист режется при любом сохранении", (A.anims_dir(persona) / f"{aid}.webp").stat().st_mtime_ns != stamp)
F.save(persona, "anim", aid, cut, {"cells": {"0": {"dx": -512, "dy": 0, "s": 1}}})
rec = A.find(persona, aid)
check("сдвиг сверх предела: кадр упёрся в предел", rec["fw"] == A.FRAME_MAX, str(rec["fw"]))
A.delete_anim(persona, aid)
# Лист портрета — всегда квадрат: клетки атласа читают по номеру
P.put_sheet(persona, "C", data_url(sheet_png(1, 2, cell=240)), 1, 2)
d = F.describe(persona, "sheet", "C")
s = F.save(persona, "sheet", "C", d["cut"], {"all": {"dx": 0, "dy": 0, "s": 1.5}})
check("лист портрета не растёт", Image.open(P.portrait_dir(persona) / "C.webp").size == (512, 256))

# ── Источник ─────────────────────────────────────────────────────────
check("чужой тип источника", raises(lambda: F.describe(persona, "x", "A"), 404))
check("нет такого листа", raises(lambda: F.describe(persona, "sheet", "Z"), 404))
check("лист не загружен", raises(lambda: F.describe(persona, "sheet", "F"), 404))
check("нет такой анимации", raises(lambda: F.describe(persona, "anim", "deadbeef"), 404))
check("путь не выйти из папки", raises(lambda: F.describe(persona, "anim", "../../x"), 404))
raw, mime = F.sheet_bytes(persona, "sheet", "A")
check("исходный лист: байты и MIME", raw == raw_a and mime == "image/png", mime)
(P.portrait_dir(persona) / "A.src").unlink()
check("исходника нет: GET живой, линий нет", F.describe(persona, "sheet", "A")["cut"]["xs"] is None)
check("исходника нет: пробная нарезка — 400", raises(lambda: F.preview(persona, "sheet", "A", {"rows": 1, "cols": 2}), 400))
check("исходника нет: лист не отдать", raises(lambda: F.sheet_bytes(persona, "sheet", "A"), 404))

# ── Маршруты ─────────────────────────────────────────────────────────

# ── Время кадров ─────────────────────────────────────────────────────
aid = A.put_anim(persona, "new", {"name": "Часы", "rows": 2, "cols": 2, "data_url": data_url(sheet_png(2, 2)), "fps": 10})["anims"][-1]["id"]
rec = A.find(persona, aid)
check("время: по умолчанию ровно и не по кругу", rec["holds"] is None and rec["loop"] is False)
d = F.describe(persona, "anim", aid)
check("время: describe отдаёт", d["timing"] == {"fps": 10, "loops": 1, "loop": False, "holds": None, "frames": 4}, str(d["timing"]))
check("время: у листа портрета его нет", F.describe(persona, "sheet", "A")["timing"] is None)
cut = dict(d["cut"])
A.put_cut(persona, aid, {**cut, "xs": None, "ys": None}, {})  # линии записаны: дальше сравнение честное
cut = dict(F.describe(persona, "anim", aid)["cut"])
F.save(persona, "anim", aid, cut, {})
atlas = A.anims_dir(persona) / f"{aid}.webp"
stamp = atlas.stat().st_mtime_ns
updated = A.find(persona, aid)["updated_at"]
s_ = F.save(persona, "anim", aid, cut, {}, {"fps": 6, "loops": 3, "loop": True, "holds": [1, 2.5, 0.5, 1]})
check("время: сохранено", s_["timing"] == {"fps": 6, "loops": 3, "loop": True, "holds": [1.0, 2.5, 0.5, 1.0], "frames": 4}, str(s_["timing"]))
check("время: то же в обычном GET", A.get_anims(persona, False)["anims"][-1]["holds"] == [1.0, 2.5, 0.5, 1.0])
check("время: fps в describe тот же", s_["fps"] == 6)
check("время: лист не резали", atlas.stat().st_mtime_ns == stamp)
check("время: версия анимации новая", A.find(persona, aid)["updated_at"] > updated)
F.save(persona, "anim", aid, cut, {"cells": {"1": {"dx": 2, "dy": 0, "s": 1}}})
check("без времени в запросе оно остаётся", A.find(persona, aid)["holds"] == [1.0, 2.5, 0.5, 1.0])
check("правка кадра режет лист", atlas.stat().st_mtime_ns != stamp)
card = {"name": "Часы", "rows": 2, "cols": 2, "fps": 8, "offset_x": 0, "offset_y": 0}
A.put_anim(persona, aid, card)
rec = A.find(persona, aid)
check("карточка: время кадров и круг целы", rec["holds"] == [1.0, 2.5, 0.5, 1.0] and rec["loop"] is True and rec["fps"] == 8)
A.put_anim(persona, aid, {**card, "loop": False})
check("карточка: круг снимается", A.find(persona, aid)["loop"] is False)
A.put_anim(persona, aid, {**card, "loop": True})
check("карточка: круг ставится", A.find(persona, aid)["loop"] is True)

def holds_of(value):
    return A._anim({"id": "abcdef01", "rows": 2, "cols": 2, "holds": value})["holds"]
check("пределы: мало и много", holds_of([0, 9999, 1, 1]) == [A.HOLD_MIN, A.HOLD_MAX, 1, 1], str(holds_of([0, 9999, 1, 1])))
check("пределы: длиннее листа — нет", holds_of([1, 2, 3, 4, 5]) is None)
check("пределы: короче листа можно", holds_of([2, 3]) == [2, 3])
check("пределы: не числа — нет", holds_of([1, "2", 3, 4]) is None and holds_of([True, 2, 3, 4]) is None and holds_of("x") is None)
check("пределы: не конечные числа — единица", holds_of([float("nan"), 2]) == [1.0, 2] and holds_of([float("inf"), 2]) == [1.0, 2])
check("пределы: все единицы — ровно", holds_of([1, 1, 1, 1]) is None and holds_of([]) is None)

F.save(persona, "anim", aid, dict(F.describe(persona, "anim", aid)["cut"]), {}, {"fps": 8, "loops": 1, "loop": False, "holds": [1, 1, 1, 1]})
check("сохранить ровно — времени кадров нет", A.find(persona, aid)["holds"] is None)
F.save(persona, "anim", aid, dict(F.describe(persona, "anim", aid)["cut"]), {}, {"fps": 8, "loops": 1, "loop": False, "holds": [2, 1, 1, 1]})
grid = dict(F.describe(persona, "anim", aid)["cut"])
F.save(persona, "anim", aid, {**grid, "rows": 1, "cols": 2, "xs": None, "ys": None}, {}, {"fps": 8, "loops": 1, "loop": False, "holds": [2, 1, 1, 1]})
check("редактор: другая сетка — время кадров долой", A.find(persona, aid)["holds"] is None)
F.save(persona, "anim", aid, dict(F.describe(persona, "anim", aid)["cut"]), {}, {"fps": 8, "loops": 1, "loop": True, "holds": [2, 1]})
A.put_anim(persona, aid, {**card, "rows": 2, "cols": 1})
rec = A.find(persona, aid)
check("карточка: другая сетка — время кадров долой, круг цел", rec["holds"] is None and rec["loop"] is True)
F.save(persona, "anim", aid, dict(F.describe(persona, "anim", aid)["cut"]), {}, {"fps": 8, "loops": 1, "loop": True, "holds": [2, 1]})
A.put_anim(persona, aid, {**card, "rows": 2, "cols": 1, "data_url": data_url(sheet_png(2, 1))})
rec = A.find(persona, aid)
check("новая картинка — время кадров долой", rec["holds"] is None)

# ── Порядок кадров анимации ──────────────────────────────────────────
def order_of(value, cells=4):
    return A._order(value, cells)
check("порядок: как на листе — нет", order_of([0, 1, 2, 3]) is None)
check("порядок: битый — нет", all(order_of(v) is None for v in ([], [0, 4], [-1], [True], ["1"], "x", [0] * (A.MAX_SLOTS + 1))))
check("порядок: переставили и удалили", order_of([2, 0]) == [2, 0])
check("порядок: клетка не раз, кадров больше клеток", order_of([0, 0]) == [0, 0] and order_of([0, 1, 2, 3, 0]) == [0, 1, 2, 3, 0])


def colour(atlas, r, c, fw, fh):
    cell = cell_of(atlas, r, c, fw, fh)
    solid = cell[:, :, 3] > 128
    return tuple(int(v) for v in cell[solid][:, :3].mean(axis=0)) if solid.any() else None


aid = A.put_anim(persona, "new", {"name": "Порядок", "rows": 1, "cols": 3, "data_url": data_url(sheet_png(1, 3)), "fps": 8})["anims"][-1]["id"]
rec = A.find(persona, aid)
fw, fh = rec["fw"], rec["fh"]
adir = A.anims_dir(persona)
plain = Image.open(adir / f"{aid}.webp").convert("RGBA")
cols_ = [colour(plain, 0, c, fw, fh) for c in range(3)]
check("порядок: по умолчанию как на листе", rec["order"] is None and len(set(cols_)) == 3, str(cols_))
d = F.describe(persona, "anim", aid)
check("порядок: describe отдаёт", d["order"] is None and F.describe(persona, "sheet", "B")["order"] is None)
cut = dict(d["cut"])
s = F.save(persona, "anim", aid, cut, {"cells": {"0": {"dx": 4, "dy": 0, "s": 1}}},
           {"fps": 8, "loops": 1, "loop": False, "holds": [2, 1, 1], "frames": 3}, [2, 0, 1])
played = Image.open(adir / f"{aid}.webp").convert("RGBA")
check("порядок: кадр 1 — третья клетка", colour(played, 0, 0, fw, fh) == cols_[2], f"{colour(played, 0, 0, fw, fh)} {cols_}")
check("порядок: кадры 2, 3 — первая и вторая", [colour(played, 0, c, fw, fh) for c in (1, 2)] == cols_[:2])
check("порядок: правка — у кадра, а не у клетки",
      box(cell_of(played, 0, 0, fw, fh))[0] - box(cell_of(plain, 0, 2, fw, fh))[0] == 4)
check("порядок: в ответе и в записи", s["order"] == [2, 0, 1] and A.find(persona, aid)["order"] == [2, 0, 1])
check("порядок: редактору — клетки по листу",
      [colour(atlas_of(s["atlas"]), 0, c, s["fw"], s["fh"]) for c in range(3)] == cols_)
s = F.save(persona, "anim", aid, cut, {}, None, [1, 2, 0])
check("порядок без правок: атлас без правок всё равно есть", (adir / f"{aid}.raw.webp").is_file())
check("порядок без правок: редактору — клетки по листу",
      [colour(atlas_of(s["atlas"]), 0, c, s["fw"], s["fh"]) for c in range(3)] == cols_)
s = F.save(persona, "anim", aid, cut, {})
check("порядок: не прислали — как был", A.find(persona, aid)["order"] == [1, 2, 0])
s = F.save(persona, "anim", aid, cut, {}, {"fps": 8, "loops": 1, "loop": False, "holds": [3, 1]}, [1, 0])
rec = A.find(persona, aid)
played = Image.open(adir / f"{aid}.webp").convert("RGBA")
check("удалили кадр: играется два, третий пуст",
      rec["frames"] == 2 and colour(played, 0, 2, rec["fw"], rec["fh"]) is None and colour(played, 0, 0, fw, fh) == cols_[1], str(rec["frames"]))
check("удалили кадр: время кадров не длиннее кадров", rec["holds"] == [3.0, 1.0], str(rec["holds"]))
s = F.save(persona, "anim", aid, cut, {"cells": {"2": {"dx": 5, "dy": 0, "s": 1}}}, {"fps": 8, "loops": 1, "loop": False, "holds": None, "frames": 1})
rec = A.find(persona, aid)
check("кадров играть — из времени", rec["frames"] == 1 and s["count"] == 1 and s["timing"]["frames"] == 1, str(rec["frames"]))
check("правка удалённого кадра не хранится", rec["fix"] == {}, str(rec["fix"]))
A.put_anim(persona, aid, {"name": "Порядок", "rows": 1, "cols": 3, "fps": 8, "frames": 3})
rec = A.find(persona, aid)
check("карточка: порядок цел, кадров не больше, чем есть", rec["order"] == [1, 0] and rec["frames"] == 2, f"{rec['order']} {rec['frames']}")
# Кадр с листа ещё раз: 1‑2‑3‑2 — атлас растёт вниз
s = F.save(persona, "anim", aid, cut, {"cells": {"3": {"dx": 6, "dy": 0, "s": 1}}},
           {"fps": 8, "loops": 1, "loop": True, "holds": [1, 1, 1, 2], "frames": 4}, [0, 1, 2, 1])
rec = A.find(persona, aid)
check("1‑2‑3‑2: порядок, кадров 4, рядов в атласе 2",
      (rec["order"], rec["frames"], rec["slots"], rec["atlas_rows"]) == ([0, 1, 2, 1], 4, 4, 2), f"{rec['order']} {rec['frames']} {rec['slots']} {rec['atlas_rows']}")
played = Image.open(adir / f"{aid}.webp").convert("RGBA")
fw2, fh2 = rec["fw"], rec["fh"]
check("1‑2‑3‑2: атлас 3×2 кадра", played.size == (3 * fw2, 2 * fh2), str(played.size))
check("1‑2‑3‑2: кадр 4 — вторая клетка, со своей правкой",
      max(abs(a - b) for a, b in zip(colour(played, 1, 0, fw2, fh2), cols_[1])) <= 2
      and box(cell_of(played, 1, 0, fw2, fh2))[0] - box(cell_of(played, 0, 1, fw2, fh2))[0] == 6)
check("1‑2‑3‑2: время 4-го кадра сохранено", rec["holds"] == [1.0, 1.0, 1.0, 2.0], str(rec["holds"]))
check("1‑2‑3‑2: редактору — клетки по листу", atlas_of(s["atlas"]).size == (3 * s["fw"], s["fh"]))
check("1‑2‑3‑2: в обычном GET — atlas_rows и slots",
      [(a["atlas_rows"], a["slots"]) for a in A.get_anims(persona, False)["anims"] if a["id"] == aid] == [(2, 4)])
F.save(persona, "anim", aid, cut, {}, None, None)
check("порядок как на листе: снова все клетки", A.find(persona, aid)["order"] is None and A.find(persona, aid)["frames"] == 3)
check("порядок как на листе: атлас в один ряд", A.find(persona, aid)["atlas_rows"] == 1)
F.save(persona, "anim", aid, cut, {}, None, [2, 1])
A.put_anim(persona, aid, {"name": "Порядок", "rows": 3, "cols": 1, "fps": 8})
check("карточка: другая сетка — порядок долой", A.find(persona, aid)["order"] is None)
F.save(persona, "anim", aid, dict(F.describe(persona, "anim", aid)["cut"]), {}, None, [2, 1])
F.save(persona, "anim", aid, {**dict(F.describe(persona, "anim", aid)["cut"]), "rows": 1, "cols": 3, "xs": None, "ys": None}, {}, None, [2, 1])
check("редактор: другая сетка — порядок долой", A.find(persona, aid)["order"] is None)
F.save(persona, "anim", aid, dict(F.describe(persona, "anim", aid)["cut"]), {}, None, [2, 1])
A.put_anim(persona, aid, {"name": "Порядок", "rows": 1, "cols": 3, "fps": 8, "data_url": data_url(sheet_png(1, 3))})
check("новая картинка — порядок долой", A.find(persona, aid)["order"] is None)
F.save(persona, "sheet", "B", F.describe(persona, "sheet", "B")["cut"], {}, None, [1, 0])
check("у листа портрета порядка нет", F.describe(persona, "sheet", "B")["order"] is None)

try:
    from fastapi.testclient import TestClient
except ImportError as e:
    TestClient = None
    print(f"(маршруты пропущены — fastapi/httpx недоступны: {e})")
if TestClient:
    import app.api.server as server_mod
    server_mod._api_token = "s3cr3t"
    client = TestClient(server_mod.app, base_url="http://127.0.0.1", client=("192.168.1.20", 50000))
    auth = {"Authorization": "Bearer s3cr3t"}
    P.put_sheet("connor", "A", data_url(sheet_png(2, 2, cell=240)), 2, 2)
    A.put_anim("connor", "new", {"name": "Шаги", "rows": 2, "cols": 2, "data_url": data_url(sheet_png(2, 2))})
    aid = A.get_anims("connor", False)["anims"][0]["id"]
    base = "/api/personas/connor/portrait/frames"
    check("без токена — 401", client.get(f"{base}/sheet/A").status_code == 401)
    check("исходник без токена — 401", client.get(f"{base}/sheet/A/sheet").status_code == 401)
    check("пробная нарезка без токена — 401", client.post(f"{base}/sheet/A/cut", json={"rows": 1, "cols": 1}).status_code == 401)
    check("сохранение без токена — 401", client.put(f"{base}/sheet/A", json={"rows": 1, "cols": 1}).status_code == 401)
    check("нет персоны — 404",
          client.get("/api/personas/no_such_persona/portrait/frames/sheet/A", headers=auth).status_code == 404)
    r = client.get(f"{base}/sheet/A", headers=auth)
    check("GET листа", r.status_code == 200 and r.json()["kind"] == "sheet" and r.json()["atlas"].startswith("data:image/webp"))
    r = client.get(f"{base}/anim/{aid}", headers=auth)
    check("GET анимации", r.status_code == 200 and r.json()["name"] == "Шаги")
    cut = r.json()["cut"]
    r = client.get(f"{base}/sheet/A/sheet", headers=auth)
    check("исходный лист: картинка", r.status_code == 200 and r.headers["content-type"] == "image/png")
    r = client.post(f"{base}/anim/{aid}/cut", headers=auth, json={**cut, "rows": 1})
    check("пробная нарезка: линии не от той сетки — 400", r.status_code == 400)
    r = client.post(f"{base}/anim/{aid}/cut", headers=auth, json={**cut, "rows": 1, "ys": None})
    check("пробная нарезка: ряд один", r.status_code == 200 and r.json()["rows"] == 1)
    r = client.put(f"{base}/anim/{aid}", headers=auth, json={**cut, "fix": {"cells": {"1": {"dx": 3, "dy": 0, "s": 1}}}})
    check("PUT: правка на месте", r.status_code == 200 and r.json()["fix"]["cells"]["1"]["dx"] == 3)
    r = client.get("/api/personas/connor/portrait/anims", headers=auth)
    check("атлас с правкой — в обычном GET анимаций", r.status_code == 200 and r.json()["anims"][0]["fix"]["cells"]["1"]["dx"] == 3)
    r = client.put(f"{base}/anim/{aid}", headers=auth, json={**cut, "timing": {"fps": 12, "loops": 2, "loop": True, "holds": [3, 1]}})
    check("PUT: время кадров", r.status_code == 200 and r.json()["timing"]["holds"][0] == 3 and r.json()["timing"]["fps"] == 12, r.text[:200])
    r = client.put(f"{base}/anim/{aid}", headers=auth, json={**cut, "timing": {"holds": [1] * 145}})
    check("PUT: длинный список времени — 422", r.status_code == 422)
    r = client.put(f"{base}/nope/x", headers=auth, json={"rows": 1, "cols": 1})
    check("чужой тип — 404", r.status_code == 404)
    from app.api import said
    _, start = said.take(None, acts=True)
    r = client.put(f"{base}/anim/{aid}", headers=auth, json={**cut, "order": [3, 1, 0]})
    check("PUT: порядок кадров", r.status_code == 200 and r.json()["order"] == [3, 1, 0], r.text[:200])
    items, _ = said.take(start, "web_user", acts=True)
    check("PUT кадров — портрету на столе событие portrait", [(m["persona"], m["kind"]) for m in items] == [("connor", "portrait")])
    _, start = said.take(None, acts=True)
    client.post(f"{base}/anim/{aid}/cut", headers=auth, json={**cut, "rows": 1, "ys": None})
    check("пробная нарезка — без события", said.take(start, "web_user", acts=True)[0] == [])
    r = client.put(f"{base}/anim/{aid}", headers=auth, json={**cut, "fix": {"cells": {"0": {"r": 15, "px": 1, "py": -40}}}})
    check("PUT без порядка — как был", r.status_code == 200 and r.json()["order"] == [3, 1, 0])
    check("PUT: поворот кадра", r.json()["fix"]["cells"]["0"] == {"dx": 0, "dy": 0, "s": 1, "r": 15.0, "px": 1.0, "py": -40.0}, r.text[:300])
    r = client.put(f"{base}/anim/{aid}", headers=auth, json={**cut, "order": None})
    check("PUT order null — как на листе", r.status_code == 200 and r.json()["order"] is None)
    r = client.put(f"{base}/anim/{aid}", headers=auth, json={**cut, "order": list(range(145))})
    check("PUT: длинный порядок — 422", r.status_code == 422)

print(f"\n{ok} OK, {fail} FAIL")
sys.exit(1 if fail else 0)

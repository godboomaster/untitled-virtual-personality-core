"""Тест живого портрета (app/api/portrait_api.py): листы ракурсов головы
режутся в атлас, фон убирается.

  - нарезка: атлас cols×rows клеток по CELL_PX; сплошной фон прозрачный, а
    серая деталь внутри головы того же цвета, что фон, остаётся; плечи у края
    клетки не мешают найти фон; лист с прозрачностью и режим keep фон не
    трогают; поля (offset) учитываются; фигура — низом к низу кадра, по
    середине; ряды голов не по сетке (шаг чаще клетки) — фигуры не обрезаны
    и стоят одинаково во всех клетках;
  - хранение: put/get/delete листов, переразметка без новой картинки,
    общая сетка (новая — остальные листы режутся заново), ошибки 400/413;
  - маршруты GET/PUT/DELETE /api/personas/{p}/portrait и список
    GET /api/portraits: 404 без персоны,
    401 без токена.

LLM, Ollama и сеть не вызываются, data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_portrait
"""

import base64
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="portrait_")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


BG = (130, 129, 129)
SIDE = 500  # лист 5×5 по 100 px


def make_sheet(color=(200, 60, 60), shoulders=False, transparent=False, offset=0, intruder=False) -> bytes:
    """Лист 5×5: в каждой клетке круг-«голова» с серой полосой цвета фона
    внутри; shoulders — шея и «плечи» до нижних углов клетки; intruder —
    обрывок соседней головы у верхнего правого угла клетки."""
    side = SIDE + 2 * offset
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0) if transparent else BG + (255,))
    d = ImageDraw.Draw(img)
    rng = np.random.default_rng(1)
    for r in range(5):
        for c in range(5):
            x0, y0 = offset + c * 100, offset + r * 100
            d.ellipse([x0 + 25, y0 + 15, x0 + 75, y0 + 65], fill=color + (255,))
            # Серая «прядь» того же цвета, что фон, — внутри головы
            d.rectangle([x0 + 45, y0 + 25, x0 + 55, y0 + 55], fill=BG + (255,))
            if shoulders:
                d.rectangle([x0 + 44, y0 + 60, x0 + 56, y0 + 80], fill=(40, 40, 40, 255))
                d.rectangle([x0, y0 + 80, x0 + 99, y0 + 99], fill=(40, 40, 40, 255))
            if intruder:
                d.ellipse([x0 + 80, y0 - 10, x0 + 99, y0 + 8], fill=(40, 160, 40, 255))
    arr = np.asarray(img).astype(np.int16)
    if not transparent:
        # Шум фона, как у сгенерированных листов
        arr[:, :, :3] += rng.integers(-2, 3, size=arr[:, :, :3].shape)
    buf = io.BytesIO()
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA").save(buf, "PNG")
    return buf.getvalue()


def data_url(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def atlas_of(url: str) -> np.ndarray:
    raw = base64.b64decode(url.split(",", 1)[1])
    return np.asarray(Image.open(io.BytesIO(raw)).convert("RGBA"))


def make_busts(pitch: int) -> bytes:
    """Лист 5×5 «бюстов» (голова и туловище), ряды и столбцы — с шагом pitch
    вместо 100: как у генератора, который ставит головы не по сетке."""
    img = Image.new("RGBA", (SIDE, SIDE), BG + (255,))
    d = ImageDraw.Draw(img)
    for r in range(5):
        for c in range(5):
            x0, y0 = 10 + c * pitch, 10 + r * pitch
            d.ellipse([x0 + 20, y0, x0 + 60, y0 + 40], fill=(200, 60, 60, 255))
            d.rectangle([x0 + 10, y0 + 38, x0 + 70, y0 + 80], fill=(40, 40, 40, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def cell_box(a: np.ndarray, r: int, c: int, cell: int):
    """Рамка непрозрачного в клетке атласа: (верх, низ, лево, право) или None."""
    solid = a[r * cell:(r + 1) * cell, c * cell:(c + 1) * cell, 3] > 128
    if not solid.any():
        return None
    ys, xs = np.nonzero(solid)
    return int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())


def test_build():
    section("A. Нарезка листа")
    from app.api.portrait_api import CELL_PX, build_atlas
    C = CELL_PX
    a = np.asarray(build_atlas(make_sheet(), 5, 5))
    check("атлас 5×5 клеток по CELL_PX", a.shape[:2] == (5 * C, 5 * C))
    box = cell_box(a, 2, 1, C)
    check("голова — низом к низу кадра, по середине",
          box is not None and box[1] >= C - 2 and abs((box[2] + box[3]) / 2 - C / 2) <= 2)
    top, bot, left, right = box
    # Клетка (2, 1): левая часть головы (прядь — посередине) и углы клетки
    cy, cx = 2 * C + (top + bot) // 2, 1 * C + left + (right - left) // 5
    check("голова непрозрачная и своего цвета",
          a[cy, cx, 3] == 255 and abs(int(a[cy, cx, 0]) - 200) < 12)
    check("фон по углам клетки прозрачный",
          a[2 * C + 3, 1 * C + 3, 3] == 0 and a[3 * C - 4, 2 * C - 4, 3] == 0)
    sy, sx = cy, 1 * C + (left + right) // 2
    check("серая прядь цвета фона внутри головы осталась", a[sy, sx, 3] == 255)

    a = np.asarray(build_atlas(make_sheet(shoulders=True), 5, 5))
    sbox = cell_box(a, 1, 3, C)
    check("плечи до углов клетки (слились с соседними): фон над ними прозрачный, плечи на месте",
          a[1 * C + 3, 3 * C + 3, 3] == 0 and sbox is not None
          and a[1 * C + sbox[1] - 3, 3 * C + sbox[2] + 3, 3] == 255)

    a = np.asarray(build_atlas(make_sheet(intruder=True), 5, 5))
    cell = a[2 * C:3 * C, 1 * C:2 * C]
    green = (cell[:, :, 3] > 0) & (cell[:, :, 1] > 120) & (cell[:, :, 0] < 100)
    check("соседняя голова у края клетки убрана, голова на месте",
          not green.any() and a[cy, cx, 3] == 255)

    a = np.asarray(build_atlas(make_sheet(transparent=True), 5, 5))
    check("лист с прозрачностью: фон прозрачный, прядь — как в листе",
          a[2 * C + 3, 1 * C + 3, 3] == 0 and a[sy, sx, 3] == 255)

    a = np.asarray(build_atlas(make_sheet(), 5, 5, bg="keep"))
    check("bg=keep: фон не тронут", a[2 * C + 3, 1 * C + 3, 3] == 255)

    a = np.asarray(build_atlas(make_sheet(offset=40), 5, 5, offset_x=40, offset_y=40))
    check("поля листа (offset) учтены: голова там же, где без полей",
          cell_box(a, 2, 1, C) == box and a[2 * C + 3, 1 * C + 3, 3] == 0)

    # Ряды и столбцы с шагом 92 при клетке 100: ровная нарезка резала бы
    # нижние ряды по макушке, правые столбцы — по боку
    a = np.asarray(build_atlas(make_busts(92), 5, 5))
    boxes = [cell_box(a, r, c, C) for r in range(5) for c in range(5)]
    ref = boxes[0]
    check("головы не по сетке: во всех 25 клетках фигура стоит одинаково",
          None not in boxes and all(max(abs(b[i] - ref[i]) for i in range(4)) <= 1 for b in boxes))
    check("…и целиком: низ у низа кадра, сверху и по бокам запас",
          ref[1] >= C - 2 and ref[0] > 10 and ref[2] > 10 and ref[3] < C - 10)


def test_store():
    section("B. Хранение листов")
    from app.api import portrait_api as pa
    persona = "portrait_t"
    empty = pa.get_portrait(persona)
    check("портрета нет — sheets пусто, сетка 5×5", empty["sheets"] == {} and (empty["rows"], empty["cols"]) == (5, 5))

    p = pa.put_sheet(persona, "A", data_url(make_sheet()), 5, 5)
    check("лист A загружен — атлас data-URL webp", p["sheets"].get("A", "").startswith("data:image/webp;base64,"))
    check("настройки листа сохранены", p["settings"]["A"]["bg"] == "auto" and p["settings"]["A"]["offset_x"] == 0)
    p = pa.put_sheet(persona, "D", data_url(make_sheet(color=(60, 60, 200))), 5, 5)
    check("второй лист D рядом с A", set(p["sheets"]) == {"A", "D"})

    p = pa.put_sheet(persona, "D", None, 5, 5, bg="keep")
    check("переразметка без картинки — из сохранённого листа",
          p["settings"]["D"]["bg"] == "keep" and atlas_of(p["sheets"]["D"])[3, 3, 3] == 255)

    p = pa.put_sheet(persona, "A", None, 4, 4)
    check("новая сетка 4×4 — и остальные листы перерезаны",
          (p["rows"], p["cols"]) == (4, 4)
          and atlas_of(p["sheets"]["D"]).shape[:2] == (4 * pa.CELL_PX, 4 * pa.CELL_PX))

    def status(fn):
        try:
            fn()
            return 200
        except pa.RoomError as e:
            return e.status

    check("неизвестный лист — 400", status(lambda: pa.put_sheet(persona, "Z", None, 5, 5)) == 400)
    check("сетка 0 или больше 10 — 400",
          status(lambda: pa.put_sheet(persona, "A", None, 0, 5)) == 400
          and status(lambda: pa.put_sheet(persona, "A", None, 11, 5)) == 400)
    check("переразметка незагруженного листа — 400", status(lambda: pa.put_sheet(persona, "B", None, 5, 5)) == 400)
    check("не картинка — 400", status(lambda: pa.put_sheet(persona, "B", "data:image/png;base64,AAAA", 5, 5)) == 400)
    check("фон не auto/keep — 400", status(lambda: pa.put_sheet(persona, "A", None, 5, 5, bg="x")) == 400)
    orig = pa.SHEET_MAX_BYTES
    pa.SHEET_MAX_BYTES = 100
    try:
        check("больше предела — 413", status(lambda: pa.put_sheet(persona, "B", data_url(make_sheet()), 5, 5)) == 413)
    finally:
        pa.SHEET_MAX_BYTES = orig

    p = pa.delete_sheet(persona, "D")
    check("удаление листа", set(p["sheets"]) == {"A"} and not (pa.portrait_dir(persona) / "D.src").exists())
    p = pa.delete_all(persona)
    check("удаление портрета целиком", p["sheets"] == {})


def test_routes():
    section("C. Маршруты /api/personas/{p}/portrait")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    orig = server_mod._api_token
    server_mod._api_token = "s3cr3t"
    try:
        client = TestClient(server_mod.app, base_url="http://127.0.0.1")
        auth = {"Authorization": "Bearer s3cr3t"}
        r = client.get("/api/personas/connor/portrait")
        check("без токена — 401", r.status_code == 401)
        r = client.get("/api/personas/no_such_persona/portrait", headers=auth)
        check("нет персоны — 404", r.status_code == 404)
        r = client.put("/api/personas/connor/portrait/A", headers=auth,
                       json={"data_url": data_url(make_sheet()), "rows": 5, "cols": 5})
        check("PUT листа — 200 и атлас", r.status_code == 200 and "A" in r.json().get("sheets", {}))
        r = client.get("/api/personas/connor/portrait", headers=auth)
        check("GET — тот же лист", r.status_code == 200 and "A" in r.json()["sheets"])
        r = client.get("/api/portraits", headers=auth)
        check("список персон с портретом", r.status_code == 200 and "connor" in r.json()["personas"])
        r = client.put("/api/personas/connor/portrait/Q", headers=auth, json={"rows": 5, "cols": 5})
        check("PUT неизвестного листа — 400", r.status_code == 400)
        r = client.delete("/api/personas/connor/portrait/A", headers=auth)
        check("DELETE листа — 200, листов нет", r.status_code == 200 and r.json()["sheets"] == {})
        r = client.get("/api/portraits", headers=auth)
        check("без листа A — не в списке", r.status_code == 200 and "connor" not in r.json()["personas"])
    finally:
        server_mod._api_token = orig


def main():
    test_build()
    test_store()
    test_routes()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

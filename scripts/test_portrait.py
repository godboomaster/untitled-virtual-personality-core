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
    401 без токена;
  - гласные (VA…VO) и эмоции: свои наборы листов «эмоция.лист», имя и
    подсказка, нота в промпт и маркер [EMO:имя], новая сетка режет и их,
    удаление с файлами, старая запись без эмоций; событие emo в ленте
    реплик — только клиентам с emo, маршруты эмоций;
  - эмоции по настроению: условие (зона, слова) хранится и снимается,
    настроение персоны из файлов живого состояния, имена своих эмоций — в
    правилах тега тика и урожая диалога, маршруты.

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
        # Запросы из сети (с этой машины токен не нужен)
        client = TestClient(server_mod.app, base_url="http://127.0.0.1", client=("192.168.1.20", 50000))
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
        sleep = r.json().get("asleep") or {}
        check("в списке — спит ли каждая (true/false/null)",
              "connor" in sleep and sleep["connor"] in (None, True, False))
        updated = (r.json().get("updated") or {}).get("connor")
        got = client.get("/api/personas/connor/portrait", headers=auth).json()
        check("в списке — когда портрет правили (то же, что updated_at портрета)",
              isinstance(updated, float) and updated == got["updated_at"])
        r = client.put("/api/personas/connor/portrait/Q", headers=auth, json={"rows": 5, "cols": 5})
        check("PUT неизвестного листа — 400", r.status_code == 400)
        r = client.delete("/api/personas/connor/portrait/A", headers=auth)
        check("DELETE листа — 200, листов нет", r.status_code == 200 and r.json()["sheets"] == {})
        r = client.get("/api/portraits", headers=auth)
        check("без листа A — не в списке", r.status_code == 200 and "connor" not in r.json()["personas"])
    finally:
        server_mod._api_token = orig


def test_emotions():
    section("D. Гласные и эмоции")
    from app.api import portrait_api as pa
    persona = "portrait_e"

    def status(fn):
        try:
            fn()
            return 200
        except pa.RoomError as e:
            return e.status

    pa.put_sheet(persona, "A", data_url(make_sheet()), 5, 5)
    p = pa.put_sheet(persona, "VO", data_url(make_sheet(color=(60, 200, 60))), 5, 5)
    check("лист гласной VO рядом с A", set(p["sheets"]) == {"A", "VO"} and p["emotions"] == [])
    check("строчной гласной нет (a.webp и A.webp — один файл на macOS) — 400",
          status(lambda: pa.put_sheet(persona, "a", None, 5, 5)) == 400)

    check("лист несуществующей эмоции — 404",
          status(lambda: pa.put_sheet(persona, "m000000.A", data_url(make_sheet()), 5, 5)) == 404)
    check("битый id эмоции — 400", status(lambda: pa.put_sheet(persona, "../x.A", None, 5, 5)) == 400)
    p = pa.put_emotion(persona, "new", "  Радость [x]\n", "когда рада")
    emo = p["created"]
    check("новая эмоция: id выдан, имя без скобок и переносов",
          bool(pa._EMO_ID_RE.match(emo)) and p["emotions"][0]["name"] == "Радость x"
          and p["emotions"][0]["hint"] == "когда рада" and p["emotions"][0]["sheets"] == {})
    check("эмоция без своего A — персона её не выбирает", pa.llm_emotions(persona) == [] and pa.emotion_note(persona) is None)
    check("второе такое же имя — 400", status(lambda: pa.put_emotion(persona, "new", "радость X")) == 400)
    check("пустое имя — 400", status(lambda: pa.put_emotion(persona, "new", " [] ")) == 400)
    check("правка неизвестной эмоции — 404", status(lambda: pa.put_emotion(persona, "m111111", "злость")) == 404)

    p = pa.put_sheet(persona, f"{emo}.A", data_url(make_sheet(color=(200, 200, 60))), 5, 5)
    e = p["emotions"][0]
    check("лист эмоции — у эмоции, не в обычных листах",
          set(p["sheets"]) == {"A", "VO"} and set(e["sheets"]) == {"A"} and e["settings"]["A"]["bg"] == "auto")
    p = pa.put_sheet(persona, f"{emo}.VA", data_url(make_sheet(color=(20, 200, 200))), 5, 5)
    check("у эмоции и свои гласные", set(p["emotions"][0]["sheets"]) == {"A", "VA"})
    check("файлы эмоции — с её id", (pa.portrait_dir(persona) / f"{emo}.VA.webp").is_file())
    check("лист эмоции известен редактору кадров", pa.known_sheet(persona, f"{emo}.A")
          and not pa.known_sheet(persona, "m999999.A"))

    note = pa.emotion_note(persona) or ""
    check("нота: имя, подсказка и маркер", "Радость x (когда рада)" in note and "[EMO:name]" in note)
    check("маркер по имени без регистра и пробелов", pa.match_emotion(persona, " радость   X ") == emo
          and pa.match_emotion(persona, "злость") is None)

    p = pa.put_sheet(persona, "A", None, 4, 4)
    check("новая сетка — и листы эмоции перерезаны",
          atlas_of(p["emotions"][0]["sheets"]["A"]).shape[:2] == (4 * pa.CELL_PX, 4 * pa.CELL_PX))

    p = pa.put_emotion(persona, emo, "Восторг", "")
    check("переименовать эмоцию — листы на месте", p["emotions"][0]["name"] == "Восторг"
          and set(p["emotions"][0]["sheets"]) == {"A", "VA"})
    p = pa.delete_sheet(persona, f"{emo}.VA")
    check("удалить лист эмоции", set(p["emotions"][0]["sheets"]) == {"A"})
    p = pa.delete_emotion(persona, emo)
    check("удалить эмоцию — с листами и файлами",
          p["emotions"] == [] and set(p["sheets"]) == {"A", "VO"}
          and not list(pa.portrait_dir(persona).glob(f"{emo}.*")))
    check("удалить ещё раз — 404", status(lambda: pa.delete_emotion(persona, emo)) == 404)

    # Старый portrait.json (без emotions) и чужие ключи читаются
    import json
    f = pa.portrait_dir(persona) / "portrait.json"
    raw = json.loads(f.read_text(encoding="utf-8"))
    raw.pop("emotions", None)
    raw["sheets"]["m222222.A"] = {"bg": "auto"}  # эмоции нет — лист не в счёт
    f.write_text(json.dumps(raw), encoding="utf-8")
    p = pa.get_portrait(persona)
    check("старая запись без эмоций читается", p["emotions"] == [] and set(p["sheets"]) == {"A", "VO"})
    pa.delete_all(persona)


def test_emotion_feed():
    section("E. Эмоция в ленте реплик и маршруты эмоций")
    from app.api import said
    said._recent.clear()
    c0 = said.cursor()
    said.note("connor", "web_user", "m123456", kind=said.EMO_KIND)
    said.note("connor", "web_user", "m123456", kind=said.EMO_KIND)
    items, _ = said.take(c0, "web_user")
    check("без emo эмоции не отдаются (старый клиент не покажет id облачком)", items == [])
    items, _ = said.take(c0, "web_user", acts=True)
    check("и клиенту только с acts — тоже", items == [])
    items, _ = said.take(c0, "web_user", emo=True)
    check("клиенту с emo — обе (событие, не повтор)", [m["kind"] for m in items] == ["emo", "emo"])
    items, _ = said.take(0, "web_user", fresh_sec=60, emo=True)
    check("после перезапуска ядра не пересказываются", items == [])
    check("слушатель emo не отмечен", not said.emo_listener_recent(60, "web_user"))
    said.mark_emo_listener("web_user")
    check("отмечен — и только в своём чате", said.emo_listener_recent(60, "web_user")
          and not said.emo_listener_recent(60, "other"))

    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (маршруты пропущены — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    client = TestClient(server_mod.app, base_url="http://127.0.0.1")
    r = client.put("/api/personas/connor/portrait/emotions/new", json={"name": "злость", "hint": "сердится"})
    emo = r.json().get("created") if r.status_code == 200 else None
    check("PUT emotions/new — 200 и id", bool(emo) and r.json()["emotions"][-1]["name"] == "злость")
    r = client.post(f"/api/personas/connor/portrait/emotions/{emo}/show")
    check("показать эмоцию без основного листа — 404", r.status_code == 404)
    r = client.put(f"/api/personas/connor/portrait/{emo}.A", json={"data_url": data_url(make_sheet()), "rows": 5, "cols": 5})
    check("PUT листа эмоции — 200", r.status_code == 200 and "A" in r.json()["emotions"][-1]["sheets"])
    c0 = said.cursor()
    r = client.post(f"/api/personas/connor/portrait/emotions/{emo}/show")
    items, _ = said.take(c0, "web_user", emo=True)
    check("показать — эмоция в ленте", r.status_code == 200 and [m["text"] for m in items if m["kind"] == "emo"] == [emo])
    r = client.get("/api/said?emo=1")
    check("GET /api/said?emo=1 — отмечает слушателя", r.status_code == 200 and said.emo_listener_recent(5, "web_user"))
    r = client.put("/api/personas/connor/portrait/emotions/new", json={"name": ""})
    check("пустое имя — 422", r.status_code == 422)
    r = client.delete(f"/api/personas/connor/portrait/emotions/{emo}")
    check("DELETE эмоции — 200", r.status_code == 200 and all(e["id"] != emo for e in r.json()["emotions"]))
    client.delete("/api/personas/connor/portrait")


def test_marker():
    section("F. Маркер [EMO:имя] в ответе")
    import app.bot_instance as bi
    cut, names = bi.BotInstance._cut_markers(bi._EMO_MARK_RE, "Ну ты даёшь! [emo:Злость]")
    check("маркер срезан, имя взято (и [emo:…] строчными)", cut.strip() == "Ну ты даёшь!" and names == ["Злость"])
    cut, names = bi.BotInstance._cut_markers(bi._ACT_MARK_RE, "Привет [EMO:радость] [ACT:машет]")
    cut, emos = bi.BotInstance._cut_markers(bi._EMO_MARK_RE, cut)
    check("вместе с [ACT:…] — оба срезаны", cut.strip() == "Привет" and names == ["машет"] and emos == ["радость"])

    # Нота и показ — как у бота веб-персоны (контекст api_<id>)
    from app.api import portrait_api as pa, said

    class Bot:
        context = "api_portrait_m"
        _act_persona = bi.BotInstance._act_persona
        _emo_note = bi.BotInstance._emo_note
        _show_emotion = bi.BotInstance._show_emotion

    bot = Bot()
    emo = pa.put_emotion("portrait_m", "new", "Злость", "когда сердится")["created"]
    pa.put_sheet("portrait_m", f"{emo}.A", data_url(make_sheet()), 5, 5)
    said._emo_seen.clear()
    check("живое лицо ленту не слушало — ноты нет", bot._emo_note("u1", "chat_m") is None)
    said.mark_emo_listener("chat_m")
    note = bot._emo_note("u1", "chat_m") or ""
    check("слушало — нота с эмоцией и маркером", "Злость (когда сердится)" in note and "[EMO:name]" in note)
    check("другой чат — без ноты", bot._emo_note("u1", "chat_other") is None)
    c0 = said.cursor()
    bot._show_emotion(["нет такой", "злость"], "u1", "chat_m")
    items, _ = said.take(c0, "chat_m", emo=True)
    check("маркер → эмоция в ленту чата (первое узнанное имя)",
          [(m["persona"], m["kind"], m["text"]) for m in items] == [("portrait_m", "emo", emo)])
    c0 = said.cursor()
    bot._show_emotion(["нет такой"], "u1", "chat_m")
    check("неизвестное имя — ничего", said.take(c0, "chat_m", emo=True)[0] == [])
    pa.delete_all("portrait_m")


def test_mood():
    section("G. Эмоции по настроению")
    import json
    from types import SimpleNamespace
    from app.api import portrait_api as pa, room_api
    from app.core import room
    import app.bot_instance as bi
    import app.core.living_persona as lp
    from app.core.state_engine import StateEngine
    persona = "portrait_g"

    check("не словарь — условия нет", pa.clean_mood(None) is None and pa.clean_mood("bad") is None)
    check("пустое условие — есть (только по имени)", pa.clean_mood({}) == {})
    got = pa.clean_mood({"valence": "bad", "arousal": "sleepy",
                         "words": [" тоска ", "Тоска", "[обид*]", "", 5] + [f"w{i}" for i in range(10)]})
    check("зона: чужое значение выброшено; слова без скобок, без повторов, не больше 8",
          got == {"valence": "bad", "words": ["тоска", "обид*", "5", "w0", "w1", "w2", "w3", "w4"]})

    pa.put_sheet(persona, "A", data_url(make_sheet()), 5, 5)
    sad = pa.put_emotion(persona, "new", "Грусть", "когда грустит")["created"]
    jealous = pa.put_emotion(persona, "new", "Ревность", "")["created"]
    marker_only = pa.put_emotion(persona, "new", "Смех", "когда смешно")["created"]
    p = pa.put_emotion(persona, sad, "Грусть", "когда грустит", mood={"valence": "bad", "words": ["тоска"]})
    e = next(x for x in p["emotions"] if x["id"] == sad)
    check("условие сохранено и отдаётся с портретом", e["mood"] == {"valence": "bad", "words": ["тоска"]})
    p = pa.put_emotion(persona, sad, "Грусть!", "когда грустит")
    e = next(x for x in p["emotions"] if x["id"] == sad)
    check("правка без mood — условие прежнее", e["name"] == "Грусть!" and e["mood"] == {"valence": "bad", "words": ["тоска"]})
    pa.put_emotion(persona, jealous, "Ревность", "", mood={})
    p = pa.get_portrait(persona)
    check("без условия — поля mood нет (только маркер)",
          "mood" not in next(x for x in p["emotions"] if x["id"] == marker_only))
    check("эмоции-настроения — и без своих листов", [x["id"] for x in pa.mood_emotions(persona)] == [sad, jealous])
    check("строка для промптов: имя — когда; имя", pa.mood_names(persona) == "Грусть! — когда грустит; Ревность")
    p = pa.put_emotion(persona, jealous, "Ревность", "", mood=None)
    check("mood=None — условие снято", "mood" not in next(x for x in p["emotions"] if x["id"] == jealous)
          and pa.mood_names(persona) == "Грусть! — когда грустит")
    check("нет персоны / битый ключ — пусто", pa.mood_emotions("../x") == [] and pa.mood_names("nobody_here") is None)

    # Настроение персоны сейчас — из файлов живого состояния, как у «Комнаты»
    live = {"features": {"state_engine": True}}
    check("живое состояние выключено — None", room_api.current_mood(persona, {}) is None)
    check("состояния ещё нет — None", room_api.current_mood(persona, live) is None)
    d = room.living_dir(f"api_{persona}")
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"chats": {"web_user": {
        "mood": {"valence": -0.62, "arousal": 7, "tag": "тоска", "trend": "down"}, "pose": "sit"}}}),
        encoding="utf-8")
    check("есть — valence/arousal (в пределах) и тег",
          room_api.current_mood(persona, live) == {"valence": -0.62, "arousal": 1.0, "tag": "тоска"})

    # Тик состояния: имена своих эмоций — в правилах тега
    fake = SimpleNamespace(primitive=False)
    prev = {"energy": 70, "mood": {"valence": 0, "arousal": 0.3, "tag": "calm"}}
    base = StateEngine._build_tick_prompt_base(fake, prev, {}, [], "")
    own = StateEngine._build_tick_prompt_base(fake, prev, {"own_emotions": "Грусть — когда грустит"}, [], "")
    check("тик: без своих эмоций — без строки", "own named emotions" not in base)
    check("тик: свои эмоции — mood.tag их точным именем",
          own.startswith(base) and "Грусть — когда грустит" in own and "mood.tag is its exact name" in own)

    # Бот отдаёт строку живой персоне (контекст api_<id> и Telegram <id>)
    class Bot:
        context = f"api_{persona}"
        _own_emotions = bi.BotInstance._own_emotions
    check("бот: строка эмоций аватара чата", Bot()._own_emotions("web_user") == "Грусть! — когда грустит")
    Bot.context = persona
    check("бот Telegram-контекста — те же эмоции", Bot()._own_emotions("123") == "Грусть! — когда грустит")

    # Урожай диалога: строка в промпте, тег — в состояние
    seen, impacts = [], []

    class Local:
        def is_available(self, task=None):
            return True

        def get_response(self, messages, **kw):
            seen.append(messages[-1]["content"])
            return json.dumps({"mood_impact": {"valence_delta": -0.2, "tag": "Грусть!"}})

    living = SimpleNamespace(
        context="api_portrait_g",
        persona=SimpleNamespace(persona_name="Тест"),
        persona_context=lambda: {"personality_summary": "тест"},
        config=SimpleNamespace(world_enabled=False, state_enabled=True),
        relationship=SimpleNamespace(known_lists=lambda cid: ("(none)", "(none)", "(none)"),
                                     add_extracted=lambda *a: None),
        chat_language=lambda cid: "ru",
        primitive=False,
        get_own_emotions=lambda cid: "Грусть! — когда грустит" if cid == "c1" else None,
        state_engine=SimpleNamespace(apply_mood_impact=lambda cid, d, tag: impacts.append((cid, d, tag))),
    )
    living.own_emotions = lambda cid: lp.LivingPersona.own_emotions(living, cid)
    orig = lp.get_local_router
    lp.get_local_router = lambda ctx: Local()
    try:
        lines = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"реплика {i}"} for i in range(6)]
        lp.LivingPersona._harvest_dialogue(living, "c1", lines)
        lp.LivingPersona._harvest_dialogue(living, "c2", lines)
    finally:
        lp.get_local_router = orig
    check("урожай: свои эмоции в правилах тега", len(seen) == 2 and "Грусть! — когда грустит" in seen[0]
          and "mood_impact.tag is its exact name" in seen[0])
    check("урожай: у чата без своих эмоций — без строки", "own named emotions" not in seen[1])
    check("урожай: тег-эмоция — в состояние", impacts and impacts[0] == ("c1", -0.2, "Грусть!"))
    living.primitive = True
    check("примитив — без своих эмоций", lp.LivingPersona.own_emotions(living, "c1") is None)
    living.primitive = False
    living.get_own_emotions = lambda cid: 1 / 0
    check("поломка источника — без строки, без исключения", lp.LivingPersona.own_emotions(living, "c1") is None)
    pa.delete_all(persona)

    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (маршруты пропущены — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    client = TestClient(server_mod.app, base_url="http://127.0.0.1")
    emo = client.put("/api/personas/connor/portrait/emotions/new", json={"name": "тоска"}).json()["created"]
    r = client.put(f"/api/personas/connor/portrait/emotions/{emo}",
                   json={"name": "тоска", "hint": "", "mood": {"valence": "bad", "arousal": "calm", "words": ["груст*"]}})
    got = next(e for e in r.json()["emotions"] if e["id"] == emo)
    check("PUT с mood — условие сохранено", r.status_code == 200
          and got.get("mood") == {"valence": "bad", "arousal": "calm", "words": ["груст*"]})
    r = client.put(f"/api/personas/connor/portrait/emotions/{emo}", json={"name": "тоска", "hint": "x"})
    got = next(e for e in r.json()["emotions"] if e["id"] == emo)
    check("PUT без поля mood — условие прежнее", got.get("mood", {}).get("valence") == "bad" and got["hint"] == "x")
    r = client.put(f"/api/personas/connor/portrait/emotions/{emo}", json={"name": "тоска", "hint": "x", "mood": None})
    check("PUT mood: null — снято", "mood" not in next(e for e in r.json()["emotions"] if e["id"] == emo))
    r = client.put(f"/api/personas/connor/portrait/emotions/{emo}", json={"name": "тоска", "mood": {"valence": "meh"}})
    check("чужая зона — 422", r.status_code == 422)
    r = client.get("/api/personas/connor/portrait/mood")
    check("GET …/portrait/mood — 200 и поле mood", r.status_code == 200 and "mood" in r.json())
    r = client.get("/api/personas/no_such_persona/portrait/mood")
    check("нет персоны — 404", r.status_code == 404)
    client.delete("/api/personas/connor/portrait")


def main():
    test_build()
    test_store()
    test_routes()
    test_emotions()
    test_emotion_feed()
    test_marker()
    test_mood()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Тест анимаций портрета на рабочем столе (app/api/portrait_anim.py): свои
листы кадров → атлас, ротация и события, персона выбирает сама.

  - нарезка: атлас cols×rows кадров, длинная сторона кадра ANIM_PX, пропорции
    клетки; сплошной фон прозрачный; реквизит, не касающийся края клетки,
    остаётся, обрывок соседнего кадра у края и крошки — нет; grid сохраняет
    прыжок (кадр выше остальных), feet ставит каждый кадр на низ и по центру
    масс; пустые клетки в конце — filled меньше; лист с прозрачностью; keep;
  - хранение: новая (id от ядра), правка полей без переразметки, переразметка
    по .src, кадров авто/вручную, удаление, настройки ротации, ошибки
    400/404/413, битый anims.json;
  - персона выбирает сама: нота только с llm-анимациями, имя маркера без
    регистра, BotInstance режет [ACT:…] и шлёт act в ленту; нота — только
    пока портрет слушает ленту (acts=1);
  - лента реплик: act — только клиентам с acts, без повторов и пересказа;
  - маршруты: GET/PUT/DELETE/play, anims-settings (не уходит в PUT листа),
    /api/portraits → anims, /api/said?acts=1, 401 без токена, 404 без персоны.

LLM, Ollama и сеть не вызываются, data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_portrait_anim
"""

import base64
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="portrait_anim_")

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


BG = (120, 180, 120)  # «хромакей»
CW, CH = 100, 150  # клетка листа: выше, чем шире


def make_sheet(rows=2, cols=3, jump=None, prop=False, intruder=False, speck=False,
               empty_tail=0, transparent=False, drift=False) -> bytes:
    """Лист кадров: в каждой клетке «человечек» (голова + туловище), ноги у
    низа клетки. jump — номер кадра, где он выше на 30 px; prop — книга в
    воздухе, не касается фигуры и края; intruder — обрывок у правого края;
    speck — точка 1×1; empty_tail — сколько последних клеток пустые;
    drift — фигура в каждом кадре сдвинута по x на 6·i px."""
    img = Image.new("RGBA", (cols * CW, rows * CH), (0, 0, 0, 0) if transparent else BG + (255,))
    d = ImageDraw.Draw(img)
    n = rows * cols
    for i in range(n - empty_tail):
        r, c = divmod(i, cols)
        x0, y0 = c * CW, r * CH
        dy = -30 if i == jump else 0
        dx = 6 * i - 15 if drift else 0
        d.ellipse([x0 + 35 + dx, y0 + 40 + dy, x0 + 65 + dx, y0 + 70 + dy], fill=(220, 180, 150, 255))
        d.rectangle([x0 + 38 + dx, y0 + 70 + dy, x0 + 62 + dx, y0 + 140 + dy], fill=(40, 40, 160, 255))
        if prop:
            d.rectangle([x0 + 70, y0 + 20, x0 + 85, y0 + 32], fill=(180, 40, 40, 255))
        if intruder:
            d.rectangle([x0 + 92, y0 + 60, x0 + 99, y0 + 80], fill=(30, 30, 30, 255))
        if speck:
            d.point([x0 + 10, y0 + 10], fill=(0, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def data_url(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def frame_box(a: np.ndarray, i: int, cols: int, fw: int, fh: int):
    """(верх, низ, лево, право) непрозрачного в кадре i атласа или None."""
    r, c = divmod(i, cols)
    solid = a[r * fh:(r + 1) * fh, c * fw:(c + 1) * fw, 3] > 128
    if not solid.any():
        return None
    ys, xs = np.nonzero(solid)
    return int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())


def solid_count(a, i, cols, fw, fh):
    r, c = divmod(i, cols)
    return int((a[r * fh:(r + 1) * fh, c * fw:(c + 1) * fw, 3] > 128).sum())


def test_build():
    section("A. Нарезка листа кадров")
    from app.api.portrait_anim import ANIM_PX, build_frames
    atlas, fw, fh, filled = build_frames(make_sheet(), 2, 3)
    a = np.asarray(atlas)
    check("кадр — длинной стороной ANIM_PX, пропорции клетки",
          fh == ANIM_PX and fw == round(ANIM_PX * CW / CH))
    check("атлас cols×rows кадров", a.shape[:2] == (2 * fh, 3 * fw))
    check("все 6 клеток непустые", filled == 6)
    box = frame_box(a, 4, 3, fw, fh)
    check("фигура — низом к низу кадра, по середине",
          box is not None and box[1] >= fh - 3 and abs((box[2] + box[3]) / 2 - fw / 2) <= 3)
    check("фон прозрачный (углы кадра)", a[2, 2, 3] == 0 and a[fh - 1, fw - 1, 3] == 0)
    check("фигура почти во весь кадр (масштаб по ней, запас FIT)",
          box[1] - box[0] > fh * 0.85)

    a = np.asarray(build_frames(make_sheet(jump=2), 2, 3)[0])
    b0, b2 = frame_box(a, 0, 3, fw, fh), frame_box(a, 2, 3, fw, fh)
    check("grid: прыжок сохранился (кадр 2 выше остальных)", b2[1] < b0[1] - 20 and b0[1] >= fh - 3)
    atlas, fw2, fh2, _ = build_frames(make_sheet(jump=2), 2, 3, align="feet")
    a = np.asarray(atlas)
    b2 = frame_box(a, 2, 3, fw2, fh2)
    check("feet: каждый кадр — ногами к низу (прыжка нет)", b2[1] >= fh2 - 3)

    a = np.asarray(build_frames(make_sheet(drift=True), 2, 3, align="feet")[0])
    mids = [(frame_box(a, i, 3, fw, fh)[2] + frame_box(a, i, 3, fw, fh)[3]) / 2 for i in range(6)]
    check("feet: дрожание генератора погашено (середины кадров совпадают)", max(mids) - min(mids) <= 3)
    a = np.asarray(build_frames(make_sheet(drift=True), 2, 3)[0])
    mids = [(frame_box(a, i, 3, fw, fh)[2] + frame_box(a, i, 3, fw, fh)[3]) / 2 for i in range(6)]
    check("grid: сдвиг по кадрам сохранён (как на листе)", max(mids) - min(mids) > 20)

    plain = np.asarray(build_frames(make_sheet(), 2, 3)[0])
    with_prop = np.asarray(build_frames(make_sheet(prop=True), 2, 3)[0])
    from scipy import ndimage
    r1 = with_prop[0:fh, fw:2 * fw, 3] > 128
    check("реквизит (книга в воздухе, края не касается) остался отдельным куском",
          ndimage.label(r1)[1] == 2)
    a = np.asarray(build_frames(make_sheet(intruder=True, speck=True), 2, 3)[0])
    box = frame_box(a, 1, 3, fw, fh)
    check("обрывок соседнего кадра у края и крошка — убраны",
          box is not None and abs((box[2] + box[3]) / 2 - fw / 2) <= 3
          and abs(solid_count(a, 1, 3, fw, fh) - solid_count(plain, 1, 3, fw, fh)) < 40)

    _, _, _, filled = build_frames(make_sheet(empty_tail=2), 2, 3)
    check("пустые клетки в конце — filled 4", filled == 4)
    a = np.asarray(build_frames(make_sheet(transparent=True), 2, 3)[0])
    check("лист с прозрачностью — фигуры на месте, фон прозрачный",
          frame_box(a, 0, 3, fw, fh) is not None and a[2, 2, 3] == 0)
    a = np.asarray(build_frames(make_sheet(), 2, 3, bg="keep")[0])
    check("keep — фон оставлен (угол кадра непрозрачный)",
          a[fh // 2, 1, 3] == 255 or a[fh - 1, fw // 2, 3] == 255)

    from app.api.portrait_api import PortraitError
    try:
        build_frames(make_sheet(), 12, 12, offset_x=200)
        check("сетка не помещается — 400", False)
    except PortraitError as e:
        check("сетка не помещается — 400", e.status == 400)
    empty = io.BytesIO()
    Image.new("RGBA", (300, 300), BG + (255,)).save(empty, "PNG")
    try:
        build_frames(empty.getvalue(), 2, 2)
        check("пустой лист — 400", False)
    except PortraitError as e:
        check("пустой лист — 400", e.status == 400 and "фигуры" in e.detail)


def upd(**kw):
    base = {"name": "Ест", "rows": 2, "cols": 3, "fps": 8, "loops": 1, "scale": 1.0, "align": "grid",
            "bg": "auto", "tolerance": 0, "offset_x": 0, "offset_y": 0, "faces": "none",
            "triggers": ["idle"], "weight": 1, "enabled": True, "frames": None}
    base.update(kw)
    return base


def test_store():
    section("B. Хранение")
    from app.api import portrait_anim as pa
    from app.api.room_api import RoomError
    P = "anim_store"
    got = pa.get_anims(P)
    check("нет анимаций — пустой список и настройки по умолчанию",
          got["anims"] == [] and got["settings"] == {"every_min": 4, "walk": True, "sleep_anim": None} and got["updated_at"] is None)
    check("updated_at/has_anims без файла — None/False", pa.updated_at(P) is None and not pa.has_anims(P))

    res = pa.put_anim(P, "new", upd(data_url=data_url(make_sheet(empty_tail=1)), triggers=["idle", "llm"]))
    check("новая — одна, id 8 hex, атлас data-URL webp",
          len(res["anims"]) == 1 and len(res["anims"][0]["id"]) == 8
          and res["anims"][0]["atlas"].startswith("data:image/webp;base64,"))
    a = res["anims"][0]
    aid = a["id"]
    check("кадров авто — до последней непустой (5)", a["frames"] == 5 and a["filled"] == 5)
    check("файлы на месте (.webp, .src, anims.json)",
          all((pa.anims_dir(P) / f).is_file() for f in (f"{aid}.webp", f"{aid}.src", "anims.json")))
    check("has_anims/updated_at", pa.has_anims(P) and isinstance(pa.updated_at(P), float))

    webp0 = (pa.anims_dir(P) / f"{aid}.webp").stat().st_mtime_ns
    time.sleep(0.01)
    res = pa.put_anim(P, aid, upd(name="  Ест суп  ", fps=50, loops=0, scale=9, weight=99,
                                  faces="right", triggers=["walk", "idle"], frames=3))
    a = res["anims"][0]
    check("правка полей: имя обрезано, числа прижаты к пределам",
          a["name"] == "Ест суп" and a["fps"] == 30 and a["loops"] == 1 and a["scale"] == 3.0 and a["weight"] == 10)
    check("триггеры — в порядке TRIGGERS", a["triggers"] == ["idle", "walk"])
    check("кадров вручную — 3", a["frames"] == 3)
    check("клавиш по умолчанию нет", a["keys"] == [])
    res = pa.put_anim(P, aid, upd(name="Ест суп", keys=["Ctrl+Shift+KeyK", "MouseBack", "Space", "Space"]))
    check("клавиши: сочетания записаны, повтор убран",
          res["anims"][0]["keys"] == ["Ctrl+Shift+KeyK", "MouseBack", "Space"])
    res = pa.put_anim(P, aid, upd(name="Ест суп", fps=12))
    check("keys не прислали (старая вкладка) — как было",
          res["anims"][0]["keys"] == ["Ctrl+Shift+KeyK", "MouseBack", "Space"])
    res = pa.put_anim(P, aid, upd(name="Ест суп", keys=[]))
    check("keys [] — клавиши сняты", res["anims"][0]["keys"] == [])
    check("_keys: кривые и сверх 8 отброшены",
          pa._keys(["Shift+Ctrl+KeyA", "ctrl+KeyA", "Ctrl+", "KeyA", 5, "Meta+MouseLeft"] + [f"F{i}" for i in range(1, 12)])
          == ["KeyA", "Meta+MouseLeft", "F1", "F2", "F3", "F4", "F5", "F6"])
    check("без смены сетки/фона лист не режется заново",
          (pa.anims_dir(P) / f"{aid}.webp").stat().st_mtime_ns == webp0)
    res = pa.put_anim(P, aid, upd(align="feet"))
    check("сменилось выравнивание — лист режется заново по .src",
          (pa.anims_dir(P) / f"{aid}.webp").stat().st_mtime_ns != webp0 and res["anims"][0]["align"] == "feet")
    check("кадров снова авто (frames null)", res["anims"][0]["frames"] == 5)
    res = pa.put_anim(P, aid, upd(rows=1, cols=3))
    a = res["anims"][0]
    check("другая сетка — переразметка, кадры по новой сетке", a["rows"] == 1 and a["filled"] == 3)

    def err(fn, *args):
        try:
            fn(*args)
        except RoomError as e:
            return e.status
        return None

    check("без названия — 400", err(pa.put_anim, P, aid, upd(name="  ")) == 400)
    check("сетка 13×1 — 400", err(pa.put_anim, P, aid, upd(rows=13)) == 400)
    check("неизвестный повод — 400", err(pa.put_anim, P, aid, upd(triggers=["dance"])) == 400)
    check("выравнивание не grid/feet — 400", err(pa.put_anim, P, aid, upd(align="x")) == 400)
    check("кривая клавиша — 400", err(pa.put_anim, P, aid, upd(keys=["Shift+Ctrl+KeyA"])) == 400)
    check("клавиш больше 8 — 400", err(pa.put_anim, P, aid, upd(keys=[f"F{i}" for i in range(1, 10)])) == 400)
    check("новая без картинки — 400", err(pa.put_anim, P, "new", upd()) == 400)
    check("чужой id — 404", err(pa.put_anim, P, "deadbeef", upd()) == 404)
    check("кривой id — 404", err(pa.put_anim, P, "../x", upd()) == 404)
    big = "data:image/png;base64," + "A" * (17 * 1024 * 1024 * 4 // 3)
    check("лист больше 16 МБ — 413", err(pa.put_anim, P, "new", upd(data_url=big)) == 413)

    res = pa.put_settings(P, 99, False)
    check("настройки ротации: every_min прижат к 60, walk false",
          res["settings"] == {"every_min": 60, "walk": False, "sleep_anim": None})
    res = pa.put_settings(P, -3, True)
    check("every_min < 0 → 0 (ротации нет)", res["settings"]["every_min"] == 0)

    res = pa.put_settings(P, 2, True, aid)
    check("сон — сохраняется выбранная анимация", res["settings"]["sleep_anim"] == aid
          and pa.get_anims(P)["settings"]["sleep_anim"] == aid)
    check("старый клиент не сбрасывает сон", pa.put_settings(P, 3, False)["settings"]["sleep_anim"] == aid)
    check("неверный id сна — 400", err(pa.put_settings, P, 3, False, "../bad") == 400)
    check("нет анимации сна — 404", err(pa.put_settings, P, 3, False, "deadbeef") == 404)
    check("ошибки не меняют выбранный сон", pa.get_anims(P)["settings"]["sleep_anim"] == aid)
    check("null сбрасывает сон", pa.put_settings(P, 3, False, None)["settings"]["sleep_anim"] is None)
    pa.put_settings(P, 3, False, aid)

    light = pa.get_anims(P, atlas=False)
    check("atlas=False — без атласов", "atlas" not in light["anims"][0])

    res = pa.delete_anim(P, aid)
    check("удаление выбранной анимации очищает сон", res["settings"]["sleep_anim"] is None)
    check("удаление — пусто, файлов нет",
          res["anims"] == [] and not (pa.anims_dir(P) / f"{aid}.webp").exists()
          and not (pa.anims_dir(P) / f"{aid}.src").exists())
    check("удалить ещё раз — 404", err(pa.delete_anim, P, aid) == 404)

    (pa.anims_dir(P) / "anims.json").write_text("{broken", encoding="utf-8")
    check("битый anims.json — пусто, не падает", pa.get_anims(P)["anims"] == [])
    (pa.anims_dir(P) / "anims.json").write_text(json.dumps(
        {"anims": [{"id": "zz"}, {"id": "0123abcd", "name": "x", "rows": 99}], "settings": {"every_min": "a"}}),
        encoding="utf-8")
    got = pa._read(P)
    check("кривые записи отброшены, числа прижаты",
          [x["id"] for x in got["anims"]] == ["0123abcd"] and got["anims"][0]["rows"] == 12
          and got["settings"]["every_min"] == 4)


def wav_bytes(n=800) -> bytes:
    import struct
    pcm = bytes(n * 2)
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def test_links_sound():
    section("B2. Связки и звук")
    from app.api import portrait_anim as pa
    from app.api.room_api import RoomError
    P = "anim_links"

    def err(fn, *args):
        try:
            fn(*args)
        except RoomError as e:
            return e.status
        return None

    sheet = data_url(make_sheet())
    ids = []
    for name in ("Падает", "Встаёт", "Машет"):
        res = pa.put_anim(P, "new", upd(name=name, data_url=sheet))
        ids.append(next(a["id"] for a in res["anims"] if a["name"] == name))
    fall, up, wave = ids
    a = next(x for x in res["anims"] if x["id"] == wave)
    check("по умолчанию: связок нет, каждый раз, без звука, громкость 80",
          a["after"] == [] and a["before"] == [] and a["chance"] == 100 and a["sound"] is None
          and a["sound_name"] == "" and a["volume"] == 80 and a["sound_loop"] is False and "sound_url" not in a)

    res = pa.put_anim(P, up, upd(name="Встаёт", triggers=[], after=[fall, wave, fall, "deadbeef", up], chance=0))
    a = next(x for x in res["anims"] if x["id"] == up)
    check("после: свои и чужие, без повтора, без себя и несуществующих",
          a["after"] == [fall, wave])
    check("chance прижат к 1..100", a["chance"] == 1)
    res = pa.put_anim(P, up, upd(name="Встаёт", triggers=[], before=[wave], chance=40))
    a = next(x for x in res["anims"] if x["id"] == up)
    check("after не прислали — как было; before и chance записаны",
          a["after"] == [fall, wave] and a["before"] == [wave] and a["chance"] == 40)
    res = pa.put_anim(P, up, upd(name="Встаёт", triggers=[]))
    a = next(x for x in res["anims"] if x["id"] == up)
    check("chance не прислали (старая вкладка) — как было", a["chance"] == 40)
    check("связка не списком — 400", err(pa.put_anim, P, up, upd(after="x")) == 400)
    check("кривой id в связке — 400", err(pa.put_anim, P, up, upd(before=["../x"])) == 400)
    check("связка длиннее 12 — 400", err(pa.put_anim, P, up, upd(after=[f"{i:08x}" for i in range(13)])) == 400)

    wav = wav_bytes()
    url = "data:audio/wav;base64," + base64.b64encode(wav).decode()
    res = pa.put_anim(P, up, upd(name="Встаёт", triggers=[], sound_url=url, sound_name="ох\n.wav", volume=150, sound_loop=True))
    a = next(x for x in res["anims"] if x["id"] == up)
    check("звук: MIME по сигнатуре, имя без управляющих, громкость прижата, по кругу",
          a["sound"] == "audio/wav" and a["sound_name"] == "ох.wav" and a["volume"] == 100 and a["sound_loop"] is True)
    check("звук: файл на диске и sound_url в ответе",
          (pa.anims_dir(P) / f"{up}.sound").read_bytes() == wav and a["sound_url"] == url)
    check("atlas=False — без sound_url, но поле sound есть",
          next(x for x in pa.get_anims(P, atlas=False)["anims"] if x["id"] == up).get("sound_url") is None)
    res = pa.put_anim(P, up, upd(name="Встаёт", triggers=[], volume=30))
    a = next(x for x in res["anims"] if x["id"] == up)
    check("sound_url не прислали — звук как был, громкость новая", a["sound"] == "audio/wav" and a["volume"] == 30
          and a["sound_loop"] is True)
    fake = "data:audio/mpeg;base64," + base64.b64encode(b"<html>not audio</html>").decode()
    check("не звук (сигнатура) — 400", err(pa.put_anim, P, up, upd(sound_url=fake)) == 400)
    big = "data:audio/wav;base64," + "A" * (3 * 1024 * 1024 * 4 // 3)
    check("звук больше 2 МБ — 413", err(pa.put_anim, P, up, upd(sound_url=big)) == 413)
    check("не data-URL — 400", err(pa.put_anim, P, up, upd(sound_url="http://x/y.mp3")) == 400)
    check("сигнатуры: mp3 (ID3 и кадр), aac, ogg, flac, m4a, webm",
          [pa.sniff_sound(b) for b in (b"ID3\x03", b"\xff\xfb\x90", b"\xff\xf1\x50", b"OggS\x00", b"fLaC\x00",
                                        b"\x00\x00\x00\x20ftypM4A ", b"\x1a\x45\xdf\xa3\x01", b"\xff\xe0")]
          == ["audio/mpeg", "audio/mpeg", "audio/aac", "audio/ogg", "audio/flac", "audio/mp4", "audio/webm", None])
    (pa.anims_dir(P) / f"{up}.sound").unlink()
    a = next(x for x in pa.get_anims(P)["anims"] if x["id"] == up)
    check("файл звука потеряли — без звука", a["sound"] is None and "sound_url" not in a)
    res = pa.put_anim(P, up, upd(name="Встаёт", triggers=[], sound_url=url, sound_name="ох.wav"))
    res = pa.put_anim(P, up, upd(name="Встаёт", triggers=[], sound_url=""))
    a = next(x for x in res["anims"] if x["id"] == up)
    check("sound_url \"\" — звук убран и файла нет",
          a["sound"] is None and a["sound_name"] == "" and not (pa.anims_dir(P) / f"{up}.sound").exists())

    res = pa.put_anim(P, up, upd(name="Встаёт", triggers=[], sound_url=url))
    pa.put_cut(P, up, {"rows": 2, "cols": 3}, None, {"fps": 10})
    a = next(x for x in pa.get_anims(P)["anims"] if x["id"] == up)
    check("правка в «Кадрах» связки и звук не трогает",
          a["after"] == [fall, wave] and a["before"] == [wave] and a["sound"] == "audio/wav" and a["fps"] == 10)
    res = pa.delete_anim(P, wave)
    a = next(x for x in res["anims"] if x["id"] == up)
    check("удалили «Машет» — из связок других пропала", a["after"] == [fall] and a["before"] == [])
    res = pa.delete_anim(P, up)
    check("удалили со звуком — файла звука нет", not (pa.anims_dir(P) / f"{up}.sound").exists())


def test_llm():
    section("C. Персона выбирает сама")
    from app.api import portrait_anim as pa
    from app.api import said
    from app.bot_instance import _ACT_MARK_RE, BotInstance
    P = "anim_llm"
    check("нет анимаций — ноты нет", pa.body_note(P) is None)
    sheet = data_url(make_sheet())
    eat = pa.put_anim(P, "new", upd(name="Ест", data_url=sheet, triggers=["llm"]))["anims"][-1]["id"]
    pa.put_anim(P, "new", upd(name="Танцует", data_url=sheet, triggers=["idle"]))
    off = pa.put_anim(P, "new", upd(name="Спит", data_url=sheet, triggers=["llm"], enabled=False))["anims"][-1]["id"]
    read = pa.put_anim(P, "new", upd(name="Read a book", data_url=sheet, triggers=["llm", "idle"]))["anims"][-1]["id"]
    note = pa.body_note(P) or ""
    check("нота: только включённые llm-анимации",
          "Ест" in note and "Read a book" in note and "Танцует" not in note and "Спит" not in note)
    check("нота по-английски, с форматом маркера", note.startswith("You have") and "[ACT:name]" in note)
    check("имя маркера — без регистра и лишних пробелов",
          pa.match_act(P, "  read   A BOOK ") == read and pa.match_act(P, "ест") == eat)
    check("выключенная и не-llm — не находятся", pa.match_act(P, "Спит") is None and pa.match_act(P, "Танцует") is None)
    check("off-анимация на месте, но выбрать нельзя", pa.find(P, off) is not None)

    text, names = BotInstance._cut_markers(_ACT_MARK_RE, "Пойду перекушу. [ACT:Ест]")
    check("маркер в конце вырезан", text == "Пойду перекушу." and names == ["Ест"])
    text, names = BotInstance._cut_markers(_ACT_MARK_RE, "Ладно [act: read a book] почитаю.\n[ACT:Ест]")
    check("[act:…] в середине и на своей строке — вырезаны оба",
          text.strip() == "Ладно почитаю." and names == ["read a book", "Ест"])

    fake = SimpleNamespace(context=f"api_{P}")
    fake._act_persona = lambda: BotInstance._act_persona(fake)
    check("нота не идёт, пока портрет не слушал ленту",
          BotInstance._act_note(fake, "web_user", None) is None)
    said.mark_acts_listener("web_user")
    check("портрет слушает (acts=1) — нота идёт", "[ACT:name]" in (BotInstance._act_note(fake, "web_user", None) or ""))
    check("чужой чат — ноты нет", BotInstance._act_note(fake, "tg_1", None) is None)
    tg = SimpleNamespace(context="don")
    tg._act_persona = lambda: BotInstance._act_persona(tg)
    check("не веб-персона (Telegram) — ноты нет", BotInstance._act_note(tg, "web_user", None) is None)

    _, start = said.take(None, acts=True)
    BotInstance._play_act(fake, ["нет такой", "read a book"], "web_user", None)
    items, _ = said.take(start, "web_user", acts=True)
    check("_play_act: первое узнанное имя → act в ленту",
          [(m["persona"], m["kind"], m["text"]) for m in items] == [(P, "act", read)])
    _, start = said.take(None, acts=True)
    BotInstance._play_act(fake, ["нет такой"], "web_user", None)
    check("ничего не узнано — в ленте пусто", said.take(start, "web_user", acts=True)[0] == [])


def test_said():
    section("D. Лента реплик: act")
    from app.api import said
    _, start = said.take(None)
    said.note("don", "web_user", "a1b2c3d4", kind="act")
    said.note("don", "web_user", "a1b2c3d4", kind="act")
    said.note("don", "web_user", "Привет")
    plain, _ = said.take(start, "web_user")
    check("без acts — act не отдаётся", [m["kind"] for m in plain] == ["reply"])
    full, _ = said.take(start, "web_user", acts=True)
    check("с acts — оба act (без повторов по тексту) и реплика",
          [m["kind"] for m in full] == ["act", "act", "reply"])
    replay, _ = said.take(0, "web_user", fresh_sec=said.REPLAY_SEC, acts=True)
    check("после перезапуска ядра act не пересказывается", all(m["kind"] != "act" for m in replay))
    check("act — в EVENT_KINDS, а не в PAUSE_KINDS",
          "act" in said.EVENT_KINDS and "act" not in said.PAUSE_KINDS)
    check("acts_listener_recent по чату", said.acts_listener_recent(60, "web_user")
          and not said.acts_listener_recent(60, "never_polled"))


def test_routes():
    section("E. Маршруты")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    from app.api import said
    orig = server_mod._api_token
    server_mod._api_token = "s3cr3t"
    try:
        client = TestClient(server_mod.app, base_url="http://127.0.0.1", client=("192.168.1.20", 50000))
        auth = {"Authorization": "Bearer s3cr3t"}
        base = "/api/personas/connor/portrait/anims"
        r = client.get(base)
        check("без токена — 401", r.status_code == 401)
        r = client.get("/api/personas/no_such_persona/portrait/anims", headers=auth)
        check("нет персоны — 404", r.status_code == 404)
        r = client.put(f"{base}/new", headers=auth, json=upd(data_url=data_url(make_sheet()), triggers=["idle", "llm"]))
        check("PUT new — 200 и анимация с атласом",
              r.status_code == 200 and len(r.json()["anims"]) == 1 and r.json()["anims"][0]["atlas"])
        aid = r.json()["anims"][0]["id"]
        r = client.get(base, headers=auth)
        check("GET — та же", r.status_code == 200 and r.json()["anims"][0]["id"] == aid)
        r = client.get(f"{base}?atlas=0", headers=auth)
        check("GET atlas=0 — без атласов", r.status_code == 200 and "atlas" not in r.json()["anims"][0])
        r = client.put(f"{base}/{aid}", headers=auth, json=upd(name="Читает"))
        check("PUT правка — 200", r.status_code == 200 and r.json()["anims"][0]["name"] == "Читает")
        r = client.put(f"{base}/{aid}", headers=auth, json=upd(name=""))
        check("PUT без названия — 400", r.status_code == 400)
        wav_url = "data:audio/wav;base64," + base64.b64encode(wav_bytes()).decode()
        r = client.put(f"{base}/{aid}", headers=auth,
                       json=upd(name="Читает", sound_url=wav_url, sound_name="шорох.wav", volume=55, chance=30, after=[]))
        a = r.json()["anims"][0] if r.status_code == 200 else {}
        check("PUT со звуком и связкой — 200, звук и шанс записаны",
              a.get("sound") == "audio/wav" and a.get("sound_url") == wav_url and a.get("volume") == 55 and a.get("chance") == 30)
        r = client.put(f"{base}/{aid}", headers=auth, json=upd(name="Читает", sound_url="data:audio/wav;base64,AAAA"))
        check("PUT с не-звуком — 400", r.status_code == 400)
        r = client.put("/api/personas/connor/portrait/anims-settings", headers=auth, json={"every_min": 7, "walk": False})
        check("anims-settings — не PUT листа, 200 и настройки",
              r.status_code == 200 and r.json()["settings"] == {"every_min": 7, "walk": False, "sleep_anim": None})
        settings_url = "/api/personas/connor/portrait/anims-settings"
        r = client.put(settings_url, headers=auth, json={"every_min": 7, "walk": False, "sleep_anim": aid})
        check("API выбирает сон", r.status_code == 200 and r.json()["settings"]["sleep_anim"] == aid)
        r = client.put(settings_url, headers=auth, json={"every_min": 5, "walk": True})
        check("API без нового поля сохраняет сон", r.status_code == 200 and r.json()["settings"]["sleep_anim"] == aid)
        r = client.put(settings_url, headers=auth, json={"sleep_anim": "deadbeef"})
        check("API не принимает чужую/отсутствующую анимацию сна", r.status_code == 404)
        r = client.put(settings_url, headers=auth, json={"sleep_anim": None})
        check("API очищает сон явно через null", r.status_code == 200 and r.json()["settings"]["sleep_anim"] is None)
        r = client.get("/api/portraits", headers=auth)
        anims = (r.json().get("anims") or {}) if r.status_code == 200 else {}
        got = client.get(base, headers=auth).json()
        check("/api/portraits → anims: метка = updated_at анимаций",
              anims.get("connor") == got["updated_at"] and isinstance(anims.get("connor"), float))

        r = client.get("/api/said?wait=0", headers=auth)
        cur, epoch = r.json()["cursor"], r.json()["epoch"]
        r = client.post(f"{base}/{aid}/play", headers=auth)
        check("play — 200", r.status_code == 200 and r.json() == {"ok": True})
        r = client.post(f"{base}/deadbeef/play", headers=auth)
        check("play чужой — 404", r.status_code == 404)
        r = client.get(f"/api/said?wait=0&since={cur}&epoch={epoch}", headers=auth)
        check("/api/said без acts — act не видно", r.status_code == 200 and r.json()["messages"] == [])
        r = client.get(f"/api/said?wait=0&since={cur}&epoch={epoch}&acts=1", headers=auth)
        msgs = r.json()["messages"] if r.status_code == 200 else []
        check("/api/said?acts=1 — act с id анимации",
              [(m["persona"], m["kind"], m["text"]) for m in msgs] == [("connor", "act", aid)])
        check("опрос с acts=1 отмечен", said.acts_listener_recent(5, "web_user"))

        # Сохранили — портрет на столе перечитывает кадры сразу (событие portrait)
        def portrait_events(since, epoch):
            r = client.get(f"/api/said?wait=0&since={since}&epoch={epoch}&acts=1&chat_id=tg_7", headers=auth)
            return [(m["persona"], m["kind"], m["text"]) for m in r.json()["messages"]]
        cur = client.get("/api/said?wait=0", headers=auth).json()["cursor"]
        client.put(f"{base}/{aid}", headers=auth, json=upd(name="Читает книгу"))
        check("PUT анимации — событие portrait, во все чаты, без текста",
              portrait_events(cur, epoch) == [("connor", "portrait", "")])
        r = client.get(f"/api/said?wait=0&since={cur}&epoch={epoch}", headers=auth)
        check("без acts — события portrait не видно", r.json()["messages"] == [])
        cur = client.get("/api/said?wait=0", headers=auth).json()["cursor"]
        client.put("/api/personas/connor/portrait/anims-settings", headers=auth, json={"every_min": 5, "walk": True})
        check("anims-settings — тоже событие", portrait_events(cur, epoch) == [("connor", "portrait", "")])
        cur = client.get("/api/said?wait=0", headers=auth).json()["cursor"]
        client.put(f"{base}/{aid}", headers=auth, json=upd(name=""))
        check("ошибка записи (400) — без события", portrait_events(cur, epoch) == [])

        cur = client.get("/api/said?wait=0", headers=auth).json()["cursor"]
        r = client.delete(f"{base}/{aid}", headers=auth)
        check("DELETE — 200, пусто", r.status_code == 200 and r.json()["anims"] == [])
        check("DELETE — событие portrait", portrait_events(cur, epoch) == [("connor", "portrait", "")])
        r = client.delete(f"{base}/{aid}", headers=auth)
        check("DELETE ещё раз — 404", r.status_code == 404)
        cur = client.get("/api/said?wait=0", headers=auth).json()["cursor"]
        r = client.put("/api/personas/connor/portrait/Q", headers=auth, json={"rows": 5, "cols": 5})
        check("PUT листа портрета по-прежнему проверяет букву — 400", r.status_code == 400)
        check("и без события", portrait_events(cur, epoch) == [])
    finally:
        server_mod._api_token = orig


def main():
    test_build()
    test_store()
    test_links_sound()
    test_llm()
    test_said()
    test_routes()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

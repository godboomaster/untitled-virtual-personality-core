"""Тест аватара у режимов персоны (app/api/avatar_modes.py): свои листы
портрета и анимации у режима, переходы между режимами.

  A. Ключ и папка: «персона@режим» → data/api_<p>/portrait_modes/<режим>/,
     плохой id режима — 400, check() — режим из файлов персоны (нет — 404).
  B. Чей аватар (resolve): нет своего — обычный; свой лист A — голова
     режима и его анимации (своих нет — никаких); только анимации — голова
     обычная, анимации режима; только переходы — всё обычное.
  C. Живой набор: live_portrait/live_anims/live_state по режиму чата,
     переходы — только в нынешний режим, метки сменились вместе с режимом.
  D. Переходы: повод mode, switch_from (поле, проверка, «как было»),
     switch_anims — названный режим раньше «из любого», выключенный — нет.
  E. Режим чата без бота — из файла состояния; лента реплик: событие mode
     без текста, в свой чат, всем клиентам, без повторов; _note_mode_change
     у бота: только у веб-персоны и только когда режим сменился.
  F. Маршруты: ?mode= у листов/анимаций/кадров, ?live=1, /portrait/modes,
     /portrait/switch, /api/portraits (mode, метки), inbox (avatar_mode).

LLM, Ollama и сеть не вызываются, data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_avatar_modes
"""

import base64
import io
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="avatar_modes_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

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


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


PERSONAS = Path(tempfile.mkdtemp(prefix="am_personas_"))
_write(PERSONAS / "hero.yaml", "name: Дон\nsystem_prompt: ОБЫЧНЫЙ\nfeatures:\n  trigger_words: [дон]\n")
_write(PERSONAS / "hero_modes" / "squire.yaml",
       "name: Санчо\nsystem_prompt: САНЧО\ntriggers:\n  names: [санчо]\n")
_write(PERSONAS / "hero_modes" / "ghost.yaml",
       "name: Призрак\nsystem_prompt: ПРИЗРАК\ntriggers:\n  keywords: [бу]\n")


def _find(n):
    p = PERSONAS / f"{n}.yaml"
    return p if p.exists() else None


PATCHES = [mock.patch("app.core.addons.persona_dirs", lambda: [PERSONAS]),
           mock.patch("app.core.addons.find_persona_file", _find),
           mock.patch("app.core.persona.persona_dirs", lambda: [PERSONAS]),
           mock.patch("app.core.persona.find_persona_file", _find),
           mock.patch("app.api.runtime.persona_dirs", lambda: [PERSONAS])]
for _p in PATCHES:
    _p.start()


def head_sheet(color=(40, 40, 160)) -> str:
    # Лист 1×1: голова на зелёном фоне
    img = Image.new("RGBA", (120, 120), (120, 180, 120, 255))
    d = ImageDraw.Draw(img)
    d.ellipse([30, 20, 90, 110], fill=color + (255,))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def anim_sheet() -> str:
    img = Image.new("RGBA", (200, 100), (120, 180, 120, 255))
    d = ImageDraw.Draw(img)
    for i in range(2):
        d.rectangle([i * 100 + 35, 20, i * 100 + 65, 95], fill=(200, 60, 60, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def upd(**kw):
    base = {"name": "Машет", "rows": 1, "cols": 2, "fps": 8, "loops": 1, "scale": 1.0, "align": "grid",
            "bg": "auto", "tolerance": 0, "offset_x": 0, "offset_y": 0, "faces": "none",
            "triggers": ["idle"], "weight": 1, "enabled": True, "frames": None}
    base.update(kw)
    return base


def set_chat_mode(persona: str, mode: str, chat: str = "web_user"):
    # Включённый режим чата — как его пишет PersonaLayer (data/api_<p>/persona_mode.json)
    from app.core import persona_modes
    from app.core.paths import data_dir
    st = persona_modes.ModeState(persona_modes.state_path(data_dir(), f"api_{persona}"))
    st.put(chat, {"active": {"mode": mode, "via": "manual", "since": time.time()}} if mode else {})


def test_keys():
    section("A. Ключ и папка аватара режима")
    from app.api import avatar_modes as am
    from app.api import portrait_api as pa
    from app.api.room_api import RoomError
    from app.core.paths import data_dir
    check("ключ режима", am.key("hero", "squire") == "hero@squire" and am.key("hero", "") == "hero")
    check("split", am.split("hero@squire") == ("hero", "squire") and am.split("hero") == ("hero", ""))
    check("папка обычного — portrait", pa.portrait_dir("hero") == data_dir() / "api_hero" / "portrait")
    check("папка режима — portrait_modes/<режим>",
          pa.portrait_dir("hero@squire") == data_dir() / "api_hero" / "portrait_modes" / "squire")
    for bad in ("hero@", "hero@a/b", "hero@..", "hero@a b"):
        try:
            pa.portrait_dir(bad)
            check(f"плохой режим «{bad}» — 400", False)
        except RoomError as e:
            check(f"плохой режим «{bad}» — 400", e.status == 400)
    check("check: пусто — обычный", am.check("hero", None) == "hero" and am.check("hero", "") == "hero")
    check("check: режим есть", am.check("hero", "squire") == "hero@squire")
    try:
        am.check("hero", "nobody")
        check("check: нет режима — 404", False)
    except RoomError as e:
        check("check: нет режима — 404", e.status == 404)
    check("режимы по файлам", am.mode_ids("hero") == {"squire", "ghost"})


def test_resolve():
    section("B. Чей аватар показывать")
    from app.api import avatar_modes as am
    from app.api import portrait_anim, portrait_api
    portrait_api.put_sheet("hero", "A", head_sheet(), 1, 1)
    portrait_anim.put_anim("hero", "new", upd(data_url=anim_sheet(), name="Обычная"))
    r = am.resolve("hero", "")
    check("обычный режим — всё обычное", r == {"mode": "", "head": "hero", "anims": "hero"})
    r = am.resolve("hero", "squire")
    check("у режима ничего своего — обычный аватар", r["head"] == "hero" and r["anims"] == "hero")
    portrait_anim.put_anim("hero@squire", "new", upd(data_url=anim_sheet(), name="Переход", triggers=["mode"]))
    r = am.resolve("hero", "squire")
    check("только переход в режим — голова и анимации обычные", r["head"] == "hero" and r["anims"] == "hero")
    portrait_anim.put_anim("hero@ghost", "new", upd(data_url=anim_sheet(), name="Летает"))
    r = am.resolve("hero", "ghost")
    check("свои анимации без головы — голова обычная, анимации режима",
          r["head"] == "hero" and r["anims"] == "hero@ghost")
    portrait_api.put_sheet("hero@squire", "A", head_sheet((160, 40, 40)), 1, 1)
    r = am.resolve("hero", "squire")
    check("своя голова — голова и анимации режима (свои — только переход)",
          r["head"] == "hero@squire" and r["anims"] == "hero@squire")
    check("обычный портрет не тронут", portrait_api.has_portrait("hero")
          and portrait_api.get_portrait("hero")["sheets"]["A"] != portrait_api.get_portrait("hero@squire")["sheets"]["A"])
    ov = am.overview("hero")
    check("overview: что у кого есть",
          ov["modes"][""] == {"head": True, "anims": 1, "switches": 0}
          and ov["modes"]["squire"] == {"head": True, "anims": 0, "switches": 1}
          and ov["modes"]["ghost"] == {"head": False, "anims": 1, "switches": 0})


def test_live():
    section("C. Живой набор по режиму чата")
    from app.api import avatar_modes as am
    set_chat_mode("hero", "")
    lp = am.live_portrait("hero")
    check("обычный: листы обычные, mode и avatar пустые",
          lp["mode"] == "" and lp["avatar"] == "" and lp["sheets"]["A"])
    la = am.live_anims("hero")
    check("обычный: анимации обычные, переходов в Санчо нет",
          [a["name"] for a in la["anims"]] == ["Обычная"] and la["mode"] == "")
    st0 = am.live_state("hero")
    set_chat_mode("hero", "squire")
    check("режим чата без бота — из файла состояния", am.active_mode("hero") == "squire")
    lp = am.live_portrait("hero")
    check("Санчо: листы режима", lp["mode"] == "squire" and lp["avatar"] == "squire")
    la = am.live_anims("hero")
    check("Санчо: только переход в него (обычные — под другую фигуру)",
          [a["name"] for a in la["anims"]] == ["Переход"])
    st1 = am.live_state("hero")
    check("метки головы и анимаций сменились вместе с режимом",
          st1["head"] != st0["head"] and st1["anims"] != st0["anims"] and st1["mode"] == "squire")
    set_chat_mode("hero", "ghost")
    la = am.live_anims("hero")
    lp = am.live_portrait("hero")
    check("Призрак: голова обычная, анимации его", lp["avatar"] == "" and [a["name"] for a in la["anims"]] == ["Летает"])
    check("другой чат — свой режим", am.active_mode("hero", "other_chat") == "")
    set_chat_mode("hero", "")
    st2 = am.live_state("hero")
    check("назад в обычный — метки как были", st2["head"] == st0["head"] and st2["anims"] == st0["anims"])
    check("метка обычного — число, как до режимов", isinstance(st2["anims"], float))


def test_switch():
    section("D. Переходы")
    from app.api import avatar_modes as am
    from app.api import portrait_anim
    from app.api.room_api import RoomError
    d = portrait_anim.put_anim("hero@squire", "new", upd(data_url=anim_sheet(), name="Из Дона",
                                                         triggers=["mode"], switch_from=[""]))
    named = next(a for a in d["anims"] if a["name"] == "Из Дона")
    check("switch_from сохранён", named["switch_from"] == [""])
    sw = am.switch_anims("hero", "", "squire")
    check("из обычного — названный переход раньше «из любого»", [a["name"] for a in sw] == ["Из Дона"])
    sw = am.switch_anims("hero", "ghost", "squire")
    check("из другого режима — «из любого»", [a["name"] for a in sw] == ["Переход"])
    check("у перехода — атлас", bool(sw and sw[0].get("atlas")))
    portrait_anim.put_anim("hero@squire", named["id"], upd(name="Из Дона", triggers=["mode"], enabled=False))
    sw = am.switch_anims("hero", "", "squire")
    check("PUT без switch_from — как было", portrait_anim.find("hero@squire", named["id"])["switch_from"] == [""])
    check("выключенный переход не играет — тогда «из любого»", [a["name"] for a in sw] == ["Переход"])
    check("перехода в обычный нет — пусто", am.switch_anims("hero", "squire", "") == [])
    for bad in (["a b"], "x", [1], ["x"] * 13):
        try:
            portrait_anim.put_anim("hero@squire", named["id"], upd(name="Из Дона", triggers=["mode"], switch_from=bad))
            check(f"плохой switch_from {bad!r:.20} — 400", False)
        except RoomError as e:
            check(f"плохой switch_from {bad!r:.20} — 400", e.status == 400)
    check("повод mode известен", "mode" in portrait_anim.TRIGGERS)
    check("is_switch_only", portrait_anim.is_switch_only({"triggers": ["mode"]})
          and not portrait_anim.is_switch_only({"triggers": ["mode", "idle"]})
          and not portrait_anim.is_switch_only({"triggers": []}))
    set_chat_mode("hero", "squire")
    la = am.live_anims("hero")
    check("живой набор Санчо: все переходы в него, выключенный тоже (решает клиент)",
          sorted(a["name"] for a in la["anims"]) == ["Из Дона", "Переход"])
    set_chat_mode("hero", "")


def test_events():
    section("E. Лента реплик и смена режима у бота")
    from app.api import chat_events, said
    from app.bot_instance import _note_mode_change, _trigger_mode
    start = said.cursor()
    said.mode_changed("hero", "web_user")
    said.mode_changed("hero", "web_user")
    got, _ = said.take(start, "web_user")
    check("mode — без текста, без повторов по тексту (два события)",
          [m["kind"] for m in got] == ["mode", "mode"] and all(m["text"] == "" for m in got))
    other, _ = said.take(start, "other_chat")
    check("только в свой чат", other == [])
    plain, _ = said.take(start, "web_user", acts=False)
    check("клиентам без acts — тоже", len(plain) == 2)
    replay, _ = said.take(0, "web_user", fresh_sec=said.REPLAY_SEC, acts=True)
    check("после перезапуска не пересказывается", all(m["kind"] != "mode" for m in replay))

    modes = {"k": "squire"}
    layer = SimpleNamespace(trigger_mode=lambda key: modes.get(key))
    bot = SimpleNamespace(persona=layer, context="api_hero")
    check("_trigger_mode: режим и обычный", _trigger_mode(bot, "k") == "squire" and _trigger_mode(bot, "x") == "")
    v0 = chat_events.changed_since(None)[0]
    start = said.cursor()
    _note_mode_change(bot, "k", "squire")
    check("режим тот же — события нет", said.take(start, "k")[0] == [])
    _note_mode_change(bot, "k", "")
    got, _ = said.take(start, "k")
    check("сменился — событие mode в чат", [(m["kind"], m["persona"]) for m in got] == [("mode", "hero")])
    check("и сигнал вебу", chat_events.changed_since(v0)[1] == ["hero"])
    start = said.cursor()
    _note_mode_change(SimpleNamespace(persona=layer, context="hero"), "k", "")
    check("не веб-персона (Telegram) — без события", said.take(start, "k")[0] == [])
    _note_mode_change(bot, "k", None)
    check("прежний режим не известен — без события", said.take(start, "k")[0] == [])


def test_routes():
    section("F. Маршруты")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    orig = server_mod._api_token
    server_mod._api_token = "s3cr3t"
    try:
        client = TestClient(server_mod.app, base_url="http://127.0.0.1", client=("192.168.1.20", 50000))
        auth = {"Authorization": "Bearer s3cr3t"}
        base = "/api/personas/hero/portrait"
        r = client.get(f"{base}?mode=squire", headers=auth)
        check("GET ?mode= — листы режима", r.status_code == 200 and r.json()["sheets"].get("A"))
        r = client.get(f"{base}?mode=nobody", headers=auth)
        check("GET ?mode= нет режима — 404", r.status_code == 404)
        r = client.get(f"{base}?mode=a%2Fb", headers=auth)
        check("GET ?mode= плохой id — 400", r.status_code == 400)
        r = client.put(f"{base}/B?mode=ghost", headers=auth,
                       json={"data_url": head_sheet((10, 200, 10)), "rows": 1, "cols": 1})
        check("PUT листа ?mode= — в аватар режима", r.status_code == 200 and "B" in r.json()["sheets"])
        r = client.get(base, headers=auth)
        check("обычный портрет без листа B", r.status_code == 200 and "B" not in r.json()["sheets"])
        r = client.delete(f"{base}/B?mode=ghost", headers=auth)
        check("DELETE листа ?mode=", r.status_code == 200 and "B" not in r.json()["sheets"])
        r = client.get(f"{base}/anims?mode=ghost&atlas=0", headers=auth)
        check("GET анимаций ?mode=", r.status_code == 200 and [a["name"] for a in r.json()["anims"]] == ["Летает"])
        r = client.put(f"{base}/anims/new?mode=ghost", headers=auth,
                       json=upd(data_url=anim_sheet(), name="Появление", triggers=["mode"], switch_from=["squire"]))
        new = next((a for a in r.json()["anims"] if a["name"] == "Появление"), None) if r.status_code == 200 else None
        check("PUT анимации ?mode= с переходом", new is not None and new["switch_from"] == ["squire"])
        r = client.put(f"{base}/anims-settings?mode=ghost", headers=auth,
                       json={"every_min": 7, "walk": False, "sleep_anim": new['id']})
        check("anims-settings ?mode=, свой сон режима", r.status_code == 200
              and r.json()["settings"]["every_min"] == 7 and r.json()["settings"]["sleep_anim"] == new['id'])
        r = client.get(f"{base}/anims?atlas=0", headers=auth)
        check("обычные анимации не тронуты", [a["name"] for a in r.json()["anims"]] == ["Обычная"]
              and r.json()["settings"]["every_min"] != 7 and r.json()["settings"]["sleep_anim"] is None)
        r = client.get(f"{base}/frames/anim/{new['id']}?mode=ghost", headers=auth)
        check("кадры анимации режима ?mode=", r.status_code == 200)
        r = client.get(f"{base}/frames/anim/{new['id']}", headers=auth)
        check("без ?mode= — её нет у обычного", r.status_code == 404)
        r = client.post(f"{base}/anims/{new['id']}/play?mode=ghost", headers=auth)
        check("play ?mode=", r.status_code == 200)
        r = client.delete(f"{base}/anims/{new['id']}?mode=ghost", headers=auth)
        check("DELETE анимации ?mode=", r.status_code == 200 and all(a["id"] != new["id"] for a in r.json()["anims"]))

        set_chat_mode("hero", "squire")
        r = client.get(f"{base}?live=1", headers=auth)
        check("GET ?live=1 — по режиму чата", r.status_code == 200 and r.json()["mode"] == "squire"
              and r.json()["avatar"] == "squire")
        r = client.get(f"{base}/anims?live=1&atlas=0", headers=auth)
        check("GET анимаций ?live=1", r.status_code == 200 and r.json()["mode"] == "squire"
              and {a["name"] for a in r.json()["anims"]} == {"Из Дона", "Переход"})
        lst = client.get("/api/portraits", headers=auth).json()
        check("/api/portraits: режим и портрет", lst["mode"].get("hero") == "squire" and "hero" in lst["personas"])
        live_head = client.get(f"{base}?live=1", headers=auth).json()
        live_anims = client.get(f"{base}/anims?live=1&atlas=0", headers=auth).json()
        check("метки /api/portraits = метки ?live=1",
              lst["updated"]["hero"] == live_head["updated_at"] and lst["anims"]["hero"] == live_anims["updated_at"])
        r = client.get(f"{base}/switch?from=&to=squire", headers=auth)
        check("/portrait/switch: переход с атласом",
              r.status_code == 200 and [a["name"] for a in r.json()["anims"]] == ["Переход"] and r.json()["anims"][0]["atlas"])
        r = client.get(f"{base}/switch?from=..&to=squire", headers=auth)
        check("/portrait/switch: плохой id — 422", r.status_code == 422)
        r = client.get(f"{base}/modes", headers=auth)
        check("/portrait/modes", r.status_code == 200 and r.json()["active"] == "squire"
              and r.json()["modes"]["squire"]["head"] is True)
        r = client.get("/api/personas/hero/inbox?client_id=t1", headers=auth)
        check("inbox: avatar_mode", r.status_code == 200 and r.json().get("avatar_mode") == "squire")
        set_chat_mode("hero", "")
        r = client.get("/api/personas/hero/inbox?client_id=t1", headers=auth)
        check("inbox: обычный — пусто", r.json().get("avatar_mode") == "")
        r = client.get(f"{base}?mode=squire")
        check("без токена — 401", r.status_code == 401)
    finally:
        server_mod._api_token = orig


if __name__ == "__main__":
    test_keys()
    test_resolve()
    test_live()
    test_switch()
    test_events()
    test_routes()
    print(f"\nИтого: {ok - failures} OK, {failures} FAIL")
    sys.exit(1 if failures else 0)

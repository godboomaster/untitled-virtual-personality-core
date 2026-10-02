"""Тест «Комнаты» (живое присутствие персоны в вебе) без LLM.

Проверяет: конфиг комнаты (дефолт и room: из YAML), санитайзер мест и
вывод по ключевым словам, spot/pose/pastime_since в тике StateEngine,
выбор источника chat_id=auto по активности обоих контекстов, сигналы
комнаты (append → consume → offline_log, курсор без двойного чтения),
размещение предметов (эвристика без модели, одна LLM-раскладка за тик),
слияние раскладки (null удаляет), клик по персоне (флаг + лимит),
проверку data-URL картинок и снимок GET /room.

Запуск: python -m scripts.test_room_presence
"""

import base64
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

# Обе папки данных — во временную ДО импорта модулей ядра:
# DATA_DIR (get_db_paths: living/) и VPC_DATA_DIR (data_dir(): known_chats,
# инвентарь, UI-файлы комнаты)
_TMP = tempfile.mkdtemp(prefix="room_presence_")
os.environ["DATA_DIR"] = _TMP
os.environ["VPC_DATA_DIR"] = _TMP

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def main():
    import importlib
    import app.core.config as config_mod
    importlib.reload(config_mod)
    from app.core import room
    importlib.reload(room)
    import app.core.state_engine as se_mod
    importlib.reload(se_mod)
    import app.core.living_persona as lp_mod
    importlib.reload(lp_mod)
    from app.api import room_api
    importlib.reload(room_api)

    ok = 0
    failed = []

    def check(name, cond):
        nonlocal ok
        status = "OK" if cond else "FAIL"
        print(f"  [{status}] {name}")
        if cond:
            ok += 1
        else:
            failed.append(name)

    # 1. Конфиг по умолчанию
    cfg = room.resolve_room_config(None)
    check("дефолт: is_default и места desk/window/shelf/bed",
          cfg["is_default"] and [s["key"] for s in cfg["spots"]] == ["desk", "window", "shelf", "bed"])
    check("дефолт: props и pet none",
          cfg["props"] == ["rug", "clock", "curtains", "shelf", "desk", "chair", "bed", "lamp", "plant"]
          and cfg["pet"] == "none")
    check("дефолт: у каждого места поза из словаря",
          all(s["pose"] in room.POSES for s in cfg["spots"]))

    # 2. room: из YAML
    ycfg = room.resolve_room_config({"room": {
        "props": ["rug", "desk", "bed"], "pet": "cat", "pet_label": "МОРИАРТИ",
        "poster_label": "FIG.03",
        "spots": [
            {"key": "desk", "place": "за столом", "label": "пишет", "pose": "write",
             "keywords": ["стол", "пиш"]},
            {"key": "kitchen", "place": "на кухне"},            # не встроенное — выкинуть
            {"key": "window", "pose": "fly"},                    # поза неверна → look
            {"key": "desk", "place": "дубль"},                   # дубль — выкинуть
            {"key": "bed", "place": "в кровати", "pose": "sleep"},
        ]}})
    check("YAML: is_default false, pet/labels",
          not ycfg["is_default"] and ycfg["pet"] == "cat"
          and ycfg["pet_label"] == "МОРИАРТИ" and ycfg["poster_label"] == "FIG.03")
    check("YAML: неизвестные и дубли выкинуты",
          [s["key"] for s in ycfg["spots"]] == ["desk", "window", "bed"])
    win = next(s for s in ycfg["spots"] if s["key"] == "window")
    check("YAML: неверная поза → дефолт места, пустые place/keywords → общие",
          win["pose"] == "look" and win["place"] == "у окна" and "окн" in win["keywords"])
    bad = room.resolve_room_config({"room": {"pet": "dragon", "spots": "x"}})
    check("YAML: мусор → pet none, места по умолчанию",
          bad["pet"] == "none" and [s["key"] for s in bad["spots"]] == ["desk", "window", "shelf", "bed"])

    # 3. Санитайзер и вывод места
    placements = {"Гитара": {"zone": "floor", "spot": {"label": "играет на гитаре",
                                                        "place": "у гитары", "pose": "sit"},
                             "placed_by": "llm", "at": time.time()},
                  "Кружка": {"zone": "desk", "spot": None, "placed_by": "heuristic",
                             "at": time.time()}}
    spots = room.allowed_spots(ycfg, placements, {}, ["Гитара", "Кружка"])
    keys = [s["key"] for s in spots]
    check("места: встроенные + item:Гитара + away",
          keys == ["desk", "window", "bed", "item:Гитара", "away"])
    check("вывод: «играет на гитаре» → item:Гитара",
          room.infer_spot("играет на гитаре", "", spots) == "item:Гитара")
    check("вывод: «уснул в кровати» → bed",
          room.infer_spot("уснул в кровати", "", spots) == "bed")
    check("вывод: «ушёл в магазин» → away",
          room.infer_spot("ушёл в магазин", "", spots) == "away")
    check("вывод: ничего общего → None", room.infer_spot("думает", "", spots) is None)

    st = {"pastime": "смотрит в окно", "location": "", "spot": "кухня", "pose": "sit"}
    room.sanitize_spot(st, {"spot": "desk", "pose": "write", "pastime": "пишет"}, spots)
    check("санитайзер: неизвестное место → по ключевым словам (window/look)",
          st["spot"] == "window" and st["pose"] == "look")
    st = {"pastime": "бренчит", "spot": "гитара", "pose": "dance"}
    room.sanitize_spot(st, {"spot": "desk"}, spots)
    check("санитайзер: имя предмета без префикса → item:, неверная поза → поза места",
          st["spot"] == "item:Гитара" and st["pose"] == "sit")
    st = {"pastime": "гуляет", "spot": "away", "pose": "sit"}
    room.sanitize_spot(st, {}, spots)
    check("санитайзер: away ⇒ поза away", st["spot"] == "away" and st["pose"] == "away")
    st = {"pastime": "думает", "spot": None, "pose": None}
    room.sanitize_spot(st, {"spot": "bed", "pose": "sleep", "pastime": "думает"}, spots)
    check("санитайзер: занятие то же, место не названо → остаётся где был",
          st["spot"] == "bed" and st["pose"] == "sleep")
    st = {"pastime": "размышляет", "spot": None}
    room.sanitize_spot(st, {"spot": "item:Скрипка", "pastime": "играет"}, spots)
    check("санитайзер: прежнего места больше нет → desk", st["spot"] == "desk")
    # Проснулась: занятие сменилось на что-то без ключевых слов — не
    # «спит в кровати» весь день (эвристика дня ключевых слов не даёт)
    st = {"pastime": "занят своими делами", "spot": None, "pose": None}
    room.sanitize_spot(st, {"spot": "bed", "pose": "sleep", "pastime": "спит"}, spots)
    check("санитайзер: проснулась (занятие сменилось) → не кровать и не sleep",
          st["spot"] == "desk" and st["pose"] not in ("sleep", "away"))
    st = {"pastime": "наблюдает за происходящим", "spot": None, "pose": None}
    room.sanitize_spot(st, {"spot": "desk", "pose": "sleep", "pastime": "спит"}, spots)
    check("санитайзер: проснулась без кровати → поза места, не sleep",
          st["spot"] == "desk" and st["pose"] == "write")

    # 4. Тик StateEngine: spot/pose, pastime_since
    eng = se_mod.StateEngine("room_ctx", "tester", use_gemma=False)
    s0 = eng.get_state("c1")
    check("state: дефолт со spot/pose/pastime_since",
          s0.get("spot") == "desk" and s0.get("pose") in room.POSES and s0.get("pastime_since"))
    prev = dict(eng.get_state("c1"))
    prev["pastime_since"] = 1000.0
    same = {"energy": 70, "mood": dict(prev["mood"]), "pastime": prev["pastime"],
            "location": prev["location"], "internal_note": "", "engine": "heuristic"}
    r1 = eng._commit_tick("c1", prev, same, {}, spots=spots)
    check("pastime_since: занятие не сменилось — метка прежняя",
          r1["pastime_since"] == 1000.0)
    changed = dict(same, pastime="читает книгу у полки", mood=dict(prev["mood"]))
    r2 = eng._commit_tick("c1", dict(r1), changed, {}, spots=spots)
    check("pastime_since: занятие сменилось — метка обновлена",
          r2["pastime_since"] > 1000.0 and time.time() - r2["pastime_since"] < 5)
    moved = dict(r2, spot="window", pose="look", mood=dict(r2["mood"]))
    eng._commit_tick("c1", dict(r2), moved, {}, spots=spots)
    last = eng.unconsumed("c1")[-1]
    check("diff: смена места пишется в offline_log (как location)",
          last["type"] == "state_change" and last["payload"]["diff"].get("spot") == "window")

    # Эвристика ночью: сон → кровать/поза sleep
    orig_now, orig_random = se_mod.timeutil.now, se_mod.random.random
    se_mod.timeutil.now = lambda: datetime(2026, 9, 27, 2, 0, 0)
    se_mod.random.random = lambda: 0.1
    try:
        h = eng._heuristic_tick(dict(r2), {}, spots=spots, user_language="ru")
        r3 = eng._commit_tick("c1", dict(r2), h, {}, spots=spots)
        h_en = eng._heuristic_tick(dict(r2), {}, spots=spots, user_language="en")
    finally:
        se_mod.timeutil.now, se_mod.random.random = orig_now, orig_random
    check("эвристика ночью: спит → bed/sleep",
          r3["pastime"] == "спит" and r3["spot"] == "bed" and r3["pose"] == "sleep")
    check("эвристика ночью (en): sleeping → bed/sleep",
          h_en["pastime"] == "sleeping" and h_en["spot"] == "bed" and h_en["pose"] == "sleep")
    st_en = {"pastime": "watching the rain", "spot": None, "pose": None}
    room.sanitize_spot(st_en, {"spot": "desk", "pose": "sit", "pastime": "x"}, spots)
    check("английский pastime → место по английским ключевым словам (окно)",
          st_en["spot"] == "window")
    se_mod.timeutil.now = lambda: datetime(2026, 9, 27, 11, 0, 0)
    se_mod.random.random = lambda: 0.9  # занятие меняется
    try:
        h = eng._heuristic_tick(dict(r3), {}, spots=spots)
        r3b = eng._commit_tick("c1", dict(r3), h, {}, spots=spots)
    finally:
        se_mod.timeutil.now, se_mod.random.random = orig_now, orig_random
    check("эвристика утром: проснулась — ушла из кровати, поза не sleep",
          r3b["pastime"] not in ("спит", "sleeping") and r3b["spot"] != "bed"
          and r3b["pose"] != "sleep")
    r4 = eng.tick("c2", {}, spots=spots)
    check("tick без модели: spot из списка, поза из словаря",
          r4["spot"] in keys and r4["pose"] in room.POSES and r4.get("pastime_since"))
    prompt = eng._build_tick_prompt(dict(r4), {}, [], "", spots=spots)
    check("тик-промпт: список мест и поля spot/pose тем же вызовом",
          "item:Гитара" in prompt and '"spot"' in prompt and '"pose"' in prompt)
    g = se_mod.StateEngine._state_from_gemma(
        {"mood": {"valence": 0.1, "arousal": 0.2, "tag": "x"}, "pastime": "p",
         "location": "l", "spot": "window", "pose": "look"}, dict(r4))
    check("разбор ответа модели: spot/pose приняты", g["spot"] == "window" and g["pose"] == "look")

    # 5. chat_id=auto: самый свежий чат по обоим контекстам
    persona = "roomtest"
    data_root = Path(_TMP)
    now = time.time()

    def write_json(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    src = room_api.resolve_source(persona)
    check("auto: данных нет → веб/web_user",
          src["context"] == f"api_{persona}" and src["chat_id"] == "web_user"
          and src["kind"] == "web" and src["last_activity"] is None)
    write_json(data_root / f"api_{persona}" / "known_chats.json",
               {"chats": ["web_user"], "activity": {"web_user": now - 600}})
    write_json(data_root / persona / "known_chats.json",
               {"chats": ["123", "456"], "activity": {"123": now - 60, "456": now - 3600}})
    src = room_api.resolve_source(persona)
    check("auto: свежее всех — Telegram-чат 123",
          src["context"] == persona and src["chat_id"] == "123" and src["kind"] == "telegram")
    write_json(data_root / f"api_{persona}" / "last_message.json", {"web_user": now - 5})
    src = room_api.resolve_source(persona)
    check("auto: last_message.json веба свежее → web_user",
          src["context"] == f"api_{persona}" and src["chat_id"] == "web_user")
    # Живое состояние есть только у Telegram-чата 456 — предпочитаем его
    write_json(room.living_dir(persona) / "state.json",
               {"chats": {"456": {"energy": 50, "mood": {"valence": 0, "arousal": 0.3, "tag": "t"},
                                  "pastime": "читает книгу", "location": "дом",
                                  "last_tick_at": now}}})
    src = room_api.resolve_source(persona)
    check("auto: при прочих — чат, у которого есть живое состояние",
          src["context"] == persona and src["chat_id"] == "456")
    src = room_api.resolve_source(persona, "web_user")
    check("явный chat_id: web_user → веб-контекст",
          src["context"] == f"api_{persona}" and src["last_activity"])
    try:
        room_api.resolve_source(persona, "123", "api_other")
        bad_ctx = False
    except room_api.RoomError as e:
        bad_ctx = e.status == 400
    check("явный context чужой персоны → 400", bad_ctx)

    # 6. Сигналы: append → consume → offline_log, курсор
    lp_persona = SimpleNamespace(persona_name=persona, system_prompt="Ты — тестовая персона.",
                                 persona_data={})
    lcfg = lp_mod.LivingPersonaConfig({"state_engine": {"enabled": True, "use_gemma": False}})
    living = lp_mod.LivingPersona(context="room_sig", persona=lp_persona, router=None,
                                  config=lcfg)
    room.append_signal("room_sig", "glance", "chat1", "Пользователь заглянул в комнату, пока ты: читает")
    living._consume_room_signals()
    got = [e for e in living.state_engine.unconsumed("chat1") if e["type"] == "room_signal"]
    check("сигнал: попал в offline_log как room_signal",
          len(got) == 1 and "заглянул" in got[0]["payload"]["event"]
          and got[0]["payload"].get("kind") == "glance")
    living._consume_room_signals()
    got = [e for e in living.state_engine.unconsumed("chat1") if e["type"] == "room_signal"]
    check("сигнал: повторный проход не дублирует", len(got) == 1)
    time.sleep(0.01)
    room.append_signal("room_sig", "focus_end", "chat1", "Пользователь закончил фокус-сессию")
    living._consume_room_signals()
    got = [e for e in living.state_engine.unconsumed("chat1") if e["type"] == "room_signal"]
    check("сигнал: новый дописанный — ровно один новый", len(got) == 2)
    check("сигнал: не считается фактом жизни для инициативы",
          living.state_engine.life_facts_count("chat1") == 0)
    fresh_consumer = room.SignalConsumer("room_sig")
    check("сигнал: курсор на диске — после «рестарта» ничего повторно",
          fresh_consumer.consume() == [])
    ctx_text = living.get_living_context("chat1", topic_text="привет")
    check("сигнал: доходит до контекста основной LLM",
          ctx_text is not None and "JUST NOW" in ctx_text and "заглянул" in ctx_text)
    check("сигнал: после показа — consumed (не повторяется)",
          not [e for e in living.state_engine.unconsumed("chat1") if e["type"] == "room_signal"])
    try:
        room.append_signal("room_sig", "hack", "chat1", "x")
        bad_type = False
    except ValueError:
        bad_type = True
    check("сигнал: неизвестный тип отклонён", bad_type)

    # ts сигнала строго растёт даже при одинаковом/отступившем time.time():
    # иначе дописанный после чтения сигнал с ts ≤ курсора терялся бы
    orig_time = room.time.time
    frozen = orig_time() + 5
    room.time.time = lambda: frozen
    try:
        room.append_signal("room_sig", "glance", "chat2", "первый")
        first = living._room_signals.consume()
        room.append_signal("room_sig", "glance", "chat2", "второй, тот же time()")
        room.time.time = lambda: frozen - 30   # часы шагнули назад
        room.append_signal("room_sig", "glance", "chat2", "третий, часы назад")
        second = living._room_signals.consume()
    finally:
        room.time.time = orig_time
    check("сигнал: равный/отступивший time() не теряется (ts монотонный)",
          [r["text"] for r in first] == ["первый"]
          and [r["text"] for r in second] == ["второй, тот же time()", "третий, часы назад"])
    # Сигнал забран поздно (тик раз в ~20 мин / владелец был выключен) —
    # свежесть по ts сигнала, а не по моменту записи в лог
    living.state_engine.log_event("chat3", "room_signal", {
        "event": "Пользователь заглянул давно", "kind": "glance",
        "ts": time.time() - 2 * 3600})
    old_ctx = living.get_living_context("chat3", topic_text="привет") or ""
    check("сигнал: старый по ts сигнала — не «JUST NOW»", "JUST NOW" not in old_ctx)

    # 7. Размещение предметов
    class FakeInventory:
        def __init__(self, names):
            self.items = [SimpleNamespace(name=n, description="") for n in names]

        def get_items(self):
            return list(self.items)

    class FakeLocal:
        def __init__(self, available, answer=""):
            self.available, self.answer, self.calls = available, answer, 0

        def is_available(self, task=None):
            return self.available

        def get_response(self, messages=None, **kw):
            self.calls += 1
            return self.answer

    living.inventory_manager = FakeInventory(["Гитара", "Яблоко", "Книга"])
    orig_glr = lp_mod.get_local_router
    try:
        lp_mod.get_local_router = lambda ctx: FakeLocal(False)
        living._place_room_items({})
        pl = room.read_placements("room_sig")
        check("размещение: модель недоступна → эвристика для всех (desk, без места)",
              set(pl) == {"Гитара", "Яблоко", "Книга"}
              and all(p["placed_by"] == "heuristic" and p["zone"] == "desk"
                      and p["spot"] is None for p in pl.values()))
        living.inventory_manager.items = living.inventory_manager.items[:1] + [
            SimpleNamespace(name="Телескоп", description="латунный"),
            SimpleNamespace(name="Скрипка", description="")]
        fake = FakeLocal(True, '{"place": true, "zone": "window", "spot": '
                               '{"label": "смотрит в телескоп", "place": "у телескопа", "pose": "look"}}')
        lp_mod.get_local_router = lambda ctx: fake
        living._place_room_items({})
        pl = room.read_placements("room_sig")
        check("размещение: удалённые предметы выброшены лениво",
              "Яблоко" not in pl and "Книга" not in pl)
        check("размещение: LLM — ровно один предмет за тик",
              fake.calls == 1 and pl.get("Телескоп", {}).get("placed_by") == "llm"
              and "Скрипка" not in pl)
        check("размещение: зона и место из ответа модели",
              pl["Телескоп"]["zone"] == "window"
              and pl["Телескоп"]["spot"] == {"label": "смотрит в телескоп",
                                             "place": "у телескопа", "pose": "look"})
        fake.answer = '{"place": false}'
        living._place_room_items({})
        pl = room.read_placements("room_sig")
        check("размещение: place:false → zone null (больше не спрашиваем)",
              "Скрипка" in pl and pl["Скрипка"]["zone"] is None and fake.calls == 2)
        living._place_room_items({})
        check("размещение: всё размещено — модель не зовётся", fake.calls == 2)
        living.config.room_llm_placement = False
        living.inventory_manager.items.append(SimpleNamespace(name="Лампа", description=""))
        living._place_room_items({})
        pl = room.read_placements("room_sig")
        check("размещение: room_llm_placement false → эвристика без вызова",
              pl["Лампа"]["placed_by"] == "heuristic" and fake.calls == 2)
        spots_now = [s["key"] for s in living.room_spots(["Гитара", "Телескоп", "Скрипка", "Лампа"])]
        check("места тика: item:Телескоп из размещения", "item:Телескоп" in spots_now)
    finally:
        lp_mod.get_local_router = orig_glr
    check("флаги: room_pokes_to_llm по умолчанию false, room_llm_placement true",
          lp_mod.LivingPersonaConfig({}).room_pokes_to_llm is False
          and lp_mod.LivingPersonaConfig({}).room_llm_placement is True)

    # 8. Раскладка: слияние, null удаляет
    lay = room_api.put_layout(persona, {"items": {"Гитара": {
        "marker": {"x": 2, "y": 0.5}, "size": 100, "icon": "guitar",
        "spot": {"label": "играет", "place": "у гитары", "pose": "zzz"}}},
        "avatar": {"head": "hex", "eyes": 1, "accessory": 0, "shade": 2}})
    g_item = lay["items"]["Гитара"]
    check("layout: координаты и размер зажаты, поза нормализована",
          g_item["marker"] == {"x": 1.0, "y": 0.5} and g_item["size"] == 25.0
          and g_item["spot"]["pose"] == "stand" and lay["avatar"]["head"] == "hex")
    lay = room_api.put_layout(persona, {"items": {"Гитара": {"hidden": True},
                                                  "Кружка": {"size": 5}}})
    check("layout: частичное слияние сохраняет прежние поля",
          lay["items"]["Гитара"]["marker"] == {"x": 1.0, "y": 0.5}
          and lay["items"]["Гитара"]["hidden"] is True and lay["avatar"]["head"] == "hex")
    lay = room_api.put_layout(persona, {"items": {"Гитара": None}, "avatar": None})
    check("layout: null удаляет предмет и сбрасывает аватар",
          "Гитара" not in lay["items"] and "Кружка" in lay["items"] and lay["avatar"] is None)
    check("layout: GET отдаёт сохранённое",
          room_api.get_layout(persona)["items"].keys() == {"Кружка"})
    check("layout: итоговые места — layout главнее размещений, hidden убирает",
          [s["key"] for s in room.item_spots(
              {"Гитара": {"spot": {"label": "a", "place": "b"}}},
              {"items": {"Гитара": {"spot": {"label": "свой", "place": "у грифа", "pose": "sit"}}}},
              None)] == ["item:Гитара"]
          and room.item_spots({"Гитара": {"spot": {"label": "a", "place": "b"}}},
                              {"items": {"Гитара": {"hidden": True}}}) == [])

    # 9. Клик по персоне: флаг + лимит
    res = room_api.poke(persona, {"features": {}})
    check("poke: флаг выключен → delivered false", res == {"ok": True, "delivered": False})
    sig_file = room.signals_path(persona)
    before = sig_file.read_text(encoding="utf-8").count("\n") if sig_file.exists() else 0
    on = {"features": {"room_pokes_to_llm": True}}
    r_first = room_api.poke(persona, on)
    r_second = room_api.poke(persona, on)
    after = sig_file.read_text(encoding="utf-8").splitlines()
    check("poke: флаг включён → доставлен один раз, повтор в 15 мин — нет",
          r_first["delivered"] is True and r_second["delivered"] is False
          and len(after) == before + 1)
    last_sig = json.loads(after[-1])
    check("poke: сигнал glance в контекст источника с pastime",
          last_sig["type"] == "glance" and last_sig["chat_id"] == "456"
          and "читает книгу" in last_sig["text"])

    # 10. data-URL: размер и MIME по сигнатуре
    good = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
    check("dataURL: валидный PNG принят", room_api.validate_data_url(good, 1024) == good)
    lying = "data:image/png;base64," + base64.b64encode(b"GIF89a" + b"x" * 40).decode()
    jpeg_as_png = "data:image/png;base64," + base64.b64encode(b"\xff\xd8\xff" + b"x" * 40).decode()

    def status_of(fn):
        try:
            fn()
            return 200
        except room_api.RoomError as e:
            return e.status
        except Exception:
            return 500  # необработанное исключение → в сервере был бы 500

    check("dataURL: чужая сигнатура → 400",
          status_of(lambda: room_api.validate_data_url(lying, 1024)) == 400)
    check("dataURL: MIME берётся из сигнатуры, а не из заголовка",
          room_api.validate_data_url(jpeg_as_png, 1024).startswith("data:image/jpeg;"))
    big = "data:image/png;base64," + base64.b64encode(PNG_1PX + b"\0" * 3000).decode()
    check("dataURL: больше лимита → 413",
          status_of(lambda: room_api.validate_data_url(big, 2048)) == 413)
    check("dataURL: не data-URL → 400",
          status_of(lambda: room_api.validate_data_url("http://x/y.png", 1024)) == 400)
    check("style: референс больше 1 МБ → 413",
          status_of(lambda: room_api.put_style(persona, {"reference": "data:image/png;base64," +
                    base64.b64encode(PNG_1PX + b"\0" * (1024 * 1024 + 10)).decode()})) == 413)
    st_saved = room_api.put_style(persona, {"description": "chibi, flat colors", "reference": good})
    check("style: сохранён и читается",
          room_api.get_style(persona)["description"] == "chibi, flat colors"
          and st_saved["reference"] == good)
    art = room_api.put_art(persona, {"sprite": {"dataUrl": good, "anchor": {"x": 0.5, "y": 1}},
                                     "sprites": {"sit": {"dataUrl": good}, "read": {"dataUrl": good}}})
    art = room_api.put_art(persona, {"sprites": {"sit": None},
                                     "room_bg": {"dataUrl": good,
                                                 "floorPoints": {"desk": {"x": 0.2, "y": 0.8}}}})
    check("art: слияние по позам, null удаляет позу",
          set(art["sprites"]) == {"read"} and art["sprite"]["anchor"] == {"x": 0.5, "y": 1.0}
          and art["room_bg"]["floorPoints"]["desk"] == {"x": 0.2, "y": 0.8})
    check("art: неизвестная поза → 400",
          status_of(lambda: room_api.put_art(persona, {"sprites": {"dance": {"dataUrl": good}}})) == 400)
    check("art: floorPoints не объект → 400 (не 500)",
          status_of(lambda: room_api.put_art(persona, {"room_bg": {
              "dataUrl": good, "floorPoints": [1, 2]}})) == 400)
    check("layout: avatar Infinity → 400 (не 500)",
          status_of(lambda: room_api.put_layout(persona, {"avatar": {"eyes": float("inf")}})) == 400)

    # 11. Снимок GET /room из файлов
    write_json(data_root / persona / "inventory.json",
               {"items": [{"name": "Гитара", "description": "", "acquired": "2026-09-01",
                           "source": "web", "tags": []}]})
    room.save_placements(persona, {
        "Гитара": {"zone": "floor", "spot": {"label": "играет на гитаре", "place": "у гитары",
                                             "pose": "sit"}, "placed_by": "llm", "at": now},
        "Старьё": {"zone": "desk", "spot": None, "placed_by": "heuristic", "at": now}})
    pdata = {"system_prompt": "x", "features": {"life": True},
             "room": {"pet": "cat", "spots": [{"key": "desk"}, {"key": "shelf"}]}}
    snap = room_api.room_snapshot(persona, pdata)
    check("снимок: источник — Telegram-чат с состоянием",
          snap["source"]["chat_id"] == "456" and snap["source"]["kind"] == "telegram")
    check("снимок: места конфига + item:Гитара",
          [s["key"] for s in snap["config"]["spots"]] == ["desk", "shelf", "item:Гитара"]
          and snap["config"]["spots"][-1]["item"] == "Гитара"
          and "keywords" not in snap["config"]["spots"][0])
    lv = snap["living"]
    check("снимок: легаси-состояние получило spot/pose по ключевым словам",
          lv and lv["state"]["spot"] == "shelf" and lv["state"]["pose"] == "read"
          and lv["state"].get("pastime_since"))
    check("снимок: инвентарь и размещения (только живые предметы)",
          [i["name"] for i in snap["inventory"]] == ["Гитара"]
          and set(snap["placements"]) == {"Гитара"})
    check("снимок: фокус не активен", snap["focus"] == {"active": False, "started_at": None,
                                                         "minutes": None})
    f = room_api.focus(persona, "start", 25)
    check("focus: start — активна, сигнал focus_start",
          f["focus"]["active"] and f["focus"]["minutes"] == 25
          and json.loads(room.signals_path(persona).read_text(encoding="utf-8")
                         .splitlines()[-1])["type"] == "focus_start")
    f = room_api.focus(persona, "end")
    check("focus: end — неактивна, сигнал focus_end в тот же чат",
          not f["focus"]["active"] and f["source"]["chat_id"] == "456"
          and json.loads(room.signals_path(persona).read_text(encoding="utf-8")
                         .splitlines()[-1])["type"] == "focus_end")
    check("focus: end активной сессии — was_active true", f.get("was_active") is True)
    sig_lines_before = len(room.signals_path(persona).read_text(encoding="utf-8").splitlines())
    f2 = room_api.focus(persona, "end")
    check("focus: повторный end — без сигнала и без реплики (was_active false)",
          f2.get("was_active") is False and len(room.signals_path(persona).read_text(
              encoding="utf-8").splitlines()) == sig_lines_before)
    nolife = room_api.room_snapshot(persona, {"system_prompt": "x", "features": {}})
    check("снимок: жизнь выключена → living null", nolife["living"] is None
          and nolife["config"]["is_default"])

    # Ротация сигналов: > 256 КБ → последние 200 строк
    big_ctx = "room_rot"
    path = room.signals_path(big_ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": 1.0, "type": "glance", "chat_id": "c", "text": "x" * 400}) + "\n"
    path.write_text(line * 700, encoding="utf-8")
    room.append_signal(big_ctx, "glance", "c", "после ротации")
    lines = path.read_text(encoding="utf-8").splitlines()
    check("сигналы: ротация до 200 строк + новая", len(lines) == 201
          and json.loads(lines[-1])["text"] == "после ротации")

    # Комната не трогает присутствие веб-чата
    import inspect
    src_code = inspect.getsource(room_api)
    import app.api.server as server_mod
    room_routes = "".join(inspect.getsource(fn) for fn in (
        server_mod.room_get, server_mod.room_poke, server_mod.room_focus,
        server_mod.room_layout_put))
    check("комната не вызывает web_presence.note",
          "web_presence.note" not in src_code and "web_presence" not in room_routes)

    total = ok + len(failed)
    print(f"\n{'ROOM PASSED' if not failed else 'ROOM FAILED'}: {ok}/{total} проверок")
    for name in failed:
        print(f"  провал: {name}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

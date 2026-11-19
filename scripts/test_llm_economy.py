"""Тест «Экономии нейронки» (app/core/llm_economy): где работу модели делает
код, и пункты настроек, возвращающие модель.

Проверяет офлайн, на фейковых роутерах/памяти (модель не зовётся никогда):
  1. хранилище: дефолты (всё — код), частичная правка, отказ на чужой ключ и
     не-bool, правка файла другим процессом видна, HTTP GET/PUT;
  2. инициативы: кубик ДО генерации (неудачный бросок — без вызова модели,
     темп прежний), отдых чата после SILENCE, рефлексия до порога монолога —
     как раньше, после промолчавшего монолога — только пунктом,
     старого порядка «текст, потом кубик» нет вовсе, новая реплика снимает
     отдых, сигнал состояния уважает отдых;
  3. урожай диалога: раз в HARVEST_MIN_MESSAGES реплик, таймер — пунктом;
  4. жизнь только чатам, где пользователь писал за LIFE_ACTIVE_DAYS дней
     (метка его реплики, не ответов персоны);
  5. тик состояния эвристикой (модель — пунктом и только при use_gemma),
     мёртвого score_initiative нет;
  6. размещение предметов комнаты эвристикой (модель — пунктом);
  7. «пока тебя не было» на пути ответа без модели (модель — пунктом);
  8. гейт веб-поиска: белый список на RU/EN-фразах, пункт «на каждое
     сообщение»; запрос — слова человека, не составной текст фото/камеры;
  9. финальный вопрос срезается кодом (RU/EN, эмодзи, *действия*, код,
     списки), регенерация — пунктом.

Запуск: OLLAMA_URL=http://127.0.0.1:9 python -m scripts.test_llm_economy
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="llm_economy_")
os.environ["DATA_DIR"] = _TMP
os.environ["VPC_DATA_DIR"] = _TMP
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")
os.chdir(_TMP)

results = {"ok": 0, "fail": 0}


def check(name, cond):
    results["ok" if cond else "fail"] += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


def section(title):
    print(f"\n── {title} ──")


def set_items(**items):
    from app.core import llm_economy
    llm_economy.update(items)


def reset_items():
    from app.core import llm_economy
    llm_economy.update(dict(llm_economy.DEFAULTS))


# ── 1. Хранилище ──────────────────────────────────────────

def test_store():
    section("1. Хранилище настроек")
    from app.core import llm_economy as le
    le._reset_cache()
    s = le.get_settings()
    check("дефолт: все пункты — код (false)",
          set(s["items"]) == set(le.KEYS) and not any(s["items"].values()))
    check("ожидаемые ключи на месте", set(le.KEYS) == {
        "proactive_reflection", "life_inactive_chats",
        "harvest_timer", "state_model_tick", "room_llm_placement", "return_summary_llm",
        "search_every_message", "question_regen"})
    check("группы: фон и путь ответа покрывают все ключи",
          sorted(k for keys in s["groups"].values() for k in keys) == sorted(le.KEYS)
          and list(s["groups"]) == ["background", "reply"])
    check("неизвестный ключ → enabled False", le.enabled("no_such_item") is False)
    out = le.update({"harvest_timer": True})
    check("частичная правка: только этот пункт", out["items"]["harvest_timer"] is True
          and sum(out["items"].values()) == 1 and le.enabled("harvest_timer"))
    saved = json.loads((Path(_TMP) / "llm_economy.json").read_text(encoding="utf-8"))
    check("записано в data/llm_economy.json", saved["items"]["harvest_timer"] is True)
    for bad, why in (({"nope": True}, "неизвестный ключ"), ({"harvest_timer": "false"}, "не bool")):
        try:
            le.update(bad)
            raised = False
        except ValueError:
            raised = True
        check(f"отказ: {why} — ValueError, файл не тронут",
              raised and le.enabled("harvest_timer") is True)
    # Другой процесс (Telegram-ядро) правит файл — видно после сверки
    old = le.RECHECK_SEC
    le.RECHECK_SEC = 0.0
    try:
        time.sleep(0.01)
        (Path(_TMP) / "llm_economy.json").write_text(
            json.dumps({"items": {"question_regen": True, "harvest_timer": False}}), encoding="utf-8")
        check("правка файла другим процессом видна", le.enabled("question_regen")
              and not le.enabled("harvest_timer"))
        (Path(_TMP) / "llm_economy.json").write_text("{битый", encoding="utf-8")
        check("битый файл → дефолты (всё — код)", not le.enabled("question_regen"))
    finally:
        le.RECHECK_SEC = old
    le._reset_cache()
    reset_items()
    check("сброс к дефолтам", not any(le.get_settings()["items"].values()))


def test_http():
    section("1b. HTTP: GET/PUT /api/settings/llm-economy")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi недоступен: {e})")
        return
    import app.api.server as server_mod
    orig = server_mod._api_token
    server_mod._api_token = ""
    try:
        c = TestClient(server_mod.app, base_url="http://127.0.0.1")
        r = c.get("/api/settings/llm-economy")
        check("GET: 200, items/defaults/groups, всё — код",
              r.status_code == 200 and {"items", "defaults", "groups"} <= set(r.json())
              and not any(r.json()["items"].values()))
        r = c.put("/api/settings/llm-economy", json={"items": {"search_every_message": True}})
        check("PUT частичный: 200, пункт включён, остальные — как были",
              r.status_code == 200 and r.json()["items"]["search_every_message"] is True
              and sum(r.json()["items"].values()) == 1)
        r = c.put("/api/settings/llm-economy", json={"items": {"bogus": True}})
        check("PUT неизвестного пункта → 422", r.status_code == 422)
        r = c.put("/api/settings/llm-economy", json={"items": {"question_regen": "true"}})
        check("PUT не-bool → 422", r.status_code == 422)
        server_mod._api_token = "secret-token"
        r = TestClient(server_mod.app, base_url="http://127.0.0.1", client=("192.168.1.20", 50000)).get("/api/settings/llm-economy")
        check("без токена при заданном API_TOKEN → 401/403", r.status_code in (401, 403))
    finally:
        server_mod._api_token = orig
        reset_items()


# ── 2. Инициативы ─────────────────────────────────────────

class FakeStm:
    def __init__(self):
        self.buffers = {}

    def _buf(self, chat_id):
        return self.buffers.setdefault(str(chat_id), [])

    def add(self, role, content, chat_id, ts=None):
        self._buf(chat_id).append({"role": role, "content": content,
                                   "timestamp": ts or time.time()})

    def get_messages(self, user_id=None, chat_id=None):
        return list(self._buf(chat_id if chat_id is not None else user_id))

    def get_last(self, n, user_id=None, chat_id=None):
        return self.get_messages(user_id, chat_id)[-n:]


class FakeMemory:
    def __init__(self):
        self.stm = FakeStm()

    def add_message(self, role, content, user_id="default", chat_id=None, **kw):
        self.stm.add(role, content, chat_id if chat_id is not None else user_id)


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, *, topic_id=None, parse_mode=None):
        self.sent.append((str(chat_id), text))
        return True


class FakeRouter:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def get_response(self, messages, **kw):
        self.calls += 1
        return self.reply


def make_pm(prob=1.0, router_reply="SILENCE", last_msg_ago=10 * 3600, ctx="econ_pm"):
    from app.features.proactive_messaging import ProactiveConfig, ProactiveMessaging
    mem = FakeMemory()
    chat = "c1"
    mem.stm.add("user", "привет, как жизнь?", chat, ts=time.time() - last_msg_ago)
    clock = {"last": time.time() - last_msg_ago}
    cfg = ProactiveConfig.from_dict({
        "enabled": True, "check_interval_minutes": 30, "silence_threshold_minutes": 180,
        "initiative_probability": prob, "feedback_enabled": False,
        "adaptive_threshold": False, "max_daily_initiatives": 50})
    pm = ProactiveMessaging(
        config=cfg, router=FakeRouter(router_reply),
        persona=SimpleNamespace(persona_data={}, system_prompt="Ты — тест.",
                                persona_name="tester", get_settings=lambda: {}),
        memory=mem, activity_tracker=None,
        get_last_message_time=lambda cid: clock["last"],
        sender=FakeSender(), context=ctx)
    # Промпт монолога строится из персоны/памяти — здесь не важен
    pm._build_monolog_prompt = lambda *a, **kw: [{"role": "user", "content": "monolog"}]
    pm._get_topic_for_chat = lambda cid: None
    return pm, chat, clock


def run(coro):
    return asyncio.run(coro)


def test_proactive():
    section("2. Инициативы: кубик до генерации, отдых, рефлексия")
    reset_items()
    import app.features.proactive_messaging as pm_mod

    # 2a. Кубик не выпал — монолог не генерируется; темп прежний (без отдыха:
    # следующий бросок — на следующей проверке, как раньше)
    pm, chat, clock = make_pm(prob=0.0)
    gen_calls = []
    pm._generate_initiative = lambda *a, **kw: gen_calls.append(a) or "Привет!"
    run(pm._check_all_chats())
    check("кубик не выпал: генератор не вызван, ничего не ушло",
          gen_calls == [] and pm._sender.sent == [])
    check("кубик не выпал: без отдыха — темп отправок прежний",
          not pm._backed_off(chat) and pm._should_send_initiative(chat))

    # 2b. Кубик выпал — генерация и отправка
    pm, chat, clock = make_pm(prob=1.0)
    pm._generate_initiative = lambda *a, **kw: "Как прошёл день?"
    run(pm._check_all_chats())
    check("кубик выпал: инициатива ушла", pm._sender.sent == [(chat, "Как прошёл день?")])

    # 2c. SILENCE настоящего монолога: рефлексии нет, чат отдыхает
    pm, chat, clock = make_pm(prob=1.0, router_reply="SILENCE")
    refl = []
    pm._generate_reflection_initiative = lambda cid, t: refl.append(cid) or "мысль"
    run(pm._check_all_chats())
    check("SILENCE: модель звалась один раз (монолог), рефлексии нет",
          pm.router.calls == 1 and refl == [] and pm._sender.sent == [])
    check("SILENCE: чат отдыхает", pm._backed_off(chat)
          and not pm._should_send_initiative(chat) and not pm.initiative_cheaply_possible(chat))
    until = pm._backoffs()[chat][0]
    check("отдых = адаптивный порог (без адаптации — silence_threshold_minutes)",
          abs(until - (time.time() + 180 * 60)) < 5)
    run(pm._check_all_chats())
    check("следующая проверка в отдыхе — модель не зовётся", pm.router.calls == 1)
    clock["last"] = time.time() - 9 * 3600  # пользователь написал (метка сдвинулась)
    check("новая реплика в чате снимает отдых", not pm._backed_off(chat))

    # 2d. Пункт proactive_reflection — рефлексия после SILENCE как раньше
    set_items(proactive_reflection=True)
    pm, chat, clock = make_pm(prob=1.0, router_reply="SILENCE")
    pm._generate_reflection_initiative = lambda cid, t: refl.append(cid) or "Вспомнил наш разговор."
    run(pm._check_all_chats())
    check("пункт рефлексии: после SILENCE рефлексия ушла",
          refl == [chat] and pm._sender.sent == [(chat, "Вспомнил наш разговор.")])
    reset_items()

    # 2e. Писать ли — всегда до генерации: старого порядка «текст, потом
    # кубик» больше нет ни в коде, ни пунктом настроек
    pm, chat, clock = make_pm(prob=0.0, ctx="econ_pm_order")
    gen_calls = []
    pm._generate_initiative = lambda *a, **kw: gen_calls.append(a) or "Привет!"
    for _ in range(3):
        run(pm._check_all_chats())
    check("кубик не выпал трижды — текст не генерировался ни разу", gen_calls == [])
    src = Path(pm_mod.__file__).read_text(encoding="utf-8")
    check("кубик в цикле инициатив — один, до генерации монолога",
          src.count("random.random() > effective_prob") == 1
          and src.index("random.random() > effective_prob") < src.index("self._generate_initiative, chat_id"))

    # 2f. До порога монолога (адаптивный порог короче): монолог модель не
    # зовёт, инициативу пишет рефлексия — единственный вызов, как раньше
    pm, chat, clock = make_pm(prob=1.0, last_msg_ago=60 * 60, ctx="econ_pm_early")  # час тишины < 180 мин
    pm.config.adaptive_threshold = True
    pm.config.min_silence_minutes = 30
    pm._calculate_adaptive_threshold = lambda cid, log=True: 30
    refl = []
    pm._generate_reflection_initiative = lambda cid, t: refl.append(cid) or "Вспомнил про твой проект."
    run(pm._check_all_chats())
    check("до порога монолога: модель монолога не звалась, рефлексия ушла (одна)",
          pm.router.calls == 0 and refl == [chat] and pm._sender.sent == [(chat, "Вспомнил про твой проект.")])
    pm, chat, clock = make_pm(prob=1.0, last_msg_ago=60 * 60, ctx="econ_pm3")
    pm.config.adaptive_threshold = True
    pm._calculate_adaptive_threshold = lambda cid, log=True: 30
    pm._generate_reflection_initiative = lambda cid, t: None
    run(pm._check_all_chats())
    check("до порога: рефлексия промолчала — чат отдыхает адаптивный порог",
          pm._backed_off(chat) and abs(pm._backoffs()[chat][0] - (time.time() + 30 * 60)) < 5)

    # 2g. Сигнал состояния: уважает отдых; SILENCE по сигналу — отдых
    pm, chat, clock = make_pm(prob=1.0, router_reply="SILENCE")
    run(pm.state_initiative_signal(chat, 0.9, "тест"))
    check("сигнал: SILENCE — модель звалась, чат отдыхает",
          pm.router.calls == 1 and pm._backed_off(chat))
    run(pm.state_initiative_signal(chat, 0.9, "тест"))
    check("сигнал в отдыхе — модель не зовётся", pm.router.calls == 1)
    pm2, chat2, _ = make_pm(prob=1.0, router_reply="SILENCE", ctx="econ_pm2")
    refl2 = []
    pm2._generate_reflection_initiative = lambda cid, t: refl2.append(cid) or "мысль"
    run(pm2.state_initiative_signal(chat2, 0.9, "тест"))
    check("сигнал: рефлексии по умолчанию нет", refl2 == [])
    from app.core import llm_economy as le
    check("пункта «сначала текст, потом кубик» нет", "proactive_generate_then_roll" not in le.KEYS)


# ── 3–7. Жизнь персоны ────────────────────────────────────

class UpLocal:
    """Локальная модель «есть»: отвечает валидным JSON, считает вызовы."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def is_available(self, task=None):
        return True

    def get_response(self, messages=None, **kw):
        self.calls.append(kw.get("task"))
        return self.answer


def make_living(ctx):
    import app.core.living_persona as lp_mod
    persona = SimpleNamespace(persona_name="tester",
                              system_prompt="Ты — тестовая персона. Говоришь коротко.")
    config = lp_mod.LivingPersonaConfig({
        "state_engine": {"enabled": True, "tick_interval_minutes": 20},
        "world_lore": {"enabled": True, "events_per_day": [1, 3]},
    })
    return lp_mod.LivingPersona(context=ctx, persona=persona, router=None, config=config)


def test_harvest():
    section("3. Урожай диалога")
    reset_items()
    import app.core.living_persona as lp_mod
    living = make_living("econ_harvest")
    calls = []
    done = threading.Event()

    def fake(chat_id, messages):
        calls.append(chat_id)
        done.set()

    living._harvest_in_dialog = fake
    msgs = [{"role": "user", "content": "привет, как ты?"}]

    def wait():
        done.wait(2)
        done.clear()

    living.on_user_message("h1", msgs)
    time.sleep(0.05)
    check("1-я реплика в медленном чате: урожая нет (таймер выключен)", calls == [])
    for _ in range(lp_mod.HARVEST_MIN_MESSAGES - 1):
        living.on_user_message("h1", msgs)
    wait()
    check(f"{lp_mod.HARVEST_MIN_MESSAGES}-я реплика — урожай", calls == ["h1"])
    set_items(harvest_timer=True)
    living.on_user_message("h2", msgs)
    wait()
    check("пункт harvest_timer: первая реплика давно молчавшего чата — урожай по таймеру",
          calls == ["h1", "h2"])
    reset_items()


def test_inactive_chats():
    section("4. Жизнь — только недавно активным чатам")
    reset_items()
    living = make_living("econ_inactive")
    now = time.time()
    activity = {"fresh": now - 86400, "stale": now - 5 * 86400, "never": 0.0}
    living._known_chats = lambda: list(activity)
    living._chat_throttled = lambda cid: False
    ticked, summarized = [], []
    living._tick_chat = lambda cid, pc, fs: ticked.append(cid)
    living.state_engine.unconsumed = lambda cid, limit=40: [{"id": 1}]
    living.summarizer.should_run_daily = lambda cid, n: True
    living.summarizer.daily_summarize = lambda cid, *a, **kw: summarized.append(cid)
    living.summarizer.should_run_screenwriter = lambda we: False
    living.get_last_user_activity = lambda cid: activity[cid]
    living._tick_all()
    check("тик и дневник — только чат с репликой за 3 дня",
          ticked == ["fresh"] and summarized == ["fresh"])
    check("метрика ticks_inactive", living.metrics["ticks_inactive"] == 2)
    ticked.clear()
    summarized.clear()
    set_items(life_inactive_chats=True)
    living._tick_all()
    check("пункт life_inactive_chats: тикают все чаты, как раньше",
          sorted(ticked) == ["fresh", "never", "stale"])
    reset_items()
    ticked.clear()
    living.get_last_user_activity = None
    living._tick_all()
    check("без источника метки (заготовка) — фильтра нет", len(ticked) == 3)

    # Источник метки в BotInstance: реплика пользователя, не ответ персоны
    from app.bot_instance import BotInstance
    bot = BotInstance.__new__(BotInstance)
    bot._activity_tracker = None
    bot.memory = FakeMemory()
    bot.memory.stm.add("user", "привет", "w", ts=now - 4 * 86400)
    bot.memory.stm.add("assistant", "инициатива персоны", "w", ts=now - 60)
    check("метка активности — по реплике пользователя (не по инициативе)",
          abs(bot._last_user_message_time("w") - (now - 4 * 86400)) < 1)
    check("чат без реплик пользователя — 0", bot._last_user_message_time("empty") == 0)
    bot._activity_tracker = SimpleNamespace(get_last_activity=lambda cid: now - 30)
    check("метка трекера (Telegram) свежее STM — берётся она",
          abs(bot._last_user_message_time("w") - (now - 30)) < 1)


def test_state_tick():
    section("5. Тик состояния")
    reset_items()
    from app.core.state_engine import StateEngine
    tick_json = ('{"energy": 60, "mood": {"valence": 0.2, "arousal": 0.3, "tag": "calm"}, '
                 '"pastime": "reading", "location": "home", "initiative_score": 0.9}')
    se = StateEngine("econ_state", "tester", use_gemma=True)
    se.local = UpLocal(tick_json)
    st = se.tick("c1", {})
    st2, score = se.tick_and_score("c1", {}, silence_hours=1.0, since_initiative_hours=1.0)
    check("по умолчанию: тик и тик+скоринг — эвристика, модель не звалась",
          st["engine"] == "heuristic" and st2["engine"] == "heuristic"
          and se.local.calls == [] and 0.0 <= score <= 1.0)
    set_items(state_model_tick=True)
    st = se.tick("c1", {})
    st2, score = se.tick_and_score("c1", {}, silence_hours=1.0, since_initiative_hours=1.0)
    check("пункт state_model_tick: тик моделью (use_gemma персоны — да)",
          st["engine"] == "gemma" and st2["engine"] == "gemma" and score == 0.9
          and len(se.local.calls) == 2)
    se_off = StateEngine("econ_state_off", "tester", use_gemma=False)
    se_off.local = UpLocal(tick_json)
    st = se_off.tick("c1", {})
    check("пункт включён, но персона запретила (use_gemma: false) — эвристика",
          st["engine"] == "heuristic" and se_off.local.calls == [])
    reset_items()
    import app.core.state_engine as se_mod
    check("мёртвого score_initiative / INITIATIVE_SCORE_PROMPT нет",
          not hasattr(StateEngine, "score_initiative")
          and not hasattr(se_mod, "INITIATIVE_SCORE_PROMPT"))


def test_room_placement():
    section("6. Размещение предметов комнаты")
    reset_items()
    import app.core.living_persona as lp_mod
    from app.core import room
    living = make_living("econ_room")
    living.inventory_manager = SimpleNamespace(
        get_items=lambda: [SimpleNamespace(name="Телескоп", description="латунный")])
    fake = UpLocal('{"place": true, "zone": "window", "spot": null}')
    orig = lp_mod.get_local_router
    lp_mod.get_local_router = lambda ctx: fake
    try:
        living._place_room_items({})
        pl = room.read_placements("econ_room")
        check("по умолчанию: эвристика без вызова модели",
              fake.calls == [] and pl["Телескоп"]["placed_by"] == "heuristic")
        set_items(room_llm_placement=True)
        room.save_placements("econ_room", {})
        living._place_room_items({})
        pl = room.read_placements("econ_room")
        check("пункт room_llm_placement: место решает модель",
              fake.calls == ["room_placement"] and pl["Телескоп"]["placed_by"] == "llm")
    finally:
        lp_mod.get_local_router = orig
        reset_items()


def test_return_summary():
    section("7. «Пока тебя не было» на пути ответа")
    reset_items()
    living = make_living("econ_return")
    fake = UpLocal('{"theses": ["Сжато моделью"]}')
    living.summarizer.local = fake
    entries = [{"id": i, "type": "world_event", "payload": {"event": f"Событие {i}"}}
               for i in range(8)]
    ctx = living.summarizer.build_return_context("c1", entries, 24.0)
    check("по умолчанию: без модели, последние 5 записей как есть",
          fake.calls == [] and ctx and "Событие 7" in ctx and "Событие 2" not in ctx
          and "Событие 3" in ctx)
    set_items(return_summary_llm=True)
    ctx = living.summarizer.build_return_context("c1", entries, 24.0)
    check("пункт return_summary_llm: тезисы сжимает модель",
          fake.calls == ["offline_summary"] and "Сжато моделью" in ctx)
    reset_items()


# ── 8. Веб-поиск ──────────────────────────────────────────

SEARCH_YES = [
    "найди рецепт борща", "погода?", "какая погода в Москве завтра", "курс доллара сегодня",
    "последние новости", "кто выиграл вчерашний матч?", "сколько стоит iPhone 17?",
    "когда выйдет GTA 6?", "кто такой Илон Маск?", "что такое ChatGPT?",
    "что нового в python 3.14", "что сегодня делал президент?", "Коннор, поищи отзывы о Samsung",
    "what's the weather like in London?", "search for cheap flights to Rome",
    "look up the population of Japan", "latest news about SpaceX",
    "who won the game last night?", "how much is a Tesla Model 3?",
    "what is the capital of Peru", "who is Elon Musk",
]
SEARCH_NO = [
    "привет", "Коннор?", "как дела?", "я сегодня устал", "почему небо голубое?",
    "что ты помнишь обо мне?", "Как тебе Москва?", "что думаешь о фильме Интерстеллар?",
    "расскажи анекдот", "What do you think about Paris?", "I love Paris",
    "Can you help me with my homework?", "what is love?", "а сейчас что?",
    "сколько будет 2+2", "что видит робот?", "what should I cook tonight?",
    "напомни купить молоко",
]


def test_search_gate():
    section("8. Гейт веб-поиска")
    reset_items()
    from app.features.search_gate import search_gate_reason, search_skip_reason
    names = ["Коннор", "connor"]
    for t in SEARCH_YES:
        check(f"искать: {t!r}", search_gate_reason(t, names) is None)
    for t in SEARCH_NO:
        check(f"не искать: {t!r}", search_gate_reason(t, names) is not None)
    old_yes = [t for t in SEARCH_NO if search_skip_reason(t, names) is None]
    check("на каждое сообщение: как раньше — ищется всё, кроме связок/вопросов о персоне",
          all(search_gate_reason(t, names, every_message=True) is None for t in old_yes)
          and len(old_yes) >= 10
          and search_gate_reason("привет", names, every_message=True) == "smalltalk")

    # Запрос — слова человека; составной текст фото/камеры в Google не уходит
    from app.bot_instance import BotInstance
    bot = BotInstance.__new__(BotInstance)
    bot.trigger_words = ["коннор"]
    bot.persona_name = "connor"
    composite = ("что видит робот?\nThe user asked you to look through the camera "
                 "of the device «pi». Who is Elon Musk? Weather news today.")
    skip, text, query = bot._search_plan(composite, "что видит робот?", composite)
    check("камера: гейт и запрос — по словам человека, не по описанию кадра",
          text == "что видит робот?" and query == "что видит робот?" and skip == "no_need")
    photo = "The user sent an image. OCR: latest news about SpaceX\nКоннор, найди этот товар"
    skip, text, query = bot._search_plan(photo, "Коннор, найди этот товар", photo)
    check("фото с подписью-просьбой: ищется подпись", skip is None
          and query == "Коннор, найди этот товар")
    skip, text, query = bot._search_plan("The user sent an image. OCR: weather today", "", "x")
    check("фото без подписи — не ищем (no_text)", skip == "no_text")
    skip, text, query = bot._search_plan("какая погода завтра", None, "какая погода завтра (rw)")
    check("обычный текст: запрос — переписанный (как раньше)",
          skip is None and query == "какая погода завтра (rw)")
    skip, _, _ = bot._search_plan("я сегодня устал", None, "я сегодня устал")
    set_items(search_every_message=True)
    skip_old, _, _ = bot._search_plan("я сегодня устал", None, "я сегодня устал")
    check("пункт search_every_message переключает гейт хода",
          skip == "no_need" and skip_old is None)
    reset_items()


# ── 9. Финальный вопрос ───────────────────────────────────

TRIM_CASES = [
    ("Хорошо. А у тебя как?", "Хорошо."),
    ("Отлично. Как ты? Что нового?", "Отлично."),
    ("Было весело! А тебе понравилось? 😊", "Было весело! 😊"),
    ("Понял тебя. Как тебе? *улыбается*", "Понял тебя. *улыбается*"),
    ("Хорошо. *улыбается* А ты как?", "Хорошо. *улыбается*"),
    ("That sounds great. What do you think?", "That sounds great."),
    ("Sure thing! Want me to go on?", "Sure thing!"),
    ("Я рад. Ну что, решил?  \n\n", "Я рад."),
    ("Хм.\nА ты как думаешь?", "Хм."),
    ("Вот код:\n```py\nx = 1\n```\nПонятно?", "Вот код:\n```py\nx = 1\n```"),
]
KEEP_CASES = [
    "Отличная мысль, т.е. ты согласен? Тогда начнём?", "Записал на 15 ч. Напомнить?",
    "Правда?", "Серьёзно?!", "Отлично, а ты как?", "Как тебе? 😊",
    "Он сказал так. Он спросил: «Ты идёшь?»",
    "Варианты:\n- чай\n- кофе\nЧто выберешь?",
    "```\nwhat?", "- первое\n- второе?", "Смотри:\n\nКак тебе?",
    "Записал. Ещё что-то? [TODO_ADD:молоко]",
]


def test_question_trim():
    section("9. Финальный вопрос")
    reset_items()
    from app.features import conversation_style as cs
    for src, want in TRIM_CASES:
        got = cs.trim_tail_question(src)
        check(f"срезано: {src!r} → {want!r}", got == want)
    for src in KEEP_CASES:
        check(f"оставлено как есть: {src!r}", cs.trim_tail_question(src) is None)
    rare = cs.ConversationStyleConfig({})
    check("should_trim: rare — первый вопрос можно, второй подряд — срезка",
          not cs.should_trim(rare, "Ок. Правда?", 0) and cs.should_trim(rare, "Ок. Правда?", 1))
    check("should_trim: вопрос за *действием* тоже виден",
          cs.should_trim(rare, "Ок. Как тебе? *улыбается*", 1))
    check("should_trim: маркеры — не трогаем",
          not cs.should_trim(rare, "Ок. Ещё? [TODO_ADD:x]", 3))

    # Переключение пути в BotInstance: код срезает, пункт — регенерация моделью
    from app.bot_instance import BotInstance
    bot = BotInstance.__new__(BotInstance)
    bot.conversation_style = rare
    router = FakeRouter("Понятно, рассказал.")
    bot.router = router
    bot._clean_response = lambda text: text
    msgs = [{"role": "user", "content": "расскажи"}]
    out = bot._final_question_guard("Рассказал. Интересно?", 1, msgs, {}, "ru")
    check("по умолчанию: срезано кодом, модель не звалась",
          out == "Рассказал." and router.calls == 0)
    out = bot._final_question_guard("Интересно?", 1, msgs, {}, "ru")
    check("по умолчанию: один вопрос — ответ как есть, без модели",
          out == "Интересно?" and router.calls == 0)
    set_items(question_regen=True)
    out = bot._final_question_guard("Рассказал. Интересно?", 1, msgs, {}, "ru")
    check("пункт question_regen: регенерация моделью", out == "Понятно, рассказал."
          and router.calls == 1)
    reset_items()


def main():
    test_store()
    test_http()
    test_proactive()
    test_harvest()
    test_inactive_chats()
    test_state_tick()
    test_room_placement()
    test_return_summary()
    test_search_gate()
    test_question_trim()
    print(f"\nИтого: {results['ok'] + results['fail']} проверок, {results['fail']} провалов")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())

"""Ответы без ноутбука (app/api/offline_api.py и маршруты /offline/*):

  - персона-кит: характер персоны, правила офлайна, правило вопросов,
    факты (с учётом приватности чата), хвост переписки, язык; секретов нет;
  - ключи: API-провайдеры персоны в её порядке, с моделью персоны; без
    модели или ключа и исключённые персоной — не отдаются;
  - приём офлайн-переписки: по времени, внутри хода пользователя, обычным
    memory.add_message (с извлечением фактов); повторы по id не пишутся;
  - маршруты: ключи с Cache-Control: no-store, проверка входа (роль, id,
    размер пачки);
  - векторы памяти для поиска по смыслу на телефоне: из базы по тексту,
    недостающие — моделью ядра, int8 base64; векторы другой модели — не
    отдаются.

Бот подставной: модели, Chroma и сеть не вызываются; данные — во временной
папке.
Запуск: PYTHONPATH=. python3 -m scripts.test_offline_api
"""

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="offline_api_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

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


class FakeRouter:
    def __init__(self):
        self.available = {
            "deepseek": {"base_url": "https://api.deepseek.com/v1/", "model": "deepseek-chat",
                         "api_keys": ["sk-ds-1", "sk-ds-2"]},
            "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "",
                     "api_keys": ["gsk-1"]},
            "kimi": {"base_url": "https://api.moonshot.ai/v1", "model": "kimi-k2",
                     "api_keys": ["sk-kimi"]},
            "zai": {"base_url": "https://api.z.ai/v1", "model": "glm", "api_keys": []},
            "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt", "api_keys": ["sk-oa"]},
        }
        self.overrides = {"groq": "llama-3.1-8b-instant"}
        self.excluded = {"openai"}

    def _get_provider_order(self):
        return ["kimi", "groq", "deepseek", "zai", "openai"]

    def _is_excluded(self, name):
        return name in self.excluded

    def model_for(self, name):
        return self.overrides.get(name) or self.available[name].get("model", "")


class FakeBot:
    context = "api_connor"

    def __init__(self):
        self.persona = SimpleNamespace(system_prompt="Ты Коннор, андроид-детектив.",
                                       persona_data={"name": "Коннор"})
        self.conversation_style = SimpleNamespace(frequency="rare")
        self.router = FakeRouter()
        self.calls = []
        self.turns = []
        stm = [
            {"role": "user", "content": "привет", "timestamp": 10.0},
            {"role": "system", "content": "служебное", "timestamp": 11.0},
            {"role": "assistant", "content": "Здравствуйте.", "timestamp": 12.0},
            {"role": "assistant", "content": "", "timestamp": 13.0},
        ]
        self.stm = stm
        bot = self

        class Ltm:
            def get_all_facts(self, user_id="default", chat_id=None):
                bot.calls.append(("facts", user_id, chat_id))
                return ["Имя: Хэнк", "  ", "Работа: полиция"]

        class Stm:
            def get_messages(self, chat_id=None):
                bot.calls.append(("stm", chat_id))
                return list(bot.stm)

        def add_message(role, content, user_id="default", chat_id=None, user_name=None, timestamp=None):
            bot.calls.append(("add", role, content, user_id, chat_id, user_name, bool(bot.turns), timestamp))

        self.memory = SimpleNamespace(ltm=Ltm(), stm=Stm(), add_message=add_message)

        class Rm:
            def upcoming_for_chat(self, key):
                bot.calls.append(("upcoming", key))
                return [{"id": "r1", "ref": "r1@2000000000", "task": "позвонить", "at": 2e9,
                         "recurring": False}]

            def add_reminder(self, chat_id, user_name, task, delay, user_id=None):
                bot.calls.append(("remind", chat_id, user_name, task, round(delay), user_id))
                return {"id": f"c{len(bot.calls)}"}

        self.reminder_manager = Rm()
        self.self_memory = SimpleNamespace(get_context_block=lambda: "[PERSONAL MEMORY Коннор]\n- [вчера] расследовал дело")

    def _get_dossier_context_line(self, chat_id, user_id):
        self.calls.append(("dossier", chat_id, user_id))
        return "Known about the user (chat analysis): interests: рыбалка"

    @staticmethod
    def stm_key(chat_id, user_id):
        return str(chat_id if chat_id is not None else user_id)

    def chat_user_language(self, key):
        return "ru"

    @contextmanager
    def user_turn(self, key):
        self.turns.append(key)
        try:
            yield {}
        finally:
            self.turns.pop()


def test_kit():
    section("A. Персона-кит")
    from app.api import offline_api
    bot = FakeBot()
    kit = offline_api.build_kit(bot, "connor", "web_user", "web_user")
    check("версия, id и имя персоны", kit["version"] == offline_api.KIT_VERSION
          and kit["persona"] == "connor" and kit["name"] == "Коннор")
    system = kit["system"]
    check("в системном тексте — характер персоны и правила офлайна",
          system.startswith("Ты Коннор") and offline_api.OFFLINE_RULES in system)
    check("…правило вопросов персоны (conversation_style)", "Do not end your reply with a question" in system)
    check("…факты о собеседнике списком", "What you know about the user:\n- Имя: Хэнк\n- Работа: полиция" in system)
    check("…и язык собеседника последним", system.rstrip().endswith(
        __import__("app.core.language", fromlist=["x"]).user_language_line("ru").rstrip()))
    check("факты — без пустых строк", kit["facts"] == ["Имя: Хэнк", "Работа: полиция"])
    parts = kit["parts"]
    check("части промпта для модели телефона: характер, правила, правило вопросов, язык",
          parts["character"].startswith("Ты Коннор") and parts["rules"] == offline_api.OFFLINE_RULES
          and "Do not end your reply with a question" in parts["style"] and parts["language"] in system)
    check("факты — с учётом приватности этого чата", ("facts", "web_user", "web_user") in bot.calls)
    from app.core.persona import (ENV_RULE, SELF_MEMORY_RULE, STM_RELEVANT_HEAD, STM_RELEVANT_RULE,
                                  WEB_SOURCE_PRIORITY)
    check("кит версии 3", kit["version"] == 3)
    check("напоминания чата — из ReminderManager (ключ чата)",
          kit["reminders"][0]["ref"] == "r1@2000000000" and ("upcoming", "web_user") in bot.calls
          and kit["reminders_enabled"] is True)
    check("правила выдачи, окружения, личной памяти и найденных реплик — те же, что в промпте ядра",
          kit["rules"] == {"web": WEB_SOURCE_PRIORITY, "env": ENV_RULE, "self_memory": SELF_MEMORY_RULE,
                           "recall_head": STM_RELEVANT_HEAD, "recall": STM_RELEVANT_RULE})
    check("строка досье — как в ответе ядра (чат и собеседник)",
          kit["dossier"].startswith("Known about the user") and ("dossier", "web_user", "web_user") in bot.calls)
    check("личная память персоны — блок ядра", kit["self_memory"].startswith("[PERSONAL MEMORY"))
    notes = kit["notes"]
    check("заметки о напоминаниях — без экранирования {{ }}, с подстановками",
          "{now}" in notes["extract"] and "{{" not in notes["extract"] and '{"task"' in notes["extract"]
          and "{when}" in notes["set"] and "{list}" in notes["scheduled"])
    check("заметки «не понял время» и «передумал» — с задачей и запретом подтверждать",
          all("{task}" in notes[k] and "Do NOT" in notes[k] for k in ("not_understood", "declined", "ask_time", "past")))
    gate = kit["search_gate"]
    check("гейт поиска: связки, обращения и шаблоны вопросов о персоне",
          "привет" in gate["smalltalk"] and "эй" in gate["address_words"] and len(gate["self_patterns"]) >= 5)
    check("без местоположения — города для погоды нет", kit["env"] is None)
    from app.features import env_context
    orig_load = env_context.load_location
    env_context.load_location = lambda: {"mode": "manual", "city": "Новосибирск", "lat": 55.0, "lon": 82.9}
    try:
        kit2 = offline_api.build_kit(bot, "connor", "web_user", "web_user")
    finally:
        env_context.load_location = orig_load
    env = kit2["env"] or {}
    check("с местоположением — город, координаты и описания погодных кодов",
          (env.get("city"), env.get("lat"), env.get("lon")) == ("Новосибирск", 55.0, 82.9)
          and env.get("codes", {}).get("0") == "clear")
    bot.reminder_manager, rm = None, bot.reminder_manager
    kit3 = offline_api.build_kit(bot, "connor", "web_user", "web_user")
    bot.reminder_manager = rm
    check("напоминания у персоны выключены — пустой список и флаг",
          kit3["reminders"] == [] and kit3["reminders_enabled"] is False)
    check("хвост переписки: только user/assistant с текстом, по порядку",
          [(m["role"], m["text"]) for m in kit["history"]] == [("user", "привет"), ("assistant", "Здравствуйте.")])
    check("переписка — этого чата", ("stm", "web_user") in bot.calls)
    check("граница истории — время первого сообщения в STM", kit["history_first_ts"] == 10.0)
    check("число сообщений для памяти телефона — только user/assistant с текстом", kit["history_total"] == 2)
    bot.stm = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}" + "я" * (3000 if i == 5 else 0),
                "timestamp": 100.0 + i} for i in range(45)]
    kit4 = offline_api.build_kit(bot, "connor", "web_user", "web_user")
    check(f"в ките — последние {offline_api.HISTORY_TAIL}",
          [m["text"] for m in kit4["history"]] == [f"m{i}" for i in range(15, 45)] and kit4["history_first_ts"] == 100.0
          and kit4["history_total"] == 45)
    mem = offline_api.build_memory(bot, "connor", "web_user", "web_user")
    check("память для телефона — вся переписка, по порядку",
          [m["text"][:3].rstrip("я") for m in mem["history"]] == [f"m{i}" for i in range(45)])
    check(f"…длинное сообщение — до {offline_api.MEMORY_TEXT} символов",
          len(mem["history"][5]["text"]) == offline_api.MEMORY_TEXT)
    check("…и факты без пустых", mem["facts"] == ["Имя: Хэнк", "Работа: полиция"] and mem["persona"] == "connor")
    check("без векторов в базе — поиск только по словам", mem["vectors"] is None and "vec" not in mem["history"][0]
          and "facts_vec" not in mem)
    bot.stm = []
    kit5 = offline_api.build_kit(bot, "connor", "web_user", "web_user")
    check("пустая переписка — граница 0, хвост пуст", kit5["history_first_ts"] == 0.0 and kit5["history"] == [])
    bot.stm = list(FakeBot().stm)
    flat = repr(kit)
    check("в ките нет ключей", "sk-" not in flat and "gsk" not in flat)


def test_providers():
    section("B. Ключи провайдеров")
    from app.api import offline_api
    creds = offline_api.provider_creds(FakeBot())
    names = [c["name"] for c in creds]
    check("порядок персоны", names == ["kimi", "groq", "deepseek"])
    check("модель персоны (llm.models) вместо общей", creds[1]["model"] == "llama-3.1-8b-instant")
    check("все ключи провайдера", creds[2]["api_keys"] == ["sk-ds-1", "sk-ds-2"])
    check("адрес без завершающего /", creds[2]["base_url"] == "https://api.deepseek.com/v1")
    check("без ключей (zai) и исключённые персоной (openai) — не отдаются",
          "zai" not in names and "openai" not in names)


def test_import():
    section("C. Приём офлайн-переписки")
    from app.api import offline_api
    bot = FakeBot()
    msgs = [
        {"id": "b", "role": "assistant", "text": "Я на связи.", "ts": 20.0, "source": "cloud"},
        {"id": "a", "role": "user", "text": "ты тут?", "ts": 19.0},
        {"id": "c", "role": "user", "text": "   ", "ts": 21.0},
    ]
    res = offline_api.import_messages(bot, "web_user", "web_user", msgs, user_name="Хэнк")
    adds = [c for c in bot.calls if c[0] == "add"]
    check("принято 3, повторов 0", res == {"imported": 3, "skipped": 0})
    check("по времени: сначала реплика пользователя, потом ответ",
          [(c[1], c[2]) for c in adds] == [("user", "ты тут?"), ("assistant", "Я на связи.")])
    check("пустой текст не пишется", len(adds) == 2)
    check("имя пользователя — у его реплик", adds[0][5] == "Хэнк" and adds[1][5] is None)
    check("запись — внутри хода пользователя", all(c[6] for c in adds))
    check("в историю этого чата", all(c[3] == "web_user" and c[4] == "web_user" for c in adds))
    check("время — с телефона", [c[7] for c in adds] == [19.0, 20.0])

    bot.calls.clear()
    res = offline_api.import_messages(bot, "web_user", "web_user",
                                      msgs + [{"id": "d", "role": "user", "text": "ещё", "ts": 30.0}])
    adds = [c for c in bot.calls if c[0] == "add"]
    check("повтор той же пачки — только новое", res == {"imported": 1, "skipped": 3}
          and [c[2] for c in adds] == ["ещё"])
    seen = offline_api._seen_path(bot)
    check("принятые id сохранены в папке данных контекста", seen.exists()
          and seen.parent.name == "api_connor")

    seen.write_text("[1, 2]")
    bot.calls.clear()
    res = offline_api.import_messages(bot, "web_user", "web_user",
                                      [{"id": "z", "role": "user", "text": "после порчи", "ts": 40.0}])
    check("битый файл id — не падает, приём идёт", res["imported"] == 1)

    bot.calls.clear()
    future = time.time() + 86400
    offline_api.import_messages(bot, "web_user", "web_user",
                                [{"id": "f", "role": "user", "text": "часы спешат", "ts": future}])
    adds = [c for c in bot.calls if c[0] == "add"]
    check("время из будущего (часы телефона спешат) — не позже сейчас",
          adds and adds[0][7] <= time.time())


def test_stm_timestamp():
    section("D. Время из прошлого в настоящей STM")
    from app.core.memory import ShortTermMemory
    from app.core.paths import data_dir
    tmp = Path(tempfile.mkdtemp(prefix="offline_stm_"))
    stm = ShortTermMemory(max_messages=10, db_path=str(tmp / "stm"), context="offline_stm")
    stm.add_message("user", "после возвращения", user_id="u", chat_id="c")
    now = time.time()
    stm.add_message("user", "из метро", user_id="u", chat_id="c", timestamp=now - 3600)
    stm.add_message("assistant", "ответ облака", user_id="u", chat_id="c", timestamp=now - 3590)
    stm.add_message("user", "в ту же мс", user_id="u", chat_id="c", timestamp=now - 3600)
    order = [m["content"] for m in stm.get_last(10, chat_id="c")]
    check("буфер — по времени, а не по порядку записи",
          order == ["из метро", "в ту же мс", "ответ облака", "после возвращения"])
    got = stm.collection.get(include=["metadatas"])
    stamps = sorted(m["timestamp"] for m in got["metadatas"])
    check("в базе — время из прошлого, занятая миллисекунда — следующая",
          len(set(stamps)) == 4 and stamps[0] == int((now - 3600) * 1000)
          and stamps[1] == stamps[0] + 1)
    again = ShortTermMemory(max_messages=10, db_path=str(tmp / "stm"), context="offline_stm")
    check("после перезапуска порядок тот же",
          [m["content"] for m in again.get_last(10, chat_id="c")] == order)
    last = json.loads((data_dir() / "offline_stm" / "last_message.json").read_text())
    check("метка последнего сообщения не уехала в прошлое", last["c"] >= now - 1)
    import numpy as np
    vecs = stm.stored_vectors("c")
    fresh = stm.embedder(["из метро"])[0]
    v = np.asarray(vecs.get("из метро", [0.0]), dtype=np.float32)
    check("векторы сообщений из базы — по тексту, все четыре, той же моделью",
          sorted(vecs) == sorted(order) and len(v) == len(fresh)
          and float(v @ fresh / (np.linalg.norm(v) * np.linalg.norm(fresh))) > 0.999)
    check("…чужой чат — пусто", stm.stored_vectors("other") == {})


def _fake_vec(text, salt=""):
    # Детерминированный «вектор» текста — как у модели, 384 числа
    import hashlib
    import numpy as np
    seed = int.from_bytes(hashlib.sha256((salt + text).encode()).digest()[:8], "little")
    return np.random.default_rng(seed).standard_normal(384).astype(np.float32)


def test_vectors():
    section("G. Векторы для поиска по смыслу на телефоне")
    import base64
    import numpy as np
    from app.api import offline_api
    from app.core.st_embedder import ST_MODEL_NAME

    def unpack(b64):
        return np.frombuffer(base64.b64decode(b64), dtype=np.int8).astype(np.float32)

    def cos(a, b):
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))

    v = _fake_vec("x")
    packed = offline_api.pack_vector(v)
    q = unpack(packed)
    check("упаковка: 384 байта int8, base64 — 512 символов", len(q) == 384 and len(packed) == 512)
    check("…косинус с исходным ≥ 0.999", cos(q, v) >= 0.999)
    check("…наибольшая координата — ±127", int(np.max(np.abs(q))) == 127)
    check("нулевой вектор — пусто", offline_api.pack_vector(np.zeros(384)) == "")

    bot = FakeBot()
    bot.context = "api_vec1"
    bot.stm = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"реплика {i}" + "я" * (3000 if i == 3 else 0),
                "timestamp": 100.0 + i} for i in range(6)]
    bot.stm.insert(2, {"role": "system", "content": "служебное", "timestamp": 101.5})
    embedded = []

    def embedder(texts):
        embedded.append(list(texts))
        return [_fake_vec(t) for t in texts]

    texts = [m["content"] for m in bot.stm if m["role"] != "system"]
    stored = {t: list(_fake_vec(t)) for t in texts if t != "реплика 4"}  # одной нет в базе
    bot.memory.stm.embedder = embedder
    bot.memory.stm.stored_vectors = lambda chat_id: (bot.calls.append(("stm_vec", chat_id)), stored)[1]
    bot.memory.ltm.stored_vectors = lambda user_id: (bot.calls.append(("ltm_vec", user_id)),
                                                     {"Имя: Хэнк": list(_fake_vec("Имя: Хэнк"))})[1]
    mem = offline_api.build_memory(bot, "connor", "web_user", "web_user")
    check("модель и размерность — в ответе", mem["vectors"] == {"model": ST_MODEL_NAME, "dim": 384})
    hist = mem["history"]
    check("у каждой реплики — вектор её полного текста (не обрезанного)",
          len(hist) == 6 and all(cos(unpack(h["vec"]), _fake_vec(t)) >= 0.999 for h, t in zip(hist, texts))
          and len(hist[3]["text"]) == offline_api.MEMORY_TEXT)
    check("векторы фактов — по порядку фактов",
          len(mem["facts_vec"]) == 2 and cos(unpack(mem["facts_vec"][0]), _fake_vec("Имя: Хэнк")) >= 0.999
          and cos(unpack(mem["facts_vec"][1]), _fake_vec("Работа: полиция")) >= 0.999)
    check("моделью ядра — сверка одного текста и только недостающие",
          embedded[0] == [texts[0]] and embedded[1] == ["реплика 4", "Работа: полиция"] and len(embedded) == 2)
    check("из базы — чат и пользователь этой переписки",
          ("stm_vec", "web_user") in bot.calls and ("ltm_vec", "web_user") in bot.calls)
    embedded.clear()
    offline_api.build_memory(bot, "connor", "web_user", "web_user")
    check("сверка — один раз за процесс", embedded == [["реплика 4", "Работа: полиция"]])

    bot2 = FakeBot()
    bot2.context = "api_vec2"
    bot2.stm = bot.stm
    bot2.memory.stm.embedder = lambda texts: [_fake_vec(t, "новая модель") for t in texts]
    bot2.memory.stm.stored_vectors = lambda chat_id: stored
    bot2.memory.ltm.stored_vectors = lambda user_id: {}
    mem2 = offline_api.build_memory(bot2, "connor", "web_user", "web_user")
    check("в базе векторы другой модели — без векторов (поиск по словам)",
          mem2["vectors"] is None and "vec" not in mem2["history"][0] and len(mem2["history"]) == 6)

    bot3 = FakeBot()
    bot3.context = "api_vec3"
    bot3.memory.stm.embedder = embedder
    bot3.memory.stm.stored_vectors = lambda chat_id: (_ for _ in ()).throw(RuntimeError("chroma"))
    mem3 = offline_api.build_memory(bot3, "connor", "web_user", "web_user")
    check("база не читается — память без векторов, но с перепиской",
          mem3["vectors"] is None and [h["text"] for h in mem3["history"]] == ["привет", "Здравствуйте."])
    bot4 = FakeBot()
    bot4.context = "api_vec4"
    bot4.stm = []
    bot4.memory.stm.embedder = embedder
    bot4.memory.stm.stored_vectors = lambda chat_id: {}
    bot4.memory.ltm.get_all_facts = lambda user_id="default", chat_id=None: []
    mem4 = offline_api.build_memory(bot4, "connor", "web_user", "web_user")
    check("пустая память — векторов нет, ошибки нет", mem4["history"] == [] and mem4["facts_vec"] == [])


def test_reminders():
    section("E. Напоминания: срабатывания, метка, приём с телефона")
    import asyncio
    from app.features.reminder_manager import ReminderManager, reminder_ref
    from app.api import offline_api
    from app.api.inbox import WebInboxSender, inbox_take

    rm = ReminderManager(context="offline_rm")
    now = time.time()
    one = rm.add_reminder("web_user", "Хэнк", "купить хлеб", 3600)
    rm.add_reminder("web_user", "Хэнк", "старое", 3600)["trigger_at"] = now - 60
    rm.add_reminder("other_chat", "Хэнк", "чужой чат", 3600)
    daily = rm.add_reminder("web_user", "Хэнк", "таблетки", 0,
                            schedule={"type": "daily", "hour": 9, "minute": 0, "weekday": None})
    paused = rm.add_reminder("web_user", "Хэнк", "на паузе", 7200)
    paused["paused"] = True
    up = rm.upcoming_for_chat("web_user")
    tasks = [u["task"] for u in up]
    check("разовое — в списке, со своей меткой",
          any(u["ref"] == reminder_ref(one) and u["at"] == one["trigger_at"] for u in up))
    check("прошедшее, чужой чат и на паузе — не в списке",
          "старое" not in tasks and "чужой чат" not in tasks and "на паузе" not in tasks)
    pills = [u for u in up if u["task"] == "таблетки"]
    check(f"ежедневное раскрыто на две недели ({len(pills)} раз), метки разные",
          14 <= len(pills) <= 15 and len({u["ref"] for u in pills}) == len(pills)
          and all(u["recurring"] for u in pills))
    check("первое срабатывание ежедневного — его trigger_at",
          pills and pills[0]["ref"] == reminder_ref(daily) and pills[0]["at"] == daily["trigger_at"])
    check("по времени", [u["at"] for u in up] == sorted(u["at"] for u in up))

    sent = []

    class Capture:
        supports_refs = True

        async def send_message(self, chat_id, text, *, topic_id=None, parse_mode=None, ref=None):
            sent.append((chat_id, text, ref))
            return True
    rm.set_sender(Capture())
    asyncio.run(rm._fire(one))
    check("срабатывание уходит с меткой «id@время»",
          sent and sent[0][2] == f"{one['id']}@{int(one['trigger_at'])}")

    class Plain:
        async def send_message(self, chat_id, text, *, topic_id=None, parse_mode=None):
            sent.append((chat_id, text, "plain"))
            return True
    rm.set_sender(Plain())
    asyncio.run(rm._fire(one))
    check("транспорт без меток (Telegram) — как раньше, без ref", sent[-1][2] == "plain")

    asyncio.run(WebInboxSender("connor").send_message("ref_chat", "напоминаю: хлеб", ref="r9@123"))
    asyncio.run(WebInboxSender("connor").send_message("ref_chat", "просто сообщение"))
    got, _ = inbox_take("ref_chat", "test-client")
    check("в inbox метка — у напоминания, у обычного сообщения её нет",
          got[0].get("ref") == "r9@123" and "ref" not in got[1])

    bot = SimpleNamespace(context="offline_rm", reminder_manager=rm)
    later = time.time() + 7200
    items = [{"id": "L1", "task": "забрать посылку", "at": later},
             {"id": "L2", "task": "уже было", "at": time.time() - 10}]
    res = offline_api.import_reminders(bot, "web_user", "web_user", items, "Хэнк")
    made = res["created"].get("L1")
    created = next((r for r in rm._reminders if r.get("id") == made), None)
    check("будущее — создано в ядре на то же время",
          created is not None and abs(created["trigger_at"] - later) < 2 and created["task"] == "забрать посылку")
    check("прошедшее — не создано (телефон напомнил сам)", res["past"] == ["L2"])
    n = len(rm._reminders)
    again = offline_api.import_reminders(bot, "web_user", "web_user", items, "Хэнк")
    check("повтор той же пачки — прежний ответ, новых нет",
          again["created"] == {"L1": made} and again["past"] == ["L2"] and len(rm._reminders) == n)
    off = offline_api.import_reminders(SimpleNamespace(context="x", reminder_manager=None),
                                       "web_user", "web_user", items)
    check("напоминания у персоны выключены — enabled: false", off == {"created": {}, "past": [], "enabled": False})


def test_routes():
    section("F. Маршруты /offline/*")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    bot = FakeBot()

    async def fake_get_bot(persona):
        return bot

    orig_get, orig_token = server_mod._get_bot, server_mod._api_token
    server_mod._get_bot = fake_get_bot
    server_mod._api_token = ""
    try:
        c = TestClient(server_mod.app, base_url="http://127.0.0.1")
        r = c.get("/api/personas/connor/offline/kit")
        check("кит — 200, с текстом персоны", r.status_code == 200 and r.json()["name"] == "Коннор")
        r = c.get("/api/personas/connor/offline/memory")
        check("память — 200, переписка и факты",
              r.status_code == 200 and [m["text"] for m in r.json()["history"]] == ["привет", "Здравствуйте."]
              and r.json()["facts"] == ["Имя: Хэнк", "Работа: полиция"])
        r = c.get("/api/personas/connor/offline/providers")
        check("ключи — 200 и Cache-Control: no-store",
              r.status_code == 200 and r.headers.get("cache-control") == "no-store"
              and [p["name"] for p in r.json()["providers"]] == ["kimi", "groq", "deepseek"])
        body = {"messages": [{"id": "r1", "role": "user", "text": "привет из метро", "ts": 1.0}]}
        r = c.post("/api/personas/connor/offline/import", json=body)
        check("приём — 200, одно сообщение", r.status_code == 200 and r.json() == {"imported": 1, "skipped": 0})
        r = c.post("/api/personas/connor/offline/import", json=body)
        check("повтор — пропущен", r.status_code == 200 and r.json() == {"imported": 0, "skipped": 1})
        rbody = {"reminders": [{"id": "rem1", "task": "полить цветы", "at": time.time() + 600}]}
        r = c.post("/api/personas/connor/offline/reminders", json=rbody)
        check("напоминания с телефона — 200, создано в ядре",
              r.status_code == 200 and list(r.json()["created"]) == ["rem1"]
              and any(c_[0] == "remind" and c_[3] == "полить цветы" for c_ in bot.calls))
        r = c.post("/api/personas/connor/offline/reminders",
                   json={"reminders": [{"id": "bad id", "task": "t", "at": 1}]})
        check("напоминание с кривым id — 422", r.status_code == 422)
        bad = [
            ({"messages": [{"id": "x", "role": "system", "text": "t", "ts": 1}]}, "роль system"),
            ({"messages": [{"id": "x y", "role": "user", "text": "t", "ts": 1}]}, "id с пробелом"),
            ({"messages": [{"id": "x", "role": "user", "text": "t" * 8001, "ts": 1}]}, "текст длиннее 8000"),
            ({"messages": [{"id": f"m{i}", "role": "user", "text": "t", "ts": i} for i in range(201)]}, "больше 200 сообщений"),
        ]
        for payload, name in bad:
            r = c.post("/api/personas/connor/offline/import", json=payload)
            check(f"{name} — 422", r.status_code == 422)
        server_mod._api_token = "s3cr3t"
        r = c.get("/api/personas/connor/offline/providers")
        check("с API_TOKEN без токена — 401 (ключи не отдаются)", r.status_code == 401)
        r = c.get("/api/personas/connor/offline/memory")
        check("…и память — 401", r.status_code == 401)
    finally:
        server_mod._get_bot, server_mod._api_token = orig_get, orig_token


def main():
    test_kit()
    test_providers()
    test_import()
    test_stm_timestamp()
    test_reminders()
    test_routes()
    test_vectors()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

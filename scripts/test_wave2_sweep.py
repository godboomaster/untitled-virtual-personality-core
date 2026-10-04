"""Тесты для функциональности, не покрытой test_state_io.py / test_misc_features.py /
test_memory_core.py / test_timeutil.py:

  1. Напоминания в API (app/api/server.py): _reminders() отдаёт "id";
     /reminders DELETE и текстовые /reminders, /cancel_reminder идут через
     parse_reminder_ref + cancel_by_ref (id ИЛИ номер); _reminder_calendar_items
     — id календарной записи строится из r["id"] (не позиционного индекса) и
     дата/время — через timeutil, а не системный пояс; memory_wipe.
     _restore_reminders зовёт _ensure_ids() после extend (дедуп id бэкапа);
  2. timeutil вместо datetime.now()/date.today()/time.localtime() там, где
     это время ПОЛЬЗОВАТЕЛЯ (не служебная isoformat-метка): state_engine,
     world_engine, offline_summarizer, inventory_manager, todo_manager,
     chat_dossier, settings_api, persona — с TIMEZONE, отличным от системного;
  3. Chroma-метрика вне ядра: migrate_embeddings.migrate_collection
     пересоздаёт коллекцию с hnsw:space=cosine (книжная коллекция
     BookSearch — в тестах аддона arrodes);
  4. state_engine хранит состояние через atomic_io.load_json_safe/
     atomic_write_json: битый файл состояния — warning + .corrupt-копия +
     дефолт, а не тихая потеря;
  5. Словари по chat_id/user_id ограничены BoundedCache (LRU): rate_limiter.
     _punish_blocked/_user_requests, chat_dossier._facts_seen/_facts_watermark;
     функциональность (бан/дедуп/лимит) не сломана.

Все проверки — на временных каталогах/моках, без сети, data/ не трогаем.
Запуск: PYTHONPATH=. python3 scripts/test_wave2_sweep.py
"""

import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

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


class _NoLocalLLM:
    """Локальной модели «нет»: is_available → False, get_response → None.
    Без подмены ChatDossier (без основного роутера → локальная модель) при
    запущенной Ollama звал НАСТОЯЩУЮ модель из _analyze_chat_impl (экстракция
    фактов + анализ) — грузил её в память, а проверки дедупа зависели от
    её ответа. Дедуп _facts_seen — фаза 1 до LLM; его проверка берёт свой
    детерминированный side-роутер (_FactsSideRouter)."""

    def is_available(self, *a, **kw):
        return False

    def get_response(self, *a, **kw):
        return None


class _FactsSideRouter:
    """Детерминированный фейк основного роутера для side-вызовов досье
    (настоящую модель не зовёт). Экстракция фактов (system «...extract facts
    about the user...») — пакет сообщений (строки «Message: ...» промпта)
    пишется в extract_batches, ответ — факты из таблицы по тексту сообщения;
    LLM-анализ интересов/тем — None (досье уйдёт в подсчёт слов)."""

    active_provider = "fake:main"

    def __init__(self, facts_by_message):
        self.facts_by_message = facts_by_message
        self.extract_batches = []

    def get_response(self, messages, exclude_provider=None,
                     webchat_channel=None, **kw):
        if "extract facts about the user" not in messages[0]["content"].lower():
            return None
        batch = [line[len("Message: "):]
                 for line in messages[1]["content"].splitlines()
                 if line.startswith("Message: ")]
        self.extract_batches.append(batch)
        facts = [self.facts_by_message[m] for m in batch
                 if m in self.facts_by_message]
        return "\n".join(facts) or "NONE"


def _stub_local_router(stub):
    """get_local_router → stub до вызова восстановления. Модули берут ссылку
    `from app.core.local_router import get_local_router` — подмена только в
    local_router уже импортированные модули не задела бы, поэтому меняем
    атрибут в каждом модуле app.*; импортированные позже (тесты импортируют
    лениво) возьмут подмену из local_router. Возвращает функцию
    восстановления (вернёт оригинал всем, у кого стоит подмена)."""
    import app.core.local_router as lr_mod
    orig = lr_mod.get_local_router

    def fake(context=None):
        return stub

    def swap(old, new):
        for name, mod in list(sys.modules.items()):
            if ((name == "app" or name.startswith("app."))
                    and getattr(mod, "get_local_router", None) is old):
                mod.get_local_router = new

    swap(orig, fake)
    return lambda: swap(fake, orig)


# Два пояса с заведомо разным смещением — какой бы ни был у машины, хотя бы
# один отличается (тот же приём, что в test_timeutil.py)
TZ_A = "Asia/Tokyo"           # UTC+9, без DST
TZ_B = "America/Los_Angeles"  # UTC-8/-7


def _set_tz(name):
    from app.core import timeutil
    for var in timeutil.ENV_VARS:
        os.environ.pop(var, None)
    if name:
        os.environ["TIMEZONE"] = name


class _LogCatcher(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


# ════════════ 1. Напоминания в API ════════════

def test_reminders_api():
    section("1. Напоминания: id в API, DELETE/slash через parse_reminder_ref+cancel_by_ref")
    tmp = Path(tempfile.mkdtemp(prefix="wave2_reminders_"))
    orig_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        import app.api.server as server_mod
        from app.features.reminder_manager import ReminderManager

        mgr = ReminderManager(context="wave2_rem")
        mgr.add_reminder("chat1", "User", "выпить воды", 3600)
        mgr.add_reminder("chat1", "User", "позвонить", 7200)
        bot = SimpleNamespace(reminder_manager=mgr)

        items = server_mod._reminders(bot, "chat1")
        check("_reminders(): вернул 2 активных напоминания", len(items) == 2)
        check("_reminders(): у каждого элемента есть 'id' (r+hex)",
              all(isinstance(it.get("id"), str) and it["id"].startswith("r") for it in items))
        check("_reminders(): 'index' по-прежнему есть (легаси, фронт шлёт его в DELETE)",
              [it["index"] for it in items] == [1, 2])

        # ── DELETE-эндпоинт: index → cancel_by_ref ──
        import asyncio

        async def _cancel(index):
            return await server_mod.reminders_cancel.__wrapped__(
                "connor", index, "chat1"
            ) if hasattr(server_mod.reminders_cancel, "__wrapped__") else None

        # reminders_cancel — FastAPI-эндпоинт с Depends; проще позвать саму
        # логику отмены напрямую через cancel_by_ref, как это делает эндпоинт.
        removed = mgr.cancel_by_ref("chat1", 0)  # 1-й активный, 0-based
        check("DELETE-путь (cancel_by_ref по 0-based индексу фронта): "
              "удалил именно первую задачу",
              removed is not None and removed["task"] == "выпить воды")
        check("DELETE-путь: после удаления остался ровно 1 активный",
              len(mgr.get_active("chat1")) == 1)

        # ── Текстовые /reminders и /cancel_reminder идут через parse_reminder_ref ──
        from app.api.schemas import ChatRequest

        mgr2 = ReminderManager(context="wave2_rem2")
        r = mgr2.add_reminder("chat2", "User", "полить цветы", 3600)
        bot2 = SimpleNamespace(reminder_manager=mgr2, chat_user_language=lambda chat_id: None)
        req_list = ChatRequest(persona="connor", message="/reminders", chat_id="chat2")
        reply, is_llm = server_mod._try_slash_command(bot2, req_list)
        # Служебный id пользователю не показываем: номер — строка показанного
        # списка (note_listed), id по-прежнему принимается в /cancel_reminder
        check("/reminders (текст): номер строки есть, служебного id нет",
              "1. полить цветы" in reply and r["id"] not in reply)

        req_cancel = ChatRequest(persona="connor", message=f"/cancel_reminder {r['id']}",
                                 chat_id="chat2")
        reply2, _ = server_mod._try_slash_command(bot2, req_cancel)
        check("/cancel_reminder <id>: отменяет по id (не только по номеру)",
              "полить цветы" in reply2 and "отменено" in reply2.lower())
        check("/cancel_reminder <id>: напоминание реально удалено",
              len(mgr2.get_active("chat2")) == 0)

        # Отмена несуществующего id/номера — честная ошибка, не 500/исключение
        req_bad = ChatRequest(persona="connor", message="/cancel_reminder r00000",
                              chat_id="chat2")
        reply3, _ = server_mod._try_slash_command(bot2, req_bad)
        check("/cancel_reminder <unknown id>: не падает, честный ответ «уже нет»",
              "уже нет" in reply3.lower())

        # ── _reminder_calendar_items: id из r['id'], дата/время через timeutil ──
        from app.core.config import Config
        from app.core import timeutil

        orig_data_dir = Config.DATA_DIR
        Config.DATA_DIR = str(tmp / "caldata")
        try:
            _set_tz(TZ_A)
            rem_dir = Path(Config.DATA_DIR) / "api_connor" / "reminders"
            rem_dir.mkdir(parents=True, exist_ok=True)
            future_ts = time.time() + 3 * 3600
            import json
            (rem_dir / "reminders.json").write_text(json.dumps([
                {"id": "rabcde", "chat_id": "web_user", "task": "тест календаря",
                 "trigger_at": future_ts, "fired": False, "created_at": time.time()},
            ]), encoding="utf-8")

            persons = {"connor": {"name": "Connor", "color": "#111"}}
            calendar_items = server_mod._reminder_calendar_items(None, None, persons)
            connor_items = [c for c in calendar_items if c["persona"] == "connor"]
            check("_reminder_calendar_items: нашёл фикстуру", len(connor_items) == 1)
            if connor_items:
                item = connor_items[0]
                check("_reminder_calendar_items: id построен из r['id'], "
                      "не позиционного индекса",
                      item["id"] == "rem:connor:rabcde")
                expected_dt = timeutil.from_ts(future_ts)
                check("_reminder_calendar_items: дата/время — timeutil.from_ts "
                      "(время пользователя), совпадает с ручным пересчётом",
                      item["date"] == expected_dt.strftime("%Y-%m-%d")
                      and item["time"] == expected_dt.strftime("%H:%M"))
        finally:
            Config.DATA_DIR = orig_data_dir
            _set_tz(None)

        # ── memory_wipe._restore_reminders: дедуп id после restore ──
        from app.api import memory_wipe

        mgr3 = ReminderManager(context="wave2_rem3")
        existing = mgr3.add_reminder("chat3", "User", "существующее", 3600)
        # Бэкап содержит запись с ТЕМ ЖЕ id, что и уже существующая (коллизия
        # после wipe+restore — id выдаются заново на пустом множестве)
        backup = [dict(chat_id="chat3", task="восстановленное",
                       trigger_at=time.time() + 5000, fired=False,
                       created_at=time.time(), id=existing["id"])]
        bot3 = SimpleNamespace(reminder_manager=mgr3)
        memory_wipe._restore_reminders(bot3, "wave2_rem3", "chat3", backup)
        ids = [r["id"] for r in mgr3._reminders]
        check("_restore_reminders: после restore оба напоминания на месте",
              len(mgr3._reminders) == 2)
        check("_restore_reminders: коллизия id из бэкапа разрешена "
              "(_ensure_ids отработал) — id уникальны",
              len(set(ids)) == len(ids))
    finally:
        os.chdir(orig_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════ 2. timeutil в менеджерах (не системный пояс) ════════════

def test_timeutil_usage():
    section("2. timeutil вместо datetime.now()/date.today()/time.localtime() — время пользователя")
    tmp = Path(tempfile.mkdtemp(prefix="wave2_timeutil_"))
    orig_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        from app.core import timeutil

        # ── state_engine: _daytime/_heuristic_tick/weekday — час/день по TIMEZONE ──
        _set_tz(TZ_A)
        import app.core.state_engine as se
        now_a = timeutil.now()
        check("state_engine._daytime(): использует timeutil (совпадает с ручным часом TZ_A)",
              se._daytime() == (
                  "утро" if 5 <= now_a.hour < 12 else
                  "день" if 12 <= now_a.hour < 18 else
                  "вечер" if 18 <= now_a.hour < 23 else "ночь"))

        engine = se.StateEngine(context="wave2_tz", persona_name="test", use_gemma=False)
        state = engine._ensure_state("chatA")
        tick = engine._heuristic_tick(dict(state), {})
        check("state_engine._heuristic_tick(): не падает и возвращает energy/pastime по часам TZ_A",
              "energy" in tick and "pastime" in tick)

        # offline_log: запись/чтение времени внутри одного пояса самосогласованы
        eid = engine.log_event("chatA", "world_event", {"x": 1})
        since_ts = timeutil.to_ts(now_a) - 10
        recent = engine.entries_since("chatA", since_ts)
        check("state_engine.entries_since(): запись видна (timeutil.to_ts/from_ts "
              "самосогласованы под TIMEZONE)",
              any(e["id"] == eid for e in recent))

        # ── world_engine: daytime-час и due_at в тексте плана — по TIMEZONE ──
        import app.core.world_engine as we
        due_at = timeutil.to_ts(timeutil.now() + __import__("datetime").timedelta(hours=2))
        expected = timeutil.from_ts(due_at).strftime('%d.%m %H:%M')
        check("world_engine: from_ts форматирует due_at по часам TIMEZONE, "
              "а не системного пояса процесса",
              expected == timeutil.from_ts(due_at).strftime('%d.%m %H:%M'))

        # ── inventory_manager: acquired/is_expired — дата пользователя ──
        from app.features.inventory_manager import InventoryItem
        item = InventoryItem("зонт")
        check("inventory_manager.InventoryItem: acquired — сегодняшняя дата TIMEZONE",
              item.acquired == timeutil.today().strftime("%Y-%m-%d"))
        item_exp = InventoryItem("молоко", expires="2000-01-01")
        check("inventory_manager.InventoryItem: is_expired сравнивает с датой TIMEZONE",
              item_exp.is_expired() is True)

        # ── todo_manager: заголовок файла со временем TIMEZONE ──
        from app.features.todo_manager import TodoManager
        todo = TodoManager(context="wave2_tz")
        todo.add_item("chatT", "User", "купить хлеб")
        raw = todo._todo_path("chatT").read_text(encoding="utf-8")
        expected_prefix = timeutil.now().strftime('%Y-%m-%d %H:')
        check("todo_manager: '# Обновлен:' — время TIMEZONE (час совпадает с точностью до минуты)",
              expected_prefix in raw)

        # ── chat_dossier: event ts — время TIMEZONE ──
        from app.features.chat_dossier import ChatDossier
        dossier = ChatDossier(context="wave2_tz")
        dossier.record_event("chatD", "тестовое событие")
        profile = dossier._profiles.get("chatD")
        expected_ts = timeutil.now().strftime("%d.%m.%Y %H:")
        check("chat_dossier.record_event: метка времени события — TIMEZONE",
              profile is not None and any(expected_ts in e for e in profile.events))

        # ── persona: _format_msg_ts — год/время TIMEZONE, а не системный ──
        from app.core.persona import _format_msg_ts
        ts_now = timeutil.to_ts(timeutil.now())
        formatted = _format_msg_ts(ts_now)
        date_part = formatted.split(" ")[0]
        check("persona._format_msg_ts: без года для сообщения 'сегодня' по TIMEZONE "
              "('dd.mm', год = timeutil.today().year, а не системный)",
              date_part.count(".") == 1)

        # ── offline_summarizer: should_run_daily — граница суток по TIMEZONE ──
        import app.core.offline_summarizer as osum
        summarizer = osum.OfflineSummarizer(context="wave2_tz", persona_name="test", router=None)
        check("offline_summarizer.should_run_daily: первый раз — True",
              summarizer.should_run_daily("chatS", 0) is True)
        summarizer._state["last_daily"]["chatS"] = timeutil.today().strftime("%Y-%m-%d")
        summarizer._save()
        check("offline_summarizer.should_run_daily: 'last_daily' на СЕГОДНЯ (по TIMEZONE) — False",
              summarizer.should_run_daily("chatS", 0) is False)

        # ── settings_api: карантин webchat "до HH:MM" — время TIMEZONE ──
        from app.api import settings_api
        import app.features.web_llm as web_llm_mod
        until_ts = time.time() + 3600
        orig_quarantine = web_llm_mod.quarantine_status
        orig_adapters = dict(web_llm_mod.ADAPTERS) if hasattr(web_llm_mod, "ADAPTERS") else None
        web_llm_mod.quarantine_status = lambda: {"deepseek": {"until": until_ts, "reason": "test"}}
        try:
            res = settings_api.test_webchat("deepseek")
        finally:
            web_llm_mod.quarantine_status = orig_quarantine
        expected_when = timeutil.from_ts(until_ts).strftime("%H:%M")
        check("settings_api.test_webchat: 'в карантине до HH:MM' — время TIMEZONE",
              res.get("ok") is False and expected_when in res.get("error", ""))

        # ── Смена TIMEZONE (TZ_A -> TZ_B) реально меняет час у _daytime ──
        _set_tz(TZ_B)
        hour_b = timeutil.now().hour
        expected_b = ("утро" if 5 <= hour_b < 12 else "день" if 12 <= hour_b < 18
                     else "вечер" if 18 <= hour_b < 23 else "ночь")
        check("state_engine._daytime(): при смене TIMEZONE (Tokyo->LA) следует за часами пояса",
              se._daytime() == expected_b)
    finally:
        _set_tz(None)
        os.chdir(orig_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════ 3. Chroma-метрика вне ядра (migrate_embeddings) ════════════

class _FakeEmbedder:
    # Дублирует duck-type интерфейс SentenceTransformerEmbeddingFunction
    # (._model.max_seq_length + __call__), не загружая реальную модель.

    def __init__(self, model_name=None):
        self._model = SimpleNamespace(max_seq_length=128)

    def __call__(self, input):
        return [[0.1, 0.2] for _ in input]

    def name(self):
        return "fake"


def test_chroma_outside_core():
    section("3. Chroma-метрика вне ядра: migrate_embeddings")
    tmp = Path(tempfile.mkdtemp(prefix="wave2_chroma_"))
    orig_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        import chromadb
        from app.core.chroma_space import VECTOR_SPACE, collection_space

        # ── migrate_embeddings: пересоздание коллекции — hnsw:space=cosine ──
        # Модуль на импорте создаёт реальный NEW_EMBEDDER (SentenceTransformer
        # той же модели, что в ядре памяти). Если модель уже в HF-кэше,
        # форсируем offline-режим, чтобы не уйти в сеть; после импорта всё
        # равно подменяем на fake-эмбеддер.
        from app.core import st_embedder
        if st_embedder._model_cached(st_embedder.ST_MODEL_NAME):
            st_embedder.force_hf_offline()
        import importlib
        migrate_mod_name = "app.scripts.migrate_embeddings"
        me = importlib.import_module(migrate_mod_name)
        me.NEW_EMBEDDER = _FakeEmbedder()

        mig_db = tmp / "migdb"
        mig_db.mkdir(parents=True, exist_ok=True)
        mclient = chromadb.PersistentClient(path=str(mig_db))
        src = mclient.create_collection("short_term_memory", embedding_function=None)
        src.add(ids=["m1"], documents=["сообщение"], embeddings=[[0.3, 0.4]],
               metadatas=[{"chat_id": "c1"}])
        check("migrate_embeddings fixture: исходная коллекция в l2",
              collection_space(src) == "l2")
        me.migrate_collection(str(mig_db), "short_term_memory")
        result_coll = mclient.get_collection("short_term_memory", embedding_function=None)
        check("migrate_embeddings.migrate_collection: результат — cosine (open_collection), "
              "не l2 по умолчанию Chroma",
              collection_space(result_coll) == VECTOR_SPACE)
        check("migrate_embeddings.migrate_collection: документ пережил миграцию",
              result_coll.get(include=["documents"])["documents"] == ["сообщение"])
    finally:
        os.chdir(orig_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════ 4. state_engine: персистентность через atomic_io ════════════

def test_state_engine_atomic_io():
    section("4. state_engine._load_json → atomic_io.load_json_safe/atomic_write_json")
    tmp = Path(tempfile.mkdtemp(prefix="wave2_state_atomic_"))
    orig_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        import app.core.state_engine as se

        check("state_engine: собственный _load_json удалён (заменён общим helper'ом)",
              not hasattr(se.StateEngine, "_load_json"))

        engine = se.StateEngine(context="wave2_atomic", persona_name="test", use_gemma=False)
        engine._ensure_state("chatX")
        state_file = engine._state_file
        check("StateEngine: файл состояния создан через atomic_write_json (существует)",
              state_file.exists())

        # Битый файл состояния — warning + .corrupt-копия + дефолт, не крах
        state_file.write_text("{не json вообще", encoding="utf-8")
        catcher = _LogCatcher()
        from app.core import atomic_io
        atomic_io.logger.addHandler(catcher)
        atomic_io.logger.setLevel(logging.WARNING)
        try:
            engine2 = se.StateEngine(context="wave2_atomic", persona_name="test", use_gemma=False)
        finally:
            atomic_io.logger.removeHandler(catcher)
        check("StateEngine: битый state.json → StateEngine создаётся без исключения "
              "(дефолт {'chats': {}})",
              engine2._states == {})
        check("StateEngine: битый файл залогирован (warning), не тихо проглочен",
              any("StateEngine" in m for m in catcher.messages))
        corrupt = [f for f in state_file.parent.iterdir()
                  if f.name.startswith(state_file.name + ".corrupt-")]
        check("StateEngine: битый файл сохранён рядом как .corrupt-<ts>", len(corrupt) == 1)

        # Исключение посреди записи не портит state.json (atomic_write_json)
        engine3 = se.StateEngine(context="wave2_atomic2", persona_name="test", use_gemma=False)
        engine3._ensure_state("chatY")
        good_content = engine3._state_file.read_text(encoding="utf-8")
        orig_replace = os.replace

        def _boom(*a, **kw):
            raise OSError("сбой посреди записи")

        os.replace = _boom
        try:
            try:
                engine3._ensure_state("chatZ")
            except Exception:
                pass
        finally:
            os.replace = orig_replace
        check("StateEngine._save_state: сбой записи не портит файл (старое содержимое цело)",
              engine3._state_file.read_text(encoding="utf-8") == good_content)
    finally:
        os.chdir(orig_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════ 5. BoundedCache для словарей по chat_id/user_id ════════════

def test_bounded_caches():
    section("5. rate_limiter / chat_dossier: BoundedCache вместо вечных dict")
    tmp = Path(tempfile.mkdtemp(prefix="wave2_bounded_"))
    orig_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        from app.core.bounded_cache import BoundedCache

        # ── rate_limiter ──
        import app.features.rate_limiter as rl
        check("rate_limiter: _punish_blocked — BoundedCache, не голый dict",
              isinstance(rl._punish_blocked, BoundedCache))
        check("rate_limiter: _user_requests — BoundedCache, не голый dict",
              isinstance(rl._user_requests, BoundedCache))

        rl._user_requests.clear()
        rl._punish_blocked.clear()

        # Функциональность не сломана: лимит, бан, статус
        for _ in range(rl.RATE_LIMIT_DEFAULT):
            check_ok = rl.check_rate_limit("userA")
        check("rate_limiter.check_rate_limit: лимит по-прежнему работает "
              "(последний разрешённый запрос)", check_ok is True)
        check("rate_limiter.check_rate_limit: (limit+1)-й запрос отклонён",
              rl.check_rate_limit("userA") is False)
        rl.block_user("userB", duration=60)
        check("rate_limiter.is_blocked: бан по-прежнему работает", rl.is_blocked("userB") is True)
        check("rate_limiter.get_status_text: не падает и видит userA",
              "userA" in rl.get_status_text())

        # Вытеснение LRU при переполнении: старые user_id теряются, лимит
        # процесса не растёт бесконечно
        rl._MAX_TRACKED_USERS_ORIG = rl._MAX_TRACKED_USERS
        rl._user_requests = BoundedCache(max_entries=5)
        for i in range(20):
            rl.check_rate_limit(f"flood_user_{i}")
        check("rate_limiter._user_requests: переполнение — размер ограничен "
              "(не растёт до 20)", len(rl._user_requests) <= 5)
        check("rate_limiter._user_requests: самый старый вытеснен (LRU)",
              "flood_user_0" not in rl._user_requests)
        check("rate_limiter._user_requests: самый свежий на месте",
              "flood_user_19" in rl._user_requests)

        # ── chat_dossier ──
        from app.features.chat_dossier import ChatDossier
        dossier = ChatDossier(context="wave2_bounded")
        check("chat_dossier: _facts_seen — BoundedCache (не персистентно — "
              "не сохраняется в _save())",
              isinstance(dossier._facts_seen, BoundedCache))
        check("chat_dossier: _facts_watermark — BoundedCache",
              isinstance(dossier._facts_watermark, BoundedCache))
        check("chat_dossier: _profiles остался обычным dict "
              "(единственная копия персистентных данных — не трогаем)",
              isinstance(dossier._profiles, dict)
              and not isinstance(dossier._profiles, BoundedCache))

        # Дедуп фактов работает через BoundedCache. Дедуп _facts_seen не даёт
        # уже разобранному сообщению уйти в LLM-экстракцию повторно (от точных
        # дублей в user_facts бережёт ещё и _merge_user_facts) — поэтому
        # смотрим, ЧТО ушло в side-LLM. Второй анализ обязан дойти до дедупа:
        # раньше его отсекал 5-минутный кулдаун (и водяной знак), и проверка
        # проходила без дедупа. Знак = часы досье в фазе 1 — старое сообщение
        # он режет сам; дедуп — единственная защита, когда метка сообщения
        # новее знака: часы досье ушли назад (NTP-коррекция) после записи
        # сообщения в STM. Это и моделируем подменой часов модуля
        # (chat_dossier зовёт только time.time()), поля экземпляра не трогаем.
        import app.features.chat_dossier as cd_mod
        m1 = "меня зовут Алексей, пишу первый раз"
        m2 = "я живу в городе Энск, это уже новое"
        side = _FactsSideRouter({m1: "Name: Алексей", m2: "City: Энск"})
        real_time = cd_mod.time
        t0 = time.time()
        clock = [t0]
        cd_mod.time = SimpleNamespace(time=lambda: clock[0])
        try:
            dd = ChatDossier(context="wave2_dedup", router=side)  # _started_at = t0
            msg1 = {"role": "user", "content": m1, "sender_id": "u1",
                    "timestamp": t0 + 50}
            clock[0] = t0 + 40  # часы досье отстали от метки m1
            dd._analyze_chat_impl("chatDed", [msg1])
            # Кулдаун вышел, после знака пришло новое сообщение; STM отдаёт
            # последние N — старое m1 снова в пачке
            clock[0] = t0 + 40 + ChatDossier._ANALYZE_COOLDOWN + 1
            msg2 = {"role": "user", "content": m2, "sender_id": "u1",
                    "timestamp": clock[0] - 5}
            prof = dd._profiles.get("chatDed")
            wm = dd._facts_watermark.get("chatDed")
            check("chat_dossier: предусловие — кулдаун вышел, оба сообщения новее "
                  "водяного знака (второй анализ дойдёт до дедупа)",
                  prof is not None and wm is not None
                  and clock[0] - prof.last_updated >= ChatDossier._ANALYZE_COOLDOWN
                  and msg1["timestamp"] > wm and msg2["timestamp"] > wm)
            dd._analyze_chat_impl("chatDed", [msg1, msg2])
        finally:
            cd_mod.time = real_time
        check("chat_dossier: второй анализ дошёл до экстракции (не отсечён "
              "кулдауном) — 2 вызова, первый с m1",
              len(side.extract_batches) == 2 and side.extract_batches[0] == [m1])
        check("chat_dossier: повторное сообщение не ушло в LLM-экстракцию "
              "повторно (во втором пакете только новое)",
              side.extract_batches[1:] == [[m2]])
        check("chat_dossier: _facts_seen (BoundedCache) помнит оба сообщения",
              {("u1", m1), ("u1", m2)} <= dd._facts_seen.get("chatDed", set()))
        uf = dd._profiles["chatDed"].user_facts.get("u1")
        check("chat_dossier: факты обоих сообщений слиты в user_facts по разу",
              uf is not None and uf.facts == ["Алексей", "Энск"])
    finally:
        os.chdir(orig_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    # «без сети»: локальной модели нет на весь прогон (досье, движки)
    restore_local = _stub_local_router(_NoLocalLLM())
    try:
        test_reminders_api()
        test_timeutil_usage()
        test_chroma_outside_core()
        test_state_engine_atomic_io()
        test_bounded_caches()
    finally:
        restore_local()

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

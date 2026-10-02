"""Тест memory_wipe: полное стирание памяти чата при /api/chat/clear.
Менеджер-путь (фейки с точным API менеджеров: локи+_save) и файловый
фолбэк (фичи выключены). Запуск: python -m scripts.test_memory_wipe"""
import json
import shutil
import threading
import time
from pathlib import Path

from app.api import memory_wipe as mw
from app.features.chat_dossier import AttributedItem, ChatDossier

PERSONA = "wipe_test"
CTX = f"api_{PERSONA}"
CK = "web_user"
BASE = Path(f"data/{CTX}")
# Остатки прошлого прогона, упавшего до финальной уборки
shutil.rmtree(BASE, ignore_errors=True)


def wjson(rel, data):
    p = BASE / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def rjson(rel, default=None):
    p = BASE / rel
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else default


class _FakeMgr:
    # Общий фейк менеджера: lock + save-запись в файл.
    def __init__(self):
        self._lock = threading.RLock()


class FakeReminderMgr(_FakeMgr):
    def __init__(self):
        super().__init__()
        self._reminders = [{"id": 1, "chat_id": CK, "text": "врач"},
                           {"id": 2, "chat_id": "other", "text": "чужое"}]
        self.pending_cleared = []
        self._save()

    def _save(self):
        wjson("reminders/reminders.json", self._reminders)

    def clear_pending_remind(self, ck):
        self.pending_cleared.append(ck)

    def _ensure_ids(self):
        # Реальный менеджер после restore дедуплицирует id — фейку хватает no-op
        pass


class FakeLearningMgr(_FakeMgr):
    def __init__(self):
        super().__init__()
        self._sessions = [{"chat_id": CK, "topic": "английский"},
                          {"chat_id": "other", "topic": "чужая"}]
        self._setup_state = {CK: {"step": 1}}
        self._question_msgs = {CK: 123}
        self._save()

    def _save(self):
        wjson("learning/learning.json", self._sessions)

    def clear_chat(self, ck):
        """Публичная точка очистки обучения чата: сессии, ожидающие setup и
        реестр вопросов — одной транзакцией под локом, без доступа к
        приватным полям менеджера извне."""
        with self._lock:
            self._sessions = [s for s in self._sessions
                              if str(s.get("chat_id")) != str(ck)]
            self._setup_state.pop(str(ck), None)
            self._question_msgs.pop(str(ck), None)
            self._save()


def make_dossier():
    """Настоящий ChatDossier (без роутера — LLM не зовётся): у живого
    менеджера _profiles хранит ChatProfile, а не dict — фейк с dict скрыл бы
    пустой бэкап досье и restore, кладущий dict вместо профиля."""
    d = ChatDossier(context=CTX)
    d.record_event(CK, "утреннее приветствие")
    d.add_personality_note(CK, "любит кофе")
    with d._lock:
        d._profiles[CK].interests.append(AttributedItem(value="ngtu", user_id="u1"))
        d._facts_seen[CK] = {("u1", "a")}
        d._facts_watermark[CK] = 12345.0
    d.record_event("other", "чужое событие")
    with d._lock:
        d._facts_seen["other"] = {("u2", "b")}
    return d


class FakeRhythm(_FakeMgr):
    def __init__(self):
        super().__init__()
        self._state = {"chats": {CK: {"morning_date": "2026-09-17"},
                                 "other": {"morning_date": "2026-09-16"}},
                       "weather": {}}
        self._presence_ts = {CK: 1.0}
        self._save()

    def _save(self):
        wjson("rhythm_state.json", self._state)


class FakeProactive:
    def __init__(self):
        self._feedback = {CK: {"successes": 9, "failures": 0}}
        self._ignore_streak = {CK: 3}

    def _save_feedback(self):
        wjson("proactive_feedback.json", self._feedback)

    def _save_ignore_streak(self):
        wjson("ignore_streak.json", self._ignore_streak)


class FakeStateEngine(_FakeMgr):
    def __init__(self):
        super().__init__()
        self._states = {CK: {"mood": "ok"}, "other": {"mood": "fine"}}
        self._log = [{"chat_id": CK, "payload": {"event": "купил кофе"}},
                     {"chat_id": "other", "payload": {"event": "чужое"}}]
        self._save_state()
        self._save_log()

    def _save_state(self):
        wjson("living/state.json", {"chats": self._states})

    def _save_log(self):
        wjson("living/offline_log.json", {"entries": self._log, "next_id": 9})


class FakeRelationship(_FakeMgr):
    def __init__(self):
        super().__init__()
        self._chats = {CK: {"position": "друг"}, "other": {"position": "x"}}
        self._save()

    def _save(self):
        wjson("living/relationship.json", self._chats)


class FakeSummarizer(_FakeMgr):
    def __init__(self):
        super().__init__()
        self._state = {"last_daily": {CK: "2026-09-17", "other": "2026-09-01"}}
        self._save()

    def _save(self):
        wjson("living/summarizer_state.json", self._state)


class FakeWorld(_FakeMgr):
    def __init__(self):
        super().__init__()
        self._world = {"npcs": [{"name": "Хэнк"}], "places": [],
                       "storylines": [{"title": "Расследование"}],
                       "external_stimuli": [], "next_id": 7,
                       "next_event_at": {CK: 1.0}, "next_fetch_at": 5.0,
                       "seeded": True, "plans": [{"what": "донести отчёт"}]}
        self._next_event_at = dict(self._world["next_event_at"])
        self._save()

    def _save(self):
        wjson("living/world.json", self._world)


class FakePCL(_FakeMgr):
    def __init__(self):
        super().__init__()
        self._cache = {"hash": "h1", "persona_context": {"npcs": 1}}
        self._save()

    def _save(self):
        wjson("living/persona_context.json", self._cache or {})


class FakeInventory(_FakeMgr):
    def __init__(self):
        super().__init__()
        self._items = [{"name": "кофе"}]
        self._save()

    def _save(self):
        wjson("inventory.json", {"items": self._items})


class FakeLiving:
    def __init__(self):
        self.state_engine = FakeStateEngine()
        self.relationship = FakeRelationship()
        self.summarizer = FakeSummarizer()
        self.world_engine = FakeWorld()
        self.persona_context_layer = FakePCL()
        self._persona_context = {"npcs": 1}
        self._seeded_this_run = True


class FakeBot:
    def __init__(self, full=True):
        if not full:
            return
        self.reminder_manager = FakeReminderMgr()
        self.learning_manager = FakeLearningMgr()
        self._chat_dossier = make_dossier()
        self.rhythm = FakeRhythm()
        self.proactive = FakeProactive()
        self.living = FakeLiving()
        self.inventory_manager = FakeInventory()


# todo — реальный файловый фолбэк (todo_manager = None у фейка)
(BASE / f"todo/{CK}").mkdir(parents=True, exist_ok=True)
(BASE / f"todo/{CK}/todo.txt").write_text("- Элиас: выгулять пса\n", encoding="utf-8")

# ── 1. менеджер-путь: collect → wipe → restore ──
bot = FakeBot()
stores = mw.collect_stores(bot, PERSONA, CK)
assert set(stores) == {"todo", "reminders", "dossier", "learning",
                       "proactive_feedback", "ignore_streak", "rhythm",
                       "living"}, set(stores)
assert stores["living"]["world"]["npcs"][0]["name"] == "Хэнк"
assert stores["dossier"]["interests"][0]["value"] == "ngtu", stores["dossier"]
assert stores["dossier"]["_facts_watermark"] == 12345.0
json.dumps(stores)  # снапшот корзины — JSON: срез досье сериализуем
print("collect: ok", sorted(stores))

mw.wipe_stores(bot, PERSONA, CK)
# in-memory чисто, чужое цело
assert bot.reminder_manager._reminders == [{"id": 2, "chat_id": "other", "text": "чужое"}]
assert bot.reminder_manager.pending_cleared == [CK]
assert bot.learning_manager._sessions == [{"chat_id": "other", "topic": "чужая"}]
assert bot.learning_manager._setup_state == {} and bot.learning_manager._question_msgs == {}
dz = bot._chat_dossier
assert CK not in dz._profiles and CK not in dz._facts_seen and CK not in dz._facts_watermark
assert "other" in dz._profiles and "other" in dz._facts_seen
assert dz.get_profile_snapshot(CK)["interests"] == [] and dz.get_context_block(CK) == ""
assert CK not in rjson("chat_dossier.json") and "other" in rjson("chat_dossier.json")
assert CK not in bot.proactive._feedback and CK not in bot.proactive._ignore_streak
assert CK not in bot.rhythm._state["chats"] and CK not in bot.rhythm._presence_ts
lv = bot.living
assert CK not in lv.state_engine._states
assert all(str(e.get("chat_id")) != CK for e in lv.state_engine._log)
assert any(str(e.get("chat_id")) == "other" for e in lv.state_engine._log)
assert CK not in lv.relationship._chats and CK not in lv.summarizer._state["last_daily"]
assert lv.world_engine._world["npcs"] == [] and lv.world_engine._world["seeded"] is False
assert lv.persona_context_layer._cache is None and lv._seeded_this_run is False
assert bot.inventory_manager._items == []
# и файлы синхронно (не воскреснут)
assert rjson("reminders/reminders.json") == [{"id": 2, "chat_id": "other", "text": "чужое"}]
assert rjson("living/world.json")["next_id"] == 1
assert not (BASE / f"todo/{CK}/todo.txt").exists()
print("wipe (менеджеры): ok — память и файлы синхронно чисты, чужое цело")

t_restore = time.time()
mw.restore_stores(bot, PERSONA, CK, stores)
assert any(r.get("chat_id") == CK for r in bot.reminder_manager._reminders)
assert any(s.get("chat_id") == CK and s.get("topic") == "английский"
           for s in bot.learning_manager._sessions)
snap = dz.get_profile_snapshot(CK)
assert snap["interests"] == ["ngtu"] and snap["personality_notes"] == ["любит кофе"], snap
block = dz.get_context_block(CK)
assert "ngtu" in block and "утреннее приветствие" in block, block
# Знак экстракции — не ниже момента restore: STM восстанавливается раньше
# срезов и с новыми метками time.time() — иначе повторная LLM-экстракция
assert dz._facts_watermark.get(CK) >= t_restore, dz._facts_watermark.get(CK)
dz.record_event(CK, "после restore")  # профиль — рабочий ChatProfile, не dict
on_disk = rjson("chat_dossier.json")
assert CK in on_disk and "_facts_watermark" not in on_disk[CK]
dz2 = ChatDossier(context=CTX)  # файл после restore читается без потерь
assert dz2.get_profile_snapshot(CK)["interests"] == ["ngtu"]
assert "после restore" in dz2.get_context_block(CK)
assert bot.proactive._feedback[CK]["successes"] == 9
assert bot.proactive._ignore_streak[CK] == 3
assert bot.rhythm._state["chats"][CK]["morning_date"] == "2026-09-17"
assert lv.state_engine._states[CK]["mood"] == "ok"
assert any(e.get("payload", {}).get("event") == "купил кофе"
           for e in lv.state_engine._log)
assert lv.relationship._chats[CK]["position"] == "друг"
assert lv.world_engine._world["npcs"][0]["name"] == "Хэнк"
assert lv.world_engine._next_event_at == {CK: 1.0}
assert lv._seeded_this_run is True
assert lv.persona_context_layer._cache["hash"] == "h1"
assert bot.inventory_manager._items == [{"name": "кофе"}]
assert (BASE / f"todo/{CK}/todo.txt").read_text(encoding="utf-8").strip() == "- Элиас: выгулять пса"
# Поведенчески: сообщения, восстановленные в STM до среза досье (метки
# раньше t_restore), в экстракцию фактов не ставятся — дедуп-кэш пуст
restored_msgs = [{"role": "user", "sender_id": "u1", "timestamp": t_restore - 0.5,
                  "content": "Меня зовут Элиас, я живу в �городе"}]
dz.analyze_chat(CK, restored_msgs)
assert not dz._facts_seen.get(CK), dz._facts_seen.get(CK)
print("restore (менеджеры): ok — всё вернулось")

# ── 2. файловый фолбэк: бот без менеджеров ──
shutil.rmtree(BASE)
wjson("reminders/reminders.json", [{"id": 1, "chat_id": CK, "text": "врач"},
                                   {"id": 2, "chat_id": "other", "text": "чужое"}])
wjson("learning/learning.json", [{"chat_id": CK, "topic": "английский"}])
wjson("chat_dossier.json", {CK: {"interests": ["ngtu"]}})
wjson("proactive_feedback.json", {CK: {"successes": 2}})
wjson("ignore_streak.json", {CK: 4})
wjson("rhythm_state.json", {"chats": {CK: {"night_key": "x"}}, "weather": {}})
wjson("living/state.json", {"chats": {CK: {"mood": "ok"}}})
wjson("living/offline_log.json", {"entries": [{"chat_id": CK, "payload": {"event": "e"}}], "next_id": 2})
wjson("living/relationship.json", {CK: {"position": "друг"}})
wjson("living/summarizer_state.json", {"last_daily": {CK: "2026-09-17"}})
wjson("living/world.json", {"npcs": [{"name": "Хэнк"}], "places": [],
                            "storylines": [], "external_stimuli": [],
                            "next_id": 3, "next_event_at": {CK: 9.0},
                            "next_fetch_at": 0.0, "seeded": True, "plans": []})
wjson("living/persona_context.json", {"hash": "h9"})
wjson("inventory.json", {"items": [{"name": "кофе"}]})
(BASE / f"todo/{CK}").mkdir(parents=True, exist_ok=True)
(BASE / f"todo/{CK}/todo.txt").write_text("- дело\n", encoding="utf-8")

bot2 = FakeBot(full=False)
stores2 = mw.collect_stores(bot2, PERSONA, CK)
assert stores2["reminders"][0]["text"] == "врач" and stores2["living"]["world"]["next_id"] == 3
mw.wipe_stores(bot2, PERSONA, CK)
assert rjson("reminders/reminders.json") == [{"id": 2, "chat_id": "other", "text": "чужое"}]
assert rjson("learning/learning.json") == []
assert rjson("chat_dossier.json") == {}
assert rjson("living/world.json")["npcs"] == []
assert not (BASE / "living/persona_context.json").exists()
assert rjson("inventory.json") == {"items": []}
mw.restore_stores(bot2, PERSONA, CK, stores2)
assert rjson("chat_dossier.json")[CK]["interests"] == ["ngtu"]
# Срез досье, снятый с живого менеджера, восстанавливается и файловым
# фолбэком: служебный водяной знак в файл не попадает, профиль читается
mw.restore_stores(bot2, PERSONA, CK, {"dossier": stores["dossier"]})
assert "_facts_watermark" not in rjson("chat_dossier.json")[CK]
assert ChatDossier(context=CTX).get_profile_snapshot(CK)["interests"] == ["ngtu"]
assert rjson("reminders/reminders.json")[1]["text"] == "врач"
assert rjson("living/world.json")["npcs"][0]["name"] == "Хэнк"
assert rjson("inventory.json")["items"] == [{"name": "кофе"}]
print("фолбэк (файлы, бот без менеджеров): ok")

# ── Режим управления: что просили и где бот был ──
# Настоящие менеджеры (computer_control, агент задач, сценарии) и файловый
# фолбэк (режим выключен, файлы прошлых запусков остались)
from types import SimpleNamespace

from app.features.cc_privacy import KnownSecrets
from app.features.computer_control import ComputerControlManager
from app.features.scenario_manager import ScenarioManager
from app.features.task_agent import TaskAgent

CC_DIR = BASE / "computer_control"


def seed_control():
    CC_DIR.mkdir(parents=True, exist_ok=True)
    rec = lambda ts, chat, url: json.dumps(
        {"ts": ts, "chat_id": chat, "ok": True, "kind": "url", "value": url},
        ensure_ascii=False)
    (CC_DIR / "audit.jsonl.1").write_text(
        rec(1, CK, "https://old.example/") + "\n", encoding="utf-8")
    (CC_DIR / "audit.jsonl").write_text(
        "\n".join([rec(2, CK, "https://example.edu/kaf/persons/1914/"),
                   rec(3, "other", "https://other.example/"),
                   rec(4, CK, "https://dodopizza.ru/")]) + "\n",
        encoding="utf-8")
    wjson("computer_control/last_tab.json", {
        "chats": {CK: {"host": "example.edu", "url": "https://example.edu/kaf/",
                       "vis": "", "ts": 20},
                  "other": {"host": "other.example",
                            "url": "https://other.example/", "vis": "", "ts": 10}},
        "host": "example.edu", "url": "https://example.edu/kaf/", "ts": 20})
    wjson("computer_control/task_memory.json", {
        CK: [{"ts": 1, "goal": "закажи пиццу", "sites": ["dodopizza.ru"],
              "qa": [], "result": "дошёл до оформления"}],
        "other": [{"ts": 2, "goal": "чужая", "sites": [], "qa": [], "result": ""}]})


def audit_chats():
    out = []
    for name in ("audit.jsonl.1", "audit.jsonl"):
        p = CC_DIR / name
        if p.is_file():
            out += [json.loads(ln)["chat_id"]
                    for ln in p.read_text(encoding="utf-8").splitlines() if ln]
    return out


seed_control()
cc = ComputerControlManager(context=CTX, config={"confirm": True})
assert cc._st(CK).last_host == "example.edu"  # контекст страницы с диска
cc.set_pending(CK, {"kind": "url", "value": "https://x.example/"}, user_id="u")
ta = TaskAgent(computer_control=cc, context=CTX)
ta._runs[CK] = {"goal": "закажи пиццу", "busy": False, "cancel": False,
                "touched": time.time(), "qa": [], "sites": ["dodopizza.ru"]}
ta.__dict__["_finished"] = {CK: {"run": {"goal": "прошлая"}, "ts": time.time(),
                                 "text": "итог"}}
sm = ScenarioManager(context=CTX, computer_control=cc)
sm._recording[CK] = {"since": time.time(), "name": "пицца"}
sm._runs[CK] = {"name": "пицца", "steps": [], "pos": 0}
sm._offered[CK] = time.time()
vault = KnownSecrets()
vault.add(CK, "hunter22")
bot3 = SimpleNamespace(computer_control=cc, task_agent=ta, scenario_manager=sm,
                       _cc_known_secrets=vault,
                       _pending_photos={CK: [{"data": b""}]},
                       _pending_more_photos={CK: {"photos": [], "ts": 0}})

stores3 = mw.collect_stores(bot3, PERSONA, CK)
ctl = stores3["control"]
assert len(ctl["audit"]) == 3 and ctl["last_tab"]["host"] == "example.edu"
assert ctl["task_memory"][0]["goal"] == "закажи пиццу"

mw.wipe_stores(bot3, PERSONA, CK)
assert audit_chats() == ["other"], audit_chats()  # и из ротации .1
tabs = rjson("computer_control/last_tab.json")
assert list(tabs["chats"]) == ["other"] and tabs["host"] == "other.example"
assert list(rjson("computer_control/task_memory.json")) == ["other"]
assert cc.get_pending(CK) is None and CK not in cc._chat_states()
assert CK not in ta._runs and CK not in ta._finished
assert not (sm._recording or sm._runs or sm._offered)
assert vault.values(CK) == []
assert CK not in bot3._pending_photos and CK not in bot3._pending_more_photos
# Новый ход того же чата — с чистого листа (страница «с диска» не всплывает)
assert cc._st(CK).last_host is None
print("режим управления (живые менеджеры): стёрто ok")

# Идущий прогон агента: снимается флагом и не дописывает стёртую память
busy = {"goal": "идущая", "busy": True, "cancel": False, "touched": time.time(),
        "qa": [["вопрос", "ответ"]], "sites": ["x.ru"]}
ta._runs[CK] = busy
ta.forget_chat(CK)
assert busy["cancel"] and busy["forget"]
ta._remember(CK, busy, "cancelled by the user")  # конец _drive
assert CK not in rjson("computer_control/task_memory.json")
print("режим управления: идущий прогон не пишет в стёртую память ok")

mw.restore_stores(bot3, PERSONA, CK, stores3)
assert sorted(audit_chats()) == sorted([CK, CK, CK, "other"])
chats_ts = [json.loads(ln)["ts"] for ln in
            (CC_DIR / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
assert chats_ts == sorted(chats_ts)  # хвост для сценариев — хронологический
assert rjson("computer_control/last_tab.json")["chats"][CK]["host"] == "example.edu"
assert rjson("computer_control/task_memory.json")[CK][0]["goal"] == "закажи пиццу"
assert cc._st(CK).last_host == "example.edu"
print("режим управления: восстановление из корзины ok")

# Файловый фолбэк: режим управления выключен — менеджеров нет
shutil.rmtree(CC_DIR)
seed_control()
bot4 = FakeBot(full=False)
stores4 = mw.collect_stores(bot4, PERSONA, CK)
assert len(stores4["control"]["audit"]) == 3
mw.wipe_stores(bot4, PERSONA, CK)
assert audit_chats() == ["other"]
assert list(rjson("computer_control/last_tab.json")["chats"]) == ["other"]
assert list(rjson("computer_control/task_memory.json")) == ["other"]
mw.restore_stores(bot4, PERSONA, CK, stores4)
assert audit_chats().count(CK) == 3
assert CK in rjson("computer_control/task_memory.json")
print("режим управления (фолбэк, файлы): ok")

shutil.rmtree(BASE)
print("ALL OK")

"""Тест memory_wipe: полное стирание памяти чата при /api/chat/clear.
Менеджер-путь (фейки с точным API менеджеров: локи+_save) и файловый
фолбэк (фичи выключены). Запуск: python -m scripts.test_memory_wipe"""
import json
import shutil
import threading
from pathlib import Path

from app.api import memory_wipe as mw

PERSONA = "wipe_test"
CTX = f"api_{PERSONA}"
CK = "web_user"
BASE = Path(f"data/{CTX}")


def wjson(rel, data):
    p = BASE / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def rjson(rel, default=None):
    p = BASE / rel
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else default


class _FakeMgr:
    """Общий фейк менеджера: lock + save-запись в файл."""
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
        """Публичная точка очистки обучения чата (learning_manager.clear_chat):
        сессии + ВСЕ ожидающие setup чата + реестр вопросов — одной
        транзакцией. memory_wipe больше не лезет в приватные поля менеджера."""
        with self._lock:
            self._sessions = [s for s in self._sessions
                              if str(s.get("chat_id")) != str(ck)]
            self._setup_state.pop(str(ck), None)
            self._question_msgs.pop(str(ck), None)
            self._save()


class FakeDossier(_FakeMgr):
    def __init__(self):
        super().__init__()
        self._profiles = {CK: {"interests": ["ngtu"]}, "other": {"x": 1}}
        self._facts_seen = {CK: {"a"}, "other": {"b"}}
        self._facts_watermark = {CK: 5}
        self._save()

    def _save(self):
        wjson("chat_dossier.json", self._profiles)


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
        self._chat_dossier = FakeDossier()
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
print("collect: ok", sorted(stores))

mw.wipe_stores(bot, PERSONA, CK)
# in-memory чисто, чужое цело
assert bot.reminder_manager._reminders == [{"id": 2, "chat_id": "other", "text": "чужое"}]
assert bot.reminder_manager.pending_cleared == [CK]
assert bot.learning_manager._sessions == [{"chat_id": "other", "topic": "чужая"}]
assert bot.learning_manager._setup_state == {} and bot.learning_manager._question_msgs == {}
assert CK not in bot._chat_dossier._profiles and CK not in bot._chat_dossier._facts_seen
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

mw.restore_stores(bot, PERSONA, CK, stores)
assert any(r.get("chat_id") == CK for r in bot.reminder_manager._reminders)
assert any(s.get("chat_id") == CK and s.get("topic") == "английский"
           for s in bot.learning_manager._sessions)
assert bot._chat_dossier._profiles[CK]["interests"] == ["ngtu"]
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
assert rjson("reminders/reminders.json")[1]["text"] == "врач"
assert rjson("living/world.json")["npcs"][0]["name"] == "Хэнк"
assert rjson("inventory.json")["items"] == [{"name": "кофе"}]
print("фолбэк (файлы, бот без менеджеров): ok")

shutil.rmtree(BASE)
print("ALL OK")

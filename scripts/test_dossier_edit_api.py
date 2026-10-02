"""Тест правок досье через веб-API (действия скина и дефолтного досье).

  - напоминание правится на месте по id (PUT /reminders/{id}): текст/время,
    id и повтор сохраняются, прошедшее время — 422, чужой/сработавший id — 404;
  - отмена напоминания по id (DELETE ?id=…) не зависит от сдвига списка;
  - удаление реплики STM по тексту+метке находит её после сдвига буфера
    deque(maxlen) и отвечает 404, если реплики уже нет;
  - частичный патч llm (только models / только fallback) не снимает
    закреплённый primary; провайдеры по назначению доходят до settings_api.

Всё на временной VPC_DATA_DIR — настоящая data/ не трогается.

Запуск: /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m scripts.test_dossier_edit_api
"""

import os
import shutil
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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


class FakeStm:
    """Буфер STM одного чата: deque(maxlen) под RLock, как у ShortTermMemory."""

    def __init__(self, maxlen: int):
        self._lock = threading.RLock()
        self.buf = deque(maxlen=maxlen)

    def add(self, role, content, ts):
        with self._lock:
            self.buf.append({"role": role, "content": content, "timestamp": ts})

    def get_messages(self, user_id=None, chat_id=None):
        with self._lock:
            return list(self.buf)

    def delete_message(self, chat_id, index):
        with self._lock:
            if index < 0 or index >= len(self.buf):
                return False
            del self.buf[index]
            return True


def main():
    tmp = Path(tempfile.mkdtemp(prefix="dossier_edit_test_"))
    old_env = os.environ.get("VPC_DATA_DIR")
    os.environ["VPC_DATA_DIR"] = str(tmp)
    try:
        run()
    finally:
        if old_env is None:
            os.environ.pop("VPC_DATA_DIR", None)
        else:
            os.environ["VPC_DATA_DIR"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


def run():
    from fastapi.testclient import TestClient
    import app.api.server as server_mod
    from app.features.reminder_manager import ReminderManager

    rm = ReminderManager(context="api_dossier_test")
    stm = FakeStm(maxlen=4)
    bot = SimpleNamespace(reminder_manager=rm, memory=SimpleNamespace(stm=stm))

    async def fake_get_bot(persona):
        return bot

    orig_token = server_mod._api_token
    server_mod._api_token = ""
    try:
        with mock.patch.object(server_mod, "_get_bot", fake_get_bot):
            client = TestClient(server_mod.app, base_url="http://127.0.0.1")
            run_reminders(client, rm)
            run_stm(client, stm)
            run_llm_patch(client)
    finally:
        server_mod._api_token = orig_token


def run_reminders(client, rm):
    section("1. Правка напоминания по id")
    base = "/api/personas/alex/reminders"
    client.post(base, json={"task": "первое", "delay_seconds": 600})
    client.post(base, json={"task": "второе", "delay_seconds": 1200})
    daily = rm.add_reminder("web_user", "web", "зарядка", 0,
                            schedule={"type": "daily", "hour": 7, "minute": 30})
    items = client.get(base).json()["items"]
    check("список отдаёт id", all(i.get("id") for i in items) and len(items) == 3)
    first = next(i for i in items if i["task"] == "первое")
    new_at = time.time() + 7200
    r = client.put(f"{base}/{first['id']}", json={"task": "первое (правка)", "trigger_at": new_at})
    check("PUT: 200", r.status_code == 200)
    after = {i["id"]: i for i in r.json()["items"]}
    check("id тот же, текст и время новые",
          first["id"] in after and after[first["id"]]["task"] == "первое (правка)"
          and abs(after[first["id"]]["trigger_at"] - new_at) < 0.01)
    check("записей столько же (правка на месте, не отмена+создание)", len(after) == 3)
    r = client.put(f"{base}/{first['id']}", json={"task": "только текст"})
    check("PUT только текста: время прежнее",
          r.status_code == 200 and abs({i["id"]: i for i in r.json()["items"]}[first["id"]]["trigger_at"] - new_at) < 0.01)
    r = client.put(f"{base}/{first['id']}", json={"trigger_at": time.time() - 60})
    check("время в прошлом: 422", r.status_code == 422)
    r = client.put(f"{base}/{first['id']}", json={"task": "   "})
    check("пустой текст: 422", r.status_code == 422)
    r = client.put(f"{base}/rffff0", json={"task": "x"})
    check("чужой id: 404", r.status_code == 404)

    # Повторяющееся: новое время переносит и расписание, повтор не теряется
    from app.core import timeutil
    target = timeutil.now().replace(hour=21, minute=15, second=0, microsecond=0)
    ts = timeutil.to_ts(target)
    if ts < time.time() + 60:
        ts += 86400
    r = client.put(f"{base}/{daily['id']}", json={"trigger_at": ts})
    rec = {i["id"]: i for i in r.json()["items"]}[daily["id"]]["recurrence"]
    check("повтор сохранён, расписание 21:15",
          r.status_code == 200 and rec and rec["type"] == "daily"
          and rec["hour"] == 21 and rec["minute"] == 15)

    section("2. Отмена напоминания по id")
    items = client.get(base).json()["items"]
    second = next(i for i in items if i["task"] == "второе")
    # Первое «срабатывает» между показом списка и отменой — индексы сдвигаются
    rm.cancel_by_ref("web_user", first["id"])
    r = client.delete(f"{base}?id={second['id']}")
    left = [i["task"] for i in r.json()["items"]] if r.status_code == 200 else None
    check("DELETE ?id=: отменено именно «второе»", left == ["зарядка"])
    r = client.delete(f"{base}?id={second['id']}")
    check("повторный DELETE по id: 404", r.status_code == 404)
    r = client.delete(base)
    check("DELETE без id и index: 422", r.status_code == 422)


def run_stm(client, stm):
    section("3. Удаление реплики STM по тексту+метке")
    t0 = 1_700_000_000.0
    for i in range(4):
        stm.add("user" if i % 2 == 0 else "assistant", f"m{i}", t0 + i)
    hist = client.get("/api/chat/history?persona=alex").json()
    shown = hist[1]  # «m1» на позиции 1 в показанной истории
    # Пока досье открыто, приходит новый обмен: deque(maxlen) вытесняет m0, m1→0
    stm.add("user", "m4", t0 + 4)
    body = {"persona": "alex", "index": 1, "user_id": "web_user", "chat_id": "web_user",
            "content": shown["content"], "timestamp": shown["timestamp"]}
    r = client.post("/api/chat/history/delete", json=body)
    contents = [m["content"] for m in stm.get_messages()]
    check("удалена именно m1, а не сосед по старому индексу",
          r.status_code == 200 and contents == ["m2", "m3", "m4"])
    r = client.post("/api/chat/history/delete", json=body)
    check("реплики уже нет: 404, буфер не тронут",
          r.status_code == 404 and [m["content"] for m in stm.get_messages()] == ["m2", "m3", "m4"])
    stm.add("assistant", "same", t0 + 5)
    stm.add("assistant", "same", t0 + 6)
    r = client.post("/api/chat/history/delete", json={
        **body, "index": 3, "content": "same", "timestamp": t0 + 6})
    got = [(m["content"], m["timestamp"]) for m in stm.get_messages()]
    check("одинаковый текст: удалена реплика с той же меткой",
          r.status_code == 200 and ("same", t0 + 5) in got and ("same", t0 + 6) not in got)


def run_llm_patch(client):
    section("4. Частичный патч llm не снимает primary")
    from app.api import settings_api
    captured = []

    def fake_update(persona, settings, stm_size, features, llm=None):
        captured.append(llm)
        return {"restart_required": False}

    with mock.patch.object(settings_api, "update_persona_config", fake_update):
        client.put("/api/personas/alex/config", json={"llm": {"models": {"openai": "gpt-x"}}})
        client.put("/api/personas/alex/config", json={"llm": {"fallback": ["a", "b"]}})
        client.put("/api/personas/alex/config", json={"llm": {"primary": None}})
        client.put("/api/personas/alex/config", json={"llm": {
            "answer_provider": "groq", "cc_provider": None, "vision_provider": ""}})
        client.put("/api/personas/alex/config", json={"llm": {"exclude": ["local"]}})
    check("set-model: в llm только models", captured[0] == {"models": {"openai": "gpt-x"}})
    check("toggle-backup: в llm только fallback", captured[1] == {"fallback": ["a", "b"]})
    check("явный primary=null доходит (снять закрепление)", captured[2] == {"primary": None})
    check("провайдеры по назначению доходят до settings_api",
          captured[3] == {"answer_provider": "groq", "cc_provider": None, "vision_provider": ""})
    check("llm.exclude доходит до settings_api как отдельное поле",
          captured[4] == {"exclude": ["local"]})

    # Семантика settings_api на реальном YAML: models-патч сохраняет primary
    import yaml
    path = Path(os.environ["VPC_DATA_DIR"]) / "p_llm.yaml"
    path.write_text(yaml.safe_dump({
        "system_prompt": "x", "llm": {"primary": "groq", "answer_provider": "openai"}}),
        encoding="utf-8")
    with mock.patch.object(settings_api, "_persona_yaml_path", lambda p: path):
        settings_api.update_persona_config("p_llm", None, None, None, captured[0])
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        check("YAML: primary и answer_provider на месте после models-патча",
              data["llm"].get("primary") == "groq" and data["llm"].get("answer_provider") == "openai")
        settings_api.update_persona_config("p_llm", None, None, None, {"answer_provider": None})
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        check("YAML: answer_provider=None снимает ключ, primary цел",
              "answer_provider" not in data["llm"] and data["llm"].get("primary") == "groq")

    # llm.exclude: персона убирает провайдер из своей автоматической цепочки
    # (PATCH → YAML → живой роутер). exclude=[] снимает ключ, primary/fallback
    # при этом не трогаются (тот же принцип, что и у fallback/models выше).
    path2 = Path(os.environ["VPC_DATA_DIR"]) / "p_excl.yaml"
    path2.write_text(yaml.safe_dump({
        "system_prompt": "x",
        "llm": {"primary": "groq", "fallback": ["zai", "webchat:qwen"]}}),
        encoding="utf-8")
    with mock.patch.object(settings_api, "_persona_yaml_path", lambda p: path2):
        settings_api.update_persona_config("p_excl", None, None, None,
                                           {"exclude": ["webchat:qwen"]})
        data = yaml.safe_load(path2.read_text(encoding="utf-8"))
        check("YAML: exclude записан, primary/fallback не тронуты",
              data["llm"].get("exclude") == ["webchat:qwen"]
              and data["llm"].get("primary") == "groq"
              and data["llm"].get("fallback") == ["zai", "webchat:qwen"])
        cfg = settings_api.get_persona_config("p_excl")
        check("get_persona_config: llm.exclude отдаётся из YAML",
              cfg["llm"]["exclude"] == ["webchat:qwen"])

        settings_api.update_persona_config("p_excl", None, None, None, {"exclude": []})
        data = yaml.safe_load(path2.read_text(encoding="utf-8"))
        check("YAML: exclude=[] снимает ключ, primary/fallback целы",
              "exclude" not in data["llm"]
              and data["llm"].get("primary") == "groq"
              and data["llm"].get("fallback") == ["zai", "webchat:qwen"])
        cfg2 = settings_api.get_persona_config("p_excl")
        check("get_persona_config: без ключа exclude — пустой список",
              cfg2["llm"]["exclude"] == [])


if __name__ == "__main__":
    sys.exit(main())

"""Тест: Gemma на телефоне как движок локальных задач (app/core/phone_llm).

  A. очередь: телефона нет на связи — вызов сразу None; ждущий опрос
     телефона получает задачу, ответ возвращается вызывающему; не успел —
     None, задача снята, поздний ответ выброшен; прогрев — без дублей,
     залежавшийся не отдаётся; Gemma на ноутбуке — опрос сразу пустой;
     messages → system / история / реплика;
  B. LocalLLMRouter: выбран телефон — доступность = телефон на связи,
     вызов уходит телефону, warm кладёт прогрев; фоновые задачи — не на
     телефон; ноутбук без Ollama — None;
  C. маршруты: GET /api/phone-llm/next (задача, enabled), POST
     /api/phone-llm/result, PUT /api/providers/local/device, строка Ollama
     в /api/providers — место и статус телефона;
  D. края: вызов без задачи (основной ответ) — телефону, OCR и приватные
     страницы — Ollama; процесс без API (Telegram) телефона не ждёт; ядро
     без облака не отказывает, пока телефон не пришёл; конец ожидания на
     ядре не продлевает «на связи»; задача, пришедшая ушедшему телефону, —
     обратно в очередь.

Сеть, Ollama и телефон не трогаются, .env не пишется.
Запуск: PYTHONPATH=. python3 -m scripts.test_phone_llm
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="phone_llm_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"  # Ollama нет

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


def reset():
    from app.core import phone_llm as pl
    with pl._lock:
        pl._queue.clear()
        pl._jobs.clear()
        pl._workers.clear()
        pl._waiters.clear()


class Phone:
    """Телефон-фейк: свой event loop в потоке, долгий опрос next_job и
    ответ complete — как приложение через API."""

    def __init__(self, worker="phone1", answer=lambda job: "ok", delay=0.0):
        self.worker, self.answer, self.delay = worker, answer, delay
        self.jobs = []
        self.stop = False
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        from app.core import phone_llm as pl
        asyncio.set_event_loop(self.loop)
        while not self.stop:
            job = self.loop.run_until_complete(
                pl.next_job(self.worker, 0.5, {"backend": "gpu", "loaded": True, "name": "Тест"}))
            if job is None:
                continue
            self.jobs.append(job)
            if job["kind"] == "chat":
                time.sleep(self.delay)
                pl.complete(self.worker, job["id"], text=self.answer(job), backend="gpu", ms=1.0)

    def __enter__(self):
        self.thread.start()
        time.sleep(0.1)
        return self

    def __exit__(self, *exc):
        self.stop = True
        self.thread.join(3)


# ════════════ A. очередь ════════════

def test_queue():
    section("A. phone_llm: очередь задач телефона")
    from app.core import phone_llm as pl
    reset()
    os.environ.pop(pl.DEVICE_ENV, None)
    check("по умолчанию Gemma на ноутбуке", pl.device() == "pc")
    os.environ[pl.DEVICE_ENV] = "nonsense"
    check("мусор в настройке — ноутбук", pl.device() == "pc")
    os.environ[pl.DEVICE_ENV] = "phone"

    t0 = time.monotonic()
    check("телефона нет на связи — сразу None",
          pl.chat([{"role": "user", "content": "x"}], timeout=5) is None
          and time.monotonic() - t0 < 0.5)
    check("и задача не осталась в очереди", not pl._queue and not pl._jobs)

    sys_, hist, text = pl._split_messages([
        {"role": "system", "content": "S1"}, {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"}, {"role": "system", "content": "S2"},
        {"role": "user", "content": "u2"}])
    check("messages → system, история, последняя реплика",
          sys_ == "S1\n\nS2" and text == "u2"
          and hist == [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}])

    with Phone(answer=lambda job: f"re:{job['text']}") as ph:
        check("опрашивающий телефон — на связи", pl.online())
        out = pl.chat([{"role": "system", "content": "sys"}, {"role": "user", "content": "q1"}],
                      max_tokens=7, temperature=0.0, timeout=5, task="book_search")
        check("ответ телефона вернулся вызывающему", out == "re:q1")
        job = ph.jobs[-1] if ph.jobs else {}
        check("телефон получил промпт, предел токенов и задачу",
              job.get("system") == "sys" and job.get("max_tokens") == 7
              and job.get("task") == "book_search")
        check("выполненная задача снята", not pl._jobs)
        st = pl.status()
        check("статус: на связи, видеоядро, имя",
              st["online"] and st["worker"]["backend"] == "gpu" and st["worker"]["name"] == "Тест")

    with Phone(delay=1.5) as ph:
        t0 = time.monotonic()
        out = pl.chat([{"role": "user", "content": "slow"}], timeout=0.5)
        check("не успел за таймаут — None без ожидания ответа",
              out is None and time.monotonic() - t0 < 1.2)
        check("задача снята", not pl._jobs)
        time.sleep(1.5)
        check("поздний ответ выброшен (complete → False)",
              pl.complete("phone1", ph.jobs[-1]["id"], text="late") is False)

    reset()
    # Свернулось, не начав: задача возвращается и достаётся другому исполнителю
    pl._note_worker("phoneA", {})
    box = {}
    th = threading.Thread(target=lambda: box.update(out=pl.chat([{"role": "user", "content": "q"}], timeout=5)))
    th.start()
    for _ in range(50):
        if pl._queue:
            break
        time.sleep(0.02)
    job = pl._take("phoneA")
    check("чужой исполнитель задачу не вернёт", job and pl.release("phoneB", job["id"]) is False)
    check("свернувшийся вернул задачу — снова в очереди",
          pl.release("phoneA", job["id"]) is True and len(pl._queue) == 1)
    again = pl._take("phoneB")
    check("её взял другой", again and again["id"] == job["id"])
    pl.complete("phoneB", job["id"], text="ok")
    th.join(5)
    check("ответ другого дошёл до вызова", box.get("out") == "ok")
    check("выполненную уже не вернуть", pl.release("phoneB", job["id"]) is False)

    reset()
    # Служба уступает приложению на экране того же телефона, пока оно ждёт
    pl._note_worker("app1-gemma", {"name": "ELI"}, waiting=True)
    pl._note_worker("app2-gemma-bg", {"name": "ELI"})
    pl._note_worker("other-gemma-bg", {"name": "Pixel"})
    job = dict(system="", history=[], max_tokens=5, temperature=0.0, task="book_search")
    pl._enqueue("chat", text="q", **job)
    check("служба того же телефона уступает", pl._take("app2-gemma-bg") is None and len(pl._queue) == 1)
    check("служба другого телефона берёт", (pl._take("other-gemma-bg") or {}).get("text") == "q")
    pl._enqueue("chat", text="q2", **job)
    pl._note_worker("app1-gemma", {}, waiting=False)
    check("приложение не ждёт — служба берёт", (pl._take("app2-gemma-bg") or {}).get("text") == "q2")

    reset()
    # Прогрев: телефон на связи, но сейчас не ждёт (отметка свежая)
    pl._note_worker("phone1", {})
    pl.warm()
    pl.warm()
    check("прогрев не дублируется", sum(1 for j in pl._queue if j["kind"] == "warm") == 1)
    check("прогрев отдаётся телефону", (pl._take("phone1") or {}).get("kind") == "warm")
    pl.warm()
    pl._queue[-1]["created"] -= pl.WARM_TTL_SEC + 1
    check("залежавшийся прогрев не отдаётся", pl._take("phone1") is None and not pl._jobs)

    os.environ[pl.DEVICE_ENV] = "pc"
    loop = asyncio.new_event_loop()
    t0 = time.monotonic()
    got = loop.run_until_complete(pl.next_job("phone1", 20, {}))
    loop.close()
    check("Gemma на ноутбуке — опрос сразу пустой, телефон отмечен",
          got is None and time.monotonic() - t0 < 0.5 and pl.status()["worker"] is not None)
    os.environ[pl.DEVICE_ENV] = "phone"
    reset()


# ════════════ B. роутер ════════════

def test_router():
    section("B. LocalLLMRouter: Gemma на телефоне")
    from app.core import phone_llm as pl
    from app.core.local_router import LocalLLMRouter
    reset()
    pl.serve()  # как run_api: этот процесс принимает телефон
    os.environ[pl.DEVICE_ENV] = "phone"
    r = LocalLLMRouter()
    check("телефона нет — задача недоступна", not r.is_available(task="book_search"))
    with Phone(answer=lambda job: "SINGLE"):
        check("телефон на связи — доступна", r.is_available(task="book_search"))
        out = r.get_response([{"role": "user", "content": "split?"}], task="book_search")
        check("вызов роутера ушёл телефону", out == "SINGLE")
    with Phone(answer=lambda job: "BG") as ph:
        check("фоновая задача на телефон не идёт (без Ollama — None)",
              r.get_response([{"role": "user", "content": "state"}], task="state_engine") is None
              and not ph.jobs)
    reset()
    pl._note_worker("phone1", {})
    r.warm(task="book_search")
    check("warm — прогрев телефону", any(j["kind"] == "warm" for j in pl._queue))
    reset()
    pl._note_worker("phone1", {})
    r.warm(task="state_engine")
    check("warm фоновой задачи — не телефону", not pl._queue)
    reset()

    os.environ[pl.DEVICE_ENV] = "pc"
    with Phone(answer=lambda job: "SHOULD-NOT"):
        check("Gemma на ноутбуке без Ollama — недоступна и None",
              not r.is_available(task="book_search")
              and r.get_response([{"role": "user", "content": "x"}], task="book_search") is None)
    reset()


# ════════════ C. маршруты ════════════

def test_routes():
    section("C. маршруты телефона и настройки")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server
    from app.api import settings_api
    from app.core import phone_llm as pl
    reset()
    orig_token, orig_persist = server._api_token, settings_api._persist_env
    server._api_token = ""
    settings_api._persist_env = lambda var, value: os.environ.__setitem__(var, value)
    try:
        client = TestClient(server.app, base_url="http://127.0.0.1")
        r = client.put("/api/providers/local/device", json={"device": "phone"})
        check("PUT device=phone", r.status_code == 200 and os.environ[pl.DEVICE_ENV] == "phone")
        r = client.put("/api/providers/local/device", json={"device": "tablet"})
        check("неизвестное устройство — 400", r.status_code == 400)

        box = {}

        def call():
            box["out"] = pl.chat([{"role": "user", "content": "hi"}], timeout=10, task="t")

        pl._note_worker("p1", {})  # на связи — вызов встанет в очередь
        th = threading.Thread(target=call)
        th.start()
        time.sleep(0.2)
        r = client.get("/api/phone-llm/next",
                       params={"worker": "p1", "wait": 5, "backend": "gpu", "name": "Honor"})
        data = r.json() if r.status_code == 200 else {}
        job = data.get("job") or {}
        check("опрос отдаёт задачу и enabled", data.get("enabled") is True and job.get("text") == "hi")
        r = client.post("/api/phone-llm/result",
                        json={"worker": "p1", "id": job.get("id", "x"), "text": "hello", "backend": "gpu"})
        th.join(5)
        check("ответ телефона дошёл до вызова", r.json().get("ok") is True and box.get("out") == "hello")
        r = client.post("/api/phone-llm/result", json={"worker": "p1", "id": "nope", "text": "x"})
        check("ответ на неизвестную задачу — ok: false", r.json().get("ok") is False)
        r = client.get("/api/phone-llm/next", params={"worker": "bad id!"})
        check("кривой id телефона — 422", r.status_code == 422)

        r = client.get("/api/providers")
        local = next((p for p in r.json().get("providers", []) if p.get("local")), {})
        check("строка Ollama: место и статус телефона",
              local.get("device") == "phone" and local.get("phone", {}).get("worker", {}).get("name") == "Honor")
        r = client.put("/api/providers/local/device", json={"device": "pc"})
        t0 = time.monotonic()
        r = client.get("/api/phone-llm/next", params={"worker": "p1", "wait": 20})
        check("Gemma на ноутбуке — опрос сразу, enabled: false",
              r.json() == {"job": None, "enabled": False} and time.monotonic() - t0 < 1)
    finally:
        server._api_token = orig_token
        settings_api._persist_env = orig_persist
        reset()


# ════════════ D. края ════════════

def test_edges():
    section("D. края: куда идут вызовы, «на связи», ушедший телефон")
    from app.core import phone_llm as pl
    from app.core import local_router as lr
    reset()
    pl.serve()
    os.environ[pl.DEVICE_ENV] = "phone"
    r = lr.LocalLLMRouter()
    with Phone(answer=lambda job: "MAIN") as ph:
        check("вызов без задачи (основной ответ при откате) — телефону",
              r.is_available() and r.get_response([{"role": "user", "content": "hi"}]) == "MAIN")
        check("OCR — только Ollama: без неё недоступен, хоть телефон и на связи",
              not r.is_available(task="ocr"))
        n = len(ph.jobs)
        check("приватная страница — не телефону",
              not r.is_available(task=lr.PRIVATE_TASK)
              and r.get_response([{"role": "user", "content": "secret"}], task=lr.PRIVATE_TASK) is None
              and len(ph.jobs) == n)
    reset()
    check("телефон ещё не пришёл — основной ответ всё равно за ним (ядро не отказывает)",
          r.answers_on_phone() and not r.is_available())

    pl._served = False
    try:
        with Phone(answer=lambda job: "SHOULD-NOT") as ph:
            check("процесс без API (Telegram) — задачи не телефону",
                  not r.answers_on_phone()
                  and r.get_response([{"role": "user", "content": "x"}], task="book_search") is None
                  and not [j for j in ph.jobs if j["kind"] == "chat"])
    finally:
        pl._served = True
    reset()

    loop = asyncio.new_event_loop()
    try:
        got = loop.run_until_complete(pl.next_job("phone1", 0.6, {}))
        seen_age = time.time() - pl._workers["phone1"]["seen"]
        check("ожидание кончилось на ядре — «был на связи» не продлён",
              got is None and seen_age >= 0.5 and not pl._workers["phone1"]["waiting"])

        calls = []

        async def gone():
            calls.append(1)
            return len(calls) > 1  # ушёл, пока ждал задачу

        box = []
        threading.Timer(0.2, lambda: box.append(
            pl._enqueue("chat", system="", history=[], text="q", max_tokens=5,
                        temperature=0.0, task="book_search"))).start()
        got = loop.run_until_complete(pl.next_job("phone1", 5, {}, gone=gone))
        job = box[0] if box else {}
        check("задача пришла ушедшему телефону — обратно в очередь",
              got is None and list(pl._queue) == [job] and job["taken"] is None)
        got = loop.run_until_complete(pl.next_job("phone2", 1, {}))
        check("её берёт следующий телефон", got is not None and got["id"] == job["id"])
    finally:
        loop.close()
        reset()


def main():
    test_queue()
    test_router()
    test_routes()
    test_edges()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

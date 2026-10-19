"""Gemma на телефоне — движок локальных задач вместо Ollama на ноутбуке.

Не на каждом ноутбуке потянет Gemma, а телефон с моделью в приложении
(Gemma 4 E2B на LiteRT-LM, видеоядро) считает служебный вызов за ~2 с.
Выбор — LOCAL_LLM_DEVICE в .env: «pc» (дефолт, Ollama) или «phone» (настройки
→ «Ключи и модели» → строка Ollama). Тогда задачи на пути ответа, которые
шли бы в Ollama (LocalLLMRouter), уходят сюда; фоновые остаются за Ollama
ноутбука, веб-чатные — веб-чатными.

Ядро не может само обратиться к телефону — телефон приходит к нему:
приложение (веб на экране, фоновая служба, пока оно свёрнуто) держит долгий
запрос GET /api/phone-llm/next; задача уходит в первый ждущий запрос,
телефон считает её своей Gemma и возвращает ответ POST /api/phone-llm/result.
Вызов роутера (поток ответа или фоновой задачи) ждёт результат до своего
таймаута. Телефона нет на связи (давно не опрашивал) — None сразу, как у
недоступной Ollama: вызывающий идёт своим откатом.

Очередь — в памяти процесса: задача, которую никто не взял до таймаута
вызывающего, снимается; ответ на снятую — выбрасывается.
"""

import asyncio
import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

DEVICE_ENV = "LOCAL_LLM_DEVICE"
DEVICES = ("pc", "phone")
# Предел ожидания долгого опроса на ядре (клиент шлёт свой wait)
MAX_WAIT_SEC = 60.0
# Телефон опрашивал не раньше стольких секунд назад — на связи: телефон шлёт
# wait=25, следующий запрос — сразу за ответом, плюс запас на сеть и задачу
ONLINE_SEC = 45.0
# Прогрев, который телефон не взял за столько секунд, уже не нужен
WARM_TTL_SEC = 30.0
# Сколько последних телефонов помнить для статуса в настройках
_MAX_WORKERS = 8
# Проверка остановки ядра во время ожидания (как в inbox)
_STOP_POLL_SEC = 1.0


_served = False


def serve() -> None:
    """Этот процесс принимает телефон: маршруты /api/phone-llm/* (run_api).
    В других процессах (Telegram-боты) телефону не к кому прийти — там
    задачи идут Ollama, как при «pc»."""
    global _served
    _served = True


def served() -> bool:
    return _served


def device() -> str:
    # Где работает Gemma: читается на каждый вызов — настройка меняется из веба
    d = (os.getenv(DEVICE_ENV) or "pc").strip().lower()
    return d if d in DEVICES else "pc"


_lock = threading.Lock()
_queue: deque = deque()
_jobs: dict[str, dict] = {}
# Телефоны, которые опрашивают: id → {seen, backend, loaded, name, waiting}
_workers: dict[str, dict] = {}
# Ожидающие запросы: (их event loop, событие «пришла задача»)
_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
_stop_check: Optional[Callable[[], bool]] = None


def set_stop_check(fn: Optional[Callable[[], bool]]) -> None:
    """Признак «ядро останавливается» для долгого опроса (app/main.py)."""
    global _stop_check
    _stop_check = fn


def _stopping() -> bool:
    try:
        return bool(_stop_check and _stop_check())
    except Exception:
        return False


def _wake_waiters() -> None:
    # Под _lock: задачу кладут из потоков роутера, ждут — на event loop API
    for loop, event in list(_waiters):
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            _waiters.discard((loop, event))  # loop закрыт


def online() -> bool:
    now = time.time()
    with _lock:
        return any(w.get("waiting") or now - w["seen"] < ONLINE_SEC
                   for w in _workers.values())


def status() -> dict:
    """Для настроек: выбранное место и последний телефон на связи."""
    now = time.time()
    with _lock:
        best = max(_workers.values(), key=lambda w: w["seen"], default=None)
        busy = sum(1 for j in _jobs.values() if not j["done"].is_set())
    worker = None
    if best is not None:
        worker = {"name": best.get("name") or "", "backend": best.get("backend") or "",
                  "loaded": bool(best.get("loaded")),
                  "seen_sec": round(now - best["seen"]),
                  "online": bool(best.get("waiting")) or now - best["seen"] < ONLINE_SEC}
    return {"device": device(), "online": bool(worker and worker["online"]),
            "worker": worker, "pending": busy}


def _split_messages(messages: list) -> tuple[str, list, str]:
    # OpenAI-messages → (system, история, последняя реплика) для localChat
    system = "\n\n".join(str(m.get("content") or "") for m in messages
                         if m.get("role") == "system").strip()
    rest = [{"role": "user" if m.get("role") == "user" else "assistant",
             "content": str(m.get("content") or "")}
            for m in messages if m.get("role") != "system" and m.get("content")]
    if rest and rest[-1]["role"] == "user":
        return system, rest[:-1], rest[-1]["content"]
    return system, rest, ""


def _enqueue(kind: str, **fields) -> dict:
    job = {"id": uuid.uuid4().hex[:16], "kind": kind, "created": time.time(),
           "taken": None, "done": threading.Event(), "result": None, **fields}
    with _lock:
        _jobs[job["id"]] = job
        _queue.append(job)
        _wake_waiters()
    return job


def _drop(job: dict) -> None:
    with _lock:
        _jobs.pop(job["id"], None)
        try:
            _queue.remove(job)
        except ValueError:
            pass  # уже взята телефоном


def chat(messages: list, max_tokens: int = 100, temperature: float = 0.0,
         timeout: float = 15.0, task: Optional[str] = None) -> Optional[str]:
    """Служебный вызов на Gemma телефона → текст ответа или None (телефона
    нет на связи, не успел за timeout, модель не ответила)."""
    if not online():
        return None
    system, history, text = _split_messages(messages)
    if not text:
        return None
    job = _enqueue("chat", system=system, history=history, text=text,
                   max_tokens=int(max_tokens), temperature=float(temperature),
                   task=task or "", deadline=time.time() + timeout)
    t0 = time.time()
    if not job["done"].wait(timeout):
        _drop(job)
        logger.info(f"[PhoneLLM] {task or 'задача'}: телефон не ответил за "
                    f"{timeout:.0f} с ({'считал' if job['taken'] else 'не взял'})")
        return None
    _drop(job)
    res = job["result"] or {}
    if res.get("error"):
        logger.warning(f"[PhoneLLM] {task or 'задача'}: {res['error']}")
        return None
    out = (res.get("text") or "").strip()
    logger.debug(f"[PhoneLLM] {task or 'задача'}: {time.time() - t0:.1f} с, "
                 f"{res.get('backend') or '?'}, {len(out)} симв.")
    return out or None


def warm() -> None:
    """Загрузить модель на телефоне заранее (вопрос пойдёт к ней через
    доли секунды, а загрузка — 10–20 с). Без ожидания; повторно — нет,
    пока прежняя не взята."""
    if not online():
        return
    with _lock:
        if any(j["kind"] == "warm" for j in _queue):
            return
    _enqueue("warm")


def _public(job: dict) -> dict:
    out = {"id": job["id"], "kind": job["kind"]}
    if job["kind"] == "chat":
        # ttl_ms — сколько вызывающий ещё ждёт: телефон считает срок по своим
        # часам и не начинает задачу, простоявшую за длинным ответом его
        # модели дольше (ответ всё равно выброшен бы)
        out.update(system=job["system"], history=job["history"], text=job["text"],
                   max_tokens=job["max_tokens"], temperature=job["temperature"],
                   task=job["task"])
        if job.get("deadline"):
            out["ttl_ms"] = max(0, int((job["deadline"] - time.time()) * 1000))
    return out


def _yields(worker_id: str) -> bool:
    # Под _lock. Фоновая служба (…-bg) уступает приложению на экране того же
    # телефона (то же имя), пока оно ждёт задачу: страница знает, что Gemma
    # занята, и рисует реже — иначе 3D стола и Gemma делят видеоядро
    if not worker_id.endswith("-bg"):
        return False
    name = (_workers.get(worker_id) or {}).get("name")
    return bool(name) and any(k != worker_id and not k.endswith("-bg") and w.get("waiting")
                              and w.get("name") == name for k, w in _workers.items())


def _take(worker_id: str) -> Optional[dict]:
    now = time.time()
    with _lock:
        if _yields(worker_id):
            return None
        while _queue:
            job = _queue.popleft()
            if job["id"] not in _jobs:
                continue  # снята вызывающим
            if job.get("deadline") and now >= job["deadline"]:
                continue  # вызывающий уже не ждёт — снимет сам
            if job["kind"] == "warm":
                # Отчёта о прогреве никто не ждёт; залежавшийся — уже не к вопросу
                _jobs.pop(job["id"], None)
                if now - job["created"] > WARM_TTL_SEC:
                    continue
            job["taken"] = worker_id
            return _public(job)
    return None


def _note_worker(worker_id: str, info: dict, waiting: Optional[bool] = None,
                 touch: bool = True) -> None:
    with _lock:
        w = _workers.setdefault(worker_id, {})
        if touch or "seen" not in w:
            w["seen"] = time.time()
        for k in ("backend", "loaded", "name"):
            if info.get(k) is not None:
                w[k] = info[k]
        if waiting is not None:
            w["waiting"] = waiting
        if len(_workers) > _MAX_WORKERS:
            old = min(_workers, key=lambda k: _workers[k]["seen"])
            _workers.pop(old, None)


async def next_job(worker_id: str, wait: float, info: dict,
                   gone: Optional[Callable[[], Awaitable[bool]]] = None) -> Optional[dict]:
    """Долгий опрос телефона: задача сразу, если есть, иначе ждём до wait
    секунд (не больше MAX_WAIT_SEC) — до задачи, остановки ядра или обрыва
    соединения. Gemma выбрана на ноутбуке — сразу пусто: телефон спросит
    позже, а в настройках его видно на связи."""
    _note_worker(worker_id, info)
    if device() != "phone":
        return None
    job = _take(worker_id)
    wait = min(max(wait, 0.0), MAX_WAIT_SEC)
    if job or wait <= 0:
        return job
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    event = asyncio.Event()
    waiter = (loop, event)
    with _lock:
        _waiters.add(waiter)
    _note_worker(worker_id, {}, waiting=True)
    try:
        while True:
            # Сброс до проверки: задача, пришедшая между ними, снова поднимет
            # событие (call_soon_threadsafe выполнится после await)
            event.clear()
            job = _take(worker_id)
            if job:
                # Телефон мог уйти, пока ждал (свернулось, пропала сеть) —
                # задачу не в оборванный запрос, а обратно в очередь
                if gone is not None and await gone():
                    _requeue(worker_id, job["id"])
                    return None
                return job
            while not event.is_set():
                left = deadline - loop.time()
                if left <= 0 or _stopping() or device() != "phone" \
                        or (gone is not None and await gone()):
                    return None
                try:
                    await asyncio.wait_for(event.wait(), min(left, _STOP_POLL_SEC))
                except (asyncio.TimeoutError, TimeoutError):
                    pass
    finally:
        with _lock:
            _waiters.discard(waiter)
        # Без отметки «был на связи»: ожидание кончилось на ядре, а не по
        # запросу телефона — пропавший без обрыва TCP телефон иначе считался
        # бы на связи ещё ONLINE_SEC после конца ожидания
        _note_worker(worker_id, {}, waiting=False, touch=False)


def release(worker_id: str, job_id: str) -> bool:
    """Телефон вернул задачу, не начав её (приложение свернулось, пока шёл
    опрос): в начало очереди — её возьмёт фоновая служба или другое
    приложение. False — задача уже не его или снята."""
    _note_worker(worker_id, {})
    return _requeue(worker_id, job_id)


def _requeue(worker_id: str, job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job["taken"] != worker_id or job["done"].is_set():
            return False
        job["taken"] = None
        _queue.appendleft(job)
        _wake_waiters()
    return True


def complete(worker_id: str, job_id: str, text: Optional[str] = None,
             error: Optional[str] = None, backend: Optional[str] = None,
             ms: Optional[float] = None) -> bool:
    """Ответ телефона. False — задачу уже сняли (вызывающий не дождался)."""
    _note_worker(worker_id, {"backend": backend, "loaded": error is None})
    with _lock:
        job = _jobs.get(job_id)
    if job is None:
        return False
    job["result"] = {"text": text, "error": error, "backend": backend, "ms": ms}
    job["done"].set()
    return True

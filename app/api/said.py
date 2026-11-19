"""Реплики персон по мере появления — для портрета на рабочем столе
(desktop/src-tauri/src/portrait.rs): облачко над головой, рот ходит.

Сюда попадает всё, что персона пишет в чат, откуда бы ни пришла реплика
пользователя: ответ в вебе, на телефоне, голосом, из поля у портрета — по
записи в историю (memory.on_stm_message), и фоновое сообщение (инициатива,
напоминание, урок) — из inbox_push. Часть фоновых пишется и в историю тем же
текстом: повтор той же реплики в чат за DEDUP_SEC второй раз не встаёт.

inbox тут не годится: ответ на реплику веб и телефон получают из своего
запроса, и его копия во входящих показалась бы там дважды. Здесь курсоров
клиентов нет — клиент сам присылает номер последней полученной реплики.

Вопрос, на который ждут ответа (режим управления «Делаю? (да/нет)», «Какой
открыть?», список «какой файл?»), идёт с options — варианты кнопок, как у
веба (BotInstance.cc_answer_options). Только у реплик хода пользователя:
фоновое сообщение посреди ожидания чужих кнопок не получает.

Строки хода агента задач («Нажал …», «Листаю страницу») — не реплики: их
запись в историю сервер пропускает сюда без них (server._without_task_lines
по журналу карточки задачи), как веб — пузырь.

Номера живут, пока жив процесс: EPOCH отличает этот процесс от прежнего.
Клиент с номером прошлого запуска (ядро перезапустили, пока он ждал)
получает реплики нового запуска за последние REPLAY_SEC — то, что персона
успела сказать, пока клиент переподключался; первый запрос (epoch пустой) —
только номер.

Кроме реплик — пауза до ответа (server._pause_heard): heard — пользователь
написал персоне, текст — его сообщение (портрет показывает его облачком
сбоку, кивает, «читает» и думает; у картинки без подписи текста нет),
settled — ход закончен (ответ уже в ленте или не случился), без текста.
Повтором они не считаются, после перезапуска ядра не повторяются —
устарели.

act — портрет на рабочем столе играет анимацию (app/api/portrait_anim):
персона выбрала её сама маркером [ACT:имя] в ответе или нажали
«Проиграть на рабочем столе» в карточке; текст — id анимации. Тоже не
реплика: без повторов и пересказа. Отдаётся только клиентам, которые
просят её (acts) — иначе старый клиент показал бы id облачком. Когда такой
клиент спрашивал последний раз, помнит acts_listener_recent: персоне
незачем знать про своё «тело», если портрета на столе нет.

look — в вебе сменили тему оформления (app/core/ui_look): портрету
перечитать, как рисовать голову. Без персоны и текста, во все чаты, тоже
только клиентам с acts.

portrait — у персоны сохранили портрет или анимации (вкладки «Портрет»,
«Анимации», «Кадры» раздела «Аватар»): её портрету на столе перечитать
кадры сразу, а не к следующему опросу. С персоной, без текста, во все чаты,
только клиентам с acts.

emo — лицо портрета меняет выражение (app/api/portrait_api, эмоции):
персона выбрала эмоцию маркером [EMO:имя] в ответе или нажали «Показать на
рабочем столе»; текст — id эмоции. Приходит перед репликой, к которой она.
Только клиентам, которые просят её (emo): портрет на рабочем столе,
голосовой режим и PiP веба. Когда такой клиент спрашивал последний раз,
помнит emo_listener_recent: без живого лица нота про эмоции персоне ни к чему.

mode — в чате сменился режим персоны (app/core/persona_modes: имя режима,
ключевое слово, «/mode»): аватар идёт за режимом (app/api/avatar_modes) —
портрету на столе и виджету телефона перечитать его и сыграть переход. С
персоной, без текста, в свой чат; всем клиентам (прежние без текста его не
покажут), без повторов и пересказа.
"""

import asyncio
import threading
import time
import uuid
from collections import deque
from typing import Awaitable, Callable, Optional

from app.api import inbox as _inbox
from app.api.delivery import client_allowed

EPOCH = uuid.uuid4().hex[:12]
MAX_WAIT_SEC = 600
DEDUP_SEC = 120
REPLAY_SEC = 120
_STOP_POLL_SEC = 1.0
# События паузы до ответа: не реплики — без повторов и без пересказа после
# перезапуска ядра; у settled (и у heard на картинку без подписи) текста нет
PAUSE_KINDS = frozenset({"heard", "settled"})
ACT_KIND = "act"
EMO_KIND = "emo"
LOOK_KIND = "look"
PORTRAIT_KIND = "portrait"
MODE_KIND = "mode"
INITIATIVE_KIND = "initiative"
REMINDER_KIND = "reminder"
DEFERRED_KINDS = frozenset({INITIATIVE_KIND, REMINDER_KIND})
# Во все чаты: про портрет, а не про разговор
BROADCAST_KINDS = frozenset({LOOK_KIND, PORTRAIT_KIND})
# Только клиентам с acts (портрет на рабочем столе): старый показал бы облачком
ACTS_ONLY_KINDS = frozenset({ACT_KIND}) | BROADCAST_KINDS
# Не реплики: без повторов по тексту и без пересказа после перезапуска ядра
EVENT_KINDS = PAUSE_KINDS | ACTS_ONLY_KINDS | {MODE_KIND, EMO_KIND} | DEFERRED_KINDS
# Без текста — и так событие
EMPTY_KINDS = PAUSE_KINDS | BROADCAST_KINDS | {MODE_KIND}

_lock = threading.Lock()
_seq = 0
_recent: deque = deque(maxlen=64)
# Ожидающие запросы: (их event loop, событие). note зовут из разных потоков
# (генерация в пуле, фоновый loop менеджеров) — будим через call_soon_threadsafe
_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
# Когда клиент с acts (с emo) последний раз опрашивал ленту: чат → time.time()
_acts_seen: dict[str, float] = {}
_emo_seen: dict[str, float] = {}
_expected_kinds: dict[tuple[str, str, str], tuple[str, float]] = {}


def expect_kind(persona: str, chat_id: str, text: str, kind: str) -> None:
    """Classify the next STM write for a known background message.

    Proactive messages are committed to memory before delivery. The STM hook
    fires during that commit, before inbox_push can label the same text, so
    keep a short-lived classification for that exact message.
    """
    key = (persona, str(chat_id), " ".join((text or "").split()))
    now = time.time()
    with _lock:
        for stale in [k for k, (_, expires) in _expected_kinds.items() if expires < now]:
            _expected_kinds.pop(stale, None)
        _expected_kinds[key] = (kind, now + DEDUP_SEC)


def note(persona: str, chat_id: str, text: str, kind: str = "reply",
         options: Optional[dict] = None) -> None:
    """Персона сказала text в чат chat_id (из любого потока). options —
    кнопки ответа на этот вопрос (BotInstance.cc_answer_options): портрет
    рисует их у облачка, кнопка шлёт обычную реплику."""
    global _seq
    text = (text or "").strip()
    if not text and kind not in EMPTY_KINDS:
        return
    chat_id = str(chat_id)
    now = time.time()
    with _lock:
        key = (persona, chat_id, " ".join(text.split()))
        expected = _expected_kinds.pop(key, None) if kind == "reply" else None
        if expected:
            expected_kind, expires = expected
            if expires >= now:
                kind = expected_kind
        # Инициативы и напоминания — отложенные реплики: копии STM/inbox убираются
        # так же, как копии обычного ответа. Анимации и паузы повторимы.
        repeatable = kind in EVENT_KINDS and kind not in DEFERRED_KINDS
        for m in reversed(_recent):
            if repeatable or now - m["ts"] > DEDUP_SEC:
                break
            if m["persona"] != persona or m["chat_id"] != chat_id:
                continue
            # Вы написали снова — тот же ответ («Да.», тот же вопрос с
            # кнопками) на новую реплику уже не повтор
            if m["kind"] == "heard":
                break
            if m["text"] == text:
                if m["kind"] == kind:
                    return
                if kind in DEFERRED_KINDS and m["kind"] == "reply":
                    # Старую реплику мог уже забрать портрет. Отправляем
                    # исправленный тип с новым seq, чтобы он убрал облачко.
                    m["kind"] = kind
                    m.pop("options", None)
                    break
                if m["kind"] not in EVENT_KINDS or m["kind"] in DEFERRED_KINDS:
                    return
        _seq += 1
        entry = {"seq": _seq, "persona": persona, "chat_id": chat_id,
                 "text": text, "kind": kind, "ts": now}
        if options and kind not in EVENT_KINDS:
            entry["options"] = options
        _recent.append(entry)
        waiters = list(_waiters)
    for loop, event in waiters:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # loop уже закрыт — ядро останавливается


def take(since: Optional[int], chat_id: str = "web_user",
         fresh_sec: Optional[float] = None, acts: bool = False,
         emo: bool = False) -> tuple[list[dict], int]:
    """Реплики чата после номера since → (реплики, номер последней).
    since=None — только номер. fresh_sec — не старше стольких секунд.
    acts — отдавать и анимации (kind act), emo — и эмоции лица (kind emo)."""
    chat_id = str(chat_id)
    with _lock:
        if since is None or since > _seq:
            return [], _seq
        oldest = time.time() - fresh_sec if fresh_sec is not None else 0
        # Пересказ после перезапуска ядра (fresh_sec) — без кнопок: вопрос,
        # которого ждали, прежний процесс унёс с собой
        items = [{**{k: m[k] for k in ("persona", "text", "kind", "ts")},
                  **({"options": m["options"]} if "options" in m and fresh_sec is None else {})}
                 for m in _recent
                 if m["seq"] > since and (m["chat_id"] == chat_id or m["kind"] in BROADCAST_KINDS)
                 and m["ts"] >= oldest
                 and client_allowed(m["persona"], "")
                 and (acts or m["kind"] not in ACTS_ONLY_KINDS)
                 and (emo or m["kind"] != EMO_KIND)
                 and not (fresh_sec is not None and m["kind"] in EVENT_KINDS)]
        return items, _seq


def cursor() -> int:
    """Номер последней записи ленты."""
    with _lock:
        return _seq


async def wait(since: Optional[int], timeout: float, chat_id: str = "web_user",
               fresh_sec: Optional[float] = None,
               gone: Optional[Callable[[], Awaitable[bool]]] = None,
               acts: bool = False, emo: bool = False) -> tuple[list[dict], int]:
    """Как take, но пустой ответ ждёт до timeout секунд (не больше
    MAX_WAIT_SEC): отвечает, как только персона что-то скажет в этот чат, по
    истечении времени, при остановке ядра или когда клиент ушёл (gone)."""
    items, cursor = take(since, chat_id, fresh_sec, acts, emo)
    timeout = min(max(timeout, 0.0), MAX_WAIT_SEC)
    if items or since is None or timeout <= 0:
        return items, cursor
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    event = asyncio.Event()
    waiter = (loop, event)
    with _lock:
        _waiters.add(waiter)
    try:
        while True:
            # Сброс до проверки: реплика, пришедшая между ними, снова поднимет
            # событие. Дальше — с номера уже проверенного: реплики другого
            # чата не отдаются, но и второй раз не перебираются
            event.clear()
            items, cursor = take(cursor, chat_id, acts=acts, emo=emo)
            if items:
                return items, cursor
            while not event.is_set():
                left = deadline - loop.time()
                if left <= 0 or _inbox._stopping() or (gone is not None and await gone()):
                    return [], cursor
                try:
                    await asyncio.wait_for(event.wait(), min(left, _STOP_POLL_SEC))
                except (asyncio.TimeoutError, TimeoutError):
                    pass
    finally:
        with _lock:
            _waiters.discard(waiter)


def look_changed() -> None:
    """В вебе сменили тему оформления — портрету на столе перерисовать голову."""
    note("", "*", "", kind=LOOK_KIND)


def portrait_changed(persona: str) -> None:
    """У персоны сохранили портрет или анимации — её портрету на столе
    перечитать кадры сейчас же."""
    note(persona, "*", "", kind=PORTRAIT_KIND)


def mode_changed(persona: str, chat_id: str) -> None:
    """В чате сменился режим персоны — аватар чата другой."""
    note(persona, chat_id, "", kind=MODE_KIND)


def mark_acts_listener(chat_id: str = "web_user") -> None:
    """Клиент с acts (портрет на рабочем столе) опрашивает ленту чата."""
    with _lock:
        _acts_seen[str(chat_id)] = time.time()


def acts_listener_recent(sec: float, chat_id: str = "web_user") -> bool:
    """Опрашивал ли ленту чата клиент с acts за последние sec секунд."""
    with _lock:
        at = _acts_seen.get(str(chat_id))
    return at is not None and time.time() - at <= sec


def mark_emo_listener(chat_id: str = "web_user") -> None:
    """Клиент с emo (живое лицо: портрет на столе, голосовой режим, PiP)
    опрашивает ленту чата."""
    with _lock:
        _emo_seen[str(chat_id)] = time.time()


def emo_listener_recent(sec: float, chat_id: str = "web_user") -> bool:
    """Опрашивал ли ленту чата клиент с emo за последние sec секунд."""
    with _lock:
        at = _emo_seen.get(str(chat_id))
    return at is not None and time.time() - at <= sec

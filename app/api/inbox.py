"""Доставка фоновых сообщений (напоминания, уроки, инициативы) в веб.

В Telegram-режиме менеджеры шлют через TelegramMessageSender. В API-режиме
транспорт — WebInboxSender: сообщение кладётся в очередь (persona, chat_id),
а фронт забирает её polling'ом GET /api/personas/{p}/inbox.

Здесь же — фоновый event loop API-режима: reminder/learning/proactive
запускаются на нём (как на loop'е бота в main.py).
"""

import asyncio
import logging
import threading
import time
import uuid
from collections import deque
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Очередь входящих: (persona, chat_id) → сообщения с порядковым номером seq.
# In-memory: переживает только жизнь процесса — при рестарте недоставленное
# теряется (приемлемо: напоминание/урок повторится по расписанию, если не
# сработало).
#
# Устройств может быть несколько (веб на ноутбуке и приложение на телефоне),
# и каждое должно получить каждое сообщение. Поэтому очередь не очищается
# при чтении: у каждого клиента (client_id — случайный id, который веб хранит
# у себя) свой курсор — номер последнего полученного сообщения. Клиент,
# который опрашивает впервые, получает только то, что ещё никому не
# доставлено, — старые сообщения есть и в истории чата.
_inbox: dict[tuple[str, str], deque] = {}
_inbox_lock = threading.Lock()
_MAX_QUEUED = 100
_seq = 0
# Наибольший номер, отданный хоть одному клиенту, по ключу очереди
_delivered: dict[tuple[str, str], int] = {}
# client_id → {ключ очереди → номер последнего полученного}
_cursors: dict[str, dict[tuple[str, str], int]] = {}
_client_seen: dict[str, float] = {}
# (client_id, chat_id) → сквозной номер на момент последнего общего опроса
# (inbox_pop_all). Очередь персоны, появившаяся после него, у клиента ещё
# без курсора — с этой отметки, а не «только недоставленное»: иначе
# сообщение новой очереди, которое первым забрал другой клиент, этот бы
# пропустил
_all_seen: dict[tuple[str, str], int] = {}
_MAX_CLIENTS = 16
_CLIENT_TTL_SEC = 7 * 24 * 3600

# Долгий опрос (inbox_wait): фоновая служба телефона держит запрос открытым,
# пока не появится сообщение, — телефон спит, а пришедший ответ его будит.
# Сквозные номера seq живут, пока жив процесс: EPOCH отличает этот процесс от
# прежнего, и курсор клиента (since) из прошлого запуска не применяется
EPOCH = uuid.uuid4().hex[:12]
MAX_WAIT_SEC = 600
# Ожидающие запросы: (их event loop, событие «пришло новое»). inbox_push
# зовут из разных потоков (фоновый loop менеджеров, потоки ответа) — будим
# через call_soon_threadsafe
_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
# Ядро останавливается (uvicorn: should_exit) — ожидающие отвечают сразу,
# иначе остановка ждала бы их до конца ожидания (минуты)
_stop_check: Optional[Callable[[], bool]] = None
_STOP_POLL_SEC = 1.0


def inbox_push(persona: str, chat_id: str, text: str, kind: str = "message"):
    global _seq
    key = (persona, str(chat_id))
    with _inbox_lock:
        _seq += 1
        q = _inbox.setdefault(key, deque(maxlen=_MAX_QUEUED))
        q.append({"seq": _seq, "text": text, "kind": kind, "ts": time.time()})
        waiters = list(_waiters)
    for loop, event in waiters:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # loop уже закрыт — ядро останавливается


def _forget_stale_clients(now: float) -> None:
    # Под _inbox_lock. Давно не опрашивавшие клиенты и лишние сверх лимита
    # (сначала самые давние) — их курсоры больше не нужны
    gone = [c for c, t in _client_seen.items() if now - t > _CLIENT_TTL_SEC]
    for cid in gone:
        _client_seen.pop(cid, None)
        _cursors.pop(cid, None)
    while len(_client_seen) > _MAX_CLIENTS:
        cid = min(_client_seen, key=_client_seen.get)
        _client_seen.pop(cid, None)
        _cursors.pop(cid, None)
        gone.append(cid)
    if gone:
        for key in [k for k in _all_seen if k[0] not in _client_seen]:
            _all_seen.pop(key, None)


def _pop_locked(key: tuple[str, str], client_id: str, cursors: dict) -> list[dict]:
    # Под _inbox_lock: новые для клиента сообщения очереди key (с его
    # курсора cursors[key]); курсор и отметка доставки двигаются
    start = cursors.get(key)
    if start is None:
        start = _all_seen.get((client_id, key[1]))
    if start is None:
        start = _delivered.get(key, 0)
    q = _inbox.get(key) or ()
    items = [m for m in q if m["seq"] > start]
    if items:
        start = items[-1]["seq"]
        _delivered[key] = max(_delivered.get(key, 0), start)
    cursors[key] = start
    return items


def inbox_pop(persona: str, chat_id: str, client_id: str = "") -> list[dict]:
    # Новые для этого клиента сообщения (с его курсора); у остальных
    # клиентов они остаются непрочитанными
    key = (persona, str(chat_id))
    now = time.time()
    with _inbox_lock:
        _client_seen[client_id] = now
        items = _pop_locked(key, client_id, _cursors.setdefault(client_id, {}))
        _forget_stale_clients(now)
        return [{"text": m["text"], "kind": m["kind"], "ts": m["ts"]} for m in items]


def inbox_pop_all(chat_id: str, client_id: str = "") -> list[dict]:
    # Новые для клиента сообщения сразу по всем персонам чата chat_id —
    # один запрос вместо опроса каждой персоны (фоновая служба приложения
    # на телефоне). Курсор тот же, что у inbox_pop: по каждой очереди
    # (persona, chat_id) клиент получает сообщение один раз, как бы он ни
    # спрашивал — по персоне или по всем сразу. Порядок — порядок появления
    return inbox_take(chat_id, client_id)[0]


def inbox_take(chat_id: str, client_id: str = "", since: Optional[int] = None) -> tuple[list[dict], int]:
    """Новые для клиента сообщения по всем персонам чата → (сообщения, курсор).

    since=None — с курсоров ядра, как inbox_pop_all. since — сквозной номер,
    до которого клиент уже всё получил (курсор из прошлого ответа): отдаётся
    всё, что после него. Так ответ, потерянный по дороге (телефон ушёл из
    Wi-Fi, пока ждал), придёт ещё раз — курсоры ядра на это не способны: они
    сдвигаются, когда ядро ответило, а не когда клиент получил. Номер больше
    текущего (курсор чужого процесса) не годится — тогда как since=None.
    Курсор ответа — сквозной номер, до которого клиент теперь получил всё."""
    chat_id = str(chat_id)
    now = time.time()
    with _inbox_lock:
        if since is not None and since > _seq:
            since = None
        _client_seen[client_id] = now
        cursors = _cursors.setdefault(client_id, {})
        got = []
        for key in [k for k in _inbox if k[1] == chat_id]:
            if since is None:
                got += [(key[0], m) for m in _pop_locked(key, client_id, cursors)]
                continue
            q = _inbox[key]
            items = [m for m in q if m["seq"] > since]
            if items:
                _delivered[key] = max(_delivered.get(key, 0), items[-1]["seq"])
            if q:
                cursors[key] = max(cursors.get(key, 0), q[-1]["seq"])
            got += [(key[0], m) for m in items]
        cursor = _all_seen[(client_id, chat_id)] = _seq
        _forget_stale_clients(now)
    got.sort(key=lambda pm: pm[1]["seq"])
    return [{"persona": p, "text": m["text"], "kind": m["kind"], "ts": m["ts"]} for p, m in got], cursor


def set_stop_check(fn: Optional[Callable[[], bool]]) -> None:
    """Признак «ядро останавливается» для долгого опроса (app/main.py)."""
    global _stop_check
    _stop_check = fn


def _stopping() -> bool:
    try:
        return bool(_stop_check and _stop_check())
    except Exception:
        return False


async def inbox_wait(chat_id: str, client_id: str, since: Optional[int], timeout: float,
                     gone: Optional[Callable[[], Awaitable[bool]]] = None) -> tuple[list[dict], int]:
    """Как inbox_take, но пустой ответ ждёт до timeout секунд (не больше
    MAX_WAIT_SEC): отвечает, как только для чата появится сообщение, по
    истечении времени или при остановке ядра. gone — клиент закрыл
    соединение (служба перезапустилась с новыми настройками): ждать больше
    некого, и сообщения для него не забираем."""
    items, cursor = inbox_take(chat_id, client_id, since)
    timeout = min(max(timeout, 0.0), MAX_WAIT_SEC)
    if items or timeout <= 0:
        return items, cursor
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    event = asyncio.Event()
    waiter = (loop, event)
    with _inbox_lock:
        _waiters.add(waiter)
    try:
        while True:
            # Сброс до проверки: сообщение, пришедшее между ними, снова
            # поднимет событие (call_soon_threadsafe выполнится после await)
            event.clear()
            items, cursor = inbox_take(chat_id, client_id, cursor)
            if items:
                return items, cursor
            while not event.is_set():
                left = deadline - loop.time()
                if left <= 0 or _stopping() or (gone is not None and await gone()):
                    return [], cursor
                try:
                    await asyncio.wait_for(event.wait(), min(left, _STOP_POLL_SEC))
                except (asyncio.TimeoutError, TimeoutError):
                    pass
    finally:
        with _inbox_lock:
            _waiters.discard(waiter)


def _move_key(old_key, new_key) -> None:
    # Под _inbox_lock: очередь, отметка доставки и курсоры клиентов — под
    # новый ключ (номера сквозные, порядок восстанавливается сортировкой)
    q = _inbox.pop(old_key, None)
    if q:
        merged = sorted([*_inbox.get(new_key, ()), *q], key=lambda m: m["seq"])
        _inbox[new_key] = deque(merged, maxlen=_MAX_QUEUED)
    if old_key in _delivered:
        _delivered[new_key] = max(_delivered.get(new_key, 0), _delivered.pop(old_key))
    for cursors in _cursors.values():
        if old_key in cursors:
            cursors[new_key] = max(cursors.get(new_key, 0), cursors.pop(old_key))


def inbox_rename(old: str, new: str):
    # Смена id персоны: недоставленные фоновые сообщения переезжают под новый id
    with _inbox_lock:
        keys = {k for k in _inbox if k[0] == old} | {k for k in _delivered if k[0] == old}
        for key in keys:
            _move_key(key, (new, key[1]))


def inbox_drop(persona: str):
    # Память id ушла в архив (новая персона «с чистого листа»): недоставленные
    # фоновые сообщения прежней персоны с тем же id новой не показываем
    with _inbox_lock:
        for key in [k for k in _inbox if k[0] == persona]:
            _inbox.pop(key, None)


class WebInboxSender:
    # MessageSender-совместимый транспорт: кладёт сообщения в inbox веб-чата.

    # Веб-чат показывает из inbox только текст (kind не рендерится), файла
    # пользователь не увидит — обучение шлёт урок текстом. У веб-сообщений
    # нет id, reply на вопрос невозможен.
    supports_documents = False
    supports_replies = False

    def __init__(self, persona: str):
        self._persona = persona

    async def send_message(self, chat_id: str, text: str, *,
                           topic_id=None, parse_mode=None) -> bool:
        inbox_push(self._persona, chat_id, text)
        return True

    async def send_document(self, chat_id: str, file_path: str, filename: str, *,
                            caption=None, topic_id=None, parse_mode=None) -> bool:
        # Файл через inbox не доставить — шлём уведомление с именем и подписью
        text = f"📎 {filename}"
        if caption:
            text += f"\n{caption}"
        inbox_push(self._persona, chat_id, text, kind="document")
        return True

    def get_last_sent_message_id(self, chat_id: str):
        return None  # у веб-сообщений нет id — reply-to-логика обучения не используется


# ── Фоновый event loop для reminder/learning/proactive ────────────────

_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_lock = threading.Lock()


def _run_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def background_loop() -> asyncio.AbstractEventLoop:
    # Общий фоновый loop API-процесса (создаётся при первом боте с фичами).
    global _bg_loop
    with _bg_lock:
        if _bg_loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=_run_loop, args=(loop,), daemon=True, name="api-bg").start()
            _bg_loop = loop
    return _bg_loop


def wire_reminder_for_api(persona: str, bot, sender=None) -> None:
    # Подключает reminder-менеджер бота к веб-inbox (sender/память/LLM-текст/
    # заморозка) и запускает его фоновый цикл. Используется при старте бота
    # и при живом включении фичи reminder через настройки (без рестарта сервера).
    if bot.reminder_manager is None:
        return
    sender = sender or getattr(bot, "_api_inbox_sender", None) or WebInboxSender(persona)
    bot._api_inbox_sender = sender
    rm = bot.reminder_manager
    rm.set_sender(sender)
    rm.set_memory(bot.memory)
    # Текст напоминаний генерируется LLM в характере персоны (как в TG)
    rm.set_router_persona(bot.router, bot.persona)
    # Заморозка персоны: напоминания молчат. bot.is_muted перечитывает флаг из
    # YAML персоны — видна и заморозка прямой правкой файла, не только из веба
    rm.set_muted_check(getattr(bot, "is_muted", None)
                       or (lambda: bool((bot.features or {}).get("muted"))))
    loop = background_loop()
    loop.call_soon_threadsafe(rm.start, loop)


def wire_rhythm_for_api(persona: str, bot, sender=None) -> None:
    # Подключает rhythm-менеджер (утро/ночь/погода) к веб-inbox и запускает
    # его фоновый цикл. Используется при старте бота и при живом включении
    # features.rhythm через настройки (без рестарта сервера).
    sender = sender or getattr(bot, "_api_inbox_sender", None) or WebInboxSender(persona)
    bot._api_inbox_sender = sender
    if getattr(bot, "rhythm", None) is None:
        bot.setup_rhythm(sender)  # no-op, если фича выключена в YAML
    rm = getattr(bot, "rhythm", None)
    if rm is None:
        return
    rm.set_sender(sender)
    loop = background_loop()
    loop.call_soon_threadsafe(rm.start, loop)


def start_bot_features(persona: str, bot):
    # Подключает inbox-sender и запускает фоновые циклы бота (идемпотентно).
    if getattr(bot, "_api_features_started", False):
        return
    bot._api_features_started = True

    sender = WebInboxSender(persona)
    bot._api_inbox_sender = sender
    loop = background_loop()

    if bot._activity_tracker is not None:
        bot.setup_proactive(sender)
    wire_reminder_for_api(persona, bot, sender=sender)
    if bot.learning_manager is not None:
        bot.setup_learning(sender)
    wire_rhythm_for_api(persona, bot, sender=sender)

    def _start_all():
        if bot.proactive is not None:
            bot.proactive.start(loop=loop)
        if bot.learning_manager is not None:
            bot.learning_manager.start(loop=loop)
        if bot.living is not None:
            bot.living.start(loop=loop)

    loop.call_soon_threadsafe(_start_all)
    logger.info(f"[api] Фоновые циклы запущены для {persona}")

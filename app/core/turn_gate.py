"""Гейт «ход пользователя ↔ фоновое сообщение» на чат (один на BotInstance).

Корень проблемы (docs/concurrency-issues-2026-09-23.md, п. 3–5): фоновые
сообщения — инициативы, рефлексия, сигнал состояния, утро/ночь/погода
ритма, напоминания, уроки — генерируются минутами на отдельном event loop
(api-bg / loop Telegram-бота) и потом без перепроверки пишутся в STM и
уходят пользователю. Живого обмена в чате они не видят: `chat_lock`
сервера — asyncio-лок другого loop, `_generating` — множество сервера, а в
Telegram нет и этого. Отсюда фоновое сообщение посреди хода пользователя,
порядок `user → инициатива → ответ` в STM и ложный зачёт ответа.

Гейт живёт на уровне BotInstance — его видят и API, и Telegram, и фоновые
менеджеры — и потокобезопасен (threading, не asyncio):

- ход пользователя открывается при ПОЛУЧЕНИИ сообщения (до распознавания
  фото/файла, до записи реплики в STM) и закрывается после ДОСТАВКИ ответа
  (части split, «печать» SSE): счётчик ходов и номер реплики (epoch) на чат;
- фоновое сообщение «коммитится» (`commit_message`) под per-chat локом:
  проверка «хода нет / пользователь не писал с метки / сообщение ещё
  актуально» и запись в STM — одна атомарная операция относительно начала
  нового хода (`begin_turn` берёт тот же лок). Ход идёт — коммит сразу
  отказывает (без ожидания): фоновый цикл повторяет на следующем тике.
  Сетевая доставка — уже после выхода из лока, при неудаче —
  `rollback_message` по идентичности записи.

Кадр хода (frame) переносится через contextvars: asyncio.to_thread копирует
контекст, поэтому process_message в рабочем потоке подхватывает ход,
открытый обработчиком Telegram/сервера на event loop (реентерабельность),
а ссылки (refs) держат ход, пока жив хоть один участник — например,
генерация, продолжающаяся после разрыва SSE.

Epoch, а не время: номер реплики строго упорядочен тем же локом, что и
коммит, — сравнение «реплика раньше/позже инициативы» не зависит от
разрешения часов и их прыжков.
"""

import asyncio
import contextvars
import itertools
import logging
import threading
import time
from collections import namedtuple
from contextlib import contextmanager
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Ход старше этого считается зависшим (поток генерации не вернулся): в
# busy/commit не учитывается, иначе один зависший ход навсегда глушил бы
# весь фон чата. Самый долгий нормальный ход — очередь веб-чатов + ответ
# (~10 мин), с запасом
TURN_TTL_SECONDS = 15 * 60

# Итог commit_message
COMMIT_OK = "ok"
COMMIT_BUSY = "busy"            # идёт ход пользователя — повторить позже
COMMIT_STALE = "stale"          # пользователь писал после метки — неактуально
COMMIT_CANCELLED = "cancelled"  # still_valid() ложь (отменено в ходе)
COMMIT_FAILED = "failed"        # запись в STM упала, в буфере ничего нет

CommitResult = namedtuple("CommitResult", "status entry epoch")

# chat_key → frame: ходы, открытые в ТЕКУЩЕМ контексте (поток/задача)
_FRAMES: contextvars.ContextVar = contextvars.ContextVar("turn_frames", default=None)
_ids = itertools.count(1)


class ChatTurnGate:
    def __init__(self):
        # Короткий лок счётчиков: busy()/epoch() с event loop не ждут записи
        # коммита в ChromaDB
        self._state = threading.Lock()
        # Per-chat лок коммита: держится на время записи в STM; begin_turn
        # берёт его же — начало хода упорядочено с идущим коммитом
        self._meta = threading.Lock()
        self._commit_locks: Dict[str, threading.Lock] = {}
        self._turns: Dict[str, Dict[int, dict]] = {}
        self._epoch: Dict[str, int] = {}

    def _commit_lock(self, key: str) -> threading.Lock:
        with self._meta:
            lock = self._commit_locks.get(key)
            if lock is None:
                lock = self._commit_locks[key] = threading.Lock()
            return lock

    def _active_locked(self, key: str) -> bool:
        """Есть ли живой (не зависший) ход. Под self._state."""
        now = time.monotonic()
        live = False
        for frame in (self._turns.get(key) or {}).values():
            if now - frame["started"] < TURN_TTL_SECONDS:
                live = True
            elif not frame.get("stale_warned"):
                frame["stale_warned"] = True
                logger.warning(f"[TurnGate] Ход в чате {key} идёт дольше "
                               f"{TURN_TTL_SECONDS // 60} мин — считаю зависшим, "
                               "фоновые сообщения его больше не ждут")
        return live

    # ── ход пользователя: явные begin/retain/release ──

    def begin_turn(self, chat_id) -> dict:
        """Открыть ход (при получении сообщения). Может коротко ждать идущий
        коммит — с event loop звать через asyncio.to_thread. Кадр закрывается
        release(); пока на него есть ссылки, ход считается идущим."""
        key = str(chat_id)
        with self._commit_lock(key):
            with self._state:
                epoch = self._epoch.get(key, 0) + 1
                self._epoch[key] = epoch
                frame = {"key": key, "id": next(_ids), "epoch": epoch,
                         "started": time.monotonic(), "refs": 1, "closed": False}
                self._turns.setdefault(key, {})[frame["id"]] = frame
        return frame

    def retain(self, frame: dict):
        """Ещё один участник хода (рабочий поток генерации). Кадр уже закрыт
        (обработчик ушёл раньше, чем поток стартовал) — ход снова идущий."""
        with self._state:
            frame["refs"] += 1
            if frame["closed"]:
                frame["closed"] = False
                self._turns.setdefault(frame["key"], {})[frame["id"]] = frame

    def release(self, frame: dict):
        """Участник хода вышел; последний — ход закрыт. Идемпотентно к
        лишним вызовам (refs не уходит ниже нуля)."""
        with self._state:
            if frame["refs"] > 0:
                frame["refs"] -= 1
            if frame["refs"] == 0 and not frame["closed"]:
                frame["closed"] = True
                turns = self._turns.get(frame["key"])
                if turns is not None:
                    turns.pop(frame["id"], None)
                    if not turns:
                        self._turns.pop(frame["key"], None)

    @contextmanager
    def adopt(self, frame: dict):
        """Сделать кадр текущим в этом контексте (и держать ход, пока внутри)."""
        self.retain(frame)
        frames = dict(_FRAMES.get() or {})
        frames[frame["key"]] = frame
        token = _FRAMES.set(frames)
        try:
            yield frame
        finally:
            _FRAMES.reset(token)
            self.release(frame)

    @contextmanager
    def user_turn(self, chat_id):
        """Ход пользователя (синхронный контекст). Если в текущем контексте
        ход этого чата уже открыт (обработчик Telegram/сервера, внешний
        user_turn) — присоединяется к нему, новой реплики не заводит."""
        key = str(chat_id)
        frame = (_FRAMES.get() or {}).get(key)
        if frame is not None:
            with self.adopt(frame):
                yield frame
            return
        frame = self.begin_turn(key)
        try:
            with self.adopt(frame):
                yield frame
        finally:
            self.release(frame)

    def current_frame(self, chat_id) -> Optional[dict]:
        """Кадр хода этого чата в ТЕКУЩЕМ контексте (или None)."""
        return (_FRAMES.get() or {}).get(str(chat_id))

    def current_turn_epoch(self, chat_id) -> Optional[int]:
        frame = self.current_frame(chat_id)
        return frame["epoch"] if frame else None

    # ── фоновые сообщения ──

    def epoch(self, chat_id) -> int:
        """Номер последней реплики пользователя в чате — метка «момент старта
        генерации» фонового сообщения (см. commit_message)."""
        with self._state:
            return self._epoch.get(str(chat_id), 0)

    def busy(self, chat_id) -> bool:
        """Идёт ли сейчас ход пользователя в чате (дёшево, без ожидания
        коммита; окончательное решение — только в commit_message)."""
        with self._state:
            return self._active_locked(str(chat_id))

    def commit_message(self, memory, chat_id, text: str,
                       since_epoch: Optional[int] = None,
                       still_valid: Optional[Callable[[], bool]] = None) -> CommitResult:
        """Атомарно: проверить, что хода нет (и, если задан since_epoch, что
        пользователь не писал после старта генерации; если задан
        still_valid — что сообщение ещё актуально: напоминание не отменено
        в только что закончившемся ходе), и записать сообщение ассистента в
        STM. Не ждёт: идёт ход — COMMIT_BUSY сразу.

        OK → entry — сама запись буфера (для rollback_message; None, если
        memory нет — проверки без записи), epoch — номер последней реплики
        (реплики с бо́льшим номером пришли ПОСЛЕ этого сообщения).

        Синхронный (под локом пишет в ChromaDB) — из корутины звать через
        asyncio.to_thread."""
        key = str(chat_id)
        with self._commit_lock(key):
            with self._state:
                active = self._active_locked(key)
                epoch = self._epoch.get(key, 0)
            if active:
                return CommitResult(COMMIT_BUSY, None, epoch)
            if since_epoch is not None and epoch != since_epoch:
                return CommitResult(COMMIT_STALE, None, epoch)
            if still_valid is not None and not still_valid():
                return CommitResult(COMMIT_CANCELLED, None, epoch)
            if memory is None:
                return CommitResult(COMMIT_OK, None, epoch)
            before = self._last_entry(memory, key)
            try:
                memory.add_message("assistant", text, user_id=chat_id, chat_id=chat_id)
            except Exception as e:
                # Частичная запись: буфер записан, а ChromaDB/хвост add_message
                # упал — запись в истории ЕСТЬ, и её надо уметь откатить, иначе
                # недоставленное сообщение осталось бы в STM
                entry = self._landed(memory, key, text, before)
                if entry is None:
                    logger.warning(f"[TurnGate] Запись в STM ({key}) не удалась: {e}")
                    return CommitResult(COMMIT_FAILED, None, epoch)
                logger.warning(f"[TurnGate] Запись в STM ({key}) частичная "
                               f"(буфер записан): {e}")
                return CommitResult(COMMIT_OK, entry, epoch)
            return CommitResult(COMMIT_OK, self._landed(memory, key, text, before), epoch)

    @staticmethod
    def _last_entry(memory, key: str):
        try:
            msgs = memory.stm.get_messages(chat_id=key)
            return msgs[-1] if msgs else None
        except Exception:
            return None

    @classmethod
    def _landed(cls, memory, key: str, text: str, before):
        """Запись, которую только что сделал коммит (последняя в буфере,
        новая относительно before, наш текст) — или None."""
        last = cls._last_entry(memory, key)
        if (last is not None and last is not before
                and last.get("role") == "assistant" and last.get("content") == text):
            return last
        return None

    @staticmethod
    def rollback_message(memory, chat_id, entry) -> bool:
        """Откат commit_message, если доставка не удалась: пользователь
        сообщения не увидел — в истории его быть не должно. Удаляется
        ИМЕННО записанная запись (по идентичности, под локом STM), а не
        «последняя с таким текстом»/по индексу снапшота: при полном deque
        или конкурентной записи те способы сносили чужую реплику."""
        if entry is None or memory is None:
            return False
        key = str(chat_id)
        try:
            remove = getattr(memory.stm, "remove_entry", None)
            if callable(remove):
                return bool(remove(key, entry))
            msgs = memory.stm.get_messages(chat_id=key)
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i] is entry:
                    return bool(memory.stm.delete_message(key, i))
        except Exception as e:
            logger.warning(f"[TurnGate] Откат недоставленного сообщения в STM "
                           f"({key}) не удался: {e}")
        return False


async def begin_turn_async(gate: ChatTurnGate, chat_id) -> dict:
    """begin_turn с event loop: в потоке (может ждать коммит), и так, чтобы
    отмена ожидающего не оставила открытый ход навсегда."""
    fut = asyncio.ensure_future(asyncio.to_thread(gate.begin_turn, chat_id))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        fut.add_done_callback(
            lambda f: gate.release(f.result()) if not f.cancelled() and f.exception() is None else None)
        raise

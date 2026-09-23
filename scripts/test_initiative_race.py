"""Гонка «ход пользователя ↔ фоновое сообщение» (docs/concurrency-issues-
2026-09-23.md, п. 3–5): гейт хода app/core/turn_gate.py.

Проверяет офлайн, на фейковых STM/отправителе и заглушке BotInstance:
- инициатива, сгенерированная во время хода пользователя или после его
  реплики, отбрасывается: не уходит и не попадает в STM; рефлексия после
  «монолог промолчал, потому что пользователь написал» не генерируется;
  сигнал состояния — так же;
- порядок STM user → ответ не рвётся;
- реплика, написанная ДО инициативы, не засчитывается как ответ на неё;
  реплика ПОСЛЕ — засчитывается;
- утро/ночь ритма не уходят посреди хода: ждут конца ответа и встают после
  него; не дождались — не уходят и не отмечаются;
- _pipeline_failure_reply пишет ошибку, только если ответа именно на
  текущую реплику нет; _rewrite_image_stm сервера не сносит инициативу.

Запуск: python -m scripts.test_initiative_race
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


class FakeStm:
    """Минимальный STM: буферы чатов, get_messages отдаёт те же dict'ы."""

    def __init__(self):
        self.buffers = {}
        self._lock = threading.RLock()

    def _buf(self, chat_id):
        return self.buffers.setdefault(str(chat_id), [])

    def add_message(self, role, content, user_id="default", chat_id=None, user_name=None):
        with self._lock:
            self._buf(chat_id if chat_id is not None else user_id).append(
                {"role": role, "content": content, "timestamp": time.time()})

    def get_messages(self, user_id=None, chat_id=None):
        with self._lock:
            return list(self._buf(chat_id if chat_id is not None else user_id))

    def get_last(self, n, user_id=None, chat_id=None):
        return self.get_messages(user_id, chat_id)[-n:]

    def delete_message(self, chat_id, index):
        with self._lock:
            buf = self._buf(chat_id)
            if 0 <= index < len(buf):
                del buf[index]
                return True
            return False

    def pop_last_n(self, n, chat_id):
        with self._lock:
            buf = self._buf(chat_id)
            n = min(n, len(buf))
            if n:
                del buf[-n:]
            return n


class FakeMemory:
    def __init__(self):
        self.stm = FakeStm()

    def add_message(self, role, content, user_id="default", chat_id=None,
                    user_name=None, light_mode=None):
        self.stm.add_message(role, content, user_id, chat_id, user_name)


class FakeSender:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    async def send_message(self, chat_id, text, *, topic_id=None, parse_mode=None):
        self.sent.append((str(chat_id), text))
        return self.ok


def main():
    tmp = tempfile.mkdtemp(prefix="initiative_race_")
    os.environ["DATA_DIR"] = tmp
    # ProactiveMessaging/RhythmManager пишут в относительный data/ — уходим в tmp
    os.chdir(tmp)

    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    from app.core import turn_gate as turn_gate_mod
    from app.core.turn_gate import ChatTurnGate, begin_turn_async
    from app.bot_instance import BotInstance
    from app.features import rhythm_manager as rhythm_mod
    from app.features.proactive_messaging import (
        InitiativeType, ProactiveConfig, ProactiveMessaging)
    from app.features.rhythm_manager import RhythmConfig, RhythmManager
    from app.core import timeutil

    def contents(mem, chat):
        return [(m["role"], m["content"]) for m in mem.stm.get_messages(chat_id=chat)]

    # ── 1. Гейт: атомарный коммит ──
    print("\n── 1. ChatTurnGate ──")
    gate = ChatTurnGate()
    mem = FakeMemory()
    mark = gate.epoch("c")
    with gate.user_turn("c") as frame:
        check("ход открыт: busy", gate.busy("c"))
        r = gate.commit_message(mem, "c", "ини")
        check("коммит посреди хода — busy сразу, STM пуст",
              r.status == "busy" and contents(mem, "c") == [])
        with gate.user_turn("c") as inner:
            check("вложенный ход того же контекста — тот же ход (epoch не растёт)",
                  inner is frame and gate.epoch("c") == mark + 1)
        check("номер хода виден из текущего контекста",
              gate.current_turn_epoch("c") == mark + 1)
    check("ход закрыт: не busy", not gate.busy("c"))
    check("пользователь писал после метки — stale",
          gate.commit_message(mem, "c", "ини", since_epoch=mark).status == "stale")
    r = gate.commit_message(mem, "c", "ини", since_epoch=gate.epoch("c"),
                            still_valid=lambda: False)
    check("still_valid ложь — cancelled, в STM ничего",
          r.status == "cancelled" and contents(mem, "c") == [])
    r = gate.commit_message(mem, "c", "ини", since_epoch=gate.epoch("c"))
    check("тишина — коммит принят, запись отдана по идентичности",
          r.status == "ok" and r.epoch == mark + 1
          and r.entry is mem.stm.get_messages(chat_id="c")[-1])
    check("откат удаляет именно эту запись",
          gate.rollback_message(mem, "c", r.entry) and contents(mem, "c") == [])
    other = []
    t = threading.Thread(target=lambda: other.append(gate.current_turn_epoch("c")))
    with gate.user_turn("c"):
        t.start()
        t.join()
    check("чужой поток хода не видит (кадр в contextvars)", other == [None])

    # Кадр переезжает в asyncio.to_thread, ссылки держат ход
    async def ctx_carry():
        fr = await begin_turn_async(gate, "c")
        seen = []
        with gate.adopt(fr):
            seen.append(await asyncio.to_thread(gate.current_turn_epoch, "c"))
        gate.release(fr)
        return fr, seen

    fr, seen = asyncio.run(ctx_carry())
    check("asyncio.to_thread видит ход обработчика (contextvars)", seen == [fr["epoch"]])
    check("все участники вышли — ход закрыт", not gate.busy("c"))
    fr = gate.begin_turn("c")
    worker_in, worker_out = threading.Event(), threading.Event()

    def worker():
        with gate.adopt(fr):
            worker_in.set()
            worker_out.wait(5)

    t = threading.Thread(target=worker)
    t.start()
    worker_in.wait(5)
    gate.release(fr)  # обработчик ушёл (разрыв SSE), генерация ещё идёт
    check("обработчик ушёл, поток генерации жив — ход идёт", gate.busy("c"))
    worker_out.set()
    t.join(5)
    check("поток генерации вышел — ход закрыт", not gate.busy("c"))

    # TTL: зависший ход не глушит фон навсегда
    old_ttl = turn_gate_mod.TURN_TTL_SECONDS
    turn_gate_mod.TURN_TTL_SECONDS = 0.05
    fr = gate.begin_turn("c")
    time.sleep(0.1)
    check("ход старше TTL не считается идущим (busy/commit)",
          not gate.busy("c") and gate.commit_message(mem, "c", "после TTL").status == "ok")
    gate.release(fr)
    turn_gate_mod.TURN_TTL_SECONDS = old_ttl
    mem.stm.buffers["c"] = []

    # Частичная запись: буфер записан, «ChromaDB» упала — откатываемая запись
    class HalfMemory(FakeMemory):
        def add_message(self, *a, **kw):
            super().add_message(*a, **kw)
            raise RuntimeError("chroma down")

    hm = HalfMemory()
    r = gate.commit_message(hm, "h", "полу")
    check("частичная запись — ok с записью (есть что откатить)",
          r.status == "ok" and r.entry is not None
          and gate.rollback_message(hm, "h", r.entry) and contents(hm, "h") == [])

    class DeadMemory(FakeMemory):
        def add_message(self, *a, **kw):
            raise RuntimeError("всё упало")

    check("запись не легла вовсе — failed",
          gate.commit_message(DeadMemory(), "h", "x").status == "failed")

    # Откат по идентичности при полном deque и конкурентной записи
    class DequeStm(FakeStm):
        def _buf(self, chat_id):
            return self.buffers.setdefault(str(chat_id), deque(maxlen=5))

        def remove_entry(self, chat_id, entry):
            with self._lock:
                buf = self._buf(chat_id)
                for i in range(len(buf) - 1, -1, -1):
                    if buf[i] is entry:
                        del buf[i]
                        return True
            return False

    dm = FakeMemory()
    dm.stm = DequeStm()
    for i in range(3):
        dm.add_message("user", f"u{i}", "u", "d")
    dm.add_message("assistant", "ИНИЦИАТИВА", "u", "d")  # старая с тем же текстом
    r = gate.commit_message(dm, "d", "ИНИЦИАТИВА")          # буфер полон: 5
    dm.add_message("user", "НОВАЯ РЕПЛИКА", "u", "d")       # вытесняет u0
    gate.rollback_message(dm, "d", r.entry)
    check("откат при полном deque: снесена только своя запись",
          contents(dm, "d") == [("user", "u1"), ("user", "u2"),
                                ("assistant", "ИНИЦИАТИВА"), ("user", "НОВАЯ РЕПЛИКА")])

    # ShortTermMemory.remove_entry (memory.py): идентичность, а не текст
    from app.core.memory import ShortTermMemory
    stm_real = ShortTermMemory.__new__(ShortTermMemory)
    stm_real._lock = threading.RLock()
    rbuf = deque(maxlen=10)
    stm_real._get_buffer = lambda chat_id: rbuf
    deleted_ids = []
    stm_real.collection = SimpleNamespace(
        get=lambda **kw: {"ids": ["a", "b"], "documents": ["шаблон", "шаблон"],
                          "metadatas": [{"timestamp": 1000}, {"timestamp": 9000}]},
        delete=lambda ids: deleted_ids.extend(ids))
    e_old = {"role": "assistant", "content": "шаблон", "timestamp": 1.0}
    e_new = {"role": "assistant", "content": "шаблон", "timestamp": 9.0}
    rbuf.extend([e_old, e_new])
    check("ShortTermMemory.remove_entry: удалена именно эта запись (и её копия в БД)",
          stm_real.remove_entry("x", e_new) and list(rbuf) == [e_old]
          and deleted_ids == ["b"])

    # ── общая обвязка: заглушка BotInstance + ProactiveMessaging ──
    mem = FakeMemory()
    bot = BotInstance.__new__(BotInstance)  # без __init__: только гейт и память
    bot.memory = mem
    bot.persona = SimpleNamespace(settings={})
    bot._pending_split_messages = {}
    bot.chat_user_language = lambda chat_id: None
    gate = bot._get_turn_gate()
    chat = "web_user"
    sender = FakeSender()
    pm = ProactiveMessaging(
        config=ProactiveConfig(enabled=True),
        router=None,
        persona=SimpleNamespace(persona_data={}, system_prompt="Ты — тест.",
                                get_settings=lambda: {}),
        memory=mem, activity_tracker=None,
        get_last_message_time=lambda cid: time.time() - 10 * 3600,
        sender=sender, context="initrace_ctx", turn_gate=gate,
    )
    bot.proactive = pm
    pm._should_send_initiative = lambda cid: True
    pm._select_initiative_type = lambda cid: InitiativeType.CONTINUATION
    pm._get_effective_probability = lambda cid: 1.0
    pm._get_topic_for_chat = lambda cid: None
    mem.stm._buf(chat)  # чат известен циклу (buffers)

    state = {"entered": threading.Event(), "release": threading.Event(), "n": 0}

    def impl(user_input, user_id="default", chat_id=None, **kw):
        """Ход пользователя: реплика в STM → «LLM» (ждёт release) → ответ."""
        state["n"] += 1
        mem.add_message("user", user_input, user_id, chat_id)
        state["entered"].set()
        state["release"].wait(5)
        mem.add_message("assistant", f"ответ на «{user_input}»", user_id, chat_id)
        pm.record_user_response(chat_id)
        return "ok"

    bot._process_message_impl = impl

    def reset_turn(release_now: bool):
        state["entered"] = threading.Event()
        state["release"] = threading.Event()
        if release_now:
            state["release"].set()

    # ── 2. Инициатива посреди хода ──
    print("\n── 2. Инициатива, пока ход пользователя идёт ──")
    reset_turn(release_now=False)
    th = {}

    def gen_during_turn(chat_id, user_id, user_name, initiative_type=None, bypass_silence=False):
        th["t"] = threading.Thread(target=bot.process_message, args=("привет",),
                                   kwargs={"user_id": "u", "chat_id": chat_id})
        th["t"].start()
        state["entered"].wait(5)
        return "Кстати, давно не болтали!"

    pm._generate_initiative = gen_during_turn
    asyncio.run(pm._check_all_chats())
    check("инициатива не отправлена", sender.sent == [])
    check("в STM посреди хода только реплика пользователя",
          contents(mem, chat) == [("user", "привет")])
    state["release"].set()
    th["t"].join(5)
    check("порядок STM: user → ответ (инициативы между ними нет)",
          contents(mem, chat) == [("user", "привет"), ("assistant", "ответ на «привет»")])

    # ── 3. Пользователь написал и получил ответ, пока генерировалась ──
    print("\n── 3. Инициатива, сгенерированная после реплики пользователя ──")
    reset_turn(release_now=True)

    def gen_after_reply(chat_id, user_id, user_name, initiative_type=None, bypass_silence=False):
        bot.process_message("как дела?", user_id="u", chat_id=chat_id)
        return "Слушай, а помнишь…"

    pm._generate_initiative = gen_after_reply
    asyncio.run(pm._check_all_chats())
    check("устаревшая инициатива не отправлена и не записана",
          sender.sent == [] and all(c != "Слушай, а помнишь…" for _, c in contents(mem, chat)))

    # ── 4. Монолог промолчал из-за реплики — рефлексия не генерируется ──
    print("\n── 4. Рефлексия после «пользователь только что написал» ──")
    reset_turn(release_now=True)
    reflected = []

    def gen_none_after_reply(chat_id, user_id, user_name, initiative_type=None, bypass_silence=False):
        bot.process_message("я тут", user_id="u", chat_id=chat_id)
        return None

    pm._generate_initiative = gen_none_after_reply
    pm._generate_reflection_initiative = lambda cid, t: reflected.append(cid) or "мысль вслух"
    asyncio.run(pm._check_all_chats())
    check("рефлексия не вызывалась, ничего не ушло", reflected == [] and sender.sent == [])

    # ── 5. Сигнал состояния — тот же гейт ──
    print("\n── 5. Сигнал состояния ──")
    reset_turn(release_now=True)
    pm._generate_initiative = gen_after_reply
    asyncio.run(pm.state_initiative_signal(chat, 0.9, "тест"))
    check("сигнал состояния после реплики пользователя — отброшен", sender.sent == [])
    reset_turn(release_now=False)
    th2 = threading.Thread(target=bot.process_message, args=("ещё",),
                           kwargs={"user_id": "u", "chat_id": chat})
    th2.start()
    state["entered"].wait(5)
    asyncio.run(pm.state_initiative_signal(chat, 0.9, "тест"))
    check("сигнал состояния посреди хода — молчит", sender.sent == [])
    state["release"].set()
    th2.join(5)

    # ── 6. Тишина: инициатива уходит; ответ ПОСЛЕ неё засчитывается ──
    print("\n── 6. Инициатива в тишине и зачёт ответа ──")
    pm._generate_initiative = lambda *a, **kw: "Как прошёл день?"
    asyncio.run(pm._check_all_chats())
    check("инициатива отправлена", sender.sent == [(chat, "Как прошёл день?")])
    check("инициатива записана в STM последней",
          contents(mem, chat)[-1] == ("assistant", "Как прошёл день?"))
    succ_before = pm._get_feedback(chat)["successes"]
    reset_turn(release_now=True)
    bot.process_message("отлично!", user_id="u", chat_id=chat)
    check("реплика после инициативы засчитана как ответ",
          pm._get_feedback(chat)["successes"] == succ_before + 1
          and pm._get_last_initiative_time(chat) == 0)

    # ── 7. Реплика ДО инициативы — не ответ на неё ──
    print("\n── 7. Ложный зачёт ответа ──")
    succ_before = pm._get_feedback(chat)["successes"]
    pm._mark_initiative_sent(chat, InitiativeType.CONTINUATION, epoch=40)
    pm.record_user_response(chat, turn_epoch=40)
    check("реплика #40 при инициативе #40 — не засчитана, метка на месте",
          pm._get_feedback(chat)["successes"] == succ_before
          and pm._get_last_initiative_time(chat) > 0)
    pm.record_user_response(chat, turn_epoch=41)
    check("реплика #41 — засчитана",
          pm._get_feedback(chat)["successes"] == succ_before + 1)
    # Без явного номера — номер хода текущего потока (process_message)
    succ_before = pm._get_feedback(chat)["successes"]
    with gate.user_turn(chat) as fr:
        pm._mark_initiative_sent(chat, InitiativeType.CONTINUATION, epoch=fr["epoch"])
        pm.record_user_response(chat)
    check("номер хода берётся из потока: реплика до инициативы не засчитана",
          pm._get_feedback(chat)["successes"] == succ_before)

    # ── 8. Доставка не удалась — запись откатывается ──
    print("\n── 8. Откат при неудачной доставке ──")
    before = contents(mem, chat)
    sender.ok = False
    pm._last_initiative_time[chat] = 0
    pm._generate_initiative = lambda *a, **kw: "Эй, ты тут?"
    asyncio.run(pm._check_all_chats())
    sender.ok = True
    check("недоставленная инициатива не осталась в STM", contents(mem, chat) == before)

    # ── 9. Ритм: не посреди хода, без ожидания в цикле ──
    print("\n── 9. Ритм посреди хода ──")
    rsender = FakeSender()
    rm = RhythmManager(context="initrace_rhythm", config=RhythmConfig.from_dict(True),
                       memory=mem, sender=rsender, turn_gate=gate)

    def hold_turn(user_text):
        """Ход пользователя в потоке, остановленный посреди «генерации»."""
        reset_turn(release_now=False)
        t = threading.Thread(target=bot.process_message, args=(user_text,),
                             kwargs={"user_id": "u", "chat_id": chat})
        t.start()
        state["entered"].wait(5)
        return t

    def finish_turn(t):
        state["release"].set()
        t.join(5)

    night_now = datetime(2026, 9, 23, 0, 30)
    night_seen = timeutil.to_ts(night_now) - 60
    t = hold_turn("не сплю")
    t0 = time.monotonic()
    asyncio.run(rm._do_night(chat, night_now, night_seen))
    check("ночь посреди хода: сразу выход (цикл не ждёт), не отправлена, не отмечена",
          time.monotonic() - t0 < 1 and rsender.sent == []
          and contents(mem, chat)[-1] == ("user", "не сплю")
          and rm._chat_state(chat).get("night_key") is None)
    check("_send посреди хода → отложено",
          asyncio.run(rm._send(chat, "текст", "night")) == rhythm_mod._DEFERRED
          and rsender.sent == [])
    finish_turn(t)
    asyncio.run(rm._do_night(chat, night_now, night_seen))
    night_text = rsender.sent[-1][1] if rsender.sent else None
    check("ночь следующим тиком: после ответа, отмечена",
          night_text is not None
          and contents(mem, chat)[-3:] == [("user", "не сплю"),
                                           ("assistant", "ответ на «не сплю»"),
                                           ("assistant", night_text)]
          and rm._chat_state(chat).get("night_key") is not None)

    morning_now = datetime(2026, 9, 23, 8, 0)
    morning_seen = timeutil.to_ts(morning_now) - 10 * 3600
    rsender.sent.clear()
    t = hold_turn("доброе")
    asyncio.run(rm._do_morning(chat, morning_now, morning_seen))
    check("утро посреди хода: отложено (запомнено для повтора), не отправлено",
          rsender.sent == [] and chat in rm._deferred_morning)

    async def retry_morning():
        rm._aio_loop = asyncio.get_running_loop()
        await rm._retry_deferred(morning_now)
        busy_kept = chat in rm._deferred_morning
        finish_turn(t)
        await rm._retry_deferred(morning_now)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if rsender.sent:
                break
        return busy_kept

    busy_kept = asyncio.run(retry_morning())
    morning_text = rsender.sent[-1][1] if rsender.sent else None
    check("утро: пока ход идёт — ждёт в отложенных; после — встало после ответа",
          busy_kept and morning_text is not None
          and contents(mem, chat)[-2:] == [("assistant", "ответ на «доброе»"),
                                           ("assistant", morning_text)]
          and rm._chat_state(chat).get("morning_date") == "2026-09-23")

    # ── 9b. Напоминания: не теряются, отменённое в ходе — не уходит ──
    print("\n── 9b. Напоминания посреди хода ──")
    from app.features import reminder_manager as rem_mod
    from app.features.reminder_manager import ReminderManager
    msender = FakeSender()
    remm = ReminderManager(context="initrace_rem")
    remm.set_sender(msender)
    remm.set_memory(mem)
    remm.set_turn_gate(gate)

    def new_reminder(task):
        remm.add_reminder(chat, "Юзер", task, 0, None, user_id="u")
        return [x for x in remm._reminders if x.get("task") == task][-1]

    rem = new_reminder("полить цветы")
    t = hold_turn("занят")
    t0 = time.monotonic()
    res = asyncio.run(remm._fire(rem))
    check("напоминание посреди хода: отложено сразу, не отправлено, не в STM",
          res == rem_mod._DEFERRED and time.monotonic() - t0 < 1 and msender.sent == []
          and contents(mem, chat)[-1] == ("user", "занят"))
    finish_turn(t)
    res = asyncio.run(remm._fire(rem))
    rem_text = msender.sent[-1][1] if msender.sent else None
    check("напоминание следующим тиком — после ответа",
          res is True and rem_text is not None
          and contents(mem, chat)[-2:] == [("assistant", "ответ на «занят»"),
                                           ("assistant", rem_text)])

    # Отменено в том самом ходе, которого ждали (репро B)
    rem2 = new_reminder("выключить духовку")
    msender.sent.clear()

    def cancel_impl(user_input, user_id="default", chat_id=None, **kw):
        mem.add_message("user", user_input, user_id, chat_id)
        with remm._lock:
            remm._reminders.remove(rem2)          # как cancel_by_ref
        mem.add_message("assistant", "Хорошо, отменила.", user_id, chat_id)
        return "ok"

    bot._process_message_impl = cancel_impl
    bot.process_message("отмени про духовку", user_id="u", chat_id=chat)
    bot._process_message_impl = impl
    res = asyncio.run(remm._fire(rem2))
    check("отменённое в ходе напоминание не уходит и не пишется в STM",
          res == rem_mod._CANCELLED and msender.sent == []
          and all("духовк" not in c for _, c in contents(mem, chat)[-1:]))
    rem3 = new_reminder("позвонить")
    with remm._lock:
        rem3["trigger_at"] = time.time() + 3600   # перенесено «через час»
    check("перенесённое в ходе — тоже не уходит",
          asyncio.run(remm._fire(rem3)) == rem_mod._CANCELLED and msender.sent == [])
    rem4 = new_reminder("проверить почту")
    before = contents(mem, chat)
    msender.ok = False
    r = asyncio.run(remm._fire(rem4))
    msender.ok = True
    check("напоминание: доставка не удалась — False, запись откатана",
          r is False and contents(mem, chat) == before)

    # ── 9c. Уроки: отложено ≠ сбой ──
    print("\n── 9c. Уроки посреди хода ──")
    from app.features import learning_manager as learn_mod
    from app.features.learning_manager import LearningManager
    lsender = FakeSender()
    lsender.docs = []

    async def send_document(chat_id, file_path, filename, *, caption=None, topic_id=None, parse_mode=None):
        lsender.docs.append((chat_id, caption))
        return lsender.ok

    lsender.send_document = send_document
    lm = LearningManager(context="initrace_learn")
    lm.set_sender(lsender)
    lm.set_memory(mem)
    lm.set_turn_gate(gate)
    lm._sessions.append({"chat_id": chat, "session_id": "s1", "active": True,
                         "subject": "падежи", "interval_seconds": 3600,
                         "lesson_count": 0, "next_lesson_at": 0})
    t = hold_turn("учусь")
    r = asyncio.run(lm._send(chat, "Урок №1: падежи", None, session_id="s1"))
    check("урок посреди хода: отложено сразу, не отправлен",
          r == learn_mod._DEFERRED and lsender.sent == [])
    lm._generate_lesson_text = lambda session: {"lesson": "ТЕЛО УРОКА", "topic": "падежи",
                                                "questions": []}
    lm._render_lesson_caption = lambda *a: "ПОДПИСЬ"
    session = lm.get_session(chat, session_id="s1")
    asyncio.run(lm._send_regular_lesson(session))
    s1 = lm.get_session(chat, session_id="s1")
    check("урок-файл посреди хода: без отката на текст, не засчитан, повтор через минуту",
          lsender.sent == [] and lsender.docs == [] and s1.get("lesson_count") == 0
          and s1["next_lesson_at"] - time.time() <= learn_mod._DEFER_RETRY_SECONDS + 1)
    finish_turn(t)
    r = asyncio.run(lm._send(chat, "Урок №1: падежи", None, session_id="s1"))
    check("урок после хода — встал после ответа",
          r is True and contents(mem, chat)[-2:] == [("assistant", "ответ на «учусь»"),
                                                     ("assistant", "Урок №1: падежи")])
    # Файл не ушёл (сбой) — текст одним сообщением: подпись без тела невозможна
    lsender.sent.clear()
    lsender.ok_doc = False

    async def send_document_fail(chat_id, file_path, filename, *, caption=None, topic_id=None, parse_mode=None):
        return False

    lsender.send_document = send_document_fail
    asyncio.run(lm._send_regular_lesson(lm.get_session(chat, session_id="s1")))
    check("сбой файла → текст ОДНИМ сообщением (подпись + тело)",
          len(lsender.sent) == 1 and "ПОДПИСЬ" in lsender.sent[0][1]
          and "ТЕЛО УРОКА" in lsender.sent[0][1])
    # Курс остановлен в ходе — урок не уходит
    lsender.sent.clear()
    bot.process_message("хватит учиться", user_id="u", chat_id=chat)
    lm._set_session(chat, session_id="s1", active=False)
    r = asyncio.run(lm._send(chat, "Урок №2", None, session_id="s1"))
    check("курс остановлен — урок отменён, не отправлен",
          r == learn_mod._CANCELLED and lsender.sent == [])
    before = contents(mem, chat)
    lsender.ok = False
    r = asyncio.run(lm._send(chat, "Урок без сессии", None))
    lsender.ok = True
    check("урок: доставка не удалась — запись откатана", r is False and contents(mem, chat) == before)

    # ── 9d. Ход держится до конца доставки (TG split / SSE «печать») ──
    print("\n── 9d. Ход до конца доставки ответа ──")
    reset_turn(release_now=True)
    rem5 = new_reminder("позвонить маме")
    msender.sent.clear()
    delivered = []

    class LogSender(FakeSender):
        async def send_message(self, chat_id, text, *, topic_id=None, parse_mode=None):
            delivered.append(text)
            return True

    remm.set_sender(LogSender())

    async def tg_handler():
        async with bot.user_turn_async(chat):
            await asyncio.to_thread(bot.process_message, "расскажи длинно",
                                    user_id="u", chat_id=chat)
            for part in ("часть 1", "часть 2", "часть 3"):
                delivered.append(part)
                await asyncio.sleep(0.1)

    async def scenario_d():
        h = asyncio.create_task(tg_handler())
        await asyncio.sleep(0.05)
        mid = await remm._fire(rem5)
        await h
        after = await remm._fire(rem5)
        return mid, after

    mid, after = asyncio.run(scenario_d())
    check("напоминание не встаёт между частями ответа: отложено, после доставки — следом",
          mid == rem_mod._DEFERRED and after is True
          and delivered[:3] == ["часть 1", "часть 2", "часть 3"] and len(delivered) == 4)
    remm.set_sender(msender)

    # ── 9e. Telegram-фото: ход с получения, инициатива не встаёт перед репликой ──
    print("\n── 9e. Фото: инициатива в окне распознавания ──")
    sender.sent.clear()
    pm._last_initiative_time[chat] = 0
    succ0 = pm._get_feedback(chat)["successes"]

    def slow_gen(chat_id, user_id, user_name, initiative_type=None, bypass_silence=False):
        time.sleep(0.3)
        return "Кстати, как прошёл день?"

    pm._generate_initiative = slow_gen

    async def photo_handler():
        async with bot.user_turn_async(chat):
            await asyncio.to_thread(time.sleep, 0.6)        # describe_image
            await asyncio.to_thread(bot.process_message, "[photo] кот",
                                    user_id="u", chat_id=chat)

    async def scenario_a():
        init = asyncio.create_task(pm._check_all_chats())
        await asyncio.sleep(0.05)
        await photo_handler()
        await init

    asyncio.run(scenario_a())
    check("инициатива, сгенерированная пока распознавалось фото, отброшена; ложного зачёта нет",
          sender.sent == [] and pm._get_feedback(chat)["successes"] == succ0
          and all(c != "Кстати, как прошёл день?" for _, c in contents(mem, chat)))

    # ── 10. _pipeline_failure_reply: ответ именно на текущую реплику ──
    print("\n── 10. Реплика-ошибка пайплайна ──")
    fchat = "fail_chat"
    with bot.user_turn(fchat):
        mem.add_message("user", "вопрос", "u", fchat)
        # чужая запись ассистента без гейта (напоминание) — не ответ
        mem.add_message("assistant", "Напоминание: полей цветы", "u", fchat)
        err = bot._pipeline_failure_reply("u", fchat)
    check("ошибка записана, хотя последней лежала чужая запись ассистента",
          contents(mem, fchat)[-1] == ("assistant", err))
    with bot.user_turn(fchat):
        mem.add_message("user", "вопрос 2", "u", fchat)
        bot._save_assistant_reply("настоящий ответ", "u", fchat)
        bot._pipeline_failure_reply("u", fchat)
    check("ответ уже записан — ошибка не дублируется",
          contents(mem, fchat)[-1] == ("assistant", "настоящий ответ"))
    n_before = len(contents(mem, fchat))
    with bot.user_turn(fchat):
        bot._pipeline_failure_reply("u", fchat)  # сбой до записи реплики
    check("реплика в STM не попала — ошибка не пишется",
          len(contents(mem, fchat)) == n_before)
    # answer_saved — только после успешной записи ответа
    real_add = mem.add_message

    def failing_add(role, content, *a, **kw):
        if role == "assistant":
            raise RuntimeError("stm down")
        return real_add(role, content, *a, **kw)

    with bot.user_turn(fchat) as fr:
        mem.add_message("user", "вопрос 3", "u", fchat)
        mem.add_message = failing_add
        try:
            bot._save_assistant_reply("не лёг", "u", fchat)
        except RuntimeError:
            pass
        mem.add_message = real_add
        check("запись ответа упала — отметки answer_saved нет", not fr.get("answer_saved"))
        bot._pipeline_failure_reply("u", fchat)
    check("…и реплика-ошибка записана", contents(mem, fchat)[-2][1] == "вопрос 3"
          and contents(mem, fchat)[-1][0] == "assistant")
    check("ключ хода = ключ STM (пустой chat_id — свой ключ \"\")",
          BotInstance.stm_key("", "u") == "" and BotInstance.stm_key(None, "u") == "u")

    # ── 11. _rewrite_image_stm сервера не сносит инициативу ──
    print("\n── 11. Правка STM картинки ──")
    try:
        from app.api.server import (_adopt_turn, _begin_turn, _rewrite_image_stm,
                                    _stm_key)
    except ImportError as e:
        print(f"  (пропущено — сервер недоступен: {e})")
    else:
        ichat = "img_chat"
        mem.add_message("user", "u0", "u", ichat)
        mem.add_message("assistant", "a0", "u", ichat)
        mem.add_message("assistant", "инициатива", "u", ichat)
        frame = asyncio.run(_begin_turn(bot, ichat))
        try:
            with _adopt_turn(bot, frame):
                # ход записал только ответ (реплика не легла в STM)
                mem.add_message("assistant", "ответ", "u", ichat)
                _rewrite_image_stm(bot, ichat, "u", "📷 фото", ["ответ"])
        finally:
            bot.end_user_turn(frame)
        check("инициатива перед ходом осталась, хвост хода переписан",
              contents(mem, ichat) == [("user", "u0"), ("assistant", "a0"),
                                       ("assistant", "инициатива"),
                                       ("user", "📷 фото"), ("assistant", "ответ")])
        check("ход сервера закрыт после ответа", not gate.busy(ichat))
        check("заготовка бота без гейта: ход None, пустой контекст",
              asyncio.run(_begin_turn(SimpleNamespace(), "x")) is None
              and _adopt_turn(SimpleNamespace(), None).__enter__() is None)
        check("_stm_key: правило MemoryManager",
              _stm_key(SimpleNamespace(chat_id="", user_id="u")) == ""
              and _stm_key(SimpleNamespace(chat_id=None, user_id="u")) == "u")

    print()
    print(f"Проверок: {ok}, провалов: {failures}")
    if failures == 0:
        print("OK")
        return 0
    print("FAILURES")
    return 1


if __name__ == "__main__":
    sys.exit(main())

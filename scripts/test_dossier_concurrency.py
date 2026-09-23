"""Конкурентность ChatDossier: LLM-вызовы анализа идут БЕЗ self._lock.

Раньше analyze_chat держал RLock персоны на весь анализ, включая вызовы
side-LLM (минуты через веб-чат) — ответ пользователю (get_profile_snapshot)
и RhythmManager._note_dossier → record_event (корутина общего фонового loop)
ждали до конца анализа. Проверяет, на фейковом медленном LLM (офлайн):
- во время анализа get_profile_snapshot / get_context_block / record_event
  из другого потока возвращаются быстро;
- результат анализа сливается с актуальным профилем: событие, записанное во
  время анализа, не теряется ни в памяти, ни на диске; счётчик — инкремент;
- второй анализ того же чата во время первого — пропуск (без LLM-вызовов),
  анализ другого чата при этом идёт; флаг снимается и при исключении;
- очистка досье во время анализа (как memory_wipe._wipe_dossier) не
  воскрешается результатом анализа, в том числе когда после очистки
  record_event уже создал новый профиль.

Запуск: python -m scripts.test_dossier_concurrency
"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

FAST = 0.5  # «быстро» — с большим запасом против минут старого поведения


class SlowRouter:
    """Фейковый основной роутер: каждый вызов LLM висит, пока тест не
    отпустит gate (не дольше timeout — тест не зависает при регрессии)."""

    active_provider = "fake:main"

    def __init__(self):
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.calls = 0
        self._calls_lock = threading.Lock()

    def get_response(self, messages, exclude_provider=None,
                     webchat_channel=None, **kw):
        with self._calls_lock:
            self.calls += 1
        self.entered.set()
        self.gate.wait(timeout=10)
        system = messages[0]["content"]
        if "extract facts about the user" in system.lower():
            return "Name: Алексей\nCity: �город"
        return json.dumps({
            "interests": ["астрономия"],
            "topics": ["телескоп своими руками"],
            "personality_notes": ["любопытный"],
            "personal_facts": ["собирает телескоп"],
        }, ensure_ascii=False)


def _messages():
    now = time.time() + 1  # новее водяного знака экстракции фактов
    return [
        {"role": "user", "sender_id": "u1", "timestamp": now,
         "content": "Меня зовут Алексей, я из �города и собираю телескоп"},
        {"role": "assistant", "content": "Круто!"},
        {"role": "user", "sender_id": "u1", "timestamp": now + 1,
         "content": "Вчера шлифовал зеркало для рефлектора, это долго"},
    ]


def _timed(fn, *a, **kw):
    t0 = time.monotonic()
    res = fn(*a, **kw)
    return res, time.monotonic() - t0


def _in_thread(fn, *a, **kw):
    """Вызов из ДРУГОГО потока (как фоновый loop ритма / поток ответа);
    возвращает (результат, длительность) или (None, inf) при зависании."""
    box = {}

    def run():
        box["r"] = _timed(fn, *a, **kw)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5)
    return box.get("r", (None, float("inf")))


def main():
    tmp = tempfile.mkdtemp(prefix="dossier_conc_")
    os.environ["DATA_DIR"] = tmp
    os.chdir(tmp)  # ChatDossier пишет в относительный data/<context>/

    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    from app.features.chat_dossier import ChatDossier

    # ── 1. Лок не держится во время LLM; слияние с актуальным профилем ──
    print("1. Анализ не блокирует чтение/запись досье")
    router = SlowRouter()
    d = ChatDossier(context="conc1", router=router)
    d.record_event("c1", "до анализа")
    t = threading.Thread(target=d.analyze_chat, args=("c1", _messages()),
                         daemon=True)
    t.start()
    check("анализ дошёл до вызова LLM", router.entered.wait(timeout=5))

    snap, dt = _in_thread(d.get_profile_snapshot, "c1", user_id="u1")
    check(f"get_profile_snapshot во время LLM вернулся быстро ({dt:.3f} с)",
          dt < FAST and isinstance(snap, dict))
    _, dt = _in_thread(d.get_context_block, "c1")
    check(f"get_context_block во время LLM вернулся быстро ({dt:.3f} с)", dt < FAST)
    _, dt = _in_thread(d.record_event, "c1", "утреннее приветствие во время анализа")
    check(f"record_event во время LLM вернулся быстро ({dt:.3f} с)", dt < FAST)
    _, dt = _in_thread(d.add_personality_note, "c1", "заметка во время анализа")
    check(f"add_personality_note во время LLM вернулся быстро ({dt:.3f} с)", dt < FAST)
    _, dt = _in_thread(d.record_fact, "c1", "факт во время анализа")
    check(f"record_fact во время LLM вернулся быстро ({dt:.3f} с)", dt < FAST)

    # ── 2. Второй анализ того же чата — пропуск ──
    print("2. Параллельный второй анализ того же чата")
    calls_before = router.calls
    _, dt = _in_thread(d.analyze_chat, "c1", _messages())
    check(f"второй analyze_chat вернулся сразу ({dt:.3f} с)", dt < FAST)
    check("второй analyze_chat не сделал ни одного LLM-вызова",
          router.calls == calls_before)
    check("флаг «анализ идёт» стоит на c1", "c1" in d._analyzing)

    router.gate.set()
    t.join(timeout=10)
    check("первый анализ завершился", not t.is_alive())
    check("флаг «анализ идёт» снят после анализа", "c1" not in d._analyzing)

    prof = d.get_profile("c1")
    events = " | ".join(prof.events)
    check("результат анализа записан (интерес)",
          any(i.value == "астрономия" for i in prof.interests))
    check("событие ДО анализа сохранилось", "до анализа" in events)
    check("событие, записанное ВО ВРЕМЯ анализа, не затёрто результатом",
          "во время анализа" in events)
    check("заметка во время анализа не затёрта",
          "заметка во время анализа" in prof.personality_notes
          and "любопытный" in prof.personality_notes)
    check("факт (facts_shared) во время анализа не затёрт",
          any("факт во время анализа" in f for f in prof.facts_shared))
    uf = prof.user_facts.get("u1")
    check("факты экстракции и анализа слиты в user_facts[u1]",
          uf is not None and "Алексей" in uf.facts
          and "собирает телескоп" in uf.facts)
    check("message_count увеличен на число реплик пользователя",
          prof.message_count == 2)

    # Тот же итог и на диске — свежий инстанс читает файл
    d_disk = ChatDossier(context="conc1")
    prof_disk = d_disk.get_profile("c1")
    check("на диске: и результат анализа, и событие во время анализа",
          prof_disk is not None
          and any(i.value == "астрономия" for i in prof_disk.interests)
          and any("во время анализа" in e for e in prof_disk.events))

    # ── 3. Анализ другого чата не ждёт идущий анализ ──
    print("3. Флаг — на чат, а не на персону")
    router3 = SlowRouter()
    d3 = ChatDossier(context="conc3", router=router3)
    ta = threading.Thread(target=d3.analyze_chat, args=("a", _messages()),
                          daemon=True)
    ta.start()
    router3.entered.wait(timeout=5)
    calls_a = router3.calls
    tb = threading.Thread(target=d3.analyze_chat, args=("b", _messages()),
                          daemon=True)
    tb.start()
    deadline = time.monotonic() + 3
    while router3.calls == calls_a and time.monotonic() < deadline:
        time.sleep(0.01)
    check("анализ чата b дошёл до LLM, пока идёт анализ чата a",
          router3.calls > calls_a)
    router3.gate.set()
    ta.join(timeout=10)
    tb.join(timeout=10)
    check("оба анализа завершились, флаги сняты",
          not ta.is_alive() and not tb.is_alive() and not d3._analyzing)

    # ── 4. Очистка во время анализа не воскрешается ──
    print("4. Очистка досье во время анализа")

    def wipe(dossier, ck):
        # Ровно как app/api/memory_wipe._wipe_dossier
        with dossier._lock:
            dossier._profiles.pop(ck, None)
            dossier._facts_seen.pop(ck, None)
            dossier._facts_watermark.pop(ck, None)
            dossier._save()

    router4 = SlowRouter()
    d4 = ChatDossier(context="conc4", router=router4)
    d4.record_event("w", "старое событие")
    t4 = threading.Thread(target=d4.analyze_chat, args=("w", _messages()),
                          daemon=True)
    t4.start()
    router4.entered.wait(timeout=5)
    _, dt = _in_thread(wipe, d4, "w")
    check(f"очистка во время LLM прошла быстро ({dt:.3f} с)", dt < FAST)
    router4.gate.set()
    t4.join(timeout=10)
    check("после анализа профиль НЕ воскрес в памяти", d4.get_profile("w") is None)
    on_disk = json.loads(Path("data/conc4/chat_dossier.json").read_text("utf-8"))
    check("после анализа профиль НЕ воскрес на диске", "w" not in on_disk)
    check("флаг снят и при отброшенном результате", "w" not in d4._analyzing)

    # Очистка + новое событие (новый профиль) во время анализа: результат
    # старого анализа в новый профиль не подмешивается, событие живо
    router5 = SlowRouter()
    d5 = ChatDossier(context="conc5", router=router5)
    t5 = threading.Thread(target=d5.analyze_chat, args=("w", _messages()),
                          daemon=True)
    t5.start()
    router5.entered.wait(timeout=5)
    wipe(d5, "w")
    d5.record_event("w", "после очистки")
    router5.gate.set()
    t5.join(timeout=10)
    p5 = d5.get_profile("w")
    check("новый профиль после очистки на месте, событие сохранено",
          p5 is not None and any("после очистки" in e for e in p5.events))
    check("в новый профиль не подмешан результат анализа старого",
          p5 is not None and not p5.interests and not p5.user_facts
          and p5.message_count == 0)

    # ── 5. Флаг снимается и при исключении внутри анализа ──
    print("5. Исключение в анализе")
    d6 = ChatDossier(context="conc6", router=SlowRouter())

    def boom(chat_id, messages):
        raise RuntimeError("boom")

    d6._analyze_chat_impl = boom
    try:
        d6.analyze_chat("x", _messages())
        raised = False
    except RuntimeError:
        raised = True
    check("исключение проброшено наружу (ловит вызывающий поток)", raised)
    check("флаг «анализ идёт» снят в finally", "x" not in d6._analyzing)

    print()
    print(f"Проверок: {ok}, провалов: {failures}")
    if failures == 0:
        print("OK")
        return 0
    print("FAILURES")
    return 1


if __name__ == "__main__":
    sys.exit(main())

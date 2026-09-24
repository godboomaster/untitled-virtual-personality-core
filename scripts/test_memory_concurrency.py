"""Тест конкурентности памяти.

Проверяет:
  1. консолидация: флаг занятости снимает поток пула, а не поток, который её
     запустил, — вторая и следующие консолидации запускаются; отказ пула
     принять задачу флаг не оставляет висеть;
  2. clear во время медленной экстракции / консолидации не воскрешает факты
     (эпоха очистки);
  3. консолидация не держит _facts_lock поверх LLM: чтение и save_facts во
     время LLM-вызова не ждут; читатель во время замены видит либо старый,
     либо новый набор целиком (правило Rule не пропадает); факты,
     добавленные за время LLM-вызова, сохраняются; удалённый за это время
     факт не воскресает;
  4. LLM-слияние APPEND-категории в save_facts идёт вне _facts_lock;
  5. id записей: два add в STM одного чата в одну миллисекунду не теряются;
  6. фоновые задачи пользователя идут по очереди: консолидация запускается
     после экстракции того же add_message, и UPDATE из экстракции её не
     отменяет; отмена из-за правки фактов возвращает счётчик к порогу;
     отменённый future снимает флаг;
  7. save_facts: эпоха проверяется до LLM-слияния; неудачное LLM-слияние не
     теряет старое значение; два факта одной категории в одном вызове.

Всё офлайн: фейковый роутер, ChromaDB во временном каталоге.
Запуск: PY -m scripts.test_memory_concurrency
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
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


def _shared_embedder():
    # Одна модель эмбеддингов на весь тест, offline, если она в кэше HF
    from app.core import st_embedder
    if st_embedder._model_cached(st_embedder.ST_MODEL_NAME):
        st_embedder.force_hf_offline()
    embedder = st_embedder.create_st_embedder()
    import app.core.memory as memory_mod
    memory_mod.create_st_embedder = lambda *a, **k: embedder
    return embedder


class GatedRouter:
    """LLM-роутер: ответ по первому подходящему ключу из replies, вызов
    висит на gate, пока тест его не отпустит (медленный веб-чат)."""

    def __init__(self, replies: dict, gate: bool = True):
        self.replies = replies
        self.gate = threading.Event()
        if not gate:
            self.gate.set()
        self.started = threading.Event()
        self.calls = []

    def get_response(self, messages, **kw):
        system = messages[0]["content"]
        self.calls.append(system)
        self.started.set()
        self.gate.wait(10)
        for key, reply in self.replies.items():
            if key in system:
                return reply
        return None

    def get_provider_model_info(self):
        return "fake"


class FakeMainRouter:
    active_provider = "fake"

    def is_local_primary(self):
        return False


def make_ltm(tmp: Path, name: str, router):
    from app.core.config import Config
    from app.core.memory import LongTermMemory
    Config.LTM_MODEL_PROVIDER = None
    ltm = LongTermMemory(db_path=str(tmp / name), context=f"memconc_{name}",
                         main_router=FakeMainRouter())
    ltm.llm_router = router
    return ltm


def drain_pool(*ltms):
    """Дождаться фоновых задач LTM: сначала опустеют очереди пользователей
    (следующая задача ставится в пул из колбэка предыдущей), потом пул
    останавливается с ожиданием; следующий вызов создаст новый."""
    from app.core.memory import LongTermMemory
    deadline = time.monotonic() + 30
    while any(ltm._serial for ltm in ltms) and time.monotonic() < deadline:
        time.sleep(0.02)
    ex = LongTermMemory._executor
    if ex is not None:
        ex.shutdown(wait=True)
        LongTermMemory._executor = None


def timed(fn, *a, **kw):
    t0 = time.monotonic()
    res = fn(*a, **kw)
    return res, time.monotonic() - t0


# ─── 1. Повторная консолидация ───────────────────────────

def test_summary_flag():
    print("\n── консолидация: повторный запуск после первой ──")
    from app.core.memory import LongTermMemory, MemoryManager

    runs = []
    gate = threading.Event()

    def summarize_user(uid):
        runs.append(uid)
        gate.wait(5)
        return 1

    mm = make_mm(summarize_user)
    ltm = mm.ltm

    # Как в боте: add_message вызывают разные долгоживущие потоки (воркеры
    # to_thread, обработчики сообщений). Потоки держим живыми до конца: ident
    # завершившегося потока переиспользуется новым, и флаг, привязанный к
    # потоку-владельцу (RLock), ложно проходил бы проверку
    release = threading.Event()
    workers = []

    def from_request_thread():
        box, done = [], threading.Event()

        def work():
            box.append(mm._run_summarize_async("u1"))
            done.set()
            release.wait(10)

        th = threading.Thread(target=work)
        th.start()
        workers.append(th)
        done.wait(10)
        return box[0]

    gate.set()
    first = from_request_thread()
    drain_pool(ltm)
    second = from_request_thread()
    drain_pool(ltm)
    third = from_request_thread()
    drain_pool(ltm)
    release.set()
    for th in workers:
        th.join()
    check("первая, вторая и третья консолидации (из разных потоков) выполнены",
          first and second and third and runs == ["u1", "u1", "u1"])

    gate.clear()
    started = mm._run_summarize_async("u1")
    busy = mm._run_summarize_async("u1")
    gate.set()
    drain_pool(ltm)
    after = mm._run_summarize_async("u1")
    drain_pool(ltm)
    check("пока консолидация идёт — параллельная пропускается, после — снова можно",
          started and not busy and after)

    def broken_executor():
        raise RuntimeError("cannot schedule new futures after shutdown")

    ltm._get_executor = lambda: types.SimpleNamespace(submit=lambda fn: broken_executor())
    refused = mm._run_summarize_async("u1")
    check("пул не принял задачу — False и флаг снят",
          refused is False and mm._summary_running is False and not ltm._serial)

    from concurrent.futures import Future

    def cancelled_submit(fn):
        f = Future()
        f.cancel()
        return f

    ltm._get_executor = lambda: types.SimpleNamespace(submit=cancelled_submit)
    try:
        mm._run_summarize_async("u1")
        cancel_ok = True
    except Exception:  # noqa: BLE001
        cancel_ok = False
    check("future отменён (shutdown пула) — флаг снят, без CancelledError",
          cancel_ok and mm._summary_running is False and not ltm._serial)
    del ltm._get_executor

    # Консолидация отменена из-за правки фактов — попытка не теряется
    from app.core.memory import SUMMARY_CONFLICT
    from app.core.memory_config import SUMMARY_SETTINGS
    mm = make_mm(lambda uid: SUMMARY_CONFLICT)
    every = SUMMARY_SETTINGS["trigger_every"]
    with mock.patch("app.core.memory.web_presence") as wp:
        wp.is_active.return_value = False
        for _ in range(every):
            mm.add_message("user", "текст", "u1", "u1")
        drain_pool(mm.ltm)
        before_next = mm._user_msg_counters.get("u1")
        mm.add_message("user", "текст", "u1", "u1")
        drain_pool(mm.ltm)
    check(f"конфликт: счётчик возвращён к порогу ({before_next} = {every - 1}), "
          f"повтор на следующем сообщении", before_next == every - 1
          and mm.summary_calls == 2)


def make_mm(summarize_user):
    """MemoryManager без баз: LTM с настоящей очередью пользователя,
    консолидация — переданная функция."""
    from app.core.memory import LongTermMemory, MemoryManager
    from app.core.bounded_cache import BoundedCache
    ltm = LongTermMemory.__new__(LongTermMemory)
    ltm._serial = {}
    ltm._serial_lock = threading.Lock()
    mm = MemoryManager.__new__(MemoryManager)
    mm.summary_calls = 0

    def counted(uid):
        mm.summary_calls += 1
        return summarize_user(uid)

    ltm.summarize_user = counted
    mm.context = "memconc"
    mm.enable_ltm_extraction = True
    mm.stm = types.SimpleNamespace(add_message=lambda *a, **kw: None,
                                   get_last=lambda *a, **kw: [])
    mm.ltm = ltm
    ltm.main_router = None
    mm._counter_lock = threading.Lock()
    mm._summary_running = False
    mm._user_msg_counters = BoundedCache(max_entries=10)
    mm._extract_counters = BoundedCache(max_entries=10)
    return mm


# ─── 2. clear во время фоновых задач ─────────────────────

def test_clear_during_extraction(tmp: Path):
    print("\n── clear во время медленной экстракции ──")
    msg = "Пользователь: я переехал в Томск в прошлом году"

    # Контроль: без очистки экстракция факт сохраняет
    router = GatedRouter({"fact extractor": "City: Tomsk"}, gate=False)
    ltm = make_ltm(tmp, "extract_ctrl", router)
    ltm.extract_facts_async(msg, "u1")
    drain_pool(ltm)
    check("контроль: без очистки экстракция сохранила факт",
          ltm.get_all_facts("u1") == ["City: Tomsk"])

    router = GatedRouter({"fact extractor": "City: Tomsk"})
    ltm = make_ltm(tmp, "extract_clear", router)
    ltm.save_facts("Name: Ivan", "u1")
    ltm.extract_facts_async(msg, "u1")
    router.started.wait(5)
    ltm.clear("u1")
    router.gate.set()
    drain_pool(ltm)
    check("очистка во время экстракции: факты не воскресли",
          ltm.get_all_facts("u1") == [])

    # Экстракция поставлена до очистки, но стартовала после (ждала пул)
    router = GatedRouter({"fact extractor": "City: Tomsk"}, gate=False)
    ltm = make_ltm(tmp, "extract_queued", router)
    with mock.patch.object(type(ltm), "_get_executor") as ge:
        pending = []
        ge.return_value = types.SimpleNamespace(
            submit=lambda fn: pending.append(fn) or mock.MagicMock())
        ltm.extract_facts_async(msg, "u1")
    ltm.clear("u1")
    pending[0]()
    check("экстракция из очереди пула после очистки ничего не пишет",
          ltm.get_all_facts("u1") == [])

    ltm.save_facts("Name: Ivan", "u1")
    check("после очистки обычная запись фактов работает",
          ltm.get_all_facts("u1") == ["Name: Ivan"])


SEED = [
    "Rule: не обращаться на вы",
    "City: Tomsk",
    "Name: Ivan",
    "Food: pizza",
    "Pets: кот Барсик",
    "Profession: инженер",
]
CLEAN = [
    "Rule: не обращаться на вы",
    "City: Tomsk",
    "Name: Ivan",
    "Food: pizza",
    "Pets: кот Барсик",
    "Profession: инженер-программист",
]


def seed(ltm, uid="u1"):
    for f in SEED:
        ltm.save_facts(f, uid)


def run_summary(ltm, uid="u1"):
    box = {}
    th = threading.Thread(target=lambda: box.setdefault("res", ltm.summarize_user(uid)))
    th.start()
    return th, box


def test_clear_during_summary(tmp: Path):
    print("\n── clear во время медленной консолидации ──")
    router = GatedRouter({"consolidate": "\n".join(CLEAN)})
    ltm = make_ltm(tmp, "sum_clear", router)
    seed(ltm)
    th, box = run_summary(ltm)
    router.started.wait(5)
    ltm.clear("u1")
    router.gate.set()
    th.join(10)
    check("очистка во время консолидации: результат отброшен, память пуста",
          box.get("res") == -1 and ltm.get_all_facts("u1") == [])


# ─── 3. Консолидация: лок, чтение, параллельная запись ───

def test_summary_consistency(tmp: Path):
    print("\n── консолидация: чтение и запись во время неё ──")
    router = GatedRouter({"consolidate": "\n".join(CLEAN)})
    ltm = make_ltm(tmp, "sum_read", router)
    seed(ltm)
    ltm.save_facts("Rule: не шутить про работу", "u2")   # чужой пользователь

    th, box = run_summary(ltm)
    router.started.wait(5)

    facts, dt_read = timed(ltm.get_all_facts, "u1")
    check(f"чтение во время LLM-вызова консолидации не ждёт ({dt_read:.2f} с)",
          dt_read < 1.0 and sorted(facts) == sorted(SEED))
    _, dt_save = timed(ltm.save_facts, "Games: шахматы", "u1")
    check(f"save_facts во время LLM-вызова консолидации не ждёт ({dt_save:.2f} с)",
          dt_save < 2.0)
    router.gate.set()
    th.join(10)
    check(f"консолидация прошла ({box.get('res')} фактов)", box.get("res") == len(CLEAN))
    check("факт, добавленный за время консолидации, сохранён",
          sorted(ltm.get_all_facts("u1")) == sorted(CLEAN + ["Games: шахматы"]))
    check("факты другого пользователя консолидация не тронула",
          ltm.get_all_facts("u2") == ["Rule: не шутить про работу"])

    # Читатели крутятся всё время замены набора
    router = GatedRouter({"consolidate": "\n".join(CLEAN)})
    ltm = make_ltm(tmp, "sum_swap", router)
    seed(ltm)
    th, box = run_summary(ltm)
    router.started.wait(5)
    old_set, new_set = sorted(SEED), sorted(CLEAN)
    seen, bad, stop = [], [], threading.Event()

    def reader():
        while not stop.is_set():
            got = sorted(ltm.get_all_facts("u1"))
            rules = ltm.get_facts_by_category("u1", "Rule")
            seen.append(got)
            if got not in (old_set, new_set) or rules != ["Rule: не обращаться на вы"]:
                bad.append((got, rules))

    readers = [threading.Thread(target=reader) for _ in range(2)]
    for r in readers:
        r.start()
    time.sleep(0.2)
    router.gate.set()
    th.join(10)
    time.sleep(0.2)
    stop.set()
    for r in readers:
        r.join()

    check(f"читатель во время замены видел только полный набор "
          f"(чтений {len(seen)}, неполных {len(bad)})",
          box.get("res") == len(CLEAN) and seen and not bad and seen[-1] == new_set)

    # Факт удалили во время LLM-вызова — чистый набор его ещё содержит
    from app.core.memory import SUMMARY_CONFLICT
    router = GatedRouter({"consolidate": "\n".join(CLEAN)})
    ltm = make_ltm(tmp, "sum_forget", router)
    seed(ltm)
    th, box = run_summary(ltm)
    router.started.wait(5)
    forgotten = ltm.forget("Pets: кот Барсик", "u1")
    router.gate.set()
    th.join(10)
    facts = ltm.get_all_facts("u1")
    check("факт забыт во время консолидации — консолидация отменена, факт не воскрес",
          forgotten == "Pets: кот Барсик" and box.get("res") == SUMMARY_CONFLICT
          and "Pets: кот Барсик" not in facts and len(facts) == len(SEED) - 1)

    # Повторная консолидация на том же LTM — работает (лок не залип)
    router2 = GatedRouter({"consolidate": "\n".join(f for f in CLEAN if not f.startswith("Pets"))},
                          gate=False)
    ltm.llm_router = router2
    res = ltm.summarize_user("u1")
    check("следующая консолидация на том же LTM проходит",
          res == len(CLEAN) - 1
          and sorted(ltm.get_all_facts("u1")) == sorted(f for f in CLEAN if not f.startswith("Pets")))


# ─── 4. Слияние APPEND вне лока ──────────────────────────

def test_merge_outside_lock(tmp: Path):
    print("\n── save_facts: LLM-слияние вне _facts_lock ──")
    router = GatedRouter({"merge values": "pizza, pasta, sushi"})
    ltm = make_ltm(tmp, "merge", router)
    ltm.save_facts("Food: pizza, pasta", "u1")
    box = {}
    th = threading.Thread(target=lambda: box.setdefault(
        "res", ltm.save_facts("Food: pizza, sushi", "u1")))
    th.start()
    started = router.started.wait(5)
    facts, dt = timed(ltm.get_all_facts, "u1")
    check(f"чтение во время LLM-слияния не ждёт ({dt:.2f} с)",
          started and dt < 1.0 and facts == ["Food: pizza, pasta"])
    router.gate.set()
    th.join(10)
    check("слияние через LLM записано",
          box.get("res") is True and ltm.get_all_facts("u1") == ["Food: pizza, pasta, sushi"])

    # Старое значение сменилось между планом и записью — сливаем без LLM
    router = GatedRouter({"merge values": "pizza, pasta, sushi"})
    ltm = make_ltm(tmp, "merge_race", router)
    ltm.save_facts("Food: pizza, pasta", "u1")
    th = threading.Thread(target=ltm.save_facts, args=("Food: pizza, sushi", "u1"))
    th.start()
    router.started.wait(5)
    ltm.update_fact("Food: pizza, pasta", "Food: pizza, ramen", "u1")
    router.gate.set()
    th.join(10)
    check("план слияния устарел — объединение без LLM, ничего не потеряно",
          ltm.get_all_facts("u1") == ["Food: pizza, ramen, sushi"])


# ─── 6–7. Очередь пользователя и save_facts ─────────────

SEED_MOSCOW = [f if not f.startswith("City") else "City: Moscow" for f in SEED]


def test_serial_and_save_fixes(tmp: Path):
    print("\n── очередь пользователя: консолидация после экстракции ──")
    router = GatedRouter({"fact extractor": "City: Moscow",
                          "consolidate": "\n".join(SEED_MOSCOW)})
    ltm = make_ltm(tmp, "serial", router)
    seed(ltm)
    mm = make_mm(lambda uid: None)
    mm.ltm = ltm                       # настоящий LTM, его очередь и консолидация
    box, real_summarize = {}, ltm.summarize_user
    ltm.summarize_user = lambda uid: box.setdefault("res", real_summarize(uid))
    # как add_message: сначала экстракция, затем консолидация
    ltm.extract_facts_async("Пользователь: я теперь живу в Москве", "u1")
    queued = mm._run_summarize_async("u1")
    router.started.wait(5)
    time.sleep(0.3)
    only_extract = [c for c in router.calls if "consolidate" in c] == []
    router.gate.set()
    drain_pool(ltm)
    facts = sorted(ltm.get_all_facts("u1"))
    check("консолидация ждёт экстракцию того же пользователя",
          queued and only_extract)
    check(f"консолидация после UPDATE экстракции не отменилась ({box.get('res', 'не звалась')})",
          facts == sorted(SEED_MOSCOW) and "City: Tomsk" not in facts)

    print("\n── save_facts: эпоха, неудачное слияние, одна категория дважды ──")
    router = GatedRouter({}, gate=False)
    ltm = make_ltm(tmp, "save_fixes", router)
    ltm.save_facts("Food: pizza, pasta", "u1")
    epoch = ltm._epoch("u1")
    ltm.clear("u1")
    res = ltm.save_facts("Food: pizza, sushi", "u1", epoch=epoch)
    check("после очистки LLM-слияние не зовётся, факты отброшены",
          res is False and router.calls == [] and ltm.get_all_facts("u1") == [])

    ltm.save_facts("Food: pizza, pasta", "u1")
    ltm.save_facts("Food: pizza, sushi", "u1")      # LLM вернёт None
    check("LLM-слияние не удалось — старое значение не потеряно",
          len(router.calls) == 1
          and ltm.get_all_facts("u1") == ["Food: pizza, pasta, sushi"])

    ltm.save_facts("City: Omsk, City: Tomsk", "u1")
    check("два факта UPDATE-категории в одном вызове — остаётся последний",
          ltm.get_facts_by_category("u1", "City") == ["City: Tomsk"])
    ltm.save_facts("City: Kazan, City: Perm", "u1")
    check("то же при уже сохранённом значении категории",
          ltm.get_facts_by_category("u1", "City") == ["City: Perm"])


# ─── 5. id записей STM/LTM ───────────────────────────────

def test_unique_ids(tmp: Path):
    print("\n── id записей в одну миллисекунду ──")
    from app.core import memory as memory_mod
    from app.core.memory import ShortTermMemory

    got, lock = [], threading.Lock()

    def taker():
        local = [memory_mod._unique_ms() for _ in range(2000)]
        with lock:
            got.extend(local)

    threads = [threading.Thread(target=taker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("_unique_ms: 8000 значений из 4 потоков — все разные",
          len(set(got)) == len(got) == 8000)
    now_ms = int(time.time() * 1000)
    check("_unique_ms не уходит от реального времени дальше чем на число вызовов",
          memory_mod._unique_ms() <= now_ms + 8001)

    stm = ShortTermMemory(max_messages=10, db_path=str(tmp / "stm_ids"),
                          context="memconc_stm")
    frozen = time.time()
    with mock.patch.object(memory_mod.time, "time", return_value=frozen):
        stm.add_message("assistant", "первая часть ответа", "u1", chat_id="c1")
        stm.add_message("assistant", "вторая часть ответа", "u1", chat_id="c1")
    rows = stm.collection.get(where={"chat_id": "c1"}, include=["documents", "metadatas"])
    check(f"два add в STM в одну мс — обе записи в БД ({len(rows['ids'])})",
          len(rows["ids"]) == 2 and len(set(rows["ids"])) == 2)
    reloaded = ShortTermMemory(max_messages=10, db_path=str(tmp / "stm_ids"),
                               context="memconc_stm")
    check("после перезагрузки порядок сообщений сохранён",
          [m["content"] for m in reloaded.get_messages(chat_id="c1")]
          == ["первая часть ответа", "вторая часть ответа"])

    ltm = make_ltm(tmp, "ltm_ids", GatedRouter({}, gate=False))
    with mock.patch.object(memory_mod.time, "time", return_value=frozen):
        ltm.save_facts("Rule: без смайликов", "u1")
        ltm.save_facts("Rule: коротко", "u1")
    check("два факта LTM в одну мс — оба сохранены",
          sorted(ltm.get_all_facts("u1")) == ["Rule: без смайликов", "Rule: коротко"])


def main():
    tmp = Path(tempfile.mkdtemp(prefix="memory_concurrency_"))
    cwd = os.getcwd()
    os.chdir(tmp)                       # data/ (last_message.json) — внутри tmp
    from app.core.config import Config
    Config.DATA_DIR = str(tmp / "data")
    try:
        _shared_embedder()
        test_summary_flag()
        test_clear_during_extraction(tmp)
        test_clear_during_summary(tmp)
        test_summary_consistency(tmp)
        test_merge_outside_lock(tmp)
        test_serial_and_save_fixes(tmp)
        test_unique_ids(tmp)
    finally:
        drain_pool()
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

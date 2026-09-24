"""Тесты общего helper'а персистентности (app.core.atomic_io) и использующих
его менеджеров.

Проверяет: атомарность записи (исключение посреди записи не портит файл,
tmp подчищается), безопасную загрузку битого JSON (warning + .corrupt-копия
+ дефолт вместо тихого {}), конкурентные add/get/clear в todo_manager и
inventory_manager из потоков (без пустых/битых чтений и исключений),
чтение контекста chat_dossier во время фонового анализа (без "dictionary/
list changed size during iteration"), многострочную задачу todo (round-trip)
и обратную совместимость со старым форматом todo.txt.

Все проверки — на временных каталогах, data/ не трогаем.
Запуск: PYTHONPATH=. python3 scripts/test_state_io.py
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _file_lock_worker(path_str, iterations):
    """Воркер для теста межпроцессного atomic_io.file_lock — на верхнем
    уровне модуля (multiprocessing со spawn пиклит target по имени)."""
    from app.core import atomic_io as _aio
    p = Path(path_str)
    for _ in range(iterations):
        with _aio.file_lock(p):
            data = _aio.load_json_safe(p, default={"count": 0})
            data["count"] = data.get("count", 0) + 1
            _aio.atomic_write_json(p, data)


def main():
    import os
    tmp = tempfile.mkdtemp(prefix="state_io_smoke_")
    os.chdir(tmp)

    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    from app.core import atomic_io

    # ── 1. atomic_write_text/json: обычная запись ──
    p = Path(tmp) / "a.json"
    atomic_io.atomic_write_json(p, {"x": 1})
    check("atomic_write_json: файл создан и валиден",
          atomic_io.load_json_safe(p, default=None) == {"x": 1})
    check("atomic_write_json: временных файлов не осталось",
          not any(f.name.startswith(".a.json.") for f in Path(tmp).iterdir()))

    # ── 2. Исключение посреди записи не портит файл ──
    p2 = Path(tmp) / "b.json"
    atomic_io.atomic_write_json(p2, {"good": True})
    _orig_replace = os.replace

    def _boom(*a, **kw):
        raise OSError("диск отвалился ровно в момент переименования")

    os.replace = _boom
    try:
        try:
            atomic_io.atomic_write_json(p2, {"good": False, "would": "corrupt"})
            check("atomic_write_json: пробросило исключение при сбое replace", False)
        except OSError:
            check("atomic_write_json: пробросило исключение при сбое replace", True)
    finally:
        os.replace = _orig_replace
    check("atomic_write_json: старое содержимое не тронуто после сбоя",
          atomic_io.load_json_safe(p2, default=None) == {"good": True})
    check("atomic_write_json: tmp-файл подчищен после сбоя",
          not any(f.name.startswith(".b.json.") for f in Path(tmp).iterdir()))

    # ── 3. Битый файл → .corrupt-копия + warning + дефолт (не тихий {}) ──
    p3 = Path(tmp) / "c.json"
    p3.write_text("{это не json", encoding="utf-8")
    caught = []
    import logging

    class _Catch(logging.Handler):
        def emit(self, record):
            caught.append(record.getMessage())

    handler = _Catch()
    atomic_io.logger.addHandler(handler)
    atomic_io.logger.setLevel(logging.WARNING)
    try:
        result = atomic_io.load_json_safe(p3, default={"fallback": True}, label="Test")
    finally:
        atomic_io.logger.removeHandler(handler)
    check("load_json_safe: битый файл → дефолт, а не пустой {}",
          result == {"fallback": True})
    check("load_json_safe: warning залогирован", any("Test" in m for m in caught))
    corrupt_files = [f for f in Path(tmp).iterdir() if f.name.startswith("c.json.corrupt-")]
    check("load_json_safe: битый файл сохранён рядом как .corrupt-<ts>",
          len(corrupt_files) == 1)
    check("load_json_safe: исходное содержимое битого файла сохранено в копии",
          "это не json" in corrupt_files[0].read_text(encoding="utf-8"))
    check("load_json_safe: файла с исходным именем больше нет (переименован)",
          not p3.exists())

    # ── 4. Файла нет вообще — тихий дефолт, без warning ──
    caught.clear()
    handler2 = _Catch()
    atomic_io.logger.addHandler(handler2)
    try:
        result = atomic_io.load_json_safe(Path(tmp) / "nope.json", default=[])
    finally:
        atomic_io.logger.removeHandler(handler2)
    check("load_json_safe: файла нет → default тихо, без warning",
          result == [] and not caught)

    # ── 5. todo_manager: многострочная задача (round-trip) ──
    from app.features.todo_manager import TodoManager
    todo = TodoManager(context="state_io_test")
    multiline_task = "купить хлеб\nи ещё молока\r\nи сыра"
    todo.add_item("chat_ml", "Аня", multiline_task)
    listing = todo.get_list("chat_ml")
    check("todo: многострочная задача видна целиком в списке",
          listing is not None and "купить хлеб" in listing
          and "и ещё молока" in listing and "и сыра" in listing)
    # Формат файла не ломается лишними "- " строками
    path = todo._todo_path("chat_ml")
    raw_lines = [l for l in path.read_text(encoding="utf-8").splitlines()
                if l.startswith("-")]
    check("todo: многострочная задача — ровно ОДНА строка '- Имя: ...' в файле",
          len(raw_lines) == 1)
    # Новый менеджер (перечитывает файл) видит ту же задачу целиком
    todo2 = TodoManager(context="state_io_test")
    listing2 = todo2.get_list("chat_ml")
    check("todo: многострочная задача переживает перечитывание файла",
          listing2 == listing)

    # ── 6. todo_manager: старый формат (без экранирования) читается ──
    old_path = todo._todo_path("chat_old")
    old_path.write_text(
        "# Список дел чата chat_old\n# Обновлен: 2020-01-01 00:00\n\n"
        "- User: купить молоко\n- Аня: позвонить маме\n",
        encoding="utf-8",
    )
    old_listing = todo.get_list("chat_old")
    check("todo: старый формат файла читается как раньше",
          old_listing is not None and "купить молоко" in old_listing
          and "позвонить маме" in old_listing)

    # ── 7. todo_manager: конкурентные add/clear/get без пустых чтений/исключений ──
    errors = []

    def _todo_worker(i):
        try:
            for j in range(20):
                todo.add_item("chat_concurrent", f"user{i}", f"задача {i}-{j}")
                todo.get_list("chat_concurrent")
                if j % 7 == 0:
                    todo.remove_item("chat_concurrent", 1)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_todo_worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    check("todo: конкурентные add/get/remove — без исключений", not errors)
    final_list = todo.get_list("chat_concurrent")
    check("todo: после конкурентной гонки список читается (не None/не битый)",
          final_list is not None)

    # ── 8. inventory_manager: конкурентные add/remove/get_context_block/
    # get_list_text без RuntimeError ("list changed size during iteration") ──
    from app.features.inventory_manager import InventoryManager
    inv = InventoryManager(context="state_io_test", max_slots=1000)
    inv_errors = []

    def _inv_writer():
        try:
            for i in range(200):
                inv.add_item(f"item{i}")
        except Exception as e:
            inv_errors.append(e)

    def _inv_reader():
        try:
            for _ in range(200):
                inv.get_context_block()
                inv.get_list_text()
        except Exception as e:
            inv_errors.append(e)

    threads = [threading.Thread(target=_inv_writer)] + \
              [threading.Thread(target=_inv_reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    check("inventory: конкурентные add + get_context_block/get_list_text — "
          "без RuntimeError", not inv_errors)

    # ── 9. chat_dossier: чтение контекста во время фонового анализа —
    # без "dictionary/list changed size during iteration" ──
    from app.features.chat_dossier import ChatDossier
    dossier = ChatDossier(context="state_io_test")
    chat_id = "dossier_chat"
    dossier.record_event(chat_id, "старт")

    stop = threading.Event()
    dossier_errors = []

    def _mutator():
        i = 0
        while not stop.is_set():
            try:
                dossier.record_fact(chat_id, f"факт {i}")
                dossier.add_personality_note(chat_id, f"заметка {i}")
                dossier.record_event(chat_id, f"событие {i}")
                i += 1
            except Exception as e:
                dossier_errors.append(e)
                break

    def _reader():
        while not stop.is_set():
            try:
                dossier.get_context_block(chat_id)
                dossier.get_interests_text(chat_id)
                dossier.get_top_interest(chat_id)
                dossier.get_profile_snapshot(chat_id)
            except Exception as e:
                dossier_errors.append(e)
                break

    threads = [threading.Thread(target=_mutator)] + \
              [threading.Thread(target=_reader) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    check("chat_dossier: чтение контекста во время фоновых мутаций — "
          "без исключений", not dossier_errors)

    # ── 10. atomic_io.file_lock: межпроцессный лок ──
    # threading.Lock внутри менеджеров не виден другому процессу — здесь
    # проверяем именно межпроцессную гонку: N процессов инкрементируют один
    # JSON-счётчик под file_lock, итог должен быть точным (без потерянных
    # инкрементов read-modify-write).
    import multiprocessing
    lock_target = Path(tmp) / "counter.json"
    atomic_io.atomic_write_json(lock_target, {"count": 0})
    n_procs, n_iter = 4, 40
    procs = [
        multiprocessing.Process(target=_file_lock_worker, args=(str(lock_target), n_iter))
        for _ in range(n_procs)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    check("file_lock: все процессы завершились (не зависли, exitcode 0)",
          all(p.exitcode == 0 for p in procs))
    final = atomic_io.load_json_safe(lock_target, default={"count": -1})
    check(f"file_lock: счётчик = {n_procs}×{n_iter} без потерянных инкрементов "
          "под межпроцессной гонкой", final.get("count") == n_procs * n_iter)
    check("file_lock: .lock-файл создан рядом с целевым",
          Path(f"{lock_target}.lock").exists())

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Smoke-тест общего календаря (calendar_manager).

Проверяет: добавление записи (валидация даты/времени/типа), выборку
по диапазону дат, правку (в т.ч. done), удаление, персистентность
(перезагрузка из файла) и атомарность сохранения (файл — валидный JSON).

Запуск: python -m scripts.test_calendar
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    import json
    import os
    tmp = tempfile.mkdtemp(prefix="calendar_smoke_")
    os.environ["DATA_DIR"] = tmp
    os.chdir(tmp)

    import importlib
    import app.core.config as config_mod
    importlib.reload(config_mod)

    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    from app.features.calendar_manager import CalendarManager

    cal = CalendarManager()

    # ── 1. Добавление ──
    e1 = cal.add_entry("сдать отчёт", "2026-09-20", "18:30",
                       kind="todo", persona="alex", note="деканату")
    check("add: поля записи", e1["title"] == "сдать отчёт" and e1["date"] == "2026-09-20"
          and e1["time"] == "18:30" and e1["kind"] == "todo" and e1["persona"] == "alex"
          and e1["done"] is False and bool(e1["id"]))
    e2 = cal.add_entry("день рождения", "2026-09-15", kind="event")
    check("add: без времени → time=None", e2["time"] is None)
    cal.add_entry("вне диапазона", "2026-10-01")

    # ── 2. Валидация ──
    for name, kwargs in [
        ("пустой заголовок", dict(title="", date="2026-09-20")),
        ("битая дата", dict(title="x", date="20.09.2026")),
        ("несуществующая дата", dict(title="x", date="2026-02-30")),
        ("битое время", dict(title="x", date="2026-09-20", time_="25:00")),
        ("битый тип", dict(title="x", date="2026-09-20", kind="party")),
    ]:
        try:
            cal.add_entry(kwargs.pop("title"), **kwargs)
            check(f"validate: {name} → ValueError", False)
        except ValueError:
            check(f"validate: {name} → ValueError", True)

    # ── 3. Диапазон и сортировка ──
    items = cal.list_entries("2026-09-01", "2026-09-30")
    check("range: сентябрь → 2 записи, 01.10 отфильтрована", len(items) == 2)
    check("range: сортировка по дате", items[0]["date"] == "2026-09-15")
    check("range: без границ → все 3", len(cal.list_entries()) == 3)

    # ── 4. Правка ──
    upd = cal.update_entry(e1["id"], done=True, time="19:00")
    check("update: done + time", upd["done"] is True and upd["time"] == "19:00")
    check("update: чужой id → None", cal.update_entry("nope", done=True) is None)
    try:
        cal.update_entry(e1["id"], date="бред")
        check("update: битая дата → ValueError", False)
    except ValueError:
        check("update: битая дата → ValueError", True)

    # ── 4b. title="" и time="" (задача №9 аудита: раньше писались как есть —
    # пустой заголовок проходил мимо проверки из add_entry, а time="" хранился
    # буквальной пустой строкой вместо None) ──
    try:
        cal.update_entry(e1["id"], title="")
        check("update: title='' → ValueError (как в add_entry)", False)
    except ValueError:
        check("update: title='' → ValueError (как в add_entry)", True)
    upd = cal.update_entry(e1["id"], time="")
    check("update: time='' нормализуется в None, а не хранится как ''",
          upd is not None and upd["time"] is None)

    # ── 5. Персистентность ──
    raw = json.loads((Path(tmp) / "calendar.json").read_text(encoding="utf-8"))
    check("save: файл — валидный JSON со всеми записями", len(raw) == 3)
    cal2 = CalendarManager()
    check("reload: записи пережили пересоздание менеджера",
          len(cal2.list_entries()) == 3
          and cal2.list_entries()[1]["id"] == e1["id"])

    # ── 6. Удаление ──
    check("remove: по id", cal2.remove_entry(e2["id"]) is True)
    check("remove: повторное → False", cal2.remove_entry(e2["id"]) is False)
    check("remove: осталось 2", len(cal2.list_entries()) == 2)

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

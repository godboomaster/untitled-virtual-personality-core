"""Тест парсера напоминаний: день, часть суток, час, перенос и стабильный id.

Проверяет: день-офсет (завтра/послезавтра/tomorrow), части суток
(утром/днём/вечером/ночью, morning/evening/night), час+родительный падеж
(«в 8 вечера», «12 ночи»), склейку дня с явным временем, извлечение задачи
вокруг вставок времени, отсутствие ложных срабатываний (числа в тексте
задачи, приветствие «good morning»), поддержку старых форм записи времени;
id напоминания — выдача при создании, миграция старых записей без id при
чтении, отмена и перенос по id вместо номера строки (номер указывает не на
то, если список изменился между показом и командой), совместимость
старого API отмены по индексу.

Запуск: python -m scripts.test_reminder_parse
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _expected_delay(day_offset: int, hour: int, minute: int = 0) -> float:
    # Эталонная задержка до (сегодня+offset) hour:minute — как в парсере.
    now = datetime.now()
    if day_offset > 0:
        target = datetime(now.year, now.month, now.day) + timedelta(
            days=day_offset, hours=hour % 24, minutes=minute)
        return (target - now).total_seconds()
    now_total = now.hour * 3600 + now.minute * 60 + now.second
    delay = (hour % 24) * 3600 + minute * 60 - now_total
    if delay <= 0:
        delay += 86400
    return float(delay)


def main():
    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    def check_time(name, parsed, day_offset, hour, minute=0, tol=5):
        # parsed должен быть (task, delay) с delay до указанного момента.
        good = parsed is not None and abs(parsed[1] - _expected_delay(day_offset, hour, minute)) <= tol
        check(name, good)

    from app.features.reminder_manager import parse_reminder, parse_recurring, parse_postpone

    # ── 1. День + часть суток ──
    check_time("'напомни tomorrow morning' → завтра 9:00",
               parse_reminder("напомни tomorrow morning"), 1, 9)
    check_time("'напомни завтра утром' → завтра 9:00",
               parse_reminder("напомни завтра утром"), 1, 9)
    check_time("'напомни в 8' (фолбэк голого часа из bot_instance) → 8:00",
               parse_reminder("напомни в 8"), 0, 8)

    # ── 2. Час + родительный падеж ──
    check_time("'напомни в 8 вечера' → 20:00",
               parse_reminder("напомни в 8 вечера"), 0, 20)
    check_time("'напомни в 8:30 вечера' → 20:30",
               parse_reminder("напомни в 8:30 вечера"), 0, 20, 30)
    check_time("'напомни в 12 ночи' → 00:00",
               parse_reminder("напомни в 12 ночи"), 0, 0)
    check_time("'напомни в 2 дня' → 14:00",
               parse_reminder("напомни в 2 дня"), 0, 14)

    # ── 3. День + явное время, задача не теряется ──
    p = parse_reminder("напомни завтра в 8 купить шоколад")
    check_time("'напомни завтра в 8 …' → завтра 8:00", p, 1, 8)
    check("задача после времени: 'купить шоколад'", p is not None and p[0] == "купить шоколад")
    p = parse_reminder("напомни завтра купить шоколад в 8")
    check_time("'…завтра … в 8' (время в конце) → завтра 8:00", p, 1, 8)
    check("задача между днём и временем сохранилась", p is not None and p[0] == "купить шоколад")
    p = parse_reminder("remind me to buy chocolate tomorrow morning")
    check_time("'remind me to buy chocolate tomorrow morning' → завтра 9:00", p, 1, 9)
    check("задача до дня: 'buy chocolate'", p is not None and p[0] == "buy chocolate")
    p = parse_reminder("remind me tomorrow at 8am")
    check_time("'remind me tomorrow at 8am' → завтра 8:00", p, 1, 8)
    check("'tomorrow' больше не утекает в задачу", p is not None and not p[0])

    # ── 4. Дни без времени ──
    check_time("'напомни послезавтра' → +2 дня 9:00",
               parse_reminder("напомни послезавтра"), 2, 9)
    check_time("'напомни вечером' → сегодня 19:00",
               parse_reminder("напомни вечером"), 0, 19)
    check_time("'напомни завтра вечером' → завтра 19:00",
               parse_reminder("напомни завтра вечером"), 1, 19)

    # ── 5. Без ложных срабатываний ──
    check("'напомни купить 2 шоколадки' → None (число в задаче — не час)",
          parse_reminder("напомни купить 2 шоколадки") is None)
    check("'good morning, remind me to call' → None (приветствие — не время)",
          parse_reminder("good morning, remind me to call") is None)

    # ── 6. Регрессия старых форм ──
    p = parse_reminder("напомни через 2 часа сделать домашку")
    check("'через 2 часа' → 7200 сек", p is not None and abs(p[1] - 7200) < 1)
    check("задача 'сделать домашку'", p is not None and p[0] == "сделать домашку")
    check_time("'remind me at 5 pm' → 17:00", parse_reminder("remind me at 5 pm"), 0, 17)
    check_time("'напомни в 11:30' → 11:30", parse_reminder("напомни в 11:30"), 0, 11, 30)

    # ── 7. Полночь: варианты словоформы («полночь»/«полуночи») распознаются ──
    p = parse_reminder("напомни в полночь позвонить")
    check_time("'напомни в полночь' → 00:00", p, 0, 0)
    check("задача сохранилась: 'позвонить'", p is not None and p[0] == "позвонить")
    check_time("'напомни к полуночи' → 00:00",
               parse_reminder("напомни к полуночи"), 0, 0)
    check_time("'remind me at midnight' → 00:00",
               parse_reminder("remind me at midnight"), 0, 0)

    # ── 8. parse_recurring: полночь и 24:00 нормализуются в час 0, а не отбрасываются ──
    rec = parse_recurring("напоминай каждый день в полночь пить воду")
    check("'каждый день в полночь' → расписание собралось",
          rec is not None and rec[1]["hour"] == 0 and rec[1]["minute"] == 0)
    rec = parse_recurring("напоминай каждый день в 24:00 пить воду")
    check("'каждый день в 24:00' → нормализуется в час 0, а не None",
          rec is not None and rec[1]["hour"] == 0)

    # ── 9. «мне» после «напомни» не остаётся в тексте задачи ──
    p = parse_reminder("напомни мне завтра в 8 купить хлеб")
    check("'напомни МНЕ завтра в 8 …' → задача без «мне»: 'купить хлеб'",
          p is not None and p[0] == "купить хлеб")

    # ── 10. Перенос на <10с от «сейчас»: распознаётся и клэмпится к минимуму 10с ──
    shift = parse_postpone("перенеси напоминание на 5 секунд")
    check("'перенеси … на 5 секунд' — распознано, не unknown",
          shift is not None and not shift.get("unknown"))
    check("'…на 5 секунд' — клэмп к минимуму 10с",
          shift is not None and shift.get("seconds") == 10.0)
    shift = parse_postpone("перенеси напоминание на 3 часа")
    check("'…на 3 часа' — обычный случай не пострадал",
          shift is not None and not shift.get("unknown") and shift.get("seconds") == 3 * 3600)

    # ── 11. Стабильный id напоминания: номер в списке нестабилен между показом и командой ──
    import json
    import os
    import tempfile
    from app.features.reminder_manager import ReminderManager, parse_reminder_ref

    check("parse_reminder_ref: id как в списке", parse_reminder_ref("r1a2b3") == ("id", "r1a2b3"))
    check("parse_reminder_ref: регистр и «#» не важны",
          parse_reminder_ref("#R1A2B3") == ("id", "r1a2b3"))
    check("parse_reminder_ref: число → 0-based индекс", parse_reminder_ref("2") == ("index", 1))
    check("parse_reminder_ref: мусор → None", parse_reminder_ref("напоминание") is None)
    check("parse_reminder_ref: пусто → None", parse_reminder_ref("") is None)

    tmp = tempfile.mkdtemp(prefix="reminder_ids_")
    os.chdir(tmp)  # ReminderManager пишет в относительный data/{context}/

    mgr = ReminderManager(context="rid_test")
    r1 = mgr.add_reminder("chat1", "User", "позвонить маме", 3600)
    r2 = mgr.add_reminder("chat1", "User", "выпить воды", 7200)
    check("add_reminder: id выдан и соответствует формату (r + hex)",
          parse_reminder_ref(r1["id"]) == ("id", r1["id"]))
    check("add_reminder: id у двух напоминаний разные", r1["id"] != r2["id"])
    check("get_active: id виден в списке",
          [r["id"] for r in mgr.get_active("chat1")] == [r1["id"], r2["id"]])

    r3 = mgr.add_reminder("chat1", "User", "забрать посылку", 60)
    # Список показан как 1..3, но между показом и командой первое
    # напоминание срабатывает и выпадает из активных — номер 2 в свежем
    # списке указывает уже на «забрать посылку», не то, что видел человек.
    # По id отменяется ровно то, что человек видел.
    shown = [r["id"] for r in mgr.get_active("chat1")]
    check("список для показа: три напоминания",
          shown == [r1["id"], r2["id"], r3["id"]])
    r1["fired"] = True  # сработало между показом и командой
    stale = mgr.cancel_by_ref("chat1", 1)  # «отмени №2» — старый путь по номеру
    check("по номеру из устаревшего списка отменяется ДРУГОЕ напоминание "
          "(демонстрация корня дефекта)",
          stale is not None and stale["id"] == r3["id"])
    removed = mgr.cancel_by_ref("chat1", r2["id"])
    check("cancel_by_ref(id): отменено ровно то, что видел пользователь",
          removed is not None and removed["id"] == r2["id"]
          and removed["task"] == "выпить воды")
    check("cancel_by_ref(id): активных больше нет (сработавшее не в списке)",
          mgr.get_active("chat1") == [])
    check("cancel_by_ref(id): повторная отмена → None",
          mgr.cancel_by_ref("chat1", r2["id"]) is None)
    r4 = mgr.add_reminder("chat1", "User", "полить цветы", 900)
    check("cancel_by_ref: чужой чат не видит напоминание",
          mgr.cancel_by_ref("chat2", r4["id"]) is None)
    check("cancel_by_ref: несуществующий id → None",
          mgr.cancel_by_ref("chat1", "rffffff") is None)
    check("cancel_reminder(index): старый API (bool) работает",
          mgr.cancel_reminder("chat1", 0) is True
          and mgr.get_active("chat1") == [])

    # Миграция: legacy-файл без id
    legacy = Path(tmp) / "data" / "legacy_ctx" / "reminders"
    legacy.mkdir(parents=True, exist_ok=True)
    old_ts = 1_700_000_000.0
    (legacy / "reminders.json").write_text(json.dumps([
        {"chat_id": "chat1", "user_name": "User", "task": "старое дело",
         "created_at": old_ts,
         "trigger_at": 4_000_000_000.0, "topic_id": None, "fired": False},
        # дубль id (например, после восстановления из бэкапа) тоже разводится
        {"chat_id": "chat1", "user_name": "User", "task": "дубль", "id": "rdead1",
         "created_at": old_ts, "trigger_at": 4_000_000_001.0, "fired": False},
        {"chat_id": "chat1", "user_name": "User", "task": "дубль 2", "id": "rdead1",
         "created_at": old_ts, "trigger_at": 4_000_000_002.0, "fired": False},
    ]), encoding="utf-8")
    legacy_mgr = ReminderManager(context="legacy_ctx")
    ids = [r["id"] for r in legacy_mgr.get_active("chat1")]
    check("миграция при чтении: старой записи без id выдан id",
          len(ids) == 3 and all(parse_reminder_ref(i) for i in ids))
    check("миграция при чтении: дубли id разведены", len(set(ids)) == 3)
    on_disk = json.loads((legacy / "reminders.json").read_text(encoding="utf-8"))
    check("миграция сохранена в файл (не только в памяти)",
          [r.get("id") for r in on_disk] == ids)
    check("миграция: отмена по новому id работает",
          (legacy_mgr.cancel_by_ref("chat1", ids[0]) or {}).get("task") == "старое дело")

    # ── 12. Перенос напоминания по id при выборе «какое именно перенести?» ──
    # pending-выбор целиком живёт в reminder_manager — begin_pending_postpone_choice
    # хранит id кандидатов в ПОКАЗАННОМ пользователю порядке и сдвиг;
    # resolve_postpone_choice разбирает ответ (номер/id/порядковое слово/
    # слова задачи) и переносит через postpone_by_id — не по индексу,
    # пересчитанному заново на момент ответа. bot_instance — тонкий вызов
    # reminder_manager, своей копии id не держит.
    import time as _time

    tmp2 = tempfile.mkdtemp(prefix="postpone_choice_")
    os.chdir(tmp2)
    pc_mgr = ReminderManager(context="postpone_choice_test")

    q1 = pc_mgr.add_reminder("chatA", "User", "позвонить маме", 3600)
    q2 = pc_mgr.add_reminder("chatA", "User", "выпить воды", 7200)
    q3 = pc_mgr.add_reminder("chatA", "User", "полить цветы", 10800)
    check("подготовка: три активных напоминания", len(pc_mgr.get_active("chatA")) == 3)

    # Бот показал список 1) позвонить маме 2) выпить воды 3) полить цветы —
    # id кандидатов запоминаем в этом порядке
    pc_mgr.begin_pending_postpone_choice(
        "chatA", ids=[q1["id"], q2["id"], q3["id"]], seconds=3600.0)
    # Между вопросом и ответом первое сработало и выпало из активных —
    # свежий список теперь 1) выпить воды 2) полить цветы, «2» по нему —
    # это «полить цветы», а не то, что человек видел под №2
    q1["fired"] = True
    result = pc_mgr.resolve_postpone_choice("chatA", "2")
    check("перенесено ровно то, что было показано под №2 («выпить воды»), "
          "а не то, что стало №2 в пересчитанном списке",
          result is not None and result.get("task") == "выпить воды")
    check("напоминание реально сдвинуто на час вперёд",
          result is not None and abs(result["trigger_at"] - (_time.time() + 3600)) < 5)
    check("pending выбора снят после переноса",
          not pc_mgr.get_pending_postpone_choice("chatA"))

    # Ответ самим id (не номером) — тоже распознаётся
    q4 = pc_mgr.add_reminder("chatB", "User", "сходить в магазин", 900)
    q5 = pc_mgr.add_reminder("chatB", "User", "полить цветы 2", 1800)
    pc_mgr.begin_pending_postpone_choice(
        "chatB", ids=[q4["id"], q5["id"]], seconds=600.0)
    result_id = pc_mgr.resolve_postpone_choice("chatB", q5["id"])
    check("ответ id напрямую (без номера) переносит нужное напоминание",
          result_id is not None and result_id.get("task") == "полить цветы 2")

    # Порядковое слово («последнее») — тоже по сохранённому, а не по свежему списку
    q6 = pc_mgr.add_reminder("chatC", "User", "первое дело", 500)
    q7 = pc_mgr.add_reminder("chatC", "User", "второе дело", 900)
    pc_mgr.begin_pending_postpone_choice(
        "chatC", ids=[q6["id"], q7["id"]], seconds=120.0)
    q6["fired"] = True  # список изменился — «последнее» из показанного списка всё равно q7
    result_ord = pc_mgr.resolve_postpone_choice("chatC", "последнее")
    check("порядковое «последнее» — по сохранённому списку",
          result_ord is not None and result_ord.get("task") == "второе дело")

    # Активных вообще не осталось — {"gone": True}, pending снят
    pc_mgr.begin_pending_postpone_choice("chatD", ids=[], seconds=60.0)
    gone = pc_mgr.resolve_postpone_choice("chatD", "1")
    check("активных нет вовсе — {'gone': True}, а не молчаливый провал",
          gone == {"gone": True})
    check("«gone»: pending тоже снят", not pc_mgr.get_pending_postpone_choice("chatD"))

    # Выбранное конкретно сработало между вопросом и ответом, но ДРУГИЕ активные
    # есть — переспрашиваем (None), а не молча двигаем что-то другое под тем же номером
    q8 = pc_mgr.add_reminder("chatE", "User", "задача E1", 300)
    q9 = pc_mgr.add_reminder("chatE", "User", "задача E2", 600)
    pc_mgr.begin_pending_postpone_choice(
        "chatE", ids=[q8["id"], q9["id"]], seconds=60.0)
    q8["fired"] = True
    result_missing = pc_mgr.resolve_postpone_choice("chatE", "1")
    check("выбранное №1 сработало между вопросом и ответом — переспрашиваем "
          "(None), не двигаем другое напоминание вместо него",
          result_missing is None)

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

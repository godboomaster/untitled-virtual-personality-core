"""Команды телефону из чата: «поставь будильник на 8», «таймер на 10 минут»,
«покажи будильники», «открой Telegram», «включи фонарик».

Исполняет их приложение на телефоне (web/src/phone/: parse.ts — разбор,
actions.ts — исполнение через плагин PhoneActions), ядру уходит только итог
(ChatRequest.phone_action), из него — заметка персоне. Проверяет:

  A. заметки ядра (app/features/phone_actions.py) и телефона
     (web/src/phone/notes.ts) совпадают на одних отчётах;
  B. разбор фраз (RU/EN): что команда, что разговор («ты поставила
     будильник?», «будильник не звонит»), «на компьютере» — не телефону;
  C. время, длительность, дни повтора, дата будильника, ответы на переспрос;
  D. подбор приложения по названию («ютуб» — YouTube, «камеру» — «Камера»,
     «браузер» — спросить, «включи музыку» — не приложение);
  E. actions.ts с поддельным плагином: что зовётся, итоги, переспросы
     (время, длительность, выбор приложения), будильник на дату, сбои;
  F. ядро: схема отчёта, _phone_note (не из скина, не с картинкой), оба
     эндпоинта чата передают заметку, process_message доводит её до хода,
     заметка — в системный блок промпта и без веб-поиска.

TS гоняется под Node (снятие типов, Node ≥ 23.6; хуки загрузчика подменяют
@capacitor/core и ../api). LLM, Ollama и сеть не вызываются.
Запуск: PYTHONPATH=. python3 -m scripts.test_phone_actions
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="phone_actions_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

PHONE = ROOT / "web" / "src" / "phone"

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


def node(code: str, payload) -> dict | None:
    """Скрипт под Node: payload — в globalThis.INPUT, ответ — последняя строка stdout (JSON)."""
    script = f"globalThis.INPUT = {json.dumps(payload, ensure_ascii=False)};\n{code}"
    res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True,
                         timeout=60, env={**os.environ, "TZ": "Europe/Moscow"})
    if res.returncode != 0:
        check(f"Node: {res.stderr.strip()[:600]}", False)
        return None
    return json.loads(res.stdout.strip().splitlines()[-1])


# ─── A. Заметки ───

REPORTS = [
    {"kind": "alarm", "status": "ok", "hour": 8, "minute": 0},
    {"kind": "alarm", "status": "ok", "hour": 7, "minute": 5, "days": [2, 3, 4, 5, 6]},
    {"kind": "alarm", "status": "ok", "hour": 9, "minute": 30, "days": [1, 7], "label": 'спорт "зал"'},
    {"kind": "alarm", "status": "ok", "hour": 6, "minute": 45, "days": [4, 2]},
    {"kind": "alarm", "status": "ok", "hour": 6, "minute": 45, "days": [1, 2, 3, 4, 5, 6, 7]},
    {"kind": "alarm", "status": "ask"},
    {"kind": "alarm", "status": "no_handler", "hour": 8, "minute": 0},
    {"kind": "alarm", "status": "unsupported_date", "hour": 7, "minute": 0, "nearest": "today"},
    {"kind": "alarm", "status": "unsupported_date", "hour": 7, "minute": 0, "nearest": "tomorrow"},
    {"kind": "alarm", "status": "failed", "hour": 23, "minute": 59},
    {"kind": "timer", "status": "ok", "seconds": 600},
    {"kind": "timer", "status": "ok", "seconds": 5430, "label": "пицца"},
    {"kind": "timer", "status": "ok", "seconds": 1},
    {"kind": "timer", "status": "ask"},
    {"kind": "timer", "status": "no_handler", "seconds": 3600},
    {"kind": "timer", "status": "failed", "seconds": 61},
    {"kind": "alarms_list", "status": "ok"},
    {"kind": "alarms_list", "status": "no_handler"},
    {"kind": "alarm_off", "status": "ok"},
    {"kind": "alarm_off", "status": "failed"},
    {"kind": "open_app", "status": "ok", "app": "Telegram"},
    {"kind": "open_app", "status": "not_found", "app": "тикток"},
    {"kind": "open_app", "status": "ambiguous", "app": "браузер", "candidates": ["Chrome", "Firefox"]},
    {"kind": "open_app", "status": "failed", "app": "VK"},
    {"kind": "torch", "status": "ok", "on": True},
    {"kind": "torch", "status": "ok", "on": False},
    {"kind": "torch", "status": "no_handler", "on": True},
    {"kind": "torch", "status": "busy", "on": True},
    {"kind": "torch", "status": "failed", "on": False},
]

NOTES_JS = """
const { actionNote } = await import(%s);
console.log(JSON.stringify(INPUT.map((r) => actionNote(r))));
"""


def test_notes():
    section("A. Заметки персоне: ядро ↔ телефон")
    from app.features.phone_actions import action_note
    got = node(NOTES_JS % json.dumps((PHONE / "notes.ts").as_uri()), REPORTS)
    if got is None:
        return
    for r, ts in zip(REPORTS, got):
        py = action_note(r)
        check(f"{r['kind']}/{r['status']}" + ("" if py == ts else f"\n      ядро:    {py}\n      телефон: {ts}"), py == ts)
    notes = [action_note(r) for r in REPORTS]
    check("ни одна заметка не зовёт «напомнить» (иначе сработал бы путь напоминаний)",
          not any("remind" in n.lower() or "напом" in n.lower() for n in notes))
    check("отказы честные: «NO alarm» / «nothing» и запрет говорить «сделано»",
          all(("NO " in n or "nothing" in n) for r, n in zip(REPORTS, notes)
              if r["status"] not in ("ok", "ask", "ambiguous")))
    check("кавычки подписи не ломают заметку", "'зал'" in action_note(REPORTS[2]))
    check("дни — с понедельника, будни/выходные/каждый день словами",
          "every Monday, Wednesday" in action_note(REPORTS[3]) and "on weekdays" in action_note(REPORTS[1])
          and "on weekends" in action_note(REPORTS[2]) and "every day" in action_note(REPORTS[4]))
    check("длительность словами", "1 hour 30 minutes 30 seconds" in action_note(REPORTS[11])
          and "1 second" in action_note(REPORTS[12]))
    check("варианты приложения — с номерами", "1. Chrome; 2. Firefox" in action_note(REPORTS[22]))


# ─── B. Разбор фраз ───

def A(h, m, **kw):
    return {"kind": "alarm", "time": {"hour": h, "minute": m}, "relative": 0, **kw}


PHRASES = [
    # будильник
    ("поставь будильник на 8:00", A(8, 0, days=[], day=None, label="")),
    ("Коннор, поставь будильник на 7 утра", A(7, 0)),
    ("будильник на половину восьмого", A(7, 30)),
    ("будильник на полвосьмого", A(7, 30)),
    ("разбуди меня в 6:30", A(6, 30)),
    ("разбуди меня без четверти семь", A(6, 45)),
    ("разбуди меня без пяти 7", A(6, 55)),
    ("поставь будильник на четверть девятого", A(8, 15)),
    ("поставь будильник на семь тридцать", A(7, 30)),
    ("разбуди в шесть сорок пять", A(6, 45)),
    ("поставь будильник на двадцать три", A(23, 0)),
    ("поставь будильник на 8 часов", A(8, 0)),
    ("будильник на 7 часов 30 минут", A(7, 30)),
    ("разбуди меня в 6 30", A(6, 30)),
    ("разбуди меня в 8 вечера", A(20, 0)),
    ("поставь будильник на полдень", A(12, 0)),
    ("заведи будильник на 7 по будням", A(7, 0, days=[2, 3, 4, 5, 6])),
    ("поставь будильник на 9 по выходным с подписью «спортзал»", A(9, 0, days=[1, 7], label="спортзал")),
    ("поставь будильник на 7 каждый понедельник и среду", A(7, 0, days=[2, 4])),
    ("будильник на 6:15 по понедельникам и пятницам", A(6, 15, days=[2, 6])),
    ("поставь будильник на 7 каждый день", A(7, 0, days=[1, 2, 3, 4, 5, 6, 7])),
    ("будильник на 6 завтра", A(6, 0, day={"offset": 1})),
    ("поставь будильник на 7 в пятницу", A(7, 0, day={"weekday": 6})),
    ("поставь будильник на 8 с подписью работа", A(8, 0, label="работа")),
    ("можешь поставить будильник на 10:15?", A(10, 15)),
    ("не могла бы ты поставить будильник на 7", A(7, 0)),
    ("поставь, пожалуйста, будильник на 7", A(7, 0)),
    ("ну поставь будильник на 7", A(7, 0)),
    ("поставь будильник на 7 на телефоне", A(7, 0)),
    ("поставь будильник через 20 минут", {"kind": "alarm", "time": None, "relative": 1200}),
    ("поставь будильник на 20 минут", {"kind": "alarm", "time": None, "relative": 1200}),
    ("разбуди меня через полтора часа", {"kind": "alarm", "time": None, "relative": 5400}),
    ("поставь будильник", {"kind": "alarm", "time": None, "relative": 0}),
    ("разбуди меня завтра", {"kind": "alarm", "time": None, "relative": 0, "day": {"offset": 1}}),
    ("set an alarm for 7am", A(7, 0)),
    ("set an alarm for 7:30 pm", A(19, 30)),
    ("wake me up at half past six", A(6, 30)),
    ("wake me up at quarter to eight", A(7, 45)),
    ("set an alarm for 6:45 on weekdays", A(6, 45, days=[2, 3, 4, 5, 6])),
    ("set an alarm for 8 every monday and friday", A(8, 0, days=[2, 6])),
    ("wake me up at seven", A(7, 0)),
    ("wake me up in 30 minutes", {"kind": "alarm", "time": None, "relative": 1800}),
    ("alarm for 9", A(9, 0)),
    ("can you set an alarm for 6 tomorrow", A(6, 0, day={"offset": 1})),
    ("set alarm", {"kind": "alarm", "time": None, "relative": 0}),
    # таймер
    ("поставь таймер на 10 минут", {"kind": "timer", "seconds": 600}),
    ("таймер на полтора часа", {"kind": "timer", "seconds": 5400}),
    ("таймер на полчаса", {"kind": "timer", "seconds": 1800}),
    ("засеки 5 минут", {"kind": "timer", "seconds": 300}),
    ("таймер на 1 час 30 минут", {"kind": "timer", "seconds": 5400}),
    ("заведи таймер на двадцать пять минут", {"kind": "timer", "seconds": 1500}),
    ("таймер на 2 минуты 30 секунд", {"kind": "timer", "seconds": 150}),
    ("таймер на 5", {"kind": "timer", "seconds": 300}),
    ("поставь таймер на 10 минут с подписью пицца", {"kind": "timer", "seconds": 600, "label": "пицца"}),
    ("поставь таймер", {"kind": "timer", "seconds": 0}),
    ("set a timer for 10 minutes", {"kind": "timer", "seconds": 600}),
    ("start a 5 minute timer", {"kind": "timer", "seconds": 300}),
    ("set a timer for half an hour", {"kind": "timer", "seconds": 1800}),
    ("timer for 90 seconds", {"kind": "timer", "seconds": 90}),
    # список и отключение
    ("покажи будильники", {"kind": "alarms_list"}),
    ("какие у меня будильники", {"kind": "alarms_list"}),
    ("открой будильник", {"kind": "alarms_list"}),
    ("show my alarms", {"kind": "alarms_list"}),
    ("выключи будильник", {"kind": "alarm_off"}),
    ("удали все будильники", {"kind": "alarm_off"}),
    ("отмени будильник на 7", {"kind": "alarm_off"}),
    ("cancel my alarm", {"kind": "alarm_off"}),
    ("turn off all alarms", {"kind": "alarm_off"}),
    # фонарик
    ("включи фонарик", {"kind": "torch", "on": True}),
    ("выключи фонарик", {"kind": "torch", "on": False}),
    ("погаси фонарик", {"kind": "torch", "on": False}),
    ("turn on the flashlight", {"kind": "torch", "on": True}),
    ("turn the torch off", {"kind": "torch", "on": False}),
    ("flashlight off", {"kind": "torch", "on": False}),
    # приложения
    ("открой телеграм", {"kind": "open_app", "name": "телеграм", "explicit": True, "weak": False}),
    ("запусти ютуб", {"kind": "open_app", "name": "ютуб", "explicit": True}),
    ("открой мне камеру", {"kind": "open_app", "name": "камеру", "explicit": True}),
    ("открой приложение сбер", {"kind": "open_app", "name": "сбер", "explicit": True}),
    ("открой сбер", {"kind": "open_app", "name": "сбер", "explicit": False}),
    ("открой ютуб на телефоне", {"kind": "open_app", "name": "ютуб"}),
    ("open spotify", {"kind": "open_app", "name": "spotify", "explicit": True}),
    ("launch the camera app", {"kind": "open_app", "name": "camera", "explicit": True}),
    ("включи ютуб", {"kind": "open_app", "name": "ютуб", "weak": True}),
    ("включи музыку", {"kind": "open_app", "name": "музыку", "weak": True}),
    ("открой «Яндекс Музыку»", {"kind": "open_app", "name": "яндекс музыку"}),
    # разговор, не команда
    ("ты поставила будильник?", None),
    ("как поставить будильник?", None),
    ("будильник не звонит", None),
    ("таймер сломался", None),
    ("не ставь будильник на 7", None),
    ("не надо открывать телеграм", None),
    ("поставь будильник на 8 на компьютере", None),
    ("открой телеграм на ноутбуке", None),
    ("open telegram on my laptop", None),
    ("did you set an alarm?", None),
    ("don't set an alarm", None),
    ("открой файл отчёт.pdf", None),
    ("открой отчёт.pdf", None),
    ("open report.docx", None),
    ("открой сайт вк", None),
    ("открой ссылку", None),
    ("открой мне тайну", None),
    ("открой окно", None),
    ("включи режим управления", None),
    ("перейди в режим управления", None),
    ("open google.com", None),
    ("напомни через 5 минут", None),
    ("напомни завтра в 8 про будильник", None),
    ("Привет, как дела?", None),
    ("у меня будильник на 7", None),
    ("фонарик", None),
]

PARSE_JS = """
const p = await import(%s);
console.log(JSON.stringify(INPUT.map((t) => p.parsePhoneCommand(t, ['Коннор']))));
"""


def _subset(want: dict, have) -> bool:
    if not isinstance(have, dict):
        return False
    return all(have.get(k) == v for k, v in want.items())


def test_parse():
    section("B. Разбор фраз")
    got = node(PARSE_JS % json.dumps((PHONE / "parse.ts").as_uri()), [p for p, _ in PHRASES])
    if got is None:
        return
    for (phrase, want), have in zip(PHRASES, got):
        same = have is None if want is None else _subset(want, have)
        check(f"{phrase!r} → {want}" + ("" if same else f"  (разбор: {have})"), same)


# ─── C. Время, длительность, дни, ответы ───

HELPERS_JS = """
const p = await import(%s);
const now = new Date(...INPUT.now);
const out = {
  clock: INPUT.clock.map((t) => p.parseClock(t, true)),
  dur: INPUT.dur.map((t) => p.parseDuration(t)),
  fits: INPUT.fits.map(([h, m, day, n]) => p.alarmDayFits({ hour: h, minute: m }, day, n ? new Date(...n) : now)),
  alarmAns: INPUT.alarmAns.map((t) => p.parseAlarmAnswer(t)),
  timerAns: INPUT.timerAns.map((t) => p.parseTimerAnswer(t)),
  choice: INPUT.choice.map(([t, n]) => p.parseChoice(t, n)),
  decline: INPUT.decline.map((t) => p.isDecline(t)),
};
console.log(JSON.stringify(out));
"""

# Воскресенье, 11.10.2026, 18:40
NOW = [2026, 9, 11, 18, 40, 0]


def test_helpers():
    section("C. Время, длительность, дата будильника, ответы на переспрос")
    data = {
        "now": NOW,
        "clock": ["7", "семь", "7 утра", "в 9 вечера", "на 7:45", "8.30", "полдень", "полночь", "без десяти восемь",
                  "полпервого ночи", "в час дня", "at 6 pm", "seven thirty", "half past ten", "10 o'clock", "бла-бла", "25"],
        "dur": ["10 минут", "1 час 30 минут", "полчаса", "полтора часа", "час с половиной", "минуту", "на час",
                "90 сек", "1ч30м", "двадцать пять минут", "an hour", "half an hour", "2 hours 15 minutes",
                "с утра", "просто так"],
        # (час, минута, дата, «сейчас» иначе NOW)
        "fits": [
            [20, 0, None, None], [7, 0, {"offset": 1}, None], [20, 0, {"offset": 0}, None],
            [7, 0, {"offset": 0}, None], [20, 0, {"offset": 1}, None], [7, 0, {"offset": 2}, None],
            [7, 0, {"weekday": 2}, None], [7, 0, {"weekday": 6}, None], [7, 0, {"offset": 1}, [2026, 9, 12, 1, 30, 0]],
        ],
        "alarmAns": ["7:30", "на 8", "в семь утра", "через 20 минут", "7", "завтра", "не знаю"],
        "timerAns": ["10 минут", "на 5 минут", "5", "полчаса", "двадцать", "не знаю", "0"],
        "choice": [["2", 3], ["второй", 3], ["второе", 2], ["the second one", 2], ["first", 2], ["5", 3],
                   ["№1", 2], ["Chrome", 2]],
        "decline": ["не надо", "нет", "отмена", "no thanks", "never mind", "нет, спасибо", "7 утра", "давай"],
    }
    got = node(HELPERS_JS % json.dumps((PHONE / "parse.ts").as_uri()), data)
    if got is None:
        return
    hm = lambda r: r and (r["hour"], r["minute"])  # noqa: E731
    want_clock = [(7, 0), (7, 0), (7, 0), (21, 0), (7, 45), (8, 30), (12, 0), (0, 0), (7, 50), (0, 30), (13, 0),
                  (18, 0), (7, 30), (10, 30), (10, 0), None, None]
    for t, have, want in zip(data["clock"], got["clock"], want_clock):
        check(f"время {t!r} → {want}", hm(have) == want if want else have is None)
    want_dur = [600, 5400, 1800, 5400, 5400, 60, 3600, 90, 5400, 1500, 3600, 1800, 8100, 0, 0]
    for t, have, want in zip(data["dur"], got["dur"], want_dur):
        check(f"длительность {t!r} → {want}", have == want)
    want_fits = [
        (True, None, "20:00 без даты — ставится"),
        (True, None, "7:00 «завтра» вечером — это и есть ближайшее"),
        (True, None, "20:00 «сегодня» в 18:40 — ближайшее"),
        (False, "tomorrow", "7:00 «сегодня» в 18:40 — прошло, часы поставят на завтра → отказ"),
        (False, "today", "20:00 «завтра» — часы зазвонят сегодня → отказ"),
        (False, "tomorrow", "«послезавтра» — дату часы не берут → отказ"),
        (True, None, "«в понедельник» в воскресенье вечером — ближайшее 7:00 и есть понедельник"),
        (False, "tomorrow", "«в пятницу» — не ближайшее → отказ"),
        (True, None, "ночью (1:30) «завтра в 7» — наступающее утро"),
    ]
    for have, (fits, nearest, name) in zip(got["fits"], want_fits):
        check(name, have.get("fits") is fits and (fits or have.get("nearest") == nearest))
    want_alarm = [(7, 30, 0), (8, 0, 0), (7, 0, 0), (None, None, 1200), (7, 0, 0), None, None]
    for t, have, want in zip(data["alarmAns"], got["alarmAns"], want_alarm):
        if want is None:
            check(f"ответ про будильник {t!r} — не время", have is None)
        else:
            h, m, rel = want
            same = have is not None and have["relative"] == rel and (
                have["time"] is None if h is None else (have["time"]["hour"], have["time"]["minute"]) == (h, m))
            check(f"ответ про будильник {t!r} → {want}", same)
    for t, have, want in zip(data["timerAns"], got["timerAns"], [600, 300, 300, 1800, 1200, 0, 0]):
        check(f"ответ про таймер {t!r} → {want}", have == want)
    for (t, n), have, want in zip(data["choice"], got["choice"], [1, 1, 1, 1, 0, None, 0, None]):
        check(f"выбор {t!r} из {n} → {want}", have == want)
    for t, have, want in zip(data["decline"], got["decline"], [True, True, True, True, True, True, False, False]):
        check(f"отказ {t!r}: {want}", have is want)


# ─── D. Подбор приложения ───

APPS = ["Telegram", "YouTube", "YouTube Music", "Камера", "Галерея", "Chrome", "Firefox", "СберБанк", "Яндекс",
        "Яндекс Музыка", "Яндекс Карты", "Карты", "Google", "Google Play Маркет", "Часы", "Музыка", "Spotify",
        "VK", "WhatsApp", "Настройки", "Калькулятор", "Календарь", "Gmail", "Погода", "Telegram"]

MATCH_JS = """
const p = await import(%s);
const apps = INPUT.apps.map((label, i) => ({ label, package: 'pkg' + i }));
// Две копии Telegram — клон приложения: разные пакеты
console.log(JSON.stringify(INPUT.cases.map(([name, weak, list]) => {
  // Ограниченный список — без клона Telegram
  const only = list && apps.filter((a, i) => list.includes(a.label) && apps.findIndex((b) => b.label === a.label) === i);
  const r = p.matchApp(name, only || apps, weak);
  return { status: r.status, label: r.app?.label ?? null, labels: r.apps?.map((a) => a.label) ?? null };
})));
"""


def test_match():
    section("D. Подбор приложения по названию")
    single = [a for a in APPS if a != "Telegram"] + ["Telegram"]
    cases = [
        # (название, weak, ограничить список, ожидание)
        ("телеграмм", False, single, ("ok", "Telegram")),
        ("ютуб", False, None, ("ok", "YouTube")),
        ("ютуб музыку", False, None, ("ok", "YouTube Music")),
        ("камеру", False, None, ("ok", "Камера")),
        ("camera", False, None, ("ok", "Камера")),
        ("галерею", False, None, ("ok", "Галерея")),
        ("сбер", False, None, ("ok", "СберБанк")),
        ("яндекс", False, None, ("ok", "Яндекс")),
        ("яндекс музыку", False, None, ("ok", "Яндекс Музыка")),
        ("карты", False, None, ("ok", "Карты")),
        ("chrome", False, None, ("ok", "Chrome")),
        ("гугл", False, None, ("ok", "Google")),
        ("плей маркет", False, None, ("ok", "Google Play Маркет")),
        ("вк", False, None, ("ok", "VK")),
        ("вотсап", False, None, ("ok", "WhatsApp")),
        ("спотифай", False, None, ("ok", "Spotify")),
        ("settings", False, None, ("ok", "Настройки")),
        ("браузер", False, None, ("ambiguous", ["Chrome", "Firefox"])),
        ("telegram", False, None, ("ambiguous", ["Telegram", "Telegram"])),
        ("тикток", False, None, ("not_found", None)),
        ("кал", False, None, ("not_found", None)),
        ("ютуб", True, None, ("ok", "YouTube")),
        ("музыку", True, None, ("not_found", None)),
        ("браузер", True, None, ("not_found", None)),
        ("сбер", True, None, ("not_found", None)),
        ("Firefox", False, ["Chrome", "Firefox"], ("ok", "Firefox")),
    ]
    got = node(MATCH_JS % json.dumps((PHONE / "parse.ts").as_uri()),
               {"apps": APPS, "cases": [[n, w, lst] for n, w, lst, _ in cases]})
    if got is None:
        return
    for (name, weak, _lst, (status, want)), have in zip(cases, got):
        if status == "ambiguous":
            same = have["status"] == "ambiguous" and sorted(have["labels"]) == sorted(want)
        else:
            same = have["status"] == status and (want is None or have["label"] == want)
        check(f"{'включи ' if weak else ''}{name!r} → {status} {want or ''}" + ("" if same else f"  ({have})"), same)


# ─── E. actions.ts с поддельным плагином ───

ACTIONS_JS = r"""
import { registerHooks } from 'node:module';
const stubs = {
  '@capacitor/core': `export const Capacitor = { isPluginAvailable: (n) => n === 'PhoneActions' };
    export function registerPlugin(name) {
      return new Proxy({}, { get: (_, k) => (...a) => globalThis.__plugin(name, k, ...a) });
    }`,
  '../api': 'export function isNativeApp() { return true; }',
};
registerHooks({
  resolve(spec, ctx, next) {
    if (spec in stubs) return { url: 'data:text/javascript,' + encodeURIComponent(stubs[spec]), shortCircuit: true };
    if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(spec + '.ts', ctx);
    return next(spec, ctx);
  },
});
// localStorage веба — для журнала команд
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
const calls = [];
let fail = null;
const APPS = [['Telegram', 'org.telegram'], ['Chrome', 'com.chrome'], ['Firefox', 'org.firefox'], ['YouTube', 'com.yt']]
  .map(([label, pkg]) => ({ label, package: pkg }));
globalThis.__plugin = async (name, method, opts) => {
  if (method === 'then') return undefined;
  calls.push([method, opts ?? null]);
  if (fail && fail.method === method) { const e = new Error('x'); e.code = fail.code; throw e; }
  if (method === 'listApps') return { apps: APPS };
  if (method === 'status') return { alarm: { package: 'com.clock', label: 'Часы' }, timer: null, alarms: null, torch: true, apps: 4, device: 'X', sdk: 35 };
  return { handler: 'com.clock' };
};
const a = await import(INPUT.actions);
const now = new Date(...INPUT.now);
const out = [];
for (const step of INPUT.steps) {
  calls.length = 0;
  fail = step.fail ?? null;
  const report = await a.runPhoneCommand(step.persona ?? 'p1', step.text, ['Коннор'], now);
  out.push({ report, calls: calls.map(([m, o]) => [m, o]) });
}
const log = a.phoneLog();
a.notePhoneSkip('поставь будильник на 7', 'control_mode');
a.notePhoneSkip('как дела?', 'control_mode');
const skipLog = a.phoneLog().slice(0, 2);
const explain = await a.explainPhrase('открой браузер');
const status = await a.phoneStatus();
a.clearPhoneLog();
console.log(JSON.stringify({ available: a.phoneActionsAvailable(), out, log, skipLog, explain, status, cleared: a.phoneLog().length }));
"""


def test_actions():
    section("E. Исполнение (actions.ts, поддельный плагин)")
    steps = [
        {"text": "поставь будильник на 8:00"},                                     # 0
        {"text": "поставь будильник на 7 по будням с подписью «работа»"},           # 1
        {"text": "поставь будильник через 30 минут"},                              # 2
        {"text": "поставь будильник на 7 в пятницу"},                              # 3
        {"text": "поставь будильник"},                                             # 4 ask
        {"text": "на 6:45"},                                                       # 5 ответ
        {"text": "поставь будильник по будням"},                                   # 6 ask с днями
        {"text": "не надо"},                                                       # 7 отказ
        {"text": "7:30"},                                                          # 8 вопрос снят — обычная реплика
        {"text": "поставь таймер"},                                                # 9 ask
        {"text": "5"},                                                             # 10 ответ — 5 минут
        {"text": "таймер на 10 минут", "fail": {"method": "setTimer", "code": "no_handler"}},  # 11
        {"text": "открой браузер"},                                                # 12 ambiguous
        {"text": "второй"},                                                        # 13 выбор
        {"text": "открой браузер"},                                                # 14 ambiguous
        {"text": "chrome"},                                                        # 15 выбор по имени
        {"text": "открой браузер"},                                                # 16 ambiguous
        {"text": "как дела?"},                                                     # 17 посторонняя — снят
        {"text": "1"},                                                             # 18 уже не выбор
        {"text": "открой телеграм"},                                               # 19
        {"text": "открой тикток"},                                                 # 20 not_found
        {"text": "открой сбер"},                                                   # 21 не явно, не нашлось → null
        {"text": "включи музыку"},                                                 # 22 weak → null
        {"text": "включи ютуб"},                                                   # 23 weak бренд
        {"text": "включи фонарик", "fail": {"method": "torch", "code": "busy"}},   # 24
        {"text": "выключи фонарик"},                                               # 25
        {"text": "выключи будильник"},                                             # 26
        {"text": "покажи будильники", "fail": {"method": "showAlarms", "code": "no_handler"}},  # 27
        {"text": "поставь будильник", "persona": "p2"},                            # 28 ask у другой персоны
        {"text": "7", "persona": "p1"},                                            # 29 у p1 вопроса нет
        {"text": "8", "persona": "p2"},                                            # 30 ответ p2
        {"text": "поставь будильник на 9", "fail": {"method": "setAlarm", "code": "weird"}},  # 31 сбой
        {"text": "привет"},                                                        # 32
        {"text": "поставь таймер"},                                                # 33 ask
        {"text": "не надо"},                                                       # 34 отказ — в журнал
    ]
    got = node(ACTIONS_JS, {"actions": (PHONE / "actions.ts").as_uri(), "now": NOW, "steps": steps})
    if got is None:
        return
    out = got["out"]
    check("плагин доступен (натив + PhoneActions)", got["available"] is True)

    def rep(i):
        return out[i]["report"]

    def called(i):
        return [c[0] for c in out[i]["calls"]]

    check("будильник 8:00 → setAlarm(8, 0), итог ok",
          rep(0) == {"kind": "alarm", "status": "ok", "hour": 8, "minute": 0}
          and out[0]["calls"] == [["setAlarm", {"hour": 8, "minute": 0, "days": [], "label": ""}]])
    check("по будням с подписью → дни и подпись в плагин и в итог",
          out[1]["calls"][0][1] == {"hour": 7, "minute": 0, "days": [2, 3, 4, 5, 6], "label": "работа"}
          and rep(1)["days"] == [2, 3, 4, 5, 6] and rep(1)["label"] == "работа")
    check("через 30 минут (в 18:40) → 19:10", rep(2)["status"] == "ok" and (rep(2)["hour"], rep(2)["minute"]) == (19, 10))
    check("на 7 в пятницу (воскресенье) → unsupported_date, часы не трогаются",
          rep(3)["status"] == "unsupported_date" and rep(3)["nearest"] == "tomorrow" and called(3) == [])
    check("без времени → ask, ничего не ставится", rep(4) == {"kind": "alarm", "status": "ask"} and called(4) == [])
    check("ответ «на 6:45» → будильник 6:45", rep(5)["status"] == "ok" and (rep(5)["hour"], rep(5)["minute"]) == (6, 45)
          and called(5) == ["setAlarm"])
    check("переспрос помнит дни повтора («по будням»)", rep(6)["status"] == "ask")
    check("«не надо» → обычная реплика, ничего не ставится", rep(7) is None and called(7) == [])
    check("после отказа «7:30» — не ответ на снятый вопрос", rep(8) is None and called(8) == [])
    check("таймер без длительности → ask", rep(9) == {"kind": "timer", "status": "ask"})
    check("ответ «5» → таймер 5 минут", rep(10) == {"kind": "timer", "status": "ok", "seconds": 300}
          and out[10]["calls"] == [["setTimer", {"seconds": 300, "label": ""}]])
    check("нет приложения часов → no_handler (честный отказ)", rep(11)["status"] == "no_handler")
    check("«открой браузер» → ambiguous, варианты, ничего не открыто",
          rep(12)["status"] == "ambiguous" and rep(12)["candidates"] == ["Chrome", "Firefox"]
          and "openApp" not in called(12))
    check("«второй» → открыт Firefox", rep(13) == {"kind": "open_app", "status": "ok", "app": "Firefox"}
          and out[13]["calls"] == [["openApp", {"package": "org.firefox"}]])
    check("ответ названием («chrome») → Chrome", rep(15) == {"kind": "open_app", "status": "ok", "app": "Chrome"})
    check("посторонняя реплика снимает выбор", rep(17) is None and rep(18) is None and "openApp" not in called(18))
    check("«открой телеграм» → Telegram", rep(19) == {"kind": "open_app", "status": "ok", "app": "Telegram"})
    check("«открой тикток» (нет на телефоне) → not_found с названием",
          rep(20) == {"kind": "open_app", "status": "not_found", "app": "тикток"} and "openApp" not in called(20))
    check("«открой сбер» без явного приложения и без совпадения → обычная реплика", rep(21) is None)
    check("«включи музыку» → обычная реплика, список приложений даже не нужен открывать", rep(22) is None
          and "openApp" not in called(22))
    check("«включи ютуб» → YouTube", rep(23) == {"kind": "open_app", "status": "ok", "app": "YouTube"})
    check("фонарик, камера занята → busy", rep(24) == {"kind": "torch", "status": "busy", "on": True})
    check("выключи фонарик → torch(off)", rep(25) == {"kind": "torch", "status": "ok", "on": False}
          and out[25]["calls"] == [["torch", {"on": False}]])
    check("выключи будильник → список будильников, итог alarm_off",
          rep(26) == {"kind": "alarm_off", "status": "ok"} and called(26) == ["showAlarms"])
    check("нет часов для списка → no_handler", rep(27) == {"kind": "alarms_list", "status": "no_handler"})
    check("переспрос — у своей персоны: «7» у p1 — не ответ", rep(28)["status"] == "ask" and rep(29) is None)
    check("…а «8» у p2 — ответ", rep(30)["status"] == "ok" and rep(30)["hour"] == 8)
    check("непонятный сбой плагина → failed", rep(31)["status"] == "failed")
    check("обычная реплика → null, плагин не зовётся", rep(32) is None and called(32) == [])
    check("список приложений кешируется (один listApps на серию)",
          sum(called(i).count("listApps") for i in range(len(out))) == 1)

    log = got["log"]
    texts = [e["text"] for e in log]
    check("журнал: последние 20 команд, свежие сверху", len(log) == 20 and texts[0] == "не надо")
    by = {e["text"]: e for e in log}
    check("журнал: итог и кто принял интент", by["поставь будильник на 9"]["result"] == "failed"
          and "weird" in by["поставь будильник на 9"].get("detail", ""))
    check("журнал: ответ на переспрос — с видом переспроса", by["8"]["result"] == "ok" and by["8"]["kind"] == "alarm")
    check("журнал: «не надо» — отменено", any(e["text"] == "не надо" and e["result"] == "declined" for e in log))
    check("журнал: «включи музыку» — не приложение", by["включи музыку"]["result"] == "not_app")
    check("журнал: открытое приложение — с пакетом", by["включи ютуб"].get("detail") == "com.yt")
    check("журнал: обычные реплики не пишутся", "как дела?" not in texts and "привет" not in texts)
    skip = got["skipLog"]
    check("пропуск чатом (режим управления) — в журнал, обычная реплика — нет",
          len(skip) >= 1 and skip[0]["text"] == "поставь будильник на 7" and skip[0]["result"] == "control_mode"
          and skip[0]["kind"] == "alarm" and all(e["text"] != "как дела?" for e in skip))
    check("разбор фразы без исполнения: «открой браузер» — варианты",
          got["explain"]["cmd"]["kind"] == "open_app" and got["explain"]["candidates"] == ["Chrome", "Firefox"])
    check("статус телефона от плагина", got["status"]["alarm"]["label"] == "Часы" and got["status"]["apps"] == 4)
    check("журнал очищается", got["cleared"] == 0)


# ─── F. Ядро ───

def test_core():
    section("F. Ядро: схема, заметка, проводка до промпта")
    from pydantic import ValidationError
    from app.api import server
    from app.api.schemas import ChatRequest
    from app.features.phone_actions import action_note

    r = ChatRequest(persona="connor", message="поставь будильник на 8")
    check("phone_action по умолчанию нет", r.phone_action is None)
    rep = {"kind": "alarm", "status": "ok", "hour": 8, "minute": 0}
    r = ChatRequest(persona="connor", message="поставь будильник на 8", phone_action=rep)
    check("отчёт принимается", r.phone_action is not None and r.phone_action.hour == 8)
    for bad, name in [
        ({"kind": "alarm", "status": "ok", "hour": 25, "minute": 0}, "час 25"),
        ({"kind": "rm_rf", "status": "ok"}, "неизвестный вид"),
        ({"kind": "alarm", "status": "done"}, "неизвестный итог"),
        ({"kind": "timer", "status": "ok", "seconds": 100000}, "таймер больше суток"),
        ({"kind": "open_app", "status": "ok", "app": "x" * 200}, "имя приложения длиннее 80"),
        ({"kind": "open_app", "status": "ambiguous", "candidates": ["a"] * 7}, "больше 6 вариантов"),
    ]:
        try:
            ChatRequest(persona="connor", message="x", phone_action=bad)
            rejected = False
        except ValidationError:
            rejected = True
        check(f"отклоняется: {name}", rejected)

    check("_phone_note: заметка по отчёту", server._phone_note(r) == action_note(rep))
    check("_phone_note: из скина — нет",
          server._phone_note(ChatRequest(persona="connor", message="x", phone_action=rep, from_skin=True)) is None)
    check("_phone_note: с картинкой — нет",
          server._phone_note(ChatRequest(persona="connor", message="x", phone_action=rep, image="data:,")) is None)
    check("_phone_note: без отчёта — нет", server._phone_note(ChatRequest(persona="connor", message="x")) is None)
    src = (ROOT / "app" / "api" / "server.py").read_text(encoding="utf-8")
    check("оба эндпоинта чата передают заметку в process_message", src.count("turn_note=_phone_note(req)") == 2)

    # process_message → _process_message_impl
    from app.bot_instance import BotInstance
    bot = BotInstance.__new__(BotInstance)
    bot.computer_control = None
    bot.user_turn = lambda key: nullcontext()
    bot.stm_key = lambda chat_id, user_id: f"{chat_id or user_id}"
    seen = {}

    bot.web_single_user = True
    bot.owner = ""
    bot._cc_allowed_users = set()

    def impl(user_input, **kw):
        seen.update(kw, user_input=user_input, cc=bot._cc_allowed(kw.get("user_id"), kw.get("chat_id")))
        if user_input == "boom":
            raise RuntimeError("boom")
        return "ответ"
    bot._process_message_impl = impl
    check("вне хода владелец допущен к режиму управления", bot._cc_allowed("web_user", "web") is True)
    bot.process_message("открой браузер", user_id="web_user", chat_id="web", turn_note="NOTE")
    check("process_message доводит turn_note до хода, реплика — как есть",
          seen.get("turn_note") == "NOTE" and seen.get("user_input") == "открой браузер")
    check("в ходе команды телефону компьютер закрыт (_cc_allowed False: ни «почини браузер», ни режим)",
          seen.get("cc") is False)
    check("после хода — снова допущен", bot._cc_allowed("web_user", "web") is True)
    try:
        bot.process_message("boom", user_id="web_user", chat_id="web", turn_note="NOTE")
    except RuntimeError:
        pass
    check("…и после исключения в ходе", bot._cc_allowed("web_user", "web") is True)
    bot.process_message("открой браузер", user_id="web_user", chat_id="web")
    check("без заметки — None, компьютер доступен как раньше", seen.get("turn_note") is None and seen.get("cc") is True)

    bsrc = (ROOT / "app" / "bot_instance.py").read_text(encoding="utf-8")
    check("заметка — в системный блок (addon_blocks) и в light-режиме",
          "if turn_note:\n                addon_blocks.append(turn_note)" in bsrc)
    check("ход с заметкой — без веб-поиска", "and not turn_note):" in bsrc)

    # Заметка в системном блоке промпта персоны
    from app.core.persona import PersonaLayer
    p = PersonaLayer("__no_such_persona__")
    p.system_prompt = "PERSONA PROMPT"
    msgs = p.prepare_messages("поставь будильник на 8", addon_blocks=[action_note(rep)])
    check("prepare_messages: заметка в системном сообщении, реплика пользователя без неё",
          action_note(rep) in msgs[0]["content"] and msgs[-1]["content"].endswith("] поставь будильник на 8")
          and action_note(rep) not in msgs[-1]["content"])


def main():
    print("Команды телефону")
    if not shutil.which("node"):
        print("  (TS-части пропущены — нет Node)")
    else:
        test_notes()
        test_parse()
        test_helpers()
        test_match()
        test_actions()
    test_core()
    print(f"\n{ok - failures}/{ok} OK" + (f", FAIL: {failures}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

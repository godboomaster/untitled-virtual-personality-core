"""Разбор напоминаний на телефоне (web/src/offline/remparse.ts) — сверка с
ядром (app/features/reminder_manager.py) на одних и тех же фразах.

Телефон без ноутбука ставит напоминание сам: время из фразы разбирает перенос
parse_reminder ядра. Ставит ли телефон напоминание там же и на то же время,
что и ядро, проверяется прогоном remparse.ts под Node (снятие типов, Node ≥
23.6) с одним и тем же «сейчас».

Запуск: PYTHONPATH=. python3 -m scripts.test_offline_remparse
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="offline_remparse_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

results = {"ok": 0, "fail": 0}


def check(name, cond):
    results["ok" if cond else "fail"] += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


# Суббота, 10.10.2026, 16:39:12 — стенные часы и ядра, и телефона
NOW = (2026, 10, 10, 16, 39, 12)

PHRASES = [
    "Напомни мне проверить уведомления через 2 минуты",
    "напомни через 30 минут позвонить маме",
    "напомни через 2 часа сделать домашку",
    "через 10 мин напомни",
    "напомни завтра утром купить шоколад",
    "напомни в 8 вечера",
    "remind me to call mom in 30 minutes",
    "remind me at 5 pm",
    "remind me tomorrow morning",
    "Коннор, напомни через полчаса выключить плиту",
    "напомни через полтора часа забрать посылку",
    "напомни через час",
    "напомни через минуты 4 проверить чайник",
    "напомни через пять минут",
    "напомни через две минуты проверить уведомления",
    "напомни через 1,5 часа",
    "напомни через 3 дня оплатить интернет",
    "напомни в 11:30 созвон",
    "напомни в 11.30 созвон",
    "напомни до 12 сдать отчёт",
    "напомни к полудню",
    "напомни в полночь",
    "напомни послезавтра в 10 записаться к врачу",
    "напомни сегодня вечером полить цветы",
    "напомни в 8:30 вечера выпить таблетку",
    "напомни ночью",
    "напомни днём позвонить",
    "напомни завтра",
    "напомни в 12 ночи",
    "доброе утро! напомни через 5 минут",
    "remind me in an hour to stretch",
    "remind me in half an hour",
    "remind me in two hours to check the oven",
    "remind me 15 minutes from now",
    "remind me after 2 hours",
    "remind me at 7:45 am to walk the dog",
    "remind me at noon",
    "remind me tomorrow at 8",
    "remind me tonight to call Sam",
    "good morning, remind me in 10 minutes",
    "remind me in the evening to read",
    "remind me the day after tomorrow at 9 pm",
    "напомни мне",
    "ты мне напомнил?",
    "напоминание пришло вовремя",
    "привет",
    "напомни в 25",
    "напомни через 40 дней",
    "напомни через 5 секунд",
    "Лира, напомни в 9 утра медитация",
    "Нова, напомни через час полить цветы",
    "напомни каждый день в 9 пить воду",
    "напоминай по пятницам в 18 убраться",
    "remind me every monday at 9 to plan the week",
    "поставь напоминание на завтра в 15:00 встреча с Петей",
    "сделай напоминание через 20 минут с содержимым: вынуть бельё",
    "напомни, пожалуйста, через 15 минут проверить духовку",
    "напомни в 7 30 будильник",
    "remind me at 5:30 p.m. to leave",
    "set a reminder in 20 minutes",
    "can you remind me in 10 minutes to stand up?",
    "Напомни через 2 минуты",
    "напомни через минутку",
    "НАПОМНИ ЗАВТРА В 9 УТРА ЗВОНОК",
    "напомни около 7 вечера",
    "напомни в 3 дня забрать ребёнка",
    "напомни до полуночи лечь спать",
    "remind me by midnight",
    "remind me in a couple of minutes",
    "remind me in 45 mins",
    "remind me in 3 days to pay rent",
    "напомни через 1 день",
    "напомни в 16:39",
    "напомни в 16:40",
    "напомни завтра днём",
    "напомни послезавтра",
    "напомни 9 утра",
]

# Обращения к персоне — как их передаёт бот (_reminder_names) и набор телефона (kit.names);
# «Нова» не в списке — её имя в тексте остаётся у обоих
NAMES = ["коннор", "connor", "жабка", "лира"]

# Ответы на «когда напомнить?» и посторонние реплики
REPLIES = ["не надо", "нет, спасибо", "в 8", "завтра утром", "через час", "ок", "18:30",
           "never mind", "at 5", "хорошо", "передумал", "давай в пятницу", "1"]

NODE_CODE = """
import * as rp from %s;
const input = JSON.parse(%s);
const now = new Date(...input.now.map((v, i) => i === 1 ? v - 1 : v));
const out = {
  parse: input.phrases.map((t) => rp.parseReminder(t, input.names, now)),
  recurring: input.phrases.map((t) => rp.isRecurringAsk(t)),
  create: input.phrases.map((t) => rp.isReminderCreateRequest(t)),
  decline: input.replies.map((t) => rp.isPendingDecline(t)),
  attempt: input.replies.map((t) => rp.looksLikeTimeAttempt(t)),
};
console.log(JSON.stringify(out));
"""


def main():
    print("Разбор напоминаний телефона ↔ ядро")
    if not shutil.which("node"):
        print("  (пропущено — нет Node)")
        return 0
    from app.features import reminder_manager as rm

    fixed = datetime(*NOW)
    rm.timeutil.now = lambda: fixed

    src = ROOT / "web" / "src" / "offline" / "remparse.ts"
    code = NODE_CODE % (json.dumps(src.as_uri()),
                        json.dumps(json.dumps({"now": list(NOW), "phrases": PHRASES, "replies": REPLIES, "names": NAMES},
                                              ensure_ascii=False)))
    env = {**os.environ, "TZ": "Europe/Moscow"}
    res = subprocess.run(["node", "--input-type=module", "-e", code],
                         capture_output=True, text=True, timeout=60, env=env)
    if res.returncode != 0:
        check(f"remparse.ts запускается под Node: {res.stderr.strip()[:400]}", False)
        return 1
    got = json.loads(res.stdout.strip().splitlines()[-1])

    print("\n— parse_reminder")
    for phrase, have in zip(PHRASES, got["parse"]):
        want = rm.parse_reminder(phrase, NAMES)
        if want is None or have is None:
            same = want is None and have is None
        else:
            same = want[0] == have["task"] and abs(want[1] - have["delay"]) < 1e-6
        shown = None if want is None else (want[0], round(want[1]))
        check(f"{phrase!r} → {shown}" + ("" if same else f"  (телефон: {have})"), same)

    print("\n— повтор / просьба создать")
    for phrase, rec, create in zip(PHRASES, got["recurring"], got["create"]):
        want_rec = rm._has_remind_word(phrase.lower()) and bool(
            rm._RECURRING_DAILY_RE.search(phrase.lower()) or rm._RECURRING_WEEKLY_RE.search(phrase.lower()))
        check(f"повтор {phrase!r}: {want_rec}", want_rec == rec)
        check(f"просьба {phrase!r}: {rm.is_reminder_create_request(phrase)}",
              rm.is_reminder_create_request(phrase) == create)

    print("\n— ответы на «когда?»")
    for reply, dec, att in zip(REPLIES, got["decline"], got["attempt"]):
        check(f"отказ {reply!r}: {rm.is_pending_decline(reply)}", rm.is_pending_decline(reply) == dec)
        check(f"время {reply!r}: {rm.looks_like_time_attempt(reply)}", rm.looks_like_time_attempt(reply) == att)

    print(f"\nИтого: {results['ok'] + results['fail']} проверок, {results['fail']} провалов")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())

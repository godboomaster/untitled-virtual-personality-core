"""Заметки на телефоне без ядра (web/src/offline/notesParse.ts) — сверка с
ядром (app/features/user_notes.py) на одних и тех же фразах и заметках.

Без ядра «запиши в заметки …», «удали заметку 2», «что у меня в заметках?»
разбирает перенос parse_request ядра, запись и удаление делает код, модели —
итог (prompt_*), список — отдельным сообщением (render_list). Совпадает ли
телефон с ядром, проверяется прогоном notesParse.ts под Node (снятие типов,
Node ≥ 23.6); даты заметок — в одном поясе у обоих.

Запуск: PYTHONPATH=. python3 -m scripts.test_offline_notes
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="offline_notes_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"
TZ = "Europe/Moscow"
os.environ["TIMEZONE"] = TZ

results = {"ok": 0, "fail": 0}


def check(name, cond):
    results["ok" if cond else "fail"] += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


NAMES = ["Коннор", "connor", "жабка"]

# Фразы из scripts/test_user_notes.py (раздел 1) и сверх них
PHRASES = [
    "запиши в заметки купить молоко",
    "Коннор, запиши в заметки: код домофона 1234",
    "запиши в заметки, что завтра приедет мама",
    "запиши купить молоко в заметки",
    "запиши мне в заметки пожалуйста позвонить Васе",
    "запиши номер Васи в заметки: 89001234567",
    "в заметки запиши: сдать отчёт до пятницы",
    "сделай заметку: проверить масло",
    "новая заметка — купить подарок",
    "заметка: пароль на роутере",
    "можешь записать в заметки, что встреча в 15:00?",
    "Мука, яйца, молоко — запиши в заметки",
    "запомни в заметках: у Пети день рождения 5 мая",
    "сохрани в блокнот идею для стартапа",
    "запиши в заметки рецепт:\nмука 200 г\nяйца 2 шт",
    "запиши в заметки напомнить маме про лекарства",
    "запиши в заметки",
    "Ты можешь записать в заметки?",
    "запиши это в заметки",
    "запиши этот рецепт в заметки",
    "запиши в заметки то, что ты сейчас сказал",
    "add buy milk to my notes",
    "Connor, add to my notes: call mom tomorrow",
    "please write down in my notes that the meeting is at 3",
    "take a note: buy a gift",
    "make a note of this",
    "запиши в заметки всё, что ты перечислил выше",
    "add your last answer to my notes",
    "запиши в заметки: Петя сказал, что ты сказал ему правду",
    "запиши в заметки купить хлеб, пожалуйста, спасибо",
    "can you add this to my notes?",
    "удали заметку 2",
    "удали из заметок код домофона",
    "удали заметку про домофон",
    "удали последнюю заметку",
    "убери заметки 1 и 3",
    "удали все заметки",
    "очисти заметки",
    "удали все заметки про работу",
    "delete note 2",
    "remove the last note",
    "clear my notes",
    "что у меня в заметках?",
    "покажи заметки",
    "мои заметки",
    "Коннор, что в заметках",
    "what's in my notes?",
    "show my notes",
    "посмотри в заметках, какой код от домофона",
    "что я записывал в заметки про машину?",
    "what did I write in my notes about the car?",
    "не записывай это в заметки",
    "don't add this to my notes",
    "запиши купить хлеб",
    "добавь в список дел купить хлеб",
    "напомни через час позвонить",
    "открой заметки",
    "note that I'm late",
    "привет",
    "",
    # сверх набора ядра
    "Жабка, черкни в заметки: шиномонтаж в субботу",
    "эй Коннор, занеси-ка в мои заметки номер страховки 123-456",
    "ЗАПИШИ В ЗАМЕТКИ КУПИТЬ БАТАРЕЙКИ",
    "запиши в заметки ёлку купить",
    "jot down in my notebook: dentist on Friday",
    "Коннор запиши в заметки идею: приложение для заметок",
    "удали заметку номер 3",
    "удали заметки 2-4",
    "сотри первую заметку",
    "wipe all my notes",
    "что там в блокноте?",
    "в заметках есть что-нибудь про отпуск?",
    "запиши " + "очень длинную мысль " * 40 + "в заметки",
    "мысль номер раз\nмысль номер два\nзапиши это в заметки",
]

NOTES = [
    {"id": "a1", "text": "Купить молоко", "created_at": 1791400000.0},
    {"id": "a2", "text": "Код домофона 1234", "created_at": 1791500000.0},
    {"id": "a3", "text": "Позвонить Васе про машину", "created_at": 1791600000.0},
    {"id": "a4", "text": "Работа: сдать отчёт до пятницы", "created_at": 1791700000.0},
    {"id": "a5", "text": "Работа: созвон с командой\nв 15:00", "created_at": 1791800000.0},
    {"id": "a6", "text": "Длинная заметка " + "слово " * 300, "created_at": 1791900000.0},
]

DELETES = ["удали заметку 2", "удали из заметок код домофона", "удали заметку про домофон",
           "удали последнюю заметку", "убери заметки 1 и 3", "удали все заметки",
           "удали все заметки про работу", "удали заметку про работу", "delete note 2",
           "remove the last note", "сотри первую заметку", "удали заметки 2-4",
           "удали заметку 99", "удали заметку про слона"]

NODE_CODE = """
import * as np from %s;
const input = JSON.parse(%s);
const req = (r) => r && { kind: r.kind, text: r.text, ref: r.ref, all: r.all };
const ids = (list) => list && list.map((n) => n.id);
const out = {
  parse: input.phrases.map((t) => req(np.parseNotesRequest(t, input.names))),
  del: input.deletes.map((t) => ids(np.resolveDelete(np.parseNotesRequest(t, input.names), input.notes))),
  delEmpty: ids(np.resolveDelete(np.parseNotesRequest('удали заметку 2', []), [])),
  list: [np.renderNotesList(input.notes, 'ru'), np.renderNotesList(input.notes, 'en'), np.renderNotesList([], 'ru')],
  prompt: np.notesForPrompt(input.notes),
  prompts: [
    np.promptSaved('Купить молоко'), np.promptSaved('x'.repeat(400)), np.promptNothingToSave(), np.promptFull(),
    np.promptDeleted(input.notes.slice(0, 2), 4), np.promptDeleteUnclear(input.notes), np.promptDeleteUnclear([]),
    np.promptShow(input.notes), np.promptShow([]), np.promptAsk(input.notes), np.promptAsk([]),
  ],
  strip: np.stripAddress('эй Коннор, запиши в заметки X', input.names),
};
console.log(JSON.stringify(out));
"""


def main():
    print("Заметки телефона ↔ ядро")
    if not shutil.which("node"):
        print("  (пропущено — нет Node)")
        return 0
    from app.features import user_notes as un

    src = ROOT / "web" / "src" / "offline" / "notesParse.ts"
    payload = {"phrases": PHRASES, "names": NAMES, "notes": NOTES, "deletes": DELETES}
    code = NODE_CODE % (json.dumps(src.as_uri()), json.dumps(json.dumps(payload, ensure_ascii=False)))
    res = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True,
                         timeout=60, env={**os.environ, "TZ": TZ})
    if res.returncode != 0:
        check(f"notesParse.ts запускается под Node: {res.stderr.strip()[:600]}", False)
        return 1
    got = json.loads(res.stdout.strip().splitlines()[-1])

    print("\n— parse_request")
    for phrase, have in zip(PHRASES, got["parse"]):
        r = un.parse_request(phrase, NAMES)
        want = r and {"kind": r.kind, "text": r.text, "ref": r.ref, "all": r.all}
        shown = phrase if len(phrase) < 70 else phrase[:67] + "…"
        check(f"{shown!r} → {want and (want['kind'], want['text'][:40])}"
              + ("" if want == have else f"  (телефон: {have})"), want == have)

    print("\n— resolve_delete")
    for phrase, have in zip(DELETES, got["del"]):
        picked = un.resolve_delete(un.parse_request(phrase, NAMES), NOTES)
        want = picked and [n["id"] for n in picked]
        check(f"{phrase!r} → {want}" + ("" if want == have else f"  (телефон: {have})"), want == have)
    check("пустая книжка → []", got["delEmpty"] == [] == un.resolve_delete(un.parse_request("удали заметку 2"), []))

    print("\n— список и промпты")
    check("render_list ru", got["list"][0] == un.render_list(NOTES, "ru"))
    check("render_list en", got["list"][1] == un.render_list(NOTES, "en"))
    check("render_list пусто", got["list"][2] == un.render_list([], "ru"))
    check("notes_for_prompt", got["prompt"] == un.notes_for_prompt(NOTES))
    want_prompts = [
        un.prompt_saved("Купить молоко"), un.prompt_saved("x" * 400), un.prompt_nothing_to_save(),
        un.prompt_full(), un.prompt_deleted(NOTES[:2], 4), un.prompt_delete_unclear(NOTES),
        un.prompt_delete_unclear([]), un.prompt_show(NOTES), un.prompt_show([]),
        un.prompt_ask(NOTES), un.prompt_ask([]),
    ]
    names = ["saved", "saved (длинная)", "nothing_to_save", "full", "deleted", "delete_unclear",
             "delete_unclear (пусто)", "show", "show (пусто)", "ask", "ask (пусто)"]
    for name, want, have in zip(names, want_prompts, got["prompts"]):
        check(f"prompt_{name}", want == have)
    check("strip_address", got["strip"] == un.strip_address("эй Коннор, запиши в заметки X", NAMES))

    print(f"\nИтого: {results['ok'] + results['fail']} проверок, {results['fail']} провалов")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())

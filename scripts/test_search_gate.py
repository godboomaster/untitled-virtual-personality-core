"""Тест гейта веб-поиска (app/features/search_gate.py) и меток дня в дневнике.

Инцидент: «что сегодня делал?» уходило в DuckDuckGo, «коннор?» гуглилось как
имя (Википедия про Джона Коннора). Выдача по SOURCE PRIORITY перебивала
дневник персоны, а ожидание поиска добавляло до 25 с к ответу.

Запуск: PYTHONPATH=. python3 scripts/test_search_gate.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


NAMES = {"коннор", "connor", "Коннор"}

SKIP = {
    "коннор?": "ping",
    "эй, коннор": "ping",
    "Коннор!": "ping",
    "привет, коннор!": "smalltalk",
    "спасибо": "smalltalk",
    "ок": "smalltalk",
    "ты тут?": "smalltalk",
    "Что сегодня делал?": "self",
    "Коннор, что ты сегодня делал?": "self",
    "а что ты делал без меня?": "self",
    "чем занимался вчера?": "self",
    "что делаешь": "self",
    "как дела?": "self",
    "как у тебя дела, коннор": "self",
    "как прошёл твой день?": "self",
    "как ты себя чувствуешь": "self",
    "где ты был?": "self",
    "что нового?": "self",
    "расскажи про свой день": "self",
    "how are you?": "self",
    "what did you do today?": "self",
}

SEARCH = [
    "что сегодня делал путин?",
    "ты знаешь погоду в москве?",
    "какая погода",
    "погода?",
    "коннор, найди рецепт борща",
    "как дела у spacex",
    "как дела в украине",
    "что делать если болит голова",
    "кто такой джон коннор",
    "что нового в python 3.14",
    "как ты думаешь, биткоин вырастет?",
]


def test_gate():
    from app.features.search_gate import search_skip_reason, is_self_question
    print("\n── search_skip_reason: без поиска ──")
    for text, reason in SKIP.items():
        check(f"{text!r} → {reason}", search_skip_reason(text, NAMES) == reason)
    print("\n── search_skip_reason: поиск нужен ──")
    for text in SEARCH:
        check(f"{text!r} → поиск", search_skip_reason(text, NAMES) is None)
    check("is_self_question: «что сегодня делал?»", is_self_question("что сегодня делал?", NAMES))
    check("is_self_question: «коннор?» — не вопрос о себе", not is_self_question("коннор?", NAMES))


def test_episode_labels():
    from app.core.self_memory import _episode_day_label as label
    print("\n── self_memory: метки дня эпизодов ──")
    now = datetime(2026, 9, 22, 23, 5)
    check("сегодняшний эпизод", label("2026-09-22T20:38:00", now) == "today, 20:38")
    check("вчерашний эпизод", label("2026-09-21T11:46:10.72", now) == "yesterday, 11:46")
    check("давний эпизод — дата", label("2026-09-19T00:23:39", now) == "2026-09-19")
    check("пустой таймстемп — без метки", label("", now) == "")
    check("None — без метки", label(None, now) == "")


if __name__ == "__main__":
    test_gate()
    test_episode_labels()
    print(f"\nИтого: {ok - failures}/{ok} OK")
    sys.exit(1 if failures else 0)

"""Гейт веб-поиска телефона без ноутбука (web/src/offline/gate.ts) — сверка с
ядром (app/features/search_gate.py) на одних и тех же фразах.

Шаблоны телефон получает в ките (offline_api._search_gate); регулярки
Python переносятся в JS (\\w → буквы Unicode). Ищет ли телефон там же, где
ядро, — проверяется прогоном gate.ts под Node (снятие типов, Node ≥ 23.6).
Три кита: белый список («Экономия нейронки», по умолчанию), пункт
search_every_message (только чёрный список) и кит старого ядра без need.

Запуск: PYTHONPATH=. python3 -m scripts.test_offline_gate
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
os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="offline_gate_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

results = {"ok": 0, "fail": 0}


def check(name, cond):
    results["ok" if cond else "fail"] += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


PHRASES = [
    "", "Коннор", "Коннор!", "эй, Коннор", "привет", "Привет, Коннор!", "спасибо", "ок",
    "ты тут?", "ну ладно", "Доброе утро", "how are you", "How's your day?", "thanks",
    "что делал сегодня?", "Что ты сегодня делал?", "как дела", "как у тебя дела?",
    "как прошёл твой день", "как себя чувствуешь", "где ты был вчера", "что нового у тебя",
    "чем занимаешься", "расскажи про свой день", "what are you up to today",
    "what have you been doing lately", "how's it going", "hey Connor how are you doing",
    "какая погода в Москве завтра", "что сегодня делал президент?", "кто выиграл матч",
    "Коннор, сколько стоит биткоин", "найди рецепт борща", "what is the capital of Peru",
    "ты знаешь погоду в Москве?", "как дела у Тесла на бирже", "расскажи про Детройт",
    # белый список: искать
    "погода?", "курс доллара сегодня", "последние новости", "кто выиграл вчерашний матч?",
    "сколько стоит iPhone 17?", "когда выйдет GTA 6?", "кто такой Илон Маск?",
    "что такое ChatGPT?", "что нового в python 3.14", "что было в 1812 году?",
    "Коннор, что такое «Детройт: Стать человеком»?", "что сейчас происходит в мире?",
    "поищи отзывы о Samsung", "загугли, когда открывается Эрмитаж",
    "what's the weather like in London?", "search for cheap flights to Rome",
    "look up the population of Japan", "latest news about SpaceX",
    "who won the game last night?", "how much is a Tesla Model 3?", "who is Elon Musk",
    "Google the best pizza in Moscow", "как дела у spacex",
    # белый список: не искать
    "я сегодня устал", "почему небо голубое?", "что ты помнишь обо мне?",
    "Как тебе Москва?", "что думаешь о фильме Интерстеллар?", "What do you think about Paris?",
    "I love Paris", "Can you help me with my homework?", "what is love?", "а сейчас что?",
    "сколько будет 2+2", "что видит робот?", "кто он такой?", "Коннор, Что думаешь?",
    "I love Google", "what should I cook tonight?", "что мне приготовить сегодня?",
    "Привет! Ну как ты там?", "КАК ДЕЛА У ТЕБЯ?", "ну и погода сегодня",
]

NODE_CODE = """
import { wantsSearch } from %s;
const input = JSON.parse(%s);
const out = input.phrases.map((t) => wantsSearch(input.kit, t));
console.log(JSON.stringify(out));
"""


def main():
    print("Гейт поиска телефона ↔ ядро")
    if not shutil.which("node"):
        print("  (пропущено — нет Node)")
        return 0
    from app.api.offline_api import _search_gate
    from app.core import llm_economy
    from app.features.search_gate import search_gate_reason, search_skip_reason

    names = ["Коннор", "connor"]
    gate = _search_gate()
    check("кит: белый список и флаг «на каждое сообщение» (по умолчанию — нет)",
          set(gate["need"]) >= {"search", "topic", "fresh", "wh", "you"}
          and gate["every_message"] is False)
    # Пункт «Экономии нейронки» доходит до кита
    llm_economy.update({"search_every_message": True})
    check("кит: пункт search_every_message → every_message", _search_gate()["every_message"] is True)
    llm_economy.update({"search_every_message": False})

    legacy = {k: gate[k] for k in ("address_words", "smalltalk", "self_patterns")}
    cases = [
        ("белый список", gate,
         [search_gate_reason(t, names) is None for t in PHRASES]),
        ("на каждое сообщение", dict(gate, every_message=True),
         [search_gate_reason(t, names, every_message=True) is None for t in PHRASES]),
        ("кит старого ядра", legacy,
         [search_skip_reason(t, names) is None for t in PHRASES]),
    ]
    gate_ts = ROOT / "web" / "src" / "offline" / "gate.ts"
    for label, rules, expected in cases:
        print(f"── {label} ──")
        kit = {"name": "Коннор", "persona": "connor", "search_gate": rules}
        code = NODE_CODE % (json.dumps(gate_ts.as_uri()),
                            json.dumps(json.dumps({"kit": kit, "phrases": PHRASES}, ensure_ascii=False)))
        res = subprocess.run(["node", "--input-type=module", "-e", code],
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            check(f"gate.ts запускается под Node: {res.stderr.strip()[:300]}", False)
            return 1
        got = json.loads(res.stdout.strip().splitlines()[-1])
        for phrase, want, have in zip(PHRASES, expected, got):
            check(f"{phrase!r}: {'искать' if want else 'не искать'}", want == have)
    # Белый список правда режет: часть фраз без него ушла бы в поиск
    check("белый список отсекает часть фраз, которые искал чёрный",
          sum(cases[1][2]) > sum(cases[0][2]))
    print(f"\nИтого: {results['ok'] + results['fail']} проверок, {results['fail']} провалов")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())

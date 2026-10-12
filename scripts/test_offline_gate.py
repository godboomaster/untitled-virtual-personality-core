"""Гейт веб-поиска телефона без ноутбука (web/src/offline/gate.ts) — сверка с
ядром (app/features/search_gate.py) на одних и тех же фразах.

Шаблоны телефон получает в ките (offline_api._search_gate); регулярки
Python переносятся в JS (\\w → буквы Unicode). Ищет ли телефон там же, где
ядро, — проверяется прогоном gate.ts под Node (снятие типов, Node ≥ 23.6).

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
    from app.features.search_gate import search_skip_reason

    kit = {"name": "Коннор", "persona": "connor", "search_gate": _search_gate()}
    expected = [search_skip_reason(t, ["Коннор", "connor"]) is None for t in PHRASES]
    gate_ts = ROOT / "web" / "src" / "offline" / "gate.ts"
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
    print(f"\nИтого: {results['ok'] + results['fail']} проверок, {results['fail']} провалов")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())

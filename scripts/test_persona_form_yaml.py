"""Форма «Создать персону»: имя, описание и слова-триггеры в YAML.

Форма собирает persona.yaml строками (PersonaCreateModal.buildYaml); значения
идут через yamlScalar (web/src/components/personaYamlSync.ts). Без него
описание «Смотритель: маяк» становилось вложенным ключом — YAML не
разбирался ни на ядре, ни на телефоне, — «#1» комментарием, «yes» булевым.
Здесь: строка → yamlScalar под Node → разбор PyYAML (так читает ядро) даёт
ту же строку.

Запуск: PYTHONPATH=. python3 -m scripts.test_persona_form_yaml
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SYNC = ROOT / "web" / "src" / "components" / "personaYamlSync.ts"

CASES = [
    "Лира", "Тестовая персона: смотритель маяка, создана на телефоне", "#1 в мире", "a: b # c",
    "yes", "on", "No", "12", "1.5", "null", "~", "- дефис", "@имя", "*звезда", "{скобки}",
    "[список]", "кавычка \" и \\ слэш", "строка\nвторая", "Великий Арродес", "Don Quixote",
]

NODE = """
const { yamlScalar } = await import(%s);
const input = JSON.parse(%s);
console.log(JSON.stringify(input.map((s) => yamlScalar(s))));
"""

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def main():
    if not shutil.which("node") or not (ROOT / "web" / "node_modules" / "yaml").is_dir():
        print("  (пропуск — нет node или web/node_modules)")
        return 0
    code = NODE % (json.dumps(SYNC.as_uri()), json.dumps(json.dumps(CASES, ensure_ascii=False)))
    res = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True,
                         timeout=60, cwd=ROOT / "web")
    if res.returncode != 0:
        check(f"Node: {res.stderr.strip()[:400]}", False)
        return 1
    scalars = json.loads(res.stdout.strip().splitlines()[-1])
    print("Скаляры формы → PyYAML")
    for s, sc in zip(CASES, scalars):
        want = " ".join(s.split("\n")).replace("  ", " ")
        try:
            got = yaml.safe_load(f"description: {sc}\n")["description"]
        except yaml.YAMLError as e:
            got = f"ERR {e}"
        check(f"{s!r} → {sc}", got == want)
    check("простое имя — без лишних кавычек", scalars[0] == "Лира" and scalars[18] == "Великий Арродес")
    # Слово-триггер — элемент списка
    doc = yaml.safe_load("trigger_words:\n" + "\n".join(f"  - {sc}" for sc in scalars))
    check("слова-триггеры читаются списком строк", all(isinstance(w, str) for w in doc["trigger_words"]))
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Снять живой снапшот страницы в фикстуру для eval_snapshot_scoring.py.

Запуск: python -m scripts.capture_snapshot_fixture <host-часть> <имя_фикстуры>
Пример: python -m scripts.capture_snapshot_fixture dodopizza.ru dodo_main

Страница должна быть открыта в браузере бота (CDP). После записи вручную
заполнить "goal" и "expect" в получившемся JSON.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    host_part, name = sys.argv[1], sys.argv[2]
    from app.features.browser_actions import snapshot_elements
    url, host, items = snapshot_elements(host_part)
    out = Path(__file__).parent / "snapshot_fixtures" / f"{name}.json"
    out.write_text(json.dumps(
        {"name": name, "url": url, "goal": "TODO", "comment": "",
         "expect": {"idx": 0}, "items": items},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Записано {len(items)} элементов → {out}")
    print("Теперь заполни в JSON поля goal и expect "
          "({\"idx\": N} | {\"none\": true} | {\"ambiguous\": true})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

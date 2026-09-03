"""Регрессионный eval скоринга/выбора элемента по фикстурам снапшотов —
без живого браузера. Каждая фикстура в scripts/snapshot_fixtures/*.json:

  {"name": "...", "goal": "текст команды", "comment": "...", 
   "items": [...],  # элементы как из snapshot_elements (idx/tag/text/ctx/…)
   "expect": {"idx": N}        # детерминированный выбор должен дать idx N
             {"none": true}    # честный отказ (кандидата нет / скор слабый)
             {"ambiguous": true}}  # явного лидера нет — уйдёт в LLM-выбор

Снимок живого сайта в новую фикстуру: python -m scripts.capture_snapshot_fixture.
Запуск: python -m scripts.eval_snapshot_scoring
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

FIXTURES = Path(__file__).parent / "snapshot_fixtures"


def run(verbose: bool = True):
    """→ (прошло, всего). LLM не вызывается (router=None): проверяем только
    детерминированный слой."""
    from app.features.computer_control import (
        ComputerControlManager, LEADER_MARGIN)
    mgr = ComputerControlManager(context="eval",
                                 config={"click": True},
                                 base_dir=Path(tempfile.mkdtemp(
                                     prefix="scoreval_")))
    passed = failed = 0
    for fp in sorted(FIXTURES.glob("*.json")):
        fix = json.loads(fp.read_text(encoding="utf-8"))
        name = fix.get("name") or fp.stem
        goal = fix["goal"]
        items = fix["items"]
        expect = fix["expect"]
        idx, meta = mgr._choose_element(goal, items, None)
        ok = True
        why = ""
        if expect.get("none"):
            ok = idx is None
            why = f"ждали отказ, получили idx={idx}"
        elif expect.get("ambiguous"):
            scored = mgr._score_candidates(items, goal)
            top = scored[0][0] if scored else 0.0
            second = scored[1][0] if len(scored) > 1 else None
            ok = bool(scored) and second is not None \
                and top - second < LEADER_MARGIN
            why = f"ждали неоднозначность, скоры: {top}/{second}"
        elif "idx" in expect:
            ok = idx == expect["idx"] and meta.get("path") in ("score",)
            why = (f"ждали idx={expect['idx']} по скору, "
                   f"получили idx={idx} (путь {meta.get('path')})")
        else:
            ok = False
            why = "неизвестный expect"
        passed += 1 if ok else 0
        failed += 0 if ok else 1
        if verbose:
            print(f"  [{'OK' if ok else 'FAIL'}] {name}: {goal!r}"
                  + ("" if ok else f" — {why}"))
    return passed, passed + failed


def main() -> int:
    passed, total = run()
    print(f"\nИтог: {passed}/{total} фикстур")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

"""Сводка audit.jsonl computer_control'а: на чём падает, какими путями
резолвятся элементы, где не хватает рецептов и как откалибровать
risk_overrides по реальным логам, а не на глаз.

Запуск: python -m scripts.audit_cc_stats [путь/к/audit.jsonl ...]
Без аргументов — все data/*/computer_control/audit.jsonl.
"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _load(paths):
    for p in paths:
        try:
            for line in Path(p).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec
        except OSError:
            continue


def _pct(ok: int, total: int) -> str:
    return f"{ok * 100 // total}%" if total else "—"


def _table(title: str, rows, col1="причина", limit: int = 15):
    print(f"\n== {title} ==")
    if not rows:
        print("  (пусто)")
        return
    width = max(len(str(k)) for k, _ in rows[:limit])
    for k, v in rows[:limit]:
        print(f"  {str(k):<{width}}  {v}")
    if len(rows) > limit:
        print(f"  … и ещё {len(rows) - limit}")


def main():
    args = sys.argv[1:]
    if args:
        paths = args
    else:
        root = Path(__file__).parent.parent / "data"
        paths = sorted(str(p) for p in
                       root.glob("*/computer_control/audit.jsonl"))
    recs = list(_load(paths))
    print(f"Записей: {len(recs)} из {len(paths)} файлов")
    if not recs:
        return 0

    # Действия по видам: ok/fail
    kinds: dict = {}
    for r in recs:
        k = str(r.get("kind") or "?")
        if k in ("resolve_fail", "overlay_dismiss"):
            continue
        ok, fail = kinds.get(k, (0, 0))
        kinds[k] = (ok + 1, fail) if r.get("ok") else (ok, fail + 1)
    rows = sorted(
        ((k, f"ok {o} / fail {f} ({_pct(o, o + f)} ok)") for k, (o, f)
         in kinds.items()),
        key=lambda kv: -(kv[1] and kinds[kv[0]][0] + kinds[kv[0]][1]))
    _table("Действия по видам (kind → ok/fail)", rows, col1="kind")

    # Причины неудач резолва × host — куда добавлять рецепты/синонимы
    fails = Counter()
    fail_hosts = Counter()
    for r in recs:
        if r.get("kind") != "resolve_fail":
            continue
        fr = str(r.get("fail_reason") or "?")
        fails[fr] += 1
        fail_hosts[(fr, str(r.get("host") or "?"))] += 1
    _table("Неудачи резолва (fail_reason)", fails.most_common())
    _table("… × host (где систематически не находится)",
           [(f"{fr} @ {h}", n) for (fr, h), n in fail_hosts.most_common()])

    # Пути резолва: сколько стоили LLM/vision (и удачно ли)
    paths: dict = {}
    for r in recs:
        p = r.get("path")
        if not p:
            continue
        o, f = paths.get(str(p), (0, 0))
        paths[str(p)] = (o + 1, f) if r.get("ok") else (o, f + 1)
    rows = sorted(
        ((p, f"ok {o} / fail {f}") for p, (o, f) in paths.items()),
        key=lambda kv: -(paths[kv[0]][0] + paths[kv[0]][1]))
    _table("Пути резолва (path → ok/fail)", rows, col1="path")

    # Closed-loop: неуверенные клики/вводы по видам
    ver = Counter()
    for r in recs:
        v = r.get("verify")
        if v and v != "ok":
            ver[(str(r.get("kind") or "?"), v)] += 1
    _table("Closed-loop: verify != ok (kind × verify)",
           [(f"{k} — {v}", n) for (k, v), n in ver.most_common()])

    # Классы ошибок исполнения
    errs = Counter(str(r.get("error_class"))
                   for r in recs if r.get("error_class"))
    _table("Классы ошибок исполнения", errs.most_common(), col1="error_class")

    # Подсказка по risk_overrides: виды действий, которые всегда ok
    print("\n== Подсказка для risk_overrides ==")
    for k, (o, f) in sorted(kinds.items()):
        if o >= 5 and f == 0:
            print(f"  {k}: {o} успехов без сбоев — кандидат на confirm=false")
        elif f > o and o + f >= 3:
            print(f"  {k}: {o} ok / {f} fail — confirm оставить")
    return 0


if __name__ == "__main__":
    sys.exit(main())

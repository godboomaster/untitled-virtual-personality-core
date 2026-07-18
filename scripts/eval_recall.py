#!/usr/bin/env python3
"""
recall@25 A/B-замеры для query distillation.

Сравнивает baseline (DISTILL_ENABLED=False) против treatment (True)
на двух сетах: английском (eval_gold.json) и русском (eval_gold_ru.json).

Метрики:
  - keyword_hit: доля вопросов, где хотя бы один gold-keyword найден в top-25
  - vol_recall : доля вопросов с gold_volume, где нужный том присутствует в top-25
  - latency_ms : среднее время одного search() вызова

Запуск:
  python3 scripts/eval_recall.py            # полный A/B
  python3 scripts/eval_recall.py --quick    # только русский сет, меньше результатов
"""
import sys
import json
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.features.book_search as bs
from app.features.book_search import BookSearch, detect_volume

DATA = Path(__file__).resolve().parent.parent / "data" / "arrodes"
N_RESULTS = 25


def load_set(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def measure(searcher: BookSearch, eval_set):
    """Прогон одного сета через searcher. Возвращает (per_query, aggregates)."""
    rows = []
    for item in eval_set:
        q = item["query"]
        keywords = [k.lower() for k in item.get("keywords", [])]
        gold_vol = item.get("gold_volume")

        t0 = time.perf_counter()
        try:
            results = searcher.search(q, n_results=N_RESULTS)
        except Exception as e:
            rows.append({
                "id": item["id"], "query": q, "error": str(e),
                "kw_hit": False, "vol_hit": None, "ms": (time.perf_counter() - t0) * 1000,
            })
            continue
        ms = (time.perf_counter() - t0) * 1000

        blob = " ".join((r.get("text", "") or "").lower() for r in results)
        kw_hit = any(kw in blob for kw in keywords) if keywords else True

        vol_hit = None
        if gold_vol is not None:
            present_vols = {r.get("volume") for r in results}
            vol_hit = gold_vol in present_vols

        rows.append({
            "id": item["id"], "query": q, "kw_hit": kw_hit,
            "vol_hit": vol_hit, "ms": ms, "n": len(results),
        })

    # Агрегаты
    ok = [r for r in rows if "error" not in r]
    kw_hits = sum(1 for r in ok if r["kw_hit"])
    vol_rows = [r for r in ok if r["vol_hit"] is not None]
    vol_hits = sum(1 for r in vol_rows if r["vol_hit"])
    agg = {
        "n": len(rows),
        "errors": len(rows) - len(ok),
        "kw_recall": kw_hits / len(ok) if ok else 0.0,
        "vol_recall": (vol_hits / len(vol_rows)) if vol_rows else None,
        "vol_n": len(vol_rows),
        "mean_ms": sum(r["ms"] for r in ok) / len(ok) if ok else 0.0,
    }
    return rows, agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="только русский сет")
    args = ap.parse_args()

    en_set = [] if args.quick else load_set(DATA / "eval_gold.json")
    ru_set = load_set(DATA / "eval_gold_ru.json")

    print("=" * 70)
    print(f"recall@{N_RESULTS} A/B: distill OFF vs ON")
    print(f"  EN set: {len(en_set)} q   RU set: {len(ru_set)} q")
    print("=" * 70)

    searcher = BookSearch()

    def fmt_vol(v):
        return "n/a" if v is None else f"{v:.1%}"

    def run(label):
        print(f"\n--- {label} ---")
        out = {}
        if en_set:
            rows, agg = measure(searcher, en_set)
            out["en"] = agg
            print(f"  EN  kw_recall={agg['kw_recall']:.1%} "
                  f"({sum(1 for r in rows if r['kw_hit'])}/{agg['n']}) "
                  f"vol_recall={fmt_vol(agg['vol_recall'])} "
                  f"({agg['vol_n']} q with gold_vol) "
                  f"mean={agg['mean_ms']:.0f}ms")
        rows, agg = measure(searcher, ru_set)
        out["ru"] = agg
        print(f"  RU  kw_recall={agg['kw_recall']:.1%} "
              f"({sum(1 for r in rows if r['kw_hit'])}/{agg['n']}) "
              f"vol_recall={fmt_vol(agg['vol_recall'])} "
              f"({agg['vol_n']} q with gold_vol) "
              f"mean={agg['mean_ms']:.0f}ms")
        return out

    # --- Baseline: distill OFF ---
    bs.DISTILL_ENABLED = False
    bs._ollama_available = None  # сброс circuit-breaker
    baseline = run("BASELINE (distill OFF)")

    # --- Treatment: distill ON ---
    bs.DISTILL_ENABLED = True
    bs._ollama_available = None  # сброс circuit-breaker
    treatment = run("TREATMENT (distill ON)")

    # --- Дельта ---
    print("\n" + "=" * 70)
    print("DELTA (treatment - baseline), recall@%d" % N_RESULTS)
    print("=" * 70)
    for sset in ("en", "ru") if not args.quick else ("ru",):
        b = baseline.get(sset)
        t = treatment.get(sset)
        if not b or not t:
            continue
        dkw = (t["kw_recall"] - b["kw_recall"]) * 100
        dvol = ((t["vol_recall"] - b["vol_recall"]) * 100
                if b["vol_recall"] is not None and t["vol_recall"] is not None else None)
        dms = t["mean_ms"] - b["mean_ms"]
        volstr = f"{dvol:+.1f}pp" if dvol is not None else "n/a"
        print(f"  {sset.upper()}: kw {dkw:+.1f}pp | vol {volstr} | latency {dms:+.0f}ms")

    print("\nDone. Positive kw/vol delta = distill helps recall; "
          "negative = hurts. Latency delta = overhead per query.")


if __name__ == "__main__":
    main()

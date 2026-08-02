"""Финальный прогон всех наборов qa_eval с замером latency поиска.

Использование:
    python3 qa_eval/final_run.py

Для каждого набора: прогоняет вопросы через BookSearch.search (как в проде),
замеряет wall-time каждого запроса (первый запрос — warmup, не считается),
сохраняет prompts/retrieval в qa_eval/final/<набор>/ и latency.json.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from app.features.book_search import BookSearch
from app.features.book_context import build_context_block
from app.core.persona import PersonaLayer

SETS = [
    ("vol1_r1", "qa_vol1.json"),
    ("vol1_r2", "round2/qa_vol1.json"),
    ("vol1_r4", "round4/qa_vol1.json"),
    ("vol1_r5", "round5/qa_vol1.json"),
    ("vol2_r1", "vol2/qa_vol2.json"),
    ("vol2_r2", "vol2_r2/qa_vol2.json"),
]

EVAL_DIR = Path(__file__).parent
OUT_ROOT = EVAL_DIR / "final"

bs = BookSearch(context="arrodes")
persona = PersonaLayer("arrodes")

# Warmup: загрузка моделей + BM25-индекс — не входит в замер
t0 = time.perf_counter()
bs.search("разогрев: кто такой Клейн Моретти?", n_results=5)
print(f"warmup: {time.perf_counter() - t0:.1f}s", flush=True)

latency = {}
for set_name, qa_rel in SETS:
    qa = json.load(open(EVAL_DIR / qa_rel, encoding="utf-8"))
    out_dir = OUT_ROOT / set_name
    (out_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (out_dir / "retrieval").mkdir(parents=True, exist_ok=True)

    times = []
    for item in qa:
        qid, question = item["id"], item["question"]
        t0 = time.perf_counter()
        fragments = bs.search(question, volume=None, n_results=25, history=None)
        dt = time.perf_counter() - t0
        times.append(dt)

        translated = bs.translate_query(question)
        book_context = build_context_block(
            fragments, original_query=question, translated_query=translated, mode="book"
        )
        messages = persona.prepare_messages(question, None, history=None, book_context=book_context)
        (out_dir / "prompts" / f"{qid}.json").write_text(
            json.dumps(messages, ensure_ascii=False, indent=1), encoding="utf-8")
        retrieval = [
            {"chapter": f.get("chapter"), "volume": f.get("volume"),
             "rerank_score": f.get("rerank_score"), "distance": f.get("distance"),
             "rescued": bool(f.get("_rescued")), "expanded": bool(f.get("_expanded"))}
            for f in fragments
        ]
        (out_dir / "retrieval" / f"{qid}.json").write_text(
            json.dumps(retrieval, ensure_ascii=False, indent=1), encoding="utf-8")
        hit = any(str(f.get("chapter", "")).startswith(f"Chapter {item['chapter']}:") for f in fragments)
        print(f"{set_name}/{qid}: {dt:.1f}s, {len(fragments)} фрагм., "
              f"глава {item['chapter']} {'НАЙДЕНА' if hit else 'не найдена'}", flush=True)

    times_sorted = sorted(times)
    n = len(times)
    latency[set_name] = {
        "n": n,
        "mean": round(sum(times) / n, 2),
        "median": round(times_sorted[n // 2], 2),
        "p95": round(times_sorted[int(n * 0.95) - 1], 2),
        "min": round(times_sorted[0], 2),
        "max": round(times_sorted[-1], 2),
    }
    print(f"== {set_name}: mean={latency[set_name]['mean']}s "
          f"median={latency[set_name]['median']}s p95={latency[set_name]['p95']}s "
          f"max={latency[set_name]['max']}s", flush=True)

all_t = sorted(t for s in latency.values() for t in [s["mean"]])
(OUT_ROOT / "latency.json").write_text(
    json.dumps(latency, ensure_ascii=False, indent=1), encoding="utf-8")
print("latency.json записан")

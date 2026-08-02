"""Harness компонента 2: прогон вопросов через Арродес-пайплайн (retrieval + персона).

Использование:
    python3 qa_eval/run_retrieval.py [qa_json] [out_dir]

Повторяет путь bot_instance.py для book_only-интента:
    fragments = BookSearch.search(query, n_results=25, volume=None, history=None)
    book_context = build_context_block(fragments, ..., mode="book")
    messages = PersonaLayer("arrodes").prepare_messages(query, None, book_context=...)

Для каждого вопроса сохраняет:
    <out_dir>/prompts/qXX.json    — messages (system+user) для LLM
    <out_dir>/retrieval/qXX.json  — метаданные найденных фрагментов (главы/тома/скоры)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from app.features.book_search import BookSearch
from app.features.book_context import build_context_block
from app.core.persona import PersonaLayer

QA_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "qa_vol1.json"
OUT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent
PROMPTS_DIR = OUT_DIR / "prompts"
RETRIEVAL_DIR = OUT_DIR / "retrieval"

qa = json.load(open(QA_PATH, encoding="utf-8"))
bs = BookSearch(context="arrodes")
persona = PersonaLayer("arrodes")

PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
RETRIEVAL_DIR.mkdir(parents=True, exist_ok=True)

for item in qa:
    qid, question = item["id"], item["question"]

    fragments = bs.search(question, volume=None, n_results=25, history=None)
    translated = bs.translate_query(question)
    # Динамический глоссарий — как в bot_instance
    from app.features.glossary_context import build_glossary_block
    _glos = build_glossary_block([question, translated or ""], fragments=fragments)
    book_context = build_context_block(
        fragments, original_query=question, translated_query=translated, mode="book"
    )
    if _glos:
        book_context = _glos + "\n\n" + book_context
    messages = persona.prepare_messages(
        question, None, history=None, book_context=book_context
    )

    (PROMPTS_DIR / f"{qid}.json").write_text(
        json.dumps(messages, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    retrieval = [
        {
            "chapter": f.get("chapter"), "volume": f.get("volume"),
            "rerank_score": f.get("rerank_score"), "distance": f.get("distance"),
        }
        for f in fragments
    ]
    (RETRIEVAL_DIR / f"{qid}.json").write_text(
        json.dumps(retrieval, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    hit = any(str(f.get("chapter", "")).startswith(f"Chapter {item['chapter']}:") for f in fragments)
    print(f"{qid}: {len(fragments)} фрагментов, целевая глава {item['chapter']} "
          f"{'НАЙДЕНА' if hit else 'не найдена'}", flush=True)

print("Готово: prompts/ и retrieval/")

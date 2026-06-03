#!/usr/bin/env python3
"""Test hybrid search: vector + BM25."""
import sys
sys.path.insert(0, '/Users/ghost/Documents/virtual-persona-core')

import re
from app.features.book_search import BookSearch, detect_volume

QUERIES = [
    "Klein Moretti",
    "Klein Moretti том 1",
    "Amon",
    "Amon том 5",
    "Evernight Goddess",
    "Tarot Club",
    "Roselle Gustav",
    "Sefirah Castle",
    "acting method",
    "Error pathway",
    "seq 0",
    "seq 1",
    "seq 2",
    "Sequence 9",
    "Fool pathway",
    "above the sequence",
]

def clean_summary(text):
    """Remove [SUMMARY: ...] prefix."""
    return re.sub(r'^\[SUMMARY:.*?\]\n\n', '', text, flags=re.DOTALL)

searcher = BookSearch()

for q in QUERIES:
    vol = detect_volume(q)
    print(f"\n{'='*60}")
    print(f"Query: {q}")
    if vol is not None:
        print(f"Volume filter: {vol}")
    print('='*60)
    try:
        results = searcher.search(q, n_results=5, volume=vol)
        for i, r in enumerate(results, 1):
            vol_name = r.get('volume_name', '?')
            ch = r.get('chapter', '?')
            dist = r.get('distance', 'N/A')
            bm25 = r.get('bm25_score', 0)
            hybrid = r.get('hybrid_score', 0)
            text = clean_summary(r.get('text', '')[:200])
            print(f"\n  {i}. [{vol_name} / {ch}] (dist: {dist:.3f}, bm25: {bm25:.1f}, hybrid: {hybrid:.3f})")
            print(f"     {text[:150]}...")
        if not results:
            print("  No results")
    except Exception as e:
        import traceback
        print(f"  ERROR: {e}")
        traceback.print_exc()

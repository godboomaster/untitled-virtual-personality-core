#!/usr/bin/env python3
"""Test seq -> sequence translation."""
import sys
sys.path.insert(0, '/Users/ghost/Documents/virtual-persona-core')

from app.features.book_search import _load_ru_to_en, _build_patterns, _translate_query
from pathlib import Path

glossary_path = Path(__file__).parent.parent / "app" / "personas" / "arrodes_glossary.yaml"
ru_to_en = _load_ru_to_en(str(glossary_path))
print(f"Loaded {len(ru_to_en)} mappings")
print(f"'seq' in mappings: {'seq' in ru_to_en}")
if 'seq' in ru_to_en:
    print(f"'seq' -> '{ru_to_en['seq']}'")

patterns = _build_patterns(ru_to_en)
print(f"Built {len(patterns)} patterns")

# Find pattern for seq
for pat, en in patterns:
    if en == 'Sequence':
        print(f"Pattern for Sequence: '{pat}'")

queries = ["seq 0", "seq 1", "seq 2", "Sequence 9"]
for q in queries:
    translated = _translate_query(q, patterns)
    print(f"'{q}' -> '{translated}'")

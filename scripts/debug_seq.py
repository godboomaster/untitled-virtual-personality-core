#!/usr/bin/env python3
import re
import sys
sys.path.insert(0, '/Users/ghost/Documents/virtual-persona-core')

from app.features.book_search import _load_ru_to_en, _build_patterns, _translate_query
from pathlib import Path

glossary_path = Path(__file__).parent.parent / "app" / "personas" / "arrodes_glossary.yaml"
ru_to_en = _load_ru_to_en(str(glossary_path))
patterns = _build_patterns(ru_to_en)

query = "Sequence 9"
result = query

for pat, en in patterns:
    new_result = re.sub(pat, en, result, flags=re.IGNORECASE)
    if new_result != result:
        print(f"MATCH: pattern='{pat}' -> '{en}'")
        print(f"  Before: '{result}'")
        print(f"  After:  '{new_result}'")
        result = new_result

print(f"\nFinal: '{result}'")

#!/usr/bin/env python3
"""Test volume detection with word forms."""
import sys
sys.path.insert(0, '/Users/ghost/Documents/virtual-persona-core')

from app.features.book_search import detect_volume

TESTS = [
    ("Klein Moretti", None),
    ("Klein Moretti том 1", 1),
    ("Klein Moretti в первом томе", 1),
    ("Amon в пятом", 5),
    ("Evernight Goddess в восьмой", 8),
    ("Tarot Club во втором", 2),
    ("Roselle Gustav в третьей книге", 3),
    ("acting method в шестом томе", 6),
    ("Sefirah Castle в седьмом", 7),
    ("что было в первом томе", 1),
    ("расскажи про третий", 3),
    ("в четвёртом томе", 4),
    ("в четвертом", 4),
    ("про вторую книгу", 2),
    ("один том", 1),
    ("два", 2),
    ("три", 3),
    ("четыре", 4),
    ("пять", 5),
    ("шесть", 6),
    ("семь", 7),
    ("восемь", 8),
    ("volume one", 1),
    ("in the first volume", 1),
    ("in the second book", 2),
    ("three", 3),
    ("fifth", 5),
    ("клоун", 1),
    ("арка шут", 8),
    ("side stories", 0),
]

print("Testing volume detection:")
print("=" * 50)
for query, expected in TESTS:
    result = detect_volume(query)
    status = "OK" if result == expected else "FAIL"
    print(f"[{status}] '{query}' -> {result} (expected {expected})")

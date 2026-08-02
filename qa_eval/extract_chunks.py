"""Выгружает сэмпл чанков 1-го тома для генерации QA-пар.

Использование:
    python3 qa_eval/extract_chunks.py [out_dir] [offset] [volume]

Берёт N глав, равномерно распределённых по тому, начиная с offset
(для других раундов берём сдвиг, чтобы главы не повторялись),
из каждой — до 2 чанков (разнесённых внутри главы).
Результат: <out_dir>/chunks/chapter_XXX.txt + <out_dir>/chunks_index.json
"""
import json
import re
import sys
from pathlib import Path

import chromadb

OUT_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
OFFSET = int(sys.argv[2]) if len(sys.argv) > 2 else 0
VOLUME = int(sys.argv[3]) if len(sys.argv) > 3 else 1
N_CHAPTERS = 20
CHUNKS_PER_CHAPTER = 2

client = chromadb.PersistentClient(path="data/arrodes/book")
col = client.get_collection("lord_of_mysteries")
data = col.get(where={"volume": VOLUME}, include=["documents", "metadatas"])

# Группируем по номеру главы (из chapter_num или из строки "Chapter N: ...")
by_chapter = {}
for doc, meta in zip(data["documents"], data["metadatas"]):
    ch_num = meta.get("chapter_num") or 0
    if not ch_num:
        m = re.search(r"Chapter (\d+)", meta.get("chapter", ""))
        ch_num = int(m.group(1)) if m else 0
    if ch_num:
        by_chapter.setdefault(ch_num, []).append((doc, meta.get("chapter", "?")))

chapters = sorted(by_chapter)
print(f"Глав с чанками: {len(chapters)}, диапазон: {chapters[0]}-{chapters[-1]}")

# Равномерный сэмпл глав со сдвигом offset
step = max(1, len(chapters) // N_CHAPTERS)
picked = chapters[OFFSET::step][:N_CHAPTERS]
print(f"Выбрано глав (offset={OFFSET}): {len(picked)} -> {picked}")

chunks_dir = OUT_DIR / "chunks"
chunks_dir.mkdir(parents=True, exist_ok=True)
index = []
for ch in picked:
    chunks = by_chapter[ch]
    # Берём чанки, разнесённые внутри главы (первый и средний)
    positions = sorted({0, len(chunks) // 2})[:CHUNKS_PER_CHAPTER]
    parts = []
    for pos in positions:
        doc, title = chunks[pos]
        parts.append(f"=== {title} (chunk {pos + 1}/{len(chunks)}) ===\n{doc}")
    text = "\n\n".join(parts)
    fname = f"chapter_{ch:03d}.txt"
    (chunks_dir / fname).write_text(text, encoding="utf-8")
    index.append({"chapter": ch, "title": chunks[0][1], "file": fname, "n_chunks_total": len(chunks)})

(OUT_DIR / "chunks_index.json").write_text(
    json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"Записано {len(index)} файлов в {chunks_dir}")

"""
Загрузка Lord of the Mysteries в ChromaDB для Арродеса.
Парсит английские epub, разбивает на чанки с метаданными тома и главы.

Usage:
    cd /Users/ghost/Documents/virtual-persona-core
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 scripts/load_book.py
"""

import os
import re
import json
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

# ─── Настройки ────────────────────────────────────────────

VOLUMES_DIR = "volumes/english"
DB_PATH = "data/arrodes/book"
COLLECTION_NAME = "lord_of_mysteries"
SUMMARIES_PATH = "data/arrodes/summaries.json"
CHUNK_SIZE = 800       # символов на чанк (используется только если CHUNK_BY_CHAPTER = False)
CHUNK_OVERLAP = 200    # перекрытие между чанками (используется только если CHUNK_BY_CHAPTER = False)
CHUNK_BY_CHAPTER = False  # True = каждая глава = один чанк, False = разбивать на куски CHUNK_SIZE
EMBEDDING_MODEL = "intfloat/multilingual-e5-base"

# Маппинг: имя файла -> (том, название)
VOLUME_MAP = {
    "Vol. 1": (1, "Clown"),
    "Vol. 2": (2, "Faceless"),
    "Vol. 3": (3, "Traveler"),
    "Vol. 4": (4, "Undying"),
    "Vol. 5": (5, "Red Priest"),
    "Vol. 6": (6, "Lightseeker"),
    "Vol. 7": (7, "The Hanged Man"),
    "Vol. 8": (8, "Fool"),
    "Side Stories": (0, "Side Stories"),
}


def get_volume_info(filename: str):
    """Извлечь номер тома и название из имени файла."""
    for key, (vol, name) in VOLUME_MAP.items():
        if key in filename:
            return vol, name
    return -1, "Unknown"


def parse_epub(filepath: str):
    """
    Парсит epub, возвращает список:
    [{"text": "...", "volume": N, "volume_name": "...", "chapter": "..."}, ...]
    """
    filename = os.path.basename(filepath)
    vol_num, vol_name = get_volume_info(filename)
    print(f"  Parsing: {filename} -> Volume {vol_num}: {vol_name}")

    book = epub.read_epub(filepath)
    items = [i for i in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)]

    chapters = []
    current_chapter = "Unknown"

    for item in items:
        content = item.get_content().decode("utf-8", errors="ignore")
        soup = BeautifulSoup(content, "html.parser")
        text = soup.get_text().strip()

        if not text or len(text) < 20:
            continue

        # Определяем главу по первой строке
        first_line = text.split("\n")[0].strip()

        # Паттерны: "Chapter 1: Crimson" или "Chapter 1- Crimson"
        ch_match = re.match(r'Chapter\s+(\d+)\s*[:\-–]\s*(.+)', first_line, re.IGNORECASE)
        if ch_match:
            ch_num = ch_match.group(1)
            ch_title = ch_match.group(2).strip()
            current_chapter = f"Chapter {ch_num}: {ch_title}"
            # Убираем строку заголовка из текста
            text = text[len(first_line):].strip()
        elif first_line.lower().startswith("chapter "):
            current_chapter = first_line[:60]
            text = text[len(first_line):].strip()

        if not text or len(text) < 20:
            continue

        chapters.append({
            "text": text,
            "chapter": current_chapter,
            "volume": vol_num,
            "volume_name": vol_name,
        })

    print(f"    -> {len(chapters)} sections extracted")
    return chapters


def chunk_sections(sections, summaries=None):
    """
    Разбивает секции на чанки.
    Если CHUNK_BY_CHAPTER = True — каждая глава = один чанк.
    Иначе — разбивает на куски CHUNK_SIZE с перекрытием CHUNK_OVERLAP.

    Если summaries предоставлены — добавляет summary в начало каждого чанка
    как префикс для улучшения эмбеддинга и поиска.
    """
    if CHUNK_BY_CHAPTER:
        chunks = []
        for sec in sections:
            text = sec["text"].strip()
            if len(text) < 20:
                continue
            chapter = sec["chapter"]
            summary = summaries.get(chapter, "") if summaries else ""
            if summary:
                text = f"[SUMMARY: {summary}]\n\n{text}"
            chunks.append({
                "text": text,
                "volume": sec["volume"],
                "volume_name": sec["volume_name"],
                "chapter": chapter,
            })
        return chunks

    chunks = []
    for sec in sections:
        text = sec["text"]
        vol = sec["volume"]
        vol_name = sec["volume_name"]
        chapter = sec["chapter"]
        summary = summaries.get(chapter, "") if summaries else ""

        if len(text) <= CHUNK_SIZE:
            chunk_text = text
            if summary:
                chunk_text = f"[SUMMARY: {summary}]\n\n{chunk_text}"
            chunks.append({
                "text": chunk_text,
                "volume": vol,
                "volume_name": vol_name,
                "chapter": chapter,
            })
            continue

        # Разбиваем с перекрытием
        start = 0
        while start < len(text):
            end = start + CHUNK_SIZE
            chunk_text = text[start:end]

            if end < len(text):
                last_dot = chunk_text.rfind(".")
                if last_dot > CHUNK_SIZE // 2:
                    end = start + last_dot + 1
                    chunk_text = text[start:end]

            final_text = chunk_text.strip()
            if summary:
                final_text = f"[SUMMARY: {summary}]\n\n{final_text}"

            chunks.append({
                "text": final_text,
                "volume": vol,
                "volume_name": vol_name,
                "chapter": chapter,
            })

            start = end - CHUNK_OVERLAP
            if start <= end - CHUNK_SIZE:
                start = end

    return chunks


def main():
    # 1. Загружаем summaries если есть
    summaries = {}
    if os.path.exists(SUMMARIES_PATH):
        with open(SUMMARIES_PATH, "r", encoding="utf-8") as f:
            summaries = json.load(f)
        print(f"Loaded {len(summaries)} summaries")

    # 2. Собираем все epub
    epub_files = sorted([
        os.path.join(VOLUMES_DIR, f)
        for f in os.listdir(VOLUMES_DIR)
        if f.endswith(".epub")
    ])
    print(f"Found {len(epub_files)} epub files")

    # 3. Парсим все тома
    all_sections = []
    for path in epub_files:
        sections = parse_epub(path)
        all_sections.extend(sections)

    print(f"\nTotal sections: {len(all_sections)}")

    # 4. Чанкуем с summaries
    chunks = chunk_sections(all_sections, summaries)
    print(f"Total chunks: {len(chunks)}")

    # 5. Подключаемся к ChromaDB
    client = chromadb.PersistentClient(path=DB_PATH)
    embedder = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
    # 512 вместо дефолтных 128 токенов: длинный [SUMMARY:]-префикс иначе
    # съедает весь бюджет, и все чанки главы получают одинаковый вектор
    embedder._model.max_seq_length = 512

    # Удаляем старую коллекцию если есть
    try:
        client.delete_collection(COLLECTION_NAME)
        print("Deleted old collection")
    except Exception:
        pass

    collection = client.get_or_create_collection(
        COLLECTION_NAME,
        embedding_function=embedder
    )

    # 6. Загружаем чанками по 200
    batch_size = 200
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        ids = [f"lotm_{i + j}" for j in range(len(batch))]
        documents = [c["text"] for c in batch]
        metadatas = [{
            "volume": c["volume"],
            "volume_name": c["volume_name"],
            "chapter": c["chapter"],
        } for c in batch]

        # e5: документы эмбеддим с префиксом 'passage: ' (модель так обучена)
        embeddings = embedder(["passage: " + d for d in documents])
        collection.add(ids=ids, documents=documents, metadatas=metadatas,
                       embeddings=embeddings)
        print(f"  Batch {i // batch_size + 1}: {len(batch)} chunks inserted")

    final = collection.count()
    print(f"\nDone! Collection '{COLLECTION_NAME}': {final} chunks in {DB_PATH}")

    # 7. Тестовый поиск
    for query in ["Klein Moretti", "Beyonder pathway", "The Fool", "Tarot Club"]:
        results = collection.query(query_texts=[query], n_results=2)
        print(f"\nQuery: '{query}'")
        for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
            print(f"  [{meta['volume_name']}] {meta['chapter']}: {doc[:100]}...")


if __name__ == "__main__":
    main()

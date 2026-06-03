"""
Генерация summary для всех глав Lord of the Mysteries.
Сохраняет результат в summaries.json — потом используется при индексации.

Usage:
    cd /Users/ghost/Documents/virtual-persona-core
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 scripts/generate_summaries.py
"""

import os
import re
import json
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from pathlib import Path
from typing import Dict, List

# ─── Настройки ────────────────────────────────────────────

VOLUMES_DIR = "volumes/english"
SUMMARIES_PATH = "data/arrodes/summaries.json"
BATCH_SIZE = 100  # сколько глав обрабатывать за один запуск

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
    for key, (vol, name) in VOLUME_MAP.items():
        if key in filename:
            return vol, name
    return -1, "Unknown"


def parse_epub(filepath: str) -> List[Dict]:
    """Парсит epub, возвращает список глав с текстом."""
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

        first_line = text.split("\n")[0].strip()

        ch_match = re.match(r'Chapter\s+(\d+)\s*[:\-–]\s*(.+)', first_line, re.IGNORECASE)
        if ch_match:
            ch_num = ch_match.group(1)
            ch_title = ch_match.group(2).strip()
            current_chapter = f"Chapter {ch_num}: {ch_title}"
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
            "chapter_num": int(ch_num) if 'ch_num' in dir() else 0,
        })

    print(f"    -> {len(chapters)} chapters")
    return chapters


def extract_names_and_keywords(text: str) -> str:
    """
    Экстрактивный summary: первые 3 предложения + имена собственные.
    Быстрая альтернатива LLM — используется если LLM недоступен.
    """
    # Первые 3 предложения
    sentences = re.split(r'(?<=[.!?])\s+', text[:1500])
    first_sentences = ' '.join(sentences[:3]).strip()

    # Имена собственные (простая эвристика: слова с заглавной буквы)
    words = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text[:3000])
    # Фильтруем частые слова
    stop_words = {"The", "A", "An", "This", "That", "These", "Those", "I", "You", "He", "She", "It", "We", "They",
                  "His", "Her", "Its", "Their", "My", "Your", "Our", "And", "But", "Or", "Nor", "For", "Yet", "So",
                  "In", "On", "At", "To", "From", "By", "With", "About", "Into", "Through", "During", "Before", "After",
                  "Above", "Below", "Between", "Under", "Again", "Further", "Then", "Once", "Here", "There", "When",
                  "Where", "Why", "How", "All", "Any", "Both", "Each", "Few", "More", "Most", "Other", "Some", "Such",
                  "No", "Not", "Only", "Own", "Same", "Than", "Too", "Very", "Can", "Will", "Just", "Should", "Now",
                  "What", "Which", "Who", "Whom", "Whose", "Would", "Could", "May", "Might", "Must", "Shall", "Had",
                  "Has", "Have", "Having", "Do", "Does", "Did", "Doing", "Done", "Be", "Been", "Being", "Am", "Is", "Are",
                  "Was", "Were", "Said", "Say", "Says", "One", "Two", "Three", "First", "Second", "Last", "Good", "New",
                  "Old", "Great", "High", "Small", "Different", "Large", "Next", "Early", "Young", "Important", "Few",
                  "Public", "Bad", "Same", "Able"}
    names = []
    seen = set()
    for w in words:
        if w not in stop_words and w not in seen and len(w) > 2:
            names.append(w)
            seen.add(w)

    names_str = ", ".join(names[:15])  # топ-15 имён

    return f"{first_sentences}\n\nKey names: {names_str}"


def generate_llm_summary(text: str, chapter_title: str, router) -> str:
    """Генерирует summary через LLM."""
    prompt = f"""Summarize this chapter in 2-3 sentences. List key characters, locations, and events.

Chapter: {chapter_title}

Text (first 2000 chars):
{text[:2000]}

Format:
Summary: <2-3 sentences>
Characters: <names>
Locations: <places>
Events: <key events>"""

    try:
        messages = [{"role": "user", "content": prompt}]
        response = router.get_response(messages, temperature=0.3, max_tokens=300)
        return response.strip()
    except Exception as e:
        print(f"    LLM error: {e}")
        return None


def main():
    # Загружаем существующие summary
    summaries = {}
    if os.path.exists(SUMMARIES_PATH):
        with open(SUMMARIES_PATH, "r", encoding="utf-8") as f:
            summaries = json.load(f)
        print(f"Loaded {len(summaries)} existing summaries")

    # Парсим все главы
    epub_files = sorted([
        os.path.join(VOLUMES_DIR, f)
        for f in os.listdir(VOLUMES_DIR)
        if f.endswith(".epub")
    ])

    all_chapters = []
    for path in epub_files:
        chapters = parse_epub(path)
        all_chapters.extend(chapters)

    print(f"\nTotal chapters: {len(all_chapters)}")

    # Определяем сколько осталось
    remaining = [ch for ch in all_chapters if ch["chapter"] not in summaries]
    print(f"Remaining to summarize: {len(remaining)}")

    if not remaining:
        print("All summaries already generated!")
        return

    # Инициализируем router
    import sys
    sys.path.insert(0, '/Users/ghost/Documents/virtual-persona-core')
    from app.core.router import ModelRouter
    router = ModelRouter()

    # Обрабатываем пачками
    batch = remaining[:BATCH_SIZE]
    print(f"\nProcessing batch of {len(batch)} chapters...")

    for i, ch in enumerate(batch, 1):
        key = ch["chapter"]
        print(f"  [{i}/{len(batch)}] {key}")

        # Сначала пробуем LLM
        summary = generate_llm_summary(ch["text"], key, router)

        # Fallback на экстрактивный
        if not summary:
            summary = extract_names_and_keywords(ch["text"])
            print(f"    -> Using extractive summary")
        else:
            print(f"    -> LLM summary generated")

        summaries[key] = summary

        # Сохраняем после каждой главы (чтобы не потерять прогресс)
        with open(SUMMARIES_PATH, "w", encoding="utf-8") as f:
            json.dump(summaries, f, ensure_ascii=False, indent=2)

    print(f"\nDone! Total summaries: {len(summaries)}/{len(all_chapters)}")
    print(f"Run again to process next batch.")


if __name__ == "__main__":
    main()

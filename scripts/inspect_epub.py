"""Скрипт для анализа структуры epub томов."""
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

path = "volumes/english/Lord of the Mysteries - Vol. 1 - Clown.epub"
book = epub.read_epub(path)
items = [i for i in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)]
print(f"Total items: {len(items)}")

for i, item in enumerate(items[:20]):
    content = item.get_content().decode("utf-8", errors="ignore")
    soup = BeautifulSoup(content, "html.parser")
    text = soup.get_text().strip()
    preview = text[:200].replace("\n", " | ")
    print(f"\n--- {i}: {item.get_name()} ---")
    print(f"  {preview}")

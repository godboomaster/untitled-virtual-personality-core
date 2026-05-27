"""
Скрипт миграции эмбеддингов на мультиязычную модель.

Запустить один раз после замены DefaultEmbeddingFunction на мультиязычную
в memory.py. Скрипт пересчитает все векторы в базах STM и LTM.

Использование:
    python -m app.scripts.migrate_embeddings
"""

import os
import sys

# Добавляем корень проекта в path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

# Новая мультиязычная модель (понимает 50+ языков, включая русский)
NEW_EMBEDDER = SentenceTransformerEmbeddingFunction(
    model_name="paraphrase-multilingual-MiniLM-L12-v2"
)

# Все контексты (персоны)
CONTEXTS = ["connor", "arrodes", "verso", "assistant", "gradio", "default", "tg"]

# Поддерживаемые коллекции
COLLECTIONS = ["short_term_memory", "long_term_memory", "file_documents", "file_full_docs"]


def get_db_paths(context: str) -> dict:
    """Пути к базам для контекста."""
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", context)
    return {
        "stm": os.path.join(base, "stm"),
        "ltm": os.path.join(base, "ltm"),
        "files": os.path.join(base, "files"),
    }


def migrate_collection(db_path: str, collection_name: str):
    """Пересчитать эмбеддинги в одной коллекции."""
    if not os.path.exists(db_path):
        print(f"  [SKIP] Путь не существует: {db_path}")
        return

    client = chromadb.PersistentClient(path=db_path)

    # Проверяем есть ли коллекция
    existing_names = [c.name for c in client.list_collections()]
    if collection_name not in existing_names:
        print(f"  [SKIP] Коллекция {collection_name} не найдена")
        return

    # 1. Читаем все данные
    old_collection = client.get_collection(collection_name)
    count = old_collection.count()

    if count == 0:
        print(f"  [SKIP] Коллекция пуста (0 записей)")
        client.delete_collection(collection_name)
        new_collection = client.get_or_create_collection(
            collection_name,
            embedding_function=NEW_EMBEDDER
        )
        return

    print(f"  [READ] {count} записей из {collection_name}")
    results = old_collection.get(include=["documents", "metadatas", "embeddings"])

    ids = results["ids"]
    documents = results["documents"]
    metadatas = results["metadatas"] if results["metadatas"] else [{}] * count

    # 2. Удаляем старую коллекцию
    client.delete_collection(collection_name)
    print(f"  [DEL] Старая коллекция удалена")

    # 3. Создаём новую с новым эмбеддером
    new_collection = client.get_or_create_collection(
        collection_name,
        embedding_function=NEW_EMBEDDER
    )
    print(f"  [NEW] Коллекция создана с multilingual embedder")

    # 4. Записываем все данные обратно (эмбеддинги пересчитаются автоматически)
    # ChromaDB может не принять весь батч сразу — разбиваем по 100
    batch_size = 100
    for i in range(0, count, batch_size):
        batch_ids = ids[i:i + batch_size]
        batch_docs = documents[i:i + batch_size]
        batch_metas = metadatas[i:i + batch_size]

        new_collection.add(
            ids=batch_ids,
            documents=batch_docs,
            metadatas=batch_metas
        )

    print(f"  [OK] {count} записей пересохранено с новыми эмбеддингами")


def main():
    print("=" * 60)
    print("Миграция эмбеддингов → paraphrase-multilingual-MiniLM-L12-v2")
    print("=" * 60)
    print()
    print("При первом запуске скачается модель (~470MB).")
    print()

    total_migrated = 0

    for context in CONTEXTS:
        paths = get_db_paths(context)
        print(f"\n[{context.upper()}]")

        for key, collection_name in [
            ("stm", "short_term_memory"),
            ("ltm", "long_term_memory"),
            ("files", "file_documents"),
            ("files", "file_full_docs"),
        ]:
            db_path = paths[key]
            print(f"\n  {collection_name} ({db_path})")
            try:
                migrate_collection(db_path, collection_name)
                total_migrated += 1
            except Exception as e:
                print(f"  [ERROR] {e}")
                import traceback
                traceback.print_exc()

    print(f"\n{'=' * 60}")
    print(f"Готово. Обработано коллекций: {total_migrated}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()

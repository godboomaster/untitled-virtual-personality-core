#!/usr/bin/env python3
"""
Восстановление памяти из JSON-дампов при старте.
Запускается автоматически перед стартом бота.
Загружает данные только если целевая коллекция пуста.

Использование:
    python -m app.restore_memory
    # или автоматически при старте telegram_bot.py
"""

import os
import sys
import json
import glob
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from app.core.config import Config, get_db_paths

logger = logging.getLogger(__name__)


def _build_restore_map() -> dict:
    """Строит маппинг динамически на основе persona-папок + gradio."""
    contexts = ["connor", "arrodes", "verso", "assistant", "gradio", "default"]
    mapping = {}
    for ctx in contexts:
        paths = get_db_paths(ctx)
        mapping[f"{ctx}_stm"] = (paths["stm"], "short_term_memory")
        mapping[f"{ctx}_ltm"] = (paths["ltm"], "long_term_memory")
        mapping[f"{ctx}_files"] = (paths["files"], "file_documents")
    return mapping

RESTORE_MAP = _build_restore_map()


def find_latest_export(export_dir: str) -> dict:
    # Находит последние JSON-файлы для каждой базы.
    if not os.path.isdir(export_dir):
        return {}

    latest = {}
    for db_name in RESTORE_MAP:
        # Ищем файлы вида: tg_ltm_20260503_221535.json
        pattern = os.path.join(export_dir, f"{db_name}_*.json")
        files = sorted(glob.glob(pattern))
        if files:
            latest[db_name] = files[-1]  # Последний по алфавиту = самый свежий

    return latest


def restore_collection(db_path: str, collection_name: str, json_path: str) -> int:
    """
    Загружает данные из JSON в коллекцию ChromaDB.
    Возвращает количество загруженных документов.
    Пропускает если коллекция уже не пуста.
    """
    client = chromadb.PersistentClient(path=db_path)
    embedder = SentenceTransformerEmbeddingFunction(
        model_name="paraphrase-multilingual-MiniLM-L12-v2"
    )
    collection = client.get_or_create_collection(
        collection_name,
        embedding_function=embedder
    )

    # Не трогаем если уже есть данные
    if collection.count() > 0:
        logger.info(f"  [Restore] {collection_name} уже содержит {collection.count()} записей, пропускаем")
        return 0

    # Читаем JSON
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    documents = data.get("documents", [])
    if not documents:
        logger.info(f"  [Restore] {json_path} пуст, пропускаем")
        return 0

    # Подготавливаем данные для batch-вставки
    ids = []
    docs = []
    metas = []

    for item in documents:
        doc_id = item.get("id")
        doc_text = item.get("document")
        metadata = item.get("metadata", {})

        if not doc_id or not doc_text:
            continue

        # ChromaDB не принимает None в metadata — конвертируем
        clean_meta = {}
        for k, v in metadata.items():
            if v is not None:
                clean_meta[k] = v

        ids.append(doc_id)
        docs.append(doc_text)
        metas.append(clean_meta)

    if not ids:
        return 0

    # Batch insert (ChromaDB лимит ~5000 за раз)
    batch_size = 5000
    for i in range(0, len(ids), batch_size):
        batch_ids = ids[i:i + batch_size]
        batch_docs = docs[i:i + batch_size]
        batch_metas = metas[i:i + batch_size]
        collection.add(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)

    logger.info(f"  [Restore] {collection_name}: загружено {len(ids)} записей из {os.path.basename(json_path)}")
    return len(ids)


def restore_all(export_dir: str = None) -> dict:
    """
    Восстанавливает все базы из последних дампов.
    Возвращает словарь {db_name: количество_загруженных}.
    """
    if export_dir is None:
        export_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "memory_export"
        )

    if not os.path.isdir(export_dir):
        logger.info(f"  [Restore] Директория {export_dir} не найдена, пропускаем")
        return {}

    latest_files = find_latest_export(export_dir)
    if not latest_files:
        logger.info(f"  [Restore] Нет файлов для восстановления в {export_dir}")
        return {}

    logger.info(f"  [Restore] Найдено {len(latest_files)} баз для восстановления")

    results = {}
    for db_name, json_path in latest_files.items():
        if db_name not in RESTORE_MAP:
            continue

        db_path, collection_name = RESTORE_MAP[db_name]
        try:
            count = restore_collection(db_path, collection_name, json_path)
            results[db_name] = count
        except Exception as e:
            logger.error(f"  [Restore] Ошибка при восстановлении {db_name}: {e}")
            results[db_name] = -1

    total = sum(v for v in results.values() if v > 0)
    logger.info(f"  [Restore] Итого восстановлено: {total} записей")
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("Восстановление памяти из memory_export/...")
    results = restore_all()
    if results:
        for name, count in results.items():
            status = f"{count} записей" if count >= 0 else "ОШИБКА"
            print(f"  {name}: {status}")
    else:
        print("  Нечего восстанавливать.")
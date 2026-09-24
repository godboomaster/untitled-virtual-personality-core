#!/usr/bin/env python3
"""
Миграция STM: загрузить сообщения из JSON-экспорта в ChromaDB.
Останавливает бота перед запуском!

Здесь же — общий код для обоих миграционных скриптов (migrate_stm.py и
migrate_stm_500.py), чтобы порядок операций был один и правильный: опечатка
в пути, недокачанный/битый JSON или неожиданный формат записи не должны
оставить STM пустой и невосстановимой. Обязательный для обоих скриптов
порядок, реализованный ниже:

    1. прочитать и провалидировать файл импорта (до единого delete);
    2. сделать бэкап текущей коллекции на диск (формат memory_export/,
       его понимает app/features/restore_memory.py);
    3. заменить содержимое, а при ошибке вставки — откатиться на снапшот.

Usage:
    cd /Users/user/Documents/virtual-persona-core
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 migrate_stm.py
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chromadb
from app.core.atomic_io import atomic_write_json
from app.core.chroma_space import COLLECTION_NAMES, open_collection
from app.core.st_embedder import create_st_embedder

DEFAULT_DB_PATH = "data/connor/stm"
DEFAULT_IMPORT_FILE = "/tmp/vp_stm_import.json"
# Бэкапы кладём туда, откуда их умеет поднимать restore_memory.restore_all()
BACKUP_DIR = "memory_export"
BATCH_SIZE = 100


def load_import_file(path: str) -> List[Dict]:
    """Прочитать и провалидировать файл импорта ДО любых изменений в базе.

    Понимает оба формата записей:
      * вложенный  — {"id": ..., "document": ..., "metadata": {...}}
      * плоский    — {"chroma_id": ..., "document": ..., "role": ...,
                      "timestamp": ..., "chat_id": ...}

    Возвращает нормализованные записи ``{"id", "document", "metadata"}``.
    При любой проблеме (нет файла, не JSON, не список, пусто, запись без
    id/текста, дубли id) — ValueError: вызывающий обязан прерваться, ничего
    в базе не тронув.
    """
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"файл импорта не найден: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"файл импорта {p} не читается как JSON: {e}") from e
    if isinstance(raw, dict) and isinstance(raw.get("documents"), list):
        raw = raw["documents"]  # формат дампа memory_export
    if not isinstance(raw, list):
        raise ValueError(f"файл импорта {p}: ожидался список записей, "
                         f"получено {type(raw).__name__}")
    if not raw:
        raise ValueError(f"файл импорта {p} пуст — замена отменена")

    records: List[Dict] = []
    seen_ids = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"запись #{i}: ожидался объект, получено "
                             f"{type(item).__name__}")
        doc_id = item.get("id") or item.get("chroma_id")
        document = item.get("document") or item.get("content")
        if not doc_id or not document:
            raise ValueError(f"запись #{i}: нет id ({doc_id!r}) или текста")
        doc_id = str(doc_id)
        if doc_id in seen_ids:
            raise ValueError(f"запись #{i}: повторный id {doc_id} — "
                             f"часть сообщений потерялась бы при вставке")
        seen_ids.add(doc_id)

        src = item.get("metadata") if isinstance(item.get("metadata"), dict) else item
        meta = {
            "role": src.get("role", "user"),
            "timestamp": src.get("timestamp", 0),
            "chat_id": str(src.get("chat_id") or src.get("user_id") or "default"),
        }
        # ChromaDB не принимает None в metadata — только непустые значения
        for key in ("user_name", "sender_id"):
            if src.get(key):
                meta[key] = src[key]
        records.append({"id": doc_id, "document": document, "metadata": meta})

    return records


def dump_collection(collection) -> List[Dict]:
    # Снапшот коллекции в памяти (ids + документы + метаданные)
    got = collection.get(include=["documents", "metadatas"])
    ids = got.get("ids") or []
    documents = got.get("documents") or []
    metadatas = got.get("metadatas") or [{}] * len(ids)
    return [
        {"id": rid, "document": doc, "metadata": meta or {}}
        for rid, doc, meta in zip(ids, documents, metadatas)
    ]


def backup_collection(snapshot: List[Dict], db_path: str,
                      kind: str = "stm", backup_dir: str = BACKUP_DIR) -> Optional[Path]:
    """Сохранить снапшот на диск ДО замены. Формат — как у memory_export,
    так что дамп поднимается штатным restore_memory.restore_all().

    Пустая коллекция — бэкапить нечего (None). Ошибку записи НЕ глушим:
    без бэкапа замену делать нельзя.
    """
    if not snapshot:
        return None
    context = Path(db_path).resolve().parent.name  # data/<context>/stm → <context>
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = Path(backup_dir) / f"{context}_{kind}_{stamp}.json"
    atomic_write_json(dest, {
        "count": len(snapshot),
        "documents": snapshot,
    })
    return dest


def replace_collection(collection, records: List[Dict],
                       snapshot: List[Dict], batch_size: int = BATCH_SIZE) -> int:
    """Заменить содержимое коллекции записями records.

    Chroma не умеет транзакций, поэтому «атомарность» здесь такая: к моменту
    удаления снапшот уже лежит на диске, а при ошибке на вставке делается
    откат из снапшота в памяти. Возвращает число вставленных записей.
    """
    old_ids = [row["id"] for row in snapshot]
    try:
        if old_ids:
            for i in range(0, len(old_ids), batch_size):
                collection.delete(ids=old_ids[i:i + batch_size])
        inserted = _add_batches(collection, records, batch_size)
    except Exception as e:
        print(f"  ОШИБКА вставки ({e}) — откатываю коллекцию на снапшот")
        try:
            existing = collection.get().get("ids") or []
            if existing:
                collection.delete(ids=existing)
            _add_batches(collection, snapshot, batch_size)
            print(f"  Откат выполнен: вернулось {len(snapshot)} записей")
        except Exception as e2:
            print(f"  ОТКАТ НЕ УДАЛСЯ: {e2}. Данные — в файле бэкапа, "
                  f"поднять их можно через app/features/restore_memory.py")
        raise
    return inserted


def _add_batches(collection, records: List[Dict], batch_size: int) -> int:
    added = 0
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        collection.add(
            ids=[r["id"] for r in batch],
            documents=[r["document"] for r in batch],
            metadatas=[r["metadata"] for r in batch],
        )
        added += len(batch)
        print(f"  Батч {i // batch_size + 1}: вставлено {len(batch)}")
    return added


def migrate_stm(db_path: str = DEFAULT_DB_PATH,
                import_file: str = DEFAULT_IMPORT_FILE) -> Tuple[int, Optional[Path]]:
    # Полный безопасный цикл миграции STM. Возвращает (вставлено, путь_бэкапа)
    # 1. Импорт читаем и валидируем ПЕРВЫМ делом — до подключения к базе
    records = load_import_file(import_file)
    print(f"Импорт {import_file}: {len(records)} записей прошли валидацию")

    client = chromadb.PersistentClient(path=db_path)
    collection = open_collection(
        client, COLLECTION_NAMES["stm"], embedding_function=create_st_embedder())
    print(f"В ChromaDB сейчас: {collection.count()} записей")

    # 2. Бэкап текущего содержимого
    snapshot = dump_collection(collection)
    backup_path = backup_collection(snapshot, db_path)
    if backup_path:
        print(f"Бэкап: {backup_path} ({len(snapshot)} записей)")
    else:
        print("Бэкап не нужен: коллекция пуста")

    # 3. Замена
    inserted = replace_collection(collection, records, snapshot)
    print(f"\nИтог в ChromaDB: {collection.count()} записей (вставлено {inserted})")

    # Проверка векторного поиска по реально импортированному чату
    chat_id = records[0]["metadata"]["chat_id"]
    test = collection.query(query_texts=["привет"], n_results=3,
                            where={"chat_id": chat_id})
    docs = test["documents"][0] if test.get("documents") else []
    print(f"Проверка: поиск 'привет' в чате {chat_id} -> {len(docs)} результатов")
    for doc in docs:
        print(f"  - {doc[:80]}...")
    return inserted, backup_path


if __name__ == "__main__":
    import_file = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IMPORT_FILE
    db_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DB_PATH
    try:
        migrate_stm(db_path=db_path, import_file=import_file)
    except ValueError as e:
        print(f"Миграция отменена (база не тронута): {e}")
        sys.exit(1)
    print("\nГотово. Буферы чатов загрузятся из базы при старте бота.")

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
import re
import sys
import json
import logging
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import chromadb
from app.core.chroma_space import COLLECTION_NAMES, open_collection
from app.core.config import Config, get_db_paths
from app.core.st_embedder import create_st_embedder

logger = logging.getLogger(__name__)

# Виды баз, которые вообще умеем восстанавливать (имена коллекций — в
# app/core/chroma_space.COLLECTION_NAMES, одно определение на проект)
RESTORE_KINDS = ("stm", "ltm", "files")

# Имя дампа: {контекст}_{вид}_{метка времени}.json, например
# api_arrodes_ltm_20260503_221535.json. Контекст — нежадно, чтобы вид
# отделился по первому же вхождению _stm_/_ltm_/_files_.
_EXPORT_RE = re.compile(
    r"^(?P<ctx>.+?)_(?P<kind>" + "|".join(RESTORE_KINDS) + r")_(?P<stamp>.+)\.json$")

# Контекст попадает в путь к базе (data/{ctx}/stm), а берётся из имени файла
# в каталоге дампов — пускаем только безопасные имена: буквы/цифры (в том
# числе не латиница), _ . -, без ".." и без разделителей путей
_CTX_RE = re.compile(r"^(?!.*\.\.)\w[\w.\-]*$", re.UNICODE)


def find_latest_export(export_dir: str,
                       contexts: Optional[List[str]] = None) -> Dict[str, Tuple[str, str, str]]:
    """Последний дамп по каждой (контекст, вид) из фактических файлов каталога.

    Раньше вместо этого был жёстко прошитый RESTORE_MAP на пять персон
    ("connor", "arrodes", "verso", "assistant", "default"): дампы веб-персон
    (data/api_*) и любой новой персоны молча не восстанавливались, а
    переименование персоны требовало правки константы. Теперь набор целей
    вычисляется из имён файлов в каталоге дампов, а путь к базе — из конфига
    (get_db_paths), так что достаточно положить дамп рядом с остальными.

    Args:
        export_dir: каталог дампов (memory_export/).
        contexts: если задан — восстанавливать только эти контексты.

    Returns:
        {"{ctx}_{kind}": (путь_к_json, путь_к_базе, имя_коллекции)}
    """
    if not os.path.isdir(export_dir):
        return {}

    allowed = set(contexts) if contexts else None
    best: Dict[str, Tuple[str, str]] = {}  # db_name → (метка времени, файл)
    for name in sorted(os.listdir(export_dir)):
        m = _EXPORT_RE.match(name)
        if not m:
            continue
        ctx, kind, stamp = m.group("ctx"), m.group("kind"), m.group("stamp")
        if not _CTX_RE.match(ctx):
            logger.warning(f"  [Restore] Пропускаю {name}: подозрительное имя контекста")
            continue
        if allowed is not None and ctx not in allowed:
            continue
        db_name = f"{ctx}_{kind}"
        # Метка времени в имени — YYYYmmdd_HHMMSS, сравнение строк = по времени
        if db_name not in best or stamp > best[db_name][0]:
            best[db_name] = (stamp, os.path.join(export_dir, name))

    latest: Dict[str, Tuple[str, str, str]] = {}
    for db_name, (_stamp, path) in best.items():
        ctx, _, kind = db_name.rpartition("_")
        latest[db_name] = (path, get_db_paths(ctx)[kind], COLLECTION_NAMES[kind])
    return latest


def restore_collection(db_path: str, collection_name: str, json_path: str) -> int:
    """
    Загружает данные из JSON в коллекцию ChromaDB.
    Возвращает количество загруженных документов.
    Пропускает если коллекция уже не пуста.
    """
    client = chromadb.PersistentClient(path=db_path)
    # Эмбеддер и метрика — как у рабочих коллекций (единая точка открытия),
    # иначе восстановленная база получала дефолтную l2 и «забудь про X»
    # переставало находить факты
    collection = open_collection(
        client, collection_name, embedding_function=create_st_embedder())

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


def restore_all(export_dir: str = None, contexts: List[str] = None) -> dict:
    """
    Восстанавливает все базы из последних дампов.
    Возвращает словарь {db_name: количество_загруженных}.

    contexts — необязательный фильтр по контекстам (по умолчанию — все, чьи
    дампы найдены в каталоге).
    """
    if export_dir is None:
        # memory_export/ лежит в корне проекта (app/features/ -> app/ -> корень)
        export_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "memory_export"
        )

    if not os.path.isdir(export_dir):
        logger.info(f"  [Restore] Директория {export_dir} не найдена, пропускаем")
        return {}

    latest_files = find_latest_export(export_dir, contexts=contexts)
    if not latest_files:
        logger.info(f"  [Restore] Нет файлов для восстановления в {export_dir}")
        return {}

    logger.info(f"  [Restore] Найдено {len(latest_files)} баз для восстановления")

    results = {}
    for db_name, (json_path, db_path, collection_name) in latest_files.items():
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
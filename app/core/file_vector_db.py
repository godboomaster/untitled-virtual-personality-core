"""
Векторная база данных для временного хранения файлов.
Хранит максимум 3 документа на пользователя.
Полный текст хранится отдельно для пересказа/анализа целиком.
"""

import chromadb
import functools
from app.core.bounded_cache import BoundedCache
from app.core.chroma_space import COLLECTION_NAMES, open_collection
from app.core.config import Config, get_db_paths
from app.core.st_embedder import create_st_embedder
import logging
import threading
import time

logger = logging.getLogger(__name__)

MAX_DOCS_DEFAULT = 3
# Сколько пользователей помнить в _loaded_docs (последний загруженный файл):
# без предела словарь рос бы на каждого пользователя за всё время процесса
MAX_LOADED_DOCS_USERS = 500


def _locked(method):
    """Выполнить метод под ``self._lock`` — одним RLock на инстанс.

    Один декоратор на все операции вместо ручного ``with`` в каждом методе —
    так новый метод не может забыть взять лок (см. комментарий в ``__init__``
    про гонки без него). RLock — вложенные вызовы (``add_file`` →
    ``_delete_full_doc``) берут тот же лок повторно.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class FileVectorDB:
    def __init__(self, db_path: str = None, context: str = "default", max_docs: int = None):
        """
        Инициализация файловой БД.

        Args:
            db_path: Путь к базе данных. Если None, выбирается по context.
            context: Контекст использования — "tg", "api_{persona}" или "default".
            max_docs: Максимальное количество файлов на пользователя. Если None — дефолт 3.
        """
        self.max_docs = max_docs or MAX_DOCS_DEFAULT
        if db_path is None:
            db_path = get_db_paths(context)["files"]

        self.context = context
        self.client = chromadb.PersistentClient(path=db_path)
        self.embedder = create_st_embedder()
        # Метрика коллекций задана явно и в одном месте (chroma_space)
        self.collection = open_collection(
            self.client, COLLECTION_NAMES["files"],
            embedding_function=self.embedder)
        # Коллекция для полных текстов документов
        self.full_docs = open_collection(
            self.client, COLLECTION_NAMES["full_docs"],
            embedding_function=self.embedder)
        self._loaded_docs = BoundedCache(max_entries=MAX_LOADED_DOCS_USERS)  # user_id -> filename
        # Все операции здесь — read-modify-write по двум коллекциям
        # (get список → delete лишнего → add нового). Без лока параллельные
        # add_file/remove_file/reset из разных потоков (например, загрузка
        # файла в мессенджере и запрос из веб-API) работали бы по устаревшему
        # списку ids: один поток удалял бы чанки, которые другой только что
        # записал, а лимит max_docs обходился бы, оставляя в базе документы
        # сверх лимита. RLock — публичные методы вызывают приватные, которые
        # тоже берут лок.
        self._lock = threading.RLock()

    @_locked
    def add_file(self, user_id: str, filename: str, content: str):
        # Добавляет файл; при совпадении имени заменяет прежнюю версию, при
        # превышении лимита удаляет самый старый документ пользователя.
        user_docs = self.collection.get(where={"user_id": user_id})
        if user_docs and user_docs["ids"]:
            existing_ids = [
                eid for eid, meta in zip(user_docs["ids"], user_docs.get("metadatas", []))
                if isinstance(meta, dict) and meta.get("filename") == filename
            ]
            if existing_ids:
                self.collection.delete(ids=existing_ids)
                # Удаляем и старые части полного текста — иначе при более
                # короткой новой версии хвост старой склеится в конец
                self._delete_full_doc(user_id, filename)
                logger.info(f"  [FileDB] Обновлён файл {filename} для {user_id}")

        # Лимит считаем по ДОКУМЕНТАМ (уникальным filename), а не по чанкам.
        # Самый старый документ — с наименьшим timestamp среди его чанков.
        docs_by_name = {}
        for meta in (user_docs.get("metadatas") or []):
            if isinstance(meta, dict) and "filename" in meta:
                fn = meta["filename"]
                if fn == filename:
                    continue  # старая версия этого файла уже удалена выше
                ts = meta.get("timestamp", 0)
                if fn not in docs_by_name or ts < docs_by_name[fn]:
                    docs_by_name[fn] = ts

        while len(docs_by_name) >= self.max_docs:
            oldest_filename = min(docs_by_name, key=docs_by_name.get)
            chunk_ids = [
                eid for eid, meta in zip(user_docs["ids"], user_docs.get("metadatas", []))
                if isinstance(meta, dict) and meta.get("filename") == oldest_filename
            ]
            if chunk_ids:
                self.collection.delete(ids=chunk_ids)
            self._delete_full_doc(user_id, oldest_filename)
            del docs_by_name[oldest_filename]
            logger.info(f"  [FileDB] Удалён старый документ {oldest_filename} для {user_id}")

        # Сохраняем полный текст отдельно (по частям если длинный)
        doc_id = f"{user_id}_{filename}"
        max_part_len = 50000
        parts = [content[i:i + max_part_len] for i in range(0, len(content), max_part_len)]
        total_parts = len(parts)

        part_ids = []
        part_docs = []
        part_metas = []
        for pi, part in enumerate(parts):
            part_ids.append(f"{doc_id}_part{pi}")
            part_docs.append(part)
            part_metas.append({
                "user_id": user_id,
                "filename": filename,
                "part": pi,
                "total_parts": total_parts,
                "total_chars": len(content),
                "timestamp": int(time.time() * 1000)
            })

        self.full_docs.upsert(ids=part_ids, documents=part_docs, metadatas=part_metas)

        # Добавляем чанки для поиска
        chunks = self._split_content(content)
        for i, chunk in enumerate(chunks):
            self.collection.add(
                ids=[f"{user_id}_{filename}_{i}"],
                documents=[chunk],
                metadatas=[{
                    "user_id": user_id,
                    "filename": filename,
                    "chunk": i,
                    "total_chunks": len(chunks),
                    "timestamp": int(time.time() * 1000)
                }]
            )

        self._loaded_docs[user_id] = filename
        logger.info(f"  [FileDB] Добавлен {filename} для {user_id} ({len(chunks)} чанков, полный текст {len(content)} символов)")

    @_locked
    def search(self, user_id: str, query: str, limit: int = 5) -> list[str]:
        user_docs = self.collection.get(where={"user_id": user_id})
        if not user_docs or not user_docs["ids"]:
            return []

        results = self.collection.query(
            query_texts=[query],
            n_results=min(limit * 3, len(user_docs["ids"])),
            where={"user_id": user_id}
        )

        if not results["documents"] or not results["documents"][0]:
            return []

        return results["documents"][0][:limit]

    @_locked
    def _assemble_full_doc(self, user_id: str, filename: str) -> str | None:
        all_parts = self.full_docs.get(where={"user_id": user_id})
        if not all_parts or not all_parts["ids"]:
            return None

        parts = []
        for doc, meta in zip(all_parts["documents"], all_parts["metadatas"]):
            if isinstance(meta, dict) and meta.get("filename") == filename:
                parts.append((meta.get("part", 0), doc))

        if not parts:
            return None

        parts.sort(key=lambda x: x[0])
        return "".join(doc for _, doc in parts)

    @_locked
    def get_full_document(self, user_id: str, filename: str = None) -> str | None:
        """
        Возвращает полный текст документа.
        Если filename не указан — возвращает последний загруженный.
        """
        if filename:
            return self._assemble_full_doc(user_id, filename)
        else:
            # Последний загруженный документ — находим по максимальному timestamp
            all_parts = self.full_docs.get(where={"user_id": user_id})
            if not all_parts or not all_parts["ids"]:
                return None

            # Группируем по filename, берём с максимальным timestamp
            files = {}
            for meta in all_parts["metadatas"]:
                if isinstance(meta, dict) and "filename" in meta:
                    fn = meta["filename"]
                    ts = meta.get("timestamp", 0)
                    if fn not in files or ts > files[fn]:
                        files[fn] = ts

            if not files:
                return None

            latest_file = max(files, key=files.get)
            return self._assemble_full_doc(user_id, latest_file)

    @_locked
    def get_loaded_files(self, user_id: str) -> list[str]:
        user_docs = self.collection.get(where={"user_id": user_id})
        if not user_docs or not user_docs["metadatas"]:
            return []

        # Уникальные имена файлов
        filenames = set()
        for meta in user_docs["metadatas"]:
            if isinstance(meta, dict) and "filename" in meta:
                filenames.add(meta["filename"])
        return list(filenames)

    @_locked
    def list_files_detailed(self, user_id: str) -> list[dict]:
        # Список файлов с метаданными: имя, размер полного текста (символов), дата загрузки.
        docs = self.full_docs.get(where={"user_id": user_id})
        out: dict = {}
        for meta in (docs.get("metadatas") or []):
            if not isinstance(meta, dict):
                continue
            fn = meta.get("filename")
            if fn and fn not in out:
                out[fn] = {
                    "filename": fn,
                    "size": meta.get("total_chars", 0),
                    "timestamp": meta.get("timestamp", 0),
                }
        return sorted(out.values(), key=lambda d: d["timestamp"])

    @_locked
    def remove_file(self, user_id: str, filename: str) -> bool:
        # Удалить один файл пользователя (чанки + полный текст). False — файла не было.
        user_docs = self.collection.get(where={"user_id": user_id})
        chunk_ids = [
            eid for eid, meta in zip(user_docs["ids"], user_docs.get("metadatas", []))
            if isinstance(meta, dict) and meta.get("filename") == filename
        ]
        if not chunk_ids:
            return False
        self.collection.delete(ids=chunk_ids)
        self._delete_full_doc(user_id, filename)
        logger.info(f"  [FileDB] Удалён файл {filename} для {user_id}")
        return True

    @_locked
    def _delete_full_doc(self, user_id: str, filename: str):
        all_parts = self.full_docs.get(where={"user_id": user_id})
        if not all_parts or not all_parts["ids"]:
            return
        ids_to_delete = [
            rid for rid, meta in zip(all_parts["ids"], all_parts["metadatas"])
            if isinstance(meta, dict) and meta.get("filename") == filename
        ]
        if ids_to_delete:
            self.full_docs.delete(ids=ids_to_delete)

    @_locked
    def reset(self, user_id: str = None):
        """
        Сбросить базу файлов.
        Если user_id указан — только для этого пользователя.
        """
        if user_id:
            user_docs = self.collection.get(where={"user_id": user_id})
            if user_docs and user_docs["ids"]:
                self.collection.delete(ids=user_docs["ids"])
                # Удаляем все полные тексты этого пользователя
                full = self.full_docs.get(where={"user_id": user_id})
                if full and full["ids"]:
                    self.full_docs.delete(ids=full["ids"])
                self._loaded_docs.pop(user_id, None)
                logger.info(f"  [FileDB] Сброшены файлы для {user_id}")
        else:
            all_docs = self.collection.get()
            if all_docs and all_docs["ids"]:
                self.collection.delete(ids=all_docs["ids"])
            all_full = self.full_docs.get()
            if all_full and all_full["ids"]:
                self.full_docs.delete(ids=all_full["ids"])
            self._loaded_docs.clear()
            logger.info("  [FileDB] Сброшены все файлы")

    def _split_content(self, content: str, chunk_size: int = 1000) -> list[str]:
        # Разбить контент на чанки с перекрытием.
        if len(content) <= chunk_size:
            return [content]

        chunks = []
        overlap = chunk_size // 4
        for i in range(0, len(content), chunk_size - overlap):
            chunks.append(content[i:i + chunk_size])
        return chunks
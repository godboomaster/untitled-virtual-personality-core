"""
Векторная база данных для временного хранения файлов.
Хранит максимум 3 документа на пользователя.
Полный текст хранится отдельно для пересказа/анализа целиком.
"""

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from app.core.config import Config, get_db_paths
import logging
import time

logger = logging.getLogger(__name__)

MAX_DOCS_DEFAULT = 3

class FileVectorDB:
    def __init__(self, db_path: str = None, context: str = "default", max_docs: int = None):
        """
        Инициализация файловой БД.

        Args:
            db_path: Путь к базе данных. Если None, выбирается по context.
            context: Контекст использования — "tg", "gradio" или "default".
            max_docs: Максимальное количество файлов на пользователя. Если None — дефолт 3.
        """
        self.max_docs = max_docs or MAX_DOCS_DEFAULT
        if db_path is None:
            db_path = get_db_paths(context)["files"]

        self.context = context
        self.client = chromadb.PersistentClient(path=db_path)
        self.embedder = SentenceTransformerEmbeddingFunction(
            model_name="paraphrase-multilingual-MiniLM-L12-v2"
        )
        self.collection = self.client.get_or_create_collection(
            "file_documents",
            embedding_function=self.embedder
        )
        # Коллекция для полных текстов документов
        self.full_docs = self.client.get_or_create_collection(
            "file_full_docs",
            embedding_function=self.embedder
        )
        self._loaded_docs: dict[str, str] = {}  # user_id -> filename

    def add_file(self, user_id: str, filename: str, content: str):
        
        # Добавить файл в базу. Если уже есть максимальное количество — удаляет самый старый.
        # Если файл с таким именем уже есть — удаляем все чанки
        
        user_docs = self.collection.get(where={"user_id": user_id})
        if user_docs and user_docs["ids"]:
            existing_ids = [
                eid for eid, meta in zip(user_docs["ids"], user_docs.get("metadatas", []))
                if isinstance(meta, dict) and meta.get("filename") == filename
            ]
            if existing_ids:
                self.collection.delete(ids=existing_ids)
                logger.info(f"  [FileDB] Обновлён файл {filename} для {user_id}")

        # Если достигли лимита — удаляем самый старый документ
        if user_docs and len(user_docs["ids"]) >= self.max_docs:
            oldest_id = user_docs["ids"][0]
            oldest_meta = user_docs["metadatas"][0]
            oldest_filename = oldest_meta.get("filename", "")
            self.collection.delete(ids=[oldest_id])
            
            # Удаляем и полный текст
            self._delete_full_doc(user_id, oldest_filename)
            logger.info(f"  [FileDB] Удалён старый документ для {user_id}")

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

    def search(self, user_id: str, query: str, limit: int = 5) -> list[str]:
        # Поиск по файлам пользователя.
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

    def _assemble_full_doc(self, user_id: str, filename: str) -> str | None:
        # Собирает полный документ из частей
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

    def get_loaded_files(self, user_id: str) -> list[str]:
        # Получить список загруженных файлов пользователя.
        user_docs = self.collection.get(where={"user_id": user_id})
        if not user_docs or not user_docs["metadatas"]:
            return []

        # Уникальные имена файлов
        filenames = set()
        for meta in user_docs["metadatas"]:
            if isinstance(meta, dict) and "filename" in meta:
                filenames.add(meta["filename"])
        return list(filenames)

    def _delete_full_doc(self, user_id: str, filename: str):
        # Удаляет все части полного текста документа
        all_parts = self.full_docs.get(where={"user_id": user_id})
        if not all_parts or not all_parts["ids"]:
            return
        ids_to_delete = [
            rid for rid, meta in zip(all_parts["ids"], all_parts["metadatas"])
            if isinstance(meta, dict) and meta.get("filename") == filename
        ]
        if ids_to_delete:
            self.full_docs.delete(ids=ids_to_delete)

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
                if user_id in self._loaded_docs:
                    del self._loaded_docs[user_id]
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
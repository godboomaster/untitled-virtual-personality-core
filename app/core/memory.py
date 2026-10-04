import chromadb
from collections import deque
from typing import List, Dict, Optional, Tuple
from app.core.atomic_io import atomic_write_text, load_json_safe
from app.core.bounded_cache import BoundedCache
from app.core.chroma_space import (
    COLLECTION_NAMES, VECTOR_SPACE, collection_space, open_collection,
)
from app.core.config import Config, get_db_paths
from app.core.router import ModelRouter
from app.core.presence import web_presence
from app.core import timeutil
from app.core.st_embedder import create_st_embedder
from app.core.memory_config import (
    build_extraction_prompt, should_ignore_message, parse_and_filter_facts,
    split_facts_text, PROMPT_SETTINGS, UPDATE_CATEGORIES, APPEND_CATEGORIES,
    MERGE_SETTINGS, build_merge_prompt, build_summary_prompt, SUMMARY_SETTINGS,
    is_public_category
)
from app.core.users import get_user_tag
from app.core.language import detect_dialogue_language, detect_language, user_language_line
import contextvars
import functools
import time
import json
import threading
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from app.core.paths import data_dir

logger = logging.getLogger(__name__)


def _facts_language_line(lang: Optional[str]) -> str:
    # Язык значений фактов; названия категорий и маркеры — всегда английские
    return (f"{user_language_line(lang)} Category names and markers like "
            "[NO_FACTS] always stay in English exactly as given; only the "
            "values are free text.")

# Сколько чатов держать в оперативных буферах STM (LRU). Вытесненный буфер
# не теряется: при следующем обращении к чату он перечитывается из ChromaDB
# (ShortTermMemory._load_chat_from_db).
MAX_CACHED_CHATS = 200

# Сколько пользователей держать в счётчиках батч-экстракции/консолидации.
# Вытеснение безобидно: счётчик начнётся заново, вызов случится позже.
MAX_COUNTER_USERS = 500

# Пороги близости для точечных операций над фактами. Это cosine-distance
# (0 = идентично, 1 = ортогонально, 2 = противоположно) — метрика коллекций
# задана явно в app/core/chroma_space.py (VECTOR_SPACE); с дефолтной для
# Chroma l2 на ненормированных эмбеддингах эти пороги не работают.
# «забудь про X»: перефразировка факта даёт d≈0.35, посторонний запрос к
# неродственному факту — d≈0.9, поэтому 1.0 (почти вся шкала) удалял бы
# первый попавшийся факт по любому запросу; 0.7 — между этими случаями
FORGET_MAX_DISTANCE = 0.7
UPDATE_FACT_MAX_DISTANCE = 0.3   # правка факта: промах затирает чужой факт

# Источник миллисекундных id записей STM/LTM (stm_{chat}_{ms},
# {user}_fact_{ms}). Две записи в одну миллисекунду (ответ, разбитый на
# части; сохранение факта рядом с консолидацией) получили бы один id, а
# Chroma молча игнорирует add с уже существующим id — вторая запись пропала
# бы без ошибки. Счётчик монотонный: не меньше текущего времени и строго
# больше прошлого значения, поэтому id по-прежнему сортируются по времени.
_ID_MS_LOCK = threading.Lock()
_last_id_ms = 0


def _unique_ms() -> int:
    global _last_id_ms
    with _ID_MS_LOCK:
        _last_id_ms = max(int(time.time() * 1000), _last_id_ms + 1)
        return _last_id_ms


# summarize_user: консолидация отменена, потому что факты пользователя
# поменяли за время её LLM-вызова (forget, правка) — попытку не теряем
SUMMARY_CONFLICT = -2


def _first_sentence(text: str, max_len: int = 80) -> str:
    # Обрезать текст до первого предложения; если длиннее max_len — добавить "..."
    if not text:
        return ""
    # Конец предложения: . ! ? или перенос строки
    for i, ch in enumerate(text):
        if ch in ".!?\n" and i > 0:
            sentence = text[:i + 1].strip()
            if len(sentence) > max_len:
                return sentence[:max_len] + "..."
            return sentence
    # Нет точки — берём до max_len
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text.strip()


class ShortTermMemory:
    """
    Краткосрочная память — буфер последних N сообщений.
    Работает по принципу FIFO (First In, First Out).
    Сохраняется в ChromaDB для восстановления после перезапуска.
    """

    def __init__(self, max_messages: int = 50, db_path: str = None, load_from_db: bool = True,
                 context: str = "default"):
        """
        Args:
            max_messages: Максимальное количество сообщений в буфере на чат.
            db_path: Путь к базе данных. Если None, выбирается по context.
            load_from_db: Загружать ли сообщения из базы при инициализации.
            context: Контекст — "tg", "api_{persona}" или "default".
        """
        self.max_messages = max_messages
        # Буферы чатов — ограниченный LRU-кеш, а не вечный dict: chat_id это
        # каждый чат, куда бота когда-либо добавляли, а значение — до
        # max_messages сообщений (см. app/core/bounded_cache.py)
        self.buffers = BoundedCache(max_entries=MAX_CACHED_CHATS)
        self.context = context
        self._lock = threading.RLock()  # защита от гонки данных при многопоточности
        # Разрешена ли подгрузка буфера чата из БД (в т.ч. после вытеснения
        # из LRU). load_from_db=False — тесты/одноразовые прогоны без истории.
        self._db_backed = load_from_db

        if db_path is None:
            db_path = get_db_paths(context)["stm"]

        self.client = chromadb.PersistentClient(path=db_path)
        self.embedder = create_st_embedder()
        self.collection = open_collection(
            self.client, COLLECTION_NAMES["stm"],
            embedding_function=self.embedder)

        if load_from_db:
            self._load_from_db()

    def _get_buffer(self, chat_id: str) -> deque:
        """Буфер чата: из кеша, иначе подгружаем историю чата из ChromaDB.

        Буфер мог быть вытеснен из LRU (или вообще не загружаться при старте) —
        без подгрузки чат терял бы контекст, хотя сообщения лежат в базе.
        Запрос к БД — вне лока, результат вставляется под локом и уступает
        уже появившемуся буферу (параллельный add_message того же чата).
        """
        with self._lock:
            buf = self.buffers.get(chat_id)
            if buf is not None:
                return buf
        restored = self._load_chat_from_db(chat_id)
        with self._lock:
            buf = self.buffers.get(chat_id)
            if buf is None:
                buf = restored
                self.buffers[chat_id] = buf
            return buf

    def _entry_from_row(self, doc: str, meta: dict) -> dict:
        """Запись буфера из строки ChromaDB — одно определение и для полной
        загрузки при старте, и для подгрузки одного чата."""
        meta = meta or {}
        msg_chat_id = meta.get("chat_id") or meta.get("user_id", "default")
        user_name = meta.get("user_name")
        sender_id = meta.get("sender_id")
        if not user_name and sender_id:
            user_name = get_user_tag(sender_id)
        entry = {
            "role": meta.get("role", "user"),
            "content": doc,
            "chat_id": msg_chat_id,
            # в БД миллисекунды, в буфере — секунды
            "timestamp": (meta.get("timestamp") or 0) / 1000,
        }
        if user_name:
            entry["user_name"] = user_name
        if sender_id:
            entry["sender_id"] = sender_id
        return entry

    def _load_chat_from_db(self, chat_id: str) -> deque:
        # История одного чата из ChromaDB (последние max_messages)
        buf = deque(maxlen=self.max_messages)
        if not self._db_backed:
            return buf
        try:
            results = self.collection.get(
                where={"chat_id": str(chat_id)},
                include=["documents", "metadatas"],
            )
            if not results or not results.get("ids"):
                # Легаси-записи писались без chat_id, только с user_id —
                # _entry_from_row их учитывает, значит и подгрузка должна
                results = self.collection.get(
                    where={"user_id": str(chat_id)},
                    include=["documents", "metadatas"],
                )
            if not results or not results.get("ids"):
                return buf
            entries = [
                self._entry_from_row(doc, meta)
                for doc, meta in zip(results.get("documents", []),
                                     results.get("metadatas", []))
            ]
            entries.sort(key=lambda e: e["timestamp"])
            for entry in entries[-self.max_messages:]:
                buf.append(entry)
        except Exception as e:
            logger.warning(f"  [STM] Не удалось подгрузить историю чата {chat_id}: {e}")
        return buf

    def _load_from_db(self):
        # Загрузить все сообщения из базы и раскидать по буферам чатов
        if self.collection.count() == 0:
            return

        results = self.collection.get(include=["documents", "metadatas"])

        if results["documents"]:
            metadatas = results.get("metadatas") or [{}] * len(results["documents"])
            entries = [
                self._entry_from_row(doc, meta)
                for doc, meta in zip(results["documents"], metadatas)
            ]
            # По возрастанию времени: буферы LRU — позже всех тронуты самые
            # свежие чаты, они же и останутся при вытеснении
            entries.sort(key=lambda e: e["timestamp"])
            with self._lock:
                for entry in entries:
                    buf = self.buffers.get(entry["chat_id"])
                    if buf is None:
                        buf = deque(maxlen=self.max_messages)
                        self.buffers[entry["chat_id"]] = buf
                    buf.append(entry)

    def _save_to_db(self, role: str, content: str, chat_id: str = "default",
                    user_name: str = None, sender_id: str = None):
        # Сохранить сообщение в базу данных. Метка — уникальная миллисекунда
        # (_unique_ms): она же id записи, совпадение id = потеря сообщения
        timestamp = _unique_ms()
        metadata = {"role": role, "timestamp": timestamp, "chat_id": chat_id}
        if user_name:
            metadata["user_name"] = user_name
        if sender_id:
            metadata["sender_id"] = sender_id

        self.collection.add(
            ids=[f"stm_{chat_id}_{timestamp}"],
            documents=[content],
            metadatas=[metadata]
        )

        # Автоочистка: удаляем самые старые записи чата если превышен лимит
        self._trim_db(chat_id)

    def _trim_db(self, chat_id: str):
        """
        Удаляет самые старые записи из ChromaDB для чата,
        если их количество превышает max_messages.
        """
        try:
            results = self.collection.get(
                where={"chat_id": chat_id},
                include=["metadatas"]
            )
            if not results or not results["ids"]:
                return

            count = len(results["ids"])
            if count <= self.max_messages:
                return

            # Сортируем по timestamp и берём самые старые для удаления
            items = list(zip(results["ids"], results["metadatas"]))
            items.sort(key=lambda x: x[1].get("timestamp", 0))

            excess = count - self.max_messages
            ids_to_delete = [item[0] for item in items[:excess]]

            if ids_to_delete:
                self.collection.delete(ids=ids_to_delete)
                logger.debug(f"  [STM] Trimmed {len(ids_to_delete)} old messages from chat {chat_id}")
        except Exception as e:
            logger.warning(f"  [STM] Trim error for chat {chat_id}: {e}")

    def add_message(self, role: str, content: str, user_id: str = "default",
                    chat_id: str = None, user_name: str = None):
        """
        Добавляет сообщение в буфер чата и сохраняет в базу.

        Args:
            user_id: Реальный ID отправителя.
            chat_id: ID чата для фильтрации. Если None — используется user_id.
            user_name: Имя пользователя для отображения в истории.
        """
        filter_id = chat_id if chat_id is not None else user_id
        entry = {"role": role, "content": content, "chat_id": filter_id, "timestamp": time.time()}
        # sender_id — отправитель (для групповых чатов)
        if chat_id is not None:
            entry["sender_id"] = user_id
        if user_name:
            entry["user_name"] = user_name
        self._get_buffer(filter_id).append(entry)
        self._save_to_db(role, content, filter_id, user_name,
                         sender_id=user_id if chat_id is not None else None)
        self._stamp_last_message(filter_id, entry["timestamp"])

    def _stamp_last_message(self, chat_key: str, ts: float):
        """Метка времени последнего сообщения чата на диске
        (data/{context}/last_message.json). API отдаёт её фронту для
        сортировки персон по свежести переписки; метка производная —
        перезаписывается при каждом новом сообщении.

        Read-modify-write под self._lock (RLock — есть повторный вход
        через другие locked-методы MemoryManager) и атомарная запись: без
        лока конкурентные add_message из разных чатов чередовали бы чтение
        и запись и теряли чужие метки. Ошибки диска логируются."""
        try:
            path = data_dir() / self.context / "last_message.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = load_json_safe(path, default={}, label="Memory.last_message")
                if not isinstance(data, dict):
                    data = {}
                data[chat_key] = ts
                atomic_write_text(path, json.dumps(data, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"[Memory] Не удалось обновить last_message для {chat_key}: {e}")

    def get_messages(self, user_id: str = None, chat_id: str = None) -> List[Dict[str, str]]:
        """
        Получить сообщения буфера конкретного чата.

        Args:
            user_id: Если указан, фильтровать только для этого пользователя.
            chat_id: Если указан, фильтровать по чату (приоритет над user_id).
        """
        filter_id = chat_id if chat_id is not None else user_id
        if filter_id is None:
            # Все сообщения из всех буферов
            with self._lock:
                all_msgs = []
                for buf in self.buffers.values():
                    all_msgs.extend(buf)
                return all_msgs
        with self._lock:
            return list(self._get_buffer(filter_id))

    def get_last(self, n: int, user_id: str = None, chat_id: str = None) -> List[Dict[str, str]]:
        """
        Получить последние N сообщений.

        Args:
            user_id: Если указан, фильтровать только для этого пользователя.
            chat_id: Если указан, фильтровать по чату (приоритет над user_id).
        """
        messages = self.get_messages(user_id, chat_id)
        return messages[-n:]

    def search_relevant(self, query: str, chat_id: str, limit: int = 5,
                        exclude_last_n: int = 15) -> List[Dict[str, str]]:
        """
        Векторный поиск по STM — возвращает семантически релевантные сообщения,
        исключая последние exclude_last_n (они и так попадут как хронология).

        Args:
            query: Текст запроса (обычно текущее сообщение пользователя).
            chat_id: ID чата для фильтрации.
            limit: Сколько релевантных сообщений вернуть.
            exclude_last_n: Сколько последних сообщений исключить (дубликаты с хронологией).

        Returns:
            Список {role, content, chat_id} релевантных сообщений, не входящих в последние n.
        """
        try:
            if self.collection.count() == 0:
                return []

            # Получаем содержимое последних exclude_last_n чтобы отфильтровать дубли
            recent = self.get_last(exclude_last_n, chat_id=chat_id)
            recent_contents = {m["content"] for m in recent}

            # Векторный поиск с запасом (на случай дубликатов с recent)
            fetch_n = limit + exclude_last_n + 10
            results = self.collection.query(
                query_texts=[query],
                n_results=fetch_n,
                where={"chat_id": chat_id},
                include=["documents", "metadatas"]
            )

            if not results or not results["documents"] or not results["documents"][0]:
                return []

            relevant = []
            seen = set()
            for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
                if doc in recent_contents:  # уже есть в хронологии
                    continue
                if doc in seen:  # дубликат внутри результатов поиска
                    continue
                seen.add(doc)

                entry = {
                    "role": meta.get("role", "user"),
                    "content": doc,
                    "chat_id": chat_id,
                    # в БД миллисекунды — приводим к секундам, как в буфере
                    "timestamp": (meta.get("timestamp") or 0) / 1000,
                }
                if meta.get("user_name"):
                    entry["user_name"] = meta["user_name"]
                relevant.append(entry)

                if len(relevant) >= limit:
                    break

            return relevant

        except Exception as e:
            logger.warning(f"  [STM] search_relevant error: {e}")
            return []

    def get_last_display(self, n: int, chat_id: str) -> List[Dict]:
        """
        Получить последние n сообщений для отображения (/last).
        Возвращает список с role, user_name, content (обрезанное до первого предложения)
        и time — «15.08 14:32» (дата + время отправки).
        """
        messages = self.get_last(n, chat_id=chat_id)
        result = []
        for m in messages:
            content = m.get("content", "")
            first_sentence = _first_sentence(content)
            # Формат метки — общее определение с persona._format_msg_ts
            # (время пользователя по TIMEZONE, год только если не текущий).
            # Импорт локальный — persona не нужна ради одной метки.
            from app.core.persona import _format_msg_ts
            time_str = _format_msg_ts(m.get("timestamp"))
            result.append({
                "role": m.get("role", "user"),
                "user_name": m.get("user_name"),
                "content": first_sentence,
                "time": time_str,
            })
        return result

    def pop_last_n(self, n: int, chat_id: str) -> int:
        """
        Удалить последние n сообщений из deque и ChromaDB.
        Возвращает количество удалённых.
        """
        with self._lock:
            # _get_buffer, а не buffers.get: буфер мог быть вытеснен из LRU,
            # и «удали последние N» молча не сделало бы ничего
            buf = self._get_buffer(chat_id)
            if not buf:
                return 0

            # Берём последние n из deque
            to_remove = []
            for _ in range(min(n, len(buf))):
                if buf:
                    to_remove.append(buf.pop())

            if not to_remove:
                return 0

            # Ищем эти записи в ChromaDB по тексту в пределах чата
            contents_to_remove = {m["content"] for m in to_remove}

            try:
                results = self.collection.get(
                    where={"chat_id": chat_id},
                    include=["documents", "metadatas"]
                )
                if results and results["ids"]:
                    ids_to_delete = []
                    for rid, doc in zip(results["ids"], results["documents"]):
                        if doc in contents_to_remove:
                            ids_to_delete.append(rid)
                    if ids_to_delete:
                        self.collection.delete(ids=ids_to_delete)
                        logger.info(f"  [STM] pop_last_n: deleted {len(ids_to_delete)} from ChromaDB")
            except Exception as e:
                logger.warning(f"  [STM] pop_last_n ChromaDB error: {e}")

            return len(to_remove)

    def delete_message(self, chat_id: str, index: int) -> bool:
        """
        Удалить одно сообщение из буфера чата по индексу
        (порядок — как в get_messages). Синхронно удаляет запись
        из ChromaDB. True — сообщение нашлось и удалено.
        """
        with self._lock:
            buf = self._get_buffer(chat_id)  # вытесненный из LRU — подгрузится
            if not buf or index < 0 or index >= len(buf):
                return False
            entry = list(buf)[index]
            del buf[index]

        # Из ChromaDB: запись этого чата с тем же текстом, ближайшая по времени
        # (в БД timestamp в миллисекундах, в буфере — в секундах)
        try:
            results = self.collection.get(
                where={"chat_id": chat_id},
                include=["documents", "metadatas"]
            )
            if results and results["ids"]:
                target_ms = round(entry.get("timestamp", 0) * 1000)
                candidates = [
                    (rid, abs(int(meta.get("timestamp", 0)) - target_ms))
                    for rid, doc, meta in zip(
                        results["ids"], results["documents"], results.get("metadatas", [])
                    )
                    if doc == entry["content"]
                ]
                if candidates:
                    rid = min(candidates, key=lambda x: x[1])[0]
                    self.collection.delete(ids=[rid])
        except Exception as e:
            logger.warning(f"  [STM] delete_message ChromaDB error: {e}")
        return True

    def remove_entry(self, chat_id: str, entry: dict) -> bool:
        """Удалить из буфера чата ИМЕННО эту запись (по идентичности dict'а,
        поиск и удаление — под одним self._lock) и её копию из ChromaDB.

        Для отката фонового сообщения, которое не удалось доставить
        (app/core/turn_gate.py): delete_message по индексу снапшота при
        полном deque (maxlen) или конкурентной записи удалил бы чужую
        реплику, а поиск по тексту — старую запись с тем же шаблонным
        текстом."""
        with self._lock:
            buf = self._get_buffer(chat_id)
            for i in range(len(buf) - 1, -1, -1):
                if buf[i] is entry:
                    del buf[i]
                    break
            else:
                return False
        try:
            results = self.collection.get(
                where={"chat_id": chat_id},
                include=["documents", "metadatas"]
            )
            if results and results["ids"]:
                target_ms = round(entry.get("timestamp", 0) * 1000)
                candidates = [
                    (rid, abs(int(meta.get("timestamp", 0)) - target_ms))
                    for rid, doc, meta in zip(
                        results["ids"], results["documents"], results.get("metadatas", [])
                    )
                    if doc == entry["content"]
                ]
                if candidates:
                    rid = min(candidates, key=lambda x: x[1])[0]
                    self.collection.delete(ids=[rid])
        except Exception as e:
            logger.warning(f"  [STM] remove_entry ChromaDB error: {e}")
        return True

    def clear(self, chat_id: str = None):
        """
        Очистить буфер.
        
        Args:
            chat_id: Если указан, очистить только для этого чата.
        """
        if chat_id is not None:
            # Очистить только сообщения конкретного чата
            results = self.collection.get(include=["metadatas"])
            if results and results["ids"]:
                ids_to_delete = [
                    rid for rid, meta in zip(results["ids"], results.get("metadatas", []))
                    if (meta.get("chat_id") or meta.get("user_id")) == chat_id
                ]
                if ids_to_delete:
                    self.collection.delete(ids=ids_to_delete)
                    print(f"  [STM] Удалено {len(ids_to_delete)} сообщений чата {chat_id}")
            with self._lock:
                self.buffers.pop(chat_id, None)
        else:
            with self._lock:
                self.buffers.clear()
            try:
                results = self.collection.get()
                if results and results["ids"]:
                    self.collection.delete(ids=results["ids"])
                    print(f"  [STM] Удалено {len(results['ids'])} сообщений из базы")
            except Exception as e:
                print(f"  [STM] Ошибка при очистке STM: {e}")

    def __len__(self) -> int:
        with self._lock:
            return sum(len(buf) for buf in self.buffers.values())


class LongTermMemory:
    """
    Долгосрочная память — векторное хранилище важных фактов.
    Использует LLM для фильтрации важной информации.
    """
    
    _executor = None
    _executor_lock = threading.Lock()
    
    # Пул из 3 потоков на уровне класса — общий для всех экземпляров
    # LongTermMemory в процессе (все персоны делят его)
    @classmethod
    def _get_executor(cls):
        # Двойная проверка под локом (double-checked locking)
        if cls._executor is None:
            with cls._executor_lock:
                if cls._executor is None:
                    cls._executor = ThreadPoolExecutor(
                        max_workers=3,
                        thread_name_prefix="ltm_extractor"
                    )
        return cls._executor
    
    def __init__(self, ltm_model_provider: str = None, db_path: str = None, context: str = "default",
                 main_router: 'ModelRouter' = None):
        """
        Args:
            ltm_model_provider: Провайдер модели для извлечения фактов.
            db_path: Путь к базе данных. Если None, выбирается по context.
            context: Контекст — "tg", "api_{persona}" или "default".
            main_router: Основной роутер бота. LTM пропустит его active_provider
                         чтобы не нагружать одну и ту же модель.
        """
        if db_path is None:
            db_path = get_db_paths(context)["ltm"]

        self.context = context
        self.client = chromadb.PersistentClient(path=db_path)
        self.embedder = create_st_embedder()
        self.collection = open_collection(
            self.client, COLLECTION_NAMES["ltm"],
            embedding_function=self.embedder)
        # Фактическая метрика коллекции: пороги forget/update_fact — под cosine.
        # Если перенос не удался (например, диск только для чтения), об этом
        # должно быть видно в логе, а не «забудь про X молча ничего не делает».
        self.space = collection_space(self.collection)
        if self.space != VECTOR_SPACE:
            logger.warning(
                f"[LTM] Метрика коллекции — {self.space} вместо {VECTOR_SPACE}: "
                f"пороги схожести (forget/update_fact) рассчитаны на "
                f"{VECTOR_SPACE}, точечные операции могут не находить факт")

        self.ltm_model_provider = ltm_model_provider or Config.LTM_MODEL_PROVIDER
        self.main_router = main_router
        self.exclude_provider = main_router.active_provider if main_router else None
        if self.ltm_model_provider:
            # Явно заданный провайдер (LTM_MODEL_PROVIDER) — отдельный роутер
            self.llm_router = ModelRouter(provider=self.ltm_model_provider, context=self.context)
            print(f"  [LTM] LTM использует модель: {self.llm_router.get_provider_model_info()}")
        elif self.main_router is not None:
            # Дефолт: побочные задачи идут по fallback-цепочке ОСНОВНОГО
            # роутера персоны, пропуская её основного провайдера
            self.llm_router = self.main_router
            print(f"  [LTM] LTM идёт по fallback-цепочке персоны (кроме {self.exclude_provider})")
        else:
            self.llm_router = ModelRouter(context=self.context)
            print(f"  [LTM] LTM использует активный провайдер: {self.llm_router.get_provider_model_info()}")
        if self.exclude_provider:
            print(f"  [LTM] Пропускает провайдер основной модели: {self.exclude_provider}")

        # Сериализует изменения фактов (save_facts, запись консолидации,
        # forget/update_fact, clear): read-modify-write по фактам не атомарен,
        # параллельные фоновые задачи иначе плодят дубли/теряют факты. Под ним
        # же читают search/get_* — многошаговая запись (удалить + добавить)
        # не видна читателю наполовину. Поэтому лок НИКОГДА не держится поверх
        # вызова LLM (до 150 с у веб-чата): иначе ответ пользователю ждал бы
        # фоновую задачу на чтении фактов.
        self._facts_lock = threading.RLock()
        # Эпохи очистки: clear(user_id) увеличивает эпоху пользователя,
        # clear_all() — общую. Фоновая задача запоминает эпоху до LLM-вызова
        # и при записи сверяет: сменилась — результат относится к стёртой
        # памяти и отбрасывается (иначе экстракция/консолидация, начатые до
        # очистки, воскресили бы факты после неё). Под _facts_lock.
        self._epochs: Dict[str, int] = {}
        self._epoch_all = 0
        # Очередь фоновых задач на пользователя (экстракция, консолидация):
        # user_id → задачи, ждущие завершения текущей. Запись в словаре есть,
        # пока у пользователя что-то выполняется. См. _submit_serial.
        self._serial: Dict[str, deque] = {}
        self._serial_lock = threading.Lock()
        # Последний язык диалога пользователя (user_id → 'ru'/'en') — для
        # фоновых промптов экстракции/слияния/консолидации (MemoryManager
        # обновляет его на каждом сообщении пользователя)
        self._user_langs: Dict[str, str] = {}

        # Режимы приватности LTM per user: "smart" (по умолчанию) | "strict"
        self._privacy_file = Path(db_path).parent / "ltm_privacy.json"
        self._privacy_modes: Dict[str, str] = self._load_privacy_modes()

    def note_user_language(self, user_id, text: str):
        lang = detect_language(text)
        if lang:
            if not hasattr(self, "_user_langs"):
                self._user_langs = {}
            self._user_langs[str(user_id)] = lang

    def user_language(self, user_id) -> Optional[str]:
        return getattr(self, "_user_langs", {}).get(str(user_id))

    def _exclude(self):
        """Кого пропускать в цепочке: текущий основной провайдер основного
        роутера. Динамически — основной могут сменить на лету через досье,
        а self.exclude_provider зафиксирован при старте."""
        if self.main_router is not None:
            return self.main_router.active_provider
        return self.exclude_provider

    def _epoch(self, user_id) -> Tuple[int, int]:
        # Текущая эпоха очистки фактов пользователя (см. _epochs)
        with self._facts_lock:
            return self._epoch_all, self._epochs.get(str(user_id), 0)

    def _epoch_stale(self, user_id, epoch) -> bool:
        return epoch is not None and self._epoch(user_id) != epoch

    def _submit_serial(self, user_id, fn, on_cancel=None) -> bool:
        """Фоновая задача пользователя — строго после предыдущих его задач.

        add_message ставит экстракцию и консолидацию одним вызовом. Если
        запустить их параллельно, экстракция за время LLM-вызова консолидации
        сделает UPDATE/слияние снятого ею факта, и консолидация отменится
        (и так каждый раз). По очереди консолидация видит факты уже после
        экстракции.

        on_cancel — если задача так и не выполнится (пул остановлен).
        False — пул не принял задачу сразу (on_cancel уже вызван).
        """
        key = str(user_id)
        # Контекст вызывающего (область диалога — свой тред веб-чата) едет
        # с задачей: executor его не копирует, а задача может стартовать
        # позже, из потока предыдущей (_serial_next)
        fn = functools.partial(contextvars.copy_context().run, fn)
        with self._serial_lock:
            queue = self._serial.get(key)
            if queue is not None:
                queue.append((fn, on_cancel))
                return True
            self._serial[key] = deque()
        return self._run_serial(key, fn, on_cancel)

    def _run_serial(self, key: str, fn, on_cancel) -> bool:
        try:
            future = self._get_executor().submit(fn)
        except Exception as e:
            print(f"  [LTM] Пул не принял фоновую задачу: {e}")
            if on_cancel:
                on_cancel()
            self._serial_next(key)
            return False

        def _done(f):
            # Отменённый future (shutdown пула) — fn не выполнялась;
            # f.exception() на нём бросает CancelledError
            if f.cancelled():
                if on_cancel:
                    on_cancel()
            elif f.exception():
                # ThreadPoolExecutor молча глотает исключения
                print(f"  [LTM] ОШИБКА фоновой задачи: {f.exception()}")
            self._serial_next(key)

        future.add_done_callback(_done)
        return True

    def _serial_next(self, key: str):
        with self._serial_lock:
            queue = self._serial.get(key)
            if not queue:
                self._serial.pop(key, None)
                return
            fn, on_cancel = queue.popleft()
        self._run_serial(key, fn, on_cancel)

    # ─── Приватность LTM ─────────────────────────────────

    def _load_privacy_modes(self) -> Dict[str, str]:
        try:
            if self._privacy_file.exists():
                with open(self._privacy_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): v for k, v in data.items() if v in ("smart", "strict")}
        except Exception as e:
            logger.warning(f"[LTM] Не удалось загрузить режимы приватности: {e}")
        return {}

    def _save_privacy_modes(self):
        try:
            self._privacy_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._privacy_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._privacy_modes, f, ensure_ascii=False, indent=2)
            tmp.replace(self._privacy_file)
        except Exception as e:
            logger.warning(f"[LTM] Не удалось сохранить режимы приватности: {e}")

    def get_privacy_mode(self, user_id: str) -> str:
        # Режим приватности пользователя: "smart" (по умолчанию) или "strict"
        return self._privacy_modes.get(str(user_id), "smart")

    def set_privacy_mode(self, user_id: str, mode: str) -> str:
        # Устанавливает режим приватности, возвращает фактически установленный
        if mode not in ("smart", "strict"):
            mode = "smart"
        self._privacy_modes[str(user_id)] = mode
        self._save_privacy_modes()
        return mode

    def _fact_visible(self, meta: dict, doc: str, user_id: str, chat_id) -> bool:
        """Виден ли факт в текущем чате.

        Правила:
        - личка (chat_id == user_id или нет chat_id) — видно всё;
        - strict: только факты, узнанные в этом чате;
        - smart (по умолчанию): факты этого чата ИЛИ публичный профиль
          (имя, город, хобби...). Легаси-факты без origin_chat считаются личными,
          кроме публичных категорий (категория парсится из текста факта).
        """
        if chat_id is None or str(chat_id) == str(user_id):
            return True
        meta = meta or {}
        if meta.get("origin_chat") == str(chat_id):
            return True
        if self.get_privacy_mode(user_id) == "strict":
            return False
        category = meta.get("category") or (doc.partition(":")[0].strip() if ":" in doc else "")
        return is_public_category(category)
    
    def extract_facts_async(self, user_message: str, user_id: str = "default", stm_context: str = None,
                            origin_chat: str = None, user_name: str = None, lang: str = None):
        """
        Запускает извлечение фактов в фоновом потоке.
        origin_chat — чат, где факт был рассказан (для скоупа приватности).
        lang — язык диалога пользователя ('ru'/'en'); None — последний
        известный язык пользователя (user_language) или по тексту.
        """
        lang = lang or self.user_language(user_id)
        # Эпоха — в момент постановки, а не старта задачи: задача может ждать
        # свободный поток пула, и очистка за это время не должна «пропустить»
        # факты из сообщений, сказанных до неё
        epoch = self._epoch(user_id)

        def _extract_and_save():
            try:
                facts_raw = self.extract_facts(user_message, stm_context, lang=lang)
                if facts_raw:
                    facts_dict = parse_and_filter_facts(facts_raw)
                    if facts_dict:
                        # Дополнительная фильтрация перед записью в базу
                        safe_facts = {
                            k: v for k, v in facts_dict.items()
                            if not v.lower().startswith(("no ", "no_", "not ", "unknown", "n/a", "нет", "не "))
                            and not v.startswith("[NO_")  # фильтруем [NO_FACTS], [NO_PETS] и т.д.
                        }
                        saved = 0
                        for category, value in safe_facts.items():
                            fact_text = f"{category}: {value}"
                            if self.save_facts(fact_text, user_id, origin_chat=origin_chat,
                                               user_name=user_name, epoch=epoch, lang=lang):
                                saved += 1
                        print(f"  [LTM] Сохранено фактов: {saved}")
                    else:
                        print(f"  [LTM] Факты отфильтрованы (пустые значения)")
                else:
                    print(f"  [LTM] Факты не найдены (фон)")
            except Exception as e:
                # ThreadPoolExecutor молча глотает исключения — перехватываем явно
                print(f"  [LTM] ОШИБКА в фоновой задаче: {e}")
                import traceback
                traceback.print_exc()

        # По очереди с другими фоновыми задачами пользователя (_submit_serial);
        # ошибки мимо try/except логирует её done-колбэк
        self._submit_serial(user_id, _extract_and_save)
        print(f"  [LTM] Extraction запущен в фоне: '{user_message[:40]}...'")
    
    def extract_facts(self, user_message: str, stm_context: str = None,
                      lang: str = None) -> Optional[str]:
        """
        Использует LLM для извлечения важных фактов из сообщения.
        Возвращает строку фактов или None.
        """
        if should_ignore_message(user_message):
            print(f"  [LTM] Сообщение игнорируется (паттерн)")
            return None

        prompt = build_extraction_prompt(user_message, stm_context)

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a fact extractor. Answer strictly by instruction. "
                    "Write facts comma-separated. "
                    "If there are no facts — write only [NO_FACTS].\n"
                    f"{_facts_language_line(lang or detect_language(user_message))}"
                )
            },
            {"role": "user", "content": prompt}
        ]

        try:
            response = self.llm_router.get_response(
                messages,
                temperature=PROMPT_SETTINGS["temperature"],
                max_tokens=PROMPT_SETTINGS["max_tokens"],
                exclude_provider=self._exclude(),
                webchat_channel="side",
                timeout=15.0
            )

            response_clean = (response or "").strip()
            print(f"  [LTM] Extraction [{self.llm_router.get_provider_model_info()}]: '{user_message[:50]}...'")
            print(f"     -> RAW: '{response}'")

            if not response_clean:
                print(f"     -> Пустой ответ")
                return None

            # Убираем кавычки по краям
            if response_clean.startswith('"') and response_clean.endswith('"'):
                response_clean = response_clean[1:-1]

            # Убираем квадратные скобки — превращает [NO_FACTS] → NO_FACTS
            if response_clean.startswith('[') and response_clean.endswith(']'):
                response_clean = response_clean[1:-1]

            # Проверяем на NO_FACTS (с учётом регистра и пробелов)
            if response_clean.strip().upper() == "NO_FACTS":
                print(f"     -> Нет фактов [NO_FACTS]")
                return None

            if len(response_clean) < 3:
                print(f"     -> Слишком короткий ответ")
                return None

            if ":" not in response_clean:
                print(f"     -> Нет формата 'Category: value'")
                return None

            print(f"     -> Факты извлечены: {response_clean}")
            return response_clean

        except Exception as e:
            print(f"  [LTM] Ошибка при вызове LLM: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def save_facts(self, facts_text: str, user_id: str = "default", origin_chat: str = None,
                   user_name: str = None, epoch: Tuple[int, int] = None,
                   lang: str = None) -> bool:
        """Потокобезопасная обёртка над _save_facts_impl.

        LLM-слияния APPEND-категорий считаются заранее, вне _facts_lock
        (_plan_merges): под локом только чтение и запись базы.
        epoch — эпоха очистки на момент, когда факты были услышаны (фоновая
        экстракция); если память с тех пор очистили — факты не пишутся.
        False — запись отброшена по эпохе.
        """
        facts_list = split_facts_text(facts_text)
        # Эпоха — и до планирования (не звать LLM-слияние ради стёртой
        # памяти), и под локом (очистка могла случиться во время слияния)
        merges = ({} if self._epoch_stale(user_id, epoch)
                  else self._plan_merges(facts_list, user_id, lang=lang))
        with self._facts_lock:
            if self._epoch_stale(user_id, epoch):
                print(f"  [LTM] Память {user_id} очищена во время экстракции — "
                      f"факты отброшены: '{facts_text[:50]}'")
                return False
            self._save_facts_impl(facts_list, user_id, origin_chat, user_name, merges)
            return True

    def _load_existing(self, user_id: str) -> Tuple[set, Dict[str, Tuple[str, str]]]:
        """Существующие факты пользователя: полные строки (для дубликатов) и
        первый факт каждой категории — category → (chroma_id, value)."""
        existing_docs = set()
        existing_by_cat = {}
        if self.collection.count() > 0:
            results = self.collection.get(
                where={"user_id": user_id},
                include=["documents"]
            )
            if results and results["documents"]:
                for idx, doc in enumerate(results["documents"]):
                    doc_stripped = doc.strip()
                    existing_docs.add(doc_stripped.lower())
                    # Разбираем "Category: value"
                    if ":" in doc_stripped:
                        cat, _, val = doc_stripped.partition(":")
                        cat_key = cat.strip()
                        if cat_key not in existing_by_cat:
                            existing_by_cat[cat_key] = (results["ids"][idx], val.strip())
        return existing_docs, existing_by_cat

    def _plan_merges(self, facts_list: List[str], user_id: str,
                     lang: str = None) -> Dict[Tuple[str, str, str], Optional[str]]:
        """Слияния APPEND-категорий (возможно, через LLM) — до захвата
        _facts_lock. Ключ — (категория, старое значение, новое значение):
        если к записи старое значение в базе успело смениться, план не
        подойдёт и _save_facts_impl сольёт без LLM."""
        wanted = []
        for fact in facts_list:
            cat, sep, val = fact.strip().partition(":")
            if sep and cat.strip() in APPEND_CATEGORIES:
                wanted.append((cat.strip(), val.strip(), fact.strip().lower()))
        if not wanted:
            return {}
        with self._facts_lock:
            existing_docs, existing_by_cat = self._load_existing(user_id)
        plan = {}
        for cat, new_val, fact_lower in wanted:
            if fact_lower in existing_docs or cat not in existing_by_cat:
                continue
            old_val = existing_by_cat[cat][1]
            key = (cat, old_val, new_val)
            if key not in plan:
                plan[key] = self._merge_append_fact(
                    cat, old_val, new_val, lang=lang or self.user_language(user_id))
        return plan

    def _save_facts_impl(self, facts_list: List[str], user_id: str = "default", origin_chat: str = None,
                         user_name: str = None, merges: Dict = None):
        """
        Сохраняет извлечённые факты в векторную базу.

        origin_chat — чат, где факт был рассказан. Хранится в metadata и
        определяет приватность: непубличный факт виден только там (и в личке).
        
        Логика:
        - Полный дубликат → пропуск
        - UPDATE-категория (City, Age и т.д.) → замена старого значения
        - APPEND-категория (Hobby, Food и т.д.) → умное слияние через LLM
        - Новая категория → обычное сохранение

        facts_list — уже нарезанный текст (split_facts_text режет по границам
        категорий, не по запятым: значение может само содержать запятые).
        merges — заранее посчитанные слияния (_plan_merges). Вызывается под
        _facts_lock, поэтому LLM здесь не зовётся.
        """
        merges = merges or {}
        # Существующие факты: полные документы + разбор по категориям
        existing_docs, existing_by_cat = self._load_existing(user_id)

        added = 0
        for fact in facts_list:
            fact_stripped = fact.strip()

            # 1. Полный дубликат
            if fact_stripped.lower() in existing_docs:
                print(f"  [LTM] Дубликат пропущен: '{fact_stripped[:50]}'")
                continue

            # Разбираем категорию нового факта
            cat_key = None
            new_val = None
            if ":" in fact_stripped:
                cat_raw, _, val_raw = fact_stripped.partition(":")
                cat_key = cat_raw.strip()
                new_val = val_raw.strip()

            # 2. Категория уже есть в базе
            if cat_key and cat_key in existing_by_cat:
                old_id, old_val = existing_by_cat[cat_key]

                if cat_key in UPDATE_CATEGORIES:
                    # Замена: удаляем старый факт, сохраняем новый
                    self.collection.delete(ids=[old_id])
                    print(f"  [LTM] UPDATE {cat_key}: '{old_val}' → '{new_val}'")

                elif cat_key in APPEND_CATEGORIES:
                    # Умное слияние (LLM) посчитано до лока в _plan_merges
                    key = (cat_key, old_val, new_val)
                    merged = merges.get(key)
                    if not merged:
                        # Плана нет (старое значение сменилось) или LLM не
                        # ответил — сливаем вручную без LLM, чтобы не терять
                        # уже накопленное значение
                        merged = self._merge_append_fact(cat_key, old_val, new_val,
                                                         allow_llm=False)
                    self.collection.delete(ids=[old_id])
                    if merged:
                        fact_stripped = f"{cat_key}: {merged}"
                    print(f"  [LTM] MERGE {cat_key}: '{old_val}' + '{new_val}' → '{merged}'")
                else:
                    # Категория без явного типа — обе записи сохраняются как есть
                    pass

            # 3. Сохраняем факт (новый или обновлённый/слитый)
            fact_id = f"{user_id}_fact_{_unique_ms()}"
            self.collection.add(
                ids=[fact_id],
                documents=[fact_stripped],
                metadatas=[{
                    "user_id": user_id,
                    "type": "long_term",
                    # ChromaDB не принимает None в metadata — пустая строка = личное (нет origin)
                    "origin_chat": str(origin_chat) if origin_chat else "",
                    "category": cat_key or "",
                    "user_name": user_name or "",
                }]
            )
            existing_docs.add(fact_stripped.lower())
            # Маппинг категории — на только что записанный факт (реальный id
            # и итоговое значение): id должен быть настоящим, иначе второй
            # факт той же категории в этом же вызове не найдёт что удалить
            # при замене/слиянии, и в базе останутся оба
            if cat_key:
                existing_by_cat[cat_key] = (fact_id, fact_stripped.partition(":")[2].strip())
            added += 1

        if added > 0:
            print(f"  [LTM] Сохранено {added} фактов (из {len(facts_list)})")

    def _merge_append_fact(self, category: str, existing: str, new_value: str,
                           allow_llm: bool = True, lang: str = None) -> Optional[str]:
        """
        Гибридное слияние фактов APPEND-категории.
        Сначала пробует ручное объединение, если сложно — вызывает LLM.
        allow_llm=False — под _facts_lock: вместо LLM объединение без
        повторов (регистр не важен, порядок сохраняется).
        """
        # 1. Быстрое ручное объединение
        existing_items = [item.strip().lower() for item in existing.split(",")]
        new_items = [item.strip().lower() for item in new_value.split(",")]
        
        # 2. Если нет пересечений и список короткий — делаем вручную
        if len(existing_items) + len(new_items) <= 5 and not set(existing_items) & set(new_items):
            all_items = list(set(existing_items + new_items))
            return ", ".join(sorted(all_items))
        
        # 3. Если есть подозрение на дубликаты или сложный случай — LLM
        if allow_llm:
            return self._merge_with_llm(category, existing, new_value, lang=lang)
        seen, items = set(), []
        for item in existing.split(",") + new_value.split(","):
            item = item.strip()
            if item and item.lower() not in seen:
                seen.add(item.lower())
                items.append(item)
        return ", ".join(items) or None

    def _merge_with_llm(self, category: str, existing: str, new_value: str,
                        lang: str = None) -> Optional[str]:
        """
        Умное слияние фактов APPEND-категории через LLM.
        Возвращает объединённое значение или None при ошибке.
        """
        prompt = build_merge_prompt(category, existing, new_value)

        messages = [
            {
                "role": "system",
                "content": ("You merge values for long-term memory. Output ONLY the final merged value.\n"
                            + _facts_language_line(lang or detect_language(new_value)))
            },
            {"role": "user", "content": prompt}
        ]

        try:
            response = self.llm_router.get_response(
                messages,
                temperature=MERGE_SETTINGS["temperature"],
                max_tokens=MERGE_SETTINGS["max_tokens"],
                exclude_provider=self._exclude(),
                webchat_channel="side",
                timeout=15.0
            )

            merged = (response or "").strip().strip('"').strip("'")

            if not merged or len(merged) < 2:
                print(f"  [LTM] MERGE: пустой ответ для {category}")
                return None

            return merged

        except Exception as e:
            print(f"  [LTM] MERGE ошибка для {category}: {e}")
            return None

    def summarize_user(self, user_id: str = "default") -> int:
        """
        Периодическая консолидация LTM для пользователя.
        LLM получает все факты, чистит противоречия и дубликаты,
        затем старые факты заменяются чистыми.

        LLM-вызов (до 150 с у веб-чата) и эмбеддинги считаются вне
        _facts_lock, чтобы не держать читателей фактов на время ответа
        модели. Снимок фактов берётся под локом, замена — тоже одним шагом
        под локом (а не clear + поштучный add), чтобы читатель не увидел
        промежуточное неполное состояние. Замена применяется только если за
        время LLM-вызова память не очищали (эпоха) и снятые факты не
        удаляли/не меняли. Факты, которые экстракция добавила за это время,
        остаются рядом с чистыми.

        Returns: количество фактов после консолидации, -1 при ошибке/очистке,
        SUMMARY_CONFLICT — отменена из-за правки фактов (стоит повторить).
        """
        with self._facts_lock:
            epoch = self._epoch(user_id)
            snap = self.collection.get(where={"user_id": user_id},
                                       include=["documents", "metadatas"])
        snap_ids = list(snap.get("ids") or [])
        snap_docs = snap.get("documents") or []
        snap_metas = snap.get("metadatas") or [{}] * len(snap_docs)
        all_facts = list(snap_docs)
        if len(all_facts) < 2:
            print(f"  [LTM SUM] Слишком мало фактов ({len(all_facts)}), консолидация не нужна")
            return len(all_facts)

        # Собираем все факты в один большой список
        raw_facts = "\n".join(f"- {f}" for f in all_facts)
        prompt = build_summary_prompt(raw_facts)

        messages = [
            {
                "role": "system",
                "content": (
                    "You consolidate long-term memory. "
                    "Output clean facts, one per line: Category: value. "
                    "No explanations, no markdown, no bullet points.\n"
                    f"{_facts_language_line(self.user_language(user_id))}"
                )
            },
            {"role": "user", "content": prompt}
        ]

        try:
            response = self.llm_router.get_response(
                messages,
                temperature=SUMMARY_SETTINGS["temperature"],
                max_tokens=SUMMARY_SETTINGS["max_tokens"],
                exclude_provider=self._exclude(),
                webchat_channel="side",
                timeout=20.0
            )

            if not response or not response.strip():
                print(f"  [LTM SUM] Пустой ответ от LLM")
                return -1

            # Парсим ответ — одна строка = один факт
            new_facts = []
            # Чистим от мусора
            for line in response.strip().split("\n"):
                line = line.strip().lstrip("-•* ").strip()
                if ":" not in line or len(line) < 4:
                    continue
                key, _, val = line.partition(":")
                if not key.strip() or not val.strip():
                    continue
                if val.strip().lower() in {"none", "unknown", "not mentioned", "n/a"}:
                    continue
                new_facts.append(line)

            if not new_facts:
                print(f"  [LTM SUM] LLM не вернул валидных фактов")
                return -1

            # Sanity: если LLM вернул подозрительно мало фактов (обрыв по max_tokens),
            # не удаляем старые — иначе потеряем большую часть памяти
            if len(new_facts) * 3 < len(all_facts):
                print(f"  [LTM SUM] Подозрительно мало фактов ({len(new_facts)} из {len(all_facts)}), консолидация отменена")
                return -1

            # Метаданные старых фактов (из снимка): после перезаписи нужно
            # восстановить origin_chat/category/user_name, иначе приватность слетит
            old_meta_by_text = {}
            old_meta_by_cat = {}
            for doc, meta in zip(snap_docs, snap_metas):
                meta = meta or {}
                old_meta_by_text[doc.strip()] = meta
                cat = meta.get("category") or (doc.partition(":")[0].strip() if ":" in doc else "")
                if cat and cat not in old_meta_by_cat:
                    old_meta_by_cat[cat] = meta

            new_metas = []
            for fact in new_facts:
                cat = fact.partition(":")[0].strip() if ":" in fact else ""
                old = old_meta_by_text.get(fact.strip()) or old_meta_by_cat.get(cat) or {}
                new_metas.append({
                    "user_id": user_id,
                    "type": "long_term",
                    "origin_chat": old.get("origin_chat", ""),
                    "category": cat,
                    "user_name": old.get("user_name", ""),
                })
            # Эмбеддинги — вне лока (иначе add считал бы их под ним, и
            # читатели ждали бы модель); не вышло — посчитает сама коллекция
            try:
                embeddings = self.embedder(new_facts)
            except Exception as e:
                logger.warning(f"[LTM SUM] Эмбеддинги вне лока не посчитаны: {e}")
                embeddings = None

            with self._facts_lock:
                if self._epoch(user_id) != epoch:
                    print(f"  [LTM SUM] Память {user_id} очищена во время консолидации — результат отброшен")
                    return -1
                cur_ids = set(self.collection.get(where={"user_id": user_id},
                                                  include=[])["ids"])
                if not set(snap_ids) <= cur_ids:
                    # Снятый факт за время LLM-вызова удалили (forget, правка,
                    # UPDATE/слияние экстракции) — чистый набор его ещё
                    # содержит и воскресил бы. Отменяем, повторим в следующий раз
                    print(f"  [LTM SUM] Факты {user_id} изменились во время консолидации — отменена")
                    return SUMMARY_CONFLICT
                kept = len(cur_ids) - len(snap_ids)
                # Сначала чистые, потом удаление старых: сбой между шагами
                # оставит дубли, а не пустую память. Читатели под тем же локом
                # промежуточного состояния не видят.
                self.collection.add(
                    ids=[f"{user_id}_fact_{_unique_ms()}" for _ in new_facts],
                    documents=new_facts,
                    metadatas=new_metas,
                    **({"embeddings": embeddings} if embeddings is not None else {}),
                )
                self.collection.delete(ids=snap_ids)

            print(f"  [LTM SUM] Консолидация: {len(all_facts)} → {len(new_facts)} фактов"
                  + (f" (+{kept} добавлено за время консолидации)" if kept else ""))
            for f in new_facts:
                print(f"    {f}")
            return len(new_facts)

        except Exception as e:
            print(f"  [LTM SUM] Ошибка: {e}")
            import traceback
            traceback.print_exc()
            return -1

    def search(self, query: str, user_id: str = "default", limit: int = 5, chat_id: str = None) -> List[str]:
        # Семантический поиск фактов с учётом приватности (chat_id — текущий чат)
        with self._facts_lock:
            if self.collection.count() == 0:
                return []

            # В группе часть фактов отфильтруется по приватности — берём с запасом
            is_group = chat_id is not None and str(chat_id) != str(user_id)
            n_results = limit * 4 if is_group else limit

            results = self.collection.query(
                query_texts=[query],
                n_results=n_results,
                where={"user_id": user_id}
            )

            if not results["documents"]:
                return []

            docs = results["documents"][0]
            metas = results["metadatas"][0] if results.get("metadatas") else [{}] * len(docs)

            if not is_group:
                return docs

            filtered = []
            for doc, meta in zip(docs, metas):
                if self._fact_visible(meta, doc, user_id, chat_id):
                    filtered.append(doc)
                if len(filtered) >= limit:
                    break
            return filtered

    def get_all_facts(self, user_id: str = "default", chat_id: str = None) -> List[str]:
        # Все факты пользователя. Если задан chat_id — только видимые в этом чате
        with self._facts_lock:
            if self.collection.count() == 0:
                return []

            results = self.collection.get()
            if results and results["ids"]:
                return [
                    doc for doc, meta in zip(results.get("documents", []), results.get("metadatas", []))
                    if meta.get("user_id") == user_id
                    and self._fact_visible(meta, doc, user_id, chat_id)
                ]
            return []
    
    def get_facts_by_category(self, user_id: str, category: str, chat_id: str = None) -> List[str]:
        # Факты пользователя одной категории (с учётом приватности)
        with self._facts_lock:
            if self.collection.count() == 0:
                return []

            results = self.collection.get(where={"user_id": user_id})
            if not results or not results["ids"]:
                return []

            out = []
            for doc, meta in zip(results.get("documents", []), results.get("metadatas", [])):
                meta = meta or {}
                cat = meta.get("category") or (doc.partition(":")[0].strip() if ":" in doc else "")
                if cat != category:
                    continue
                if self._fact_visible(meta, doc, user_id, chat_id):
                    out.append(doc)
            return out

    def get_all_facts_with_meta(self, user_id: str = "default") -> List[Dict]:
        # Все факты пользователя с метаданными (для экспорта), без фильтра приватности
        with self._facts_lock:
            if self.collection.count() == 0:
                return []

            results = self.collection.get()
            if not results or not results["ids"]:
                return []

            facts = []
            for doc, meta in zip(results.get("documents", []), results.get("metadatas", [])):
                meta = meta or {}
                if meta.get("user_id") != user_id:
                    continue
                category = meta.get("category") or (doc.partition(":")[0].strip() if ":" in doc else "")
                facts.append({
                    "fact": doc,
                    "category": category,
                    "origin_chat": meta.get("origin_chat", ""),
                })
            return facts

    def get_chat_facts(self, chat_id: str, exclude_user_id: str = None, limit: int = 50) -> List[Dict]:
        """Факты ВСЕХ пользователей, узнанные в этом чате (origin_chat == chat_id).

        Это публичные для чата данные: сказанное здесь при всех можно обсуждать
        здесь со всеми. Факты из личных чатов сюда не попадают никогда.
        """
        with self._facts_lock:
            if self.collection.count() == 0:
                return []

            results = self.collection.get(
                where={"origin_chat": str(chat_id)},
                include=["documents", "metadatas"],
            )
            if not results or not results["ids"]:
                return []

            facts = []
            for doc, meta in zip(results.get("documents", []), results.get("metadatas", [])):
                meta = meta or {}
                if exclude_user_id is not None and meta.get("user_id") == str(exclude_user_id):
                    continue
                facts.append({
                    "fact": doc,
                    "category": meta.get("category", ""),
                    "user_id": meta.get("user_id", ""),
                    "user_name": meta.get("user_name", ""),
                })
                if len(facts) >= limit:
                    break
            return facts

    def forget(self, query: str, user_id: str = "default") -> Optional[str]:
        """
        Точечное забывание: ищет самый похожий факт пользователя и удаляет его.
        Возвращает текст удалённого факта или None, если похожего нет.
        """
        if self.collection.count() == 0:
            return None

        with self._facts_lock:
            results = self.collection.query(
                query_texts=[query],
                n_results=1,
                where={"user_id": user_id},
                include=["documents", "distances", "metadatas"],
            )
            if not results["ids"] or not results["ids"][0]:
                return None

            # cosine distance: 0 = идентично, 2 = противоположно (метрика
            # задана явно при открытии коллекции — chroma_space.VECTOR_SPACE).
            # Выше порога — считаем, что похожего факта нет, и не трогаем память.
            distance = results["distances"][0][0] if results.get("distances") else 2.0
            if distance > FORGET_MAX_DISTANCE:
                return None

            fact_id = results["ids"][0][0]
            doc = results["documents"][0][0]
            self.collection.delete(ids=[fact_id])
            logger.info(f"[LTM] Забыт факт для {user_id}: '{doc}' (distance={distance:.3f})")
            return doc

    def update_fact(self, old_query: str, new_text: str, user_id: str = "default") -> Optional[str]:
        """
        Замена факта новым текстом (ручная правка из веб-UI): находит факт
        пользователя (точное совпадение текста, иначе ближайший по смыслу —
        как в forget), удаляет его и сохраняет новый как есть, с origin/user_name
        старого. Возвращает текст заменённого факта или None, если не найден.
        """
        new_text = (new_text or "").strip()
        if not new_text or self.collection.count() == 0:
            return None

        with self._facts_lock:
            fact_id = None
            old_doc = None
            old_meta: Dict = {}

            # 1. Точное совпадение текста (UI присылает исходную строку факта)
            results = self.collection.get(
                where={"user_id": user_id},
                include=["documents", "metadatas"],
            )
            if results and results["ids"]:
                want = old_query.strip().lower()
                for rid, doc, meta in zip(results["ids"],
                                          results.get("documents", []),
                                          results.get("metadatas", [])):
                    if (doc or "").strip().lower() == want:
                        fact_id, old_doc, old_meta = rid, doc, meta or {}
                        break

            # 2. Семантический поиск — как в forget(), но порог жёстче:
            # правка должна попасть в ТОТ факт, а не в «самый похожий» —
            # промах здесь затирает чужой факт новым текстом
            if fact_id is None:
                res = self.collection.query(
                    query_texts=[old_query],
                    n_results=1,
                    where={"user_id": user_id},
                    include=["documents", "distances", "metadatas"],
                )
                if not res["ids"] or not res["ids"][0]:
                    return None
                distance = res["distances"][0][0] if res.get("distances") else 2.0
                if distance > UPDATE_FACT_MAX_DISTANCE:
                    return None
                fact_id = res["ids"][0][0]
                old_doc = res["documents"][0][0]
                old_meta = (res["metadatas"][0][0] or {}) if res.get("metadatas") else {}

            self.collection.delete(ids=[fact_id])

            # Сохраняем напрямую, минуя merge-логику _save_facts_impl:
            # ручная правка должна лечь в базу ровно так, как её написали
            cat_key = None
            if ":" in new_text:
                cat_raw, _, _ = new_text.partition(":")
                cat_key = cat_raw.strip()
            self.collection.add(
                ids=[f"{user_id}_fact_{_unique_ms()}"],
                documents=[new_text],
                metadatas=[{
                    "user_id": user_id,
                    "type": "long_term",
                    "origin_chat": old_meta.get("origin_chat", ""),
                    "category": cat_key or "",
                    "user_name": old_meta.get("user_name", ""),
                }]
            )
            logger.info(f"[LTM] Факт обновлён для {user_id}: '{old_doc}' → '{new_text}'")
            return old_doc

    def clear(self, user_id: str = "default"):
        """Стереть факты пользователя. Под _facts_lock и с новой эпохой:
        идущие экстракция/консолидация (они начались до очистки) при записи
        увидят смену эпохи и отбросят результат, а не воскресят факты."""
        with self._facts_lock:
            self._epochs[str(user_id)] = self._epochs.get(str(user_id), 0) + 1
            try:
                # Получаем все записи и фильтруем по user_id вручную
                results = self.collection.get()
                if results and results["ids"]:
                    ids_to_delete = [
                        rid for rid, meta in zip(results["ids"], results.get("metadatas", []))
                        if meta.get("user_id") == user_id
                    ]
                    if ids_to_delete:
                        self.collection.delete(ids=ids_to_delete)
                        print(f"  [LTM] Удалено {len(ids_to_delete)} фактов пользователя {user_id}")
                    else:
                        print(f"  [LTM] Нет фактов для пользователя {user_id}")
                else:
                    print(f"  [LTM] Коллекция пуста")
            except Exception as e:
                print(f"  [LTM] Ошибка при очистке LTM: {e}")

    def clear_all(self):
        """Стереть факты всех пользователей — как clear(), с общей эпохой:
        удаление мимо LTM (collection.delete напрямую) фоновые задачи не
        заметили бы и дописали факты в пустую память."""
        with self._facts_lock:
            self._epoch_all += 1
            results = self.collection.get(include=[])
            if results and results["ids"]:
                self.collection.delete(ids=results["ids"])


class MemoryManager:
    
    # Единый менеджер памяти — объединяет краткосрочную и долгосрочную память.

    def __init__(
        self,
        stm_size: int = 50,
        enable_ltm_extraction: bool = True,
        ltm_model_provider: str = None,
        stm_db_path: str = None,
        ltm_db_path: str = None,
        load_stm_from_db: bool = True,
        context: str = "default",
        main_router: 'ModelRouter' = None
    ):
        self.context = context
        self.stm = ShortTermMemory(
            max_messages=stm_size,
            db_path=stm_db_path,
            load_from_db=load_stm_from_db,
            context=context
        )
        self.ltm = LongTermMemory(
            ltm_model_provider=ltm_model_provider,
            db_path=ltm_db_path,
            context=context,
            main_router=main_router
        )
        self.enable_ltm_extraction = enable_ltm_extraction
        # Счётчики — ограниченные LRU-кеши, а не вечные dict: у бота в группах
        # user_id это каждый, кто когда-либо писал. Вытеснение безобидно:
        # счётчик начнётся заново, экстракция/консолидация случится позже.
        self._user_msg_counters = BoundedCache(max_entries=MAX_COUNTER_USERS)  # user_id → count (консолидация)
        self._extract_counters = BoundedCache(max_entries=MAX_COUNTER_USERS)   # user_id → count (батч-экстракция)
        self._counter_lock = threading.Lock()
        # «Консолидация идёт» — флаг под _counter_lock (см. _run_summarize_async)
        self._summary_running = False

    # Пакетная LTM-экстракция: каждые N сообщений диалога (user+assistant) —
    # один вызов на пачку новых N (обычный режим — 15, light — 6, под размер
    # контекста ответа), а не на каждое сообщение — иначе LLM через веб-чат
    # съедала бы дневную квоту и спамила служебный чат.
    EXTRACT_EVERY = 15
    EXTRACT_EVERY_LIGHT = 6

    def add_message(self, role: str, content: str, user_id: str = "default",
                    chat_id: str = None, user_name: str = None,
                    light_mode: bool = None):
        """
        Добавить сообщение в память.
        Факты для LTM извлекаются в фоновом потоке пакетами: каждые
        EXTRACT_EVERY (light — EXTRACT_EVERY_LIGHT) сообщений диалога —
        один вызов по сообщениям, накопленным с прошлой экстракции.

        Args:
            chat_id: ID чата для STM. Если None — STM использует user_id.
                     LTM всегда использует user_id (персональная).
            user_name: Имя пользователя для отображения в истории.
            light_mode: урезанный контекст (local-primary / light_context) —
                     интервал 6 вместо 15; None — определить по роутеру.
        """
        self.stm.add_message(role, content, user_id, chat_id, user_name)
        if role == "user":
            self.ltm.note_user_language(user_id, content)

        # Батч-экстракция: считаем ВСЕ сообщения диалога; вызов — на
        # пользовательском, когда новых накопилось ≥ every. Батч — все
        # сообщения с прошлой экстракции (покрытие без дыр).
        if self.enable_ltm_extraction and role in ("user", "assistant"):
            if light_mode is None:
                light_mode = bool(
                    self.ltm.main_router
                    and self.ltm.main_router.is_local_primary())
            every = self.EXTRACT_EVERY_LIGHT if light_mode else self.EXTRACT_EVERY
            with self._counter_lock:
                self._extract_counters[user_id] = self._extract_counters.get(user_id, 0) + 1
                count = self._extract_counters[user_id]
                extract_due = role == "user" and count >= every
                # Веб-вкладка ЭТОГО чата активна — экстракция ждёт: счётчик не
                # сбрасываем, батч доберётся при сообщении в неактивности
                # (чат другой персоны или чат другой платформы ничего не тормозит)
                if extract_due and web_presence.is_active(
                        self.context, chat_id or user_id):
                    extract_due = False
                if extract_due:
                    self._extract_counters[user_id] = 0
            if extract_due:
                # Только сообщения этого пользователя и ответы бота ему —
                # иначе в группе экстрактор сохранит чужие факты под его user_id
                own = [
                    msg for msg in self.stm.get_last(count * 2, chat_id=chat_id or user_id)
                    if msg.get("sender_id") in (None, user_id)
                ]
                batch_text = "\n".join(
                    f"{'User' if msg['role'] == 'user' else 'Assistant'}: {msg['content']}"
                    for msg in own[-count:]
                )
                if batch_text.strip():
                    # Язык — по репликам самого пользователя (метки ролей
                    # латиницей сбили бы детект по всему батчу)
                    batch_lang = detect_dialogue_language(
                        "", own[-count:], sender_id=user_id) or self.ltm.user_language(user_id)
                    self.ltm.extract_facts_async(batch_text, user_id, None,
                                                 origin_chat=chat_id, user_name=user_name,
                                                 lang=batch_lang)

        if role == "user" and self.enable_ltm_extraction:
            # Периодическая консолидация LTM
            with self._counter_lock:
                self._user_msg_counters[user_id] = self._user_msg_counters.get(user_id, 0) + 1
                due = self._user_msg_counters[user_id] >= SUMMARY_SETTINGS["trigger_every"]
            # Активная веб-вкладка ЭТОГО чата — консолидация ждёт:
            # счётчик не сбрасываем, повторим на следующем
            # сообщении. Консолидация идёт по user_id, но гейт — по чату, из
            # которого пришло сообщение: смысл гейта не «персона занята», а
            # «не тратим модель, пока человек ждёт ответа в этом чате»
            # Счётчик сбрасывает сам _run_summarize_async — только если
            # консолидация реально поставлена (до постановки: иначе быстрая
            # задача вернула бы счётчик к порогу, а сброс здесь его затёр)
            if due and not web_presence.is_active(self.context, chat_id or user_id):
                self._run_summarize_async(user_id)

    def _run_summarize_async(self, user_id: str) -> bool:
        # Запускает консолидацию LTM в фоне, с защитой от параллельного запуска.
        # Возвращает True, если задача поставлена в пул.
        # Защита — флаг под коротким локом, а не RLock на всё время задачи:
        # RLock принадлежит захватившему потоку, а release() звался бы из
        # потока пула — это бросает RuntimeError и оставляет лок занятым
        # навсегда, блокируя все следующие консолидации. Флаг снимает любой поток.
        with self._counter_lock:
            if self._summary_running:
                print("  [LTM SUM] Пропуск — консолидация уже запущена")
                return False
            self._summary_running = True
            prev_count = self._user_msg_counters.get(user_id, 0)
            self._user_msg_counters[user_id] = 0

        def _finish():
            with self._counter_lock:
                self._summary_running = False

        def _cancelled():
            # Задача так и не выполнится (пул не принял / остановлен):
            # снять флаг — иначе консолидация заблокирована навсегда — и
            # вернуть счётчик, чтобы попытка не потерялась
            with self._counter_lock:
                self._summary_running = False
                self._user_msg_counters[user_id] = max(
                    self._user_msg_counters.get(user_id, 0), prev_count)

        def _do():
            try:
                if self.ltm.summarize_user(user_id) == SUMMARY_CONFLICT:
                    # Отменена из-за правки фактов — счётчик обратно к порогу:
                    # повтор на следующем сообщении, а не через trigger_every
                    with self._counter_lock:
                        self._user_msg_counters[user_id] = max(
                            self._user_msg_counters.get(user_id, 0),
                            SUMMARY_SETTINGS["trigger_every"] - 1)
            finally:
                _finish()

        # В очередь пользователя — после экстракции, поставленной этим же
        # add_message (иначе её UPDATE/слияние отменили бы консолидацию)
        if not self.ltm._submit_serial(user_id, _do, on_cancel=_cancelled):
            print(f"  [LTM SUM] Не удалось запустить консолидацию для {user_id}")
            return False
        print(f"  [LTM SUM] Консолидация запущена в фоне для {user_id}")
        return True

    def get_context(self, user_id: str = "default", chat_id: str = None,
                    ltm_limit: int = 5, ltm_query: str = "",
                    stm_relevant_limit: int = 5, stm_recent_n: int = 15) -> Tuple[List[Dict], List[str], List[Dict]]:
        """
        Возвращает контекст для формирования промпта.

        Returns:
            (stm_messages, ltm_facts, stm_relevant)
            - stm_messages: последние n сообщений (хронология)
            - ltm_facts: факты из долгосрочной памяти
            - stm_relevant: семантически релевантные сообщения из STM (не из хронологии)
        """
        stm_messages = self.stm.get_last(stm_recent_n, chat_id=chat_id) if chat_id else self.stm.get_last(stm_recent_n, user_id)
        ltm_facts = (
            self.ltm.search(ltm_query, user_id, limit=ltm_limit, chat_id=chat_id)
            if ltm_query else self.ltm.get_all_facts(user_id, chat_id=chat_id)[:ltm_limit]
        )

        # Векторный поиск по STM — только для чатов (не для личных)
        stm_relevant = []
        if chat_id:
            stm_relevant = self.stm.search_relevant(
                query=ltm_query or "",
                chat_id=chat_id,
                limit=stm_relevant_limit,
                exclude_last_n=stm_recent_n
            )

        return stm_messages, ltm_facts, stm_relevant

    def get_context_for_prompt(self, user_id: str = "default", ltm_query: str = "") -> str:
        stm_messages = self.stm.get_last(10, user_id)
        ltm_facts = self.ltm.search(ltm_query, user_id) if ltm_query else self.ltm.get_all_facts(user_id)

        context_parts = []

        if ltm_facts:
            context_parts.append("Important information:")
            for fact in ltm_facts:
                context_parts.append(f"  - {fact}")

        if stm_messages:
            context_parts.append("\nRecent messages:")
            for msg in stm_messages[-5:]:
                role_ru = "User" if msg["role"] == "user" else "Assistant"
                context_parts.append(f"  {role_ru}: {msg['content'][:100]}")

        return "\n".join(context_parts)

    def search_ltm(self, query: str, user_id: str = "default", limit: int = 5) -> List[str]:
        return self.ltm.search(query, user_id, limit)

    def get_chat_facts_block(self, chat_id: str, exclude_user_id: str = None) -> Optional[str]:
        """Текстовый блок «факты об участниках этого чата» для промпта.

        Только факты, сказанные в этом чате (публичные для его участников);
        факты спрашивающего исключаются — они уже есть в его персональном блоке.
        """
        facts = self.ltm.get_chat_facts(chat_id, exclude_user_id=exclude_user_id)
        if not facts:
            return None
        lines = []
        for f in facts:
            name = f["user_name"] or get_user_tag(f["user_id"]) or "Participant"
            lines.append(f"  {name}: {f['fact']}")
        return "Facts about this chat's participants (said publicly here):\n" + "\n".join(lines)

    def clear_stm(self, chat_id: str = None):
        self.stm.clear(chat_id)

    def clear_ltm(self, user_id: str = "default"):
        self.ltm.clear(user_id)

    def get_stats(self, user_id: str = "default", chat_id: str = None) -> Dict:
        # LTM count — только для конкретного пользователя
        ltm_count = len(self.ltm.get_all_facts(user_id))
        stm_count = len(self.stm.get_messages(chat_id=chat_id)) if chat_id else len(self.stm.get_messages(user_id))
        return {
            "stm_count": stm_count,
            "stm_max": self.stm.max_messages,
            "ltm_count": ltm_count
        }
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from collections import deque
from typing import List, Dict, Optional, Tuple
from app.core.config import Config, get_db_paths
from app.core.router import ModelRouter
from app.core.memory_config import (
    build_extraction_prompt, should_ignore_message, parse_and_filter_facts,
    PROMPT_SETTINGS, UPDATE_CATEGORIES, APPEND_CATEGORIES, MERGE_SETTINGS,
    build_merge_prompt, build_summary_prompt, SUMMARY_SETTINGS, is_public_category
)
from app.core.users import get_user_tag
import time
import json
import threading
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


def _first_sentence(text: str, max_len: int = 80) -> str:
    """Обрезать текст до первого предложения. Если длиннее max_len — добавить ..."""
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
            context: Контекст — "tg", "gradio" или "default".
        """
        self.max_messages = max_messages
        self.buffers: Dict[str, deque] = {}  # {"chat_id": deque(maxlen=50)}
        self.context = context
        self._lock = threading.RLock()  # защита от гонки данных при многопоточности

        if db_path is None:
            db_path = get_db_paths(context)["stm"]

        self.client = chromadb.PersistentClient(path=db_path)
        self.embedder = SentenceTransformerEmbeddingFunction(
            model_name="paraphrase-multilingual-MiniLM-L12-v2"
        )
        self.collection = self.client.get_or_create_collection(
            "short_term_memory",
            embedding_function=self.embedder
        )

        if load_from_db:
            self._load_from_db()

    def _get_buffer(self, chat_id: str) -> deque:
        # Получить или создать буфер для чата (потокобезопасно)
        with self._lock:
            if chat_id not in self.buffers:
                self.buffers[chat_id] = deque(maxlen=self.max_messages)
            return self.buffers[chat_id]

    def _load_from_db(self):
        # Загрузить все сообщения из базы и раскидать по буферам чатов
        if self.collection.count() == 0:
            return

        results = self.collection.get(include=["documents", "metadatas"])

        if results["documents"]:
            messages = []
            for i, doc in enumerate(results["documents"]):
                metadata = results["metadatas"][i] if results["metadatas"] else {}
                msg_chat_id = metadata.get("chat_id") or metadata.get("user_id", "default")
                timestamp = metadata.get("timestamp", 0)
                role = metadata.get("role", "user")
                msg_user_name = metadata.get("user_name")
                sender_id = metadata.get("sender_id")
                if not msg_user_name and sender_id:
                    msg_user_name = get_user_tag(sender_id)
                messages.append({
                    "timestamp": timestamp,
                    "role": role,
                    "content": doc,
                    "chat_id": msg_chat_id,
                    "user_name": msg_user_name,
                    "sender_id": sender_id,
                })

            messages.sort(key=lambda x: x["timestamp"])
            with self._lock:
                for msg in messages:
                    buf = self.buffers.get(msg["chat_id"])
                    if buf is None:
                        buf = deque(maxlen=self.max_messages)
                        self.buffers[msg["chat_id"]] = buf
                    entry = {"role": msg["role"], "content": msg["content"], "chat_id": msg["chat_id"],
                             "timestamp": msg["timestamp"] / 1000}  # в БД миллисекунды, в буфере — секунды
                    if msg.get("user_name"):
                        entry["user_name"] = msg["user_name"]
                    if msg.get("sender_id"):
                        entry["sender_id"] = msg["sender_id"]
                    buf.append(entry)

    def _save_to_db(self, role: str, content: str, chat_id: str = "default",
                    user_name: str = None, sender_id: str = None):
        # Сохранить сообщение в базу данных.
        timestamp = int(time.time() * 1000)
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
        Добавление сообщения в буфер чата и сохранить в базу.

        Args:
            user_id: Реальный ID отправителя (Telegram user_id).
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
                # Пропускаем дубликаты с хронологией
                if doc in recent_contents:
                    continue
                # Пропускаем дубликаты внутри результатов
                if doc in seen:
                    continue
                seen.add(doc)

                entry = {
                    "role": meta.get("role", "user"),
                    "content": doc,
                    "chat_id": chat_id
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
        Возвращает список с role, user_name, content (обрезанное до первого предложения).
        """
        messages = self.get_last(n, chat_id=chat_id)
        result = []
        for m in messages:
            content = m.get("content", "")
            # Берём только первое предложение
            first_sentence = _first_sentence(content)
            result.append({
                "role": m.get("role", "user"),
                "user_name": m.get("user_name"),
                "content": first_sentence,
            })
        return result

    def pop_last_n(self, n: int, chat_id: str) -> int:
        """
        Удалить последние n сообщений из deque и ChromaDB.
        Возвращает количество удалённых.
        """
        with self._lock:
            buf = self.buffers.get(chat_id)
            if not buf:
                return 0

            # Берём последние n из deque
            to_remove = []
            for _ in range(min(n, len(buf))):
                if buf:
                    to_remove.append(buf.pop())

            if not to_remove:
                return 0

            # Ищем эти записи в ChromaDB по содержимому + chat_id
            # (chroma_id = stm_{chat_id}_{timestamp}, но мы храним timestamp в metadata)
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
            # Удаляем буфер чата
            with self._lock:
                if chat_id in self.buffers:
                    del self.buffers[chat_id]
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
    
    # Синглтон с экзекьтором (менеджер рабочих потоков) для контролирования потоков
    # Благодаря синглтону оба бота пользуются одним пулом из 3 потоков
    @classmethod
    def _get_executor(cls):
        # Проверка на созданый экзекьютор + замок
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
            context: Контекст — "tg", "gradio" или "default".
            main_router: Основной роутер бота. LTM пропустит его active_provider
                         чтобы не нагружать одну и ту же модель.
        """
        if db_path is None:
            db_path = get_db_paths(context)["ltm"]

        self.context = context
        self.client = chromadb.PersistentClient(path=db_path)
        self.embedder = SentenceTransformerEmbeddingFunction(
            model_name="paraphrase-multilingual-MiniLM-L12-v2"
        )
        self.collection = self.client.get_or_create_collection(
            "long_term_memory",
            embedding_function=self.embedder
        )
        
        self.ltm_model_provider = ltm_model_provider or Config.LTM_MODEL_PROVIDER
        self.main_router = main_router
        self.exclude_provider = main_router.active_provider if main_router else None
        if self.ltm_model_provider:
            self.llm_router = ModelRouter(provider=self.ltm_model_provider)
            print(f"  [LTM] LTM использует модель: {self.llm_router.get_provider_model_info()}")
        else:
            self.llm_router = ModelRouter()
            print(f"  [LTM] LTM использует активный провайдер: {self.llm_router.get_provider_model_info()}")
        if self.exclude_provider:
            print(f"  [LTM] Пропускает провайдер основной модели: {self.exclude_provider}")

        # Сериализует save_facts и summarize_user: read-modify-write по фактам
        # не атомарен, параллельные фоновые задачи иначе плодят дубли/теряют факты
        self._facts_lock = threading.RLock()

        # Режимы приватности LTM per user: "smart" (по умолчанию) | "strict"
        self._privacy_file = Path(db_path).parent / "ltm_privacy.json"
        self._privacy_modes: Dict[str, str] = self._load_privacy_modes()

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
        """Режим приватности пользователя: 'smart' (по умолчанию) или 'strict'."""
        return self._privacy_modes.get(str(user_id), "smart")

    def set_privacy_mode(self, user_id: str, mode: str) -> str:
        """Устанавливает режим приватности. Возвращает фактически установленный."""
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
                            origin_chat: str = None, user_name: str = None):
        """
        Запускает извлечение фактов в фоновом потоке.
        Не блокирует основной поток.
        origin_chat — чат, где факт был рассказан (для скоупа приватности).
        """
        def _extract_and_save():
            try:
                facts_raw = self.extract_facts(user_message, stm_context)
                if facts_raw:
                    facts_dict = parse_and_filter_facts(facts_raw)
                    if facts_dict:
                        # Дополнительная фильтрация перед записью в базу
                        safe_facts = {
                            k: v for k, v in facts_dict.items()
                            if not v.lower().startswith(("no ", "no_", "not ", "unknown", "n/a", "нет", "не "))
                            and not v.startswith("[NO_")  # фильтруем [NO_FACTS], [NO_PETS] и т.д.
                        }
                        for category, value in safe_facts.items():
                            fact_text = f"{category}: {value}"
                            self.save_facts(fact_text, user_id, origin_chat=origin_chat, user_name=user_name)
                        print(f"  [LTM] Сохранено фактов: {len(safe_facts)}")
                    else:
                        print(f"  [LTM] Факты отфильтрованы (пустые значения)")
                else:
                    print(f"  [LTM] Факты не найдены (фон)")
            except Exception as e:
                # ThreadPoolExecutor молча глотает исключения — перехватываем явно
                print(f"  [LTM] ОШИБКА в фоновой задаче: {e}")
                import traceback
                traceback.print_exc()

        executor = self._get_executor()
        future = executor.submit(_extract_and_save)

        # Дополнительный колбэк — ловит ошибки которые прошли мимо try/except
        def _log_future_error(f):
            exc = f.exception()
            if exc:
                print(f"  [LTM] ОШИБКА future: {exc}")

        future.add_done_callback(_log_future_error)
        print(f"  [LTM] Extraction запущен в фоне: '{user_message[:40]}...'")
    
    def extract_facts(self, user_message: str, stm_context: str = None) -> Optional[str]:
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
                    "If there are no facts — write only [NO_FACTS]."
                )
            },
            {"role": "user", "content": prompt}
        ]

        try:
            response = self.llm_router.get_response(
                messages,
                temperature=PROMPT_SETTINGS["temperature"],
                max_tokens=PROMPT_SETTINGS["max_tokens"],
                exclude_provider=self.exclude_provider,
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
                   user_name: str = None):
        """Потокобезопасная обёртка над _save_facts_impl."""
        with self._facts_lock:
            self._save_facts_impl(facts_text, user_id, origin_chat, user_name)

    def _save_facts_impl(self, facts_text: str, user_id: str = "default", origin_chat: str = None,
                         user_name: str = None):
        """
        Сохраняет извлечённые факты в векторную базу.

        origin_chat — чат, где факт был рассказан. Хранится в metadata и
        определяет приватность: непубличный факт виден только там (и в личке).
        
        Логика:
        - Полный дубликат → пропуск
        - UPDATE-категория (City, Age и т.д.) → замена старого значения
        - APPEND-категория (Hobby, Food и т.д.) → умное слияние через LLM
        - Новая категория → обычное сохранение
        """
        if "," in facts_text:
            facts_list = [f.strip() for f in facts_text.split(",") if f.strip()]
        else:
            facts_list = [f.strip() for f in facts_text.split("\n") if f.strip()]

        # Загружаем существующие факты: полные документы + разбор по категориям
        existing_docs = set()       # полные строки для проверки дубликатов
        existing_by_cat = {}        # category → (chroma_id, value)

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

        added = 0
        for i, fact in enumerate(facts_list):
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
                    # Умное слияние через LLM
                    merged = self._merge_append_fact(cat_key, old_val, new_val)
                    self.collection.delete(ids=[old_id])

                    if merged:
                        fact_stripped = f"{cat_key}: {merged}"
                        print(f"  [LTM] MERGE {cat_key}: '{old_val}' + '{new_val}' → '{merged}'")
                    else:
                        # Слияние не удалось — сохраняем как есть
                        print(f"  [LTM] MERGE не удался для {cat_key}, сохраняю как есть")
                else:
                    # Категория без явного типа — сохраняем обе (старый подход)
                    pass

                # Обновляем маппинг категории на новый факт
                existing_by_cat[cat_key] = ("_pending_", new_val)

            # 3. Сохраняем факт (новый или обновлённый/слитый)
            self.collection.add(
                ids=[f"{user_id}_fact_{int(time.time() * 1000) + i}"],
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
            added += 1

        if added > 0:
            print(f"  [LTM] Сохранено {added} фактов (из {len(facts_list)})")

    def _merge_append_fact(self, category: str, existing: str, new_value: str) -> Optional[str]:
        """
        Гибридное слияние фактов APPEND-категории.
        Сначала пробует ручное объединение, если сложно — вызывает LLM.
        """
        # 1. Быстрое ручное объединение
        existing_items = [item.strip().lower() for item in existing.split(",")]
        new_items = [item.strip().lower() for item in new_value.split(",")]
        
        # 2. Если нет пересечений и список короткий — делаем вручную
        if len(existing_items) + len(new_items) <= 5 and not set(existing_items) & set(new_items):
            all_items = list(set(existing_items + new_items))
            return ", ".join(sorted(all_items))
        
        # 3. Если есть подозрение на дубликаты или сложный случай — LLM
        return self._merge_with_llm(category, existing, new_value)

    def _merge_with_llm(self, category: str, existing: str, new_value: str) -> Optional[str]:
        """
        Умное слияние фактов APPEND-категории через LLM.
        Возвращает объединённое значение или None при ошибке.
        """
        prompt = build_merge_prompt(category, existing, new_value)

        messages = [
            {
                "role": "system",
                "content": "You merge values for long-term memory. Output ONLY the final merged value."
            },
            {"role": "user", "content": prompt}
        ]

        try:
            response = self.llm_router.get_response(
                messages,
                temperature=MERGE_SETTINGS["temperature"],
                max_tokens=MERGE_SETTINGS["max_tokens"],
                exclude_provider=self.exclude_provider,
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
        """Потокобезопасная обёртка: консолидация не должна пересекаться с save_facts."""
        with self._facts_lock:
            return self._summarize_user_impl(user_id)

    def _summarize_user_impl(self, user_id: str = "default") -> int:
        """
        Периодическая консолидация LTM для пользователя.
        LLM получает все факты, чистит противоречия и дубликаты,
        затем старые факты удаляются и записываются чистые.
        
        Returns: количество фактов после консолидации, или -1 при ошибке.
        """
        all_facts = self.get_all_facts(user_id)
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
                    "No explanations, no markdown, no bullet points."
                )
            },
            {"role": "user", "content": prompt}
        ]

        try:
            response = self.llm_router.get_response(
                messages,
                temperature=SUMMARY_SETTINGS["temperature"],
                max_tokens=SUMMARY_SETTINGS["max_tokens"],
                exclude_provider=self.exclude_provider,
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

            # Сохраняем метаданные старых фактов: после перезаписи нужно восстановить
            # origin_chat/category/user_name, иначе приватность фактов слетит
            old_meta_by_text = {}
            old_meta_by_cat = {}
            old_rows = self.collection.get(where={"user_id": user_id}, include=["documents", "metadatas"])
            for doc, meta in zip(old_rows.get("documents", []), old_rows.get("metadatas", [])):
                meta = meta or {}
                old_meta_by_text[doc.strip()] = meta
                cat = meta.get("category") or (doc.partition(":")[0].strip() if ":" in doc else "")
                if cat and cat not in old_meta_by_cat:
                    old_meta_by_cat[cat] = meta

            # Удаляем все старые факты пользователя
            self.clear(user_id)

            # Записываем чистые
            for i, fact in enumerate(new_facts):
                cat = fact.partition(":")[0].strip() if ":" in fact else ""
                old = old_meta_by_text.get(fact.strip()) or old_meta_by_cat.get(cat) or {}
                self.collection.add(
                    ids=[f"{user_id}_fact_{int(time.time() * 1000) + i}"],
                    documents=[fact],
                    metadatas=[{
                        "user_id": user_id,
                        "type": "long_term",
                        "origin_chat": old.get("origin_chat", ""),
                        "category": cat,
                        "user_name": old.get("user_name", ""),
                    }]
                )

            print(f"  [LTM SUM] Консолидация: {len(all_facts)} → {len(new_facts)} фактов")
            for f in new_facts:
                print(f"    {f}")
            return len(new_facts)

        except Exception as e:
            print(f"  [LTM SUM] Ошибка: {e}")
            import traceback
            traceback.print_exc()
            return -1

    def search(self, query: str, user_id: str = "default", limit: int = 5, chat_id: str = None) -> List[str]:
        """Семантический поиск фактов с учётом приватности (chat_id — текущий чат)."""
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
        """Все факты пользователя. Если задан chat_id — только видимые в этом чате."""
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
        """Факты пользователя одной категории (с учётом приватности)."""
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
        """Все факты пользователя с метаданными (для экспорта). Без фильтра приватности."""
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

            # cosine distance: 0 = идентично, 2 = противоположно.
            # Выше порога — считаем, что похожего факта нет, и не трогаем память.
            distance = results["distances"][0][0] if results.get("distances") else 2.0
            if distance > 1.0:
                return None

            fact_id = results["ids"][0][0]
            doc = results["documents"][0][0]
            self.collection.delete(ids=[fact_id])
            logger.info(f"[LTM] Забыт факт для {user_id}: '{doc}' (distance={distance:.3f})")
            return doc

    def clear(self, user_id: str = "default"):
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
        self._user_msg_counters = {}  # user_id → count
        self._summary_lock = threading.RLock()
        self._counter_lock = threading.Lock()  # отдельный: summary lock держится долго

    def add_message(self, role: str, content: str, user_id: str = "default",
                    chat_id: str = None, user_name: str = None):
        """
        Добавить сообщение в память.
        Автоматически извлекает факты для LTM в фоновом потоке.

        Args:
            chat_id: ID чата для STM. Если None — STM использует user_id.
                     LTM всегда использует user_id (персональная).
            user_name: Имя пользователя для отображения в истории.
        """
        self.stm.add_message(role, content, user_id, chat_id, user_name)

        if role == "user" and self.enable_ltm_extraction:
            # Контекст — последние 5 сообщений из чата
            # (chat_id=None без fallback читал бы буферы ВСЕХ чатов — утечка контекста)
            stm_messages = self.stm.get_last(5, chat_id=chat_id or user_id)
            # В группе оставляем только сообщения ЭТОГО пользователя и ответы бота ему —
            # иначе экстрактор сохранит чужие факты под его user_id
            own_messages = [
                msg for msg in stm_messages
                if msg.get("sender_id") in (None, user_id)
            ]
            stm_context = "\n".join([
                f"{msg.get('user_name', 'User') if msg['role'] == 'user' else 'Assistant'}: {msg['content']}"
                for msg in own_messages[:-1]
            ]) if len(own_messages) > 1 else None

            self.ltm.extract_facts_async(content, user_id, stm_context, origin_chat=chat_id, user_name=user_name)

            # Периодическая консолидация LTM
            with self._counter_lock:
                self._user_msg_counters[user_id] = self._user_msg_counters.get(user_id, 0) + 1
                due = self._user_msg_counters[user_id] >= SUMMARY_SETTINGS["trigger_every"]
            if due and self._run_summarize_async(user_id):
                # Сбрасываем счётчик только если консолидация реально запустилась
                with self._counter_lock:
                    self._user_msg_counters[user_id] = 0

    def _run_summarize_async(self, user_id: str) -> bool:
        # Запускает консолидацию LTM в фоне, с защитой от параллельного запуска.
        # Возвращает True, если задача поставлена в пул.
        if not self._summary_lock.acquire(blocking=False):
            print("  [LTM SUM] Пропуск — консолидация уже запущена")
            return False

        def _do():
            try:
                self.ltm.summarize_user(user_id)
            finally:
                self._summary_lock.release()

        executor = self.ltm._get_executor()
        future = executor.submit(_do)
        future.add_done_callback(lambda f: f.exception() and print(f"  [LTM SUM] ОШИБКА: {f.exception()}"))
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
            context_parts.append("Важная информация:")
            for fact in ltm_facts:
                context_parts.append(f"  - {fact}")

        if stm_messages:
            context_parts.append("\nПоследние сообщения:")
            for msg in stm_messages[-5:]:
                role_ru = "Пользователь" if msg["role"] == "user" else "Ассистент"
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
            name = f["user_name"] or get_user_tag(f["user_id"]) or "Участник"
            lines.append(f"  {name}: {f['fact']}")
        return "Факты об участниках этого чата (сказанные здесь публично):\n" + "\n".join(lines)

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
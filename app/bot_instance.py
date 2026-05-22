"""
BotInstance — один бот с конкретной персоной и набором фичей.
Содержит VirtualPersonality, FileVectorDB и читает features из YAML.
"""

import re
import yaml
import logging
from pathlib import Path
from typing import Optional, Dict, List
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from app.core.persona import PersonaLayer
from app.core.memory import MemoryManager
from app.core.router import ModelRouter
from app.core.config import Config
from app.core.file_vector_db import FileVectorDB
from app.core.file_reader import extract_text, MAX_FILE_SIZE_DEFAULT

logger = logging.getLogger(__name__)


class BotInstance:
    """
    Единый экземпляр бота.
    Каждому Telegram-боту (Коннор, Арродес) соответствует свой BotInstance.
    """

    def __init__(self, persona_name: str, context: str = None):
        self.persona_name = persona_name
        self.context = context or persona_name
        self.persona = PersonaLayer(persona_name=persona_name)

        # Читаем features из YAML
        persona_data = self.persona.persona_data
        self.features: dict = persona_data.get("features", {}) # получаем навыки персоны

        # STM size из YAML (fallback на Config)
        self.stm_size: int = persona_data.get("stm_size", Config.STM_SIZE)

        # Max docs из YAML (fallback на дефолт 3)
        self.max_docs: int = persona_data.get("max_docs", 3)

        # Max file size из YAML в МБ (fallback на дефолт 10 МБ)
        max_file_size_mb = persona_data.get("max_file_size_mb", 10)
        self.max_file_size: int = max_file_size_mb * 1024 * 1024

        # Trigger words
        self.trigger_words: set = set(self.features.get("trigger_words", [persona_name.lower()]))

        # File DB (только если file_upload)
        self.file_db: Optional[FileVectorDB] = None
        if self.features.get("file_upload", False):
            self.file_db = FileVectorDB(context=context, max_docs=self.max_docs)
            logger.info(f"  [{persona_name}] FileVectorDB включён")

        # Router (создаём до Memory, чтобы передать в LTM)
        self.router = ModelRouter()

        # Memory + Router
        self.memory = MemoryManager(
            stm_size=self.stm_size,
            enable_ltm_extraction=Config.LTM_EXTRACTION_ENABLED,
            ltm_model_provider=Config.LTM_MODEL_PROVIDER,
            load_stm_from_db=True,
            context=context,
            main_router=self.router
        )

        # Web search
        self._web_search_enabled = self.features.get("web_search", False)
        self._web_pool = None
        if self._web_search_enabled:
            from app.features.web_search import search_web, format_web_results
            self._search_web = search_web
            self._format_web_results = format_web_results
            self._web_pool = ThreadPoolExecutor(max_workers=1)
            logger.info(f"  [{persona_name}] Web search включён (pool: 1 worker)")

        # Rate limiter
        self._rate_limit_enabled = self.features.get("rate_limit", False)
        self._rate_limit_individual: dict = {}
        if self._rate_limit_enabled:
            from app.features.rate_limiter import check_rate_limit, block_user, is_blocked, get_status_text
            self._check_rate_limit = check_rate_limit
            self._block_user = block_user
            self._is_blocked = is_blocked
            self._rate_limit_status = get_status_text
            # Парсим individual limits из YAML (ключи-строки)
            raw = self.features.get("rate_limit_individual", {})
            self._rate_limit_individual = {str(k): v for k, v in raw.items()}
            logger.info(f"  [{persona_name}] Rate limiter включён ({len(self._rate_limit_individual)} индивидуальных)")

        # Moderation
        self._moderation_enabled = self.features.get("moderation", False)
        if self._moderation_enabled:
            from app.features.moderation import moderate_message
            self._moderate_message = moderate_message
            logger.info(f"  [{persona_name}] Модерация включена")

        # Punish block
        self._punish_enabled = self.features.get("punish_block", False)

        # Владелец — полная защита от всех блокировок
        self.owner: str = str(self.features.get("owner", ""))

        # Allowed DM users — могут писать в личку, но подлежат наказаниям
        self.allowed_dm_users: set = set(self.features.get("allowed_dm_users", []))
        self.blocked_users: set = set(self.features.get("blocked_users", []))

        logger.info(f"  [{persona_name}] BotInstance создан | stm_size={self.stm_size} | features: {list(self.features.keys())}")

    # Trigger logic

    def should_respond(self, text: str) -> bool:
        lower = text.strip().lower()
        for trigger in self.trigger_words:
            if lower.startswith(trigger):
                return True
        return False

    def strip_trigger(self, text: str) -> str:
        lower = text.strip().lower()
        for trigger in sorted(self.trigger_words, key=len, reverse=True):
            if lower.startswith(trigger):
                return text.strip()[len(trigger):].strip().lstrip(",.!?:; ")
        return text

    # Pre-check pipeline

    def pre_check(self, user_id: str, text: str, is_private: bool) -> Optional[str]:
        """
        Проверки перед обработкой. Возвращает текст ошибки или None если всё ОК.
        """
        # 0. Владелец — полная защита
        if user_id == self.owner:
            return None

        # 1. Заблокированные
        if user_id in self.blocked_users:
            return "BLOCKED"

        # 2. DM только для разрешённых
        if is_private and user_id not in self.allowed_dm_users:
            return "BLOCKED"

        # 3. Punish block
        if self._punish_enabled and self._is_blocked(user_id):
            return "PUNISH_BLOCKED"

        # 4. Rate limit
        if self._rate_limit_enabled and not self._check_rate_limit(user_id, self._rate_limit_individual):
            return "RATE_LIMITED"

        # 5. Moderation
        if self._moderation_enabled and self._moderate_message(text):
            if self._punish_enabled:
                self._block_user(user_id)
            return "MODERATION_BLOCKED"

        return None

    # Main processing

    def process_message(self, user_input: str, user_id: str = "default",
                        chat_id: str = None, user_name: str = None) -> str:
        # 1. Запускаем веб-поиск в фоне (параллельно с памятью)
        web_future = None
        if self._web_search_enabled and not self._is_docs_only_request(user_input):
            web_future = self._web_pool.submit(self._search_web, user_input, 5)

        try:
            self.memory.add_message("user", user_input, user_id, chat_id, user_name)
            stm_messages, ltm_facts = self.memory.get_context(user_id, chat_id, ltm_query=user_input)
            file_context = None
            if self.file_db:
                if self._is_full_doc_request(user_input):
                    full_text = self.file_db.get_full_document(user_id)
                    if full_text:
                        file_context = f"Полный текст загруженного документа:\n{full_text}"
                else:
                    file_chunks = self.file_db.search(user_id=user_id, query=user_input, limit=5)
                    if file_chunks:
                        file_context = "Контекст из загруженных файлов:\n" + "\n---\n".join(file_chunks)
            web_context = None
            if web_future is not None:
                try:
                    results = web_future.result(timeout=10)
                    if results:
                        web_context = self._format_web_results(results)
                except FuturesTimeoutError:
                    pass
                except Exception:
                    pass
            context_parts_out = []
            if ltm_facts:
                context_parts_out.append("\n".join(ltm_facts))
            if file_context:
                context_parts_out.append(file_context)
            memory_text = "\n\n".join(context_parts_out) if context_parts_out else None
            has_files = file_context is not None
            messages = self.persona.prepare_messages(
                user_input, memory_text, history=stm_messages,
                user_id=user_id, user_name=user_name, web_context=web_context,
                has_files=has_files
            )
            settings = self.persona.get_settings()
            answer = self.router.get_response(messages, **settings)

            # 9. Punish parsing
            if self._punish_enabled:
                answer = self._parse_punishment(answer, user_id)

            # 10. Сохраняем ответ
            self.memory.add_message("assistant", answer, user_id, chat_id)

            return answer
        finally:
            # Ничего не делаем — пул живёт всё время жизни бота
            pass

    def _parse_punishment(self, response: str, user_id: str) -> str:
        #Парсит маркеры наказания, выполняет действия.
        if "[PUNISH:BLOCK]" in response:
            response = response.replace("[PUNISH:BLOCK]", "").strip()
            self._block_user(user_id)
            logger.info(f"Пользователь {user_id} заблокирован (PUNISH:BLOCK)")

        fact_match = re.search(r'\[PUNISH:FACT:(.+?)\]', response)
        if fact_match:
            fact_text = fact_match.group(1).strip()
            response = response[:fact_match.start()] + response[fact_match.end():]
            response = response.strip()
            self.inject_fact(fact_text, user_id)
            logger.info(f"Пользователь {user_id} — подставной факт: {fact_text}")

        return response

    # File helpers

    _FULL_DOC_KEYWORDS = [
        "перескажи", "пересказ", "резюме", "суммаризуй", "суммаризация",
        "краткое содержание", "основная мысль", "главная идея",
        "перепиши текст", "изложи", "выжимка",
        "расскажи содержание", "о чём документ", "о чем документ",
        "расскажи текст", "весь текст", "полный текст",
        "доклад по", "анализ документа", "разбор документа", "проанализируй"
    ]

    def _is_full_doc_request(self, text: str) -> bool:
        # Определяет, просит ли пользователь пересказ/анализ документа целиком.
        lower = text.lower()
        return any(kw in lower for kw in self._FULL_DOC_KEYWORDS)

    _DOCS_ONLY_KEYWORDS = [
        "только документ", "только файл", "только из документ",
        "по документу", "по файлу", "из файла", "из документа",
        "без поиска", "не ищи", "не используй поиск",
        "без интернета", "без веб", "offline",
        "only documents", "no search", "without search",
    ]

    def _is_docs_only_request(self, text: str) -> bool:
        """Пользователь просит ответить только по документам — без веб-поиска."""
        lower = text.lower()
        return any(kw in lower for kw in self._DOCS_ONLY_KEYWORDS)

    # Memory helpers

    def get_memory_stats(self, user_id: str = "default", chat_id: str = None) -> dict:
        return self.memory.get_stats(user_id, chat_id)

    def clear_memory(self, user_id: str = "default", chat_id: str = None):
        self.memory.clear_stm(chat_id)
        self.memory.clear_ltm(user_id)

    def clear_ltm_only(self, user_id: str = "default"):
        self.memory.clear_ltm(user_id)

    def inject_fact(self, fact_text: str, user_id: str = "default"):
        self.memory.ltm.save_facts(fact_text, user_id)

    def clear_all_memory(self):
        self.memory.clear_stm()
        try:
            results = self.memory.ltm.collection.get()
            if results and results["ids"]:
                self.memory.ltm.collection.delete(ids=results["ids"])
        except Exception:
            pass

    def get_rate_limit_status(self) -> str:
        if self._rate_limit_enabled:
            return self._rate_limit_status(self._rate_limit_individual)
        return "Rate limiter не активен."

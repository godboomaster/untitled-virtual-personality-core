"""
BotInstance — один бот с конкретной персоной и набором фич.
Содержит VirtualPersonality, FileVectorDB и читает features из YAML.
"""

import re
import os
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
    
    # Каждому Telegram-боту (Коннор, Арродес) соответствует свой BotInstance.


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
            load_stm_from_db=not persona_data.get("fresh_stm", False),
            context=context,
            main_router=self.router
        )

        # Web search
        self._web_search_enabled = self.features.get("web_search", False)
        self._web_search_disabled_chats: set = set()  # chat_id где /web выключил поиск
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

            # Парсим individual limits из env (RATE_LIMIT_USER_<ID>=<seconds>)
            self._rate_limit_individual = {}
            for key, value in os.environ.items():
                if key.startswith("RATE_LIMIT_USER_"):
                    uid = key[len("RATE_LIMIT_USER_"):]
                    try:
                        self._rate_limit_individual[uid] = int(value)
                    except ValueError:
                        pass
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

        # Self memory (эпизодическая память бота)
        self.self_memory = None
        if self.features.get("self_memory", False):
            from app.core.self_memory import BotSelfMemory
            self.self_memory = BotSelfMemory(
                context=context,
                persona_name=persona_name,
                router=self.router
            )

        # Book search (RAG по книге для персон)
        self.book_search = None
        if self.features.get("book_search", False):
            from app.features.book_search import BookSearch
            self.book_search = BookSearch(context=context or persona_name)
            logger.info(f"  [{persona_name}] Book search включён")

        # Proactive messaging (самоинициатива)
        self.proactive = None
        self._activity_tracker = None
        proactive_config = self.features.get("proactive", {})
        if proactive_config.get("enabled", False):
            from app.features.proactive_messaging import ProactiveConfig, ProactiveMessaging, ChatActivityTracker
            self._activity_tracker = ChatActivityTracker(context=context)
            self.proactive = ProactiveMessaging(
                config=ProactiveConfig.from_dict(proactive_config),
                router=self.router,
                persona=self.persona,
                memory=self.memory,
                activity_tracker=self._activity_tracker,
                get_last_message_time=self._get_last_message_time,
                send_message=self._send_proactive_message,
                context=context,
                self_memory=self.self_memory,
            )
            logger.info(f"  [{persona_name}] Proactive messaging включён")

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

        # Проверки перед обработкой. Возвращает текст ошибки или None если всё ОК.
        
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
                        chat_id: str = None, user_name: str = None,
                        reply_context: str = None) -> str:
        # 1. Запускаем веб-поиск в фоне (параллельно с памятью)
        web_future = None
        if self._web_search_enabled and chat_id not in self._web_search_disabled_chats and not self._is_docs_only_request(user_input):
            web_future = self._web_pool.submit(self._search_web, user_input, 5)

        try:
            self.memory.add_message("user", user_input, user_id, chat_id, user_name)
            stm_messages, ltm_facts, stm_relevant = self.memory.get_context(
                user_id, chat_id, ltm_query=user_input
            )
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

            # Получаем блок личной памяти бота
            self_memory_block = None
            if self.self_memory:
                self_memory_block = self.self_memory.get_context_block()

            # Формируем блок релевантного STM-контекста
            stm_relevant_text = None
            if stm_relevant:
                parts = []
                for msg in stm_relevant:
                    role_ru = msg.get("user_name", "Пользователь") if msg["role"] == "user" else "Ассистент"
                    parts.append(f"  {role_ru}: {msg['content'][:200]}")
                stm_relevant_text = "\n".join(parts)

            # Intent classification — нужен ли контекст книги?
            _book_intent = "book_only"
            if self.book_search:
                try:
                    from app.features.intent_router import classify_intent
                    _book_intent = classify_intent(user_input, stm_messages)
                    logger.info(f"[IntentRouter] intent={_book_intent} for: '{user_input[:60]}'")
                except Exception as ie:
                    logger.debug(f"Intent classification error: {ie}")

            # Поиск по книге (RAG) — пропускаем при chat_only
            book_context = None
            context_mode = "book"
            if self.book_search and _book_intent != "chat_only":
                try:
                    context_mode = "mixed" if _book_intent == "mixed" else "book"
                    from app.features.book_search import detect_volume

                    def _detect_position(text: str):
                        q = text.lower()
                        start_kw = ["начал", "в начале", "начало", "первых главах", "первые главы"]
                        end_kw = ["конц", "в конце", "конец", "последних главах", "последние главы"]
                        if any(w in q for w in start_kw):
                            return "start"
                        if any(w in q for w in end_kw):
                            return "end"
                        return None

                    # Volume определяется внутри search() после перевода
                    volume = None
                    position = _detect_position(user_input)

                    # Position-запросы → summaries вместо chunk-поиска
                    if position is not None and volume is not None:
                        import json, re
                        # _db_path = "data/arrodes/book" -> context_dir = "data/arrodes"
                        context_dir = "/".join(self.book_search._db_path.split("/")[:-1])
                        summaries_path = f"{context_dir}/summaries.json"
                        try:
                            with open(summaries_path, encoding="utf-8") as sf:
                                all_summaries = json.load(sf)
                        except FileNotFoundError:
                            summaries_path = "data/arrodes/summaries.json"
                            with open(summaries_path, encoding="utf-8") as sf:
                                all_summaries = json.load(sf)

                        # Диапазоны глав по томам LotM
                        VOL_RANGES = {
                            1: (1, 213), 2: (214, 408), 3: (409, 600),
                            4: (601, 783), 5: (784, 980), 6: (981, 1138),
                        }
                        lo, hi = VOL_RANGES.get(volume, (1, 9999))
                        total_chapters = hi - lo + 1
                        n_take = min(20, total_chapters)
                        if position == "start":
                            ch_lo, ch_hi = lo, lo + n_take - 1
                        else:
                            ch_lo, ch_hi = hi - n_take + 1, hi

                        selected = []
                        for k, v in all_summaries.items():
                            m = re.search(r"Chapter (\d+):", k)
                            if m and ch_lo <= int(m.group(1)) <= ch_hi:
                                selected.append((int(m.group(1)), k, v))
                        selected.sort(key=lambda x: x[0])

                        if selected:
                            lines = [f"[Summaries for Volume {volume}, {'beginning' if position == 'start' else 'end'}]"]
                            for ch_num, title, summary in selected:
                                lines.append(f"{title}\n{summary}")
                            book_context = "\n\n".join(lines)
                            logger.info(f"[BookContext] Position query: {position} vol={volume}, loaded {len(selected)} chapter summaries ({len(book_context)} chars)")
                        else:
                            logger.info(f"[BookContext] Position query: no summaries found for vol={volume} {position}")
                    else:
                        # Обычный RAG-поиск
                        if volume is not None:
                            logger.info(f"[BookSearch] Detected volume filter: {volume}")
                        _n = 5 if _book_intent == "mixed" else 25
                        # История диалога строками — для резолюции местоимений
                        # (last N messages: user + assistant, текущая не входит).
                        _coref_history = [
                            m["content"] for m in stm_messages
                            if isinstance(m, dict) and m.get("content")
                        ][-6:]
                        fragments = self.book_search.search(
                            user_input, volume=volume, n_results=_n,
                            history=_coref_history,
                        )
                        translated_query = self.book_search.translate_query(user_input)
                        if fragments:
                            from app.features.book_context import build_context_block
                            book_context = build_context_block(
                                fragments,
                                original_query=user_input,
                                translated_query=translated_query,
                                mode=context_mode
                            )
                            logger.info(f"[BookContext] {len(fragments)} fragments, {len(book_context)} chars for query: '{user_input[:60]}'")
                        else:
                            from app.features.book_context import build_context_block
                            book_context = build_context_block(
                                [],
                                original_query=user_input,
                                translated_query=translated_query,
                                mode=context_mode
                            )
                            logger.info(f"[BookContext] No fragments for query: '{user_input[:60]}'")
                except Exception as e:
                    logger.debug(f"Book search error: {e}")

            messages = self.persona.prepare_messages(
                user_input, memory_text, history=stm_messages,
                user_id=user_id, user_name=user_name, web_context=web_context,
                has_files=has_files, self_memory_block=self_memory_block,
                reply_context=reply_context, stm_relevant=stm_relevant_text,
                book_context=book_context
            )
            settings = self.persona.get_settings()
            answer = self.router.get_response(messages, **settings)

            # 9. Punish parsing
            if self._punish_enabled:
                answer = self._parse_punishment(answer, user_id)

            # 10. Сохраняем ответ
            self.memory.add_message("assistant", answer, user_id, chat_id)

            # 11. Эпизодическая память (self_memory)
            if self.self_memory:
                self.self_memory.tick(stm_messages, user_id, user_input)

            return answer
        finally:
            # Ничего не делаем — пул живёт всё время жизни бота
            pass

    def _clean_response(self, response: str) -> str:
        # Очищает ответ от лишнего Markdown-форматирования.
        if not response:
            return response
        response = self._strip_markdown(response)
        return response.strip()

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """ Удаляет Markdown-разметку, которую Telegram не поддерживает.
        Жирный (**), курсив (*), код (`) — остаётся, их конвертит _md_to_html.
        Code-блоки (```...```) не трогаются — их обрабатывает file_sender."""
        
        # Сохраняем блоки кода, чтобы не повредить их чисткой
        # Заменяем на специальные редкие символы
        code_blocks = []
        def _save(m):
            code_blocks.append(m.group(0))
            return f'\x00CB{len(code_blocks) - 1}\x00'
        text = re.sub(r'```.*?```', _save, text, flags=re.DOTALL)

        # Пустые маркеры: **\n\n** или *** без контента убрать
        text = re.sub(r'\*{2,}\s*\*{2,}', '', text)
        # Заголовки: #### Заголовок → Заголовок
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
        # Изображения: ![alt](url) → alt
        text = re.sub(r'!\[(.+?)\]\(.+?\)', r'\1', text)
        # Горизонтальная линия: --- → юникод-разделитель
        text = re.sub(r'^-{3,}\s*$', '───────────', text, flags=re.MULTILINE)
        # Горизонтальная линия: *** или ___ → пустая строка
        text = re.sub(r'^[*_]{3,}\s*$', '', text, flags=re.MULTILINE)
        # Убираем лишние пустые строки
        text = re.sub(r'\n{3,}', '\n\n', text)

        # Восстанавливаем code-блоки
        for i, block in enumerate(code_blocks):
            text = text.replace(f'\x00CB{i}\x00', block)
        return text.strip()

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
        # Пользователь просит ответить только по документам — без веб-поиска.
        lower = text.lower()
        return any(kw in lower for kw in self._DOCS_ONLY_KEYWORDS)

    # Memory helpers

    def get_memory_stats(self, user_id: str = "default", chat_id: str = None) -> dict:
        return self.memory.get_stats(user_id, chat_id)

    def get_stm_last_display(self, n: int, chat_id: str) -> list:
        return self.memory.stm.get_last_display(n, chat_id)

    def stm_pop_last_n(self, n: int, chat_id: str) -> int:
        return self.memory.stm.pop_last_n(n, chat_id)

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

    def toggle_web_search(self, chat_id: str) -> bool:
        # Переключает web_search для чата. Возвращает новое состояние (True=включён).
        if not self._web_search_enabled:
            return False
        if chat_id in self._web_search_disabled_chats:
            self._web_search_disabled_chats.discard(chat_id)
            return True
        else:
            self._web_search_disabled_chats.add(chat_id)
            return False

    def get_rate_limit_status(self) -> str:
        if self._rate_limit_enabled:
            return self._rate_limit_status(self._rate_limit_individual)
        return "Rate limiter не активен."

    # Proactive messaging helpers

    def _get_last_message_time(self, chat_id: str) -> float:
        """Возвращает timestamp последнего сообщения в чате.
        Сначала смотрит в activity_tracker, потом в STM буфер."""
        # 1. Смотрим в activity tracker (сохраняется между перезапусками)
        if self._activity_tracker:
            ts = self._activity_tracker.get_last_activity(chat_id)
            if ts > 0:
                return ts

        # 2. Fallback: смотрим в STM буфер (текущая сессия)
        try:
            messages = self.memory.stm.get_messages(chat_id=chat_id)
            if messages:
                # Берем время последнего сообщения из STM
                last_msg = messages[-1]
                if isinstance(last_msg, dict) and "timestamp" in last_msg:
                    return float(last_msg["timestamp"])
                # Если timestamp нет, используем время загрузки из метаданных
                if isinstance(last_msg, dict) and "metadata" in last_msg:
                    meta = last_msg["metadata"]
                    if isinstance(meta, dict) and "timestamp" in meta:
                        return float(meta["timestamp"])
        except Exception:
            pass

        # 3. Fallback: смотрим в текущий буфер в памяти
        try:
            if hasattr(self.memory.stm, "buffers") and chat_id in self.memory.stm.buffers:
                buf = self.memory.stm.buffers[chat_id]
                if buf:
                    last = buf[-1]
                    if isinstance(last, dict) and "timestamp" in last:
                        return float(last["timestamp"])
        except Exception:
            pass

        return 0

    async def _send_proactive_message(self, chat_id: str, message: str, topic_id: Optional[int] = None):
        """Отправляет proactive-сообщение в чат.
        Этот метод будет переопределён в telegram_bot.py через замыкание."""
        logger.warning(f"[Proactive] _send_proactive_message не переопределён для {chat_id}: {message[:60]}...")

    def record_activity(self, chat_id: str):
        """Записывает активность в чате. Вызывается при каждом сообщении."""
        if self._activity_tracker:
            self._activity_tracker.record_activity(chat_id)

    def record_topic(self, chat_id: str, topic_id: int):
        """Записывает ID топика для чата."""
        if self._activity_tracker:
            self._activity_tracker.record_topic(chat_id, topic_id)

    def get_chat_topic(self, chat_id: str) -> Optional[int]:
        """Возвращает ID топика для чата."""
        if self._activity_tracker:
            return self._activity_tracker.get_topic(chat_id)
        return None

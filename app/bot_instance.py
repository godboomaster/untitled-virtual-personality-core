"""
BotInstance — один бот с конкретной персоной и набором фич.
Содержит VirtualPersonality, FileVectorDB и читает features из YAML.
"""

import re
import os
import json
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
from app.core.interfaces import MessageSender
from app.features.todo_manager import (
    TodoManager, is_todo_request, extract_task,
    is_todo_done_request, extract_todo_done_index,
)
from app.features.reminder_manager import ReminderManager, parse_reminder
from app.features.learning_manager import LearningManager, parse_frequency, classify_continue_answer
from app.features.learning_intent import classify_learning_intent, extract_subject
from app.features.inventory_manager import (
    InventoryManager,
    is_inventory_add_request,
    is_inventory_remove_request,
    extract_inventory_item,
    extract_inventory_remove,
)

logger = logging.getLogger(__name__)

# Сколько последних реплик передавать в query rewriter для разрешения кореференций
# кореференций - местоимения и указательные слова, которые ссылаются на что-то из предыдущего контекста
_REWRITE_HISTORY = 8

# Эвристика обрыва ответа по max_tokens (learning_manager._looks_truncated —
# та же логика, но там она приватная для LLM-вызовов учебного модуля).
_SENTENCE_END_RE = re.compile(r'[.!?…»"\)\]]\s*$')


def _looks_truncated(text: str) -> bool:
    """Эвристика обрыва ответа по max_tokens, 
    текст не заканчивается знаком завершения предложения."""
    t = (text or "").rstrip()
    if not t:
        return False
    return not _SENTENCE_END_RE.search(t)


# Эвристика: похоже ли сообщение на ответ про частоту уроков (а не на произвольную реплику
# или запрос другой фичи). Используется, чтобы бот в setup-состоянии (ждёт «как часто?»)
# активировался на «раз в день»/«каждые 2 часа», но НЕ на длинное сообщение или посторонний
# текст. Короткое (до ~12 слов) И содержит временнУю лексику — типичный ответ о периодичности.
_FREQUENCY_WORDS_RE = re.compile(
    r"\b(?:раз\s+в|кажды[ей]|каждую|через|полчаса|ежечасн\w*|ежедневн\w*|еженедельн\w*|интервал\w*|"
    r"час(?:а|ов|у|е|ом|ы|ами|ах)?\b|минут(?:а|ы|у|е|ой|ам|ами|ах)?\b|"
    r"день\b|дн[юяей]\w*|недел[ьюя]\w*|месяц\w*|секунд\w*)",
    re.IGNORECASE,
)


# Эвристика: похоже ли сообщение на исправление/поправку бота или просьбу запомнить.
# Срабатывание лишь запускает локальную LLM-формулировку правила (см. ниже) — дёшево.
_CORRECTION_HINT_RE = re.compile(
    r"\b(?:не\s+так|неправильно|неверно|я\s+име[лл]\s+в\s+виду|запомни|не\s+называй|"
    r"не\s+надо\s+так|не\s+говори\s+так|поправ\w*|исправь|ты\s+опять|ты\s+снова)\b",
    re.IGNORECASE,
)


# «Зови меня X» — предпочитаемое имя пользователя
_ALIAS_RE = re.compile(r"(?:зови|называй)\s+меня\s+([А-Яа-яЁёA-Za-z\-]{2,30})", re.IGNORECASE)


def _looks_like_frequency_answer(text: str) -> bool:
    if not text or not text.strip():
        return False
    # Короткое сообщение (ответ о частоте обычно в одну строчку)
    word_count = len(text.split())
    if word_count > 12:
        return False
    return bool(_FREQUENCY_WORDS_RE.search(text))


# Максимальная длина «короткого» ответа на вопрос «продолжаем?» (в словах).
_CONTINUE_SHORT_ANSWER_MAX_WORDS = 6


def _is_plain_yes_no(text: str) -> bool:
    """Похоже ли сообщение на короткий ответ «да/нет» — такой безопасно принять как
    ответ на вопрос «продолжаем?» даже без Telegram-reply. Длинное сообщение, пусть и
    начинающееся с «да», обычно несёт свой вопрос/тему — его нельзя съедать ответом
    на «продолжаем?», оно должно уйти в обычную обработку."""
    return bool(text) and len(text.split()) <= _CONTINUE_SHORT_ANSWER_MAX_WORDS


class BotInstance:
    
    # Каждому Telegram-боту (Коннор, Арродес) соответствует свой BotInstance.


    def __init__(self, persona_name: str, context: str = None):
        self.persona_name = persona_name
        self.context = context or persona_name
        self.persona = PersonaLayer(persona_name=persona_name)

        # Список дел/инвентарь для отправки отдельным сообщением после основного ответа.
        # Per-chat (dict по chat_id): process_message крутится конкурентно в потоках для
        # разных чатов, и общий атрибут давал гонки — список одного чата мог уехать в другой.
        self._pending_list_messages: Dict[str, List[str]] = {}

        # Тип последнего ответа-вопроса бота ('frequency' | 'continue'), per-chat — по той
        # же причине: общий флаг один чат сбрасывал/перезаписывал за другим.
        self._pending_question_kind: Dict[str, Optional[str]] = {}

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

        # Todo manager (только если todo)
        self.todo_manager: Optional[TodoManager] = None
        if self.features.get("todo", False):
            self.todo_manager = TodoManager(context=self.context)
            logger.info(f"  [{persona_name}] Todo manager включён")

        # Reminder manager — независимый флаг reminder (раньше был расширением todo)
        self.reminder_manager: Optional[ReminderManager] = None
        if self.features.get("reminder", False):
            self.reminder_manager = ReminderManager(context=self.context)
            logger.info(f"  [{persona_name}] Reminder manager включён")

        # Inventory manager (только если inventory)
        self.inventory_manager: Optional[InventoryManager] = None
        if self.features.get("inventory", False):
            self.inventory_manager = InventoryManager(context=self.context)
            logger.info(f"  [{persona_name}] Inventory manager включён")

        # Learning manager (только если learning) — режим обучения по запросу
        self.learning_manager: Optional[LearningManager] = None
        if self.features.get("learning", False):
            learning_cfg = self.features.get("learning") or {}
            if isinstance(learning_cfg, bool):
                learning_cfg = {}
            self.learning_manager = LearningManager(context=self.context, config=learning_cfg)
            logger.info(f"  [{persona_name}] Learning manager включён")

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
            self._web_pool = ThreadPoolExecutor(max_workers=2)
            logger.info(f"  [{persona_name}] Web search включён (pool: 2 workers)")

        # Local router (для query rewriting)
        self._local_router = None
        try:
            from app.core.local_router import get_local_router
            self._local_router = get_local_router()
        except Exception:
            pass

        # Punish block (нужен до rate limiter: использует block_user/is_blocked)
        self._punish_enabled = self.features.get("punish_block", False)

        # Rate limiter
        self._rate_limit_enabled = self.features.get("rate_limit", False)
        self._rate_limit_individual: dict = {}
        if self._rate_limit_enabled or self._punish_enabled:
            from app.features.rate_limiter import check_rate_limit, block_user, is_blocked, get_status_text
            self._check_rate_limit = check_rate_limit
            self._block_user = block_user
            self._is_blocked = is_blocked
            self._rate_limit_status = get_status_text

        if self._rate_limit_enabled:
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

        # Владелец — полная защита от всех блокировок
        self.owner: str = str(self.features.get("owner", ""))

        # Allowed DM users — могут писать в личку, но подлежат наказаниям
        # Пустые записи ("") отбрасываем, id приводим к str; пустой список = ЛС открыты всем
        self.allowed_dm_users: set = {
            str(u).strip() for u in self.features.get("allowed_dm_users", []) if str(u).strip()
        }
        self.blocked_users: set = {
            str(u).strip() for u in self.features.get("blocked_users", []) if str(u).strip()
        }

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
        self._sender: Optional[MessageSender] = None
        proactive_config = self.features.get("proactive", {})
        if proactive_config.get("enabled", False):
            from app.features.proactive_messaging import ProactiveConfig, ProactiveMessaging, ChatActivityTracker
            self._activity_tracker = ChatActivityTracker(context=context)
            # sender будет установлен позже через setup_sender()
            self.proactive = None  # создадим после установки sender
            logger.info(f"  [{persona_name}] Proactive messaging подготовлен (ожидает sender)")

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

        # 2. DM только для разрешённых (пустой список = ЛС открыты всем)
        if is_private and self.allowed_dm_users and user_id not in self.allowed_dm_users:
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

    # ── per-chat pending-состояние (досылка списков, регистрация вопросов) ──

    def _pending_lists(self, chat_id) -> List[str]:
        """Бакет списков (дел/инвентаря) текущего чата для досылки после ответа."""
        return self._pending_list_messages.setdefault(str(chat_id), [])

    def pop_pending_list_messages(self, chat_id) -> List[str]:
        """Забирает накопленные списки чата для досылки — и очищает бакет.
        Вызывается telegram-слоем после отправки основного ответа."""
        return self._pending_list_messages.pop(str(chat_id), [])

    def pop_pending_question_kind(self, chat_id) -> Optional[str]:
        """Забирает (и снимает) тип последнего ответа-вопроса бота для чата:
        'frequency' | 'continue' | None. Pop-семантика: флаг одноразовый, старое
        значение не может протечь в следующий ответ ни в этом, ни в чужом чате."""
        return self._pending_question_kind.pop(str(chat_id), None)

    # Main processing

    def process_message(self, user_input: str, user_id: str = "default",
                        chat_id: str = None, user_name: str = None,
                        reply_context: str = None,
                        reply_to_bot_message_id: Optional[int] = None) -> str:
        from app.features.query_rewriter import rewrite_query

        # Очищаем pending-состояние ЭТОГО чата от предыдущего вызова (атрибуты per-chat:
        # process_message выполняется конкурентно в потоках для разных чатов, и общие
        # атрибуты давали гонки — чужой фидбек/вопрос уезжал не в тот чат).
        self._pending_list_messages[str(chat_id)] = []
        # Каким был последний ответ-вопрос: 'frequency' | 'continue' | None.
        # Нужно telegram-слою, чтобы зарегистрировать отправленное сообщение как «вопрос бота»
        # для reply-to-логики обучения (пользователь может ответить reply-ом на этот вопрос).
        self._pending_question_kind[str(chat_id)] = None
        # Локальная переменная (раньше — общий атрибут, та же гонка): готовый ответ,
        # минующий основной LLM-вызов (фидбек теста, реплики setup/continue обучения).
        skip_llm_answer = None

        logger.info(f"[BotInstance] process_message START: '{user_input[:60]}' | chat_id={chat_id}")

        # Берём историю до добавления нового сообщения — для контекста rewriter'а
        history_for_rewrite = self.memory.stm.get_last(_REWRITE_HISTORY, chat_id=chat_id)
        logger.info(f"[BotInstance] history_for_rewrite: {len(history_for_rewrite)} messages")

        # Переписываем запрос: разрешаем местоимения и анафору
        persona_context = self._get_persona_context_for_search()
        ru_rewritten = rewrite_query(
            user_input, history_for_rewrite, self._local_router, persona_context=persona_context
        )
        logger.info(f"[BotInstance] rewrite_query: '{user_input[:60]}' -> '{ru_rewritten[:60]}'")

        # 1. Запускаем веб-поиск в фоне (параллельно с памятью)
        # QueryEnhancer преобразует ru_rewritten в короткий поисковый запрос через LLM
        # Передаём историю и контекст персоны для корректного понимания вопроса
        web_future = None
        if self._web_search_enabled and chat_id not in self._web_search_disabled_chats and not self._is_docs_only_request(user_input):
            # Собираем контекст персоны для QueryEnhancer
            persona_context = self._get_persona_context_for_search()
            # Берём последние 6 сообщений для контекста
            history_for_search = self.memory.stm.get_last(6, chat_id=chat_id)
            web_future = self._web_pool.submit(
                self._search_web, ru_rewritten, 5, True, None, history_for_search, persona_context
            )

        try:
            # В STM сохраняем переписанную русскую версию (с разрешёнными местоимениями)
            self.memory.add_message("user", ru_rewritten, user_id, chat_id, user_name)
            stm_messages, ltm_facts, stm_relevant = self.memory.get_context(
                user_id, chat_id, ltm_query=ru_rewritten
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
                    # search_web (LLM-enhance + DDG + загрузка страниц) регулярно
                    # занимает больше 10с — при меньшем таймауте результат терялся
                    results = web_future.result(timeout=25)
                    if results:
                        web_context = self._format_web_results(results)
                except FuturesTimeoutError:
                    web_future.cancel()
                    logger.info("  [WebSearch] Таймаут ожидания результатов, ищем без веба")
                except Exception:
                    pass
            context_parts_out = []
            if ltm_facts:
                context_parts_out.append("\n".join(ltm_facts))
            if file_context:
                context_parts_out.append(file_context)
            # В группе добавляем факты других участников, сказанные публично в этом чате
            if chat_id and str(chat_id) != str(user_id):
                chat_facts_block = self.memory.get_chat_facts_block(chat_id, exclude_user_id=user_id)
                if chat_facts_block:
                    context_parts_out.append(chat_facts_block)

            # Исправления → правила: пользователь поправляет бота — формулируем
            # правило локальной LLM и сохраняем; оно запинится в промпт ниже
            if _CORRECTION_HINT_RE.search(user_input):
                rule = self._extract_rule_from_correction(user_input)
                if rule:
                    self.memory.ltm.save_facts(
                        f"Rule: {rule}", user_id, origin_chat=chat_id, user_name=user_name
                    )
                    logger.info(f"[Rules] Новое правило для {user_id}: {rule}")

            # «Зови меня X» — сохраняем как факт Name (UPDATE-категория заменяет старый)
            alias_match = _ALIAS_RE.search(user_input)
            if alias_match:
                alias = alias_match.group(1).strip()
                self.memory.ltm.save_facts(
                    f"Name: {alias}", user_id, origin_chat=chat_id, user_name=user_name
                )
                logger.info(f"[Alias] {user_id} попросил называть его «{alias}»")

            # Правила от пользователя пиним ВСЕГДА, не полагаясь на семантический
            # поиск — иначе бот повторит ту же ошибку в другом контексте
            user_rules = self.memory.ltm.get_facts_by_category(user_id, "Rule", chat_id=chat_id)
            if user_rules:
                context_parts_out.append(
                    "Правила от пользователя (соблюдай всегда, они важнее привычек):\n"
                    + "\n".join(f"  - {r}" for r in user_rules[-10:])
                )
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

            # Reminder: перехватываем перед todo (напомни через N ...)
            # Любой текст со словом "напом" — это путь напоминаний, не todo.
            reminder_context = None
            is_reminder_request = False

            # Pending /remind без времени: пользователь отвечал на «через сколько?»
            if self.reminder_manager and chat_id:
                pending_task = self.reminder_manager.get_pending_remind(chat_id)
                logger.info(f"[Reminder] pending_remind для chat={chat_id}: {pending_task!r}")

                # Конфликт ожиданий: одновременно висит вопрос обучения «как часто уроки?».
                # Ответ о периодичности («раз в день», «каждые 2 часа») принадлежит тому, кто
                # спросил ПОЗЖЕ — человек отвечает на последний заданный вопрос. Если свежее
                # setup обучения — уступаем: напоминание НЕ потребляем (остаётся pending,
                # на него можно ответить следующим сообщением), сообщение разберёт
                # learning-блок ниже. Раньше напоминание всегда съедало такой ответ,
                # и setup курса зависал навсегда.
                _yield_to_learning = False
                if pending_task and self.learning_manager:
                    _setup = self.learning_manager.get_setup_state(chat_id)
                    if _setup and (
                        self.learning_manager.is_reply_to_question(chat_id, reply_to_bot_message_id)
                        or _looks_like_frequency_answer(user_input)
                    ):
                        _remind_at = self.reminder_manager.get_pending_remind_asked_at(chat_id) or 0
                        _yield_to_learning = (_setup.get("asked_at") or 0) > _remind_at
                        if _yield_to_learning:
                            logger.info(f"[Reminder] chat={chat_id}: ответ уступаю обучению (его вопрос свежее)")

                if pending_task and not _yield_to_learning:
                    # Пытаемся вытащить время из ответа пользователя
                    rem_delay = None
                    parsed_pending = parse_reminder("напомни " + user_input)
                    if parsed_pending:
                        _, rem_delay = parsed_pending
                    if rem_delay is None:
                        # Пробуем парсер частоты из learning
                        rem_delay = parse_frequency(user_input)
                    if rem_delay:
                        self.reminder_manager.clear_pending_remind(chat_id)
                        rem_task = self._reformulate_task(pending_task)
                        topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                        delay_text = self.reminder_manager.format_delay(rem_delay)
                        self.reminder_manager.add_reminder(
                            chat_id, user_name or "Пользователь", rem_task, rem_delay, topic_id
                        )
                        task_display = f" '{rem_task}'" if rem_task else ""
                        reminder_context = (
                            f"Пользователь указал время для напоминания{task_display} — через {delay_text}. "
                            f"Напоминание запланировано — подтверди это в своём стиле, коротко."
                        )
                        is_reminder_request = True
                    else:
                        # Время снова не поняли — переспрашиваем ещё раз
                        reminder_context = (
                            f"Пользователь отвечает на вопрос про время напоминания «{pending_task}», "
                            "но время не удалось понять. Переспроси: через сколько напомнить "
                            "(например, «через 2 часа», «завтра в 12»). В своём стиле, коротко."
                        )
                        is_reminder_request = True

                # ВАЖНО: раньше это был `elif` на том же уровне, что и `if self.reminder_manager
                # and chat_id:` выше — а условие elif было строгим подмножеством условия if,
                # так что при отсутствии pending_task (обычный случай) сюда вообще никогда не
                # попадали, и свежие текстовые запросы «напомни мне X через Y» никогда не
                # парсились. Теперь это правильно вложено: проверяем «напом» только когда
                # НЕТ pending-задачи.
                elif "напом" in user_input.lower():
                    is_reminder_request = True
                    parsed = parse_reminder(user_input)
                    logger.info(f"[Reminder] parse_reminder({user_input[:60]!r}) -> {parsed}")
                    if parsed:
                        rem_task, rem_delay = parsed
                        # Переформулирование задачи через LLM
                        if rem_task:
                            rem_task = self._reformulate_task(rem_task)
                        topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                        delay_text = self.reminder_manager.format_delay(rem_delay)
                        self.reminder_manager.add_reminder(
                            chat_id, user_name or "Пользователь", rem_task, rem_delay, topic_id
                        )
                        task_display = f" '{rem_task}'" if rem_task else ""
                        reminder_context = (
                            f"Пользователь попросил напомнить{task_display} через {delay_text}. "
                            f"Напоминание уже запланировано — просто подтверди это в своём стиле, коротко."
                        )
                    else:
                        # Время не указано — переспрашиваем и ЗАПОМИНАЕМ задачу (как в /remind),
                        # иначе следующее сообщение "через 10 минут" не с чем будет связать.
                        rem_task = self._reformulate_task(user_input)
                        self.reminder_manager.begin_pending_remind(chat_id, rem_task)
                        reminder_context = (
                            f"Пользователь попросил напомнить «{rem_task}», но не указал через сколько. "
                            "Уточни когда ему напомнить — в своём стиле, коротко."
                        )

            # Learning-контекст: режим обучения («научи меня X»)
            learning_context = None
            is_learning_request = False
            if self.learning_manager and chat_id:
                # Ответил ли пользователь reply-ом на один из последних «вопросов» бота
                # (частота уроков / «продолжаем?» / тест)? Это даёт обучению приоритет:
                # если да — сообщение трактуется как ответ на этот вопрос.
                reply_to_question = self.learning_manager.is_reply_to_question(chat_id, reply_to_bot_message_id)
                # Явно другая фича (напоминание/todo/инвентарь) — даже без reply у неё
                # приоритет над обучением, чтобы «напомни через 5 минут» не создавало
                # фейковый урок через parse_frequency("5 минут")=300с.
                explicit_other_feature = (
                    is_reminder_request
                    or is_inventory_add_request(user_input)
                    or is_inventory_remove_request(user_input)
                    or is_todo_request(user_input)
                    or is_todo_done_request(user_input)
                )

                # Сбрасываем счётчик молчания — но только если сообщение не про другую
                # фичу (напоминание не должно сбрасывать молчание курса).
                if not explicit_other_feature:
                    self.learning_manager.record_user_activity(chat_id)

                # 1. Если ждём ответа на «как часто?» — парсим частоту.
                #    Чтобы не перехватывать напоминания/todo/инвентарь, активируемся только если:
                #    (a) это reply на бот-вопрос о частоте, ИЛИ
                #    (b) сообщение не похоже ни на какую другую фичу (explicit_other_feature=False).
                setup = self.learning_manager.get_setup_state(chat_id)
                if setup and not explicit_other_feature and (reply_to_question or _looks_like_frequency_answer(user_input)):
                    topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                    subject = setup.get("subject", "")
                    delay = self.learning_manager.parse_frequency_smart(user_input)
                    if delay:
                        self.learning_manager.commit_session(chat_id, delay, topic_id)
                        delay_text = self.learning_manager.format_delay(delay)
                        # Изолированный вызов (без STM/истории) — см. docstring
                        # render_setup_reply про то, почему это не идёт через
                        # общий self.persona.prepare_messages(...).
                        skip_llm_answer = self.learning_manager.render_setup_reply(subject, "confirmed", delay_text)
                    else:
                        skip_llm_answer = self.learning_manager.render_setup_reply(subject, "reask")
                    # Если частоту не поняли (reask) — бот переспрашивает, это «вопрос».
                    # Если поняли (confirmed) — это не вопрос, а подтверждение старта.
                    self._pending_question_kind[str(chat_id)] = "frequency" if not delay else None
                    is_learning_request = True
                else:
                    # 2/3. Один из параллельных курсов ждёт ответа — на тест ИЛИ на
                    # «продолжаем?». Но если сообщение явно про другую фичу (напоминание/
                    # todo/инвентарь) — не перехватываем, даём той фиче приоритет.
                    _pending = None
                    if not explicit_other_feature:
                        _pending = self.learning_manager.resolve_pending_target(chat_id, user_input)

                    _pending_handled = False
                    if _pending and _pending["_pending_kind"] == "continue":
                        # Ответ на «продолжаем?» принимаем двумя путями:
                        # (a) настоящий Telegram-reply на само сообщение с вопросом —
                        #     однозначный сигнал от пользователя, поэтому содержимое можно
                        #     разобрать умным классификатором (LLM, с OFFTOPIC-веткой);
                        # (b) обычное КОРОТКОЕ сообщение с однозначным да/нет по regex —
                        #     в личных чатах reply почти не используют, и без этого пути
                        #     вопрос висел до авто-остановки курса в _loop(), хотя человек
                        #     по сути ответил. Лимит длины важен: длинное сообщение, пусть
                        #     и начинающееся с «да», обычно несёт свой вопрос/тему — его
                        #     нельзя съедать ответом на «продолжаем?».
                        _session_id = _pending.get("session_id")
                        decision = None
                        if reply_to_question:
                            _smart = self.learning_manager.classify_continue_answer_smart(user_input)
                            # OFFTOPIC — это reply на вопрос, но не по теме: вопрос не
                            # трогаем, сообщение уходит в обычную обработку ниже.
                            if _smart != "OFFTOPIC":
                                decision = _smart
                        elif _is_plain_yes_no(user_input):
                            fast = classify_continue_answer(user_input)
                            if fast in ("YES", "NO"):
                                decision = fast
                        if decision:
                            self.learning_manager.resolve_continue(chat_id, decision, session_id=_session_id)
                            # Изолированный вызов (без STM/истории) — см. docstring
                            # render_continue_reply про то, почему это не идёт через
                            # общий self.persona.prepare_messages(...).
                            skip_llm_answer = self.learning_manager.render_continue_reply(chat_id, decision, session_id=_session_id)
                            # При UNKNOWN бот переспрашивает «да/нет» — это «вопрос».
                            self._pending_question_kind[str(chat_id)] = "continue" if decision == "UNKNOWN" else None
                            is_learning_request = True
                            _pending_handled = True
                        # иначе — сообщение не про «продолжаем?»: падаем в обычную
                        # обработку ниже, вопрос остаётся висеть НЕТРОНУТЫМ
                    elif _pending and _pending["_pending_kind"] == "quiz":
                        _session_id = _pending.get("session_id")
                        feedback = self.learning_manager.submit_quiz_answer(chat_id, user_input, session_id=_session_id, is_reply=reply_to_question)
                        if feedback is not None:
                            # Реальная попытка ответить (пусть неверная) — фидбек уже
                            # сгенерирован в образе персоны в _evaluate_quiz. Возвращаем
                            # напрямую, минуя основной LLM-вызов, чтобы персонаж не
                            # «достроил» к оценке новый урок.
                            learning_context = None
                            skip_llm_answer = feedback
                            is_learning_request = True
                            _pending_handled = True
                        # feedback is None — офф-топ: ученик сменил тему, тест остаётся
                        # открытым. Сообщение уходит в обычную генерацию ответа, без
                        # пометки is_learning_request — персона ответит по сути вопроса.

                    if not _pending_handled:
                        # 4. Новая просьба об обучении — классификатор намерения
                        intent = classify_learning_intent(user_input)
                        if intent == "LEARN":
                            subject = extract_subject(user_input)
                            self.learning_manager.begin_setup(chat_id, subject, user_id or "default", user_name or "Пользователь")
                            learning_context = (
                                f"Пользователь хочет, чтобы ты научил его «{subject}». "
                                "Спроси его коротко и в своём стиле, как часто присылать уроки "
                                "(например: раз в день, каждые 2 часа). Обучение начнётся после его ответа."
                            )
                            is_learning_request = True
                            # Ответ ниже — вопрос «как часто?»: отмечаем, чтобы telegram-слой
                            # зарегистрировал его message_id и reply пользователя распознался
                            # как ответ о частоте (без этого reply-gate работал только на
                            # переспросах, а на первом вопросе — нет).
                            self._pending_question_kind[str(chat_id)] = "frequency"
                        else:
                            # 5. Сессия(и) активна, но это не команда/тест/setup/continue.
                            # Пользователь, скорее всего, отвечает на контрольные вопросы прошлого урока
                            # или просто пишет по теме. Запрещаем персоне самой продолжать курс:
                            # следующий урок придёт по расписанию как отдельный файл.
                            # Курсов может быть несколько параллельно — перечисляем все темы,
                            # т.к. get_session(chat_id) больше не возвращает одну сессию однозначно.
                            active_sessions = self.learning_manager.get_sessions(chat_id)
                            if active_sessions:
                                subjects = [s.get("subject", "") for s in active_sessions if s.get("subject")]
                                subjects_str = "«" + "», «".join(subjects) + "»" if subjects else ""
                                courses_note = (
                                    f"по теме {subjects_str}" if len(subjects) == 1
                                    else f"сразу по нескольким темам: {subjects_str}"
                                )
                                learning_context = (
                                    f"В этом чате идёт обучение {courses_note}. "
                                    "Пользователь пишет в рамках учебной беседы — возможно, отвечает на "
                                    "контрольные вопросы прошлого урока (по одной из тем) или обсуждает тему. "
                                    "Реагируй ТОЛЬКО на сообщение пользователя, в своём стиле. "
                                    "СТРОГИЕ ПРАВИЛА:\n"
                                    "— НЕ генерируй новый урок, новую тему, контрольные вопросы или тест "
                                    "ни по одной из тем.\n"
                                    "— НЕ пиши учебный материал вперемешку с ответом — следующий урок "
                                    "придёт позже по расписанию отдельным сообщением-файлом.\n"
                                    "— Реагируй коротко: ответь/поясни/прокомментируй, не более."
                                )
                                # is_learning_request здесь НАМЕРЕННО не ставим: само
                                # сообщение — обычный разговор при фоново активном курсе,
                                # а не учебно-административное действие (setup/continue/
                                # тест/новый курс — там флаг стоит). Гейты todo/inventory-
                                # маркеров ниже пропускают обработку при флаге, чтобы LLM
                                # не создавал сущности из учебных реплик; если выставить
                                # его на КАЖДОЕ сообщение при активном курсе, то «добавь
                                # в дела X» / «добавь в инвентарь Y» во время курса молча
                                # перестают работать, а сырые маркеры [TODO_ADD:...]
                                # утекают пользователю в ответ.
                                # get_nag_guard: персона сама (без инструкции) любит напоминать
                                # о незакрытых контрольных вопросах почти в каждом ответе —
                                # отсюда навязчивые «который раз за сессию», «паттерн
                                # подтверждён» и т.п. Кулдаун 4ч на курс: пока не истекло с
                                # последнего такого напоминания — явный запрет повторять;
                                # как истекло — ничего не добавляем, оставляя персоне свободу
                                # упомянуть (или нет) по своему усмотрению.
                                nag_guard = self.learning_manager.get_nag_guard(chat_id)
                                if nag_guard:
                                    learning_context += "\n\n" + nag_guard
                                # Изредка (кулдаун + вероятность внутри) добавляем к этой же
                                # инструкции подсказку органично закрепить пройденный материал —
                                # словом/фразой для языковых курсов или уместной отсылкой без
                                # объяснений для остальных тем. Не отдельный контекст, а
                                # дополнение к нему — иначе он никогда бы не сработал, ведь
                                # guard выше занимает learning_context на КАЖДОМ сообщении.
                                reinforcement = self.learning_manager.get_reinforcement_hint(chat_id)
                                if reinforcement:
                                    learning_context += "\n\n" + reinforcement

            # Todo-контекст: определяем, является ли запрос todo-запросом
            # Может работать параллельно с напоминанием (напр. "напомни через час X и добавь в список дел")
            todo_context = None
            extracted_task = None
            extracted_done_index = None

            # Какие эвристики фич сработали на этом сообщении
            _fired_intents = set()
            if self.todo_manager and chat_id:
                if is_todo_done_request(user_input):
                    _fired_intents.add("todo_remove")
                elif is_todo_request(user_input):
                    _fired_intents.add("todo_add")
            if self.inventory_manager and not is_reminder_request:
                if is_inventory_add_request(user_input):
                    _fired_intents.add("inventory_add")
                elif is_inventory_remove_request(user_input):
                    _fired_intents.add("inventory_remove")

            # Арбитр намерений: при конфликте триггеров («убери из списка дел задачу» —
            # todo_remove И inventory_remove одновременно) локальная LLM выбирает
            # одно намерение. Без конфликта классификатор не вызывается — бесплатно.
            if len(_fired_intents) > 1:
                winner = self._classify_intent(user_input, sorted(_fired_intents))
                if winner == "CHAT":
                    logger.info(f"[Intent] Конфликт {_fired_intents} → CHAT (локальная LLM)")
                    _fired_intents = set()
                elif winner:
                    logger.info(f"[Intent] Конфликт {_fired_intents} → {winner} (локальная LLM)")
                    _fired_intents = {winner}
                # локальная недоступна — оставляем прежнее поведение

            if self.todo_manager and chat_id:
                if "todo_remove" in _fired_intents:
                    # Запрос на удаление/завершение дела
                    extracted_done_index = extract_todo_done_index(user_input)
                    current_todo = self.todo_manager.get_list(chat_id)
                    todo_context = current_todo or "Список дел пуст."
                elif "todo_add" in _fired_intents:
                    extracted_task = extract_task(user_input)
                    if extracted_task:
                        extracted_task = self._reformulate_task(extracted_task)
                    current_todo = self.todo_manager.get_list(chat_id)
                    todo_context = current_todo or "Список дел пуст."

            # Inventory-контекст: вещи бота
            # (пропускаем если это напоминание — чтобы LLM не добавил мусор в инвентарь)
            inventory_context = None
            extracted_inventory_item = None
            extracted_inventory_remove = None
            inventory_events = []  # События для LLM-реакции (использование, просрочка)
            if not is_reminder_request and self.inventory_manager:
                inv_block = self.inventory_manager.get_context_block()
                if inv_block:
                    inventory_context = inv_block
                # Проверяем запрос на добавление/удаление
                if "inventory_add" in _fired_intents:
                    extracted_inventory_item = extract_inventory_item(user_input)
                elif "inventory_remove" in _fired_intents:
                    extracted_inventory_remove = extract_inventory_remove(user_input)

                # Проверяем, не сказал ли пользователь что бот использовал предмет
                # (например: "ты использовал X", "ты съел Y", "ты выпил Z", "давай съедим Z")
                used_item = self._extract_user_reported_usage(user_input)
                if used_item:
                    # Проверяем что предмет действительно есть в инвентаре
                    if self.inventory_manager.has_item(used_item):
                        result = self.inventory_manager.use_item(used_item)
                        inventory_events.append(f"Предмет '{used_item}' был использован и теперь его нет в инвентаре.")
                    else:
                        # Пробуем найти похожий предмет (по части названия)
                        found = self._find_inventory_item_by_substring(used_item)
                        if found:
                            result = self.inventory_manager.use_item(found)
                            inventory_events.append(f"Предмет '{found}' был использован и теперь его нет в инвентаре.")
                        else:
                            inventory_events.append(f"Пользователь говорит об использовании '{used_item}', но такого предмета нет в инвентаре.")

                # Проверяем просроченные предметы
                expired = self.inventory_manager.remove_expired_items()
                for exp_name in expired:
                    inventory_events.append(f"Предмет '{exp_name}' испортился/просрочился и исчез из инвентаря.")

                # Обновляем контекст инвентаря после всех изменений
                inv_block = self.inventory_manager.get_context_block()
                if inv_block:
                    inventory_context = inv_block

            # Ранний возврат: готовый ответ, минующий LLM (фидбек теста, не генерируем новый контент)
            if skip_llm_answer:
                answer = self._clean_response(skip_llm_answer)
                self.memory.add_message("assistant", answer, user_id, chat_id)
                if self.proactive and chat_id:
                    self.proactive.record_user_response(chat_id)
                return answer

            # Предпочитаемое имя: последний факт категории Name заменяет
            # telegram-имя в форматировании — так работает «зови меня X»
            name_facts = self.memory.ltm.get_facts_by_category(user_id, "Name", chat_id=chat_id)
            if name_facts:
                preferred = name_facts[-1].partition(":")[2].strip()
                if preferred and len(preferred) <= 40:
                    user_name = preferred

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
                todo_context=todo_context,
                reminder_context=reminder_context,
                inventory_context=inventory_context,
                inventory_events=inventory_events,
                learning_context=learning_context,
                book_context=book_context
            )
            settings = self.persona.get_settings()
            # Когда есть учебный контекст (анонс/пересказ урока, фидбек) — ответ выходит
            # длиннее обычной реплики, для которой рассчитан persona.get_settings(). Берём
            # более щедрый max_tokens, чтобы не упираться в лимит и не дёргать догенерацию.
            if learning_context:
                settings = dict(settings)
                settings["max_tokens"] = max(int(settings.get("max_tokens", 2000)), 3000)
            answer = self.router.get_response(messages, **settings)
            if not answer:
                logger.error("Все LLM-провайдеры недоступны, ответ не сгенерирован")
                return "Сейчас все LLM-провайдеры недоступны. Попробуй позже."

            # Защита от обрыва по max_tokens (persona.get_settings() рассчитан на обычную
            # реплику; когда learning_context просит анонсировать/пересказать урок, ответ
            # выходит длиннее и может упереться в лимит) — просим модель дописать.
            _continuations = 0
            while _looks_truncated(answer) and _continuations < 2:
                follow_up_messages = messages + [
                    {"role": "assistant", "content": answer},
                    {"role": "user", "content": "Ты остановился на середине фразы. Допиши строго с того места, где прервался — не повторяй уже написанное и не начинай заново."},
                ]
                cont = self.router.get_response(follow_up_messages, **settings)
                if not cont:
                    break
                answer = answer + cont
                _continuations += 1

            # Очистка ответа от мета-рассуждений и Markdown
            answer = self._clean_response(answer)

            # Обработка todo-маркера
            # (пропускаем для учебно-административных сообщений — setup/continue/тест/
            # новый курс, там is_learning_request=True; обычный разговор при активном
            # курсе флаг не выставляет, и todo во время курса работает как обычно)
            if self.todo_manager and chat_id and todo_context and not is_learning_request:
                answer = self._process_todo_marker(
                    answer, chat_id, user_name or "Пользователь",
                    fallback_task=extracted_task,
                    fallback_done_index=extracted_done_index,
                    user_text=user_input,
                )

            # Обработка inventory-маркеров (добавление/удаление/использование через маркеры)
            # (пропускаем если это напоминание или учебно-административное сообщение —
            # там LLM не должен добавлять в инвентарь; обычный разговор при активном
            # курсе сюда проходит — инвентарь во время курса работает как обычно)
            if not is_reminder_request and not is_learning_request and self.inventory_manager:
                answer = self._process_inventory_markers(answer, extracted_inventory_item, extracted_inventory_remove, user_name or "пользователь", user_text=user_input, chat_id=chat_id)
            if self._punish_enabled:
                answer = self._parse_punishment(answer, user_id)

            # 10. Сохраняем ответ
            self.memory.add_message("assistant", answer, user_id, chat_id)

            # 11. Эпизодическая память (self_memory)
            if self.self_memory:
                self.self_memory.tick(stm_messages, user_id, user_input)

            # 12. Обратная связь proactive: если ждем ответа на инициативу -- фиксируем успех
            if self.proactive and chat_id:
                self.proactive.record_user_response(chat_id)
                # Также обновляем досье на каждое входящее сообщение
                self.proactive.record_incoming_message(chat_id)

            return answer
        finally:
            # Ничего не делаем — пул живёт всё время жизни бота
            pass

    def _reformulate_task(self, raw_task: str) -> str:
        """
        Очищает сырой текст задачи через local LLM.
        'что пора написать Коннор' -> 'Написать Коннор'
        'мне сделать апдейт' -> 'Сделать апдейт'
        """
        if not raw_task or len(raw_task.strip()) < 2:
            return raw_task

        if not self._local_router or not self._local_router.is_available():
            return raw_task.strip()

        try:
            response = self._local_router.get_response(
                messages=[
                    {"role": "system", "content": (
                        "Очисти текст задачи от местоимений и мусора. "
                        "Ответ — только короткий текст задачи в инфинитиве."
                    )},
                    {"role": "user", "content": raw_task.strip()},
                ],
                temperature=0.0,
                max_tokens=60,
            )

            if response:
                cleaned = response.strip().strip('"\'""«»')

                # Жёсткая валидация — локальная модель часто возвращает мусор
                # 1. Не длиннее исходного + 20 символов
                if len(cleaned) > len(raw_task) + 20:
                    logger.info(f"[Task] Переформулирование отклонено (длиннее оригинала): '{cleaned[:60]}'")
                    return raw_task.strip()

                # 2. Не длиннее 100 символов
                if len(cleaned) > 100:
                    logger.info(f"[Task] Переформулирование отклонено (слишком длинный): '{cleaned[:60]}'")
                    return raw_task.strip()

                # 3. Не содержит слов из системного промпта (модель эхо)
                _FORBIDDEN_WORDS = (
                    "очисти", "убери", "местоимени", "мусор", "инфинитив",
                    "разговорн", "обращени", "ответ", "только текст",
                    # инфинитивные формы (модель перефразирует промпт)
                    "очистить", "убрать", "оставить", "сохранить смысл",
                    "суть задачи", "текст задачи", "короткий текст",
                    "убери местоимения", "убрать местоимения",
                    "в своём характере", "напиши короткое",
                    "мета-пометки", "не используй markdown",
                )
                lower = cleaned.lower()
                for word in _FORBIDDEN_WORDS:
                    if word in lower:
                        logger.info(f"[Task] Переформулирование отклонено (эхо промпта): '{cleaned[:60]}'")
                        return raw_task.strip()

                # 4. Минимум 2 символа
                if len(cleaned) >= 2:
                    logger.info(f"[Task] Переформулировано: '{raw_task}' -> '{cleaned}'")
                    return cleaned

        except Exception as e:
            logger.debug(f"[Task] Переформулирование не удалось: {e}")

        return raw_task.strip()

    def _extract_rule_from_correction(self, user_text: str) -> Optional[str]:
        """
        Если реплика — исправление бота или просьба запомнить правило/предпочтение,
        формулирует короткое правило через ЛОКАЛЬНУЮ LLM. Иначе возвращает None.
        """
        if not self._local_router or not self._local_router.is_available():
            return None
        try:
            resp = self._local_router.get_response(
                messages=[
                    {"role": "system", "content": (
                        "Определи, является ли реплика исправлением бота или просьбой запомнить "
                        "правило/предпочтение (как обращаться, что делать или не делать). "
                        "Если да — сформулируй правило ОДНИМ коротким предложением (до 12 слов), "
                        "без пояснений и кавычек. Если это обычный разговор или вопрос — ответь ровно NO."
                    )},
                    {"role": "user", "content": user_text[:400]},
                ],
                temperature=0.0, max_tokens=60,
            )
            if not resp:
                return None
            rule = resp.strip().strip('"\'""«»').strip()
            if not rule or rule.upper().startswith("NO") or not (5 <= len(rule) <= 150):
                return None
            return rule
        except Exception as e:
            logger.debug(f"[Rules] Извлечение правила не удалось: {e}")
            return None

    def _classify_intent(self, user_text: str, candidates: list) -> Optional[str]:
        """
        Арбитр намерений при конфликте эвристик: локальная LLM выбирает ОДНО
        намерение из candidates (snake_case: todo_add, inventory_remove...).
        Возвращает winner (snake_case), "CHAT" (ничего не подходит) или None
        (локальная модель недоступна — оставляем прежнее поведение).
        """
        if not self._local_router or not self._local_router.is_available():
            return None

        intent_desc = {
            "todo_add": "TODO_ADD — записать новую задачу в список дел",
            "todo_remove": "TODO_REMOVE — убрать/вычеркнуть задачу из списка дел",
            "inventory_add": "INVENTORY_ADD — дать/передать предмет боту в его инвентарь",
            "inventory_remove": "INVENTORY_REMOVE — забрать/выбросить предмет из инвентаря бота",
        }
        valid_outputs = [c.upper() for c in candidates]
        options_text = "\n".join(f"- {intent_desc[c]}" for c in candidates)

        verdict = self._local_router.classify(
            system_prompt=(
                "Ты — классификатор намерений. Определи, что пользователь просит сделать.\n"
                f"Варианты:\n{options_text}\n"
                "- CHAT — обычный разговор, ничего из перечисленного.\n"
                "Внимание на объект действия: «список дел» — это TODO, «инвентарь/у тебя/тебе» — INVENTORY. "
                "Ответь одним словом."
            ),
            user_prompt=f"Реплика пользователя: «{user_text}»",
            valid_outputs=valid_outputs + ["CHAT"],
            temperature=0.0,
            max_tokens=10,
        )
        if not verdict:
            return None
        if verdict == "CHAT":
            return "CHAT"
        return verdict.lower()

    def _confirm_intent(
        self, user_text: str, candidate: str, intent: str
    ) -> str:
        """
        Подтверждает через локальную LLM, что эвристически извлечённый кандидат —
        это явная просьба пользователя, а не огрызок из обычной реплики.

        intent: 'inventory_add' | 'inventory_remove' | 'todo_add'.
        Возвращает:
          'ADD'  — локальная LLM подтвердила намерение;
          'SKIP' — локальная LLM отклонила;
          'ASK'  — локальная LLM недоступна → переспросить пользователя.
        """
        if not self._local_router or not self._local_router.is_available():
            logger.info(f"[Intent] Локальная LLM недоступна, переспрос для '{candidate[:40]}'")
            return "ASK"

        intent_desc = {
            "inventory_add": "добавить предмет в инвентарь персонажа",
            "inventory_remove": "выбросить/убрать предмет из инвентаря",
            "todo_add": "записать задачу в список дел",
            "todo_remove": "отметить задачу выполненной и убрать из списка дел",
        }.get(intent, "выполнить действие")

        system_prompt = (
            "Ты — классификатор намерений. Реши, просит ли пользователь ЯВНО и ОСОЗНАННО "
            f"{intent_desc}, или это просто обычная реплика/рассказ о прошлом. "
            "Повествование о прошедших событиях («получил», «сделал», «купил») — НЕ просьба. "
            "Ответь одним словом: ADD (явная просьба) или SKIP (не просьба)."
        )
        user_prompt = (
            f"Реплика пользователя: «{user_text}»\n"
            f"Извлечённый кандидат: «{candidate}»\n"
            f"Это явная просьба {intent_desc}? ADD или SKIP."
        )

        try:
            verdict = self._local_router.classify(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                valid_outputs=["ADD", "SKIP"],
                temperature=0.0,
                max_tokens=5,
            )
        except Exception as e:
            logger.warning(f"[Intent] Ошибка классификации: {e}")
            return "ASK"

        if verdict is None:
            logger.info(f"[Intent] Локальная LLM не распознала ответ для '{candidate[:40]}'")
            return "ASK"

        logger.info(f"[Intent] {intent}: '{candidate[:40]}' -> {verdict}")
        return verdict

    def _clean_response(self, response: str) -> str:
        # Очищает ответ от лишнего Markdown-форматирования и мета-рассуждений LLM.
        if not response:
            return response
        response = self._strip_meta_reasoning(response)
        response = self._strip_markdown(response)
        response = self._strip_inline_lists(response)
        return response.strip()

    @staticmethod
    def _strip_inline_lists(text: str) -> str:
        """Удаляет секции 'Список дел:' и 'Инвентарь:' из ответа LLM.
        Эти списки отправляются отдельным сообщением через _pending_list_messages."""
        # Вырезаем секцию "Список дел:" и все её пункты до пустой строки или конца текста
        text = re.sub(r'\n*Список дел:\n.*?(?=\n\s*\n|\Z)', '', text, flags=re.DOTALL)
        # Вырезаем секцию "Инвентарь:" и все её пункты до пустой строки или конца текста
        text = re.sub(r'\n*Инвентарь:\n.*?(?=\n\s*\n|\Z)', '', text, flags=re.DOTALL)
        # Схлопываем лишние пустые строки, оставшиеся после вырезания
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    @staticmethod
    def _strip_meta_reasoning(text: str) -> str:
        """Удаляет мета-рассуждения LLM: вероятности, запросы, внутренний монолог."""
        # Сохраняем блоки кода
        code_blocks = []
        def _save(m):
            code_blocks.append(m.group(0))
            return f'\x00CB{len(code_blocks) - 1}\x00'
        text = re.sub(r'```.*?```', _save, text, flags=re.DOTALL)

        # Мета-фразы инвентаря
        meta_patterns = [
            r'Запрос на добавление предмета в инвентарь\.?\s*',
            r'Запрос на пиццу совпадает с предыдущим контекстом разговора\.?\s*',
            r'Вероятность:\s*\d+%\.?\s*',
            r'Вероятность продолжения темы:\s*\d+%\.?\s*',
            r'Требуется создание описания для [^.]+\.?\s*',
            r'Инвентарь обновл[её]н\.?\s*',
            r'Предмет получен\.?\s*',
            r'Пицца получена\.?\s*',
            r'\*\s*Запрос на [^.]+\*\s*',
            r'\*\s*Вероятность[^*]+\*\s*',
            r'\*\s*Требуется[^*]+\*\s*',
            r'Я принимаю [^.]+ от вас[^.]*\.?\s*',
        ]
        for pattern in meta_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)

        # Убираем лишние пустые строки
        text = re.sub(r'\n{3,}', '\n\n', text)

        # Восстанавливаем code-блоки
        for i, block in enumerate(code_blocks):
            text = text.replace(f'\x00CB{i}\x00', block)
        return text.strip()

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """ Удаляет Markdown-разметку, которую Telegram не поддерживает.
        Жирный (**), курсив (*), код (`), спойлер (||), подчеркивание (__), 
        выделение (==) — остаётся, их конвертит _md_to_html.
        Code-блоки (```...```) не трогаются — их обрабатывает file_sender."""
        
        # Сохраняем блоки кода, чтобы не повредить их чисткой
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

    def _process_todo_marker(
        self, response: str, chat_id: str, user_name: str,
        fallback_task: Optional[str] = None,
        fallback_done_index: Optional[int] = None,
        user_text: str = "",
    ) -> str:
        """Парсит маркеры [TODO_ADD:...] и [TODO_DONE:N], обновляет список дел.
        Список дел отправляется отдельным сообщением через _pending_list_messages.
        Эвристический fallback подтверждается локальной LLM (_confirm_intent)."""
        if not self.todo_manager:
            return response

        # Удаление: [TODO_DONE:N]
        done_match = re.search(r'\[TODO_DONE:(\d+)\]', response)
        if done_match:
            index = int(done_match.group(1))
            response = response[:done_match.start()] + response[done_match.end():]
            result = self.todo_manager.remove_item(chat_id, index)
            if result:
                self._pending_lists(chat_id).append(result)
            return response.strip()

        # Fallback удаление через эвристику — подтверждаем через LLM, иначе
        # «готово, прочитал 3 главы» молча удаляло пункт №3
        if fallback_done_index is not None:
            verdict = self._confirm_intent(user_text, f"пункт №{fallback_done_index}", "todo_remove")
            if verdict == "ADD":
                result = self.todo_manager.remove_item(chat_id, fallback_done_index)
                if result:
                    self._pending_lists(chat_id).append(result)
            elif verdict == "ASK":
                self._pending_lists(chat_id).append(f"Отметить пункт №{fallback_done_index} как выполненный?")
            return response.strip()

        # Добавление: [TODO_ADD:...]
        match = re.search(r'\[TODO_ADD:([^\]]+)\]', response)
        task = None
        if match:
            task = match.group(1).strip()
            response = response[:match.start()] + response[match.end():]
        elif fallback_task:
            # Эвристический fallback — подтверждаем через LLM
            verdict = self._confirm_intent(user_text, fallback_task, "todo_add")
            if verdict == "ADD":
                task = fallback_task
            elif verdict == "ASK":
                self._pending_lists(chat_id).append(f"Записать «{fallback_task}» в список дел?")
            # SKIP — игнорируем

        if task:
            todo_list = self.todo_manager.add_item(chat_id, user_name, task)
            self._pending_lists(chat_id).append(todo_list)

        return response.strip()

    def _process_inventory_markers(self, response: str, fallback_add: Optional[str] = None, fallback_remove: Optional[str] = None, giver_name: str = "", user_text: str = "", chat_id=None) -> str:
        """Парсит маркеры [INVENTORY_ADD:...], [INVENTORY_REMOVE:...], [INVENTORY_USE:...], обновляет инвентарь.
        Инвентарь отправляется отдельным сообщением через _pending_list_messages (per-chat бакет
        по chat_id — иначе при параллельных чатах список уезжал не в тот чат).
        Эвристический fallback подтверждается локальной LLM (_confirm_intent), чтобы не добавлять
        предметы из обычных реплик («получил жабку» не должно добавлять «л жабку»)."""
        if not self.inventory_manager:
            return response

        inventory_changed = False

        # INVENTORY_USE — бот использует предмет (удаляется)
        use_match = re.search(r'\[INVENTORY_USE:([^\]]+)\]', response)
        if use_match:
            name = use_match.group(1).strip()
            response = response[:use_match.start()] + response[use_match.end():]
            self.inventory_manager.use_item(name)
            inventory_changed = True

        # INVENTORY_ADD — приоритет: маркер от LLM
        # Формат из промпта: [INVENTORY_ADD:Название:описание:YYYY-MM-DD] (дата опциональна)
        add_match = re.search(r'\[INVENTORY_ADD:([^:\]]+?)(?::([^:\]]*))?(?::(\d{4}-\d{2}-\d{2}))?\]', response)
        if add_match:
            name = add_match.group(1).strip()
            desc = (add_match.group(2) or "").strip()
            expires = add_match.group(3)
            response = response[:add_match.start()] + response[add_match.end():]
            # Дополняем описание/срок через локальную модель, если LLM их не указала
            desc, expires = self._enrich_inventory_item(name, desc, expires)
            self.inventory_manager.add_item(name, desc, source=giver_name, expires=expires)
            inventory_changed = True
        # Fallback: эвристика нашла предмет, но маркера нет — подтверждаем через LLM
        elif fallback_add:
            verdict = self._confirm_intent(user_text, fallback_add, "inventory_add")
            if verdict == "ADD":
                f_desc, f_expires = self._enrich_inventory_item(fallback_add)
                self.inventory_manager.add_item(fallback_add, f_desc, source=giver_name, expires=f_expires)
                inventory_changed = True
            elif verdict == "ASK":
                # Локальная LLM недоступна — переспрашиваем вместо слепого добавления
                self._pending_lists(chat_id).append(f"Добавить «{fallback_add}» в инвентарь?")
            # SKIP — игнорируем, предмет не создаётся

        # INVENTORY_REMOVE — приоритет: маркер от LLM (пользователь забирает или отменяет)
        remove_match = re.search(r'\[INVENTORY_REMOVE:([^\]]+)\]', response)
        if remove_match:
            name = remove_match.group(1).strip()
            response = response[:remove_match.start()] + response[remove_match.end():]
            self.inventory_manager.remove_item(name)
            inventory_changed = True
        elif fallback_remove:
            verdict = self._confirm_intent(user_text, fallback_remove, "inventory_remove")
            if verdict == "ADD":
                self.inventory_manager.remove_item(fallback_remove)
                inventory_changed = True
            elif verdict == "ASK":
                self._pending_lists(chat_id).append(f"Убрать «{fallback_remove}» из инвентаря?")

        # Проверяем просроченные предметы
        expired = self.inventory_manager.remove_expired_items()
        if expired:
            inventory_changed = True

        if inventory_changed:
            self._pending_lists(chat_id).append(self.inventory_manager.get_list_text())

        return response.strip()

    # File helpers

    _FULL_DOC_KEYWORDS = [
        "перескажи", "пересказ", "резюме", "суммаризуй", "суммаризация",
        "краткое содержание", "основная мысль", "главная идея",
        "перепиши текст", "изложи", "выжимка",
        "расскажи содержание", "о чём документ", "о чем документ",
        "расскажи текст", "весь текст", "полный текст",
        "доклад по", "анализ документа", "разбор документа", "проанализируй"
    ]

    def _get_persona_context_for_search(self) -> str:
        """Собирает краткий контекст персоны для QueryEnhancer (имя, роль, ключевые черты)."""
        data = self.persona.persona_data
        parts = []
        
        name = data.get("name", self.persona_name)
        if name:
            parts.append(f"Имя персоны: {name}")
        
        description = data.get("description", "")
        if description:
            parts.append(f"Описание: {description}")
        
        # Из system_prompt берём только первые 500 символов — основная роль и внешность
        system_prompt = data.get("system_prompt", "")
        if system_prompt:
            # Берём начало до первого крупного раздела
            prompt_preview = system_prompt[:500].strip()
            if prompt_preview:
                parts.append(f"Роль и характер: {prompt_preview}")
        
        return "\n".join(parts) if parts else ""

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

    # Inventory helpers

    _INVENTORY_USAGE_PATTERNS = [
        # Прямые утверждения: "ты съел X", "ты использовал Y"
        re.compile(r"(?:ты|вы)\s+(?:использовал[ао]?|съел[ао]?|выпил[ао]?|применил[ао]?|взял[ао]?|открыл[ао]?|закурил[ао]?|съел[ао]?|съела|съел|поел[ао]?|попил[ао]?|съешь|выпей|используй|примени|съешь|выпей|открой|закури|возьми)\s+(.+)", re.IGNORECASE),
        # "ты уже ..."
        re.compile(r"(?:ты|вы)\s+(?:уже)\s+(?:использовал[ао]?|съел[ао]?|выпил[ао]?|применил[ао]?|взял[ао]?|открыл[ао]?|съел[ао]?|поел[ао]?|попил[ао]?)\s+(.+)", re.IGNORECASE),
        # Предложения совместного действия: "давай съедим X", "давай выпьем Y"
        re.compile(r"(?:давай|давайте)\s+(?:вместе\s+)?(?:съедим|поедим|выпьем|попьем|используем|применим|откроем|возьмем|съедим|выпьем)\s+(.+)", re.IGNORECASE),
        # "X, которая у тебя есть" + контекст совместного использования
        re.compile(r"(?:съедим|поедим|выпьем|попьем|используем|применим|откроем|возьмем)\s+(.+?)(?:\s+котор[аяое]\s+у\s+тебя\s+есть|\s+из\s+инвентаря|\s+что\s+у\s+тебя\s+есть)", re.IGNORECASE),
    ]

    def _find_inventory_item_by_substring(self, text: str) -> Optional[str]:
        """
        Ищет предмет в инвентаре по подстроке.
        Например, 'пиццу' найдет 'Пицца с ананасом и халапеньо'.
        """
        if not self.inventory_manager:
            return None
        text_lower = text.strip().lower()
        items = self.inventory_manager.get_items()
        # Сначала точное совпадение
        for item in items:
            if item.name.lower() == text_lower:
                return item.name
        # Затем по подстроке (предмет содержит запрос)
        for item in items:
            if text_lower in item.name.lower():
                return item.name
        # Затем запрос содержит название предмета
        for item in items:
            if item.name.lower() in text_lower:
                return item.name
        return None

    def _extract_user_reported_usage(self, text: str) -> Optional[str]:
        """
        Извлекает название предмета из сообщения пользователя о том,
        что бот использовал/съел/выпил предмет.
        Например: 'ты использовал меч', 'ты съел яблоко', 'ты выпил зелье'.
        """
        for pattern in self._INVENTORY_USAGE_PATTERNS:
            match = pattern.search(text)
            if match:
                item = match.group(1).strip()
                # Убираем trailing punctuation
                item = re.sub(r"[.!?\s]+$", "", item).strip()
                # Убираем 'пожалуйста' и подобное
                item = re.sub(r"\s+пожалуйста\s*$", "", item, flags=re.IGNORECASE).strip()
                if item and len(item) > 1:
                    return item
        return None

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

    def get_ltm_privacy(self, user_id: str) -> str:
        """Режим приватности LTM пользователя: 'smart' (по умолчанию) | 'strict'."""
        return self.memory.ltm.get_privacy_mode(user_id)

    def set_ltm_privacy(self, user_id: str, mode: str) -> str:
        """Устанавливает режим приватности LTM. Возвращает установленный режим."""
        return self.memory.ltm.set_privacy_mode(user_id, mode)

    def forget_fact(self, query: str, user_id: str) -> Optional[str]:
        """Точечное забывание факта из LTM. Возвращает текст удалённого или None."""
        return self.memory.ltm.forget(query, user_id)

    def get_relations_text(self, user_id: str, chat_id: str = None) -> str:
        """Социальный граф: связи пользователя и (в группе) других участников."""
        from app.core.users import get_user_tag
        lines = []
        own = self.memory.ltm.get_facts_by_category(user_id, "Relation", chat_id=chat_id)
        if own:
            lines.append("Твои связи:")
            lines += [f"  - {r.partition(':')[2].strip()}" for r in own]

        if chat_id and str(chat_id) != str(user_id):
            facts = self.memory.ltm.get_chat_facts(chat_id, exclude_user_id=user_id)
            rel = [f for f in facts if f["category"] == "Relation"]
            if rel:
                if lines:
                    lines.append("")
                lines.append("Связи участников этого чата:")
                for f in rel:
                    name = f["user_name"] or get_user_tag(f["user_id"]) or "Участник"
                    lines.append(f"  - {name}: {f['fact'].partition(':')[2].strip()}")

        return "\n".join(lines) if lines else "Пока ничего не знаю о связях."

    def debug_context(self, user_id: str, chat_id: str = None, query: str = "") -> str:
        """Собирает блоки, которые ушли бы в промпт (отладка для owner'а)."""
        stm_messages, ltm_facts, stm_relevant = self.memory.get_context(
            user_id, chat_id, ltm_query=query or "контекст"
        )
        parts = [f"== LTM facts ({len(ltm_facts)}) =="]
        parts += ltm_facts or ["(пусто)"]
        rules = self.memory.ltm.get_facts_by_category(user_id, "Rule", chat_id=chat_id)
        parts.append(f"\n== Rules ({len(rules)}) ==")
        parts += rules or ["(пусто)"]
        if chat_id and str(chat_id) != str(user_id):
            block = self.memory.get_chat_facts_block(chat_id, exclude_user_id=user_id)
            parts.append("\n== Chat facts (другие участники) ==")
            parts.append(block or "(пусто)")
        parts.append(f"\n== STM последние ({len(stm_messages)}) ==")
        for m in stm_messages:
            name = m.get("user_name") or m.get("role")
            parts.append(f"[{m['role']}] {name}: {m['content'][:120]}")
        parts.append(f"\n== STM relevant ({len(stm_relevant)}) ==")
        for m in stm_relevant:
            parts.append(f"- {m['content'][:120]}")
        return "\n".join(parts)

    def export_ltm_file(self, user_id: str) -> Optional[str]:
        """Создаёт JSON-файл со всеми фактами LTM пользователя. Путь к файлу или None."""
        import tempfile
        from datetime import datetime
        facts = self.memory.ltm.get_all_facts_with_meta(user_id)
        if not facts:
            return None
        payload = {
            "user_id": str(user_id),
            "persona": self.persona_name,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "privacy_mode": self.get_ltm_privacy(user_id),
            "facts": facts,
        }
        tmp_dir = tempfile.mkdtemp(prefix="ltm_export_")
        path = os.path.join(tmp_dir, f"ltm_{user_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

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

    def setup_proactive(self, sender: MessageSender):
        """Создает ProactiveMessaging с готовым sender. Вызывается после инициализации Telegram Bot."""
        if not self._activity_tracker:
            return
        from app.features.proactive_messaging import ProactiveConfig, ProactiveMessaging
        proactive_config = self.features.get("proactive", {})
        self._sender = sender
        self.proactive = ProactiveMessaging(
            config=ProactiveConfig.from_dict(proactive_config),
            router=self.router,
            persona=self.persona,
            memory=self.memory,
            activity_tracker=self._activity_tracker,
            get_last_message_time=self._get_last_message_time,
            sender=sender,
            context=self.context,
            self_memory=self.self_memory,
        )
        # Создаем досье на чат
        from app.features.chat_dossier import ChatDossier
        self.proactive.dossier = ChatDossier(context=self.context)
        logger.info(f"  [{self.persona_name}] Proactive messaging инициализирован с sender и досье")

    def setup_learning(self, sender: MessageSender):
        """Передаёт sender, роутеры и memory в learning_manager. Вызывается после инициализации Telegram Bot."""
        if not self.learning_manager:
            return
        self.learning_manager.set_sender(sender)
        self.learning_manager.set_routers_persona(self.router, self.persona, self._local_router)
        self.learning_manager.set_memory(self.memory)
        logger.info(f"  [{self.persona_name}] Learning manager инициализирован с sender, роутерами и memory")

    # ── слэш-команды: создание сущности + ответ через LLM в образе персоны ──

    def describe_image(self, image_bytes: bytes, question: str = "") -> Optional[str]:
        """OCR + описание изображения через vision-провайдер основного роутера.
        Возвращает None, если ни один vision-провайдер не настроен/не ответил."""
        if not self.router.supports_vision():
            return None
        prompt = (
            "Пользователь прислал изображение. Вытащи с него весь видимый текст (OCR) "
            "и коротко опиши, что изображено (1-2 предложения).\n"
            "Формат ответа:\nТЕКСТ: <текст с изображения или «нет текста»>\nОПИСАНИЕ: <...>"
        )
        if question:
            prompt += f"\nДополнительно ответь на вопрос пользователя об изображении: {question}"
        return self.router.get_response_with_image(prompt, image_bytes)

    def _enrich_inventory_item(self, name: str, desc: str = "", expires: Optional[str] = None) -> tuple:
        """Дополняет описание и срок годности предмета через ЛОКАЛЬНУЮ модель
        (основную не трогаем). Срок придумывается только для портящихся предметов.
        Возвращает (desc, expires) — незаполненные поля остаются как были."""
        if not name:
            return desc, expires
        if not self._local_router or not self._local_router.is_available():
            logger.info(f"[Inventory] Локальная модель недоступна — «{name}» без описания/срока")
            return desc, expires
        try:
            from datetime import date
            today = date.today().isoformat()
            messages = [
                {"role": "system", "content": (
                    f"Сегодня {today}. Для предмета придумай:\n"
                    "1) ОПИСАНИЕ — краткое (5-15 слов), без названия и кавычек.\n"
                    "2) СРОК — дату годности ГГГГ-ММ-ДД, ТОЛЬКО если предмет портится "
                    "(еда, напитки, цветы и т.п.); для непортящихся предметов напиши «-».\n"
                    "Формат ответа строго две строки:\nОПИСАНИЕ: ...\nСРОК: ..."
                )},
                {"role": "user", "content": name.strip()},
            ]
            resp = self._local_router.get_response(messages, temperature=0.3, max_tokens=80, top_p=0.9)
            if resp:
                for line in resp.strip().splitlines():
                    line = line.strip()
                    low = line.lower()
                    if not desc and low.startswith("описание"):
                        candidate = line.partition(":")[2].strip().strip('"\'""«»').strip()
                        if 3 <= len(candidate) <= 120:
                            desc = candidate
                    elif not expires and low.startswith("срок"):
                        m = re.search(r"\d{4}-\d{2}-\d{2}", line)
                        if m and m.group(0) >= today:  # прошедшую дату не принимаем
                            expires = m.group(0)
                logger.info(f"[Inventory] «{name}» → описание={desc!r}, срок={expires!r}")
            else:
                logger.info(f"[Inventory] Локальная модель не ответила для «{name}»")
        except Exception as e:
            logger.debug(f"[Inventory] Обогащение предмета не удалось: {e}")
        return desc, expires

    def command_reply(
        self, context_note: str, note_kind: str,
        chat_id: str, user_id: str, user_name: str, user_input: str,
    ) -> str:
        """
        Генерирует ответ на слэш-команду в характере персоны.
        Сущность (напоминание/задача/предмет/сессия обучения) уже создана в _dispatch_command —
        здесь только формируется контекстная инструкция и вызывается LLM.
        БЕЗ detection-блоков и обработки маркеров (чтобы не создать сущность повторно).
        """
        # Сохраняем сообщение пользователя (текст команды) в STM
        self.memory.add_message("user", user_input, user_id, chat_id, user_name)

        # Собираем контекст
        stm_messages, ltm_facts, stm_relevant = self.memory.get_context(
            user_id, chat_id, ltm_query=user_input
        )
        context_parts = []
        if ltm_facts:
            context_parts.append("\n".join(ltm_facts))
        # В группе — факты других участников, сказанные публично в этом чате
        if chat_id and str(chat_id) != str(user_id):
            chat_facts_block = self.memory.get_chat_facts_block(chat_id, exclude_user_id=user_id)
            if chat_facts_block:
                context_parts.append(chat_facts_block)
        memory_text = "\n\n".join(context_parts)
        self_memory_block = None
        if self.self_memory:
            self_memory_block = self.self_memory.get_context_block()
        stm_relevant_text = None
        if stm_relevant:
            parts = []
            for msg in stm_relevant:
                role_ru = msg.get("user_name", "Пользователь") if msg["role"] == "user" else "Ассистент"
                parts.append(f"  {role_ru}: {msg['content'][:200]}")
            stm_relevant_text = "\n".join(parts)

        # Маршрутизируем note в нужный *_context параметр prepare_messages
        kwargs = dict(
            user_message=user_input, memory_context=memory_text, history=stm_messages,
            user_id=user_id, user_name=user_name,
            has_files=False, self_memory_block=self_memory_block,
            stm_relevant=stm_relevant_text,
        )
        if note_kind == "reminder":
            kwargs["reminder_context"] = context_note
        elif note_kind == "todo":
            kwargs["todo_context"] = context_note
        elif note_kind == "learning":
            kwargs["learning_context"] = context_note
        elif note_kind == "inventory":
            kwargs["inventory_context"] = context_note

        messages = self.persona.prepare_messages(**kwargs)
        settings = self.persona.get_settings()
        answer = self.router.get_response(messages, **settings)
        if not answer:
            logger.error("Все LLM-провайдеры недоступны, ответ не сгенерирован")
            return "Сейчас все LLM-провайдеры недоступны. Попробуй позже."

        # Защита от обрыва по max_tokens — та же логика, что в основном процессе сообщений.
        _continuations = 0
        while _looks_truncated(answer) and _continuations < 2:
            follow_up_messages = messages + [
                {"role": "assistant", "content": answer},
                {"role": "user", "content": "Ты остановился на середине фразы. Допиши строго с того места, где прервался — не повторяй уже написанное и не начинай заново."},
            ]
            cont = self.router.get_response(follow_up_messages, **settings)
            if not cont:
                break
            answer = answer + cont
            _continuations += 1

        answer = self._clean_response(answer)

        self.memory.add_message("assistant", answer, user_id, chat_id)
        return answer

    def _dispatch_command(
        self, kind: str, args: str, chat_id: str, user_id: str, user_name: str,
    ) -> str:
        """
        Оркестратор слэш-команд. Создаёт сущность через manager API и формирует
        контекстную инструкцию для ответа в образе персоны.
        kind: 'remind' | 'todo' | 'inventory' | 'learn'
        """
        args = (args or "").strip()
        user_input_cmd = f"/{kind} {args}".strip()  # что сохранится в STM
        # Якорь личности автора команды — иначе в групповом чате LLM может
        # приписать команду другому участнику из истории (по имени/теме)
        who = f"{user_name} (ID:{user_id})"

        # Как и в process_message: сбрасываем флаг «последний ответ — вопрос бота»
        # ЭТОГО чата. Команда тоже может закончиться вопросом (пока только /learn —
        # «как часто присылать уроки?»), и telegram-слой по флагу регистрирует
        # message_id ответа.
        self._pending_question_kind[str(chat_id)] = None

        if kind == "remind":
            if not self.reminder_manager:
                return "Напоминания не активны для этой персоны."
            if not args:
                return "Использование: /remind <что напомнить> [через N ...]"
            parsed = parse_reminder("напомни " + args)
            if parsed:
                rem_task, rem_delay = parsed
                if rem_task:
                    rem_task = self._reformulate_task(rem_task)
                topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                delay_text = self.reminder_manager.format_delay(rem_delay)
                self.reminder_manager.add_reminder(chat_id, user_name or "Пользователь", rem_task, rem_delay, topic_id)
                task_display = f" «{rem_task}»" if rem_task else ""
                note = (
                    f"Пользователь {who} командой попросил напомнить{task_display} через {delay_text}. "
                    "Напоминание уже запланировано — подтверди это в своём стиле, коротко. "
                    f"Обращайся именно к {user_name}, а не к другим участникам чата."
                )
            else:
                # Время не указано — переспрашиваем, запоминаем задачу
                rem_task = self._reformulate_task(args)
                self.reminder_manager.begin_pending_remind(chat_id, rem_task)
                note = (
                    f"Пользователь {who} командой попросил напомнить «{rem_task}», но не указал через сколько. "
                    "Уточни когда ему напомнить (например, «через 2 часа», «завтра в 12») — в своём стиле, коротко. "
                    f"Обращайся именно к {user_name}, а не к другим участникам чата."
                )
            return self.command_reply(note, "reminder", chat_id, user_id, user_name, user_input_cmd)

        if kind == "todo":
            if not self.todo_manager:
                return "Список дел не активен для этой персоны."
            if not args:
                return "Использование: /todo <задача>"
            task = self._reformulate_task(args)
            self.todo_manager.add_item(chat_id, user_name or "Пользователь", task)
            note = (
                f"Пользователь {who} командой добавил в список дел задачу «{task}». "
                "Подтверди это в своём стиле, коротко."
            )
            return self.command_reply(note, "todo", chat_id, user_id, user_name, user_input_cmd)

        if kind == "inventory":
            if not self.inventory_manager:
                return "Инвентарь не активен для этой персоны."
            if not args:
                return "Использование: /inventory <название предмета>[: описание]"
            # Разбираем название и опциональное описание (разделитель : или —)
            parts = re.split(r"\s*[:—–-]\s*", args, maxsplit=1)
            name = parts[0].strip()
            desc = parts[1].strip() if len(parts) > 1 else ""
            # Описание и срок годности (для портящегося) придумает локальная модель
            desc, expires = self._enrich_inventory_item(name, desc, None)
            result = self.inventory_manager.add_item(name, description=desc, source=user_name or "пользователь", expires=expires)
            note = (
                f"Пользователь {who} командой положил тебе в инвентарь предмет «{name}»"
                + (f" (описание: {desc})" if desc else "")
                + (f" (годен до: {expires})" if expires else "")
                + f". Результат: {result} "
                "Подтверди это в своём стиле, коротко."
            )
            return self.command_reply(note, "inventory", chat_id, user_id, user_name, user_input_cmd)

        if kind == "learn":
            if not self.learning_manager:
                return "Режим обучения не активен для этой персоны."
            if not args:
                return "Использование: /learn <тема>"
            subject = args
            self.learning_manager.begin_setup(chat_id, subject, user_id or "default", user_name or "Пользователь")
            note = (
                f"Пользователь {who} командой попросил научить его «{subject}». "
                "Спроси коротко и в своём стиле, как часто присылать уроки "
                "(например: раз в день, каждые 2 часа). Обучение начнётся после его ответа."
            )
            # Ответ ниже — вопрос «как часто?»: отмечаем, чтобы telegram-слой
            # зарегистрировал его message_id и reply пользователя распознался
            # как ответ о частоте (без этого reply-gate для /learn не работал).
            self._pending_question_kind[str(chat_id)] = "frequency"
            return self.command_reply(note, "learning", chat_id, user_id, user_name, user_input_cmd)

        return "Неизвестная команда."

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

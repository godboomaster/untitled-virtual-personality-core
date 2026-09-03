"""
BotInstance — один бот с конкретной персоной и набором фич.
Содержит VirtualPersonality, FileVectorDB и читает features из YAML.
"""

import re
import os
import json
import time
import yaml
import logging
from pathlib import Path
from typing import Optional, Dict, List
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from app.core.persona import PersonaLayer, _format_msg_ts
from app.core.language import detect_language, detect_dialogue_language
from app.core.memory import MemoryManager
from app.core.router import ModelRouter
from app.core.config import Config
from app.core.file_vector_db import FileVectorDB
from app.core.file_reader import extract_text, MAX_FILE_SIZE_DEFAULT
from app.core.interfaces import MessageSender
from app.core.users import get_username
from app.features.todo_manager import (
    TodoManager, is_todo_request, extract_task,
    is_todo_done_request, extract_todo_done_index, is_todo_list_request,
)
from app.features.reminder_manager import (
    ReminderManager, parse_reminder, parse_recurring, parse_postpone,
    extract_postpone_hint, format_schedule,
)
from app.features.learning_manager import LearningManager, parse_frequency, classify_continue_answer
from app.features.learning_intent import classify_learning_intent, extract_subject
from app.features.inventory_manager import (
    InventoryManager,
    is_inventory_add_request,
    is_inventory_remove_request,
    extract_inventory_item,
    extract_inventory_remove,
)
from app.features.computer_control import (
    ComputerControlManager, classify_confirmation, config_enabled as cc_config_enabled,
    PAGE_REF, parse_cart_request, parse_click_request, parse_close_request,
    parse_control_mode, parse_download_request, parse_key_request,
    parse_media_request,
    parse_open_on_page, parse_open_many, parse_open_with_url, parse_page_question,
    parse_read_request,
    parse_scroll_request, parse_search_on_site, parse_send_request,
    parse_slider_request,
    parse_tab_list_query, parse_tab_switch, parse_type_request)
from app.features.scenario_manager import ScenarioManager

logger = logging.getLogger(__name__)

# Сколько последних реплик передавать в query rewriter для разрешения кореференций
# кореференций - местоимения и указательные слова, которые ссылаются на что-то из предыдущего контекста
_REWRITE_HISTORY = 8

# Эвристика обрыва ответа по max_tokens (learning_manager._looks_truncated —
# та же логика, но там она приватная для LLM-вызовов учебного модуля).
_SENTENCE_END_RE = re.compile(r'[.!?…»"\)\]]\s*$')

# Хвостовой «декор» реплики — каомодзи/эмодзи вроде «(。•̀ᴗ-)✧», «(´• ω •`)ノ» —
# не признак обрыва: срезаем и судим по тексту до него (знаки завершения
# фразы в классе-исключении, поэтому срезка остановится на них).
_TRAILING_DECOR_RE = re.compile(r'[^0-9A-Za-zА-Яа-яЁё.!?…»"\)\]]+$')


def _fmt_reminder_choices(choices: list) -> str:
    """Нумерованный список напоминаний для LLM-контекста: 1) "задача" at 12:30."""
    from datetime import datetime as _dt
    parts = []
    for i, c in enumerate(choices):
        when_dt = _dt.fromtimestamp(c["trigger_at"])
        when = when_dt.strftime("%H:%M")
        if when_dt.date() != _dt.now().date():
            when = when_dt.strftime("%d.%m %H:%M")
        parts.append(f"{i+1}) \"{c.get('task') or '?'}\" at {when}")
    return "; ".join(parts)


def _postpone_result_context(result: Optional[dict]) -> str:
    """LLM-контекст после попытки переноса напоминания (см. process_message).
    Явно сообщаем, применён ли перенос, — иначе модель «подтверждала»
    перенос, которого на самом деле не было."""
    if not result:
        return (
            "The user asked to move a reminder, but there is nothing to move "
            "(no active and no recently fired reminders). Say there is nothing "
            "to move — in your own style, briefly. Do NOT confirm any rescheduling."
        )
    from datetime import datetime as _dt
    when_dt = _dt.fromtimestamp(result["trigger_at"])
    when = when_dt.strftime("%H:%M")
    if when_dt.date() != _dt.now().date():
        when = when_dt.strftime("%d.%m %H:%M")
    task_disp = f" '{result['task']}'" if result.get("task") else ""
    if result.get("recreated"):
        return (
            f"The user asked to move a reminder{task_disp}, but it had already fired, "
            f"so a NEW reminder with the same task was created — at {when}. "
            f"The reminder is scheduled — confirm briefly in your own style. "
            f"Name the reminder exactly as given above."
        )
    return (
        f"The user asked to move the reminder{task_disp}. "
        f"It is now scheduled for {when} — the change is ALREADY applied. "
        f"Confirm briefly in your own style. "
        f"Name the reminder exactly as given above."
    )


def _looks_truncated(text: str) -> bool:
    """Эвристика обрыва ответа по max_tokens,
    текст не заканчивается знаком завершения предложения.
    Каомодзи/эмодзи на хвосте («…выбрали? (。•̀ᴗ-)✧») обрывом не считаются."""
    t = (text or "").rstrip()
    if not t:
        return False
    t = _TRAILING_DECOR_RE.sub("", t).rstrip()
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

        # Хвост расщеплённого ответа (settings.split_messages): ответ режется
        # по абзацам на отдельные сообщения, первая часть возвращается как обычно,
        # остальные ждут здесь — платформа (веб/TG) забирает их после отправки
        # первой. Per-chat — та же защита от гонок, что у списков выше.
        self._pending_split_messages: Dict[str, List[str]] = {}

        # Читаем features из YAML
        persona_data = self.persona.persona_data
        self.features: dict = persona_data.get("features", {}) # получаем навыки персоны

        # Уровень интеллекта (план уровней): tier + overrides; без блока
        # intellect — legacy-режим, уровневые механики не активируются
        from app.core.intellect import IntellectConfig
        self.intellect = IntellectConfig(persona_data)

        # Платформенное правило финальных вопросов (conversation_style):
        # дефолт rare применяется ко ВСЕМ персонам, включая legacy
        from app.features.conversation_style import ConversationStyleConfig
        self.conversation_style = ConversationStyleConfig(persona_data)

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
            # §3.5 плана уровней: primitive — минимальная вербализация напоминаний
            self.reminder_manager.set_intellect_tier(self.intellect.tier)
            logger.info(f"  [{persona_name}] Reminder manager включён")

        # Inventory manager (только если inventory)
        self.inventory_manager: Optional[InventoryManager] = None
        if self.features.get("inventory", False):
            self.inventory_manager = InventoryManager(context=self.context)
            logger.info(f"  [{persona_name}] Inventory manager включён")

        # Computer control (только если computer_control): открытие сайтов/
        # приложений/именованных задач на компьютере пользователя (уровень 1).
        # Режим выключен: false/отсутствует, пустой dict или enabled: false
        # внутри dict (веб-настройка фич так гасит режим, сохраняя allowlist'ы)
        self.computer_control: Optional[ComputerControlManager] = None
        cc_cfg = self.features.get("computer_control", False)
        if cc_config_enabled(cc_cfg):
            self.computer_control = ComputerControlManager(
                context=self.context, config=cc_cfg)
            logger.info(f"  [{persona_name}] Computer control включён "
                        f"(confirm={self.computer_control.confirm})")

        # Сценарии (запись/воспроизведение цепочек действий) — надстройка над
        # computer_control: без него бессмысленны. `scenarios: false` гасит.
        self.scenario_manager: Optional[ScenarioManager] = None
        if self.computer_control and self.features.get("scenarios", True):
            self.scenario_manager = ScenarioManager(
                context=self.context, computer_control=self.computer_control)
            logger.info(f"  [{persona_name}] Scenario manager включён")

        # Режим управления (per chat): computer control работает ТОЛЬКО в нём
        # («перейди в режим управления»), иначе CC-команды не перехватываются.
        # В режиме наоборот молчат напоминания/дела/инвентарь/обучение
        self._control_mode: set = set()

        # Learning manager (только если learning) — режим обучения по запросу
        self.learning_manager: Optional[LearningManager] = None
        learning_cfg = self.features.get("learning", False)
        if isinstance(learning_cfg, bool):
            learning_on, learning_cfg = learning_cfg, {}
        else:
            # dict: enabled решает явно; без него непустой dict — включён (старое поведение)
            learning_on = bool(learning_cfg) and learning_cfg.get("enabled", True)
        if learning_on:
            self.learning_manager = LearningManager(context=self.context, config=learning_cfg)
            logger.info(f"  [{persona_name}] Learning manager включён")

        # Router (создаём до Memory, чтобы передать в LTM)
        self.router = ModelRouter(context=self.context)

        # Персональный выбор провайдеров (YAML, секция llm):
        # основной + приоритет fallback-цепочки + свои модели + веб-чат сайт
        # + лимиты веб-чатов (webchat_limits)
        llm_cfg = persona_data.get("llm") or {}
        if llm_cfg:
            self.router.set_persona_llm(llm_cfg.get("primary"), llm_cfg.get("fallback"),
                                        llm_cfg.get("models"), webchat=llm_cfg.get("webchat"),
                                        webchat_limits=llm_cfg.get("webchat_limits"))

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

        # Владелец — полная защита от всех блокировок.
        # Fallback: YAML персоны → глобальный OWNER_USER_ID из окружения.
        self.owner: str = str(self.features.get("owner") or os.getenv("OWNER_USER_ID") or "")

        # Однопользовательский режим (веб/API): собеседник один — он и владелец.
        # Флаг выставляет API-реестр (app/api/runtime.py); в Telegram-режиме False.
        self.web_single_user: bool = False

        # Allowed DM users — могут писать в личку, но подлежат наказаниям
        # Пустые записи ("") отбрасываем, id приводим к str; пустой список = ЛС открыты всем
        self.allowed_dm_users: set = {
            str(u).strip() for u in self.features.get("allowed_dm_users", []) if str(u).strip()
        }
        self.blocked_users: set = {
            str(u).strip() for u in self.features.get("blocked_users", []) if str(u).strip()
        }

        # Self memory (эпизодическая память бота)
        # Режим по intellect tier (§3.1): none — модуль не создаётся вообще,
        # primitive — вспышки-впечатления, full — как обычно
        self.self_memory = None
        if self.features.get("self_memory", False) and self.intellect.self_memory_mode != "none":
            from app.core.self_memory import BotSelfMemory
            self.self_memory = BotSelfMemory(
                context=context,
                persona_name=persona_name,
                router=self.router,
                mode=self.intellect.self_memory_mode,
            )
            if self.intellect.self_memory_mode == "primitive":
                logger.info(f"  [{persona_name}] Self memory в примитивном режиме (вспышки-впечатления)")

        # Living persona (слои state/world плана «живой» персоны):
        # тики состояния, офлайн-события мира, суммаризация, сюжетные арки.
        # Intellect tier сужает слои (§3.3-3.4 плана уровней): primitive —
        # world без NPC/арок, события = физические действия с инвентарём
        self.living = None
        try:
            from app.core.living_persona import LivingPersona, LivingPersonaConfig
            _living_cfg = LivingPersonaConfig(self.features)
            if _living_cfg.enabled:
                self.living = LivingPersona(
                    context=context or persona_name,
                    persona=self.persona,
                    router=self.router,
                    config=_living_cfg,
                    self_memory=self.self_memory,
                    intellect=self.intellect,
                    inventory_manager=self.inventory_manager,
                )
                logger.info(
                    f"  [{persona_name}] Living persona включена "
                    f"(state={_living_cfg.state_enabled}, world={_living_cfg.world_enabled}"
                    + (", primitive-режим" if self.intellect.is_primitive else "") + ")")
        except Exception as e:
            logger.warning(f"  [{persona_name}] Living persona не запущена: {e}")

        # Напоминания знают о living-состоянии (mood/energy в тексте, §7)
        if self.reminder_manager is not None and self.living is not None:
            self.reminder_manager.set_living(self.living)

        # Book search (RAG по книге для персон)
        self.book_search = None
        if self.features.get("book_search", False):
            from app.features.book_search import BookSearch
            self.book_search = BookSearch(context=context or persona_name,
                                          router=self.router)
            logger.info(f"  [{persona_name}] Book search включён")

        # Proactive messaging (самоинициатива)
        self.proactive = None
        self._activity_tracker = None
        self._sender: Optional[MessageSender] = None
        # Общее досье на чаты: один экземпляр на бота (proactive + rhythm),
        # иначе два экземпляра перезатирали бы записи друг друга на диске
        self._chat_dossier = None
        proactive_config = self.features.get("proactive", {})
        if isinstance(proactive_config, bool):  # допускаем простой true/false
            proactive_config = {"enabled": proactive_config}
        if proactive_config.get("enabled", False):
            from app.features.proactive_messaging import ProactiveConfig, ProactiveMessaging, ChatActivityTracker
            self._activity_tracker = ChatActivityTracker(context=context)
            # sender будет установлен позже через setup_sender()
            self.proactive = None  # создадим после установки sender
            logger.info(f"  [{persona_name}] Proactive messaging подготовлен (ожидает sender)")

        # Суточный ритм: утреннее приветствие / ночной «пора спать» / погода
        self.rhythm = None
        rhythm_config = self.features.get("rhythm", {})
        if isinstance(rhythm_config, bool):  # допускаем простой true/false
            rhythm_config = {"enabled": rhythm_config}
        if rhythm_config.get("enabled", False):
            if self._activity_tracker is None:
                from app.features.proactive_messaging import ChatActivityTracker
                self._activity_tracker = ChatActivityTracker(context=context)
            # manager создастся позже через setup_rhythm(sender)
            logger.info(f"  [{persona_name}] Rhythm (утро/ночь/погода) подготовлен (ожидает sender)")

        logger.info(f"  [{persona_name}] BotInstance создан | stm_size={self.stm_size} | features: {list(self.features.keys())}")

    def sync_feature_managers(self) -> dict:
        """Приводит менеджеры reminder/todo/inventory в соответствие с self.features
        (живое включение/выключение фич из веб-настроек, без рестарта бота):
        создаёт недостающие, останавливает и убирает лишние. Платформенную
        обвязку (sender, запуск фонового цикла) делает вызывающая сторона —
        см. _apply_feature_managers_live в settings_api / start_bot_features в inbox.
        Возвращает {feature: включена ли} после синхронизации."""
        result = {}
        if self.features.get("reminder", False):
            if self.reminder_manager is None:
                self.reminder_manager = ReminderManager(context=self.context)
                self.reminder_manager.set_intellect_tier(self.intellect.tier)
                logger.info(f"  [{self.persona_name}] Reminder manager включён (live)")
        elif self.reminder_manager is not None:
            self.reminder_manager.stop()
            self.reminder_manager = None
            logger.info(f"  [{self.persona_name}] Reminder manager выключен (live)")
        result["reminder"] = self.reminder_manager is not None

        if self.features.get("todo", False):
            if self.todo_manager is None:
                self.todo_manager = TodoManager(context=self.context)
                logger.info(f"  [{self.persona_name}] Todo manager включён (live)")
        elif self.todo_manager is not None:
            self.todo_manager = None
            logger.info(f"  [{self.persona_name}] Todo manager выключен (live)")
        result["todo"] = self.todo_manager is not None

        if self.features.get("inventory", False):
            if self.inventory_manager is None:
                self.inventory_manager = InventoryManager(context=self.context)
                logger.info(f"  [{self.persona_name}] Inventory manager включён (live)")
        elif self.inventory_manager is not None:
            self.inventory_manager = None
            logger.info(f"  [{self.persona_name}] Inventory manager выключен (live)")
        result["inventory"] = self.inventory_manager is not None
        return result

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

    def is_owner(self, user_id: str) -> bool:
        """Владелец ли пользователь: id из YAML персоны или OWNER_USER_ID.
        В однопользовательском веб-режиме собеседник всегда владелец."""
        if self.web_single_user:
            return True
        return bool(user_id) and user_id in {self.owner, os.getenv("OWNER_USER_ID", "")}

    # Pre-check pipeline

    def pre_check(self, user_id: str, text: str, is_private: bool) -> Optional[str]:

        # Проверки перед обработкой. Возвращает текст ошибки или None если всё ОК.
        
        # 0. Владелец — полная защита
        if self.is_owner(user_id):
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

    def pop_pending_split_messages(self, chat_id) -> List[str]:
        """Забирает хвост расщеплённого ответа чата (и очищает бакет).
        Вызывается платформой сразу после process_message/command_reply —
        до pop_pending_list_messages, чтобы части ушли раньше досылаемых списков."""
        return self._pending_split_messages.pop(str(chat_id), [])

    def split_reply_parts(self, text: str) -> List[str]:
        """Расщепление ответа на отдельные сообщения (settings.split_messages).

        Маркер границы — пустая строка: каждый абзац (блок строк между пустыми
        строками) становится отдельным сообщением. Выключено или абзац один —
        возвращается [text] как есть. Пустые куски отбрасываются."""
        if not text or not text.strip():
            return []
        if not self.persona.settings.get("split_messages"):
            return [text]
        parts = [p.strip() for p in re.split(r"\n[ \t]*\n+", text)]
        parts = [p for p in parts if p]
        return parts or [text.strip()]

    def _save_assistant_reply(self, answer: str, user_id: str, chat_id: str) -> str:
        """Сохраняет ответ персоны в STM и возвращает текст для отправки.

        При включённом settings.split_messages ответ режется на абзацы: каждая
        часть пишется в STM отдельным сообщением (история совпадает с тем, что
        видит пользователь), а возвращается только первая часть — хвост ждёт
        в _pending_split_messages, его платформа досылает следом."""
        parts = self.split_reply_parts(answer)
        if len(parts) <= 1:
            self.memory.add_message("assistant", answer, user_id, chat_id)
            return answer
        for part in parts:
            self.memory.add_message("assistant", part, user_id, chat_id)
        self._pending_split_messages[str(chat_id)] = parts[1:]
        return parts[0]

    # Main processing

    def control_mode_on(self, chat_id) -> bool:
        """Включён ли режим управления (computer control) для чата."""
        return str(chat_id) in self._control_mode

    def _control_mode_switch(self, chat_id: str, turn_on: bool) -> str:
        """Реплика на «перейди в режим управления»/«выйди из режима
        управления» + побочки переключения (чистка подвисших CC-состояний)."""
        if turn_on:
            if not self.computer_control:
                return ("Управление компьютером у меня выключено в "
                        "настройках — включи его в досье («Инструменты»).")
            if chat_id in self._control_mode:
                return ("Я уже в режиме управления. Обратно — «выйди из "
                        "режима управления».")
            self._control_mode.add(chat_id)
            logger.info(f"[BotInstance] режим управления ON (chat {chat_id})")
            return ("Режим управления включён: «открой …», «нажми …», "
                    "«введи …», сценарии — всё работает. На время режима "
                    "молчат: напоминания, список дел, инвентарь, обучение. "
                    "Закончить — «выйди из режима управления».")
        if chat_id not in self._control_mode:
            return "Режим управления и так выключен."
        self._control_mode.discard(chat_id)
        logger.info(f"[BotInstance] режим управления OFF (chat {chat_id})")
        # Подвисшие CC-состояния чата недействительны вне режима
        try:
            if self.computer_control:
                self.computer_control.clear_pending(chat_id)
        except Exception:
            pass
        try:
            if self.scenario_manager:
                if self.scenario_manager.active(chat_id):
                    self.scenario_manager.cancel(chat_id)
                if self.scenario_manager.recording(chat_id):
                    self.scenario_manager.record_stop(chat_id)
        except Exception:
            pass
        return ("Вышел из режима управления — браузером не управляю. "
                "Напоминания, список дел, инвентарь и обучение снова "
                "работают.")

    def process_message(self, user_input: str, user_id: str = "default",
                        chat_id: str = None, user_name: str = None,
                        reply_context: str = None,
                        reply_to_bot_message_id: Optional[int] = None,
                        on_token=None) -> str:
        from app.features.query_rewriter import rewrite_query

        # Очищаем pending-состояние ЭТОГО чата от предыдущего вызова (атрибуты per-chat:
        # process_message выполняется конкурентно в потоках для разных чатов, и общие
        # атрибуты давали гонки — чужой фидбек/вопрос уезжал не в тот чат).
        self._pending_list_messages[str(chat_id)] = []
        # Хвост расщеплённого ответа от предыдущего вызова тоже гасим
        self._pending_split_messages[str(chat_id)] = []
        # Каким был последний ответ-вопрос: 'frequency' | 'continue' | None.
        # Нужно telegram-слою, чтобы зарегистрировать отправленное сообщение как «вопрос бота»
        # для reply-to-логики обучения (пользователь может ответить reply-ом на этот вопрос).
        self._pending_question_kind[str(chat_id)] = None
        # Локальная переменная (раньше — общий атрибут, та же гонка): готовый ответ,
        # минующий основной LLM-вызов (фидбек теста, реплики setup/continue обучения).
        skip_llm_answer = None
        # Вопрос о секции открытой страницы («что находится в X?»): живой текст
        # секции заполняется в cc fast-path ниже и уезжает в LLM контекстом
        # (context_parts_out) — список/ответ формулирует модель, не шаблон
        page_section_note = None

        logger.info(f"[BotInstance] process_message START: '{user_input[:60]}' | chat_id={chat_id}")

        # «перейди в режим управления» / «выйди из режима управления» —
        # переключатель computer control. Работает всегда и раньше всех
        # fast-path: иначе «выйди…» мог бы съесть CC-парсер, а «перейди…» —
        # отвечаться LLM
        if chat_id:
            _mode = parse_control_mode(user_input)
            if _mode is not None:
                _cm_reply = self._control_mode_switch(str(chat_id), _mode)
                self.memory.add_message("user", user_input, user_id, chat_id, user_name)
                self.memory.add_message("assistant", _cm_reply, user_id, chat_id)
                if self.proactive:
                    self.proactive.record_user_response(chat_id)
                return _cm_reply

        # Быстрый путь computer_control: перехват «да»/«нет» на pending-действие
        # и голая команда «открой X» — оба обслуживаются шаблонно, весь тяжёлый
        # LLM-пайплайн (rewrite/поиск/LTM/генерация, ~10+ сек) пропускается.
        # Работает только в режиме управления («перейди в режим управления»).
        if (self.computer_control and chat_id
                and self.control_mode_on(chat_id)):
            # Сценарии — ДО pending-confirm и fast-path парсеров: ответы слотов
            # («гавайскую») и «отмена» при живом прогоне не должны уходить
            # в команды странице; «запомни сценарий X» и имя сценария —
            # тоже раньше «открой X»
            if self.scenario_manager:
                sc_reply = None
                try:
                    if self.scenario_manager.active(chat_id):
                        sc_reply = (self.scenario_manager.cancel(chat_id)
                                    if self.scenario_manager.parse_cancel(user_input)
                                    else self.scenario_manager.feed(
                                        chat_id, user_input, self.router))
                    else:
                        # «начни записывать сценарий (X)» — явные скобки
                        # записи; «сохрани сценарий» внутри неё берёт трассу
                        # с момента старта (обрабатывает record_reply)
                        sc_start = self.scenario_manager.parse_start_record(
                            user_input)
                        if sc_start is not None:
                            sc_reply = self.scenario_manager.record_start(
                                chat_id, sc_start)
                        elif self.scenario_manager.parse_stop_record(user_input):
                            sc_reply = self.scenario_manager.record_stop(chat_id)
                        else:
                            sc_save = self.scenario_manager.parse_save_request(user_input)
                            if sc_save is not None:
                                sc_reply = self.scenario_manager.record_reply(
                                    chat_id, sc_save, self.router)
                            else:
                                sc_name = self.scenario_manager.find_scenario(user_input)
                                if sc_name:
                                    logger.info(f"[Scenarios] fast-path: "
                                                f"'{user_input[:40]}' → «{sc_name}»")
                                    sc_reply = self.scenario_manager.start(
                                        sc_name, chat_id, self.router)
                except Exception as e:
                    logger.warning(f"[Scenarios] fast-path упал: {e}")
                    self.scenario_manager.cancel(chat_id)
                    sc_reply = f"Сценарий сломался ({str(e)[:80]}) — отменил его."
                if sc_reply is not None:
                    self.memory.add_message("user", user_input, user_id, chat_id, user_name)
                    self.memory.add_message("assistant", sc_reply, user_id, chat_id)
                    if self.proactive and chat_id:
                        self.proactive.record_user_response(chat_id)
                    return sc_reply
            cc_pending = self.computer_control.get_pending(chat_id)
            if cc_pending:
                cc_verdict = classify_confirmation(user_input)
                cc_reply = None
                if cc_verdict == "YES":
                    self.computer_control.stats["confirmed"] += 1
                    # Pending снимаем ДО исполнения: иначе следующее «да»
                    # (на любой другой вопрос) исполнит действие повторно
                    self.computer_control.clear_pending(chat_id)
                    cc_ok, cc_detail = self.computer_control.execute(
                        cc_pending, chat_id, router=self.router)
                    cc_reply = (
                        f"Готово, {self.computer_control.describe_done(cc_pending)}."
                        if cc_ok else
                        f"Не удалось {self.computer_control.describe(cc_pending)}: {cc_detail}."
                    )
                elif cc_verdict == "NO":
                    self.computer_control.stats["declined"] += 1
                    self.computer_control.clear_pending(chat_id)
                    cc_reply = "Хорошо, не выполняю."
                if cc_reply is not None:
                    self.memory.add_message("user", user_input, user_id, chat_id, user_name)
                    self.memory.add_message("assistant", cc_reply, user_id, chat_id)
                    if self.proactive and chat_id:
                        self.proactive.record_user_response(chat_id)
                    return cc_reply
                # UNKNOWN — не перехватываем: сообщение уходит в обычный поток,
                # pending живёт до TTL

            # «включи X на ютубе» (поиск на сайте) проверяем ДО «открой X»:
            # иначе «интерстеллар на кинопоиске» уйдёт в резолв как имя сайта
            cc_action = None
            cc_err = None
            cc_pair = parse_search_on_site(user_input)
            if cc_pair:
                try:
                    cc_action = self.computer_control.resolve_search(*cc_pair)
                except Exception as e:
                    logger.debug(f"[CompControl] fast-path поиск на сайте не удался: {e}")
            # «открой на ciu.nstu.ru/827 студентам — …»: явный адрес в фразе —
            # открываем его, даже когда вокруг длинный текст; хвост по
            # сепараторам « - »/«→» — путь кликами по странице (nav-действие).
            # До клика и open_many: те обе длинную фразу отвергнут по длине
            if cc_action is None:
                cc_nav = parse_open_with_url(user_input)
                if cc_nav:
                    try:
                        cc_action = self.computer_control.resolve_nav(*cc_nav)
                    except Exception as e:
                        logger.debug(f"[CompControl] fast-path явный URL не удался: {e}")
            # «нажми X (на сайте)» / «скачай X» / «введи X в поле Y» /
            # «отправь» (Enter в поле) / «открой X на этой странице» —
            # агентный клик, скачивание и ввод:
            # снапшот страницы + выбор элемента; подтверждение покажет, что
            # именно нажмётся/скачается/введётся. Неудача — честный отказ
            # шаблоном: LLM-путь это выполнить не может, а изобразить
            # выполнение («Действие выполнено», «Введено») — может, поэтому
            # туда не пускаем. «Не наша» команда ввода (resolve_type вернул
            # (None, None) — «напиши мне письмо») уходит в обычный поток.
            # Под-переключатель click: при выключенном клике команда уходит в
            # обычный LLM-поток (фича клика off — бот просто отвечает текстом)
            if cc_action is None and self.computer_control.click:
                cc_parsed = None  # (цель, сайт/PAGE_REF, вид: click/download/type)
                cc_page_goal = parse_open_on_page(user_input)
                if cc_page_goal:
                    cc_parsed = (cc_page_goal, PAGE_REF, "click")
                else:
                    cc_dl = parse_download_request(user_input)
                    if cc_dl:
                        cc_parsed = (cc_dl[0], cc_dl[1], "download")
                    else:
                        cc_type = parse_type_request(user_input)
                        if cc_type:
                            # Сайт/поле/текст разберёт resolve_type по снапшоту
                            cc_parsed = (cc_type, None, "type")
                        else:
                            cc_send = parse_send_request(user_input)
                            if cc_send:
                                # «отправь» — Enter в поле, сайт как у клика
                                cc_parsed = (None, cc_send[1], "send")
                            else:
                                # «пауза»/«тише»/«громче»/«без звука» —
                                # медиа-команды плеера клавишами; ДО
                                # клавиш-«нажми» и клика
                                cc_media = parse_media_request(user_input)
                                if cc_media:
                                    cc_parsed = (cc_media, None, "key")
                                else:
                                    # «нажми пробел/энтер/эскейп…» — клавиша
                                    # в страницу (без выбора элемента); ДО
                                    # generic-клика: «нажми esc» — не цель «esc»
                                    cc_key = parse_key_request(user_input)
                                    if cc_key:
                                        cc_parsed = (cc_key[0], cc_key[1], "key")
                                    else:
                                        # «промотай страницу» / «стоп»: листание в фоне.
                                        # «промотай раздел слева (вверх)» — режим
                                        # уезжает резолверу кортежем ("start", side, dir).
                                        # «стоп» без активного листания резолвер вернёт
                                        # (None, None) — фраза уйдёт в обычный диалог
                                        cc_scroll = parse_scroll_request(user_input)
                                        if cc_scroll:
                                            cc_parsed = ((cc_scroll[0], cc_scroll[2],
                                                          cc_scroll[3]),
                                                         cc_scroll[1], "scroll")
                                        else:
                                            # «убери X из корзины» / «убавь/прибавь X» —
                                            # корзина сайта, ДО generic-клика и до
                                            # инвентаря бота (тот ловит «убери X»)
                                            cc_cart = parse_cart_request(user_input)
                                            if cc_cart:
                                                cc_parsed = (cc_cart, None, "cart")
                                            else:
                                                # «перетащи/поставь слайдер X на N» —
                                                # ползунок на странице; числовой
                                                # хвост «на N» отличает от клика
                                                cc_slider = parse_slider_request(
                                                    user_input)
                                                if cc_slider:
                                                    cc_parsed = (cc_slider[0],
                                                                 cc_slider[1],
                                                                 "slider")
                                                else:
                                                    # «закрой окно/попап/соусы к
                                                    # бортикам» — закрытие (целевое
                                                    # или крестик), ДО generic-клика
                                                    cc_close = parse_close_request(user_input)
                                                    if cc_close:
                                                        cc_parsed = (cc_close[0], cc_close[1], "click")
                                                    else:
                                                        cc_click = parse_click_request(user_input)
                                                        if cc_click:
                                                            cc_parsed = (cc_click[0], cc_click[1], "click")
                if cc_parsed:
                    resolver = {"download": self.computer_control.resolve_download,
                                "type": self.computer_control.resolve_type,
                                "scroll": self.computer_control.resolve_scroll,
                                "cart": self.computer_control.resolve_cart,
                                "send": self.computer_control.resolve_send,
                                "key": self.computer_control.resolve_key,
                                "slider": self.computer_control.resolve_slider}.get(
                        cc_parsed[2], self.computer_control.resolve_click)
                    try:
                        cc_action, cc_err = resolver(cc_parsed[0], cc_parsed[1],
                                                     self.router,
                                                     chat_id=str(chat_id or ""))
                    except Exception as e:
                        logger.debug(f"[CompControl] fast-path клик/скачивание не удалось: {e}")
                        cc_err = "Не удалось выполнить действие на странице."
                    if cc_action is None and cc_err:
                        self.memory.add_message("user", user_input, user_id, chat_id, user_name)
                        self.memory.add_message("assistant", cc_err, user_id, chat_id)
                        if self.proactive and chat_id:
                            self.proactive.record_user_response(chat_id)
                        return cc_err
            # «перейди на вкладку X» / «какие вкладки открыты» — переключение
            # и список вкладок. Ничего не меняют на страницах — выполняются
            # сразу, без подтверждения (как чтение). Явная форма со словом
            # «вкладку» при промахе — честный отказ со списком открытых;
            # мягкая («перейди на X») при промахе молча уходит дальше по
            # fast-path (может, это «открой X» или вообще не наша команда)
            cc_tab_direct = False
            if cc_action is None and self.computer_control.click:
                if parse_tab_list_query(user_input):
                    try:
                        cc_tabs_text = \
                            self.computer_control.list_open_tabs_text()
                    except Exception as e:
                        logger.debug(f"[CompControl] список вкладок не "
                                     f"удался: {e}")
                        cc_tabs_text = "Не удалось получить список вкладок."
                    self.memory.add_message("user", user_input, user_id,
                                            chat_id, user_name)
                    self.memory.add_message("assistant", cc_tabs_text,
                                            user_id, chat_id)
                    if self.proactive and chat_id:
                        self.proactive.record_user_response(chat_id)
                    return cc_tabs_text
                cc_tab = parse_tab_switch(user_input)
                if cc_tab:
                    try:
                        cc_action, cc_err = \
                            self.computer_control.resolve_tab_switch(
                                cc_tab[0], cc_tab[1],
                                chat_id=str(chat_id or ""))
                        # Переключение — сразу; фолбэк на открытие сайта
                        # (url-действие) идёт обычным путём с подтверждением
                        cc_tab_direct = bool(cc_action) and \
                            cc_action.get("kind") == "tab_switch"
                    except Exception as e:
                        logger.debug(f"[CompControl] fast-path переключение "
                                     f"вкладки не удалось: {e}")
                        cc_err = "Не удалось переключить вкладку."
                    if cc_action is None and cc_err:
                        self.memory.add_message("user", user_input, user_id,
                                                chat_id, user_name)
                        self.memory.add_message("assistant", cc_err, user_id,
                                                chat_id)
                        if self.proactive and chat_id:
                            self.proactive.record_user_response(chat_id)
                        return cc_err
            # «прочитай последнее сообщение (на кладе)» / «что ответил клод» —
            # чтение со страницы: без подтверждения (ничего не меняет),
            # прочитанный текст — сразу ответом
            cc_read_kind = None
            if cc_action is None and self.computer_control.click:
                cc_read = parse_read_request(user_input)
                if cc_read:
                    try:
                        cc_action, cc_err = self.computer_control.resolve_read(
                            *cc_read, chat_id=str(chat_id or ""))
                        cc_read_kind = "read" if cc_action else None
                    except Exception as e:
                        logger.debug(f"[CompControl] fast-path чтение не удалось: {e}")
                        cc_err = "Не удалось прочитать страницу."
                    if cc_action is None and cc_err:
                        self.memory.add_message("user", user_input, user_id, chat_id, user_name)
                        self.memory.add_message("assistant", cc_err, user_id, chat_id)
                        if self.proactive and chat_id:
                            self.proactive.record_user_response(chat_id)
                        return cc_err
            if cc_action is None:
                cc_names = parse_open_many(user_input)
                if cc_names:
                    try:
                        cc_action = self.computer_control.resolve_many(cc_names)
                    except Exception as e:
                        logger.debug(f"[CompControl] fast-path резолв не удался: {e}")
            if cc_action:
                logger.info(f"[CompControl] fast-path: '{user_input[:40]}' → "
                            f"{self.computer_control.describe(cc_action)}")
                self.memory.add_message("user", user_input, user_id, chat_id, user_name)
                if cc_read_kind == "read":
                    cc_ok, cc_detail = self.computer_control.execute(
                        cc_action, chat_id, router=self.router)
                    cc_reply = (cc_detail if cc_ok else
                                f"Не удалось прочитать: {cc_detail}.")
                elif cc_tab_direct:
                    # Переключение вкладки — как чтение: без подтверждения
                    cc_ok, cc_detail = self.computer_control.execute(
                        cc_action, chat_id, router=self.router)
                    cc_reply = (
                        f"Готово, {self.computer_control.describe_done(cc_action)}."
                        if cc_ok else
                        f"Не удалось {self.computer_control.describe(cc_action)}: {cc_detail}.")
                elif self.computer_control.confirm:
                    self.computer_control.set_pending(chat_id, cc_action)
                    cc_reply = self.computer_control.confirm_question(cc_action)
                else:
                    cc_ok, cc_detail = self.computer_control.execute(
                        cc_action, chat_id, router=self.router)
                    cc_reply = (
                        f"Готово, {self.computer_control.describe_done(cc_action)}."
                        if cc_ok else
                        f"Не удалось {self.computer_control.describe(cc_action)}: {cc_detail}."
                    )
                self.memory.add_message("assistant", cc_reply, user_id, chat_id)
                if self.proactive and chat_id:
                    self.proactive.record_user_response(chat_id)
                return cc_reply
            # «что находится в X?» / «что в разделе X?» — вопрос о содержимом
            # секции открытой страницы: текст секции читаем со страницы и
            # подаём в общий LLM-поток контекстом — список формулирует модель.
            # Не fast-path ответ: return тут нет. Секция не нашлась / страницу
            # не открывали — молча обычный диалог (вопрос мог быть не о странице)
            if self.computer_control.click:
                cc_pq = parse_page_question(user_input)
                if cc_pq:
                    try:
                        _pq = self.computer_control.read_page_section(*cc_pq)
                    except Exception as e:
                        logger.debug(f"[CompControl] чтение секции страницы не удалось: {e}")
                        _pq = None
                    if _pq:
                        _pq_text, _pq_host, _pq_q = _pq
                        logger.info(f"[CompControl] секция «{_pq_q}» прочитана "
                                    f"({len(_pq_text)} симв., {_pq_host})")
                        page_section_note = (
                            f"The user asked about the \"{_pq_q}\" section of the web page "
                            f"currently open in the browser ({_pq_host}). Here is the section's "
                            f"actual content, read live from the page just now:\n---\n"
                            f"{_pq_text}\n---\n"
                            "Answer STRICTLY from this content: list the items (with prices, "
                            "if shown). Do not invent items that are not listed there — if the "
                            "user expects something that is missing, say the section does not "
                            "show it right now.")
            # не резолвится — обычный путь через LLM

        # Берём историю до добавления нового сообщения — для контекста rewriter'а
        history_for_rewrite = self.memory.stm.get_last(_REWRITE_HISTORY, chat_id=chat_id)
        logger.info(f"[BotInstance] history_for_rewrite: {len(history_for_rewrite)} messages")

        # Серия подряд идущих ответов бота с финальным вопросом (conversation_style):
        # по истории ДО текущего сообщения; ниже решает, нужна ли регенерация
        from app.features.conversation_style import count_question_streak
        question_streak = count_question_streak(history_for_rewrite)

        # Живой контекст персоны (state/world): считаем ДО добавления сообщения
        # в STM — по последней реплике истории ещё видна пауза отсутствия
        # пользователя, и приветствие-дневник (§7) собирается честно
        living_context = None
        if self.living is not None and chat_id:
            living_context = self._build_living_context(
                chat_id, history_for_rewrite, user_message=user_input)

        # Стилевой модификатор помощи по intellect tier (§4 плана уровней):
        # Gemma-детекция help-запроса идёт ФОНОМ, параллельно с rewrite/
        # памятью/поиском — результат собирается ниже перед prepare_messages
        help_style_future = None
        if self.intellect.active:
            try:
                from app.features.help_style import submit_block_for_message
                help_style_future = submit_block_for_message(
                    user_input, self.intellect, self._local_router)
            except Exception as e:
                logger.debug(f"[HelpStyle] фоновая детекция не запущена: {e}")

        # Переписываем запрос: разрешаем местоимения и анафору
        persona_context = self._get_persona_context_for_search()
        ru_rewritten = rewrite_query(
            user_input, history_for_rewrite, self._local_router, persona_context=persona_context
        )
        logger.info(f"[BotInstance] rewrite_query: '{user_input[:60]}' -> '{ru_rewritten[:60]}'")

        # Лёгкий режим: отвечать будет локальная модель (Ollama) ИЛИ флаг
        # features.light_context принудительно включён в YAML персоны — слабая
        # модель тонет в большом промпте, поэтому урезаем всё необязательное
        # (короткая история, минимум фактов, без RAG/веба/self-memory/файлов).
        light_mode = self.router.is_local_primary() or self.features.get("light_context") is True
        if light_mode:
            logger.info(
                "[BotInstance] light-режим контекста "
                f"(local-провайдер: {self.router.is_local_primary()}, "
                f"features.light_context: {self.features.get('light_context') is True})"
            )

        # 1. Запускаем веб-поиск в фоне (параллельно с памятью)
        # QueryEnhancer преобразует ru_rewritten в короткий поисковый запрос через LLM
        # Передаём историю и контекст персоны для корректного понимания вопроса
        web_future = None
        # При прочитанной секции страницы веб-поиск не нужен: ответ целиком
        # в живом тексте секции, выдача DDG только сместит фокус ответа
        if (self._web_search_enabled and chat_id not in self._web_search_disabled_chats
                and not self._is_docs_only_request(user_input)
                and page_section_note is None):
            # Собираем контекст персоны для QueryEnhancer
            persona_context = self._get_persona_context_for_search()
            # Берём последние 6 сообщений для контекста
            history_for_search = self.memory.stm.get_last(6, chat_id=chat_id)
            web_future = self._web_pool.submit(
                self._search_web, ru_rewritten, 5, True, None, history_for_search, persona_context
            )

        try:
            # В STM сохраняем переписанную русскую версию (с разрешёнными местоимениями)
            self.memory.add_message("user", ru_rewritten, user_id, chat_id, user_name,
                                    light_mode=light_mode)
            if light_mode:
                stm_messages, ltm_facts, stm_relevant = self.memory.get_context(
                    user_id, chat_id, ltm_query=ru_rewritten,
                    stm_recent_n=6, ltm_limit=3, stm_relevant_limit=0,
                )
            else:
                stm_messages, ltm_facts, stm_relevant = self.memory.get_context(
                    user_id, chat_id, ltm_query=ru_rewritten
                )
            file_context = None
            if self.file_db and not light_mode:
                if self._is_full_doc_request(user_input):
                    full_text = self.file_db.get_full_document(user_id)
                    if full_text:
                        file_context = f"Full text of the uploaded document:\n{full_text}"
                else:
                    file_chunks = self.file_db.search(user_id=user_id, query=user_input, limit=5)
                    if file_chunks:
                        file_context = "Context from uploaded files:\n" + "\n---\n".join(file_chunks)
            web_context = None
            if web_future is not None:
                try:
                    # search_web (LLM-enhance + DDG + загрузка страниц) регулярно
                    # занимает больше 10с — при меньшем таймауте результат терялся
                    results = web_future.result(timeout=25)
                    if results:
                        web_context = self._format_web_results(results)
                        # В light-режиме веб-выдача (полные тексты страниц) без ограничения
                        # по размеру — режем жёстко, иначе слабая модель теряет нить.
                        if light_mode and web_context and len(web_context) > 1500:
                            web_context = web_context[:1500] + "\n[...truncated]"
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
            if chat_id and str(chat_id) != str(user_id) and not light_mode:
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
                    "Rules from the user (always follow them, they override habits):\n"
                    + "\n".join(f"  - {r}" for r in user_rules[-10:])
                )

            # Портрет из досье (интересы + стиль) — одна короткая строка, чтобы
            # автоанализ диалога работал и в обычных ответах, а не только когда
            # бот пишет первым
            if not light_mode and chat_id:
                try:
                    dossier_line = self._get_dossier_context_line(chat_id, user_id)
                    if dossier_line:
                        context_parts_out.append(dossier_line)
                except Exception as _de:
                    logger.debug(f"[Dossier] Строка контекста недоступна: {_de}")
            # Секция открытой страницы («что находится в X?») — реальный текст,
            # прочитанный со страницы в fast-path; ответ строится только из него
            if page_section_note:
                context_parts_out.append(page_section_note)
            memory_text = "\n\n".join(context_parts_out) if context_parts_out else None
            has_files = file_context is not None

            # Окружение пользователя (город, его локальное время, погода) — одна
            # строка, кешируется; добавляется и в light-режиме (она крошечная)
            env_context = None
            try:
                from app.features import env_context as _env_ctx
                env_context = _env_ctx.get_env_line()
            except Exception as _ee:
                logger.debug(f"[Env] Строка окружения недоступна: {_ee}")

            # Получаем блок личной памяти бота
            self_memory_block = None
            if self.self_memory and not light_mode:
                self_memory_block = self.self_memory.get_context_block()

            # Формируем блок релевантного STM-контекста
            stm_relevant_text = None
            if stm_relevant:
                parts = []
                for msg in stm_relevant:
                    role_ru = msg.get("user_name", "User") if msg["role"] == "user" else "Assistant"
                    ts = _format_msg_ts(msg.get("timestamp"))
                    ts_tag = f" [{ts}]" if ts else ""
                    parts.append(f"  {role_ru}{ts_tag}: {msg['content'][:200]}")
                stm_relevant_text = "\n".join(parts)

            # Reminder: перехватываем перед todo (напомни через N ...)
            # Любой текст со словом "напом" — это путь напоминаний, не todo.
            reminder_context = None
            is_reminder_request = False

            # Pending /remind без времени: пользователь отвечал на «через сколько?»
            if self.reminder_manager and chat_id \
                    and not self.control_mode_on(chat_id):
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

                # Ответ на «какое именно напоминание перенести?» (несколько активных,
                # подсказки не было — сдвиг уже запомнен в pending)
                if not _yield_to_learning and self.reminder_manager.get_pending_postpone_choice(chat_id):
                    is_reminder_request = True
                    result = self.reminder_manager.resolve_postpone_choice(chat_id, user_input)
                    if result and result.get("gone"):
                        reminder_context = (
                            "The user was choosing which reminder to move, but there are no "
                            "active reminders anymore. Say there is nothing to move — "
                            "in your own style, briefly."
                        )
                    elif result:
                        reminder_context = _postpone_result_context(result)
                    else:
                        reminder_context = (
                            "The user is choosing which reminder to move, but the answer does "
                            "not match any of them. Ask again: reply with the number or words "
                            "from the task. In your own style, briefly. "
                            "Do NOT say anything was moved."
                        )

                # Ответ на «на когда перенести напоминание?» (перенос без времени).
                # Без этой ветки ответ улетал в общий LLM, и модель могла
                # «подтвердить» перенос, который нигде не применялся.
                elif not _yield_to_learning and self.reminder_manager.get_pending_postpone(chat_id):
                    is_reminder_request = True
                    shift = parse_postpone(f"перенеси напоминание {user_input}")
                    p_delay = p_abs = None
                    p_rel = False
                    if shift and not shift.get("unknown"):
                        p_delay = shift.get("seconds")
                        p_abs = shift.get("abs")
                        p_rel = bool(shift.get("relative_to_trigger"))
                    if p_delay is None and p_abs is None:
                        # Ответ вида «через 10 минут» / «в 18:00» / голое «18:30»
                        parsed_shift = parse_reminder("напомни " + user_input)
                        if not parsed_shift:
                            parsed_shift = parse_reminder("напомни в " + user_input)
                        if parsed_shift:
                            _, p_delay = parsed_shift
                        else:
                            p_delay = parse_frequency(user_input)
                    if p_delay is not None or p_abs is not None:
                        self.reminder_manager.clear_pending_remind(chat_id)
                        # Подсказка задачи — только если пользователь переформулировал
                        # весь запрос («перенеси напоминание приготовить еду на час»),
                        # а не просто ответил на вопрос («на 15 минут» — там подсказки нет,
                        # а extract вытащил бы мусор вроде «через час»).
                        p_hint = (
                            extract_postpone_hint(user_input)
                            if parse_postpone(user_input) else None
                        )
                        reminder_context = self._postpone_handled_context(
                            chat_id,
                            self.reminder_manager.postpone_reminder(
                                chat_id, seconds=p_delay, abs_time=p_abs,
                                relative_to_trigger=p_rel,
                                task_hint=p_hint,
                            ),
                            seconds=p_delay, abs_time=p_abs, relative_to_trigger=p_rel,
                        )
                    else:
                        reminder_context = (
                            "The user is answering the question about when to move the "
                            "reminder to, but the time could not be understood. Ask again: "
                            "to what time should the reminder be moved "
                            "(for example, \"in 10 minutes\", \"at 18:30\")? "
                            "In your own style, briefly. "
                            "Do NOT say the reminder was moved — it was NOT."
                        )

                elif pending_task and not _yield_to_learning:
                    # Пытаемся вытащить время из ответа пользователя.
                    # Сначала — повторяющееся расписание («каждый день в 12»).
                    rec_pending = parse_recurring("напомни " + user_input)
                    rem_delay = None
                    if rec_pending:
                        rec_task, rec_schedule = rec_pending
                        self.reminder_manager.clear_pending_remind(chat_id)
                        rem_task = self._reformulate_task(rec_task or pending_task)
                        topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                        self.reminder_manager.add_reminder(
                            chat_id, user_name or "User", rem_task, 0, topic_id,
                            schedule=rec_schedule,
                            user_id=user_id, username=get_username(user_id),
                        )
                        task_display = f" '{rem_task}'" if rem_task else ""
                        reminder_context = (
                            f"The user asked to be reminded{task_display} — "
                            f"{format_schedule(rec_schedule)}. The reminder is scheduled — "
                            f"confirm this in your own style, briefly."
                        )
                        is_reminder_request = True
                        parsed_pending = None
                    else:
                        parsed_pending = parse_reminder("напомни " + user_input)
                    if not is_reminder_request and parsed_pending:
                        _, rem_delay = parsed_pending
                    if rem_delay is None and not is_reminder_request:
                        # Пробуем парсер частоты из learning
                        rem_delay = parse_frequency(user_input)
                    if rem_delay:
                        self.reminder_manager.clear_pending_remind(chat_id)
                        rem_task = self._reformulate_task(pending_task)
                        topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                        delay_text = self.reminder_manager.format_delay(rem_delay)
                        self.reminder_manager.add_reminder(
                            chat_id, user_name or "User", rem_task, rem_delay, topic_id,
                            user_id=user_id, username=get_username(user_id),
                        )
                        task_display = f" '{rem_task}'" if rem_task else ""
                        reminder_context = (
                            f"The user specified the time for the reminder{task_display} — in {delay_text}. "
                            f"The reminder is scheduled — confirm this in your own style, briefly."
                        )
                        is_reminder_request = True
                    else:
                        # Время снова не поняли — переспрашиваем ещё раз
                        # (только если повторяющееся напоминание не создано выше)
                        if not is_reminder_request:
                            reminder_context = (
                                f"The user is answering the question about the time for the reminder \"{pending_task}\", "
                                "but the time could not be understood. Ask again: how soon to remind "
                                "(for example, \"in 2 hours\", \"tomorrow at 12\", \"every day at 9\"). "
                                "In your own style, briefly. "
                                "Do NOT confirm that any reminder was scheduled — it was NOT."
                            )
                            is_reminder_request = True

                # ВАЖНО: раньше это был `elif` на том же уровне, что и `if self.reminder_manager
                # and chat_id:` выше — а условие elif было строгим подмножеством условия if,
                # так что при отсутствии pending_task (обычный случай) сюда вообще никогда не
                # попадали, и свежие текстовые запросы «напомни мне X через Y» никогда не
                # парсились. Теперь это правильно вложено: проверяем «напом»/«remind» только
                # когда НЕТ pending-задачи.
                elif "напом" in user_input.lower() or re.search(r"\bremind", user_input.lower()):
                    is_reminder_request = True
                    # Перенос существующего напоминания («перенеси/отложи/сдвинь
                    # напоминание ...») — строго ДО обычного парсера: иначе весь текст
                    # уезжал в pending-задачу нового напоминания, реального переноса не
                    # происходило, а бот словами его «подтверждал».
                    postpone = parse_postpone(user_input)
                    if postpone and postpone.get("unknown"):
                        self.reminder_manager.begin_pending_postpone(chat_id)
                        reminder_context = (
                            "The user asked to move/reschedule a reminder, but did not say "
                            "to when. Ask: to what time should the reminder be moved "
                            "(for example, \"in 10 minutes\", \"at 18:30\")? "
                            "In your own style, briefly. Do NOT say anything was rescheduled yet."
                        )
                    elif postpone:
                        reminder_context = self._postpone_handled_context(
                            chat_id,
                            self.reminder_manager.postpone_reminder(
                                chat_id,
                                seconds=postpone.get("seconds"),
                                abs_time=postpone.get("abs"),
                                relative_to_trigger=postpone.get("relative_to_trigger", False),
                                task_hint=extract_postpone_hint(user_input),
                            ),
                            seconds=postpone.get("seconds"),
                            abs_time=postpone.get("abs"),
                            relative_to_trigger=postpone.get("relative_to_trigger", False),
                        )
                    # Сначала — повторяющееся расписание («каждый день в 9», «по пятницам в 18:00»)
                    rec = parse_recurring(user_input) if not postpone else None
                    if rec:
                        rem_task, rec_schedule = rec
                        if rem_task:
                            rem_task = self._reformulate_task(rem_task)
                        topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                        self.reminder_manager.add_reminder(
                            chat_id, user_name or "User", rem_task, 0, topic_id,
                            schedule=rec_schedule,
                            user_id=user_id, username=get_username(user_id),
                        )
                        task_display = f" '{rem_task}'" if rem_task else ""
                        reminder_context = (
                            f"The user asked to be reminded{task_display} — {format_schedule(rec_schedule)}. "
                            f"The reminder is already scheduled — just confirm this in your own style, briefly."
                        )
                    elif not postpone:
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
                                chat_id, user_name or "User", rem_task, rem_delay, topic_id,
                                user_id=user_id, username=get_username(user_id),
                            )
                            task_display = f" '{rem_task}'" if rem_task else ""
                            reminder_context = (
                                f"The user asked to be reminded{task_display} in {delay_text}. "
                                f"The reminder is already scheduled — just confirm this in your own style, briefly."
                            )
                        else:
                            # Время не указано — переспрашиваем и ЗАПОМИНАЕМ задачу (как в /remind),
                            # иначе следующее сообщение "через 10 минут" не с чем будет связать.
                            rem_task = self._reformulate_task(user_input)
                            self.reminder_manager.begin_pending_remind(chat_id, rem_task)
                            reminder_context = (
                                f"The user asked to be reminded \"{rem_task}\", but did not specify how soon. "
                                "Ask when to remind them — in your own style, briefly."
                            )

            # Learning-контекст: режим обучения («научи меня X»)
            learning_context = None
            is_learning_request = False
            # В режиме управления обучение молчит (как и остальные фичи-слова)
            if self.learning_manager and chat_id \
                    and not self.control_mode_on(chat_id):
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
                    or is_todo_list_request(user_input)
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
                        # общий self.persona.prepare_messages(...). Язык триггерного
                        # сообщения передаём явно: в изолированном вызове реплик
                        # пользователя нет, иначе модель ответит на языке персоны.
                        skip_llm_answer = self.learning_manager.render_setup_reply(
                            subject, "confirmed", delay_text,
                            user_language=detect_language(user_input))
                    else:
                        skip_llm_answer = self.learning_manager.render_setup_reply(
                            subject, "reask", user_language=detect_language(user_input))
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
                            # общий self.persona.prepare_messages(...). Язык передаём
                            # явно — реплик пользователя в изолированном вызове нет.
                            skip_llm_answer = self.learning_manager.render_continue_reply(
                                chat_id, decision, session_id=_session_id,
                                user_language=detect_language(user_input))
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
                            self.learning_manager.begin_setup(chat_id, subject, user_id or "default", user_name or "User")
                            learning_context = (
                                f"The user wants you to teach them \"{subject}\". "
                                "Ask them briefly and in your own style how often to send lessons "
                                "(for example: once a day, every 2 hours). The course starts after their reply."
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
                                    f"on the topic {subjects_str}" if len(subjects) == 1
                                    else f"on several topics at once: {subjects_str}"
                                )
                                learning_context = (
                                    f"A learning course is running in this chat {courses_note}. "
                                    "The user is writing within the learning conversation — possibly answering "
                                    "the previous lesson's review questions (on one of the topics) or discussing the topic. "
                                    "React ONLY to the user's message, in your own style. "
                                    "STRICT RULES:\n"
                                    "— DO NOT generate a new lesson, new topic, review questions or a quiz "
                                    "on any of the topics.\n"
                                    "— DO NOT mix study material into your reply — the next lesson "
                                    "will arrive later on schedule as a separate file message.\n"
                                    "— React briefly: answer/explain/comment, nothing more."
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
            if self.todo_manager and chat_id \
                    and not self.control_mode_on(chat_id):
                if is_todo_done_request(user_input):
                    _fired_intents.add("todo_remove")
                elif is_todo_list_request(user_input):
                    _fired_intents.add("todo_show")
                elif is_todo_request(user_input):
                    _fired_intents.add("todo_add")
            if (self.inventory_manager and not is_reminder_request
                    and not self.control_mode_on(chat_id)):
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
                    todo_context = current_todo or "The todo list is empty."
                elif "todo_show" in _fired_intents:
                    # Просьба показать список: только контекст со списком, без
                    # extracted_task — добавление не срабатывает, LLM показывает список
                    todo_context = self.todo_manager.get_list(chat_id) or "The todo list is empty."
                elif "todo_add" in _fired_intents:
                    extracted_task = extract_task(user_input)
                    if extracted_task:
                        extracted_task = self._reformulate_task(extracted_task)
                    current_todo = self.todo_manager.get_list(chat_id)
                    todo_context = current_todo or "The todo list is empty."

            # Inventory-контекст: вещи бота
            # (пропускаем если это напоминание — чтобы LLM не добавил мусор в инвентарь)
            inventory_context = None
            extracted_inventory_item = None
            extracted_inventory_remove = None
            inventory_events = []  # События для LLM-реакции (использование, просрочка)
            if (not is_reminder_request and self.inventory_manager
                    and not (chat_id and self.control_mode_on(chat_id))):
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
                        inventory_events.append(f"The item '{used_item}' was used and is no longer in the inventory.")
                    else:
                        # Пробуем найти похожий предмет (по части названия)
                        found = self._find_inventory_item_by_substring(used_item)
                        if found:
                            result = self.inventory_manager.use_item(found)
                            inventory_events.append(f"The item '{found}' was used and is no longer in the inventory.")
                        else:
                            inventory_events.append(f"The user mentions using '{used_item}', but there is no such item in the inventory.")

                # Проверяем просроченные предметы
                expired = self.inventory_manager.remove_expired_items()
                for exp_name in expired:
                    inventory_events.append(f"The item '{exp_name}' has spoiled/expired and disappeared from the inventory.")

                # Обновляем контекст инвентаря после всех изменений
                inv_block = self.inventory_manager.get_context_block()
                if inv_block:
                    inventory_context = inv_block

            # Ранний возврат: готовый ответ, минующий LLM (фидбек теста, не генерируем новый контент)
            if skip_llm_answer:
                answer = self._clean_response(skip_llm_answer)
                answer = self._save_assistant_reply(answer, user_id, chat_id)
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
            if self.book_search and not light_mode:
                try:
                    from app.features.intent_router import classify_intent
                    _book_intent = classify_intent(user_input, stm_messages)
                    logger.info(f"[IntentRouter] intent={_book_intent} for: '{user_input[:60]}'")
                except Exception as ie:
                    logger.debug(f"Intent classification error: {ie}")

            # Поиск по книге (RAG) — пропускаем при chat_only
            book_context = None
            context_mode = "book"
            _book_frag_count = None  # для валидации маркеров [ФN] в ответе
            if self.book_search and _book_intent != "chat_only" and not light_mode:
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
                        # ВАЖНО: здесь НЕ делать `import json, re` — инлайновый импорт
                        # делал re локальным для ВСЕГО process_message, и любой
                        # re.* выше по функции падал с UnboundLocalError
                        # (json и re уже импортированы на уровне модуля)
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
                        _book_frag_count = len(fragments) if fragments else 0
                        translated_query = self.book_search.translate_query(user_input)

                        # Динамический глоссарий: только записи, релевантные
                        # вопросу (раньше весь глоссарий ~17k токенов шёл
                        # в системный промпт каждого сообщения).
                        from app.features.glossary_context import build_glossary_block
                        _glos = build_glossary_block(
                            [user_input, translated_query or ""],
                            fragments=fragments,
                        )

                        if fragments:
                            from app.features.book_context import build_context_block
                            book_context = build_context_block(
                                fragments,
                                original_query=user_input,
                                translated_query=translated_query,
                                mode=context_mode
                            )
                            if _glos:
                                book_context = _glos + "\n\n" + book_context
                            logger.info(f"[BookContext] {len(fragments)} fragments, {len(book_context)} chars for query: '{user_input[:60]}'")
                        else:
                            from app.features.book_context import build_context_block
                            book_context = build_context_block(
                                [],
                                original_query=user_input,
                                translated_query=translated_query,
                                mode=context_mode
                            )
                            if _glos:
                                book_context = _glos + "\n\n" + book_context
                            logger.info(f"[BookContext] No fragments for query: '{user_input[:60]}'")
                except Exception as e:
                    logger.debug(f"Book search error: {e}")

            # Стилевой модификатор помощи по intellect tier (§4 плана
            # уровней): детекция стартовала фоном в начале process_message —
            # здесь только забираем результат (обычно уже готов)
            help_style_block = None
            if help_style_future is not None:
                try:
                    help_style_block = help_style_future.result(timeout=10)
                except Exception as e:
                    logger.debug(f"[HelpStyle] модификатор не собран: {e}")

            # Платформенное правило финальных вопросов (conversation_style):
            # нота последней в системном блоке. Не подмешивается в учебные
            # сообщения — там вопросы пользователю часть механики курса
            from app.features.conversation_style import build_style_note
            conv_style_note = None
            if not learning_context:
                conv_style_note = build_style_note(self.conversation_style.frequency)

            # Computer control: инструкция о маркерах — только в режиме
            # управления (иначе LLM изображает «Открыл», ничего не открыв)
            cc_prompt = None
            if self.computer_control and chat_id \
                    and self.control_mode_on(chat_id):
                cc_prompt = self.computer_control.instruction_block()

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
                book_context=book_context,
                env_context=env_context,
                living_context=living_context,
                help_style_context=help_style_block,
                conversation_style_context=conv_style_note,
                computer_control_context=cc_prompt
            )
            settings = self.persona.get_settings()
            # Когда есть учебный контекст (анонс/пересказ урока, фидбек) — ответ выходит
            # длиннее обычной реплики, для которой рассчитан persona.get_settings(). Берём
            # более щедрый max_tokens, чтобы не упираться в лимит и не дёргать догенерацию.
            if learning_context:
                settings = dict(settings)
                settings["max_tokens"] = max(int(settings.get("max_tokens", 2000)), 3000)
            if light_mode:
                # Локальная модель: длинная генерация — медленно и чаще «уезжает»,
                # ограничиваем размер ответа. Догенерация в light-режиме отключена
                # (модель дублирует реплику вместо продолжения): короткие обрывы
                # ловит гвард мусорных ответов ниже, а с num_ctx 8192 потолок
                # 1200 токенов не давит на контекст.
                settings = dict(settings)
                settings["max_tokens"] = min(int(settings.get("max_tokens", 2000)), 1200)
            # Стриминг (on_token задан): токены уходят подписчику сырыми,
            # финальный ответ после _clean_response/маркеров возвращается как обычно.
            # Веб/API сюда не передаёт on_token — /api/chat/stream сам «печатает»
            # финальный reply порциями, чтобы клиент не показывал сырой стрим.
            if on_token is not None:
                answer = self.router.get_response_stream(messages, on_token, **settings)
            else:
                answer = self.router.get_response(messages, **settings)
            if not answer:
                logger.error("Все LLM-провайдеры недоступны, ответ не сгенерирован")
                return "Сейчас все LLM-провайдеры недоступны. Попробуй позже."

            # Защита от обрыва по max_tokens (persona.get_settings() рассчитан на обычную
            # реплику; когда learning_context просит анонсировать/пересказать урок, ответ
            # выходит длиннее и может упереться в лимит) — просим модель дописать.
            # В light-режиме догенерация отключена: слабая локальная модель на повторном
            # заходе плодит варианты реплики вместо продолжения.
            # Для webchat — тоже отключена: «continue» засоряет непрерывный чат
            # служебными репликами (их видно в ленте), а веб-модель вместо строгого
            # продолжения выдаёт новую вариацию ответа — склейка даёт дубли.
            _webchat_answered = str(getattr(self.router, "_last_provider", "") or "").startswith("webchat")
            _continuations = 0
            while not (light_mode or _webchat_answered) and _looks_truncated(answer) and _continuations < 2:
                follow_up_messages = messages + [
                    {"role": "assistant", "content": answer},
                    {"role": "user", "content": "You stopped mid-sentence. Continue strictly from where you left off — do not repeat what was already written and do not start over. Continue in the same language as the reply."},
                ]
                cont = self.router.get_response(follow_up_messages, **settings)
                if not cont:
                    break
                answer = answer + cont
                _continuations += 1

            # Очистка ответа от мета-рассуждений и Markdown
            answer = self._clean_response(answer)

            # Webchat-модель нередко обрезается лимитом длины прямо посреди
            # маркера источника — «…убил его. [Ф1». Висящий хвост ломает баланс
            # скобок, и garbage-гард ниже выбрасывает целиком валидный ответ
            # (догенерация для webchat отключена). Чиним обрыв до проверки.
            answer = self._repair_truncated_markers(answer)

            # Страховка от оборванной/мусорной генерации (маленькие локальные
            # модели иногда выдают обрывки маркеров — «[», «[16.» — или пустоту):
            # один повторный заход, иначе нейтральная заглушка вместо мусора.
            def _is_garbage(t: str) -> bool:
                t = (t or "").rstrip()
                return (not re.search(r"[0-9A-Za-zА-Яа-яЁё]", t)
                        or t.count("[") != t.count("]")  # оборванный маркер
                        # короткий обрыв посреди слова («Анализ запроса. Тре») —
                        # нет знака завершения фразы. Только light-режим (там
                        # нет догенерации); в обычном короткая реплика без
                        # точки в конце — разговорный стиль, а не мусор
                        or (light_mode and len(t) < 60
                            and not _SENTENCE_END_RE.search(t)))
            if _is_garbage(answer):
                logger.warning(f"[BotInstance] Мусорный ответ ({answer!r}) — регенерация")
                retry = self.router.get_response(messages, **settings)
                if retry:
                    answer = self._repair_truncated_markers(self._clean_response(retry))
                else:
                    answer = ""
                if _is_garbage(answer):
                    answer = "Не удалось сформулировать ответ — попробуй переформулировать."

            # Предохранитель conversation_style: ответ закончился вопросом сверх
            # лимита серии — одна регенерация с усиленным напоминанием (модель
            # сама оставит вопрос, если он нужен по смыслу). Регенерированный
            # текст ниже проходит ту же обработку маркеров, что и обычный.
            # Учебные сообщения пропускаем — там вопросы часть механики курса.
            if not learning_context:
                from app.features.conversation_style import (
                    should_regenerate, regenerate_without_tail_question)
                if should_regenerate(self.conversation_style, answer, question_streak):
                    logger.info(
                        f"[ConvStyle] Финальный вопрос сверх лимита "
                        f"(streak={question_streak}, mode={self.conversation_style.frequency}) — регенерация")
                    new_answer = regenerate_without_tail_question(
                        self.router, messages, answer, settings)
                    if new_answer:
                        answer = self._clean_response(new_answer) or answer

            # Срезка маркеров источников [ФN] (книжный режим): строки со
            # ссылкой на несуществующий фрагмент удаляются как выдуманные.
            if _book_frag_count is not None:
                answer = self._strip_fact_markers(answer, _book_frag_count)
            elif "Ф" in answer:
                # Поиска не было (chat_only/light) — валидных фрагментов нет,
                # но веб-чат видит старые RAG-инструкции в истории чата и
                # копирует стиль: маркеры-мимикрию срезаем, строки не трогаем
                # (frag_count=0 — любой маркер «вне диапазона»).
                answer = self._strip_fact_markers(answer, 0)

            # Обработка todo-маркера
            # (пропускаем для учебно-административных сообщений — setup/continue/тест/
            # новый курс, там is_learning_request=True; обычный разговор при активном
            # курсе флаг не выставляет, и todo во время курса работает как обычно)
            if self.todo_manager and chat_id and todo_context and not is_learning_request:
                answer = self._process_todo_marker(
                    answer, chat_id, user_name or "User",
                    fallback_task=extracted_task,
                    fallback_done_index=extracted_done_index,
                    user_text=user_input,
                )

            # Обработка inventory-маркеров (добавление/удаление/использование через маркеры)
            # (пропускаем если это напоминание или учебно-административное сообщение —
            # там LLM не должен добавлять в инвентарь; обычный разговор при активном
            # курсе сюда проходит — инвентарь во время курса работает как обычно)
            if not is_reminder_request and not is_learning_request and self.inventory_manager:
                answer = self._process_inventory_markers(answer, extracted_inventory_item, extracted_inventory_remove, user_name or "user", user_text=user_input, chat_id=chat_id)

            # Маркеры управления компьютером (open_url/open_app/run_task): срезка +
            # pending на подтверждение (или немедленное исполнение при confirm: false).
            # Те же пропуски, что у инвентаря — административные ответы маркеров не несут;
            # и только в режиме управления — иначе LLM-маркер не исполняем
            if (self.computer_control and chat_id
                    and self.control_mode_on(chat_id)
                    and not is_reminder_request and not is_learning_request):
                answer, cc_notices = self.computer_control.process_markers(answer, chat_id)
                for _cc_note in cc_notices:
                    self._pending_lists(chat_id).append(_cc_note)
            if self._punish_enabled:
                answer = self._parse_punishment(answer, user_id)

            # 9.5 Автопредложение записать сценарий: закрывающая реплика
            # («спасибо»/«готово») после цепочки действий → один раз
            # предлагаем «запомни сценарий …». Только в режиме управления.
            if (self.scenario_manager and chat_id
                    and self.control_mode_on(chat_id)):
                try:
                    _sc_offer = self.scenario_manager.maybe_offer(chat_id, user_input)
                    if _sc_offer:
                        answer = f"{answer}\n\n{_sc_offer}"
                except Exception as e:
                    logger.debug(f"[Scenarios] maybe_offer не удался: {e}")

            # 10. Сохраняем ответ (при split_messages — по частям, хвост в pending)
            answer = self._save_assistant_reply(answer, user_id, chat_id)

            # 11. Эпизодическая память (self_memory)
            if self.self_memory:
                self.self_memory.tick(stm_messages, user_id, user_input)

            # 11b. Мир персоны: детекция новых NPC/мест из диалога (в фоне)
            if self.living is not None and chat_id:
                try:
                    self.living.on_user_message(str(chat_id), stm_messages)
                except Exception as e:
                    logger.debug(f"[Living] Диалоговый тик не удался: {e}")

            # 12. Обратная связь proactive: если ждем ответа на инициативу -- фиксируем успех
            # (record_user_response сам обновляет досье — отдельный вызов
            # record_incoming_message здесь гнал счётчик анализа вдвое быстрее)
            if self.proactive and chat_id:
                self.proactive.record_user_response(chat_id)

            return answer
        finally:
            # Ничего не делаем — пул живёт всё время жизни бота
            pass

    def chat_user_language(self, chat_id: str) -> Optional[str]:
        """Язык пользователя чата по последним репликам STM ('ru'/'en'/None).
        Для служебных текстов вне LLM-пайплайна (список дел и т.п.)."""
        try:
            return detect_dialogue_language(
                "", self.memory.stm.get_last(8, chat_id=chat_id))
        except Exception:
            return None

    def _build_living_context(self, chat_id: str, history: List[Dict],
                              user_message: str = "") -> Optional[str]:
        """Собирает living-контекст для prepare_messages: приветствие-дневник
        при долгой паузе (§7) + текущее состояние персоны. Пауза считается по
        последней реплике истории ДО добавления текущего сообщения в STM.
        user_message — текущая реплика: последний факт жизни включается,
        только когда у него есть топическая зацепка (реактивная подача)."""
        if self.living is None:
            return None
        parts = []
        try:
            last_ts = None
            for msg in reversed(history or []):
                ts = msg.get("timestamp")
                if isinstance(ts, (int, float)) and ts > 0:
                    last_ts = float(ts)
                    break
            if last_ts:
                absence_h = (time.time() - last_ts) / 3600
                if absence_h >= 12:
                    entries = self.living.state_engine.entries_since(
                        chat_id, time.time() - absence_h * 3600)
                    return_ctx = self.living.summarizer.build_return_context(
                        chat_id, entries, absence_h,
                        user_language=detect_dialogue_language("", history))
                    if return_ctx:
                        parts.append(return_ctx)
                        self.living.state_engine.mark_consumed(
                            [e["id"] for e in entries])
            state_ctx = self.living.get_living_context(
                chat_id, topic_text=user_message)
            if state_ctx:
                parts.append(state_ctx)
        except Exception as e:
            logger.debug(f"[Living] Контекст не собран: {e}")
        return "\n\n".join(p for p in parts if p) or None

    def _postpone_handled_context(self, chat_id: str, result: Optional[dict],
                                  seconds: Optional[float] = None,
                                  abs_time: Optional[tuple] = None,
                                  relative_to_trigger: bool = False) -> str:
        """LLM-контекст после попытки переноса напоминания.
        ambiguous — запоминаем сдвиг и спрашиваем КАКОЕ напоминание двигать
        (показываем нумерованный список); not_found — такого нет, ничего не
        двинуто; остальное — стандартное подтверждение/отказ."""
        if result and result.get("ambiguous"):
            self.reminder_manager.begin_pending_postpone_choice(
                chat_id, seconds=seconds, abs_time=abs_time,
                relative_to_trigger=relative_to_trigger,
            )
            choices = _fmt_reminder_choices(result["choices"])
            return (
                "No reminder was moved. NOTHING was rescheduled. "
                f"There are several active reminders: {choices}. "
                "Your reply MUST be a question asking which one to move, "
                "showing the numbered list above. "
                "In your own style, briefly."
            )
        if result and result.get("not_found"):
            choices = _fmt_reminder_choices(result.get("choices") or [])
            listing = f" Active reminders: {choices}." if choices else ""
            return (
                "No reminder was moved. NOTHING was rescheduled. "
                f"The user asked to move a reminder matching \"{result.get('hint')}\", "
                f"but no reminder matches it.{listing} "
                "Say that no such reminder was found (mention what does exist, if anything). "
                "In your own style, briefly."
            )
        return _postpone_result_context(result)

    def _reformulate_task(self, raw_task: str) -> str:
        """
        Очищает сырой текст задачи через local LLM.
        'что пора написать Коннор' -> 'Написать Коннор'
        'мне сделать апдейт' -> 'Сделать апдейт'
        """
        if not raw_task or len(raw_task.strip()) < 2:
            return raw_task

        if not self._local_router or not self._local_router.is_available(task="todo_cleanup"):
            return raw_task.strip()

        try:
            response = self._local_router.get_response(
                messages=[
                    {"role": "system", "content": (
                        "Clean up the task text: remove pronouns and clutter. "
                        "The answer is only the short task text, phrased as an infinitive. "
                        "STRICTLY keep the language of the original text: "
                        "do NOT translate — Russian stays Russian, English stays English."
                    )},
                    {"role": "user", "content": raw_task.strip()},
                ],
                temperature=0.0,
                max_tokens=60,
                task="todo_cleanup",
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
                    "clean up", "pronoun", "clutter", "infinitive",
                    "only the short", "task text", "short task",
                    "rephrase", "meta-note", "markdown",
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
        if not self._local_router or not self._local_router.is_available(task="rule_extract"):
            return None
        try:
            resp = self._local_router.get_response(
                messages=[
                    {"role": "system", "content": (
                        "Determine whether the message is a correction of the bot or a request to remember "
                        "a rule/preference (how to address the user, what to do or not do). "
                        "If yes — formulate the rule as ONE short sentence (up to 12 words), "
                        "without explanations or quotes. If it is ordinary conversation or a question — answer exactly NO."
                    )},
                    {"role": "user", "content": user_text[:400]},
                ],
                temperature=0.0, max_tokens=60,
                task="rule_extract",
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
        if not self._local_router or not self._local_router.is_available(task="intent_router"):
            return None

        intent_desc = {
            "todo_add": "TODO_ADD — write down a new task in the todo list",
            "todo_remove": "TODO_REMOVE — remove/cross out a task from the todo list",
            "todo_show": "TODO_SHOW — show/read the current todo list",
            "inventory_add": "INVENTORY_ADD — give/hand an item to the bot for its inventory",
            "inventory_remove": "INVENTORY_REMOVE — take away/discard an item from the bot's inventory",
        }
        valid_outputs = [c.upper() for c in candidates]
        options_text = "\n".join(f"- {intent_desc[c]}" for c in candidates)

        verdict = self._local_router.classify(
            system_prompt=(
                "You are an intent classifier. Determine what the user is asking to do.\n"
                f"Options:\n{options_text}\n"
                "- CHAT — ordinary conversation, none of the above.\n"
                "Pay attention to the object of the action: \"todo list\" is TODO, \"inventory/you have/to you\" is INVENTORY. "
                "Answer with one word."
            ),
            user_prompt=f"User message: \"{user_text}\"",
            valid_outputs=valid_outputs + ["CHAT"],
            temperature=0.0,
            max_tokens=10,
            task="intent_router",
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
        if not self._local_router or not self._local_router.is_available(task="intent_router"):
            logger.info(f"[Intent] Локальная LLM недоступна, переспрос для '{candidate[:40]}'")
            return "ASK"

        intent_desc = {
            "inventory_add": "add an item to the character's inventory",
            "inventory_remove": "discard/remove an item from the inventory",
            "todo_add": "write down a task in the todo list",
            "todo_remove": "mark a task as done and remove it from the todo list",
        }.get(intent, "perform an action")

        system_prompt = (
            "Ты — классификатор намерений. Определи, ЯВНО ли пользователь просит "
            f"{intent_desc}, или это просто реплика/рассказ.\n"
            "ПРАВИЛА:\n"
            "- Пользователь вручает предмет персонажу («держи X», «возьми X», «вот тебе X», "
            "«надень X», «дарю X») — это ADD.\n"
            "- Рассказ о прошлом («я купил X», «мне подарили X», «получил X») — это SKIP.\n"
            "- Кандидат уже извлечён из сообщения автоматически — оцени, осознанно ли "
            "пользователь это просит.\n"
            "Ответь ОДНИМ словом: ADD или SKIP."
        )
        user_prompt = (
            f"Сообщение пользователя: \"{user_text}\"\n"
            f"Извлечённый кандидат: \"{candidate}\"\n"
            f"Это явная просьба: {intent_desc}? Ответь ADD или SKIP."
        )

        try:
            verdict = self._local_router.classify(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                valid_outputs=["ADD", "SKIP"],
                temperature=0.0,
                max_tokens=5,
                task="intent_router",
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
        # Слабые модели копируют метку времени [DD.MM HH:MM] из промпта в ответ
        response = re.sub(
            r"^\s*\[\d{2}\.\d{2}(?:\.\d{4})?\s+\d{1,2}:\d{2}\]\s*", "", response)
        return response.strip()

    @staticmethod
    def _repair_truncated_markers(text: str) -> str:
        """Срезает маркер [ФN], оборванный лимитом длины на самом хвосте ответа.

        Обрыв приходится на конец текста («…убил его. [Ф1», «…[Ф22, Ф2»),
        догенерация для webchat отключена — без починки баланс скобок
        нарушен и garbage-гард выбрасывает валидный ответ целиком.
        Валидные закрытые маркеры не трогает: у них есть «]» после номера.
        """
        if not text or "[" not in text:
            return text
        return re.sub(
            r"\[\s*Ф\s*[0-9]*(?:\s*[,;\-–—]\s*Ф?\s*[0-9]*)*\s*$",
            "", text.rstrip()).rstrip()

    def _strip_fact_markers(self, response: str, frag_count: int) -> str:
        """Срезает скрытые маркеры источников [ФN] из книжного ответа.

        Модель помечает каждую фактическую фразу номером фрагмента-источника
        (см. build_context_block). Правила:
          - валидный номер (1..frag_count) — маркер просто срезаем;
          - близкий промах (frag_count+1..frag_count+3) — модель сбилась в
            счёте фрагментов (в контексте их до 25): срезаем только маркер,
            строку сохраняем;
          - дикий номер (n < 1 или n > frag_count+3) — удаляем ВСЮ строку:
            ссылка на несуществующий источник — сигнал выдумки (модель
            «подтверждает» деталь фрагментом, которого нет);
          - когда фрагментов нет (пустой поиск) — любой маркер невалиден,
            но ответ и так строится на честном неведении, поэтому только
            срезаем маркеры, не удаляя строки;
          - коллапс-гард: если удаление диких строк опустошило ответ (не
            осталось ни одной содержательной строки — обычно выживает только
            «ритуальный» вопрос, который инструкция освобождает от маркеров),
            значит модель ошиблась в нумерации всего ответа — возвращаем все
            строки, срезав лишь сами маркеры.
        Возвращает очищенный текст; статистика — в лог.
        """
        if not response or "Ф" not in response:
            return response

        # Одиночные, составные и диапазонные маркеры:
        # [Ф2], [Ф22, Ф23], [Ф22,Ф23], [Ф5–6], [Ф10-11], [Ф1–3, Ф5]
        marker_re = re.compile(
            r"\[\s*Ф\s*[0-9]+(?:\s*[,;\-–—]\s*Ф?\s*[0-9]+)*\s*\]")
        num_re = re.compile(r"[0-9]+")
        tolerance = 3

        def _clean_line(line: str) -> str:
            cleaned = marker_re.sub("", line)
            cleaned = re.sub(r"\s+([.,!?…:;])", r"\1", cleaned)
            return re.sub(r" {2,}", " ", cleaned).rstrip()

        valid = near_miss = invalid = 0
        out_lines = []
        strip_only_lines = []
        for line in response.splitlines():
            nums = [int(n) for m in marker_re.findall(line)
                    for n in num_re.findall(m)]
            cleaned_line = _clean_line(line)
            strip_only_lines.append(cleaned_line)
            if nums and any(n < 1 or n > frag_count for n in nums):
                if frag_count > 0 and any(n < 1 or n > frag_count + tolerance
                                          for n in nums):
                    invalid += 1
                    logger.info(f"[FactMarkers] Удалена строка с выдуманным источником: {line[:100]}")
                    continue
                near_miss += 1
            elif nums:
                valid += 1
            out_lines.append(cleaned_line)

        def _collapse_ws(text: str) -> str:
            return re.sub(r"\n{3,}", "\n\n", text).strip()

        cleaned = _collapse_ws("\n".join(out_lines))
        if invalid:
            def _substantive(text: str) -> bool:
                return len(re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", text)) >= 60
            if not _substantive(cleaned):
                logger.warning(
                    f"[FactMarkers] Удаление {invalid} строк опустошило ответ "
                    f"(frag_count={frag_count}) — откат: маркеры срезаны, строки сохранены")
                cleaned = _collapse_ws("\n".join(strip_only_lines))
        if valid or near_miss or invalid:
            logger.info(f"[FactMarkers] валидных: {valid}, промахов счёта: "
                        f"{near_miss}, выдуманных: {invalid}")
        return cleaned

    @staticmethod
    def _strip_inline_lists(text: str) -> str:
        """Удаляет секции 'Список дел:' и 'Инвентарь:' из ответа LLM.
        Эти списки отправляются отдельным сообщением через _pending_list_messages."""
        # Вырезаем секцию "Список дел:"/"Todo list:" и все её пункты до пустой строки или конца текста
        text = re.sub(r'\n*(?:Список дел|Todo list):\n.*?(?=\n\s*\n|\Z)', '', text, flags=re.DOTALL)
        # Вырезаем секцию "Инвентарь:"/"Inventory:" и все её пункты до пустой строки или конца текста
        text = re.sub(r'\n*(?:Инвентарь|Inventory):\n.*?(?=\n\s*\n|\Z)', '', text, flags=re.DOTALL)
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

        # Вставки в двойных звёздочках — художественный приём («внутренние
        # процессы», см. connor.yaml), читатель должен их видеть: рендер
        # Telegram сам показывает **x** жирным. Мета-паттерны ниже писались
        # под одиночные *...* — внутри пары ** они матчатся посередине и
        # съедают текст, оставляя висящий **.
        bold_spans = []
        def _save_bold(m):
            bold_spans.append(m.group(0))
            return f'\x00MB{len(bold_spans) - 1}\x00'
        text = re.sub(r'\*\*.+?\*\*', _save_bold, text, flags=re.DOTALL)

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

        # Восстанавливаем bold-вставки и code-блоки
        # (bold раньше code: спан мог сохранить внутри себя плейсхолдер блока)
        for i, span in enumerate(bold_spans):
            text = text.replace(f'\x00MB{i}\x00', span)
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

        # Пустые маркеры: **\n\n** или *** без контента убрать.
        # (?<!\S) — не трогаем ** между двумя жирными фрагментами
        # («**воду** и **зонт**»): там это закрывающая и открывающая пары,
        # а не пустой маркер — иначе фрагменты склеятся в «**водузонт**»
        text = re.sub(r'(?<!\S)\*{2,}\s*\*{2,}', '', text)
        # Заголовки: #### Заголовок → Заголовок
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
        # Изображения: ![alt](url) → alt
        text = re.sub(r'!\[(.+?)\]\(.+?\)', r'\1', text)
        # Горизонтальная линия: --- → юникод-разделитель
        text = re.sub(r'^-{3,}\s*$', '───────────', text, flags=re.MULTILINE)
        # Горизонтальная линия: *** или ___ → пустая строка; заодно и
        # осиротевшая линия ** — остаток оборванного/испорченного маркера
        text = re.sub(r'^[*_]{2,}\s*$', '', text, flags=re.MULTILINE)
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
            result = self.todo_manager.remove_item(chat_id, index, lang=detect_language(user_text))
            if result:
                self._pending_lists(chat_id).append(result)
            return response.strip()

        # Fallback удаление через эвристику — подтверждаем через LLM, иначе
        # «готово, прочитал 3 главы» молча удаляло пункт №3
        if fallback_done_index is not None:
            verdict = self._confirm_intent(user_text, f"item #{fallback_done_index}", "todo_remove")
            if verdict == "ADD":
                result = self.todo_manager.remove_item(chat_id, fallback_done_index, lang=detect_language(user_text))
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
            todo_list = self.todo_manager.add_item(chat_id, user_name, task, lang=detect_language(user_text))
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
            parts.append(f"Persona name: {name}")

        description = data.get("description", "")
        if description:
            parts.append(f"Description: {description}")

        # Из system_prompt берём только первые 500 символов — основная роль и внешность
        system_prompt = data.get("system_prompt", "")
        if system_prompt:
            # Берём начало до первого крупного раздела
            prompt_preview = system_prompt[:500].strip()
            if prompt_preview:
                parts.append(f"Role and character: {prompt_preview}")
        
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

    def get_dossier_snapshot(self, chat_id: str, user_id: str = None) -> dict:
        """Профиль досье чата (интересы/темы/наблюдения) для веб-UI.
        user_id — только записи этого участника (персональный контекст)."""
        if self._chat_dossier is None:
            from app.features.chat_dossier import ChatDossier
            self._chat_dossier = ChatDossier(context=self.context, router=self.router)
        return self._chat_dossier.get_profile_snapshot(chat_id, user_id=user_id)

    def _get_dossier_context_line(self, chat_id: str, user_id: str) -> Optional[str]:
        """Короткая строка портрета из досье (интересы + пара наблюдений) в
        основной ответ — чтобы бот опирался на неё не только в инициативах.
        Темы сознательно не берём: они ситуативные, в постоянном контексте — шум."""
        snap = self.get_dossier_snapshot(chat_id, user_id=user_id)
        parts = []
        interests = snap["interests"][-8:]
        if interests:
            parts.append("interests: " + ", ".join(interests))
        # Наблюдения не атрибутированы по пользователям — в группе это
        # смешанный портрет, поэтому даём их только в личном чате
        if str(chat_id) == str(user_id):
            notes = snap["personality_notes"][-2:]
            if notes:
                parts.append("style: " + "; ".join(notes))
        if not parts:
            return None
        return "Known about the user (chat analysis): " + "; ".join(parts)

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

    def update_fact(self, old_query: str, new_text: str, user_id: str) -> Optional[str]:
        """Замена факта новым текстом (правка из веб-UI). Возвращает старый текст или None."""
        return self.memory.ltm.update_fact(old_query, new_text, user_id)

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
            living=self.living,
            intellect=self.intellect,
        )
        # Создаем досье на чат (общий экземпляр с rhythm — один файл на бота)
        from app.features.chat_dossier import ChatDossier
        if self._chat_dossier is None:
            self._chat_dossier = ChatDossier(context=self.context, router=self.router)
        self.proactive.dossier = self._chat_dossier
        logger.info(f"  [{self.persona_name}] Proactive messaging инициализирован с sender и досье")

        # Живая персона: сигналы инициативы + источники чатов (§3.2)
        if self.living is not None:
            self.living.on_initiative_signal = self.proactive.state_initiative_signal
            self.living.get_known_chats = self._activity_tracker.get_known_chats
            self.living.get_last_message_time = self._get_last_message_time
            self.living.get_last_initiative_time = (
                lambda chat_id: self.proactive._last_initiative_time.get(str(chat_id), 0))
            # дешёвые гейты перед LLM-скорингом инициативы (§3.4)
            self.living.pre_initiative_gate = self.proactive.initiative_cheaply_possible
            logger.info(f"  [{self.persona_name}] Living persona связана с proactive")

    def setup_learning(self, sender: MessageSender):
        """Передаёт sender, роутеры и memory в learning_manager. Вызывается после инициализации Telegram Bot."""
        if not self.learning_manager:
            return
        self.learning_manager.set_sender(sender)
        self.learning_manager.set_routers_persona(self.router, self.persona, self._local_router)
        self.learning_manager.set_memory(self.memory)
        logger.info(f"  [{self.persona_name}] Learning manager инициализирован с sender, роутерами и memory")

    def setup_rhythm(self, sender: MessageSender):
        """Создает RhythmManager с готовым sender (утренние приветствия /
        ночные «пора спать» / погодные предупреждения). Вызывается после
        инициализации Telegram Bot / веб-inbox; no-op при выключенной фиче."""
        rhythm_config = self.features.get("rhythm", {})
        if isinstance(rhythm_config, bool):
            rhythm_config = {"enabled": rhythm_config}
        if not rhythm_config.get("enabled", False):
            return
        from app.features.rhythm_manager import RhythmConfig, RhythmManager
        if self._activity_tracker is None:
            from app.features.proactive_messaging import ChatActivityTracker
            self._activity_tracker = ChatActivityTracker(context=self.context)
        # Досье общее с proactive (один экземпляр на бота); включённому без
        # proactive нужен свой — отметки событий rhythm в досье чата
        if self._chat_dossier is None:
            from app.features.chat_dossier import ChatDossier
            self._chat_dossier = ChatDossier(context=self.context, router=self.router)
        self.rhythm = RhythmManager(
            context=self.context,
            config=RhythmConfig.from_dict(rhythm_config),
            router=self.router,
            persona=self.persona,
            memory=self.memory,
            activity_tracker=self._activity_tracker,
            sender=sender,
            muted_check=lambda: bool((self.features or {}).get("muted")),
            dossier=self._chat_dossier,
        )
        logger.info(f"  [{self.persona_name}] Rhythm инициализирован с sender")

    # ── слэш-команды: создание сущности + ответ через LLM в образе персоны ──

    def describe_image(self, image_bytes: bytes, question: str = "") -> Optional[str]:
        """OCR + описание изображения через vision-провайдер основного роутера.
        Возвращает None, если ни один vision-провайдер не настроен/не ответил."""
        if not self.router.supports_vision():
            return None
        prompt = (
            "The user sent an image. Extract all visible text from it (OCR) "
            "and briefly describe what is shown (1-2 sentences).\n"
            "Response format:\nTEXT: <text from the image or \"no text\">\nDESCRIPTION: <...>"
        )
        if question:
            prompt += f"\nAdditionally answer the user's question about the image: {question}"
        return self.router.get_response_with_image(prompt, image_bytes)

    def _enrich_inventory_item(self, name: str, desc: str = "", expires: Optional[str] = None) -> tuple:
        """Дополняет описание и срок годности предмета через ЛОКАЛЬНУЮ модель
        (основную не трогаем). Срок придумывается только для портящихся предметов.
        Возвращает (desc, expires) — незаполненные поля остаются как были."""
        if not name:
            return desc, expires
        if not self._local_router or not self._local_router.is_available(task="inventory_enrich"):
            logger.info(f"[Inventory] Локальная модель недоступна — «{name}» без описания/срока")
            return desc, expires
        try:
            from datetime import date
            today = date.today().isoformat()
            messages = [
                {"role": "system", "content": (
                    f"Today is {today}. For the item, come up with:\n"
                    "1) DESCRIPTION — brief (5-15 words), without the name and without quotes.\n"
                    "2) EXPIRES — an expiration date YYYY-MM-DD, ONLY if the item can spoil "
                    "(food, drinks, flowers, etc.); for non-perishable items write \"-\".\n"
                    "The answer is strictly two lines:\nDESCRIPTION: ...\nEXPIRES: ..."
                )},
                {"role": "user", "content": name.strip()},
            ]
            resp = self._local_router.get_response(messages, temperature=0.3, max_tokens=80, top_p=0.9, task="inventory_enrich")
            if resp:
                for line in resp.strip().splitlines():
                    line = line.strip()
                    low = line.lower()
                    if not desc and low.startswith(("description", "описание")):
                        candidate = line.partition(":")[2].strip().strip('"\'""«»').strip()
                        if 3 <= len(candidate) <= 120:
                            desc = candidate
                    elif not expires and low.startswith(("expires", "срок")):
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
                role_ru = msg.get("user_name", "User") if msg["role"] == "user" else "Assistant"
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
        # Для webchat догенерация отключена (см. process_message): «continue» в
        # непрерывном чате даёт дубли реплики и мусор в ленте.
        _webchat_answered = str(getattr(self.router, "_last_provider", "") or "").startswith("webchat")
        _continuations = 0
        while not _webchat_answered and _looks_truncated(answer) and _continuations < 2:
            follow_up_messages = messages + [
                {"role": "assistant", "content": answer},
                {"role": "user", "content": "You stopped mid-sentence. Continue strictly from where you left off — do not repeat what was already written and do not start over."},
            ]
            cont = self.router.get_response(follow_up_messages, **settings)
            if not cont:
                break
            answer = answer + cont
            _continuations += 1

        answer = self._clean_response(answer)

        answer = self._save_assistant_reply(answer, user_id, chat_id)
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
            # Повторяющееся расписание («каждый день в 9», «по пятницам в 18»)
            rec = parse_recurring("напомни " + args)
            if rec:
                rem_task, rec_schedule = rec
                if rem_task:
                    rem_task = self._reformulate_task(rem_task)
                topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                self.reminder_manager.add_reminder(
                    chat_id, user_name or "User", rem_task, 0, topic_id,
                    schedule=rec_schedule,
                    user_id=user_id, username=get_username(user_id),
                )
                task_display = f" «{rem_task}»" if rem_task else ""
                return f"Хорошо, буду напоминать{task_display} — {format_schedule(rec_schedule)}."
            parsed = parse_reminder("напомни " + args)
            if parsed:
                rem_task, rem_delay = parsed
                if rem_task:
                    rem_task = self._reformulate_task(rem_task)
                topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                delay_text = self.reminder_manager.format_delay(rem_delay)
                self.reminder_manager.add_reminder(chat_id, user_name or "User", rem_task, rem_delay, topic_id,
                                                   user_id=user_id, username=get_username(user_id))
                task_display = f" «{rem_task}»" if rem_task else ""
                note = (
                    f"User {who} used a command to ask to be reminded{task_display} in {delay_text}. "
                    "The reminder is already scheduled — confirm this in your own style, briefly. "
                    f"Address {user_name} specifically, not other chat participants."
                )
            else:
                # Время не указано — переспрашиваем, запоминаем задачу
                rem_task = self._reformulate_task(args)
                self.reminder_manager.begin_pending_remind(chat_id, rem_task)
                note = (
                    f"User {who} used a command to ask to be reminded \"{rem_task}\", but did not specify how soon. "
                    "Ask when to remind them (for example, \"in 2 hours\", \"tomorrow at 12\") — in your own style, briefly. "
                    f"Address {user_name} specifically, not other chat participants."
                )
            return self.command_reply(note, "reminder", chat_id, user_id, user_name, user_input_cmd)

        if kind == "todo":
            if not self.todo_manager:
                return "Список дел не активен для этой персоны."
            if not args:
                return "Использование: /todo <задача>"
            task = self._reformulate_task(args)
            self.todo_manager.add_item(chat_id, user_name or "User", task)
            note = (
                f"User {who} used a command to add the task \"{task}\" to the todo list. "
                "Confirm this in your own style, briefly."
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
            result = self.inventory_manager.add_item(name, description=desc, source=user_name or "user", expires=expires)
            note = (
                f"User {who} used a command to put the item \"{name}\" into your inventory"
                + (f" (description: {desc})" if desc else "")
                + (f" (expires: {expires})" if expires else "")
                + f". Result: {result} "
                "Confirm this in your own style, briefly."
            )
            return self.command_reply(note, "inventory", chat_id, user_id, user_name, user_input_cmd)

        if kind == "learn":
            if not self.learning_manager:
                return "Режим обучения не активен для этой персоны."
            if not args:
                return "Использование: /learn <тема>"
            subject = args
            self.learning_manager.begin_setup(chat_id, subject, user_id or "default", user_name or "User")
            note = (
                f"User {who} used a command to ask you to teach them \"{subject}\". "
                "Ask briefly and in your own style how often to send lessons "
                "(for example: once a day, every 2 hours). The course starts after their reply."
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

    def note_presence(self, chat_id: str):
        """Сигнал «пользователь появился» (сообщение в TG / поллинг веб-инбокса) —
        триггер утреннего приветствия фичи rhythm. Дёшев, не блокирует."""
        if self.rhythm is not None:
            try:
                self.rhythm.note_presence(chat_id)
            except Exception as e:
                logger.debug(f"[{self.persona_name}] note_presence: {e}")

    def record_topic(self, chat_id: str, topic_id: int):
        """Записывает ID топика для чата."""
        if self._activity_tracker:
            self._activity_tracker.record_topic(chat_id, topic_id)

    def get_chat_topic(self, chat_id: str) -> Optional[int]:
        """Возвращает ID топика для чата."""
        if self._activity_tracker:
            return self._activity_tracker.get_topic(chat_id)
        return None

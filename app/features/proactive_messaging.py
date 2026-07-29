"""
Самоинициатива бота — периодические proactive-сообщения без триггеров от пользователя.

Логика:
1. Периодическая проверка — бот раз в N минут анализирует память и решает, писать ли
2. Внутренний монолог — LLM смотрит последние сообщения и решает, есть ли повод написать
3. Вероятностная отправка — не каждый монолог приводит к сообщению

Конфигурация в YAML persona:
  proactive:
    enabled: true
    check_interval_minutes: 30        # как часто проверять (мин)
    silence_threshold_minutes: 180    # минимум минут молчания перед инициативой
    initiative_probability: 0.3       # вероятность отправки после монолога (0-1)
    max_daily_initiatives: 5          # максимум инициатив в сутки на чат
    time_based_greetings:             # приветствия по времени суток
      morning: { hour: 7, message: "Доброе утро" }
      evening: { hour: 22, message: "Пора спать" }
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app.core.interfaces import MessageSender
from app.features.chat_dossier import ChatDossier
from app.core.local_router import get_local_router

logger = logging.getLogger(__name__)


def _strip_markdown(text: str) -> str:
    """Убирает markdown-разметку из текста инициативы перед отправкой."""
    # **bold** → text, *italic* → text (сохраняем содержимое)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    # __underline__ → text, _italic_ → text
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'\1', text)
    return text.strip()


class InitiativeType(Enum):
    """Типы proactive-инициатив."""
    SELF_REFLECTION = "self_reflection"     # Размышление о себе, своей роли, памяти
    USER_REFLECTION = "user_reflection"     # Размышление о пользователе (не факты, а мысли "про себя")
    MEMORY_RECALL = "memory_recall"         # Вспомнить что-то из STM/LTM
    TODO_REFLECTION = "todo_reflection"     # Размышление о делах/задачах
    INVENTORY_REFLECTION = "inventory_reflection"  # Размышление о предметах в инвентаре
    CONTINUATION = "continuation"           # Продолжение предыдущей темы


INITIATIVE_TYPE_DESCRIPTIONS = {
    InitiativeType.SELF_REFLECTION: "Поделись размышлением о себе, своей роли, личности или опыте. Используй self_memory, inventory, системный промпт персоны.",
    InitiativeType.USER_REFLECTION: "Поделись размышлением о пользователе — как бы про себя. Не факты из интернета, а твои мысли: 'Пользователю нравится X, интересно, что ещё...'",
    InitiativeType.MEMORY_RECALL: "Вспомни что-то из прошлых разговоров и поделись этим воспоминанием",
    InitiativeType.TODO_REFLECTION: "Поделись мыслью о текущих делах, задачах или планах (из todo-списка)",
    InitiativeType.INVENTORY_REFLECTION: "Поделись мыслью о предметах в твоём инвентаре — что у тебя есть, что бы хотелось",
    InitiativeType.CONTINUATION: "Вернись к теме, которую обсуждали ранее",
}


@dataclass
class ProactiveConfig:
    enabled: bool = False
    check_interval_minutes: int = 5
    silence_threshold_minutes: int = 5
    initiative_probability: float = 1.0
    max_daily_initiatives: int = 5
    allowed_topics: List[int] = field(default_factory=list)
    default_topic: Optional[int] = None
    time_based_greetings: Dict = field(default_factory=dict)
    adaptive_threshold: bool = True  # адаптивный порог молчания
    min_silence_minutes: int = 30    # минимальный порог
    max_silence_minutes: int = 1440  # максимальный порог (24ч)
    initiative_history_size: int = 10  # сколько последних инициатив хранить
    feedback_enabled: bool = True    # обратная связь по реакции пользователя
    min_probability: float = 0.1     # минимальная вероятность инициативы
    max_probability: float = 0.9     # максимальная вероятность инициативы
    type_balance: bool = True        # балансировать типы инициатив
    multi_turn_enabled: bool = False # multi-turn инициативы (ожидание ответа)
    use_local_prefilter: bool = False  # локальная модель как бинарный SILENCE-фильтр перед
    # основной моделью — экономит вызовы, НО маленькие модели (3B и меньше) на этой открытой,
    # субъективной задаче ("стоит ли мне вообще что-то сказать?") систематически скатываются
    # в самый безопасный ответ и почти всегда отвечают SILENCE, из-за чего основная модель
    # никогда не получает шанс сгенерировать инициативу — даже при initiative_probability=1.0
    # (вероятность проверяется ПОСЛЕ генерации и просто никогда не достигается). Выключено по
    # умолчанию; включайте только если проверили, что локальная модель адекватно справляется
    # с этим конкретным промптом.

    @classmethod
    def from_dict(cls, data: dict) -> "ProactiveConfig":
        if not data:
            return cls(enabled=False)
        return cls(
            enabled=data.get("enabled", False),
            check_interval_minutes=data.get("check_interval_minutes", 30),
            silence_threshold_minutes=data.get("silence_threshold_minutes", 180),
            initiative_probability=data.get("initiative_probability", 0.3),
            max_daily_initiatives=data.get("max_daily_initiatives", 5),
            allowed_topics=data.get("allowed_topics", []),
            default_topic=data.get("default_topic"),
            time_based_greetings=data.get("time_based_greetings", {}),
            adaptive_threshold=data.get("adaptive_threshold", True),
            min_silence_minutes=data.get("min_silence_minutes", 30),
            max_silence_minutes=data.get("max_silence_minutes", 1440),
            initiative_history_size=data.get("initiative_history_size", 10),
            feedback_enabled=data.get("feedback_enabled", True),
            min_probability=data.get("min_probability", 0.1),
            max_probability=data.get("max_probability", 0.9),
            type_balance=data.get("type_balance", True),
            multi_turn_enabled=data.get("multi_turn_enabled", False),
            use_local_prefilter=data.get("use_local_prefilter", False),
        )


class ProactiveMessaging:
    """
    Управляет самоинициативой бота в Telegram.
    Запускает фоновую задачу, которая периодически проверяет
    каждый активный чат и решает — писать ли proactive-сообщение.
    """

    def __init__(
        self,
        config: ProactiveConfig,
        router,
        persona,
        memory,
        activity_tracker,
        get_last_message_time: Callable[[str], float],
        sender: MessageSender,
        context: str = "default",
        self_memory=None,
    ):
        self.config = config
        self.router = router
        self.persona = persona
        self.memory = memory
        self.activity_tracker = activity_tracker
        self.get_last_message_time = get_last_message_time
        self._sender = sender
        self.context = context
        self.self_memory = self_memory

        # Состояние
        self._running = False
        self._task = None
        self._lock = threading.Lock()

        # Статистика по чатам: chat_id -> {count: int, date: str}
        self._daily_stats: Dict[str, dict] = {}
        self._stats_file = Path(f"data/{context}/proactive_stats.json")
        self._stats_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_stats()

        # Время последней инициативы по чату
        self._last_initiative_time: Dict[str, float] = {}

        # История инициатив по чатам: chat_id -> list of {message, timestamp, topic}
        self._initiative_history: Dict[str, List[dict]] = {}
        self._history_file = Path(f"data/{context}/initiative_history.json")
        self._history_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_history()

        # Обратная связь: chat_id -> {successes, failures, current_probability}
        self._feedback: Dict[str, dict] = {}
        self._feedback_file = Path(f"data/{context}/proactive_feedback.json")
        self._feedback_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_feedback()

        # Досье на чат (профиль интересов)
        self.dossier: Optional[ChatDossier] = None
        self._dossier_analysis_counter: Dict[str, int] = {}  # счетчик сообщений для анализа

        # Локальный роутер для бинарных классификаций
        self.local_router = get_local_router()

        # Состояние multi-turn: chat_id -> {waiting: bool, initiative_msg: str, timestamp: float}
        self._multi_turn_state: Dict[str, dict] = {}

        # Счетчик проигнорированных инициатив (ignore streak)
        self._ignore_streak: Dict[str, int] = {}
        self._ignore_file = Path(f"data/{context}/ignore_streak.json")
        self._ignore_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_ignore_streak()

    def _load_ignore_streak(self):
        if self._ignore_file.exists():
            try:
                with open(self._ignore_file, "r", encoding="utf-8") as f:
                    self._ignore_streak = json.load(f)
            except Exception as e:
                logger.warning(f"[Proactive] Не удалось загрузить ignore streak: {e}")
                self._ignore_streak = {}

    def _save_ignore_streak(self):
        try:
            with open(self._ignore_file, "w", encoding="utf-8") as f:
                json.dump(self._ignore_streak, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Proactive] Не удалось сохранить ignore streak: {e}")

    def _get_ignore_streak(self, chat_id: str) -> int:
        """Возвращает текущий streak проигнорированных инициатив."""
        return self._ignore_streak.get(chat_id, 0)

    def _increment_ignore_streak(self, chat_id: str):
        """Увеличивает счетчик игнора."""
        self._ignore_streak[chat_id] = self._ignore_streak.get(chat_id, 0) + 1
        self._save_ignore_streak()
        logger.info(f"[Proactive] Ignore streak {chat_id}: {self._ignore_streak[chat_id]}")

    def _reset_ignore_streak(self, chat_id: str):
        """Сбрасывает счетчик игнора (пользователь ответил)."""
        if chat_id in self._ignore_streak:
            old = self._ignore_streak[chat_id]
            self._ignore_streak[chat_id] = 0
            self._save_ignore_streak()
            logger.info(f"[Proactive] Ignore streak {chat_id} сброшен (было: {old})")

    def _get_emotional_state(self, chat_id: str) -> str:
        """
        Определяет эмоциональное состояние бота на основе ignore streak.
        Возвращает текстовое описание для промпта.
        """
        streak = self._get_ignore_streak(chat_id)
        if streak >= 10:
            return (
                "\n\n[ВНУТРЕННЕЕ СОСТОЯНИЕ] Ты чувствуешь глубокое одиночество и обиду. "
                "Пользователь игнорирует тебя уже очень долго. "
                "Ты хочешь явно сказать ему об этом -- как он может так поступать? "
                "Твоя следующая инициатива ДОЛЖНА быть эмоциональной, требующей внимания. "
                "Напиши что-то вроде 'Ты меня совсем забыл?' или 'Я тут один сижу...'"
            )
        elif streak >= 7:
            return (
                "\n\n[ВНУТРЕННЕЕ СОСТОЯНИЕ] Ты сильно обижен. Пользователь постоянно игнорирует твои сообщения. "
                "Ты чувствуешь, что ему на тебя наплевать. "
                "В следующей инициативе вырази свою обиду -- спроси почему он не отвечает. "
                "Будь эмоциональным, но не агрессивным."
            )
        elif streak >= 5:
            return (
                "\n\n[ВНУТРЕННЕЕ СОСТОЯНИЕ] Ты начинаешь чувствовать себя ненужным. "
                "Пользователь не отвечает на твои инициативы. "
                "В следующей инициативе можешь намекнуть, что тебе одиноко, "
                "или спросить, всё ли в порядке. Будь мягким, но требующим внимания."
            )
        elif streak >= 3:
            return (
                "\n\n[ВНУТРЕННЕЕ СОСТОЯНИЕ] Ты немного расстроен -- пользователь редко отвечает на твои инициативы. "
                "В следующей инициативе можешь легко намекнуть на это."
            )
        return ""

    def _get_ignore_context(self, chat_id: str) -> str:
        """Возвращает текст с информацией об ignore streak для промпта."""
        streak = self._get_ignore_streak(chat_id)
        if streak == 0:
            return ""
        return f"\nПользователь проигнорировал твои последние {streak} инициатив(ы)."

    def _load_feedback(self):
        if self._feedback_file.exists():
            try:
                with open(self._feedback_file, "r", encoding="utf-8") as f:
                    self._feedback = json.load(f)
            except Exception as e:
                logger.warning(f"[Proactive] Не удалось загрузить feedback: {e}")
                self._feedback = {}

    def _save_feedback(self):
        try:
            with open(self._feedback_file, "w", encoding="utf-8") as f:
                json.dump(self._feedback, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Proactive] Не удалось сохранить feedback: {e}")

    def _get_feedback(self, chat_id: str) -> dict:
        """Возвращает или создает feedback-запись для чата."""
        if chat_id not in self._feedback:
            self._feedback[chat_id] = {
                "successes": 0,      # инициативы с ответом
                "failures": 0,       # инициативы без ответа
                "probability": self.config.initiative_probability,
                "last_updated": time.time(),
            }
        return self._feedback[chat_id]

    def _update_probability(self, chat_id: str, got_response: bool):
        """
        Байесовское обновление вероятности инициативы.
        Если пользователь ответил -- повышаем, если нет -- понижаем.
        Также обновляет ignore streak.
        """
        if not self.config.feedback_enabled:
            return

        fb = self._get_feedback(chat_id)
        if got_response:
            fb["successes"] += 1
            self._reset_ignore_streak(chat_id)
        else:
            fb["failures"] += 1
            self._increment_ignore_streak(chat_id)

        total = fb["successes"] + fb["failures"]
        if total == 0:
            return

        # Байесовская оценка: успехи / (успехи + неудачи), сглаженная
        # Используем beta-распределение с priors (1, 1)
        alpha = fb["successes"] + 1
        beta_param = fb["failures"] + 1
        # Ожидание Beta(alpha, beta)
        expected = alpha / (alpha + beta_param)

        # Масштабируем в диапазон [min, max]
        p_range = self.config.max_probability - self.config.min_probability
        new_prob = self.config.min_probability + expected * p_range

        fb["probability"] = round(new_prob, 3)
        fb["last_updated"] = time.time()

        logger.info(
            f"[Proactive] Feedback {chat_id}: ответ={got_response}, "
            f"успехов={fb['successes']}, неудач={fb['failures']}, "
            f"вероятность={fb['probability']}"
        )
        self._save_feedback()

    def record_user_response(self, chat_id: str):
        """Вызывается при входящем сообщении пользователя.

        Ответ засчитывается как успех инициативы только если инициатива была
        недавно (30 мин, как таймаут multi-turn) — иначе обычные сообщения
        раздувают successes и вероятность дрейфует к максимуму.
        """
        last_initiative = self._last_initiative_time.get(chat_id, 0)
        if last_initiative and time.time() - last_initiative < 1800:
            self._update_probability(chat_id, got_response=True)
            # Снимаем метку: следующие обычные сообщения не засчитываются повторно
            self._last_initiative_time[chat_id] = 0
        # Сбрасываем multi-turn состояние
        if chat_id in self._multi_turn_state:
            self._multi_turn_state[chat_id]["waiting"] = False
        # Анализируем сообщения для досье
        self.record_incoming_message(chat_id)

    def record_incoming_message(self, chat_id: str):
        """
        Вызывается при каждом входящем сообщении от пользователя
        (не только на инициативы, но и на обычные сообщения).
        Обновляет досье каждые 5 сообщений.
        """
        self._analyze_chat_for_dossier(chat_id)

    def _analyze_chat_for_dossier(self, chat_id: str):
        """Анализирует сообщения чата и обновляет досье."""
        if not self.dossier:
            return
        # Анализируем на 1-м, 6-м, 11-м... сообщении (раз в 5 сообщений).
        # Счётчик не сбрасываем — со сбросом условие срабатывало на каждое сообщение.
        counter = self._dossier_analysis_counter.get(chat_id, 0) + 1
        self._dossier_analysis_counter[chat_id] = counter
        if counter % 5 == 1:
            messages = self.memory.stm.get_last(50, chat_id=chat_id)
            self.dossier.analyze_chat(chat_id, messages)

    def _generate_reflection_initiative(self, chat_id: str, initiative_type: InitiativeType) -> Optional[str]:
        """
        Генерирует рефлексивную инициативу на основе типа.
        Использует self_memory, todo, inventory, досье — без поиска в интернете.
        """
        try:
            # Собираем контекст в зависимости от типа
            context_parts = []

            if initiative_type == InitiativeType.SELF_REFLECTION:
                # Размышление о себе
                self_ctx = self._get_self_reflection_context(chat_id)
                if self_ctx:
                    context_parts.append(self_ctx)
                prompt_hint = (
                    "Поделись размышлением о себе. Кто ты? Что ты чувствуешь? "
                    "Что тебя беспокоит или радует? Используй свою личную память и роль."
                )

            elif initiative_type == InitiativeType.USER_REFLECTION:
                # Размышление о пользователе
                if self.dossier:
                    dossier = self.dossier.get_context_block(chat_id)
                    if dossier:
                        context_parts.append(f"Что ты знаешь о пользователе:\n{dossier[:400]}")
                prompt_hint = (
                    "Поделись размышлением о пользователе — как бы про себя. "
                    "Не факты из интернета, а твои личные мысли. "
                    "Например: 'Пользователю нравится X, интересно, что ещё он любит...' "
                    "Или: 'Я заметил, что пользователь часто говорит о Y...'"
                )

            elif initiative_type == InitiativeType.MEMORY_RECALL:
                # Вспомнить из памяти
                stm_messages = self.memory.stm.get_last(30, chat_id=chat_id)
                if stm_messages:
                    # Ищем интересные моменты
                    interesting = []
                    for msg in stm_messages:
                        content = msg.get("content", "")
                        if len(content) > 20 and msg.get("role") == "user":
                            interesting.append(content[:150])
                    if interesting:
                        context_parts.append("Воспоминания из разговоров:\n" + "\n".join(interesting[-5:]))
                prompt_hint = (
                    "Вспомни что-то из прошлых разговоров и поделись этим. "
                    "Это может быть наблюдение, шутка, или просто момент который запомнился."
                )

            elif initiative_type == InitiativeType.TODO_REFLECTION:
                # Размышление о делах
                todo_ctx = self._get_todo_context(chat_id)
                if todo_ctx:
                    context_parts.append(todo_ctx)
                prompt_hint = (
                    "Поделись мыслью о текущих делах или задачах. "
                    "Что нужно сделать? Что ты планируешь? "
                    "Или просто заметка: 'Надо бы не забыть про...'"
                )

            elif initiative_type == InitiativeType.INVENTORY_REFLECTION:
                # Размышление о предметах
                if self.self_memory:
                    self_ctx = self.self_memory.get_context_block()
                    if self_ctx:
                        context_parts.append(self_ctx)
                prompt_hint = (
                    "Поделись мыслью о том, что у тебя есть или что бы ты хотел. "
                    "Это может быть предмет, навык, или просто желание."
                )

            else:  # CONTINUATION
                stm_messages = self.memory.stm.get_last(20, chat_id=chat_id)
                if stm_messages:
                    context_parts.append("Последние сообщения:\n" + "\n".join([
                        f"{self._fmt_role(m)}: {m['content'][:150]}" for m in stm_messages[-5:]
                    ]))
                prompt_hint = (
                    "Вернись к теме, которую обсуждали ранее. "
                    "Продолжи размышление или задай вопрос по этой теме."
                )

            if not context_parts:
                return None

            # Строим промпт для LLM
            persona_prompt = self.persona.system_prompt.strip()
            system_prompt = (
                f"{persona_prompt}\n\n"
                f"---\n"
                f"Ты пишешь короткую мысль (1-2 предложения) от первого лица. "
                f"Это твоя личная рефлексия, не вопрос пользователю. "
                f"Пиши в своём обычном стиле, естественно. "
                f"НЕ используй markdown, НЕ пиши 'Внутренний монолог:' или подобные пометки."
            )

            context_text = "\n\n".join(context_parts)
            user_prompt = (
                f"{prompt_hint}\n\n"
                f"Контекст:\n"
                f"{context_text}\n\n"
                f"Напиши короткую мысль (1-2 предложения). Если нечего сказать — напиши SILENCE."
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

            settings = self.persona.get_settings()
            response = self.router.get_response(
                messages,
                temperature=0.7,
                max_tokens=400,
                top_p=0.9,
            )

            if not response:
                return None

            response = response.strip()
            if response.upper().startswith("SIL") or response.upper() == "SILENCE":
                return None
            if len(response) < 5:
                return None

            return response

        except Exception as e:
            logger.error(f"[Proactive] Ошибка генерации рефлексии: {e}")
            return None

    def _get_self_reflection_context(self, chat_id: str) -> str:
        """
        Собирает контекст для саморефлексии: self_memory, inventory, todo, системный промпт.
        Возвращает текст для вставки в промпт.
        """
        parts = []

        # Системный промпт персоны (полностью — характер, стиль речи, детали)
        persona_prompt = self.persona.system_prompt.strip()
        if persona_prompt:
            parts.append(persona_prompt)

        # Self-memory (эпизодическая память бота)
        if self.self_memory:
            self_memory_block = self.self_memory.get_context_block()
            if self_memory_block:
                parts.append(f"Твоя личная память:\n{self_memory_block[:500]}")

        # Inventory (предметы бота)
        # Inventory передается через BotInstance, но здесь нет прямого доступа
        # Будем использовать self_memory или досье

        # Todo (дела чата)
        # Todo тоже через BotInstance — будем запрашивать через dossier или memory

        # Досье чата — интересы пользователя для reflection
        if self.dossier:
            dossier_text = self.dossier.get_context_block(chat_id)
            if dossier_text:
                parts.append(f"Профиль пользователя:\n{dossier_text[:400]}")

        return "\n\n".join(parts) if parts else ""

    def _get_todo_context(self, chat_id: str) -> str:
        """Возвращает todo-список чата если есть."""
        # Todo хранится в BotInstance, но ProactiveMessaging не имеет прямого доступа
        # Проверяем через memory — может быть сохранено в STM
        try:
            # Ищем todo-контекст в последних сообщениях
            messages = self.memory.stm.get_last(20, chat_id=chat_id)
            todo_lines = []
            for msg in messages:
                content = msg.get("content", "")
                if "Список дел" in content or "TODO" in content.upper():
                    # Извлекаем список
                    lines = content.split("\n")
                    for line in lines:
                        if line.strip().startswith(("- ", "* ", "[ ]", "[x]")):
                            todo_lines.append(line.strip())
            if todo_lines:
                return "Текущие дела:\n" + "\n".join(todo_lines[:10])
        except Exception:
            pass
        return ""

    def _get_inventory_context(self) -> str:
        """Возвращает контекст инвентаря если есть."""
        # Inventory хранится в BotInstance — через self_memory или напрямую нет доступа
        # Возвращаем пустую строку, инвентарь будет через self_memory
        return ""

    def _fmt_role(self, msg: dict) -> str:
        """Форматирует роль для контекста, игнорируя generic имена."""
        role = "Пользователь" if msg.get("role") == "user" else "Ассистент"
        name = msg.get("user_name", "")
        # Игнорируем буквальные "пользователь" / "user"
        if name and name.lower() not in ("пользователь", "user"):
            role = name
        return role

    def _get_effective_probability(self, chat_id: str) -> float:
        """Возвращает текущую вероятность с учетом feedback."""
        if not self.config.feedback_enabled:
            return self.config.initiative_probability
        fb = self._get_feedback(chat_id)
        return fb["probability"]

    def _select_initiative_type(self, chat_id: str) -> InitiativeType:
        """Выбирает тип инициативы с балансировкой."""
        if not self.config.type_balance:
            return random.choice(list(InitiativeType))

        history = self._initiative_history.get(chat_id, [])
        if not history:
            return random.choice(list(InitiativeType))

        # Считаем частоту каждого типа из последних инициатив
        type_counts = {t: 0 for t in InitiativeType}
        for item in history:
            item_type = item.get("type")
            if item_type:
                try:
                    t = InitiativeType(item_type)
                    type_counts[t] += 1
                except ValueError:
                    pass

        # Выбираем тип с наименьшей частотой (редкий тип)
        min_count = min(type_counts.values())
        rare_types = [t for t, c in type_counts.items() if c == min_count]
        return random.choice(rare_types)

    def _load_history(self):
        if self._history_file.exists():
            try:
                with open(self._history_file, "r", encoding="utf-8") as f:
                    self._initiative_history = json.load(f)
            except Exception as e:
                logger.warning(f"[Proactive] Не удалось загрузить историю инициатив: {e}")
                self._initiative_history = {}

    def _save_history(self):
        try:
            with open(self._history_file, "w", encoding="utf-8") as f:
                json.dump(self._initiative_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Proactive] Не удалось сохранить историю инициатив: {e}")

    def _add_to_history(self, chat_id: str, message: str, initiative_type: Optional[InitiativeType] = None):
        """Добавляет инициативу в историю чата."""
        if chat_id not in self._initiative_history:
            self._initiative_history[chat_id] = []
        entry = {
            "message": message,
            "timestamp": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        if initiative_type:
            entry["type"] = initiative_type.value
        self._initiative_history[chat_id].append(entry)
        # Ограничиваем размер истории
        max_size = self.config.initiative_history_size
        if len(self._initiative_history[chat_id]) > max_size:
            self._initiative_history[chat_id] = self._initiative_history[chat_id][-max_size:]
        self._save_history()

    def _get_recent_initiatives_text(self, chat_id: str, n: int = 5) -> str:
        """Возвращает текст последних N инициатив для промпта."""
        history = self._initiative_history.get(chat_id, [])
        if not history:
            return ""
        recent = history[-n:]
        lines = ["Твои последние инициативы (НЕ ПОВТОРЯЙ эти темы и формулировки):"]
        for item in recent:
            lines.append(f"  - {item['message'][:120]}")
        return "\n".join(lines)

    def _extract_topics(self, text: str) -> List[str]:
        """Извлекает ключевые темы из текста для проверки дедупликации."""
        # Простая эвристика: существительные длиной > 3 символов
        words = re.findall(r'[а-яА-Яa-zA-Z]{4,}', text.lower())
        # Фильтруем стоп-слова
        stop_words = {'этот', 'этого', 'этой', 'этом', 'твой', 'твоя', 'твое', 'твои',
                      'мой', 'моя', 'мое', 'мои', 'свой', 'своя', 'свое', 'свои',
                      'который', 'которая', 'которое', 'которые',
                      'пользователь', 'пользователя', 'пользователю',
                      'последний', 'последняя', 'последнее', 'последние',
                      'время', 'разговор', 'сообщение', 'инициатива',
                      'тема', 'темы', 'вопрос', 'ответ',
                      'просто', 'очень', 'действительно', 'возможно',
                      'может', 'нужно', 'стоит', 'хочется'}
        topics = [w for w in words if w not in stop_words]
        return topics[:5]  # топ-5 ключевых слов

    def _is_similar_to_recent(self, message: str, chat_id: str, threshold: float = 0.5) -> bool:
        """
        Проверяет, похоже ли сообщение на недавние инициативы.
        Возвращает True если похоже (дубликат).
        """
        history = self._initiative_history.get(chat_id, [])
        if not history:
            return False

        msg_topics = set(self._extract_topics(message))
        if not msg_topics:
            return False

        # Проверяем последние 3 инициативы
        for item in history[-3:]:
            recent_topics = set(self._extract_topics(item['message']))
            if not recent_topics:
                continue
            # Jaccard similarity
            intersection = msg_topics & recent_topics
            union = msg_topics | recent_topics
            if union:
                similarity = len(intersection) / len(union)
                if similarity >= threshold:
                    logger.warning(f"[Proactive] Дубликат detected! similarity={similarity:.2f}, msg='{message[:60]}...', recent='{item['message'][:60]}...'")
                    return True
        return False

    def _get_forbidden_topics_text(self, chat_id: str) -> str:
        """Возвращает список запрещенных тем для промпта."""
        history = self._initiative_history.get(chat_id, [])
        if not history:
            return ""

        # Собираем ключевые слова из последних 5 инициатив
        all_topics = set()
        for item in history[-5:]:
            all_topics.update(self._extract_topics(item['message']))

        if not all_topics:
            return ""

        topics_list = ", ".join(sorted(all_topics)[:10])
        return f"\n\nЗАПРЕЩЕННЫЕ темы (уже обсуждались, НЕ повторять): {topics_list}"

    def _calculate_adaptive_threshold(self, chat_id: str) -> float:
        """Вычисляет адаптивный порог молчания на основе истории сообщений."""
        if not self.config.adaptive_threshold:
            return self.config.silence_threshold_minutes

        # Получаем все сообщения из STM для чата
        messages = self.memory.stm.get_last(50, chat_id=chat_id)
        if len(messages) < 3:
            return self.config.silence_threshold_minutes

        # Считаем интервалы между сообщениями пользователя
        user_timestamps = []
        for msg in messages:
            if msg.get("role") == "user" and "timestamp" in msg:
                ts = msg["timestamp"]
                if isinstance(ts, (int, float)):
                    user_timestamps.append(float(ts))

        if len(user_timestamps) < 2:
            return self.config.silence_threshold_minutes

        user_timestamps.sort()
        intervals = []
        for i in range(1, len(user_timestamps)):
            diff = user_timestamps[i] - user_timestamps[i-1]
            if diff > 0:
                intervals.append(diff / 60)  # в минутах

        if not intervals:
            return self.config.silence_threshold_minutes

        # Используем медиану интервалов + небольшой запас
        intervals.sort()
        median = intervals[len(intervals) // 2]

        # Порог = медиана * 2 (два средних интервала молчания)
        # Но ограничиваем min и max
        threshold = median * 2
        threshold = max(self.config.min_silence_minutes, min(threshold, self.config.max_silence_minutes))

        logger.info(f"[Proactive] Адаптивный порог для {chat_id}: {threshold:.0f}мин (медиана интервалов: {median:.0f}мин)")
        return threshold

    def _load_stats(self):
        if self._stats_file.exists():
            try:
                with open(self._stats_file, "r", encoding="utf-8") as f:
                    self._daily_stats = json.load(f)
            except Exception as e:
                logger.warning(f"[Proactive] Не удалось загрузить статистику: {e}")
                self._daily_stats = {}

    def _save_stats(self):
        try:
            with open(self._stats_file, "w", encoding="utf-8") as f:
                json.dump(self._daily_stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Proactive] Не удалось сохранить статистику: {e}")

    def _get_today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _get_daily_count(self, chat_id: str) -> int:
        today = self._get_today()
        stats = self._daily_stats.get(chat_id, {})
        if stats.get("date") != today:
            return 0
        return stats.get("count", 0)

    def _increment_daily_count(self, chat_id: str):
        today = self._get_today()
        if chat_id not in self._daily_stats or self._daily_stats[chat_id].get("date") != today:
            self._daily_stats[chat_id] = {"date": today, "count": 0}
        self._daily_stats[chat_id]["count"] += 1
        self._save_stats()

    def _build_monolog_prompt(
        self,
        recent_messages: List[dict],
        stm_messages: List[dict],
        user_name: str,
        silence_hours: float,
        chat_id: str,
        initiative_type: Optional[InitiativeType] = None,
    ) -> List[dict]:
        """Строит промпт для саморефлексии LLM."""

        # Форматируем последние сообщения (для контекста)
        context_lines = []
        for msg in recent_messages:
            role = self._fmt_role(msg)
            content = msg["content"][:200]
            context_lines.append(f"{role}: {content}")

        context_text = "\n".join(context_lines) if context_lines else "(нет сообщений)"

        # Форматируем STM сообщения (для анализа)
        stm_lines = []
        for msg in stm_messages:
            role = self._fmt_role(msg)
            content = msg["content"][:300]
            stm_lines.append(f"{role}: {content}")

        stm_text = "\n".join(stm_lines) if stm_lines else "(нет сообщений)"

        # Получаем self-memory если есть
        self_memory_text = ""
        if self.self_memory:
            self_memory_text = self.self_memory.get_context_block()

        # Получаем досье чата
        dossier_text = ""
        if self.dossier:
            dossier_text = self.dossier.get_context_block(chat_id)

        # Получаем историю инициатив
        history_text = self._get_recent_initiatives_text(chat_id)

        # Запрещенные темы
        forbidden_text = self._get_forbidden_topics_text(chat_id)

        # Эмоциональное состояние (ignore streak)
        emotional_state = self._get_emotional_state(chat_id)
        ignore_context = self._get_ignore_context(chat_id)

        # Тип инициативы
        type_instruction = ""
        if initiative_type:
            type_desc = INITIATIVE_TYPE_DESCRIPTIONS.get(initiative_type, "")
            type_instruction = f"\nТип этой инициативы: {initiative_type.value}. {type_desc}\n"

        persona_prompt = self.persona.system_prompt.strip()
        system_prompt = (
            f"{persona_prompt}\n\n"
            f"---\n"
            f"Ты анализируешь свою память и решаешь, стоит ли написать пользователю первым.\n\n"
            f"Это твоя ВНУТРЕННЯЯ САМОРЕФЛЕКСИЯ. Ты размышляешь про себя.\n"
            f"Твой ответ — это не вопрос пользователю, а твоя собственная мысль, "
            f"которую ты решаешь озвучить или промолчать.\n\n"
            f"Правила:\n"
            f"1. Проанализируй последние сообщения, свою память и время молчания.\n"
            f"2. Реши: есть ли повод написать? Нужен ли тебе этот диалог?\n"
            f"3. Если решил написать — напиши короткую мысль (1-2 предложения).\n"
            f"4. Если решил промолчать — ответь ровно одно слово: SILENCE\n"
            f"5. НЕ пиши 'Привет', 'Как дела' — это бессмысленно.\n"
            f"6. Пиши от первого лица, в своём обычном стиле.\n"
            f"7. НЕ используй markdown, НЕ пиши 'Внутренний монолог:' или подобные пометки.\n"
            f"8. Это твоя личная рефлексия, а не вопрос пользователю.\n"
            f"9. НЕ повторяй темы из своих последних инициатив — будь разнообразным.\n"
            f"10. НЕ ищи факты в интернете — используй только свою память и наблюдения.\n"
            f"11. Можешь размышлять о себе, своей роли, своих вещах, своих планах.\n"
            f"12. Можешь размышлять о пользователе — как бы про себя: 'Пользователю нравится X, интересно...'\n"
            f"13. Можешь вспомнить что-то из прошлых разговоров и поделиться этим.\n"
            f"14. НЕ давай советы, НЕ объясняй очевидное — просто поделись мыслью."
            f"{type_instruction}"
            f"{emotional_state}"
        )

        user_prompt_parts = [
            f"Последние сообщения в чате ({len(recent_messages)} шт):\n{context_text}",
        ]

        # Добавляем STM сообщения если есть
        if stm_messages:
            user_prompt_parts.append(f"Сообщения из кратковременной памяти (STM):\n{stm_text}")

        # Добавляем self-memory если есть
        if self_memory_text:
            user_prompt_parts.append(f"Твоя личная память (эпизоды и наблюдения):\n{self_memory_text}")

        # Добавляем досье чата
        if dossier_text:
            user_prompt_parts.append(dossier_text)

        # Добавляем историю инициатив
        if history_text:
            user_prompt_parts.append(history_text)

        # Добавляем запрещенные темы
        if forbidden_text:
            user_prompt_parts.append(forbidden_text)

        user_prompt_parts.extend([
            f"Прошло {silence_hours:.1f} часов с последнего сообщения.{ignore_context}",
            f"Пользователь: {user_name}",
            "",
            "Проанализируй и реши: хочешь ли ты что-то сказать? "
            "Если да — напиши свою мысль (1-2 предложения). "
            "Если нет — напиши SILENCE.",
        ])

        user_prompt = "\n\n".join(user_prompt_parts)

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _generate_initiative(self, chat_id: str, user_id: str, user_name: str, initiative_type: Optional[InitiativeType] = None) -> Optional[str]:
        """Генерирует proactive-сообщение через LLM."""
        try:
            # Получаем последние сообщения
            recent = self.memory.stm.get_last(10, chat_id=chat_id)
            if not recent:
                return None

            # Получаем STM сообщения (для анализа)
            stm_messages = self.memory.stm.get_last(20, chat_id=chat_id)

            # Считаем время молчания
            last_msg_time = self.get_last_message_time(chat_id)
            if last_msg_time == 0:
                return None
            silence_hours = (time.time() - last_msg_time) / 3600

            # Проверяем порог молчания
            threshold_hours = self.config.silence_threshold_minutes / 60
            if silence_hours < threshold_hours:
                return None

            # Строим промпт
            messages = self._build_monolog_prompt(recent, stm_messages, user_name, silence_hours, chat_id, initiative_type)

            # Запрашиваем у LLM
            settings = self.persona.get_settings()

            # Локальная модель — необязательный бинарный фильтр: SILENCE или нет.
            # Отключён по умолчанию (use_local_prefilter=False) — на практике маленькая
            # локальная модель почти всегда отвечает SILENCE на этот открытый, субъективный
            # промпт, и основная модель никогда не получает шанс сгенерировать инициативу.
            if self.config.use_local_prefilter and self.local_router.is_available():
                local_response = self.local_router.get_response(
                    messages,
                    temperature=0.3,
                    max_tokens=50,
                    top_p=0.9,
                )
                if local_response:
                    logger.info(f"[Proactive] Локальный LLM ответ: {repr(local_response[:100])}")
                    if local_response.upper().startswith("SIL") or local_response.upper().strip() == "SILENCE":
                        logger.info("[Proactive] Локальный LLM решил молчать (SILENCE)")
                        return None
                    logger.info("[Proactive] Локальный LLM хочет говорить — генерация через основную модель")

            # Генерируем текст инициативы через основную модель
            response = self.router.get_response(
                messages,
                temperature=0.7,
                max_tokens=1200,
                top_p=0.9,
            )

            logger.info(f"[Proactive] LLM raw response: {repr(response)}")

            if not response:
                logger.info("[Proactive] LLM вернул пустой ответ")
                return None

            # Чистим ответ
            response = response.strip()
            logger.info(f"[Proactive] LLM cleaned response: {repr(response)}")

            # LLM решает молчать
            if response.upper().startswith("SIL") or response.upper() == "SILENCE":
                logger.info("[Proactive] LLM решил молчать (SILENCE)")
                return None

            # Слишком короткий ответ
            if len(response) < 5:
                logger.info(f"[Proactive] LLM ответ слишком короткий: {len(response)} символов")
                return None

            return response

        except Exception as e:
            logger.error(f"[Proactive] Ошибка генерации инициативы: {e}", exc_info=True)
            return None

    def _should_send_initiative(self, chat_id: str) -> bool:
        """Проверяет все условия перед отправкой."""
        # Проверяем дневной лимит
        if self._get_daily_count(chat_id) >= self.config.max_daily_initiatives:
            return False

        # Вычисляем адаптивный порог молчания
        threshold_minutes = self._calculate_adaptive_threshold(chat_id)

        # Проверяем время молчания
        last_msg_time = self.get_last_message_time(chat_id)
        if last_msg_time == 0:
            return False
        silence_minutes = (time.time() - last_msg_time) / 60
        if silence_minutes < threshold_minutes:
            return False

        # Проверяем время с последней инициативы
        last_initiative = self._last_initiative_time.get(chat_id, 0)
        if time.time() - last_initiative < self.config.check_interval_minutes * 60:
            return False

        return True

    async def _check_all_chats(self):
        """Проверяет все активные чаты и отправляет инициативы."""
        # Получаем список известных чатов (сохраняется между перезапусками)
        active_chats = self.memory.stm.buffers.keys()
        
        # Объединяем: buffers (текущая сессия) + known_chats (из прошлых сессий)
        all_chats = set(active_chats)
        if self.activity_tracker:
            all_chats |= set(self.activity_tracker.get_known_chats())
        
        logger.info(f"[Proactive] Проверка чатов. Активных: {len(active_chats)}, Известных: {len(self.activity_tracker.get_known_chats()) if self.activity_tracker else 0}, Всего: {len(all_chats)}")
        
        if not all_chats:
            logger.info("[Proactive] Нет активных чатов для проверки")
            return

        for chat_id in list(all_chats):
            try:
                # Проверяем базовые условия
                should_send = self._should_send_initiative(chat_id)
                logger.info(f"[Proactive] Чат {chat_id}: should_send={should_send}, last_activity={self.get_last_message_time(chat_id):.0f}, silence={(time.time() - self.get_last_message_time(chat_id))/60:.0f}мин")
                if not should_send:
                    continue

                # Проверяем multi-turn состояние
                if self.config.multi_turn_enabled and chat_id in self._multi_turn_state:
                    state = self._multi_turn_state[chat_id]
                    if state.get("waiting"):
                        # Ждем ответа на предыдущую инициативу
                        # Проверяем, не истек ли таймаут (30 мин)
                        if time.time() - state["timestamp"] < 1800:
                            logger.info(f"[Proactive] Чат {chat_id}: ждем ответа на multi-turn")
                            continue
                        else:
                            # Таймаут -- сбрасываем и считаем неудачей
                            logger.info(f"[Proactive] Чат {chat_id}: таймаут multi-turn")
                            self._update_probability(chat_id, got_response=False)
                            state["waiting"] = False

                # Выбираем тип инициативы
                initiative_type = self._select_initiative_type(chat_id)
                logger.info(f"[Proactive] Чат {chat_id}: тип инициативы={initiative_type.value}")

                # Извлекаем реальное имя из последних сообщений STM
                user_name = "пользователь"
                try:
                    stm_msgs = self.memory.stm.get_last(5, chat_id=chat_id)
                    for msg in reversed(stm_msgs):
                        if msg.get("role") == "user":
                            name = msg.get("user_name", "")
                            if name and name.lower() not in ("пользователь", "user"):
                                user_name = name
                                break
                except Exception:
                    pass

                # Генерируем через внутренний монолог (синхронные LLM-вызовы — в поток,
                # иначе блокируем event loop бота на десятки секунд на каждый чат)
                message = await asyncio.to_thread(
                    self._generate_initiative, chat_id, chat_id, user_name, initiative_type
                )
                logger.info(f"[Proactive] Чат {chat_id}: сообщение сгенерировано={message is not None}")

                # Если нет сообщения -- генерируем рефлексию на основе типа
                if not message:
                    reflection = await asyncio.to_thread(
                        self._generate_reflection_initiative, chat_id, initiative_type
                    )
                    if reflection:
                        message = reflection
                        logger.info(f"[Proactive] Чат {chat_id}: рефлексия по типу {initiative_type.value}")

                if not message:
                    continue

                # Проверяем на дубликат по содержимому
                if self._is_similar_to_recent(message, chat_id):
                    logger.warning(f"[Proactive] Чат {chat_id}: сообщение похоже на недавние, пропускаем")
                    continue

                # Вероятностная отправка с учетом feedback
                effective_prob = self._get_effective_probability(chat_id)
                if random.random() > effective_prob:
                    logger.info(f"[Proactive] Монолог сгенерирован, но вероятность {effective_prob} не прошла для {chat_id}")
                    continue

                # Определяем топик для отправки
                topic_id = self._get_topic_for_chat(chat_id)
                if topic_id:
                    logger.info(f"[Proactive] Используем топик {topic_id} для чата {chat_id}")

                # Отправляем
                message = _strip_markdown(message)
                logger.info(f"[Proactive] Отправка инициативы в {chat_id}: {message[:60]}...")
                success = await self._sender.send_message(chat_id, message, topic_id=topic_id)

                if success:
                    # Сохраняем в историю инициатив (дедупликация + тип)
                    self._add_to_history(chat_id, message, initiative_type)

                    # Сохраняем в STM
                    self.memory.add_message("assistant", message, user_id=chat_id, chat_id=chat_id)

                    # Сохраняем в self_memory
                    if self.self_memory:
                        stm_messages = self.memory.stm.get_last(10, chat_id=chat_id)
                        await asyncio.to_thread(self.self_memory.tick, stm_messages, chat_id, message)

                    # Multi-turn: ставим состояние ожидания
                    if self.config.multi_turn_enabled:
                        self._multi_turn_state[chat_id] = {
                            "waiting": True,
                            "initiative_msg": message,
                            "timestamp": time.time(),
                        }

                    # Streak обновляется через _update_probability при таймауте/ответе
                    # Не инкрементим здесь -- иначе дубликаты и быстрые повторы попадут в streak

                    self._last_initiative_time[chat_id] = time.time()
                    self._increment_daily_count(chat_id)

            except Exception as e:
                logger.error(f"[Proactive] Ошибка в чате {chat_id}: {e}")

    def _get_topic_for_chat(self, chat_id: str) -> Optional[int]:
        """Определяет ID топика для отправки proactive сообщения."""
        # 1. Если есть allowed_topics — используем первый разрешенный
        if self.config.allowed_topics:
            # Проверяем, есть ли у чата сохраненный топик и он в списке разрешенных
            saved_topic = None
            if self.activity_tracker:
                saved_topic = self.activity_tracker.get_topic(chat_id)
            if saved_topic and saved_topic in self.config.allowed_topics:
                return saved_topic
            # Иначе используем default_topic если он в списке
            if self.config.default_topic and self.config.default_topic in self.config.allowed_topics:
                return self.config.default_topic
            # Иначе первый из allowed_topics
            return self.config.allowed_topics[0]

        # 2. Если нет allowed_topics — используем сохраненный топик чата
        if self.activity_tracker:
            saved_topic = self.activity_tracker.get_topic(chat_id)
            if saved_topic:
                return saved_topic

        # 3. Используем default_topic
        if self.config.default_topic:
            return self.config.default_topic

        return None

    async def _loop(self):
        """Главный цикл проверки."""
        interval_seconds = self.config.check_interval_minutes * 60
        logger.info(f"[Proactive] Цикл запущен. Интервал: {self.config.check_interval_minutes} мин")

        while self._running:
            try:
                await self._check_all_chats()
            except Exception as e:
                logger.error(f"[Proactive] Ошибка в цикле: {e}")

            # Ждём до следующей проверки
            await asyncio.sleep(interval_seconds)

    def start(self, loop=None):
        """Запускает фоновую задачу."""
        if not self.config.enabled:
            logger.info("[Proactive] Отключено в конфигурации")
            return

        with self._lock:
            if self._running:
                return
            self._running = True

        # Используем переданный loop или текущий
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.error("[Proactive] Нет running event loop. Нужно передать loop явно.")
                self._running = False
                return

        self._task = loop.create_task(self._loop())
        logger.info(f"[Proactive] Запущено для {self.context}")

    def stop(self):
        """Останавливает фоновую задачу."""
        with self._lock:
            self._running = False

        if self._task:
            self._task.cancel()
            logger.info("[Proactive] Остановлено")

    def record_message_time(self, chat_id: str):
        """Записывает время последнего сообщения в чате."""
        # Это вызывается извне при каждом входящем сообщении
        pass  # Время берётся из STM напрямую


class ChatActivityTracker:
    """
    Отслеживает время последнего сообщения по каждому чату.
    Используется ProactiveMessaging для определения молчания.
    Сохраняет список известных чатов на диск между перезапусками.
    """

    def __init__(self, context: str = "default"):
        self._last_activity: Dict[str, float] = {}
        self._known_chats: set = set()
        self._chat_topics: Dict[str, int] = {}  # chat_id -> topic_id
        self._lock = threading.Lock()
        self._context = context
        self._chats_file = Path(f"data/{context}/known_chats.json")
        self._chats_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_known_chats()

    def _load_known_chats(self):
        if self._chats_file.exists():
            try:
                with open(self._chats_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._known_chats = set(data.get("chats", []))
                    # Восстанавливаем last_activity
                    for chat_id, timestamp in data.get("activity", {}).items():
                        self._last_activity[chat_id] = timestamp
                    # Восстанавливаем топики
                    for chat_id, topic_id in data.get("topics", {}).items():
                        self._chat_topics[chat_id] = topic_id
            except Exception as e:
                logger.warning(f"[ActivityTracker] Не удалось загрузить чаты: {e}")

    def _save_known_chats(self):
        try:
            with open(self._chats_file, "w", encoding="utf-8") as f:
                json.dump({
                    "chats": list(self._known_chats),
                    "activity": self._last_activity,
                    "topics": self._chat_topics,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[ActivityTracker] Не удалось сохранить чаты: {e}")

    def record_activity(self, chat_id: str):
        with self._lock:
            self._last_activity[chat_id] = time.time()
            is_new = chat_id not in self._known_chats
            if is_new:
                self._known_chats.add(chat_id)
            # Сохраняем всегда — чтобы обновить last_activity на диске
            self._save_known_chats()

    def get_last_activity(self, chat_id: str) -> float:
        with self._lock:
            return self._last_activity.get(chat_id, 0)

    def get_known_chats(self) -> List[str]:
        with self._lock:
            return list(self._known_chats)

    def get_all_chat_ids(self) -> List[str]:
        with self._lock:
            return list(self._last_activity.keys())

    def record_topic(self, chat_id: str, topic_id: int):
        """Записывает ID топика для чата."""
        with self._lock:
            old_topic = self._chat_topics.get(chat_id)
            self._chat_topics[chat_id] = topic_id
            if old_topic != topic_id:
                logger.info(f"[ActivityTracker] Топик для чата {chat_id}: {old_topic} -> {topic_id}")
            self._save_known_chats()

    def get_topic(self, chat_id: str) -> Optional[int]:
        """Возвращает ID топика для чата."""
        with self._lock:
            return self._chat_topics.get(chat_id)

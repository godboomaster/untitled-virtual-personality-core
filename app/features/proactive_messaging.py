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
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


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
        send_message: Callable[[str, str, Optional[int]], None],
        context: str = "default",
        self_memory=None,
    ):
        self.config = config
        self.router = router
        self.persona = persona
        self.memory = memory
        self.activity_tracker = activity_tracker
        self.get_last_message_time = get_last_message_time
        self._send_message = send_message
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
    ) -> List[dict]:
        """Строит промпт для саморефлексии LLM."""

        # Форматируем последние сообщения (для контекста)
        context_lines = []
        for msg in recent_messages:
            role = "Пользователь" if msg["role"] == "user" else "Ассистент"
            name = msg.get("user_name", "")
            if name:
                role = name
            content = msg["content"][:200]
            context_lines.append(f"{role}: {content}")

        context_text = "\n".join(context_lines) if context_lines else "(нет сообщений)"

        # Форматируем STM сообщения (для анализа)
        stm_lines = []
        for msg in stm_messages:
            role = "Пользователь" if msg["role"] == "user" else "Ассистент"
            name = msg.get("user_name", "")
            if name:
                role = name
            content = msg["content"][:300]
            stm_lines.append(f"{role}: {content}")

        stm_text = "\n".join(stm_lines) if stm_lines else "(нет сообщений)"

        # Получаем self-memory если есть
        self_memory_text = ""
        if self.self_memory:
            self_memory_text = self.self_memory.get_context_block()

        system_prompt = (
            f"Ты — {self.persona.persona_data.get('name', 'ассистент')}. "
            f"Ты анализируешь свою память и решаешь, стоит ли написать пользователю первым.\n\n"
            f"Это твоя ВНУТРЕННЯЯ САМОРЕФЛЕКСИЯ. Ты размышляешь про себя.\n"
            f"Твой ответ — это не вопрос пользователю, а твоя собственная мысль, "
            f"которую ты решаешь озвучить или промолчать.\n\n"
            f"Правила:\n"
            f"1. Проанализируй последние сообщения из STM, свою память и время молчания.\n"
            f"2. Реши: есть ли повод написать? Нужен ли тебе этот диалог?\n"
            f"3. Если решил написать — напиши короткую мысль (1-2 предложения).\n"
            f"4. Если решил промолчать — ответь ровно одно слово: МОЛЧУ\n"
            f"5. НЕ пиши 'Привет', 'Как дела' — это бессмысленно.\n"
            f"6. Пиши от первого лица, в своём обычном стиле.\n"
            f"7. НЕ используй markdown, НЕ пиши 'Внутренний монолог:' или подобные пометки.\n"
            f"8. Это твоя личная рефлексия, а не вопрос пользователю."
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

        user_prompt_parts.extend([
            f"Прошло {silence_hours:.1f} часов с последнего сообщения.",
            f"Пользователь: {user_name}",
            "",
            "Проанализируй и реши: хочешь ли ты что-то сказать? "
            "Если да — напиши свою мысль (1-2 предложения). "
            "Если нет — напиши МОЛЧУ.",
        ])

        user_prompt = "\n\n".join(user_prompt_parts)

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _generate_initiative(self, chat_id: str, user_id: str, user_name: str) -> Optional[str]:
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
            messages = self._build_monolog_prompt(recent, stm_messages, user_name, silence_hours)

            # Запрашиваем у LLM
            settings = self.persona.get_settings()
            response = self.router.get_response(
                messages,
                temperature=0.7,
                max_tokens=200,
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
            if response.upper() == "МОЛЧУ":
                logger.info("[Proactive] LLM решил молчать (МОЛЧУ)")
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

        # Проверяем время молчания
        last_msg_time = self.get_last_message_time(chat_id)
        if last_msg_time == 0:
            return False
        silence_minutes = (time.time() - last_msg_time) / 60
        if silence_minutes < self.config.silence_threshold_minutes:
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

                # Генерируем через внутренний монолог (всегда, без приветствий)
                message = self._generate_initiative(chat_id, chat_id, "пользователь")
                logger.info(f"[Proactive] Чат {chat_id}: сообщение сгенерировано={message is not None}")
                if not message:
                    continue

                # Вероятностная отправка
                if random.random() > self.config.initiative_probability:
                    logger.info(f"[Proactive] Монолог сгенерирован, но вероятность не прошла для {chat_id}")
                    continue

                # Определяем топик для отправки
                topic_id = self._get_topic_for_chat(chat_id)
                if topic_id:
                    logger.info(f"[Proactive] Используем топик {topic_id} для чата {chat_id}")

                # Отправляем
                logger.info(f"[Proactive] Отправка инициативы в {chat_id}: {message[:60]}...")
                await self._send_message(chat_id, message, topic_id)

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

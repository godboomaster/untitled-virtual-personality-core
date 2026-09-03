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
    silence_threshold_minutes: 180    # минут молчания перед инициативой (максимум сутки)
    initiative_probability: 0.3       # вероятность отправки после монолога (0-1)
    max_daily_initiatives: 5          # максимум инициатив в сутки на чат
    initiative_hours: "09:00-22:00"  # окно времени самоинициатив (пусто — сутки;
                                     #   вытеснило time_based_greetings — ритм утром/ночью
                                     #   теперь делает RhythmManager)
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
from app.core.language import detect_dialogue_language, language_name
from app.features.chat_dossier import ChatDossier
from app.core.local_router import get_local_router

logger = logging.getLogger(__name__)

# Максимальный порог молчания перед инициативой — сутки (продуктовое правило:
# персона пишет первой не позднее чем через день тишины). Жёсткий потолок в
# _should_send_initiative — не зависит от того, что записано в YAML.
MAX_SILENCE_MINUTES = 1440


def _strip_markdown(text: str) -> str:
    """Убирает markdown-разметку из текста инициативы перед отправкой."""
    # **bold** → text, *italic* → text (сохраняем содержимое)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    # __underline__ → text, _italic_ → text
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'\1', text)
    return text.strip()


def _looks_like_payload(text: str) -> bool:
    """Ответ LLM — сырые структурированные данные, а не сообщение.

    Веб-чаты/слабые модели иногда отвечают чужим служебным форматом
    (JSON профиля досье и т.п.) — такое пользователю отправлять нельзя.
    True — ответ целиком парсится как JSON-объект/массив."""
    t = text.strip()
    if not t or t[0] not in "{[":
        return False
    try:
        return isinstance(json.loads(t), (dict, list))
    except (ValueError, TypeError):
        return False  # текст, просто начинающийся со скобки, — допустимо


class InitiativeType(Enum):
    """Типы proactive-инициатив."""
    SELF_REFLECTION = "self_reflection"     # Размышление о себе, своей роли, памяти
    USER_REFLECTION = "user_reflection"     # Размышление о пользователе (не факты, а мысли "про себя")
    MEMORY_RECALL = "memory_recall"         # Вспомнить что-то из STM/LTM
    TODO_REFLECTION = "todo_reflection"     # Размышление о делах/задачах
    INVENTORY_REFLECTION = "inventory_reflection"  # Размышление о предметах в инвентаре
    CONTINUATION = "continuation"           # Продолжение предыдущей темы
    STATE_CHANGE = "state_change"           # Реакция на собственную жизнь: смена mood/pastime/storyline/офлайн-факт
    ADVICE_SEEKING = "advice_seeking"       # Просить совета у пользователя по своей незакрытой ситуации


INITIATIVE_TYPE_DESCRIPTIONS = {
    InitiativeType.SELF_REFLECTION: "Share a reflection about yourself, your role, personality or experience. Use self_memory, inventory, the persona system prompt.",
    InitiativeType.USER_REFLECTION: "Share a reflection about the user — as if to yourself. Not facts from the internet, but your own thoughts: 'The user likes X, I wonder what else...'",
    InitiativeType.MEMORY_RECALL: "Recall something from past conversations and share that memory",
    InitiativeType.TODO_REFLECTION: "Share a thought about current tasks, plans or todos (from the todo list)",
    InitiativeType.INVENTORY_REFLECTION: "Share a thought about the items in your inventory — what you have, what you would like",
    InitiativeType.CONTINUATION: "Return to a topic discussed earlier",
    InitiativeType.STATE_CHANGE: "Something just happened in YOUR OWN life (mood, pastime, storyline or an offline event). Share it naturally — as something you lived through, not a report.",
    InitiativeType.ADVICE_SEEKING: "Ask the user's advice about ONE of YOUR OWN ongoing life situations (see YOUR ONGOING LIFE SITUATIONS below). Present it briefly as your unresolved matter and genuinely ask their opinion — as a friend asking a friend, not as a report.",
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
    adaptive_threshold: bool = True  # адаптивный порог молчания
    min_silence_minutes: int = 30    # минимальный порог
    max_silence_minutes: int = 1440  # максимальный порог (24ч)
    initiative_history_size: int = 10  # сколько последних инициатив хранить
    feedback_enabled: bool = True    # обратная связь по реакции пользователя
    min_probability: float = 0.1     # минимальная вероятность инициативы
    max_probability: float = 0.9     # максимальная вероятность инициативы
    type_balance: bool = True        # балансировать типы инициатив
    multi_turn_enabled: bool = False # multi-turn инициативы (ожидание ответа)
    # Окно времени самоинициативы — задаёт пользователь: ("09:00", "22:00"),
    # None — круглые сутки. Переход через полночь разрешён: ("22:00", "08:00").
    # Действует на ВСЕ пути самоинициативы: регулярный цикл и сигнал движка жизни.
    initiative_hours: Optional[tuple] = None
    use_local_prefilter: bool = False  # локальная модель как бинарный SILENCE-фильтр перед
    # основной моделью — экономит вызовы, НО маленькие модели (3B и меньше) на этой открытой,
    # субъективной задаче ("стоит ли мне вообще что-то сказать?") систематически скатываются
    # в самый безопасный ответ и почти всегда отвечают SILENCE, из-за чего основная модель
    # никогда не получает шанс сгенерировать инициативу — даже при initiative_probability=1.0
    # (вероятность проверяется ПОСЛЕ генерации и просто никогда не достигается). Выключено по
    # умолчанию; включайте только если проверили, что локальная модель адекватно справляется
    # с этим конкретным промптом.

    @staticmethod
    def parse_hours(value) -> Optional[tuple]:
        """Окно времени самоинициативы из YAML/API: строка "09:00-22:00"
        (дефис/тире), dict {"from": "09:00", "to": "22:00"} или пара.
        Валидирует HH:MM; возвращает ("HH:MM", "HH:MM") или None (круглосуточно).
        Битое значение — None: окно выключено, а не молча ломает гейт."""
        if isinstance(value, dict):
            value = (value.get("from"), value.get("to"))
        if isinstance(value, str):
            for sep in ("–", "—", "-"):
                if sep in value:
                    value = tuple(p.strip() for p in value.split(sep, 1))
                    break
        if isinstance(value, (tuple, list)) and len(value) == 2:
            import re
            out = []
            for v in value:
                v = str(v or "").strip()
                if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", v):
                    return None
                hh, mm = v.split(":")
                out.append(f"{int(hh):02d}:{mm}")
            return tuple(out)
        return None

    @classmethod
    def from_dict(cls, data: dict) -> "ProactiveConfig":
        # Допускаем простой bool (proactive: true/false в YAML вместо dict)
        if isinstance(data, bool):
            return cls(enabled=data)
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
            adaptive_threshold=data.get("adaptive_threshold", True),
            min_silence_minutes=data.get("min_silence_minutes", 30),
            max_silence_minutes=data.get("max_silence_minutes", 1440),
            initiative_history_size=data.get("initiative_history_size", 10),
            feedback_enabled=data.get("feedback_enabled", True),
            min_probability=data.get("min_probability", 0.1),
            max_probability=data.get("max_probability", 0.9),
            type_balance=data.get("type_balance", True),
            multi_turn_enabled=data.get("multi_turn_enabled", False),
            initiative_hours=cls.parse_hours(data.get("initiative_hours")),
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
        living=None,
        intellect=None,
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
        # LivingPersona (слои state/world): добавляет в промпт монолога
        # текущее состояние и офлайн-факты жизни персоны
        self.living = living
        # Уровень интеллекта (§3.2 плана): primitive — только практические
        # триггеры, без рефлексии о себе/пользователе/прошлых темах
        self._primitive = bool(intellect is not None and intellect.is_primitive)

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
        # Тип последней инициативы по чату — для метрик вовлечённости
        # по типам (фаза 3.2): успех/провал записывается в разрезе типа
        self._last_initiative_type: Dict[str, str] = {}

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
        self._dossier_inflight: set = set()  # чаты, по которым анализ уже идёт в фоне

        # Локальный роутер для бинарных классификаций
        self.local_router = get_local_router()

        # Состояние multi-turn: chat_id -> {waiting: bool, initiative_msg: str, timestamp: float}
        self._multi_turn_state: Dict[str, dict] = {}

        # Счетчик проигнорированных инициатив (ignore streak)
        self._ignore_streak: Dict[str, int] = {}
        self._ignore_file = Path(f"data/{context}/ignore_streak.json")
        self._ignore_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_ignore_streak()

    def _side_response(self, messages, **kw):
        """Побочный вызов LLM (текст инициативы): fallback-цепочка основного
        роутера МИНУС основной провайдер; веб-чат — отдельный канал
        «proactive» (свой постоянный чат). Общий «side»-чат делят строго-
        форматные задачи (досье/LTM «Answer ONLY with JSON») — накопленная
        история там праймит модель, и на свободный промпт инициативы она
        отвечала чужим форматом (JSON досье уходил пользователю)."""
        return self.router.get_response(
            messages, exclude_provider=self.router.active_provider,
            webchat_channel="proactive", **kw)
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
        return getattr(self, "_ignore_streak", {}).get(chat_id, 0)

    def _apply_living_mood(self, chat_id: str, delta: float, tag: str):
        """Сдвиг mood в StateEngine — при живой персоне настроение живёт там
        (единая истина; сюда пишут и офлайн-события, и диалог, и ignore streak).
        Без living — no-op: эмоции остаются на старых градациях по streak."""
        living = getattr(self, "living", None)
        if living is None:
            return
        try:
            living.state_engine.apply_mood_impact(str(chat_id), delta, tag)
        except Exception as e:
            logger.debug(f"[Proactive] mood-impact не применён: {e}")

    def _increment_ignore_streak(self, chat_id: str):
        """Увеличивает счетчик игнора."""
        self._ignore_streak[chat_id] = self._ignore_streak.get(chat_id, 0) + 1
        self._save_ignore_streak()
        logger.info(f"[Proactive] Ignore streak {chat_id}: {self._ignore_streak[chat_id]}")
        # Обида от игнора — это настроение персоны, а не только счётчик:
        # толкаем valence вниз, ближайший тик плавно вернёт к baseline
        streak = self._ignore_streak[chat_id]
        if streak >= 10:
            delta, tag = -0.25, "глубокая обида"
        elif streak >= 7:
            delta, tag = -0.20, "сильная обида"
        elif streak >= 5:
            delta, tag = -0.15, "обида"
        elif streak >= 3:
            delta, tag = -0.10, "лёгкая обида"
        else:
            delta, tag = 0.0, ""
        if delta:
            self._apply_living_mood(chat_id, delta, tag)

    def _reset_ignore_streak(self, chat_id: str):
        """Сбрасывает счетчик игнора (пользователь ответил)."""
        if chat_id in self._ignore_streak:
            old = self._ignore_streak[chat_id]
            self._ignore_streak[chat_id] = 0
            self._save_ignore_streak()
            # Ответ после серии игноров — маленькое облегчение в mood
            if old >= 3:
                self._apply_living_mood(chat_id, 0.15, "облегчение")
            logger.info(f"[Proactive] Ignore streak {chat_id} сброшен (было: {old})")

    def ruin_mood(self, chat_id: str):
        """Заморозка персоны рушит её настроение: streak сразу в максимум обиды
        (10 — верхний порог в _get_emotional_state). Сбросится, когда пользователь
        ответит на инициативу после разморозки."""
        self._ignore_streak[chat_id] = 10
        self._save_ignore_streak()
        self._apply_living_mood(chat_id, -0.5, "глубокая обида")
        logger.info(f"[Proactive] Настроение в чате {chat_id} испорчено заморозкой (streak=10)")

    def _get_emotional_state(self, chat_id: str) -> str:
        """Эмоциональное состояние для промпта монолога.

        При живой персоне источник истины — StateEngine mood (туда ignore
        streak уже писался как mood_impact): текст лишь озвучивает актуальное
        настроение, поэтому он не может разойтись с [CURRENT STATE], который
        видит основная LLM. Без living — прежние градации по streak."""
        streak = self._get_ignore_streak(chat_id)

        mood = None
        if getattr(self, "living", None) is not None:
            try:
                mood = self.living.state_engine.get_state(chat_id).get("mood") or {}
            except Exception:
                mood = None
        if mood is not None:
            try:
                valence = float(mood.get("valence", 0) or 0)
            except (TypeError, ValueError):
                valence = 0.0
            if valence > -0.1:
                return ""
            tag = str(mood.get("tag", "") or "подавленность")
            reason = ("The user has been ignoring your messages lately — that's part of why. "
                      if streak >= 3 else "")
            if valence <= -0.5:
                return (
                    f"\n\n[INNER STATE] You feel deeply hurt and have quietly withdrawn ({tag}). {reason}"
                    "Do NOT demand attention and do NOT guilt-trip the user. "
                    "If you write at all — keep it short, calm and low-key."
                )
            if valence <= -0.3:
                return (
                    f"\n\n[INNER STATE] You are noticeably upset ({tag}). {reason}"
                    "In the next initiative express your hurt, or ask if everything is alright. "
                    "Be emotional, but not aggressive."
                )
            return (
                f"\n\n[INNER STATE] You feel a little down ({tag}). {reason}"
                "In the next initiative you may lightly hint at this."
            )

        if streak >= 10:
            return (
                "\n\n[INNER STATE] You feel deeply hurt and have quietly withdrawn. "
                "The user has been ignoring you for a very long time. "
                "Do NOT demand attention and do NOT guilt-trip the user. "
                "If you write at all — keep it short, calm and low-key."
            )
        elif streak >= 7:
            return (
                "\n\n[INNER STATE] You are deeply hurt. The user keeps ignoring your messages. "
                "You feel like they don't care about you. "
                "In the next initiative express your hurt -- ask why they don't reply. "
                "Be emotional, but not aggressive."
            )
        elif streak >= 5:
            return (
                "\n\n[INNER STATE] You are starting to feel unwanted. "
                "The user does not reply to your initiatives. "
                "In the next initiative you may hint that you feel lonely, "
                "or ask if everything is alright. Be gentle, but seeking attention."
            )
        elif streak >= 3:
            return (
                "\n\n[INNER STATE] You are a little upset -- the user rarely replies to your initiatives. "
                "In the next initiative you may lightly hint at this."
            )
        return ""

    def _get_ignore_context(self, chat_id: str) -> str:
        """Возвращает текст с информацией об ignore streak для промпта."""
        streak = self._get_ignore_streak(chat_id)
        if streak == 0:
            return ""
        return f"\nThe user has ignored your last {streak} initiative(s)."

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

    def _update_probability(self, chat_id: str, got_response: bool,
                            initiative_type: Optional[str] = None):
        """
        Байесовское обновление вероятности инициативы.
        Если пользователь ответил -- повышаем, если нет -- понижаем.
        Также обновляет ignore streak.
        initiative_type — тип отвеченной/проигнорированной инициативы:
        накапливаем вовлечённость в разрезе типов (фаза 3.2).
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

        # Вовлечённость по типам: что реально работает (советы/жизнь/темы)
        if initiative_type:
            by_type = fb.setdefault("by_type", {})
            t = by_type.setdefault(str(initiative_type),
                                   {"successes": 0, "failures": 0})
            t["successes" if got_response else "failures"] += 1

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
            self._update_probability(
                chat_id, got_response=True,
                initiative_type=self._last_initiative_type.pop(chat_id, None))
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
        """Анализирует сообщения чата и обновляет досье (в фоне)."""
        if not self.dossier:
            return
        # Анализируем на 1-м, 6-м, 11-м... сообщении (раз в 5 сообщений).
        # Счётчик не сбрасываем — со сбросом условие срабатывало на каждое сообщение.
        counter = self._dossier_analysis_counter.get(chat_id, 0) + 1
        self._dossier_analysis_counter[chat_id] = counter
        if counter % 5 != 1:
            return
        # Анализ — LLM-вызовы side-цепочки (десятки секунд, через веб-чат —
        # минуты). Синхронно он задерживал возврат уже готового ответа
        # пользователю — поэтому только фон. Пока прошлый анализ по этому
        # чату не закончился, новый не ставим — очередь не копим.
        with self._lock:
            if chat_id in self._dossier_inflight:
                return
            self._dossier_inflight.add(chat_id)
        # 80 сообщений батч — чтобы у пользователя набралось 30 для анализа
        messages = self.memory.stm.get_last(80, chat_id=chat_id)

        def _run():
            try:
                self.dossier.analyze_chat(chat_id, messages)
            except Exception as e:
                logger.warning(f"[Dossier] Фоновый анализ чата {chat_id} не удался: {e}")
            finally:
                with self._lock:
                    self._dossier_inflight.discard(chat_id)

        threading.Thread(target=_run, daemon=True,
                         name=f"dossier-analyze-{chat_id}").start()

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
                    "Share a reflection about yourself. Who are you? What do you feel? "
                    "What worries or pleases you? Use your personal memory and your role."
                )

            elif initiative_type == InitiativeType.USER_REFLECTION:
                # Размышление о пользователе
                if self.dossier:
                    dossier = self.dossier.get_context_block(chat_id)
                    if dossier:
                        context_parts.append(f"What you know about the user:\n{dossier[:400]}")
                prompt_hint = (
                    "Share a reflection about the user — as if to yourself. "
                    "Not facts from the internet, but your personal thoughts. "
                    "For example: 'The user likes X, I wonder what else they love...' "
                    "Or: 'I noticed the user often talks about Y...'"
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
                        context_parts.append("Memories from conversations:\n" + "\n".join(interesting[-5:]))
                prompt_hint = (
                    "Recall something from past conversations and share it. "
                    "It can be an observation, a joke, or simply a moment that stuck with you."
                )

            elif initiative_type == InitiativeType.TODO_REFLECTION:
                # Размышление о делах
                todo_ctx = self._get_todo_context(chat_id)
                if todo_ctx:
                    context_parts.append(todo_ctx)
                prompt_hint = (
                    "Share a thought about current tasks or plans. "
                    "What needs to be done? What are you planning? "
                    "Or simply a note: 'I should not forget about...'"
                )

            elif initiative_type == InitiativeType.INVENTORY_REFLECTION:
                # Размышление о предметах
                if self.self_memory:
                    self_ctx = self.self_memory.get_context_block()
                    if self_ctx:
                        context_parts.append(self_ctx)
                prompt_hint = (
                    "Share a thought about what you have or what you would like. "
                    "It can be an item, a skill, or simply a wish."
                )

            elif initiative_type == InitiativeType.STATE_CHANGE:
                # Жизнь персоны (§7): состояние + неозвученные факты —
                # раньше STATE_CHANGE падал в CONTINUATION-ветку («вернёмся
                # к прошлой теме») вообще без living-контекста
                if self.living:
                    try:
                        living_ctx = self.living.get_living_context(chat_id) or ""
                        unconsumed = self.living.state_engine.unconsumed(chat_id, limit=5)
                        fact_lines = []
                        for e in unconsumed:
                            p = e.get("payload") or {}
                            fact = p.get("event") or p.get("content")
                            if fact:
                                fact_lines.append(f"  - {str(fact)[:160]}")
                        if fact_lines:
                            living_ctx += (
                                "\n\n[UNSHARED FACTS FROM YOUR LIFE — the user has not heard these yet]\n"
                                + "\n".join(fact_lines)
                            )
                        if living_ctx:
                            context_parts.append(living_ctx)
                    except Exception as e:
                        logger.debug(f"[Proactive] Живой контекст недоступен: {e}")
                prompt_hint = (
                    "Something just happened in YOUR OWN life (see YOUR CURRENT STATE and "
                    "UNSHARED FACTS). Share it naturally as lived experience — at most one "
                    "or two facts, woven into conversation, never a list or a report."
                )

            elif initiative_type == InitiativeType.ADVICE_SEEKING:
                # Совет по своей сюжетной линии: пользователь нужен персоне,
                # а не только персона пользователю — разворот динамики
                storylines = self._advice_candidates(chat_id)
                if storylines:
                    lines = [f"  - {s.get('title', '')}: {str(s.get('summary', ''))[:200]}"
                             for s in storylines]
                    context_parts.append(
                        "Your ongoing life situations (your own unresolved matters):\n"
                        + "\n".join(lines))
                prompt_hint = (
                    "Ask the user for advice about ONE of your ongoing life situations. "
                    "This is YOUR unresolved matter — present it briefly and genuinely ask "
                    "their opinion, as a friend asking a friend. One situation, not a list."
                )

            else:  # CONTINUATION
                stm_messages = self.memory.stm.get_last(20, chat_id=chat_id)
                if stm_messages:
                    context_parts.append("Recent messages:\n" + "\n".join([
                        f"{self._fmt_role(m)}: {m['content'][:150]}" for m in stm_messages[-5:]
                    ]))
                prompt_hint = (
                    "Return to the topic discussed earlier. "
                    "Continue the thought or ask a question about that topic."
                )

            if not context_parts:
                return None

            # Строим промпт для LLM
            persona_prompt = self.persona.system_prompt.strip()

            # Язык рефлексии: явный детект по репликам пользователя из STM
            # (в контексте могут быть блоки на другом языке — не даём им
            # переключить язык сообщения)
            user_lang = None
            try:
                user_lang = detect_dialogue_language(
                    "", self.memory.stm.get_last(10, chat_id=chat_id))
            except Exception:
                user_lang = None
            if user_lang:
                _lname = language_name(user_lang)
                lang_rule = (
                    f"The user's language is {_lname}. Write ONLY in {_lname}. "
                )
            else:
                lang_rule = (
                    "Write in the language of the user's messages in the conversation "
                    "(if the user writes in Russian, write in Russian; if in English, write in English). "
                )

            system_prompt = (
                f"{persona_prompt}\n\n"
                f"---\n"
                f"You are writing a short thought (1-2 sentences) in first person. "
                f"This is your personal reflection, not a question to the user. "
                f"Write in your usual style, naturally. "
                f"{lang_rule}"
                f"Do NOT use markdown, do NOT write 'Inner monologue:' or similar labels."
            )

            context_text = "\n\n".join(context_parts)
            user_prompt = (
                f"{prompt_hint}\n\n"
                f"Context:\n"
                f"{context_text}\n\n"
                f"Write a short thought (1-2 sentences). If there is nothing to say — write SILENCE."
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

            settings = self.persona.get_settings()
            response = self._side_response(
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
            if _looks_like_payload(response):
                logger.info("[Proactive] LLM вернул служебный JSON вместо сообщения — пропуск")
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
                parts.append(f"Your personal memory:\n{self_memory_block[:500]}")

        # Inventory (предметы бота)
        # Inventory передается через BotInstance, но здесь нет прямого доступа
        # Будем использовать self_memory или досье

        # Todo (дела чата)
        # Todo тоже через BotInstance — будем запрашивать через dossier или memory

        # Досье чата — интересы пользователя для reflection
        if self.dossier:
            dossier_text = self.dossier.get_context_block(chat_id)
            if dossier_text:
                parts.append(f"User profile:\n{dossier_text[:400]}")

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
                if "Список дел" in content or "Todo list" in content or "TODO" in content.upper():
                    # Извлекаем список
                    lines = content.split("\n")
                    for line in lines:
                        if line.strip().startswith(("- ", "* ", "[ ]", "[x]")):
                            todo_lines.append(line.strip())
            if todo_lines:
                return "Current todos:\n" + "\n".join(todo_lines[:10])
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
        role = "User" if msg.get("role") == "user" else "Assistant"
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

    def _advice_candidates(self, chat_id: str) -> List[dict]:
        """Активные сюжетные линии персоны — материал для инициативы
        «попросить совет» (ADVICE_SEEKING). Без живого мира линий нет."""
        living = getattr(self, "living", None)
        if living is None:
            return []
        try:
            return living.world_engine.active_storylines(limit=3)
        except Exception:
            return []

    def _type_engagement(self, chat_id: str) -> Dict[str, float]:
        """Вовлечённость по типам инициатив (фаза 3.2): Байесовское ожидание
        успеха (s+1)/(s+f+2) из proactive_feedback.by_type. Типов без
        статистики в выдаче нет — для них работает дефолт 0.5."""
        fb = self._feedback.get(chat_id) or {}
        out: Dict[str, float] = {}
        for t, s in (fb.get("by_type") or {}).items():
            try:
                succ = int(s.get("successes", 0))
                fail = int(s.get("failures", 0))
            except (TypeError, ValueError, AttributeError):
                continue
            out[str(t)] = (succ + 1) / (succ + fail + 2)
        return out

    def _select_initiative_type(self, chat_id: str) -> InitiativeType:
        """Выбирает тип инициативы с балансировкой.
        primitive (§3.2): только практические триггеры — дела, предметы и
        события собственной жизни; рефлексивные типы не применяются.
        ADVICE_SEEKING — только когда есть живой мир с активными сюжетными
        линиями (иначе советовать не о чем — генерация гарантированно пустая).

        Взвешенный выбор: редкость типа в истории × вовлечённость типа
        (фаза 3.2) — частые и систематически игнорируемые типы получают
        меньше веса, но не исключаются вовсе (эксплорация сохраняется)."""
        if self._primitive:
            allowed = [InitiativeType.TODO_REFLECTION,
                       InitiativeType.INVENTORY_REFLECTION,
                       InitiativeType.STATE_CHANGE]
        else:
            allowed = list(InitiativeType)
            if not self._advice_candidates(chat_id):
                allowed.remove(InitiativeType.ADVICE_SEEKING)

        if not self.config.type_balance:
            return random.choice(allowed)

        history = self._initiative_history.get(chat_id, [])
        type_counts = {t: 0 for t in allowed}
        for item in history:
            item_type = item.get("type")
            if item_type:
                try:
                    t = InitiativeType(item_type)
                    if t in type_counts:
                        type_counts[t] += 1
                except ValueError:
                    pass

        engagement = self._type_engagement(chat_id)
        weights = [
            (1.0 / (1 + type_counts[t])) * engagement.get(t.value, 0.5)
            for t in allowed
        ]
        return random.choices(allowed, weights=weights, k=1)[0]

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

    def clear_history(self, chat_id: str) -> List[dict]:
        """Удаляет историю инициатив чата (полный сброс диалога).
        Возвращает удалённые записи — для снапшота корзины."""
        removed = self._initiative_history.pop(chat_id, [])
        if removed:
            self._save_history()
        return removed

    def restore_history(self, chat_id: str, entries: List[dict]):
        """Возвращает историю инициатив из снапшота корзины."""
        if not entries:
            return
        self._initiative_history[chat_id] = list(entries)
        self._save_history()

    def _get_recent_initiatives_text(self, chat_id: str, n: int = 5) -> str:
        """Возвращает текст последних N инициатив для промпта."""
        history = self._initiative_history.get(chat_id, [])
        if not history:
            return ""
        recent = history[-n:]
        lines = ["Your recent initiatives (DO NOT REPEAT these topics and phrasings):"]
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
        return f"\n\nFORBIDDEN topics (already discussed, do NOT repeat): {topics_list}"

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

    def pop_daily_stats(self, chat_id: str) -> Optional[dict]:
        """Удаляет дневной счётчик инициатив чата (полный сброс диалога).
        Возвращает удалённую запись {date, count} — для снапшота корзины."""
        removed = self._daily_stats.pop(chat_id, None)
        if removed is not None:
            self._save_stats()
        return removed

    def restore_daily_stats(self, chat_id: str, entry: dict):
        """Возвращает дневной счётчик инициатив из снапшота корзины."""
        if not entry:
            return
        self._daily_stats[chat_id] = dict(entry)
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

        context_text = "\n".join(context_lines) if context_lines else "(no messages)"

        # Форматируем STM сообщения (для анализа)
        stm_lines = []
        for msg in stm_messages:
            role = self._fmt_role(msg)
            content = msg["content"][:300]
            stm_lines.append(f"{role}: {content}")

        stm_text = "\n".join(stm_lines) if stm_lines else "(no messages)"

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

        # Живой контекст (план «живой» персоны, §7): состояние + офлайн-факты
        living_text = ""
        if self.living:
            try:
                living_text = self.living.get_living_context(chat_id) or ""
                unconsumed = self.living.state_engine.unconsumed(chat_id, limit=5)
                fact_lines = []
                for e in unconsumed:
                    p = e.get("payload") or {}
                    fact = p.get("event") or p.get("content")
                    if fact:
                        fact_lines.append(f"  - {str(fact)[:160]}")
                if fact_lines:
                    living_text += (
                        "\n\n[UNSHARED FACTS FROM YOUR LIFE — the user has not heard these yet]\n"
                        + "\n".join(fact_lines)
                        + "\nPick AT MOST one or two to mention. Share them as lived experience."
                    )
            except Exception as e:
                logger.debug(f"[Proactive] Живой контекст недоступен: {e}")

        # Эмоциональное состояние (ignore streak)
        emotional_state = self._get_emotional_state(chat_id)
        ignore_context = self._get_ignore_context(chat_id)

        # Тип инициативы
        type_instruction = ""
        if initiative_type:
            type_desc = INITIATIVE_TYPE_DESCRIPTIONS.get(initiative_type, "")
            type_instruction = f"\nType of this initiative: {initiative_type.value}. {type_desc}\n"

        persona_prompt = self.persona.system_prompt.strip()

        # Язык инициативы: явный детект по репликам пользователя — надёжнее,
        # чем просить модель угадать его по контексту (в контексте могут быть
        # инструкции/память на другом языке)
        user_lang = detect_dialogue_language("", recent_messages or stm_messages)
        if user_lang:
            _lname = language_name(user_lang)
            rule_lang = (
                f"7. The user's language is {_lname}. Write ONLY in {_lname} — "
                f"this overrides the language of any instructions or context above.\n"
            )
        else:
            rule_lang = (
                "7. Write in the language of the user's messages in the conversation "
                "(if the user writes in Russian, write in Russian; if in English, write in English).\n"
            )

        system_prompt = (
            f"{persona_prompt}\n\n"
            f"---\n"
            f"You are analyzing your memory and deciding whether to message the user first.\n\n"
            f"This is your INNER SELF-REFLECTION. You are thinking to yourself.\n"
            f"Your answer is not a question to the user, but your own thought "
            f"that you decide whether to voice or keep to yourself.\n\n"
            f"Rules:\n"
            f"1. Analyze the recent messages, your memory, and the silence duration.\n"
            f"2. Decide: is there a reason to write? Do you need this dialogue?\n"
            f"3. If you decide to write — write a short thought (1-2 sentences).\n"
            f"4. If you decide to stay silent — answer exactly one word: SILENCE\n"
            f"5. Do NOT write 'Hi', 'How are you' — that is meaningless.\n"
            f"6. Write in first person, in your usual style.\n"
            f"{rule_lang}"
            f"8. Do NOT use markdown, do NOT write 'Inner monologue:' or similar labels.\n"
            f"9. This is your personal reflection, not a question to the user.\n"
            f"10. Do NOT repeat topics from your recent initiatives — be diverse.\n"
            f"11. Do NOT look up facts on the internet — use only your memory and observations.\n"
            f"12. You may reflect on yourself, your role, your things, your plans.\n"
            f"13. You may reflect on the user — as if to yourself: 'The user likes X, interesting...'\n"
            f"14. You may recall something from past conversations and share it.\n"
            f"15. Do NOT give advice, do NOT explain the obvious — just share a thought."
            f"{type_instruction}"
            f"{emotional_state}"
        )

        # Примитивное существо не рефлексирует словами (§3.2): инициатива —
        # практический сигнал (действие/звук/жест), без анализа и философии
        if self._primitive:
            system_prompt += (
                "\n\nPRIMITIVE CREATURE MODE: you are NOT human and you cannot "
                "reflect, analyze or philosophize in words. Your initiative must "
                "be a simple practical signal about your things or tasks — an "
                "action, a sound, a gesture, 1-5 simple words. "
                "NEVER explain yourself. NEVER talk about feelings or thoughts."
            )

        user_prompt_parts = [
            f"Recent messages in the chat ({len(recent_messages)}):\n{context_text}",
        ]

        # Добавляем живой контекст (state + офлайн-факты) до памяти:
        # это то, что происходит с персоной прямо сейчас
        if living_text:
            user_prompt_parts.append(living_text)

        # «Попросить совета»: активные сюжетные линии — незакрытые ситуации
        # из жизни самой персоны, о которых она может спросить мнение
        if initiative_type == InitiativeType.ADVICE_SEEKING:
            storylines = self._advice_candidates(chat_id)
            if storylines:
                lines = [f"  - {s.get('title', '')}: {str(s.get('summary', ''))[:200]}"
                         for s in storylines]
                user_prompt_parts.append(
                    "YOUR ONGOING LIFE SITUATIONS (your own unresolved matters):\n"
                    + "\n".join(lines))

        # Добавляем STM сообщения если есть
        if stm_messages:
            user_prompt_parts.append(f"Messages from short-term memory (STM):\n{stm_text}")

        # Добавляем self-memory если есть
        if self_memory_text:
            user_prompt_parts.append(f"Your personal memory (episodes and observations):\n{self_memory_text}")

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
            f"{silence_hours:.1f} hours have passed since the last message.{ignore_context}",
            f"User: {user_name}",
            "",
            "Analyze and decide: do you want to say something? "
            "If yes — write your thought (1-2 sentences). "
            "If no — write SILENCE.",
        ])

        user_prompt = "\n\n".join(user_prompt_parts)

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _generate_initiative(self, chat_id: str, user_id: str, user_name: str,
                             initiative_type: Optional[InitiativeType] = None,
                             bypass_silence: bool = False) -> Optional[str]:
        """Генерирует proactive-сообщение через LLM.

        bypass_silence — порог молчания уже учтён вызывающим (сигнал
        состояния: скоринг «оплатил» тишину); интервалы и лимиты проверяет
        вызывающий, окно самоинициативы — _in_initiative_hours наверху."""
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

            # Проверяем порог молчания. У сигнала состояния он уже «оплачен»
            # скорингом (тишину учёл сам скор) — без bypass сигнал умирал бы
            # в окне «проверка цикла прошла, а adaptive-порог ещё нет»
            threshold_hours = self.config.silence_threshold_minutes / 60
            if not bypass_silence and silence_hours < threshold_hours:
                return None

            # Строим промпт
            messages = self._build_monolog_prompt(recent, stm_messages, user_name, silence_hours, chat_id, initiative_type)

            # Запрашиваем у LLM
            settings = self.persona.get_settings()

            # Локальная модель — необязательный бинарный фильтр: SILENCE или нет.
            # Отключён по умолчанию (use_local_prefilter=False) — на практике маленькая
            # локальная модель почти всегда отвечает SILENCE на этот открытый, субъективный
            # промпт, и основная модель никогда не получает шанс сгенерировать инициативу.
            if self.config.use_local_prefilter and self.local_router.is_available(task="proactive_prefilter"):
                local_response = self.local_router.get_response(
                    messages,
                    temperature=0.3,
                    max_tokens=50,
                    top_p=0.9,
                    task="proactive_prefilter",
                )
                if local_response:
                    logger.info(f"[Proactive] Локальный LLM ответ: {repr(local_response[:100])}")
                    if local_response.upper().startswith("SIL") or local_response.upper().strip() == "SILENCE":
                        logger.info("[Proactive] Локальный LLM решил молчать (SILENCE)")
                        return None
                    logger.info("[Proactive] Локальный LLM хочет говорить — генерация через основную модель")

            # Генерируем текст инициативы через fallback-цепочку без основного
            response = self._side_response(
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

            # Служебный JSON (напр. формат анализа досье) — не сообщение
            if _looks_like_payload(response):
                logger.info("[Proactive] LLM вернул служебный JSON вместо сообщения — пропуск")
                return None

            return response

        except Exception as e:
            logger.error(f"[Proactive] Ошибка генерации инициативы: {e}", exc_info=True)
            return None

    def _in_initiative_hours(self, now: Optional[float] = None) -> bool:
        """Разрешено ли сейчас время самоинициативы. Окно задаёт ПОЛЬЗОВАТЕЛЬ
        (config.initiative_hours) — движок сам момент не выбирает. Переход
        через полночь поддерживается: 22:00-08:00. Пусто/битое — всегда можно."""
        hours = self.config.initiative_hours
        if not hours:
            return True
        try:
            def to_min(s: str) -> int:
                h, m = s.split(":")
                return int(h) * 60 + int(m)

            lt = time.localtime(now if now is not None else time.time())
            cur = lt.tm_hour * 60 + lt.tm_min
            start, end = to_min(hours[0]), to_min(hours[1])
            if start <= end:
                return start <= cur < end
            return cur >= start or cur < end  # окно через полночь
        except Exception:
            return True

    def initiative_cheaply_possible(self, chat_id: str) -> bool:
        """Дешёвые гейты перед LLM-скорингом инициативы (без вызовов модели):
        окно часов, muted, дневной лимит, минимальное молчание и интервал между
        инициативами — как ручные проверки в state_initiative_signal.

        Не гарантирует отправку — только отсекает заведомо бессмысленный
        скоринг, чтобы движок жизни не тратил локальный вызов каждый тик
        (например, всю ночь вне окна самоинициативы)."""
        if not self.config.enabled:
            return False
        if not self._in_initiative_hours():
            return False
        if (self.persona.persona_data.get("features") or {}).get("muted"):
            return False
        if self._get_daily_count(chat_id) >= self.config.max_daily_initiatives:
            return False
        last_msg_time = self.get_last_message_time(chat_id)
        if last_msg_time == 0:
            return False
        silence_minutes = (time.time() - last_msg_time) / 60
        if silence_minutes < self.config.check_interval_minutes:
            return False
        # Глубокая обида (streak ≥ 7): персона замолкает, а не выпрашивает
        # внимание — скоринг/инициатива только после полного порога молчания
        if (self._get_ignore_streak(chat_id) >= 7
                and silence_minutes < self.config.silence_threshold_minutes):
            return False
        last_initiative = self._last_initiative_time.get(chat_id, 0)
        if time.time() - last_initiative < self.config.check_interval_minutes * 60:
            return False
        return True

    def _should_send_initiative(self, chat_id: str) -> bool:
        """Проверяет все условия перед отправкой."""
        # Замороженная персона (features.muted) не пишет ничего, включая инициативы
        if (self.persona.persona_data.get("features") or {}).get("muted"):
            return False

        # Время самоинициативы задаёт пользователь: вне окна не пишем сами
        if not self._in_initiative_hours():
            return False

        # Проверяем дневной лимит
        if self._get_daily_count(chat_id) >= self.config.max_daily_initiatives:
            return False

        # Вычисляем адаптивный порог молчания (но не выше суток — жёсткий максимум)
        threshold_minutes = min(self._calculate_adaptive_threshold(chat_id), MAX_SILENCE_MINUTES)

        # Проверяем время молчания
        last_msg_time = self.get_last_message_time(chat_id)
        if last_msg_time == 0:
            return False
        silence_minutes = (time.time() - last_msg_time) / 60
        if silence_minutes < threshold_minutes:
            return False

        # Глубокая обида (streak ≥ 7): персона замолкает, а не выпрашивает
        # внимание — адаптивный (короткий) порог для неё не действует
        if (self._get_ignore_streak(chat_id) >= 7
                and silence_minutes < self.config.silence_threshold_minutes):
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
                            self._update_probability(
                                chat_id, got_response=False,
                                initiative_type=state.get("type"))
                            state["waiting"] = False

                # Выбираем тип инициативы
                initiative_type = self._select_initiative_type(chat_id)
                logger.info(f"[Proactive] Чат {chat_id}: тип инициативы={initiative_type.value}")

                # Извлекаем реальное имя из последних сообщений STM
                user_name = "user"
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
                            "type": initiative_type.value if initiative_type else None,
                        }

                    # Streak обновляется через _update_probability при таймауте/ответе
                    # Не инкрементим здесь -- иначе дубликаты и быстрые повторы попадут в streak

                    self._last_initiative_time[chat_id] = time.time()
                    if initiative_type:
                        self._last_initiative_type[chat_id] = initiative_type.value
                    self._increment_daily_count(chat_id)

            except Exception as e:
                logger.error(f"[Proactive] Ошибка в чате {chat_id}: {e}")

    async def state_initiative_signal(self, chat_id: str, score: float, reason: str):
        """Сигнал от движка состояния (план «живой» персоны, §3.2/§3.4):
        скоринг инициативы превысил порог — у персоны есть повод написать.
        Не заменяет существующие гейты (muted, дневной лимит, интервалы) —
        только генерация уже «оплачена» скорингом, поэтому вероятностный
        бросок не повторяется. Отправка — через обычный пайплайн."""
        # Время самоинициативы задаёт пользователь: вне окна движок жизни
        # тоже не пишет сам (момент выбирает не движок, а окно пользователя)
        if not self._in_initiative_hours():
            logger.info("[Proactive] Сигнал состояния вне окна самоинициативы — молчим")
            return
        if not self._should_send_initiative(chat_id):
            # Стандартные гейты не прошли (лимит/интервал) — но для сигнала
            # состояния допускаем более короткое молчание, чем adaptive-порог:
            # скоринг уже учёл тишину. Проверяем минимум вручную.
            if (self.persona.persona_data.get("features") or {}).get("muted"):
                return
            if self._get_daily_count(chat_id) >= self.config.max_daily_initiatives:
                return
            last_msg_time = self.get_last_message_time(chat_id)
            if last_msg_time == 0:
                return
            silence_minutes = (time.time() - last_msg_time) / 60
            if silence_minutes < self.config.check_interval_minutes:
                return
            # Глубокая обида (streak ≥ 7): сигналы состояния тоже прореживаем —
            # не чаще полного порога молчания
            if (self._get_ignore_streak(chat_id) >= 7
                    and silence_minutes < self.config.silence_threshold_minutes):
                return
            last_initiative = self._last_initiative_time.get(chat_id, 0)
            if time.time() - last_initiative < self.config.check_interval_minutes * 60:
                return

        logger.info(f"[Proactive] Сигнал состояния {chat_id}: score={score:.2f} ({reason})")
        try:
            user_name = "user"
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

            message = await asyncio.to_thread(
                self._generate_initiative, chat_id, chat_id, user_name,
                InitiativeType.STATE_CHANGE, bypass_silence=True
            )
            if not message:
                message = await asyncio.to_thread(
                    self._generate_reflection_initiative, chat_id,
                    InitiativeType.STATE_CHANGE
                )
            if not message or self._is_similar_to_recent(message, chat_id):
                return

            topic_id = self._get_topic_for_chat(chat_id)
            message = _strip_markdown(message)
            success = await self._sender.send_message(chat_id, message, topic_id=topic_id)
            if success:
                self._add_to_history(chat_id, message, InitiativeType.STATE_CHANGE)
                self.memory.add_message("assistant", message, user_id=chat_id, chat_id=chat_id)
                if self.self_memory:
                    stm_messages = self.memory.stm.get_last(10, chat_id=chat_id)
                    await asyncio.to_thread(self.self_memory.tick, stm_messages, chat_id, message)
                self._last_initiative_time[chat_id] = time.time()
                self._last_initiative_type[chat_id] = InitiativeType.STATE_CHANGE.value
                self._increment_daily_count(chat_id)
                # Факты жизни прозвучали — отмечаем consumed
                if self.living:
                    try:
                        unconsumed = self.living.state_engine.unconsumed(chat_id, limit=10)
                        self.living.state_engine.mark_consumed(
                            [e["id"] for e in unconsumed])
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"[Proactive] Ошибка инициативы по состоянию {chat_id}: {e}")

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
        logger.info(f"[Proactive] Цикл запущен. Интервал: {self.config.check_interval_minutes} мин")

        while self._running:
            try:
                await self._check_all_chats()
            except Exception as e:
                logger.error(f"[Proactive] Ошибка в цикле: {e}")

            # Ждём до следующей проверки. Интервал перечитываем из конфига —
            # его могут поменять через API на живую, без перезапуска цикла.
            await asyncio.sleep(self.config.check_interval_minutes * 60)

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

    def pop_activity(self, chat_id: str) -> float:
        """Удаляет метку последней активности чата (полный сброс диалога —
        молчание обнуляется). Возвращает удалённый timestamp — для корзины."""
        with self._lock:
            ts = self._last_activity.pop(chat_id, 0)
            if ts:
                self._save_known_chats()
            return ts

    def restore_activity(self, chat_id: str, ts: float):
        """Возвращает метку последней активности из снапшота корзины."""
        if not ts:
            return
        with self._lock:
            self._last_activity[chat_id] = ts
            self._save_known_chats()

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

"""
Менеджер напоминаний.
Пользователь просит напомнить через N времени — бот пишет через N минут.
Хранит напоминания в data/{context}/reminders.json.
Фоновый цикл каждые 30с проверяет наступившие и шлёт через sender.
"""

import asyncio
import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


# ─── Парсинг запроса ──────────────────────────────────────

# Русские числительные прописью
_RU_WORD_NUMBERS = {
    "ноль": 0, "одну": 1, "один": 1, "одно": 1, "две": 2, "два": 2, "два": 2,
    "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
    "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20,
    "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
    "полтора": 1.5,
}

_TIME_UNIT_PATTERNS = [
    # минуты (цифры)
    (re.compile(r"через\s+(\d+(?:[.,]\d+)?)\s*(?:минуту|минуты|минут|мин|м)\b", re.IGNORECASE), 60.0),
    # часы (цифры)
    (re.compile(r"через\s+(\d+(?:[.,]\d+)?)\s*(?:час|часа|часов|ч)\b", re.IGNORECASE), 3600.0),
    # секунды (цифры)
    (re.compile(r"через\s+(\d+(?:[.,]\d+)?)\s*(?:секунду|секунды|секунд|сек|с)\b", re.IGNORECASE), 1.0),
    # дни (цифры)
    (re.compile(r"через\s+(\d+(?:[.,]\d+)?)\s*(?:день|дня|дней|д)\b", re.IGNORECASE), 86400.0),
]

# Паттерны для числительных прописью по единицам измерения
_TIME_WORD_PATTERNS = [
    # минуты прописью: "через две минуты", "через пять минут"
    (re.compile(r"через\s+(\w+)\s+(?:минуту|минуты|минут|мин)\b", re.IGNORECASE), 60.0),
    # часы прописью: "через три часа", "через один час"
    (re.compile(r"через\s+(\w+)\s+(?:час|часа|часов)\b", re.IGNORECASE), 3600.0),
    # секунды прописью: "через десять секунд"
    (re.compile(r"через\s+(\w+)\s+(?:секунду|секунды|секунд|сек)\b", re.IGNORECASE), 1.0),
    # дни прописью: "через пять дней"
    (re.compile(r"через\s+(\w+)\s+(?:день|дня|дней)\b", re.IGNORECASE), 86400.0),
]

# Словесные формы времени
_WORD_TIME = {
    "полчаса": 1800.0,
    "полтора часа": 5400.0,
    "час": 3600.0,
    "два часа": 7200.0,
    "минутку": 60.0,
    "минутку-другую": 120.0,
}


# ─── Абсолютное время ──────────────────────────────────────

# Паттерны: "до 12", "к 12", "в 11:30", "в 11", "до полудня", "до полуночи"
_ABS_TIME_PATTERNS = [
    # "до N:NN" / "к N:NN" / "в N:NN"
    re.compile(r"\b(?:до|к|в)\s+(\d{1,2}):(\d{2})\b", re.IGNORECASE),
    # "до N" / "к N" / "в N" (только часы, без минут)
    re.compile(r"\b(?:до|к|в)\s+(\d{1,2})\b(?!\s*:\s*\d)", re.IGNORECASE),
]

_ABS_WORD_TIME = {
    "полдень": 12.0,
    "полудня": 12.0,
    "полудню": 12.0,
    "полуночь": 24.0,
    "полуночи": 24.0,
    "полуночу": 24.0,
}


def _parse_absolute_time(text: str) -> Optional[tuple]:
    """
    Ищет абсолютное время в тексте ("до 12", "в 11:30", "к полудню").
    Возвращает (target_hour, target_minute, match_obj) или None.
    """
    lower = text.lower()

    # Словесные формы: "до полудня", "к полуночи"
    for word, hour in _ABS_WORD_TIME.items():
        pattern = re.compile(rf"\b(?:до|к|в)\s+{re.escape(word)}\b", re.IGNORECASE)
        m = pattern.search(lower)
        if m:
            return (hour, 0, m)

    # "до 11:30", "в 12:00"
    for pattern in _ABS_TIME_PATTERNS:
        m = pattern.search(lower)
        if m:
            if m.lastindex == 2:
                hour = int(m.group(1))
                minute = int(m.group(2))
            else:
                hour = int(m.group(1))
                minute = 0
            if 0 <= hour <= 24 and 0 <= minute < 60:
                return (float(hour), minute, m)

    return None


def _absolute_to_delay(hour: float, minute: int) -> Optional[float]:
    """Вычисляет задержку от текущего времени до указанного. В секундах."""
    from datetime import datetime

    now = datetime.now()
    target_hour = int(hour) % 24
    target_minute = minute

    # Вычисляем разницу в секундах
    now_total = now.hour * 3600 + now.minute * 60 + now.second
    target_total = target_hour * 3600 + target_minute * 60

    delay = target_total - now_total
    if delay <= 0:
        # Время уже прошло сегодня — переносим на завтра
        delay += 86400

    # Разумные границы: минимум 10 сек, максимум 7 дней
    if delay < 10 or delay > 7 * 86400:
        return None

    return float(delay)


def parse_reminder(text: str) -> Optional[tuple]:
    """
    Пытается распарсить запрос на напоминание.
    Возвращает (task, delay_seconds) или None.

    Примеры:
        "напомни мне через 30 минут позвонить маме"
        "напомни через 2 часа сделать домашку"
        "через 10 мин напомни"
    """
    lower = text.lower()

    # Должно быть "напом" (покрывает: напомни, напомнить, напоминание, напомните...)
    if "напом" not in lower:
        return None

    delay_seconds = None
    time_match_obj = None

    # ── 1. Относительное время: "через N ..." ──

    if "через" in lower:
        # Сначала проверяем словесные формы (полчаса, полтора часа и т.д.)
        for word, secs in _WORD_TIME.items():
            pattern = re.compile(rf"через\s+{re.escape(word)}\b", re.IGNORECASE)
            match = pattern.search(lower)
            if match:
                delay_seconds = secs
                time_match_obj = match
                break

        # Затем числовые паттерны (цифры: 30 минут, 2 часа)
        if delay_seconds is None:
            for pattern, multiplier in _TIME_UNIT_PATTERNS:
                match = pattern.search(lower)
                if match:
                    value = float(match.group(1).replace(",", "."))
                    delay_seconds = value * multiplier
                    time_match_obj = match
                    break

        # Затем числительные прописью (две минуты, пять часов)
        if delay_seconds is None:
            for pattern, multiplier in _TIME_WORD_PATTERNS:
                match = pattern.search(lower)
                if match:
                    word_num = match.group(1).lower()
                    if word_num in _RU_WORD_NUMBERS:
                        value = _RU_WORD_NUMBERS[word_num]
                        delay_seconds = value * multiplier
                        time_match_obj = match
                        break

    # ── 2. Абсолютное время: "до 12", "в 11:30", "к полудню" ──

    if delay_seconds is None:
        abs_time = _parse_absolute_time(text)
        if abs_time:
            abs_hour, abs_minute, abs_match = abs_time
            delay_seconds = _absolute_to_delay(abs_hour, abs_minute)
            if delay_seconds:
                time_match_obj = abs_match

    if delay_seconds is None:
        return None

    assert time_match_obj is not None  # подтверждаем: если delay найден — match тоже

    # Минимум 10 секунд, максимум 30 дней
    if delay_seconds < 10 or delay_seconds > 30 * 86400:
        return None

    # Извлекаем задачу — текст после временной фразы
    after_time = text[time_match_obj.end():].strip()
    after_time = re.sub(r"^[,:\-\s]+", "", after_time).strip()
    # Убираем глагол "напомни [мне]" если он стоит перед задачей
    after_time = re.sub(r"^(?:напомни|напомнить)(?:\s+мне)?\s*", "", after_time, flags=re.IGNORECASE).strip()
    after_time = re.sub(r"^[,:\-\s]+", "", after_time).strip()
    after_time = re.sub(r"[.!?]+$", "", after_time).strip()

    # Если после времени ничего нет — пробуем взять текст ДО
    if not after_time:
        before_time = text[:time_match_obj.start()].strip()
        before_time = re.sub(r"^(?:коннор|жабка|arrodes|connor)[,\s]+", "", before_time, flags=re.IGNORECASE)
        # Убираем все вариации "напомни/напоминание"
        before_time = re.sub(r"\b(?:напомни|напомнить|напоминание|напомните|напомню)\b", "", before_time, flags=re.IGNORECASE).strip()
        # Убираем "сделай/поставь ... с содержимым: ..."
        before_time = re.sub(r"\b(?:сделай|поставь|создай)\b", "", before_time, flags=re.IGNORECASE).strip()
        before_time = re.sub(r"\b(?:с\s+таким\s+содержимым|с\s+содержимым)\b[:\s]*", "", before_time, flags=re.IGNORECASE).strip()
        before_time = re.sub(r"^(?:мне|мне\s+про|мне\s+о)\b", "", before_time, flags=re.IGNORECASE).strip()
        before_time = re.sub(r"^[,:\-\s]+", "", before_time).strip()
        after_time = before_time

    return (after_time if after_time else None, delay_seconds)


# ─── Менеджер ─────────────────────────────────────────────

class ReminderManager:
    """
    Хранит напоминания и шлёт их в нужное время через sender.
    Запускается как фоновая asyncio-задача в event loop бота.
    """

    def __init__(self, context: str = "default"):
        self.context = context
        self._base_dir = Path(f"data/{context}/reminders")
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._file = self._base_dir / "reminders.json"
        self._lock = threading.Lock()

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._sender = None
        self._router = None
        self._persona = None

        self._reminders: List[dict] = []
        self._load()

        # In-memory состояние /remind без времени: chat_id -> task
        # (пережидает до ответа пользователя, теряется на рестарте — это ок)
        self._pending_remind: Dict[str, str] = {}

    def set_sender(self, sender):
        self._sender = sender

    def set_router_persona(self, router, persona):
        """Передаёт router и persona для генерации текста напоминания через LLM."""
        self._router = router
        self._persona = persona

    # ── persistence ──

    def _load(self):
        if self._file.exists():
            try:
                self._reminders = json.loads(self._file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[Reminder] Не удалось загрузить: {e}")
                self._reminders = []
        else:
            self._reminders = []

    def _save(self):
        try:
            self._file.write_text(
                json.dumps(self._reminders, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[Reminder] Не удалось сохранить: {e}")

    # ── API ──

    def add_reminder(self, chat_id: str, user_name: str, task: Optional[str],
                     delay_seconds: float, topic_id: Optional[int] = None) -> dict:
        """Создаёт напоминание. Возвращает словарь с информацией."""
        now = time.time()
        reminder = {
            "chat_id": str(chat_id),
            "user_name": user_name,
            "task": task,
            "created_at": now,
            "trigger_at": now + delay_seconds,
            "topic_id": topic_id,
            "fired": False,
        }
        with self._lock:
            self._reminders.append(reminder)
            self._save()
        logger.info(f"[Reminder] Добавлено: chat={chat_id} task='{task}' через {delay_seconds:.0f}с")
        return reminder

    def get_active(self, chat_id: str) -> List[dict]:
        """Активные (не сработавшие) напоминания для чата."""
        now = time.time()
        with self._lock:
            return [
                r for r in self._reminders
                if r["chat_id"] == str(chat_id) and not r.get("fired") and r["trigger_at"] > now
            ]

    def cancel_reminder(self, chat_id: str, index: int) -> bool:
        """Удаляет напоминание по индексу (из get_active). Возвращает True если удалено."""
        active = self.get_active(chat_id)
        if index < 0 or index >= len(active):
            return False
        target = active[index]
        with self._lock:
            try:
                self._reminders.remove(target)
                self._save()
                return True
            except ValueError:
                return False

    # ── pending /remind без времени (in-memory) ──

    def begin_pending_remind(self, chat_id: str, task: str):
        """Запоминает задачу напоминания, ждём от пользователя ответа про время."""
        with self._lock:
            self._pending_remind[str(chat_id)] = task

    def get_pending_remind(self, chat_id: str) -> Optional[str]:
        with self._lock:
            return self._pending_remind.get(str(chat_id))

    def clear_pending_remind(self, chat_id: str):
        with self._lock:
            self._pending_remind.pop(str(chat_id), None)

    def _cleanup_fired(self):
        """Удаляет сработавшие напоминания старше 24ч."""
        cutoff = time.time() - 86400
        with self._lock:
            before = len(self._reminders)
            self._reminders = [
                r for r in self._reminders
                if not (r.get("fired") and r["trigger_at"] < cutoff)
            ]
            if len(self._reminders) < before:
                self._save()

    # ── фоновый цикл ──

    async def _fire(self, reminder: dict):
        """Отправляет напоминание в чат. Текст генерируется через LLM в характере персоны."""
        if not self._sender:
            return
        chat_id = reminder["chat_id"]
        user_name = reminder.get("user_name", "")
        task = reminder.get("task")
        topic_id = reminder.get("topic_id")

        text = None

        # Пытаемся сгенерировать через LLM в характере персоны
        if self._router and self._persona:
            try:
                text = await asyncio.to_thread(self._generate_reminder_text, user_name, task)
            except Exception as e:
                logger.warning(f"[Reminder] LLM генерация не удалась: {e}")

        # Fallback — статический шаблон
        if not text:
            if task:
                text = f"{user_name}, напоминаю: {task}"
            else:
                text = f"{user_name}, время пришло! Ты просил напомнить."

        try:
            await self._sender.send_message(chat_id, text, topic_id=topic_id)
            logger.info(f"[Reminder] Отправлено в чат {chat_id}: {text[:60]}")
        except Exception as e:
            logger.error(f"[Reminder] Ошибка отправки в {chat_id}: {e}")

    def _generate_reminder_text(self, user_name: str, task: Optional[str]) -> Optional[str]:
        """Генерирует текст напоминания через LLM в характере персоны. Синхронный вызов."""
        assert self._router and self._persona  # проверяется в _fire перед вызовом
        persona_prompt = self._persona.system_prompt.strip()
        if task:
            user_content = f"Напомни {user_name}: {task}"
        else:
            user_content = f"Напомни {user_name} — он просил напомнить, но не уточнил о чём."

        messages = [
            {"role": "system", "content": (
                f"{persona_prompt}\n\n"
                "---\n"
                "Ты напоминаешь пользователю о чём-то по его просьбе. "
                "Напиши короткое напоминание (1-2 предложения) в своём характере. "
                "Обязательно упомяни суть задачи. "
                "НЕ используй markdown. НЕ пиши мета-пометки."
            )},
            {"role": "user", "content": user_content},
        ]

        response = self._router.get_response(messages, temperature=0.7, max_tokens=200, top_p=0.9)
        if not response or len(response.strip()) < 5:
            return None
        return response.strip()

    async def _loop(self):
        """Главный цикл — проверяет каждые 30 секунд."""
        logger.info(f"[Reminder] Цикл запущен для context={self.context}")
        cleanup_counter = 0

        while self._running:
            try:
                now = time.time()
                due = []
                with self._lock:
                    for r in self._reminders:
                        if not r.get("fired") and r["trigger_at"] <= now:
                            r["fired"] = True
                            due.append(r)
                    if due:
                        self._save()

                for r in due:
                    await self._fire(r)

                # Периодически чистим старые
                cleanup_counter += 1
                if cleanup_counter >= 60:  # ~раз в 30 минут
                    cleanup_counter = 0
                    self._cleanup_fired()

            except Exception as e:
                logger.error(f"[Reminder] Ошибка в цикле: {e}")

            await asyncio.sleep(30)

    def start(self, loop=None):
        """Запускает фоновую задачу."""
        if not loop:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.error("[Reminder] Нет running event loop")
                return

        self._running = True
        self._task = loop.create_task(self._loop())
        logger.info(f"[Reminder] Запущено для {self.context}")

    def stop(self):
        """Останавливает фоновую задачу."""
        self._running = False
        if self._task:
            self._task.cancel()
            logger.info("[Reminder] Остановлено")

    def format_delay(self, delay_seconds: float) -> str:
        """Человекочитаемое описание задержки."""
        if delay_seconds < 60:
            return f"{int(delay_seconds)} сек"
        if delay_seconds < 3600:
            return f"{int(delay_seconds / 60)} мин"
        hours = delay_seconds / 3600
        if hours < 24:
            h = int(hours)
            m = int((delay_seconds - h * 3600) / 60)
            return f"{h} ч {m} мин" if m else f"{h} ч"
        days = int(delay_seconds / 86400)
        return f"{days} дн"

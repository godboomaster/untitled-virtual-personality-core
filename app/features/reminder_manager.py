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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict

from app.core.language import detect_language, detect_dialogue_language, language_name

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

# Английские числительные прописью ("a"/"an" — для «in an hour»)
_EN_WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "a": 1, "an": 1,
}

_ALL_WORD_NUMBERS = {**_RU_WORD_NUMBERS, **_EN_WORD_NUMBERS}


def _has_remind_word(lower: str) -> bool:
    """Маркер просьбы о напоминании: «напом…» (рус) или «remind…» (англ)."""
    return "напом" in lower or re.search(r"\bremind", lower) is not None


def _remind_word_pos(lower: str) -> int:
    """Позиция первого вхождения «напом»/«remind» (-1 — нет)."""
    pos = lower.find("напом")
    m = re.search(r"\bremind", lower)
    if m and (pos == -1 or m.start() < pos):
        pos = m.start()
    return pos


# Английские единицы времени: множитель по первой букве (s/sec, m/min, h/hr, d/day)
_EN_UNITS_RE = r"hours?|hrs?|h|minutes?|mins?|min|seconds?|secs?|sec|days?|d"


def _en_unit_multiplier(unit: str) -> float:
    u = unit.lower()
    if u.startswith("s"):
        return 1.0
    if u.startswith("m"):
        return 60.0
    if u.startswith("h"):
        return 3600.0
    return 86400.0  # d / day / days

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

# Обратный (разговорный) порядок: единица, затем число — «через минуты 4»
_TIME_UNIT_FIRST_PATTERNS = [
    # минуты
    (re.compile(r"через\s+(?:минуту|минуты|минут|мин)\s+(\d+(?:[.,]\d+)?)\b", re.IGNORECASE), 60.0),
    # часы
    (re.compile(r"через\s+(?:час|часа|часов)\s+(\d+(?:[.,]\d+)?)\b", re.IGNORECASE), 3600.0),
    # секунды
    (re.compile(r"через\s+(?:секунду|секунды|секунд|сек)\s+(\d+(?:[.,]\d+)?)\b", re.IGNORECASE), 1.0),
    # дни
    (re.compile(r"через\s+(?:день|дня|дней)\s+(\d+(?:[.,]\d+)?)\b", re.IGNORECASE), 86400.0),
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

# Английские относительные паттерны: "in 30 minutes", "after 2 hours"
_EN_REL_NUM_RE = re.compile(
    rf"\b(?:in|after)\s+(\d+(?:[.,]\d+)?)\s*({_EN_UNITS_RE})\b", re.IGNORECASE)
# «N minutes from now» — время в конце фразы
_EN_REL_NUM_FROMNOW_RE = re.compile(
    rf"\b(\d+(?:[.,]\d+)?)\s*({_EN_UNITS_RE})\s+from\s+now\b", re.IGNORECASE)
# Числительные прописью: "in two hours", "in ten minutes"
_EN_REL_WORD_RE = re.compile(
    rf"\b(?:in|after)\s+({'|'.join(_EN_WORD_NUMBERS)})\s+({_EN_UNITS_RE})\b",
    re.IGNORECASE)

# Словесные формы времени (английские); порядок важен: «half an hour» раньше «an hour»
_EN_WORD_TIME = {
    "half an hour": 1800.0,
    "a half hour": 1800.0,
    "an hour": 3600.0,
    "an hour and a half": 5400.0,
    "a minute": 60.0,
    "a couple of minutes": 120.0,
    "a couple minutes": 120.0,
}


# ─── Абсолютное время ──────────────────────────────────────

# Паттерны: "до 12", "к 12", "в 11:30", "в 11.30", "в 11 30", "в 11", "до полудня",
# "at 11:30", "at 5 pm", "by noon" (англ.)
# am/pm опциональны; разделитель Ч:М — двоеточие, точка или пробел
_ABS_PREPOSITIONS = r"(?:до|к|в|at|by|until|till)"
_AMPM = r"(?:a\.?m\.?|p\.?m\.?)?"
_ABS_HM_RE = re.compile(
    rf"\b{_ABS_PREPOSITIONS}\s+(\d{{1,2}})[:.\s](\d{{2}})\s*({_AMPM})", re.IGNORECASE)
# Только часы, без минут
_ABS_HOUR_RE = re.compile(
    rf"\b{_ABS_PREPOSITIONS}\s+(\d{{1,2}})\s*({_AMPM})\b(?!\s*:\s*\d)", re.IGNORECASE)

_ABS_WORD_TIME = {
    "полдень": 12.0,
    "полудня": 12.0,
    "полудню": 12.0,
    "полуночь": 24.0,
    "полуночи": 24.0,
    "полуночу": 24.0,
}

# Словесные формы абсолютного времени (англ.)
_ABS_WORD_TIME_EN = {
    "noon": 12.0,
    "midnight": 0.0,
}


def _apply_ampm(hour: int, ampm: Optional[str]) -> int:
    """Сдвигает час по am/pm («5 pm» → 17, «12 am» → 0, «12 pm» → 12)."""
    if not ampm:
        return hour
    if re.match(r"p", ampm.strip(". "), re.IGNORECASE):
        return hour + 12 if hour < 12 else hour
    return hour % 12  # am


def _parse_absolute_time(text: str) -> Optional[tuple]:
    """
    Ищет абсолютное время в тексте ("до 12", "в 11:30", "к полудню", "at 5:30 pm").
    Возвращает (target_hour, target_minute, match_obj) или None.
    """
    lower = text.lower()

    # Словесные формы: "до полудня", "к полуночи", "at noon", "by midnight"
    for word, hour in _ABS_WORD_TIME.items():
        pattern = re.compile(rf"\b(?:до|к|в)\s+{re.escape(word)}\b", re.IGNORECASE)
        m = pattern.search(lower)
        if m:
            return (hour, 0, m)
    for word, hour in _ABS_WORD_TIME_EN.items():
        pattern = re.compile(rf"\b{_ABS_PREPOSITIONS}\s+{word}\b", re.IGNORECASE)
        m = pattern.search(lower)
        if m:
            return (hour, 0, m)

    # "до 11:30", "в 12:00", "at 5:30 pm"
    m = _ABS_HM_RE.search(lower)
    if m:
        hour = _apply_ampm(int(m.group(1)), m.group(3))
        minute = int(m.group(2))
        if 0 <= hour <= 24 and 0 <= minute < 60:
            return (float(hour), minute, m)

    # "до 11", "в 12", "at 5 pm"
    m = _ABS_HOUR_RE.search(lower)
    if m:
        hour = _apply_ampm(int(m.group(1)), m.group(2))
        if 0 <= hour <= 24:
            return (float(hour), 0, m)

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
        "remind me to call mom in 30 minutes"
        "remind me at 5 pm"
    """
    lower = text.lower()

    # Маркер просьбы: «напом…» или «remind…»
    if not _has_remind_word(lower):
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

        # Обратный порядок (разговорный): «через минуты 4», «через часа 2»
        if delay_seconds is None:
            for pattern, multiplier in _TIME_UNIT_FIRST_PATTERNS:
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
                        delay_seconds = _RU_WORD_NUMBERS[word_num] * multiplier
                        time_match_obj = match
                        break

    # ── 1b. Относительное время (англ.): "in 30 minutes", "after 2 hours" ──

    if delay_seconds is None:
        m = _EN_REL_NUM_RE.search(lower) or _EN_REL_NUM_FROMNOW_RE.search(lower)
        if m:
            value = float(m.group(1).replace(",", "."))
            delay_seconds = value * _en_unit_multiplier(m.group(2))
            time_match_obj = m

    if delay_seconds is None:
        # Словесные формы: "in half an hour", "in an hour"
        for phrase, secs in _EN_WORD_TIME.items():
            m = re.search(rf"\bin\s+{re.escape(phrase)}\b", lower)
            if m:
                delay_seconds = secs
                time_match_obj = m
                break

    if delay_seconds is None:
        # Числительные прописью: "in two hours", "in ten minutes"
        m = _EN_REL_WORD_RE.search(lower)
        if m:
            word_num = m.group(1).lower()
            if word_num in _EN_WORD_NUMBERS:
                delay_seconds = _EN_WORD_NUMBERS[word_num] * _en_unit_multiplier(m.group(2))
                time_match_obj = m

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
    # Английский вариант: "in 30 minutes remind me to call" → "call"
    after_time = re.sub(r"^remind(?:er|ers)?(?:\s+(?:me|us))?(?:\s+to)?\s*",
                        "", after_time, flags=re.IGNORECASE).strip()
    after_time = re.sub(r"^to\s+", "", after_time, flags=re.IGNORECASE).strip()
    after_time = re.sub(r"^[,:\-\s]+", "", after_time).strip()
    after_time = re.sub(r"[.!?]+$", "", after_time).strip()

    # Если после времени ничего нет — пробуем взять текст ДО
    if not after_time:
        before_time = text[:time_match_obj.start()].strip()
        before_time = re.sub(r"^(?:коннор|жабка|arrodes|connor)[,\s]+", "", before_time, flags=re.IGNORECASE)
        # Убираем все вариации "напомни/напоминание"
        before_time = re.sub(r"\b(?:напомни|напомнить|напоминание|напомните|напомню)\b", "", before_time, flags=re.IGNORECASE).strip()
        # Английские вариации: "remind me to call mom" → "call mom"
        before_time = re.sub(r"\bremind\w*(?:\s+(?:me|us))?(?:\s+to)?\b", "", before_time, flags=re.IGNORECASE).strip()
        before_time = re.sub(r"^(?:please|please,)\s+", "", before_time, flags=re.IGNORECASE).strip()
        before_time = re.sub(r"^(?:can|could|would|will)\s+you\s+", "", before_time, flags=re.IGNORECASE).strip()
        # Убираем "сделай/поставь ... с содержимым: ..." (рус) и "set/make a reminder" (англ)
        before_time = re.sub(r"\b(?:сделай|поставь|создай)\b", "", before_time, flags=re.IGNORECASE).strip()
        before_time = re.sub(r"\b(?:set|make|create)\s+(?:a\s+)?remind\w*\b", "", before_time, flags=re.IGNORECASE).strip()
        before_time = re.sub(r"\b(?:с\s+таким\s+содержимым|с\s+содержимым)\b[:\s]*", "", before_time, flags=re.IGNORECASE).strip()
        before_time = re.sub(r"^(?:мне|мне\s+про|мне\s+о)\b", "", before_time, flags=re.IGNORECASE).strip()
        before_time = re.sub(r"^[,:\-\s]+", "", before_time).strip()
        after_time = before_time

    return (after_time if after_time else None, delay_seconds)


# ─── Перенос напоминания ──────────────────────────────────

_POSTPONE_VERB_RE = re.compile(
    r"\b(перенеси|перенести|перенос|отложи|отложить|сдвинь|сдвинуть|передвинь|передвинуть"
    r"|postpone|reschedule|move|snooze|shift|delay|defer)\b",
    re.IGNORECASE,
)

# «ещё» (рус) / «another» (англ) — сдвиг от прежнего времени срабатывания
_MORE_MARKERS = r"(?:ещ[её]|another)"

# Относительный сдвиг цифрами: «на 5 минут», «ещё на 2 часа», «на ещё 2 часа»,
# «by 5 minutes», «for another 10 minutes»
_POSTPONE_REL_NUM_RE = re.compile(
    rf"({_MORE_MARKERS}\s+)?(?:на|by|for)\s+({_MORE_MARKERS}\s+)?(\d+(?:[.,]\d+)?)\s*"
    rf"(минут[ауы]?|мин|час(?:а|ов)?|секунд[ауы]?|сек|день|дня|дней|{_EN_UNITS_RE})\b",
    re.IGNORECASE,
)
# То же прописью: «на пять минут», «by five minutes»
_POSTPONE_REL_WORD_RE = re.compile(
    rf"({_MORE_MARKERS}\s+)?(?:на|by|for)\s+({_MORE_MARKERS}\s+)?(\w+)\s+"
    rf"(минут[ауы]?|мин|час(?:а|ов)?|секунд[ауы]?|сек|день|дня|дней|{_EN_UNITS_RE})\b",
    re.IGNORECASE,
)
# Абсолютное время: «на 18:30», «на 18.30», «на 18 30», «to 18:30», «at 6.30 pm»
_POSTPONE_ABS_HM_RE = re.compile(
    rf"\b(?:на|в|к|to|at|until|till)\s+(\d{{1,2}})[:.\s](\d{{2}})\s*({_AMPM})",
    re.IGNORECASE)
# Абсолютное, только час: «на 18» (не съедает «на 5 минут» — единицы отсекаются
# относительными паттернами раньше; здесь число не должно продолжаться временем/единицей)
_POSTPONE_ABS_HOUR_RE = re.compile(
    rf"\b(?:на|в|к|to|at)\s+(\d{{1,2}})\s*({_AMPM})\b(?!\s*:\s*\d)"
    rf"(?!\s*(?:минут[ауы]?|мин|час(?:а|ов)?|секунд[ауы]?|сек|день|дня|дней|{_EN_UNITS_RE})\b)",
    re.IGNORECASE,
)
# Слова: «на полдень», «на полночь», «to noon», «at midnight»
_POSTPONE_ABS_WORDS = (
    ("полдень", 12), ("полудня", 12), ("полночь", 0), ("полуночи", 0),
    ("noon", 12), ("midnight", 0),
)

# Слова-единицы без числа: «на полчаса», «на час», «на минуту»,
# «for half an hour», «by an hour»
_POSTPONE_WORD_DELAYS = (
    ("полчаса", 1800.0), ("час", 3600.0), ("минуту", 60.0),
    ("half an hour", 1800.0), ("a half hour", 1800.0),
    ("an hour", 3600.0), ("a minute", 60.0),
)

# Порядковые ответы на «какое напоминание перенести?» (-1 — последнее в списке)
_CHOICE_ORDINALS = {
    "первое": 0, "первый": 0, "первого": 0, "первая": 0, "первую": 0,
    "второе": 1, "второй": 1, "второго": 1, "вторая": 1, "вторую": 1,
    "третье": 2, "третий": 2, "третьего": 2, "третья": 2, "третью": 2,
    "последнее": -1, "последний": -1, "последнего": -1, "последняя": -1, "последнюю": -1,
    "first": 0, "second": 1, "third": 2, "last": -1,
}


def _unit_multiplier(unit: str) -> float:
    u = unit.lower()
    if u.startswith(("мин", "min", "m")):
        return 60.0
    if u.startswith(("час", "h")):
        return 3600.0
    if u.startswith(("сек", "s")):
        return 1.0
    return 86400.0  # день/дня/дней, d/day/days


def parse_postpone(text: str) -> Optional[dict]:
    """
    Запрос на ПЕРЕНОС существующего напоминания:
    «перенеси напоминание на 10 минут», «отложи напоминание ещё на 5 минут»,
    «сдвинь напоминание на 18:30».

    Возвращает dict:
      {"seconds": float, "relative_to_trigger": bool} — сдвиг (relative_to_trigger=True
          при «ещё на ...»: отсчёт от прежнего времени срабатывания, иначе — от сейчас)
      {"abs": (hour, minute)} — перенос на конкретное локальное время
      {"unknown": True} — перенос просят, но время не разобрать
    None — это не запрос переноса.
    """
    lower = text.lower()
    if not _has_remind_word(lower):
        return None
    verb = _POSTPONE_VERB_RE.search(lower)
    # Глагол переноса должен стоять ДО «напоминания» («перенеси напоминание…»),
    # иначе это обычная просьба-напоминание с глаголом в задаче
    # («напомни перенести файлы через час»; англ. «postpone the reminder» /
    # «remind me to move the files»).
    if not verb or verb.start() > _remind_word_pos(lower):
        return None

    def _rel(seconds: float, more: Optional[str]) -> dict:
        if seconds < 10 or seconds > 30 * 86400:
            return {"unknown": True}
        return {"seconds": seconds, "relative_to_trigger": bool(more)}

    # «на полчаса» / «на час» / «на минуту» (и «на ещё час»),
    # «for half an hour» / «by an hour»
    for word, secs in _POSTPONE_WORD_DELAYS:
        m = re.search(
            rf"({_MORE_MARKERS}\s+)?(?:на|by|for)\s+({_MORE_MARKERS}\s+)?{word}\b", lower)
        if m:
            return _rel(secs, m.group(1) or m.group(2))

    # Цифрами: «на 5 минут», «ещё на 2 часа», «на ещё 2 часа», «by 5 minutes»
    m = _POSTPONE_REL_NUM_RE.search(lower)
    if m:
        secs = float(m.group(3).replace(",", ".")) * _unit_multiplier(m.group(4))
        return _rel(secs, m.group(1) or m.group(2))

    # Прописью: «на пять минут», «by five minutes»
    m = _POSTPONE_REL_WORD_RE.search(lower)
    if m and m.group(3).lower() in _ALL_WORD_NUMBERS:
        secs = _ALL_WORD_NUMBERS[m.group(3).lower()] * _unit_multiplier(m.group(4))
        return _rel(secs, m.group(1) or m.group(2))

    # Абсолютное: «на 18:30», «на 18.30», «на 18 30», «to 18:30», «at 6.30 pm»
    m = _POSTPONE_ABS_HM_RE.search(lower)
    if m:
        hour = _apply_ampm(int(m.group(1)), m.group(3))
        minute = int(m.group(2))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return {"abs": (hour, minute)}
        # Невалидно («на 11 75») — проваливаемся в hour-only ниже

    # Абсолютное, только час: «на 18», «в 8», «to 7» (минуты всегда требуют слово
    # «минут», поэтому голое число трактуем как час). Число с единицей
    # («на 5 минут») сюда не доходит — относительные паттерны выше.
    m = _POSTPONE_ABS_HOUR_RE.search(lower)
    if m:
        hour = _apply_ampm(int(m.group(1)), m.group(2))
        if 0 <= hour < 24:
            return {"abs": (hour, 0)}
        return {"unknown": True}

    # Слова: «на полдень», «к полуночи», «to noon», «at midnight»
    for word, hour in _POSTPONE_ABS_WORDS:
        if re.search(rf"(?:на|в|к|to|at|by|until|till)\s+{word}\b", lower):
            return {"abs": (hour, 0)}

    return {"unknown": True}


def extract_postpone_hint(text: str) -> Optional[str]:
    """Подсказка задачи из запроса переноса: «перенеси напоминание про чай на 12 10»
    → «чай». None — подсказки нет (тогда: одно активное — двигаем его,
    несколько — уточняем какое)."""
    t = text.lower()
    t = _POSTPONE_VERB_RE.sub(" ", t)
    t = re.sub(r"\b(?:напом\w*|remind\w*)", " ", t)
    t = _POSTPONE_REL_NUM_RE.sub(" ", t)
    t = _POSTPONE_REL_WORD_RE.sub(" ", t)
    t = _POSTPONE_ABS_HM_RE.sub(" ", t)
    t = _POSTPONE_ABS_HOUR_RE.sub(" ", t)
    for word, _ in _POSTPONE_WORD_DELAYS:
        t = re.sub(rf"(?:{_MORE_MARKERS}\s+)?(?:на|by|for)\s+(?:{_MORE_MARKERS}\s+)?{word}\b", " ", t)
    for word, _ in _POSTPONE_ABS_WORDS:
        t = re.sub(rf"(?:на|в|к|to|at|by|until|till)\s+{word}\b", " ", t)
    t = re.sub(
        r"\b(?:ещ[её]|про|обо?|со?|котор\w*|где|там|это\w*|мо(?:ё|я|й|е|его)|мне|"
        r"пожалуйста|опять|снова|все\s+равно)\b", " ", t)
    t = re.sub(
        r"\b(?:the|a|an|please|again|it|this|that|about|one|to|by|for)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.:;!?—-")
    return t or None


def _task_matches(hint: Optional[str], task: Optional[str]) -> bool:
    """Подсказка совпадает с задачей: подстрокой целиком или любым словом от 3 букв."""
    if not hint or not task:
        return False
    h, t = hint.lower(), task.lower()
    if h in t:
        return True
    return any(len(w) >= 3 and w in t for w in re.split(r"\s+", h))


# ─── Повторяющиеся напоминания (каждый день / каждый день недели) ──────────

_WEEKDAYS = {
    "понедельник": 0, "понедельникам": 0,
    "вторник": 1, "вторникам": 1,
    "среду": 2, "средам": 2, "среда": 2,
    "четверг": 3, "четвергам": 3,
    "пятницу": 4, "пятницам": 4, "пятница": 4,
    "субботу": 5, "субботам": 5, "суббота": 5,
    "воскресенье": 6, "воскресеньям": 6,
    # английские
    "monday": 0, "mondays": 0,
    "tuesday": 1, "tuesdays": 1,
    "wednesday": 2, "wednesdays": 2,
    "thursday": 3, "thursdays": 3,
    "friday": 4, "fridays": 4,
    "saturday": 5, "saturdays": 5,
    "sunday": 6, "sundays": 6,
}
_WEEKDAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]

_RECURRING_DAILY_RE = re.compile(
    r"\b(?:каждый\s+день|ежедневно|every\s+day|each\s+day|daily)\b", re.IGNORECASE)
_RECURRING_WEEKLY_RE = re.compile(
    r"\b(?:каждый\s+(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)"
    r"|по\s+(понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям)"
    r"|(?:every|each)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?"
    r"|on\s+(mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays))\b",
    re.IGNORECASE,
)


def parse_recurring(text: str) -> Optional[tuple]:
    """
    Повторяющееся напоминание: «напоминай каждый день в 12:30»,
    «напоминай каждый понедельник в 18», «по пятницам в 9:00 напоминай»,
    «remind me every day at 12:30», «every friday at 6 pm».

    Возвращает (task, schedule), где
        schedule = {"type": "daily"|"weekly", "hour": int, "minute": int, "weekday": int|None}
    Время обязательно — без него возвращаем None (сработает pending-флоу
    «уточни время», и ответ пользователя пройдёт через этот же парсер).
    Время считается по ЛОКАЛЬНОМУ времени устройства/сервера.
    """
    lower = text.lower()
    if not _has_remind_word(lower):
        return None

    recur_match = _RECURRING_DAILY_RE.search(lower)
    schedule = None
    if recur_match:
        schedule = {"type": "daily", "weekday": None}
    else:
        recur_match = _RECURRING_WEEKLY_RE.search(lower)
        if recur_match:
            wd_word = next(g for g in recur_match.groups() if g).lower()
            schedule = {"type": "weekly", "weekday": _WEEKDAYS[wd_word]}
    if schedule is None:
        return None

    abs_time = _parse_absolute_time(text)
    if not abs_time:
        return None  # время не указано — уточним через pending
    abs_hour, abs_minute, time_match = abs_time
    if abs_hour >= 24:
        return None
    schedule["hour"] = int(abs_hour)
    schedule["minute"] = abs_minute

    # Задача — текст без маркеров повторения, времени и «напомни».
    # Спаны удаляем с КОНЦА строки, чтобы офсеты не съезжали.
    task = text
    for s, e in sorted([recur_match.span(), time_match.span()], reverse=True):
        task = task[:s] + " " + task[e:]
    task = re.sub(r"\b(?:напомни|напоминай|напомнить|напоминание|напомните|напомню|напоминал)\b", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"\bremind\w*(?:\s+(?:me|us))?\b", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"^(?:коннор|жабка|arrodes|connor|арродес)[,\s]+", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"\b(?:мне|мне\s+про|мне\s+о)\b", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"\b(?:me|us)\b", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"^(?:to|please)\s+", " ", task.strip(), flags=re.IGNORECASE)
    task = re.sub(r"\s+", " ", task).strip(" ,.:;!?—-\n")

    return (task if task else None, schedule)


def _next_occurrence(schedule: dict, after: float) -> float:
    """Ближайшее время срабатывания после `after` по локальному времени устройства."""
    base = datetime.fromtimestamp(after)
    target = base.replace(hour=schedule["hour"], minute=schedule["minute"],
                          second=0, microsecond=0)
    if schedule["type"] == "weekly":
        days_ahead = (schedule["weekday"] - base.weekday()) % 7
        target = target + timedelta(days=days_ahead)
    if target.timestamp() <= after:
        target = target + timedelta(days=7 if schedule["type"] == "weekly" else 1)
    return target.timestamp()


def format_schedule(schedule: dict) -> str:
    """Человекочитаемое описание расписания: «every day at 12:30»."""
    hh = f"{schedule['hour']:02d}:{schedule['minute']:02d}"
    if schedule["type"] == "weekly":
        return f"every {_WEEKDAY_NAMES[schedule['weekday']]} at {hh}"
    return f"every day at {hh}"


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
        self._living = None
        self._primitive = False
        self._persona = None
        self._memory = None

        self._reminders: List[dict] = []
        self._load()

        # In-memory состояние /remind без времени: chat_id -> {task, asked_at}
        # (пережидает до ответа пользователя, теряется на рестарте — это ок)
        self._pending_remind: Dict[str, dict] = {}

        # Проверка заморозки персоны (callable → bool), подключается извне;
        # None — заморозки нет
        self._muted_check = None

    def set_sender(self, sender):
        self._sender = sender

    def set_memory(self, memory):
        """Передаёт MemoryManager — сработавшие напоминания логируются в STM."""
        self._memory = memory

    def set_muted_check(self, check):
        """Передаёт callable () -> bool: заморожена ли персона (features.muted)."""
        self._muted_check = check

    def set_router_persona(self, router, persona):
        """Передаёт router и persona для генерации текста напоминания через LLM."""
        self._router = router
        self._persona = persona

    def set_living(self, living):
        """Передаёт LivingPersona — текущий mood/energy попадают в текст
        напоминания (план «живой» персоны, §7)."""
        self._living = living

    def set_intellect_tier(self, tier):
        """Уровень интеллекта (§3.5 плана уровней): primitive — минимальная
        вербализация напоминаний, почти шаблонная, без характерного текста."""
        self._primitive = bool(tier == "primitive")

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
        """Атомарная запись: пишем во временный файл, затем переименовываем.
        Защищает от порчи файла (0 байт / битый JSON) при аварийном завершении процесса
        в момент записи."""
        try:
            import os, tempfile
            data = json.dumps(self._reminders, ensure_ascii=False, indent=2)
            fd, tmp_path = tempfile.mkstemp(dir=str(self._base_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp_path, self._file)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(f"[Reminder] Не удалось сохранить: {e}")

    # ── API ──

    def add_reminder(self, chat_id: str, user_name: str, task: Optional[str],
                     delay_seconds: float, topic_id: Optional[int] = None,
                     schedule: Optional[dict] = None,
                     user_id: Optional[str] = None,
                     username: Optional[str] = None) -> dict:
        """Создаёт напоминание. Возвращает словарь с информацией.

        schedule (из parse_recurring) — повторяющееся напоминание:
        trigger_at считается от ближайшего времени по расписанию,
        после срабатывания перепланируется автоматически.
        user_id/username — кто попросил: при срабатывании бот тегает
        (@username) или называет по имени.
        """
        now = time.time()
        reminder = {
            "chat_id": str(chat_id),
            "user_name": user_name,
            "user_id": str(user_id) if user_id else None,
            "username": username or "",
            "task": task,
            "created_at": now,
            "trigger_at": (_next_occurrence(schedule, now) if schedule
                           else now + delay_seconds),
            "topic_id": topic_id,
            "fired": False,
        }
        if schedule:
            reminder["recurrence"] = schedule
        with self._lock:
            self._reminders.append(reminder)
            self._save()
        logger.info(f"[Reminder] Добавлено: chat={chat_id} task='{task}' "
                    + (format_schedule(schedule) if schedule else f"через {delay_seconds:.0f}с"))
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

    def postpone_reminder(self, chat_id: str, seconds: Optional[float] = None,
                          abs_time: Optional[tuple] = None,
                          relative_to_trigger: bool = False,
                          task_hint: Optional[str] = None) -> Optional[dict]:
        """Переносит напоминание чата.

        seconds — сдвиг: от текущего момента, либо от прежнего времени
        срабатывания (relative_to_trigger=True — «ещё на 5 минут»).
        abs_time=(hour, minute) — перенос на конкретное локальное время.
        task_hint — подсказка задачи («про чай» → «чай») из extract_postpone_hint.

        Выбор цели: с подсказкой — ближайшее из совпавших (нет совпадений —
        {"not_found": ...}, ничего не двигаем); без подсказки — единственное
        активное, а если их несколько — {"ambiguous": ..., "choices": [...]}
        (НЕ угадываем: раньше молча двигалось ближайшее, а бот словами
        «подтверждал» перенос другого).

        Если активных нет, но за последние 24ч есть сработавшее — создаёт
        НОВОЕ напоминание с той же задачей (результат с recreated=True).

        Возвращает {"task", "trigger_at", "recreated"} | {"ambiguous"/"not_found"} |
        None — переносить нечего.
        """
        now = time.time()
        if abs_time:
            delay = _absolute_to_delay(abs_time[0], abs_time[1])
            if delay is None:
                return None

        with self._lock:
            active = [
                r for r in self._reminders
                if r["chat_id"] == str(chat_id) and not r.get("fired") and r["trigger_at"] > now
            ]

        def _choices(items) -> list:
            return [
                {"task": r.get("task"), "trigger_at": r["trigger_at"]}
                for r in sorted(items, key=lambda x: x["trigger_at"])
            ]

        if active:
            candidates = active
            if task_hint:
                matched = [r for r in active if _task_matches(task_hint, r.get("task"))]
                if not matched:
                    logger.info(f"[Reminder] Перенос: нет совпадений с '{task_hint}' "
                                f"(активных: {len(active)})")
                    return {"not_found": True, "hint": task_hint, "choices": _choices(active)}
                candidates = matched
            elif len(active) > 1:
                logger.info(f"[Reminder] Перенос без подсказки при {len(active)} активных — уточняем")
                return {"ambiguous": True, "choices": _choices(active)}
            target = min(candidates, key=lambda r: r["trigger_at"])
            if abs_time:
                new_trigger = now + delay
            else:
                base = target["trigger_at"] if relative_to_trigger else now
                new_trigger = base + seconds
            with self._lock:
                target["trigger_at"] = new_trigger
                self._save()
            logger.info(f"[Reminder] Перенесено: chat={chat_id} task='{target.get('task')}' "
                        f"на {datetime.fromtimestamp(new_trigger).strftime('%d.%m %H:%M')}")
            return {"task": target.get("task"), "trigger_at": new_trigger, "recreated": False}

        # Активных нет — возможно, переносят уже сработавшее: пересоздаём с той же задачей
        cutoff = now - 86400
        with self._lock:
            recent_fired = [
                r for r in self._reminders
                if r["chat_id"] == str(chat_id) and r.get("fired")
                and not r.get("recurrence") and r["trigger_at"] >= cutoff
            ]
        if task_hint:
            recent_fired = [r for r in recent_fired if _task_matches(task_hint, r.get("task"))]
        if not recent_fired:
            if task_hint:
                return {"not_found": True, "hint": task_hint, "choices": []}
            return None
        src = max(recent_fired, key=lambda r: r["trigger_at"])
        new_delay = delay if abs_time else seconds
        new_r = self.add_reminder(
            chat_id, src.get("user_name") or "User", src.get("task"), new_delay,
            src.get("topic_id"), user_id=src.get("user_id"), username=src.get("username"),
        )
        logger.info(f"[Reminder] Пересоздано при переносе: chat={chat_id} task='{new_r.get('task')}'")
        return {"task": new_r.get("task"), "trigger_at": new_r["trigger_at"], "recreated": True}

    # ── pending /remind без времени (in-memory) ──

    def begin_pending_remind(self, chat_id: str, task: str):
        """Запоминает задачу напоминания, ждём от пользователя ответа про время.
        asked_at нужен, чтобы при одновременно висящем вопросе обучения «как часто?»
        отдать ответ о периодичности тому, кто спросил ПОЗЖЕ (см. process_message)."""
        with self._lock:
            self._pending_remind[str(chat_id)] = {"task": task, "asked_at": time.time()}

    def get_pending_remind(self, chat_id: str) -> Optional[str]:
        """Текст задачи pending-напоминания (или None)."""
        with self._lock:
            entry = self._pending_remind.get(str(chat_id))
            # Совместимость со старым форматом (голая строка)
            return entry.get("task") if isinstance(entry, dict) else entry

    def get_pending_remind_asked_at(self, chat_id: str) -> Optional[float]:
        """Когда был задан вопрос «через сколько напомнить?» (timestamp или None)."""
        with self._lock:
            entry = self._pending_remind.get(str(chat_id))
            return entry.get("asked_at") if isinstance(entry, dict) else None

    def clear_pending_remind(self, chat_id: str):
        with self._lock:
            self._pending_remind.pop(str(chat_id), None)

    def begin_pending_postpone(self, chat_id: str):
        """Ждём ответа «на когда перенести?» (перенос без указания времени)."""
        with self._lock:
            self._pending_remind[str(chat_id)] = {
                "task": None, "postpone": True, "asked_at": time.time(),
            }

    def get_pending_postpone(self, chat_id: str) -> bool:
        """Висит ли вопрос «на когда перенести напоминание?»."""
        with self._lock:
            entry = self._pending_remind.get(str(chat_id))
            return bool(isinstance(entry, dict) and entry.get("postpone"))

    def begin_pending_postpone_choice(self, chat_id: str, seconds: Optional[float] = None,
                                      abs_time: Optional[tuple] = None,
                                      relative_to_trigger: bool = False):
        """Несколько активных напоминаний и подсказки нет — ждём ответа
        «какое именно перенести?». Сдвиг запоминаем, применим к выбранному."""
        with self._lock:
            self._pending_remind[str(chat_id)] = {
                "task": None, "postpone_choice": True,
                "seconds": seconds, "abs": abs_time,
                "rel": relative_to_trigger, "asked_at": time.time(),
            }

    def get_pending_postpone_choice(self, chat_id: str) -> bool:
        """Висит ли вопрос «какое напоминание перенести?»."""
        with self._lock:
            entry = self._pending_remind.get(str(chat_id))
            return bool(isinstance(entry, dict) and entry.get("postpone_choice"))

    def resolve_postpone_choice(self, chat_id: str, reply: str) -> Optional[dict]:
        """Применяет отложенный сдвиг к напоминанию, выбранному в ответе:
        номером из списка («1», «2.»), порядковым («первое», «последнее»)
        или словами из задачи («чай»). Список — по близости срабатывания,
        в том же порядке, в каком его показал бот.

        Возвращает {"task", "trigger_at", "recreated": False} | {"gone": True}
        (активных больше нет) | None (ответ не распознан — переспросить).
        """
        with self._lock:
            entry = self._pending_remind.get(str(chat_id))
        if not (isinstance(entry, dict) and entry.get("postpone_choice")):
            return None

        now = time.time()
        with self._lock:
            active = sorted(
                (r for r in self._reminders
                 if r["chat_id"] == str(chat_id) and not r.get("fired") and r["trigger_at"] > now),
                key=lambda r: r["trigger_at"],
            )
        if not active:
            self.clear_pending_remind(chat_id)
            return {"gone": True}

        text = reply.strip().lower()
        target = None
        m = re.fullmatch(r"(\d{1,2})[.)]?", text)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(active):
                target = active[idx]
        if target is None:
            for word, idx in _CHOICE_ORDINALS.items():
                if re.search(rf"\b{word}\b", text):
                    target = active[idx] if idx < len(active) else None
                    break
        if target is None:
            matched = [r for r in active if _task_matches(text, r.get("task"))]
            if len(matched) == 1:
                target = matched[0]
        if target is None:
            return None

        abs_time = entry.get("abs")
        if abs_time:
            delay = _absolute_to_delay(abs_time[0], abs_time[1])
            if delay is None:
                self.clear_pending_remind(chat_id)
                return {"gone": True}
            new_trigger = now + delay
        else:
            base = target["trigger_at"] if entry.get("rel") else now
            new_trigger = base + entry["seconds"]
        with self._lock:
            target["trigger_at"] = new_trigger
            self._save()
            self._pending_remind.pop(str(chat_id), None)
        logger.info(f"[Reminder] Перенесено по выбору: chat={chat_id} "
                    f"task='{target.get('task')}' "
                    f"на {datetime.fromtimestamp(new_trigger).strftime('%d.%m %H:%M')}")
        return {"task": target.get("task"), "trigger_at": new_trigger, "recreated": False}

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
        # Замороженная персона молчит: напоминание «сгорает» без отправки
        # (True — как доставленное: одноразовое гаснет, повторяющееся переносится)
        if self._muted_check and self._muted_check():
            logger.info(f"[Reminder] Персона заморожена — напоминание пропущено: {reminder.get('task')}")
            return True
        chat_id = reminder["chat_id"]
        user_name = reminder.get("user_name", "")
        task = reminder.get("task")
        topic_id = reminder.get("topic_id")

        text = None

        # Язык напоминания: по тексту задачи, иначе — по последним сообщениям
        # чата (LLM переписку пользователя при генерации НЕ видит, поэтому
        # раньше отвечала на языке английской служебной обёртки промпта)
        lang = self._reminder_lang(chat_id, task)

        # Пытаемся сгенерировать через LLM в характере персоны.
        # primitive (§3.5): характерного текста нет — почти шаблонная
        # минимальная вербализация, LLM-генерация пропускается
        if self._router and self._persona and not self._primitive:
            try:
                text = await asyncio.to_thread(
                    self._generate_reminder_text, user_name, task, lang, chat_id)
            except Exception as e:
                logger.warning(f"[Reminder] LLM генерация не удалась: {e}")

        # Fallback — статический шаблон
        if not text:
            if lang == "Russian":
                text = f"напоминаю: {task}" if task else "время пришло! Ты просил напомнить."
            else:
                text = f"reminder: {task}" if task else "time's up! You asked for a reminder."

        # Кто попросил: тегаем через @username, иначе просто называем по имени.
        # LLM-текст обычно уже обращается по имени — не дублируем приставку.
        prefix = None
        if reminder.get("username"):
            prefix = f"@{reminder['username']}"
        elif user_name:
            prefix = user_name
        if prefix and not text.lstrip().lower().startswith(prefix.lstrip("@").lower()):
            text = f"{prefix}, {text}"

        try:
            ok = await self._sender.send_message(chat_id, text, topic_id=topic_id)
            if ok:
                logger.info(f"[Reminder] Отправлено в чат {chat_id}: {text[:60]}")
                # Логируем в STM, чтобы в буфере и в чате картина была одна
                # (роль assistant — LTM-экстракция на неё не срабатывает)
                if self._memory:
                    try:
                        await asyncio.to_thread(
                            self._memory.add_message, "assistant", text,
                            user_id=chat_id, chat_id=chat_id,
                        )
                    except Exception as e:
                        logger.warning(f"[Reminder] Не удалось записать напоминание в STM: {e}")
            else:
                logger.error(f"[Reminder] Отправка в {chat_id} вернула False")
            return bool(ok)
        except Exception as e:
            logger.error(f"[Reminder] Ошибка отправки в {chat_id}: {e}")
            return False

    def _reminder_lang(self, chat_id: str, task: Optional[str]) -> str:
        """Язык напоминания: сначала текст задачи (он продиктован пользователем),
        иначе — последние сообщения пользователя из чата (общий детектор
        app.core.language: реплики ассистента и синтетика не считаются).
        Ничего не определено — русский (как в rhythm)."""
        lang = detect_language(task or "")
        if not lang and self._memory:
            try:
                lang = detect_dialogue_language(
                    "", self._memory.stm.get_last(8, chat_id=chat_id))
            except Exception:
                lang = None
        return language_name(lang) or "Russian"

    def _generate_reminder_text(self, user_name: str, task: Optional[str],
                                lang: str = "English", chat_id: str = None) -> Optional[str]:
        """Генерирует текст напоминания через LLM в характере персоны. Синхронный вызов."""
        assert self._router and self._persona  # проверяется в _fire перед вызовом
        persona_prompt = self._persona.system_prompt.strip()
        # Текущее mood/energy персоны (§7): лёгкий фоновый контекст, не директива
        living_block = ""
        if self._living and chat_id:
            try:
                state_ctx = self._living.get_living_context(chat_id)
                if state_ctx:
                    living_block = f"\n\n{state_ctx}"
            except Exception:
                pass
        if task:
            user_content = (
                f"Remind {user_name}: {task}" if lang == "English"
                else f"Напомни {user_name}: {task}"
            )
        else:
            user_content = (
                f"Remind {user_name} — they asked for a reminder but didn't specify what for."
                if lang == "English"
                else f"Напомни {user_name} — пользователь просил напомнить, но не уточнил о чём."
            )

        messages = [
            {"role": "system", "content": (
                f"{persona_prompt}\n\n"
                "---\n"
                "You are reminding the user about something at their request. "
                "Write a short reminder (1-2 sentences) in your character. "
                "Be sure to mention the essence of the task. "
                f"Write the reminder in {lang}. "
                "Do NOT use markdown. Do NOT write meta-notes."
                f"{living_block}"
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
                with self._lock:
                    due = [r for r in self._reminders
                           if not r.get("fired") and r["trigger_at"] <= now]

                # fired ставим только ПОСЛЕ успешной отправки — иначе при сбое
                # (падение процесса, ошибка сети) напоминание терялось без retry
                changed = False
                for r in due:
                    success = await self._fire(r)
                    with self._lock:
                        if success:
                            if r.get("recurrence"):
                                # Повторяющееся: не гасим, переносим на следующее время
                                r["trigger_at"] = _next_occurrence(r["recurrence"], time.time())
                                r["attempts"] = 0
                            else:
                                r["fired"] = True
                        else:
                            r["attempts"] = r.get("attempts", 0) + 1
                            if r["attempts"] >= 3:
                                if r.get("recurrence"):
                                    # Пропускаем этот раз (сбой сети/процесса),
                                    # переносим на следующее время расписания
                                    r["trigger_at"] = _next_occurrence(r["recurrence"], time.time())
                                    r["attempts"] = 0
                                    logger.warning(f"[Reminder] Пропущено после 3 попыток, перенесено: {r.get('task')}")
                                else:
                                    r["fired"] = True  # сдаёмся после 3 попыток
                                    logger.error(f"[Reminder] Не доставлено после 3 попыток: {r.get('task')}")
                        changed = True
                if changed:
                    with self._lock:
                        self._save()

                # Периодически чистим старые
                cleanup_counter += 1
                if cleanup_counter >= 60:  # ~раз в 30 минут
                    cleanup_counter = 0
                    self._cleanup_fired()

            except Exception as e:
                logger.error(f"[Reminder] Ошибка в цикле: {e}")

            await asyncio.sleep(30)

    def start(self, loop=None):
        """Запускает фоновую задачу. Идемпотентна: повторный вызов (например,
        живое включение фичи поверх уже запущенного цикла) — no-op."""
        if self._running:
            return
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
            return f"{int(delay_seconds)} sec"
        if delay_seconds < 3600:
            return f"{int(delay_seconds / 60)} min"
        hours = delay_seconds / 3600
        if hours < 24:
            h = int(hours)
            m = int((delay_seconds - h * 3600) / 60)
            return f"{h} h {m} min" if m else f"{h} h"
        days = int(delay_seconds / 86400)
        return f"{days} d"

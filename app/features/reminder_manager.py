"""
Менеджер напоминаний.
Пользователь просит напомнить через N времени — бот пишет через N минут.
Хранит напоминания в data/{context}/reminders.json.
Фоновый цикл каждые 30с проверяет наступившие и шлёт через sender.

У каждого напоминания есть стабильный id («r» + hex): список показывает его,
отмена идёт по нему (см. parse_reminder_ref / cancel_by_ref) — номер строки
списка указывает не на то, если список изменился между показом и командой.

Время — часы ПОЛЬЗОВАТЕЛЯ: единый источник app.core.timeutil (пояс TIMEZONE),
epoch ↔ стенные часы только через timeutil.to_ts/from_ts.
"""

import asyncio
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict

from app.core import timeutil
from app.core.paths import data_dir
from app.core.atomic_io import atomic_write_json, load_json_safe
from app.core.language import (detect_language, detect_dialogue_language, language_name,
                               persona_language, user_language_line)

logger = logging.getLogger(__name__)

# Итоги _fire помимо True/False (app/core/turn_gate.py): идёт ход
# пользователя — напоминание не теряется, цикл повторит его следующим тиком
# без траты попытки доставки; отменено/перенесено в том самом ходе, которого
# ждали, — не доставляется, запись напоминания цикл не трогает
_DEFERRED = "deferred"
_CANCELLED = "cancelled"

# Имена бота для обрезки обращения в начале фразы («коннор, напомни…»).
# Если персона не передала свои trigger_words — используем этот список
# (текущее поведение до введения персон-специфичных имён).
_DEFAULT_TRIGGER_NAMES = ("коннор", "жабка", "arrodes", "connor", "арродес")


def _trigger_names_alt(trigger_words: Optional[List[str]]) -> str:
    # Имена для regex-альтернации |, экранированные под re.
    names = trigger_words if trigger_words else _DEFAULT_TRIGGER_NAMES
    return "|".join(re.escape(w) for w in names)


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
    # Маркер просьбы о напоминании: «напом…» (рус) или «remind…» (англ).
    return "напом" in lower or re.search(r"\bremind", lower) is not None


def _remind_word_pos(lower: str) -> int:
    # Позиция первого вхождения «напом»/«remind» (-1 — нет).
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

# Единая таблица словесных «круглых» времён — источник для _ABS_WORD_TIME,
# _ABS_WORD_TIME_EN и _POSTPONE_ABS_WORDS: одно место, где полночь = 0
# (не 24), чтобы RU/EN-варианты и все потребители (_absolute_to_delay/
# _next_occurrence/postpone) не расходились в трактовке (см. _normalize_hour).
_ROUND_TIME_WORDS = {
    "полдень": 12, "полудня": 12, "полудню": 12,
    "полночь": 0, "полуночи": 0,
}
_ROUND_TIME_WORDS_EN = {
    "noon": 12,
    "midnight": 0,
}
_ABS_WORD_TIME = {w: float(h) for w, h in _ROUND_TIME_WORDS.items()}

# Словесные формы абсолютного времени (англ.)
_ABS_WORD_TIME_EN = {w: float(h) for w, h in _ROUND_TIME_WORDS_EN.items()}


def _normalize_hour(hour: float) -> int:
    """Час к диапазону [0, 24): «полночь»/24:00 и 0:00 — один и тот же час,
    единый нормализатор, чтобы _absolute_to_delay/_delay_until/parse_recurring
    не расходились в трактовке «конца суток» каждый по-своему."""
    return int(hour) % 24


def _apply_ampm(hour: int, ampm: Optional[str]) -> int:
    # Сдвигает час по am/pm («5 pm» → 17, «12 am» → 0, «12 pm» → 12).
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
    """Вычисляет задержку от текущего времени до указанного. В секундах.
    «Текущее» — время пользователя (app.core.timeutil, пояс TIMEZONE), а не
    системный пояс процесса: «напомни в 9» — это 9 утра у человека."""
    now = timeutil.now()
    target_hour = _normalize_hour(hour)
    target_minute = minute

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


# ─── День и часть суток ────────────────────────────────────
# «завтра», «послезавтра», «tomorrow», «утром», «в 8 вечера»,
# «tomorrow morning at 8» — частые ответы на уточняющий «когда напомнить?».

# День-офсет: послезавтра → +2, завтра → +1, сегодня → 0 (последнее — чтобы
# слово «сегодня» вырезалось из текста задачи, а не слипалось с ней)
_DAY_OFFSET_RES = [
    (re.compile(r"\bпослезавтра\b|\bпосле\s+завтра\b|\bday\s+after\s+tomorrow\b", re.IGNORECASE), 2),
    (re.compile(r"\bзавтра\b|\btomorrow\b", re.IGNORECASE), 1),
    (re.compile(r"\bсегодня\b|\btoday\b", re.IGNORECASE), 0),
]

# Standalone части суток → вид (для часа по умолчанию и поправки явного)
_DAYPART_RES = [
    (re.compile(r"\bутром\b", re.IGNORECASE), "am"),
    (re.compile(r"\bднём\b", re.IGNORECASE), "day"),
    (re.compile(r"\bвечером\b", re.IGNORECASE), "evening"),
    (re.compile(r"\bночью\b", re.IGNORECASE), "night"),
    (re.compile(r"\b(?:in\s+the\s+|at\s+)?morning\b", re.IGNORECASE), "am"),
    (re.compile(r"\b(?:in\s+the\s+|at\s+)?afternoon\b", re.IGNORECASE), "day"),
    (re.compile(r"\b(?:in\s+the\s+|at\s+)?evening\b", re.IGNORECASE), "evening"),
    (re.compile(r"\b(?:at\s+)?night\b", re.IGNORECASE), "night"),
    (re.compile(r"\btonight\b", re.IGNORECASE), "night"),
]

# Час + родительный падеж части суток: «в 8 вечера», «8 утра», «в 8:30 вечера».
# У часовой формы — lookbehind: «8:30 вечера» не должно матчиться как «30 вечера».
_HOUR_MIN_DAYPART_RE = re.compile(
    r"\b(\d{1,2})[:.](\d{2})\s*(утра|дня|вечера|ночи)\b", re.IGNORECASE)
_HOUR_DAYPART_RE = re.compile(
    r"(?<![:.\d])\b(\d{1,2})\s*(утра|дня|вечера|ночи)\b", re.IGNORECASE)

_DAYPART_DEFAULT_HOUR = {"am": 9, "day": 15, "evening": 19, "night": 23}
_HOUR_GEN_TO_KIND = {"утра": "am", "дня": "day", "вечера": "evening", "ночи": "night"}

# Приветствия перед частью суток — не время напоминания («good morning, remind me…»)
_DAYPART_GREETING_PREFIXES = ("good ", "добр")


def _adjust_hour_by_daypart(hour: float, kind: str) -> float:
    """Уточняет явный час по части суток: «8 вечера» → 20, «2 дня» → 14,
    «12 ночи» → 0, «8 утра» → 8."""
    h = _normalize_hour(hour)
    if kind in ("day", "evening"):
        return float(h + 12) if h < 12 else float(h)
    if kind == "night":
        if h == 12:
            return 0.0
        if 6 <= h < 12:
            return float(h + 12)
    return float(h)  # am/утром: без изменений


def _remove_spans(text: str, spans) -> str:
    """Вырезает совпадения из текста — чтобы извлечь задачу вокруг вставок
    времени («напомни завтра купить шоколад в 8» → «напомни купить шоколад»)."""
    out = []
    prev = 0
    for s, e in sorted(spans):
        if s < prev:
            s = prev
        out.append(text[prev:s])
        prev = max(prev, e)
    out.append(text[prev:])
    return " ".join("".join(out).split())


def _clean_task_fragment(fragment: str, trigger_words: Optional[List[str]] = None) -> str:
    """Чистит остаток текста после вырезания времени: убирает «напомни/remind me»,
    обращения, разделители — остаётся текст задачи."""
    s = fragment.strip()
    s = re.sub(rf"^(?:{_trigger_names_alt(trigger_words)})[,\s]+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(?:напомни|напомнить|напоминание|напомните|напомню)\b", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\bremind\w*(?:\s+(?:me|us))?(?:\s+to)?\b", "", s, flags=re.IGNORECASE).strip()
    # «напомни мне завтра в 8 купить хлеб»: «завтра в 8» вырезаются спанами
    # до вызова этой функции, «напомни» — строкой выше; дативное «мне»
    # («нам/ему/ей/им») перед задачей убираем отдельно (см. аналогичную
    # чистку в parse_reminder для ветки без дня/части суток).
    s = re.sub(r"^(?:мне|нам|ему|ей|им)\b\s*", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^(?:please)\s+", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^to\s+", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^[,:\-\s]+", "", s).strip()
    s = re.sub(r"[,:\-\s]+$", "", s).strip()
    s = re.sub(r"[.!?]+$", "", s).strip()
    return s


def _delay_until(day_offset: int, hour: float, minute: int) -> Optional[float]:
    """Секунды до (сегодня + day_offset дней) в hour:minute.
    offset=0 — как _absolute_to_delay: на завтра, если время сегодня уже прошло."""
    if day_offset <= 0:
        return _absolute_to_delay(hour, minute)
    now = timeutil.now()
    target = datetime(now.year, now.month, now.day) + timedelta(
        days=day_offset, hours=_normalize_hour(hour), minutes=minute)
    delay = (target - now).total_seconds()
    if delay < 10 or delay > 30 * 86400:
        return None
    return float(delay)


def _parse_day_daypart(text: str) -> Optional[tuple]:
    """Разбирает день и/или часть суток с явным временем или без:
    «завтра», «утром», «завтра в 8», «в 8 вечера», «tomorrow morning at 8».

    Возвращает (delay_seconds, spans) — спаны всех совпадений, чтобы
    вырезать их из текста задачи, — или None."""
    lower = text.lower()
    spans = []
    day_offset = None
    for pattern, off in _DAY_OFFSET_RES:
        m = pattern.search(lower)
        if m:
            day_offset = off
            spans.append((m.start(), m.end()))
            break

    kind = None
    explicit_hour = None
    explicit_minute = 0

    # Час + родительный падеж («в 8 вечера», «8 утра»)
    hg = _HOUR_MIN_DAYPART_RE.search(lower)
    if hg:
        explicit_hour = float(int(hg.group(1)))
        explicit_minute = int(hg.group(2))
        kind = _HOUR_GEN_TO_KIND[hg.group(3).lower()]
        spans.append((hg.start(), hg.end()))
    else:
        hg = _HOUR_DAYPART_RE.search(lower)
        if hg:
            explicit_hour = float(int(hg.group(1)))
            kind = _HOUR_GEN_TO_KIND[hg.group(2).lower()]
            spans.append((hg.start(), hg.end()))

    # Standalone часть суток («утром», «in the morning», «at night»)
    if kind is None:
        for pattern, k in _DAYPART_RES:
            m = pattern.search(lower)
            if m:
                # «good morning» / «добрым утром» — приветствие, не время
                prefix = lower[max(0, m.start() - 8):m.start()]
                if any(g in prefix for g in _DAYPART_GREETING_PREFIXES):
                    continue
                kind = k
                spans.append((m.start(), m.end()))
                break

    if day_offset is None and kind is None:
        return None

    # Явное время («завтра в 8», «tomorrow morning at 8:30»)
    if explicit_hour is None:
        abs_time = _parse_absolute_time(text)
        if abs_time:
            explicit_hour, explicit_minute, abs_match = abs_time
            spans.append((abs_match.start(), abs_match.end()))

    if explicit_hour is not None and kind is not None:
        hour = _adjust_hour_by_daypart(explicit_hour, kind)
    elif explicit_hour is not None:
        hour = float(int(explicit_hour) % 24)
    elif kind is not None:
        hour = float(_DAYPART_DEFAULT_HOUR[kind])
    else:
        hour = 9.0  # «завтра» без уточнений — утро
    minute = explicit_minute if explicit_hour is not None else 0

    delay = _delay_until(day_offset or 0, hour, minute)
    if delay is None:
        return None
    return (delay, spans)


def parse_reminder(text: str, trigger_words: Optional[List[str]] = None) -> Optional[tuple]:
    """
    Пытается распарсить запрос на напоминание.
    Возвращает (task, delay_seconds) или None.
    trigger_words — имена персоны для обрезки обращения в начале фразы.

    Примеры:
        "напомни мне через 30 минут позвонить маме"
        "напомни через 2 часа сделать домашку"
        "через 10 мин напомни"
        "напомни завтра утром купить шоколад"
        "напомни в 8 вечера"
        "remind me to call mom in 30 minutes"
        "remind me at 5 pm"
        "remind me tomorrow morning"
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

    # ── 1c. День и часть суток: «завтра», «утром», «в 8 вечера»,
    # «tomorrow morning at 8» — своя ветка, т.к. день+время нужно
    # склеить в одно совпадение и вырезать из задачи оба куска.
    # Идёт ДО общего абсолютного разбора: иначе «завтра в 8» съедалось бы
    # как «в 8», а «завтра» оставалось бы в тексте задачи.

    if delay_seconds is None:
        day_parsed = _parse_day_daypart(text)
        if day_parsed:
            delay_seconds, spans = day_parsed
            task = _clean_task_fragment(_remove_spans(text, spans), trigger_words)
            return (task if task else None, delay_seconds)

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
        before_time = re.sub(rf"^(?:{_trigger_names_alt(trigger_words)})[,\s]+", "", before_time, flags=re.IGNORECASE)
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
# Слова: «на полдень», «на полночь», «to noon», «at midnight» — из единой
# таблицы _ROUND_TIME_WORDS(_EN) (см. её комментарий выше).
_POSTPONE_ABS_WORDS = tuple(_ROUND_TIME_WORDS.items()) + tuple(_ROUND_TIME_WORDS_EN.items())

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
        # seconds распознан и валиден, но ниже разумного минимума
        # («перенеси напоминание на 5 секунд») — округляем вверх до минимума,
        # а не считаем «не разобрано»: иначе бот переспрашивал бы время,
        # которое пользователь уже назвал.
        if seconds <= 0 or seconds > 30 * 86400:
            return {"unknown": True}
        return {"seconds": max(seconds, 10.0), "relative_to_trigger": bool(more)}

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
    # Подсказка совпадает с задачей: подстрокой целиком или любым словом от 3 букв.
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


def parse_recurring(text: str, trigger_words: Optional[List[str]] = None) -> Optional[tuple]:
    """
    Повторяющееся напоминание: «напоминай каждый день в 12:30»,
    «напоминай каждый понедельник в 18», «по пятницам в 9:00 напоминай»,
    «remind me every day at 12:30», «every friday at 6 pm».
    trigger_words — имена персоны для обрезки обращения в начале фразы.

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
    # 24:00 и 0:00 — один и тот же момент, поэтому час нормализуем, а не
    # отбрасываем запрос («каждый день в 24:00», «к полуночи»).
    schedule["hour"] = _normalize_hour(abs_hour)
    schedule["minute"] = abs_minute

    # Задача — текст без маркеров повторения, времени и «напомни».
    # Спаны удаляем с КОНЦА строки, чтобы офсеты не съезжали.
    task = text
    for s, e in sorted([recur_match.span(), time_match.span()], reverse=True):
        task = task[:s] + " " + task[e:]
    task = re.sub(r"\b(?:напомни|напоминай|напомнить|напоминание|напомните|напомню|напоминал)\b", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"\bremind\w*(?:\s+(?:me|us))?\b", " ", task, flags=re.IGNORECASE)
    task = re.sub(rf"^(?:{_trigger_names_alt(trigger_words)})[,\s]+", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"\b(?:мне|мне\s+про|мне\s+о)\b", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"\b(?:me|us)\b", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"^(?:to|please)\s+", " ", task.strip(), flags=re.IGNORECASE)
    task = re.sub(r"\s+", " ", task).strip(" ,.:;!?—-\n")

    return (task if task else None, schedule)


def _next_occurrence(schedule: dict, after: float) -> float:
    """Ближайшее время срабатывания после `after` по времени пользователя
    (пояс из app.core.timeutil; epoch ↔ стенные часы — только через
    from_ts/to_ts, у naive-datetime .timestamp() дал бы системный пояс)."""
    base = timeutil.from_ts(after)
    target = base.replace(hour=schedule["hour"], minute=schedule["minute"],
                          second=0, microsecond=0)
    if schedule["type"] == "weekly":
        days_ahead = (schedule["weekday"] - base.weekday()) % 7
        target = target + timedelta(days=days_ahead)
    if timeutil.to_ts(target) <= after:
        target = target + timedelta(days=7 if schedule["type"] == "weekly" else 1)
    return timeutil.to_ts(target)


def format_schedule(schedule: dict) -> str:
    # Человекочитаемое описание расписания: «every day at 12:30».
    hh = f"{schedule['hour']:02d}:{schedule['minute']:02d}"
    if schedule["type"] == "weekly":
        return f"every {_WEEKDAY_NAMES[schedule['weekday']]} at {hh}"
    return f"every day at {hh}"


# ─── Стабильный id напоминания ────────────────────────────
# Номер в списке get_active нестабилен: список пересчитывается на каждый
# показ, и между «/reminders» и «/cancel_reminder 2» порядок может
# измениться (одно сработало и выпало, добавилось новое, перенос сдвинул
# порядок по trigger_at) — тот же номер укажет уже на другую запись. Поэтому
# у каждого напоминания есть свой id: рождается вместе с записью, хранится
# в файле рядом с ней и показывается в списке.
#
# Формат — «r» + 5 hex. Буква в начале обязательна, иначе «/cancel_reminder 3»
# было бы неоднозначно: и «третье в списке», и «напоминание с id 3».
_RID_PREFIX = "r"
_RID_RE = re.compile(rf"^#?({_RID_PREFIX}[0-9a-f]{{4,12}})$", re.IGNORECASE)


def _new_rid(taken) -> str:
    # Свежий id, не занятый среди `taken`.
    for _ in range(100):
        rid = _RID_PREFIX + uuid.uuid4().hex[:5]
        if rid not in taken:
            return rid
    return _RID_PREFIX + uuid.uuid4().hex[:12]  # практически недостижимо


def parse_reminder_ref(arg: str) -> Optional[tuple]:
    """Аргумент «/cancel_reminder …» → ("id", "r3f9a2") | ("index", 0) | None.

    Единственный разборщик ссылки на напоминание: и Telegram, и веб-API
    зовут его, а не разбирают строку каждый по-своему. id — как он показан
    в списке (регистр и решётка не важны); число — номер строки того же
    списка, приводится к 0-based индексу.
    """
    s = str(arg or "").strip()
    m = _RID_RE.match(s)
    if m:
        return ("id", m.group(1).lower())
    if re.fullmatch(r"\d{1,3}", s):
        return ("index", int(s) - 1)
    return None


# ─── Отмена, список и просьба создать — текстом ──────────────
# «Отмени напоминание про воду», «какие у меня напоминания?» раньше шли в
# ветку создания (любой текст с «напом») и заводили НОВОЕ напоминание с
# вопросом «когда?». Эти формы разбираются до создания и им не становятся.

# Сколько ждём ответа на «когда напомнить?» / «на когда перенести?» /
# «какое именно?». Дольше — это уже не ответ, а новая реплика: раньше вопрос
# висел до рестарта и забирал любое следующее сообщение чата
PENDING_REMIND_TTL_SEC = 600

_CANCEL_REMIND_RE = re.compile(
    r"^\s*(?:(?:пожалуйста|please)[\s,]+)?"
    r"(?:отмени(?:те)?|отменить|удали(?:те)?|удалить|убери(?:те)?|убрать|"
    r"отключи(?:те)?|сними(?:те)?|сотри(?:те)?|cancel|delete|remove|clear|drop)\s+"
    r"(?P<all>(?:все|всё|all)(?:\s+(?:the|my|мои))?\s+)?"
    r"(?:(?:мо[её]|мои|это|эти|этот|the|my|this|that|these)\s+)?"
    r"(?:(?P<ord>перв\w+|втор\w+|трет\w+|последн\w+|first|second|third|last)\s+)?"
    r"(?:напоминани\w*|reminders?)(?![\wё])\s*(?P<rest>.*?)\s*[.!?…]*\s*$",
    re.IGNORECASE)
_CANCEL_HINT_HEAD_RE = re.compile(
    r"^(?:про|о|об|обо|насч[её]т|на\s+тему|что(?:бы)?|about|for|to|of|on|"
    r"regarding|that)\s+", re.IGNORECASE)

_LIST_REMIND_RE = re.compile(
    r"(?:какие|что\s+за)\s+(?:у\s+(?:меня|нас)\s+)?(?:есть\s+)?(?:сейчас\s+)?"
    r"(?:активные\s+)?напоминани|"
    r"(?:покажи|выведи|перечисли|назови|скажи)\s+(?:мне\s+)?(?:все\s+|мои\s+)?"
    r"(?:активные\s+)?напоминани|"
    r"(?:список|перечень)\s+(?:моих\s+|всех\s+|активных\s+)?напоминаний|"
    r"^\s*(?:мои|все|активные)\s+напоминания\s*\??\s*$|"
    r"есть\s+(?:ли\s+)?(?:у\s+(?:меня|нас)\s+)?(?:какие-?(?:то|нибудь)\s+)?напоминани\w*\s*\?|"
    r"(?:о\s+ч[её]м|что)\s+ты\s+(?:мне\s+)?(?:собираешься\s+)?напомнишь|"
    r"\b(?:what|which|any)\s+reminders\b|"
    r"\b(?:list|show)\s+(?:me\s+)?(?:all\s+)?(?:of\s+)?(?:my\s+|the\s+)?(?:active\s+)?reminders\b|"
    r"^\s*(?:my|active)\s+reminders\s*\??\s*$|"
    r"\bdo\s+i\s+have\s+(?:any\s+)?reminders\b",
    re.IGNORECASE)

# Просьба СОЗДАТЬ напоминание (а не любое упоминание «напом…»): без неё
# «напоминание пришло вовремя» или «ты мне напомнил?» спрашивали «когда
# напомнить?» и заводили задачу из всей фразы
_REMIND_CREATE_RE = re.compile(
    r"(?<![\wё])(?:напомни(?:те)?|напомнить|напоминай(?:те)?|"
    r"(?:поставь|создай|сделай|добавь|заведи|установи)(?:те)?\s+(?:мне\s+)?напоминани\w*)"
    r"(?![\wё])|\bremind\s+(?:me|us)\b|"
    r"\b(?:set|create|add|make)\s+(?:me\s+)?(?:a|an|the)?\s*reminder\b",
    re.IGNORECASE)

# Голый отказ на вопрос «когда напомнить?»
_PENDING_DECLINE_RE = re.compile(
    r"^\s*(?:не\s+надо|не\s+нужно|нет|отмена|отмени|забудь|неважно|не\s+важно|"
    r"передумал[аи]?|no|nope|cancel|never\s*mind|forget\s+it|don[’']?t)"
    r"(?:[\s,]+(?:спасибо|thanks|thank\s+you))?\s*[.!…]*\s*$", re.IGNORECASE)

# Похоже ли сообщение на попытку назвать время (тогда «не понял» —
# переспрос; иначе это новая реплика, и ожидание отпускается)
_TIME_ATTEMPT_RE = re.compile(
    r"\d|(?<![\wё])(?:через|в|во|к|на|завтра|послезавтра|сегодня|утр\w*|вечер\w*|"
    r"ноч\w*|дн[её]м|полдень|полночь|час\w*|минут\w*|секунд\w*|сутки|недел\w*|"
    r"кажд\w*|ежедневно|понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|"
    r"суббот\w*|воскресень\w*|полчаса|полтора|"
    r"in|at|on|tomorrow|today|tonight|morning|evening|noon|midnight|hours?|"
    r"minutes?|mins?|seconds?|every|daily|week|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|half)(?![\wё])", re.IGNORECASE)


def parse_cancel_reminder(text: str) -> Optional[dict]:
    """«отмени напоминание про воду» → {"all": False, "ref": None, "hint": "воду"};
    «удали напоминание 2» / «… r3f9a2» / «… второе» → ref (как у
    /cancel_reminder); «отмени все напоминания» → all. None — не отмена."""
    if not text or len(text) > 120:
        return None
    m = _CANCEL_REMIND_RE.match(text)
    if not m:
        return None
    rest = m.group("rest").strip(" ,.;:!?…\"'«»")
    out = {"all": bool(m.group("all")) or bool(re.fullmatch(r"(?:все|всё|all)", rest, re.IGNORECASE)),
           "ref": None, "hint": None}
    ordinal = _CHOICE_ORDINALS.get((m.group("ord") or "").lower())
    if ordinal is not None and not out["all"]:
        out["ref"] = ("index", ordinal)
        return out
    if out["all"] or not rest:
        return out
    bare = re.sub(r"^(?:номер|№|number|no\.?)\s*", "", rest, flags=re.IGNORECASE).strip()
    ref = parse_reminder_ref(bare)
    if ref:
        out["ref"] = ref
        return out
    idx = _CHOICE_ORDINALS.get(bare.lower())
    if idx is not None:
        out["ref"] = ("index", idx)
        return out
    out["hint"] = _CANCEL_HINT_HEAD_RE.sub("", rest).strip() or None
    return out


def is_list_reminders_request(text: str) -> bool:
    """«какие у меня напоминания?» / «покажи напоминания» / «what reminders
    do I have» — вопрос-список, а не просьба напомнить."""
    return bool(text) and len(text) <= 120 and bool(_LIST_REMIND_RE.search(text))


def is_reminder_create_request(text: str) -> bool:
    """Просьба создать напоминание («напомни …», «поставь напоминание»,
    «remind me …»), а не любое упоминание напоминаний."""
    return bool(text) and bool(_REMIND_CREATE_RE.search(text))


def is_pending_decline(text: str) -> bool:
    """«не надо» / «отмена» / «never mind» в ответ на «когда напомнить?»."""
    return bool(text) and bool(_PENDING_DECLINE_RE.match(text))


def looks_like_time_attempt(text: str) -> bool:
    """Сообщение похоже на попытку назвать время («в 8», «завтра утром»,
    «через час») — непонятое время переспрашиваем, остальное отпускаем."""
    return bool(text) and bool(_TIME_ATTEMPT_RE.search(text))


# ─── Пауза ────────────────────────────────────────────────
# Напоминание на паузе хранится с "paused": true; флага нет — активно (так
# читаются и файлы, записанные до появления паузы). Пауза не трогает ни
# trigger_at, ни расписание: планировщик такие записи просто не берёт.
# В списке (get_active) напоминание на паузе видно и после своего времени —
# его всё ещё можно продолжить или отменить.

def _choice_target_id(stored_ids: List[str], active: List[dict], reply: str) -> Optional[str]:
    """Ответ на «какое именно?» (перенос/отмена) → id выбранного напоминания:
    номер/id — через parse_reminder_ref, порядковое слово («второе»,
    «последнее») — по списку id из вопроса, слова задачи — по активным.
    None — не распознан."""
    text = reply.strip().lower()
    pool = stored_ids or [r.get("id") for r in sorted(active, key=lambda r: r["trigger_at"])]
    ref = parse_reminder_ref(text.rstrip(".)"))
    if ref:
        kind, val = ref
        if kind == "id":
            return val
        if kind == "index" and 0 <= val < len(pool):
            return pool[val]
    for word, idx in _CHOICE_ORDINALS.items():
        if re.search(rf"\b{word}\b", text):
            try:
                return pool[idx]
            except IndexError:
                return None
    matched = [r for r in active if _task_matches(text, r.get("task"))]
    if len(matched) == 1:
        return matched[0].get("id")
    return None


def is_paused(r: dict) -> bool:
    return bool(r.get("paused"))


def _is_listed(r: dict, now: float) -> bool:
    # Запись видна в списке: не погашена и либо ещё впереди, либо на паузе.
    return not r.get("fired") and (is_paused(r) or r["trigger_at"] > now)


def _is_armed(r: dict, now: float) -> bool:
    # Запись ждёт срабатывания: не погашена, не на паузе, время впереди.
    return not r.get("fired") and not is_paused(r) and r["trigger_at"] > now


def format_reminder_when(r: dict) -> str:
    """«Когда» для текстового списка напоминаний (/reminders в Telegram и
    веб-чате): расписание повтора или остаток до срабатывания, у стоящего
    на паузе — пометка; время разового, прошедшее на паузе, остатком не
    показываем (отрицательных минут не бывает)."""
    if r.get("recurrence"):
        when = format_schedule(r["recurrence"])
    else:
        remain = r["trigger_at"] - time.time()
        mins = int(remain / 60)
        if remain <= 0:
            when = "время прошло"
        else:
            when = f"через {mins} мин" if mins > 0 else f"через {int(remain)} сек"
    return f"{when} (на паузе)" if is_paused(r) else when


# ─── Менеджер ─────────────────────────────────────────────

class ReminderManager:
    """
    Хранит напоминания и шлёт их в нужное время через sender.
    Запускается как фоновая asyncio-задача в event loop бота.
    """

    def __init__(self, context: str = "default"):
        self.context = context
        self._base_dir = data_dir() / context / "reminders"
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
        # Гейт хода пользователя (общий с BotInstance): напоминание не
        # пишется в STM и не уходит посреди идущего хода — см. _fire
        self._turn_gate = None

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
        # Передаёт MemoryManager — сработавшие напоминания логируются в STM.
        self._memory = memory

    def set_turn_gate(self, gate):
        # Передаёт ChatTurnGate бота (app/core/turn_gate.py).
        self._turn_gate = gate

    def set_muted_check(self, check):
        # Передаёт callable () -> bool: заморожена ли персона (features.muted).
        self._muted_check = check

    def set_router_persona(self, router, persona):
        # Передаёт router и persona для генерации текста напоминания через LLM.
        self._router = router
        self._persona = persona

    def set_living(self, living):
        # Передаёт LivingPersona — текущий mood/energy попадают в текст напоминания.
        self._living = living

    def set_intellect_tier(self, tier):
        """Уровень интеллекта: primitive — минимальная вербализация
        напоминаний, почти шаблонная, без характерного текста."""
        self._primitive = bool(tier == "primitive")

    # ── persistence ──

    def _load(self):
        self._reminders = load_json_safe(self._file, default=[], label="Reminder")
        self._ensure_ids()

    def _ensure_ids(self):
        """Миграция при чтении: записи без id (старый формат файла) и
        случайные дубли (ручная правка файла, восстановление из бэкапа)
        получают свой id. Файл перезаписывается один раз — дальше id уже в нём.
        Без этого часть напоминаний осталась бы адресуемой только по номеру —
        без гарантии устойчивой адресации, ради которой id вводился."""
        taken = set()
        changed = False
        for r in self._reminders:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or "")
            if not rid or rid in taken:
                rid = _new_rid(taken)
                r["id"] = rid
                changed = True
            taken.add(rid)
        if changed:
            logger.info(f"[Reminder] id проставлены при загрузке "
                        f"({len(taken)} записей в файле)")
            self._save()

    def _save(self):
        # Атомарная запись (общий helper app.core.atomic_io — tmp-файл + os.replace).
        try:
            atomic_write_json(self._file, self._reminders)
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
            "id": None,  # стабильный id, выдаётся под локом (см. _new_rid)
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
            # id выдаём под локом — иначе два одновременных add_reminder могли
            # бы выбрать один и тот же (проверка занятости шла бы по одному
            # снимку списка)
            reminder["id"] = _new_rid({str(r.get("id") or "")
                                       for r in self._reminders})
            self._reminders.append(reminder)
            self._save()
        logger.info(f"[Reminder] Добавлено: id={reminder['id']} chat={chat_id} task='{task}' "
                    + (format_schedule(schedule) if schedule else f"через {delay_seconds:.0f}с"))
        return reminder

    def get_active(self, chat_id: str) -> List[dict]:
        # Не сработавшие напоминания чата, включая поставленные на паузу
        # (у них "paused": true — списки помечают их, см. is_paused).
        now = time.time()
        with self._lock:
            return [
                r for r in self._reminders
                if r["chat_id"] == str(chat_id) and _is_listed(r, now)
            ]

    def cancel_reminder(self, chat_id: str, index: int) -> bool:
        """Удаляет напоминание по индексу (0-based, из get_active) → True/False.
        Оставлено для веб-API, который присылает номер строки списка; надёжный
        путь — :meth:`cancel_by_ref` с id (см. parse_reminder_ref)."""
        return self.cancel_by_ref(chat_id, int(index)) is not None

    def cancel_by_ref(self, chat_id: str, ref) -> Optional[dict]:
        """Удаляет напоминание по ссылке: id («r3f9a2», регистр и «#» не
        важны) или 0-based индексу в get_active (для совместимости с вводом
        номера). Возвращает удалённую запись — вызывающая сторона называет её
        задачу в ответе, так промах адресации виден сразу, — или None.

        Единственная точка удаления по ссылке: и Telegram, и веб-API, и старый
        cancel_reminder идут через неё, поэтому «что считается ссылкой»
        определено в одном месте.
        """
        active = self.get_active(chat_id)
        target = None
        if isinstance(ref, int) and not isinstance(ref, bool):
            if 0 <= ref < len(active):
                target = active[ref]
        else:
            rid = str(ref or "").strip().lstrip("#").lower()
            if rid:
                target = next((r for r in active
                               if str(r.get("id") or "").lower() == rid), None)
        if target is None:
            return None
        with self._lock:
            try:
                # Удаляем сам объект, а не «элемент №N»: параллельное
                # срабатывание/добавление не может сдвинуть цель
                self._reminders.remove(target)
            except ValueError:
                return None
            self._save()
        logger.info(f"[Reminder] Отменено: chat={chat_id} id={target.get('id')} "
                    f"task='{target.get('task')}'")
        return target

    def postpone_by_id(self, chat_id: str, rid: str, seconds: Optional[float] = None,
                       abs_time: Optional[tuple] = None,
                       relative_to_trigger: bool = False) -> Optional[dict]:
        """Переносит ровно то напоминание, чей id передан — тот же приём, что
        cancel_by_ref для отмены (см. выше): адресация по id, а не по
        пересчитанному заново индексу/порядку списка. Нужен, когда id
        кандидатов уже показан пользователю (см. resolve_postpone_choice) —
        список активных к моменту ответа мог измениться (одно сработало,
        добавилось новое), а переносить нужно то, что видел пользователь.

        Возвращает {"task", "trigger_at", "recreated": False}, как
        postpone_reminder, либо None — такого АКТИВНОГО напоминания в этом
        чате уже нет (отменено/сработало/id не совпал).
        """
        now = time.time()
        if abs_time:
            delay = _absolute_to_delay(abs_time[0], abs_time[1])
            if delay is None:
                return None
        rid = str(rid or "").strip().lstrip("#").lower()
        if not rid:
            return None
        with self._lock:
            # Напоминание на паузе перенос не берёт: сдвигается то, что
            # должно сработать (продолжить — через update_by_id)
            target = next((r for r in self._reminders
                           if r["chat_id"] == str(chat_id) and _is_armed(r, now)
                           and str(r.get("id") or "").lower() == rid), None)
            if target is None:
                return None
            if abs_time:
                new_trigger = now + delay
            else:
                base = target["trigger_at"] if relative_to_trigger else now
                new_trigger = base + seconds
            target["trigger_at"] = new_trigger
            self._save()
        logger.info(f"[Reminder] Перенесено по id: chat={chat_id} id={rid} "
                    f"task='{target.get('task')}' на "
                    f"{timeutil.from_ts(new_trigger).strftime('%d.%m %H:%M')}")
        return {"task": target.get("task"), "trigger_at": new_trigger, "recreated": False}

    def update_by_id(self, chat_id: str, rid: str, task: Optional[str] = None,
                     trigger_at: Optional[float] = None,
                     active: Optional[bool] = None) -> Optional[dict]:
        """Правка напоминания по id на месте (веб-досье): текст, время
        срабатывания и/или пауза. Одна запись под одним локом — в отличие от
        «отменить + создать заново», сбой посередине ничего не теряет, а id,
        автор и расписание повтора остаются прежними.

        У повторяющегося новое время задаёт и расписание: час/минута (у
        еженедельного — и день недели) берутся из trigger_at по часам
        пользователя (timeutil).

        active=False ставит на паузу (планировщик запись не берёт), True —
        продолжает:
          - повторяющееся — со следующего времени по расписанию после «сейчас»
            (срабатывания, пропущенные на паузе, не догоняются); если прежнее
            trigger_at ещё впереди, оно остаётся — короткая пауза не сбрасывает
            перенос, сделанный до неё;
          - разовое, чьё время прошло за время паузы, — trigger_at не
            трогаем, и цикл отправляет его ближайшим тиком, как любое
            просроченное (например, после рестарта). Раз пользователь
            продолжил напоминание, он хочет его получить; молча потерять
            или оставить на паузе было бы хуже. Нужно другое время —
            передаётся вместе с active в trigger_at.

        Возвращает обновлённую запись (копию) либо None — такого напоминания
        (не сработавшего: впереди или на паузе) в чате нет. ValueError —
        время в прошлом.
        """
        now = time.time()
        if trigger_at is not None and trigger_at < now + 10:
            raise ValueError("время напоминания уже прошло")
        rid = str(rid or "").strip().lstrip("#").lower()
        if not rid:
            return None
        with self._lock:
            target = next((r for r in self._reminders
                           if r["chat_id"] == str(chat_id) and _is_listed(r, now)
                           and str(r.get("id") or "").lower() == rid), None)
            if target is None:
                return None
            if task is not None:
                target["task"] = task or None
            if trigger_at is not None:
                schedule = target.get("recurrence")
                if schedule:
                    dt = timeutil.from_ts(trigger_at)
                    schedule = {**schedule, "hour": dt.hour, "minute": dt.minute}
                    if schedule.get("type") == "weekly":
                        schedule["weekday"] = dt.weekday()
                    target["recurrence"] = schedule
                target["trigger_at"] = float(trigger_at)
                target["attempts"] = 0
            if active is False and not is_paused(target):
                target["paused"] = True
            elif active is True and is_paused(target):
                target.pop("paused", None)
                target["attempts"] = 0
                schedule = target.get("recurrence")
                if schedule and trigger_at is None and target["trigger_at"] <= now:
                    target["trigger_at"] = _next_occurrence(schedule, now)
            self._save()
            updated = dict(target)
        logger.info(f"[Reminder] Изменено по id: chat={chat_id} id={rid} "
                    f"task='{updated.get('task')}' на "
                    f"{timeutil.from_ts(updated['trigger_at']).strftime('%d.%m %H:%M')}"
                    + (" (на паузе)" if is_paused(updated) else ""))
        return updated

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
        (не угадываем: молча передвинутое ближайшее может быть не тем
        напоминанием, которое имел в виду пользователь).

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

        # Напоминания на паузе в выбор цели не входят: переносится то, что
        # должно сработать
        with self._lock:
            active = [
                r for r in self._reminders
                if r["chat_id"] == str(chat_id) and _is_armed(r, now)
            ]

        def _choices(items) -> list:
            return [
                {"id": r.get("id"), "task": r.get("task"),
                 "trigger_at": r["trigger_at"]}
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
                        f"на {timeutil.from_ts(new_trigger).strftime('%d.%m %H:%M')}")
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

    def _pending_entry(self, chat_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        """Живое ожидание чата: старше PENDING_REMIND_TTL_SEC — снимается (это
        уже не ответ на вопрос). user_id — кто пишет: в группе на вопрос
        отвечает тот, кому его задали, чужая реплика ожидание не трогает."""
        with self._lock:
            entry = self._pending_remind.get(str(chat_id))
            if not isinstance(entry, dict):
                return None
            if time.time() - float(entry.get("asked_at") or 0) > PENDING_REMIND_TTL_SEC:
                self._pending_remind.pop(str(chat_id), None)
                return None
            owner = entry.get("user_id")
            if owner and user_id and str(owner) != str(user_id):
                return None
            return entry

    def begin_pending_remind(self, chat_id: str, task: str, user_id: Optional[str] = None):
        """Запоминает задачу напоминания, ждём от пользователя ответа про время.
        asked_at нужен, чтобы при одновременно висящем вопросе обучения «как часто?»
        отдать ответ о периодичности тому, кто спросил ПОЗЖЕ (см. process_message),
        и для срока жизни вопроса; user_id — кто должен ответить."""
        with self._lock:
            self._pending_remind[str(chat_id)] = {
                "task": task, "asked_at": time.time(),
                "user_id": str(user_id) if user_id else None}

    def get_pending_remind(self, chat_id: str, user_id: Optional[str] = None) -> Optional[str]:
        # Текст задачи pending-напоминания (или None).
        entry = self._pending_entry(chat_id, user_id)
        return entry.get("task") if entry else None

    def get_pending_remind_asked_at(self, chat_id: str) -> Optional[float]:
        # Когда был задан вопрос «через сколько напомнить?» (timestamp или None).
        entry = self._pending_entry(chat_id)
        return entry.get("asked_at") if entry else None

    def clear_pending_remind(self, chat_id: str):
        with self._lock:
            self._pending_remind.pop(str(chat_id), None)

    def begin_pending_postpone(self, chat_id: str, user_id: Optional[str] = None):
        # Ждём ответа «на когда перенести?» (перенос без указания времени).
        with self._lock:
            self._pending_remind[str(chat_id)] = {
                "task": None, "postpone": True, "asked_at": time.time(),
                "user_id": str(user_id) if user_id else None,
            }

    def get_pending_postpone(self, chat_id: str, user_id: Optional[str] = None) -> bool:
        # Висит ли вопрос «на когда перенести напоминание?».
        entry = self._pending_entry(chat_id, user_id)
        return bool(entry and entry.get("postpone"))

    def begin_pending_postpone_choice(self, chat_id: str, ids: Optional[List[str]] = None,
                                      seconds: Optional[float] = None,
                                      abs_time: Optional[tuple] = None,
                                      relative_to_trigger: bool = False,
                                      user_id: Optional[str] = None):
        """Несколько активных напоминаний и подсказки нет — ждём ответа
        «какое именно перенести?». Сдвиг запоминаем, применим к выбранному.

        ``ids`` — id кандидатов в том порядке, в каком список показан
        пользователю (см. _fmt_reminder_choices в bot_instance): единый
        источник истины для resolve_postpone_choice — список активных к
        моменту ответа мог измениться (одно сработало, добавилось новое),
        а «номер 2»/«последнее» должны указывать на то, что видел
        пользователь, а не на пересчитанный заново список."""
        with self._lock:
            self._pending_remind[str(chat_id)] = {
                "task": None, "postpone_choice": True,
                "ids": list(ids) if ids else [],
                "seconds": seconds, "abs": abs_time,
                "rel": relative_to_trigger, "asked_at": time.time(),
                "user_id": str(user_id) if user_id else None,
            }

    def get_pending_postpone_choice(self, chat_id: str, user_id: Optional[str] = None) -> bool:
        # Висит ли вопрос «какое напоминание перенести?».
        entry = self._pending_entry(chat_id, user_id)
        return bool(entry and entry.get("postpone_choice"))

    # ── отмена текстом ──

    def begin_pending_cancel_choice(self, chat_id: str, ids: List[str],
                                    user_id: Optional[str] = None):
        """Под отмену подходят несколько напоминаний — ждём «какое именно?».
        ids — в том порядке, в каком список показан (как у переноса)."""
        with self._lock:
            self._pending_remind[str(chat_id)] = {
                "task": None, "cancel_choice": True, "ids": list(ids),
                "asked_at": time.time(), "user_id": str(user_id) if user_id else None,
            }

    def get_pending_cancel_choice(self, chat_id: str, user_id: Optional[str] = None) -> bool:
        entry = self._pending_entry(chat_id, user_id)
        return bool(entry and entry.get("cancel_choice"))

    def cancel_request(self, chat_id: str, req: dict) -> dict:
        """Отмена по разбору parse_cancel_reminder → {"cancelled": [записи]} |
        {"ambiguous": [записи]} (подходят несколько — спросить, какое) |
        {"not_found": True, "active": [...]} | {"none": True} (активных нет).
        Без подсказки и номера — единственное активное отменяется, при
        нескольких — вопрос."""
        active = self.get_active(chat_id)
        if not active:
            return {"none": True}
        if req.get("all"):
            removed = [r for r in (self.cancel_by_ref(chat_id, a.get("id")) for a in active) if r]
            return {"cancelled": removed}
        ref = req.get("ref")
        if ref:
            kind, val = ref
            if kind == "index":
                val = len(active) - 1 if val == -1 else val
            target = self.cancel_by_ref(chat_id, val)
            return {"cancelled": [target]} if target else {"not_found": True, "active": active}
        hint = req.get("hint")
        matched = [r for r in active if _task_matches(hint, r.get("task"))] if hint else list(active)
        if not matched:
            return {"not_found": True, "active": active}
        if len(matched) == 1:
            target = self.cancel_by_ref(chat_id, matched[0].get("id"))
            return {"cancelled": [target]} if target else {"not_found": True, "active": active}
        return {"ambiguous": matched}

    def resolve_cancel_choice(self, chat_id: str, reply: str) -> Optional[dict]:
        """Ответ на «какое отменить?» → {"cancelled": [запись]} | {"gone": True}
        (активных больше нет) | None (не распознан — переспросить)."""
        with self._lock:
            entry = self._pending_remind.get(str(chat_id))
        if not (isinstance(entry, dict) and entry.get("cancel_choice")):
            return None
        active = self.get_active(chat_id)
        if not active:
            self.clear_pending_remind(chat_id)
            return {"gone": True}
        target_id = _choice_target_id(entry.get("ids") or [], active, reply)
        if target_id is None:
            return None
        target = self.cancel_by_ref(chat_id, target_id)
        if target is None:
            return None
        self.clear_pending_remind(chat_id)
        return {"cancelled": [target]}

    def resolve_postpone_choice(self, chat_id: str, reply: str) -> Optional[dict]:
        """Разбирает ответ на «какое именно напоминание перенести?» и
        переносит ЕГО — по id, сохранённому в begin_pending_postpone_choice
        на момент вопроса (entry["ids"]), а не по индексу в списке активных,
        пересчитанному заново на момент ответа: список мог измениться между
        вопросом и ответом (одно сработало, добавилось новое) — «номер 2»
        указал бы уже не туда.

        Номер/id — через parse_reminder_ref (тот же разборщик, что у
        cancel_by_ref и веб-API); порядковые слова («первое», «последнее»)
        — через _CHOICE_ORDINALS по списку id из вопроса; слова из задачи
        («чай») — по текущим активным (задача могла сохраниться, даже если
        порядок в списке сместился). Сам перенос — postpone_by_id (тот же
        приём, что у отмены по id): нужно то, что видел пользователь, а не
        то, что оказалось первым в пересчитанном списке.

        Возвращает {"task", "trigger_at", "recreated": False} | {"gone": True}
        (активных больше нет) | None (ответ не распознан, либо указанное
        напоминание уже не активно — переспросить)."""
        with self._lock:
            entry = self._pending_remind.get(str(chat_id))
        if not (isinstance(entry, dict) and entry.get("postpone_choice")):
            return None

        active = [r for r in self.get_active(chat_id) if not is_paused(r)]
        if not active:
            self.clear_pending_remind(chat_id)
            return {"gone": True}

        target_id = _choice_target_id(entry.get("ids") or [], active, reply)
        if target_id is None:
            return None

        result = self.postpone_by_id(
            chat_id, target_id, seconds=entry.get("seconds"),
            abs_time=entry.get("abs"), relative_to_trigger=bool(entry.get("rel")),
        )
        if result is None:
            # То самое напоминание, что видел пользователь, уже не активно
            # (сработало/отменено между вопросом и ответом) — переспрашиваем,
            # а не молча двигаем что-то другое под тем же номером
            return None
        self.clear_pending_remind(chat_id)
        logger.info(f"[Reminder] Перенесено по выбору: chat={chat_id} id={target_id} "
                    f"task='{result.get('task')}'")
        return result

    def _cleanup_fired(self):
        # Удаляет сработавшие напоминания старше 24ч.
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
        # Отправляет напоминание в чат. Текст генерируется через LLM в характере персоны.
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
        # чата. Определяем явно: LLM переписку при генерации не видит и иначе
        # ответит на языке английской служебной обёртки промпта
        lang = self._reminder_lang(chat_id, task)

        # Пытаемся сгенерировать через LLM в характере персоны.
        # primitive: характерного текста нет — почти шаблонная минимальная
        # вербализация, LLM-генерация пропускается
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

        # Кто попросил: в группах тегаем через @username; в личке не
        # печатаем заглушку «User» вместо имени. Не дублируем приставку,
        # если LLM уже обратился по имени в начале текста.
        prefix = None
        if reminder.get("username"):
            prefix = f"@{reminder['username']}"
        elif user_name and user_name.strip().lower() not in ("user", "пользователь"):
            prefix = user_name
        if prefix and prefix.lstrip("@").lower() not in text.lstrip()[:120].lower():
            text = f"{prefix}, {text}"

        # Посреди хода пользователя (он написал, ответ генерируется или
        # доставляется) не пишем и не шлём — иначе напоминание легло бы в
        # STM между репликой и ответом и перебило бы живой обмен. Запись в
        # STM — атомарно под локом гейта: хода нет И напоминание всё ещё в
        # силе (могло быть отменено/перенесено в только что закончившемся
        # ходе — тогда не шлём). Ход идёт — откладываем до следующего тика,
        # не ждём в цикле.
        entry = None
        gate = self._turn_gate
        if gate is not None:
            res = await asyncio.to_thread(
                gate.commit_message, self._memory, chat_id, text,
                None, lambda: self._still_due(reminder))
            if res.status == "busy":
                logger.info(f"[Reminder] Идёт ход пользователя в {chat_id} — "
                            "напоминание отложено до следующего тика")
                return _DEFERRED
            if res.status == "cancelled":
                logger.info(f"[Reminder] Напоминание отменено/перенесено, пока шёл "
                            f"ход пользователя, — не отправляем: {task}")
                return _CANCELLED
            entry = res.entry

        try:
            ok = await self._sender.send_message(chat_id, text, topic_id=topic_id)
        except Exception as e:
            logger.error(f"[Reminder] Ошибка отправки в {chat_id}: {e}")
            ok = False
        if not ok:
            logger.error(f"[Reminder] Отправка в {chat_id} не удалась")
            # Пользователь напоминания не увидел — в истории его быть не должно
            if entry is not None:
                await asyncio.to_thread(gate.rollback_message, self._memory, chat_id, entry)
            return False
        logger.info(f"[Reminder] Отправлено в чат {chat_id}: {text[:60]}")
        # Логируем в STM, чтобы в буфере и в чате картина была одна
        # (роль assistant — LTM-экстракция на неё не срабатывает). С гейтом
        # запись уже сделана выше, до доставки
        if gate is None and self._memory:
            try:
                await asyncio.to_thread(
                    self._memory.add_message, "assistant", text,
                    user_id=chat_id, chat_id=chat_id,
                )
            except Exception as e:
                logger.warning(f"[Reminder] Не удалось записать напоминание в STM: {e}")
        return True

    def _still_due(self, reminder: dict) -> bool:
        """Напоминание всё ещё должно сработать: оно в списке (не отменено —
        сравнение по идентичности), не погашено, не на паузе и не перенесено
        на потом.
        Зовётся под локом гейта хода, перед записью в STM."""
        with self._lock:
            return (any(r is reminder for r in self._reminders)
                    and not reminder.get("fired")
                    and not is_paused(reminder)
                    and reminder.get("trigger_at", 0) <= time.time())

    def _reminder_lang(self, chat_id: str, task: Optional[str]) -> str:
        """Язык напоминания: сначала текст задачи (он продиктован пользователем),
        иначе — последние сообщения пользователя из чата (общий детектор
        app.core.language: реплики ассистента и синтетика не считаются).
        Ничего не определено — русский (как в rhythm_manager)."""
        lang = detect_language(task or "")
        if not lang and self._memory:
            try:
                lang = detect_dialogue_language(
                    "", self._memory.stm.get_last(8, chat_id=chat_id))
            except Exception:
                lang = None
        if not lang and self._persona is not None:
            lang = persona_language(getattr(self._persona, "system_prompt", ""))
        return language_name(lang) or "Russian"

    def _generate_reminder_text(self, user_name: str, task: Optional[str],
                                lang: str = "English", chat_id: str = None) -> Optional[str]:
        # Генерирует текст напоминания через LLM в характере персоны. Синхронный вызов.
        assert self._router and self._persona  # проверяется в _fire перед вызовом
        persona_prompt = self._persona.system_prompt.strip()
        # Текущее mood/energy персоны: лёгкий фоновый контекст, не директива
        living_block = ""
        if self._living and chat_id:
            try:
                state_ctx = self._living.get_living_context(chat_id)
                if state_ctx:
                    living_block = f"\n\n{state_ctx}"
            except Exception:
                pass
        # Обращение по имени: заглушку «User» в промпт не подставляем —
        # модель переносит её в текст дословно
        addr = user_name.strip()
        if addr.lower() in ("user", "пользователь"):
            addr = ""
        if not addr:
            addr = "the user"
        if task:
            user_content = f"Remind {addr}: {task}"
        else:
            user_content = (
                f"Remind {addr} — they asked for a reminder but didn't specify what for."
            )
        # lang — имя языка ("Russian"/"English"), строке языка нужен код
        code = {"Russian": "ru", "English": "en"}.get(lang)

        messages = [
            {"role": "system", "content": (
                f"{persona_prompt}\n\n"
                "---\n"
                "You are reminding the user about something at their request. "
                "Write a short reminder (1-2 sentences) in your character. "
                "Be sure to mention the essence of the task. "
                "Do NOT use markdown. Do NOT write meta-notes."
                f"{living_block}\n\n"
                f"{user_language_line(code)}"
            )},
            {"role": "user", "content": user_content},
        ]

        # Канал «proactive»: фон не делит инстанс/лок с ответом пользователю
        # (main) — иначе зависшее напоминание блокировало бы диалог
        response = self._router.get_response(messages, temperature=0.7,
                                             max_tokens=200, top_p=0.9,
                                             webchat_channel="proactive")
        if not response or len(response.strip()) < 5:
            return None
        return response.strip()

    async def _check_due(self):
        """Один проход планировщика: отправляет наступившие напоминания.
        Им же цикл догоняет просроченные после рестарта (первый тик берёт всё,
        чьё время прошло). Напоминания на паузе не берутся вовсе."""
        now = time.time()
        with self._lock:
            due = [r for r in self._reminders
                   if not r.get("fired") and not is_paused(r) and r["trigger_at"] <= now]

        # fired ставим только ПОСЛЕ успешной отправки — иначе при сбое
        # (падение процесса, ошибка сети) напоминание потерялось бы без повтора
        changed = False
        for r in due:
            # Идёт ход пользователя в этом чате — напоминание ждёт
            # следующего тика целиком (без генерации текста и без траты
            # попытки), чтобы не держать цикл и напоминания других чатов
            gate = self._turn_gate
            if gate is not None and gate.busy(r["chat_id"]):
                logger.info(f"[Reminder] Идёт ход пользователя в {r['chat_id']} — "
                            f"«{r.get('task')}» ждёт следующего тика")
                continue
            success = await self._fire(r)
            if success == _DEFERRED or success == _CANCELLED:
                # отложено ходом — повтор тиком, не сбой; отменено —
                # запись уже убрана/перенесена/поставлена на паузу самим ходом
                continue
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

    async def _loop(self):
        # Главный цикл — проверяет каждые 30 секунд.
        logger.info(f"[Reminder] Цикл запущен для context={self.context}")
        cleanup_counter = 0

        while self._running:
            try:
                await self._check_due()

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
        # Останавливает фоновую задачу.
        self._running = False
        if self._task:
            self._task.cancel()
            logger.info("[Reminder] Остановлено")

    def format_delay(self, delay_seconds: float) -> str:
        # Человекочитаемое описание задержки.
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

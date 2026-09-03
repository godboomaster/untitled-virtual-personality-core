"""
Менеджер обучения.
Пользователь просит «научи меня X» — бот уточняет частоту и регулярно присылает уроки.
Каждый N-й урок — тест. После N молчаний подряд бот спрашивает «продолжать?» и при
дальнейшем молчании/«нет» останавливает обучение.

Хранит сессии в data/{context}/learning/learning.json (список словарей).
Фоновый asyncio-цикл каждые 30с проверяет наступившие уроки и шлёт через sender.
По форме повторяет ReminderManager.
"""

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Any

from app.core.language import language_name

logger = logging.getLogger(__name__)


# ─── Парсинг частоты уроков ──────────────────────────────────

# Все формы русских единиц времени (все падежи/числа), которые может написать пользователь.
_UNIT_TO_SECONDS = {
    # секунды
    "секунду": 1, "секунды": 1, "секунд": 1, "секунда": 1, "сек": 1, "с": 1,
    # минуты
    "минуту": 60, "минуты": 60, "минут": 60, "минута": 60, "мин": 60, "м": 60,
    # часы
    "час": 3600, "часа": 3600, "часов": 3600, "часы": 3600,
    # дни
    "день": 86400, "дня": 86400, "дней": 86400, "сутки": 86400, "суток": 86400,
    # недели
    "неделю": 604800, "недели": 604800, "недель": 604800, "неделя": 604800,
    # месяц
    "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
    # английские
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "day": 86400, "days": 86400,
    "week": 604800, "weeks": 604800,
    "month": 2592000, "months": 2592000,
}

# Числительные прописью для формулировок «каждые десять минут», «раз в два часа».
_RU_WORD_NUMBERS = {
    "ноль": 0, "полтора": 1.5,
    "одну": 1, "один": 1, "одно": 1, "одна": 1,
    "две": 2, "два": 2,
    "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
    "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20,
    "тридцать": 30, "сорок": 40, "пятьдесят": 50,
}

# Английские числительные прописью + «once»/«twice» для «twice a day».
_EN_WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "once": 1, "twice": 2, "an": 1, "a": 1,
}
# «a couple of hours» — отдельной парой слов
_EN_COUPLE_RE = re.compile(r"\b(?:a\s+)?couple\s+of?\s+(hours?|hrs?|minutes?|mins?|days?)\b", re.IGNORECASE)

# «раз в <единицу>»: единица в винительном падеже ед.ч.
_SINGLE_UNIT_TO_SECONDS = {
    "секунду": 1, "минуту": 60, "час": 3600, "день": 86400, "сутки": 86400,
    "неделю": 604800, "месяц": 2592000,
    "second": 1, "minute": 60, "hour": 3600, "day": 86400,
    "week": 604800, "month": 2592000,
}

# Указательные слова, после которых ожидается «N единиц»: «каждые/через N минут»,
# «in 10 minutes», «every 2 hours».
_LEAD_WORDS_RE = re.compile(r"\b(?:каждые|каждую|каждое|каждого|через|спустя|раз\s+в|интервал[а-я]*|с\s+интервалом|every|each|in|after|once|twice)\b", re.IGNORECASE)

# Паттерн «N <единиц>» — число (цифра) + единица в любой форме (рус/англ).
_N_UNIT_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([a-zа-яё]+)", re.IGNORECASE)
# Паттерн «числительное-прописью <единиц>».
_WORD_N_UNIT_RE = re.compile(r"([a-zа-яё]+)\s+([a-zа-яё]+)", re.IGNORECASE)


def parse_frequency(text: str, min_seconds: float = 300, max_seconds: float = 2592000) -> Optional[float]:
    """
    Парсит частоту уроков из ответа пользователя. Устойчив к морфологии русского и
    разным формулировкам: «каждые 10 минут», «каждый 10 минут», «10 минут», «раз в 10 минут»,
    «раз в день», «два раза в день», «каждый час», «через полчаса» и т.п.
    Возвращает интервал в секундах или None.
    """
    lower = text.lower()

    # ── 0. Английские «once/twice/N times a day» и «a couple of hours»
    m = _EN_COUPLE_RE.search(lower)
    if m:
        unit = _UNIT_TO_SECONDS.get(m.group(1), 0)
        if unit:
            return _clip(unit * 2, min_seconds, max_seconds)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s+times?\s+(?:a|an|per)\s+([a-zа-яё]+)", lower)
    if m:
        try:
            times = float(m.group(1).replace(",", "."))
            unit = _UNIT_TO_SECONDS.get(m.group(2), 0)
            if times > 0 and unit:
                return _clip(unit / times, min_seconds, max_seconds)
        except ValueError:
            pass
    m = re.search(r"\b(once|twice)\s+(?:a|an|per)\s+([a-zа-яё]+)", lower)
    if m and m.group(1) in _EN_WORD_NUMBERS:
        times = _EN_WORD_NUMBERS[m.group(1)]
        unit = _UNIT_TO_SECONDS.get(m.group(2), 0)
        if times > 0 and unit:
            return _clip(unit / times, min_seconds, max_seconds)

    # ── 1. «N раз в <единицу>»: 2 раза в день → интервал = единица / N
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*раз[а-я]*\s+в\s+(секунду|минуту|час|день|сутки|неделю|месяц)", lower)
    if m:
        try:
            times = float(m.group(1).replace(",", "."))
            unit = _SINGLE_UNIT_TO_SECONDS.get(m.group(2), 0)
            if times > 0 and unit:
                return _clip(unit / times, min_seconds, max_seconds)
        except ValueError:
            pass

    # ── 1b. «<числительное-прописью> раз в <единицу>»: два раза в день
    m = re.search(r"([а-яё]+)\s+раз[а-я]*\s+в\s+(секунду|минуту|час|день|сутки|неделю|месяц)", lower)
    if m and m.group(1) in _RU_WORD_NUMBERS:
        times = _RU_WORD_NUMBERS[m.group(1)]
        unit = _SINGLE_UNIT_TO_SECONDS.get(m.group(2), 0)
        if times > 0 and unit:
            return _clip(unit / times, min_seconds, max_seconds)

    # ── 2. «раз в <единицу>» (без числа): раз в день → единица
    m = re.search(r"\bраз[а-я]*\s+в\s+(секунду|минуту|час|день|сутки|неделю|месяц)", lower)
    if m:
        unit = _SINGLE_UNIT_TO_SECONDS.get(m.group(1), 0)
        if unit:
            return _clip(unit, min_seconds, max_seconds)

    # ── 2b. «каждый <единица-ед.ч.>»: каждый час / каждый день / каждое утро,
    #       «every hour» / «each day» (англ).
    # ВАЖНО: идёт ДО общего поиска «N <единиц>» (шаг 3) — иначе «каждый день в 3 часа»
    # ошибочно давал 3 часа вместо суток (бралось «3 часа» раньше).
    # «утро»/«вечер» — это раз в сутки: бот работает по интервалам, а не по времени суток.
    m = re.search(r"\bкажд(ый|ую|ое|ые|ого|ая)\s+(секунду|минуту|час|день|сутки|неделю|месяц|утро|вечер)", lower)
    if m:
        if m.group(2) in ("утро", "вечер"):
            return _clip(86400, min_seconds, max_seconds)
        unit = _UNIT_TO_SECONDS.get(m.group(2)) or _SINGLE_UNIT_TO_SECONDS.get(m.group(2), 0)
        if unit:
            return _clip(unit, min_seconds, max_seconds)
    m = re.search(r"\b(?:every|each)\s+(second|minute|hour|day|week|month)\b", lower)
    if m:
        unit = _SINGLE_UNIT_TO_SECONDS.get(m.group(1), 0)
        if unit:
            return _clip(unit, min_seconds, max_seconds)

    # ── 3. Ищем «N <единиц>» (цифра) в тексте — с приоритетом после указательных слов
    #     (каждые/через/раз в/интервал). Берём первое подходящее.
    candidates_num: List[float] = []
    for mm in _N_UNIT_NUM_RE.finditer(lower):
        try:
            value = float(mm.group(1).replace(",", "."))
        except ValueError:
            continue
        unit = _UNIT_TO_SECONDS.get(mm.group(2))
        if value <= 0 or not unit:
            continue
        candidates_num.append(value * unit)
    if candidates_num:
        # Если есть указательное слово — отдаём приоритет числу сразу после него,
        # иначе просто первому найденному.
        lead = _LEAD_WORDS_RE.search(lower)
        if lead:
            after = lower[lead.end():]
            mm = _N_UNIT_NUM_RE.search(after)
            if mm:
                try:
                    value = float(mm.group(1).replace(",", "."))
                    unit = _UNIT_TO_SECONDS.get(mm.group(2))
                    if value > 0 and unit:
                        return _clip(value * unit, min_seconds, max_seconds)
                except ValueError:
                    pass
        return _clip(candidates_num[0], min_seconds, max_seconds)

    # ── 4. «числительное-прописью <единиц>»: «каждые десять минут», «через два часа»,
    #       «in ten minutes» (англ)
    lead = _LEAD_WORDS_RE.search(lower)
    search_text = lower[lead.end():] if lead else lower
    for mm in _WORD_N_UNIT_RE.finditer(search_text):
        word = mm.group(1)
        unit = _UNIT_TO_SECONDS.get(mm.group(2))
        if word in _RU_WORD_NUMBERS and unit:
            return _clip(_RU_WORD_NUMBERS[word] * unit, min_seconds, max_seconds)
        if word in _EN_WORD_NUMBERS and unit:
            return _clip(_EN_WORD_NUMBERS[word] * unit, min_seconds, max_seconds)

    # ── 6. Отдельные слова: «полчаса», «ежечасно», «ежедневно», «half an hour», «daily»
    if "полчаса" in lower or "пол-часа" in lower:
        return _clip(1800, min_seconds, max_seconds)
    _WORD_INTERVALS = {
        "ежечасно": 3600, "каждый час": 3600,
        "ежедневно": 86400, "ежесуточно": 86400, "каждый день": 86400,
        "еженедельно": 604800, "каждую неделю": 604800,
        "half an hour": 1800, "an hour": 3600,
        "hourly": 3600, "every hour": 3600,
        "daily": 86400, "every day": 86400,
        "weekly": 604800, "every week": 604800,
    }
    for phrase, secs in _WORD_INTERVALS.items():
        if phrase in lower:
            return _clip(secs, min_seconds, max_seconds)

    return None


def _clip(value: float, lo: float, hi: float) -> float:
    """Прижимает значение к границам диапазона (clamping).
    Раньше при выходе за границы возвращал None — из-за этого понятая, но слишком
    частая/редкая периодичность («каждую минуту») отвечалась пользователю как
    «не понял частоту». Теперь прижимаем к ближайшей границе: подтверждение setup
    всё равно показывает фактический интервал (format_delay), подмены незаметной нет."""
    return max(lo, min(hi, value))


# ─── Ответ «да/нет» на «продолжать обучение?» ───────────────

_POSITIVE_RE = re.compile(r"\b(?:да|давай|продолж\w*|хочу|ок|ok|yes|конечно|поехали|угу)\b", re.IGNORECASE)
_NEGATIVE_RE = re.compile(r"\b(?:нет|не\s+надо|хватит|стоп|останов\w*|no|не\s+хочу|отстань)\b", re.IGNORECASE)


def classify_continue_answer(text: str) -> str:
    """Определяет ответ пользователя на «продолжать обучение?».
    Возвращает 'YES' | 'NO' | 'UNKNOWN'."""
    if _NEGATIVE_RE.search(text):
        return "NO"
    if _POSITIVE_RE.search(text):
        return "YES"
    return "UNKNOWN"


# ─── Менеджер ───────────────────────────────────────────────

class LearningManager:
    """
    Хранит сессии обучения и шлёт уроки по расписанию через sender.
    Запускается как фоновая asyncio-задача в event loop бота.
    """

    DEFAULT_QUIZ_EVERY = 3
    DEFAULT_SILENCE_THRESHOLD = 3
    REINFORCE_COOLDOWN_SECONDS = 6 * 3600  # не чаще раза в 6 часов на курс
    REINFORCE_PROBABILITY = 0.25  # и даже тогда — не гарантированно, для органичности
    NAG_COOLDOWN_SECONDS = 4 * 3600  # не чаще раза в 4 часа — напоминание про незакрытые вопросы
    # Сколько подряд сообщений «мимо теста» (ОФФТОП, не reply) терпим, прежде чем
    # перестать гонять каждое сообщение в LLM-проверку ответа (см. submit_quiz_answer).
    QUIZ_OFFTOPIC_LIMIT = 3
    # Сколько хранить остановленные (неактивные) сессии, прежде чем вычистить из learning.json.
    INACTIVE_SESSION_TTL_SECONDS = 7 * 86400

    _SENTENCE_END_RE = re.compile(r'[.!?…»"\)\]]\s*$')

    @classmethod
    def _looks_truncated(cls, text: str) -> bool:
        """Эвристика: похоже, что ответ оборван по лимиту max_tokens посреди мысли,
        а не закончен естественным образом (знак завершения предложения в конце).
        Примечание: ранее здесь была хрупкая проверка парности **/```, но она давала
        ложно-позитивы на нормально завершённых текстах с одиночной ** (например, курсив
        персоны) — это запускало лишнюю догенерацию и текст задваивался. Оставлен только
        надёжный сигнал: отсутствие знака завершения фразы."""
        t = (text or "").rstrip()
        if not t:
            return False
        return not cls._SENTENCE_END_RE.search(t)

    def _get_response_complete(
        self, messages: list, *, temperature: float, max_tokens: int,
        top_p: float = 0.9, max_continuations: int = 2,
    ) -> Optional[str]:
        """Обёртка над router.get_response: если ответ похож на обрезанный посреди фразы
        (не хватило max_tokens), просит модель дописать с того же места и склеивает —
        вместо того чтобы молча отправлять пользователю недописанный текст."""
        if not self._router:
            return None
        response = self._side_response(messages, temperature=temperature, max_tokens=max_tokens, top_p=top_p)
        if not response:
            return response
        full = response
        convo = list(messages)
        attempts = 0
        while self._looks_truncated(full) and attempts < max_continuations:
            convo = convo + [
                {"role": "assistant", "content": full},
                {"role": "user", "content": "You stopped mid-sentence. Continue strictly from where you left off — do not repeat what was already written and do not start over."},
            ]
            cont = self._side_response(convo, temperature=temperature, max_tokens=max_tokens, top_p=top_p)
            if not cont:
                break
            full += cont
            attempts += 1
        return full

    def __init__(self, context: str = "default", config: Optional[dict] = None):
        self.context = context
        self._base_dir = Path(f"data/{context}/learning")
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._file = self._base_dir / "learning.json"
        self._lock = threading.Lock()

        cfg = config or {}
        self.default_quiz_every = int(cfg.get("quiz_every", self.DEFAULT_QUIZ_EVERY))
        self.default_silence_threshold = int(cfg.get("silence_threshold", self.DEFAULT_SILENCE_THRESHOLD))
        self.min_interval = float(cfg.get("min_interval_seconds", 300))
        self.max_interval = float(cfg.get("max_interval_seconds", 2592000))

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._sender = None
        self._router = None

        self._persona = None
        self._local_router = None
        self._memory = None

        self._sessions: List[dict] = []
        self._load()

        # Состояние диалога «как часто?» — in-memory (теряется на рестарте, это ок)
        self._setup_state: Dict[str, dict] = {}

        # Реестр message_id → признак, что это сообщение бота было вопросом (частота уроков /
        # «продолжаем?» / тест). Нужен, чтобы понять, отвечает ли пользователь reply-ом именно
        # на бот-вопрос, а не пишет произвольное сообщение. In-memory, обрезается до последних N.
        self._question_msgs: Dict[str, list] = {}
        self._QUESTION_MSG_LIMIT = 20

    def _side_response(self, messages, **kw):
        """Побочный вызов LLM (уроки/квизы): fallback-цепочка основного
        роутера МИНУС основной провайдер; веб-чат — отдельный side-чат."""
        if not self._router:
            return None
        return self._router.get_response(
            messages, exclude_provider=self._router.active_provider,
            webchat_channel="side", **kw)
    # ── реестр сообщений-вопросов бота ──

    def register_question_message(self, chat_id: str, message_id: int):
        """Отмечает, что сообщение бота с этим message_id — это вопрос (частота/continue/тест).
        Потом is_reply_to_question проверит, ответил ли пользователь reply-ом именно на него."""
        if not message_id:
            return
        chat_id = str(chat_id)
        with self._lock:
            bucket = self._question_msgs.setdefault(chat_id, [])
            if message_id not in bucket:
                bucket.append(message_id)
            # Обрезаем до последних N
            if len(bucket) > self._QUESTION_MSG_LIMIT:
                self._question_msgs[chat_id] = bucket[-self._QUESTION_MSG_LIMIT:]

    def is_reply_to_question(self, chat_id: str, message_id: Optional[int]) -> bool:
        """ True если message_id — это одно из последних сообщений-вопросов бота в чате."""
        if not message_id:
            return False
        with self._lock:
            return message_id in self._question_msgs.get(str(chat_id), [])

    # ── инъекция зависимостей ──

    def set_sender(self, sender):
        self._sender = sender

    def set_routers_persona(self, router, persona, local_router=None):
        """Передаёт router, persona (и опционально local_router) для генерации уроков."""
        self._router = router
        self._persona = persona
        self._local_router = local_router

    def set_memory(self, memory):
        """Передаёт MemoryManager, чтобы сохранять уроки в STM (контекст для последующих вопросов)."""
        self._memory = memory

    # ── persistence ──

    def _load(self):
        if self._file.exists():
            try:
                self._sessions = json.loads(self._file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[Learning] Не удалось загрузить: {e}")
                self._sessions = []
        else:
            self._sessions = []

    def _save(self):
        """Атомарная запись: пишем во временный файл, затем переименовываем.
        Защищает от порчи файла (0 байт / битый JSON) при аварийном завершении процесса
        в момент записи — иначе _load молча вернёт пустой список и все сессии «исчезнут».
        Заодно вычищает давно остановленные сессии — иначе learning.json растёт бесконечно."""
        # Неактивные сессии старше TTL выкидываем (точного stopped_at не храним,
        # created_at для мёртвой истории достаточно).
        cutoff = time.time() - self.INACTIVE_SESSION_TTL_SECONDS
        self._sessions = [
            s for s in self._sessions
            if s.get("active") or s.get("created_at", 0) >= cutoff
        ]
        try:
            import os, tempfile
            data = json.dumps(self._sessions, ensure_ascii=False, indent=2)
            fd, tmp_path = tempfile.mkstemp(dir=str(self._base_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp_path, self._file)
            except Exception:
                # Если переименование не удалось — чистим временный файл
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(f"[Learning] Не удалось сохранить: {e}")

    # ── setup-диалог (как часто?) ──

    def begin_setup(self, chat_id: str, subject: str, user_id: str, user_name: str):
        with self._lock:
            self._setup_state[str(chat_id)] = {
                "subject": subject,
                "user_id": user_id,
                "user_name": user_name,
                # Когда задан вопрос «как часто?» — при конфликте с pending-напоминанием
                # ответ о периодичности получает тот, кто спросил ПОЗЖЕ (см. process_message).
                "asked_at": time.time(),
            }

    # Setup-состояние «как часто?» живёт ограниченное время — иначе любое сообщение
    # с временнОй лексикой спустя дни неожиданно создаст курс
    SETUP_TTL_SECONDS = 3600

    def get_setup_state(self, chat_id: str) -> Optional[dict]:
        with self._lock:
            state = self._setup_state.get(str(chat_id))
            if state and time.time() - state.get("asked_at", 0) > self.SETUP_TTL_SECONDS:
                self._setup_state.pop(str(chat_id), None)
                return None
            return state

    def clear_setup(self, chat_id: str):
        with self._lock:
            self._setup_state.pop(str(chat_id), None)

    # ── API сессий ──

    def commit_session(self, chat_id: str, interval_seconds: float, topic_id: Optional[int] = None) -> Optional[dict]:
        """Создаёт активную сессию из setup-состояния. Возвращает сессию или None.

        Несколько параллельных курсов на один chat_id — норма: если пользователь уже
        учит тему А и просит «научи меня Б», сессия по Б добавляется, а не заменяет А.
        Заменяется (пересоздаётся) только сессия с ТОЙ ЖЕ темой — чтобы повторное
        «научи меня X», пока X уже идёт, не плодило дубликаты одного курса.
        """
        chat_id = str(chat_id)
        setup = self.get_setup_state(chat_id)
        if not setup:
            return None
        self.clear_setup(chat_id)

        interval = max(self.min_interval, min(self.max_interval, interval_seconds))
        now = time.time()
        # Нормализуем тему в именительный падёж («китайскому» → «китайский язык»)
        raw_subject = setup.get("subject", "")
        subject = self._normalize_subject(raw_subject)
        # LANGUAGE vs TOPIC — определяет, как закреплять материал в обычном разговоре
        # (см. get_reinforcement_hint): словами/фразами или уместной отсылкой без объяснений.
        course_kind = self._classify_course_kind(subject)
        session = {
            "session_id": uuid.uuid4().hex,
            "chat_id": chat_id,
            "user_id": setup.get("user_id", ""),
            "user_name": setup.get("user_name", "Пользователь"),
            "topic_id": topic_id,
            "subject": subject,
            "course_kind": course_kind,
            "interval_seconds": interval,
            "next_lesson_at": now + interval,
            "lesson_count": 0,
            "covered_topics": [],
            "learned_vocabulary": [],
            "last_reinforced_at": None,
            "last_nag_at": None,
            "quiz_every": self.default_quiz_every,
            "silence_threshold": self.default_silence_threshold,
            "consecutive_silences": 0,
            "asked_continue": False,
            "quiz_pending": None,
            "quiz_set_at": None,
            "quiz_offtopic_count": 0,
            "continue_asked_at": None,
            "active": True,
            "created_at": now,
        }
        with self._lock:
            # Заменяем существующую активную сессию ТОЛЬКО с той же темой у этого чата —
            # остальные параллельные курсы не трогаем.
            self._sessions = [
                s for s in self._sessions
                if not (s["chat_id"] == chat_id and s.get("active") and self._same_subject(s.get("subject", ""), subject))
            ]
            self._sessions.append(session)
            self._save()
        logger.info(f"[Learning] Сессия создана: chat={chat_id} subject='{session['subject']}' интервал={interval:.0f}с")
        return session

    @staticmethod
    def _same_subject(a: str, b: str) -> bool:
        return a.strip().casefold() == b.strip().casefold()

    def get_sessions(self, chat_id: str) -> List[dict]:
        """Все активные сессии (параллельные курсы) этого чата."""
        chat_id = str(chat_id)
        with self._lock:
            return [s for s in self._sessions if s["chat_id"] == chat_id and s.get("active")]

    def get_session(self, chat_id: str, session_id: Optional[str] = None, subject: Optional[str] = None) -> Optional[dict]:
        """Возвращает активную сессию чата.
        - session_id указан → ищем именно её (работает даже если сессия неактивна).
        - subject указан → ищем активную сессию с таким же subject.
        - ничего не указано → если у чата ровно одна активная сессия, возвращаем её
          (для обратной совместимости вызовов, где раньше на chat_id была одна сессия);
          если сессий несколько — вернём None, вызывающий код должен уточнить, о какой
          именно теме идёт речь (через session_id/subject), иначе решения будут случайными.
        """
        chat_id = str(chat_id)
        with self._lock:
            if session_id:
                for s in self._sessions:
                    if s.get("session_id") == session_id:
                        return s
                return None
            active = [s for s in self._sessions if s["chat_id"] == chat_id and s.get("active")]
            if subject:
                for s in active:
                    if self._same_subject(s.get("subject", ""), subject):
                        return s
                return None
            if len(active) == 1:
                return active[0]
            return None

    def resolve_pending_target(self, chat_id: str, user_text: str) -> Optional[dict]:
        """Определяет, к какому из «ожидающих ответа» курсов этого чата относится
        сообщение пользователя — тест ИЛИ вопрос «продолжаем?» по любой из параллельных тем.
        Возвращает сессию с добавленным полем '_pending_kind': 'quiz' | 'continue', либо
        None, если ни один курс сейчас ничего не ждёт.

        Раньше continue-вопрос и тест проверялись отдельно, друг за другом (сначала всегда
        continue) — из-за этого при курсах А (тест) и Б (продолжаем?) одновременно ЛЮБОЙ
        ответ уходил в Б, даже если пользователь явно отвечал на тест по А. Здесь оба вида
        ожидания собираются в один список кандидатов и, если он не единственный,
        разрешаются одним LLM-вызовом по смыслу сообщения — а не по тому, что произошло позже."""
        candidates: List[tuple] = []
        for s in self.get_sessions(chat_id):
            # asked_continue проверяем ПЕРВЫМ: «продолжаем?» может быть задан только
            # после теста (по молчанию пользователя), т.е. это всегда более поздний
            # и актуальный вопрос. Штатно quiz_pending при отправке «продолжаем?»
            # закрывается (см. _send_continue_question), но сессии, сохранённые до
            # этого исправления, могут хранить оба флага — для них приоритет критичен:
            # иначе «да/нет» уходило в проверку теста вместо resolve_continue.
            if s.get("asked_continue"):
                candidates.append((s, "continue"))
            elif s.get("quiz_pending"):
                candidates.append((s, "quiz"))
        if not candidates:
            return None
        if len(candidates) == 1:
            s, kind = candidates[0]
            return {**s, "_pending_kind": kind}

        def _fallback_most_recent():
            ordered = sorted(
                candidates,
                key=lambda sk: sk[0].get("quiz_set_at") or sk[0].get("continue_asked_at") or 0,
                reverse=True,
            )
            s, kind = ordered[0]
            return {**s, "_pending_kind": kind}

        if not self._router:
            return _fallback_most_recent()

        options_desc = []
        for i, (s, kind) in enumerate(candidates, 1):
            if kind == "quiz":
                q = (s.get("quiz_pending") or {}).get("question", "")
                options_desc.append(f"{i}. Topic \"{s.get('subject', '')}\" — waiting for a quiz answer: {q}")
            else:
                options_desc.append(f"{i}. Topic \"{s.get('subject', '')}\" — waiting for a yes/no answer to \"continue the course?\"")

        messages = [
            {"role": "system", "content": (
                "The user is studying several topics in parallel. Determine which of the "
                "listed options the user's message RELATES to in meaning. "
                "Answer STRICTLY with one number — the option number (for example: 2). "
                "If the message is clearly not about any of the options — answer 0. Write nothing else."
            )},
            {"role": "user", "content": "Options:\n" + "\n".join(options_desc) + f"\n\nUser message: {user_text}"},
        ]
        response = self._side_response(messages, temperature=0.0, max_tokens=5, top_p=1.0)
        idx = None
        if response:
            m = re.search(r"\d+", response)
            if m:
                idx = int(m.group())
        if idx and 1 <= idx <= len(candidates):
            s, kind = candidates[idx - 1]
            return {**s, "_pending_kind": kind}
        # LLM не смог определить (0/пусто/не число) — fallback на прежнюю эвристику
        return _fallback_most_recent()

    def find_pending_quiz_session(self, chat_id: str) -> Optional[dict]:
        """Среди ВСЕХ параллельных курсов этого чата ищет тот, где открыт тест.
        Если открытых тестов несколько сразу — берём тот, что был задан позже (обычно
        именно на него отвечают в первую очередь); это эвристика, не точная привязка
        ответа к теме."""
        candidates = [s for s in self.get_sessions(chat_id) if s.get("quiz_pending")]
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.get("quiz_set_at") or 0, reverse=True)
        return candidates[0]

    def find_awaiting_continue_session(self, chat_id: str) -> Optional[dict]:
        """Аналогично find_pending_quiz_session, но для вопроса «продолжаем?»."""
        candidates = [s for s in self.get_sessions(chat_id) if s.get("asked_continue")]
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.get("continue_asked_at") or 0, reverse=True)
        return candidates[0]

    def has_pending_quiz(self, chat_id: str) -> bool:
        return bool(self.find_pending_quiz_session(chat_id))

    def is_awaiting_continue(self, chat_id: str) -> bool:
        return bool(self.find_awaiting_continue_session(chat_id))

    def stop_session(self, chat_id: str, session_id: Optional[str] = None, subject: Optional[str] = None):
        """Останавливает курс. Если у чата несколько параллельных курсов, нужно указать
        session_id или subject — иначе (при >1 активных сессий и без уточнения) не
        останавливаем ничего, чтобы не остановить не тот курс наугад."""
        with self._lock:
            targets = [s for s in self._sessions if s.get("active") and (
                (session_id and s.get("session_id") == session_id) or
                (not session_id and s["chat_id"] == str(chat_id) and (
                    self._same_subject(s.get("subject", ""), subject) if subject else True
                ))
            )]
            if not session_id and not subject:
                active_for_chat = [s for s in self._sessions if s["chat_id"] == str(chat_id) and s.get("active")]
                if len(active_for_chat) > 1:
                    logger.warning(f"[Learning] stop_session: у чата {chat_id} несколько курсов, нужен subject/session_id — ничего не остановлено")
                    return []
            for s in targets:
                s["active"] = False
                s["quiz_pending"] = None
                s["asked_continue"] = False
            if targets:
                self._save()
                logger.info(f"[Learning] Сессия остановлена: chat={chat_id} ({len(targets)} шт.)")

    def _set_session(self, chat_id: str, session_id: Optional[str] = None, **fields):
        """Обновляет поля активной сессии и сохраняет. Возвращает сессию или None.
        Если session_id не передан и у чата несколько активных сессий — ничего не
        обновляем (иначе можно случайно поправить не тот курс)."""
        with self._lock:
            if session_id:
                for s in self._sessions:
                    if s.get("session_id") == session_id:
                        s.update(fields)
                        self._save()
                        return s
                return None
            matches = [s for s in self._sessions if s["chat_id"] == str(chat_id) and s.get("active")]
            if len(matches) != 1:
                return None
            matches[0].update(fields)
            self._save()
            return matches[0]

    # ── обратная связь от пользователя ──

    def record_user_activity(self, chat_id: str):
        """Сбрасывает счётчик молчания. Вызывается из process_message на КАЖДОЕ сообщение
        пользователя в активной сессии — включая ответы на контрольные вопросы/уточнения.

        Флаг asked_continue здесь намеренно НЕ трогаем: пока сессия ждёт явного да/нет на
        «продолжаем?», это решает только resolve_continue() — иначе при decision=UNKNOWN
        комментарий "оставляем asked_continue=True" в resolve_continue не имеет смысла,
        т.к. флаг уже был бы сброшен здесь раньше по конвейеру обработки сообщения.

        При нескольких параллельных курсах сбрасываем молчание для ВСЕХ курсов этого
        чата — раз пользователь написал хоть что-то, он «на связи» для всех тем сразу.
        """
        for s in self.get_sessions(chat_id):
            if s.get("consecutive_silences", 0) > 0:
                self._set_session(chat_id, session_id=s.get("session_id"), consecutive_silences=0)

    def resolve_continue(self, chat_id: str, decision: str, session_id: Optional[str] = None):
        """Обработка ответа пользователя на «продолжаем?».
        session_id — если не передан, берём курс, который last спросил «продолжаем?»
        (см. find_awaiting_continue_session) — актуально при нескольких параллельных курсах."""
        if not session_id:
            target = self.find_awaiting_continue_session(chat_id)
            session_id = target.get("session_id") if target else None
        if decision == "NO":
            self.stop_session(chat_id, session_id=session_id)
        elif decision == "YES":
            s = self.get_session(chat_id, session_id=session_id) if session_id else self.get_session(chat_id)
            interval = s.get("interval_seconds", self.min_interval) if s else self.min_interval
            # Важно: next_lesson_at был заранее отодвинут на interval+86400 в _loop(),
            # когда отправлялся вопрос «продолжаем?» — здесь пересчитываем заново
            # от текущего момента, иначе следующий урок придёт по «испорченному» графику.
            self._set_session(
                chat_id,
                session_id=session_id,
                asked_continue=False,
                consecutive_silences=0,
                next_lesson_at=time.time() + interval,
            )
        # UNKNOWN — оставляем asked_continue=True, ждём явного ответа

    def get_nag_guard(self, chat_id: str) -> str:
        """Персона сама (без нашей инструкции) любит напоминать о незакрытых контрольных
        вопросах прошлого урока — и делает это в КАЖДОМ ответе, что ощущается навязчиво.
        Полностью запрещать это неверно (напоминание иногда уместно и полезно для учёбы) —
        поэтому здесь кулдаун на курс: пока он не истёк, явно запрещаем упоминание в этом
        ответе; как истёк — ничего не говорим, оставляя персоне свободу упомянуть (или нет)
        по своему усмотрению, как обычно."""
        sessions = self.get_sessions(chat_id)
        if not sessions:
            return ""
        now = time.time()

        def _cooled_down(s: dict) -> bool:
            # Если ни разу не напоминали (last_nag_at is None) — кулдауна нет, разрешаем.
            last = s.get("last_nag_at")
            if last is None:
                return True
            return (now - last) >= self.NAG_COOLDOWN_SECONDS

        ready = [s for s in sessions if _cooled_down(s)]
        if ready:
            # Кулдаун истёк хотя бы для одного курса — разрешаем, отмечаем его как
            # "напомнили" (даже если модель в итоге не упомянёт — это лишь верхний лимит
            # частоты, не гарантия, что напоминание случится).
            for s in ready:
                self._set_session(chat_id, session_id=s.get("session_id"), last_nag_at=now)
            return ""

        return (
            "You may feel the urge to remind about the unanswered review questions "
            "from the previous lesson or that the request is \"off the study topic\" — you have already done that "
            "recently. DO NOT repeat it in this reply (no \"how many times this session\", "
            "\"pattern confirmed\" etc.) — just answer the message itself."
        )

    def get_reinforcement_hint(self, chat_id: str) -> Optional[str]:
        """Для ОБЫЧНОГО (не учебно-административного) сообщения — иногда подсказывает
        персоне вплести в ответ что-то из активных курсов, для закрепления материала через
        повторяющееся воздействие в разговоре, а не только в уроках. Намеренно редкое и
        необязательное:
        - кулдаун на курс (не чаще раза в REINFORCE_COOLDOWN_SECONDS) — чтобы не долбить
          одним и тем же почти каждым сообщением;
        - вероятность даже после кулдауна (REINFORCE_PROBABILITY) — чтобы это ощущалось как
          органичная случайность, а не расписание;
        - инструкция персоне явно разрешает пропустить вставку, если она не ложится
          естественно в контекст разговора — принудительная вставка на любую цену испортит
          эффект куда быстрее, чем редкий пропуск.
        LANGUAGE-курсы закрепляются словами/фразами; остальные — уместной отсылкой/метафорой
        БЕЗ объяснения, что она значит (см. пример «третий ключ» для криптографии)."""
        import random
        eligible = [
            s for s in self.get_sessions(chat_id)
            if int(s.get("lesson_count", 0)) >= 1 and (s.get("covered_topics") or s.get("learned_vocabulary"))
        ]
        if not eligible:
            return None

        now = time.time()

        def _cooled_down(s: dict) -> bool:
            last = s.get("last_reinforced_at") or s.get("created_at") or 0
            return (now - last) >= self.REINFORCE_COOLDOWN_SECONDS

        eligible = [s for s in eligible if _cooled_down(s)]
        if not eligible:
            return None
        if random.random() > self.REINFORCE_PROBABILITY:
            return None

        session = random.choice(eligible)
        subject = session.get("subject", "")

        if session.get("course_kind") == "LANGUAGE" and session.get("learned_vocabulary"):
            pick = random.sample(session["learned_vocabulary"], k=min(2, len(session["learned_vocabulary"])))
            hint = (
                f"The user is taking a \"{subject}\" course. If it fits NATURALLY into your "
                f"reply — use 1 of these previously learned words/phrases, aptly, "
                f"without translation and without noting it is from a lesson: {'; '.join(pick)}. "
                "If it doesn't fit organically — just skip it, don't force it."
            )
        elif session.get("covered_topics"):
            pick = random.choice(session["covered_topics"])
            hint = (
                f"The user is taking a \"{subject}\" course, recently covered: \"{pick}\". "
                "If it fits NATURALLY into your reply — you may make a short reference "
                "to this concept/metaphor in your own style, WITHOUT explaining what it means "
                "(the user will either get it or just move past — both are fine). "
                "If it doesn't fit organically — just skip it, don't force it."
            )
        else:
            return None

        self._set_session(chat_id, session_id=session.get("session_id"), last_reinforced_at=now)
        return hint

    def parse_frequency_smart(self, text: str) -> Optional[float]:
        """Парсит частоту уроков: сначала regex (parse_frequency) — он мгновенный, бесплатный
        и не ошибается в арифметике для чётких формулировок вида «каждые 10 минут». Если regex
        не справился (человек сформулировал нестандартно — «через полчасика», с опечаткой и
        т.п.), подключаем LLM как фолбэк, а не основной парсер: интервал уроков — единственное
        место, где ошибка модели бьёт не по качеству реплики, а по реальному расписанию
        отправки, поэтому детерминированный путь всегда в приоритете."""
        delay = parse_frequency(text, min_seconds=self.min_interval, max_seconds=self.max_interval)
        if delay:
            return delay
        return self._parse_frequency_via_llm(text)

    def _parse_frequency_via_llm(self, text: str) -> Optional[float]:
        if not self._router:
            return None
        messages = [
            {"role": "system", "content": (
                "The user is answering the question \"how often should lessons be sent\". Determine "
                "the interval IN SECONDS from their answer. Answer STRICTLY with a single integer "
                "number of seconds and nothing else. If the frequency cannot be understood from the text — answer exactly "
                "UNKNOWN."
            )},
            {"role": "user", "content": text},
        ]
        response = self._side_response(messages, temperature=0.0, max_tokens=10, top_p=1.0)
        if not response:
            return None
        response = response.strip()
        if "UNKNOWN" in response.upper():
            return None
        m = re.search(r"\d+", response)
        if not m:
            return None
        try:
            seconds = float(m.group())
        except ValueError:
            return None
        return _clip(seconds, self.min_interval, self.max_interval)

    def render_setup_reply(self, subject: str, kind: str, delay_text: str = "",
                           user_language: Optional[str] = None) -> str:
        """Короткая изолированная реплика персоны на этапе настройки частоты уроков
        (kind: 'confirmed' — частота распознана и обучение запущено, 'reask' — частоту
        не удалось понять). Как и render_continue_reply — БЕЗ доступа к STM/истории:
        через общий пайплайн модель на этом шаге придумывала небылицы про архитектуру бота
        и сразу выдавала полноценный текст урока вместо короткого подтверждения настройки.

        user_language ('ru'/'en') — язык сообщения пользователя: в изолированный
        вызов не попадает ни одной его реплики, поэтому без этого параметра модель
        не знает язык и отвечает на языке персоны."""
        fallback = {
            "confirmed": f"Хорошо, начинаем! Уроки по теме «{subject}» — {delay_text}.",
            "reask": "Не понял частоту. Как часто присылать уроки — например, «раз в день» или «каждые 2 часа»?",
        }.get(kind, "Хорошо.")
        if not self._router:
            return fallback

        task = {
            "confirmed": (
                f"The user chose the lesson frequency for the topic \"{subject}\": {delay_text}. "
                "The course has just started. Confirm this with ONE short phrase in your own style."
            ),
            "reask": (
                f"The user was answering the question about lesson frequency for the topic \"{subject}\", but "
                "the frequency could not be recognized. Ask again with ONE short phrase in your own "
                "style: how often to send lessons (for example, \"once a day\", \"every 2 hours\")."
            ),
        }.get(kind, "Reply briefly in your own style.")

        lang_line = (
            f"The user's language is {language_name(user_language)}. "
            f"Reply ONLY in {language_name(user_language)}.\n"
            if user_language else
            "Reply in the language of the user's messages.\n"
        )

        messages = [
            {"role": "system", "content": (
                f"{self._persona_block()}\n\n---\n{task}\n"
                f"{lang_line}"
                "FORBIDDEN in this reply: lesson text, lesson number, review questions, "
                "any study content, and reasoning about the bot's architecture and "
                "capabilities/limitations. Only the reaction to the schedule setup itself."
            )},
            {"role": "user", "content": f"Topic: \"{subject}\"." if subject else "React."},
        ]
        response = self._get_response_complete(messages, temperature=0.6, max_tokens=100, top_p=0.9)
        return (response or fallback).strip()

    def classify_continue_answer_smart(self, text: str) -> str:
        """YES/NO по regex (быстро, надёжно и бесплатно). Если неоднозначно — уточняем через
        LLM, к какой из двух категорий это относится:
        - UNKNOWN — похоже на попытку ответить да/нет, но неоднозначно («наверное», «хз»);
        - OFFTOPIC — сообщение вообще не про вопрос «продолжаем?», пользователь пишет о чём-то
          другом (шутит, спрашивает что-то не по теме и т.п.).
        Разница важна: раньше ЛЮБОЕ неясное сообщение считалось UNKNOWN и бот переспрашивал
        «да или нет?», игнорируя то, что человек реально написал — при активном continue-вопросе
        любая шутка или посторонний вопрос утыкались в зацикленный переспрос. OFFTOPIC вместо
        этого оставляет вопрос висеть НЕТРОНУТЫМ и пускает сообщение в обычную обработку —
        аналогично тому, как офф-топ уже обрабатывается в submit_quiz_answer."""
        fast = classify_continue_answer(text)
        if fast in ("YES", "NO"):
            return fast
        if not self._router:
            return fast
        result = self._llm_clean_line(
            system_prompt=(
                "The user was asked \"continue the course? (yes/no)\". Classify their "
                "message: (a) an ambiguous attempt to answer yes/no (for example, \"maybe\", "
                "\"dunno\") — answer UNKNOWN; (b) the message is not about this question at all, "
                "the user writes/asks about something else — answer OFFTOPIC. "
                "Answer STRICTLY with one word: UNKNOWN or OFFTOPIC."
            ),
            user_content=text,
            max_tokens=6,
        )
        if result and "OFFTOPIC" in result.upper():
            return "OFFTOPIC"
        return "UNKNOWN"

    def render_continue_reply(self, chat_id: str, decision: str, session_id: Optional[str] = None,
                              user_language: Optional[str] = None) -> str:
        """Короткая реплика персоны на решение «продолжать обучение?» — YES/NO/UNKNOWN.
        Намеренно ИЗОЛИРОВАННЫЙ вызов (без STM/истории диалога, без прочего контекста):
        когда эту реплику генерировал общий пайплайн бота (persona.prepare_messages со всей
        историей), модель, видя в контексте прошлые уроки, игнорировала инструкцию
        «не пиши урок» и заново выдавала полноценный урок вместо короткого подтверждения.
        Здесь модель физически не видит ничего, кроме темы и решения — переигрывать урок ей
        неоткуда. user_language ('ru'/'en') — язык сообщения пользователя: без него модель
        не знает язык (реплик пользователя в вызове нет) и отвечает на языке персоны.

        session_id стоит передавать явно (полученный ДО вызова resolve_continue) — иначе,
        если у чата несколько параллельных курсов или resolve_continue уже деактивировал
        сессию (decision=NO), тему определить будет не из чего."""
        s = self.get_session(chat_id, session_id=session_id) if session_id else self.get_session(chat_id)
        subject = (s or {}).get("subject", "")
        fallback = {
            "YES": "Хорошо, продолжаем!",
            "NO": "Понял, останавливаем обучение.",
        }.get(decision, "Не понял — продолжаем или нет? Напиши «да» или «нет».")
        if not self._router:
            return fallback

        task = {
            "YES": (
                "The user confirmed they want to continue the course after a pause. "
                "Reply with ONE short phrase in your own style, just confirm the continuation."
            ),
            "NO": (
                "The user decided to stop the course. Reply with ONE short phrase "
                "in your own style, confirm the stop."
            ),
        }.get(decision, (
            "It was not possible to understand whether the user wants to continue the course or not. "
            "Reply with ONE short phrase in your own style and ask again \"yes\" or \"no\"."
        ))

        lang_line = (
            f"The user's language is {language_name(user_language)}. "
            f"Reply ONLY in {language_name(user_language)}.\n"
            if user_language else
            "Reply in the language of the user's messages.\n"
        )

        messages = [
            {"role": "system", "content": (
                f"{self._persona_block()}\n\n---\n{task}\n"
                f"{lang_line}"
                "FORBIDDEN in this reply: lesson text, lesson number, review questions, "
                "the next lesson's topic or any study content. Only the reaction itself."
            )},
            {"role": "user", "content": f"Course topic: \"{subject}\"." if subject else "React."},
        ]
        response = self._get_response_complete(messages, temperature=0.6, max_tokens=100, top_p=0.9)
        return (response or fallback).strip()

    def submit_quiz_answer(self, chat_id: str, answer_text: str, session_id: Optional[str] = None,
                           is_reply: bool = False) -> Optional[str]:
        """Проверяет сообщение пользователя по тесту через LLM.
        Возвращает фидбек, если это была попытка ответить — тест засчитывается и закрывается.
        Возвращает None, если сообщение не по теме вопроса (ученик сменил тему/задал другой
        вопрос) — тест остаётся открытым (quiz_pending не сбрасывается), а вызывающий код
        должен обработать сообщение как обычное, не как ответ на тест.
        session_id — если не передан, берём курс с самым недавно заданным тестом
        (см. find_pending_quiz_session) — актуально при нескольких параллельных курсах.
        is_reply — сообщение пришло Telegram-reply на сам вопрос теста: сильный сигнал,
        оцениваем всегда. Без reply после QUIZ_OFFTOPIC_LIMIT подряд сообщений «мимо»
        (ОФФТОП) проверку через LLM пропускаем: пользователь явно не отвечает на тест,
        а каждая проверка — лишний вызов LLM и риск съесть обычное сообщение ложной
        оценкой. Тест при этом остаётся открытым — ответить можно reply-ом."""
        if not session_id:
            target = self.find_pending_quiz_session(chat_id)
            session_id = target.get("session_id") if target else None
        s = self.get_session(chat_id, session_id=session_id) if session_id else self.get_session(chat_id)
        if not s or not s.get("quiz_pending"):
            return None
        if not is_reply and int(s.get("quiz_offtopic_count", 0)) >= self.QUIZ_OFFTOPIC_LIMIT:
            return None
        quiz = s["quiz_pending"]
        is_offtopic, feedback = self._evaluate_quiz(quiz, answer_text)
        if is_offtopic:
            self._set_session(
                chat_id, session_id=session_id,
                quiz_offtopic_count=int(s.get("quiz_offtopic_count", 0)) + 1,
            )
            return None
        self._set_session(chat_id, session_id=session_id, quiz_pending=None, quiz_offtopic_count=0)
        return feedback

    # ── генерация контента (через LLM) ──

    def _persona_block(self) -> str:
        if self._persona:
            return self._persona.system_prompt.strip()
        return ""

    # ── LLM-хелперы для нормализации ──

    def _llm_clean_line(self, system_prompt: str, user_content: str, max_tokens: int = 40) -> Optional[str]:
        """Базовый вызов локальной (или основной) LLM для короткой очистки текста."""
        router = self._local_router or self._router
        if not router:
            return None
        # task= — только локальному роутеру (движок задачи «learning»);
        # основному роутеру параметр незнаком
        task_kw = {"task": "learning"} if router is self._local_router else {}
        try:
            response = router.get_response(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.0,
                max_tokens=max_tokens,
                **task_kw,
            )
            if not response:
                return None
            cleaned = response.strip().strip('"\'""«»').strip()
            # Жёсткая валидация: не эхо промпта, разумная длина
            if 2 <= len(cleaned) <= 120:
                return cleaned
        except Exception as e:
            logger.debug(f"[Learning] LLM-очистка не удалась: {e}")
        return None

    def _normalize_subject(self, raw: str) -> str:
        """Приводит тему в именительный падёж, кратко: 'китайскому' → 'китайский язык'."""
        if not raw or len(raw.strip()) < 2:
            return raw or ""
        result = self._llm_clean_line(
            system_prompt=(
                "Normalize the course subject into its base (nominative) form, briefly (2-6 words), without verbs. "
                "For example: 'китайскому' → 'китайский язык', 'программированию на python' → 'программирование на Python'. "
                "Keep the language of the input. The answer is only the subject, without quotes or explanations."
            ),
            user_content=raw.strip(),
        )
        if result:
            logger.info(f"[Learning] subject нормализован: '{raw}' -> '{result}'")
            return result
        return raw.strip()

    def _extract_topic(self, lesson_text: str) -> str:
        """Извлекает ОДНУ короткую тему урока в именительном падеже (до ~6 слов)."""
        if not lesson_text:
            return ""
        result = self._llm_clean_line(
            system_prompt=(
                "Determine the ONE main topic of the study text. "
                "The answer is a short phrase in base (nominative) form (up to 6 words), "
                "in the language of the source text. "
                "For example: 'Пиньинь и тоны', 'Цикл for в Python'. "
                "Only the topic, without quotes or explanations."
            ),
            user_content=lesson_text[:1500],
            max_tokens=25,
        )
        if result:
            return result
        # Fallback: первая не-разметочная строка
        return self._strip_markdown_topic(lesson_text)

    @staticmethod
    def _strip_markdown_topic(text: str) -> str:
        """Fallback-эвристика: берёт первую строку без разметки персоны (*...* / **...**)."""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # Пропускаем действия персоны (курсив) и внутренние вставки (звёздочки)
            if line.startswith("*") or line.startswith("#"):
                continue
            # Чистим ведущие маркеры списков
            line = re.sub(r"^[-*\d.)\s]+", "", line).strip()
            if len(line) >= 3:
                return line[:60]
        return ""

    def _classify_course_kind(self, subject: str) -> str:
        """LANGUAGE — курс живого/иностранного языка (испанский, китайский, латынь) — для
        таких курсов закрепление уместно словами/фразами в обычной речи.
        TOPIC — любая другая тема (в т.ч. языки программирования, технические предметы,
        науки) — для них закрепление уместно уместной отсылкой/метафорой, а не словарём.
        Ключевые слова вроде «язык» ненадёжны («язык программирования» — не язык в этом
        смысле), поэтому решает LLM, а не regex."""
        if not subject:
            return "TOPIC"
        result = self._llm_clean_line(
            system_prompt=(
                "Is this a course for learning a NATURAL/foreign language (for example, Spanish, "
                "Chinese, Latin) or some other topic (including programming languages, "
                "technical subjects, sciences, arts, etc.)? "
                "Answer STRICTLY with one word: LANGUAGE or TOPIC."
            ),
            user_content=f"Course subject: \"{subject}\"",
            max_tokens=6,
        )
        if result and "LANGUAGE" in result.upper():
            return "LANGUAGE"
        return "TOPIC"

    def _extract_vocabulary(self, lesson_text: str) -> List[str]:
        """Достаёт 2-4 ключевых слова/фразы урока (для курсов живого языка) в формате
        'слово/фраза — краткий перевод или смысл' — потом естественно вплетаются в обычный
        разговор для закрепления (см. get_reinforcement_hint)."""
        router = self._local_router or self._router
        if not lesson_text or not router:
            return []
        task_kw = {"task": "learning"} if router is self._local_router else {}
        try:
            response = router.get_response(
                messages=[
                    {"role": "system", "content": (
                        "From the study text, pick the 2-4 most important new words/phrases of the lesson. "
                        "Each on its own line, in the format 'word/phrase — brief "
                        "translation or meaning'. Write nothing else."
                    )},
                    {"role": "user", "content": lesson_text[:1500]},
                ],
                temperature=0.3,
                max_tokens=150,
                **task_kw,
            )
        except Exception as e:
            logger.debug(f"[Learning] Извлечение словаря не удалось: {e}")
            return []
        if not response:
            return []
        items = [ln.strip(" \t-•*") for ln in response.splitlines() if ln.strip(" \t-•*")]
        return items[:4]

    def _generate_lesson_text(self, session: dict) -> Optional[dict]:
        """
        Генерирует структурированный урок. Возвращает dict:
        {topic, questions (list[str]), lesson (str)} или None.
        Учебный материал генерируется БЕЗ характера персоны — это чистый учебный контент,
        без действий/курсивов персонажа.
        """
        if not self._router:
            return None
        subject = session.get("subject", "")
        lesson_num = session.get("lesson_count", 0) + 1
        covered = session.get("covered_topics", [])
        covered_str = ", ".join(covered[-8:]) if covered else "nothing yet"

        messages = [
            {"role": "system", "content": (
                f"You are an experienced teacher. You teach the subject \"{subject}\". Lesson #{lesson_num}.\n"
                f"Previously covered: {covered_str}.\n"
                "Give ONE small lesson on a NEW subtopic (do not repeat what was covered). "
                "Explain clearly, with examples. This is pure study material — NO actions, "
                "NO character lines, NO italics or asterisks, only content. "
                "Write the lesson in the language of the course subject (if the subject is in Russian, "
                "write in Russian; if in English, write in English).\n\n"
                "Answer STRICTLY in this format (each field on a new line):\n"
                "TOPIC: <short lesson topic, up to 6 words, base form>\n"
                "QUESTIONS: <2-3 review questions separated by semicolons>\n"
                "VOCAB: <2-4 short \"reinforcement units\" separated by semicolons — "
                "if this is a LANGUAGE, give real words/phrases in the studied language with a brief "
                "translation after \" — \" (for example: 你好 — привет); if it is NOT a language (tech, "
                "science, etc.), give a short vivid term or metaphor from the lesson that "
                "can be naturally mentioned in ordinary conversation without explanations (for example: "
                "third key; session key). No explanations, only the list itself.>\n"
                "LESSON:\n<full lesson text, markdown allowed>"
            )},
            {"role": "user", "content": f"Give lesson #{lesson_num} on the subject \"{subject}\"."},
        ]
        response = self._get_response_complete(messages, temperature=0.6, max_tokens=2200, top_p=0.9)
        if not response or len(response.strip()) < 20:
            # Fallback: простая генерация без жёсткого формата
            return self._generate_lesson_simple(session)
        parsed = self._parse_lesson(response.strip())
        if parsed:
            return parsed
        # Формат не распарсен — пробуем простой fallback
        return self._generate_lesson_simple(session)

    def _generate_lesson_simple(self, session: dict) -> Optional[dict]:
        """Fallback-генерация урока без жёсткого формата — просто учебный текст."""
        if not self._router:
            return None
        subject = session.get("subject", "")
        lesson_num = session.get("lesson_count", 0) + 1
        covered = session.get("covered_topics", [])
        covered_str = ", ".join(covered[-8:]) if covered else "nothing yet"
        messages = [
            {"role": "system", "content": (
                f"You are an experienced teacher of the subject \"{subject}\". Lesson #{lesson_num}. "
                f"Covered: {covered_str}. Give ONE coherent lesson on a new subtopic (3-6 paragraphs), "
                "with examples. Pure study text, without character actions. "
                "Write the lesson in the language of the course subject."
            )},
            {"role": "user", "content": f"Give lesson #{lesson_num}."},
        ]
        response = self._get_response_complete(messages, temperature=0.6, max_tokens=2200, top_p=0.9)
        if not response or len(response.strip()) < 20:
            return None
        text = response.strip()
        # Извлекаем тему из текста (LLM-хелпер), вопросов нет
        topic = self._extract_topic(text)
        return {"topic": topic, "questions": [], "lesson": text}

    @staticmethod
    def _parse_lesson(raw: str) -> Optional[dict]:
        """Разбирает ответ LLM в формате TOPIC/QUESTIONS/VOCAB/LESSON."""
        topic_m = re.search(r"(?:TOPIC|ТЕМА):\s*(.+)", raw, re.IGNORECASE)
        questions_m = re.search(r"(?:QUESTIONS|ВОПРОСЫ):\s*(.+)", raw, re.IGNORECASE)
        vocab_m = re.search(r"(?:VOCAB|VOCABULARY):\s*(.+)", raw, re.IGNORECASE)
        lesson_m = re.search(r"(?:LESSON|УРОК):\s*\n?(.+)$", raw, re.IGNORECASE | re.DOTALL)

        if lesson_m:
            lesson = lesson_m.group(1).strip()
        else:
            # LESSON: не найден — убираем строки-маркеры TOPIC/QUESTIONS/VOCAB, остальное = урок
            cleaned_lines = []
            for line in raw.splitlines():
                if re.match(r"^\s*(TOPIC|ТЕМА|QUESTIONS|ВОПРОСЫ|VOCAB|VOCABULARY|LESSON|УРОК)\s*:", line, re.IGNORECASE):
                    continue
                cleaned_lines.append(line)
            lesson = "\n".join(cleaned_lines).strip()
        topic = topic_m.group(1).strip() if topic_m else ""
        questions_raw = questions_m.group(1).strip() if questions_m else ""
        # Вопросы разделены ; или | или перенесены
        questions = [q.strip() for q in re.split(r"\s*[;|]\s*|\n", questions_raw) if q.strip()]
        vocab_raw = vocab_m.group(1).strip() if vocab_m else ""
        vocab = [v.strip() for v in re.split(r"\s*[;|]\s*|\n", vocab_raw) if v.strip()]
        if not lesson:
            return None
        return {"topic": topic, "questions": questions, "vocab": vocab, "lesson": lesson}

    def _generate_quiz(self, session: dict, simple: bool = False) -> Optional[dict]:
        """Генерирует тест. Возвращает {question, answer, explanation} или None.
        simple=True — упрощённый промпт для повторной попытки."""
        if not self._router:
            return None
        subject = session.get("subject", "")
        covered = session.get("covered_topics", [])
        covered_str = ", ".join(covered[-8:]) if covered else "basic concepts"

        if simple:
            sys_prompt = (
                f"Topic: \"{subject}\". Come up with ONE simple question with a brief answer. "
                "Strict format:\nQUESTION: ...\nANSWER: ...\nEXPLANATION: ..."
            )
            user_msg = "Give a question."
        else:
            sys_prompt = (
                f"You are a teacher. You are testing knowledge of the subject \"{subject}\".\n"
                f"Covered: {covered_str}.\n"
                "Come up with ONE review question on the covered material. "
                "An open question or a task with a short answer (not multiple choice). "
                "Write in the language of the course subject. "
                "Answer STRICTLY in this format (fields on separate lines):\n"
                "QUESTION: <question text>\n"
                "ANSWER: <short correct answer>\n"
                "EXPLANATION: <short explanation>\n"
                "No other text."
            )
            user_msg = "Give a quiz on the topic."

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ]
        response = self._side_response(messages, temperature=0.5, max_tokens=400, top_p=0.9)
        if not response:
            return None

        # Гибкий парсинг: допускаем QUESTION/ВОПРОС, ANSWER/ОТВЕТ
        q = re.search(r"(?:QUESTION|ВОПРОС):\s*(.+?)(?=\n\s*(?:ANSWER|ОТВЕТ|EXPLANATION|ПОЯСНЕНИЕ):|$)", response, re.IGNORECASE | re.DOTALL)
        a = re.search(r"(?:ANSWER|ОТВЕТ):\s*(.+?)(?=\n\s*(?:EXPLANATION|ПОЯСНЕНИЕ):|$)", response, re.IGNORECASE | re.DOTALL)
        e = re.search(r"(?:EXPLANATION|ПОЯСНЕНИЕ):\s*(.+)$", response, re.IGNORECASE | re.DOTALL)
        if not (q and a):
            return None
        return {
            "question": q.group(1).strip(),
            "answer": a.group(1).strip(),
            "explanation": (e.group(1).strip() if e else ""),
        }

    def _evaluate_quiz(self, quiz: dict, user_answer: str) -> tuple:
        """Оценивает сообщение ученика по контрольному вопросу через LLM.
        Возвращает (is_offtopic, feedback_text).
        is_offtopic=True — сообщение НЕ является попыткой ответить на вопрос (ученик сменил
        тему/задал другой вопрос) — в этом случае тест не закрываем и оценку не придумываем."""
        if not self._router:
            correct = quiz.get("answer", "")
            return False, f"Правильный ответ: {correct}. (Оценка недоступна.)"
        persona = self._persona_block()
        messages = [
            {"role": "system", "content": (
                f"{persona}\n\n"
                "---\n"
                "You are a teacher, checking a student's message against the review question.\n"
                "If the message IS genuinely an attempt to answer (even a wrong or "
                "incomplete one), evaluate it.\n"
                "If the message is NOT an attempt to answer — the student changed the subject, asked "
                "a completely different question or writes about something unrelated — do not invent a forced "
                "evaluation, use VERDICT: OFFTOPIC.\n"
                "IMPORTANT: a question FROM the student is NOT an attempt to answer, even if it is about "
                "the quiz topic: clarifying the conditions (\"what do you mean?\", \"why?\", a question "
                "about the material) — VERDICT: OFFTOPIC. The student's question will be answered by the normal dialogue, "
                "not by you — do not close the quiz with a false evaluation.\n"
                "Write the FEEDBACK in the language of the student's messages.\n"
                "Answer STRICTLY in this format:\n"
                "VERDICT: CORRECT | WRONG | PARTIAL | OFFTOPIC\n"
                "FEEDBACK: <short comment; for OFFTOPIC may be left empty>\n"
                "No other text."
            )},
            {"role": "user", "content": (
                f"Question: {quiz.get('question', '')}\n"
                f"Correct answer: {quiz.get('answer', '')}\n"
                f"Explanation: {quiz.get('explanation', '')}\n"
                f"Student's message: {user_answer}"
            )},
        ]
        response = self._get_response_complete(messages, temperature=0.3, max_tokens=500, top_p=0.9)
        if not response:
            return False, f"Правильный ответ был: {quiz.get('answer', '?')}."
        v = re.search(r"VERDICT:\s*(.+?)(?:\n\s*FEEDBACK:|$)", response, re.IGNORECASE | re.DOTALL)
        f = re.search(r"FEEDBACK:\s*(.+)$", response, re.IGNORECASE | re.DOTALL)
        verdict = v.group(1).strip() if v else ""
        feedback = f.group(1).strip() if f else response.strip()
        if re.search(r"офф?топ|off.?topic", verdict, re.IGNORECASE):
            return True, ""
        # VERDICT не распознан (модель отступила от формата) — отдаём текст без
        # пустой приставки, иначе фидбек начинался бы с висячей «. ».
        if not verdict:
            return False, feedback
        return False, f"{verdict}. {feedback}".strip()

    def _render_quiz_announcement(self, subject: str, question: str) -> str:
        """Оборачивает объявление о тесте в характер персоны.
        Сам вопрос сохраняется ДОСЛОВНО (он же хранится отдельно в quiz_pending для проверки
        ответа) — LLM только меняет подачу/вступление, не содержание вопроса."""
        persona = self._persona_block()
        if not persona or not self._router:
            return f"Тест по теме «{subject}».\n\n{question}"
        messages = [
            {"role": "system", "content": (
                f"{persona}\n\n"
                "---\n"
                "You are a teacher, announcing a review question on the covered topic to a student. "
                "Announce it briefly (1-2 phrases) in your own style, and quote the question itself VERBATIM, "
                "without changes and without cuts. Do not give the answer and do not hint. "
                "Write in the language of the user's messages. "
                "No emoji."
            )},
            {"role": "user", "content": f"Topic: \"{subject}\". Question: {question}"},
        ]
        try:
            response = self._get_response_complete(messages, temperature=0.5, max_tokens=350, top_p=0.9)
            if response and response.strip():
                return response.strip()
        except Exception as e:
            logger.warning(f"[Learning] Стилизация объявления теста не удалась: {e}")
        return f"Тест по теме «{subject}».\n\n{question}"

    def _render_lesson_caption(self, subject: str, topic: str, next_num: int, questions: List[str]) -> str:
        """Оборачивает подпись к уроку (тема + контрольные вопросы) в характер персоны.
        Контрольные вопросы сохраняются ДОСЛОВНО — стилизация касается только подачи/вступления."""
        persona = self._persona_block()
        if not persona or not self._router:
            lines = [f"Урок №{next_num}: {topic or subject}"]
            if questions:
                lines.append("")
                lines.append("Контрольные вопросы:")
                for i, q in enumerate(questions[:3], 1):
                    lines.append(f"{i}. {q}")
            return "\n".join(lines)
        questions_block = "\n".join(f"{i}. {q}" for i, q in enumerate(questions[:3], 1)) if questions else "(no questions)"
        messages = [
            {"role": "system", "content": (
                f"{persona}\n\n"
                "---\n"
                "You are a teacher, announcing the topic of a new lesson and its review questions. "
                "Tell briefly (1-2 phrases) in your own style which topic the lesson covers, "
                "and be sure to quote the review questions VERBATIM, without changes or cuts. "
                "Write in the language of the user's messages. "
                "No emoji. Format: a short intro, then the questions block."
            )},
            {"role": "user", "content": (
                f"Course subject: \"{subject}\". "
                f"Topic of lesson #{next_num}: \"{topic or subject}\". "
                f"Review questions:\n{questions_block}"
            )},
        ]
        try:
            response = self._get_response_complete(messages, temperature=0.5, max_tokens=350, top_p=0.9)
            if response and response.strip():
                return response.strip()
        except Exception as e:
            logger.warning(f"[Learning] Стилизация подписи урока не удалась: {e}")
        lines = [f"Урок №{next_num}: {topic or subject}"]
        if questions:
            lines.append("")
            lines.append("Контрольные вопросы:")
            for i, q in enumerate(questions[:3], 1):
                lines.append(f"{i}. {q}")
        return "\n".join(lines)

    # ── отправка ──

    def _save_to_stm(self, chat_id: str, text: str):
        """Сохраняет отправленное сообщение в STM, чтобы бот имел контекст при последующих вопросах."""
        if not self._memory:
            return
        try:
            self._memory.add_message("assistant", text, user_id=chat_id, chat_id=chat_id)
        except Exception as e:
            logger.warning(f"[Learning] Не удалось сохранить урок в STM: {e}")

    def _is_muted(self) -> bool:
        """Персона заморожена (features.muted в её YAML, применяется на живую)."""
        if self._persona is None:
            return False
        return bool((self._persona.persona_data.get("features") or {}).get("muted"))

    async def _send(self, chat_id: str, text: str, topic_id: Optional[int], is_question: bool = False) -> bool:
        if not self._sender:
            return False
        # Замороженная персона молчит: False — урок не засчитывается,
        # попытка переносится на следующий интервал (курс паузится без порчи)
        if self._is_muted():
            logger.info(f"[Learning] Персона заморожена — отправка в {chat_id} пропущена")
            return False
        try:
            success = await self._sender.send_message(chat_id, text, topic_id=topic_id)
            logger.info(f"[Learning] Отправлено в чат {chat_id}: {text[:60]}")
            if success:
                self._save_to_stm(chat_id, text)
                # Вопросы (тест, «продолжаем?») регистрируем, чтобы потом понять,
                # ответил ли пользователь reply-ом именно на них. message_id берём
                # по ЭТОМУ чату — общий атрибут sender'а при конкурентных отправках
                # отдавал чужой id, и reply-gate привязывался не к тому сообщению.
                if is_question:
                    get_last_id = getattr(self._sender, "get_last_sent_message_id", None)
                    msg_id = get_last_id(chat_id) if get_last_id else getattr(self._sender, "last_sent_message_id", None)
                    if msg_id:
                        self.register_question_message(chat_id, msg_id)
            return bool(success)
        except Exception as e:
            logger.error(f"[Learning] Ошибка отправки в {chat_id}: {e}")
            return False

    async def _send_document(self, chat_id: str, file_path: str, filename: str,
                             caption: str, topic_id: Optional[int]) -> bool:
        """Отправляет md-файл урока. Подпись сохраняется в STM.
        Возвращает успех True/False — по False вызывающий код делает fallback на
        отправку текстом. Раньше успех наружу не возвращался (исключения гасились
        здесь же), и fallback в _send_regular_lesson был мёртвым кодом: файл «не
        ушёл» (например, caption длиннее лимита Telegram) — и урок молча терялся."""
        if not self._sender:
            return False
        # Замороженная персона молчит (см. _send)
        if self._is_muted():
            logger.info(f"[Learning] Персона заморожена — файл урока в {chat_id} пропущен")
            return False
        try:
            success = await self._sender.send_document(
                chat_id, file_path, filename, caption=caption, topic_id=topic_id
            )
            logger.info(f"[Learning] Файл урока отправлен в чат {chat_id}: {filename}")
            if success:
                # ВАЖНО: не пишем в STM служебную пометку вида "[Урок во вложении: ...]" —
                # LLM читает историю как свои собственные прошлые реплики и при случае
                # (например, отвечая на «продолжаем?») дословно повторяет такой текст
                # пользователю, включая квадратные скобки — выглядит как «файл не прикрепился».
                # Сохраняем только подпись — этого достаточно, чтобы модель знала тему/вопросы
                # последнего урока для последующего контекста.
                self._save_to_stm(chat_id, caption)
            return bool(success)
        except Exception as e:
            logger.error(f"[Learning] Ошибка отправки файла в {chat_id}: {e}")
            return False

    async def _send_lesson(self, session: dict):
        """Генерирует и отправляет очередной урок/тест. Обновляет состояние сессии."""
        chat_id = session["chat_id"]
        topic_id = session.get("topic_id")
        subject = session.get("subject", "")
        quiz_every = int(session.get("quiz_every", self.default_quiz_every))
        next_num = int(session.get("lesson_count", 0)) + 1
        is_quiz = quiz_every > 0 and next_num % quiz_every == 0

        if is_quiz:
            if session.get("quiz_pending"):
                # Прошлый тест ещё не отвечен. Новый вопрос здесь перезаписал бы
                # quiz_pending — и ответ пользователя на СТАРЫЙ вопрос проверялся
                # бы уже по новому (submit_quiz_answer читает только текущий
                # quiz_pending). Поэтому контент не шлём и тест не трогаем: просто
                # переносим следующий тик на интервал. Счётчик молчания уже вырос
                # в _loop() — по достижении порога спросим «продолжаем?», а при
                # дальнейшем молчании курс остановится штатно.
                logger.info(f"[Learning] Тест ещё не отвечен, пропускаю новый (chat={chat_id})")
                self._set_session(
                    chat_id,
                    session_id=session.get("session_id"),
                    next_lesson_at=time.time() + session["interval_seconds"],
                )
                return
            quiz = None
            try:
                quiz = await asyncio.to_thread(self._generate_quiz, session)
            except Exception as e:
                logger.warning(f"[Learning] Генерация теста не удалась: {e}")
            # Повтор с упрощённым промптом, если первый не удался
            if not quiz:
                try:
                    quiz = await asyncio.to_thread(self._generate_quiz, session, True)
                except Exception as e:
                    logger.warning(f"[Learning] Повторная генерация теста не удалась: {e}")
            if quiz:
                text = await asyncio.to_thread(self._render_quiz_announcement, subject, quiz["question"])
                sent = await self._send(chat_id, text, topic_id, is_question=True)
                if sent:
                    # Тест выставляем ТОЛЬКО после доставки анонса — иначе при сбое
                    # отправки пользователь, не видевший вопроса, отвечал бы вслепую
                    # на невидимый тест (следующее сообщение проверялось бы по нему).
                    self._set_session(
                        chat_id,
                        session_id=session.get("session_id"),
                        quiz_pending=quiz,
                        quiz_set_at=time.time(),
                        quiz_offtopic_count=0,
                        lesson_count=next_num,
                        next_lesson_at=time.time() + session["interval_seconds"],
                    )
                else:
                    # Анонс не ушёл — тик не засчитываем (lesson_count не растёт):
                    # на следующем интервале тест сгенерируется и уйдёт заново.
                    logger.warning(f"[Learning] Анонс теста не доставлен (chat={chat_id}), тест не выставлен, повтор на следующем тике")
                    self._set_session(
                        chat_id,
                        session_id=session.get("session_id"),
                        next_lesson_at=time.time() + session["interval_seconds"],
                    )
            else:
                # Тест не сгенерировался — отправляем обычный урок, интервал не теряется
                logger.info(f"[Learning] Тест не сгенерирован, отправляю урок-файл (chat={chat_id})")
                await self._send_regular_lesson(session)
            return

        await self._send_regular_lesson(session)

    async def _send_regular_lesson(self, session: dict):
        """Генерирует урок, отправляет как md-файл + сообщение с темой и контрольными вопросами.
        Сохраняет тему в covered_topics (через LLM-извлечение), обновляет состояние."""
        chat_id = session["chat_id"]
        topic_id = session.get("topic_id")
        subject = session.get("subject", "")
        next_num = int(session.get("lesson_count", 0)) + 1

        try:
            lesson = await asyncio.to_thread(self._generate_lesson_text, session)
        except Exception as e:
            logger.warning(f"[Learning] Генерация урока не удалась: {e}")
            lesson = None

        if not lesson or not lesson.get("lesson"):
            # Урок не сгенерировался — тик засчитываем как раньше (lesson_count растёт)
            # и честно сообщаем в чат; учебного состояния (темы/словарь) не меняем.
            self._set_session(
                chat_id,
                session_id=session.get("session_id"),
                lesson_count=next_num,
                next_lesson_at=time.time() + session["interval_seconds"],
            )
            await self._send(chat_id, f"Урок по теме «{subject}» не удалось подготовить. Продолжим в следующий раз.", topic_id)
            return

        # Извлекаем тему для covered_topics
        topic_title = lesson.get("topic", "") or ""
        # Если LLM не вернула TOPIC явно — извлекаем из текста
        if not topic_title:
            topic_title = await asyncio.to_thread(self._extract_topic, lesson.get("lesson", ""))

        # Для языковых курсов достаём ключевые слова/фразы урока — пригодятся, чтобы
        # потом естественно вплетать их в обычный разговор для закрепления
        # (см. get_reinforcement_hint).
        new_vocab_items: List[str] = []
        if session.get("course_kind") == "LANGUAGE":
            # Сначала пробуем vocab из структурированного ответа урока
            new_vocab_items = list(lesson.get("vocab") or [])
            if not new_vocab_items:
                try:
                    new_vocab_items = await asyncio.to_thread(self._extract_vocabulary, lesson.get("lesson", ""))
                except Exception as e:
                    logger.debug(f"[Learning] Извлечение словаря не удалось: {e}")

        # Сообщение в чат: тема + контрольные вопросы (стилизация через персону)
        questions = lesson.get("questions", [])
        chat_msg = await asyncio.to_thread(
            self._render_lesson_caption, subject, topic_title or subject, next_num, questions
        )

        # md-файл с полным текстом урока
        safe_topic = re.sub(r"[^\w\-]+", "_", topic_title or subject)[:40].strip("_") or f"lesson_{next_num}"
        filename = f"{safe_topic}.md"
        md_content = f"# Урок №{next_num}: {topic_title or subject}\n\n{lesson['lesson']}"
        if questions:
            md_content += "\n\n---\n\n**Контрольные вопросы:**\n"
            for i, q in enumerate(questions, 1):
                md_content += f"{i}. {q}\n"

        # Пишем во временный файл и отправляем
        import tempfile, os
        tmp_path = None
        sent = False
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
                f.write(md_content)
                tmp_path = f.name
            sent = await self._send_document(chat_id, tmp_path, filename, caption=chat_msg, topic_id=topic_id)
        except Exception as e:
            logger.error(f"[Learning] Ошибка отправки md-файла: {e}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        if not sent:
            # Файл не ушёл (сеть, лимит caption, ошибка Telegram) — отдаём урок текстом.
            # Длинный текст sender разобьёт на части сам (лимит сообщения Telegram 4096).
            logger.info(f"[Learning] Файл не доставлен, отправляю урок текстом (chat={chat_id})")
            sent_caption = await self._send(chat_id, chat_msg, topic_id)
            sent_body = await self._send(chat_id, lesson["lesson"], topic_id)
            sent = sent_caption and sent_body

        # Состояние обновляем ТОЛЬКО после успешной доставки. Раньше оно сохранялось
        # ДО отправки — и при сбое урок считался пройденным (lesson_count вырос, тема
        # в covered_topics), хотя пользователь его не видел.
        if sent:
            covered_addition = [topic_title] if topic_title else []
            new_covered = (session.get("covered_topics") or []) + covered_addition
            new_vocab = (session.get("learned_vocabulary") or []) + new_vocab_items
            self._set_session(
                chat_id,
                session_id=session.get("session_id"),
                lesson_count=next_num,
                next_lesson_at=time.time() + session["interval_seconds"],
                covered_topics=new_covered[-20:],
                learned_vocabulary=new_vocab[-40:],
            )
        else:
            # Урок не дошёл ни файлом, ни текстом — тик не засчитываем, переносим
            # попытку на следующий интервал.
            logger.warning(f"[Learning] Урок не доставлен ни файлом, ни текстом (chat={chat_id}) — тик не засчитан, повтор на следующем")
            self._set_session(
                chat_id,
                session_id=session.get("session_id"),
                next_lesson_at=time.time() + session["interval_seconds"],
            )

    async def _send_continue_question(self, session: dict):
        chat_id = session["chat_id"]
        topic_id = session.get("topic_id")
        text = (
            f"{session.get('user_name', '')}, по теме «{session.get('subject', '')}» "
            "я давно не получаю ответов. Продолжаем обучение? (да/нет)"
        )
        sent = await self._send(chat_id, text, topic_id, is_question=True)
        if sent:
            # Вопрос «продолжаем?» — теперь единственный живой вопрос курса: висящий
            # тест (quiz_pending) закрываем, иначе ответ «да/нет» уходил в проверку
            # теста вместо resolve_continue (resolve_pending_target брал quiz первым),
            # а курс потом останавливался по молчанию, хотя человек ответил «да».
            self._set_session(
                chat_id, session_id=session.get("session_id"),
                asked_continue=True, continue_asked_at=time.time(),
                quiz_pending=None, quiz_offtopic_count=0,
            )
        else:
            # Вопрос не доставлен — флаг не выставляем (иначе курс остановился бы на
            # следующем тике, хотя пользователь вопроса не видел). Просто переносим
            # попытку на интервал.
            logger.warning(f"[Learning] Вопрос «продолжаем?» не доставлен (chat={chat_id}), повтор на следующем тике")
            self._set_session(
                chat_id, session_id=session.get("session_id"),
                next_lesson_at=time.time() + session["interval_seconds"],
            )

    # ── фоновый цикл ──

    async def _loop(self):
        logger.info(f"[Learning] Цикл запущен для context={self.context}")
        while self._running:
            try:
                now = time.time()
                # Снимаем due-сессии под локом
                due: List[dict] = []
                with self._lock:
                    for s in self._sessions:
                        if not s.get("active"):
                            continue
                        if now < s.get("next_lesson_at", 0):
                            continue
                        # Время пришло. Если ждём ответа на «продолжать?» и пользователь молчит — стоп
                        if s.get("asked_continue"):
                            due.append({**s, "_action": "stop"})
                            s["active"] = False
                            s["quiz_pending"] = None
                            s["asked_continue"] = False
                            continue
                        # Иначе — пора отправлять урок (который может стать вопросом «продолжать?»)
                        due.append({**s, "_action": "lesson"})
                        # Сдвигаем далеко вперёд, чтобы не отправить повторно до обработки
                        s["next_lesson_at"] = now + s["interval_seconds"] + 86400
                    if due:
                        self._save()

                for s in due:
                    # Каждая сессия — в своём try: одно исключение НЕ должно ломать
                    # остальные курсы. Раньше ошибка в середине цикла пропускала все
                    # следующие сессии, а их next_lesson_at уже был отодвинут на
                    # interval+86400 — они молча ждали лишние сутки.
                    try:
                        if s.get("_action") == "stop":
                            logger.info(f"[Learning] Остановка по молчанию: chat={s['chat_id']} session={s.get('session_id')}")
                            continue
                        # Считаем молчание: для реальной сессии (не копии) инкрементируем.
                        # Целимся по session_id — при нескольких параллельных курсах одного чата
                        # chat_id больше не идентифицирует сессию однозначно.
                        self._increment_silence(s.get("session_id"))
                        real = self.get_session(s["chat_id"], session_id=s.get("session_id"))
                        if not real:
                            continue
                        # Гонка: пока шла due-сборка, сессию могли остановить из потока
                        # process_message (пользователь ответил «нет» на «продолжаем?»)
                        if not real.get("active"):
                            logger.info(f"[Learning] Сессия {s.get('session_id')} уже остановлена, урок не шлём")
                            continue
                        if real.get("consecutive_silences", 0) >= int(real.get("silence_threshold", self.default_silence_threshold)):
                            # Достигли порога — спрашиваем, урок пропускаем
                            await self._send_continue_question(real)
                        else:
                            await self._send_lesson(real)
                    except Exception as e:
                        logger.error(
                            f"[Learning] Ошибка обработки сессии {s.get('session_id')} "
                            f"(chat={s.get('chat_id')}): {e}", exc_info=True
                        )
                        # Переносим попытку на интервал, а не на сутки (сентинел
                        # +86400 выше нужен только от повторной отправки до конца
                        # обработки, он не должен становиться наказанием за ошибку).
                        try:
                            self._set_session(
                                s["chat_id"], session_id=s.get("session_id"),
                                next_lesson_at=time.time() + s.get("interval_seconds", 3600),
                            )
                        except Exception:
                            pass

            except Exception as e:
                logger.error(f"[Learning] Ошибка в цикле: {e}", exc_info=True)

            await asyncio.sleep(30)

    def _increment_silence(self, session_id: str):
        with self._lock:
            for s in self._sessions:
                if s.get("session_id") == session_id and s.get("active"):
                    s["consecutive_silences"] = s.get("consecutive_silences", 0) + 1
                    self._save()
                    return

    def format_delay(self, delay_seconds: float) -> str:
        """Человекочитаемое описание интервала."""
        if delay_seconds < 60:
            return f"{int(delay_seconds)} sec"
        if delay_seconds < 3600:
            return f"{int(delay_seconds / 60)} min"
        hours = delay_seconds / 3600
        if hours < 24:
            h = int(hours)
            m = int((delay_seconds - h * 3600) / 60)
            return f"{h} h {m} min" if m else f"{h} h"
        days = delay_seconds / 86400
        if days < 7:
            d = int(days)
            return f"{d} d"
        weeks = int(days / 7)
        return f"{weeks} wk"

    def start(self, loop=None):
        if not loop:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.error("[Learning] Нет running event loop")
                return
        self._running = True
        self._task = loop.create_task(self._loop())
        logger.info(f"[Learning] Запущено для {self.context}")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            logger.info("[Learning] Остановлено")

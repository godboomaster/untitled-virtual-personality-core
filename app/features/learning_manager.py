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

# «раз в <единицу>»: единица в винительном падеже ед.ч.
_SINGLE_UNIT_TO_SECONDS = {
    "секунду": 1, "минуту": 60, "час": 3600, "день": 86400, "сутки": 86400,
    "неделю": 604800, "месяц": 2592000,
}

# Указательные слова, после которых ожидается «N единиц»: «каждые/через N минут».
_LEAD_WORDS_RE = re.compile(r"\b(?:каждые|каждую|каждое|каждого|через|спустя|раз\s+в|интервал[а-я]*|с\s+интервалом)\b", re.IGNORECASE)

# Паттерн «N <единиц>» — число (цифра) + единица в любой форме.
_N_UNIT_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([а-яё]+)", re.IGNORECASE)
# Паттерн «числительное-прописью <единиц>».
_WORD_N_UNIT_RE = re.compile(r"([а-яё]+)\s+([а-яё]+)", re.IGNORECASE)


def parse_frequency(text: str, min_seconds: float = 300, max_seconds: float = 2592000) -> Optional[float]:
    """
    Парсит частоту уроков из ответа пользователя. Устойчив к морфологии русского и
    разным формулировкам: «каждые 10 минут», «каждый 10 минут», «10 минут», «раз в 10 минут»,
    «раз в день», «два раза в день», «каждый час», «через полчаса» и т.п.
    Возвращает интервал в секундах или None.
    """
    lower = text.lower()

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

    # ── 2b. «каждый <единица-ед.ч.>»: каждый час / каждый день / каждое утро.
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

    # ── 4. «числительное-прописью <единиц>»: «каждые десять минут», «через два часа»
    lead = _LEAD_WORDS_RE.search(lower)
    search_text = lower[lead.end():] if lead else lower
    for mm in _WORD_N_UNIT_RE.finditer(search_text):
        word = mm.group(1)
        unit = _UNIT_TO_SECONDS.get(mm.group(2))
        if word in _RU_WORD_NUMBERS and unit:
            return _clip(_RU_WORD_NUMBERS[word] * unit, min_seconds, max_seconds)

    # ── 6. Отдельные слова: «полчаса», «ежечасно», «ежедневно»
    if "полчаса" in lower or "пол-часа" in lower:
        return _clip(1800, min_seconds, max_seconds)
    _WORD_INTERVALS = {
        "ежечасно": 3600, "каждый час": 3600,
        "ежедневно": 86400, "ежесуточно": 86400, "каждый день": 86400,
        "еженедельно": 604800, "каждую неделю": 604800,
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

_POSITIVE_RE = re.compile(r"\b(?:да|давай|продолж|хочу|ок|ok|yes|конечно|поехали|угу)\b", re.IGNORECASE)
_NEGATIVE_RE = re.compile(r"\b(?:нет|не надо|хватит|стоп|останов|no|не хочу|отстань)\b", re.IGNORECASE)


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
        response = self._router.get_response(messages, temperature=temperature, max_tokens=max_tokens, top_p=top_p)
        if not response:
            return response
        full = response
        convo = list(messages)
        attempts = 0
        while self._looks_truncated(full) and attempts < max_continuations:
            convo = convo + [
                {"role": "assistant", "content": full},
                {"role": "user", "content": "Ты остановился на середине фразы. Допиши строго с того места, где прервался — не повторяй уже написанное и не начинай заново."},
            ]
            cont = self._router.get_response(convo, temperature=temperature, max_tokens=max_tokens, top_p=top_p)
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

    def get_setup_state(self, chat_id: str) -> Optional[dict]:
        with self._lock:
            return self._setup_state.get(str(chat_id))

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
                options_desc.append(f"{i}. Тема «{s.get('subject', '')}» — ждём ответ на тест: {q}")
            else:
                options_desc.append(f"{i}. Тема «{s.get('subject', '')}» — ждём ответ да/нет на вопрос «продолжаем обучение?»")

        messages = [
            {"role": "system", "content": (
                "Пользователь учится параллельно по нескольким темам. Определи, к какому из "
                "перечисленных вариантов ОТНОСИТСЯ по смыслу сообщение пользователя. "
                "Ответь СТРОГО одним числом — номером варианта (например: 2). "
                "Если сообщение явно не про один из вариантов — ответь 0. Больше ничего не пиши."
            )},
            {"role": "user", "content": "Варианты:\n" + "\n".join(options_desc) + f"\n\nСообщение пользователя: {user_text}"},
        ]
        response = self._router.get_response(messages, temperature=0.0, max_tokens=5, top_p=1.0)
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
            "У тебя может возникнуть желание напомнить о незакрытых контрольных вопросах "
            "прошлого урока или о том, что запрос «не по теме учёбы» — ты уже делал это "
            "недавно. НЕ повторяй это в этом ответе (никаких «который раз за сессию», "
            "«паттерн подтверждён» и т.п.) — просто ответь по существу сообщения."
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
                f"У пользователя идёт курс «{subject}». Если это ляжет ЕСТЕСТВЕННО в твой "
                f"ответ — используй в реплике 1 из этих ранее пройденных слов/фраз, к месту, "
                f"без перевода и без пометки, что это из урока: {'; '.join(pick)}. "
                "Если не ложится органично — просто пропусти, не притягивай за уши."
            )
        elif session.get("covered_topics"):
            pick = random.choice(session["covered_topics"])
            hint = (
                f"У пользователя идёт курс «{subject}», недавно проходили: «{pick}». "
                "Если это ляжет ЕСТЕСТВЕННО в твой ответ — можешь сделать короткую отсылку "
                "к этому понятию/метафору в своём стиле, БЕЗ объяснения, что она значит "
                "(пользователь либо поймёт, либо просто пройдёт мимо — оба варианта ок). "
                "Если не ложится органично — просто пропусти, не притягивай за уши."
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
                "Пользователь отвечает на вопрос «как часто присылать уроки». Определи "
                "интервал В СЕКУНДАХ из его ответа. Ответь СТРОГО одним целым числом секунд "
                "и ничего больше. Если по тексту нельзя понять периодичность — ответь ровно "
                "UNKNOWN."
            )},
            {"role": "user", "content": text},
        ]
        response = self._router.get_response(messages, temperature=0.0, max_tokens=10, top_p=1.0)
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

    def render_setup_reply(self, subject: str, kind: str, delay_text: str = "") -> str:
        """Короткая изолированная реплика персоны на этапе настройки частоты уроков
        (kind: 'confirmed' — частота распознана и обучение запущено, 'reask' — частоту
        не удалось понять). Как и render_continue_reply — БЕЗ доступа к STM/истории:
        через общий пайплайн модель на этом шаге придумывала небылицы про архитектуру бота
        и сразу выдавала полноценный текст урока вместо короткого подтверждения настройки."""
        fallback = {
            "confirmed": f"Хорошо, начинаем! Уроки по теме «{subject}» — {delay_text}.",
            "reask": "Не понял частоту. Как часто присылать уроки — например, «раз в день» или «каждые 2 часа»?",
        }.get(kind, "Хорошо.")
        if not self._router:
            return fallback

        task = {
            "confirmed": (
                f"Пользователь выбрал частоту уроков по теме «{subject}»: {delay_text}. "
                "Обучение только что запущено. Подтверди это ОДНОЙ короткой фразой в своём стиле."
            ),
            "reask": (
                f"Пользователь отвечал на вопрос о частоте уроков по теме «{subject}», но "
                "частоту не удалось распознать. Переспроси ОДНОЙ короткой фразой в своём "
                "стиле: как часто присылать уроки (например, «раз в день», «каждые 2 часа»)."
            ),
        }.get(kind, "Ответь коротко в своём стиле.")

        messages = [
            {"role": "system", "content": (
                f"{self._persona_block()}\n\n---\n{task}\n"
                "ЗАПРЕЩЕНО в этом ответе: текст урока, номер урока, контрольные вопросы, "
                "любое учебное содержание, а также рассуждения об архитектуре и "
                "возможностях/ограничениях бота. Только сама реакция на настройку расписания."
            )},
            {"role": "user", "content": f"Тема: «{subject}»." if subject else "Реагируй."},
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
                "Пользователю задали вопрос «продолжаем обучение? (да/нет)». Определи его "
                "сообщение: (а) неоднозначная попытка ответить да/нет (например, «наверное», "
                "«не знаю», «хз») — ответь UNKNOWN; (б) сообщение вообще не про этот вопрос, "
                "пользователь пишет/спрашивает о чём-то другом — ответь OFFTOPIC. "
                "Ответь СТРОГО одним словом: UNKNOWN или OFFTOPIC."
            ),
            user_content=text,
            max_tokens=6,
        )
        if result and "OFFTOPIC" in result.upper():
            return "OFFTOPIC"
        return "UNKNOWN"

    def render_continue_reply(self, chat_id: str, decision: str, session_id: Optional[str] = None) -> str:
        """Короткая реплика персоны на решение «продолжать обучение?» — YES/NO/UNKNOWN.
        Намеренно ИЗОЛИРОВАННЫЙ вызов (без STM/истории диалога, без прочего контекста):
        когда эту реплику генерировал общий пайплайн бота (persona.prepare_messages со всей
        историей), модель, видя в контексте прошлые уроки, игнорировала инструкцию
        «не пиши урок» и заново выдавала полноценный урок вместо короткого подтверждения.
        Здесь модель физически не видит ничего, кроме темы и решения — переигрывать урок ей
        неоткуда.

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
                "Пользователь подтвердил, что хочет продолжить обучение после паузы. "
                "Ответь ОДНОЙ короткой фразой в своём стиле, просто подтверди продолжение."
            ),
            "NO": (
                "Пользователь решил остановить обучение. Ответь ОДНОЙ короткой фразой "
                "в своём стиле, подтверди остановку."
            ),
        }.get(decision, (
            "Не удалось понять, хочет ли пользователь продолжать обучение или нет. "
            "Ответь ОДНОЙ короткой фразой в своём стиле и переспроси «да» или «нет»."
        ))

        messages = [
            {"role": "system", "content": (
                f"{self._persona_block()}\n\n---\n{task}\n"
                "ЗАПРЕЩЕНО в этом ответе: текст урока, номер урока, контрольные вопросы, "
                "тему следующего урока или любое учебное содержание. Только сама реакция."
            )},
            {"role": "user", "content": f"Тема обучения: «{subject}»." if subject else "Реагируй."},
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
        try:
            response = router.get_response(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.0,
                max_tokens=max_tokens,
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
                "Приведи тему обучения в именительный падеж, кратко (2-6 слов), без глаголов. "
                "Например: 'китайскому' → 'китайский язык', 'программированию на python' → 'программирование на Python'. "
                "Ответ — только тема, без кавычек и пояснений."
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
                "Определи ОДНУ главную тему учебного текста. "
                "Ответ — короткая фраза в именительном падеже (до 6 слов). "
                "Например: 'Пиньинь и тоны', 'Цикл for в Python'. "
                "Только тема, без кавычек и пояснений."
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
                "Это курс изучения ЖИВОГО/иностранного языка (например, испанский, "
                "китайский, латынь) или какая-то другая тема (включая языки "
                "программирования, технические предметы, науки, искусства и т.п.)? "
                "Ответь СТРОГО одним словом: LANGUAGE или TOPIC."
            ),
            user_content=f"Тема курса: «{subject}»",
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
        try:
            response = router.get_response(
                messages=[
                    {"role": "system", "content": (
                        "Из учебного текста выбери 2-4 самых важных новых слова/фразы урока. "
                        "Каждое — на отдельной строке, в формате 'слово/фраза — краткий "
                        "перевод или смысл'. Больше ничего не пиши."
                    )},
                    {"role": "user", "content": lesson_text[:1500]},
                ],
                temperature=0.3,
                max_tokens=150,
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
        covered_str = ", ".join(covered[-8:]) if covered else "пока ничего"

        messages = [
            {"role": "system", "content": (
                f"Ты опытный преподаватель. Обучаешь теме «{subject}». Урок №{lesson_num}.\n"
                f"Ранее уже пройдено: {covered_str}.\n"
                "Дай ОДИН небольшой урок по НОВОЙ подтеме (не повторяй пройденное). "
                "Объясняй понятно, с примерами. Это чистый учебный материал — БЕЗ действий, "
                "БЕЗ реплик персонажа, БЕЗ курсива и звёздочек, только содержание.\n\n"
                "Ответь СТРОГО в формате (каждое поле с новой строки):\n"
                "TOPIC: <короткая тема урока, до 6 слов, именительный падеж>\n"
                "QUESTIONS: <2-3 контрольных вопроса через точку с запятой>\n"
                "VOCAB: <2-4 коротких «единицы для закрепления» через точку с запятой — "
                "если это ЯЗЫК, давай реальные слова/фразы на изучаемом языке с кратким "
                "переводом через « — » (например: 你好 — привет); если это НЕ язык (техника, "
                "наука и т.п.), давай короткий яркий термин или метафору из урока, который "
                "можно естественно упомянуть в обычном разговоре без объяснений (например: "
                "третий ключ; сессионный ключ). Без пояснений, только сам список.>\n"
                "LESSON:\n<полный текст урока, markdown разрешён>"
            )},
            {"role": "user", "content": f"Дай урок №{lesson_num} по теме «{subject}»."},
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
        covered_str = ", ".join(covered[-8:]) if covered else "пока ничего"
        messages = [
            {"role": "system", "content": (
                f"Ты опытный преподаватель темы «{subject}». Урок №{lesson_num}. "
                f"Пройдено: {covered_str}. Дай ОДИН связный урок по новой подтеме (3-6 абзацев), "
                "с примерами. Чистый учебный текст, без действий персонажа."
            )},
            {"role": "user", "content": f"Дай урок №{lesson_num}."},
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
        covered_str = ", ".join(covered[-8:]) if covered else "базовые понятия"

        if simple:
            sys_prompt = (
                f"Тема: «{subject}». Придумай ОДИН простой вопрос с кратким ответом. "
                "Формат строго:\nВОПРОС: ...\nОТВЕТ: ...\nПОЯСНЕНИЕ: ..."
            )
            user_msg = "Дай вопрос."
        else:
            sys_prompt = (
                f"Ты преподаватель. Проверяешь знания по теме «{subject}».\n"
                f"Пройдено: {covered_str}.\n"
                "Придумай ОДИН проверочный вопрос по пройденному материалу. "
                "Открытый вопрос или задача с кратким ответом (не тест с вариантами). "
                "Ответь СТРОГО в формате (поля с новой строки):\n"
                "QUESTION: <текст вопроса>\n"
                "ANSWER: <короткий правильный ответ>\n"
                "EXPLANATION: <короткое пояснение>\n"
                "Никакого другого текста."
            )
            user_msg = "Дай тест по теме."

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ]
        response = self._router.get_response(messages, temperature=0.5, max_tokens=400, top_p=0.9)
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
                "Ты — преподаватель, проверяешь сообщение ученика по контрольному вопросу.\n"
                "Если сообщение — это ДЕЙСТВИТЕЛЬНО попытка ответить (пусть неверная или "
                "неполная), оцени её.\n"
                "Если сообщение НЕ является попыткой ответить — ученик сменил тему, задал "
                "совсем другой вопрос или пишет о постороннем — не придумывай натянутую "
                "оценку, используй VERDICT: ОФФТОП.\n"
                "ВАЖНО: вопрос с ученика — это НЕ попытка ответить, даже если он про тему "
                "теста: уточнение условия («а что ты имеешь в виду?», «а почему?», вопрос "
                "по материалу) — VERDICT: ОФФТОП. На вопрос ученика ответит обычный диалог, "
                "а не ты — не закрывай тест ложной оценкой.\n"
                "Ответь СТРОГО в формате:\n"
                "VERDICT: ВЕРНО | НЕВЕРНО | ЧАСТИЧНО | ОФФТОП\n"
                "FEEDBACK: <короткий комментарий; для ОФФТОП можно оставить пустым>\n"
                "Никакого другого текста."
            )},
            {"role": "user", "content": (
                f"Вопрос: {quiz.get('question', '')}\n"
                f"Правильный ответ: {quiz.get('answer', '')}\n"
                f"Пояснение: {quiz.get('explanation', '')}\n"
                f"Сообщение ученика: {user_answer}"
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
                "Ты — преподаватель, объявляешь ученику проверочный вопрос по пройденной теме. "
                "Сообщи об этом коротко (1-2 фразы) в своём стиле, и приведи сам вопрос ДОСЛОВНО, "
                "без изменений и без сокращений. Не давай ответ и не подсказывай. "
                "Без эмодзи."
            )},
            {"role": "user", "content": f"Тема: «{subject}». Вопрос: {question}"},
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
        questions_block = "\n".join(f"{i}. {q}" for i, q in enumerate(questions[:3], 1)) if questions else "(без вопросов)"
        messages = [
            {"role": "system", "content": (
                f"{persona}\n\n"
                "---\n"
                "Ты — преподаватель, объявляешь тему нового урока и контрольные вопросы к нему. "
                "Сообщи коротко (1-2 фразы) в своём стиле, какую тему затрагивает урок, "
                "и обязательно приведи контрольные вопросы ДОСЛОВНО, без изменений и сокращений. "
                "Без эмодзи. Формат: короткое вступление, затем блок вопросов."
            )},
            {"role": "user", "content": (
                f"Тема курса: «{subject}». "
                f"Тема урока №{next_num}: «{topic or subject}». "
                f"Контрольные вопросы:\n{questions_block}"
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

    async def _send(self, chat_id: str, text: str, topic_id: Optional[int], is_question: bool = False) -> bool:
        if not self._sender:
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
            return f"{int(delay_seconds)} сек"
        if delay_seconds < 3600:
            return f"{int(delay_seconds / 60)} мин"
        hours = delay_seconds / 3600
        if hours < 24:
            h = int(hours)
            m = int((delay_seconds - h * 3600) / 60)
            return f"{h} ч {m} мин" if m else f"{h} ч"
        days = delay_seconds / 86400
        if days < 7:
            d = int(days)
            return f"{d} дн"
        weeks = int(days / 7)
        return f"{weeks} нед"

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

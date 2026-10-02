"""
Досье на чат — профиль интересов пользователей для proactive-инициатив.

Анализирует историю сообщений, извлекает темы и интересы,
сохраняет профиль чата. Используется для:
- Персонализированных инициатив (факты по интересам)
- Понимания контекста без перечитывания всей истории
"""

import json
import logging
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from app.core import timeutil
from app.core.paths import data_dir
from app.core.atomic_io import atomic_write_json, load_json_safe
from app.core.bounded_cache import BoundedCache
from app.core.language import detect_language, user_language_line
from app.core.local_router import get_local_router

logger = logging.getLogger(__name__)

_DOSSIER_ANALYSIS_PROMPT = """Analyze the user's messages. Answer STRICTLY in JSON format:
{{
  "interests": ["concrete interest"],
  "topics": ["concrete conversation topic"],
  "personality_notes": ["observation about style/character"],
  "personal_facts": ["concrete fact about the person"]
}}

Rules:
- interests: hobbies, technologies, profession — only concrete words (python, games, music). NO generic phrases.
- topics: concrete conversation topics (3-6 words). NO one-word junk like "rubles", "now".
  Minimum 2 words per topic, except proper names. Do NOT include utility requests (translations, formatting).
- personality_notes: only real observations about the person. No more than 1-2 items.
- personal_facts: ONLY what the user explicitly said about themselves — name, city, profession, age.
  Do NOT include: the user's tasks, their requests to the bot, mentioned amounts of money, game/movie titles.
  If there are no explicit facts about the person — an empty list [].
- Keep the JSON keys exactly as above (in English).
- {language_line}

User messages:
{messages}

JSON:"""


@dataclass
class UserFacts:
    # Факты конкретного пользователя в чате.
    user_id: str
    facts: List[str] = field(default_factory=list)  # Факты которые пользователь сказал о себе
    last_updated: float = 0.0


@dataclass
class AttributedItem:
    # Интерес или топик с указанием автора.
    value: str        # Само значение ("python", "настольные игры")
    user_id: str      # Кто упомянул
    ts: float = 0.0   # Когда добавлено (unix timestamp)

    def to_dict(self) -> dict:
        return {"value": self.value, "user_id": self.user_id, "ts": self.ts}

    @staticmethod
    def from_dict(d: dict) -> "AttributedItem":
        return AttributedItem(
            value=d.get("value", ""),
            user_id=d.get("user_id", "unknown"),
            ts=d.get("ts", 0.0),
        )

    @staticmethod
    def from_legacy(value: str) -> "AttributedItem":
        # Миграция из старого формата (просто строка).
        return AttributedItem(value=value, user_id="unknown", ts=0.0)


@dataclass
class ChatProfile:
    # Профиль чата — интересы, предпочтения, факты.
    chat_id: str
    interests: List[AttributedItem] = field(default_factory=list)  # Топ интересов с авторами
    topics: List[AttributedItem] = field(default_factory=list)      # Темы с авторами
    facts_shared: List[str] = field(default_factory=list)  # Уже рассказанные факты (ботом)
    personality_notes: List[str] = field(default_factory=list)  # Наблюдения о пользователе
    user_facts: Dict[str, UserFacts] = field(default_factory=dict)  # user_id -> факты пользователя
    events: List[str] = field(default_factory=list)  # События от бота (rhythm: приветствия/предупреждения)
    last_updated: float = 0.0
    message_count: int = 0


class ChatDossier:
    """
    Ведет досье на чаты. Анализирует сообщения, извлекает интересы,
    предоставляет контекст для proactive-инициатив.
    """

    STOP_WORDS = {
        'этот', 'этого', 'этой', 'этом', 'твой', 'твоя', 'твое', 'твои',
        'мой', 'моя', 'мое', 'мои', 'свой', 'своя', 'свое', 'свои',
        'который', 'которая', 'которое', 'которые', 'такой', 'такая', 'такое',
        'пользователь', 'пользователя', 'пользователю', 'пользователи',
        'последний', 'последняя', 'последнее', 'последние',
        'время', 'разговор', 'сообщение', 'сообщения', 'инициатива',
        'тема', 'темы', 'вопрос', 'ответ', 'вопросы', 'ответы',
        'просто', 'очень', 'действительно', 'возможно', 'конечно',
        'можно', 'нужно', 'надо', 'стоит', 'хочется', 'хочу', 'думаю',
        'знаю', 'понимаю', 'говорю', 'сказал', 'сказала',
        'будет', 'было', 'были', 'была', 'был',
        'чтобы', 'когда', 'где', 'куда', 'откуда',
        'потому', 'поэтому', 'однако', 'хотя', 'если',
        'даже', 'только', 'уже', 'еще', 'ещё',
        'сейчас', 'тогда', 'сегодня', 'завтра', 'вчера',
        'здесь', 'там', 'тут', 'вот', 'вон',
        'какой', 'какая', 'какое', 'какие',
        'как', 'что', 'кто', 'чей', 'чья',
        'весь', 'вся', 'все', 'всё', 'всех',
        'каждый', 'каждая', 'каждое', 'каждые',
        'другой', 'другая', 'другое', 'другие',
        'самый', 'самая', 'самое', 'самые',
        'тот', 'та', 'то', 'те',
        'один', 'одна', 'одно', 'одни',
        'два', 'две', 'три', 'четыре', 'пять',
        'первый', 'второй', 'третий',
        'большой', 'большая', 'большое', 'большие',
        'маленький', 'маленькая', 'маленькое',
        'хороший', 'хорошая', 'хорошее', 'плохой',
        'новый', 'новая', 'новое', 'старый',
        'длинный', 'короткий', 'высокий', 'низкий',
        'правильный', 'неправильный', 'верный',
        'главный', 'основной', 'важный',
        'понятно', 'ясно', 'ладно', 'окей', 'ок',
        'спасибо', 'пожалуйста', 'извини', 'прости',
        'привет', 'пока', 'до свидания',
        'ага', 'ну', 'э', 'мм', 'аа',
        # Модальные глаголы / вспомогательные (часто попадают как "интересы")
        'можно', 'нельзя', 'надо', 'нужен', 'нужна', 'нужно', 'нужны',
        'должен', 'должна', 'должно', 'должны',
        'быть', 'есть', 'иметь', 'делать', 'сделать',
        'буду', 'будешь', 'будет', 'будем', 'будете', 'будут',
        'стать', 'становиться',
        # Местоимения
        'я', 'ты', 'он', 'она', 'оно', 'мы', 'вы', 'они',
        'меня', 'тебя', 'его', 'ее', 'её', 'нас', 'вас', 'их',
        'мне', 'тебе', 'ему', 'ей', 'нам', 'вам', 'им',
        'мной', 'тобой', 'им', 'ей', 'нами', 'вами', 'ими',
        # Предлоги / союзы (если попадают)
        'для', 'про', 'при', 'без', 'через', 'после', 'перед',
        'между', 'около', 'возле', 'вдоль', 'поперек',
        # Глаголы общего назначения
        'смотреть', 'видеть', 'слышать', 'читать', 'писать',
        'говорить', 'сказать', 'рассказать', 'спросить',
        'понять', 'знать', 'думать', 'верить', 'надеяться',
        'любить', 'нравиться', 'хотеть', 'желать',
        'работать', 'учить', 'учиться', 'изучать',
        'делать', 'создавать', 'строить', 'использовать',
        'помогать', 'пытаться', 'стараться', 'начинать',
        'заканчивать', 'продолжать', 'ждать', 'получать',
        'давать', 'брать', 'ходить', 'идти', 'ехать',
        'сидеть', 'стоять', 'лежать', 'жить',
    }

    # IT/технические ключевые слова для приоритизации
    TECH_KEYWORDS = {
        'python', 'javascript', 'java', 'cpp', 'c++', 'go', 'rust', 'kotlin',
        'typescript', 'react', 'vue', 'angular', 'django', 'flask', 'fastapi',
        'docker', 'kubernetes', 'aws', 'azure', 'gcp', 'cloud',
        'linux', 'ubuntu', 'debian', 'arch', 'fedora',
        'git', 'github', 'gitlab', 'ci/cd', 'devops',
        'machine', 'learning', 'ml', 'ai', 'neural', 'network',
        'database', 'sql', 'postgresql', 'mysql', 'mongodb', 'redis',
        'api', 'rest', 'graphql', 'websocket', 'grpc',
        'frontend', 'backend', 'fullstack', 'mobile', 'ios', 'android',
        'security', 'hacking', 'crypto', 'blockchain', 'bitcoin',
        'algorithm', 'data', 'structure', 'pattern',
        'framework', 'library', 'package', 'module',
        'server', 'client', 'browser', 'http', 'https',
        'programming', 'coding', 'development', 'software',
        'hardware', 'cpu', 'gpu', 'ram', 'ssd',
        'network', 'internet', 'protocol', 'tcp', 'udp',
    }

    # Мусорные фразы от LLM при извлечении фактов
    JUNK_PATTERNS = (
        "не указ", "неизвест", "нет факт", "нет данн", "нет информ",
        "невозможно", "требуется", "запрашивает", "пользователь пытается",
        "none", "нет", "не знаю",
        "unknown", "not specified", "not mentioned", "no facts", "n/a",
    )

    # Совпадение по границе слова — иначе «нет» режет «интернет», «кабинет», «монета»
    _JUNK_RE = re.compile(
        r"(?<![а-яёa-z])(?:" + "|".join(re.escape(p) for p in JUNK_PATTERNS) + ")",
        re.IGNORECASE,
    )

    def __init__(self, context: str = "default", router=None):
        self.context = context
        self._profiles: Dict[str, ChatProfile] = {}
        # Лок данных досье: держится только на чтение/запись _profiles и
        # кэшей, НИКОГДА поверх вызова LLM (см. analyze_chat)
        self._lock = threading.RLock()
        # Чаты, по которым анализ уже идёт (под self._lock): второй анализ
        # того же чата во время первого — пропуск, а не параллельная гонка
        self._analyzing: set = set()
        self._file = data_dir() / context / "chat_dossier.json"
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._router = router  # основной роутер бота (побочные — fallback минус primary)
        self._local_router = get_local_router(context)
        # Уже обработанные сообщения (chat_id → маркеры): analyze_chat идёт по
        # последним 50 сообщениям каждые 5 входящих — без дедупликации старые
        # сообщения экстрактились бы заново на каждом цикле (спам LLM-вызовами).
        # BoundedCache, а не dict: в отличие от self._profiles это НЕ
        # персистентные данные — не сохраняются в _save()/_load(), только для
        # дедупликации в рамках жизни процесса, поэтому вечный рост на каждый
        # новый chat_id нечем оправдать, а вытеснение безобидно (сообщение
        # просто переэкстрактится заново).
        self._facts_seen: BoundedCache = BoundedCache(max_entries=2000)
        # Водяной знак: бэклог STM (сообщения ДО старта процесса) в экстракцию
        # не берём вообще — факты нужны только из свежих сообщений
        self._facts_watermark: BoundedCache = BoundedCache(max_entries=2000)
        self._started_at = time.time()
        self._load()

    def _side_response(self, messages, **kw):
        """Побочный вызов LLM (анализ досье): fallback-цепочка основного
        роутера МИНУС основной провайдер (веб-чат — отдельный side-чат);
        без основного роутера — локальная модель."""
        if self._router is not None:
            return self._router.get_response(
                messages, exclude_provider=self._router.active_provider,
                webchat_channel="side", **kw)
        if self._local_router and self._local_router.is_available(task="dossier"):
            return self._local_router.get_response(messages, task="dossier", **kw)
        return None

    # Ключ водяного знака экстракции в экспорте профиля (export_profile).
    # С подчёркиванием — не поле ChatProfile: _profile_from_dict его
    # пропускает, файл досье его не хранит.
    _WATERMARK_KEY = "_facts_watermark"

    @staticmethod
    def _profile_to_dict(profile: ChatProfile) -> dict:
        """Сериализация одного профиля — формат файла chat_dossier.json.
        Списки копируются: результат можно отдавать наружу из-под лока, не
        разделяя изменяемые объекты с живым профилем."""
        return {
            "chat_id": profile.chat_id,
            "interests": [i.to_dict() for i in profile.interests],
            "topics": [t.to_dict() for t in profile.topics],
            "facts_shared": list(profile.facts_shared),
            "personality_notes": list(profile.personality_notes),
            "user_facts": {
                uid: {
                    "user_id": uf.user_id,
                    "facts": list(uf.facts),
                    "last_updated": uf.last_updated,
                }
                for uid, uf in profile.user_facts.items()
            },
            "events": list(profile.events),
            "last_updated": profile.last_updated,
            "message_count": profile.message_count,
        }

    @staticmethod
    def _profile_from_dict(data: dict, chat_id: str = "") -> ChatProfile:
        """Десериализация одного профиля (формат _profile_to_dict, плюс
        старый: интересы/темы строками). Вход не мутирует; неизвестные ключи
        (служебные ключи экспорта, поля будущих версий) пропускает — иначе
        ChatProfile(**...) падал на них и профиль терялся целиком. Бросает
        исключение на битых данных — вызывающий решает, что с ним делать."""
        def _items(raw) -> List[AttributedItem]:
            out = []
            for item in raw or []:
                if isinstance(item, dict):
                    out.append(AttributedItem.from_dict(item))
                elif isinstance(item, str):
                    out.append(AttributedItem.from_legacy(item))
            return out

        user_facts = {}
        for uid, uf_data in (data.get("user_facts") or {}).items():
            user_facts[uid] = UserFacts(
                user_id=uf_data.get("user_id", uid),
                facts=list(uf_data.get("facts", [])),
                last_updated=uf_data.get("last_updated", 0.0),
            )
        plain = {k: data[k] for k in ("facts_shared", "personality_notes",
                                      "events", "last_updated", "message_count")
                 if k in data}
        for k in ("facts_shared", "personality_notes", "events"):
            if k in plain:
                plain[k] = list(plain[k])
        return ChatProfile(
            chat_id=data.get("chat_id") or chat_id,
            interests=_items(data.get("interests")),
            topics=_items(data.get("topics")),
            user_facts=user_facts,
            **plain,
        )

    def export_profile(self, chat_id: str) -> Optional[dict]:
        """Снимок профиля чата для бэкапа (корзина /api/chat/clear,
        app/api/memory_wipe): формат файла досье + водяной знак экстракции
        фактов под _WATERMARK_KEY. None — профиля нет.

        Знак в бэкапе — нижняя граница для import_profile; сам по себе он
        повторную экстракцию не предотвращает: restore STM (server.py, через
        memory.add_message) пишет сообщения с НОВЫМИ метками time.time(),
        они новее любого знака из бэкапа. Поэтому import_profile ставит знак
        не ниже момента восстановления (см. там). _facts_seen в бэкап не
        берём: множество кортежей не JSON, а знак его заменяет."""
        with self._lock:
            profile = self._profiles.get(chat_id)
            if profile is None:
                return None
            data = self._profile_to_dict(profile)
            wm = self._facts_watermark.get(chat_id)
            if wm is not None:
                data[self._WATERMARK_KEY] = wm
            return data

    def import_profile(self, chat_id: str, data: dict):
        """Восстановление профиля из export_profile (или из записи файла
        досье — фолбэк-бэкап без живого менеджера). Заменяет текущий
        профиль НОВЫМ объектом: идущий в этот момент analyze_chat увидит
        подмену в фазе слияния и свой результат отбросит (см. фазу 3).

        Водяной знак — max(знак из бэкапа, сейчас): restore STM в server.py
        идёт РАНЬШЕ срезов памяти и через memory.add_message, т.е. с новыми
        метками time.time(). Знак из бэкапа (старше) пропустил бы всю
        восстановленную переписку в LLM-экстракцию заново, а её факты уже
        лежат в восстановленном профиле."""
        profile = self._profile_from_dict(data, chat_id=chat_id)
        with self._lock:
            self._profiles[chat_id] = profile
            wm = data.get(self._WATERMARK_KEY)
            wm = float(wm) if isinstance(wm, (int, float)) else 0.0
            self._facts_watermark[chat_id] = max(wm, time.time())
            self._save()

    def _load(self):
        """Загружает досье с диска. Полностью битый файл — warning + .corrupt-
        копия (общий helper), затем пустое досье; один битый ПРОФИЛЬ внутри
        иначе валидного файла — своя изоляция ниже (не роняет остальные)."""
        data = load_json_safe(self._file, default={}, label="Dossier")
        if isinstance(data, dict) and data:
            try:
                for chat_id, profile_data in data.items():
                    try:
                        self._profiles[chat_id] = self._profile_from_dict(
                            profile_data, chat_id=chat_id)
                    except Exception as e:
                        # Один битый профиль не должен обнулять досье всех чатов
                        logger.warning(f"[Dossier] Пропущен битый профиль чата {chat_id}: {e}")
                logger.info(f"[Dossier] Загружено {len(self._profiles)} профилей")
            except Exception as e:
                logger.warning(f"[Dossier] Не удалось загрузить: {e}")
                self._profiles = {}

    def _save(self):
        # Сохраняет досье на диск (атомарно, под блокировкой).
        with self._lock:
            try:
                data = {}
                for chat_id, profile in list(self._profiles.items()):
                    data[chat_id] = self._profile_to_dict(profile)
                atomic_write_json(self._file, data)
            except Exception as e:
                logger.warning(f"[Dossier] Не удалось сохранить: {e}")

    def _extract_words(self, text: str) -> List[str]:
        # Извлекает значимые слова из текста.
        if not text:
            return []
        words = re.findall(r'[а-яА-Яa-zA-Z]{4,}', text.lower())
        filtered = [w for w in words if w not in self.STOP_WORDS]
        return filtered

    def _extract_tech_keywords(self, text: str) -> List[str]:
        # Извлекает IT/технические ключевые слова.
        if not text:
            return []
        words = re.findall(r'[a-zA-Z+#/]{2,}', text.lower())
        return [w for w in words if w in self.TECH_KEYWORDS]

    _ANALYZE_COOLDOWN = 300  # минимум 5 минут между анализами одного чата

    def analyze_chat(self, chat_id: str, messages: List[dict]):
        """Потокобезопасная обёртка — вызывается из рабочих потоков конкурентно.

        Лок держится только вокруг снимка входа и слияния результата (см.
        _analyze_chat_impl); LLM-вызовы идут без лока — лок общий на персону,
        и на время side-чата (через веб-чат — минуты в общей очереди фоновых
        вызовов) под ним встали бы ответ пользователю (get_profile_snapshot
        при сборке контекста) и RhythmManager._note_dossier → record_event —
        корутина общего фонового loop, т.е. ритм, напоминания и инициативы
        ВСЕХ персон процесса.

        Флаг _analyzing сериализует параллельные анализы одного чата: раз
        лок их не сериализует, второй вызов дублировал бы LLM-вызовы и
        сливал бы те же интересы/факты поверх первого. Второй — пропуск, не
        ожидание: его сообщения — те же последние N из STM, первый их уже
        разбирает."""
        with self._lock:
            if chat_id in self._analyzing:
                logger.info(f"[Dossier] Анализ чата {chat_id} уже идёт — пропуск")
                return
            self._analyzing.add(chat_id)
        try:
            self._analyze_chat_impl(chat_id, messages)
        finally:
            with self._lock:
                self._analyzing.discard(chat_id)

    def _analyze_chat_impl(self, chat_id: str, messages: List[dict]):
        """
        Анализирует сообщения чата через LLM и обновляет профиль.
        Вызывается периодически или при накоплении N сообщений.

        Три фазы: (1) под локом — троттлинг, профиль, разбор сообщений и
        дедуп-кэши; (2) без лока — LLM-вызовы, только над локальными данными;
        (3) под локом — слияние результата в АКТУАЛЬНЫЙ профиль. Слияние идёт
        в живой объект, а не записью снимка «до»: за минуты LLM в тот же
        профиль могли дописать record_event/record_fact/add_personality_note —
        снимок их бы затёр.
        """
        if not messages:
            return

        # ── Фаза 1: снимок входа (под локом) ──
        with self._lock:
            # Throttle: не анализируем чаще раза в 5 минут
            _existing = self._profiles.get(chat_id)
            if _existing and (time.time() - _existing.last_updated) < self._ANALYZE_COOLDOWN:
                return

            profile = self._profiles.get(chat_id)
            if not profile:
                profile = ChatProfile(chat_id=chat_id)
                self._profiles[chat_id] = profile

            # Группируем сообщения по user_id чтобы знать кто что написал
            # { user_id -> [content, ...] }
            # Водяной знак экстракции фактов: старше него — не трогаем
            watermark = self._facts_watermark.get(chat_id, self._started_at)
            by_user: Dict[str, List[str]] = {}
            new_facts: Dict[str, List[str]] = {}  # новые сообщения для пакетной экстракции
            for msg in messages:
                if msg.get("role") == "user":

                    content = msg.get("content", "")
                    sender_id = (
                        msg.get("sender_id") or
                        msg.get("user_id") or
                        msg.get("from_id") or
                        msg.get("user_name") or
                        "unknown"
                    )
                    sender_id = str(sender_id).strip() if sender_id else "unknown"
                    if len(content) > 10:
                        by_user.setdefault(sender_id, []).append(content[:500])
                    msg_ts = float(msg.get("timestamp") or 0)
                    if (msg_ts > watermark and len(content) >= 15
                            and not content.startswith("/")):
                        # Дедуп: одно и то же сообщение не уходит в экстракцию дважды
                        seen = self._facts_seen.setdefault(chat_id, set())
                        marker = (sender_id, content[:200])
                        if marker not in seen:
                            seen.add(marker)
                            if len(seen) > 500:
                                self._facts_seen[chat_id] = set(list(seen)[-250:])
                            new_facts.setdefault(sender_id, []).append(content)
            self._facts_watermark[chat_id] = time.time()
            user_msg_count = len([m for m in messages if m.get("role") == "user"])

        # ── Фаза 2: LLM (БЕЗ лока) ──
        # Пакетная экстракция фактов: один LLM-вызов на все новые сообщения
        # отправителя за цикл, а не по вызову на сообщение
        extracted: Dict[str, List[str]] = {}
        # Язык чата — по последней реплике пользователя с буквами (STM чата);
        # у каждого отправителя свой язык, чат — фолбэк
        chat_lang = self._user_lang(
            [m.get("content", "") for m in messages if m.get("role") == "user"])
        for sender_id, contents in new_facts.items():
            facts = self._extract_user_facts(
                chat_id, sender_id, contents,
                lang=self._user_lang(contents, chat_lang))
            if facts:
                extracted[sender_id] = facts
        # Анализируем каждого пользователя отдельно (интересы/топики с его user_id)
        analyses: Dict[str, Optional[dict]] = {
            sender_id: self._analyze_with_llm(
                user_messages, lang=self._user_lang(user_messages, chat_lang))
            for sender_id, user_messages in by_user.items()
        }

        # ── Фаза 3: слияние в актуальный профиль (под локом) ──
        with self._lock:
            # Профиль мог быть вытерт (memory_wipe: pop из _profiles) или
            # заменён другим объектом (restore / новый record_event) за время
            # LLM-вызовов — тогда результат относится к досье, которого
            # больше нет. Искать/создавать профиль заново по chat_id
            # воскресило бы вытертые данные или подмешало бы их в новое
            # досье, поэтому сверяем по идентичности объекта из фазы 1.
            if self._profiles.get(chat_id) is not profile:
                logger.info(f"[Dossier] Досье чата {chat_id} очищено во время "
                            f"анализа — результат отброшен")
                return

            for sender_id, facts in extracted.items():
                self._merge_user_facts(profile, sender_id, facts)

            if not by_user:
                if extracted:
                    self._save()
                return

            any_llm_success = False
            for sender_id, llm_analysis in analyses.items():
                if not llm_analysis:
                    continue
                any_llm_success = True
                existing_interests = {i.value for i in profile.interests}
                for interest in llm_analysis.get("interests", []):
                    interest = interest.lower().strip()
                    if interest and interest not in existing_interests and len(interest) > 2:
                        profile.interests.append(AttributedItem(
                            value=interest, user_id=sender_id, ts=time.time()
                        ))
                        existing_interests.add(interest)
                # Вытесняем unknown, когда есть реальные user_id. Кап с
                # головы ([:20]) отбрасывал бы новые интересы при уже полном
                # списке — поэтому known ограничиваем с хвоста ([-20:]),
                # свежие вытесняют старые.
                known = [i for i in profile.interests
                         if i.user_id not in ("unknown", "")][-20:]
                unknown_items = [i for i in profile.interests
                                 if i.user_id in ("unknown", "")]
                free = 20 - len(known)
                profile.interests = known + (unknown_items[-free:]
                                             if free > 0 else [])

                existing_topics = {t.value for t in profile.topics}
                for topic in llm_analysis.get("topics", []):
                    topic = topic.lower().strip()
                    # Тема — минимум 2 слова или одно слово от 5 символов
                    words_in_topic = topic.split()
                    if len(words_in_topic) < 2 and len(topic) < 5:
                        continue
                    # Фильтруем стоп-слова как самостоятельные темы
                    if topic in self.STOP_WORDS:
                        continue
                    if topic and topic not in existing_topics:
                        profile.topics.append(AttributedItem(
                            value=topic, user_id=sender_id, ts=time.time()
                        ))
                        existing_topics.add(topic)
                # Кап с хвоста, а не с головы: [:30] отбрасывал бы новые темы
                # при уже полном списке.
                profile.topics = profile.topics[-30:]

                for note in llm_analysis.get("personality_notes", []):
                    note = note.strip()
                    if note and note not in profile.personality_notes:
                        profile.personality_notes.append(note)
                profile.personality_notes = profile.personality_notes[-10:]

                # personal_facts идут в user_facts[sender_id], а НЕ в facts_shared
                personal_facts = llm_analysis.get("personal_facts", []) or llm_analysis.get("facts_to_remember", [])
                good = []
                for fact in personal_facts:
                    fact = fact.strip()
                    if not fact or len(fact) < 3 or len(fact) > 150:
                        continue
                    if self._JUNK_RE.search(fact.lower()):
                        continue
                    good.append(fact)
                self._merge_user_facts(profile, sender_id, good)

            if not any_llm_success:
                # Fallback без LLM: подсчёт частоты слов, без атрибуции автору
                self._analyze_with_words(chat_id, messages, profile)

            # Инкремент, а не присваивание — счётчик мог измениться за время LLM
            profile.message_count += user_msg_count
            profile.last_updated = time.time()

            self._save()
            logger.info(f"[Dossier] Профиль {chat_id} обновлен: интересы={profile.interests[:5]}")

    @staticmethod
    def _merge_user_facts(profile: ChatProfile, user_id: str, facts: List[str]):
        """Сливает уже отфильтрованные факты в user_facts[user_id] профиля
        (вызывать под self._lock). Дубли сверяются с АКТУАЛЬНЫМ списком на
        момент слияния, а не на момент старта анализа."""
        if not facts:
            return
        uf = profile.user_facts.get(user_id)
        if uf is None:
            uf = profile.user_facts[user_id] = UserFacts(user_id=user_id)
        known = {f.lower() for f in uf.facts}
        added = False
        for fact in facts:
            if fact.lower() in known:
                continue
            uf.facts.append(fact)
            known.add(fact.lower())
            added = True
        if added:
            uf.facts = uf.facts[-20:]
            uf.last_updated = time.time()

    @staticmethod
    def _user_lang(contents: List[str], fallback: Optional[str] = None) -> Optional[str]:
        # Язык по последнему сообщению, где он определим; иначе — фолбэк
        for text in reversed(contents or []):
            lang = detect_language(text)
            if lang:
                return lang
        return fallback

    def _analyze_with_llm(self, user_messages: List[str],
                          lang: Optional[str] = None) -> Optional[dict]:
        # Анализирует сообщения через LLM (fallback-цепочка без основного).
        if self._router is None and not self._local_router.is_available():
            return None

        try:
            messages_text = "\n---\n".join(user_messages[-30:])

            prompt = _DOSSIER_ANALYSIS_PROMPT.format(
                messages=messages_text, language_line=user_language_line(lang))

            response = self._side_response(
                messages=[
                    {"role": "system", "content": "You are an analyst. Extract facts from messages. Answer ONLY with JSON.\n"
                                                  + user_language_line(lang)},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=500,
            )

            if not response:
                return None

            response = response.strip()

            # Ищем JSON блок
            json_start = response.find("{")
            json_end = response.rfind("}")
            if json_start == -1 or json_end == -1 or json_end <= json_start:
                logger.debug(f"[Dossier] Не найден JSON в ответе: {response[:100]}")
                return None

            json_str = response[json_start:json_end + 1]
            data = json.loads(json_str)

            # Валидируем структуру (поддерживаем старый facts_to_remember для совместимости)
            result = {}
            for key in ["interests", "topics", "personality_notes", "personal_facts", "facts_to_remember"]:
                value = data.get(key)
                if isinstance(value, list):
                    result[key] = [str(v).strip() for v in value if str(v).strip()]
                else:
                    result[key] = []

            logger.info(f"[Dossier] LLM анализ: интересы={result.get('interests', [])}")
            return result

        except Exception as e:
            logger.warning(f"[Dossier] Ошибка LLM-анализа: {e}")
            return None

    def _analyze_with_words(self, chat_id: str, messages: List[dict], profile: ChatProfile):
        # Fallback без LLM: интересы/темы по частоте слов, без атрибуции автору.
        all_words = []
        tech_words = []
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                all_words.extend(self._extract_words(content))
                tech_words.extend(self._extract_tech_keywords(content))

        if not all_words:
            return

        word_counts = Counter(all_words)
        tech_counts = Counter(tech_words)

        top_words = [w for w, c in word_counts.most_common(20)]
        tech_interests = [w for w, c in tech_counts.most_common(10)]

        now = time.time()
        existing_interest_values = {i.value for i in profile.interests}
        for w in tech_interests + [w for w in top_words if w not in tech_interests]:
            if w not in existing_interest_values:
                profile.interests.append(AttributedItem(value=w, user_id="unknown", ts=now))
                existing_interest_values.add(w)
        profile.interests = profile.interests[-15:]

        existing_topic_values = {t.value for t in profile.topics}
        for w in top_words:
            if w not in existing_topic_values:
                profile.topics.append(AttributedItem(value=w, user_id="unknown", ts=now))
                existing_topic_values.add(w)
        profile.topics = profile.topics[-30:]

    def get_profile(self, chat_id: str) -> Optional[ChatProfile]:
        """Возвращает профиль чата (сам объект, не копию — см. docstring
        get_profile_snapshot про то, почему для веб-UI/промпта нужен снимок,
        а не этот метод)."""
        with self._lock:
            return self._profiles.get(chat_id)

    def get_interests_text(self, chat_id: str) -> str:
        """Возвращает текст с интересами для промпта.

        Под локом: без него чтение profile.interests могло пересечься с
        _analyze_chat_impl, мутирующим тот же список из фонового потока
        (фаза слияния — под self._lock; LLM-вызовы анализа лок не держат,
        так что ожидание здесь короткое) — "dictionary/list changed size
        during iteration" при сборке системного промпта."""
        with self._lock:
            profile = self._profiles.get(chat_id)
            if not profile or not profile.interests:
                return ""
            interests = ", ".join(i.value for i in profile.interests[:8])
        return f"\n\nUser interests (mentioned in conversations): {interests}"

    def get_top_interest(self, chat_id: str) -> Optional[str]:
        """Возвращает главный интерес для поиска фактов (под локом — см.
        get_interests_text)."""
        with self._lock:
            profile = self._profiles.get(chat_id)
            if not profile or not profile.interests:
                return None
            return profile.interests[0].value

    def record_fact(self, chat_id: str, fact: str):
        # Запоминает факт, уже рассказанный ботом в чате (последние 20).
        with self._lock:
            profile = self._profiles.get(chat_id)
            if not profile:
                return
            profile.facts_shared.append(fact[:200])
            if len(profile.facts_shared) > 20:
                profile.facts_shared = profile.facts_shared[-20:]
            self._save()

    def record_event(self, chat_id: str, event: str):
        """Записывает событие от бота (напр., rhythm: отправлено утреннее
        приветствие / погодное предупреждение) — персона видит его в контексте
        досье и не повторяется. С меткой времени, последние 20."""
        with self._lock:
            profile = self._profiles.get(chat_id)
            if not profile:
                profile = ChatProfile(chat_id=chat_id)
                self._profiles[chat_id] = profile
            ts = timeutil.now().strftime("%d.%m.%Y %H:%M")
            profile.events.append(f"[{ts}] {event[:200]}")
            if len(profile.events) > 20:
                profile.events = profile.events[-20:]
            self._save()

    def was_fact_shared(self, chat_id: str, fact: str) -> bool:
        # Рассказывался ли уже похожий факт (общие слова с последними 5).
        with self._lock:
            profile = self._profiles.get(chat_id)
            if not profile or not profile.facts_shared:
                return False
            recent_facts = list(profile.facts_shared[-5:])
        # Простая проверка по ключевым словам — вне лока: только чтение
        # локальной копии, extract_words не трогает состояние досье
        fact_words = set(self._extract_words(fact))
        for old_fact in recent_facts:
            old_words = set(self._extract_words(old_fact))
            if fact_words & old_words:
                return True
        return False

    def _extract_user_facts(self, chat_id: str, user_id: str, contents: List[str],
                            lang: Optional[str] = None) -> List[str]:
        """Извлекает факты о пользователе ПАКЕТОМ: один LLM-вызов на все
        новые сообщения цикла анализа, а не по вызову на сообщение.

        Только LLM и разбор ответа — состояние досье НЕ трогает и лок не
        берёт (идёт в фазе 2 analyze_chat, без лока); возвращает
        отфильтрованные факты, в профиль их сливает _merge_user_facts."""
        if self._router is None and (not self._local_router
                                     or not self._local_router.is_available()):
            return []
        if not contents:
            return []

        block = "\n".join(f"Message: {c[:500]}" for c in contents[-10:])
        prompt = (
            "Extract concrete facts about the user: name, city, job, hobbies, age, goals.\n"
            "Only what is explicitly stated in the messages. No guesses.\n"
            "If there is nothing — answer with one word: NONE\n"
            "Format: one line = one fact. No explanations, no 'not specified', no 'unknown'.\n"
            "The NONE marker stays in English.\n\n"
            f"{block}\n\n"
            f"{user_language_line(lang)}"
        )

        try:
            response = self._side_response(
                messages=[
                    {"role": "system", "content": "You extract facts about the user. Only facts, nothing extra.\n"
                                                  + user_language_line(lang)},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=100,
            )

            if not response or response.strip().strip(".").upper() in ("NONE", "НЕТ"):
                return []

            facts: List[str] = []
            for line in response.strip().split("\n"):
                line = line.strip()
                if not line or line.strip(".").upper() in ("NONE", "НЕТ"):
                    continue
                # Форматы: "Факт: значение" или просто "значение"
                if ":" in line:
                    _, _, val = line.partition(":")
                    val = val.strip()
                else:
                    val = line

                if len(val) < 3 or len(val) > 100:
                    continue

                # Фильтруем мусор от LLM
                if self._JUNK_RE.search(val.lower()):
                    continue

                facts.append(val)
            return facts

        except Exception as e:
            logger.debug(f"[Dossier] Ошибка извлечения фактов пользователя {user_id}: {e}")
            return []

    def add_personality_note(self, chat_id: str, note: str):
        # Добавляет наблюдение о пользователе.
        with self._lock:
            profile = self._profiles.get(chat_id)
            if not profile:
                profile = ChatProfile(chat_id=chat_id)
                self._profiles[chat_id] = profile
            profile.personality_notes.append(note[:200])
            if len(profile.personality_notes) > 10:
                profile.personality_notes = profile.personality_notes[-10:]
            self._save()

    def get_profile_snapshot(self, chat_id: str, user_id: str = None) -> dict:
        """Снимок профиля чата для веб-UI и контекста ответа: интересы, темы,
        наблюдения. Только чтение; пустые списки если профиля ещё нет.
        user_id — оставить только записи этого участника (в группе чужие
        интересы/темы не подмешиваются в его персональный контекст)."""
        with self._lock:
            profile = self._profiles.get(chat_id)
            if not profile:
                return {"interests": [], "topics": [], "personality_notes": []}

            def _own(item) -> bool:
                return (user_id is None or item.user_id == user_id
                        or item.user_id in ("unknown", ""))

            # Мусорные значения («none» и т.п.) в чипсы UI и промпт не отдаём
            junk = {"none", "unknown", "n/a", "нет", "null", "-"}
            return {
                "interests": [i.value for i in profile.interests
                              if _own(i) and i.value.strip().lower() not in junk],
                "topics": [t.value for t in profile.topics
                           if _own(t) and t.value.strip().lower() not in junk],
                "personality_notes": list(profile.personality_notes),
            }

    def get_context_block(self, chat_id: str) -> str:
        """Возвращает полный блок контекста для промпта.

        Всё чтение — под локом (см. get_interests_text про гонку с фоновым
        analyze_chat): это самый частый читатель профиля (собирается на
        каждый ответ персоны), без лока здесь возможно
        "dictionary changed size during iteration"."""
        with self._lock:
            return self._context_block_locked(chat_id)

    def _context_block_locked(self, chat_id: str) -> str:
        profile = self._profiles.get(chat_id)
        if not profile:
            return ""
        parts = []
        if profile.interests:
            # Группируем интересы по user_id для читаемого вывода
            by_user: Dict[str, List[str]] = {}
            for item in profile.interests[:15]:
                by_user.setdefault(item.user_id, []).append(item.value)
            interest_lines = []
            for uid, vals in by_user.items():
                uid_short = uid[:8] if uid != "unknown" else "unknown"
                interest_lines.append(f"{uid_short}: {', '.join(vals[:5])}")
            parts.append("Interests:\n  " + "\n  ".join(interest_lines))

        if profile.topics:
            by_user_t: Dict[str, List[str]] = {}
            for item in profile.topics[-20:]:
                by_user_t.setdefault(item.user_id, []).append(item.value)
            topic_lines = []
            for uid, vals in by_user_t.items():
                uid_short = uid[:8] if uid != "unknown" else "unknown"
                topic_lines.append(f"{uid_short}: {', '.join(vals[:5])}")
            parts.append("Topics:\n  " + "\n  ".join(topic_lines))

        if profile.personality_notes:
            parts.append(f"Observations: {'; '.join(profile.personality_notes[-3:])}")
        if profile.facts_shared:
            parts.append(f"Facts already shared: {len(profile.facts_shared)}")

        # Факты по пользователям
        if profile.user_facts:
            user_parts = []
            for uid, uf in profile.user_facts.items():
                if uf.facts:
                    uid_short = uid[:8] if uid != "unknown" else "unknown"
                    user_parts.append(f"  {uid_short}: {', '.join(uf.facts[:5])}")
            if user_parts:
                parts.append("Facts about users:")
                parts.extend(user_parts)

        # События от бота (rhythm: приветствия, «пора спать», погода)
        if profile.events:
            parts.append(f"Recent bot-initiated events: {'; '.join(profile.events[-3:])}")

        if not parts:
            return ""

        return "\n\n[CHAT DOSSIER]\n" + "\n".join(parts)

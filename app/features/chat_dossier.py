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
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from app.core.local_router import get_local_router

logger = logging.getLogger(__name__)

# Промпт для LLM-анализа досье
_DOSSIER_ANALYSIS_PROMPT = """Проанализируй сообщения пользователя. Ответь СТРОГО в формате JSON:
{{
  "interests": ["конкретный интерес"],
  "topics": ["конкретная тема разговора"],
  "personality_notes": ["наблюдение о стиле/характере"],
  "personal_facts": ["конкретный факт о человеке"]
}}

Правила:
- interests: хобби, технологии, профессия — только конкретные слова (python, игры, музыка). НЕ общие фразы.
- topics: конкретные темы разговора (3-6 слов). НЕ однословные мусорные слова вроде "рублей", "теперь".
  Минимум 2 слова в теме, кроме имён собственных. НЕ включай служебные запросы (переводы, форматирование).
- personality_notes: только реальные наблюдения о человеке. Не более 1-2 штук.
- personal_facts: ТОЛЬКО то, что пользователь явно сказал о себе — имя, город, профессия, возраст.
  НЕ включай: задачи пользователя, его запросы к боту, упомянутые суммы денег, названия игр/фильмов.
  Если нет явных фактов о человеке — пустой список [].
- Пиши на русском языке.

Сообщения пользователя:
{messages}

JSON:"""


@dataclass
class UserFacts:
    """Факты конкретного пользователя в чате."""
    user_id: str
    facts: List[str] = field(default_factory=list)  # Факты которые пользователь сказал о себе
    last_updated: float = 0.0


@dataclass
class AttributedItem:
    """Интерес или топик с указанием автора."""
    value: str        # Само значение ("python", "resident evil")
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
        """Миграция из старого формата (просто строка)."""
        return AttributedItem(value=value, user_id="unknown", ts=0.0)


@dataclass
class ChatProfile:
    """Профиль чата — интересы, предпочтения, факты."""
    chat_id: str
    interests: List[AttributedItem] = field(default_factory=list)  # Топ интересов с авторами
    topics: List[AttributedItem] = field(default_factory=list)      # Темы с авторами
    facts_shared: List[str] = field(default_factory=list)  # Уже рассказанные факты (ботом)
    personality_notes: List[str] = field(default_factory=list)  # Наблюдения о пользователе
    user_facts: Dict[str, UserFacts] = field(default_factory=dict)  # user_id -> факты пользователя
    last_updated: float = 0.0
    message_count: int = 0


class ChatDossier:
    """
    Ведет досье на чаты. Анализирует сообщения, извлекает интересы,
    предоставляет контекст для proactive-инициатив.
    """

    # Стоп-слова для фильтрации
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
    )

    def __init__(self, context: str = "default"):
        self.context = context
        self._profiles: Dict[str, ChatProfile] = {}
        self._file = Path(f"data/{context}/chat_dossier.json")
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._local_router = get_local_router()
        self._load()

    def _load(self):
        """Загружает досье с диска."""
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for chat_id, profile_data in data.items():
                    # Десериализуем user_facts
                    user_facts_raw = profile_data.pop("user_facts", {})
                    user_facts = {}
                    for uid, uf_data in user_facts_raw.items():
                        user_facts[uid] = UserFacts(
                            user_id=uf_data.get("user_id", uid),
                            facts=uf_data.get("facts", []),
                            last_updated=uf_data.get("last_updated", 0.0),
                        )

                    # Десериализуем interests (новый формат dict или старый формат str)
                    raw_interests = profile_data.pop("interests", [])
                    interests = []
                    for item in raw_interests:
                        if isinstance(item, dict):
                            interests.append(AttributedItem.from_dict(item))
                        elif isinstance(item, str):
                            interests.append(AttributedItem.from_legacy(item))

                    # Десериализуем topics
                    raw_topics = profile_data.pop("topics", [])
                    topics = []
                    for item in raw_topics:
                        if isinstance(item, dict):
                            topics.append(AttributedItem.from_dict(item))
                        elif isinstance(item, str):
                            topics.append(AttributedItem.from_legacy(item))

                    profile = ChatProfile(**profile_data)
                    profile.user_facts = user_facts
                    profile.interests = interests
                    profile.topics = topics
                    self._profiles[chat_id] = profile
                logger.info(f"[Dossier] Загружено {len(self._profiles)} профилей")
            except Exception as e:
                logger.warning(f"[Dossier] Не удалось загрузить: {e}")
                self._profiles = {}

    def _save(self):
        """Сохраняет досье на диск."""
        try:
            data = {}
            for chat_id, profile in self._profiles.items():
                # Сериализуем user_facts
                user_facts_data = {}
                for uid, uf in profile.user_facts.items():
                    user_facts_data[uid] = {
                        "user_id": uf.user_id,
                        "facts": uf.facts,
                        "last_updated": uf.last_updated,
                    }
                data[chat_id] = {
                    "chat_id": profile.chat_id,
                    "interests": [i.to_dict() for i in profile.interests],
                    "topics": [t.to_dict() for t in profile.topics],
                    "facts_shared": profile.facts_shared,
                    "personality_notes": profile.personality_notes,
                    "user_facts": user_facts_data,
                    "last_updated": profile.last_updated,
                    "message_count": profile.message_count,
                }
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Dossier] Не удалось сохранить: {e}")

    def _extract_words(self, text: str) -> List[str]:
        """Извлекает значимые слова из текста."""
        if not text:
            return []
        # Слова 4+ символов
        words = re.findall(r'[а-яА-Яa-zA-Z]{4,}', text.lower())
        # Фильтруем стоп-слова
        filtered = [w for w in words if w not in self.STOP_WORDS]
        return filtered

    def _extract_tech_keywords(self, text: str) -> List[str]:
        """Извлекает IT/технические ключевые слова."""
        if not text:
            return []
        words = re.findall(r'[a-zA-Z+#/]{2,}', text.lower())
        return [w for w in words if w in self.TECH_KEYWORDS]

    _ANALYZE_COOLDOWN = 300  # минимум 5 минут между анализами одного чата

    def analyze_chat(self, chat_id: str, messages: List[dict]):
        """
        Анализирует сообщения чата через LLM и обновляет профиль.
        Вызывается периодически или при накоплении N сообщений.
        """
        if not messages:
            return

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
        by_user: Dict[str, List[str]] = {}
        for msg in messages:
            if msg.get("role") == "user":
                logger.info(f"[Dossier] msg keys: {list(msg.keys())}, role={msg.get('role')}")
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
                self._extract_user_facts(chat_id, sender_id, content)

        if not by_user:
            return

        # Анализируем каждого пользователя отдельно и добавляем интересы/топики с его user_id
        any_llm_success = False
        for sender_id, user_messages in by_user.items():
            llm_analysis = self._analyze_with_llm(user_messages)
            if llm_analysis:
                any_llm_success = True
                existing_interests = {i.value for i in profile.interests}
                for interest in llm_analysis.get("interests", []):
                    interest = interest.lower().strip()
                    if interest and interest not in existing_interests and len(interest) > 2:
                        profile.interests.append(AttributedItem(
                            value=interest, user_id=sender_id, ts=time.time()
                        ))
                        existing_interests.add(interest)
                # Вытесняем unknown когда есть реальные user_id
                known = [i for i in profile.interests if i.user_id not in ("unknown", "")]
                unknown_items = [i for i in profile.interests if i.user_id in ("unknown", "")]
                profile.interests = (known + unknown_items)[:20]

                existing_topics = {t.value for t in profile.topics}
                for topic in llm_analysis.get("topics", []):
                    topic = topic.lower().strip()
                    # Фильтруем: минимум 2 слова ИЛИ имя собственное длиннее 4 символов
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
                profile.topics = profile.topics[:30]

                for note in llm_analysis.get("personality_notes", []):
                    note = note.strip()
                    if note and note not in profile.personality_notes:
                        profile.personality_notes.append(note)
                profile.personality_notes = profile.personality_notes[-10:]

                # personal_facts идут в user_facts[sender_id], а НЕ в facts_shared
                personal_facts = llm_analysis.get("personal_facts", []) or llm_analysis.get("facts_to_remember", [])
                if personal_facts:
                    if sender_id not in profile.user_facts:
                        profile.user_facts[sender_id] = UserFacts(user_id=sender_id)
                    uf = profile.user_facts[sender_id]
                    for fact in personal_facts:
                        fact = fact.strip()
                        if not fact or len(fact) < 3 or len(fact) > 150:
                            continue
                        fact_lower = fact.lower()
                        if any(j in fact_lower for j in self.JUNK_PATTERNS):
                            continue
                        if not any(fact_lower == f.lower() for f in uf.facts):
                            uf.facts.append(fact)
                    uf.facts = uf.facts[-20:]
                    uf.last_updated = time.time()

        if not any_llm_success:
            # Fallback: старый метод подсчета слов (без атрибуции)
            self._analyze_with_words(chat_id, messages, profile)

        profile.message_count += len([m for m in messages if m.get("role") == "user"])
        profile.last_updated = time.time()

        self._save()
        logger.info(f"[Dossier] Профиль {chat_id} обновлен: интересы={profile.interests[:5]}")

    def _analyze_with_llm(self, user_messages: List[str]) -> Optional[dict]:
        """Анализирует сообщения через локальную LLM."""
        if not self._local_router.is_available():
            return None

        try:
            messages_text = "\n---\n".join(user_messages[-10:])  # последние 10 сообщений

            prompt = _DOSSIER_ANALYSIS_PROMPT.format(messages=messages_text)

            response = self._local_router.get_response(
                messages=[
                    {"role": "system", "content": "Ты аналитик. Извлекай факты из сообщений. Отвечай ТОЛЬКО JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=500,
            )

            if not response:
                return None

            # Пытаемся найти JSON в ответе
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
        """Fallback: анализ через подсчет слов (старый метод)."""
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
        profile.interests = profile.interests[:15]

        existing_topic_values = {t.value for t in profile.topics}
        for w in top_words:
            if w not in existing_topic_values:
                profile.topics.append(AttributedItem(value=w, user_id="unknown", ts=now))
                existing_topic_values.add(w)
        profile.topics = profile.topics[:30]

    def get_profile(self, chat_id: str) -> Optional[ChatProfile]:
        """Возвращает профиль чата."""
        return self._profiles.get(chat_id)

    def get_interests_text(self, chat_id: str) -> str:
        """Возвращает текст с интересами для промпта."""
        profile = self._profiles.get(chat_id)
        if not profile or not profile.interests:
            return ""
        interests = ", ".join(i.value for i in profile.interests[:8])
        return f"\n\nИнтересы пользователя (упоминались в разговорах): {interests}"

    def get_top_interest(self, chat_id: str) -> Optional[str]:
        """Возвращает главный интерес для поиска фактов."""
        profile = self._profiles.get(chat_id)
        if not profile or not profile.interests:
            return None
        return profile.interests[0].value

    def record_fact(self, chat_id: str, fact: str):
        """Записывает факт который уже был рассказан."""
        profile = self._profiles.get(chat_id)
        if not profile:
            return
        profile.facts_shared.append(fact[:200])  # обрезаем для хранения
        if len(profile.facts_shared) > 20:
            profile.facts_shared = profile.facts_shared[-20:]
        self._save()

    def was_fact_shared(self, chat_id: str, fact: str) -> bool:
        """Проверялся ли похожий факт ранее."""
        profile = self._profiles.get(chat_id)
        if not profile or not profile.facts_shared:
            return False
        # Простая проверка по ключевым словам
        fact_words = set(self._extract_words(fact))
        for old_fact in profile.facts_shared[-5:]:
            old_words = set(self._extract_words(old_fact))
            if fact_words & old_words:
                return True
        return False

    def _extract_user_facts(self, chat_id: str, user_id: str, content: str):
        """Извлекает факты о пользователе из сообщения через LLM."""
        if not self._local_router or not self._local_router.is_available():
            return

        # Пропускаем слишком короткие сообщения и команды
        if len(content) < 15 or content.startswith("/"):
            return

        prompt = (
            "Извлеки конкретные факты о пользователе: имя, город, работа, хобби, возраст, цели.\n"
            "Только то, что явно указано в сообщении. Никаких предположений.\n"
            "Если ничего нет — ответь одним словом: NONE\n"
            "Формат: одна строка = один факт. Без пояснений, без 'не указано', без 'неизвестно'.\n\n"
            f"Сообщение: {content[:500]}"
        )

        try:
            response = self._local_router.get_response(
                messages=[
                    {"role": "system", "content": "Ты извлекаешь факты о пользователе. Только факты, ничего лишнего."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=100,
            )

            if not response or response.strip().upper() == "NONE":
                return

            profile = self._profiles.get(chat_id)
            if not profile:
                profile = ChatProfile(chat_id=chat_id)
                self._profiles[chat_id] = profile

            # Получаем или создаем UserFacts для этого пользователя
            if user_id not in profile.user_facts:
                profile.user_facts[user_id] = UserFacts(user_id=user_id)

            user_facts = profile.user_facts[user_id]

            # Парсим факты из ответа
            for line in response.strip().split("\n"):
                line = line.strip()
                if not line or line.upper() == "NONE":
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
                val_lower = val.lower()
                if any(j in val_lower for j in self.JUNK_PATTERNS):
                    continue

                # Проверяем дубликаты
                if any(val_lower == f.lower() for f in user_facts.facts):
                    continue

                user_facts.facts.append(val)
                user_facts.last_updated = time.time()

            # Ограничиваем до 20 фактов на пользователя
            if len(user_facts.facts) > 20:
                user_facts.facts = user_facts.facts[-20:]

            self._save()

        except Exception as e:
            logger.debug(f"[Dossier] Ошибка извлечения фактов пользователя {user_id}: {e}")

    def add_personality_note(self, chat_id: str, note: str):
        """Добавляет наблюдение о пользователе."""
        profile = self._profiles.get(chat_id)
        if not profile:
            profile = ChatProfile(chat_id=chat_id)
            self._profiles[chat_id] = profile
        profile.personality_notes.append(note[:200])
        if len(profile.personality_notes) > 10:
            profile.personality_notes = profile.personality_notes[-10:]
        self._save()

    def get_context_block(self, chat_id: str) -> str:
        """Возвращает полный блок контекста для промпта."""
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
            parts.append("Интересы:\n  " + "\n  ".join(interest_lines))

        if profile.topics:
            by_user_t: Dict[str, List[str]] = {}
            for item in profile.topics[-20:]:
                by_user_t.setdefault(item.user_id, []).append(item.value)
            topic_lines = []
            for uid, vals in by_user_t.items():
                uid_short = uid[:8] if uid != "unknown" else "unknown"
                topic_lines.append(f"{uid_short}: {', '.join(vals[:5])}")
            parts.append("Темы:\n  " + "\n  ".join(topic_lines))

        if profile.personality_notes:
            parts.append(f"Наблюдения: {'; '.join(profile.personality_notes[-3:])}")
        if profile.facts_shared:
            parts.append(f"Уже рассказано фактов: {len(profile.facts_shared)}")

        # Факты по пользователям
        if profile.user_facts:
            user_parts = []
            for uid, uf in profile.user_facts.items():
                if uf.facts:
                    uid_short = uid[:8] if uid != "unknown" else "unknown"
                    user_parts.append(f"  {uid_short}: {', '.join(uf.facts[:5])}")
            if user_parts:
                parts.append("Факты о пользователях:")
                parts.extend(user_parts)

        if not parts:
            return ""

        return "\n\n[ДОСЬЕ ЧАТА]\n" + "\n".join(parts)

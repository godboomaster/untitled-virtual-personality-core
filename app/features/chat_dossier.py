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
_DOSSIER_ANALYSIS_PROMPT = """Проанализируй сообщения пользователя и извлеки структурированную информацию.

Ответь СТРОГО в формате JSON:
{{
  "interests": ["тема1", "тема2", "тема3"],
  "topics": ["обсуждавшаяся тема1", "тема2"],
  "personality_notes": ["наблюдение о пользователе"],
  "facts_to_remember": ["важный факт о пользователе"]
}}

Правила:
- interests: конкретные интересы пользователя (технологии, хобби, профессия). НЕ общие слова.
- topics: о чём шёл разговор
- personality_notes: наблюдения о характере, стиле общения, предпочтениях
- facts_to_remember: важные факты которые стоит запомнить (имя, город, работа, цели)
- НЕ включай слова: можно, надо, нужно, стоит, хочу, думаю, знаю, просто, очень
- Пиши на русском языке

Сообщения пользователя:
{messages}

JSON:"""


@dataclass
class ChatProfile:
    """Профиль чата — интересы, предпочтения, факты."""
    chat_id: str
    interests: List[str] = field(default_factory=list)  # Топ интересов
    topics: List[str] = field(default_factory=list)      # Обсуждавшиеся темы
    facts_shared: List[str] = field(default_factory=list)  # Уже рассказанные факты
    personality_notes: List[str] = field(default_factory=list)  # Наблюдения о пользователе
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
                    self._profiles[chat_id] = ChatProfile(**profile_data)
                logger.info(f"[Dossier] Загружено {len(self._profiles)} профилей")
            except Exception as e:
                logger.warning(f"[Dossier] Не удалось загрузить: {e}")
                self._profiles = {}

    def _save(self):
        """Сохраняет досье на диск."""
        try:
            data = {}
            for chat_id, profile in self._profiles.items():
                data[chat_id] = {
                    "chat_id": profile.chat_id,
                    "interests": profile.interests,
                    "topics": profile.topics,
                    "facts_shared": profile.facts_shared,
                    "personality_notes": profile.personality_notes,
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

    def analyze_chat(self, chat_id: str, messages: List[dict]):
        """
        Анализирует сообщения чата через LLM и обновляет профиль.
        Вызывается периодически или при накоплении N сообщений.
        """
        if not messages:
            return

        profile = self._profiles.get(chat_id)
        if not profile:
            profile = ChatProfile(chat_id=chat_id)
            self._profiles[chat_id] = profile

        # Собираем сообщения пользователя для анализа
        user_messages = []
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if len(content) > 10:
                    user_messages.append(content[:500])

        if not user_messages:
            return

        # Пробуем LLM-анализ
        llm_analysis = self._analyze_with_llm(user_messages)

        if llm_analysis:
            # Обновляем профиль из LLM-анализа
            if llm_analysis.get("interests"):
                # Добавляем новые интересы, избегая дубликатов
                existing = set(profile.interests)
                for interest in llm_analysis["interests"]:
                    interest = interest.lower().strip()
                    if interest and interest not in existing and len(interest) > 2:
                        profile.interests.append(interest)
                        existing.add(interest)
                # Ограничиваем до 10
                profile.interests = profile.interests[:10]

            if llm_analysis.get("topics"):
                existing = set(profile.topics)
                for topic in llm_analysis["topics"]:
                    topic = topic.lower().strip()
                    if topic and topic not in existing and len(topic) > 2:
                        profile.topics.append(topic)
                        existing.add(topic)
                profile.topics = profile.topics[:30]

            if llm_analysis.get("personality_notes"):
                for note in llm_analysis["personality_notes"]:
                    note = note.strip()
                    if note and note not in profile.personality_notes:
                        profile.personality_notes.append(note)
                profile.personality_notes = profile.personality_notes[-10:]

            if llm_analysis.get("facts_to_remember"):
                for fact in llm_analysis["facts_to_remember"]:
                    fact = fact.strip()
                    if fact and fact not in profile.facts_shared:
                        profile.facts_shared.append(fact)
                profile.facts_shared = profile.facts_shared[-20:]
        else:
            # Fallback: старый метод подсчета слов
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

            # Валидируем структуру
            result = {}
            for key in ["interests", "topics", "personality_notes", "facts_to_remember"]:
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

        profile.interests = tech_interests + [w for w in top_words if w not in tech_interests][:10]
        profile.topics = list(dict.fromkeys(profile.topics + top_words))[:30]

    def get_profile(self, chat_id: str) -> Optional[ChatProfile]:
        """Возвращает профиль чата."""
        return self._profiles.get(chat_id)

    def get_interests_text(self, chat_id: str) -> str:
        """Возвращает текст с интересами для промпта."""
        profile = self._profiles.get(chat_id)
        if not profile or not profile.interests:
            return ""
        interests = ", ".join(profile.interests[:8])
        return f"\n\nИнтересы пользователя (упоминались в разговорах): {interests}"

    def get_top_interest(self, chat_id: str) -> Optional[str]:
        """Возвращает главный интерес для поиска фактов."""
        profile = self._profiles.get(chat_id)
        if not profile or not profile.interests:
            return None
        return profile.interests[0]

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
            parts.append(f"Интересы: {', '.join(profile.interests[:8])}")
        if profile.personality_notes:
            parts.append(f"Наблюдения: {'; '.join(profile.personality_notes[-3:])}")
        if profile.facts_shared:
            parts.append(f"Уже рассказано фактов: {len(profile.facts_shared)}")

        if not parts:
            return ""

        return "\n\n[ДОСЬЕ ЧАТА]\n" + "\n".join(parts)

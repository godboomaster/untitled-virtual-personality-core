"""
Query Enhancer — преобразование вопросов пользователя в оптимальные поисковые запросы через локальную LLM.

Использование:
    enhancer = QueryEnhancer()
    enhanced = enhancer.enhance("Какая погода в Москве?")
    # -> "погода Москва сегодня"
"""

import logging
import re
from typing import Optional

from app.core.local_router import get_local_router

logger = logging.getLogger(__name__)


class QueryEnhancer:
    """
    Преобразует вопросы пользователя в короткие поисковые запросы через локальную LLM.
    Если LLM недоступна — использует fallback-правила.
    """

    _SYSTEM_PROMPT = (
        "You convert user questions into short search queries for DuckDuckGo.\n\n"
        "RULES (in order of importance):\n"
        "1. KEEP all words from the original question — do NOT replace them with synonyms, do NOT rephrase\n"
        "2. If a word is unclear — leave it as is, do not replace it with a similar one\n"
        "3. Remove question words (what, how, where, when, why, who, how many)\n"
        "4. Remove question marks and extra punctuation\n"
        "5. The query must be in the language of the question (Russian -> Russian, English -> English)\n"
        "6. Length: 1-7 words\n"
        "7. Do NOT add explanations, do NOT use quotes, do NOT include the persona's name in the query\n"
        "8. Answer with ONLY the search query, nothing else\n\n"
        "IMPORTANT: Take the persona context and dialogue history into account. "
        "If the question is about the persona (their appearance, character, preferences), "
        "convert it into a SEARCH QUERY about the same subject, but WITHOUT mentioning the persona's name. "
        "For example: 'what food do you like' -> 'tasty food', not 'Connor prefers'.\n"
        "If the question is NOT about the persona — just remove the question words and keep all other words unchanged."
    )

    _FEW_SHOT_EXAMPLES = []

    def __init__(self):
        self.router = get_local_router()

    def enhance(self, user_question: str, history: list[dict] | None = None, persona_context: str | None = None) -> str:
        """
        Преобразует вопрос пользователя в поисковый запрос.
        Учитывает историю диалога и контекст персоны.

        Args:
            user_question: Исходный вопрос пользователя
            history: Список последних сообщений (role/content) для контекста
            persona_context: Описание персоны (имя, роль, внешность) из YAML

        Returns:
            Оптимизированный поисковый запрос
        """
        if not user_question or not user_question.strip():
            logger.info("[QueryEnhancer] Пустой вопрос, возвращаем как есть")
            return user_question

        # Проверяем доступность локальной модели
        logger.info(f"[QueryEnhancer] Проверка доступности: router={self.router is not None}")
        if self.router:
            logger.info(f"[QueryEnhancer] router.is_available()={self.router.is_available(task='query_rewrite')}")
        if not self.router or not self.router.is_available(task="query_rewrite"):
            logger.warning("[QueryEnhancer] Локальная LLM недоступна, используем fallback")
            fallback = self._fallback_enhance(user_question)
            logger.info(f"[QueryEnhancer] Fallback: '{user_question}' -> '{fallback}'")
            return fallback

        # Собираем контекст для LLM
        context_parts = []

        if persona_context:
            context_parts.append(f"Persona context:\n{persona_context}")

        if history:
            history_lines = []
            for msg in history[-6:]:
                role = msg.get("role", "")
                content = msg.get("content", "")[:200]
                if role in ("user", "assistant"):
                    history_lines.append(f"{role}: {content}")
            if history_lines:
                context_parts.append("Dialogue history:\n" + "\n".join(history_lines))

        # Формируем промпт с few-shot примерами и контекстом
        messages = [
            {"role": "system", "content": self._SYSTEM_PROMPT},
        ]
        messages.extend(self._FEW_SHOT_EXAMPLES)

        user_content = ""
        if context_parts:
            user_content = "\n\n".join(context_parts) + "\n\n"
        user_content += f"Question: {user_question.strip()}\nQuery:"

        messages.append({"role": "user", "content": user_content})

        logger.info(f"[QueryEnhancer] Отправляем в LLM: '{user_question[:100]}...' (history={len(history) if history else 0}, persona={'yes' if persona_context else 'no'})")

        try:
            response = self.router.get_response(
                messages=messages,
                temperature=0.2,
                max_tokens=50,
                top_p=0.9,
                task="query_rewrite",
            )

            if not response:
                logger.warning("[QueryEnhancer] LLM вернул пустой ответ, используем fallback")
                fallback = self._fallback_enhance(user_question)
                logger.info(f"[QueryEnhancer] Fallback: '{user_question}' -> '{fallback}'")
                return fallback

            # Чистим ответ
            enhanced = self._clean_response(response)

            if not enhanced or len(enhanced) < 2:
                logger.warning(f"[QueryEnhancer] LLM вернул слишком короткий ответ: '{response}', используем fallback")
                fallback = self._fallback_enhance(user_question)
                logger.info(f"[QueryEnhancer] Fallback: '{user_question}' -> '{fallback}'")
                return fallback

            logger.info(f"[QueryEnhancer] LLM: '{user_question}' -> '{enhanced}'")
            return enhanced

        except Exception as e:
            logger.error(f"[QueryEnhancer] Ошибка LLM: {e}")
            fallback = self._fallback_enhance(user_question)
            logger.info(f"[QueryEnhancer] Fallback после ошибки: '{user_question}' -> '{fallback}'")
            return fallback

    def _clean_response(self, response: str) -> str:
        """Чистит ответ LLM от лишних символов и форматирования."""
        # Убираем кавычки
        response = response.strip().strip('"').strip("'")
        # Убираем markdown (одиночные подчёркивания НЕ трогаем —
        # они легитимны в запросах: user_id, all_MiniLM_L6_v2)
        response = re.sub(r'\*{1,3}|_{2,}', '', response)
        # Убираем префиксы
        response = re.sub(r'^(запрос|query|поиск|search)[:\s]*', '', response, flags=re.IGNORECASE)
        # Убираем лишние пробелы
        response = re.sub(r'\s+', ' ', response).strip()
        return response

    def _fallback_enhance(self, user_question: str) -> str:
        """
        Fallback-преобразование без LLM — правила.
        """
        query = user_question.strip()
        
        # Убираем вопросительные слова в начале
        query = re.sub(
            r'^(?:что\s+такое|что|как|где|когда|почему|зачем|кто|сколько|какой|какая|какое|какие|каков|какова|каковы|который|которая|которое|которые)\s+',
            '',
            query,
            flags=re.IGNORECASE
        )
        
        # Убираем знаки вопроса и лишнюю пунктуацию
        query = query.strip('?').strip('!').strip('.')
        
        # Убираем лишние пробелы
        query = re.sub(r'\s+', ' ', query).strip()
        
        # Если получилось слишком короткое — добавляем контекст
        if len(query.split()) < 2:
            query = f"{query} информация"
        
        logger.debug(f"[QueryEnhancer] Fallback преобразование: '{user_question}' -> '{query}'")
        return query

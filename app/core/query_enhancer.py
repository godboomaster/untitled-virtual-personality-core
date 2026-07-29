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
        "Ты преобразуешь вопросы пользователя в короткие поисковые запросы для DuckDuckGo.\n\n"
        "ПРАВИЛА (в порядке важности):\n"
        "1. СОХРАНЯЙ все слова из оригинального вопроса — НЕ заменяй их синонимами, НЕ перефразируй\n"
        "2. Если слово непонятно — оставь его как есть, не заменяй на близкое по смыслу\n"
        "3. Убери вопросительные слова (что, как, где, когда, почему, зачем, кто, сколько)\n"
        "4. Убери знаки вопроса и лишнюю пунктуацию\n"
        "5. Запрос должен быть на языке вопроса (русский -> русский, английский -> английский)\n"
        "6. Длина: 1-7 слов\n"
        "7. НЕ добавляй пояснений, НЕ используй кавычки, НЕ включай имя персоны в запрос\n"
        "8. Ответь ТОЛЬКО поисковым запросом, ничего больше\n\n"
        "ВАЖНО: Учитывай контекст персоны и историю диалога. "
        "Если вопрос относится к персоне (ее внешность, характер, предпочтения), "
        "преобразуй в ПОИСКОВЫЙ ЗАПРОС о том же предмете, но БЕЗ упоминания имени персоны. "
        "Например: 'какая еда тебе нравится' -> 'вкусная еда', а не 'Коннор предпочитает'.\n"
        "Если вопрос НЕ о персоне — просто убери вопросительные слова и оставь все остальные слова без изменений."
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
            logger.info(f"[QueryEnhancer] router.is_available()={self.router.is_available()}")
        if not self.router or not self.router.is_available():
            logger.warning("[QueryEnhancer] Локальная LLM недоступна, используем fallback")
            fallback = self._fallback_enhance(user_question)
            logger.info(f"[QueryEnhancer] Fallback: '{user_question}' -> '{fallback}'")
            return fallback

        # Собираем контекст для LLM
        context_parts = []

        if persona_context:
            context_parts.append(f"Контекст персоны:\n{persona_context}")

        if history:
            history_lines = []
            for msg in history[-6:]:
                role = msg.get("role", "")
                content = msg.get("content", "")[:200]
                if role in ("user", "assistant"):
                    history_lines.append(f"{role}: {content}")
            if history_lines:
                context_parts.append("История диалога:\n" + "\n".join(history_lines))

        # Формируем промпт с few-shot примерами и контекстом
        messages = [
            {"role": "system", "content": self._SYSTEM_PROMPT},
        ]
        messages.extend(self._FEW_SHOT_EXAMPLES)

        user_content = ""
        if context_parts:
            user_content = "\n\n".join(context_parts) + "\n\n"
        user_content += f"Вопрос: {user_question.strip()}\nЗапрос:"

        messages.append({"role": "user", "content": user_content})

        logger.info(f"[QueryEnhancer] Отправляем в LLM: '{user_question[:100]}...' (history={len(history) if history else 0}, persona={'yes' if persona_context else 'no'})")

        try:
            response = self.router.get_response(
                messages=messages,
                temperature=0.2,
                max_tokens=50,
                top_p=0.9,
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

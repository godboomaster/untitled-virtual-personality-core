"""
Определяет, нужен ли веб-поиск для ответа на вопрос пользователя.
Лёгкий вызов LLM с коротким промптом — минимум токенов, быстрый ответ.
"""

import logging
from app.core.router import ModelRouter

_router = ModelRouter()

logger = logging.getLogger(__name__)

DECISION_PROMPT = """You are a search necessity classifier. You MUST reply with exactly one word: SEARCH or SKIP.

CRITICAL RULES for SEARCH:
- Any question about current time, date, year, season → SEARCH (you do NOT know the current time)
- Any question with words like "сейчас", "сегодня", "недавно", "latest", "current", "now", "recently" → SEARCH
- News, weather, prices, exchange rates, sports results, charts, rankings → SEARCH
- Specific facts, numbers, dates NOT in the conversation context → SEARCH
- Questions about people, places, events not mentioned in context → SEARCH

Rules for SKIP:
- Answer is clearly present in the conversation context below
- Opinions, feelings, advice, creative writing, roleplay, philosophical topics
- Greetings, small talk, social interaction
- User refers to previously discussed topics or uploaded files

When in doubt, answer SEARCH.

Reply ONLY with SEARCH or SKIP. Nothing else."""

# Ключевые слова, гарантированно требующие поиска
_SEARCH_KEYWORDS = [
    # Время/дата
    "сегодня", "сейчас", "недавно", "вчера", "завтра",
    "какая дата", "какое число", "который час", "какое сегодня",
    "какая сегодня", "какой сегодня", "текущ", "актуальн",
    # Погода
    "погода", "температура", "дождь", "снег", "ветер", "прогноз",
    "weather",
    # Новости/события
    "новости", "новость", "что случилось", "что произошло",
    "news", "latest",
    # Цифры/факты
    "курс", "цена", "стоимость", "сколько стоит",
    "рейтинг", "топ ", "лучший ",
    "exchange rate", "price",
    # "Current" / "now"
    "current", "now", "recent", "today",
]


def _keyword_match(text: str) -> bool:
    # Быстрая проверка по ключевым словам — без вызова LLM
    lower = text.lower()
    return any(kw in lower for kw in _SEARCH_KEYWORDS)


def need_web_search(user_question: str, context_summary: str = "") -> bool:
    """
    Решает, нужен ли веб-поиск.

    Args:
        user_question: Текст вопроса пользователя.
        context_summary: Краткая сводка последних сообщений (STM + LTM + файлы).

    Returns:
        True если нужен веб-поиск, False если ответ уже есть в контексте.
    """
    # Формируем контекст для промпта
    context_block = ""
    if context_summary:
        # Обрезаем контекст — нам нужно только понять, есть ли уже ответ
        context_block = f"\nCONVERSATION CONTEXT (recent messages, facts, files):\n{context_summary[:1500]}\n"

    user_block = f"\nUSER QUESTION: {user_question}"

    # 1. Быстрая проверка по ключевым словам (без LLM)
    if _keyword_match(user_question):
        logger.info(f"[NEED_SEARCH] Q='{user_question[:50]}' -> SEARCH (keyword match)")
        return True

    messages = [
        {"role": "system", "content": DECISION_PROMPT},
        {"role": "user", "content": context_block + user_block}
    ]

    try:
        answer = _router.get_response(
            messages,
            temperature=0.0,
            max_tokens=5,
            top_p=1.0
        )
        # Ищем SEARCH или SKIP в ответе (модель может добавить лишний текст)
        raw = answer.strip().upper() if answer else ""
        if "SEARCH" in raw:
            first_word = "SEARCH"
        elif "SKIP" in raw:
            first_word = "SKIP"
        else:
            first_word = raw.split()[0] if raw else "SKIP"
        need = first_word == "SEARCH"

        logger.info(f"[NEED_SEARCH] Q='{user_question[:50]}' -> {first_word} | raw='{answer.strip()[:80]}' (provider: {_router.active_provider})")
        return need

    except Exception as e:
        logger.error(f"[NEED_SEARCH] Ошибка: {e}")
        # При ошибке — всё равно ищем, лучше лишний поиск чем тишина
        return True

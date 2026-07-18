"""
intent_router.py — классификатор намерений для Арродеса.

Определяет, нужен ли контекст книги (RAG) для ответа, или это обычный разговор.
Использует локальную Ollama (qwen2.5:3b). При недоступности — keyword fallback.

Гибридная логика:
  1. _find_glossary_entries (из book_search) находит совпадения со словарём.
  2. Найденные термины передаются в промпт LLM для контекста.
  3. LLM классифицирует намерение.
  4. Если LLM дал chat_only, но словарь нашёл совпадения — повышаем до mixed.
"""

import httpx
import logging
from pathlib import Path

from app.features.book_search import _find_glossary_entries, _load_ru_to_en

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "gemma3:4b"

# Имена, которые не считаются book-сигналом (имя самого бота = обращение)
_BOT_NAMES = {"арродес", "зеркало", "arrodes", "mirror"}

# Кеш словаря ru_to_en (загружается один раз)
_ru_to_en: dict[str, str] | None = None


def _get_ru_to_en() -> dict[str, str]:
    """Загружает ru_to_en из глоссария. Кешируется."""
    global _ru_to_en
    if _ru_to_en is not None:
        return _ru_to_en
    glossary_path = Path(__file__).parent.parent / "personas" / "arrodes_glossary.yaml"
    _ru_to_en = _load_ru_to_en(str(glossary_path))
    logger.info(f"[IntentRouter] Loaded {len(_ru_to_en)} glossary entries")
    return _ru_to_en


CLASSIFIER_PROMPT = """Classify the intent of the user's message.

book_only  — user asks about Lord of the Mysteries lore, plot events, character details, worldbuilding, or specific scenes from the book. The answer requires book knowledge.

chat_only  — anything NOT about the Lord of the Mysteries novel: real-world questions (recipes, cooking, weather, homework, advice, how to do something), casual conversation, greetings, praise, emotions, small talk, meta-dialogue. A real-world "tell me how to X" or "give me a recipe" is chat_only, NOT book_only. Mentions of the bot's own name (Arrodes, Арродес) or being addressed directly is NOT a book query.

mixed      — the message combines a real book question with casual chat in the same message.

Examples:
"Who is Klein Moretti?" → book_only
"What is the Fool pathway?" → book_only
"Tell me about the Forsaken Land of the Gods" → book_only
"Give me an iced tea recipe" → chat_only
"How do I cook pasta" → chat_only
"What's the weather like" → chat_only
"Recommend me a movie" → chat_only
"Good job Arrodes" → chat_only
"Thank you mirror" → chat_only
"Arrodes, you are wise" → chat_only
"Hey, who is Arrodes really?" → book_only
"Arrodes, tell me about sequence 9" → mixed

{book_terms}

Recent conversation (last 3 messages):
{history}

New message: "{message}"

Reply with ONLY one word: book_only, chat_only, or mixed"""

VALID_INTENTS = {"book_only", "chat_only", "mixed"}


def classify_intent(message: str, stm: list[dict]) -> str:
    """
    Классифицирует намерение с учётом истории STM.
    Возвращает: 'book_only' | 'chat_only' | 'mixed'
    """
    # Детерминированный поиск совпадений со словарём
    ru_to_en = _get_ru_to_en()
    matched = _find_glossary_entries(message, ru_to_en)
    # Убираем имя бота из совпадений
    book_terms = {
        ru: en for ru, en in matched.items()
        if ru.lower() not in _BOT_NAMES
    }

    recent = stm[-3:] if len(stm) >= 3 else stm
    history = "\n".join(
        f"{m['role']}: {m['content'][:120]}"
        for m in recent
    ) if recent else "(начало диалога)"

    # Передаём найденные термины в промпт
    terms_line = ""
    if book_terms:
        terms = ", ".join(book_terms.values())
        terms_line = f"Matched book terms found in message: {terms}"

    intent = None
    try:
        resp = httpx.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "prompt": CLASSIFIER_PROMPT.format(
                    history=history,
                    message=message,
                    book_terms=terms_line
                ),
                "stream": False,
                "options": {
                    "temperature": 0.0,
                    "num_predict": 5,
                    "stop": ["\n", " ", "."]
                }
            },
            timeout=8.0
        )
        result = resp.json()["response"].strip().lower()
        if result in VALID_INTENTS:
            intent = result
        else:
            logger.warning(f"Unexpected intent response: '{result}', falling back")
            intent = _keyword_fallback(message)
    except httpx.TimeoutException:
        logger.warning("Ollama timeout, using keyword fallback")
        intent = _keyword_fallback(message)
    except Exception as e:
        logger.error(f"Intent classification failed: {e}")
        intent = "chat_only"

    # Жёсткое правило: словарный сигнал не даёт опуститься ниже mixed
    if intent == "chat_only" and book_terms:
        logger.info(f"[IntentRouter] Glossary override: chat_only -> mixed (terms: {list(book_terms.values())})")
        return "mixed"

    logger.info(f"[IntentRouter] intent={intent}, book_terms={list(book_terms.values())}")
    return intent


# --- Keyword fallback (если Ollama недоступна) ---

BOOK_SIGNALS = [
    "последовательность", "путь стража", "карта таро", "шут",
    "клейн", "морети", "андерсон", "аудрей", "алджер", "герман",
    "бейондер", "ритуал", "молитва", "фогги", "тайна"
]

CHAT_SIGNALS = [
    "как дела", "что думаешь", "твоё мнение", "расскажи о себе",
    "посоветуй", "что лучше", "помоги мне"
]


def _keyword_fallback(message: str) -> str:
    msg = message.lower()
    book_score = sum(1 for kw in BOOK_SIGNALS if kw in msg)
    chat_score = sum(1 for kw in CHAT_SIGNALS if kw in msg)
    if book_score > 0 and chat_score > 0:
        return "mixed"
    elif book_score > 0:
        return "book_only"
    else:
        return "chat_only"

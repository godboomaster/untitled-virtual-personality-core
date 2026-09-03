"""
Query rewriting — разрешение кореференций в запросе пользователя.

Использует LLM для анализа контекста диалога и замены местоимений/анафор
на конкретные сущности (имена персонажей, названия игр/фильмов и т.д.).

Rule-based подход заменён на LLM-based, т.к. regex не справляется с:
- именами без явных маркеров ("персонаж X")
- разграничением сущностей из разных контекстов ("Симон из Expedition 33" vs "другой Симон")
- падежами и склонениями
"""

import logging
import re

logger = logging.getLogger(__name__)

# Сколько последних реплик из истории передавать в rewriter
_CONTEXT_WINDOW = 8

_SYSTEM_PROMPT = """\
You are a coreference resolution module for a search engine.

TASK:
The user wrote a message that may contain pronouns or anaphora
("he", "she", "this game", "his abilities", etc.) referring to entities
from previous dialogue turns.

Your job is to rewrite the user's message so that it becomes a self-sufficient
search query: replace all pronouns and indefinite references with concrete
names/titles from the context.

RULES:
1. Replace ONLY what explicitly refers to a concrete entity from the history.
2. Do not change words that are already concrete (proper names, titles, numbers).
3. Do not add words that appear neither in the question nor in the history.
4. If it is unclear what a pronoun refers to — leave the question as is.
5. Keep the grammar and syntax of the phrase as much as possible.
6. CRITICAL: distinguish entities from different contexts — "Simon from Expedition 33"
   and another "Simon" from a different game are different entities. Use the one
   discussed immediately before the user's question.
7. PERSONA: if a "Persona context" is provided, the persona's name is the
   bot assistant's name. Address forms like "you", "your", "to you" are addressed
   to the bot, NOT to game characters. Do NOT replace such pronouns — they are
   not anaphora, they address the bot.
   Example: "what do you think about his abilities" — keep "you", replace "his".
8. CRITICAL: keep the language of the user's message — rewrite a Russian message
   in Russian, an English message in English.
9. Answer ONLY with the rewritten query, no explanations.

EXAMPLES:
History: [user: tell me about Simon from Expedition 33, assistant: Simon is the main hero...]
Question: "tell me about his combat features in the game"
Answer: "Simon combat features in Expedition 33"

History: [user: what is Elden Ring, assistant: ..., user: and who is Malenia, assistant: Malenia is a boss...]
Question: "how to beat her"
Answer: "how to beat Malenia in Elden Ring"

History: [user: посоветуй фильм, assistant: советую Inception]
Question: «о чём он»
Answer: «о чём фильм Inception»

History: [user: привет]
Question: «как дела»
Answer: «как дела»\
"""


def rewrite_query(
    user_input: str,
    history: list[dict],
    local_router=None,
    persona_context: str | None = None,
) -> str:
    """
    Переписывает запрос пользователя, разрешая местоимения и анафору через LLM.

    Args:
        user_input: текущее сообщение пользователя
        history: список сообщений STM (dicts с полями role/content)
        local_router: экземпляр LocalRouter (опционально, берётся из get_local_router)
        persona_context: описание персоны бота из YAML (имя, роль и т.п.).
            Используется чтобы LLM не путала «ты/твои» (обращение к боту)
            с местоимениями, ссылающимися на игровых персонажей/объекты.

    Returns:
        Переписанный запрос (или оригинал, если переписывать нечего / LLM недоступна)
    """
    # Быстрая эвристика: если сообщение без анафорических маркеров — пропускаем LLM
    if _is_self_contained(user_input):
        logger.info(f"[QueryRewriter] Самодостаточный запрос, пропускаем: '{user_input[:60]}'")
        return user_input

    # Берём последние N реплик из истории
    recent = [
        m for m in history
        if m.get("role") in ("user", "assistant")
    ][-_CONTEXT_WINDOW:]

    if not recent:
        logger.info(f"[QueryRewriter] Нет истории для контекста, возвращаем оригинал: '{user_input[:60]}'")
        return user_input

    # Получаем router
    router = local_router
    if router is None:
        try:
            from app.core.local_router import get_local_router
            router = get_local_router()
        except Exception:
            pass

    if not router or not router.is_available(task="query_rewrite"):
        logger.warning("[QueryRewriter] LLM недоступна, возвращаем оригинал")
        return user_input

    # Форматируем историю для промпта
    history_text = _format_history(recent)

    # Собираем блок контекста — персона идёт первой, чтобы LLM понимала
    # кто такой «ассистент» в истории и не путала его имя с игровыми персонажами
    context_parts = []
    if persona_context:
        context_parts.append(f"Persona context (the bot in the dialogue):\n{persona_context.strip()}")
    context_parts.append(f"Dialogue history:\n{history_text}")

    user_content = (
        "\n\n".join(context_parts) + "\n\n"
        f"User question: \"{user_input.strip()}\"\n\n"
        f"Rewritten query:"
    )

    try:
        response = router.get_response(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            task="query_rewrite",
            temperature=0.1,   # минимум случайности — нужна точность
            max_tokens=80,
            top_p=0.9,
        )

        if not response:
            logger.warning("[QueryRewriter] LLM вернул пустой ответ")
            return user_input

        rewritten = _clean_response(response)

        # Санити-чек: если LLM вернула что-то странное — используем оригинал
        if not rewritten or len(rewritten) < 3:
            logger.warning(f"[QueryRewriter] Слишком короткий ответ LLM: '{response}'")
            return user_input

        if rewritten.lower() == user_input.lower().strip():
            logger.info(f"[QueryRewriter] Запрос не изменился: '{user_input[:60]}'")
            return user_input

        logger.info(f"[QueryRewriter] '{user_input[:60]}' -> '{rewritten[:60]}'")
        return rewritten

    except Exception as e:
        logger.error(f"[QueryRewriter] Ошибка LLM: {e}")
        return user_input


def _format_history(messages: list[dict]) -> str:
    """Форматирует историю диалога в читаемый текст для промпта."""
    lines = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "").strip()
        if not content:
            continue
        # Обрезаем длинные ответы ассистента — нам важны сущности, не полный текст
        if role == "assistant" and len(content) > 300:
            content = content[:300] + "..."
        role_label = "user" if role == "user" else "assistant"
        lines.append(f"[{role_label}]: {content}")
    return "\n".join(lines)


def _clean_response(response: str) -> str:
    """Чистит ответ LLM: убирает кавычки, лишние префиксы, markdown."""
    text = response.strip()
    # Убираем обрамляющие кавычки (одинарные, двойные, «ёлочки»)
    text = text.strip('"').strip("'").strip("«").strip("»")
    # Убираем возможные префиксы типа "Ответ: ..."
    text = re.sub(r'^(ответ|запрос|answer|query)[:\s]+', '', text, flags=re.IGNORECASE)
    # Убираем markdown (одиночные подчёркивания НЕ трогаем —
    # они легитимны в именах файлов и идентификаторах)
    text = re.sub(r'\*{1,3}|_{2,}', '', text)
    # Сжимаем пробелы
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _is_self_contained(text: str) -> bool:
    """
    Эвристика: сообщение самодостаточно если не содержит анафорических маркеров.
    Возвращает True — LLM вызывать не нужно.
    """
    anaphora = [
        # Личные местоимения (только косвенные падежи)
        " неё ", " него ", " нём ", " ней ",
        " её ", " его ", " им ", " ими ",
        "у неё", "у него", "о ней", "о нём",
        "в ней", "в нём", "с ней", "с ним",
        # Указательные местоимения (косвенные падежи — специфичнее)
        " этой ", " этого ", " этом ", " этому ", " этим ",
        " той ", " того ", " том ", " тому ", " тем ",
        # НЕ включаем " такой " — срабатывает на "кто такой X" (не анафора)
        # НЕ включаем " она/они/оно " — слишком широкие без контекста
        # Составные фразы — более специфичны, false positive маловероятен
        "эта игра", "этот фильм", "эта книга", "этот сериал",
        "это аниме", "эта песня", "этот альбом", "этот персонаж",
        "в этой игре",
        # "в игре" — анафора когда нет явного названия рядом.
        # Добавляем с пробелами чтобы не срабатывать внутри длинных фраз,
        # но ловить "пиктос в игре", "босс в игре" и т.п.
        " в игре",
        # Предложные конструкции с местоимениями
        " про него ", " про неё ", " про них ",
        " о нём ", " о ней ", " о них ",
        " к нему ", " к ней ",
        " для него ", " для неё ", " для них ",
    ]
    lower = f" {text.lower()} "
    return not any(marker in lower for marker in anaphora)

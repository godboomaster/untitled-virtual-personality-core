"""
Query rewriting — разрешение кореференций и перевод запроса для веб-поиска.

Пример:
    Пользователь: "Расскажи про Nier Automata"
    Пользователь: "А какие у неё концовки?"
    → ru_rewritten: "А какие у Nier Automata концовки?"
    → en_rewritten: "What are the endings of Nier Automata?"
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Сколько последних реплик из истории передавать в rewriter
_CONTEXT_WINDOW = 6


def rewrite_query(
    user_input: str,
    history: list[dict],
    local_router=None,
) -> tuple[str, Optional[str]]:
    """
    Переписывает запрос пользователя:
    - Разрешает местоимения и анафору, опираясь на историю диалога
    - Переводит на английский если тема международная/техническая

    Args:
        user_input: текущее сообщение пользователя
        history: список сообщений STM (dicts с полями role/content)
        local_router: локальный LLM роутер (Gemma); если None — возвращает оригинал

    Returns:
        (ru_rewritten, en_rewritten)
        ru_rewritten — переписанный запрос на русском (или оригинал если переписывать нечего)
        en_rewritten — английский перевод для поиска (или None)
    """
    if not local_router:
        return user_input, None

    try:
        if not local_router.is_available():
            return user_input, None
    except Exception:
        return user_input, None

    # Быстрая эвристика: если сообщение длинное и без местоимений — не тратим вызов
    if _is_self_contained(user_input):
        # Переводим, но не переписываем
        en = _translate_only(user_input, local_router)
        return user_input, en

    # Берём последние N реплик из истории (только user + assistant, без system)
    recent = [
        m for m in history
        if m.get("role") in ("user", "assistant")
    ][-_CONTEXT_WINDOW:]

    if not recent:
        # Нет истории — нечего разрешать
        en = _translate_only(user_input, local_router)
        return user_input, en

    # Форматируем историю для промпта
    history_lines = []
    for m in recent:
        role = "Пользователь" if m["role"] == "user" else "Ассистент"
        content = m.get("content", "")[:300]
        history_lines.append(f"{role}: {content}")
    history_text = "\n".join(history_lines)

    prompt = (
        "Тебе дана история диалога и новое сообщение пользователя.\n"
        "Твоя задача:\n"
        "1. Перепиши сообщение на русском, заменив все местоимения и указания "
        "(она, он, это, там, её, его, там, оттуда, эта игра, этот фильм и т.п.) "
        "на конкретные названия из истории.\n"
        "   Если заменять нечего — оставь как есть.\n"
        "2. Если тема международная/техническая — переведи на английский для поиска.\n"
        "   Если тема сугубо русская (российские события, русские люди) — en = null.\n\n"
        "Ответь СТРОГО в формате JSON (без пояснений):\n"
        '{"ru": "переписанное сообщение", "en": "english version or null"}\n\n'
        f"История:\n{history_text}\n\n"
        f"Новое сообщение: {user_input}"
    )

    try:
        response = local_router.get_response(
            messages=[
                {
                    "role": "system",
                    "content": "Ты помогаешь переписывать сообщения. Отвечай ТОЛЬКО JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=150,
        )

        if not response:
            return user_input, None

        # Вырезаем JSON из ответа
        response = response.strip()
        j_start = response.find("{")
        j_end = response.rfind("}") + 1
        if j_start == -1 or j_end == 0:
            return user_input, None

        data = json.loads(response[j_start:j_end])
        ru = (data.get("ru") or "").strip() or user_input
        en = (data.get("en") or "").strip() or None
        if en and en.lower() in ("null", "none", "-", ""):
            en = None

        # Санити-чек: не принимаем явную чушь (слишком длинный rewrite)
        if len(ru) > len(user_input) * 5:
            logger.warning(f"[QueryRewriter] Слишком длинный rewrite, используем оригинал")
            return user_input, en

        if ru != user_input:
            logger.info(f"[QueryRewriter] '{user_input[:60]}' → '{ru[:60]}'")

        return ru, en

    except Exception as e:
        logger.debug(f"[QueryRewriter] Ошибка: {e}")
        return user_input, None


def _is_self_contained(text: str) -> bool:
    """
    Эвристика: сообщение самодостаточно если не содержит анафорических маркеров.
    Список намеренно консервативный — лучше лишний раз вызвать LLM, чем пропустить.
    """
    anaphora = [
        " неё ", " него ", " нём ", " ней ",
        " её ", " его ", " им ", " ими ",
        " этой ", " этого ", " этом ", " этому ",
        " той ", " того ", " там ", " туда ", " оттуда ",
        " такой ", " такого ", " такая ",
        " оно ", " они ", " она ",  # опечатки тоже
        "у неё", "у него", "о ней", "о нём",
        "в ней", "в нём", "с ней", "с ним",
        "эта игра", "этот фильм", "эта книга", "этот сериал",
        "это аниме", "эта песня", "этот альбом",
    ]
    lower = f" {text.lower()} "
    return not any(marker in lower for marker in anaphora)


def _translate_only(text: str, local_router) -> Optional[str]:
    """Только перевод на английский без переписывания."""
    try:
        prompt = (
            "Переведи запрос на английский для поиска в Google/DuckDuckGo.\n"
            "Если тема сугубо русская — ответь null.\n"
            "Ответь ТОЛЬКО строкой перевода или словом null.\n\n"
            f"Запрос: {text}"
        )
        response = local_router.get_response(
            messages=[
                {"role": "system", "content": "Переводчик поисковых запросов. Только результат."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=80,
        )
        if not response:
            return None
        response = response.strip().strip('"').strip("'")
        if response.lower() in ("null", "none", ""):
            return None
        return response
    except Exception:
        return None

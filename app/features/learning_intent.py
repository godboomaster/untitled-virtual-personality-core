"""
Определяет, просит ли пользователь НАУЧИТЬ его чему-то (LEARN) или просто
спрашивает информацию по теме (INFO).

Лёгкий вызов LLM с коротким промптом, по образцу need_search.py.
«Научи меня китайскому» → LEARN.
«Расскажи про CBC-MAC» → INFO.
"""

import logging
import re
from app.core.router import ModelRouter
from app.core.local_router import get_local_router

_router = ModelRouter()
_local = get_local_router()

logger = logging.getLogger(__name__)

DECISION_PROMPT = """You are an intent classifier. Reply with exactly one word: LEARN or INFO.

LEARN — the user wants to BE TAUGHT / LEARN something gradually (a course, recurring lessons, step by step).
  Triggers: "научи меня", "обучи меня", "хочу выучить", "давай учить", "будешь учить меня", "teach me".
INFO — the user just asks a question or wants an explanation about a topic ONCE.
  Triggers: "расскажи про", "что такое", "объясни", "как работает", "расскажи о".

Rule: a single question about a specific concept is INFO, NOT LEARN.
When in doubt between teaching vs explaining, answer INFO.

Reply ONLY with LEARN or INFO. Nothing else."""

# Word-boundary keyword gate. «научи» не должно ловить «научный»/«наука».
_LEARN_KEYWORD_RE = re.compile(
    r"\b(?:научи|обучи|научить|обучить|научись|выучи|научимся|обучимся)\b",
    re.IGNORECASE,
)


def _keyword_match(text: str) -> bool:
    """Быстрая проверка по ключевым словам — без вызова LLM."""
    return bool(_LEARN_KEYWORD_RE.search(text))


def classify_learning_intent(text: str) -> str:
    """
    Возвращает 'LEARN' | 'INFO'.
    'LEARN' — пользователь хочет, чтобы его учили (курс, регулярные уроки).
    'INFO'  — обычный вопрос/объяснение по теме.
    """
    user_block = f"\nUSER MESSAGE: {text}"

    # 1. Быстрая проверка по ключевым словам (без LLM)
    if not _keyword_match(text):
        logger.info(f"[LEARN_INTENT] Q='{text[:50]}' -> INFO (no keyword)")
        return "INFO"

    # 2. Локальная модель
    if _local.is_available():
        verdict = _local.classify(
            system_prompt=DECISION_PROMPT,
            user_prompt=user_block,
            valid_outputs=["LEARN", "INFO"],
            temperature=0.0,
            max_tokens=10,
        )
        if verdict:
            logger.info(f"[LEARN_INTENT] Q='{text[:50]}' -> {verdict} (local)")
            return verdict

    # 3. Fallback на основной роутер
    try:
        messages = [
            {"role": "system", "content": DECISION_PROMPT},
            {"role": "user", "content": user_block},
        ]
        answer = _router.get_response(messages, temperature=0.0, max_tokens=5, top_p=1.0)
        raw = (answer or "").strip().upper()
        if "LEARN" in raw:
            verdict = "LEARN"
        elif "INFO" in raw:
            verdict = "INFO"
        else:
            verdict = raw.split()[0] if raw else "INFO"
            verdict = verdict if verdict in ("LEARN", "INFO") else "INFO"
        logger.info(f"[LEARN_INTENT] Q='{text[:50]}' -> {verdict} | raw='{(answer or '').strip()[:80]}'")
        return verdict
    except Exception as e:
        logger.error(f"[LEARN_INTENT] Ошибка: {e}")
        return "INFO"


# Паттерны извлечения темы: «научи меня X», «обучи меня X», «научи меня X на python»
_SUBJECT_PATTERNS = [
    # «научи меня <тема>»
    re.compile(r"\b(?:научи|обучи|научить|обучить)\b\s+(?:меня\s+|нас\s+)?(.+)", re.IGNORECASE),
    # «хочу выучить <тема>»
    re.compile(r"\b(?:хочу|давай)\s+(?:выучить|учить|научиться|обучиться)\s+(.+)", re.IGNORECASE),
]


def extract_subject(text: str) -> str:
    """Извлекает тему обучения из фразы «научи меня X»."""
    for pattern in _SUBJECT_PATTERNS:
        m = pattern.search(text)
        if m:
            subject = m.group(1).strip()
            # Чистим хвосты: «на python с нуля» → «python» (берём ядро)
            subject = re.sub(r"\s*(?:с нуля|с самого начала|пожалуйста|плиз|пож-та)[.!?\s]*$", "", subject, flags=re.IGNORECASE).strip()
            if len(subject) >= 2:
                return subject
    # Fallback — весь текст после удаления обращений
    cleaned = re.sub(r"^(?:(?:коннор|жабка|connor)[,\s]+)+", "", text, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^(?:научи|обучи)\S*\s+(?:меня\s+)?", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned[:80] if cleaned else "эта тема"

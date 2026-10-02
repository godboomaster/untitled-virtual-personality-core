"""
Определяет, просит ли пользователь НАУЧИТЬ его чему-то (LEARN) или просто
спрашивает информацию по теме (INFO).

Сначала — регулярки по формам просьбы, обращённой к боту («научи меня X»,
«хочу выучить X», «можешь научить меня X?», «teach me X»); прошедшее время
(«выучил»), «научись» (учиться самому боту) и отрицание («не хочу учить»)
просьбой не считаются. Затем, если у персоны включено LLM-уточнение
(side_tasks.learning_intent_llm), — лёгкий вызов LLM с коротким промптом.
«Научи меня китайскому» → LEARN.
«Расскажи про CBC-MAC» → INFO.
"""

import logging
import re
from typing import Optional

from app.core.router import ModelRouter
from app.core.local_router import get_local_router
from app.core.language import detect_language, user_language_line

_router = ModelRouter()
_local = get_local_router()

logger = logging.getLogger(__name__)

DECISION_PROMPT = """You are an intent classifier. Reply with exactly one word: LEARN or INFO.

LEARN — the user wants to BE TAUGHT / LEARN something gradually (a course, recurring lessons, step by step).
  Triggers: "научи меня", "обучи меня", "хочу выучить", "давай учить", "будешь учить меня", "teach me", "I want to learn".
INFO — the user just asks a question or wants an explanation about a topic ONCE.
  Triggers: "расскажи про", "что такое", "объясни", "как работает", "расскажи о",
  "научи, как сделать X" / "teach me how to fix X" about one concrete task.

Rule: a single question about a specific concept or a one-off how-to is INFO, NOT LEARN.
When in doubt between teaching vs explaining, answer INFO.

Reply ONLY with LEARN or INFO. Nothing else."""


# ─── Формы просьбы «научи меня» ─────────────────────────────

_WORD = r"[^\s.,!?;:]+"
# «научи/обучи/поучи» — повелительное к боту; «научись» (учись сам) и
# «научил» (прошедшее) сюда не попадают
_TEACH_IMPERATIVE = r"(?:научи|обучи|поучи|научите|обучите|поучите)(?:-ка)?"
# «учи меня» — только с «меня/нас»: голое «учи» — это «учи уроки»
_TEACH_ME = r"(?:учи|учите)\s+(?:меня|нас)"
# «можешь/будешь … научить/учить (меня)»
_MODAL = (r"(?:можешь|сможешь|можете|сможете|мог\s+бы|могла\s+бы|могли\s+бы|"
          r"будешь|будете|станешь)")
_TEACH_INF = r"(?:научить|обучить|поучить|учить|обучать|позаниматься\s+со\s+мной)"
# «хочу/давай … выучить/изучать X» — желание самого пользователя
_WANT = (r"(?:хочу|хотел\s+бы|хотела\s+бы|хочется|давай|давайте|будем|помоги|помогите|"
         r"решил|решила|планирую)")
# + «давай выучим/изучим» (1-е лицо мн. ч.)
_LEARN_INF = (r"(?:выучить|учить|изучить|изучать|научиться|обучиться|освоить|подтянуть|"
              r"выучим|изучим|освоим|поучим|поучимся)")

# Группа rest — всё после глагола: из неё extract_subject берёт тему
_LEARN_REQUEST_RES = [re.compile(p, re.IGNORECASE) for p in (
    rf"\b{_TEACH_IMPERATIVE}\b(?P<rest>.*)",
    rf"\b{_TEACH_ME}\b(?P<rest>.*)",
    rf"\b{_MODAL}\b[^.!?\n]{{0,20}}?\b{_TEACH_INF}\b(?P<rest>.*)",
    rf"\b{_WANT}\b(?:\s+{_WORD}){{0,2}}?\s+{_LEARN_INF}\b(?P<rest>.*)",
    r"\bпозанимай(?:ся|тесь)\s+со\s+мной\b(?P<rest>.*)",
    r"\bдавай(?:те)?\s+позанимаемся\b(?P<rest>.*)",
    r"\b(?:teach|tutor)\s+(?:me|us)\b(?P<rest>.*)",
    r"\bi\s*(?:'d|\s+would)\s+like\s+to\s+(?:learn|study)\b(?P<rest>.*)",
    r"\bi\s+(?:really\s+)?(?:want|need|plan)\s+to\s+(?:learn|study)\b(?P<rest>.*)",
    r"\bi\s+wanna\s+(?:learn|study)\b(?P<rest>.*)",
    r"\b(?:help\s+me|let'?s)\s+(?:learn|study)\b(?P<rest>.*)",
)]
# Отрицание прямо перед просьбой: «не хочу учить», «don't teach me»
_NEGATION_TAIL_RE = re.compile(r"(?:\bне|\bnot|n't|\bdon'?t)\s*$", re.IGNORECASE)
# Служебные слова в начале темы: «меня», «пожалуйста», «с тобой», запятые
_LEADING_FILLER_RE = re.compile(
    r"^(?:[\s,;:–—-]+|(?:меня|нас|мне|с\s+тобой|пожалуйста|плиз|please|me|us)\b)+",
    re.IGNORECASE,
)
# Разовое «как сделать X» вместо курса: «научи, как сварить яйцо», «teach me how to fix it»
_HOWTO_RE = re.compile(
    r"^(?:как|что\s+такое|что\s+значит|почему|зачем|how\b|what\s+is\b|why\b)",
    re.IGNORECASE,
)


def _match_learn_request(text: str) -> Optional[re.Match]:
    # Первая форма просьбы без отрицания перед ней
    for rx in _LEARN_REQUEST_RES:
        for m in rx.finditer(text or ""):
            if not _NEGATION_TAIL_RE.search(text[:m.start()]):
                return m
    return None


def _rest_after_filler(m: re.Match) -> str:
    rest = m.group("rest") or ""
    # Тема — до конца предложения
    rest = re.split(r"[.!?\n]", rest, maxsplit=1)[0]
    return _LEADING_FILLER_RE.sub("", rest).strip()


def learn_request_kind(text: str) -> Optional[str]:
    """Похоже ли сообщение на просьбу научить: 'learn' — да; 'howto' —
    просьба, но про разовое «как сделать X» (курс или ответ — решает LLM,
    без LLM это обычный вопрос); None — нет."""
    m = _match_learn_request(text)
    if not m:
        return None
    return "howto" if _HOWTO_RE.match(_rest_after_filler(m)) else "learn"


def classify_learning_intent(text: str, local_router=None) -> str:
    """Возвращает 'LEARN' | 'INFO'. 'LEARN' — пользователь хочет, чтобы его
    учили (курс, регулярные уроки); 'INFO' — обычный вопрос/объяснение
    по теме. local_router — роутер персоны (без него — общий)."""
    local = local_router or _local
    user_block = f"\nUSER MESSAGE: {text}"
    system_prompt = f"{DECISION_PROMPT}\n\n{user_language_line(detect_language(text))}"

    # 1. Быстрая проверка по формам просьбы (без LLM)
    if not learn_request_kind(text):
        logger.info(f"[LEARN_INTENT] Q='{text[:50]}' -> INFO (no request form)")
        return "INFO"

    # 2. Локальная модель
    if local.is_available(task="learning_intent"):
        verdict = local.classify(
            system_prompt=system_prompt,
            user_prompt=user_block,
            valid_outputs=["LEARN", "INFO"],
            temperature=0.0,
            max_tokens=10,
            task="learning_intent",
        )
        if verdict:
            logger.info(f"[LEARN_INTENT] Q='{text[:50]}' -> {verdict} (local)")
            return verdict

    # 3. Fallback на основной роутер
    try:
        messages = [
            {"role": "system", "content": system_prompt},
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


# Хвост с периодичностью («каждые десять минут», «раз в день», «через 2 часа») —
# не часть темы, срезаем. «через» — только с временным словом, чтобы не резать
# легитимные темы вида «API через requests».
_FREQUENCY_TAIL_RE = re.compile(
    r"[\s,;–—-]+(?:кажд\w+|раз\s+в\b|ежечасн\w*|ежедневн\w*|еженедельн\w*|интервал\w*|"
    r"через\s+(?:\d|полчаса|полтора|день|дн[яейю]|недел|месяц|час|минут|секунд)|"
    r"every|each|once|twice|daily|hourly|weekly)(?:\s.*)?$",
    re.IGNORECASE,
)
# Хвосты, не относящиеся к теме: «с нуля», «пожалуйста», «from scratch»
_SUBJECT_TAIL_RE = re.compile(
    r"[\s,]*(?:с\s+нуля|с\s+самого\s+начала|пожалуйста|плиз|пож-та|from\s+scratch|please)\s*$",
    re.IGNORECASE,
)


def extract_subject(text: str) -> str:
    """Извлекает тему обучения из просьбы («научи меня X», «хочу выучить X»,
    «teach me X»). Пустая строка — тему не назвали («научи меня чему-нибудь»
    сюда тоже не попадёт — «чему-нибудь» станет темой, нормализатор разберёт)."""
    m = _match_learn_request(text)
    if m:
        subject = _rest_after_filler(m)
        # Разовое «как …» — тема то, что после «как»
        subject = re.sub(r"^(?:как|how\s+to|how)\s+", "", subject, flags=re.IGNORECASE)
    else:
        # Не по форме просьбы (например, /learn-подобный текст) — весь текст без
        # обращения в начале («Коннор, …»)
        subject = re.sub(r"^[^\s,]+,\s+", "", (text or "").strip())
    # Срезаем хвост про частоту уроков и прочие не-темы
    subject = _FREQUENCY_TAIL_RE.sub("", subject)
    prev = None
    while prev != subject:
        prev = subject
        subject = _SUBJECT_TAIL_RE.sub("", subject).strip(" ,.;!?")
    return subject[:80] if len(subject) >= 2 else ""

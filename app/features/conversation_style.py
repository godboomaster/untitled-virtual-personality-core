"""
Платформенное правило финальных вопросов («рефлекторный вопрос» в конце
каждой реплики — главный маркер ассистента, а не человека).

Три слоя защиты:

1. Промпт-нота (всегда, для ограниченных режимов): явный запрет рефлекторного
   финального вопроса, вставляется ПОСЛЕДНЕЙ в системный блок — ближе всего
   к месту генерации. Это общесистемный модификатор уровня платформы (как
   help_response_style): персона может усилить/переопределить его своим
   conversation_style в yaml, базовый запрет не зависит от автора персоны.

2. Частотный лимит, а не полный запрет: люди тоже иногда спрашивают.
   Режим per-persona (yaml):
     conversation_style:
       question_frequency: rare   # none | rare | natural | frequent
     none    — финальных вопросов нет вообще (жёстко)
     rare    — дефолт платформы: вопрос только если он реально нужен по смыслу,
               и не чаще, чем через сообщение (серия ≤ 1 подряд)
     natural — платформа не вмешивается (для персон, где вопросы — характер)
     frequent — то же, что natural (явный opt-in в вопрошающий стиль)

3. Пост-обработка (предохранитель): если ответ всё же закончился вопросом
   и серия предыдущих ответов с вопросом уже достигла лимита — по умолчанию
   код срезает финальное вопросительное предложение (trim_tail_question),
   если после него остаётся хоть одно утверждение; иначе ответ остаётся как
   есть. Осторожно: вопрос в кавычках (чужая речь), внутри кода, после
   списка или двоеточия (выбор из вариантов) не трогаем. Пункт «Экономии
   нейронки» question_regen (app/core/llm_economy) — как раньше, ОДНА
   регенерация с усиленным напоминанием (второй полный вызов модели; она
   сама решает, был ли вопрос нужным). И срезка, и регенерация пропускаются,
   когда в ответе есть функциональные маркеры ([TODO_…], [INVENTORY_…],
   [PUNISH:…], [OPEN_URL:…] и т.п., [ФN]) — потерять маркер хуже, чем
   пропустить вопрос.

Серия считается по STM-истории: сколько последних ответов ассистента подряд
заканчивались вопросом (реплики пользователя между ними серию не сбрасывают —
она про ответы бота).
"""

import logging
import re
from typing import List, Dict, Optional

from app.core.language import detect_dialogue_language, user_language_line

logger = logging.getLogger(__name__)

FREQUENCIES = ("none", "rare", "natural", "frequent")
DEFAULT_FREQUENCY = "rare"

# Сколько подряд предыдущих ответов с финальным вопросом допустимо до того,
# как сработает регенерация: none — ни одного (любой «?» → регенерация),
# rare — один (вопрос не чаще, чем через сообщение).
_MAX_STREAK = {"none": 0, "rare": 1}

# Финальный вопрос: «?»/«？» в конце, допускаем хвост из пунктуации/кавычек/
# эмодзи (не букв/цифр) — «Правда? 😊», «Серьёзно?!», 'Он спросил: «Идёшь?»'
_QUESTION_TAIL_RE = re.compile(r"[?？][?!？…]*[^\w]{0,6}$", re.UNICODE)

# Функциональные маркеры, которые регенерация может потерять, — такие ответы
# не трогаем (см. docstring, слой 3)
_FUNCTIONAL_MARKERS_RE = re.compile(
    r"\[(?:TODO|INVENTORY|PUNISH|OPEN_URL|OPEN_APP|RUN_TASK)[A-Z_]*:|\[\s*Ф\s*\d")

# Хвост ответа за финальным вопросом: *действия*/_действия_, эмодзи,
# пунктуация, закрывающие кавычки и скобки — «Как тебе? 😊», «Правда?!»,
# «Как тебе? *улыбается*»
_TAIL_DECOR_RE = re.compile(r"(?:\s*(?:\*[^*\n]{1,80}\*|_[^_\n]{1,80}_|[^\w\s?？*_]))+\s*$")
# Что из хвоста вернуть после срезки: действия и эмодзи (не пунктуацию)
_DECOR_KEEP_RE = re.compile(r"\*[^*\n]{1,80}\*|_[^_\n]{1,80}_|[^\w\s.,!?？…»«\"'”“„)(\[\]:;–—-]+")
# Закрывающая кавычка за вопросом — это чужая речь («Он спросил: «Идёшь?»»)
_QUOTE_CLOSE_RE = re.compile(r"[»”\"']")
# Граница перед последним предложением: конец предыдущего (.!?… и
# закрывающие кавычки/скобки), конец *действия* (не **жирного**) или
# перевод строки
_SENT_BOUNDARY_RE = re.compile(r"(?:[.!?？…]+[)»\"”']*|(?<=[^\s*])\*)\s+|\n")
# Строка-пункт списка: «- …», «* …», «• …», «1. …», «2) …»
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*•–—]|\d+[.)])\s")
_ACTION_RE = re.compile(r"\*[^*\n]+\*|_[^_\n]+_")
# Остаток кончается сокращением («т.е.», «e.g.») — точка была не концом
# предложения, срезка оборвала бы мысль: «Отличная мысль, т.е.»
_ABBR_END_RE = re.compile(
    r"(?:\b(?:т\.\s?[едкпн]|и\.\s?т\.\s?[дп]|e\.\s?g|i\.\s?e|vs|etc|др|пр|см|напр|т\.\s?ч)|\b\w)\.$",
    re.IGNORECASE)

# Счётчики для наблюдаемости (снапшот — LivingPersona.get_state_for_ui)
_STATS = {"notes_applied": 0, "regen_attempts": 0,
          "regen_model_kept_question": 0, "regen_failures": 0,
          "trim_applied": 0, "trim_kept": 0}


def get_stats() -> dict:
    return dict(_STATS)


class ConversationStyleConfig:
    """Разобранный блок conversation_style персоны.

    Применяется платформенно: блока нет в yaml → дефолт rare для любой
    персоны (включая legacy без intellect)."""

    def __init__(self, persona_data: Optional[dict]):
        persona_data = persona_data or {}
        raw = persona_data.get("conversation_style") or {}
        if not isinstance(raw, dict):
            raw = {}
        freq = raw.get("question_frequency", DEFAULT_FREQUENCY)
        if freq not in FREQUENCIES:
            logger.warning(
                f"[ConvStyle] Неизвестный question_frequency {freq!r} — "
                f"беру дефолт {DEFAULT_FREQUENCY!r} (допустимо: {FREQUENCIES})")
            freq = DEFAULT_FREQUENCY
        self.frequency: str = freq

    @property
    def limited(self) -> bool:
        # Платформа ограничивает финальные вопросы (нота + пост-обработка).
        return self.frequency in _MAX_STREAK

    @property
    def max_streak(self) -> Optional[int]:
        return _MAX_STREAK.get(self.frequency)


def ends_with_question(text: Optional[str]) -> bool:
    # Ответ заканчивается вопросом (с учётом хвоста из пунктуации/эмодзи)?
    if not text:
        return False
    return bool(_QUESTION_TAIL_RE.search(text.rstrip()))


def count_question_streak(history: Optional[List[Dict]]) -> int:
    """Сколько последних ответов ассистента подряд закончились вопросом.
    Реплики пользователя между ними серию не сбрасывают."""
    streak = 0
    for msg in reversed(history or []):
        if msg.get("role") != "assistant":
            continue
        if ends_with_question(msg.get("content")):
            streak += 1
        else:
            break
    return streak


def build_style_note(frequency: str) -> Optional[str]:
    """Промпт-нота (слой 1) для вставки ПОСЛЕДНЕЙ в системный блок.
    None — платформа не вмешивается (natural/frequent)."""
    if frequency == "none":
        body = (
            "Do not ask questions at the end of your reply at all: finish the "
            "reply with a statement, the way a person finishes a thought. A "
            "question is allowed only if another instruction in this prompt "
            "explicitly requires answering with a question — it takes priority."
        )
    elif frequency == "rare":
        body = (
            "Do not end your reply with a question unless you truly cannot do "
            "without the user's answer (a choice, clarification or "
            "confirmation is needed for a further action). The reply must "
            "sound complete on its own — the way a person finishes a thought, "
            "not the way an assistant invites the user to keep talking. "
            "Reflexive \"autopilot\" questions (\"And you?\", \"Tell me more?\", "
            "\"Anything else I can help with?\") are forbidden. If another "
            "instruction in this prompt explicitly requires answering with a "
            "question — it takes priority."
        )
    else:
        return None
    _STATS["notes_applied"] += 1
    return f"[CONVERSATION STYLE — system rule]\n{body}"


def has_functional_markers(answer: Optional[str]) -> bool:
    return bool(answer) and bool(_FUNCTIONAL_MARKERS_RE.search(answer))


def should_regenerate(cfg: ConversationStyleConfig, answer: Optional[str],
                      streak: int) -> bool:
    """Нужна ли регенерация (слой 3): режим ограничен, ответ закончился
    вопросом и серия вопросов уже достигла лимита режима."""
    if not cfg.limited or not ends_with_question(answer):
        return False
    if has_functional_markers(answer):
        logger.debug("[ConvStyle] Регенерация пропущена: функциональные маркеры в ответе")
        return False
    return streak >= cfg.max_streak


def _split_tail(text: str):
    # (тело, хвост) — хвост из *действий*/эмодзи/пунктуации за последним словом
    m = _TAIL_DECOR_RE.search(text)
    if not m:
        return text, ""
    return text[:m.start()], text[m.start():]


def _ends_with_question_decorated(text: Optional[str]) -> bool:
    # Финальный вопрос, в т.ч. за хвостом из *действий*/эмодзи
    if not text:
        return False
    if ends_with_question(text):
        return True
    return _split_tail(text.rstrip())[0].endswith(("?", "？"))


def should_trim(cfg: ConversationStyleConfig, answer: Optional[str],
                streak: int) -> bool:
    """Нужна ли срезка финального вопроса кодом (слой 3 по умолчанию): как
    should_regenerate, но вопрос виден и за хвостом из *действий*/эмодзи."""
    if not cfg.limited or not _ends_with_question_decorated(answer):
        return False
    if has_functional_markers(answer):
        logger.debug("[ConvStyle] Срезка пропущена: функциональные маркеры в ответе")
        return False
    return streak >= cfg.max_streak


def trim_tail_question(answer: Optional[str]) -> Optional[str]:
    """Срезает финальные вопросительные предложения ответа (слой 3 без
    модели). None — резать нельзя, ответ остаётся как есть: после срезки не
    осталось бы ни одного утверждения; вопрос в кавычках (чужая речь);
    вопрос — пункт списка или идёт за списком/двоеточием (выбор из
    вариантов); незакрытый блок кода; функциональные маркеры.
    *Действия* и эмодзи из хвоста за вопросом возвращаются в конец."""
    if not answer or has_functional_markers(answer):
        return None
    text = answer.rstrip()
    body, decor = _split_tail(text)
    if not body.endswith(("?", "？")) or _QUOTE_CLOSE_RE.search(decor):
        return None
    if body.count("```") % 2 or _LIST_LINE_RE.match(body.rsplit("\n", 1)[-1]):
        return None
    cut = len(body)
    while True:
        head = body[:cut].rstrip()
        if not head.endswith(("?", "？")):
            break
        start = 0
        for m in _SENT_BOUNDARY_RE.finditer(head):
            start = m.end()
        cut = start
        if not cut:
            break
    remaining = body[:cut].rstrip()
    # Осталось хоть одно утверждение (не только *действия* и знаки)?
    if not re.search(r"\w{2,}", _ACTION_RE.sub(" ", remaining)):
        _STATS["trim_kept"] += 1
        return None
    last_line = remaining.rsplit("\n", 1)[-1]
    if (remaining.count("```") % 2 or _LIST_LINE_RE.match(last_line)
            or remaining.rstrip("*_").endswith(":") or _ABBR_END_RE.search(remaining)):
        _STATS["trim_kept"] += 1
        return None
    keep = _DECOR_KEEP_RE.findall(decor)
    _STATS["trim_applied"] += 1
    return remaining + (" " + " ".join(k.strip() for k in keep) if keep else "")


_REGEN_INSTRUCTION = (
    "Rewrite your last reply so it does NOT end with a question. End it as a "
    "complete statement in your own voice — the way a person finishes a "
    "thought, not the way an assistant invites the user to keep talking. "
    "Keep the meaning, style and roughly the same length. Keep a final "
    "question ONLY if the conversation genuinely cannot continue without the "
    "user's answer (a required choice or clarification), or another "
    "instruction in this conversation explicitly demanded a question — then "
    "keep exactly that one question. Output only the rewritten reply text."
)


def _regen_instruction(messages: List[Dict], lang: Optional[str] = None) -> str:
    # Язык — пользователя в диалоге: иначе регенерация закрепит дрейф языка
    # исходного ответа
    if lang is None:
        last_user = next((m.get("content", "") for m in reversed(messages)
                          if m.get("role") == "user"
                          and isinstance(m.get("content"), str)), "")
        lang = detect_dialogue_language(last_user, messages)
    return f"{_REGEN_INSTRUCTION}\n{user_language_line(lang)}"


def regenerate_without_tail_question(router, messages: List[Dict],
                                     answer: str, settings: dict,
                                     lang: Optional[str] = None) -> Optional[str]:
    """Одна регенерация с усиленным напоминанием (слой 3). Модель сама решает,
    был ли вопрос нужным: нужный оставит, рефлекторный перепишет.
    None — регенерация не удалась, оставляем исходный ответ."""
    _STATS["regen_attempts"] += 1
    try:
        follow_up = messages + [
            {"role": "assistant", "content": answer},
            {"role": "user", "content": _regen_instruction(messages, lang)},
        ]
        new_answer = router.get_response(follow_up, **settings)
    except Exception as e:
        _STATS["regen_failures"] += 1
        logger.debug(f"[ConvStyle] Регенерация не удалась: {e}")
        return None
    if not new_answer or not new_answer.strip():
        _STATS["regen_failures"] += 1
        return None
    if ends_with_question(new_answer):
        # Модель настояла — значит, по её оценке вопрос нужен по смыслу
        _STATS["regen_model_kept_question"] += 1
        logger.info("[ConvStyle] Регенерация: модель оставила вопрос (сочла нужным)")
    else:
        logger.info("[ConvStyle] Рефлекторный финальный вопрос переписан регенерацией")
    return new_answer

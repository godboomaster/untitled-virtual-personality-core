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
   и серия предыдущих ответов с вопросом уже достигла лимита — ОДНА
   регенерация с усиленным напоминанием (модель сама решает, был ли вопрос
   нужным: нужный она оставит, рефлекторный перепишет). Программной обрезки
   текста нет — она рискованна грамматически. Регенерация пропускается, когда
   в ответе есть функциональные маркеры ([TODO_…], [INVENTORY_…], [PUNISH:…],
   [ФN]) — потерять маркер хуже, чем пропустить вопрос.

Серия считается по STM-истории: сколько последних ответов ассистента подряд
заканчивались вопросом (реплики пользователя между ними серию не сбрасывают —
она про ответы бота).
"""

import logging
import re
from typing import List, Dict, Optional

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

# Счётчики для наблюдаемости (снапшот — LivingPersona.get_state_for_ui)
_STATS = {"notes_applied": 0, "regen_attempts": 0,
          "regen_model_kept_question": 0, "regen_failures": 0}


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
        """Платформа ограничивает финальные вопросы (нота + пост-обработка)."""
        return self.frequency in _MAX_STREAK

    @property
    def max_streak(self) -> Optional[int]:
        return _MAX_STREAK.get(self.frequency)


def ends_with_question(text: Optional[str]) -> bool:
    """Ответ заканчивается вопросом (с учётом хвоста из пунктуации/эмодзи)?"""
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
            "Не задавай вопросов в конце ответа вообще: завершай реплику "
            "утверждением, как человек заканчивает мысль. Вопрос допустим, "
            "только если другая инструкция в этом промпте явно требует "
            "ответить вопросом — она важнее."
        )
    elif frequency == "rare":
        body = (
            "Не заканчивай ответ вопросом, если без ответа пользователя тебе "
            "действительно не обойтись (нужен выбор, уточнение или "
            "подтверждение для дальнейшего действия). Реплика должна звучать "
            "завершённой сама по себе — так заканчивает мысль человек, а не "
            "ассистент, приглашающий продолжить диалог. Рефлекторные вопросы "
            "«на автомате» («А у тебя?», «Расскажи подробнее?», «Чем ещё "
            "помочь?») запрещены. Если другая инструкция в этом промпте явно "
            "требует ответить вопросом — она важнее."
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


_REGEN_INSTRUCTION = (
    "Rewrite your last reply so it does NOT end with a question. End it as a "
    "complete statement in your own voice — the way a person finishes a "
    "thought, not the way an assistant invites the user to keep talking. "
    "Keep the meaning, style, language and roughly the same length. Keep a final "
    "question ONLY if the conversation genuinely cannot continue without the "
    "user's answer (a required choice or clarification), or another "
    "instruction in this conversation explicitly demanded a question — then "
    "keep exactly that one question. Output only the rewritten reply text."
)


def regenerate_without_tail_question(router, messages: List[Dict],
                                     answer: str, settings: dict) -> Optional[str]:
    """Одна регенерация с усиленным напоминанием (слой 3). Модель сама решает,
    был ли вопрос нужным: нужный оставит, рефлекторный перепишет.
    None — регенерация не удалась, оставляем исходный ответ."""
    _STATS["regen_attempts"] += 1
    try:
        follow_up = messages + [
            {"role": "assistant", "content": answer},
            {"role": "user", "content": _REGEN_INSTRUCTION},
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

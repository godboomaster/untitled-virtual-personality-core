"""
Стилевые модификаторы ответа на просьбы о помощи (§4 плана уровней интеллекта).

Центральная механика различия primitive / normal / bot: не «насколько персонаж
умный», а КАК он помогает. Три общесистемных фрагмента (не per-persona yaml):

  action_only     (primitive) — действие/минимальный жест, без объяснений
  casual_human    (normal)    — коротко по-бытовому, без ассистентских уточнений
  full_assistant  (bot)       — право на полный разбор: расчёты, код, уточнения

Детекция (§4.1): лёгкий Gemma-классификатор «является ли сообщение просьбой
о помощи в теме (а не бытовым разговором)» — модификатор подключается только
когда он реально нужен, остальное время тон персоны не трогается.

Приоритет (§4.3): tier-модификатор — ограничение СВЕРХУ; он перекрывает
конфликтующие инструкции system_prompt, всё остальное в характере остаётся.
"""

import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from app.core.persona_context import _extract_json

logger = logging.getLogger(__name__)

# Общий пул для фоновой детекции: process_message запускает классификатор
# параллельно с rewrite/памятью/поиском и забирает результат перед сборкой
# промпта — без этого Gemma-вызов добавлял бы задержку к каждому ответу
_detect_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="help-detect")

# Кэш вердиктов детекции: повторные/типичные сообщения («ок», «спасибо»)
# не дёргают Gemma повторно. Ключ — нормализованный текст, TTL 10 минут.
_DETECT_CACHE_TTL_SEC = 600
_DETECT_CACHE_MAX = 256
_detect_cache: "OrderedDict[str, tuple]" = OrderedDict()
_detect_cache_lock = threading.Lock()

# Счётчики для наблюдаемости (снапшот — LivingPersona.get_state_for_ui)
_STATS = {"detect_calls": 0, "cache_hits": 0, "blocks_applied": 0}


def get_stats() -> dict:
    return dict(_STATS)

_HELP_DETECT_PROMPT = """Определи: сообщение пользователя — это просьба о помощи / совете / объяснении / решении конкретной задачи в предметной области (а НЕ бытовой разговор)?

HELP — вопросы «как», «почему», «что делать», просьбы объяснить/посчитать/написать/выбрать, учебные и рабочие задачи.
CHAT — приветствия, болтовня, эмоции, рассказы о себе, реакции, «спасибо», обсуждение самого персонажа.

Верни JSON: {{"is_help_request": true, "domain": "тема одним-двумя словами"}} или {{"is_help_request": false, "domain": ""}}

Сообщение: «{text}»"""

# Фрагменты §4.2 — общие шаблоны уровня системы. Персональные правила подачи
# (как у Коннора — «Мне нужно уточнить одну деталь» перед точным вопросом)
# ложатся поверх, если не противоречат.
STYLE_FRAGMENTS = {
    "action_only": (
        "Если пользователь просит о помощи в предметной области (совет, объяснение, "
        "решение задачи) — ты НЕ можешь объяснять или советовать словами на человеческом "
        "уровне. Либо выполни доступное тебе действие (напоминание/todo/инвентарь), "
        "либо отреагируй минимально — односложно, жестом, без развёрнутого ответа. "
        "Ты не разбираешься в темах, которые не относятся к твоим базовым функциям."
    ),
    "casual_human": (
        "Если пользователь просит о помощи в теме, требующей экспертизы — отвечай "
        "КОРОТКО, как обычный человек, а не эксперт: своими словами, без претензии "
        "на полноту: \"вроде надо делать так\", \"я не спец, но кажется...\". "
        "НЕ задавай уточняющих вопросов в стиле ассистента. НЕ давай точных, "
        "структурированных, развёрнутых формулировок. Можешь ошибаться или дать "
        "неполный ответ — это нормально, ты не справочник."
    ),
    "full_assistant": (
        "Если пользователь просит о помощи в теме — ты можешь разобрать её досконально: "
        "точные расчёты, код с нуля, структурированный анализ, уточняющие вопросы "
        "там, где это нужно для точности ответа. Длина ответа по-прежнему определяется "
        "твоими правилами длины (см. остальной system_prompt) — точность не значит "
        "избыточную многословность."
    ),
}


def detect_help_request(text: str, local_router) -> Optional[dict]:
    """Gemma-детекция просьбы о помощи (§4.1).
    Возвращает {"is_help_request": bool, "domain": str} или None, если
    локальная модель недоступна."""
    if not text or len(text.strip()) < 3:
        return None
    if local_router is None or not local_router.is_available(task="help_detect"):
        return None
    try:
        response = local_router.get_response(
            messages=[
                {"role": "system", "content": "Ты — бинарный классификатор. Отвечаешь только валидным JSON."},
                {"role": "user", "content": _HELP_DETECT_PROMPT.format(text=text[:500])},
            ],
            temperature=0.0,
            max_tokens=80,
            task="help_detect",
        )
        data = _extract_json(response or "")
        if data and "is_help_request" in data:
            return {
                "is_help_request": bool(data.get("is_help_request")),
                "domain": str(data.get("domain", ""))[:60],
            }
        # JSON не осилила — дешёвый бинарный fallback
        verdict = local_router.classify(
            system_prompt="Classify the user message: is it a request for help/advice/explanation in some domain (not small talk)?",
            user_prompt=text[:500],
            valid_outputs=["HELP", "CHAT"],
            temperature=0.0,
            max_tokens=5,
            task="help_detect",
        )
        if verdict:
            return {"is_help_request": verdict == "HELP", "domain": ""}
    except Exception as e:
        logger.debug(f"[HelpStyle] Детекция не удалась: {e}")
    return None


def build_style_block(style: str, domain: str = "") -> Optional[str]:
    """Собирает промпт-фрагмент для вставки в system_prompt (§4.2 + §4.3)."""
    fragment = STYLE_FRAGMENTS.get(style)
    if not fragment:
        return None
    domain_note = f"\nDetected request domain: {domain}." if domain else ""
    return (
        "[HELP RESPONSE MODE — system tier rule]\n"
        f"{fragment}{domain_note}\n"
        "This is a HARD system-level limit: it OVERRIDES any conflicting "
        "instructions in your persona prompt. Within this limit your own "
        "character, speech style and manner remain fully yours."
    )


def _detect_cached(text: str, local_router) -> Optional[dict]:
    """detect_help_request с TTL-кэшем по нормализованному тексту."""
    key = " ".join((text or "").lower().split())[:200]
    now = time.time()
    with _detect_cache_lock:
        hit = _detect_cache.get(key)
        if hit and now - hit[0] < _DETECT_CACHE_TTL_SEC:
            _detect_cache.move_to_end(key)
            _STATS["cache_hits"] += 1
            return dict(hit[1])
    _STATS["detect_calls"] += 1
    verdict = detect_help_request(text, local_router)
    if verdict is not None:
        with _detect_cache_lock:
            _detect_cache[key] = (now, dict(verdict))
            _detect_cache.move_to_end(key)
            while len(_detect_cache) > _DETECT_CACHE_MAX:
                _detect_cache.popitem(last=False)
    return verdict


def build_block_for_message(text: str, intellect, local_router) -> Optional[str]:
    """Полный пайплайн для process_message: детекция → модификатор.
    None — модификатор не нужен (не help-запрос или legacy-режим).

    Fallback-политика при недоступности Gemma: ограничивающие стили
    (action_only, casual_human) применяются ВСЕГДА — ограничение безопаснее
    свободы; full_assistant (разрешающий) не подставывается зря."""
    if intellect is None or not intellect.active:
        return None
    style = intellect.help_response_style
    if not style:
        return None

    verdict = _detect_cached(text, local_router)
    if verdict is not None:
        if not verdict["is_help_request"]:
            return None
        _STATS["blocks_applied"] += 1
        return build_style_block(style, verdict["domain"])

    # Классификатор недоступен: ограничивающие стили — всегда, разрешающий — нет
    if style != "full_assistant":
        logger.debug("[HelpStyle] Gemma недоступна — стилевое ограничение применено без детекции")
        _STATS["blocks_applied"] += 1
        return build_style_block(style)
    return None


def submit_block_for_message(text: str, intellect, local_router):
    """Фоновый вариант build_block_for_message (§4.1): возвращает Future,
    чтобы Gemma-детекция шла параллельно с остальной подготовкой контекста
    в process_message, а не добавляла задержку к ответу.
    None — модификатор заведомо не нужен (legacy/нет стиля)."""
    if intellect is None or not intellect.active:
        return None
    if not intellect.help_response_style:
        return None
    return _detect_pool.submit(build_block_for_message, text, intellect, local_router)

"""
Опциональные служебные LLM-шаги пайплайна сообщения (аддон персоны).

Здесь собраны «пер-месседж» вызовы локальной модели: резолюция местоимений
(query_rewrite), детект help-запроса для intellect tier (help_detect),
LLM-уточнение детекта «научи меня» (learning_intent_llm), LLM-улучшение
поискового запроса (search_query_enhance) и верификация его перевода
(translate_verify). Каждый такой шаг — вызов локальной LLM на каждое
сообщение, из-за которого модель не выгружается из памяти.

По умолчанию всё выключено — базовый пайплайн обходится без служебных
LLM-вызовов. Включается пофлагово в YAML персоны:

features:
  side_tasks:
    query_rewrite: true        # резолюция местоимений/анафоры по истории
    help_detect: true          # intellect tier: LLM-детект help-запроса
    learning_intent_llm: true  # LLM-уточнение LEARN/INFO после keyword-гейта
    search_query_enhance: true # QueryEnhancer для веб-поиска
    translate_verify: true     # LLM-проверка машинного перевода запроса
"""

import logging

logger = logging.getLogger(__name__)


def enabled(bot, name: str) -> bool:
    # Флаг из features.side_tasks персоны; без блока — всё выключено.
    features = getattr(bot, "features", None) or {}
    side = features.get("side_tasks")
    if not isinstance(side, dict):
        return False
    return bool(side.get(name, False))


def rewrite_query_if_enabled(bot, user_input: str, history,
                             persona_context: str = None) -> str:
    # Резолюция местоимений/анафоры. Флаг выкл — исходный текст без LLM.
    if not enabled(bot, "query_rewrite"):
        return user_input
    from app.features.query_rewriter import rewrite_query
    return rewrite_query(
        user_input, history, bot._local_router, persona_context=persona_context)


def submit_help_style_if_enabled(bot, user_input: str):
    """Фоновая детекция help-запроса (intellect tier). None — флаг выкл
    или tier-механики не активны: блок просто не подставится в промпт."""
    if not bot.intellect.active or not enabled(bot, "help_detect"):
        return None
    try:
        from app.features.help_style import submit_block_for_message
        return submit_block_for_message(user_input, bot.intellect,
                                        bot._local_router)
    except Exception as e:
        logger.debug(f"[SideTasks] help_style не запущен: {e}")
        return None


def classify_learning_intent_if_enabled(bot, user_input: str) -> str:
    """'LEARN' | 'INFO'. Флаг выкл — только регулярки по формам просьбы
    («научи меня X», «хочу выучить X», «teach me X»), без LLM-уточнения;
    разовое «научи, как сделать X» без LLM — обычный вопрос."""
    from app.features.learning_intent import learn_request_kind
    kind = learn_request_kind(user_input)
    if not kind:
        return "INFO"
    if not enabled(bot, "learning_intent_llm"):
        return "LEARN" if kind == "learn" else "INFO"
    from app.features.learning_intent import classify_learning_intent
    return classify_learning_intent(user_input, bot._local_router)


def search_enhance_enabled(bot) -> bool:
    # LLM-улучшение запроса веб-поиска (QueryEnhancer).
    return enabled(bot, "search_query_enhance")


def translate_verify_enabled(bot) -> bool:
    # LLM-проверка машинного перевода поискового запроса.
    return enabled(bot, "translate_verify")

"""
Опциональные служебные LLM-шаги пайплайна сообщения (аддон персоны).

Здесь собраны «пер-месседж» вызовы локальной модели: резолюция местоимений
(query_rewrite), детект help-запроса для intellect tier (help_detect),
LLM-уточнение детекта «научи меня» (learning_intent_llm), LLM-улучшение
поискового запроса (search_query_enhance) и верификация его перевода
(translate_verify). Всё это дёргало Ollama на каждое сообщение и не давало
модели выгрузиться из памяти.

По умолчанию всё выключено — базовый пайплайн обходится без служебных
LLM-вызовов. Включается пофлагово в YAML персоны:

features:
  side_tasks:
    query_rewrite: true        # резолюция местоимений/анафоры по истории
    help_detect: true          # intellect tier: LLM-детект help-запроса
    learning_intent_llm: true  # LLM-уточнение LEARN/INFO после keyword-гейта
    search_query_enhance: true # QueryEnhancer для веб-поиска
    translate_verify: true     # LLM-проверка Google-перевода запроса
"""

import logging

logger = logging.getLogger(__name__)


def enabled(bot, name: str) -> bool:
    """Флаг из features.side_tasks персоны; без блока — всё выключено."""
    features = getattr(bot, "features", None) or {}
    side = features.get("side_tasks")
    if not isinstance(side, dict):
        return False
    return bool(side.get(name, False))


def rewrite_query_if_enabled(bot, user_input: str, history,
                             persona_context: str = None) -> str:
    """Резолюция местоимений/анафоры. Флаг выкл — исходный текст без LLM."""
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
    """'LEARN' | 'INFO'. Флаг выкл — только keyword-гейт («научи/обучи/
    выучить…»), без LLM-уточнения."""
    from app.features.learning_intent import _keyword_match
    if not _keyword_match(user_input):
        return "INFO"
    if not enabled(bot, "learning_intent_llm"):
        return "LEARN"
    from app.features.learning_intent import classify_learning_intent
    return classify_learning_intent(user_input)


def search_enhance_enabled(bot) -> bool:
    """LLM-улучшение запроса веб-поиска (QueryEnhancer)."""
    return enabled(bot, "search_query_enhance")


def translate_verify_enabled(bot) -> bool:
    """LLM-проверка Google-перевода поискового запроса."""
    return enabled(bot, "translate_verify")

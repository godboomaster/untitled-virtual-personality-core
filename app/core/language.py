"""Определение языка сообщений пользователя для правила языка ответа.

Проект двуязычный (русский/английский), поэтому детект скриптовый:
кириллица → 'ru', латиница → 'en'. Это тот же подход, что раньше
жил локально в rhythm_manager._lang и reminder_manager._reminder_lang,
теперь в одном месте для всех.
"""

import re
from typing import Dict, List, Optional

_CYR_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_LAT_RE = re.compile(r"[a-z]", re.IGNORECASE)

# Синтетические обёртки для файлов/картинок код пишет на английском —
# это не речь пользователя, определять язык по ним нельзя
_SYNTHETIC_PREFIXES = ("the user sent",)

_LANGUAGE_NAMES = {"ru": "Russian", "en": "English"}
_LANGUAGE_NAMES_RU = {"ru": "русский", "en": "английский"}


def detect_language(text: str) -> Optional[str]:
    """Язык одного текста: 'ru' / 'en' / None (букв нет или синтетика)."""
    if not text:
        return None
    if text.strip().lower().startswith(_SYNTHETIC_PREFIXES):
        return None
    cyr = len(_CYR_RE.findall(text))
    lat = len(_LAT_RE.findall(text))
    if cyr > 0 and cyr >= lat:
        return "ru"
    if lat > 0:
        return "en"
    return None


def detect_dialogue_language(user_message: str,
                             history: Optional[List[Dict]] = None,
                             sender_id=None) -> Optional[str]:
    """Язык, на котором сейчас говорит пользователь.

    Текущее сообщение важнее истории: язык ответа задаёт то, что написано
    сейчас. Если в нём нет букв (эмодзи, цифры) или это синтетика — берём
    последний определимый язык ЭТОГО же пользователя из истории
    (в групповом чате чужие реплики не должны переключать язык).
    """
    lang = detect_language(user_message)
    if lang:
        return lang
    for msg in reversed(history or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        msg_sender = msg.get("sender_id")
        if sender_id and msg_sender and str(msg_sender) != str(sender_id):
            continue
        lang = detect_language(msg.get("content", ""))
        if lang:
            return lang
    return None


def language_name(code: Optional[str]) -> Optional[str]:
    return _LANGUAGE_NAMES.get(code or "")


def language_name_ru(code: Optional[str]) -> Optional[str]:
    """Имя языка по-русски — для промптов, написанных на русском."""
    return _LANGUAGE_NAMES_RU.get(code or "")


def response_language_note(code: Optional[str]) -> Optional[str]:
    """Директива языка ответа для системного промпта.

    None — язык не определён, ноту не добавляем (персона говорит как
    обычно). Вставляется ПОСЛЕДНЕЙ в системный блок: инструкция ближе
    к генерации выполняется стабильнее и должна перекрывать язык всех
    инструкций выше.
    """
    name = language_name(code)
    if not name:
        return None
    return (
        "\n\n[RESPONSE LANGUAGE — highest priority rule]\n"
        f"The user's message is written in {name}. You MUST write your reply in {name}.\n"
        "The language of the user's current message ALWAYS determines the language "
        "of your reply. This rule has priority over the persona format and over "
        "the language of any instructions, memories, context or fragments in this "
        "prompt. If the user switches language — switch with them. "
        "Do not mix languages in one reply."
    )

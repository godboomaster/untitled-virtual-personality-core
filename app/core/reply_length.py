"""Длина ответа: бюджет в токенах → «примерно столько слов и предложений».

API-провайдеру лимит длины передаётся числом (max_tokens). Веб-чату в
браузере — нельзя: сайт пишет столько, сколько хочет (эссе на тысячи знаков
за минуту-две). Поэтому веб-чату тот же бюджет уходит словами в промпт
(webchat_note): «не больше ~N слов, примерно A–B предложений».

Токен в словах — по языку реплики (кириллица дробится мельче):
  английский ≈ 0.75 слова на токен, русский ≈ 0.5;
предложение — 12–25 слов (короткие разговорные — до длинных описательных).

Бюджет, полоса и что уходит в промпт (русский):
  < 40 токенов        — ничего: классификаторы «да/нет», формат задан задачей
  40–1200             — «at most about N words (roughly A–B sentences)»
  > 1200              — ничего: обычная реплика персоны (1500–3000), лимит
                        не ограничивает, а число слов в промпте только
                        подталкивает растянуть ответ
Пример: 400 токенов (голосом) → ~200 слов, 8–17 предложений; 200 → ~100
слов, 4–8; 100 → ~50 слов, 2–4. Числа — верхняя граница, не цель.

Константы — ниже; WEBCHAT_LENGTH_HINT=0 в .env — без подсказки."""
from __future__ import annotations

import os
import re
from typing import Optional

MIN_TOKENS = 40
MAX_TOKENS = 1200
WORDS_PER_TOKEN = {"en": 0.75, "ru": 0.5}
SENTENCE_WORDS = (12, 25)  # короткое и длинное предложение, слов

_SENTENCE_END = re.compile(r'[.!?…](?:[»"\')\]]+)?(?=\s|$)')


def enabled() -> bool:
    return (os.getenv("WEBCHAT_LENGTH_HINT") or "1").strip().lower() not in ("0", "false", "no", "off")


def text_lang(text: str) -> str:
    lat = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    cyr = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch in "ёЁ")
    return "en" if lat > cyr else "ru"


def budget_words(max_tokens: int, lang: str = "ru") -> tuple[int, int, int]:
    """Бюджет → (слов не больше, предложений от, до)."""
    words = max(5, int(round(max_tokens * WORDS_PER_TOKEN.get(lang, 0.5) / 5.0)) * 5)
    lo = max(1, round(words / SENTENCE_WORDS[1]))
    hi = max(lo, round(words / SENTENCE_WORDS[0]))
    return words, lo, hi


def _last_user_text(messages: list) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


def webchat_note(max_tokens, messages: list) -> Optional[str]:
    """Подсказка длины для веб-чата (в инструкции промпта); None — бюджет не
    ограничивает или подсказка выключена."""
    try:
        n = int(max_tokens)
    except (TypeError, ValueError):
        return None
    if not enabled() or n < MIN_TOKENS or n > MAX_TOKENS:
        return None
    words, lo, hi = budget_words(n, text_lang(_last_user_text(messages)))
    sentences = f"{lo}" if lo == hi else f"{lo}–{hi}"
    return (f"Length limit: at most about {words} words (roughly {sentences} "
            f"sentence{'s' if hi > 1 else ''}). Stop once the point is made; "
            "this limits length only, keep any format the task asks for.")


def trim_to_sentence(text: str, min_keep: float = 0.4) -> str:
    """Ответ оборван лимитом посреди фразы — до последней законченной. Если
    законченная часть меньше min_keep от текста (одна длинная фраза) — как
    есть: обрывок лучше, чем почти ничего."""
    t = (text or "").rstrip()
    ends = list(_SENTENCE_END.finditer(t))
    if not ends:
        return t
    cut = ends[-1].end()
    if cut >= len(t) or cut < len(t) * min_keep:
        return t
    return t[:cut].rstrip()

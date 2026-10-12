"""Имя персоны в распознанной речи (голосовая активация, «Коннор, …»).

Распознаватель пишет имена на слух: «коннер» вместо «коннор», «родис»
вместо «арродес», «жабко» вместо «жабка». Поэтому сравнение — по грубому
фонетическому ключу (безударные гласные сведены, двойные согласные — одна,
оглушение на конце, латиница — кириллицей) с допуском по взвешенному
расстоянию правки: путаница гласных стоит половину, согласных — целую.

Имя ищется только в начале реплики (первые MAX_LEAD слов): «а Коннор умеет…»
будит, «мы вчера с Коннором…» — нет.

Тот же алгоритм — в приложении Android (WakeMatch.java); общие примеры —
scripts/data/wake_match_cases.json, их гоняют оба теста.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

# Сколько слов может стоять перед именем: «эй, Коннор», «слушай, Коннор»
MAX_LEAD = 2
# Имя распознаватель может склеить или разбить: окно до стольких слов
MAX_WINDOW = 3

_LATIN = [
    ("sch", "ш"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"),
    ("yu", "ю"), ("ya", "я"), ("yo", "е"), ("ck", "к"), ("ph", "ф"), ("th", "т"),
    ("ee", "и"), ("oo", "у"), ("x", "кс"), ("q", "к"), ("w", "в"), ("y", "и"),
    ("j", "дж"), ("h", "х"), ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"),
    ("e", "е"), ("f", "ф"), ("g", "г"), ("i", "и"), ("k", "к"), ("l", "л"),
    ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"),
    ("t", "т"), ("u", "у"), ("v", "в"), ("z", "з"),
]
_VOWEL_MAP = {"о": "а", "я": "а", "е": "и", "э": "и", "ы": "и", "й": "и", "ю": "у"}
_DEVOICE = {"б": "п", "в": "ф", "г": "к", "д": "т", "ж": "ш", "з": "с"}
_VOWELS = frozenset("аиу")
_WORD = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)


def _translit(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        for lat, cyr in _LATIN:
            if s.startswith(lat, i):
                out.append(cyr)
                i += len(lat)
                break
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def to_cyrillic(text: str) -> str:
    """Имя кириллицей для подсказки распознавателю (hotwords): русская
    модель латиницу не закодирует. «Connor» → «коннор»."""
    s = _translit(text.lower().replace("ё", "е"))
    return " ".join("".join(ch if "а" <= ch <= "я" else " " for ch in s).split())


def phonetic_key(text: str) -> str:
    """Грубый «звук» слова или фразы (пробелы выкидываются)."""
    s = _translit(text.lower().replace("ё", "е"))
    s = "".join(_VOWEL_MAP.get(ch, ch) for ch in s if "а" <= ch <= "я")
    s = s.replace("ь", "").replace("ъ", "")
    if s and s[-1] in _DEVOICE:
        s = s[:-1] + _DEVOICE[s[-1]]
    # Двойные буквы — одна: «арродес» и «ародес» звучат одинаково
    out = []
    for ch in s:
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


def _cost(ch: str) -> float:
    return 0.5 if ch in _VOWELS else 1.0


def key_distance(a: str, b: str) -> float:
    """Расстояние правки: гласные (вставка, удаление, замена гласной на
    гласную) — 0.5, всё остальное — 1."""
    prev = [0.0]
    for ch in b:
        prev.append(prev[-1] + _cost(ch))
    for ca in a:
        cur = [prev[0] + _cost(ca)]
        for j, cb in enumerate(b, 1):
            if ca == cb:
                sub = 0.0
            elif ca in _VOWELS and cb in _VOWELS:
                sub = 0.5
            else:
                sub = 1.0
            cur.append(min(prev[j - 1] + sub, prev[j] + _cost(ca), cur[j - 1] + _cost(cb)))
        prev = cur
    return prev[-1]


def tolerance(key: str) -> float:
    """Допуск по длине ключа имени: короткое — почти точно."""
    n = len(key)
    if n <= 3:
        return 0.0
    if n <= 5:
        return 0.5
    if n <= 7:
        return 1.0
    return 1.5


@dataclass(frozen=True)
class WakePhrase:
    persona: str
    phrase: str
    key: str


@dataclass(frozen=True)
class WakeHit:
    persona: str
    phrase: str
    # Что сказано после имени («какая сегодня погода»), может быть пустым
    command: str
    distance: float


def wake_phrases(personas: Iterable[dict], only: Optional[Iterable[str]] = None) -> list[WakePhrase]:
    """Имена для пробуждения из ответа GET /api/personas: features.trigger_words.
    Персоны без своих trigger_words голосом не будятся (имя вроде «Ассистент» —
    обычное слово). only — ограничить списком id (WAKE_PERSONAS). Одинаковое
    по звуку имя у двух персон достаётся первой."""
    allow = {p.strip() for p in only or () if p.strip()} or None
    seen: set[str] = set()
    out: list[WakePhrase] = []
    for p in personas:
        pid = str(p.get("id") or "")
        if not pid or (allow is not None and pid not in allow):
            continue
        words = (p.get("features") or {}).get("trigger_words") or []
        for w in words:
            if not isinstance(w, str) or not w.strip():
                continue
            key = phonetic_key(w)
            if len(key) < 3 or key in seen:
                continue
            seen.add(key)
            out.append(WakePhrase(persona=pid, phrase=w.strip(), key=key))
    return out


def match_wake(text: str, phrases: list[WakePhrase]) -> Optional[WakeHit]:
    """Имя в начале реплики → кто разбужен и что сказано после имени."""
    if not phrases or not text:
        return None
    tokens = list(_WORD.finditer(text))
    best: Optional[tuple[float, int, WakePhrase, int]] = None
    for start in range(min(MAX_LEAD + 1, len(tokens))):
        for size in range(1, MAX_WINDOW + 1):
            end = start + size
            if end > len(tokens):
                break
            key = phonetic_key("".join(t.group() for t in tokens[start:end]))
            if not key:
                continue
            for ph in phrases:
                # Окно заметно длиннее имени — это уже не имя
                if len(key) > len(ph.key) + 3:
                    continue
                d = key_distance(key, ph.key)
                if d > tolerance(ph.key):
                    continue
                # Лучшее — ближе по звуку, затем раньше в реплике, затем длиннее
                rank = (d, start, -len(ph.key))
                if best is None or rank < (best[0], best[1], -len(best[2].key)):
                    best = (d, start, ph, end)
    if best is None:
        return None
    d, _, ph, end = best
    rest = text[tokens[end - 1].end():] if end <= len(tokens) else ""
    command = rest.strip(" \t\n,.!?;:—-")
    return WakeHit(persona=ph.persona, phrase=ph.phrase, command=command, distance=d)

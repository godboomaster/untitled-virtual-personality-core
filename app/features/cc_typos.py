"""Опечатки в командах режима управления.

Команда с опечаткой («включи режим урпавления», «откой ютуб», «пролистй
вниз») или набранная не в той раскладке («jnrhjq .ne,») уходила модели:
ответ шёл секунды, чаще всего «на словах», а человек ждал его и набирал
команду заново. Здесь — дешёвая правка до модели:

- fix_mode_phrase — фраза включения/выключения режима целиком: каждое слово
  сверяется со словарём этой фразы (глаголы, «режим», «управления»,
  control/mode);
- fix_command_verb — первое слово команды: со словарём глаголов лесенки.

Правка — только кандидат: команда засчитывается, если исправленный текст
разобрал обычный regex-парсер (parse_control_mode, лесенка _cc_ladder).
Расстояние — Дамерау (перестановка соседних букв — одна ошибка). Допуск по
длине слова: до 3 букв — не правим; до 4 — только перестановка, пропуск
или лишняя буква (замена в коротком слове чаще даёт другое слово:
fine → find, plan → play); 5–9 букв — одна ошибка; длиннее — две. Ничья
между словами разного смысла — не правим («вылючи»: включи или выключи?).
"""

import re
from typing import Dict, Iterable, Optional

_WORD_RE = re.compile(r"[^\W\d_]+")
_CYR_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_LAT_RE = re.compile(r"[a-z]", re.IGNORECASE)

# Раскладка: та же клавиша в ЙЦУКЕН и QWERTY (macOS/Windows, без Shift —
# буквы; «,» и «.» — на месте «б» и «ю»)
_EN_KEYS = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
_RU_KEYS = "йцукенгшщзхъфывапролджэячсмитьбюё"
_EN2RU = {e: r for e, r in zip(_EN_KEYS, _RU_KEYS)}
_RU2EN = {r: e for e, r in zip(_EN_KEYS, _RU_KEYS)}
# Латиница вместо похожей кириллицы в русском слове («рeжим» с латинской e)
_HOMOGLYPHS = str.maketrans("aeopcxyk", "аеорсхук")


def _limit(word: str) -> int:
    n = len(word)
    if n <= 3:
        return 0
    return 1 if n <= 9 else 2


def _osa(a: str, b: str, limit: int) -> int:
    """Расстояние Дамерау (OSA) с отсечкой: больше limit — limit + 1."""
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    prev2 = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        row_min = cur[0]
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (prev2 is not None and j > 1 and a[i - 1] == b[j - 2]
                    and a[i - 2] == b[j - 1]):
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            row_min = min(row_min, v)
        if row_min > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[lb] if prev[lb] <= limit else limit + 1


def _is_substitution(a: str, b: str) -> bool:
    # Одна замена буквы (не перестановка соседних): длины равны, отличие в
    # одной позиции
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


_FORM_ENDINGS = frozenset({"у", "ю", "а", "о", "я", "е", "л", "ла", "ло", "ли"})
_FORM_SUFFIXES = frozenset({"л", "ла", "ло", "ли",
                            "s", "es", "d", "ed", "r", "er"})


def _other_form(word: str, cand: str) -> bool:
    """Другая форма того же глагола, а не опечатка в нём: «открою» /
    «нажму» (1-е лицо), «включил» / «пришла» (прошедшее), «saved» /
    «types» — человек рассказывает, а не командует."""
    stem = cand[:-1]
    if word.startswith(stem) and word[len(stem):] in _FORM_ENDINGS:
        return True
    return word.startswith(cand) and word[len(cand):] in _FORM_SUFFIXES


def nearest(word: str, vocab: Iterable[str], groups: Optional[Dict[str, str]] = None,
            exclude: Iterable[str] = ()) -> Optional[str]:
    """Слово из vocab, в которое правится word, или None (правка не нужна,
    невозможна или неоднозначна). groups — смысл слова: ничья между словами
    одного смысла («режим»/«режима») — не беда, разного — отказ."""
    w = word.lower().replace("ё", "е")
    vocab = list(vocab)
    if w in vocab or w in exclude:
        return None
    lim = _limit(w)
    if not lim:
        return None
    best, best_d = [], lim + 1
    for v in vocab:
        d = _osa(w, v.replace("ё", "е"), lim)
        if d > lim:
            continue
        if len(w) <= 4 and d == 1 and _is_substitution(w, v):
            continue
        if _other_form(w, v):
            continue
        if d < best_d:
            best, best_d = [v], d
        elif d == best_d:
            best.append(v)
    if not best:
        return None
    if len(best) > 1:
        g = groups or {}
        if len({g.get(v, v) for v in best}) > 1:
            return None
    return best[0]


def switch_layout(text: str) -> Optional[str]:
    """Текст, набранный не в той раскладке: вся латиница → кириллица по
    клавишам («jnrhjq .ne,» → «открой ютуб»), вся кириллица → латиница
    («щзут нщгегиу» → «open youtube»). Смешанный текст — None."""
    s = str(text or "").strip()
    if not s:
        return None
    letters = [c for c in s.lower() if c.isalpha()]
    if not letters:
        return None
    if all(_LAT_RE.match(c) for c in letters):
        table = _EN2RU
    elif all(_CYR_RE.match(c) for c in letters):
        table = _RU2EN
    else:
        return None
    out = "".join(table.get(c, c) for c in s.lower())
    return out if out != s.lower() else None


def _fix_homoglyphs(word: str) -> str:
    # Латинская «e»/«o»/«a» внутри русского слова — та же кириллица
    if _CYR_RE.search(word) and _LAT_RE.search(word):
        return word.lower().translate(_HOMOGLYPHS)
    return word


# ── Фраза включения/выключения режима ──

_MODE_ON = ("перейди", "переключись", "войди", "зайди", "включи", "активируй",
            "enter", "start", "enable", "activate", "switch")
_MODE_OFF = ("выйди", "выйти", "выключи", "отключи", "покинь", "покинуть",
             "деактивируй", "exit", "leave", "quit", "stop", "disable",
             "deactivate")
_MODE_REST = ("режим", "режима", "управления", "управление", "control", "mode",
              "turn", "into", "the")
_MODE_GROUPS = {**{w: "on" for w in _MODE_ON}, **{w: "off" for w in _MODE_OFF},
                "режим": "mode", "режима": "mode",
                "управления": "control", "управление": "control"}
_MODE_VOCAB = _MODE_ON + _MODE_OFF + _MODE_REST


def _fix_words(text: str, fix) -> Optional[str]:
    """Каждое слово text — через fix(слово) → замена или None (оставить).
    Ничего не поменялось — None."""
    changed = False

    def sub(m):
        nonlocal changed
        w = m.group(0)
        g = _fix_homoglyphs(w)
        r = fix(g) or (g if g != w else None)
        if r and r != w:
            changed = True
            return r
        return w

    out = _WORD_RE.sub(sub, text)
    return out if changed else None


def fix_mode_phrase(text: str) -> Iterable[str]:
    """Кандидаты исправления фразы режима — по порядку: опечатки в словах,
    затем та же фраза в другой раскладке (и её опечатки). Решает
    parse_control_mode: засчитывается кандидат, который разобрала регулярка."""
    s = str(text or "")
    seen = set()
    for base in (s, switch_layout(s)):
        if not base:
            continue
        for cand in (base if base != s else None,
                     _fix_words(base, lambda w: nearest(w, _MODE_VOCAB, _MODE_GROUPS))):
            if cand and cand not in seen:
                seen.add(cand)
                yield cand


# ── Первое слово команды режима управления ──

# Глаголы лесенки (computer_control: открыть/клик/ввод/листание/наведение/
# вкладки/чтение/масштаб/медиа…). Повелительное наклонение — так пишут
# команды; инфинитивы «можешь открыть» normalize_command уже свёл к нему
_CMD_VERBS_RU = (
    "открой", "запусти", "включи", "выключи", "нажми", "кликни", "тыкни",
    "ткни", "щелкни", "наведи", "подведи", "подержи", "введи", "впиши",
    "напиши", "набери", "заполни", "отправь", "пришли", "скинь", "скачай",
    "сохрани", "закрой", "сверни", "скрой", "удали", "сотри", "убери", "найди",
    "поищи", "отыщи", "покажи", "перейди", "переключись", "переключи",
    "вернись", "верни", "зайди", "войди", "пролистай", "полистай", "листай",
    "прокрути", "покрути", "промотай", "перемотай", "отмотай", "проскролль",
    "скролль", "докрути", "долистай", "доскролль", "прочитай", "прочти",
    "зачитай", "увеличь", "уменьши", "прибавь", "убавь", "поставь",
    "установи", "выставь", "передвинь", "перетащи", "измени", "поменяй",
    "останови", "продолжай", "добавь", "посмотри", "глянь", "обнови",
    "перезагрузи")
_CMD_VERBS_EN = (
    "open", "launch", "start", "click", "press", "hover", "type", "enter",
    "write", "send", "download", "save", "close", "hide", "dismiss", "find",
    "search", "show", "switch", "read", "play", "watch", "pause", "skip",
    "rewind", "delete", "erase", "remove", "scroll", "zoom", "submit",
    "navigate", "visit", "refresh", "reload")
# Настоящие слова в шаге от глагола-команды: «смотри» — не «сотри», «верно»
# — не «верни», «lunch» — не «launch»
_CMD_NOT_TYPO = frozenset({
    "смотри", "веди", "верно", "убеди", "пошли", "найти", "войти", "выйти",
    "clone", "clock", "dress", "cover", "lover", "cause", "white", "wrote",
    "spend", "match", "patch", "batch", "catch", "remote", "stroll", "twitch",
    "lunch", "plays", "shows", "finds", "opens", "sends", "types", "saves",
})
_CMD_GROUPS = {"включи": "on", "выключи": "off"}
CMD_VERBS = _CMD_VERBS_RU + _CMD_VERBS_EN
OPEN_VERBS = frozenset({"открой", "запусти", "включи", "open", "launch", "start"})
# Первое слово реплики в чужой раскладке, по которому её стоит перенабрать:
# глагол команды или начало фразы режима («ht;bv eghfdktybz»)
_LAYOUT_LEADS = tuple(dict.fromkeys(
    CMD_VERBS + _MODE_ON + _MODE_OFF + ("режим", "режима", "control")))
_LAYOUT_GROUPS = {**_MODE_GROUPS, **_CMD_GROUPS}


def _first_word(text: str):
    m = _WORD_RE.search(text)
    if not m or text[:m.start()].strip():
        return None
    return m


def fix_command_verb(text: str) -> Optional[str]:
    """Команда с исправленным первым словом-глаголом: «откой ютуб» →
    «открой ютуб», «jnrhjq .ne,» → «открой ютуб». None — первое слово уже
    глагол лесенки, на глагол не похоже или правка неоднозначна. Годится
    ли исправленное — решает лесенка (regex-парсеры)."""
    s = str(text or "").strip()
    if not s:
        return None
    m = _first_word(s)
    if not m:
        return None
    word = m.group(0).lower().replace("ё", "е")
    if word in CMD_VERBS:
        return None
    glyph = _fix_homoglyphs(word)
    fixed_word = glyph if glyph in CMD_VERBS else nearest(
        glyph, CMD_VERBS, _CMD_GROUPS, exclude=_CMD_NOT_TYPO)
    if fixed_word:
        return fixed_word + s[m.end():]
    # Не та раскладка: засчитываем, только если первое слово — глагол
    # (опечатку в перенабранном слове тоже прощаем)
    other = switch_layout(s)
    if other:
        m2 = _first_word(other)
        if m2:
            w2 = m2.group(0)
            if w2 in CMD_VERBS:
                return other
            fw = nearest(w2, CMD_VERBS, _CMD_GROUPS, exclude=_CMD_NOT_TYPO)
            if fw:
                return fw + other[m2.end():]
    return None


def layout_command(text: str) -> Optional[str]:
    """Сырая реплика целиком в чужой раскладке, если после перенабора она
    начинается с глагола-команды: «jnrhjq .ne,» → «открой ютуб». Нужна до
    normalize_command: та срезала бы «,» и «.» по краям, а в чужой
    раскладке это буквы «б» и «ю»."""
    other = switch_layout(text)
    if not other:
        return None
    m = _first_word(other)
    if not m:
        return None
    w = m.group(0)
    if w in _LAYOUT_LEADS or nearest(w, _LAYOUT_LEADS, _LAYOUT_GROUPS,
                                     exclude=_CMD_NOT_TYPO):
        return other
    return None


def first_verb(text: str) -> str:
    m = _first_word(str(text or "").strip())
    return m.group(0).lower().replace("ё", "е") if m else ""

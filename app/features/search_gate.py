"""Гейт веб-поиска: нужен ли DuckDuckGo для этого сообщения.

Раньше поиск запускался на КАЖДОЕ сообщение: «коннор?» (обращение по имени)
гуглилось как имя и тянуло в промпт Википедию про Джона Коннора, а «что
сегодня делал?» (вопрос о дне самой персоны) — случайные страницы, которые
по SOURCE PRIORITY перебивали дневник персоны. Плюс до 25 с ожидания поиска
на критическом пути ответа.

Отсекаем только то, что уверенно не требует внешних данных, — целиком
совпадающие шаблоны после снятия обращения по имени:
  - пустое сообщение / одно обращение по имени («коннор?», «эй, коннор»);
  - реплики-связки: приветствия, «ок», «спасибо», «да/нет»…;
  - вопросы о самой персоне: что делал/делаешь, как дела, как день прошёл,
    где был, как себя чувствуешь…

Шаблоны сопоставляются со всей строкой (fullmatch): «что сегодня делал
путин?» или «ты знаешь погоду в Москве?» проходят в поиск как раньше.
"""

import re
from typing import Iterable

_PUNCT_RE = re.compile(r"[^\w\s-]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")

# Обращения и междометия, которые снимаем вместе с именем персоны
_ADDRESS_WORDS = {"эй", "слушай", "ну", "а", "и", "так", "hey", "hi", "yo"}

# Реплики-связки целиком: ответ на них — из разговора, не из интернета
_SMALLTALK = {
    "привет", "приветик", "здравствуй", "здравствуйте", "здорово", "хай",
    "добрый день", "доброе утро", "добрый вечер", "доброй ночи", "спокойной ночи",
    "пока", "до встречи", "до завтра", "спасибо", "спс", "благодарю", "пожалуйста",
    "ок", "окей", "ага", "угу", "да", "нет", "неа", "ясно", "понятно", "хорошо",
    "ладно", "круто", "класс", "супер", "отлично", "норм", "хм", "ммм", "ау",
    "ты тут", "ты здесь", "ты где", "ты со мной", "ты меня слышишь", "слышишь",
    "hello", "hey", "hi", "thanks", "thank you", "ok", "okay", "yes", "no", "bye",
    "good morning", "good night", "are you there", "you there",
}

_TIME = r"(?:сегодня|вчера|сейчас|днем|утром|вечером|ночью|весь день|за день|с утра|недавно|без меня|пока меня не было)"
_YOU = r"(?:ты|тебя|тебе|у тебя|с тобой)"

# Вопросы о самой персоне — полностью, с необязательными «ты»/временем
_SELF_PATTERNS = [
    # что (ты) (сегодня) делал(а) / делаешь / поделывал / успел
    rf"(?:что|чем)(?: {_YOU})?(?: {_TIME})*(?: {_YOU})?(?: {_TIME})* "
    rf"(?:делал\w*|делаешь|поделыва\w*|успел\w*|занимал\w*|занимаешься|занят\w*|был\w* занят\w*)"
    rf"(?: {_YOU})?(?: {_TIME})*(?: хорошего| интересного| нового)?",
    # как (у тебя) дела / настроение / день (прошёл) / самочувствие
    rf"как(?: {_YOU})?(?: {_TIME})? (?:дела|делишки|настроение|день|денек|самочувствие|жизнь|ты|поживаешь|сам|сама)"
    rf"(?: {_YOU})?(?: прош[её]л| прошел)?(?: {_TIME})?",
    rf"как(?: {_YOU})? прош[её]л(?: тво[йе])? день",
    rf"как(?: {_YOU})? себя чувствуешь(?: {_TIME})?",
    # где (ты) был(а) / что нового (у тебя)
    rf"где(?: {_YOU})? (?:был\w*|пропадал\w*)(?: {_TIME})?",
    rf"что(?: {_YOU})? нового(?: {_YOU})?(?: {_TIME})?",
    rf"(?:расскажи|рассказывай)(?: мне)?(?: про| о)? (?:свой|твой|как прош[её]л)? ?(?:день|дела)",
    # English
    r"(?:what|what have) (?:did )?(?:you )?(?:do|done|doing|been doing|been up to|up to)(?: today| yesterday| lately)?",
    r"what are you (?:doing|up to)(?: now| today)?",
    r"how (?:are|r) (?:you|u)(?: doing)?(?: today)?",
    r"how(?:'s| is| was) (?:your|ur) day",
    r"how(?:'s| is) it going",
]
_SELF_RE = [re.compile(p) for p in _SELF_PATTERNS]


def _normalize(text: str) -> str:
    t = (text or "").lower().replace("ё", "е")
    t = _PUNCT_RE.sub(" ", t)
    return _SPACE_RE.sub(" ", t).strip()


def _strip_names(text: str, names: Iterable[str]) -> str:
    """Снимает обращение по имени/триггеру и междометия с краёв фразы."""
    name_set = {_normalize(n) for n in names if n and _normalize(n)}
    words = text.split()

    def is_address(i: int) -> int:
        """Длина обращения, начинающегося с words[i] (многословные имена)."""
        for n in sorted(name_set, key=len, reverse=True):
            nw = n.split()
            if words[i:i + len(nw)] == nw:
                return len(nw)
        return 1 if words[i] in _ADDRESS_WORDS else 0

    changed = True
    while words and changed:
        changed = False
        k = is_address(0)
        if k:
            words = words[k:]
            changed = True
            continue
        for n in sorted(name_set, key=len, reverse=True):
            nw = n.split()
            if len(words) >= len(nw) and words[-len(nw):] == nw:
                words = words[:-len(nw)]
                changed = True
                break
    return " ".join(words)


def search_skip_reason(text: str, names: Iterable[str] = ()) -> str | None:
    """Причина НЕ искать в вебе ('ping' / 'smalltalk' / 'self') или None,
    если поиск уместен. names — имя персоны и её trigger_words."""
    core = _strip_names(_normalize(text), names)
    if not core:
        return "ping"
    if core in _SMALLTALK:
        return "smalltalk"
    if any(r.fullmatch(core) for r in _SELF_RE):
        return "self"
    return None


def is_self_question(text: str, names: Iterable[str] = ()) -> bool:
    """Вопрос о самой персоне (её дне, делах, самочувствии)."""
    return search_skip_reason(text, names) == "self"

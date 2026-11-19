"""Гейт веб-поиска: нужен ли внешний поиск для этого сообщения.

Без фильтра поиск уходил бы на КАЖДОЕ сообщение: голое обращение по имени
персоны искалось бы как имя и тянуло в промпт статью про однофамильца, а
вопрос о дне самой персоны («что сегодня делал?») — случайные страницы,
которые по SOURCE PRIORITY перебивали бы дневник персоны. Плюс до 25 с
ожидания поиска на критическом пути ответа.

Отсекаем только то, что уверенно не требует внешних данных, — целиком
совпадающие шаблоны после снятия обращения по имени:
  - пустое сообщение / одно обращение по имени;
  - реплики-связки: приветствия, «ок», «спасибо», «да/нет»…;
  - вопросы о самой персоне: что делал/делаешь, как дела, как день прошёл,
    где был, как себя чувствуешь…

Шаблоны сопоставляются со всей строкой (fullmatch): «что сегодня делал
президент?» или «ты знаешь погоду в Москве?» уходят в поиск как обычно.

Белый список (search_need_reason, «Экономия нейронки»): без него поиск (AI
Mode Google) шёл почти на каждое сообщение. По умолчанию ищем, только когда
поиск и правда нужен:
  - явная просьба найти: «найди», «загугли», «в интернете», search, look up…;
  - тема, которой без внешних данных не ответить: погода, новости, курс,
    цена, счёт матча, релиз — в вопросе, просьбе или короткой реплике;
  - вопрос не к самой персоне (без «ты/вы», «думаешь»…) о внешнем:
    «кто такой X», с сущностью (имя собственное, латиница в русском тексте,
    год, кавычки) или о свежем («сегодня», «сейчас», «последние», latest).
Пункт search_every_message (app/core/llm_economy) — по-старому, только
чёрный список. Списки уходят телефону в ките (offline_api._search_gate):
телефон без ноутбука решает так же (web/src/offline/gate.ts), сверка —
scripts/test_offline_gate.py.
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

# ── Белый список: когда поиск НУЖЕН ──
# Слова — в виде после _normalize (строчные, ё→е, без пунктуации). «*» в
# конце — префикс (русские окончания), пробел — фраза из слов подряд.
_NEED = {
    # Явная просьба найти — где угодно в реплике
    "search": [
        "найди", "найдите", "поищи", "поищите", "поискать", "ищи", "загугли",
        "загуглить", "загуглите", "погугли", "погуглить", "погуглите", "нагугли",
        "прогугли", "гугли", "гуглани", "пробей", "узнай", "узнайте", "выясни",
        "разузнай", "можешь найти", "сможешь найти", "помоги найти",
        "в интернете", "в инете", "в сети", "в гугле", "в google", "в яндексе",
        "в поисковике",
        "look up", "lookup", "find out", "search for", "google it", "google that",
        "google this", "can you google", "could you google", "please google",
        "can you search", "can you find", "could you find", "on the internet",
        "on the web", "web search",
    ],
    # То же, но только первым словом: английские глаголы многозначны
    # («I love Google», «where did I find it»)
    "search_start": ["search", "google", "find", "look"],
    # Без внешних данных не ответить — в вопросе, просьбе или короткой реплике
    "topic": [
        "погод*", "прогноз*", "новост*", "котировк*", "бирж*", "акции", "акций",
        "курс доллар*", "курс евро", "курс рубл*", "курс валют*", "курс биткоин*",
        "курс юан*", "курс крипт*", "курс акци*", "цена", "цены", "цену", "ценой",
        "цене", "ценах", "ценник*", "стоимость", "стоимости", "сколько стоит",
        "сколько стоят", "сколько стоил*", "почем", "сколько градусов", "матч",
        "матча", "матче", "матчи", "матчей", "турнир*", "чемпионат*", "какой счет",
        "кто выиграл", "кто победил", "кто проиграл", "кто забил", "выборы",
        "выборов", "выборах", "релиз*", "дата выхода", "когда выйдет",
        "когда выходит", "когда вышел", "когда вышла", "когда вышло",
        "когда вышли", "вышел ли", "вышла ли", "премьер*",
        "weather", "forecast*", "news", "headline*", "price", "prices", "priced",
        "cost of", "how much is", "how much are", "how much does", "how much do",
        "exchange rate*", "stock price*", "stocks", "share price*", "market cap",
        "score", "scores", "who won", "who wins", "who is winning", "who lost",
        "election*", "release date", "released", "come out", "comes out",
        "came out", "coming out", "premiere*", "box office",
    ],
    # Свежее: в вопросе не к персоне, от FRESH_MIN_WORDS слов («а сейчас
    # что?» — не запрос о мире)
    "fresh": [
        "сегодня", "сейчас", "нынче", "текущ*", "актуальн*", "последн*",
        "свеж*", "недавн*", "вчера", "завтра", "на днях", "в этом году",
        "на этой неделе", "в этом месяце",
        "today", "tonight", "now", "currently", "current", "latest", "recent",
        "recently", "yesterday", "tomorrow", "this year", "this week",
        "this month", "right now", "these days",
    ],
    # Вопрос: вопросительное слово первым (или «?» в тексте, или ask)
    "wh": [
        "кто", "что", "чего", "чему", "чем", "где", "когда", "куда", "откуда",
        "почему", "зачем", "какой", "какая", "какое", "какие", "каких", "каким",
        "какую", "какого", "какому", "каком", "каков", "какова", "каковы",
        "сколько", "чей", "чья", "чье", "чьи", "как",
        "who", "whom", "whose", "what", "whats", "when", "where", "why", "which",
        "how", "hows",
    ],
    # Просьба сказать — тоже вопрос; её «ты»/«знаешь» не делают его вопросом
    # О персоне («ты знаешь, кто выиграл?»)
    "ask": [
        "знаешь", "знаете", "ты знаешь", "вы знаете", "не знаешь", "не знаете",
        "ты не знаешь", "вы не знаете", "знаешь ли ты", "подскажи", "подскажите",
        "подскажи мне", "скажи", "скажите", "скажи мне", "скажите мне", "назови",
        "назовите", "перечисли", "покажи", "покажите", "интересно",
        "мне интересно", "хочу знать", "хочу узнать",
        "tell me", "do you know", "you know", "can you tell me",
        "could you tell me", "do you happen to know", "show me", "give me",
        "i wonder", "i want to know", "i d like to know",
    ],
    # Вопрос к самой персоне — не к интернету
    "you": [
        "ты", "тебя", "тебе", "тобой", "тобою", "твой", "твоя", "твое", "твои",
        "твоего", "твоей", "твоих", "твоим", "твоими", "твою", "твоему",
        "вы", "вас", "вам", "вами", "ваш", "ваша", "ваше", "ваши", "вашего",
        "вашей", "ваших", "вашим", "вашими", "вашу", "вашему",
        "you", "your", "yours", "yourself", "u", "ur", "ya",
    ],
    # Вопрос о свежем от первого лица («что мне приготовить сегодня?») —
    # о делах самого пользователя, не о мире
    "me": [
        "я", "мне", "меня", "мной", "мой", "моя", "мое", "мои", "моего", "моей",
        "моих", "моим", "мою", "мы", "нам", "нас", "наш", "наша", "наше", "наши",
        "i", "me", "my", "mine", "we", "us", "our",
    ],
    # Глагол 2-го лица («думаешь», «учишься») — тоже к персоне; слово
    # длиннее окончания хотя бы на 2 буквы («тишь» — не глагол)
    "you_suffix": ["ешь", "ишь", "ешься", "ишься"],
    # «Кто такой X» — о внешнем человеке, если X не местоимение
    "who": [
        "кто такой", "кто такая", "кто такие", "кто это такой", "кто это такая",
        "who is", "who was", "who were", "who s",
    ],
    "pronoun": [
        "он", "она", "оно", "они", "это", "этот", "эта", "эти", "тот", "та",
        "he", "she", "it", "they", "this", "that", "him", "her", "them",
    ],
    # Латиница в русском тексте — сущность (бренд, технология), кроме этих
    "latin_stop": [
        "ok", "okay", "kk", "lol", "hi", "hey", "yo", "wow", "omg", "btw", "pls",
        "plz", "thx", "ty", "np", "xd", "ps", "imho", "bro", "gg", "brb", "afk",
        "idk", "irl", "fyi", "asap", "haha", "ahah", "hah", "lmao", "rofl",
        "ofc", "tbh", "yes", "no", "nope", "yep", "yeah", "bye",
    ],
}
# Минимум слов у вопроса о свежем («а сейчас что?» — не запрос о мире);
# короткие запросы темы («новости?») ловит "topic"
FRESH_MIN_WORDS = 4
# Короткая реплика с темой — уже запрос («погода в москве», «курс евро»)
TOPIC_SHORT_WORDS = 5

# Токены сырого текста для поиска сущностей: конец предложения, слово, число
_TOKEN_RE = re.compile(r"[.!?…]+(?=\s|$)|\n|[^\W\d_]+|[0-9]+")
_QUOTED_RE = re.compile(r"«[^»]{2,}»|\"[^\"]{2,}\"|“[^”]{2,}”")
_CYR_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_LATIN_RE = re.compile(r"[a-zA-Z]{2,}")
# Служебные слова — не сущность, даже с заглавной («Что», «Вы», «I»)
_ENTITY_STOP = ({"i"} | _ADDRESS_WORDS | set(_NEED["latin_stop"])
                | {w for key in ("wh", "you", "ask", "pronoun")
                   for p in _NEED[key] for w in p.split()})


def _normalize(text: str) -> str:
    t = (text or "").lower().replace("ё", "е")
    t = _PUNCT_RE.sub(" ", t)
    return _SPACE_RE.sub(" ", t).strip()


def _strip_names(text: str, names: Iterable[str]) -> str:
    # Снимает обращение по имени/триггеру и междометия с краёв фразы.
    name_set = {_normalize(n) for n in names if n and _normalize(n)}
    words = text.split()

    def is_address(i: int) -> int:
        # Длина обращения, начинающегося с words[i] (многословные имена).
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


def _word_hit(word: str, pattern: str) -> bool:
    return word.startswith(pattern[:-1]) if pattern.endswith("*") else word == pattern


def _match_at(words: list, i: int, pattern: str) -> int:
    # Длина совпадения фразы pattern со слов words[i:] (0 — нет)
    parts = pattern.split()
    if i + len(parts) > len(words):
        return 0
    return len(parts) if all(_word_hit(words[i + j], p) for j, p in enumerate(parts)) else 0


def _spans(words: list, patterns: Iterable[str]) -> set:
    # Номера слов, покрытых совпадениями любого из шаблонов
    hit = set()
    for p in patterns:
        for i in range(len(words)):
            n = _match_at(words, i, p)
            if n:
                hit.update(range(i, i + n))
    return hit


def _has_entity(text: str, names: Iterable[str]) -> bool:
    """Сущность в сыром тексте: кавычки, год, латиница в русском тексте,
    слово с заглавной не в начале предложения (обращение по имени, служебные
    слова и местоимения — не сущность)."""
    if _QUOTED_RE.search(text):
        return True
    name_words = {w for n in names for w in _normalize(n).split()}
    latin_ok = bool(_CYR_RE.search(text))
    # Всё капсом — заглавные ничего не значат
    caps_ok = any(c.islower() for c in text)
    start = True
    for m in _TOKEN_RE.finditer(text):
        tok = m.group()
        if tok == "\n" or tok[0] in ".!?…":
            start = True
            continue
        if tok.isdigit():
            if len(tok) == 4 and 1000 <= int(tok) <= 2099:
                return True
            start = False
            continue
        low = tok.lower().replace("ё", "е")
        if low in name_words:
            continue  # обращение прозрачно: следующее слово — всё ещё начало
        first, start = start, False
        if low in _ENTITY_STOP:
            continue
        if latin_ok and _LATIN_RE.fullmatch(tok):
            return True
        if caps_ok and not first and tok[0].isupper():
            return True
    return False


def search_need_reason(text: str, names: Iterable[str] = ()) -> str | None:
    """Почему поиск НУЖЕН ('explicit' / 'topic' / 'fact' / 'fresh') или None —
    внешних данных реплика не требует (белый список, см. docstring модуля).
    names — имя персоны и её trigger_words."""
    core = _strip_names(_normalize(text), names)
    if not core:
        return None
    words = core.split()
    if _spans(words, _NEED["search"]) or any(
            _match_at(words, 0, p) for p in _NEED["search_start"]):
        return "explicit"
    ask = _spans(words, _NEED["ask"])
    question = ("?" in text or "？" in text or bool(ask)
                or any(_match_at(words, 0, p) for p in _NEED["wh"]))
    if _spans(words, _NEED["topic"]) and (question or len(words) <= TOPIC_SHORT_WORDS):
        return "topic"
    if not question:
        return None
    # Вопрос к самой персоне («как тебе…», «что думаешь…») — из её памяти
    rest = [w for i, w in enumerate(words) if i not in ask]
    if any(w in _NEED["you"] or any(len(w) >= len(suf) + 2 and w.endswith(suf)
                                   for suf in _NEED["you_suffix"])
           for w in rest):
        return None
    for p in _NEED["who"]:
        for i in range(len(words)):
            n = _match_at(words, i, p)
            if n and i + n < len(words) and words[i + n] not in _NEED["pronoun"]:
                return "fact"
    if _has_entity(text, names):
        return "fact"
    if (_spans(words, _NEED["fresh"]) and len(words) >= FRESH_MIN_WORDS
            and not any(w in _NEED["me"] for w in rest)):
        return "fresh"
    return None


def search_gate_reason(text: str, names: Iterable[str] = (),
                       every_message: bool = False) -> str | None:
    """Причина НЕ искать или None — искать. Сначала чёрный список
    (search_skip_reason), затем — если не every_message (пункт «Экономии
    нейронки» search_every_message) — белый: без явной просьбы или вопроса
    о внешнем/свежем — 'no_need'."""
    skip = search_skip_reason(text, names)
    if skip or every_message:
        return skip
    return None if search_need_reason(text, names) else "no_need"


def is_self_question(text: str, names: Iterable[str] = ()) -> bool:
    # Вопрос о самой персоне (её дне, делах, самочувствии).
    return search_skip_reason(text, names) == "self"

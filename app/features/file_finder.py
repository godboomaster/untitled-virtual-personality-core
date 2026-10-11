"""«Открой файл X» / «пришли файл про Y»: поиск файла на компьютере ядра.

Ищем по всему устройству, где работает ядро, через системный индекс:
Spotlight (mdfind) на macOS, Windows Search на Windows, locate на Linux.
Без индекса — обход домашней папки с бюджетом времени (только по имени).

Что делаем с найденным, решает канал (вызывающий):
  * веб-интерфейс на самом компьютере ядра — открываем программой по
    умолчанию (``open_file``);
  * приложение на телефоне и Telegram — присылаем файл в чат.

Поиск и выбор — без модели: выбирает код по совпадению слов, при
равенстве — нумерованный список человеку (см. file_access).

Безопасность:
  * системные, служебные и скрытые папки (~/Library, .ssh, AppData, папка
    данных ядра…) не ищутся вовсе — там ключи, cookies, базы переписки;
    облачные диски внутри ~/Library (iCloud, CloudStorage) — ищутся;
  * файлы-секреты (ключи, сертификаты, базы паролей) не открываются и не
    отправляются;
  * открыть по сообщению можно только документ: программы, скрипты,
    установщики и ярлыки «открыть» = запустить (open на .command/.py с
    Python Launcher, .js/.vbs на Windows), их не открываем — только
    присылаем, если просят прислать.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Сколько строк выдачи индекса читаем на один запрос: широкий запрос по
# содержимому даёт тысячи путей, ранжировать их все незачем
MAX_RAW = 400
# Бюджет одного запроса к индексу и обхода папок без индекса
INDEX_TIMEOUT_SEC = 6.0
WALK_BUDGET_SEC = 4.0
WALK_MAX_ENTRIES = 300_000
# Вариантов в списке «какой файл?»
MAX_CHOICES = 5
# Явный лидер: без списка, сразу действие
LEADER_MIN_SCORE = 55.0
LEADER_MARGIN = 12.0
# Ниже — не показываем вовсе (случайное совпадение одной буквосочетанием)
MIN_SCORE = 20.0

_HOME = Path.home()


# ---------------------------------------------------------------- разбор

@dataclass
class FileRequest:
    """Разобранная просьба про файл."""
    verb: str                       # "open" | "send" | "find"
    query: str                      # что ищем — как написал человек (для ответа)
    terms: List[str]                # основы слов для поиска
    mode: str = "name"              # "name" — по названию, "topic" — про что файл
    exts: Optional[Set[str]] = None  # ограничение типа («презентацию», «pdf»)
    strict_ext: bool = False        # тип назван явно (pdf/.docx) — только он
    folder: Optional[str] = None    # «из папки X» — слово в пути
    roots: List[Path] = field(default_factory=list)  # «с рабочего стола»
    recent: bool = False            # «последний», «свежий»
    exact: Optional[str] = None     # имя в кавычках или с расширением
    need_name: bool = False         # «пришли презентацию» — какую? переспросить
    # «методичку в db в LabS7»: подсказки папок (как сказал человек)
    hints: List[str] = field(default_factory=list)
    # Без слова «файл», только с папками: наше, лишь если папки нашлись на
    # диске и в них есть файл с подходящим именем («открой поиск в ютубе» —
    # не наше)
    soft: bool = False
    # Заполняет search: найденные папки → похожесть, ненайденные подсказки
    root_sims: Dict[str, float] = field(default_factory=dict)
    unresolved: List[str] = field(default_factory=list)
    # «открой ~/Documents/LabsS7/km» — путь целиком: файл или папка
    path: Optional[str] = None
    # Язык глагола («открой km_lab1_report.pdf» — русский, хотя латиницы
    # больше); None — глагола нет (голое «km_lab1_report.pdf»)
    lang: Optional[str] = None


_OPEN_VERBS = (r"открой(?:те)?|открыть|откроешь|открывай(?:те)?|покажи(?:те)?|"
               r"показать|найди(?:те)?\s+и\s+открой(?:те)?|запусти(?:те)?")
_SEND_VERBS = (r"пришли(?:те)?|прислать|пришлёшь|пришлешь|присылай(?:те)?|"
               r"скинь(?:те)?|скинуть|скинешь|скидывай(?:те)?|"
               r"отправь(?:те)?|отправить|отправишь|кинь(?:те)?|перешли(?:те)?|"
               r"вышли(?:те)?|дай(?:те)?|найди(?:те)?\s+и\s+"
               r"(?:пришли|скинь|отправь|кинь)(?:те)?")
_FIND_VERBS = r"найди(?:те)?|найти|отыщи(?:те)?|поищи(?:те)?|где"
_EN_OPEN = r"open|show(?:\s+me)?|pull\s+up|find\s+and\s+open|launch"
_EN_SEND = (r"send(?:\s+me)?|share|give\s+me|get\s+me|fetch(?:\s+me)?|"
            r"find\s+and\s+send(?:\s+me)?|text\s+me|drop\s+me")
_EN_FIND = r"find(?:\s+me)?|locate|search\s+for|where(?:['’]s|\s+is)"

# Вежливость/обращение перед глаголом: «Коннор, пожалуйста, открой …»,
# «можешь открыть …», «could you send me …»
_LEAD = (r"^\s*(?:[\w-]{2,20}\s*,\s*)?"
         r"(?:(?:пожалуйста|плиз|please|pls|а|ну|слушай|эй|hey|ok|ок|окей)[\s,]+)*"
         r"(?:(?:можешь|можете|мог\s+бы|могла\s+бы|могли\s+бы|сможешь|"
         r"(?:could|can|would|will)\s+you)[\s,]+)?"
         r"(?:(?:пожалуйста|please)[\s,]+)?")

# Существительное «файл» и его родня: без него просьба — не про файл
# (кроме имени с расширением). Ключ — что вычеркнуть из запроса, значение —
# типы. Слова, по которым ищут («отчёт», «реферат»), сюда не входят: они
# часть имени
_NOUNS: List[Tuple[str, Optional[Set[str]]]] = [
    (r"файл\w*|файлик\w*|file|files", None),
    (r"документ\w*|док|доку|доки|document|documents|doc|docs", None),
    (r"презентаци\w+|презу|презку|презух\w*|слайд\w*|presentation|slides|deck",
     {"pptx", "ppt", "key", "odp", "pdf"}),
    (r"таблиц\w+|табличк\w+|эксел\w*|excel\w*|spreadsheet|sheet",
     {"xlsx", "xls", "numbers", "ods", "csv"}),
    (r"вордовск\w+|ворд\w*|word", {"docx", "doc", "rtf", "odt", "pages"}),
    (r"pdf\w*|пдф\w*|пдфк\w+", {"pdf"}),
    (r"текстовик\w*|текстов\w+\s+файл\w*|txt", {"txt", "md", "rtf"}),
    (r"фото(?:графи\w+)?|фотк\w+|картинк\w+|изображени\w+|скрин\w*|"
     r"скриншот\w*|photo|picture|image|screenshot",
     {"jpg", "jpeg", "png", "heic", "gif", "webp", "bmp", "tif", "tiff"}),
    (r"видеофайл\w*|видео|ролик\w*|video", {"mp4", "mov", "mkv", "avi", "webm", "m4v"}),
    (r"аудиофайл\w*|аудио|запис\w+|песн\w+|трек\w*|audio|song|track|recording",
     {"mp3", "m4a", "wav", "flac", "ogg", "aac", "opus"}),
    (r"книг\w+|book|ebook", {"epub", "fb2", "pdf", "djvu", "mobi"}),
    (r"архив\w*|archive", {"zip", "rar", "7z", "tar", "gz"}),
]
# Только «файл/документ» делают просьбу просьбой о файле сами; фото, видео,
# песня, книга — лишь со словом «файл», расширением или местом («с рабочего
# стола»): «пришли фотку» персоне — просьба к ней, «открой видео про котов» —
# YouTube
_STRONG_NOUNS = 6   # первые N записей _NOUNS — сильные

# Расширения, по которым имя само говорит «это файл» («открой отчёт.docx»).
# Без доменов верхнего уровня: .md, .ai, .zip, .mov, .pl, .sh, .py — сайты
_BARE_EXTS = {
    "pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "odt", "ods", "odp",
    "rtf", "txt", "csv", "epub", "fb2", "djvu", "pages", "numbers", "key",
    "jpg", "jpeg", "png", "heic", "gif", "webp", "mp3", "mp4", "m4a", "wav",
    "flac", "mkv", "rar", "7z", "json", "yaml", "yml", "ipynb", "log",
}
_EXT_TOKEN_RE = re.compile(r"[\w\-]+\.([a-z0-9]{2,5})\b", re.IGNORECASE)

# Места: фраза → корень поиска
_PLACES: List[Tuple[str, Tuple[str, ...]]] = [
    (r"(?:с|со|на|из)\s+рабоч\w+\s+стол\w*|(?:from|on)\s+(?:my\s+|the\s+)?desktop",
     ("Desktop",)),
    (r"(?:из|в)\s+загруз\w*|(?:из|в)\s+скачанн\w+|(?:from|in)\s+(?:my\s+|the\s+)?downloads?",
     ("Downloads",)),
    (r"(?:из|в)\s+документ(?:ов|ах)|(?:from|in)\s+(?:my\s+|the\s+)?documents",
     ("Documents",)),
]
# «с компьютера», «на ноуте» — не место, просто «отсюда»: срезаем
_HERE_RE = re.compile(
    r"\b(?:с|со|на|из|в)\s+(?:мо\w+\s+)?(?:компьютер\w*|компа|компе|комп|ноут\w*|"
    r"пк|диск\w*|маке|мака|mac\w*)\b|\b(?:from|on)\s+(?:my\s+|the\s+)?"
    r"(?:computer|laptop|pc|mac|disk|drive)\b", re.IGNORECASE)
_FOLDER_RE = re.compile(
    r"\b(?:из|в|во)\s+папк\w+\s+(«[^»]+»|\"[^\"]+\"|[\w\-.~/\\:]+)"
    r"|\b(?:from|in)\s+(?:the\s+)?(?:folder|directory)\s+(\"[^\"]+\"|[\w\-.~/\\:]+)"
    r"|\b(?:from|in)\s+(?:the\s+)?(\"[^\"]+\"|[\w\-.]+)\s+(?:folder|directory)",
    re.IGNORECASE)
_RECENT_RE = re.compile(
    r"\b(?:последн\w+|свеж\w+|нов\w+|недавн\w+|сегодняшн\w+|вчерашн\w+|"
    r"latest|last|recent|newest|new)\b", re.IGNORECASE)
# Связки «про что файл» и «как называется»
_TOPIC_RE = re.compile(
    r"^(?:про|о|об|обо|по|насч[её]т|касательно|на\s+тему|с|со|где|"
    r"(?:в\s+)?котор\w+\s+(?:есть|говорится|написано|упоминается)|"
    r"about|on|regarding|re|with|containing|that\s+mentions|mentioning)\s+",
    re.IGNORECASE)
_NAME_RE = re.compile(
    r"^(?:под\s+названием|с\s+названием|с\s+именем|(?:котор\w+\s+)?называ\w+|"
    r"по\s+имени|named|called|titled|with\s+the\s+name)\s+", re.IGNORECASE)
_QUOTED_RE = re.compile(r"«([^»]+)»|\"([^\"]+)\"|“([^”]+)”|'([^']+)'")

# «… в db в LabS7», «… из LabS7», «… in db»: подсказка папки в конце фразы
_HINT_RE = re.compile(
    r"(?:^|\s)(?:в|во|из|на|in|from|inside|under|within)\s+"
    r"(?:(?:папк\w*|директори\w*|каталог\w*|(?:the\s+)?folder|directory)\s+)?"
    r"(«[^»]+»|\"[^\"]+\"|[^\s,«»\"]+)\s*$", re.IGNORECASE)
# «пришли файл в телеграм / в чат / на телефон» — куда прислать, не папка
_DELIVERY_RE = re.compile(
    r"^(?:телеграм\w*|телеге|тг|telegram|tg|чат\w*|chat|личк\w*|лс|сообщени\w*|"
    r"телефон\w*|phone|почт\w*|e?-?mail|мессенджер\w*|приложени\w*|app)$", re.IGNORECASE)
# «в формате pdf», «на русском» — не папки: подсказки не разбираем дальше
_NOT_HINT_RE = re.compile(
    r"^(?:формат\w*|format|вид\w*|русск\w*|английск\w*|english|russian|целом|"
    r"общем|итоге|конце|начале|случае|смысле|теме|интернет\w*|сети|сеть|"
    r"гугл\w*|google|браузер\w*|browser)$", re.IGNORECASE)

# Документные слова: как файл называется у людей и как — на диске
# («методичка» — «УП_…», «Методические указания»; «лекция» — «lek1…»).
# Короткие варианты (≤ 3 букв) — только целым словом имени
_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "методичк": ("методическ", "пособи", "указани", "практикум", "уп", "му",
                 "manual"),
    "пособи": ("методичк", "учебник", "уп"),
    "учебник": ("пособи", "textbook", "book"),
    "лекци": ("лекц", "lecture", "lek", "конспект"),
    "лекц": ("лекци", "lecture", "lek"),
    "конспект": ("лекци", "notes", "summary"),
    "отчет": ("report",), "report": ("отчет",),
    "задани": ("task", "assignment", "вариант"),
    "лаб": ("lab", "лабораторн", "лр"), "лабораторн": ("lab", "лаб", "лр"),
    "lab": ("лаб", "лр"), "лр": ("lab", "лаб", "лабораторн"),
    # «отчёт по первой работе» — у студента это лабораторная
    "работ": ("lab", "лр"),
    "договор": ("contract", "agreement"), "contract": ("договор",),
    "резюме": ("resume", "cv"), "resume": ("резюме", "cv"),
    "билет": ("вопрос", "question"), "вопрос": ("билет", "question"),
    "счет": ("invoice", "bill"), "invoice": ("счет",),
    "паспорт": ("passport",), "passport": ("паспорт",),
    "диплом": ("thesis", "вкр"), "курсов": ("coursework", "курсач"),
    "расписани": ("schedule", "timetable"), "schedule": ("расписани",),
}

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_STOP = frozenset({
    "по", "о", "об", "обо", "про", "с", "со", "для", "к", "ко", "у", "от",
    "до", "из", "за", "под", "над", "при", "через", "about", "on", "with",
    "in", "from", "at", "by",
    "мне", "мой", "моя", "моё", "мое", "мои", "мою", "моего", "моей", "этот",
    "эту", "это", "этого", "тот", "ту", "того", "который", "которая",
    "которое", "пожалуйста", "плиз", "сюда", "сейчас", "быстро", "срочно",
    "там", "тут", "один", "одна", "одну", "какой", "какой-то", "какую-то",
    "нибудь", "мне", "нам", "его", "её", "ее", "их", "и", "а", "в", "на",
    "the", "a", "an", "my", "this", "that", "me", "please", "here", "now",
    "one", "some", "of", "for", "to", "it", "its", "and", "file", "files",
})

# Слово запроса: «Отчёт_Лабораторная_работа_1» — четыре слова
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
# «Открой сайт с файлами», «пришли ссылку на файл» — не про файл с компьютера
_NOT_FILE_RE = re.compile(
    r"\b(?:сайт\w*|страниц\w*|вкладк\w*|ссылк\w*|site|website|page|tab|link|url)\b",
    re.IGNORECASE)
# Слова перед «файлом», которые его не отменяют: «этот», «мой», «вордовский»
_MODIFIER_RE = re.compile(
    r"^(?:вордовск\w*|текстов\w*|старый|старую|старого|нужн\w+|важн\w+|"
    r"тот|та|ту|то|сам\w*|один|одну|какой-то|какую-то|the|a|an|my|this|that|"
    r"same|old)$", re.IGNORECASE)

# «отчёт по первой работе», «3-й лабе», «second lab» — номер, как в имени
# «km_lab1_report»
_ORDINALS = (("перв", "1"), ("втор", "2"), ("трет", "3"), ("четверт", "4"),
             ("пят", "5"), ("шест", "6"), ("седьм", "7"), ("восьм", "8"),
             ("девят", "9"), ("десят", "10"))
_ORD_END = (r"(?:ый|ий|ой|ая|яя|ое|ее|ую|юю|ого|его|ому|ему|ом|ем|ые|ие|ых|их|"
            r"ым|им|ыми|ими|ья|ье|ью|ьи|ьей|ьего|ьему|ьим|ьих|ьем)")
_EN_ORDINALS = {"first": "1", "second": "2", "third": "3", "fourth": "4",
                "fifth": "5", "sixth": "6", "seventh": "7", "eighth": "8",
                "ninth": "9", "tenth": "10"}

# Путь целиком: «~/Documents/LabsS7», «/Users/…», «C:\Users\…»; «/help» —
# команда, не путь (у пути от корня — хотя бы две части)
_ABS_PATH_RE = re.compile(r"^(?:~(?=$|[/\\])|[A-Za-z]:[\\/]|/[^/\s]+/)")
# Голое имя файла репликой: «km_lab1_report.pdf»
_BARE_NAME_RE = re.compile(r"^[^\s/\\«»\"]+\.([a-z0-9]{2,5})$", re.IGNORECASE)


# Разговор о файлах и папках компьютера: «файл», «папка», путь, имя с
# расширением (заметка модели, что сама она их не видит)
_FILE_TALK_RE = re.compile(
    r"\b(?:файл\w*|папк\w*|директори\w*|каталог\w*|документ\w*|"
    r"files?|folders?|director(?:y|ies)|documents?)\b"
    r"|(?:^|\s)~[/\\]|(?:^|\s)/(?:Users|home|Volumes)/|\b[A-Za-z]:\\"
    r"|\b[\w\-]+\.(?:pdf|docx?|xlsx?|pptx?|odt|ods|odp|rtf|txt|csv|md|epub|djvu|"
    r"zip|rar|7z|jpe?g|png|heic|mp3|mp4|mov|ipynb)\b",
    re.IGNORECASE)


def looks_like_file_talk(text: str) -> bool:
    return bool(text) and bool(_FILE_TALK_RE.search(text))


def _ordinal(word: str) -> Optional[str]:
    """Порядковое числительное → номер строкой: «первой» → «1», «3-й» → «3»."""
    w = _fold(word)
    m = re.fullmatch(r"(\d{1,3})-?(?:й|я|е|ю|ой|ая|ую|ий|ый|ого|ом|th|st|nd|rd)", w)
    if m:
        return str(int(m.group(1)))
    for base, n in _ORDINALS:
        if re.fullmatch(base + _ORD_END, w):
            return n
    return _EN_ORDINALS.get(w)


def is_path_like(s: str) -> bool:
    """Похоже на путь: «~/…», «/…», «C:\\…» или «km/artifacts/lab1»."""
    s = (s or "").strip()
    return bool(s) and (bool(_ABS_PATH_RE.match(s)) or "/" in s or "\\" in s)


def expand_path(s: str) -> Path:
    s = s.strip().strip("«»\"'“”")
    if s == "~":
        return _HOME
    if s.startswith(("~/", "~\\")):
        return _HOME / s[2:]
    return Path(s)


def _fold(s: str) -> str:
    # NFC: имена файлов на macOS разложены (ё = е + U+0308, й = и + U+0306)
    return unicodedata.normalize("NFC", s).lower().replace("ё", "е")


# Окончания для основы при поиске подстрокой в имени и префиксом в тексте:
# длиннее, чем app.core.word_stem (там «лабораторную» → «лабораторну»)
_ENDINGS = tuple(sorted({
    "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ую", "юю",
    "ая", "яя", "ое", "ее", "ой", "ей", "ый", "ий", "ых", "их", "ым", "им",
    "ом", "ем", "ам", "ям", "ах", "ях", "ов", "ев", "ью", "ия", "ие", "ии",
    "иями", "иях", "ией", "а", "я", "у", "ю", "о", "е", "ы", "и", "ь", "й",
}, key=len, reverse=True))


def stem(word: str) -> str:
    """Основа для поиска: «лабораторную» → «лабораторн», «отчёта» → «отчет»,
    «reports» → «report». Короткие слова (≤ 5 букв) — до 3 букв («лабе» →
    «лаб»), длинные — не короче 4."""
    w = _fold(word)
    if w.isdigit() or not w:
        return w
    floor = 3 if len(w) <= 5 else 4
    if re.match(r"^[a-z]+$", w):
        for e in ("ies", "es", "s"):
            if w.endswith(e) and len(w) - len(e) >= floor:
                return w[:-len(e)] + ("y" if e == "ies" else "")
        return w
    for e in _ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= floor:
            return w[:-len(e)]
    return w


def _match_lead(text: str) -> Optional[Tuple[str, str, str]]:
    """Глагол в начале реплики → (verb, хвост, язык глагола)."""
    lead = re.match(_LEAD, text, re.IGNORECASE)
    rest = text[lead.end():] if lead else text
    for verb, pat, lang in (("open", _OPEN_VERBS, "ru"), ("send", _SEND_VERBS, "ru"),
                            ("find", _FIND_VERBS, "ru"), ("open", _EN_OPEN, "en"),
                            ("send", _EN_SEND, "en"), ("find", _EN_FIND, "en")):
        m = re.match(rf"(?:{pat})\b[\s,]*", rest, re.IGNORECASE)
        if m:
            return verb, rest[m.end():], lang
    return None


def _terms_of(words: Iterable[str]) -> List[str]:
    """Слова запроса → основы для поиска; «первой» → «1»."""
    terms: List[str] = []
    for w in words:
        t = _ordinal(w) or stem(w)
        if t and (len(t) >= 2 or t.isdigit()) and t not in terms:
            terms.append(t)
    return terms


def _bare_request(s: str) -> Optional[FileRequest]:
    """Реплика — только путь или имя файла: «~/Documents/LabsS7/km»,
    «km_lab1_report.pdf» (ответ на «назови имя файла»). Имя — мягко: наше,
    только если такой файл нашёлся, иначе это просто упоминание."""
    x = s.strip().strip("«»\"'“”")
    if _ABS_PATH_RE.match(x):
        return FileRequest(verb="open", query=x, terms=[], path=str(expand_path(x)))
    m = _BARE_NAME_RE.match(x)
    if not m or m.group(1).lower() not in _BARE_EXTS:
        return None
    ext = m.group(1).lower()
    terms = _terms_of(w for w in _WORD_RE.findall(x) if _fold(w) != ext)
    if not terms:
        return None
    return FileRequest(verb="open", query=x, terms=terms[:8], exts={ext},
                       strict_ext=True, exact=x, soft=True)


def parse_file_request(text: str) -> Optional[FileRequest]:
    """«открой файл отчёт по физике» → FileRequest; не про файл — None.

    Нужны глагол в начале реплики и признак файла: «файл»/«документ»/
    «презентация»/«pdf»…, имя с расширением («отчёт.docx») или место
    («с рабочего стола»). Без признака «открой отчёт» — не наше: это может
    быть сайт, приложение или просто разговор."""
    if not text or len(text) > 300 or "\n" in text.strip():
        return None
    s = " ".join(text.strip().split()).rstrip(".!?…")
    lead = _match_lead(s)
    if not lead:
        return _bare_request(s)
    verb, rest, lang = lead
    rest = re.sub(r"^(?:мне|нам|me|us)\b[\s,]*", "", rest, flags=re.IGNORECASE)
    # «открой ~/Documents/LabsS7/km/artifacts/lab1» — путь целиком
    target = rest.strip().strip("«»\"'“”")
    if _ABS_PATH_RE.match(target):
        return FileRequest(verb=verb, query=target, terms=[],
                           path=str(expand_path(target)), lang=lang)
    if _NOT_FILE_RE.search(rest):
        return None

    exts: Optional[Set[str]] = None
    strong = False
    weak = False
    strict = False
    roots: List[Path] = []
    folder = None
    exact = None

    q = _QUOTED_RE.search(rest)
    if q:
        exact = next(g for g in q.groups() if g)
    m_ext = _EXT_TOKEN_RE.search(rest)
    if m_ext and m_ext.group(1).lower() in _BARE_EXTS:
        strong = True
        if not exact:
            exact = m_ext.group(0)
        exts, strict = {m_ext.group(1).lower()}, True

    for pat, roots_names in _PLACES:
        m = re.search(pat, rest, re.IGNORECASE)
        if m:
            roots = [_HOME / r for r in roots_names]
            rest = (rest[:m.start()] + " " + rest[m.end():]).strip()
            weak = True
            break
    m = _FOLDER_RE.search(rest)
    hints: List[str] = []
    if m:
        folder = next(g for g in m.groups() if g).strip("«»\"")
        hints.append(folder)
        rest = (rest[:m.start()] + " " + rest[m.end():]).strip()
        weak = True
    rest = _HERE_RE.sub(" ", rest).strip()
    # «в формате pdf», «in pdf format», «в виде таблицы» — тип, не слова имени
    rest = re.sub(r"\b(?:в|во|in)\s+(?:формат\w*|виде)\b|\bformat\b", " ", rest,
                  flags=re.IGNORECASE).strip()
    # «методичку в db в LabS7»: «в X» в конце — подсказки папок; порядок
    # (снаружи внутрь или наоборот) не важен — цепочку проверяет search
    while len(hints) < 4:
        m = _HINT_RE.search(rest)
        if not m:
            break
        word = next(g for g in m.groups() if g).strip("«»\"")
        if _DELIVERY_RE.match(word):
            rest = rest[:m.start()].rstrip()       # «… в телеграм» — куда, не где
            continue
        if (_NOT_HINT_RE.match(word) or _fold(word) in _STOP or len(word) < 2
                or any(re.fullmatch(rf"(?:{pat})", word, re.IGNORECASE) for pat, _k in _NOUNS)):
            break
        hints.append(word)
        rest = rest[:m.start()].rstrip()
    # «отчёт в ~/Documents/LabsS7/km» — путь сам говорит «это про файлы»
    if any(is_path_like(h) for h in hints):
        strong = True
    recent = bool(_RECENT_RE.search(rest))
    if recent:
        rest = _RECENT_RE.sub(" ", rest)

    # Признак «файл» — первым словом после глагола (через «этот», «мой»,
    # «вордовский») или последним («open my resume file»): «открой файл X»,
    # но не «открой X с файлами про Y»
    def _noun_idx(w: str) -> Optional[int]:
        for i, (pat, _k) in enumerate(_NOUNS):
            if re.fullmatch(rf"(?:{pat})", w, re.IGNORECASE):
                return i
        return None
    sig = [w for w in (x.strip(",.:;«»\"") for x in rest.split())
           if w and (_noun_idx(w) is not None
                     or not (_fold(w) in _STOP or _MODIFIER_RE.match(w)))]
    tail = sig[-1:]
    raw_words = rest.split()
    if len(raw_words) >= 2 and re.match(r"^(?:и|а|или|and|or)$", raw_words[-2], re.IGNORECASE):
        tail = []   # «открой ютуб и файлы» — две команды, не файл
    for w in (sig[:1] + tail) if sig else ():
        i = _noun_idx(w)
        if i is not None:
            if i < _STRONG_NOUNS:
                strong = True
            else:
                weak = True
    # Существительные «файл» и родня — тип; вычёркиваем из запроса
    for i, (pat, kinds) in enumerate(_NOUNS):
        rx = re.compile(rf"\b(?:{pat})\b", re.IGNORECASE)
        if rx.search(rest):
            if kinds and not strict:
                exts = set(kinds) if exts is None else (exts & kinds or exts)
                strict = strict or pat.startswith("pdf")
            rest = rx.sub(" ", rest)
    # Медиа без «файл»/места/расширения — не про файл (см. _STRONG_NOUNS).
    # С подсказками папок — «мягкая» просьба: решит диск (см. soft)
    soft = False
    if not strong and not (weak and (roots or folder)):
        if hints:
            soft = True
        elif not (weak and verb == "find"):
            return None
    # Ведущие «мне», «этот», «the», «my» — до разбора связки «про»/«named»
    words0 = rest.split()
    while words0 and (_fold(words0[0]) in _STOP - {"про", "о", "об", "обо", "по", "с",
                                                     "со", "about", "on", "with"}
                      or _MODIFIER_RE.match(words0[0])):
        words0.pop(0)
    rest = " ".join(words0)

    mode = "name"
    m = _NAME_RE.match(rest)
    if m:
        rest = rest[m.end():]
    else:
        m = _TOPIC_RE.match(rest)
        if m:
            mode = "topic"
            rest = rest[m.end():]
    query_text = (exact or rest).strip(" ,.:;-—«»\"")
    words = [w for w in _WORD_RE.findall(query_text) if _fold(w) not in _STOP]
    if exact and m_ext and exact == m_ext.group(0):
        # «отчёт.docx»: расширение — в exts, не в словах
        words = [w for w in words if _fold(w) != m_ext.group(1).lower()]
    terms = _terms_of(words)
    # Без слов — только «последний/с рабочего стола/из папки X»; «пришли
    # презентацию» без названия — переспросим (need_name), а не отдадим
    # персоне: та «откроет» файл на словах
    need_name = False
    if soft and not terms:
        return None
    if not terms and not (roots or hints or (recent and exts)):
        # «открой файлы/документы» — скорее приложение «Файлы», не файл
        if not strong or (sig and re.fullmatch(
                r"файлы|файлов|files|документы|документов|documents|docs|доки",
                sig[0], re.IGNORECASE)):
            return None
        need_name = True
    return FileRequest(verb=verb, query=query_text, terms=terms[:8], mode=mode,
                       exts=exts, strict_ext=strict, folder=folder, roots=roots,
                       recent=recent, exact=exact, need_name=need_name,
                       hints=hints, soft=soft, lang=lang)


# ---------------------------------------------------------------- фильтры

# Служебные папки — не заходим на любой глубине
_SKIP_ANYWHERE = frozenset({
    "node_modules", "__pycache__", "site-packages", "dist-packages", "venv",
    "$recycle.bin", "system volume information", "appdata", "caches",
})
# Системные папки в корне диска (/System, C:\Windows) и в корне домашней
# папки (~/Library, %USERPROFILE%\AppData) — не заходим; ниже по дереву
# «temp» или «var» — обычные папки пользователя
_SKIP_ROOT = frozenset({
    "system", "library", "private", "usr", "bin", "sbin", "opt", "cores",
    "dev", "proc", "sys", "snap", "var", "etc", "boot", "lost+found", "tmp",
    "windows", "program files", "program files (x86)", "programdata",
    "applications", "recovery",
})
_SKIP_HOME_ROOT = frozenset({"library", "appdata", "applications"})
# Пакеты macOS (папки, которые Finder показывает файлом): внутрь не идём
_BUNDLE_SUFFIXES = (".app", ".bundle", ".framework", ".photoslibrary",
                    ".musiclibrary", ".imovielibrary", ".tvlibrary",
                    ".xcarchive", ".kext", ".plugin", ".pkg", ".mpkg",
                    ".prefpane", ".appex", ".xpc", ".lproj", ".scptd",
                    ".workflow", ".action", ".qlgenerator", ".mdimporter",
                    ".sparsebundle", ".noindex", ".xcodeproj", ".xcworkspace",
                    ".playground", ".band", ".logicx", ".fcpbundle")
# Разрешённые места внутри ~/Library: облачные диски
_LIBRARY_OK = ("library/mobile documents/com~apple~clouddocs",
               "library/cloudstorage")

# Секреты: не открываем и не отправляем
_SECRET_NAMES = re.compile(
    r"^(?:id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|known_hosts|authorized_keys|"
    r"\.?env(?:\..*)?|\.netrc|\.pgpass|credentials(?:\.\w+)?|"
    r"wallet\.dat|login\.keychain(?:-db)?|.*\.keychain(?:-db)?|"
    r"cookies(?:\.sqlite)?|key4\.db|logins\.json|"
    r"identity\.json|devices\.json)$", re.IGNORECASE)
_SECRET_EXTS = {"pem", "p12", "pfx", "kdbx", "kdb", "keychain", "gpg", "asc",
                "ovpn", "jks", "keystore", "ppk", "agilekeychain", "opvault",
                "1pif", "psafe3"}
# «Открыть» = запустить: только присылать
_EXEC_EXTS = {
    "app", "command", "tool", "sh", "bash", "zsh", "csh", "ksh", "fish",
    "py", "pyw", "pyc", "pyz", "rb", "pl", "php", "lua", "tcl", "js", "jse",
    "mjs", "vbs", "vbe", "wsf", "wsh", "ps1", "psm1", "psd1", "bat", "cmd",
    "com", "exe", "msi", "msix", "msp", "appx", "appxbundle", "scr", "pif",
    "lnk", "url", "reg", "hta", "cpl", "jar", "jnlp", "apk", "aab", "pkg",
    "mpkg", "dmg", "iso", "img", "terminal", "scpt", "applescript",
    "workflow", "action", "webloc", "inetloc", "fileloc", "desktop", "run",
    "bin", "out", "appimage", "deb", "rpm", "mobileconfig", "prefpane",
    "saver", "kext", "plugin", "xpc", "service", "osax", "dylib", "so",
    "dll", "sys", "drv", "gadget", "msc", "inf", "settingcontent-ms",
    "library-ms", "searchconnector-ms", "application", "xbap", "xll",
    "website", "chm", "diagcab",
}


def _ext(path: str) -> str:
    name = os.path.basename(path)
    return name.rsplit(".", 1)[-1].lower() if "." in name.lstrip(".") else ""


def _data_dir_prefix() -> Optional[str]:
    try:
        from app.core.paths import data_dir
        return _fold(str(Path(data_dir()).resolve())) + os.sep
    except Exception:
        return None


def path_allowed(path: str) -> bool:
    """Можно ли этот путь показывать, открывать или отправлять: не в
    системной/служебной/скрытой папке, не секрет, не данные ядра."""
    try:
        p = os.path.realpath(path)
    except Exception:
        return False
    if not _dir_parts_ok(p):
        return False
    name = os.path.basename(p)
    if _SECRET_NAMES.match(name) or _ext(name) in _SECRET_EXTS:
        return False
    if os.path.isdir(p) or not os.path.isfile(p):
        return False
    return True


def _home_real() -> str:
    try:
        return _fold(os.path.realpath(str(_HOME))) + os.sep
    except Exception:
        return _fold(str(_HOME)) + os.sep


def _dir_parts_ok(p: str) -> bool:
    """Путь (уже realpath) не идёт через запрещённые папки и не в данных ядра."""
    low = _fold(p)
    home = _home_real()
    data = _data_dir_prefix()
    if data and (low + os.sep).startswith(data):
        return False
    in_home = low.startswith(home)
    rel = low[len(home):] if in_home else low
    rel = rel.replace("\\", "/")
    # Облачные диски внутри ~/Library: iCloud Drive, Google Drive, Dropbox
    if in_home and any(rel.startswith(x + "/") for x in _LIBRARY_OK):
        rel = rel.split("/", 2)[-1]
    parts = [x for x in re.split(r"/+", rel) if x]
    if not parts:
        return False
    # Windows: «c:» — диск, корень — следующая часть
    if not in_home and re.fullmatch(r"[a-z]:", parts[0]):
        parts = parts[1:]
    if not in_home and parts and parts[0] == "users" and len(parts) > 2:
        # Чужая или своя домашняя папка не по realpath: корень — после имени
        parts = parts[2:]
        if parts and parts[0] in _SKIP_HOME_ROOT:
            return False
    name = parts[-1] if parts else ""
    for i, part in enumerate(parts[:-1]):
        if part.startswith(".") or part.startswith("~$"):
            return False
        if part.endswith(_BUNDLE_SUFFIXES) or part in _SKIP_ANYWHERE:
            return False
        if i == 0 and (part in _SKIP_HOME_ROOT if in_home else part in _SKIP_ROOT):
            return False
    if not name or name.startswith(".") or name.startswith("~$"):
        return False
    return True


def openable(path: str) -> bool:
    """Открыть программой по умолчанию — не запуск: не программа, не
    скрипт, не установщик, не ярлык и не файл без расширения с правом
    исполнения (open на нём запускает Терминал)."""
    ext = _ext(path)
    if ext in _EXEC_EXTS:
        return False
    if not ext and sys.platform != "win32":
        try:
            if os.access(path, os.X_OK):
                return False
        except Exception:
            return False
    return True


# ---------------------------------------------------------------- поиск

def _read_lines(cmd: List[str], env=None, stdin_text: Optional[str] = None,
                sep: str = "\n", timeout: float = INDEX_TIMEOUT_SEC,
                limit: int = MAX_RAW) -> List[str]:
    """Первые limit строк вывода команды за timeout; дальше процесс гасим —
    широкий запрос к индексу не должен держать ход."""
    out: List[str] = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            env=env, **_no_window())
    except (OSError, ValueError) as e:
        logger.debug(f"[FileFinder] {cmd[0]} не запустился: {e}")
        return out
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        if stdin_text is not None:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass
        buf = b""
        bsep = sep.encode()
        while len(out) < limit:
            chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") \
                else proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(bsep)
            out.extend(x.decode("utf-8", "replace").strip("\r") for x in lines if x)
        if buf and len(out) < limit:
            out.append(buf.decode("utf-8", "replace").strip("\r\n"))
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=1)
        except Exception:
            pass
    return [x for x in out[:limit] if x]


def _no_window() -> dict:
    # Windows: без мелькающего окна консоли
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _safe_term(t: str) -> str:
    # Основы — \w, но страхуемся: кавычки/звёздочки/обратный слеш ломают запрос
    return re.sub(r"[\"'*\\%_\[\]]", "", t)


def _mdfind_queries(req: FileRequest) -> List[Tuple[str, str]]:
    """Запросы Spotlight → [(вид, запрос)]: по имени (все основы подстрокой,
    c — без регистра, d — ё=е) и по содержимому (основы префиксом слова)."""
    terms = [_safe_term(t) for t in req.terms if _safe_term(t)]
    not_dir = '(kMDItemContentType != "public.folder")'
    qs = []
    if terms:
        def _alts(t):
            alts = [t] + [a for a in _SYNONYMS.get(t, ()) if len(a) >= 4]
            return "(" + " || ".join(f'(kMDItemFSName == "*{_safe_term(a)}*"cd)'
                                     for a in alts if _safe_term(a)) + ")"
        name = " && ".join(_alts(t) for t in terms)
        qs.append(("name", f"{name} && {not_dir}"))
        words = [t for t in terms if not t.isdigit()]
        if words:
            text = " && ".join(f'(kMDItemTextContent == "{t}*"cdw)' for t in words)
            qs.append(("content", f"{text} && {not_dir}"))
        if len(terms) > 1:
            # Ни один файл не содержит всех слов в имени — хотя бы часть
            anyname = " || ".join(f'(kMDItemFSName == "*{t}*"cd)'
                                  for t in terms if len(t) >= 4 or t.isdigit())
            if anyname:
                qs.append(("name_any", f"({anyname}) && {not_dir}"))
    return qs


def _search_mdfind(req: FileRequest) -> Dict[str, Set[str]]:
    found: Dict[str, Set[str]] = {}
    only = []
    for r in req.roots:
        only += ["-onlyin", str(r)]
    for kind, q in _mdfind_queries(req):
        if kind == "name_any" and any(found.values()):
            break
        for p in _read_lines(["mdfind", *only, "-0", q], sep="\0"):
            found.setdefault(p, set()).add(kind)
    return found


def _ps_sql(req: FileRequest) -> List[Tuple[str, str]]:
    """Запросы к индексу Windows Search (SystemIndex)."""
    terms = [_safe_term(t) for t in req.terms if _safe_term(t)]
    if not terms:
        return []
    scope = "SCOPE='file:'"
    if req.roots:
        scope = "(" + " OR ".join(
            "SCOPE='file:{}'".format(str(r).replace("'", "''")) for r in req.roots) + ")"
    base = (f"SELECT TOP {MAX_RAW} System.ItemPathDisplay FROM SystemIndex "
            f"WHERE {scope} AND System.ItemType <> 'Directory'")
    qs = [("name", base + "".join(f" AND System.FileName LIKE '%{t}%'" for t in terms))]
    words = [t for t in terms if not t.isdigit()]
    if words:
        cond = " AND ".join(f'"{t}*"' for t in words)
        qs.append(("content", base + f" AND CONTAINS(System.Search.Contents, '{cond}')"))
    if len(terms) > 1:
        anyname = " OR ".join(f"System.FileName LIKE '%{t}%'" for t in terms)
        qs.append(("name_any", base + f" AND ({anyname})"))
    return qs


_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$c = New-Object -ComObject ADODB.Connection
$c.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
$rs = $c.Execute($env:VPC_FIND_SQL)
while (-not $rs.EOF) { [Console]::Out.WriteLine($rs.Fields.Item(0).Value); $rs.MoveNext() }
$rs.Close(); $c.Close()
"""


def _search_windows(req: FileRequest) -> Dict[str, Set[str]]:
    found: Dict[str, Set[str]] = {}
    for kind, sql in _ps_sql(req):
        if kind == "name_any" and any(found.values()):
            break
        env = dict(os.environ, VPC_FIND_SQL=sql)
        for p in _read_lines(["powershell", "-NoProfile", "-NonInteractive",
                              "-ExecutionPolicy", "Bypass", "-Command", "-"],
                             env=env, stdin_text=_PS_SCRIPT):
            found.setdefault(p, set()).add(kind)
    return found


def _search_locate(req: FileRequest) -> Dict[str, Set[str]]:
    exe = shutil.which("plocate") or shutil.which("locate")
    found: Dict[str, Set[str]] = {}
    if not exe or not req.terms:
        return found
    pats = [f"*{_safe_term(t)}*" for t in req.terms]
    for p in _read_lines([exe, "-i", "-A", "-l", str(MAX_RAW), "--basename", *pats]):
        found.setdefault(p, set()).add("name")
    return found


def _skip_dir(name: str, at_home_root: bool = False) -> bool:
    # Обход без индекса: те же правила, что у path_allowed
    low = _fold(name)
    return (low.startswith(".") or low.startswith("~$") or low in _SKIP_ANYWHERE
            or low.endswith(_BUNDLE_SUFFIXES)
            or (at_home_root and low in _SKIP_HOME_ROOT))


def _walk_roots(req: FileRequest) -> List[Path]:
    if req.roots:
        return [r for r in req.roots if r.is_dir()]
    roots = [_HOME]
    for cloud in ("Library/Mobile Documents/com~apple~CloudDocs", "Library/CloudStorage"):
        p = _HOME / cloud
        if p.is_dir():
            roots.append(p)
    return roots


def _search_walk(req: FileRequest) -> Dict[str, Set[str]]:
    """Без индекса: обход домашней папки (и облачных дисков) по имени, с
    бюджетом времени и числа записей. Содержимое не читаем — долго."""
    found: Dict[str, Set[str]] = {}
    deadline = time.monotonic() + WALK_BUDGET_SEC
    seen = 0
    # Внутри папок из подсказок место уже узкое: берём файл и по части слов
    # («отчёт по лр 1» — в «km_lab1_report» нет «лр»), отсеет ранжирование
    scoped = bool(req.root_sims)
    limit = MAX_RAW * 5 if scoped else MAX_RAW
    for root in _walk_roots(req):
        for dirpath, dirnames, filenames in os.walk(root):
            top = os.path.normpath(dirpath) == os.path.normpath(str(_HOME))
            dirnames[:] = [d for d in dirnames if not _skip_dir(d, top)]
            for fn in filenames:
                seen += 1
                low = _fold(fn)
                toks = _name_tokens_fold(fn)
                if not req.terms:
                    ok = True
                elif scoped:
                    ok = any(_term_score(t, low, toks) for t in req.terms)
                else:
                    ok = all(_term_score(t, low, toks) for t in req.terms)
                if ok:
                    found.setdefault(os.path.join(dirpath, fn), set()).add("name")
            if (time.monotonic() > deadline or seen > WALK_MAX_ENTRIES
                    or len(found) >= limit):
                logger.info(f"[FileFinder] обход остановлен на {seen} записях")
                return found
    return found


# ---------------------------------------------------------------- папки-подсказки

FOLDER_MIN_SIM = 0.8      # ниже — папка «не та»
FOLDER_KEEP_GAP = 0.05    # корни хуже лучшего больше чем на это — отбрасываем
SCOPED_MAX_DEPTH = 6      # «db» ищем внутри «LabsS7» не глубже

# Папки сборки: «в km» — не «build/…/values-km» из проекта Android
_DEV_DIRS = frozenset({
    "build", "intermediates", "dist", "target", "deriveddata", "pods", "obj",
    "generated", "cmake-build-debug", "cmake-build-release",
})


def _in_dev_tree(path: Path) -> bool:
    # Папка лежит внутри папки сборки (сама может так называться: «в build»)
    return any(_fold(x) in _DEV_DIRS for x in path.parts[:-1])


def _latin(s: str) -> str:
    return "".join(_TRANSLIT.get(c, c) for c in _fold(s))


def _norm_name(s: str) -> str:
    # «LabS7» → «labs7», «дб» → «db», «Учёба» → «ucheba»
    return re.sub(r"[^a-z0-9]", "", _latin(s))


def _name_tokens_fold(name: str) -> Set[str]:
    base = name.rsplit(".", 1)[0] if "." in name.lstrip(".") else name
    return {t for t in re.split(r"[^\w]|_", _fold(base)) if t}


def _numbers(toks: Iterable[str]) -> Set[int]:
    """Номера в словах имени: «km_lab1_report» → {1}, «Работа 2 КМ» → {2}."""
    return {int(x) for tok in toks for x in re.findall(r"\d+", tok) if len(x) <= 6}


def _term_score(t: str, low: str, toks: Set[str]) -> float:
    """Слово запроса в имени файла: само (1.0) или документный синоним
    (0.85: «методичка» — «УП_…»); короткий синоним — только целым словом.
    Номер — только целым числом: «1» есть в «lab1», но не в «lab12» и «2021»."""
    if t.isdigit():
        return 1.0 if int(t) in _numbers(toks) else 0.0
    if t in low:
        return 1.0
    for a in _SYNONYMS.get(t, ()):
        if len(a) > 3:
            if a in low:
                return 0.85
        # Короткий — целым словом или с номером: «уп», «lek1», «lab2»
        elif any(x == a or (x.startswith(a) and x[len(a):].isdigit()) for x in toks):
            return 0.85
    return 0.0


def folder_sim(hint: str, name: str) -> float:
    """Похожесть подсказки на имя папки, 0..1: «LabS7» ≈ «LabsS7» (0.88),
    «LabsS7_1» — дальше (0.81); «дб» = «db»; «учёбе» ≈ «Учёба»."""
    n = _norm_name(name)
    if not n:
        return 0.0
    toks = {_norm_name(t) for t in re.split(r"[^\w]|_", _fold(name)) if t}
    best = 0.0
    for h in {_norm_name(hint), _norm_name(stem(hint))}:
        if not h:
            continue
        if h == n:
            return 1.0
        if h in toks:
            best = max(best, 0.95)
        elif len(h) >= 3 and n.startswith(h):
            best = max(best, 0.82 + 0.15 * len(h) / len(n))
        elif len(h) >= 4 and h in n:
            best = max(best, 0.78 + 0.15 * len(h) / len(n))
        if len(h) >= 4:
            r = SequenceMatcher(None, h, n).ratio()
            if r >= FOLDER_MIN_SIM:
                best = max(best, r * 0.97)
    return best


def _hint_patterns(hint: str) -> List[str]:
    """Подстроки для запроса папок к Spotlight: каждое слово подсказки
    («LabsS7_1» → «labss7», «1») и «начало+конец» длинного слова (опечатка в
    середине — «LabS7» при «LabsS7»), по-русски и транслитом."""
    out = []
    for form in {_fold(stem(hint)), _latin(stem(hint))}:
        toks = [re.sub(r'["*\\]', "", x) for x in re.split(r"[^\w]|_", form)]
        toks = [x for x in toks if len(x) >= 2 and not x.isdigit()] or \
            [x for x in toks if x]
        for f in toks[:3]:
            out.append(f'(kMDItemFSName == "*{f}*"cd)')
            if len(f) >= 5:
                out.append(f'((kMDItemFSName == "*{f[:3]}*"cd) && (kMDItemFSName == "*{f[-2:]}*"cd))')
    return list(dict.fromkeys(out))


def _global_dirs(hint: str) -> List[Tuple[Path, float]]:
    """Папки по всему устройству, похожие на подсказку."""
    cands: List[str] = []
    backend = _backend()
    if backend == "mdfind":
        pats = _hint_patterns(hint)
        if pats:
            q = f'({" || ".join(pats)}) && (kMDItemContentType == "public.folder")'
            cands = _read_lines(["mdfind", "-0", q], sep="\0")
    elif backend == "windows":
        f = _safe_term(_fold(stem(hint)))
        if f:
            likes = " OR ".join(f"System.FileName LIKE '%{x}%'" for x in {f, f[:3]} if x)
            sql = (f"SELECT TOP {MAX_RAW} System.ItemPathDisplay FROM SystemIndex WHERE "
                   f"SCOPE='file:' AND System.ItemType = 'Directory' AND ({likes})")
            env = dict(os.environ, VPC_FIND_SQL=sql)
            cands = _read_lines(["powershell", "-NoProfile", "-NonInteractive",
                                 "-ExecutionPolicy", "Bypass", "-Command", "-"],
                                env=env, stdin_text=_PS_SCRIPT)
    if not cands:
        # Индекс есть и ответил пусто — папки нет; обход — только без индекса
        if backend in ("mdfind", "windows"):
            return []
        return _scoped_dirs([p for p in _walk_roots(FileRequest("find", "", []))], hint,
                            max_depth=12)
    out = []
    for c in cands:
        sim = folder_sim(hint, os.path.basename(c.rstrip("/\\")))
        if sim >= FOLDER_MIN_SIM and not _in_dev_tree(Path(c)) and path_allowed_dir(c):
            out.append((Path(c), sim))
    return out


def _scoped_dirs(roots: Iterable[Path], hint: str,
                 max_depth: int = SCOPED_MAX_DEPTH) -> List[Tuple[Path, float]]:
    """Папки внутри roots, похожие на подсказку (обход с бюджетом)."""
    out: List[Tuple[Path, float]] = []
    deadline = time.monotonic() + WALK_BUDGET_SEC
    for root in roots:
        base_depth = str(root).rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, _f in os.walk(root):
            top = os.path.normpath(dirpath) == os.path.normpath(str(_HOME))
            dirnames[:] = [d for d in dirnames if not _skip_dir(d, top)]
            if dirpath.count(os.sep) - base_depth >= max_depth:
                dirnames[:] = []
            for d in dirnames:
                sim = folder_sim(hint, d)
                if sim >= FOLDER_MIN_SIM and not _in_dev_tree(Path(dirpath) / d):
                    out.append((Path(dirpath) / d, sim))
            # В папки сборки не спускаемся (сама «build» — кандидат выше)
            dirnames[:] = [d for d in dirnames if _fold(d) not in _DEV_DIRS]
            if time.monotonic() > deadline:
                break
    return out


def _ancestor_sim(path: Path, hint: str) -> float:
    # Подсказка — одна из папок над найденной («в LabS7» для …/LabsS7/db)
    home = os.path.realpath(str(_HOME))
    parts = Path(os.path.relpath(str(path), home)).parts if str(path).startswith(home) \
        else path.parts
    return max((folder_sim(hint, x) for x in parts), default=0.0)


def resolve_hints(hints: List[str], base: List[Path]
                  ) -> Tuple[List[Tuple[Path, float]], List[str]]:
    """Подсказки папок → (корни поиска с похожестью, ненайденные подсказки).

    Самая длинная подсказка ищется первой (по всему устройству или внутри
    мест «с рабочего стола»), остальные — среди папок над найденной или
    внутри неё: «в db в LabS7» и «в LabS7 в db» дают одно и то же.
    Путь от корня («в ~/Documents/LabsS7») — сама папка, без поиска;
    относительный («в km/artifacts/lab1») — цепочка из его частей."""
    flat: List[str] = []
    cur: Optional[List[Tuple[Path, float]]] = None
    unresolved: List[str] = []
    for h in hints:
        if _ABS_PATH_RE.match(h.strip()):
            p = expand_path(h)
            if p.is_dir() and path_allowed_dir(str(p)):
                cur = (cur or []) + [(p, 1.0)]
            else:
                unresolved.append(h)
        elif is_path_like(h):
            flat += [x for x in re.split(r"[/\\]+", h) if x and x != "."]
        else:
            flat.append(h)
    if unresolved:
        return [], unresolved      # явного пути нет — дальше не ищем
    order = sorted(dict.fromkeys(flat), key=lambda h: -len(_norm_name(h)))
    for h in order:
        if cur is None:
            cands = _scoped_dirs(base, h) if base else _global_dirs(h)
            if not cands:
                unresolved.append(h)
                continue
            cur = cands
        else:
            nxt: List[Tuple[Path, float]] = []
            for p, sc in cur:
                anc = _ancestor_sim(p, h)
                if anc >= FOLDER_MIN_SIM:
                    nxt.append((p, min(sc, anc)))
                    continue
                nxt += [(d, min(sc, s)) for d, s in _scoped_dirs([p], h)]
            if not nxt:
                unresolved.append(h)
                continue
            cur = nxt
    if not cur:
        return [], unresolved
    # Цепочка — по самому слабому звену: «LabS7» → LabsS7 (0.88) лучше,
    # чем LabsS7_1 (0.81), даже если «db» в обеих совпала точно
    best = max(sc for _p, sc in cur)
    keep: List[Tuple[Path, float]] = []
    for p, sc in sorted(cur, key=lambda x: (-x[1], len(x[0].parts))):
        if sc < best - FOLDER_KEEP_GAP:
            continue
        # Внутри уже взятого корня — он и так покрыт
        if any(str(p).startswith(str(k) + os.sep) or p == k for k, _ in keep):
            continue
        keep.append((p, sc))
        if len(keep) >= 5:
            break
    return keep, unresolved


def path_allowed_dir(path: str) -> bool:
    # Папка, в которой можно искать: те же запреты, что у файлов в ней
    probe = os.path.join(path, "probe.txt")
    try:
        return os.path.isdir(path) and _dir_parts_ok(probe)
    except Exception:
        return False


def _list_recent(req: FileRequest) -> Dict[str, Set[str]]:
    """«Последний файл из загрузок», «файл из папки X»: верхний уровень
    мест по дате."""
    found: Dict[str, Set[str]] = {}
    for root in _walk_roots(req)[:5]:
        try:
            with os.scandir(root) as it:
                for e in it:
                    if e.is_file() and not e.name.startswith("."):
                        found.setdefault(e.path, set()).add("recent")
        except OSError:
            continue
    return found


def _backend() -> str:
    if sys.platform == "darwin" and shutil.which("mdfind"):
        return "mdfind"
    if sys.platform == "win32":
        return "windows"
    if shutil.which("plocate") or shutil.which("locate"):
        return "locate"
    return "walk"


def _raw_search(req: FileRequest) -> Dict[str, Set[str]]:
    if not req.terms:
        return _list_recent(req)
    backend = _backend()
    found: Dict[str, Set[str]] = {}
    if req.root_sims:
        # Папки уже найдены по подсказкам: внутри — обход по имени (с
        # синонимами и опечатками индекса не ждём) плюс индекс по содержимому
        found = _search_walk(req)
        if backend == "mdfind":
            try:
                for p, kinds in _search_mdfind(req).items():
                    found.setdefault(p, set()).update(kinds)
            except Exception as e:
                logger.warning(f"[FileFinder] индекс mdfind не ответил: {e}")
        return found
    try:
        if backend == "mdfind":
            found = _search_mdfind(req)
        elif backend == "windows":
            found = _search_windows(req)
        elif backend == "locate":
            found = _search_locate(req)
    except Exception as e:
        logger.warning(f"[FileFinder] индекс {backend} не ответил: {e}")
    # Индекс пуст (выключен, домашняя папка исключена) — обход по имени
    if not found and backend != "walk":
        found = _search_walk(req)
    elif backend == "walk":
        found = _search_walk(req)
    return found


# ---------------------------------------------------------------- ранжирование

@dataclass
class FileHit:
    path: str
    name: str
    folder: str          # для показа: ~/Documents/учёба
    size: int
    mtime: float
    score: float
    openable: bool
    name_frac: float = 0.0   # доля слов запроса в имени (с синонимами)


def display_folder(path: str) -> str:
    d = unicodedata.normalize("NFC", os.path.dirname(path))
    home = unicodedata.normalize("NFC", str(_HOME))
    if d == home or d.startswith(home + os.sep):
        d = "~" + d[len(home):]
    cloud = "~/Library/Mobile Documents/com~apple~CloudDocs"
    if d.startswith(cloud):
        d = "iCloud Drive" + d[len(cloud):]
    return d


_COPY_RE = re.compile(r"\s*(?:\(\d+\)|copy(?:\s+\d+)?|копия(?:\s+\d+)?|-\s*копия)$",
                      re.IGNORECASE)
_FAVORED = ("Documents", "Desktop", "Downloads",
            "Library/Mobile Documents/com~apple~CloudDocs", "Library/CloudStorage")


def _name_key(name: str) -> str:
    base, dot, ext = name.rpartition(".")
    base = base if dot else name
    return _fold(_COPY_RE.sub("", base)) + "." + (_fold(ext) if dot else "")


def _dir_numbers(req: FileRequest, path: str) -> Set[int]:
    """Номера в папках файла ниже папки из подсказок («km/src/lab1/reports»
    при «в km» → {1}); без подсказок — в его папке. Выше подсказки не
    смотрим: «S7» в «LabsS7» — не номер работы."""
    parent = os.path.dirname(path)
    under = [r for r in req.root_sims
             if parent == r or parent.startswith(r.rstrip(os.sep) + os.sep)]
    if under:
        rel = parent[len(max(under, key=len)):]
        dirs = [x for x in re.split(r"[/\\]+", rel) if x]
    else:
        dirs = [os.path.basename(parent)]
    return _numbers(t for d in dirs for t in re.split(r"[^\w]|_", _fold(d)) if t)


def _match_frac(req: FileRequest, path: str) -> float:
    """Доля слов запроса в имени файла (с синонимами); номер засчитываем и
    по папке: «отчёт по лр 1» — «…/lab1/report.pdf»."""
    if not req.terms:
        return 0.0
    name = os.path.basename(path)
    low, toks = _fold(name), _name_tokens_fold(name)
    dir_nums: Optional[Set[int]] = None
    total = 0.0
    for t in req.terms:
        s = _term_score(t, low, toks)
        if not s and t.isdigit():
            if dir_nums is None:
                dir_nums = _dir_numbers(req, path)
            s = 0.85 if int(t) in dir_nums else 0.0
        total += s
    return total / len(req.terms)


def _num_conflict(req: FileRequest, path: str) -> bool:
    """Номер назван, а у файла (или его папки) другой: «лр 1» — не «lab2».
    Большие числа (годы, коды) номером работы не считаем."""
    nums = {int(t) for t in req.terms if t.isdigit()}
    if not nums:
        return False
    toks = _name_tokens_fold(os.path.basename(path))
    have = {n for n in _numbers(toks) | _dir_numbers(req, path) if n < 100}
    return bool(have) and not have & nums


def _score(req: FileRequest, path: str, kinds: Set[str], st) -> float:
    name = os.path.basename(path)
    low = _fold(name)
    base_low = low.rsplit(".", 1)[0] if "." in low else low
    terms = req.terms
    score = 0.0
    toks = _name_tokens_fold(name)
    if terms:
        frac = _match_frac(req, path)
        if _num_conflict(req, path):
            score -= 20
        content = "content" in kinds
        if req.mode == "topic":
            score += 45 * frac + (40 if content else 0)
        else:
            score += 65 * frac + (20 if content else 0)
        if req.exact:
            ex = _fold(req.exact)
            if low == ex or base_low == ex.rsplit(".", 1)[0]:
                score += 30
            elif ex in low:
                score += 15
        # Подряд, как в запросе («лабораторная работа 1»)
        if all(t in low for t in terms) and len(terms) > 1:
            joined = re.sub(r"[\W_]+", " ", base_low)
            if re.search(r"\s*".join(map(re.escape, terms)), joined.replace(" ", "")):
                score += 8
    else:
        score += 40                      # «последний файл из загрузок»
    ext = _ext(name)
    if req.exts:
        if ext in req.exts:
            score += 12
        elif req.strict_ext:
            return 0.0
        else:
            score -= 10
    # Внутри папки из подсказок: чем точнее совпала папка, тем выше
    if req.root_sims:
        under = [sim for r, sim in req.root_sims.items()
                 if path == r or path.startswith(r.rstrip(os.sep) + os.sep)]
        if not under:
            return 0.0
        score += 12 * max(under)
    rel = os.path.relpath(path, _HOME) if path.startswith(str(_HOME)) else ""
    if rel and rel.startswith(_FAVORED):
        score += 6
    if rel:
        score -= min(rel.count(os.sep), 8) * 0.5
    age_days = max(0.0, (time.time() - st.st_mtime) / 86400)
    if req.recent:
        score += max(0.0, 30 - age_days)          # свежее — выше, резко
    else:
        score += max(0.0, 6 - age_days / 30)      # слабый бонус за свежесть
    return score


def _path_hits(req: FileRequest, for_send: bool) -> List[FileHit]:
    """«открой ~/…/report.pdf» — сам файл; «открой ~/…/lab1» — файлы папки
    (только её верхний уровень, свежие первыми)."""
    p = req.path or ""
    if os.path.isfile(p):
        files, single = [p], True
    elif os.path.isdir(p) and path_allowed_dir(p):
        try:
            with os.scandir(p) as it:
                files = [e.path for e in it if e.is_file()]
        except OSError:
            files = []
        single = False
    else:
        return []
    out: List[FileHit] = []
    for f in files:
        if not path_allowed(f):
            continue
        ok_open = openable(f)
        if not for_send and not ok_open:
            continue
        try:
            st = os.stat(f)
        except OSError:
            continue
        out.append(FileHit(path=f, name=unicodedata.normalize("NFC", os.path.basename(f)),
                           folder=display_folder(f), size=st.st_size, mtime=st.st_mtime,
                           score=100.0 if single else 40.0, openable=ok_open,
                           name_frac=1.0 if single else 0.0))
    out.sort(key=lambda h: -h.mtime)
    return out[:MAX_CHOICES * 4]


def search(req: FileRequest, for_send: bool = False) -> List[FileHit]:
    """Найти и упорядочить кандидатов. for_send — присылать: программы и
    скрипты допустимы (их не запускаем); иначе — только открываемое."""
    if req.path:
        return _path_hits(req, for_send)
    if req.hints and not req.root_sims and not req.unresolved:
        roots, req.unresolved = resolve_hints(req.hints, list(req.roots))
        if roots:
            req.root_sims = {str(p): sim for p, sim in roots}
            req.roots = [p for p, _s in roots]
        if req.unresolved:
            if req.soft:
                return []        # «открой поиск в ютубе» — папки нет, не наше
            # «файл отчёт в 2025» — не папка, так слово запроса; явное «из
            # папки X» и путь — нет (ответит «папки не нашлось»)
            if req.folder in req.unresolved or any(is_path_like(h) for h in req.unresolved):
                return []
            extra = [stem(h) for h in req.unresolved if h != req.folder]
            req.terms = (req.terms + [t for t in extra if t not in req.terms])[:8]
    raw = _raw_search(req)
    hits: Dict[str, FileHit] = {}
    for path, kinds in raw.items():
        path = unicodedata.normalize("NFC", path) if sys.platform != "darwin" else path
        if not path_allowed(path):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        ok_open = openable(path)
        if not for_send and not ok_open:
            continue
        sc = _score(req, path, kinds, st)
        if sc < MIN_SCORE:
            continue
        name = unicodedata.normalize("NFC", os.path.basename(path))
        # Копии «X (1).pdf» (скачан дважды) — один файл: оставляем свежую
        key = _name_key(name)
        prev = hits.get(key)
        if prev and prev.mtime >= st.st_mtime:
            continue
        hits[key] = FileHit(path=path, name=name, folder=display_folder(path),
                            size=st.st_size, mtime=st.st_mtime, score=sc,
                            openable=ok_open, name_frac=_match_frac(req, path))
    ordered = sorted(hits.values(), key=lambda h: (-h.score, -h.mtime))
    # Есть файлы с названным номером — файлы с другим номером не предлагаем
    if any(t.isdigit() for t in req.terms):
        fit = [h for h in ordered if not _num_conflict(req, h.path)]
        if fit:
            ordered = fit
    return ordered[:MAX_CHOICES * 4]


def pick(hits: List[FileHit], req: Optional[FileRequest] = None
         ) -> Tuple[Optional[FileHit], List[FileHit]]:
    """(лидер, варианты): явный лидер — сразу он; иначе до MAX_CHOICES
    вариантов списком; пусто — ничего не нашлось. Без слов («пришли фото
    с рабочего стола») лидер — только единственный файл или «последний»."""
    if not hits:
        return None, []
    top = hits[0]
    if req is not None and not req.terms:
        if len(hits) == 1 or req.recent:
            return top, [top]
        return None, hits[:MAX_CHOICES]
    second = hits[1].score if len(hits) > 1 else 0.0
    if top.score >= LEADER_MIN_SCORE and top.score - second >= LEADER_MARGIN:
        return top, [top]
    return None, hits[:MAX_CHOICES]


# ---------------------------------------------------------------- действие

class FileOpenError(Exception):
    """Открыть не вышло; текст — для пользователя."""


def open_file(path: str) -> None:
    """Открыть программой по умолчанию на компьютере ядра. Повторная
    проверка — путь мог прийти из старого списка, а файл с тех пор
    подменили."""
    if not path_allowed(path) or not openable(path):
        raise FileOpenError("этот файл открывать нельзя")
    if sys.platform == "darwin":
        cmd = ["open", "--", path]
    elif sys.platform == "win32":
        os.startfile(path)  # noqa: S606 — путь из индекса, тип проверен
        return
    else:
        cmd = ["xdg-open", path]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=15, **_no_window())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise FileOpenError(str(e))
    if r.returncode != 0:
        detail = (r.stderr or b"").decode("utf-8", "replace").strip()
        raise FileOpenError(detail[:200] or f"код {r.returncode}")


def open_folder(path: str) -> None:
    """Открыть папку в Finder / Проводнике на компьютере ядра."""
    if not path_allowed_dir(path):
        raise FileOpenError("эту папку открывать нельзя")
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 — папка проверена path_allowed_dir
        return
    cmd = ["open", "--", path] if sys.platform == "darwin" else ["xdg-open", path]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=15, **_no_window())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise FileOpenError(str(e))
    if r.returncode != 0:
        detail = (r.stderr or b"").decode("utf-8", "replace").strip()
        raise FileOpenError(detail[:200] or f"код {r.returncode}")


def human_size(n: int, lang: Optional[str] = None) -> str:
    en = str(lang or "").startswith("en")
    units = ("B", "KB", "MB", "GB") if en else ("Б", "КБ", "МБ", "ГБ")
    x = float(n)
    for u in units:
        if x < 1024 or u == units[-1]:
            s = f"{x:.0f}" if u == units[0] or x >= 10 else f"{x:.1f}"
            return f"{s.replace('.', ',') if not en else s} {u}"
        x /= 1024
    return f"{n} {units[0]}"

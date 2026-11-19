"""Управление экраном телефона (способ 2 команд телефону): «на телефоне
нажми «Отправить»», «на телефоне листай вниз», «телефон, назад», «что на
экране телефона?», «на телефоне введи «привет» в поле поиска», «открой на
телефоне Telegram».

Исполняет служба специальных возможностей на телефоне (PhoneControlService,
web/android): пока человек её включил, телефон — устройство ядра
(app/core/devices) с возможностями screen.*, apps.list, app.open. Что
нажать, решает этот модуль: фразу разбирает код, элемент — по подписи из
снимка экрана, ответ — короткая служебная реплика (как fast-path режима
управления), без нейросети. Только «что на экране» отвечает персона: ей —
заметка с тем, что на экране на самом деле. Агент по цели («напиши Каре в
телеграме…») — следующий этап на тех же командах устройства.

Обращение к телефону — только явное («на телефоне», «телефон,», «on my
phone»): «нажми «Далее»» без него — режиму управления компьютером.

Безопасность — как у режима управления на компьютере:
* кто — тот же гейт, что у режима управления (владелец или allowlist), не
  из скина, не в группе Telegram (экран телефона увидели бы все) — это
  решает BotInstance;
* необратимое по подписи («Отправить», «Удалить», «Подтвердить» —
  risky_label режима управления) — только после «да» автора просьбы
  (PENDING_TTL_SEC), оплата — никогда: её нажимает человек;
* приватные приложения (банки, кошельки, пароли, коды входа, Госуслуги) —
  экран не читается и не нажимается, работают только «назад», «домой»,
  «недавние», «шторка»; коды из уведомлений в заметку персоне не идут;
* поле пароля — ввод только после «да», введённое в ответе не повторяется;
* в лог — вид команды и пакет приложения, не подписи и не введённый текст.
"""

import logging
import re
import threading
import time
from typing import Iterable, Optional

from app.core import devices
from app.features.cc_texts import clip, is_en
from app.features.computer_control import (
    ComputerControlManager, classify_confirmation, parse_choice)

logger = logging.getLogger(__name__)

READ = "screen.read"
TAP = "screen.tap"
TYPE = "screen.type"
SCROLL = "screen.scroll"
GLOBAL = "screen.global"
APPS = "apps.list"
OPEN = "app.open"

READ_TIMEOUT_SEC = 12.0
# Нажатие + ожидание, пока экран успокоится (до 3 с), + свежий снимок
ACT_TIMEOUT_SEC = 15.0
OPEN_TIMEOUT_SEC = 20.0
# «Да» на «нажать «Отправить»?» и номер на «какой из…?» — столько ждём;
# дальше экран уже другой, и «да» — не ответ на этот вопрос
PENDING_TTL_SEC = 120
APPS_TTL_SEC = 600
# Нужного на экране нет — перечитать через столько (переход ещё идёт)
REREAD_DELAY_SEC = 0.8
MAX_CHOICES = 6
# Заметка персоне «что на экране»: столько строк и символов
READ_MAX_LINES = 60
READ_MAX_CHARS = 2500

# ── Тексты ──────────────────────────────────────────────────────────

_T = {
    "not_set_up": {
        "ru": "Управление экраном телефона не включено. В приложении на телефоне: "
              "«Настройки» → «Управление экраном» → «Открыть спец. возможности» — и включите "
              "там «Virtual Persona: управление экраном».",
        "en": "Phone screen control isn't turned on. In the app on the phone: Settings → "
              "Screen control → Open Accessibility, and turn on \"Virtual Persona: screen "
              "control\" there."},
    "offline": {
        "ru": "Телефон «{name}» сейчас не на связи с ядром. Проверьте, что служба «Virtual "
              "Persona: управление экраном» включена, а телефон в той же сети.",
        "en": "The phone \"{name}\" isn't connected to the core right now. Check that "
              "\"Virtual Persona: screen control\" is on and the phone is on the same network."},
    "timeout": {"ru": "Телефон не ответил вовремя — попробуйте ещё раз. Если не выйдет, проверьте, "
                      "что служба «Virtual Persona: управление экраном» включена, а телефон в сети.",
                "en": "The phone didn't answer in time — please try again. If it still fails, check "
                      "that \"Virtual Persona: screen control\" is on and the phone is online."},
    "unsupported": {"ru": "Приложение на телефоне этого ещё не умеет — обновите его.",
                    "en": "The app on the phone can't do that yet — please update it."},
    "failed": {"ru": "На телефоне не получилось: {detail}.",
               "en": "It didn't work on the phone: {detail}."},
    "screen_off": {"ru": "Экран телефона выключен — включите его, и я продолжу.",
                   "en": "The phone's screen is off — turn it on and I'll go on."},
    "locked": {"ru": "Телефон заблокирован — разблокируйте его, и я продолжу.",
               "en": "The phone is locked — unlock it and I'll go on."},
    "no_window": {"ru": "Экран телефона сейчас не читается — попробуйте ещё раз.",
                  "en": "The phone's screen can't be read right now — please try again."},
    "private": {
        "ru": "«{app}» — приложение с деньгами, паролями или кодами входа: его экран я не "
              "читаю и не нажимаю. «Назад» и «домой» — пожалуйста.",
        "en": "\"{app}\" handles money, passwords or sign-in codes: I don't read or tap its "
              "screen. \"Back\" and \"home\" still work."},
    "not_found": {"ru": "На экране телефона ({app}) нет «{target}».",
                  "en": "There's no \"{target}\" on the phone's screen ({app})."},
    "visible": {"ru": " Видно: {items}.", "en": " Visible: {items}."},
    "choose": {"ru": "На экране телефона несколько «{target}» — какое нажать?\n{items}",
               "en": "There are several \"{target}\" on the phone's screen — which one?\n{items}"},
    "choose_field": {"ru": "В какое поле ввести?\n{items}",
                     "en": "Which field should I type into?\n{items}"},
    "choose_app": {"ru": "На телефоне несколько приложений «{target}» — какое открыть?\n{items}",
                   "en": "There are several \"{target}\" apps on the phone — which one?\n{items}"},
    "choice_range": {"ru": "Назовите номер от 1 до {n}.", "en": "Please pick a number from 1 to {n}."},
    "confirm_tap": {"ru": "Нажать «{label}» {where} на телефоне?",
                    "en": "Tap \"{label}\" {where} on the phone?"},
    "confirm_type_pw": {"ru": "Ввести текст в поле пароля «{label}» {where}?",
                        "en": "Type into the password field \"{label}\" {where}?"},
    "payment": {"ru": "«{label}» похоже на оплату — её нажмите сами, я платежи не нажимаю.",
                "en": "\"{label}\" looks like a payment — please tap it yourself, I never tap payments."},
    "declined": {"ru": "Хорошо, не нажимаю.", "en": "Okay, I won't tap it."},
    "declined_type": {"ru": "Хорошо, не ввожу.", "en": "Okay, I won't type it."},
    "disabled": {"ru": "«{label}» сейчас неактивна — приложение не даёт её нажать.",
                 "en": "\"{label}\" is disabled right now — the app won't let it be tapped."},
    "gone": {"ru": "Пока я нажимал, экран телефона сменился — «{label}» больше не видно.",
             "en": "The phone's screen changed meanwhile — \"{label}\" isn't there anymore."},
    "tapped": {"ru": "Нажал «{label}».", "en": "Tapped \"{label}\"."},
    "now_app": {"ru": " Теперь на экране: {app}.", "en": " Now on screen: {app}."},
    "no_field": {"ru": "На экране телефона нет поля ввода.",
                 "en": "There's no text field on the phone's screen."},
    "no_such_field": {"ru": "На экране телефона нет поля «{target}».",
                      "en": "There's no \"{target}\" field on the phone's screen."},
    "typed": {"ru": "Ввёл «{text}» в поле «{label}».", "en": "Typed \"{text}\" into \"{label}\"."},
    "typed_pw": {"ru": "Ввёл пароль в поле «{label}».",
                 "en": "Typed the password into \"{label}\"."},
    "type_failed": {"ru": "Поле «{label}» не приняло текст.",
                    "en": "The field \"{label}\" didn't take the text."},
    "scrolled": {"ru": "Пролистал {dir}.", "en": "Scrolled {dir}."},
    "scroll_end": {"ru": "Пролистал {dir}, но экран не сдвинулся — похоже, дальше некуда.",
                   "en": "Scrolled {dir}, but nothing moved — looks like that's the end."},
    "back": {"ru": "Нажал «Назад».", "en": "Pressed Back."},
    "home": {"ru": "Вышел на главный экран.", "en": "Went to the home screen."},
    "recents": {"ru": "Открыл недавние приложения.", "en": "Opened recent apps."},
    "notifications": {"ru": "Открыл шторку уведомлений.", "en": "Opened the notification shade."},
    "quick_settings": {"ru": "Открыл быстрые настройки.", "en": "Opened quick settings."},
    "opened": {"ru": "Открыл {app} на телефоне.", "en": "Opened {app} on the phone."},
    "no_app": {"ru": "Приложения «{target}» на телефоне не нашёл.",
               "en": "I couldn't find an app called \"{target}\" on the phone."},
}

_DIRS = {
    "down": {"ru": "вниз", "en": "down"}, "up": {"ru": "вверх", "en": "up"},
    "left": {"ru": "влево", "en": "left"}, "right": {"ru": "вправо", "en": "right"},
}
_KIND_WORDS = {
    "button": {"ru": "кнопка", "en": "button"}, "item": {"ru": "строка", "en": "row"},
    "field": {"ru": "поле", "en": "field"}, "switch": {"ru": "переключатель", "en": "switch"},
    "check": {"ru": "флажок", "en": "checkbox"}, "text": {"ru": "надпись", "en": "text"},
    "image": {"ru": "картинка", "en": "image"}, "list": {"ru": "список", "en": "list"},
}
_PLACES = {"top": {"ru": "вверху", "en": "at the top"},
           "middle": {"ru": "посередине", "en": "in the middle"},
           "bottom": {"ru": "внизу", "en": "at the bottom"}}


def text(key: str, lang: Optional[str] = None, **values) -> str:
    row = _T.get(key) or {}
    s = (row.get("en") if is_en(lang) else None) or row.get("ru") or key
    return s.format(**values)


def _word(table: dict, key: str, lang: Optional[str]) -> str:
    row = table.get(key) or {}
    return row.get("en" if is_en(lang) else "ru") or key


# ── Разбор фразы ────────────────────────────────────────────────────

# Явное обращение к телефону. «В телефоне» — тоже («что в телефоне
# открыто?»); без глагола команды это обычная реплика
_MARK_RE = re.compile(
    r"\bна\s+экране\s+(?:мо(?:его|ем|ём)\s+|сво(?:его|ем|ём)\s+|этого\s+)?(?:телефона|смартфона)\b"
    r"|\bэкран(?:е|а)?\s+(?:мо(?:его|ем|ём)\s+|сво(?:его|ем|ём)\s+)?(?:телефона|смартфона)\b"
    r"|\b(?:на|в)\s+(?:мо[её]м\s+|сво[её]м\s+|этом\s+)?(?:телефоне|смартфоне|мобильном)\b"
    r"|^\s*(?:телефон|смартфон)\s*[,:!]"
    r"|\bon\s+(?:my|the|this)\s+phone(?:(?:['’]s)?\s+screen)?\b"
    r"|\b(?:my|the)\s+phone(?:['’]s)?\s+screen\b"
    r"|^\s*phone\s*[,:!]",
    re.IGNORECASE)
_NEG_RE = re.compile(
    r"\bне\s+(?:нажимай|нажми|жми|трогай|тыкай|листай|открывай|запускай|вводи|пиши|"
    r"кликай|выбирай|прокручивай)\w*|\b(?:don['’]?t|do\s+not|never)\s+(?:tap|press|click|"
    r"touch|scroll|open|type|enter|launch)\b", re.IGNORECASE)
# Просьба вопросом — всё равно просьба: «можешь на телефоне нажать назад?»
_POLITE_RE = re.compile(
    r"^(?:(?:а|ну|так|слушай|пожалуйста)[\s,]+)*(?:(?:ты\s+)?(?:можешь|можете|могли\s+бы|"
    r"сможешь)|(?:could|can|would|will)\s+you)\s+", re.IGNORECASE)
_FILLER_RE = re.compile(
    r"^(?:(?:пожалуйста|please|а|ну|так|теперь|now|then|ещё|еще|и|and|давай|сейчас|"
    r"сначала|потом|ok|ок|окей|okay)[\s,]+)+", re.IGNORECASE)
_TAIL_RE = re.compile(r"[\s,]*(?:пожалуйста|please)?[\s.!?…,]*$", re.IGNORECASE)
_QUOTE_RE = re.compile(r"^[«\"“„'‘](.*?)[»\"”“'’]$")

# Инфинитивы — для вежливой формы («можешь нажать …?»)
_TAP_VERBS = (r"нажми(?:те)?|нажать|нажмёшь|нажмешь|кликни(?:те)?|кликнуть|тапни(?:те)?|"
              r"тапнуть|тыкни(?:те)?|ткни(?:те)?|ткнуть|жми(?:те)?|коснись|коснуться|"
              r"выбери(?:те)?|выбрать|tap|press|click|hit|select|choose|touch")
_TAP_RE = re.compile(
    rf"^(?:{_TAP_VERBS})(?:\s+(?:на|по|on))?"
    r"(?:\s+(?:кнопк[уа]|пункт|ссылк[уа]|иконк[уа]|значок|вкладк[уа]|строк[уа]|"
    r"надпись|the|button|item|link|icon|tab))*\s+(?P<t>.+)$", re.IGNORECASE)
_TAP_SUFFIX_RE = re.compile(r"\s+(?:button|link|icon|tab|кнопк[уа]|ссылк[уа]|вкладк[уа])$",
                            re.IGNORECASE)
_BACK_RE = re.compile(
    rf"^(?:(?:{_TAP_VERBS})\s+(?:на\s+|on\s+)?(?:кнопку\s+|the\s+)?(?:назад|back)(?:\s+button)?|"
    r"назад|вернись(?:те)?(?:\s+назад)?|вернуться(?:\s+назад)?|шаг\s+назад|"
    r"go\s+back|back)$", re.IGNORECASE)
_HOME_RE = re.compile(
    rf"^(?:(?:{_TAP_VERBS})\s+(?:на\s+|on\s+)?(?:кнопку\s+|the\s+)?(?:домой|home)(?:\s+button)?|"
    r"домой|"
    r"(?:выйди|вернись|перейди)(?:те)?\s+(?:на\s+)?(?:главный\s+экран|рабочий\s+стол|домой)|"
    r"(?:на\s+|открой\s+|покажи\s+)?(?:главный\s+экран|рабочий\s+стол)|"
    r"(?:go\s+)?home(?:\s+screen)?|go\s+to\s+(?:the\s+)?home\s+screen)$", re.IGNORECASE)
_RECENTS_RE = re.compile(
    r"^(?:(?:открой|покажи)(?:те)?\s+)?(?:недавние|последние|открытые|запущенные)"
    r"(?:\s+приложения)?|(?:open\s+|show\s+)?(?:the\s+|my\s+)?recent(?:s|\s+apps)$",
    re.IGNORECASE)
_NOTIF_RE = re.compile(
    r"^(?:(?:открой|покажи|опусти|раскрой|потяни)(?:те)?\s+)?(?:шторк[уа]|уведомления|"
    r"панель\s+уведомлений)(?:\s+уведомлений)?$|"
    r"^(?:(?:open|show|pull\s+down)\s+)?(?:the\s+|my\s+)?notifications(?:\s+shade)?$",
    re.IGNORECASE)
_QUICK_RE = re.compile(
    r"^(?:(?:открой|покажи)(?:те)?\s+)?быстрые\s+настройки$|^(?:open\s+)?quick\s+settings$",
    re.IGNORECASE)
_SCROLL_RE = re.compile(
    r"^(?:(?:по|про)?(?:листай|листни|листать|листнуть|крути|прокрути|прокрутить|мотай|"
    r"промотай|скролль|скрольни|пролистать)(?:те)?|scroll|swipe)"
    r"(?:\s+(?:экран|страницу|список|ленту|the\s+screen|the\s+page))?"
    r"(?:\s+(?P<d>вниз|вверх|наверх|ниже|выше|дальше|назад|влево|вправо|down|up|left|right))?"
    r"(?:\s+(?:экран|страницу|список|ленту))?$"
    r"|^(?P<b>вниз|вверх|ниже|выше)$", re.IGNORECASE)
_SCROLL_DIRS = {"вниз": "down", "ниже": "down", "дальше": "down", "down": "down",
                "вверх": "up", "наверх": "up", "выше": "up", "назад": "up", "up": "up",
                "влево": "left", "left": "left", "вправо": "right", "right": "right"}
_READ_RE = re.compile(
    r"^(?:что(?:\s+(?:сейчас|там|тут|у\s+меня|теперь))*(?:\s+(?:на\s+экране|открыто|видно|"
    r"происходит|показано|показывает|написано))?|"
    r"(?:прочитай|прочти|опиши|перескажи)(?:те)?(?:\s+мне)?(?:\s+(?:экран|что\s+(?:там|на\s+экране)"
    r"|с\s+экрана|что\s+открыто))?|"
    r"(?:посмотри|глянь|взгляни)(?:те)?,?\s+что\s+(?:там|на\s+экране|открыто)|"
    r"what(?:['’]s|\s+is)(?:\s+(?:on(?:\s+(?:the\s+)?screen)?|open|there|showing))?(?:\s+now)?|"
    r"read(?:\s+me)?(?:\s+(?:the\s+)?screen|\s+what(?:['’]s|\s+is)\s+on\s+(?:it|the\s+screen))?|"
    r"describe(?:\s+the\s+screen)?)$", re.IGNORECASE)
_TYPE_VERBS = (r"введи(?:те)?|ввести|впиши(?:те)?|вписать|вбей(?:те)?|вбить|напечатай(?:те)?|"
               r"type|enter")
_FIELD_TAIL = (r"(?:\s+(?:в|во)\s+(?:поле|строк[уе]|графу|окошко)\s*(?P<f>.*)"
               r"|\s+(?:в|во)\s+(?P<s>поиск\w*|строку\s+поиска|поле\s+поиска)"
               r"|\s+in(?:to)?\s+(?:the\s+)?(?P<e>.+?)(?:\s+(?:field|box))?)")
_TYPE_RE = re.compile(rf"^(?:{_TYPE_VERBS})\s+(?P<x>.+?){_FIELD_TAIL}?$", re.IGNORECASE)
# «Напиши» — только с текстом в кавычках: «напиши Каре, что опоздаю» — цель
# для агента, а не ввод в поле
_WRITE_QUOTED_RE = re.compile(
    rf"^(?:напиши(?:те)?|написать|write)\s+(?P<x>[«\"“].+?[»\"”]){_FIELD_TAIL}?$", re.IGNORECASE)
_OPEN_RE = re.compile(
    r"^(?:открой(?:те)?|открыть|запусти(?:те)?|запустить|включи(?:те)?|open|launch|start)"
    r"\s+(?:приложение\s+|прогу\s+|программу\s+|the\s+app\s+|app\s+|the\s+)?(?P<a>.+)$",
    re.IGNORECASE)
# Это команды способа 1 (интенты, web/src/phone) или вовсе не приложения
_OPEN_NOT_APP_RE = re.compile(
    r"^(?:фонарик\w*|будильник\w*|таймер\w*|flashlight|torch|alarm\w*|timer\w*|"
    r"сайт\b|страниц\w*|ссылк\w*|website|page|link)", re.IGNORECASE)


# Просьба-цель телефону, которую первый этап не делает: «на телефоне напиши
# Каре, что опоздаю», «закажи на телефоне пиццу», «позвони с телефона маме»
_GOAL_VERB_RE = re.compile(
    r"^(?:напиши|отправь|пошли|перешли|позвони|набери|поставь|включи|выключи|открой|запусти|найди|закажи|"
    r"купи|оплати|удали|скачай|установи|запиши|ответь|сделай|сними|сфоткай|сфотографируй|"
    r"настрой|смени|поменяй|переведи|забронируй|подпишись|отпишись|"
    r"send|call|write|text|message|order|buy|pay|delete|remove|install|download|reply|find|open|launch|"
    r"set|turn|make|play|record|book|change|subscribe|unsubscribe)(?:те)?\b", re.IGNORECASE)

UNSUPPORTED_NOTE = (
    "The user asked you to do something on their phone. You cannot do that yet: on the phone "
    "you can only do single steps the user names explicitly — tap a button or row by its label, "
    "type text into a field, scroll, go back or home, open an app, open the notification shade, "
    "or read what is on the screen (for example \"on the phone, tap Send\"). Nothing was done "
    "on the phone or on the computer in this turn. Say so honestly and briefly, in your own "
    "style, and suggest doing it step by step; do NOT claim that you did anything.")


def mentions_phone(text: str) -> bool:
    """В реплике явное обращение к телефону («на телефоне», «on my phone»)."""
    return bool(text and _MARK_RE.search(text))


def _strip_names(s: str, names: Iterable[str]) -> str:
    # «Коннор, на телефоне нажми …» — обращение к персоне в начале
    for n in sorted({str(x).strip() for x in names or () if str(x).strip()}, key=len, reverse=True):
        m = re.match(rf"^\s*{re.escape(n)}\w{{0,2}}\s*[,:!]?\s+", s, re.IGNORECASE)
        if m:
            return s[m.end():]
    return s


def _unquote(s: str) -> str:
    s = s.strip()
    m = _QUOTE_RE.match(s)
    return m.group(1).strip() if m else s


def _request_text(text: str, names: Iterable[str]) -> tuple:
    """Реплика без обращений, «на телефоне», вежливости и хвоста →
    (текст, вопрос ли, вежливая ли форма); None — не к телефону или отрицание."""
    if not text or len(text) > 300 or not mentions_phone(text):
        return None
    if _NEG_RE.search(text):
        return None
    s = _strip_names(" ".join(text.split()), names)
    s = _MARK_RE.sub(" ", s)
    # «посмотри на телефоне, что там» → «посмотри, что там»: без пробела перед знаком
    s = re.sub(r"\s+([,.!?…])", r"\1", " ".join(s.split())).strip(" ,:;")
    polite = bool(_POLITE_RE.match(s))
    s = _POLITE_RE.sub("", s)
    s = _FILLER_RE.sub("", s)
    question = text.rstrip().endswith("?")
    s = _TAIL_RE.sub("", s).strip(" ,:;")
    return s, question, polite


def parse(text: str, names: Iterable[str] = ()) -> Optional[dict]:
    """Команда экрану телефона → {kind, …} или None (не команда телефону).
    kind: tap {target} | type {text, field} | scroll {dir} | read |
    back | home | recents | notifications | quick_settings | open {app}."""
    req = _request_text(text, names)
    if req is None:
        return None
    s, question, polite = req
    if _READ_RE.match(s):
        return {"kind": "read"}
    if not s or (question and not polite):
        return None
    # Вежливая форма — инфинитивом: «можешь нажать» → как «нажми»
    for rx, kind in ((_BACK_RE, "back"), (_HOME_RE, "home"), (_RECENTS_RE, "recents"),
                     (_NOTIF_RE, "notifications"), (_QUICK_RE, "quick_settings")):
        if rx.match(s):
            return {"kind": kind}
    m = _SCROLL_RE.match(s)
    if m:
        d = (m.group("d") or m.group("b") or "down").lower()
        return {"kind": "scroll", "dir": _SCROLL_DIRS.get(d, "down")}
    m = _TYPE_RE.match(s) or _WRITE_QUOTED_RE.match(s)
    if m:
        value = _unquote(m.group("x"))
        field = m.group("f") or m.group("e") or ""
        if m.group("s"):
            field = "поиск"
        field = _unquote(field)
        if re.match(r"^(?:поиск\w*|строк\w+\s+поиска|search)$", field, re.IGNORECASE):
            field = "поиск"
        if value:
            return {"kind": "type", "text": value, "field": field}
    m = _TAP_RE.match(s)
    if m:
        target = _unquote(_TAP_SUFFIX_RE.sub("", m.group("t").strip()))
        if target:
            return {"kind": "tap", "target": target}
    m = _OPEN_RE.match(s)
    if m:
        app = _unquote(m.group("a").strip())
        if app and not _OPEN_NOT_APP_RE.match(app):
            return {"kind": "open", "app": app}
    return None


# ── Подписи и поиск элемента ────────────────────────────────────────

_NORM_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def _norm(s) -> str:
    s = str(s or "").lower().replace("ё", "е")
    return " ".join(_NORM_RE.sub(" ", s).split())


def _osa_ok(a: str, b: str) -> bool:
    # Опечатка в подписи: как у команд режима управления (cc_typos)
    from app.features import cc_typos
    limit = cc_typos._limit(b)
    return limit > 0 and cc_typos._osa(a, b, limit) <= limit


def _score(target: str, label: str) -> float:
    """Насколько подпись элемента похожа на то, что назвал человек: 0 — нет."""
    t, l = _norm(target), _norm(label)
    if not t or not l:
        return 0.0
    if t == l:
        return 5.0
    words = l.split()
    if l.startswith(t + " ") or l.endswith(" " + t):
        return 4.0
    if f" {t} " in f" {l} ":
        return 3.5
    if len(t) >= 3 and l.startswith(t):
        return 3.0
    if len(t) >= 4 and t in l:
        return 2.0
    if any(_osa_ok(t, w) for w in words) or _osa_ok(t, l):
        return 1.5
    return 0.0


def _elements(screen: dict) -> list:
    return [e for e in (screen.get("elements") or []) if isinstance(e, dict)]


def _label(e: dict) -> str:
    # У поля имя — подсказка («Поиск»), текст в нём — содержимое
    if e.get("ed"):
        return str(e.get("h") or e.get("t") or "").strip()
    return str(e.get("t") or e.get("h") or "").strip()


def _tap_target(screen: dict, e: dict) -> dict:
    """Текст внутри нажимаемой строки — жмём строку (её номер — p)."""
    if e.get("c") or e.get("ed") or "on" in e:
        return e
    p = e.get("p")
    els = _elements(screen)
    if isinstance(p, int) and 0 <= p < len(els):
        return els[p]
    return e


def match(screen: dict, target: str, fields_only: bool = False) -> list:
    """Лучшие кандидаты на «нажми <target>» — список нажимаемых целей
    (одна цель — один раз), все с одинаковым лучшим счётом."""
    best = 0.0
    out: list = []
    seen = set()
    for e in _elements(screen):
        if e.get("k") == "list" and not _label(e):
            continue
        if fields_only and not e.get("ed"):
            continue
        if e.get("ed"):
            # Поле узнаём по названию («Поиск»), а не по тому, что в нём набрано:
            # иначе «нажми РабИнфСем» после ввода «РабИнфСем» в поиск жмёт поле,
            # а не найденный чат (Honor, Telegram, 30.10)
            sc = max(_score(target, e.get("h") or ""), 0.5 * _score(target, e.get("t") or ""))
        else:
            sc = max(_score(target, e.get("t") or ""), _score(target, e.get("h") or ""))
        if sc <= 0:
            continue
        tgt = _tap_target(screen, e)
        key = tgt.get("i")
        if sc > best:
            best, out, seen = sc, [], set()
        if sc == best and key not in seen:
            seen.add(key)
            out.append({"el": e, "target": tgt})
    return _one_per_control(out)


# Вложенные друг в друга элементы с одной подписью — один элемент управления
# (строка поиска: контейнер «Поиск», кнопка «Поиск», поле «Поиск» — Honor):
# спрашивать «какое из четырёх?» не о чем. Оставляем самый конкретный
_KIND_RANK = {"field": 0, "button": 1, "switch": 2, "check": 2, "item": 3}


def _box(e: dict) -> Optional[list]:
    b = e.get("b") or []
    return b if len(b) == 4 else None


def _center_in(inner: list, outer: list) -> bool:
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def _same_control(a: dict, b: dict) -> bool:
    A, B = _box(a), _box(b)
    return bool(A and B) and (_center_in(A, B) or _center_in(B, A))


def _one_per_control(cands: list) -> list:
    groups: list = []
    for c in cands:
        for g in groups:
            if any(_same_control(c["target"], x["target"]) for x in g):
                g.append(c)
                break
        else:
            groups.append([c])

    def rank(c):
        t = c["target"]
        b = _box(t) or [0, 0, 0, 0]
        return _KIND_RANK.get(t.get("k"), 4), (b[2] - b[0]) * (b[3] - b[1])
    return [min(g, key=rank) for g in groups]


def _place(screen: dict, e: dict) -> str:
    b = e.get("b") or []
    h = float(screen.get("h") or 0)
    if len(b) != 4 or h <= 0:
        return "middle"
    y = (b[1] + b[3]) / 2 / h
    return "top" if y < 0.33 else "bottom" if y > 0.66 else "middle"


def _describe(screen: dict, cand: dict, lang) -> str:
    e, tgt = cand["el"], cand["target"]
    label = _label(tgt) or _label(e)
    kind = _word(_KIND_WORDS, tgt.get("k") or "button", lang)
    return f"«{clip(label, 60)}» — {kind}, {_word(_PLACES, _place(screen, tgt), lang)}"


def _visible(screen: dict, limit: int = 8) -> str:
    out, seen = [], set()
    for e in _elements(screen):
        if not (e.get("c") or e.get("ed") or "on" in e):
            continue
        lab = _label(e)
        if lab and _norm(lab) not in seen:
            seen.add(_norm(lab))
            out.append(f"«{clip(lab, 40)}»")
        if len(out) >= limit:
            break
    return ", ".join(out)


# ── Приватные приложения ────────────────────────────────────────────

# Деньги, пароли, коды входа, госуслуги: экран не читается и не нажимается
_PRIVATE_PKG_RE = re.compile(
    r"bank|sber|tinkoff|tbank|vtb|alfa|raiffeisen|gazprombank|psbank|pochtabank|otkritie|"
    r"sovcom|rshb|uralsib|rosbank|akbars|mtsbank|ozonbank|yoomoney|qiwi|wallet|"
    r"\.pay\b|\bpay\.|paypal|revolut|monzo|transferwise|\bwise\b|"
    r"password|passwd|bitwarden|keepass|lastpass|1password|onepassword|dashlane|nordpass|"
    r"enpass|authenticator|authy|\.otp|totp|aegis|freeotp|"
    r"ru\.rostel|gosuslugi|esia|nalog|\bmir\.|mirpay|sbp\b|binance|bybit|crypto|metamask|"
    r"trustwallet|coinbase", re.IGNORECASE)
_PRIVATE_APP_RE = re.compile(
    r"банк|сбер|т-банк|тинькофф|втб|альфа|райф|газпромбанк|кошел[её]к|пароли|"
    r"госуслуги|налог|bank|wallet|password|authenticator", re.IGNORECASE)
# Код из уведомления («Код: 4821», «Your code is 912 334») — не в заметку персоне
_CODE_RE = re.compile(
    r"(?i)(код|code|пароль|password|otp|pin|пин)([^\d\n]{0,24})\d[\d\s-]{2,9}\d")


def is_private(screen: dict) -> bool:
    pkg = str(screen.get("pkg") or "")
    app = str(screen.get("app") or "")
    return bool(_PRIVATE_PKG_RE.search(pkg) or _PRIVATE_APP_RE.search(app))


_SYSTEM_SCREENS = {
    "home": {"ru": "главный экран", "en": "the home screen"},
    "com.android.systemui": {"ru": "шторка уведомлений", "en": "the notification shade"},
}


# Где это — с предлогом: «в Telegram», «в шторке уведомлений», «на главном экране»
_SYSTEM_WHERE = {
    "home": {"ru": "на главном экране", "en": "on the home screen"},
    "com.android.systemui": {"ru": "в шторке уведомлений", "en": "in the notification shade"},
}


def _where(screen: dict, lang) -> str:
    key = "home" if screen.get("home") else screen.get("pkg")
    if key in _SYSTEM_WHERE:
        return _word(_SYSTEM_WHERE, key, lang)
    return f"in {_app_name(screen, lang)}" if is_en(lang) else f"в {_app_name(screen, lang)}"


def _app_name(screen: dict, lang) -> str:
    if screen.get("home"):
        return _word(_SYSTEM_SCREENS, "home", lang)
    if screen.get("pkg") in _SYSTEM_SCREENS:
        return _word(_SYSTEM_SCREENS, screen["pkg"], lang)
    app = str(screen.get("app") or screen.get("pkg") or "").strip()
    return clip(app, 40) if app else ("the app" if is_en(lang) else "приложении")


# ── Устройство ──────────────────────────────────────────────────────

class PhoneError(Exception):
    """Ответ человеку готов (text) — команда не выполнена."""

    def __init__(self, reply: str):
        super().__init__(reply)
        self.reply = reply


def find_phone() -> tuple:
    """Телефон с управлением экраном → (устройство на связи | None, текст отказа | None)."""
    phones = devices.with_capability(TAP)
    if not phones:
        return None, "not_set_up"
    online = [d for d in phones if d.get("online")]
    if online:
        return online[0], None
    return phones[0], "offline"


def _failure_detail(detail: str) -> tuple:
    # «gone: элемента больше нет» → ("gone", «элемента больше нет»)
    code, _, msg = str(detail or "").partition(":")
    return code.strip(), msg.strip()


def _command(dev: dict, action: str, args: Optional[dict], timeout: float, lang,
             label: str = "") -> dict:
    try:
        res = devices.command(dev["id"], action, args or {}, timeout=timeout)
    except devices.DeviceError as e:
        if e.code in ("offline", "unknown"):
            raise PhoneError(text("offline", lang, name=dev.get("name") or "phone"))
        if e.code == "timeout":
            raise PhoneError(text("timeout", lang))
        if e.code == "unsupported":
            raise PhoneError(text("unsupported", lang))
        code, msg = _failure_detail(e.detail)
        if code in ("gone", "disabled"):
            raise PhoneError(text(code, lang, label=clip(label, 60)))
        if code == "no_window":
            raise PhoneError(text("no_window", lang))
        if code in ("no_field", "not_field"):
            raise PhoneError(text("no_field", lang))
        raise PhoneError(text("failed", lang, detail=clip(msg or code or e.detail, 120)))
    return res.get("data") or {}


def _read(dev: dict, lang) -> dict:
    data = _command(dev, READ, {}, READ_TIMEOUT_SEC, lang)
    screen = data.get("screen") or {}
    _check_screen(screen, lang)
    return screen


def _reread(dev: dict, lang) -> dict:
    # Нужного нет: экран мог ещё доигрывать переход после прошлой команды
    # (голосом и агентом команды идут подряд, без паузы на набор)
    time.sleep(REREAD_DELAY_SEC)
    return _read(dev, lang)


def _check_screen(screen: dict, lang) -> None:
    if screen.get("screen_off"):
        raise PhoneError(text("screen_off", lang))
    if screen.get("locked"):
        raise PhoneError(text("locked", lang))


def _labels_sig(screen: dict) -> tuple:
    return tuple(_label(e) for e in _elements(screen)[:40])


# ── Отложенные вопросы («нажать?», «какое?») ────────────────────────

class _Pending:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_chat: dict = {}

    def put(self, key: str, user_id, entry: dict) -> None:
        entry = dict(entry, user_id=str(user_id) if user_id else None, at=time.time())
        with self._lock:
            self._by_chat[str(key)] = entry

    def take(self, key: str, user_id) -> Optional[dict]:
        with self._lock:
            entry = self._by_chat.get(str(key))
            if not entry:
                return None
            if time.time() - entry["at"] > PENDING_TTL_SEC:
                self._by_chat.pop(str(key), None)
                return None
            owner = entry.get("user_id")
            if owner and user_id and str(owner) != str(user_id):
                return None
            return self._by_chat.pop(str(key))

    def has(self, key: str) -> bool:
        with self._lock:
            entry = self._by_chat.get(str(key))
            return bool(entry) and time.time() - entry["at"] <= PENDING_TTL_SEC

    def clear(self, key: str) -> None:
        with self._lock:
            self._by_chat.pop(str(key), None)


_pending = _Pending()
_apps_cache: dict = {}  # device id → (время, [{label, pkg}])


def has_pending(key: str) -> bool:
    return _pending.has(key)


def clear_pending(key: str) -> None:
    _pending.clear(key)


# ── Ход ─────────────────────────────────────────────────────────────

def turn(message: str, key: str, user_id, lang: Optional[str] = None,
         names: Iterable[str] = (), foreign_pending: bool = False) -> Optional[dict]:
    """Реплика → {"reply": текст} (ответ готов, без модели), {"note": блок}
    (ответ — персоне, с заметкой о том, что на экране) или None (не наше).
    foreign_pending — в чате ждёт ответа подтверждение режима управления
    компьютером: оно новее нашего вопроса (наш при создании его гасит),
    «да» и номер — ему."""
    pending = _pending.take(key, user_id)
    if pending is not None and not foreign_pending:
        res = _answer(pending, message, key, user_id, lang, names)
        if res is not None:
            return res
    cmd = parse(message, names)
    if cmd is None:
        # Цель телефону, которую мы ещё не умеем, — персоне честная заметка
        # (иначе она «сделает» на словах, а в режиме управления фразу взял бы
        # компьютер); телефона с управлением нет — обычный ход
        req = _request_text(message, names)
        if req and not req[1] and _GOAL_VERB_RE.match(req[0]) and devices.with_capability(TAP):
            logger.info("[PhoneControl] просьба-цель телефону — пока не умеем, заметка персоне")
            return {"note": UNSUPPORTED_NOTE}
        return None
    dev, err = find_phone()
    if err:
        name = (dev or {}).get("name") or "phone"
        logger.info(f"[PhoneControl] {cmd['kind']}: телефона нет ({err})")
        return {"reply": text(err, lang, name=name)}
    try:
        return _run(cmd, dev, key, user_id, lang)
    except PhoneError as e:
        return {"reply": e.reply}


def _run(cmd: dict, dev: dict, key: str, user_id, lang) -> dict:
    kind = cmd["kind"]
    if kind in ("back", "home", "recents", "notifications", "quick_settings"):
        data = _command(dev, GLOBAL, {"name": kind}, ACT_TIMEOUT_SEC, lang)
        logger.info(f"[PhoneControl] {kind}")
        reply = text(kind, lang)
        if kind == "back":
            reply += _now_app(None, data.get("screen") or {}, lang)
        return {"reply": reply}
    if kind == "open":
        return _open(cmd["app"], dev, key, user_id, lang)
    screen = _read(dev, lang)
    app = _app_name(screen, lang)
    if is_private(screen):
        logger.info(f"[PhoneControl] {kind}: приватное приложение {screen.get('pkg')}")
        return {"reply": text("private", lang, app=app)}
    if kind == "read":
        logger.info(f"[PhoneControl] read: {screen.get('pkg')}, {len(_elements(screen))} элементов")
        return {"note": read_note(screen)}
    if kind == "scroll":
        before = _labels_sig(screen)
        data = _command(dev, SCROLL, {"dir": cmd["dir"]}, ACT_TIMEOUT_SEC, lang)
        after = _labels_sig(data.get("screen") or {})
        logger.info(f"[PhoneControl] scroll {cmd['dir']}: {data.get('method')}")
        word = _word(_DIRS, cmd["dir"], lang)
        if after and after == before:
            return {"reply": text("scroll_end", lang, dir=word)}
        return {"reply": text("scrolled", lang, dir=word)}
    if kind == "tap":
        cands = match(screen, cmd["target"])
        if not cands:
            # Экран мог ещё открываться после прошлой команды — посмотрим снова
            screen = _reread(dev, lang)
            if is_private(screen):
                return {"reply": text("private", lang, app=_app_name(screen, lang))}
            cands = match(screen, cmd["target"])
        if not cands:
            return {"reply": _not_found(screen, cmd["target"], lang)}
        if len(cands) > 1:
            return _ask_choice(screen, cands, cmd, key, user_id, lang, "choose")
        return _tap(dev, screen, cands[0], key, user_id, lang)
    if kind == "type":
        if not any(e.get("ed") for e in _elements(screen)):
            screen = _reread(dev, lang)
            if is_private(screen):
                return {"reply": text("private", lang, app=_app_name(screen, lang))}
        return _type(dev, screen, cmd, key, user_id, lang)
    return {"reply": text("failed", lang, detail=kind)}


def _not_found(screen: dict, target: str, lang) -> str:
    reply = text("not_found", lang, app=_app_name(screen, lang), target=clip(target, 60))
    vis = _visible(screen)
    return reply + (text("visible", lang, items=vis) if vis else "")


def _ask_choice(screen: dict, cands: list, cmd: dict, key: str, user_id, lang,
                question: str) -> dict:
    cands = cands[:MAX_CHOICES]
    lines = "\n".join(f"{i}. {_describe(screen, c, lang)}" for i, c in enumerate(cands, 1))
    _pending.put(key, user_id, {"kind": "choose", "cmd": cmd, "screen": screen,
                                "cands": cands})
    return {"reply": text(question, lang, target=clip(cmd.get("target") or "", 60), items=lines)}


# На телефоне одно «Купить» списывает с сохранённой карты (магазин
# приложений, покупки внутри приложения) — тоже оплата
_BUY_RE = re.compile(r"^(?:купить|приобрести|оплатить|buy|purchase|pay)\b", re.IGNORECASE)


def _risk(screen: dict, cand: dict) -> Optional[str]:
    labels = {_label(cand["el"]), _label(cand["target"])}
    risks = set()
    for lab in labels:
        if not lab:
            continue
        if _BUY_RE.match(_norm(lab)):
            risks.add("payment")
        # Строже, чем у одиночной команды на компьютере: как у агента
        # задач («Buy now», «Купить в 1 клик» — оплата)
        r = ComputerControlManager.risky_label({"kind": "click", "element": lab, "origin": "task"})
        if r:
            risks.add(r)
    if "payment" in risks:
        return "payment"
    return next(iter(sorted(risks)), None)


def _tap(dev: dict, screen: dict, cand: dict, key: str, user_id, lang,
         confirmed: bool = False) -> dict:
    tgt = cand["target"]
    label = _label(cand["el"]) or _label(tgt)
    risk = _risk(screen, cand)
    if risk == "payment":
        logger.info(f"[PhoneControl] tap: оплата не нажимается ({screen.get('pkg')})")
        return {"reply": text("payment", lang, label=clip(label, 60))}
    if risk and not confirmed:
        _pending.put(key, user_id, {"kind": "confirm", "action": "tap", "screen": screen,
                                    "cand": cand})
        return {"reply": text("confirm_tap", lang, label=clip(label, 60),
                              where=_where(screen, lang)), "confirm": True}
    if tgt.get("off"):
        return {"reply": text("disabled", lang, label=clip(label, 60))}
    args = {"snap": screen.get("snap"), "i": tgt.get("i"), "t": tgt.get("t") or "",
            "b": tgt.get("b")}
    data = _command(dev, TAP, args, ACT_TIMEOUT_SEC, lang, label=label)
    logger.info(f"[PhoneControl] tap ({risk or 'обычное'}) в {screen.get('pkg')}: {data.get('method')}")
    return {"reply": text("tapped", lang, label=clip(label, 60))
            + _now_app(screen, data.get("screen") or {}, lang)}


def _now_app(before: Optional[dict], after: dict, lang) -> str:
    # Нажатие открыло другое приложение — скажем какое
    if not after.get("pkg") or (before is not None and after.get("pkg") == before.get("pkg")):
        return ""
    return text("now_app", lang, app=_app_name(after, lang))


def _type(dev: dict, screen: dict, cmd: dict, key: str, user_id, lang,
          field: Optional[dict] = None, confirmed: bool = False) -> dict:
    if field is None:
        fields = [e for e in _elements(screen) if e.get("ed")]
        if not fields:
            return {"reply": text("no_field", lang)}
        want = cmd.get("field") or ""
        if want:
            cands = match(screen, want, fields_only=True)
            if not cands and _norm(want).startswith("поиск"):
                cands = match(screen, "search", fields_only=True)
            if not cands:
                return {"reply": text("no_such_field", lang, target=clip(want, 60))}
        else:
            focused = [e for e in fields if e.get("f")]
            pool = focused or fields
            cands = [{"el": e, "target": e} for e in pool]
        if len(cands) > 1:
            return _ask_choice(screen, cands, cmd, key, user_id, lang, "choose_field")
        field = cands[0]["target"]
    label = _label(field) or ("field" if is_en(lang) else "поле")
    if field.get("pw") and not confirmed:
        _pending.put(key, user_id, {"kind": "confirm", "action": "type", "screen": screen,
                                    "cmd": cmd, "field": field})
        return {"reply": text("confirm_type_pw", lang, label=clip(label, 60),
                              where=_where(screen, lang)), "confirm": True}
    if field.get("off"):
        return {"reply": text("disabled", lang, label=clip(label, 60))}
    args = {"snap": screen.get("snap"), "i": field.get("i"), "t": field.get("t") or "",
            "b": field.get("b"), "text": cmd["text"]}
    data = _command(dev, TYPE, args, ACT_TIMEOUT_SEC, lang, label=label)
    logger.info(f"[PhoneControl] type в {screen.get('pkg')}: {data.get('method')}")
    if field.get("pw"):
        return {"reply": text("typed_pw", lang, label=clip(label, 60))}
    return {"reply": text("typed", lang, text=clip(cmd["text"], 80), label=clip(label, 60))}


# ── Подбор приложения — зеркало matchApp из web/src/phone/parse.ts ──
# (способ 1 открывает приложение сам; scripts/test_phone_control сверяет
# обе версии на одних названиях)

_ALIAS_GROUPS = [
    ["ютуб", "ютюб", "youtube"],
    ["ютуб музыка", "youtube music", "yt music"],
    ["вотсап", "ватсап", "вацап", "воцап", "whatsapp"],
    ["телега", "телеграм", "телеграмм", "telegram"],
    ["вк", "вконтакте", "vk", "vkontakte"],
    ["инста", "инстаграм", "instagram"],
    ["хром", "гугл хром", "chrome", "google chrome"],
    ["гугл", "google"],
    ["плей маркет", "плеймаркет", "гугл плей", "плей стор", "google play", "play store", "play маркет"],
    ["вайбер", "viber"],
    ["зум", "zoom"],
    ["скайп", "skype"],
    ["фейсбук", "facebook"],
    ["твиттер", "twitter"],
    ["тикток", "тик ток", "tiktok"],
    ["спотифай", "spotify"],
    ["нетфликс", "netflix"],
    ["дискорд", "discord"],
    ["яндекс", "yandex"],
    ["почта", "gmail", "mail", "email", "почта mail", "электронная почта"],
    ["камера", "camera"],
    ["галерея", "gallery", "фото", "photos", "google фото", "фотографии"],
    ["настройки", "settings"],
    ["калькулятор", "calculator"],
    ["календарь", "calendar"],
    ["часы", "clock"],
    ["заметки", "notes", "keep", "google keep"],
    ["контакты", "contacts"],
    ["сообщения", "смс", "messages", "sms"],
    ["телефон", "звонилка", "phone", "dialer"],
    ["браузер", "browser", "chrome", "firefox", "яндекс браузер", "opera"],
    ["карты", "maps", "google maps", "яндекс карты"],
    ["музыка", "music"],
    ["погода", "weather"],
    ["диктофон", "recorder", "voice recorder", "запись звука"],
]
_TRANSLIT = dict(zip(
    "абвгдежзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
     "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def _translit(s: str) -> str:
    return "".join(_TRANSLIT.get(c, c) for c in s)


def _app_clean(s: str) -> str:
    s = str(s or "").lower().replace("ё", "е").replace("’", "'").replace("`", "'")
    return " ".join(re.sub(r"[^\w ]+|_", " ", " ".join(s.split())).split())


def _nominative(s: str) -> str:
    # «камеру» → «камера», «галерею» → «галерея»
    out = []
    for w in s.split(" "):
        if len(w) >= 3 and re.search(r"[а-я]у$", w):
            w = w[:-1] + "а"
        elif len(w) >= 3 and re.search(r"[а-я]ю$", w):
            w = w[:-1] + "я"
        out.append(w)
    return " ".join(out)


def _alias_group(name: str) -> Optional[list]:
    forms = {_app_clean(name), _nominative(_app_clean(name))}
    out: list = []
    for g in _ALIAS_GROUPS:
        if any(a in forms for a in g):
            out.extend(a for a in g if a not in out)
    return out or None


def _levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        diag, prev[0] = prev[0], i
        for j in range(1, len(b) + 1):
            tmp = prev[j]
            prev[j] = min(prev[j] + 1, prev[j - 1] + 1, diag + (0 if a[i - 1] == b[j - 1] else 1))
            diag = tmp
    return prev[len(b)]


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return 1 - _levenshtein(a, b) / max(len(a), len(b))


def _app_score(variants: list, label: str) -> float:
    lab = _app_clean(label)
    forms = list(dict.fromkeys([lab, _translit(lab)]))
    best = 0.0
    for v, w in variants:
        for f in forms:
            sc = 0.0
            if v == f:
                sc = 1.0
            elif v.replace(" ", "") == f.replace(" ", ""):
                sc = 0.98
            elif f.startswith(v + " "):
                sc = 0.8
            elif v in f.split(" "):
                sc = 0.75
            elif len(v) >= 4 and f.replace(" ", "").startswith(v.replace(" ", "")):
                sc = 0.7
            sim = _similarity(v.replace(" ", ""), f.replace(" ", ""))
            if sim >= 0.8:
                sc = max(sc, sim * 0.95)
            best = max(best, sc * w)
    return best


def match_app(name: str, apps: list) -> tuple:
    """Приложение по названию → ("ok", app) | ("ambiguous", [app…]) | ("not_found", None).
    apps — [{label, pkg}] из apps.list."""
    base = _app_clean(name)
    group = _alias_group(name)
    own = [v for v in dict.fromkeys([base, _nominative(base), _translit(base),
                                     _translit(_nominative(base))]) if v]
    variants = [(v, 1.0) for v in own] + [(a, 0.97) for a in (group or []) if a not in own]
    scored = sorted(((a, _app_score(variants, a.get("label") or "")) for a in apps),
                    key=lambda x: -x[1])
    scored = [x for x in scored if x[1] >= 0.66]
    if not scored:
        return "not_found", None
    top = scored[0][1]
    exact = [x for x in scored if x[1] >= top - 0.001 and x[1] >= 0.9]
    if len(exact) == 1:
        return "ok", exact[0][0]
    if len(exact) > 1:
        return "ambiguous", [x[0] for x in exact[:MAX_CHOICES]]
    if len(scored) == 1 or top - scored[1][1] >= 0.1:
        return "ok", scored[0][0]
    return "ambiguous", [x[0] for x in scored if top - x[1] < 0.1][:MAX_CHOICES]


def _apps(dev: dict, lang) -> list:
    cached = _apps_cache.get(dev["id"])
    if cached and time.time() - cached[0] < APPS_TTL_SEC:
        return cached[1]
    data = _command(dev, APPS, {}, READ_TIMEOUT_SEC, lang)
    apps = [a for a in data.get("apps") or [] if isinstance(a, dict) and a.get("pkg")]
    _apps_cache[dev["id"]] = (time.time(), apps)
    return apps


def _open(name: str, dev: dict, key: str, user_id, lang, chosen: Optional[dict] = None) -> dict:
    if chosen is None:
        status, found = match_app(name, _apps(dev, lang))
        if status == "not_found":
            return {"reply": text("no_app", lang, target=clip(name, 60))}
        if status == "ambiguous":
            # Одинаковые названия (клон приложения) — различаем по пакету
            labels = [str(a.get("label") or "") for a in found]
            lines = "\n".join(
                f"{i}. {clip(a.get('label'), 60)}"
                + (f" ({a.get('pkg')})" if labels.count(str(a.get("label") or "")) > 1 else "")
                for i, a in enumerate(found, 1))
            _pending.put(key, user_id, {"kind": "choose", "cmd": {"kind": "open", "app": name},
                                        "apps": found})
            return {"reply": text("choose_app", lang, target=clip(name, 60), items=lines)}
        chosen = found
    _command(dev, OPEN, {"pkg": chosen["pkg"]}, OPEN_TIMEOUT_SEC, lang)
    logger.info(f"[PhoneControl] open {chosen['pkg']}")
    return {"reply": text("opened", lang, app=clip(chosen.get("label") or chosen["pkg"], 40))}


def _answer(p: dict, message: str, key: str, user_id, lang, names) -> Optional[dict]:
    """Ответ на наш вопрос → итог или None (не ответ: вопрос снят, реплика —
    обычным путём)."""
    if p["kind"] == "confirm":
        verdict = classify_confirmation(message, names)
        if verdict == "NO":
            return {"reply": text("declined_type" if p.get("action") == "type" else "declined", lang)}
        if verdict != "YES":
            return None
        dev, err = find_phone()
        if err:
            return {"reply": text(err, lang, name=(dev or {}).get("name") or "phone")}
        try:
            if p.get("action") == "type":
                return _type(dev, p["screen"], p["cmd"], key, user_id, lang,
                             field=p["field"], confirmed=True)
            return _tap(dev, p["screen"], p["cand"], key, user_id, lang, confirmed=True)
        except PhoneError as e:
            return {"reply": e.reply}
    if p["kind"] == "choose":
        n = parse_choice(message, names)
        if n is None:
            return None
        options = p.get("apps") or p.get("cands") or []
        if not 1 <= n <= len(options):
            _pending.put(key, user_id, {k: v for k, v in p.items() if k not in ("at", "user_id")})
            return {"reply": text("choice_range", lang, n=len(options))}
        dev, err = find_phone()
        if err:
            return {"reply": text(err, lang, name=(dev or {}).get("name") or "phone")}
        cmd = p["cmd"]
        try:
            if cmd["kind"] == "open":
                return _open(cmd["app"], dev, key, user_id, lang, chosen=options[n - 1])
            if cmd["kind"] == "type":
                return _type(dev, p["screen"], cmd, key, user_id, lang,
                             field=options[n - 1]["target"])
            return _tap(dev, p["screen"], options[n - 1], key, user_id, lang)
        except PhoneError as e:
            return {"reply": e.reply}
    return None


# ── «Что на экране» — заметка персоне ───────────────────────────────

def _scrub(s: str) -> str:
    # Код входа из уведомления или СМС — не модели
    return _CODE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[hidden]", s)


def read_note(screen: dict) -> str:
    """Заметка персоне: что на экране телефона на самом деле (по снимку)."""
    lines, seen, size = [], set(), 0
    els = _elements(screen)
    # Строки, у которых тексты внутри идут отдельными элементами
    has_texts = {e.get("p") for e in els if isinstance(e.get("p"), int)}
    for e in els:
        k = e.get("k")
        lab = _scrub(_label(e))
        if (k == "item" and e.get("i") in has_texts) or (k == "list" and not lab):
            continue  # тексты строки идут отдельными элементами
        p = e.get("p")
        if isinstance(p, int) and 0 <= p < len(els) and lab and els[p].get("k") != "item" \
                and _norm(lab) in _norm(_label(els[p])):
            continue  # подпись плитки («Internet, AndroidWifi») его уже говорит
        if e.get("pw"):
            line = f"[password field \"{clip(e.get('h') or lab, 40)}\" — content hidden]"
        elif k == "field":
            hint = _scrub(str(e.get("h") or ""))
            val = _scrub(str(e.get("t") or ""))
            name = f' "{clip(hint, 40)}"' if hint else ""
            content = f'"{clip(val, 120)}"' if val else "empty"
            line = f"[text field{name}: {content}]"
        elif k in ("switch", "check"):
            line = f"[{k} {'on' if e.get('on') else 'off'}] {clip(lab, 80)}"
        elif k == "button":
            line = f"[button] {clip(lab, 80)}"
        elif k == "item":
            line = f"[row] {clip(lab, 120)}"  # строка без текстов внутри (Telegram)
        else:
            line = clip(lab, 160)
        sig = _norm(line)
        if (not lab and k != "field") or sig in seen:
            continue
        seen.add(sig)
        if size + len(line) > READ_MAX_CHARS or len(lines) >= READ_MAX_LINES:
            lines.append("…")
            break
        lines.append(line)
        size += len(line) + 1
    app = _app_name(screen, "en") if (screen.get("home") or screen.get("pkg") in _SYSTEM_SCREENS) \
        else str(screen.get("app") or screen.get("pkg") or "unknown app")
    title = str(screen.get("title") or "").strip()
    head = f"App on screen: {app}" + (f" (window: {title})" if title and title != app else "")
    body = "\n".join(f"- {x}" for x in lines) if lines else "- (nothing readable on the screen)"
    return ("The user asked what is on their phone screen. The phone's screen-control service "
            "has just read it — this is what is actually there, top to bottom:\n"
            f"{head}\n{body}\n"
            "Tell the user briefly, in your own style, what is on the screen; mention only "
            "what is listed. You only read the screen: nothing was tapped or changed.")

"""
Заметки пользователя: «запиши в заметки …», «что у меня в заметках?»,
«удали заметку 2» — и блок «Заметки» на главной веба.

Одна записная книжка на персону: data/api_<персона>/notes/notes.json —
общая для веб-чата и Telegram-бота (другой процесс), поэтому каждая
операция читает файл заново под межпроцессным локом (file_lock), а не
держит копию в памяти.

Кто что видит в чате: владелец — свои заметки (написанные им в чате или
с главной), остальные собеседники — только написанные ими самими. Главная
веба — интерфейс владельца, там видны все заметки персоны.

Запись и удаление делает код, не модель: текст заметки вырезается из
реплики регулярками, модели в промпт уходит только итог («записано: …»),
чтобы она подтвердила своими словами.
"""

import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from app.core import timeutil
from app.core.atomic_io import atomic_write_json, file_lock, load_json_safe
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

MAX_NOTES = 500
MAX_TEXT = 4000
# Сколько текста заметок уходит в промпт и в список сообщением
_CONTEXT_NOTE_MAX = 1200
_CONTEXT_TOTAL_MAX = 6000
_LIST_NOTE_MAX = 400
_LIST_TOTAL_MAX = 3500

_PERSONA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NOTE_ID_RE = re.compile(r"^[a-f0-9]{6,32}$")


def notes_path(persona: str) -> Optional[Path]:
    # data/api_<persona>/notes/notes.json; None — недопустимый id
    if not _PERSONA_ID_RE.match(str(persona or "")):
        return None
    return data_dir() / f"api_{persona}" / "notes" / "notes.json"


def _clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class UserNotes:
    """Записная книжка персоны. Заметка — dict: id, text, created_at,
    updated_at, author_id, author_name, owner (написал владелец), source
    ('chat' | 'home'). Порядок в файле — по времени записи."""

    def __init__(self, persona: str):
        self.persona = persona
        self.path = notes_path(persona)
        if self.path is None:
            raise ValueError(f"недопустимый id персоны: {persona!r}")
        self._lock = threading.Lock()

    def _load(self) -> list:
        data = load_json_safe(self.path, default=[], label="Notes")
        return [n for n in data if isinstance(n, dict) and n.get("id")] \
            if isinstance(data, list) else []

    def all(self) -> List[dict]:
        with self._lock, file_lock(self.path):
            return self._load()

    def visible(self, user_id, owner: bool) -> List[dict]:
        """Заметки, которые этот собеседник видит в чате: владелец — свои
        (owner) и написанные им самим, остальные — только свои."""
        uid = str(user_id or "")
        return [n for n in self.all()
                if (owner and n.get("owner")) or (uid and str(n.get("author_id") or "") == uid)]

    def add(self, text: str, *, author_id="", author_name: str = "",
            owner: bool = False, source: str = "chat") -> Optional[dict]:
        """Новая заметка или None (пустой текст / книжка заполнена)."""
        text = str(text or "").strip()[:MAX_TEXT]
        if not text:
            return None
        note = {
            "id": secrets.token_hex(6),
            "text": text,
            "created_at": time.time(),
            "updated_at": None,
            "author_id": str(author_id or ""),
            "author_name": str(author_name or "")[:64],
            "owner": bool(owner),
            "source": source,
        }
        with self._lock, file_lock(self.path):
            notes = self._load()
            if len(notes) >= MAX_NOTES:
                return None
            notes.append(note)
            atomic_write_json(self.path, notes)
        logger.info(f"[Notes] {self.persona}: заметка {note['id']} ({len(text)} симв.)")
        return note

    def update(self, note_id: str, text: str) -> Optional[dict]:
        text = str(text or "").strip()[:MAX_TEXT]
        if not text:
            return None
        with self._lock, file_lock(self.path):
            notes = self._load()
            for n in notes:
                if n.get("id") == note_id:
                    n["text"] = text
                    n["updated_at"] = time.time()
                    atomic_write_json(self.path, notes)
                    return n
        return None

    def remove(self, note_ids: Iterable[str]) -> List[dict]:
        # Удалённые заметки (каких уже нет — пропускаются молча)
        ids = set(note_ids)
        if not ids:
            return []
        with self._lock, file_lock(self.path):
            notes = self._load()
            removed = [n for n in notes if n.get("id") in ids]
            if removed:
                atomic_write_json(self.path, [n for n in notes if n.get("id") not in ids])
        if removed:
            logger.info(f"[Notes] {self.persona}: удалено {len(removed)}")
        return removed

    def is_full(self) -> bool:
        return len(self.all()) >= MAX_NOTES


def valid_note_id(note_id: str) -> bool:
    return bool(_NOTE_ID_RE.match(str(note_id or "")))


# ── Разбор реплики ─────────────────────────────────────────────────────

@dataclass
class NotesRequest:
    # kind: add — записать; delete — удалить; show — показать список;
    # ask — реплика про заметки (вопрос по ним) — модель отвечает по списку
    kind: str
    text: str = ""       # add: текст заметки; delete: что удалить
    ref: bool = False    # add: «запиши это» — текст из прошлого сообщения
    all: bool = False    # delete: «все заметки»


_I = re.IGNORECASE
_IS = re.IGNORECASE | re.DOTALL

_RU_NOUN = r"(?:заметк\w*|заметок|блокнот\w*)"
_EN_NOUN = r"(?:notes|notebook|notepad)"
_ANY_NOUN_RE = re.compile(rf"(?<![^\W\d_]){_RU_NOUN}|\b(?:notes?|notebook|notepad)\b", _I)

# «Запиши / занеси / сохрани …» (+ «-ка»); «можешь записать …?»
_RU_ADD_VERB = (r"(?:запиши|запишите|записывай|занеси|занесите|внеси|внесите|впиши|впишите|"
                r"добавь|добавьте|сохрани|сохраните|запомни|запомните|черкни|закинь|"
                r"(?:можешь|можете|сможешь|мог(?:ла|ли)?\s+бы)\s+(?:ты\s+|вы\s+)?(?:мне\s+)?"
                r"(?:записать|занести|внести|вписать|добавить|сохранить))(?:-ка)?")
_RU_TARGET = (r"(?:в|во)\s+(?:мои\s+|свои\s+|наши\s+|твои\s+|мой\s+|свой\s+|твой\s+)?"
              rf"{_RU_NOUN}")
# «запиши (X) в заметки (Y)» и «в заметки запиши Y»
_RU_ADD_RE = re.compile(
    rf"^(?P<lead>.*?)(?<![^\W\d_])(?P<verb>{_RU_ADD_VERB})(?![^\W\d_])(?P<mid>.*?)"
    rf"(?<![^\W\d_])(?P<target>{_RU_TARGET})(?![^\W\d_])(?P<post>.*)$", _IS)
_RU_ADD_REV_RE = re.compile(
    rf"^(?P<lead>.*?)(?<![^\W\d_])(?P<target>{_RU_TARGET})\s+(?:мне\s+|себе\s+)?"
    rf"(?P<verb>{_RU_ADD_VERB})(?![^\W\d_])(?P<post>.*)$", _IS)
# «сделай заметку: X», «новая заметка — X», «заметка: X»
_RU_NOTE_OBJ_RE = re.compile(
    r"^(?P<lead>.*?)(?<![^\W\d_])(?:(?:сделай|создай|оставь|добавь|запиши|напиши|заведи)\s+"
    r"(?:мне\s+|себе\s+)?(?:новую\s+|ещё\s+одну\s+|еще\s+одну\s+)?заметку|"
    r"нов(?:ая|ую)\s+заметк[ау])(?![^\W\d_])(?P<post>.*)$", _IS)
_RU_NOTE_COLON_RE = re.compile(r"^(?P<lead>)заметка\s*[:—–-]\s*(?P<post>.+)$", _IS)

_EN_ADD_VERB = (r"(?:add|put|write(?:\s+down)?|jot(?:\s+down)?|save|note(?:\s+down)?|"
                r"record|store|keep)")
_EN_TARGET = rf"(?:to|in|into|on)\s+(?:my\s+|the\s+|your\s+|our\s+)?{_EN_NOUN}"
_EN_ADD_RE = re.compile(
    rf"^(?P<lead>.*?)\b(?P<verb>{_EN_ADD_VERB})\b(?P<mid>.*?)\b(?P<target>{_EN_TARGET})\b(?P<post>.*)$",
    _IS)
_EN_NOTE_OBJ_RE = re.compile(
    r"^(?P<lead>.*?)\b(?:(?:take|make|add|create|leave|write|jot\s+down)\s+"
    r"(?:me\s+)?(?:a|another)\s+(?:new\s+|quick\s+)?note|new\s+note)\b"
    r"(?:\s+(?:of|that|about)\b)?(?P<post>.*)$", _IS)

# Отрицание перед глаголом: «не записывай это в заметки», «don't add …»
_NEG_TAIL_RE = re.compile(
    r"(?:(?<![^\W\d_])не\s+(?:надо\s+|нужно\s+|стоит\s+|будем\s+)?|"
    r"\b(?:don['’]?t|do\s+not|never|no\s+need\s+to)\s+(?:\w+\s+)?)$", _I)

# Слова-вставки вокруг текста заметки
_FILLER = (r"(?:мне|нам|себе|у\s+себя|пожалуйста|пжлст|пж|плиз|плз|please|pls|for\s+me|"
           r"быстренько|быстро|ка|down|it\s+down)")
_LEAD_FILLER_RE = re.compile(rf"^(?:[\s,:;.\-—–!]+|{_FILLER}(?![^\W\d_]))+", _I)
# Хвост срезается по одному слову (_strip_tail), не одной регуляркой: серия
# «(?:\s+|…)+$» на длинном пробеле перед буквой — экспоненциальный перебор
_TAIL_WORD_RE = re.compile(
    rf"(?<![^\W\d_])(?:{_FILLER}|спасибо|thanks)$", _I)
_TAIL_SEP = " \t\r\n,;-—–"
# Окно поиска просьбы в длинном тексте: она в начале или в конце реплики —
# ленивые «.*?» шаблонов записи на десятках тысяч символов заняли бы секунды
_SCAN = 400
# Вежливое начало просьбы — не текст заметки: «can you add …», «слушай, а запиши …»
_POLITE_LEAD_RE = re.compile(
    r"^(?:\s*(?:please|pls|can|could|would|will|you|hey|ok|okay|and|also|now|just|so|"
    r"слушай|а|и|ну|так|ещё|еще|пожалуйста|давай|ты|вы|можешь|можете|сможешь|мог|могла|"
    r"могли|бы)(?![^\W\d_])\s*,?)+", _I)
# «… что X», «… то, что X», «… о том, что X», «… that X»
_LEAD_THAT_RE = re.compile(r"^(?:(?:о\s+)?то[мй]?\s*,?\s*)?(?:что|чтобы|that)(?![^\W\d_])\s*,?\s*", _I)

# «это», «этот рецепт», «то, что ты сказал», «this», «what you said» —
# текст заметки в прошлом сообщении
_REF_RE = re.compile(
    r"^(?:вот\s+)?(?:это|эту|этот|эти|этого|всё\s+это|все\s+это|это\s+всё|это\s+все|всё|все|"
    r"сказанное|написанное|последнее|"
    r"this|that|it|these|those|everything|all\s+of\s+(?:this|that|it))"
    r"(?:\s+[^\W\d_][\w-]*){0,2}\s*[.!?…]*$", _I)
# Вся фраза — ссылка: «(всё, что) ты сейчас сказал (выше)», «твой ответ»;
# «Петя сказал, что ты сказал ему правду» — текст заметки
_REF_YOU_RE = re.compile(
    r"^(?:(?:всё|все|то|это)\s*,?\s*(?:что\s+)?)?(?:(?:ты|вы)\s+(?:только\s+что\s+|сейчас\s+|выше\s+|мне\s+)*"
    r"(?:сказал|написал|дал|прислал|предложил|перечислил|посоветовал|скинул|ответил|рассказал)\w*"
    r"|(?:твой|твоё|твое|твоего|последний)\s+(?:последний\s+)?(?:ответ|сообщени\w*)"
    r"|(?:all\s+of\s+)?what\s+you\s+(?:just\s+)?(?:said|wrote|sent|told|suggested|listed)"
    r"|your\s+(?:last\s+)?(?:answer|message|reply)"
    r"|выше|above)(?:\s+[^\s.!?…]+){0,3}\s*[.!?…]*$", _I)

_RU_DEL_RE = re.compile(
    r"(?<![^\W\d_])(?:удали|удалите|убери|уберите|вычеркни|вычеркните|сотри|сотрите|"
    r"выкинь|выбрось|почисти|очисти|очистите)(?:-ка)?(?![^\W\d_])(?P<rest>.*)$", _IS)
_EN_DEL_RE = re.compile(
    r"\b(?:delete|remove|erase|clear|cross\s+out|drop|wipe)\b(?P<rest>.*)$", _IS)
# Что срезается из хвоста просьбы удалить, чтобы остался номер или текст
_DEL_NOISE_RE = re.compile(
    rf"(?<![^\W\d_])(?:из|в|у|с)\s+(?:моих\s+|своих\s+|мои\s+)?{_RU_NOUN}"
    rf"|(?<![^\W\d_])(?:мои|свои|моих|все|всё|эту|эти|одну|мне|пожалуйста|номер|под\s+номером)(?![^\W\d_])"
    rf"|{_RU_NOUN}"
    r"|(?<![^\W\d_])(?:про|о|об|где|которая|которую|в\s+которой|насчёт|насчет|с\s+текстом|со\s+словами)(?![^\W\d_])"
    rf"|\b(?:from|in)\s+(?:my\s+|the\s+)?{_EN_NOUN}\b"
    r"|\b(?:my|the|all|a|note|notes|number|please|about|that\s+says|with|saying)\b"
    r"|[№#]", _I)
_DEL_ALL_RE = re.compile(r"(?<![^\W\d_])(?:все|всё)(?![^\W\d_])|\ball\b", _I)
_CLEAR_VERB_RE = re.compile(r"(?<![^\W\d_])(?:почисти|очисти|очистите)(?![^\W\d_])|\b(?:clear|wipe)\b", _I)

# Показать список: просьба кончается словом «заметки»
_SHOW_END_RE = re.compile(rf"(?:{_RU_NOUN}|{_EN_NOUN})\s*[?.!…]*\s*$", _I)
_SHOW_WORD_RE = re.compile(
    r"(?<![^\W\d_])(?:покажи|покажите|прочитай|прочти|прочитайте|зачитай|выведи|перечисли|"
    r"дай|скинь|пришли|напомни|глянь|посмотри|проверь|что|чего|какие|список|мои|все)(?![^\W\d_])"
    r"|\b(?:show|read|list|give|send|tell|check|see|what|whats|what's|my)\b"
    r"|^\s*(?:заметки|notes)\s*[?.!…]*\s*$", _I)
# Явная команда приложению «открой заметки» — не наш список (это приложение
# «Заметки» на компьютере/телефоне)
_APP_CMD_RE = re.compile(r"^\s*(?:открой|запусти|закрой|open|launch|close|start)\b", _I)
# Английское «note» многозначно («note that …»): реплика про заметки — только
# «my notes», «in notes», «notebook»
_EN_MENTION_RE = re.compile(
    rf"\b(?:my|the|your|our)\s+{_EN_NOUN}\b|\bin\s+(?:my\s+)?notes\b|\b(?:notebook|notepad)\b", _I)
_RU_MENTION_RE = re.compile(rf"(?<![^\W\d_]){_RU_NOUN}", _I)


def _names_alt(names: Iterable[str]) -> str:
    ws = sorted({str(n).strip() for n in names or () if n and len(str(n).strip()) >= 2},
                key=len, reverse=True)
    return "|".join(re.escape(w) for w in ws)


def strip_address(text: str, names: Iterable[str] = ()) -> str:
    # «Коннор, запиши …», «эй Коннор …», «hey Connor, …» → «запиши …»
    alt = _names_alt(names)
    lead = r"\s*(?:(?:hey|hi|ok|okay|эй|слушай|так)\b\s*,?\s*)?"
    if alt:
        text = re.sub(rf"^(?:{lead}(?:{alt})(?![\w'’-])[\s,:;!.…—–-]*)+", "", text, flags=_I)
    return text.strip()


def _strip_tail(part: str) -> str:
    # «… пожалуйста», «… мне, спасибо» — по слову с конца
    while True:
        cut = part.rstrip(_TAIL_SEP)
        m = _TAIL_WORD_RE.search(cut[-40:])
        if m:
            cut = cut[: len(cut) - (len(cut[-40:]) - m.start())]
        if cut == part:
            return part
        part = cut


def _clean(part: str) -> str:
    part = part or ""
    # Края — в окне: середина длинной заметки не трогается
    head, body = part[:200], part[200:]
    part = _LEAD_FILLER_RE.sub("", head) + body
    return _strip_tail(part).strip()


def _lead_sep(lead_raw: str) -> bool:
    # Текст перед глаголом отделён знаком: «Мука, яйца — запиши в заметки»
    tail = lead_raw.rstrip(" \t")
    return tail.endswith("\n") or tail.rstrip()[-1:] in ("—", "–", ":", "-", ".", "!")


def _is_ref(part: str) -> bool:
    # Ссылка — короткая фраза: «ты сказал» внутри длинной заметки — её текст
    return bool(part) and len(part) <= 60 and (
        bool(_REF_RE.match(part)) or bool(_REF_YOU_RE.match(part)))


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _add_request(gd: dict, polite_q: bool) -> NotesRequest:
    lead_raw = gd.get("lead") or ""
    lead = _clean(_POLITE_LEAD_RE.sub("", lead_raw))
    mid = _clean(gd.get("mid") or "")
    post_raw = gd.get("post") or ""
    post = _clean(_LEAD_THAT_RE.sub("", _LEAD_FILLER_RE.sub("", post_raw)))
    if polite_q:
        post = post.rstrip("?").rstrip()
        mid = mid.rstrip("?").rstrip()
    mid = _clean(_LEAD_THAT_RE.sub("", mid))
    ref = False
    if post and mid and not _is_ref(mid) and not _is_ref(post):
        sep = ": " if re.match(r"\s*[:—–-]", post_raw) else " "
        text = f"{mid}{sep}{post}"
    else:
        text = post if post and not _is_ref(post) else (mid if mid and not _is_ref(mid) else "")
        ref = not text and (_is_ref(post) or _is_ref(mid))
    # «Мука, яйца, молоко — запиши в заметки»: текст до глагола (отделён
    # знаком или переводом строки)
    if (not text) and lead and _lead_sep(lead_raw):
        text, ref = lead, False
    return NotesRequest("add", text=_cap(text), ref=ref)


def _delete_request(rest: str, clear_verb: bool) -> NotesRequest:
    target = _DEL_NOISE_RE.sub(" ", rest)
    target = re.sub(r"\s+", " ", target).strip(" ,.:;!?…-—–")
    # «удали все заметки (про X)», «очисти заметки»
    all_ = bool(_DEL_ALL_RE.search(rest)) or (clear_verb and not target)
    return NotesRequest("delete", text=target, all=all_)


def _add_groups(rx: "re.Pattern", t: str) -> Optional[dict]:
    """Группы шаблона записи. Длинный текст — просьба в первых или последних
    _SCAN символах (остальное — текст заметки до или после неё)."""
    if len(t) <= 2 * _SCAN:
        m = rx.match(t)
        return m.groupdict() if m else None
    m = rx.match(t[:_SCAN])
    if m:
        gd = m.groupdict()
        gd["post"] = (gd.get("post") or "") + t[_SCAN:]
        return gd
    start = len(t) - _SCAN
    m = rx.match(t[start:])
    if m:
        gd = m.groupdict()
        gd["lead"] = t[:start] + (gd.get("lead") or "")
        return gd
    return None


def parse_request(text: str, names: Iterable[str] = ()) -> Optional[NotesRequest]:
    """Просьба про заметки в реплике или None. names — обращения к персоне
    (срезаются в начале фразы)."""
    if not text or not _ANY_NOUN_RE.search(text):
        return None
    t = strip_address(str(text), names)
    if not t:
        return None
    head = t.split("\n", 1)[0][:_SCAN]
    polite_q = bool(re.search(r"(?<![^\W\d_])(?:можешь|можете|сможешь|мог(?:ла|ли)?\s+бы)(?![^\W\d_])"
                              r"|\b(?:can|could|would)\s+you\b", head, _I))

    # Удаление — раньше записи: «удали из заметок …» не текст новой заметки
    for rx in (_RU_DEL_RE, _EN_DEL_RE):
        m = rx.search(head)
        if m and _ANY_NOUN_RE.search(m.group("rest")) \
                and not _NEG_TAIL_RE.search(head[:m.start()]):
            rest = m.group("rest") + t[len(head):]
            return _delete_request(rest, bool(_CLEAR_VERB_RE.search(m.group(0))))

    for rx in (_RU_ADD_RE, _RU_ADD_REV_RE, _RU_NOTE_OBJ_RE, _RU_NOTE_COLON_RE,
               _EN_ADD_RE, _EN_NOTE_OBJ_RE):
        gd = _add_groups(rx, t)
        if gd is None:
            continue
        lead_raw = gd.get("lead") or ""
        if _NEG_TAIL_RE.search(lead_raw[-40:]):
            return NotesRequest("ask")
        # Английский глагол — повелительный, в начале фразы: «what did I
        # write in my notes …?» — вопрос, а не запись
        if rx in (_EN_ADD_RE, _EN_NOTE_OBJ_RE) and _POLITE_LEAD_RE.sub("", lead_raw[:_SCAN]).strip() \
                and not _lead_sep(lead_raw):
            continue
        # Глагол — в первой строке: «…\nзапиши в заметки» в середине длинного
        # текста ловится, только если просьба — первая или последняя строка
        if "\n" in lead_raw.strip() and "\n" in (gd.get("post") or "").strip():
            break
        return _add_request(gd, polite_q)

    if _APP_CMD_RE.match(t):
        return None
    if len(t) <= 80 and _SHOW_END_RE.search(t) and _SHOW_WORD_RE.search(t):
        return NotesRequest("show")
    if _RU_MENTION_RE.search(t) or _EN_MENTION_RE.search(t):
        return NotesRequest("ask")
    return None


def is_command(text: str, names: Iterable[str] = ()) -> bool:
    # Просьба записать, удалить или показать заметки (не просто упоминание)
    req = parse_request(text, names)
    return bool(req and req.kind != "ask")


# ── Удаление: номер или текст ──────────────────────────────────────────

_ENDING_RE = re.compile(
    r"(?:ами|ями|ого|его|ому|ему|ыми|ими|ую|юю|ая|яя|ой|ей|ое|ее|ые|ие|ый|ий|ым|им|ом|ем|ах|ях|"
    r"у|ю|а|я|о|е|ы|и|й|ь|s)$")


def _stems(text: str) -> List[str]:
    words = re.findall(r"[^\W_]+", (text or "").lower().replace("ё", "е"))
    return [_ENDING_RE.sub("", w) if len(w) > 4 else w for w in words]


_NUMS_RE = re.compile(r"^\s*\d+(?:\s*[-–—]\s*\d+)?(?:\s*(?:,|и|and|;)\s*\d+(?:\s*[-–—]\s*\d+)?)*\s*$", _I)
_LAST_RE = re.compile(r"^(?:последн\w*|last|latest|the\s+last\s+one)$", _I)
_FIRST_RE = re.compile(r"^(?:перв\w*|first)$", _I)


def resolve_delete(req: NotesRequest, notes: List[dict]) -> Optional[List[dict]]:
    """Какие заметки удалить (из notes — список, видимый собеседнику, по
    порядку записи; номера — его строки). None — непонятно какие."""
    target = (req.text or "").strip()
    if not notes:
        return []
    if not target:
        return list(notes) if req.all else None
    if _LAST_RE.match(target):
        return [notes[-1]]
    if _FIRST_RE.match(target):
        return [notes[0]]
    if _NUMS_RE.match(target):
        idx = set()
        for lo, hi in re.findall(r"(\d+)(?:\s*[-–—]\s*(\d+))?", target):
            lo, hi = int(lo), int(hi or lo)
            lo, hi = min(lo, hi), max(lo, hi)
            if hi - lo < 50:
                idx.update(range(lo, hi + 1))
        picked = [notes[i - 1] for i in sorted(idx) if 1 <= i <= len(notes)]
        return picked or None
    want = [s for s in _stems(target) if len(s) >= 2]
    if not want:
        return None
    matched = []
    for n in notes:
        words = _stems(n.get("text") or "")
        if all(any(w.startswith(s) for w in words) for s in want):
            matched.append(n)
    if req.all:
        return matched or None
    return matched if len(matched) == 1 else None


# ── Тексты для пользователя и для модели ───────────────────────────────

_LIST_TITLE = {"ru": "📝 Заметки:", "en": "📝 Notes:"}
_LIST_MORE = {"ru": "…и ещё {n} — все заметки на главной странице.",
              "en": "…and {n} more — all notes are on the home page."}
_LIST_EMPTY = {"ru": "Заметок пока нет.", "en": "No notes yet."}


def _date(ts) -> str:
    try:
        return timeutil.from_ts(float(ts)).strftime("%d.%m")
    except (TypeError, ValueError, OSError):
        return ""


def render_list(notes: List[dict], lang: Optional[str] = None) -> str:
    """Список сообщением: «1. текст · 12.10»; длинные — обрезаны."""
    lang = lang if lang in _LIST_TITLE else "ru"
    if not notes:
        return _LIST_EMPTY[lang]
    lines = [_LIST_TITLE[lang]]
    total = 0
    for i, n in enumerate(notes, 1):
        body = _clip(n.get("text"), _LIST_NOTE_MAX).replace("\n", "\n   ")
        d = _date(n.get("created_at"))
        line = f"{i}. {body}" + (f" · {d}" if d else "")
        if total + len(line) > _LIST_TOTAL_MAX and i > 1:
            lines.append(_LIST_MORE[lang].format(n=len(notes) - i + 1))
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def notes_for_prompt(notes: List[dict]) -> str:
    # Нумерованные заметки для промпта: свежие важнее — при нехватке места
    # выпадают самые старые (номера остаются настоящими)
    lines = []
    total = 0
    for i in range(len(notes), 0, -1):
        n = notes[i - 1]
        d = _date(n.get("created_at"))
        line = f"{i}. [{d}] {_clip(n.get('text'), _CONTEXT_NOTE_MAX)}" if d else \
            f"{i}. {_clip(n.get('text'), _CONTEXT_NOTE_MAX)}"
        if total + len(line) > _CONTEXT_TOTAL_MAX and lines:
            lines.append(f"(+{i} older notes not shown)")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(reversed(lines))


NOTES_RULE = (
    "Notes are the user's own notebook kept by the system for you. Never "
    "invent notes, never claim you cannot keep notes, and do not mention "
    "files, the system or technical details."
)


def prompt_saved(text: str) -> str:
    return ("You have just written this into the user's notes — it is "
            f"already saved:\n«{_clip(text, 300)}»\n"
            "Confirm briefly, in your own style, that it is written down. Do "
            "not repeat the whole note back. " + NOTES_RULE)


def prompt_nothing_to_save() -> str:
    return ("The user wants to save something to their notes but did not say "
            "what. Nothing was saved. Ask briefly what to write down. " + NOTES_RULE)


def prompt_full() -> str:
    return (f"The user's notes are full ({MAX_NOTES} notes), so nothing was "
            "saved. Say so briefly: old notes can be deleted on the home page "
            "or with «удали заметку N». " + NOTES_RULE)


def prompt_deleted(removed: List[dict], left: int) -> str:
    items = "\n".join(f"- {_clip(n.get('text'), 200)}" for n in removed)
    return (f"You have just deleted these from the user's notes:\n{items}\n"
            f"{left} notes are left. Confirm briefly, in your own style. " + NOTES_RULE)


def prompt_delete_unclear(notes: List[dict]) -> str:
    if not notes:
        return ("The user asked to delete a note, but their notes are empty — "
                "nothing was deleted. Say so briefly. " + NOTES_RULE)
    return ("The user asked to delete a note, but it is unclear which one — "
            "NOTHING was deleted. Ask which note number to delete. Their notes "
            f"(number. [date] text):\n{notes_for_prompt(notes)}\n" + NOTES_RULE)


def prompt_show(notes: List[dict]) -> str:
    if not notes:
        return ("The user asked what is in their notes, but there are no notes "
                "yet. Say so briefly; they can ask you «запиши в заметки …». " + NOTES_RULE)
    return ("The user asked to see their notes. The full list is sent to them "
            "automatically as a separate message right after your reply — DO "
            "NOT print, list or retell the notes yourself. Reply with one short "
            "line in your own style. For reference, their notes:\n"
            f"{notes_for_prompt(notes)}\n" + NOTES_RULE)


def prompt_ask(notes: List[dict]) -> str:
    if not notes:
        return ("The user's notes are empty — there is nothing written down "
                "yet. If they ask about their notes, say so honestly. " + NOTES_RULE)
    return ("The user's notes (number. [date] text), written down by them "
            f"earlier:\n{notes_for_prompt(notes)}\n"
            "If the user asks about their notes or about something they wrote "
            "down, answer strictly from these notes; if it is not there, say so "
            "honestly. If the message is not about the notes, ignore this block. "
            + NOTES_RULE)

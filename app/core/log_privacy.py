"""
Приватность логов процесса — единая точка, через которую проходит каждая
запись logging: фабрика LogRecord (все записи INFO+ любого логгера, в т.ч.
хендлеров uvicorn и кольцевого буфера веб-панели, добавленных позже) и
фильтр хендлеров корня (DEBUG — только если хендлер его реально пишет).

Маской уходят: известные секреты чатов (cc_privacy.KnownSecrets — пароль,
данный ответом агенту/слоту сценария), значения после «пароль/код/PIN …»
(command_secret_values), email/телефоны/карты/токены и секретные параметры
URL. Точечная маска в строке лога забывается на новом пути — фильтр ловит
любой. Правила чисел мягче redact_inline: id чатов Telegram (-100…),
user_id и метки времени в логе остаются читаемыми.
"""

import logging
import re
import threading
from typing import Iterable, Optional

from app.features.cc_privacy import (_CMD_SECRET_FILLER, _CMD_SECRET_VERB_RE,
                                     _CMD_SECRET_WORD_RE, _EMAIL_RE,
                                     _looks_token, command_secret_values,
                                     known_secret_values, mask, scrub_url)

_URL_SPLIT_RE = re.compile(r"(https?://[^\s\"'<>«»]+)")
# Карта: 13–19 цифр (группы через пробел/дефис), первая 2–6, сходится Луна.
# «-100…» (id групп Telegram), цифры внутри слова/числа — не карта
_CARD_RE = re.compile(r"(?<![\w\-.])[2-6](?:[ -]?\d){12,18}(?!\w)")
# Телефон: с «+», российский 8/7 + 10 цифр (с разделителями или слитно),
# 999 123-45-67 с разделителями. Голые 10 цифр — это id, не телефон
_PHONE_RE = re.compile(
    r"(?<![\w+\-=.])(?:"
    r"\+\d[\d ()\-]{8,16}\d"
    r"|[78][ (\-]*\d{3}[ )\-]*\d{3}[ \-]*\d{2}[ \-]*\d{2}"
    r"|\(?\d{3}\)?[ \-]\d{3}[ \-]\d{2}[ \-]?\d{2}"
    r")(?!\d)")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-+/=.]{20,}")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")

_known_cache = ((), None)  # (значения, скомпилированная альтернатива)
_tls = threading.local()


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def _luhn(num: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(num)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _card(m: "re.Match") -> str:
    d = _digits(m.group(0))
    return mask(m.group(0)) if 13 <= len(d) <= 19 and _luhn(d) else m.group(0)


def _phone(m: "re.Match") -> str:
    return mask(m.group(0)) if 10 <= len(_digits(m.group(0))) <= 13 \
        else m.group(0)


def _token(m: "re.Match") -> str:
    s = m.group(0)
    if _UUID_RE.fullmatch(s) or not _looks_token(s):
        return s
    return mask(s)


def _known_re(values) -> Optional["re.Pattern"]:
    # Альтернатива известных секретов — компилируется заново, только когда
    # набор поменялся (каждая запись лога не компилирует regex)
    global _known_cache
    key = tuple(sorted({str(v) for v in values if v}, key=len, reverse=True))
    if not key:
        return None
    cached = _known_cache
    if cached[0] == key:
        return cached[1]
    rx = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(v) for v in key)
                    + r")(?!\w)", re.IGNORECASE)
    _known_cache = (key, rx)
    return rx


# «password=…», «code: …», «token=…» в тексте лога — значение маской
# (3-значный код статуса «code 200» остаётся)
_KV_SECRET_RE = re.compile(
    r"(?<![^\W\d_])(?:парол\w*|password\w*|passwd|pwd|pin|пин|код|code|otp|"
    r"2fa|token|токен\w*|secret|секрет\w*|api[_-]?key|apikey)"
    r"(\s*[=:]\s*)([^\s,;&'\"«»]{4,})", re.IGNORECASE)
_WORD_TOK_RE = re.compile(r"[^\s«»\"'“”„`,;]+")


# Окно после слова секрета: столько содержательных токенов (служебные
# «от/для/is» считаются, чистая пунктуация «—»/«:» — нет)
_CMD_WINDOW = 4
# Короткий цифровой секрет (3 цифры) — только после cvv/cvc/pin
_SHORT_SECRET_WORD_RE = re.compile(r"(?:cvv2?|cvc2?|pin|пин)", re.IGNORECASE)


def _secretish(tok: str, short_ok: bool) -> bool:
    # Значение-секрет в окне: буквы+цифры, цифры (≥4, после cvv/pin — ≥3)
    # или спецсимвол; «12:00:01»/«200» — не секрет
    if "***(" in tok:
        return False
    has_d = any(ch.isdigit() for ch in tok)
    has_a = any(ch.isalpha() for ch in tok)
    if has_d and has_a and len(tok) >= 4:
        return True
    if tok.isdigit():
        return len(tok) >= (3 if short_ok else 4)
    return len(tok) >= 4 and bool(re.search(r"[!@#$%^&*+=?~]", tok))


def _cmd_mask(text: str) -> str:
    """Значения секрета по правилам команды ввода (command_secret_values).
    Эхо команды («введи Kotik2019 в поле пароль» — глагол ввода + слово
    секрета) — как у маски истории; иначе в окне из нескольких слов после
    «пароль/код/PIN …» («пароль от почты X», «пароль — X», «код из смс
    4821») — первое секретоподобное, а нет его — слово сразу после (≥4
    символов, «пароль qwerty»). В обычной строке лога со словом «code»
    числа (id, коды статуса) не трогаем."""
    if not _CMD_SECRET_WORD_RE.search(text):
        return text
    vals = []
    if _CMD_SECRET_VERB_RE.search(text):
        try:
            vals = list(command_secret_values(text) or ())
        except Exception:
            vals = []
    else:
        toks = [raw.strip(".!?…:()") for raw in _WORD_TOK_RE.findall(text)]
        # Токен без букв и цифр («—», «:», «=») — пунктуация, окно не тратит
        toks = [t for t in toks if any(ch.isalnum() for ch in t)]
        for i, tok in enumerate(toks):
            if not _CMD_SECRET_WORD_RE.fullmatch(tok):
                continue
            short_ok = bool(_SHORT_SECRET_WORD_RE.fullmatch(tok))
            window = []
            for t in toks[i + 1:i + 1 + _CMD_WINDOW]:
                if _CMD_SECRET_WORD_RE.fullmatch(t):
                    break
                window.append(t)
            hit = next((t for t in window if _secretish(t, short_ok)), None)
            if hit is None:
                first = next((t for t in window
                              if t.lower() not in _CMD_SECRET_FILLER), "")
                if len(first) >= 4 and not _CMD_SECRET_VERB_RE.fullmatch(first):
                    hit = first
            if hit:
                vals.append(hit)
    vals = [v for v in vals if v and len(v) >= 3 and "***(" not in v]
    if not vals:
        return text
    rx = re.compile(r"(?<!\w)(?:" + "|".join(
        re.escape(v) for v in sorted(set(vals), key=len, reverse=True))
        + r")(?!\w)")
    return rx.sub(lambda m: mask(m.group(0)), text)


def redact_log(text) -> str:
    """Строка лога без секретов (см. модуль). Идемпотентна."""
    s = "" if text is None else str(text)
    if not s:
        return s
    rx = _known_re(known_secret_values())
    if rx is not None:
        s = rx.sub(lambda m: mask(m.group(0)), s)
    chunks = _URL_SPLIT_RE.split(s)
    for i, ch in enumerate(chunks):
        if i % 2:
            chunks[i] = scrub_url(ch)
            continue
        ch = _cmd_mask(ch)
        ch = _KV_SECRET_RE.sub(
            lambda m: m.group(0) if "***(" in m.group(2)
            else m.group(0)[:m.start(2) - m.start(0)] + mask(m.group(2)), ch)
        if "@" in ch:
            ch = _EMAIL_RE.sub(lambda m: mask(m.group(0)), ch)
        if any(c.isdigit() for c in ch):
            ch = _CARD_RE.sub(_card, ch)
            ch = _PHONE_RE.sub(_phone, ch)
        if len(ch) >= 20:
            ch = _TOKEN_RE.sub(_token, ch)
        chunks[i] = ch
    return "".join(chunks)


def redact_record(rec: logging.LogRecord) -> None:
    """Сообщение (и текст исключения) записи — через redact_log. Структура
    args сохраняется, когда маска ложится по аргументам (uvicorn.access
    разбирает args кортежем); иначе msg — готовая строка, args пусты."""
    if getattr(_tls, "busy", False):
        return
    mark = getattr(rec, "_cc_redacted", None)
    if mark is not None and mark[0] is rec.msg and mark[1] is rec.args:
        return
    _tls.busy = True
    try:
        try:
            msg = rec.getMessage()
        except Exception:
            return  # ошибку форматирования покажет сам хендлер
        new = redact_log(msg)
        if new != msg:
            args = rec.args
            per_arg = None
            if isinstance(args, tuple) and args:
                per_arg = tuple(redact_log(a) if isinstance(a, str) else a
                                for a in args)
            done = False
            if per_arg is not None and isinstance(rec.msg, str):
                try:
                    done = (rec.msg % per_arg) == new
                except Exception:
                    done = False
            if done or (per_arg is not None and str(
                    rec.name or "").startswith("uvicorn.access")):
                rec.args = per_arg
            else:
                rec.msg, rec.args = new, ()
        exc = rec.exc_info
        if exc and exc[0] is not None and not rec.exc_text:
            try:
                txt = logging.Formatter().formatException(exc)
                red = redact_log(txt)
                if red != txt:
                    rec.exc_text = red
            except Exception:
                pass
        rec._cc_redacted = (rec.msg, rec.args)
    finally:
        _tls.busy = False


class LogPrivacyFilter(logging.Filter):
    """Фильтр хендлера: та же маска (записи, созданные мимо фабрики, —
    makeLogRecord — и DEBUG, который фабрика пропускает)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            redact_record(record)
        except Exception:
            pass
        return True


_install_lock = threading.Lock()
_installed = False
_FILTER = LogPrivacyFilter()


def _attach(loggers: Iterable[logging.Logger]) -> None:
    for lg in loggers:
        for h in list(getattr(lg, "handlers", ()) or ()):
            if not any(isinstance(f, LogPrivacyFilter) for f in h.filters):
                h.addFilter(_FILTER)


def install() -> None:
    """Поставить маску логов (идемпотентно; повторный вызов вешает фильтр
    на хендлеры, появившиеся после прошлого — кольцевой буфер API)."""
    global _installed
    with _install_lock:
        if not _installed:
            orig = logging.getLogRecordFactory()

            def factory(*args, **kwargs):
                rec = orig(*args, **kwargs)
                # DEBUG — дешевле в фильтре хендлера: он есть, только если
                # хендлер запись действительно пишет. makeLogRecord зовёт
                # фабрику с level=None — такую запись не трогаем (её поля
                # придут позже, их маскирует фильтр хендлера)
                try:
                    lvl = rec.levelno
                    if isinstance(lvl, int) and lvl >= logging.INFO:
                        redact_record(rec)
                except Exception:
                    pass
                return rec

            factory._cc_log_privacy = True
            logging.setLogRecordFactory(factory)
            _installed = True
        _attach([logging.getLogger()] + [
            logging.getLogger(n) for n in ("uvicorn", "uvicorn.error",
                                           "uvicorn.access")])


def input_for_log(text, hide: bool = False, limit: int = 60) -> str:
    """Реплика пользователя для строки лога: hide (чат в режиме управления —
    ответы агенту/слоту сценария бывают паролями без признаков секрета) —
    только длина; иначе без секретов и обрезанная."""
    s = "" if text is None else str(text)
    if hide:
        return f"<{len(s)} chars>"
    red = redact_log(s)
    return "'" + (red[:limit] + ("…" if len(red) > limit else "")) + "'"


def control_mode_active(bot, *keys) -> bool:
    """Чат в режиме управления (чистая проверка, без побочных эффектов
    control_mode_on)."""
    modes = getattr(bot, "_control_mode", None) or ()
    return any(k is not None and str(k) in modes for k in keys)

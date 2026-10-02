"""
Приватность режима управления компьютером — единое место редактирования
секретов (аудит-лог, трассы сценариев, промпты) и правила «приватных»
страниц, данные которых не уходят облачным/веб-чат моделям.

Используется computer_control (аудит, vision/LLM-ярусы, строка открытой
страницы в system prompt), scenario_manager (трасса → облачное обобщение)
и scripts/scrub_cc_audit.py (редактирование уже записанных логов) — одни и
те же функции, чтобы правила не разъезжались.
"""

import json
import logging
import math
import os
import re
import threading
import time
import weakref
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# ── Маска ────────────────────────────────────────────────

# «***(12)»: длина оставлена — по аудиту видно, что ввод был и какого
# размера (отладка «ввёл пусто/обрезалось»), а содержимого нет
_MASK_RE = re.compile(r"^\*\*\*\(\d+\)$")


def mask(value) -> str:
    s = "" if value is None else str(value)
    if _MASK_RE.match(s):
        return s  # идемпотентно: повторная чистка не меняет длину
    return f"***({len(s)})"


def is_masked(value) -> bool:
    return bool(_MASK_RE.match(str(value or "")))


# ── Чувствительные поля ──────────────────────────────────

# Подпись/тип поля, ввод в которое — секрет или персональные данные.
# Границы слов — по буквам (\w в Python юникодный): «код» не ловит «кодекс»
_SENSITIVE_FIELD_RE = re.compile(
    r"(?<![^\W\d_])(?:"
    r"pass(?:word|wd|code|phrase)?|pwd|парол\w*|pin|пин(?:-?код)?|"
    r"code|код[аеуы]?|кодом|otp|2fa|mfa|totp|sms|смс|one[- ]?time|верификац\w*|"
    r"card|карт[аыуе]?|cvv2?|cvc2?|cc-?\w*|security code|expir\w*|срок действия|"
    r"e-?mail|почт\w*|mail|phone|tel|телефон\w*|mobile|моб\w*|"
    r"login|логин\w*|username|user ?name|пользовател\w*|account|аккаунт\w*|"
    r"passport|паспорт\w*|снилс|инн|ssn|secret|секрет\w*|token|токен\w*|"
    r"iban|bic|бик|счёт|счет"
    r")(?![^\W\d_])", re.IGNORECASE)


def is_sensitive_label(label) -> bool:
    return bool(_SENSITIVE_FIELD_RE.search(str(label or "")))


# ── Секретоподобные значения ─────────────────────────────

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Номер карты: 13–19 цифр подряд (пробелы/дефисы между группами допустимы)
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
# Телефон: +7 999 123-45-67 / 89991234567 — 10–13 цифр с разделителями
_PHONE_RE = re.compile(r"(?<![\w+])\+?\d[\d ()-]{8,16}\d(?!\d)")
# Длинный «случайный» токен без пробелов (ключи API, сессии, JWT)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-+/=.]{20,}")
# Известные префиксы ключей (GitHub, Slack, Stripe, OpenAI, Anthropic,
# AWS, Google, GitLab, HF): «ghp_AbCd…» иначе принимался за слаг из слов
_TOKEN_PREFIX_RE = re.compile(
    r"(?:gh[pousr]_|github_pat_|xox[abposr]-|sk-(?:proj|ant|live)-|"
    r"sk-(?=[A-Za-z0-9]{20})|[sr]k_(?:live|test)_|pk_live_|AKIA|ASIA|AIza|"
    r"glpat-|hf_)[A-Za-z0-9_\-]{16,}$")


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    freq: Dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in freq.values())


def _looks_token(s: str) -> bool:
    # Токен: и буквы, и цифры, высокая энтропия; не URL, не слаг из слов
    # через дефис (iphone-15-pro-max-256gb — это товар, а не ключ)
    if len(s) < 24 or "://" in s or "/" in s:
        return False
    if _TOKEN_PREFIX_RE.match(s):
        return True  # ghp_/sk-proj-/xoxb- — ключ, хоть и похож на слаг
    if not re.search(r"\d", s) or not re.search(r"[A-Za-z]", s):
        return False
    if re.search(r"[A-Za-z]{3,}[-_][A-Za-z]{3,}", s):
        return False
    return _entropy(s) >= 3.5


def _digits(s: str) -> int:
    return sum(ch.isdigit() for ch in s)


def looks_secret(value) -> bool:
    """Значение само по себе похоже на секрет/ПДн: email, номер карты,
    телефон, длинный случайный токен. Проверка всего значения."""
    s = str(value or "").strip()
    if not s or is_masked(s):
        return False
    if _EMAIL_RE.fullmatch(s):
        return True
    compact = re.sub(r"[ ()-]", "", s)
    if compact.lstrip("+").isdigit() and 10 <= len(compact.lstrip("+")) <= 19:
        return True  # телефон/карта/номер документа
    if " " not in s and _looks_token(s):
        return True
    return False


def _looks_pii(value) -> bool:
    # Email / телефон / номер карты целиком (без токенов — см. scrub_url)
    s = str(value or "").strip()
    if not s or is_masked(s):
        return False
    if _EMAIL_RE.fullmatch(s):
        return True
    compact = re.sub(r"[ ()-]", "", s).lstrip("+")
    return compact.isdigit() and 10 <= len(compact) <= 19


def redact_inline(text, limit: Optional[int] = None) -> str:
    """Свободный текст (подписи, ответы LLM, детали ошибок): вырезает
    встроенные email/карты/телефоны/токены и чистит URL внутри; limit —
    обрезка. Обычные слова не трогает."""
    s = "" if text is None else str(text)
    if not s:
        return s
    # URL внутри текста чистим по правилам URL, остальное — масками;
    # маски токенов по самим URL не гоняем (слаги/пути товаров не секрет)
    chunks = re.split(r"(https?://[^\s\"'<>«»]+)", s)
    for i, ch in enumerate(chunks):
        if i % 2:
            chunks[i] = scrub_url(ch)
            continue
        ch = _EMAIL_RE.sub(lambda m: mask(m.group(0)), ch)
        ch = _CARD_RE.sub(lambda m: mask(m.group(0))
                          if _digits(m.group(0)) >= 13 else m.group(0), ch)
        ch = _PHONE_RE.sub(lambda m: mask(m.group(0))
                           if 10 <= _digits(m.group(0)) <= 13
                           else m.group(0), ch)
        chunks[i] = _TOKEN_RE.sub(lambda m: mask(m.group(0))
                                  if _looks_token(m.group(0))
                                  else m.group(0), ch)
    s = "".join(chunks)
    if limit is not None and len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return s


def typed_is_sensitive(text, label=None, flagged: bool = False) -> bool:
    """Введённый текст — секрет: поле помечено чувствительным (флаг sn
    снапшота: password/email/tel/autocomplete), подпись поля говорит о
    пароле/коде/карте/почте/телефоне/логине, или значение само похоже на
    секрет."""
    return bool(flagged) or is_sensitive_label(label) or looks_secret(text)


def redact_typed(text, label=None, flagged: bool = False) -> str:
    if typed_is_sensitive(text, label, flagged):
        return mask(text)
    return redact_inline(text)


def looks_contact(value) -> bool:
    """Значение — контакт (email или телефон 10–12 цифр), а не пароль/карта/
    токен: в обычной реплике это не секрет («мой email …» в разговоре),
    секретом он становится, только когда уходит в поле действием."""
    s = str(value or "").strip()
    if not s or is_masked(s):
        return False
    if _EMAIL_RE.fullmatch(s):
        return True
    compact = re.sub(r"[ ()-]", "", s).lstrip("+")
    return compact.isdigit() and 10 <= len(compact) <= 12


def mask_values(text, values: Iterable[str]) -> str:
    """Известные секреты в тексте → маской, без учёта регистра, длинные
    первыми; только отдельным словом («1234» внутри «12345» не трогаем)."""
    s = "" if text is None else str(text)
    for v in sorted({str(v) for v in values or () if v}, key=len,
                    reverse=True):
        rx = re.compile(r"(?<!\w)" + re.escape(v) + r"(?!\w)", re.IGNORECASE)
        s = rx.sub(lambda _m, _v=v: mask(_v), s)
    return s


# ── Секрет в тексте команды ──────────────────────────────

# Секрет в САМОЙ команде («введи пароль Kotik2019 …», «логин ivan, пасс
# Kotik2019!»): такая фраза не уходит в облачный LLM-ярус разбора, а её
# значения — маской в историю/KnownSecrets. Слово секрета + (глагол ввода
# или значение с цифрой/похожее на секрет); «покажи код страницы» — не то.
# Единый список: computer_control (реэкспорт), bot_instance, фильтр логов
_CMD_SECRET_WORD_RE = re.compile(
    r"(?<![^\W\d_])(?:парол\w*|password\w*|passwd|passcode|pass|pswd|pwd|"
    r"pw|пасс|пассворд\w*|pin|пин(?:-?код\w*)?|cvv2?|cvc2?|"
    r"код(?:а|у|ом|е|ы)?|code|otp|2fa|смс-?код\w*|sms-?code|секретн\w*|"
    r"secret|токен\w*|token|credentials?|creds|креды|кредов|"
    r"данн\w*\s+(?:для\s+)?(?:входа|авторизации)|учётн\w*\s+данн\w*|"
    r"учетн\w*\s+данн\w*|login\s+details|sign[- ]?in\s+details)"
    r"(?![^\W\d_])", re.IGNORECASE)
_CMD_SECRET_VERB_RE = re.compile(
    r"(?<![^\W\d_])(?:введи|ввести|впиши|вписать|напиши|написать|набери|"
    r"набрать|вставь|вставить|вбей|вбить|заполни|заполнить|укажи|указать|"
    r"type|enter|fill|paste|input|put)(?![^\W\d_])", re.IGNORECASE)
# Пара «логин / пароль» без слова секрета («ivan / Kotik2019!»): слева
# логин (есть буква), справа значение с цифрой или спецсимволом. Пробелы
# вокруг «/» обязательны: «12/27», «site.ru/path» — не пара
_CMD_CRED_PAIR_RE = re.compile(
    r"(?<!\S)([^\s/«»\"']{2,64})\s+/\s+([^\s«»\"']{4,128})(?!\S)")
_CMD_TOKEN_RE = re.compile(r"[^\s«»\"'“”„`,;]+")
# Служебные слова вокруг секрета — не значение («пароль в поле …»)
_CMD_SECRET_FILLER = frozenset(
    "в во на поле поля форму форма мой моя мое моё мои это такой такая "
    "вот и а от для из к is my the to into in field for of from".split())


def _cred_pairs(s: str) -> List[str]:
    """Правые части пар «логин / секрет» в тексте (с краевой пунктуацией и
    без)."""
    out: List[str] = []
    for m in _CMD_CRED_PAIR_RE.finditer(s):
        left, right = m.group(1), m.group(2)
        if "://" in left or "://" in right:
            continue
        if not re.search(r"[^\W\d_]", left):
            continue  # «12 / 2027» — не логин
        if not (re.search(r"\d", right) or re.search(r"[^\w.\-]", right)):
            continue
        # И с краевой пунктуацией: «Kotik2019!» — пароль с «!» или конец фразы
        for val in (right.strip(".!?…:()"), right.rstrip(".,;")):
            if len(val) >= 3 and val not in out:
                out.append(val)
    return out


def command_has_secret(text: str) -> bool:
    """Команда содержит секрет пользователя (пароль/код/PIN …, пара
    «логин / пароль») — её текст нельзя отдавать облачному LLM-ярусу."""
    s = str(text or "")
    if _cred_pairs(s):
        return True
    if not _CMD_SECRET_WORD_RE.search(s):
        return False
    if _CMD_SECRET_VERB_RE.search(s):
        return True
    for tok in _CMD_TOKEN_RE.findall(s):
        tok = tok.strip(".!?…:()")
        if len(tok) >= 3 and (any(ch.isdigit() for ch in tok)
                              or looks_secret(tok)):
            return True
    return False


def command_secret_values(text: str) -> List[str]:
    """Вероятные значения секрета в команде — для маски истории: слова с
    цифрой/похожие на секрет, слово сразу после «пароль/код/…» и правая
    часть пары «логин / пароль»."""
    s = str(text or "")
    out: List[str] = list(_cred_pairs(s))
    if not _CMD_SECRET_WORD_RE.search(s):
        return out
    toks = [t.strip(".!?…:()") for t in _CMD_TOKEN_RE.findall(s)]
    for i, tok in enumerate(toks):
        if not tok or len(tok) < 3:
            continue
        val = any(ch.isdigit() for ch in tok) or looks_secret(tok)
        if not val and i and _CMD_SECRET_WORD_RE.fullmatch(toks[i - 1]):
            val = (tok.lower() not in _CMD_SECRET_FILLER
                   and not _CMD_SECRET_WORD_RE.fullmatch(tok)
                   and not _CMD_SECRET_VERB_RE.fullmatch(tok))
        if val and tok not in out:
            out.append(tok)
    return out


# ── Известные секреты чата ───────────────────────────────

# Сколько живёт значение, про которое уже известно, что это секрет:
# столько же, сколько прогон агента без ответа (RUN_TTL_SEC)
KNOWN_SECRET_TTL = 1800

# Все хранилища процесса (по одному на бота-персону): фильтр логов и аудит
# маскируют известные секреты любого чата — ключ чата у них и у истории
# может не совпасть (веб-чат без chat_id), а лишняя маска безвредна
_KS_REGISTRY: "weakref.WeakSet" = weakref.WeakSet()
_KS_REGISTRY_LOCK = threading.Lock()


def known_secret_values() -> List[str]:
    """Известные секреты всех чатов всех хранилищ процесса (без истёкших)."""
    with _KS_REGISTRY_LOCK:
        stores = list(_KS_REGISTRY)
    out: List[str] = []
    for ks in stores:
        try:
            out.extend(ks.all_values())
        except Exception:
            continue
    return list(dict.fromkeys(out))


class KnownSecrets:
    """Значения, про которые в чате уже известно, что это секрет: ответ на
    вопрос агента о пароле/коде, ввод в чувствительное поле, значение после
    «пароль …» в команде. Маски хода живут один ход, а агент вводит пароль,
    данный ответом, ходом позже — в поле без признаков секрета; здесь
    значение живёт ttl (с последней регистрации) или до конца прогона
    (purge), и им маскируются все следующие записи истории чата и поисковые
    запросы агента."""

    def __init__(self, ttl: float = KNOWN_SECRET_TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._by_chat: Dict[str, Dict[str, float]] = {}
        with _KS_REGISTRY_LOCK:
            _KS_REGISTRY.add(self)

    def all_values(self) -> List[str]:
        # Значения всех чатов (фильтр логов/аудит — см. known_secret_values)
        now = time.time()
        with self._lock:
            return [v for vals in self._by_chat.values()
                    for v, exp in vals.items() if exp > now]

    def add(self, chat, value) -> None:
        s = str(value or "").strip()
        # Короче 3 символов — это «да»/«ок», а не секрет; маска — не значение
        if len(s) < 3 or len(s) > 400 or is_masked(s):
            return
        with self._lock:
            self._by_chat.setdefault(str(chat), {})[s] = time.time() + self.ttl

    def values(self, chat) -> List[str]:
        now = time.time()
        with self._lock:
            vals = self._by_chat.get(str(chat))
            if not vals:
                return []
            for k in [k for k, exp in vals.items() if exp <= now]:
                vals.pop(k, None)
            if not vals:
                self._by_chat.pop(str(chat), None)
            return list(vals)

    def purge(self, chat) -> None:
        with self._lock:
            self._by_chat.pop(str(chat), None)


# ── URL ──────────────────────────────────────────────────

# Имена query-параметров, значения которых — секреты/ПДн. Сравнение по
# частям имени (access_token → access, token) + подстроки сильных маркеров
_SECRET_PARAM_PARTS = frozenset({
    "token", "code", "auth", "session", "sess", "sid", "key", "apikey",
    "sig", "signature", "password", "passwd", "pass", "pwd", "email", "mail",
    "phone", "tel", "state", "ticket", "otp", "secret", "jwt", "nonce",
    "hash", "login", "user", "username", "credential", "credentials",
    "csrf", "xsrf", "saml", "samlrequest", "samlresponse", "assertion",
    "refresh", "access", "id_token", "authuser", "card", "pin", "cvv",
})
_SECRET_PARAM_SUBSTR = ("token", "passw", "secret", "session", "signature",
                        "auth", "ticket", "otp", "csrf", "apikey", "api_key",
                        "credential", "email", "phone",
                        # PHPSESSID/jsessid, hmac-подпись, verification-код,
                        # magic-link, reset-ссылка
                        "sess", "hmac", "verif", "magic", "reset")
# Безвредные параметры, которые нужны для смысла URL (видео, плейлист,
# таймкод, поиск, страница) — не трогаем даже если значение длинное.
# «p» тут нет: это и номер страницы, и пароль (?p=…) — см. _AMBIGUOUS_PARAMS
_SAFE_PARAMS = frozenset({"v", "list", "t", "index", "q", "query", "text",
                          "search", "page", "lang", "hl", "tab",
                          "sort", "category", "utm_source", "utm_medium",
                          "utm_campaign", "start", "lr", "ref", "from"})
_KEEP_RAW_PARAMS = frozenset({"v", "list", "t", "index", "start"})
# id объекта: hex (md5-подобный) или UUID
_HEX_ID_RE = re.compile(r"[0-9a-fA-F]{16,64}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}"
                        r"-[0-9a-fA-F]{12}")
# Метки рекламных кликов: длинные и «случайные», но не секрет
_TRACKING_PARAMS = frozenset({"yclid", "ysclid", "gclid", "gbraid", "wbraid",
                              "dclid", "fbclid", "msclkid", "ttclid",
                              "twclid", "igshid", "mc_eid", "_openstat"})
# Однобуквенные «то страница, то секрет»: число оставляем (p=2 — страница,
# id поста, s=20 — метка шаринга), остальное — маской (s=<ключ шаринга/
# подписи>, lk=<ключ ссылки>)
_AMBIGUOUS_PARAMS = frozenset({"p", "pw", "s", "lk"})
# «sid» в любом месте имени — id сессии (gdsid, ssid, usid, sid2), но не
# «side»/«consider»/«president» (sid перед «e» — обычное слово)
_SID_RE = re.compile(r"sid(?!e)")


_JWT_RE = re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")
_SECRET_PATH_PREV_RE = re.compile(
    r"^(?:reset\w*|token|verify\w*|confirm\w*|activat\w*|magic\w*|auth\w*|"
    r"invite|invitation|unsubscribe|password\w*|otp|login|session|key|"
    # короткие «носители» ссылок-ключей: /r/<token>, /d/<id документа>,
    # /s/<share>, приглашения и шаринг
    r"r|d|s|share|shared|join)$")
# Сегмент-токен без «секретного» предыдущего: только длинный (≥32 —
# md5/sha/base64-ключи). id каналов/товаров (UC…, 24 симв.) короче
_PATH_TOKEN_MIN = 32


def _secret_param(name: str) -> bool:
    n = name.lower()
    if n in _SAFE_PARAMS:
        return False
    # accessToken / access_token / access-token → access, token
    split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
    parts = [p for p in re.split(r"[^a-z0-9]+", split) if p]
    if n in _SECRET_PARAM_PARTS or any(p in _SECRET_PARAM_PARTS
                                       for p in parts):
        return True
    return (any(sub in n for sub in _SECRET_PARAM_SUBSTR)
            or bool(_SID_RE.search(n)))


# Глубина разбора вложенных адресов (return_to=https%3A…%3Fnext%3D…)
_NEST_MAX = 3
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_NEST_BASE = "http://nested.invalid"


def _scrub_param_value(name: str, v: str, depth: int) -> str:
    """Значение несекретного по имени параметра: вложенный адрес — теми же
    правилами (рекурсивно), ПДн/JWT/токен/ключ ссылки — маской."""
    n = name.lower()
    if not v or is_masked(v) or n in _KEEP_RAW_PARAMS:
        return v
    if depth < _NEST_MAX:
        # Адрес в значении (parse_qsl уже раскодировал %3A%2F%2F):
        # абсолютный, «//host/…» или путь «/reset?token=…»
        if _SCHEME_RE.match(v):
            return scrub_url(v, _depth=depth + 1)
        if v.startswith("//"):
            return scrub_url("http:" + v, _depth=depth + 1)[len("http:"):]
        if v.startswith("/"):
            out = scrub_url(_NEST_BASE + v, _depth=depth + 1)
            return out[len(_NEST_BASE):] or "/"
    if _looks_pii(v):
        return mask(v)
    if n in _AMBIGUOUS_PARAMS and not v.isdigit():
        return mask(v)
    # JWT/ключ где угодно в значении, длинный случайный токен целиком —
    # под любым «безобидным» именем (data=, state_blob=, h=). Метки
    # рекламных кликов (yclid/gclid/fbclid) — не секрет, их не трогаем
    if _JWT_RE.search(v):
        return mask(v)
    # Hex/UUID под обычным именем — id объекта (variation=<32 hex> товара
    # Додо, studentId=<uuid>): по нему сценарий открывает ту же страницу
    if (n not in _TRACKING_PARAMS and not re.search(r"\s", v)
            and not _HEX_ID_RE.fullmatch(v) and _looks_token(v)):
        return mask(v)
    # Однобуквенное имя (k=, c=, h=, u=) и значение-ключ из букв и цифр
    if (len(n) == 1 and n not in _SAFE_PARAMS and len(v) >= 8
            and re.fullmatch(r"[A-Za-z0-9_\-]+", v)
            and re.search(r"\d", v) and re.search(r"[A-Za-z]", v)):
        return mask(v)
    return v


def scrub_url(url, _depth: int = 0) -> str:
    """URL для лога/промпта: без фрагмента (#access_token=…), без userinfo,
    без секретных query-параметров; сегменты пути, похожие на токен
    (/reset/<token>), маскируются; вложенные адреса в значениях параметров
    чистятся рекурсивно. Не-URL возвращается как есть."""
    s = "" if url is None else str(url)
    if not _SCHEME_RE.match(s):
        return s
    try:
        parts = urlsplit(s)
    except Exception:
        return s
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    # Сегмент пути маскируем, только если он явно секрет: JWT, email, токен
    # после «секретного» сегмента (/reset/<token>, /verify/<code>, /r/<key>)
    # или длинный случайный токен где угодно. ID каналов/товаров не трогаем —
    # сценарий по такому URL должен открыться
    segs = parts.path.split("/")
    for i, seg in enumerate(segs):
        prev = segs[i - 1].lower() if i else ""
        if not seg or is_masked(seg):
            continue
        if (_JWT_RE.fullmatch(seg) or _EMAIL_RE.fullmatch(unquote(seg))
                or (len(seg) >= _PATH_TOKEN_MIN and _looks_token(seg))
                # t.me/+<инвайт>: ключ приглашения в закрытый чат
                or (seg.startswith("+") and len(seg) >= 10
                    and re.search(r"\d", seg) and re.search(r"[A-Za-z]", seg))
                or (_SECRET_PATH_PREV_RE.search(prev) and len(seg) >= 12
                    and re.search(r"\d", seg) and re.search(r"[A-Za-z]", seg)
                    and not re.search(r"[A-Za-z]{3,}[-_][A-Za-z]{3,}", seg))):
            segs[i] = mask(seg)
    path = "/".join(segs)
    query = parts.query
    if query:
        try:
            pairs = parse_qsl(query, keep_blank_values=True)
        except Exception:
            pairs = []
        # Секретное имя — параметр убираем; в обычном — вложенный адрес
        # чистим рекурсивно, ПДн/JWT/токены маскируем (_scrub_param_value).
        # Числовые id без секретного имени (yclid, variation) не трогаем: по
        # URL сценарий должен открыться
        kept = [(k, _scrub_param_value(k, v, _depth))
                for k, v in pairs if not _secret_param(k)]
        query = urlencode(kept, doseq=True, safe="*()")
    return urlunsplit((parts.scheme, netloc, path, query, ""))


# ── Приватные страницы ───────────────────────────────────

# Встроенные признаки страниц входа/оплаты/госуслуг/почты/мессенджеров/
# корзины: на них скриншоты и текст страницы облачным и веб-чат моделям не
# уходят (vision-ярусы пропускаются, LLM-выбор — только локальной моделью
# или по скорингу). Матч — по ЦЕЛЫМ частям хоста и сегментам пути, а не
# подстрокой: «author.today», «espresso.ru», «/author/…» — обычные сайты
#
# Часть хоста (кроме TLD) целиком — приватная
_PRIVATE_HOST_LABELS = frozenset({
    "auth", "login", "signin", "sso", "oauth", "passport", "bank", "pay",
    "payment", "payments", "checkout", "billing", "wallet", "secure",
    "gosuslugi", "esia"})
# Часть хоста содержит — банки/госуслуги («online.sberbank.ru», «alfabank»)
_PRIVATE_LABEL_SUBSTR = ("bank", "gosuslugi")
# Первая часть хоста: id.vk.com, accounts.google.com, mail.yandex.ru, lk.…
_PRIVATE_FIRST_LABELS = frozenset({
    "id", "accounts", "account", "login", "auth", "sso", "lk", "my",
    "mail", "webmail", "passport"})
# Часть хоста начинается с — платёжные сервисы (paypal, payu, paymaster)
_PRIVATE_LABEL_PREFIX = ("pay",)
# Хосты целиком (и их поддомены): почта, мессенджеры, банки, кошельки
_PRIVATE_HOSTS = frozenset({
    "mail.google.com", "e.mail.ru", "octavius.mail.ru", "touch.mail.ru",
    "outlook.live.com", "outlook.office.com", "outlook.office365.com",
    "web.telegram.org", "web.whatsapp.com", "messenger.com",
    "tinkoff.ru", "tbank.ru", "vtb.ru", "qiwi.com", "yoomoney.ru",
    "paypal.com", "nalog.gov.ru", "nalog.ru"})
# Сегменты пути страницы входа/оплаты/корзины/кабинета на обычном хосте
# (example.com/login). Сегмент — до точки: «login.php» → «login»
_PRIVATE_PATH_SEGS = frozenset({
    "login", "log-in", "logon", "signin", "sign-in", "signup", "sign-up",
    "register", "auth", "authorize", "authentication", "authenticate",
    "oauth", "oauth2", "sso", "saml", "checkout", "payment", "payments",
    "pay", "billing", "cart", "basket", "2fa", "mfa", "otp", "verify",
    "password", "passwords", "reset-password", "forgot-password",
    "password-reset", "lk", "wallet",
    # кабинет/профиль (WooCommerce /my-account/, /account, /profile) и
    # ключи API (platform.openai.com/api-keys, dashboard.stripe.com/apikeys)
    "account", "my-account", "myaccount", "profile", "personal",
    "api-keys", "apikeys", "api-tokens", "access-tokens"})
# Подстрока сегмента: /gocheckout, /onepagecheckout, /usersignin
_PRIVATE_SEG_SUBSTR = ("checkout", "signin")
# Пары сегментов (и хост + первый сегмент): настройки безопасности,
# ключи/токены, переписка
_PRIVATE_PATH_PAIRS = frozenset({
    ("account", "security"), ("settings", "security"),
    ("settings", "tokens"), ("settings", "keys"),
    ("settings", "applications"), ("settings", "password")})
_PRIVATE_HOST_PATHS = {
    "vk.com": ("im", "mail"), "facebook.com": ("messages",),
    "instagram.com": ("direct",), "x.com": ("messages",),
    "twitter.com": ("messages",), "linkedin.com": ("messaging",),
    "ok.ru": ("messages",), "discord.com": ("channels",),
}


def _host_of(host_or_url) -> Tuple[str, str]:
    s = str(host_or_url or "").strip()
    if "://" in s:
        try:
            p = urlsplit(s)
            return (p.hostname or "").lower(), p.path or ""
        except Exception:
            return "", ""
    host, _, rest = s.partition("/")
    return host.split(":")[0].lower(), ("/" + rest) if rest else ""


def is_private_page(host_or_url, extra_hosts: Iterable[str] = (),
                    builtin: bool = True) -> bool:
    """Страница приватная: хост из private_hosts конфига (сам домен или
    поддомен) или, при builtin, встроенные признаки входа/оплаты в хосте
    или пути."""
    host, path = _host_of(host_or_url)
    if not host:
        return False
    for d in extra_hosts or ():
        d = str(d or "").strip().lower().lstrip(".")
        if d and (host == d or host.endswith("." + d)):
            return True
    if not builtin:
        return False
    # Маршрут SPA/CMS бывает не в пути: #/checkout, #!/signin,
    # ?route=checkout/checkout (OpenCart), ?page=login — проверяем и его
    return any(_builtin_private(host, p)
               for p in [path] + _route_paths(host_or_url))


# Query-параметры, в которых CMS/SPA держат маршрут страницы
_ROUTE_PARAMS = frozenset({"route", "page", "r", "path", "view", "action",
                           "controller", "module", "mod", "act", "do",
                           "pagename", "section", "screen", "step"})


def _route_paths(host_or_url) -> List[str]:
    """Маршруты страницы вне пути: фрагмент («#/checkout», «#!/signin») и
    значения маршрутных query-параметров — как пути для _builtin_private."""
    s = str(host_or_url or "").strip()
    if "?" not in s and "#" not in s:
        return []
    try:
        p = urlsplit(s if "://" in s else "http://" + s)
    except Exception:
        return []
    out: List[str] = []
    frag = (p.fragment or "").lstrip("!")
    if frag:
        out.append("/" + frag.lstrip("/"))
    try:
        pairs = parse_qsl(p.query or "", keep_blank_values=False)
    except Exception:
        pairs = []
    for k, v in pairs:
        if k.lower() in _ROUTE_PARAMS and v and not v.isdigit():
            out.append("/" + v.lstrip("/"))
    return out


def page_candidates(action) -> List[str]:
    """Адреса страницы действия/записи аудита для проверки приватности:
    полные URL (value/url — приватность бывает по пути: vk.com/im, */login)
    и хост; у multi — и вложенных действий. value не-URL (запрос, клавиша)
    не берём: «pay bills» не хост."""
    out: List[str] = []
    if not isinstance(action, dict):
        return out
    items = [action]
    if action.get("kind") == "multi" and isinstance(action.get("items"), list):
        items += [a for a in action["items"] if isinstance(a, dict)]
    for a in items:
        for f in _URL_FIELDS:
            v = a.get(f)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                out.append(v)
        if a.get("host"):
            out.append(str(a["host"]))
    return list(dict.fromkeys(out))


def is_private_any(candidates: Iterable, extra_hosts: Iterable[str] = (),
                   builtin: bool = True) -> bool:
    """Хоть один адрес приватный — консервативно (URL и хост одной страницы
    проверяются оба: хост обычный, а путь — переписка/вход)."""
    return any(is_private_page(c, extra_hosts, builtin)
               for c in candidates or () if c)


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _builtin_private(host: str, path: str) -> bool:
    if any(_under(host, d) for d in _PRIVATE_HOSTS):
        return True
    labels = host.split(".")
    # TLD не смотрим (.id — Индонезия, .pay и т.п.)
    inner = labels[:-1] if len(labels) > 1 else labels
    if inner and inner[0] in _PRIVATE_FIRST_LABELS and len(labels) > 2:
        return True
    for lab in inner:
        if lab in _PRIVATE_HOST_LABELS or lab.startswith(_PRIVATE_LABEL_PREFIX):
            return True
        if any(sub in lab for sub in _PRIVATE_LABEL_SUBSTR):
            return True
    segs = [s.split(".", 1)[0].lower()
            for s in (path or "").split("?", 1)[0].split("#", 1)[0].split("/")]
    segs = [s for s in segs if s]
    if any(s in _PRIVATE_PATH_SEGS for s in segs):
        return True
    if any(sub in s for s in segs for sub in _PRIVATE_SEG_SUBSTR):
        return True
    if any(p in _PRIVATE_PATH_PAIRS for p in zip(segs, segs[1:])):
        return True
    base = host[4:] if host.startswith(("www.", "web.")) else host
    first = segs[0] if segs else ""
    for d, heads in _PRIVATE_HOST_PATHS.items():
        if _under(base, d) and first in heads:
            return True
    return False


class PrivateRouter:
    """Обёртка роутера на приватной странице: текстовые LLM-вызовы — только
    локальной модели (Ollama; веб-чат-движок локального роутера не
    годится — это тоже сторонний сайт), vision — никогда. Нет локальной —
    None: вызывающий код откатывается на скоринг или честный отказ.
    Остальные атрибуты — от исходного роутера."""

    cc_provider = None
    vision_provider = None

    def __init__(self, router):
        self._router = router

    def __getattr__(self, name):
        return getattr(self._router, name)

    @staticmethod
    def supports_vision() -> bool:
        return False

    @staticmethod
    def get_response_with_image(*args, **kwargs):
        return None

    def get_response(self, messages, temperature: float = 0.0,
                     max_tokens: int = 100, top_p: float = 0.9,
                     timeout: float = 60.0, **_kw) -> Optional[str]:
        try:
            from app.core.local_router import get_local_router
            local = get_local_router()
            base = getattr(local, "_base", local)
            backend, _sites = base._resolve_task(None, None)
            if backend != "ollama" or not local.is_available():
                return None
            return local.get_response(messages, temperature=temperature,
                                      max_tokens=max_tokens, top_p=top_p,
                                      timeout=timeout)
        except Exception as e:
            logger.debug(f"[CCPrivacy] локальная модель недоступна: {e}")
            return None


def private_router(router, host_or_url, extra_hosts: Iterable[str] = (),
                   builtin: bool = True):
    """router как есть либо PrivateRouter для приватной страницы.
    host_or_url — адрес или список адресов (приватен хоть один — обёртка)."""
    if router is None or isinstance(router, PrivateRouter):
        return router
    cands = (host_or_url if isinstance(host_or_url, (list, tuple))
             else [host_or_url])
    if is_private_any(cands, extra_hosts, builtin):
        return PrivateRouter(router)
    return router


# ── Запись аудита ────────────────────────────────────────

_TEXT_LIMITS = {"detail": 300, "element": 80, "llm_response": 200,
                "value": 300, "url": 300, "fail_reason": 40}
_CANDIDATES_MAX = 10
_URL_FIELDS = ("value", "url")

# Тело команды ввода «ТЕКСТ в поле ПОЛЕ» / «TEXT into FIELD» (крайний
# сепаратор, как в resolve_type): текст — секрет, название поля — нет
_TYPE_BODY_SEP_RE = re.compile(
    r"\s+(?:в\s+поле|в\s+форму|в\s+поиск\w*|into|in\s+field)(?:\s+|$)",
    re.IGNORECASE)
# detail чтения страницы в аудите — только длина (идемпотентно)
_READ_DETAIL_RE = re.compile(r"^read:\d+ chars$")
# Убранный заголовок name_check приватной страницы (идемпотентно)
_NC_TITLE_DROPPED_RE = re.compile(r"^<\d+ chars>$")


def redact_type_body(body) -> str:
    """«Kotik2019! в поле пароль» → «***(10) в поле пароль»: вводимый текст
    маской, название поля как есть. Нет сепаратора — маской всё тело.
    Идемпотентна."""
    s = "" if body is None else str(body)
    if not s or is_masked(s):
        return s
    seps = [m for m in _TYPE_BODY_SEP_RE.finditer(s) if s[:m.start()].strip()]
    if not seps:
        return mask(s)
    m = seps[-1]
    head = s[:m.start()].strip()
    return (head if is_masked(head) else mask(head)) + s[m.start():]


def read_detail(text) -> str:
    # Прочитанный текст страницы (переписка, почта) в аудит не пишем
    s = "" if text is None else str(text)
    return s if _READ_DETAIL_RE.match(s) else f"read:{len(s)} chars"


def contains_value(text, values: Iterable[str]) -> bool:
    """В тексте есть одно из значений (отдельным словом, без регистра)."""
    s = str(text or "")
    if not s:
        return False
    low = s.lower()
    for v in values or ():
        v = str(v or "")
        if v and v.lower() in low and re.search(
                r"(?<!\w)" + re.escape(v) + r"(?!\w)", s, re.IGNORECASE):
            return True
    return False


def redact_audit_record(rec: dict, extra_hosts: Iterable[str] = (),
                        builtin: bool = True,
                        known: Iterable[str] = ()) -> Tuple[dict, Dict[str, int]]:
    """Запись аудита без секретов: маска введённого текста (чувствительные
    поля и секретоподобные значения, ЛЮБОЙ ввод на приватной странице —
    переписка/банк/вход — и ввод с известным секретом чата known), чистка
    URL, обрезка и чистка текстов страницы (кандидаты, ответ LLM, detail),
    известные секреты — маской во всех текстовых полях. Идемпотентна.
    → (новая запись, {поле: сколько раз изменено})."""
    out = dict(rec)
    changed: Dict[str, int] = {}
    known = [str(v) for v in known or () if v]

    def _set(field: str, new) -> None:
        if new != out.get(field):
            changed[field] = changed.get(field, 0) + 1
            out[field] = new

    kind = out.get("kind")
    label = out.get("element")
    if out.get("text") is not None:
        flagged = bool(out.get("field_sensitive"))
        if kind == "type" and typed_is_sensitive(out["text"], label, flagged):
            if not out.get("field_sensitive"):
                out["field_sensitive"] = True
            _set("text", mask(out["text"]))
        elif kind == "type" and (
                contains_value(out["text"], known)
                or is_private_any(page_candidates(out), extra_hosts, builtin)):
            # Подпись поля обычная («Сообщение»), но страница приватная или
            # в тексте пароль, данный ответом, — маской, какой бы ни была
            # подпись. Флаг поля не ставим: поле само не секретное
            _set("text", mask(out["text"]))
        else:
            _set("text", redact_inline(out["text"], 80))
    v = out.get("value")
    if kind == "resolve_fail" and isinstance(v, str) and v \
            and not v.startswith(("http://", "https://")):
        # Неудача ввода: value — тело команды «ТЕКСТ в поле ПОЛЕ» (no_fields)
        # — текст маской, название поля остаётся
        if out.get("fail_reason") == "no_fields" or re.search(
                r"\s(?:в\s+поле|в\s+форму|in\s+field)\s", v, re.IGNORECASE):
            _set("value", redact_type_body(v))
    if kind == "read" and out.get("ok") and isinstance(out.get("detail"), str) \
            and out["detail"]:
        # detail успешного чтения — прочитанный текст страницы: только длина
        _set("detail", read_detail(out["detail"]))
    for f in _URL_FIELDS:
        v = out.get(f)
        if isinstance(v, str) and v:
            if re.match(r"^https?://\S+$", v):
                # URL не режем по длине: по нему сценарий открывает страницу
                _set(f, scrub_url(v))
            else:
                _set(f, redact_inline(v, _TEXT_LIMITS[f]))
    for f in ("detail", "element", "llm_response"):
        v = out.get(f)
        if isinstance(v, str) and v:
            _set(f, redact_inline(v, _TEXT_LIMITS[f]))
    cands = out.get("candidates")
    if isinstance(cands, list):
        new_c = []
        for c in cands[:_CANDIDATES_MAX]:
            if isinstance(c, dict):
                c = dict(c)
                if isinstance(c.get("text"), str):
                    c["text"] = redact_inline(c["text"], 60)
            new_c.append(c)
        _set("candidates", new_c)
    tiers = out.get("tiers")
    if isinstance(tiers, list):
        new_t = []
        for t in tiers:
            if isinstance(t, dict) and isinstance(t.get("resp"), str):
                t = dict(t)
                t["resp"] = redact_inline(t["resp"], 60)
            new_t.append(t)
        _set("tiers", new_t)
    for f in ("overlays", "vetoed"):
        v = out.get(f)
        if isinstance(v, list):
            _set(f, [redact_inline(x, 60) if isinstance(x, str) else x
                     for x in v])
    nc = out.get("name_check")
    if isinstance(nc, dict) and isinstance(nc.get("title"), str) \
            and nc["title"] and not _NC_TITLE_DROPPED_RE.match(nc["title"]):
        # Заголовок открытой страницы (почта: «Входящие — ivan@…»): на
        # приватной — только длина, иначе без ПДн и известных секретов
        t = nc["title"]
        if is_private_any(page_candidates(out), extra_hosts, builtin):
            t = f"<{len(t)} chars>"
        else:
            t = redact_inline(mask_values(t, known), 80)
        if t != nc["title"]:
            _set("name_check", dict(nc, title=t))
    if known:
        # Известный секрет чата — маской в любом текстовом поле (describe
        # multi в value, ошибка с введённым текстом в detail, подпись)
        for f in ("value", "detail", "element", "llm_response", "product"):
            v = out.get(f)
            if isinstance(v, str) and v and contains_value(v, known):
                _set(f, mask_values(v, known))
        cands = out.get("candidates")
        if isinstance(cands, list) and any(
                isinstance(c, dict) and contains_value(c.get("text"), known)
                for c in cands):
            _set("candidates", [
                dict(c, text=mask_values(c.get("text"), known))
                if isinstance(c, dict) and isinstance(c.get("text"), str)
                else c for c in cands])
    return out, changed


# Ротация по размеру: audit.jsonl + .1 + .2 (по 10 МБ) — лог не растёт
# бесконечно, а хвост для сценариев всегда в текущем файле
AUDIT_MAX_BYTES = 10 * 1024 * 1024
AUDIT_BACKUPS = 2
_audit_io_lock = threading.Lock()
# Межпроцессный лок записи/ротации (бот ↔ scripts/scrub_cc_audit --apply):
# flock на скрытом файле рядом — один на audit.jsonl и его .1/.2
AUDIT_LOCK_TIMEOUT = 10.0


def audit_lock_path(path: Path) -> Path:
    path = Path(path)
    base = re.sub(r"\.\d+$", "", path.name)
    return path.with_name(f".{base}.lock")


class audit_file_lock:
    """with audit_file_lock(path) as locked: — flock (POSIX) на lock-файле
    аудита. timeout=None — ждать сколько нужно. Нет fcntl (Windows) или
    не дождались — locked=False: вызывающий решает сам (запись всё равно
    пишет, чистка отказывается)."""

    def __init__(self, path: Path, timeout: Optional[float] = AUDIT_LOCK_TIMEOUT):
        self.path = audit_lock_path(path)
        self.timeout = timeout
        self._fd = None
        self.locked = False

    def __enter__(self) -> bool:
        try:
            import fcntl
        except ImportError:
            return False
        try:
            self._fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            logger.debug(f"[CCPrivacy] lock-файл аудита не открыт: {e}")
            return False
        deadline = (None if self.timeout is None
                    else time.monotonic() + self.timeout)
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.locked = True
                return True
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            try:
                if self.locked:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
                self.locked = False


def _rotate(path: Path, backups: int) -> None:
    for i in range(backups, 0, -1):
        src = path.with_name(f"{path.name}.{i - 1}") if i > 1 else path
        dst = path.with_name(f"{path.name}.{i}")
        if src.exists():
            os.replace(src, dst)


def audit_append(path: Path, record: dict,
                 max_bytes: int = AUDIT_MAX_BYTES,
                 backups: int = AUDIT_BACKUPS) -> None:
    """Дописать запись (уже отредактированную) в jsonl с ротацией."""
    line = json.dumps(record, ensure_ascii=False) + "\n"
    path = Path(path)
    # Лок процесса + межпроцессный (чистка --apply не теряет строки и не
    # затирает ротированный файл). Не дождались — пишем всё равно: запись
    # аудита важнее, а чистка сама проверит, что файл не менялся
    with _audit_io_lock, audit_file_lock(path):
        try:
            if path.stat().st_size + len(line.encode("utf-8")) > max_bytes:
                _rotate(path, backups)
        except FileNotFoundError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


def _tail_one(path: Path, n: int, block: int) -> List[str]:
    # Хвост одного файла: блоки с конца, пока не набралось n строк
    if n <= 0:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            buf = b""
            while pos > 0 and buf.count(b"\n") <= n:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.debug(f"[CCPrivacy] хвост {path.name} не прочитан: {e}")
        return []
    raw = buf.decode("utf-8", errors="replace").splitlines()
    if pos > 0:
        raw = raw[1:]  # первая строка блока может быть обрезана
    return [ln for ln in raw if ln.strip()][-n:]


def _audit_rewrite(path: Path, keep) -> List[str]:
    """Переписать один файл аудита, оставив строки, для которых keep(line)
    истинно, → выброшенные строки. Атомарно (tmp + os.replace, права
    файла сохраняются). Вызывать под локами записи аудита."""
    try:
        mode = os.stat(path).st_mode & 0o777
        lines = path.read_text(encoding="utf-8",
                               errors="replace").splitlines(keepends=True)
    except FileNotFoundError:
        return []
    kept, dropped = [], []
    for line in lines:
        if not line.strip():
            continue
        (kept if keep(line) else dropped).append(line)
    if not dropped:
        return []
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as out:
        out.writelines(ln if ln.endswith("\n") else ln + "\n" for ln in kept)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return [ln.rstrip("\n") for ln in dropped]


def _audit_chat_of(line: str) -> Optional[str]:
    try:
        rec = json.loads(line)
    except Exception:
        return None
    return str(rec.get("chat_id")) if isinstance(rec, dict) else None


def audit_pop_chat(path: Path, chat_id,
                   backups: int = AUDIT_BACKUPS) -> List[str]:
    """Убрать из аудита (текущий файл и ротации .1…) все записи чата →
    убранные строки, от старых к новым (для корзины очистки диалога).
    Под теми же локами, что audit_append: запись в середине прохода ждёт,
    строки не теряются. Межпроцессный лок не взят (Windows, чистка скриптом)
    — не трогаем файл: половина переписанного аудита хуже, чем ничего."""
    path = Path(path)
    ck = str(chat_id)
    out: List[str] = []
    with _audit_io_lock, audit_file_lock(path) as locked:
        # На Windows flock нет (locked=False всегда) — хватает лока процесса
        if not locked and os.name != "nt":
            raise TimeoutError(f"{path.name}: аудит занят — не очищен")
        for i in range(backups, -1, -1):
            p = path if i == 0 else path.with_name(f"{path.name}.{i}")
            out += _audit_rewrite(p, lambda ln: _audit_chat_of(ln) != ck)
    return out


def audit_restore_lines(path: Path, lines: Iterable[str]) -> int:
    """Вернуть строки аудита (отмена очистки диалога) в текущий файл,
    слияние по ts — хвост для сценариев остаётся хронологическим. → сколько
    строк вернули."""
    path = Path(path)
    add = [str(ln).rstrip("\n") for ln in lines if str(ln).strip()]
    if not add:
        return 0

    def _ts(line: str) -> float:
        try:
            return float(json.loads(line).get("ts") or 0)
        except Exception:
            return 0.0
    with _audit_io_lock, audit_file_lock(path):
        try:
            mode = os.stat(path).st_mode & 0o777
            cur = [ln.rstrip("\n") for ln in path.read_text(
                encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        except FileNotFoundError:
            mode, cur = 0o600, []
        merged = sorted(cur + add, key=_ts)  # sorted стабилен: порядок равных ts цел
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        with open(tmp, "w", encoding="utf-8") as out:
            out.writelines(ln + "\n" for ln in merged)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    return len(add)


def read_tail_lines(path: Path, n: int, block: int = 64 * 1024) -> List[str]:
    """Последние n строк jsonl чтением с конца — без чтения всего лога.
    Сразу после ротации текущий файл короткий — добираем из .1."""
    path = Path(path)
    tail = _tail_one(path, n, block)
    if len(tail) < n:
        tail = _tail_one(path.with_name(f"{path.name}.1"),
                         n - len(tail), block) + tail
    return tail

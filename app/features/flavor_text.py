"""Короткие реплики в характере персоны о результатах команд управления
компьютером (CC) — «системные сообщения», которым не нужны история диалога,
LTM, погода и прочий контекст.

Живой вызов идёт ТОЛЬКО в Google AI Mode (webchat:google, stateless-канал
«cc»: каждый вызов — свежий чат без прошлого контекста; лок инстанса ждётся
не дольше _LIVE_LOCK_TIMEOUT_SEC, пока идёт фоновая генерация банка — живой
вызов пропускается в пользу шаблонов). Генерация/пополнение банка — канал
«cc_gen»: отдельный инстанс со своим локом, чтобы не блокировать живые
реплики. Основной путь — банк заранее сгенерированных фраз
(data/{context}/flavor_bank.json) с плейсхолдерами ({host}, {element},
{text}, {detail}); последний фоллбек — честный шаблон caller'а
(describe_done и т.п.).

Ошибки тоже проходят через flavor, но с жёстким требованием: суть ошибки
сохраняется ({detail} в банковских фразах, инструкция в живом промпте) —
причину не скрывать и не приукрашивать.

Напоминания сюда НЕ входят — они всегда генерируются LLM намеренно.

Банк генерируется при создании персоны (settings_api.create_persona) и при
старте бота, если хэш system_prompt изменился (BotInstance.__init__);
дополнительно пополняется новыми вариантами, когда банк старше
FLAVOR_BANK_REFRESH_DAYS и пользователь неактивен (тик проактив-модуля).
"""

import hashlib
import json
import logging
import random
import re
import threading
import time
from pathlib import Path
from typing import Optional

from app.core.atomic_io import atomic_write_json, load_json_safe

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2  # +секция phrases (служебные фразы голосом персоны)
FLAVOR_BANK_REFRESH_DAYS = 3
_OK_PER_KIND = 6    # вариантов успеха просим у генератора
_ERR_PER_KIND = 4   # вариантов ошибки
_TOPUP_OK = 3       # вариантов на пополнение
_TOPUP_ERR = 2
_MAX_PER_BUCKET = 15  # потолок фраз на kind/bucket при пополнении
_LIVE_MAX_CHARS = 400
_LIVE_LOCK_TIMEOUT_SEC = 5.0  # живой flavor не ждёт занятый инстанс дольше

# Типы команд банка (CC action kinds сводятся к ним — см. _map_kind)
_BANK_KINDS = ("open", "click", "type", "send", "key", "scroll",
               "tab_switch", "download", "hover", "slider", "generic")

_KIND_MAP = {
    "url": "open", "nav": "open",
    "cart": "click",
    "press": "key", "media_vol": "key",
    "scroll_stop": "scroll",
    "multi": "generic",
}

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

# Служебные фразы режима управления/сценариев — тоже голосом персоны (банк,
# секция "phrases"). spec — обязательный смысл для генератора (команды
# в «кавычках» передаются пользователю дословно); ph — допустимые
# плейсхолдеры; req — обязательные (без них фраза теряет смысл)
_PHRASE_KEYS = {
    "cc_mode_on": {"ph": set(), "req": set(), "spec":
        "режим управления компьютером включён: работают команды «открой …», "
        "«нажми …», «введи …» и сценарии; на время режима молчат напоминания, "
        "список дел, инвентарь и обучение; выход — сказать «выйди из режима "
        "управления»"},
    "cc_mode_off": {"ph": set(), "req": set(), "spec":
        "режим управления выключен: браузером больше не управляю; "
        "напоминания, список дел, инвентарь и обучение снова работают"},
    "cc_mode_already_on": {"ph": set(), "req": set(), "spec":
        "режим управления уже был включён; выход — «выйди из режима "
        "управления»"},
    "cc_mode_already_off": {"ph": set(), "req": set(), "spec":
        "режим управления уже был выключен"},
    "cc_mode_disabled": {"ph": set(), "req": set(), "spec":
        "управление компьютером выключено в настройках бота; включить его "
        "можно в досье, раздел «Инструменты»"},
    "scenario_record_start": {"ph": set(), "req": set(), "spec":
        "началась запись сценария: действия («открой …», «нажми …», "
        "«введи …») пойдут в запись; закончить — «сохрани сценарий» (можно "
        "сразу с названием); отменить — «отмени запись»"},
    "scenario_record_already": {"ph": {"since"}, "req": {"since"}, "spec":
        "запись сценария уже идёт (со временем {since}); закончить — "
        "«сохрани сценарий», отменить — «отмени запись»"},
    "scenario_record_cancel": {"ph": set(), "req": set(), "spec":
        "запись сценария отменена, ничего не сохранено"},
    "scenario_record_cancel_none": {"ph": set(), "req": set(), "spec":
        "записи сценария не было — нечего отменять"},
    "scenario_save_ask_name": {"ph": set(), "req": set(), "spec":
        "бот спрашивает, как назвать сценарий; формат для пользователя: "
        "«сохрани сценарий заказ пиццы»"},
    "scenario_saved": {"ph": {"name", "steps"}, "req": {"name"}, "spec":
        "сценарий «{name}» записан, в нём {steps} шагов; запуск — просто "
        "сказать «{name}»"},
    "scenario_not_found": {"ph": {"name"}, "req": {"name"}, "spec":
        "сценария «{name}» у бота нет"},
    "scenario_started": {"ph": {"name", "steps"}, "req": {"name"}, "spec":
        "запускаю сценарий «{name}» ({steps} шагов); отменить — сказать "
        "«отмена»"},
    "scenario_stuck": {"ph": set(), "req": set(), "spec":
        "сценарий стоит на сбойном шаге; варианты для пользователя: "
        "«повтори», «дальше» (пропустить шаг) или «отмена»"},
    "scenario_run_cancel": {"ph": {"name"}, "req": {"name"}, "spec":
        "сценарий «{name}» отменён"},
    "scenario_run_cancel_none": {"ph": set(), "req": set(), "spec":
        "нечего отменять — сценарий не запущен"},
    "scenario_offer": {"ph": set(), "req": set(), "spec":
        "бот предлагает запомнить только что пройденный сюжет как сценарий, "
        "чтобы в следующий раз бот прошёл его сам; формат для пользователя: "
        "«запомни сценарий …» и название"},
}

_BANK_LOCK = threading.Lock()
_GEN_LOCK = threading.Lock()       # одна генерация/пополнение на процесс
_GEN_STARTED: set = set()          # context → фоновая генерация уже идёт

# Кэш webchat-инстансов google по контексту (канал «cc» — stateless)
_WC: dict = {}
_WC_LOCK = threading.Lock()


# ── Банк: файл ─────────────────────────────────────────────

def _bank_path(context: str) -> Path:
    return Path(f"data/{context}/flavor_bank.json")


def _load_bank(context: str) -> dict:
    bank = load_json_safe(_bank_path(context), default={}, label="Flavor")
    return bank if isinstance(bank, dict) else {}


def _save_bank(context: str, bank: dict):
    path = _bank_path(context)
    try:
        atomic_write_json(path, bank)
    except Exception as e:
        logger.warning(f"[Flavor] банк не записан ({context}): {e}")


def _prompt_hash(system_prompt: str) -> str:
    return hashlib.sha256(
        f"{SCHEMA_VERSION}:{system_prompt}".encode("utf-8")).hexdigest()[:16]


# ── Выдача реплики ─────────────────────────────────────────

def _map_kind(action: Optional[dict]) -> str:
    kind = str((action or {}).get("kind") or "generic")
    kind = _KIND_MAP.get(kind, kind)
    return kind if kind in _BANK_KINDS else "generic"


def _placeholder_values(action: Optional[dict], detail: Optional[str]) -> dict:
    a = action or {}
    return {
        "host": str(a.get("host") or ""),
        "site": str(a.get("host") or ""),
        "element": str(a.get("element") or a.get("slider_label") or ""),
        "text": str(a.get("text") or "")[:40],
        "detail": str(detail or ""),
    }


def _from_bank(context: str, kind: str, bucket: str,
               action: Optional[dict], detail: Optional[str]) -> Optional[str]:
    """Случайная фраза из банка с подстановкой плейсхолдеров. None — банк
    пуст/промахнулся (caller идёт в живой вызов). Промах — в т.ч. когда все
    варианты kind отбракованы фильтрами (тогда пробуем generic)."""
    with _BANK_LOCK:
        bank = _load_bank(context)
    kinds = bank.get("kinds") or {}
    values = _placeholder_values(action, detail)

    def _pick(candidates) -> Optional[str]:
        candidates = [p for p in candidates or []
                      if isinstance(p, str) and p.strip()]
        # Раньше — random.sample(candidates, 3): если первым трём случайно
        # попадались только фразы без {detail}/с пустым плейсхолдером, функция
        # молча возвращала None и звала честный шаблон, хотя дальше в банке
        # мог быть годный вариант. Перебираем ВСЕХ кандидатов в случайном
        # порядке — до первого подходящего, а не до третьей попытки.
        shuffled = list(candidates)
        random.shuffle(shuffled)
        for phrase in shuffled:
            # err-фраза без {detail} теряет суть ошибки — не годится
            if bucket == "err" and "{detail}" not in phrase:
                continue
            # Плейсхолдер с пустым значением уродует фразу («Выполнено: .» —
            # кейс 19.09, «пауза»: key-действие без element): вариант годен,
            # только если все его плейсхолдеры непусты у этого действия
            if any(not values.get(p)
                   for p in _PLACEHOLDER_RE.findall(phrase)):
                continue
            try:
                text = phrase.format_map(values).strip()
            except (KeyError, IndexError, ValueError):
                continue  # неизвестный плейсхолдер — другая фраза
            if text:
                return text
        return None

    text = _pick((kinds.get(kind) or {}).get(bucket))
    if text is None and kind != "generic":
        text = _pick((kinds.get("generic") or {}).get(bucket))
    return text


def _google_cc_chat(context: str, channel: str = "cc"):
    """WebChatLLM google на stateless-канале (свежий чат на вызов, история
    не копится). Кэшируется по (контекст, канал): у ДОЛГОЙ генерации банка
    канал «cc_gen» — отдельный инстанс со своим локом и вкладкой, чтобы
    она не блокировала живые реплики канала «cc» (кейс 18.09: ответ
    пользователю ждал генерацию банка 2.5 мин)."""
    key = (context, channel)
    with _WC_LOCK:
        chat = _WC.get(key)
        if chat is None:
            from app.features.web_llm import WebChatLLM
            chat = WebChatLLM("google", context=context, channel=channel,
                              quota_per_hour=None)
            _WC[key] = chat
        return chat


def _clean_live(text: Optional[str]) -> Optional[str]:
    """Живой ответ google → короткая реплика. None — ответ не годится."""
    t = (text or "").strip().strip('"«»').strip()
    if not t or t.startswith("{") or t.startswith("["):
        return None
    if len(t) > _LIVE_MAX_CHARS:
        t = t[:_LIVE_MAX_CHARS - 1].rstrip() + "…"
    return t


def _from_live(bot, kind: str, bucket: str,
               action: Optional[dict], detail: Optional[str]) -> Optional[str]:
    """Живой вызов Google AI Mode (канал cc). None — недоступен (шаблон).
    Пока идёт фоновая генерация банка — не дёргаем вовсе: дождёмся банка,
    до него честные шаблоны (кейс 18.09). Лок инстанса ждём не дольше
    _LIVE_LOCK_TIMEOUT_SEC — реплика на пользовательском пути."""
    context = getattr(bot, "context", "default")
    if context in _GEN_STARTED:
        return None
    persona_prompt = (getattr(getattr(bot, "persona", None), "system_prompt", "")
                      or "")[:2500]
    cc = getattr(bot, "computer_control", None)
    if bucket == "ok":
        what = cc.describe_done(action) if (action and cc) else "действие выполнено"
        task = (f"Действие выполнено: {what}.\n"
                "Напиши одну короткую реплику в характере персоны, сообщающую "
                "об этом (1-2 предложения; короткое действие в *курсиве* "
                "уместно, если это в характере). Без вопросов пользователю.")
    else:
        what = cc.describe(action) if (action and cc) else "команда"
        err = str(detail or "неизвестная ошибка")
        task = (f"Действие НЕ удалось: {what}. Причина: {err}.\n"
                "Напиши одну короткую реплику в характере персоны о неудаче. "
                f"Суть причины («{err}») сохрани дословно по смыслу — причину "
                "не скрывай и не приукрашивай. Без вопросов пользователю.")
    messages = [
        {"role": "system", "content": (
            "Характер персоны (соблюдай строго, не пересказывай):\n"
            f"{persona_prompt}")},
        {"role": "user", "content": task},
    ]
    try:
        chat = _google_cc_chat(context)
        return _clean_live(chat.get_response(messages, temperature=0.7,
                                             max_tokens=150, top_p=0.9,
                                             timeout=30.0,
                                             lock_timeout=_LIVE_LOCK_TIMEOUT_SEC))
    except Exception as e:
        logger.debug(f"[Flavor] живой google не ответил: {e}")
        return None


def cc_reply(bot, action: Optional[dict], ok: bool,
             detail: Optional[str] = None) -> Optional[str]:
    """Реплика в характере персоны о результате CC-команды:
    банк → живой google → None (caller берёт честный шаблон)."""
    try:
        context = getattr(bot, "context", "default")
        kind = _map_kind(action)
        bucket = "ok" if ok else "err"
        text = _from_bank(context, kind, bucket, action, detail)
        if text:
            return text
        return _from_live(bot, kind, bucket, action, detail)
    except Exception as e:
        logger.debug(f"[Flavor] cc_reply не удался: {e}")
        return None


def phrase(context: str, key: str, template: str, **values) -> str:
    """Служебная фраза (режим управления, сценарии) голосом персоны из банка
    (секция phrases), с подстановкой плейсхолдеров. Живого вызова нет — это
    ответы на действие пользователя, ждать LLM нельзя: ключа/вариантов нет
    или плейсхолдеры не сошлись — честный шаблон template."""
    try:
        with _BANK_LOCK:
            bank = _load_bank(context)
        variants = [v for v in ((bank.get("phrases") or {}).get(key) or [])
                    if isinstance(v, str) and v.strip()]
        vals = {k: str(v) for k, v in values.items()}
        # Все варианты в случайном порядке — см. комментарий в _pick() внутри
        # _from_bank про то, почему sample(3) молча терял годные варианты.
        shuffled_variants = list(variants)
        random.shuffle(shuffled_variants)
        for variant in shuffled_variants:
            # Как в банке команд: плейсхолдер с пустым значением уродует
            # фразу — такой вариант пропускаем
            if any(not vals.get(p)
                   for p in _PLACEHOLDER_RE.findall(variant)):
                continue
            try:
                text = variant.format_map(vals).strip()
            except (KeyError, IndexError, ValueError):
                continue  # неизвестный плейсхолдер — другой вариант
            if text:
                return text
    except Exception as e:
        logger.debug(f"[Flavor] phrase({key}) не удалась: {e}")
    return template


# ── Генерация банка ────────────────────────────────────────

def _gen_prompt(system_prompt: str, ok_n: int, err_n: int) -> str:
    kinds_descr = "\n".join(f"- {k}" for k in _BANK_KINDS)
    return (
        "Генерируешь короткие реплики Telegram-бота в характере персоны. "
        "Отвечаешь строго JSON, без пояснений.\n\n"
        f"Характер персоны:\n{system_prompt[:4000]}\n\n"
        "Бот выполняет команды управления компьютером (открыть сайт, нажать "
        "элемент, ввести текст, листать и т.п.) и коротко сообщает результат "
        "пользователю. Для КАЖДОГО типа команды придумай:\n"
        f"- \"ok\": {ok_n} разнообразных фраз об успехе;\n"
        f"- \"err\": {err_n} фраз о неудаче; каждая err-фраза ОБЯЗАНА "
        "содержать плейсхолдер {detail} — туда подставится суть ошибки, "
        "её нельзя скрывать или приукрашивать.\n\n"
        "Допустимые плейсхолдеры (подставляются данными команды): {host} — "
        "сайт, {element} — элемент страницы, {text} — введённый текст, "
        "{detail} — суть ошибки (только err). Других плейсхолдеров не "
        "используй. Плейсхолдер ставь только там, где он уместен.\n\n"
        f"Типы команд:\n{kinds_descr}\n\n"
        "Формат ответа — строго JSON:\n"
        "{\"open\": {\"ok\": [...], \"err\": [...]}, \"click\": {...}, ...}\n\n"
        "Фразы короткие (1-2 предложения), в характере персоны, "
        "разнообразные по лексике и конструкции. Короткое действие "
        "в *курсиве* уместно, если это в характере. Без вопросов "
        "пользователю."
    )


def _gen_prompt_phrases(system_prompt: str) -> str:
    """Отдельная генерация служебных фраз (секция phrases). Своим вызовом:
    объединённый промпт (kinds + phrases) раздувал и запрос, и ответ —
    google AI Mode думал ~8 минут и не укладывался в таймаут чтения
    (кейс 19.09: ответ дорендерился на вкладке, но обёртка уже ушла)."""
    lines = "\n".join(f'- "{k}": {v["spec"]}' for k, v in _PHRASE_KEYS.items())
    return (
        "Пишешь служебные сообщения Telegram-бота в характере персоны. "
        "Отвечаешь строго JSON, без пояснений.\n\n"
        f"Характер персоны:\n{system_prompt[:4000]}\n\n"
        "Ниже — ситуации и обязательный смысл каждого сообщения. Для КАЖДОЙ "
        "придумай 3 разнообразных варианта. Это ГОТОВЫЕ сообщения бота "
        "пользователю: пиши от лица бота и в характере персоны, а не "
        "пересказывай описание ситуации. Команды в «кавычках» сохраняй "
        "дословно — это инструкции для пользователя. Плейсхолдеры в "
        "{фигурных} скобках оставляй как есть — в них подставятся данные; "
        "других плейсхолдеров не используй.\n\n"
        f"Ситуации:\n{lines}\n\n"
        "Формат ответа — строго JSON:\n"
        "{\"cc_mode_on\": [...], \"cc_mode_off\": [...], ...}\n\n"
        "Каждый вариант — 1-2 предложения, разнообразие по лексике. "
        "Без вопросов пользователю от себя."
    )


def _validate_phrases(raw_ph) -> dict:
    """{key: [варианты]} → валидированные phrases: только известные ключи
    _PHRASE_KEYS, строки, без чужих плейсхолдеров, обязательные (req)
    присутствуют."""
    out = {}
    if not isinstance(raw_ph, dict):
        return out
    for key, variants in raw_ph.items():
        spec = _PHRASE_KEYS.get(str(key).strip())
        if spec is None or not isinstance(variants, list):
            continue
        good = []
        for v in variants[:5]:
            if not isinstance(v, str) or not v.strip():
                continue
            phs = set(_PLACEHOLDER_RE.findall(v))
            if phs - spec["ph"] or not spec["req"] <= phs:
                continue
            good.append(v.strip())
        if good:
            out[str(key).strip()] = good
    return out


def _parse_phrases_json(raw: str) -> dict:
    """Ответ генератора служебных фраз → {"phrases": {...}} ({} — мусор).
    Допускаем и плоский вид, и обёртку {"phrases": {...}}."""
    from app.features.web_llm import extract_json
    data = extract_json(raw or "")
    if not isinstance(data, dict):
        return {}
    inner = data.get("phrases") if isinstance(data.get("phrases"), dict) \
        else data
    phrases = _validate_phrases(inner)
    return {"phrases": phrases} if phrases else {}


def _parse_bank_json(raw: str) -> dict:
    """Ответ генератора → {"kinds": {kind: {"ok"/"err": [...]}}, "phrases":
    {key: [...]}}. Валидация: только строки; err — только с {detail}; phrases —
    только известные ключи _PHRASE_KEYS, без чужих плейсхолдеров, обязательные
    (req) присутствуют."""
    from app.features.web_llm import extract_json
    data = extract_json(raw or "")
    if not isinstance(data, dict):
        return {}
    allowed_ph = {"host", "site", "element", "text", "detail"}
    out = {}
    for kind, buckets in data.items():
        if not isinstance(buckets, dict):
            continue
        k = str(kind).strip()
        if k not in _BANK_KINDS:
            continue
        ok_list, err_list = [], []
        for phrase in (buckets.get("ok") or [])[:10]:
            if not isinstance(phrase, str) or not phrase.strip():
                continue
            phs = set(_PLACEHOLDER_RE.findall(phrase))
            if phs - allowed_ph or "detail" in phs:
                continue
            ok_list.append(phrase.strip())
        for phrase in (buckets.get("err") or [])[:10]:
            if not isinstance(phrase, str) or not phrase.strip():
                continue
            phs = set(_PLACEHOLDER_RE.findall(phrase))
            if "{detail}" not in phrase or phs - allowed_ph:
                continue
            err_list.append(phrase.strip())
        if ok_list or err_list:
            out[k] = {"ok": ok_list, "err": err_list}
    phrases = _validate_phrases(data.get("phrases"))
    result = {"kinds": out}
    if phrases:
        result["phrases"] = phrases
    return result


def _generate_kinds(context: str, system_prompt: str, router,
                    ok_n: int, err_n: int) -> dict:
    """Генерация реплик команд (kinds): google (канал cc_gen — отдельный
    инстанс, чтобы не держать лок живых реплик) → одна попытка по обычной
    цепочке роутера. → {"kinds": ...}; {} — ничего не вышло (работаем на
    шаблонах)."""
    messages = [{"role": "user", "content": _gen_prompt(system_prompt, ok_n, err_n)}]
    raw = None
    try:
        raw = _google_cc_chat(context, channel="cc_gen").get_response(
            messages, temperature=0.8, max_tokens=2500, top_p=0.9,
            timeout=300.0)
    except Exception as e:
        logger.debug(f"[Flavor] генерация через google не удалась: {e}")
    if not raw and router is not None:
        try:
            raw = router.get_response(messages, temperature=0.8,
                                      max_tokens=2500, timeout=300.0,
                                      webchat_channel="cc_gen")
        except Exception as e:
            logger.debug(f"[Flavor] генерация по цепочке не удалась: {e}")
    kinds = (_parse_bank_json(raw or "")).get("kinds") or {}
    if kinds:
        logger.info(f"[Flavor] {context}: сгенерировано вариантов — "
                    f"{sum(len(b['ok']) + len(b['err']) for b in kinds.values())}")
    else:
        logger.warning(f"[Flavor] {context}: генерация банка не удалась")
    return {"kinds": kinds} if kinds else {}


def _generate_phrases(context: str, system_prompt: str, router) -> dict:
    """Генерация служебных фраз (phrases) — ОТДЕЛЬНЫМ вызовом от kinds:
    объединённый промпт раздувал ответ вдвое, google AI Mode думал ~8 минут
    и не укладывался в таймаут чтения (кейс 19.09: ответ дорендерился на
    вкладке, но обёртка уже ушла по timeout=120). {} — не вышло (шаблоны)."""
    messages = [{"role": "user", "content": _gen_prompt_phrases(system_prompt)}]
    raw = None
    try:
        raw = _google_cc_chat(context, channel="cc_gen").get_response(
            messages, temperature=0.8, max_tokens=3000, top_p=0.9,
            timeout=300.0)
    except Exception as e:
        logger.debug(f"[Flavor] генерация фраз через google не удалась: {e}")
    if not raw and router is not None:
        try:
            raw = router.get_response(messages, temperature=0.8,
                                      max_tokens=3000, timeout=300.0,
                                      webchat_channel="cc_gen")
        except Exception as e:
            logger.debug(f"[Flavor] генерация фраз по цепочке не удалась: {e}")
    phrases = (_parse_phrases_json(raw or "")).get("phrases") or {}
    if phrases:
        logger.info(f"[Flavor] {context}: сгенерировано служебных фраз — "
                    f"{sum(len(v) for v in phrases.values())}")
    else:
        logger.warning(f"[Flavor] {context}: генерация фраз не удалась")
    return {"phrases": phrases} if phrases else {}


def _generate_safe(context: str, system_prompt: str, router=None):
    try:
        with _GEN_LOCK:
            parsed_k = _generate_kinds(context, system_prompt, router,
                                       _OK_PER_KIND, _ERR_PER_KIND)
            parsed_p = _generate_phrases(context, system_prompt, router)
            kinds = (parsed_k or {}).get("kinds") or {}
            phrases = (parsed_p or {}).get("phrases") or {}
            if not kinds and not phrases:
                return
            with _BANK_LOCK:
                bank = _load_bank(context)
                if kinds:
                    bank["kinds"] = kinds
                if phrases:
                    bank["phrases"] = phrases
                bank["_meta"] = {
                    "prompt_hash": _prompt_hash(system_prompt),
                    "schema": SCHEMA_VERSION,
                    "generated_at": time.time(),
                }
                _save_bank(context, bank)
    except Exception as e:
        logger.warning(f"[Flavor] генерация банка упала: {e}")
    finally:
        _GEN_STARTED.discard(context)


def ensure_flavor_bank(bot=None, context: str = None, system_prompt: str = None,
                       background: bool = True):
    """Банк flavor-фраз актуален (есть kinds и хэш system_prompt совпадает)?
    Нет — фоновая генерация. Вызывается при старте бота и создании персоны."""
    context = context or getattr(bot, "context", "default")
    sp = system_prompt
    if sp is None and bot is not None:
        sp = getattr(getattr(bot, "persona", None), "system_prompt", "") or ""
    if not (sp or "").strip():
        return
    with _BANK_LOCK:
        bank = _load_bank(context)
    if bank.get("kinds") and bank.get("phrases") and \
            (bank.get("_meta") or {}).get("prompt_hash") == _prompt_hash(sp):
        return  # актуален
    # check-and-add в _GEN_STARTED — под _GEN_LOCK: раньше "in _GEN_STARTED" и
    # ".add(context)" были двумя отдельными шагами без лока между ними — два
    # параллельных ensure_flavor_bank(context) (например, две персоны на одном
    # контексте стартуют одновременно) оба проходили проверку до того, как
    # любой из них успевал добавить context, и запускали ДВЕ фоновые генерации
    # банка одновременно.
    with _GEN_LOCK:
        if context in _GEN_STARTED:
            return
        _GEN_STARTED.add(context)
    router = getattr(bot, "router", None)
    if not background:
        _generate_safe(context, sp, router)
        return
    threading.Thread(target=_generate_safe, args=(context, sp, router),
                     daemon=True, name=f"flavor-gen-{context}").start()
    logger.info(f"[Flavor] {context}: банк устарел/отсутствует — "
                "фоновая генерация")


# ── Фоновое пополнение (разнообразие) ─────────────────────

def maybe_topup_flavor_bank(context: str, system_prompt: str, router,
                            idle_ok: bool) -> bool:
    """Банк старше FLAVOR_BANK_REFRESH_DAYS и пользователь неактивен →
    догенерировать порцию вариантов и слить с дедупликацией (пополняются
    только kinds — служебные phrases из начальной генерации достаточны).
    True — пополнение выполнено."""
    if not idle_ok or not (system_prompt or "").strip():
        return False
    with _BANK_LOCK:
        bank = _load_bank(context)
    kinds = bank.get("kinds") or {}
    if not kinds:
        return False  # пустой банк — дело ensure_flavor_bank
    generated_at = float((bank.get("_meta") or {}).get("generated_at") or 0)
    if time.time() - generated_at < FLAVOR_BANK_REFRESH_DAYS * 86400:
        return False
    if not _GEN_LOCK.acquire(blocking=False):
        return False
    try:
        extra = _generate_kinds(context, system_prompt, router,
                                _TOPUP_OK, _TOPUP_ERR)
        extra_kinds = (extra or {}).get("kinds") or {}
        if not extra_kinds:
            return False
        with _BANK_LOCK:
            bank = _load_bank(context)
            cur = bank.setdefault("kinds", {})
            for kind, buckets in extra_kinds.items():
                tgt = cur.setdefault(kind, {"ok": [], "err": []})
                for bucket in ("ok", "err"):
                    seen = {p.strip().casefold()
                            for p in tgt.get(bucket, [])}
                    for phrase in buckets.get(bucket, []):
                        if phrase.strip().casefold() in seen:
                            continue
                        tgt.setdefault(bucket, []).append(phrase)
                        seen.add(phrase.strip().casefold())
                    tgt[bucket] = tgt.get(bucket, [])[:_MAX_PER_BUCKET]
            meta = bank.setdefault("_meta", {})
            meta["prompt_hash"] = _prompt_hash(system_prompt)
            meta["schema"] = SCHEMA_VERSION
            meta["generated_at"] = time.time()
            _save_bank(context, bank)
        logger.info(f"[Flavor] {context}: банк пополнен новыми вариантами")
        return True
    except Exception as e:
        logger.warning(f"[Flavor] пополнение банка упало: {e}")
        return False
    finally:
        _GEN_LOCK.release()

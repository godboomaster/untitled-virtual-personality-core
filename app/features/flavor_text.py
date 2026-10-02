"""Короткие реплики в характере персоны о результатах команд управления
компьютером (CC) — «системные сообщения», которым не нужны история диалога,
LTM, погода и прочий контекст.

Живой вызов: если у персоны назначен провайдер ответа (llm.answer_provider,
router.answer_provider) — ОДНА попытка через него (router.get_response_assigned,
для веб-чата — разовый канал пути пользователя), неудача — сразу None (честный шаблон caller'а), без фоллбека на
google или обычную цепочку — пользователь ждёт готовую реплику, а не долгий
перебор провайдеров. Не назначен — как раньше, webchat-провайдер google
(канал «cc», stateless: каждый вызов — свежий чат без прошлого контекста;
лок инстанса ждётся не дольше _LIVE_LOCK_TIMEOUT_SEC, пока идёт фоновая
генерация банка — живой вызов тогда пропускается в пользу шаблонов).
Генерация/пополнение банка (kinds/phrases) — тоже сначала назначенный
провайдер (канал «cc_gen», одна попытка), неудача — google (канал «cc_gen»,
отдельный инстанс со своим локом, чтобы не блокировать живые реплики) →
обычная цепочка роутера. Основной путь выдачи реплики — банк заранее
сгенерированных фраз (data/{context}/flavor_bank.json) с плейсхолдерами
({host}, {element}, {text}, {detail}); последний фоллбек — честный шаблон
caller'а (describe_done и т.п.).

Ошибки тоже проходят через flavor, но суть ошибки всегда сохраняется
({detail} в банковских фразах, инструкция в живом промпте) — причину не
скрывать и не приукрашивать.

Напоминания сюда не входят — они всегда генерируются LLM намеренно.

Банк генерируется при создании персоны и при старте бота, если хэш
system_prompt изменился, и дополнительно пополняется новыми вариантами,
когда банк старше FLAVOR_BANK_REFRESH_DAYS и пользователь неактивен.
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
from app.core.language import persona_language, user_language_line
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2  # версия схемы банка: включает секцию phrases (служебные фразы)
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
        "computer control mode is on: the commands «открой …», «нажми …», "
        "«введи …» and scenarios work; while the mode is on, reminders, the "
        "todo list, the inventory and learning stay silent; to exit — say "
        "«выйди из режима управления»"},
    "cc_mode_off": {"ph": set(), "req": set(), "spec":
        "control mode is off: the bot no longer controls the browser; "
        "reminders, the todo list, the inventory and learning work again"},
    "cc_mode_already_on": {"ph": set(), "req": set(), "spec":
        "control mode was already on; to exit — «выйди из режима "
        "управления»"},
    "cc_mode_already_off": {"ph": set(), "req": set(), "spec":
        "control mode was already off"},
    "cc_mode_disabled": {"ph": set(), "req": set(), "spec":
        "computer control is disabled in the bot settings; it can be "
        "enabled in the dossier, section «Инструменты»"},
    "scenario_record_start": {"ph": set(), "req": set(), "spec":
        "scenario recording has started: actions («открой …», «нажми …», "
        "«введи …») go into the recording; to finish — «сохрани сценарий» "
        "(a name can be given right away); to cancel — «отмени запись»"},
    "scenario_record_already": {"ph": {"since"}, "req": {"since"}, "spec":
        "a scenario is already being recorded (since {since}); to finish — "
        "«сохрани сценарий», to cancel — «отмени запись»"},
    "scenario_record_cancel": {"ph": set(), "req": set(), "spec":
        "scenario recording cancelled, nothing was saved"},
    "scenario_record_cancel_none": {"ph": set(), "req": set(), "spec":
        "no scenario was being recorded — nothing to cancel"},
    "scenario_save_ask_name": {"ph": set(), "req": set(), "spec":
        "the bot asks what to name the scenario; format for the user: "
        "«сохрани сценарий заказ пиццы»"},
    "scenario_saved": {"ph": {"name", "steps"}, "req": {"name"}, "spec":
        "scenario «{name}» is saved, it has {steps} steps; to run it — just "
        "say «{name}»"},
    "scenario_not_found": {"ph": {"name"}, "req": {"name"}, "spec":
        "the bot has no scenario «{name}»"},
    "scenario_started": {"ph": {"name", "steps"}, "req": {"name"}, "spec":
        "starting scenario «{name}» ({steps} steps); to cancel — say "
        "«отмена»"},
    "scenario_stuck": {"ph": set(), "req": set(), "spec":
        "the scenario is stuck on a failed step; options for the user: "
        "«повтори», «дальше» (skip the step) or «отмена»"},
    "scenario_run_cancel": {"ph": {"name"}, "req": {"name"}, "spec":
        "scenario «{name}» cancelled"},
    "scenario_run_cancel_none": {"ph": set(), "req": set(), "spec":
        "nothing to cancel — no scenario is running"},
    "scenario_offer": {"ph": set(), "req": set(), "spec":
        "the bot offers to remember the flow just completed as a scenario, "
        "so that next time the bot runs it by itself; format for the user: "
        "«запомни сценарий …» and a name"},
}

_BANK_LOCK = threading.Lock()
_GEN_LOCK = threading.Lock()       # одна генерация/пополнение на процесс
_GEN_STARTED: set = set()          # context → фоновая генерация уже идёт

# Кэш webchat-инстансов google по контексту (канал «cc» — stateless)
_WC: dict = {}
_WC_LOCK = threading.Lock()


# ── Банк: файл ─────────────────────────────────────────────

def _bank_path(context: str) -> Path:
    return data_dir() / context / "flavor_bank.json"


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
        # Перебираем всех кандидатов в случайном порядке до первого
        # подходящего: при усечении до нескольких попыток можно наткнуться
        # только на фразы без нужных плейсхолдеров и остаться без варианта,
        # хотя годный был дальше в списке.
        shuffled = list(candidates)
        random.shuffle(shuffled)
        for phrase in shuffled:
            # err-фраза без {detail} теряет суть ошибки — не годится
            if bucket == "err" and "{detail}" not in phrase:
                continue
            # Плейсхолдер с пустым значением портит фразу («Выполнено: .»
            # для key-действия без element) — вариант годен, только если
            # все его плейсхолдеры непусты для этого действия
            if any(not values.get(p)
                   for p in _PLACEHOLDER_RE.findall(phrase)):
                continue
            vals = values
            # «Причина: {detail}.» + detail с точкой на конце дал бы «..»
            # — точку фразы оставляем, хвостовую точку detail срезаем
            if re.search(r"\{detail\}[.!?…]", phrase):
                vals = {**values,
                        "detail": values["detail"].rstrip().rstrip(".")}
            try:
                text = phrase.format_map(vals).strip()
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
    """WebChatLLM на провайдере google, stateless-канал (свежий чат на
    каждый вызов, история не копится). Кэш по (контекст, канал): долгая
    генерация банка идёт в отдельном канале «cc_gen» со своим инстансом
    и локом, чтобы не блокировать живые реплики канала «cc»."""
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
    # Живой ответ google → короткая реплика. None — ответ не годится.
    t = (text or "").strip().strip('"«»').strip()
    if not t or t.startswith("{") or t.startswith("["):
        return None
    if len(t) > _LIVE_MAX_CHARS:
        t = t[:_LIVE_MAX_CHARS - 1].rstrip() + "…"
    return t


def _from_live(bot, kind: str, bucket: str,
               action: Optional[dict], detail: Optional[str],
               lang: Optional[str] = None) -> Optional[str]:
    """Живой вызов. Назначен провайдер ответа персоны (llm.answer_provider,
    router.answer_provider) — ОДНА попытка через него (канал «cc»), неудача
    — сразу None (без google и без обычной цепочки: пользователь ждёт
    готовую реплику, а не долгий перебор). Не назначен — как раньше, google
    (канал cc). None — недоступен, тогда шаблон (caller берёт честный).
    Пока идёт фоновая генерация банка — вызов пропускается: до готовности
    банка отвечаем шаблонами. Лок инстанса google ждём не дольше
    _LIVE_LOCK_TIMEOUT_SEC — реплика на пользовательском пути.
    lang — язык пользователя в диалоге (None — язык персоны)."""
    context = getattr(bot, "context", "default")
    if context in _GEN_STARTED:
        return None
    full_prompt = getattr(getattr(bot, "persona", None), "system_prompt", "") or ""
    persona_prompt = full_prompt[:2500]
    lang = lang or persona_language(full_prompt)
    cc = getattr(bot, "computer_control", None)
    # Реплика генерируется внешней моделью (google AI Mode/назначенный
    # провайдер). Действие на приватной странице (банк, переписка, вход — по
    # полному URL и хосту; без адреса — отслеживаемая страница чата) туда
    # не описываем вовсе: None — caller берёт шаблон. Введённый текст —
    # всегда маской (пароль в поле без подписи по ней не распознать),
    # известные секреты чатов и ПДн в описании/причине — маской
    from app.features.cc_privacy import (known_secret_values, mask,
                                         mask_values, page_candidates,
                                         redact_inline)
    if action and cc is not None:
        from app.features.cc_privacy import _host_of
        check = getattr(cc, "is_private_page", None)
        cands = page_candidates(action)
        try:
            last = getattr(cc, "_last_url", None)
        except Exception:
            last = None
        if last:
            # Отслеживаемая страница чата — всегда, если адресов нет или
            # хост тот же: у download/cart/zoom адрес действия — файл или
            # только хост, а сама страница (vk.com/im) приватна по пути
            def _h(s) -> str:
                h = _host_of(str(s or ""))[0]
                return h[4:] if h.startswith("www.") else h
            if not cands or _h(last) in {_h(c) for c in cands}:
                cands = cands + [str(last)]
        try:
            if callable(check) and any(check(c) for c in cands):
                return None
        except Exception:
            return None

    def _safe(a: dict) -> dict:
        if isinstance(a, dict) and a.get("kind") == "type":
            return dict(a, text=mask(a.get("text")))
        return a
    if action:
        action = _safe(action)
        if action.get("kind") == "multi" and isinstance(action.get("items"), list):
            action = dict(action, items=[_safe(a) for a in action["items"]])
    known = known_secret_values()
    detail = redact_inline(mask_values(detail, known)) if detail else detail
    if bucket == "ok":
        what = cc.describe_done(action) if (action and cc) else "the action was completed"
    else:
        what = cc.describe(action) if (action and cc) else "a command"
    # describe берёт подписи/URL из действия — те же маски, что и у detail
    what = redact_inline(mask_values(what, known))
    if bucket == "ok":
        task = (f"Action completed: {what}.\n"
                "Write one short line in the persona's character reporting "
                "it (1-2 sentences; a short action in *italics* is fine if "
                "it fits the character). No questions to the user.")
    else:
        err = str(detail or "unknown error").rstrip().rstrip(".") \
            or "unknown error"
        task = (f"Action FAILED: {what}. Reason: {err}.\n"
                "Write one short line in the persona's character about the failure. "
                f"Keep the gist of the reason («{err}») faithful in meaning — do "
                "not hide or embellish the reason. No questions to the user.")
    task += "\n" + user_language_line(lang)
    messages = [
        {"role": "system", "content": (
            "Persona's character (follow it strictly, do not retell it):\n"
            f"{persona_prompt}\n\n{user_language_line(lang)}")},
        {"role": "user", "content": task},
    ]
    router = getattr(bot, "router", None)
    provider = getattr(router, "answer_provider", None)
    if provider:
        # Назначенный провайдер ответа — одна попытка ВНЕ обычной цепочки
        # и вне google: пользователь ждёт готовую реплику прямо сейчас,
        # долгий перебор фоллбеков здесь не к месту (в отличие от
        # get_response(force_provider=...), где неудача уходит в цепочку).
        # Канал side + user_path=True: веб-чат подменяется разовым
        # USER_PATH_CHANNEL — лок не дольше USER_PATH_QUEUE_WAIT_SEC и без
        # пола ответа 150 с. Канал cc так не умеет: его лок делят решения
        # режима управления, ждётся без ограничения, а таймаут 30 с
        # поднимается до 150 с (см. Router._try_webchat).
        try:
            return _clean_live(router.get_response_assigned(
                provider, messages, temperature=0.7, max_tokens=150,
                top_p=0.9, timeout=30.0, webchat_channel="side",
                user_path=True))
        except Exception as e:
            logger.debug(f"[Flavor] назначенный провайдер {provider} "
                        f"не ответил: {e}")
        return None
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
             detail: Optional[str] = None,
             lang: Optional[str] = None) -> Optional[str]:
    """Реплика в характере персоны о результате CC-команды:
    банк → живой google → None (caller берёт честный шаблон).
    lang — язык пользователя в диалоге; банк сгенерирован на языке персоны,
    и если язык пользователя другой — банк пропускается (живой вызов)."""
    try:
        context = getattr(bot, "context", "default")
        kind = _map_kind(action)
        bucket = "ok" if ok else "err"
        sp = getattr(getattr(bot, "persona", None), "system_prompt", "") or ""
        bank_lang = persona_language(sp)
        if not (lang and bank_lang and lang != bank_lang):
            text = _from_bank(context, kind, bucket, action, detail)
            if text:
                return text
        return _from_live(bot, kind, bucket, action, detail, lang=lang)
    except Exception as e:
        logger.debug(f"[Flavor] cc_reply не удался: {e}")
        return None


def phrase(context: str, key: str, template: str, **values) -> str:
    """Служебная фраза (режим управления, сценарии) голосом персоны из банка
    (секция phrases), с подстановкой плейсхолдеров. Живого вызова нет —
    это ответ на действие пользователя, ждать LLM нельзя: ключа/вариантов
    нет или плейсхолдеры не сошлись — честный шаблон template."""
    try:
        with _BANK_LOCK:
            bank = _load_bank(context)
        variants = [v for v in ((bank.get("phrases") or {}).get(key) or [])
                    if isinstance(v, str) and v.strip()]
        vals = {k: str(v) for k, v in values.items()}
        # Все варианты перебираем в случайном порядке, чтобы не пропустить
        # годный вариант дальше по списку (как в _pick() внутри _from_bank).
        shuffled_variants = list(variants)
        random.shuffle(shuffled_variants)
        for variant in shuffled_variants:
            # Как в банке команд: плейсхолдер с пустым значением портит
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
    # Банк — на контекст персоны, без чата: язык — язык персоны
    kinds_descr = "\n".join(f"- {k}" for k in _BANK_KINDS)
    return (
        "You generate short lines for a Telegram bot in the persona's character. "
        "You answer strictly in JSON, without explanations.\n\n"
        f"Persona's character:\n{system_prompt[:4000]}\n\n"
        "The bot executes computer control commands (open a site, click an "
        "element, type text, scroll, etc.) and briefly reports the result "
        "to the user. For EACH command type come up with:\n"
        f"- \"ok\": {ok_n} varied phrases about success;\n"
        f"- \"err\": {err_n} phrases about failure; every err phrase MUST "
        "contain the placeholder {detail} — the gist of the error is substituted "
        "there, it must not be hidden or embellished.\n\n"
        "Allowed placeholders (filled with the command's data): {host} — "
        "the site, {element} — the page element, {text} — the typed text, "
        "{detail} — the gist of the error (err only). Do not use any other "
        "placeholders. Put a placeholder only where it fits.\n\n"
        f"Command types:\n{kinds_descr}\n\n"
        "Answer format — strictly JSON:\n"
        "{\"open\": {\"ok\": [...], \"err\": [...]}, \"click\": {...}, ...}\n\n"
        "Phrases are short (1-2 sentences), in the persona's character, "
        "varied in wording and structure. A short action "
        "in *italics* is fine if it fits the character. No questions "
        "to the user.\n"
        f"{user_language_line(persona_language(system_prompt))}"
    )


def _gen_prompt_phrases(system_prompt: str) -> str:
    """Отдельная генерация служебных фраз (секция phrases): объединённый с
    kinds промпт раздувал и запрос, и ответ настолько, что генерация не
    укладывалась в таймаут чтения ответа."""
    lines = "\n".join(f'- "{k}": {v["spec"]}' for k, v in _PHRASE_KEYS.items())
    return (
        "You write service messages for a Telegram bot in the persona's character. "
        "You answer strictly in JSON, without explanations.\n\n"
        f"Persona's character:\n{system_prompt[:4000]}\n\n"
        "Below are the situations and the required meaning of each message. For EACH "
        "one come up with 3 varied variants. These are READY messages from the bot "
        "to the user: write as the bot and in the persona's character, do not "
        "retell the situation description. Keep the commands in «quotes» "
        "verbatim (do not translate them) — these are the exact commands the "
        "bot recognizes. Leave placeholders in {curly} braces as they are — "
        "data is substituted into them; do not use any other placeholders.\n\n"
        f"Situations:\n{lines}\n\n"
        "Answer format — strictly JSON:\n"
        "{\"cc_mode_on\": [...], \"cc_mode_off\": [...], ...}\n\n"
        "Each variant is 1-2 sentences, varied in wording. "
        "No questions to the user of your own.\n"
        f"{user_language_line(persona_language(system_prompt))}"
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
    {key: [...]}}. Валидация: только строки; err — только с {detail};
    phrases — только известные ключи _PHRASE_KEYS, без чужих
    плейсхолдеров, обязательные (req) присутствуют."""
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
    """Генерация реплик команд (kinds): назначенный провайдер ответа
    (llm.answer_provider, канал cc_gen, одна попытка) → google (канал
    cc_gen — отдельный инстанс, чтобы не держать лок живых реплик) → одна
    попытка по обычной цепочке роутера. → {"kinds": ...}; {} — ничего не
    вышло (шаблоны)."""
    messages = [{"role": "user", "content": _gen_prompt(system_prompt, ok_n, err_n)}]
    raw = None
    provider = getattr(router, "answer_provider", None)
    if provider:
        try:
            raw = router.get_response_assigned(
                provider, messages, temperature=0.8, max_tokens=2500,
                top_p=0.9, timeout=300.0, webchat_channel="cc_gen")
        except Exception as e:
            logger.debug(f"[Flavor] генерация через назначенный провайдер "
                        f"{provider} не удалась: {e}")
    if not raw:
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
    """Генерация служебных фраз (phrases) отдельным вызовом от kinds:
    объединённый промпт раздувал ответ вдвое и не укладывался в таймаут
    чтения ответа. Источники — в том же порядке, что в _generate_kinds:
    назначенный провайдер ответа (канал cc_gen) → google (канал cc_gen) →
    обычная цепочка роутера. {} — не вышло (работаем на шаблонах)."""
    messages = [{"role": "user", "content": _gen_prompt_phrases(system_prompt)}]
    raw = None
    provider = getattr(router, "answer_provider", None)
    if provider:
        try:
            raw = router.get_response_assigned(
                provider, messages, temperature=0.8, max_tokens=3000,
                top_p=0.9, timeout=300.0, webchat_channel="cc_gen")
        except Exception as e:
            logger.debug(f"[Flavor] генерация фраз через назначенный "
                        f"провайдер {provider} не удалась: {e}")
    if not raw:
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
    # check-and-add в _GEN_STARTED делаем под одним _GEN_LOCK: иначе два
    # параллельных запуска ensure_flavor_bank(context) (например, две
    # персоны на одном контексте стартуют одновременно) проходят проверку
    # одновременно и оба запускают фоновую генерацию банка.
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

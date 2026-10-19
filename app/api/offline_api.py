"""Ответы без ноутбука — приложение на телефоне, когда ядро недоступно.

Без связи с ноутбуком приложение отвечает само: облачной моделью по ключу
прямо с телефона, а без ключей или без интернета — локальной Gemma на
телефоне. Для этого при каждой связи оно забирает:

  - персона-кит (build_kit): характер персоны, правила офлайна, факты о
    собеседнике (с учётом приватности чата), хвост переписки — без секретов;
  - ключи (provider_creds): API-провайдеры персоны в её порядке — адрес,
    модель, ключи. Только то, что нужно для ответов: остальные секреты .env
    (токены Telegram-ботов, API_TOKEN, пароль посредника…) остаются здесь.
    Веб-чаты и локальная Ollama телефону недоступны — они живут на ноутбуке.

В ките же — то, что телефону нужно для поиска, погоды и напоминаний без
ноутбука: город (погоду телефон берёт сам у Open-Meteo), шаблоны гейта
веб-поиска (app/features/search_gate.py), правила к выдаче и к строке
окружения (те же, что в промпте ядра), срабатывания напоминаний чата на две
недели вперёд — будильник телефона напомнит, даже если ноутбука нет.

Память персоны на телефоне (build_memory) — отдельно от кита и реже: вся
переписка чата из STM, все факты о собеседнике. По ней телефон сам находит
реплики к теме вопроса (как search_relevant ядра) — модель видит не только
хвост переписки. Ищет по словам и по смыслу: векторы реплик и фактов —
готовые из базы ядра, вектор вопроса телефон считает той же моделью
(paraphrase-multilingual-MiniLM-L12-v2). В ките — граница истории (history_first_ts): что старше,
на ноутбуке уже стёрто (/clear, срезка), и телефон это тоже забывает.

Вернувшись, приложение отдаёт офлайн-переписку (import_messages): она встаёт
в историю чата обычным путём memory.add_message — с извлечением фактов, как
у живых сообщений. Факты появляются только здесь: телефон их не создаёт.
Повтор по id не записывается дважды (сеть оборвалась после записи).
Напоминания, поставленные на телефоне (import_reminders), переходят к
ReminderManager ядра; дальше ими управляет он.
"""

import base64
import logging
import time
from typing import Optional

import numpy as np

from app.core.atomic_io import atomic_write_json, load_json_safe
from app.core.language import user_language_line
from app.core.paths import data_dir
from app.core.persona import (ENV_RULE, SELF_MEMORY_RULE, STM_RELEVANT_HEAD, STM_RELEVANT_RULE,
                              WEB_SOURCE_PRIORITY, persona_prompt)
from app.core.st_embedder import ST_MODEL_NAME
from app.features import short_prompt

logger = logging.getLogger(__name__)

KIT_VERSION = 3
HISTORY_TAIL = 30   # сообщений переписки в ките
MAX_FACTS = 80      # фактов о собеседнике (последние)
MEMORY_HISTORY = 500    # сообщений переписки в памяти для телефона (весь STM чата)
MEMORY_TEXT = 2000      # символов одного сообщения в ней
MEMORY_FACTS = 300      # фактов в ней
MAX_EMBED_FILL = 200    # векторов, которых нет в базе, — досчитать за раз
VECTOR_MATCH_MIN = 0.98     # сходство вектора из базы с пересчитанным — та же модель
MAX_IMPORT = 200    # сообщений за один приём
MAX_TEXT = 8000     # символов в одном сообщении
_SEEN_KEEP = 2000   # сколько принятых id помнить для защиты от повтора

OFFLINE_RULES = (
    "You are answering from the user's phone: the computer you normally run on is "
    "unreachable, so your tools are unavailable right now. You remember the recent "
    "conversation, the facts listed below and what the app notes from your earlier "
    "conversation. Stay fully in character and keep replies short and conversational. "
    "Reminders are handled by the app: confirm one only when a note from the app says "
    "it was set, and never claim to have set one otherwise. Web search results, when present, are given "
    "below — you can't browse by yourself. Don't claim to add to-dos or inventory, open "
    "sites or control the computer — if asked, say you'll do it once you're back on the "
    "computer. Don't invent facts about the user beyond what is listed below or noted "
    "from your earlier conversation."
)

# Правила маленькой модели телефона — для прежних сборок приложения: новые
# берут свои (web/src/offline/prompt.ts, LOCAL_RULES — там и почему они такие),
# чтобы правка приходила с приложением, без запуска ядра.
# scripts/test_offline_recall.py сверяет копии
OFFLINE_RULES_LOCAL = (
    "Reply directly to the user's latest message, in character, briefly and "
    "conversationally. Don't repeat your earlier replies word for word. Write everything "
    "in the reply's language, including actions in asterisks. Reminders are handled by "
    "the app: confirm one only "
    "when a note from the app says it was set. If asked to do something on the computer "
    "(open sites, to-dos, inventory), say you'll do it once you're back on the computer. "
    "Don't invent facts about the user."
)

# Заметки для ответа о напоминании на телефоне ({task} — « 'задача'» или
# пусто, {when} — время словами) и разбор просьбы моделью ({now} — местные
# дата и время телефона). Часы частей дня — как в reminder_manager
# (_DAYPART_DEFAULT_HOUR)
REMINDER_NOTES = {
    "set": ("The user asked to be reminded{task} — {when}. The reminder is scheduled "
            "on the phone — confirm this in your own style, briefly, saying when exactly as "
            "given here; don't recalculate the time."),
    "ask_time": ("The user wants a reminder{task} but didn't say when. Nothing was scheduled. "
                 "Ask briefly when to remind them. Do NOT say any reminder was set."),
    "past": ("The user asked for a reminder{task}, but that time has already passed. Nothing was "
             "scheduled. Ask briefly for a time in the future. Do NOT say any reminder was set."),
    "not_understood": ("The user is answering the question about the time for the reminder{task}, "
                       "but the time could not be understood. Ask again: how soon to remind (for "
                       "example, \"in 2 hours\", \"tomorrow at 12\"). In your own style, briefly. "
                       "Do NOT confirm that any reminder was scheduled — it was NOT."),
    "declined": ("The user decided not to set the reminder{task}. Nothing was scheduled. "
                 "Acknowledge briefly in your own style. Do NOT say any reminder was set."),
    "recurring": ("The user asked for a repeating reminder{task}. Repeating reminders can only "
                  "be set once you're back on the computer — say so briefly and offer a "
                  "one-time reminder instead."),
    "scheduled": "Reminders already scheduled (mention them only if the user asks):\n{list}",
    "extract": (
        "Extract the reminder request from the user's message. Current local date and time: "
        "{now}. Reply with ONLY a JSON object and nothing else: "
        '{{"task": "what to remind about, short, in the user\'s language, or empty", '
        '"at": "YYYY-MM-DDTHH:MM local time, or null if no time is given", '
        '"recurring": true if they ask to repeat it (every day, on weekdays...), otherwise false}}. '
        "Resolve relative times against the current time: \"in 20 minutes\", \"tomorrow\". "
        "Morning means 09:00, afternoon 15:00, evening 19:00, night 23:00 unless an hour is given."
    ),
}


def _persona_name(bot, persona_id: str) -> str:
    data = getattr(getattr(bot, "persona", None), "persona_data", None) or {}
    return str(data.get("name") or persona_id)


def _address_names(bot) -> list[str]:
    try:
        return bot._reminder_names()
    except Exception:
        return []  # заготовка бота без имён — телефон возьмёт имя и id персоны


def build_kit(bot, persona_id: str, chat_id: str, user_id: str) -> dict:
    """Персона-кит для ответов без ноутбука (секретов в нём нет)."""
    key = bot.stm_key(chat_id, user_id)
    persona = getattr(bot, "persona", None)
    # Характер — режима собеседника (Великий Мастер / смертный), если у
    # персоны есть файлы режимов, иначе общий
    character = persona_prompt(persona, chat_id=chat_id, user_id=user_id).strip()
    try:
        mode = persona.mode_for(user_id=user_id, chat_id=chat_id) if persona is not None else None
    except Exception:
        mode = None

    try:
        facts = bot.memory.ltm.get_all_facts(user_id=user_id, chat_id=key)
    except Exception as e:
        logger.warning(f"[Offline] {persona_id}: факты для кита не собраны: {e}")
        facts = []
    facts = [f for f in facts if isinstance(f, str) and f.strip()][-MAX_FACTS:]

    try:
        stm = bot.memory.stm.get_messages(chat_id=key)
    except Exception as e:
        logger.warning(f"[Offline] {persona_id}: переписка для кита не собрана: {e}")
        stm = []
    lines = _history(stm[-MEMORY_HISTORY:])
    history = lines[-HISTORY_TAIL:]
    first_ts = float(stm[0].get("timestamp") or 0) if stm else 0.0

    style = None
    try:
        from app.features.conversation_style import build_style_note
        style = build_style_note(bot.conversation_style.frequency)
    except Exception as e:
        logger.debug(f"[Offline] {persona_id}: правило вопросов не добавлено: {e}")
    try:
        lang = bot.chat_user_language(key)
    except Exception:
        lang = None
    language = user_language_line(lang)
    facts_block = ("What you know about the user:\n" + "\n".join(f"- {f}" for f in facts)) if facts else ""

    # Портрет из досье и личная память персоны — как в ответе ядра
    dossier = ""
    try:
        dossier = bot._get_dossier_context_line(chat_id, user_id) or ""
    except Exception as e:
        logger.debug(f"[Offline] {persona_id}: строка досье недоступна: {e}")
    # Кто пишет — особый собеседник (тон, обращение, правила) или обычный, как в
    # ответе ядра; без этого без ядра персона говорила бы с особым как со всеми.
    # Вместе с досье: его берут и облако, и Gemma телефона
    try:
        who = (bot.persona._get_special_user_note(user_id) or "").strip()
    except Exception as e:
        who = ""
        logger.debug(f"[Offline] {persona_id}: строка «кто пишет» недоступна: {e}")
    if who:
        dossier = f"{who}\n\n{dossier}" if dossier else who
    self_memory = ""
    if getattr(bot, "self_memory", None) is not None:
        try:
            self_memory = bot.self_memory.get_context_block() or ""
        except Exception as e:
            logger.debug(f"[Offline] {persona_id}: личная память недоступна: {e}")

    # Краткое описание для модели телефона; нет — начнёт составляться в фоне,
    # а телефон пока обойдётся началом полного характера
    try:
        short = short_prompt.get_short_prompt(bot, mode=mode)
    except Exception as e:
        logger.warning(f"[Offline] {persona_id}: краткое описание недоступно: {e}")
        short = {"text": "", "source": "", "status": "failed", "at": 0, "error": str(e)[:200]}

    reminders = []
    rm = getattr(bot, "reminder_manager", None)
    if rm is not None:
        try:
            reminders = rm.upcoming_for_chat(key)
        except Exception as e:
            logger.warning(f"[Offline] {persona_id}: напоминания для кита не собраны: {e}")

    return {
        "version": KIT_VERSION,
        "persona": persona_id,
        "name": _persona_name(bot, persona_id),
        # Обращения к персоне — срезать из текста напоминания на телефоне
        "names": _address_names(bot),
        "generated_at": time.time(),
        # Целиком — для облачной модели; части — чтобы телефон собрал
        # короче для своей модели (меньше фактов — быстрее первое слово)
        "system": "\n\n".join(p for p in (character, OFFLINE_RULES, style, facts_block, language) if p),
        # short и rules_local — для модели телефона (краткое описание, пусто —
        # ещё не составлено; правила короче)
        "parts": {"character": character, "rules": OFFLINE_RULES, "style": style or "",
                  "language": language, "short": short["text"], "rules_local": OFFLINE_RULES_LOCAL},
        "short_prompt": {k: short[k] for k in ("source", "status", "at") if k in short},
        # Язык чата — запасной: телефон берёт язык из текущей реплики, как ядро
        "lang": lang,
        "facts": facts,
        "history": history,
        # Сообщения старше — на ноутбуке уже стёрты; 0 — переписки нет.
        # Сколько их в памяти для телефона: не совпало с его копией — её пора
        # забрать заново (дописано в прошлое, стёрто из середины)
        "history_first_ts": first_ts,
        "history_total": len(lines),
        "dossier": dossier,
        "self_memory": self_memory,
        "env": _env_location(),
        "reminders": reminders,
        "reminders_enabled": rm is not None,
        "rules": {"web": WEB_SOURCE_PRIORITY, "env": ENV_RULE, "self_memory": SELF_MEMORY_RULE,
                  "recall_head": STM_RELEVANT_HEAD, "recall": STM_RELEVANT_RULE},
        # Подстановка на телефоне — простой заменой {now}/{task}/{when}/{list}
        "notes": {k: v.replace("{{", "{").replace("}}", "}") for k, v in REMINDER_NOTES.items()},
        "search_gate": _search_gate(),
    }


def _history(messages) -> list[dict]:
    return [{"role": m.get("role"), "text": str(m.get("content") or "")[:MEMORY_TEXT],
             "ts": float(m.get("timestamp") or 0)}
            for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]


def pack_vector(vec) -> str:
    """Вектор для телефона: int8 по наибольшей по модулю координате, base64
    (384 байта вместо 384 чисел в JSON). Масштаб телефону не нужен —
    косинус от него не зависит."""
    v = np.asarray(vec, dtype=np.float32)
    top = float(np.max(np.abs(v))) if v.size else 0.0
    if not np.isfinite(top) or top == 0.0:
        return ""
    q = np.clip(np.rint(v / top * 127), -127, 127).astype(np.int8)
    return base64.b64encode(q.tobytes()).decode("ascii")


# Векторы в базе посчитаны той же моделью, что считает сейчас ядро: сверка
# одного текста на контекст за процесс (контекст → совпали ли)
_vectors_match: dict[str, bool] = {}


def _memory_vectors(bot, persona_id: str, key: str, user_id: str,
                    texts: list[str], facts: list[str]) -> Optional[tuple[list[str], list[str], int]]:
    """Векторы реплик и фактов для поиска по смыслу на телефоне: из базы
    (их посчитали при записи), которых там нет — моделью ядра. Телефон
    считает той же моделью только вектор вопроса. → (реплик, фактов,
    размерность); None — векторов не будет (телефон ищет по словам)."""
    stm, ltm = bot.memory.stm, bot.memory.ltm
    embedder = getattr(stm, "embedder", None)
    if embedder is None or not hasattr(stm, "stored_vectors"):
        return None
    try:
        stored = stm.stored_vectors(key)
        fact_stored = ltm.stored_vectors(user_id) if hasattr(ltm, "stored_vectors") else {}
    except Exception as e:
        logger.warning(f"[Offline] {persona_id}: векторы из базы не прочитаны: {e}")
        return None
    context = str(getattr(bot, "context", persona_id))
    sample = next((t for t in texts if t in stored), None) or next((f for f in facts if f in fact_stored), None)
    if sample is not None and context not in _vectors_match:
        try:
            fresh = np.asarray(embedder([sample])[0], dtype=np.float32)
            old = np.asarray(stored.get(sample, fact_stored.get(sample)), dtype=np.float32)
            cos = float(fresh @ old / (np.linalg.norm(fresh) * np.linalg.norm(old) or 1.0))
            _vectors_match[context] = cos >= VECTOR_MATCH_MIN
            if cos < VECTOR_MATCH_MIN:
                logger.warning(f"[Offline] {persona_id}: векторы в базе посчитаны другой моделью "
                               f"(сходство {cos:.2f}) — телефону поиск только по словам")
        except Exception as e:
            logger.warning(f"[Offline] {persona_id}: сверка векторов не удалась: {e}")
            return None
    if _vectors_match.get(context) is False:
        return None
    known = {**fact_stored, **stored}
    missing = list(dict.fromkeys(t for t in [*texts, *facts] if t not in known))[-MAX_EMBED_FILL:]
    if missing:
        try:
            known.update(zip(missing, embedder(missing)))
        except Exception as e:
            logger.warning(f"[Offline] {persona_id}: недостающие векторы не посчитаны: {e}")
    pack = {}

    def packed(text: str) -> str:
        if text not in pack:
            pack[text] = pack_vector(known[text]) if text in known else ""
        return pack[text]

    dim = next((len(v) for v in known.values()), 0)
    return [packed(t) for t in texts], [packed(f) for f in facts], dim


def build_memory(bot, persona_id: str, chat_id: str, user_id: str) -> dict:
    """Память персоны для телефона: вся переписка чата из STM и все факты о
    собеседнике — телефон ищет в них реплики к теме вопроса сам: по словам
    и по смыслу (векторы — те же, что в базе ядра). Тяжелее кита, поэтому
    отдельно: телефон берёт её реже (кит с хвостом переписки держит её
    свежей)."""
    key = bot.stm_key(chat_id, user_id)
    try:
        stm = bot.memory.stm.get_messages(chat_id=key)
    except Exception as e:
        logger.warning(f"[Offline] {persona_id}: переписка для памяти не собрана: {e}")
        stm = []
    try:
        facts = bot.memory.ltm.get_all_facts(user_id=user_id, chat_id=key)
    except Exception as e:
        logger.warning(f"[Offline] {persona_id}: факты для памяти не собраны: {e}")
        facts = []
    messages = [m for m in stm[-MEMORY_HISTORY:] if m.get("role") in ("user", "assistant") and m.get("content")]
    history = _history(messages)
    facts = [f for f in facts if isinstance(f, str) and f.strip()][-MEMORY_FACTS:]
    vectors = _memory_vectors(bot, persona_id, key, user_id,
                              [str(m.get("content")) for m in messages], facts)
    out = {
        "persona": persona_id,
        "generated_at": time.time(),
        "history": history,
        "facts": facts,
        "vectors": None,
    }
    if vectors is not None:
        hist_vecs, fact_vecs, dim = vectors
        for line, vec in zip(history, hist_vecs):
            line["vec"] = vec
        out["facts_vec"] = fact_vecs
        out["vectors"] = {"model": ST_MODEL_NAME, "dim": dim}
    return out


def _env_location() -> Optional[dict]:
    """Город пользователя для погоды на телефоне (погоду телефон берёт сам у
    Open-Meteo) — если местоположение включено в настройках."""
    try:
        from app.features.env_context import load_location
        cfg = load_location()
    except Exception as e:
        logger.debug(f"[Offline] местоположение не прочитано: {e}")
        return None
    if cfg.get("mode") not in ("manual", "geo") or "lat" not in cfg or "lon" not in cfg:
        return None
    from app.features.env_context import _WMO_DESC
    # Описания погодных кодов — строка погоды на телефоне как у ядра
    return {"city": cfg.get("city") or "", "lat": cfg["lat"], "lon": cfg["lon"],
            "codes": {str(k): v for k, v in _WMO_DESC.items()}}


def _search_gate() -> dict:
    """Шаблоны гейта веб-поиска (app/features/search_gate.py): телефон без
    ноутбука ищет по тем же правилам. Регулярки — синтаксис Python; \\w
    телефон заменяет на буквы Unicode."""
    from app.features import search_gate as g
    return {"address_words": sorted(g._ADDRESS_WORDS), "smalltalk": sorted(g._SMALLTALK),
            "self_patterns": list(g._SELF_PATTERNS)}


def provider_creds(bot) -> list[dict]:
    """API-провайдеры персоны в её порядке (основной, llm.fallback, остальные
    с ключами) — с моделью персоны (llm.models), адресом и ключами. Без модели
    провайдер не используется и здесь (как в роутере); исключённые персоной
    (llm.exclude) не отдаются."""
    router = bot.router
    out = []
    for name in router._get_provider_order():
        if router._is_excluded(name):
            continue
        cfg = router.available.get(name) or {}
        model = router.model_for(name)
        keys = [k for k in (cfg.get("api_keys") or []) if k]
        if not (model and keys and cfg.get("base_url")):
            continue
        out.append({"name": name, "base_url": str(cfg["base_url"]).rstrip("/"),
                    "model": model, "api_keys": keys})
    return out


def _seen_path(bot):
    return data_dir() / bot.context / "offline_imported.json"


def _reminders_seen_path(bot):
    return data_dir() / bot.context / "offline_reminders.json"


def import_reminders(bot, chat_id: str, user_id: str, items: list[dict],
                     user_name: Optional[str] = None) -> dict:
    """Напоминания, поставленные на телефоне без ноутбука → ReminderManager.

    items — [{"id": местный id, "task", "at": unix-секунды}]. Повтор того же
    id отдаёт прежний результат. Прошедшее (меньше чем через 30 с) не
    создаётся: телефон уже напомнил сам. → {"created": {местный id: id
    ядра}, "past": [местные id], "enabled": bool}."""
    rm = getattr(bot, "reminder_manager", None)
    if rm is None:
        return {"created": {}, "past": [], "enabled": False}
    path = _reminders_seen_path(bot)
    raw = load_json_safe(path, default={}, label="Offline reminders")
    seen = raw.get("map") if isinstance(raw, dict) and isinstance(raw.get("map"), dict) else {}
    created, past = {}, []
    now = time.time()
    for it in items:
        lid = str(it.get("id") or "")
        if not lid:
            continue
        if lid in seen:
            if seen[lid]:
                created[lid] = seen[lid]
            else:
                past.append(lid)
            continue
        at = float(it.get("at") or 0)
        if at <= now + 30:
            seen[lid] = None
            past.append(lid)
            continue
        task = (str(it.get("task") or "").strip() or None)
        r = rm.add_reminder(chat_id, user_name or "User", task, at - now, user_id=user_id)
        seen[lid] = r.get("id")
        created[lid] = r.get("id")
    if created or past:
        keep = dict(list(seen.items())[-_SEEN_KEEP:])
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"map": keep})
        logger.info(f"[Offline] {bot.context}: напоминаний с телефона — {len(created)}, "
                    f"прошедших {len(past)}")
    return {"created": created, "past": past, "enabled": True}


def import_messages(bot, chat_id: str, user_id: str, messages: list[dict],
                    user_name: Optional[str] = None) -> dict:
    """Офлайн-переписка → история чата и память. → {"imported", "skipped"}.

    Вызывать в потоке (пишет в Chroma) под локом чата. Сообщения — по
    времени; уже принятые (по id) пропускаются. Ход пользователя открыт на
    всю запись: фоновые сообщения между репликами не встают."""
    path = _seen_path(bot)
    raw = load_json_safe(path, default={}, label="Offline import")
    seen_list = [str(i) for i in (raw.get("ids") if isinstance(raw, dict) else None) or []]
    seen = set(seen_list)
    fresh = []
    for m in sorted(messages, key=lambda m: float(m.get("ts") or 0)):
        mid = str(m.get("id") or "")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        fresh.append(m)
    skipped = len(messages) - len(fresh)
    if fresh:
        key = bot.stm_key(chat_id, user_id)
        with bot.user_turn(key):
            for m in fresh:
                role = "user" if m.get("role") == "user" else "assistant"
                text = str(m.get("text") or "")[:MAX_TEXT]
                if not text.strip():
                    continue
                # Время с телефона, но не из будущего (часы телефона могут спешить)
                at = min(float(m.get("ts") or 0) or time.time(), time.time())
                if role == "user":
                    bot.memory.add_message("user", text, user_id, chat_id, user_name, timestamp=at)
                else:
                    bot.memory.add_message("assistant", text, user_id, chat_id, timestamp=at)
        seen_list = (seen_list + [str(m.get("id")) for m in fresh])[-_SEEN_KEEP:]
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"ids": seen_list})
        logger.info(f"[Offline] {bot.context}: принято {len(fresh)} офлайн-сообщений, повторов {skipped}")
    return {"imported": len(fresh), "skipped": skipped}

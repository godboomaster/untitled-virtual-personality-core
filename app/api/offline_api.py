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

Вернувшись, приложение отдаёт офлайн-переписку (import_messages): она встаёт
в историю чата обычным путём memory.add_message — с извлечением фактов, как
у живых сообщений. Факты появляются только здесь: телефон их не создаёт.
Повтор по id не записывается дважды (сеть оборвалась после записи).
"""

import logging
import time
from typing import Optional

from app.core.atomic_io import atomic_write_json, load_json_safe
from app.core.language import user_language_line
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

KIT_VERSION = 1
HISTORY_TAIL = 30   # сообщений переписки в ките
MAX_FACTS = 80      # фактов о собеседнике (последние)
MAX_IMPORT = 200    # сообщений за один приём
MAX_TEXT = 8000     # символов в одном сообщении
_SEEN_KEEP = 2000   # сколько принятых id помнить для защиты от повтора

OFFLINE_RULES = (
    "You are answering from the user's phone: the computer you normally run on is "
    "unreachable, so your full memory, tools and web search are unavailable right now. "
    "Stay fully in character and keep replies short and conversational. Don't claim to "
    "set reminders, add to-dos or inventory, search the web, open sites or control the "
    "computer — if asked, say you'll do it once you're back on the computer. Don't "
    "invent facts about the user beyond what is listed below."
)


def _persona_name(bot, persona_id: str) -> str:
    data = getattr(getattr(bot, "persona", None), "persona_data", None) or {}
    return str(data.get("name") or persona_id)


def build_kit(bot, persona_id: str, chat_id: str, user_id: str) -> dict:
    """Персона-кит для ответов без ноутбука (секретов в нём нет)."""
    key = bot.stm_key(chat_id, user_id)
    persona = getattr(bot, "persona", None)
    character = (getattr(persona, "system_prompt", "") or "").strip()

    try:
        facts = bot.memory.ltm.get_all_facts(user_id=user_id, chat_id=key)
    except Exception as e:
        logger.warning(f"[Offline] {persona_id}: факты для кита не собраны: {e}")
        facts = []
    facts = [f for f in facts if isinstance(f, str) and f.strip()][-MAX_FACTS:]

    try:
        tail = bot.memory.stm.get_last(HISTORY_TAIL, chat_id=key)
    except Exception as e:
        logger.warning(f"[Offline] {persona_id}: хвост переписки для кита не собран: {e}")
        tail = []
    history = [{"role": m.get("role"), "text": str(m.get("content") or ""),
                "ts": float(m.get("timestamp") or 0)}
               for m in tail if m.get("role") in ("user", "assistant") and m.get("content")]

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

    return {
        "version": KIT_VERSION,
        "persona": persona_id,
        "name": _persona_name(bot, persona_id),
        "generated_at": time.time(),
        # Целиком — для облачной модели; части — чтобы телефон собрал
        # короче для своей модели (меньше фактов — быстрее первое слово)
        "system": "\n\n".join(p for p in (character, OFFLINE_RULES, style, facts_block, language) if p),
        "parts": {"character": character, "rules": OFFLINE_RULES, "style": style or "",
                  "language": language},
        "facts": facts,
        "history": history,
    }


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

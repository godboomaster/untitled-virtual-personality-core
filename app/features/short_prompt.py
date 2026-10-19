"""Краткое описание персоны для маленькой модели (Gemma на телефоне).

Полный характер персоны (system_prompt) — тысячи символов с заголовками
капсом, разделителями и тонкими оговорками. Маленькая модель на телефоне
понимает его буквально и по кускам: обрезанный по символам характер
Коннора («Молчание — тоже ответ», «собираешь данные») превращался в «Я не
отвечаю на вопросы. Я СОБИРАЮ ДАННЫЕ». Поэтому для неё — отдельная
карточка на 800–1500 символов: кто персона, как говорит, чего нельзя, пара
примеров реплик.

Карточку один раз составляет сильная модель персоны (побочный канал, без
локальной — Ollama на ноутбуке тяжёлая, а маленькая сама плохо сожмёт) и
кладёт в data/<контекст>/short_prompt.json вместе с хэшем характера:
поменялся характер — карточка составится заново. Своё описание можно
задать в YAML персоны полем short_prompt — тогда модель не зовётся.
У персоны с режимами (<персона>_modes/master.yaml, mortal.yaml —
app/core/persona.py) карточка у каждого режима своя: по его характеру, в
short_prompt.<режим>.json (или поле short_prompt файла режима).
Составление — в фоне, по одной персоне за раз: пока карточки нет, телефон
обходится началом полного характера.
"""

import hashlib
import logging
import re
import threading
import time
from typing import Optional

from app.core.atomic_io import atomic_write_json, load_json_safe
from app.core.dialog_scope import dialog_scope
from app.core.language import language_name, persona_language
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

CARD_MIN = 200      # символов: короче — модель не справилась
CARD_MAX = 2500     # длиннее — обрезается по абзацу
RETRY_AFTER_SEC = 30 * 60   # не вышло — следующая попытка не раньше

CARD_PROMPT = (
    "Below is the full character description of a chat persona. Rewrite it as a compact "
    "card for a small 2-billion-parameter model that runs on a phone. Small models take "
    "every line literally, so the card must be short, concrete and unambiguous.\n\n"
    "Requirements:\n"
    "- 800-1500 characters, second person (\"You are ...\").\n"
    "- Plain text: no words or headings in capital letters, no separator lines, no markdown "
    "headings.\n"
    "- Structure: two or three sentences on who the persona is; then \"How you talk:\" with "
    "five or six short bullet points of concrete behaviour (what to do, not abstract "
    "traits); then \"Never:\" with three or four of the most important prohibitions; then "
    "\"Example replies:\" with two or three very short replies in the persona's typical style "
    "and length, each preceded by the situation in brackets.\n"
    "- Keep the persona's formatting conventions (for example *actions* in asterisks) only "
    "if they are central to the persona, and show them in the examples.\n"
    "- The persona always answers the user's latest message directly. Leave out traits a "
    "small model could turn into refusing to answer, hostility or silence.\n"
    "- Leave out tools, the computer, reminders, memory and the app.\n"
    "Reply with the card text only."
)

# Строки-разделители промпта (────, ====, ***) — в карточке не нужны
_SEPARATOR_RE = re.compile(r"^[\s\-─━═=_*~#]+$")
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_FENCE_RE = re.compile(r"^```[a-z]*\s*|\s*```$")

_gen_lock = threading.Lock()    # составление — по одной персоне за раз
_state_lock = threading.Lock()
_running: set[str] = set()      # контексты, для которых карточка составляется


def _file(context: str, mode: str = ""):
    return data_dir() / context / (f"short_prompt.{mode}.json" if mode else "short_prompt.json")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _mode_data(bot, mode: str) -> dict:
    modes = getattr(getattr(bot, "persona", None), "modes", None) or {}
    return (modes.get(mode) or {}) if mode else {}


def _character(bot, mode: str = "") -> str:
    if mode:
        return str(_mode_data(bot, mode).get("system_prompt") or "").strip()
    return str(getattr(getattr(bot, "persona", None), "system_prompt", "") or "").strip()


def _own(bot, mode: str = "") -> str:
    data = _mode_data(bot, mode) if mode else getattr(getattr(bot, "persona", None), "persona_data", None) or {}
    return str(data.get("short_prompt") or "").strip()


def clean_card(text: str) -> str:
    """Ответ модели → карточка: без рассуждений, обёртки ``` и разделителей;
    длиннее CARD_MAX — по последнему абзацу, что влез. Пусто — не вышло."""
    text = _THINK_RE.sub("", text or "")
    text = _FENCE_RE.sub("", text.strip()).strip()
    lines = [ln.rstrip() for ln in text.splitlines() if not _SEPARATOR_RE.match(ln)]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(text) > CARD_MAX:
        cut = text[:CARD_MAX]
        para = cut.rfind("\n\n")
        line = cut.rfind("\n")
        text = cut[:para] if para >= CARD_MAX // 2 else cut[:line] if line > 0 else cut
    return text if len(text) >= CARD_MIN else ""


def get_short_prompt(bot, start: bool = True, mode: Optional[str] = None) -> dict:
    """Краткое описание персоны для модели телефона →
    {text, source: 'yaml' | 'auto' | '', status, at, chars}:
    status — ready, pending (составляется), failed (не вышло, error — почему),
    none (характера нет). start — не готово, так начать составлять в фоне.
    mode — режим собеседника (persona.mode_for): карточка по его характеру;
    режима у персоны нет — общая."""
    mode = mode if mode and _character(bot, mode) else ""
    character = _character(bot, mode)
    chars = len(character)
    own = _own(bot, mode)
    if own:
        return {"text": own, "source": "yaml", "status": "ready", "at": 0, "chars": chars}
    if not character:
        return {"text": "", "source": "", "status": "none", "at": 0, "chars": 0}
    context = bot.context
    job = f"{context}:{mode}" if mode else context
    digest = _hash(character)
    saved = load_json_safe(_file(context, mode), {}, label="ShortPrompt")
    if not isinstance(saved, dict):
        saved = {}
    if saved.get("hash") == digest and saved.get("text"):
        return {"text": saved["text"], "source": "auto", "status": "ready",
                "at": float(saved.get("at") or 0), "chars": chars}
    with _state_lock:
        running = job in _running
    failed = saved.get("failed_hash") == digest
    retry = not failed or time.time() - float(saved.get("failed_at") or 0) >= RETRY_AFTER_SEC
    if start and not running and retry:
        _start(bot, context, character, digest, mode)
        running = True
    out = {"text": "", "source": "", "status": "pending" if running else "failed",
           "at": 0, "chars": chars}
    if failed and not running:
        out["error"] = str(saved.get("error") or "")
    return out


def _start(bot, context: str, character: str, digest: str, mode: str = "") -> None:
    job = f"{context}:{mode}" if mode else context
    with _state_lock:
        if job in _running:
            return
        _running.add(job)
    threading.Thread(target=_generate, args=(bot, context, character, digest, mode),
                     name=f"short-prompt-{job}", daemon=True).start()


def _generate(bot, context: str, character: str, digest: str, mode: str = "") -> None:
    path = _file(context, mode)
    job = f"{context}:{mode}" if mode else context
    try:
        with _gen_lock:
            name = language_name(persona_language(character))
            lang = (f"Write the card in {name}, the language of the description."
                    if name else "Write the card in the language of the description.")
            messages = [
                {"role": "system", "content": f"{CARD_PROMPT}\n\n{lang}"},
                {"role": "user", "content": character},
            ]
            answer = None
            error = ""
            try:
                # Без локальной модели: Ollama на ноутбуке рядом с ботом тяжела,
                # а маленькая модель сама плохо сожмёт характер. Свой тред
                # веб-чата у каждой карточки: в общем сайт помнит прошлые ходы,
                # и карточка режима смешалась бы с общей или с другим режимом
                with dialog_scope(f"short-prompt:{mode or 'persona'}"):
                    answer = bot.router.get_response(
                        messages, temperature=0.4, max_tokens=1200, top_p=0.9,
                        exclude_provider="local", timeout=180.0, webchat_channel="side")
            except Exception as e:
                error = str(e)[:200]
            card = clean_card(answer or "")
            saved = load_json_safe(path, {}, label="ShortPrompt")
            if not isinstance(saved, dict):
                saved = {}
            if card:
                saved = {"hash": digest, "text": card, "at": time.time()}
                logger.info(f"[ShortPrompt] {job}: краткое описание составлено "
                            f"({len(card)} из {len(character)} символов)")
            else:
                error = error or ("модель не ответила" if not answer else "ответ модели не подошёл")
                saved.update({"failed_hash": digest, "failed_at": time.time(), "error": error})
                logger.warning(f"[ShortPrompt] {job}: краткое описание не составлено — {error}")
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, saved)
    except Exception as e:
        logger.warning(f"[ShortPrompt] {job}: ошибка составления: {e}")
    finally:
        with _state_lock:
            _running.discard(job)

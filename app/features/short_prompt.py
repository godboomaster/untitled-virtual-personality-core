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
У персоны с режимами (<персона>_modes/master.yaml, mortal.yaml и режимы по
триггеру — app/core/persona.py, persona_modes.py) карточка у каждого режима
своя: по его характеру, в short_prompt.<режим>.json (или поле short_prompt
файла режима).
Составление — в фоне, по одной персоне за раз: пока карточки нет, телефон
обходится началом полного характера.

Окно «Краткое описание» (web/src/offline/ShortPromptModal.tsx) даёт два
пути: своё описание — вставить текст (его пишет в YAML
settings_api.set_persona_short_prompt), а промпт для любой нейросети
(manual_prompt — тот же, что уходит модели персоны) скопировать; или
«Составить заново» моделью персоны (compose). Телефон без ядра составляет
карточку сам, своей цепочкой провайдеров, и при связи отдаёт её сюда
(import_card): ядро не тратит на неё вызов модели.
"""

import hashlib
import logging
import re
import threading
import time
import zlib
from typing import Optional

from app.core.atomic_io import atomic_write_json, load_json_safe
from app.core.dialog_scope import dialog_scope
from app.core.language import language_name, persona_language
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

CARD_MIN = 200      # символов: короче — модель не справилась
CARD_MAX = 2500     # длиннее — обрезается по абзацу
OWN_MAX = 4000      # своё описание — не длиннее (столько характера берёт телефон)
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

# Промпт для своей нейросети: системная часть и характер одним текстом —
# как у промптов скинов (web/src/skins/manualPrompt.ts, PROMPT_JOINER)
MANUAL_JOINER = "\n\n---\n\n"

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


def character_crc(text: str) -> int:
    """crc32 характера — им телефон помечает, по какому характеру составил
    карточку (crc32 в web/src/offline/localPersonas.ts — тот же, по UTF-8)."""
    return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF


def card_messages(character: str) -> list[dict]:
    """Запрос карточки: системная часть с языком описания + характер."""
    name = language_name(persona_language(character))
    lang = (f"Write the card in {name}, the language of the description."
            if name else "Write the card in the language of the description.")
    return [
        {"role": "system", "content": f"{CARD_PROMPT}\n\n{lang}"},
        {"role": "user", "content": character},
    ]


def manual_prompt(character: str) -> str:
    """Тот же запрос одним текстом — скопировать в любую нейросеть."""
    if not character:
        return ""
    return MANUAL_JOINER.join(m["content"] for m in card_messages(character))


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


def get_short_prompt(bot, start: bool = True, mode: Optional[str] = None,
                     with_prompt: bool = False) -> dict:
    """Краткое описание персоны для модели телефона →
    {text, source: 'yaml' | 'auto' | '', status, at, chars, composing}:
    status — ready, pending (составляется), failed (не вышло, error — почему),
    none (характера нет); composing — готовое есть, а модель составляет
    новое («Составить заново»; не вышло — error при ready). start — не
    готово, так начать составлять в фоне. mode — режим собеседника
    (persona.mode_for): карточка по его характеру; режима у персоны нет —
    общая. with_prompt — и промпт для своей нейросети (prompt)."""
    mode = mode if mode and _character(bot, mode) else ""
    character = _character(bot, mode)
    chars = len(character)
    extra = {"prompt": manual_prompt(character)} if with_prompt else {}
    own = _own(bot, mode)
    if own:
        return {"text": own, "source": "yaml", "status": "ready", "at": 0, "chars": chars,
                "composing": False, **extra}
    if not character:
        return {"text": "", "source": "", "status": "none", "at": 0, "chars": 0,
                "composing": False, **extra}
    context = bot.context
    job = f"{context}:{mode}" if mode else context
    digest = _hash(character)
    saved = load_json_safe(_file(context, mode), {}, label="ShortPrompt")
    if not isinstance(saved, dict):
        saved = {}
    with _state_lock:
        running = job in _running
    failed = saved.get("failed_hash") == digest
    if saved.get("hash") == digest and saved.get("text"):
        out = {"text": saved["text"], "source": "auto", "status": "ready",
               "at": float(saved.get("at") or 0), "chars": chars, "composing": running, **extra}
        # «Составить заново» не вышло — прежняя карточка остаётся, видно почему
        if failed and not running and float(saved.get("failed_at") or 0) > out["at"]:
            out["error"] = str(saved.get("error") or "")
        return out
    retry = not failed or time.time() - float(saved.get("failed_at") or 0) >= RETRY_AFTER_SEC
    if start and not running and retry:
        _start(bot, context, character, digest, mode)
        running = True
    out = {"text": "", "source": "", "status": "pending" if running else "failed",
           "at": 0, "chars": chars, "composing": running, **extra}
    if failed and not running:
        out["error"] = str(saved.get("error") or "")
    return out


def compose(bot) -> dict:
    """«Составить заново»: модель персоны составляет карточку по общему
    характеру, даже если готовая есть (она остаётся, пока новая не готова).
    Своё описание сначала убирает вызывающий (settings_api) — иначе модель
    не нужна. → то же, что get_short_prompt."""
    character = _character(bot)
    if character and not _own(bot):
        _start(bot, bot.context, character, _hash(character))
    return get_short_prompt(bot, start=False, with_prompt=True)


def import_card(bot, text: str, crc: int) -> Optional[dict]:
    """Карточка, составленная на телефоне без ядра (его цепочкой провайдеров):
    crc — crc32 характера, по которому составлена. Характер на ядре другой —
    None (составит ядро); текст не похож на карточку — ValueError."""
    character = _character(bot)
    if not character or character_crc(character) != int(crc):
        return None
    card = clean_card(text)
    if not card:
        raise ValueError("текст не похож на краткое описание")
    path = _file(bot.context)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"hash": _hash(character), "text": card, "at": time.time(), "from": "phone"})
    logger.info(f"[ShortPrompt] {bot.context}: краткое описание с телефона ({len(card)} символов)")
    return get_short_prompt(bot, start=False, with_prompt=True)


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
            messages = card_messages(character)
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

"""
Слой «Состояние» (State Engine) — внутренняя жизнь персоны между сообщениями.

Модель данных (по каждому чату, персона фиксирована инстансом бота):
  persona_state: energy (0-100), mood {valence, arousal, tag},
                 pastime, location, last_tick_at, updated_at,
                 spot/pose/pastime_since (комната в вебе, app/core/room.py)
  offline_log (append-only): type (state_change | world_event |
                 external_stimulus), payload, consumed

Тик-цикл: каждые tick_interval_minutes для каждого известного чата Gemma
механически обновляет параметры состояния (STATE_TICK_PROMPT) и оценивает
повод написать пользователю (INITIATIVE_SCORE_PROMPT). Если Gemma
недоступна — детерминированный эвристический дрейф: энергия падает днём и
восстанавливается ночью, mood тянется к baseline_mood из выжимки персонажа.
Без LLM система продолжает жить, просто скучнее.

Ключевой принцип: Gemma работает с persona_context (выжимкой), не с полным
system_prompt. Любой текст, который видит пользователь, генерирует основная
LLM — движок состояния только решает «что произошло».
"""

import json
import logging
import random
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.core import timeutil
from app.core.atomic_io import atomic_write_json, load_json_safe
from app.core.config import get_db_paths
from app.core.local_router import get_local_router
from app.core.retention import CHAT_RETENTION_DAYS, RetentionTimer, prune_stale
from app.core import room
from app.core.language import user_language_line

logger = logging.getLogger(__name__)

DEFAULT_TICK_MINUTES = 20          # разумный диапазон — 15-30 мин
INITIATIVE_THRESHOLD = 0.62        # порог скоринга инициативы
MAX_OFFLINE_LOG = 500              # append-only, но не бесконечно
OFFLINE_LOG_TTL_DAYS = 14          # consumed-записи старше — вычищаются

# Настроение дня: цель дрейфа mood — baseline персоны плюс сдвиг, стабильный
# в пределах суток (хороший / обычный / тяжёлый день). Без него mood за пару
# тиков схлопывался в baseline и персона звучала одинаково изо дня в день.
DAY_MOOD_SPREAD = 0.18             # σ дневного сдвига valence
DAY_MOOD_MAX = 0.35                # |сдвиг valence| не больше
DAY_LABEL_DELTA = 0.12             # сдвиг больше — день «хороший»/«тяжёлый»
MOOD_DRIFT = 0.2                   # доля пути к цели за эвристический тик
MOOD_TICK_MAX_STEP = 0.2           # тик Gemma двигает valence не дальше
MOOD_SETTLED = 0.1                 # |valence − цель| меньше — событийный тег гаснет
MOOD_LOG_DELTA = 0.1               # сдвиг valence, достойный offline_log
MOOD_TREND_DELTA = 0.03            # меньше — trend «flat»

_WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday"]

# Дефолты состояния/эвристики — на языке пользователя чата (они же
# показываются в комнате и попадают в промпты); без языка — английский
_DEFAULT_MOOD_TAG = {"ru": "спокойствие", "en": "calm"}
_DEFAULT_PASTIME = {"ru": "наблюдает за происходящим", "en": "watching what is going on"}
_DEFAULT_LOCATION = {"ru": "своё обычное место", "en": "their usual place"}
_SLEEP_PASTIME = {"ru": "спит", "en": "sleeping"}
_SLEEP_PASTIMES = set(_SLEEP_PASTIME.values())
_HEURISTIC_PASTIMES = {
    "ru": ["занят своими делами", "наблюдает за происходящим",
           "перебирает накопившиеся мысли", "реставрирует порядок вокруг себя"],
    "en": ["busy with their own things", "watching what is going on",
           "sorting through accumulated thoughts", "tidying up around themselves"],
}
_HEURISTIC_PASTIMES_PRIMITIVE = {
    "ru": ["спит", "грызёт игрушку", "обнюхивает углы",
           "смотрит в окно", "точит когти", "ворочается в подстилке"],
    "en": ["sleeping", "gnawing a toy", "sniffing the corners",
           "looking out the window", "sharpening claws", "tossing in the bedding"],
}
# Guard mood.tag по behavioral_rules: основы слов тега и запретов (ru/en)
_GUARD_TAG_STEMS = ["устал", "раздраж", "скука", "обижен", "грустит",
                    "tired", "irritat", "annoy", "bored", "offend", "sad"]
_GUARD_RULE_STEMS = ["устал", "раздраж", "скуку", "обиж", "груст",
                     "tired", "fatigue", "irritat", "annoy", "bored", "boredom",
                     "offend", "sad"]


def _lang_key(user_language: Optional[str]) -> str:
    return "ru" if user_language == "ru" else "en"


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# Хвост-пояснение тега: скобки, двоеточие, тире, связки «и/and/but»
_TAG_TAIL = re.compile(r"\s*(?:[(\[;:—–]|\s-\s|\s(?:и|но|and|but|with)\s)", re.I)


def short_mood_tag(tag) -> str:
    """Тег mood — 1-3 слова. Выжимка персоны и тик-модель любят описания
    («Normal day (friendly, no big deal either way)», «Машинное спокойствие
    и аналитическая сосредоточенность») — в комнате и промпте это шум, а
    дрейф тащил такое описание в тег навсегда."""
    s = " ".join(str(tag or "").split()).strip(" .,!\"'«»")
    if not s:
        return ""
    head = _TAG_TAIL.split(s, maxsplit=1)[0].strip(" .,") or s
    words = head.split()[:3]
    out = " ".join(words).strip(" .,")[:40]
    # «Нейтрально» / «Calm» → строчная, кроме аббревиатур
    if len(out) > 1 and not out[:2].isupper():
        out = out[0].lower() + out[1:]
    return out


def day_mood(seed_key: str, baseline: Optional[dict]) -> dict:
    """Цель дрейфа mood на сегодня: baseline + дневной сдвиг. Детерминирован
    по (персона, чат, дата) — все тики суток тянут к одной точке, и после
    рестарта день остаётся тем же."""
    baseline = baseline or {}
    v0 = _clamp(_num(baseline.get("valence"), 0.0), -1.0, 1.0)
    a0 = _clamp(_num(baseline.get("arousal"), 0.3), 0.0, 1.0)
    date = timeutil.now().date().isoformat()
    rng = random.Random(f"{seed_key}:{date}")
    dv = _clamp(rng.gauss(0.0, DAY_MOOD_SPREAD), -DAY_MOOD_MAX, DAY_MOOD_MAX)
    da = _clamp(rng.gauss(0.0, 0.08), -0.15, 0.15)
    label = ("good" if dv >= DAY_LABEL_DELTA else
             "rough" if dv <= -DAY_LABEL_DELTA else "ordinary")
    return {
        "date": date,
        "label": label,
        "baseline": round(v0, 2),
        "valence": round(_clamp(v0 + dv, -1.0, 1.0), 2),
        "arousal": round(_clamp(a0 + da, 0.0, 1.0), 2),
        "tag": short_mood_tag(baseline.get("tag")),
    }


def _set_trend(mood: dict, prev_valence) -> None:
    # Направление последнего сдвига valence — для «тренда» в вебе
    delta = _num(mood.get("valence"), 0.0) - _num(prev_valence, 0.0)
    mood["trend"] = ("up" if delta >= MOOD_TREND_DELTA else
                     "down" if delta <= -MOOD_TREND_DELTA else "flat")


# ── Промпты ────────────────────────────────────────────────────────────

STATE_TICK_PROMPT = """You are the simulation engine of a character's inner state.
Your task is to update the parameters mechanically, NOT to compose literary text.

Return STRICTLY JSON:
{{
  "energy": <int 0-100>,
  "mood": {{"valence": <float -1..1>, "arousal": <float 0..1>, "tag": "<1-2 words>"}},
  "pastime": "<string>",
  "location": "<string>",
  "internal_note": "<1 short phrase, a draft, not to be shown to the user>"
}}

CONTEXT:
Current state: {state}
Time: {daytime}, {weekday}
Character's routine at this time of day: {routine}
Personality (brief): {personality_summary}
Baseline temperament: {baseline_mood}
Today is {day_label} day — the mood drifts to: {day_mood}
Forbidden manifestations: {behavioral_rules}
Active storylines: {storylines}
Latest world fact: {last_world_fact}

RULES:
- energy drops during the day (faster with active pastimes), recovers at night/during rest
- pastime/location follow the routine: at night the character is almost always asleep,
  unless the events give a reason otherwise
- mood drifts slowly (at most ~0.2 valence per tick) to today's mood; a recent
  push from an event or the conversation fades gradually, not at once
- mood.tag is 1-2 plain words (calm, cheerful, wary, tired), not a description;
  it must match the valence and must not violate the forbidden manifestations (for example, if the character is forbidden
  to "show irritation/tiredness" — do not name that state directly,
  only indirect numeric shifts)
- pastime does not change every tick — it usually holds for 1-3 ticks in a row
- do not invent new NPCs/places — use only what is given in the context"""

INITIATIVE_SCORE_PROMPT = """Rate from 0 to 1: how much of a reason the character has right now to write to the user.
Consider: time since the last message, change of mood/pastime, presence of an
unread fact from the character's life, the usual frequency of communication of this pair.

Return JSON: {{"score": <float 0..1>, "reason": "<short explanation>"}}

Context:
Time since the user's last message: {silence_hours:.1f} h
Current state: {state}
Previous state: {prev_state}
Unconsumed life facts: {unconsumed_count}
Hours since the last initiative: {since_initiative:.1f}
{language_line}"""

# Суффикс к тик-промпту для объединённого вызова: один Gemma-вызов на
# чат/тик вместо двух. Настройки proactive из yaml передаются в скоринг.
_INITIATIVE_SUFFIX = """
Additionally rate from 0 to 1: how much of a reason the character has right now to write to the user.
Consider: time since the last message, change of mood/pastime, presence of
unread facts from the character's life, the usual frequency of communication of this pair
and the persona's initiative settings.

Time since the user's last message: {silence_hours:.1f} h
Hours since the last initiative: {since_initiative:.1f}
Unconsumed life facts: {unconsumed_count}
Persona's initiative settings (yaml proactive): {proactive_settings}

Add the fields "initiative_score": <float 0..1> and "initiative_reason": "<brief>" to the response JSON."""

# Суффикс к тик-промпту: место в комнате (веб «Комната») тем же вызовом.
# Список мест — из room: персоны + места вокруг предметов + away.
_SPOT_SUFFIX = """

Additionally state where the character is right now in their room. Add the fields
"spot": "<spot key STRICTLY from the list>" and "pose": "<stand|sit|read|write|look|sleep|away>" to the response JSON.
Spots:
{spots}
- spot is consistent with pastime/location: sleeping — the bed (if there is one), reading — the shelf, etc.
- away — the character is not in the room (went out on business, location outside home), then pose: away
- the spot changes together with the pastime, not every tick"""

# Вариант для primitive: состояние — чисто физическое, pastime — действие,
# internal_note — сенсорное впечатление без рефлексии
STATE_TICK_PROMPT_PRIMITIVE = """You are the engine of a simple physical state of a creature (not human in its type of thinking).
Update the parameters mechanically, do NOT compose text. Return STRICTLY JSON:
{{
  "energy": <int 0-100>,
  "mood": {{"valence": <float -1..1>, "arousal": <float 0..1>, "tag": "<1-2 words: sleepy, content, scared>"}},
  "pastime": "<a physical action: sleeping, gnawing a toy, sniffing the corners, looking out the window>",
  "location": "<where it is>",
  "internal_note": "<a sensory impression up to 5 words: warm, noisy, smells of food>"
}}

CONTEXT:
Current state: {state}
Time: {daytime}, {weekday}
Who the creature is (brief): {personality_summary}
What was happening nearby: {storylines}
Latest fact: {last_world_fact}

RULES:
- energy drops from activity during the day, recovers during sleep/rest
- mood drifts to calm; tag is a simple word, without abstractions
- pastime is only a physical action, holds for 1-3 ticks
- do not invent new places"""

_NONE = "(none)"


def _daytime_en() -> str:
    # Время суток для промптов (англ.); _daytime() — ключ распорядка персоны
    h = timeutil.now().hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 18:
        return "afternoon"
    if 18 <= h < 23:
        return "evening"
    return "night"


def _language_tail(user_language: Optional[str]) -> str:
    return "\n\n" + user_language_line(user_language)


def _daytime() -> str:
    # Ключ распорядка персоны (daily_routine: утро/день/вечер/ночь)
    h = timeutil.now().hour
    if 5 <= h < 12:
        return "утро"
    if 12 <= h < 18:
        return "день"
    if 18 <= h < 23:
        return "вечер"
    return "ночь"


def _now_iso() -> str:
    # timeutil.now(), а не datetime.now(): значение позже обратно переводится
    # в epoch через timeutil.to_ts (см. _prune_log/entries_since) — писать и
    # читать нужно одним и тем же поясом, иначе naive .timestamp() молча
    # съедет на разницу между TIMEZONE и системным поясом.
    return timeutil.now().isoformat(timespec="seconds")


class StateEngine:
    # Хранение + тики состояния. Потокобезопасен (RLock), файлы — JSON.

    def __init__(self, context: str, persona_name: str,
                 tick_interval_minutes: int = DEFAULT_TICK_MINUTES,
                 primitive: bool = False, use_gemma: bool = True):
        self.context = context
        self.persona_name = persona_name
        self.tick_interval_minutes = tick_interval_minutes
        # primitive: физическое состояние без рефлексии — свой тик-промпт
        # и запрет вербализации состояния в диалоге
        self.primitive = primitive
        # use_gemma=false (features.state_engine.use_gemma): только
        # эвристический дрейф, локальная модель не дёргается вовсе
        self.use_gemma = use_gemma
        self.local = get_local_router(context)
        self._lock = threading.RLock()
        # Счётчики для наблюдаемости (in-memory, снапшот — get_state_for_ui)
        self.stats = {"ticks_gemma": 0, "ticks_heuristic": 0}
        # Дозор: не чаще раза в RETENTION_TICK_HOURS перезапускать
        # прореживание из tick()/tick_and_score() (см. _maybe_prune_stale)
        self._retention_timer = RetentionTimer()

        db = get_db_paths(context)
        base = Path(db["stm"]).parent / "living"
        base.mkdir(parents=True, exist_ok=True)
        self._state_file = base / "state.json"
        self._log_file = base / "offline_log.json"

        self._states: Dict[str, dict] = load_json_safe(
            self._state_file, {"chats": {}}, label="StateEngine")["chats"]
        log_data = load_json_safe(
            self._log_file, {"entries": [], "next_id": 1}, label="StateEngine")
        self._log: List[dict] = log_data["entries"]
        self._next_id: int = log_data["next_id"]

        # Чат давно неактивен — состояние для него не нужно хранить вечно
        # (app.core.retention). Прогоняем сразу при загрузке, чтобы
        # разовые/заброшенные чаты не таскались тиком (_tick_all — по
        # объединению _states.keys() и known_chats) годами.
        if self._prune_stale_states():
            self._save_state()

    def _prune_stale_states(self) -> list:
        """last_seen — updated_at состояния (тот же маркер, что рефрешат
        tick/apply_mood_impact): метки нет (легаси-запись, созданная до
        ретенции) — НЕ трогаем, пока следующая запись её не проставит."""
        def _seen(chat_id, state):
            ts = state.get("updated_at") if isinstance(state, dict) else None
            if not ts:
                return None
            try:
                return timeutil.to_ts(datetime.fromisoformat(ts))
            except (ValueError, TypeError):
                return None
        return prune_stale(self._states, _seen, CHAT_RETENTION_DAYS, label="StateEngine")

    def _maybe_prune_stale(self):
        """Повтор ретенции на живущем процессе: без него прунились бы
        только при загрузке, а процесс месяцами не перезапускается — разовые
        чаты копились бы между рестартами бессрочно. Зовётся из tick()/
        tick_and_score() — периодического тик-цикла living_persona, НЕ из
        обработчика входящего сообщения. Дозор гасит частоту до раза в
        RETENTION_TICK_HOURS; лок держим только на мутацию словаря, save —
        уже вне лока."""
        if not self._retention_timer.due():
            return
        with self._lock:
            removed = self._prune_stale_states()
        if removed:
            self._save_state()

    # ── Хранение (app.core.atomic_io: atomic-запись с fsync, ошибки
    # идут в лог, а не проглатываются молча) ──────────────────────────

    def _save_state(self):
        try:
            atomic_write_json(self._state_file, {"chats": self._states})
        except Exception as e:
            logger.error(f"[StateEngine] Ошибка сохранения state: {e}")

    def _save_log(self):
        try:
            atomic_write_json(self._log_file, {"entries": self._log, "next_id": self._next_id})
        except Exception as e:
            logger.error(f"[StateEngine] Ошибка сохранения offline_log: {e}")

    # ── Публичный API ────────────────────────────────────

    def get_state(self, chat_id: str) -> dict:
        # Состояние чата (создаёт дефолтное при первом обращении).
        with self._lock:
            return self._ensure_state(chat_id)

    def _ensure_state(self, chat_id: str,
                      user_language: Optional[str] = None) -> dict:
        state = self._states.get(chat_id)
        if state is None:
            lk = _lang_key(user_language)
            state = {
                "energy": 78,
                "mood": {"valence": 0.0, "arousal": 0.3, "tag": _DEFAULT_MOOD_TAG[lk],
                         "trend": "flat"},
                "pastime": _DEFAULT_PASTIME[lk],
                "location": _DEFAULT_LOCATION[lk],
                "spot": "desk",
                "pose": "sit",
                "pastime_since": time.time(),
                "last_tick_at": time.time(),
                "updated_at": _now_iso(),
            }
            self._states[chat_id] = state
            self._save_state()
        return state

    def apply_mood_impact(self, chat_id: str, valence_delta: float, tag: str):
        """Толчок mood от события мира / диалога / игнора. Возврат к
        настроению дня доделают тики — постепенно (MOOD_DRIFT), событие
        только толкает. Эмоциональный толчок заодно слегка будоражит."""
        with self._lock:
            state = self._ensure_state(chat_id)
            mood = state["mood"]
            delta = _num(valence_delta, 0.0)
            prev_valence = _num(mood.get("valence"), 0.0)
            mood["valence"] = round(_clamp(prev_valence + delta, -1.0, 1.0), 2)
            mood["arousal"] = round(_clamp(
                _num(mood.get("arousal"), 0.3) + abs(delta) * 0.5, 0.0, 1.0), 2)
            tag = short_mood_tag(tag)
            if tag:
                mood["tag"] = tag
            _set_trend(mood, prev_valence)
            state["updated_at"] = _now_iso()
            self._save_state()

    # ── Offline log ──────────────────────────────────────

    def log_event(self, chat_id: str, entry_type: str, payload: dict) -> int:
        # Append-only запись в offline_log. Возвращает id записи.
        with self._lock:
            entry = {
                "id": self._next_id,
                "chat_id": str(chat_id),
                "timestamp": _now_iso(),
                "type": entry_type,  # state_change | world_event | external_stimulus
                "payload": payload,
                "consumed": False,
            }
            self._next_id += 1
            self._log.append(entry)
            self._prune_log()
            self._save_log()
            return entry["id"]

    def _prune_log(self):
        cutoff = time.time() - OFFLINE_LOG_TTL_DAYS * 86400
        fresh = []
        for e in self._log:
            try:
                ts = timeutil.to_ts(datetime.fromisoformat(e["timestamp"]))
            except (ValueError, TypeError):
                ts = time.time()
            if e.get("consumed") and ts < cutoff:
                continue
            fresh.append(e)
        if len(fresh) > MAX_OFFLINE_LOG:
            fresh = fresh[-MAX_OFFLINE_LOG:]
        if len(fresh) != len(self._log):
            self._log = fresh

    def unconsumed(self, chat_id: str, limit: int = 20) -> List[dict]:
        with self._lock:
            return [e for e in self._log
                    if e["chat_id"] == str(chat_id) and not e.get("consumed")][-limit:]

    def life_facts_count(self, chat_id: str) -> int:
        """Непотреблённые факты жизни для скоринга инициативы. Сигналы
        комнаты (room_signal: «заглянул», фокус-сессия) — не факты жизни
        персоны: они не должны толкать её написать первой (особенно посреди
        совместной работы)."""
        return sum(1 for e in self.unconsumed(chat_id)
                   if e.get("type") != "room_signal")

    def recent_entries(self, chat_id: str, limit: int = 10) -> List[dict]:
        """Последние записи чата НЕЗАВИСИМО от consumed (лента комнаты):
        после дневника/инициативы факты остаются видимыми (приглушённо),
        а не схлопываются в моки. consumed-флаг отдаём клиенту."""
        with self._lock:
            return [dict(e) for e in self._log
                    if e["chat_id"] == str(chat_id)][-limit:]

    def mark_consumed(self, entry_ids: List[int]):
        if not entry_ids:
            return
        ids = set(entry_ids)
        with self._lock:
            for e in self._log:
                if e["id"] in ids:
                    e["consumed"] = True
            self._save_log()

    def entries_since(self, chat_id: str, since_ts: float) -> List[dict]:
        """Записи лога с заданного времени (для приветствия-дневника).
        consumed-записи пропускаем: уже озвученные (дневник/инициатива)
        факты не пересобираются в приветствие повторно."""
        with self._lock:
            out = []
            for e in self._log:
                if e["chat_id"] != str(chat_id) or e.get("consumed"):
                    continue
                try:
                    ts = timeutil.to_ts(datetime.fromisoformat(e["timestamp"]))
                except (ValueError, TypeError):
                    continue
                if ts >= since_ts:
                    out.append(e)
            return out

    # ── Тик состояния ─────────────────────────────────────

    def tick(self, chat_id: str, persona_context: dict,
             storylines: Optional[List] = None,
             last_world_fact: str = "",
             known_places: Optional[List[str]] = None,
             spots: Optional[List[dict]] = None,
             user_language: Optional[str] = None) -> dict:
        """Один тик: обновляет состояние, пишет diff в offline_log.
        Возвращает новое состояние. Синхронный LLM-вызов — звать из потока.
        Для primitive storylines — строки об окружении (предметы/факты).
        known_places — известные места мира: location вне списка мягко
        откатывается к прежнему (санитайзер дрейфа мира).
        spots — допустимые места комнаты (room.allowed_spots): модель
        выбирает spot/pose тем же вызовом, санитайзер держит их в списке.
        user_language — язык пользователя чата: свободный текст состояния
        (pastime/tag/note) пишется на нём."""
        with self._lock:
            prev = dict(self._ensure_state(chat_id, user_language))
        day = self.day_mood(chat_id, persona_context)
        new_state = self._tick_via_gemma(
            chat_id, prev, persona_context, storylines or [], last_world_fact,
            spots=spots, user_language=user_language, day=day)
        if new_state is None:
            new_state = self._heuristic_tick(prev, persona_context, spots=spots,
                                             user_language=user_language, day=day)
        result = self._commit_tick(chat_id, prev, new_state, persona_context,
                                   known_places=known_places, spots=spots, day=day)
        self._maybe_prune_stale()
        return result

    def tick_and_score(self, chat_id: str, persona_context: dict,
                       storylines: Optional[List] = None,
                       last_world_fact: str = "", silence_hours: float = 0.0,
                       since_initiative_hours: float = 24.0,
                       proactive_settings: Optional[dict] = None,
                       known_places: Optional[List[str]] = None,
                       spots: Optional[List[dict]] = None,
                       user_language: Optional[str] = None) -> tuple:
        """Тик состояния + скоринг инициативы ОДНИМ Gemma-вызовом — вдвое
        меньше локальных вызовов на чат/тик. Настройки proactive из yaml
        уходят в промпт скоринга. known_places — санитайзер location
        (см. tick). Возвращает (new_state, score 0..1). Без Gemma —
        эвристика обоих."""
        with self._lock:
            prev = dict(self._ensure_state(chat_id, user_language))
        day = self.day_mood(chat_id, persona_context)
        new_state, score = None, None
        if self.use_gemma and self.local.is_available(task="state_engine"):
            try:
                prompt = self._build_tick_prompt(
                    prev, persona_context, storylines or [], last_world_fact,
                    spots=spots, day=day)
                prompt += _INITIATIVE_SUFFIX.format(
                    silence_hours=silence_hours,
                    since_initiative=since_initiative_hours,
                    unconsumed_count=self.life_facts_count(chat_id),
                    proactive_settings=json.dumps(
                        proactive_settings or {}, ensure_ascii=False))
                prompt += _language_tail(user_language)
                response = self.local.get_response(
                    messages=[
                        {"role": "system", "content": "You return only valid JSON without explanations."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=410 if spots else 380,
                    task="state_engine",
                )
                if response:
                    from app.core.persona_context import _extract_json
                    data = _extract_json(response)
                    if data and isinstance(data.get("mood"), dict):
                        new_state = self._state_from_gemma(data, prev)
                        score = self._parse_score(data)
            except Exception as e:
                logger.warning(f"[StateEngine] Тик+скоринг Gemma не удался: {e}")
        if new_state is None:
            new_state = self._heuristic_tick(prev, persona_context, spots=spots,
                                             user_language=user_language, day=day)
        state = self._commit_tick(chat_id, prev, new_state, persona_context,
                                  known_places=known_places, spots=spots, day=day)
        if score is None:
            score = self._heuristic_score(chat_id, silence_hours,
                                          since_initiative_hours)
        self._maybe_prune_stale()
        return state, score

    def day_mood(self, chat_id: str, persona_context: Optional[dict]) -> dict:
        # Настроение дня этого чата (см. модульную day_mood)
        return day_mood(f"{self.context}:{chat_id}",
                        (persona_context or {}).get("baseline_mood"))

    @staticmethod
    def _parse_score(data: dict) -> Optional[float]:
        try:
            return max(0.0, min(1.0, float(data["initiative_score"])))
        except (KeyError, TypeError, ValueError):
            return None

    def _heuristic_score(self, chat_id: str, silence_hours: float,
                         since_initiative_hours: float) -> float:
        # Скоринг без LLM: молчание + непотреблённые факты + давность инициативы.
        score = 0.0
        score += min(0.35, silence_hours / 24.0)
        score += min(0.30, self.life_facts_count(chat_id) * 0.10)
        if since_initiative_hours > 6:
            score += 0.10
        return max(0.0, min(1.0, score))

    def _commit_tick(self, chat_id: str, prev: dict, new_state: dict,
                     persona_context: Optional[dict] = None,
                     known_places: Optional[List[str]] = None,
                     spots: Optional[List[dict]] = None,
                     day: Optional[dict] = None) -> dict:
        """Постобработка тика: guard mood.tag по behavioral_rules,
        санитайзер location по известным местам мира, spot/pose по местам
        комнаты, pastime_since, штампы времени, сохранение, осмысленный
        diff → offline_log."""
        # mood.tag не должен нарушать behavioral_rules: грубая проверка —
        # если tag пересекается с запрещёнными словами, оставляем прежний tag
        prev_mood = prev.get("mood", {})
        forbidden_words = " ".join(
            (persona_context or {}).get("behavioral_rules") or []).lower()
        tag = (new_state.get("mood") or {}).get("tag", "")
        if tag and forbidden_words and any(
                w and w in tag.lower() for w in _GUARD_TAG_STEMS
        ) and any(w in forbidden_words for w in _GUARD_RULE_STEMS):
            new_state["mood"]["tag"] = prev_mood.get("tag", _DEFAULT_MOOD_TAG["en"])
        _set_trend(new_state["mood"], prev_mood.get("valence"))
        # Настроение дня — в состоянии: блок промпта и веб показывают
        # хороший/тяжёлый день и отклонение от обычного
        new_state["mood_day"] = day or self.day_mood(chat_id, persona_context)

        # Location вне известных мест мира — откат к прежней: инструкция
        # «не выдумывай места» в промпте не enforced, а дрейф location
        # размывает мир (новые места появляются только через WorldEngine —
        # засев из промпта и детекцию из диалога)
        if known_places:
            loc = str(new_state.get("location", "")).strip()
            prev_loc = str(prev.get("location", ""))
            if loc and loc != prev_loc and not any(
                    loc.lower() in p.lower() or p.lower() in loc.lower()
                    for p in known_places):
                new_state["location"] = prev_loc

        # Место в комнате: только из допустимых (встроенные + предметы +
        # away); pastime_since — момент смены занятия, не каждого тика
        room.sanitize_spot(new_state, prev, spots)
        if (new_state.get("pastime") != prev.get("pastime")
                or not prev.get("pastime_since")):
            new_state["pastime_since"] = (
                time.time() if new_state.get("pastime") != prev.get("pastime")
                else float(prev.get("last_tick_at") or time.time()))
        else:
            new_state["pastime_since"] = prev["pastime_since"]

        new_state["last_tick_at"] = time.time()
        new_state["updated_at"] = _now_iso()

        with self._lock:
            self._states[str(chat_id)] = new_state
            self._save_state()

        diff = self._state_diff(prev, new_state)
        # Чистый дрейф energy без других сдвигов — шум: его никто не читает
        # (суммаризатор берёт pastime/location/mood, лента UI — события),
        # а unconsumed-счётчик раздувает скоринг инициативы и длину лога
        if {k for k in diff if k != "energy"}:
            self.log_event(chat_id, "state_change", {"diff": diff})
        engine = new_state.get("engine")
        if engine in ("gemma", "heuristic"):
            self.stats[f"ticks_{engine}"] = self.stats.get(f"ticks_{engine}", 0) + 1
        return new_state

    def _build_tick_prompt(self, prev: dict, persona_context: dict,
                           storylines: List, last_world_fact: str,
                           spots: Optional[List[dict]] = None,
                           day: Optional[dict] = None) -> str:
        prompt = self._build_tick_prompt_base(prev, persona_context, storylines,
                                              last_world_fact, day=day)
        if spots:
            prompt += _SPOT_SUFFIX.format(spots=room.spots_prompt_lines(spots))
        return prompt

    def _build_tick_prompt_base(self, prev: dict, persona_context: dict,
                                storylines: List, last_world_fact: str,
                                day: Optional[dict] = None) -> str:
        if self.primitive:
            # storylines для primitive — просто строки об окружении
            # (предметы/недавние факты), их LivingPersona передаёт как строки
            surroundings = "; ".join(str(s) for s in list(storylines)[:8]) or _NONE
            return STATE_TICK_PROMPT_PRIMITIVE.format(
                state=json.dumps(prev, ensure_ascii=False),
                daytime=_daytime_en(),
                weekday=_WEEKDAYS_EN[timeutil.now().weekday()],
                personality_summary=(persona_context or {}).get("personality_summary", "")[:300],
                storylines=surroundings,
                last_world_fact=last_world_fact or _NONE,
            )
        day = day or day_mood("", (persona_context or {}).get("baseline_mood"))
        baseline = dict((persona_context or {}).get("baseline_mood") or {})
        if baseline.get("tag"):
            baseline["tag"] = short_mood_tag(baseline["tag"])
        return STATE_TICK_PROMPT.format(
            state=json.dumps(prev, ensure_ascii=False),
            daytime=_daytime_en(),
            weekday=_WEEKDAYS_EN[timeutil.now().weekday()],
            routine=((persona_context or {}).get("daily_routine") or {}).get(
                _daytime(), "—"),
            personality_summary=(persona_context or {}).get("personality_summary", ""),
            baseline_mood=json.dumps(baseline, ensure_ascii=False),
            day_label=("a good" if day["label"] == "good" else
                       "a rough" if day["label"] == "rough" else "an ordinary"),
            day_mood=json.dumps({"valence": day["valence"], "arousal": day["arousal"]}),
            behavioral_rules="; ".join(
                (persona_context or {}).get("behavioral_rules") or []),
            storylines=json.dumps(
                [s.get("title") for s in storylines[:3]], ensure_ascii=False),
            last_world_fact=last_world_fact or _NONE,
        )

    @staticmethod
    def _state_from_gemma(data: dict, prev: dict) -> dict:
        mood = data["mood"]
        prev_mood = prev.get("mood") or {}
        # Шаг тика ограничен: модель любит одним тиком «сбросить» mood в
        # baseline, стирая толчок от диалога/события
        pv = _num(prev_mood.get("valence"), 0.0)
        pa = _num(prev_mood.get("arousal"), 0.3)
        valence = _clamp(_num(mood.get("valence"), pv),
                         pv - MOOD_TICK_MAX_STEP, pv + MOOD_TICK_MAX_STEP)
        arousal = _clamp(_num(mood.get("arousal"), pa),
                         pa - MOOD_TICK_MAX_STEP, pa + MOOD_TICK_MAX_STEP)
        return {
            "energy": int(max(0, min(100, int(data.get("energy", prev["energy"]))))),
            "mood": {"valence": round(_clamp(valence, -1.0, 1.0), 2),
                     "arousal": round(_clamp(arousal, 0.0, 1.0), 2),
                     "tag": (short_mood_tag(mood.get("tag"))
                             or prev_mood.get("tag") or _DEFAULT_MOOD_TAG["en"])},
            "pastime": str(data.get("pastime", prev["pastime"]))[:200],
            "location": str(data.get("location", prev["location"]))[:200],
            "internal_note": str(data.get("internal_note", ""))[:200],
            # spot/pose — сырые, валидирует room.sanitize_spot в _commit_tick
            "spot": (str(data["spot"])[:140] if data.get("spot") else None),
            "pose": (str(data["pose"])[:20] if data.get("pose") else None),
            "engine": "gemma",
        }

    def _tick_via_gemma(self, chat_id: str, prev: dict, persona_context: dict,
                        storylines: List, last_world_fact: str,
                        spots: Optional[List[dict]] = None,
                        user_language: Optional[str] = None,
                        day: Optional[dict] = None) -> Optional[dict]:
        if not self.use_gemma or not self.local.is_available(task="state_engine"):
            return None
        try:
            prompt = self._build_tick_prompt(prev, persona_context, storylines,
                                             last_world_fact, spots=spots, day=day)
            prompt += _language_tail(user_language)
            response = self.local.get_response(
                messages=[
                    {"role": "system", "content": "You return only valid JSON without explanations."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=330 if spots else 300,
                task="state_engine",
            )
            if not response:
                return None
            from app.core.persona_context import _extract_json
            data = _extract_json(response)
            if not data or not isinstance(data.get("mood"), dict):
                return None
            return self._state_from_gemma(data, prev)
        except Exception as e:
            logger.warning(f"[StateEngine] Тик Gemma не удался: {e}")
            return None

    def _heuristic_tick(self, prev: dict, persona_context: dict,
                        spots: Optional[List[dict]] = None,
                        user_language: Optional[str] = None,
                        day: Optional[dict] = None) -> dict:
        """Дрейф без LLM: энергия по времени суток, mood — к настроению дня.
        Ночью не-primitive почти всегда спит (в кровати, если она есть).
        Место в комнате: сон — кровать; иначе spot не задаётся — его
        выведет санитайзер по ключевым словам нового pastime.
        Тексты эвристики — на языке пользователя (user_language)."""
        lk = _lang_key(user_language)
        hour = timeutil.now().hour
        energy = prev["energy"]
        pastime = prev["pastime"]
        if 7 <= hour < 23:
            energy = max(15, energy - random.randint(2, 5))
        else:
            energy = min(100, energy + 7)

        # pastime держится 1-3 тика: иногда меняем; ночью — сон
        keep = random.random() < 0.55
        if not (7 <= hour < 23) and not self.primitive:
            pastime = _SLEEP_PASTIME[lk] if random.random() < 0.85 else pastime
        elif not keep:
            if self.primitive:
                pastime = random.choice(_HEURISTIC_PASTIMES_PRIMITIVE[lk])
            else:
                routine = ((persona_context or {}).get("daily_routine") or {})
                pastime = routine.get(_daytime()) or random.choice(
                    _HEURISTIC_PASTIMES[lk])

        day = day or day_mood("", (persona_context or {}).get("baseline_mood"))
        mood = dict(prev["mood"])
        valence = _num(mood.get("valence"), 0.0)
        arousal = _num(mood.get("arousal"), 0.3)
        mood["valence"] = round(valence + (day["valence"] - valence) * MOOD_DRIFT, 2)
        mood["arousal"] = round(arousal + (day["arousal"] - arousal) * MOOD_DRIFT, 2)
        # Толчок выдохся (valence почти у цели дня) — событийный тег
        # («обида», «радость») уступает темпераменту; раньше тег сбрасывался
        # монеткой, даже когда персона ещё была глубоко обижена
        if abs(mood["valence"] - day["valence"]) < MOOD_SETTLED:
            mood["tag"] = day["tag"] or _DEFAULT_MOOD_TAG[lk]
        else:
            mood["tag"] = short_mood_tag(mood.get("tag")) or _DEFAULT_MOOD_TAG[lk]

        result = {
            "energy": energy,
            "mood": mood,
            "pastime": pastime,
            "location": prev["location"],
            "internal_note": "",
            "engine": "heuristic",
        }
        if pastime in _SLEEP_PASTIMES:
            result["pose"] = "sleep"
            if spots and any(s.get("key") == "bed" for s in spots):
                result["spot"] = "bed"
            elif prev.get("spot") and prev.get("spot") != "away":
                result["spot"] = prev["spot"]
        return result

    @staticmethod
    def _state_diff(prev: dict, new: dict) -> dict:
        diff = {}
        if prev.get("energy") != new.get("energy"):
            diff["energy"] = [prev.get("energy"), new.get("energy")]
        if prev.get("pastime") != new.get("pastime"):
            diff["pastime"] = new.get("pastime")
        if prev.get("location") != new.get("location"):
            diff["location"] = new.get("location")
        # Переход по комнате — как смена location (первое проставление
        # места у легаси-состояния без spot — не событие)
        if prev.get("spot") and prev.get("spot") != new.get("spot"):
            diff["spot"] = new.get("spot")
        # Mood — только заметная смена (тег или valence ≥ MOOD_LOG_DELTA):
        # дрейф по 0.01 за тик раньше писался каждый тик, забивал ленту
        # комнаты и раздувал счётчик фактов жизни для скоринга инициативы
        p_mood, n_mood = prev.get("mood", {}), new.get("mood", {})
        if (p_mood.get("tag") != n_mood.get("tag")
                or abs(_num(p_mood.get("valence"), 0.0)
                       - _num(n_mood.get("valence"), 0.0)) >= MOOD_LOG_DELTA):
            diff["mood"] = n_mood
        note = new.get("internal_note")
        if note:
            diff["internal_note"] = note
        return diff

    # ── Скоринг инициативы ────────────────────────────────

    def score_initiative(self, chat_id: str, silence_hours: float,
                         since_initiative_hours: float,
                         user_language: Optional[str] = None) -> float:
        """0..1. Отдельный Gemma-скоринг (standalone-вариант; в фоновом цикле
        живой персоны используется объединённый tick_and_score — один вызов).
        При недоступности Gemma — эвристика по сигналам."""
        with self._lock:
            state = self._states.get(chat_id)
        if state is None:
            return 0.0

        unconsumed_count = self.life_facts_count(chat_id)
        if self.use_gemma and self.local.is_available(task="state_engine"):
            try:
                prompt = INITIATIVE_SCORE_PROMPT.format(
                    silence_hours=silence_hours,
                    state=json.dumps(state, ensure_ascii=False),
                    prev_state="{}",
                    unconsumed_count=unconsumed_count,
                    since_initiative=since_initiative_hours,
                    language_line=user_language_line(user_language),
                )
                response = self.local.get_response(
                    messages=[
                        {"role": "system", "content": "You return only valid JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                    max_tokens=120,
                    task="state_engine",
                )
                from app.core.persona_context import _extract_json
                data = _extract_json(response or "")
                if data and "score" in data:
                    return max(0.0, min(1.0, float(data["score"])))
            except Exception as e:
                logger.debug(f"[StateEngine] Скоринг Gemma не удался: {e}")

        return self._heuristic_score(chat_id, silence_hours, since_initiative_hours)

    # ── Контекст для промптов ────────────────────────────

    def get_state_context_block(self, chat_id: str) -> str:
        """Компактный блок состояния для промпта основной LLM.
        Для primitive — запрет вербализации: состояние выражается только
        действием/звуком/жестом, не человеческой рефлексией."""
        state = self.get_state(chat_id)
        mood = state.get("mood", {})

        # Проекция состояния в НАБЛЮДАЕМОЕ поведение ответа: «react
        # naturally» почти не давит на длину/тон, конкретные инструкции —
        # давят. Только поведенческие следствия, без просьб «сказать, что
        # устал» — это не нарушает behavioral_rules-персон с запретом
        # показывать усталость/раздражение
        projection = []
        try:
            energy = int(state.get("energy", 50))
            valence = float(mood.get("valence", 0) or 0)
        except (TypeError, ValueError):
            energy, valence = 50, 0.0
        # Отклонение от обычного темперамента персоны: у мрачной по натуре
        # персоны valence −0.2 — норма, у жизнерадостной — уже спад
        day = state.get("mood_day") or {}
        shift = valence - _num(day.get("baseline"), 0.0)
        if energy < 25:
            projection.append(
                "Your energy is critically low right now: keep your reply shorter "
                "than usual, don't start new topics or elaborate plans.")
        if valence <= -0.3 or shift <= -0.3:
            projection.append(
                "Your mood is clearly low: fewer questions and less enthusiasm "
                "than usual — don't force cheerfulness.")
        elif shift >= 0.25:
            projection.append(
                "Your mood is noticeably good: a bit livelier and warmer than usual, "
                "within your character.")
        projection_text = ("\n" + "\n".join(projection)) if projection else ""
        day_line = ""
        if day.get("date") == timeutil.now().date().isoformat() and day.get("label"):
            day_line = f"Kind of day: {day['label']}\n"

        header = (
            f"[CURRENT PHYSICAL STATE]\n"
            f"Energy: {state.get('energy', 50)}/100\n"
            f"Mood: {mood.get('tag', '—')} "
            f"(valence {mood.get('valence', 0):+.2f}, arousal {mood.get('arousal', 0):.2f})\n"
            f"Doing: {state.get('pastime', '—')}\n"
            f"Where: {state.get('location', '—')}\n"
            "STRICT RULE: this is your physical state. You are a primitive "
            "creature — you CANNOT discuss or analyze it in human words. "
            "Express it ONLY through actions, sounds, gestures (1-3 simple words). "
            "NEVER mention any engine/state/system."
            if self.primitive else
            f"[CURRENT STATE]\n"
            f"Energy: {state.get('energy', 50)}/100\n"
            f"Mood: {mood.get('tag', '—')} "
            f"(valence {mood.get('valence', 0):+.2f}, arousal {mood.get('arousal', 0):.2f})\n"
            f"{day_line}"
            f"Pastime: {state.get('pastime', '—')}\n"
            f"Location: {state.get('location', '—')}"
            f"{projection_text}\n"
            "STRICT RULE: this is your CURRENT inner state and what you are doing "
            "right now. React to it naturally in your replies, but do NOT list these "
            "parameters verbatim and do NOT mention any engine/state/system."
        )
        return header

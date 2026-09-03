"""
Слой «Состояние» (State Engine) — внутренняя жизнь персоны между сообщениями.

Модель данных (по каждому чату, персона фиксирована инстансом бота):
  persona_state: energy (0-100), mood {valence, arousal, tag},
                 pastime, location, last_tick_at, updated_at
  offline_log (append-only): type (state_change | world_event |
                 external_stimulus), payload, consumed

Тик-цикл (§3.2): каждые tick_interval_minutes для каждого известного чата
Gemma механически обновляет параметры состояния (STATE_TICK_PROMPT) и
оценивает повод написать пользователю (INITIATIVE_SCORE_PROMPT). Если Gemma
(Ollama) недоступна — детерминированный эвристический дрейф: энергия падает
днём и восстанавливается ночью, mood тянется к baseline_mood из выжимки
персонажа. Без LLM система продолжает жить, просто скучнее.

Ключевой принцип (§1.2): Gemma работает с persona_context (выжимкой), не с
полным system_prompt. Любой текст, который видит пользователь, генерирует
основная LLM — движок состояния только решает «что произошло».
"""

import json
import logging
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.core.config import get_db_paths
from app.core.local_router import get_local_router

logger = logging.getLogger(__name__)

DEFAULT_TICK_MINUTES = 20          # §3.2: 15-30 мин
INITIATIVE_THRESHOLD = 0.62        # порог скоринга инициативы (§3.4)
MAX_OFFLINE_LOG = 500              # append-only, но не бесконечно
OFFLINE_LOG_TTL_DAYS = 14          # consumed-записи старше — вычищаются
_MOOD_TAGS = [                     # допустимые теги mood (fallback-случай)
    "спокойствие", "любопытство", "сосредоточенность", "интерес",
    "настороженность", "удовлетворение", "апатия",
]

_WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг",
                "пятница", "суббота", "воскресенье"]

# ── Промпты (§3.3, §3.4) ────────────────────────────────────────────────

STATE_TICK_PROMPT = """Ты — движок симуляции внутреннего состояния персонажа.
Твоя задача — механически обновить параметры, НЕ сочинять литературный текст.

Верни СТРОГО JSON:
{{
  "energy": <int 0-100>,
  "mood": {{"valence": <float -1..1>, "arousal": <float 0..1>, "tag": "<string>"}},
  "pastime": "<string>",
  "location": "<string>",
  "internal_note": "<1 короткая фраза, черновик, не для показа пользователю>"
}}

КОНТЕКСТ:
Текущее состояние: {state}
Время: {daytime}, {weekday}
Распорядок персонажа в это время суток: {routine}
Характер (кратко): {personality_summary}
Базовый темперамент (к чему дрейфует mood): {baseline_mood}
Запрещённые проявления: {behavioral_rules}
Активные сюжетные линии: {storylines}
Последний факт мира: {last_world_fact}

ПРАВИЛА:
- energy падает днём (быстрее при активных pastime), восстанавливается ночью/при отдыхе
- pastime/location держатся распорядка: ночью персонаж почти всегда спит,
  если в событиях нет причины иначе
- mood дрейфует к базовому темпераменту; событие может толкнуть его, но mood.tag
  не должен нарушать запрещённые проявления (например, если персонажу запрещено
  «демонстрировать раздражение/усталость» — не называй это состояние прямо,
  только косвенные числовые сдвиги)
- pastime меняется не каждый тик — обычно держится 1-3 тика подряд
- не выдумывай новых NPC/мест — используй только то, что дано в контексте"""

INITIATIVE_SCORE_PROMPT = """Оцени от 0 до 1: насколько сейчас есть повод персонажу написать пользователю.
Учти: время с последнего сообщения, смену настроения/занятия, наличие
непрочитанного факта из жизни персонажа, обычную частоту общения этой пары.

Верни JSON: {{"score": <float 0..1>, "reason": "<короткое пояснение>"}}

Контекст:
Время с последнего сообщения пользователя: {silence_hours:.1f} ч
Текущее состояние: {state}
Предыдущее состояние: {prev_state}
Непотреблённые факты жизни: {unconsumed_count}
Часов с последней инициативы: {since_initiative:.1f}"""

# Суффикс к тик-промпту для объединённого вызова (§3.4): один Gemma-вызов
# на чат/тик вместо двух. Настройки proactive из yaml передаются в скоринг.
_INITIATIVE_SUFFIX = """
Дополнительно оцени от 0 до 1: насколько сейчас есть повод персонажу написать пользователю.
Учти: время с последнего сообщения, смену настроения/занятия, наличие
непрочитанных фактов из жизни персонажа, обычную частоту общения этой пары
и настройки инициативы персоны.

Время с последнего сообщения пользователя: {silence_hours:.1f} ч
Часов с последней инициативы: {since_initiative:.1f}
Непотреблённых фактов жизни: {unconsumed_count}
Настройки инициативы персоны (yaml proactive): {proactive_settings}

Добавь в ответ-JSON поля "initiative_score": <float 0..1> и "initiative_reason": "<коротко>"."""

# Примитивный вариант (intellect tier primitive): состояние — чисто физическое,
# pastime — действие, internal_note — сенсорное впечатление без рефлексии
STATE_TICK_PROMPT_PRIMITIVE = """Ты — движок простого физического состояния существа (не человека по типу мышления).
Обнови параметры механически, НЕ сочиняй текст. Верни СТРОГО JSON:
{{
  "energy": <int 0-100>,
  "mood": {{"valence": <float -1..1>, "arousal": <float 0..1>, "tag": "<1-2 слова: сонливость, довольно, испуг>"}},
  "pastime": "<физическое действие: спит, грызёт игрушку, обнюхивает углы, смотрит в окно>",
  "location": "<где находится>",
  "internal_note": "<сенсорное впечатление до 5 слов: тепло, шумно, пахнет едой>"
}}

КОНТЕКСТ:
Текущее состояние: {state}
Время: {daytime}, {weekday}
Кто существо (кратко): {personality_summary}
Что происходило рядом: {storylines}
Последний факт: {last_world_fact}

ПРАВИЛА:
- energy падает от активности днём, восстанавливается во сне/покое
- mood дрейфует к спокойствию; tag — простое слово, без абстракций
- pastime — только физическое действие, держится 1-3 тика
- не выдумывай новых мест"""


def _daytime() -> str:
    h = datetime.now().hour
    if 5 <= h < 12:
        return "утро"
    if 12 <= h < 18:
        return "день"
    if 18 <= h < 23:
        return "вечер"
    return "ночь"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class StateEngine:
    """Хранение + тики состояния. Потокобезопасен (RLock), файлы — JSON."""

    def __init__(self, context: str, persona_name: str,
                 tick_interval_minutes: int = DEFAULT_TICK_MINUTES,
                 primitive: bool = False, use_gemma: bool = True):
        self.context = context
        self.persona_name = persona_name
        self.tick_interval_minutes = tick_interval_minutes
        # primitive (intellect tier): физическое состояние без рефлексии —
        # свой тик-промпт и запрет вербализации состояния в диалоге
        self.primitive = primitive
        # use_gemma=false (features.state_engine.use_gemma): только
        # эвристический дрейф, локальная модель не дёргается вовсе
        self.use_gemma = use_gemma
        self.local = get_local_router()
        self._lock = threading.RLock()
        # Счётчики для наблюдаемости (in-memory, снапшот — get_state_for_ui)
        self.stats = {"ticks_gemma": 0, "ticks_heuristic": 0}

        db = get_db_paths(context)
        base = Path(db["stm"]).parent / "living"
        base.mkdir(parents=True, exist_ok=True)
        self._state_file = base / "state.json"
        self._log_file = base / "offline_log.json"

        self._states: Dict[str, dict] = self._load_json(self._state_file, {"chats": {}})["chats"]
        log_data = self._load_json(self._log_file, {"entries": [], "next_id": 1})
        self._log: List[dict] = log_data["entries"]
        self._next_id: int = log_data["next_id"]

    # ── Хранение ─────────────────────────────────────────

    @staticmethod
    def _load_json(path: Path, default: dict) -> dict:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[StateEngine] Битый файл {path}: {e}")
        return default

    def _save_state(self):
        try:
            tmp = self._state_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"chats": self._states}, f, ensure_ascii=False, indent=2)
            tmp.replace(self._state_file)
        except Exception as e:
            logger.error(f"[StateEngine] Ошибка сохранения state: {e}")

    def _save_log(self):
        try:
            tmp = self._log_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"entries": self._log, "next_id": self._next_id},
                          f, ensure_ascii=False, indent=2)
            tmp.replace(self._log_file)
        except Exception as e:
            logger.error(f"[StateEngine] Ошибка сохранения offline_log: {e}")

    # ── Публичный API ────────────────────────────────────

    def get_state(self, chat_id: str) -> dict:
        """Состояние чата (создаёт дефолтное при первом обращении)."""
        with self._lock:
            return self._ensure_state(chat_id)

    def _ensure_state(self, chat_id: str) -> dict:
        state = self._states.get(chat_id)
        if state is None:
            state = {
                "energy": 78,
                "mood": {"valence": 0.0, "arousal": 0.3, "tag": "спокойствие"},
                "pastime": "наблюдает за происходящим",
                "location": "своё обычное место",
                "last_tick_at": time.time(),
                "updated_at": _now_iso(),
            }
            self._states[chat_id] = state
            self._save_state()
        return state

    def apply_mood_impact(self, chat_id: str, valence_delta: float, tag: str):
        """Толчок mood от события мира (§4.3 mood_impact). Дрейф к baseline
        доделает ближайший тик — событие только толкает."""
        with self._lock:
            state = self._ensure_state(chat_id)
            mood = state["mood"]
            mood["valence"] = max(-1.0, min(1.0, mood["valence"] + float(valence_delta)))
            if tag:
                mood["tag"] = str(tag)[:80]
            state["updated_at"] = _now_iso()
            self._save_state()

    # ── Offline log ──────────────────────────────────────

    def log_event(self, chat_id: str, entry_type: str, payload: dict) -> int:
        """Append-only запись в offline_log. Возвращает id записи."""
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
                ts = datetime.fromisoformat(e["timestamp"]).timestamp()
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
        """Записи лога с заданного времени (приветствие-дневник, §7).
        consumed-записи пропускаем: уже озвученные (дневник/инициатива)
        факты не пересобираются в приветствие повторно."""
        with self._lock:
            out = []
            for e in self._log:
                if e["chat_id"] != str(chat_id) or e.get("consumed"):
                    continue
                try:
                    ts = datetime.fromisoformat(e["timestamp"]).timestamp()
                except (ValueError, TypeError):
                    continue
                if ts >= since_ts:
                    out.append(e)
            return out

    # ── Тик состояния (§3.3) ─────────────────────────────

    def tick(self, chat_id: str, persona_context: dict,
             storylines: Optional[List] = None,
             last_world_fact: str = "",
             known_places: Optional[List[str]] = None) -> dict:
        """Один тик: обновляет состояние, пишет diff в offline_log.
        Возвращает новое состояние. Синхронный LLM-вызов — звать из потока.
        Для primitive storylines — строки об окружении (предметы/факты).
        known_places — известные места мира: location вне списка мягко
        откатывается к прежнему (санитайзер дрейфа мира, §4.1)."""
        with self._lock:
            prev = dict(self._ensure_state(chat_id))
        new_state = self._tick_via_gemma(
            chat_id, prev, persona_context, storylines or [], last_world_fact
        )
        if new_state is None:
            new_state = self._heuristic_tick(prev, persona_context)
        return self._commit_tick(chat_id, prev, new_state, persona_context,
                                 known_places=known_places)

    def tick_and_score(self, chat_id: str, persona_context: dict,
                       storylines: Optional[List] = None,
                       last_world_fact: str = "", silence_hours: float = 0.0,
                       since_initiative_hours: float = 24.0,
                       proactive_settings: Optional[dict] = None,
                       known_places: Optional[List[str]] = None) -> tuple:
        """Тик состояния (§3.3) + скоринг инициативы (§3.4) ОДНИМ Gemma-вызовом —
        вдвое меньше локальных вызовов на чат/тик. Настройки proactive из yaml
        уходят в промпт скоринга (§3.4: скоринг учитывает существующие поля).
        known_places — санитайзер location (см. tick).
        Возвращает (new_state, score 0..1). Без Gemma — эвристика обоих."""
        with self._lock:
            prev = dict(self._ensure_state(chat_id))
        new_state, score = None, None
        if self.use_gemma and self.local.is_available(task="state_engine"):
            try:
                prompt = self._build_tick_prompt(
                    prev, persona_context, storylines or [], last_world_fact)
                prompt += _INITIATIVE_SUFFIX.format(
                    silence_hours=silence_hours,
                    since_initiative=since_initiative_hours,
                    unconsumed_count=len(self.unconsumed(chat_id)),
                    proactive_settings=json.dumps(
                        proactive_settings or {}, ensure_ascii=False))
                response = self.local.get_response(
                    messages=[
                        {"role": "system", "content": "Ты возвращаешь только валидный JSON без пояснений."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=380,
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
            new_state = self._heuristic_tick(prev, persona_context)
        state = self._commit_tick(chat_id, prev, new_state, persona_context,
                                  known_places=known_places)
        if score is None:
            score = self._heuristic_score(chat_id, silence_hours,
                                          since_initiative_hours)
        return state, score

    @staticmethod
    def _parse_score(data: dict) -> Optional[float]:
        try:
            return max(0.0, min(1.0, float(data["initiative_score"])))
        except (KeyError, TypeError, ValueError):
            return None

    def _heuristic_score(self, chat_id: str, silence_hours: float,
                         since_initiative_hours: float) -> float:
        """Скоринг без LLM: молчание + непотреблённые факты + давность инициативы."""
        score = 0.0
        score += min(0.35, silence_hours / 24.0)
        score += min(0.30, len(self.unconsumed(chat_id)) * 0.10)
        if since_initiative_hours > 6:
            score += 0.10
        return max(0.0, min(1.0, score))

    def _commit_tick(self, chat_id: str, prev: dict, new_state: dict,
                     persona_context: Optional[dict] = None,
                     known_places: Optional[List[str]] = None) -> dict:
        """Постобработка тика: guard mood.tag по behavioral_rules (§1.2),
        санитайзер location по известным местам мира, штампы времени,
        сохранение, осмысленный diff → offline_log."""
        # mood.tag не должен нарушать behavioral_rules: грубая проверка —
        # если tag пересекается с запрещёнными словами, оставляем прежний tag
        prev_mood = prev.get("mood", {})
        forbidden_words = " ".join(
            (persona_context or {}).get("behavioral_rules") or []).lower()
        tag = (new_state.get("mood") or {}).get("tag", "")
        if tag and forbidden_words and any(
                w and w in tag.lower() for w in
                ["устал", "раздраж", "скука", "обижен", "грустит"]
        ) and any(w in forbidden_words for w in
                  ["устал", "раздраж", "скуку", "обиж", "груст"]):
            new_state["mood"]["tag"] = prev_mood.get("tag", "спокойствие")

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
                           storylines: List, last_world_fact: str) -> str:
        if self.primitive:
            # storylines для primitive — просто строки об окружении
            # (предметы/недавние факты), их LivingPersona передаёт как строки
            surroundings = "; ".join(str(s) for s in list(storylines)[:8]) or "(нет)"
            return STATE_TICK_PROMPT_PRIMITIVE.format(
                state=json.dumps(prev, ensure_ascii=False),
                daytime=_daytime(),
                weekday=_WEEKDAYS_RU[datetime.now().weekday()],
                personality_summary=(persona_context or {}).get("personality_summary", "")[:300],
                storylines=surroundings,
                last_world_fact=last_world_fact or "(нет)",
            )
        return STATE_TICK_PROMPT.format(
            state=json.dumps(prev, ensure_ascii=False),
            daytime=_daytime(),
            weekday=_WEEKDAYS_RU[datetime.now().weekday()],
            routine=((persona_context or {}).get("daily_routine") or {}).get(
                _daytime(), "—"),
            personality_summary=(persona_context or {}).get("personality_summary", ""),
            baseline_mood=json.dumps(
                (persona_context or {}).get("baseline_mood", {}), ensure_ascii=False),
            behavioral_rules="; ".join(
                (persona_context or {}).get("behavioral_rules") or []),
            storylines=json.dumps(
                [s.get("title") for s in storylines[:3]], ensure_ascii=False),
            last_world_fact=last_world_fact or "(нет)",
        )

    @staticmethod
    def _state_from_gemma(data: dict, prev: dict) -> dict:
        mood = data["mood"]
        valence = max(-1.0, min(1.0, float(mood.get("valence", 0))))
        arousal = max(0.0, min(1.0, float(mood.get("arousal", 0.3))))
        return {
            "energy": int(max(0, min(100, int(data.get("energy", prev["energy"]))))),
            "mood": {"valence": round(valence, 2), "arousal": round(arousal, 2),
                     "tag": str(mood.get("tag", "спокойствие"))[:80]},
            "pastime": str(data.get("pastime", prev["pastime"]))[:200],
            "location": str(data.get("location", prev["location"]))[:200],
            "internal_note": str(data.get("internal_note", ""))[:200],
            "engine": "gemma",
        }

    def _tick_via_gemma(self, chat_id: str, prev: dict, persona_context: dict,
                        storylines: List, last_world_fact: str) -> Optional[dict]:
        if not self.use_gemma or not self.local.is_available(task="state_engine"):
            return None
        try:
            prompt = self._build_tick_prompt(prev, persona_context, storylines,
                                             last_world_fact)
            response = self.local.get_response(
                messages=[
                    {"role": "system", "content": "Ты возвращаешь только валидный JSON без пояснений."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=300,
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

    def _heuristic_tick(self, prev: dict, persona_context: dict) -> dict:
        """Дрейф без LLM: энергия по времени суток, mood — к baseline.
        Распорядок (фаза C): ночью не-primitive почти всегда спит."""
        hour = datetime.now().hour
        energy = prev["energy"]
        pastime = prev["pastime"]
        if 7 <= hour < 23:
            energy = max(15, energy - random.randint(2, 5))
        else:
            energy = min(100, energy + 7)

        # pastime держится 1-3 тика (§3.3): иногда меняем; ночью — сон
        keep = random.random() < 0.55
        if not (7 <= hour < 23) and not self.primitive:
            pastime = "спит" if random.random() < 0.85 else pastime
        elif not keep:
            if self.primitive:
                pastime = random.choice([
                    "спит", "грызёт игрушку", "обнюхивает углы",
                    "смотрит в окно", "точит когти", "ворочается в подстилке",
                ])
            else:
                routine = ((persona_context or {}).get("daily_routine") or {})
                pastime = routine.get(_daytime()) or random.choice([
                    "занят своими делами", "наблюдает за происходящим",
                    "перебирает накопившиеся мысли", "реставрирует порядок вокруг себя",
                ])

        baseline = (persona_context or {}).get("baseline_mood") or {}
        mood = dict(prev["mood"])
        mood["valence"] = round(
            mood["valence"] + (float(baseline.get("valence", 0)) - mood["valence"]) * 0.2, 2)
        mood["arousal"] = round(
            mood["arousal"] + (float(baseline.get("arousal", 0.3)) - mood["arousal"]) * 0.2, 2)
        # tag возвращаем к темпераменту, если сильно ушли
        if mood["tag"] not in _MOOD_TAGS and random.random() < 0.4:
            mood["tag"] = baseline.get("tag") or "спокойствие"

        return {
            "energy": energy,
            "mood": mood,
            "pastime": pastime,
            "location": prev["location"],
            "internal_note": "",
            "engine": "heuristic",
        }

    @staticmethod
    def _state_diff(prev: dict, new: dict) -> dict:
        diff = {}
        if prev.get("energy") != new.get("energy"):
            diff["energy"] = [prev.get("energy"), new.get("energy")]
        if prev.get("pastime") != new.get("pastime"):
            diff["pastime"] = new.get("pastime")
        if prev.get("location") != new.get("location"):
            diff["location"] = new.get("location")
        p_mood, n_mood = prev.get("mood", {}), new.get("mood", {})
        if (p_mood.get("tag"), p_mood.get("valence")) != (n_mood.get("tag"), n_mood.get("valence")):
            diff["mood"] = n_mood
        note = new.get("internal_note")
        if note:
            diff["internal_note"] = note
        return diff

    # ── Скоринг инициативы (§3.4) ────────────────────────

    def score_initiative(self, chat_id: str, silence_hours: float,
                         since_initiative_hours: float) -> float:
        """0..1. Отдельный Gemma-скоринг (standalone-вариант; в фоновом цикле
        живой персоны используется объединённый tick_and_score — один вызов).
        При недоступности Gemma — эвристика по сигналам."""
        with self._lock:
            state = self._states.get(chat_id)
        if state is None:
            return 0.0

        unconsumed_count = len(self.unconsumed(chat_id))
        if self.use_gemma and self.local.is_available(task="state_engine"):
            try:
                prompt = INITIATIVE_SCORE_PROMPT.format(
                    silence_hours=silence_hours,
                    state=json.dumps(state, ensure_ascii=False),
                    prev_state="{}",
                    unconsumed_count=unconsumed_count,
                    since_initiative=since_initiative_hours,
                )
                response = self.local.get_response(
                    messages=[
                        {"role": "system", "content": "Ты возвращаешь только валидный JSON."},
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
        """Компактный блок состояния для промпта основной LLM (§7).
        Для primitive — запрет вербализации: состояние выражается только
        действием/звуком/жестом, не человеческой рефлексией (§2 матрицы)."""
        state = self.get_state(chat_id)
        mood = state.get("mood", {})

        # Проекция состояния в НАБЛЮДАЕМОЕ поведение ответа (§7): «react
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
        if energy < 25:
            projection.append(
                "Your energy is critically low right now: keep your reply shorter "
                "than usual, don't start new topics or elaborate plans.")
        if valence <= -0.3:
            projection.append(
                "Your mood is clearly low: fewer questions and less enthusiasm "
                "than usual — don't force cheerfulness.")
        projection_text = ("\n" + "\n".join(projection)) if projection else ""

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
            f"Pastime: {state.get('pastime', '—')}\n"
            f"Location: {state.get('location', '—')}"
            f"{projection_text}\n"
            "STRICT RULE: this is your CURRENT inner state and what you are doing "
            "right now. React to it naturally in your replies, but do NOT list these "
            "parameters verbatim and do NOT mention any engine/state/system."
        )
        return header

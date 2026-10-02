"""
Persona context layer — компактная выжимка system_prompt для структурных задач.

Локальная LLM на тиках состояния не может получать полный system_prompt —
он заточен под диалоговую генерацию и перегружает JSON-constrained
генерацию. Вместо этого один раз при создании/правке персоны основная LLM
извлекает структурированную выжимку:

  personality_summary — 3-5 предложений: кто персонаж, ключевая черта
  speech_dna          — маркеры речи (allowed/forbidden), тон
  behavioral_rules    — короткий список запретов из блока «НЕЛЬЗЯ»
  baseline_mood       — темперамент, к которому дрейфует mood между событиями
  interests           — темы для внешних стимулов
  role_context        — роль персонажа в мире
  world_binding       — привязка к реальному миру:
                        real_world | fictional_universe | unspecified

Ключевой принцип: локальная LLM работает с выжимкой и решает «что
произошло», основная LLM работает с полным system_prompt и решает «как
это прозвучит».

Кэширование: data/{context}/living/persona_context.json, ключ — sha256
system_prompt. Правка промпта персоны автоматически инвалидирует кэш.

Жёсткий gate: реальный интернет (web_search для фактов мира) разрешён
ТОЛЬКО персонам с world_binding.type == real_world — проверяется кодом
(external_stimuli_allowed), а не только флагом в конфиге.
"""

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

from app.core.config import get_db_paths
from app.core.language import user_language_line

logger = logging.getLogger(__name__)

# Дефолты выжимки, если основная LLM недоступна/не осилила схему.
# Ошибка в сторону «не завязан на реальность» безопаснее.
_DEFAULT_WORLD_BINDING = {
    "type": "fictional_universe",
    "location": None,
    "universe_note": None,
}

DEFAULT_PERSONA_CONTEXT = {
    "personality_summary": "",
    "speech_dna": {"allowed_markers": [], "forbidden_markers": [], "tone": ""},
    "behavioral_rules": [],
    "baseline_mood": {"valence": 0.0, "arousal": 0.3, "tag": "calm"},
    "interests": [],
    "role_context": "",
    "world_binding": dict(_DEFAULT_WORLD_BINDING),
    # Суточный распорядок: чем персона обычно занята по времени суток.
    # Ключи — служебные (утро/день/вечер/ночь, их читает state_engine._daytime),
    # значения попадают в промпты и в комнату — дефолты на английском
    "daily_routine": {
        "утро": "waking up and getting ready",
        "день": "busy with their own things",
        "вечер": "resting after the day",
        "ночь": "sleeping",
    },
}

# Выжимка на другом языке, чем нужен сейчас, переизвлекается не чаще раза
# в сутки: у персоны может быть несколько чатов на разных языках
LANGUAGE_REFRESH_SEC = 86400

# Промпт извлечения: основная LLM, разовый вызов. Просим СТРОГО JSON —
# маленький ответ, поэтому можно не жалеть инструкций.
# Литеральные скобки JSON экранированы ({{ }}) — промпт проходит через .format().
_EXTRACT_PROMPT = """You are a character card parser. From the system_prompt below, extract a structured summary according to the schema. Return STRICTLY one JSON object without markdown wrappers or explanations.

Schema:
{{
  "personality_summary": "3-5 sentences: who the character is, the key trait, the manner of behaving",
  "speech_dna": {{
    "allowed_markers": ["characteristic turns of speech that the character uses"],
    "forbidden_markers": ["turns of speech that the character NEVER uses"],
    "tone": "brief description of the tone"
  }},
  "behavioral_rules": ["short prohibitions from the MUST NOT/forbidden block, without details"],
  "baseline_mood": {{
    "valence": 0.0,
    "arousal": 0.3,
    "tag": "1-2 words: the default mood outside the dialogue (calm, cheerful, wary)"
  }},
  "interests": ["topics the character is interested in"],
  "role_context": "the character's role in their world (1 sentence)",
  "world_binding": {{
    "type": "real_world | fictional_universe | unspecified",
    "location": "city/place if specified in the card, otherwise null",
    "universe_note": "if it is a fictional universe — a brief description of how it differs from reality, otherwise null"
  }},
  "daily_routine": {{
    "утро": "what the character usually does in the morning (1 phrase)",
    "день": "... in the afternoon",
    "вечер": "... in the evening",
    "ночь": "... at night (usually sleep)"
  }}
}}
The daily_routine keys are fixed service keys (morning / afternoon / evening / night): keep them exactly as written.

Rules for world_binding.type:
- "real_world" — the card EXPLICITLY ties the character to our reality: a real city/country of residence, "lives here and now with the user", no fantastic setting.
- "fictional_universe" — the character exists in their own fictional universe (canon of a game/book/fantasy), even if it resembles the real one.
- "unspecified" — no explicit indications either way.
For valence: -1 (negative) .. 1 (positive). For arousal: 0 (calm) .. 1 (excitement).

system_prompt:
---
{system_prompt}
---
{language_line}
 JSON:"""


def _hash_prompt(system_prompt: str) -> str:
    return hashlib.sha256((system_prompt or "").encode("utf-8")).hexdigest()


def _clamp(value: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def _extract_json(text: str) -> Optional[dict]:
    # Достаёт первый JSON-объект из ответа LLM (модель любит обёртки/пояснения).
    if not text:
        return None
    # Убираем markdown-обёртку ```json ... ```
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start = cleaned.find("{")
    if start == -1:
        return None
    # Ищем парную закрывающую скобку простым подсчётом
    depth = 0
    for i in range(start, len(cleaned)):
        if cleaned[i] == "{":
            depth += 1
        elif cleaned[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _normalize(raw: dict) -> dict:
    # Приводит ответ LLM к схеме: недостающие поля — из дефолтов.
    speech = raw.get("speech_dna") or {}
    mood = raw.get("baseline_mood") or {}
    binding = raw.get("world_binding") or {}
    routine_raw = raw.get("daily_routine") or {}
    default_routine = DEFAULT_PERSONA_CONTEXT["daily_routine"]
    daily_routine = {
        k: (str(routine_raw.get(k, "")).strip()[:200] or default_routine[k])
        for k in ("утро", "день", "вечер", "ночь")
    }
    wb_type = binding.get("type")
    if wb_type not in ("real_world", "fictional_universe", "unspecified"):
        wb_type = "unspecified"
    # unspecified трактуем как fictional_universe (без доступа к реальному интернету)
    effective_type = "fictional_universe" if wb_type == "unspecified" else wb_type

    def _str_list(value) -> list:
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()][:12]

    return {
        "personality_summary": str(raw.get("personality_summary", "")).strip()[:1200],
        "speech_dna": {
            "allowed_markers": _str_list(speech.get("allowed_markers")),
            "forbidden_markers": _str_list(speech.get("forbidden_markers")),
            "tone": str(speech.get("tone", "")).strip()[:200],
        },
        "behavioral_rules": _str_list(raw.get("behavioral_rules")),
        "baseline_mood": {
            "valence": _clamp(mood.get("valence"), -1.0, 1.0),
            "arousal": _clamp(mood.get("arousal"), 0.0, 1.0),
            "tag": str(mood.get("tag", "")).strip()[:80] or "calm",
        },
        "interests": _str_list(raw.get("interests")),
        "role_context": str(raw.get("role_context", "")).strip()[:400],
        "daily_routine": daily_routine,
        "world_binding": {
            "type": effective_type,
            "detected_type": wb_type,  # что сказала модель ДО дефолта unspecified
            "location": (str(binding["location"]).strip()
                         if binding.get("location") else None),
            "universe_note": (str(binding["universe_note"]).strip()
                              if binding.get("universe_note") else None),
        },
    }


def _heuristic_fallback(system_prompt: str) -> dict:
    # Без LLM: черновая выжимка regex'ами. Хуже, но система живёт.
    rules = []
    forbidden = []
    # Строки после маркеров запретов: «— Не говорить...», «НЕЛЬЗЯ»-блоки
    for line in (system_prompt or "").splitlines():
        line_s = line.strip()
        if re.match(r"^—\s*(?:Не|Нельзя|Никогда)", line_s):
            rule = line_s.lstrip("— ").strip()
            if 8 < len(rule) <= 200:
                rules.append(rule)
                if len(rules) >= 8:
                    break
    if "не чувствую" in system_prompt or "я чувствую" in system_prompt.lower():
        forbidden.append("«я чувствую»")

    summary = " ".join((system_prompt or "").split())[:500]
    return {
        "personality_summary": summary,
        "speech_dna": {"allowed_markers": [], "forbidden_markers": forbidden, "tone": ""},
        "behavioral_rules": rules,
        "baseline_mood": {"valence": 0.0, "arousal": 0.3, "tag": "calm"},
        "interests": [],
        "role_context": "",
        "daily_routine": dict(DEFAULT_PERSONA_CONTEXT["daily_routine"]),
        "world_binding": dict(_DEFAULT_WORLD_BINDING),
    }


class PersonaContextLayer:
    """Ленивая выжимка system_prompt с кэшем на диске.

    Потокобезопасен: get() зовут несколько потоков разом (первый заход и
    тики фонового цикла living, урожай диалога, гейт стимулов), а память
    чата (/api/chat/clear, undo — app/api/memory_wipe.py) берёт self._lock,
    чтобы снять/вернуть кэш.

    LLM-вызов извлечения идёт БЕЗ self._lock: это фоновый канал веб-чата
    (очередь сайта + ответ до 150 с) — под локом он держал бы очистку
    переписки пользователя (to_thread в API ждал лок до конца извлечения).
    Под локом — только проверка/запись кэша. Повторное извлечение одного и
    того же промпта не дублируется: второй поток ждёт первого на Condition
    (wait отпускает лок — очистка чата проходит сразу).
    На пути ответа пользователю get() нет: get_living_context выжимку не
    читает, process_message её не вызывает.
    """

    def __init__(self, context: str, router=None,
                 manual_binding: Optional[dict] = None):
        self.context = context
        self.router = router
        # Ручной world_binding из YAML персоны (ключ world_binding: {type,
        # location, universe_note}) — приоритет над LLM-экстрактом: экстракт
        # может отличаться от правки к правке промпта, а гейт внешних
        # стимулов должен быть детерминированным.
        self.manual_binding = manual_binding if isinstance(manual_binding, dict) else None
        self._lock = threading.RLock()
        # Ожидание чужого извлечения того же промпта (на self._lock)
        self._cond = threading.Condition(self._lock)
        # Хэши промптов, которые сейчас извлекаются (у каждого свой поток);
        # один слот на все промпты путал ожидание разных промптов
        self._extracting: set = set()
        # Хэш промпта из ПОСЛЕДНЕГО запроса get(): кэш пишется только для
        # него — результат устаревшего промпта (правку прислали, пока шло
        # извлечение старого) в кэш не попадает
        self._latest_hash: Optional[str] = None
        db = get_db_paths(context)
        self._base_dir = Path(db["stm"]).parent / "living"
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._file = self._base_dir / "persona_context.json"
        self._cache: Optional[dict] = None  # {hash, persona_context}
        self._load()

    # ── Загрузка/сохранение ──────────────────────────────

    def _load(self):
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except Exception as e:
                logger.warning(f"[PersonaContext] Битый кэш {self._file}: {e}")
                self._cache = None

    def _save(self):
        try:
            tmp = self._file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            tmp.replace(self._file)
        except Exception as e:
            logger.error(f"[PersonaContext] Ошибка сохранения: {e}")

    # ── Публичный API ────────────────────────────────────

    def _apply_manual_binding(self, pc: dict) -> dict:
        """Наложить ручной world_binding из YAML поверх выжимки (копию,
        кэш хранит сырой экстракт). Тип валидируем: мусор из YAML не должен
        открывать real_world-гейт."""
        if not self.manual_binding:
            return pc
        out = dict(pc or {})
        binding = dict(out.get("world_binding") or {})
        t = str(self.manual_binding.get("type") or "").strip().lower()
        if t in ("real_world", "fictional_universe", "unspecified"):
            binding["type"] = t
            binding["manual"] = True
        if self.manual_binding.get("location") is not None:
            binding["location"] = str(self.manual_binding["location"])[:120] or None
        if self.manual_binding.get("universe_note"):
            binding["universe_note"] = str(self.manual_binding["universe_note"])[:400]
        out["world_binding"] = binding
        return out

    def _cache_fresh(self, h: str, user_language: Optional[str]) -> bool:
        """Кэш годен для промпта h. Выжимка на другом языке (свободный текст
        уходит в промпты и в комнату) переизвлекается, но не чаще
        LANGUAGE_REFRESH_SEC. Легаси-кэш без поля lang писался русским
        промптом — считаем его русским."""
        if not self._cache or self._cache.get("hash") != h:
            return False
        if not user_language:
            return True
        cached_lang = self._cache.get("lang", "ru")
        if cached_lang == user_language:
            return True
        return time.time() - float(self._cache.get("ts") or 0) < LANGUAGE_REFRESH_SEC

    def get(self, system_prompt: str, user_language: Optional[str] = None) -> dict:
        """Возвращает актуальную выжимку. Если кэш протух (правка промпта) —
        переизвлекает основной LLM (разово). При недоступности LLM —
        эвристический черновик, чтобы тики не падали. Ручной world_binding
        из YAML (если задан) накладывается поверх — каждый раз, детерминировано.
        user_language — язык пользователя (LivingPersona.global_language):
        свободный текст выжимки пишется на нём."""
        h = _hash_prompt(system_prompt)
        with self._lock:
            self._latest_hash = h
            while True:
                if self._cache_fresh(h, user_language):
                    return self._apply_manual_binding(self._cache["persona_context"])
                if h not in self._extracting:
                    break
                # Тот же промпт уже извлекается другим потоком — ждём его
                # результат (wait отпускает лок), а не шлём второй вызов
                self._cond.wait(timeout=5.0)
            self._extracting.add(h)
        extracted = None
        try:
            # Вне лока: LLM-вызов может идти минуты (очередь веб-чата)
            extracted = self._extract(system_prompt, user_language)
        finally:
            with self._lock:
                self._extracting.discard(h)
                # Кэш — только для актуального промпта: пока шло извлечение,
                # могли прислать правку (новый хэш) — тогда этот результат
                # отдаём своему вызывающему, но не записываем поверх.
                # Сброс/возврат кэша очисткой чата за это время записи не
                # мешает: выжимка — производная того же промпта
                if extracted is not None and self._latest_hash == h:
                    self._cache = {"hash": h, "persona_context": extracted,
                                   "lang": user_language, "ts": time.time()}
                    self._save()
                self._cond.notify_all()
        return self._apply_manual_binding(extracted)

    def refresh(self, system_prompt: str, user_language: Optional[str] = None) -> dict:
        # Принудительное переизвлечение (правка персоны).
        with self._lock:
            self._cache = None
        return self.get(system_prompt, user_language)

    def _extract(self, system_prompt: str, user_language: Optional[str] = None) -> dict:
        if not (system_prompt or "").strip():
            return json.loads(json.dumps(DEFAULT_PERSONA_CONTEXT))

        raw = None
        if self.router is not None:
            try:
                response = self.router.get_response(
                    messages=[
                        {"role": "system", "content": "You extract structured data from text. You answer strictly in JSON."},
                        {"role": "user", "content": _EXTRACT_PROMPT.format(
                            system_prompt=system_prompt[:12000],
                            language_line=user_language_line(user_language))},
                    ],
                    temperature=0.1,
                    max_tokens=900,
                    timeout=60.0,
                    webchat_channel="side",
                )
                raw = _extract_json(response or "")
            except Exception as e:
                logger.warning(f"[PersonaContext] Извлечение LLM не удалось: {e}")

        if raw:
            normalized = _normalize(raw)
            logger.info(
                f"[PersonaContext] Выжимка извлечена | "
                f"world_binding={normalized['world_binding']['type']} | "
                f"правил: {len(normalized['behavioral_rules'])} | "
                f"интересов: {len(normalized['interests'])}"
            )
            return normalized

        logger.info("[PersonaContext] LLM недоступен — эвристический черновик выжимки")
        return _heuristic_fallback(system_prompt)

    # ── Gate внешних стимулов ─────────────────────────────

    def external_stimuli_allowed(self, persona_context: dict, features: dict) -> bool:
        """Жёсткая проверка кодом: реальный интернет для фактов мира разрешён
        ТОЛЬКО real_world-персонам, даже если флаг включён руками.
        Возвращает True только при выполнении ОБЕИХ условий."""
        binding = (persona_context or {}).get("world_binding") or {}
        if binding.get("type") != "real_world":
            return False
        stimuli_cfg = (features or {}).get("external_stimuli") or {}
        if isinstance(stimuli_cfg, bool):
            return stimuli_cfg
        return bool(stimuli_cfg.get("enabled", False))


def default_external_stimuli_flag(persona_context: dict) -> bool:
    """Дефолт features.external_stimuli.enabled по world_binding:
    true только для real_world. Ручной override в YAML всё равно проходит
    через жёсткий gate external_stimuli_allowed()."""
    binding = (persona_context or {}).get("world_binding") or {}
    return binding.get("type") == "real_world"

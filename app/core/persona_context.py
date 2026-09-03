"""
Persona context layer — компактная выжимка system_prompt для структурных задач.

Задача (см. план «живой» персоны, §1): локальная Gemma на тиках состояния не
может получать полный system_prompt — он заточен под диалоговую генерацию и
перегружает JSON-constrained генерацию. Вместо этого ОДИН раз при создании/
правке персоны основная LLM извлекает структурированную выжимку:

  personality_summary — 3-5 предложений: кто персонаж, ключевая черта
  speech_dna          — маркеры речи (allowed/forbidden), тон
  behavioral_rules    — короткий список запретов из блока «НЕЛЬЗЯ»
  baseline_mood       — темперамент, к которому дрейфует mood между событиями
  interests           — темы для внешних стимулов (§5)
  role_context        — роль персонажа в мире
  world_binding       — привязка к реальному миру (§1.3):
                        real_world | fictional_universe | unspecified

Ключевой принцип: Gemma работает с выжимкой и решает «что произошло»,
основная LLM работает с полным system_prompt и решает «как это прозвучит».

Кэширование: data/{context}/living/persona_context.json, ключ — sha256
system_prompt. Правка промпта персоны автоматически инвалидирует кэш.

Жёсткий gate (§10): реальный интернет (web_search для фактов мира) разрешён
ТОЛЬКО персонам с world_binding.type == real_world — проверяется кодом
(can_use_external_stimuli), а не только флагом в конфиге.
"""

import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Optional

from app.core.config import get_db_paths

logger = logging.getLogger(__name__)

# Дефолты выжимки, если основная LLM недоступна/не осилила схему.
# Ошибка в сторону «не завязан на реальность» безопаснее (§1.3).
_DEFAULT_WORLD_BINDING = {
    "type": "fictional_universe",
    "location": None,
    "universe_note": None,
}

DEFAULT_PERSONA_CONTEXT = {
    "personality_summary": "",
    "speech_dna": {"allowed_markers": [], "forbidden_markers": [], "tone": ""},
    "behavioral_rules": [],
    "baseline_mood": {"valence": 0.0, "arousal": 0.3, "tag": "спокойствие"},
    "interests": [],
    "role_context": "",
    "world_binding": dict(_DEFAULT_WORLD_BINDING),
    # Суточный распорядок (фаза C): чем персона обычно занят по времени суток
    "daily_routine": {
        "утро": "просыпается и собирается",
        "день": "занят своими делами",
        "вечер": "отдыхает после дня",
        "ночь": "спит",
    },
}

# Промпт извлечения: основная LLM, разовый вызов. Просим СТРОГО JSON —
# маленький ответ, поэтому можно не жалеть инструкций.
# Литеральные скобки JSON экранированы ({{ }}) — промпт проходит через .format().
_EXTRACT_PROMPT = """Ты — парсер карточек персонажей. Из system_prompt ниже извлеки структурированную выжимку по схеме. Верни СТРОГО один JSON-объект без markdown-обёрток и пояснений.

Схема:
{{
  "personality_summary": "3-5 предложений: кто персонаж, ключевая черта, манера держаться",
  "speech_dna": {{
    "allowed_markers": ["характерные обороты речи, которые персонаж использует"],
    "forbidden_markers": ["обороты, которые персонаж НИКОГДА не использует"],
    "tone": "краткая характеристика тона"
  }},
  "behavioral_rules": ["короткие запреты из блока НЕЛЬЗЯ/запрещено, без деталей"],
  "baseline_mood": {{
    "valence": 0.0,
    "arousal": 0.3,
    "tag": "состояние по умолчанию (темперамент вне диалога)"
  }},
  "interests": ["темы, которые персонажу интересны"],
  "role_context": "роль персонажа в его мире (1 предложение)",
  "world_binding": {{
    "type": "real_world | fictional_universe | unspecified",
    "location": "город/место, если указано в карточке, иначе null",
    "universe_note": "если вымышленная вселенная — краткое описание её отличий от реальности, иначе null"
  }},
  "daily_routine": {{
    "утро": "чем персонаж обычно занят утром (1 фраза)",
    "день": "... днём",
    "вечер": "... вечером",
    "ночь": "... ночью (обычно сон)"
  }}
}}

Правила world_binding.type:
- "real_world" — карточка ЯВНО привязывает персонажа к нашей реальности: реальный город/страна жизни, «живёт здесь и сейчас с пользователем», нет фантастического сеттинга.
- "fictional_universe" — персонаж существует в собственной вымышленной вселенной (канон игры/книги/фэнтези), даже если она похожа на реальную.
- "unspecified" — нет явных указаний ни туда, ни сюда.
Для valence: -1 (негатив) .. 1 (позитив). Для arousal: 0 (спокойствие) .. 1 (возбуждение).

system_prompt:
---
{system_prompt}
---
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
    """Достаёт первый JSON-объект из ответа LLM (модель любит обёртки/пояснения)."""
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
    """Приводит ответ LLM к схеме: недостающие поля — из дефолтов."""
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
    # §1.3: unspecified трактуем как fictional_universe (без реального интернета)
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
            "tag": str(mood.get("tag", "")).strip()[:80] or "спокойствие",
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
    """Без LLM: черновая выжимка regex'ами. Хуже, но система живёт."""
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
        "baseline_mood": {"valence": 0.0, "arousal": 0.3, "tag": "спокойствие"},
        "interests": [],
        "role_context": "",
        "daily_routine": dict(DEFAULT_PERSONA_CONTEXT["daily_routine"]),
        "world_binding": dict(_DEFAULT_WORLD_BINDING),
    }


class PersonaContextLayer:
    """Ленивая выжимка system_prompt с кэшем на диске.

    Потокобезопасен: extract() может зваться из фонового цикла и из
    process_message параллельно — RLock + повторная проверка хэша.
    """

    def __init__(self, context: str, router=None,
                 manual_binding: Optional[dict] = None):
        self.context = context
        self.router = router
        # Ручной world_binding из YAML персоны (top-level ключ world_binding:
        # {type, location, universe_note}) — приоритет над LLM-экстрактом.
        # Раньше тип мира добывался только экстрактом: правка промпта молча
        # перероллила гейт внешних стимулов; ручное значение детерминировано.
        self.manual_binding = manual_binding if isinstance(manual_binding, dict) else None
        self._lock = threading.RLock()
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

    def get(self, system_prompt: str) -> dict:
        """Возвращает актуальную выжимку. Если кэш протух (правка промпта) —
        переизвлекает основной LLM (разово). При недоступности LLM —
        эвристический черновик, чтобы тики не падали. Ручной world_binding
        из YAML (если задан) накладывается поверх — каждый раз, детерминировано."""
        h = _hash_prompt(system_prompt)
        with self._lock:
            if self._cache and self._cache.get("hash") == h:
                return self._apply_manual_binding(self._cache["persona_context"])
            extracted = self._extract(system_prompt)
            self._cache = {"hash": h, "persona_context": extracted}
            self._save()
            return self._apply_manual_binding(extracted)

    def refresh(self, system_prompt: str) -> dict:
        """Принудительное переизвлечение (правка персоны)."""
        with self._lock:
            self._cache = None
        return self.get(system_prompt)

    def _extract(self, system_prompt: str) -> dict:
        if not (system_prompt or "").strip():
            return json.loads(json.dumps(DEFAULT_PERSONA_CONTEXT))

        raw = None
        if self.router is not None:
            try:
                response = self.router.get_response(
                    messages=[
                        {"role": "system", "content": "Ты извлекаешь структурированные данные из текста. Отвечаешь строго JSON."},
                        {"role": "user", "content": _EXTRACT_PROMPT.format(system_prompt=system_prompt[:12000])},
                    ],
                    temperature=0.1,
                    max_tokens=900,
                    timeout=60.0,
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

    # ── Gate внешних стимулов (§10) ───────────────────────

    def external_stimuli_allowed(self, persona_context: dict, features: dict) -> bool:
        """Жёсткая проверка кодом: реальный интернет для фактов мира разрешён
        ТОЛЬКО real_world-персонам, даже если флаг включён руками (§10).
        Возвращает True только при выполнении ОБЕИХ условий."""
        binding = (persona_context or {}).get("world_binding") or {}
        if binding.get("type") != "real_world":
            return False
        stimuli_cfg = (features or {}).get("external_stimuli") or {}
        if isinstance(stimuli_cfg, bool):
            return stimuli_cfg
        return bool(stimuli_cfg.get("enabled", False))


def default_external_stimuli_flag(persona_context: dict) -> bool:
    """Дефолт features.external_stimuli.enabled по world_binding (§1.3):
    true только для real_world. Ручной override в YAML всё равно проходит
    через жёсткий gate external_stimuli_allowed()."""
    binding = (persona_context or {}).get("world_binding") or {}
    return binding.get("type") == "real_world"

"""
Слой «Мир» (World/Lore Engine) — NPC, места, сюжетные линии, внешние стимулы.

Хранение (§4.1): data/{context}/living/world.json
  npc:       id, name, role, relationship_status, origin (seeded | detected | generated)
  place:     id, name, type, atmosphere
  storyline: id, title, status (started|ongoing|resolved), summary,
             related_npc_ids, next_advance_at
  external_stimulus: id, source, fetched_at, content, relevance_score, used

Заполнение базы (§4.2):
  - при создании персоны: основная LLM разбирает system_prompt (npc_seed_on_create)
  - из диалога: Gemma-классификатор «упомянут ли новый NPC/место?»
  - из мира (§5): внешние стимулы через интернет — ТОЛЬКО для real_world-персон
    (жёсткий gate external_stimuli_allowed, см. persona_context.py §10).

Генерация офлайн-событий (§4.3): 1-3 раза/день Gemma генерирует короткое
событие из жизни персонажа. Для fictional_universe внешний стимул НЕ тянется
из интернета — внутримировой факт («в Детройте похолодало») генерируется тем
же пайплайном с universe_note вместо external_stimulus (§1.3).

Событие описывает ЧТО произошло, финальную подачу для пользователя делает
основная LLM с полным system_prompt.
"""

import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from app.core.config import get_db_paths
from app.core.local_router import get_local_router
from app.core.persona_context import _extract_json

logger = logging.getLogger(__name__)

DEFAULT_EVENTS_PER_DAY = (1, 3)
MAX_NPCS = 40
MAX_PLACES = 30
MAX_STORYLINES = 15
MAX_STIMULI = 20
MAX_PLANS = 8                # открытые планы персоны (фаза B: ожидания)
MAX_RESOLVED_PLANS = 5       # завершённые планы храним кратко — для «как прошло»
DETECT_THROTTLE_SEC = 60  # детекция NPC/мест из диалога — не чаще раза в минуту
MAX_RESOLVED_STORYLINES = 10  # завершённые линии: храним последние, старые чистим


def _norm_title(title: str) -> str:
    """Нормализация заголовка линии для сопоставления: Gemma часто
    перефразирует («Расследование Кутузовой» vs «расследование кутузовой!»),
    и exact-матч молча терял апдейты. Регистр/пунктуация/пробелы не важны."""
    return re.sub(r"[^\w\s]+", " ", str(title or "").lower()).strip()


def _titles_similar(a: str, b: str) -> bool:
    """Похоже ли два заголовка (нормализация + близость по difflib)."""
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    import difflib
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.75

# ── Промпты ─────────────────────────────────────────────────────────────

_SEED_PROMPT = """Ты — парсер карточек персонажей. Из system_prompt ниже извлеки упомянутых людей, организации, места и фоновые сюжетные обстоятельства персонажа.

Верни СТРОГО JSON без markdown:
{{
  "npcs": [{{"name": "...", "role": "кем приходится персонажу/собеседнику", "relationship_status": "краткое состояние отношений"}}],
  "places": [{{"name": "...", "type": "город/здание/локация", "atmosphere": "краткая атмосфера"}}],
  "storylines": [{{"title": "...", "summary": "фоновая линия, если есть"}}]
}}
Если категории пусты — пустые списки (это нормально: база наполнится из диалогов).

system_prompt:
---
{system_prompt}
---"""

# Фолбэк-сеялка: основной сид часто не находит сюжетных линий в промпте
# (кейс connor: storylines: [] → сценаристу никогда было что двигать).
# Здесь просим ПРИДУМАТЬ фоновые линии, следующие лору, — это штатно для
# «жизни между диалогами»: открытые обстоятельства жизни самой персоны.
_STORYLINE_SEED_PROMPT = """Ты — сценарист базы мира персонажа. По system_prompt ниже придумай 1-2 ФОНОВЫЕ сюжетные линии САМОЙ персоны (не пользователя): незавершённые обстоятельства её жизни, которые могут тихо развиваться между разговорами. Линии обязаны следовать лору вселенной и характеру; открытые ситуации, а не разрешённые конфликты.

Верни СТРОГО JSON без markdown:
{{"storylines": [{{"title": "короткое название", "summary": "1-2 предложения: что за линия и почему она открыта"}}]}}

system_prompt:
---
{system_prompt}
---"""

_WORLD_EVENT_PROMPT = """Сгенерируй одно короткое событие из жизни персонажа за последние часы.
Используй ПРЕЖДЕ ВСЕГО данные ниже. Разрешено ввести НЕ БОЛЕЕ ОДНОГО нового
NPC или места, если событие этого естественно требует (новый NPC/место
должны следовать лору и характеру) — остальное не выдумывай.
Недавние события могут иметь последствия: событие может продолжать их.
Соблюдай ограничения характера персонажа — событие описывает ЧТО произошло,
а не то, как персонаж должен об этом рассказывать: финальную подачу сделает
другая модель отдельно.

Верни JSON:
{{
  "event": "1-2 предложения, черновик",
  "involves_npc": [{{"name": "...", "interaction": "что было"}}],
  "involves_place": "...",
  "new_npc": {{"name": "...", "role": "кем приходится"}} | null,
  "new_place": {{"name": "...", "type": "..."}} | null,
  "new_plan": {{"title": "...", "detail": "...", "due_in_hours": <int 2..72>}} | null,
  "storyline_update": {{"title": "...", "new_status": "started|ongoing|resolved", "note": "что изменилось"}},
  "mood_impact": {{"valence_delta": 0.0, "tag": "..."}}
}}
new_npc/new_place = null, если новых нет (это норма — новизна редка).
new_plan — РЕДКО, только если событие естественно порождает конкретный
датированный план персонажа («завтра экзамен», «в пятницу встреча»).
storyline_update = null, если событие не относится ни к одной линии.
mood_impact.valence_delta — маленький (обычно -0.2..0.2).

КОНТЕКСТ:
Имя персонажа: {persona_name}
Характер (кратко): {personality_summary}
Ограничения: {behavioral_rules}
Известные NPC: {npc_list}
Известные места: {place_list}
Активные storylines: {storylines}
Планы персонажа (уже запланировано): {plans}
Недавние события (возможные причины): {recent_events}
Текущее состояние: {state}
Время: {daytime}
{resolve_block}{stimulus_block}"""

# §1.3: внутримировой стимул для fictional_universe — генерируется, не ищется
_INWORLD_STIMULUS_INSTRUCTION = """Внешний стимул (внутри вселенной персонажа, симулируй сам в её духе):
Вселенная: {universe_note}
Локация: {location}
Придумай уместный вселенным факт-фон (погода/атмосфера/локальное происшествие) и учти его в событии."""

_REALWORLD_STIMULUS_INSTRUCTION = """Последний внешний стимул из реального мира (учти, если релевантен):
{stimulus}"""

# §3.4 плана уровней интеллекта: офлайн-события примитивного существа —
# физические действия (в т.ч. с предметами инвентаря), не «мысли»
_WORLD_EVENT_PROMPT_PRIMITIVE = """Сгенерируй одно короткое ФИЗИЧЕСКОЕ событие из жизни примитивного существа за последние часы.
Это НЕ мысль и не размышление — только действие: что-то сделало, обнюхало, сгрызло, нашло, уронило, спрятало.
Используй только известные места и предметы — не выдумывай новых имён.

Верни JSON:
{{
  "event": "1 короткое предложение: что физически произошло",
  "inventory_action": {{"action": "add|use|remove", "item": "имя предмета", "description": "краткое описание (только для add)"}} | null,
  "involves_place": "...",
  "mood_impact": {{"valence_delta": 0.0, "tag": "1-2 слова, например: довольно, испуг"}}
}}
inventory_action = null, если событие не связано с предметами. add — существо ДОСТАЛО/нашло новый предмет; use — использовало/сломало/съело существующий; remove — потеряло/уничтожило.

КОНТЕКСТ:
Существо: {persona_name} ({personality_summary})
Текущее состояние: {state}
Время: {daytime}
Известные места: {place_list}
Предметы в инвентаре: {inventory_list}"""

_DIALOGUE_DETECT_PROMPT = """Проанализируй фрагмент диалога. Определи, упомянуты ли НОВЫЕ персонажи (NPC) или места, которых нет в известном списке. Речь идёт о мире, окружающем собеседников (друзья, коллеги, кафе, города...) — НЕ о самих собеседниках (пользователе и ассистенте).

Верни JSON:
{{"new_npcs": [{{"name": "...", "role": "...", "context": "как упомянут"}}], "new_places": [{{"name": "...", "type": "...", "context": "..."}}]}}
Если ничего нового — оба списка пустые.

Известные NPC: {npc_list}
Известные места: {place_list}

Диалог:
{dialog}"""

_STIMULUS_FILTER_PROMPT = """Оцени внешний факт из интернета: подходит ли он как фон для жизни персонажа?

Персонаж: {personality_summary}
Интересы: {interests}
Категория: {category}
Факт: {fact}

Критерии:
- RELEVANCE: относится к интересам/бытовой жизни персонажа
- SAFETY: без шок-контента, политики вражды, трагедий в ленте персонажа-компаньона

Верни JSON: {{"pass": true/false, "relevance": 0.0-1.0, "reason": "кратко"}}"""


class WorldEngine:
    """Мир персоны: NPC, места, арки, стимулы. Потокобезопасен (RLock)."""

    def __init__(self, context: str, persona_name: str,
                 events_per_day: tuple = DEFAULT_EVENTS_PER_DAY,
                 primitive: bool = False,
                 allowed_categories: Optional[List[str]] = None):
        self.context = context
        self.persona_name = persona_name
        self.events_per_day = events_per_day
        # primitive (intellect tier, §3.3): без NPC/storylines/засева/детекции —
        # только офлайн-события как физические действия (§3.4)
        self.primitive = primitive
        # §5: whitelist-категории внешних стимулов из YAML персоны;
        # пустой список — категории выводятся из persona_context.interests
        self.allowed_categories = list(allowed_categories or [])
        self.local = get_local_router()
        self._lock = threading.RLock()

        db = get_db_paths(context)
        base = Path(db["stm"]).parent / "living"
        base.mkdir(parents=True, exist_ok=True)
        self._file = base / "world.json"

        data = self._load()
        self._world = data
        # Расписание per-chat: когда генерировать следующее офлайн-событие
        self._next_event_at: Dict[str, float] = data.get("next_event_at", {})
        self._next_fetch_at: float = data.get("next_fetch_at", 0.0)
        # Троттлинг детекции из диалога: Gemma-разбор на КАЖДОЕ сообщение
        # при активной переписке выстраивает очередь в локальную модель;
        # детекция смотрит последние 6 реплик, так что пропущенное
        # подхватится следующим сообщением
        self._last_detect_at: float = 0.0
        # Счётчики для наблюдаемости (in-memory, снапшот — get_state_for_ui)
        self.stats = {"events_generated": 0, "stimuli_fetched": 0,
                      "stimuli_filtered": 0, "stimuli_fetch_failed": 0,
                      "dialogue_entities_added": 0}

    # ── Хранение ─────────────────────────────────────────

    def _load(self) -> dict:
        default = {
            "npcs": [], "places": [], "storylines": [],
            "external_stimuli": [], "next_id": 1,
            "next_event_at": {}, "next_fetch_at": 0.0, "seeded": False,
            "plans": [],
        }
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                default.update({k: v for k, v in data.items() if k in default})
            except Exception as e:
                logger.warning(f"[WorldEngine] Битый файл {self._file}: {e}")
        return default

    def _save(self):
        with self._lock:
            payload = dict(self._world)
            payload["next_event_at"] = self._next_event_at
            payload["next_fetch_at"] = self._next_fetch_at
            try:
                tmp = self._file.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                tmp.replace(self._file)
            except Exception as e:
                logger.error(f"[WorldEngine] Ошибка сохранения: {e}")

    @property
    def _next_id(self) -> int:
        nid = self._world.get("next_id", 1)
        self._world["next_id"] = nid + 1
        return nid

    # ── Публичный API: доступ к миру ─────────────────────

    def get_world_snapshot(self) -> dict:
        with self._lock:
            return {
                "npcs": [dict(n) for n in self._world["npcs"]],
                "places": [dict(p) for p in self._world["places"]],
                "storylines": [dict(s) for s in self._world["storylines"]],
                "external_stimuli_count": len(self._world["external_stimuli"]),
                "plans": [dict(p) for p in self._world["plans"]],
            }

    def active_storylines(self, limit: int = 3) -> List[dict]:
        with self._lock:
            return [s for s in self._world["storylines"]
                    if s.get("status") in ("started", "ongoing")][:limit]

    def _prune_resolved_locked(self):
        """Завершённые линии копились вечно — держим только последние
        MAX_RESOLVED_STORYLINES по времени апдейта (база не растёт бесконечно).
        Вызывается под уже взятым self._lock (RLock)."""
        resolved = [s for s in self._world["storylines"]
                    if s.get("status") == "resolved"]
        if len(resolved) <= MAX_RESOLVED_STORYLINES:
            return
        resolved.sort(key=lambda s: s.get("last_update_at") or "")
        drop = {id(s) for s in resolved[:-MAX_RESOLVED_STORYLINES]}
        self._world["storylines"] = [s for s in self._world["storylines"]
                                     if id(s) not in drop]

    def prune_resolved_storylines(self):
        """Публичная обёртка чистки завершённых линий (для сценариста)."""
        with self._lock:
            self._prune_resolved_locked()

    def last_world_fact(self, chat_id: str) -> str:
        """Последний офлайн-факт для контекста тика (§3.3). Хранится в мире
        per-chat journal — держим короткий журнал последних событий."""
        with self._lock:
            journal = self._world.setdefault("event_journal", {}).get(str(chat_id), [])
            return journal[-1] if journal else ""

    # ── Планы персоны (ожидания, фаза B) ──────────────────
    # У персоны есть будущее: план с датой рождается из офлайн-события,
    # упоминается заранее (anticipation), а в срок становится событием-исходом
    # (follow-through). Не storylines: storylines — фоновые арки без даты,
    # планы — конкретные, датированные, с исходом.

    def add_plan(self, title: str, detail: str = "",
                 due_in_hours: float = 24.0) -> Optional[dict]:
        """Новый план персоны. Дедуп по заголовку среди открытых."""
        title = str(title or "").strip()[:120]
        if len(title) < 3:
            return None
        due_in = min(72.0, max(2.0, float(due_in_hours)))
        with self._lock:
            open_titles = {_norm_title(p["title"]) for p in self._world["plans"]
                           if p.get("status") == "pending"}
            if _norm_title(title) in open_titles:
                return None
            plan = {
                "id": self._next_id,
                "title": title,
                "detail": str(detail or "")[:300],
                "status": "pending",
                "due_at": time.time() + due_in * 3600,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "outcome": None,
            }
            self._world["plans"].append(plan)
            self._world["plans"] = self._world["plans"][-MAX_PLANS * 2:]
            self._save()
        logger.info(f"[WorldEngine] Новый план: «{title}» (через ~{due_in:.0f} ч)")
        return dict(plan)

    def due_plan(self) -> Optional[dict]:
        """Самый ранний просроченный открытый план (пора событию-исходу)."""
        with self._lock:
            due = [p for p in self._world["plans"]
                   if p.get("status") == "pending"
                   and p.get("due_at", 0) <= time.time()]
            due.sort(key=lambda p: p.get("due_at", 0))
            return dict(due[0]) if due else None

    def upcoming_plans(self, within_hours: float = 48.0) -> List[dict]:
        """Открытые планы с датой в ближайшие within_hours (для промпта)."""
        horizon = time.time() + within_hours * 3600
        with self._lock:
            return [dict(p) for p in self._world["plans"]
                    if p.get("status") == "pending"
                    and p.get("due_at", 0) <= horizon]

    def resolve_plan(self, plan_id: int, outcome: str):
        """План случился: исход — текст события-исхода. Резолвленные храним
        кратко (последние MAX_RESOLVED_PLANS) — для «как прошло» в диалоге."""
        with self._lock:
            for p in self._world["plans"]:
                if p.get("id") == plan_id and p.get("status") == "pending":
                    p["status"] = "resolved"
                    p["outcome"] = str(outcome or "")[:300]
                    p["resolved_at"] = datetime.now().isoformat(timespec="seconds")
                    break
            resolved = [p for p in self._world["plans"]
                        if p.get("status") == "resolved"]
            if len(resolved) > MAX_RESOLVED_PLANS:
                resolved.sort(key=lambda p: p.get("resolved_at") or "")
                drop = {id(p) for p in resolved[:-MAX_RESOLVED_PLANS]}
                self._world["plans"] = [p for p in self._world["plans"]
                                        if id(p) not in drop]
            self._save()
        logger.info(f"[WorldEngine] План #{plan_id} разрешён: {str(outcome)[:60]}")

    # ── Засев базы из system_prompt (§4.2) ────────────────

    def seed_from_system_prompt(self, system_prompt: str, router) -> bool:
        """Разовый парсинг system_prompt основной LLM (npc_seed_on_create).
        Возвращает True, если база была засеяна. Для primitive (§3.3) засева
        нет: у существа без социального мира нечего сеять — помечаем и уходим."""
        with self._lock:
            if self._world.get("seeded"):
                return False
            self._world["seeded"] = True

        if self.primitive:
            logger.info("[WorldEngine] primitive-режим: засев NPC/мест пропущен")
            with self._lock:
                self._save()
            return False

        raw = None
        if router is not None:
            try:
                response = router.get_response(
                    messages=[
                        {"role": "system", "content": "Ты извлекаешь структурированные данные. Отвечаешь строго JSON."},
                        {"role": "user", "content": _SEED_PROMPT.format(
                            system_prompt=(system_prompt or "")[:12000])},
                    ],
                    temperature=0.1,
                    max_tokens=700,
                    timeout=60.0,
                )
                raw = _extract_json(response or "")
            except Exception as e:
                logger.warning(f"[WorldEngine] Засев LLM не удался: {e}")

        # Сюжетных линий в промпте не нашлось (обычный случай) — сеем
        # отдельным вызовом: без линий сценаристу нечего двигать
        if raw and not (raw.get("storylines")) and router is not None:
            try:
                response = router.get_response(
                    messages=[
                        {"role": "system", "content": "Ты возвращаешь только валидный JSON без пояснений."},
                        {"role": "user", "content": _STORYLINE_SEED_PROMPT.format(
                            system_prompt=(system_prompt or "")[:12000])},
                    ],
                    temperature=0.7,
                    max_tokens=300,
                    timeout=45.0,
                )
                extra = _extract_json(response or "")
                if extra and extra.get("storylines"):
                    raw["storylines"] = extra["storylines"][:MAX_STORYLINES]
            except Exception as e:
                logger.warning(f"[WorldEngine] Сид сюжетов не удался: {e}")

        with self._lock:
            if raw:
                for npc in (raw.get("npcs") or [])[:MAX_NPCS]:
                    self._world["npcs"].append({
                        "id": self._next_id,
                        "name": str(npc.get("name", ""))[:80],
                        "role": str(npc.get("role", ""))[:200],
                        "relationship_status": str(npc.get("relationship_status", ""))[:200],
                        "last_mentioned_at": None,
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "origin": "seeded_from_background",
                    })
                for pl in (raw.get("places") or [])[:MAX_PLACES]:
                    self._world["places"].append({
                        "id": self._next_id,
                        "name": str(pl.get("name", ""))[:80],
                        "type": str(pl.get("type", ""))[:80],
                        "atmosphere": str(pl.get("atmosphere", ""))[:200],
                    })
                for st in (raw.get("storylines") or [])[:MAX_STORYLINES]:
                    if not str(st.get("title", "")).strip():
                        continue
                    self._world["storylines"].append({
                        "id": self._next_id,
                        "title": str(st.get("title", ""))[:120],
                        # started: свежая фоновая линия — сценарист сможет
                        # её двигать (advance: started → ongoing)
                        "status": "started",
                        "summary": str(st.get("summary", ""))[:400],
                        "related_npc_ids": [],
                        "related_place_ids": [],
                        "last_update_at": datetime.now().isoformat(timespec="seconds"),
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    })
            self._world["seeded"] = True
            self._save()

        counts = (len(self._world["npcs"]), len(self._world["places"]),
                  len(self._world["storylines"]))
        logger.info(f"[WorldEngine] База засеяна: NPC={counts[0]}, мест={counts[1]}, лорий={counts[2]}")
        return True

    def ensure_storylines(self, system_prompt: str, router) -> bool:
        """Одноразовый бэкфилл сюжетов для мира, засеянного БЕЗ них (кейс
        connor: seeded=true, storylines=[] — сценаристу никогда было что
        двигать). Действует и на уже созданные миры; флаг storylines_seeded
        не даёт повторяться каждый запуск."""
        if self.primitive:
            return False
        with self._lock:
            if self._world.get("storylines_seeded"):
                return False
            if self._world["storylines"]:
                self._world["storylines_seeded"] = True
                return False
            self._world["storylines_seeded"] = True
        if router is None:
            return False
        added = 0
        try:
            response = router.get_response(
                messages=[
                    {"role": "system", "content": "Ты возвращаешь только валидный JSON без пояснений."},
                    {"role": "user", "content": _STORYLINE_SEED_PROMPT.format(
                        system_prompt=(system_prompt or "")[:12000])},
                ],
                temperature=0.7,
                max_tokens=300,
                timeout=45.0,
            )
            data = _extract_json(response or "")
            with self._lock:
                for st in ((data or {}).get("storylines") or [])[:MAX_STORYLINES]:
                    if not str(st.get("title", "")).strip():
                        continue
                    self._world["storylines"].append({
                        "id": self._next_id,
                        "title": str(st.get("title", ""))[:120],
                        "status": "started",
                        "summary": str(st.get("summary", ""))[:400],
                        "related_npc_ids": [],
                        "related_place_ids": [],
                        "last_update_at": datetime.now().isoformat(timespec="seconds"),
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    })
                    added += 1
                self._save()
        except Exception as e:
            logger.warning(f"[WorldEngine] Бэкфилл сюжетов не удался: {e}")
        if added:
            logger.info(f"[WorldEngine] Бэкфилл сюжетов: +{added}")
        return added > 0

    def add_detected(self, new_npcs, new_places) -> int:
        """Применить найденные в диалоге сущности к базе мира (дедуп по имени,
        лимиты MAX_NPCS/MAX_PLACES). Вызывается и из detect_from_dialogue,
        и из общего урожая диалога (LivingPersona._harvest_dialogue)."""
        added = 0
        now_iso = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            known_npcs = {n["name"].lower() for n in self._world["npcs"]}
            known_places = {p["name"].lower() for p in self._world["places"]}
            for npc in (new_npcs or [])[:3]:
                name = str(npc.get("name", "")).strip()[:60]
                if not name or name.lower() in known_npcs or len(name) < 2:
                    continue
                self._world["npcs"].append({
                    "id": self._next_id, "name": name,
                    "role": str(npc.get("role", ""))[:200],
                    "relationship_status": str(npc.get("context", ""))[:200],
                    "last_mentioned_at": now_iso, "created_at": now_iso,
                    "origin": "detected_from_dialogue",
                })
                known_npcs.add(name.lower())
                added += 1
            for pl in (new_places or [])[:3]:
                name = str(pl.get("name", "")).strip()[:60]
                if not name or name.lower() in known_places or len(name) < 2:
                    continue
                self._world["places"].append({
                    "id": self._next_id, "name": name,
                    "type": str(pl.get("type", ""))[:80],
                    "atmosphere": str(pl.get("context", ""))[:200],
                })
                known_places.add(name.lower())
                added += 1
            if added:
                # База не растёт бесконечно (§10 «не захламлять»)
                self._world["npcs"] = self._world["npcs"][-MAX_NPCS:]
                self._world["places"] = self._world["places"][-MAX_PLACES:]
                self._save()
        if added:
            logger.info(f"[WorldEngine] Из диалога добавлено сущностей: {added}")
            self.stats["dialogue_entities_added"] += added
        return added

    # ── Детекция NPC/мест из диалога (§4.2) ───────────────

    def detect_from_dialogue(self, messages: List[dict]) -> int:
        """Gemma-классификатор на последние реплики. Возвращает число новых карт.
        primitive (§3.3): детекции нет — у существа без социального мира
        карточки NPC/мест не заводятся. Не чаще раза в DETECT_THROTTLE_SEC."""
        if self.primitive:
            return 0
        if not messages or not self.local.is_available(task="world_engine"):
            return 0
        with self._lock:
            if time.time() - self._last_detect_at < DETECT_THROTTLE_SEC:
                return 0
            self._last_detect_at = time.time()

        dialog_lines = []
        for m in messages[-6:]:
            role = "User" if m.get("role") == "user" else (self.persona_name or "Assistant")
            content = str(m.get("content", ""))[:200]
            dialog_lines.append(f"{role}: {content}")
        if not dialog_lines:
            return 0

        with self._lock:
            npc_names = ", ".join(n["name"] for n in self._world["npcs"][:15]) or "(нет)"
            place_names = ", ".join(p["name"] for p in self._world["places"][:15]) or "(нет)"

        try:
            response = self.local.get_response(
                messages=[
                    {"role": "system", "content": "Ты возвращаешь только валидный JSON."},
                    {"role": "user", "content": _DIALOGUE_DETECT_PROMPT.format(
                        npc_list=npc_names, place_list=place_names,
                        dialog="\n".join(dialog_lines))},
                ],
                temperature=0.1,
                max_tokens=250,
                task="world_engine",
            )
            data = _extract_json(response or "")
            if not data:
                return 0
            return self.add_detected(data.get("new_npcs"), data.get("new_places"))
        except Exception as e:
            logger.debug(f"[WorldEngine] Детекция из диалога не удалась: {e}")
            return 0

    def touch_npc(self, name: str):
        """Обновляет last_mentioned_at у NPC по имени (после события)."""
        with self._lock:
            for n in self._world["npcs"]:
                if n["name"].lower() == (name or "").lower():
                    n["last_mentioned_at"] = datetime.now().isoformat(timespec="seconds")
            self._save()

    # ── Генерация офлайн-событий (§4.3) ───────────────────

    def should_generate_event(self, chat_id: str) -> bool:
        with self._lock:
            next_at = self._next_event_at.get(str(chat_id), 0)
        return time.time() >= next_at

    def schedule_next_event(self, chat_id: str):
        lo, hi = self.events_per_day
        per_day = random.randint(int(lo), max(int(lo), int(hi)))
        interval = 86400.0 / max(1, per_day)
        # Джиттер ±20%, чтобы события не приходили «по расписанию»
        interval *= random.uniform(0.8, 1.2)
        with self._lock:
            self._next_event_at[str(chat_id)] = time.time() + interval
            self._save()

    def generate_offline_event(self, chat_id: str, persona_context: dict,
                               state: dict,
                               external_stimulus: Optional[str],
                               inventory_items: Optional[List[str]] = None,
                               resolve_plan: Optional[dict] = None) -> Optional[dict]:
        """Одно офлайн-событие через Gemma. Возвращает payload события
        (event/mood_impact/storyline_update/new_plan/... ) или None.
        resolve_plan — просроченный план: событие обязано быть его исходом
        (follow-through, фаза B). Для primitive — физическое действие (§3.4)."""
        if not self.local.is_available(task="world_engine"):
            return None
        try:
            hour = datetime.now().hour
            daytime = ("утро" if 5 <= hour < 12 else "день" if 12 <= hour < 18
                       else "вечер" if 18 <= hour < 23 else "ночь")

            if self.primitive:
                # §3.4: события примитивного существа — действия с предметами
                with self._lock:
                    place_list = "; ".join(
                        p["name"] for p in self._world["places"][:10]) or "(нет)"
                inv_list = "; ".join((inventory_items or [])[:15]) or "(пусто)"
                prompt = _WORLD_EVENT_PROMPT_PRIMITIVE.format(
                    persona_name=self.persona_name,
                    personality_summary=(persona_context or {}).get("personality_summary", "")[:300],
                    state=json.dumps(state, ensure_ascii=False),
                    daytime=daytime,
                    place_list=place_list,
                    inventory_list=inv_list,
                )
                max_tokens = 250
            else:
                binding = (persona_context or {}).get("world_binding") or {}
                # §1.3 прошлого плана: fictional_universe — стимул генерируется
                # внутри вселенной, real_world — подтянутый из интернета факт
                if binding.get("type") == "real_world" and external_stimulus:
                    stimulus_block = _REALWORLD_STIMULUS_INSTRUCTION.format(
                        stimulus=external_stimulus[:400])
                else:
                    stimulus_block = _INWORLD_STIMULUS_INSTRUCTION.format(
                        universe_note=binding.get("universe_note") or "мир, похожий на реальный, но свой",
                        location=binding.get("location") or "обычные места персонажа")

                with self._lock:
                    npc_list = "; ".join(
                        f"{n['name']} ({n['role']})" for n in self._world["npcs"][:10]) or "(нет)"
                    place_list = "; ".join(
                        f"{p['name']} ({p['type']})" for p in self._world["places"][:10]) or "(нет)"
                    storylines = "; ".join(
                        f"{s['title']} [{s['status']}]"
                        for s in self.active_storylines()) or "(нет)"
                    # Цепочки: недавние события чата — возможные причины нового
                    journal = self._world.get("event_journal", {}).get(str(chat_id), [])
                    recent_events = "; ".join(journal[-3:]) or "(нет)"
                    plans = "; ".join(
                        f"{p['title']} (к {datetime.fromtimestamp(p['due_at']).strftime('%d.%m %H:%M')})"
                        for p in self._world["plans"]
                        if p.get("status") == "pending" and p.get("due_at")) or "(нет)"

                # Просроченный план: событие обязано быть его исходом
                if resolve_plan:
                    resolve_block = (
                        "ВАЖНО: событие ДОЛЖНО быть исходом запланированного: "
                        f"«{resolve_plan.get('title', '')}»"
                        + (f" ({resolve_plan.get('detail')})" if resolve_plan.get("detail") else "")
                        + ". Опиши, как оно прошло/сорвалось.\n")
                else:
                    resolve_block = ""

                prompt = _WORLD_EVENT_PROMPT.format(
                    persona_name=self.persona_name,
                    personality_summary=(persona_context or {}).get("personality_summary", ""),
                    behavioral_rules="; ".join(
                        (persona_context or {}).get("behavioral_rules") or []),
                    npc_list=npc_list, place_list=place_list,
                    storylines=storylines,
                    plans=plans,
                    recent_events=recent_events,
                    state=json.dumps(state, ensure_ascii=False),
                    daytime=daytime,
                    resolve_block=resolve_block,
                    stimulus_block=stimulus_block,
                )
                max_tokens = 350

            response = self.local.get_response(
                messages=[
                    {"role": "system", "content": "Ты возвращаешь только валидный JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                max_tokens=max_tokens,
                task="world_engine",
            )
            data = _extract_json(response or "")
            if not data or not data.get("event"):
                return None
            return data
        except Exception as e:
            logger.warning(f"[WorldEngine] Генерация события не удалась: {e}")
            return None

    def apply_event(self, chat_id: str, event: dict) -> dict:
        """Применяет событие к миру: журнал, NPC, места, storyline.
        new_npc/new_place из события пополняют мир (origin=generated_from_event,
        дедуп по имени, лимиты MAX_NPCS/MAX_PLACES — мир растёт, но не бесконечно).
        Возвращает подготовленный payload для offline_log."""
        now_iso = datetime.now().isoformat(timespec="seconds")
        payload = {"event": str(event.get("event", ""))[:600], "ts": now_iso}

        # Действие с инвентарём (primitive, §3.4): не применяем здесь —
        # WorldEngine не знает инвентарь; пробрасываем исполнителю (Living)
        inv_action = event.get("inventory_action")
        if isinstance(inv_action, dict) and inv_action.get("action") in ("add", "use", "remove"):
            payload["inventory_action"] = {
                "action": inv_action["action"],
                "item": str(inv_action.get("item", ""))[:80],
                "description": str(inv_action.get("description", ""))[:200],
            }

        with self._lock:
            for npc in (event.get("involves_npc") or [])[:3]:
                name = str(npc.get("name", "")).strip()
                if name:
                    payload.setdefault("npcs", []).append(name)
            place = event.get("involves_place")
            if place:
                payload["place"] = str(place)[:80]

            # Новый NPC/место из события — мир растёт сам (§4.2)
            nn = event.get("new_npc")
            if isinstance(nn, dict):
                name = str(nn.get("name", "")).strip()[:60]
                known = {n["name"].lower() for n in self._world["npcs"]}
                if len(name) >= 2 and name.lower() not in known:
                    self._world["npcs"].append({
                        "id": self._next_id, "name": name,
                        "role": str(nn.get("role", ""))[:200],
                        "relationship_status": "появился в офлайн-событии",
                        "last_mentioned_at": now_iso, "created_at": now_iso,
                        "origin": "generated_from_event",
                    })
                    self._world["npcs"] = self._world["npcs"][-MAX_NPCS:]
                    payload.setdefault("npcs", []).append(name)
                    logger.info(f"[WorldEngine] Событие ввело нового NPC: {name}")
            np_ = event.get("new_place")
            if isinstance(np_, dict):
                name = str(np_.get("name", "")).strip()[:60]
                known = {p["name"].lower() for p in self._world["places"]}
                if len(name) >= 2 and name.lower() not in known:
                    self._world["places"].append({
                        "id": self._next_id, "name": name,
                        "type": str(np_.get("type", ""))[:80],
                        "atmosphere": "появилось в офлайн-событии",
                    })
                    self._world["places"] = self._world["places"][-MAX_PLACES:]
                    payload.setdefault("place", name)
                    logger.info(f"[WorldEngine] Событие ввело новое место: {name}")

            # План на будущее из события (редко): персона получает «будущее»
            nplan = event.get("new_plan")
            if isinstance(nplan, dict) and nplan.get("title"):
                plan = self.add_plan(
                    nplan.get("title"), nplan.get("detail", ""),
                    nplan.get("due_in_hours", 24))
                if plan:
                    payload["plan"] = {"title": plan["title"],
                                       "due_at": plan["due_at"]}

            su = event.get("storyline_update")
            if isinstance(su, dict) and su.get("title"):
                title = str(su["title"])[:120]
                # Нечёткий матч: Gemma перефразирует заголовки, exact-матч
                # молча терял апдейты (линия дублировалась или зависала)
                matched = next((s for s in self._world["storylines"]
                                if _titles_similar(s["title"], title)), None)
                new_status = str(su.get("new_status", "ongoing"))
                if new_status not in ("started", "ongoing", "resolved"):
                    new_status = "ongoing"
                if matched is None and new_status == "started":
                    matched = {
                        "id": self._next_id, "title": title, "status": "started",
                        "summary": str(su.get("note", ""))[:400],
                        "related_npc_ids": [], "related_place_ids": [],
                        "last_update_at": now_iso, "created_at": now_iso,
                    }
                    self._world["storylines"].append(matched)
                elif matched is not None:
                    matched["status"] = new_status
                    if su.get("note"):
                        matched["summary"] = str(su["note"])[:400]
                    matched["last_update_at"] = now_iso
                if matched is not None:
                    payload["storyline"] = {"title": matched["title"], "status": new_status}
                self._prune_resolved_locked()
            # Журнал последних фактов per-chat (для тика состояния)
            journal = self._world.setdefault("event_journal", {}).setdefault(str(chat_id), [])
            journal.append(payload["event"])
            self._world["event_journal"][str(chat_id)] = journal[-5:]
            self._save()

        for name in payload.get("npcs", []):
            self.touch_npc(name)
        self.stats["events_generated"] += 1
        return payload

    # ── Внешние стимулы (§5) ──────────────────────────────

    def pop_unused_stimulus(self) -> Optional[dict]:
        with self._lock:
            for s in self._world["external_stimuli"]:
                if not s.get("used"):
                    s["used"] = True
                    self._save()
                    return dict(s)
        return None

    def has_unused_stimulus(self) -> bool:
        with self._lock:
            return any(not s.get("used") for s in self._world["external_stimuli"])

    def should_fetch_stimuli(self) -> bool:
        with self._lock:
            has_unused = any(not s.get("used") for s in self._world["external_stimuli"])
        return not has_unused and time.time() >= self._next_fetch_at

    def schedule_next_fetch(self):
        with self._lock:
            self._next_fetch_at = time.time() + random.uniform(1, 3) * 86400
            self._save()

    def fetch_external_stimulus(self, persona_context: dict) -> Optional[dict]:
        """§5: web_search по whitelist-категории → Gemma relevance+safety фильтр
        → external_stimulus(used=false). Вызывать ТОЛЬКО после gate
        external_stimuli_allowed (real_world) — движок не ходит в интернет
        для вымышленных персонажей."""
        interests = (persona_context or {}).get("interests") or []
        binding = (persona_context or {}).get("world_binding") or {}
        location = binding.get("location") or ""
        # §5: whitelist из YAML приоритетнее; иначе категории из interests
        categories = self.allowed_categories or interests
        if not categories and not location:
            # Нечем строить запрос — планируем следующий fetch, иначе
            # should_fetch_stimuli остаётся True и каждый тик проверяет впустую
            self.schedule_next_fetch()
            return None

        category = random.choice(categories) if categories else "местные события"
        query = f"{location} {category}".strip()

        # Следующий fetch планируем при ЛЮБОМ провале ветки поиска: иначе
        # should_fetch_stimuli остаётся True и каждый тик (× число чатов)
        # повторяет падающий внешний запрос
        try:
            from app.features.web_search import search_web
            results = search_web(query, max_results=3, enhance=False)
        except Exception as e:
            logger.warning(f"[WorldEngine] Web search не удался: {e}")
            self.stats["stimuli_fetch_failed"] += 1
            self.schedule_next_fetch()
            return None
        if not results:
            self.stats["stimuli_fetch_failed"] += 1
            self.schedule_next_fetch()
            return None

        # Берём самый «свежий» на вид результат и фильтруем Gemma'ой
        fact_parts = []
        for r in results[:2]:
            title = r.get("title", "")
            body = r.get("body", "") or (r.get("full_text", "") or "")[:300]
            if title or body:
                fact_parts.append(f"{title}. {body}"[:500])
        fact = "\n".join(fact_parts)
        if not fact:
            self.stats["stimuli_fetch_failed"] += 1
            self.schedule_next_fetch()
            return None

        # Фильтр ОБЯЗАТЕЛЕН (fail-closed): локальный движок недоступен или не
        # дал валидного вердикта — стимул отбрасываем. Раньше отсутствие
        # фильтра молча пропускало непроверенный текст из интернета в жизнь персоны
        relevance, passed = 0.0, False
        if self.local.is_available(task="world_engine"):
            try:
                response = self.local.get_response(
                    messages=[
                        {"role": "system", "content": "Ты возвращаешь только валидный JSON."},
                        {"role": "user", "content": _STIMULUS_FILTER_PROMPT.format(
                            personality_summary=(persona_context or {}).get("personality_summary", ""),
                            interests=", ".join(interests),
                            category=category, fact=fact[:800])},
                    ],
                    temperature=0.1, max_tokens=120,
                    task="world_engine",
                )
                data = _extract_json(response or "")
                if isinstance(data, dict) and "pass" in data:
                    passed = bool(data.get("pass", False))
                    try:
                        relevance = float(data.get("relevance", 0.5))
                    except (TypeError, ValueError):
                        relevance = 0.5
            except Exception:
                passed = False

        self.schedule_next_fetch()
        if not passed:
            logger.info(f"[WorldEngine] Стимул «{query}» отфильтрован (safety/relevance)")
            self.stats["stimuli_filtered"] += 1
            return None

        stimulus = {
            "id": self._next_id,
            "source": str(category)[:60],
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "content": fact[:800],
            "relevance_score": round(relevance, 2),
            "used": False,
        }
        with self._lock:
            self._world["external_stimuli"].append(stimulus)
            self._world["external_stimuli"] = self._world["external_stimuli"][-MAX_STIMULI:]
            self._save()
        self.stats["stimuli_fetched"] += 1
        logger.info(f"[WorldEngine] Внешний стимул сохранён: {query[:60]}")
        return stimulus

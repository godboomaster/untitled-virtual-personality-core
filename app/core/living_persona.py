"""
LivingPersona — оркестратор «живой» персоны (слои 2 и 3 плана).

Связывает вместе:
  PersonaContextLayer (§1)  — выжимка system_prompt + world_binding gate
  StateEngine (§3)          — тики состояния + offline_log + скоринг инициативы
  WorldEngine (§4, §5)      — NPC/места/арки + офлайн-события + внешние стимулы
  OfflineSummarizer (§6)    — дневные эпизоды, приветствие-дневник, сценарист

Единый фоновый цикл (§9 фазы 1-5):
  каждые tick_interval_minutes:
    - тик состояния для каждого известного чата
    - офлайн-событие мира по расписанию events_per_day
    - внешние стимулы по расписанию 1-3 дня (только real_world, жёсткий gate)
    - при score > порога: сигнал в существующий proactive-модуль
  раз в день: суммаризация offline_log → episode (основная LLM)
  раз в 1-2 недели: сценарист продвигает storylines (основная LLM)

Всё, что видит пользователь, генерирует основная LLM с полным system_prompt;
здесь только структурные операции и черновики (§1.2).
"""

import asyncio
import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.core.config import get_db_paths
from app.core.local_router import get_local_router
from app.core.persona_context import PersonaContextLayer
from app.core.state_engine import StateEngine, INITIATIVE_THRESHOLD
from app.core.world_engine import WorldEngine
from app.core.offline_summarizer import OfflineSummarizer
from app.core.relationship import RelationshipMemory
from app.core.language import detect_dialogue_language

logger = logging.getLogger(__name__)

# §3.2: «для неактивных пользователей — реже». Чат с молчанием дольше
# INACTIVE_SILENCE_HOURS тикается в INACTIVE_TICK_FACTOR раз реже
# (база 20 мин → ~2 ч): state/world почти не меняются без собеседника,
# а Gemma-вызовы на каждый мёртвый чат каждые 20 минут — чистая трата.
INACTIVE_SILENCE_HOURS = 72
INACTIVE_TICK_FACTOR = 6

# Диалог → состояние/мир/отношения: ОДИН локальный вызов («урожай диалога»)
# вместо трёх разрозненных (NPC-детекция, mood, моменты) на одни и те же
# реплики. Планировщик: каждые HARVEST_MIN_MESSAGES сообщений пользователя
# или по таймеру HARVEST_INTERVAL_SEC (что раньше)
HARVEST_INTERVAL_SEC = 600
HARVEST_MIN_MESSAGES = 10

# Офлайн-событие мира не генерируется посреди активного диалога (§4.3):
# «за последние часы случилось» не должно падать в разгар переписки —
# событие остаётся дью и сработает на следующем тике, когда чат затихнет
EVENT_DEFER_QUIET_MINUTES = 30

# Топическая зацепка факта жизни к реплике пользователя (§7): совпадение
# содержательных слов — тот же подход, что _extract_topics в proactive.
_TOPIC_STOP = {
    "этот", "этого", "этой", "этом", "твой", "твоя", "твое", "твои",
    "который", "которая", "которое", "которые", "пользователь",
    "просто", "очень", "может", "нужно", "хочется", "тебя", "меня",
    "что", "как", "это", "вот", "так", "уже", "еще", "ещё", "когда",
    "если", "только", "сегодня", "завтра", "вчера", "сейчас", "потом",
    "the", "this", "that", "with", "you", "your", "have", "what",
    "when", "then", "than", "just", "really", "about",
}


def _topic_words(text: str) -> set:
    return {w for w in re.findall(r"[а-яА-ЯёЁa-zA-Z]{4,}", (text or "").lower())
            if w not in _TOPIC_STOP}


def _topics_overlap(a: str, b: str) -> bool:
    """Есть ли общие содержательные слова (≥4 букв): точное совпадение или
    по префиксу — русская морфология («детройт»/«детройте») ломает exact."""
    wa, wb = _topic_words(a), _topic_words(b)
    if wa & wb:
        return True
    return any(x.startswith(y) or y.startswith(x) for x in wa for y in wb)


# Урожай диалога: ОДИН вызов вместо трёх (NPC/места + mood + моменты/темы/
# позиции). Раньше мир, состояние и отношения разбирали одни и те же реплики
# независимо — на веб-чате это три side-вызова на каждый разбор.
_HARVEST_PROMPT = """Проанализируй фрагмент диалога между персонажем ({persona_name}) и пользователем. Один проход — несколько выводов сразу. Верни СТРОГО JSON без markdown:

{{
  "new_npcs": [{{"name": "...", "role": "...", "context": "как упомянут"}}],
  "new_places": [{{"name": "...", "type": "...", "context": "..."}}],
  "mood_impact": {{"valence_delta": <float -0.3..0.3>, "tag": "<1-2 слова настроения или пустая строка>"}},
  "moments": ["<общий момент/внутренняя шутка, до 10 слов>", ...],
  "topics": ["<общая тема интересов>", ...],
  "stance_changes": [{{"topic": "<тема>", "position": "<текущая позиция персонажа, коротко>"}}]
}}

ПРАВИЛА:
- new_npcs/new_places — НОВЫЕ персонажи/места мира вокруг собеседников (друзья, коллеги, кафе, города), которых нет в известных списках; НЕ сами собеседники
- mood_impact — как реплики повлияли на состояние ПЕРСОНАЖА (теплота/интерес к нему — плюс, резкость/пренебрежение — минус); нейтральная беседа — 0.0 и ""
- moments/topics/stance_changes — только НОВОЕ, чего нет в известных списках; персонаж высказал или пересмотрел мнение → stance_changes
- чего-то нет — пустые списки/нулевая дельта (это нормально)

Персонаж (кратко): {personality_summary}
Известные NPC: {known_npcs}
Известные места: {known_places}
Известные моменты: {known_moments}
Известные темы: {known_topics}
Текущие позиции персонажа: {known_stances}

Диалог:
{dialog}"""


class LivingPersonaConfig:
    """features.life / state_engine / world_lore / external_stimuli из YAML.

    life — фича-рубильник «жизнь персоны»: true (или dict с enabled: true)
    включает ВЕСЬ стек с дефолтами — не нужно знать про отдельные блоки.
    Тонкая настройка (tick_interval_minutes, events_per_day и т.п.) — прямо
    в dict'е life или в отдельных блоках; явный блок имеет приоритет, его
    enabled: false гасит слой даже при life: true."""

    def __init__(self, features: dict):
        features = features or {}

        life_raw = features.get("life")
        if isinstance(life_raw, bool):
            life_raw = {"enabled": life_raw}
        life_cfg = life_raw if isinstance(life_raw, dict) else {}
        life_on = bool(life_cfg.get("enabled", False)) if life_cfg else False

        state_cfg = features.get("state_engine")
        if isinstance(state_cfg, bool):
            state_cfg = {"enabled": state_cfg}
        if not isinstance(state_cfg, dict):
            state_cfg = {}
        if not state_cfg and life_on:
            # фича включена, отдельного блока нет — слой со значениями из life
            state_cfg = {
                "enabled": True,
                "tick_interval_minutes": life_cfg.get("tick_interval_minutes", 20),
                "use_gemma": life_cfg.get("use_gemma", True),
            }
        self.state_enabled = bool(state_cfg.get("enabled", False))
        self.tick_interval_minutes = int(state_cfg.get("tick_interval_minutes", 20))
        self.use_gemma = bool(state_cfg.get("use_gemma", True))

        world_cfg = features.get("world_lore")
        if isinstance(world_cfg, bool):
            world_cfg = {"enabled": world_cfg}
        if not isinstance(world_cfg, dict):
            world_cfg = {}
        if not world_cfg and life_on:
            world_cfg = {
                "enabled": True,
                "npc_seed_on_create": life_cfg.get("npc_seed_on_create", True),
                "max_active_storylines": life_cfg.get("max_active_storylines", 2),
                "events_per_day": life_cfg.get("events_per_day", [1, 3]),
            }
        self.world_enabled = bool(world_cfg.get("enabled", False))
        self.npc_seed_on_create = bool(world_cfg.get("npc_seed_on_create", True))
        self.max_active_storylines = int(world_cfg.get("max_active_storylines", 2))
        events = world_cfg.get("events_per_day") or [1, 3]
        if isinstance(events, (list, tuple)) and len(events) == 2:
            self.events_per_day = (int(events[0]), int(events[1]))
        else:
            self.events_per_day = (1, 3)

        stimuli_cfg = (features or {}).get("external_stimuli") or {}
        if isinstance(stimuli_cfg, bool):
            stimuli_cfg = {"enabled": stimuli_cfg}
        # §1.3: явный enabled в YAML — ручной override; без него дефолт
        # выводится из world_binding.type (true только для real_world) —
        # см. LivingPersona.external_stimuli_allowed()
        self.stimuli_flag_explicit = "enabled" in stimuli_cfg
        self.external_stimuli_flag = bool(stimuli_cfg.get("enabled", False))
        self.allowed_categories = list(stimuli_cfg.get("allowed_categories") or [])

        # Комната/настроение в вебе: с включённой жизнью оживает по умолчанию
        self.ui_room_mood_sync = bool(features.get("ui_room_mood_sync", life_on))

    @property
    def enabled(self) -> bool:
        return self.state_enabled or self.world_enabled


def _manual_world_binding(persona) -> Optional[Dict]:
    """world_binding из YAML персоны (top-level ключ): {type, location,
    universe_note}. None — не задан, работает LLM-экстракт как раньше."""
    try:
        mb = (getattr(persona, "persona_data", None) or {}).get("world_binding")
        if isinstance(mb, dict) and str(mb.get("type") or "").strip():
            return mb
    except Exception:
        pass
    return None


class LivingPersona:
    """Фасад над всеми подсистемами живой персоны + фоновый цикл."""

    def __init__(self, context: str, persona, router, config: LivingPersonaConfig,
                 self_memory=None, intellect=None, inventory_manager=None):
        self.context = context
        self.persona = persona
        self.router = router
        self.config = config
        self.self_memory = self_memory
        # Уровень интеллекта (план уровней): primitive сужает слои —
        # state без вербализации-рефлексии, world без NPC/арок (события =
        # физические действия, в т.ч. с инвентарём §3.4), эпизоды-вспышки
        self.intellect = intellect
        self.primitive = bool(intellect is not None and intellect.is_primitive)
        # Инвентарь для офлайн-действий примитивных существ (add/use/remove)
        self.inventory_manager = inventory_manager

        # Последний язык пользователя по чату ('ru'/'en'): дневник/тезисы
        # офлайн-жизни пишутся на нём. Обновляется в on_user_message —
        # суммаризатор своего доступа к STM не имеет.
        self._chat_user_lang: Dict[str, str] = {}
        # Планировщик урожая диалога: chat_id -> ts последнего вызова /
        # сообщений пользователя с последнего вызова
        self._harvest_at: Dict[str, float] = {}
        self._harvest_msgs: Dict[str, int] = {}

        self.persona_context_layer = PersonaContextLayer(
            context, router,
            # Ручная привязка к миру из YAML (top-level world_binding) —
            # приоритет над LLM-экстрактом: гейт стимулов не перероллится
            manual_binding=_manual_world_binding(persona))
        self.state_engine = StateEngine(
            context, persona.persona_name,
            tick_interval_minutes=config.tick_interval_minutes,
            primitive=self.primitive,
            use_gemma=config.use_gemma)
        self.world_engine = WorldEngine(
            context, persona.persona_name,
            events_per_day=config.events_per_day,
            primitive=self.primitive,
            allowed_categories=config.allowed_categories)
        self.summarizer = OfflineSummarizer(
            context, persona.persona_name, router,
            primitive=self.primitive)
        # Память отношений (фаза 2.1): счётчики + общие моменты/темы;
        # у primitive нет вербальной истории отношений — блок не строится
        self.relationship = RelationshipMemory(context, primitive=self.primitive)

        # §3.3 плана уровней: если уровень intelligence явно выключает слой
        # мира (override world_lore_enabled: false у primitive) — гасим его
        # целиком, pipeline-проверка, а не только конфиг-флаг
        if intellect is not None and intellect.active and config.world_enabled:
            if not (intellect.world_lore_full(True) or intellect.world_lore_partial(True)):
                config.world_enabled = False
                logger.info(f"[Living] Слой мира выключен уровнем интеллекта ({intellect.tier})")

        # Кэш выжимки на жизнь процесса (инвалидация — по хэшу промпта в слое)
        self._persona_context: Optional[dict] = None
        self._pc_lock = threading.Lock()

        # Сигнал в proactive: async (chat_id, score, reason) -> None
        self.on_initiative_signal: Optional[Callable] = None
        # Дешёвые гейты перед LLM-скорингом (chat_id) -> bool: ставит proactive
        # (initiative_cheaply_possible). Без него скоринг идёт каждому чату
        # каждый тик — вплоть до ночи вне окна самоинициативы
        self.pre_initiative_gate: Optional[Callable[[str], bool]] = None
        # Источник известных чатов: () -> list[str] (activity tracker)
        self.get_known_chats: Optional[Callable[[], List[str]]] = None
        # Время последнего сообщения чата: (chat_id) -> float
        self.get_last_message_time: Optional[Callable[[str], float]] = None
        # Время последней proactive-инициативы чата: (chat_id) -> float
        self.get_last_initiative_time: Optional[Callable[[str], float]] = None

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._seeded_this_run = False

        # Счётчики для наблюдаемости (§9: раскатка фаз по метрикам).
        # In-memory: обнуляются при рестарте процесса; снапшот — get_state_for_ui,
        # дневная история — metrics_log.jsonl (_persist_metrics_daily)
        self.metrics = {
            "ticks_total": 0, "ticks_throttled": 0,
            "initiative_signals": 0, "events_generated": 0,
            "episodes_written": 0, "screenwriter_runs": 0,
        }
        self._metrics_saved_day = ""

    # ── Выжимка персоны ──────────────────────────────────

    def persona_context(self) -> dict:
        """Актуальная выжимка (переизвлекается при правке system_prompt)."""
        with self._pc_lock:
            if self._persona_context is None:
                self._persona_context = self.persona_context_layer.get(
                    self.persona.system_prompt)
            else:
                # Дешёвая проверка: не поменялся ли промпт (по хэшу в слое)
                self._persona_context = self.persona_context_layer.get(
                    self.persona.system_prompt)
            return self._persona_context

    def external_stimuli_allowed(self) -> bool:
        """Жёсткий gate §10: реальный интернет только для real_world-персон.
        Флаг: явный `external_stimuli.enabled` из YAML — ручной override
        (в т.ч. выключение для real_world); если не задан — дефолт по
        world_binding.type (§1.3). Для fictional/unspecified — False всегда."""
        from app.core.persona_context import default_external_stimuli_flag
        pc = self.persona_context()
        flag = (self.config.external_stimuli_flag
                if self.config.stimuli_flag_explicit
                else default_external_stimuli_flag(pc))
        return self.persona_context_layer.external_stimuli_allowed(
            pc, {"external_stimuli": {"enabled": flag}})

    # ── Публичный API для интеграций ─────────────────────

    def on_user_message(self, chat_id: str, messages: List[dict]) -> None:
        """Вызывается из process_message ПОСЛЕ добавления сообщения в STM.
        Раньше здесь была вторая ветка приветствия-дневника — мёртвая:
        бот-инстанс собирает дневник возвращения ДО входа сообщения в STM
        (_build_living_context), а пост-фактум проверка «≥12 ч absence» уже
        не проходит. Живое: язык пользователя (дневник офлайн-жизни пишется
        на нём), счётчики отношений и планировщик общего урожая диалога —
        всё дешёвое, LLM-вызов уходит в фоновый поток."""
        try:
            user_lang = detect_dialogue_language("", messages)
            if user_lang:
                self._chat_user_lang[str(chat_id)] = user_lang
        except Exception as e:
            logger.warning(f"[Living] Язык чата не обновлён: {e}")

        if not messages:
            return
        try:
            self.relationship.record_message(str(chat_id))
        except Exception as e:
            logger.debug(f"[Living] Отношения не обновлены: {e}")

        # Урожай диалога (один локальный вызов на NPC + mood + моменты):
        # раз в HARVEST_MIN_MESSAGES реплик пользователя или по таймеру
        cid = str(chat_id)
        msgs_n = self._harvest_msgs.get(cid, 0) + 1
        self._harvest_msgs[cid] = msgs_n
        last = self._harvest_at.get(cid, 0.0)
        now = time.time()
        if msgs_n >= HARVEST_MIN_MESSAGES or (msgs_n >= 1 and now - last >= HARVEST_INTERVAL_SEC):
            self._harvest_msgs[cid] = 0
            self._harvest_at[cid] = now
            threading.Thread(target=self._harvest_dialogue, args=(cid, messages),
                             daemon=True,
                             name=f"living-harvest-{self.context}").start()

    def _harvest_dialogue(self, chat_id: str, messages: List[dict]):
        """Один локальный вызов по свежему диалогу → раздача трём движкам:
        новые NPC/места — миру, mood_impact — состоянию, моменты/темы/
        позиции — памяти отношений. Фоновый поток, никогда не бросает."""
        local = get_local_router()
        if not local.is_available(task="dialogue_harvest"):
            return
        lines = []
        for m in (messages or [])[-8:]:
            role = ("User" if m.get("role") == "user"
                    else (self.persona.persona_name or "Assistant"))
            content = str(m.get("content", ""))[:200].strip()
            if content:
                lines.append(f"{role}: {content}")
        if len(lines) < 4:
            return
        try:
            pc = self.persona_context()
        except Exception:
            pc = {}
        known_npcs = known_places = "(нет)"
        if self.config.world_enabled:
            try:
                snap = self.world_engine.get_world_snapshot()
                known_npcs = "; ".join(n["name"] for n in snap["npcs"][:15]) or "(нет)"
                known_places = "; ".join(p["name"] for p in snap["places"][:15]) or "(нет)"
            except Exception:
                pass
        known_moments, known_topics, known_stances = \
            self.relationship.known_lists(chat_id)
        try:
            response = local.get_response(
                messages=[
                    {"role": "system", "content": "Ты возвращаешь только валидный JSON без пояснений."},
                    {"role": "user", "content": _HARVEST_PROMPT.format(
                        persona_name=self.persona.persona_name or "персонаж",
                        personality_summary=(pc or {}).get("personality_summary", "")[:300],
                        known_npcs=known_npcs, known_places=known_places,
                        known_moments=known_moments, known_topics=known_topics,
                        known_stances=known_stances,
                        dialog="\n".join(lines))},
                ],
                temperature=0.2,
                max_tokens=500,
                task="dialogue_harvest",
            )
            from app.core.persona_context import _extract_json
            data = _extract_json(response or "")
        except Exception as e:
            logger.debug(f"[Living] Урожай диалога не удался: {e}")
            return
        if not isinstance(data, dict):
            return

        # Раздача по движкам
        if self.config.world_enabled:
            try:
                self.world_engine.add_detected(
                    data.get("new_npcs"), data.get("new_places"))
            except Exception as e:
                logger.debug(f"[Living] Урожай: мир не обновлён: {e}")
        if self.config.state_enabled:
            mi = data.get("mood_impact")
            if isinstance(mi, dict):
                try:
                    delta = float(mi.get("valence_delta", 0) or 0)
                except (TypeError, ValueError):
                    delta = 0.0
                delta = max(-0.35, min(0.35, delta))
                if abs(delta) >= 0.05:
                    self.state_engine.apply_mood_impact(
                        chat_id, delta, str(mi.get("tag", "") or "")[:80])
        try:
            self.relationship.add_extracted(
                chat_id, data.get("moments"), data.get("topics"),
                data.get("stance_changes"))
        except Exception as e:
            logger.debug(f"[Living] Урожай: отношения не обновлены: {e}")

    def _apply_inventory_action(self, action: Optional[dict]):
        """Исполняет inventory_action из офлайн-события примитива (§3.4):
        существо достало/использовало/потеряло предмет — физическое
        изменение инвентаря, без LLM."""
        if not action or self.inventory_manager is None:
            return
        try:
            kind = action.get("action")
            item = str(action.get("item", "")).strip()
            if not item or len(item) < 2:
                return
            if kind == "add":
                self.inventory_manager.add_item(
                    item, str(action.get("description", ""))[:200],
                    source="living_event")
                logger.info(f"[Living] Инвентарь: существо добыло «{item}»")
            elif kind == "use":
                found = self.inventory_manager.has_item(item) and item
                if not found:
                    # нестрогий поиск по подстроке — Gemma склоняет слова
                    for i in self.inventory_manager.get_items():
                        if item.lower() in i.name.lower() or i.name.lower() in item.lower():
                            found = i.name
                            break
                if found:
                    self.inventory_manager.use_item(found)
                    logger.info(f"[Living] Инвентарь: существо использовало «{found}»")
            elif kind == "remove":
                self.inventory_manager.remove_item(item)
                logger.info(f"[Living] Инвентарь: существо потеряло «{item}»")
        except Exception as e:
            logger.debug(f"[Living] Инвентарное действие не удалось: {e}")

    def get_living_context(self, chat_id: str, topic_text: str = "") -> Optional[str]:
        """Блок контекста для prepare_messages (§7): текущее состояние +
        последний офлайн-факт. Компактный — основной промпт и так большой.

        topic_text — текущая реплика пользователя: последний факт жизни
        попадает в промпт только при топической зацепке (реактивная подача
        «это мне напомнило…» вместо анонса в каждом ответе). Без topic_text —
        факт включается всегда (инициативы/напоминания: там он и есть
        содержание)."""
        if not self.config.state_enabled:
            return None
        parts = [self.state_engine.get_state_context_block(chat_id)]
        if self.config.world_enabled:
            fact = self.world_engine.last_world_fact(chat_id)
            if fact and (not topic_text or _topics_overlap(topic_text, fact)):
                parts.append(
                    f"[LAST THING THAT HAPPENED IN YOUR LIFE]\n{fact}\n"
                    "This happened to you recently outside the dialogue. "
                    "You may reference it naturally if relevant — never as a report."
                )
        # Ближайшие планы персоны (фаза B): у персоны есть будущее — можно
        # упомянуть заранее (anticipation), а не только «что было»
        if self.config.world_enabled and not self.primitive:
            try:
                plans = self.world_engine.upcoming_plans()
                if plans:
                    lines = []
                    for p in plans[:2]:
                        hrs = max(0.0, (p.get("due_at", 0) - time.time()) / 3600)
                        when = ("сегодня" if hrs < 18 else
                                "завтра" if hrs < 42 else f"через ~{int(hrs // 24)} дн.")
                        line = f"- {p.get('title', '')} ({when})"
                        if p.get("detail"):
                            line += f" — {p['detail']}"
                        lines.append(line)
                    parts.append(
                        "[UPCOMING IN YOUR LIFE]\n" + "\n".join(lines) + "\n"
                        "These are your own upcoming plans. You may mention one "
                        "naturally if it fits — as lived anticipation, never "
                        "as a report.")
            except Exception:
                pass
        # Отношения с пользователем (фаза 2.1): стадия + общие темы/моменты
        try:
            rel = self.relationship.get_context_block(chat_id)
            if rel:
                parts.append(rel)
        except Exception:
            pass
        return "\n\n".join(p for p in parts if p) or None

    def get_state_for_ui(self, chat_id: str) -> dict:
        """Снимок для вкладок комната/настроение (ui_room_mood_sync, §7).
        metrics — операционные счётчики движков (наблюдаемость, §9)."""
        snapshot = {
            "enabled": self.config.state_enabled,
            "ui_sync": self.config.ui_room_mood_sync,
            "state": self.state_engine.get_state(chat_id) if self.config.state_enabled else None,
        }
        try:
            snapshot["relationship"] = self.relationship.get_snapshot(chat_id)
        except Exception:
            pass
        if self.config.world_enabled:
            world = self.world_engine.get_world_snapshot()
            snapshot["world"] = {
                "storylines": world["storylines"],
                "npcs": world["npcs"][-8:],
                "places": world["places"][-8:],
                "plans": world.get("plans", []),
            }
            snapshot["last_events"] = self.state_engine.unconsumed(chat_id, limit=5)
            # Лента комнаты: недавние события независимо от consumed — иначе
            # после дневника/инициативы лента молча падала в моки
            snapshot["recent_events"] = self.state_engine.recent_entries(
                chat_id, limit=8)
        help_stats = {}
        try:
            from app.features.help_style import get_stats as _help_stats
            help_stats = _help_stats()
        except Exception:
            pass
        conv_stats = {}
        try:
            from app.features.conversation_style import get_stats as _conv_stats
            conv_stats = _conv_stats()
        except Exception:
            pass
        snapshot["metrics"] = {
            "living": dict(self.metrics),
            "state_engine": dict(self.state_engine.stats),
            "world_engine": dict(self.world_engine.stats),
            "help_style": help_stats,
            "conversation_style": conv_stats,
        }
        return snapshot

    # ── Фоновый цикл ─────────────────────────────────────

    def _persist_metrics_daily(self):
        """Снапшот счётчиков движков в metrics_log.jsonl раз в день (§9):
        in-memory метрики обнуляются рестартом, а вопрос «работает ли жизнь
        и не спамит ли локальную модель» должен отвечаться задним числом."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._metrics_saved_day == today:
            return
        self._metrics_saved_day = today
        try:
            db = get_db_paths(self.context)
            path = Path(db["stm"]).parent / "living" / "metrics_log.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "date": today,
                "ts": datetime.now().isoformat(timespec="seconds"),
                "living": dict(self.metrics),
                "state_engine": dict(self.state_engine.stats),
                "world_engine": dict(self.world_engine.stats),
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[Living] Снапшот метрик не записан: {e}")
    def start(self, loop: asyncio.AbstractEventLoop,
              get_known_chats: Optional[Callable[[], List[str]]] = None,
              get_last_message_time: Optional[Callable[[str], float]] = None):
        if not self.config.enabled:
            return
        if self._running:
            return
        self._running = True
        self.get_known_chats = get_known_chats or self.get_known_chats
        self.get_last_message_time = get_last_message_time or self.get_last_message_time
        self._task = loop.create_task(self._loop())
        logger.info(
            f"[Living] Запущен для {self.context} | "
            f"state={self.config.state_enabled} world={self.config.world_enabled} | "
            f"тик={self.config.tick_interval_minutes}мин")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            logger.info("[Living] Остановлен")

    async def _loop(self):
        # Первый заход: выжимка + засев мира (разовые вызовы основной LLM)
        try:
            await asyncio.to_thread(self.persona_context)
            if (self.config.world_enabled and self.config.npc_seed_on_create
                    and not self._seeded_this_run):
                await asyncio.to_thread(
                    self.world_engine.seed_from_system_prompt,
                    self.persona.system_prompt, self.router)
                self._seeded_this_run = True
            # Мир мог быть засеян раньше БЕЗ сюжетов (кейс connor) —
            # одноразовый бэкфилл, чтобы сценаристу было что двигать
            if self.config.world_enabled and not self.primitive:
                await asyncio.to_thread(
                    self.world_engine.ensure_storylines,
                    self.persona.system_prompt, self.router)
        except Exception as e:
            logger.warning(f"[Living] Инициализация не удалась (повторим позже): {e}")

        while self._running:
            try:
                signals = await asyncio.to_thread(self._tick_all)
                # Сигналы инициативы планируем здесь, в event loop:
                # из рабочего потока to_thread loop недоступен (get_event_loop
                # в чужом потоке кидает RuntimeError)
                for sig in signals or []:
                    try:
                        chat_id, score, reason = sig
                        asyncio.get_running_loop().create_task(
                            self.on_initiative_signal(chat_id, score, reason))
                    except Exception as e:
                        logger.debug(f"[Living] Сигнал инициативы не поставлен: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[Living] Ошибка цикла: {e}")
            await asyncio.sleep(max(1, self.config.tick_interval_minutes) * 60)

    def _known_chats(self) -> List[str]:
        chats = set(self.state_engine._states.keys())
        if self.get_known_chats:
            try:
                chats.update(str(c) for c in self.get_known_chats())
            except Exception:
                pass
        return [c for c in chats if c]

    def _chat_throttled(self, chat_id: str) -> bool:
        """True — чат неактивен давно, тик прореживаем (§3.2).
        Чаты без метки активности (свежие/неизвестные) не прореживаются."""
        if not self.get_last_message_time:
            return False
        try:
            last = self.get_last_message_time(chat_id)
        except Exception:
            return False
        if not last:
            return False
        silence_h = (time.time() - last) / 3600
        if silence_h < INACTIVE_SILENCE_HOURS:
            return False
        last_tick = self.state_engine._states.get(str(chat_id), {}).get("last_tick_at", 0)
        interval = max(1, self.config.tick_interval_minutes) * 60 * INACTIVE_TICK_FACTOR
        return (time.time() - last_tick) < interval

    def _tick_all(self) -> List[tuple]:
        """Один проход цикла: тики + события + стимулы + инициативы.
        Возвращает сигналы инициативы [(chat_id, score, reason)] — их
        планированием на loop занимается асинхронный _loop()."""
        signals: List[tuple] = []
        pc = self.persona_context()
        chats = self._known_chats()
        if not chats:
            return signals
        self._persist_metrics_daily()

        # Внешние стимулы: 1 раз/1-3 дня, жёсткий gate по world_binding (§5)
        fetch_stimulus = False
        if (self.config.world_enabled and self.world_engine.should_fetch_stimuli()
                and self.external_stimuli_allowed()):
            fetch_stimulus = True

        active_chats = []
        for chat_id in chats:
            if self._chat_throttled(chat_id):
                self.metrics["ticks_throttled"] += 1
                continue
            active_chats.append(chat_id)
            try:
                sig = self._tick_chat(chat_id, pc, fetch_stimulus)
                if sig:
                    signals.append(sig)
            except Exception as e:
                logger.error(f"[Living] Тик {chat_id} не удался: {e}")

        # Дневная суммаризация по чату с накопленными записями (§6)
        try:
            for chat_id in active_chats:
                entries = self.state_engine.unconsumed(chat_id, limit=40)
                if entries and self.summarizer.should_run_daily(
                        chat_id, len(entries)):
                    episode = self.summarizer.daily_summarize(
                        chat_id, entries, self.persona,
                        self.state_engine, self.self_memory,
                        user_language=self._chat_user_lang.get(str(chat_id)))
                    if episode:
                        self.metrics["episodes_written"] += 1
        except Exception as e:
            logger.error(f"[Living] Суммаризация не удалась: {e}")

        # Сценарист: раз в 1-2 недели (§6). Метрика — только реальные прогоны
        # (продвинул хотя бы одну линию), иначе счётчик врал о пустых запусках
        try:
            if self.config.world_enabled and self.summarizer.should_run_screenwriter(
                    self.world_engine):
                advanced = self.summarizer.advance_storylines(
                    self.persona, self.world_engine)
                if advanced:
                    self.metrics["screenwriter_runs"] += 1
        except Exception as e:
            logger.error(f"[Living] Сценарист не удался: {e}")

        return signals

    def _tick_chat(self, chat_id: str, pc: dict, fetch_stimulus: bool) -> Optional[tuple]:
        """Тик одного чата. Возвращает (chat_id, score, reason), если скоринг
        инициативы превысил порог — сигнал обработает _loop() на event loop."""
        # Предметы инвентаря: primitive передаёт их как «окружение» в тик
        # состояния (§3.4: pastime в терминах действий с предметами), для
        # остальных — источник объектов офлайн-событий
        inventory_items = []
        if self.inventory_manager is not None:
            try:
                inventory_items = [i.name for i in self.inventory_manager.get_items()]
            except Exception:
                inventory_items = []

        # Известные места мира — мягкая валидация location в тике (§4.1):
        # модель не должна телепортировать персону в выдуманные места
        known_places = None
        if self.config.world_enabled and not self.primitive:
            try:
                known_places = [p["name"] for p in
                                self.world_engine.get_world_snapshot()["places"]]
            except Exception:
                known_places = None

        # 1. Тик состояния (§3.3). Если подписан proactive — скоринг
        # инициативы (§3.4) идёт тем же Gemma-вызовом (один вызов вместо двух).
        # Дешёвые гейты (окно часов, дневной лимит, молчание) режут скоринг
        # ДО вызова модели — ночью вне окна score никому не нужен
        if self.primitive:
            surroundings = inventory_items[:8] or [
                self.world_engine.last_world_fact(chat_id)]
            storylines_ctx = surroundings
        else:
            storylines_ctx = (self.world_engine.active_storylines(
                self.config.max_active_storylines)
                if self.config.world_enabled else [])
        last_fact = (self.world_engine.last_world_fact(chat_id)
                     if self.config.world_enabled else "")

        want_score = self.on_initiative_signal is not None
        if want_score and self.pre_initiative_gate is not None:
            try:
                want_score = bool(self.pre_initiative_gate(chat_id))
            except Exception:
                want_score = True
        score = None
        if want_score:
            silence_h, since_init_h = 0.0, 24.0
            if self.get_last_message_time:
                last = self.get_last_message_time(chat_id)
                if last:
                    silence_h = max(0.0, (time.time() - last) / 3600)
            if self.get_last_initiative_time:
                last_init = self.get_last_initiative_time(chat_id) or 0
                since_init_h = max(0.0, (time.time() - last_init) / 3600)
            # §3.4: скоринг учитывает существующие proactive-настройки персоны
            proactive_cfg: dict = {}
            try:
                raw_cfg = (((self.persona.persona_data or {}).get("features") or {})
                           .get("proactive") or {})
                if isinstance(raw_cfg, dict):
                    proactive_cfg = {k: raw_cfg[k] for k in
                                     ("initiative_probability", "max_daily_initiatives",
                                      "silence_threshold_minutes") if k in raw_cfg}
            except Exception:
                proactive_cfg = {}
            state, score = self.state_engine.tick_and_score(
                chat_id, pc, storylines_ctx, last_world_fact=last_fact,
                silence_hours=silence_h, since_initiative_hours=since_init_h,
                proactive_settings=proactive_cfg, known_places=known_places)
        else:
            state = self.state_engine.tick(
                chat_id, pc, storylines_ctx, last_world_fact=last_fact,
                known_places=known_places)
        self.metrics["ticks_total"] += 1

        # 2. Офлайн-событие мира по расписанию (§4.3); для primitive это
        # физическое действие, в т.ч. с инвентарём (§3.4). Посреди активного
        # диалога не генерируем: событие остаётся дью и сработает на следующем
        # тике, когда чат затихнет (иначе «случилось за последние часы»
        # падает в разгар непрерывной переписки — временной парадокс).
        # Просроченный план (фаза B) — тоже повод для события: это его исход.
        due_plan = None
        if self.config.world_enabled and not self.primitive:
            try:
                due_plan = self.world_engine.due_plan()
            except Exception:
                due_plan = None
        event_due = (self.config.world_enabled
                     and (self.world_engine.should_generate_event(chat_id)
                          or due_plan is not None))
        if event_due and self.get_last_message_time:
            try:
                last_msg = self.get_last_message_time(chat_id) or 0.0
            except Exception:
                last_msg = 0.0
            if last_msg and (time.time() - last_msg) < EVENT_DEFER_QUIET_MINUTES * 60:
                event_due = False
        if event_due:
            # §5: на генерацию события подтягивается 1 неиспользованный стимул
            stimulus_obj = self.world_engine.pop_unused_stimulus() \
                if self.external_stimuli_allowed() else None
            stimulus_text = None
            if stimulus_obj:
                stimulus_text = stimulus_obj.get("content")
                self.state_engine.log_event(
                    chat_id, "external_stimulus",
                    {"content": stimulus_text, "source": stimulus_obj.get("source")})

            event = self.world_engine.generate_offline_event(
                chat_id, pc, state, stimulus_text,
                inventory_items=inventory_items, resolve_plan=due_plan)
            self.world_engine.schedule_next_event(chat_id)
            if event:
                payload = self.world_engine.apply_event(chat_id, event)
                if due_plan is not None:
                    # Follow-through: план случился, исход — текст события
                    self.world_engine.resolve_plan(due_plan["id"], payload["event"])
                self._apply_inventory_action(payload.get("inventory_action"))
                self.state_engine.log_event(chat_id, "world_event", payload)
                self.metrics["events_generated"] += 1
                mi = event.get("mood_impact") or {}
                try:
                    delta = float(mi.get("valence_delta", 0))
                except (TypeError, ValueError):
                    delta = 0.0
                if delta:
                    self.state_engine.apply_mood_impact(
                        chat_id, delta, str(mi.get("tag", ""))[:80])

        # 3. Новый fetch стимула, если пул пуст (только real_world — gate выше)
        if fetch_stimulus and not self.world_engine.has_unused_stimulus():
            self.world_engine.fetch_external_stimulus(pc)

        # 4. Скоринг инициативы уже посчитан в шаге 1 — порог → сигнал.
        # Последний скор держим в метриках: наблюдаемость порога (§9) —
        # раньше он нигде не был виден, включая /state
        if score is not None:
            try:
                self.metrics["last_initiative_score"] = round(float(score), 2)
            except (TypeError, ValueError):
                pass
            if score >= INITIATIVE_THRESHOLD:
                self.metrics["initiative_signals"] += 1
                return (chat_id, score, "state_engine")
        return None

"""
Уровни интеллекта персон (intellect tiers) — отдельное измерение поверх
характера (system_prompt) и фич (features.*).

Три уровня (§1 плана):
  primitive — существо с нечеловеческим типом мышления (животное, дух,
              примитивный робот): простая речь, помощь = действие, дневник
              из инстинктивных впечатлений, мир без NPC/арок
  normal    — человек: помощь по-бытовому коротко, без ассистентских
              уточнений; полноценная внутренняя жизнь
  bot       — высокий интеллект: право на полный разбор (расчёты, код,
              уточнения), подача всё равно в стиле персоны

Ключевой принцип (§4.3): tier-модификатор ОГРАНИЧИВАЕТ, system_prompt
наполняет характером внутри ограничения. `normal` — не «глупее» `bot`,
это другой режим поведения при просьбах о помощи.

YAML (§2):
  intellect:
    tier: normal            # primitive | normal | bot
    overrides:              # для нетипичных персон своего tier
      self_memory_mode: null    # none | primitive | full
      world_lore_enabled: null
      help_response_style: null # action_only | casual_human | full_assistant

Персоны БЕЗ блока intellect работают в legacy-режиме: уровневые механики
не активируются вообще (ручная разметка tier — рекомендация §7 плана:
ошибка автодетекта испортила бы стиль помощи, поэтому только явный выбор).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

TIERS = ("primitive", "normal", "bot")
SELF_MEMORY_MODES = ("none", "primitive", "full")
HELP_STYLES = ("action_only", "casual_human", "full_assistant")

# Дефолт-таблица (§2): что включается по tier, если overrides.* не задан
_TIER_DEFAULTS = {
    "primitive": {
        "self_memory_mode": "primitive",
        "help_response_style": "action_only",
        "world_lore_full": False,
    },
    "normal": {
        "self_memory_mode": "full",
        "help_response_style": "casual_human",
        "world_lore_full": True,
    },
    "bot": {
        "self_memory_mode": "full",
        "help_response_style": "full_assistant",
        "world_lore_full": True,
    },
}


class IntellectConfig:
    """Разобранный блок intellect персоны + производные решения.

    Атрибуты:
      tier          — 'primitive' | 'normal' | 'bot' | None (legacy)
      active        — True, если tier задан явно (уровневые механики включены)
      self_memory_mode — резолюция features.self_memory × tier × override
      help_response_style — стиль помощи (None в legacy)
    """

    def __init__(self, persona_data: Optional[dict]):
        persona_data = persona_data or {}
        features = persona_data.get("features") or {}
        raw = persona_data.get("intellect") or {}
        if not isinstance(raw, dict):
            raw = {}

        tier = raw.get("tier")
        if tier not in TIERS:
            if tier is not None:
                logger.warning(f"[Intellect] Неизвестный tier {tier!r} — игнорирую (допустимо: {TIERS})")
            tier = None
        self.tier: Optional[str] = tier
        self.active: bool = tier is not None

        overrides = raw.get("overrides") or {}
        if not isinstance(overrides, dict):
            overrides = {}

        # ── self_memory: features-флаг × tier-дефолт × override (§3.1) ──
        if not features.get("self_memory", False):
            mode = "none"  # фича выключена — модуля нет при любом tier
        else:
            mode = overrides.get("self_memory_mode")
            if mode not in SELF_MEMORY_MODES:
                if mode is not None:
                    logger.warning(f"[Intellect] Неизвестный self_memory_mode {mode!r} — беру дефолт tier")
                mode = _TIER_DEFAULTS[tier or "normal"]["self_memory_mode"]
        self.self_memory_mode: str = mode

        # ── help_response_style: дефолт по tier, override сверху (§4) ──
        style = overrides.get("help_response_style")
        if style not in HELP_STYLES:
            if style is not None:
                logger.warning(f"[Intellect] Неизвестный help_response_style {style!r} — беру дефолт tier")
            style = _TIER_DEFAULTS[tier]["help_response_style"] if tier else None
        self.help_response_style: Optional[str] = style

        # ── world_lore: override булев; дефолт по tier (§3.3) ──
        wl = overrides.get("world_lore_enabled")
        self.world_lore_override: Optional[bool] = None if wl is None else bool(wl)

        if self.active:
            logger.info(
                f"[Intellect] tier={tier} | self_memory={self.self_memory_mode} | "
                f"help_style={self.help_response_style}")

    # ── Удобные предикаты ────────────────────────────────

    @property
    def is_primitive(self) -> bool:
        return self.tier == "primitive"

    @property
    def is_bot(self) -> bool:
        return self.tier == "bot"

    def world_lore_full(self, features_enabled: bool) -> bool:
        """Полный ли слой мира (NPC/места/storylines)?

        §3.3: для primitive полный слой не положен никогда — даже если
        features.world_lore включён (pipeline-проверка, не только конфиг).
        Выключенный features.world_lore выключает всё, включая частичный
        режим (только state_engine)."""
        if not features_enabled:
            return False
        if self.world_lore_override is not None:
            return self.world_lore_override and not self.is_primitive
        if not self.active:
            return True  # legacy: как сконфигурировано фичей
        return _TIER_DEFAULTS[self.tier]["world_lore_full"]

    def world_lore_partial(self, features_enabled: bool) -> bool:
        """Частичный слой для primitive (§3.3): state_engine + офлайн-события
        (действия с инвентарём), без NPC/мест/storylines.
        Явный override world_lore_enabled: false выключает и его."""
        if not features_enabled or not self.is_primitive:
            return False
        if self.world_lore_override is not None:
            return self.world_lore_override
        return True

"""Управление провайдерами LLM и конфигами персон через API.

Ключи и активный провайдер персистятся в .env (рядом строки KEY=VALUE,
без перезаписи чужих переменных) и сразу применяются к живому процессу:
PROVIDER_CONFIGS перечитывается, роутеры уже созданных ботов обновляются.

Конфиг персоны пишется в её YAML (app/personas/{name}.yaml): settings,
stm_size, proactive, computer_control и менеджерные фичи (reminder/todo/
inventory) применяются к живому BotInstance сразу — менеджеры создаются
и запускаются на лету (sync_feature_managers), рестарт не нужен. Остальные
флаги features — после перезапуска.
"""

import json
import logging
import os
import re
from pathlib import Path

import yaml

from app.core.config import (
    OLLAMA_MODEL,
    PROVIDER_CONFIGS,
    _collect_api_keys,
    get_available_providers,
)

logger = logging.getLogger(__name__)

# Флаги features, применяемые к живому боту без рестарта сервера
_LIVE_FEATURE_KEYS = {
    "proactive", "muted", "light_context", "computer_control",
    "reminder", "todo", "inventory", "rhythm", "life",
}

_ENV_PATH = Path(__file__).parent.parent.parent / ".env"


def _persist_env(var: str, value: str):
    """Записать переменную в .env: заменить существующую строку или дописать."""
    lines = []
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
    prefix = f"{var}="
    for i, line in enumerate(lines):
        if line.startswith(prefix) or line.startswith(f"{var} ="):
            lines[i] = f"{var}={value}"
            break
    else:
        lines.append(f"{var}={value}")
    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[var] = value


def _refresh_live_routers():
    """Перечитать провайдеров в роутерах уже созданных ботов."""
    from app.api.runtime import registry
    available = get_available_providers()
    for bot in registry._bots.values():
        bot.router.available = available


def _mask_key(key: str) -> str:
    """Маска для UI: первые и последние 4 символа, середина скрыта."""
    if len(key) <= 8:
        return key[:2] + "…"
    return f"{key[:4]}…{key[-4:]}"


def list_providers() -> dict:
    """Список провайдеров для UI: статус ключей, модель, активность."""
    active = os.getenv("ACTIVE_PROVIDER")
    available = get_available_providers()
    if active not in available:
        active = next(iter(available), None)  # первый с ключом — как в ModelRouter

    providers = [
        {
            "id": pid,
            "name": pid.upper(),
            "key_set": bool(cfg["api_keys"]),
            "keys_count": len(cfg["api_keys"]),
            "keys": [_mask_key(k) for k in cfg["api_keys"]],
            "model": cfg.get("model", ""),
            "active": pid == active,
            "local": False,
        }
        for pid, cfg in PROVIDER_CONFIGS.items()
    ]

    # Локальная модель (Ollama) — без ключа
    local_available = False
    local_model = OLLAMA_MODEL
    try:
        from app.core.local_router import get_local_router
        local_available = get_local_router().is_available()
    except Exception:
        pass
    providers.append({
        "id": "local",
        "name": "Ollama",
        "key_set": local_available,
        "keys_count": 0,
        "keys": [],
        "model": local_model,  # то же имя, что читает LocalLLMRouter
        "active": active == "local",
        "local": True,
    })
    # Веб-чаты как провайдеры без ключей: включённые сайты (env, порядок =
    # порядок перебора) и доступные адаптеры
    from app.features.web_llm import ADAPTERS as _WC_ADAPTERS
    from app.core.router import _parse_webchat_sites
    webchat_sites = _parse_webchat_sites()
    # Движки локальных задач: что из службыбных вызовов идёт в Ollama,
    # а что — в веб-чат (и на какой сайт)
    from app.core.local_router import get_local_router
    return {"providers": providers, "active": active,
            "webchat_site": webchat_sites[0] if webchat_sites else None,
            "webchat_sites": webchat_sites,
            "webchat_options": sorted(_WC_ADAPTERS),
            "local_tasks": get_local_router().task_snapshot()}


def set_local_task(task: str, backend: str, site: str | None = None) -> dict:
    """Выбрать движок локальной задачи (всё, что поручено Ollama): «ollama» —
    локальная Ollama, «webchat» — веб-чат пользователя; site — конкретный
    сайт веб-чата для этой задачи (пусто — первый включённый). Канал side:
    отдельный чат и квота. Применяется к живому синглтону сразу, персист в
    data/local_backends.json, рестарт не нужен."""
    from app.core.local_router import get_local_router
    ok, detail = get_local_router().set_task_backend(task, backend, site)
    if not ok:
        return {"ok": False, "detail": detail}
    return {"ok": True, "task": task, "backend": backend}


def set_webchat(sites) -> dict:
    """Выбор веб-чатов как провайдеров: список сайтов из ADAPTERS в порядке
    перебора (пусто/off — выкл; одиночная строка — один сайт, совместимость).
    Персист в .env (WEBCHAT_SITES + legacy WEBCHAT_SITE=первый сайт) и
    применение к живым роутерам без рестарта. Персональная секция llm
    в YAML персоны имеет приоритет над глобальным."""
    from app.features.web_llm import ADAPTERS
    if isinstance(sites, str):
        sites = [sites] if sites.strip() else []
    norm: list = []
    for s in sites or []:
        site = str(s).strip().lower()
        if site in ("", "off", "none"):
            continue
        if site not in ADAPTERS:
            return {"ok": False,
                    "detail": f"неизвестный веб-чат «{site}» "
                              f"(есть: {', '.join(sorted(ADAPTERS))})"}
        if site not in norm:
            norm.append(site)
    _persist_env("WEBCHAT_SITES", ",".join(norm))
    _persist_env("WEBCHAT_SITE", norm[0] if norm else "")
    from app.api.runtime import registry
    for bot in registry._bots.values():
        bot.router.webchat_sites = list(norm)
        bot.router._webchats = {}  # экземпляры пересоздадутся на следующем вызове
    # Задачи локального движка не могут остаться на выключенном веб-чате —
    # возвращаем их на Ollama (иначе вызовы молча ходили бы в никуда)
    if not norm:
        from app.core.local_router import get_local_router
        get_local_router().reset_webchat_tasks()
        if (os.getenv("LOCAL_LLM_BACKEND") or "").lower() == "webchat":
            _persist_env("LOCAL_LLM_BACKEND", "ollama")
    logger.info(f"[Settings] webchat-провайдеры: {','.join(norm) or 'выключены'}")
    return {"ok": True, "webchat_site": norm[0] if norm else None,
            "webchat_sites": norm}


def local_status() -> dict:
    """Свежая проверка локальной Ollama (кнопка «Проверить доступность»).

    В отличие от list_providers (там кеш до 30 сек из is_available) ходит в
    /api/tags напрямую и различает «сервер не отвечает» и «модель не
    установлена». Результат сразу пишется в кеш живого LocalLLMRouter —
    локальные фичи включаются/выключаются без рестарта.
    """
    import time as _time

    import httpx

    from app.core.local_router import get_local_router

    r = get_local_router()
    server = False
    models: list = []
    try:
        resp = httpx.get(f"{r.base_url}/api/tags", timeout=5.0)
        if resp.status_code == 200:
            server = True
            models = [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception:
        pass
    # Ollama хранит имя с тегом: llama3 -> llama3:latest
    present = server and (r.model in models or f"{r.model}:latest" in models)
    available = server and present
    r._available = available
    r._last_check = _time.time()
    return {
        "server": server,
        "url": r.base_url,
        "model": r.model,
        "model_present": present,
        "models": models,
        "available": available,
    }


def set_provider_model(provider: str, model: str) -> dict:
    """Сменить модель провайдера: .env ({PREFIX}_MODEL / OLLAMA_MODEL) + рантайм.

    Дефолт в config.py остаётся запасным — пустую строку не принимаем."""
    model = model.strip()
    if not model:
        return {"ok": False, "detail": "Пустое имя модели"}

    if provider == "local":
        _persist_env("OLLAMA_MODEL", model)
        # Живой singleton LocalLLMRouter (мог быть ещё не создан — тогда
        # прочитает env при первом обращении)
        try:
            from app.core.local_router import get_local_router, _local_router
            if _local_router is not None:
                _local_router.model = model
        except Exception:
            pass
        return {"ok": True, "provider": provider, "model": model}

    if provider not in PROVIDER_CONFIGS:
        return {"ok": False, "detail": f"Неизвестный провайдер: {provider}"}
    _persist_env(f"{provider.upper()}_MODEL", model)
    PROVIDER_CONFIGS[provider]["model"] = model
    _refresh_live_routers()
    # Вердикты автопробы vision относились к старой модели — сбрасываем
    from app.api.runtime import registry
    for bot in registry._bots.values():
        getattr(bot.router, "_vision_verdict", {}).pop(provider, None)
    return {"ok": True, "provider": provider, "model": model}


def add_provider_key(provider: str, key: str) -> dict:
    """Добавить ключ провайдеру: в .env (первый свободный слот), в рантайм, в живых ботов."""
    if provider not in PROVIDER_CONFIGS:
        return {"ok": False, "detail": f"Неизвестный провайдер: {provider}"}
    key = key.strip()
    if not key:
        return {"ok": False, "detail": "Пустой ключ"}

    prefix = provider.upper()
    existing = _collect_api_keys(prefix)
    if key in existing:
        return {"ok": False, "detail": "Такой ключ уже добавлен"}

    if not existing:
        var = f"{prefix}_API_KEY"
    else:
        i = 1
        while os.getenv(f"{prefix}_API_KEY_{i}"):
            i += 1
        var = f"{prefix}_API_KEY_{i}"

    _persist_env(var, key)
    PROVIDER_CONFIGS[provider]["api_keys"] = _collect_api_keys(prefix)
    _refresh_live_routers()
    return {"ok": True, "keys_count": len(PROVIDER_CONFIGS[provider]["api_keys"])}


def _key_vars(provider: str) -> list[str]:
    """Переменные окружения с ключами провайдера в том же порядке,
    что их собирает _collect_api_keys (основная, затем _1, _2, ...)."""
    prefix = provider.upper()
    variables = []
    seen = set()
    main = os.getenv(f"{prefix}_API_KEY")
    if main:
        variables.append(f"{prefix}_API_KEY")
        seen.add(main)
    i = 1
    empty_count = 0
    while empty_count < 5:  # как в _collect_api_keys: до 5 пропусков
        var = f"{prefix}_API_KEY_{i}"
        value = os.getenv(var)
        if value:
            if value not in seen:
                variables.append(var)
                seen.add(value)
            empty_count = 0
        else:
            empty_count += 1
        i += 1
    return variables


def _remove_env(var: str):
    """Удалить переменную из .env и из окружения процесса."""
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
        lines = [
            line for line in lines
            if not (line.startswith(f"{var}=") or line.startswith(f"{var} ="))
        ]
        _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.pop(var, None)


def delete_provider_key(provider: str, index: int) -> dict:
    """Удалить ключ провайдера по индексу (порядок — как в list_providers)."""
    if provider not in PROVIDER_CONFIGS:
        return {"ok": False, "detail": f"Неизвестный провайдер: {provider}"}
    variables = _key_vars(provider)
    if index < 0 or index >= len(variables):
        return {"ok": False, "detail": f"Нет ключа с индексом {index}"}
    _remove_env(variables[index])
    PROVIDER_CONFIGS[provider]["api_keys"] = _collect_api_keys(provider.upper())
    _refresh_live_routers()
    return {"ok": True, "keys_count": len(PROVIDER_CONFIGS[provider]["api_keys"])}


def set_active_provider(provider: str) -> dict:
    """Сменить активного провайдера: .env + живые роутеры.

    Роутеры с персональным основным провайдером (секция llm в YAML персоны)
    глобальная смена не трогает — у них свой закреплённый primary."""
    if provider != "local" and provider not in PROVIDER_CONFIGS:
        return {"ok": False, "detail": f"Неизвестный провайдер: {provider}"}
    _persist_env("ACTIVE_PROVIDER", provider)
    from app.api.runtime import registry
    for bot in registry._bots.values():
        if getattr(bot.router, "pinned_provider", None):
            continue
        bot.router.active_provider = provider
        bot.router.available = get_available_providers()
    return {"ok": True, "active": provider}


def _apply_llm_to_bot(persona: str, llm_cfg: dict):
    """Применить секцию llm к живому роутеру бота (без перезапуска)."""
    from app.api.runtime import registry
    bot = registry._bots.get(persona)
    if bot is not None:
        bot.router.set_persona_llm(llm_cfg.get("primary"), llm_cfg.get("fallback"),
                                   llm_cfg.get("models"),
                                   webchat_limits=llm_cfg.get("webchat_limits"))


def _apply_computer_control_live(bot, cc_cfg):
    """computer_control на живую: allowlist'ы перечитываются менеджером,
    при включении фичи менеджер создаётся, при выключении — снимается.
    Выключение — false/пусто или dict с enabled: false (списки сохраняются).
    Pending-подтверждения и статистика переживают обновление."""
    from app.features.computer_control import ComputerControlManager, config_enabled
    if config_enabled(cc_cfg):
        if getattr(bot, "computer_control", None) is not None:
            bot.computer_control.update_config(cc_cfg if isinstance(cc_cfg, dict) else {})
        else:
            bot.computer_control = ComputerControlManager(
                context=bot.context, config=cc_cfg if isinstance(cc_cfg, dict) else {})
        logger.info(f"[{bot.persona_name}] Computer control обновлён на живую")
    else:
        bot.computer_control = None


# Редактируемые через UI параметры проактивности → (тип, min, max)
_PROACTIVE_FIELDS = {
    "enabled": (bool, None, None),
    "silence_threshold_minutes": (int, 1, 1440),  # порог молчания: не больше суток
    "check_interval_minutes": (int, 1, 1440),
    "initiative_probability": (float, 0.0, 1.0),
    "max_daily_initiatives": (int, 1, 100),
    "adaptive_threshold": (bool, None, None),
    "feedback_enabled": (bool, None, None),
    # initiative_hours — окно времени самоинициативы: обрабатывается отдельно
    # (строка "HH:MM-HH:MM" / dict / пусто = круглые сутки)
}


def _clean_initiative_hours(value) -> str | None:
    """Нормализация окна самоинициативы для YAML: "HH:MM-HH:MM" или None
    (круглые сутки / снять). Битое значение — ошибка."""
    if value is None or value == "" or value is False:
        return None
    from app.features.proactive_messaging import ProactiveConfig
    parsed = ProactiveConfig.parse_hours(value)
    if parsed is None:
        raise ValueError(f"Некорректное окно времени: {value!r} (формат HH:MM-HH:MM)")
    return f"{parsed[0]}-{parsed[1]}"


def get_persona_proactive(persona: str) -> dict | None:
    """Параметры features.proactive из YAML персоны (для GET /initiative,
    когда живого менеджера нет — проактивность выключена). None — персоны нет."""
    path = _PERSONAS_DIR / f"{persona}.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not data.get("system_prompt"):
        return None
    proactive = (data.get("features") or {}).get("proactive")
    return proactive if isinstance(proactive, dict) else {}


def _apply_proactive_live(persona: str, bot, proactive_cfg) -> None:
    """Применить features.proactive к живому боту без рестарта (API-режим).

    Менеджер ещё не создан и enabled=true — полная активация (трекер,
    ProactiveMessaging, фоновый цикл). Менеджер есть — синхронизируем поля
    конфига и стартуем/останавливаем цикл по enabled.
    """
    if bot is None:
        return
    enabled = bool(proactive_cfg.get("enabled", False)) if isinstance(proactive_cfg, dict) else bool(proactive_cfg)

    if bot.proactive is None:
        if not enabled or not bot.web_single_user:
            return  # вне API-режима живую активацию не делаем (там sender Telegram)
        from app.features.proactive_messaging import ChatActivityTracker
        from app.api.inbox import WebInboxSender, background_loop
        bot._activity_tracker = ChatActivityTracker(context=bot.context)
        bot.setup_proactive(WebInboxSender(persona))
        if bot.proactive is not None:
            loop = background_loop()
            loop.call_soon_threadsafe(bot.proactive.start, loop)
            logger.info(f"[{persona}] Проактивность активирована на живую")
        return

    # Менеджер существует: переносим редактируемые поля из YAML в живой конфиг
    if isinstance(proactive_cfg, dict):
        for key in _PROACTIVE_FIELDS:
            if key in proactive_cfg:
                setattr(bot.proactive.config, key, proactive_cfg[key])
        # Окно самоинициативы — не скаляр: парсится в пару "HH:MM"
        if "initiative_hours" in proactive_cfg:
            from app.features.proactive_messaging import ProactiveConfig
            bot.proactive.config.initiative_hours = ProactiveConfig.parse_hours(
                proactive_cfg.get("initiative_hours"))
    if enabled and not bot.proactive._running:
        from app.api.inbox import background_loop
        loop = background_loop()
        loop.call_soon_threadsafe(bot.proactive.start, loop)
        logger.info(f"[{persona}] Проактивность запущена на живую")
    elif not enabled and bot.proactive._running:
        bot.proactive.stop()
        logger.info(f"[{persona}] Проактивность остановлена на живую")


def _apply_rhythm_live(persona: str, bot, rhythm_cfg) -> None:
    """Применить features.rhythm (утро/ночь/погода) к живому боту без рестарта
    (API-режим). Менеджер ещё не создан и enabled=true — полная активация
    (setup_rhythm + веб-inbox + фоновый цикл). Менеджер есть — синхронизируем
    конфиг и стартуем/останавливаем цикл по enabled."""
    if bot is None:
        return
    enabled = bool(rhythm_cfg.get("enabled", False)) if isinstance(rhythm_cfg, dict) else bool(rhythm_cfg)

    if getattr(bot, "rhythm", None) is None:
        if not enabled or not bot.web_single_user:
            return  # вне API-режима живую активацию не делаем (там sender Telegram)
        from app.api.inbox import wire_rhythm_for_api
        wire_rhythm_for_api(persona, bot)
        if getattr(bot, "rhythm", None) is not None:
            logger.info(f"[{persona}] Rhythm активирован на живую")
        return

    rm = bot.rhythm
    rm.update_config(rhythm_cfg if isinstance(rhythm_cfg, dict) else {"enabled": enabled})
    if enabled and not rm._running:
        from app.api.inbox import background_loop
        loop = background_loop()
        loop.call_soon_threadsafe(rm.start, loop)
        logger.info(f"[{persona}] Rhythm запущен на живую")
    elif not enabled and rm._running:
        rm.stop()
        logger.info(f"[{persona}] Rhythm остановлен на живую")


def _apply_life_live(persona: str, bot, life_cfg) -> None:
    """Фича «жизнь персоны» на живого бота (API-режим, как proactive/rhythm).

    Решение — по RESOLVED-конфигу всего features (фича life ИЛИ явные блоки
    state_engine/world_lore): включение создаёт LivingPersona и стартует
    цикл на общем api-bg loop, выключение — останавливает и снимает
    (переключение обратно пересоздаст с актуальными настройками)."""
    if bot is None:
        return
    from app.core.living_persona import LivingPersona, LivingPersonaConfig
    config = LivingPersonaConfig(bot.features or {})

    if getattr(bot, "living", None) is None:
        if not config.enabled or not getattr(bot, "web_single_user", False):
            return  # выключено или вне API-режима (там living стартует main.py)
        try:
            bot.living = LivingPersona(
                context=bot.context, persona=bot.persona, router=bot.router,
                config=config, self_memory=bot.self_memory,
                intellect=bot.intellect,
                inventory_manager=bot.inventory_manager)
        except Exception as e:
            logger.warning(f"[{persona}] Living persona не создана: {e}")
            return
        # Связка с proactive — как в setup_proactive (сигнал инициативы
        # и источники чатов), чтобы жизнь умела писать первой
        if bot.proactive is not None:
            bot.living.on_initiative_signal = bot.proactive.state_initiative_signal
            if getattr(bot, "_activity_tracker", None) is not None:
                bot.living.get_known_chats = bot._activity_tracker.get_known_chats
            bot.living.get_last_message_time = bot._get_last_message_time
            bot.living.get_last_initiative_time = (
                lambda chat_id: bot.proactive._last_initiative_time.get(str(chat_id), 0))
            # обратная ссылка: STATE_CHANGE-инициативы и mood-синк ignore streak
            bot.proactive.living = bot.living
            # дешёвые гейты перед LLM-скорингом инициативы (§3.4)
            bot.living.pre_initiative_gate = bot.proactive.initiative_cheaply_possible
        logger.info(f"[{persona}] Жизнь персоны активирована на живую")

    if config.enabled and not bot.living._running:
        from app.api.inbox import background_loop
        loop = background_loop()
        loop.call_soon_threadsafe(
            bot.living.start, loop,
            bot.living.get_known_chats, bot.living.get_last_message_time)
        logger.info(f"[{persona}] Living persona запущена на живую")
    elif not config.enabled and bot.living._running:
        bot.living.stop()
        bot.living = None  # повторное включение пересоздаст с актуальным конфигом
        if bot.proactive is not None:
            bot.proactive.living = None
        logger.info(f"[{persona}] Living persona остановлена на живую")


def _ruin_persona_mood(persona: str) -> None:
    """Заморозка рушит настроение персоны: ignore-streak в максимум («глубокая
    обида») для всех известных чатов + веб-чата.

    Живой proactive-менеджер правим через него (он же персистит); если менеджера
    нет (проактивность выключена) — правим файл напрямую, подхватится при старте.
    """
    from app.api.runtime import registry
    bot = registry._bots.get(persona)
    proactive = getattr(bot, "proactive", None) if bot is not None else None
    if proactive is not None:
        for chat_id in set(proactive._ignore_streak) | {"web_user"}:
            proactive.ruin_mood(chat_id)
        return
    path = Path(f"data/api_{persona}/ignore_streak.json")
    streak = {}
    if path.is_file():
        try:
            streak = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            streak = {}
    for chat_id in set(streak) | {"web_user"}:
        streak[chat_id] = 10  # верхний порог обиды в _get_emotional_state
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(streak, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"[{persona}] Настроение испорчено заморозкой (файл, proactive не активен)")
    except Exception as e:
        logger.warning(f"[{persona}] Не удалось записать ignore_streak: {e}")


def update_persona_proactive(persona: str, patch: dict) -> dict | None:
    """Обновить параметры проактивности (features.proactive в YAML персоны).

    Пишет только известные ключи с клампом значений. Работает и при выключенной
    проактивности (менеджер не создан): параметры сохраняются в YAML, а
    enabled=true активирует цикл на живую. None — персоны нет;
    {"ok": False, detail} — некорректный патч.
    """
    path = _PERSONAS_DIR / f"{persona}.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not data.get("system_prompt"):
        return None

    features = data.get("features") or {}
    proactive = features.get("proactive")
    if not isinstance(proactive, dict):
        proactive = {}

    cleaned = {}
    for key, value in patch.items():
        if key == "initiative_hours":
            # Окно времени самоинициативы: "HH:MM-HH:MM" / dict / пусто (снять)
            try:
                cleaned[key] = _clean_initiative_hours(value)
            except ValueError as e:
                return {"ok": False, "detail": str(e)}
            continue
        spec = _PROACTIVE_FIELDS.get(key)
        if spec is None:
            continue
        typ, lo, hi = spec
        try:
            if typ is bool:
                cleaned[key] = bool(value)
            else:
                v = typ(value)
                cleaned[key] = max(lo, min(hi, v)) if lo is not None else v
        except (TypeError, ValueError):
            return {"ok": False, "detail": f"Некорректное значение {key}: {value!r}"}
    if not cleaned:
        return {"ok": False, "detail": "Пустой патч"}

    proactive.update(cleaned)
    features["proactive"] = proactive
    data["features"] = features
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )

    # Живой бот: конфиг читается циклом на каждой итерации — применяется сразу;
    # включение активирует цикл без рестарта, выключение — останавливает
    from app.api.runtime import registry
    bot = registry._bots.get(persona)
    if bot is not None:
        bot.persona.persona_data = data
        bot.features = features
        _apply_proactive_live(persona, bot, proactive)

    return {"ok": True, "updated": cleaned}


# ── Конфиг персоны (YAML) ─────────────────────────────────────────────

_PERSONAS_DIR = Path(__file__).parent.parent / "personas"


def get_persona_config(persona: str) -> dict | None:
    path = _PERSONAS_DIR / f"{persona}.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not data.get("system_prompt"):
        return None
    llm = data.get("llm") or {}
    return {
        "settings": data.get("settings") or {},
        "stm_size": data.get("stm_size"),
        "features": data.get("features") or {},
        "llm": {
            "primary": llm.get("primary"),  # None — используется глобальный активный
            "fallback": llm.get("fallback") or [],
            "models": llm.get("models") or {},  # свои модели по провайдерам
            # лимиты веб-чатов: {сайт: {enabled, per_hour}}; нет сайта — дефолт 40/ч
            "webchat_limits": llm.get("webchat_limits") or {},
        },
    }


def _apply_feature_managers_live(persona: str, bot) -> None:
    """reminder/todo/inventory на живом боте: создаёт/останавливает менеджеры
    сразу, без рестарта. Свежесозданный reminder-менеджер подключается к
    веб-inbox, и его фоновый цикл стартует на общем api-bg loop."""
    had_reminder = bot.reminder_manager is not None
    bot.sync_feature_managers()
    if bot.reminder_manager is not None and not had_reminder:
        from app.api.inbox import wire_reminder_for_api
        wire_reminder_for_api(persona, bot)


def update_persona_config(persona: str, settings: dict | None,
                          stm_size: int | None, features: dict | None,
                          llm: dict | None = None) -> dict | None:
    """Обновить settings/stm_size/features в YAML персоны.

    Комментарии в YAML при записи теряются (safe_dump) — данные сохраняются.
    Возвращает {"restart_required": bool} или None, если персоны нет.
    """
    path = _PERSONAS_DIR / f"{persona}.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not data.get("system_prompt"):
        return None

    if settings:
        merged = data.get("settings") or {}
        merged.update(settings)
        data["settings"] = merged
    if stm_size is not None:
        data["stm_size"] = stm_size
    restart_required = False
    mute_ruins_mood = False
    if features:
        merged_f = data.get("features") or {}
        for k, v in features.items():
            # reminder/todo/inventory (как и proactive/muted/light_context/
            # computer_control) применяются на живую — рестарт не нужен
            if (k not in _LIVE_FEATURE_KEYS
                    and merged_f.get(k) != v):
                restart_required = True
            if k == "muted" and v is True and merged_f.get(k) is not True:
                mute_ruins_mood = True  # свежая заморозка — рушим настроение ниже
            merged_f[k] = v
        data["features"] = merged_f
    if llm is not None:
        # Секция llm: primary=None → снять закрепление (глобальный активный),
        # пустой fallback → убрать персональный приоритет
        merged_l = data.get("llm") or {}
        if "primary" in llm:
            if llm["primary"]:
                merged_l["primary"] = llm["primary"]
            else:
                merged_l.pop("primary", None)
        if llm.get("fallback") is not None:
            if llm["fallback"]:
                merged_l["fallback"] = llm["fallback"]
            else:
                merged_l.pop("fallback", None)
        if llm.get("models") is not None:
            # Персональные модели: {provider: model}; пустое значение снимает override
            merged_m = merged_l.get("models") or {}
            for k, v in llm["models"].items():
                if k not in PROVIDER_CONFIGS:
                    continue
                if v and str(v).strip():
                    merged_m[k] = str(v).strip()
                else:
                    merged_m.pop(k, None)
            if merged_m:
                merged_l["models"] = merged_m
            else:
                merged_l.pop("models", None)
        if llm.get("webchat_limits") is not None:
            # Лимиты веб-чатов: {сайт: {enabled, per_hour}}. enabled:false —
            # лимит снят; per_hour 1..500; мусорная запись сбрасывает к дефолту
            from app.features.web_llm import ADAPTERS as _WC_ADAPTERS
            merged_w = merged_l.get("webchat_limits") or {}
            for site, cfg in llm["webchat_limits"].items():
                if site not in _WC_ADAPTERS or not isinstance(cfg, dict):
                    continue
                if not cfg.get("enabled", True):
                    merged_w[site] = {"enabled": False}
                    continue
                try:
                    ph = int(cfg.get("per_hour") or 0)
                except (TypeError, ValueError):
                    ph = 0
                if 0 < ph <= 500:
                    merged_w[site] = {"enabled": True, "per_hour": ph}
                else:
                    merged_w.pop(site, None)
            if merged_w:
                merged_l["webchat_limits"] = merged_w
            else:
                merged_l.pop("webchat_limits", None)
        if merged_l:
            data["llm"] = merged_l
        else:
            data.pop("llm", None)

    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )

    # Живой бот: генерация, stm_size, провайдеры и проактивность применяем
    # сразу, остальные features — после рестарта
    from app.api.runtime import registry
    bot = registry._bots.get(persona)
    if bot is not None:
        bot.persona.persona_data = data
        bot.persona.settings = data.get("settings") or {}
        if stm_size is not None:
            bot.stm_size = stm_size
        bot.features = data.get("features") or {}
        _apply_feature_managers_live(persona, bot)
        if features is not None and "proactive" in features:
            _apply_proactive_live(persona, bot, bot.features.get("proactive"))
        if features is not None and "rhythm" in features:
            _apply_rhythm_live(persona, bot, bot.features.get("rhythm"))
        if features is not None and "life" in features:
            _apply_life_live(persona, bot, bot.features.get("life"))
        if features is not None and "computer_control" in features:
            _apply_computer_control_live(bot, bot.features.get("computer_control"))
        if llm is not None:
            _apply_llm_to_bot(persona, data.get("llm") or {})

    # Свежая заморозка рушит настроение персоны (после применения к живому боту)
    if mute_ruins_mood:
        _ruin_persona_mood(persona)

    return {"restart_required": restart_required}


def save_persona_yaml(persona: str, raw: str) -> dict | None:
    """Записать сырой YAML персоны (редактор в вебе): валидация, файл, живой бот.

    None — персоны нет; {"ok": False, "detail"} — YAML невалиден;
    {"ok": True, "restart_required"} — записано.
    """
    path = _PERSONAS_DIR / f"{persona}.yaml"
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return {"ok": False, "detail": f"YAML не парсится: {e}"}
    # Без system_prompt файл перестанет быть персоной (list_personas его потеряет)
    if not isinstance(data, dict) or not data.get("system_prompt"):
        return {"ok": False, "detail": "YAML должен быть объектом с непустым system_prompt"}

    path.write_text(raw, encoding="utf-8")

    # Живой бот: применяем то же, что и update_persona_config
    from app.api.runtime import registry
    bot = registry._bots.get(persona)
    restart_required = False
    if bot is not None:
        old_f = bot.features or {}
        new_f = data.get("features") or {}
        changed_keys = {k for k in set(old_f) | set(new_f) if old_f.get(k) != new_f.get(k)}
        restart_required = bool(changed_keys - _LIVE_FEATURE_KEYS)
        bot.persona.persona_data = data
        bot.persona.settings = data.get("settings") or {}
        if data.get("stm_size") is not None:
            bot.stm_size = data["stm_size"]
        bot.features = new_f
        _apply_feature_managers_live(persona, bot)
        _apply_llm_to_bot(persona, data.get("llm") or {})
    return {"ok": True, "restart_required": restart_required}


# ── Создание / удаление / дублирование персон ──

# Допустимый id персоны = имя YAML-файла (фронт шлёт его же в API-вызовах,
# поэтому файл обязан совпадать с полем id)
_PERSONA_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def create_persona(raw: str) -> dict:
    """Создать новую персону из сырого YAML (модалка создания в вебе).

    Имя файла = поле id из YAML. Файл подхватывается реестром автоматически
    (list_personas читает диск), рестарт не нужен.
    {"ok": True, "persona": id} | {"ok": False, "detail", "conflict": bool}
    """
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return {"ok": False, "detail": f"YAML не парсится: {e}"}
    if not isinstance(data, dict) or not data.get("system_prompt"):
        return {"ok": False, "detail": "YAML должен быть объектом с непустым system_prompt"}
    persona_id = str(data.get("id") or "").strip()
    if not persona_id or not _PERSONA_ID_RE.match(persona_id):
        return {"ok": False, "detail": "Поле id обязательно: латиница, цифры, _ и - (до 64 символов)"}
    path = _PERSONAS_DIR / f"{persona_id}.yaml"
    if path.exists():
        return {"ok": False, "conflict": True, "detail": f"Персона '{persona_id}' уже существует"}
    path.write_text(raw, encoding="utf-8")
    logger.info(f"[api] Создана персона {persona_id}")
    return {"ok": True, "persona": persona_id}


def delete_persona(persona: str) -> bool:
    """Удалить YAML персоны и выгрузить бота из реестра (фоновые циклы стоп).

    Память персоны (data/api_{persona}/) намеренно остаётся на диске.
    """
    from app.api.runtime import list_personas, registry
    if persona not in list_personas():  # защита и от traversal, и от удаления служебных yaml
        return False
    registry.evict(persona)
    (_PERSONAS_DIR / f"{persona}.yaml").unlink()
    logger.info(f"[api] Удалена персона {persona}")
    return True


def duplicate_persona(persona: str) -> dict | None:
    """Копия YAML персоны с новым id/name. None — персоны нет.

    Правятся только верхнеуровневые id:/name: — остальной текст (включая
    комментарии) копируется как есть.
    """
    src = _PERSONAS_DIR / f"{persona}.yaml"
    if not src.is_file():
        return None
    raw = src.read_text(encoding="utf-8")

    n = 1
    while True:
        new_id = f"{persona}_copy" if n == 1 else f"{persona}_copy{n}"
        if not (_PERSONAS_DIR / f"{new_id}.yaml").exists():
            break
        n += 1

    data = yaml.safe_load(raw) or {}
    new_name = f"{data.get('name') or persona} (копия)"

    def set_field(text: str, key: str, line: str) -> str:
        pattern = rf"(?m)^{key}:.*$"
        if re.search(pattern, text):
            return re.sub(pattern, lambda _m: line, text, count=1)
        return line + "\n" + text

    out = set_field(raw, "id", f"id: {new_id}")
    escaped = new_name.replace("\\", "\\\\").replace('"', '\\"')
    out = set_field(out, "name", f'name: "{escaped}"')
    (_PERSONAS_DIR / f"{new_id}.yaml").write_text(out, encoding="utf-8")
    logger.info(f"[api] Персона {persona} продублирована в {new_id}")
    return {"ok": True, "persona": new_id}

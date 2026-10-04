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

from app.api.security import (
    PERSONA_ID_RE,
    atomic_write_text,
    persist_env as _security_persist_env,
    remove_env as _security_remove_env,
    safe_join,
    validate_env_value,
    yaml_write_lock,
)
from app.core.config import (
    OLLAMA_MODEL,
    PROVIDER_CONFIGS,
    _collect_api_keys,
    get_available_providers,
)
from app.core.addons import CORE_PERSONAS_DIR, persona_dirs
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

# Флаги features, применяемые к живому боту без рестарта сервера
_LIVE_FEATURE_KEYS = {
    "proactive", "muted", "light_context", "computer_control",
    "reminder", "todo", "inventory", "rhythm", "life",
}

# Ключи верхнего уровня YAML, которые BotInstance читает только при создании
# (правка через YAML-редактор действует после перезапуска)
_RESTART_TOP_KEYS = {"intellect", "conversation_style", "max_docs", "max_file_size_mb"}

# Доступ к режиму управления (computer_control с allowed_users, owner). На
# живую он применяется только к ботам API-процесса; Telegram-бот персоны —
# отдельный процесс, YAML читает при старте (на лету — лишь muted): там
# правка действует после его перезапуска
_TELEGRAM_RESTART_KEYS = {"computer_control", "owner"}


def _has_telegram_bot(persona: str) -> bool:
    """У персоны есть Telegram-бот: задан <ID>_BOT_TOKEN (как в app/main.py)."""
    return bool(os.getenv(f"{persona.upper()}_BOT_TOKEN"))


_ENV_PATH = Path(__file__).parent.parent.parent / ".env"


def _persist_env(var: str, value: str):
    """Записать переменную в .env: заменить существующую строку или дописать.

    Атомарно (tmp + os.replace, права файла сохраняются) и под общим локом —
    см. app/api/security.persist_env. Значение с \\r/\\n/NUL или var не из
    [A-Z0-9_] отклоняется ValueError-ом: иначе \\n в значении дописал бы в
    .env произвольную вторую строку, которая применилась бы при следующем
    рестарте."""
    _security_persist_env(_ENV_PATH, var, value)
    os.environ[var] = value


def _refresh_live_routers():
    # Перечитать провайдеров в роутерах уже созданных ботов.
    from app.api.runtime import registry
    available = get_available_providers()
    for bot in registry._bots.values():
        bot.router.available = available


def _mask_key(key: str) -> str:
    # Маска для UI: первые и последние 4 символа, середина скрыта.
    if len(key) <= 8:
        return key[:2] + "…"
    return f"{key[:4]}…{key[-4:]}"


def list_providers() -> dict:
    # Список провайдеров для UI: статус ключей, модель, активность.
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
    # Движки служебных задач (Ollama/веб-чат) — на персону, см.
    # get_persona_local_tasks
    return {"providers": providers, "active": active,
            "webchat_site": webchat_sites[0] if webchat_sites else None,
            "webchat_sites": webchat_sites,
            "webchat_options": sorted(_WC_ADAPTERS)}


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
        # Экземпляры пересоздадутся на следующем вызове; reset_webchats, а не
        # `_webchats = {}`: выбывшие инстансы закрывают свои вкладки (идущий
        # вызов — по его завершении), иначе они жили бы в пуле до конца процесса
        bot.router.reset_webchats()
        # Отдельный роутер LTM (LTM_MODEL_PROVIDER) — те же сайты и сброс,
        # иначе фон памяти продолжал бы ходить в выключенный веб-чат
        ltm_router = getattr(getattr(getattr(bot, "memory", None), "ltm", None),
                             "llm_router", None)
        if ltm_router is not None and ltm_router is not bot.router \
                and hasattr(ltm_router, "reset_webchats"):
            ltm_router.webchat_sites = list(norm)
            ltm_router.reset_webchats()
    # Веб-чаты выключены: вкладки side служебных задач закрываются (сайты
    # задач персон резолвятся заново на каждом вызове — по их цепочкам)
    if not norm:
        from app.core.local_router import get_local_router
        get_local_router().reset_webchats()
        if (os.getenv("LOCAL_LLM_BACKEND") or "").lower() == "webchat":
            _persist_env("LOCAL_LLM_BACKEND", "ollama")
    logger.info(f"[Settings] webchat-провайдеры: {','.join(norm) or 'выключены'}")
    return {"ok": True, "webchat_site": norm[0] if norm else None,
            "webchat_sites": norm}


def test_webchat(site) -> dict:
    """Проба веб-чата из общих настроек: одно слово «test» в stateless-канал
    probe (свежий чат — постоянный чат сайта и его лимиты не трогаем).
    ok=True — сайт ответил (с latency и превью); иначе честная причина:
    карантин (с временем восстановления), недоступность браузера, таймаут."""
    import time as _time
    from app.core import timeutil
    from app.features import web_llm
    site = str(site or "").strip().lower()
    if site not in web_llm.ADAPTERS:
        return {"ok": False, "error": f"неизвестный веб-чат «{site or '—'}»"}
    q = (web_llm.quarantine_status() or {}).get(site)
    if q:
        until = float(q.get("until") or 0)
        # timeutil.from_ts, а не time.localtime: карантин истекает по
        # времени пользователя, а не системного пояса процесса
        when = timeutil.from_ts(until).strftime("%H:%M") if until \
            else "позже"
        reason = str(q.get("reason") or "блокировка")[:80]
        return {"ok": False, "error": f"в карантине до {when}: {reason}"}
    chat = web_llm.WebChatLLM(site, context="settings_probe", channel="probe",
                              quota_per_hour=None)
    t0 = _time.monotonic()
    try:
        resp = chat.get_response([{"role": "user", "content": "test"}],
                                 timeout=90.0, lock_timeout=10.0)
    except Exception as e:
        logger.info(f"[Settings] проба веб-чата {site} упала: {e}")
        return {"ok": False, "error": str(e)[:150]}
    dt = _time.monotonic() - t0
    if resp and resp.strip():
        logger.info(f"[Settings] проба веб-чата {site}: ok за {dt:.1f}с")
        return {"ok": True, "latency_sec": round(dt, 1),
                "preview": " ".join(resp.split())[:80]}
    return {"ok": False, "error": f"нет ответа за {int(dt)}с"}


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
    try:
        validate_env_value(model)
    except ValueError as e:
        return {"ok": False, "detail": f"Некорректное имя модели: {e}"}

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
    # Добавить ключ провайдеру: в .env (первый свободный слот), в рантайм, в живых ботов.
    if provider not in PROVIDER_CONFIGS:
        return {"ok": False, "detail": f"Неизвестный провайдер: {provider}"}
    key = key.strip()
    if not key:
        return {"ok": False, "detail": "Пустой ключ"}
    try:
        validate_env_value(key)
    except ValueError as e:
        return {"ok": False, "detail": f"Некорректный ключ: {e}"}

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
    # Удалить переменную из .env и из окружения процесса (атомарно, под локом).
    _security_remove_env(_ENV_PATH, var)
    os.environ.pop(var, None)


def delete_provider_key(provider: str, index: int) -> dict:
    # Удалить ключ провайдера по индексу (порядок — как в list_providers).
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


# ── Часовой пояс пользователя (TIMEZONE, см. app/core/timeutil) ────────
# По образцу /api/settings/location: настройка глобальная, персист в .env +
# живой процесс через _persist_env/_remove_env, действует сразу — timeutil
# читает TIMEZONE из окружения на каждый вызов, пересоздавать менеджеров не
# нужно.

def _system_tz_label() -> str:
    """Человеко-читаемая метка системного пояса для UI, когда TIMEZONE не
    задан (или задан, но не распознан) — IANA-имени системного пояса без
    сторонних зависимостей не достать, поэтому берём то, что отдаёт aware-
    datetime: сокращение ("MSK") или смещение от UTC как запасной вариант."""
    from datetime import datetime
    try:
        local = datetime.now().astimezone()
        return local.tzname() or str(local.utcoffset())
    except Exception:
        return "system"


def get_timezone() -> dict:
    """Текущий часовой пояс: timezone — сырое значение TIMEZONE из окружения
    ("" — не задано), effective — фактически используемый пояс, source —
    откуда он взят ("env" — TIMEZONE распознан, "system" — не задан или имя
    не распознано, работает системный локальный пояс)."""
    from app.core import timeutil
    configured = timeutil.tz_name()
    if timeutil.tz() is not None:
        return {"timezone": configured, "effective": configured, "source": "env"}
    return {"timezone": configured, "effective": _system_tz_label(), "source": "system"}


def set_timezone(value: str) -> dict:
    """Сохранить часовой пояс пользователя: пусто — сброс на системный
    (TIMEZONE удаляется из .env), иначе имя зоны IANA, проверенное через
    zoneinfo.ZoneInfo — невалидное не попадает ни в .env, ни в окружение
    живого процесса (в отличие от timeutil.tz(), который битое имя на чтении
    просто игнорирует — здесь его нужно отклонить ДО записи)."""
    value = (value or "").strip()
    if not value:
        _remove_env("TIMEZONE")
        return {"ok": True, **get_timezone()}
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return {"ok": False, "detail": "zoneinfo недоступен на этом Python"}
    try:
        ZoneInfo(value)
    except Exception as e:
        return {"ok": False, "detail": f"Неизвестный часовой пояс «{value}»: {e}"}
    try:
        _persist_env("TIMEZONE", value)
    except ValueError as e:
        return {"ok": False, "detail": f"Некорректное имя пояса: {e}"}
    return {"ok": True, **get_timezone()}


def _apply_llm_to_bot(persona: str, llm_cfg: dict):
    # Применить секцию llm к живому роутеру бота (без перезапуска).
    from app.api.runtime import registry
    bot = registry._bots.get(persona)
    if bot is not None:
        bot.router.set_persona_llm(llm_cfg.get("primary"), llm_cfg.get("fallback"),
                                   llm_cfg.get("models"),
                                   webchat_limits=llm_cfg.get("webchat_limits"),
                                   webchat_modes=llm_cfg.get("webchat_mode"),
                                   answer_provider=llm_cfg.get("answer_provider"),
                                   cc_provider=llm_cfg.get("cc_provider"),
                                   vision_provider=llm_cfg.get("vision_provider"),
                                   exclude=llm_cfg.get("exclude"))
        _bind_local_tasks(bot, llm_cfg)


def _bind_local_tasks(bot, llm_cfg: dict):
    # Движки служебных задач персоны: llm.local_tasks + её цепочка провайдеров
    # (веб-чаты фоновых задач резолвятся из bot.router на каждом вызове)
    from app.core.local_router import get_local_router
    get_local_router().bind_persona(bot.context, bot.router,
                                    llm_cfg.get("local_tasks"))


def get_persona_local_tasks(persona: str) -> dict | None:
    """Движки служебных задач персоны (досье → настройки): bg_site, её
    основной/первый fallback веб-чат и resolved-движок каждой задачи.
    None — персоны нет."""
    from app.api.runtime import registry
    from app.core.local_router import get_local_router
    bot = registry.get(persona)
    if bot is None:
        return None
    return get_local_router().task_snapshot(bot.context)


def update_persona_local_tasks(persona: str, task: str | None = None,
                               backend: str | None = None,
                               site: str | None = None,
                               bg_site: str | None = None) -> dict | None:
    """Выбор движков служебных задач персоны (YAML, llm.local_tasks).

    task + backend: «ollama» | «webchat» (site — сайт задачи; пусто —
    веб-чат фоновых задач персоны) | «default» — снять выбор, движок по роду
    задачи. bg_site: веб-чат фоновых задач — «fallback» (первый веб-чат
    цепочки после основного, дефолт) | «primary» (основной) | «rotate»
    (сайты по очереди из llm.local_tasks.rotate) | имя сайта.
    Применяется к живому боту сразу. None — персоны нет; {"ok": False,
    "detail"} — отказ; иначе {"ok": True, **снимок}."""
    from app.core.local_router import (BG_SITE_MODES, DEFAULT_BG_SITE,
                                       LOCAL_TASKS)
    from app.features.web_llm import ADAPTERS as _WC_ADAPTERS

    entry = None
    if task is not None:
        if task not in LOCAL_TASKS:
            return {"ok": False, "detail": f"Неизвестная задача «{task}»"}
        backend = (backend or "").strip().lower()
        if backend not in ("ollama", "webchat", "default"):
            return {"ok": False,
                    "detail": "Движок должен быть «ollama», «webchat» или «default»"}
        if backend == "webchat" and LOCAL_TASKS[task]:
            return {"ok": False,
                    "detail": "Эта задача технически не может уйти в веб-чат"}
        site = (site or "").strip().lower() or None
        if site is not None and site not in _WC_ADAPTERS:
            return {"ok": False, "detail": f"Неизвестный веб-чат «{site}»"}
        if backend != "default":
            entry = {"backend": backend}
            if backend == "webchat" and site:
                entry["site"] = site
    if bg_site is not None:
        bg_site = bg_site.strip().lower()
        if bg_site not in BG_SITE_MODES and bg_site not in _WC_ADAPTERS:
            return {"ok": False, "detail": f"Неизвестный веб-чат «{bg_site}»"}

    path = _persona_yaml_path(persona)
    if path is None:
        return None
    # Лок на весь read-modify-write (см. update_persona_proactive)
    with yaml_write_lock:
        if not path.is_file():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not data.get("system_prompt"):
            return None
        llm = data.get("llm") or {}
        lt = llm.get("local_tasks")
        lt = dict(lt) if isinstance(lt, dict) else {}
        tasks = lt.get("tasks")
        tasks = dict(tasks) if isinstance(tasks, dict) else {}
        if task is not None:
            if entry is None:
                tasks.pop(task, None)
            else:
                tasks[task] = entry
        if tasks:
            lt["tasks"] = tasks
        else:
            lt.pop("tasks", None)
        if bg_site is not None:
            if bg_site == DEFAULT_BG_SITE:
                lt.pop("bg_site", None)
            else:
                lt["bg_site"] = bg_site
        if lt:
            llm["local_tasks"] = lt
        else:
            llm.pop("local_tasks", None)
        if llm:
            data["llm"] = llm
        else:
            data.pop("llm", None)
        atomic_write_text(path, _dump_persona_yaml(data))

    from app.api.runtime import registry
    from app.core.local_router import get_local_router
    bot = registry.get(persona)
    if bot is None:
        return None
    bot.persona.persona_data = data
    _bind_local_tasks(bot, data.get("llm") or {})
    return {"ok": True, **get_local_router().task_snapshot(bot.context)}


def _apply_computer_control_live(bot, cc_cfg):
    """computer_control на живую: allowlist'ы перечитываются менеджером,
    при включении фичи менеджер создаётся, при выключении — снимается.
    Выключение — false/пусто или dict с enabled: false (списки сохраняются).
    Pending-подтверждения и статистика переживают обновление."""
    from app.features.computer_control import ComputerControlManager, config_enabled
    # allowed_users читаем независимо от enabled — сохраняется, как и прочие
    # allowlist'ы фичи, даже пока сам режим временно выключен
    bot._cc_allowed_users = {
        str(u).strip() for u in (cc_cfg.get("allowed_users", []) if isinstance(cc_cfg, dict) else [])
        if str(u).strip()
    }
    if config_enabled(cc_cfg):
        if getattr(bot, "computer_control", None) is not None:
            bot.computer_control.update_config(cc_cfg if isinstance(cc_cfg, dict) else {})
        else:
            bot.computer_control = ComputerControlManager(
                context=bot.context, config=cc_cfg if isinstance(cc_cfg, dict) else {})
        logger.info(f"[{bot.persona_name}] Computer control обновлён на живую")
    else:
        bot.computer_control = None
    # Сценарии/агент/flavor-банк — на текущий менеджер (или сняты вместе с ним)
    rebind = getattr(bot, "rebind_computer_control", None)
    if callable(rebind):
        try:
            rebind()
        except Exception as e:
            logger.warning(f"[{bot.persona_name}] надстройки computer control "
                           f"не перепривязались: {e}")


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
    path = _persona_yaml_path(persona)
    if path is None or not path.is_file():
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
            # дешёвые гейты перед LLM-скорингом инициативы
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
    path = data_dir() / f"api_{persona}" / "ignore_streak.json"
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
    path = _persona_yaml_path(persona)
    if path is None or not path.is_file():
        return None

    # Лок на весь read-modify-write: без него конкурентная правка (например,
    # автосохранение формы) может перечитать данные до записи другого запроса
    # и затереть его правку своей — атомарность самой записи (tmp+os.replace)
    # этого не решает, нужна сериализация всего цикла целиком.
    with yaml_write_lock:
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
        atomic_write_text(
            path, _dump_persona_yaml(data)
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


class _PersonaYamlDumper(yaml.SafeDumper):
    """SafeDumper, пишущий многострочные строки блоком `|`.

    Стандартный safe_dump кладёт многострочный system_prompt в кавычки с
    экранированием (`...don't\\n  \\ just shows.\\n`) — в файле и в
    веб-редакторе остаются \\n и слэши вместо переносов строк.
    """


def _represent_str(dumper: yaml.SafeDumper, value: str):
    if "\n" in value:
        # Литеральный блок PyYAML не берёт при пробелах в конце строк —
        # они незначимы, срезаем, чтобы не откатываться в кавычки
        value = "\n".join(line.rstrip() for line in value.split("\n"))
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_PersonaYamlDumper.add_representer(str, _represent_str)


def _dump_persona_yaml(data: dict) -> str:
    """YAML персоны для записи в файл (read-modify-write из веб-настроек)."""
    return yaml.dump(
        data, Dumper=_PersonaYamlDumper, allow_unicode=True, sort_keys=False, width=120
    )


# Сюда пишутся новые персоны (создание, копия)
_PERSONAS_DIR = CORE_PERSONAS_DIR


def _persona_yaml_path(persona: str) -> Path | None:
    """Путь к YAML персоны, если имя прошло проверку и путь не выходит за
    пределы папки персон — иначе None (везде ниже это уже означает «персоны
    нет», как и отсутствующий файл). Существующий файл ищется в app/personas
    и в папках персон установленных аддонов; нет нигде — путь в
    _PERSONAS_DIR (туда его создаст create_persona/duplicate_persona).
    Общая точка для всех мест этого модуля (get_persona_config/
    update_persona_config/save_persona_yaml/duplicate_persona и т.д.): их
    вызывают эндпоинты БЕЗ предварительного _get_bot()/list_personas(),
    поэтому без этой проверки traversal-имя дошло бы сюда напрямую и
    читало/писало бы произвольный существующий файл."""
    dirs = [_PERSONAS_DIR] + [d for d in persona_dirs() if d != CORE_PERSONAS_DIR]
    for personas_dir in dirs:
        path = safe_join(personas_dir, persona, ".yaml")
        if path is None:
            return None
        if path.is_file():
            return path
    return safe_join(_PERSONAS_DIR, persona, ".yaml")


def get_persona_config(persona: str) -> dict | None:
    path = _persona_yaml_path(persona)
    if path is None:
        return None
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
            # Провайдеры, убранные персоной из своей автоматической цепочки
            "exclude": llm.get("exclude") or [],
            "models": llm.get("models") or {},  # свои модели по провайдерам
            # Провайдеры по назначению: реплики персоны в режиме управления /
            # решения режима управления / vision-фолбэк (None — обычная цепочка)
            "answer_provider": llm.get("answer_provider"),
            "cc_provider": llm.get("cc_provider"),
            "vision_provider": llm.get("vision_provider"),
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
    path = _persona_yaml_path(persona)
    if path is None:
        return None

    # Лок на весь read-modify-write (см. update_persona_proactive) —
    # без него конкурентная правка теряется при перезаписи.
    with yaml_write_lock:
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
            telegram = _has_telegram_bot(persona)
            for k, v in features.items():
                # reminder/todo/inventory (как и proactive/muted/light_context/
                # computer_control) применяются на живую — рестарт не нужен;
                # доступ к режиму управления у Telegram-бота — после его рестарта
                if merged_f.get(k) != v and (
                        k not in _LIVE_FEATURE_KEYS
                        or (telegram and k in _TELEGRAM_RESTART_KEYS)):
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
            # Провайдеры по назначению: пустая строка/None — снять (цепочка)
            for key in ("answer_provider", "cc_provider", "vision_provider"):
                if key in llm:
                    if llm[key]:
                        merged_l[key] = llm[key]
                    else:
                        merged_l.pop(key, None)
            if llm.get("fallback") is not None:
                if llm["fallback"]:
                    merged_l["fallback"] = llm["fallback"]
                else:
                    merged_l.pop("fallback", None)
            if llm.get("exclude") is not None:
                # Провайдеры, убранные персоной из своей цепочки: None —
                # не трогать, пустой список — убрать ключ (нет исключений)
                if llm["exclude"]:
                    merged_l["exclude"] = llm["exclude"]
                else:
                    merged_l.pop("exclude", None)
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

        atomic_write_text(
            path, _dump_persona_yaml(data)
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
    path = _persona_yaml_path(persona)
    if path is None or not path.is_file():
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return {"ok": False, "detail": f"YAML не парсится: {e}"}
    # Без system_prompt файл перестанет быть персоной (list_personas его потеряет)
    if not isinstance(data, dict) or not data.get("system_prompt"):
        return {"ok": False, "detail": "YAML должен быть объектом с непустым system_prompt"}

    new_f = data.get("features") or {}
    if not isinstance(new_f, dict):
        new_f = {}
    with yaml_write_lock:
        try:
            old_file = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            old_file = {}
        old_file_f = old_file.get("features") if isinstance(old_file, dict) else None
        if not isinstance(old_file_f, dict):
            old_file_f = {}
        atomic_write_text(path, raw)
    # Свежая заморозка (как тумблер в форме) — рушим настроение ниже
    mute_ruins_mood = new_f.get("muted") is True and old_file_f.get("muted") is not True
    # Доступ к режиму управления у Telegram-бота — после его перезапуска
    restart_required = _has_telegram_bot(persona) and any(
        old_file_f.get(k) != new_f.get(k) for k in _TELEGRAM_RESTART_KEYS)

    # Живой бот: то же, что применяет форма настроек (update_persona_config),
    # по каждой изменённой секции features. Без этого выключенный в YAML
    # computer_control или убранный из allowed_users человек сохраняли бы
    # доступ к режиму управления до перезапуска, а restart_required молчал бы.
    from app.api.runtime import registry
    bot = registry._bots.get(persona)
    if bot is not None:
        old_data = getattr(bot.persona, "persona_data", None) or {}
        old_f = bot.features or {}
        changed_keys = {k for k in set(old_f) | set(new_f) if old_f.get(k) != new_f.get(k)}
        changed_top = {k for k in _RESTART_TOP_KEYS if old_data.get(k) != data.get(k)}
        restart_required = (restart_required or bool(changed_keys - _LIVE_FEATURE_KEYS)
                            or bool(changed_top))
        bot.persona.persona_data = data
        bot.persona.system_prompt = data.get("system_prompt", "")
        bot.persona.settings = data.get("settings") or {}
        if data.get("stm_size") is not None:
            bot.stm_size = data["stm_size"]
        bot.features = new_f
        _apply_feature_managers_live(persona, bot)
        if "proactive" in changed_keys:
            _apply_proactive_live(persona, bot, new_f.get("proactive"))
        if "rhythm" in changed_keys:
            _apply_rhythm_live(persona, bot, new_f.get("rhythm"))
        if "life" in changed_keys:
            _apply_life_live(persona, bot, new_f.get("life"))
        if "computer_control" in changed_keys:
            _apply_computer_control_live(bot, new_f.get("computer_control"))
        _apply_llm_to_bot(persona, data.get("llm") or {})

    # Свежая заморозка рушит настроение — как при заморозке из формы
    if mute_ruins_mood:
        _ruin_persona_mood(persona)
    return {"ok": True, "restart_required": restart_required}


# ── Создание / удаление / дублирование персон ──
#
# Допустимый id персоны = имя YAML-файла (фронт шлёт его же в API-вызовах,
# поэтому файл обязан совпадать с полем id) — формат единый на весь модуль,
# см. app/api/security.PERSONA_ID_RE.


def create_persona(raw: str, memory: str | None = None) -> dict:
    """Создать новую персону из сырого YAML (модалка создания в вебе).

    Имя файла = поле id из YAML. Файл подхватывается реестром автоматически
    (list_personas читает диск), рестарт не нужен.

    Под этим id на диске могла остаться память (удалённая персона её не
    стирает) — молча её не подхватываем: без memory ответ — конфликт с
    memory_exists. memory="keep" — подхватить как есть, "fresh" — убрать
    старую память в архив (_archive_persona_memory) и начать с чистого листа.
    {"ok": True, "persona": id, "archived": [...]} |
    {"ok": False, "detail", "conflict": bool, "memory_exists"?: True, "status"?: int}
    """
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return {"ok": False, "detail": f"YAML не парсится: {e}"}
    if not isinstance(data, dict) or not data.get("system_prompt"):
        return {"ok": False, "detail": "YAML должен быть объектом с непустым system_prompt"}
    persona_id = str(data.get("id") or "").strip()
    if not persona_id or not PERSONA_ID_RE.match(persona_id):
        return {"ok": False, "detail": "Поле id обязательно: латиница, цифры, _ и - (до 64 символов)"}
    if _reserved_id(persona_id):
        return {"ok": False, "status": 400, "detail": _reserved_id_detail(persona_id)}
    path = _persona_yaml_path(persona_id)
    if path is None:
        return {"ok": False, "detail": "Поле id обязательно: латиница, цифры, _ и - (до 64 символов)"}
    archived: list[tuple[Path, Path]] = []
    with yaml_write_lock:
        if path.exists():
            return {"ok": False, "conflict": True, "detail": f"Персона '{persona_id}' уже существует"}
        leftovers = _leftover_memory(persona_id)
        if leftovers and memory not in ("keep", "fresh"):
            return {"ok": False, "conflict": True, "memory_exists": True, "persona": persona_id,
                    "detail": (f"Под id '{persona_id}' осталась память прежней персоны "
                               f"({', '.join(str(p) for p in leftovers)}). Подхватить её "
                               "(memory: keep) или начать с чистого листа — старая "
                               "уйдёт в архив (memory: fresh)?")}
        if leftovers and memory == "fresh":
            _forget_previous_persona(persona_id)
            try:
                archived = _archive_persona_memory(persona_id)
            except Exception as e:
                logger.exception(f"[api] Память '{persona_id}' не убрана в архив")
                return {"ok": False, "status": 500,
                        "detail": f"Не удалось убрать старую память в архив: {e}"}
        atomic_write_text(path, raw)
    if leftovers and memory == "keep":
        logger.info(f"[api] Персона {persona_id} подхватила оставшуюся память")
    logger.info(f"[api] Создана персона {persona_id}")
    # Банк flavor-реплик для CC-команд: фоновая генерация
    # сразу при создании персоны, если у неё включён computer_control.
    # Контекст — веб-бота (api_<id>, как его создаёт registry): он и читает
    # банк. Telegram-бот (контекст <id>) соберёт свой при старте сам
    try:
        from app.features.computer_control import config_enabled as _cc_on
        if _cc_on((data.get("features") or {}).get("computer_control", False)):
            from app.core.presence import web_context
            from app.features import flavor_text
            flavor_text.ensure_flavor_bank(
                context=web_context(persona_id), system_prompt=str(data["system_prompt"]))
    except Exception as _fe:
        logger.debug(f"[api] flavor-банк для {persona_id} не запущен: {_fe}")
    return {"ok": True, "persona": persona_id, "archived": [str(d) for _, d in archived]}


def delete_persona(persona: str) -> bool:
    """Удалить YAML персоны и выгрузить бота из реестра (фоновые циклы стоп).

    Память персоны (data/api_{persona}/) намеренно остаётся на диске: новая
    персона с тем же id молча её не подхватит — create_persona спросит,
    подхватить или убрать в архив.
    """
    from app.api.runtime import list_personas, registry
    if persona not in list_personas():  # защита и от traversal, и от удаления служебных yaml
        return False
    path = _persona_yaml_path(persona)
    if path is None:  # не должно случиться после проверки выше — доп. рубеж
        return False
    registry.evict(persona)
    path.unlink()
    try:
        from app.api import skins_api
        skins_api.forget_persona(persona)
    except Exception:
        logger.exception("[api] Удаление персоны: назначение скина не снято")
    logger.info(f"[api] Удалена персона {persona}")
    return True


def _set_top_field(text: str, key: str, line: str, after: str | None = None) -> str:
    """Заменить верхнеуровневое `key: ...` строкой line (текст и комментарии
    остальных строк не трогаются). Поля нет — вставить после строки `after:`
    или в начало файла."""
    pattern = rf"(?m)^{key}:.*$"
    if re.search(pattern, text):
        return re.sub(pattern, lambda _m: line, text, count=1)
    if after and re.search(rf"(?m)^{after}:.*$", text):
        return re.sub(rf"(?m)^({after}:.*)$", lambda m: m.group(1) + "\n" + line, text, count=1)
    return line + "\n" + text


def duplicate_persona(persona: str) -> dict | None:
    """Копия YAML персоны с новым id/name. None — персоны нет.

    Правятся только верхнеуровневые id:/name: — остальной текст (включая
    комментарии) копируется как есть.
    """
    src = _persona_yaml_path(persona)
    if src is None or not src.is_file():
        return None

    with yaml_write_lock:
        raw = src.read_text(encoding="utf-8")

        n = 1
        dest = None
        while n <= 1000:  # разумный потолок — не крутиться вечно на патологии
            new_id = f"{persona}_copy" if n == 1 else f"{persona}_copy{n}"
            candidate = _persona_yaml_path(new_id)
            if candidate is None:
                # new_id вышел за формат id (например, persona у самого предела
                # длины) — короче не станет, дальше пробовать бессмысленно
                return {"ok": False, "detail": "Не удалось подобрать id для копии"}
            # Копия — новая персона: id, под которым осталась чужая память
            # (удалённая прежняя копия), пропускаем — иначе подхватила бы её;
            # служебные имена папок данных — тоже
            if (not candidate.exists() and not _reserved_id(new_id)
                    and not _leftover_memory(new_id)):
                dest = candidate
                break
            n += 1
        if dest is None:
            return {"ok": False, "detail": "Не удалось подобрать id для копии"}

        data = yaml.safe_load(raw) or {}
        new_name = f"{data.get('name') or persona} (копия)"

        out = _set_top_field(raw, "id", f"id: {new_id}")
        escaped = new_name.replace("\\", "\\\\").replace('"', '\\"')
        out = _set_top_field(out, "name", f'name: "{escaped}"')
        atomic_write_text(dest, out)
    logger.info(f"[api] Персона {persona} продублирована в {new_id}")
    return {"ok": True, "persona": new_id}


_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def set_persona_color(persona: str, color: str | None) -> dict:
    """Цвет метки персоны (карточка, календарь): строка `color:` в YAML.

    Правится только эта строка (текст и комментарии остального файла не
    трогаются); None — убрать поле, цвет снова вычисляется из id
    (runtime.persona_color). {"ok": True, "color"} | {"ok": False, "detail", "status"}.
    """
    from app.api.runtime import list_personas, persona_color, persona_yaml_path
    if persona not in list_personas():
        return {"ok": False, "status": 404, "detail": f"Персона '{persona}' не найдена"}
    if color is not None and not _COLOR_RE.match(color):
        return {"ok": False, "status": 400, "detail": "Цвет — в формате #RRGGBB"}
    path = persona_yaml_path(persona)
    with yaml_write_lock:
        raw = path.read_text(encoding="utf-8")
        if color is None:
            out = re.sub(r"(?m)^color:.*\n?", "", raw, count=1)
        else:
            out = _set_top_field(raw, "color", f"color: '{color.lower()}'", after="description")
        if out != raw:
            atomic_write_text(path, out)
        data = yaml.safe_load(out) or {}
    logger.info(f"[api] Цвет персоны {persona}: {color or 'по умолчанию'}")
    return {"ok": True, "color": persona_color(persona, data)}


def _data_roots() -> list[Path]:
    """Папки данных, где может лежать память персоны. Их несколько: data_dir()
    (VPC_DATA_DIR), Config.DATA_DIR (DATA_DIR — Chroma, living, self_memory,
    календарь) и data/ в корне проекта (clear_backups) — обычно это одна и та
    же папка, но env может развести их."""
    from app.core.config import Config
    roots: list[Path] = []
    for root in (data_dir(), Path(Config.DATA_DIR), Path(__file__).parent.parent.parent / "data"):
        r = root.resolve()
        if r not in roots:
            roots.append(r)
    return roots


# Общие папки в корне данных, совпадающие по форме с id персоны: это не её
# память (Telegram-контекст «tg», скины, черновики, служебные роутеры) —
# переносить/архивировать их вместе с персоной нельзя, а персона с таким id
# делила бы папку Telegram-контекста data/<id> с ними (_reserved_id)
_SHARED_DATA_NAMES = {"tg", "default", "skins", "skin_gen", "settings_probe", "persona_drafts"}

# Файлы в папке персоны, которые не память: выводятся заново из YAML (банк
# flavor-фраз пересобирается по хэшу system_prompt). Папка только с ними —
# не повод спрашивать «подхватить память?»
_DERIVED_FILES = {"flavor_bank.json"}


def _reserved_id(persona: str) -> bool:
    """id совпадает со служебной папкой данных (без учёта регистра: на
    macOS/Windows TG и tg — одна папка)."""
    return persona.lower() in _SHARED_DATA_NAMES


def _reserved_id_detail(persona: str) -> str:
    return (f"id '{persona}' совпадает со служебной папкой данных "
            f"({', '.join(sorted(_SHARED_DATA_NAMES))}) — выберите другой")


def _persona_data_dirs(persona: str) -> list[Path]:
    """Кандидаты папок памяти персоны: api_<id> (веб/API) и <id> (Telegram).

    Под папкой — всё, что ключено контекстом персоны: STM/LTM (Chroma), living,
    self_memory, файлы, дела, напоминания, обучение, досье, проактивность,
    ритм, инвентарь, комната, аватар, режим управления (в т.ч. адреса
    веб-чатов), корзина очистки; банк flavor-фраз там же — производный, памятью
    не считается (_DERIVED_FILES). Вне этих папок по id ключены
    только записи календаря (метка persona), назначение скина, токен
    <ID>_BOT_TOKEN в .env, метка прогрева api_last_seen.json и очередь
    фоновых сообщений в памяти процесса."""
    out = []
    for root in _data_roots():
        for prefix in ("api_", ""):
            if not prefix and _reserved_id(persona):
                continue
            path = safe_join(root, persona, prefix=prefix)
            if path is not None:
                out.append(path)
    return out


def _holds_memory(path: Path) -> bool:
    """Есть ли по пути память: что-то кроме производных файлов (_DERIVED_FILES)."""
    if not path.is_dir():
        return path.exists()
    try:
        return any(p.name not in _DERIVED_FILES for p in path.iterdir())
    except OSError:
        return True


def _leftover_memory(persona: str) -> list[Path]:
    """Оставшаяся на диске память id (например, от удалённой персоны)."""
    return [p for p in _persona_data_dirs(persona) if p.exists() and _holds_memory(p)]


def _release_memory_dirs(dirs) -> None:
    """Перед переносом папок памяти: закрыть их базы Chroma в этом процессе —
    иначе новая персона под тем же путём получила бы закешированную базу,
    смотрящую в перенесённый файл (см. chroma_space.release_clients_under)."""
    from app.core.chroma_space import release_clients_under
    try:
        release_clients_under(dirs)
    except Exception:
        logger.exception("[api] Базы Chroma перед переносом папок не выгружены")


def _archive_paths(paths: list[Path]) -> list[tuple[Path, Path]]:
    """Убрать папки в архивные рядом: <папка>.archived-ГГГГММДД-ЧЧММСС.
    Ничего не удаляется. Точка в имени — вне формата id, архив не станет
    памятью будущей персоны. Возвращает [(было, стало)]; сбой — откат уже
    перенесённого и исключение."""
    from app.core import timeutil
    stamp = timeutil.now().strftime("%Y%m%d-%H%M%S")
    _release_memory_dirs(paths)
    done: list[tuple[Path, Path]] = []
    try:
        for src in paths:
            dst = src.with_name(f"{src.name}.archived-{stamp}")
            n = 2
            while dst.exists():
                dst = src.with_name(f"{src.name}.archived-{stamp}-{n}")
                n += 1
            src.rename(dst)
            done.append((src, dst))
    except Exception:
        for src, dst in reversed(done):
            try:
                dst.rename(src)
            except OSError:
                logger.exception(f"[api] Откат архива памяти: не удалось вернуть {dst} → {src}")
        raise
    if done:
        logger.info(f"[api] В архив: {', '.join(str(d) for _, d in done)}")
    return done


def _archive_persona_memory(persona: str) -> list[tuple[Path, Path]]:
    """Всё, что осталось на диске под id (память и производные файлы), — в
    архив рядом (_archive_paths): новая персона начинает с чистого листа."""
    return _archive_paths([p for p in _persona_data_dirs(persona) if p.exists()])


def _forget_previous_persona(persona: str) -> None:
    """Перед архивом памяти id: выгрузить бота (его фоновые циклы пишут в
    папку памяти), снять недоставленные фоновые сообщения и назначение скина
    прежней персоны (сам скин остаётся в библиотеке)."""
    from app.api.runtime import registry
    registry.evict(persona)
    try:
        from app.api.inbox import inbox_drop
        inbox_drop(persona)
    except Exception:
        logger.exception("[api] Очередь фоновых сообщений не очищена")
    try:
        from app.api import skins_api
        skins_api.forget_persona(persona)
    except Exception:
        logger.exception("[api] Назначение скина прежней персоны не снято")


def rename_persona(persona: str, new_id: str, memory: str | None = None) -> dict:
    """Сменить id персоны: YAML-файл (в той же папке персон), поле id:, папки
    памяти data/api_<id> и data/<id> (с аватаром), записи календаря, токен
    <ID>_BOT_TOKEN в .env и недоставленные фоновые сообщения.

    Chroma-коллекции общие по имени и изолированы только папкой, id внутри
    базы нет — переименования папки достаточно. Бот персоны выгружается из
    реестра и пересоздаётся под новым id при первом обращении.

    Под новым id осталась память (удалённой персоны) — как в create_persona:
    без memory — 409 с memory_exists; "fresh" — старая память уходит в архив,
    "keep" — подхватить её (только если своя память персоны с ней не
    пересекается по папкам — can_keep в ответе 409).

    {"ok": True, "persona", "restart_required", "archived"} |
    {"ok": False, "detail", "status", "memory_exists"?, "can_keep"?}
    (404 — нет персоны, 409 — id занят).
    """
    from app.api.runtime import list_personas, persona_color, persona_yaml_path, registry

    if persona not in list_personas():
        return {"ok": False, "status": 404, "detail": f"Персона '{persona}' не найдена"}
    new_id = (new_id or "").strip()
    if not PERSONA_ID_RE.match(new_id):
        return {"ok": False, "status": 400,
                "detail": "id: латиница, цифры, _ и - (до 64 символов)"}
    if new_id == persona:
        return {"ok": False, "status": 400, "detail": "Новый id совпадает с текущим"}
    # Регистр важен: на macOS/Windows файловая система его не различает —
    # alex → Alex дал бы «конфликт» с самим собой; такие переименования не делаем
    if new_id.lower() == persona.lower():
        return {"ok": False, "status": 400, "detail": "id отличается только регистром букв — выберите другой"}
    if _reserved_id(new_id):
        return {"ok": False, "status": 400, "detail": _reserved_id_detail(new_id)}
    if persona_yaml_path(new_id) is not None:
        return {"ok": False, "status": 409, "detail": f"Персона '{new_id}' уже существует"}
    src_path = persona_yaml_path(persona)
    dest_path = safe_join(src_path.parent, new_id, ".yaml") if src_path else None
    if src_path is None or dest_path is None:
        return {"ok": False, "status": 404, "detail": f"Персона '{persona}' не найдена"}

    moves = [(src, safe_join(src.parent, new_id, prefix=src.name[:-len(persona)]))
             for src in _persona_data_dirs(persona) if src.is_dir()]

    leftovers = _leftover_memory(new_id)
    if leftovers:
        # Чужая (например, от удалённой персоны) память молча подмешалась бы
        # к этой — только по явному выбору. Подхватить можно, лишь если
        # своя память персоны ляжет в другие папки, не поверх оставшейся
        can_keep = not any(dst is None or (dst.exists() and _holds_memory(dst))
                           for _, dst in moves)
        busy = ", ".join(str(p) for p in leftovers)
        if memory == "keep" and not can_keep:
            return {"ok": False, "status": 409, "memory_exists": True, "can_keep": False,
                    "persona": new_id,
                    "detail": (f"Под id '{new_id}' осталась память прежней персоны ({busy}), "
                               "а у этой персоны своя — совместить нельзя. Старую можно "
                               "убрать в архив (memory: fresh)")}
        if memory not in ("keep", "fresh"):
            return {"ok": False, "status": 409, "memory_exists": True, "can_keep": can_keep,
                    "persona": new_id,
                    "detail": (f"Под id '{new_id}' осталась память прежней персоны ({busy}). "
                               "Убрать её в архив (memory: fresh)"
                               + (" или подхватить (memory: keep)?" if can_keep else "?"))}

    with yaml_write_lock:
        raw = src_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
        out = _set_top_field(raw, "id", f"id: {new_id}")
        # Цвет без явного color: считается от id — закрепляем текущий, чтобы
        # метка персоны в календаре и карточке не сменила цвет
        if not (isinstance(data.get("color"), str) and data["color"].strip()):
            out = _set_top_field(out, "color", f"color: '{persona_color(persona, data)}'", after="description")

        if leftovers and memory == "fresh":
            to_archive = [p for p in _persona_data_dirs(new_id) if p.exists()]
        else:
            # Производные остатки (банк фраз) на пути своей памяти — в архив
            to_archive = [dst for _, dst in moves
                          if dst is not None and dst.exists() and not _holds_memory(dst)]
        # Страховка: путь переноса занят тем, что в архив не уходит (гонка,
        # чужая папка) — поверх не переносим (пустую папку rename бы заменил)
        blocked = [str(dst) for _, dst in moves
                   if dst is None or (dst.exists() and dst not in to_archive)]
        if blocked:
            return {"ok": False, "status": 409,
                    "detail": f"Папка данных для '{new_id}' уже существует: {', '.join(blocked)}"}

        # Выгрузить бота до переноса: его фоновые циклы пишут в папку памяти
        registry.evict(persona)
        if leftovers and memory == "fresh":
            _forget_previous_persona(new_id)
        try:
            archived = _archive_paths(to_archive)
        except Exception as e:
            logger.exception(f"[api] Смена id: память '{new_id}' не убрана в архив")
            return {"ok": False, "status": 500,
                    "detail": f"Не удалось убрать старую память в архив: {e}"}
        # Базы Chroma переносимых папок — закрыть: иначе новая персона под
        # старым id получила бы закешированную базу, смотрящую в перенесённую
        _release_memory_dirs([src for src, _ in moves])
        done: list[tuple[Path, Path]] = []
        try:
            for src, dst in moves:
                src.rename(dst)
                done.append((src, dst))
            atomic_write_text(dest_path, out)
            src_path.unlink()
        except Exception as e:
            # Откат: папки назад, затем архив — на освободившееся место;
            # недописанный новый YAML — убрать
            for src, dst in [*reversed(done), *reversed(archived)]:
                try:
                    dst.rename(src)
                except OSError:
                    logger.exception(f"[api] Откат смены id: не удалось вернуть {dst} → {src}")
            if src_path.exists() and dest_path.exists():
                dest_path.unlink(missing_ok=True)
            logger.exception(f"[api] Смена id {persona} → {new_id} не удалась")
            return {"ok": False, "status": 500, "detail": f"Не удалось сменить id: {e}"}

    # Дальше — некритичные ссылки на id: сбой не откатывает переименование
    try:
        from app.features.calendar_manager import get_calendar
        cal = get_calendar()
        for entry in cal.list_entries():
            if entry.get("persona") == persona:
                cal.update_entry(entry["id"], persona=new_id)
    except Exception:
        logger.exception("[api] Смена id: записи календаря не обновлены")

    restart_required = False
    old_var, new_var = f"{persona.upper()}_BOT_TOKEN", f"{new_id.upper()}_BOT_TOKEN"
    token = os.getenv(old_var)
    if token:
        try:
            # Telegram-бот читает токен при старте — нужен его перезапуск
            _persist_env(new_var, token)
            _remove_env(old_var)
            restart_required = True
        except ValueError:
            # new_id с «-» не годится в имя переменной окружения
            logger.warning(f"[api] Смена id: {old_var} не перенесён — {new_var} недопустимое имя переменной")

    try:
        from app.api.inbox import inbox_rename
        inbox_rename(persona, new_id)
    except Exception:
        logger.exception("[api] Смена id: очередь фоновых сообщений не перенесена")

    try:
        from app.api import skins_api
        skins_api.rename_persona(persona, new_id)
    except Exception:
        logger.exception("[api] Смена id: назначение скина не перенесено")

    logger.info(f"[api] id персоны {persona} → {new_id} (папки: {[str(d) for _, d in moves]})")
    return {"ok": True, "persona": new_id, "restart_required": restart_required,
            "archived": [str(d) for _, d in archived]}

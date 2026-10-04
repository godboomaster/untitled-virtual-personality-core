"""FastAPI-сервер поверх BotInstance.

Тонкий HTTP-слой для веб-фронта (web/) и десктоп-приложения. Вся логика —
в BotInstance; здесь только сериализация, auth и разнос блокирующих вызовов
по потокам (process_message синхронный и может идти десятки секунд).

Запуск: python -m app.main api  (или uvicorn app.api.server:app)

Авторизация: если задан env API_TOKEN — все /api/* (кроме /api/health)
требуют заголовок ``Authorization: Bearer <API_TOKEN>``. Без API_TOKEN API
открыт (локальный режим по умолчанию).
"""

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api import runtime
from app.api.runtime import chat_lock, get_persona_info, list_personas
from app.core.router import NoProvidersError
from app.api.security import (BodySizeLimit, LOOPBACK_HOSTS, LOOPBACK_ORIGIN_RE,
                              LocalOriginGuard, PersonaIdPath, PersonaIdQuery)
from app.api.schemas import (
    ActiveProviderRequest,
    CalendarEntryCreate,
    CalendarEntryUpdate,
    ChatRequest,
    ChatResponse,
    ClearChatRequest,
    FactRequest,
    FactUpdateRequest,
    HistoryMessage,
    InitiativeUpdate,
    InventoryAddRequest,
    LearningStartRequest,
    LocationRequest,
    TimezoneRequest,
    MemoryStats,
    PersonaConfigUpdate,
    PersonaCreateRequest,
    PersonaDraftSave,
    PersonaInfo,
    PersonaAvatarUpdate,
    PersonaColorUpdate,
    PersonaRenameRequest,
    PersonaYamlUpdate,
    PresenceRequest,
    ProviderKeyRequest,
    ProviderModelRequest,
    PersonaLocalTasksUpdate,
    ReminderAddRequest,
    ReminderUpdateRequest,
    RoomArtUpdate,
    RoomFocusRequest,
    RoomLayoutUpdate,
    RoomPokeRequest,
    RoomStyleDescribeRequest,
    RoomStyleUpdate,
    StmDeleteRequest,
    StmTrimRequest,
    TodoAddRequest,
    WebchatRequest,
)
from app.core.file_reader import extract_text
from app.core.language import detect_language
from app.core.message_pacing import send_delay
from app.core.config import Config
from app.core.paths import data_dir
from app.core.presence import web_context, web_presence
from app.core import timeutil
from app.features.calendar_manager import get_calendar

logger = logging.getLogger(__name__)

# Префиксы, которыми extract_text сообщает об ошибке
_EXTRACT_ERROR_PREFIXES = ("Ошибка", "Формат", "Не удалось", "Библиотека")

app = FastAPI(title="Virtual Persona API", version="1.0")


@app.exception_handler(NoProvidersError)
async def _no_providers(request, exc: NoProvidersError):
    # Бот персоны не создаётся без единого источника ответов. Чистая установка:
    # веб открывается, а в чат приходит понятная причина вместо 500
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.on_event("startup")
def _start_warmup():
    # Фоновый подъём ботов персон, с которыми пользователь недавно общался
    # (история и настройки готовы до захода в чат) — см. app/api/warmup.py
    from app.api import warmup
    warmup.start_warmup()


@app.on_event("shutdown")
def _shutdown_bot_browser():
    # Гасим браузер бота при остановке API — иначе он переживает выход,
    # держа окно и вкладки веб-чатов (фоновая нагрузка на систему)
    try:
        from app.features import browser_actions as ba
        ba.shutdown_browser(reason="остановка API")
    except Exception:
        pass

# Буфер логов для режима разработчика (GET /api/logs)
from app.api import log_buffer
log_buffer.install()
# Маска секретов логов (и в буфере панели): после log_buffer — фильтр
# ложится и на его хендлер; запуск uvicorn напрямую тоже покрыт
from app.core import log_privacy  # noqa: E402
log_privacy.install()

# Лимит тела для маршрутов с многомегабайтными HTML-файлами скинов: 413 до
# разбора JSON (security.BodySizeLimit). Добавлен раньше CORS — значит, внутри
# него, и отказ тоже уходит с CORS-заголовками
from app.api import skin_gen_api as _skin_gen_limits, skins_api as _skins_limits  # noqa: E402
app.add_middleware(BodySizeLimit, rules=[
    ("POST", r"/api/skins/generate", _skin_gen_limits.MAX_BODY_BYTES),
    ("POST", r"/api/skins/direction", _skin_gen_limits.DIRECTION_MAX_BODY_BYTES),
    ("POST", r"/api/skins", _skins_limits.MAX_BODY_BYTES),
    ("PUT", r"/api/skins/[^/]+", _skins_limits.MAX_BODY_BYTES),
    # Комната: спрайты/фон/картинки предметов — data-URL до 2 МБ каждый
    ("PUT", r"/api/personas/[^/]+/room/art", 40 * 1024 * 1024),
    ("PUT", r"/api/personas/[^/]+/room/layout", 20 * 1024 * 1024),
    ("PUT", r"/api/personas/[^/]+/room/style", 2 * 1024 * 1024),
    ("POST", r"/api/personas/[^/]+/room/style/describe", 2 * 1024 * 1024),
])

# Источники: API_CORS_ORIGINS — явный список ("*" — любой сайт); не задано
# или пусто — только свой фронт на этой машине: localhost/127.0.0.1/[::1] на
# любом порту (vite dev 5173, preview 4173 и др.)
_cors_origins = [o.strip() for o in os.getenv("API_CORS_ORIGINS", "").split(",") if o.strip()]
_cors_regex = None if _cors_origins else LOOPBACK_ORIGIN_RE
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=_cors_regex,
    allow_methods=["*"],
    allow_headers=["*"],
)

_bearer = HTTPBearer(auto_error=False)
_api_token = os.getenv("API_TOKEN", "")
_api_host = os.getenv("API_HOST", "127.0.0.1")

# Origin и Host (security.LocalOriginGuard) — снаружи CORS: чужая страница
# получает отказ раньше, чем её запрос исполнится. Host: при привязке к
# loopback — локальные имена и API_ALLOWED_HOSTS; при привязке наружу без
# API_ALLOWED_HOSTS имя хоста заранее неизвестно — не проверяется
_extra_hosts = {h.strip().lower() for h in os.getenv("API_ALLOWED_HOSTS", "").split(",") if h.strip()}
if _api_host in LOOPBACK_HOSTS or _extra_hosts:
    _allowed_hosts = set(LOOPBACK_HOSTS) | _extra_hosts
else:
    _allowed_hosts = None
app.add_middleware(LocalOriginGuard, origins=_cors_origins, origin_regex=_cors_regex,
                   hosts=_allowed_hosts)

# Не-loopback хост без токена — API открыт всем в локальной сети/интернете
# без единой проверки: не роняем процесс (это может быть осознанный выбор
# в доверенном окружении), но громко предупреждаем в лог при старте.
if _api_host not in ("127.0.0.1", "localhost", "::1") and not _api_token:
    logging.getLogger(__name__).warning(
        f"[Security] API_HOST={_api_host!r} (не loopback), а API_TOKEN не задан — "
        "все /api/* эндпоинты доступны без авторизации всем, кто достучится до "
        "этого хоста. Задайте API_TOKEN в .env, если сервер смотрит наружу."
    )

# Чаты, где прямо сейчас идёт генерация ответа: "persona:chat_key" → число
# активных/ожидающих генераций. Inbox отдаёт это фронту — индикатор
# «печатает» переживает перезагрузку страницы (запрос-то на сервере
# продолжается). Счётчик, а не множество: два запроса в один чат (второй ждёт
# лок) не должны гасить флаг друг другу — первый завершившийся снимал бы его,
# пока второй ещё в работе.
_generating: dict[str, int] = {}
# Сильные ссылки на фоновые задачи генерации: event loop держит задачи только
# слабо, «осиротевшую» после разрыва соединения задачу мог бы собрать GC
_detached_generations: set[asyncio.Task] = set()


def _generating_enter(key: str) -> None:
    _generating[key] = _generating.get(key, 0) + 1


def _generating_exit(key: str) -> None:
    n = _generating.get(key, 0) - 1
    if n > 0:
        _generating[key] = n
    else:
        _generating.pop(key, None)


def _log_detached_failure(task: asyncio.Task) -> None:
    # Ошибка задачи, которую уже никто не ждёт (клиент отключился), — в лог,
    # а не в "Task exception was never retrieved"
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("[chat] фоновая генерация завершилась ошибкой", exc_info=exc)


def _cc_turn_enter(bot, req):
    """Режим управления, до лока чата: «стоп» при идущем ходе, реплика
    занятому агенту, дубль ещё идущей команды — ответ сразу (см.
    BotInstance.cc_turn_enter). → (reply | None, token)."""
    enter = getattr(bot, "cc_turn_enter", None)
    # Реплика из скина в режим управления не попадает вовсе (даже «стоп»
    # и «ещё работаю») — см. ChatRequest.from_skin
    if not callable(enter) or req.image or req.from_skin:
        return None, None
    try:
        return enter(req.message, req.user_id, req.chat_id)
    except Exception as e:
        logger.debug(f"[chat] cc_turn_enter: {e}")
        return None, None


def _cc_turn_exit(bot, token) -> None:
    exit_ = getattr(bot, "cc_turn_exit", None)
    if token and callable(exit_):
        try:
            exit_(token)
        except Exception:
            pass


async def _run_generation(gen_key: str, fn, on_done=None):
    """Генерация ответа, которую не отменяет разрыв соединения клиента.

    fn — синхронная тяжёлая часть (process_message), идёт в потоке пула;
    поток нельзя отменить, поэтому флаг «генерирует» и лок чата держит
    отдельная задача и снимает их, только когда fn реально вернулся. Вызывающий
    ждёт её через shield: разрыв соединения (CancelledError) саму задачу не
    трогает, поток дописывает ответ в STM, а флаг и лок остаются выставленными
    до этого момента — иначе следующее сообщение в тот же чат могло бы уйти
    параллельно с ещё живым потоком.

    Флаг ставится до ожидания лока: сообщение, вставшее в очередь за
    предыдущим, тоже показывает «печатает». on_done — когда поток реально
    закончил (снять регистрацию идущей команды режима управления)."""
    async def _job():
        _generating_enter(gen_key)
        try:
            async with chat_lock(gen_key):
                return await asyncio.to_thread(fn)
        finally:
            _generating_exit(gen_key)
            if on_done is not None:
                on_done()

    task = asyncio.create_task(_job())
    _detached_generations.add(task)
    task.add_done_callback(_detached_generations.discard)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Клиент ушёл: задача доработает сама, ответ ляжет в STM (фронт
        # перечитает историю по спаду флага / росту last_ts). Её ошибку —
        # в лог, раз результат уже никому не нужен.
        task.add_done_callback(_log_detached_failure)
        logger.info(f"[chat] {gen_key}: клиент отключился, генерация продолжается в фоне")
        raise


async def require_auth(credentials: HTTPAuthorizationCredentials = Depends(_bearer)):
    # Bearer-авторизация активна только когда задан API_TOKEN
    if not _api_token:
        return
    # compare_digest вместо != — токен не сравнивается char-by-char с ранним
    # выходом на первом несовпадении, что убирает timing-канал подбора токена
    if credentials is None or not hmac.compare_digest(credentials.credentials, _api_token):
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий токен")


async def _touch_persona(persona: str):
    # Метка «пользователь заходил к персоне» — по ней warmup решает, кого
    # поднимать при следующем старте API
    from app.api import warmup
    await asyncio.to_thread(warmup.touch, persona)


async def _get_bot(persona: str):
    # Бот персоны или 404; создание инстанса блокирующее — выполняем в потоке
    bot = await asyncio.to_thread(runtime.registry.get, persona)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    return bot


def _check_not_muted(bot, persona: str):
    # Замороженная персона (features.muted) не отвечает: 409, сообщение не
    # пишется в STM — персона полностью выключена
    if (bot.features or {}).get("muted"):
        raise HTTPException(status_code=409,
                            detail=f"Персона '{persona}' заморожена и молчит, пока её не разморозят")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/logs", dependencies=[Depends(require_auth)])
async def get_logs(since: int = 0, limit: int = 500):
    # Инкрементальная лента логов ядра (режим разработчика в веб-UI)
    return log_buffer.since(since, limit)


@app.get("/api/system/memory", dependencies=[Depends(require_auth)])
async def system_memory():
    # Загрузка RAM/свопа хоста — виджет режима разработчика;
    # без psutil — ok=false, виджет прячется
    try:
        import psutil
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        gb = 1024 ** 3
        return {
            "ok": True,
            "mem_percent": round(vm.percent, 1),
            "mem_used_gb": round((vm.total - vm.available) / gb, 1),
            "mem_total_gb": round(vm.total / gb, 1),
            "mem_available_gb": round(vm.available / gb, 2),
            "swap_used_gb": round(sm.used / gb, 1),
            "swap_total_gb": round(sm.total / gb, 1),
        }
    except Exception:
        return {"ok": False}


# ── Панель на рабочем столе (desktop/) ────────────────────────────────
# Панель следит за бэкендом снаружи: живость — /api/health, остальное —
# здесь. Браузеры бота и карантины веб-чатов общие для процесса — в inbox
# они те же, но inbox требует персону.

@app.get("/api/system/status", dependencies=[Depends(require_auth)])
async def system_status():
    # pool_status пробует CDP пула H по сети (до 2 с) — не в event loop
    try:
        from app.features import browser_actions as _ba
        pools = await asyncio.to_thread(_ba.pool_status)
    except Exception:
        pools = {}
    try:
        from app.features import web_llm as _wl
        quarantine = _wl.quarantine_status()
    except Exception:
        quarantine = {}
    return {"pid": os.getpid(), "browser_pools": pools,
            "webchat_quarantine": quarantine}


@app.post("/api/system/shutdown", dependencies=[Depends(require_auth)])
async def system_shutdown():
    # Мягкая остановка: тот же путь, что Ctrl+C/kill — uvicorn ловит SIGTERM,
    # гасит сервер и зовёт shutdown-хуки (браузер бота). Сигнал — после
    # ответа, иначе панель не узнала бы, что запрос принят. На Windows мягко
    # иначе не остановить: процесс без консоли не получает Ctrl+C/Ctrl+Break
    import signal
    logger.info("[API] Остановка по запросу панели")
    asyncio.get_running_loop().call_later(0.3, signal.raise_signal, signal.SIGTERM)
    return {"ok": True, "pid": os.getpid()}


@app.post("/api/browser/rescue", dependencies=[Depends(require_auth)])
async def browser_rescue():
    # «Показать браузер бота»: пул H — видимым (вход в веб-чаты, капча), как
    # по реплике «почини браузер». Перезапуск Chrome блокирующий
    from app.features import browser_actions as _ba
    ok = await asyncio.to_thread(_ba.rescue_pool_h)
    return {"ok": bool(ok)}


@app.post("/api/browser/rescue/finish", dependencies=[Depends(require_auth)])
async def browser_rescue_finish():
    # «Готово»: правило то же, что у реплики после «почини браузер» — rescue
    # кончается, если капч/входов не ждёт ни один процесс бота
    from app.features import web_llm as _wl
    finished = await asyncio.to_thread(_wl.finish_idle_rescue)
    return {"finished": bool(finished)}


@app.get("/api/personas", response_model=list[PersonaInfo],
         dependencies=[Depends(require_auth)])
async def personas():
    return [get_persona_info(name) for name in list_personas()]


def _memory_conflict(result: dict) -> JSONResponse:
    # 409 «под id осталась память»: detail строкой (как у HTTPException) +
    # флаги для выбора в вебе — подхватить / в архив / отмена
    return JSONResponse(status_code=409, content={
        "detail": result["detail"], "memory_exists": True,
        "persona": result.get("persona"), "can_keep": result.get("can_keep", True),
    })


@app.post("/api/personas", dependencies=[Depends(require_auth)])
async def persona_create(req: PersonaCreateRequest):
    # Создать новую персону из YAML (имя файла = поле id из YAML). Под id
    # осталась память — 409 с memory_exists, пока не выбрано memory: keep/fresh
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.create_persona, req.yaml, req.memory)
    if not result["ok"]:
        if result.get("memory_exists"):
            return _memory_conflict(result)
        status = result.get("status") or (409 if result.get("conflict") else 400)
        raise HTTPException(status_code=status, detail=result["detail"])
    return result


@app.delete("/api/personas/{persona}", dependencies=[Depends(require_auth)])
async def persona_delete(persona: PersonaIdPath):
    # Удалить персону: YAML-файл + выгрузка из реестра; память остаётся на диске.
    # Своя копия встроенной персоны удаляется — остаётся встроенная (reset);
    # встроенную без своей копии удалить нельзя — 409
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.delete_persona, persona)
    if not result["ok"]:
        raise HTTPException(status_code=result.get("status", 404), detail=result["detail"])
    return {"status": "ok", "reset": result["reset"]}


@app.post("/api/personas/{persona}/duplicate", dependencies=[Depends(require_auth)])
async def persona_duplicate(persona: PersonaIdPath):
    # Копия YAML персоны с новым id/name ({id}_copy, имя + «(копия)»)
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.duplicate_persona, persona)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    return result


@app.post("/api/personas/{persona}/rename", dependencies=[Depends(require_auth)])
async def persona_rename(persona: PersonaIdPath, req: PersonaRenameRequest):
    # Смена id: YAML-файл, папки памяти (с аватаром), календарь, токен бота
    if any(key.startswith(f"{persona}:") for key in _generating):
        raise HTTPException(status_code=409, detail="Персона сейчас отвечает — дождитесь конца ответа")
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.rename_persona, persona, req.new_id, req.memory)
    if not result["ok"]:
        if result.get("memory_exists"):
            return _memory_conflict(result)
        raise HTTPException(status_code=result.get("status", 400), detail=result["detail"])
    return result


@app.put("/api/personas/{persona}/color", dependencies=[Depends(require_auth)])
async def persona_color_update(persona: PersonaIdPath, req: PersonaColorUpdate):
    # Цвет метки персоны: строка color: в YAML (карточка, календарь, главная)
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.set_persona_color, persona, req.color)
    if not result["ok"]:
        raise HTTPException(status_code=result.get("status", 400), detail=result["detail"])
    return result


# ── Главная страница веба: сводка по всем персонам без подъёма ботов ──


@app.get("/api/home", dependencies=[Depends(require_auth)])
async def home_overview(chat_id: str = "web_user"):
    # Состояние персон, лента «пока вас не было» и телеметрия — из файлов
    # персон (см. app/api/home_api.py); BotInstance не создаётся
    from app.api import home_api

    def _build():
        infos = {p: get_persona_info(p) or {} for p in list_personas()}
        return home_api.home_overview(infos, chat_id)

    return await asyncio.to_thread(_build)


# ── Аватары персон: файл в data/api_<id>/, общий для всех браузеров ──


@app.get("/api/persona-avatars", dependencies=[Depends(require_auth)])
async def persona_avatars():
    # {id: data-URL} всех персон с аватаром — одним запросом при загрузке UI
    from app.api import avatars_api
    return {"avatars": await asyncio.to_thread(avatars_api.list_avatars, list_personas())}


@app.put("/api/personas/{persona}/avatar", dependencies=[Depends(require_auth)])
async def persona_avatar_set(persona: PersonaIdPath, req: PersonaAvatarUpdate):
    if persona not in list_personas():
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    from app.api import avatars_api
    result = await asyncio.to_thread(avatars_api.set_avatar, persona, req.data_url)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


@app.delete("/api/personas/{persona}/avatar", dependencies=[Depends(require_auth)])
async def persona_avatar_delete(persona: PersonaIdPath):
    if persona not in list_personas():
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    from app.api import avatars_api
    await asyncio.to_thread(avatars_api.delete_avatar, persona)
    return {"ok": True}


# ── Библиотека скинов: data/skins/, общая для всех браузеров ──

from app.api.skins_api import SkinAssign, SkinCreate, SkinUpdate  # noqa: E402


async def _skins_call(fn, *args):
    # Операция библиотеки скинов в потоке; SkinError → HTTP-код с текстом
    from app.api import skins_api
    try:
        return await asyncio.to_thread(fn, *args)
    except skins_api.SkinError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@app.get("/api/skins", dependencies=[Depends(require_auth)])
async def skins_list():
    # Метаданные всех скинов (без HTML — файлы грузятся по одному скину)
    from app.api import skins_api
    return {
        "skins": await _skins_call(skins_api.list_skins),
        "hidden_builtins": await _skins_call(skins_api.hidden_builtins),
    }


@app.post("/api/skins/builtins/restore", dependencies=[Depends(require_auth)])
async def skins_restore_builtins():
    # Вернуть в библиотеку скрытые («удалённые») встроенные скины
    from app.api import skins_api
    return {"restored": await _skins_call(skins_api.restore_builtins)}


@app.get("/api/skins/{skin_id}", dependencies=[Depends(require_auth)])
async def skin_get(skin_id: PersonaIdPath):
    from app.api import skins_api
    return await _skins_call(skins_api.get_skin, skin_id)


@app.post("/api/skins", dependencies=[Depends(require_auth)])
async def skin_create(req: SkinCreate):
    from app.api import skins_api
    return {"skin": await _skins_call(skins_api.create_skin, req)}


@app.put("/api/skins/{skin_id}", dependencies=[Depends(require_auth)])
async def skin_update(skin_id: PersonaIdPath, req: SkinUpdate):
    from app.api import skins_api
    return {"skin": await _skins_call(skins_api.update_skin, skin_id, req)}


@app.delete("/api/skins/{skin_id}", dependencies=[Depends(require_auth)])
async def skin_delete(skin_id: PersonaIdPath):
    # Удаление снимает скин со всех персон, которым он был назначен
    from app.api import skins_api
    return {"ok": True, "unassigned": await _skins_call(skins_api.delete_skin, skin_id)}


@app.get("/api/skin-assignments", dependencies=[Depends(require_auth)])
async def skin_assignments():
    from app.api import skins_api
    return {"assignments": await _skins_call(skins_api.get_assignments)}


@app.put("/api/personas/{persona}/skin", dependencies=[Depends(require_auth)])
async def persona_skin_set(persona: PersonaIdPath, req: SkinAssign):
    # skin_id=null — снять скин (персона показывается дефолтным UI)
    if persona not in list_personas():
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    from app.api import skins_api
    await _skins_call(skins_api.assign_skin, persona, req.skin_id)
    return {"ok": True}


# ── Генерация скина нейросетью (панель скинов, SkinGenerator) ──

from app.api.skin_gen_api import SkinDirectionRequest, SkinGenRequest  # noqa: E402


@app.post("/api/skins/direction", dependencies=[Depends(require_auth)])
async def skin_direction(req: SkinDirectionRequest):
    """Арт-направления для генерации скина: {"directions": [...], "provider",
    "model"} — 1–3 варианта (сюжет, палитра, шрифты, раскладка, фирменный
    элемент…). Выбранное клиент шлёт с каждым запросом /api/skins/generate."""
    from app.api import skin_gen_api
    try:
        return await asyncio.to_thread(skin_gen_api.propose_directions, req)
    except skin_gen_api.SkinGenError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@app.post("/api/skins/generate", dependencies=[Depends(require_auth)])
async def skin_generate(req: SkinGenRequest):
    """SSE: {"status"} (у "continue" — ещё "round", у "direction" — подобранное
    сервером направление, если клиент его не прислал), {"progress", "elapsed"},
    финал {"done", "html", "truncated", "provider", "model"} или {"error"}.
    Проверка и автоисправление — на клиенте."""
    from app.api import skin_gen_api
    try:
        messages, assets = skin_gen_api.prepare(req)
    except skin_gen_api.SkinGenError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return StreamingResponse(skin_gen_api.generate_events(req, messages, assets),
                             media_type="text/event-stream")


@app.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(require_auth)])
async def chat(req: ChatRequest):
    bot = await _get_bot(req.persona)
    _check_not_muted(bot, req.persona)
    await _touch_persona(req.persona)
    # Лок на (персона, чат): сериализуем сообщения одного чата,
    # разные чаты и персоны обрабатываются параллельно.
    lock_key = f"{req.persona}:{req.chat_id or req.user_id}"
    stm_key = _stm_key(req)
    # До лока чата: «стоп»/дубль/занятый агент в режиме управления
    early, cc_token = _cc_turn_enter(bot, req)
    if early is not None:
        return ChatResponse(
            reply=early, extra_messages=[], question_kind=None,
            persona=req.persona, chat_id=req.chat_id or req.user_id,
            provider=None, model=None,
            control_mode=bot.control_mode_on(req.chat_id or req.user_id),
            images=[])
    # Ход пользователя — от получения сообщения до отдачи ответа (см.
    # _begin_turn): фоновые сообщения чата в это окно в STM не встают
    try:
        frame = await _begin_turn(bot, stm_key)
    except BaseException:
        _cc_turn_exit(bot, cc_token)
        raise
    try:
        return await _chat_in_turn(bot, req, lock_key, stm_key, frame, cc_token)
    finally:
        _end_turn(bot, frame)


async def _chat_in_turn(bot, req: ChatRequest, lock_key: str, stm_key: str, frame,
                        cc_token=None):
    def _generate():
        """Синхронная часть целиком (в потоке пула, под локом чата): и
        генерация, и разбор pending-бакетов — они должны сниматься тем же
        запросом, что их наполнил, до следующего сообщения в чат."""
        # Поток генерации держит ход сам (_adopt_turn): при разрыве
        # соединения обработчик уходит, а генерация и правка STM картинки
        # ещё идут — ход закроется, только когда выйдет и она
        with _adopt_turn(bot, frame):
            cmd = _try_slash_command(bot, req)
            if cmd is not None:
                # Слэш-команда: без process_message (как в TG — отдельные хендлеры)
                reply, llm_used = cmd
                provider, model = _answer_provider(bot) if llm_used else (None, None)
                split_extra = bot.pop_pending_split_messages(req.chat_id)
            else:
                llm_input = (
                    _prepare_image_input(bot, req.message, req.image,
                                         req.chat_id, req.user_id)
                    if req.image else req.message
                )
                reply = bot.process_message(
                    llm_input,
                    user_id=req.user_id,
                    chat_id=req.chat_id,
                    user_name=req.user_name,
                    reply_context=req.reply_context,
                    # Подтверждение pending-действия (computer_control) — только
                    # по тому, что реально ввёл пользователь, а не по OCR внутри
                    # llm_input (там текст с картинки — см. _prepare_image_input)
                    raw_user_text=req.message if req.image else None,
                    from_skin=req.from_skin,
                )
                # Провайдер ответа — СРАЗУ после process_message и в этом же
                # потоке (см. _answer_provider)
                provider, model = _answer_provider(bot)
                # Хвост расщеплённого ответа (settings.split_messages) забираем
                # до правки STM картинки — переписываемый хвост включает его части
                split_extra = bot.pop_pending_split_messages(req.chat_id)
                if req.image:
                    cap = req.message.strip()
                    _rewrite_image_stm(
                        bot, stm_key, req.user_id,
                        f"📷 {cap}" if cap else "📷 (изображение)", [reply] + split_extra,
                    )
        # Части расщеплённого ответа идут раньше досылаемых списков:
        # это продолжение реплики, а список — приложение к ней
        extra = split_extra + bot.pop_pending_list_messages(req.chat_id)
        question_kind = bot.pop_pending_question_kind(req.chat_id)
        return reply, provider, model, extra, question_kind

    # Лок на (персона, чат) и флаг «генерирует» держит _run_generation:
    # сообщения одного чата сериализуются, разные чаты и персоны идут
    # параллельно; разрыв соединения генерацию не обрывает
    reply, provider, model, extra, question_kind = await _run_generation(
        lock_key, _generate, on_done=lambda: _cc_turn_exit(bot, cc_token))
    return ChatResponse(
        reply=reply,
        extra_messages=extra,
        question_kind=question_kind,
        persona=req.persona,
        chat_id=req.chat_id or req.user_id,
        provider=provider,
        model=model,
        # Может измениться самим этим сообщением («перейди в режим управления»)
        control_mode=bot.control_mode_on(req.chat_id or req.user_id),
        # Скриншоты страницы из режима управления («что на странице?»)
        images=_pending_images(bot, req.chat_id),
    )


def _pending_images(bot, chat_id) -> list[str]:
    # Скриншоты страницы из режима управления (pending-фото бакет бота) —
    # в dataURL для поля images ответа
    out = []
    for ph in bot.pop_pending_photos(chat_id):
        data = ph.get("data")
        if data:
            out.append("data:image/jpeg;base64,"
                       + base64.b64encode(data).decode("ascii"))
    return out


def _stm_key(req: ChatRequest) -> str:
    """Ключ чата в STM — то же правило, что у BotInstance/MemoryManager
    (chat_id, иначе user_id; пустой chat_id — свой ключ ""): ход и правка
    STM картинки ключуются так же, как реально пишется история."""
    return str(req.chat_id if req.chat_id is not None else req.user_id)


async def _begin_turn(bot, chat_key):
    """Открыть ход пользователя (BotInstance — гейт против фоновых
    инициатив/ритма/напоминаний, app/core/turn_gate.py) при получении
    сообщения. Заготовки бота без гейта (тесты) — None."""
    begin = getattr(bot, "begin_user_turn_async", None)
    return await begin(chat_key) if callable(begin) else None


def _end_turn(bot, frame) -> None:
    if frame is not None:
        bot.end_user_turn(frame)


def _adopt_turn(bot, frame):
    # Присоединить рабочий поток генерации к открытому ходу (кадр передаётся
    # явно: SSE-генератор не должен трогать contextvars)
    adopt = getattr(bot, "adopt_turn", None)
    if frame is None or not callable(adopt):
        return contextlib.nullcontext()
    return adopt(frame)


def _answer_provider(bot) -> tuple[str | None, str | None]:
    """Провайдер и модель, реально давшие ответ (с учётом fallback-цепочки
    и персональных override модели).

    Звать в том же потоке, что генерировал ответ, и сразу после
    process_message: router._last_provider — потоко-локальное поле, его пишут
    все потоки персоны (ответ, инициатива, досье), и из другого потока или
    event loop оно было бы пустым или чужим. getattr с дефолтом — для
    роутеров без этого поля (заглушки в тестах)."""
    router = bot.router
    pid = getattr(router, "_last_provider", None) or router.active_provider
    if not pid:
        return None, None
    if pid == "local":
        from app.core.config import OLLAMA_MODEL
        model = getattr(router, "_last_local_model", None) or OLLAMA_MODEL
    else:
        model = router.model_for(pid) if hasattr(router, "model_for") else (router.available.get(pid) or {}).get("model", "")
    return pid, model or None


def _decode_image(data: str) -> bytes:
    # base64 или dataURL ("data:image/...;base64,...") → байты изображения
    import base64
    try:
        if data.startswith("data:"):
            _, _, data = data.partition(",")
        return base64.b64decode(data)
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректные данные изображения (base64)")


def _prepare_image_input(bot, message: str, image_b64: str,
                         chat_id: str = None, user_id: str = None) -> str:
    # Текст для LLM из сообщения с картинкой: подпись + содержимое по
    # vision-модели. Каскад как у TG-бота: vision-провайдер основного роутера
    # → локальная LLM (OCR)
    image_bytes = _decode_image(image_b64)
    question = message.strip()
    # Язык описания — язык подписи, без неё — язык пользователя в чате
    lang = detect_language(question)
    if not lang and (chat_id or user_id):
        try:
            lang = bot.chat_user_language(bot.stm_key(chat_id, user_id))
        except Exception:
            lang = None
    ocr = bot.describe_image(image_bytes, question, lang)
    if not ocr and getattr(bot, "_local_router", None) and bot._local_router.is_available():
        ocr = bot._local_router.ocr_image(image_bytes, question, lang)
    if not ocr:
        raise HTTPException(status_code=503, detail="Ни одна vision-модель недоступна — не могу посмотреть картинку")
    return (f"{question}\n\n" if question else "") + (
        "The user sent an image. Its contents according to the vision model:\n" + ocr
    )


def _rewrite_image_stm(bot, chat_key: str, user_id: str, display_text: str, reply_parts: list):
    """В STM попал служебный vision-текст (в TG это норма — история внутренняя),
    но веб-чат показывает STM пользователю: заменяем пару «синтетика + ответ»
    на читабельные «📷 подпись» и части ответа бота (при split_messages ответ
    лежит в STM несколькими сообщениями — переписываем весь хвост целиком)."""
    try:
        # Хвост STM: [синтетическое user-сообщение, часть1, ..., частьN].
        # Точная граница — записи ЭТОГО хода (якорь хода у BotInstance): без
        # него пришлось бы считать по завершающей серии assistant-сообщений,
        # а это ловит как часть ответа инициативу, успевшую лечь в STM после
        # него (хотя пользователь её уже видел), а если ход не записал
        # user-реплику — сносит инициативу перед ходом и сообщение до неё.
        # Запрос держит ход пользователя до конца правки (_user_turn), так что
        # фоновых записей внутри хвоста нет.
        tail_fn = getattr(bot, "_turn_stm_tail", None)
        tail = tail_fn(chat_key) if callable(tail_fn) else None
        if tail is not None:
            n_pop = len(tail)
        else:
            # Якоря нет (заготовка бота / буфер перечитан) — эвристика:
            # завершающая серия assistant-частей и user-сообщение перед ней
            msgs = bot.memory.stm.get_messages(chat_id=chat_key)
            n_assist = 0
            for m in reversed(msgs):
                if m.get("role") != "assistant":
                    break
                n_assist += 1
            n_pop = min(n_assist + 1, len(msgs))
        bot.memory.stm.pop_last_n(n_pop, chat_key)
        # Через маски хода режима управления (секрет ввода, текст приватной
        # страницы): прямой stm.add_message вернул бы их в историю открытыми
        add = getattr(bot, "stm_add_message", None)
        if not callable(add):
            add = bot.memory.stm.add_message
        add("user", display_text, user_id, chat_key)
        for part in reply_parts:
            add("assistant", part, user_id, chat_key)
    except Exception as e:
        logger.warning(f"Не удалось переписать STM для изображения: {e}")


# Слэш-команды веб-чата (зеркало TG): /learn, /remind, /add_todo,
# /add_inventory — через общий ядровой _dispatch_command (создаёт сущность
# и отвечает в образе персоны, пишет пару в STM); /web, /todo, /reminders,
# /cancel_reminder, /inventory, /files, /help — утилитарные, без LLM.
_SLASH_DISPATCH = {
    "learn": "learn",
    "stop_learning": "stop_learning",
    "remind": "remind",
    "add_todo": "todo",
    "add_inventory": "inventory",
}

_SLASH_HELP = (
    "Команды:\n"
    "/learn <тема> — начать обучение\n"
    "/stop_learning [тема] — остановить курс\n"
    "/remind <что> [через N / в HH:MM] — напомнить\n"
    "/web — вкл/выкл веб-поиск\n"
    "/todo — список дел · /add_todo <задача>\n"
    "/reminders — активные напоминания · /cancel_reminder N\n"
    "/inventory — инвентарь · /add_inventory <предмет>[: описание]\n"
    "/files — загруженные файлы\n"
    "/help — этот список"
)


def _try_slash_command(bot, req: ChatRequest) -> tuple[str, bool] | None:
    # Перехват слэш-команды: возвращает (ответ, был_ли_llm) или None, если
    # сообщение не команда
    text = req.message.strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split(None, 1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    chat_id = req.chat_id or req.user_id
    user_name = req.user_name or "User"
    logger.info(f"[Slash] /{cmd} {args[:60]} (persona={req.persona}, chat={chat_id})")

    if cmd == "help":
        return _SLASH_HELP, False

    if cmd == "web":
        enabled = bot.toggle_web_search(chat_id)
        return ("Веб-поиск включён." if enabled else "Веб-поиск выключен."), False

    if cmd == "todo":
        if args:
            return _try_slash_command(
                bot, req.model_copy(update={"message": f"/add_todo {args}"})
            )
        if not bot.todo_manager:
            return "Список дел не активен для этой персоны.", False
        lang = bot.chat_user_language(chat_id)
        empty = "The todo list is empty." if lang == "en" else "Список дел пуст."
        return (bot.todo_manager.get_list(chat_id, lang=lang) or empty), False

    if cmd == "reminders":
        if not bot.reminder_manager:
            return "Напоминания не активны для этой персоны.", False
        active = bot.reminder_manager.get_active(chat_id)
        if not active:
            return "Активных напоминаний нет.", False
        from app.features.reminder_manager import format_reminder_when
        lines = ["Активные напоминания:"]
        for i, r in enumerate(active):
            task = r.get("task") or "(без описания)"
            when = format_reminder_when(r)
            lines.append(f"{i + 1}. {task} — {when} [id {r.get('id')}]")
        lines += ["", "Чтобы отменить: /cancel_reminder N (или id)"]
        return "\n".join(lines), False

    if cmd == "cancel_reminder":
        if not bot.reminder_manager:
            return "Напоминания не активны для этой персоны.", False
        from app.features.reminder_manager import parse_reminder_ref
        ref = parse_reminder_ref(args)
        if ref is None:
            return "Нужен номер или id напоминания из /reminders.", False
        removed = bot.reminder_manager.cancel_by_ref(chat_id, ref[1])
        if removed is None:
            return "Напоминание с таким номером/id не найдено.", False
        task = removed.get("task") or "(без описания)"
        return f"Напоминание «{task}» (id {removed.get('id')}) отменено.", False

    if cmd == "inventory":
        if args:
            return _try_slash_command(
                bot, req.model_copy(update={"message": f"/add_inventory {args}"})
            )
        if not bot.inventory_manager:
            return "Инвентарь не активен для этой персоны.", False
        return bot.inventory_manager.get_list_text(), False

    if cmd == "files":
        # Как /files в TG: файлы собеседника (в вебе — req.user_id, тот же
        # ключ, что у загрузки через /api/personas/{p}/files)
        if not getattr(bot, "file_db", None):
            return "Загрузка файлов не активна для этой персоны.", False
        files = sorted(bot.file_db.get_loaded_files(req.user_id))
        if not files:
            return "Нет загруженных файлов.", False
        return "Загруженные файлы:\n" + "\n".join(f"- {f}" for f in files), False

    kind = _SLASH_DISPATCH.get(cmd)
    if kind:
        reply = bot._dispatch_command(kind, args, chat_id, req.user_id, user_name)
        # Сущность уже создана диспетчером напрямую — маркеры ([TODO_ADD:...]
        # и т.п.) в тексте ответа — мусор для отображения, обрезаем
        reply = re.sub(
            r"\[(?:TODO_ADD|TODO_DONE|INVENTORY_ADD|INVENTORY_REMOVE)[^\]]*\]", "", reply
        ).strip()
        return reply, True

    return "Неизвестная команда. Список: /help", False


def _reply_stm_ts(bot, stm_key: str, reply: str):
    """Серверная метка ответа хода — timestamp его записи в STM (та же, что
    отдаст /api/chat/history). Фронт ставит по ней пузырь ответа: иначе он
    вставал в ленту по моменту отправки вопроса, выше промежуточных сообщений
    хода из inbox («Нажал …» режима управления). Хода/якоря нет — None
    (фронт — по-старому); текста ответа в STM нет — «сейчас» (после всех
    сообщений хода; сверки текста с историей у такого пузыря и так нет)."""
    try:
        tail = bot._turn_stm_tail(stm_key)
    except Exception:
        return None
    if tail is None:
        return None
    hit = next((m for m in reversed(tail) if m.get("role") == "assistant"
                and m.get("content") == reply), None)
    ts = (hit or {}).get("timestamp")
    return float(ts) if isinstance(ts, (int, float)) else time.time()


async def _typed_chunks(text: str):
    """«Печать» порциями по 6 символов — чистый asyncio.sleep в event loop'е,
    без потока пула: time.sleep() внутри asyncio.to_thread держал бы воркер
    пула все секунды анимации, и под нагрузкой параллельные /api/chat/stream
    исчерпывали бы общий пул, а другие эндпоинты зависали бы в очереди на
    поток. Вынесена на уровень модуля — тестируется независимо от bot/req,
    см. scripts/test_api_security.py."""
    for i in range(0, len(text), 6):
        yield {"token": text[i:i + 6]}
        await asyncio.sleep(0.02)


@app.post("/api/chat/stream", dependencies=[Depends(require_auth)])
async def chat_stream(req: ChatRequest):
    """SSE-стриминг ответа: события {"token": ...}, финал {"done": ..., "reply": ...}.

    Ядро генерирует ответ целиком (со всей постобработкой: _clean_response,
    маркеры, списки), затем финальный текст отдаётся порциями — клиент
    показывает эффект печати уже окончательного текста, без сырого стрима
    и замены содержимого пузыря в конце.
    """
    bot = await _get_bot(req.persona)
    _check_not_muted(bot, req.persona)
    await _touch_persona(req.persona)
    gen_key = f"{req.persona}:{req.chat_id or req.user_id}"
    stm_key = _stm_key(req)
    turn = {"frame": None}

    def _generate():
        """Тяжёлая синхронная часть — идёт в потоке пула (asyncio.to_thread).
        «Печать» сюда не входит (см. _gen ниже): она не блокирующая и не
        должна держать поток пула все секунды анимации — под нагрузкой
        параллельные /api/chat/stream иначе исчерпывали бы общий пул
        asyncio.to_thread, и другие эндпоинты зависали бы в очереди на
        поток."""
        # Поток генерации держит ход сам (см. /api/chat, _adopt_turn)
        with _adopt_turn(bot, turn["frame"]):
            cmd = _try_slash_command(bot, req)
            if cmd is not None:
                # Слэш-команда: без process_message (как в TG)
                reply, llm_used = cmd
                provider, model = _answer_provider(bot) if llm_used else (None, None)
                split_rest = bot.pop_pending_split_messages(req.chat_id)
            else:
                llm_input = (
                    _prepare_image_input(bot, req.message, req.image,
                                         req.chat_id, req.user_id)
                    if req.image else req.message
                )
                reply = bot.process_message(
                    llm_input,
                    user_id=req.user_id,
                    chat_id=req.chat_id,
                    user_name=req.user_name,
                    reply_context=req.reply_context,
                    # См. /api/chat: подтверждение pending-действия — только по
                    # тому, что реально ввёл пользователь, не по OCR картинки
                    raw_user_text=req.message if req.image else None,
                    from_skin=req.from_skin,
                )
                # Провайдер ответа — сразу и в этом же потоке (_answer_provider)
                provider, model = _answer_provider(bot)
                split_rest = bot.pop_pending_split_messages(req.chat_id)
                if req.image:
                    cap = req.message.strip()
                    _rewrite_image_stm(
                        bot, stm_key, req.user_id,
                        f"📷 {cap}" if cap else "📷 (изображение)", [reply] + split_rest,
                    )
            # В кадре хода — пока якорь STM этого хода виден
            reply_ts = _reply_stm_ts(bot, stm_key, reply)
        # Pending-бакеты снимаем здесь же, в потоке под локом чата: без лока
        # следующее сообщение в тот же чат успело бы обнулить/перехватить
        # бакеты этого ответа
        extra = split_rest + bot.pop_pending_list_messages(req.chat_id)
        question_kind = bot.pop_pending_question_kind(req.chat_id)
        images = _pending_images(bot, req.chat_id)
        return (reply, split_rest, extra, question_kind, images, provider,
                model, reply_ts)

    async def _gen():
        # Ход пользователя держим до конца «печати» (split-пузыри с паузами) —
        # иначе, закрывшись вместе с генерацией, он пустил бы напоминание/утро
        # из фона между частями ответа. finally — при любом исходе, включая
        # разрыв SSE (aclose генератора)
        turn["frame"] = await _begin_turn(bot, stm_key)
        try:
            async for chunk in _gen_in_turn():
                yield chunk
        finally:
            _end_turn(bot, turn["frame"])

    async def _gen_in_turn():
        # Генерация — в отдельной задаче (_run_generation): перезагрузка
        # страницы отменяет этот генератор, но не её; флаг «генерирует» и лок
        # чата снимутся, только когда ответ реально ляжет в STM
        # До лока чата: «стоп»/дубль/занятый агент в режиме управления —
        # короткий ответ сразу, без генерации
        early, cc_token = _cc_turn_enter(bot, req)
        if early is not None:
            async for ev in _typed_chunks(early):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            payload = {
                "done": True, "reply": early, "extra_messages": [],
                "question_kind": None, "persona": req.persona,
                "chat_id": req.chat_id or req.user_id,
                "provider": None, "model": None,
                "control_mode": bot.control_mode_on(req.chat_id or req.user_id),
                "images": [],
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return
        try:
            (reply, split_rest, extra, question_kind, images,
             provider, model, reply_ts) = await _run_generation(
                gen_key, _generate, on_done=lambda: _cc_turn_exit(bot, cc_token))
        except Exception:
            # Клиенту — обезличенное сообщение: текст исключения LLM-клиента
            # может содержать фрагменты API-ключа/URL с токеном авторизации
            # (так отвечают некоторые провайдеры на 401/403). Полный текст —
            # только в лог.
            logger.exception(f"[chat/stream] {req.persona}: ошибка генерации")
            yield f"data: {json.dumps({'error': 'Не удалось получить ответ. Попробуйте ещё раз.'}, ensure_ascii=False)}\n\n"
            return

        if reply_ts is not None:
            # Метка ответа — до первой порции: пузырь сразу встаёт в ленте
            # после промежуточных сообщений хода
            yield f"data: {json.dumps({'reply_ts': reply_ts})}\n\n"
        # «Печать» финального текста: порции по несколько символов
        async for ev in _typed_chunks(reply):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        # Расщеплённый хвост — отдельные пузыри: part_break велит фронту
        # начать новое сообщение, дальше части печатаются как обычно.
        # Пауза перед пузырём — как у TG-бота: растёт с длиной части
        for part in split_rest:
            yield f"data: {json.dumps({'part_break': True}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(send_delay(part))
            async for ev in _typed_chunks(part):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

        payload = {
            "done": True,
            "reply": reply,
            "extra_messages": extra,
            "question_kind": question_kind,
            "persona": req.persona,
            "chat_id": req.chat_id or req.user_id,
            "provider": provider,
            "model": model,
            # Режим управления после обработки сообщения (фронт гасит
            # дебаунс-паузу отправки для команд управления)
            "control_mode": bot.control_mode_on(req.chat_id or req.user_id),
            # Скриншоты страницы из режима управления («что на странице?»)
            "images": images,
            "reply_ts": reply_ts,
        }
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")


@app.get("/api/chat/history", response_model=list[HistoryMessage],
         dependencies=[Depends(require_auth)])
async def chat_history(
    persona: PersonaIdQuery,
    user_id: str = "web_user",
    chat_id: str | None = None,
):
    bot = await _get_bot(persona)
    await _touch_persona(persona)
    messages = await asyncio.to_thread(
        bot.memory.stm.get_messages, user_id=user_id, chat_id=chat_id
    )
    return messages


@app.post("/api/chat/clear", dependencies=[Depends(require_auth)])
async def chat_clear(req: ClearChatRequest):
    """ПОЛНОЕ стирание памяти персоны: STM диалога + LTM-факты пользователя
    + дневник персоны (self_memory) + история и статистика самоинициатив +
    метка активности + адреса веб-чатов LLM + todo, напоминания, досье чата,
    обучение, ритм, живое состояние персоны (срезы чата + глобальные мир/
    инвентарь/кэш контекста) и память режима управления (что просили и где
    бот был: аудит действий чата, страница, агент задач — см.
    app/api/memory_wipe.py). Перед сбросом
    делается снапшот в корзину (data/api_{persona}/clear_backups/, 7 дней)
    — см. эндпоинт restore. Не трогаем: сохранённые сценарии
    (пользовательские плейбуки), базу знаний книги, загруженные файлы.

    req.parts — стереть только эти части (опасная зона досье, кнопки по
    отдельности); в корзину уходят только они, restore вернёт их же."""
    from app.api import clear_backup
    from app.api import memory_wipe
    from app.features import web_llm as _wl
    bot = await _get_bot(req.persona)
    chat_key = req.chat_id or req.user_id
    parts = set(req.parts) if req.parts else set(memory_wipe.ALL_PARTS)
    full = parts == set(memory_wipe.ALL_PARTS)
    # Снапшот ДО удаления — на случай ошибочной очистки. _pop_* уже снимают
    # записи инициатив с диска (вернутся из снапшота)
    stm_msgs = (await asyncio.to_thread(bot.memory.stm.get_messages, None, chat_key)
                if "stm" in parts else [])
    ltm_facts = (await asyncio.to_thread(bot.memory.ltm.get_all_facts_with_meta, req.user_id)
                 if "ltm" in parts else [])
    diary_state = (bot.self_memory.export_state()
                   if "diary" in parts and bot.self_memory else None)
    initiatives, daily_stats, last_activity = [], None, 0
    if "initiatives" in parts:
        initiatives = await asyncio.to_thread(_pop_initiative_history, bot, req.persona, chat_key)
        daily_stats = await asyncio.to_thread(_pop_daily_stats, bot, req.persona, chat_key)
        last_activity = await asyncio.to_thread(_pop_last_activity, bot, req.persona, chat_key)
    chat_urls = (await asyncio.to_thread(_wl.collect_chat_urls, f"api_{req.persona}")
                 if "webchat" in parts else {})
    stores = await asyncio.to_thread(memory_wipe.collect_stores, bot, req.persona,
                                     chat_key, None if full else parts)
    await asyncio.to_thread(
        clear_backup.make_backup, req.persona, req.user_id, chat_key,
        stm_msgs, ltm_facts, diary_state, initiatives, daily_stats, last_activity,
        chat_urls=chat_urls, stores=stores,
        parts=None if full else [p for p in memory_wipe.ALL_PARTS if p in parts],
    )
    if "stm" in parts:
        await asyncio.to_thread(bot.memory.clear_stm, chat_key)
        # Метка свежести переписки — производная STM: сбрасываем вместе с ней
        # (в корзину не кладём: перепишется первым же новым сообщением)
        await asyncio.to_thread(
            _pop_json_key, data_dir() / f"api_{req.persona}" / "last_message.json", chat_key
        )
    if "ltm" in parts:
        await asyncio.to_thread(bot.memory.clear_ltm, req.user_id)
    if "diary" in parts and bot.self_memory:
        await asyncio.to_thread(bot.self_memory.clear_all)
    # Постоянные веб-чаты LLM — тоже память диалога (адреса старых чатов
    # сохранены в снапшоте выше)
    if "webchat" in parts:
        await asyncio.to_thread(_wl.clear_chat_urls, f"api_{req.persona}")
    # Остальное: todo/напоминания/досье/обучение/инициативы/ритм/living/управление
    await asyncio.to_thread(memory_wipe.wipe_stores, bot, req.persona, chat_key,
                            None if full else parts)
    return {"status": "ok", "parts": sorted(parts) if not full else "all"}


def _initiative_history_path(persona: str) -> Path:
    return data_dir() / f"api_{persona}" / "initiative_history.json"


def _proactive_stats_path(persona: str) -> Path:
    return data_dir() / f"api_{persona}" / "proactive_stats.json"


def _pop_json_key(path: Path, key: str):
    # Удалить ключ из json-словаря на диске, вернуть удалённое значение
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or key not in data:
            return None
        removed = data.pop(key)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return removed
    except Exception:
        return None


def _restore_json_key(path: Path, key: str, value):
    # Вернуть ключ в json-словарь на диске (восстановление из корзины)
    try:
        data = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        data[key] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _pop_initiative_history(bot, persona: str, chat_key: str) -> list:
    """Забрать и удалить историю самоинициатив чата (для снапшота корзины).

    Через живой менеджер, если проактивность включена; иначе напрямую из
    файла — менеджер при выключенной фиче не создаётся, а история могла
    остаться с прошлых сессий."""
    p = bot.proactive
    if p is not None:
        return p.clear_history(chat_key)
    removed = _pop_json_key(_initiative_history_path(persona), chat_key)
    return removed if isinstance(removed, list) else []


def _restore_initiative_history(bot, persona: str, chat_key: str, entries: list):
    # Вернуть историю самоинициатив из снапшота корзины (менеджер или файл)
    if not entries:
        return
    p = bot.proactive
    if p is not None:
        p.restore_history(chat_key, entries)
        return
    _restore_json_key(_initiative_history_path(persona), chat_key, list(entries))


def _pop_daily_stats(bot, persona: str, chat_key: str) -> dict | None:
    # Забрать и удалить дневной счётчик инициатив («инициатив сегодня»)
    p = bot.proactive
    if p is not None:
        return p.pop_daily_stats(chat_key)
    removed = _pop_json_key(_proactive_stats_path(persona), chat_key)
    return removed if isinstance(removed, dict) else None


def _restore_daily_stats(bot, persona: str, chat_key: str, entry: dict):
    # Вернуть дневной счётчик инициатив из снапшота корзины
    if not entry:
        return
    p = bot.proactive
    if p is not None:
        p.restore_daily_stats(chat_key, entry)
        return
    _restore_json_key(_proactive_stats_path(persona), chat_key, dict(entry))


def _known_chats_path(persona: str) -> Path:
    return data_dir() / f"api_{persona}" / "known_chats.json"


def _pop_last_activity(bot, persona: str, chat_key: str) -> float:
    """Забрать и удалить метку последней активности чата — молчание
    пользователя обнуляется (после сброса нет ни STM, ни активности,
    поэтому proactive не считает чат молчащим)."""
    tracker = getattr(bot, "_activity_tracker", None)
    if tracker is not None:
        return tracker.pop_activity(chat_key)
    path = _known_chats_path(persona)
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        activity = data.get("activity") if isinstance(data, dict) else None
        if not isinstance(activity, dict) or chat_key not in activity:
            return 0
        ts = activity.pop(chat_key)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return float(ts or 0)
    except Exception:
        return 0


def _restore_last_activity(bot, persona: str, chat_key: str, ts: float):
    # Вернуть метку последней активности из снапшота корзины
    if not ts:
        return
    tracker = getattr(bot, "_activity_tracker", None)
    if tracker is not None:
        tracker.restore_activity(chat_key, ts)
        return
    path = _known_chats_path(persona)
    try:
        data = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        activity = data.setdefault("activity", {})
        activity[chat_key] = ts
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


@app.get("/api/personas/{persona}/clear-backup", dependencies=[Depends(require_auth)])
async def clear_backup_info(persona: PersonaIdPath, chat_id: str | None = None):
    # Есть ли в корзине снапшот последней очистки этого чата (для кнопки
    # восстановления) — бэкапы привязаны к (persona, chat_id), см. clear_backup.py
    if persona not in list_personas():
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    from app.api import clear_backup
    return await asyncio.to_thread(clear_backup.backup_info, persona, chat_id or "web_user")


@app.post("/api/personas/{persona}/clear-backup/restore", dependencies=[Depends(require_auth)])
async def clear_backup_restore(persona: PersonaIdPath, chat_id: str | None = None):
    # Восстановить STM/LTM/дневник/историю инициатив из свежего снапшота
    # корзины этого чата (файл удаляется)
    from app.api import clear_backup
    bot = await _get_bot(persona)
    data = await asyncio.to_thread(clear_backup.pop_latest, persona, chat_id or "web_user")
    if not data:
        raise HTTPException(status_code=404, detail="Корзина пуста — нечего восстанавливать")

    chat_key = data.get("chat_id") or data.get("user_id")
    user_id = data.get("user_id") or "web_user"
    restored = {"stm": 0, "ltm": 0, "diary": False}

    for msg in data.get("stm") or []:
        await asyncio.to_thread(
            bot.memory.add_message,
            msg.get("role", "user"), msg.get("content", ""),
            msg.get("sender_id") or user_id, chat_key, msg.get("user_name"),
        )
        restored["stm"] += 1
    for fact in data.get("ltm") or []:
        # по одному факту: save_facts режет по запятым, а в тексте факта они бывают
        await asyncio.to_thread(
            bot.memory.ltm.save_facts, fact.get("fact", ""), user_id,
            fact.get("origin_chat") or None,
        )
        restored["ltm"] += 1
    if data.get("diary") and bot.self_memory:
        await asyncio.to_thread(bot.self_memory.import_state, data["diary"])
        restored["diary"] = True
    init_entries = data.get("initiatives") or []
    if init_entries:
        await asyncio.to_thread(_restore_initiative_history, bot, persona, chat_key, init_entries)
        restored["initiatives"] = len(init_entries)
    daily_stats = data.get("daily_stats")
    if daily_stats:
        await asyncio.to_thread(_restore_daily_stats, bot, persona, chat_key, daily_stats)
        restored["initiatives_today"] = daily_stats.get("count", 0)
    last_activity = data.get("last_activity") or 0
    if last_activity:
        await asyncio.to_thread(_restore_last_activity, bot, persona, chat_key, last_activity)
        restored["last_activity"] = True
    # Адреса веб-чатов LLM: персона «вспомнила» всё — возвращаем и её чаты
    chat_urls = data.get("chat_urls") or {}
    if chat_urls:
        from app.features import web_llm as _wl
        await asyncio.to_thread(_wl.restore_chat_urls, f"api_{persona}", chat_urls)
        restored["webchat_chats"] = len(chat_urls)
    # Остальные срезы памяти (todo/напоминания/досье/обучение/ритм/living)
    stores = data.get("stores") or {}
    if stores:
        from app.api import memory_wipe
        await asyncio.to_thread(memory_wipe.restore_stores, bot, persona,
                                chat_key, stores)
        restored["stores"] = len(stores)

    return {"status": "ok", "restored": restored}


@app.post("/api/chat/history/delete", dependencies=[Depends(require_auth)])
async def chat_history_delete(req: StmDeleteRequest):
    # Удалить одно сообщение из STM (поштучное удаление в досье)
    bot = await _get_bot(req.persona)
    chat_key = req.chat_id or req.user_id
    if req.content is None:
        ok = await asyncio.to_thread(bot.memory.stm.delete_message, chat_key, req.index)
    else:
        ok = await asyncio.to_thread(
            _stm_delete_matching, bot.memory.stm, chat_key, req.index, req.content, req.timestamp)
    if not ok:
        raise HTTPException(status_code=404, detail="Сообщение не найдено — история изменилась, обновите её")
    return {"status": "ok"}


def _stm_delete_matching(stm, chat_key: str, hint: int, content: str,
                         timestamp: float | None) -> bool:
    """Удалить из STM реплику с этим текстом (и меткой времени, если она
    была в истории). Буфер — deque(maxlen): пока фронт показывал историю,
    старые реплики могли вытесниться и индексы сдвинуться, поэтому индекс
    с фронта — только подсказка при одинаковых репликах. Поиск и удаление —
    под локом буфера (RLock, delete_message берёт его повторно)."""
    with stm._lock:
        msgs = stm.get_messages(chat_id=chat_key)
        matches = [
            i for i, m in enumerate(msgs)
            if m.get("content") == content and (
                timestamp is None
                or abs(float(m.get("timestamp") or 0) - timestamp) < 0.001)
        ]
        if not matches:
            return False
        idx = min(matches, key=lambda i: abs(i - hint))
        return stm.delete_message(chat_key, idx)


@app.post("/api/chat/history/trim", dependencies=[Depends(require_auth)])
async def chat_history_trim(req: StmTrimRequest):
    # Удалить последние N сообщений из STM (кнопка «Удалить» в досье)
    bot = await _get_bot(req.persona)
    chat_key = req.chat_id or req.user_id
    n = await asyncio.to_thread(bot.memory.stm.pop_last_n, req.count, chat_key)
    return {"status": "ok", "deleted": n}


# ── Память ────────────────────────────────────────────────────────────

@app.get("/api/personas/{persona}/memory/stats", response_model=MemoryStats,
         dependencies=[Depends(require_auth)])
async def memory_stats(persona: PersonaIdPath, user_id: str = "web_user",
                       chat_id: str | None = None):
    bot = await _get_bot(persona)
    return await asyncio.to_thread(bot.get_memory_stats, user_id, chat_id)


@app.get("/api/personas/{persona}/memory/ltm", response_model=list[str],
         dependencies=[Depends(require_auth)])
async def memory_ltm(persona: PersonaIdPath, user_id: str = "web_user"):
    bot = await _get_bot(persona)
    return await asyncio.to_thread(bot.memory.ltm.get_all_facts, user_id)


@app.get("/api/personas/{persona}/dossier", dependencies=[Depends(require_auth)])
async def persona_dossier(persona: PersonaIdPath, chat_id: str = "web_user"):
    # Профиль досье чата: интересы/темы/наблюдения (анализ диалога, не LTM)
    bot = await _get_bot(persona)
    return await asyncio.to_thread(bot.get_dossier_snapshot, chat_id)


@app.post("/api/personas/{persona}/memory/facts", dependencies=[Depends(require_auth)])
async def memory_add_fact(persona: PersonaIdPath, req: FactRequest):
    bot = await _get_bot(persona)
    await asyncio.to_thread(bot.inject_fact, req.fact, req.user_id)
    return {"status": "ok"}


@app.put("/api/personas/{persona}/memory/facts", dependencies=[Depends(require_auth)])
async def memory_update_fact(persona: PersonaIdPath, req: FactUpdateRequest):
    # Замена факта отредактированным текстом (правка в досье)
    bot = await _get_bot(persona)
    new_text = req.new.strip()
    if not new_text:
        raise HTTPException(status_code=400, detail="Пустой текст факта")
    old = await asyncio.to_thread(bot.update_fact, req.old, new_text, req.user_id)
    if old is None:
        raise HTTPException(status_code=404, detail="Факт не найден")
    return {"status": "ok", "old": old}


@app.delete("/api/personas/{persona}/memory/facts", dependencies=[Depends(require_auth)])
async def memory_forget_fact(persona: PersonaIdPath, query: str, user_id: str = "web_user"):
    bot = await _get_bot(persona)
    removed = await asyncio.to_thread(bot.forget_fact, query, user_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="Факт не найден")
    return {"status": "ok", "removed": removed}


@app.post("/api/personas/{persona}/memory/clear", dependencies=[Depends(require_auth)])
async def memory_clear(persona: PersonaIdPath, user_id: str = "web_user",
                       chat_id: str | None = None):
    bot = await _get_bot(persona)
    await asyncio.to_thread(bot.clear_memory, user_id, chat_id)
    return {"status": "ok"}


# ── Файлы ─────────────────────────────────────────────────────────────

@app.post("/api/personas/{persona}/files", dependencies=[Depends(require_auth)])
async def upload_file(persona: PersonaIdPath, file: UploadFile, user_id: str = "web_user"):
    bot = await _get_bot(persona)
    if bot.file_db is None:
        raise HTTPException(
            status_code=400,
            detail="Загрузка файлов недоступна для этой персоны (file_upload: false)",
        )
    file_bytes = await file.read()
    filename = file.filename or "file"

    def _process() -> list[str]:
        text = extract_text(file_bytes, filename)
        if text.startswith(_EXTRACT_ERROR_PREFIXES):
            raise HTTPException(status_code=400, detail=text)
        bot.file_db.add_file(user_id=user_id, filename=filename, content=text)
        return bot.file_db.list_files_detailed(user_id)

    loaded = await asyncio.to_thread(_process)
    return {"status": "ok", "filename": filename, "files": loaded}


@app.get("/api/personas/{persona}/files", dependencies=[Depends(require_auth)])
async def list_files(persona: PersonaIdPath, user_id: str = "web_user"):
    bot = await _get_bot(persona)
    if bot.file_db is None:
        return {"files": []}
    files = await asyncio.to_thread(bot.file_db.list_files_detailed, user_id)
    return {"files": files}


@app.get("/api/personas/{persona}/files/{filename}/content", dependencies=[Depends(require_auth)])
async def file_content(persona: PersonaIdPath, filename: str, user_id: str = "web_user"):
    bot = await _get_bot(persona)
    if bot.file_db is None:
        raise HTTPException(status_code=400, detail="Загрузка файлов недоступна для этой персоны")
    content = await asyncio.to_thread(bot.file_db.get_full_document, user_id, filename)
    if content is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    return {"filename": filename, "content": content}


@app.delete("/api/personas/{persona}/files/{filename}", dependencies=[Depends(require_auth)])
async def delete_file(persona: PersonaIdPath, filename: str, user_id: str = "web_user"):
    bot = await _get_bot(persona)
    if bot.file_db is None:
        raise HTTPException(status_code=400, detail="Загрузка файлов недоступна для этой персоны")
    ok = await asyncio.to_thread(bot.file_db.remove_file, user_id, filename)
    if not ok:
        raise HTTPException(status_code=404, detail="Файл не найден")
    files = await asyncio.to_thread(bot.file_db.list_files_detailed, user_id)
    return {"files": files}


@app.delete("/api/personas/{persona}/files", dependencies=[Depends(require_auth)])
async def reset_files(persona: PersonaIdPath, user_id: str = "web_user"):
    bot = await _get_bot(persona)
    if bot.file_db is not None:
        await asyncio.to_thread(bot.file_db.reset, user_id)
    return {"status": "ok"}


# ── Дела (todo) ───────────────────────────────────────────────────────
# Менеджеры фич ключуются по chat_id — веб-фронт использует "web_user".

def _todo_items(bot, chat_id: str) -> list[dict]:
    if bot.todo_manager is None:
        return []
    # Читаем файл напрямую: get_list() отдаёт отрендеренный текст, не формат хранения
    path = bot.todo_manager._todo_path(chat_id)
    if not path.exists():
        return []
    items = bot.todo_manager._parse_items(path.read_text(encoding="utf-8"))
    return [
        {"index": i + 1, "user_name": name, "task": task}
        for i, (name, task) in enumerate(items)
    ]


@app.get("/api/personas/{persona}/todo", dependencies=[Depends(require_auth)])
async def todo_list(persona: PersonaIdPath, chat_id: str = "web_user"):
    bot = await _get_bot(persona)
    return {"items": await asyncio.to_thread(_todo_items, bot, chat_id)}


@app.post("/api/personas/{persona}/todo", dependencies=[Depends(require_auth)])
async def todo_add(persona: PersonaIdPath, req: TodoAddRequest):
    bot = await _get_bot(persona)
    if bot.todo_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет модуля дел (todo: false)")
    await asyncio.to_thread(bot.todo_manager.add_item, req.chat_id, req.user_name, req.task)
    return {"items": await asyncio.to_thread(_todo_items, bot, req.chat_id)}


@app.delete("/api/personas/{persona}/todo", dependencies=[Depends(require_auth)])
async def todo_remove(persona: PersonaIdPath, index: int, chat_id: str = "web_user"):
    bot = await _get_bot(persona)
    if bot.todo_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет модуля дел (todo: false)")
    result = await asyncio.to_thread(bot.todo_manager.remove_item, chat_id, index)
    if result is None:
        raise HTTPException(status_code=404, detail="Пункт не найден")
    return {"items": await asyncio.to_thread(_todo_items, bot, chat_id)}


# ── Напоминания ───────────────────────────────────────────────────────

def _reminders(bot, chat_id: str) -> list[dict]:
    if bot.reminder_manager is None:
        return []
    return [
        {
            "index": i + 1,  # 1-based позиция в этом списке — фронт шлёт её в DELETE
            "id": r.get("id"),
            "task": r.get("task") or "",
            "trigger_at": r.get("trigger_at"),
            "recurrence": r.get("recurrence"),
            "user_name": r.get("user_name") or "",
            # False — на паузе: не сработает, пока не продолжат (PUT active)
            "active": not r.get("paused"),
        }
        for i, r in enumerate(bot.reminder_manager.get_active(chat_id))
    ]


@app.get("/api/personas/{persona}/reminders", dependencies=[Depends(require_auth)])
async def reminders_list(persona: PersonaIdPath, chat_id: str = "web_user"):
    bot = await _get_bot(persona)
    return {"items": await asyncio.to_thread(_reminders, bot, chat_id)}


@app.post("/api/personas/{persona}/reminders", dependencies=[Depends(require_auth)])
async def reminders_add(persona: PersonaIdPath, req: ReminderAddRequest):
    from app.features.reminder_manager import normalize_schedule
    bot = await _get_bot(persona)
    if bot.reminder_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет напоминаний (reminder: false)")
    schedule = None
    if req.recurrence is not None:
        try:
            schedule = normalize_schedule(req.recurrence.model_dump(exclude_none=True),
                                          time.time() + req.delay_seconds)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"Недопустимый повтор: {e}")
    await asyncio.to_thread(
        bot.reminder_manager.add_reminder,
        req.chat_id, req.user_name, req.task, req.delay_seconds,
        schedule=schedule,
    )
    return {"items": await asyncio.to_thread(_reminders, bot, req.chat_id)}


@app.delete("/api/personas/{persona}/reminders", dependencies=[Depends(require_auth)])
async def reminders_cancel(persona: PersonaIdPath, index: int | None = None,
                           id: str | None = None, chat_id: str = "web_user"):
    # Надёжный путь — id напоминания (он же в _reminders()): между показом
    # списка и отменой одно могло сработать, и номер строки указал бы на
    # соседнее. 1-based index — для совместимости; оба идут в единую точку
    # удаления по ссылке (parse_reminder_ref/cancel_by_ref, см. reminder_manager).
    bot = await _get_bot(persona)
    if bot.reminder_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет напоминаний (reminder: false)")
    if id:
        ref = id
    elif index is not None:
        ref = index - 1
    else:
        raise HTTPException(status_code=422, detail="Нужен id или index напоминания")
    removed = await asyncio.to_thread(bot.reminder_manager.cancel_by_ref, chat_id, ref)
    if removed is None:
        raise HTTPException(status_code=404, detail="Напоминание не найдено")
    return {"items": await asyncio.to_thread(_reminders, bot, chat_id)}


@app.put("/api/personas/{persona}/reminders/{rid}", dependencies=[Depends(require_auth)])
async def reminders_update(persona: PersonaIdPath, rid: str, req: ReminderUpdateRequest):
    # Правка текста/времени/паузы/повтора напоминания на месте, по id
    # (повтор не передан — сохраняется, явный null — снимается)
    from app.features.reminder_manager import UNSET
    bot = await _get_bot(persona)
    if bot.reminder_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет напоминаний (reminder: false)")
    task = req.task.strip() if req.task is not None else None
    if task is not None and not task:
        raise HTTPException(status_code=422, detail="Текст напоминания пустой")
    recurrence = UNSET
    if "recurrence" in req.model_fields_set:
        recurrence = (req.recurrence.model_dump(exclude_none=True)
                      if req.recurrence is not None else None)
    try:
        updated = await asyncio.to_thread(
            bot.reminder_manager.update_by_id, req.chat_id, rid, task, req.trigger_at,
            req.active, recurrence)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Недопустимое время или повтор: {e}")
    if updated is None:
        raise HTTPException(status_code=404, detail="Напоминание не найдено")
    return {"items": await asyncio.to_thread(_reminders, bot, req.chat_id)}


# ── Календарь ─────────────────────────────────────────────────────────
# Один на всех персон (data/calendar.json). Кроме собственных записей
# выдача включает активные напоминания всех персон — как readonly-строки
# (source="reminder"), чтобы общий календарь отражал и то, что боты
# назначили через чат.

def _calendar_personas() -> dict:
    # id персоны → {name, color} для меток записей календаря
    out = {}
    for name in list_personas():
        info = get_persona_info(name)
        if info:
            out[name] = {"name": info["name"], "color": info["color"]}
    return out


def _calendar_view(e: dict, persons: dict) -> dict:
    # Запись календаря + имя/цвет персоны-владельца для отображения
    info = persons.get(e.get("persona") or "") or {}
    return {
        **e,
        "persona_name": info.get("name"),
        "color": info.get("color"),
        "source": "calendar",
        "readonly": False,
    }


def _reminder_calendar_items(start, end, persons) -> list[dict]:
    # Активные напоминания всех персон как readonly-записи календаря.
    # Читаем JSON-файлы напрямую, без подъёма BotInstance (он тяжёлый)
    items = []
    now = time.time()
    for persona in list_personas():
        path = Path(Config.DATA_DIR) / f"api_{persona}" / "reminders" / "reminders.json"
        if not path.is_file():
            continue
        try:
            reminders = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        info = persons.get(persona) or {}
        for i, r in enumerate(reminders):
            # На паузе — не в календаре: он показывает то, что сработает
            if (r.get("fired") or r.get("paused") or not r.get("trigger_at")
                    or r["trigger_at"] <= now):
                continue
            # timeutil.from_ts, а не datetime.fromtimestamp: это время
            # пользователя (TIMEZONE), а не системного пояса процесса.
            dt = timeutil.from_ts(r["trigger_at"])
            date = dt.strftime("%Y-%m-%d")
            if (start and date < start) or (end and date > end):
                continue
            items.append({
                # id напоминания, а не позиционный индекс i: после отмены/срабатывания
                # более раннего напоминания индексы остальных сдвигаются, и
                # тот же календарный элемент менял бы id между обновлениями.
                "id": f"rem:{persona}:{r.get('id') or i}",
                "title": r.get("task") or "",
                "date": date,
                "time": dt.strftime("%H:%M"),
                "kind": "reminder",
                "persona": persona,
                "persona_name": info.get("name") or persona,
                "color": info.get("color"),
                "note": "",
                "done": False,
                "created_at": r.get("created_at"),
                "source": "reminder",
                "readonly": True,
                "recurrence": r.get("recurrence"),
            })
    return items


@app.get("/api/calendar", dependencies=[Depends(require_auth)])
async def calendar_list(start: str | None = None, end: str | None = None):
    # Записи календаря за диапазон дат (YYYY-MM-DD, включительно) +
    # активные напоминания всех персон
    persons = await asyncio.to_thread(_calendar_personas)
    entries = await asyncio.to_thread(get_calendar().list_entries, start, end)
    items = [_calendar_view(e, persons) for e in entries]
    items += await asyncio.to_thread(_reminder_calendar_items, start, end, persons)
    items.sort(key=lambda x: (x["date"], x.get("time") or ""))
    return {"items": items}


@app.post("/api/calendar", dependencies=[Depends(require_auth)])
async def calendar_add(req: CalendarEntryCreate):
    if req.persona and get_persona_info(req.persona) is None:
        raise HTTPException(status_code=404, detail=f"Персона '{req.persona}' не найдена")
    try:
        entry = await asyncio.to_thread(
            get_calendar().add_entry,
            req.title, req.date, req.time, req.kind, req.persona, req.user_name, req.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    persons = await asyncio.to_thread(_calendar_personas)
    return _calendar_view(entry, persons)


@app.put("/api/calendar/{entry_id}", dependencies=[Depends(require_auth)])
async def calendar_update(entry_id: str, req: CalendarEntryUpdate):
    # exclude_unset: явный null сбрасывает time/persona, непереданные поля не трогаем
    patch = req.model_dump(exclude_unset=True)
    if req.persona and get_persona_info(req.persona) is None:
        raise HTTPException(status_code=404, detail=f"Персона '{req.persona}' не найдена")
    try:
        entry = await asyncio.to_thread(get_calendar().update_entry, entry_id, **patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if entry is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    persons = await asyncio.to_thread(_calendar_personas)
    return _calendar_view(entry, persons)


@app.delete("/api/calendar/{entry_id}", dependencies=[Depends(require_auth)])
async def calendar_remove(entry_id: str):
    ok = await asyncio.to_thread(get_calendar().remove_entry, entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return {"status": "ok"}


# ── Инвентарь ─────────────────────────────────────────────────────────
# Инвентарь общий на персону (не на чат/пользователя).

@app.get("/api/personas/{persona}/inventory", dependencies=[Depends(require_auth)])
async def inventory_list(persona: PersonaIdPath):
    bot = await _get_bot(persona)
    if bot.inventory_manager is None:
        return {"items": []}
    items = await asyncio.to_thread(
        lambda: [i.to_dict() for i in bot.inventory_manager.get_items()]
    )
    return {"items": items}


@app.post("/api/personas/{persona}/inventory", dependencies=[Depends(require_auth)])
async def inventory_add(persona: PersonaIdPath, req: InventoryAddRequest):
    bot = await _get_bot(persona)
    if bot.inventory_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет инвентаря (inventory: false)")
    result = await asyncio.to_thread(
        bot.inventory_manager.add_item, req.name, req.description, req.source
    )
    return {"result": result}


@app.delete("/api/personas/{persona}/inventory", dependencies=[Depends(require_auth)])
async def inventory_remove(persona: PersonaIdPath, name: str):
    bot = await _get_bot(persona)
    if bot.inventory_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет инвентаря (inventory: false)")
    result = await asyncio.to_thread(bot.inventory_manager.remove_item, name)
    return {"result": result}


# ── Обучение ──────────────────────────────────────────────────────────

def _learning_sessions(bot, chat_id: str) -> list[dict]:
    if bot.learning_manager is None:
        return []
    return [
        {
            "session_id": s["session_id"],
            "subject": s["subject"],
            "active": s["active"],
            "lesson_count": s["lesson_count"],
            "covered_topics": s["covered_topics"],
            "learned_vocabulary": s["learned_vocabulary"],
            "next_lesson_at": s["next_lesson_at"],
            "interval_seconds": s["interval_seconds"],
            "quiz_pending": s["quiz_pending"] is not None,
        }
        for s in bot.learning_manager.get_sessions(chat_id)
    ]


@app.get("/api/personas/{persona}/learning", dependencies=[Depends(require_auth)])
async def learning_list(persona: PersonaIdPath, chat_id: str = "web_user"):
    bot = await _get_bot(persona)
    return {"sessions": await asyncio.to_thread(_learning_sessions, bot, chat_id)}


@app.post("/api/personas/{persona}/learning", dependencies=[Depends(require_auth)])
async def learning_start(persona: PersonaIdPath, req: LearningStartRequest):
    bot = await _get_bot(persona)
    if bot.learning_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет обучения (learning: false)")

    def _start():
        # Двухшаговое создание курса: setup (тема) + commit (частота).
        # user_id="web_user" в обоих — begin_setup кладёт setup именно под
        # этим ключом (chat_id, user_id), а без user_id в commit_session
        # применяется фолбэк get_setup_state (см. learning_manager): «единственный
        # ожидающий» верно почти всегда, но параллельный /learn из другого
        # источника в тот же chat_id (TG-бот и веб одновременно) увёл бы commit
        # не в тот setup.
        bot.learning_manager.begin_setup(req.chat_id, req.subject, "web_user", req.user_name)
        return bot.learning_manager.commit_session(
            req.chat_id, req.interval_seconds, user_id="web_user"
        )

    session = await asyncio.to_thread(_start)
    if session is None:
        raise HTTPException(status_code=400, detail="Не удалось создать курс")
    return {"sessions": await asyncio.to_thread(_learning_sessions, bot, req.chat_id)}


@app.delete("/api/personas/{persona}/learning", dependencies=[Depends(require_auth)])
async def learning_stop(persona: PersonaIdPath, session_id: str, chat_id: str = "web_user"):
    bot = await _get_bot(persona)
    if bot.learning_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет обучения (learning: false)")
    await asyncio.to_thread(
        bot.learning_manager.stop_session, chat_id, session_id
    )
    return {"sessions": await asyncio.to_thread(_learning_sessions, bot, chat_id)}


# ── Дневник (self_memory) ─────────────────────────────────────────────

@app.get("/api/personas/{persona}/diary", dependencies=[Depends(require_auth)])
async def diary(persona: PersonaIdPath):
    bot = await _get_bot(persona)
    sm = bot.self_memory
    if sm is None:
        return {"episodes": [], "notes": [], "life_summary": ""}

    def _read():
        episodes = getattr(sm, "_episodes", {}) or {}
        notes = getattr(sm, "_notes", {}) or {}
        return {
            "episodes": episodes.get("active", []) + episodes.get("archive", []),
            "notes": notes.get("notes", []),
            "life_summary": episodes.get("life_summary", ""),
        }

    return await asyncio.to_thread(_read)


# ── Живая персона: состояние + мир (ui_room_mood_sync) ────────────────
@app.get("/api/personas/{persona}/state", dependencies=[Depends(require_auth)])
async def living_state(persona: PersonaIdPath, chat_id: str = "web_user"):
    # Текущее состояние персоны (energy/mood/pastime/location), сюжетные
    # линии и лента последних событий — для вкладок комната/настроение
    bot = await _get_bot(persona)
    living = getattr(bot, "living", None)
    if living is None:
        return {"enabled": False, "ui_sync": False, "state": None}
    return await asyncio.to_thread(living.get_state_for_ui, chat_id)


# ── Комната: живое присутствие персоны (app/api/room_api.py) ──────────
# Всё из файлов обоих контекстов (веб api_<p> и Telegram <p> — другой
# процесс), без создания BotInstance. web_presence здесь НЕ трогаем: открытая
# комната не должна морозить фоновую жизнь персоны, это забота вкладки чата.

def _room_persona(persona: str) -> dict:
    from app.core import room
    data = room.load_persona_data(persona)
    if not data or not data.get("system_prompt"):
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    return data


async def _room_call(fn, *args):
    # Операция комнаты в потоке; RoomError → HTTP-код с текстом
    from app.api import room_api
    try:
        return await asyncio.to_thread(fn, *args)
    except room_api.RoomError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@app.get("/api/personas/{persona}/room", dependencies=[Depends(require_auth)])
async def room_get(persona: PersonaIdPath, chat_id: str = "auto", context: str | None = None):
    # Снимок комнаты: источник (самый свежий чат), конфиг, живое состояние,
    # инвентарь с размещениями, фокус-сессия. Фронт опрашивает раз в минуту
    from app.api import room_api
    data = await asyncio.to_thread(_room_persona, persona)
    return await _room_call(room_api.room_snapshot, persona, data, chat_id, context)


@app.get("/api/personas/{persona}/room/layout", dependencies=[Depends(require_auth)])
async def room_layout_get(persona: PersonaIdPath):
    from app.api import room_api
    await asyncio.to_thread(_room_persona, persona)
    return await _room_call(room_api.get_layout, persona)


@app.put("/api/personas/{persona}/room/layout", dependencies=[Depends(require_auth)])
async def room_layout_put(persona: PersonaIdPath, req: RoomLayoutUpdate):
    from app.api import room_api
    await asyncio.to_thread(_room_persona, persona)
    return await _room_call(room_api.put_layout, persona, req.model_dump(exclude_unset=True))


@app.get("/api/personas/{persona}/room/style", dependencies=[Depends(require_auth)])
async def room_style_get(persona: PersonaIdPath):
    from app.api import room_api
    await asyncio.to_thread(_room_persona, persona)
    return await _room_call(room_api.get_style, persona)


@app.put("/api/personas/{persona}/room/style", dependencies=[Depends(require_auth)])
async def room_style_put(persona: PersonaIdPath, req: RoomStyleUpdate):
    from app.api import room_api
    await asyncio.to_thread(_room_persona, persona)
    return await _room_call(room_api.put_style, persona, req.model_dump(exclude_unset=True))


@app.post("/api/personas/{persona}/room/style/describe", dependencies=[Depends(require_auth)])
async def room_style_describe(persona: PersonaIdPath, req: RoomStyleDescribeRequest):
    # Описание ТОЛЬКО стиля референса (медиум, линия, палитра, пропорции)
    # vision-цепочкой основного роутера персоны — тем же путём, что ответ
    # на фото в чате (router.get_response_with_image)
    from app.api import room_api
    await asyncio.to_thread(_room_persona, persona)
    raw, mime = await _room_call(room_api.decode_data_url, req.reference,
                                 room_api.REFERENCE_MAX_BYTES)
    bot = await _get_bot(persona)
    if not bot.router.supports_vision():
        raise HTTPException(status_code=501,
                            detail="Нет vision-модели: ни один провайдер персоны не принимает картинки")
    answer = await asyncio.to_thread(
        bot.router.get_response_with_image, room_api.STYLE_DESCRIBE_PROMPT, raw,
        max_tokens=200, timeout=90.0, image_mime=mime)
    if not answer or not str(answer).strip():
        raise HTTPException(status_code=502, detail="Vision-модель не ответила")
    return {"description": room_api.trim_words(answer, 60)}


@app.get("/api/personas/{persona}/room/art", dependencies=[Depends(require_auth)])
async def room_art_get(persona: PersonaIdPath):
    from app.api import room_api
    await asyncio.to_thread(_room_persona, persona)
    return await _room_call(room_api.get_art, persona)


@app.put("/api/personas/{persona}/room/art", dependencies=[Depends(require_auth)])
async def room_art_put(persona: PersonaIdPath, req: RoomArtUpdate):
    from app.api import room_api
    await asyncio.to_thread(_room_persona, persona)
    return await _room_call(room_api.put_art, persona, req.model_dump(exclude_unset=True))


@app.post("/api/personas/{persona}/room/poke", dependencies=[Depends(require_auth)])
async def room_poke(persona: PersonaIdPath, req: RoomPokeRequest):
    # Клик по персоне: до LLM доходит только при features.room_pokes_to_llm
    # и не чаще раза в 15 мин на чат; иначе delivered: false (всё равно 200)
    from app.api import room_api
    data = await asyncio.to_thread(_room_persona, persona)
    return await _room_call(room_api.poke, persona, data, req.chat_id or "auto")


@app.post("/api/personas/{persona}/room/focus", dependencies=[Depends(require_auth)])
async def room_focus(persona: PersonaIdPath, req: RoomFocusRequest):
    """Фокус-сессия «поработать рядом»: start/end + сигнал персоне. На end —
    одна короткая реплика персоны («как прошло?») основной LLM веб-бота
    персоны; она же уходит в веб-инбокс и STM чата web_user (как инициатива).
    Не вышло — line: null."""
    from app.api import room_api
    await asyncio.to_thread(_room_persona, persona)
    result = await _room_call(room_api.focus, persona, req.action, req.minutes,
                              req.chat_id or "auto")
    if req.action != "end":
        return result
    line = None
    if not result.pop("was_active", True):
        # Повторный end (сессия уже закрыта) — без второй реплики и LLM-вызова
        result["line"] = None
        return result
    try:
        bot = await asyncio.to_thread(runtime.registry.get, persona)
        if bot is not None:
            line = await asyncio.to_thread(
                room_api.generate_focus_line, bot, result.get("elapsed_min") or 0)
            if line:
                from app.api.inbox import inbox_push
                await asyncio.to_thread(
                    bot.memory.add_message, "assistant", line,
                    user_id=room_api.WEB_CHAT_ID, chat_id=room_api.WEB_CHAT_ID)
                inbox_push(persona, room_api.WEB_CHAT_ID, line)
    except Exception as e:
        logger.warning(f"[room] Реплика конца фокус-сессии не доставлена: {e}")
    result["line"] = line
    return result


# Без persona (старый фронт — см. PresenceRequest.persona) предупреждаем
# в лог один раз на процесс, а не на каждый запрос: иначе массовый старый
# кэш фронта у многих клиентов залил бы лог одинаковым warning на каждый
# heartbeat.
_presence_no_persona_warned = False


# ── Inbox: фоновые сообщения (напоминания, уроки, инициативы) ─────────
@app.post("/api/presence", dependencies=[Depends(require_auth)])
async def presence(req: PresenceRequest):
    """Состояние вкладки веб-чата (видима и в фокусе): мгновенный сигнал по
    visibilitychange/focus/blur. Пока активна — фоновая работа бота по ЭТОМУ
    чату молчит (ключ — персона + чат, см. app/core/presence.py).

    persona отсутствует (старый фронт) — запрос принимается (200), отметка
    не ставится (ключ без персоны построить нельзя): не 422, чтобы старый
    фронт не сыпал ошибками в консоль, но и не притворяемся, что что-то
    сработало."""
    if req.persona is None:
        global _presence_no_persona_warned
        if not _presence_no_persona_warned:
            logger.warning(
                "[Presence] /api/presence без persona (старый фронт?) — "
                "отметка присутствия не поставлена"
            )
            _presence_no_persona_warned = True
        return {"ok": True}
    web_presence.note(web_context(req.persona), req.chat_id, req.active)
    return {"ok": True}


def _read_last_message_ts(persona: str, chat_id: str) -> float:
    # Метка последнего сообщения чата из data/api_{persona}/last_message.json
    # (синхронное чтение — вызывать через asyncio.to_thread)
    lm_file = data_dir() / f"api_{persona}" / "last_message.json"
    if not lm_file.is_file():
        return 0.0
    try:
        lm_data = json.loads(lm_file.read_text(encoding="utf-8"))
        if isinstance(lm_data, dict):
            return float(lm_data.get(chat_id, 0) or 0)
    except Exception:
        pass
    return 0.0


@app.get("/api/personas/{persona}/inbox", dependencies=[Depends(require_auth)])
async def inbox(persona: PersonaIdPath, chat_id: str = "web_user", focused: bool = False):
    from app.api.inbox import inbox_pop
    from app.api.runtime import registry
    if get_persona_info(persona) is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    # Heartbeat активности вкладки: только установка, не сброс — иначе поллинг
    # скрытой вкладки гасил бы флаг активной (сброс — POST /api/presence или TTL)
    if focused:
        web_presence.note(web_context(persona), chat_id, True)
    # Бота не создаём: inbox — фоновый опрос всех персон, не должен
    # инициализировать тяжёлые инстансы. Нет бота — нет и сообщений.
    # generating — идёт ли сейчас генерация ответа в этом чате (фронт
    # показывает «печатает» даже после перезагрузки страницы).
    generating = f"{persona}:{chat_id}" in _generating
    # Метка последнего сообщения чата (STM штампует её на каждое сообщение) —
    # фронт сортирует по ней список персон по свежести переписки. inbox —
    # часто опрашиваемый эндпоинт (поллинг фронта): синхронное чтение файла
    # прямо в хендлере блокировало бы event loop на каждый тик
    last_ts = await asyncio.to_thread(_read_last_message_ts, persona, chat_id)
    bot = registry._bots.get(persona)
    # Карантины веб-чатов (антибот) и состояние пулов браузера — общие для
    # процесса, не per-chat
    try:
        from app.features import web_llm as _wl
        quarantine = _wl.quarantine_status()
    except Exception:
        quarantine = {}
    try:
        from app.features import browser_actions as _ba
        pools = _ba.pool_status()
    except Exception:
        pools = {}
    if bot is None:
        return {"messages": [], "generating": generating, "last_ts": last_ts,
                "control_mode": False, "webchat_quarantine": quarantine,
                "browser_pools": pools}
    # Поллинг инбокса = пользователь открыл веб: сигнал присутствия для rhythm
    # (дёшев, с внутренним троттлингом — триггер утреннего приветствия)
    try:
        bot.note_presence(chat_id)
    except Exception:
        pass
    try:
        control_mode = bot.control_mode_on(chat_id)
    except Exception:
        control_mode = False
    return {"messages": inbox_pop(persona, chat_id), "generating": generating,
            "last_ts": last_ts, "control_mode": control_mode,
            "webchat_quarantine": quarantine, "browser_pools": pools}


# ── Инициатива (proactive) ────────────────────────────────────────────

def _read_ignore_streak(persona: str, chat_id: str) -> int:
    # Ступень «игнора» чата из data/api_{persona}/ignore_streak.json
    # (синхронное чтение — вызывать через asyncio.to_thread)
    ignore_file = data_dir() / f"api_{persona}" / "ignore_streak.json"
    if not ignore_file.is_file():
        return 0
    try:
        return int((json.loads(ignore_file.read_text(encoding="utf-8")) or {}).get(str(chat_id), 0))
    except Exception:
        return 0


@app.get("/api/personas/{persona}/initiative", dependencies=[Depends(require_auth)])
async def initiative(persona: PersonaIdPath, chat_id: str = "web_user"):
    bot = await _get_bot(persona)
    p = bot.proactive
    if p is None:
        # Менеджер не создан (проактивность выключена) — отдаём параметры
        # из YAML, чтобы вкладка показывала реальные значения и давала их
        # редактировать, а не мок.
        from app.api import settings_api
        cfg = await asyncio.to_thread(settings_api.get_persona_proactive, persona)
        if cfg is None:
            raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
        # streak читаем из файла: настроение может быть испорчено заморозкой
        # даже при выключенной проактивности (фронт считает ступень обиды из него).
        # Синхронное чтение — в поток пула, не в event loop
        streak = await asyncio.to_thread(_read_ignore_streak, persona, chat_id)
        return {
            "enabled": bool(cfg.get("enabled", False)),
            # В YAML могут остаться значения выше суточного максимума — показываем
            # эффективный порог (рантайм всё равно обрезает его до суток)
            "silence_threshold_minutes": min(1440, int(cfg.get("silence_threshold_minutes", 180))),
            "check_interval_minutes": int(cfg.get("check_interval_minutes", 30)),
            "initiative_probability": float(cfg.get("initiative_probability", 0.3)),
            "max_daily_initiatives": int(cfg.get("max_daily_initiatives", 5)),
            "adaptive_threshold": bool(cfg.get("adaptive_threshold", True)),
            "feedback_enabled": bool(cfg.get("feedback_enabled", True)),
            # окно самоинициативы "HH:MM-HH:MM" (нет — круглые сутки)
            "initiative_hours": cfg.get("initiative_hours"),
            "ignore_streak": streak,
            # Порог молчания, после которого персона пишет сама (менеджера
            # нет — адаптивный не посчитать, отдаём заданный)
            "effective_silence_minutes": min(1440, int(cfg.get("silence_threshold_minutes", 180))),
            "initiatives_today": 0,
            "emotional_state": "",
            "history": [],
        }

    def _read():
        cfg = p.config
        key = str(chat_id)
        history = getattr(p, "_initiative_history", {}) or {}
        return {
            "enabled": cfg.enabled,
            # Живой конфиг может содержать значение выше суточного максимума
            # (старый YAML) — рантайм обрезает до суток, показываем эффективное
            "silence_threshold_minutes": min(1440, cfg.silence_threshold_minutes),
            "check_interval_minutes": cfg.check_interval_minutes,
            "initiative_probability": cfg.initiative_probability,
            "max_daily_initiatives": cfg.max_daily_initiatives,
            "adaptive_threshold": cfg.adaptive_threshold,
            "feedback_enabled": cfg.feedback_enabled,
            "initiative_hours": "-".join(cfg.initiative_hours) if cfg.initiative_hours else None,
            "ignore_streak": (getattr(p, "_ignore_streak", {}) or {}).get(key, 0),
            # Порог молчания, после которого персона пишет сама: адаптивный
            # (две медианы интервала между репликами) или заданный
            "effective_silence_minutes": round(min(1440, p._calculate_adaptive_threshold(key, log=False))),
            "initiatives_today": p._get_daily_count(key),
            "emotional_state": p._get_emotional_state(key),
            "history": list(history.get(key, []))[-20:],
        }

    return await asyncio.to_thread(_read)


@app.put("/api/personas/{persona}/initiative", dependencies=[Depends(require_auth)])
async def initiative_update(persona: PersonaIdPath, req: InitiativeUpdate):
    # Записать параметры проактивности в YAML персоны и в живой конфиг
    from app.api import settings_api
    # Только присланные поля; null имеет смысл лишь у окна часов — «снять окно»
    sent = req.model_dump(exclude_unset=True)
    patch = {k: v for k, v in sent.items() if v is not None or k == "initiative_hours"}
    result = await asyncio.to_thread(settings_api.update_persona_proactive, persona, patch)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


# ── Настройки: провайдеры LLM и конфиг персоны ────────────────────────

@app.get("/api/providers", dependencies=[Depends(require_auth)])
async def providers_list():
    from app.api import settings_api
    return await asyncio.to_thread(settings_api.list_providers)


@app.get("/api/providers/local/status", dependencies=[Depends(require_auth)])
async def local_provider_status():
    # Свежая проверка локальной Ollama: сервер, настроенная модель, список моделей
    from app.api import settings_api
    return await asyncio.to_thread(settings_api.local_status)


@app.post("/api/providers/{provider}/keys", dependencies=[Depends(require_auth)])
async def provider_add_key(provider: str, req: ProviderKeyRequest):
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.add_provider_key, provider, req.key)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


@app.delete("/api/providers/{provider}/keys/{index}", dependencies=[Depends(require_auth)])
async def provider_delete_key(provider: str, index: int):
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.delete_provider_key, provider, index)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


@app.post("/api/providers/active", dependencies=[Depends(require_auth)])
async def provider_set_active(req: ActiveProviderRequest):
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.set_active_provider, req.provider)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


@app.put("/api/providers/{provider}/model", dependencies=[Depends(require_auth)])
async def provider_set_model(provider: str, req: ProviderModelRequest):
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.set_provider_model, provider, req.model)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


@app.put("/api/providers/webchat", dependencies=[Depends(require_auth)])
async def provider_set_webchat(req: WebchatRequest):
    from app.api import settings_api
    sites = req.sites if req.sites is not None else ([req.site] if req.site else [])
    result = await asyncio.to_thread(settings_api.set_webchat, sites)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


@app.post("/api/providers/webchat/test", dependencies=[Depends(require_auth)])
async def provider_test_webchat(req: WebchatRequest):
    # Проба веб-чата из настроек: «test» в свежий чат, ok — сайт ответил.
    # Долго (сайт может думать до ~90с) — в потоке, роутер не блокируем
    from app.api import settings_api
    return await asyncio.to_thread(settings_api.test_webchat, req.site)


@app.get("/api/personas/{persona}/local-tasks", dependencies=[Depends(require_auth)])
async def persona_local_tasks(persona: PersonaIdPath):
    # Движки служебных задач персоны (Ollama/веб-чат) + веб-чат фоновых задач
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.get_persona_local_tasks, persona)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    return result


@app.put("/api/personas/{persona}/local-tasks", dependencies=[Depends(require_auth)])
async def persona_local_tasks_update(persona: PersonaIdPath, req: PersonaLocalTasksUpdate):
    # Движок задачи (ollama | webchat | default) и/или веб-чат фоновых задач
    from app.api import settings_api
    result = await asyncio.to_thread(
        settings_api.update_persona_local_tasks, persona, req.task, req.backend,
        req.site, req.bg_site)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


@app.get("/api/settings/timezone", dependencies=[Depends(require_auth)])
async def timezone_get():
    from app.api import settings_api
    return await asyncio.to_thread(settings_api.get_timezone)


@app.put("/api/settings/timezone", dependencies=[Depends(require_auth)])
async def timezone_set(req: TimezoneRequest):
    # Невалидное имя зоны → 422, .env не трогается (settings_api.set_timezone)
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.set_timezone, req.timezone)
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result.get("detail") or "Некорректный часовой пояс")
    return result


@app.get("/api/settings/location", dependencies=[Depends(require_auth)])
async def location_get():
    from app.features import env_context
    return await asyncio.to_thread(env_context.load_location)


@app.post("/api/settings/location", dependencies=[Depends(require_auth)])
async def location_set(req: LocationRequest):
    from app.features import env_context
    if req.mode == "off":
        return await asyncio.to_thread(env_context.set_off)
    if req.mode == "manual":
        if not req.city or not req.city.strip():
            raise HTTPException(status_code=400, detail="Укажите город")
        result = await asyncio.to_thread(env_context.set_manual_city, req.city)
        if result is None:
            raise HTTPException(status_code=400, detail=f"Город '{req.city}' не найден")
        return result
    if req.mode == "geo":
        if req.lat is None or req.lon is None:
            raise HTTPException(status_code=400, detail="Нет координат от браузера")
        result = await asyncio.to_thread(env_context.set_geo, req.lat, req.lon)
        if result is None:
            raise HTTPException(status_code=400, detail="Не удалось определить местоположение")
        return result
    raise HTTPException(status_code=400, detail=f"Неизвестный режим '{req.mode}'")


@app.get("/api/settings/env-preview", dependencies=[Depends(require_auth)])
async def env_preview():
    # Точная строка окружения, которая уйдёт в контекст персон (None — выкл)
    from app.features import env_context
    line = await asyncio.to_thread(env_context.get_env_line)
    return {"line": line, "location": env_context.load_location()}


@app.get("/api/personas/{persona}/config", dependencies=[Depends(require_auth)])
async def persona_config(persona: PersonaIdPath):
    from app.api import settings_api
    cfg = await asyncio.to_thread(settings_api.get_persona_config, persona)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    return cfg


@app.get("/api/personas/{persona}/yaml", dependencies=[Depends(require_auth)])
async def persona_yaml(persona: PersonaIdPath):
    # Сырой YAML-файл персоны (просмотр из топбара, read-only)
    if persona not in list_personas():
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    path = runtime.persona_yaml_path(persona)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    # Синхронное чтение файла — в поток пула: иначе на время I/O блокирует
    # весь event loop
    yaml_text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    return {"persona": persona, "yaml": yaml_text}


@app.put("/api/personas/{persona}/yaml", dependencies=[Depends(require_auth)])
async def persona_yaml_update(persona: PersonaIdPath, req: PersonaYamlUpdate):
    # Записать отредактированный YAML персоны (с валидацией)
    if persona not in list_personas():
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.save_persona_yaml, persona, req.yaml)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


@app.put("/api/personas/{persona}/config", dependencies=[Depends(require_auth)])
async def persona_config_update(persona: PersonaIdPath, req: PersonaConfigUpdate):
    from app.api import settings_api
    result = await asyncio.to_thread(
        settings_api.update_persona_config, persona, req.settings, req.stm_size, req.features,
        # exclude_unset: частичный патч llm (только models или fallback) не
        # должен приходить с primary=None — это сняло бы закрепление провайдера
        req.llm.model_dump(exclude_unset=True) if req.llm else None,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    return {"status": "ok", **result}


# ── Черновики новых персон (модалка создания) ──


@app.get("/api/persona-drafts", dependencies=[Depends(require_auth)])
async def persona_drafts():
    # Все черновики целиком (form + yaml), свежие сверху
    from app.api import drafts_api
    return {"drafts": await asyncio.to_thread(drafts_api.list_drafts)}


@app.post("/api/persona-drafts", dependencies=[Depends(require_auth)])
async def persona_draft_save(req: PersonaDraftSave):
    # Создать (id=None) или обновить черновик
    from app.api import drafts_api
    draft = await asyncio.to_thread(drafts_api.save_draft, req.id, req.name, req.form, req.yaml)
    if draft is None:
        raise HTTPException(status_code=400, detail="Недопустимый id черновика")
    return draft


@app.delete("/api/persona-drafts/{draft_id}", dependencies=[Depends(require_auth)])
async def persona_draft_delete(draft_id: str):
    from app.api import drafts_api
    if not await asyncio.to_thread(drafts_api.delete_draft, draft_id):
        raise HTTPException(status_code=404, detail="Черновик не найден")
    return {"status": "ok"}

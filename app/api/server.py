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
import json
import logging
import os
import re
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api import runtime
from app.api.runtime import chat_lock, get_persona_info, list_personas
from app.api.schemas import (
    ActiveProviderRequest,
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
    MemoryStats,
    PersonaConfigUpdate,
    PersonaDraftSave,
    PersonaInfo,
    PersonaYamlUpdate,
    ProviderKeyRequest,
    ProviderModelRequest,
    LocalBackendRequest,
    ReminderAddRequest,
    StmDeleteRequest,
    StmTrimRequest,
    TodoAddRequest,
    WebchatRequest,
)
from app.core.file_reader import extract_text
from app.core.message_pacing import send_delay

logger = logging.getLogger(__name__)

# Префиксы, которыми extract_text сообщает об ошибке
_EXTRACT_ERROR_PREFIXES = ("Ошибка", "Формат", "Не удалось", "Библиотека")

app = FastAPI(title="Virtual Persona API", version="1.0")

# Буфер логов для режима разработчика (GET /api/logs)
from app.api import log_buffer
log_buffer.install()

_cors_origins = os.getenv("API_CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

_bearer = HTTPBearer(auto_error=False)
_api_token = os.getenv("API_TOKEN", "")

# Чаты, где прямо сейчас идёт генерация ответа: "persona:chat_key".
# Inbox отдаёт это фронту — индикатор «печатает» переживает перезагрузку
# страницы (запрос-то на сервере продолжается).
_generating: set[str] = set()


async def require_auth(credentials: HTTPAuthorizationCredentials = Depends(_bearer)):
    """Bearer-авторизация. Активна только когда задан API_TOKEN."""
    if not _api_token:
        return
    if credentials is None or credentials.credentials != _api_token:
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий токен")


async def _get_bot(persona: str):
    """Бот персоны или 404. Создание инстанса блокирующее — в потоке."""
    bot = await asyncio.to_thread(runtime.registry.get, persona)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    return bot


def _check_not_muted(bot, persona: str):
    """Замороженная персона (features.muted) не отвечает: 409, сообщение не принимается
    (в STM оно не пишется — персона полностью «выключена»)."""
    if (bot.features or {}).get("muted"):
        raise HTTPException(status_code=409,
                            detail=f"Персона '{persona}' заморожена и молчит, пока её не разморозят")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/logs", dependencies=[Depends(require_auth)])
async def get_logs(since: int = 0, limit: int = 500):
    """Инкрементальная лента логов ядра (режим разработчика в веб-UI)."""
    return log_buffer.since(since, limit)


@app.get("/api/personas", response_model=list[PersonaInfo],
         dependencies=[Depends(require_auth)])
async def personas():
    return [get_persona_info(name) for name in list_personas()]


@app.post("/api/personas", dependencies=[Depends(require_auth)])
async def persona_create(req: PersonaYamlUpdate):
    """Создать новую персону из YAML (имя файла = поле id из YAML)."""
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.create_persona, req.yaml)
    if not result["ok"]:
        raise HTTPException(status_code=409 if result.get("conflict") else 400,
                            detail=result["detail"])
    return result


@app.delete("/api/personas/{persona}", dependencies=[Depends(require_auth)])
async def persona_delete(persona: str):
    """Удалить персону: YAML-файл + выгрузка из реестра. Память остаётся на диске."""
    from app.api import settings_api
    if not await asyncio.to_thread(settings_api.delete_persona, persona):
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    return {"status": "ok"}


@app.post("/api/personas/{persona}/duplicate", dependencies=[Depends(require_auth)])
async def persona_duplicate(persona: str):
    """Копия YAML персоны с новым id/name ({id}_copy, имя + «(копия)»)."""
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.duplicate_persona, persona)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    return result


@app.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(require_auth)])
async def chat(req: ChatRequest):
    bot = await _get_bot(req.persona)
    _check_not_muted(bot, req.persona)
    # Лок на (персона, чат): сериализуем сообщения одного чата,
    # разные чаты и персоны обрабатываются параллельно.
    lock_key = f"{req.persona}:{req.chat_id or req.user_id}"
    _generating.add(lock_key)
    try:
        async with chat_lock(lock_key):
            cmd = _try_slash_command(bot, req)
            if cmd is not None:
                # Слэш-команда: без process_message (как в TG — отдельные хендлеры)
                reply, llm_used = cmd
                provider, model = _answer_provider(bot) if llm_used else (None, None)
                split_extra = bot.pop_pending_split_messages(req.chat_id)
            else:
                llm_input = (
                    await asyncio.to_thread(_prepare_image_input, bot, req.message, req.image)
                    if req.image else req.message
                )
                reply = await asyncio.to_thread(
                    bot.process_message,
                    llm_input,
                    user_id=req.user_id,
                    chat_id=req.chat_id,
                    user_name=req.user_name,
                    reply_context=req.reply_context,
                )
                # Хвост расщеплённого ответа (settings.split_messages) забираем
                # до правки STM картинки — переписываемый хвост включает его части
                split_extra = bot.pop_pending_split_messages(req.chat_id)
                if req.image:
                    cap = req.message.strip()
                    await asyncio.to_thread(
                        _rewrite_image_stm, bot, req.chat_id or req.user_id, req.user_id,
                        f"📷 {cap}" if cap else "📷 (изображение)", [reply] + split_extra,
                    )
                provider, model = _answer_provider(bot)
            # Части расщеплённого ответа идут раньше досылаемых списков:
            # это продолжение реплики, а список — приложение к ней
            extra = split_extra + bot.pop_pending_list_messages(req.chat_id)
            question_kind = bot.pop_pending_question_kind(req.chat_id)
    finally:
        _generating.discard(lock_key)
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
    )


def _answer_provider(bot) -> tuple[str | None, str | None]:
    """Провайдер и модель, реально давшие ответ (с учётом fallback-цепочки
    и персональных override модели)."""
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
    """base64 или dataURL («data:image/...;base64,...») → байты изображения."""
    import base64
    try:
        if data.startswith("data:"):
            _, _, data = data.partition(",")
        return base64.b64decode(data)
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректные данные изображения (base64)")


def _prepare_image_input(bot, message: str, image_b64: str) -> str:
    """Текст для LLM из сообщения с картинкой: подпись + содержимое по
    vision-модели. Каскад как у TG-бота: vision-провайдер основного роутера
    → локальная gemma (OCR)."""
    image_bytes = _decode_image(image_b64)
    question = message.strip()
    ocr = bot.describe_image(image_bytes, question)
    if not ocr and getattr(bot, "_local_router", None) and bot._local_router.is_available():
        ocr = bot._local_router.ocr_image(image_bytes, question)
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
        msgs = bot.memory.stm.get_messages(chat_id=chat_key)
        # Хвост STM: [синтетическое user-сообщение, часть1, ..., частьN] —
        # считаем завершающую серию assistant-частей и переписываем её
        # вместе с user-сообщением перед ней
        n_assist = 0
        for m in reversed(msgs):
            if m.get("role") != "assistant":
                break
            n_assist += 1
        n_pop = min(n_assist + 1, len(msgs))
        bot.memory.stm.pop_last_n(n_pop, chat_key)
        bot.memory.stm.add_message("user", display_text, user_id, chat_key)
        for part in reply_parts:
            bot.memory.stm.add_message("assistant", part, user_id, chat_key)
    except Exception as e:
        logger.warning(f"Не удалось переписать STM для изображения: {e}")


# Слэш-команды веб-чата (зеркало TG): /learn, /remind, /add_todo,
# /add_inventory — через общий ядровой _dispatch_command (создаёт сущность
# и отвечает в образе персоны, пишет пару в STM); /web, /todo, /reminders,
# /cancel_reminder, /inventory, /help — утилитарные, без LLM.
_SLASH_DISPATCH = {
    "learn": "learn",
    "remind": "remind",
    "add_todo": "todo",
    "add_inventory": "inventory",
}

_SLASH_HELP = (
    "Команды:\n"
    "/learn <тема> — начать обучение\n"
    "/remind <что> [через N / в HH:MM] — напомнить\n"
    "/web — вкл/выкл веб-поиск\n"
    "/todo — список дел · /add_todo <задача>\n"
    "/reminders — активные напоминания · /cancel_reminder N\n"
    "/inventory — инвентарь · /add_inventory <предмет>[: описание]\n"
    "/help — этот список"
)


def _try_slash_command(bot, req: ChatRequest) -> tuple[str, bool] | None:
    """Перехват слэш-команды. Возвращает (ответ, был_ли_llm) или None,
    если сообщение — не команда."""
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
        from app.features.reminder_manager import format_schedule
        lines = ["Активные напоминания:"]
        for i, r in enumerate(active):
            task = r.get("task") or "(без описания)"
            if r.get("recurrence"):
                when = format_schedule(r["recurrence"])
            else:
                remain = r["trigger_at"] - time.time()
                mins = int(remain / 60)
                when = f"через {mins} мин" if mins > 0 else f"через {int(remain)} сек"
            lines.append(f"{i + 1}. {task} — {when}")
        lines += ["", "Чтобы отменить: /cancel_reminder N"]
        return "\n".join(lines), False

    if cmd == "cancel_reminder":
        if not bot.reminder_manager:
            return "Напоминания не активны для этой персоны.", False
        try:
            idx = int(args) - 1
        except ValueError:
            return "Нужно число — номер напоминания из /reminders.", False
        ok = bot.reminder_manager.cancel_reminder(chat_id, idx)
        return ("Напоминание отменено." if ok else "Напоминание с таким номером не найдено."), False

    if cmd == "inventory":
        if args:
            return _try_slash_command(
                bot, req.model_copy(update={"message": f"/add_inventory {args}"})
            )
        if not bot.inventory_manager:
            return "Инвентарь не активен для этой персоны.", False
        return bot.inventory_manager.get_list_text(), False

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
    loop = asyncio.get_running_loop()
    gen_key = f"{req.persona}:{req.chat_id or req.user_id}"
    lock = chat_lock(gen_key)
    q: asyncio.Queue = asyncio.Queue()

    def _run():
        _generating.add(gen_key)
        try:
            cmd = _try_slash_command(bot, req)
            if cmd is not None:
                # Слэш-команда: без process_message (как в TG)
                reply, llm_used = cmd
                provider, model = _answer_provider(bot) if llm_used else (None, None)
                split_rest = bot.pop_pending_split_messages(req.chat_id)
            else:
                llm_input = _prepare_image_input(bot, req.message, req.image) if req.image else req.message
                reply = bot.process_message(
                    llm_input,
                    user_id=req.user_id,
                    chat_id=req.chat_id,
                    user_name=req.user_name,
                    reply_context=req.reply_context,
                )
                split_rest = bot.pop_pending_split_messages(req.chat_id)
                if req.image:
                    cap = req.message.strip()
                    _rewrite_image_stm(
                        bot, req.chat_id or req.user_id, req.user_id,
                        f"📷 {cap}" if cap else "📷 (изображение)", [reply] + split_rest,
                    )
                provider, model = _answer_provider(bot)
            # «Печать» финального текста: порции по несколько символов
            def _type_text(text: str):
                for i in range(0, len(text), 6):
                    loop.call_soon_threadsafe(q.put_nowait, {"token": text[i:i + 6]})
                    time.sleep(0.02)
            _type_text(reply)
            # Расщеплённый хвост — отдельные пузыри: part_break велит фронту
            # начать новое сообщение, дальше части печатаются как обычно.
            # Пауза перед пузырём — как у TG-бота: растёт с длиной части
            for part in split_rest:
                loop.call_soon_threadsafe(q.put_nowait, {"part_break": True})
                time.sleep(send_delay(part))
                _type_text(part)
            payload = {
                "done": True,
                "reply": reply,
                "extra_messages": split_rest + bot.pop_pending_list_messages(req.chat_id),
                "question_kind": bot.pop_pending_question_kind(req.chat_id),
                "persona": req.persona,
                "chat_id": req.chat_id or req.user_id,
                "provider": provider,
                "model": model,
                # Режим управления после обработки сообщения (фронт гасит
                # дебаунс-паузу отправки для команд управления)
                "control_mode": bot.control_mode_on(req.chat_id or req.user_id),
            }
            loop.call_soon_threadsafe(q.put_nowait, payload)
        except Exception as e:
            loop.call_soon_threadsafe(q.put_nowait, {"error": str(e)})
        finally:
            # Клиент мог отключиться (перезагрузка) — флаг снимаем здесь,
            # когда серверная генерация реально завершилась
            _generating.discard(gen_key)

    async def _gen():
        await lock.acquire()
        asyncio.create_task(asyncio.to_thread(_run))
        try:
            while True:
                event = await q.get()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("done") or event.get("error"):
                    break
        finally:
            lock.release()

    return StreamingResponse(_gen(), media_type="text/event-stream")


@app.get("/api/chat/history", response_model=list[HistoryMessage],
         dependencies=[Depends(require_auth)])
async def chat_history(
    persona: str,
    user_id: str = "web_user",
    chat_id: str | None = None,
):
    bot = await _get_bot(persona)
    messages = await asyncio.to_thread(
        bot.memory.stm.get_messages, user_id=user_id, chat_id=chat_id
    )
    return messages


@app.post("/api/chat/clear", dependencies=[Depends(require_auth)])
async def chat_clear(req: ClearChatRequest):
    """Полный сброс переписки персоны: STM диалога + LTM-факты пользователя
    + дневник персоны (self_memory) + история самоинициатив чата.
    Перед сбросом делается снапшот в корзину
    (data/api_{persona}/clear_backups/, 7 дней) — см. эндпоинт restore."""
    from app.api import clear_backup
    bot = await _get_bot(req.persona)
    chat_key = req.chat_id or req.user_id
    # Снапшот ДО удаления — на случай ошибочной очистки
    stm_msgs = await asyncio.to_thread(bot.memory.stm.get_messages, None, chat_key)
    ltm_facts = await asyncio.to_thread(bot.memory.ltm.get_all_facts_with_meta, req.user_id)
    diary_state = bot.self_memory.export_state() if bot.self_memory else None
    initiatives = await asyncio.to_thread(_pop_initiative_history, bot, req.persona, chat_key)
    daily_stats = await asyncio.to_thread(_pop_daily_stats, bot, req.persona, chat_key)
    last_activity = await asyncio.to_thread(_pop_last_activity, bot, req.persona, chat_key)
    await asyncio.to_thread(
        clear_backup.make_backup, req.persona, req.user_id, chat_key,
        stm_msgs, ltm_facts, diary_state, initiatives, daily_stats, last_activity,
    )
    await asyncio.to_thread(bot.memory.clear_stm, chat_key)
    await asyncio.to_thread(bot.memory.clear_ltm, req.user_id)
    if bot.self_memory:
        await asyncio.to_thread(bot.self_memory.clear_all)
    # Метка свежести переписки — производная STM: сбрасываем вместе с ней
    # (в корзину не кладём: перепишется первым же новым сообщением)
    await asyncio.to_thread(
        _pop_json_key, Path(f"data/api_{req.persona}/last_message.json"), chat_key
    )
    return {"status": "ok"}


def _initiative_history_path(persona: str) -> Path:
    return Path(f"data/api_{persona}/initiative_history.json")


def _proactive_stats_path(persona: str) -> Path:
    return Path(f"data/api_{persona}/proactive_stats.json")


def _pop_json_key(path: Path, key: str):
    """Удалить ключ из json-словаря на диске, вернуть удалённое значение."""
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
    """Вернуть ключ в json-словарь на диске (восстановление из корзины)."""
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
    """Вернуть историю самоинициатив из снапшота корзины (менеджер или файл)."""
    if not entries:
        return
    p = bot.proactive
    if p is not None:
        p.restore_history(chat_key, entries)
        return
    _restore_json_key(_initiative_history_path(persona), chat_key, list(entries))


def _pop_daily_stats(bot, persona: str, chat_key: str) -> dict | None:
    """Забрать и удалить дневной счётчик инициатив («инициатив сегодня»)."""
    p = bot.proactive
    if p is not None:
        return p.pop_daily_stats(chat_key)
    removed = _pop_json_key(_proactive_stats_path(persona), chat_key)
    return removed if isinstance(removed, dict) else None


def _restore_daily_stats(bot, persona: str, chat_key: str, entry: dict):
    """Вернуть дневной счётчик инициатив из снапшота корзины."""
    if not entry:
        return
    p = bot.proactive
    if p is not None:
        p.restore_daily_stats(chat_key, entry)
        return
    _restore_json_key(_proactive_stats_path(persona), chat_key, dict(entry))


def _known_chats_path(persona: str) -> Path:
    return Path(f"data/api_{persona}/known_chats.json")


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
    """Вернуть метку последней активности из снапшота корзины."""
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
async def clear_backup_info(persona: str):
    """Есть ли в корзине снапшот последней очистки (для кнопки восстановления)."""
    if persona not in list_personas():
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    from app.api import clear_backup
    return await asyncio.to_thread(clear_backup.backup_info, persona)


@app.post("/api/personas/{persona}/clear-backup/restore", dependencies=[Depends(require_auth)])
async def clear_backup_restore(persona: str):
    """Восстановить STM/LTM/дневник/историю инициатив из свежего снапшота корзины (файл удаляется)."""
    from app.api import clear_backup
    bot = await _get_bot(persona)
    data = await asyncio.to_thread(clear_backup.pop_latest, persona)
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

    return {"status": "ok", "restored": restored}


@app.post("/api/chat/history/delete", dependencies=[Depends(require_auth)])
async def chat_history_delete(req: StmDeleteRequest):
    """Удалить одно сообщение из STM (поштучное удаление в досье)."""
    bot = await _get_bot(req.persona)
    chat_key = req.chat_id or req.user_id
    ok = await asyncio.to_thread(bot.memory.stm.delete_message, chat_key, req.index)
    if not ok:
        raise HTTPException(status_code=404, detail="Сообщение с таким индексом не найдено")
    return {"status": "ok"}


@app.post("/api/chat/history/trim", dependencies=[Depends(require_auth)])
async def chat_history_trim(req: StmTrimRequest):
    """Удалить последние N сообщений из STM (кнопка «Удалить» в досье)."""
    bot = await _get_bot(req.persona)
    chat_key = req.chat_id or req.user_id
    n = await asyncio.to_thread(bot.memory.stm.pop_last_n, req.count, chat_key)
    return {"status": "ok", "deleted": n}


# ── Память ────────────────────────────────────────────────────────────

@app.get("/api/personas/{persona}/memory/stats", response_model=MemoryStats,
         dependencies=[Depends(require_auth)])
async def memory_stats(persona: str, user_id: str = "web_user",
                       chat_id: str | None = None):
    bot = await _get_bot(persona)
    return await asyncio.to_thread(bot.get_memory_stats, user_id, chat_id)


@app.get("/api/personas/{persona}/memory/ltm", response_model=list[str],
         dependencies=[Depends(require_auth)])
async def memory_ltm(persona: str, user_id: str = "web_user"):
    bot = await _get_bot(persona)
    return await asyncio.to_thread(bot.memory.ltm.get_all_facts, user_id)


@app.get("/api/personas/{persona}/dossier", dependencies=[Depends(require_auth)])
async def persona_dossier(persona: str, chat_id: str = "web_user"):
    """Профиль досье чата: интересы/темы/наблюдения (анализ диалога — не LTM)."""
    bot = await _get_bot(persona)
    return await asyncio.to_thread(bot.get_dossier_snapshot, chat_id)


@app.post("/api/personas/{persona}/memory/facts", dependencies=[Depends(require_auth)])
async def memory_add_fact(persona: str, req: FactRequest):
    bot = await _get_bot(persona)
    await asyncio.to_thread(bot.inject_fact, req.fact, req.user_id)
    return {"status": "ok"}


@app.put("/api/personas/{persona}/memory/facts", dependencies=[Depends(require_auth)])
async def memory_update_fact(persona: str, req: FactUpdateRequest):
    """Замена факта отредактированным текстом (правка в досье)."""
    bot = await _get_bot(persona)
    new_text = req.new.strip()
    if not new_text:
        raise HTTPException(status_code=400, detail="Пустой текст факта")
    old = await asyncio.to_thread(bot.update_fact, req.old, new_text, req.user_id)
    if old is None:
        raise HTTPException(status_code=404, detail="Факт не найден")
    return {"status": "ok", "old": old}


@app.delete("/api/personas/{persona}/memory/facts", dependencies=[Depends(require_auth)])
async def memory_forget_fact(persona: str, query: str, user_id: str = "web_user"):
    bot = await _get_bot(persona)
    removed = await asyncio.to_thread(bot.forget_fact, query, user_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="Факт не найден")
    return {"status": "ok", "removed": removed}


@app.post("/api/personas/{persona}/memory/clear", dependencies=[Depends(require_auth)])
async def memory_clear(persona: str, user_id: str = "web_user",
                       chat_id: str | None = None):
    bot = await _get_bot(persona)
    await asyncio.to_thread(bot.clear_memory, user_id, chat_id)
    return {"status": "ok"}


# ── Файлы ─────────────────────────────────────────────────────────────

@app.post("/api/personas/{persona}/files", dependencies=[Depends(require_auth)])
async def upload_file(persona: str, file: UploadFile, user_id: str = "web_user"):
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
async def list_files(persona: str, user_id: str = "web_user"):
    bot = await _get_bot(persona)
    if bot.file_db is None:
        return {"files": []}
    files = await asyncio.to_thread(bot.file_db.list_files_detailed, user_id)
    return {"files": files}


@app.get("/api/personas/{persona}/files/{filename}/content", dependencies=[Depends(require_auth)])
async def file_content(persona: str, filename: str, user_id: str = "web_user"):
    bot = await _get_bot(persona)
    if bot.file_db is None:
        raise HTTPException(status_code=400, detail="Загрузка файлов недоступна для этой персоны")
    content = await asyncio.to_thread(bot.file_db.get_full_document, user_id, filename)
    if content is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    return {"filename": filename, "content": content}


@app.delete("/api/personas/{persona}/files/{filename}", dependencies=[Depends(require_auth)])
async def delete_file(persona: str, filename: str, user_id: str = "web_user"):
    bot = await _get_bot(persona)
    if bot.file_db is None:
        raise HTTPException(status_code=400, detail="Загрузка файлов недоступна для этой персоны")
    ok = await asyncio.to_thread(bot.file_db.remove_file, user_id, filename)
    if not ok:
        raise HTTPException(status_code=404, detail="Файл не найден")
    files = await asyncio.to_thread(bot.file_db.list_files_detailed, user_id)
    return {"files": files}


@app.delete("/api/personas/{persona}/files", dependencies=[Depends(require_auth)])
async def reset_files(persona: str, user_id: str = "web_user"):
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
async def todo_list(persona: str, chat_id: str = "web_user"):
    bot = await _get_bot(persona)
    return {"items": await asyncio.to_thread(_todo_items, bot, chat_id)}


@app.post("/api/personas/{persona}/todo", dependencies=[Depends(require_auth)])
async def todo_add(persona: str, req: TodoAddRequest):
    bot = await _get_bot(persona)
    if bot.todo_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет модуля дел (todo: false)")
    await asyncio.to_thread(bot.todo_manager.add_item, req.chat_id, req.user_name, req.task)
    return {"items": await asyncio.to_thread(_todo_items, bot, req.chat_id)}


@app.delete("/api/personas/{persona}/todo", dependencies=[Depends(require_auth)])
async def todo_remove(persona: str, index: int, chat_id: str = "web_user"):
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
            "index": i + 1,  # индекс для отмены — внутри этого же списка
            "task": r.get("task") or "",
            "trigger_at": r.get("trigger_at"),
            "recurrence": r.get("recurrence"),
            "user_name": r.get("user_name") or "",
        }
        for i, r in enumerate(bot.reminder_manager.get_active(chat_id))
    ]


@app.get("/api/personas/{persona}/reminders", dependencies=[Depends(require_auth)])
async def reminders_list(persona: str, chat_id: str = "web_user"):
    bot = await _get_bot(persona)
    return {"items": await asyncio.to_thread(_reminders, bot, chat_id)}


@app.post("/api/personas/{persona}/reminders", dependencies=[Depends(require_auth)])
async def reminders_add(persona: str, req: ReminderAddRequest):
    bot = await _get_bot(persona)
    if bot.reminder_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет напоминаний (reminder: false)")
    await asyncio.to_thread(
        bot.reminder_manager.add_reminder,
        req.chat_id, req.user_name, req.task, req.delay_seconds,
    )
    return {"items": await asyncio.to_thread(_reminders, bot, req.chat_id)}


@app.delete("/api/personas/{persona}/reminders", dependencies=[Depends(require_auth)])
async def reminders_cancel(persona: str, index: int, chat_id: str = "web_user"):
    bot = await _get_bot(persona)
    if bot.reminder_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет напоминаний (reminder: false)")
    ok = await asyncio.to_thread(bot.reminder_manager.cancel_reminder, chat_id, index - 1)
    if not ok:
        raise HTTPException(status_code=404, detail="Напоминание не найдено")
    return {"items": await asyncio.to_thread(_reminders, bot, chat_id)}


# ── Инвентарь ─────────────────────────────────────────────────────────
# Инвентарь общий на персону (не на чат/пользователя).

@app.get("/api/personas/{persona}/inventory", dependencies=[Depends(require_auth)])
async def inventory_list(persona: str):
    bot = await _get_bot(persona)
    if bot.inventory_manager is None:
        return {"items": []}
    items = await asyncio.to_thread(
        lambda: [i.to_dict() for i in bot.inventory_manager.get_items()]
    )
    return {"items": items}


@app.post("/api/personas/{persona}/inventory", dependencies=[Depends(require_auth)])
async def inventory_add(persona: str, req: InventoryAddRequest):
    bot = await _get_bot(persona)
    if bot.inventory_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет инвентаря (inventory: false)")
    result = await asyncio.to_thread(
        bot.inventory_manager.add_item, req.name, req.description, req.source
    )
    return {"result": result}


@app.delete("/api/personas/{persona}/inventory", dependencies=[Depends(require_auth)])
async def inventory_remove(persona: str, name: str):
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
async def learning_list(persona: str, chat_id: str = "web_user"):
    bot = await _get_bot(persona)
    return {"sessions": await asyncio.to_thread(_learning_sessions, bot, chat_id)}


@app.post("/api/personas/{persona}/learning", dependencies=[Depends(require_auth)])
async def learning_start(persona: str, req: LearningStartRequest):
    bot = await _get_bot(persona)
    if bot.learning_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет обучения (learning: false)")

    def _start():
        # Двухшаговое создание курса: setup (тема) + commit (частота)
        bot.learning_manager.begin_setup(req.chat_id, req.subject, "web_user", req.user_name)
        return bot.learning_manager.commit_session(req.chat_id, req.interval_seconds)

    session = await asyncio.to_thread(_start)
    if session is None:
        raise HTTPException(status_code=400, detail="Не удалось создать курс")
    return {"sessions": await asyncio.to_thread(_learning_sessions, bot, req.chat_id)}


@app.delete("/api/personas/{persona}/learning", dependencies=[Depends(require_auth)])
async def learning_stop(persona: str, session_id: str, chat_id: str = "web_user"):
    bot = await _get_bot(persona)
    if bot.learning_manager is None:
        raise HTTPException(status_code=400, detail="У этой персоны нет обучения (learning: false)")
    await asyncio.to_thread(
        bot.learning_manager.stop_session, chat_id, session_id
    )
    return {"sessions": await asyncio.to_thread(_learning_sessions, bot, chat_id)}


# ── Дневник (self_memory) ─────────────────────────────────────────────

@app.get("/api/personas/{persona}/diary", dependencies=[Depends(require_auth)])
async def diary(persona: str):
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
async def living_state(persona: str, chat_id: str = "web_user"):
    """Текущее состояние персоны (energy/mood/pastime/location), сюжетные
    линии и лента последних событий — для вкладок комната/настроение."""
    bot = await _get_bot(persona)
    living = getattr(bot, "living", None)
    if living is None:
        return {"enabled": False, "ui_sync": False, "state": None}
    return await asyncio.to_thread(living.get_state_for_ui, chat_id)


# ── Inbox: фоновые сообщения (напоминания, уроки, инициативы) ─────────
@app.get("/api/personas/{persona}/inbox", dependencies=[Depends(require_auth)])
async def inbox(persona: str, chat_id: str = "web_user"):
    from app.api.inbox import inbox_pop
    from app.api.runtime import registry
    if get_persona_info(persona) is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    # Бота не создаём: inbox — фоновый опрос всех персон, не должен
    # инициализировать тяжёлые инстансы. Нет бота — нет и сообщений.
    # generating — идёт ли сейчас генерация ответа в этом чате (фронт
    # показывает «печатает» даже после перезагрузки страницы).
    generating = f"{persona}:{chat_id}" in _generating
    # Метка последнего сообщения чата (STM штампует её на каждое сообщение) —
    # фронт сортирует по ней список персон по свежести переписки
    last_ts = 0.0
    lm_file = Path(f"data/api_{persona}/last_message.json")
    if lm_file.is_file():
        try:
            lm_data = json.loads(lm_file.read_text(encoding="utf-8"))
            if isinstance(lm_data, dict):
                last_ts = float(lm_data.get(chat_id, 0) or 0)
        except Exception:
            last_ts = 0.0
    bot = registry._bots.get(persona)
    if bot is None:
        return {"messages": [], "generating": generating, "last_ts": last_ts,
                "control_mode": False}
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
            "last_ts": last_ts, "control_mode": control_mode}


# ── Инициатива (proactive) ────────────────────────────────────────────

@app.get("/api/personas/{persona}/initiative", dependencies=[Depends(require_auth)])
async def initiative(persona: str, chat_id: str = "web_user"):
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
        # даже при выключенной проактивности (фронт считает ступень обиды из него)
        streak = 0
        ignore_file = Path(f"data/api_{persona}/ignore_streak.json")
        if ignore_file.is_file():
            try:
                streak = int((json.loads(ignore_file.read_text(encoding="utf-8")) or {}).get(str(chat_id), 0))
            except Exception:
                streak = 0
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
            "initiatives_today": p._get_daily_count(key),
            "emotional_state": p._get_emotional_state(key),
            "history": list(history.get(key, []))[-20:],
        }

    return await asyncio.to_thread(_read)


@app.put("/api/personas/{persona}/initiative", dependencies=[Depends(require_auth)])
async def initiative_update(persona: str, req: InitiativeUpdate):
    """Записать параметры проактивности в YAML персоны и в живой конфиг."""
    from app.api import settings_api
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
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
    """Свежая проверка локальной Ollama: сервер, настроенная модель, список моделей."""
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


@app.put("/api/providers/local-tasks/{task}", dependencies=[Depends(require_auth)])
async def provider_set_local_task(task: str, req: LocalBackendRequest):
    """Движок локальной задачи (всё, что ходило в Ollama): ollama | webchat
    + сайт веб-чата для этой задачи."""
    from app.api import settings_api
    result = await asyncio.to_thread(settings_api.set_local_task, task, req.backend, req.site)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["detail"])
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
    """Точная строка окружения, которая уйдёт в контекст персон (None — выкл)."""
    from app.features import env_context
    line = await asyncio.to_thread(env_context.get_env_line)
    return {"line": line, "location": env_context.load_location()}


@app.get("/api/personas/{persona}/config", dependencies=[Depends(require_auth)])
async def persona_config(persona: str):
    from app.api import settings_api
    cfg = await asyncio.to_thread(settings_api.get_persona_config, persona)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    return cfg


@app.get("/api/personas/{persona}/yaml", dependencies=[Depends(require_auth)])
async def persona_yaml(persona: str):
    """Сырой YAML-файл персоны (просмотр из топбара, read-only)."""
    if persona not in list_personas():
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    path = runtime.PERSONAS_DIR / f"{persona}.yaml"
    return {"persona": persona, "yaml": path.read_text(encoding="utf-8")}


@app.put("/api/personas/{persona}/yaml", dependencies=[Depends(require_auth)])
async def persona_yaml_update(persona: str, req: PersonaYamlUpdate):
    """Записать отредактированный YAML персоны (с валидацией)."""
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
async def persona_config_update(persona: str, req: PersonaConfigUpdate):
    from app.api import settings_api
    result = await asyncio.to_thread(
        settings_api.update_persona_config, persona, req.settings, req.stm_size, req.features,
        req.llm.model_dump() if req.llm else None,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"Персона '{persona}' не найдена")
    return {"status": "ok", **result}


# ── Черновики новых персон (модалка создания) ──


@app.get("/api/persona-drafts", dependencies=[Depends(require_auth)])
async def persona_drafts():
    """Все черновики целиком (form + yaml), свежие сверху."""
    from app.api import drafts_api
    return {"drafts": await asyncio.to_thread(drafts_api.list_drafts)}


@app.post("/api/persona-drafts", dependencies=[Depends(require_auth)])
async def persona_draft_save(req: PersonaDraftSave):
    """Создать (id=None) или обновить черновик."""
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

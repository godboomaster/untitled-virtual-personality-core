# Pydantic-схемы запросов и ответов API.

from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.api.security import PersonaId, SafeId

# id персоны в теле запроса валидируется здесь же, на входе в схему (422 при
# нарушении формата) — до того, как строка вообще попадёт в бизнес-логику.
# Это не отменяет проверку в runtime.get_persona_info/settings_api (там —
# последний рубеж, на случай пути в обход pydantic), а дополняет её.


class PhoneActionReport(BaseModel):
    """Итог команды телефону («поставь будильник на 8», «открой Telegram»):
    исполняет само приложение на телефоне (web/src/phone/), ядру — только
    итог, из него app/features/phone_actions.py собирает заметку персоне."""
    kind: Literal["alarm", "timer", "alarms_list", "alarm_off", "open_app", "torch"]
    # ok — сделано; ask — не названо время/длительность, ничего не сделано;
    # no_handler — на телефоне нет приложения (часов) или фонарика;
    # unsupported_date — будильник на дату, а часы ставят только ближайшее время;
    # not_found / ambiguous — приложение не нашлось / подходят несколько;
    # busy — фонарик занят камерой; background — голосом при свёрнутом
    # приложении, а Android не дал открыть часы/приложение из фона
    status: Literal["ok", "ask", "no_handler", "unsupported_date", "not_found",
                    "ambiguous", "busy", "background", "failed"]
    hour: Optional[int] = Field(None, ge=0, le=23)
    minute: Optional[int] = Field(None, ge=0, le=59)
    # Дни повтора будильника: 1 — воскресенье … 7 — суббота (Calendar Android)
    days: list[int] = Field(default_factory=list, max_length=7)
    # Ближайшее срабатывание будильника — для unsupported_date
    nearest: Optional[Literal["today", "tomorrow"]] = None
    seconds: Optional[int] = Field(None, ge=1, le=86400)
    label: Optional[str] = Field(None, max_length=80)
    app: Optional[str] = Field(None, max_length=80)
    candidates: list[str] = Field(default_factory=list, max_length=6)
    on: Optional[bool] = None


class ChatRequest(BaseModel):
    persona: PersonaId
    message: str
    user_id: str = "web_user"
    chat_id: Optional[str] = None  # None → личный чат, буфер STM ключуется по user_id
    user_name: Optional[str] = None
    reply_context: Optional[str] = None
    image: Optional[str] = None  # картинка: base64 или dataURL («data:image/...;base64,...»)
    # camera — кадр с камеры: смотрит только локальная модель (как экран);
    # upload — обычное вложение, каскад vision-провайдер → локальная
    image_source: Literal["upload", "camera"] = "upload"
    # Реплика пришла из скина (SkinFrame): скин — сторонний код и мог отправить
    # её сам, поэтому в таком ходе режим управления недоступен
    # (BotInstance.process_message(from_skin=True))
    from_skin: bool = False
    # Команда телефону, уже исполненная приложением: персона отвечает по
    # итогу (заметка в промпт хода), сама ничего не исполняет
    phone_action: Optional[PhoneActionReport] = None
    # Клиент — приложение на телефоне: «открой файл X» присылает файл в чат,
    # а не открывает его на компьютере ядра (server._file_delivery)
    client: Optional[Literal["app"]] = None
    # Просьба с голосовой активации («Коннор, …»): одну фразу могли услышать
    # и компьютер ядра, и телефон — вторая копия получит duplicate
    voice: bool = False
    # Id устройства (тот же client_id, что у опроса inbox): пока ответа нет,
    # реплику показывают другие устройства, а это — и так показывает свою
    client_id: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_-]*$")


class VoiceWakeHold(BaseModel):
    """Пауза голосовой активации ядра (POST /api/voice/wake/hold), секунды."""
    seconds: float = Field(..., ge=0, le=600)


class OfflineMessage(BaseModel):
    """Реплика, написанная без ноутбука (приложение на телефоне)."""
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    role: Literal["user", "assistant"]
    text: str = Field(..., max_length=8000)
    ts: float
    # cloud — ответила облачная модель с телефона, local — Gemma на телефоне,
    # queued — ответа не было (только реплика пользователя)
    source: Optional[Literal["cloud", "local", "queued"]] = None


class OfflineImportRequest(BaseModel):
    user_id: str = "web_user"
    chat_id: Optional[str] = "web_user"
    user_name: Optional[str] = None
    messages: list[OfflineMessage] = Field(..., max_length=200)


class OfflineReminder(BaseModel):
    # Напоминание, поставленное на телефоне без ноутбука (offline_api.import_reminders)
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    task: str = Field("", max_length=500)
    at: float


class OfflineRemindersRequest(BaseModel):
    user_id: str = "web_user"
    chat_id: Optional[str] = "web_user"
    user_name: Optional[str] = None
    reminders: list[OfflineReminder] = Field(..., max_length=50)


class ChatResponse(BaseModel):
    reply: str
    extra_messages: list[str] = Field(default_factory=list)
    question_kind: Optional[str] = None
    persona: str
    chat_id: str
    provider: Optional[str] = None  # кто реально ответил (с учётом fallback)
    model: Optional[str] = None
    # Режим управления (computer control) после обработки сообщения: фронт
    # по нему отключает «реалистичную» паузу-дебаунс отправки — команды
    # управления должны уходить мгновенно
    control_mode: bool = False
    # Скриншоты страницы из режима управления («что на странице?») — dataURL
    images: list[str] = Field(default_factory=list)
    # Кнопки ответа на вопрос режима управления, которого ждёт чат
    # (BotInstance.cc_answer_options): {kind, options: [{label, send} | {role}]}
    answer_options: Optional[dict] = None
    # Карточка задачи агента (BotInstance.cc_task_card): цель, статус,
    # план заказа с отметками и журнал хода
    task: Optional[dict] = None
    # Файлы с компьютера ядра («пришли файл X»): {id, name, size, url} —
    # url отдаёт файл под тем же токеном (app/api/file_links)
    files: list[dict] = Field(default_factory=list)
    # Голосовая просьба, которую уже обрабатывает другое устройство
    # (ChatRequest.voice): reply пустой, озвучивать нечего
    duplicate: bool = False


class PresenceRequest(BaseModel):
    # Вкладка веб-чата видима и в фокусе — гейт фоновой активности бота
    active: bool
    # Чей именно чат открыт: гейт ключуется парой (персона, чат), иначе одна
    # вкладка морозила бы фон всех персон и всех чатов, включая Telegram
    # (см. app/core/presence.py). persona валидируется как id персоны —
    # строка уходит в ключ контекста api_{persona}.
    # Optional — старый фронт может слать запрос без persona: тогда запрос
    # просто принимается (200), но отметка присутствия не ставится
    # (см. app/api/server.py: presence).
    persona: Optional[PersonaId] = None
    chat_id: str = "web_user"


class PersonaInfo(BaseModel):
    id: str
    name: str
    description: str = ""
    color: Optional[str] = None  # цвет метки персоны (общий календарь и т.п.)
    features: dict = Field(default_factory=dict)
    settings: dict = Field(default_factory=dict)


class HistoryMessage(BaseModel):
    role: str
    content: str
    timestamp: Optional[float] = None  # unix-секунды
    user_name: Optional[str] = None
    sender_id: Optional[str] = None
    # Файлы, присланные этим ответом, пока ссылки живы (app/api/file_links)
    files: Optional[list[dict]] = None


# Части «Очистить диалог» (опасная зона досье): stm — переписка, ltm — факты,
# diary — дневник, initiatives — история/счётчик/отклик самоинициатив и метка
# активности, webchat — адреса веб-чатов LLM, остальные — срезы memory_wipe
ClearPart = Literal["stm", "ltm", "diary", "initiatives", "webchat", "todo",
                    "reminders", "dossier", "learning", "rhythm", "living", "control"]


class ClearChatRequest(BaseModel):
    persona: PersonaId
    chat_id: Optional[str] = None
    user_id: str = "web_user"
    # None — всё сразу (полная очистка); список — только эти части
    parts: Optional[list[ClearPart]] = Field(None, min_length=1)


class StmDeleteRequest(BaseModel):
    persona: PersonaId
    index: int  # позиция в буфере (порядок — как в /api/chat/history)
    chat_id: Optional[str] = None
    user_id: str = "web_user"
    # Сверка цели: буфер STM — deque(maxlen), индекс сдвигается при каждой
    # новой реплике. С content (и timestamp, если был в истории) сервер ищет
    # именно эту реплику, а index — лишь подсказка; не нашлась — 404
    content: Optional[str] = None
    timestamp: Optional[float] = None


class StmTrimRequest(BaseModel):
    persona: PersonaId
    count: int  # сколько последних сообщений удалить
    chat_id: Optional[str] = None
    user_id: str = "web_user"


class PersonaYamlUpdate(BaseModel):
    yaml: str  # новое содержимое YAML-файла персоны целиком


# Под id осталась память (удалённой персоны): keep — подхватить, fresh — убрать
# старую в архив data/api_<id>.archived-<время>; не задано — 409 с memory_exists
MemoryChoice = Literal["keep", "fresh"]


class PersonaCreateRequest(BaseModel):
    yaml: str  # YAML новой персоны целиком (имя файла = поле id)
    memory: Optional[MemoryChoice] = None


class PersonaRenameRequest(BaseModel):
    new_id: str  # новый id персоны = имя YAML-файла и папки памяти data/api_<id>
    memory: Optional[MemoryChoice] = None


class PersonaColorUpdate(BaseModel):
    color: Optional[str] = None  # "#rrggbb"; null — вернуть цвет по умолчанию (из id)


class PersonaAvatarUpdate(BaseModel):
    data_url: str  # data:image/png|jpeg|webp;base64,… (фронт сжимает до 256×256)


class MemoryStats(BaseModel):
    stm_count: int
    stm_max: int
    ltm_count: int


class FactRequest(BaseModel):
    fact: str
    user_id: str = "web_user"


class FactUpdateRequest(BaseModel):
    old: str  # исходный текст факта (как показан в UI)
    new: str  # новый текст после правки
    user_id: str = "web_user"


class FileInfo(BaseModel):
    filename: str


class TodoAddRequest(BaseModel):
    task: str
    chat_id: str = "web_user"
    user_name: str = "web"


class ReminderRecurrence(BaseModel):
    # Повтор напоминания: каждый день или по дням недели (0 — пн … 6 — вс).
    # hour/minute — по часам пользователя; не заданы — из срока напоминания
    type: Literal["daily", "weekly"]
    weekdays: Optional[list[int]] = None
    hour: Optional[int] = None
    minute: Optional[int] = None


class ReminderAddRequest(BaseModel):
    task: str
    delay_seconds: float = 3600
    # Повтор; первое срабатывание — ближайшее по расписанию не раньше delay
    recurrence: Optional[ReminderRecurrence] = None
    chat_id: str = "web_user"
    user_name: str = "web"


class ReminderUpdateRequest(BaseModel):
    # Правка напоминания по id: не заданное поле не меняется
    task: Optional[str] = None
    trigger_at: Optional[float] = None  # unix-секунды нового срабатывания
    active: Optional[bool] = None  # False — на паузу, True — продолжить
    # Повтор: не передан — прежний; явный null — снять (станет разовым)
    recurrence: Optional[ReminderRecurrence] = None
    chat_id: str = "web_user"


class CalendarEntryCreate(BaseModel):
    # Новая запись общего календаря.
    title: str
    date: str  # YYYY-MM-DD
    time: Optional[str] = None  # HH:MM
    kind: str = "note"  # todo | reminder | note | event
    persona: Optional[PersonaId] = None  # id персоны-владельца записи
    user_name: str = "web"
    note: str = ""


class CalendarEntryUpdate(BaseModel):
    # Патч записи календаря (все поля опциональны).
    title: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    kind: Optional[str] = None
    persona: Optional[PersonaId] = None
    note: Optional[str] = None
    done: Optional[bool] = None


class InventoryAddRequest(BaseModel):
    name: str
    description: str = ""
    source: str = "web"


class LearningStartRequest(BaseModel):
    subject: str
    interval_seconds: float = 86400
    chat_id: str = "web_user"
    user_name: str = "web"


class ProviderKeyRequest(BaseModel):
    key: str


class ProviderModelRequest(BaseModel):
    model: str


class ActiveProviderRequest(BaseModel):
    provider: str


class WebchatRequest(BaseModel):
    sites: list[str] | None = None  # список сайтов веб-чата в порядке перебора; [] — выкл
    site: str | None = None  # legacy: один сайт веб-чата; ""/off — выкл


class PersonaLocalTasksUpdate(BaseModel):
    # Движки служебных задач персоны (llm.local_tasks): одна задача и/или
    # веб-чат фоновых задач
    task: str | None = None  # id задачи из LOCAL_TASKS
    backend: str | None = None  # "ollama" | "webchat" | "default" (снять выбор)
    site: str | None = None  # сайт веб-чата задачи; пусто — веб-чат фоновых задач
    bg_site: str | None = None  # "fallback" | "primary" | имя сайта


class PersonaLlmConfig(BaseModel):
    primary: Optional[str] = None   # None → глобальный активный провайдер
    fallback: Optional[list[str]] = None  # приоритет цепочки после основного
    # Провайдеры, убранные персоной из СВОЕЙ автоматической цепочки (токены
    # как в primary/fallback); пустой список — исключений нет
    exclude: Optional[list[str]] = None
    models: Optional[dict[str, str]] = None  # свои модели по провайдерам (пустая строка — снять)
    # Лимиты веб-чатов: {сайт: {"enabled": bool, "per_hour": int}}.
    # Поле должно быть объявлено явно: иначе pydantic молча отбрасывает
    # webchat_limits из запроса, и отключение лимита в UI не сохраняется.
    webchat_limits: Optional[dict[str, dict]] = None
    # Провайдеры по назначению (реплики персоны в режиме управления /
    # решения режима управления / зрение); None/пустая строка — снять.
    # Хендлер передаёт только присланные поля (model_dump(exclude_unset=True)):
    # не присланный ключ не трогается
    answer_provider: Optional[str] = None
    cc_provider: Optional[str] = None
    vision_provider: Optional[str] = None


class InitiativeUpdate(BaseModel):
    # Патч параметров проактивности (все поля опциональны).
    enabled: Optional[bool] = None
    silence_threshold_minutes: Optional[int] = None
    check_interval_minutes: Optional[int] = None
    initiative_probability: Optional[float] = None
    max_daily_initiatives: Optional[int] = None
    adaptive_threshold: Optional[bool] = None
    feedback_enabled: Optional[bool] = None
    # Окно самоинициативы "HH:MM-HH:MM"; явный null — снять окно (круглые сутки)
    initiative_hours: Optional[str] = None


class PersonaConfigUpdate(BaseModel):
    settings: Optional[dict] = None
    stm_size: Optional[int] = None
    features: Optional[dict] = None
    llm: Optional[PersonaLlmConfig] = None


class TimezoneRequest(BaseModel):
    # Часовой пояс пользователя (app/core/timeutil): IANA-имя зоны;
    # пустая строка — сброс на системный пояс машины.
    timezone: str = ""


class LocationRequest(BaseModel):
    """Настройка местоположения пользователя (для окружения: время/погода).

    mode: "off" | "manual" (нужен city) | "geo" (нужны lat/lon от браузера).
    """
    mode: str
    city: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


class PersonaDraftSave(BaseModel):
    # Сохранение черновика новой персоны (id=None → создать новый).
    id: Optional[SafeId] = None
    name: str = ""
    form: dict = Field(default_factory=dict)  # непрозрачное состояние формы фронта
    yaml: str = ""  # снапшот сгенерированного YAML на момент сохранения


# ── Комната персоны (веб «Комната», app/api/room_api.py) ──────────────
# Частичные обновления: поле не передано — не трогаем, null — удалить/
# сбросить (различаем через model_dump(exclude_unset=True)).

class RoomLayoutUpdate(BaseModel):
    # {"<имя предмета>": {marker, size, icon, image, spot, hidden} | null}
    items: Optional[dict[str, Optional[dict]]] = None
    avatar: Optional[dict] = None  # {head, eyes, accessory, shade} | null


class RoomStyleUpdate(BaseModel):
    description: Optional[str] = Field(default=None, max_length=1500)
    reference: Optional[str] = None  # data-URL (≤ 1 МБ) | null


class RoomStyleDescribeRequest(BaseModel):
    reference: str  # data-URL картинки-референса (≤ 1 МБ)


class RoomArtUpdate(BaseModel):
    sprite: Optional[dict] = None                     # {dataUrl, anchor} | null
    sprites: Optional[dict[str, Optional[dict]]] = None  # {поза: спрайт | null}
    room_bg: Optional[dict] = None                    # {dataUrl, floorPoints} | null


class RoomPokeRequest(BaseModel):
    chat_id: Optional[str] = "auto"


class RoomFocusRequest(BaseModel):
    action: str  # start | end
    minutes: Optional[int] = None
    chat_id: Optional[str] = "auto"

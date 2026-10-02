# Pydantic-схемы запросов и ответов API.

from typing import Optional

from pydantic import BaseModel, Field

from app.api.security import PersonaId, SafeId

# id персоны в теле запроса валидируется здесь же, на входе в схему (422 при
# нарушении формата) — до того, как строка вообще попадёт в бизнес-логику.
# Это не отменяет проверку в runtime.get_persona_info/settings_api (там —
# последний рубеж, на случай пути в обход pydantic), а дополняет её.


class ChatRequest(BaseModel):
    persona: PersonaId
    message: str
    user_id: str = "web_user"
    chat_id: Optional[str] = None  # None → личный чат, буфер STM ключуется по user_id
    user_name: Optional[str] = None
    reply_context: Optional[str] = None
    image: Optional[str] = None  # картинка: base64 или dataURL («data:image/...;base64,...»)


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


class ClearChatRequest(BaseModel):
    persona: PersonaId
    chat_id: Optional[str] = None
    user_id: str = "web_user"


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


class PersonaRenameRequest(BaseModel):
    new_id: str  # новый id персоны = имя YAML-файла и папки памяти data/api_<id>


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


class ReminderAddRequest(BaseModel):
    task: str
    delay_seconds: float = 3600
    chat_id: str = "web_user"
    user_name: str = "web"


class ReminderUpdateRequest(BaseModel):
    # Правка напоминания по id: не заданное поле не меняется
    task: Optional[str] = None
    trigger_at: Optional[float] = None  # unix-секунды нового срабатывания
    active: Optional[bool] = None  # False — на паузу, True — продолжить
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

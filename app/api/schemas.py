"""Pydantic-схемы запросов и ответов API."""

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
    # вкладка морозила фон всех персон и всех чатов, включая Telegram-чаты
    # (см. app/core/presence.py). persona валидируется как id персоны —
    # строка уходит в ключ контекста api_{persona}.
    # Optional — старый фронт мог слать запрос вовсе без persona (до того,
    # как гейт стали ключевать парой персона+чат); раньше это было 422 без
    # объяснения на стороне фронта. Без persona запрос просто принимается
    # (200), отметка не ставится — см. app/api/server.py: presence.
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


class StmTrimRequest(BaseModel):
    persona: PersonaId
    count: int  # сколько последних сообщений удалить
    chat_id: Optional[str] = None
    user_id: str = "web_user"


class PersonaYamlUpdate(BaseModel):
    yaml: str  # новое содержимое YAML-файла персоны целиком


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


class CalendarEntryCreate(BaseModel):
    """Новая запись общего календаря."""
    title: str
    date: str  # YYYY-MM-DD
    time: Optional[str] = None  # HH:MM
    kind: str = "note"  # todo | reminder | note | event
    persona: Optional[PersonaId] = None  # id персоны-владельца записи
    user_name: str = "web"
    note: str = ""


class CalendarEntryUpdate(BaseModel):
    """Патч записи календаря (все поля опциональны)."""
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
    sites: list[str] | None = None  # ["qwen", "deepseek"] в порядке перебора; [] — выкл
    site: str | None = None  # legacy: один сайт (deepseek|qwen|claude|zai|chatgpt); ""/off — выкл


class LocalBackendRequest(BaseModel):
    backend: str  # движок задачи: "ollama" | "webchat"
    site: str | None = None  # сайт веб-чата для задачи; пусто — первый включённый


class PersonaLlmConfig(BaseModel):
    primary: Optional[str] = None   # None → глобальный активный провайдер
    fallback: Optional[list[str]] = None  # приоритет цепочки после основного
    models: Optional[dict[str, str]] = None  # свои модели по провайдерам (пустая строка — снять)
    # Лимиты веб-чатов: {сайт: {"enabled": bool, "per_hour": int}} —
    # enabled:false — лимит снят. Без этого поля pydantic молча отбрасывал
    # webchat_limits из запроса, и отключение лимита в UI не сохранялось
    webchat_limits: Optional[dict[str, dict]] = None


class InitiativeUpdate(BaseModel):
    """Патч параметров проактивности (все поля опциональны)."""
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
    """Часовой пояс пользователя (app/core/timeutil): IANA-имя зоны;
    пустая строка — сброс на системный пояс машины."""
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
    """Сохранение черновика новой персоны (id=None → создать новый)."""
    id: Optional[SafeId] = None
    name: str = ""
    form: dict = Field(default_factory=dict)  # непрозрачное состояние формы фронта
    yaml: str = ""  # снапшот сгенерированного YAML на момент сохранения

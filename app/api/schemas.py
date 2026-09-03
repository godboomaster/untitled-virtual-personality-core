"""Pydantic-схемы запросов и ответов API."""

from typing import Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    persona: str
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


class PersonaInfo(BaseModel):
    id: str
    name: str
    description: str = ""
    features: dict = Field(default_factory=dict)
    settings: dict = Field(default_factory=dict)


class HistoryMessage(BaseModel):
    role: str
    content: str
    timestamp: Optional[float] = None  # unix-секунды
    user_name: Optional[str] = None
    sender_id: Optional[str] = None


class ClearChatRequest(BaseModel):
    persona: str
    chat_id: Optional[str] = None
    user_id: str = "web_user"


class StmDeleteRequest(BaseModel):
    persona: str
    index: int  # позиция в буфере (порядок — как в /api/chat/history)
    chat_id: Optional[str] = None
    user_id: str = "web_user"


class StmTrimRequest(BaseModel):
    persona: str
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
    id: Optional[str] = None
    name: str = ""
    form: dict = Field(default_factory=dict)  # непрозрачное состояние формы фронта
    yaml: str = ""  # снапшот сгенерированного YAML на момент сохранения

# Pydantic-схемы запросов и ответов API.

import re
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator

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
    # Эта реплика заменяет ход, который ещё генерируется в этом чате (сказали
    # новое, пока персона думала): его генерация обрывается, ответ в историю
    # не ляжет (app/core/turn_cancel)
    supersede: bool = False
    # Id устройства (тот же client_id, что у опроса inbox): пока ответа нет,
    # реплику показывают другие устройства, а это — и так показывает свою
    client_id: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_-]*$")
    # Каким именем персону позвали голосом («Девиант, …» — в message имени
    # уже нет): имя режима персоны включает режим (app/core/persona_modes)
    called_as: Optional[str] = Field(None, max_length=80)


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
    # Режим персоны, которым ответил телефон (из кита; "" — обычный)
    mode: Optional[str] = Field(None, max_length=40, pattern=r"^[\w-]*$")


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
    # Режим управления телефоном (app/features/phone_control) после хода —
    # веб показывает его значок; с control_mode взаимоисключающие
    phone_mode: bool = False
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
    # Ход отменён, пока генерировался (новая реплика с supersede или
    # POST /api/chat/cancel): reply пустой, в историю ответ не лёг
    cancelled: bool = False


class ChatCancel(BaseModel):
    """POST /api/chat/cancel — оборвать начатую генерацию ответа в чате."""
    persona: PersonaId
    user_id: str = "web_user"
    chat_id: Optional[str] = None


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
    # Режимы по триггеру (<id>_modes/<режим>.yaml): id, name, names,
    # keywords, exit_keywords, mood, hold_minutes — голосовой слушатель
    # будит персону и именами режимов
    modes: list[dict] = Field(default_factory=list)


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


class PersonaPhraseSave(BaseModel):
    group_id: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=1, max_length=64)
    variants: list[StrictStr] = Field(min_length=1, max_length=15)


class PersonaModeSave(BaseModel):
    # Файл режима <id>_modes/<режим>.yaml целиком (app/api/modes_api)
    yaml: str = Field(..., max_length=200_000)
    create: bool = False  # новый режим: такой уже есть — 409, а не перезапись


class ShortPromptUpdate(BaseModel):
    # Краткое описание для модели телефона (app/features/short_prompt.py):
    # source yaml — своё (пустое — убрать, составит модель персоны); auto —
    # составлено на телефоне без ядра, crc — crc32 характера, по которому
    text: str = Field("", max_length=4000)
    source: Literal["yaml", "auto"] = "yaml"
    crc: Optional[int] = Field(None, ge=0, le=0xFFFFFFFF)


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


class NoteRequest(BaseModel):
    # Новая заметка или правка текста (блок «Заметки» на главной)
    text: str = Field(min_length=1, max_length=4000)


class PhoneNote(BaseModel):
    id: str = Field(min_length=6, max_length=32)
    text: str = Field(min_length=1, max_length=4000)
    created_at: float
    updated_at: Optional[float] = None


class NotesImportRequest(BaseModel):
    # Заметки, записанные на телефоне без ядра (перенос при связи)
    notes: list[PhoneNote] = Field(..., max_length=100)


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


class LocalDeviceRequest(BaseModel):
    device: str  # где работает Gemma локальных задач: "pc" | "phone"


class PhoneLlmResult(BaseModel):
    # Ответ Gemma телефона на задачу из GET /api/phone-llm/next
    worker: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    id: str = Field(min_length=1, max_length=32)
    text: str | None = Field(None, max_length=20000)
    error: str | None = Field(None, max_length=500)
    backend: str | None = Field(None, max_length=16)  # "gpu" | "cpu"
    ms: float | None = None
    # Не начата — вернуть в очередь другому исполнителю (приложение свернулось)
    release: bool = False


_CAPABILITY_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")


class DeviceBodyStatus(BaseModel):
    state: Literal['awake', 'asleep']
    session: str = Field(pattern=r'^[a-f0-9]{32}$')


class DeviceHello(BaseModel):
    # Устройство ядра представилось (app/core/devices): Pi с камерой и т.п.
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=64)  # «Робот» — в ответах и ошибках
    # Как к нему обращаются в чате («робот», «robot»): по ним узнаётся просьба
    names: list[str] = Field(default_factory=list, max_length=8)
    capabilities: list[str] = Field(default_factory=list, max_length=32)  # «camera.snapshot»
    body: DeviceBodyStatus | None = None

    @field_validator("names")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        out = [" ".join(n.split()) for n in v]
        if any(not n or len(n) > 32 for n in out):
            raise ValueError("имя устройства — от 1 до 32 символов")
        return out

    @field_validator("capabilities")
    @classmethod
    def _caps(cls, v: list[str]) -> list[str]:
        if any(not _CAPABILITY_RE.match(c) for c in v):
            raise ValueError("возможность — латиница в нижнем регистре, цифры, «._-»")
        return v


class DeviceResult(BaseModel):
    # Итог команды из GET /api/devices/{id}/commands
    command_id: str = Field(min_length=1, max_length=32)
    ok: bool
    body: DeviceBodyStatus | None = None
    error: str | None = Field(None, max_length=500)
    image: str | None = None  # JPEG base64 или dataURL; размер режет BodySizeLimit
    data: dict | None = None


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


class LlmEconomyUpdate(BaseModel):
    # «Экономия нейронки» (app/core/llm_economy): частичная правка пунктов,
    # true — модель (как раньше), false — код. Только bool: «false» строкой
    # не должен молча включать модель
    items: dict[str, StrictBool] = Field(default_factory=dict, max_length=32)


class AvatarTextUpdate(BaseModel):
    # Скорость текста у аватара (app/core/avatar_text): частичная правка;
    # пределы проверяет avatar_text.update (вне пределов — 422)
    words_per_sec: Optional[StrictFloat | StrictInt] = None
    part_sec: Optional[StrictFloat | StrictInt] = None


class UiLookUpdate(BaseModel):
    # Как выглядит веб на компьютере (app/core/ui_look): скин, свои темы
    # персон, схема — целиком; id проверяет ui_look.update (негодный — 422)
    skin: Optional[StrictStr] = Field(None, max_length=48)
    personas: Dict[StrictStr, StrictStr] = Field(default_factory=dict)
    theme: Optional[Literal["light", "dark"]] = None


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
    avatar: Optional[dict] = None  # {head, eyes, accessory, shade, body?} | null


class RoomStyleUpdate(BaseModel):
    description: Optional[str] = Field(default=None, max_length=1500)
    reference: Optional[str] = None  # data-URL (≤ 1 МБ) | null


class RoomStyleDescribeRequest(BaseModel):
    reference: str  # data-URL картинки-референса (≤ 1 МБ)


class RoomArtUpdate(BaseModel):
    sprite: Optional[dict] = None                     # {dataUrl, anchor} | null
    sprites: Optional[dict[str, Optional[dict]]] = None  # {поза: спрайт | null}
    room_bg: Optional[dict] = None                    # {dataUrl, floorPoints} | null


class PortraitSheetUpdate(BaseModel):
    # Лист живого портрета (app/api/portrait_api.py): data_url — новая
    # картинка, без него — переразметить уже загруженный лист
    data_url: Optional[str] = None
    rows: int = 5
    cols: int = 5
    offset_x: int = 0
    offset_y: int = 0
    bg: str = "auto"      # auto — убрать сплошной фон, keep — как есть
    tolerance: int = 0    # 0 — по шуму фона


class PortraitEmotionMood(BaseModel):
    # Когда эмоция на лице сама, по настроению персоны (state engine):
    # зона valence/arousal и/или слова тега настроения
    valence: Optional[Literal["good", "bad"]] = None
    arousal: Optional[Literal["calm", "excited"]] = None
    words: List[str] = Field(default_factory=list, max_length=8)


class PortraitEmotionUpdate(BaseModel):
    # Эмоция портрета (app/api/portrait_api.py): имя — его персона пишет в
    # маркере [EMO:имя], подсказка — когда эмоция уместна (идёт в промпт).
    # mood: null — только по маркеру; поля нет — условие прежнее
    name: str = Field(..., min_length=1, max_length=40)
    hint: str = Field("", max_length=200)
    mood: Optional[PortraitEmotionMood] = None


class AnimUpdate(BaseModel):
    # Анимация портрета на рабочем столе (app/api/portrait_anim.py):
    # data_url — новый лист кадров, без него — правка полей или переразметка
    # загруженного листа. Числа вне пределов ядро прижимает к ним
    data_url: Optional[str] = None
    name: str = Field(..., max_length=200)
    rows: int = 1
    cols: int = 1
    frames: Optional[int] = None  # None — до последней непустой клетки
    fps: int = 8
    loops: int = 1
    loop: Optional[bool] = None  # по кругу; None — как было
    scale: float = 1.0
    align: str = "grid"     # grid — общий сдвиг, feet — каждый кадр по ногам
    bg: str = "auto"
    tolerance: int = 0
    offset_x: int = 0
    offset_y: int = 0
    faces: str = "none"     # куда смотрит фигура: none, left, right
    triggers: list[str] = Field(default_factory=list, max_length=40)
    # Клавиши и кнопки мыши (Ctrl+Shift+KeyK, MouseLeft); None — как было
    keys: Optional[list[str]] = Field(default=None, max_length=8)
    # Связки: после каких и перед какими анимациями играет (id), chance —
    # в скольких процентах случаев; None — как было
    after: Optional[list[str]] = Field(default=None, max_length=12)
    before: Optional[list[str]] = Field(default=None, max_length=12)
    chance: Optional[int] = None
    # Переход между режимами (повод mode): из каких режимов ("" — обычный;
    # пусто — из любого); None — как было
    switch_from: Optional[list[str]] = Field(default=None, max_length=12)
    # Звук: sound_url None — как был, "" — убрать, data-URL — новый
    sound_url: Optional[str] = None
    sound_name: Optional[str] = Field(default=None, max_length=200)
    sound_loop: Optional[bool] = None
    volume: Optional[int] = None
    weight: int = 1
    enabled: bool = True


class FrameCutRequest(BaseModel):
    # Нарезка листа в редакторе кадров (app/api/portrait_frames.py): сетка,
    # поля, фон и линии сетки (xs — cols+1 штук, ys — rows+1, px листа)
    rows: int
    cols: int
    offset_x: int = 0
    offset_y: int = 0
    bg: str = "auto"
    tolerance: int = 0
    align: Optional[str] = None  # только у анимации: grid или feet
    xs: Optional[list[int]] = Field(default=None, max_length=13)
    ys: Optional[list[int]] = Field(default=None, max_length=13)


class FrameTiming(BaseModel):
    # Время кадров анимации: fps — шаг, holds — сколько шагов стоит каждый
    # кадр (None — ровно). Пределы держит ядро (portrait_anim)
    fps: int = 8
    loops: int = 1
    loop: bool = False
    holds: Optional[list[float]] = Field(default=None, max_length=144)
    frames: Optional[int] = None  # сколько кадров играть; None — как было


class FrameSaveRequest(FrameCutRequest):
    # fix — {all: {dx, dy, s}, cells: {"номер кадра": {dx, dy, s, r, px, py}}};
    # пределы держит ядро (portrait_api.clean_fix). order — порядок кадров
    # анимации (номера клеток листа); не прислали — как был, null — как на листе
    fix: Optional[dict] = None
    timing: Optional[FrameTiming] = None
    order: Optional[list[int]] = Field(default=None, max_length=144)


class AnimSettingsUpdate(BaseModel):
    every_min: int = 4      # средний интервал ротации, минут; 0 — выкл
    walk: bool = True       # гуляет ли сама по экрану
    sleep_anim: Optional[str] = None  # цикл на всё время сна; null — обычный портрет


class LoreSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    previous: str = Field(default="", max_length=4000)


class RoomPokeRequest(BaseModel):
    chat_id: Optional[str] = "auto"
    # glance — клик по персоне в комнате, pet — погладили живой портрет
    kind: Optional[str] = "glance"


class RoomFocusRequest(BaseModel):
    action: str  # start | end
    minutes: Optional[int] = None
    chat_id: Optional[str] = "auto"

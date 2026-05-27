# Virtual Persona Core

**Virtual Persona Core** — это модульная Python-платформа для создания и запуска AI-ботов с развитой системой памяти, настраиваемыми персонами и множеством подключаемых функций. Платформа поддерживает работу через Telegram и веб-интерфейс Gradio, позволяет создавать уникальных персонажей с собственным характером, веб-поиском, загрузкой файлов и двухуровневой памятью (краткосрочной и долгосрочной).

Каждый бот — это отдельная "персона", поведение которой полностью определяется YAML-конфигурацией: системным промптом, набором активных модулей и параметрами генерации. Платформа поддерживает одновременную работу нескольких ботов с независимыми базами данных и изолированными контекстами.

---

## Архитектура

```
virtual-persona-core/
├── app/
│   ├── __init__.py
│   ├── main.py                  # Точка входа — запуск ботов и Gradio
│   ├── gradio_app.py            # Веб-интерфейс на Gradio
│   ├── telegram_bot.py          # Telegram-обработчики
│   ├── bot_instance.py          # Ядро: BotInstance — один бот = одна персона
│   ├── core/                    # Базовые модули
│   │   ├── config.py            # Конфигурация провайдеров LLM
│   │   ├── persona.py           # Загрузка персоны из YAML
│   │   ├── memory.py            # STM + LTM + MemoryManager
│   │   ├── memory_config.py     # Промпты и логика извлечения фактов
│   │   ├── router.py            # Маршрутизатор LLM-провайдеров
│   │   ├── embedder.py          # Эмбеддинги через HuggingFace
│   │   ├── file_reader.py       # Чтение файлов (markitdown)
│   │   ├── file_vector_db.py    # Векторная БД для файлов
│   │   ├── self_memory.py       # Эпизодическая память бота
│   │   └── users.py             # Регистрация пользователей
│   ├── features/                # Опциональные модули
│   │   ├── web_search.py        # Поиск через DuckDuckGo
│   │   ├── moderation.py        # Модерация контента
│   │   ├── rate_limiter.py      # Ограничение частоты
│   │   ├── file_sender.py       # Отправка кода файлами
│   │   ├── reply_context.py     # Контекст reply-сообщений
│   │   ├── export_server.py     # HTTP-сервер экспорта памяти
│   │   ├── restore_memory.py    # Восстановление памяти из JSON
│   │   └── need_search.py       # Определение необходимости поиска
│   ├── personas/                # YAML-файлы персон
│   │   ├── arrodes.yaml
│   │   └── connor.yaml
│   └── scripts/
│       └── migrate_embeddings.py # Миграция векторных баз
├── data/                        # ChromaDB базы (STM, LTM, файлы)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env                         # API-ключи и токены
```

### Ключевые компоненты

| Компонент | Назначение |
|-----------|-----------|
| `BotInstance` | Центральный класс — создаёт персону, память, роутер и подключает фичи |
| `PersonaLayer` | Загружает YAML-конфиг персоны: промпт, настройки, спец. пользователи |
| `MemoryManager` | Объединяет STM (краткосрочную) и LTM (долгосрочную) память |
| `ShortTermMemory` | Буфер последних N сообщений на чат, FIFO, ChromaDB |
| `LongTermMemory` | Векторное хранилище фактов, извлечение через LLM |
| `ModelRouter` | Fallback-маршрутизатор по провайдерам LLM с ротацией ключей |
| `FileVectorDB` | ChromaDB для временного хранения загруженных документов |
| `BotSelfMemory` | Эпизодическая память бота — дневник, наблюдения, жизненная история |

---

## Требования

- **Python** 3.11+
- **Docker** + Docker Compose (опционально)
- **API-ключи** минимум для одного LLM-провайдера (см. Конфигурация)
- **Токены Telegram** (если используете Telegram-ботов)

### Зависимости

Основные зависимости из `requirements.txt`:

```
python-dotenv
pyyaml
openai
chromadb
sentence-transformers
httpx
python-telegram-bot
gradio
python-docx
pypdf
pdfplumber
python-pptx
openpyxl
ddgs
duckduckgo-search
markitdown
```

---

## Установка

### Локальная установка

```bash
# 1. Клонируйте репозиторий
git clone <repo-url>
cd virtual-persona-core

# 2. Создайте виртуальное окружение
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# 3. Установите зависимости
pip install -r requirements.txt
```

### Установка через Docker

```bash
docker-compose up --build
```

---

## Конфигурация

Все секреты и ключи хранятся в файле `.env` в корне проекта:

```env
# ─── Telegram токены ───────────────────────────
CONNOR_BOT_TOKEN=123456:ABC-DEF...
ARRODES_BOT_TOKEN=123456:XYZ-ABC...

# ─── LLM провайдеры (минимально один, для лучшей работы два) ─────
# Формат: <PREFIX>_API_KEY, <PREFIX>_API_KEY_1, <PREFIX>_API_KEY_2...
ZAI_API_KEY=sk-...
OPENAI_API_KEY=sk-...
GROQ_API_KEY=gsk_...
DEEPSEEK_API_KEY=sk-...
KIMI_API_KEY=sk-...
GOOGLE_API_KEY=...
HF_API_KEY=hf_...
MIMO_API_KEY=...

# Активный провайдер (если не задан — первый с ключом)
ACTIVE_PROVIDER=groq

# ─── Настройки памяти ──────────────────────────
STM_SIZE=50
LTM_EXTRACTION_ENABLED=true
LTM_MODEL_PROVIDER=hf

# ─── Rate limiting (лимит сообщений на одного пользователя) ─────────────────────────────
RATE_LIMIT_DEFAULT=6
RATE_WINDOW=3600              # 6 сообщений в час (3600 секунд)
RATE_LIMIT_USER_123456789=0   # 0 = без лимита для пользователя

# ─── Специальные пользователи ──────────────────
ARRODES_SPECIAL_USER_ID=123456789
CONNOR_SPECIAL_USER_ID=123456789
OWNER_USER_ID=123456789

# ─── Export server ─────────────────────────────
EXPORT_PORT=8080
```

### Провайдеры LLM

Платформа поддерживает множество провайдеров через OpenAI-совместимый API:

| Провайдер | Переменная окружения | Базовый URL |
|-----------|---------------------|-------------|
| ZAI | `ZAI_API_KEY` | https://open.bigmodel.cn/api/paas/v4/ |
| OpenAI | `OPENAI_API_KEY` | https://api.openai.com/v1 |
| Anthropic | `ANTHROPIC_API_KEY` | https://api.anthropic.com/v1/ |
| Groq | `GROQ_API_KEY` | https://api.groq.com/openai/v1 |
| DeepSeek | `DEEPSEEK_API_KEY` | https://api.deepseek.com |
| Kimi (Moonshot) | `KIMI_API_KEY` | https://api.moonshot.cn/v1 |
| Google | `GOOGLE_API_KEY` | https://generativelanguage.googleapis.com |
| Mimo | `MIMO_API_KEY` | https://token-plan-sgp.xiaomimimo.com/v1 |
| HuggingFace | `HF_API_KEY` | https://router.huggingface.co/v1 |

**Где получить бесплатные API-ключи:**

- **Groq** — быстрые бесплатные тиры: https://console.groq.com/keys
- **HuggingFace** — токен для Inference API: https://huggingface.co/settings/tokens

Можно задать несколько ключей для одного провайдера — роутер будет ротировать их при ошибках:

```env
GROQ_API_KEY=gsk_main
GROQ_API_KEY_1=gsk_backup_1
GROQ_API_KEY_2=gsk_backup_2
```

---

## Запуск

### Интерактивное меню

```bash
python -m app.main
```

### Запуск конкретного бота

```bash
python -m app.main connor      # Только Коннор
python -m app.main arrodes     # Только Арродес
python -m app.main all         # Оба бота одновременно
python -m app.main gradio      # Только Gradio-интерфейс
```

### Через переменную окружения

```bash
BOT_TARGET=arrodes python -m app.main
```

---

## Docker

### Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# Предзагрузка SentenceTransformer модели в образ
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

COPY . .

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7860/')" || exit 1

CMD ["python", "-m", "app.main"]
```

Особенности образа:
- Используется `python:3.11-slim` для минимального размера
- PyTorch устанавливается в CPU-версии (`--index-url https://download.pytorch.org/whl/cpu`)
- Модель `paraphrase-multilingual-MiniLM-L12-v2` предзагружается на этапе сборки, ускоряя старт контейнера
- Healthcheck проверяет доступность Gradio на порту 7860

### docker-compose.yml

```yaml
services:
  gradio:
    build: .
    container_name: virtual-persona-gradio
    restart: unless-stopped
    command: ["python", "-m", "app.main", "gradio"]
    env_file: .env
    environment:
      - BOT_TARGET=gradio
    volumes:
      - persona_data:/app/data
    ports:
      - "7860:7860"

  # connor:
  #   build: .
  #   container_name: virtual-persona-connor
  #   restart: unless-stopped
  #   command: ["python", "-m", "app.main", "connor"]
  #   env_file: .env
  #   environment:
  #     - BOT_TARGET=connor
  #   volumes:
  #     - persona_data:/app/data

volumes:
  persona_data:
```

Команды:

```bash
# Запуск Gradio через docker-compose
docker-compose up -d gradio

# Запуск с интерактивным меню (через docker run)
docker run -it --rm --name vp-menu -p 7860:7860 --env-file .env -v persona_data:/app/data virtual-persona

# Запуск Telegram-бота (раскомментируйте в docker-compose.yml)
docker-compose up -d connor

# Запуск всех сервисов
docker-compose up -d
```

**Интерактивное меню через `docker run`:**

Команда `docker run -it --rm ... virtual-persona` запускает контейнер с TTY и интерактивным вводом, позволяя выбрать режим работы (Gradio, Connor, Arrodes или все боты). Флаги:
- `-i` — интерактивный режим (stdin открыт)
- `-t` — pseudo-TTY (терминал)
- `--rm` — удалить контейнер после остановки
- `-p 7860:7860` — проброс порта для Gradio
- `--env-file .env` — загрузка переменных окружения
- `-v persona_data:/app/data` — сохранение данных между запусками

**Запуск Gradio напрямую (без меню):**

```bash
docker run -d --name vp-gradio -p 7860:7860 --env-file .env -v persona_data:/app/data virtual-persona python -m app.main gradio
```

Общий volume `persona_data` обеспечивает персистентность ChromaDB между перезапусками.

---

## Автоматическое создание файлов из ответов LLM

Модуль `app/features/file_sender.py` анализирует ответы LLM и автоматически решает, как их отправить пользователю в Telegram. Это позволяет избежать огромных сообщений с кодом и отправлять код как отдельные файлы с правильными расширениями.

### Логика работы

```
Ответ LLM
    |
    v
[Есть код-блоки?]
    |-- Да --> [Код > 50% ответа?]
    |              |-- Да --> Отправить основной код как файл, остальное текстом
    |              |-- Нет --> Код в файлы, текст несколькими сообщениями
    |
    |-- Нет --> [Длина > 4000 символов?]
                   |-- Да --> Разбить на несколько сообщений
                   |-- Нет --> Одно сообщение
```

### Правила

| Сценарий | Что происходит |
|----------|---------------|
| Ответ преимущественно код (>50%) | Главный блок кода отправляется как файл (`code.py`, `code.js` и т.д.), пояснение — текстом. Дополнительные блоки — отдельными файлами (`code_1.py`, `code_2.js`) |
| Смешанный ответ (код + текст) | Каждый блок кода — отдельный файл, текст без кода отправляется несколькими сообщениями |
| Длинный текст без кода | Разбивается на части по ~4000 символов (по абзацам, не посреди слова) |
| Короткий текст | Одно сообщение как есть |

### Поддерживаемые языки

Python, JavaScript, TypeScript, Java, C/C++, C#, Go, Rust, Ruby, PHP, Swift, Kotlin, SQL, HTML, CSS, SCSS, JSON, YAML, XML, Bash, PowerShell, Dockerfile, Lua, R, MATLAB, Dart, Vue, React/JSX, TSX, TOML, INI, Nginx, GraphQL, Protobuf.

Расширение файла определяется автоматически по маркеру языка в markdown-блоке (python → .py).

### Технические детали

- Файлы создаются во временной директории (`tempfile.mkdtemp`) и удаляются после отправки
- Текстовые сообщения разбиваются по абзацам (двойной перенос строки), чтобы не резать посреди мысли
- Если один абзац длиннее лимита — разбивается по строкам
- HTML-форматирование (жирный, курсив, код) конвертируется из Markdown перед отправкой в Telegram

---

## Как создать свою персону

Персона определяется YAML-файлом в директории `app/personas/`. `persona_template.yaml` можно использовать как шаблон для создания своей персоны.

### Структура persona.yaml

```yaml
id: your_persona_id          # Уникальный идентификатор
name: Имя персонажа          # Человекочитаемое имя
version: "1.0"
description: Краткое описание
stm_size: 50                 # Размер краткосрочной памяти

features:
  owner: ""                        # ID владельца — полный иммунитет
  rate_limit: false                # Ограничение частоты
  rate_limit_individual: []        # Индивидуальные лимиты
  moderation: false                # Модерация контента
  punish_block: false              # Блокировка за нарушения
  web_search: false                # Веб-поиск
  file_upload: false               # Загрузка файлов
  export_server: false             # HTTP-сервер экспорта
  restore_memory: false            # Восстановление памяти
  self_memory: false               # Эпизодическая память бота
  trigger_words:                   # Слова-активаторы
    - "ключевое слово 1"
    - "ключевое слово 2"
  allowed_dm_users:                # Кто может писать в ЛС
    - ""
  blocked_users:                   # Заблокированные пользователи
    - ""

special_users:
  - id: "${SPECIAL_USER_ID}"       # ID через env-переменную
    aliases:
      - "Псевдоним 1"
      - "Псевдоним 2"

system_prompt: |
  Ты — [имя персонажа]. [Описание]

  ## Характер
  [Тон, стиль речи, манера поведения]

  ## Режимы поведения
  ### Режим 1 — Обычный пользователь
  [Как ведёшь себя с обычными людьми]

  ### Режим 2 — Особый пользователь, можно не указывать
  [Как меняешься со специальным пользователем]

  ## Запрещено
  - [Список запретов]

settings:
  temperature: 0.75 # чем ближе к 0, тем детерминированнее, предсказуемее ответы, значения выше 1.0 - более творческие и непредсказуемые
  max_tokens: 2000 # ограничивает максимальную длину ответа
  top_p: 0.90 # при значении 0.1 модель выбирает только из наиболее вероятных токенов, при 1.0 рассматривает все варианты
```

### Ключевые поля

**`features`** — управляет доступными модулями:

- `trigger_words` — бот отвечает только если сообщение начинается с одного из этих слов
- `owner` — ID пользователя-владельца, полностью защищённого от всех ограничений
- `rate_limit` + `punish_block` — система наказаний: лимит сообщений, блокировка
- `web_search` — включает DuckDuckGo-поиск с приоритетом памяти над интернетом
- `file_upload` — позволяет пользователям загружать документы для векторного поиска
- `self_memory` — бот ведёт личный дневник: эпизоды разговоров и наблюдения

**`special_users`** — пользователи с особыми привилегиями. ID может содержать `${ENV_VAR}` для подстановки из переменных окружения. Бот распознаёт их по ID и переключает режим поведения.

**`system_prompt`** — главный промпт, определяющий личность. Может содержать:
- Разделы с `##` — структурируют промпт
- Системные маркеры `[PUNISH:BLOCK]`, `[PUNISH:FACT:...]` — выполняются кодом
- Примеры диалогов и форматов ответов
- Запреты — чёткие границы поведения

### Пример простой персоны

```yaml
id: assistant
name: Помощник
version: "1.0"
description: Вежливый ассистент
stm_size: 30

features:
  trigger_words:
    - помощник
    - ассистент
    - assistant
  web_search: true
  file_upload: true

system_prompt: |
  Ты — полезный ассистент. Отвечай кратко и по существу.
  Используй только русский язык.

settings:
  temperature: 0.7
  max_tokens: 1500
  top_p: 0.9
```

---

## Описание модулей

### `app/bot_instance.py`

Центральный класс `BotInstance` — создание одного бота. Инициализирует персону, память, роутер и опциональные фичи на основе YAML-конфигурации.

```python
class BotInstance:
    def __init__(self, persona_name: str, context: str = None):
        self.persona = PersonaLayer(persona_name=persona_name)
        self.features: dict = persona_data.get("features", {})
        self.stm_size: int = persona_data.get("stm_size", Config.STM_SIZE)
        # ... инициализация memory, router, web_search, rate_limiter, moderation
```

Ключевые методы:

- `should_respond(text)` — проверяет, начинается ли сообщение с trigger-слова
- `strip_trigger(text)` — удаляет trigger-слово из сообщения
- `pre_check(user_id, text, is_private)` — pipeline проверок: владелец → блокировка → DM → punish → rate limit → модерация
- `process_message(user_input, ...)` — основной пайплайн обработки: память → файлы → веб-поиск → LLM → парсинг наказаний → сохранение

Pipeline `process_message` работает асинхронно: веб-поиск запускается в фоне через `ThreadPoolExecutor`, параллельно с извлечением контекста из памяти.

```python
# Параллельный запуск веб-поиска
web_future = None
if self._web_search_enabled and not self._is_docs_only_request(user_input):
    web_future = self._web_pool.submit(self._search_web, user_input, 5)

# ... работа с памятью ...

# Получение результатов поиска
if web_future is not None:
    try:
        results = web_future.result(timeout=10)
    except FuturesTimeoutError:
        pass
```

---

### `app/core/persona.py`

Загружает персону из YAML и формирует сообщения для LLM.

```python
class PersonaLayer:
    def __init__(self, persona_name: str = "connor"):
        self.persona_data = self._load_persona(persona_name)
        self.system_prompt = self.persona_data.get("system_prompt", "")
        self.settings = self.persona_data.get("settings", {})
```

Метод `prepare_messages` собирает финальный список сообщений:

1. **System prompt** — базовый промпт + контекст памяти + веб-результаты + self_memory
2. **Special user note** — если пользователь совпадает с `special_users` из YAML, подставляется дополнительная инструкция с псевдонимами и поведением
3. **История** — сообщения из STM, форматированные с именами и ID
4. **Текущее сообщение** — с именем пользователя и контекстом reply

```python
def prepare_messages(self, user_message: str, memory_context: Optional[str] = None,
                     history: Optional[List[Dict]] = None, user_id: str = None, ...):
    # Контекст из памяти, файлов, веб-поиска
    context_block = ...
    # Проверка особого пользователя
    special_note = self._get_special_user_note(user_id) or ""
    # Сборка сообщений
    messages = [
        {"role": "system", "content": self.system_prompt + context_block + special_note},
        # ... история ...
        {"role": "user", "content": formatted_message}
    ]
    return messages
```

Метод `_get_special_user_note` поддерживает подстановку переменных окружения:

```python
su_id = str(su.get("id", ""))
if su_id.startswith("${") and su_id.endswith("}"):
    su_id = os.getenv(su_id[2:-1], "")
```

---

### `app/core/memory.py`

Три класса: `ShortTermMemory`, `LongTermMemory`, `MemoryManager`.

#### ShortTermMemory

Буфер последних N сообщений (FIFO) с персистентностью в ChromaDB.

```python
class ShortTermMemory:
    def __init__(self, max_messages: int = 50, db_path: str = None,
                 load_from_db: bool = True, context: str = "default"):
        self.buffers: Dict[str, deque] = {}  # chat_id -> deque
        self.collection = self.client.get_or_create_collection("short_term_memory")
```

- Хранит сообщения по `chat_id` — отдельные буферы для каждого чата
- `deque(maxlen=N)` автоматически вытесняет старые сообщения
- Все сообщения сохраняются в ChromaDB и восстанавливаются при перезапуске
- Потокобезопасность через `threading.RLock`

#### LongTermMemory

Векторное хранилище фактов с LLM-экстракцией.

```python
class LongTermMemory:
    def extract_facts_async(self, user_message: str, user_id: str = "default", stm_context: str = None):
        # Запускает извлечение фактов в фоновом потоке
        executor = self._get_executor()
        future = executor.submit(_extract_and_save)
```

Процесс извлечения фактов:
1. LLM анализирует сообщение по специальному промпту с примерами
2. Результат парсится в `Category: value`
3. Фильтруются пустые значения (`NO_FACTS`, `unknown`, `none`)
4. Факты сохраняются в ChromaDB

Дедупликация и слияние:
- **UPDATE-категории** (`City`, `Age`, `Profession`...) — старое значение заменяется
- **APPEND-категории** (`Hobby_*`, `Food`, `Pets`...) — умное слияние через LLM

```python
def save_facts(self, facts_text: str, user_id: str = "default"):
    if fact_stripped.lower() in existing_docs:
        continue  # Дубликат
    if cat_key in UPDATE_CATEGORIES:
        self.collection.delete(ids=[old_id])  # Замена
    elif cat_key in APPEND_CATEGORIES:
        merged = self._merge_append_fact(cat_key, old_val, new_val)  # Слияние
```

Периодическая консолидация (каждые N сообщений) вызывает `summarize_user` — LLM чистит противоречия и дубликаты.

#### MemoryManager

Единый интерфейс для обоих типов памяти.

```python
def add_message(self, role: str, content: str, user_id: str = "default", ...):
    self.stm.add_message(role, content, user_id, chat_id, user_name)
    if role == "user" and self.enable_ltm_extraction:
        self.ltm.extract_facts_async(content, user_id, stm_context)
        # Периодическая консолидация
        if self._user_msg_counters[user_id] >= SUMMARY_SETTINGS["trigger_every"]:
            self._run_summarize_async(user_id)
```

---

### `app/core/router.py`

Маршрутизатор LLM-провайдеров с fallback-цепочкой и ротацией ключей.

```python
class ModelRouter:
    def __init__(self, provider: str = None):
        self.available = get_available_providers()
        self.active_provider = provider or os.getenv("ACTIVE_PROVIDER")
```

Логика работы `get_response`:
1. Формируется очередь провайдеров — активный первым, остальные fallback
2. Для каждого провайдера перебираются все ключи (`API_KEY`, `API_KEY_1`, `API_KEY_2`...)
3. Первый успешный ответ возвращается
4. Если все провайдеры недоступны — возвращается ошибка

```python
def get_response(self, messages, temperature: float = 0.7,
                 max_tokens: int = 2000, top_p: float = 0.9,
                 exclude_provider: str = None, timeout: float = 60.0) -> str:
    provider_order = self._get_provider_order()
    if exclude_provider and len(provider_order) > 1:
        provider_order = [p for p in provider_order if p != exclude_provider]

    for provider in provider_order:
        answer = self._call_with_keys(provider, cfg, messages, ...)
        if answer is not None:
            return answer
    return "Ошибка: все провайдеры недоступны."
```

Параметр `exclude_provider` используется LTM-экстрактором, чтобы не нагружать ту же модель, что и основной диалог.

---

### `app/core/file_vector_db.py`

Векторная база для загруженных файлов: хранит до 3 документов на пользователя.

```python
class FileVectorDB:
    def __init__(self, context: str = "default", max_docs: int = 3):
        self.collection = self.client.get_or_create_collection("file_documents")
        self.full_docs = self.client.get_or_create_collection("file_full_docs")
```

Две коллекции:
- `file_documents` — чанки для семантического поиска (chunk_size=1000, overlap=25%)
- `file_full_docs` — полные тексты для пересказа/анализа целиком

При загрузке нового файла при превышении лимита удаляется самый старый документ.

```python
def add_file(self, user_id: str, filename: str, content: str):
    # Удаление старого при лимите
    if len(user_docs["ids"]) >= self.max_docs:
        self.collection.delete(ids=[oldest_id])
    # Сохранение полного текста частями (по 50KB)
    parts = [content[i:i + max_part_len] for i in range(0, len(content), max_part_len)]
    # Добавление чанков для поиска
    chunks = self._split_content(content)
```

---

### `app/core/self_memory.py`

Эпизодическая память бота — личный дневник с эпизодами, наблюдениями и жизненной историей.

```python
class BotSelfMemory:
    EPISODE_EVERY = 5            # сообщений между эпизодами
    MAX_ACTIVE_EPISODES = 10     # в промпте
    MAX_ARCHIVE_EPISODES = 40    # перед суммаризацией
    MAX_NOTES = 15               # заметок в промпте
```

Три типа записей:
- **Эпизоды** — развёрнутые записи о фрагментах разговора (5-8 предложений), создаются каждые 5 сообщений через LLM
- **Заметки** — короткие наблюдения, создаются по маркерам рефлексии ("чувствую", "надоело", "впервые"...)
- **Life summary** — консолидированная история, создаётся при переполнении архива

```python
def tick(self, messages: List[Dict], user_id: str, last_message: str):
    self._msg_since_episode += 1
    if self._msg_since_episode >= EPISODE_EVERY:
        self._write_episode(messages)
    if (self._msg_since_last_note >= MIN_NOTE_INTERVAL
            and _has_reflection_marker(last_message)):
        self._maybe_write_note(last_message, user_id, messages[-5:])
```

Контекст вставляется в system prompt с запретом явно упоминать источник:

```
СТРОГОЕ ПРАВИЛО: блок выше — твои внутренние воспоминания.
КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО явно упоминать их в ответе.
```

---

### `app/features/web_search.py`

Поиск через DuckDuckGo с загрузкой полного текста страниц.

```python
def search_web(query: str, max_results: int = 5) -> list[dict]:
    results = list(ddgs.text(query, max_results=max_results))
    # Загрузка полного текста для топ-2 результатов
    for r in results[:FETCH_TOP_N]:
        full = fetch_page_text(url)
        r["full_text"] = full
    return results
```

Приоритет источников (задаётся в `prepare_messages`):
1. Память (LTM-факты) и загруженные файлы — веб-поиск игнорируется
2. Если в памяти нет ответа — используются данные из веб-поиска
3. Личные чувства — используются данные из self_memory

---

### `app/features/moderation.py`

Бинарная классификация сообщений через LLM: `BLOCK` или `ALLOW`.

```python
MODERATION_PROMPT = """Classify the user message. Reply with exactly ONE word: BLOCK or ALLOW.

BLOCK ONLY if the message EXPLICITLY contains:
- Sexual content, pornography, erotica
- Real-world modern politics: elections, politicians, wars

ALLOW everything else."""
```

При срабатывании moderation + `punish_block: true` — пользователь блокируется.

---

### `app/features/rate_limiter.py`

Ограничение частоты сообщений с скользящим окном.

```python
RATE_LIMIT_DEFAULT = 6      # сообщений
RATE_WINDOW = 3600          # секунд (1 час)

_user_requests: dict[str, list[float]] = defaultdict(list)
_punish_blocked: dict[str, float] = {}  # блокировка
```

Индивидуальные лимиты задаются через env: `RATE_LIMIT_USER_<ID>=<seconds>`.

---

### `app/telegram_bot.py`

Создаёт handlers для `python-telegram-bot` v20+.

Ключевые особенности:
- Функция `create_handlers` возвращает замыкания над `BotInstance` — каждый бот получает свой набор handlers
- Markdown конвертируется в HTML через `_md_to_html` — поддержка `**жирный**`, `*курсив*`, `` `код` ``, ```блоки кода```
- Ответы с кодом автоматически разбиваются: код идёт файлом, пояснение текстом (`prepare_response`)
- Reply-контекст — если пользователь отвечает на чужое сообщение, бот видит оригинальный текст
- Pre-check pipeline выполняется до обработки: rate limit, moderation, punish block

---

### `app/gradio_app.py`

Веб-интерфейс на Gradio с выбором персоны, статистикой памяти и очисткой.

```python
class GradioBot:
    def __init__(self):
        self.persona = PersonaLayer(persona_name="arrodes")
        self.memory = MemoryManager(context="gradio", main_router=self.router)
        self.file_db = FileVectorDB(context="gradio")
```

Использует отдельный контекст `"gradio"` — база данных не пересекается с Telegram.

---

### `app/main.py`

Точка входа с тремя способами выбора цели:

```python
def main():
    # 1. Аргумент командной строки
    if len(sys.argv) > 1:
        target = sys.argv[1]
    # 2. Переменная окружения
    elif os.getenv("BOT_TARGET"):
        target = os.getenv("BOT_TARGET")
    # 3. Интерактивное меню
    elif sys.stdin.isatty():
        target = show_menu()
```

Режим `all` запускает каждого бота в отдельном потоке (`threading.Thread`) с собственным asyncio event loop.

```python
if target == "all":
    threads = []
    if connor_token:
        t = threading.Thread(target=run_bot, args=(connor_token, "connor"), daemon=False)
        threads.append(t)
    for t in threads:
        t.start()
        t.join()
```

---

### `app/scripts/migrate_embeddings.py`

Утилита для миграции векторных баз на новую модель эмбеддингов. Пересчитывает все векторы в указанных коллекциях через batch-вставку.

```python
NEW_EMBEDDER = SentenceTransformerEmbeddingFunction(
    model_name="paraphrase-multilingual-MiniLM-L12-v2"
)

# Чтение → удаление старой коллекции → создание новой → batch-вставка
for i in range(0, count, batch_size):
    new_collection.add(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
```
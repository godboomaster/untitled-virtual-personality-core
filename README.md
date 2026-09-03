# Virtual Persona Core

**Virtual Persona Core** — модульная Python-платформа для запуска «живых» AI-персонажей: ботов, которые общаются почти как люди — помнят собеседника, живут своей жизнью между сообщениями и сами проявляют инициативу. Платформа работает одновременно в **Telegram** и в **веб-интерфейсе** (FastAPI-бэкенд + React-фронт в `web/`), умеет **управлять компьютером пользователя** (открытие сайтов, клики, ввод текста, сценарии) и может отвечать **вообще без API-ключей** — через веб-чаты LLM (DeepSeek, Qwen, Claude, ChatGPT, Kimi, Z.AI) в браузере пользователя или через локальную модель (Ollama).

Каждый бот — это отдельная "персона", поведение которой полностью определяется YAML-конфигурацией: системным промптом, набором активных модулей и параметрами генерации. Платформа поддерживает одновременную работу нескольких ботов с независимыми базами данных и изолированными контекстами.

---

## Возможности

### Каналы

- **Telegram** (`app/telegram_bot.py`): группы и личные сообщения, триггер-слова, reply-контекст, длинные ответы с кодом уходят файлами, Markdown → HTML.
- **Веб-интерфейс** (`web/`, React): чат с персонами, история, досье чата, память (STM/LTM), дела, напоминания, курсы обучения, инвентарь, дневник персоны, «комната» с живым состоянием (настроение/занятие/место), настройки фич и провайдеров — всё на живом API, переключение без рестарта.
- **HTTP API** (FastAPI, порт 8000): стриминг ответов (SSE), управление памятью, файлами и фичами; фоновые сообщения бота (напоминания, уроки, инициативы) складываются в inbox и забираются polling'ом.
- Все каналы обслуживает один класс `BotInstance` — логика не дублируется.

### Персоны общаются почти как люди

- **Характер из YAML**: system_prompt с режимами поведения, особыми пользователями (свой режим по ID) и запретами. Уровни интеллекта (`primitive`/`normal`/`bot`) задают, *как* персона помогает: человек не отвечает простынёй ассистента на бытовой вопрос, а существо-примитив — жестом.
- **Память о собеседнике**: краткосрочная (последние N сообщений на чат) + долгосрочная (LLM извлекает факты, дедупликация и умное слияние, периодическая консолидация противоречий). Плюс досье чата (интересы, недавние события) и память отношений.
- **Жизнь между сообщениями** (living persona): состояние (настроение, энергия, занятие, место), собственный мир с NPC и сюжетными арками, офлайн-события. Возвращаясь после паузы, персона «помнит», чем жила, и вплетает это в разговор.
- **Суточный ритм**: утреннее приветствие, ночное «пора спать», погодные предупреждения (нужна настроенная локация).
- **Проактивность**: бот сам пишет первым, когда есть повод, — внутренний монолог, скоринг инициативы и антиспам-ограничения.
- **Человеческая подача**: ответ разбивается на сообщения с паузами «как печатает», язык ответа следует за языком пользователя (ru/en), платформенное правило гасит рефлекторные «А у тебя?» в конце каждого ответа (см. «Стиль разговора»).

### Помощник по хозяйству

- **Напоминания**: «напомни через 2 часа», «завтра в 12», повторяющиеся («каждый день в 9», «по пятницам в 18:00»); при срабатывании бот тегает автора.
- **Дела**: список todo на чат — «добавь дело…», «что у меня по делам».
- **Инвентарь**: предметы персонажа; офлайн-события living-движка реально их меняют.
- **Обучение**: «научи меня X» — регулярные уроки в характере персоны, каждый N-й — тест; при долгом молчании бот сам вежливо останавливает курс.
- **Веб-поиск** (DuckDuckGo) с приоритетом памяти и файлов над интернетом.
- **Файлы**: загрузка документов (docx/pdf/pptx/xlsx и т.д.) с векторным поиском по содержимому и пересказом целиком.

### Управление компьютером (режим управления)

- Включается на чат командой «перейди в режим управления» (см. «Управление компьютером пользователя»), работает на macOS и Windows через единый CDP-бэкенд (Chromium-браузеры; на macOS есть fallback на AppleScript и Safari).
- **Открытие сайтов и приложений** («открой ютуб»), **поиск на сайте** («включи интерстеллар на кинопоиске»), **агентные клики** («нажми „Скачать"»), **ввод текста**, клавиши, прокрутка, скачивание, чтение страниц, многошаговая навигация по меню и **записываемые сценарии** («запомни сценарий заказ пиццы»).
- Браузерные рецепты: пауза/следующее видео/звук на YouTube, «запусти третий результат», «открой второе видео в плейлисте».
- Безопасность: исполняется только то, что в allowlist'ах YAML; действия требуют подтверждения в чате; каждое исполнение пишется в аудит.

### Модели: API, веб-чаты или локально

- **API-провайдеры** (OpenAI-совместимые): ZAI, OpenAI, Anthropic, Groq, DeepSeek, Kimi, Google, Mimo, HuggingFace — fallback-цепочка и ротация нескольких ключей.
- **Веб-чаты вместо API** (провайдер `webchat`): промпт уходит в чат DeepSeek/Qwen/Claude/ChatGPT/Kimi/Z.AI в браузере пользователя, ответ читается из DOM — бот живёт вообще без ключей (подробно — в разделе про `router.py`).
- **Локальная модель** (Ollama, Gemma): классификации, тики состояния, черновики living-движка — разгружает облачные LLM и работает офлайн.

### Для лор-ботов

- **Книжный RAG**: гибридный поиск по томам книги (вектор + BM25 + rerank), мультитомные запросы, маркеры источников с анти-галлюцинационной проверкой.
- **Динамический глоссарий**: в промпт подгружаются только записи, релевантные вопросу, — а не весь словарь.
- Воспроизводимый harness оценки качества ответов (`qa_eval/`).

Подробности — в соответствующих разделах ниже («living persona», «Уровни интеллекта», «Управление компьютером пользователя», «Стиль разговора» и др.).

---

## Архитектура

```
virtual-persona-core/
├── app/
│   ├── __init__.py
│   ├── main.py                  # Точка входа — запуск ботов и API
│   ├── telegram_bot.py          # Telegram-обработчики
│   ├── bot_instance.py          # Ядро: BotInstance — один бот = одна персона
│   ├── core/                    # Базовые модули
│   │   ├── config.py            # Конфигурация провайдеров LLM
│   │   ├── persona.py           # Загрузка персоны из YAML
│   │   ├── memory.py            # STM + LTM + MemoryManager
│   │   ├── router.py            # Маршрутизатор LLM-провайдеров (fallback, ротация)
│   │   ├── local_router.py      # Локальная модель (Ollama) для классификаций
│   │   ├── embedder.py          # Эмбеддинги через HuggingFace
│   │   ├── file_vector_db.py    # Векторная БД для файлов
│   │   ├── self_memory.py       # Эпизодическая память бота (дневник)
│   │   ├── living_persona.py    # Оркестратор «живой» персоны
│   │   ├── state_engine.py      # Тики состояния (mood/energy/pastime/location)
│   │   ├── world_engine.py      # Мир: NPC, места, сюжетные арки
│   │   ├── offline_summarizer.py# Дневная суммаризация офлайн-жизни
│   │   ├── persona_context.py   # Кэшируемая выжимка характера из промпта
│   │   ├── relationship.py      # Память отношений с пользователем
│   │   ├── intellect.py         # Уровни интеллекта персон
│   │   ├── message_pacing.py    # Паузы между частями ответа
│   │   └── language.py          # Детект языка пользователя (ru/en)
│   ├── features/                # Опциональные модули
│   │   ├── web_search.py        # Поиск через DuckDuckGo
│   │   ├── reminder_manager.py  # Напоминания (в т.ч. повторяющиеся)
│   │   ├── todo_manager.py      # Список дел
│   │   ├── inventory_manager.py # Инвентарь персоны
│   │   ├── learning_manager.py  # Режим обучения (уроки + тесты)
│   │   ├── proactive_messaging.py # Инициативные сообщения
│   │   ├── rhythm_manager.py    # Суточный ритм: утро/ночь/погода
│   │   ├── chat_dossier.py      # Досье чата (интересы, события)
│   │   ├── computer_control.py  # Управление компьютером пользователя
│   │   ├── browser_actions.py   # Браузерный бэкенд: рецепты, клики, снапшоты
│   │   ├── scenario_manager.py  # Запись/воспроизведение сценариев действий
│   │   ├── web_llm.py           # Провайдер webchat: веб-чаты LLM без API-ключей
│   │   ├── book_search.py       # Книжный RAG (вектор + BM25 + rerank)
│   │   ├── glossary_context.py  # Динамическая подгрузка глоссария
│   │   ├── moderation.py        # Модерация контента
│   │   ├── rate_limiter.py      # Ограничение частоты
│   │   ├── file_sender.py       # Отправка кода файлами
│   │   ├── conversation_style.py# Правило против рефлекторных вопросов
│   │   ├── export_server.py     # HTTP-сервер экспорта памяти
│   │   └── restore_memory.py    # Восстановление памяти из JSON
│   ├── api/                     # FastAPI-сервер для веб-фронта
│   ├── personas/                # YAML-файлы персон
│   └── scripts/                 # Миграции векторных баз
├── web/                         # React-фронт (чат, досье, комната, настройки)
├── scripts/                     # Smoke-тесты и eval-утилиты
├── qa_eval/                     # Harness оценки качества лор-бота
├── data/                        # ChromaDB базы и JSON-состояние (per context)
├── persona_template.yaml        # Шаблон новой персоны
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
python -m app.main api         # API-сервер для веб-фронта (порт 8000)
```

### Через переменную окружения

```bash
BOT_TARGET=arrodes python -m app.main
```

---

## API-сервер (FastAPI)

HTTP API поверх `BotInstance` для веб-фронта (`web/`) и десктоп-приложения. Использует ту же логику, что и Telegram, без дублирования. Память каждой персоны изолирована (контекст `api_{persona}` в `data/`).

### Запуск

```bash
python -m app.main api
```

Сервер поднимается на `http://127.0.0.1:8000` (Swagger — `/docs`). Настройки через `.env`:

```env
API_HOST=127.0.0.1        # хост
API_PORT=8000             # порт
API_TOKEN=secret          # если задан — все /api/* требуют "Authorization: Bearer <token>"
API_CORS_ORIGINS=*        # список origin через запятую
```

### Эндпоинты

| Метод | Путь | Назначение |
|-------|------|-----------|
| GET | `/api/health` | Проверка живости (без авторизации) |
| GET | `/api/personas` | Список персон (без служебных YAML) |
| POST | `/api/chat` | Отправить сообщение персоне |
| POST | `/api/chat/stream` | То же со стримингом (SSE): события `{"token"}`, финал `{"done", "reply", ...}` |
| GET | `/api/chat/history?persona=&user_id=&chat_id=` | История из STM |
| POST | `/api/chat/clear` | Очистить STM чата |
| GET | `/api/personas/{p}/memory/stats?user_id=` | Статистика памяти |
| GET | `/api/personas/{p}/memory/ltm?user_id=` | Все факты LTM пользователя |
| POST | `/api/personas/{p}/memory/facts` | Добавить факт (`{"fact": "..."}`) |
| DELETE | `/api/personas/{p}/memory/facts?query=&user_id=` | Забыть факт по запросу |
| POST | `/api/personas/{p}/memory/clear?user_id=` | Очистить память пользователя |
| POST | `/api/personas/{p}/files?user_id=` | Загрузить файл (multipart `file`) |
| GET | `/api/personas/{p}/files?user_id=` | Список файлов (имя, размер, дата) |
| GET | `/api/personas/{p}/files/{name}/content` | Полный текст файла |
| DELETE | `/api/personas/{p}/files/{name}` | Удалить один файл |
| DELETE | `/api/personas/{p}/files?user_id=` | Очистить файловую базу |
| GET | `/api/personas/{p}/todo?chat_id=` | Список дел |
| POST | `/api/personas/{p}/todo` | Добавить дело (`{"task": "..."}`) |
| DELETE | `/api/personas/{p}/todo?index=&chat_id=` | Удалить дело (1-based индекс) |
| GET | `/api/personas/{p}/reminders?chat_id=` | Активные напоминания |
| POST | `/api/personas/{p}/reminders` | Добавить (`{"task", "delay_seconds"}`) |
| DELETE | `/api/personas/{p}/reminders?index=&chat_id=` | Отменить напоминание |
| GET | `/api/personas/{p}/inventory` | Инвентарь персоны |
| POST | `/api/personas/{p}/inventory` | Добавить предмет (`{"name", "description"}`) |
| DELETE | `/api/personas/{p}/inventory?name=` | Удалить предмет |
| GET | `/api/personas/{p}/learning?chat_id=` | Активные курсы обучения |
| POST | `/api/personas/{p}/learning` | Начать курс (`{"subject", "interval_seconds"}`) |
| DELETE | `/api/personas/{p}/learning?session_id=&chat_id=` | Остановить курс |
| GET | `/api/personas/{p}/diary` | Дневник (эпизоды, заметки, сводка) |
| GET | `/api/personas/{p}/state` | Живое состояние персоны: mood/energy/pastime/location, storylines, лента событий |
| GET | `/api/personas/{p}/initiative?chat_id=` | Состояние проактивности и история инициатив |
| GET | `/api/personas/{p}/inbox?chat_id=` | Забрать фоновые сообщения (напоминания, уроки, инициативы), pop-семантика |
| GET | `/api/providers` | Провайдеры LLM: статус ключей, модель, активный |
| POST | `/api/providers/{id}/keys` | Добавить ключ (`{"key": "..."}`) — пишется в `.env`, применяется сразу |
| POST | `/api/providers/active` | Сменить активного провайдера (`{"provider": "groq"}`) |
| GET | `/api/personas/{p}/config` | Конфиг персоны: settings, stm_size, features |
| PUT | `/api/personas/{p}/config` | Обновить конфиг (settings/stm_size — сразу, features — после рестарта) |

В API-режиме фоновые циклы бота (напоминания, обучение, проактивность) запускаются автоматически при первом обращении к персоне. Их сообщения складываются в inbox очередь (in-memory, до 100 шт.) — фронт забирает их polling'ом раз в 15 секунд.

Фичи ядра (дела, напоминания, обучение, проактивность) ключуются по `chat_id` — веб-фронт использует `chat_id=web_user`. Если модуль у персоны выключен в YAML, эндпоинты возвращают пустые списки или 400.

### Пример

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"persona": "connor", "message": "Привет!", "user_id": "web_user"}'
```

Ответ: `{"reply": "...", "extra_messages": [...], "question_kind": null, "persona": "connor", "chat_id": "web_user"}`.

### Веб-фронтенд

React-фронт (`web/`) при доступном бэкенде автоматически работает с реальными данными: список персон, история чата и отправка сообщений, STM/LTM в досье. Без бэкенда — моковый режим прототипа. Адрес API задаётся переменной `VITE_API_URL` (по умолчанию `http://127.0.0.1:8000`), токен — в localStorage под ключом `vpc-api-token`.

```bash
cd web && npm run dev   # http://localhost:5173
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

CMD ["python", "-m", "app.main"]
```

Особенности образа:
- Используется `python:3.11-slim` для минимального размера
- PyTorch устанавливается в CPU-версии (`--index-url https://download.pytorch.org/whl/cpu`)
- Модель `paraphrase-multilingual-MiniLM-L12-v2` предзагружается на этапе сборки, ускоряя старт контейнера

### docker-compose.yml

```yaml
services:
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
# Запуск Telegram-бота (раскомментируйте в docker-compose.yml)
docker-compose up -d connor

# Запуск с интерактивным меню (через docker run)
docker run -it --rm --name vp-menu --env-file .env -v persona_data:/app/data virtual-persona

# Запуск всех сервисов
docker-compose up -d
```

**Интерактивное меню через `docker run`:**

Команда `docker run -it --rm ... virtual-persona` запускает контейнер с TTY и интерактивным вводом, позволяя выбрать режим работы (Connor, Arrodes или все боты). Флаги:
- `-i` — интерактивный режим (stdin открыт)
- `-t` — pseudo-TTY (терминал)
- `--rm` — удалить контейнер после остановки
- `--env-file .env` — загрузка переменных окружения
- `-v persona_data:/app/data` — сохранение данных между запусками

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
# Батч-экстракция: каждые 15 сообщений пользователя (light-режим — 6,
# под размер контекста ответа) — один LLM-вызов по последним N сообщениям.
def add_message(self, role: str, content: str, user_id: str = "default", ...,
                light_mode: bool = None):
    self.stm.add_message(role, content, user_id, chat_id, user_name)
    if role == "user" and self.enable_ltm_extraction:
        every = EXTRACT_EVERY_LIGHT if light_mode else EXTRACT_EVERY  # 6 / 15
        if self._extract_counters[user_id] >= every:
            self.ltm.extract_facts_async(batch_text, user_id, ...)
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

**Провайдер `webchat` — веб-чат вместо API-ключей** (`app/features/web_llm.py`): промпт уходит в чат deepseek/qwen/claude в Chrome пользователя (тот же CDP-механизм, что и у computer_control) и ответ читается из DOM. Свежий чат на каждый вызов — стейтлесс, контекст веб-чата не становится вторым неконтролируемым слоем памяти; ввод — мгновенный `fill()` (посимвольный набор системного промпта занял бы минуты); конец стриминга — по стабильности текста в опросе (2 одинаковых непустых замера подряд, таймаут 150с). Самолечение битых чатов: баннер ошибки сайта (qwen «Oops!… parent_id is not exist», deepseek «Length limit reached. Please start a new chat.» — div с хэш-классом вне блоков ответа, ловится пробой по видимым коротким текстам страницы) → чат сбрасывается, открывается новый, промпт отправляется повторно. Защита аккаунта: пейсинг ≥5с между вызовами и дневная квота 80 (`data/<context>/computer_control/web_llm_state.json`). Позиция в цепочке: по умолчанию после облачных, перед local; в fallback-списке персоны — на своей позиции; `primary: webchat` — основной (включая режим «вообще без ключей», тогда активируется по `WEBCHAT_SITE`). Включение: `llm.webchat: qwen` в YAML персоны или env `WEBCHAT_SITE=deepseek|qwen|claude`. Любая неудача (сайт сломал вёрстку, таймаут, квота) — None, цепочка идёт дальше. Честные ограничения: ToS веб-чатов автоматизацию не приветствует (квота/пейсинг смягчают, не устраняют), латентность 10–60с, стриминга токенов нет, структурные ответы — через «ответь строго JSON» + `extract_json`. Smoke-тест: `python -m scripts.test_web_llm` (14 проверок).

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
---

## Актуальное состояние (обновление `update`)

Крупное обновление платформы: переработанный книжный RAG, повторяющиеся напоминания, мультитомный поиск, динамический глоссарий и система оценки качества ответов.

### Персоны и их роли

- **connor** — основной ассистент (tier `bot`): память, напоминания, todo, инвентарь, обучение, проактивность, веб-поиск, файлы, управление компьютером.
- **arrodes** — путеводитель по лору «Lord of the Mysteries» с полным ассистентным набором: отвечает по книге через RAG и при этом умеет напоминать, вести todo, инвентарь и проявлять инициативу, как обычный бот.
- **arrodes_master** — отдельная персона режима «Великий Мастер» для особого пользователя.
- **assistant** — дружелюбный ИИ-ассистент (минимальный пример персоны).
- **alex** — «друг постарше» (tier `normal`): разговаривает по-человечески, без ассистентских уточнений.
- **verso** — ролевая персона (Verso из Clair Obscur: Expedition 33, tier `normal`).
- **pip_the_sprite** — лесной спрайт (tier `primitive`): речь простыми фразами, жесты вместо объяснений.

### Книжный RAG (`app/features/book_search.py`)

Гибридный поиск по книге: вектор (`intfloat/multilingual-e5-base` с префиксами `query:`/`passage:`) + BM25 → слияние через RRF (Reciprocal Rank Fusion) → cross-encoder rerank → дедупликация по главам. Поверх:

- **Query fan-out**: перевод через локальную Ollama, дистилляция запроса, алиасы имён и транслитерация, расширение по путям/последовательностям, concept-синонимы, **аспектный сплит** многоаспектных вопросов.
- **Small-to-big**: подтягивание соседних чанков сцены (±1) для глав выдачи.
- **Rescue с якорем на сцену**: аспектные чанки гарантированно доходят до контекста, без keyword-шума других томов.
- **Мультитомный поиск**: вопросы вида «год в первом томе и в 8-м» — поиск по каждому упомянутому тому с гарантией покрытия.
- **Маркеры источников `[ФN]`**: модель помечает факты номером фрагмента; код срезает маркеры и удаляет строки со ссылкой на несуществующий фрагмент (анти-галлюцинации).

### Динамический глоссарий (`app/features/glossary_context.py`)

Вместо всего словаря (~17k токенов на каждое сообщение) в промпт идёт только релевантное (~5 КБ): записи из вопроса и фрагментов, ядро частых терминов, справочные заметки, **таймлайн последовательностей** персонажей по томам (`arrodes_seq_timeline.yaml`) и **принадлежность к организациям** (`arrodes_affiliations.yaml`). Экономия ~40% промпта.

### Напоминания (`app/features/reminder_manager.py`)

- Обычные: «напомни через 2 часа / завтра в 12».
- **Повторяющиеся**: «напоминай каждый день в 9», «по пятницам в 18:00» — по локальному времени устройства, автоматическое перепланирование после срабатывания.
- Хранится автор; при срабатывании бот тегает (`@username`) или называет по имени; `/reminders` показывает автора и расписание.

### Суточный ритм (`app/features/rhythm_manager.py`)

Персона живёт в ритме суток пользователя (Telegram и веб):

- **Утреннее приветствие** — «доброе утро» в характере персоны, когда пользователь включает ноутбук утром. Бот работает на той же машине, поэтому фоновый цикл ловит момент пробуждения (во время сна ОС wall-clock уходит вперёд относительно monotonic — эта разница и есть детект). Запасной триггер — первое сообщение в TG или открытие веб-чата в утреннем окне после долгой паузы. Один раз в день, на чат.
- **Ночное «пора спать»** — в полночь (настраивается), но только если пользователь был активен в последние ~2 часа: утром человек не должен находить «иди спать», отправленное в пустоту. Один раз за ночь.
- **Погодные предупреждения** — прогноз Open-Meteo раз в 30 минут: сейчас сухо, но осадки в ближайшие часы; гроза; перепад температуры ≥ 8°C за полсуток. Нужна настроенная локация (веб-настройки окружения, `data/env_location.json`); без неё погодная часть просто не срабатывает. Кулдауны: 6 ч на осадки/грозу, 12 ч на перепад.

Текст генерируется LLM в характере персоны (при сбое — короткий шаблон), язык — по последним сообщениям чата. Отправка через общий sender: Telegram напрямую, в вебе — inbox с уведомлениями. Замороженная персона (`muted`) молчит, событие сгорает. Отправленные события отмечаются в досье чата (`record_event`: последние 3 видны в контексте досье — персона знает, что уже здоровалась/предупреждала); отключается настройкой `dossier: false`. Фича **живая**: включается/выключается в веб-настройках без рестарта. Состояние — `data/{context}/rhythm_state.json`. Smoke-тест: `python -m scripts.test_rhythm`.

```yaml
features:
  rhythm:
    enabled: true
    dossier: true             # отмечать события в досье чата (ChatDossier)
    morning_greeting:
      enabled: true
      window_start: 5        # утреннее окно [5, 12), часы локального времени
      window_end: 12
      min_gap_hours: 4       # пауза до появления ≥ 4 ч = «начало нового дня»
    sleep_nudge:
      enabled: true
      bedtime_hour: 0        # полночь
      active_within_minutes: 120   # слать только при активности за это время
    weather_alerts:
      enabled: true
      check_interval_minutes: 30
      rain_lead_hours: 3     # предупреждать за 3 ч до осадков
      temp_delta_c: 8        # перепад температуры за ~12 ч
```

### Оценка качества ответов (`qa_eval/`)

Воспроизводимый harness тестирования лор-бота: генерация QA-пар по чанкам томов → прогон через продовый retrieval+персону → ответы и независимая оценка. Отчёты по всем 8 томам (`qa_eval/*/report.md`) и инструкции запуска (`extract_chunks.py`, `run_retrieval.py`, `final_run.py`).

---

## Система «живой» персоны (living persona)

Персона имеет состояние и историю, которые существуют и меняются **между** сообщениями пользователя. Разделение труда: **локальная Gemma** (Ollama) делает частые структурные операции (тики состояния, черновые события, фильтрация), **основная LLM** — редкие высокостейковые генерации, которые видит пользователь (реплики, эпизоды, приветствия). Любой финальный текст идёт через полный `system_prompt` персоны — голос персонажа не может «утечь» через черновую генерацию.

### Слои

| Слой | Модуль | Что делает |
|---|---|---|
| Выжимка персоны | `app/core/persona_context.py` | Разово извлекает основной LLM из `system_prompt` компактную выжимку: `personality_summary`, `speech_dna`, `behavioral_rules`, `baseline_mood`, `interests`, `role_context`, `world_binding`. Кэш по sha256 промпта — правка персоны инвалидирует автоматически. При недоступности LLM — regex-черновик |
| Состояние | `app/core/state_engine.py` | Тики каждые N минут: energy/mood/pastime/location по чату + append-only `offline_log`. Gemma — структурный JSON-тик **со встроенным скорингом инициативы** (один вызов вместо двух; в скоринг передаются proactive-настройки персоны из YAML), при недоступности — детерминированный дрейф (энергия днём падает, mood тянется к baseline). Скоринг 0..1 — дополнительный сигнал в proactive |
| Мир | `app/core/world_engine.py` | NPC/места/storylines (JSON в `data/{ctx}/living/world.json`): засев из `system_prompt` при создании, детекция новых сущностей из диалога (Gemma), генерация офлайн-событий 1-3/день с mood-эффектами |
| Суммаризация | `app/core/offline_summarizer.py` | Ежедневно: offline_log → тезисы (Gemma) → эпизод дневника (основная LLM, полный system_prompt → self_memory). Приветствие-дневник при возврате после 12+ ч отсутствия. Раз в 1-2 недели «сценарист» продвигает сюжетные арки |
| Оркестратор | `app/core/living_persona.py` | Единый фоновый цикл, контекст для промптов, снимок для UI |

### world_binding: реальный мир vs собственная вселенная

`persona_context.world_binding.type` определяется при извлечении:
- **`real_world`** — карточка явно привязывает персонажа к нашей реальности (реальный город, «живёт здесь и сейчас»). Только таким персонам доступны внешние стимулы (§5): web_search по интересам → Gemma relevance/safety-фильтр → пул стимулов.
- **`fictional_universe`** — собственная вселенная (Коннор: Detroit: Become Human). Реальный интернет **никогда не подключается** — жёсткий gate в коде (`external_stimuli_allowed`), не только флаг в YAML. Мир «живёт» через генерируемые внутримировые факты («в Детройте похолодало» — факт его Детройта).
- `unspecified` → трактуется как `fictional_universe` (безопасный дефолт).

### Конфигурация (в YAML персоны)

```yaml
features:
  world_lore:
    enabled: true
    npc_seed_on_create: true     # засеять NPC/места из system_prompt
    max_active_storylines: 2
    events_per_day: [1, 3]       # офлайн-событий в день
  state_engine:
    enabled: true
    tick_interval_minutes: 20    # частота тиков состояния (чаты, молчащие 72+ ч,
                                 # тикаются в 6 раз реже — §3.2 «неактивным реже»)
    use_gemma: true              # false — строго эвристический дрейф, без локальной LLM
  external_stimuli:              # работает только для real_world-персон (жёсткий gate в коде)
    enabled: false               # необязательный: без ключа дефолт = (world_binding == real_world);
                                 # явное значение — ручной override (в т.ч. выключение real_world)
    allowed_categories: []       # whitelist тем web_search; пусто — из persona_context.interests
  ui_room_mood_sync: true        # живые данные в комната/настроение веб-UI
```

### Точки выхода к пользователю

- **Промпт ответа**: состояние + последний офлайн-факт уходят в `prepare_messages` (`living_context`) — персона «помнит», чем занималась.
- **Proactive**: состояние и непотреблённые факты жизни — в промпте монолога; новый тип инициативы `state_change` (реакция на собственную жизнь). Скоринг инициативы движка состояния — дополнительный триггер.
- **Напоминания**: текущий mood/energy — в контекст генерации текста.
- **Возврат после 12+ ч**: факты за период отсутствия → тезисы → вплетаются в первый ответ (приветствие-дневник).
- **Self-memory**: дневные офлайн-эпизоды пишутся в дневник (`add_external_episode`).
- **UI**: `GET /api/personas/{p}/state` — energy/mood/pastime/location, storyline, лента событий; вкладка «Комната» веб-фронта показывает живые значения вместо моковых (только при `ui_room_mood_sync: true`). Там же — блок `metrics`: операционные счётчики движков (тики gemma/heuristic, прореженные тики, события, стимулы fetched/filtered/failed, help-детекции) для наблюдаемости при раскатке.

### Запуск

Цикл стартует автоматически в Telegram-режиме (`app/main.py`) и API-режиме (`app/api/inbox.py`), если у персоны включён `state_engine` или `world_lore`. Хранение — JSON в `data/{context}/living/`, никакого выделенного сервера не нужно. Gemma не обязательна: без Ollama система деградирует до эвристического дрейфа и прямых тезисов.

Smoke-тест связки: `python -m scripts.test_living_persona`.

---

## Уровни интеллекта персон (intellect tiers)

Отдельное измерение поверх характера: уровень задаёт **какие модули доступны** и **как персона отвечает на просьбы о помощи**. Характер внутри рамки — по-прежнему зона `system_prompt`.

### Три уровня

| | `primitive` | `normal` | `bot` |
|---|---|---|---|
| Сущность | животное, дух, примитивный робот | человек | высокий интеллект |
| Речь | простые фразы/жесты | человеческая | развёрнутая по требованию темы |
| Просьба о помощи | действие/минимальный жест, без объяснений (`action_only`) | коротко по-бытовому, без ассистентских уточнений (`casual_human`) | полный разбор: расчёты, код, уточнения (`full_assistant`) |
| Дневник self-memory | вспышки-впечатления («Тепло. Дремал.»), без заметок-рефлексий, life_summary = список паттернов | полный | полный |
| Мир (NPC/арки) | выключен; опционально частичный (state + события) | полный | полный |
| Офлайн-события | физические действия, в т.ч. с инвентарём (достал/сгрыз/потерял — реально меняет инвентарь) | как в living-плане | как в living-плане |
| Proactive | только практические триггеры (дела/предметы/события) | полный баланс типов | полный баланс типов |
| Напоминания | почти шаблонная минимальная вербализация | в характере персоны | в характере персоны |

`normal` — не «глупее» `bot`: это другой режим помощи. Точность — не обязанность человека-собеседника, и он не задаёт уточняющих вопросов в стиле ассистента.

### Конфигурация

```yaml
intellect:
  tier: normal                 # primitive | normal | bot
  overrides:                   # для нетипичных персон своего tier
    self_memory_mode: null     # none | primitive | full (null = дефолт tier)
    world_lore_enabled: null   # null = дефолт tier
    help_response_style: null  # action_only | casual_human | full_assistant
```

**Персоны без блока `intellect` работают в legacy-режиме** — уровневые механики не активируются вообще (рекомендация плана: ручная разметка tier, автодетект рискован). Размечены: Коннор — `bot`, Ассистент — `bot`, Alex и Verso — `normal`; шаблон `persona_template.yaml` содержит документированный блок.

### Как работает ограничение помощи

Перед генерацией ответа Gemma-классификатор определяет: это просьба о помощи в предметной области или бытовой разговор. Детекция идёт **фоном** (параллельно с rewrite/памятью/поиском — не добавляет задержки ответу) и **кэшируется** по нормализованному тексту (TTL 10 мин). Модификатор подключается **только** на help-запросах — остальное время тон персоны не трогается. Правило приоритета зашито в промпт-сборку: **tier-модификатор ограничивает, `system_prompt` наполняет характером внутри ограничения** — конфликтующие инструкции персоны перекрываются. При недоступности Gemma ограничивающие стили (`action_only`, `casual_human`) применяются всегда, разрешающий (`full_assistant`) — не подставывается.

Pipeline-проверки уровня (не только конфиг-флаги): `self_memory_mode: none` не создаёт модуль вовсе; primitive не сеет NPC и не детектирует их из диалога даже при включённом `world_lore`; `override world_lore_enabled: false` гасит слой мира целиком; инвентарные действия офлайн-событий исполняются кодом (`add`/`use`/`remove` через InventoryManager).

Smoke-тест: `python -m scripts.test_intellect` (35 проверок, включая живую Gemma-детекцию и генерацию примитивных событий).

---

## Стиль разговора: финальные вопросы (conversation_style)

Платформенное правило против «рефлекторного вопроса» — реплик вроде «А у тебя?» / «Чем ещё помочь?», которые LLM ставит в конец каждого ответа на автомате. Три слоя защиты (`app/features/conversation_style.py`):

1. **Промпт-нота** — явный запрет рефлекторного финального вопроса, вставляется **последней** в системный блок (ближе всего к месту генерации). Общесистемный модификатор, не зависит от автора персоны. Лазейка зашита в текст ноты: если другая инструкция промпта явно требует ответить вопросом (например, выбор напоминания для переноса) — она важнее.
2. **Частотный лимит, а не полный запрет** — люди тоже иногда спрашивают. Режим per-persona:

```yaml
conversation_style:
  question_frequency: rare   # none | rare | natural | frequent
```

   Без блока действует **дефолт `rare` для всех персон**, включая legacy: вопрос только если нужен по смыслу, и не чаще чем через сообщение (серия ≤ 1 подряд). `none` — финальных вопросов нет вообще; `natural`/`frequent` — платформа не вмешивается (для персон, где вопросы — часть характера; так размечены Verso и Арродес — у него финальный вопрос обязателен по принципу взаимности).
3. **Пост-обработка (предохранитель)** — если ответ всё же закончился вопросом сверх лимита серии (считается по STM: сколько последних ответов бота подряд заканчивались «?»), выполняется **одна регенерация** с усиленным напоминанием: модель сама решает, был ли вопрос нужным — нужный оставляет, рефлекторный переписывает. Программной обрезки текста нет (рискованна грамматически). Регенерация пропускается для ответов с функциональными маркерами (`[TODO_…]`, `[INVENTORY_…]`, `[PUNISH:…]`, `[ФN]`) — потерять маркер хуже, чем пропустить вопрос. Учебные сообщения (`learning_context`) правилом не затрагиваются — там вопросы часть механики курса.

Правило действует и в light-режиме (когда отвечает локальная модель): маленькие модели подвержены тику сильнее всего. Черновики Gemma из living-движков до пользователя напрямую не долетают — там предохранитель не нужен. Счётчики (`notes_applied`, `regen_attempts`, `regen_model_kept_question`, `regen_failures`) — в `get_state_for_ui` → `metrics.conversation_style`.

Smoke-тест: `python -m scripts.test_conversation_style` (38 проверок).

---

## Управление компьютером пользователя (computer_control, уровень 1)

Бот может **открывать сайты, запускать приложения и выполнять именованные задачи** на компьютере пользователя (macOS/Windows). Детерминированно, без vision-агента: LLM пишет маркер в ответе — тот же паттерн, что `[TODO_ADD:…]` / `[INVENTORY_ADD:…]`.

Режим включается/выключается как обычная фича: чекбокс «Управление компьютером» в досье → «Настройки» → «Фичи» (применяется на живую, без перезапуска) или кнопка в досье → «Инструменты». Выключение пишет `enabled: false` внутрь dict конфига — allowlist'ы сайтов/приложений при этом сохраняются и вернутся при повторном включении. Пока режим выключен, бот не выполняет никаких команд управления: ни fast-path («открой X», «нажми X»), ни маркеров из ответа LLM.

```
[OPEN_URL:https://example.com]   открыть сайт (только http/https)
[OPEN_APP:ключ]                  запустить приложение из allowlist'а
[RUN_TASK:ключ]                  выполнить именованную команду из allowlist'а
```

Конфигурация (`app/features/computer_control.py`, по умолчанию выключено):

```yaml
features:
  computer_control:
    confirm: true            # подтверждение в чате перед исполнением (дефолт)
    allow_domains: []        # пусто = любые http(s); иначе whitelist доменов
    sites:                   # алиасы частых сайтов: мгновенный и точный резолв «открой X»
      ютуб: youtube.com      #   (дальше — история браузера, затем поиск DDG)
      кинопоиск: kinopoisk.ru
    search:                  # «включи X на ютубе»: URL-шаблон поиска по сайту, {q} — запрос
      ютуб:                  #   first: regex ссылки первого результата — открыть сразу его
        url: "https://www.youtube.com/results?search_query={q}"
        first: '/watch\?v=[\w-]{11}'
      кинопоиск: "https://www.kinopoisk.ru/index.php?kp_query={q}"
                             #   без first: выдача закрыта антиботом (sso.passport.yandex)
    apps:                    # ключ → что запускать (строка или per-OS: darwin/win32)
      safari: Safari
      chrome: {darwin: "Google Chrome", win32: "chrome"}
    tasks:                   # ключ → shell-команда (строка или per-OS)
      музыка: {darwin: 'shortcuts run "Музыка"'}   # на macOS — шаблоны Shortcuts
```

### Режим управления

Computer control работает **только в режиме управления** — он включается на чат командой «перейди в режим управления» и выключается «выйди из режима управления». Вне режима браузерные команды («открой …», «нажми …», «введи …», сценарии) не перехватываются и уходят в обычный диалог, а LLM не получает инструкцию о маркерах управления (не изображает «Открыл»). На время режима наоборот приглушены фичи-слова: напоминания («напомни»), список дел, инвентарь и обучение («научи») не разбираются — чтобы не спорить с командами управления. При выходе из режима подвисшие состояния чата (pending-подтверждение, прогон/запись сценария) сбрасываются. Режим хранится в памяти процесса per chat: перезапуск бота его сбрасывает.

### Быстрый путь «открой X»

Голая команда («открой ютуб», «открой мне сайт нгту», «запусти телеграм», «open discord.com») перехватывается в самом начале `process_message` и обслуживается **без LLM-пайплайна**: резолв по цепочке `apps`/`tasks` → алиасы `sites` → домен с точкой → **история браузера** → лёгкий поиск DDG (без улучшения запроса и загрузки страниц). Поисковый резолв берёт первый результат с совпадением по домену (кириллица — через перевод, «ютуб»→youtube.com; punycode-домены .рф декодируются), для мультисловных названий — по всем словам в домене+пути или заголовке (сами слова или перевод/основа: «гугл карты» → google.com/maps, «кутузовой нгту» → страница Кутузовой на ciu.nstu.ru); соцсети/Википедия/магазины приложений/контентные площадки отбрасываются, если запрос не называет их самих. Итоговый URL: сайт-запрос → корень (+путь до сегмента со словом запроса: /maps), страница-запрос → страница целиком. Слепого «первого результата» нет: нет совпадения — уточнение, а не чужой сайт. Ключи apps/tasks/sites матчатся и по основе слова («включи музыку» → ключ «музыка»). Ответ-вопрос и подтверждение «да»/«нет» — шаблонные, мгновенные (~0.1–0.3 с на тёплом инстансе против ~15–20 с через LLM). Если резолв не удался (или это не голая команда) — сообщение проваливается в обычный LLM-путь с маркерами. Стоп-лист (`открой дверь/напоминание/…`) защищает соседние фичи и ролеплей.

**Мультикоманды**: «открой ютуб и кинопоиск», «открой ютуб и запусти музыку» — части после «и» (со своим глаголом/филлерами) резолвятся каждая, исполняются по одному подтверждению; если хоть одна цель не резолвится — вся фраза уходит в LLM-путь.

**Тихое открытие вкладки** (macOS): открытие сайта делает новую вкладку **активной** в окне браузера, но окно не всплывает поверх занятий пользователя и фокус не крадётся. Вкладка создаётся фоном (`Target.createTarget background:true`), затем переключается AppleScript'ом `set active tab index` без `activate`; если страница перехватывает фокус (чаты с автофокусом поля ввода и т.п.) — фокус тут же возвращается прежнему frontmost-приложению. CDP-пути `Page.bringToFront`/`Target.activateTarget` для этого не годятся — они активируют приложение целиком. Любая неудача (не macOS, нет разрешения на автоматизацию) — вкладка просто остаётся фоновой: тишина важнее переключения. Явная команда «перейди на вкладку X» по-прежнему поднимает окно — это осознанный запрос «покажи».

**Поиск на сайте**: «включи интерстеллар на кинопоиске», «open X on youtube» — по шаблонам из `search:` (сайт матчится по основе слова: «на кинопоиске» → «кинопоиск»; ключи дублируются кириллицей и латиницей). Филлер в начале запроса («открой ВИДЕО winter is here… на ютуб») срезается — в поиск идёт только название; лимиты длины — 120 символов команда, 80 запрос. Глагол решает, ЧТО открыть: «открой/включи/play/open…» — при наличии у сайта regex `first` (словарная форма) одной загрузкой выдачи извлекается ссылка первого результата и открывается сразу она («открой utopia show на ютуб» → само видео); «найди/поищи/find/search…» — всегда страница поиска сайта. При любой неудаче извлечения (сеть, капча, смена вёрстки) — тихий фолбэк на страницу поиска. Подтверждение различает режимы: «Открыть «X» на ютуб?» vs «Найти «X» на ютуб?».

**История браузера** (`app/features/browser_history.py`): Chrome/Edge/Chromium/Firefox хранят историю в SQLite — читается копия файла (браузер держит БД залоченной), read-only, наружу не уходит ничего; Safari на macOS закрыт TCC — читается только с выданным Full Disk Access, иначе тихо пропускается. Кэш 10 мин, порог ≥3 визитов, localhost отбрасывается. Заголовки почтовых/соцсетевых хостов (gmail, x.com, vk, t.me…) в матче не участвуют — тема письма «платформа» не должна делать gmail «платформой». Однословный запрос → корень самого посещаемого подходящего хоста, мультисловный → сама страница из истории.

Принципы безопасности:

- исполняется **только** то, что описано в allowlist'ах yaml — свободный текст от LLM в shell не попадает никогда (неизвестный ключ/домен отклоняется);
- по умолчанию действие **не исполняется сразу**: маркер складывается в pending (TTL 5 мин), бот спрашивает подтверждение в своём стиле, следующее «да»/«нет» перехватывается и исполняется/отменяется (ответ «не да и не нет» не перехватывается — уходит в обычный поток);
- `file://`, `javascript:` и прочие не-http(s) схемы отклоняются;
- каждое исполнение пишется в аудит: `data/{context}/computer_control/audit.jsonl`.

Исполнение по платформам: URL — через `webbrowser`; приложения — `open -a` (macOS) / `start ""` (Windows); задачи — shell-команда из yaml (на macOS сюда ложится `shortcuts run "…"`, что даёт всю мощь шаблонов Shortcuts: музыка, заметки, умный дом). «Нажимать кнопки» внутри произвольных окон этот уровень не покрывает — это уровень 3 (vision-агент), см. обсуждение в истории проекта.

### Браузерные рецепты (этап 3b)

Значение задачи вида `recipe:<id>` исполняет не shell, а рецепт из реестра `app/features/browser_actions.py` — JS-сниппет внутри уже открытой вкладки. LLM в браузер не лезет: доступны только рецепты реестра, на которые ссылается allowlist `tasks`. Нет браузера/вкладки/разрешения — задача вежливо не выполняется с подсказкой, что включить. Пилотный набор: `youtube_toggle` (пауза/плей), `youtube_next`, `youtube_mute`, `kinopoisk_first`, `search_first`/`search_pick:N` (первый/N-й результат на ютубе/кинопоиске/гугле — «запусти третий результат», «второе видео», «5 результат»; номерные фразы распознаются кодом, отдельных ключей в yaml не нужно; на ютубе работает и на выдаче, и на главной, и в up-next — заголовочные ссылки всех раскладок, включая lockup-вёрстку 2025). Скоп «в плейлисте» («открой третье видео в плейлисте») — отдельный рецепт `playlist_pick:N`: сначала пункты панели/страницы плейлиста, фолбэк — общий список видео; «в выдаче/в поиске/в списке» просто срезается.

**Браузерный бэкенд — единый CDP на обеих ОС**: Chrome/Edge с `--remote-debugging-port` + Playwright `connect_over_cdp` (постоянное подключение в выделенном потоке, реестр отслеживаемых вкладок). Браузер бот умеет запускать сам (`browser.launch: true`, дефолт): бинарь авто-детектируется (Chrome/Edge), профиль — `browser.user_data_dir` из конфига или выделенный automation-профиль по умолчанию. Занятый профиль детектируется по `SingletonLock` (живой pid → понятная инструкция закрыть Chrome; протухший лок снимается). ВНИМАНИЕ: с Chrome 136+ `--remote-debugging-port` игнорируется для профиля по умолчанию — поэтому дефолтный профиль выделенный, а на основной профиль (`~/Library/Application Support/Google/Chrome`, `%LOCALAPPDATA%\Google\Chrome\User Data`) имеет смысл переключать только Chrome <136. Если CDP недоступен (Chrome уже открыт без отладки, политика безопасности), на macOS остаётся fallback на AppleScript (JS в реальной вкладке через Apple Events); `browser.backend: cdp|applescript` фиксирует бэкенд явно — applescript даёт работу в основном окне Chrome с учётками пользователя, но только на macOS и ценой синтетического JS-клика вместо playwright-клика. Перед снапшотом страница ждёт готовности: load → networkidle (best effort) → стабильность DOM (2 одинаковых опроса хэша подряд с шагом 250 мс), общий бюджет 6 сек — дальше работаем с тем, что есть.

**Агентный клик** («нажми „скачать"», «кликни „войти" на гитхабе»): без рецепта под сайт. Снапшот собирает видимые кликабельные элементы вкладки (ссылки, кнопки, инпуты; плюс иконки-раскрыватели JS-меню — img/svg с class/src вида `menu-open`, `chevron`: подписью служит текст родительского пункта; плюс текстовые пункты JS-меню без ссылки — элементы с menu-похожим классом или кастомным polymer-тегом (`NSTUMenuThemeFolderLinkText`, `ytd-compact-link-renderer`; «мёртвые» якоря `<a href="">` за ссылки не считаются); пункты открытого попапа/выпадашки собираются первыми, deepest-only — иначе лента youtube выедает бюджет в 100 элементов раньше них) с текстом, `aria-label`, `title`, `alt`/`placeholder`, ролью и реальной видимостью (rect + `getComputedStyle`, флаг «во вьюпорте»). Выбор элемента — **скоринг**, а не бинарный матч: точный текст > точный aria-label/title > основы слов (плюс словарь синонимов `_GOAL_SYNONYMS` — «аватар» засчитывается за «Меню аккаунта»: иконочные кнопки пользователь зовёт не по aria-label) > подстрока, со штрафами за крошечный размер, позицию вне вьюпорта и позднее место в DOM. Явный лидер (скор ≥ 60 и отрыв ≥ 15, либо единственный кандидат ≥ 50) — без LLM. Иначе в LLM уходят **только top-5 кандидатов** (текст+тег+роль) с требованием ответить строго одной цифрой; «нет» → честный отказ, невалидный ответ → фолбэк на лучшего по скору (≥ 50) или отказ — парсинг не «докручивается». Клик на CDP — настоящий playwright-клик (скролл, actionability, фолбэк force), а не слепой `el.click()`. **Closed-loop проверка**: после клика бот ждёт до 3 сек изменения состояния (URL/DOM-хэш); нет изменений — честное «клик отправлен, но не уверен, что сработало» (отдельный класс ошибки `uncertain`, не путать с «элемент не найден»). Подтверждение показывает, что именно нажмётся: «Нажать „Скачать" на github.com?». Цель вкладки: сайт из команды (алиасы `sites`) → последняя открытая ботом вкладка → активная; если активная — сам чат, бот честно просит назвать сайт (кликать произвольную вкладку опаснее, чем спросить). Неудачи (нет вкладки, элемент потерян, нет подходящего элемента) возвращаются честным текстом — в LLM-путь команды клика не проваливаются, чтобы модель не «изображала» выполнение. Клик — отдельный под-переключатель: `click: false` в конфиге `computer_control` (или чекбокс в досье → «Инструменты») выключает только клики, оставляя открытие сайтов/приложений. **Скоуп-клик** («нажми „выбрать“ на Цезарь с беконом»): каждый элемент снапшота несёт `ctx` — текст ближайшего предка-контейнера (карточки); когда плоский матч пуст, слова действия ищутся в тексте элемента, а слова скопа — в `ctx` (точное фразовое попадание скопа отсекает совпадения по отдельным словам: «Цезарь с беконом» побеждает «Цезарь с сыром и беконом»). Однословный скоп, съеденный site-регексом («на маргарите»), возвращается в цель, если это не алиас и не домен; слова-пустышки («на странице/сайте») — не возвращаются.

**Скачивание** («скачай язык структурных запросов sql на ciu.nstu.ru», «скачай файл отчёт»): тот же снапшот и скоринг, что у клика, но у найденного элемента берётся `href` и файл скачивается принудительно — кликом синтетического `<a download>` внутри страницы (прямой клик по ссылке с `target=_blank` открывал бы мёртвую вкладку вместо загрузки). На CDP старт скачивания подтверждается событием `download` (5 сек; нет события — «не уверен, что началось»). У элемента без `href` (иконка меню, кнопка) — честный отказ «не ссылка на файл». Подтверждение: «Скачать „Язык структурных запросов SQL…" с ciu.nstu.ru?». Гейт — тот же `click`.

**Многошаговая навигация** («открой на ciu.nstu.ru/kaf/persons/827 студентам - Технологии баз данных - Методические указания»): явный адрес в команде открывается, а хвост по сепараторам « - »/«→» проходится кликами по пунктам страницы — включая раскрытие древовидных меню. Структура шагов — детерминированная state-machine в коде (не в LLM): каждый шаг — снапшот → выбор элемента (скоринг → при неоднозначности LLM) → клик → проверка эффекта → следующий шаг. Подтверждение показывает весь путь: «Открыть … и пройти: студентам → Технологии баз данных → Методические указания?». Страницу после открытия/перехода бот ждёт до 10 секунд с повторными снапшотами; «элемент потерян» (DOM перерисовался между снапшотом и кликом) или «клик без эффекта» — один повтор шага. Вкладка открывается отслеживаемой (стабильный id — CDP-реестр или AppleScript-id) и вся навигация идёт по ней — старые вкладки того же сайта и порядок окон не мешают. Осечка честная: «не нашёл на странице пункт „X" (прошёл: студентам)».

**Ввод текста** («введи новосибирск в поле поиск», «введи в поле поиск новосибирск», «напиши привет в чат с оператором»): поля ввода — часть того же снапшота (input/textarea/contenteditable/role=textbox с флагом `ed`; подпись поля — `aria-label` → связанный `<label>` → `placeholder` → `name`). Поле и текст разделяются по странице, а не по грамматике: «ТЕКСТ в поле ПОЛЕ» — скорингом по подписям полей, «в поле ПОЛЕ ТЕКСТ» — префиксным матчем подписи (по основам слов), без предлогов — матчем цепочки слов подписи внутри фразы; одно поле на странице + одно слово — в него. Сайт с конца фразы срезается, только если разрешается (алиас `sites`/домен): «в чат» — это поле, а не сайт. **Гео-плейсхолдер**: «введи мой город» (или «…город в поле поиск») подставляет город из местоположения пользователя (`env_location.json`; досье → «Настройки» → местоположение) — город диктовать не нужно; местоположение выключено — честная просьба назвать город текстом. Ввод на CDP — фокус кликом → очистка → **посимвольный набор** (реальные key-события: suggest-виджеты вроде выбора города реагируют именно на них), фолбэк `fill()`; на AppleScript — native setter + события `input`/`change` (React-совместимо). **Closed-loop**: значение поля читается обратно, несовпадение — честное «не уверен, что ввод сработал» (`FillUncertain`, класс `uncertain`), а не тихое «Введено». Хвост **«…и отправь»** («введи привет и отправь», «в поле сообщение X и отправь»): после ввода жмётся Enter в том же поле — для чатов, где кнопка отправки безымянная иконка (chat.deepseek.com: `div.ds-button` без текста/aria — в снапшот не попадает вовсе); подтверждение отправки — поле очистилось или страница изменилась, иначе `FillUncertain`. Только CDP (на AppleScript — честный отказ). Standalone **«отправь»/«send»** («отправь сообщение», «отправь на кладе») — Enter без ввода: цель выбирает JS — единственное непустое поле → поле в фокусе → единственное поле; несколько пустых — честная просьба кликнуть нужное; та же closed-loop проверка отправки. В обычный LLM-поток уходят только просьбы сгенерировать текст без явных признаков ввода («напиши письмо», «эссе в стиле классиков» — голое «в/во» маркером не считается) и идиома «введи меня в курс дела»; безусловно «нашей» команде (сепаратор «в поле», сайт, гео-плейсхолдер, одно слово-значение вроде email) любая неудача возвращает честный текст с подсказкой по видимым полям — иначе модель изображает ввод, которого не было («Успешно введено» при пустом поле: сама команда распознана, но страница в переходном состоянии отдала (None, None) — больше не отдаёт). В системной инструкции модели отдельно закреплено: кликов и ввода у неё нет — писать «Нажато»/«Введено» от себя запрещено (раньше модель изображала ввод, которого не было).

**Клавиши страницы** («нажми пробел», «press esc», «нажми энтер на этой странице»): специальные клавиши Space/Enter/Escape/Tab/Backspace/m/стрелки (в т.ч. «интер»/«эскейп»/«таб»/«бэкспейс»/«мьют») уходят в страницу **без выбора элемента** — в активный фокус или документ: пауза плеера, закрытие попапа, игра. Адресация вкладки — та же, что у клика (алиас/домен/«на этой странице»/последняя открытая). Разбирается ДО агентного клика — «нажми esc» не становится целью «нажать элемент esc». Нажатие — доверенное key-событие (CDP: playwright; Safari: key code через System Events); `times` — повтор (громкость стрелками). Best effort **без closed-loop проверки**: клавиша может не менять видимый DOM (canvas-игры), поэтому бот обещает только нажатие («нажал пробел на youtube.com»), не эффект. Под-переключатель — общий с кликом (`click: false` гасит и клавиши).

**Медиа-команды плеера** («пауза»/«поставь на паузу»/«плей»/«продолжи», «тише»/«громче»/«громкость вниз»/«уменьши громкость», «без звука»/«выключи звук»/«включи звук»/«мьют»): голые слова в режиме управления → клавиши YouTube (пробел = play/pause, стрелки = громкость ±10% за нажатие — «тише» жмёт дважды, m = звук выкл/вкл). Работают на youtube.com и music.youtube.com; на остальных сайтах — те же клавиши по своей семантике. Громкость жмётся на сам `<video>` с фокусом: YouTube игнорирует стрелки громкости без фокуса плеера (листает ленту). Подтверждение: «Уменьшить громкость на www.youtube.com?».

**Плеер YouTube в агентных командах**: панель управления (пауза/звук/настройки) прячется автохайдом — перед снапшотом бот раскрывает её (персистентный style-оверрайд + mousemove): «нажми пауза», «нажми настройки», крестик и меню мини-плеера становятся видимыми для скоринга. Кнопка-бургер на ru-YouTube называется «Гид» — синонимы «бургер»/«гамбургер»/«три полоски» → гид/меню/guide; «троеточие» → ещё/параметры/действия/меню (на watch-странице это «Меню действий»). «Промотай» при открытом всплывающем меню (настройки видео, dropdown) листает **меню**, а не страницу под ним.

**Контекст открытой страницы**: последняя открытая ботом страница сохраняется на диск (`last_tab.json`) и восстанавливается после перезапуска процесса — «введи мой город»/«нажми X» без названия сайта целятся в неё (вкладка находится заново по хосту, id вкладок между процессами не стабильны). Та же страница подмешивается строкой в системную инструкцию модели («Сейчас открытая мной страница: …») — LLM понимает, о каком сайте речь, и не отвечает в отрыве от браузерного контекста. **Попапы**: клик, открывший новое окно/вкладку (окно «Вход — Google Аккаунты» и т.п.), переносит отслеживание на неё — следующие «введи email»/«нажми Далее» работают уже в попапе; появление новой страницы считается изменением состояния в closed-loop клика (иначе честный клик по «Войти» выглядел бы «не сработало» — исходная страница не меняется). Если окно открыто не ботом (или раньше клика) и трекинга на нём нет — **кросс-страничный фолбэк**: элемент, не найденный на целевой вкладке (и сайт не назван явно), ищется снапшотами по остальным открытым страницам; берётся только единственный явный лидер без LLM (чат и localhost исключены), лидеры на двух страницах — честный отказ, а не гадание.

**Чтение со страницы** («прочитай последнее сообщение», «что ответил клод», «прочитай страницу на ютубе»): возвращает текст в чат. Режим `last` — последнее сообщение чата (claude.ai: строки `group/message-row` с `p.font-claude-response-body`/`[data-testid=user-message]`; chatgpt: `[data-message-author-role]`; иначе последний видимый `[class*=message]`-блок), с префиксом роли («Ассистент:»/«Вы:»), до 2000 символов; режим `page` — текст `main/article` (до 3000). Единственное действие без подтверждения: чтение ничего не меняет. Вкладка не выдёргивается на передний план (`front=False`). Нечего читать — честный текст, а не выдумка LLM.

**Закрытие попапов** («закрой окно», «сверни анкету», «закрой соусы к бортикам»): целевое закрытие ищет крестик в контексте названного блока, generic — единственный крестик модалки; авто-dismiss оверлеев при этом отключён (иначе съел бы крестик раньше команды). Если крестика нет в снапшоте (модалки без close-контрола), но диалог реально виден (`role=dialog`/`aria-modal`/`dialog[open]`/fixed-перекрытие с модальным классом) или раскрыт выпадающий список (`role=listbox/menu`, `vs__dropdown-menu`, `multiselect__content-wrapper`… — у него крестика нет по определению) — фолбэк на клавишу **Escape** с closed-loop проверкой: осталось открытым — честное «окно игнорирует Escape». **Слайдеры** («перетащи слайдер рабочие часы на 8», «выставь громкость на 5»): ползунки `input[type=range]`/`[role=slider]` выбираются по словам подписи в предках (ключ = совпавшие слова × 10 − глубина предка: своя строка бьёт общую секцию); для range — нативный value-сеттер + события `input`/`change` (React-совместимо), для кастомного — доверенный клик по точке трека; значение читается обратно, откат виджетом — честный отказ. **Выпадающие списки** («нажми пиццамейкер» → раскрылось, «нажми кассир» → выбралось): элементы открытого списка (`role=listbox/menu/option`, классы `dropdown-menu`/`select-dropdown`/`suggest`…) несут флаг `dd` с бонусом +20 — пункт открытого списка важнее одноимённого фона страницы (карточка вакансии с тем же названием). А когда у топ-кандидатов одинаковый текст (чип поля, пункт списка, карточка — все «Пиццамейкер»), LLM их в списке не различит и выбирала бы жребием — поэтому одноимённые разруливаются детерминированно по скору, без LLM.

**Наблюдаемость**: в `audit.jsonl` пишется не только исполненное действие, но и путь выбора элемента (`path`: `score`/`llm`/`llm_fallback`/`match`), рассмотренные кандидаты со скорами, сырой ответ LLM, результат closed-loop проверки (`verify`: `ok`/`uncertain`/`failed`) и класс ошибки (`error_class`). Метрика `ComputerControlManager.metrics()` — доля решений, ушедших в LLM-фолбэк, и доля валидных ответов LLM: показывает, где детерминированный слой ещё слабый.

Ответы с этими маркерами исключены из регенерации conversation_style (см. выше) — маркер нельзя потерять переписыванием. Smoke-тест: `python -m scripts.test_computer_control` (364 проверки).

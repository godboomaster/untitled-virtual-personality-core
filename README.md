# Virtual Personality Core

> Многоперсональная платформа интеллектуального диалога с многоуровневой системой памяти и отказоустойчивой маршрутизацией LLM-запросов.

Каждая персона — полноценный AI-собеседник с собственным характером, памятью и историей отношений с пользователем. Персоны живут в изолированных контекстах: факты, рассказанные одной, неизвестны другой.

---

## Возможности

| Компонент | Описание |
|-----------|----------|
| **Многоуровневая память** | STM (последние 50-60 сообщений), LTM (извлечение фактов через LLM), эпизодическая (Self-Memory — личный дневник бота) |
| **Мультипровайдерная маршрутизация** | 9 LLM-провайдеров с каскадным fallback при недоступности |
| **Конфигурируемые персоны** | YAML-файлы с системными промптами, параметрами генерации и переключаемыми фичами |
| **Веб-поиск** | DuckDuckGo с загрузкой полного текста топовых результатов, выполняется параллельно с подготовкой контекста |
| **Обработка файлов** | PDF, DOCX, TXT, MD и 30+ форматов через markitdown, векторная индексация для семантического поиска |
| **Два интерфейса** | Telegram Bot API (основной) и Gradio (веб-интерфейс для отладки) |
| **Экспорт/восстановление** | HTTP API для выгрузки памяти в JSON, восстановление из дампов |

---

## Персоны

| Персона | Описание | Особенности |
|---------|----------|-------------|
| **Коннор** | Андроид RK800 из Detroit: Become Human | file_upload, export_server, restore_memory, self_memory, web_search |
| **Арродес** | Великое зеркало из Lord of the Mysteries | rate_limit, moderation, punish_block, web_search, special_users |
| **Verso** | Бессмертное отражение из Clair Obscur | Минимальная конфигурация, англоязычный |
| **Assistant** | Базовый дружелюбный ассистент | Все фичи отключены |

---

## Архитектура

```
app/
├── core/
│   ├── persona.py           # PersonaLayer: системные промпты, special_users
│   ├── memory.py            # MemoryManager: STM + LTM фасад
│   ├── memory_config.py     # Категории фактов, промпты экстракции/слияния/консолидации
│   ├── self_memory.py       # BotSelfMemory: эпизоды, наблюдения, life_summary
│   ├── router.py            # ModelRouter: 9 провайдеров с fallback
│   ├── config.py            # Загрузка .env, конфигурация провайдеров
│   ├── file_vector_db.py    # Векторная БД для документов (чанки + полный текст)
│   ├── file_reader.py       # Извлечение текста (markitdown)
│   ├── embedder.py          # SentenceTransformer эмбеддинги
│   └── users.py             # Маппинг пользователей
├── features/
│   ├── web_search.py        # DuckDuckGo поиск с загрузкой страниц
│   ├── need_search.py       # Классификатор необходимости поиска
│   ├── file_sender.py       # Автоматическая отправка кода файлами, разбиение длинных текстов
│   ├── rate_limiter.py      # Ограничение частоты сообщений
│   ├── moderation.py        # LLM-модерация контента
│   ├── export_server.py     # HTTP API для экспорта памяти
│   ├── restore_memory.py    # Восстановление из JSON-дампов
│   └── reply_context.py     # Контекст reply-сообщений
├── personas/
│   ├── connor.yaml
│   ├── arrodes.yaml
│   ├── verso.yaml
│   └── assistant.yaml
├── bot_instance.py          # Ядро: обработка сообщений, интеграция модулей
├── telegram_bot.py          # Handlers Telegram
├── gradio_app.py            # Веб-интерфейс
└── main.py                  # Точка входа

data/                        # ChromaDB базы (персистентные, per-context)
docs/                        # Документация
memory_export/               # JSON-дампы для восстановления
```

---

## Поток обработки сообщения

```
Пользователь
    |
    v
[Telegram Bot]  -- should_respond(trigger words)? -->  [BotInstance.process_message]
                                                            |
                                    +-----------------------+-----------------------+
                                    |                       |                       |
                                    v                       v                       v
                              [STM: сохранить]      [Web Search: фон]        [LTM: извлечь факты]
                                    |                       |                       |
                                    v                       v                       v
                              [Получить контекст]    [DuckDuckGo + fetch]     [LLM экстракция]
                                    |                       |                       |
                                    +-----------------------+-----------------------+
                                                            |
                                                            v
                                              [PersonaLayer.prepare_messages]
                                                  system_prompt
                                                  + LTM факты
                                                  + Self-Memory (эпизоды + наблюдения)
                                                  + Web-контекст
                                                  + Файловый контекст
                                                  + История STM
                                                            |
                                                            v
                                              [ModelRouter.get_response]
                                                  active_provider -> fallback chain
                                                            |
                                                            v
                                              [Пост-обработка]
                                                  - Punish parsing
                                                  - Markdown cleanup
                                                  - File splitting
                                                            |
                                                            v
                                              [Сохранение ответа в STM]
                                              [Self-Memory tick]
                                                            |
                                                            v
                                              [Отправка пользователю]
```

---

## Требования

- Python 3.11+
- macOS / Linux / Windows (WSL)
- API-ключи хотя бы одного LLM-провайдера (для лучшей работы рекомендуется иметь минимум два ключа разных провайдеров)
- Telegram Bot Token (для Telegram-интерфейса, по желанию) 

---

## Установка

```bash
# Клонировать репозиторий
git clone <repo-url>
cd virtual-persona-core

# Создать виртуальное окружение
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
venv\Scripts\activate  # Windows

# Установить зависимости
pip install -r requirements.txt
```

---

## Конфигурация

Создать два файла в корне проекта:

### `.env` (секреты)

```ini
# Telegram токены
CONNOR_BOT_TOKEN=123456:ABC...
ARRODES_BOT_TOKEN=123456:DEF...

# API ключи провайдеров (один/два минимум)
ZAI_API_KEY=sk-...
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-...
GROQ_API_KEY=gsk-...
DEEPSEEK_API_KEY=sk-...
KIMI_API_KEY=sk-...
GOOGLE_API_KEY=...
MIMO_API_KEY=...
HF_API_KEY=hf-...

# ID владельца (для админ-команд)
OWNER_USER_ID=123456789
```

### `.env.config` (публичный конфиг)

```ini
# Активный провайдер
ACTIVE_PROVIDER=zai

# Память
DATA_DIR=./data
STM_SIZE=50 # Размер, сколько последних сообщений хравнится в памяти. Можно менять.
LTM_EXTRACTION_ENABLED=true
LTM_MODEL_PROVIDER=hf # Провайдер для долгосрочных фактов, можно не указывать

# Rate limiter (Арродес)
RATE_LIMIT_DEFAULT=6 # Лимит сообщений
RATE_WINDOW=3600 # Время через сколько обновляется лимит

# Экспорт
EXPORT_PORT=8080
```

---

## Запуск

```bash
# Интерактивное меню
python -m app.main

# Конкретный бот
python -m app.main connor
python -m app.main arrodes

# Все боты параллельно
python -m app.main all

# Только Gradio
python -m app.main gradio
```

---

## Docker

```bash
docker-compose up --build
```

---

## Команды Telegram

| Команда | Описание | Доступ |
|---------|----------|--------|
| `/start` | Приветствие | Все |
| `/help` | Список команд | Все |
| `/stats` | Статистика памяти (STM/LTM) | Все |
| `/clear` | Очистить STM текущего чата | Все |
| `/reset` | Сбросить LTM текущего пользователя | Все |
| `/resetall` | Сбросить всю память | Только владелец |
| `/reset_diary` | Полная очистка эпизодической памяти | Только владелец |
| `/files` | Список загруженных файлов | Все (если file_upload) |
| `/reset_files` | Удалить все файлы | Все (если file_upload) |
| `/web` | Переключить веб-поиск | Все (если web_search) |
| `/ratelimits` | Статус rate limiter | Только владелец |

---

## Провайдеры LLM

Все провайдеры используют OpenAI-совместимый API. Порядок fallback определяется порядком ключей в `PROVIDER_CONFIGS`.

| Провайдер | Модель по умолчанию | Базовый URL |
|-----------|---------------------|-------------|
| ZAI | glm-5-turbo | https://open.bigmodel.cn/api/paas/v4/ |
| OpenAI | gpt-4o-mini | https://api.openai.com/v1 |
| Anthropic | claude-sonnet-4 | https://api.anthropic.com/v1/ |
| Groq | llama-3.3-70b | https://api.groq.com/openai/v1 |
| DeepSeek | deepseek-chat | https://api.deepseek.com |
| Kimi | moonshot-v1-8k | https://api.moonshot.cn/v1 |
| Google | gemini-2.0-flash | https://generativelanguage.googleapis.com/v1beta/openai/ |
| Mimo | mimo-v2.5-pro | https://token-plan-sgp.xiaomimimo.com/v1 |
| HuggingFace | Qwen/Qwen2.5-7B-Instruct | https://router.huggingface.co/v1 |

---

## Память: детали реализации

### Short-Term Memory (STM)
- Буфер FIFO на `deque` с `maxlen=50-60`
- Персистентность через ChromaDB (`data/{context}/stm/`)
- Потокобезопасность через `threading.RLock`
- Групповые чаты: фильтрация по `chat_id`, идентификация отправителей по `user_name` + `sender_id`

### Long-Term Memory (LTM)
- Векторное хранилище на ChromaDB с `SentenceTransformer` (`paraphrase-multilingual-MiniLM-L12-v2`)
- Асинхронная экстракция фактов через отдельный `ThreadPoolExecutor` (3 workers)
- Категории фактов: 25+ категорий с подкатегориями (Hobby_music, Skills_tech и т.д.)
- **UPDATE-категории** (City, Profession, Age) — заменяются при новом значении
- **APPEND-категории** (Hobby_*, Food, Pets) — умное слияние через LLM
- Периодическая консолидация: каждые 20 сообщений LLM чистит противоречия и дубликаты

### Self-Memory (эпизодическая)
- Хранение в JSON-файлах (`data/{context}/self_memory/`)
- **Эпизоды**: дневниковые записи от первого лица каждые 5 сообщений
- **Наблюдения**: записи по маркерам рефлексии (чувствую, впервые, на самом деле...)
- **Life Summary**: периодическая суммаризация архива эпизодов
- Архитектура: active (10) -> archive (40) -> life_summary

---

## Безопасность и модерация

| Фича | Описание |
|------|----------|
| **Rate Limiter** | Ограничение частоты сообщений, индивидуальные лимиты через env (`RATE_LIMIT_USER_<ID>=<seconds>`) |
| **Moderation** | LLM-проверка контента, автоматическая блокировка при нарушениях |
| **Punish Block** | Система наказаний: блокировка, постыдные факты через маркеры `[PUNISH:BLOCK]`, `[PUNISH:FACT:...]` |
| **DM Gate** | Личные сообщения только для `allowed_dm_users` |
| **Owner Shield** | Владелец полностью защищен от всех блокировок |

---

## Лицензия

MIT

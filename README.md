# Virtual Persona Core

Единый процесс для двух Telegram-ботов с разными персонами и фичами.

## Боты

| Бот | Персона | Фичи |
|-----|---------|-------|
| Коннор | connor.yaml | file_upload, export_server, restore_memory |
| Арродес | arrodes.yaml | rate_limit, moderation, punish_block, web_search |

## Запуск

```bash
# Установить зависимости
pip install -r requirements.txt

# Настроить .env (скопировать из .env.example)
cp .env.example .env

# Запустить оба бота
python -m app.main
```

## Архитектура

```
app/
├── core/           # Общие модули (config, router, memory, persona...)
├── features/       # Опциональные модули (rate_limiter, moderation, web_search...)
├── personas/       # YAML-конфиги персон (с блоком features)
├── bot_instance.py # Класс BotInstance — один бот с фичами
├── telegram_bot.py # Handlers для Telegram
└── main.py         # Запуск двух ботов в отдельных потоках
```

Каждый YAML содержит блок `features:` — BotInstance активирует только нужные модули.

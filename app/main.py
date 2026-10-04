"""
Запуск:
    python -m app.main              # Интерактивное меню
    python -m app.main <персона>    # Только эта персона (нужен <ПЕРСОНА>_BOT_TOKEN в .env)
    python -m app.main all          # Все персоны, для которых задан токен
    python -m app.main api          # FastAPI-сервер (порт 8000)
"""

import asyncio
import os
import sys
import logging
import threading
from pathlib import Path
from typing import Dict, Tuple
from app.core.envfile import load_env_file

# .env/.env.config текущей папки — приоритет над .env ядра (у отдельного
# проекта-аддона, запущенного не из корня ядра, свой конфиг и свои токены).
# Если процесс запущен из корня ядра, это те же файлы, что и ниже —
# повторная загрузка ничего не меняет.
_project_root = Path(__file__).parent.parent
_cwd = Path.cwd()
load_env_file(_cwd / ".env")
load_env_file(_cwd / ".env.config")
load_env_file(_project_root / ".env")
load_env_file(_project_root / ".env.config")

from app.bot_instance import BotInstance
from app.core.persona import PersonaLayer

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
# Маска секретов во ВСЕХ записях лога процесса (пароли из ответов агенту/
# слоту сценария, email/телефоны/карты/токены) — одна точка на все пути
from app.core import log_privacy
log_privacy.install()
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("duckduckgo_search").setLevel(logging.WARNING)
logging.getLogger("ddgs").setLevel(logging.WARNING)
logging.getLogger("primp").setLevel(logging.WARNING)
logging.getLogger("curl_cffi").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3.connection").setLevel(logging.ERROR)
logging.getLogger("urllib3.util.connection").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

# Event loop'ы запущенных ботов — для остановки из главного потока по Ctrl+C
_running_loops: list = []


def _persona_bot_tokens() -> Dict[str, str]:
    # Персоны из всех папок персон (available_personas()),
    # для которых в .env задан <ПЕРСОНА>_BOT_TOKEN.
    layer = PersonaLayer()
    tokens = {}
    for name in sorted(layer.available_personas()):
        token = os.getenv(f"{name.upper()}_BOT_TOKEN")
        if token:
            tokens[name] = token
    return tokens


def _persona_label(persona_name: str) -> str:
    # Подпись пункта меню: «<имя из YAML> (<персона>, Telegram)»
    display_name = PersonaLayer(persona_name).persona_data.get("name", persona_name)
    return f"{display_name} ({persona_name}, Telegram)"


def _build_menu() -> Dict[str, Tuple[str, str]]:
    # Нумерованное меню: персоны с заданным токеном, затем all и api.
    choices = {}
    n = 0
    for persona_name in _persona_bot_tokens():
        n += 1
        choices[str(n)] = (persona_name, _persona_label(persona_name))
    choices[str(n + 1)] = ("all", "Все боты")
    choices[str(n + 2)] = ("api", "API-сервер (FastAPI)")
    return choices


def run_bot(token: str, persona_name: str, context: str = "tg"):
    
    # Создаёт и запускает одного бота в своём потоке.
    # Импорты telegram — ленивые: режим api не требует python-telegram-bot.
    from telegram import Update
    from telegram.ext import Application
    from telegram.request import HTTPXRequest

    from app.telegram_bot import register_handlers

    logger.info(f"Инициализация бота: {persona_name}")

    bot_instance = BotInstance(persona_name=persona_name, context=context)

    # Восстановление памяти
    if bot_instance.features.get("restore_memory", False):
        from app.features.restore_memory import restore_all
        results = restore_all()
        if any(v > 0 for v in results.values()):
            logger.info(f"[{persona_name}] Память восстановлена: {results}")

    # Export server
    if bot_instance.features.get("export_server", False):
        from app.features.export_server import start_export_server
        export_port = int(os.getenv("EXPORT_PORT", "8080"))
        start_export_server(port=export_port)
        logger.info(f"[{persona_name}] Export server на порту {export_port}")

    # Команды меню — Telegram показывает их во всплывающем списке при вводе "/"
    # (автодополнение работает на стороне клиента через set_my_commands)
    commands = [
        ("start", "Начать диалог"),
        ("help", "Справка по командам"),
        ("stats", "Статистика памяти"),
        ("reset", "Сбросить мои факты из памяти"),
        ("forget", "Забыть факт: /forget <что забыть>"),
        ("relations", "Связи участников чата"),
        ("last", "Последние N сообщений чата"),
        ("ratelimits", "Статистика лимитов"),
        ("ltm_privacy", "Приватность памяти: smart | strict"),
        ("ltm_export", "Выгрузить мою память файлом (в личку)"),
    ]
    if bot_instance.todo_manager:
        commands.append(("todo", "Мой список дел"))
        commands.append(("add_todo", "Добавить дело: /add_todo <текст>"))
    if bot_instance.inventory_manager:
        commands.append(("inventory", "Инвентарь бота"))
        commands.append(("add_inventory", "Дать предмет: /add_inventory <название>"))
    if bot_instance.reminder_manager:
        commands.append(("remind", "Напоминание: /remind <когда> <что>"))
        commands.append(("reminders", "Мои напоминания"))
        commands.append(("cancel_reminder", "Отменить: /cancel_reminder <номер>"))
    if bot_instance.learning_manager:
        commands.append(("learn", "Учить тему: /learn <тема>"))
        commands.append(("stop_learning", "Остановить курс: /stop_learning [тема]"))
    if bot_instance.file_db:
        commands.append(("files", "Список загруженных файлов"))
        commands.append(("reset_files", "Сбросить файловую базу"))
    if bot_instance._web_search_enabled:
        commands.append(("web", "Вкл/выкл веб-поиск в этом чате"))

    # Owner-only команды — показываем только владельцу (scope на его чат)
    owner_commands = [
        ("erase", "Удалить последние N сообщений STM"),
        ("context", "Контекст, уходящий в промпт"),
        ("resetall", "Стереть ВСЮ память бота"),
    ]
    if bot_instance.self_memory:
        owner_commands.append(("reset_diary", "Очистить дневник бота"))

    from telegram import BotCommand, BotCommandScopeChat
    bot_commands = [BotCommand(cmd, desc) for cmd, desc in commands]
    bot_owner_commands = [BotCommand(cmd, desc) for cmd, desc in (commands + owner_commands)]

    # Post-init hook: регистрируем команды внутри event loop бота
    async def post_init(app):
        await app.bot.set_my_commands(bot_commands)
        logger.info(f"[{persona_name}] Зарегистрировано {len(bot_commands)} команд")
        # Владельцу — полный список (scope на личный чат с ним)
        owner_id = os.getenv("OWNER_USER_ID") or str(bot_instance.owner or "")
        if owner_id.isdigit():
            try:
                await app.bot.set_my_commands(
                    bot_owner_commands,
                    scope=BotCommandScopeChat(chat_id=int(owner_id)),
                )
            except Exception as e:
                logger.warning(f"[{persona_name}] Не удалось задать owner-команды: {e}")

    # Telegram Application
    # concurrent_updates=True — slash-команды (/reminders, /todo, /stats...)
    # выполняются сразу, не дожидаясь окончания LLM-ответа на предыдущее сообщение.
    # Порядок внутри одного чата защищён per-chat блокировкой в telegram_bot.
    request = HTTPXRequest(connect_timeout=30, read_timeout=60)
    app = (
        Application.builder().token(token).request(request)
        .concurrent_updates(True)
        .post_init(post_init).build()
    )
    register_handlers(app, bot_instance)

    # run_polling() не работает в дочернем потоке (add_signal_handler) —
    # управляем event loop вручную.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _running_loops.append(loop)

    async def _start():
        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )

    loop.run_until_complete(_start())
    logger.info(f"[{persona_name}] Бот запущен и ожидает сообщения...")

    # Запускаем proactive messaging если включено
    if bot_instance.proactive:
        def _start_proactive():
            bot_instance.proactive.start(loop=loop)
        loop.call_soon_threadsafe(_start_proactive)
        logger.info(f"[{persona_name}] Proactive messaging запущен")

    # Запускаем суточный ритм (утро/ночь/погода) если включено
    if bot_instance.rhythm is not None:
        def _start_rhythm():
            bot_instance.rhythm.start(loop=loop)
        loop.call_soon_threadsafe(_start_rhythm)
        logger.info(f"[{persona_name}] Rhythm запущен")

    # Запускаем reminder loop если включено
    rm = bot_instance.reminder_manager
    if rm:
        def _start_reminders():
            rm.start(loop=loop)
        loop.call_soon_threadsafe(_start_reminders)
        logger.info(f"[{persona_name}] Reminder manager запущен")

    # Запускаем learning loop если включено
    lm = bot_instance.learning_manager
    if lm:
        def _start_learning():
            lm.start(loop=loop)
        loop.call_soon_threadsafe(_start_learning)
        logger.info(f"[{persona_name}] Learning manager запущен")

    # Запускаем живую персону (тики состояния + события мира) если включена
    if bot_instance.living is not None:
        def _start_living():
            bot_instance.living.start(loop=loop)
        loop.call_soon_threadsafe(_start_living)
        logger.info(f"[{persona_name}] Living persona запущена")

    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        # Останавливаем proactive
        if bot_instance.proactive:
            bot_instance.proactive.stop()
        # Останавливаем reminders
        if bot_instance.reminder_manager:
            bot_instance.reminder_manager.stop()
        # Останавливаем суточный ритм
        if bot_instance.rhythm is not None:
            bot_instance.rhythm.stop()
        # Останавливаем learning
        if bot_instance.learning_manager:
            bot_instance.learning_manager.stop()
        # Останавливаем живую персону
        if bot_instance.living is not None:
            bot_instance.living.stop()
        async def _stop():
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        loop.run_until_complete(_stop())
        loop.close()
        # Гасим Chrome бота последним (менеджеры в stop() могут ещё
        # дёргать webchat); иначе он переживает выход процесса, и окно
        # с вкладками веб-чатов грузит систему бессрочно
        try:
            from app.features import browser_actions as ba
            ba.shutdown_browser()
        except Exception:
            pass


def run_api():
    # Запускает FastAPI-сервер для веб-фронта и десктоп-приложения.
    import uvicorn
    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("app.api.server:app", host=host, port=port)


def start_target(target: str):
    # Запускает выбранную цель.
    if target == "api":
        run_api()
        return

    bots = _persona_bot_tokens()

    if target == "all":
        if not bots:
            logger.error("Не задан ни один <ПЕРСОНА>_BOT_TOKEN в .env")
            sys.exit(1)

        threads = []

        # Создание потоков — по одному на каждую персону с заданным токеном
        for persona_name, token in bots.items():
            t = threading.Thread(
                target=run_bot,
                args=(token, persona_name),
                kwargs={"context": persona_name},
                name=f"bot-{persona_name}",
                # daemon=True — страховка: если graceful shutdown зависнет,
                # потоки не заблокируют выход процесса
                daemon=True,
            )
            threads.append(t)

        for t in threads:
            t.start()
            logger.info(f"  Поток {t.name} запущен")

        logger.info(f"Запущено {len(threads)} ботов. Ctrl+C для остановки.")

        try:
            for t in threads:
                # ждёт завершения потоков
                t.join()
        except KeyboardInterrupt:
            logger.info("Остановка по Ctrl+C...")
            # Останавливаем event loop'ы ботов — finally в run_bot выполнит
            # корректное завершение (stop менеджеров, app.shutdown, loop.close)
            for bot_loop in _running_loops:
                try:
                    bot_loop.call_soon_threadsafe(bot_loop.stop)
                except RuntimeError:
                    pass
            for t in threads:
                t.join(timeout=30)
            alive = [t.name for t in threads if t.is_alive()]
            if alive:
                logger.warning(f"Потоки не завершились за 30с: {alive} — выходим принудительно")
        return

    # Конкретная персона
    token = bots.get(target)
    if not token:
        logger.error(f"{target.upper()}_BOT_TOKEN не задан в .env")
        sys.exit(1)
    run_bot(token, target, context=target)


def show_menu():
    # Интерактивное меню выбора.
    choices = _build_menu()
    for key, (_, label) in choices.items():
        print(f"  {key}. {label}")
    print()
    names = list(_persona_bot_tokens()) + ["all", "api"]
    print(f"  Или имя напрямую: {' / '.join(names)}")
    print()

    choice = input("  >  ").strip().lower()

    if choice in choices:
        return choices[choice][0]
    if choice in names:
        return choice

    print(f"  Неизвестный выбор: {choice}")
    sys.exit(1)


def _install_windows_browser_logic():
    # Windows: окна Chrome бота — своя логика (app/features/browser_win.py,
    # monkey-patch browser_actions). На macOS/Linux модуль даже не
    # импортируется — поведение прежнее. VPC_WIN_WINDOWS=0 — выключить.
    if sys.platform != "win32":
        return
    try:
        from app.features import browser_win
        browser_win.install()
    except Exception as e:  # окна — удобство, не повод не запустить бота
        logger.warning(f"[WinMain] Windows-логика окон не подключена: {e}")


def main():
    # 1. Аргумент командной строки
    # 2. Env-переменная BOT_TARGET
    # 3. Интерактивное меню (только если есть TTY)
    valid_targets = set(_persona_bot_tokens()) | {"all", "api"}

    if len(sys.argv) > 1:
        arg = sys.argv[1].strip().lower()
        if arg in valid_targets:
            target = arg
        elif arg in _build_menu():
            target = _build_menu()[arg][0]
        else:
            print(f"Неизвестный аргумент: {arg}")
            print(f"Допустимо: {', '.join(sorted(valid_targets))}")
            sys.exit(1)
    elif os.getenv("BOT_TARGET"):
        target = os.getenv("BOT_TARGET").strip().lower()
        if target not in valid_targets:
            print(f"Неизвестный BOT_TARGET: {target}")
            sys.exit(1)
    elif sys.stdin.isatty():
        target = show_menu()
    else:
        print("Не указана цель запуска. Используйте аргумент или BOT_TARGET.")
        print("  python -m app.main all")
        print("  BOT_TARGET=all python -m app.main")
        sys.exit(1)

    logger.info(f"Запуск: {target}")
    _install_windows_browser_logic()
    from app.core.router import NoProvidersError
    try:
        start_target(target)
    except NoProvidersError as e:
        # Telegram-боту без источника ответов работать нечем — понятная причина
        # вместо трейсбека (API-режим без провайдеров запускается: настройка — в вебе)
        logger.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()

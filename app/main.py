"""
Запуск:
    python -m app.main              # Интерактивное меню
    python -m app.main connor       # Только Коннор
    python -m app.main arrodes      # Только Арродес
    python -m app.main all          # Оба бота
    python -m app.main gradio       # Gradio-интерфейс
"""

import os
import sys
import logging
import threading
from pathlib import Path
from dotenv import load_dotenv

# Загружаем .env
_project_root = Path(__file__).parent.parent
load_dotenv(_project_root / ".env.config")
load_dotenv(_project_root / ".env")

from telegram import Update
from telegram.ext import Application
from telegram.request import HTTPXRequest

from app.bot_instance import BotInstance
from app.telegram_bot import register_handlers

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_CHOICES = {
    "1": ("connor", "Коннор (RK800, Telegram)"),
    "2": ("arrodes", "Арродес (Зеркало, Telegram)"),
    "3": ("all", "Все боты"),
    "4": ("gradio", "Gradio (веб-интерфейс)"),
}


def run_bot(token: str, persona_name: str, context: str = "tg"):
    """
    Создаёт и запускает одного бота в своём потоке.
    """
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

    # Команды меню
    commands = [
        ("start", "Начать диалог"),
        ("help", "Справка по командам"),
        ("stats", "Статистика памяти"),
        ("reset", "Сбросить память пользователя"),
        ("resetall", "Сбросить память всех пользователей"),
    ]
    if bot_instance.file_db:
        commands.append(("files", "Список загруженных файлов"))
        commands.append(("reset_files", "Сбросить файловую базу"))
    if bot_instance._rate_limit_enabled:
        commands.append(("ratelimits", "Статистика лимитов (владелец)"))

    from telegram import BotCommand
    bot_commands = [BotCommand(cmd, desc) for cmd, desc in commands]

    # Post-init hook: регистрируем команды внутри event loop бота
    async def post_init(app):
        await app.bot.set_my_commands(bot_commands)
        logger.info(f"[{persona_name}] Зарегистрировано {len(bot_commands)} команд")

    # Telegram Application
    request = HTTPXRequest(connect_timeout=30, read_timeout=60)
    app = Application.builder().token(token).request(request).post_init(post_init).build()
    register_handlers(app, bot_instance)

    logger.info(f"[{persona_name}] Бот запущен и ожидает сообщения...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


def run_gradio():
    """Запускает Gradio-интерфейс."""
    from app.gradio_app import bot, demo
    print("\n  [Gradio] Запуск веб-интерфейса на http://localhost:7860\n")
    demo.launch()


def start_target(target: str):
    """Запускает выбранную цель."""
    connor_token = os.getenv("CONNOR_BOT_TOKEN")
    arrodes_token = os.getenv("ARRODES_BOT_TOKEN")

    if target == "gradio":
        run_gradio()
        return

    if target == "connor":
        if not connor_token:
            logger.error("CONNOR_BOT_TOKEN не задан в .env")
            sys.exit(1)
        run_bot(connor_token, "connor", context="connor")
        return

    if target == "arrodes":
        if not arrodes_token:
            logger.error("ARRODES_BOT_TOKEN не задан в .env")
            sys.exit(1)
        run_bot(arrodes_token, "arrodes", context="arrodes")
        return

    if target == "all":
        if not connor_token and not arrodes_token:
            logger.error("Не заданы токены CONNOR_BOT_TOKEN и/или ARRODES_BOT_TOKEN в .env")
            sys.exit(1)

        threads = []

        if connor_token:
            t = threading.Thread(
                target=run_bot,
                args=(connor_token, "connor"),
                kwargs={"context": "connor"},
                name="bot-connor",
                daemon=False,
            )
            threads.append(t)

        if arrodes_token:
            t = threading.Thread(
                target=run_bot,
                args=(arrodes_token, "arrodes"),
                kwargs={"context": "arrodes"},
                name="bot-arrodes",
                daemon=False,
            )
            threads.append(t)

        for t in threads:
            t.start()
            logger.info(f"  Поток {t.name} запущен")

        logger.info(f"Запущено {len(threads)} ботов. Ctrl+C для остановки.")

        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            logger.info("Остановка по Ctrl+C...")
            sys.exit(0)


def show_menu():
    """Интерактивное меню выбора."""
    for key, (_, label) in BOT_CHOICES.items():
        print(f"  {key}. {label}")
    print()
    print("  Или имя напрямую: connor / arrodes / all / gradio")
    print()

    choice = input("  >  ").strip().lower()

    if choice in BOT_CHOICES:
        return BOT_CHOICES[choice][0]
    if choice in ("connor", "arrodes", "all", "gradio"):
        return choice

    print(f"  Неизвестный выбор: {choice}")
    sys.exit(1)


def main():
    # Аргумент командной строки или меню
    if len(sys.argv) > 1:
        arg = sys.argv[1].strip().lower()
        if arg in ("connor", "arrodes", "all", "gradio"):
            target = arg
        elif arg in BOT_CHOICES:
            target = BOT_CHOICES[arg][0]
        else:
            print(f"Неизвестный аргумент: {arg}")
            print("Допустимо: connor, arrodes, all, gradio")
            sys.exit(1)
    else:
        target = show_menu()

    logger.info(f"Запуск: {target}")
    start_target(target)


if __name__ == "__main__":
    main()

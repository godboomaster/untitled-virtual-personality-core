"""
Ядро уходит вместе с тем, кто его запустил. Трей Арродеса и панель запускают
ядро своим ребёнком и при выходе гасят его. Но запускавший мог уйти и сам —
упал, убит, перезапущен: ядро оставалось сиротой и держало микрофон
(голосовая активация), хаб интеграций и Chrome бота, а новый трей его не
видел и не гасил (2026-10-28: сирота с 22:18 слушал микрофон до утра).

Запускавший ставит VPC_EXIT_WITH_PARENT=1 — тогда раз в секунду проверяем,
жив ли родитель; ушёл — on_gone(). Из терминала флага нет: закрытый
терминал или nohup ядро не гасят.
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

ENV = "VPC_EXIT_WITH_PARENT"
CHECK_SEC = 1.0


def enabled() -> bool:
    return (os.getenv(ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def _parent_alive():
    """«Родитель жив?» — по PID и времени запуска (psutil: PID мог достаться
    другому процессу; так и на Windows, где сирот не усыновляют). Без psutil —
    unix: сироту усыновляет launchd/init, PID родителя меняется"""
    ppid = os.getppid()
    if ppid <= 1:
        # Родитель ушёл ещё до проверки — флаг ставят только трей и панель
        return lambda: False
    try:
        import psutil
        return psutil.Process(ppid).is_running
    except Exception:
        return lambda: os.getppid() == ppid


def install(on_gone) -> bool:
    """Следить за родителем, если запускавший просил (VPC_EXIT_WITH_PARENT).
    on_gone зовётся один раз, из фонового потока"""
    if not enabled():
        return False
    alive = _parent_alive()

    def watch():
        while alive():
            time.sleep(CHECK_SEC)
        logger.warning("[Parent] Запустивший ядро процесс ушёл — останавливаюсь")
        on_gone()

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()
    return True

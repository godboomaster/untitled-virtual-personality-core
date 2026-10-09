"""Путь к директории данных ядра — одной на всё: персоны пользователя, память
(ChromaDB), состояние персон, журналы.

Десятки мест в коде используют относительный путь "data/..." — он зависит
от текущей рабочей папки процесса. data_dir() даёт единую точку: по
умолчанию "data", а VPC_DATA_DIR в .env задаёт свою папку (отдельному
проекту-аддону, тестовому стенду). DATA_DIR — прежнее имя той же настройки.

Раньше переменные значили разное: базы памяти (ChromaDB) лежали по DATA_DIR
(по умолчанию ./data), остальное — по VPC_DATA_DIR. Стенд, задавший только
одну из них, делил вторую половину данных с настоящей папкой — так тестовое
ядро однажды открыло настоящие базы памяти параллельно с рабочим.
legacy_data_elsewhere() находит данные, оставшиеся на старом месте.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DBS = ("stm", "ltm", "files")


def data_dir() -> Path:
    # Пустое значение («VPC_DATA_DIR=») — как не заданное, а не текущая папка
    return Path(os.getenv("VPC_DATA_DIR") or os.getenv("DATA_DIR") or "data")


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _has_memory(ctx_dir: Path) -> bool:
    return any((ctx_dir / db / "chroma.sqlite3").exists() for db in _DBS)


def legacy_data_elsewhere() -> list[str]:
    """Данные, которые по старым правилам лежали не в data_dir() и теперь не
    находятся → строки для пользователя; пусто — всё на месте.

    Память — по старому DATA_DIR (по умолчанию data), персоны — по старому
    VPC_DATA_DIR (по умолчанию data). Найденным считается только то, чего
    в новой папке нет: память контекста, у которого там своей базы нет;
    персоны, если в новой папке их папки нет."""
    new = data_dir()
    found = []
    old_db = Path(os.getenv("DATA_DIR") or "data")
    if not _same(old_db, new) and old_db.is_dir():
        lost = sorted(p.name for p in old_db.iterdir()
                      if p.is_dir() and _has_memory(p) and not _has_memory(new / p.name))
        if lost:
            found.append(f"память ({', '.join(lost)}) — в {old_db}")
    old_vpc = Path(os.getenv("VPC_DATA_DIR") or "data")
    if (not _same(old_vpc, new) and any((old_vpc / "personas").glob("*.yaml"))
            and not (new / "personas").is_dir()):
        found.append(f"ваши персоны — в {old_vpc / 'personas'}")
    return found


def warn_legacy_data() -> None:
    """При запуске: данные на старом месте — предупреждение, что перенести."""
    try:
        found = legacy_data_elsewhere()
    except OSError as e:
        logger.debug(f"[Data] Проверка старых папок данных: {e}")
        return
    if found:
        logger.warning(
            f"[Data] Папка данных теперь одна на всё — {data_dir()} (VPC_DATA_DIR, "
            f"иначе DATA_DIR). На старом месте осталось: {'; '.join(found)}. Ядро их там "
            "больше не ищет: остановите его и перенесите эти папки в папку данных или "
            "укажите VPC_DATA_DIR на папку, где они лежат.")

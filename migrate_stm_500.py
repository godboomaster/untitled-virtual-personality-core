#!/usr/bin/env python3
"""
Миграция STM: загрузить последние 500 сообщений в ChromaDB.
Запускать ПОСЛЕ остановки бота.

Вся логика — общая с migrate_stm.py (чтение и валидация импорта → бэкап
текущей коллекции → замена с откатом при ошибке).

Usage:
    cd <папка проекта>
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 migrate_stm_500.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from migrate_stm import DEFAULT_DB_PATH, migrate_stm

IMPORT_FILE = "/tmp/stm_import_500.json"

if __name__ == "__main__":
    import_file = sys.argv[1] if len(sys.argv) > 1 else IMPORT_FILE
    db_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DB_PATH
    try:
        migrate_stm(db_path=db_path, import_file=import_file)
    except ValueError as e:
        print(f"Миграция отменена (база не тронута): {e}")
        sys.exit(1)
    print("\nГотово. Буферы чатов (последние сообщения) загрузятся из базы "
          "при старте бота — ShortTermMemory._load_from_db().")

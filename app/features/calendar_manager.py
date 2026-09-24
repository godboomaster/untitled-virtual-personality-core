"""
Общий календарь всех персон.

В отличие от todo/напоминаний (у каждой персоны свой файл в data/{context}/),
календарь один на всё ядро: data/calendar.json в корне каталога данных —
по образцу env_location.json и persona_drafts/. Каждая запись может быть
привязана к персоне (поле persona) — тогда в UI она помечается её цветом.

Хранимая запись:
    {
        "id": "a1b2c3d4e5f6",
        "title": "сдать отчёт",
        "date": "2026-09-20",          # YYYY-MM-DD, обязательно
        "time": "18:30" | None,        # HH:MM, опционально
        "kind": "todo" | "reminder" | "note" | "event",
        "persona": "<id>" | None,      # id персоны-владельца записи
        "user_name": "web",            # кто попросил
        "note": "",
        "done": False,
        "created_at": 1757790000.0,
    }
"""

import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.core.atomic_io import atomic_write_json, load_json_safe
from app.core.config import Config

logger = logging.getLogger(__name__)

KINDS = ("todo", "reminder", "note", "event")


def _valid_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def _valid_time(value: str) -> bool:
    try:
        datetime.strptime(value, "%H:%M")
        return True
    except (ValueError, TypeError):
        return False


class CalendarManager:
    # CRUD над общим файлом календаря. Потокобезопасен.

    def __init__(self, data_dir: Optional[str] = None):
        self._base_dir = Path(data_dir or Config.DATA_DIR)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._file = self._base_dir / "calendar.json"
        self._lock = threading.Lock()
        self._entries: List[dict] = []
        self._load()

    # ── persistence ──

    def _load(self):
        data = load_json_safe(self._file, default=[], label="Calendar")
        self._entries = data if isinstance(data, list) else []

    def _save(self):
        # Атомарная запись (общий helper app.core.atomic_io — tmp-файл + os.replace).
        try:
            atomic_write_json(self._file, self._entries)
        except Exception as e:
            logger.warning(f"[Calendar] Не удалось сохранить: {e}")

    # ── API ──

    def add_entry(self, title: str, date: str, time_: Optional[str] = None,
                  kind: str = "note", persona: Optional[str] = None,
                  user_name: str = "web", note: str = "") -> dict:
        # Создаёт запись. ValueError — при невалидных дате/времени/типе.
        title = (title or "").strip()
        if not title:
            raise ValueError("пустой заголовок")
        if not _valid_date(date):
            raise ValueError(f"невалидная дата: {date!r}")
        if time_ and not _valid_time(time_):
            raise ValueError(f"невалидное время: {time_!r}")
        if kind not in KINDS:
            raise ValueError(f"невалидный тип: {kind!r}")
        entry = {
            "id": uuid.uuid4().hex[:12],
            "title": title,
            "date": date,
            "time": time_ or None,
            "kind": kind,
            "persona": persona or None,
            "user_name": user_name,
            "note": (note or "").strip(),
            "done": False,
            "created_at": time.time(),
        }
        with self._lock:
            self._entries.append(entry)
            self._save()
        logger.info(f"[Calendar] Добавлено: {date} {persona or '-'} '{title}'")
        return entry

    def list_entries(self, start: Optional[str] = None,
                     end: Optional[str] = None) -> List[dict]:
        """Записи в диапазоне дат [start, end] включительно (строки YYYY-MM-DD
        сравниваются лексикографически). Без границ — все записи."""
        with self._lock:
            items = [
                e for e in self._entries
                if (start is None or e["date"] >= start)
                and (end is None or e["date"] <= end)
            ]
        return sorted(items, key=lambda e: (e["date"], e.get("time") or "", e["created_at"]))

    def update_entry(self, entry_id: str, **patch) -> Optional[dict]:
        """Правит запись по id. None — запись не найдена, ValueError — невалидное поле.
        None-значения принимаются только у time/persona (сброс поля).

        Пустой title запрещён, как и в add_entry. Пустая строка time (веб-форма
        шлёт её при сбросе времени) нормализуется в None, чтобы отсутствие
        времени хранилось одним способом, а не двумя вперемешку."""
        if "title" in patch:
            patch["title"] = (patch["title"] or "").strip()
            if not patch["title"]:
                raise ValueError("пустой заголовок")
        if "time" in patch and patch["time"] == "":
            patch["time"] = None
        if "date" in patch and not _valid_date(patch["date"]):
            raise ValueError(f"невалидная дата: {patch['date']!r}")
        if patch.get("time") and not _valid_time(patch["time"]):
            raise ValueError(f"невалидное время: {patch['time']!r}")
        if "kind" in patch and patch["kind"] not in KINDS:
            raise ValueError(f"невалидный тип: {patch['kind']!r}")
        with self._lock:
            for e in self._entries:
                if e["id"] == entry_id:
                    for key in ("title", "date", "time", "kind", "persona", "note", "done"):
                        if key not in patch:
                            continue
                        value = patch[key]
                        if value is None and key not in ("time", "persona"):
                            continue
                        e[key] = value
                    self._save()
                    return e
        return None

    def remove_entry(self, entry_id: str) -> bool:
        # Удаляет запись по id. True — удалена.
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e["id"] != entry_id]
            if len(self._entries) == before:
                return False
            self._save()
        return True


# Общий синглтон: календарь один на всё ядро, не per-context.
_instance: Optional[CalendarManager] = None
_instance_lock = threading.Lock()


def get_calendar() -> CalendarManager:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = CalendarManager()
        return _instance

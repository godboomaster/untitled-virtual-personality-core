"""
Инвентарь бота — хранит предметы персонажа в inventory.json.
Один файл на контекст (персону), не на чат.
"""

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class InventoryItem:
    def __init__(self, name: str, description: str = "", acquired: str = "",
                 source: str = "", tags: Optional[List[str]] = None):
        self.name = name
        self.description = description
        self.acquired = acquired or datetime.now().strftime("%Y-%m-%d")
        self.source = source
        self.tags = tags or []

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "acquired": self.acquired,
            "source": self.source,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InventoryItem":
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            acquired=data.get("acquired", ""),
            source=data.get("source", ""),
            tags=data.get("tags", []),
        )


class InventoryManager:
    """
    Управляет инвентарем персонажа.
    Файл: data/{context}/inventory.json
    """

    def __init__(self, context: str = "default", max_slots: int = 10):
        self.context = context
        self.max_slots = max_slots
        self._file = Path(f"data/{context}/inventory.json")
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._items: List[InventoryItem] = []
        self._load()

    def _load(self):
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._items = [InventoryItem.from_dict(i) for i in data.get("items", [])]
            except Exception as e:
                logger.warning(f"[Inventory] Не удалось загрузить {self._file}: {e}")
                self._items = []

    def _save(self):
        try:
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(
                    {"items": [i.to_dict() for i in self._items]},
                    f, ensure_ascii=False, indent=2
                )
        except Exception as e:
            logger.warning(f"[Inventory] Не удалось сохранить {self._file}: {e}")

    def add_item(self, name: str, description: str = "", source: str = "") -> str:
        """Добавляет предмет в инвентарь. Возвращает результат операции."""
        name = name.strip()
        if not name:
            return "Название предмета не может быть пустым."

        with self._lock:
            # Проверяем дубликат
            for item in self._items:
                if item.name.lower() == name.lower():
                    return f"Предмет '{name}' уже есть в инвентаре."

            if len(self._items) >= self.max_slots:
                return f"Инвентарь полон ({self.max_slots}/{self.max_slots}). Удалите что-нибудь."

            item = InventoryItem(name=name, description=description, source=source)
            self._items.append(item)
            self._save()

        return f"Предмет '{name}' добавлен в инвентарь."

    def remove_item(self, name: str) -> str:
        """Удаляет предмет из инвентаря."""
        name = name.strip().lower()
        with self._lock:
            for i, item in enumerate(self._items):
                if item.name.lower() == name:
                    removed = self._items.pop(i)
                    self._save()
                    return f"Предмет '{removed.name}' удален из инвентаря."
        return f"Предмет '{name}' не найден."

    def get_items(self) -> List[InventoryItem]:
        """Возвращает список предметов."""
        with self._lock:
            return list(self._items)

    def get_context_block(self) -> Optional[str]:
        """Возвращает форматированный блок для system prompt."""
        if not self._items:
            return None
        lines = ["Твой инвентарь:"]
        for item in self._items:
            desc = f" — {item.description}" if item.description else ""
            lines.append(f"  • {item.name}{desc}")
        return "\n".join(lines)

    def get_list_text(self) -> str:
        """Возвращает текст для команды /inventory."""
        if not self._items:
            return "Инвентарь пуст."
        lines = ["Инвентарь:"]
        for i, item in enumerate(self._items, 1):
            desc = f" — {item.description}" if item.description else ""
            src = f" (источник: {item.source})" if item.source else ""
            lines.append(f"{i}. {item.name}{desc}{src}")
        return "\n".join(lines)

    def has_item(self, name: str) -> bool:
        name = name.strip().lower()
        with self._lock:
            return any(item.name.lower() == name for item in self._items)


# Эвристика для определения запросов на добавление/удаление предмета
_INVENTORY_ADD_TRIGGERS = [
    "возьми", "получи", "надень", "экипируй", "подбери", "забери",
    "вот тебе", "держи", "дарю", "отдаю", "передаю",
    "добавь в инвентарь", "положи в карман",
]

_INVENTORY_REMOVE_TRIGGERS = [
    "выбрось", "убери", "сними", "разэкипируй", "выкинь",
    "удали из инвентаря", "убери из кармана",
]

_INVENTORY_ADD_PATTERNS = [
    re.compile(r"(?:возьми|получи|надень|подбери|забери)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:вот тебе|держи|дарю|отдаю)\s*[,:]?\s*(.+)", re.IGNORECASE),
    re.compile(r"(?:добавь в инвентарь|положи в карман)\s*[,:]?\s*(.+)", re.IGNORECASE),
]

_INVENTORY_REMOVE_PATTERNS = [
    re.compile(r"(?:выбрось|убери|сними|выкинь)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:удали из инвентаря|убери из кармана)\s*[,:]?\s*(.+)", re.IGNORECASE),
]


def is_inventory_add_request(text: str) -> bool:
    lower = text.lower()
    return any(t in lower for t in _INVENTORY_ADD_TRIGGERS)


def is_inventory_remove_request(text: str) -> bool:
    lower = text.lower()
    return any(t in lower for t in _INVENTORY_REMOVE_TRIGGERS)


def extract_inventory_item(text: str) -> Optional[str]:
    """Извлекает название предмета из запроса на добавление."""
    for pattern in _INVENTORY_ADD_PATTERNS:
        match = pattern.search(text)
        if match:
            item = match.group(1).strip()
            item = re.sub(r"[.!?\s]*пожалуйста[.!?\s]*$", "", item, flags=re.IGNORECASE).strip()
            if item:
                return item
    # Fallback
    if is_inventory_add_request(text):
        cleaned = re.sub(r"^(?:(?:коннор|жабка|connor)[,\s]+)+", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"[\s,]+пожалуйста\s*$", "", cleaned, flags=re.IGNORECASE).strip()
        # Убираем триггерное слово из начала
        for trigger in _INVENTORY_ADD_TRIGGERS:
            if cleaned.lower().startswith(trigger):
                cleaned = cleaned[len(trigger):].strip().lstrip(",.:; ")
                break
        if cleaned:
            return cleaned
    return None


def extract_inventory_remove(text: str) -> Optional[str]:
    """Извлекает название предмета из запроса на удаление."""
    for pattern in _INVENTORY_REMOVE_PATTERNS:
        match = pattern.search(text)
        if match:
            item = match.group(1).strip()
            if item:
                return item
    # Fallback
    if is_inventory_remove_request(text):
        cleaned = re.sub(r"^(?:(?:коннор|жабка|connor)[,\s]+)+", "", text, flags=re.IGNORECASE)
        for trigger in _INVENTORY_REMOVE_TRIGGERS:
            if cleaned.lower().startswith(trigger):
                cleaned = cleaned[len(trigger):].strip().lstrip(",.:; ")
                break
        if cleaned:
            return cleaned
    return None

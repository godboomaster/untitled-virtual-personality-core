"""
Инвентарь бота — хранит предметы персонажа в inventory.json.
Один файл на контекст (персону), не на чат.
"""

import logging
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

from app.core import timeutil
from app.core.atomic_io import atomic_write_json, load_json_safe
from app.core.paths import data_dir

logger = logging.getLogger(__name__)


class InventoryItem:
    def __init__(self, name: str, description: str = "", acquired: str = "",
                 source: str = "", tags: Optional[List[str]] = None,
                 expires: Optional[str] = None):
        self.name = name
        self.description = description
        self.acquired = acquired or timeutil.today().strftime("%Y-%m-%d")
        self.source = source
        self.tags = tags or []
        self.expires = expires  # ISO date или None

    def is_expired(self) -> bool:
        if not self.expires:
            return False
        try:
            return timeutil.today().strftime("%Y-%m-%d") > self.expires
        except Exception:
            return False

    def to_dict(self) -> dict:
        result = {
            "name": self.name,
            "description": self.description,
            "acquired": self.acquired,
            "source": self.source,
            "tags": self.tags,
        }
        if self.expires:
            result["expires"] = self.expires
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "InventoryItem":
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            acquired=data.get("acquired", ""),
            source=data.get("source", ""),
            tags=data.get("tags", []),
            expires=data.get("expires"),
        )


class InventoryManager:
    """
    Управляет инвентарем персонажа.
    Файл: data/{context}/inventory.json
    """

    def __init__(self, context: str = "default", max_slots: int = 10):
        self.context = context
        self.max_slots = max_slots
        self._file = data_dir() / context / "inventory.json"
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._items: List[InventoryItem] = []
        self._load()

    def _load(self):
        data = load_json_safe(self._file, default={}, label="Inventory")
        items = data.get("items", []) if isinstance(data, dict) else []
        self._items = [InventoryItem.from_dict(i) for i in items]

    def _save(self):
        try:
            atomic_write_json(self._file, {"items": [i.to_dict() for i in self._items]})
        except Exception as e:
            logger.warning(f"[Inventory] Не удалось сохранить {self._file}: {e}")

    def add_item(self, name: str, description: str = "", source: str = "", expires: Optional[str] = None) -> str:
        # Добавляет предмет в инвентарь. Возвращает результат операции.
        name = name.strip()
        if not name:
            return "Item name cannot be empty."

        with self._lock:
            for item in self._items:
                if item.name.lower() == name.lower():
                    return f"Item '{name}' is already in the inventory."

            if len(self._items) >= self.max_slots:
                return f"Inventory is full ({self.max_slots}/{self.max_slots}). Remove something first."

            item = InventoryItem(name=name, description=description, source=source, expires=expires)
            self._items.append(item)
            self._save()

        return f"Item '{name}' added to the inventory."

    def remove_item(self, name: str) -> str:
        # Удаляет предмет из инвентаря.
        name = name.strip().lower()
        with self._lock:
            for i, item in enumerate(self._items):
                if item.name.lower() == name:
                    removed = self._items.pop(i)
                    self._save()
                    return f"Item '{removed.name}' removed from the inventory."
        return f"Item '{name}' not found."

    def get_items(self) -> List[InventoryItem]:
        # Возвращает список предметов.
        with self._lock:
            return list(self._items)

    def use_item(self, name: str) -> str:
        # Использует предмет из инвентаря (удаляет его).
        name = name.strip().lower()
        with self._lock:
            for i, item in enumerate(self._items):
                if item.name.lower() == name:
                    used = self._items.pop(i)
                    self._save()
                    return f"Item '{used.name}' used and removed from the inventory."
        return f"Item '{name}' not found in the inventory."

    def remove_expired_items(self) -> List[str]:
        # Удаляет просроченные предметы. Возвращает список удаленных.
        removed = []
        with self._lock:
            remaining = []
            for item in self._items:
                if item.is_expired():
                    removed.append(item.name)
                else:
                    remaining.append(item)
            if removed:
                self._items = remaining
                self._save()
        return removed

    def get_expired_items(self) -> List[InventoryItem]:
        # Возвращает список просроченных предметов без удаления.
        with self._lock:
            return [item for item in self._items if item.is_expired()]

    def get_context_block(self) -> str:
        """Возвращает форматированный блок для system prompt.

        Пустой инвентарь — тоже блок («пуст»): вместе с ним в промпт идёт
        инструкция маркеров, иначе модель не знает [INVENTORY_ADD] и
        «держи ключ» в пустой инвентарь не попадает никогда.

        Снимок self._items берётся под локом: иначе сборка системного
        промпта (частый читатель) и add_item/remove_item из другого потока
        (HTTP-обработчик /inventory) пересекаются на живом списке →
        RuntimeError "list changed size during iteration"."""
        with self._lock:
            items = list(self._items)
        if not items:
            return "Your inventory is empty — you have no items yet."
        lines = ["Your inventory:"]
        for item in items:
            desc = f" — {item.description}" if item.description else ""
            exp = " [expired]" if item.is_expired() else ""
            lines.append(f"  • {item.name}{desc}{exp}")
        return "\n".join(lines)

    def get_list_text(self) -> str:
        """Возвращает текст для команды /inventory (снимок списка под локом —
        см. get_context_block про гонку с add_item/remove_item)."""
        with self._lock:
            items = list(self._items)
        if not items:
            return "Инвентарь пуст."
        lines = ["Инвентарь:"]
        for i, item in enumerate(items, 1):
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


# Word-boundary regex для триггеров: «получи» не должно ловить «получил/получила»,
# «надень» — «наденьте» и т.п. \b в Python 3 Unicode-aware, корректно для кириллицы.
_INVENTORY_ADD_TRIGGER_RE = re.compile(
    r"\b(?:возьми|получи|надень|экипируй|подбери|забери|вот\s+тебе|держи|дарю|отдаю|передаю|"
    r"добавь\s+в\s+инвентарь|положи\s+в\s+карман)\b",
    re.IGNORECASE,
)

_INVENTORY_REMOVE_TRIGGER_RE = re.compile(
    r"\b(?:выбрось|убери|сними|разэкипируй|выкинь|удали\s+из\s+инвентаря|убери\s+из\s+кармана)\b",
    re.IGNORECASE,
)


def is_inventory_add_request(text: str) -> bool:
    return bool(_INVENTORY_ADD_TRIGGER_RE.search(text))


def is_inventory_remove_request(text: str) -> bool:
    return bool(_INVENTORY_REMOVE_TRIGGER_RE.search(text))


def extract_inventory_item(text: str) -> Optional[str]:
    # Извлекает название предмета из запроса на добавление.
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
    # Извлекает название предмета из запроса на удаление.
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


# Явная просьба про предмет — только она даёт переспрос «Добавить «X» в
# инвентарь?» / «Убрать «X» из инвентаря?», когда локальной модели нет.
# Глаголы-триггеры широки: «держи меня в курсе», «возьми паузу», «подбери
# мне фильм», «передаю привет», «убери его». Кроме прямого «в инвентарь»/
# «из кармана» нужен короткий предмет без местоимения, предлога или слова
# идиомы в начале, и реплика — не вопрос
_INVENTORY_EXPLICIT_RE = re.compile(
    r"\b(?:в|из)\s+(?:инвентар\w*|карман\w*)", re.IGNORECASE)
_ITEM_STOP_HEAD = frozenset({
    "меня", "мне", "мной", "себя", "себе", "собой", "тебя", "тебе", "нас",
    "нам", "вас", "вам", "его", "её", "ее", "их", "им", "это", "этот", "эту",
    "эти", "то", "так", "вот", "свои", "свой", "своё", "свою", "своего",
    "в", "во", "на", "за", "с", "со", "под", "по", "от", "из", "к", "о",
    "и", "а", "но", "что", "как",
    "паузу", "трубку", "слово", "слова", "ответ", "привет", "должное", "курс",
})
_ITEM_MAX_WORDS = 4


def explicit_inventory_item(text: str, item: Optional[str]) -> Optional[str]:
    """Предмет для переспроса или None — реплика не похожа на просьбу про
    предмет. Хвост после первого знака («ключ, он пригодится») отрезается."""
    if not text or not item:
        return None
    head = re.split(r"[,.;:!?…]", item, maxsplit=1)[0].strip()
    if not head:
        return None
    if _INVENTORY_EXPLICIT_RE.search(text):
        return head
    if "?" in text:
        return None
    words = re.findall(r"[\w-]+", head.lower())
    if not words or len(words) > _ITEM_MAX_WORDS or words[0] in _ITEM_STOP_HEAD:
        return None
    return head

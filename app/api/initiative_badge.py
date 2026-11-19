"""Appearance of the desktop portrait's pending-initiative marker."""

import json

from app.api.portrait_api import portrait_dir
from app.api.room_api import RoomError, validate_data_url
from app.core.atomic_io import atomic_write_json

MIN_SIZE, MAX_SIZE = 24, 72
DEFAULT_SIZE = 34
DEFAULT = {"kind": "symbol", "symbol": "!", "image": None, "size": DEFAULT_SIZE}
MAX_IMAGE_BYTES = 256 * 1024


def _valid_size(size) -> bool:
    return type(size) is int and MIN_SIZE <= size <= MAX_SIZE


def get(persona: str) -> dict:
    path = portrait_dir(persona) / "initiative_badge.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT.copy()
    if not isinstance(value, dict):
        return DEFAULT.copy()
    size = value.get("size")
    if not _valid_size(size):
        size = DEFAULT_SIZE
    if value.get("kind") == "image" and isinstance(value.get("image"), str):
        return {"kind": "image", "symbol": "!", "image": value["image"], "size": size}
    symbol = value.get("symbol")
    if isinstance(symbol, str) and 1 <= len(symbol) <= 4 and symbol.strip() == symbol:
        return {"kind": "symbol", "symbol": symbol, "image": None, "size": size}
    return DEFAULT.copy()


def put(persona: str, kind: str, symbol: str | None, image: str | None, size: int | None = None) -> dict:
    if size is None:
        size = get(persona)["size"]
    elif not _valid_size(size):
        raise RoomError(400, f"Размер значка: целое число от {MIN_SIZE} до {MAX_SIZE} пикселей")
    if kind == "image":
        value = {"kind": "image", "symbol": "!",
                 "image": validate_data_url(image, MAX_IMAGE_BYTES, "Значок уведомления")}
    elif kind == "symbol":
        if not isinstance(symbol, str) or not 1 <= len(symbol) <= 4 or symbol.strip() != symbol \
                or any(ord(c) < 32 for c in symbol):
            raise RoomError(400, "Символ уведомления: от 1 до 4 знаков без пробелов")
        value = {"kind": "symbol", "symbol": symbol, "image": None}
    else:
        raise RoomError(400, "Вид уведомления: symbol или image")
    value["size"] = size
    path = portrait_dir(persona) / "initiative_badge.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, value)
    return value

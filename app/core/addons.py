"""
Аддоны персон: подключаемые модули, которые дополняют ход диалога.

Аддон собирает свой блок системного промпта (build_context), чинит сырой
ответ модели до garbage-гарда (repair) и чистит финальный ответ
(postprocess). Ядро вызывает их в фиксированных местах process_message и
ничего не знает об их содержимом.

Аддоны находятся через entry points группы virtual_persona.addons, персона
включает их в YAML:

    features:
      addons: [arrodes_book]

Установленный пакет может объявить и папку со своими персонами (группа
virtual_persona.personas) — PersonaLayer ищет YAML в app/personas и в них.
"""

import logging
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

ADDONS_GROUP = "virtual_persona.addons"
PERSONAS_GROUP = "virtual_persona.personas"

CORE_PERSONAS_DIR = Path(__file__).resolve().parent.parent / "personas"

# Старый ключ features.book_search: true — то же, что addons: [arrodes_book].
# Нужен arrodes_master, чей YAML не меняется
_LEGACY_FLAGS = {
    "book_search": "arrodes_book",
}

# Имена, о которых уже предупредили: одно предупреждение на процесс
_warned: set = set()


@dataclass
class TurnInfo:
    user_input: str
    history: list          # stm_messages хода
    user_id: str
    chat_id: Optional[str]
    persona_name: str
    context: str


@dataclass
class AddonResult:
    prompt_block: Optional[str]              # текст для системного промпта
    state: dict = field(default_factory=dict)  # данные для repair/postprocess


@runtime_checkable
class Addon(Protocol):
    name: str

    def setup(self, bot) -> None: ...

    # Блок промпта на ход. Не вызывается в light-режиме
    def build_context(self, turn: TurnInfo) -> Optional[AddonResult]: ...

    # Сырой ответ, до garbage-гарда. result — None, если build_context
    # на этом ходу не вызывался или ничего не вернул
    def repair(self, answer: str, result: Optional[AddonResult]) -> str: ...

    # Финальная чистка ответа
    def postprocess(self, answer: str, result: Optional[AddonResult]) -> str: ...


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message)


def _entry_points(group: str) -> list:
    try:
        return list(entry_points(group=group))
    except Exception as e:
        logger.warning(f"[Addons] Entry points {group} не прочитаны: {e}")
        return []


def find_addon_class(name: str):
    # Класс аддона по имени из entry points. None — такой не установлен
    for ep in _entry_points(ADDONS_GROUP):
        if ep.name == name:
            return ep.load()
    return None


def enabled_addon_names(features: dict) -> list[str]:
    # Имена аддонов персоны: features.addons + старые флаги-алиасы
    names: list[str] = []
    raw = (features or {}).get("addons") or []
    if isinstance(raw, str):
        raw = [raw]
    for item in raw:
        if isinstance(item, str) and item.strip():
            if item.strip() not in names:
                names.append(item.strip())
        else:
            _warn_once(f"bad:{item!r}", f"[Addons] Непонятная запись в features.addons: {item!r}")
    for flag, addon_name in _LEGACY_FLAGS.items():
        if (features or {}).get(flag, False) and addon_name not in names:
            names.append(addon_name)
    return names


def load_addons(features: dict, persona_name: str = "") -> list:
    # Экземпляры аддонов персоны (ещё без setup). Ненайденный или упавший
    # при импорте аддон пропускается с предупреждением
    addons = []
    for name in enabled_addon_names(features):
        try:
            cls = find_addon_class(name)
        except Exception as e:
            _warn_once(f"import:{name}",
                       f"[Addons] Аддон «{name}» не загружен ({persona_name}): {e}")
            continue
        if cls is None:
            _warn_once(f"missing:{name}",
                       f"[Addons] Аддон «{name}» не установлен — пропущен ({persona_name})")
            continue
        try:
            addon = cls()
        except Exception as e:
            logger.warning(f"[Addons] Аддон «{name}» не создан ({persona_name}): {e}")
            continue
        if not getattr(addon, "name", None):
            addon.name = name
        addons.append(addon)
    return addons


def persona_dirs() -> list[Path]:
    # Папки с YAML персон: app/personas, затем объявленные установленными пакетами
    dirs = [CORE_PERSONAS_DIR]
    for ep in _entry_points(PERSONAS_GROUP):
        try:
            obj = ep.load()
            if callable(obj):
                obj = obj()
            path = Path(obj).resolve()
        except Exception as e:
            _warn_once(f"personas:{ep.name}",
                       f"[Addons] Папка персон «{ep.name}» не прочитана: {e}")
            continue
        if path.is_dir() and path not in dirs:
            dirs.append(path)
    return dirs


def find_persona_file(name: str) -> Optional[Path]:
    # YAML персоны в первой папке, где он есть
    for d in persona_dirs():
        path = d / f"{name}.yaml"
        if path.exists():
            return path
    return None

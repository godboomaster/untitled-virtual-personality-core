"""Реестр BotInstance и утилиты персон для API-слоя.

Боты создаются лениво (инициализация тяжёлая: загрузка эмбеддинг-модели,
ChromaDB) и кешируются по имени персоны. Память каждой персоны изолирована
контекстом ``api_{persona}``, чтобы
факты разных персон не смешивались для одного веб-пользователя.
"""

import asyncio
import threading
from pathlib import Path

import yaml

from app.bot_instance import BotInstance

PERSONAS_DIR = Path(__file__).parent.parent / "personas"


def _load_persona_yaml(name: str) -> dict | None:
    path = PERSONAS_DIR / f"{name}.yaml"
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def list_personas() -> list[str]:
    """Имена персон = YAML-файлы с непустым system_prompt.

    В app/personas/ лежат и служебные файлы (глоссарий, таймлайн и т.п.) —
    они отфильтровываются по отсутствию system_prompt.
    """
    result = []
    for path in sorted(PERSONAS_DIR.glob("*.yaml")):
        data = _load_persona_yaml(path.stem)
        if data and data.get("system_prompt"):
            result.append(path.stem)
    return result


def get_persona_info(name: str) -> dict | None:
    """Публичная информация о персоне из её YAML. None — если не персона."""
    data = _load_persona_yaml(name)
    if not data or not data.get("system_prompt"):
        return None
    return {
        # id = имя файла, а не поле id из YAML: все API-эндпоинты адресуются
        # файлом, а поле id в файле может дублироваться (verso_ru_group.yaml → id: verso)
        "id": name,
        "name": data.get("name", name),
        "description": data.get("description", ""),
        "features": data.get("features") or {},
        "settings": data.get("settings") or {},
    }


class BotRegistry:
    """Ленивый потокобезопасный реестр BotInstance по имени персоны."""

    def __init__(self):
        self._bots: dict[str, BotInstance] = {}
        self._lock = threading.Lock()

    def get(self, persona: str) -> BotInstance | None:
        """Возвращает (создавая при первом обращении) BotInstance персоны.
        None — если такой персоны нет. Вызывать из рабочего потока
        (создание инстанса блокирующее)."""
        with self._lock:
            bot = self._bots.get(persona)
            if bot is not None:
                return bot
        if get_persona_info(persona) is None:
            return None
        with self._lock:
            if persona not in self._bots:
                from app.api.inbox import start_bot_features
                bot = BotInstance(persona_name=persona, context=f"api_{persona}")
                # Веб/API — однопользовательский режим: собеседник всегда владелец
                bot.web_single_user = True
                bot.persona.web_single_user = True  # special_users из YAML матчатся на него
                if not bot.owner:
                    bot.owner = "web_user"
                start_bot_features(persona, bot)
                self._bots[persona] = bot
            return self._bots[persona]

    def evict(self, persona: str) -> None:
        """Убрать бота из кеша и остановить его фоновые циклы (удаление персоны)."""
        with self._lock:
            bot = self._bots.pop(persona, None)
        if bot is None:
            return
        # rhythm раньше не останавливался — цикл переживёт выгрузку персоны
        for mgr in (bot.proactive, bot.reminder_manager, bot.learning_manager,
                    bot.living, getattr(bot, "rhythm", None)):
            try:
                if mgr is not None:
                    mgr.stop()
            except Exception:
                pass  # остановка менеджера не должна ронять удаление


# ── Per-chat сериализация ─────────────────────────────────────────────
# BotInstance.process_message потокобезопасен между чатами, но порядок
# сообщений внутри одного чата не гарантирует (в Telegram это делают
# _chat_locks в telegram_bot.py). Здесь — свой аналог для asyncio.

_chat_locks: dict[str, asyncio.Lock] = {}
_chat_locks_guard = threading.Lock()


def chat_lock(key: str) -> asyncio.Lock:
    with _chat_locks_guard:
        lock = _chat_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _chat_locks[key] = lock
        return lock


registry = BotRegistry()

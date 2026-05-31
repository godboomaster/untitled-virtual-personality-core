"""
Эпизодическая память бота — личный опыт и саморефлексия.
Отдельный слой поверх LTM: бот накапливает собственный опыт.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

from app.core.router import ModelRouter
from app.core.config import get_db_paths

logger = logging.getLogger(__name__)

# ─── Константы ───────────────────────────────────────────

EPISODE_EVERY = 25            # сообщений между эпизодами
MAX_ACTIVE_EPISODES = 8    # сколько эпизодов в промпте
MAX_ARCHIVE_EPISODES = 20   # архив перед суммаризацией
MAX_NOTES = 15              # заметок в промпте
MIN_NOTE_INTERVAL = 5      # минимум сообщений между заметками
MIN_MSG_LEN_FOR_NOTE = 30   # минимальная длина сообщения для заметки

# Маркеры для триггера саморефлексии
_REFLECTION_MARKERS = [
    # Эмоции и состояния
    "чувствую", "ощущаю", "странно", "удивительно", "неожиданно",
    "впервые", "всегда", "никогда", "надоело", "нравится", "раздражает",
    # Самораскрытие
    "на самом деле", "честно говоря", "признаться", "вообще-то",
    "мне кажется", "я думаю что ты", "ты всегда", "ты никогда",
    # Паттерны
    "опять", "снова", "как обычно", "в который раз",
    # Рефлексия о боте
    "ты изменился", "ты стал", "ты как будто", "ты похож",
]


def _has_reflection_marker(text: str) -> bool:
    text_lower = text.lower()
    return any(marker in text_lower for marker in _REFLECTION_MARKERS)


# ─── Промпты ─────────────────────────────────────────────

_EPISODE_PROMPT_TEMPLATE = """Ты — {persona_name}. Ниже — фрагмент разговора.
Напиши подробную личную заметку (2-3 предложений) от первого лица.

Опиши подробно: о чём шла речь, какие темы затрагивались, что тебя зацепило, какие эмоции вызвало.
Упомяни детали разговора — конкретные факты, аргументы, мнения. Это твой личный дневник, пиши свободно и развёрнуто.

[ДИАЛОГ]
{dialog}
[КОНЕЦ]

Заметка:"""

_NOTE_PROMPT_TEMPLATE = """Ты — {persona_name}. Прочитай сообщение пользователя и контекст.
Реши: есть ли здесь что-то стоящее записать себе как наблюдение?

Записывай ТОЛЬКО если:
- Увидел устойчивый паттерн поведения пользователя
- Понял что-то новое о себе или своих реакциях
- Произошло что-то нетипичное или значимое
- Пользователь раскрыл что-то важное о себе косвенно

НЕ записывай:
- Обычный small talk
- Факты которые уже есть в памяти
- Единичные случаи без паттерна

Ответь строго в формате:
SKIP — если записывать нечего
NOTE: {{текст наблюдения}} — если есть что записать

Пользователь ({user_id}): {message}
Контекст: {context}

Решение:"""

_SUMMARY_PROMPT_TEMPLATE = """Ты — {persona_name}. Ниже — твои старые дневниковые записи.
Напиши один краткий абзац (5-7 предложений) — общий смысл всей истории.
Что ты пережил, чему научился, как изменился. Это твоя "жизненная история".

[СТАРЫЕ ЗАПИСИ]
{episodes}
[КОНЕЦ]

Жизненная история:"""


class BotSelfMemory:
    """
    Личная память бота — эпизоды и наблюдения.
    Хранение: JSON-файлы (data/{context}/self_memory/)
    """

    def __init__(self, context: str, persona_name: str, router: ModelRouter):
        self.context = context
        self.persona_name = persona_name
        self.router = router

        # Пути к файлам
        db = get_db_paths(context)
        self._base_dir = Path(db["stm"]).parent / "self_memory"
        self._base_dir.mkdir(parents=True, exist_ok=True)

        self._episodes_file = self._base_dir / "episodes.json"
        self._notes_file = self._base_dir / "notes.json"
        self._state_file = self._base_dir / "state.json"

        # Загружаем или создаём
        self._episodes = self._load_json(self._episodes_file, {
            "active": [],      # [{text, timestamp, msg_count}]
            "archive": [],     # [{text, timestamp, msg_count}]
            "life_summary": "" # строка
        })
        self._notes = self._load_json(self._notes_file, {
            "notes": []        # [{text, timestamp, user_id, trigger_message}]
        })

        # Счётчики — загружаем из state.json чтобы сохранить при перезапуске
        state = self._load_json(self._state_file, {
            "msg_since_episode": 0,
            "msg_since_last_note": 0,
        })
        self._msg_since_episode = state["msg_since_episode"]
        self._msg_since_last_note = state["msg_since_last_note"]

        logger.info(f"[{persona_name}] BotSelfMemory инициализирован | "
                   f"эпизодов: {len(self._episodes['active'])} активных, "
                   f"{len(self._episodes['archive'])} архивных, "
                   f"заметок: {len(self._notes['notes'])}")

    # ─── Загрузка / сохранение ───────────────────────────

    def _load_json(self, path: Path, default: dict) -> dict:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"[SelfMemory] Ошибка загрузки {path}: {e}")
        return default

    def _save_json(self, path: Path, data: dict):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[SelfMemory] Ошибка сохранения {path}: {e}")

    # ─── Публичный API ───────────────────────────────────

    def _save_state(self):
        """Сохраняет текущие счётчики в state.json."""
        self._save_json(self._state_file, {
            "msg_since_episode": self._msg_since_episode,
            "msg_since_last_note": self._msg_since_last_note,
        })

    def tick(self, messages: List[Dict], user_id: str, last_message: str):
        """
        Вызывается после каждого сообщения пользователя.
        Решает, нужно ли писать эпизод или заметку.
        """
        self._msg_since_episode += 1
        self._msg_since_last_note += 1

        # Эпизод каждые N сообщений
        if self._msg_since_episode >= EPISODE_EVERY:
            self._msg_since_episode = 0
            self._write_episode(messages)

        # Заметка — по маркерам и интервалу
        if (self._msg_since_last_note >= MIN_NOTE_INTERVAL
                and len(last_message) >= MIN_MSG_LEN_FOR_NOTE
                and _has_reflection_marker(last_message)):
            self._msg_since_last_note = 0
            self._maybe_write_note(last_message, user_id, messages[-5:])

        self._save_state()

    def get_context_block(self) -> str:
        # Возвращает блок для вставки в system prompt.
        parts = [f"[ЛИЧНАЯ ПАМЯТЬ {self.persona_name}]"]

        # Жизненная история
        summary = self._episodes.get("life_summary", "")
        if summary:
            parts.append(f"История: {summary}")
            parts.append("")

        # Активные эпизоды
        active = self._episodes.get("active", [])
        if active:
            parts.append("Недавние эпизоды:")
            for ep in active:
                parts.append(f"- {ep['text']}")
            parts.append("")

        # Заметки
        notes = self._notes.get("notes", [])
        if notes:
            recent_notes = notes[-MAX_NOTES:]
            parts.append("Наблюдения:")
            for note in recent_notes:
                parts.append(f"- {note['text']}")
            parts.append("")

        parts.append("[КОНЕЦ ЛИЧНОЙ ПАМЯТИ]")

        return "\n".join(parts)

    def clear_all(self):
        # Полная очистка: активные, архив, life_summary, заметки, счётчики.
        self._episodes = {"active": [], "archive": [], "life_summary": ""}
        self._notes = {"notes": []}
        self._msg_since_episode = 0
        self._msg_since_last_note = 0
        self._save_json(self._episodes_file, self._episodes)
        self._save_json(self._notes_file, self._notes)
        self._save_state()
        logger.info(f"[{self.persona_name}] BotSelfMemory полностью очищена")

    # ─── Приватные методы ────────────────────────────────

    def _write_episode(self, messages: List[Dict]):
        # Создание эпизода из последних сообщений (с прошлого эпизода).
        try:
            # Берём только сообщения с прошлого эпизода
            recent = messages[-(EPISODE_EVERY * 2):] if len(messages) > EPISODE_EVERY * 2 else messages

            # Форматируем диалог
            dialog_lines = []
            for msg in recent:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "user":
                    name = msg.get("user_name", "Пользователь")
                    dialog_lines.append(f"{name}: {content}")
                else:
                    dialog_lines.append(f"{self.persona_name}: {content}")
            dialog_text = "\n".join(dialog_lines)

            prompt = _EPISODE_PROMPT_TEMPLATE.format(
                persona_name=self.persona_name,
                dialog=dialog_text
            )

            response = self.router.get_response(
                messages=[
                    {"role": "system", "content": "Ты пишешь дневник от первого лица. Сухо, точно, без выдумок."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=800,
                timeout=30.0,
            )

            if not response or len(response.strip()) < 5:
                logger.info(f"[SelfMemory] Эпизод пустой, пропускаю")
                return

            episode = {
                "text": response.strip(),
                "timestamp": datetime.now().isoformat(),
                "msg_count": len(messages)
            }

            # Добавляем в активные
            self._episodes["active"].append(episode)

            # Архивация если переполнено
            if len(self._episodes["active"]) > MAX_ACTIVE_EPISODES:
                moved = self._episodes["active"].pop(0)
                self._episodes["archive"].append(moved)
                logger.info(f"[SelfMemory] Эпизод архивирован")

            # Суммаризация архива если переполнен
            if len(self._episodes["archive"]) >= MAX_ARCHIVE_EPISODES:
                self._summarize_archive()

            self._save_json(self._episodes_file, self._episodes)
            logger.info(f"[SelfMemory] Эпизод записан ({len(self._episodes['active'])} активных)")

        except Exception as e:
            logger.error(f"[SelfMemory] Ошибка записи эпизода: {e}")

    def _maybe_write_note(self, message: str, user_id: str, context_messages: List[Dict]):
        # Попытка записать наблюдение.
        try:
            # Формируем контекст
            context_lines = []
            for msg in context_messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                name = msg.get("user_name", "Пользователь") if role == "user" else self.persona_name
                context_lines.append(f"{name}: {content[:200]}")
            context_text = "\n".join(context_lines)

            prompt = _NOTE_PROMPT_TEMPLATE.format(
                persona_name=self.persona_name,
                user_id=user_id,
                message=message[:500],
                context=context_text
            )

            response = self.router.get_response(
                messages=[
                    {"role": "system", "content": "Ты решаешь, стоит ли записать наблюдение. Отвечай только SKIP или NOTE: ..."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=100,
                timeout=15.0,
            )

            if not response:
                return

            response_clean = response.strip()

            if response_clean.upper().startswith("SKIP"):
                logger.info(f"[SelfMemory] Заметка пропущена (SKIP)")
                return

            if response_clean.upper().startswith("NOTE:"):
                note_text = response_clean[5:].strip()
                if len(note_text) < 10:
                    return

                note = {
                    "text": note_text,
                    "timestamp": datetime.now().isoformat(),
                    "user_id": str(user_id),
                    "trigger_message": message[:200]
                }

                self._notes["notes"].append(note)

                # Лимит заметок — удаляем старые
                if len(self._notes["notes"]) > MAX_NOTES * 3:
                    self._notes["notes"] = self._notes["notes"][-MAX_NOTES * 2:]

                self._save_json(self._notes_file, self._notes)
                logger.info(f"[SelfMemory] Заметка записана ({len(self._notes['notes'])} всего)")

        except Exception as e:
            logger.error(f"[SelfMemory] Ошибка записи заметки: {e}")

    def _summarize_archive(self):
        # Суммаризирует архивные эпизоды в life_summary.
        try:
            archive = self._episodes["archive"]
            if not archive:
                return

            episodes_text = "\n\n".join(
                f"[{i+1}] {ep['text']}" for i, ep in enumerate(archive)
            )

            prompt = _SUMMARY_PROMPT_TEMPLATE.format(
                persona_name=self.persona_name,
                episodes=episodes_text
            )

            response = self.router.get_response(
                messages=[
                    {"role": "system", "content": "Ты суммаризируешь свою жизненную историю. Кратко, по существу."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=200,
                timeout=30.0,
            )

            if response and len(response.strip()) > 20:
                self._episodes["life_summary"] = response.strip()
                self._episodes["archive"] = []  # очищаем архив
                logger.info(f"[SelfMemory] Жизненная история обновлена")

        except Exception as e:
            logger.error(f"[SelfMemory] Ошибка суммаризации: {e}")

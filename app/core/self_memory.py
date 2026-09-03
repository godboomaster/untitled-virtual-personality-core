"""
Эпизодическая память бота — личный опыт и саморефлексия.
Отдельный слой поверх LTM: бот накапливает собственный опыт.
"""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

from app.core.router import ModelRouter
from app.core.config import get_db_paths
from app.core.local_router import get_local_router
from app.core.language import (
    detect_language, detect_dialogue_language, language_name, language_name_ru,
)

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

_EPISODE_PROMPT_TEMPLATE = """You are {persona_name}. Below is a fragment of a conversation.
Write a detailed personal entry (2-3 sentences) in first person.

Describe in detail: what was discussed, which topics came up, what hooked you, what emotions it stirred.
Mention details from the conversation — concrete facts, arguments, opinions. This is your personal diary, write freely and fully.

IMPORTANT: write the entry in the language the user uses in the conversation — if the user writes in English, write the entry in English.

[DIALOGUE]
{dialog}
[END]

Entry:"""

_NOTE_PROMPT_TEMPLATE = """You are {persona_name}. Read the user's message and the context.
Decide: is there anything here worth writing down for yourself as an observation?

Record ONLY if:
- You noticed a persistent pattern in the user's behavior
- You learned something new about yourself or your reactions
- Something unusual or significant happened
- The user indirectly revealed something important about themselves

DO NOT record:
- Ordinary small talk
- Facts that are already in memory
- One-off cases without a pattern

Answer strictly in this format:
SKIP — if there is nothing to record
NOTE: {{observation text}} — if there is something to record
Write the observation in the language the user uses in the conversation (if the user writes in English, write in English).

User ({user_id}): {message}
Context: {context}

Decision:"""

_SUMMARY_PROMPT_TEMPLATE = """You are {persona_name}. Below are your old diary entries.
Write one short paragraph (5-7 sentences) — the overall meaning of the whole story.
What you lived through, what you learned, how you changed. This is your "life story".
Write it in the language the entries themselves are written in.

[OLD ENTRIES]
{episodes}
[END]

Life story:"""

# Примитивный режим (intellect tier primitive, §3.1 плана уровней интеллекта):
# эпизод — не нарратив, а вспышка сенсорного/инстинктивного впечатления.
_EPISODE_PROMPT_PRIMITIVE = """Ты — {persona_name}, примитивное существо (не человек по типу мышления).
Ниже — фрагмент общения. Запиши ОДНО короткое впечатление-вспышку (1 предложение, до 10 слов):
сенсорное или инстинктивное, БЕЗ причин, БЕЗ выводов, БЕЗ наблюдений о себе или собеседнике.
Тон примеров: «Тепло. Дремал.», «Громкий звук. Спрятался.», «Предмет блестит. Хочу.»
Пиши на языке реплик собеседника (русские реплики — пиши по-русски).

[ДИАЛОГ]
{dialog}
[END]

Впечатление:"""

# life_summary для primitive (§3.1): не «история жизни», а список повторяющихся
# паттернов — «любит блестящие предметы», «пугается громких звуков»
_SUMMARY_PROMPT_PRIMITIVE = """Ты — {persona_name}, примитивное существо. Ниже — твои старые впечатления-вспышки.
Выпиши 3-5 ПОВТОРЯЮЩИХСЯ паттернов существа (что любит, чего боится, что делает снова и снова).
Каждый паттерн — короткая строка без рефлексии и объяснений. Пиши на языке записей.

[СТАРЫЕ ВПЕЧАТЛЕНИЯ]
{episodes}
[END]

Верни JSON: {{"patterns": ["паттерн 1", "паттерн 2"]}}"""


class BotSelfMemory:
    """
    Личная память бота — эпизоды и наблюдения.
    Хранение: JSON-файлы (data/{context}/self_memory/)
    """

    def __init__(self, context: str, persona_name: str, router: ModelRouter,
                 mode: str = "full"):
        """
        mode (intellect tiers, §3.1):
          full     — обычный режим (эпизоды + заметки + life_summary)
          primitive — вспышки-впечатления, без заметок (наблюдения о
                      пользователе — слишком рефлексивно), life_summary —
                      список паттернов
          none     — сюда не доходим: модуль не создаётся в BotInstance
        """
        self.mode = mode if mode in ("full", "primitive") else "full"
        self.context = context
        self.persona_name = persona_name
        self.router = router

        self.local_router = get_local_router()
        # tick() вызывается конкурентно (потоки to_thread, proactive-цикл, API)
        self._lock = threading.RLock()
        # Фоновая запись эпизода/заметки идёт максимум одна — очередь не копим
        self._bg_write_inflight = False

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

    def _side_response(self, messages, **kw):
        """Побочный вызов LLM (дневник, саммари): fallback-цепочка основного
        роутера МИНУС основной провайдер; веб-чат — отдельный side-чат."""
        return self.router.get_response(
            messages, exclude_provider=self.router.active_provider,
            webchat_channel="side", **kw)
    # ─── Загрузка / сохранение ───────────────────────────

    def _load_json(self, path: Path, default: dict) -> dict:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"[SelfMemory] Ошибка загрузки {path}: {e}")
                # Битый файл не затираем дефолтом — сохраняем копию для ручного восстановления
                try:
                    backup = path.with_suffix(path.suffix + ".corrupted")
                    os.replace(path, backup)
                    logger.error(f"[SelfMemory] Битый файл сохранён как {backup}")
                except Exception:
                    pass
        return default

    def _save_json(self, path: Path, data: dict):
        # Атомарная запись: tmp + rename, иначе конкурентный/оборванный dump портит JSON
        with self._lock:
            try:
                tmp = path.with_suffix(path.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
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
        Решает, нужно ли писать эпизод или заметку. Сама запись — в фоне:
        это LLM-вызовы side-цепочки (десятки секунд), а tick идёт по
        request-path уже после генерации ответа — ждать его нельзя.
        """
        with self._lock:
            self._msg_since_episode += 1
            self._msg_since_last_note += 1

            # Эпизод каждые N сообщений
            episode_due = self._msg_since_episode >= EPISODE_EVERY
            if episode_due:
                self._msg_since_episode = 0

            # Заметка — по маркерам и интервалу. Примитивный режим не пишет
            # заметок: наблюдения о паттернах пользователя — рефлексия не
            # того уровня (§3.1)
            note_due = (self.mode == "full"
                        and self._msg_since_last_note >= MIN_NOTE_INTERVAL
                        and len(last_message) >= MIN_MSG_LEN_FOR_NOTE
                        and _has_reflection_marker(last_message))
            if note_due:
                self._msg_since_last_note = 0

            run_bg = (episode_due or note_due) and not self._bg_write_inflight
            if run_bg:
                self._bg_write_inflight = True
            self._save_state()

        if not run_bg:
            return
        snapshot = list(messages)

        def _run():
            try:
                if episode_due:
                    self._write_episode(snapshot)
                if note_due:
                    self._maybe_write_note(last_message, user_id, snapshot[-5:])
            finally:
                with self._lock:
                    self._bg_write_inflight = False

        threading.Thread(target=_run, daemon=True,
                         name=f"selfmem-write-{self.persona_name}").start()

    def get_context_block(self) -> str:
        # Возвращает блок для вставки в system prompt.
        parts = [f"[PERSONAL MEMORY {self.persona_name}]"]

        # Жизненная история
        summary = self._episodes.get("life_summary", "")
        if summary:
            parts.append(f"Story: {summary}")
            parts.append("")

        # Активные эпизоды
        active = self._episodes.get("active", [])
        if active:
            parts.append("Recent episodes:")
            for ep in active:
                parts.append(f"- {ep['text']}")
            parts.append("")

        # Заметки
        notes = self._notes.get("notes", [])
        if notes:
            recent_notes = notes[-MAX_NOTES:]
            parts.append("Observations:")
            for note in recent_notes:
                parts.append(f"- {note['text']}")
            parts.append("")

        parts.append("[END OF PERSONAL MEMORY]")

        return "\n".join(parts)

    def add_external_episode(self, text: str):
        """Эпизод из офлайн-жизни персоны (план «живой» персоны, §6):
        текст сгенерирован основной LLM в стиле персоны — просто кладём
        в дневник с обычной архивацией/лимитами."""
        if not text or len(text.strip()) < 10:
            return
        with self._lock:
            self._episodes["active"].append({
                "text": text.strip(),
                "timestamp": datetime.now().isoformat(),
                "msg_count": -1,  # маркер: эпизод не привязан к счётчику сообщений
            })
            if len(self._episodes["active"]) > MAX_ACTIVE_EPISODES:
                moved = self._episodes["active"].pop(0)
                self._episodes["archive"].append(moved)
            if len(self._episodes["archive"]) >= MAX_ARCHIVE_EPISODES:
                self._summarize_archive()
            self._save_json(self._episodes_file, self._episodes)

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

    # ─── Бэкап/восстановление (корзина очистки диалога) ───

    def export_state(self) -> dict:
        """Снапшот дневника для бэкапа перед очисткой."""
        with self._lock:
            return {
                "episodes": self._episodes,
                "notes": self._notes,
                "msg_since_episode": self._msg_since_episode,
                "msg_since_last_note": self._msg_since_last_note,
            }

    def import_state(self, state: dict):
        """Восстановление дневника из снапшота (полная замена)."""
        self._episodes = state.get("episodes") or {"active": [], "archive": [], "life_summary": ""}
        self._notes = state.get("notes") or {"notes": []}
        self._msg_since_episode = int(state.get("msg_since_episode", 0))
        self._msg_since_last_note = int(state.get("msg_since_last_note", 0))
        self._save_json(self._episodes_file, self._episodes)
        self._save_json(self._notes_file, self._notes)
        self._save_state()
        logger.info(f"[{self.persona_name}] BotSelfMemory восстановлена из бэкапа")

    # ─── Приватные методы ────────────────────────────────

    def _write_episode(self, messages: List[Dict]):
        # Создание эпизода из последних сообщений (с прошлого эпизода).
        # Для primitive-режима — свой шаблон: вспышка-впечатление, не нарратив.
        try:
            # Берём только сообщения с прошлого эпизода
            recent = messages[-(EPISODE_EVERY * 2):] if len(messages) > EPISODE_EVERY * 2 else messages

            # Форматируем диалог
            dialog_lines = []
            for msg in recent:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "user":
                    name = msg.get("user_name", "User")
                    dialog_lines.append(f"{name}: {content}")
                else:
                    dialog_lines.append(f"{self.persona_name}: {content}")
            dialog_text = "\n".join(dialog_lines)

            if self.mode == "primitive":
                prompt = _EPISODE_PROMPT_PRIMITIVE.format(
                    persona_name=self.persona_name,
                    dialog=dialog_text
                )
                system_msg = (
                    "Ты пишешь одно примитивное сенсорное впечатление. "
                    "Только вывод, без пояснений. Пиши на языке реплик собеседника."
                )
                gen_temperature, gen_max_tokens = 0.6, 80
            else:
                prompt = _EPISODE_PROMPT_TEMPLATE.format(
                    persona_name=self.persona_name,
                    dialog=dialog_text
                )
                system_msg = (
                    "You write a first-person diary. Dry, precise, no inventions. "
                    "Write in the language of the user's messages — if the user "
                    "writes in English, write the entry in English."
                )
                gen_temperature, gen_max_tokens = 0.7, 800

            # Язык дневника = язык пользователя: детект по его репликам,
            # явно дописываем в системное сообщение — иначе модель может
            # взять язык промпта-шаблона или персоны
            ep_lang = detect_dialogue_language("", messages)
            if ep_lang:
                if self.mode == "primitive":
                    system_msg += (f" Язык собеседника — {language_name_ru(ep_lang)}. "
                                   f"Пиши только на нём.")
                else:
                    system_msg += (f" The user's language is {language_name(ep_lang)}. "
                                   f"Write the entry ONLY in {language_name(ep_lang)}.")

            response = self._side_response(
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt}
                ],
                temperature=gen_temperature,
                max_tokens=gen_max_tokens,
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

            with self._lock:
                # Добавляем в активные
                self._episodes["active"].append(episode)

                # Архивация если переполнено
                if len(self._episodes["active"]) > MAX_ACTIVE_EPISODES:
                    moved = self._episodes["active"].pop(0)
                    self._episodes["archive"].append(moved)
                    logger.info(f"[SelfMemory] Эпизод архивирован")

                archive_full = len(self._episodes["archive"]) >= MAX_ARCHIVE_EPISODES
                self._save_json(self._episodes_file, self._episodes)

            # Суммаризация архива если переполнен (тоже LLM — вне лока,
            # чтобы tick следующего сообщения не ждал; сохраняет сама)
            if archive_full:
                self._summarize_archive()

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
                name = msg.get("user_name", "User") if role == "user" else self.persona_name
                context_lines.append(f"{name}: {content[:200]}")
            context_text = "\n".join(context_lines)

            prompt = _NOTE_PROMPT_TEMPLATE.format(
                persona_name=self.persona_name,
                user_id=user_id,
                message=message[:500],
                context=context_text
            )

            # Локальная модель — только фильтр SKIP/NOTE: classify() возвращает
            # ровно одну строку из valid_outputs и не может вернуть текст заметки,
            # поэтому сам текст всегда генерирует основной роутер
            # Язык заметки = язык пользователя (детект по его сообщению/репликам)
            note_lang = detect_language(message) or detect_dialogue_language("", context_messages)
            note_lang_line = (
                f" The user's language is {language_name(note_lang)}. "
                f"Write the observation ONLY in {language_name(note_lang)}."
                if note_lang else ""
            )
            if self.local_router.is_available(task="self_memory"):
                local_response = self.local_router.classify(
                    system_prompt=(
                        "You decide whether an observation is worth recording. "
                        "Answer ONLY SKIP or NOTE: ..."
                    ),
                    user_prompt=prompt,
                    valid_outputs=["SKIP", "NOTE"],
                    temperature=0.0,
                    max_tokens=50,
                    task="self_memory",
                )
                if local_response:
                    logger.info(f"[SelfMemory] Локальная классификация: {local_response}")
                    if local_response.upper().startswith("SKIP"):
                        logger.info(f"[SelfMemory] Заметка пропущена (SKIP, локально)")
                        return

            response = self._side_response(
                messages=[
                    {"role": "system", "content": "You decide whether an observation is worth recording. Answer only SKIP or NOTE: ... Write the observation in the language of the user's messages." + note_lang_line},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=100,
                timeout=15.0,
            )
            response_clean = response.strip() if response else ""

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

                with self._lock:
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
        # primitive: не «история жизни», а список повторяющихся паттернов (§3.1).
        try:
            with self._lock:
                archive = list(self._episodes["archive"])
            if not archive:
                return

            episodes_text = "\n\n".join(
                f"[{i+1}] {ep['text']}" for i, ep in enumerate(archive)
            )
            # Язык саммари = язык самих записей (они на языке пользователя)
            sum_lang = detect_language(episodes_text)

            if self.mode == "primitive":
                prompt = _SUMMARY_PROMPT_PRIMITIVE.format(
                    persona_name=self.persona_name,
                    episodes=episodes_text
                )
                if sum_lang:
                    prompt += (f"\nЯзык паттернов — {language_name_ru(sum_lang)}. "
                               f"Пиши только на нём.")
                system_msg = "Ты возвращаешь только валидный JSON со списком паттернов."
                response = self._side_response(
                    messages=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.3,
                    max_tokens=150,
                    timeout=30.0,
                )
                from app.core.persona_context import _extract_json
                data = _extract_json(response or "")
                patterns = (data or {}).get("patterns") or []
                patterns = [str(p).strip()[:80] for p in patterns[:5] if str(p).strip()]
                if patterns:
                    with self._lock:
                        self._episodes["life_summary"] = "Паттерны:\n" + "\n".join(
                            f"- {p}" for p in patterns)
                        self._episodes["archive"] = []
                        self._save_json(self._episodes_file, self._episodes)
                    logger.info(f"[SelfMemory] Паттерны primitive обновлены ({len(patterns)})")
                return

            prompt = _SUMMARY_PROMPT_TEMPLATE.format(
                persona_name=self.persona_name,
                episodes=episodes_text
            )
            if sum_lang:
                prompt += (f"\nThe entries' language is {language_name(sum_lang)}. "
                           f"Write ONLY in {language_name(sum_lang)}.")

            response = self._side_response(
                messages=[
                    {"role": "system", "content": "You summarize your own life story. Brief and to the point. Write in the language of the diary entries."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=200,
                timeout=30.0,
            )

            if response and len(response.strip()) > 20:
                with self._lock:
                    self._episodes["life_summary"] = response.strip()
                    self._episodes["archive"] = []  # очищаем архив
                    self._save_json(self._episodes_file, self._episodes)
                logger.info(f"[SelfMemory] Жизненная история обновлена")

        except Exception as e:
            logger.error(f"[SelfMemory] Ошибка суммаризации: {e}")

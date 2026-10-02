"""
Суммаризация офлайн-жизни персонажа и продвижение сюжетных арок.

Пайплайн дневной суммаризации: локальная LLM сжимает unconsumed offline_log
в 3-5 фактических тезисов (черновик), затем основная LLM превращает тезисы
в episode в стиле персоны — с ПОЛНЫМ system_prompt, как для обычных
эпизодов self_memory.

Приветствие-дневник при возврате после паузы: offline_log за период
пропуска сжимается локальной LLM в тезисы, которые уходят в контекст
основной LLM — она вплетает их в первый ответ пользователю (полный
system_prompt).

Сценарист (раз в 1-2 недели): основная LLM смотрит на активные storylines
и решает, продвигать ли к повороту/развязке. Промпт включает полный
system_prompt — нужна авторская консистентность.
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from app.core import timeutil
from app.core.config import get_db_paths
from app.core.local_router import get_local_router
from app.core.persona_context import _extract_json
from app.core.language import user_language_line

logger = logging.getLogger(__name__)

DAILY_SUMMARY_TRIGGER = 8  # unconsumed-записей достаточно для внеплановой суммаризации

_THESES_PROMPT = """Compress the entries from the character's life over the period into 3-5 factual theses. A draft, not literature: one thesis — one line, only facts ("what happened").

Entries:
{entries}

Return JSON: {{"theses": ["thesis 1", "thesis 2", ...]}}
{language_line}"""

_EPISODE_PROMPT = """Below are theses about what was happening in your life while you were not talking to your interlocutor. Turn them into an entry in your personal diary — 2-3 sentences in the first person, in your character and style. This is your own life: write it as a diary, not a report.

Theses:
{theses}

{language_line}

Diary entry:"""

# Примитивный вариант: одна вспышка-впечатление из событий жизни, а не нарратив
_EPISODE_PROMPT_PRIMITIVE = """You are a primitive creature (not human in your type of thinking). Below is what was happening to you. Write down ONE short impression (1 sentence, up to 10 words): sensory, instinctive, without reasons or conclusions.

What was happening:
{theses}

{language_line}

Impression:"""

_SCREENWRITER_PROMPT = """You are the screenwriter of the character's life. Below are the active storylines. Decide for each one: leave it as is, advance it to a turn, or conclude it. Take the character's personality into account — a storyline must not require violating their rules.

Storylines:
{storylines}

Known NPCs: {npc_list}

Return JSON: {{"updates": [{{"title": "...", "action": "keep|advance|resolve", "note": "what changed, 1 sentence for the summary"}}]}}
Move only 1 storyline at a time towards its resolution — do not force them all at once.
Keep the titles exactly as given.
{language_line}"""


class OfflineSummarizer:
    # Дневные эпизоды, приветствие при возврате, продвижение сюжетных арок.

    def __init__(self, context: str, persona_name: str, router,
                 primitive: bool = False):
        self.context = context
        self.persona_name = persona_name
        self.router = router

        self.primitive = primitive
        self.local = get_local_router(context)
        self._lock = threading.RLock()

        db = get_db_paths(context)
        base = Path(db["stm"]).parent / "living"
        base.mkdir(parents=True, exist_ok=True)
        self._file = base / "summarizer_state.json"
        self._state = self._load()

    def _side_response(self, messages, **kw):
        """Побочный вызов LLM (эпизоды/сценарии): fallback-цепочка минус
        основной провайдер; веб-чат — отдельный side-чат."""
        return self.router.get_response(
            messages, exclude_provider=self.router.active_provider,
            webchat_channel="side", **kw)
    def _load(self) -> dict:
        default = {"last_daily": {}, "last_screenwriter": None}
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                default.update({k: v for k, v in data.items() if k in default})
            except Exception as e:
                logger.warning(f"[Summarizer] Битый файл: {e}")
        return default

    def _save(self):
        with self._lock:
            try:
                tmp = self._file.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._state, f, ensure_ascii=False, indent=2)
                tmp.replace(self._file)
            except Exception as e:
                logger.error(f"[Summarizer] Ошибка сохранения: {e}")

    # ── Дневная суммаризация ──────────────────────────────

    def should_run_daily(self, chat_id: str, unconsumed_count: int) -> bool:
        today = timeutil.today().strftime("%Y-%m-%d")
        with self._lock:
            last = self._state["last_daily"].get(str(chat_id))
        return last != today or unconsumed_count >= DAILY_SUMMARY_TRIGGER * 2

    def daily_summarize(self, chat_id: str, entries: List[dict], persona,
                        state_engine, self_memory=None,
                        user_language: Optional[str] = None) -> Optional[str]:
        """Тезисы локальной LLM → episode основной LLM → self_memory. Список
        записей помечается consumed. Возвращает текст эпизода или None.
        user_language ('ru'/'en') — язык пользователя чата: тезисы и запись
        дневника пишутся на нём, а не на языке внутренних промптов."""
        if not entries:
            return None

        theses = self._compress_entries(entries, user_language)
        if not theses:
            return None

        episode = self._theses_to_episode(theses, persona, user_language)
        today = timeutil.today().strftime("%Y-%m-%d")
        with self._lock:
            self._state["last_daily"][str(chat_id)] = today
            self._save()
        state_engine.mark_consumed([e["id"] for e in entries])

        if episode and self_memory is not None:
            try:
                try:
                    self_memory.add_external_episode(
                        episode, user_language=user_language)
                except TypeError:
                    # старый интерфейс без языка (заглушки)
                    self_memory.add_external_episode(episode)
                logger.info(f"[Summarizer] Офлайн-эпизод записан в дневник ({chat_id})")
            except Exception as e:
                logger.warning(f"[Summarizer] Эпизод не записан в self_memory: {e}")
        return episode

    def _compress_entries(self, entries: List[dict],
                          user_language: Optional[str] = None) -> List[str]:
        lines = []
        for e in entries:
            p = e.get("payload") or {}
            if e.get("type") == "world_event" and p.get("event"):
                lines.append(f"- {p['event']}")
            elif e.get("type") == "external_stimulus" and p.get("content"):
                lines.append(f"- External fact: {p['content'][:200]}")
            elif e.get("type") == "room_signal" and p.get("event"):
                # Комната в вебе: пользователь заглянул / посидел рядом
                lines.append(f"- {str(p['event'])[:200]}")
            elif e.get("type") == "state_change":
                diff = p.get("diff") or {}
                bits = []
                if "pastime" in diff:
                    bits.append(f"pastime: {diff['pastime']}")
                if "location" in diff:
                    bits.append(f"location: {diff['location']}")
                if "mood" in diff:
                    bits.append(f"mood: {diff['mood'].get('tag', '')}")
                if "internal_note" in diff:
                    bits.append(str(diff["internal_note"]))
                if bits:
                    lines.append(f"- " + "; ".join(bits))
        if not lines:
            return []

        # Локальная LLM сжимает; при недоступности — берём последние как есть.
        # Тезисы — на языке пользователя (дневник ведётся на нём)
        if self.local.is_available(task="offline_summary"):
            try:
                response = self.local.get_response(
                    messages=[
                        {"role": "system", "content": "You return only valid JSON."},
                        {"role": "user", "content": _THESES_PROMPT.format(
                            entries="\n".join(lines[:40]),
                            language_line=user_language_line(user_language))},
                    ],
                    temperature=0.2, max_tokens=300,
                    task="offline_summary",
                    # Ответ пользователю ждёт сжатие: веб-чату — не больше
                    # 40 с на сайт (зависший сайт не держит реплику минутами)
                    webchat_timeout=40.0,
                )
                data = _extract_json(response or "")
                if data and data.get("theses"):
                    return [str(t)[:300] for t in data["theses"][:5]]
            except Exception as e:
                logger.debug(f"[Summarizer] Gemma-сжатие не удалось: {e}")
        return [l.lstrip("- ")[:200] for l in lines[-5:]]

    def _theses_to_episode(self, theses: List[str], persona,
                           user_language: Optional[str] = None) -> Optional[str]:
        """Финальный эпизод — основная LLM с ПОЛНЫМ system_prompt.
        Для primitive — вспышка-впечатление вместо дневникового нарратива.
        user_language — язык записи (= язык пользователя чата)."""
        system_prompt = (persona.system_prompt or "").strip()
        if not system_prompt and not self.primitive:
            return None
        # Явный язык записи: системный промпт персоны и шаблон могут тянуть
        # модель на свой язык — директива в user-сообщении надёжнее
        lang_line = user_language_line(user_language)
        try:
            if self.primitive:
                messages = [
                    {"role": "system", "content": (
                        "You write one primitive sensory impression. "
                        "Output only, no explanations.")},
                    {"role": "user", "content": _EPISODE_PROMPT_PRIMITIVE.format(
                        theses="\n".join(f"- {t}" for t in theses),
                        language_line=lang_line)},
                ]
                temperature, max_tokens = 0.6, 80
            else:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": _EPISODE_PROMPT.format(
                        theses="\n".join(f"- {t}" for t in theses),
                        language_line=lang_line)},
                ]
                temperature, max_tokens = 0.7, 400
            response = self._side_response(
                messages, temperature=temperature,
                max_tokens=max_tokens, timeout=30.0)
            text = (response or "").strip()
            return text if len(text) >= 5 else None
        except Exception as e:
            logger.warning(f"[Summarizer] Генерация эпизода не удалась: {e}")
            return None

    # ── Приветствие-дневник при возврате ──────────────────

    def build_return_context(self, chat_id: str, entries: List[dict],
                             absence_hours: float,
                             user_language: Optional[str] = None) -> Optional[str]:
        """Контекст «что было, пока тебя не было» для вплетения в ответ.
        Финальный текст — основная LLM в обычном пайплайне ответа.
        user_language — язык пользователя: тезисы сжимаем на нём, чтобы
        блок не тянул ответ на язык внутренних событий."""
        if not entries or absence_hours < 12:
            # Короткая пауза — дневник не нужен, факты дойдут через state
            return None
        theses = self._compress_entries(entries, user_language)
        if not theses:
            return None
        theses_text = "\n".join(f"- {t}" for t in theses)
        if self.primitive:
            return (
                f"[WHAT HAPPENED WHILE THE USER WAS AWAY ({absence_hours:.0f} hours)]\n"
                f"{theses_text}\n"
                "These are physical things that happened to you. You are a "
                "primitive creature: show AT MOST one of them through an action, "
                "sound or gesture (1-3 simple words) — NEVER describe them in "
                "human words, NEVER list them, NEVER mention any system."
            )
        return (
            f"[WHAT HAPPENED WHILE THE USER WAS AWAY ({absence_hours:.0f} hours)]\n"
            f"{theses_text}\n"
            "This is what happened in YOUR life during the user's absence. "
            "Weave 1-2 of these facts naturally into your reply if appropriate — "
            "as lived experience, NOT as a report. DO NOT list them all. "
            "DO NOT mention logs, entries or any system."
        )

    # ── Сценарист (раз в 1-2 недели) ──────────────────────

    def should_run_screenwriter(self, world_engine) -> bool:
        last = self._state.get("last_screenwriter")
        if last:
            try:
                if (timeutil.now() - datetime.fromisoformat(last)).days < 10:
                    return False
            except ValueError:
                pass
        return bool(world_engine.active_storylines(limit=1))

    def advance_storylines(self, persona, world_engine,
                           user_language: Optional[str] = None) -> int:
        # Основная LLM в роли сценариста. Возвращает число обновлённых линий.
        # user_language — язык пользователя (LivingPersona.global_language):
        # на нём пишутся заметки линий (они уходят в промпты и в комнату)
        storylines = world_engine.active_storylines(limit=5)
        if not storylines:
            return 0
        snapshot = world_engine.get_world_snapshot()
        npc_list = "; ".join(f"{n['name']} ({n['role']})" for n in snapshot["npcs"][:10]) or "(none)"

        system_prompt = (persona.system_prompt or "").strip() or "You are a screenwriter."
        try:
            response = self._side_response(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": _SCREENWRITER_PROMPT.format(
                        storylines=json.dumps(
                            [{"title": s["title"], "status": s["status"],
                              "summary": s.get("summary", "")} for s in storylines],
                            ensure_ascii=False, indent=1),
                        npc_list=npc_list,
                        language_line=user_language_line(user_language))},
                ],
                temperature=0.6,
                max_tokens=500,
                timeout=45.0,
            )
            data = _extract_json(response or "")
        except Exception as e:
            logger.warning(f"[Summarizer] Сценарист не удался: {e}")
            return 0
        if not data or not data.get("updates"):
            return 0

        now_iso = timeutil.now().isoformat(timespec="seconds")
        updated = 0
        with world_engine._lock:
            from app.core.world_engine import _titles_similar
            for u in data["updates"][:3]:
                title = str(u.get("title", "")).strip()
                # Нечёткий матч с линиями мира: сценарист перефразирует
                # заголовки, и exact-матч молча терял бы апдейты
                s = next((sl for sl in world_engine._world["storylines"]
                          if _titles_similar(sl["title"], title)), None)
                if not s:
                    continue
                action = u.get("action")
                if action == "resolve":
                    s["status"] = "resolved"
                    updated += 1
                elif action == "advance" and s["status"] == "started":
                    s["status"] = "ongoing"
                    updated += 1
                if u.get("note"):
                    s["summary"] = str(u["note"])[:400]
                s["last_update_at"] = now_iso
            if updated:
                world_engine._prune_resolved_locked()
                world_engine._save()
        with self._lock:
            self._state["last_screenwriter"] = now_iso
            self._save()
        if updated:
            logger.info(f"[Summarizer] Сценарист продвинул лорий: {updated}")
        return updated

"""
Суммаризация офлайн-жизни (§6) и сюжетные арки (§5-фаза 5).

Пайплайн дневной суммаризации:
  Gemma: сжать unconsumed offline_log в 3-5 фактических тезисов (черновик)
  Основная LLM: превратить тезисы в episode в стиле персоны —
  с ПОЛНЫМ system_prompt, как для обычных эпизодов self_memory.

Приветствие-дневник (§7 «возврат после паузы»): offline_log за период
пропуска → Gemma сжимает → тезисы уходят в контекст основной LLM,
которая вплетает их в первый ответ пользователю (полный system_prompt).

Сценарист (§6, раз в 1-2 недели): основная LLM смотрит на активные
storylines и решает, продвигать ли к повороту/развязке. Промпт включает
полный system_prompt — нужна авторская консистентность.
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from app.core.config import get_db_paths
from app.core.local_router import get_local_router
from app.core.persona_context import _extract_json
from app.core.language import language_name, language_name_ru

logger = logging.getLogger(__name__)

DAILY_SUMMARY_TRIGGER = 8  # unconsumed-записей достаточно для внеплановой суммаризации

_THESES_PROMPT = """Сожми записи из жизни персонажа за период в 3-5 фактических тезисов. Черновик, не литература: один тезис — одна строка, только факты («что произошло»).

Записи:
{entries}

Верни JSON: {{"theses": ["тезис 1", "тезис 2", ...]}}"""

_EPISODE_PROMPT = """Ниже — тезисы о том, что происходило в твоей жизни, пока ты не общался с собеседником. Преврати их в запись в своём личном дневнике — 2-3 предложения от первого лица, в твоём характере и стиле. Это твоя собственная жизнь: пиши как дневник, а не отчёт.

Тезисы:
{theses}

Запись в дневнике:"""

# Примитивный вариант (intellect primitive, §3.1): из событий жизни — одна
# вспышка-впечатление, не нарратив
_EPISODE_PROMPT_PRIMITIVE = """Ты — примитивное существо (не человек по типу мышления). Ниже — что происходило с тобой. Запиши ОДНО короткое впечатление (1 предложение, до 10 слов): сенсорное, инстинктивное, без причин и выводов. Пиши на языке тезисов.

Что происходило:
{theses}

Впечатление:"""

_SCREENWRITER_PROMPT = """Ты — сценарист жизни персонажа. Ниже — активные сюжетные линии. Реши по каждой: оставить как есть, продвинуть к повороту или завершить. Учти характер персонажа — линия не должна требовать нарушений его правил.

Сюжетные линии:
{storylines}

Известные NPC: {npc_list}

Верни JSON: {{"updates": [{{"title": "...", "action": "keep|advance|resolve", "note": "что изменилось, 1 предложение для summary"}}]}}
Двигай к развязке только 1 линию за раз — не форсируй все сразу."""


class OfflineSummarizer:
    """Дневные эпизоды + приветствие при возврате + продвижение арок."""

    def __init__(self, context: str, persona_name: str, router,
                 primitive: bool = False):
        self.context = context
        self.persona_name = persona_name
        self.router = router

        self.primitive = primitive
        self.local = get_local_router()
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

    # ── Дневная суммаризация (§6) ─────────────────────────

    def should_run_daily(self, chat_id: str, unconsumed_count: int) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            last = self._state["last_daily"].get(str(chat_id))
        return last != today or unconsumed_count >= DAILY_SUMMARY_TRIGGER * 2

    def daily_summarize(self, chat_id: str, entries: List[dict], persona,
                        state_engine, self_memory=None,
                        user_language: Optional[str] = None) -> Optional[str]:
        """Gemma-тезисы → episode основной LLM → self_memory. Список записей
        помечается consumed. Возвращает текст эпизода или None.
        user_language ('ru'/'en') — язык пользователя чата: тезисы и запись
        дневника пишутся на нём, а не на языке внутренних промптов."""
        if not entries:
            return None

        theses = self._compress_entries(entries, user_language)
        if not theses:
            return None

        episode = self._theses_to_episode(theses, persona, user_language)
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            self._state["last_daily"][str(chat_id)] = today
            self._save()
        state_engine.mark_consumed([e["id"] for e in entries])

        if episode and self_memory is not None:
            try:
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
                lines.append(f"- Внешний факт: {p['content'][:200]}")
            elif e.get("type") == "state_change":
                diff = p.get("diff") or {}
                bits = []
                if "pastime" in diff:
                    bits.append(f"занятие: {diff['pastime']}")
                if "location" in diff:
                    bits.append(f"место: {diff['location']}")
                if "mood" in diff:
                    bits.append(f"настроение: {diff['mood'].get('tag', '')}")
                if "internal_note" in diff:
                    bits.append(str(diff["internal_note"]))
                if bits:
                    lines.append(f"- " + "; ".join(bits))
        if not lines:
            return []

        # Gemma сжимает; при недоступности — берём последние как есть.
        # Тезисы — на языке пользователя (дневник ведётся на нём)
        if self.local.is_available(task="offline_summary"):
            try:
                lang_prefix = (
                    f"Язык тезисов — {language_name_ru(user_language)}. Пиши только на нём.\n\n"
                    if user_language else ""
                )
                response = self.local.get_response(
                    messages=[
                        {"role": "system", "content": "Ты возвращаешь только валидный JSON."},
                        {"role": "user", "content": lang_prefix + _THESES_PROMPT.format(
                            entries="\n".join(lines[:40]))},
                    ],
                    temperature=0.2, max_tokens=300,
                    task="offline_summary",
                )
                data = _extract_json(response or "")
                if data and data.get("theses"):
                    return [str(t)[:300] for t in data["theses"][:5]]
            except Exception as e:
                logger.debug(f"[Summarizer] Gemma-сжатие не удалось: {e}")
        return [l.lstrip("- ")[:200] for l in lines[-5:]]

    def _theses_to_episode(self, theses: List[str], persona,
                           user_language: Optional[str] = None) -> Optional[str]:
        """Финальный эпизод — основная LLM с ПОЛНЫМ system_prompt (§6).
        Для primitive — вспышка-впечатление вместо дневникового нарратива.
        user_language — язык записи (= язык пользователя чата)."""
        system_prompt = (persona.system_prompt or "").strip()
        if not system_prompt and not self.primitive:
            return None
        # Явный язык записи: системный промпт персоны и шаблон могут тянуть
        # модель на свой язык — директива в user-сообщении надёжнее
        lang_line = (
            f"\n\nЯзык записи — {language_name_ru(user_language)}. Пиши только на нём."
            if user_language else ""
        )
        try:
            if self.primitive:
                messages = [
                    {"role": "system", "content": (
                        "Ты пишешь одно примитивное сенсорное впечатление. "
                        "Только вывод, без пояснений.")},
                    {"role": "user", "content": _EPISODE_PROMPT_PRIMITIVE.format(
                        theses="\n".join(f"- {t}" for t in theses)) + lang_line},
                ]
                temperature, max_tokens = 0.6, 80
            else:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": _EPISODE_PROMPT.format(
                        theses="\n".join(f"- {t}" for t in theses)) + lang_line},
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

    # ── Приветствие-дневник при возврате (§7) ─────────────

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

    # ── Сценарист (§6, раз в 1-2 недели) ──────────────────

    def should_run_screenwriter(self, world_engine) -> bool:
        last = self._state.get("last_screenwriter")
        if last:
            try:
                if (datetime.now() - datetime.fromisoformat(last)).days < 10:
                    return False
            except ValueError:
                pass
        return bool(world_engine.active_storylines(limit=1))

    def advance_storylines(self, persona, world_engine) -> int:
        """Основная LLM в роли сценариста. Возвращает число обновлённых линий."""
        storylines = world_engine.active_storylines(limit=5)
        if not storylines:
            return 0
        snapshot = world_engine.get_world_snapshot()
        npc_list = "; ".join(f"{n['name']} ({n['role']})" for n in snapshot["npcs"][:10]) or "(нет)"

        system_prompt = (persona.system_prompt or "").strip() or "Ты — сценарист."
        try:
            response = self._side_response(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": _SCREENWRITER_PROMPT.format(
                        storylines=json.dumps(
                            [{"title": s["title"], "status": s["status"],
                              "summary": s.get("summary", "")} for s in storylines],
                            ensure_ascii=False, indent=1),
                        npc_list=npc_list)},
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

        now_iso = datetime.now().isoformat(timespec="seconds")
        updated = 0
        with world_engine._lock:
            from app.core.world_engine import _titles_similar
            for u in data["updates"][:3]:
                title = str(u.get("title", "")).strip()
                # Нечёткий матч с линиями мира: сценарист перефразирует
                # заголовки — exact-матч молча терял апдейты
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

"""
Память отношений с пользователем (план «развитие отношений», фаза 2.1).

Хранение data/{context}/living/relationship.json, per chat:
  first_met_at, user_messages, last_message_at — счётчики (бесплатно,
  на каждое сообщение пользователя через LivingPersona.on_user_message)
  shared_topics:  list[str] — общие темы интересов
  shared_moments: list[str] — внутренние шутки, запомнившиеся эпизоды
Стадия близости — вычисляемая (дни знакомства × сообщений), не хранится.

Наполнение: раз в MOMENT_EXTRACT_EVERY пользовательских сообщений локальный
движок (задача relationship) извлекает из последних реплик НОВЫЕ общие
моменты/темы, с дедупом против уже известных.

Подача: компактный блок в контекст ответа (get_context_block) — стадия,
общие темы, последние моменты; дежурное «не перечисляй механически».
Для primitive-персон блок не строится: вербальной истории отношений нет.
"""

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app.core.config import get_db_paths
from app.core.local_router import get_local_router
from app.core.persona_context import _extract_json

logger = logging.getLogger(__name__)

MOMENT_EXTRACT_EVERY = 20   # разбор диалога — раз в столько сообщений пользователя
MAX_MOMENTS = 15            # внутренние шутки/моменты не копятся бесконечно
MAX_TOPICS = 15
MAX_STANCES = 10            # позиции персоны по темам (эволюция мнений)

_MOMENTS_PROMPT = """Проанализируй фрагмент диалога между персонажем ({persona_name}) и пользователем. Выдели НОВЫЕ общие моменты их отношений: внутренние шутки, запомнившиеся совместные эпизоды, общие темы интересов. Только то, чего нет в уже известных списках.

Отдельно отметь, высказал ли персонаж своё мнение по какой-то теме или ПЕРЕСМОТРЕЛ его в ходе обсуждения (спор, убеждение, смена позиции).

Верни СТРОГО JSON:
{{"moments": ["<коротко, до 10 слов>", ...], "topics": ["<тема>", ...],
  "stance_changes": [{{"topic": "<тема>", "position": "<текущая позиция персонажа, коротко>"}}]}}
Новых нет — пустые списки (это нормально).

Уже известные моменты: {known_moments}
Уже известные темы: {known_topics}
Текущие позиции персонажа: {known_stances}

Диалог:
{dialog}"""

_STAGES = [
    "acquaintance — you are still getting to know each other",
    "friendly — you have found your rhythm together",
    "friends — trust and shared history",
    "close bond — deep connection, you may reference your shared past freely",
]


def _stage_index(days: float, msgs: int) -> int:
    """Стадия близости от длительности и интенсивности общения."""
    if days >= 30 and msgs >= 300:
        return 3
    if days >= 10 and msgs >= 100:
        return 2
    if days >= 3 and msgs >= 30:
        return 1
    return 0


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s]+", " ", str(text or "").lower()).strip()


class RelationshipMemory:
    """Потокобезопасное хранилище отношений per chat. Файл — JSON."""

    def __init__(self, context: str, primitive: bool = False):
        self.context = context
        self.primitive = primitive
        self.local = get_local_router()
        self._lock = threading.RLock()

        db = get_db_paths(context)
        base = Path(db["stm"]).parent / "living"
        base.mkdir(parents=True, exist_ok=True)
        self._file = base / "relationship.json"
        self._chats: Dict[str, dict] = self._load()

    def _load(self) -> dict:
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data.get("chats", {})
            except Exception as e:
                logger.warning(f"[Relationship] Битый файл {self._file}: {e}")
        return {}

    def _save(self):
        with self._lock:
            try:
                tmp = self._file.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"chats": self._chats}, f, ensure_ascii=False, indent=2)
                tmp.replace(self._file)
            except Exception as e:
                logger.error(f"[Relationship] Ошибка сохранения: {e}")

    def _rec(self, chat_id: str) -> dict:
        rec = self._chats.get(str(chat_id))
        if rec is None:
            rec = {
                "first_met_at": time.time(),
                "user_messages": 0,
                "last_message_at": None,
                "shared_topics": [],
                "shared_moments": [],
                # Позиции персоны по темам: {topic, position, previous, revisions}
                "stances": [],
            }
            self._chats[str(chat_id)] = rec
        rec.setdefault("stances", [])
        return rec

    # ── Счётчики (на каждое сообщение пользователя) ──────

    def record_message(self, chat_id: str) -> bool:
        """Учесть сообщение пользователя. True — прошло MOMENT_EXTRACT_EVERY
        сообщений (устаревший триггер: извлечение теперь делает общий
        урожай диалога LivingPersona._harvest_dialogue; флаг оставлен для
        совместимости)."""
        with self._lock:
            rec = self._rec(chat_id)
            rec["user_messages"] += 1
            rec["last_message_at"] = time.time()
            due = rec["user_messages"] % MOMENT_EXTRACT_EVERY == 0
            self._save()
            return due

    def known_lists(self, chat_id: str) -> tuple:
        """(moments, topics, stances) строками для промптов-разборов диалога."""
        with self._lock:
            rec = self._rec(chat_id)
            return (
                "; ".join(rec["shared_moments"][-8:]) or "(нет)",
                "; ".join(rec["shared_topics"][-8:]) or "(нет)",
                "; ".join(f"{s['topic']}: {s['position']}"
                          for s in rec["stances"][-6:]) or "(нет)",
            )

    # ── Извлечение общих моментов/тем (локальный движок) ──

    def add_extracted(self, chat_id: str, moments=None, topics=None,
                      stance_changes=None) -> int:
        """Применить извлечённые из диалога моменты/темы/позиции (дедуп,
        лимиты). Вызывается и из extract_moments, и из общего урожая
        диалога (LivingPersona._harvest_dialogue). primitive — мимо."""
        if self.primitive:
            return 0
        added = 0
        with self._lock:
            rec = self._rec(chat_id)
            known_m = {_norm(x) for x in rec["shared_moments"]}
            for moment in (moments or [])[:2]:
                moment = str(moment).strip()[:120]
                nm = _norm(moment)
                if len(moment) < 8 or not nm or nm in known_m:
                    continue
                if any(nm in k or k in nm for k in known_m):
                    continue
                rec["shared_moments"].append(moment)
                known_m.add(nm)
                added += 1
            known_t = {_norm(x) for x in rec["shared_topics"]}
            for topic in (topics or [])[:3]:
                topic = str(topic).strip()[:60]
                nt = _norm(topic)
                if len(topic) < 3 or not nt or nt in known_t:
                    continue
                rec["shared_topics"].append(topic)
                known_t.add(nt)
                added += 1
            # Позиции персоны: высказывания и пересмотры мнений (фаза 3.1)
            for st in (stance_changes or [])[:2]:
                if not isinstance(st, dict):
                    continue
                if self._merge_stance(
                        rec,
                        str(st.get("topic", "")).strip()[:60],
                        str(st.get("position", "")).strip()[:150]):
                    added += 1
            if added:
                rec["shared_moments"] = rec["shared_moments"][-MAX_MOMENTS:]
                rec["shared_topics"] = rec["shared_topics"][-MAX_TOPICS:]
                self._save()
                logger.info(f"[Relationship] Чат {chat_id}: +{added} общих моментов/тем")
        return added

    def extract_moments(self, chat_id: str, messages: List[dict],
                        persona_name: str = "") -> int:
        """Разбор последних реплик → новые общие моменты/темы.
        Возвращает число добавленных записей. Синхронный LLM-вызов —
        звать из фонового потока."""
        if self.primitive or not self.local.is_available(task="relationship"):
            return 0
        lines = []
        for m in (messages or [])[-8:]:
            role = "User" if m.get("role") == "user" else (persona_name or "Assistant")
            content = str(m.get("content", ""))[:200].strip()
            if content:
                lines.append(f"{role}: {content}")
        if len(lines) < 4:
            return 0

        known_moments, known_topics, known_stances = self.known_lists(chat_id)

        try:
            response = self.local.get_response(
                messages=[
                    {"role": "system", "content": "Ты возвращаешь только валидный JSON без пояснений."},
                    {"role": "user", "content": _MOMENTS_PROMPT.format(
                        persona_name=persona_name or "персонаж",
                        known_moments=known_moments,
                        known_topics=known_topics,
                        known_stances=known_stances,
                        dialog="\n".join(lines))},
                ],
                temperature=0.2,
                max_tokens=250,
                task="relationship",
            )
            data = _extract_json(response or "")
        except Exception as e:
            logger.debug(f"[Relationship] Извлечение не удалось: {e}")
            return 0
        if not isinstance(data, dict):
            return 0
        return self.add_extracted(chat_id, data.get("moments"),
                                  data.get("topics"), data.get("stance_changes"))

    @staticmethod
    def _merge_stance(rec: dict, topic: str, position: str) -> bool:
        """Добавить/обновить позицию персоны по теме. Смена позиции хранит
        предыдущую и считает ревизии — «ты раньше думал X, теперь Y».
        True — запись изменилась."""
        if len(topic) < 3 or len(position) < 3:
            return False
        nt = _norm(topic)
        for s in rec["stances"]:
            if _norm(s.get("topic")) == nt:
                if _norm(s.get("position")) == _norm(position):
                    return False
                s["previous"] = s.get("position")
                s["position"] = position
                s["revisions"] = int(s.get("revisions", 0)) + 1
                s["updated_at"] = datetime.now().isoformat(timespec="seconds")
                return True
        rec["stances"].append({
            "topic": topic, "position": position, "previous": None,
            "revisions": 0,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        rec["stances"] = rec["stances"][-MAX_STANCES:]
        return True

    def get_context_block(self, chat_id: str) -> Optional[str]:
        """Компактный блок отношений для промпта основной LLM.
        None — пока нечего сказать (знакомство без общих тем/моментов)
        или primitive-персона (нет вербальной истории отношений)."""
        if self.primitive:
            return None
        with self._lock:
            rec = self._chats.get(str(chat_id))
            if not rec:
                return None
            rec = dict(rec)
        days = max(0, int((time.time() - rec["first_met_at"]) / 86400))
        msgs = int(rec.get("user_messages", 0))
        stage = _stage_index(days, msgs)
        topics = rec["shared_topics"][-6:]
        moments = rec["shared_moments"][-3:]
        stances = rec["stances"][-3:]
        if stage == 0 and not topics and not moments and not stances:
            return None

        lines = [
            "[YOUR RELATIONSHIP WITH THE USER]",
            f"Stage: {_STAGES[stage]} (known each other for {days} days, ~{msgs} user messages)",
        ]
        if topics:
            lines.append("Shared topics: " + ", ".join(topics))
        if moments:
            lines.append("Shared moments:")
            lines.extend(f"  - {m}" for m in moments)
        if stances:
            lines.append("Your evolving opinions:")
            for s in stances:
                line = f"  - {s['topic']}: {s['position']}"
                if s.get("revisions"):
                    line += f" (revised — before: {s.get('previous')})"
                lines.append(line)
        lines.append(
            "STRICT RULE: this is your shared history and your own opinions. "
            "Hold or change them naturally — you may recall a moment when it "
            "fits; never list these mechanically, never mention any system.")
        return "\n".join(lines)

    def get_snapshot(self, chat_id: str) -> Optional[dict]:
        """Снимок для UI (комната/настроение)."""
        with self._lock:
            rec = self._chats.get(str(chat_id))
            if not rec:
                return None
            rec = dict(rec)
        days = max(0, int((time.time() - rec["first_met_at"]) / 86400))
        return {
            "stage": _stage_index(days, rec.get("user_messages", 0)),
            "days_known": days,
            "user_messages": rec.get("user_messages", 0),
            "shared_topics": list(rec["shared_topics"]),
            "shared_moments": list(rec["shared_moments"]),
            "stances": [dict(s) for s in rec["stances"]],
        }

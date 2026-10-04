"""Переспрос про список дел и инвентарь: «Записать «X» в список дел?».

Эвристика нашла явную просьбу, основная модель маркер не поставила, а
локальной модели, которая подтвердила бы намерение, нет — бот не пишет
вслепую, а спрашивает. Здесь отложенный вопрос чата (как «когда
напомнить?» у напоминаний): живёт LIST_OFFER_TTL_SEC, принадлежит тому,
кого спросили. «Да» бот выполняет сам, без LLM; «нет» снимает вопрос с
коротким ответом; другая реплика снимает его молча и идёт обычным путём.
Тексты — ru/en по языку хода."""

import re
import threading
import time
from typing import Dict, Optional

from app.features.cc_texts import is_en
from app.features.computer_control import classify_confirmation
from app.features.reminder_manager import is_pending_decline

# Сколько ждём ответа — как у «когда напомнить?» (PENDING_REMIND_TTL_SEC):
# позже «да» — уже не ответ на этот вопрос
LIST_OFFER_TTL_SEC = 600

# Отказ с хвостом длиннее — уже новая реплика («нет, лучше расскажи про
# погоду»): вопрос снимаем, а реплику отдаём обычному пути
_MAX_DECLINE_WORDS = 4

_T = {
    "ask_todo_add": {"ru": "Записать «{v}» в список дел?",
                     "en": "Add \"{v}\" to the todo list?"},
    "ask_todo_done": {"ru": "Отметить пункт №{v} как выполненный?",
                      "en": "Mark item #{v} as done?"},
    "ask_inventory_add": {"ru": "Добавить «{v}» в инвентарь?",
                          "en": "Add \"{v}\" to the inventory?"},
    "ask_inventory_remove": {"ru": "Убрать «{v}» из инвентаря?",
                             "en": "Remove \"{v}\" from the inventory?"},
    "done_todo_add": {"ru": "Готово — «{v}» в списке дел.",
                      "en": "Done — \"{v}\" is on the todo list."},
    "done_todo_done": {"ru": "Готово — пункт №{v} вычеркнут.",
                       "en": "Done — item #{v} is crossed off."},
    "gone_todo_done": {"ru": "Пункта №{v} в списке уже нет.",
                       "en": "There's no item #{v} on the list anymore."},
    "done_inventory_add": {"ru": "Готово — «{v}» в инвентаре.",
                           "en": "Done — \"{v}\" is in the inventory."},
    "dup_inventory_add": {"ru": "«{v}» уже есть в инвентаре.",
                          "en": "\"{v}\" is already in the inventory."},
    "full_inventory_add": {"ru": "Инвентарь полон — сначала убери что-нибудь.",
                           "en": "The inventory is full — remove something first."},
    "done_inventory_remove": {"ru": "Готово — «{v}» больше нет в инвентаре.",
                              "en": "Done — \"{v}\" is out of the inventory."},
    "gone_inventory_remove": {"ru": "«{v}» в инвентаре уже нет.",
                              "en": "\"{v}\" isn't in the inventory anymore."},
    "declined": {"ru": "Хорошо, не буду.", "en": "Okay, I won't."},
}


def text(key: str, lang: Optional[str] = None, **values) -> str:
    # Фиксированная реплика на языке хода; языка нет — русский шаблон
    row = _T.get(key) or {}
    s = (row.get("en") if is_en(lang) else None) or row.get("ru") or key
    return s.format(**values)


# Глагол самого действия в ответе — тоже согласие: «да, запиши»,
# «добавляй», «add it». Подменяем его на «да» и отдаём реплику общему
# классификатору подтверждений режима управления: отрицание рядом
# («не записывай» → «не да», «don't add») он сам превращает в NO
_ACT_RE = {
    "add": re.compile(
        r"\b(?:запиши(?:те)?|записывай(?:те)?|записать|добавь(?:те)?|"
        r"добавляй(?:те)?|добавить|внеси|сохрани|возьми|бери|забирай|"
        r"add|keep|take|save|write(?:\s+it)?\s+down)\b", re.IGNORECASE),
    "remove": re.compile(
        r"\b(?:вычеркни(?:те)?|вычёркивай|вычеркивай|зачеркни|отметь(?:те)?|"
        r"убери(?:те)?|убирай|удали(?:те)?|удаляй|выбрось|выкинь|выкидывай|"
        r"remove|delete|cross(?:\s+it)?\s+off|mark(?:\s+it)?(?:\s+as)?\s+done|"
        r"throw(?:\s+it)?\s+(?:away|out))\b", re.IGNORECASE),
}


def classify_reply(kind: str, reply: str, names=None) -> Optional[str]:
    """Ответ на вопрос вида kind → "YES" | "NO" | None (не ответ на него).
    names — обращения к персоне («Коннор, да»)."""
    if not reply or not reply.strip():
        return None
    group = "remove" if kind in ("todo_done", "inventory_remove") else "add"
    verdict = classify_confirmation(_ACT_RE[group].sub("да", reply), names)
    if verdict == "YES":
        return "YES"
    if is_pending_decline(reply) or (
            verdict == "NO" and len(reply.split()) <= _MAX_DECLINE_WORDS):
        return "NO"
    return None


class ListOffers:
    """Отложенные вопросы по чатам: один на чат, новый заменяет старый."""

    def __init__(self):
        self._lock = threading.Lock()
        self._offers: Dict[str, dict] = {}

    def begin(self, chat_id, kind: str, value, user_id=None,
              user_name: str = "", question: str = "") -> None:
        with self._lock:
            self._offers[str(chat_id)] = {
                "kind": kind, "value": value,
                "user_id": str(user_id) if user_id else None,
                "user_name": user_name or "", "question": question,
                "asked_at": time.time()}

    def peek(self, chat_id) -> Optional[dict]:
        # Вопрос чата как есть — без срока и владельца
        with self._lock:
            entry = self._offers.get(str(chat_id))
            return dict(entry) if entry else None

    def take(self, chat_id, user_id=None) -> Optional[dict]:
        """Забирает живой вопрос чата для user_id. Истёкший снимается и не
        возвращается; заданный другому участнику (группа) — не трогается:
        чужая реплика его не отпускает."""
        with self._lock:
            entry = self._offers.get(str(chat_id))
            if not entry:
                return None
            if time.time() - float(entry.get("asked_at") or 0) > LIST_OFFER_TTL_SEC:
                self._offers.pop(str(chat_id), None)
                return None
            owner = entry.get("user_id")
            if owner and user_id and str(owner) != str(user_id):
                return None
            return self._offers.pop(str(chat_id))

    def clear(self, chat_id) -> None:
        with self._lock:
            self._offers.pop(str(chat_id), None)

"""Ход «открой файл X» / «пришли файл про Y» (поиск — app/features/file_finder).

Что сделать с найденным, говорит канал (BotInstance.process_message,
file_delivery):
  * "open" — веб-интерфейс на самом компьютере ядра: открыть программой по
    умолчанию; программу или скрипт — только показать в папке (Finder/
    Проводник), запускать по сообщению нельзя;
  * "send" — приложение на телефоне и Telegram: прислать файл в чат
    (бакет файлов хода, его забирает канал — BotInstance.pop_pending_files).

Один явный лидер — действие сразу; несколько похожих — нумерованный список,
ответ номером («да» — первый, «нет» — отмена), как у списка сайтов режима
управления. Ответы — служебные, без модели: поиск и выбор идут без LLM. В
историю диалога (её видят облачные модели в следующих ходах) список ложится
заглушкой — имена посторонних файлов туда не попадают; имя выбранного
файла остаётся (о нём человек и просил). Каждое действие пишется в
audit.jsonl (папка данных персоны, file_access/).
"""

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.features import file_finder as ff

logger = logging.getLogger(__name__)

# Список «какой файл?» ждёт ответа столько же, сколько список сайтов
PENDING_TTL_SEC = 300
# Больше не присылаем: Telegram Bot API принимает документы до 50 МБ, а
# телефон по VPC Link держит файл в памяти WebView целиком
MAX_SEND_BYTES = 50 * 1024 * 1024

_T = {
    "choices": {
        "ru": "Нашлось несколько файлов — какой {verb}?\n{items}\n"
              "Ответь номером («да» — первый) или «нет».",
        "en": "I found several files — which one should I {verb}?\n{items}\n"
              "Reply with a number (\"yes\" means the first) or \"no\"."},
    # Одно совпадение, но не уверенное (слово нашлось только внутри файла)
    "one_choice": {
        "ru": "Нашёлся похожий файл — {verb} его?\n{item}\nОтветь «да» или «нет».",
        "en": "I found a file that might be it — {verb} it?\n{item}\nReply \"yes\" or \"no\"."},
    "one_choice_history": {
        "ru": "Нашёлся похожий файл — вопрос показан в чате.",
        "en": "Found a file that might be it — the question was shown in the chat."},
    # В историю вместо списка: история уходит облачной модели в следующих
    # ходах, а в списке — имена и папки посторонних файлов
    "choices_history": {
        "ru": "Нашлось несколько файлов ({n}) — выбор показан в чате.",
        "en": "Found several files ({n}) — the choices were shown in the chat."},
    "range": {"ru": "Варианта {n} нет — выбери от 1 до {max} или скажи «нет».",
              "en": "There's no option {n} — pick 1 to {max} or say \"no\"."},
    "declined_open": {"ru": "Хорошо, не открываю.", "en": "Okay, I won't open it."},
    "declined_send": {"ru": "Хорошо, не присылаю.", "en": "Okay, I won't send it."},
    "not_found": {
        "ru": "На компьютере с ядром не нашлось файла «{q}»{where}. Попробуй другое "
              "слово из названия или «файл про …» — тогда поищу и по содержимому.",
        "en": "I couldn't find a file matching “{q}”{where} on the core's computer. "
              "Try another word from its name, or “the file about …” to search "
              "inside files too."},
    "in_folder": {"ru": " в {folder}", "en": " in {folder}"},
    "folder_not_found": {
        "ru": "Не нашлось папки {folders} на компьютере с ядром. Проверь название "
              "или скажи, в какой она папке.",
        "en": "I couldn't find the folder {folders} on the core's computer. Check "
              "the name or tell me which folder it is in."},
    "ask_name": {
        "ru": "Какой файл? Напиши название или слово из него — или «файл про …», "
              "тогда поищу и по содержимому.",
        "en": "Which file? Tell me its name or a word from it — or “the file "
              "about …” to search inside files too."},
    "not_found_recent": {
        "ru": "Там нет подходящих файлов.",
        "en": "There are no matching files there."},
    "opened": {"ru": "Открываю «{name}» — {folder}.",
               "en": "Opening “{name}” — {folder}."},
    "revealed": {
        "ru": "«{name}» — программа или скрипт: запускать по сообщению не буду. "
              "Показываю его в папке — {folder}.",
        "en": "“{name}” is a program or script — I won't run it from a message. "
              "Showing it in its folder — {folder}."},
    "open_failed": {"ru": "Не получилось открыть «{name}»: {detail}.",
                    "en": "Couldn't open “{name}”: {detail}."},
    "sent": {"ru": "Вот «{name}» ({size}) — из {folder}.",
             "en": "Here's “{name}” ({size}) from {folder}."},
    "too_big": {
        "ru": "«{name}» весит {size} — больше {limit}, прислать не получится.",
        "en": "“{name}” is {size} — over the {limit} limit, so I can't send it."},
    "send_failed": {"ru": "Не получилось прислать «{name}»: {detail}.",
                    "en": "Couldn't send “{name}”: {detail}."},
    "gone": {
        "ru": "«{name}» больше нет на месте — видимо, его переместили или удалили.",
        "en": "“{name}” is no longer there — it was probably moved or deleted."},
}


def t(key: str, lang: Optional[str] = None, **values) -> str:
    en = str(lang or "").lower().startswith("en")
    return _T[key]["en" if en else "ru"].format(**values)


def _clip_middle(s: str, limit: int = 60) -> str:
    if len(s) <= limit:
        return s
    keep = (limit - 1) // 2
    return s[:keep] + "…" + s[-(limit - keep - 1):]


def _date(ts: float) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def choice_label(hit: ff.FileHit, lang: Optional[str] = None) -> str:
    return (f"{hit.name} — {_clip_middle(hit.folder)} · {_date(hit.mtime)}, "
            f"{ff.human_size(hit.size, lang)}")


def reveal(path: str) -> None:
    """Показать файл в папке, не запуская: Finder с выделением, Проводник
    с /select, на Linux — сама папка."""
    if sys.platform == "darwin":
        cmd = ["open", "-R", "--", path]
    elif sys.platform == "win32":
        cmd = ["explorer", f"/select,{path}"]
    else:
        cmd = ["xdg-open", os.path.dirname(path)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=15, **ff._no_window())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ff.FileOpenError(str(e))


class FileAccess:
    """Ход «файл с компьютера» одной персоны: список ждёт ответа по чату."""

    def __init__(self, base_dir: Optional[Path] = None):
        self._lock = threading.Lock()
        self._pending: Dict[str, dict] = {}
        # Ответ-список → заглушка для истории (забирает history_text)
        self._history: Dict[str, str] = {}
        self.base_dir = base_dir

    # --- ожидание ответа на список

    def has_pending(self, chat_key: str, user_id=None) -> bool:
        return self._peek(chat_key, user_id) is not None

    def _peek(self, chat_key: str, user_id=None) -> Optional[dict]:
        with self._lock:
            p = self._pending.get(str(chat_key))
            if not p:
                return None
            if time.time() > p["expires"]:
                self._pending.pop(str(chat_key), None)
                return None
            if p.get("user_id") and user_id is not None and str(user_id) != p["user_id"]:
                return None
            return p

    def clear(self, chat_key: str) -> None:
        with self._lock:
            self._pending.pop(str(chat_key), None)

    def answer_options(self, chat_key: str, user_id=None,
                       lang: Optional[str] = None) -> Optional[dict]:
        """Кнопки веба на список «какой файл?» — номер варианта и «нет»."""
        p = self._peek(chat_key, user_id)
        if not p:
            return None
        # scope — веб показывает эти кнопки и вне режима управления
        if len(p["hits"]) == 1:
            return {"kind": "yesno", "scope": "files",
                    "options": [{"role": "yes"}, {"role": "no"}]}
        opts = [{"label": f"{h.name} — {_clip_middle(h.folder, 40)}", "send": str(i)}
                for i, h in enumerate(p["hits"], 1)]
        return {"kind": "options", "scope": "files", "options": opts + [{"role": "no"}]}

    # --- ход

    def turn(self, text: str, chat_key: str, user_id: str, delivery: str,
             lang: Optional[str] = None, names=None,
             foreign_pending: bool = False) -> Optional[Tuple[str, List[dict]]]:
        """Реплика → (ответ, файлы к отправке) или None — реплика не наша.
        Что записать в историю вместо ответа — history_text(ответ).

        foreign_pending — в чате ждёт ответа подтверждение режима управления,
        созданное позже нашего списка: «да»/«2» относятся к нему."""
        from app.features.computer_control import classify_confirmation, parse_choice
        p = self._peek(chat_key, user_id)
        if p and foreign_pending:
            self.clear(chat_key)
            p = None
        if p:
            n = parse_choice(text, names)
            verdict = "YES" if n is not None else classify_confirmation(text, names)
            hits = p["hits"]
            if n is not None and not 1 <= n <= len(hits):
                return t("range", lang, n=n, max=len(hits)), []
            if verdict == "NO":
                self.clear(chat_key)
                return t("declined_send" if p["delivery"] == "send"
                         else "declined_open", lang), []
            self.clear(chat_key)
            if verdict == "YES":
                return self._act(hits[(n or 1) - 1], p["delivery"], chat_key, lang)
            # Не ответ на список — разбираем как новую реплику
        req = ff.parse_file_request(text)
        if req is None:
            return None
        if req.need_name:
            return t("ask_name", lang), []
        hits = ff.search(req, for_send=True)
        if req.soft:
            # Без слова «файл»: наше, только если папки нашлись и в них есть
            # файл с подходящим именем («открой поиск в ютубе» — не наше)
            hits = [h for h in hits if h.name_frac > 0]
            if not hits:
                return None
        elif req.unresolved:
            # «отчёт в 2025»: папки нет, слово ищем в имени; без него в имени
            # это не тот файл — честно «папки не нашлось»
            need = [ff.stem(h) for h in req.unresolved]
            hits = [h for h in hits if all(n in ff._fold(h.name) for n in need)]
        leader, choices = ff.pick(hits, req)
        if not choices:
            self._audit(chat_key, "not_found", None, False, query=req.query)
            if req.unresolved:
                return t("folder_not_found", lang,
                         folders=", ".join(f"«{h}»" for h in req.unresolved)), []
            where = ""
            if req.root_sims:
                where = t("in_folder", lang, folder=", ".join(
                    ff.display_folder(os.path.join(r, "x")) for r in req.root_sims))
            return t("not_found_recent" if not req.terms else "not_found",
                     lang, q=req.query or req.folder or "", where=where), []
        if leader is not None:
            return self._act(leader, delivery, chat_key, lang)
        with self._lock:
            self._pending[str(chat_key)] = {
                "hits": choices, "delivery": delivery,
                "expires": time.time() + PENDING_TTL_SEC,
                "user_id": str(user_id) if user_id is not None else None,
            }
        en = str(lang or "").startswith("en")
        verb = ("send" if en else "прислать") if delivery == "send" \
            else ("open" if en else "открыть")
        if len(choices) == 1:
            reply = t("one_choice", lang, verb=verb, item=choice_label(choices[0], lang))
            stub = t("one_choice_history", lang)
        else:
            items = "\n".join(f"{i}. {choice_label(h, lang)}"
                              for i, h in enumerate(choices, 1))
            reply = t("choices", lang, verb=verb, items=items)
            stub = t("choices_history", lang, n=len(choices))
        with self._lock:
            # Ответ, не дошедший до истории (сбой хода), не копится
            while len(self._history) >= 50:
                self._history.pop(next(iter(self._history)))
            self._history[reply] = stub
        return reply, []

    def history_text(self, reply: str) -> str:
        """Текст ответа для истории: список вариантов — заглушкой (имена и
        папки посторонних файлов облачной модели не нужны), остальное как есть."""
        with self._lock:
            return self._history.pop(reply, reply)

    def _act(self, hit: ff.FileHit, delivery: str, chat_key: str,
             lang: Optional[str]) -> Tuple[str, List[dict]]:
        # Между списком и ответом файл могли убрать или подменить на
        # запрещённое — проверяем заново
        if not os.path.isfile(hit.path) or not ff.path_allowed(hit.path):
            self._audit(chat_key, delivery, hit.path, False, detail="gone")
            return t("gone", lang, name=hit.name), []
        try:
            size = os.path.getsize(hit.path)
        except OSError:
            size = hit.size
        if delivery == "send":
            if size > MAX_SEND_BYTES:
                self._audit(chat_key, "send", hit.path, False, detail="too_big")
                return t("too_big", lang, name=hit.name, size=ff.human_size(size, lang),
                         limit=ff.human_size(MAX_SEND_BYTES, lang)), []
            self._audit(chat_key, "send", hit.path, True, size=size)
            return (t("sent", lang, name=hit.name, size=ff.human_size(size, lang),
                      folder=hit.folder),
                    [{"path": hit.path, "name": hit.name, "size": size}])
        if not ff.openable(hit.path):
            try:
                reveal(hit.path)
            except ff.FileOpenError as e:
                self._audit(chat_key, "reveal", hit.path, False, detail=str(e))
                return t("open_failed", lang, name=hit.name, detail=e), []
            self._audit(chat_key, "reveal", hit.path, True)
            return t("revealed", lang, name=hit.name, folder=hit.folder), []
        try:
            ff.open_file(hit.path)
        except ff.FileOpenError as e:
            self._audit(chat_key, "open", hit.path, False, detail=str(e))
            return t("open_failed", lang, name=hit.name, detail=e), []
        self._audit(chat_key, "open", hit.path, True)
        return t("opened", lang, name=hit.name, folder=hit.folder), []

    def _audit(self, chat_key: str, action: str, path: Optional[str], ok: bool,
               **extra) -> None:
        # Журнал на этой машине (как audit.jsonl режима управления): что
        # открыто или отправлено и куда. Сбой записи действие не ломает
        if self.base_dir is None:
            return
        try:
            from app.features.cc_privacy import audit_append
            self.base_dir.mkdir(parents=True, exist_ok=True)
            rec = {"ts": round(time.time(), 3), "chat_id": str(chat_key),
                   "action": action, "ok": ok}
            if path:
                rec["path"] = path
            rec.update({k: v for k, v in extra.items() if v is not None})
            audit_append(self.base_dir / "audit.jsonl", rec)
        except Exception as e:
            logger.debug(f"[FileAccess] аудит не записан: {e}")

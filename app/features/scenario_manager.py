"""
Сценарии (playbooks) — запись и воспроизведение цепочек действий на
компьютере пользователя. Надстройка над ComputerControlManager.

Идея: пользователь один раз вручную проводит бота по «сюжету» (открой сайт
пиццерии → выбери пиццу → в корзину → оформи), говорит «запомни сценарий
заказ пиццы» — и дальше фраза «закажи пиццу» запускает весь путь автоматически. Бот сам
спрашивает то, что меняется от раза к разу (какую пиццу, адрес), и
останавливается перед оплатой: деньги — всегда за человеком.

Механика:

  * запись — два режима: «запомни сценарий X» обобщает аудит-трассу
    ComputerControlManager за последние 30 минут; явные скобки «начни
    записывать сценарий (X)» … «сохрани сценарий» — трассу с момента
    старта (порог мягче: 2 действия). «отмени запись» — снять без
    сохранения. Сама сборка: LLM обобщает конкретные действия в шаги со
    слотами ({pizza}), при провале LLM — rule-based фолбэк (каждый ввод →
    вопрос);
  * шаги с оплатой (цель матчится под «оплатить/карта/pay/…») отрезаются
    и заменяются финальным handoff «дальше человек» — жёсткое правило,
    поверх любого LLM-вывода;
  * воспроизведение — state machine per chat: шаги исполняются подряд без
    пошаговых подтверждений, паузы только на вопросах к пользователю и на
    сбое («повтори»/«дальше»/«отмена»); элементы ищутся заново по ТЕКСТУ
    через резолверы computer_control (idx между сессиями нестабилен).
    Сбой шага спасается по цепочке: повтор после стабилизации DOM →
    LLM-восстановление
    (модель выбирает из живого снапшота, что нажать, чтобы приблизиться
    к цели — открыть меню/закрыть попап — клик исполняет система) →
    честный стоп;
  * автопредложение: после завершённой цепочки (≥3 действия в окне +
    закрывающая реплика «спасибо»/«готово»/…) бот один раз предлагает
    записать сценарий.

Формат хранения — data/{context}/scenarios.json:

  {"заказ пиццы": {"name", "aliases": [...], "created": ts,
                   "steps": [{"op": "open"|"ask"|"click"|"type"|"send"|"handoff", ...}]}}

Выключение: `features: {scenarios: false}` (по умолчанию вкл, когда включён
computer_control — без него сценарии бесполезны).
"""

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.core import timeutil
from app.core.language import detect_language, user_language_line
from app.core.atomic_io import atomic_write_json, load_json_safe
from app.core.paths import data_dir
from app.features.cc_privacy import (is_masked, read_tail_lines, redact_typed,
                                     scrub_url, typed_is_sensitive)

logger = logging.getLogger(__name__)

# Окно трассы для записи: «сюжет» — это действия за последние полчаса
TRACE_WINDOW_SEC = 1800
# Минимум действий в окне, чтобы было что записывать/предлагать
MIN_TRACE_ACTIONS = 3
# Какие действия аудита попадают в сценарий (read/scroll/скачивание — бытовые,
# не часть воспроизводимого сюжета; multi/nav в трассе не расчленяются)
_TRACE_KINDS = ("url", "click", "type", "send")

# Оплата — граница сценария: эти шаги отрезаются и заменяются handoff.
# Правило «это платёжный шаг» — одно на весь режим управления (needs_confirm,
# агент, сценарии): живёт в computer_control, здесь реэкспорт
from app.features.computer_control import (  # noqa: E402
    _MAP_SENSE_RE, _PAYMENT_RE, _is_payment)


# «запомни/запиши/сохрани (этот) сценарий (как/под названием) X»
_SAVE_RE = re.compile(
    r"^\s*(?:запомни|запиши|сохрани)\s+(?:этот\s+)?сценари[йя]\s*"
    r"(?:как\s+|под\s+названием\s+)?[«\"']?(.*?)[»\"']?\s*[.!…]*\s*$",
    re.IGNORECASE)
# «начни записывать сценарий (X)» — явные скобки записи: от этой команды
# до «сохрани сценарий» действия идут в трассу сценария (вместо окна 30 мин)
_START_REC_RE = re.compile(
    r"^\s*(?:начни|начать|включи|запусти|старт)\s+"
    r"(?:записывать|запись)\s*(?:сценари[йя]\s*)?"
    r"(?:как\s+|под\s+названием\s+)?[«\"']?(.*?)[»\"']?\s*[.!…]*\s*$",
    re.IGNORECASE)
# Англ.: «save (this) scenario (as) X», «start recording (a scenario) (as) X»
_SAVE_EN_RE = re.compile(
    r"^\s*(?:save|remember|record)\s+(?:this\s+|the\s+)?scenario\s*"
    r"(?:as\s+|named\s+|called\s+)?[«\"']?(.*?)[»\"']?\s*[.!…]*\s*$",
    re.IGNORECASE)
_START_REC_EN_RE = re.compile(
    r"^\s*(?:start|begin)\s+recording(?:\s+(?:a\s+|the\s+)?scenario)?\s*"
    r"(?:as\s+|named\s+|called\s+)?[«\"']?(.*?)[»\"']?\s*[.!…]*\s*$",
    re.IGNORECASE)
# «отмени запись» — снять запись без сохранения
_STOP_REC_RE = re.compile(
    r"^\s*(?:(?:отмени|останови|прекрати)\s+(?:запись|записывание)"
    r"(?:\s+сценари[йя])?|(?:cancel|stop|abort)\s+(?:the\s+)?recording"
    r"(?:\s+(?:the\s+)?scenario)?)\s*[.!…]*\s*$", re.IGNORECASE)
# Отмена активного прогона (проверяется только когда прогон идёт)
_CANCEL_RE = re.compile(
    r"^\s*(?:отмена|отмени|стоп\s+сценарий|отмени\s+сценарий|хватит|"
    r"прекрати|не\s+надо|забудь|выход|выйди|брось|отстань|"
    # Голое «стоп» при ждущем прогоне — отмена, а не ответ на слот
    # (иначе «стоп» вписался бы в поле сайта)
    r"стоп|остановись|stop|cancel|abort|quit|exit|never\s*mind|forget\s+it|"
    r"that[’']?s\s+enough|enough|(?:stop|cancel)\s+(?:the\s+)?scenario)\s*[.!…]*\s*$",
    re.IGNORECASE)
# Отрицательный ответ на опциональный вопрос («что-то ещё?» — «нет»)
_NO_RE = re.compile(
    r"^\s*(?:нет|не|ничего|не\s+надо|вс[её]|хватит|достаточно|пропусти|"
    r"пропустить|no|nope|nah|nothing|none|no\s+thanks|that[’']?s\s+(?:all|it)|"
    r"skip(?:\s+it)?)\s*[.!…]*\s*$", re.IGNORECASE)
# Управление после сбоя шага
_RETRY_RE = re.compile(r"^\s*(?:повтори|ещ[её]\s+раз|retry|try\s+again|again|"
                       r"repeat(?:\s+it)?)\s*[.!…]*\s*$", re.IGNORECASE)
_SKIP_RE = re.compile(r"^\s*(?:дальше|пропусти|скип|skip(?:\s+it)?|next|move\s+on)"
                      r"\s*[.!…]*\s*$", re.IGNORECASE)
# Закрывающая реплика для автопредложения записи
_CLOSE_RE = re.compile(
    r"^\s*(?:вс[её]|спасибо|готово|отлично|супер|класс|благодарю|ладно|"
    r"здорово|ок(?:ей)?|ok(?:ay)?|thanks|thank\s+you|done|great|perfect|awesome|"
    r"cool|nice|alright)\b", re.IGNORECASE)

_SLOT_RE = re.compile(r"\{([^\s{}]+)\}")

# Органы управления страницей — в список кандидатов для LLM-восстановления
# попадают принудительно (именно они открывают скрытые разделы)
_CTL_RE = re.compile(
    r"меню|menu|бургер|burger|закрыт|close|войти|кабинет|назад|главн|home",
    re.IGNORECASE)


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


# Матч имени сценария (match_scenario): слова без пунктуации; вежливые
# наполнители не мешают фразе быть «целиком именем»
_SC_WORD_RE = re.compile(r"\w+", re.UNICODE)
_SC_FILLER = frozenset({"пожалуйста", "плиз", "please", "ну", "а", "же"})
# Явный запуск: «запусти/выполни/давай (сценарий) X»
_SC_START_RE = re.compile(
    r"^(?:запусти|запустить|выполни|выполнить|включи|начни|сделай|давай|"
    r"run|start|play)\s+(?:(?:сценарий|сценария|scenario)\s+)?(.+)$"
    r"|^(?:сценарий|scenario)\s+(.+)$")


def _secret_typed(rec: dict) -> bool:
    """Ввод из трассы — секрет: уже замаскирован аудитом, поле помечено
    чувствительным, подпись/значение похожи на пароль/код/карту/контакт."""
    text = rec.get("text")
    return is_masked(text) or typed_is_sensitive(
        text, rec.get("element"), bool(rec.get("field_sensitive")))


def _slotify_secrets(steps: List[dict], trace: List[dict]) -> List[dict]:
    """Жёсткое правило поверх LLM: ввод секрета в сценарии — только слотом
    «спросить каждый раз», литерал не сохраняется в scenarios.json. URL
    шагов открытия — без токенов/секретных параметров."""
    secrets = {str(r.get("text")) for r in trace
               if r.get("kind") == "type" and r.get("text")
               and _secret_typed(r)}
    known = {s["slot"] for s in steps if s.get("op") == "ask"}
    out: List[dict] = []
    n = 0
    for s in steps:
        if s.get("op") == "open":
            s = dict(s, url=scrub_url(s.get("url")))
        elif s.get("op") == "type":
            value = str(s.get("value") or "")
            field = str(s.get("field") or "")
            literal = _SLOT_RE.sub("", value).strip()
            if literal and (value in secrets or is_masked(literal)
                            or "<SECRET" in value
                            or typed_is_sensitive(literal, field)):
                n += 1
                slot = f"секрет{n}"
                while slot in known:
                    n += 1
                    slot = f"секрет{n}"
                known.add(slot)
                out.append({"op": "ask", "slot": slot,
                            "question": f"Что ввести в поле «{field[:60]}»? "
                                        "(не сохраняю — спрошу в следующий раз)"})
                s = dict(s, value="{" + slot + "}")
        out.append(s)
    return out


class ScenarioManager:
    # Хранилище сценариев, запись из трассы и state machine воспроизведения.

    def __init__(self, context: str = "default", computer_control=None,
                 base_dir: Optional[Path] = None):
        self.context = context
        self.cc = computer_control
        self.base_dir = base_dir or data_dir() / context
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.base_dir / "scenarios.json"
        # RLock — один лок менеджера на ВСЁ его изменяемое состояние:
        # библиотеку сценариев (record_reply/build_from_trace держат лок на
        # время _save()) и in-memory словари ниже (_runs/_recording/_offered).
        # Telegram-поллинг, HTTP-обработчики веба и фоновые потоки зовут эти
        # методы одновременно, и без лока «проверил-и-записал» (идёт ли
        # запись, предлагали ли уже, есть ли прогон) может разъехаться на
        # два потока — двойной старт записи, два предложения на одно окно,
        # KeyError на снятом прогоне. Реентерабельность нужна, потому что
        # locked-методы зовут друг друга (record_reply → build_from_trace).
        self._lock = threading.RLock()
        self._scenarios: Dict[str, dict] = self._load()
        # Активные прогоны: chat_id → {name, steps, pos, slots, awaiting, failed}
        self._runs: Dict[str, dict] = {}
        # Явная запись: chat_id → {"since": ts, "name": str} — скобки
        # «начни записывать сценарий» … «сохрани сценарий»
        self._recording: Dict[str, dict] = {}
        # Автопредложение: chat_id → ts последнего действия трассы, на которое
        # уже ответили предложением (не донимать повторно в том же окне)
        self._offered: Dict[str, float] = {}

    # ── Хранилище ──────────────────────────────────────────

    def _load(self) -> Dict[str, dict]:
        # Битый scenarios.json (обрыв процесса посреди записи) не должен тихо
        # обнулять всю библиотеку сценариев незаметно для пользователя — как
        # у остальных менеджеров, это warning в лог + .corrupt-копия файла.
        data = load_json_safe(self._file, default={}, label="Scenarios")
        if not isinstance(data, dict):
            return {}
        out: Dict[str, dict] = {}
        for k, v in data.items():
            if not (isinstance(v, dict) and isinstance(v.get("steps"), list)):
                continue
            # Тот же валидатор, что на записи: сценарий с неисполнимым шагом
            # (например, ввод в поле без подписи) не поднимаем молча, чтобы
            # он не ломался посреди прогона; в лог — имя и причина.
            steps = self._validate_steps(v.get("steps"))
            if steps is None:
                logger.warning(f"[Scenarios] «{k}» пропущен при загрузке: шаги не "
                               "проходят валидацию (запиши сценарий заново)")
                continue
            out[str(k)] = {**v, "steps": steps}
        return out

    def _save(self):
        try:
            atomic_write_json(self._file, self._scenarios)
        except Exception as e:
            logger.warning(f"[Scenarios] scenarios.json не записан: {e}")

    def list_names(self) -> List[str]:
        with self._lock:
            return sorted(self._scenarios)

    def _phrase(self, key: str, template: str, **values) -> str:
        """Служебная реплика голосом персоны (flavor-банк), при пустом
        банке — честный шаблон; английский ход — английский шаблон
        (cc_texts.phrase)."""
        try:
            from app.features import cc_texts
            return cc_texts.phrase(self.context, key, template, self._lang(),
                                   **values)
        except Exception:
            return template

    def _lang(self) -> Optional[str]:
        # Язык текущего хода — его ставит бот менеджеру управления (set_turn)
        fn = getattr(self.cc, "turn_lang", None)
        try:
            return fn() if callable(fn) else None
        except Exception:
            return None

    def _t(self, key: str, **values) -> str:
        # Фиксированная реплика без банка — на языке хода
        from app.features import cc_texts
        return cc_texts.t(key, self._lang(), **values)

    # ── Парсеры команд ─────────────────────────────────────

    @staticmethod
    def parse_save_request(text: str) -> Optional[str]:
        # «запомни сценарий (как) X» → имя (может быть пустым — спросим).
        m = _SAVE_RE.match(str(text or "")) or _SAVE_EN_RE.match(str(text or ""))
        if not m:
            return None
        return m.group(1).strip()

    @staticmethod
    def parse_start_record(text: str) -> Optional[str]:
        """«начни записывать сценарий (заказ пиццы)» → имя или "" (без имени).
        None — не команда записи."""
        if not text or len(text) > 60:
            return None
        m = _START_REC_RE.match(str(text)) or _START_REC_EN_RE.match(str(text))
        if not m:
            return None
        return m.group(1).strip()

    @staticmethod
    def parse_stop_record(text: str) -> bool:
        return bool(_STOP_REC_RE.match(str(text or "")))

    # ── Явная запись (скобки «начни записывать»…«сохрани») ──

    def recording(self, chat_id) -> bool:
        with self._lock:
            return str(chat_id) in self._recording

    def record_start(self, chat_id, name: str = "") -> str:
        chat_id = str(chat_id)
        # Проверка «уже пишу?» и установка метки — одной операцией под локом,
        # иначе два «начни записывать» подряд заводят запись дважды и первая
        # (с её since) теряется
        with self._lock:
            rec = self._recording.get(chat_id)
            if rec is None:
                self._recording[chat_id] = {"since": time.time(),
                                            "name": str(name or "")}
        if rec is not None:
            since = timeutil.from_ts(rec["since"]).strftime("%H:%M")
            return self._phrase(
                "scenario_record_already",
                f"Уже записываю (с {since}). Когда закончишь — скажи "
                "«сохрани сценарий», передумал — «отмени запись».",
                since=since)
        logger.info(f"[Scenarios] Запись началась (chat {chat_id}, "
                    f"имя {name!r})")
        return self._phrase(
            "scenario_record_start",
            "Записываю сценарий. Делай действия как обычно — «открой …», "
            "«нажми …», «введи …» — всё пойдёт в запись. Закончить: "
            "«сохрани сценарий» (можно сразу с названием). Отменить: "
            "«отмени запись».")

    def record_stop(self, chat_id) -> str:
        with self._lock:
            rec = self._recording.pop(str(chat_id), None)
        if rec is None:
            return self._phrase("scenario_record_cancel_none",
                                "Запись не шла — нечего отменять.")
        logger.info(f"[Scenarios] Запись отменена (chat {chat_id})")
        return self._phrase("scenario_record_cancel",
                            "Запись отменена — ничего не сохранил.")

    @staticmethod
    def parse_cancel(text: str) -> bool:
        return bool(_CANCEL_RE.match(str(text or "")))

    def find_scenario(self, text: str, names=()) -> Optional[str]:
        """Фраза пользователя → имя сценария, только при уверенном матче
        (см. match_scenario): неоднозначное упоминание — None."""
        m = self.match_scenario(text, names)
        return m[0] if m and m[1] else None

    def match_scenario(self, text: str, names=()) -> Optional[Tuple[str, bool]]:
        """Фраза → (имя сценария, уверенно ли). Сценарий кликает по сайтам
        без подтверждения каждого шага, поэтому запуск — только когда фраза
        ЦЕЛИКОМ и есть имя/алиас (с точностью до словоформ, обращения к
        персоне, «пожалуйста») или явное «запусти/выполни (сценарий) X».
        Имя лишь встретилось в короткой фразе («мне заказ пиццы бы») —
        (имя, False): спросить подтверждение. Вопрос («сколько стоит заказ
        пиццы?») и длинный текст — не запуск вовсе (None)."""
        raw = str(text or "")
        filler = set(_SC_FILLER)
        for n in names or ():
            filler.update(_SC_WORD_RE.findall(_norm(n)))
        words = [w for w in _SC_WORD_RE.findall(_norm(raw)) if w not in filler]
        core = " ".join(words)
        if len(core) < 3:
            return None
        m = _SC_START_RE.match(core)
        explicit = (m.group(1) or m.group(2) or "").strip() if m else None
        from app.features.web_search import _stem

        def _stems(ws):
            return {_stem(w) for w in ws if len(w) >= 3}

        question = "?" in raw
        best = None  # (confident, длина ключа, имя)
        # Снимок под локом — сама проверка кандидатов идёт по копии, не
        # держим лок на время работы _stem/regex (иначе запись нового
        # сценария из другого чата ждала бы дольше, чем нужно)
        with self._lock:
            scenarios_snapshot = list(self._scenarios.items())
        for name, sc in scenarios_snapshot:
            keys = [name] + [str(a) for a in (sc.get("aliases") or [])]
            for key in keys:
                kw = _SC_WORD_RE.findall(_norm(key))
                k = " ".join(kw)
                if len(k) < 4:
                    continue
                confident = False
                for cand in (core, explicit):
                    if not cand:
                        continue
                    cw = cand.split()
                    if cand == k or (len(kw) >= 2 and len(cw) == len(kw)
                                     and _stems(cw) == _stems(kw)
                                     and _stems(kw)):
                        confident = True
                        break
                if not confident:
                    if question or len(words) - len(kw) > 3:
                        continue
                    padded = f" {core} "
                    loose = f" {k} " in padded or (
                        len(kw) >= 2 and _stems(kw)
                        and _stems(kw) <= _stems(words))
                    if not loose:
                        continue
                cand_rank = (confident, len(k), name)
                if best is None or cand_rank[:2] > best[:2]:
                    best = cand_rank
        return (best[2], best[0]) if best else None

    # ── Запись из аудит-трассы ─────────────────────────────

    def _trace(self, chat_id: str,
               window: int = TRACE_WINDOW_SEC,
               since: Optional[float] = None) -> List[dict]:
        """Хвост audit.jsonl: успешные действия этого чата за окно (или с
        момента since — явная запись «начни записывать сценарий»)."""
        if self.cc is None:
            return []
        path = Path(self.cc.base_dir) / "audit.jsonl"
        # Только хвост: лог ротируется по 10 МБ, читать его целиком ради
        # последних 400 строк — лишние мегабайты на каждую запись сценария
        try:
            lines = read_tail_lines(path, 400)
        except Exception:
            return []
        since_ts = float(since) if since else time.time() - window
        out = []
        for line in lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if (str(rec.get("chat_id")) == str(chat_id)
                    and rec.get("kind") in _TRACE_KINDS
                    and float(rec.get("ts") or 0) >= since_ts
                    # verify=uncertain: клик отправлен, но closed-loop не увидел
                    # эффекта — у JS-меню это ложный провал, шаг реально сработал
                    # и в сценарии нужен (иначе «меню» потеряется и следующий
                    # шаг «Расписание» не найдётся при воспроизведении)
                    and (rec.get("ok") or rec.get("verify") == "uncertain")):
                out.append(rec)
        # Приватность — на входе ВСЕХ потребителей трассы (обобщение LLM,
        # rule-based, _slotify_secrets): запись с приватной страницы (по
        # полному URL и хосту) помечается _private, ввод на ней и ввод с
        # известным секретом чата — маской (записи аудита до этой маски)
        from app.features.cc_privacy import (contains_value,
                                             known_secret_values, mask,
                                             page_candidates)
        known = known_secret_values()
        check = getattr(self.cc, "is_private_page", None)
        for rec in out:
            priv = False
            if callable(check):
                try:
                    priv = any(check(c) for c in page_candidates(rec))
                except Exception:
                    priv = False
            if priv:
                rec["_private"] = True
            text = rec.get("text")
            if rec.get("kind") == "type" and text and not is_masked(text) \
                    and (priv or contains_value(text, known)):
                rec["text"] = mask(text)
        return out

    @staticmethod
    def _trace_lines(trace: List[dict], cloud: bool = True) -> str:
        """Трасса строками для LLM-обобщения. cloud — промпт уходит облачной
        модели: подписи и адреса приватных записей (счёт, получатель,
        переписка) — заглушкой (обобщение с ней не сойдётся — rule-based);
        локальной модели приватной трассы — как есть. Ввод приватной
        страницы замаскирован ещё в _trace — становится слотом."""
        rows = []
        for n, rec in enumerate(trace, 1):
            kind, host = rec.get("kind"), rec.get("host") or ""
            hide = cloud and bool(rec.get("_private"))
            elem = "<PRIVATE>" if hide else (rec.get("element") or "?")
            if kind == "url":
                rows.append(f"{n}. open " + ("<PRIVATE PAGE>" if hide
                                             else scrub_url(rec.get('value'))))
            elif kind == "click":
                rows.append(f"{n}. click \"{elem}\" on {host}")
            elif kind == "type":
                # Секрет (пароль/код/карта/почта/телефон) в облачную модель
                # не отдаём: вместо значения — указание сделать слот
                text = ("<SECRET — must be an ask step, asked every time>"
                        if hide or _secret_typed(rec)
                        else rec.get("text") or "")
                rows.append(f"{n}. type \"{text}\" into the field "
                            f"\"{elem}\" on {host}")
            elif kind == "send":
                rows.append(f"{n}. send (Enter) on {host}")
        return "\n".join(rows)

    def _llm_generalize(self, trace: List[dict], name: str,
                        router) -> Optional[dict]:
        """LLM обобщает трассу в шаги со слотами. None — не удалось (фолбэк
        на rule-based). Ответ строго JSON, валидация схемы обязательна.
        Трасса с приватной страницы (банк, переписка, вход) — только
        локальной модели (PrivateRouter), нет её — rule-based; адреса open
        — только из трассы, алиасы — не «да»-слова и не другие команды."""
        if router is None or not trace:
            return None
        from app.features.cc_privacy import PrivateRouter
        private = any(r.get("_private") for r in trace)
        if private and not isinstance(router, PrivateRouter):
            router = PrivateRouter(router)
        lang = detect_language(name)
        lines = self._trace_lines(trace,
                                  cloud=not isinstance(router, PrivateRouter))
        prompt = (
            f"From the chain of actions on the computer, build a reusable "
            f"scenario \"{name}\".\n\nActions:\n{lines}\n\n"
            "Reply with ONLY JSON (no explanations and no markdown):\n"
            '{"aliases": ["..."], "steps": [...]}\n\n'
            "Step format:\n"
            '- {"op":"open","url":"..."} — open a site\n'
            '- {"op":"click","target":"button/link text","host":"..."} — click\n'
            '- {"op":"type","field":"field label","value":"text","host":"..."} — type\n'
            '- {"op":"send","host":"..."} — send (Enter)\n'
            '- {"op":"ask","slot":"slot_name","question":"question to the user"} '
            "— ask; the answer is substituted into the following steps as {slot_name}\n"
            '- {"op":"handoff","message":"..."} — final: a human acts from here\n\n'
            "Rules:\n"
            "- EVERY action in the list must become a step (or an ask+step pair). "
            "Actions must not be removed or merged: two identical clicks "
            "(\"menu\") on different pages are TWO different steps, both required. "
            "The number of open/click/type/send steps must equal the number of actions "
            "(except payment steps — handoff replaces them).\n"
            "- Values that will be different next time (product name, "
            "address, text) — replace with an ask step BEFORE the step that uses them, "
            "and put {slot} in that step: {\"op\":\"click\",\"target\":\"{pizza}\"}.\n"
            "- A typed value shown as <SECRET …> (password, code, card, email, "
            "phone, login) MUST become an ask step with a {slot}; never write "
            "a literal value for it.\n"
            "- Keep navigation clicks (menu, sign in, cart, checkout, next) "
            "literal and in the original order. Copy button/field labels "
            "(target, field) exactly as in the actions, do not translate them.\n"
            "- Replace payment steps (оплатить/pay, карта/card) with one final "
            '{"op":"handoff","message":"Payment is up to you from here."}.\n'
            "- aliases: 2-4 short conversational trigger phrases "
            "(\"order pizza\" / «закажи пиццу»).\n"
            "The question, message and aliases texts are shown to the user. "
            + user_language_line(lang))
        try:
            resp = router.get_response([{"role": "user", "content": prompt}],
                                       temperature=0.0, max_tokens=900, top_p=0.1)
        except Exception as e:
            logger.debug(f"[Scenarios] LLM-обобщение трассы не удалось: {e}")
            return None
        if resp is None and isinstance(router, PrivateRouter):
            logger.info("[Scenarios] трасса с приватной страницы, локальной "
                        "модели нет — rule-based")
            return None
        data = self._extract_json(resp or "")
        if not isinstance(data, dict):
            logger.info(f"[Scenarios] LLM-ответ не JSON: {(resp or '')[:80]!r}")
            return None
        steps = self._validate_steps(data.get("steps"))
        if steps is None:
            logger.info("[Scenarios] LLM-шаги не прошли валидацию")
            return None
        # Адрес open пишет модель — открывается при воспроизведении без
        # вопроса: только адреса из самой трассы (после scrub_url), иначе
        # rule-based. Заглушка приватной записи в шаге — тоже мимо
        allowed = {self._url_key(r.get("value")) for r in trace
                   if r.get("kind") == "url" and r.get("value")}
        for s in steps:
            if s["op"] == "open" and self._url_key(s["url"]) not in allowed:
                logger.info(f"[Scenarios] LLM дала адрес не из трассы "
                            f"({scrub_url(s['url'])[:80]}) — rule-based")
                return None
            if "<PRIVATE" in " ".join(str(s.get(k) or "") for k in
                                      ("target", "field", "value", "url")):
                logger.info("[Scenarios] LLM-шаг с заглушкой приватной "
                            "записи — rule-based")
                return None
        # Страховка от «потерянных» шагов: LLM любит выкинуть «лишние», с его
        # точки зрения, клики (второе «меню» на новой странице, промежуточные
        # экраны) — и сценарий рассыпается при воспроизведении (негде нажать
        # «Расписание», если пропущен клик, открывающий меню). Исполняемых
        # шагов должно быть не меньше, чем действий в трассе (минус шаги
        # оплаты — их заменяет handoff); иначе — rule-based фолбэк.
        pay_in_trace = sum(
            1 for r in trace
            if _is_payment(str(r.get("element") or "")
                           + " " + str(r.get("text") or "")))
        n_exec = sum(1 for s in steps
                     if s["op"] in ("open", "click", "type", "send"))
        if n_exec < len(trace) - pay_in_trace:
            logger.info(f"[Scenarios] LLM потеряла шаги: {n_exec} из "
                        f"{len(trace) - pay_in_trace} — фолбэк на rule-based")
            return None
        aliases = [str(a).strip() for a in (data.get("aliases") or [])
                   if str(a).strip()]
        # Алиас запускает сценарий раньше «да» на pending и команд режима
        # управления: «давай»/«открой ютуб» от модели перехватили бы их
        good = [a for a in aliases if self._alias_ok(a)]
        if len(good) < len(aliases):
            logger.info(f"[Scenarios] отброшены алиасы-команды: "
                        f"{len(aliases) - len(good)}")
        return {"aliases": good[:5], "steps": steps}

    @staticmethod
    def _url_key(url) -> str:
        # Сравнение адресов шага и трассы: после scrub_url, без схемы/www,
        # хост без регистра, без хвостового «/»
        from urllib.parse import urlsplit
        s = scrub_url(str(url or "").strip())
        try:
            p = urlsplit(s)
        except Exception:
            return s
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        key = host + (p.path or "").rstrip("/")
        return key + ("?" + p.query if p.query else "")

    def _alias_ok(self, alias: str) -> bool:
        """Алиас от модели годен: не «да/нет/отмена/стоп/повтори» и не
        фраза, которую разбирает парсер команды режима управления
        (открой X, нажми Y, листай…) или запись/задача."""
        s = " ".join(str(alias or "").split())
        if len(s) < 3:
            return False
        from app.features import computer_control as ccm
        try:
            if ccm.classify_confirmation(s) != "UNKNOWN":
                return False
        except Exception:
            return False
        if (self.parse_cancel(s) or _NO_RE.match(s) or _RETRY_RE.match(s)
                or _SKIP_RE.match(s) or self._ALIAS_STOP_RE.match(s)
                or self.parse_save_request(s) is not None
                or self.parse_start_record(s) is not None
                or self.parse_stop_record(s)):
            return False
        try:
            if ccm.parse_control_mode(s) is not None:
                return False
        except Exception:
            pass
        try:
            from app.features.task_agent import parse_task_request
            if parse_task_request(s):
                return False
        except Exception:
            pass
        for fname in self._ALIAS_CMD_PARSERS:
            fn = getattr(ccm, fname, None)
            if fn is None:
                continue
            try:
                if fn(s):
                    return False
            except Exception:
                continue
        return True

    # Стоп-слова хода (стоп листания/действия) — не алиас
    _ALIAS_STOP_RE = re.compile(
        r"^\s*(?:стоп|stop|хватит|останови\w*|отмена|cancel|enough)\b",
        re.IGNORECASE)
    # Парсеры команд режима управления: фраза, которую разбирает любой из
    # них, — команда, а не имя сценария
    _ALIAS_CMD_PARSERS = (
        "parse_open_many", "parse_open_with_url", "parse_open_request",
        "parse_open_on_page", "parse_click_request", "parse_hover_request",
        "parse_type_request", "parse_send_request", "parse_key_request",
        "parse_scroll_request", "parse_scroll_to_goal", "parse_tab_op",
        "parse_tab_switch", "parse_tab_list_query", "parse_close_request",
        "parse_read_request", "parse_page_view_request", "parse_media_request",
        "parse_slider_request", "parse_zoom_request", "parse_download_request",
        "parse_erase_request", "parse_cart_request", "parse_search_on_site")

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        # Первый сбалансированный {...} в ответе (LLM любит ```json-обёртки).
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None

    @staticmethod
    def _validate_steps(raw) -> Optional[List[dict]]:
        """Строгая схема шагов; каждый {слот} должен быть определён ask-шагом
        раньше использования. None — схема не сошлась.

        ЕДИНСТВЕННЫЙ валидатор шага в модуле: через него проходит и вывод LLM,
        и rule-based фолбэк, и то, что читается из scenarios.json (см. _load) —
        иначе неисполнимый шаг может попасть в файл из фолбэка или зависнуть
        там от старой записи."""
        if not isinstance(raw, list) or not raw:
            return None
        steps: List[dict] = []
        known_slots = set()
        for s in raw:
            if not isinstance(s, dict):
                return None
            op = s.get("op")
            if op == "open":
                url = str(s.get("url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    return None
                steps.append({"op": "open", "url": url})
            elif op == "click":
                target = str(s.get("target") or "").strip()
                if not target:
                    return None
                for slot in _SLOT_RE.findall(target):
                    if slot not in known_slots:
                        return None
                steps.append({"op": "click", "target": target[:80],
                              "host": str(s.get("host") or "") or None})
            elif op == "type":
                field = str(s.get("field") or "").strip()
                value = str(s.get("value") or "")
                # Пустая подпись поля делает шаг неисполнимым: resolve_type
                # получает «текст в поле » и ищет поле без имени — при
                # воспроизведении это гарантированный «не нашёл поле «»»
                # посреди сценария. Отсекаем на записи и на загрузке, а не
                # в момент прогона у пользователя.
                if not field:
                    return None
                for slot in _SLOT_RE.findall(field + value):
                    if slot not in known_slots:
                        return None
                steps.append({"op": "type", "field": field[:80],
                              "value": value[:200],
                              "host": str(s.get("host") or "") or None})
            elif op == "send":
                steps.append({"op": "send",
                              "host": str(s.get("host") or "") or None})
            elif op == "ask":
                slot = str(s.get("slot") or "").strip()
                question = str(s.get("question") or "").strip()
                if not slot or not question or slot in known_slots:
                    return None
                known_slots.add(slot)
                step = {"op": "ask", "slot": slot[:30], "question": question[:200]}
                if s.get("optional"):
                    step["optional"] = True
                steps.append(step)
            elif op == "handoff":
                msg = str(s.get("message") or "").strip() or \
                    "Дальше — за тобой."
                steps.append({"op": "handoff", "message": msg[:200]})
            else:
                return None
        return steps or None

    @staticmethod
    def _rule_steps(trace: List[dict]) -> List[dict]:
        # Фолбэк без LLM: действия как есть, каждый ввод текста — вопросом.
        steps: List[dict] = []
        n = 0
        for rec in trace:
            kind, host = rec.get("kind"), rec.get("host") or None
            if kind == "url":
                steps.append({"op": "open", "url": str(rec.get("value") or "")})
            elif kind == "click":
                steps.append({"op": "click",
                              "target": str(rec.get("element") or "?")[:80],
                              "host": host})
            elif kind == "type":
                n += 1
                slot = f"текст{n}"
                field = str(rec.get("element") or "")[:80]
                steps.append({"op": "ask", "slot": slot,
                              "question": f"Что ввести в поле «{field}»?"})
                steps.append({"op": "type", "field": field,
                              "value": "{" + slot + "}", "host": host})
            elif kind == "send":
                steps.append({"op": "send", "host": host})
        return steps

    @staticmethod
    def _strip_payment(steps: List[dict]) -> List[dict]:
        # Всё от первого шага с оплатой отрезается, вместо него — handoff.
        # Коммит/отправка/удаление остаются шагами: при воспроизведении их
        # ждёт «да» человека (_exec_step_once, гейт execute), а найденная
        # на живой странице оплата — тот же handoff
        from app.features.computer_control import ComputerControlManager
        out = []
        for s in steps:
            hay = " ".join(str(s.get(k) or "")
                           for k in ("target", "field", "value"))
            _lab = str(s.get("target") or s.get("field") or "")
            if s["op"] in ("click", "type") and (
                    _is_payment(hay) or ComputerControlManager.risky_label(
                        {"kind": "click", "element": _lab,
                         "host": s.get("host")}) == "payment"):
                out.append({"op": "handoff",
                            "message": "Дальше оплата — это уже за тобой, "
                                       "я к деньгам не прикасаюсь."})
                return out
            if s["op"] == "handoff":
                out.append(s)
                return out  # handoff — всегда финал
            out.append(s)
        return out

    def build_from_trace(self, chat_id: str, name: str, router,
                         since: Optional[float] = None
                         ) -> Tuple[Optional[dict], Optional[str]]:
        """Трасса → сценарий (LLM-обобщение, фолбэк rule-based). since —
        явная запись: трасса с момента «начни записывать сценарий», порог
        действий мягче (2 вместо 3 — короткий макрос тоже сценарий)."""
        trace = self._trace(chat_id, since=since)
        min_actions = 2 if since else MIN_TRACE_ACTIONS
        if len(trace) < min_actions:
            return None, self._t("scenario_trace_short_since" if since
                                 else "scenario_trace_short_recent",
                                 n=len(trace))
        built = self._llm_generalize(trace, name, router)
        if built is None:
            # Фолбэк проходит ТОТ ЖЕ валидатор, что и вывод LLM — иначе
            # неисполнимый шаг («ввести в поле «»») мог бы попасть в файл
            rule = self._validate_steps(self._rule_steps(trace))
            if rule is None:
                logger.info(f"[Scenarios] «{name}»: rule-based шаги не прошли "
                            "валидацию (скорее всего ввод в поле без подписи)")
                return None, self._t("scenario_trace_unlabeled")
            built = {"aliases": [], "steps": rule}
            logger.info(f"[Scenarios] «{name}»: rule-based запись "
                        f"({len(built['steps'])} шагов)")
        steps = _slotify_secrets(self._strip_payment(built["steps"]), trace)
        if len(steps) < 2:
            return None, self._t("scenario_trace_empty")
        key = _norm(name)
        scenario = {"name": key, "aliases": built.get("aliases") or [],
                    "created": time.time(), "steps": steps}
        with self._lock:
            self._scenarios[key] = scenario
            self._save()
        logger.info(f"[Scenarios] Записан «{key}»: {len(steps)} шагов, "
                    f"алиасы {scenario['aliases']}")
        return scenario, None

    def record_reply(self, chat_id: str, name: str, router) -> str:
        """Ответ на «запомни/сохрани сценарий X»: собрать, сохранить, описать.
        При активной записи («начни записывать сценарий») — трасса с момента
        старта, имя по умолчанию из стартовой команды; после сохранения
        запись снимается."""
        with self._lock:
            rec = self._recording.get(str(chat_id))
        if rec is not None and not name:
            name = str(rec.get("name") or "")
        if not name:
            return self._phrase(
                "scenario_save_ask_name",
                "Как назвать сценарий? Скажи так: «сохрани сценарий "
                "заказ пиццы».")
        scenario, err = self.build_from_trace(
            chat_id, name, router, since=rec.get("since") if rec else None)
        if err:
            if rec is not None:
                return err + self._t("scenario_record_goes_on")
            return err
        with self._lock:
            self._recording.pop(str(chat_id), None)
        asks = [s["question"] for s in scenario["steps"] if s["op"] == "ask"]
        tail = (self._t("scenario_saved_asks",
                        questions=" ".join(f"«{q}»" for q in asks))
                if asks else "")
        # Хвост с вопросами слотов — данные, а не голос персоны: добавляем
        # к сгенерированной фразе как есть
        return (self._phrase(
                    "scenario_saved",
                    f"Записал сценарий «{scenario['name']}» — "
                    f"{len(scenario['steps'])} шагов. Теперь просто скажи "
                    f"«{scenario['name']}».",
                    name=scenario["name"], steps=len(scenario["steps"]))
                + tail)

    # ── Воспроизведение (state machine per chat) ───────────

    def active(self, chat_id) -> bool:
        with self._lock:
            return str(chat_id) in self._runs

    def _subst(self, text: str, slots: Dict[str, str]) -> str:
        return _SLOT_RE.sub(
            lambda m: slots.get(m.group(1), m.group(0)), str(text or ""))

    def start(self, name: str, chat_id, router) -> str:
        # Запуск сценария: первый батч шагов до первой паузы.
        with self._lock:
            sc = self._scenarios.get(name)
        if sc is None:
            return self._phrase("scenario_not_found",
                                f"Сценария «{name}» у меня нет.", name=name)
        run = {"name": name, "steps": sc["steps"], "pos": 0, "slots": {},
               "awaiting": None, "failed": False,
               # Шаг на «да»/«нет» (гейт подтверждения) и кто его подтверждает
               "confirm": None, "user_id": self._requester(chat_id)}
        with self._lock:
            self._runs[str(chat_id)] = run
        lines = [self._phrase(
            "scenario_started",
            f"Погнали — «{name}» ({len(sc['steps'])} шагов). "
            "Скажи «отмена», если передумаешь.",
            name=name, steps=len(sc["steps"]))]
        # _advance исполняет шаги (браузер, LLM — десятки секунд) — лок на это
        # время НЕ держим: он защищает словари менеджера, а сам прогон
        # принадлежит одному чату
        lines += self._advance(run, chat_id, router)
        if run["pos"] >= len(run["steps"]) and not run["awaiting"]:
            with self._lock:
                self._runs.pop(str(chat_id), None)
        return "\n".join(lines)

    def feed(self, chat_id, user_input: str, router) -> Optional[str]:
        """Ответ пользователя внутри прогона: слот, повтор/пропуск после
        сбоя, либо «не понял». None — «сообщение не наше»: прогон уже снят,
        фраза должна уйти в обычный диалог (антизалипание)."""
        with self._lock:
            run = self._runs.get(str(chat_id))
        if run is None:
            return None
        msg = str(user_input or "").strip()
        pre: List[str] = []
        if run.get("awaiting"):
            step = run["awaiting"]
            run["awaiting"] = None
            if step.get("optional") and _NO_RE.match(msg):
                pass  # опциональный слот пропущен
            else:
                run["slots"][step["slot"]] = msg[:200]
        elif run.get("confirm"):
            # Шаг ждёт «да» (оформление/отправка/удаление, непроверенная
            # подпись): только от того, кто запустил сценарий, и PENDING_TTL
            from app.features.computer_control import (
                PENDING_TTL_SEC, classify_confirmation)
            pend = run["confirm"]
            who = self._requester(chat_id)
            if pend.get("user_id") and who and who != pend["user_id"]:
                return self._t("scenario_confirm_foreign")
            verdict = classify_confirmation(msg)
            if verdict == "NO":
                with self._lock:
                    self._runs.pop(str(chat_id), None)
                return self._t("scenario_declined", name=run["name"])
            if verdict != "YES":
                run["unhandled"] = run.get("unhandled", 0) + 1
                if run["unhandled"] >= 2:
                    # Антизалипание, как на сбойном шаге
                    with self._lock:
                        self._runs.pop(str(chat_id), None)
                    logger.info(f"[Scenarios] «{run['name']}» снят: 2 "
                                "нераспознанных ответа на подтверждение шага")
                    return None
                return self._t("scenario_confirm_wait")
            run["confirm"] = None
            run["unhandled"] = 0
            if time.time() - float(pend.get("ts") or 0) > PENDING_TTL_SEC:
                # «да» спустя минуту — не про эту страницу: шаг резолвится
                # заново и при риске спросит ещё раз
                pre.append(self._t("scenario_confirm_expired"))
            else:
                # Единственное место, где шаг сценария получает токен «да»
                self._grant(pend["act"], "scenario", who or pend.get("user_id"))
                run["confirmed_act"] = pend["act"]
        elif run.get("failed"):
            if _RETRY_RE.match(msg):
                run["failed"] = False
                run["unhandled"] = 0
            elif _SKIP_RE.match(msg):
                run["failed"] = False
                run["unhandled"] = 0
                run["pos"] += 1
            else:
                # Антизалипание: человек пишет что-то своё, а бот в ответ
                # вечно твердит «повтори/дальше/отмена» — так бот «ничего не
                # может». Первый раз напоминаем, на второй подряд — снимаем
                # прогон и отдаём сообщение в обычный поток (None).
                run["unhandled"] = run.get("unhandled", 0) + 1
                if run["unhandled"] >= 2:
                    name = run["name"]
                    with self._lock:
                        self._runs.pop(str(chat_id), None)
                    logger.info(f"[Scenarios] «{name}» снят: 2 нераспознанных "
                                "сообщения подряд на сбойном шаге")
                    return None
                return self._phrase(
                    "scenario_stuck",
                    "Стою на сбойном шаге. Скажи «повтори», "
                    "«дальше» (пропустить) или «отмена».")
        else:
            # Прогон ждёт только при awaiting/failed/confirm; иначе — не наше
            return None
        lines = pre + self._advance(run, chat_id, router)
        if run["pos"] >= len(run["steps"]) and not run["awaiting"]:
            with self._lock:
                self._runs.pop(str(chat_id), None)
        return "\n".join(lines) or self._t("scenario_continue")

    def _requester(self, chat_id) -> Optional[str]:
        # Автор текущего хода (менеджер управления знает его с начала хода)
        fn = getattr(self.cc, "current_requester", None)
        try:
            return fn(chat_id) if callable(fn) else None
        except Exception:
            return None

    @staticmethod
    def _grant(act: dict, via: str, by=None) -> None:
        from app.features.computer_control import ComputerControlManager
        ComputerControlManager.grant_confirmation(act, via, by=by)

    def request_stop(self, chat_id) -> bool:
        """«стоп» до лока хода (cc_turn_enter): прогону, который сейчас
        исполняет шаги, — флаг; цикл шагов проверяет его перед каждым. Ждущий
        прогон (слот/сбой/«да») не трогаем — «стоп» дойдёт до него своим ходом
        (parse_cancel)."""
        with self._lock:
            run = self._runs.get(str(chat_id))
            if run is None or not run.get("advancing"):
                return False
            run["stop"] = True
            return True

    def _stop_hit(self, run: dict, chat_id) -> bool:
        # «стоп» этого прогона: флаг от бота или флаг менеджера управления
        if run.get("stop"):
            return True
        fn = getattr(self.cc, "stop_requested", None)
        if not callable(fn):
            return False
        k = "" if chat_id is None else str(chat_id).strip()
        try:
            return bool(fn(None if k in ("", "None") else k))
        except Exception:
            return False

    def cancel(self, chat_id) -> str:
        with self._lock:
            run = self._runs.pop(str(chat_id), None)
        if run:
            return self._phrase("scenario_run_cancel",
                                f"Сценарий «{run['name']}» отменён.",
                                name=run["name"])
        return self._phrase("scenario_run_cancel_none",
                            "Нечего отменять — сценарий не запущен.")

    def _exec_step(self, step: dict, run: dict, chat_id, router
                   ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Один шаг → (ok, ошибка|None, реплика об успехе|None).
        Порядок спасения: 1) повтор после стабилизации DOM (страница
        догружается, оверлей анимируется — ждём событие, а не слепой слип);
        2) LLM-восстановление — модель смотрит живой снапшот
        и выбирает, что нажать, чтобы приблизиться к цели (открыть меню,
        закрыть попап, другое название), затем шаг повторяется; 3) честный
        стоп на «повтори/дальше/отмена»."""
        def _halt() -> bool:
            # Пауза на «да», передача оплаты человеку или «стоп» — не сбой:
            # ни повтора, ни LLM-восстановления
            return bool(run.get("confirm") or run.get("handoff")
                        or self._stop_hit(run, chat_id))

        r = self._exec_step_once(step, run, chat_id, router)
        if r[0] or step["op"] not in ("click", "type") or _halt():
            return r
        try:
            from app.features import browser_actions as _ba
            _ba.wait_dom_idle(getattr(self.cc, "_last_host", None),
                              getattr(self.cc, "_last_tab_id", None),
                              timeout_sec=2.5, min_wait=0.8)
        except Exception:
            time.sleep(2)
        r = self._exec_step_once(step, run, chat_id, router)
        if r[0] or _halt():
            return r
        rec = self._llm_recover(step, run, chat_id, router)
        if rec == "skip":
            # Шаг устарел (страница уже дальше по сценарию) — считаем пройденным
            return True, None, self._t("scenario_step_skipped")
        if rec:
            r = self._exec_step_once(step, run, chat_id, router)
        return r

    @staticmethod
    def _step_line(step: dict, slots: Dict[str, str]) -> str:
        # Шаг сценария одной человекочитаемой строкой (для LLM-контекста).
        # Значения слотов в LLM-контекст не подставляем НИКОГДА — только
        # плейсхолдер {слот} (ответ на «спросить» — пароль/ключ под любой
        # подписью поля, Contraseña/Passwort regexp не узнает); литералы —
        # redact_typed + маска известных секретов чата
        from app.features.cc_privacy import (known_secret_values, mask_values,
                                             redact_inline)
        known = known_secret_values()
        op = step["op"]

        def lit(s, field=None, typed=False):
            s = str(s or "")
            parts = re.split(r"(\{[^\s{}]+\})", s)
            for i, p in enumerate(parts):
                if i % 2 or not p:
                    continue
                p = mask_values(p, known)
                parts[i] = (redact_typed(p, field) if typed
                            else redact_inline(p))
            return "".join(parts)
        if op == "open":
            return f"open {scrub_url(step.get('url'))}"
        if op == "click":
            return f"click \"{lit(step.get('target'))}\" on {step.get('host') or 'the page'}"
        if op == "type":
            value = lit(str(step.get("value") or "")[:30], step.get("field"),
                        typed=True)
            return (f"type \"{value}\" into the field "
                    f"\"{lit(step.get('field'))}\" on {step.get('host') or 'the page'}")
        if op == "send":
            return f"send (Enter) on {step.get('host') or 'the page'}"
        if op == "ask":
            return f"ask the user: \"{step.get('question')}\""
        return str(step.get("message") or "hand control over to a human")

    def _llm_recover(self, step: dict, run: dict, chat_id, router) -> bool:
        """Сбой «элемент не найден/не нажался»: спросить LLM по ЖИВОМУ
        снапшоту, что нажать, чтобы приблизиться к цели. LLM выбирает номер
        из реальных элементов страницы (как _choose_element в computer_control)
        — «сыграть» клик она не может, исполняет и проверяет система.
        True — клик восстановления выполнен (исходный шаг повторят снаружи);
        "skip" — шаг уже не нужен, страница сама ушла дальше; False — не
        восстановили."""
        if router is None or self.cc is None or step["op"] not in ("click", "type"):
            return False
        try:
            url, host, items, tab_id, err = self.cc._snapshot_for(
                step.get("host") or None, chat_id=str(chat_id))
        except Exception:
            return False
        if not items:
            return False
        # Живой снапшот — текст страницы: с приватной (вход/оплата) — только
        # локальной модели, иначе восстановления нет
        privacy_router = getattr(self.cc, "_privacy_router", None)
        if privacy_router is not None:
            router = privacy_router(router, url or host)
        goal = self._subst(step.get("target") or step.get("field") or "",
                           run["slots"])
        # Цель шага — подпись элемента (со слотом «кому» — имя нужно модели
        # для выбора), но секрет чата/похожее на секрет значение слота — маской
        from app.features.cc_privacy import (is_sensitive_label,
                                             known_secret_values, mask_values)
        goal_llm = mask_values(goal, list(known_secret_values()) + [
            v for k, v in (run.get("slots") or {}).items()
            if v and (typed_is_sensitive(v) or is_sensitive_label(k)
                      or re.match(r"(?:секрет|secret)", str(k), re.I))])
        # Дорожная карта сценария: модель видит, что уже сделано, на каком
        # шаге сломались и что дальше — выбор «что нажать» становится
        # осмысленным («меню» открывает панель, где живёт «Расписание»)
        roadmap = []
        for n, s in enumerate(run["steps"]):
            mark = "✓" if n < run["pos"] else ("✗" if n == run["pos"] else "·")
            roadmap.append(f"{mark} {n + 1}. {self._step_line(s, run['slots'])}")
        roadmap_txt = "\n".join(roadmap)
        # Кандидаты для модели — не «первые 25 сырых» (в них могут попасть
        # ссылки подвала сайта), а: топ по релевантности цели
        # (тот же скоринг, что у выбора элемента) + принудительно органы
        # управления страницей (меню/бургер/закрыть/войти) — именно они
        # открывают скрытые разделы, и без них модель слепа
        try:
            scored = self.cc._score_candidates(items, goal, host=host)
            ranked = [it for _s, it in scored]
        except Exception:
            ranked = list(items)
        top = ranked[:15]
        ctl = [it for it in items
               if _CTL_RE.search(str(it.get("text") or ""))
               and all(it.get("idx") != t.get("idx") for t in top)]
        shown = (top + ctl)[:25]
        lines = "\n".join(
            f"{n}) [{it.get('tag')}/{it.get('role') or '-'}] "
            f"{str(it.get('text') or '')[:60]}"
            for n, it in enumerate(shown, 1))
        prompt = (
            f"We are running the scenario \"{run['name']}\" step by step "
            "(✓ — already done, ✗ — broke here, · — next):\n"
            f"{roadmap_txt}\n\n"
            f"At step ✗ we need to "
            + (f"click \"{goal_llm}\"" if step["op"] == "click"
               else f"type text into the field \"{goal_llm}\"")
            + f", but there is no such element among the visible ones on the page {host}.\n"
            f"Visible page elements:\n{lines}\n"
            "Maybe a menu has to be opened first, a popup closed, "
            "or the element has a different name. Considering what is already done and what "
            "must come after, reply with ONLY the number of the element worth "
            "clicking to get closer to the goal of step ✗. "
            "If step ✗ is no longer needed (the page already moved on along the scenario — "
            "for example, after signing in we were already redirected to the right site) — "
            "reply \"skip\". "
            "If nothing will help — reply \"no\".\n"
            + user_language_line(detect_language(run["name"]) or detect_language(goal)))
        try:
            resp = router.get_response([{"role": "user", "content": prompt}],
                                       temperature=0.0, max_tokens=8, top_p=0.1)
        except Exception as e:
            logger.debug(f"[Scenarios] LLM-восстановление недоступно: {e}")
            return False
        # «пропустить» — шаг устарел: страница сама ушла дальше по сценарию
        # (напр., SSO-вход перекинул на целевой сайт без промежуточного клика)
        if (resp or "").strip().lower().lstrip("«\"'").startswith(("пропуст", "skip")):
            logger.info(f"[Scenarios] LLM-восстановление: шаг «{goal_llm[:40]}» "
                        "устарел — пропускаем")
            return "skip"
        m = re.fullmatch(r"\s*(\d{1,2})\s*", resp or "")
        if not m or not (1 <= int(m.group(1)) <= len(shown)):
            logger.info(f"[Scenarios] LLM-восстановление: нет кандидата "
                        f"({(resp or '')[:40]!r})")
            return False
        item = shown[int(m.group(1)) - 1]
        act = {"kind": "click", "idx": int(item["idx"]),
               "element": str(item.get("text") or "")[:80],
               "host": host, "value": url, "origin": "scenario"}
        # aria/title — в действие: у иконки подпись бывает только там
        for k in ("aria", "title"):
            if item.get(k):
                act[k] = str(item[k])[:80]
        if tab_id is not None:
            act["tab_id"] = tab_id
        # Элемент выбрала модель по подписям живой страницы (недоверенный
        # текст): оплату, финальный коммит/отправку, удаление/выход
        # восстановлением не нажимаем — шаг встанет на «повтори/дальше/
        # отмена», решает человек. «Закрыть» попап — рутина, его можно
        from app.features.computer_control import ComputerControlManager
        hay = " ".join(str(item.get(k) or "") for k in ("text", "aria", "title"))
        risk = ComputerControlManager.risky_label(act) or (
            "payment" if _is_payment(hay) else None)
        # Подпись в лог — тем же правилом, что у резолвера: на приватной
        # странице только номер (лог виден в /api/logs)
        lbl_fn = getattr(self.cc, "_label_for_log", None)
        lbl = (lbl_fn(act["element"], url, host, idx=act["idx"])
               if callable(lbl_fn) else f"#{act['idx']}")
        if risk:
            logger.info(f"[Scenarios] LLM-восстановление: «{lbl}» "
                        f"рискованный ({risk}) — не жму, жду человека")
            return False
        goal_lbl = (lbl_fn(goal, url, host) if callable(lbl_fn)
                    else f"({len(str(goal))} симв.)")
        logger.info(f"[Scenarios] LLM-восстановление: жму «{lbl}» "
                    f"(idx {act['idx']}) ради «{goal_lbl}»")
        ok, detail = self.cc.execute(act, chat_id, router=router)
        # uncertain тоже годится: JS-меню открывается без видимого эффекта
        # для closed-loop — исходный шаг снаружи покажет, помогло ли
        return ok or "не уверен" in str(detail)

    def _exec_step_once(self, step: dict, run: dict, chat_id, router
                        ) -> Tuple[bool, Optional[str], Optional[str]]:
        op = step["op"]
        slots = run["slots"]
        try:
            act = None
            if op == "open":
                act = {"kind": "url", "value": step["url"]}
            elif op == "click":
                target = self._subst(step.get("target"), slots)
                # Сценарий работает на отслеживаемой вкладке — это общее
                # правило адресации (окно браузера живёт само по себе,
                # за взглядом пользователя ничего не переключаем)
                act, err = self.cc.resolve_click(
                    target, step.get("host") or None, router,
                    chat_id=str(chat_id))
                if act is None:
                    return False, err or self._t("scenario_err_no_target",
                                                 target=target), None
            elif op == "type":
                value = self._subst(step.get("value"), slots)
                field = self._subst(step.get("field"), slots)
                act, err = self.cc.resolve_type(
                    f"{value} в поле {field}".strip(),
                    step.get("host") or None, router, chat_id=str(chat_id))
                if act is None:
                    return False, err or self._t("scenario_err_no_field",
                                                 field=field), None
            elif op == "send":
                act = {"kind": "send", "host": step.get("host") or None}
            else:
                return False, self._t("scenario_err_unknown_step", op=op), None
            act["origin"] = "scenario"  # источник для аудита
            if self._gate_pause(act, step, run):
                return False, None, None
            return self._run_act(act, run, chat_id, router)
        except Exception as e:
            logger.info(f"[Scenarios] Шаг {op} упал: {e}")
            return False, str(e)[:120], None

    def _slot_typed(self, step: Optional[dict], run: dict) -> bool:
        # Значение ввода — целиком ответ человека на вопрос ЭТОГО прогона
        if not step or step.get("op") != "type":
            return False
        m = re.fullmatch(r"\s*\{([^\s{}]+)\}\s*", str(step.get("value") or ""))
        return bool(m) and m.group(1) in (run.get("slots") or {})

    def _gate_pause(self, act: dict, step: Optional[dict], run: dict,
                    reason: Optional[str] = None) -> bool:
        """Гейт подтверждения для шага сценария (та же причина, что у
        execute: ComputerControlManager.confirm_reason). Оплата — handoff
        человеку (run["handoff"]); оформление/отправка/удаление, непроверенная
        подпись, клик по точке — пауза с вопросом «да/нет» (run["confirm"]).
        Ввод в чувствительное поле значения, которое человек сам дал на вопрос
        этого прогона, — уже подтверждён им. True — шаг не исполнять."""
        from app.features import cc_texts
        from app.features.computer_control import ComputerControlManager as _C
        reason = reason or _C.confirm_reason(act)
        if not reason or _C.is_confirmed(act):
            return False
        label = str(act.get("element") or act.get("aria")
                    or act.get("title") or "")[:80]
        if reason == "payment":
            logger.info(f"[Scenarios] «{run['name']}»: шаг «{label[:40]}» — "
                        "оплата, передаю человеку")
            run["handoff"] = self._t("scenario_payment_handoff", label=label)
            return True
        if reason == "sensitive_field" and self._slot_typed(step, run):
            self._grant(act, "scenario_slot", run.get("user_id"))
            return False
        lang = self._lang()
        safe = getattr(self.cc, "_describe_safe", None)
        try:
            what = (safe(act, lang=lang)
                    if act.get("kind") == "type" and callable(safe)
                    else self.cc.describe(act, lang=lang))
        except TypeError:
            what = self.cc.describe(act)
        run["confirm"] = {
            "act": act, "ts": time.time(), "reason": reason,
            "user_id": run.get("user_id") or self._requester(
                run.get("chat_id")),
            "question": self._t("scenario_step_confirm", what=what,
                                risk=cc_texts.gate_risk(reason, lang))}
        logger.info(f"[Scenarios] «{run['name']}»: шаг ждёт «да» ({reason})")
        return True

    def _run_act(self, act: dict, run: dict, chat_id, router
                 ) -> Tuple[bool, Optional[str], Optional[str]]:
        # Исполнение действия шага → (ok, ошибка|None, реплика|None)
        ok, detail = self.cc.execute(act, chat_id, router=router)
        if not ok and isinstance(act.get("confirm_required"), dict):
            # Гейт execute отказал (шаг не спросил) — та же пауза/handoff
            reason = act["confirm_required"].get("reason")
            act.pop("confirm_required", None)
            if self._gate_pause(act, None, run, reason=reason):
                return False, None, None
        if not ok:
            # «Не уверен, что сработало» (closed-loop не увидел эффекта):
            # у JS-меню/бургеров это частый ложный провал — клик реально
            # открыл меню, просто DOM-эвристика его не засекла. В сценарии
            # это не остановка: идём дальше, следующий шаг сам проверит
            # состояние страницы (не найдёт элемент — честный сбой там).
            if "не уверен" in str(detail):
                done = self._describe_done(act)
                return True, None, self._t(
                    "scenario_step_unsure", done=done[0].upper() + done[1:])
            return False, self._t("scenario_err_failed",
                                  detail=str(detail).rstrip(".")), None
        done = self._describe_done(act)
        return True, None, done[0].upper() + done[1:] + "."

    def _advance(self, run: dict, chat_id, router) -> List[str]:
        """Исполняет шаги от текущего pos до ближайшей паузы (ask/сбой/«да»)
        или финала (handoff/конец/«стоп»). Возвращает строки реплик."""
        lines: List[str] = []
        steps = run["steps"]
        run["chat_id"] = chat_id
        run["advancing"] = True

        def _stopped() -> List[str]:
            # «стоп» между шагами: прогон снимается (pos в конец), кликов
            # после «стоп» нет
            logger.info(f"[Scenarios] «{run['name']}» остановлен по «стоп»")
            run["confirm"] = None
            run.pop("confirmed_act", None)
            run["pos"] = len(steps)
            lines.append(self._t("scenario_stopped", name=run["name"]))
            return lines

        try:
            while run["pos"] < len(steps):
                if self._stop_hit(run, chat_id):
                    return _stopped()
                step = steps[run["pos"]]
                op = step["op"]
                if op == "ask":
                    run["awaiting"] = step
                    run["pos"] += 1
                    lines.append(step["question"])
                    return lines
                if op == "handoff":
                    run["pos"] = len(steps)
                    lines.append(step["message"])
                    lines.append(self._t("scenario_finished", name=run["name"]))
                    return lines
                act0 = run.pop("confirmed_act", None)
                if act0 is not None:
                    # Шаг, подтверждённый «да»: ровно то действие, что было в
                    # вопросе (с токеном), без нового резолва
                    ok, err, done = self._run_act(act0, run, chat_id, router)
                else:
                    ok, err, done = self._exec_step(step, run, chat_id, router)
                if self._stop_hit(run, chat_id):
                    if ok:
                        lines.append(done or self._t("scenario_step_ok"))
                    return _stopped()
                if run.get("handoff"):
                    run["pos"] = len(steps)
                    lines.append(run.pop("handoff"))
                    lines.append(self._t("scenario_finished", name=run["name"]))
                    return lines
                if run.get("confirm"):
                    lines.append(run["confirm"]["question"])
                    return lines
                if not ok:
                    run["failed"] = True
                    lines.append(self._t("scenario_step_failed",
                                         err=str(err or "").rstrip(".")))
                    return lines
                lines.append(done or self._t("scenario_step_ok"))
                run["pos"] += 1
            lines.append(self._t("scenario_finished", name=run["name"]))
            return lines
        finally:
            run["advancing"] = False
            run.pop("stop", None)

    def _describe_done(self, act: dict) -> str:
        # describe_done на языке хода; фейковый cc тестов без lang= — как было
        try:
            return self.cc.describe_done(act, lang=self._lang())
        except TypeError:
            return self.cc.describe_done(act)

    # ── Автопредложение записи ─────────────────────────────

    def _trace_known(self, trace: List[dict]) -> bool:
        # Такая последовательность шагов уже записана в сценарий?
        kind_map = {"url": "open", "click": "click", "type": "type",
                    "send": "send"}
        sig = [(kind_map.get(r.get("kind")),
                _norm(r.get("element") or r.get("value") or ""))
               for r in trace]
        with self._lock:
            scenarios_snapshot = list(self._scenarios.values())
        for sc in scenarios_snapshot:
            sc_sig = [(s["op"], _norm(s.get("target") or s.get("url") or ""))
                      for s in sc["steps"] if s["op"] in kind_map.values()]
            if sc_sig and sc_sig == sig:
                return True
        return False

    def maybe_offer(self, chat_id, user_input: str) -> Optional[str]:
        """Строка-предложение записать сюжет или None. Условия: закрывающая
        реплика, в окне ≥3 действий, такой сценарий ещё не записан, на это
        окно ещё не предлагали."""
        if not chat_id or self.cc is None:
            return None
        if self.recording(chat_id):
            # Идёт явная запись — пользователь уже знает про сценарии
            return None
        msg = str(user_input or "").strip()
        if len(msg) > 60 or not _CLOSE_RE.match(msg):
            return None
        try:
            trace = self._trace(chat_id)
        except Exception:
            return None
        if len(trace) < MIN_TRACE_ACTIONS:
            return None
        marker = float(trace[-1].get("ts") or 0)
        try:
            if self._trace_known(trace):
                return None
        except Exception:
            pass
        # Проверка «на это окно уже предлагали?» и отметка — одной операцией:
        # иначе две реплики «спасибо» подряд из разных потоков дадут два
        # предложения на одно и то же окно трассы
        with self._lock:
            if self._offered.get(str(chat_id), 0) >= marker:
                return None
            self._offered[str(chat_id)] = marker
        return self._phrase(
            "scenario_offer",
            "Кстати, у нас вышел целый сюжет — могу запомнить его как "
            "сценарий и в следующий раз пройти сам. Скажи «запомни "
            "сценарий …» и название.")

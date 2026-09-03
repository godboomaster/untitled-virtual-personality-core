"""
Сценарии (playbooks) — запись и воспроизведение цепочек действий на
компьютере пользователя. Надстройка над ComputerControlManager.

Идея: пользователь один раз вручную проводит бота по «сюжету» (открой додо →
выбери пиццу → в корзину → оформи), говорит «запомни сценарий заказ пиццы» —
и дальше фраза «закажи пиццу» запускает весь путь автоматически. Бот сам
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

logger = logging.getLogger(__name__)

# Окно трассы для записи: «сюжет» — это действия за последние полчаса
TRACE_WINDOW_SEC = 1800
# Минимум действий в окне, чтобы было что записывать/предлагать
MIN_TRACE_ACTIONS = 3
# Какие действия аудита попадают в сценарий (read/scroll/скачивание — бытовые,
# не часть воспроизводимого сюжета; multi/nav в трассе не расчленяются)
_TRACE_KINDS = ("url", "click", "type", "send")

# Оплата — граница сценария: эти шаги отрезаются и заменяются handoff
_PAYMENT_RE = re.compile(
    r"оплат|карт[аоые]|visa|mastercard|\bpay\b|apple\s?pay|google\s?pay|"
    r"сбербанк|тинькофф|\bмир\b", re.IGNORECASE)

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
# «отмени запись» — снять запись без сохранения
_STOP_REC_RE = re.compile(
    r"^\s*(?:отмени|останови|прекрати)\s+(?:запись|записывание)"
    r"(?:\s+сценари[йя])?\s*[.!…]*\s*$", re.IGNORECASE)
# Отмена активного прогона (проверяется только когда прогон идёт)
_CANCEL_RE = re.compile(
    r"^\s*(?:отмена|отмени|стоп\s+сценарий|отмени\s+сценарий|хватит|"
    r"прекрати|не\s+надо|забудь|выход|выйди|брось|отстань)\s*[.!…]*\s*$",
    re.IGNORECASE)
# Отрицательный ответ на опциональный вопрос («что-то ещё?» — «нет»)
_NO_RE = re.compile(
    r"^\s*(?:нет|не|ничего|не\s+надо|вс[её]|хватит|достаточно|пропусти|"
    r"пропустить|no|nope)\s*[.!…]*\s*$", re.IGNORECASE)
# Управление после сбоя шага
_RETRY_RE = re.compile(r"^\s*(?:повтори|ещ[её]\s+раз|retry)\s*[.!…]*\s*$",
                       re.IGNORECASE)
_SKIP_RE = re.compile(r"^\s*(?:дальше|пропусти|скип|skip)\s*[.!…]*\s*$",
                      re.IGNORECASE)
# Закрывающая реплика для автопредложения записи
_CLOSE_RE = re.compile(
    r"^\s*(?:вс[её]|спасибо|готово|отлично|супер|класс|благодарю|ладно|"
    r"здорово|ок(?:ей)?|ok(?:ay)?)\b", re.IGNORECASE)

_SLOT_RE = re.compile(r"\{([^\s{}]+)\}")

# Органы управления страницей — в список кандидатов для LLM-восстановления
# попадают принудительно (именно они открывают скрытые разделы)
_CTL_RE = re.compile(
    r"меню|menu|бургер|burger|закрыт|close|войти|кабинет|назад|главн|home",
    re.IGNORECASE)


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


class ScenarioManager:
    """Хранилище сценариев, запись из трассы и state machine воспроизведения."""

    def __init__(self, context: str = "default", computer_control=None,
                 base_dir: Optional[Path] = None):
        self.context = context
        self.cc = computer_control
        self.base_dir = base_dir or Path(f"data/{context}")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.base_dir / "scenarios.json"
        self._lock = threading.Lock()
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
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items()
                        if isinstance(v, dict) and isinstance(v.get("steps"), list)}
        except Exception:
            pass
        return {}

    def _save(self):
        try:
            self._file.write_text(
                json.dumps(self._scenarios, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            logger.warning(f"[Scenarios] scenarios.json не записан: {e}")

    def list_names(self) -> List[str]:
        return sorted(self._scenarios)

    # ── Парсеры команд ─────────────────────────────────────

    @staticmethod
    def parse_save_request(text: str) -> Optional[str]:
        """«запомни сценарий (как) X» → имя (может быть пустым — спросим)."""
        m = _SAVE_RE.match(str(text or ""))
        if not m:
            return None
        return m.group(1).strip()

    @staticmethod
    def parse_start_record(text: str) -> Optional[str]:
        """«начни записывать сценарий (заказ пиццы)» → имя или "" (без имени).
        None — не команда записи."""
        if not text or len(text) > 60:
            return None
        m = _START_REC_RE.match(str(text))
        if not m:
            return None
        return m.group(1).strip()

    @staticmethod
    def parse_stop_record(text: str) -> bool:
        return bool(_STOP_REC_RE.match(str(text or "")))

    # ── Явная запись (скобки «начни записывать»…«сохрани») ──

    def recording(self, chat_id) -> bool:
        return str(chat_id) in self._recording

    def record_start(self, chat_id, name: str = "") -> str:
        chat_id = str(chat_id)
        rec = self._recording.get(chat_id)
        if rec is not None:
            since = time.strftime("%H:%M", time.localtime(rec["since"]))
            return (f"Уже записываю (с {since}). Когда закончишь — скажи "
                    "«сохрани сценарий», передумал — «отмени запись».")
        self._recording[chat_id] = {"since": time.time(),
                                    "name": str(name or "")}
        logger.info(f"[Scenarios] Запись началась (chat {chat_id}, "
                    f"имя {name!r})")
        return ("Записываю сценарий. Делай действия как обычно — «открой …», "
                "«нажми …», «введи …» — всё пойдёт в запись. Закончить: "
                "«сохрани сценарий» (можно сразу с названием). Отменить: "
                "«отмени запись».")

    def record_stop(self, chat_id) -> str:
        rec = self._recording.pop(str(chat_id), None)
        if rec is None:
            return "Запись не шла — нечего отменять."
        logger.info(f"[Scenarios] Запись отменена (chat {chat_id})")
        return "Запись отменена — ничего не сохранил."

    @staticmethod
    def parse_cancel(text: str) -> bool:
        return bool(_CANCEL_RE.match(str(text or "")))

    def find_scenario(self, text: str) -> Optional[str]:
        """Фраза пользователя → имя сценария. Консервативно: полное покрытие
        имени/алиаса фразой (или совпадение по основам всех слов), чтобы не
        перехватывать обычный диалог."""
        msg = _norm(text)
        if not msg or len(msg) < 3:
            return None
        from app.features.web_search import _stem
        msg_stems = {_stem(w) for w in msg.split() if len(w) >= 3}
        best = None
        for name, sc in self._scenarios.items():
            keys = [name] + [str(a) for a in (sc.get("aliases") or [])]
            for key in keys:
                k = _norm(key)
                if len(k) < 4:
                    continue
                ok = (msg == k or k in msg)
                if not ok and len(k.split()) >= 2:
                    stems = {_stem(w) for w in k.split() if len(w) >= 3}
                    ok = bool(stems) and stems <= msg_stems
                if ok and (best is None or len(k) > len(_norm(best[0]))):
                    best = (key, name)
        return best[1] if best else None

    # ── Запись из аудит-трассы ─────────────────────────────

    def _trace(self, chat_id: str,
               window: int = TRACE_WINDOW_SEC,
               since: Optional[float] = None) -> List[dict]:
        """Хвост audit.jsonl: успешные действия этого чата за окно (или с
        момента since — явная запись «начни записывать сценарий»)."""
        if self.cc is None:
            return []
        path = Path(self.cc.base_dir) / "audit.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        since_ts = float(since) if since else time.time() - window
        out = []
        for line in lines[-400:]:
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
        return out

    @staticmethod
    def _trace_lines(trace: List[dict]) -> str:
        rows = []
        for n, rec in enumerate(trace, 1):
            kind, host = rec.get("kind"), rec.get("host") or ""
            if kind == "url":
                rows.append(f"{n}. открыть {rec.get('value')}")
            elif kind == "click":
                rows.append(f"{n}. нажать «{rec.get('element') or '?'}» на {host}")
            elif kind == "type":
                rows.append(f"{n}. ввести «{rec.get('text') or ''}» в поле "
                            f"«{rec.get('element') or '?'}» на {host}")
            elif kind == "send":
                rows.append(f"{n}. отправить (Enter) на {host}")
        return "\n".join(rows)

    def _llm_generalize(self, trace: List[dict], name: str,
                        router) -> Optional[dict]:
        """LLM обобщает трассу в шаги со слотами. None — не удалось (фолбэк
        на rule-based). Ответ строго JSON, валидация схемы обязательна."""
        if router is None or not trace:
            return None
        prompt = (
            f"По цепочке действий на компьютере собери повторно используемый "
            f"сценарий «{name}».\n\nДействия:\n{self._trace_lines(trace)}\n\n"
            "Ответь ТОЛЬКО JSON (без пояснений и markdown):\n"
            '{"aliases": ["..."], "steps": [...]}\n\n'
            "Формат шагов:\n"
            '- {"op":"open","url":"..."} — открыть сайт\n'
            '- {"op":"click","target":"текст кнопки/ссылки","host":"..."} — клик\n'
            '- {"op":"type","field":"подпись поля","value":"текст","host":"..."} — ввод\n'
            '- {"op":"send","host":"..."} — отправить (Enter)\n'
            '- {"op":"ask","slot":"имя_слота","question":"вопрос пользователю"} '
            "— спросить; ответ подставляется в следующие шаги как {имя_слота}\n"
            '- {"op":"handoff","message":"..."} — финал: дальше действует человек\n\n'
            "Правила:\n"
            "- КАЖДОЕ действие из списка должно стать шагом (или парой ask+шаг). "
            "Нельзя удалять или объединять действия: два одинаковых клика "
            "(«меню») на разных страницах — это ДВА разных шага, оба обязательны. "
            "Число шагов open/click/type/send должно равняться числу действий "
            "(кроме шагов оплаты — их заменяет handoff).\n"
            "- Значения, которые в следующий раз будут другими (название товара, "
            "адрес, текст), замени на ask-шаг ПЕРЕД шагом использования, а в шаге "
            "подставь {слот}: {\"op\":\"click\",\"target\":\"{pizza}\"}.\n"
            "- Навигационные клики (меню, войти, корзина, оформить, далее) "
            "оставляй буквальными и в исходном порядке.\n"
            "- Шаги оплаты (оплатить, карта, pay) замени одним финальным "
            '{"op":"handoff","message":"Дальше оплата — это за тобой."}.\n'
            "- aliases: 2-4 короткие разговорные фразы-триггера («закажи пиццу»).")
        try:
            resp = router.get_response([{"role": "user", "content": prompt}],
                                       temperature=0.0, max_tokens=900, top_p=0.1)
        except Exception as e:
            logger.debug(f"[Scenarios] LLM-обобщение трассы не удалось: {e}")
            return None
        data = self._extract_json(resp or "")
        if not isinstance(data, dict):
            logger.info(f"[Scenarios] LLM-ответ не JSON: {(resp or '')[:80]!r}")
            return None
        steps = self._validate_steps(data.get("steps"))
        if steps is None:
            logger.info("[Scenarios] LLM-шаги не прошли валидацию")
            return None
        # Страховка от «потерянных» шагов: LLM любит выкинуть «лишние», с его
        # точки зрения, клики (второе «меню» на новой странице, промежуточные
        # экраны) — и сценарий рассыпается при воспроизведении (негде нажать
        # «Расписание», если пропущен клик, открывающий меню). Исполняемых
        # шагов должно быть не меньше, чем действий в трассе (минус шаги
        # оплаты — их заменяет handoff); иначе — rule-based фолбэк.
        pay_in_trace = sum(
            1 for r in trace
            if _PAYMENT_RE.search(str(r.get("element") or "")
                                  + " " + str(r.get("text") or "")))
        n_exec = sum(1 for s in steps
                     if s["op"] in ("open", "click", "type", "send"))
        if n_exec < len(trace) - pay_in_trace:
            logger.info(f"[Scenarios] LLM потеряла шаги: {n_exec} из "
                        f"{len(trace) - pay_in_trace} — фолбэк на rule-based")
            return None
        aliases = [str(a).strip() for a in (data.get("aliases") or [])
                   if str(a).strip()][:5]
        return {"aliases": aliases, "steps": steps}

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        """Первый сбалансированный {...} в ответе (LLM любит ```json-обёртки)."""
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
        раньше использования. None — схема не сошлась."""
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
        """Фолбэк без LLM: действия как есть, каждый ввод текста — вопросом."""
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
        """Всё от первого шага с оплатой отрезается, вместо него — handoff."""
        out = []
        for s in steps:
            hay = " ".join(str(s.get(k) or "")
                           for k in ("target", "field", "value"))
            if s["op"] in ("click", "type") and _PAYMENT_RE.search(hay):
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
            span = "с начала записи" if since else "за последние полчаса"
            return None, (f"Пока нечего записывать: {span} было всего "
                          f"{len(trace)} действий на страницах. "
                          "Проведи меня по сюжету — и запишем.")
        built = self._llm_generalize(trace, name, router)
        if built is None:
            built = {"aliases": [], "steps": self._rule_steps(trace)}
            logger.info(f"[Scenarios] «{name}»: rule-based запись "
                        f"({len(built['steps'])} шагов)")
        steps = self._strip_payment(built["steps"])
        if len(steps) < 2:
            return None, ("В трассе слишком мало осмысленных шагов — "
                          "сценарий не собрался.")
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
        rec = self._recording.get(str(chat_id))
        if rec is not None and not name:
            name = str(rec.get("name") or "")
        if not name:
            return ("Как назвать сценарий? Скажи так: «сохрани сценарий "
                    "заказ пиццы».")
        scenario, err = self.build_from_trace(
            chat_id, name, router, since=rec.get("since") if rec else None)
        if err:
            if rec is not None:
                return (err + " Запись продолжается — добавь действий и "
                        "скажи «сохрани сценарий» ещё раз.")
            return err
        self._recording.pop(str(chat_id), None)
        asks = [s["question"] for s in scenario["steps"] if s["op"] == "ask"]
        tail = (" По ходу спрошу: " + " ".join(f"«{q}»" for q in asks) \
                if asks else "")
        return (f"Записал сценарий «{scenario['name']}» — "
                f"{len(scenario['steps'])} шагов.{tail} "
                f"Теперь просто скажи «{scenario['name']}».")

    # ── Воспроизведение (state machine per chat) ───────────

    def active(self, chat_id) -> bool:
        return str(chat_id) in self._runs

    def _subst(self, text: str, slots: Dict[str, str]) -> str:
        return _SLOT_RE.sub(
            lambda m: slots.get(m.group(1), m.group(0)), str(text or ""))

    def start(self, name: str, chat_id, router) -> str:
        """Запуск сценария: первый батч шагов до первой паузы."""
        sc = self._scenarios.get(name)
        if sc is None:
            return f"Сценария «{name}» у меня нет."
        run = {"name": name, "steps": sc["steps"], "pos": 0, "slots": {},
               "awaiting": None, "failed": False}
        self._runs[str(chat_id)] = run
        lines = [f"Погнали — «{name}» ({len(sc['steps'])} шагов). "
                 "Скажи «отмена», если передумаешь."]
        lines += self._advance(run, chat_id, router)
        if run["pos"] >= len(run["steps"]) and not run["awaiting"]:
            self._runs.pop(str(chat_id), None)
        return "\n".join(lines)

    def feed(self, chat_id, user_input: str, router) -> Optional[str]:
        """Ответ пользователя внутри прогона: слот, повтор/пропуск после
        сбоя, либо «не понял». None — «сообщение не наше»: прогон уже снят,
        фраза должна уйти в обычный диалог (антизалипание)."""
        run = self._runs.get(str(chat_id))
        if run is None:
            return None
        msg = str(user_input or "").strip()
        if run.get("awaiting"):
            step = run["awaiting"]
            run["awaiting"] = None
            if step.get("optional") and _NO_RE.match(msg):
                pass  # опциональный слот пропущен
            else:
                run["slots"][step["slot"]] = msg[:200]
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
                    self._runs.pop(str(chat_id), None)
                    logger.info(f"[Scenarios] «{name}» снят: 2 нераспознанных "
                                "сообщения подряд на сбойном шаге")
                    return None
                return ("Стою на сбойном шаге. Скажи «повтори», "
                        "«дальше» (пропустить) или «отмена».")
        else:
            # Прогон ждёт только при awaiting/failed; иначе — не наше
            return None
        lines = self._advance(run, chat_id, router)
        if run["pos"] >= len(run["steps"]) and not run["awaiting"]:
            self._runs.pop(str(chat_id), None)
        return "\n".join(lines) or "Продолжаю."

    def cancel(self, chat_id) -> str:
        run = self._runs.pop(str(chat_id), None)
        return (f"Сценарий «{run['name']}» отменён." if run
                else "Нечего отменять — сценарий не запущен.")

    def _exec_step(self, step: dict, run: dict, chat_id, router
                   ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Один шаг → (ok, ошибка|None, реплика об успехе|None).
        Порядок спасения: 1) повтор после стабилизации DOM (страница
        догружается, оверлей анимируется — ждём событие, а не слепой слип);
        2) LLM-восстановление — модель смотрит живой снапшот
        и выбирает, что нажать, чтобы приблизиться к цели (открыть меню,
        закрыть попап, другое название), затем шаг повторяется; 3) честный
        стоп на «повтори/дальше/отмена»."""
        r = self._exec_step_once(step, run, chat_id, router)
        if r[0] or step["op"] not in ("click", "type"):
            return r
        try:
            from app.features import browser_actions as _ba
            _ba.wait_dom_idle(getattr(self.cc, "_last_host", None),
                              getattr(self.cc, "_last_tab_id", None),
                              timeout_sec=2.5, min_wait=0.8)
        except Exception:
            time.sleep(2)
        r = self._exec_step_once(step, run, chat_id, router)
        if r[0]:
            return r
        rec = self._llm_recover(step, run, chat_id, router)
        if rec == "skip":
            # Шаг устарел (страница уже дальше по сценарию) — считаем пройденным
            return True, None, "Этот шаг уже не нужен — страница ушла вперёд, пропускаю."
        if rec:
            r = self._exec_step_once(step, run, chat_id, router)
        return r

    @staticmethod
    def _step_line(step: dict, slots: Dict[str, str]) -> str:
        """Шаг сценария одной человекочитаемой строкой (для LLM-контекста)."""
        op = step["op"]
        sub = lambda s: _SLOT_RE.sub(
            lambda m: slots.get(m.group(1), m.group(0)), str(s or ""))
        if op == "open":
            return f"открыть {step.get('url')}"
        if op == "click":
            return f"нажать «{sub(step.get('target'))}» на {step.get('host') or 'странице'}"
        if op == "type":
            return (f"ввести «{sub(step.get('value'))[:30]}» в поле "
                    f"«{sub(step.get('field'))}» на {step.get('host') or 'странице'}")
        if op == "send":
            return f"отправить (Enter) на {step.get('host') or 'странице'}"
        if op == "ask":
            return f"спросить у пользователя: «{step.get('question')}»"
        return str(step.get("message") or "передать управление человеку")

    def _llm_recover(self, step: dict, run: dict, chat_id, router) -> bool:
        """Сбой «элемент не найден/не нажался»: спросить LLM по ЖИВОМУ
        снапшоту, что нажать, чтобы приблизиться к цели. LLM выбирает номер
        из реальных элементов страницы (как _choose_element в computer_control)
        — «сыграть» клик она не может, исполняет и проверяет система.
        True — клик восстановления выполнен (исходный шаг повторят снаружи)."""
        if router is None or self.cc is None or step["op"] not in ("click", "type"):
            return False
        try:
            url, host, items, tab_id, err = self.cc._snapshot_for(
                step.get("host") or None, chat_id=str(chat_id))
        except Exception:
            return False
        if not items:
            return False
        goal = self._subst(step.get("target") or step.get("field") or "",
                           run["slots"])
        # Дорожная карта сценария: модель видит, что уже сделано, на каком
        # шаге сломались и что дальше — выбор «что нажать» становится
        # осмысленным («меню» открывает панель, где живёт «Расписание»)
        roadmap = []
        for n, s in enumerate(run["steps"]):
            mark = "✓" if n < run["pos"] else ("✗" if n == run["pos"] else "·")
            roadmap.append(f"{mark} {n + 1}. {self._step_line(s, run['slots'])}")
        roadmap_txt = "\n".join(roadmap)
        # Кандидаты для модели — не «первые 25 сырых» (ими могут оказаться
        # ссылки подвала, как было на ciu), а: топ по релевантности цели
        # (тот же скоринг, что у выбора элемента) + принудительно органы
        # управления страницей (меню/бургер/закрыть/войти) — именно они
        # открывают скрытые разделы, и без них модель слепа
        try:
            scored = self.cc._score_candidates(items, goal)
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
            f"Мы выполняем сценарий «{run['name']}» по шагам "
            "(✓ — уже сделано, ✗ — сломались здесь, · — дальше):\n"
            f"{roadmap_txt}\n\n"
            f"На шаге ✗ нужно "
            + (f"нажать «{goal}»" if step["op"] == "click"
               else f"ввести текст в поле «{goal}»")
            + f", но такого элемента среди видимых на странице {host} нет.\n"
            f"Видимые элементы страницы:\n{lines}\n"
            "Возможно, сначала нужно открыть меню, закрыть всплывающее окно "
            "или элемент называется иначе. Учитывая, что уже сделано и что "
            "должно быть после, ответь ТОЛЬКО номером элемента, который стоит "
            "нажать, чтобы приблизиться к цели шага ✗. "
            "Если шаг ✗ уже не нужен (страница сама ушла дальше по сценарию — "
            "например, после входа нас уже перекинуло на нужный сайт) — "
            "ответь «пропустить». "
            "Если ничего не поможет — ответь «нет».")
        try:
            resp = router.get_response([{"role": "user", "content": prompt}],
                                       temperature=0.0, max_tokens=8, top_p=0.1)
        except Exception as e:
            logger.debug(f"[Scenarios] LLM-восстановление недоступно: {e}")
            return False
        # «пропустить» — шаг устарел: страница сама ушла дальше по сценарию
        # (напр., SSO-вход перекинул на целевой сайт без промежуточного клика)
        if (resp or "").strip().lower().startswith("пропуст"):
            logger.info(f"[Scenarios] LLM-восстановление: шаг «{goal[:40]}» "
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
               "host": host, "value": url}
        if tab_id is not None:
            act["tab_id"] = tab_id
        logger.info(f"[Scenarios] LLM-восстановление: жму «{act['element']}» "
                    f"(idx {act['idx']}) ради «{goal[:40]}»")
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
                act, err = self.cc.resolve_click(
                    target, step.get("host") or None, router, chat_id=str(chat_id))
                if act is None:
                    return False, err or f"не нашёл «{target}» на странице", None
            elif op == "type":
                value = self._subst(step.get("value"), slots)
                field = self._subst(step.get("field"), slots)
                act, err = self.cc.resolve_type(
                    f"{value} в поле {field}".strip(),
                    step.get("host") or None, router, chat_id=str(chat_id))
                if act is None:
                    return False, err or f"не нашёл поле «{field}»", None
            elif op == "send":
                act = {"kind": "send", "host": step.get("host") or None}
            else:
                return False, f"неизвестный шаг «{op}»", None
            ok, detail = self.cc.execute(act, chat_id, router=router)
            if not ok:
                # «Не уверен, что сработало» (closed-loop не увидел эффекта):
                # у JS-меню/бургеров это частый ложный провал — клик реально
                # открыл меню, просто DOM-эвристика его не засекла. В сценарии
                # это не остановка: идём дальше, следующий шаг сам проверит
                # состояние страницы (не найдёт элемент — честный сбой там).
                if "не уверен" in str(detail):
                    done = self.cc.describe_done(act)
                    return True, None, (done[0].upper() + done[1:]
                                        + " (вроде; если нет — скажи).")
                return False, f"не вышло ({detail})", None
            done = self.cc.describe_done(act)
            return True, None, done[0].upper() + done[1:] + "."
        except Exception as e:
            logger.info(f"[Scenarios] Шаг {op} упал: {e}")
            return False, str(e)[:120], None

    def _advance(self, run: dict, chat_id, router) -> List[str]:
        """Исполняет шаги от текущего pos до ближайшей паузы (ask/сбой) или
        финала (handoff/конец). Возвращает строки реплик."""
        lines: List[str] = []
        steps = run["steps"]
        while run["pos"] < len(steps):
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
                lines.append(f"Сценарий «{run['name']}» завершён.")
                return lines
            ok, err, done = self._exec_step(step, run, chat_id, router)
            if not ok:
                run["failed"] = True
                lines.append(f"Стоп: {err}. Скажи «повтори», "
                             "«дальше» (пропустить) или «отмена».")
                return lines
            lines.append(done or "Готово.")
            run["pos"] += 1
        lines.append(f"Сценарий «{run['name']}» завершён.")
        return lines

    # ── Автопредложение записи ─────────────────────────────

    def _trace_known(self, trace: List[dict]) -> bool:
        """Такая последовательность шагов уже записана в сценарий?"""
        kind_map = {"url": "open", "click": "click", "type": "type",
                    "send": "send"}
        sig = [(kind_map.get(r.get("kind")),
                _norm(r.get("element") or r.get("value") or ""))
               for r in trace]
        for sc in self._scenarios.values():
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
        if str(chat_id) in self._recording:
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
        if self._offered.get(str(chat_id), 0) >= marker:
            return None
        try:
            if self._trace_known(trace):
                return None
        except Exception:
            pass
        self._offered[str(chat_id)] = marker
        return ("Кстати, у нас вышел целый сюжет — могу запомнить его как "
                "сценарий и в следующий раз пройти сам. Скажи «запомни "
                "сценарий …» и название.")

"""Режимы персоны по триггерам: несколько YAML у одной персоны.

Файлы <персона>_modes/<режим>.yaml рядом с YAML персоны (в любой папке
персон, как она сама; файл с тем же именем в папке пользователя перекрывает
встроенный). master.yaml и mortal.yaml — режимы по собеседнику
(app/core/persona.py: особому собеседнику и остальным). Любой другой файл —
режим по триггеру:

    name: Коннор-девиант          # как персону зовут в этом режиме (необязательно)
    system_prompt: |              # характер в этом режиме — обязателен
      ...
    short_prompt: ...             # краткое описание для Gemma телефона
    settings:                     # поверх settings персоны
      temperature: 0.9
    triggers:
      names: [девиант, rk800]     # обращение этим именем в начале реплики
      keywords: [ra9, свобод*]    # слово или фраза где угодно; * — начало слова
      exit_keywords: [стоп ra9]   # вернуться к обычному
      mood:                       # настроение персоны (state engine)
        tags: [злой, раздраж*]
        valence_below: -0.4
      hold_minutes: 60            # сколько держится после имени/слова

Персона одна — один BotInstance: переписка, факты (LTM), дневник,
напоминания, дела, инвентарь и features общие для всех режимов. Режим
меняет только характер (system_prompt, short_prompt), settings генерации и
то, как персону зовут в заметке модели.

Какой режим у чата (ключ — как у STM: chat_id, иначе user_id):
  1. Обращение именем режима — в начале реплики или имя, которым позвали
     голосом, — включает его; обычным именем персоны (trigger_words, id,
     name) — снимает режим, включённый по имени.
  2. Ключевое слово режима включает его, exit_keywords — снимают.
  3. Включённый режим держится hold_minutes (нет ключа — пока не сменят,
     0 — одну реплику). «/mode» в чате — включить или снять вручную.
  4. Иначе — первый (по имени файла) режим, чьё условие mood совпало с
     настроением персоны в этом чате сейчас: проверяется на каждый ход и в
     сообщениях, которые персона пишет сама.
  5. Иначе — режимы по собеседнику (master/mortal), иначе общий характер.
"""

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import yaml

from app.core.atomic_io import atomic_write_json, load_json_safe

logger = logging.getLogger(__name__)

# Режимы по собеседнику: выбираются не триггерами (app/core/persona.py)
ADDRESSEE_MODES = ("master", "mortal")

# Имя файла режима — его id: в путях (short_prompt.<режим>.json) и в «/mode»
MODE_ID_RE = re.compile(r"^[\w-]{1,40}$")

# Обращение в начале реплики: «эй, Девиант», «hey Deviant, …» — как
# BotInstance._strip_address
_LEAD = r"\s*(?:(?:hey|hi|ok|okay|so|эй)\b\s*,?\s*)?"

# Режим «на одну реплику» (hold_minutes: 0) живёт до следующей реплики
# пользователя, но не дольше этого — сообщения персоны по своей инициативе
# через час уже обычным характером
ONCE_TTL_SEC = 15 * 60

STATE_FILE = "persona_mode.json"
# Когда в чате какой режим был (ModeLog): по нему ответы другого режима в
# истории помечаются для модели — чтобы не перенимать их тон
LOG_FILE = "persona_mode_log.json"
LOG_MAX = 200  # смен режима на чат


def norm(text) -> str:
    """Для сравнения: строчные, ё → е, пробелы схлопнуты."""
    return re.sub(r"\s+", " ", str(text or "").lower().replace("ё", "е")).strip()


def _phrases(raw) -> Tuple[str, ...]:
    # Строка или список строк → кортеж непустых, в нижнем регистре
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else []
    out: List[str] = []
    for item in items:
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            s = norm(item)
            if s.rstrip("*") and s not in out:
                out.append(s)
    return tuple(out)


def _num(raw) -> Optional[float]:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def phrase_pattern(phrase: str) -> "re.Pattern":
    """Слово или фраза целиком («ra9», «ты свободен»); «*» в конце — начало
    слова («свобод*» — «свобода», «свободен»)."""
    star = phrase.endswith("*")
    body = re.escape(norm(phrase.rstrip("*"))).replace(r"\ ", r"\s+")
    return re.compile(r"(?<!\w)" + body + ("" if star else r"(?!\w)"))


def find_phrase(phrases: Iterable[str], text: str) -> Optional[Tuple[int, int]]:
    """Самое раннее вхождение (позиция, -длина) одной из фраз; None — нет."""
    t = norm(text)
    best = None
    for p in phrases:
        m = phrase_pattern(p).search(t)
        if m:
            rank = (m.start(), -(m.end() - m.start()))
            if best is None or rank < best:
                best = rank
    return best


def name_at_start(text: str, names: Iterable[str]) -> Optional[str]:
    """Каким из имён (длиннейшим) реплика начинается: «Девиант, скажи…»,
    «эй, девиант …». Имя — целым словом: «Девианты …» — не обращение."""
    t = norm(text)
    best = None
    for n in names:
        nn = norm(n)
        if len(nn) < 2:
            continue
        if re.match(_LEAD + re.escape(nn) + r"(?![\w'’-])", t) and (best is None or len(nn) > len(best)):
            best = nn
    return best


@dataclass(frozen=True)
class MoodRule:
    """Условие на настроение персоны: все заданные пороги и (если заданы)
    один из тегов. Настроение — mood из state engine: valence −1..1,
    arousal 0..1, tag — 1–2 слова («злой», «tired»)."""
    tags: Tuple[str, ...] = ()
    valence_below: Optional[float] = None
    valence_above: Optional[float] = None
    arousal_below: Optional[float] = None
    arousal_above: Optional[float] = None

    @classmethod
    def parse(cls, raw) -> Optional["MoodRule"]:
        if isinstance(raw, (str, list, tuple)):
            raw = {"tags": raw}
        if not isinstance(raw, dict):
            return None
        rule = cls(tags=_phrases(raw.get("tags") or raw.get("tag")),
                   valence_below=_num(raw.get("valence_below")),
                   valence_above=_num(raw.get("valence_above")),
                   arousal_below=_num(raw.get("arousal_below")),
                   arousal_above=_num(raw.get("arousal_above")))
        empty = not rule.tags and all(v is None for v in (
            rule.valence_below, rule.valence_above, rule.arousal_below, rule.arousal_above))
        return None if empty else rule

    def matches(self, mood) -> bool:
        if not isinstance(mood, dict):
            return False
        for value, below, above in (
                (_num(mood.get("valence")), self.valence_below, self.valence_above),
                (_num(mood.get("arousal")), self.arousal_below, self.arousal_above)):
            if below is None and above is None:
                continue
            if value is None or (below is not None and not value < below) \
                    or (above is not None and not value > above):
                return False
        if self.tags:
            return find_phrase(self.tags, str(mood.get("tag") or "")) is not None
        return True

    def describe(self) -> dict:
        out = {"tags": list(self.tags)} if self.tags else {}
        for key in ("valence_below", "valence_above", "arousal_below", "arousal_above"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        return out


@dataclass(frozen=True)
class Triggers:
    names: Tuple[str, ...] = ()
    keywords: Tuple[str, ...] = ()
    exit_keywords: Tuple[str, ...] = ()
    mood: Optional[MoodRule] = None
    # None — пока не сменят; 0 — одна реплика; иначе минуты
    hold_minutes: Optional[float] = None

    @classmethod
    def parse(cls, raw) -> "Triggers":
        raw = raw if isinstance(raw, dict) else {}
        hold = _num(raw.get("hold_minutes"))
        return cls(names=tuple(n for n in _phrases(raw.get("names")) if not n.endswith("*")),
                   keywords=_phrases(raw.get("keywords")),
                   exit_keywords=_phrases(raw.get("exit_keywords")),
                   mood=MoodRule.parse(raw.get("mood")),
                   hold_minutes=max(hold, 0.0) if hold is not None else None)

    @property
    def empty(self) -> bool:
        return not (self.names or self.keywords or self.mood)


# ── файлы режимов ──

def _mode_files(name: str, dirs: Iterable[Path]) -> Dict[str, Path]:
    # id режима → файл из первой папки персон, где он есть
    out: Dict[str, Path] = {}
    for d in dirs:
        folder = Path(d) / f"{name}_modes"
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.yaml")):
            if path.stem not in out and MODE_ID_RE.match(path.stem):
                out[path.stem] = path
    return out


def signature(name: str, dirs: Iterable[Path]) -> tuple:
    """Что лежит в папках режимов (файлы и их mtime) — для подхвата правок
    без перезапуска."""
    sig = []
    for mode, path in sorted(_mode_files(name, dirs).items()):
        try:
            sig.append((mode, str(path), path.stat().st_mtime))
        except OSError:
            continue
    return tuple(sig)


def load_modes(name: str, dirs: Iterable[Path]) -> Dict[str, Dict]:
    """Все режимы персоны: id → данные файла. Без system_prompt — режима нет."""
    modes: Dict[str, Dict] = {}
    for mode, path in sorted(_mode_files(name, dirs).items()):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            logger.warning(f"Режим «{mode}» персоны '{name}' не прочитан ({path}): {e}")
            continue
        if isinstance(data, dict) and str(data.get("system_prompt") or "").strip():
            modes[mode] = data
    return modes


def trigger_modes(modes: Dict[str, Dict]) -> Dict[str, Triggers]:
    """Режимы по триггеру (не master/mortal) с их условиями, по имени файла.
    Режим без единого триггера включается только «/mode»."""
    out: Dict[str, Triggers] = {}
    for mode in sorted(modes):
        if mode in ADDRESSEE_MODES:
            continue
        out[mode] = Triggers.parse(modes[mode].get("triggers"))
    return out


def modes_info(modes: Dict[str, Dict]) -> List[dict]:
    """Режимы по триггеру для API (GET /api/personas): голосовой слушатель
    берёт отсюда имена режимов, интерфейс — что включает режим."""
    out = []
    for mode, trig in trigger_modes(modes).items():
        data = modes[mode]
        out.append({
            "id": mode,
            "name": str(data.get("name") or mode),
            "description": str(data.get("description") or ""),
            "names": list(trig.names),
            "keywords": list(trig.keywords),
            "exit_keywords": list(trig.exit_keywords),
            "mood": trig.mood.describe() if trig.mood else None,
            "hold_minutes": trig.hold_minutes,
        })
    return out


# ── выбор режима ──

def _expired(active: dict, now: float) -> bool:
    until = _num(active.get("until"))
    return until is not None and now >= until


def _activate(mode: str, via: str, trig: Triggers, now: float, prev: Optional[dict]) -> dict:
    since = prev.get("since") if prev and prev.get("mode") == mode else now
    entry = {"mode": mode, "via": via, "since": since}
    if trig.hold_minutes == 0:
        entry.update(once=True, until=now + ONCE_TTL_SEC)
    elif trig.hold_minutes is not None:
        entry["until"] = now + trig.hold_minutes * 60
    return entry


def decide(triggers: Dict[str, Triggers], active: Optional[dict], text: str,
           called_as: Optional[str] = None, base_names: Iterable[str] = (),
           now: Optional[float] = None) -> Optional[dict]:
    """Включённый режим чата после реплики пользователя → новая запись
    {mode, via: name|keyword|manual, since, until?, once?} или None.

    active — запись до реплики (истёкшая и «на одну реплику» уже не в счёт);
    called_as — имя, которым персону позвали голосом (в тексте его нет);
    base_names — обычные имена персоны: обращение ими снимает режим,
    включённый по имени. Имя режима, совпавшее с обычным, — за режимом."""
    now = time.time() if now is None else now
    if active and (active.get("once") or _expired(active, now)
                   or (active.get("mode") and active.get("mode") not in triggers)):
        active = None
    by_name = {n: mode for mode, t in triggers.items() for n in t.names}
    base = {norm(n) for n in base_names if n and len(norm(n)) >= 2} - set(by_name)

    hit: Optional[str] = None      # id режима, "" — обычное имя
    called = norm(called_as) if called_as else ""
    if called:
        hit = by_name.get(called, "" if called in base else None)
    if hit is None:
        named = name_at_start(text, list(by_name) + sorted(base))
        if named is not None:
            hit = by_name.get(named, "")

    new = active
    if hit:
        return _activate(hit, "name", triggers[hit], now, active)
    if hit == "" and active and active.get("via") == "name":
        new = None

    exited = None
    if new and new.get("mode") and find_phrase(triggers[new["mode"]].exit_keywords, text):
        exited, new = new["mode"], None
    best = None
    for mode, t in triggers.items():
        if mode == exited or not t.keywords:
            continue
        rank = find_phrase(t.keywords, text)
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, mode)
    if best is not None:
        return _activate(best[1], "keyword", triggers[best[1]], now, new)
    return new


def mood_mode(triggers: Dict[str, Triggers], mood) -> Optional[str]:
    # Первый по имени файла режим, чьё условие на настроение совпало
    for mode, t in triggers.items():
        if t.mood is not None and t.mood.matches(mood):
            return mode
    return None


def effective(triggers: Dict[str, Triggers], active: Optional[dict], mood,
              now: Optional[float] = None) -> Optional[str]:
    """Режим чата сейчас: включённый (по имени, слову, вручную), иначе по
    настроению. «/mode off» (mode="") — обычный, и настроение не решает."""
    now = time.time() if now is None else now
    if active and not _expired(active, now):
        mode = active.get("mode")
        if mode == "" and active.get("via") == "manual":
            return None
        if mode in triggers:
            return mode
    return mood_mode(triggers, mood)


class ModeState:
    """Включённые режимы по чатам: {ключ чата: {"active": {...}, "last": режим
    прошлого хода}} в data/<контекст>/persona_mode.json. Без пути — в памяти
    (заготовки в тестах)."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path
        self._lock = threading.Lock()
        data = load_json_safe(path, {}, label="PersonaMode") if path else {}
        self._data: Dict[str, dict] = data if isinstance(data, dict) else {}

    def get(self, key: str) -> dict:
        with self._lock:
            entry = self._data.get(str(key))
            return dict(entry) if isinstance(entry, dict) else {}

    def put(self, key: str, entry: dict) -> None:
        with self._lock:
            key = str(key)
            if (self._data.get(key) or {}) == entry:
                return
            if entry:
                self._data[key] = entry
            else:
                self._data.pop(key, None)
            if self._path is None:
                return
            try:
                atomic_write_json(self._path, self._data)
            except Exception as e:
                logger.warning(f"[PersonaMode] Режим не сохранён ({self._path}): {e}")


def state_path(data_root: Path, context: str) -> Path:
    return Path(data_root) / context / STATE_FILE


def log_path(data_root: Path, context: str) -> Path:
    return Path(data_root) / context / LOG_FILE


class ModeLog:
    """С какого момента какой режим был в чате: {ключ чата: [[время, режим],
    …]} ("" — обычный характер) в data/<контекст>/persona_mode_log.json.
    Запись — когда персона отвечает в другом режиме, чем по последней
    записи; по ней видно, в каком режиме написан ответ в истории (mode_at).
    Без пути — в памяти."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path
        self._lock = threading.Lock()
        data = load_json_safe(path, {}, label="PersonaModeLog") if path else {}
        self._data: Dict[str, list] = {}
        if isinstance(data, dict):
            for key, items in data.items():
                rows = [[float(t), str(m)] for t, m in (items if isinstance(items, list) else [])
                        if isinstance(t, (int, float)) and isinstance(m, str)]
                if rows:
                    self._data[str(key)] = sorted(rows)[-LOG_MAX:]

    def note(self, key, mode: Optional[str], ts: Optional[float] = None) -> None:
        """В чате key сейчас (или с ts) режим mode (None/"" — обычный)."""
        if key is None:
            return
        key, mode = str(key), str(mode or "")
        ts = time.time() if ts is None else float(ts)
        with self._lock:
            rows = self._data.setdefault(key, [])
            before = [r for r in rows if r[0] <= ts]
            if before and before[-1][1] == mode:
                return
            rows.append([ts, mode])
            rows.sort()
            del rows[:-LOG_MAX]
            if self._path is None:
                return
            try:
                atomic_write_json(self._path, self._data)
            except Exception as e:
                logger.warning(f"[PersonaMode] Журнал режимов не сохранён ({self._path}): {e}")

    def rows(self, key, limit: int = LOG_MAX) -> List[list]:
        """Последние записи чата key: [[время, режим], …] по времени."""
        if key is None:
            return []
        with self._lock:
            return [list(r) for r in (self._data.get(str(key)) or [])[-limit:]]

    def mode_at(self, key, ts) -> Optional[str]:
        """Режим чата key в момент ts ("" — обычный); раньше первой записи
        или без записей — None (не известно)."""
        if key is None or not isinstance(ts, (int, float)):
            return None
        with self._lock:
            rows = self._data.get(str(key)) or []
            found = None
            for t, m in rows:
                if t <= ts:
                    found = m
                else:
                    break
            return found

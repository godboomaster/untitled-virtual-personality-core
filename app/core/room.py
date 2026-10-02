"""Комната персоны (веб-раздел «Комната»): где персона «находится» сейчас.

Эффект присутствия: персона всегда рядом, не обязательно разговаривая.
Здесь — всё, что нужно обеим сторонам без FastAPI:

  * конфиг комнаты из YAML персоны (top-level ключ ``room:``) с общим
    дефолтом, если ключа нет;
  * места (spots): встроенные desk/window/shelf/bed/chair/floor + away
    («нет в комнате») + места вокруг предметов инвентаря ``item:<имя>``;
  * вывод места по ключевым словам pastime/location и санитайзер
    spot/pose/pastime_since для тика StateEngine;
  * хранилище размещений предметов (пишет ТОЛЬКО процесс-владелец
    LivingPersona контекста) и сигналы комнаты между процессами
    (API дописывает jsonl, владелец читает по своему курсору);
  * чтение JSON/YAML с кешем по mtime — /room опрашивается раз в минуту,
    файлы почти всегда не меняются.

Контексты: веб — ``api_<persona>`` (чат web_user), Telegram — ``<persona>``.
API и Telegram — разные процессы: всё общение между ними только через файлы.
"""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from app.core.atomic_io import atomic_write_json, file_lock
from app.core.config import get_db_paths
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

# ── Словарь ────────────────────────────────────────────────────────────

BUILTIN_SPOTS = ("desk", "window", "shelf", "bed", "chair", "floor", "away")
POSES = ("stand", "sit", "read", "write", "look", "sleep", "away")
ZONES = ("desk", "shelf", "window", "floor", "wall", "bed")
ITEM_PREFIX = "item:"

# Поза по умолчанию для встроенного места (если в YAML не задана)
SPOT_DEFAULT_POSE = {
    "desk": "sit", "window": "look", "shelf": "read", "bed": "sleep",
    "chair": "sit", "floor": "sit", "away": "away",
}

DEFAULT_PROPS = ["rug", "clock", "curtains", "shelf", "desk", "chair", "bed",
                 "lamp", "plant"]

# Общий дефолт мест: у персоны без room: в YAML
_GENERIC_SPOTS = {
    "desk": {"place": "за столом", "label": "занят делами за столом", "pose": "sit",
             "keywords": ["стол", "пиш", "запис", "дневник", "письм", "работа",
                          "компьют", "ноутбук", "терминал", "код", "черт",
                          "документ", "отчёт", "отчет", "сортир"]},
    "window": {"place": "у окна", "label": "смотрит в окно", "pose": "look",
               "keywords": ["окн", "окош", "дожд", "небо", "неба", "звёзд", "звезд",
                            "туман", "рассвет", "закат", "снег"]},
    "shelf": {"place": "у полки", "label": "перебирает книги", "pose": "read",
              "keywords": ["книг", "полк", "чита", "читает", "перелист", "том",
                           "библиот", "свит"]},
    "bed": {"place": "в кровати", "label": "отдыхает", "pose": "sleep",
            "keywords": ["спит", "спать", "сон", "сплю", "засып", "усн", "дрем",
                         "дрём", "кроват", "постел", "лежит", "прилёг", "прилег"]},
    "chair": {"place": "в кресле", "label": "сидит в кресле", "pose": "sit",
              "keywords": ["кресл", "стул", "сидит"]},
    "floor": {"place": "на полу", "label": "сидит на полу", "pose": "sit",
              "keywords": ["полу", "ковр", "ковёр"]},
}
_DEFAULT_SPOT_KEYS = ("desk", "window", "shelf", "bed")

# Английские основы для встроенных мест: pastime/location пишутся на языке
# пользователя, а ключевые слова YAML персон — обычно только русские.
# Добавляются к ключевым словам места всегда (и к своим из YAML)
_SPOT_KEYWORDS_EN = {
    "desk": ["desk", "table", "writ", "note", "journal", "diary", "letter",
             "working", "comput", "laptop", "terminal", "code", "coding", "draw",
             "sketch", "document", "report", "sort", "typing"],
    "window": ["window", "rain", "sky", "stars", "fog", "mist", "sunrise",
               "sunset", "dawn", "snow", "outside the window"],
    "shelf": ["book", "shelf", "shelv", "read", "leaf", "flip", "tome",
              "librar", "scroll"],
    "bed": ["sleep", "asleep", "nap", "doz", "slumber", "bed", "lying",
            "lies down"],
    "chair": ["armchair", "chair", "sitting", "seated"],
    "floor": ["floor", "carpet", "rug"],
}

# away — всегда неявно разрешено: персоны нет в комнате (сцена пустая)
AWAY_SPOT = {
    "key": "away", "place": "не дома", "label": "вне комнаты", "pose": "away",
    "item": None,
    "keywords": ["ушёл", "ушел", "ушла", "вышел", "вышла", "гуля", "прогул",
                 "в магазин", "в городе", "в пути", "в дороге", "на работе",
                 "на улице", "нет дома", "не дома", "снаружи", "отсутств",
                 "уехал", "уехала", "в гостях",
                 "went out", "gone out", "left", "walk", "stroll", "to the store",
                 "shopping", "in town", "in the city", "on the way", "on the road",
                 "at work", "outside", "not home", "away from home", "absent",
                 "travel", "visiting"],
}

MAX_TEXT = 80          # label/place места
MAX_SPOTS = 16         # мест из YAML
SIGNAL_TYPES = ("glance", "focus_start", "focus_end")
SIGNALS_MAX_BYTES = 256 * 1024
SIGNALS_KEEP_LINES = 200
# Сигнал старше — уже не новость (владелец был выключен): не в лог
SIGNAL_MAX_AGE_SEC = 6 * 3600
# Последний выданный ts сигнала в этом процессе (append_signal, под file_lock)
_last_signal_ts = 0.0

_PERSONA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ── Кеш чтения по mtime ────────────────────────────────────────────────

_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 512


def _stat_key(path: Path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def read_json_cached(path, default=None):
    """JSON-файл с кешем по (mtime, size). Нет файла / битый — default.
    Файл чужого процесса только читаем: в отличие от load_json_safe битый
    файл НЕ переименовываем в .corrupt (он может писаться прямо сейчас).
    Возвращает общий закешированный объект — вызывающий его не мутирует."""
    path = Path(path)
    key = _stat_key(path)
    if key is None:
        return default
    skey = str(path)
    with _cache_lock:
        hit = _cache.get(skey)
        if hit is not None and hit[0] == key:
            return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(f"[Room] {path} не прочитан: {e}")
        return default
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[skey] = (key, data)
    return data


def _read_yaml_cached(path: Path) -> Optional[dict]:
    key = _stat_key(path)
    if key is None:
        return None
    skey = "yaml:" + str(path)
    with _cache_lock:
        hit = _cache.get(skey)
        if hit is not None and hit[0] == key:
            return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.debug(f"[Room] YAML {path} не прочитан: {e}")
        return None
    if not isinstance(data, dict):
        data = {}
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[skey] = (key, data)
    return data


def load_persona_data(persona: str) -> Optional[dict]:
    """YAML персоны (весь dict) с кешем по mtime; None — файла нет."""
    if not _PERSONA_ID_RE.match(str(persona or "")):
        return None
    from app.core.addons import find_persona_file
    path = find_persona_file(persona)
    if path is None:
        return None
    return _read_yaml_cached(path)


# ── Пути ───────────────────────────────────────────────────────────────

def living_dir(context: str) -> Path:
    # Та же папка, что у StateEngine/WorldEngine: data/<context>/living
    return Path(get_db_paths(context)["stm"]).parent / "living"


def persona_room_dir(persona: str) -> Optional[Path]:
    """data/api_<persona>/room — UI-данные комнаты уровня персоны (пишет
    только API-процесс). None — недопустимый id."""
    if not _PERSONA_ID_RE.match(str(persona or "")):
        return None
    return data_dir() / f"api_{persona}" / "room"


def placements_path(context: str) -> Path:
    return living_dir(context) / "room_placements.json"


def signals_path(context: str) -> Path:
    return living_dir(context) / "room_signals.jsonl"


def signals_cursor_path(context: str) -> Path:
    return living_dir(context) / "room_signals_cursor.json"


# ── Конфиг комнаты ─────────────────────────────────────────────────────

def _clip(value, limit: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _norm_keywords(raw) -> List[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for k in raw:
        k = str(k or "").strip().lower()
        if k and k not in out:
            out.append(k[:40])
    return out[:24]


def _norm_pose(pose, key: str) -> str:
    pose = str(pose or "").strip().lower()
    if pose in POSES:
        return pose
    return SPOT_DEFAULT_POSE.get(key, "stand")


def _with_en_keywords(key: str, keywords: List[str]) -> List[str]:
    return keywords + [k for k in _SPOT_KEYWORDS_EN.get(key, []) if k not in keywords]


def _normalize_builtin_spot(raw: dict) -> Optional[dict]:
    key = str(raw.get("key") or "").strip().lower()
    if key not in BUILTIN_SPOTS or key == "away":
        return None
    generic = _GENERIC_SPOTS.get(key, {})
    keywords = _norm_keywords(raw.get("keywords"))
    return {
        "key": key,
        "place": _clip(raw.get("place")) or generic.get("place", key),
        "label": _clip(raw.get("label")) or generic.get("label", ""),
        "pose": _norm_pose(raw.get("pose") or generic.get("pose"), key),
        "item": None,
        # Своих ключевых слов нет — общие для этого места
        "keywords": _with_en_keywords(
            key, keywords or list(generic.get("keywords", []))),
    }


def _generic_spot(key: str) -> dict:
    g = _GENERIC_SPOTS[key]
    return {"key": key, "place": g["place"], "label": g["label"],
            "pose": g["pose"], "item": None,
            "keywords": _with_en_keywords(key, list(g["keywords"]))}


def resolve_room_config(persona_data: Optional[dict]) -> dict:
    """Конфиг комнаты из YAML персоны (ключ room:). Нет ключа — общий
    дефолт с is_default: true. Места вокруг предметов сюда не входят —
    их добавляет merge_item_spots (зависят от контекста)."""
    raw = (persona_data or {}).get("room")
    if not isinstance(raw, dict):
        return {
            "props": list(DEFAULT_PROPS),
            "pet": "none", "pet_label": "", "poster_label": "",
            "spots": [_generic_spot(k) for k in _DEFAULT_SPOT_KEYS],
            "is_default": True,
        }
    props = raw.get("props")
    if isinstance(props, (list, tuple)):
        props = [str(p).strip()[:32] for p in props if str(p or "").strip()][:40]
    else:
        props = list(DEFAULT_PROPS)
    pet = str(raw.get("pet") or "none").strip().lower()
    if pet not in ("cat", "crow", "none"):
        pet = "none"
    spots, seen = [], set()
    for s in (raw.get("spots") or [])[:MAX_SPOTS]:
        if not isinstance(s, dict):
            continue
        spot = _normalize_builtin_spot(s)
        if spot is None or spot["key"] in seen:
            if spot is None:
                logger.debug(f"[Room] Неизвестное место в room.spots: {s.get('key')!r}")
            continue
        seen.add(spot["key"])
        spots.append(spot)
    if not spots:
        spots = [_generic_spot(k) for k in _DEFAULT_SPOT_KEYS]
    return {
        "props": props,
        "pet": pet,
        "pet_label": _clip(raw.get("pet_label"), 60),
        "poster_label": _clip(raw.get("poster_label"), 60),
        "spots": spots,
        "is_default": False,
    }


def item_keywords(name: str) -> List[str]:
    """Ключевые слова места-предмета из его имени: основы слов (≥4 букв —
    без последней буквы, морфология «гитара/гитаре»)."""
    out = []
    for w in re.findall(r"[а-яёa-z0-9]+", str(name or "").lower()):
        if len(w) < 3:
            continue
        stem = w[:-1] if len(w) >= 5 else w
        if stem not in out:
            out.append(stem)
    return out[:6]


def _norm_item_spot(name: str, spot) -> Optional[dict]:
    if not isinstance(spot, dict):
        return None
    label = _clip(spot.get("label"))
    place = _clip(spot.get("place"))
    if not label and not place:
        return None
    return {
        "key": ITEM_PREFIX + name,
        "place": place or f"у предмета «{name[:40]}»",
        "label": label or place,
        "pose": _norm_pose(spot.get("pose"), "chair"),
        "item": name,
        "keywords": item_keywords(name),
    }


def item_spots(placements: Optional[dict], layout: Optional[dict],
               inventory_names: Optional[List[str]] = None) -> List[dict]:
    """Места вокруг предметов: spot из размещений (LLM) + правки раскладки
    (spot из layout главнее, hidden убирает). inventory_names задан —
    только предметы, которые ещё в инвентаре."""
    placements = placements if isinstance(placements, dict) else {}
    items_layout = ((layout or {}).get("items") if isinstance(layout, dict) else None) or {}
    if not isinstance(items_layout, dict):
        items_layout = {}
    alive = None
    if inventory_names is not None:
        alive = {str(n).lower() for n in inventory_names}
    names = list(placements.keys()) + [n for n in items_layout if n not in placements]
    out = []
    for name in names:
        if alive is not None and str(name).lower() not in alive:
            continue
        lay = items_layout.get(name) if isinstance(items_layout.get(name), dict) else {}
        if lay.get("hidden"):
            continue
        if "spot" in lay:
            spot = _norm_item_spot(name, lay.get("spot"))
        else:
            pl = placements.get(name) if isinstance(placements.get(name), dict) else {}
            spot = _norm_item_spot(name, pl.get("spot"))
        if spot:
            out.append(spot)
    return out


def allowed_spots(config: dict, placements: Optional[dict] = None,
                  layout: Optional[dict] = None,
                  inventory_names: Optional[List[str]] = None) -> List[dict]:
    """Полный список допустимых мест: встроенные из конфига + места-предметы
    + away (всегда). Для тика StateEngine."""
    spots = [dict(s) for s in (config or {}).get("spots") or []]
    spots += item_spots(placements, layout, inventory_names)
    spots.append(dict(AWAY_SPOT))
    return spots


def public_spot(spot: dict) -> dict:
    # Место для ответа API: без служебных ключевых слов
    return {k: spot.get(k) for k in ("key", "place", "label", "pose", "item")}


# ── Вывод места и санитайзер тика ──────────────────────────────────────

def _kw_hits(text: str, keywords: List[str]) -> int:
    if not text or not keywords:
        return 0
    low = text.lower()
    words = re.findall(r"[а-яёa-z0-9]+", low)
    hits = 0
    for kw in keywords:
        if " " in kw:
            if kw in low:
                hits += 1
        elif any(w.startswith(kw) for w in words):
            hits += 1
    return hits


def infer_spot(pastime: str, location: str, spots: List[dict]) -> Optional[str]:
    """Место по ключевым словам: совпадения в pastime весят вдвое больше,
    чем в location. Ничья — порядок списка (away в конце). None — ничего."""
    best, best_score = None, 0
    for s in spots or []:
        kws = s.get("keywords") or []
        score = 2 * _kw_hits(pastime, kws) + _kw_hits(location, kws)
        if score > best_score:
            best, best_score = s.get("key"), score
    return best


def _match_spot_key(raw, spots: List[dict]) -> Optional[str]:
    """Ключ места из ответа модели: точный ключ, имя предмета без
    префикса или текст place («у окна»). None — не распознан."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    low = raw.lower()
    for s in spots:
        if s["key"] == raw or s["key"].lower() == low:
            return s["key"]
    for s in spots:
        item = s.get("item")
        if item and (item.lower() == low or (ITEM_PREFIX + item).lower() == low):
            return s["key"]
    for s in spots:
        if s.get("place") and s["place"].lower() == low:
            return s["key"]
    return None


def spot_default_pose(key: str, spots: List[dict]) -> str:
    for s in spots or []:
        if s.get("key") == key:
            return _norm_pose(s.get("pose"), key)
    return SPOT_DEFAULT_POSE.get(key, "stand")


def fallback_spot(spots: List[dict]) -> str:
    keys = [s.get("key") for s in spots or []]
    if "desk" in keys or not keys:
        return "desk"
    return next((k for k in keys if k != "away"), "desk")


def sanitize_spot(new_state: dict, prev: dict, spots: Optional[List[dict]]) -> None:
    """Санитайзер spot/pose нового состояния (мутирует new_state).
    Неизвестный spot → по ключевым словам pastime/location → прежний →
    desk. Неверная поза → поза места по умолчанию. away ⇔ поза away."""
    if not spots:
        # Список мест не передан (старый вызов) — держим прежнее
        if not new_state.get("spot"):
            new_state["spot"] = prev.get("spot") or "desk"
        pose = str(new_state.get("pose") or prev.get("pose") or "").lower()
        new_state["pose"] = pose if pose in POSES else SPOT_DEFAULT_POSE.get(
            new_state["spot"], "stand")
        return
    keys = {s["key"] for s in spots}
    raw_spot = new_state.get("spot")
    key = _match_spot_key(raw_spot, spots) if raw_spot else None
    raw_pose = str(new_state.get("pose") or "").strip().lower()
    prev_spot = prev.get("spot") if prev.get("spot") in keys else None
    if key is None:
        if raw_spot:
            # Модель назвала несуществующее место — её поза к нему не относится
            raw_pose = ""
        same_activity = (new_state.get("pastime") == prev.get("pastime"))
        # Занятие сменилось, а персона спала — проснулась: ни прежняя поза
        # sleep, ни кровать сами по себе не наследуются (иначе после ночи
        # эвристического тика персона «спит» весь день — у дневных занятий
        # эвристики нет ключевых слов места)
        woke = not same_activity and str(prev.get("pose") or "") == "sleep"
        if not raw_spot and same_activity and prev_spot:
            # Занятие то же, место не названо — персона остаётся где была
            key = prev_spot
        else:
            key = infer_spot(new_state.get("pastime", ""),
                             new_state.get("location", ""), spots)
        if key is None:
            key = prev_spot or fallback_spot(spots)
            if woke and key == prev_spot:
                key = fallback_spot(spots)
        if raw_pose not in POSES and key == prev_spot and not woke:
            raw_pose = str(prev.get("pose") or "")
    pose = raw_pose if raw_pose in POSES else spot_default_pose(key, spots)
    if key == "away":
        pose = "away"
    elif pose == "away":
        pose = spot_default_pose(key, spots)
    new_state["spot"] = key
    new_state["pose"] = pose


def spots_prompt_lines(spots: List[dict]) -> str:
    # Список мест для тик-промпта: ключ, где, поза по умолчанию
    lines = []
    for s in spots or []:
        text = s.get("place") or ""
        if s.get("label") and s.get("key") != "away":
            text += f" — {s['label']}"
        lines.append(f"- {s['key']}: {text} (pose: {s.get('pose')})")
    return "\n".join(lines)


# ── Размещения предметов (пишет только владелец контекста) ─────────────

def read_placements(context: str) -> dict:
    data = read_json_cached(placements_path(context), {})
    return data if isinstance(data, dict) else {}


def save_placements(context: str, placements: dict) -> None:
    path = placements_path(context)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, placements)
    except Exception as e:
        logger.warning(f"[Room] Размещения не сохранены ({context}): {e}")


def normalize_placement(data, placed_by: str) -> dict:
    """Ответ модели {place, zone, spot} → запись размещения. place:false —
    предмету не место в комнате (zone null)."""
    data = data if isinstance(data, dict) else {}
    place = data.get("place", True)
    if isinstance(place, str):
        place = place.strip().lower() not in ("false", "no", "0", "нет")
    zone = str(data.get("zone") or "").strip().lower()
    spot = None
    if place:
        if zone not in ZONES:
            zone = "desk"
        raw = data.get("spot")
        if isinstance(raw, dict):
            label = _clip(raw.get("label"), 60)
            where = _clip(raw.get("place"), 60)
            if label or where:
                spot = {"label": label or where, "place": where or label,
                        "pose": _norm_pose(raw.get("pose"), "chair")}
    return {"zone": zone if place else None, "spot": spot,
            "placed_by": placed_by, "at": time.time()}


def heuristic_placement() -> dict:
    return {"zone": "desk", "spot": None, "placed_by": "heuristic", "at": time.time()}


# ── Сигналы комнаты (API дописывает, владелец читает) ──────────────────

def append_signal(context: str, sig_type: str, chat_id: str, text: str) -> dict:
    """Дописать сигнал в data/<context>/living/room_signals.jsonl. Пока
    файл < 256 КБ — только append; больше — атомарно переписываем
    последние 200 строк (курсор владельца — по ts, ротация его не ломает)."""
    global _last_signal_ts
    if sig_type not in SIGNAL_TYPES:
        raise ValueError(f"неизвестный тип сигнала: {sig_type}")
    record = {"ts": 0.0, "type": sig_type, "chat_id": str(chat_id),
              "text": str(text or "")[:500]}
    path = signals_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        # ts — под локом и строго растущий: курсор владельца «ts > курсора»,
        # и сигнал с меньшим/равным ts, дописанный ПОСЛЕ уже прочитанного
        # (гонка потоков API, одинаковый time.time(), шаг часов назад),
        # потерялся бы навсегда. Пишет сигналы только API-процесс —
        # монотонности внутри процесса достаточно
        ts = max(time.time(), _last_signal_ts + 1e-6)
        _last_signal_ts = ts
        record["ts"] = ts
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size >= SIGNALS_MAX_BYTES:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()[-SIGNALS_KEEP_LINES:]
                from app.core.atomic_io import atomic_write_text
                atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
            except Exception as e:
                logger.warning(f"[Room] Ротация сигналов не удалась ({context}): {e}")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_signals_since(context: str, since_ts: float) -> List[dict]:
    path = signals_path(context)
    if not path.is_file():
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = float(rec.get("ts", 0))
                except (ValueError, TypeError, AttributeError):
                    continue
                if ts > since_ts and rec.get("type") in SIGNAL_TYPES:
                    out.append(rec)
    except OSError as e:
        logger.debug(f"[Room] Сигналы не прочитаны ({context}): {e}")
    out.sort(key=lambda r: float(r.get("ts", 0)))
    return out


class SignalConsumer:
    """Сторона владельца контекста: забирает новые сигналы по своему
    курсору (ts последнего взятого) — каждый ровно один раз. Файл не
    перечитывается, пока не изменился его mtime."""

    def __init__(self, context: str):
        self.context = context
        self._seen_stat = None
        self._lock = threading.Lock()

    def _cursor(self) -> float:
        data = read_json_cached(signals_cursor_path(self.context), {})
        try:
            return float((data or {}).get("ts", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def consume(self) -> List[dict]:
        with self._lock:
            key = _stat_key(signals_path(self.context))
            if key is None or key == self._seen_stat:
                return []
            cursor = self._cursor()
            fresh = read_signals_since(self.context, cursor)
            self._seen_stat = key
            if not fresh:
                return []
            new_cursor = max(float(r["ts"]) for r in fresh)
            try:
                atomic_write_json(signals_cursor_path(self.context), {"ts": new_cursor})
            except Exception as e:
                # Курсор не записан — не отдаём сигналы, иначе они придут
                # повторно после рестарта; попробуем на следующем тике
                logger.warning(f"[Room] Курсор сигналов не сохранён: {e}")
                self._seen_stat = None
                return []
            horizon = time.time() - SIGNAL_MAX_AGE_SEC
            return [r for r in fresh if float(r["ts"]) >= horizon]


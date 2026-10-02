"""Комната персоны в вебе: живое присутствие (GET /room) и UI-данные комнаты.

Всё читается из файлов — одинаково для веб-контекста (``api_<persona>``,
чат web_user) и Telegram-контекста (``<persona>``, живущего в ДРУГОМ
процессе): ни одного обращения к in-process ботам, кеш по mtime
(app/core/room.py). Поэтому опрос комнаты раз в минуту дёшев и не создаёт
BotInstance.

Комната НЕ трогает web_presence: присутствие пользователя в чате — забота
вкладки чата; открытая комната не должна морозить фоновую жизнь персоны.

UI-данные уровня персоны — data/api_<persona>/room/ (пишет только
API-процесс): layout.json (раскладка предметов, пресет аватара),
style.json (описание стиля + референс), art.json (спрайты и фон),
focus.json (фокус-сессия «поработать рядом»). Сигналы в LivingPersona
владельца контекста — через room.append_signal (jsonl).
"""

import base64
import logging
import re
import threading
import time
from pathlib import Path

from app.api.avatars_api import _sniff
from app.api.security import safe_join
from app.core import room, timeutil
from app.core.atomic_io import atomic_write_json
from app.core.language import detect_dialogue_language, persona_language, user_language_line
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

WEB_CHAT_ID = "web_user"
IMAGE_MAX_BYTES = 2 * 1024 * 1024       # спрайт/фон/картинка предмета
REFERENCE_MAX_BYTES = 1024 * 1024       # референс стиля
STYLE_MAX_CHARS = 1500
POKE_INTERVAL_SEC = 15 * 60             # клик по персоне → LLM не чаще
FOCUS_DEFAULT_MIN = 25
FOCUS_GRACE_SEC = 15 * 60               # забытая сессия гаснет сама
LAYOUT_MAX_ITEMS = 100
LAYOUT_MAX_FILE = 16 * 1024 * 1024
ART_MAX_FILE = 32 * 1024 * 1024
# Клиентские позы (with_you, glance) — у них тоже могут быть спрайты
SPRITE_POSES = tuple(room.POSES) + ("with_you", "glance")

_DATA_URL_RE = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\s]+)$")
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,64}$")

_write_lock = threading.Lock()
_poke_at: dict[str, float] = {}
_poke_lock = threading.Lock()


class RoomError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ── Пути и чтение ──────────────────────────────────────────────────────

def room_dir(persona: str) -> Path:
    base = safe_join(data_dir(), persona, prefix="api_")
    if base is None:
        raise RoomError(400, "Недопустимый id персоны")
    return base / "room"


def _read(persona: str, name: str, default):
    data = room.read_json_cached(room_dir(persona) / name, None)
    return data if isinstance(data, dict) else default


def _write(persona: str, name: str, data: dict, max_bytes: int | None = None):
    path = room_dir(persona) / name
    if max_bytes is not None:
        import json
        size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        if size > max_bytes:
            raise RoomError(413, f"Данные комнаты больше {max_bytes // (1024 * 1024)} МБ")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)


def _now_iso() -> str:
    return timeutil.now().isoformat(timespec="seconds")


# ── Картинки: data-URL с проверкой сигнатуры ───────────────────────────

def validate_data_url(value, max_bytes: int, what: str = "Картинка") -> str:
    """data-URL PNG/JPEG/WebP не больше max_bytes; MIME — по сигнатуре
    байтов (как у аватаров), заявленному типу не доверяем. Возвращает
    нормализованный data-URL (MIME из сигнатуры) или RoomError(400/413)."""
    m = _DATA_URL_RE.match(value or "") if isinstance(value, str) else None
    if not m:
        raise RoomError(400, f"{what}: ожидается data-URL картинки PNG, JPEG или WebP")
    b64 = re.sub(r"\s+", "", m.group(2))
    # Размер до декодирования: не распаковываем заведомо лишнее
    if len(b64) * 3 // 4 > max_bytes + 3:
        raise RoomError(413, f"{what}: больше {max_bytes // 1024} КБ")
    try:
        raw = base64.b64decode(b64, validate=True)
    except ValueError:
        raise RoomError(400, f"{what}: повреждена (base64)")
    if len(raw) > max_bytes:
        raise RoomError(413, f"{what}: больше {max_bytes // 1024} КБ")
    mime = _sniff(raw)
    if mime is None:
        raise RoomError(400, f"{what}: файл не похож на PNG, JPEG или WebP")
    return f"data:{mime};base64,{b64}"


def decode_data_url(value: str, max_bytes: int) -> tuple[bytes, str]:
    url = validate_data_url(value, max_bytes, "Референс")
    head, b64 = url.split(",", 1)
    return base64.b64decode(b64), head[5:].split(";", 1)[0]


# ── Источник: какой контекст/чат показывать ───────────────────────────

def _activity(context: str) -> dict:
    data = room.read_json_cached(data_dir() / context / "known_chats.json", {})
    act = (data or {}).get("activity") if isinstance(data, dict) else None
    return act if isinstance(act, dict) else {}


def _web_last_message(persona: str) -> float:
    data = room.read_json_cached(data_dir() / f"api_{persona}" / "last_message.json", {})
    try:
        return float((data or {}).get(WEB_CHAT_ID, 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _states(context: str) -> dict:
    data = room.read_json_cached(room.living_dir(context) / "state.json", {})
    chats = (data or {}).get("chats") if isinstance(data, dict) else None
    return chats if isinstance(chats, dict) else {}


def _kind(persona: str, context: str) -> str:
    return "web" if context == f"api_{persona}" else "telegram"


def resolve_source(persona: str, chat_id: str | None = "auto",
                   context: str | None = None, prefer_with_state: bool = True) -> dict:
    """chat_id=auto — самый свежий чат по картам активности known_chats.json
    обоих контекстов (веб — ещё и last_message.json); при равных — чат, у
    которого уже есть живое состояние. Нет ничего — веб/web_user."""
    web_ctx, tg_ctx = f"api_{persona}", persona
    if chat_id and chat_id != "auto":
        chat_id = str(chat_id)
        if not _CHAT_ID_RE.match(chat_id):
            raise RoomError(400, "Недопустимый chat_id")
        if context is None:
            context = web_ctx if (chat_id == WEB_CHAT_ID
                                  or not chat_id.lstrip("-").isdigit()) else tg_ctx
        if context not in (web_ctx, tg_ctx):
            raise RoomError(400, "context — только api_<персона> или <персона>")
        ts = _activity(context).get(chat_id)
        if context == web_ctx and chat_id == WEB_CHAT_ID:
            ts = max(float(ts or 0), _web_last_message(persona)) or None
        return {"context": context, "chat_id": chat_id,
                "kind": _kind(persona, context),
                "last_activity": float(ts) if ts else None}

    candidates = []
    for ctx in (web_ctx, tg_ctx):
        for cid, ts in _activity(ctx).items():
            try:
                candidates.append((float(ts), ctx, str(cid)))
            except (TypeError, ValueError):
                continue
    web_ts = _web_last_message(persona)
    if web_ts:
        candidates.append((web_ts, web_ctx, WEB_CHAT_ID))
    candidates.sort(key=lambda c: c[0], reverse=True)
    chosen = None
    if prefer_with_state:
        for ts, ctx, cid in candidates:
            if cid in _states(ctx):
                chosen = (ts, ctx, cid)
                break
    if chosen is None and candidates:
        chosen = candidates[0]
    if chosen is None:
        return {"context": web_ctx, "chat_id": WEB_CHAT_ID, "kind": "web",
                "last_activity": None}
    ts, ctx, cid = chosen
    return {"context": ctx, "chat_id": cid, "kind": _kind(persona, ctx),
            "last_activity": ts}


# ── Снимок комнаты (GET /room) ─────────────────────────────────────────

def _inventory(context: str) -> list:
    data = room.read_json_cached(data_dir() / context / "inventory.json", {})
    items = (data or {}).get("items") if isinstance(data, dict) else None
    out = []
    for i in items if isinstance(items, list) else []:
        if isinstance(i, dict) and i.get("name"):
            out.append({k: i.get(k) for k in
                        ("name", "description", "acquired", "source", "tags")}
                       | ({"expires": i["expires"]} if i.get("expires") else {}))
    return out


def _focus_view(persona: str) -> dict:
    f = _read(persona, "focus.json", {})
    started = f.get("started_at")
    minutes = f.get("minutes")
    active = bool(f.get("active")) and isinstance(started, (int, float))
    if active:
        limit = float(started) + int(minutes or FOCUS_DEFAULT_MIN) * 60 + FOCUS_GRACE_SEC
        active = time.time() < limit
    return {"active": active,
            "started_at": float(started) if active else None,
            "minutes": int(minutes) if active and minutes else None}


def _living(persona_data: dict, source: dict, spots: list) -> dict | None:
    from app.core.living_persona import LivingPersonaConfig
    cfg = LivingPersonaConfig(persona_data.get("features") or {})
    if not cfg.state_enabled:
        return None
    ctx, chat_id = source["context"], source["chat_id"]
    raw = _states(ctx).get(chat_id)
    state = None
    if isinstance(raw, dict):
        state = dict(raw)
        # Легаси-состояние без места / место исчезнувшего предмета —
        # тот же санитайзер, что в тике (файл владельца не трогаем)
        room.sanitize_spot(state, dict(raw), spots)
        state.setdefault("pastime_since", raw.get("last_tick_at"))
        state.pop("internal_note", None)
    ld = room.living_dir(ctx)
    log = room.read_json_cached(ld / "offline_log.json", {})
    entries = (log or {}).get("entries") if isinstance(log, dict) else None
    recent = [dict(e) for e in (entries if isinstance(entries, list) else [])
              if isinstance(e, dict) and e.get("chat_id") == str(chat_id)][-8:]
    plans = []
    if cfg.world_enabled:
        world = room.read_json_cached(ld / "world.json", {})
        raw_plans = world.get("plans") if isinstance(world, dict) else None
        for p in raw_plans if isinstance(raw_plans, list) else []:
            if not isinstance(p, dict) or p.get("status") != "pending":
                continue
            due_at = p.get("due_at")
            due = None
            if isinstance(due_at, (int, float)):
                try:
                    due = timeutil.from_ts(float(due_at)).isoformat(timespec="minutes")
                except Exception:
                    due = None
            plans.append({"title": p.get("title", ""), "detail": p.get("detail", ""),
                          "due": due, "due_at": due_at})
        plans.sort(key=lambda p: p.get("due_at") or 0)
    return {"enabled": True, "ui_sync": cfg.ui_room_mood_sync, "state": state,
            "recent_events": recent, "plans": plans[:5]}


def room_snapshot(persona: str, persona_data: dict, chat_id: str | None = "auto",
                  context: str | None = None) -> dict:
    source = resolve_source(persona, chat_id, context)
    ctx = source["context"]
    config = room.resolve_room_config(persona_data)
    inventory = _inventory(ctx)
    names = [i["name"] for i in inventory]
    placements = {n: p for n, p in room.read_placements(ctx).items()
                  if n in names and isinstance(p, dict)}
    layout = _read(persona, "layout.json", {})
    item_spots = room.item_spots(placements, layout, names)
    spots = room.allowed_spots(config, placements, layout, names)
    return {
        "source": source,
        "config": {
            "props": config["props"], "pet": config["pet"],
            "pet_label": config["pet_label"], "poster_label": config["poster_label"],
            "spots": [room.public_spot(s) for s in config["spots"] + item_spots],
            "away": room.public_spot(room.AWAY_SPOT),
            "is_default": config["is_default"],
        },
        "living": _living(persona_data, source, spots),
        "inventory": inventory,
        "placements": placements,
        "focus": _focus_view(persona),
    }


# ── Раскладка (layout.json) ────────────────────────────────────────────

def _frac(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        raise RoomError(400, "Координата должна быть числом 0..1")


def _point(v) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise RoomError(400, "Точка — объект {x, y}")
    return {"x": _frac(v.get("x")), "y": _frac(v.get("y"))}


def _clean_item_patch(patch: dict) -> dict:
    out = {}
    if "marker" in patch:
        out["marker"] = _point(patch["marker"])
    if "size" in patch:
        try:
            out["size"] = max(2.0, min(25.0, float(patch["size"])))
        except (TypeError, ValueError):
            raise RoomError(400, "size — число 2..25")
    if "icon" in patch:
        icon = patch["icon"]
        out["icon"] = str(icon)[:64] if icon is not None else None
    if "image" in patch:
        img = patch["image"]
        out["image"] = (validate_data_url(img, IMAGE_MAX_BYTES, "Картинка предмета")
                        if img is not None else None)
    if "spot" in patch:
        spot = patch["spot"]
        if spot is None:
            out["spot"] = None
        elif isinstance(spot, dict):
            pose = str(spot.get("pose") or "").lower()
            out["spot"] = {
                "label": str(spot.get("label") or "").strip()[:80],
                "place": str(spot.get("place") or "").strip()[:80],
                "pose": pose if pose in room.POSES and pose != "away" else "stand",
            }
        else:
            raise RoomError(400, "spot — объект {label, place, pose} или null")
    if "hidden" in patch:
        out["hidden"] = bool(patch["hidden"])
    return out


def _clean_avatar(av) -> dict | None:
    if av is None:
        return None
    if not isinstance(av, dict):
        raise RoomError(400, "avatar — объект или null")
    out = {}
    if "head" in av:
        out["head"] = str(av.get("head") or "")[:32]
    for k in ("eyes", "accessory", "shade"):
        if k in av:
            try:
                out[k] = int(av[k])
            except (TypeError, ValueError, OverflowError):
                # OverflowError — Infinity (json.loads его принимает)
                raise RoomError(400, f"avatar.{k} — целое число")
    return out


def get_layout(persona: str) -> dict:
    data = _read(persona, "layout.json", {})
    items = data.get("items") if isinstance(data.get("items"), dict) else {}
    out = {"items": items, "avatar": data.get("avatar")}
    if data.get("updated_at"):
        out["updated_at"] = data["updated_at"]
    return out


def put_layout(persona: str, patch: dict) -> dict:
    """Частичное слияние: items[name] = null — удалить запись предмета,
    объект — слить поля; avatar — заменить (null — сбросить)."""
    with _write_lock:
        cur = get_layout(persona)
        items = {k: dict(v) for k, v in cur["items"].items() if isinstance(v, dict)}
        if "items" in patch and patch["items"] is not None:
            if not isinstance(patch["items"], dict):
                raise RoomError(400, "items — объект {имя предмета: поля}")
            for name, item_patch in patch["items"].items():
                name = str(name).strip()[:120]
                if not name:
                    continue
                if item_patch is None:
                    items.pop(name, None)
                    continue
                if not isinstance(item_patch, dict):
                    raise RoomError(400, f"items[{name}] — объект или null")
                entry = items.get(name, {})
                entry.update(_clean_item_patch(item_patch))
                items[name] = entry
            if len(items) > LAYOUT_MAX_ITEMS:
                raise RoomError(400, f"Больше {LAYOUT_MAX_ITEMS} предметов в раскладке")
        avatar = cur.get("avatar")
        if "avatar" in patch:
            avatar = _clean_avatar(patch["avatar"])
        data = {"items": items, "avatar": avatar, "updated_at": _now_iso()}
        _write(persona, "layout.json", data, LAYOUT_MAX_FILE)
        return data


# ── Стиль (style.json) ─────────────────────────────────────────────────

def get_style(persona: str) -> dict:
    data = _read(persona, "style.json", {})
    if not data:
        return {"description": "", "reference": None}
    return {"description": str(data.get("description") or ""),
            "reference": data.get("reference"),
            "updated_at": data.get("updated_at")}


def put_style(persona: str, patch: dict) -> dict:
    with _write_lock:
        cur = get_style(persona)
        if "description" in patch:
            desc = str(patch["description"] or "")
            if len(desc) > STYLE_MAX_CHARS:
                raise RoomError(400, f"Описание стиля длиннее {STYLE_MAX_CHARS} символов")
            cur["description"] = desc
        if "reference" in patch:
            ref = patch["reference"]
            cur["reference"] = (validate_data_url(ref, REFERENCE_MAX_BYTES, "Референс")
                                if ref is not None else None)
        cur["updated_at"] = _now_iso()
        _write(persona, "style.json", cur)
        return cur


# ── Арт (art.json) ─────────────────────────────────────────────────────

def _clean_sprite(v, what: str) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise RoomError(400, f"{what} — объект {{dataUrl, anchor}} или null")
    anchor = v.get("anchor")
    return {"dataUrl": validate_data_url(v.get("dataUrl"), IMAGE_MAX_BYTES, what),
            "anchor": _point(anchor) if anchor is not None else {"x": 0.5, "y": 1.0}}


def _spot_key_ok(key: str) -> bool:
    return key in room.BUILTIN_SPOTS or (
        key.startswith(room.ITEM_PREFIX) and 0 < len(key) <= 130)


def _clean_room_bg(v) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise RoomError(400, "room_bg — объект {dataUrl, floorPoints} или null")
    raw_points = v.get("floorPoints") or {}
    if not isinstance(raw_points, dict):
        # Иначе .items() у списка/строки — AttributeError и 500 вместо 400
        raise RoomError(400, "floorPoints — объект {место: {x, y}}")
    points = {}
    for k, p in raw_points.items():
        k = str(k)
        if not _spot_key_ok(k):
            raise RoomError(400, f"floorPoints: неизвестное место {k!r}")
        if p is not None:
            points[k] = _point(p)
    return {"dataUrl": validate_data_url(v.get("dataUrl"), IMAGE_MAX_BYTES, "Фон комнаты"),
            "floorPoints": points}


def get_art(persona: str) -> dict:
    data = _read(persona, "art.json", {})
    sprites = data.get("sprites") if isinstance(data.get("sprites"), dict) else {}
    return {"sprite": data.get("sprite"), "sprites": sprites,
            "room_bg": data.get("room_bg")}


def put_art(persona: str, patch: dict) -> dict:
    """Частичное слияние: sprite/room_bg — заменить (null — удалить);
    sprites — по позам (null у позы — удалить её спрайт)."""
    with _write_lock:
        cur = get_art(persona)
        sprites = dict(cur["sprites"])
        if "sprite" in patch:
            cur["sprite"] = _clean_sprite(patch["sprite"], "Спрайт")
        if "sprites" in patch and patch["sprites"] is not None:
            if not isinstance(patch["sprites"], dict):
                raise RoomError(400, "sprites — объект {поза: спрайт}")
            for pose, sp in patch["sprites"].items():
                if pose not in SPRITE_POSES:
                    raise RoomError(400, f"sprites: неизвестная поза {pose!r}")
                if sp is None:
                    sprites.pop(pose, None)
                else:
                    sprites[pose] = _clean_sprite(sp, f"Спрайт «{pose}»")
        cur["sprites"] = sprites
        if "room_bg" in patch:
            cur["room_bg"] = _clean_room_bg(patch["room_bg"])
        _write(persona, "art.json", cur, ART_MAX_FILE)
        return cur


# ── Клик по персоне и фокус-сессия ─────────────────────────────────────

def _pastime(source: dict) -> str:
    st = _states(source["context"]).get(source["chat_id"])
    return str((st or {}).get("pastime") or "").strip() if isinstance(st, dict) else ""


def _source_lang(source: dict) -> str | None:
    """Язык пользователя чата-источника: его сохраняет владелец контекста
    (LivingPersona, data/<context>/living/user_lang.json). Тексты сигналов
    уходят в промпт ответа и в ленту комнаты — пишем их на этом языке."""
    try:
        from app.core.living_persona import stored_chat_language
        return stored_chat_language(source["context"], source["chat_id"])
    except Exception:
        return None


def _signal_text(lang: str | None, ru: str, en: str) -> str:
    return ru if lang == "ru" else en


def poke(persona: str, persona_data: dict, chat_id: str | None = "auto") -> dict:
    """Клик по персоне: сигнал glance владельцу контекста — только при
    features.room_pokes_to_llm и не чаще раза в 15 минут на чат. Иначе
    delivered: false (реакция остаётся локальной в браузере)."""
    features = (persona_data or {}).get("features") or {}
    if not bool(features.get("room_pokes_to_llm", False)):
        return {"ok": True, "delivered": False}
    source = resolve_source(persona, chat_id)
    key = f"{source['context']}:{source['chat_id']}"
    now = time.time()
    with _poke_lock:
        if now - _poke_at.get(key, 0.0) < POKE_INTERVAL_SEC:
            return {"ok": True, "delivered": False}
        _poke_at[key] = now
    pastime = _pastime(source)
    lang = _source_lang(source)
    text = (_signal_text(lang, f"Пользователь заглянул в комнату, пока ты: {pastime}",
                         f"The user looked into your room while you were: {pastime}")
            if pastime else
            _signal_text(lang, "Пользователь заглянул к тебе в комнату",
                         "The user looked into your room"))
    room.append_signal(source["context"], "glance", source["chat_id"], text)
    return {"ok": True, "delivered": True}


def focus(persona: str, action: str, minutes: int | None = None,
          chat_id: str | None = "auto") -> dict:
    """start — запомнить сессию и дать сигнал focus_start; end — закрыть,
    сигнал focus_end. Возвращает {"ok", "focus", "source", "elapsed_min",
    "was_active"}: end без активной сессии (повтор, вторая вкладка) —
    без сигнала, was_active false (реплику персоны не генерируем)."""
    if action not in ("start", "end"):
        raise RoomError(400, "action — start или end")
    with _write_lock:
        cur = _read(persona, "focus.json", {})
        now = time.time()
        if action == "start":
            mins = int(minutes or FOCUS_DEFAULT_MIN)
            mins = max(5, min(180, mins))
            source = resolve_source(persona, chat_id)
            data = {"active": True, "started_at": now, "minutes": mins,
                    "context": source["context"], "chat_id": source["chat_id"],
                    "ended_at": None}
            _write(persona, "focus.json", data)
            text = _signal_text(
                _source_lang(source),
                f"Пользователь сел поработать рядом с тобой (фокус-сессия на {mins} мин)",
                f"The user sat down to work next to you (focus session for {mins} min)")
            room.append_signal(source["context"], "focus_start", source["chat_id"], text)
            return {"ok": True, "focus": _focus_view(persona), "source": source,
                    "elapsed_min": 0}
        if not cur.get("active"):
            # Сессия уже закрыта (или её не было): повторный end не должен
            # дублировать сигнал focus_end и звать основную LLM ещё раз
            return {"ok": True, "focus": _focus_view(persona),
                    "source": resolve_source(persona, chat_id),
                    "elapsed_min": 0, "was_active": False}
        # end: источник — тот, где сессия начиналась (если он ещё валиден)
        ctx, cid = cur.get("context"), cur.get("chat_id")
        if ctx in (f"api_{persona}", persona) and isinstance(cid, str) \
                and _CHAT_ID_RE.match(cid) and (not chat_id or chat_id == "auto"):
            source = {"context": ctx, "chat_id": cid, "kind": _kind(persona, ctx),
                      "last_activity": None}
        else:
            source = resolve_source(persona, chat_id)
        started = cur.get("started_at") if cur.get("active") else None
        elapsed = int(round((now - float(started)) / 60)) if isinstance(
            started, (int, float)) else int(minutes or 0)
        elapsed = max(0, elapsed)
        data = dict(cur)
        data.update({"active": False, "ended_at": now})
        _write(persona, "focus.json", data)
        text = _signal_text(
            _source_lang(source),
            "Пользователь закончил фокус-сессию рядом с тобой"
            + (f" (~{elapsed} мин)" if elapsed else ""),
            "The user finished a focus session next to you"
            + (f" (~{elapsed} min)" if elapsed else ""))
        room.append_signal(source["context"], "focus_end", source["chat_id"], text)
        return {"ok": True, "focus": _focus_view(persona), "source": source,
                "elapsed_min": elapsed, "was_active": True}


FOCUS_LINE_PROMPT = (
    "[Service note, not a user message] The user has just finished a focus "
    "session: {elapsed}they were doing their own things, and you were nearby. "
    "Write them ONE short line in your character (no more than 25 words): ask "
    "how it went. No lists, no quotes, no mention of the timer, the app or "
    "the system.\n{language_line}")

STYLE_DESCRIBE_PROMPT = (
    "Describe ONLY the art style of this image: medium, line quality, color "
    "palette, proportions (e.g. chibi), shading and texture. Do NOT describe "
    "the character, subject, pose or scene content. Answer in English, one "
    "paragraph, at most 60 words, no lists.")


def generate_focus_line(bot, elapsed_min: int) -> str | None:
    """Одна короткая реплика персоны в конце фокус-сессии — основной LLM
    с полным system_prompt персоны. Блокирующая, звать из потока."""
    try:
        if (bot.features or {}).get("muted"):
            return None
        system = (bot.persona.system_prompt or "").strip()
        if not system:
            return None
        messages = [{"role": "system", "content": system}]
        try:
            for m in bot.memory.stm.get_last(6, chat_id=WEB_CHAT_ID):
                if m.get("role") in ("user", "assistant") and m.get("content"):
                    messages.append({"role": m["role"],
                                     "content": str(m["content"])[:400]})
        except Exception:
            pass
        elapsed = f"for about {elapsed_min} min " if elapsed_min else ""
        lang = (detect_dialogue_language("", messages[1:])
                or persona_language(system))
        messages.append({"role": "user",
                         "content": FOCUS_LINE_PROMPT.format(
                             elapsed=elapsed,
                             language_line=user_language_line(lang))})
        answer = bot.router.get_response(messages, temperature=0.8, max_tokens=120,
                                         timeout=45.0, webchat_channel="proactive")
    except Exception as e:
        logger.warning(f"[room] Реплика конца фокус-сессии не сгенерирована: {e}")
        return None
    line = re.sub(r"\s+", " ", str(answer or "")).strip().strip('"«»')
    if not line:
        return None
    words = line.split(" ")
    if len(words) > 40:
        line = " ".join(words[:40]).rstrip(",;:") + "…"
    return line[:400]


def trim_words(text: str, limit: int = 60) -> str:
    words = re.sub(r"\s+", " ", str(text or "")).strip().split(" ")
    if len(words) <= limit:
        return " ".join(words).strip()
    return " ".join(words[:limit]).rstrip(",;:") + "."

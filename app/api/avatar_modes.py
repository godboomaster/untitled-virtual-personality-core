"""Аватар у режима персоны: у каждого YAML режима (app/core/persona_modes:
<персона>_modes/<режим>.yaml) могут быть свои листы портрета и свои
анимации, а смену режима может сопровождать анимация перехода.

Хранение. Аватар режима устроен так же, как обычный
(data/api_<персона>/portrait/: portrait.json, листы, anims/), и лежит в
data/api_<персона>/portrait_modes/<режим>/. Ключ аватара — id персоны
(обычный) или «персона@режим»: его понимает portrait_api.portrait_dir, а с
ним вся нарезка, анимации и редактор кадров (portrait_api, portrait_anim,
portrait_frames). Маршруты ядра берут режим параметром ?mode=.

Какой аватар сейчас. Клиенты, что показывают голову персоны (портрет на
рабочем столе, голосовой режим веба и PiP, виджет и голова поверх
приложений на телефоне), пишут в основной чат персоны (web_user) — аватар
идёт за его режимом: по триггеру (имя, слово, настроение, «/mode»), иначе
по собеседнику (master/mortal). Бот персоны ещё не поднят — режим по
триггеру из файла состояния чата (без настроения и без master/mortal).

- Голова: у режима свой лист A — его листы, иначе обычные.
- Анимации: у режима есть свои (не только переходы) — его; голова своя, а
  анимаций нет — никаких (обычные нарисованы под другую фигуру); иначе
  обычные.
- Переход — анимация с поводом mode в аватаре режима, В КОТОРЫЙ переходят;
  switch_from — из каких режимов ("" — обычный, пусто — из любого). В живой
  набор анимаций (live_anims) входят переходы только в нынешний режим:
  клиент видит, что режим сменился, играет подходящий переход, а следом —
  уже голова нового режима. Перехода нет — голова просто меняется.

Сменился режим чата (BotInstance: реплика с именем режима, ключевое слово,
«/mode») — событие mode в ленте реплик (app/api/said) и сигнал вебу
(app/api/chat_events): портрет на столе и виджет телефона перечитывают
аватар, веб узнаёт режим из inbox (avatar_mode).
"""

from __future__ import annotations

import threading
from typing import Optional

from app.api import portrait_anim, portrait_api
from app.api.portrait_api import MODE_SEP, PortraitError
from app.core import persona_modes
from app.core.persona_modes import MODE_ID_RE

WEB_CHAT = "web_user"
SWITCH = "mode"  # повод анимации-перехода (portrait_anim.TRIGGERS)


def key(persona: str, mode: Optional[str] = None) -> str:
    """Ключ аватара: обычный — id персоны, режима — «персона@режим»."""
    return f"{persona}{MODE_SEP}{mode}" if mode else persona


def split(avatar: str) -> tuple[str, str]:
    """Ключ аватара → (персона, режим; "" — обычный)."""
    persona, _, mode = str(avatar).partition(MODE_SEP)
    return persona, mode


def _dirs():
    from app.core.addons import persona_dirs
    return persona_dirs()


def mode_ids(persona: str) -> set[str]:
    """Все режимы персоны по файлам (и master/mortal)."""
    return set(persona_modes._mode_files(persona, _dirs()))


def check(persona: str, mode: Optional[str]) -> str:
    """Ключ аватара для маршрута с ?mode=: пусто — обычный; режим — из
    файлов персоны (удалённого режима нет — 404)."""
    if not mode:
        return persona
    if not MODE_ID_RE.match(mode):
        raise PortraitError(400, "Недопустимый id режима")
    if mode not in mode_ids(persona):
        raise PortraitError(404, "Режим не найден")
    return key(persona, mode)


# ── Какой режим у чата сейчас ──

_disk_lock = threading.Lock()
_disk_triggers: dict[str, tuple] = {}  # персона → (подпись папки режимов, триггеры)


def _disk_mode(persona: str, chat_id: str) -> str:
    # Бот не поднят: включённый режим по триггеру из файла состояния чата
    from app.core.paths import data_dir
    dirs = _dirs()
    sig = persona_modes.signature(persona, dirs)
    if not sig:
        return ""
    with _disk_lock:
        hit = _disk_triggers.get(persona)
    if hit is None or hit[0] != sig:
        hit = (sig, persona_modes.trigger_modes(persona_modes.load_modes(persona, dirs)))
        with _disk_lock:
            _disk_triggers[persona] = hit
    triggers = hit[1]
    if not triggers:
        return ""
    state = persona_modes.ModeState(persona_modes.state_path(data_dir(), f"api_{persona}")).get(chat_id)
    return persona_modes.effective(triggers, state.get("active"), None) or ""


def active_mode(persona: str, chat_id: str = WEB_CHAT) -> str:
    """Режим чата сейчас — тот, за которым идёт аватар; "" — обычный."""
    from app.api.runtime import registry
    layer = getattr(registry.peek(persona), "persona", None)
    if layer is not None:
        try:
            return str(layer.mode_for(user_id=chat_id, chat_id=chat_id) or "")
        except Exception:
            return ""
    try:
        return _disk_mode(persona, chat_id)
    except Exception:
        return ""


# ── Чей аватар показывать ──

def _anims(avatar: str) -> list[dict]:
    # Записи анимаций без атласов; аватара нет — пусто
    try:
        return portrait_anim._read(avatar)["anims"]
    except PortraitError:
        return []


def resolve(persona: str, mode: str) -> dict:
    """{"mode", "head": ключ листов портрета, "anims": ключ обычных анимаций}."""
    if not mode:
        return {"mode": "", "head": persona, "anims": persona}
    own = key(persona, mode)
    head = own if portrait_api.has_portrait(own) else persona
    regular = any(not portrait_anim.is_switch_only(a) for a in _anims(own))
    return {"mode": mode, "head": head, "anims": own if regular or head == own else persona}


def live_portrait(persona: str, chat_id: str = WEB_CHAT) -> dict:
    """Листы портрета, которые сейчас показывать (как get_portrait) + mode —
    режим чата и avatar — чьи листы ("" — обычные)."""
    mode = active_mode(persona, chat_id)
    r = resolve(persona, mode)
    out = portrait_api.get_portrait(r["head"])
    out.update(mode=mode, avatar=split(r["head"])[1])
    return out


def _stamp(persona: str, r: dict):
    # Метка живого набора анимаций: сменилась — клиенту качать заново. У
    # обычного режима — метка его анимаций, как до режимов
    a = portrait_anim.updated_at(r["anims"])
    if not r["mode"]:
        return a
    own = key(persona, r["mode"])
    b = portrait_anim.updated_at(own) if own != r["anims"] else None
    if a is None and b is None:
        return None
    return f"{r['mode']}:{split(r['anims'])[1]}:{a}:{b}"


def live_anims(persona: str, chat_id: str = WEB_CHAT, atlas: bool = True) -> dict:
    """Анимации, которые сейчас играть (как get_anims): обычные анимации
    аватара (resolve) и переходы в нынешний режим; mode — режим чата."""
    mode = active_mode(persona, chat_id)
    r = resolve(persona, mode)
    src = portrait_anim.get_anims(r["anims"], atlas)
    items = [a for a in src["anims"] if not portrait_anim.is_switch_only(a)]
    own = key(persona, mode)
    into = src if own == r["anims"] else portrait_anim.get_anims(own, atlas)
    seen = {a["id"] for a in items}
    items += [a for a in into["anims"] if SWITCH in a["triggers"] and a["id"] not in seen]
    return {"anims": items, "settings": src["settings"], "updated_at": _stamp(persona, r), "mode": mode}


def live_state(persona: str, chat_id: str = WEB_CHAT) -> dict:
    """Для GET /api/portraits без атласов: {mode — режим чата, has_head —
    есть ли портрет, head — метка его листов, has_anims — есть ли анимации,
    anims — метка их живого набора}."""
    mode = active_mode(persona, chat_id)
    r = resolve(persona, mode)
    has_head = portrait_api.has_portrait(r["head"])
    has_anims = portrait_anim.has_anims(r["anims"]) or bool(mode and portrait_anim.has_anims(key(persona, mode)))
    return {"mode": mode, "has_head": has_head,
            "head": portrait_api.updated_at(r["head"]) if has_head else None,
            "has_anims": has_anims, "anims": _stamp(persona, r) if has_anims else None}


def switch_anims(persona: str, from_mode: str, to_mode: str) -> list[dict]:
    """Переходы из from_mode в to_mode с атласами: сначала те, где этот
    режим назван, нет таких — «из любого». Пусто — перехода нет."""
    try:
        data = portrait_anim.get_anims(key(persona, to_mode), True)
    except PortraitError:
        return []
    cand = [a for a in data["anims"] if a["enabled"] and SWITCH in a["triggers"] and a.get("frames", 0) > 0]
    named = [a for a in cand if from_mode in (a.get("switch_from") or [])]
    return named or [a for a in cand if not a.get("switch_from")]


def overview(persona: str) -> dict:
    """Что есть у аватаров режимов (вкладка «Аватар» в вебе): {режим:
    {head: есть ли свой лист A, anims: своих анимаций (без переходов),
    switches: переходов в него}}; "" — обычный. Плюс active — режим
    основного чата сейчас."""
    out = {}
    for mode in [""] + sorted(mode_ids(persona)):
        k = key(persona, mode)
        try:
            items = _anims(k)
            out[mode] = {"head": portrait_api.has_portrait(k),
                         "anims": sum(1 for a in items if not portrait_anim.is_switch_only(a)),
                         "switches": sum(1 for a in items if SWITCH in a["triggers"])}
        except PortraitError:
            continue
    return {"modes": out, "active": active_mode(persona)}

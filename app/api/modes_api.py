"""Режимы персоны в вебе: вкладка «Режимы» в досье и в окне «Редактировать».

Режим — файл <id персоны>_modes/<режим>.yaml (app/core/persona_modes): свой
характер и триггеры, память у персоны общая. Здесь — список, текст файла,
запись (правка, новый режим по заготовке, загрузка файла с диска) и удаление.
Пишется всегда в папку персон пользователя: файл встроенной персоны (часть
проекта) перекрывается своим, а удаление своего возвращает встроенный.
Живой бот подхватывает правку сразу (Persona._refresh_modes), Telegram-боты
(другой процесс) — за несколько секунд.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

import yaml

from app.api import settings_api
from app.api.security import atomic_write_text, yaml_write_lock
from app.core import persona_modes
from app.core.persona_modes import ADDRESSEE_MODES, MODE_ID_RE, Triggers, norm

logger = logging.getLogger(__name__)

MAX_YAML_CHARS = 200_000
MAX_BODY_BYTES = MAX_YAML_CHARS * 4 + 64 * 1024
# Слова команды /mode (BotInstance.mode_command): режим с таким id ею не включить
RESERVED_IDS = frozenset({"off", "base", "normal", "обычный", "обычная", "выкл", "auto", "авто"})
# Удалённые режимы — копией сюда (внутри папки режимов, загрузчик её не видит)
TRASH_DIR = ".deleted"
# Что из файла режима действует (Persona: prompt_for, get_settings, mode_label)
USED_KEYS = {"name", "description", "system_prompt", "short_prompt", "settings", "triggers", "voice_examples"}


class ModeError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── файлы ──

def _read_dirs() -> list[Path]:
    return settings_api._read_dirs()


def _own_folder(persona: str) -> Path:
    # persona уже проверена (PersonaIdPath) — в путь не попадёт «..» и «/»
    return settings_api._write_dir() / f"{persona}_modes"


def _is_builtin(path: Path) -> bool:
    return path.parent.parent.resolve() in settings_api._builtin_dirs()


def _files(persona: str) -> dict[str, Path]:
    return persona_modes._mode_files(persona, _read_dirs())


def _builtin_file(persona: str, mode: str) -> Optional[Path]:
    # Встроенный файл режима, который перекрыт своим (или сам по себе)
    for d in _read_dirs():
        path = Path(d) / f"{persona}_modes" / f"{mode}.yaml"
        if path.is_file() and _is_builtin(path):
            return path
    return None


def check_mode_id(mode: str) -> str:
    mode = (mode or "").strip()
    if not MODE_ID_RE.match(mode):
        raise ModeError("Id режима — буквы, цифры, _ и - (до 40 знаков)")
    if mode.lower() in RESERVED_IDS:
        raise ModeError(f"«{mode}» — слово команды /mode, выберите другой id")
    return mode


def suggest_id(text: str) -> str:
    """Id из имени файла или имени режима: «Sancho (1).yaml» → «sancho_1»."""
    s = re.sub(r"\.(ya?ml)$", "", (text or "").strip(), flags=re.IGNORECASE).lower()
    s = re.sub(r"[^\w-]+", "_", s).strip("_-")[:40].strip("_-")
    return s if MODE_ID_RE.match(s or "") and s not in RESERVED_IDS else ""


def _kind(mode: str) -> str:
    return mode if mode in ADDRESSEE_MODES else "trigger"


def _persona_names(persona: str) -> list[str]:
    path = settings_api._persona_yaml_path(persona)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path and path.is_file() else {}
    except Exception:
        data = {}
    words = ((data or {}).get("features") or {}).get("trigger_words") if isinstance(data, dict) else None
    return [str(w) for w in words if str(w).strip()] if isinstance(words, list) else []


# ── список и текст ──

def _entry(persona: str, mode: str, path: Path) -> dict:
    out = {
        "id": mode,
        "kind": _kind(mode),
        "builtin": _is_builtin(path),
        # Свой файл поверх встроенного: удаление вернёт встроенный
        "overrides_builtin": not _is_builtin(path) and _builtin_file(persona, mode) is not None,
        "name": mode, "description": "",
        "names": [], "keywords": [], "exit_keywords": [], "mood": None, "hold_minutes": None,
        "error": "",
    }
    try:
        out["updated"] = path.stat().st_mtime
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        out["error"] = f"YAML не читается: {e}"[:300]
        return out
    if not isinstance(data, dict):
        out["error"] = "Файл — не объект YAML"
        return out
    out["name"] = str(data.get("name") or mode)
    out["description"] = str(data.get("description") or "")
    if not str(data.get("system_prompt") or "").strip():
        out["error"] = "Нет system_prompt — режим не действует"
    if out["kind"] == "trigger":
        trig = Triggers.parse(data.get("triggers"))
        out.update(names=list(trig.names), keywords=list(trig.keywords),
                   exit_keywords=list(trig.exit_keywords),
                   mood=trig.mood.describe() if trig.mood else None,
                   hold_minutes=trig.hold_minutes)
    return out


def list_modes(persona: str) -> dict:
    files = _files(persona)
    # Файлы, которые загрузчик не возьмёт (id с пробелом, точкой…) — чтобы
    # пользователь видел, почему его режима нет
    skipped = []
    for d in _read_dirs():
        folder = Path(d) / f"{persona}_modes"
        if folder.is_dir():
            skipped += [p.name for p in sorted(folder.glob("*.y*ml"))
                        if p.suffix in (".yaml", ".yml") and p.stem not in files
                        and (p.suffix == ".yml" or not MODE_ID_RE.match(p.stem))]
    return {
        "persona": persona,
        "modes": [_entry(persona, m, p) for m, p in sorted(files.items())],
        "base_names": _persona_names(persona),
        "folder": str(_own_folder(persona)),
        "skipped": sorted(set(skipped)),
    }


def get_yaml(persona: str, mode: str) -> dict:
    path = _files(persona).get(mode)
    if path is None:
        raise ModeError("Режим не найден", 404)
    return {"mode": mode, "yaml": path.read_text(encoding="utf-8"), "builtin": _is_builtin(path)}


_SKELETON = {
    "ru": """# Режим персоны {persona}: свой характер, память — общая с персоной.
# Пояснения ко всем ключам — persona_mode_template.yaml в папке проекта.
name: {name}
# description: коротко, чем режим отличается

# Характер в этом режиме — целиком, вместо system_prompt персоны
system_prompt: |
  You are {title}. Describe the character in this mode here.

# voice_examples:      # 3–6 коротких реплик в манере режима — держат его тон
#   - "Short line in this mode's voice."

# settings:            # параметры генерации поверх settings персоны
#   temperature: 0.8

triggers:
  # Обращение этим именем в начале реплики включает режим; обычное имя
  # персоны — возвращает. Чат, голос, аватар, Telegram
  names: [{trigger}]
  # keywords: [слово, начало*]      # слово в любом месте реплики
  # exit_keywords: [вернись]        # фразы, которые снимают режим
  # mood: [злой]                    # настроение персоны (теги или пороги)
  # hold_minutes: 60                # нет — пока не сменят, 0 — одна реплика
""",
    "en": """# A mode of persona {persona}: its own character, memory shared with the persona.
# Every key is explained in persona_mode_template.yaml in the project folder.
name: {name}
# description: in short, how this mode differs

# The character in this mode — in full, instead of the persona's system_prompt
system_prompt: |
  You are {title}. Describe the character in this mode here.

# voice_examples:      # 3–6 short lines in this mode's voice — keep its tone
#   - "Short line in this mode's voice."

# settings:            # generation settings over the persona's settings
#   temperature: 0.8

triggers:
  # Addressing by this name at the start of a message switches the mode on;
  # the persona's usual name switches it back. Chat, voice, avatar, Telegram
  names: [{trigger}]
  # keywords: [word, prefix*]       # a word anywhere in the message
  # exit_keywords: [come back]      # phrases that switch the mode off
  # mood: [angry]                   # the persona's mood (tags or thresholds)
  # hold_minutes: 60                # none — until changed, 0 — one message
""",
}


def _scalar(value: str) -> str:
    # Строка для YAML как есть или в кавычках («on», «12», «a: b»)
    return yaml.safe_dump(value, allow_unicode=True).strip().removesuffix("...").strip()


def skeleton(persona: str, name: str, lang: str = "ru") -> str:
    name = " ".join((name or "").split())[:80] or "mode"
    return _SKELETON["en" if lang == "en" else "ru"].format(
        persona=persona, name=_scalar(name), title=name, trigger=_scalar(norm(name)))


# ── запись ──

def _parse(raw: str) -> dict:
    if len(raw) > MAX_YAML_CHARS:
        raise ModeError(f"Файл больше {MAX_YAML_CHARS // 1000} тыс. знаков")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ModeError(f"YAML не разбирается: {e}"[:400])
    if not isinstance(data, dict) or not str(data.get("system_prompt") or "").strip():
        raise ModeError("Нужен объект YAML с непустым system_prompt — характером в этом режиме")
    if data.get("triggers") is not None and not isinstance(data.get("triggers"), (dict, list, str)):
        raise ModeError("triggers — объект: names, keywords, exit_keywords, mood, hold_minutes")
    if data.get("settings") is not None and not isinstance(data.get("settings"), dict):
        raise ModeError("settings — объект (temperature, top_p, max_tokens)")
    return data


def _warnings(persona: str, mode: str, data: dict) -> list[str]:
    out = []
    ignored = sorted(k for k in data if k not in USED_KEYS and k != "id")
    if ignored:
        out.append("Не действуют в режиме (они общие с персоной): " + ", ".join(ignored))
    if mode in ADDRESSEE_MODES:
        if data.get("triggers"):
            out.append(f"{mode}.yaml — режим по собеседнику: triggers в нём не действуют")
        return out
    trig = Triggers.parse(data.get("triggers"))
    if not (trig.names or trig.keywords or trig.mood):
        out.append("Нет триггеров: режим включится только командой /mode " + mode)
    mine = {norm(n) for n in trig.names}
    base = {norm(w) for w in _persona_names(persona)}
    if mine & base:
        out.append("Имена совпадают с обычными именами персоны: " + ", ".join(sorted(mine & base))
                   + " — по ним режим снимается, а не включается")
    for other, path in _files(persona).items():
        if other == mode or other in ADDRESSEE_MODES:
            continue
        try:
            o = Triggers.parse((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("triggers"))
        except Exception:
            continue
        same = mine & {norm(n) for n in o.names}
        if same:
            out.append(f"Имена уже у режима {other}: " + ", ".join(sorted(same)))
    return out


def _refresh_live(persona: str) -> None:
    from app.api.runtime import registry
    bot = registry._bots.get(persona)
    if bot is None:
        settings_api._ensure_persona_flavor(persona)
        return
    try:
        bot.persona._modes_checked_at = float("-inf")
        bot.persona._refresh_modes()
        settings_api._ensure_persona_flavor(persona)
    except Exception:
        logger.exception(f"[Modes] {persona}: режимы не перечитаны")


def save(persona: str, mode: str, raw: str, create: bool = False) -> dict:
    """Записать режим. create — новый: есть такой — ошибка 409 (интерфейс
    переспросит и пришлёт без create)."""
    mode = check_mode_id(mode)
    data = _parse(raw)
    folder = _own_folder(persona)
    path = folder / f"{mode}.yaml"
    with yaml_write_lock:
        if create and mode in _files(persona):
            raise ModeError(f"Режим {mode} уже есть", 409)
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, raw if raw.endswith("\n") else raw + "\n")
    logger.info(f"[Modes] {persona}: режим {mode} записан ({path})")
    _refresh_live(persona)
    result = list_modes(persona)
    result.update(saved=mode, warnings=_warnings(persona, mode, data))
    return result


def _with_triggers_from_persona(raw: str, data: dict) -> tuple[str, bool]:
    """Загрузили YAML персоны целиком: её имена (features.trigger_words) —
    имена-обращения режима, если своих triggers в файле нет."""
    if "triggers" in data:
        return raw, False
    feats = data.get("features")
    words = feats.get("trigger_words") if isinstance(feats, dict) else None
    words = [str(w).strip() for w in words if str(w).strip()] if isinstance(words, list) else []
    if not words:
        return raw, False
    block = yaml.safe_dump({"triggers": {"names": words}}, allow_unicode=True, sort_keys=False)
    note = "\n# Добавлено при загрузке: имена-обращения — из features.trigger_words файла\n"
    return raw.rstrip("\n") + "\n" + note + block, True


def upload(persona: str, filename: str, body: bytes, mode: str = "",
           overwrite: bool = False) -> dict:
    """Файл YAML с диска — режимом. Id — заданный, иначе из поля id файла
    (YAML персоны), иначе из имени файла."""
    if not re.search(r"\.ya?ml$", filename or "", re.IGNORECASE):
        raise ModeError("Нужен файл .yaml или .yml")
    try:
        raw = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ModeError("Файл не в UTF-8")
    raw = raw.replace("\r\n", "\n")
    data = _parse(raw)
    if mode:
        mode = check_mode_id(mode)
    else:
        own_id = data.get("id")
        mode = (suggest_id(str(own_id)) if isinstance(own_id, str) else "") or suggest_id(filename)
        if not mode:
            raise ModeError("Не получилось взять id из имени файла — переименуйте файл латиницей")
        mode = check_mode_id(mode)
    if not overwrite and mode in _files(persona):
        raise ModeError(f"Режим {mode} уже есть", 409)
    raw, added = _with_triggers_from_persona(raw, data)
    result = save(persona, mode, raw)
    if added:
        result["warnings"].insert(0, "Имена-обращения взяты из features.trigger_words файла")
    return result


def delete(persona: str, mode: str) -> dict:
    """Удалить свой файл режима (копия — в .deleted). Встроенный удалить
    нельзя — его можно перекрыть своим; свой поверх встроенного — вернёт его."""
    path = _files(persona).get(mode)
    if path is None:
        raise ModeError("Режим не найден", 404)
    if _is_builtin(path):
        raise ModeError("Встроенный режим — часть проекта: его можно переписать своим, но не удалить")
    trash = path.parent / TRASH_DIR
    with yaml_write_lock:
        trash.mkdir(exist_ok=True)
        path.replace(trash / f"{mode}-{time.strftime('%Y%m%d-%H%M%S')}.yaml")
    logger.info(f"[Modes] {persona}: режим {mode} удалён (копия в {trash})")
    _refresh_live(persona)
    result = list_modes(persona)
    result["reset"] = mode in _files(persona)
    return result

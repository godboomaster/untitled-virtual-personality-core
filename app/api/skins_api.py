"""Библиотека скинов веб-интерфейса: файлы скинов и назначения персонам.

Раньше скины жили в localStorage браузера (у каждого браузера свои, лимит
квоты ~5 МБ). Теперь библиотека — на сервере, общая для всех браузеров:

    data/skins/<skin_id>.json      метаданные скина: имя, автор, версия,
                                   контракт, экраны → хэш файла, цвета
    data/skins/files/<sha256>.html файлы экранов; одинаковый HTML (legacy-файл
                                   со всеми тремя экранами, копия скина)
                                   хранится один раз
    data/skins/assignments.json    {id персоны: skin_id}
    data/skins/hidden_builtins.json [id встроенных скинов, удалённых из библиотеки]

Встроенные скины (id с префиксом builtin-) поставляются с фронтом и на
сервере не хранятся — назначать их можно, читать/править нельзя. «Удаление»
встроенного скина скрывает его из библиотеки (и снимает с персон); вернуть
все скрытые — restore_builtins.
Файлы в запросах передаются списком files + ссылки screens {экран: индекс}:
комбинированный файл на три экрана уходит по сети один раз.
"""

import hashlib
import json
import logging
import re
import secrets
import threading
import time
from pathlib import Path

from pydantic import BaseModel

from app.api.security import atomic_write_text, is_safe_id, safe_join
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

SCREENS = ("chat", "dossier", "room")
BUILTIN_PREFIX = "builtin-"
MAX_FILE_BYTES = 3 * 1024 * 1024  # как SKIN_MAX_BYTES во фронте (engine.ts)
# Тело POST/PUT /api/skins целиком (проверяется до разбора JSON, см.
# security.BodySizeLimit): до трёх файлов по MAX_FILE_BYTES, запас на
# JSON-экранирование HTML (кавычки, переводы строк) и на метаданные/цвета
MAX_BODY_BYTES = len(SCREENS) * MAX_FILE_BYTES * 5 // 4 + 1024 * 1024
MAX_SKINS = 200
MAX_TOTAL_BYTES = 300 * 1024 * 1024  # все файлы библиотеки вместе
MAX_COLORS = 400
_MAX_TEXT = 200  # имя/автор/версия

_COLOR_NAME_RE = re.compile(r"^--[A-Za-z0-9_-]{1,100}$")
# Цвет: hex или rgb()/rgba()/hsl()/hsla() с числами — значение уходит в CSS
# скина и каркаса, поэтому никаких произвольных строк
_COLOR_VALUE_RE = re.compile(
    r"^(#[0-9a-fA-F]{3,8}|(rgb|rgba|hsl|hsla)\(\s*[0-9.,%\s/+-]{1,60}\))$"
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# id пользовательского скина — ровно то, что выдаёт create_skin: иначе
# «assignments» (имя файла назначений) или любое другое имя в data/skins
# сошло бы за скин (DELETE /api/skins/assignments стёр бы все назначения)
_SKIN_ID_RE = re.compile(r"^sk_[0-9a-f]{12}$")
# id встроенного скина (поставляется с фронтом) — только для назначения
_BUILTIN_ID_RE = re.compile(rf"^{BUILTIN_PREFIX}[a-z0-9][a-z0-9-]{{0,55}}$")

# Все изменения библиотеки — под одним локом: метаданные, файлы и назначения
# меняются согласованно (удаление скина снимает назначения и чистит файлы)
_lock = threading.Lock()


class SkinCreate(BaseModel):
    name: str
    author: str | None = None
    version: str | None = None
    contract: int = 1
    files: list[str]
    screens: dict[str, int]
    colors: dict[str, str] = {}
    hue_shift: float = 0


class SkinUpdate(BaseModel):
    name: str | None = None
    author: str | None = None
    version: str | None = None
    contract: int | None = None
    files: list[str] = []
    # экран → индекс в files; None — убрать экран из скина
    screens: dict[str, int | None] | None = None
    colors: dict[str, str] | None = None
    hue_shift: float | None = None


class SkinAssign(BaseModel):
    skin_id: str | None = None


class SkinError(Exception):
    """Ошибка запроса к библиотеке: status — HTTP-код для эндпоинта."""

    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def _root() -> Path:
    return data_dir() / "skins"


def _files_dir() -> Path:
    return _root() / "files"


def _assign_path() -> Path:
    return _root() / "assignments.json"


def _hidden_path() -> Path:
    return _root() / "hidden_builtins.json"


def _new_skin_id() -> str:
    return "sk_" + secrets.token_hex(6)


def _skin_path(skin_id: str) -> Path | None:
    if not isinstance(skin_id, str) or not _SKIN_ID_RE.match(skin_id):
        return None
    return safe_join(_root(), skin_id, ".json")


def _file_path(digest: str) -> Path | None:
    if not _HASH_RE.match(digest or ""):
        return None
    return _files_dir() / f"{digest}.html"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        logger.exception(f"[skins] Не прочитан {path}")
        return default


def _write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=1))


def _load_skin(skin_id: str) -> dict | None:
    path = _skin_path(skin_id)
    if path is None or not path.is_file():
        return None
    data = _read_json(path, None)
    return data if isinstance(data, dict) else None


def _scan_skins() -> tuple[list[dict], bool]:
    """(скины библиотеки, все ли файлы скинов прочитались)."""
    root = _root()
    if not root.is_dir():
        return [], True
    out = []
    intact = True
    for path in root.glob("*.json"):
        if not _SKIN_ID_RE.match(path.stem):
            continue
        data = _read_json(path, None)
        if isinstance(data, dict) and data.get("id") == path.stem:
            out.append(data)
        else:
            intact = False
    return out, intact


def _all_skins() -> list[dict]:
    return _scan_skins()[0]


def _load_assignments() -> dict[str, str]:
    data = _read_json(_assign_path(), {})
    if not isinstance(data, dict):
        return {}
    return {p: s for p, s in data.items() if is_safe_id(p) and is_safe_id(s)}


def _load_hidden() -> list[str]:
    data = _read_json(_hidden_path(), [])
    if not isinstance(data, list):
        return []
    return [s for s in data if isinstance(s, str) and _BUILTIN_ID_RE.match(s)]


def _meta(skin: dict) -> dict:
    """Метаданные скина для списка: без HTML, с размерами файлов экранов."""
    sizes = {}
    for screen, digest in (skin.get("screens") or {}).items():
        path = _file_path(digest)
        try:
            sizes[screen] = path.stat().st_size if path else 0
        except OSError:
            sizes[screen] = 0
    return {
        "id": skin["id"],
        "name": skin.get("name") or skin["id"],
        "author": skin.get("author"),
        "version": skin.get("version"),
        "contract": skin.get("contract") or 1,
        "screens": dict(skin.get("screens") or {}),
        "sizes": sizes,
        "colors": dict(skin.get("colors") or {}),
        "hue_shift": skin.get("hue_shift") or 0,
        "created_at": skin.get("created_at"),
        "updated_at": skin.get("updated_at"),
    }


def _clean_text(value, field: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise SkinError(f"Не задано поле {field}")
        return None
    value = str(value).strip()
    if required and not value:
        raise SkinError(f"Поле {field} не может быть пустым")
    if len(value) > _MAX_TEXT:
        raise SkinError(f"Поле {field} длиннее {_MAX_TEXT} символов")
    return value or None


def _clean_colors(colors: dict) -> dict[str, str]:
    if len(colors) > MAX_COLORS:
        raise SkinError(f"Слишком много цветов (больше {MAX_COLORS})")
    out = {}
    for name, value in colors.items():
        value = str(value).strip()
        if not _COLOR_NAME_RE.match(name):
            raise SkinError(f"Недопустимое имя CSS-переменной: {name[:60]!r}")
        if not _COLOR_VALUE_RE.match(value):
            raise SkinError(f"Недопустимый цвет для {name}: {value[:60]!r}")
        out[name] = value
    return out


def _clean_hue(value) -> float:
    try:
        hue = float(value)
    except (TypeError, ValueError):
        raise SkinError("hue_shift должен быть числом")
    if not -360 <= hue <= 360:
        raise SkinError("hue_shift вне диапазона -360…360")
    return round(hue, 2)


def _files_total() -> int:
    total = 0
    d = _files_dir()
    if d.is_dir():
        for path in d.glob("*.html"):
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def _store_files(files: list[str], refs: dict[str, int | None]) -> dict[str, str | None]:
    """Сохранить файлы, на которые ссылаются экраны: {экран: sha256 | None}."""
    for screen in refs:
        if screen not in SCREENS:
            raise SkinError(f"Неизвестный экран: {screen[:30]!r}")
    if len(files) > len(SCREENS):
        raise SkinError("Не больше трёх файлов за запрос")
    encoded = []
    for i, html in enumerate(files):
        raw = html.encode("utf-8")
        if len(raw) > MAX_FILE_BYTES:
            raise SkinError(f"Файл №{i + 1} больше {MAX_FILE_BYTES // 1024 // 1024} МБ", 413)
        if not raw.strip():
            raise SkinError(f"Файл №{i + 1} пустой")
        encoded.append(raw)
    for screen, index in refs.items():
        if index is not None and not 0 <= index < len(encoded):
            raise SkinError(f"Экран {screen}: нет файла №{index}")

    used = {index for index in refs.values() if index is not None}
    digests = {i: hashlib.sha256(encoded[i]).hexdigest() for i in used}
    new = {digests[i]: encoded[i] for i in used if not _file_path(digests[i]).is_file()}
    if new and _files_total() + sum(len(raw) for raw in new.values()) > MAX_TOTAL_BYTES:
        raise SkinError(f"Библиотека скинов переполнена (больше {MAX_TOTAL_BYTES // 1024 // 1024} МБ) — удалите ненужные скины", 413)
    for digest, raw in new.items():
        atomic_write_text(_file_path(digest), raw.decode("utf-8"))
    return {screen: (digests[index] if index is not None else None) for screen, index in refs.items()}


def _gc_files() -> None:
    """Удалить файлы, на которые больше не ссылается ни один скин."""
    skins, intact = _scan_skins()
    if not intact:
        # Непрочитанный файл скина (битый JSON) — его экраны неизвестны:
        # сборка мусора удалила бы их HTML безвозвратно. Лучше пропустить
        logger.warning("[skins] Сборка мусора файлов пропущена: есть непрочитанные скины")
        return
    used = set()
    for skin in skins:
        used.update((skin.get("screens") or {}).values())
    d = _files_dir()
    if not d.is_dir():
        return
    for path in d.glob("*.html"):
        if path.stem not in used:
            path.unlink(missing_ok=True)


# ── Публичные операции (вызываются из server.py через asyncio.to_thread) ──


def list_skins() -> list[dict]:
    skins = sorted(_all_skins(), key=lambda s: s.get("created_at") or 0)
    return [_meta(s) for s in skins]


def get_skin(skin_id: str) -> dict:
    """Скин целиком: метаданные + {sha256: html} файлов его экранов."""
    skin = _load_skin(skin_id)
    if skin is None:
        raise SkinError(f"Скин '{skin_id}' не найден", 404)
    files = {}
    for digest in set((skin.get("screens") or {}).values()):
        path = _file_path(digest)
        try:
            files[digest] = path.read_text(encoding="utf-8")
        except (OSError, AttributeError):
            logger.warning(f"[skins] У скина {skin_id} потерян файл {digest}")
    return {"skin": _meta(skin), "files": files}


def create_skin(req: SkinCreate) -> dict:
    name = _clean_text(req.name, "name", required=True)
    colors = _clean_colors(req.colors)
    hue = _clean_hue(req.hue_shift)
    if not any(i is not None for i in req.screens.values()):
        raise SkinError("В скине нет ни одного экрана")
    with _lock:
        if len(_all_skins()) >= MAX_SKINS:
            raise SkinError(f"В библиотеке уже {MAX_SKINS} скинов — удалите ненужные", 413)
        screens = {s: d for s, d in _store_files(req.files, req.screens).items() if d}
        skin_id = _new_skin_id()
        while _skin_path(skin_id).exists():
            skin_id = _new_skin_id()
        now = int(time.time() * 1000)
        skin = {
            "id": skin_id,
            "name": name,
            "author": _clean_text(req.author, "author"),
            "version": _clean_text(req.version, "version"),
            "contract": max(1, int(req.contract or 1)),
            "screens": screens,
            "colors": colors,
            "hue_shift": hue,
            "created_at": now,
            "updated_at": now,
        }
        _write_json(_skin_path(skin_id), skin)
    logger.info(f"[skins] Создан скин {skin_id} «{name}» (экраны: {', '.join(screens)})")
    return _meta(skin)


def update_skin(skin_id: str, req: SkinUpdate) -> dict:
    with _lock:
        skin = _load_skin(skin_id)
        if skin is None:
            raise SkinError(f"Скин '{skin_id}' не найден", 404)
        if req.name is not None:
            skin["name"] = _clean_text(req.name, "name", required=True)
        if req.author is not None:
            skin["author"] = _clean_text(req.author, "author")
        if req.version is not None:
            skin["version"] = _clean_text(req.version, "version")
        if req.contract is not None:
            skin["contract"] = max(1, int(req.contract))
        if req.colors is not None:
            skin["colors"] = _clean_colors(req.colors)
        if req.hue_shift is not None:
            skin["hue_shift"] = _clean_hue(req.hue_shift)
        replaced = False
        if req.screens:
            screens = dict(skin.get("screens") or {})
            for screen, digest in _store_files(req.files, req.screens).items():
                if digest:
                    screens[screen] = digest
                else:
                    screens.pop(screen, None)
            if not screens:
                raise SkinError("В скине не останется ни одного экрана — удалите скин целиком")
            skin["screens"] = screens
            replaced = True
        skin["updated_at"] = int(time.time() * 1000)
        _write_json(_skin_path(skin_id), skin)
        if replaced:
            _gc_files()
    return _meta(skin)


def delete_skin(skin_id: str) -> list[str]:
    """Удалить скин и снять его со всех персон; возвращает, с каких сняли.
    Встроенный скин не удаляется, а скрывается из библиотеки."""
    if isinstance(skin_id, str) and skin_id.startswith(BUILTIN_PREFIX):
        return _hide_builtin(skin_id)
    with _lock:
        path = _skin_path(skin_id)
        if path is None or not path.is_file():
            raise SkinError(f"Скин '{skin_id}' не найден", 404)
        path.unlink()
        assignments = _load_assignments()
        unassigned = [p for p, s in assignments.items() if s == skin_id]
        if unassigned:
            _write_json(_assign_path(), {p: s for p, s in assignments.items() if s != skin_id})
        _gc_files()
    logger.info(f"[skins] Удалён скин {skin_id} (снят с персон: {unassigned or '—'})")
    return unassigned


def _hide_builtin(skin_id: str) -> list[str]:
    if not _BUILTIN_ID_RE.match(skin_id):
        raise SkinError(f"Скин '{skin_id}' не найден", 404)
    with _lock:
        hidden = _load_hidden()
        if skin_id not in hidden:
            _write_json(_hidden_path(), hidden + [skin_id])
        assignments = _load_assignments()
        unassigned = [p for p, s in assignments.items() if s == skin_id]
        if unassigned:
            _write_json(_assign_path(), {p: s for p, s in assignments.items() if s != skin_id})
    logger.info(f"[skins] Скрыт встроенный скин {skin_id} (снят с персон: {unassigned or '—'})")
    return unassigned


def hidden_builtins() -> list[str]:
    return _load_hidden()


def restore_builtins() -> list[str]:
    """Вернуть в библиотеку все скрытые встроенные скины; возвращает их id."""
    with _lock:
        hidden = _load_hidden()
        if hidden:
            _hidden_path().unlink(missing_ok=True)
    return hidden


def get_assignments() -> dict[str, str]:
    return _load_assignments()


def assign_skin(persona: str, skin_id: str | None) -> None:
    with _lock:
        if skin_id is not None:
            if not is_safe_id(skin_id):
                raise SkinError("Недопустимый id скина")
            if skin_id.startswith(BUILTIN_PREFIX):
                if not _BUILTIN_ID_RE.match(skin_id):
                    raise SkinError("Недопустимый id встроенного скина")
            elif _load_skin(skin_id) is None:
                raise SkinError(f"Скин '{skin_id}' не найден", 404)
        assignments = _load_assignments()
        if skin_id is None:
            if assignments.pop(persona, None) is None:
                return
        else:
            assignments[persona] = skin_id
        _write_json(_assign_path(), assignments)


def rename_persona(old_id: str, new_id: str) -> None:
    """Смена id персоны: назначение скина переезжает под новый id."""
    with _lock:
        assignments = _load_assignments()
        if old_id not in assignments:
            return
        assignments[new_id] = assignments.pop(old_id)
        _write_json(_assign_path(), assignments)


def forget_persona(persona: str) -> None:
    """Персона удалена: снять с неё скин (сам скин остаётся в библиотеке)."""
    with _lock:
        assignments = _load_assignments()
        if assignments.pop(persona, None) is not None:
            _write_json(_assign_path(), assignments)

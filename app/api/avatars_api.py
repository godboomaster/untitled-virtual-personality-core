"""Аватары персон (веб-интерфейс): картинка в папке персоны data/api_<id>/.

Раньше аватары жили в localStorage браузера — у каждого браузера свои и
терялись при смене id персоны. Теперь файл лежит рядом с памятью персоны:
общий для всех браузеров и переезжает вместе с папкой при смене id
(settings_api.rename_persona). Фронт уже сжимает картинку до 256×256.

Отдаются data-URL-ами (а не ссылкой на файл): <img src> на эндпоинт не
прошёл бы авторизацию — Bearer-токен фронт шлёт только в заголовке fetch.
"""

import base64
import logging
import os
import re
import tempfile
from pathlib import Path

from app.api.security import safe_join
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

# MIME → расширение файла; сигнатура первых байт проверяется отдельно
_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_EXT_TO_MIME = {ext: mime for mime, ext in _TYPES.items()}
_MAX_BYTES = 1024 * 1024  # 256×256 PNG с запасом; больше — не аватар
_DATA_URL_RE = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\s]+)$")


def _persona_dir(persona: str) -> Path | None:
    return safe_join(data_dir(), persona, prefix="api_")


def _avatar_files(persona: str) -> list[Path]:
    base = _persona_dir(persona)
    if base is None or not base.is_dir():
        return []
    return [base / f"avatar.{ext}" for ext in _TYPES.values() if (base / f"avatar.{ext}").is_file()]


def _sniff(raw: bytes) -> str | None:
    # MIME по сигнатуре: заявленному в data-URL типу не доверяем
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def get_avatar(persona: str) -> str | None:
    """data-URL аватара персоны или None."""
    files = _avatar_files(persona)
    if not files:
        return None
    path = files[0]
    mime = _EXT_TO_MIME[path.suffix[1:]]
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def list_avatars(personas: list[str]) -> dict[str, str]:
    """{id: data-URL} для персон, у которых аватар задан."""
    out = {}
    for persona in personas:
        try:
            url = get_avatar(persona)
        except OSError:
            continue
        if url:
            out[persona] = url
    return out


def set_avatar(persona: str, data_url: str) -> dict:
    """Сохранить аватар из data-URL. {"ok": True} | {"ok": False, "detail"}."""
    m = _DATA_URL_RE.match(data_url or "")
    if not m:
        return {"ok": False, "detail": "Ожидается data-URL картинки PNG, JPEG или WebP"}
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", m.group(2)), validate=True)
    except ValueError:
        return {"ok": False, "detail": "Картинка повреждена (base64)"}
    if len(raw) > _MAX_BYTES:
        return {"ok": False, "detail": "Картинка больше 1 МБ"}
    mime = _sniff(raw)
    if mime is None:
        return {"ok": False, "detail": "Файл не похож на PNG, JPEG или WebP"}
    base = _persona_dir(persona)
    if base is None:
        return {"ok": False, "detail": "Недопустимый id персоны"}
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"avatar.{_TYPES[mime]}"
    # Атомарно: tmp в той же папке + os.replace — читатель не увидит полфайла
    fd, tmp = tempfile.mkstemp(dir=base, prefix=".avatar-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp, target)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    # Аватар другого формата от прошлой загрузки больше не нужен
    for old in _avatar_files(persona):
        if old != target:
            old.unlink(missing_ok=True)
    logger.info(f"[api] Аватар персоны {persona} обновлён ({mime}, {len(raw)} байт)")
    return {"ok": True}


def delete_avatar(persona: str) -> bool:
    """Удалить аватар; False — его и не было."""
    files = _avatar_files(persona)
    for path in files:
        path.unlink(missing_ok=True)
    return bool(files)

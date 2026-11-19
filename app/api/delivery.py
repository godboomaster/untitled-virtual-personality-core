"""Addon-owned API delivery. Other core clients leave these messages to the addon's app.

An addon may declare api_client_prefixes = {persona_id: client_id_prefix}.
This is delivery routing, not authentication: all readers still require API_TOKEN.
Persona/addon configuration is read without constructing a BotInstance.
"""

from functools import lru_cache
from pathlib import Path

import yaml

from app.core.addons import enabled_addon_names, find_addon_class, find_persona_file


@lru_cache(maxsize=128)
def _prefix(path: str, mtime: int, size: int, persona: str) -> str:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return ""
    for name in enabled_addon_names(data.get("features") or {}):
        try:
            cls = find_addon_class(name)
            prefixes = getattr(cls, "api_client_prefixes", {})
            prefix = prefixes.get(persona, "") if isinstance(prefixes, dict) else ""
            if isinstance(prefix, str) and prefix:
                return prefix
        except Exception:
            continue  # Unavailable optional addons do not change ordinary API delivery.
    return ""


def client_prefix(persona: str) -> str:
    path = find_persona_file(persona)
    if path is None:
        return ""
    try:
        stat = path.stat()
        return _prefix(str(path), stat.st_mtime_ns, stat.st_size, persona)
    except (OSError, ValueError, yaml.YAMLError):
        return ""


def client_allowed(persona: str, client_id: str) -> bool:
    prefix = client_prefix(persona)
    return not prefix or client_id.startswith(prefix)

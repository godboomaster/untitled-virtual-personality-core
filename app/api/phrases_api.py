"""Просмотр и правка всего банка реплик веб-персоны без загрузки BotInstance."""

import hashlib
import json
from string import Formatter

from app.core import persona_modes
from app.core.addons import persona_dirs
from app.core.presence import web_context
from app.core.language import detect_language, persona_language
from app.features import flavor_text as ft


class PhraseError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid"):
        super().__init__(message)
        self.status, self.code = status, code


def _revision(variants: list[str]) -> str:
    return hashlib.sha256(json.dumps(variants, ensure_ascii=False).encode()).hexdigest()[:20]


def _groups(bank: dict, data: dict, modes: dict) -> list[dict]:
    groups = []
    profiles = ft._error_profiles(data["system_prompt"], modes, data.get("voice_examples"))
    manual = bank.get("manual") or {}

    def add(path, values, section, key, *, mode="", language=None, bucket=None, stale=False):
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values) or not values:
            return
        group_id = ft.phrase_group_id(path)
        edited = group_id in manual
        allowed, required = set(), set()
        if section == "phrases":
            spec = ft._PHRASE_KEYS.get(key, {})
            allowed, required = spec.get("ph", set()), spec.get("req", set())
        elif section == "kinds":
            allowed = {"host", "element", "text", "detail"}
            if bucket == "err":
                required = {"detail"}
            else:
                allowed.discard("detail")
        groups.append({
            "id": group_id, "section": section, "key": key, "bucket": bucket,
            "mode": mode, "mode_name": str((modes.get(mode) or {}).get("name") or mode),
            "language": language or next((detect_language(v) for v in values if detect_language(v)),
                                         persona_language(data["system_prompt"]) or "ru"),
            "variants": values, "revision": _revision(values), "edited": edited,
            "stale": bool(stale and not edited),
            "placeholders": sorted(allowed), "required_placeholders": sorted(required),
            "max_length": 320 if section == "errors" else 2000,
        })

    for mode, entry in (bank.get("errors") or {}).items():
        if not isinstance(entry, dict):
            continue
        stale = entry.get("prompt_hash") != (profiles.get(mode) or {}).get("prompt_hash")
        for lang in ("ru", "en"):
            for key, values in ((entry.get("languages") or {}).get(lang) or {}).items():
                if key in ft._ERROR_SPECS:
                    add(["errors", mode, "languages", lang, key], values, "errors", key,
                        mode=mode, language=lang, stale=stale)
    stale = (bank.get("_meta") or {}).get("prompt_hash") != ft._prompt_hash(data["system_prompt"])
    for kind, buckets in (bank.get("kinds") or {}).items():
        if kind not in ft._BANK_KINDS or not isinstance(buckets, dict):
            continue
        for bucket in ("ok", "err"):
            add(["kinds", kind, bucket], buckets.get(bucket), "kinds", kind,
                bucket=bucket, stale=stale)
    for key, values in (bank.get("phrases") or {}).items():
        if key in ft._PHRASE_KEYS:
            add(["phrases", key], values, "phrases", key, stale=stale)
    return groups


def _persona(persona: str):
    from app.api.runtime import _load_persona_yaml
    data = _load_persona_yaml(persona)
    if not data or not data.get("system_prompt"):
        raise PhraseError("Персона не найдена", 404, "not_found")
    return data, persona_modes.load_modes(persona, persona_dirs())


def list_phrases(persona: str) -> dict:
    data, modes = _persona(persona)
    context = web_context(persona)
    with ft._BANK_LOCK:
        bank = ft._load_bank(context)
    return {"persona": persona, "groups": _groups(bank, data, modes),
            "generating": context in ft._GEN_STARTED}


def _validate(group: dict, variants: list[str]) -> list[str]:
    limit = {"errors": 8, "kinds": 15, "phrases": 5}[group["section"]]
    if not variants or len(variants) > limit:
        raise PhraseError(f"Допустимо от 1 до {limit} вариантов", code="count")
    cleaned = [v.strip() for v in variants]
    if any(not v or len(v) > group["max_length"] for v in cleaned):
        raise PhraseError(f"Фразы не должны быть пустыми или длиннее {group['max_length']} символов",
                          code="length")
    if len({v.casefold() for v in cleaned}) != len(cleaned):
        raise PhraseError("Варианты не должны повторяться", code="duplicates")
    if group["section"] == "errors":
        if len(ft._error_variants(cleaned, group["language"])) != len(cleaned):
            raise PhraseError("Сохраните язык раздела; в ошибках нельзя использовать подстановки, "
                              "служебные маркеры или названия провайдеров", code="error_text")
    else:
        for text in cleaned:
            try:
                fields = {field for _, field, spec, conversion in Formatter().parse(text)
                          if field is not None}
                simple = all(not spec and not conversion for _, field, spec, conversion in Formatter().parse(text)
                             if field is not None)
            except ValueError:
                fields, simple = {"invalid"}, False
            if (not simple or fields - set(group["placeholders"])
                    or not set(group["required_placeholders"]) <= fields):
                raise PhraseError("Сохраните обязательные подстановки и не добавляйте неизвестные",
                                  code="placeholders")
    return cleaned


def save_phrases(persona: str, group_id: str, revision: str, variants: list[str]) -> dict:
    data, modes = _persona(persona)
    context = web_context(persona)
    with ft._BANK_LOCK:
        bank = ft._load_bank(context)
        group = next((g for g in _groups(bank, data, modes) if g["id"] == group_id), None)
        if group is None:
            raise PhraseError("Группа фраз не найдена", 404, "not_found")
        if group["revision"] != revision:
            raise PhraseError("Фразы уже изменились. Обновите список перед сохранением", 409, "conflict")
        cleaned = _validate(group, variants)
        bank.setdefault("manual", {})[group_id] = cleaned
        ft._save_bank(context, bank, strict=True)
        updated = next(g for g in _groups(bank, data, modes) if g["id"] == group_id)
    return {"ok": True, "group": updated}

"""Общие защитные примитивы API-слоя.

Задача №6 аудита: строки из тела/query запроса (имя персоны в первую
очередь) без единой точки валидации доходили до Path(...) как есть —
``persona="../../persona_template"`` превращал ``_PERSONAS_DIR / f"{persona}.yaml"``
в путь вне app/personas/, а ``context=f"api_{persona}"`` тем же способом уводил
BotInstance вне data/. Раньше свой regex был только в settings_api
(создание персоны) — остальные функции (get_persona_config,
update_persona_proactive, save_persona_yaml, duplicate_persona,
runtime.get_persona_info и т.д.) строили путь без проверки вовсе.

Здесь — единственное место, где определён допустимый формат id персоны
(и вообще «безопасного» id вроде черновика персоны), плюс safe_join —
защита в глубину поверх regex на случай путей, которые придут в обход него.
Плюс безопасная запись .env (см. persist_env/remove_env).
"""

import re
import threading
from pathlib import Path
from typing import Annotated

from fastapi import Path as ApiPath
from fastapi import Query as ApiQuery
from pydantic import AfterValidator

# Единственная реализация — app/core/atomic_io.py (задача №6 аудита, хвост:
# здесь раньше лежала вторая копия tmp+os.replace без fsync и без сохранения
# прав файла). Реэкспорт — чтобы не трогать импорты `from app.api.security
# import atomic_write_text` по всему проекту (settings_api и т.д.).
from app.core.atomic_io import atomic_write_text  # noqa: F401

# id персоны = имя YAML-файла в app/personas/ (и черновика в data/persona_drafts/,
# формат тот же): латиница, цифры, "_", "-", 1..64 символов. "/" и ".." в
# алфавит не входят — traversal-строка просто не пройдёт regex.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PERSONA_ID_RE = SAFE_ID_RE  # алиас: так понятнее в местах про персон конкретно


def is_safe_id(value) -> bool:
    return isinstance(value, str) and bool(SAFE_ID_RE.match(value))


def _check_safe_id(value: str) -> str:
    if not is_safe_id(value):
        raise ValueError(
            "недопустимый id: разрешены латиница, цифры, «_» и «-» (1-64 символов)"
        )
    return value


# ── Аннотации для pydantic-схем и FastAPI path/query-параметров ────────
# Annotated с pattern защищает эндпоинт ДО того, как строка попадёт в
# бизнес-логику: невалидное имя → 422, а не 404/500 где-то внутри. Новый
# эндпоинт, использующий эти типы вместо голого str, получает защиту
# автоматически — не нужно помнить регекс на каждом месте.
SafeId = Annotated[str, AfterValidator(_check_safe_id)]
PersonaId = SafeId

PersonaIdPath = Annotated[
    str, ApiPath(pattern=SAFE_ID_RE.pattern, description="id персоны (имя YAML-файла)")
]
PersonaIdQuery = Annotated[
    str, ApiQuery(pattern=SAFE_ID_RE.pattern, description="id персоны (имя YAML-файла)")
]


def safe_join(base: Path, name: str, suffix: str = "", prefix: str = "") -> Path | None:
    """``base / f"{prefix}{name}{suffix}"``, только если результат не выходит
    за пределы ``base``. None — имя не прошло формат ИЛИ (запасной рубеж)
    итоговый путь после resolve() всё равно оказался снаружи base (символьные
    ссылки, нестандартный base и т.п.) — так путь физически не может
    указывать наружу, даже если regex-проверку выше по стеку забыли сделать
    или обойти.

    Валидируется ИМЕННО ``name`` (то, что реально пришло из запроса) — префикс
    вроде "api_" (контекст персоны в data/) не в счёт лимита длины имени,
    иначе персона у самой границы 64 символов ложно бы не проходила.
    """
    if not is_safe_id(name):
        return None
    base_r = base.resolve()
    candidate = base / f"{prefix}{name}{suffix}"
    try:
        candidate_r = candidate.resolve()
        candidate_r.relative_to(base_r)
    except (ValueError, OSError):
        return None
    return candidate


# ── Безопасная запись .env ──────────────────────────────────────────────
# python-dotenv (set_key/unset_key) уже пишет атомарно (tmp-файл в той же
# директории + os.replace, права исходного файла сохраняются) и корректно
# квотирует значение — перенос строки внутри значения остаётся ВНУТРИ
# кавычек и не порождает вторую строку KEY=... при обратном чтении. Здесь
# добавляем: проверку имени переменной, отказ на \r/\n/NUL (даже с учётом
# безопасного квотирования — лишняя строка в .env не должна появляться и
# по форме, не только по факту парсинга) и общий лок против гонки конкурентных
# запросов API, которые пишут в один файл.
_env_lock = threading.Lock()

ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def validate_env_value(value: str) -> None:
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise ValueError("значение не может содержать переносы строк или NUL-байт")


def persist_env(path: Path, var: str, value: str) -> None:
    """Записать/обновить переменную в .env атомарно и под локом."""
    if not ENV_VAR_RE.match(var):
        raise ValueError(f"недопустимое имя переменной окружения: {var!r}")
    validate_env_value(value)
    from dotenv import set_key
    with _env_lock:
        set_key(str(path), var, value, quote_mode="always")


def remove_env(path: Path, var: str) -> None:
    """Удалить переменную из .env атомарно и под локом (нет файла/ключа — no-op)."""
    if not ENV_VAR_RE.match(var):
        raise ValueError(f"недопустимое имя переменной окружения: {var!r}")
    from dotenv import unset_key
    with _env_lock:
        if path.exists():
            unset_key(str(path), var)


# ── Общий лок для read-modify-write YAML персон ────────────────────────
# YAML персоны правится в settings_api по схеме read → merge → write
# (safe_load всего файла, правка словаря, safe_dump обратно) — без лока
# конкурентные запросы (например, автосохранение формы) чередуют чтение и
# запись и теряют чужие правки. Атомарность самой записи — write_text во
# temp-файл + os.replace, лок только сериализует read-modify-write целиком.
yaml_write_lock = threading.Lock()


# ── Безопасный path-сегмент из произвольной строки (chat_id, user_id...) ──
# id персоны идёт через SAFE_ID_RE (строгий формат, отказ на невалидном
# вводе — обосновано: имя персоны выбирает сам проект). chat_id/user_id —
# формат диктует Telegram/фронтенд, не наш код (например, у групп он
# отрицательный: "-1001234567890"), поэтому здесь не отказ, а приведение к
# безопасному виду: любой символ вне "буква/цифра (в т.ч. юникод)/_/-"
# заменяется на "_" (\w — как в исходном re.sub(r"[^\w\-]", "_", ...), что
# уже было продублировано в app/api/memory_wipe.py::_todo_file — сведено
# сюда как общий helper). Traversal невозможен по построению — "/", "\\",
# ".." в \w и "-" не входят и превращаются в "_", какой бы алфавит ни был.
_UNSAFE_SEGMENT_RE = re.compile(r"[^\w-]+", re.UNICODE)


def safe_segment(value) -> str:
    s = _UNSAFE_SEGMENT_RE.sub("_", str(value))
    s = s[:128]
    return s or "_"

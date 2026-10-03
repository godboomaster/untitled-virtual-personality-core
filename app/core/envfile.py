"""Загрузка .env с семантикой shell для пустых значений.

python-dotenv читает строку «KEY=   # комментарий» как значение
«# комментарий»: комментарий он отрезает только после непустого значения.
Так API_TOKEN получал текст комментария (веб ловил 401), а VPC_DATA_DIR —
папку с именем-комментарием. Здесь такая строка — пустое значение, как в
shell: «#» после пробела начинает комментарий. «KEY=#abc» (без пробела)
и значения в кавычках читаются как у python-dotenv.
"""

import os
import re
from pathlib import Path
from typing import Dict, Optional, Union

from dotenv import dotenv_values

_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=")
_EMPTY_WITH_COMMENT_RE = re.compile(r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_.]*\s*=[ \t]+#")


def read_env_file(path: Union[str, Path]) -> Dict[str, Optional[str]]:
    """Значения .env-файла (нет файла — пусто). Решает последнее
    присваивание ключа, как у python-dotenv."""
    path = Path(path)
    if not path.is_file():
        return {}
    values = dict(dotenv_values(path))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return values
    comment_only: Dict[str, bool] = {}
    for line in lines:
        m = _ASSIGN_RE.match(line)
        if m:
            comment_only[m.group(1)] = bool(_EMPTY_WITH_COMMENT_RE.match(line))
    for key, empty in comment_only.items():
        if empty and key in values:
            values[key] = ""
    return values


def load_env_file(path: Union[str, Path], override: bool = False) -> bool:
    """Замена load_dotenv: уже заданные переменные окружения не
    перезаписываются (если не override). True — файл что-то задал."""
    values = read_env_file(path)
    for key, value in values.items():
        if value is None:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
    return bool(values)

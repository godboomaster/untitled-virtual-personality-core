"""Загрузка .env (app/core/envfile.py): «KEY=   # комментарий» — пустое
значение, а не текст комментария (python-dotenv отдавал «# комментарий»:
API_TOKEN становился комментарием, веб ловил 401; VPC_DATA_DIR — папкой
с именем-комментарием). Остальное — как у python-dotenv; пустой
VPC_DATA_DIR / DATA_DIR — дефолтная папка, а не текущая.

Запуск: python3 -m scripts.test_envfile
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.envfile import load_env_file, read_env_file  # noqa: E402

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def main():
    tmp = Path(tempfile.mkdtemp(prefix="envfile_"))
    try:
        env = tmp / ".env"
        env.write_text(
            "# заголовок\n"
            "API_TOKEN=           # токен API (пусто — без авторизации)\n"
            "VPC_DATA_DIR=\t# папка данных\n"
            "EMPTY=\n"
            "PLAIN=value   # комментарий после значения\n"
            "HASH_IN_VALUE=a#b\n"
            "LEADING_HASH=#abc\n"
            'QUOTED="  # не комментарий"\n'
            "export EXPORTED= # тоже пусто\n"
            "DUP=   # сначала пусто\n"
            "DUP=final\n"
            "DUP2=first\n"
            "DUP2=   # потом пусто\n",
            encoding="utf-8",
        )
        v = read_env_file(env)
        check("API_TOKEN= # … → пусто", v.get("API_TOKEN") == "")
        check("VPC_DATA_DIR=<tab># … → пусто", v.get("VPC_DATA_DIR") == "")
        check("EMPTY= → пусто", v.get("EMPTY") == "")
        check("значение + комментарий → значение", v.get("PLAIN") == "value")
        check("a#b — значение целиком", v.get("HASH_IN_VALUE") == "a#b")
        check("=#abc без пробела — значение (как в shell)", v.get("LEADING_HASH") == "#abc")
        check("в кавычках — как есть", v.get("QUOTED") == "  # не комментарий")
        check("export KEY= # … → пусто", v.get("EXPORTED") == "")
        check("повтор: решает последнее присваивание (значение)", v.get("DUP") == "final")
        check("повтор: решает последнее присваивание (пусто)", v.get("DUP2") == "")
        check("нет файла — пусто", read_env_file(tmp / "missing.env") == {})

        # Загрузка в окружение: заданное не перезаписывается
        keys = ("API_TOKEN", "VPC_DATA_DIR", "PLAIN", "LEADING_HASH")
        saved = {k: os.environ.pop(k, None) for k in keys}
        try:
            os.environ["PLAIN"] = "from-env"
            load_env_file(env)
            check("load: API_TOKEN пустой, не комментарий", os.environ.get("API_TOKEN") == "")
            check("load: заданное окружение не перезаписано", os.environ.get("PLAIN") == "from-env")
            load_env_file(env, override=True)
            check("load override: перезаписано", os.environ.get("PLAIN") == "value")

            # Пустой VPC_DATA_DIR — дефолтная папка «data», а не текущая
            from app.core.paths import data_dir
            check("data_dir() при пустом VPC_DATA_DIR — data", data_dir() == Path("data"))
        finally:
            for k, val in saved.items():
                if val is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = val

        # Поставляемый пример: ни одно значение не стало текстом комментария
        root = Path(__file__).parent.parent
        for name in (".env.example", ".env.config"):
            vals = read_env_file(root / name)
            bad = [k for k, val in vals.items() if (val or "").lstrip().startswith("#")]
            check(f"{name}: нет значений-комментариев ({len(vals)} ключей)", not bad and len(vals) > 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

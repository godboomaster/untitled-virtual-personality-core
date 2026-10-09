"""Тест единой папки данных (app/core/paths.py, Config.DATA_DIR):

  - папка одна: VPC_DATA_DIR, иначе прежнее имя DATA_DIR, иначе data;
    пустые значения — как не заданные;
  - Config.DATA_DIR и пути баз памяти (get_db_paths) следуют за ней на
    лету — стенд, задавший только VPC_DATA_DIR, не открывает настоящие базы
    (так однажды и случилось); подмена Config.DATA_DIR в тестах снимается;
  - данные на старом месте (память по прежнему DATA_DIR, персоны по
    прежнему VPC_DATA_DIR) находятся и называются, а то, что уже на новом
    месте, — нет.

Работает во временной папке (chdir), настоящая data/ не читается.
Запуск: PYTHONPATH=. python3 -m scripts.test_data_dir
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


def env(**values):
    # VPC_DATA_DIR/DATA_DIR ровно такие (None — убрать)
    patch = {k: v for k, v in values.items() if v is not None}
    clear = [k for k, v in values.items() if v is None]
    ctx = mock.patch.dict(os.environ, patch)

    class _Env:
        def __enter__(self):
            ctx.__enter__()
            for k in clear:
                os.environ.pop(k, None)

        def __exit__(self, *a):
            return ctx.__exit__(*a)
    return _Env()


def memory(root: Path, ctx: str):
    db = root / ctx / "stm"
    db.mkdir(parents=True, exist_ok=True)
    (db / "chroma.sqlite3").write_bytes(b"")


def test_resolution():
    section("A. Одна папка данных")
    from app.core.paths import data_dir
    from app.core.config import Config, get_db_paths

    with env(VPC_DATA_DIR=None, DATA_DIR=None):
        check("ничего не задано — data", data_dir() == Path("data") and Config.DATA_DIR == "data")
    with env(VPC_DATA_DIR="/tmp/vpc", DATA_DIR=None):
        check("только VPC_DATA_DIR", data_dir() == Path("/tmp/vpc"))
    with env(VPC_DATA_DIR=None, DATA_DIR="/tmp/old"):
        check("только прежнее DATA_DIR — тоже папка данных", data_dir() == Path("/tmp/old"))
    with env(VPC_DATA_DIR="/tmp/vpc", DATA_DIR="/tmp/old"):
        check("обе заданы — VPC_DATA_DIR важнее", data_dir() == Path("/tmp/vpc"))
    with env(VPC_DATA_DIR="", DATA_DIR="/tmp/old"):
        check("пустой VPC_DATA_DIR — как не заданный", data_dir() == Path("/tmp/old"))

    with env(VPC_DATA_DIR="/tmp/stand", DATA_DIR=None):
        paths = get_db_paths("api_connor")
        check("стенд с одним VPC_DATA_DIR: базы памяти — в его папке, а не в data/",
              Config.DATA_DIR == "/tmp/stand"
              and paths["stm"] == os.path.join("/tmp/stand", "api_connor", "stm")
              and all(p.startswith("/tmp/stand/") for p in paths.values()))
    with env(VPC_DATA_DIR="/tmp/a", DATA_DIR=None):
        first = Config.DATA_DIR
        os.environ["VPC_DATA_DIR"] = "/tmp/b"
        check("Config.DATA_DIR читается на лету, а не при импорте", first == "/tmp/a" and Config.DATA_DIR == "/tmp/b")

    with env(VPC_DATA_DIR="/tmp/real", DATA_DIR=None):
        with mock.patch.object(Config, "DATA_DIR", "/tmp/patched"):
            patched = Config.DATA_DIR
        check("подмена Config.DATA_DIR в тесте работает и снимается",
              patched == "/tmp/patched" and Config.DATA_DIR == "/tmp/real")
        check("экземпляр Config видит ту же папку", Config().DATA_DIR == "/tmp/real")


def test_legacy():
    section("B. Данные на старом месте")
    from app.core.paths import legacy_data_elsewhere

    cwd = os.getcwd()
    base = Path(tempfile.mkdtemp(prefix="data_dir_"))
    os.chdir(base)
    try:
        memory(base / "data", "api_connor")
        memory(base / "data", "connor")
        (base / "data" / "personas").mkdir(parents=True)
        (base / "data" / "personas" / "lena.yaml").write_text("id: lena\n")

        with env(VPC_DATA_DIR=None, DATA_DIR=None):
            check("всё в data — ничего не найдено", legacy_data_elsewhere() == [])
        with env(VPC_DATA_DIR="data", DATA_DIR="./data"):
            check("обе указывают на одну папку разными строками — ничего", legacy_data_elsewhere() == [])

        with env(VPC_DATA_DIR=str(base / "new"), DATA_DIR=None):
            found = legacy_data_elsewhere()
        check("только VPC_DATA_DIR: память в data называется по контекстам",
              len(found) == 1 and "api_connor" in found[0] and "connor" in found[0] and "data" in found[0])

        memory(base / "new", "api_connor")
        with env(VPC_DATA_DIR=str(base / "new"), DATA_DIR=None):
            found = legacy_data_elsewhere()
        check("контекст, у которого база уже на новом месте, не называется",
              len(found) == 1 and "connor" in found[0] and "api_connor" not in found[0])

        with env(VPC_DATA_DIR=str(base / "new"), DATA_DIR=str(base / "data")):
            found = legacy_data_elsewhere()
        check("обе заданы и разные: память по DATA_DIR находится", len(found) == 1 and "connor" in found[0])

        with env(VPC_DATA_DIR=None, DATA_DIR=str(base / "olddb")):
            found = legacy_data_elsewhere()
        check("только прежнее DATA_DIR: персоны в data называются",
              any("персоны" in f and "personas" in f for f in found))
        (base / "olddb" / "personas").mkdir(parents=True)
        with env(VPC_DATA_DIR=None, DATA_DIR=str(base / "olddb")):
            check("папка персон на новом месте есть — о персонах молчим",
                  not any("персоны" in f for f in legacy_data_elsewhere()))
    finally:
        os.chdir(cwd)


def main():
    test_resolution()
    test_legacy()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

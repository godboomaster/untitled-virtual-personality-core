"""Общий helper надёжной персистентности состояния менеджеров (задача №9 аудита).

Корень группы дефектов: каждый менеджер (proactive_messaging, scenario_manager,
inventory_manager, todo_manager, chat_dossier, env_context и т.д.) сам изобретал
запись файла состояния — где-то неатомарно (``open(path, "w")``/``write_text``
поверх старого файла: аварийное завершение процесса посреди записи оставляет
0 байт или обрезанный JSON), где-то без лога (битый файл на диске тихо
проглатывался в ``except Exception: return {}`` — вся история/сценарии/досье
пропадали без единого следа в логах, а следующий ``_save()`` навсегда
закреплял потерю). Часть менеджеров (reminder_manager, calendar_manager,
learning_manager, rhythm_manager) уже делали атомарную запись правильно
(tmp-файл в той же директории + os.replace) — здесь этот же приём собран в
одном месте, чтобы остальные менеджеры не изобретали его заново и не забывали
частей (лог, подчистка временного файла при ошибке, sync на диск).

Использование:
    self._items = load_json_safe(self._file, default=[], label="Inventory")
    ...
    atomic_write_json(self._file, self._items)
"""

import contextlib
import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Запись текста через tmp-файл в той же директории + fsync + os.replace.

    tmp — в той же директории, что и ``path`` (не в системном /tmp): os.replace
    атомарен только в пределах одной файловой системы. fsync перед replace —
    данные физически на диске раньше, чем переименование сделает их видимыми
    под старым именем (без него в самый неудачный момент — обрыв питания между
    replace и flush ОС кэша — можно получить path, указывающий на нулевую
    длину). Исключение посреди записи не оставляет ``path`` усечённым/битым:
    либо остаётся старое содержимое, либо целиком новое; временный файл в
    любом случае подчищается.

    Единственная реализация на проект (раньше была продублирована в
    app/api/security.py — свой tmp+os.replace без fsync и без сохранения
    прав; security.atomic_write_text теперь просто реэкспортирует эту
    функцию, см. задачу №6 аудита, хвост).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # не все ФС/окружения поддерживают fsync — не критично
        try:
            # mkstemp создаёт tmp с правами 0600 — для НОВОГО файла это
            # разумный (более строгий) дефолт, но при ПЕРЕЗАПИСИ существующего
            # файла права не должны незаметно ужесточаться (например,
            # settings_api пишет так YAML персон, который мог быть 644/640)
            existing_mode = stat.S_IMODE(os.stat(path).st_mode)
            os.chmod(tmp_path, existing_mode)
        except OSError:
            pass  # файла ещё нет или chmod не поддерживается — оставляем tmp как есть
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any, ensure_ascii: bool = False, indent: int = 2) -> None:
    """``json.dumps`` + :func:`atomic_write_text`. Сериализация — до открытия
    tmp-файла, так что исключение (например, несериализуемый объект) вообще
    не трогает файл на диске."""
    content = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent)
    atomic_write_text(path, content)


def _quarantine_corrupt(path: Path) -> Optional[Path]:
    """Переименовывает битый файл в ``<имя>.corrupt-<unix-ts>`` рядом с ним —
    для расследования/восстановления вручную, вместо того чтобы просто
    перезаписать его дефолтом на следующем ``_save()`` и потерять содержимое
    безвозвратно и без следа."""
    try:
        dest = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        os.replace(path, dest)
        return dest
    except OSError as e:
        logger.warning(f"[atomic_io] Не удалось сохранить битый файл {path} "
                       f"как .corrupt: {e}")
        return None


def load_json_safe(path: Path, default: Any, *, label: str = "") -> Any:
    """Безопасная загрузка JSON-файла состояния.

    Файла нет — default тихо (это норма, первый запуск). Файл битый
    (JSONDecodeError, обрезан, права и т.п.) — WARNING в лог с указанием
    файла и причины, копия битого файла рядом как ``.corrupt-<ts>``, и только
    затем default. Раньше типичный код был ``except Exception: return {}`` —
    и битый файл (например, из-за одновременной записи двумя процессами до
    этого фикса) тихо стирал всю накопленную историю без единой строки в
    логе, которая объяснила бы, что произошло.
    """
    path = Path(path)
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text)
    except Exception as e:
        tag = f"[{label}] " if label else ""
        quarantined = _quarantine_corrupt(path)
        where = f", файл сохранён как {quarantined.name} для восстановления" if quarantined else ""
        logger.warning(f"{tag}Не удалось загрузить {path}: {e}{where}")
        return default


# ── Межпроцессный лок на файл ───────────────────────────────────────────
# threading.Lock в менеджерах (todo/scenario/inventory/...) сериализует
# запись ТОЛЬКО внутри одного процесса. Если данные (например,
# web_llm_state.json — задача №7 аудита) пишутся из нескольких независимых
# процессов на общий data/, поток одного процесса не видит лок другого —
# нужен лок на уровне ОС поверх самого файла.
_file_lock_warned = False


@contextlib.contextmanager
def file_lock(path):
    """Межпроцессный эксклюзивный лок на ``<path>.lock`` (path сам не трогаем
    и не открываем — так лок работает и для файлов, которых на диске ещё нет).

    POSIX — ``fcntl.flock`` (блокирующий, снимается автоматически при закрытии
    fd, так что падение процесса без явного unlock не подвешивает лок навсегда).
    Windows — ``msvcrt.locking`` (тот же принцип, 1 байт). Ни то ни другое не
    доступно (экзотическая платформа) — no-op с одним warning в лог при первом
    использовании: межпроцессная гонка возможна, но within-process
    threading.Lock в вызывающем коде по-прежнему работает.

    Использование:
        with file_lock(path):
            atomic_write_json(path, data)
    """
    global _file_lock_warned
    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+")
    try:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except ImportError:
            try:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            except ImportError:
                if not _file_lock_warned:
                    logger.warning(
                        "[atomic_io] file_lock: ни fcntl (POSIX), ни msvcrt "
                        "(Windows) не доступны на этой платформе — "
                        "межпроцессный лок работает как no-op"
                    )
                    _file_lock_warned = True
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except ImportError:
            try:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except ImportError:
                pass
        f.close()

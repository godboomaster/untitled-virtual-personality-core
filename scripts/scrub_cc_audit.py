"""Чистка уже записанных аудит-логов режима управления от секретов.

Те же правила, что при записи (app/features/cc_privacy.redact_audit_record —
единый источник): маска ввода в чувствительные поля и секретоподобных
значений, URL без фрагментов/секретных параметров, обрезка текстов страницы.

Запуск:
  python -m scripts.scrub_cc_audit                 # dry-run по data/*/computer_control/audit.jsonl*
  python -m scripts.scrub_cc_audit PATH [PATH…]    # dry-run по указанным файлам
  python -m scripts.scrub_cc_audit --apply [PATH…] # перезаписать атомарно

Dry-run печатает только счётчики по полям — сами значения не выводятся.
--apply пишет отредактированную копию во временный файл рядом и заменяет
оригинал (os.replace); незачищенная резервная копия НЕ остаётся. На время
прохода берётся тот же межпроцессный лок, что у записи аудита
(cc_privacy.audit_file_lock): бот ждёт, строки не теряются, ротация не
затирает новый файл. Если файл всё же изменился за проход (бот старой
версии без лока) — файл не заменяется, чистку надо повторить при
остановленном боте.
"""

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Iterable, List, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.paths import data_dir  # noqa: E402
from app.features.cc_privacy import (  # noqa: E402
    audit_file_lock, redact_audit_record, redact_inline)


class FileChanged(RuntimeError):
    """Файл аудита изменился за проход --apply — замена отменена."""


def _sig(path: Path) -> Tuple[int, int, int]:
    st = os.stat(path)
    return st.st_ino, st.st_size, st.st_mtime_ns


def _redact_line(line: str, counts: Counter) -> str:
    raw = line.rstrip("\n")
    if not raw.strip():
        return ""
    try:
        rec = json.loads(raw)
    except Exception:
        # Битая строка: оставляем, но без секретоподобных фрагментов
        new = redact_inline(raw)
        if new != raw:
            counts["<non-json>"] += 1
        return new + "\n"
    if not isinstance(rec, dict):
        return raw + "\n"
    new_rec, changed = redact_audit_record(rec)
    for field, n in changed.items():
        counts[field] += n
    if changed:
        counts["<records changed>"] += 1
    return json.dumps(new_rec, ensure_ascii=False) + "\n"


def _scan(path: Path) -> Tuple[int, Counter]:
    counts: Counter = Counter()
    total = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                total += 1
                _redact_line(line, counts)
    return total, counts


def _apply(path: Path, lock_timeout: float = 30.0) -> Tuple[int, Counter]:
    counts: Counter = Counter()
    total = 0
    # Тот же лок, что у audit_append: бот ждёт конца прохода, запись и
    # ротация в середине невозможны. Без лока (Windows, не дождались) —
    # только проверка «файл не менялся» перед заменой
    with audit_file_lock(path, timeout=lock_timeout):
        before = _sig(path)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                   prefix=f".{path.name}.", suffix=".tmp")
        try:
            mode = os.stat(path).st_mode & 0o777
            with os.fdopen(fd, "w", encoding="utf-8") as out, \
                    open(path, "rb") as src:
                # Ровно тот размер, что был при старте: дописанное позже не
                # читаем (иначе недописанная строка стала бы двумя битыми)
                data = src.read(before[1])
                for bline in data.splitlines(keepends=True):
                    line = bline.decode("utf-8", errors="replace")
                    if line.strip():
                        total += 1
                        out.write(_redact_line(line, counts))
                out.flush()
                os.fsync(out.fileno())
            if _sig(path) != before:
                raise FileChanged(
                    f"{path}: файл изменился во время чистки (бот пишет "
                    "аудит?) — не заменён; останови бота и повтори")
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return total, counts


def _default_paths() -> List[Path]:
    root = data_dir()
    out: List[Path] = []
    for p in sorted(root.glob("*/computer_control/audit.jsonl*")):
        if p.is_file() and not p.name.endswith(".tmp"):
            out.append(p)
    return out


def main(argv: Iterable[str] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", help="audit.jsonl файлы (по умолчанию все в data/)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="только посчитать (по умолчанию)")
    mode.add_argument("--apply", action="store_true",
                      help="перезаписать файлы атомарно, без резервной копии")
    args = ap.parse_args(list(argv) if argv is not None else None)
    paths = [Path(p) for p in args.paths] or _default_paths()
    if not paths:
        print("Аудит-логов не найдено.")
        return 0
    grand: Counter = Counter()
    grand_total = 0
    refused = 0
    for p in paths:
        if not p.is_file():
            print(f"{p}: нет файла")
            continue
        try:
            total, counts = _apply(p) if args.apply else _scan(p)
        except FileChanged as e:
            print(str(e))
            refused += 1
            continue
        grand_total += total
        grand.update(counts)
        fields = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "—"
        print(f"{p}: записей {total}; изменено: {fields}")
    verb = "Отредактировано" if args.apply else "Будет отредактировано (dry-run)"
    fields = ", ".join(f"{k}={v}" for k, v in sorted(grand.items())) or "—"
    print(f"{verb}: записей всего {grand_total}; по полям: {fields}")
    if refused:
        print(f"Не заменено файлов: {refused} (менялись во время чистки)")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

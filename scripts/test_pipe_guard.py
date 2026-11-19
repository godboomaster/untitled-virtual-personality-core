"""Ядро без читателя вывода: трей, запустивший ядро, ушёл.

Трей читает stdout/stderr ядра через трубы; упал или перезапущен — ядро
осиротело, и первая же запись в трубу кидала BrokenPipeError: полоса tqdm
«Batches» (эмбеддинг реплики) роняла ход — «Сбой пайплайна … [Errno 32]
Broken pipe», и реплика-ошибка тоже не записывалась в STM. Здесь: дочерний
Python с трубами, читающие концы закрыты; без pipe_guard print падает
(контроль сценария), с ним print, stderr, tqdm и logging проходят, а
дескрипторы 1/2 уходят на /dev/null.

Запуск: PYTHONPATH=. python3 -m scripts.test_pipe_guard
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHILD = r"""
import json, logging, os, sys
from pathlib import Path
if sys.argv[2] == "1":
    from app.core import pipe_guard
    pipe_guard.install()
    pipe_guard.install()
logging.basicConfig(level=logging.INFO)
sys.stdin.readline()
out = {}

def attempt(name, fn):
    try:
        fn()
        out[name] = "ok"
    except BrokenPipeError:
        out[name] = "EPIPE"

def bar():
    from tqdm import tqdm
    for _ in tqdm(range(3), desc="Batches", mininterval=0):
        pass

attempt("print", lambda: print("x" * 100, flush=True))
attempt("stderr", lambda: (sys.stderr.write("y\n"), sys.stderr.flush()))
try:
    import tqdm
    attempt("tqdm", bar)
except ImportError:
    out["tqdm"] = "skip"
attempt("logging", lambda: logging.getLogger("t").info("z"))
attempt("print again", lambda: print("x", flush=True))
out["isatty"] = sys.stdout.isatty()
out["fd"] = sys.stderr.fileno()
out["devnull"] = os.path.samestat(os.fstat(2), os.stat(os.devnull))
Path(sys.argv[1]).write_text(json.dumps(out))
"""

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def orphaned(guard: bool):
    """Дочерний процесс, чей читатель вывода ушёл; его отчёт и код выхода"""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "out.json"
        p = subprocess.Popen([sys.executable, "-c", CHILD, str(report), "1" if guard else "0"],
                             cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        p.stdout.close()
        p.stderr.close()
        p.stdin.write(b"go\n")
        p.stdin.close()
        code = p.wait(timeout=60)
        return (json.loads(report.read_text()) if report.exists() else {}), code


def main():
    print("Без pipe_guard (контроль)")
    out, _ = orphaned(False)
    check(f"print в трубу без читателя падает: {out.get('print')}", out.get("print") == "EPIPE")

    print("С pipe_guard")
    out, code = orphaned(True)
    for name in ("print", "stderr", "logging", "print again"):
        check(f"{name}: {out.get(name)}", out.get(name) == "ok")
    check(f"tqdm: {out.get('tqdm')}", out.get("tqdm") in ("ok", "skip"))
    check("атрибуты потока — от настоящего (isatty, fileno)",
          out.get("isatty") is False and out.get("fd") == 2)
    check("fd 2 переведён на /dev/null", out.get("devnull") is True)
    check(f"процесс выходит чисто (код {code})", code == 0)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

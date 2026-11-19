"""Ядро без запустившего: трей или панель ушли, не погасив ядро.

2026-10-28 трей Арродеса ушёл (упал/убит), ядро осталось сиротой: новый трей
его не видел, а при остановке оно повисло на выходе с открытым микрофоном
(голосовая активация) — до утра. Теперь запускавший ставит
VPC_EXIT_WITH_PARENT=1, и ядро, потеряв родителя, гаснет само: сперва
микрофон, затем сервер; повисла остановка — выход силой.

Здесь: «трей» — промежуточный Python, читающий вывод ребёнка; его убивают
SIGKILL (как падение), ребёнок должен уйти сам. Ядро — настоящий run_api
с подменённым приложением (FastAPI-заглушка), микрофоном и фоновой службой.

Запуск: PYTHONPATH=. python3 -m scripts.test_parent_watch
"""

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# «Трей»: запускает ребёнка, читает его вывод (как трей — трубой), печатает PID
LAUNCHER = r"""
import os, subprocess, sys, threading
env = dict(os.environ)
if sys.argv[1] == "1":
    env["VPC_EXIT_WITH_PARENT"] = "1"
else:
    env.pop("VPC_EXIT_WITH_PARENT", None)
p = subprocess.Popen([sys.executable, "-c", sys.argv[2]] + sys.argv[3:], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
threading.Thread(target=lambda: [None for _ in p.stdout], daemon=True).start()
print(p.pid, flush=True)
p.wait()
"""

# Один parent_watch: родитель ушёл — отметка и выход
WATCH = r"""
import os, sys, time
from pathlib import Path
from app.core import parent_watch
def gone():
    Path(sys.argv[1]).write_text("gone")
    os._exit(0)
Path(sys.argv[1] + ".installed").write_text(str(parent_watch.install(gone)))
while True:
    time.sleep(1)
"""

# Настоящий run_api: приложение — заглушка (hang — её shutdown-хук виснет),
# микрофон и служба телефона — отметки в файле
CORE = r"""
import asyncio, sys
from pathlib import Path
marks = Path(sys.argv[1])
hang = sys.argv[2] == "hang"
def mark(s):
    with open(marks, "a") as f:
        f.write(s + "\n")
import uvicorn
from fastapi import FastAPI
stub = FastAPI()
@stub.on_event("shutdown")
async def _bye():
    mark("shutdown-hook")
    if hang:
        await asyncio.sleep(3600)
_Config = uvicorn.Config
uvicorn.Config = lambda app, **kw: _Config(stub, **kw)
from app.voice import wake_listener
wake_listener.start_with_core = lambda h, p: mark("mic-on")
wake_listener.stop_with_core = lambda: mark("mic-off")
from app.core import phone_llm
phone_llm.serve = lambda: None
import app.main as m
m.ORPHAN_EXIT_SEC = 2
m.run_api()
mark("returned")
"""

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_until(cond, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_open(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        return True
    except OSError:
        return False


def launch(flag: bool, script: str, *args, env=None):
    """«Трей» с ребёнком; (трей, PID ребёнка)"""
    tray = subprocess.Popen([sys.executable, "-c", LAUNCHER, "1" if flag else "0", script, *args],
                            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    return tray, int(tray.stdout.readline())


def crash(tray):
    tray.send_signal(signal.SIGKILL)
    tray.wait(timeout=10)


def watch_only(tmp: Path):
    print("parent_watch: флаг стоит — трей упал, ребёнок уходит сам")
    mark = tmp / "watch"
    tray, pid = launch(True, WATCH, str(mark))
    wait_until(lambda: Path(str(mark) + ".installed").exists(), 30)
    check("слежка включена", Path(str(mark) + ".installed").read_text() == "True")
    time.sleep(1.5)
    check("пока трей жив — ребёнок живёт", alive(pid) and not mark.exists())
    crash(tray)
    check("трей ушёл — ребёнок за ~1 с отметился и вышел",
          wait_until(lambda: mark.exists() and not alive(pid), 5))

    print("parent_watch: флага нет (запуск из терминала) — ребёнок живёт")
    mark = tmp / "nowatch"
    tray, pid = launch(False, WATCH, str(mark))
    wait_until(lambda: Path(str(mark) + ".installed").exists(), 30)
    check("слежка не включена", Path(str(mark) + ".installed").read_text() == "False")
    crash(tray)
    time.sleep(3)
    check("трей ушёл — ребёнок живёт дальше", alive(pid) and not mark.exists())
    os.kill(pid, signal.SIGKILL)


def core(tmp: Path, hang: bool):
    marks = tmp / ("core-hang" if hang else "core")
    port = free_port()
    env = dict(os.environ, API_HOST="127.0.0.1", API_PORT=str(port), PYTHONPATH=str(ROOT))
    tray, pid = launch(True, CORE, str(marks), "hang" if hang else "ok", env=env)
    up = wait_until(lambda: port_open(port), 90)
    check(f"ядро поднялось на :{port}", up)
    if not up:
        crash(tray)
        os.kill(pid, signal.SIGKILL)
        return
    crash(tray)
    gone = wait_until(lambda: not alive(pid), 15)
    lines = marks.read_text().split() if marks.exists() else []
    if not gone:
        os.kill(pid, signal.SIGKILL)
    if hang:
        check(f"shutdown-хук повис — ядро вышло силой (отметки: {lines})", gone and "returned" not in lines)
    else:
        check(f"ядро остановилось штатно и вышло (отметки: {lines})", gone and "returned" in lines)
    check("микрофон закрыт до остановки сервера",
          "mic-off" in lines and "shutdown-hook" in lines and lines.index("mic-off") < lines.index("shutdown-hook"))
    check("порт свободен", not port_open(port))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        watch_only(tmp)
        print("run_api: трей упал — ядро гаснет само")
        core(tmp, hang=False)
        print("run_api: трей упал, остановка повисла — выход силой")
        core(tmp, hang=True)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Межпроцессный жизненный цикл Chrome пула H: Chrome пула H общий для
всех процессов бота, и перезапуск в одном процессе (закрытие → убийство по
профилю) не должен убивать Chrome, который в это окно лениво поднял другой
процесс — независимо от того, основные у того вызовы или только фоновые.

Проверяется:
  1. гонка на ДВУХ процессах: Y перезапускает Chrome, X в окне между
     закрытием и убийством ловит «Chrome не жив» и стартует лениво — старт X
     ждёт конца перезапуска Y, Chrome X остаётся жив. Контроль: без
     межпроцессного лока тот же сценарий убивает Chrome X (тест ловит гонку);
  2. последовательная форма: решение «перезапустить», принятое для старого
     Chrome, не убивает свежий Chrome, уже поднятый соседом;
  3. ожидание чужого лока ограничено: _raw_call — бюджетом вызова
     (RawCallTimeout), перезапуск — POOL_H_RESTART_WAIT_SEC (пропуск без
     убийства); локи после отказа отпущены;
  4. реентерабельность внутри процесса (смена режима в ленивом старте →
     teardown → запуск) без самоблокировки на втором fd;
  5. ФС без flock — только внутрипроцессный лок;
  6. выключение процесса бота гасит общий Chrome H только ПОСЛЕДНИМ
     пользователем (разделяемый flock <профиль>.bot-users.lock): живой сосед —
     Chrome жив; сосед упал (SIGKILL) — его регистрацию снимает ядро; без
     учёта (нет flock) — Chrome гасится безусловно. Погасивший Chrome
     снимает и rescue (иначе он пережил бы перезапуск бота);
  7. Chrome, поднятый не этим процессом: режим — по его командной строке,
     rescue — общий для процессов (<профиль>.bot-rescue);
  8. зависший Chrome пула H (жив, CDP молчит) перезапускается сам — на
     таймауте вызова (проверка в фоне) и при переподключении.

Настоящий Chrome не запускается и не убивается: «Chrome» — спящий python-
подпроцесс с --user-data-dir=<tmp-профиль> в командной строке и симлинком
SingletonLock, как у настоящего; убийство — настоящее _kill_chrome_on_profile.
Запуск: python3 -m scripts.test_pool_h_lifecycle
"""

import multiprocessing
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

WINDOW_SEC = 1.0   # окно «Chrome закрыт, ещё не добит» у перезапуска
LAUNCH_SEC = 0.3   # сколько «поднимается» фейковый Chrome


def _log(log_path: str, *parts):
    with open(log_path, "a") as f:
        f.write(" ".join(str(p) for p in parts) + "\n")


def _read_log(log_path: str):
    try:
        with open(log_path) as f:
            return [ln.split() for ln in f.read().splitlines() if ln]
    except OSError:
        return []


def _spawn_chrome(udd: str, headless: bool = True,
                  port: int = 0) -> int:
    """Фейковый Chrome на профиле: процесс с --user-data-dir (и, как у
    штатного пула H, --headless=new) в cmdline + SingletonLock «host-pid»
    (атомарная подмена симлинка). headless=False — видимый, как после rescue;
    port — ещё и --remote-debugging-port, как у настоящего Chrome пула H."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)",
         f"--user-data-dir={udd}", *(["--headless=new"] if headless else []),
         *([f"--remote-debugging-port={port}"] if port else [])],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    tmp = os.path.join(udd, f".sl-{proc.pid}")
    os.symlink(f"{socket.gethostname()}-{proc.pid}", tmp)
    os.replace(tmp, os.path.join(udd, "SingletonLock"))
    # Жнец: иначе убитый «Chrome» висит зомби у родителя, и _pid_alive
    # считает его живым до SIGKILL по грейсу (тест медленнее, смысл тот же)
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc.pid


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    # зомби (ребёнок этого процесса) — мёртв
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                             capture_output=True, text=True, timeout=5).stdout
        return bool(out.strip()) and not out.strip().startswith("Z")
    except Exception:
        return True


class _FakeClient:
    def call(self, method, params=None, session_id=None, timeout=None):
        return {}

    def close(self):
        pass


def _setup(udd: str, log_path: str, who: str, no_flock: bool = False):
    """Подмены браузерного слоя на фейки — ДО любого вызова (реальный
    профиль и реальный Chrome не трогаются никогда)."""
    import app.features.browser_actions as ba
    ba._pool_h_profile = lambda: udd
    ba._is_default_browser_profile = lambda u: False
    ba._RawCdp = lambda url=None: _FakeClient()
    ba._sweep_orphan_tabs = lambda c, p: None
    ba._POOL_H_RUNNING_MODE = None
    ba._POOL_H_MODE_PID = None
    ba._POOL_H_MODE_OVERRIDE = None
    ba._POOL_H_RESCUE_UNTIL = 0.0
    ba._POOL_H_RESCUE_SHARED = False
    ba._POOL_H_PROC = None

    def _fake_alive():
        pid = ba._pool_h_chrome_pid()
        return bool(pid) and _alive(pid) and not os.path.exists(
            os.path.join(udd, f"closed-{pid}"))

    def _fake_launch_locked():
        _log(log_path, "launch-start", who, time.time())
        time.sleep(LAUNCH_SEC)
        pid = _spawn_chrome(udd)
        _log(log_path, "launch-end", who, time.time(), pid)

    real_kill = ba._kill_chrome_on_profile

    def _logged_kill(proc, u, grace_sec=10.0):
        _log(log_path, "kill-start", who, time.time(), ba._pool_h_chrome_pid())
        out = real_kill(proc, u, grace_sec=grace_sec)
        _log(log_path, "kill-end", who, time.time())
        return out

    ba._pool_h_alive = _fake_alive
    ba._launch_pool_h_chrome_locked = _fake_launch_locked
    ba._kill_chrome_on_profile = _logged_kill
    if no_flock:
        ba._pool_life_open = lambda pool, deadline: None
    return ba


def _mp_restarter(udd, log_path, seen_pid, in_window, no_flock, q):
    """Процесс Y: перезапуск пула H. Browser.close «гасит» Chrome (порт
    мёртв), затем окно WINDOW_SEC до убийства по профилю."""
    ba = _setup(udd, log_path, "Y", no_flock)
    ba._POOL_SEEN_PID["h"] = seen_pid

    def _fake_close():
        pid = ba._pool_h_chrome_pid()
        open(os.path.join(udd, f"closed-{pid}"), "w").close()
        try:
            os.kill(pid, signal.SIGTERM)  # Chrome вышел по Browser.close
        except OSError:
            pass
        _log(log_path, "close", "Y", time.time(), pid)
        in_window.set()
        time.sleep(WINDOW_SEC)

    ba._close_pool_h_graceful = _fake_close
    try:
        q.put(("Y", ba.restart_browser("тест", cooldown_sec=0, pool="h")))
    except Exception as e:
        q.put(("Y", f"err {e!r}"))


def _mp_lazy(udd, log_path, go, no_flock, q):
    # Процесс X: только фоновый вызов — лениво поднимает Chrome пула H.
    ba = _setup(udd, log_path, "X", no_flock)
    go.wait(10)
    try:
        ba._raw_call("Browser.getVersion", pool="h", timeout=10)
        q.put(("X", ba._pool_h_chrome_pid()))
    except Exception as e:
        q.put(("X", f"err {e!r}"))


def _mp_hold_life(udd, log_path, ready, release):
    # Держит лок жизненного цикла пула H (будто запускает Chrome).
    ba = _setup(udd, log_path, "H")
    with ba._pool_h_lifecycle():
        ready.set()
        release.wait(30)


def _mp_try_life(udd, log_path, q):
    ba = _setup(udd, log_path, "T")
    try:
        with ba._pool_h_lifecycle(time.monotonic() + 1.0):
            q.put(True)
    except ba.RawCallTimeout:
        q.put(False)


def _mp_user(udd, log_path, ready, release, q):
    """Процесс-пользователь Chrome пула H: подключился (регистрация — в
    _raw_call), ждёт команды: "shutdown" — своё выключение (как выход бота),
    "exit" — просто выйти; SIGKILL от теста — падение."""
    ba = _setup(udd, log_path, "U")
    ba._close_pool_h_graceful = lambda: None
    ba._raw_call("Browser.getVersion", pool="h", timeout=10)
    ready.set()
    cmd = release.get(timeout=60)
    if cmd == "shutdown":
        ba._shutdown_pool_h("тест")
        q.put(("U", ba._pool_h_chrome_pid()))


def _race(tmp: Path, name: str, no_flock: bool):
    # Сценарий 1 → (Y-результат, X-результат, лог, pid1).
    udd = str(tmp / name / "profile")
    os.makedirs(udd)
    log_path = str(tmp / name / "events.log")
    pid1 = _spawn_chrome(udd)
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    in_window = ctx.Event()
    py = ctx.Process(target=_mp_restarter,
                     args=(udd, log_path, pid1, in_window, no_flock, q))
    px = ctx.Process(target=_mp_lazy,
                     args=(udd, log_path, in_window, no_flock, q))
    px.start()
    py.start()
    res = {}
    for _ in range(2):
        try:
            k, v = q.get(timeout=40)
            res[k] = v
        except Exception:
            break
    py.join(10)
    px.join(10)
    return res, _read_log(log_path), pid1


def main():
    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    if sys.platform == "win32":
        print("  [SKIP] posix-only (SingletonLock/fcntl)")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="pool_h_life_"))
    spawned = []
    try:
        # ── 1. Гонка двух процессов: перезапуск Y против ленивого старта X ──
        res, log, pid1 = _race(tmp, "race", no_flock=False)
        pid2 = res.get("X")
        spawned += [pid1, pid2]
        kill_end = [float(e[2]) for e in log if e[0] == "kill-end"]
        x_launch = [float(e[2]) for e in log
                    if e[0] == "launch-start" and e[1] == "X"]
        killed_x = [e for e in log if e[0] == "kill-start"
                    and len(e) > 3 and e[3] == str(pid2)]
        check("2 процесса: перезапуск Y прошёл (True)", res.get("Y") is True)
        check("2 процесса: ленивый старт X (только фоновый вызов) ждёт конца "
              "перезапуска Y — запуск после убийства",
              len(x_launch) == 1 and kill_end
              and x_launch[0] >= kill_end[0])
        check("2 процесса: Chrome, поднятый X, жив (перезапуск Y его не убил)",
              isinstance(pid2, int) and _alive(pid2) and not killed_x)
        check("2 процесса: старый Chrome убит", not _alive(pid1))

        # Контроль: без межпроцессного лока тот же сценарий воспроизводит
        # гонку — значит, проверка выше ловит именно её
        res_c, log_c, pid1c = _race(tmp, "control", no_flock=True)
        pid2c = res_c.get("X")
        spawned += [pid1c, pid2c]
        killed_c = [e for e in log_c if e[0] == "kill-start"
                    and len(e) > 3 and e[3] == str(pid2c)]
        check("контроль: без flock перезапуск Y убивает свежий Chrome X "
              "(гонка воспроизводится)",
              isinstance(pid2c, int) and bool(killed_c) and not _alive(pid2c))

        # ── 2. Решение о перезапуске для старого Chrome не убивает свежий ──
        udd = str(tmp / "stale" / "profile")
        os.makedirs(udd)
        log_path = str(tmp / "stale" / "events.log")
        ba = _setup(udd, log_path, "P")
        ba._close_pool_h_graceful = lambda: None
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        fresh = _spawn_chrome(udd)
        spawned.append(fresh)
        ba._POOL_SEEN_PID["h"] = dead.pid  # мы видели Chrome, которого уже нет
        r = ba.restart_browser("тест", cooldown_sec=0, pool="h")
        check("stale: Chrome сменён соседом — перезапуск его не убивает",
              r is True and _alive(fresh)
              and not any(e[0] == "kill-start" for e in _read_log(log_path)))
        ba._POOL_SEEN_PID["h"] = fresh  # а тот, что видели, — убивается
        r = ba.restart_browser("тест", cooldown_sec=0, pool="h")
        time.sleep(0.2)
        check("stale: Chrome, в котором залипли, перезапуск убивает",
              r is True and not _alive(fresh))

        # ── 3. Ожидание чужого лока ограничено ──
        udd = str(tmp / "busy" / "profile")
        os.makedirs(udd)
        log_path = str(tmp / "busy" / "events.log")
        ba = _setup(udd, log_path, "P")
        ba._close_pool_h_graceful = lambda: None
        live = _spawn_chrome(udd)
        spawned.append(live)
        ctx = multiprocessing.get_context("spawn")
        ready, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(target=_mp_hold_life,
                             args=(udd, log_path, ready, release))
        holder.start()
        try:
            ready.wait(20)
            ba._RAW_CLIENTS["h"] = None
            t0 = time.monotonic()
            err = None
            try:
                ba._raw_call("Browser.getVersion", pool="h", timeout=0.6)
            except Exception as e:
                err = e
            dt = time.monotonic() - t0
            check("занято другим процессом: _raw_call — RawCallTimeout в "
                  "бюджет вызова, без запуска Chrome",
                  isinstance(err, ba.RawCallTimeout) and 0.5 <= dt < 1.5
                  and not any(e[0] == "launch-start"
                              for e in _read_log(log_path)))
            saved_wait = ba.POOL_H_RESTART_WAIT_SEC
            ba.POOL_H_RESTART_WAIT_SEC = 0.5
            try:
                ba._POOL_SEEN_PID["h"] = live
                t0 = time.monotonic()
                r = ba.restart_browser("тест", cooldown_sec=0, pool="h")
                dt = time.monotonic() - t0
            finally:
                ba.POOL_H_RESTART_WAIT_SEC = saved_wait
            check("занято другим процессом: перезапуск пропущен за "
                  "POOL_H_RESTART_WAIT_SEC, Chrome не тронут",
                  r is False and dt < 1.5 and _alive(live))
            lk = ba._RAW_LOCKS["h"].acquire(blocking=False)
            if lk:
                ba._RAW_LOCKS["h"].release()
            check("после отказа локи отпущены (_RAW_LOCKS[H], глубина 0)",
                  lk and ba._POOL_LIFE["h"]["depth"] == 0
                  and ba._POOL_LIFE["h"]["fh"] is None)
        finally:
            release.set()
            holder.join(10)

        # ── 4. Реентерабельность внутри процесса ──
        done = threading.Event()

        def _nested():
            with ba._pool_h_lifecycle(time.monotonic() + 2):
                with ba._pool_h_lifecycle(time.monotonic() + 2):
                    ba._ensure_pool_h_browser()  # жив → no-op
            done.set()

        tn = threading.Thread(target=_nested, daemon=True)
        tn.start()
        check("реентерабельность: вложенный захват не блокирует сам себя",
              done.wait(3))
        # Смена режима (rescue off) в ленивом старте: teardown + запуск под
        # тем же захватом, без повторного ожидания flock
        ba._POOL_H_RUNNING_MODE = "headed"
        ba._RAW_CLIENTS["h"] = None
        t0 = time.monotonic()
        ba._raw_call("Browser.getVersion", pool="h", timeout=5)
        new_pid = ba._pool_h_chrome_pid()
        spawned.append(new_pid)
        check("смена режима в ленивом старте: teardown + запуск под одним "
              "захватом, старый Chrome убит, новый жив",
              time.monotonic() - t0 < 5 and not _alive(live)
              and new_pid != live and _alive(new_pid))
        q = ctx.Queue()
        pt = ctx.Process(target=_mp_try_life, args=(udd, log_path, q))
        pt.start()
        got = q.get(timeout=20)
        pt.join(10)
        check("после выхода из секции лок свободен для другого процесса",
              got is True)

        # ── 5. ФС без flock (ENOTSUP) — фолбэк на внутрипроцессный, а не
        # «занято» с RawCallTimeout на каждом захвате ──
        import errno
        import fcntl
        real_flock = fcntl.flock

        def _nosup(fd, op):
            raise OSError(errno.ENOTSUP, "Operation not supported")

        fcntl.flock = _nosup
        try:
            t0 = time.monotonic()
            try:
                with ba._pool_h_lifecycle(time.monotonic() + 1.0):
                    entered = ba._POOL_LIFE["h"]["fh"] is None
            except ba.RawCallTimeout:
                entered = False
        finally:
            fcntl.flock = real_flock
        check("ФС без flock: секция входится сразу, только внутрипроцессно",
              entered and time.monotonic() - t0 < 0.5)

        # ── 6. Выключение процесса гасит Chrome H только ПОСЛЕДНИМ
        # пользователем (<профиль>.bot-users.lock, разделяемый flock) ──
        udd = str(tmp / "users" / "profile")
        os.makedirs(udd)
        log_path = str(tmp / "users" / "events.log")
        ba = _setup(udd, log_path, "P")
        ba._close_pool_h_graceful = lambda: None
        live = _spawn_chrome(udd)
        spawned.append(live)

        def _kills():
            return [e for e in _read_log(log_path) if e[0] == "kill-start"]

        def _connect_main():
            ba._RAW_CLIENTS["h"] = None
            ba._raw_call("Browser.getVersion", pool="h", timeout=5)

        ctx = multiprocessing.get_context("spawn")
        ready, cmdq, q = ctx.Event(), ctx.Queue(), ctx.Queue()
        pu = ctx.Process(target=_mp_user,
                         args=(udd, log_path, ready, cmdq, q))
        pu.start()
        try:
            ready.wait(20)
            _connect_main()
            fh = ba._POOL_USERS["h"]
            check("пользователь зарегистрирован при подключении; fd не "
                  "наследуется (Chrome его не удержит)",
                  bool(fh) and os.get_inheritable(fh.fileno()) is False
                  and ba._pool_users_path("h").endswith(
                      "profile.bot-users.lock")
                  and ba._pool_users_path("h").startswith(str(tmp)))
            # Идёт rescue (окно капчи в общем Chrome)
            with open(udd + ".bot-rescue", "w") as f:
                f.write(str(time.time() + 600))
            ba._shutdown_pool_h("тест")
            check("выключение при живом соседе-пользователе: Chrome H жив, "
                  "свои вкладки/сокет/регистрация сброшены",
                  _alive(live) and not _kills()
                  and ba._RAW_CLIENTS["h"] is None
                  and ba._POOL_USERS["h"] is None)
            check("выключение при живом соседе: rescue в общем Chrome "
                  "продолжается", ba.pool_h_rescue_active())
            # Убрать до переподключения: иначе оно сменит режим Chrome
            os.unlink(udd + ".bot-rescue")
            # Сосед падает (SIGKILL): ядро снимает его flock — «вечного
            # пользователя» нет, следующее выключение гасит Chrome
            os.kill(pu.pid, signal.SIGKILL)
            pu.join(10)
            _connect_main()
            with open(udd + ".bot-rescue", "w") as f:
                f.write(str(time.time() + 600))
            ba._shutdown_pool_h("тест")
            time.sleep(0.2)
            check("сосед упал (SIGKILL) — выключение последнего гасит Chrome H",
                  not _alive(live) and len(_kills()) == 1
                  and ba._POOL_USERS["h"] is None)
            check("Chrome погашен последним — rescue снят: перезапущенный бот "
                  "поднимет пул H в штатном режиме, а не снова видимым",
                  not ba.pool_h_rescue_active()
                  and not os.path.exists(udd + ".bot-rescue")
                  and ba._pool_h_desired_mode() == "headless")
        finally:
            if pu.is_alive():
                pu.kill()
            pu.join(5)

        # Наоборот: выходит СОСЕД, а этот процесс ещё пользуется Chrome
        live2 = _spawn_chrome(udd)
        spawned.append(live2)
        _connect_main()
        ready, cmdq, q = ctx.Event(), ctx.Queue(), ctx.Queue()
        pu = ctx.Process(target=_mp_user,
                         args=(udd, log_path, ready, cmdq, q))
        pu.start()
        try:
            ready.wait(20)
            cmdq.put("shutdown")
            k, v = q.get(timeout=30)
            pu.join(10)
            check("выход соседа не гасит Chrome H, которым пользуется этот "
                  "процесс", _alive(live2) and v == live2
                  and len(_kills()) == 1)
        finally:
            if pu.is_alive():
                pu.kill()
            pu.join(5)
        ba._shutdown_pool_h("тест")
        time.sleep(0.2)
        check("затем выключение последнего пользователя гасит Chrome H",
              not _alive(live2) and len(_kills()) == 2)

        # Без пользователей вовсе (Chrome осиротел после падения бота, никто
        # его не трогал): выключение гасит его безусловно
        live3 = _spawn_chrome(udd)
        spawned.append(live3)
        ba._shutdown_pool_h("тест")
        time.sleep(0.2)
        check("осиротевший Chrome H (пользователей нет) выключение гасит",
              not _alive(live3))

        # ФС без flock / нет разделяемых локов (msvcrt): учёт недоступен —
        # Chrome гасится, даже если сосед держит регистрацию
        live4 = _spawn_chrome(udd)
        spawned.append(live4)
        ready, cmdq, q = ctx.Event(), ctx.Queue(), ctx.Queue()
        pu = ctx.Process(target=_mp_user,
                         args=(udd, log_path, ready, cmdq, q))
        pu.start()
        try:
            ready.wait(20)
            fcntl.flock = _nosup
            try:
                _connect_main()
                unsup = ba._POOL_USERS["h"] is False
                ba._shutdown_pool_h("тест")
            finally:
                fcntl.flock = real_flock
            time.sleep(0.2)
            check("учёт пользователей недоступен — выключение гасит, как "
                  "раньше (регистрация помечена недоступной, без ретраев)",
                  unsup and not _alive(live4))
        finally:
            cmdq.put("exit")
            pu.join(10)
            if pu.is_alive():
                pu.kill()

        # ── 7. Chrome пула H, поднятый НЕ этим процессом: режим — по его
        # командной строке, rescue — общий для процессов (<профиль>.bot-rescue) ──
        udd = str(tmp / "adopt" / "profile")
        os.makedirs(udd)
        log_path = str(tmp / "adopt" / "events.log")
        ba = _setup(udd, log_path, "P")
        ba._close_pool_h_graceful = lambda: None
        hidden_calls = []
        ba._hide_pool_window = lambda pid=None: hidden_calls.append(pid)

        def _adopt_kills():
            return [e for e in _read_log(log_path) if e[0] == "kill-start"]

        def _ensure():
            with ba._pool_h_lifecycle(time.monotonic() + 5):
                ba._ensure_pool_h_browser()

        vis = _spawn_chrome(udd, headless=False)
        spawned.append(vis)
        _ensure()
        time.sleep(0.2)
        fresh = ba._pool_h_chrome_pid()
        spawned.append(fresh)
        check("видимый Chrome от прошлого запуска бота (rescue) — "
              "перезапуск в штатный headless",
              not _alive(vis) and len(_adopt_kills()) == 1
              and fresh != vis and _alive(fresh))

        ba._POOL_H_RUNNING_MODE, ba._POOL_H_MODE_PID = None, None
        _ensure()
        check("headless Chrome соседа подхватывается без перезапуска",
              len(_adopt_kills()) == 1 and _alive(fresh)
              and ba._POOL_H_RUNNING_MODE == "headless")

        # Сосед включил rescue: его видимый Chrome сменил наш headless
        ba._RAW_CLIENTS["h"] = None
        os.kill(fresh, signal.SIGKILL)
        time.sleep(0.2)
        rescue = _spawn_chrome(udd, headless=False)
        spawned.append(rescue)
        with open(udd + ".bot-rescue", "w") as f:
            f.write(str(time.time() + 600))
        _ensure()
        check("rescue соседа: его видимый Chrome не убит, rescue виден и "
              "здесь (свой режим — от прежнего Chrome — не в счёт)",
              _alive(rescue) and len(_adopt_kills()) == 1
              and ba._POOL_H_RUNNING_MODE == "headed"
              and ba.pool_h_rescue_active())
        ba.end_rescue_pool_h()
        check("конец rescue в любом процессе завершает его для всех "
              "(общий файл удалён)",
              not ba.pool_h_rescue_active()
              and not os.path.exists(udd + ".bot-rescue")
              and ba._pool_h_mode_stale() is False)
        _ensure()
        time.sleep(0.2)
        after = ba._pool_h_chrome_pid()
        spawned.append(after)
        check("после rescue видимый Chrome перезапускается в headless",
              not _alive(rescue) and len(_adopt_kills()) == 2
              and after != rescue and _alive(after))

        # Свой rescue: срок пишется в общий файл; сосед его завершил
        real_teardown = ba._teardown_pool_h
        ba._launch_pool_h_chrome_locked = lambda: None
        ba._teardown_pool_h = lambda grace_sec=3.0, **kw: True
        ok_r = ba.rescue_pool_h(duration_min=10)
        shared = ba._shared_rescue_until()
        check("свой rescue: срок записан в общий файл",
              ok_r and ba.pool_h_rescue_active()
              and shared and abs(shared - ba._POOL_H_RESCUE_UNTIL) < 1)
        os.unlink(udd + ".bot-rescue")
        check("свой rescue, завершённый соседом, — завершён и здесь",
              not ba.pool_h_rescue_active()
              and ba._pool_h_desired_mode() == "headless"
              and ba._POOL_H_MODE_OVERRIDE is None)

        # Нужен hidden: видимый Chrome соседа прячем, не перезапуская
        ba._BCFG["pool_h_mode"] = "hidden"
        try:
            ba._POOL_H_RUNNING_MODE, ba._POOL_H_MODE_PID = None, None
            os.kill(after, signal.SIGKILL)
            time.sleep(0.2)
            vis2 = _spawn_chrome(udd, headless=False)
            spawned.append(vis2)
            _ensure()
            check("pool_h_mode hidden: видимый Chrome соседа спрятан и "
                  "принят как hidden, без перезапуска",
                  _alive(vis2) and hidden_calls == [vis2]
                  and ba._POOL_H_RUNNING_MODE == "hidden")
        finally:
            ba._BCFG["pool_h_mode"] = "headless"

        # ── 8. Chrome пула H завис (жив, CDP молчит и на /json/version):
        # бот перезапускает его сам — и на таймауте вызова, и при
        # переподключении (иначе запуск упирался в его SingletonLock) ──
        udd = str(tmp / "hung" / "profile")
        os.makedirs(udd)
        log_path = str(tmp / "hung" / "events.log")
        ba = _setup(udd, log_path, "P")
        ba._teardown_pool_h = real_teardown  # раздел 7 подменял
        ba._close_pool_h_graceful = lambda: None
        port = urlparse(ba._pool_h_cdp_url()).port or 9223
        responds = {"v": False}
        # Пробник — подмена: настоящий :9223 (Chrome живого бота) не трогаем
        ba._pool_h_responds = lambda timeout: responds["v"]

        def _hung_kills():
            return [e for e in _read_log(log_path) if e[0] == "kill-start"]

        def _launch_with_port():
            _log(log_path, "launch-start", "P", time.time())
            _spawn_chrome(udd, port=port)
        ba._launch_pool_h_chrome_locked = _launch_with_port

        hung = _spawn_chrome(udd, port=port)
        spawned.append(hung)
        os.makedirs(str(tmp / "hung" / "other"))
        other = _spawn_chrome(str(tmp / "hung" / "other"), port=port)
        spawned.append(other)
        check("зависание: Chrome пула (порт и профиль пула) молчит — завис",
              ba._pool_h_hung() is True)
        responds["v"] = True
        check("зависание: отвечает на /json/version — не завис (долгий "
              "вызов ≠ зависший Chrome)", ba._pool_h_hung() is False)
        responds["v"] = False
        plain = _spawn_chrome(udd)  # тот же профиль, без отладочного порта
        spawned.append(plain)
        check("зависание: процесс на профиле без порта пула — не наш Chrome, "
              "не трогаем", ba._pool_h_hung() is False and _alive(plain))
        os.kill(plain, signal.SIGKILL)
        time.sleep(0.2)
        hung = _spawn_chrome(udd, port=port)
        spawned.append(hung)

        # Переподключение: быстрый пробник молчит, профиль держит зависший
        # Chrome → убить и поднять свежий
        with open(os.path.join(udd, f"closed-{hung}"), "w"):
            pass
        with ba._pool_h_lifecycle(time.monotonic() + 5):
            ba._ensure_pool_h_browser()
        time.sleep(0.2)
        fresh = ba._pool_h_chrome_pid()
        spawned.append(fresh)
        check("зависание при переподключении: зависший Chrome убит, поднят "
              "свежий (а не «профиль занят»)",
              not _alive(hung) and len(_hung_kills()) == 1
              and fresh != hung and _alive(fresh) and _alive(other))

        # Таймаут вызова на живом сокете: проверка в фоне → перезапуск
        class _SilentClient(_FakeClient):
            def call(self, method, params=None, session_id=None, timeout=None):
                raise ba.RawCallTimeout(f"CDP {method}: ответа нет")

        def _timeout_call():
            ba._RAW_CLIENTS["h"] = _SilentClient()
            try:
                ba._raw_call("Target.createTarget", pool="h", timeout=1)
                return False
            except ba.RawCallTimeout:
                return True
            finally:
                # дождаться фоновой проверки
                if ba._POOL_H_HANG_CHECK.acquire(timeout=30):
                    ba._POOL_H_HANG_CHECK.release()

        ba._POOL_SEEN_PID["h"] = fresh
        responds["v"] = True
        ba._LAST_RESTART_TS = 0.0
        timed = _timeout_call()
        check("таймаут вызова, Chrome отвечает на пробник — не трогаем",
              timed and _alive(fresh) and len(_hung_kills()) == 1)
        responds["v"] = False
        timed = _timeout_call()
        time.sleep(0.2)
        check("таймаут вызова, Chrome молчит и на пробник — перезапуск в фоне",
              timed and not _alive(fresh) and len(_hung_kills()) == 2
              and ba._RAW_CLIENTS["h"] is None)
    finally:
        for pid in spawned:
            if isinstance(pid, int):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

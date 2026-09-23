"""Межпроцессный жизненный цикл Chrome пула V (хвост отчёта
docs/concurrency-issues-2026-09-23.md, «Осталось»): профиль *-headed и порт
9222 у пула V тоже общие для всех процессов бота (API-сервер и Telegram-бот
с computer_control), а запуск/убийство его Chrome межпроцессного лока не
имели, и простой/выключение одного процесса гасили Chrome соседу.

Проверяется:
  1. простой пула V в процессе A при живом пользователе B — Chrome жив, A
     только отключился и снял регистрацию; B упал (SIGKILL) — следующий
     простой A гасит Chrome (регистрацию упавшего снимает ядро);
  2. выключение процесса (shutdown_browser) — то же правило;
  3. два процесса разом лениво поднимают пул V — запуск ОДИН, второй
     подключается к Chrome первого. Контроль: без межпроцессного лока —
     два запуска на одном профиле (гонка воспроизводится);
  4. убийство пула V ждёт чужой запуск (не поверх него), ожидание чужого
     цикла ограничено (пропуск без убийства);
  5. перезапуск (лечение) гасит Chrome и при живых соседях-пользователях,
     но устаревший (решён для Chrome X, а сосед уже поднял Y) Y не трогает;
  6. регистрация ждёт гасящего соседа ≤ своего потолка; учёт без flock —
     гасим, как раньше;
  7. процесс, ходящий в Chrome V только сырым сокетом (без воркера), —
     тоже пользователь; в своём простое снимает регистрацию.

Настоящий Chrome не запускается и не убивается: «Chrome» — спящий python-
подпроцесс с --user-data-dir=<tmp-профиль> и симлинком SingletonLock;
подключение воркера — фейк поверх него, убийство — настоящее
_kill_chrome_on_profile. Профили H и V — временные.
Запуск: python3 -m scripts.test_pool_v_lifecycle
"""

import multiprocessing
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.test_pool_h_lifecycle import (  # noqa: E402
    _FakeClient, _alive, _log, _read_log, _spawn_chrome)

LAUNCH_SEC = 0.5   # сколько «поднимается» фейковый Chrome


def _chrome_pid(vdd: str):
    try:
        return int(os.readlink(os.path.join(vdd, "SingletonLock"))
                   .rsplit("-", 1)[-1])
    except (OSError, ValueError):
        return None


def _setup(vdd: str, log_path: str, who: str, no_flock: bool = False):
    """Подмены браузерного слоя на фейки — ДО любого вызова: оба профиля
    временные, реальный Chrome и лок-файлы живого бота не трогаются."""
    import app.features.browser_actions as ba
    hdd = vdd.rstrip("/") + "-h"
    os.makedirs(hdd, exist_ok=True)
    ba._pool_v_profile = lambda: vdd
    ba._pool_h_profile = lambda: hdd
    ba._shutdown_pool_h = lambda reason="": None
    ba._is_default_browser_profile = lambda u: False
    ba._pool_v_media_playing = lambda: False
    ba._start_pool_v_watchdog = lambda: None  # простой — только по вызову теста
    ba._RawCdp = lambda url=None: _FakeClient()
    ba._sweep_orphan_tabs = lambda c, p: None

    class _FakeBrowser:
        def __init__(self, pid):
            self.pid = pid

        def is_connected(self):
            return _alive(self.pid) and not os.path.exists(
                os.path.join(vdd, f"closed-{self.pid}"))

        def close(self):  # browser.close() по CDP гасит Chrome
            open(os.path.join(vdd, f"closed-{self.pid}"), "w").close()
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass

    def _fake_connect(self, timeout_ms=0):
        pid = _chrome_pid(vdd)
        if not pid or not _alive(pid) or os.path.exists(
                os.path.join(vdd, f"closed-{pid}")):
            raise ba.BrowserUnavailable("порт закрыт")
        self._browser = _FakeBrowser(pid)
        _log(log_path, "connect", who, time.time(), pid)

    def _fake_launch_locked(self):
        _log(log_path, "launch-start", who, time.time())
        time.sleep(LAUNCH_SEC)
        pid = _spawn_chrome(vdd)
        _log(log_path, "launch-end", who, time.time(), pid)
        self._connect()

    real_kill = ba._kill_chrome_on_profile

    def _logged_kill(proc, u, grace_sec=10.0):
        _log(log_path, "kill-start", who, time.time(), _chrome_pid(vdd))
        out = real_kill(proc, u, grace_sec=grace_sec)
        _log(log_path, "kill-end", who, time.time())
        return out

    ba._CdpWorker._connect = _fake_connect
    ba._CdpWorker._launch_chrome_locked = _fake_launch_locked
    ba._kill_chrome_on_profile = _logged_kill
    if no_flock:
        ba._pool_life_open = lambda pool, deadline: None
    return ba


def _mp_user(vdd, log_path, ready, cmdq, q):
    """Процесс B: пользуется пулом V (подключение через воркер — запуск,
    если Chrome нет), ждёт команды; SIGKILL от теста — падение."""
    ba = _setup(vdd, log_path, "B")
    ba._WORKER.submit(lambda w: w.ensure_browser(allow_launch=True),
                      timeout=30)
    ready.set()
    cmd = cmdq.get(timeout=60)
    if cmd == "shutdown":
        ba.shutdown_browser("тест")
        q.put(("B", _chrome_pid(vdd)))


def _mp_lazy(vdd, log_path, who, go, no_flock, q):
    """Ленивый старт пула V по сигналу go (оба процесса — разом)."""
    ba = _setup(vdd, log_path, who, no_flock)
    go.wait(20)
    try:
        ba._WORKER.submit(lambda w: w.ensure_browser(allow_launch=True),
                          timeout=30)
        q.put((who, ba._WORKER._browser.pid))
    except Exception as e:
        q.put((who, f"err {e!r}"))


def _mp_hold_life(vdd, log_path, ready, release):
    """Держит лок цикла пула V (будто запускает Chrome)."""
    ba = _setup(vdd, log_path, "L")
    with ba._pool_lifecycle("v"):
        ready.set()
        release.wait(30)


def _mp_launch_slow(vdd, log_path, started):
    """Запуск пула V «долгий»: процесс поднимает Chrome под локом цикла."""
    ba = _setup(vdd, log_path, "S")

    def _slow_launch(self):
        started.set()
        _log(log_path, "launch-start", "S", time.time())
        time.sleep(1.0)
        pid = _spawn_chrome(vdd)
        _log(log_path, "launch-end", "S", time.time(), pid)

    ba._CdpWorker._launch_chrome_locked = _slow_launch
    ba._WORKER._launch_chrome()


def _mp_raw_user(vdd, log_path, ready, cmdq, q):
    """Процесс R: ходит в Chrome V ТОЛЬКО сырым сокетом (без воркера) —
    фоновая headed-вкладка веб-чата в Chrome, поднятом соседом."""
    ba = _setup(vdd, log_path, "R")
    ba._raw_call("Browser.getVersion", pool="v", timeout=5)
    with ba._RAW_TABS_LOCK:  # фоновая вкладка веб-чата в Chrome V
        ba._RAW_TABS[999] = {"targetId": "t", "sessionId": "s", "pool": "v"}
    q.put(("R-reg", bool(ba._POOL_USERS["v"])
           and ba._WORKER._thread is None))
    ready.set()
    while True:
        cmd = cmdq.get(timeout=60)
        if cmd == "idle":
            ba._POOL_V_TS = 0.0
            ba._pool_v_idle_check()
            # Chrome жив (гасит его не наш простой) — вкладка в реестре
            # остаётся: иначе сирота в общем Chrome V
            q.put(("R-idle", ba._POOL_USERS["v"] is None
                   and ba._RAW_CLIENTS["v"] is None
                   and 999 in ba._RAW_TABS))
        else:
            return


def _lazy_pair(tmp: Path, name: str, no_flock: bool):
    vdd = str(tmp / name / "profile")
    os.makedirs(vdd)
    log_path = str(tmp / name / "events.log")
    ctx = multiprocessing.get_context("spawn")
    q, go = ctx.Queue(), ctx.Event()
    ps = [ctx.Process(target=_mp_lazy,
                      args=(vdd, log_path, who, go, no_flock, q))
          for who in ("A", "B")]
    for p in ps:
        p.start()
    time.sleep(1.5)  # оба импортировали модуль и ждут сигнала
    go.set()
    res = {}
    for _ in ps:
        try:
            k, v = q.get(timeout=40)
            res[k] = v
        except Exception:
            break
    for p in ps:
        p.join(10)
    return res, _read_log(log_path)


def main():
    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    if sys.platform == "win32":
        print("  [SKIP] posix-only (SingletonLock/fcntl)")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="pool_v_life_"))
    spawned = []
    ctx = multiprocessing.get_context("spawn")
    try:
        # ── 1. Простой A при живом пользователе B ──
        vdd = str(tmp / "idle" / "profile")
        os.makedirs(vdd)
        log_path = str(tmp / "idle" / "events.log")
        ba = _setup(vdd, log_path, "A")
        vp = ba._pool_users_path("v")
        check("файлы локов пула V — рядом с ВРЕМЕННЫМ профилем",
              vp == vdd + ".bot-users.lock"
              and ba._pool_life_path("v") == vdd + ".bot-lifecycle.lock")

        def _kills():
            return [e for e in _read_log(log_path) if e[0] == "kill-start"]

        def _idle():
            ba._POOL_V_TS = 0.0
            ba._pool_v_idle_check()

        ready, cmdq, q = ctx.Event(), ctx.Queue(), ctx.Queue()
        pb = ctx.Process(target=_mp_user, args=(vdd, log_path, ready, cmdq, q))
        pb.start()
        try:
            ready.wait(30)
            chrome = _chrome_pid(vdd)
            spawned.append(chrome)
            ba._WORKER.submit(lambda w: w.ensure_browser(allow_launch=True),
                              timeout=30)
            check("A подключился к Chrome B (без своего запуска), "
                  "зарегистрирован пользователем",
                  ba._WORKER._browser.pid == chrome
                  and bool(ba._POOL_USERS["v"])
                  and [e[1] for e in _read_log(log_path)
                       if e[0] == "launch-start"] == ["B"])
            _idle()
            check("простой A при живом пользователе B: Chrome жив, A "
                  "отключился и снял регистрацию",
                  _alive(chrome) and not _kills()
                  and ba._WORKER._browser is None
                  and ba._POOL_USERS["v"] is None)
            os.kill(pb.pid, signal.SIGKILL)  # B падает
            pb.join(10)
            _idle()
            time.sleep(0.2)
            check("B упал (SIGKILL) — следующий простой A гасит Chrome",
                  not _alive(chrome) and len(_kills()) == 1)
        finally:
            if pb.is_alive():
                pb.kill()
            pb.join(5)

        # ── 2. Выключение процесса: то же правило ──
        ready, cmdq, q = ctx.Event(), ctx.Queue(), ctx.Queue()
        pb = ctx.Process(target=_mp_user, args=(vdd, log_path, ready, cmdq, q))
        pb.start()
        try:
            ready.wait(30)
            chrome = _chrome_pid(vdd)
            spawned.append(chrome)
            ba._WORKER.submit(lambda w: w.ensure_browser(allow_launch=True),
                              timeout=30)
            cmdq.put("shutdown")  # выходит B, A ещё пользуется
            k, v = q.get(timeout=40)
            pb.join(10)
            check("выход B не гасит Chrome V, которым пользуется A",
                  v == chrome and _alive(chrome) and len(_kills()) == 1)
        finally:
            if pb.is_alive():
                pb.kill()
            pb.join(5)
        # playwright browser.close() на залипшем Chrome висит без потолка —
        # под локом цикла он не зовётся (Browser.close сырым сокетом)
        ba._WORKER._browser.close = lambda: time.sleep(3600)
        t0 = time.monotonic()
        ba.shutdown_browser("тест")
        dt = time.monotonic() - t0
        time.sleep(0.2)
        check("выключение последнего пользователя (A) гасит Chrome V; "
              "зависающий playwright close под локом не зовётся",
              not _alive(chrome) and len(_kills()) == 2
              and ba._POOL_USERS["v"] is None and dt < 15)

        # ── 3. Два ленивых старта разом ──
        res, log = _lazy_pair(tmp, "lazy", no_flock=False)
        spawned += [int(e[3]) for e in log if e[0] == "launch-end"]
        launches = [e for e in log if e[0] == "launch-start"]
        check("2 процесса разом: запуск ОДИН, второй подключился к Chrome "
              "первого",
              len(launches) == 1 and isinstance(res.get("A"), int)
              and res.get("A") == res.get("B"))
        res_c, log_c = _lazy_pair(tmp, "lazy_ctl", no_flock=True)
        spawned += [int(e[3]) for e in log_c if e[0] == "launch-end"]
        check("контроль: без межпроцессного лока — два запуска на одном "
              "профиле (гонка воспроизводится)",
              len([e for e in log_c if e[0] == "launch-start"]) == 2)

        # ── 4. Убийство ждёт чужой запуск; ожидание ограничено ──
        vdd = str(tmp / "kill" / "profile")
        os.makedirs(vdd)
        log_path = str(tmp / "kill" / "events.log")
        ba = _setup(vdd, log_path, "A")
        started = ctx.Event()
        ps = ctx.Process(target=_mp_launch_slow,
                         args=(vdd, log_path, started))
        ps.start()
        try:
            started.wait(20)
            ba._kill_pool_v_direct(vdd, grace_sec=1.0)
            log = _read_log(log_path)
            l_end = [float(e[2]) for e in log if e[0] == "launch-end"]
            k_st = [float(e[2]) for e in log if e[0] == "kill-start"]
            check("убийство пула V ждёт конца чужого запуска (не поверх "
                  "него)", l_end and k_st and k_st[0] >= l_end[0])
        finally:
            ps.join(15)
            if ps.is_alive():
                ps.kill()
        live = _spawn_chrome(vdd)
        spawned.append(live)
        ready, release = ctx.Event(), ctx.Event()
        ph = ctx.Process(target=_mp_hold_life,
                         args=(vdd, log_path, ready, release))
        ph.start()
        saved_wait = ba.POOL_V_LIFECYCLE_WAIT_SEC
        try:
            ready.wait(20)
            ba.POOL_V_LIFECYCLE_WAIT_SEC = 0.5
            n0 = len(_kills_of(log_path))
            t0 = time.monotonic()
            ba._kill_pool_v_direct(vdd, grace_sec=1.0)
            dt = time.monotonic() - t0
            check("цикл пула V занят другим процессом: убийство пропущено за "
                  "потолок ожидания, Chrome не тронут",
                  dt < 1.5 and _alive(live)
                  and len(_kills_of(log_path)) == n0)
            lk = ba._POOL_V_LIFE_LOCK.acquire(blocking=False)
            if lk:
                ba._POOL_V_LIFE_LOCK.release()
            check("после отказа локи пула V отпущены",
                  lk and ba._POOL_LIFE["v"]["depth"] == 0
                  and ba._POOL_LIFE["v"]["fh"] is None)
        finally:
            ba.POOL_V_LIFECYCLE_WAIT_SEC = saved_wait
            release.set()
            ph.join(10)

        # ── 5. Перезапуск (лечение) гасит и при живых пользователях ──
        ready, cmdq, q = ctx.Event(), ctx.Queue(), ctx.Queue()
        pb = ctx.Process(target=_mp_user, args=(vdd, log_path, ready, cmdq, q))
        pb.start()
        try:
            ready.wait(30)
            old = _chrome_pid(vdd)
            ba._WORKER.submit(lambda w: w.ensure_browser(allow_launch=True),
                              timeout=30)
            r = ba.restart_browser("тест", cooldown_sec=0, pool="v")
            new = _chrome_pid(vdd)
            spawned.append(new)
            time.sleep(0.2)
            check("перезапуск пула V гасит залипший Chrome и при живом "
                  "соседе-пользователе, поднимает новый",
                  r is True and not _alive(old) and new != old
                  and _alive(new) and ba._WORKER._browser.pid == new)
        finally:
            cmdq.put("exit")
            pb.join(10)
            if pb.is_alive():
                pb.kill()

        # ── 5б. Устаревший перезапуск: решение принято для Chrome X, а сосед
        # уже убил X и поднял свой Y — Y не трогаем (как _POOL_SEEN_PID у H) ──
        x = _chrome_pid(vdd)
        check("seen pid пула V записан при подключении",
              ba._POOL_SEEN_PID["v"] == x and ba._WORKER._browser.pid == x)
        os.kill(x, signal.SIGKILL)  # сосед убил X…
        time.sleep(0.3)
        y = _spawn_chrome(vdd)      # …и поднял свой Y
        spawned.append(y)
        n0 = len(_kills_of(log_path))
        r = ba.restart_browser("тест", cooldown_sec=0, pool="v")
        check("stale: Chrome V сменён соседом — перезапуск его не убивает, "
              "подключаемся к нему",
              r is True and _alive(y) and len(_kills_of(log_path)) == n0
              and ba._WORKER._browser.pid == y
              and ba._POOL_SEEN_PID["v"] == y)
        ba._kill_pool_v_direct(vdd, grace_sec=1.0, expect_pid=x)
        check("stale: прямой фолбэк с устаревшим pid тоже не убивает",
              _alive(y) and len(_kills_of(log_path)) == n0)
        r = ba.restart_browser("тест", cooldown_sec=0, pool="v")
        z = _chrome_pid(vdd)
        spawned.append(z)
        time.sleep(0.2)
        check("stale: Chrome, в котором залипли (seen = Y), перезапуск убивает",
              r is True and not _alive(y) and z != y and _alive(z)
              and len(_kills_of(log_path)) == n0 + 1)

        # ── 6. Регистрация и гасящий сосед; учёт без flock ──
        ba._pool_user_release("v")
        held, go = threading.Event(), threading.Event()

        def _killer():
            with ba._pool_lifecycle("v"):
                with ba._pool_last_user("v") as last:
                    held.set()
                    go.wait(5)
                    time.sleep(0.4)
            held.last = last

        tk = threading.Thread(target=_killer, daemon=True)
        tk.start()
        held.wait(5)
        ba._pool_user_register("v", wait=0.0)
        not_now = ba._POOL_USERS["v"] is None
        go.set()
        t0 = time.monotonic()
        ba._pool_user_register("v", wait=2.0)
        dt = time.monotonic() - t0
        tk.join(5)
        check("регистрация: пока сосед гасит Chrome — не сразу; дождалась "
              "его конца в свой потолок",
              held.last is True and not_now and bool(ba._POOL_USERS["v"])
              and 0.2 <= dt < 1.5)
        ba._pool_user_release("v")

        import errno
        import fcntl
        real_flock = fcntl.flock

        def _nosup(fd, op):
            raise OSError(errno.ENOTSUP, "Operation not supported")

        # Сосед держит регистрацию, но у нас flock недоступен: гасим как раньше
        ready, cmdq, q = ctx.Event(), ctx.Queue(), ctx.Queue()
        pb = ctx.Process(target=_mp_user, args=(vdd, log_path, ready, cmdq, q))
        pb.start()
        try:
            ready.wait(30)
            chrome = _chrome_pid(vdd)
            fcntl.flock = _nosup
            try:
                ba._pool_user_register("v")
                unsup = ba._POOL_USERS["v"] is False
                with ba._pool_lifecycle("v", time.monotonic() + 1.0):
                    with ba._pool_last_user("v") as last:
                        pass
            finally:
                fcntl.flock = real_flock
            check("учёт без flock: регистрация помечена недоступной, "
                  "«последний пользователь» — да (гасим, как раньше)",
                  unsup and last is True and _alive(chrome))
        finally:
            cmdq.put("exit")
            pb.join(10)
            if pb.is_alive():
                pb.kill()

        # ── 7. Пользователь только через сырой сокет (без воркера) ──
        ba._pool_user_release("v")
        chrome = _chrome_pid(vdd)
        n0 = len(_kills_of(log_path))
        ready, cmdq, q = ctx.Event(), ctx.Queue(), ctx.Queue()
        pr = ctx.Process(target=_mp_raw_user,
                         args=(vdd, log_path, ready, cmdq, q))
        pr.start()
        try:
            _, reg = q.get(timeout=30)
            ready.wait(10)
            ba._WORKER.submit(lambda w: w.ensure_browser(allow_launch=True),
                              timeout=30)
            ba._POOL_V_TS = 0.0
            ba._pool_v_idle_check()
            check("сырой сокет к Chrome V регистрирует пользователя: простой "
                  "соседа Chrome не гасит",
                  reg is True and _alive(chrome)
                  and len(_kills_of(log_path)) == n0)
            cmdq.put("idle")
            _, rel = q.get(timeout=30)
            ba._WORKER.submit(lambda w: w.ensure_browser(allow_launch=True),
                              timeout=30)
            ba._POOL_V_TS = 0.0
            ba._pool_v_idle_check()
            time.sleep(0.2)
            check("простой процесса без воркера снимает его регистрацию — "
                  "затем простой последнего пользователя гасит Chrome",
                  rel is True and not _alive(chrome)
                  and len(_kills_of(log_path)) == n0 + 1)
        finally:
            cmdq.put("exit")
            pr.join(10)
            if pr.is_alive():
                pr.kill()
    finally:
        for pid in spawned:
            if isinstance(pid, int):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


def _kills_of(log_path):
    return [e for e in _read_log(log_path) if e[0] == "kill-start"]


if __name__ == "__main__":
    sys.exit(main())

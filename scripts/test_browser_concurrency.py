"""Конкурентность браузерного слоя: пул H (фоновые вкладки веб-чатов, raw CDP).

Живого браузера нет: сокет, клиент CDP, запуск/убийство Chrome — фейки.
Проверяется:

  1. _RawCdp мультиплексирует ответы по id: зависший вызов одной вкладки не
     задерживает ответ другой; обрыв сокета будит всех ждущих сразу;
  2. _raw_call не держит лок пула на время ответа (awaitPromise аплоада
     картинки не останавливает соседние вкладки);
  3. ожидание лока пула ограничено и входит в бюджет вызова → RawCallTimeout;
  4. перезапуск пула H и ленивый запуск Chrome взаимоисключающи: поток
     ответа не поднимает свой Chrome посреди перезапуска; вызов в вкладку,
     сброшенную перезапуском, Chrome не поднимает; поколение пула растёт;
  5. snapshot_elements отказывает фоновой вкладке сразу, не трогая пул V.

Запуск: python3 -m scripts.test_browser_concurrency
"""

import json
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class _WsTimeout(Exception):
    pass


_WsTimeout.__name__ = "WebSocketTimeoutException"


class _FakeWs:
    """Сокет с ответами по id: responder(msg) → dict ответа или None (молчит).
    Ответы кладутся в очередь в момент send — как настоящий браузер, который
    отвечает независимо от того, кто сейчас читает сокет."""

    def __init__(self, responder):
        self.responder = responder
        self.q: "queue.Queue" = queue.Queue()
        self.timeout = 5.0
        self.closed = False
        self.readers = 0
        self.max_readers = 0
        self._lk = threading.Lock()

    def settimeout(self, t):
        self.timeout = t

    def send(self, data):
        msg = json.loads(data)
        res = self.responder(msg)
        if res is not None:
            delay, body = res
            item = dict(body)
            item["id"] = msg["id"]
            if delay:
                threading.Timer(delay, self.q.put, (json.dumps(item),)).start()
            else:
                self.q.put(json.dumps(item))

    def recv(self):
        with self._lk:
            self.readers += 1
            self.max_readers = max(self.max_readers, self.readers)
        try:
            try:
                item = self.q.get(timeout=self.timeout)
            except queue.Empty:
                raise _WsTimeout("timed out")
            if item == "__boom__":
                raise OSError("socket closed")
            return item
        finally:
            with self._lk:
                self.readers -= 1

    def close(self):
        self.closed = True


def main():
    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    import app.features.browser_actions as ba

    _RawCdpCls = ba._RawCdp  # класс сохраняем: ниже _RawCdp подменяется фабриками

    def _client(responder):
        cl = _RawCdpCls.__new__(_RawCdpCls)
        cl._ws = _FakeWs(responder)
        return cl

    # ── 1. Мультиплексор _RawCdp ──
    def _resp(msg):
        if msg["method"] == "hang":
            return None
        if msg["method"] == "slow":
            return 0.6, {"result": {"v": "slow"}}
        return 0, {"result": {"v": msg["method"]}}

    cl = _client(_resp)
    box = {}

    def _hang():
        t0 = time.monotonic()
        try:
            cl.call("hang", timeout=2.0)
            box["hang"] = "returned"
        except ba.RawCallTimeout:
            box["hang"] = time.monotonic() - t0

    th = threading.Thread(target=_hang)
    th.start()
    time.sleep(0.2)  # зависший вызов успел стать ведущим и читает сокет
    t0 = time.monotonic()
    fast = cl.call("fast", timeout=5.0)
    fast_dt = time.monotonic() - t0
    th.join(5)
    check("mux: ответ соседа не ждёт зависший вызов (раньше — до 20с под локом)",
          fast == {"v": "fast"} and fast_dt < 1.0)
    check("mux: зависший вызов честно падает RawCallTimeout в свой бюджет",
          isinstance(box.get("hang"), float) and 1.8 <= box["hang"] < 3.5)
    check("mux: сокет читает не больше одного потока за раз",
          cl._ws.max_readers == 1)

    # Несколько параллельных вызовов с разной задержкой — каждый получает СВОЙ
    # ответ (id не путаются)
    res = {}

    def _worker(i):
        m = "slow" if i % 2 else f"m{i}"
        res[i] = cl.call(m, timeout=5.0)

    ths = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(5)
    check("mux: 8 параллельных вызовов — каждому свой ответ",
          all(res.get(i) == ({"v": "slow"} if i % 2 else {"v": f"m{i}"})
              for i in range(8)))
    check("mux: после таймаута одного вызова соединение живо",
          cl._dead is None and not cl._ws.closed
          and cl.call("again", timeout=2.0) == {"v": "again"})

    # Обрыв сокета будит ВСЕХ ждущих сразу, а не по их бюджетам
    cl2 = _client(lambda m: None)
    errs = []

    def _wait_dead():
        t0 = time.monotonic()
        try:
            cl2.call("hang", timeout=10.0)
        except Exception as e:
            errs.append((type(e), time.monotonic() - t0))

    ths = [threading.Thread(target=_wait_dead) for _ in range(3)]
    for t in ths:
        t.start()
    time.sleep(0.3)
    cl2._ws.q.put("__boom__")
    for t in ths:
        t.join(5)
    check("mux: обрыв сокета — все ждущие сразу получают ошибку транспорта",
          len(errs) == 3
          and all(not issubclass(tp, ba.BrowserUnavailable) for tp, _ in errs)
          and max(dt for _, dt in errs) < 2.0)
    try:
        cl2.call("x", timeout=1.0)
        after_dead = False
    except ba.BrowserUnavailable:
        after_dead = False
    except Exception:
        after_dead = True
    check("mux: мёртвый клиент сразу отказывает (не ждёт бюджет)", after_dead)

    # ── 2-3. _raw_call: лок только на подключение, ожидание — в бюджете ──
    class _FakeCdp:
        def __init__(self):
            self.calls = []
            self.release = threading.Event()
            self.closed = False
            self.broken = threading.Event()

        def call(self, method, params=None, session_id=None, timeout=None):
            self.calls.append((method, session_id, timeout))
            if method == "Runtime.evaluate" and \
                    "UPLOAD" in str((params or {}).get("expression")):
                # awaitPromise аплоада: страница ждёт до timeout
                end = time.monotonic() + float(timeout or 0)
                while time.monotonic() < end:
                    if self.broken.is_set():
                        raise OSError("socket closed")
                    if self.release.wait(0.05):
                        break
                return {"result": {"value": "ready"}}
            if self.broken.is_set():
                raise OSError("socket closed")
            if method == "Runtime.evaluate":
                return {"result": {"value": "ok"}}
            if method == "Target.createTarget":
                return {"targetId": "T-new"}
            if method == "Target.attachToTarget":
                return {"sessionId": "S-new"}
            if method == "Browser.close":
                close_seen.append((990002 in ba._RAW_TABS,
                                   ba.raw_pool_generation("h")))
                chrome["alive"] = False
                self.broken.set()
            return {}

        def close(self):
            self.closed = True
            self.broken.set()

    saved = dict(clients=dict(ba._RAW_CLIENTS), tabs=dict(ba._RAW_TABS),
                 rawcdp=ba._RawCdp, alive=ba._pool_h_alive,
                 launch=ba._launch_pool_h_chrome,
                 kill=ba._kill_chrome_on_profile,
                 is_def=ba._is_default_browser_profile,
                 prof=ba._pool_h_profile, proc=ba._POOL_H_PROC,
                 mode=ba._POOL_H_RUNNING_MODE, last=ba._LAST_RESTART_TS,
                 sel=ba._select_backend, submit=ba._WORKER.submit,
                 swept=set(ba._RAW_SWEPT))
    chrome = {"alive": True}
    events = []
    close_seen = []
    try:
        ba._RAW_SWEPT.add("h")
        fc = _FakeCdp()
        ba._RAW_CLIENTS["h"] = fc
        ba._RAW_TABS.clear()
        ba._RAW_TABS[990001] = {"targetId": "TA", "sessionId": "SA", "pool": "h"}
        ba._RAW_TABS[990002] = {"targetId": "TB", "sessionId": "SB", "pool": "h"}

        # Аплоад картинки во вкладке A (awaitPromise до 3с) — вкладка B
        # в это время отвечает сразу
        up = {}

        def _upload():
            up["out"] = ba._raw_eval(990001, "UPLOAD", timeout_sec=3.0)

        tu = threading.Thread(target=_upload)
        tu.start()
        time.sleep(0.2)
        t0 = time.monotonic()
        out_b = ba._raw_eval(990002, "1+1")
        dt_b = time.monotonic() - t0
        fc.release.set()
        tu.join(5)
        check("raw_call: ожидание аплоада во вкладке A не держит вкладку B "
              "(лок пула не держится на ответ)",
              out_b == "ok" and dt_b < 0.5 and up.get("out") == "ready")

        # Лок пула занят (подключение/перезапуск) дольше бюджета → RawCallTimeout
        held = threading.Event()
        rel = threading.Event()

        def _holder():
            with ba._RAW_LOCKS["h"]:
                held.set()
                rel.wait(5)

        th = threading.Thread(target=_holder)
        th.start()
        held.wait(2)
        t0 = time.monotonic()
        err = None
        try:
            ba._raw_tab_call(990002, "Runtime.evaluate", {"expression": "1"},
                             timeout=0.5)
        except Exception as e:
            err = e
        dt = time.monotonic() - t0
        rel.set()
        th.join(5)
        check("raw_call: ожидание лока пула ограничено бюджетом → RawCallTimeout",
              isinstance(err, ba.RawCallTimeout) and 0.4 <= dt < 1.5)

        # Ожидание лока засчитывается в бюджет: клиенту уходит остаток
        rel2 = threading.Event()
        held2 = threading.Event()

        def _holder2():
            with ba._RAW_LOCKS["h"]:
                held2.set()
                rel2.wait(0.4)

        th = threading.Thread(target=_holder2)
        th.start()
        held2.wait(2)
        fc.calls.clear()
        ba._raw_tab_call(990002, "Runtime.evaluate", {"expression": "1"},
                         timeout=2.0)
        th.join(5)
        passed = fc.calls[-1][2]
        check("raw_call: время в очереди за локом входит в бюджет вызова",
              passed is not None and 1.3 <= passed <= 1.7)

        # ── 4. Перезапуск пула H против ленивого запуска ──
        gen0 = ba.raw_pool_generation("h")
        kill_entered = threading.Event()

        def _fake_kill(proc, udd, grace_sec=10.0):
            events.append(("kill-start", time.monotonic()))
            kill_entered.set()
            time.sleep(0.5)  # окно: ленивый запуск не должен стартовать тут
            chrome["alive"] = False
            events.append(("kill-end", time.monotonic()))
            return True

        def _fake_launch():
            events.append(("launch", time.monotonic()))
            chrome["alive"] = True
            ba._POOL_H_RUNNING_MODE = None

        new_clients = []

        def _fake_rawcdp(url=None):
            c = _FakeCdp()
            new_clients.append(c)
            return c

        ba._pool_h_alive = lambda: chrome["alive"]
        ba._launch_pool_h_chrome = _fake_launch
        ba._kill_chrome_on_profile = _fake_kill
        ba._is_default_browser_profile = lambda udd: False
        ba._pool_h_profile = lambda: "/nonexistent/vpc-test-profile"
        ba._RawCdp = _fake_rawcdp
        ba._POOL_H_PROC = None
        ba._POOL_H_RUNNING_MODE = None
        ba._LAST_RESTART_TS = 0.0

        # В полёте — долгий вызов вкладки A на старом клиенте (ответ ещё
        # ждётся): перезапуск рвёт его сокет
        fc.release.clear()
        inflight = {}

        def _inflight():
            try:
                inflight["out"] = ba._raw_eval(990001, "UPLOAD", timeout_sec=5.0)
            except Exception as e:
                inflight["err"] = e

        ti = threading.Thread(target=_inflight)
        ti.start()
        time.sleep(0.2)

        rs = {}
        tr = threading.Thread(target=lambda: rs.setdefault(
            "ok", ba.restart_browser("тест", cooldown_sec=0, pool="h")))
        tr.start()
        kill_entered.wait(3)
        # Поток ответа открывает вкладку, пока перезапуск убивает Chrome
        opened = {}

        def _open():
            try:
                opened["tid"] = ba._raw_open("about:blank", pool="h")
            except Exception as e:
                opened["err"] = e

        to = threading.Thread(target=_open)
        to.start()
        tr.join(10)
        to.join(10)
        ti.join(10)
        kinds = [k for k, _ in events]
        kill_end = next(t for k, t in events if k == "kill-end")
        launches = [t for k, t in events if k == "launch"]
        check("restart H: ленивый запуск ждёт конца перезапуска, а не "
              "поднимает Chrome, который перезапуск убьёт",
              rs.get("ok") is True and launches
              and all(t >= kill_end for t in launches)
              and kinds.count("launch") == 1)
        check("restart H: вкладка, открытая во время перезапуска, живёт в "
              "новом Chrome",
              "tid" in opened and opened["tid"] in ba._RAW_TABS
              and chrome["alive"] and len(new_clients) == 1
              and ba._RAW_CLIENTS["h"] is new_clients[0])
        check("restart H: вызов в полёте на старом сокете — честное «вкладка "
              "закрыта», без второго Chrome",
              isinstance(inflight.get("err"), ba.BrowserUnavailable)
              and not isinstance(inflight.get("err"), ba.RawCallTimeout)
              and 990001 not in ba._RAW_TABS
              and kinds.count("launch") == 1)
        check("restart H: поколение пула выросло (вкладки прежнего мертвы)",
              ba.raw_pool_generation("h") > gen0)
        check("restart H: вкладки ушли из реестра и поколение выросло ДО "
              "Browser.close (обрыв в окне закрытия — «вкладка умерла», "
              "а не «залипла»)",
              close_seen == [(False, gen0 + 1)])

        # Вызов в вкладку, сброшенную перезапуском, Chrome НЕ поднимает
        ba._RAW_CLIENTS["h"] = None
        chrome["alive"] = False
        events.clear()
        ba._RAW_TABS[990003] = {"targetId": "TC", "sessionId": "SC", "pool": "h"}
        ba._reset_raw_pool("h")
        dead = None
        try:
            ba._raw_eval(990003, "1+1")
        except ba.BrowserUnavailable as e:
            dead = e
        check("raw_call: мёртвая вкладка после сброса пула — отказ без запуска "
              "Chrome",
              dead is not None and not any(k == "launch" for k, _ in events))

        # ── 5. snapshot_elements: фоновая вкладка — отказ сразу ──
        touched = []
        ba._select_backend = lambda tab_op=True: touched.append("sel") or "cdp"
        ba._WORKER.submit = lambda fn, timeout=None: touched.append("submit")
        ba._RAW_TABS[990004] = {"targetId": "TD", "sessionId": "SD", "pool": "h"}
        t0 = time.monotonic()
        refused = False
        try:
            ba.snapshot_elements("chat.deepseek.com", tab_id=990004)
        except ba.RawTabUnsupported:
            refused = True
        check("snapshot_elements: фоновая вкладка — RawTabUnsupported сразу, "
              "без очереди воркера пула V",
              refused and not touched and time.monotonic() - t0 < 0.2)
    finally:
        ba._RawCdp = saved["rawcdp"]
        ba._pool_h_alive = saved["alive"]
        ba._launch_pool_h_chrome = saved["launch"]
        ba._kill_chrome_on_profile = saved["kill"]
        ba._is_default_browser_profile = saved["is_def"]
        ba._pool_h_profile = saved["prof"]
        ba._POOL_H_PROC = saved["proc"]
        ba._POOL_H_RUNNING_MODE = saved["mode"]
        ba._LAST_RESTART_TS = saved["last"]
        ba._select_backend = saved["sel"]
        ba._WORKER.submit = saved["submit"]
        ba._RAW_CLIENTS.clear()
        ba._RAW_CLIENTS.update(saved["clients"])
        ba._RAW_TABS.clear()
        ba._RAW_TABS.update(saved["tabs"])
        ba._RAW_SWEPT.clear()
        ba._RAW_SWEPT.update(saved["swept"])

    # ── 6. Переattach после обрыва — ровно один на вкладку ──
    import collections
    saved6 = dict(rawcdp=ba._RawCdp, ensure=ba._ensure_pool_h_browser,
                  url=ba._pool_h_cdp_url, sweep=ba._sweep_orphan_tabs,
                  life=ba._pool_h_life_path,
                  clients=dict(ba._RAW_CLIENTS), tabs=dict(ba._RAW_TABS))
    attaches = collections.Counter()
    valid = {}
    made = []

    def _make(gen):
        def _r(msg):
            m, sid = msg["method"], msg.get("sessionId")
            if m == "Target.attachToTarget":
                t = msg["params"]["targetId"]
                attaches[t] += 1
                new = f"s{gen}-{t}-{attaches[t]}"
                valid[new] = gen
                return 0.05, {"result": {"sessionId": new}}
            if sid is not None and valid.get(sid) != gen:
                return 0, {"error": {"message":
                                     "Session with given id not found"}}
            return 0.3, {"result": {"ok": sid}}
        return _r

    def _factory(url=None):
        c = _client(_make(len(made) + 1))
        made.append(c)
        return c

    try:
        ba._RawCdp = _factory
        ba._ensure_pool_h_browser = lambda: None
        ba._pool_h_cdp_url = lambda: "http://fake"
        ba._sweep_orphan_tabs = lambda c, p: None
        # Не реальный лок-файл профиля: иначе тест делит его с живым ботом
        _life6 = tempfile.NamedTemporaryFile(suffix=".bot-lifecycle.lock",
                                             delete=False).name
        ba._pool_h_life_path = lambda: _life6
        ba._RAW_TABS.clear()
        first = _factory()
        ba._RAW_CLIENTS["h"] = first
        for i, t in enumerate(("T1", "T2", "T3")):
            valid[f"s1-{t}-0"] = 1
            ba._RAW_TABS[2_000_000 + i] = {"targetId": t,
                                           "sessionId": f"s1-{t}-0",
                                           "pool": "h"}
        out6 = {}

        def _w6(tid):
            try:
                out6[tid] = ba._raw_call("Runtime.evaluate", {}, pool="h",
                                         tab_id=tid, timeout=10)
            except Exception as e:
                out6[tid] = e

        ths = [threading.Thread(target=_w6, args=(2_000_000 + i,))
               for i in range(3)]
        for t in ths:
            t.start()
        time.sleep(0.1)
        first._ws.q.put("__boom__")
        for t in ths:
            t.join(30)
        check("обрыв сокета: три вкладки в полёте — по ОДНОМУ переattach'у "
              "на таргет (без утечки сессий), все вызовы дошли",
              dict(attaches) == {"T1": 1, "T2": 1, "T3": 1}
              and all(isinstance(v, dict) for v in out6.values())
              and len(made) == 2)
    finally:
        ba._RawCdp = saved6["rawcdp"]
        ba._ensure_pool_h_browser = saved6["ensure"]
        ba._pool_h_cdp_url = saved6["url"]
        ba._sweep_orphan_tabs = saved6["sweep"]
        ba._pool_h_life_path = saved6["life"]
        ba._RAW_CLIENTS.clear()
        ba._RAW_CLIENTS.update(saved6["clients"])
        ba._RAW_TABS.clear()
        ba._RAW_TABS.update(saved6["tabs"])

    # ── 7. _raw_eval: сбой контекста при навигации — не смерть вкладки ──
    orig_tc = ba._raw_tab_call
    saved7 = dict(ba._RAW_TABS)
    try:
        ba._RAW_TABS[990010] = {"targetId": "TN", "sessionId": "SN",
                                "pool": "h"}
        for msg7 in ("Execution context was destroyed.",
                     "Inspected target navigated or closed",
                     "Cannot find context with specified id"):
            ba._raw_tab_call = lambda *a, _m=msg7, **k: (_ for _ in ()).throw(
                ba.BrowserUnavailable(f"CDP Runtime.evaluate: {_m}"))
            err7 = None
            try:
                ba._raw_eval(990010, "1")
            except ba.BrowserUnavailable as e:
                err7 = e
            check(f"raw_eval: «{msg7[:30]}…» дважды — ошибка вызова, "
                  "вкладка остаётся в реестре",
                  err7 is not None and 990010 in ba._RAW_TABS)
        seq = [ba.BrowserUnavailable("CDP Runtime.evaluate: Execution "
                                     "context was destroyed."),
               {"result": {"value": "fine"}}]

        def _once(*a, **k):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        ba._raw_tab_call = _once
        check("raw_eval: разовый сбой контекста — ретрай и результат",
              ba._raw_eval(990010, "1") == "fine")
        ba._raw_tab_call = lambda *a, **k: (_ for _ in ()).throw(
            ba.BrowserUnavailable("CDP Runtime.evaluate: No target"))
        try:
            ba._raw_eval(990010, "1")
        except ba.BrowserUnavailable:
            pass
        check("raw_eval: прочие протокольные отказы по-прежнему роняют вкладку",
              990010 not in ba._RAW_TABS)
    finally:
        ba._raw_tab_call = orig_tc
        ba._RAW_TABS.clear()
        ba._RAW_TABS.update(saved7)

    # ── 8. Ошибка send не оставляет висящую запись ожидания ──
    cl8 = _client(lambda m: None)

    def _bad_send(data):
        raise OSError("broken pipe")
    cl8._ws.send = _bad_send
    try:
        cl8.call("x", timeout=1.0)
    except Exception:
        pass
    check("mux: ошибка send — запись ожидания убрана, клиент помечен мёртвым",
          not cl8._waiting and cl8._dead is not None)

    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

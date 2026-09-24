"""Конкурентность роутера LLM и веб-чат-провайдера:
- _last_provider/_last_local_model — на поток (ThreadLocalAttr);
- гонка создания инстансов ModelRouter._webchats — один инстанс на ключ;
- burst-вкладка закрывается после вызова; потеря вкладки main → burst;
- очередь фона по сайту + общий семафор, короткое ожидание, карантин ДО
  очереди, ограниченное ожидание лока вкладки фона;
- роутер: занятый сайт фона → следующий провайдер, второй заход, если
  занята вся цепочка; короткий таймаут вызывающего = бюджет очереди, пол
  ожидания ответа 150 с остаётся;
- _wait_answer: вкладка исчезла из реестра → мгновенный выход (_TabLost);
- перезапуск Chrome пула запрещён под чужим основным вызовом (в т.ч.
  другого ПРОЦЕССА — flock);
- PersonaContextLayer: LLM-вызов извлечения вне лока, без дублей.
Браузер, сеть и LLM не трогаются — всё на фейках.
Запуск: python -m scripts.test_router_concurrency"""

import json
import multiprocessing
import sys
import tempfile
import threading
import time
from pathlib import Path


def _mp_hold_inflight(inflight_dir: str, pool: str, ready, release):
    """Воркер ОТДЕЛЬНОГО процесса: держит разделяемый flock «идёт основной
    вызов» пула, пока не скажут отпустить (модульная функция — spawn)."""
    from app.features import web_llm as wl
    wl._INFLIGHT_DIR = Path(inflight_dir)
    with wl._inflight_call(pool, foreground=True):
        ready.set()
        release.wait(30)


def _mp_try_inflight(inflight_dir: str, pool: str, q):
    """Воркер отдельного процесса: пробует войти основным вызовом в пул с
    коротким бюджетом ожидания, отдаёт результат в очередь."""
    from app.features import web_llm as wl
    wl._INFLIGHT_DIR = Path(inflight_dir)
    with wl._inflight_call(pool, foreground=True, wait_sec=0.3) as ok:
        q.put(bool(ok))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="router_conc_"))
    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    import app.core.router as rt
    from app.features import web_llm as wl
    from app.features import browser_actions as ba

    # Сеть не трогаем: проба интернета — всегда «онлайн»
    rt.internet_available = lambda: True
    wl._INFLIGHT_DIR = tmp / "inflight"
    wl.POLL_SEC = 0.01

    # Всё, что могло бы дойти до браузера, — фейки (восстановим в конце)
    _ba_saved = {}

    def patch_ba(**kw):
        for k, v in kw.items():
            if k not in _ba_saved:
                _ba_saved[k] = getattr(ba, k)
            setattr(ba, k, v)

    restarts = []
    patch_ba(restart_browser=lambda reason="", **kw: restarts.append(reason) or True,
             reload_tab=lambda *a, **kw: None,
             close_background_tab=lambda t: True,
             detect_antibot=lambda *a, **kw: None,
             try_challenge_autoclick=lambda *a, **kw: False,
             wait_input=lambda *a, **kw: None,
             eval_js=lambda *a, **kw: "",
             pool_h_rescue_active=lambda: False,
             pool_v_webchat_allowed=lambda: True)

    def stub_router(sites):
        r = rt.ModelRouter.__new__(rt.ModelRouter)  # без __init__
        r.context = "test_conc"
        r.available = {}
        r.active_provider = "webchat"
        r.pinned_provider = None
        r.fallback_order = None
        r.model_overrides = {}
        r.webchat_sites = list(sites)
        r._webchats = {}
        r.webchat_limits = {}
        r.webchat_modes = {}
        r._last_key_index = {}
        r._provider_sems = {}
        r.answer_provider = r.cc_provider = r.vision_provider = None
        # Локальную модель не трогаем
        r._try_local = lambda *a, **kw: None
        return r

    # ── 1. _last_provider / _last_local_model — на поток ──
    r = stub_router(["qwen"])
    check("tls: без записей getattr с дефолтом работает как у атрибута",
          getattr(r, "_last_local_model", "?") == "?"
          and getattr(r, "_last_provider", None) is None)
    seen = {}
    b1, b2 = threading.Barrier(2), threading.Barrier(2)

    def _writer(name, val):
        r._last_provider = val
        b1.wait()          # оба записали — чужая запись уже случилась
        b2.wait()
        seen[name] = r._last_provider

    t1 = threading.Thread(target=_writer, args=("a", "webchat:deepseek"))
    t2 = threading.Thread(target=_writer, args=("b", "kimi"))
    t1.start(); t2.start(); t1.join(); t2.join()
    check("tls: каждый поток читает СВОЕГО провайдера, а не последнего записавшего",
          seen == {"a": "webchat:deepseek", "b": "kimi"})
    check("tls: поток без своей записи видит последнее значение любого "
          "(совместимость с общим полем)",
          r._last_provider in ("webchat:deepseek", "kimi"))
    r2 = rt.ModelRouter.__new__(rt.ModelRouter)
    r2._last_provider = "x"
    check("tls: значения разных роутеров не смешиваются",
          r._last_provider != "x" and r2._last_provider == "x")

    # ── 2. Гонка создания инстансов _webchats ──
    class SlowChat:
        created = []

        def __init__(self, site, context=None, channel="main",
                     quota_per_hour=None, browser_pool=None):
            time.sleep(0.2)  # окно гонки «get → создать → записать»
            self.site, self.channel = site, channel
            self.last_call_lock_miss = False
            self.used_by = []
            type(self).created.append(self)

        def get_response(self, messages, **kw):
            self.used_by.append(threading.current_thread().name)
            return "ок"

    _wc = wl.WebChatLLM
    wl.WebChatLLM = SlowChat
    try:
        r = stub_router(["qwen"])
        ths = [threading.Thread(target=r._try_webchat,
                                args=([{"role": "user", "content": "x"}],
                                      0.7, 100, 0.9, 10.0, ["qwen"], "side"),
                                name=f"w{i}") for i in range(2)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        cached = r._webchats.get("qwen#side")
        used = [c for c in SlowChat.created if c.used_by]
        check("webchats: два потока — в кэше ОДИН инстанс и оба вызова шли "
              "через него (не две вкладки на одном чате)",
              cached is not None and used == [cached]
              and sorted(cached.used_by) == ["w0", "w1"])
    finally:
        wl.WebChatLLM = _wc

    # ── 3. Burst: вкладка закрывается; потеря вкладки main → burst ──
    class FakeChat:
        instances = []
        main_mode = "busy"  # busy | lost | ok

        def __init__(self, site, context=None, channel="main",
                     quota_per_hour=None, browser_pool=None):
            self.site, self.channel = site, channel
            self.last_call_lock_miss = False
            self.last_call_tab_lost = False
            self.closed = False
            self.kw = []
            type(self).instances.append(self)

        def get_response(self, messages, **kw):
            self.kw.append(kw)
            if self.channel == "main":
                mode = type(self).main_mode
                self.last_call_lock_miss = mode == "busy"
                self.last_call_tab_lost = mode == "lost"
                return "из main" if mode == "ok" else None
            if self.channel == "burst" and type(self).burst_boom:
                raise RuntimeError("burst упал")
            return f"из {self.channel}"

        def close(self):
            self.closed = True

    FakeChat.burst_boom = False
    wl.WebChatLLM = FakeChat
    try:
        for mode, why in (("busy", "занят"), ("lost", "потерял вкладку")):
            FakeChat.main_mode = mode
            FakeChat.instances = []
            r = stub_router(["deepseek"])
            ans = r._try_webchat([{"role": "user", "content": "привет"}],
                                 0.7, 100, 0.9, 10.0, ["deepseek"], "main")
            bursts = [c for c in FakeChat.instances if c.channel == "burst"]
            check(f"burst: main {why} → ответ из burst, вкладка burst закрыта",
                  ans == "из burst" and len(bursts) == 1 and bursts[0].closed
                  and "deepseek#burst" not in r._webchats)
        FakeChat.main_mode, FakeChat.burst_boom = "busy", True
        FakeChat.instances = []
        r = stub_router(["deepseek"])
        ans = r._try_webchat([{"role": "user", "content": "привет"}],
                             0.7, 100, 0.9, 10.0, ["deepseek"], "main")
        bursts = [c for c in FakeChat.instances if c.channel == "burst"]
        check("burst: вызов упал исключением — вкладка всё равно закрыта",
              ans is None and len(bursts) == 1 and bursts[0].closed)
        FakeChat.burst_boom = False

        # Таймауты: короткий таймаут фона — бюджет очереди, пол ответа 150 с
        FakeChat.instances = []
        r = stub_router(["qwen"])
        r._try_webchat([{"role": "user", "content": "факты"}],
                       0.7, 100, 0.9, 15.0, ["qwen"], "side")
        side = [c for c in FakeChat.instances if c.channel == "side"][0]
        check("timeout: фон с timeout=15 — очередь ждёт ≤15 с (lock_timeout), "
              "ответ после отправки — с полом 150 с",
              side.kw[0]["lock_timeout"] == 15.0
              and side.kw[0]["timeout"] == 150.0)
        FakeChat.instances = []
        r._try_webchat([{"role": "user", "content": "дневник"}],
                       0.7, 100, 0.9, 60.0, ["qwen"], "side")
        side = [c for c in FakeChat.instances if c.channel == "side"]
        check("timeout: фон с timeout=60 — очередь ждёт не дольше "
              "BG_QUEUE_WAIT_SEC",
              r._webchats["qwen#side"].kw[-1]["lock_timeout"]
              == wl.BG_QUEUE_WAIT_SEC)
    finally:
        wl.WebChatLLM = _wc

    # ── 4. Роутер: занятый сайт фона → следующий; второй заход ──
    class QueueChat:
        plan = {}      # site -> список ответов по попыткам: "busy"/None/текст
        calls = []

        def __init__(self, site, context=None, channel="main",
                     quota_per_hour=None, browser_pool=None):
            self.site, self.channel = site, channel
            self.last_call_lock_miss = False
            self.last_call_tab_lost = False

        def get_response(self, messages, **kw):
            type(self).calls.append((self.site, kw.get("lock_timeout"),
                                     self.channel, kw.get("timeout")))
            seq = type(self).plan.get(self.site) or []
            step = seq.pop(0) if seq else None
            self.last_call_lock_miss = step == "busy"
            return None if step in ("busy", None) else step

    wl.WebChatLLM = QueueChat
    try:
        QueueChat.plan = {"qwen": ["busy"], "deepseek": ["ответ deepseek"]}
        QueueChat.calls = []
        r = stub_router(["qwen", "deepseek"])
        ans = r.get_response([{"role": "user", "content": "факты"}],
                             timeout=15.0, webchat_channel="side")
        check("bg-chain: qwen занят — без ожидания к deepseek, ответ оттуда",
              ans == "ответ deepseek"
              and [c[0] for c in QueueChat.calls] == ["qwen", "deepseek"]
              and r._last_provider == "webchat:deepseek")

        QueueChat.plan = {"qwen": ["busy", "busy", "ответ qwen"],
                          "deepseek": ["busy", None]}
        QueueChat.calls = []
        r = stub_router(["qwen", "deepseek"])
        ans = r.get_response([{"role": "user", "content": "факты"}],
                             timeout=15.0, webchat_channel="side")
        retry = QueueChat.calls[2:]
        check("bg-chain: занято всё — второй заход квантами по кругу, "
              "отказавший не по занятости сайт выбывает, работа не теряется",
              ans == "ответ qwen"
              and [c[0] for c in QueueChat.calls]
              == ["qwen", "deepseek", "qwen", "deepseek", "qwen"]
              and all(c[1] <= rt.BG_RETRY_SLICE_SEC for c in retry))

        QueueChat.plan = {"qwen": ["busy", "ответ"]}
        QueueChat.calls = []
        r = stub_router(["qwen"])
        r.active_provider = "webchat"
        ans = r.get_response([{"role": "user", "content": "x"}],
                             webchat_channel="cc")
        check("bg-chain: не фоновый канал — второго захода нет",
              ans is None and len(QueueChat.calls) == 1)
    finally:
        wl.WebChatLLM = _wc

    # ── 5. Очереди фона в web_llm ──
    active = {"total": 0, "max_total": 0, "by_site": {}, "max_by_site": {}}
    alock = threading.Lock()

    def _slow_locked(site):
        def _run(*a, **kw):
            with alock:
                active["total"] += 1
                active["by_site"][site] = active["by_site"].get(site, 0) + 1
                active["max_total"] = max(active["max_total"], active["total"])
                active["max_by_site"][site] = max(
                    active["max_by_site"].get(site, 0), active["by_site"][site])
            time.sleep(0.3)
            with alock:
                active["total"] -= 1
                active["by_site"][site] -= 1
            return f"ответ {site}"
        return _run

    def mk(site, channel="side", sub="x"):
        c = wl.WebChatLLM(site, base_dir=tmp / f"{site}_{channel}_{sub}",
                          channel=channel)
        c._get_response_locked = _slow_locked(site)
        return c

    # Один side-чат персоны держат ДВА инстанса (роутер бота и роутер LTM)
    q1, q2 = mk("qwen", sub="bot"), mk("qwen", sub="ltm")
    res = []
    ths = [threading.Thread(target=lambda c=c: res.append(
        c.get_response([{"role": "user", "content": "x"}], lock_timeout=5)))
        for c in (q1, q2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    check("queue: фон одного сайта (два инстанса одного side-чата) — "
          "строго по одному",
          res == ["ответ qwen"] * 2 and active["max_by_site"]["qwen"] == 1)

    active["max_total"] = 0
    trio = [mk("qwen", sub="a"), mk("deepseek", sub="a"), mk("zai", sub="a")]
    res = []
    t0 = time.monotonic()
    ths = [threading.Thread(target=lambda c=c: res.append(
        c.get_response([{"role": "user", "content": "x"}], lock_timeout=5)))
        for c in trio]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    dt = time.monotonic() - t0
    check(f"queue: разные сайты идут параллельно, но не больше "
          f"BG_MAX_PARALLEL={wl.BG_MAX_PARALLEL} ({dt:.2f}с)",
          len(res) == 3 and all(res)
          and active["max_total"] == wl.BG_MAX_PARALLEL and dt < 0.85)

    # Короткое ожидание: очередь сайта занята — быстрый промах
    kimi = mk("kimi")
    q = wl._bg_site_queue("kimi")
    q.acquire()
    try:
        t0 = time.monotonic()
        ans = kimi.get_response([{"role": "user", "content": "x"}],
                                lock_timeout=0.2)
        dt = time.monotonic() - t0
        check("queue: очередь сайта занята — None за бюджет, "
              "last_call_lock_miss=True",
              ans is None and kimi.last_call_lock_miss is True and dt < 1.0)
    finally:
        q.release()
    ans = kimi.get_response([{"role": "user", "content": "x"}], lock_timeout=0.2)
    check("queue: очередь освободилась — вызов прошёл, флаг промаха сброшен",
          ans == "ответ kimi" and kimi.last_call_lock_miss is False)

    # Флаг промаха — на поток: промах одного потока не виден другому
    seen_flags = {}
    q.acquire()
    try:
        tb = threading.Thread(target=lambda: seen_flags.setdefault(
            "busy", (kimi.get_response([{"role": "user", "content": "x"}],
                                       lock_timeout=0.1),
                     kimi.last_call_lock_miss)))
        tb.start(); tb.join()
    finally:
        q.release()
    check("queue: last_call_lock_miss — потоко-локальный (чужой промах "
          "не протекает)",
          seen_flags["busy"] == (None, True) and kimi.last_call_lock_miss is False)

    # Карантин проверяется ДО очереди
    wl.quarantine_site("zai", "тест", ttl=60)
    zq = wl._bg_site_queue("zai")
    zq.acquire()
    try:
        zai = mk("zai", sub="q")
        t0 = time.monotonic()
        ans = zai.get_response([{"role": "user", "content": "x"}], lock_timeout=5)
        dt = time.monotonic() - t0
        check("queue: сайт на карантине — пропуск мгновенно, очередь не "
              "отстаивается",
              ans is None and dt < 0.5 and zai.last_call_lock_miss is False)
    finally:
        zq.release()
        wl.clear_quarantine("zai")
        wl.pop_quarantine_alerts()

    # Лок вкладки фона — ожидание ограничено бюджетом
    ds = mk("deepseek", sub="lock")
    ds._lock.acquire()
    try:
        t0 = time.monotonic()
        ans = ds.get_response([{"role": "user", "content": "x"}], lock_timeout=0.3)
        dt = time.monotonic() - t0
        check("queue: лок вкладки фона занят — ждём в пределах бюджета, "
              "затем промах",
              ans is None and ds.last_call_lock_miss is True and dt < 1.0)
    finally:
        ds._lock.release()
    check("queue: после промаха очередь сайта и семафор отпущены",
          wl._bg_site_queue("deepseek").acquire(timeout=0.1)
          and (wl._bg_site_queue("deepseek").release() or True)
          and wl._BG_PARALLEL.acquire(timeout=0.1)
          and (wl._BG_PARALLEL.release() or True))

    # ── 6. _wait_answer: вкладка исчезла → мгновенный выход ──
    alive = {"v": True, "gen": 7}
    RAW = 1_000_777
    ticks = {"n": 0}

    def _count_blocks(*a, **kw):
        ticks["n"] += 1
        if ticks["n"] >= 3:
            alive["v"] = False  # перезапуск Chrome пула посреди ожидания
        return 0

    raw_closes = []
    patch_ba(_raw_call=lambda method, params=None, **kw: raw_closes.append(
                 (method, (params or {}).get("targetId"), kw.get("pool"))) or {},
             is_raw_tab=lambda t: t == RAW and alive["v"],
             _raw_tab=lambda t: {"pool": "h", "targetId": "T"},
             raw_pool_generation=lambda pool="h": alive["gen"],
             count_blocks=_count_blocks,
             last_block_text=lambda *a, **kw: "",
             answer_blocks_after=lambda *a, **kw: (None, "", True))
    main_chat = wl.WebChatLLM("qwen", base_dir=tmp / "vanish", channel="main")
    main_chat._save_state({"chat_url": "https://chat.qwen.ai/c/keep"})

    def _ensure(fresh=False):
        main_chat._tab_id = RAW
        main_chat._remember_tab_snap(ba, RAW)  # снимок — при «открытии»
        return RAW
    main_chat._ensure_chat = _ensure
    main_chat._send_verified = lambda *a, **kw: None
    restarts.clear()
    t0 = time.monotonic()
    ans = main_chat.get_response([{"role": "user", "content": "привет"}],
                                 timeout=150.0, lock_timeout=3.0)
    dt = time.monotonic() - t0
    check(f"vanish: вкладка пропала из реестра — выход за {dt:.2f}с, "
          "а не через 150 с; флаг потери для burst",
          ans is None and dt < 2.0 and main_chat.last_call_tab_lost is True
          and main_chat.last_call_lock_miss is False)
    check("vanish: чат не сброшен, Chrome не перезапускался, вкладка забыта",
          main_chat._chat_url() == "https://chat.qwen.ai/c/keep"
          and not restarts and main_chat._tab_id is None)
    for _ in range(50):
        if raw_closes:
            break
        time.sleep(0.02)
    check("vanish: поколение пула то же (Chrome жив) — осиротевшая страница "
          "закрыта по targetId (в фоне)",
          raw_closes == [("Target.closeTarget", "T", "h")])

    # Пул сбросили МЕЖДУ открытием вкладки и ожиданием ответа: снимок взят
    # при открытии, поэтому детект не отключается
    alive["v"], ticks["n"] = True, 0

    def _ensure_then_reset(fresh=False):
        _ensure(fresh)
        alive["v"] = False  # сброс пула сразу после открытия
        return RAW
    main_chat._ensure_chat = _ensure_then_reset
    t0 = time.monotonic()
    ans = main_chat.get_response([{"role": "user", "content": "привет"}],
                                 timeout=150.0, lock_timeout=3.0)
    check("vanish: пул сброшен до ожидания — снимок с открытия ловит потерю",
          ans is None and time.monotonic() - t0 < 2.0
          and main_chat.last_call_tab_lost is True)
    main_chat._ensure_chat = _ensure
    raw_closes.clear()

    # Поколение пула сменилось (Chrome перезапущен), вкладка ещё в реестре
    alive["v"], ticks["n"] = True, 0

    def _count_gen(*a, **kw):
        ticks["n"] += 1
        if ticks["n"] >= 3:
            alive["gen"] += 1
        return 0
    patch_ba(count_blocks=_count_gen)
    t0 = time.monotonic()
    ans = main_chat.get_response([{"role": "user", "content": "привет"}],
                                 timeout=150.0, lock_timeout=3.0)
    check("vanish: смена поколения пула — тоже мгновенный выход",
          ans is None and time.monotonic() - t0 < 2.0
          and main_chat.last_call_tab_lost is True)
    time.sleep(0.2)
    check("vanish: Chrome перезапускался — по targetId не закрываем "
          "(страница умерла вместе с ним)",
          not [c for c in raw_closes if c[0] == "Target.closeTarget"])

    # Отправка упала, потому что вкладку убили, — без reload/перезапуска
    alive["v"] = True

    def _send_dies(*a, **kw):
        alive["v"] = False
        raise ba.BrowserUnavailable("фоновая вкладка закрыта")
    main_chat._send_verified = _send_dies
    main_chat._send_fail_streak = 0
    restarts.clear()
    ans = main_chat.get_response([{"role": "user", "content": "привет"}],
                                 lock_timeout=3.0)
    check("vanish: отправка упала из-за смерти вкладки — не «залипание»: "
          "без счётчика отказов, reload и перезапуска Chrome",
          ans is None and main_chat.last_call_tab_lost is True
          and main_chat._send_fail_streak == 0 and not restarts
          and main_chat._chat_url() == "https://chat.qwen.ai/c/keep")

    # ── 7. Перезапуск Chrome пула — не под чужим основным вызовом ──
    def restart_from(chat):
        chat._last_tab_reload_ts = time.time()  # reload недавно был
        chat._tab_id = None
        restarts.clear()
        chat._restart_stuck_browser(ba, "залипло")
        return bool(restarts)

    bg_chat = wl.WebChatLLM("deepseek", base_dir=tmp / "rs_bg", channel="side")
    fg_chat = wl.WebChatLLM("qwen", base_dir=tmp / "rs_fg", channel="main")
    hold, go = threading.Event(), threading.Event()

    def _main_call():
        with wl._inflight_call("h", foreground=True):
            hold.set()
            go.wait(10)
    tm = threading.Thread(target=_main_call)
    tm.start()
    hold.wait(5)
    try:
        check("restart: фон под идущим основным ответом Chrome НЕ перезапускает",
              restart_from(bg_chat) is False)
        check("restart: и основной вызов под ЧУЖИМ основным — тоже нет",
              restart_from(fg_chat) is False)
    finally:
        go.set()
        tm.join()
    check("restart: основных вызовов в пуле нет — перезапуск разрешён",
          restart_from(bg_chat) is True)
    with wl._inflight_call("h", foreground=True):
        check("restart: основной вызов, кроме него в пуле никого — разрешён "
              "(свой вызов и свой flock не в счёт)",
              restart_from(fg_chat) is True)
    with wl._inflight_call("v", foreground=True):
        check("restart: основной вызов в ДРУГОМ пуле не мешает",
              restart_from(bg_chat) is True)
    with wl._inflight_call("h", foreground=False):
        check("restart: чужой ФОНОВЫЙ вызов перезапуск не блокирует",
              restart_from(bg_chat) is True)

    # TOCTOU: основной вызов, начатый ВО ВРЕМЯ перезапуска, ждёт его
    # конца и не попадает под убийство Chrome
    order = []

    def _slow_restart(reason="", **kw):
        order.append(("restart_start", wl._INFLIGHT_FG.get("h", 0)))
        time.sleep(0.5)
        order.append(("restart_end", wl._INFLIGHT_FG.get("h", 0)))
        return True
    patch_ba(restart_browser=_slow_restart)
    entered = []

    def _late_main():
        time.sleep(0.1)  # стартует посреди перезапуска
        with wl._inflight_call("h", foreground=True) as ok_in:
            entered.append((ok_in, time.monotonic()))
    tl = threading.Thread(target=_late_main)
    bg_chat._last_tab_reload_ts = time.time()
    tl.start()
    t_rs = time.monotonic()
    bg_chat._restart_stuck_browser(ba, "залипло")
    t_done = time.monotonic()
    tl.join()
    check("restart/TOCTOU: основной вызов, начатый во время перезапуска, "
          "ждёт его конца; Chrome убит без основных вызовов в пуле",
          [o[1] for o in order] == [0, 0] and entered
          and entered[0][0] is True and entered[0][1] >= t_rs + 0.45)
    # Перезапуск дольше бюджета ожидания — вызов не начинается (фолбэк)
    _wait_saved = wl.INFLIGHT_RESTART_WAIT_SEC
    wl.INFLIGHT_RESTART_WAIT_SEC = 0.1
    entered.clear()
    tl = threading.Thread(target=_late_main)
    tl.start()
    bg_chat._restart_stuck_browser(ba, "залипло")
    tl.join()
    wl.INFLIGHT_RESTART_WAIT_SEC = _wait_saved
    check("restart/TOCTOU: перезапуск дольше бюджета ожидания — основной "
          "вызов не начинается (False → фолбэк), не висит",
          entered and entered[0][0] is False)
    patch_ba(restart_browser=lambda reason="", **kw: restarts.append(reason) or True)

    # Другой процесс бота с идущим основным вызовом (flock)
    try:
        import fcntl  # noqa: F401
        has_fcntl = True
    except ImportError:
        has_fcntl = False
    if has_fcntl:
        ctx = multiprocessing.get_context("spawn")
        ready, release = ctx.Event(), ctx.Event()
        p = ctx.Process(target=_mp_hold_inflight,
                        args=(str(wl._INFLIGHT_DIR), "h", ready, release))
        p.start()
        try:
            got = ready.wait(60)
            check("restart: основной вызов в ДРУГОМ процессе (flock) — "
                  "перезапуск отложен",
                  got and restart_from(bg_chat) is False)
        finally:
            release.set()
            p.join(30)
        check("restart: процесс отпустил — перезапуск снова разрешён",
              restart_from(bg_chat) is True)

        # Обратное направление: этот процесс перезапускает пул и держит
        # эксклюзивный flock — основной вызов ДРУГОГО процесса не входит
        q_res = ctx.Queue()
        blocker = wl._begin_restart("h", own_foreground=False)
        try:
            p2 = ctx.Process(target=_mp_try_inflight,
                             args=(str(wl._INFLIGHT_DIR), "h", q_res))
            p2.start()
            got2 = q_res.get(timeout=60)
            p2.join(30)
        finally:
            wl._end_restart("h")
        p3 = ctx.Process(target=_mp_try_inflight,
                         args=(str(wl._INFLIGHT_DIR), "h", q_res))
        p3.start()
        got3 = q_res.get(timeout=60)
        p3.join(30)
        check("restart: пока идёт перезапуск (эксклюзивный flock), основной "
              "вызов другого процесса не начинается — ретрай с потолком, "
              "не вечное ожидание; после — входит",
              blocker is None and got2 is False and got3 is True)

    # ── 8. PersonaContextLayer: LLM вне лока, без дублей ──
    import app.core.persona_context as pcm
    _gdp = pcm.get_db_paths
    pcm.get_db_paths = lambda ctx: {"stm": str(tmp / "pc" / "stm.db")}
    try:
        calls = []
        started = threading.Event()

        class SlowRouter:
            def get_response(self, messages, **kw):
                calls.append(kw.get("webchat_channel"))
                started.set()
                time.sleep(0.6)
                return json.dumps({"personality_summary": "тест",
                                   "world_binding": {"type": "real_world"}})

        layer = pcm.PersonaContextLayer("pc_test", router=SlowRouter())
        out = []
        ths = [threading.Thread(target=lambda: out.append(layer.get("ПРОМПТ")))
               for _ in range(2)]
        ths[0].start()
        started.wait(5)
        ths[1].start()
        t0 = time.monotonic()
        got_lock = layer._lock.acquire(timeout=0.3)
        lock_dt = time.monotonic() - t0
        if got_lock:
            layer._lock.release()
        for t in ths:
            t.join()
        check("persona_context: пока идёт LLM-извлечение, лок слоя свободен "
              "(очистка чата не ждёт извлечения)",
              got_lock and lock_dt < 0.3)
        check("persona_context: второй поток ждёт первое извлечение — "
              "один LLM-вызов, одинаковый результат",
              len(calls) == 1 and len(out) == 2 and out[0] == out[1]
              and out[0]["personality_summary"] == "тест")
        check("persona_context: кэш записан, повторный get без LLM",
              layer.get("ПРОМПТ")["personality_summary"] == "тест"
              and len(calls) == 1)

        # Правка промпта во время извлечения старого: оба извлекаются
        # (разные хэши — не ждут друг друга), в кэш — только актуальный
        calls.clear()
        started.clear()
        layer2 = pcm.PersonaContextLayer("pc_test2", router=SlowRouter())
        outs = {}
        ta = threading.Thread(target=lambda: outs.__setitem__(
            "old", layer2.get("СТАРЫЙ")))
        ta.start()
        started.wait(5)
        tb2 = threading.Thread(target=lambda: outs.__setitem__(
            "new", layer2.get("НОВЫЙ")))
        tb2.start()
        ta.join(); tb2.join()
        check("persona_context: разные промпты не ждут друг друга, результат "
              "устаревшего промпта в кэш не пишется",
              len(calls) == 2 and "old" in outs and "new" in outs
              and layer2._cache["hash"] == pcm._hash_prompt("НОВЫЙ")
              and not layer2._extracting)
    finally:
        pcm.get_db_paths = _gdp

    # ── 9. user_path: фон на пути ответа — короткая очередь, без 2-го захода ──
    wl.WebChatLLM = QueueChat
    try:
        QueueChat.plan = {"qwen": ["busy", "ответ qwen"]}
        QueueChat.calls = []
        r = stub_router(["qwen"])
        ans = r.get_response([{"role": "user", "content": "кто он?"}],
                             webchat_channel="side", user_path=True)
        check("user_path: side на пути ответа — очередь ≤ "
              "USER_PATH_QUEUE_WAIT_SEC, занято → None без второго захода",
              ans is None and len(QueueChat.calls) == 1
              and QueueChat.calls[0][1] == wl.USER_PATH_QUEUE_WAIT_SEC)
        check("user_path: разовый канал inline (не постоянный side, без "
              "фоновой очереди/семафора), ответ ждётся таймаутом "
              "вызывающего без пола 150 с",
              QueueChat.calls[0][2] == wl.USER_PATH_CHANNEL
              and QueueChat.calls[0][3] == 60.0
              and wl.USER_PATH_CHANNEL in wl._STATELESS_CHANNELS
              and wl.USER_PATH_CHANNEL in wl._NO_TIMEOUT_FLOOR_CHANNELS
              and wl.USER_PATH_CHANNEL not in wl._BACKGROUND_CHANNELS
              and "qwen#inline" in r._webchats)
        # Метка провайдера сбрасывается в начале вызова: поток пула не
        # отдаёт метку своего прошлого запроса
        QueueChat.plan = {"qwen": ["ответ", None]}
        r = stub_router(["qwen"])
        a1 = r.get_response([{"role": "user", "content": "1"}])
        lp1 = r._last_provider
        a2 = r.get_response([{"role": "user", "content": "2"}])
        check("last_provider: неудачный вызов не наследует метку прошлого "
              "запроса этого потока",
              a1 == "ответ" and lp1 == "webchat:qwen" and a2 is None
              and r._last_provider is None)
        QueueChat.plan = {"qwen": ["busy", "ответ qwen"]}
        QueueChat.calls = []
        tokens = []
        ans = r.get_response_stream([{"role": "user", "content": "x"}],
                                    tokens.append, webchat_channel="side",
                                    user_path=True)
        check("user_path: и в стрим-варианте второго захода нет",
              ans is None and len(QueueChat.calls) == 1 and not tokens)
    finally:
        wl.WebChatLLM = _wc

    # ── 10. Локальный роутер: короткая очередь веб-чата → откат на локальную модель ──
    import app.core.local_router as lrm
    lr = lrm.LocalLLMRouter.__new__(lrm.LocalLLMRouter)
    lr._task_cfg = {"query_rewrite": {"backend": "webchat", "site": "qwen"}}
    lr._available = False
    got_kw = {}

    class _LChat:
        def get_response(self, messages, **kw):
            got_kw.update(kw)
            return None
    lr._get_webchat = lambda site=None: _LChat()
    lr.get_response([{"role": "user", "content": "x"}], task="query_rewrite")
    check("local_router: очередь side ждётся "
          f"{lrm.LOCAL_WEBCHAT_QUEUE_WAIT_SEC}с, а не до 600 с",
          got_kw.get("lock_timeout") == lrm.LOCAL_WEBCHAT_QUEUE_WAIT_SEC)
    lr.get_response([{"role": "user", "content": "x"}], task="query_rewrite",
                    queue_wait=42.0)
    check("local_router: явный queue_wait уважается",
          got_kw.get("lock_timeout") == 42.0)

    # ── 11. retire: сброс кэша закрывает вкладки старых инстансов ──
    closed_tabs = []
    patch_ba(close_background_tab=lambda t: closed_tabs.append(t) or True)
    free = wl.WebChatLLM("qwen", base_dir=tmp / "ret_free", channel="main")
    free._tab_id = 1_000_901
    busy_i = wl.WebChatLLM("deepseek", base_dir=tmp / "ret_busy", channel="main")
    busy_i._tab_id = 1_000_902
    in_call, finish = threading.Event(), threading.Event()

    def _long_locked(*a, **kw):
        in_call.set()
        finish.wait(5)
        return "ответ"
    busy_i._get_response_locked = _long_locked
    res_busy = []
    tb = threading.Thread(target=lambda: res_busy.append(busy_i.get_response(
        [{"role": "user", "content": "x"}], lock_timeout=3.0)))
    tb.start()
    in_call.wait(5)
    r = stub_router(["qwen", "deepseek"])
    r._webchats = {"qwen": free, "deepseek": busy_i}
    r.webchat_site = "qwen"
    check("retire: свободный инстанс — вкладка закрыта сразу, кэш пуст",
          closed_tabs == [1_000_901] and r._webchats == {}
          and free._tab_id is None)
    check("retire: инстанс с идущим вызовом — вкладку не трогаем посреди ответа",
          busy_i._tab_id == 1_000_902 and 1_000_902 not in closed_tabs)
    finish.set()
    tb.join()
    check("retire: вызов закончился — ответ отдан, вкладка закрыта",
          res_busy == ["ответ"] and 1_000_902 in closed_tabs
          and busy_i._tab_id is None)

    for k, v in _ba_saved.items():
        setattr(ba, k, v)
    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

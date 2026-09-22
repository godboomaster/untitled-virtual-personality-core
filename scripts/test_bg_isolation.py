"""Тесты изоляции фона от диалога (кейс 19.09: фоновая генерация держала
лок main-чата, ответ пользователю ждал; KIMI падал с 403 concurrent):
burst-фолбэк роутера (занят main → свежий чат), last_call_lock_miss,
семафор max_concurrent API-провайдеров, фоновые каналы rhythm/reminder.
Браузерные вызовы не трогаем — локи и HTTP мокаются.
Запуск: python -m scripts.test_bg_isolation"""

import sys
import tempfile
import threading
import types
from pathlib import Path


def main():
    tmp = Path(tempfile.mkdtemp(prefix="bgiso_data_"))
    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    from app.features import web_llm as wl
    from app.core import router as rt

    # ── 1. Канал burst — stateless, но с полом таймаута (настоящий ответ) ──
    check("burst в _STATELESS_CHANNELS, но не в _NO_TIMEOUT_FLOOR_CHANNELS",
          "burst" in wl._STATELESS_CHANNELS
          and "burst" not in wl._NO_TIMEOUT_FLOOR_CHANNELS)
    chat_burst = wl.WebChatLLM("deepseek", base_dir=tmp, channel="burst")
    check("burst-инстанс: stateless=True",
          chat_burst.stateless is True
          and chat_burst.last_call_lock_miss is False)

    # ── 2. last_call_lock_miss: промах по локу отличим от прочих None ──
    chat = wl.WebChatLLM("deepseek", base_dir=tmp, channel="main")
    chat._get_response_locked = lambda *a, **kw: "ok"  # браузер не нужен
    chat._lock.acquire()  # держим лок — чат занят другой генерацией
    ans = chat.get_response([{"role": "user", "content": "hi"}],
                            lock_timeout=0.2)
    check("занятый лок: None + last_call_lock_miss=True",
          ans is None and chat.last_call_lock_miss is True)
    chat._lock.release()
    ans = chat.get_response([{"role": "user", "content": "hi"}],
                            lock_timeout=0.2)
    check("свободный лок: ответ + last_call_lock_miss сброшен",
          ans == "ok" and chat.last_call_lock_miss is False)

    # ── 3. Burst в роутере: main занят → свежий чат, не-main — без burst ──
    class FakeChat:
        instances = []

        def __init__(self, site, context=None, channel="main",
                     quota_per_hour=None, browser_pool=None):
            self.site = site
            self.channel = channel
            self.last_call_lock_miss = False
            self.lock_timeouts = []
            type(self).instances.append(self)

        def get_response(self, messages, temperature=0.7, max_tokens=2000,
                         top_p=0.9, timeout=60.0, lock_timeout=None):
            self.lock_timeouts.append(lock_timeout)
            if self.channel == "main":
                # Имитация занятого лока main-чата
                self.last_call_lock_miss = True
                return None
            return f"ответ из {self.channel}"

    _orig_wc = wl.WebChatLLM
    wl.WebChatLLM = FakeChat
    try:
        r = rt.ModelRouter()
        r.webchat_sites = ["deepseek"]
        FakeChat.instances = []
        ans = r._try_webchat([{"role": "user", "content": "привет"}],
                             0.7, 100, 0.9, 10.0, sites=["deepseek"],
                             channel="main")
        mains = [c for c in FakeChat.instances if c.channel == "main"]
        bursts = [c for c in FakeChat.instances if c.channel == "burst"]
        check("main занят → burst-инстанс, ответ отдан, lock_timeout=3с",
              ans == "ответ из burst"
              and len(mains) == 1 and len(bursts) == 1
              and mains[0].lock_timeouts == [rt.BURST_LOCK_WAIT_SEC]
              and bursts[0].lock_timeouts == [None]
              and "deepseek" in r._webchats
              and "deepseek#burst" not in r._webchats)  # burst не кэшируется

        # main ответил сам (без занятости) — burst не нужен
        class FakeOk(FakeChat):
            def get_response(self, *a, **kw):
                self.lock_timeouts.append(kw.get("lock_timeout"))
                return "ответ из main"
        wl.WebChatLLM = FakeOk
        r2 = rt.ModelRouter()
        r2.webchat_sites = ["deepseek"]
        FakeOk.instances = []
        ans2 = r2._try_webchat([{"role": "user", "content": "привет"}],
                               0.7, 100, 0.9, 10.0, sites=["deepseek"],
                               channel="main")
        check("main свободен → без burst",
              ans2 == "ответ из main"
              and len([c for c in FakeOk.instances if c.channel == "main"]) == 1
              and not [c for c in FakeOk.instances if c.channel == "burst"])

        # Не-main канал: без burst и без lock_timeout (фон может ждать)
        wl.WebChatLLM = FakeChat
        r3 = rt.ModelRouter()
        r3.webchat_sites = ["deepseek"]
        FakeChat.instances = []
        ans3 = r3._try_webchat([{"role": "user", "content": "фон"}],
                               0.7, 100, 0.9, 10.0, sites=["deepseek"],
                               channel="side")
        sides = [c for c in FakeChat.instances if c.channel == "side"]
        check("канал side: без burst, lock_timeout=None",
              ans3 == "ответ из side" and len(sides) == 1
              and sides[0].lock_timeouts == [None]
              and not [c for c in FakeChat.instances if c.channel == "burst"])

        # Burst тоже молчит → None (цепочка идёт дальше)
        class FakeAllBusy(FakeChat):
            def get_response(self, *a, **kw):
                self.lock_timeouts.append(kw.get("lock_timeout"))
                if self.channel == "main":
                    self.last_call_lock_miss = True
                return None
        wl.WebChatLLM = FakeAllBusy
        r4 = rt.ModelRouter()
        r4.webchat_sites = ["deepseek"]
        FakeAllBusy.instances = []
        ans4 = r4._try_webchat([{"role": "user", "content": "привет"}],
                               0.7, 100, 0.9, 10.0, sites=["deepseek"],
                               channel="main")
        check("burst не ответил → None (фолбэк по цепочке)",
              ans4 is None
              and len([c for c in FakeAllBusy.instances
                       if c.channel == "burst"]) == 1)
    finally:
        wl.WebChatLLM = _orig_wc

    # ── 4. Семафор max_concurrent: занят → мгновенный None, без HTTP ──
    r5 = rt.ModelRouter()
    cfg = {"api_keys": ["k1"], "base_url": "http://127.0.0.1:9",
           "model": "m", "max_concurrent": 1}
    sem = r5._provider_sem("kimi", cfg)
    check("семафор создаётся по max_concurrent; без ключа — None",
          sem is not None
          and r5._provider_sem("openai", {"api_keys": []}) is None)

    class _Completions:
        def create(self, **kw):
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content="ок"))])

    class FakeOpenAI:
        calls = 0

        def __init__(self, **kw):
            FakeOpenAI.calls += 1
            self.chat = types.SimpleNamespace(completions=_Completions())

    _orig_oai = rt.OpenAI
    rt.OpenAI = FakeOpenAI
    try:
        sem.acquire()  # провайдер занят фоновой задачей
        FakeOpenAI.calls = 0
        ans = r5._call_with_keys("kimi", cfg, [{"role": "user",
                                                "content": "hi"}],
                                 0.7, 100, 0.9, 5.0)
        check("провайдер занят → мгновенный None, HTTP-вызовов нет",
              ans is None and FakeOpenAI.calls == 0)
        sem.release()
        ans = r5._call_with_keys("kimi", cfg, [{"role": "user",
                                                "content": "hi"}],
                                 0.7, 100, 0.9, 5.0)
        check("после release вызов проходит и семафор свободен",
              ans == "ок" and FakeOpenAI.calls == 1
              and sem.acquire(blocking=False))
        sem.release()
    finally:
        rt.OpenAI = _orig_oai

    # ── 5. Фоновые генераторы ушли с канала main ──
    class CapRouter:
        def __init__(self):
            self.kw = None

        def get_response(self, messages, **kw):
            self.kw = kw
            return "достаточно длинный ответ персоны"

    from app.features.rhythm_manager import RhythmManager
    rm = RhythmManager.__new__(RhythmManager)
    rm._router = CapRouter()
    rm._persona = types.SimpleNamespace(system_prompt="Ты — Коннор.")
    rm._generate_text("temp", "ясно, +5", "Russian")
    check("rhythm: webchat_channel=proactive",
          rm._router.kw.get("webchat_channel") == "proactive")

    from app.features.reminder_manager import ReminderManager
    rem = ReminderManager.__new__(ReminderManager)
    rem._router = CapRouter()
    rem._persona = types.SimpleNamespace(system_prompt="Ты — Коннор.")
    rem._living = None
    rem._generate_reminder_text("Test", "позвонить маме", "Russian")
    check("reminder: webchat_channel=proactive",
          rem._router.kw.get("webchat_channel") == "proactive")

    print(f"\nИтог: {ok} проверок")
    sys.exit(0 if ok > 0 else 1)


if __name__ == "__main__":
    main()

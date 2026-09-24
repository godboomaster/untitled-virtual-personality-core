"""Тест router.internet_available: путь ответа не ждёт TCP-проб — истёкший
кэш сразу отдаёт последний вердикт, проба идёт одним фоновым потоком без
дублей; ждут только первый вызов без вердикта и вызов, запустивший пробу
при устаревшем «офлайн». Офлайн фиксируется после двух пустых серий,
note_internet_ok подтверждает онлайн; сетевая ошибка облака — лишь повод
перепроверить.
Сеть не трогается — _probe_round подменяется.
Запуск: python -m scripts.test_internet_probe"""

import sys
import threading
import time


def main():
    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    import app.core.router as rt

    saved = {k: getattr(rt, k) for k in (
        "_probe_round", "_NET_FIRST_WAIT_SEC", "_NET_STALE_OFFLINE_WAIT_SEC")}
    rt._NET_FIRST_WAIT_SEC = 0.3
    rt._NET_STALE_OFFLINE_WAIT_SEC = 0.15

    rounds: list[int] = []
    gate = threading.Event()   # «сеть висит»: проба ждёт, пока не отпустят

    def set_state(v, age):
        with rt._net_lock:
            rt._net_ok, rt._net_checked = v, time.monotonic() - age

    def wait_idle(t=3.0):
        end = time.monotonic() + t
        while rt._net_refresh_done is not None and time.monotonic() < end:
            time.sleep(0.01)
        return rt._net_refresh_done is None

    def probe_false():
        rounds.append(1)
        return False

    def probe_true():
        rounds.append(1)
        return True

    def probe_hang(result):
        def f():
            rounds.append(1)
            gate.wait(5)
            return result
        return f

    # 1. Первый вызов, сеть мертва и отвечает быстро: две пустые серии — офлайн
    rounds.clear()
    rt._probe_round = probe_false
    set_state(None, 0)
    r = rt.internet_available()
    check("первый вызов: офлайн после двух пустых серий", r is False
          and len(rounds) == 2 and wait_idle())

    # 2. Первый вызов, проба висит: ждём не дольше _NET_FIRST_WAIT_SEC и
    #    считаем «онлайн», вердикт не записан
    rounds.clear()
    gate.clear()
    rt._probe_round = probe_hang(False)
    set_state(None, 0)
    t0 = time.monotonic()
    r = rt.internet_available()
    dt = time.monotonic() - t0
    check(f"первый вызов, проба висит: «онлайн» за {dt:.2f} с (≤ 0.3 + ε)",
          r is True and dt < 0.6 and rt._net_ok is None)
    gate.set()
    wait_idle()
    check("проба первого вызова досчиталась в фоне — «офлайн» записан",
          rt._net_ok is False and len(rounds) == 2)

    # 3. Устаревший «офлайн», сеть всё ещё висит: 10 параллельных ответов —
    #    одна проба, запустивший ждёт ≤ _NET_STALE_OFFLINE_WAIT_SEC, прочие —
    #    сразу; все получают «офлайн» по кэшу
    rounds.clear()
    gate.clear()
    rt._probe_round = probe_hang(False)
    set_state(False, 60)
    res, times = [], []
    lock = threading.Lock()

    def worker():
        t = time.monotonic()
        v = rt.internet_available()
        with lock:
            res.append(v)
            times.append(time.monotonic() - t)

    ths = [threading.Thread(target=worker) for _ in range(10)]
    for th in ths:
        th.start()
    for th in ths:
        th.join(5)
    check(f"устаревший офлайн, 10 вызовов: одна проба ({len(rounds)}), все "
          f"«офлайн», максимум ожидания {max(times):.2f} с",
          len(rounds) == 1 and res == [False] * 10 and max(times) < 0.5
          and sorted(times)[-2] < 0.1)
    gate.set()
    wait_idle()

    # 4. Устаревший «офлайн», сеть вернулась: первый же хост отвечает —
    #    запустивший пробу получает «онлайн» сразу, ответ идёт в облако
    rounds.clear()
    rt._probe_round = probe_true
    set_state(False, 60)
    r = rt.internet_available()
    check("устаревший офлайн, сеть вернулась: «онлайн» тем же вызовом",
          r is True and len(rounds) == 1 and wait_idle())

    # 5. Устаревший «онлайн», проба висит: старый вердикт мгновенно; проба
    #    досчиталась «офлайн» — следующий вызов уже «офлайн»
    rounds.clear()
    gate.clear()
    rt._probe_round = probe_hang(False)
    set_state(True, 120)
    t0 = time.monotonic()
    r = rt.internet_available()
    dt = time.monotonic() - t0
    check(f"устаревший онлайн: старый вердикт за {dt*1000:.0f} мс",
          r is True and dt < 0.05)
    gate.set()
    wait_idle()
    check("фоновая проба: две пустые серии — следующий вызов «офлайн»",
          rt.internet_available() is False and len(rounds) == 2)

    # 6. note_internet_ok во время пробы — факт свежее её «офлайн»
    rounds.clear()
    gate.clear()
    rt._probe_round = probe_hang(False)
    set_state(True, 120)
    rt.internet_available()
    time.sleep(0.02)
    rt.note_internet_ok()
    gate.set()
    wait_idle()
    check("ответ облака во время пробы: «офлайн» пробы его не перетирает",
          rt._net_ok is True and rt.internet_available() is True)

    # 7. Сбой самой пробы: вердикт не меняется, флаг пробы снят
    def probe_boom():
        raise RuntimeError("boom")
    rt._probe_round = probe_boom
    set_state(False, 60)
    r = rt.internet_available()
    check("проба упала: вердикт прежний, следующая проба возможна",
          r is False and wait_idle() and rt._net_ok is False)

    # 8. note_internet_suspect: свежий вердикт — без пробы; старше порога —
    #    одна фоновая проба (повтор не дублирует), вызывающий не ждёт
    rounds.clear()
    gate.clear()
    rt._probe_round = probe_hang(True)
    set_state(True, 0)
    rt.note_internet_suspect()
    check("suspect при свежем вердикте — пробы нет",
          rt._net_refresh_done is None and not rounds)
    set_state(True, 10)
    t0 = time.monotonic()
    rt.note_internet_suspect()
    rt.note_internet_suspect()
    dt = time.monotonic() - t0
    time.sleep(0.05)
    check(f"suspect при старом вердикте — одна фоновая проба, без ожидания "
          f"({dt*1000:.0f} мс)", len(rounds) == 1 and dt < 0.05
          and rt._net_ok is True)
    gate.set()
    wait_idle()

    # 9. Классификация ошибок облака для подсказки
    import httpx
    from openai import APIConnectionError, APITimeoutError
    req = httpx.Request("POST", "https://example.invalid")
    check("сетевые ошибки: APIConnectionError/APITimeoutError/OSError — да; "
          "ValueError — нет",
          rt._is_network_error(APIConnectionError(request=req))
          and rt._is_network_error(APITimeoutError(request=req))
          and rt._is_network_error(ConnectionRefusedError())
          and not rt._is_network_error(ValueError("bad key")))

    # 10. Роутер: сетевая ошибка облака → досрочная проба; ошибка API — нет
    class _Boom:
        def __init__(self, exc):
            self.exc = exc

        def __call__(self, *a, **kw):
            exc = self.exc

            class _C:
                class chat:
                    class completions:
                        @staticmethod
                        def create(**kw):
                            raise exc
            return _C()

    saved_openai = rt.OpenAI
    router = rt.ModelRouter.__new__(rt.ModelRouter)
    router._last_key_index = {}
    router.model_overrides = {}
    cfg = {"api_keys": ["k"], "base_url": "http://x", "model": "m"}
    calls = []
    saved_suspect = rt.note_internet_suspect
    rt.note_internet_suspect = lambda: calls.append(1)
    try:
        rt.OpenAI = _Boom(APIConnectionError(request=req))
        router._call_with_keys_locked("p", cfg, [], 0.5, 10, 1.0, 5)
        n_net = len(calls)
        rt.OpenAI = _Boom(ValueError("401"))
        router._call_with_keys_locked("p", cfg, [], 0.5, 10, 1.0, 5)
        n_api = len(calls) - n_net
    except Exception as e:
        print(f"    роутер упал: {e!r}")
        n_net, n_api = -1, -1
    finally:
        rt.OpenAI = saved_openai
        rt.note_internet_suspect = saved_suspect
    check("роутер: сетевая ошибка облака → suspect; ошибка API — нет",
          n_net == 1 and n_api == 0)

    for k, v in saved.items():
        setattr(rt, k, v)
    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

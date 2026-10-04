"""Smoke-тест эндпоинтов панели на рабочем столе (desktop/):

  - GET /api/system/status — браузеры бота и карантины веб-чатов без персоны;
    сетевой пробник пулов идёт не в event loop;
  - POST /api/system/shutdown — ответ сразу, SIGTERM себе — после ответа
    (uvicorn гасит сервер штатно, как по Ctrl+C);
  - POST /api/browser/rescue и /rescue/finish — пул H видимым и «готово»;
  - всё за API_TOKEN, если он задан, и чужая страница (Origin) получает отказ.

Браузер, веб-чаты и сам сигнал подменены — ничего настоящего не трогается.

Запуск: PYTHONPATH=. python3 scripts/test_desktop_api.py
"""

import signal
import sys
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


def main():
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return 0

    import app.api.server as server_mod
    from app.features import browser_actions as ba
    from app.features import web_llm as wl

    orig_token = server_mod._api_token
    server_mod._api_token = ""
    loop_threads = []

    def _pool_status():
        loop_threads.append(threading.current_thread())
        return {"h": {"alive": True, "mode": "headless", "rescue": False},
                "v": {"alive": False, "idle_sec": 5}}

    quarantine = {"deepseek": {"until": time.time() + 600, "reason": "капча",
                               "kind": "challenge", "pool": "h"}}
    try:
        with mock.patch.object(ba, "pool_status", _pool_status), \
                mock.patch.object(wl, "quarantine_status", return_value=quarantine), \
                mock.patch.object(ba, "rescue_pool_h", return_value=True) as m_rescue, \
                mock.patch.object(wl, "finish_idle_rescue", return_value=False) as m_finish, \
                mock.patch.object(ba, "shutdown_browser"), \
                mock.patch("app.api.warmup.start_warmup"), \
                mock.patch("signal.raise_signal") as m_signal:
            # with — живой event loop между запросами: call_later успевает
            with TestClient(server_mod.app, base_url="http://127.0.0.1") as client:
                section("A. GET /api/system/status")
                r = client.get("/api/system/status")
                body = r.json() if r.status_code == 200 else {}
                check("200", r.status_code == 200)
                check("пулы браузера отданы как есть",
                      body.get("browser_pools", {}).get("h", {}).get("mode") == "headless")
                check("карантины веб-чатов отданы",
                      "deepseek" in body.get("webchat_quarantine", {}))
                check("pid процесса в ответе", isinstance(body.get("pid"), int))
                # asyncio.to_thread — пул потоков loop'а с префиксом «asyncio»
                check("пробник пулов — в потоке пула, не в event loop",
                      bool(loop_threads) and loop_threads[0].name.startswith("asyncio"))

                section("B. POST /api/browser/rescue, /rescue/finish")
                r = client.post("/api/browser/rescue")
                check("rescue: 200 и ok=true",
                      r.status_code == 200 and r.json().get("ok") is True)
                check("rescue: вызван rescue_pool_h", m_rescue.call_count == 1)
                r = client.post("/api/browser/rescue/finish")
                check("finish: 200 и finished=false (капчу ещё ждут)",
                      r.status_code == 200 and r.json().get("finished") is False)
                check("finish: правило finish_idle_rescue", m_finish.call_count == 1)

                section("C. POST /api/system/shutdown")
                r = client.post("/api/system/shutdown")
                check("200 и ok=true сразу", r.status_code == 200 and r.json().get("ok") is True)
                check("сигнала ещё нет в момент ответа", m_signal.call_count == 0)
                time.sleep(0.6)
                check("после ответа — SIGTERM себе",
                      m_signal.call_count == 1
                      and m_signal.call_args.args == (signal.SIGTERM,))

                section("D. Origin и API_TOKEN")
                r = client.post("/api/system/shutdown",
                                headers={"Origin": "https://evil.example"})
                check("чужая страница не выключает бота (403)", r.status_code == 403)
                time.sleep(0.4)
                check("сигнал от чужой страницы не ушёл", m_signal.call_count == 1)

                server_mod._api_token = "secret-token"
                for method, path in (("get", "/api/system/status"),
                                     ("post", "/api/system/shutdown"),
                                     ("post", "/api/browser/rescue"),
                                     ("post", "/api/browser/rescue/finish")):
                    r = getattr(client, method)(path)
                    check(f"{method.upper()} {path}: без токена 401", r.status_code == 401)
                r = client.get("/api/system/status",
                               headers={"Authorization": "Bearer secret-token"})
                check("с токеном — 200", r.status_code == 200)
                r = client.get("/api/health")
                check("/api/health открыт и с токеном", r.status_code == 200)
                time.sleep(0.4)
                check("без токена сигнал не ушёл", m_signal.call_count == 1)
    finally:
        server_mod._api_token = orig_token

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

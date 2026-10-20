"""Тест веб-чата без входа (google_anon — Google AI Mode в отдельном
контексте Chrome пула H).

Проверяет:
* куки для файла: без кук входа (SID, *PSID, SIDCC …), без сессионных и
  разделённых, только поля CookieParam;
* pool_h_context: новый контекст получает куки из файла; живой — тот же
  (и чужого процесса — по файлу); после перезапуска Chrome — новый с
  куками; save_context_cookies пишет без кук входа, файл 0600;
* вкладка контекста: createTarget с browserContextId; не в пул V, не в
  видимую вкладку, не в rescue — отказ;
* адаптер: без аккаунта, свежий тред на вызов, пул всегда H; «Войти» на
  странице — не разлогин; капча — долгий карантин без автоклика и rescue;
  просьба войти — карантин без rescue;
* роутер: google_anon — запасной без аккаунта для коротких промптов.

Браузер не нужен: CDP (_raw_call) и страница подменены.

Запуск: PYTHONPATH=. python3 scripts/test_webchat_anon.py
"""

import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    tmp = Path(tempfile.mkdtemp(prefix="webchat_anon_"))
    from app.features import browser_actions as ba
    from app.features import web_llm as wl
    saved = {k: getattr(ba, k) for k in (
        "_raw_call", "_ctx_path", "pool_h_rescue_active", "_pool_h_rescue_path",
        "_WORKER", "_pool_h_mode_stale")}
    ba._ctx_path = lambda key: tmp / "ctx" / f"{key}.json"
    ba._pool_h_rescue_path = lambda: str(tmp / "h.bot-rescue")
    ba._pool_h_mode_stale = lambda: False
    rescue = {"on": False}
    ba.pool_h_rescue_active = lambda: rescue["on"]
    wl._pace_path = lambda: tmp / "pacing.json"
    wl._suspended_path = lambda: tmp / "suspended.json"

    cdp = {"live": set(), "calls": [], "cookies": [], "n": 0}

    def raw_call(method, params=None, session_id=None, pool="v", tab_id=None,
                 timeout=None, _retried=False):
        cdp["calls"].append((method, dict(params or {}), pool))
        if method == "Target.getBrowserContexts":
            return {"browserContextIds": sorted(cdp["live"])}
        if method == "Target.createBrowserContext":
            cdp["n"] += 1
            cid = f"CTX{cdp['n']}"
            cdp["live"].add(cid)
            return {"browserContextId": cid}
        if method == "Storage.setCookies":
            cdp["cookies"] = list(params["cookies"])
            return {}
        if method == "Storage.getCookies":
            return {"cookies": cdp["cookies"]}
        if method == "Target.createTarget":
            return {"targetId": f"T{len(cdp['calls'])}"}
        if method == "Target.attachToTarget":
            return {"sessionId": "S"}
        return {}
    ba._raw_call = raw_call

    def calls(method):
        return [c for c in cdp["calls"] if c[0] == method]

    far = time.time() + 86400 * 30
    google_cookies = [
        {"name": "NID", "value": "n1", "domain": ".google.com", "path": "/",
         "expires": far, "size": 10, "httpOnly": True, "secure": True,
         "session": False, "sameSite": "None", "priority": "Medium",
         "sourceScheme": "Secure", "sourcePort": 443},
        {"name": "AEC", "value": "a1", "domain": ".google.com", "path": "/",
         "expires": far, "session": False},
        {"name": "SID", "value": "secret", "domain": ".google.com",
         "path": "/", "expires": far},
        {"name": "__Secure-1PSID", "value": "secret", "domain": ".google.com",
         "path": "/", "expires": far},
        {"name": "SIDCC", "value": "secret", "domain": ".google.com",
         "path": "/", "expires": far},
        {"name": "tmp", "value": "x", "domain": ".google.com", "path": "/",
         "expires": -1, "session": True},
        {"name": "part", "value": "x", "domain": ".google.com", "path": "/",
         "expires": far, "partitionKey": {"topLevelSite": "https://a.b"}},
    ]
    try:
        # ── 1. Куки для файла ──
        print("куки:")
        clean = ba._ctx_cookies_clean(google_cookies)
        names = [c["name"] for c in clean]
        check(f"остались NID и AEC ({names})", names == ["NID", "AEC"])
        check("только поля CookieParam (size/session — нет)",
              all("size" not in c and "session" not in c for c in clean)
              and clean[0]["sourcePort"] == 443)

        # ── 2. Контекст ──
        print("контекст пула H:")
        path = tmp / "ctx" / "google_anon.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"context": "OLD", "cookies": google_cookies}))
        cid = ba.pool_h_context("google_anon")
        check(f"контекста из файла нет в Chrome — создан новый ({cid})",
              cid == "CTX1" and len(calls("Target.createBrowserContext")) == 1
              and calls("Target.createBrowserContext")[0][1]
              == {"disposeOnDetach": False})
        check("куки из файла — в новый контекст, без кук входа",
              [c["name"] for c in cdp["cookies"]] == ["NID", "AEC"]
              and calls("Storage.setCookies")[0][1]["browserContextId"]
              == "CTX1")
        check("все вызовы — в пул H", all(c[2] == "h" for c in cdp["calls"]))
        check("id контекста записан в файл",
              json.loads(path.read_text())["context"] == "CTX1")
        cdp["calls"].clear()
        check("живой контекст — тот же, без создания",
              ba.pool_h_context("google_anon") == "CTX1"
              and not calls("Target.createBrowserContext"))
        ba._CTX_IDS.clear()
        check("другой процесс (память пуста) — контекст по файлу",
              ba.pool_h_context("google_anon") == "CTX1"
              and not calls("Target.createBrowserContext"))
        cdp["live"].clear()
        cdp["cookies"] = []
        cid = ba.pool_h_context("google_anon")
        check(f"Chrome перезапущен — новый контекст с куками ({cid})",
              cid == "CTX2" and [c["name"] for c in cdp["cookies"]]
              == ["NID", "AEC"])
        cdp["cookies"] = google_cookies + [
            {"name": "NID", "value": "n2", "domain": ".google.com",
             "path": "/", "expires": far}]
        n = ba.save_context_cookies("google_anon")
        st = json.loads(path.read_text())
        check(f"save_context_cookies: без кук входа ({n})",
              n == 3 and not any("SID" in c["name"] for c in st["cookies"])
              and "secret" not in path.read_text())
        check("файл кук — только владельцу (0600)",
              stat.S_IMODE(os.stat(path).st_mode) == 0o600)
        check("неизвестный контекст — -1",
              ba.save_context_cookies("nosuch") == -1)

        # ── 3. Вкладка контекста ──
        print("вкладка:")
        cdp["calls"].clear()
        tab = ba.open_new_tab("https://www.google.com/search?udm=50",
                              background=True, pool="h",
                              context="google_anon")
        ct = calls("Target.createTarget")
        check("createTarget — в контексте, пул H",
              ct and ct[0][1].get("browserContextId") == "CTX2"
              and ct[0][2] == "h" and tab in ba._RAW_TABS)
        ba._raw_forget(tab)
        try:
            ba._raw_open("https://x", pool="v", context="google_anon")
            refused = False
        except ba.BrowserUnavailable:
            refused = True
        check("контекст в пуле V — отказ", refused)
        rescue["on"] = True
        try:
            ba.open_new_tab("https://x", background=True, pool="h",
                            context="google_anon")
            refused = False
        except ba.BrowserUnavailable:
            refused = True
        check("rescue (пул H видимый) — отказ, окна на экране нет", refused)
        rescue["on"] = False

        class Worker:
            used = False

            def submit(self, *a, **k):
                Worker.used = True
                return 99
        ba._WORKER = Worker()
        ba._raw_call = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("chrome down"))
        try:
            ba.open_new_tab("https://x", background=True, pool="h",
                            context="google_anon")
            refused = False
        except ba.BrowserUnavailable:
            refused = True
        check("пул H не поднялся — отказ, а не вкладка в профиле со входом",
              refused and not Worker.used)
        ba._raw_call = raw_call

        # ── 4. Адаптер ──
        print("адаптер:")
        ad = wl.ADAPTERS["google_anon"]
        check("без аккаунта, без входа, свой контекст, картинок нет",
              not wl.site_has_account("google_anon") and ad["signed_out"]
              and ad["browser_context"] == "google_anon"
              and ad["images"] is False and ad["host"] == "www.google.com")
        chat = wl.WebChatLLM("google_anon", context="t_anon", channel="cc",
                             browser_pool="v")
        check("пул — всегда H (в V профиль со входом)",
              chat.browser_pool == "h")
        check("канал cc — свежий тред на вызов, как у google", chat.stateless)
        opened = []
        fake_ba = type("FakeBA", (), {"open_new_tab": staticmethod(
            lambda url, **kw: opened.append(kw) or 7)})
        chat._open_tab(fake_ba, "https://www.google.com/search?udm=50")
        g_side = wl.WebChatLLM("google", context="t_anon", channel="side")
        g_side._open_tab(fake_ba, "https://www.google.com/search?udm=50")
        check(f"вкладка google_anon — с контекстом, google — без ({opened})",
              opened[0].get("context") == "google_anon"
              and "context" not in opened[1])
        check("и постоянный канал (side) — без адреса треда: адрес с q= "
              "задал бы прошлый вопрос заново",
              wl.WebChatLLM("google_anon", context="t_anon",
                            channel="side").stateless
              and not wl.WebChatLLM("google", context="t_anon",
                                    channel="side").stateless)

        class Page:
            def __init__(self, data):
                self.data = data

            def eval_js(self, host, tab_id, js):
                return json.dumps(self.data)
        st_anon = chat._login_state(Page({"btn": True,
                                          "url": "https://www.google.com/search"}),
                                    1)
        g = wl.WebChatLLM("google", context="t_anon", channel="cc")
        st_g = g._login_state(Page({"btn": True,
                                    "url": "https://www.google.com/search"}), 1)
        check(f"«Войти» на странице: google_anon — не разлогин ({st_anon[0]}), "
              f"google — разлогин ({st_g[0]})",
              st_anon[0] != "login" and st_g[0] == "login")
        check("google_anon: поле пароля — всё же страница входа",
              chat._login_state(Page({"pwd": True}), 1)[0] == "login")

        class Captcha:
            clicked = False

            def detect_antibot(self, *a, **k):
                return "recaptcha"

            def try_challenge_autoclick(self, *a, **k):
                Captcha.clicked = True
                return False
        chat._suspension_check = lambda ba_, tab: False
        got = chat._challenge_check(Captcha(), 1)
        q = wl._SITE_QUARANTINE.get("google_anon") or {}
        check(f"капча: карантин ratelimit на 6 ч, без автоклика ({q.get('kind')})",
              got is True and q.get("kind") == "ratelimit"
              and q.get("until", 0) - time.time() > 5 * 3600
              and not Captcha.clicked)
        check("капча google_anon не ждёт rescue",
              "google_anon" not in wl._rescue_pending_sites())
        wl.clear_quarantine("google_anon")
        chat._on_logged_out("страница входа: адрес /signin")
        check("просьба войти: карантин без rescue (refused)",
              wl.quarantine_kind("google_anon") == "refused")
        wl.clear_quarantine("google_anon")

        # ── 5. Роутер ──
        print("роутер:")
        from app.core import router as rt
        from app.core.pace_scope import automated_calls
        r = rt.ModelRouter.__new__(rt.ModelRouter)
        r.context = "t_anon"
        r.available = {}
        r.active_provider = r.pinned_provider = "webchat:deepseek"
        r.fallback_order = None
        r.excluded = set()
        r.model_overrides = {}
        r.webchat_sites = ["deepseek", "qwen"]
        r.webchat_limits = r.webchat_modes = {}
        now = time.time()
        (tmp / "pacing.json").write_text(json.dumps(
            {"qwen": {"last": now - 9000}, "duckai": {"last": now - 60}}))
        with automated_calls("task:x"):
            short = r._auto_candidates([{"role": "user", "content": "x" * 500}],
                                       {"deepseek"})
            long_ = r._auto_candidates([{"role": "user", "content": "x" * 9000}],
                                       {"deepseek"})
        check(f"короткий промпт: без аккаунта первыми — google_anon (ещё не "
              f"было) и duck.ai, потом qwen ({short})",
              short == ["google_anon", "duckai", "qwen"])
        check(f"длинный (больше поля AI Mode) — google_anon мимо ({long_})",
              long_ == ["duckai", "qwen"])
    finally:
        for k, v in saved.items():
            setattr(ba, k, v)

    print(f"\nИтого: {ok - failures}/{ok} OK, {failures} FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

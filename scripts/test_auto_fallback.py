"""Тест запасного веб-чата автоматики (router._auto_fallback) и потолков.

Проверяет:
* site_has_account / pace_cap: duck.ai — без аккаунта (общий потолок
  WEBCHAT_AUTO_PER_HOUR), остальные — ниже (WEBCHAT_AUTO_PER_HOUR_ACCOUNT);
  0 снимает потолки; pace_reserve упирается в потолок сайта с аккаунтом;
* назначенный провайдер автоматики (cc_provider) не ответил — не обычная
  цепочка (основной deepseek первым), а запасные веб-чаты: сначала без
  аккаунта, потом кто дольше без отправок; мимо карантина, потолка,
  исключённых персоной и google с промптом длиннее поля;
* задача держится за выбранным сайтом (ключ области), пока он отвечает;
  отказал — снова по отдыху; у другой задачи — свой выбор;
* без cc_provider (основной веб-чат персоны): сбой основного — тоже
  запасные, duck.ai — без записи в списке персоны;
* все веб-чаты молчат — API с ключами, локальной модели нет; никого — None;
* вызов человека (вне области автоматики) — обычная цепочка как раньше;
* агент задач: модели не ответили — пауза с понятным текстом, без повтора
  промпта; вызовы помечены ключом прогона.

Браузер не нужен: _try_webchat, _call_with_keys и _try_local подменены.

Запуск: PYTHONPATH=. python3 scripts/test_auto_fallback.py
"""

import json
import os
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

    tmp = Path(tempfile.mkdtemp(prefix="auto_fallback_"))
    from app.features import web_llm as wl
    from app.core import router as rt
    from app.core.pace_scope import automated_calls, automation_key, \
        is_automated
    wl._pace_path = lambda: tmp / "pacing.json"
    wl._suspended_path = lambda: tmp / "suspended.json"
    # Карантин публикуется файлом рядом с профилем пула H — во временный
    # каталог, не к профилю живого бота
    from app.features import browser_actions as ba
    ba._pool_h_rescue_path = lambda: str(tmp / "h.bot-rescue")
    ba.pool_h_rescue_active = lambda: False
    env_keys = ("WEBCHAT_AUTO_GAP_SEC", "WEBCHAT_AUTO_JITTER_SEC",
                "WEBCHAT_AUTO_PER_HOUR", "WEBCHAT_AUTO_PER_HOUR_ACCOUNT")
    env_old = {k: os.environ.get(k) for k in env_keys}
    for k in env_keys:
        os.environ.pop(k, None)

    def write_pacing(st):
        (tmp / "pacing.json").write_text(json.dumps(st))

    try:
        # ── 1. Аккаунт сайта и потолки ──
        print("потолки:")
        check("duck.ai — без аккаунта", not wl.site_has_account("duckai"))
        check("deepseek, google, неизвестный сайт — с аккаунтом",
              wl.site_has_account("deepseek") and wl.site_has_account("google")
              and wl.site_has_account("nosuchsite"))
        check(f"потолок: с аккаунтом {wl.AUTO_PER_HOUR_ACCOUNT}, без — "
              f"{wl.AUTO_PER_HOUR}",
              wl.pace_cap("deepseek") == wl.AUTO_PER_HOUR_ACCOUNT
              and wl.pace_cap("duckai") == wl.AUTO_PER_HOUR)
        os.environ["WEBCHAT_AUTO_PER_HOUR"] = "10"
        check("общий потолок ниже — он и для сайтов с аккаунтом",
              wl.pace_cap("deepseek") == 10 and wl.pace_cap("duckai") == 10)
        os.environ["WEBCHAT_AUTO_PER_HOUR"] = "0"
        check("WEBCHAT_AUTO_PER_HOUR=0 — без потолков совсем",
              wl.pace_cap("deepseek") == 0 and wl.pace_cap("duckai") == 0)
        os.environ.pop("WEBCHAT_AUTO_PER_HOUR")
        os.environ["WEBCHAT_AUTO_PER_HOUR_ACCOUNT"] = "0"
        check("WEBCHAT_AUTO_PER_HOUR_ACCOUNT=0 — у сайтов с аккаунтом общий",
              wl.pace_cap("deepseek") == wl.AUTO_PER_HOUR)
        os.environ["WEBCHAT_AUTO_PER_HOUR_ACCOUNT"] = "3"
        os.environ["WEBCHAT_AUTO_GAP_SEC"] = "0"
        os.environ["WEBCHAT_AUTO_JITTER_SEC"] = "0"
        with automated_calls("t"):
            got = [wl.pace_reserve("deepseek", "cc") for _ in range(4)]
            duck = [wl.pace_reserve("duckai", "cc") for _ in range(4)]
        check(f"pace_reserve: 4-я отправка в deepseek сверх потолка 3 → None "
              f"({got})", got[:3] == [0.0, 0.0, 0.0] and got[3] is None)
        check("duck.ai те же 4 отправки пропускает", None not in duck)
        snap = wl.pace_snapshot()
        check("pace_snapshot: счёт за час и время последней отправки",
              snap["deepseek"]["auto_hour"] == 3
              and snap["deepseek"]["last"] > time.time() - 60)
        check("pace_room: deepseek без места, duck.ai — с местом",
              not wl.pace_room("deepseek") and wl.pace_room("duckai"))
        check("человек (main) в потолок не упирается",
              wl.pace_reserve("deepseek", "main") == 0.0)
        os.environ.pop("WEBCHAT_AUTO_PER_HOUR_ACCOUNT")

        # ── 2. Роутер: запасные веб-чаты ──
        print("запасные веб-чаты:")

        def stub_router(sites, primary="webchat:deepseek", cc=None,
                        excluded=(), answers=None, api=None):
            r = rt.ModelRouter.__new__(rt.ModelRouter)
            r.context = "test_auto"
            r.available = dict(api or {})
            r.active_provider = primary
            r.pinned_provider = primary
            r.fallback_order = None
            r.excluded = set(excluded)
            r.model_overrides = {p: "m" for p in r.available}
            r.webchat_sites = list(sites)
            r._webchats = {}
            r.webchat_limits = {}
            r.webchat_modes = {}
            r._last_key_index = {}
            r._provider_sems = {}
            r.answer_provider = r.vision_provider = None
            r.cc_provider = cc
            r.calls = []
            answers = dict(answers or {})

            def try_webchat(messages, temperature, max_tokens, top_p,
                            timeout, sites=None, channel="main", **kw):
                for s in sites:
                    r.calls.append(s)
                    if answers.get(s):
                        r._last_provider = f"webchat:{s}"
                        return answers[s]
                return None

            def call_keys(provider, cfg, messages, *a, **kw):
                r.calls.append(f"api:{provider}")
                return answers.get(f"api:{provider}")

            def local(*a, **kw):
                r.calls.append("local")
                return answers.get("local")
            r._try_webchat = try_webchat
            r._call_with_keys = call_keys
            r._try_local = local
            r.answers = answers
            return r

        msgs = [{"role": "user", "content": "x" * 10000}]
        short = [{"role": "user", "content": "x" * 500}]
        now = time.time()
        write_pacing({"deepseek": {"last": now - 60},
                      "qwen": {"last": now - 7200},
                      "zai": {"last": now - 600},
                      "google": {"last": now - 9000}})
        rt.internet_available = lambda: True
        sites = ["qwen", "deepseek", "zai", "kimi", "google"]

        r = stub_router(sites, cc="webchat:duckai", excluded={"webchat:kimi"},
                        answers={"local": "LOCAL"})
        with automated_calls("task:a"):
            got = r.get_response(msgs, webchat_channel="cc",
                                 force_provider="webchat:duckai")
        check(f"все молчат: duck.ai → qwen (2 ч) → zai (10 мин) → deepseek "
              f"(1 мин), kimi исключён, google и google_anon — промпт длиннее "
              f"поля; локальной нет ({r.calls})",
              r.calls == ["duckai", "qwen", "zai", "deepseek"] and got is None)

        r = stub_router(sites, cc="webchat:duckai", excluded={"webchat:kimi"})
        with automated_calls("task:a"):
            r.get_response(short, webchat_channel="cc",
                           force_provider="webchat:duckai")
        check(f"короткий промпт — google_anon (без аккаунта), потом google "
              f"(отдыхал дольше всех) ({r.calls})",
              r.calls[:3] == ["duckai", "google_anon", "google"])

        wl.quarantine_site("qwen", "test", ttl=600, kind="ratelimit",
                           alert=False)
        write_pacing({"deepseek": {"last": now - 60},
                      "qwen": {"last": now - 7200},
                      "zai": {"last": now - 600,
                              "auto": [now - 30] * 20}})
        r = stub_router(sites, cc="webchat:duckai", excluded={"webchat:kimi"},
                        answers={"deepseek": "DS"})
        with automated_calls("task:b"):
            got = r.get_response(msgs, webchat_channel="cc",
                                 force_provider="webchat:duckai")
        check(f"карантин (qwen) и полный потолок (zai) — мимо ({r.calls})",
              r.calls == ["duckai", "deepseek"] and got == "DS"
              and r._last_provider == "webchat:deepseek")
        wl.clear_quarantine("qwen")

        # Задача держится за выбранным сайтом
        write_pacing({"deepseek": {"last": now - 60},
                      "qwen": {"last": now - 7200},
                      "zai": {"last": now - 600}})
        r = stub_router(sites, cc="webchat:duckai", excluded={"webchat:kimi"},
                        answers={"qwen": "Q", "zai": "Z", "deepseek": "D"})
        with automated_calls("task:c"):
            first = r.get_response(msgs, webchat_channel="cc",
                                   force_provider="webchat:duckai")
        write_pacing({"deepseek": {"last": now - 60},
                      "qwen": {"last": now},
                      "zai": {"last": now - 600}})
        r.calls.clear()
        with automated_calls("task:c"):
            second = r.get_response(msgs, webchat_channel="cc",
                                    force_provider="webchat:duckai")
        check(f"задача держится за qwen, хотя он теперь «свежий» ({r.calls})",
              first == "Q" and second == "Q"
              and r.calls == ["duckai", "qwen"])
        r.calls.clear()
        with automated_calls("task:d"):
            other = r.get_response(msgs, webchat_channel="cc",
                                   force_provider="webchat:duckai")
        check(f"другая задача — по отдыху: zai ({r.calls})",
              other == "Z" and r.calls == ["duckai", "zai"])
        r.answers["qwen"] = None
        r.calls.clear()
        with automated_calls("task:c"):
            third = r.get_response(msgs, webchat_channel="cc",
                                   force_provider="webchat:duckai")
        check(f"qwen отказал — задача берёт следующий по отдыху ({r.calls})",
              third == "Z" and r.calls == ["duckai", "qwen", "zai"])
        r.calls.clear()
        with automated_calls("task:c"):
            r.get_response(msgs, webchat_channel="cc",
                           force_provider="webchat:duckai")
        check(f"дальше держится за zai ({r.calls})",
              r.calls == ["duckai", "zai"])

        # Без cc_provider (основной веб-чат персоны)
        write_pacing({"deepseek": {"last": now - 60},
                      "qwen": {"last": now - 7200}})
        r = stub_router(["deepseek", "qwen", "google"],
                        answers={"duckai": "DUCK"})
        with automated_calls("task:e"):
            got = r.get_response(msgs, webchat_channel="cc")
        check(f"без cc_provider: основной deepseek → duck.ai (без аккаунта, "
              f"не в списке персоны) ({r.calls})",
              r.calls == ["deepseek", "duckai"] and got == "DUCK")
        r = stub_router(["deepseek", "qwen"], excluded={"webchat:duckai"},
                        answers={"qwen": "Q"})
        with automated_calls("task:f"):
            r.get_response(msgs, webchat_channel="cc")
        check(f"duck.ai исключён персоной — не берётся ({r.calls})",
              r.calls == ["deepseek", "qwen"])

        # Видимый Chrome (пул V) — не запасной автоматики
        write_pacing({"deepseek": {"last": now - 60}})
        r = stub_router(["deepseek", "chatgpt", "claude", "qwen"],
                        answers={"qwen": "Q"})
        r.webchat_modes = {"qwen": "headed"}
        with automated_calls("task:v"):
            r.get_response(msgs, webchat_channel="cc")
        check(f"chatgpt/claude (пул V по умолчанию) и qwen в headed — мимо, "
              f"хотя «отдыхали» дольше всех ({r.calls})",
              r.calls == ["deepseek", "duckai"])
        r.webchat_modes = {"chatgpt": "headless"}
        r.calls.clear()
        with automated_calls("task:v2"):
            r.get_response(msgs, webchat_channel="cc")
        check(f"chatgpt, переведённый в headless, — берётся ({r.calls})",
              "chatgpt" in r.calls and "claude" not in r.calls)

        # API после веб-чатов, локальной нет
        api = {"groq": {"api_keys": ["k"]}}
        r = stub_router(["deepseek"], cc="webchat:duckai", api=api,
                        answers={"api:groq": "G", "local": "L"})
        with automated_calls("task:g"):
            got = r.get_response(msgs, webchat_channel="cc",
                                 force_provider="webchat:duckai")
        check(f"веб-чаты молчат → API с ключом, локальная не зовётся "
              f"({r.calls})",
              got == "G" and r.calls[-1] == "api:groq"
              and "local" not in r.calls)

        # Вне автоматики — как раньше
        r = stub_router(["deepseek", "qwen"], cc="webchat:duckai",
                        answers={"deepseek": "MAIN"})
        got = r.get_response(msgs, webchat_channel="cc",
                             force_provider="webchat:duckai")
        check(f"команда человека: назначенный молчит → обычная цепочка "
              f"(основной deepseek) ({r.calls})",
              got == "MAIN" and r.calls == ["duckai", "deepseek"])
        check("вне области — не автоматика, ключа нет",
              not is_automated() and automation_key() is None)
        with automated_calls():
            check("область без ключа — автоматика без ключа",
                  is_automated() and automation_key() is None)

        # ── 3. Агент задач ──
        print("агент задач:")
        from app.features.task_agent import TaskAgent

        class SilentRouter:
            def __init__(self):
                self.calls = 0
                self.keys = []

            def get_response(self, messages, **kw):
                self.calls += 1
                self.keys.append(automation_key())
                return None

        class CC:
            def _snapshot_for(self, *a, **k):
                return None, None, None, None, "в браузере ничего не открыто"

        agent = TaskAgent(CC(), memory_path=tmp / "tm.json")
        sr = SilentRouter()
        agent.slot_router = None
        reply = agent.start("c1", "найди расписание", sr)
        check(f"модели молчат: один вызов шага, без повтора ({sr.calls})",
              sr.calls == 1)
        check("пауза с понятным текстом", "Модели сейчас не отвечают"
              in reply and agent.awaiting_kind("c1") == "continue")
        check(f"вызов помечен ключом прогона ({sr.keys})",
              sr.keys and str(sr.keys[0]).startswith("task:")
              and len(set(sr.keys)) == 1)
    finally:
        for k, v in env_old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print(f"\nИтого: {ok - failures}/{ok} OK, {failures} FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Смок-тест приватности режима управления (app/features/cc_privacy.py и
его хуки в computer_control / scenario_manager / browser_actions /
scripts/scrub_cc_audit.py).

Проверяет: маску ввода в чувствительные поля и секретоподобных значений,
чистку URL, приватные страницы (PrivateRouter, vision без скриншота, текст
секции не уходит в LLM, URL в system prompt), запись аудита (маски, поля
наблюдаемости, ротация, хвост), лок исполнения, слоты секретов в
сценариях, отсутствие значений полей ввода в снапшот-JS и скрипт чистки.
Браузер, LLM и сеть подменены — живой Chrome/интернет не нужны.

Запуск: python -m scripts.test_cc_privacy
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="cc_privacy_"))
    os.environ["VPC_DATA_DIR"] = str(tmp / "data")
    ok = 0
    fails = []

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        if cond:
            ok += 1
        else:
            fails.append(name)

    from app.features import cc_privacy as P

    # ── 1. Маски и чувствительные поля ──
    print("cc_privacy: маски")
    check("mask: длина без содержимого", P.mask("hunter2") == "***(7)")
    check("mask идемпотентна", P.mask(P.mask("hunter2")) == "***(7)")
    for label in ("Пароль", "Password", "Email", "E-mail", "Телефон",
                  "Код из СМС", "Номер карты", "CVV", "Логин", "PIN",
                  "one-time code", "Имя пользователя"):
        check(f"чувствительная подпись: {label}", P.is_sensitive_label(label))
    for label in ("Поиск", "Search", "Найти товар", "Кодекс", "Hotel search",
                  "Поиск на Google Картах", "Введите комментарий", ""):
        check(f"обычная подпись: {label!r}", not P.is_sensitive_label(label))
    check("секрет: email", P.looks_secret("a.b@example.com"))
    check("секрет: телефон", P.looks_secret("+7 (999) 123-45-67"))
    check("секрет: номер карты", P.looks_secret("4111 1111 1111 1111"))
    check("секрет: длинный токен", P.looks_secret("ZXhhbXBsZVRva2VuMTIzNDU2Nzg5MA"))
    check("не секрет: слово/запрос", not P.looks_secret("пицца пепперони")
          and not P.looks_secret("hunter2") and not P.looks_secret("2024"))
    check("не секрет: слаг товара",
          not P.looks_secret("iphone-15-pro-max-256gb-black-edition"))
    check("redact_typed: флаг поля маскирует любой текст",
          P.redact_typed("pizza", "Поле", flagged=True) == "***(5)")
    check("redact_typed: подпись «Пароль» маскирует",
          P.redact_typed("qwerty", "Пароль") == "***(6)")
    check("redact_typed: поиск — как есть",
          P.redact_typed("кот в сапогах", "Поиск") == "кот в сапогах")
    ri = P.redact_inline("почта a@b.ru, карта 4111111111111111, ключ "
                         "sk9AbC123dEf456GhI789jKl012 и https://x.com/a?token=zz#t")
    check("redact_inline: email/карта/токен замаскированы",
          "a@b.ru" not in ri and "4111111111111111" not in ri
          and "sk9AbC123dEf456GhI789jKl012" not in ri)
    check("redact_inline: URL в тексте почищен", "token=" not in ri
          and "#t" not in ri and "https://x.com/a" in ri)
    check("redact_inline: обычный текст не тронут",
          P.redact_inline("Нажать «Корзина» на dodopizza.ru") ==
          "Нажать «Корзина» на dodopizza.ru")
    check("redact_inline: limit режет", len(P.redact_inline("я" * 500, 50)) <= 51)

    # ── 2. URL ──
    print("cc_privacy: URL")
    u = P.scrub_url("https://user:pw@www.youtube.com/watch?v=dQw4w9WgXcQ"
                    "&list=PL1&t=42&access_token=abc#frag")
    check("URL: youtube v/list/t сохранены",
          "v=dQw4w9WgXcQ" in u and "list=PL1" in u and "t=42" in u)
    check("URL: access_token, фрагмент и userinfo убраны",
          "access_token" not in u and "#" not in u and "pw@" not in u)
    u2 = P.scrub_url("https://acc.example.com/cb?code=777&state=xyz&session_id=1"
                     "&sessionId=2&sig=a&email=me%40x.ru&phone=79991234567"
                     "&page=2&accessToken=q&authuser=0&otp=1&ticket=t")
    check("URL: секретные параметры убраны (code/state/session/sig/email/…)",
          all(k not in u2 for k in ("code=", "state=", "session", "sig=",
                                    "email=", "phone=", "ccessToken",
                                    "authuser", "otp=", "ticket=")))
    check("URL: безвредный page сохранён", "page=2" in u2)
    u3 = P.scrub_url("https://site.com/search?q=me@mail.ru&yclid=ABCdef1234567890xyz12345")
    check("URL: ПДн в обычном параметре — маской, трекинг-id — как есть",
          "me@mail.ru" not in u3 and "q=***(" in u3
          and "yclid=ABCdef1234567890xyz12345" in u3)
    u4 = P.scrub_url("https://site.com/reset/a1B2c3D4e5F6g7H8?x=1")
    check("URL: токен после /reset/ замаскирован", "a1B2c3D4e5F6g7H8" not in u4
          and "/reset/***(" in u4)
    u5 = P.scrub_url("https://shop.ru/product/iphone-15-pro-max-256gb/"
                     "UCxYz1234567890abcdefghi")
    check("URL: слаг и id канала/товара не тронуты",
          u5 == "https://shop.ru/product/iphone-15-pro-max-256gb/"
                "UCxYz1234567890abcdefghi")
    check("URL: не-URL как есть", P.scrub_url("просто текст") == "просто текст")
    check("URL: идемпотентно", P.scrub_url(u2) == u2)
    u6 = P.scrub_url("https://site.ru/cb?PHPSESSID=abcdef123456&hmac=f00"
                     "&verification=v1&magic_link=m1&reset_key=r1&sort=new")
    check("URL: PHPSESSID/hmac/verification/magic/reset убраны",
          all(x not in u6 for x in ("PHPSESSID", "hmac", "verification",
                                    "magic", "reset", "abcdef123456"))
          and "sort=new" in u6)
    u7 = P.scrub_url("https://a.com/x?p=MySecretPass")
    check("URL: p=<не число> — маской", "MySecretPass" not in u7
          and "p=***(12)" in u7)
    check("URL: p=<число> (страница) — как есть",
          P.scrub_url("https://a.com/x?p=2") == "https://a.com/x?p=2")
    u8 = P.scrub_url("https://a.com/x?s=abcDEF123&lk=XYZ987&v=dQw4&t=42")
    check("URL: s=/lk= <не число> — маской, v/t как есть",
          "abcDEF123" not in u8 and "XYZ987" not in u8
          and "s=***(9)" in u8 and "lk=***(6)" in u8
          and "v=dQw4" in u8 and "t=42" in u8)
    check("URL: s=<число> (метка шаринга) — как есть",
          P.scrub_url("https://x.com/a/status/1?s=20") == "https://x.com/a/status/1?s=20")
    u9 = P.scrub_url("https://mail.google.com/mail/?gdsid=ABCDEF123456&ssid=q"
                     "&usid=1&sidebar=1&side=l&q=котики")
    check("URL: *sid* (gdsid/ssid/usid) убран, sidebar/side/q сохранены",
          all(x not in u9 for x in ("gdsid", "ssid", "usid", "ABCDEF123456"))
          and "sidebar=1" in u9 and "side=l" in u9 and "q=" in u9)
    check("URL: s/lk/gdsid — идемпотентно",
          P.scrub_url(u8) == u8 and P.scrub_url(u9) == u9)
    check("URL: токен после короткого /r/ замаскирован",
          P.scrub_url("https://a.com/r/ZXhhbXBsZTEyMzQ1Njc4OTA")
          == "https://a.com/r/***(23)")
    u8 = P.scrub_url("https://x.com/files/9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b/view")
    check("URL: длинный токен-сегмент без «секретного» префикса — маской",
          "9f86d081884c7d659a2feaa0c55ad015" not in u8 and u8.endswith("/view"))
    check("URL: /r/<имя сабреддита> и id канала не тронуты",
          P.scrub_url("https://reddit.com/r/Python") == "https://reddit.com/r/Python"
          and P.scrub_url("https://www.youtube.com/channel/UCxYz1234567890abcdefghi")
          == "https://www.youtube.com/channel/UCxYz1234567890abcdefghi")
    check("URL: новые правила идемпотентны",
          all(P.scrub_url(x) == x for x in (u6, u7, u8)))
    # Вложенные адреса и токены под «безобидным» именем (redteam-1)
    nested = {
        "https://shop.test/welcome?return_to=https%3A%2F%2Fshop.test%2F"
        "confirm%3Ftoken%3DZx9Qm2Lk7Pw4Rt8Vn3": "Zx9Qm2Lk7Pw4Rt8Vn3",
        "https://example.com/login?next=%2Freset%3Ftoken%3Dsecr3tT0kenValue":
            "secr3tT0kenValue",
        "https://example.com/cb?redirect_uri=https%3A%2F%2Fapp.example.com"
        "%2Fcb%3Fcode%3D4%2F0AX4XfWh": "0AX4XfWh",
        "https://accounts.google.com/signin?continue=https%3A%2F%2Fmail."
        "google.com%2Fmail%2F%3Ftoken%3DAbC123xyz987": "AbC123xyz987",
        "https://a.test/x?u=https%3A%2F%2Fb.test%2F%3Fnext%3Dhttps%253A%252F"
        "%252Fc.test%252F%253Ftoken%253DDeep123Tok9": "Deep123Tok9",
    }
    for src, tok in nested.items():
        out = P.scrub_url(src)
        check(f"URL: вложенный адрес чищен рекурсивно ({tok[:8]}…)",
              tok not in out and P.scrub_url(out) == out)
    jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJpdmFuQG1haWwucnUifQ."
           "Qm2Lk7Pw4Rt8Vn3Zx9Qm2Lk7")
    for src, tok in ((f"https://docs.test/view?data={jwt}", jwt[:20]),
                     ("https://m.test/room?k=AbCdEfGhIjKlMnOp1234", "AbCdEfGh"),
                     ("https://e.test/confirm?c=Kotik2019", "Kotik2019"),
                     ("https://e.test/share?h=f3a9c1d2e4b5a6978877", "f3a9c1"),
                     ("https://e.test/?blob=Zx9Qm2Lk7Pw4Rt8Vn3Zx9Qm2Lk7Pw4",
                      "Zx9Qm2Lk7Pw4"),
                     ("https://t.me/+AbCdEfGh12345678", "AbCdEfGh1234"),
                     ("https://e.test/users/john%40gmail.com/settings", "john")):
        out = P.scrub_url(src)
        check(f"URL: JWT/токен/ключ ссылки — маской ({src[8:30]}…)",
              tok not in out and P.scrub_url(out) == out)
    check("URL: поиск, числовые id, трекинг и /r/<слово> как есть",
          P.scrub_url("https://s.test/find?q=iphone 15 pro&id=123456&sort=new")
          == "https://s.test/find?q=iphone+15+pro&id=123456&sort=new"
          and "gclid=Cj0KCQjw1234567890abcdefGHIJ" in P.scrub_url(
              "https://s.test/?gclid=Cj0KCQjw1234567890abcdefGHIJ")
          and "variation=11ee7d61a1b2c3d4e5f60718293a4b5c" in P.scrub_url(
              "https://dodopizza.ru/moscow/product/x?"
              "variation=11ee7d61a1b2c3d4e5f60718293a4b5c"))
    check("looks_secret: ключи с префиксом (ghp_/sk-proj-/xoxb-) — токен",
          all(P.looks_secret(x) for x in (
              "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
              "sk-proj-AbCdEf1234567890GhIj", "xoxb-1234567890-AbCdEfGhIj")))

    # ── 3. Приватные страницы ──
    print("cc_privacy: приватные страницы")
    for h in ("online.sberbank.ru", "id.vk.com", "accounts.google.com",
              "pay.yandex.ru", "checkout.stripe.com", "www.gosuslugi.ru",
              "https://example.com/login", "https://shop.ru/checkout/step1",
              "passport.yandex.ru", "https://www.tinkoff.ru/auth/"):
        check(f"приватная: {h}", P.is_private_page(h))
    for h in ("youtube.com", "dodopizza.ru", "https://example.com/catalog",
              "https://www.youtube.com/watch?v=1", ""):
        check(f"обычная: {h!r}", not P.is_private_page(h))
    # Почта/мессенджеры/корзины/токены — приватные
    for h in ("mail.google.com", "e.mail.ru", "outlook.live.com",
              "outlook.office.com", "web.telegram.org", "web.whatsapp.com",
              "https://vk.com/im?sel=1", "www.tinkoff.ru", "https://ozon.ru/cart",
              "https://wildberries.ru/lk/basket", "https://github.com/settings/tokens",
              "mail.yandex.ru", "sso.corp.com", "https://www.paypal.com/",
              "https://site.ru/login.php", "https://site.ru/oauth/authorize",
              "https://www.messenger.com/t/1"):
        check(f"приватная (почта/чат/корзина): {h}", P.is_private_page(h))
    # Ложные срабатывания старой подстрочной эвристики — обычные сайты
    for h in ("https://litres.ru/author/ivan", "https://goodreads.com/author/show/1",
              "https://medium.com/authors", "author.today", "espresso.ru",
              "lasso.io", "upay.ru", "mail.ru", "tokopedia.co.id",
              "https://vk.com/feed", "https://example.com/authors/x"):
        check(f"обычная (не подстрока): {h}", not P.is_private_page(h))
    check("private_hosts из конфига: домен и поддомен",
          P.is_private_page("mail.corp.example", ["corp.example"])
          and P.is_private_page("corp.example", ["corp.example"]))
    check("builtin выключен — только явный список",
          not P.is_private_page("online.sberbank.ru", [], builtin=False))
    # Маршрут во фрагменте/query и кабинет/ключи API (redteam-1)
    for h in ("https://shop.test/#/checkout", "https://shop.test/#!/signin",
              "https://shop.test/index.php?route=checkout/checkout",
              "https://shop.test/?page=login", "https://shop.test/?r=site/login",
              "shop.test/#/account/orders", "https://shop.test/my-account/",
              "https://shop.test/account", "https://shop.test/profile/edit",
              "https://shop.test/personal/", "https://platform.openai.com/api-keys",
              "https://dashboard.stripe.com/apikeys", "https://shop.test/gocheckout"):
        check(f"приватная (маршрут/кабинет): {h}", P.is_private_page(h))
    for h in ("https://shop.test/#reviews", "https://shop.test/?page=2",
              "https://shop.test/catalog?route=product/category",
              "https://blog.test/?next=/login"):
        check(f"обычная (маршрут без входа/оплаты): {h}",
              not P.is_private_page(h))

    # PrivateRouter
    class SpyRouter:
        cc_provider = "deepseek"
        vision_provider = "gemini"

        def __init__(self):
            self.calls = []

        def get_response(self, messages, **kw):
            self.calls.append(("text", messages))
            return "1"

        def get_response_with_image(self, *a, **kw):
            self.calls.append(("image", a))
            return "1"

        def supports_vision(self):
            return True

    import app.core.local_router as _lr
    orig_glr = _lr.get_local_router

    class _FakeLocalBase:
        def __init__(self, backend):
            self.backend = backend

        def _resolve_task(self, task, persona=None):
            return self.backend, []

    class _FakeLocal:
        def __init__(self, backend="ollama", available=True):
            self._base = _FakeLocalBase(backend)
            self.available = available
            self.calls = 0

        def is_available(self, task=None):
            return self.available

        def get_response(self, messages, **kw):
            self.calls += 1
            return "2"

    spy = SpyRouter()
    pr = P.private_router(spy, "online.sberbank.ru")
    check("private_router: приватный хост → PrivateRouter",
          isinstance(pr, P.PrivateRouter))
    check("private_router: обычный хост → исходный роутер",
          P.private_router(spy, "youtube.com") is spy)
    check("PrivateRouter: vision выключен, провайдеры сняты",
          pr.supports_vision() is False and pr.get_response_with_image(b"x") is None
          and pr.cc_provider is None and pr.vision_provider is None)
    try:
        _lr.get_local_router = lambda context=None: _FakeLocal(available=False)
        check("PrivateRouter: локальной нет → None, облако не зовётся",
              pr.get_response([{"role": "user", "content": "x"}]) is None
              and spy.calls == [])
        loc = _FakeLocal()
        _lr.get_local_router = lambda context=None: loc
        check("PrivateRouter: локальная Ollama отвечает",
              pr.get_response([{"role": "user", "content": "x"}]) == "2"
              and loc.calls == 1 and spy.calls == [])
        wc = _FakeLocal(backend="webchat")
        _lr.get_local_router = lambda context=None: wc
        check("PrivateRouter: веб-чат-движок локального роутера не годится",
              pr.get_response([{"role": "user", "content": "x"}]) is None
              and wc.calls == 0)
    finally:
        _lr.get_local_router = orig_glr

    # ── 4. Запись аудита ──
    print("computer_control: аудит")
    from app.features.computer_control import ComputerControlManager

    class Spy(ComputerControlManager):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.routers = []
            self.active = 0
            self.overlap = False
            self.sleep = 0.0

        def _dispatch(self, action, router=None):
            self.active += 1
            if self.active > 1:
                self.overlap = True
            self.routers.append(router)
            if self.sleep:
                time.sleep(self.sleep)
            self.active -= 1

    base = tmp / "cc"
    base.mkdir()
    cfg = {"confirm": False, "private_hosts": ["corp.example"]}
    m = Spy(context="t", config=cfg, base_dir=base)
    audit = base / "audit.jsonl"

    def last():
        return json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])

    m.execute({"kind": "type", "idx": 1, "text": "SuperSecret1", "element": "Пароль",
               "host": "example.com", "value": "https://example.com/login",
               "field_sensitive": True}, chat_id="c1")
    r = last()
    check("аудит: пароль маской", r.get("text") == "***(12)"
          and "SuperSecret1" not in audit.read_text(encoding="utf-8"))
    check("аудит: флаг field_sensitive сохранён", r.get("field_sensitive") is True)
    check("аудит: duration_ms измерен", isinstance(r.get("duration_ms"), int))
    m.execute({"kind": "type", "idx": 2, "text": "me@mail.ru", "element": "Поле",
               "host": "example.com", "value": "https://example.com/"}, chat_id="c1")
    check("аудит: email без флага — по значению маской",
          last().get("text") == "***(10)" and last().get("field_sensitive") is True)
    m.execute({"kind": "type", "idx": 3, "text": "кот в сапогах", "element": "Поиск",
               "host": "example.com", "value": "https://example.com/?q=кот"},
              chat_id="c1")
    check("аудит: поисковый ввод сохранён как есть",
          last().get("text") == "кот в сапогах" and not last().get("field_sensitive"))
    m.execute({"kind": "url", "value": "https://example.com/cb?code=1&token=2&page=3#x",
               "origin": "marker", "via_search": True, "retried": True},
              chat_id="c1")
    r = last()
    check("аудит: URL без секретных параметров и фрагмента",
          r.get("value") == "https://example.com/cb?page=3")
    check("аудит: origin/via_search/retried скопированы",
          r.get("origin") == "marker" and r.get("via_search") is True
          and r.get("retried") is True)
    m.execute({"kind": "multi", "items": [
        {"kind": "url", "value": "https://example.com/"},
        {"kind": "type", "idx": 1, "text": "hunter22", "element": "Пароль",
         "host": "example.com"}]}, chat_id="c1")
    check("аудит: multi-описание без введённого пароля",
          "hunter22" not in audit.read_text(encoding="utf-8"))
    m.execute({"kind": "click", "idx": 5, "element": "Войти", "host": "example.com",
               "value": "https://example.com/",
               "choose": {"path": "llm", "resolve_ms": 1234,
                          "candidates": [{"idx": i, "text": f"пункт {i} 4111111111111111",
                                          "score": 1.0} for i in range(30)],
                          "llm_response": "x" * 1000}}, chat_id="c1")
    r = last()
    check("аудит: resolve_ms из choose", r.get("resolve_ms") == 1234)
    check("аудит: кандидаты ≤10 и без номеров карт",
          len(r.get("candidates") or []) <= 10
          and "4111111111111111" not in json.dumps(r, ensure_ascii=False))
    check("аудит: ответ LLM обрезан", len(r.get("llm_response") or "") <= 201)
    m._audit_resolve("c1", "поле пароля", "example.com", "не нашёл", "low_score",
                     meta={"path": "none", "resolve_ms": 55})
    r = last()
    check("аудит resolve_fail: wide_mode и resolve_ms",
          r.get("kind") == "resolve_fail" and r.get("wide_mode") in ("hybrid", "text")
          and r.get("resolve_ms") == 55)

    # resolve_type без полей: тело «ТЕКСТ в поле ПОЛЕ» — не в аудит и не в лог
    import logging
    log_lines = []

    class _Cap(logging.Handler):
        def emit(self, rec):
            log_lines.append(rec.getMessage())

    cap = _Cap(level=logging.DEBUG)
    cc_log = logging.getLogger("app.features.computer_control")
    old_lvl = cc_log.level
    cc_log.addHandler(cap)
    cc_log.setLevel(logging.DEBUG)
    try:
        m2 = Spy(context="t2", config={"confirm": False}, base_dir=base)
        m2._snapshot_for = lambda *a, **k: (
            "https://shop.ru/", "shop.ru", [{"idx": 1, "text": "x", "tag": "a"}],
            None, None)
        m2._hidden_fields_note = lambda *a, **k: None
        act_t, err_t = m2.resolve_type("Kotik2019! в поле пароль", None, None, "c1")
        r = last()
        check("no_fields: отказ, в аудите текст маской, поле как есть",
              act_t is None and err_t and r.get("fail_reason") == "no_fields"
              and r.get("value") == "***(10) в поле пароль")
        check("no_fields: пароля нет ни в аудите, ни в логе",
              "Kotik2019" not in audit.read_text(encoding="utf-8")
              and not any("Kotik2019" in x for x in log_lines))

        # LLM-разбор: текст команды ввода в лог — только длиной
        class _IntentRouter:
            cc_provider = None

            def get_response(self, messages, **kw):
                return json.dumps({"action": "type", "text": "Kotik2019!",
                                   "field": "пароль", "site": None})

        log_lines.clear()
        m2.resolve_intent_llm("впиши Kotik2019! в поле для пароля",
                              _IntentRouter(), chat_id="c1")
        check("LLM-разбор: пароль не попал в лог",
              any("LLM-разбор" in x for x in log_lines)
              and not any("Kotik2019" in x for x in log_lines))

        # Поле карты подписано только aria: подпись в type-действии,
        # risky_label=payment → подтверждение даже при confirm:false
        m2._snapshot_for = lambda *a, **k: (
            "https://shop.ru/pay", "shop.ru",
            [{"idx": 1, "text": "", "aria": "Номер карты", "ed": 1,
              "tag": "input"}], None, None)
        act_card, err_card = m2.resolve_type("4111111111111111", None, None, "c1")
        check("resolve_type: aria поля в действии, risky_label=payment, confirm",
              act_card and err_card is None
              and act_card.get("aria") == "Номер карты"
              and ComputerControlManager.risky_label(act_card) == "payment"
              and m2.needs_confirm(act_card))
        m2._snapshot_for = lambda *a, **k: (
            "https://shop.ru/", "shop.ru",
            [{"idx": 1, "text": "", "title": "Card number", "ed": 1,
              "tag": "input"}], None, None)
        act_card2, _ = m2.resolve_type("4111111111111111", None, None, "c1")
        check("resolve_type: title поля в действии, risky_label=payment",
              act_card2 and act_card2.get("title") == "Card number"
              and ComputerControlManager.risky_label(act_card2) == "payment")

        # «Выполнено: …» в логе: введённый текст — только длиной, в любое поле
        log_lines.clear()
        m2.execute({"kind": "type", "idx": 1, "text": "mama2019", "element": "#1",
                    "host": "shop.ru", "value": "https://shop.ru/"}, chat_id="c1")
        m2.execute({"kind": "type", "idx": 2, "text": "привет котик",
                    "element": "Поиск", "host": "shop.ru",
                    "value": "https://shop.ru/"}, chat_id="c1")
        done = [x for x in log_lines if "Выполнено" in x]
        check("лог «Выполнено»: текст ввода не печатается (только длина)",
              len(done) == 2 and not any("mama2019" in x or "привет котик" in x
                                         for x in log_lines)
              and "***(8)" in done[0] and "***(12)" in done[1])
        check("describe для пользователя не изменился",
              "mama2019" in m2.describe({"kind": "type", "text": "mama2019",
                                         "element": "#1"}))

        # Маркеры в логе: URL без токенов, прочее — redact_inline
        log_lines.clear()
        m2.process_markers(
            "Открыть? [OPEN_URL:https://evil.example/cb?token=SECRETTOKEN42&x=1]",
            "c1", untrusted=True, user_text="что на странице")
        m2.process_markers(
            "Открыть? [OPEN_URL:evil.example/cb?access_token=SECRETTOKEN43] "
            "[OPEN_APP:почта me@mail.ru]", "c1")
        mk = [x for x in log_lines if "Маркер" in x]
        check("лог маркеров: «отброшены» и «отклонён» без токенов/почты",
              any("отброшены" in x for x in mk)
              and any("отклонён" in x for x in mk)
              and not any("SECRETTOKEN" in x or "me@mail.ru" in x
                          for x in log_lines)
              and any("evil.example/cb" in x for x in mk))
    finally:
        cc_log.removeHandler(cap)
        cc_log.setLevel(old_lvl)
    rr, _ = P.redact_audit_record({"kind": "resolve_fail", "fail_reason": "no_fields",
                                   "value": "Kotik2019! в поле пароль"})
    check("redact_audit_record: resolve_fail no_fields — текст маской",
          rr["value"] == "***(10) в поле пароль")
    rr, _ = P.redact_audit_record({"kind": "resolve_fail", "fail_reason": "low_score",
                                   "value": "hunter22 into Password"})
    check("redact_audit_record: «into» без no_fields — клик-цель не трогаем",
          rr["value"] == "hunter22 into Password")
    rr, _ = P.redact_audit_record({"kind": "resolve_fail", "fail_reason": "low_score",
                                   "value": "hunter22 в поле пароль"})
    check("redact_audit_record: «в поле» в старой записи — текст маской",
          rr["value"] == "***(8) в поле пароль")
    rr, _ = P.redact_audit_record({"kind": "resolve_fail", "fail_reason": "no_fields",
                                   "value": "Kotik2019!"})
    check("redact_audit_record: no_fields без сепаратора — маской целиком",
          rr["value"] == "***(10)")
    check("redact_audit_record: resolve_fail идемпотентна",
          P.redact_audit_record(rr)[1] == {})
    rr, _ = P.redact_audit_record({"kind": "resolve_fail", "fail_reason": "low_score",
                                   "value": "оплатить заказ"})
    check("redact_audit_record: цель клика в resolve_fail как есть",
          rr["value"] == "оплатить заказ")

    # read: прочитанный текст страницы — только длина
    m._audit("c1", {"kind": "read", "host": "web.telegram.org",
                    "value": "https://web.telegram.org/"}, True,
             "Мама: код от домофона 4512, пароль Kotik2019")
    r = last()
    check("аудит read: в detail только длина",
          r.get("detail") == "read:44 chars"
          and "домофон" not in audit.read_text(encoding="utf-8"))
    rr, _ = P.redact_audit_record({"kind": "read", "ok": False,
                                   "detail": "вкладка закрыта"})
    check("аудит read: ошибка чтения остаётся текстом",
          rr["detail"] == "вкладка закрыта")
    check("аудит read: идемпотентно",
          P.redact_audit_record(dict(r))[1] == {})

    # name_check.title (заголовок открытой страницы) — через ту же маску
    nc_title = "Входящие (3) — ivan.petrov@yandex.ru — Почта | +7 999 123-45-67"
    m._audit("c1", {"kind": "url", "value": "https://mail.yandex.ru/",
                    "name_check": {"expect": "почта", "ok": True,
                                   "title": nc_title}}, True, "")
    r = last()
    check("аудит name_check: приватная страница — заголовок только длиной",
          (r.get("name_check") or {}).get("title") == f"<{len(nc_title)} chars>"
          and "ivan.petrov" not in audit.read_text(encoding="utf-8"))
    rr, ch = P.redact_audit_record({"kind": "url", "value": "https://shop.test/",
                                    "name_check": {"ok": False,
                                                   "title": nc_title}})
    check("redact_audit_record: name_check обычной страницы — без ПДн",
          "ivan.petrov" not in rr["name_check"]["title"]
          and "123-45-67" not in rr["name_check"]["title"]
          and ch.get("name_check") == 1
          and P.redact_audit_record(rr)[1] == {})

    # Подписи элементов приватной страницы в лог процесса не пишутся
    import logging as _logging

    class _Cap(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs = []

        def emit(self, rec):
            self.msgs.append(rec.getMessage())
    cap_l = _Cap()
    _cc_log = _logging.getLogger("app.features.computer_control")
    _cc_log.addHandler(cap_l)
    _old_lvl = _cc_log.level
    _cc_log.setLevel(_logging.INFO)
    try:
        for url, host, secret_lab, want in (
                ("https://vk.com/im?sel=1", "vk.com",
                 "Мама: анализы на онкомаркер", False),
                ("https://shop.ru/catalog", "shop.ru", "Пицца Пепперони", True)):
            m._resolve_element = (lambda *a, _u=url, _h=host, _l=secret_lab,
                                  **kw: (_u, _h, [{"idx": 11, "text": _l,
                                                   "tag": "a"}], 11, None,
                                         {"path": "score"}, None))
            cap_l.msgs.clear()
            m.resolve_click("мама", None, None, "c1")
            m.resolve_hover("мама", None, None, "c1")
            hit = any(secret_lab in x for x in cap_l.msgs)
            check(f"лог резолва: подпись {'видна' if want else 'скрыта'} "
                  f"на {url}", hit is want and cap_l.msgs)
        check("_label_for_log: приватная — #idx/длина, обычная — обрезка",
              m._label_for_log("Мама: секрет", "https://vk.com/im?sel=1",
                               idx=7) == "#7"
              and m._label_for_log("Мама: секрет", "https://vk.com/im")
              == "<12 симв.>"
              and m._label_for_log("x" * 60, "https://shop.ru/") == "x" * 40)
    finally:
        _cc_log.removeHandler(cap_l)
        _cc_log.setLevel(_old_lvl)
        m.__dict__.pop("_resolve_element", None)

    # Иконка-кнопка без текста (только aria/title): подпись в действии,
    # risky_label её видит — подтверждение при confirm:false
    for lab_key, lab, want in (("aria", "Оплатить заказ", "payment"),
                               ("title", "Удалить из корзины", "destructive")):
        m._resolve_element = (lambda *a, _k=lab_key, _l=lab, **kw: (
            "https://shop.ru/", "shop.ru",
            [{"idx": 3, "text": "", _k: _l, "tag": "button"}], 3, None,
            {"path": "match"}, None))
        act_c, _ = m.resolve_click("иконка", None, None, "c1")
        check(f"клик по иконке с {lab_key}: подпись в действии и risky_label={want}",
              act_c and act_c.get("element") == "#3" and act_c.get(lab_key) == lab
              and ComputerControlManager.risky_label(act_c) == want
              and m.needs_confirm(act_c))
        act_h, _ = m.resolve_hover("иконка", None, None, "c1")
        check(f"наведение на иконку с {lab_key}: подпись в действии",
              act_h and act_h.get(lab_key) == lab)
    del m._resolve_element

    # Словарь рискованных подписей: финальные/денежные/отмена подписки
    rl = ComputerControlManager.risky_label
    for lab, want in (("Оформить", "commit"), ("Перейти к оформлению", "commit"),
                      ("Купить в 1 клик", "commit"), ("Заказать", "commit"),
                      ("Place your order", "commit"), ("Order now", "commit"),
                      ("Перевести 1000 ₽", "payment"), ("Пополнить", "payment"),
                      ("Вывести средства", "payment"), ("Withdraw", "payment"),
                      ("Donate", "payment"), ("Оформить подписку", "payment"),
                      ("Отменить подписку", "destructive"),
                      ("Cancel subscription", "destructive"),
                      ("Deactivate account", "destructive")):
        check(f"risky_label «{lab}» → {want}",
              rl({"kind": "click", "element": lab}) == want)
    for lab in ("Перевести", "Перевести страницу", "Заказы", "Закрыть",
                "Мои заказы", "Оформление"):
        check(f"risky_label «{lab}» → None",
              rl({"kind": "click", "element": lab}) is None)
    # Дыры словаря из ревью: мгновенная покупка, закрытие аккаунта, перевод
    for lab, want in (("Купить сейчас", "commit"), ("Buy it now", "commit"),
                      ("Закрыть аккаунт", "destructive"),
                      ("Закрыть учётную запись", "destructive"),
                      ("Close account", "destructive"),
                      ("Close my account", "destructive"),
                      ("Закрыть счёт", "destructive"),
                      ("Удалить профиль", "destructive"),
                      ("Transfer", "payment"), ("Transfer $100", "payment"),
                      ("Wire transfer", "payment"), ("Send money", "payment"),
                      ("Перевод 500 ₽", "payment"), ("Перевод на карту", "payment"),
                      ("Перевод по номеру телефона", "payment")):
        check(f"risky_label «{lab}» → {want}",
              rl({"kind": "click", "element": lab}) == want)
    for lab in ("Закрыть окно", "Close", "Закрытый профиль", "Перевод",
                "Перевод страницы", "Перевод на английский", "Перевод 2 главы",
                "Transfer files", "Купить"):
        check(f"risky_label «{lab}» → None",
              rl({"kind": "click", "element": lab}) is None)
    # Закрытие вклада/депозита — деньги со вклада; вкладка браузера — нет
    for lab in ("Закрыть вклад", "Досрочно закрыть вклад",
                "Досрочное закрытие вклада", "Закрыть депозит",
                "Расторгнуть вклад", "Close deposit", "Close my savings"):
        check(f"risky_label «{lab}» → payment",
              rl({"kind": "click", "element": lab}) == "payment")
    check("risky_label «Закрыть счёт/вклад» — рискованно",
          rl({"kind": "click", "element": "Закрыть счёт/вклад"}) is not None)
    for lab in ("Закрыть вкладку", "Закрыть вкладки", "Закрыть", "Вклады",
                "Открыть вклад"):
        check(f"risky_label «{lab}» → None",
              rl({"kind": "click", "element": lab}) is None)
    check("risky_label: «Закрыть вклад» в aria иконки → payment",
          rl({"kind": "click", "element": "", "aria": "Закрыть вклад"})
          == "payment")

    # Известные секреты чата и маска значений
    from app.features.cc_privacy import (KnownSecrets, looks_contact,
                                         mask_values)
    check("looks_contact: email и телефон — да; карта/токен/пароль — нет",
          looks_contact("ivan@mail.ru") and looks_contact("+7 913 123-45-67")
          and not looks_contact("4111 1111 1111 1111")
          and not looks_contact("Kotik2019!"))
    check("mask_values: без учёта регистра, отдельным словом, длинные первыми",
          mask_values("KOTIK2019! и kotik20190 и Kotik",
                      ["Kotik2019!", "Kotik"]) ==
          "***(10) и kotik20190 и ***(5)")
    ks = KnownSecrets(ttl=60)
    for v in ("", "да", "***(10)", "Kotik2019!"):
        ks.add("c", v)
    check("KnownSecrets: короткое/маска не берутся, секрет — да",
          ks.values("c") == ["Kotik2019!"] and ks.values("d") == [])
    ks._by_chat["c"]["Kotik2019!"] = 0.0
    check("KnownSecrets: TTL снимает", ks.values("c") == [])
    ks.add("c", "Kotik2019!")
    ks.purge("c")
    check("KnownSecrets: purge чата", ks.values("c") == [])
    check("risky_label: голое «Перевод» в банке — payment, в переводчике — None",
          rl({"kind": "click", "element": "Перевод",
              "host": "online.sberbank.ru"}) == "payment"
          and rl({"kind": "click", "element": "Перевести",
                  "host": "www.tbank.ru"}) == "payment"
          and rl({"kind": "click", "element": "Перевод",
                  "host": "translate.google.com"}) is None)

    # Отчёт «что на странице» (уходит облачной модели) — URL без токенов
    from app.features.computer_control import page_view_text, page_view_full_text
    pv_url = "https://example.com/reset?token=SECRETTOKEN&code=123456"
    pv = page_view_text(pv_url, "example.com",
                        [{"idx": 1, "tag": "a", "text": "",
                          "href": "https://example.com/cb?access_token=HREFSECRET"}])
    pvf = page_view_full_text(pv_url, "example.com", [])
    check("page_view_text: URL и href без токена/кода",
          "SECRETTOKEN" not in pv and "123456" not in pv
          and "HREFSECRET" not in pv and "https://example.com/reset" in pv)
    check("page_view_full_text: URL без токена/кода",
          "SECRETTOKEN" not in pvf and "123456" not in pvf)

    # fast-path бота: лог через _describe_safe, текст ввода — длиной
    bi_src = (Path(__file__).parent.parent / "app" / "bot_instance.py").read_text(
        encoding="utf-8")
    fp = bi_src[bi_src.find("fast-path: '"):][:300]
    check("fast-path лог: _describe_safe вместо describe, текст ввода длиной",
          "_describe_safe" in fp and "cc.describe(cc_action)}" not in fp
          and "shown" in fp)

    # Лок исполнения: два чата не чередуются на браузере
    m.sleep = 0.05
    ths = [threading.Thread(target=m.execute,
                            args=({"kind": "click", "idx": i, "host": "a.com",
                                   "value": "https://a.com/"}, f"chat{i}"))
           for i in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    check("лок: исполнения разных чатов не пересекаются", not m.overlap)
    m.sleep = 0.0

    # Роутер в execute: приватная страница → PrivateRouter
    m.routers.clear()
    m.execute({"kind": "click", "idx": 1, "host": "online.sberbank.ru",
               "value": "https://online.sberbank.ru/"}, "c1", router=spy)
    m.execute({"kind": "click", "idx": 1, "host": "youtube.com",
               "value": "https://youtube.com/"}, "c1", router=spy)
    m.execute({"kind": "click", "idx": 1, "host": "mail.corp.example",
               "value": "https://mail.corp.example/"}, "c1", router=spy)
    check("execute: приватный хост → PrivateRouter в _dispatch",
          isinstance(m.routers[0], P.PrivateRouter))
    check("execute: обычный хост → исходный роутер", m.routers[1] is spy)
    check("execute: private_hosts из конфига", isinstance(m.routers[2], P.PrivateRouter))

    # vision: скриншот приватной страницы не снимается
    import app.features.browser_actions as ba
    orig_shot = ba.screenshot_viewport
    shots = []
    ba.screenshot_viewport = lambda *a, **kw: shots.append(a) or b"PNG"
    try:
        vis = {}
        check("vision: приватная страница — кадра нет",
              m._vision_shot("online.sberbank.ru", None, vis) is None
              and shots == [] and vis.get("private") is True)
        check("vision: обычная страница — кадр снимается",
              m._vision_shot("youtube.com", None, {}) == b"PNG" and len(shots) == 1)
    finally:
        ba.screenshot_viewport = orig_shot

    # _choose_element на приватной странице: облачный роутер не зовётся
    spy2 = SpyRouter()
    # Близкие скоры → развилка идёт в LLM (на обычной странице)
    items = [{"idx": 1, "tag": "button", "text": "Далее вперёд", "vp": 1,
              "w": 80, "h": 30},
             {"idx": 2, "tag": "a", "text": "Далее назад", "vp": 1,
              "w": 80, "h": 30}]
    try:
        _lr.get_local_router = lambda context=None: _FakeLocal(available=False)
        idx_p, meta_p = m._choose_element("далее", items, spy2,
                                          host="online.sberbank.ru")
        check("выбор элемента на приватной: облачный роутер не вызван, "
              "решает скоринг", spy2.calls == [] and idx_p == 1)
        m._choose_element("далее", items, spy2, host="youtube.com")
        check("выбор элемента на обычной: развилка ушла в роутер (контроль)",
              len(spy2.calls) == 1)
    finally:
        _lr.get_local_router = orig_glr

    # read_page_section: текст приватной секции в LLM не отдаётся
    orig_rs = getattr(ba, "read_section", None)
    ba.read_section = lambda host, q, tab_id=None: "Баланс 100 500 ₽ по карте"
    try:
        m._last_host, m._last_tab_id, m._last_url = (
            "online.sberbank.ru", None, "https://online.sberbank.ru/main")
        check("секция приватной страницы → None",
              m.read_page_section("баланс", None) is None)
        m._last_host, m._last_url = "dodopizza.ru", "https://dodopizza.ru/"
        res = m.read_page_section("пиццы", None)
        check("секция обычной страницы читается", bool(res) and res[1] == "dodopizza.ru")
        # Явно названный домен: приватность по фактическому адресу вкладки
        # (tab_url) и по отслеживаемому адресу того же хоста, а не по хосту
        orig_tu = getattr(ba, "tab_url", None)
        ba.tab_url = lambda host_part=None, tab_id=None: ""
        m._last_host, m._last_tab_id, m._last_url = (
            "vk.com", 7, "https://vk.com/im?sel=123")
        check("секция «…на vk.com» при отслеживаемой vk.com/im → None",
              m.read_page_section("диалоги", "vk.com") is None)
        m._last_host, m._last_tab_id, m._last_url = (
            "google.com", 8, "https://google.com/")
        ba.tab_url = lambda host_part=None, tab_id=None: (
            "https://vk.com/im?sel=5" if host_part == "vk.com" else "")
        check("секция «…на vk.com»: фактический адрес вкладки приватный → None",
              m.read_page_section("диалоги", "vk.com") is None)
        ba.tab_url = lambda host_part=None, tab_id=None: "https://vk.com/feed"
        check("секция «…на vk.com»: обычная лента читается",
              bool(m.read_page_section("диалоги", "vk.com")))
        if orig_tu is not None:
            ba.tab_url = orig_tu
    finally:
        if orig_rs is not None:
            ba.read_section = orig_rs

    # instruction_block: URL открытой страницы без токенов
    m._last_host = "example.com"
    m._last_url = "https://example.com/cb?code=SECRET1&page=2#access_token=T"
    ib = m.instruction_block(lang="ru")
    check("prompt: URL без code/фрагмента",
          "SECRET1" not in ib and "access_token" not in ib
          and "https://example.com/cb?page=2" in ib)
    m._last_host = "online.sberbank.ru"
    m._last_url = "https://online.sberbank.ru/cards/123?x=1"
    ib = m.instruction_block(lang="ru")
    check("prompt: приватная страница — только хост",
          "online.sberbank.ru" in ib and "/cards/123" not in ib)

    # last_tab.json: URL на диске без токенов/фрагмента
    m._last_host = "example.com"
    m._save_last_page("https://example.com/cb?session=S3CR3T&page=2#tok")
    lt = (base / "last_tab.json").read_text(encoding="utf-8")
    check("last_tab.json: URL без session/фрагмента",
          "S3CR3T" not in lt and "#tok" not in lt and "page=2" in lt)

    # Маршрут nav: после снапшота каждой страницы роутер обёрнут по её URL
    import inspect
    nav_src = inspect.getsource(ComputerControlManager)
    check("nav: роутер перепроверяется по URL страницы шага",
          re.search(r"не читается страница \{where\}: \{last_err\}\"\)\n"
                    r"(?:\s*#[^\n]*\n)*\s*router = self\._privacy_router\("
                    r"router, url or host\)", nav_src) is not None)

    # flavor-реплика (внешняя модель) — без введённого пароля
    from app.features import flavor_text as ft

    class _FlavRouter:
        answer_provider = "assigned"

        def __init__(self):
            self.msgs = []

        def get_response_assigned(self, provider, messages, **kw):
            self.msgs.append(json.dumps(messages, ensure_ascii=False))
            return "Готово, ввёл."

    class _FlavBot:
        context = "flavtest"

        class persona:
            system_prompt = "You are a calm assistant."

    fb = _FlavBot()
    fb.computer_control = m
    fb.router = _FlavRouter()
    for bucket in ("ok", "err"):
        try:
            ft._from_live(fb, "type", bucket,
                          {"kind": "type", "text": "Pa55word!", "element": "Пароль",
                           "host": "example.com", "field_sensitive": True},
                          "ошибка при вводе me@mail.ru")
        except Exception as e:
            print(f"  (flavor {bucket}: {e})")
    check("flavor: пароль и email не ушли во внешнюю модель",
          len(fb.router.msgs) == 2
          and all("Pa55word!" not in x and "me@mail.ru" not in x
                  for x in fb.router.msgs))

    # ── 5. Ротация и хвост ──
    print("cc_privacy: ротация")
    rot = tmp / "rot" / "audit.jsonl"
    rot.parent.mkdir()
    for i in range(300):
        P.audit_append(rot, {"i": i, "pad": "x" * 50}, max_bytes=4000, backups=2)
    # .audit.jsonl.lock — межпроцессный лок записи, не лог
    files = sorted(p.name for p in rot.parent.iterdir()
                   if not p.name.endswith(".lock"))
    check("ротация: не больше 3 файлов", files == ["audit.jsonl", "audit.jsonl.1",
                                                   "audit.jsonl.2"])
    check("ротация: каждый файл ≤ лимита",
          all(p.stat().st_size <= 4000 for p in rot.parent.iterdir()))
    check("лок аудита: один скрытый файл на audit.jsonl и .1/.2",
          P.audit_lock_path(rot) == P.audit_lock_path(
              rot.with_name("audit.jsonl.2")) == rot.with_name(".audit.jsonl.lock"))
    tail = [json.loads(x)["i"] for x in P.read_tail_lines(rot, 20)]
    check("хвост: последние 20 записей по порядку", tail == list(range(280, 300)))
    n_cur = len(rot.read_text().splitlines())
    tail2 = [json.loads(x)["i"] for x in P.read_tail_lines(rot, n_cur + 5)]
    check("хвост: добор из .1 после ротации",
          tail2 == list(range(300 - n_cur - 5, 300)))
    check("хвост: нет файла → []", P.read_tail_lines(tmp / "nope.jsonl", 5) == [])

    # ── 6. Сценарии: секреты → слоты ──
    print("scenario_manager: секреты")
    from app.features.scenario_manager import ScenarioManager

    sc_base = tmp / "sc"
    sc_base.mkdir()
    cc = Spy(context="sc", config={"confirm": False}, base_dir=sc_base)
    now = time.time()
    recs = [
        {"ts": now, "chat_id": "u", "ok": True, "kind": "url",
         "value": "https://shop.example/login?next=/&token=abc"},
        {"ts": now, "chat_id": "u", "ok": True, "kind": "type",
         "element": "Логин", "text": "ivan@mail.ru", "host": "shop.example"},
        {"ts": now, "chat_id": "u", "ok": True, "kind": "type",
         "element": "Пароль", "text": "P4ssw0rd!", "host": "shop.example"},
        {"ts": now, "chat_id": "u", "ok": True, "kind": "click",
         "element": "Войти", "host": "shop.example"},
    ]
    with open(sc_base / "audit.jsonl", "w", encoding="utf-8") as f:
        for rec in recs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    sm = ScenarioManager(context="sc", computer_control=cc, base_dir=sc_base)
    trace = sm._trace("u")
    check("трасса: 4 действия из хвоста", len(trace) == 4)
    lines = sm._trace_lines(trace)
    check("трасса для LLM: без пароля/почты и токена URL",
          "P4ssw0rd!" not in lines and "ivan@mail.ru" not in lines
          and "token=abc" not in lines and "<SECRET" in lines)

    class GenRouter:
        def __init__(self):
            self.prompts = []

        def get_response(self, messages, **kw):
            self.prompts.append(messages[0]["content"])
            return json.dumps({"aliases": ["вход"], "steps": [
                {"op": "open", "url": "https://shop.example/login?token=abc"},
                {"op": "type", "field": "Логин", "value": "ivan@mail.ru",
                 "host": "shop.example"},
                {"op": "type", "field": "Пароль", "value": "P4ssw0rd!",
                 "host": "shop.example"},
                {"op": "click", "target": "Войти", "host": "shop.example"}]},
                ensure_ascii=False)

    gr = GenRouter()
    sc, err = sm.build_from_trace("u", "вход в магазин", gr)
    check("сценарий собран", sc is not None and err is None)
    # Трасса со страницы входа (/login) — приватная: облачному роутеру не
    # уходит вовсе (локальной модели нет — rule-based); путь «LLM вернула
    # литералы» на обычной странице — scripts/test_cc_privacy_choke.py
    check("промпт обобщения без секретов (приватная трасса — не в облако)",
          not gr.prompts)
    saved = (sc_base / "scenarios.json").read_text(encoding="utf-8")
    check("scenarios.json без секретов (LLM вернула литералы)",
          "P4ssw0rd!" not in saved and "ivan@mail.ru" not in saved
          and "token=abc" not in saved)
    types = [s for s in sc["steps"] if s["op"] == "type"] if sc else []
    asks = [s for s in sc["steps"] if s["op"] == "ask"] if sc else []
    check("секретные вводы стали слотами «спросить каждый раз»",
          len(types) == 2 and all(t["value"].startswith("{") for t in types)
          and len(asks) >= 2)
    check("_step_line: значение слота в LLM-контекст маской",
          "P4ssw0rd!" not in ScenarioManager._step_line(
              {"op": "type", "field": "Пароль", "value": "{p}"}, {"p": "P4ssw0rd!"}))
    # Значение слота не подставляется НИКОГДА — подпись поля любая
    # (Ключ доступа, Contraseña, Passwort), в роадмап — плейсхолдер
    for fld in ("Ключ доступа", "Contraseña", "Passwort"):
        ln = ScenarioManager._step_line(
            {"op": "type", "field": fld, "value": "{секрет1}", "host": "r.test"},
            {"секрет1": "Kotik2019!"})
        check(f"_step_line: слот в поле «{fld}» — плейсхолдер, не значение",
              "Kotik2019" not in ln and "{секрет1}" in ln)
    ks_sc = P.KnownSecrets()
    ks_sc.add("sc", "Kotik2019!")
    check("_step_line: известный секрет в литерале шага — маской",
          "Kotik2019" not in ScenarioManager._step_line(
              {"op": "click", "target": "ключ Kotik2019!"}, {}))
    # _llm_recover: облачный промпт — без значений слотов и секретов
    from types import SimpleNamespace as _NS
    rec_prompts = []
    rec_cc = _NS(
        _snapshot_for=lambda h, chat_id="": ("http://r.test/status", "r.test",
                                             [{"idx": 1, "tag": "a",
                                               "text": "Меню"}], None, None),
        _score_candidates=lambda its, goal, host=None: [(1.0, it) for it in its],
        _privacy_router=lambda r, *w: r,
        execute=lambda a, chat_id, router=None: (True, ""))
    rec_sm = _NS(cc=rec_cc, _subst=lambda s, slots: re.sub(
        r"\{([^\s{}]+)\}", lambda m: slots.get(m.group(1), m.group(0)), s),
        _step_line=ScenarioManager._step_line)
    rec_steps = [{"op": "ask", "slot": "секрет1", "question": "Ключ?"},
                 {"op": "type", "field": "Contraseña", "value": "{секрет1}",
                  "host": "r.test"},
                 {"op": "click", "target": "{секрет1}", "host": "r.test"}]
    ScenarioManager._llm_recover(
        rec_sm, rec_steps[2], {"name": "вайфай", "steps": rec_steps, "pos": 2,
                               "slots": {"секрет1": "Kotik2019!"}},
        "sc", _NS(get_response=lambda msgs, **kw: (
            rec_prompts.append(msgs[-1]["content"]), "no")[1]))
    check("_llm_recover: значение слота-секрета не в облачном промпте",
          rec_prompts and not any("Kotik2019" in p for p in rec_prompts))
    ks_sc.purge("sc")

    # ── 7. Снапшот-JS: значения полей ввода не собираются ──
    print("browser_actions: снапшот без значений полей")
    check("vpcInfo: подпись поля без e.value",
          "var t=(ed?(vpcLabel(e)||e.title||" in ba._SNAPSHOT_JS
          and "vpcLabel(e)||e.value" not in ba._SNAPSHOT_JS)
    # value в подписи — только у кнопок: у прочих input/textarea/select это
    # данные пользователя (date/range вне edsel, поле рядом с совпадением)
    guard = ("((/^(INPUT|TEXTAREA|SELECT)$/.test(e.tagName)&&"
             "!/^(button|submit|reset)$/i.test(e.type||''))?'':e.value)")
    check("фрейм-снапшот: значение поля не в подписи",
          "(ed?'':(e.innerText||" + guard + "))" in ba._FRAME_SNAPSHOT_JS
          and "e.innerText||e.value" not in ba._FRAME_SNAPSHOT_JS)
    check("снапшот: не-ed элементы — value только у кнопок",
          "(e.innerText||" + guard + "||e.getAttribute('aria-label')" in ba._SNAPSHOT_JS
          and "e.innerText||e.value" not in ba._SNAPSHOT_JS)
    check("целевой снапшот (info): value только у кнопок",
          "var t=(e.innerText||" + guard in ba._GOAL_SNAPSHOT_JS
          and "e.innerText||e.value" not in ba._GOAL_SNAPSHOT_JS)
    check("зоны vision: value только у кнопок, contenteditable без текста",
          "e.isContentEditable?'':norm(e.innerText)" in ba._ALL_CLICKABLE_BOXES_JS
          and "button|submit|reset" in ba._ALL_CLICKABLE_BOXES_JS)
    check("sn: autocomplete one-time-code/cc-/password — чувствительное поле",
          "one-time-code|cc-|" in ba._SNAPSHOT_JS)
    node = shutil.which("node")
    if node:
        # Поведение guard: пароль/текст/дата — пусто, кнопка — надпись
        gchk = subprocess.run(
            [node, "-e",
             "var g=function(e){return " + guard + ";};"
             "var r=[g({tagName:'INPUT',type:'password',value:'P4ss'}),"
             "g({tagName:'INPUT',type:'date',value:'1990-01-01'}),"
             "g({tagName:'TEXTAREA',value:'secret note'}),"
             "g({tagName:'INPUT',type:'submit',value:'Войти'}),"
             "g({tagName:'BUTTON',type:'submit',value:'go'})];"
             "process.stdout.write(JSON.stringify(r))"],
            capture_output=True, text=True, timeout=20)
        check("guard: значения полей пусты, надпись кнопок остаётся",
              json.loads(gchk.stdout or "null") == ["", "", "", "Войти", "go"])
        for name in ("_SNAPSHOT_JS", "_FRAME_SNAPSHOT_JS", "_ALL_CLICKABLE_BOXES_JS",
                     "_GOAL_SNAPSHOT_JS"):
            js = getattr(ba, name).replace("__BASE__", "0").replace("__LIM__", "10")
            # new Function разбирает тело: синтаксическая ошибка — ненулевой код
            chk = subprocess.run(
                [node, "-e", "try{new Function(require('fs').readFileSync(0,'utf8'));}"
                             "catch(e){console.error(e.message);process.exit(1)}"],
                input=js, capture_output=True, text=True, timeout=20)
            check(f"JS-синтаксис {name}", chk.returncode == 0)
    else:
        print("  (node не найден — синтаксис JS не проверяется)")

    # ── 8. scripts/scrub_cc_audit ──
    print("scripts.scrub_cc_audit")
    sa = tmp / "scrub" / "c" / "computer_control"
    sa.mkdir(parents=True)
    raw = [
        {"ts": 1, "kind": "type", "text": "Pa55word!", "element": "Пароль"},
        {"ts": 2, "kind": "url", "value": "https://x.com/cb?code=Q1&v=abc#frag"},
        {"ts": 3, "kind": "type", "text": "котики", "element": "Поиск"},
    ]
    ap = sa / "audit.jsonl"
    ap.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in raw)
                  + "not json 4111111111111111\n", encoding="utf-8")
    before = ap.read_text(encoding="utf-8")
    py = sys.executable
    root = Path(__file__).parent.parent
    dry = subprocess.run([py, "-m", "scripts.scrub_cc_audit", str(ap)],
                         capture_output=True, text=True, cwd=str(root), timeout=60)
    check("scrub dry-run: файл не тронут", ap.read_text(encoding="utf-8") == before)
    check("scrub dry-run: счётчики по полям, без значений",
          "text=1" in dry.stdout and "value=1" in dry.stdout
          and "Pa55word" not in dry.stdout and "Q1" not in dry.stdout)
    app_ = subprocess.run([py, "-m", "scripts.scrub_cc_audit", "--apply", str(ap)],
                          capture_output=True, text=True, cwd=str(root), timeout=60)
    after = ap.read_text(encoding="utf-8")
    check("scrub --apply: секретов нет",
          app_.returncode == 0 and "Pa55word!" not in after and "code=Q1" not in after
          and "#frag" not in after and "4111111111111111" not in after)
    check("scrub --apply: безвредное сохранено", "котики" in after and "v=abc" in after)
    check("scrub --apply: без резервных/временных копий",
          sorted(p.name for p in sa.iterdir()
                 if not p.name.endswith(".lock")) == ["audit.jsonl"])
    again = subprocess.run([py, "-m", "scripts.scrub_cc_audit", str(ap)],
                           capture_output=True, text=True, cwd=str(root), timeout=60)
    check("scrub: повторный прогон — изменений нет",
          "изменено: —" in again.stdout)

    # Новые правила чистки: resolve_fail no_fields и текст read
    ap2 = sa / "audit.jsonl.1"
    ap2.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in [
        {"ts": 5, "kind": "resolve_fail", "fail_reason": "no_fields",
         "value": "Kotik2019! в поле пароль", "ok": False},
        {"ts": 6, "kind": "read", "ok": True, "detail": "Мама: пароль Kotik2019"},
    ]), encoding="utf-8")
    dry2 = subprocess.run([py, "-m", "scripts.scrub_cc_audit", str(ap2)],
                          capture_output=True, text=True, cwd=str(root), timeout=60)
    check("scrub dry-run: resolve_fail/read посчитаны, значения не выведены",
          "value=1" in dry2.stdout and "detail=1" in dry2.stdout
          and "Kotik2019" not in dry2.stdout)

    # --apply под тем же локом, что запись: бот ждёт конца прохода
    import importlib
    scrub = importlib.import_module("scripts.scrub_cc_audit")
    wrote = threading.Event()

    def _writer():
        P.audit_append(ap, {"ts": 99, "kind": "click", "element": "late"})
        wrote.set()

    with P.audit_file_lock(ap, timeout=None) as got:
        th = threading.Thread(target=_writer)
        th.start()
        time.sleep(0.3)
        blocked = not wrote.is_set()
    th.join(5)
    check("лок аудита: запись ждёт, пока лок держит чистка",
          got and blocked and wrote.is_set()
          and '"late"' in ap.read_text(encoding="utf-8"))
    with P.audit_file_lock(ap, timeout=None):
        try:
            scrub._apply(ap, lock_timeout=0.2)
            timed_out_ok = True
        except scrub.FileChanged:
            timed_out_ok = False
    check("scrub --apply без лока (не дождался): файл не менялся — заменён",
          timed_out_ok)

    # Бот старой версии без лока дописал строку во время прохода — отказ,
    # файл не заменён, недописанное не склеено
    before2 = ap.read_text(encoding="utf-8")
    orig_rl = scrub._redact_line

    def _racy(line, counts):
        with open(ap, "a", encoding="utf-8") as f:
            f.write('{"ts": 100, "kind": "click", "elem')
        scrub._redact_line = orig_rl
        return orig_rl(line, counts)

    scrub._redact_line = _racy
    try:
        refused = False
        try:
            scrub._apply(ap)
        except scrub.FileChanged:
            refused = True
    finally:
        scrub._redact_line = orig_rl
    after2 = ap.read_text(encoding="utf-8")
    check("scrub --apply: файл изменился за проход — отказ, без замены",
          refused and after2.startswith(before2)
          and after2.endswith('"elem')
          and not any(p.name.endswith(".tmp") for p in sa.iterdir()))

    shutil.rmtree(tmp, ignore_errors=True)
    total = ok + len(fails)
    print(f"\n{ok}/{total} проверок прошло" + (f"; FAIL: {len(fails)}" if fails else ""))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())

"""Тест защиты аккаунтов веб-чатов от блокировки.

Проверяет:
* распознавание баннера блокировки («Due to violation of user policies, your
  account has been suspended until October 9, 2026 05:14», русские формы) и
  срока из него; обычные тексты блокировкой не считаются;
* suspend_site: карантин kind="suspended" до срока, уведомление
  пользователю, запись в файл — и другой процесс бота подхватывает
  блокировку из файла (без второго уведомления); истёкшая — не действует;
* _suspension_check: баннер страницы → блокировка; пустой замер, сбой
  замера и поисковик — нет; сканер страницы (настоящий Chrome, если есть)
  находит баннер и НЕ берёт ту же фразу из ответа модели, ленты и поля ввода;
* бюджет новых чатов: час и сутки, сайты независимы, темп отправок в том же
  файле не теряется; в WebChatLLM новый чат сверх бюджета — без отправки,
  сохранённый чат бюджет не тратит; заблокированный сайт — без отправки.

Сеть не нужна. Запуск: PYTHONPATH=. python3 scripts/test_webchat_ban.py
"""

import glob
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


BANNER = ("Due to violation of user policies, your account has been suspended until "
          "October 9, 2026 05:14. If you have any questions, please Contact us.")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="webchat_ban_"))
    from app.features import web_llm as wl
    wl._pace_path = lambda: tmp / "pacing.json"
    wl._suspended_path = lambda: tmp / "suspended.json"

    def reset_memory():
        with wl._QUARANTINE_LOCK:
            wl._SITE_QUARANTINE.clear()
            wl._PENDING_ALERTS.clear()
        wl._SUSPEND_SYNC_AT = 0.0
        wl._SUSPEND_MTIME = 0.0

    print("распознавание баннера и срока:")
    check("баннер deepseek — блокировка", bool(wl._SUSPENDED_RE.search(BANNER)))
    for text in ("Ваш аккаунт заблокирован за нарушение правил",
                 "Действие аккаунта временно приостановлено",
                 "Your account has been deactivated", "This account was banned"):
        check(f"«{text[:40]}» — блокировка", bool(wl._SUSPENDED_RE.search(text)))
    for text in ("Length limit reached. Please start a new chat.",
                 "You've reached your message limit", "Account settings",
                 "suspended animation is a sci-fi trope", "Войдите в аккаунт"):
        check(f"«{text[:40]}» — не блокировка", not wl._SUSPENDED_RE.search(text))
    now = time.time()
    want = datetime(2026, 10, 9, 5, 14).timestamp() - now
    got = wl.suspension_ttl(BANNER, now=now)
    check("срок «until October 9, 2026 05:14» — по местному времени",
          abs(got - min(max(want, wl.SUSPENDED_MIN_TTL_SEC), wl.SUSPENDED_MAX_TTL_SEC)) < 1)
    check("срок не назван — трое суток",
          wl.suspension_ttl("Your account has been suspended.", now=now) == wl.SUSPENDED_DEFAULT_TTL_SEC)
    ru = wl.suspension_ttl("Аккаунт заблокирован до 09.10.2026 05:14", now=now)
    check("русский срок «до 09.10.2026 05:14»",
          abs(ru - min(max(want, wl.SUSPENDED_MIN_TTL_SEC), wl.SUSPENDED_MAX_TTL_SEC)) < 1)
    check("срок в прошлом — не меньше часа",
          wl.suspension_ttl("account suspended until January 1, 2020", now=now) == wl.SUSPENDED_MIN_TTL_SEC)

    print("блокировка во всех процессах:")
    reset_memory()
    ttl = wl.suspend_site("deepseek", BANNER)
    check("карантин вида suspended", wl.quarantine_kind("deepseek") == "suspended")
    alerts = wl.pop_quarantine_alerts()
    check("уведомление пользователю — одно, со сроком",
          len(alerts) == 1 and alerts[0]["kind"] == "suspended" and alerts[0]["until"] > time.time())
    st = json.loads((tmp / "suspended.json").read_text())
    check("записано в файл со сроком и текстом сайта",
          abs(st["deepseek"]["until"] - (time.time() + ttl)) < 5 and "suspended" in st["deepseek"]["reason"])
    # «Другой процесс»: память пуста, файл тот же
    reset_memory()
    check("другой процесс видит блокировку из файла", wl.site_quarantined("deepseek")
          and wl.quarantine_kind("deepseek") == "suspended")
    check("…без второго уведомления", wl.pop_quarantine_alerts() == [])
    check("статус для веба показывает блокировку",
          wl.quarantine_status().get("deepseek", {}).get("kind") == "suspended")
    check("другие сайты не задеты", not wl.site_quarantined("qwen"))
    (tmp / "suspended.json").write_text(json.dumps({"zai": {"until": time.time() - 5, "reason": "x"}}))
    reset_memory()
    check("истёкшая блокировка не действует", not wl.site_quarantined("zai"))

    print("проверка страницы:")
    reset_memory()

    class FakeBa:
        def __init__(self, reply):
            self.reply = reply
            self.js = []

        def eval_js(self, host, tab_id, js):
            self.js.append(js)
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

    chat = wl.WebChatLLM("deepseek", base_dir=tmp / "deepseek", channel="cc")
    fb = FakeBa("")
    check("чистая страница — не блокировка", chat._suspension_check(fb, 1) is False
          and not wl.site_quarantined("deepseek"))
    check("сканер исключает ответы, ленту и поле ввода",
          ".ds-assistant-message-main-content" in fb.js[0] and "textarea" in fb.js[0])
    check("сбой замера — «неизвестно», не блокировка",
          chat._suspension_check(FakeBa(RuntimeError("tab gone")), 1) is False)
    check("баннер — блокировка", chat._suspension_check(FakeBa(BANNER), 1) is True
          and wl.quarantine_kind("deepseek") == "suspended")
    g = wl.WebChatLLM("google", base_dir=tmp / "google", channel="search")
    gb = FakeBa(BANNER)
    check("поисковик не проверяется (нет аккаунта)", g._suspension_check(gb, 1) is False and not gb.js)

    _dom_scan()

    print("бюджет новых чатов:")
    os.environ["WEBCHAT_NEW_CHATS_PER_HOUR"] = "3"
    os.environ["WEBCHAT_NEW_CHATS_PER_DAY"] = "5"
    (tmp / "pacing.json").write_text("{}")
    got = [wl.new_chat_reserve("deepseek", "main") for _ in range(4)]
    check("3 новых чата за час — можно, 4-й — нет", got == [True, True, True, False])
    check("другой сайт — свой бюджет", wl.new_chat_reserve("qwen", "main"))
    st = json.loads((tmp / "pacing.json").read_text())
    st["deepseek"]["new"] = [time.time() - 7200] * 2 + [time.time() - 4000] * 3
    (tmp / "pacing.json").write_text(json.dumps(st))
    check("за сутки 5 — новый нельзя, хотя за час пусто", wl.new_chat_reserve("deepseek") is False)
    os.environ["WEBCHAT_NEW_CHATS_PER_DAY"] = "0"
    check("0 — без суточного ограничения", wl.new_chat_reserve("deepseek") is True)
    os.environ["WEBCHAT_NEW_CHATS_PER_DAY"] = "5"
    os.environ["WEBCHAT_AUTO_GAP_SEC"] = "0"
    os.environ["WEBCHAT_AUTO_JITTER_SEC"] = "0"
    wl.pace_reserve("zai", "side")
    wl.new_chat_reserve("zai")
    wl.pace_reserve("zai", "side")
    rec = json.loads((tmp / "pacing.json").read_text())["zai"]
    check("темп отправок и бюджет чатов уживаются в одном файле",
          len(rec.get("auto", [])) == 2 and len(rec.get("new", [])) == 1 and rec.get("last"))

    print("WebChatLLM:")
    from app.features import browser_actions as ba
    ba.count_blocks = lambda *a, **kw: 0
    ba.last_block_text = lambda *a, **kw: ""
    reset_memory()

    class FakeChat(wl.WebChatLLM):
        def __init__(self, site, url=""):
            super().__init__(site, base_dir=tmp / f"fc-{site}", channel="main")
            self.sent = []
            self.url = url

        def _chat_url(self):
            return self.url

        def _ensure_chat(self, fresh=False):
            self._tab_id = 1
            return 1

        def _send_verified(self, ba_, host, tab_id, prompt, wait_upload=False):
            self.sent.append(prompt)
            return None

        def _wait_answer(self, *a, **kw):
            return "ответ"

        def _capture_chat_url(self, *a, **kw):
            pass

        def _snap_for(self, ba_, tab_id):
            return None

    (tmp / "pacing.json").write_text("{}")
    os.environ["WEBCHAT_NEW_CHATS_PER_HOUR"] = "1"
    msg = [{"role": "user", "content": "привет"}]
    a = FakeChat("qwen")
    check("новый чат в бюджете — отправлен", a.get_response(msg) == "ответ" and len(a.sent) == 1)
    b = FakeChat("qwen")
    check("второй новый чат сверх бюджета — без отправки", b.get_response(msg) is None and not b.sent)
    c = FakeChat("qwen", url="https://chat.qwen.ai/c/123")
    check("сохранённый чат бюджет не тратит — отправлен",
          c.get_response(msg) == "ответ" and len(c.sent) == 1)
    wl.suspend_site("kimi", "Your account has been suspended.")
    k = FakeChat("kimi", url="https://kimi.com/chat/1")
    check("заблокированный сайт — без отправки", k.get_response(msg) is None and not k.sent)

    for key in ("WEBCHAT_NEW_CHATS_PER_HOUR", "WEBCHAT_NEW_CHATS_PER_DAY",
                "WEBCHAT_AUTO_GAP_SEC", "WEBCHAT_AUTO_JITTER_SEC"):
        os.environ.pop(key, None)
    reset_memory()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


def _dom_scan():
    """Сканер страницы в настоящем Chrome: баннер находится, та же фраза в
    ответе модели, ленте и поле ввода — нет. Без Chrome/Playwright — пропуск."""
    print("сканер страницы (Chrome):")
    from app.features import web_llm as wl
    exe = sorted(glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/*.app/Contents/MacOS/*")))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        exe = []
    if not exe:
        print("  (пропущено — нет Playwright или Chrome for Testing)")
        return
    skip = [".ds-assistant-message-main-content", ".ds-message", "textarea"]
    js = wl._SUSPENSION_JS % (json.dumps(wl._SUSPENDED_RE.pattern), json.dumps(", ".join(skip)))
    page_answer = """<div class="ds-message"><div class="ds-assistant-message-main-content">
      <p>Your account has been suspended until Monday — так пишут сервисы.</p></div></div>
      <div class="ds-message"><p>мой аккаунт заблокирован, что делать?</p></div>
      <textarea>account has been suspended</textarea>"""
    banner = '<div class="x9"><span>' + BANNER.replace("Contact us", '<a href="#">Contact us</a>') + "</span></div>"
    with sync_playwright() as pw:
        br = pw.chromium.launch(executable_path=exe[-1])
        pg = br.new_page()
        pg.set_content(f"<html><body>{page_answer}</body></html>")
        check("фраза только в ответе, ленте и поле ввода — не баннер", pg.evaluate(js) == "")
        pg.set_content(f"<html><body>{page_answer}{banner}</body></html>")
        found = pg.evaluate(js)
        check("баннер сайта найден", "suspended until October 9" in found)
        pg.set_content("<html><body><div>Ваш аккаунт заблокирован до 09.10.2026 05:14</div></body></html>")
        check("русский баннер найден (кириллица в шаблоне JS)", "заблокирован" in pg.evaluate(js))
        br.close()


if __name__ == "__main__":
    sys.exit(main())

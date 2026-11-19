"""Тест: «Сервисы» на телефоне без компьютера и их перенос на компьютер.

Телефон проверяет ленты, GitHub, почту IMAP и Gmail сам, в Java
(web/android/app/src/main/java/io/vpcore/app/Svc*.java — порт
app/integrations без модели). Классы Svc* без android.* — тест собирает их
обычным javac и гоняет обвязкой scripts/java/SvcHarness.java на тех же
данных, что и ядро:

  1. сборка: Svc* собираются javac без Android (org.json — из кеша Gradle);
  2. разбор: правила (основы слов, источник, текст), признаки всех
     источников, пересказ — те же решения, что triage.decide (engine rules);
  3. тексты уведомлений и сводок ru/en, тихие часы, слоты сводки;
  4. уведомитель: срочное в тихие часы, лимит в час, «не беспокоить»,
     сводка по расписанию, «прочитано в сервисе», устаревшее — в сводку;
  5. лента и настройки: запись события и её id, повторы, нормализация
     настроек и правил;
  6. RSS/Atom: разбор (CDATA, HTML, сущности, content:encoded, dc:date,
     ссылки Atom, RDF, без id, DOCTYPE, не XML), строки и даты — и опрос по
     HTTP (первый — курсор, новые, ETag/304, сломанная лента) рядом с ядром;
  7. GitHub: поддельный API — проверка токена, первый опрос, новые с
     текстом комментария, 304, 401, адреса страниц, «прочитано»;
  8. Gmail и вход Google: письмо → событие, опрос (history, 404,
     непрочитанные, обновление токена), client JSON, PKCE, state, обмен
     кода, вставка адреса, свой сервер на 127.0.0.1;
  9. IMAP: поддельный сервер — опрос ядра (imaplib) и телефона дают те же
     события (кодированные заголовки, base64/QP, koi8-r/1251, вложения,
     рассылки, прочитанные), смена UIDVALIDITY, неверный пароль, нет папки;
 10. перенос: тело от телефона (SvcTransfer) → POST /api/integrations/import →
     ключи, курсоры, ленты, правила, лента событий; ядро не присылает второй
     раз виденное телефоном и присылает новое; компьютер главный (другой
     аккаунт — skipped); повтор переноса без дублей; проверка тела (422).

Сеть наружу, Ollama и настоящие аккаунты не трогаются: всё — на 127.0.0.1;
data/ — временный каталог.
Запуск: OLLAMA_URL=http://127.0.0.1:9 python3 -m scripts.test_phone_services
"""

import base64
import glob
import hashlib
import http.server
import imaplib
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_TMP = tempfile.mkdtemp(prefix="phone_services_")
os.environ["VPC_DATA_DIR"] = _TMP
os.environ["INTEGRATIONS_HUB"] = "0"
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")
os.environ.pop("TIMEZONE", None)  # пояс — системный, как у JVM
ROOT = Path(__file__).parent.parent
JAVA_SRC = ROOT / "web/android/app/src/main/java/io/vpcore/app"
CLASSES = ["SvcUtil", "SvcTriage", "SvcParse", "SvcSources", "SvcImap", "SvcGoogleLogin", "SvcTransfer"]

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


def reset_data():
    shutil.rmtree(Path(_TMP) / "integrations", ignore_errors=True)


# ── Java ────────────────────────────────────────────────────────────────

_BUILD = {"dir": None, "jar": None}


def json_jar():
    found = sorted(glob.glob(os.path.expanduser(
        "~/.gradle/caches/modules-2/files-2.1/org.json/json/*/*/json-*.jar")))
    return found[-1] if found else None


def build():
    jar = json_jar()
    if not jar or not shutil.which("javac"):
        return False, "нет javac или org.json (соберите APK один раз — Gradle скачает org.json)"
    out = Path(_TMP) / "classes"
    out.mkdir(exist_ok=True)
    srcs = [str(JAVA_SRC / f"{c}.java") for c in CLASSES] + [str(ROOT / "scripts/java/SvcHarness.java")]
    r = subprocess.run(["javac", "-encoding", "UTF-8", "--release", "11", "-cp", jar, "-d", str(out), *srcs],
                       capture_output=True, text=True)
    _BUILD.update(dir=str(out), jar=jar)
    return r.returncode == 0, r.stderr[-2000:]


def java(ops, timeout=120):
    """Операции обвязки → результаты (ошибка — {"__error", "__auth"})."""
    inp = Path(tempfile.mktemp(dir=_TMP, suffix=".json"))
    out = Path(tempfile.mktemp(dir=_TMP, suffix=".json"))
    inp.write_text(json.dumps(ops, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(["java", "-Dfile.encoding=UTF-8", "-cp", f"{_BUILD['dir']}{os.pathsep}{_BUILD['jar']}",
                        "io.vpcore.app.SvcHarness", str(inp), str(out)],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-2000:])
    return json.loads(out.read_text(encoding="utf-8"))


def j1(op):
    return java([op])[0]


def ev_dict(e):
    """Event ядра → словарь, как событие телефона."""
    return {"source": e.source, "key": e.key, "kind": e.kind, "title": e.title, "sender": e.sender,
            "text": e.text, "url": e.url, "ts": e.ts, "meta": e.meta, "group": e.group}


def same_events(py, jv, tol=1e-3):
    """Списки событий совпадают (время — с допуском)."""
    py = [ev_dict(e) if not isinstance(e, dict) else e for e in py]
    if len(py) != len(jv):
        print(f"      длина: ядро {len(py)}, телефон {len(jv)}")
        return False
    for a, b in zip(py, jv):
        for k in ("source", "key", "kind", "title", "sender", "text", "url", "meta", "group"):
            if a.get(k) != b.get(k):
                print(f"      {a.get('key')}: {k}: ядро {a.get(k)!r} ≠ телефон {b.get(k)!r}")
                return False
        if abs(float(a.get("ts") or 0) - float(b.get("ts") or 0)) > tol:
            print(f"      {a.get('key')}: ts {a.get('ts')} ≠ {b.get('ts')}")
            return False
    return True


# ── HTTP-сервер на 127.0.0.1 (ленты, GitHub, Google) ────────────────────

class Fake:
    """routes: (метод, путь без query) → функция(handler, query, body) → (код, заголовки, тело)."""

    def __init__(self):
        self.routes = {}
        self.calls = []
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def _do(self, method):
                u = urllib.parse.urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.calls.append((method, u.path, dict(self.headers), body, u.query))
                fn = fake.routes.get((method, u.path))
                if fn is None:
                    code, headers, out = 404, {}, b'{"message": "Not Found"}'
                else:
                    code, headers, out = fn(self, urllib.parse.parse_qs(u.query), body)
                if isinstance(out, (dict, list)):
                    out = json.dumps(out).encode()
                    headers = {"Content-Type": "application/json", **headers}
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(out or b"")))
                self.end_headers()
                if out and code != 304:
                    self.wfile.write(out)

            def do_GET(self):  # noqa: N802
                self._do("GET")

            def do_POST(self):  # noqa: N802
                self._do("POST")

            def log_message(self, *a):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# ── Данные ──────────────────────────────────────────────────────────────

NOW = time.time()


def _settings(**notify):
    from app.integrations import store
    s = store.normalize_settings({})
    s["notify"]["engine"] = "rules"
    s["notify"].update(notify)
    return store.normalize_settings(s)


def _events():
    from app.integrations.feed import Event

    def ev(source, key, kind, title="", sender="", text="", meta=None, group="", url=""):
        return Event(source=source, key=key, kind=kind, title=title, sender=sender, text=text, url=url,
                     ts=NOW - 60, meta=meta or {}, group=group)

    long = "Очень длинное сообщение " * 20
    return [
        ev("gmail", "g1", "message", "Счёт за октябрь", "Бухгалтерия", "Оплатите до пятницы",
           {"labels": ["INBOX", "UNREAD"], "email": "buh@corp.ru"}, "buh@corp.ru"),
        ev("gmail", "g2", "message", "Скидки 50%", "Ozon", "Только сегодня",
           {"labels": ["INBOX", "CATEGORY_PROMOTIONS"], "email": "news@ozon.ru"}),
        ev("gmail", "g3", "message", "Новый подписчик", "Twitter", "", {"labels": ["INBOX", "CATEGORY_SOCIAL"]}),
        ev("gmail", "g4", "message", "Your receipt", "Shop", "", {"labels": ["INBOX", "CATEGORY_UPDATES"],
                                                                 "email": "noreply@shop.com"}),
        ev("gmail", "g5", "message", "Win", "x", "", {"labels": ["SPAM"]}),
        ev("gmail", "g6", "message", "Привет", "Маша", "Как дела?", {"labels": ["INBOX"], "email": "masha@mail.ru"}),
        ev("gmail", "g7", "message", "Weekly", "Newsletter Team", "", {"labels": ["INBOX"],
                                                                      "email": "team@newsletter.io"}),
        ev("imap", "i1", "message", "Рассылка", "Shop", "акция", {"email": "shop@x.ru", "bulk": True}),
        ev("imap", "i2", "message", "Вопрос по курсовой", "Иван Петров", "Когда сдавать?",
           {"email": "ivan@uni.ru", "bulk": False}),
        ev("imap", "i3", "message", "Digest", "Daily", "", {"email": "digest@news.com"}),
        ev("github", "t1:2026", "review_requested", "Fix parser", "trinity", "Посмотри",
           {"reason": "review_requested", "repo": "me/vpc"}, "me/vpc"),
        ev("github", "t2", "ci_activity", "Build failed: main", "", "", {"reason": "ci_activity", "repo": "o/x"}, "o/x"),
        ev("github", "t3", "ci_activity", "Build succeeded", "", "", {"reason": "ci_activity"}),
        ev("github", "t4", "security_alert", "CVE in lodash", "", "", {"reason": "security_alert"}),
        ev("github", "t5", "subscribed", "Release 1.0", "", "", {"reason": "subscribed"}),
        ev("github", "t6", "update", "No reason", "", "", {}),
        ev("github", "t7", "ci_activity", "Сборка упала", "", "", {"reason": "ci_activity"}),
        ev("github", "t8", "mention", "Ping", "neo", "", {"reason": "mention", "repo": "a/b"}, "a/b"),
        ev("rss", "r1", "item", "Новая статья про агентов", "Блог", "Текст", {"feed": "Блог"}, "Блог"),
        ev("rss", "r2", "item", "Погода", "Новости", "", {"feed": "Новости"}, "Новости"),
        ev("drive", "d1", "shared", "Doc", "Anna"),
        ev("drive", "d2", "edited", "Doc", "Bob"),
        ev("telegram", "tg1", "channel", "", "Канал", "пост"),
        ev("telegram", "tg2", "private", "", "Маша", long),
        ev("telegram", "tg3", "group", "", "Петя", "сообщение", {"muted": True}, "Чат"),
        ev("telegram", "tg4", "mention", "", "Петя", "@me", {"muted": True, "mentioned": True}, "Чат"),
        ev("telegram", "tg5", "group", "", "Петя", "x", {}, "Чат"),
        ev("webhook", "w1", "event", "Backup failed", "", "disk   full", {"priority": "high"}),
        ev("webhook", "w2", "event", "Webhook", "", "ping"),
        ev("webhook", "w3", "event", "Deploy", "", "ok", {"priority": 5}),
        ev("webhook", "w4", "event", "x", "", "y", {"priority": "LOW"}),
        ev("webhook", "w5", "event", "", "", "plain " * 50),
    ]


RULES = [
    {"id": "aaaaaaaa01", "action": "never", "source": "any", "match": "Ozon"},
    {"id": "aaaaaaaa02", "action": "urgent", "source": "gmail", "match": "бухгалтерией"},
    {"id": "aaaaaaaa03", "action": "always", "source": "rss", "match": "агентов"},
    {"id": "aaaaaaaa04", "action": "digest", "source": "github", "match": "me/vpc"},
    {"id": "aaaaaaaa05", "action": "always", "source": "any", "match": "курсовой"},
    {"id": "aaaaaaaa06", "action": "urgent", "source": "webhook", "match": "disk"},
    {"id": "aaaaaaaa07", "action": "never", "source": "telegram", "match": "про погоду"},
]


# ── 1. Сборка ───────────────────────────────────────────────────────────

def test_build():
    section("1. Сборка классов телефона обычным javac")
    good, err = build()
    check("Svc*.java собираются без Android (только org.json)", good)
    if not good:
        print(err)
    srcs = "".join((JAVA_SRC / f"{c}.java").read_text(encoding="utf-8") for c in CLASSES)
    import re
    check("в портах нет java.time, java.util.Base64, String.join (их нет на Android 7)",
          not re.search(r"import\s+java\.time|java\.time\.[A-Z]|import\s+java\.util\.Base64|Base64\.get(De|En)coder"
                        r"|String\.join\(|\.chars\(\)", srcs))
    return good


# ── 2–5. Разбор, тексты, уведомитель, лента ─────────────────────────────

def test_triage():
    section("2. Разбор: правила и признаки — как triage.decide (engine rules)")
    from app.integrations import triage
    events = _events()
    items = [e.record() for e in events]
    for variant, settings in (
            ("без интересов", _settings()),
            ("с интересами", _settings(interests="агенты и роботы")),
    ):
        settings["rules"] = [dict(r) for r in RULES]
        py = triage.decide(items, settings, llm=None)
        jv = j1({"op": "decide", "items": items, "settings": settings})
        diff = [i for i in py if py[i] != jv.get(i)]
        for i in diff[:3]:
            print(f"      {i}: ядро {py[i]} ≠ телефон {jv.get(i)}")
        check(f"решения по {len(items)} событиям совпадают ({variant})", not diff and len(py) == len(jv))
    for mode in ("digest", "ignore", "normal"):
        s = _settings()
        s["services"]["telegram"]["muted"] = mode
        tg = [it for it in items if it["source"] == "telegram"]
        ops = [{"op": "heuristic", "item": it, "settings": s} for it in tg]
        res = java(ops)
        check(f"Telegram «без звука» = {mode}: признаки совпадают",
              all(list(triage.heuristic(it, s)) == r for it, r in zip(tg, res)))
    s = _settings()
    ops = [{"op": "heuristic", "item": it, "settings": s} for it in items]
    res = java(ops)
    bad = [it["id"] for it, r in zip(items, res) if list(triage.heuristic(it, s)) != r]
    check("признаки каждого источника совпадают", not bad)
    rules = [dict(r) for r in RULES]
    res = java([{"op": "match", "item": it, "rules": rules} for it in items])
    bad = [it["title"] for it, r in zip(items, res)
           if ((triage.match_rule(it, rules) or {}).get("id")) != r]
    check("какое правило совпало — то же", not bad)
    words = ["ивановой", "счета", "агентов", "кот", "бухгалтерией", "releases", "ёжиком", "скидках", "ozon"]
    from app.core.word_stem import stem
    res = java([{"op": "stem", "word": w} for w in words])
    check("основы слов (word_stem) совпадают", res == [stem(w) for w in words])
    res = java([{"op": "summary", "item": it} for it in items])
    check("пересказ без модели (fallback_summary) совпадает", res == [triage.fallback_summary(it) for it in items])


def _texts_items():
    base = [e.record() for e in _events()]
    by = {it["id"]: it for it in base}
    out = []
    for i, it in enumerate(base):
        it = dict(it)
        it["level"] = "urgent" if i % 7 == 0 else "notify"
        it["summary"] = "" if i % 5 == 0 else f"Пересказ {i}"
        it["url"] = f"https://x.example/{i}" if i % 3 == 0 else ""
        it["received"] = NOW - 300 + i
        out.append(it)
    return out, by


def test_texts():
    section("3. Тексты, тихие часы, слоты сводки — как notifier.py")
    from app.integrations import notifier
    items, _ = _texts_items()
    cases = [items[:1], items[1:2], items[:3], items[3:6], items[:12], items, [items[22], items[24]],
             [items[10], items[18]]]
    ops = []
    for lang in ("ru", "en"):
        for c in cases:
            ops.append({"op": "notification", "items": c, "lang": lang})
            ops.append({"op": "digest", "items": c, "lang": lang})
    res = java(ops)
    want = []
    for lang in ("ru", "en"):
        for c in cases:
            want.append(notifier.compose_notification(c, lang))
            want.append(notifier.compose_digest(c, lang))
    bad = [i for i, (a, b) in enumerate(zip(want, res)) if a != b]
    for i in bad[:2]:
        print(f"      ядро:\n{want[i]}\n      телефон:\n{res[i]}")
    check(f"уведомления и сводки ru/en ({len(want)} текстов) совпадают", not bad)
    for n in (1, 2, 5, 11, 21, 22, 104, 111, 112):
        many = [dict(items[0], id=f"{k:016x}") for k in range(n)]
        r = j1({"op": "digest", "items": many, "lang": "ru"})
        if r != notifier.compose_digest(many, "ru"):
            check(f"склонение «событие» для {n}", False)
            break
    else:
        check("склонение «событие/события/событий» совпадает", True)

    day = time.localtime(NOW)
    base = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1))
    quiet_cases = []
    for frm, to in (("23:00", "08:00"), ("09:00", "18:00"), ("07:00", "07:00"), ("00:00", "06:30")):
        for hour in (0, 3, 7, 8, 12, 17.99, 18, 22.5, 23.5):
            s = _settings(quiet={"enabled": True, "from": frm, "to": to, "urgent": True})
            quiet_cases.append((s, base + hour * 3600))
    s = _settings(quiet={"enabled": False, "from": "00:00", "to": "23:59", "urgent": True})
    quiet_cases.append((s, base + 3600 * 5))
    res = java([{"op": "quiet", "settings": s, "now": t} for s, t in quiet_cases])
    check("тихие часы (через полночь, днём, пустые, выключены) совпадают",
          res == [notifier.in_quiet(s, t) for s, t in quiet_cases])
    slot_cases = []
    for times in (["09:00", "19:00"], ["07:30"], [], ["23:59", "00:00"]):
        for hour in (0.5, 7.5, 8.99, 9, 12, 19.5, 23.99):
            slot_cases.append((_settings(digest_times=times), base + hour * 3600))
    res = java([{"op": "slot", "settings": s, "now": t} for s, t in slot_cases])
    want = [notifier.last_digest_slot(s, t) for s, t in slot_cases]
    check("последний слот сводки (сегодня/вчера) совпадает",
          all((a is None and b is None) or (a is not None and b is not None and abs(a - b) < 1e-6)
              for a, b in zip(want, res)))


def _write_feed(records):
    from app.core.atomic_io import atomic_write_json
    from app.integrations.feed import feed
    feed.path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(feed.path, {"items": records, "seen": [r["id"] for r in records]})


def test_tick():
    section("4. Уведомитель: тик — как Notifier.tick")
    from app.integrations import notifier, store
    from app.integrations.feed import feed
    items, _ = _texts_items()
    day = time.localtime(NOW)
    noon = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 12, 0, 0, 0, 0, -1))

    def q(i, level, status="queued", ts=None, received=None):
        it = dict(items[i])
        it.update(level=level, status=status, ts=ts if ts is not None else noon - 120,
                  received=received if received is not None else noon - 300)
        return it

    scenarios = [
        ("обычное и срочное днём", _settings(), [q(0, "notify"), q(1, "urgent"), q(2, "notify")], {}, noon, []),
        ("тихие часы: только срочное", _settings(quiet={"enabled": True, "from": "11:00", "to": "13:00",
                                                         "urgent": True}),
         [q(0, "notify"), q(1, "urgent")], {}, noon, []),
        ("тихие часы без срочного", _settings(quiet={"enabled": True, "from": "11:00", "to": "13:00",
                                                      "urgent": False}),
         [q(0, "notify"), q(1, "urgent")], {}, noon, []),
        ("лимит в час исчерпан", _settings(max_per_hour=2), [q(0, "notify"), q(1, "urgent")],
         {"sent": [noon - 100, noon - 200, noon - 4000]}, noon, []),
        ("сводка по расписанию", _settings(digest_times=["09:00", "11:30"]),
         [q(3, "digest", "digest"), q(4, "digest", "digest"), q(5, "notify")], {"last_digest": noon - 86400}, noon, []),
        ("сводка уже была", _settings(digest_times=["09:00"]), [q(3, "digest", "digest")],
         {"last_digest": noon - 3600}, noon, []),
        ("прочитано в сервисе", _settings(), [q(0, "notify"), q(10, "notify"), q(11, "urgent")], {}, noon,
         [items[10]["id"]]),
        ("ничего не ждёт, слот прошёл", _settings(digest_times=["10:00"]), [q(6, "ignore", "ignored")],
         {}, noon, []),
    ]
    dnd = _settings()
    dnd["dnd_until"] = noon + 600
    scenarios.append(("«не беспокоить»", dnd, [q(0, "notify"), q(1, "urgent")], {}, noon, []))
    for name, settings, recs, nstate, now, gone in scenarios:
        reset_data()
        store.update_settings(lambda s, st=settings: s.update(st))
        store.update_state("notifier", dict(nstate), replace=True)
        _write_feed([dict(r) for r in recs])
        sent = []

        def deliver(text, its, kind):
            sent.append({"text": text, "ids": [i["id"] for i in its], "kind": kind})
            return True

        n = notifier.Notifier(deliver, relevant=lambda batch, g=gone: {b["id"] for b in batch if b["id"] in g},
                              lang=lambda: "ru")
        n.tick(now)
        py_status = {it["id"]: it["status"] for it in feed.items()}
        py_n = store.load_state("notifier")
        jv = j1({"op": "tick", "settings": settings, "feed": {"items": [dict(r) for r in recs], "seen": []},
                 "nstate": dict(nstate), "now": now, "lang": "ru", "gone": gone})
        jv_status = {it["id"]: it["status"] for it in jv["feed"]["items"]}
        same = (sent == jv["deliveries"] and py_status == jv_status
                and float(py_n.get("last_digest") or 0) == float(jv["nstate"].get("last_digest") or 0)
                and len(py_n.get("sent") or []) == len(jv["nstate"].get("sent") or []))
        if not same:
            print(f"      ядро: {sent} {py_status} {py_n}\n      телефон: {jv['deliveries']} {jv_status} {jv['nstate']}")
        check(f"{name}: доставки, статусы и состояние совпадают", same)

    reset_data()
    recs = [items[0], items[1], items[2]]
    decisions = {recs[0]["id"]: {"level": "notify", "summary": "a", "reason": "r", "by": "signals"},
                 recs[1]["id"]: {"level": "urgent", "summary": "b", "reason": "", "by": "rule"},
                 recs[2]["id"]: {"level": "ignore", "summary": "c", "reason": "x", "by": "signals"}}
    old = [dict(r, ts=NOW - 7 * 3600) for r in recs]
    _write_feed([dict(r) for r in old])
    notifier.Notifier(lambda *a: True).accept(decisions, old, NOW)
    py = {it["id"]: (it["level"], it["status"], it["reason"]) for it in feed.items()}
    jv = {it["id"]: (it["level"], it["status"], it["reason"])
          for it in j1({"op": "accept", "decisions": decisions, "items": old, "now": NOW})}
    check("устаревшее (ядро/телефон долго молчали) — в сводку с пометкой stale", py == jv)


def test_feed_settings():
    section("5. Лента и настройки — как feed.py и store.py")
    from app.integrations import store
    from app.integrations.feed import Event, event_id
    events = _events()
    res = java([{"op": "record", "event": ev_dict(e), "now": NOW} for e in events])
    good = True
    for e, r in zip(events, res):
        want = e.record()
        for k in ("received",):
            want.pop(k)
            r.pop(k)
        if want != r:
            good = False
            print(f"      {e.key}: {want} ≠ {r}")
            break
    check("запись ленты (id, обрезка, поля) совпадает", good)
    long_ev = Event(source="imap", key="k", kind="message", title="Т" * 400, sender=" s " * 100,
                    text="текст😀" * 500, url="u" * 1200, group="g" * 300)
    r = j1({"op": "record", "event": ev_dict(long_ev), "now": NOW})
    want = long_ev.record()
    check("обрезка «…» по символам (и эмодзи)", all(r[k] == want[k] for k in ("title", "sender", "text", "url", "group")))
    check("id события = feed.event_id", j1({"op": "event_id", "source": "rss", "key": "abc"}) == event_id("rss", "abc"))
    payload = [ev_dict(e) for e in events[:5]]
    r = j1({"op": "ingest", "feed": {"items": [], "seen": [events[0].id]}, "events": payload + payload[:2], "now": NOW})
    check("повтор по id и «уже видел» — не добавляются", [a["id"] for a in r["added"]] == [e.id for e in events[1:5]])

    raw = {"notify": {"quiet": {"from": "9:5", "to": "25:00"}, "digest_times": ["19:00", "9:00", "x", "09:00"],
                      "max_per_hour": 0, "language": "de", "persona": "con nor"},
           "services": {"rss": {"feeds": ["https://a.example/f", {"url": "ftp://x"}, {"url": "https://a.example/f"},
                                          {"url": "https://b.example/f", "title": "B" * 200}]}},
           "rules": [{"action": "never", "match": "  a   b  ", "source": "zzz", "id": "abcdef0123"},
                     {"action": "bad", "match": "x"}, {"action": "digest", "match": ""}, "junk"]}
    py = store.normalize_settings(json.loads(json.dumps(raw)))
    jv = j1({"op": "normalize", "settings": raw})
    same = (py["notify"]["quiet"] == jv["notify"]["quiet"] and py["notify"]["digest_times"] == jv["notify"]["digest_times"]
            and py["notify"]["max_per_hour"] == jv["notify"]["max_per_hour"]
            and py["notify"]["language"] == jv["notify"]["language"]
            and py["services"]["rss"]["feeds"] == jv["services"]["rss"]["feeds"]
            and [(r["id"], r["action"], r["source"], r["match"]) for r in py["rules"]]
            == [(r["id"], r["action"], r["source"], r["match"]) for r in jv["rules"]])
    check("нормализация настроек и правил совпадает", same)
    check("непригодный id персоны отброшен, интервал — 15/30 мин",
          jv["notify"]["persona"] is None and jv["interval_min"] == 15
          and j1({"op": "normalize", "settings": {"interval_min": 25}})["interval_min"] == 15
          and j1({"op": "normalize", "settings": {"interval_min": 30}})["interval_min"] == 30)


# ── 6. RSS ──────────────────────────────────────────────────────────────

RSS1 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>  Блог &amp; заметки </title><link>https://b.example/</link>
<item><title>Первая</title><link>https://b.example/1</link><guid isPermaLink="false">g1</guid>
<description><![CDATA[<p>Привет,&nbsp;мир! <b>жирно</b><br/>строка 2</p><script>alert(1)</script>
<style>p{}</style>

<p>   </p><p>ещё</p>]]></description>
<pubDate>Mon, 19 Oct 2026 10:00:00 +0300</pubDate></item>
<item><title>Вторая</title><link>https://b.example/2</link><content:encoded><![CDATA[<div>Полный &laquo;текст&raquo; &#8212; &#x41;BC &copy;2026 &unknown; &amp</div>]]></content:encoded><dc:date>2026-10-18T09:30:00Z</dc:date></item>
<item><title>Без ссылки</title><description>просто текст &lt;b&gt;жир&lt;/b&gt;</description><pubDate>19 Oct 2026 08:00 GMT</pubDate></item>
<item><description>нет ни id, ни заголовка</description></item>
<item><title>RFC 850</title><guid>g5</guid><pubDate>Monday, 19-Oct-26 10:00:00 GMT</pubDate></item>
</channel></rss>""".encode("utf-8")

ATOM1 = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Releases</title>
<entry><id>tag:github.com,2008:Repository/1/v1.2</id><title>v1.2</title>
<link rel="enclosure" href="https://g.example/file.zip"/><link href="https://g.example/v1.2"/>
<updated>2026-10-19T12:00:00+02:00</updated><content type="html">&lt;p&gt;Fixes &amp;amp; more&lt;/p&gt;</content></entry>
<entry><title>No id</title><link rel="alternate" href="https://g.example/x"/><published>2026-10-19T12:00:00.123Z</published><summary>Short</summary></entry>
<entry><title type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">Rich</div></title><id>e3</id><content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>X</p></div></content></entry>
</feed>"""

RDF = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="x"><title>RDF feed</title></channel>
<item rdf:about="https://r.example/1"><title>R1</title><link>https://r.example/1</link><dc:date>2026-10-19</dc:date></item></rdf:RDF>"""

DOCTYPE = b"""<?xml version="1.0"?><!DOCTYPE rss PUBLIC "-//Netscape Communications//DTD RSS 0.91//EN" "http://my.netscape.com/publish/formats/rss-0.91.dtd">
<rss version="0.91"><channel><title>Old</title><item><title>T</title><link>https://o.example/1</link></item></channel></rss>"""

WIN1251 = '<?xml version="1.0" encoding="windows-1251"?><rss><channel><title>Кириллица</title><item><title>Заголовок</title><guid>w1</guid></item></channel></rss>'.encode("cp1251")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_rss_parse():
    section("6а. RSS/Atom: разбор — как rss.parse_feed")
    from app.integrations.sources import rss, SourceError
    from app.integrations.sources.google import strip_html
    import html as html_mod
    for name, data in (("RSS 2.0", RSS1), ("Atom", ATOM1), ("RDF", RDF), ("DOCTYPE", DOCTYPE), ("windows-1251", WIN1251)):
        title, items = rss.parse_feed(data)
        jv = j1({"op": "feed", "b64": b64(data)})
        same = jv.get("title") == title and len(jv.get("items") or []) == len(items) and all(
            a["id"] == b["id"] and a["title"] == b["title"] and a["link"] == b["link"]
            and a["summary"] == b["summary"] and abs(a["ts"] - b["ts"]) < 1e-3
            for a, b in zip(items, jv["items"]))
        if not same:
            print(f"      ядро: {title!r} {items}\n      телефон: {jv}")
        check(f"{name}: название и записи совпадают", same)
    for bad in (b"<html><body>not xml", b"", b"\xff\xfe junk"):
        try:
            rss.parse_feed(bad)
            py_err = False
        except SourceError:
            py_err = True
        jv = j1({"op": "feed", "b64": b64(bad)})
        check(f"не XML ({bad[:12]!r}) — ошибка и у ядра, и у телефона", py_err and "__error" in jv)
    texts = [
        "a &amp; b &lt;c&gt; &quot;d&quot; &#39;e&#39; &nbsp;f &mdash; g &hellip; &laquo;h&raquo; &#8212; &#x2014;",
        "&#128; &#0; &#1; &copy2026 &ampx &unknown; &AMP; &euro; &notit; &#xD800; &#1114112; &Auml;&eacute;",
        "no entities at all", "&", "&#", "&#x;", "&lt", "AT&T",
    ]
    res = java([{"op": "unescape", "text": t} for t in texts])
    check("html.unescape: сущности, числовые, без «;», битые", res == [html_mod.unescape(t) for t in texts])
    htmls = [
        "<p>Hello</p><p>World</p>", "<div>A<br>B<BR/>C<br />D</div>", "<head><title>x</title></head>body",
        "<SCRIPT type='x'>var a=1;</SCRIPT>text<style>.a{}</style>", "a\t\tb  \r\n\n\n   \n c",
        "<li>one</li><li>two</li>\n\n \n<p>&nbsp;</p>", "  <b>bold</b>  ", "",
    ]
    res = java([{"op": "strip_html", "text": t} for t in htmls])
    bad = [(t, a, b) for t, a, b in zip(htmls, [strip_html(t) for t in htmls], res) if a != b]
    for t, a, b in bad[:2]:
        print(f"      {t!r}: ядро {a!r} ≠ телефон {b!r}")
    check("strip_html совпадает", not bad)
    dates = ["Mon, 19 Oct 2026 10:00:00 +0300", "19 Oct 2026 10:00 GMT", "Mon, 19 Oct 2026 10:00:00",
             "Monday, 19-Oct-26 10:00:00 GMT", "2026-10-19T10:00:00Z", "2026-10-19T10:00:00.5+03:00",
             "2026-10-19", "2026-10-19 10:00:00", "garbage", "", "Tue, 31 Feb 2026 10:00:00 GMT",
             "19 Oct 2026 10:00:00 EST", "Mon, 19 Oct 2026 10:00:00 -0000", "20261019T100000Z",
             "Oct 19 2026 10:00:00 +0000", "19 Oct 99 10:00:00 +0000", "2026-10-19T10:00"]
    res = java([{"op": "date", "text": d} for d in dates])
    bad = [(d, rss._date(d), r) for d, r in zip(dates, res) if abs(rss._date(d) - r) > 1e-3]
    for d, a, b in bad[:3]:
        print(f"      {d!r}: ядро {a} ≠ телефон {b}")
    check("даты лент (RFC 2822, RFC 850, ISO, без пояса, битые) совпадают", not bad)
    check("ключ записи ленты = rss._key",
          j1({"op": "rss_key", "url": "https://a.example/f", "id": "g1"}) == rss._key("https://a.example/f", "g1"))


def test_rss_poll(fake):
    section("6б. RSS: опрос по HTTP рядом с ядром")
    from app.integrations import store
    from app.integrations.sources import rss, SourceError
    from app.integrations import sources
    sources._transport = None
    reset_data()
    feed_state = {"body": RSS1, "etag": '"e1"'}

    def serve(h, q, body):
        if h.headers.get("If-None-Match") == feed_state["etag"]:
            return 304, {}, b""
        return 200, {"ETag": feed_state["etag"], "Last-Modified": "Mon, 19 Oct 2026 10:00:00 GMT"}, feed_state["body"]

    fake.routes[("GET", "/feed.xml")] = serve
    fake.routes[("GET", "/broken.xml")] = lambda h, q, b: (500, {}, b"oops")
    url = fake.base + "/feed.xml"
    feeds = [{"url": url, "title": "Мой блог"}]
    store.update_settings(lambda s: s["services"]["rss"].update(feeds=feeds, enabled=True))
    src = rss.RssSource()
    py1 = src.poll()
    jv1 = j1({"op": "rss_poll", "feeds": feeds, "state": {}, "now": NOW})
    py_state = store.load_state("rss")["feeds"][url]
    jv_state = jv1["state"]["feeds"][url]
    check("первый опрос — без событий, «уже видел» те же ключи",
          py1 == [] and jv1["events"] == [] and py_state["seen"] == jv_state["seen"] and jv_state["seen_init"])
    feed_state["body"] = RSS1.replace(b"<item><title>\xd0\x9f\xd0\xb5\xd1\x80\xd0\xb2\xd0\xb0\xd1\x8f",
                                      b"<item><title>New one</title><guid>n1</guid><link>https://b.example/n1</link>"
                                      b"<description>&lt;p&gt;fresh&lt;/p&gt;</description></item>"
                                      b"<item><title>\xd0\x9f\xd0\xb5\xd1\x80\xd0\xb2\xd0\xb0\xd1\x8f", 1)
    feed_state["etag"] = '"e2"'
    py2 = src.poll()
    jv2 = j1({"op": "rss_poll", "feeds": feeds, "state": jv1["state"], "now": NOW})
    check("новая запись — одно и то же событие", len(py2) == 1 and same_events(py2, jv2["events"], tol=1e9))
    check("ETag и Last-Modified запомнены", jv2["state"]["feeds"][url]["etag"] == '"e2"'
          and store.load_state("rss")["feeds"][url]["etag"] == '"e2"')
    py3 = src.poll()
    jv3 = j1({"op": "rss_poll", "feeds": feeds, "state": jv2["state"], "now": NOW})
    check("без новостей — 304, событий нет", py3 == [] and jv3["events"] == []
          and fake.calls[-1][2].get("If-None-Match") == '"e2"')
    feeds2 = feeds + [{"url": fake.base + "/broken.xml", "title": ""}]
    jv4 = j1({"op": "rss_poll", "feeds": feeds2, "state": jv3["state"], "now": NOW})
    check("одна лента сломана — остальные работают, ошибка в состоянии",
          jv4["events"] == [] and "HTTP 500" in jv4["state"].get("partial_error", ""))
    jv5 = j1({"op": "rss_poll", "feeds": [{"url": fake.base + "/broken.xml"}], "state": {}, "now": NOW})
    store.update_settings(lambda s: s["services"]["rss"].update(feeds=[{"url": fake.base + "/broken.xml"}]))
    try:
        rss.RssSource().poll()
        py_err = False
    except SourceError:
        py_err = True
    check("все ленты сломаны — ошибка источника у обоих", py_err and "HTTP 500" in jv5.get("__error", ""))
    r = j1({"op": "rss_check", "url": url})
    check("«Проверить» ленту: название и число записей", r.get("ok") and r.get("title") == "Блог & заметки"
          and r.get("items") == 5)
    return jv3["state"]


# ── 7. GitHub ───────────────────────────────────────────────────────────

GH_TOKEN = "ghp_" + "a" * 36


def _threads(base):
    return [
        {"id": "101", "reason": "review_requested", "updated_at": "2026-10-19T10:00:00Z",
         "subject": {"title": "Fix parser", "url": "https://api.github.com/repos/me/vpc/pulls/7",
                     "latest_comment_url": f"{base}/gh/comments/1", "type": "PullRequest"},
         "repository": {"full_name": "me/vpc"}},
        {"id": "102", "reason": "ci_activity", "updated_at": "2026-10-19T11:00:00Z",
         "subject": {"title": "CI failed", "url": None, "latest_comment_url": None, "type": "CheckSuite"},
         "repository": {"full_name": "me/vpc"}},
        {"id": 103, "reason": "subscribed", "updated_at": "2026-10-19T09:00:00Z",
         "subject": {"title": "Release v2", "url": "https://api.github.com/repos/o/r/releases/55", "type": "Release"},
         "repository": {"full_name": "o/r"}},
        {"id": "104", "reason": "mention", "updated_at": "2026-01-01T00:00:00Z",
         "subject": {"title": "Old", "type": "Issue"}, "repository": {"full_name": "me/vpc"}},
        {"id": "105", "reason": "security_alert", "updated_at": "2026-10-19T12:00:00Z",
         "subject": {"title": "Vuln", "type": "RepositoryVulnerabilityAlert"}, "repository": {"full_name": "me/vpc"}},
        {"id": "106", "reason": "comment", "updated_at": "2026-10-19T12:30:00Z",
         "subject": {"title": "Issue", "url": "https://api.github.com/repos/me/vpc/issues/9",
                     "latest_comment_url": "https://api.github.com/repos/me/vpc/issues/9", "type": "Issue"},
         "repository": {"full_name": "me/vpc"}},
        {"id": "107", "reason": "author", "updated_at": "2026-10-19T12:40:00Z",
         "subject": {"title": "Disc", "type": "Discussion", "latest_comment_url": f"{base}/gh/comments/404"},
         "repository": {"full_name": "a/b"}},
    ]


def test_github(fake):
    section("7. GitHub: поддельный API рядом с ядром")
    from app.integrations import store
    from app.integrations.sources import AuthError
    from app.integrations.sources import github as gh_src
    reset_data()
    api = fake.base + "/gh"
    gh_src.API = api
    state = {"threads": _threads(fake.base), "lm": "Mon, 19 Oct 2026 12:40:00 GMT"}

    def auth(h):
        return h.headers.get("Authorization") == f"Bearer {GH_TOKEN}"

    def user(h, q, b):
        if not auth(h):
            return 401, {}, {"message": "Bad credentials"}
        return 200, {"X-OAuth-Scopes": "notifications, repo"}, {"login": "neo", "name": "Neo"}

    def notes(h, q, b):
        if not auth(h):
            return 401, {}, {"message": "Bad credentials"}
        if q.get("per_page") == ["1"]:
            return 200, {}, []
        if h.headers.get("If-Modified-Since") == state["lm"]:
            return 304, {"X-Poll-Interval": "60"}, b""
        return 200, {"Last-Modified": state["lm"], "X-Poll-Interval": "120"}, state["threads"]

    fake.routes[("GET", "/gh/user")] = user
    fake.routes[("GET", "/gh/notifications")] = notes
    fake.routes[("GET", "/gh/comments/1")] = lambda h, q, b: (200, {}, {
        "user": {"login": "trinity"}, "body": "Посмотри, пожалуйста " * 100,
        "html_url": "https://github.com/me/vpc/pull/7#issuecomment-1"})
    fake.routes[("GET", "/gh/notifications/threads/102")] = lambda h, q, b: (200, {}, {"unread": False})
    fake.routes[("GET", "/gh/notifications/threads/105")] = lambda h, q, b: (200, {}, {"unread": True})

    store.update_secrets("github", {"token": GH_TOKEN})
    py_check = gh_src.GitHubSource().check(GH_TOKEN)
    jv_check = j1({"op": "github_check", "github_api": api, "token": GH_TOKEN})
    check("проверка токена: логин и права", py_check == jv_check)
    bad = j1({"op": "github_check", "github_api": api, "token": "ghp_" + "b" * 36})
    check("чужой токен — «доступ отозван» (AuthError)", bad.get("__auth") is True)
    jv0 = j1({"op": "github_poll", "github_api": api, "token": GH_TOKEN, "state": {}, "now": NOW})
    check("первый опрос — только курсор «с этого момента»", jv0["events"] == [] and abs(jv0["state"]["since"] - NOW) < 1)
    since = 1777777777.0  # 2026-05-03
    store.update_state("github", {"since": since}, replace=True)
    src = gh_src.GitHubSource()
    py = src.poll()
    jv = j1({"op": "github_poll", "github_api": api, "token": GH_TOKEN, "state": {"since": since}, "now": NOW})
    check("новые уведомления — те же события (комментарий, адреса, причины)", len(py) == 6 and same_events(py, jv["events"]))
    py_st = store.load_state("github")
    check("курсор и Last-Modified — те же", abs(py_st["since"] - jv["state"]["since"]) < 1e-6
          and py_st["last_modified"] == jv["state"]["last_modified"] and jv["state"]["poll_interval"] == 120)
    jv2 = j1({"op": "github_poll", "github_api": api, "token": GH_TOKEN, "state": jv["state"], "now": NOW})
    check("без новостей — 304 по If-Modified-Since", jv2["events"] == [] and src.poll() == [])
    revoked = j1({"op": "github_poll", "github_api": api, "token": "ghp_" + "c" * 36, "state": {"since": since}})
    try:
        store.update_secrets("github", {"token": "ghp_" + "c" * 36})
        gh_src.GitHubSource().poll()
        py_auth = False
    except AuthError:
        py_auth = True
    check("отозванный токен — AuthError у обоих", py_auth and revoked.get("__auth") is True)
    store.update_secrets("github", {"token": GH_TOKEN})
    items = [e.record() for e in py]
    jv_gone = set(j1({"op": "github_gone", "github_api": api, "token": GH_TOKEN, "items": items}))
    check("«прочитано на GitHub» — те же", jv_gone == gh_src.GitHubSource().gone(items) and len(jv_gone) == 1)
    subjects = [({"url": "https://api.github.com/repos/a/b/pulls/1"}, "a/b"),
                ({"url": "https://api.github.com/repos/a/b/issues/2"}, "a/b"),
                ({"url": "https://api.github.com/repos/a/b/commits/abc"}, "a/b"),
                ({"url": "https://api.github.com/repos/a/b/releases/9"}, "a/b"),
                ({"type": "CheckSuite"}, "a/b"), ({"type": "Discussion"}, "a/b"), ({}, ""), ({}, "x/y")]
    res = java([{"op": "html_url", "subject": s, "repo": r} for s, r in subjects])
    check("адреса страниц github.com — те же", res == [gh_src.html_url(s, r) for s, r in subjects])
    tokens = [GH_TOKEN, "github_pat_" + "A1_" * 10, "ghx_" + "a" * 36, "ghp_short", ""]
    from app.integrations.sources.github import valid_token
    check("проверка формата токена — как у ядра", [valid_token(t) for t in tokens] == [True, True, False, False, False])
    return jv["state"]


# ── 8. Gmail и вход Google ──────────────────────────────────────────────

def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")


def _gmail_msgs():
    def msg(mid, labels, headers, payload_parts=None, body=None, snippet="snip", thread=None, internal="1760860800000"):
        payload = {"mimeType": "multipart/alternative" if payload_parts else "text/plain",
                   "headers": [{"name": k, "value": v} for k, v in headers]}
        if payload_parts:
            payload["parts"] = payload_parts
        elif body is not None:
            payload["body"] = {"data": _b64url(body)}
        out = {"id": mid, "labelIds": labels, "payload": payload, "snippet": snippet, "internalDate": internal}
        if thread:
            out["threadId"] = thread
        return out

    return [
        msg("m1", ["INBOX", "UNREAD", "IMPORTANT"], [("From", "Бухгалтерия <buh@corp.ru>"), ("Subject", "Счёт"),
                                                     ("Date", "Mon, 19 Oct 2026 10:00:00 +0300")],
            [{"mimeType": "text/plain", "body": {"data": _b64url("Оплатите до пятницы.\nСпасибо\n> старая цитата")}},
             {"mimeType": "text/html", "body": {"data": _b64url("<p>html</p>")}}], thread="t1"),
        msg("m2", ["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"], [("From", "news@shop.com"), ("Subject", "  Sale  "),
                                                               ("List-Unsubscribe", "<mailto:u@x>")],
            [{"mimeType": "multipart/related", "parts": [
                {"mimeType": "text/html", "body": {"data": _b64url("<div>Скидки&nbsp;<b>50%</b><br>Только сегодня</div>")}}]}]),
        msg("m3", ["INBOX"], [("From", '"Ivanov, Ivan" <ivan@uni.ru>'), ("Subject", ""),
                              ("Date", "Tue, 20 Oct 2026 11:00:00 +0000")],
            body="Hi\n\nOn Mon, 19 Oct 2026 at 10:00 Ivan wrote:\n> old", internal="0"),
        msg("m4", ["SENT"], [("From", "me@gmail.com"), ("Subject", "sent")], body="x"),
        msg("m5", ["INBOX", "UNREAD", "DRAFT"], [("From", "me@gmail.com")], body="draft"),
        msg("m6", ["INBOX", "UNREAD"], [("From", "Маша <masha@mail.ru>"), ("Subject", "Привет"),
                                        ("Precedence", "bulk")], body="", snippet="только сниппет"),
        msg("m7", ["INBOX", "UNREAD"], [("from", "lower@case.ru"), ("SUBJECT", "Регистр")],
            body="Текст\n19.10.2026 10:00 Иван пишет:\n> цитата"),
    ]


def test_gmail(fake):
    section("8. Gmail и вход Google — как google.py")
    from app.integrations import store
    from app.integrations.sources import google as google_src
    reset_data()
    store.update_secrets("google", {"email": "me@gmail.com"})
    msgs = _gmail_msgs()
    res = java([{"op": "gmail_event", "msg": m, "account": "me@gmail.com"} for m in msgs])
    good = True
    for m, r in zip(msgs, res):
        e = google_src.message_event(m)
        if (e is None) != (r is None) or (e is not None and not same_events([e], [r])):
            good = False
            print(f"      {m['id']}: ядро {e and ev_dict(e)} ≠ телефон {r}")
    check("письма → события: тело, цитата, метки, без темы, черновики/отправленные — как у ядра", good)

    # Опрос: поддельные Gmail API и токены
    tokens = {"refresh": 0, "codes": []}
    sec_base = {"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec", "refresh_token": "rt-1",
                "email": "me@gmail.com", "scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}
    served = {m["id"]: m for m in msgs}
    served["m8"] = dict(msgs[0], id="m8", labelIds=["INBOX"])  # прочитано раньше, чем увидели

    def token(h, q, body):
        form = urllib.parse.parse_qs(body.decode())
        if form.get("grant_type") == ["refresh_token"]:
            tokens["refresh"] += 1
            if form.get("refresh_token") != ["rt-1"]:
                return 400, {}, {"error": "invalid_grant"}
            return 200, {}, {"access_token": "at-2", "expires_in": 3600}
        tokens["codes"].append(form)
        if form.get("code") == ["no-refresh"]:
            return 200, {}, {"access_token": "x"}
        idt = "h." + base64.urlsafe_b64encode(json.dumps({"email": "me@gmail.com"}).encode()).decode().rstrip("=") + ".s"
        return 200, {}, {"access_token": "at-1", "refresh_token": "rt-new", "expires_in": 3600, "id_token": idt,
                         "scope": "openid https://www.googleapis.com/auth/gmail.readonly email"}

    def authed(h):
        return h.headers.get("Authorization") == "Bearer at-2"

    def profile(h, q, b):
        return (200, {}, {"emailAddress": "me@gmail.com", "historyId": "500", "messagesTotal": 7}) if authed(h) \
            else (401, {}, {"error": {"message": "bad"}})

    def history(h, q, b):
        if not authed(h):
            return 401, {}, {}
        start = q.get("startHistoryId", [""])[0]
        if start == "1":
            return 404, {}, {"error": {"message": "too old"}}
        if q.get("pageToken"):
            return 200, {}, {"history": [{"messagesAdded": [{"message": {"id": "m6"}}, {"message": {"id": "m9"}}]}],
                             "historyId": "620"}
        return 200, {}, {"history": [{"messagesAdded": [{"message": {"id": "m1"}}, {"message": {"id": "m2"}}]},
                                     {"messagesAdded": [{"message": {"id": "m1"}}, {"message": {"id": "m8"}}]}],
                         "historyId": "610", "nextPageToken": "p2"}

    def message(h, q, b):
        mid = h.path.split("?")[0].rsplit("/", 1)[-1]
        if mid not in served:
            return 404, {}, {"error": {"message": "Not Found"}}
        return 200, {}, served[mid]

    fake.routes[("POST", "/token")] = token
    fake.routes[("GET", "/gmail/profile")] = profile
    fake.routes[("GET", "/gmail/history")] = history
    for mid in list(served) + ["m9"]:
        fake.routes[("GET", f"/gmail/messages/{mid}")] = message
    google_src.GMAIL = fake.base + "/gmail"
    google_src.TOKEN_URL = fake.base + "/token"
    eps = {"token_url": fake.base + "/token", "gmail_api": fake.base + "/gmail"}

    store.update_secrets("google", dict(sec_base), replace=True)
    store.update_state("gmail", {"history_id": "100"}, replace=True)
    py = google_src.GmailSource().poll()
    jv = j1({"op": "gmail_poll", **eps, "sec": dict(sec_base), "state": {"history_id": "100"}, "now": NOW})
    check("опрос Gmail: история по страницам, только непрочитанные, удалённые пропущены",
          [e.key for e in py] == ["m1", "m2", "m6"] and same_events(py, jv["events"]))
    check("курсор historyId — тот же", jv["state"]["history_id"] == store.load_state("gmail")["history_id"] == "620")
    check("access_token обновлён по refresh_token и сохранён в секреты",
          jv["sec"]["access_token"] == "at-2" and jv["sec"]["expires_at"] > NOW + 3000)
    jv0 = j1({"op": "gmail_poll", **eps, "sec": jv["sec"], "state": {}, "now": NOW})
    check("первый опрос — только курсор из профиля", jv0["events"] == [] and jv0["state"]["history_id"] == "500")
    jv404 = j1({"op": "gmail_poll", **eps, "sec": jv["sec"], "state": {"history_id": "1"}, "now": NOW})
    check("история устарела (404) — с текущего, без событий", jv404["events"] == [] and jv404["state"]["history_id"] == "500")
    revoked = j1({"op": "gmail_poll", **eps, "sec": dict(sec_base, refresh_token="rt-x"), "state": {"history_id": "1"}})
    check("refresh_token отозван — AuthError", revoked.get("__auth") is True)
    items = [dict(ev_dict(e), id=e.id, meta=e.meta) for e in py]
    served["m2"] = dict(served["m2"], labelIds=["INBOX"])
    gone = j1({"op": "gmail_gone", **eps, "sec": jv["sec"], "items": items, "now": NOW})
    check("прочитанное в Gmail — не сообщать (как GmailSource.gone)",
          gone == [items[1]["id"]] and set(gone) == google_src.GmailSource().gone(items))

    # Вход
    raw_json = json.dumps({"installed": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "s3"}})
    r = java([{"op": "parse_client", "json_text": raw_json},
              {"op": "parse_client", "client_id": "x.apps.googleusercontent.com", "client_secret": " s "},
              {"op": "parse_client", "client_id": "bad", "client_secret": "s"}])
    check("ключ клиента: из JSON и из полей, неверный — ошибка",
          r[0] == google_src.parse_client(raw_json) and r[1] == google_src.parse_client(
              {"client_id": "x.apps.googleusercontent.com", "client_secret": " s "}) and "__error" in r[2])
    pending = {"state": "st-1", "verifier": "v" * 86, "redirect": "http://127.0.0.1:5555/", "expires": NOW + 600}
    url = j1({"op": "google_url", "client_id": "cid.apps.googleusercontent.com", "pending": pending})
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(("v" * 86).encode()).digest()).decode().rstrip("=")
    check("ссылка входа: PKCE S256, state, loopback, offline, только gmail.readonly",
          q["code_challenge"] == [challenge] and q["code_challenge_method"] == ["S256"] and q["state"] == ["st-1"]
          and q["redirect_uri"] == ["http://127.0.0.1:5555/"] and q["access_type"] == ["offline"]
          and "https://www.googleapis.com/auth/gmail.readonly" in q["scope"][0] and "drive" not in q["scope"][0])
    client = {"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec"}
    ok_res = j1({"op": "google_finish", **eps, "pending": pending, "client": client, "code": "c-1", "state": "st-1",
                 "now": NOW})
    form = tokens["codes"][-1]
    check("обмен кода: verifier, redirect_uri, секрет клиента; refresh_token и почта — в секреты",
          ok_res.get("ok") and ok_res["email"] == "me@gmail.com" and ok_res["gmail"] is True
          and ok_res["secrets"]["refresh_token"] == "rt-new" and form["code_verifier"] == ["v" * 86]
          and form["redirect_uri"] == ["http://127.0.0.1:5555/"] and form["client_secret"] == ["sec"]
          and form["grant_type"] == ["authorization_code"])
    errs = java([
        {"op": "google_finish", **eps, "pending": pending, "client": client, "code": "c", "state": "other", "now": NOW},
        {"op": "google_finish", **eps, "pending": pending, "client": client, "error": "access_denied",
         "state": "st-1", "now": NOW},
        {"op": "google_finish", **eps, "pending": pending, "client": client, "code": "c", "state": "st-1",
         "now": NOW + 700},
        {"op": "google_finish", **eps, "pending": pending, "client": client, "code": "no-refresh", "state": "st-1",
         "now": NOW},
        {"op": "google_paste", **eps, "pending": pending, "client": client, "now": NOW,
         "pasted": "http://127.0.0.1:5555/?state=st-1&code=c-2&scope=email"},
    ])
    check("чужой state, отказ, устаревший вход, нет refresh_token — понятные ошибки",
          "state не совпал" in errs[0]["error"] and errs[1]["error"] == "доступ не выдан"
          and "устарел" in errs[2]["error"] and "refresh_token" in errs[3]["error"])
    check("вставка адреса из браузера — вход завершён", errs[4].get("ok") is True)

    # Вход целиком: сервер приложения на 127.0.0.1, «браузер» приносит код
    ready = Path(_TMP) / "google_ready.json"
    ops_path = Path(_TMP) / "google_ops.json"
    out_path = Path(_TMP) / "google_out.json"
    ops_path.write_text(json.dumps([{"op": "google_serve", **eps, "client": client, "ready": str(ready)}]))
    proc = subprocess.Popen(["java", "-cp", f"{_BUILD['dir']}{os.pathsep}{_BUILD['jar']}", "io.vpcore.app.SvcHarness",
                             str(ops_path), str(out_path)])
    try:
        for _ in range(100):
            if ready.exists() and ready.stat().st_size:
                break
            time.sleep(0.1)
        info = json.loads(ready.read_text())
        state = urllib.parse.parse_qs(urllib.parse.urlparse(info["url"]).query)["state"][0]
        redirect = urllib.parse.parse_qs(urllib.parse.urlparse(info["url"]).query)["redirect_uri"][0]
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/favicon.ico", timeout=5)
            fav = 200
        except urllib.error.HTTPError as e:
            fav = e.code
        page = urllib.request.urlopen(f"{redirect}?state={state}&code=c-3", timeout=10).read().decode()
        proc.wait(timeout=20)
        result = json.loads(out_path.read_text())[0]
    finally:
        if proc.poll() is None:
            proc.kill()
    check("сервер входа на 127.0.0.1: посторонний запрос — 404, ответ Google — страница «подключён»",
          redirect == f"http://127.0.0.1:{info['port']}/" and fav == 404 and "Google подключён" in page
          and result.get("ok") is True and result["secrets"]["refresh_token"] == "rt-new")


# ── 9. IMAP ─────────────────────────────────────────────────────────────

class FakeImapServer:
    """Поддельный сервер IMAP4rev1 (без TLS): LOGIN, EXAMINE, UID SEARCH/FETCH."""

    def __init__(self):
        self.user, self.password = "me@yandex.ru", "app pass \"42\""
        self.folders = {"INBOX": {"validity": "77", "msgs": {}}}
        self.commands = []
        fake = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                w = self.wfile
                w.write(b"* OK fake IMAP4rev1 ready\r\n")
                authed = False
                folder = None
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    line = line.rstrip(b"\r\n")
                    args = self._args(line)
                    if not args:
                        continue
                    tag, cmd, rest = args[0], args[1].upper(), args[2:]
                    if cmd == "UID":
                        cmd = "UID " + rest[0].upper()
                        rest = rest[1:]
                    fake.commands.append(cmd + " " + " ".join(rest))
                    if cmd == "CAPABILITY":
                        w.write(b"* CAPABILITY IMAP4rev1 LITERAL+\r\n" + tag.encode() + b" OK done\r\n")
                    elif cmd == "LOGIN":
                        authed = rest[:2] == [fake.user, fake.password]
                        w.write(tag.encode() + (b" OK logged in\r\n" if authed
                                                else b" NO [AUTHENTICATIONFAILED] Invalid credentials\r\n"))
                    elif cmd in ("SELECT", "EXAMINE") and authed:
                        f = fake.folders.get(rest[0])
                        if f is None:
                            w.write(tag.encode() + b" NO no such mailbox\r\n")
                            continue
                        folder = f
                        w.write(f"* {len(f['msgs'])} EXISTS\r\n* OK [UIDVALIDITY {f['validity']}] UIDs valid\r\n"
                                f"{tag} OK [READ-ONLY] {cmd} done\r\n".encode())
                    elif cmd == "UID SEARCH" and folder is not None:
                        uids = " ".join(str(u) for u in sorted(folder["msgs"]))
                        w.write(f"* SEARCH {uids}\r\n{tag} OK done\r\n".encode().replace(b"SEARCH \r", b"SEARCH\r"))
                    elif cmd == "UID FETCH" and folder is not None:
                        uid = int(rest[0])
                        assert "PEEK" in " ".join(rest), "письма не помечаются прочитанными"
                        if uid in folder["msgs"]:
                            flags, raw = folder["msgs"][uid]
                            seq = sorted(folder["msgs"]).index(uid) + 1
                            w.write(f"* {seq} FETCH (UID {uid} FLAGS ({flags}) BODY[]<0> {{{len(raw)}}}\r\n".encode()
                                    + raw + b")\r\n")
                        w.write(tag.encode() + b" OK done\r\n")
                    elif cmd == "LOGOUT":
                        w.write(b"* BYE bye\r\n" + tag.encode() + b" OK done\r\n")
                        return
                    else:
                        w.write(tag.encode() + b" BAD unknown\r\n")

            def _args(self, line):
                # атомы, "строки" и литералы {n}
                out, i, s = [], 0, line
                while i < len(s):
                    c = s[i:i + 1]
                    if c == b" ":
                        i += 1
                    elif c == b'"':
                        j, buf = i + 1, b""
                        while s[j:j + 1] != b'"':
                            if s[j:j + 1] == b"\\":
                                j += 1
                            buf += s[j:j + 1]
                            j += 1
                        out.append(buf.decode())
                        i = j + 1
                    elif c == b"{" and s.endswith(b"}") and s.rfind(b"{") == i:
                        n = int(s[i + 1:-1].rstrip(b"+"))
                        self.wfile.write(b"+ go on\r\n")
                        out.append(self.rfile.read(n).decode())
                        s = self.rfile.readline().rstrip(b"\r\n")
                        i = 0
                    else:
                        j = s.find(b" ", i)
                        j = len(s) if j < 0 else j
                        out.append(s[i:j].decode())
                        i = j
                return out

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _mails():
    def em(subject, sender, body, **headers):
        m = EmailMessage()
        m["From"] = sender
        if subject is not None:
            m["Subject"] = subject
        m["Date"] = "Mon, 19 Oct 2026 10:00:00 +0300"
        for k, v in headers.items():
            m[k.replace("_", "-")] = v
        m.set_content(body)
        return m.as_bytes().replace(b"\n", b"\r\n")

    alt = EmailMessage()
    alt["From"] = "Учебный офис <office@uni.ru>"
    alt["Subject"] = "Расписание на неделю: изменения в понедельник и вторник, аудитория 404"
    alt.set_content("Текст письма\nвторая строка")
    alt.add_alternative("<p>Текст <b>письма</b></p>", subtype="html")
    mixed = EmailMessage()
    mixed["From"] = "a@b.ru"
    mixed["Subject"] = "Вложение"
    mixed.set_content("Смотри файл")
    mixed.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="doc.pdf")
    mixed.add_attachment("текст вложения", filename="notes.txt")
    html_koi = (b"From: =?koi8-r?B?" + base64.b64encode("Дирекция".encode("koi8-r")) + b"?= <dir@uni.ru>\r\n"
                b"Subject: =?koi8-r?Q?=F0=D2=C9=CB=C1=DA?= =?koi8-r?Q?_=CE=CF=CD=C5=D2_5?=\r\n"
                b"Date: Tue, 20 Oct 2026 09:15:00 +0000\r\nMIME-Version: 1.0\r\n"
                b"Content-Type: multipart/alternative; boundary=\"XYZ\"\r\n\r\npreamble\r\n--XYZ\r\n"
                b"Content-Type: text/html; charset=koi8-r\r\nContent-Transfer-Encoding: base64\r\n\r\n"
                + base64.encodebytes("<html><body><p>Приказ&nbsp;N5</p><p>о сессии</p></body></html>".encode("koi8-r"))
                + b"\r\n--XYZ--\r\nepilogue\r\n")
    qp = (b"From: \"Petrov, P.\" <p@x.ru>\r\nSubject: Re: =?utf-8?q?=D0=9F=D1=80=D0=B8=D0=B2=D0=B5=D1=82?= world\r\n"
          b"Date: 20 Oct 2026 11:00 GMT\r\nContent-Type: text/plain; charset=\"windows-1251\"\r\n"
          b"Content-Transfer-Encoding: quoted-printable\r\nPrecedence: bulk\r\n\r\n"
          b"=CF=F0=E8=E2=E5=F2, =EA=E0=EA =E4=E5=EB=E0? =\r\n=EF=EE=EA=E0\r\n")
    nodate = b"From: x@y.ru\r\nContent-Type: text/plain\r\n\r\nbody only\r\n"
    words = (b"From: =?utf-8?b?" + base64.b64encode("Иван".encode()) + b"?=\r\n =?utf-8?b?"
             + base64.b64encode(" Петров".encode()) + b"?= <ivan@uni.ru>\r\nSubject: =?utf-8?b?"
             + base64.b64encode("Часть 1".encode()) + b"?=  =?utf-8?b?" + base64.b64encode(", часть 2".encode())
             + b"?=\r\n\r\ntext\r\n")
    return {
        1: ("\\Seen", em("old", "a@b.ru", "x")),
        2: ("", em("Coursework", "Ivan Petrov <ivan@uni.ru>", "When is the deadline?")),
        3: ("", em("Счёт за октябрь", "Бухгалтерия <buh@corp.ru>", "Оплатите до пятницы.\nСпасибо!")),
        4: ("", alt.as_bytes().replace(b"\n", b"\r\n")),
        5: ("", mixed.as_bytes().replace(b"\n", b"\r\n")),
        6: ("", html_koi),
        7: ("", qp),
        8: ("\\Seen \\Answered", em("Прочитано", "x@y.ru", "y")),
        9: ("", em("Скидки", "Shop <news@shop.ru>", "акция", List_Unsubscribe="<mailto:u@shop.ru>")),
        10: ("\\Flagged", nodate),
        11: ("", words),
    }


def test_imap():
    section("9. IMAP: поддельный сервер рядом с ядром (imaplib)")
    from app.integrations import store
    from app.integrations.sources import AuthError, SourceError
    from app.integrations.sources import imap_mail
    reset_data()
    srv = FakeImapServer()
    mails = _mails()
    try:
        # Письмо → событие напрямую
        res = java([{"op": "mail_event", "b64": b64(raw), "key": f"77:{uid}", "flags": flags}
                    for uid, (flags, raw) in mails.items()])
        good = True
        for (uid, (flags, raw)), r in zip(mails.items(), res):
            e = imap_mail.parse_message(raw, f"77:{uid}", flags)
            if not same_events([e], [r]):
                good = False
        check("письма → события: кодированные заголовки, base64/QP, koi8-r/1251, вложения, рассылки", good)
        hdrs = ["=?utf-8?q?a?= b", "a =?utf-8?q?b?=", "=?utf-8?q?a?==?utf-8?q?b?=", "=?utf-8?q?a?= =?utf-8?q?b?=",
                "=?utf-8?b?" + base64.b64encode("Да".encode()).decode() + "?=x", "plain", "",
                "=?unknown-cs?q?a?= z", "=?us-ascii?q?Hi?= there"]
        res = java([{"op": "decode_header", "text": h} for h in hdrs])
        check("заголовки RFC 2047 — как decode_header/make_header", res == [imap_mail._decode(h) for h in hdrs])

        acc = {"user": srv.user, "password": srv.password, "host": "127.0.0.1", "port": srv.port, "folder": "INBOX"}
        store.update_secrets("imap", acc)

        def plain(a):
            c = imaplib.IMAP4("127.0.0.1", srv.port)
            try:
                c.login(a["user"], a["password"])
            except imaplib.IMAP4.error as e:
                raise AuthError(f"imap: вход не принят ({e})")
            return c

        imap_mail.connect_factory = plain
        srv.folders["INBOX"]["msgs"] = {1: mails[1]}
        src = imap_mail.ImapSource()
        py0 = src.poll()
        jv0 = j1({"op": "imap_poll", "acc": acc, "state": {}})
        check("первый опрос — курсор UID и UIDVALIDITY, без событий",
              py0 == [] and jv0["events"] == [] and jv0["state"] == {"validity": "77", "last_uid": 1}
              and store.load_state("imap") == jv0["state"])
        check("ящик открыт только на чтение (EXAMINE)", any(c.startswith("EXAMINE") for c in srv.commands)
              and not any(c.startswith("SELECT") for c in srv.commands))
        srv.password = "пароль приложения"
        lit = j1({"op": "imap_check", "acc": dict(acc, password=srv.password)})
        check("пароль не-ASCII — литералом {n} (imaplib ядра так не умеет)", lit.get("ok") is True)
        srv.password = acc["password"]
        srv.folders["INBOX"]["msgs"] = dict(mails)
        py1 = src.poll()
        jv1 = j1({"op": "imap_poll", "acc": acc, "state": jv0["state"]})
        check("новые непрочитанные — те же события, прочитанные пропущены",
              same_events(py1, jv1["events"]) and len(py1) == 9 and jv1["state"]["last_uid"] == 11)
        check("письма берутся BODY.PEEK (не помечаются прочитанными)",
              all("PEEK" in c for c in srv.commands if c.startswith("UID FETCH")))
        jv2 = j1({"op": "imap_poll", "acc": acc, "state": jv1["state"]})
        check("повторный опрос — без событий", jv2["events"] == [] and src.poll() == [])
        srv.folders["INBOX"]["validity"] = "78"
        jv3 = j1({"op": "imap_poll", "acc": acc, "state": jv1["state"]})
        check("сменился UIDVALIDITY — курсор заново, без событий",
              jv3["events"] == [] and jv3["state"]["validity"] == "78" and src.poll() == [])
        bad = j1({"op": "imap_poll", "acc": dict(acc, password="wrong"), "state": {}})
        check("неверный пароль — AuthError", bad.get("__auth") is True)
        nofolder = j1({"op": "imap_check", "acc": dict(acc, folder="Архив 2026")})
        check("нет папки — понятная ошибка (имя с пробелом — в кавычках)",
              "нет папки" in nofolder.get("__error", "") and any('Архив 2026' in c for c in srv.commands))
        okc = j1({"op": "imap_check", "acc": acc})
        check("«Проверить» ящик", okc == {"ok": True, "account": srv.user})
        norm = java([{"op": "imap_normalize", "acc": {"user": "x@mail.ru", "password": "p"}},
                     {"op": "imap_normalize", "acc": {"user": "x@gmail.com", "password": "p", "folder": " "}},
                     {"op": "imap_normalize", "acc": {"user": "x@unknown.org", "password": "p"}}])
        check("пресеты серверов и проверка полей — как normalize_account",
              norm[0] == imap_mail.normalize_account({"user": "x@mail.ru", "password": "p"})
              and norm[1] == imap_mail.normalize_account({"user": "x@gmail.com", "password": "p", "folder": " "})
              and "__error" in norm[2])
        try:
            imap_mail.ImapSource().check(dict(acc, folder="Missing"))
            py_err = False
        except SourceError:
            py_err = True
        check("нет папки — ошибка и у ядра", py_err)
        return srv, jv1["state"]
    except Exception:
        srv.stop()
        raise


# ── 10. Перенос на компьютер ────────────────────────────────────────────

def test_transfer(fake, rss_state, gh_state, srv, imap_state):
    section("10. Перенос на компьютер: POST /api/integrations/import")
    from fastapi.testclient import TestClient
    import app.api.server as server_mod
    from app.integrations import store
    from app.integrations.feed import feed
    from app.integrations.sources import github as gh_src
    from app.integrations.sources import google as google_src
    from app.integrations.sources import imap_mail, rss
    reset_data()
    srv.folders["INBOX"]["validity"] = "77"  # ящик тот же, что видел телефон
    feed_url = fake.base + "/feed.xml"
    imap_acc = {"user": srv.user, "password": srv.password, "host": "127.0.0.1", "port": srv.port, "folder": "INBOX"}
    phone_settings = j1({"op": "normalize", "settings": {
        "notify": {"persona": "connor", "quiet": {"enabled": True, "from": "22:00", "to": "07:00", "urgent": False},
                   "digest_times": ["08:30"], "max_per_hour": 3},
        "services": {"rss": {"enabled": True, "feeds": [{"url": feed_url, "title": "Мой блог"}]},
                     "github": {"enabled": True}, "imap": {"enabled": True}, "gmail": {"enabled": True}},
        "rules": [dict(r) for r in RULES[:3]]}})
    secrets = {"github": {"token": GH_TOKEN, "login": "neo"}, "imap": imap_acc,
               "google": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec",
                          "refresh_token": "rt-1", "email": "me@gmail.com", "access_token": "at-2",
                          "expires_at": NOW + 3000,
                          "scopes": ["email", "https://www.googleapis.com/auth/gmail.readonly"]}}
    events = _events()[:6]
    ing = j1({"op": "ingest", "feed": {"items": [], "seen": []}, "events": [ev_dict(e) for e in events], "now": NOW})
    phone_feed = ing["feed"]
    for i, it in enumerate(phone_feed["items"]):
        it.update(level="notify" if i else "digest", status="notified" if i else "digest", summary=f"s{i}")
    state = {"rss": rss_state, "github": gh_state, "imap": imap_state, "gmail": {"history_id": "620"}}
    bundle = j1({"op": "bundle", "settings": phone_settings, "secrets": secrets, "state": state, "feed": phone_feed})
    check("тело переноса: все четыре источника, ключи и курсоры",
          bundle["sources"] == ["rss", "github", "imap", "gmail"] and bundle["github"]["token"] == GH_TOKEN
          and bundle["imap"]["validity"] == "77" and bundle["imap"]["last_uid"] == 11
          and bundle["google"]["history_id"] == "620" and bundle["rss"]["feeds"][0]["seen_init"] is True
          and len(bundle["rss"]["feeds"][0]["seen"]) == 5)

    orig = server_mod._api_token
    server_mod._api_token = ""
    try:
        c = TestClient(server_mod.app, base_url="http://127.0.0.1")
        r = c.post("/api/integrations/import", json=bundle)
        res = r.json()
        check("ядро приняло: перенесено всё, без пропусков",
              r.status_code == 200 and res["moved"] == ["rss", "github", "imap", "gmail"] and res["skipped"] == {})
        s = store.load_settings()
        check("ключи доступа — в secrets.json ядра",
              store.load_secrets("github")["token"] == GH_TOKEN and store.load_secrets("imap")["password"] == srv.password
              and store.load_secrets("google")["refresh_token"] == "rt-1")
        check("источники включены, лента в списке, правила добавлены",
              all(s["services"][n]["enabled"] for n in ("rss", "github", "imap", "gmail"))
              and s["services"]["rss"]["feeds"] == [{"url": feed_url, "title": "Мой блог"}]
              and res["rules"] == 3 and len(s["rules"]) == 3)
        check("настройки уведомлений с телефона — туда, где у ядра умолчание (и персона)",
              s["notify"]["quiet"]["from"] == "22:00" and s["notify"]["digest_times"] == ["08:30"]
              and s["notify"]["max_per_hour"] == 3 and s["notify"]["persona"] == "connor")
        check("курсоры: «уже видел» ленты, GitHub since, IMAP UID, Gmail historyId",
              store.load_state("rss")["feeds"][feed_url]["seen"] == rss_state["feeds"][feed_url]["seen"]
              and abs(store.load_state("github")["since"] - gh_state["since"]) < 1e-6
              and store.load_state("imap") == {"validity": "77", "last_uid": 11}
              and store.load_state("gmail") == {"history_id": "620"})
        ids = {it["id"] for it in feed.items()}
        check("лента телефона — в ленте ядра (ждущее сводки — ждёт сводки ядра)",
              res["items"] == 6 and ids == {e.id for e in events}
              and [it["status"] for it in feed.items() if it["id"] == events[0].id] == ["digest"])

        # Ядро не присылает второй раз виденное телефоном — и присылает новое
        check("RSS: старое — не новое (304 по ETag телефона)", rss.RssSource().poll() == [])
        check("GitHub: старое — не новое", gh_src.GitHubSource().poll() == [])
        check("IMAP: старое — не новое", imap_mail.ImapSource().poll() == []
              and store.load_state("imap")["last_uid"] == 11)
        newer = EmailMessage()
        newer["From"] = "new@uni.ru"
        newer["Subject"] = "Свежее"
        newer.set_content("после переноса")
        srv.folders["INBOX"]["msgs"][12] = ("", newer.as_bytes())
        got = imap_mail.ImapSource().poll()
        check("IMAP: письмо после переноса — событие", [e.title for e in got] == ["Свежее"])
        # то же письмо в ленте телефона — повтором не встаёт
        check("событие, которое телефон уже видел, лента ядра не примет",
              feed.add([events[1]]) == [] and len(feed.add(got)) == 1)
        got = google_src.GmailSource().poll()
        check("Gmail: опрос с historyId телефона", [e.key for e in got] == ["m1", "m6"]
              or [e.key for e in got] == ["m1", "m2", "m6"])

        # Повтор переноса — без дублей
        r2 = c.post("/api/integrations/import", json=bundle).json()
        s2 = store.load_settings()
        check("повтор: тот же аккаунт — перенесено, правила/ленты/записи не задвоились",
              r2["moved"] == ["rss", "github", "imap", "gmail"] and r2["rules"] == 0 and r2["items"] == 0
              and len(s2["rules"]) == 3 and len(s2["services"]["rss"]["feeds"]) == 1)

        # Компьютер главный: другой аккаунт — не заменяется
        reset_data()
        store.update_secrets("github", {"token": "ghp_" + "z" * 36, "login": "other"})
        store.update_secrets("imap", {"user": "boss@corp.ru", "password": "x", "host": "imap.corp.ru", "port": 993})
        store.update_secrets("google", {"refresh_token": "rt-core", "email": "work@corp.ru",
                                        "client_id": "c.apps.googleusercontent.com", "client_secret": "s"})
        store.update_settings(lambda st: st["notify"].update(max_per_hour=10))
        r3 = c.post("/api/integrations/import", json=bundle).json()
        check("на компьютере свои GitHub/почта/Google — пропущены с причиной, ключи ядра целы",
              r3["moved"] == ["rss"] and set(r3["skipped"]) == {"github", "imap", "gmail"}
              and store.load_secrets("github")["login"] == "other"
              and store.load_secrets("google")["refresh_token"] == "rt-core")
        check("изменённая на компьютере настройка не перезаписана", store.load_settings()["notify"]["max_per_hour"] == 10)
        bad = dict(bundle, imap=dict(bundle["imap"], validity="abc"))
        check("неверное тело — 422", c.post("/api/integrations/import", json=bad).status_code == 422)
        check("пустой перенос — ничего не ломает", c.post("/api/integrations/import", json={}).json()["moved"] == [])
        moved = j1({"op": "normalize", "settings": dict(phone_settings, moved={"rss": NOW, "gmail": NOW, "x": 1})})
        left = j1({"op": "bundle", "settings": moved, "secrets": secrets, "state": state, "feed": phone_feed})
        check("перенесённое телефон не предлагает снова", left["sources"] == ["github", "imap"]
              and "rss" not in left and "google" not in left)
    finally:
        server_mod._api_token = orig


def main():
    from app.integrations import triage
    triage._call_local = lambda *a, **k: None  # Ollama в тестах не зовём
    fake = None
    srv = None
    try:
        if not test_build():
            print("\nИтого: сборка не удалась")
            return 1
        test_triage()
        test_texts()
        test_tick()
        test_feed_settings()
        test_rss_parse()
        fake = Fake()
        rss_state = test_rss_poll(fake)
        gh_state = test_github(fake)
        test_gmail(fake)
        srv, imap_state = test_imap()
        test_transfer(fake, rss_state, gh_state, srv, imap_state)
    finally:
        from app.integrations import sources
        from app.integrations.sources import imap_mail
        sources._transport = None
        imap_mail.connect_factory = None
        if fake:
            fake.stop()
        if srv:
            srv.stop()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\nИтого: {ok - failures}/{ok} OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

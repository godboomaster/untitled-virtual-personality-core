"""Тест закрытых дыр доступа:

  1. Сервер экспорта памяти: без EXPORT_TOKEN не открыт всем — токен
     создаётся в <data>/export_token (права 0600) и переживает перезапуск;
     без токена/с чужим — 401; ?token= и Bearer — 200; EXPORT_TOKEN из env
     главнее файла; в ответе нет Access-Control-Allow-Origin.
  2. Telegram: каждая слэш-команда проходит тот же гейт, что и сообщения
     (blocked/allowed_dm/punish/rate limit/модерация) — заблокированный не
     зовёт /remind, /reset и т.д.; владелец проходит.
  3. /context — только владельцу (в группе дамп содержит факты других).
  4. API: запрос со страницы принимается только со своего фронта
     (localhost/127.0.0.1/[::1] или API_CORS_ORIGINS), Host — только свои
     имена (DNS rebinding), в том числе «простые» POST и preflight.

Данные — во временной папке (VPC_DATA_DIR), настоящий data/ не трогается.
Часть 2–3 требует python-telegram-bot: без него — SKIP.
Запуск: python3 -m scripts.test_security_gates
"""

import asyncio
import json
import os
import socket
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _wait_up(port):
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def test_export_server(tmp: Path):
    section("1. Сервер экспорта: только по токену")
    os.environ.pop("EXPORT_TOKEN", None)
    from app.features import export_server as es

    port = _free_port()
    es.start_export_server(port=port, host="127.0.0.1")
    check("сервер поднялся", _wait_up(port))
    token_path = tmp / "export_token"
    check("без EXPORT_TOKEN токен создан в <data>/export_token", token_path.exists())
    token = token_path.read_text(encoding="utf-8").strip()
    check("токен непустой и не короткий", len(token) >= 20)
    check("права файла токена 0600",
          stat.S_IMODE(token_path.stat().st_mode) == 0o600)

    base = f"http://127.0.0.1:{port}/api/export-memory/list"
    code, _, _ = _get(base)
    check("без токена — 401", code == 401)
    code, _, _ = _get(base + "?token=wrong")
    check("чужой ?token= — 401", code == 401)
    code, _, _ = _get(base, {"Authorization": "Bearer wrong"})
    check("чужой Bearer — 401", code == 401)
    code, headers, body = _get(base + f"?token={token}")
    check("верный ?token= — 200", code == 200)
    check("ответ — JSON", isinstance(json.loads(body), dict))
    check("нет Access-Control-Allow-Origin (сторонняя страница не прочитает)",
          not any(k.lower() == "access-control-allow-origin" for k in headers))
    code, _, _ = _get(base + "?token=wrong", {"Authorization": f"Bearer {token}"})
    check("верный Bearer при чужом ?token= — 200", code == 200)
    code, _, _ = _get(f"http://127.0.0.1:{port}/api/export-memory?token={token}")
    check("полный экспорт по токену — 200", code == 200)
    code, _, _ = _get(f"http://127.0.0.1:{port}/health")
    check("/health открыт (без данных)", code == 200)

    # Перезапуск: тот же токен из файла, а не новый
    port2 = _free_port()
    es.start_export_server(port=port2, host="127.0.0.1")
    _wait_up(port2)
    check("после перезапуска токен из файла тот же", es.EXPORT_TOKEN == token)

    # EXPORT_TOKEN из env главнее файла
    os.environ["EXPORT_TOKEN"] = "env-token-123456789012345"
    try:
        port3 = _free_port()
        es.start_export_server(port=port3, host="127.0.0.1")
        _wait_up(port3)
        u3 = f"http://127.0.0.1:{port3}/api/export-memory/list"
        check("EXPORT_TOKEN из env принимается", _get(u3 + "?token=env-token-123456789012345")[0] == 200)
        check("токен файла при заданном env — 401", _get(u3 + f"?token={token}")[0] == 401)
    finally:
        os.environ.pop("EXPORT_TOKEN", None)


# ── Telegram ──

class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []
        self.documents = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)

    async def reply_document(self, document=None, caption=None, **kw):
        self.documents.append(caption)


def _update(text, user_id, chat_type="private"):
    msg = FakeMessage(text)
    return SimpleNamespace(
        message=msg,
        effective_user=SimpleNamespace(id=int(user_id), first_name="U", username=None),
        effective_chat=SimpleNamespace(id=int(user_id) if chat_type == "private" else -100500,
                                       type=chat_type),
    )


class FakeApp:
    def __init__(self):
        self.bot = object()
        self.handlers = []

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))


def _fake_bot(verdicts):
    """pre_check: verdicts[user_id] — что вернёт гейт (None — пропустить)."""
    calls = {"debug_context": 0}

    def debug_context(user_id, chat_id):
        calls["debug_context"] += 1
        return "ctx"

    bot = SimpleNamespace(
        persona_name="gate_test", features={}, owner="111",
        self_memory=None, file_db=None, _web_search_enabled=False,
        todo_manager=None, reminder_manager=None, inventory_manager=None,
        learning_manager=None, _activity_tracker=None, _rate_limit_enabled=False,
        setup_rhythm=lambda sender: None,
        pre_check=lambda user_id, text, is_private: verdicts.get(user_id),
        debug_context=debug_context,
    )
    return bot, calls


def test_telegram(tmp: Path):
    section("2. Telegram: команды проходят гейт сообщений")
    try:
        import telegram  # noqa: F401
        from telegram.ext import CommandHandler
    except ImportError:
        print("  [SKIP] нет python-telegram-bot")
        return
    os.environ.pop("OWNER_USER_ID", None)
    import app.telegram_bot as tb

    # Подменные обработчики: считаем, кого реально вызвали
    called = []
    names = ["start", "help", "stats", "erase", "last", "reset", "forget",
             "context", "relations", "resetall", "ltm_privacy", "ltm_export",
             "reset_diary", "files", "reset_files", "ratelimits", "web", "todo",
             "reminders", "cancel_reminder", "inventory", "remind", "add_todo",
             "add_inventory", "learn", "stop_learning"]

    def fake_create(bot):
        def mk(n):
            async def h(update, context):
                called.append(n)
            return h
        d = {n: mk(n) for n in names}
        d.update(handle_message=mk("msg"), handle_document=None, handle_photo=mk("photo"))
        return d

    verdicts = {"222": "BLOCKED", "333": "RATE_LIMITED", "444": "PUNISH_BLOCKED"}
    bot, _ = _fake_bot(verdicts)
    real_create = tb.create_handlers
    tb.create_handlers = fake_create
    try:
        app = FakeApp()
        tb.register_handlers(app, bot)
    finally:
        tb.create_handlers = real_create

    cmds = {}
    for h, _g in app.handlers:
        if isinstance(h, CommandHandler):
            for c in h.commands:
                cmds[c] = h.callback
    check("зарегистрированы все 26 команд", sorted(cmds) == sorted(names))

    async def run_all(user_id):
        called.clear()
        for n, cb in cmds.items():
            await cb(_update(f"/{n} аргумент", user_id), None)
        return list(called)

    blocked = asyncio.run(run_all("222"))
    check("заблокированный: ни одна команда не выполнена", blocked == [])
    check("упёршийся в лимит: ни одна команда не выполнена", asyncio.run(run_all("333")) == [])
    check("наказанный: ни одна команда не выполнена", asyncio.run(run_all("444")) == [])
    passed = asyncio.run(run_all("111"))
    check("владелец/разрешённый: выполнены все команды", sorted(passed) == sorted(names))

    # Модерация: команда не выполняется, отвечает реплика гейта
    verdicts["555"] = "MODERATION_BLOCKED"
    called.clear()
    upd = _update("/remind плохое", "555")
    asyncio.run(cmds["remind"](upd, None))
    check("модерация: /remind не выполнен", called == [])
    check("модерация: ответ гейта отправлен", bool(upd.message.replies))

    section("3. /context — только владельцу")
    bot, calls = _fake_bot({})
    h = tb.create_handlers(bot)
    upd = _update("/context", "999", chat_type="supergroup")
    asyncio.run(h["context"](upd, None))
    check("посторонний в группе: дамп не собран", calls["debug_context"] == 0)
    check("посторонний в группе: файл не отправлен", upd.message.documents == [])
    upd = _update("/context", "111", chat_type="supergroup")
    asyncio.run(h["context"](upd, None))
    check("владелец: дамп собран и отправлен файлом",
          calls["debug_context"] == 1 and len(upd.message.documents) == 1)
    os.environ["OWNER_USER_ID"] = "777"
    try:
        upd = _update("/context", "777")
        asyncio.run(h["context"](upd, None))
        check("OWNER_USER_ID: дамп отправлен", len(upd.message.documents) == 1)
    finally:
        os.environ.pop("OWNER_USER_ID", None)

    help_upd = _update("/help", "999")
    asyncio.run(h["help"](help_upd, None))
    check("/help постороннему не показывает /context",
          "/context" not in "\n".join(help_upd.message.replies))
    help_upd = _update("/help", "111")
    asyncio.run(h["help"](help_upd, None))
    check("/help владельцу показывает /context",
          "/context" in "\n".join(help_upd.message.replies))


def test_api_origin():
    section("4. API: только свой фронт (Origin) и свои имена хоста (Host)")
    for k in ("API_CORS_ORIGINS", "API_HOST", "API_ALLOWED_HOSTS", "API_TOKEN"):
        os.environ.pop(k, None)
    from fastapi.testclient import TestClient
    import app.api.server as server_mod
    from app.api.security import LocalOriginGuard, LOOPBACK_HOSTS

    check("LocalOriginGuard подключён к приложению",
          any(m.cls is LocalOriginGuard for m in server_mod.app.user_middleware))
    c = TestClient(server_mod.app, base_url="http://127.0.0.1")

    def get(origin=None, path="/api/health"):
        return c.get(path, headers={"Origin": origin} if origin else {})

    check("без Origin (curl/скрипт) — 200", get().status_code == 200)
    for o in ("http://localhost:5173", "http://127.0.0.1:5198", "http://[::1]:4173"):
        r = get(o)
        check(f"свой фронт {o} — 200 и CORS-заголовок",
              r.status_code == 200 and r.headers.get("access-control-allow-origin") == o)
    for o in ("https://evil.example", "http://localhost.evil.com", "null",
              "http://127.0.0.1.evil.com:5173", "file://"):
        r = get(o)
        check(f"чужой Origin {o!r} — 403 без CORS-заголовка",
              r.status_code == 403 and "access-control-allow-origin" not in r.headers)
    r = c.post("/api/personas/connor/memory/clear", headers={"Origin": "https://evil.example"})
    check("«простой» POST без тела с чужой страницы — 403 до исполнения", r.status_code == 403)
    r = c.options("/api/chat", headers={"Origin": "https://evil.example",
                                        "Access-Control-Request-Method": "POST"})
    check("preflight с чужой страницы — 403", r.status_code == 403)
    r = c.options("/api/chat", headers={"Origin": "http://localhost:5173",
                                        "Access-Control-Request-Method": "POST",
                                        "Access-Control-Request-Headers": "content-type"})
    check("preflight своего фронта — 200 с разрешением",
          r.status_code == 200 and r.headers.get("access-control-allow-origin") == "http://localhost:5173")

    def host(h, origin=None):
        hdrs = {"Host": h}
        if origin:
            hdrs["Origin"] = origin
        return c.get("/api/health", headers=hdrs).status_code

    check("Host 127.0.0.1:8000 — 200", host("127.0.0.1:8000") == 200)
    check("Host localhost:8000 — 200", host("localhost:8000") == 200)
    check("Host [::1]:8000 — 200", host("[::1]:8000") == 200)
    check("DNS rebinding: Host evil.example:8000 — 400", host("evil.example:8000") == 400)
    check("DNS rebinding со «своим» Origin — 400",
          host("evil.example:8000", "http://evil.example:8000") == 400)

    # Политика из env — на голом приложении, чтобы не перезагружать server
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    def mini(**kw):
        a = Starlette(routes=[Route("/", lambda r: PlainTextResponse("ok"))])
        a.add_middleware(LocalOriginGuard, **kw)
        return TestClient(a, base_url="http://127.0.0.1")

    m = mini(origins=["http://192.168.1.5:5173"], hosts=set(LOOPBACK_HOSTS))
    check("API_CORS_ORIGINS списком: указанный источник — 200",
          m.get("/", headers={"Origin": "http://192.168.1.5:5173"}).status_code == 200)
    check("API_CORS_ORIGINS списком: localhost не добавляется сам — 403",
          m.get("/", headers={"Origin": "http://localhost:5173"}).status_code == 403)
    m = mini(origins=["*"], hosts=None)
    check("API_CORS_ORIGINS=* — любой источник",
          m.get("/", headers={"Origin": "https://any.example"}).status_code == 200)
    check("привязка наружу без API_ALLOWED_HOSTS — Host не проверяется",
          m.get("/", headers={"Host": "192.168.1.5:8000"}).status_code == 200)
    m = mini(origin_regex=None, hosts=set(LOOPBACK_HOSTS) | {"vpc.local"})
    check("API_ALLOWED_HOSTS: добавленное имя — 200",
          m.get("/", headers={"Host": "vpc.local:8000"}).status_code == 200)


def test_env_bind_mount(tmp: Path):
    section("5. .env, смонтированный отдельным файлом (Docker): os.replace → EBUSY")
    import errno
    from unittest import mock
    from app.api import security
    env = tmp / ".env"
    env.write_text("A=1\nB=2\n", encoding="utf-8")
    inode = env.stat().st_ino
    real_replace = os.replace

    def busy_replace(src, dst, *a, **k):
        if Path(dst) == env:
            raise OSError(errno.EBUSY, "Device or resource busy")
        return real_replace(src, dst, *a, **k)

    with mock.patch("os.replace", busy_replace):
        security.persist_env(env, "C", "x y")
        security.persist_env(env, "A", "9")
        security.remove_env(env, "B")
    text = env.read_text(encoding="utf-8")
    check("запись и удаление дошли до файла", "C='x y'" in text and "A='9'" in text
          and "B=" not in text)
    check("файл тот же (не подменён переименованием)", env.stat().st_ino == inode)
    check("временных файлов рядом не осталось",
          sorted(f.name for f in tmp.iterdir() if f.name.startswith(".tmp")) == [])

    def other_error(src, dst, *a, **k):
        raise OSError(errno.EACCES, "Permission denied")

    with mock.patch("os.replace", other_error):
        try:
            security.persist_env(env, "D", "1")
            raised = False
        except OSError:
            raised = True
    check("другая ошибка replace — не глушится", raised and "D=" not in env.read_text())


def main():
    tmp = Path(tempfile.mkdtemp(prefix="sec_gates_"))
    os.environ["VPC_DATA_DIR"] = str(tmp)
    test_export_server(tmp)
    test_telegram(tmp)
    test_api_origin()
    test_env_bind_mount(tmp)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

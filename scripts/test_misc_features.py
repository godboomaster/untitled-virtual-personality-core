"""Smoke-тест точечных сценариев, не покрытых test_api_security.py/
test_state_io.py:

  - clear_backup: снапшот корзины /api/chat/clear привязан к (persona,
    chat_id), а не только к persona — восстановление одного чата не должно
    задевать другой; старый плоский формат бэкапа читается миграцией;
  - memory_wipe: полная очистка чата стирает ВСЕ ожидающие setup «как
    часто» learning_manager в этом чате, а не только один;
  - web_search: is_safe_public_url — SSRF-фильтр (приватные/loopback/
    link-local/metadata-адреса, IPv4/IPv6, «резолвится и туда, и сюда»),
    fetch_page_text проверяет КАЖДЫЙ хоп редиректа, а не только исходный URL;
  - query_rewriter: анафора перед знаком препинания («его?», «него.»)
    распознаётся;
  - local_router: get_local_router() — потокобезопасная ленивая
    инициализация (double-checked locking, ровно один экземпляр под гонкой);
  - file_sender: код-блок без языкового тега распознаётся и получает
    вменяемое расширение; частично записанные tmp-файлы подчищаются при
    сбое записи посреди prepare_response().

Все проверки — на временных каталогах/моках, без сети и без реального data/.
Запуск: PYTHONPATH=. python3 scripts/test_misc_features.py
"""

import json
import shutil
import sys
import tempfile
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


# ════════════ A. clear_backup: привязка к (persona, chat_id) ════════════

def test_clear_backup_chat_isolation():
    section("A. clear_backup: бэкапы привязаны к (persona, chat_id)")
    from app.api import clear_backup as cb

    tmp = Path(tempfile.mkdtemp(prefix="clearbackup_"))
    orig_data_dir = cb._DATA_DIR
    cb._DATA_DIR = tmp
    try:
        persona = "testp"
        (tmp / f"api_{persona}").mkdir(parents=True, exist_ok=True)

        p1 = cb.make_backup(persona, "u1", "chat1",
                            stm=[{"role": "user", "content": "chat1 msg"}],
                            ltm=[], diary=None)
        check("make_backup: файл chat1 создан", p1 is not None and p1.is_file())
        p2 = cb.make_backup(persona, "u2", "chat2",
                            stm=[{"role": "user", "content": "chat2 msg"}],
                            ltm=[], diary=None)
        check("make_backup: файл chat2 создан", p2 is not None and p2.is_file())
        check("make_backup: chat1 и chat2 — РАЗНЫЕ каталоги",
              p1 != p2 and p1.parent != p2.parent)

        b1 = cb.latest_backup(persona, "chat1")
        b2 = cb.latest_backup(persona, "chat2")
        check("latest_backup(chat1): свой снапшот, не чужой",
              b1 is not None and b1["stm"][0]["content"] == "chat1 msg")
        check("latest_backup(chat2): свой снапшот, не перепутан с chat1",
              b2 is not None and b2["stm"][0]["content"] == "chat2 msg")

        info1 = cb.backup_info(persona, "chat1")
        check("backup_info(chat1): exists=True, свои counts",
              info1["exists"] and info1["counts"]["stm"] == 1)

        popped1 = cb.pop_latest(persona, "chat1")
        check("pop_latest(chat1): вернул снапшот chat1", popped1["stm"][0]["content"] == "chat1 msg")
        check("pop_latest(chat1): файл chat1 удалён", not p1.exists())
        check("pop_latest(chat1): chat2 НЕ затронут restore'ом chat1", p2.exists())
        b2_after = cb.latest_backup(persona, "chat2")
        check("chat2: бэкап цел после восстановления chat1", b2_after is not None
              and b2_after["stm"][0]["content"] == "chat2 msg")

        # ── миграция чтения: старый плоский формат бэкапа ──
        root = cb._backup_root(persona)
        legacy_path = root / "1000000000.json"
        legacy_path.write_text(json.dumps({
            "ts": 1000000000, "persona": persona, "user_id": "u3", "chat_id": "legacy_chat",
            "stm": [{"role": "user", "content": "legacy msg"}], "ltm": [], "diary": None,
            "initiatives": [], "daily_stats": None, "last_activity": 0,
            "chat_urls": {}, "stores": {},
        }, ensure_ascii=False), encoding="utf-8")
        legacy = cb.latest_backup(persona, "legacy_chat")
        check("latest_backup: старый плоский бэкап найден по chat_id внутри (миграция чтения)",
              legacy is not None and legacy["stm"][0]["content"] == "legacy msg")
        check("latest_backup: чужой chat_id не матчит легаси-файл",
              cb.latest_backup(persona, "not_legacy_chat") is None)
        popped_legacy = cb.pop_latest(persona, "legacy_chat")
        check("pop_latest: легаси-файл вернулся и удалился из корня",
              popped_legacy is not None and not legacy_path.exists())

        # ── traversal через chat_id не выходит за пределы clear_backups/ ──
        bdir_evil = cb._backup_dir(persona, "../../../etc")
        check("_backup_dir: traversal в chat_id не выходит за clear_backups/",
              bdir_evil is not None and root.resolve() in bdir_evil.resolve().parents)
    finally:
        cb._DATA_DIR = orig_data_dir
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════ B. memory_wipe: learning setup — все пользователи чата ════════════

def test_memory_wipe_learning_setup_multiuser():
    section("B. memory_wipe: очистка чата стирает ВСЕ ожидающие setup «как часто»")
    from app.api import memory_wipe as mw

    class _FakeLearningMgr:
        def __init__(self):
            self._lock = threading.RLock()
            self._sessions = []
            # Реальная форма: chat_id -> user_id -> state (см. learning_manager.begin_setup)
            self._setup_state = {
                "chat1": {"userA": {"subject": "english"}, "userB": {"subject": "spanish"}},
                "other_chat": {"userC": {"subject": "french"}},
            }
            self._question_msgs = {"chat1": 111, "other_chat": 222}
            self.saved = 0

        def _save(self):
            self.saved += 1

        def clear_chat(self, ck):
            """Публичная очистка обучения чата: memory_wipe снимает сессии и
            ожидающие setup через неё, а не через приватные поля."""
            with self._lock:
                ck = str(ck)
                self._sessions = [s for s in self._sessions
                                  if str(s.get("chat_id")) != ck]
                self._setup_state.pop(ck, None)
                self._question_msgs.pop(ck, None)
                self._save()

    class _FakeBot:
        pass

    bot = _FakeBot()
    bot.learning_manager = _FakeLearningMgr()

    mw._wipe_learning(bot, "api_testp", "chat1")
    check("wipe_learning: у chat1 не осталось НИ ОДНОГО ожидающего setup (ни userA, ни userB) — "
          "раньше mgr.clear_setup(ck) без user_id чистил только «единственного», "
          "теперь это делает публичный mgr.clear_chat(chat_id)",
          "chat1" not in bot.learning_manager._setup_state)
    check("wipe_learning: setup ДРУГОГО чата не тронут",
          "other_chat" in bot.learning_manager._setup_state
          and "userC" in bot.learning_manager._setup_state["other_chat"])
    check("wipe_learning: _question_msgs своего чата очищен",
          "chat1" not in bot.learning_manager._question_msgs)
    check("wipe_learning: _save() вызван под тем же локом", bot.learning_manager.saved >= 1)


# ════════════ C. web_search: SSRF-фильтр ════════════

def test_web_search_ssrf_filter():
    section("C. web_search.is_safe_public_url — SSRF-фильтр")
    from app.features import web_search as ws
    import socket

    check("is_safe_public_url: не-http(s) схема отклонена",
          not ws.is_safe_public_url("file:///etc/passwd"))
    check("is_safe_public_url: URL без хоста отклонён",
          not ws.is_safe_public_url("http:///path"))

    def _resolve_to(*ips):
        def _fake(host, *a, **kw):
            return [(2, 1, 6, '', (ip, 0)) for ip in ips]
        return _fake

    with mock.patch("socket.getaddrinfo", _resolve_to("93.184.216.34")):
        check("is_safe_public_url: публичный IPv4 — разрешён",
              ws.is_safe_public_url("https://example.com/page"))
    with mock.patch("socket.getaddrinfo", _resolve_to("10.0.0.5")):
        check("is_safe_public_url: приватный 10.x — отклонён",
              not ws.is_safe_public_url("http://internal.example/"))
    with mock.patch("socket.getaddrinfo", _resolve_to("127.0.0.1")):
        check("is_safe_public_url: 127.0.0.1 — отклонён",
              not ws.is_safe_public_url("http://sneaky.example/"))
    with mock.patch("socket.getaddrinfo", _resolve_to("169.254.169.254")):
        check("is_safe_public_url: 169.254.169.254 (облачные метаданные) — отклонён",
              not ws.is_safe_public_url("http://metadata.example/"))
    with mock.patch("socket.getaddrinfo", _resolve_to("93.184.216.34", "127.0.0.1")):
        check("is_safe_public_url: имя резолвится и в публичный, и в приватный — "
              "отклонено целиком (не «хотя бы один публичный», защита от DNS rebinding)",
              not ws.is_safe_public_url("http://rebind.example/"))

    def _fake_ipv6_loopback(host, *a, **kw):
        return [(10, 1, 6, '', ('::1', 0, 0, 0))]

    with mock.patch("socket.getaddrinfo", _fake_ipv6_loopback):
        check("is_safe_public_url: IPv6 ::1 — отклонён",
              not ws.is_safe_public_url("http://v6loop.example/"))

    def _fake_fail(host, *a, **kw):
        raise socket.gaierror("no such host")

    with mock.patch("socket.getaddrinfo", _fake_fail):
        check("is_safe_public_url: DNS не резолвится — отклонён, без исключения наружу",
              not ws.is_safe_public_url("http://nonexistent.invalid/"))

    check("fetch_page_text: приватный адрес отсекается ДО сети (пустая строка)",
          ws.fetch_page_text("http://127.0.0.1:6379/") == "")


def test_fetch_page_text_redirect_hops():
    section("C2. fetch_page_text: SSRF-фильтр на КАЖДОМ хопе редиректа")
    from app.features import web_search as ws

    class _FakeResp:
        def __init__(self, redirect_to=None, text=""):
            self.is_redirect = redirect_to is not None
            self.headers = {"location": redirect_to} if redirect_to else {}
            self.text = text

        def raise_for_status(self):
            pass

    class _FakeClientOneHop:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            if url == "https://public.example/start":
                return _FakeResp(redirect_to="http://127.0.0.1:8080/internal")
            return _FakeResp(text="<html><body>should not reach here</body></html>")

    seen_urls = []

    def _fake_is_safe(url):
        seen_urls.append(url)
        return "127.0.0.1" not in url and "localhost" not in url

    with mock.patch.object(ws, "is_safe_public_url", side_effect=_fake_is_safe), \
         mock.patch.object(ws.httpx, "Client", _FakeClientOneHop):
        result = ws.fetch_page_text("https://public.example/start")
    check("fetch_page_text: редирект публичный->loopback — итог пустая строка",
          result == "")
    check("fetch_page_text: is_safe_public_url вызван и на URL редиректа тоже "
          "(не только на исходном)",
          any("127.0.0.1" in u for u in seen_urls))

    class _FakeClientLoop:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return _FakeResp(redirect_to="https://public.example/next")

    with mock.patch.object(ws, "is_safe_public_url", return_value=True), \
         mock.patch.object(ws.httpx, "Client", _FakeClientLoop):
        result = ws.fetch_page_text("https://public.example/start")
    check("fetch_page_text: бесконечная цепочка редиректов обрывается по MAX_REDIRECTS "
          "(не виснет, пустой результат)", result == "")


# ════════════ C3. web_search: DNS rebinding — коннект по резолву проверки ════════════

def test_web_search_dns_rebinding_pinned_connect():
    section("C3. get_with_safe_redirects: коннект по адресу, прошедшему проверку "
            "(без повторного резолва — DNS rebinding)")
    from app.features import web_search as ws

    # «Сервер» маршрутизирует по Host — как настоящий: URL приходит с IP,
    # а какую страницу вернуть, решает исходное имя в заголовке.
    class _FakeResp:
        def __init__(self, redirect_to=None, text=""):
            self.is_redirect = redirect_to is not None
            self.headers = {"location": redirect_to} if redirect_to else {}
            self.text = text

        def raise_for_status(self):
            pass

    pin_calls = []
    pin_pages = {}

    class _FakePinnedClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, extensions=None):
            headers = headers or {}
            pin_calls.append({"url": url, "headers": dict(headers),
                               "extensions": dict(extensions or {})})
            return pin_pages.get(headers.get("Host", ""), _FakeResp(text=""))

    def _resolve_to(mapping, default="93.184.216.34"):
        calls = []

        def _fake(host, *a, **kw):
            calls.append(host)
            ip = mapping.get(host, default)
            return [(2, 1, 6, "", (ip, 0))]
        _fake.calls = calls
        return _fake

    # 1) публичный резолв → запрос ушёл на IP, Host/sni_hostname = имя,
    #    путь и query сохранены, финальный URL для склейки ссылок — по имени
    pin_calls.clear()
    pin_pages.clear()
    pin_pages["public.example"] = _FakeResp(text="ok")
    resolve_fn = _resolve_to({})
    with mock.patch("socket.getaddrinfo", resolve_fn), \
         mock.patch.object(ws.httpx, "Client", _FakePinnedClient):
        client = ws.httpx.Client(follow_redirects=False)
        resp, final_url = ws.get_with_safe_redirects(
            client, "https://public.example/path?q=1")
    check("pinned GET: резолв дал публичный IP — запрос ушёл на URL с этим IP",
          resp is not None and pin_calls
          and pin_calls[0]["url"] == "https://93.184.216.34/path?q=1")
    check("pinned GET: Host — исходное имя",
          pin_calls[0]["headers"].get("Host") == "public.example")
    check("pinned GET: extensions.sni_hostname (SNI + проверка сертификата) — "
          "исходное имя", pin_calls[0]["extensions"].get("sni_hostname")
          == "public.example")
    check("pinned GET: финальный URL — по имени (для склейки относительных ссылок)",
          final_url == "https://public.example/path?q=1")
    check("pinned GET: между проверкой и запросом — ровно один резолв на хоп "
          "(не два разных, как было бы при разрыве проверка/коннект)",
          resolve_fn.calls.count("public.example") == 1)

    # 2) редирект на имя, резолвящееся в приватный адрес — отказ ДО запроса
    #    этого хопа (не только до чтения тела)
    pin_calls.clear()
    pin_pages.clear()
    pin_pages["public.example"] = _FakeResp(
        redirect_to="https://internal.example/secret")
    resolve_fn2 = _resolve_to({"internal.example": "10.0.0.5"})
    with mock.patch("socket.getaddrinfo", resolve_fn2), \
         mock.patch.object(ws.httpx, "Client", _FakePinnedClient):
        client2 = ws.httpx.Client(follow_redirects=False)
        resp2, why2 = ws.get_with_safe_redirects(
            client2, "https://public.example/start")
    check("редирект на приватный адрес: отказ, причина называет хоп",
          resp2 is None and "internal.example" in why2)
    check("редирект на приватный адрес: GET на этот хоп не выполнен",
          len(pin_calls) == 1)  # только первый (безопасный) хоп реально запрошен

    # 3) IPv6 публичный — адрес соединения в квадратных скобках
    pin_calls.clear()
    pin_pages.clear()
    pin_pages["v6.example"] = _FakeResp(text="ok")

    # 2001:4860:4860::8888 — публичный IPv6-адрес; 2001:db8::/32 для этого
    # не годится — это документационный префикс RFC 3849, ipaddress считает
    # его приватным (is_private=True), а тест проверяет именно отклонение.
    def _resolve_v6(host, *a, **kw):
        return [(10, 1, 6, "", ("2001:4860:4860::8888", 0, 0, 0))]

    with mock.patch("socket.getaddrinfo", _resolve_v6), \
         mock.patch.object(ws.httpx, "Client", _FakePinnedClient):
        client3 = ws.httpx.Client(follow_redirects=False)
        resp3, _ = ws.get_with_safe_redirects(client3, "https://v6.example/x")
    check("IPv6: адрес соединения — в квадратных скобках",
          resp3 is not None and pin_calls
          and pin_calls[0]["url"] == "https://[2001:4860:4860::8888]/x")

    # 4) fetch_page_text целиком, на фейковом клиенте (без сети)
    pin_calls.clear()
    pin_pages.clear()
    pin_pages["public.example"] = _FakeResp(
        text="<html><body><p>Привет мир</p></body></html>")
    with mock.patch("socket.getaddrinfo", resolve_fn), \
         mock.patch.object(ws.httpx, "Client", _FakePinnedClient):
        text = ws.fetch_page_text("https://public.example/article")
    check("fetch_page_text: текст получен через пиннинг-путь end-to-end",
          "Привет мир" in text)
    check("fetch_page_text: GET тоже ушёл на IP с Host исходного имени",
          pin_calls and pin_calls[0]["url"].startswith("https://93.184.216.34")
          and pin_calls[0]["headers"].get("Host") == "public.example")


# ════════════ D. query_rewriter: анафора перед пунктуацией ════════════

def test_query_rewriter_punctuation():
    section("D. query_rewriter._is_self_contained — местоимение перед знаком препинания")
    from app.features import query_rewriter as qr

    check("«что его?» — анафора перед '?' распознана",
          not qr._is_self_contained("что его?"))
    check("«расскажи про него.» — анафора перед '.' распознана",
          not qr._is_self_contained("расскажи про него."))
    check("«ты знаком с ней!» — анафора перед '!' распознана",
          not qr._is_self_contained("ты знаком с ней!"))
    check("«мне нравится эта игра?» — составной маркер перед '?' распознан",
          not qr._is_self_contained("мне нравится эта игра?"))
    check("контрольная группа без пунктуации по-прежнему матчится (регрессия)",
          not qr._is_self_contained("что его"))
    check("«как дела?» — самодостаточно (нет анафоры, знак препинания не влияет)",
          qr._is_self_contained("как дела?"))
    check("«расскажи про Симона» — самодостаточно (не анафора)",
          qr._is_self_contained("расскажи про Симона"))


# ════════════ F. local_router: потокобезопасный singleton ════════════

def test_local_router_singleton_lock():
    section("F. local_router.get_local_router — потокобезопасная ленивая инициализация")
    from app.core import local_router as lr

    orig = lr._local_router
    lr._local_router = None
    created = []

    def _slow_init(self, *a, **kw):
        # Окно для гонки + без реальной сети (не ходим в Ollama)
        time.sleep(0.05)
        created.append(1)
        self.base_url = "http://localhost:11434"
        self.model = "test-model"
        self.timeout = 1.0
        self._personas = {}
        self._client = None
        self._last_check = 0.0
        self._available = False
        self._webchats = {}

    try:
        with mock.patch.object(lr.LocalLLMRouter, "__init__", _slow_init):
            results = []
            lock = threading.Lock()

            def _get():
                r = lr.get_local_router()
                with lock:
                    results.append(r)

            threads = [threading.Thread(target=_get) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
        check("get_local_router: конкурентные вызовы создали РОВНО ОДИН экземпляр "
              "(не double-init из разных потоков)", len(created) == 1)
        check("get_local_router: все 10 потоков получили ОДИН И ТОТ ЖЕ объект",
              len(results) == 10 and len({id(r) for r in results}) == 1)
    finally:
        lr._local_router = orig


# ════════════ G. file_sender: код без тега языка + утечка tmp ════════════

def test_file_sender_untagged_and_leak():
    section("G. file_sender: код-блок без языка + утечка tmp при сбое")
    from app.features import file_sender as fs

    # ── код-блок БЕЗ языкового тега ──
    text = "Вот файл:\n```\nprint(1)\nprint(2)\n```\n"
    blocks = fs._extract_code_blocks(text)
    check("_extract_code_blocks: код-блок БЕЗ языка распознан (раньше \\w+ требовал тег)",
          len(blocks) == 1 and blocks[0][0] == "" and "print(1)" in blocks[0][1])
    check("_get_extension(''): .txt, а не пустое расширение (\".\")",
          fs._get_extension("") == ".txt")

    messages, files = fs.prepare_response(text)
    try:
        check("prepare_response: untagged код-блок ушёл файлом с расширением .txt",
              files is not None and any(fn.endswith(".txt") for _, fn in files))
    finally:
        if files:
            fs.cleanup_files(files)
            check("cleanup_files: файл(ы) untagged-блока удалены",
                  all(not Path(fp).exists() for fp, _ in files))

    # ── частично записанные tmp-файлы подчищаются при сбое посреди записи ──
    multi_text = "```python\nprint('ok')\n```\n\n```js\nconsole.log(1)\n```\n"
    orig_write = fs._write_temp_file
    written_paths = []
    call_n = {"i": 0}

    def _flaky_write(content, filename):
        call_n["i"] += 1
        if call_n["i"] == 2:
            raise OSError("диск полон (симуляция)")
        p = orig_write(content, filename)
        written_paths.append(p)
        return p

    with mock.patch.object(fs, "_write_temp_file", side_effect=_flaky_write):
        try:
            fs.prepare_response(multi_text)
            check("prepare_response: сбой записи второго файла пробрасывается наружу", False)
        except OSError:
            check("prepare_response: сбой записи второго файла пробрасывается наружу", True)

    check("prepare_response: ровно один файл был реально записан до сбоя",
          len(written_paths) == 1)
    check("prepare_response: файл первого (успешно записанного) блока подчищен "
          "после сбоя второго — не остался висеть", not Path(written_paths[0]).exists())
    check("prepare_response: его tmp-директория тоже подчищена",
          not Path(written_paths[0]).parent.exists())


def main():
    test_clear_backup_chat_isolation()
    test_memory_wipe_learning_setup_multiuser()
    test_web_search_ssrf_filter()
    test_fetch_page_text_redirect_hops()
    test_web_search_dns_rebinding_pinned_connect()
    test_query_rewriter_punctuation()
    test_local_router_singleton_lock()
    test_file_sender_untagged_and_leak()

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

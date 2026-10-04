"""Треды веб-чата по диалогам: у каждого чата (Telegram, веб) свой тред на
сайте, вне области диалога — общий тред канала, как раньше.

05.10 Арродес в одной группе назвал человека из другой: тред deepseek был
один на персону, и сайт показывал модели промпты других чатов.

Проверяет: область диалога (contextvar, scoped_by, перенос в asyncio.to_thread
и в очередь LTM), адреса тредов по диалогам в web_llm_state.json, навигацию
вкладки при смене диалога (в т.ч. оба на home), стейтless-каналы без тредов,
снапшот/сброс/восстановление адресов для корзины очистки, область в
process_message. Браузер — заглушки, сети нет.
Запуск: python -m scripts.test_webchat_dialogs"""

import asyncio
import json
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path

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


def test_scope():
    section("1. Область диалога")
    from app.core.dialog_scope import current_dialog, dialog_scope, scoped_by

    check("вне области — None", current_dialog() is None)
    with dialog_scope(-100123):
        inner = current_dialog()
        with dialog_scope("web_user"):
            nested = current_dialog()
        after = current_dialog()
    check("ключ — строка; вложенная область и возврат к внешней",
          inner == "-100123" and nested == "web_user" and after == "-100123")
    check("после выхода — снова None", current_dialog() is None)
    with dialog_scope("A"), dialog_scope(None), dialog_scope(""):
        kept = current_dialog()
    check("пустой ключ область не меняет", kept == "A")

    @scoped_by(lambda chat_id, *a, **k: chat_id)
    def sync_fn(chat_id):
        return current_dialog()

    @scoped_by(lambda session: session["chat_id"])
    async def async_fn(session):
        # asyncio.to_thread копирует контекст: область доходит до потока
        return await asyncio.to_thread(current_dialog)

    @scoped_by(lambda session: session["chat_id"])
    def broken_key(session):
        return current_dialog()

    check("scoped_by: синхронная функция", sync_fn("c1") == "c1")
    check("scoped_by: корутина, область доходит до asyncio.to_thread",
          asyncio.run(async_fn({"chat_id": "c2"})) == "c2")
    check("scoped_by: ошибка ключа — вызов без области, не исключение",
          broken_key({}) is None)
    check("scoped_by: после вызова область снята", current_dialog() is None)

    seen = []
    with dialog_scope("T"):
        t = threading.Thread(target=lambda: seen.append(current_dialog()))
        t.start()
        t.join()
    check("threading.Thread контекст не наследует (поэтому область там — явно)",
          seen == [None])


def test_ltm_queue():
    section("2. Очередь LTM переносит область вызывающего")
    from concurrent.futures import ThreadPoolExecutor
    from app.core.dialog_scope import current_dialog, dialog_scope
    from app.core.memory import LongTermMemory

    ltm = LongTermMemory.__new__(LongTermMemory)
    ltm._serial_lock = threading.Lock()
    ltm._serial = {}
    pool = ThreadPoolExecutor(max_workers=2)
    ltm._get_executor = lambda: pool
    got = []
    gate = threading.Event()

    def first():
        gate.wait(5)
        got.append(("first", current_dialog()))

    def second():
        got.append(("second", current_dialog()))

    with dialog_scope("chatA"):
        ltm._submit_serial("u1", first)
    # Вторая задача того же пользователя ждёт первую в очереди и стартует
    # из её потока — область у неё своя, с момента постановки
    with dialog_scope("chatB"):
        ltm._submit_serial("u1", second)
    gate.set()
    deadline = time.time() + 5
    while len(got) < 2 and time.time() < deadline:
        time.sleep(0.02)
    pool.shutdown(wait=True)
    check("задача из очереди видит область, в которой её поставили",
          got == [("first", "chatA"), ("second", "chatB")])


def test_webchat_threads():
    section("3. WebChatLLM: тред на диалог")
    from app.core.dialog_scope import dialog_scope
    from app.features import browser_actions as ba
    from app.features import web_llm as wl

    tmp = Path(tempfile.mkdtemp(prefix="webchat_dialogs_"))
    home = wl.ADAPTERS["deepseek"]["home"]
    url_a = "https://chat.deepseek.com/a/chat/s/aaa"
    url_b = "https://chat.deepseek.com/a/chat/s/bbb"
    url_shared = "https://chat.deepseek.com/a/chat/s/shared"

    llm = wl.WebChatLLM("deepseek", base_dir=tmp / "cc")
    with dialog_scope("A"):
        llm._remember_chat_url(url_a)
        a_seen = llm._chat_url()
    with dialog_scope("B"):
        b_before = llm._chat_url()
        llm._remember_chat_url(url_b)
    llm._remember_chat_url(url_shared)
    st = json.loads((tmp / "cc" / "web_llm_state.json").read_text("utf-8"))
    entry = st["sites"]["deepseek"]
    check("адрес треда запоминается в своём диалоге",
          a_seen == url_a and entry["chat_urls"] == {"A": url_a, "B": url_b})
    check("у нового диалога своего треда нет (не берёт чужой и не общий)",
          b_before is None)
    check("вне области — общий тред канала (chat_url), как раньше",
          entry["chat_url"] == url_shared and llm._chat_url() == url_shared)
    with dialog_scope("A"):
        llm._set_chat_url("")
        a_after = llm._chat_url()
    with dialog_scope("B"):
        b_after = llm._chat_url()
    check("сброс сломанного треда — только у своего диалога",
          a_after is None and b_after == url_b and llm._chat_url() == url_shared)

    # Навигация вкладки при смене диалога
    calls = {"open": [], "nav": []}
    tab_at = {"url": ""}
    saved = (ba.open_new_tab, ba.navigate_tab, ba.tab_url, wl.FRESH_CHAT_SETTLE_SEC)

    def _open(url, **kw):
        calls["open"].append(url)
        tab_at["url"] = url
        return 77

    def _nav(url, tab_id=None):
        calls["nav"].append(url)
        tab_at["url"] = url

    ba.open_new_tab, ba.navigate_tab = _open, _nav
    ba.tab_url = lambda *a, **kw: tab_at["url"]
    wl.FRESH_CHAT_SETTLE_SEC = 0
    try:
        nav = wl.WebChatLLM("deepseek", base_dir=tmp / "nav")
        nav._after_nav = lambda ba_: None
        nav._challenge_check = lambda ba_, tid: False
        nav._remember_tab_snap = lambda ba_, tid: None
        with dialog_scope("A"):
            nav._set_chat_url(url_a)
        with dialog_scope("A"):
            nav._ensure_chat()
        check("диалог A: вкладка открыта на его треде",
              calls["open"] == [url_a] and calls["nav"] == [])
        with dialog_scope("A"):
            nav._ensure_chat()
        check("тот же диалог — без навигации", calls["nav"] == [])
        with dialog_scope("B"):
            nav._ensure_chat()
        check("диалог B без треда — навигация на home (новый чат), не в тред A",
              calls["nav"] == [home])
        with dialog_scope("C"):
            nav._ensure_chat()
        check("C после B: обе цели home, но тред чужой — навигация всё равно",
              calls["nav"] == [home, home])
        with dialog_scope("A"):
            nav._ensure_chat()
        check("возврат к A — навигация на его тред",
              calls["nav"] == [home, home, url_a])
        nav._ensure_chat()
        check("вызов вне области после A — навигация (общий тред — другой чат)",
              calls["nav"] == [home, home, url_a, home])

        # Стейтless-канал (поисковик): треды диалогов не заводятся
        g = wl.WebChatLLM("google", base_dir=tmp / "g", channel="search")
        with dialog_scope("A"):
            check("стейтless-канал: диалог не учитывается",
                  g.stateless and g._dialog() is None)
    finally:
        ba.open_new_tab, ba.navigate_tab, ba.tab_url, wl.FRESH_CHAT_SETTLE_SEC = saved


def test_snapshot_roundtrip():
    section("4. Корзина очистки: снапшот, сброс, восстановление")
    from app.core.dialog_scope import dialog_scope
    from app.core import paths
    from app.features import web_llm as wl
    import os

    tmp = Path(tempfile.mkdtemp(prefix="webchat_snap_"))
    old = os.environ.get("VPC_DATA_DIR")
    os.environ["VPC_DATA_DIR"] = str(tmp)
    try:
        ctx = "api_probe"
        llm = wl.WebChatLLM("deepseek", context=ctx)
        side = wl.WebChatLLM("deepseek", context=ctx, channel="side")
        llm._remember_chat_url("https://chat.deepseek.com/a/chat/s/shared")
        with dialog_scope("web_user"):
            llm._remember_chat_url("https://chat.deepseek.com/a/chat/s/w1")
            side._remember_chat_url("https://chat.deepseek.com/a/chat/s/w1side")
        snap = wl.collect_chat_urls(ctx)
        check("снапшот: общий тред и треды диалогов всех каналов",
              snap == {"deepseek": "https://chat.deepseek.com/a/chat/s/shared",
                       "deepseek@@web_user": "https://chat.deepseek.com/a/chat/s/w1",
                       "deepseek#side@@web_user": "https://chat.deepseek.com/a/chat/s/w1side"})
        n = wl.clear_chat_urls(ctx)
        with dialog_scope("web_user"):
            cleared = (llm._chat_url(), side._chat_url())
        check("сброс: все адреса, в том числе треды диалогов",
              n == 3 and cleared == (None, None) and llm._chat_url() is None)
        wl.restore_chat_urls(ctx, snap)
        with dialog_scope("web_user"):
            back = (llm._chat_url(), side._chat_url())
        check("undo: треды диалогов и общий тред вернулись на места",
              back == ("https://chat.deepseek.com/a/chat/s/w1",
                       "https://chat.deepseek.com/a/chat/s/w1side")
              and llm._chat_url() == "https://chat.deepseek.com/a/chat/s/shared"
              and wl.collect_chat_urls(ctx) == snap)
    finally:
        if old is None:
            os.environ.pop("VPC_DATA_DIR", None)
        else:
            os.environ["VPC_DATA_DIR"] = old
        assert paths.data_dir()  # модуль путей жив


def test_process_message_scope():
    section("5. process_message и слэш-команды — в области своего чата")
    from app.bot_instance import BotInstance
    from app.core.dialog_scope import current_dialog

    b = BotInstance.__new__(BotInstance)
    b.user_turn = lambda key: nullcontext()
    seen = []
    b._process_message_impl = lambda *a, **k: (seen.append(current_dialog()), "ok")[1]
    b._dispatch_command_impl = lambda *a, **k: (seen.append(current_dialog()), "ok")[1]
    b.process_message("привет", user_id="42", chat_id="-100777")
    b.process_message("привет", user_id="42", chat_id=None)
    b._dispatch_command("todo", "купить хлеб", "-100888", "42", "Тест")
    check("ответ в группе — область группы; в личке без chat_id — пользователя "
          "(ключ STM); команда — её чата",
          seen == ["-100777", "42", "-100888"])
    check("после хода область снята", current_dialog() is None)


def main():
    test_scope()
    test_ltm_queue()
    test_webchat_threads()
    test_snapshot_roundtrip()
    test_process_message_scope()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

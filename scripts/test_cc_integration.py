"""Стыки зон режима управления (волна 3): общий контракт полей действия
(origin / pending_from / via_search / retried / choose.resolve_ms) доходит
до needs_confirm и audit.jsonl; английское «scroll … on <site>» — сайт, а не
контейнер; живой прогон агента получает только написанное человеком (OCR
фото/текст файла — не ответ); открытие сайта агентом помечено origin=task.
Браузер и LLM не трогаются.

Запуск: python -m scripts.test_cc_integration
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cc_integ_smoke_")
    tmp = Path(tempfile.mkdtemp(prefix="cc_integ_data_"))
    import app.core.router as _net_router
    _net_router.internet_available = lambda: True
    _net_router._net_ok, _net_router._net_checked = True, float("inf")

    fails = 0
    total = 0

    def check(name, cond):
        nonlocal fails, total
        total += 1
        if not cond:
            fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")

    from app.features.computer_control import (
        ComputerControlManager, parse_scroll_request)

    def make(cfg=None):
        return ComputerControlManager(
            context="integ", config=cfg or {"confirm": False, "click": True},
            base_dir=tmp / f"m{os.urandom(3).hex()}")

    def aud(m):
        p = m.base_dir / "audit.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]

    # ── 1. Контракт → needs_confirm (connor: confirm:false) ──
    print("\n── 1. Контракт полей → needs_confirm ──")
    m = make()
    url = {"kind": "url", "value": "https://example.org/"}
    check("confirm:false: обычное открытие — без вопроса",
          m.needs_confirm(dict(url)) is False)
    check("via_search → подтверждение всегда",
          m.needs_confirm(dict(url, via_search=True)) is True)
    check("via_search у элемента multi → подтверждение всего multi",
          m.needs_confirm({"kind": "multi", "items": [
              dict(url), dict(url, via_search=True)]}) is True)
    check("force_confirm → подтверждение",
          m.needs_confirm({"kind": "click", "idx": 1, "element": "X",
                           "force_confirm": True}) is True)
    check("origin=marker → подтверждение",
          m.needs_confirm(dict(url, origin="marker")) is True)

    # ── 2. Контракт → audit.jsonl ──
    print("\n── 2. Контракт полей → аудит ──")
    m = make()
    m._audit("c1", {"kind": "click", "idx": 3, "element": "Купить",
                    "origin": "pending", "pending_from": "marker",
                    "retried": True, "via_search": False, "duration_ms": 12,
                    "choose": {"path": "score", "resolve_ms": 345,
                               "n_pool": 7}},
             True, "clicked")
    rec = (aud(m) or [{}])[-1]
    check("аудит: origin и pending_from (откуда пришло подтверждённое)",
          rec.get("origin") == "pending" and rec.get("pending_from") == "marker")
    check("аудит: retried / via_search / duration_ms",
          rec.get("retried") is True and rec.get("via_search") is False
          and rec.get("duration_ms") == 12)
    check("аудит: choose.resolve_ms / n_pool / path",
          rec.get("resolve_ms") == 345 and rec.get("n_pool") == 7
          and rec.get("path") == "score")

    # ── 3. Английское листание: предлог сайта и мера ──
    print("\n── 3. parse_scroll_request: on/in и «way» ──")
    check("«scroll down on youtube» → сайт youtube, без контейнера",
          parse_scroll_request("scroll down on youtube")
          == ("start", "youtube", None, None, None))
    check("«scroll way down pls» → обычное листание, не контейнер «way»",
          parse_scroll_request("scroll way down pls")
          == ("start", None, None, None, None))
    check("«scroll comments» — контейнер по-прежнему",
          parse_scroll_request("scroll comments")
          == ("start", None, None, None, "comments"))
    check("«пролистай комментарии вниз» — без изменений",
          parse_scroll_request("пролистай комментарии вниз")
          == ("start", None, None, None, "комментарии"))
    check("«промотай страницу на ютубе» — без изменений",
          parse_scroll_request("промотай страницу на ютубе")
          == ("start", "ютубе", None, None, None))

    # ── 4. Живой прогон агента: только написанное человеком ──
    print("\n── 4. _task_agent_turn: feed_text ──")
    from app.bot_instance import BotInstance

    class _Mem:
        def __init__(self):
            self.log = []

        def add_message(self, role, content, *a, **kw):
            self.log.append((role, content))

    def mkbot():
        b = BotInstance.__new__(BotInstance)
        b.memory = _Mem()
        b.proactive = None
        b.router = None
        b._cc_notifier = lambda chat_id, user_id=None: None
        fed = []
        b.task_agent = SimpleNamespace(
            feed=lambda c, t, r, notify=None, user_id=None, names=(): (
                fed.append(t), "FED")[1],
            start=lambda *a, **kw: "START",
            cancel=lambda c: None)
        return b, fed

    b, fed = mkbot()
    r = b._task_agent_turn("The user sent an image. Text on it:\nда",
                           "u", "c", "U", "ru", feed_text="")
    check("фото без подписи при живом прогоне — агенту не передаём, просим текст",
          fed == [] and r.startswith("Жду ответ текстом"))
    check("реплика записана в историю один раз (user + assistant)",
          [x[0] for x in b.memory.log] == ["user", "assistant"])
    b, fed = mkbot()
    r = b._task_agent_turn("пепперони\n\n[файл: … да …]", "u", "c", "U", "ru",
                           feed_text="пепперони")
    check("подпись есть — агент получает только её, не текст файла",
          fed == ["пепперони"] and r == "FED")
    b, fed = mkbot()
    r = b._task_agent_turn("да", "u", "c", "U", "ru")
    check("без feed_text — прежнее поведение (user_input)",
          fed == ["да"] and r == "FED")

    # process_message передаёт raw_user_text в живой прогон
    import inspect
    src = inspect.getsource(BotInstance._process_message_impl)
    check("диспетчер: живой прогон агента получает feed_text=raw_user_text",
          "feed_text=raw_user_text" in src)

    # ── 5. Открытие сайта агентом — origin=task ──
    print("\n── 5. TaskAgent._do_open: origin ──")
    from app.features.task_agent import TaskAgent
    executed = []
    fake = SimpleNamespace(
        # sites: адрес алиаса из конфига — открывается без «да»
        cc=SimpleNamespace(resolve_url=lambda t: None,
                           sites={"ютуб": "https://youtube.com/"},
                           resolve=lambda t, web_search=True: {
                               "kind": "url", "value": "https://youtube.com/"}),
        _execute=lambda run, chat_id, router, a, line: (
            executed.append(a), ("progress", None))[1],
        _record=lambda *a, **kw: None)
    TaskAgent._do_open(fake, {}, "c", None, "ютуб")
    check("открытие сайта агентом помечено origin=task",
          executed and executed[0].get("origin") == "task")

    print(f"\nИтог: {total} проверок, {fails} провалов")


if __name__ == "__main__":
    main()

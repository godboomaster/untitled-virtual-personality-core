"""Веб-поиск гонкой: Google AI Mode (веб-чат в пуле H) и DuckDuckGo параллельно.

Зачем. Прежний поиск (DDG + загрузка двух страниц) занимал 16-23 с (замер
23.09) и тащил в промпт до 6000 символов сырых страниц. AI Mode отвечает
собранной сводкой по выдаче Google за 4-6 с на тёплом браузере, но
иногда отказывает (фильтр контента) или недоступен (карантин, пул не
поднялся).

Политика — не «кто первый», а приоритет качества с дедлайном:
  1. обе ноги стартуют сразу (DDG — только сниппеты, без загрузки страниц);
  2. AI Mode ждём до AI_DEADLINE_SEC: успел — берём его;
  3. AI Mode отказал/упал раньше — берём DDG, как только он готов;
  4. дедлайн, AI Mode молчит — берём DDG, если готов, иначе ждём первого
     из двух до TOTAL_TIMEOUT_SEC.
Чистая гонка выбирала бы худший источник: сниппеты DDG часто приходят
раньше сводки Google.

Проигравшего не отменяем (поток нельзя прервать): его результат
выбрасывается, время пишется в лог. Канал AI Mode — stateless «search»
(свежий тред на вопрос), инстанс не ждёт своего лока: пока вкладка
дописывает брошенный ответ прошлого поиска, новый поиск идёт по DDG.

Включается у персон с основным провайдером-веб-чатом (браузерный пул и
так поднят); features.web_search_ai_mode: true/false — явный override.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Callable, Optional

logger = logging.getLogger(__name__)

AI_DEADLINE_SEC = 10.0      # сколько ждём сводку AI Mode, пока DDG уже готов
TOTAL_TIMEOUT_SEC = 15.0    # потолок всей гонки (бот ждёт web_future до 25 с)
AI_MAX_CHARS = 3000         # сводка AI Mode в промпт — не длиннее

# Свой пул: две ноги на поиск, несколько персон одновременно. Пул бота
# (_web_pool, 2 потока) держит только сам вызов race_search
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="web-race")

_ai_instances: dict[str, object] = {}
_ai_lock = threading.Lock()


def _ai_llm(context: str):
    """Инстанс AI Mode на канале search — один на контекст персоны (у него
    своя вкладка и лок)."""
    with _ai_lock:
        llm = _ai_instances.get(context)
        if llm is None:
            from app.features.web_llm import WebChatLLM
            llm = WebChatLLM("google", context=context, channel="search")
            _ai_instances[context] = llm
        return llm


def ai_mode_available() -> bool:
    """AI Mode не в карантине (антибот/лимит) — иначе ногу не запускаем."""
    try:
        from app.features.web_llm import site_quarantined
        return not site_quarantined("google")
    except Exception:
        return False


def ai_result(answer: str) -> dict:
    """Сводка AI Mode в формате результата поиска (format_web_results)."""
    text = answer.strip()
    if len(text) > AI_MAX_CHARS:
        text = text[:AI_MAX_CHARS].rsplit(" ", 1)[0] + " […]"
    return {"title": "Google AI Mode (сводка по выдаче Google)", "body": "",
            "href": "google.com (AI Mode)", "full_text": text,
            "_source": "google_ai"}


def race_search(query: str, *, context: str,
                ddg_search: Callable[[str], list],
                ai_ask: Optional[Callable[[str], Optional[str]]] = None,
                ai_deadline: float = AI_DEADLINE_SEC,
                total_timeout: float = TOTAL_TIMEOUT_SEC) -> list[dict]:
    """Результаты поиска по политике модуля. ddg_search(query) → list[dict]
    (сниппеты); ai_ask(query) → текст сводки | None (по умолчанию — AI Mode
    инстанса контекста). Пусто — оба источника ничего не дали."""
    t0 = time.monotonic()
    if ai_ask is None:
        llm = _ai_llm(context)
        ai_ask = lambda q: llm.ask_by_url(q, timeout=total_timeout, lock_timeout=0.0)  # noqa: E731

    times: dict[str, float] = {}

    def _timed(name: str, fn: Callable, *a):
        try:
            return fn(*a)
        finally:
            times[name] = time.monotonic() - t0

    ai_f: Future = _pool.submit(_timed, "ai", ai_ask, query)
    ddg_f: Future = _pool.submit(_timed, "ddg", ddg_search, query)

    def _ok(f: Future):
        """Результат ноги или None (ошибка/пусто) — только для завершённой."""
        try:
            r = f.result(timeout=0)
        except Exception:
            return None
        return r or None

    def _left(limit: float) -> float:
        return max(0.0, limit - (time.monotonic() - t0))

    winner, result = None, []
    # Шаг 1-3: ждём AI до дедлайна; AI упал раньше — сразу к DDG
    pending = {ai_f, ddg_f}
    while ai_f in pending and _left(ai_deadline) > 0:
        done, pending = wait(pending, timeout=_left(ai_deadline),
                             return_when=FIRST_COMPLETED)
        if ai_f in done:
            break
    ans = _ok(ai_f) if ai_f.done() else None
    if ans:
        winner, result = "google_ai", [ai_result(ans)]
    else:
        # AI не успел или не ответил: DDG — если уже готов или успеет до
        # общего потолка; AI, если он всё же успеет раньше DDG, — тоже в дело
        if _ok(ddg_f):
            winner, result = "ddg", list(_ok(ddg_f))
        rest = {f for f in (ai_f, ddg_f) if not f.done()}
        while not winner and rest and _left(total_timeout) > 0:
            done, rest = wait(rest, timeout=_left(total_timeout),
                              return_when=FIRST_COMPLETED)
            if ai_f in done and _ok(ai_f):
                winner, result = "google_ai", [ai_result(_ok(ai_f))]
            elif ddg_f in done and _ok(ddg_f):
                winner, result = "ddg", list(_ok(ddg_f))

    elapsed = time.monotonic() - t0

    def _leg(name: str, f: Future) -> str:
        if name not in times:
            return "ещё идёт"
        state = "есть" if _ok(f) else "пусто/ошибка"
        return f"{times[name]:.1f}s ({state})"

    logger.info(f"[WEB_RACE] '{query[:50]}': победил {winner or 'никто'} за "
                f"{elapsed:.1f}s | ai {_leg('ai', ai_f)} | ddg {_leg('ddg', ddg_f)}")
    # Проигравший, который ещё идёт, — дописываем его время в лог по завершении
    for name, f in (("ai", ai_f), ("ddg", ddg_f)):
        if not f.done():
            f.add_done_callback(lambda fut, n=name: logger.info(
                f"[WEB_RACE] '{query[:50]}': отставшая нога {n} завершилась "
                f"за {times.get(n, 0.0):.1f}s ({'есть' if _ok(fut) else 'пусто/ошибка'}) — отброшена"))
    return result

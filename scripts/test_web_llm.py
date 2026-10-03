"""Smoke-тесты web_llm (веб-чат как LLM-провайдер): склейка промпта,
квота/пейсинг (per-site состояние), постоянный чат (один URL на сайт,
ожидание НОВОГО блока ответа), восстановление при сломанном чате,
extract_json, интеграция webchat-токенов в ModelRouter.
Браузерные вызовы (browser_actions) — моки.
Запуск: python -m scripts.test_web_llm"""

import json
import logging
import multiprocessing
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def _mp_quota_worker(base_dir: str, site: str, channel: str, n: int):
    """Воркер отдельного процесса (multiprocessing, не потока): threading.Lock
    (_STATE_FILE_LOCKS) сериализует запись web_llm_state.json только внутри
    одного процесса, и несколько процессов персон на общий data/ без
    межпроцессного лока теряли бы параллельный инкремент квоты
    (read-modify-write). Модульная функция, не замыкание внутри main(): multiprocessing (spawn,
    дефолт на macOS) подгружает воркер через pickle по имени, а замыкания так
    не передаются."""
    from app.features import web_llm as _wl
    inst = _wl.WebChatLLM(site, base_dir=Path(base_dir), channel=channel)
    for _ in range(n):
        inst._quota_bump()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="webllm_data_"))
    ok = 0
    # Тест не должен зависеть от живой сети: internet_available() — TCP-пробы
    # 1.1.1.1:443/8.8.8.8:53, которые в песочнице/за файрволом молчат, и тогда
    # webchat-ветка роутера и резолв сайтов честно «офлайн» → ложные FAIL
    import app.core.router as _net_router
    _real_internet_available = _net_router.internet_available
    _net_router.internet_available = lambda: True
    _net_router._net_ok, _net_router._net_checked = True, float("inf")

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    # Проба интернета: слабая сеть не должна ложно давать «офлайн» (иначе
    # режим управления откатывается на решения локальной модели) — офлайн
    # признаётся только после ДВУХ пустых серий подряд, кэш «офлайн» короткий,
    # а успешный ответ облака/веб-чата подтверждает онлайн без пробы
    _orig_probe = _net_router._probe_round
    _rounds = []
    _net_router._probe_round = lambda: (_rounds.append(1), False)[1]
    _net_router._net_ok, _net_router._net_checked = None, 0.0
    _off = _real_internet_available()
    _n_off = len(_rounds)
    _net_router._probe_round = lambda: (_rounds.append(1), True)[1]
    _cached = _real_internet_available()  # кэш «офлайн» ещё жив — без пробы
    _n_cached = len(_rounds)
    _net_router.note_internet_ok()
    _on = _real_internet_available()
    _n_on = len(_rounds)
    _net_router._net_checked = 0.0  # кэш истёк — проба снова, сеть вернулась
    _back = _real_internet_available()
    # Устаревший «онлайн» отдаётся сразу, проба — в фоновом потоке: ждём её
    # конца, иначе счётчик серий гоняется с потоком, а его вердикт
    # (_net_checked = now) перетирает float("inf") ниже
    _pending = _net_router._net_refresh_done
    if _pending is not None:
        _pending.wait(5)
    _net_router._probe_round = _orig_probe
    _net_router._net_ok, _net_router._net_checked = True, float("inf")
    check("probe: офлайн — после двух пустых серий подряд",
          _off is False and _n_off == 2)
    check("probe: кэш «офлайн» жив — повторной пробы нет",
          _cached is False and _n_cached == _n_off)
    check("probe: ответ облака подтверждает онлайн без пробы; проба после "
          "истечения кэша — онлайн",
          _on is True and _n_on == _n_off and _back is True
          and len(_rounds) == _n_on + 1)
    check("probe: таймаут пробы ≥ 3 с (слабая сеть ≠ офлайн)",
          _net_router._NET_PROBE_TIMEOUT_SEC >= 3.0
          and _net_router._NET_OFFLINE_TTL_SEC < _net_router._NET_CHECK_TTL_SEC)

    from app.features import web_llm as wl
    from app.features import browser_actions as ba

    # ── 1. Склейка OpenAI-messages в один промпт ──
    joined = wl.WebChatLLM._join_messages([
        {"role": "system", "content": "Ты — Коннор."},
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "здорово"},
        {"role": "user", "content": [{"type": "image"}]},  # мультимодальное — пропуск
        {"role": "user", "content": "как дела?"},
    ])
    check("join: system — блоком инструкций, роли с префиксами, image пропущен",
          joined.startswith("Instructions") and "Ты — Коннор." in joined
          and "User: привет" in joined
          and "Assistant: здорово" in joined
          and joined.endswith("User: как дела?")
          and "image" not in joined)

    # ── 2. extract_json ──
    check("extract_json: чистый/ограждённый/в прозе/массив/мусор",
          wl.extract_json('{"a": 1}') == {"a": 1}
          and wl.extract_json('Вот:\n```json\n{"a": 2}\n```') == {"a": 2}
          and wl.extract_json('ответ: [1, 2]!') == [1, 2]
          and wl.extract_json("никакого json") is None
          and wl.extract_json("") is None)

    # Опрос в тестах без пауз — ускоряет прогон
    wl.POLL_SEC = 0

    # Якорное чтение (answer_blocks_after) по умолчанию «не находит якорь» —
    # старые проверки идут по baseline-пути; якорные сценарии стабят его сами
    _aba_orig = ba.answer_blocks_after
    ba.answer_blocks_after = lambda *a, **kw: (None, "", True)
    # Перезапуск браузера при залипшей отправке — мок: реальный трогал бы
    # Chrome на машине, где гоняют тесты
    _rb_orig = ba.restart_browser
    restarts = []
    ba.restart_browser = lambda reason="", **kw: restarts.append(reason) or True
    # Перезагрузка вкладки — первая ступень лечения залипшей отправки
    # (перезапуск всего Chrome — только если reload недавно был и не помог)
    _rl_orig = ba.reload_tab
    reloads = []
    ba.reload_tab = lambda tab_id=None, **kw: reloads.append(tab_id) or ("", "")

    # ── 3. Состояние per-site: квота (окно per_hour × QUOTA_WINDOW_HOURS),
    #       изоляция сайтов, миграция формата ──
    # Квота opt-in: дефолт снят, поэтому лимит задаём явно
    today = wl.time.strftime("%Y-%m-%d")
    llm = wl.WebChatLLM("qwen", base_dir=tmp / "q1", quota_per_hour=40)
    (tmp / "q1" / "web_llm_state.json").write_text(json.dumps(
        {"sites": {"qwen": {"window_start": wl.time.time(),
                            "count": 40 * wl.QUOTA_WINDOW_HOURS,
                            "last_ts": 0}}}),
        encoding="utf-8")
    called = []
    _cfs = ba.chat_fill_send
    ba.chat_fill_send = lambda *a, **kw: called.append(1) or "sent"
    try:
        res = llm.get_response([{"role": "user", "content": "привет"}])
        check("quota: потолок окна (40/ч × 1ч) — None, браузер не вызывался",
              res is None and not called)
    finally:
        ba.chat_fill_send = _cfs
    # Окно истекло — счётчик обнуляется, лимит обновляется полностью
    (tmp / "q1" / "web_llm_state.json").write_text(json.dumps(
        {"sites": {"qwen": {"window_start": wl.time.time()
                            - (wl.QUOTA_WINDOW_HOURS * 3600 + 1),
                            "count": 9999, "last_ts": 0}}}),
        encoding="utf-8")
    check("quota: окно истекло — лимит обновился", llm._quota_check())
    # Свой лимит: 2/час → ёмкость окна 2 × QUOTA_WINDOW_HOURS
    llm_cap = wl.WebChatLLM("qwen", base_dir=tmp / "qcap", quota_per_hour=2)
    check("quota: ёмкость окна = per_hour × окно",
          llm_cap._quota_capacity() == 2 * wl.QUOTA_WINDOW_HOURS)
    # Снятый лимит (None, дефолт): счётчик не мешает
    llm_free = wl.WebChatLLM("qwen", base_dir=tmp / "q1", quota_per_hour=None)
    check("quota: снятый лимит — ёмкость бесконечна, вызов разрешён",
          llm_free._quota_capacity() is None and llm_free._quota_check())

    llm_q = wl.WebChatLLM("qwen", base_dir=tmp / "shared")
    llm_d = wl.WebChatLLM("deepseek", base_dir=tmp / "shared")
    llm_q._save_state({"chat_url": "https://chat.qwen.ai/c/1",
                       "date": today, "count": 5})
    check("state: сайты изолированы в одном файле",
          llm_q._chat_url() == "https://chat.qwen.ai/c/1"
          and llm_d._load_state() == {})
    (tmp / "shared" / "web_llm_state.json").write_text(json.dumps(
        {"date": today, "count": 999, "last_ts": 0}), encoding="utf-8")
    check("state: старый плоский формат игнорируется (квота с нуля)",
          llm_q._load_state() == {} and llm_q._quota_check())

    # ── 3b. _same_page / _remember_chat_url ──
    check("same_page: слеш и query не различают страницу",
          wl.WebChatLLM._same_page("https://chat.qwen.ai/c/1?x=2",
                                   "https://chat.qwen.ai/c/1/")
          and not wl.WebChatLLM._same_page("https://chat.qwen.ai/c/1",
                                           "https://chat.qwen.ai/c/2"))
    llm_r = wl.WebChatLLM("qwen", base_dir=tmp / "rem")
    llm_r._remember_chat_url("https://chat.qwen.ai/")       # home — не чат
    llm_r._remember_chat_url("https://example.com/c/9")     # чужой хост
    llm_r._remember_chat_url("https://chat.qwen.ai/c/new-chat")  # «новый чат»
    check("remember: home, чужой хост и страница new-chat не запоминаются",
          llm_r._chat_url() is None)
    llm_r._remember_chat_url("https://chat.qwen.ai/c/abc")
    check("remember: постоянный URL чата запоминается",
          llm_r._chat_url() == "https://chat.qwen.ai/c/abc")

    # ── 4. Счастливый путь: постоянный чат — отправка, захват URL,
    #       повторный вызов БЕЗ перенавигации, ждём новый блок ──
    llm2 = wl.WebChatLLM("qwen", base_dir=tmp / "q2")
    calls = {"nav": [], "open": [], "send": []}
    _open, _nav = ba.open_new_tab, ba.navigate_tab
    _send, _read = ba.chat_fill_send, ba.last_block_text
    _cnt, _url = ba.count_blocks, ba.tab_url
    ba.open_new_tab = lambda url, **kw: (calls["open"].append(url), 42)[1]
    ba.navigate_tab = lambda url, tab_id=None: calls["nav"].append(url)
    ba.tab_url = lambda *a, **kw: "https://chat.qwen.ai/c/chat-1"
    ba.chat_fill_send = lambda host, tab_id, sel, text: (
        calls["send"].append((tab_id, sel, text)), "sent")[1]
    counts = iter([])
    texts = iter([])
    md_flags = []
    ba.count_blocks = lambda *a, **kw: next(counts)
    def _lbt_happy(host, tid, sels=None, **kw):
        # user-блоки читает _send_verified: отдаём последний отправленный
        # промпт (подтверждение доставки), итератор ответов не трогаем
        if sels and list(sels) == (llm2.adapter.get("user") or []):
            return calls["send"][-1][2] if calls["send"] else ""
        md_flags.append(kw.get("markdown"))
        return next(texts)
    ba.last_block_text = _lbt_happy
    try:
        counts = iter([0, 1, 1, 1, 1, 1])  # baseline=0, затем новый блок
        texts = iter(["", "Рабо", "Работает", "Работает", "Работает"])
        res2 = llm2.get_response([{"role": "user", "content": "ответь: работает"}])
        check("happy: вкладка открыта на home, промпт ушёл, ответ дочитан "
              "до стабильного, URL чата запомнен",
              res2 == "Работает"
              and calls["open"] == [wl.ADAPTERS["qwen"]["home"]]
              and len(calls["send"]) == 1
              and calls["send"][0][0] == 42
              and calls["send"][0][1] == wl.ADAPTERS["qwen"]["input"]
              and "ответь: работает" in calls["send"][0][2]
              and llm2._chat_url() == "https://chat.qwen.ai/c/chat-1")
        counts = iter([1, 2, 2, 2])  # baseline=1 (старый ответ в DOM)
        texts = iter(["ок", "ок", "ок", "ок"])  # baseline_text + 3 замера
        res3 = llm2.get_response([{"role": "user", "content": "ещё"}])
        check("happy: повторный вызов — тот же чат, БЕЗ навигации и новой "
              "вкладки; прошлый ответ не засчитан (ждали блок > baseline)",
              res3 == "ок" and calls["nav"] == []
              and len(calls["open"]) == 1 and len(calls["send"]) == 2)
        check("happy: тики опроса — plain-текст (дёшево), markdown "
              "снимается один раз в конце, по стабилизации",
              md_flags == [False] * 4 + [True] + [False] * 3 + [True])
    finally:
        ba.open_new_tab, ba.navigate_tab = _open, _nav
        ba.chat_fill_send, ba.last_block_text = _send, _read
        ba.count_blocks, ba.tab_url = _cnt, _url

    # ── 4b. Канал side (фоновые задачи): полная стабилизация STABLE_POLLS ──
    llm2s = wl.WebChatLLM("qwen", base_dir=tmp / "q2s", channel="side")
    sent_s, md_s = [], []
    _o4, _n4 = ba.open_new_tab, ba.navigate_tab
    _s4, _r4, _c4, _u4 = (ba.chat_fill_send, ba.last_block_text,
                          ba.count_blocks, ba.tab_url)
    ba.open_new_tab = lambda url, **kw: 43
    ba.navigate_tab = lambda *a, **kw: None
    ba.tab_url = lambda *a, **kw: "https://chat.qwen.ai/c/side-1"
    ba.chat_fill_send = lambda host, tab_id, sel, text: (
        sent_s.append(text), "sent")[1]
    counts_s = iter([0, 1, 1, 1, 1, 1, 1])
    texts_s = iter(["", "Чер", "Черновик", "Черновик", "Черновик"])
    ba.count_blocks = lambda *a, **kw: next(counts_s)

    def _lbt_side(host, tid, sels=None, **kw):
        if sels and list(sels) == (llm2s.adapter.get("user") or []):
            return sent_s[-1] if sent_s else ""
        md_s.append(kw.get("markdown"))
        return next(texts_s)
    ba.last_block_text = _lbt_side
    try:
        res_s = llm2s.get_response([{"role": "user", "content": "черновик"}])
        check("side: фоновый канал — полные STABLE_POLLS замера "
              "стабильности (markdown после 2 одинаковых подряд)",
              res_s == "Черновик" and md_s == [False] * 5 + [True])
    finally:
        ba.open_new_tab, ba.navigate_tab = _o4, _n4
        ba.chat_fill_send, ba.last_block_text = _s4, _r4
        ba.count_blocks, ba.tab_url = _c4, _u4

    # ── 4c. done_selector (kimi): main берёт ответ сразу по маркеру
    #       завершения — ноль стабилизационных тиков ──
    llm_k = wl.WebChatLLM("kimi", base_dir=tmp / "k1")
    sent_k, aba_k = [], []
    _o5, _n5 = ba.open_new_tab, ba.navigate_tab
    _s5, _r5, _c5, _u5 = (ba.chat_fill_send, ba.last_block_text,
                          ba.count_blocks, ba.tab_url)
    _a5 = ba.answer_blocks_after
    ba.open_new_tab = lambda url, **kw: 44
    ba.navigate_tab = lambda *a, **kw: None
    ba.tab_url = lambda *a, **kw: "https://www.kimi.ai/chat/k-1"
    ba.chat_fill_send = lambda host, tab_id, sel, text: (
        sent_k.append(text), "sent")[1]
    ba.count_blocks = lambda *a, **kw: 0

    def _lbt_kimi(host, tid, sels=None, **kw):
        if sels and list(sels) == (llm_k.adapter.get("user") or []):
            return sent_k[-1] if sent_k else ""
        return ""

    def _aba_kimi(host, tid, user_sels, ans_sels, marker, **kw):
        aba_k.append(kw.get("markdown"))
        return (1, "готово-md" if kw.get("markdown") else "готово", True)
    ba.last_block_text = _lbt_kimi
    ba.answer_blocks_after = _aba_kimi
    try:
        res_k = llm_k.get_response([{"role": "user", "content": "привет"}])
        check("done_selector: main-канал — ответ сразу по маркеру конца "
              "генерации (1 plain-тик + 1 markdown, без стабилизации)",
              res_k == "готово-md" and aba_k == [False, True])
    finally:
        ba.open_new_tab, ba.navigate_tab = _o5, _n5
        ba.chat_fill_send, ba.last_block_text = _s5, _r5
        ba.count_blocks, ba.tab_url = _c5, _u5
        ba.answer_blocks_after = _a5

    # ── 5. Тишина по таймауту → None (фолбэк вызывающего) ──
    llm3 = wl.WebChatLLM("deepseek", base_dir=tmp / "q3")
    _cfs2, _lbt2 = ba.chat_fill_send, ba.last_block_text
    _cnt2, _url2, _open2 = ba.count_blocks, ba.tab_url, ba.open_new_tab
    ba.open_new_tab = lambda url, **kw: 42
    ba.chat_fill_send = lambda *a, **kw: "sent"
    ba.count_blocks = lambda *a, **kw: 0   # новый блок не появляется
    ba.last_block_text = lambda *a, **kw: ""
    ba.tab_url = lambda *a, **kw: ""
    _to = wl.ANSWER_TIMEOUT_SEC
    wl.ANSWER_TIMEOUT_SEC = 0.01
    try:
        res4 = llm3.get_response([{"role": "user", "content": "hi"}],
                                 timeout=0.01)
        check("timeout: ответа нет — None, а не выдумка",
              res4 is None)
        check("stuck: сообщение не появилось в ленте → перезагрузка вкладки "
              "(перезапуск браузера — только если reload не помог)",
              reloads == [42] and not restarts and llm3._tab_id == 42)
    finally:
        ba.chat_fill_send, ba.last_block_text = _cfs2, _lbt2
        ba.count_blocks, ba.tab_url, ba.open_new_tab = _cnt2, _url2, _open2
        wl.ANSWER_TIMEOUT_SEC = _to

    # ── 5b. Виртуализованная лента (deepseek держит в DOM один обмен):
    #       счётчик блоков НЕ растёт — ответ ловим по смене текста ──
    llm_v = wl.WebChatLLM("deepseek", base_dir=tmp / "q5b")
    _ov, _sv = ba.open_new_tab, ba.chat_fill_send
    _lv, _uv, _nv = ba.last_block_text, ba.tab_url, ba.count_blocks
    ba.open_new_tab = lambda url, **kw: 42
    ba.chat_fill_send = lambda *a, **kw: "sent"
    ba.tab_url = lambda *a, **kw: "https://chat.deepseek.com/a/chat/s/v1"
    ba.count_blocks = lambda *a, **kw: 1  # в DOM всегда один блок ответа
    txts_v = iter(["старый ответ", "старый ответ",
                   "нов", "новый ответ", "новый ответ", "новый ответ"])
    def _lbt_v(host, tid, sels=None, **kw):
        if sels and list(sels) == (llm_v.adapter.get("user") or []):
            return "User: hi"
        return next(txts_v)
    ba.last_block_text = _lbt_v
    try:
        res_v = llm_v.get_response([{"role": "user", "content": "hi"}])
        check("virtualized: счётчик не растёт — ответ пойман по смене "
              "текста последнего блока", res_v == "новый ответ")
    finally:
        ba.open_new_tab, ba.chat_fill_send = _ov, _sv
        ba.last_block_text, ba.tab_url, ba.count_blocks = _lv, _uv, _nv

    # ── 6. Ошибка отправки → None ──
    llm4 = wl.WebChatLLM("qwen", base_dir=tmp / "q4")
    _cfs3, _open3 = ba.chat_fill_send, ba.open_new_tab
    _cnt3, _url3, _lbt3 = ba.count_blocks, ba.tab_url, ba.last_block_text
    ba.open_new_tab = lambda url, **kw: 42
    ba.count_blocks = lambda *a, **kw: 0
    ba.tab_url = lambda *a, **kw: ""
    ba.last_block_text = lambda *a, **kw: ""
    def _boom_send(*a, **kw):
        raise ba.BrowserUnavailable("поле чата не приняло ввод")
    ba.chat_fill_send = _boom_send
    try:
        # reload был недавно (уже лечил залипание) — сразу эскалация на
        # перезапуск браузера
        llm4._last_tab_reload_ts = wl.time.time()
        check("send fail: BrowserUnavailable → None",
              llm4.get_response([{"role": "user", "content": "hi"}],
                                timeout=0.01) is None)
        check("stuck: reload не помог → перезапуск браузера, вкладка забыта",
              len(restarts) == 1
              and "поле чата не приняло ввод" in restarts[0]
              and llm4._tab_id is None)
    finally:
        ba.chat_fill_send, ba.open_new_tab = _cfs3, _open3
        ba.count_blocks, ba.tab_url = _cnt3, _url3
        ba.last_block_text = _lbt3

    # ── 6b. Сохранённый чат сломался → сброс chat_url и свежий чат ──
    llm5 = wl.WebChatLLM("deepseek", base_dir=tmp / "q5")
    llm5._save_state({"chat_url": "https://chat.deepseek.com/a/chat/s/old"})
    flow = {"open": [], "nav": [], "send": 0}
    _open4, _nav4 = ba.open_new_tab, ba.navigate_tab
    _cfs4, _lbt4 = ba.chat_fill_send, ba.last_block_text
    _cnt4, _url4 = ba.count_blocks, ba.tab_url
    urls = iter(["https://chat.deepseek.com/a/chat/s/old",
                 "https://chat.deepseek.com/a/chat/s/cafe123"])
    ba.open_new_tab = lambda url, **kw: (flow["open"].append(url), 42)[1]
    ba.navigate_tab = lambda url, tab_id=None: flow["nav"].append(url)
    ba.tab_url = lambda *a, **kw: next(urls)
    def _flaky_send(*a, **kw):
        flow["send"] += 1
        if flow["send"] == 1:
            raise ba.BrowserUnavailable("поле чата не приняло ввод")
        return "sent"
    ba.chat_fill_send = _flaky_send
    cnts = iter([0, 1, 2, 2, 2])  # baseline: 0 (битая), 1 (свежая, с прошлым
                                    # блоком в DOM) → ждём блок > 1
    txts = iter(["ок"] * 5)  # baseline_text ×2 + 3 замера
    ba.count_blocks = lambda *a, **kw: next(cnts)
    def _lbt_rec(host, tid, sels=None, **kw):
        if sels and list(sels) == (llm5.adapter.get("user") or []):
            return "User: hi"
        return next(txts)
    ba.last_block_text = _lbt_rec
    try:
        res5 = llm5.get_response([{"role": "user", "content": "hi"}])
        check("recover: сломанный чат → home (новый чат), URL перезаписан",
              res5 == "ок"
              and flow["open"] == ["https://chat.deepseek.com/a/chat/s/old"]
              and flow["nav"] == [wl.ADAPTERS["deepseek"]["home"]]
              and llm5._chat_url() == "https://chat.deepseek.com/a/chat/s/cafe123")
    finally:
        ba.open_new_tab, ba.navigate_tab = _open4, _nav4
        ba.chat_fill_send, ba.last_block_text = _cfs4, _lbt4
        ba.count_blocks, ba.tab_url = _cnt4, _url4

    # ── 6c. Сайт вернул баннер ошибки (битый чат, parent_id не существует) →
    #       сброс chat_url и свежий чат, ответ дочитан уже там ──
    llm6 = wl.WebChatLLM("qwen", base_dir=tmp / "q6")
    llm6._save_state({"chat_url": "https://chat.qwen.ai/c/dead"})
    flow6 = {"nav": [], "send": 0}
    _open6, _nav6 = ba.open_new_tab, ba.navigate_tab
    _cfs6, _lbt6 = ba.chat_fill_send, ba.last_block_text
    _cnt6, _url6 = ba.count_blocks, ba.tab_url
    ba.open_new_tab = lambda url, **kw: 42
    ba.navigate_tab = lambda url, tab_id=None: flow6["nav"].append(url)
    ba.tab_url = lambda *a, **kw: "https://chat.qwen.ai/c/new1"
    ba.chat_fill_send = lambda *a, **kw: (
        flow6.__setitem__("send", flow6["send"] + 1), "sent")[1]
    oops = "Oops! There was an issue connecting to Qwen3.7-Plus."
    cnts6 = iter([1, 1, 1,      # попытка 1: baseline + 2 замера ошибки
                  0, 1, 1, 1, 1])  # попытка 2: baseline + 4 замера ответа
    txts6 = iter(["старый ответ", oops, oops,
                  "", "свеж", "свежий ответ", "свежий ответ", "свежий ответ"])
    ba.count_blocks = lambda *a, **kw: next(cnts6)
    def _lbt6(host, tid, sels=None, **kw):
        if sels and list(sels) == (llm6.adapter.get("user") or []):
            return "User: hi"
        return next(txts6)
    ba.last_block_text = _lbt6
    try:
        res6 = llm6.get_response([{"role": "user", "content": "hi"}])
        check("recover: баннер ошибки сайта → сброс чата, свежий чат, ответ",
              res6 == "свежий ответ"
              and flow6["send"] == 2
              and flow6["nav"] == [wl.ADAPTERS["qwen"]["home"]]
              and llm6._chat_url() == "https://chat.qwen.ai/c/new1")
    finally:
        ba.open_new_tab, ba.navigate_tab = _open6, _nav6
        ba.chat_fill_send, ba.last_block_text = _cfs6, _lbt6
        ba.count_blocks, ba.tab_url = _cnt6, _url6

    # ── 6d. Баннер ошибки рендерится в assistant-контейнере без content-
    #       классов — answer-селекторы его не видят (last_block_text всегда
    #       отдаёт старый ответ), ловим только через error_scope ──
    llm7 = wl.WebChatLLM("qwen", base_dir=tmp / "q7")
    llm7._save_state({"chat_url": "https://chat.qwen.ai/c/dead"})
    flow7 = {"nav": [], "send": 0}
    _open7, _nav7 = ba.open_new_tab, ba.navigate_tab
    _cfs7, _lbt7 = ba.chat_fill_send, ba.last_block_text
    _cnt7, _url7, _ev7 = ba.count_blocks, ba.tab_url, ba.eval_js
    ba.open_new_tab = lambda url, **kw: 42
    ba.navigate_tab = lambda url, tab_id=None: flow7["nav"].append(url)
    ba.tab_url = lambda *a, **kw: "https://chat.qwen.ai/c/new2"
    ba.chat_fill_send = lambda *a, **kw: (
        flow7.__setitem__("send", flow7["send"] + 1), "sent")[1]
    oops = "Oops! There was an issue connecting to Qwen3.7-Plus. parent_id x-x is not exist"
    # scope-пробник: ошибка только на 1-й попытке; mode_js — «ok»
    ba.eval_js = lambda *a, **kw: (
        oops if "qwen-chat-message-assistant" in (a[2] if len(a) > 2 else "") and flow7["send"] == 1
        else "ok")
    # answer-блоки ошибку НЕ содержат: baseline(3, «старый ответ») + 2 полла
    # без изменений; свежий чат: baseline(0) + 3 полла нового ответа
    cnts7 = iter([3, 3, 3,      0, 1, 1, 1])
    txts7 = iter(["старый ответ", "старый ответ", "старый ответ",
                  "", "новый ответ", "новый ответ", "новый ответ"])
    ba.count_blocks = lambda *a, **kw: next(cnts7)
    def _lbt7(host, tid, sels=None, **kw):
        if sels and list(sels) == (llm7.adapter.get("user") or []):
            return "User: hi"
        return next(txts7)
    ba.last_block_text = _lbt7
    try:
        res7 = llm7.get_response([{"role": "user", "content": "hi"}])
        check("recover: ошибка только в error_scope (не в answer-блоках) → сброс и ответ",
              res7 == "новый ответ"
              and flow7["send"] == 2
              and flow7["nav"] == [wl.ADAPTERS["qwen"]["home"]]
              and llm7._chat_url() == "https://chat.qwen.ai/c/new2")
    finally:
        ba.open_new_tab, ba.navigate_tab = _open7, _nav7
        ba.chat_fill_send, ba.last_block_text = _cfs7, _lbt7
        ba.count_blocks, ba.tab_url, ba.eval_js = _cnt7, _url7, _ev7

    # ── 6e. Hydration-гонка: closed-loop «поле очистилось» срабатывает ложно,
    #       сообщение не попадает в ленту — _send_verified обязан заметить
    #       это по user-блоку и повторить отправку ──
    _sv, _pl = wl.SEND_VERIFY_SEC, wl.POLL_SEC
    wl.SEND_VERIFY_SEC, wl.POLL_SEC = 0.5, 0.05
    llm8 = wl.WebChatLLM("qwen", base_dir=tmp / "q8")
    flow8 = {"send": 0, "delivered": False}
    _open8 = ba.open_new_tab
    _cfs8, _lbt8 = ba.chat_fill_send, ba.last_block_text
    _cnt8, _url8, _ev8 = ba.count_blocks, ba.tab_url, ba.eval_js
    ba.open_new_tab = lambda url, **kw: 42
    ba.tab_url = lambda *a, **kw: "https://chat.qwen.ai/c/ok1"
    ba.eval_js = lambda *a, **kw: "ok"

    def _send8(*a, **kw):
        flow8["send"] += 1
        if flow8["send"] >= 2:
            flow8["delivered"] = True
        return "sent"

    def _lbt8(host, tid, sels, **kw):
        if sels and "user" in sels[0]:
            return "User: hi" if flow8["delivered"] else "старый чужой"
        return "ответ" if flow8["delivered"] else ""

    ba.chat_fill_send = _send8
    ba.last_block_text = _lbt8
    ba.count_blocks = lambda *a, **kw: 1 if flow8["delivered"] else 0
    try:
        res8 = llm8.get_response([{"role": "user", "content": "hi"}])
        check("send-verify: испарившаяся отправка — повтор, ответ получен",
              res8 == "ответ" and flow8["send"] == 2
              and llm8._chat_url() == "https://chat.qwen.ai/c/ok1")

        # Полный провал доставки (обе попытки мимо) — быстрый None, без 150с
        llm9 = wl.WebChatLLM("qwen", base_dir=tmp / "q9")
        flow8["send"] = 0
        flow8["delivered"] = False
        ba.chat_fill_send = lambda *a, **kw: (
            flow8.__setitem__("send", flow8["send"] + 1), "sent")[1]
        res9 = llm9.get_response([{"role": "user", "content": "hi"}])
        check("send-verify: сообщение не попало в ленту за 2 попытки → None сразу",
              res9 is None and flow8["send"] == 2)
        check("stuck: двойной промах ленты (кейс 26.08) → перезагрузка "
              "вкладки (без перезапуска браузера)",
              reloads[-1] == 42 and len(restarts) == 1)
    finally:
        wl.SEND_VERIFY_SEC, wl.POLL_SEC = _sv, _pl
        ba.open_new_tab = _open8
        ba.chat_fill_send, ba.last_block_text = _cfs8, _lbt8
        ba.count_blocks, ba.tab_url, ba.eval_js = _cnt8, _url8, _ev8

    # ── 6g. Карантин: антибот-челлендж → автопопытка → карантин сайта,
    #       дальнейшие вызовы пропускаются мгновенно; ручное снятие ──
    wl.clear_quarantine("qwen")
    _da, _ac = ba.detect_antibot, ba.try_challenge_autoclick
    _oq, _uq = ba.open_new_tab, ba.tab_url
    ba.detect_antibot = lambda *a, **kw: "widget: iframe[src*=challenges.cloudflare]"
    ba.try_challenge_autoclick = lambda *a, **kw: False  # чекбокс не прошли
    ba.open_new_tab = lambda url, **kw: 42
    ba.tab_url = lambda *a, **kw: "https://chat.qwen.ai/c/q1"
    llm_q1 = wl.WebChatLLM("qwen", base_dir=tmp / "qq1")
    try:
        res_q = llm_q1.get_response([{"role": "user", "content": "hi"}])
        check("quarantine: челлендж → None, сайт в карантине, alert в очереди",
              res_q is None and wl.site_quarantined("qwen")
              and any(a["site"] == "qwen" for a in wl.pop_quarantine_alerts()))
        check("quarantine: вызов в карантине — мгновенный None",
              llm_q1.get_response([{"role": "user", "content": "hi"}]) is None)
        # Автоклик помог (челлендж ушёл) — карантина нет
        ba.detect_antibot = lambda *a, **kw: None
        wl.clear_quarantine("qwen")
        llm_q2 = wl.WebChatLLM("qwen", base_dir=tmp / "qq2")
        llm_q2._tab_id = 42
        check("quarantine: чистая страница — карантина нет, вкладка жива",
              llm_q2._challenge_check(ba, 42) is False
              and not wl.site_quarantined("qwen"))
        # Замер антибота проводится СТРОГО: обёртка не имеет права глушить
        # ошибку в None — «не смогли посмотреть» ≠ «посмотрели, чисто»
        _strict_flags = []
        def _da_strict(host=None, tab_id=None, strict=False):
            _strict_flags.append(strict)
            return None
        ba.detect_antibot = _da_strict
        llm_q2._challenge_check(ba, 42)
        check("quarantine: _challenge_check замеряет строго (strict=True)",
              _strict_flags == [True])
        # Сбой замера не считается «чисто»: detect_antibot обязан различать
        # «не удалось проверить» и «страница чистая», иначе карантин
        # снимается вслепую
        wl.quarantine_site("qwen", "widget: turnstile")
        wl.pop_quarantine_alerts()
        def _da_boom(host=None, tab_id=None, strict=False):
            raise ba.BrowserUnavailable("антибот-проверка не выполнена")
        ba.detect_antibot = _da_boom
        _ends = []
        _orig_end = ba.end_rescue_pool_h
        ba.end_rescue_pool_h = lambda: _ends.append(1)
        try:
            ch_unknown = llm_q2._challenge_check(ba, 42)
        finally:
            ba.end_rescue_pool_h = _orig_end
        check("quarantine: сбой замера ≠ «чисто» — карантин остаётся",
              ch_unknown is False and wl.site_quarantined("qwen")
              and not _ends)
        # Положительное подтверждение чистоты — карантин снимается
        ba.detect_antibot = _da_strict
        llm_q2._challenge_check(ba, 42)
        check("quarantine: чистота подтверждена замером — карантин снят",
              not wl.site_quarantined("qwen"))

        # Отпускание вкладки закрывает её в браузере, а не только забывает
        _closed = []
        _orig_close = ba.close_background_tab
        ba.close_background_tab = lambda t: (_closed.append(t), True)[1]
        try:
            llm_q2._tab_id = 1000007
            llm_q2._drop_tab(ba, "тест")
            check("вкладка: _drop_tab закрывает фоновую вкладку и забывает id",
                  _closed == [1000007] and llm_q2._tab_id is None)
            # reload не помог — вкладка закрывается, а не бросается
            _closed.clear()
            llm_q2._tab_id = 1000008
            llm_q2._last_tab_reload_ts = 0.0
            _orig_reload = ba.reload_tab
            ba.reload_tab = lambda *a, **kw: (_ for _ in ()).throw(
                ba.BrowserUnavailable("вкладка не перезагрузилась"))
            try:
                llm_q2._restart_stuck_browser(ba, "тест")
            finally:
                ba.reload_tab = _orig_reload
            check("вкладка: неудачный reload закрывает вкладку (не копим сирот)",
                  _closed == [1000008] and llm_q2._tab_id is None)
        finally:
            ba.close_background_tab = _orig_close
    finally:
        ba.detect_antibot, ba.try_challenge_autoclick = _da, _ac
        ba.open_new_tab, ba.tab_url = _oq, _uq
        wl.clear_quarantine("qwen")

    # ── 6h. Разлогин: поля ввода нет, страница входа → карантин login
    #       (не «refused» и без перезапуска браузера); проба раз в
    #       LOGIN_PROBE_SEC снимает карантин, когда вход восстановлен ──
    wl.clear_quarantine("deepseek")
    wl.pop_quarantine_alerts()
    wl._LOGIN_PROBE_AT.clear()
    _saved = {k: getattr(ba, k) for k in (
        "open_new_tab", "tab_url", "eval_js", "detect_antibot",
        "chat_fill_send", "pool_h_rescue_active", "end_rescue_pool_h")}
    page = {"url": "https://chat.deepseek.com/sign_in", "comp": False,
            "pwd": True, "btn": True}

    def _eval_login(host, tab_id, js, *a, **kw):
        return json.dumps(page) if "comp:" in js else ""
    sends = []
    ba.open_new_tab = lambda url, **kw: 42
    ba.tab_url = lambda *a, **kw: "https://chat.deepseek.com/sign_in"
    ba.eval_js = _eval_login
    ba.detect_antibot = lambda *a, **kw: None
    ba.chat_fill_send = lambda *a, **kw: (sends.append(1), (_ for _ in ()).throw(
        ba.BrowserUnavailable("поле чата не приняло ввод")))[1]
    ba.pool_h_rescue_active = lambda: False
    ba.end_rescue_pool_h = lambda: None
    try:
        llm_l = wl.WebChatLLM("deepseek", base_dir=tmp / "dl1", channel="cc")
        restarts_l = []
        llm_l._restart_stuck_browser = lambda *a, **kw: restarts_l.append(1)
        check("login: состояние вкладки — страница входа распознана",
              llm_l._login_state(ba, 42)[0] == "login")
        res_l = llm_l.get_response([{"role": "user", "content": "hi"}])
        alerts_l = wl.pop_quarantine_alerts()
        check("login: отправка сорвалась на странице входа → карантин login, "
              "браузер не перезапускается",
              res_l is None and wl.quarantine_kind("deepseek") == "login"
              and not restarts_l
              and [a["kind"] for a in alerts_l] == ["login"])
        n_sends = len(sends)
        check("login: следующий вызов до интервала пробы — мгновенный пропуск",
              llm_l.get_response([{"role": "user", "content": "hi"}]) is None
              and len(sends) == n_sends)
        # Чистая от капчи страница входа НЕ снимает карантин разлогина
        llm_l._challenge_check(ba, 42)
        check("login: _challenge_check на странице входа карантин не снимает",
              wl.quarantine_kind("deepseek") == "login")
        # Интервал пробы истёк, вход всё ещё не выполнен → молча продлён
        wl._LOGIN_PROBE_AT["deepseek"] = 0.0
        llm_l.get_response([{"role": "user", "content": "hi"}])
        check("login: проба на странице входа — без отправки, карантин продлён, "
              "повторного уведомления нет",
              len(sends) == n_sends and wl.quarantine_kind("deepseek") == "login"
              and not wl.pop_quarantine_alerts())
        # Человек вошёл: поле ввода на месте → проба снимает карантин
        page.update(url="https://chat.deepseek.com/", comp=True, pwd=False,
                    btn=False)
        wl._LOGIN_PROBE_AT["deepseek"] = 0.0
        llm_l.get_response([{"role": "user", "content": "hi"}])
        check("login: вход восстановлен — проба снимает карантин",
              not wl.site_quarantined("deepseek"))
        # Непохожая на вход страница без поля — «неизвестно», старое поведение
        page.update(url="https://chat.deepseek.com/", comp=False, pwd=False,
                    btn=False)
        check("login: без признаков входа — «unknown», не разлогин",
              llm_l._login_state(ba, 42)[0] == "unknown")
        # Окно подтверждения возраста поверх видимого поля — тоже «нужен
        # человек»: бот за него 18+ не подтверждает
        page.update(comp=True, age="Age verification. Confirm your age")
        st_age = llm_l._login_state(ba, 42)
        check("age: окно возраста поверх чата → нужен человек",
              st_age[0] == "login" and "возраст" in st_age[1])
        llm_l._on_logged_out(st_age[1])
        al_age = wl.pop_quarantine_alerts()
        check("age: карантин с причиной про возраст (для текста уведомления)",
              al_age and "возраст" in al_age[0]["reason"])
        page.update(age="")
        # Недоставленное «выкинул из аккаунта» выбрасывается при входе
        wl.clear_quarantine("deepseek")
        llm_l._on_logged_out("страница входа: поле пароля")
        llm_l._login_restored(ba)
        check("login: вход восстановлен — недоставленное уведомление снято",
              not wl.pop_quarantine_alerts())
        # A/B-панель: клик по первому «I prefer this response»
        ba.eval_js = lambda host, tab_id, js, *a, **kw: (
            "clicked:2" if "prefer this response" in js else "")
        check("ab: панель выбора из двух ответов — выбран первый",
              llm_l._resolve_ab_choice(ba, 42) is True)
        ba.eval_js = lambda *a, **kw: ""
        check("ab: панели нет — ничего не жмём",
              llm_l._resolve_ab_choice(ba, 42) is False)
    finally:
        for k, v in _saved.items():
            setattr(ba, k, v)
        wl.clear_quarantine("deepseek")
        wl.pop_quarantine_alerts()

    # ── 6i. Пул H после rescue: видимый Chrome с живым сокетом возвращается
    #       в штатный режим при открытии новой вкладки (раньше — никогда) ──
    _sv_cl = ba._RAW_CLIENTS[ba._POOL_H]
    _sv_mode, _sv_ov = ba._POOL_H_RUNNING_MODE, ba._POOL_H_MODE_OVERRIDE
    _sv_reset, _sv_call = ba._reset_raw_pool, ba._raw_call
    events = []
    try:
        ba._RAW_CLIENTS[ba._POOL_H] = object()
        ba._POOL_H_RUNNING_MODE = "headed"
        ba._POOL_H_MODE_OVERRIDE = None  # rescue окончен
        ba._reset_raw_pool = lambda pool, forget_tabs=True: events.append(("reset", pool))
        ba._raw_call = lambda method, params=None, **kw: (
            events.append(("call", method)),
            {"targetId": "t1", "sessionId": "s1"})[1]
        check("pool H: видимый после rescue — режим устарел",
              ba._pool_h_mode_stale())
        ba._raw_open("about:blank", pool=ba._POOL_H)
        check("pool H: новая вкладка сначала сбрасывает сокет (перезапуск "
              "в штатном режиме), потом создаётся",
              events[:2] == [("reset", ba._POOL_H),
                             ("call", "Target.createTarget")])
        events.clear()
        ba._POOL_H_RUNNING_MODE = "headless"
        ba._raw_open("about:blank", pool=ba._POOL_H)
        check("pool H: режим совпадает — сокет не трогаем",
              ("reset", ba._POOL_H) not in events)
    finally:
        ba._RAW_CLIENTS[ba._POOL_H] = _sv_cl
        ba._POOL_H_RUNNING_MODE, ba._POOL_H_MODE_OVERRIDE = _sv_mode, _sv_ov
        ba._reset_raw_pool, ba._raw_call = _sv_reset, _sv_call
        with ba._RAW_TABS_LOCK:
            for _t in [t for t, v in ba._RAW_TABS.items()
                       if v.get("targetId") == "t1"]:
                ba._RAW_TABS.pop(_t, None)

    # ── 6j. Rescue, которому чинить нечего: реплика пользователя («готово»)
    #       завершает его — и свой, и соседа; карантин капчи/входа (свой или
    #       соседа) — нет. Поиск Google во время rescue окон выдачи не
    #       открывает ──
    _rescue_file = Path(ba._pool_h_rescue_path())  # временный (см. ниже)
    # Файл ожидания «соседа» — процесса бота с живым pid (родитель теста)
    _nb_wait = Path(f"{_rescue_file}.wait.{os.getppid()}")
    _sv_r = (ba._pool_h_alive, ba._raw_open, ba._POOL_H_MODE_OVERRIDE,
             ba._POOL_H_RESCUE_UNTIL, ba._POOL_H_RESCUE_SHARED)
    opened = []
    ba._pool_h_alive = lambda: True
    ba._raw_open = lambda url, pool=None: (opened.append(url), 7)[1]
    try:
        # как rescue_pool_h, без перезапуска Chrome
        ba._POOL_H_MODE_OVERRIDE = "headed"
        ba._POOL_H_RESCUE_UNTIL = wl.time.time() + 600
        ba._POOL_H_RESCUE_SHARED = ba._write_shared_rescue(
            ba._POOL_H_RESCUE_UNTIL)
        try:
            ba.open_headless_tab("https://www.google.com/search?q=x")
            refused = False
        except ba.BrowserUnavailable:
            refused = True
        check("rescue: поиск Google не открывает окно выдачи — отказ "
              "(поиск уйдёт в другой поисковик)", refused and not opened)
        wl.quarantine_site("qwen", "widget: turnstile")
        check("rescue: капча в карантине — реплика rescue не завершает",
              wl.finish_idle_rescue() is False and ba.pool_h_rescue_active())
        wl.clear_quarantine("qwen")
        wl.quarantine_site("qwen", "лимит", ttl=600, kind="ratelimit")
        check("rescue: чинить нечего (лимит руками не снять) — реплика "
              "завершает rescue, общий файл удалён",
              wl.finish_idle_rescue() is True
              and not ba.pool_h_rescue_active()
              and not _rescue_file.exists())
        ba.open_headless_tab("https://www.google.com/search?q=x")
        check("rescue окончен — поиск Google снова через пул H", len(opened) == 1)
        # Rescue соседа: свой override пуст, срок — только в общем файле.
        # Сосед ждёт капчу (его файл ожидания) — реплика rescue не завершает;
        # капч/входов не ждёт никто — завершает и не включавший процесс
        # (раньше чужой rescue не трогали: окно висело до конца срока)
        _rescue_file.write_text(str(wl.time.time() + 600))
        _nb_wait.write_text(json.dumps({"deepseek": wl.time.time() + 600}))
        check("rescue соседа, сосед ждёт капчу — реплика его не завершает",
              ba._pid_alive(os.getppid())
              and wl.finish_idle_rescue() is False
              and ba.pool_h_rescue_active())
        _nb_wait.unlink()
        check("rescue соседа, капч/входов не ждёт никто — реплика завершает "
              "его и здесь, общий файл удалён",
              wl.finish_idle_rescue() is True
              and not ba.pool_h_rescue_active()
              and not _rescue_file.exists())
    finally:
        (ba._pool_h_alive, ba._raw_open, ba._POOL_H_MODE_OVERRIDE,
         ba._POOL_H_RESCUE_UNTIL, ba._POOL_H_RESCUE_SHARED) = _sv_r
        _rescue_file.unlink(missing_ok=True)
        _nb_wait.unlink(missing_ok=True)
        wl.clear_quarantine("qwen")
        wl.pop_quarantine_alerts()

    # ── 6k. Rescue ждёт ВСЕ капчи/входы: снятие одного карантина при
    #       оставшихся других rescue не завершает (раньше — завершал по
    #       первому, пул H уходил в headless посреди второй капчи). Лимит/
    #       отказ в rescue не пробуются и чистой страницей не снимаются;
    #       чужой rescue завершает и этот процесс, если капч/входов не ждёт
    #       никто (межпроцессная часть — 6l) ──
    _sv_k = (ba._POOL_H_MODE_OVERRIDE, ba._POOL_H_RESCUE_UNTIL,
             ba._POOL_H_RESCUE_SHARED, ba.detect_antibot)
    _k_sites = ("deepseek", "qwen", "zai", "kimi", "chatgpt")
    _k_logs = []

    class _KHandler(logging.Handler):
        def emit(self, record):
            _k_logs.append(record.getMessage())
    _k_handler = _KHandler(level=logging.INFO)
    _k_level = wl.logger.level
    wl.logger.addHandler(_k_handler)
    wl.logger.setLevel(logging.INFO)

    def _own_rescue():
        # как rescue_pool_h, без перезапуска Chrome
        ba._POOL_H_MODE_OVERRIDE = "headed"
        ba._POOL_H_RESCUE_UNTIL = wl.time.time() + 600
        ba._POOL_H_RESCUE_SHARED = ba._write_shared_rescue(
            ba._POOL_H_RESCUE_UNTIL)

    def _k_reset(*quars):
        for s in list(wl.quarantine_status()):
            wl.clear_quarantine(s)
        for s, kind, *pool in quars:
            wl.quarantine_site(s, "тест", ttl=600, kind=kind,
                               pool=pool[0] if pool else "h")
        wl.pop_quarantine_alerts()
        _k_logs.clear()

    def _k_llm(site, sub):
        inst = wl.WebChatLLM(site, base_dir=tmp / f"k_{sub}")
        inst._tab_id = 42
        return inst
    ba.detect_antibot = lambda *a, **kw: None  # страница чистая
    try:
        # Две капчи: первая пройдена — rescue идёт, вторая — завершён
        _own_rescue()
        _k_reset(("deepseek", "challenge"), ("qwen", "challenge"))
        _k_llm("deepseek", "d1")._challenge_check(ba, 42)
        check("rescue: две капчи, пройдена одна — её карантин снят, rescue "
              "идёт (ждёт вторую)",
              not wl.site_quarantined("deepseek")
              and wl.quarantine_kind("qwen") == "challenge"
              and ba.pool_h_rescue_active() and _rescue_file.exists())
        check("rescue: в логе — кого ещё ждём",
              any("ещё ждём: qwen" in m for m in _k_logs))
        _k_llm("qwen", "q1")._challenge_check(ba, 42)
        check("rescue: пройдена и вторая капча — rescue завершён, общий "
              "файл удалён",
              not wl.site_quarantined("qwen")
              and not ba.pool_h_rescue_active() and not _rescue_file.exists())

        # Вход + капча: вход восстановлен — rescue ждёт капчу
        _own_rescue()
        _k_reset(("deepseek", "login"), ("qwen", "challenge"))
        _k_llm("deepseek", "d2")._login_restored(ba)
        check("rescue: вход восстановлен, капча ещё в карантине — rescue идёт",
              not wl.site_quarantined("deepseek")
              and wl.quarantine_kind("qwen") == "challenge"
              and ba.pool_h_rescue_active())
        _k_llm("qwen", "q2")._challenge_check(ba, 42)
        check("rescue: капча после входа пройдена — rescue завершён",
              not ba.pool_h_rescue_active())
        # Обратный порядок: капча пройдена — rescue ждёт вход
        _own_rescue()
        _k_reset(("deepseek", "login"), ("qwen", "challenge"))
        _k_llm("qwen", "q3")._challenge_check(ba, 42)
        check("rescue: капча пройдена, вход ещё не выполнен — rescue идёт, "
              "чистая страница карантин входа не снимает",
              ba.pool_h_rescue_active()
              and wl.quarantine_kind("deepseek") == "login")
        _k_llm("deepseek", "d3")._login_restored(ba)
        check("rescue: вход после капчи восстановлен — rescue завершён",
              not ba.pool_h_rescue_active())

        # Лимит в rescue: мгновенный пропуск, чистой страницей не снимается
        # и rescue не держит
        _own_rescue()
        _k_reset(("zai", "ratelimit"), ("kimi", "refused"),
                 ("qwen", "challenge"))
        zai_k = _k_llm("zai", "z1")
        kimi_k = _k_llm("kimi", "k1")
        check("rescue: лимит (ratelimit) и отказ (refused) в rescue — "
              "мгновенный пропуск, как без rescue",
              zai_k._quarantine_skip() is True
              and zai_k._quarantine_skip(ba) is True
              and kimi_k._quarantine_skip() is True)
        check("rescue: капча в rescue — пропуска нет (пробуем сайт)",
              _k_llm("qwen", "q4")._quarantine_skip() is False)
        zai_k._challenge_check(ba, 42)
        kimi_k._challenge_check(ba, 42)
        check("rescue: чистая страница лимит/отказ не снимает и rescue не "
              "завершает",
              wl.quarantine_kind("zai") == "ratelimit"
              and wl.quarantine_kind("kimi") == "refused"
              and ba.pool_h_rescue_active())
        _k_llm("qwen", "q5")._challenge_check(ba, 42)
        check("rescue: последняя капча пройдена — rescue завершён, хотя "
              "лимит ещё в карантине (его rescue не ждёт)",
              not ba.pool_h_rescue_active()
              and wl.quarantine_kind("zai") == "ratelimit")
        # Без rescue лимит тоже пропускается, а чистая страница его не
        # снимает (гонка: карантин начался, пока шёл вызов)
        check("лимит без rescue: пропуск мгновенный",
              zai_k._quarantine_skip() is True)
        zai_k._challenge_check(ba, 42)
        check("лимит без rescue: чистая страница не снимает",
              wl.quarantine_kind("zai") == "ratelimit")

        # Чужой rescue: свой override пуст, срок — только в общем файле.
        # Снят последний карантин во всех процессах (соседи ничего не
        # ждут) — rescue завершает и не включавший его процесс (раньше
        # «завершит владелец»: окно висело до его действия или конца срока)
        _k_reset(("qwen", "challenge"))
        _rescue_file.write_text(str(wl.time.time() + 600))
        check("чужой rescue: капча в rescue пробуется (окно видимое у всех)",
              _k_llm("qwen", "q6")._quarantine_skip() is False)
        _k_llm("qwen", "q7")._challenge_check(ba, 42)
        check("чужой rescue: снят последний карантин во всех процессах — "
              "rescue завершён и не владельцем, общий файл удалён",
              not wl.site_quarantined("qwen")
              and not ba.pool_h_rescue_active()
              and not _rescue_file.exists())
        _rescue_file.write_text(str(wl.time.time() + 600))
        _k_reset(("deepseek", "login"))
        _k_llm("deepseek", "d4")._login_restored(ba)
        check("чужой rescue: вход восстановлен, больше никто не ждёт — "
              "rescue завершён",
              not ba.pool_h_rescue_active() and not _rescue_file.exists())
        _rescue_file.unlink(missing_ok=True)

        # Пул V: его капча rescue пула H не держит, и её снятие rescue пула H
        # не завершает (капчу проходили не в окне rescue)
        _own_rescue()
        _k_reset(("chatgpt", "challenge", "v"), ("qwen", "challenge"))
        _k_llm("qwen", "q8")._challenge_check(ba, 42)
        check("rescue: капча сайта пула V rescue пула H не держит",
              not ba.pool_h_rescue_active()
              and wl.quarantine_kind("chatgpt") == "challenge")
        _own_rescue()
        _k_reset(("chatgpt", "challenge", "v"))
        _k_llm("chatgpt", "c1")._challenge_check(ba, 42)
        check("rescue: снятие карантина сайта пула V rescue пула H не "
              "завершает",
              not wl.site_quarantined("chatgpt") and ba.pool_h_rescue_active())
        # Реплика «готово» — то же правило: капча в карантине держит rescue,
        # снята последняя — завершает; лимит не держит
        _k_reset(("deepseek", "challenge"), ("qwen", "challenge"))
        _k_llm("deepseek", "d5")._challenge_check(ba, 42)
        check("finish_idle_rescue: капча ещё в карантине — не завершает",
              wl.finish_idle_rescue() is False and ba.pool_h_rescue_active())
        _k_reset(("zai", "ratelimit"), ("chatgpt", "challenge", "v"))
        check("finish_idle_rescue: только лимит и капча пула V — завершает",
              wl.finish_idle_rescue() is True
              and not ba.pool_h_rescue_active())
        check("finish_idle_rescue: rescue нет — False",
              wl.finish_idle_rescue() is False)
    finally:
        (ba._POOL_H_MODE_OVERRIDE, ba._POOL_H_RESCUE_UNTIL,
         ba._POOL_H_RESCUE_SHARED, ba.detect_antibot) = _sv_k
        _rescue_file.unlink(missing_ok=True)
        wl.logger.removeHandler(_k_handler)
        wl.logger.setLevel(_k_level)
        for s in _k_sites:
            wl.clear_quarantine(s)
        wl.pop_quarantine_alerts()
        wl._LOGIN_PROBE_AT.clear()

    # ── 6l. Кого ждёт rescue — общее между процессами бота: каждый процесс
    #       публикует свои капчи/входы пула H в <rescue>.wait.<pid>; rescue
    #       завершается, когда не ждёт НИ ОДИН живой процесс, — кем угодно.
    #       Раньше владелец не видел капчу соседа (завершал rescue посреди
    #       неё), а сосед, сняв последний карантин, rescue не завершал.
    #       Второй процесс эмулирован файлом публикации с чужим живым pid
    #       (родитель теста) и с мёртвым pid (завершившийся подпроцесс) ──
    import subprocess as _l_sp
    _l_own = Path(f"{_rescue_file}.wait.{os.getpid()}")
    _l_peer_pid = os.getppid()
    _l_peer = Path(f"{_rescue_file}.wait.{_l_peer_pid}")
    _l_proc = _l_sp.Popen([sys.executable, "-c", "pass"])
    _l_proc.wait()
    _l_dead_pid = _l_proc.pid
    _l_dead = Path(f"{_rescue_file}.wait.{_l_dead_pid}")
    _l_bad = Path(f"{_rescue_file}.wait.1")         # живой чужой pid, битый JSON
    _l_tmp = Path(f"{_rescue_file}.wait.{_l_peer_pid}.tmp")  # не файл ожидания
    _l_files = (_l_own, _l_peer, _l_dead, _l_bad, _l_tmp)

    def _l_read(p):
        return json.loads(p.read_text()) if p.exists() else None

    def _l_put(p, data):
        p.write_text(json.dumps(data))
    _sv_l = (ba._POOL_H_MODE_OVERRIDE, ba._POOL_H_RESCUE_UNTIL,
             ba._POOL_H_RESCUE_SHARED, ba.detect_antibot,
             ba.try_challenge_autoclick, wl.atomic_write_json)
    _l_sites = ("deepseek", "qwen", "zai", "kimi", "chatgpt")
    wl.logger.addHandler(_k_handler)
    wl.logger.setLevel(logging.INFO)
    ba.detect_antibot = lambda *a, **kw: None  # страница чистая
    try:
        check("6l: эмуляция соседей — родитель теста жив, подпроцесс "
              "завершён; файлы — во временном каталоге, не у профиля",
              ba._pid_alive(_l_peer_pid) and not ba._pid_alive(_l_dead_pid)
              and "vpc-browser-profile" not in str(_l_own))

        # (г) Публикация обновляется при quarantine/clear/истечении; в ней
        # только капчи/входы пула H, без ждущих — файла нет
        _k_reset()
        check("публикация: ждущих нет — файла нет", not _l_own.exists())
        t0 = wl.time.time()
        wl.quarantine_site("qwen", "тест", ttl=600)
        pub = _l_read(_l_own) or {}
        check("публикация: капча → файл процесса {сайт: срок карантина}",
              list(pub) == ["qwen"] and abs(pub["qwen"] - (t0 + 600)) < 5)
        wl.quarantine_site("zai", "лимит", ttl=600, kind="ratelimit")
        wl.quarantine_site("kimi", "отказ", ttl=600, kind="refused")
        wl.quarantine_site("chatgpt", "тест", ttl=600, pool="v")
        check("публикация: лимит, отказ и капча пула V — не публикуются",
              list(_l_read(_l_own) or {}) == ["qwen"])
        wl.quarantine_site("deepseek", "разлогин", ttl=600, kind="login")
        check("публикация: разлогин добавлен",
              sorted(_l_read(_l_own) or {}) == ["deepseek", "qwen"])
        wl.clear_quarantine("qwen")
        check("публикация: снятие карантина убирает сайт",
              list(_l_read(_l_own) or {}) == ["deepseek"])
        with wl._QUARANTINE_LOCK:
            wl._SITE_QUARANTINE["deepseek"]["until"] = wl.time.time() - 1
        check("публикация: истечение (site_quarantined) — последний ждущий "
              "ушёл, файл удалён",
              wl.site_quarantined("deepseek") is False
              and not _l_own.exists())
        wl.quarantine_site("qwen", "тест", ttl=600)
        with wl._QUARANTINE_LOCK:
            wl._SITE_QUARANTINE["qwen"]["until"] = wl.time.time() - 1
        wl.quarantine_status()
        check("публикация: истечение (quarantine_status) — файл удалён",
              not _l_own.exists())
        wl.quarantine_site("qwen", "тест", ttl=600)
        wl._drop_rescue_wait()  # штатный выход процесса (atexit)
        check("публикация: выход процесса убирает его файл",
              not _l_own.exists())
        wl.quarantine_site("kimi", "капча", ttl=600)
        check("публикация: после выхода-хука новая капча публикуется снова",
              sorted(_l_read(_l_own) or {}) == ["kimi", "qwen"])

        # (д) Сбой записи не ломает вызов веб-чата: карантин в памяти есть,
        # исключения нет; следующее изменение публикует заново
        _k_reset()

        def _l_fail(*a, **kw):
            raise OSError(28, "No space left on device")
        wl.atomic_write_json = _l_fail
        ba.detect_antibot = lambda *a, **kw: "widget: turnstile"
        ba.try_challenge_autoclick = lambda *a, **kw: False
        try:
            got = _k_llm("qwen", "l0")._challenge_check(ba, 42)
            raised = None
        except Exception as e:
            got, raised = None, e
        check("сбой записи: капча поймана, карантин есть, вызов не упал",
              raised is None and got is True
              and wl.quarantine_kind("qwen") == "challenge"
              and not _l_own.exists())
        wl.atomic_write_json = _sv_l[5]
        ba.detect_antibot = lambda *a, **kw: None
        wl.quarantine_site("deepseek", "тест", ttl=600)
        check("сбой записи: следующее изменение публикует всё ждущее",
              sorted(_l_read(_l_own) or {}) == ["deepseek", "qwen"])

        # (а) Владелец не завершает rescue, пока чужой живой процесс ждёт
        # капчу: ни снятием своего последнего карантина, ни репликой
        _own_rescue()
        _k_reset(("qwen", "challenge"))
        _l_put(_l_peer, {"deepseek": wl.time.time() + 600})
        _k_llm("qwen", "l1")._challenge_check(ba, 42)
        check("владелец: свой последний карантин снят, сосед ждёт капчу — "
              "rescue идёт",
              not wl.site_quarantined("qwen")
              and ba.pool_h_rescue_active() and _rescue_file.exists())
        check("владелец: в логе — чей сайт ждём",
              any("ещё ждём: deepseek (другой процесс бота)" in m
                  for m in _k_logs))
        check("владелец: реплика «готово» при капче соседа не завершает",
              wl.finish_idle_rescue() is False and ba.pool_h_rescue_active())
        _l_peer.unlink()  # сосед снял свою капчу — его файла нет
        check("владелец: сосед больше не ждёт — реплика завершает rescue",
              wl.finish_idle_rescue() is True
              and not ba.pool_h_rescue_active()
              and not _rescue_file.exists())

        # (б) Не владелец (срок только в общем файле) завершает rescue, когда
        # снят последний карантин во всех процессах
        _k_reset(("qwen", "challenge"), ("kimi", "login"))
        _rescue_file.write_text(str(wl.time.time() + 600))
        _l_put(_l_peer, {"deepseek": wl.time.time() + 600})
        _k_llm("qwen", "l2")._challenge_check(ba, 42)
        check("не владелец: снят свой карантин, ждут свой вход и капча "
              "соседа — rescue идёт",
              ba.pool_h_rescue_active()
              and any("ещё ждём: kimi, deepseek (другой процесс бота)" in m
                      for m in _k_logs))
        _l_put(_l_peer, {})  # сосед снял капчу (пустая публикация — тоже)
        _k_llm("kimi", "l3")._login_restored(ba)
        check("не владелец: снят последний карантин во всех процессах — "
              "rescue завершён, общий файл удалён",
              not ba.pool_h_rescue_active() and not _rescue_file.exists()
              and not _l_own.exists())

        # (в) Файл мёртвого процесса, просроченные записи живого, битый
        # файл и чужой tmp не держат rescue; файл мёртвого подчищается
        _own_rescue()
        _k_reset()
        _l_put(_l_dead, {"qwen": wl.time.time() + 600})
        _l_put(_l_peer, {"zai": wl.time.time() - 5})
        _l_bad.write_text("{битый")
        _l_put(_l_tmp, {"kimi": wl.time.time() + 600})
        check("мёртвый процесс и просроченное: реплика завершает rescue",
              wl.finish_idle_rescue() is True
              and not ba.pool_h_rescue_active())
        check("мёртвый процесс: его файл подчищен, файл живого соседа — нет",
              not _l_dead.exists() and _l_peer.exists())
        # Тот же сайт ждут и этот процесс, и сосед — сосед держит rescue,
        # когда свой карантин уже снят (капчу снимает каждый процесс своим
        # вызовом к сайту)
        _own_rescue()
        _k_reset(("qwen", "challenge"))
        _l_put(_l_peer, {"qwen": wl.time.time() + 600})
        _k_llm("qwen", "l4")._challenge_check(ba, 42)
        check("тот же сайт у соседа: свой снят, сосед ждёт — rescue идёт",
              ba.pool_h_rescue_active()
              and any("ещё ждём: qwen (другой процесс бота)" in m
                      for m in _k_logs))
        # Снятие карантина сайта пула V rescue пула H не завершает, даже
        # когда больше никто ничего не ждёт
        _l_peer.unlink()
        _k_reset()
        check("пул V: правило конца rescue пула H не срабатывает",
              wl._finish_rescue_if_done(ba, cleared="chatgpt", pool="v")
              is False and ba.pool_h_rescue_active())
        ba.end_rescue_pool_h()
        check("rescue нет — правило ничего не делает (False)",
              wl._finish_rescue_if_done(ba) is False)
    finally:
        (ba._POOL_H_MODE_OVERRIDE, ba._POOL_H_RESCUE_UNTIL,
         ba._POOL_H_RESCUE_SHARED, ba.detect_antibot,
         ba.try_challenge_autoclick, wl.atomic_write_json) = _sv_l
        wl.logger.removeHandler(_k_handler)
        wl.logger.setLevel(_k_level)
        for s in _l_sites:
            wl.clear_quarantine(s)
        wl.pop_quarantine_alerts()
        wl._LOGIN_PROBE_AT.clear()
        _rescue_file.unlink(missing_ok=True)
        for p in _l_files:
            p.unlink(missing_ok=True)

    # ── 6f. «Реформулировка вместо ответа»: страница чата непрогрета —
    #       baseline=0, хотя в ленте уже лежит СТАРЫЙ завершённый ответ
    #       (реплика coref), и baseline-путь вернул бы именно её. Якорный
    #       путь ждёт блок ПОСЛЕ нашего сообщения ──
    llm10 = wl.WebChatLLM("qwen", base_dir=tmp / "q10")
    llm10._save_state({"chat_url": "https://chat.qwen.ai/c/c1"})
    _open10, _nav10 = ba.open_new_tab, ba.navigate_tab
    _cfs10, _lbt10 = ba.chat_fill_send, ba.last_block_text
    _cnt10, _url10, _ev10 = ba.count_blocks, ba.tab_url, ba.eval_js
    ba.open_new_tab = lambda url, **kw: 42
    ba.navigate_tab = lambda url, tab_id=None: None
    ba.tab_url = lambda *a, **kw: "https://chat.qwen.ai/c/c1"
    ba.chat_fill_send = lambda *a, **kw: "sent"
    ba.eval_js = lambda *a, **kw: "ok"
    ba.count_blocks = lambda *a, **kw: 0  # история ещё не отрендерилась
    def _lbt10(host, tid, sels=None, **kw):
        if sels and list(sels) == (llm10.adapter.get("user") or []):
            return "User: привет"   # подтверждение отправки
        return "старая реформулировка"      # её вернул бы baseline-путь
    ba.last_block_text = _lbt10
    # done=False до завершения генерации — стабильность не считается
    after = iter([(0, "", False), (1, "насто", False),
                  (1, "настоящий ответ", False), (1, "настоящий ответ", False),
                  (1, "настоящий ответ", True), (1, "настоящий ответ", True),
                  (1, "настоящий ответ", True)])
    ba.answer_blocks_after = lambda *a, **kw: next(after)
    try:
        res10 = llm10.get_response([{"role": "user", "content": "привет"}])
        check("anchor: старый завершённый блок НЕ возвращён — ждали ответ "
              "после своего сообщения", res10 == "настоящий ответ")
    finally:
        ba.open_new_tab, ba.navigate_tab = _open10, _nav10
        ba.chat_fill_send, ba.last_block_text = _cfs10, _lbt10
        ba.count_blocks, ba.tab_url, ba.eval_js = _cnt10, _url10, _ev10
        ba.answer_blocks_after = lambda *a, **kw: (None, "", True)

    # ── 6g. Якорь не найден (лента виртуализована и съела своё сообщение):
    #       разовый откат на baseline-путь ──
    llm11 = wl.WebChatLLM("qwen", base_dir=tmp / "q11")
    _open11, _cfs11 = ba.open_new_tab, ba.chat_fill_send
    _lbt11, _cnt11 = ba.last_block_text, ba.count_blocks
    _url11, _ev11 = ba.tab_url, ba.eval_js
    ba.open_new_tab = lambda url, **kw: 42
    ba.tab_url = lambda *a, **kw: "https://chat.qwen.ai/c/c2"
    ba.chat_fill_send = lambda *a, **kw: "sent"
    ba.eval_js = lambda *a, **kw: "ok"
    ba.answer_blocks_after = lambda *a, **kw: (None, "", True)  # якоря нет
    cnts11 = iter([0, 1, 1, 1])   # baseline=0, затем новый блок
    txts11 = iter(["", "ок", "ок", "ок"])
    ba.count_blocks = lambda *a, **kw: next(cnts11)
    def _lbt11(host, tid, sels=None, **kw):
        if sels and list(sels) == (llm11.adapter.get("user") or []):
            return "User: привет"
        return next(txts11)
    ba.last_block_text = _lbt11
    try:
        res11 = llm11.get_response([{"role": "user", "content": "привет"}])
        check("anchor-fallback: якоря нет — baseline-путь ловит новый блок",
              res11 == "ок")
    finally:
        ba.open_new_tab, ba.chat_fill_send = _open11, _cfs11
        ba.last_block_text, ba.count_blocks = _lbt11, _cnt11
        ba.tab_url, ba.eval_js = _url11, _ev11
        ba.answer_blocks_after = lambda *a, **kw: (None, "", True)

    # ── 7. Роутер: позиция webchat-токенов в цепочке ──
    from app.core.router import ModelRouter

    def _stub_router(sites):
        r = ModelRouter.__new__(ModelRouter)  # без __init__: ключи/env не нужны
        r.available = {}
        r.active_provider = None
        r.pinned_provider = None
        r.fallback_order = None
        r.excluded = set()
        r.model_overrides = {}
        r.webchat_sites = list(sites)
        r._webchats = {}
        r.webchat_limits = {}
        r._last_key_index = {}
        r.answer_provider = None
        r.cc_provider = None
        r.vision_provider = None
        return r

    r = _stub_router(["qwen", "deepseek"])
    check("router: веб-чаты по умолчанию после облачных, перед local",
          r._get_full_order() == ["webchat:qwen", "webchat:deepseek", "local"])
    r.active_provider = "local"
    check("router: основной local — веб-чаты последний рубеж (local не дублируется)",
          r._get_full_order() == ["webchat:qwen", "webchat:deepseek"])
    r.active_provider = None
    r.webchat_sites = []
    check("router: веб-чаты выключены — цепочка как раньше",
          r._get_full_order() == ["local"])

    # ── 8. set_persona_llm: сайты, токены, primary ──
    r.set_persona_llm(None, ["webchat"], None, webchat="deepseek")
    check("router: llm.webchat из YAML принят, голый webchat развёрнут в сайт",
          r.webchat_site == "deepseek" and r.fallback_order == ["webchat:deepseek"])
    r.set_persona_llm("webchat", None, None)
    check("router: primary=webchat — активный, сайт сохраняется",
          r.active_provider == "webchat" and r.webchat_site == "deepseek")
    r.set_persona_llm(None, None, None, webchat="ya.ru")
    check("router: неизвестный webchat-сайт отклонён, прежний сохранён",
          r.webchat_site == "deepseek")
    r.set_persona_llm("webchat:qwen", ["webchat:deepseek", "local"])
    check("router: primary=webchat:<сайт> закреплён, сайт добавлен в список",
          r.pinned_provider == "webchat:qwen"
          and r.webchat_sites == ["deepseek", "qwen"]
          and r.fallback_order == ["webchat:deepseek", "local"])
    check("router: закреплённый сайт первым не дублируется в цепочке",
          r._get_full_order() == ["webchat:deepseek", "local", "webchat:qwen"])
    r.available = {"zai": {"model": "m", "api_keys": ["k"]}}
    r.set_persona_llm("zai", ["webchat:deepseek", "webchat:qwen", "local"])
    check("router: порядок нескольких веб-чатов в fallback сохраняется",
          r._get_full_order() == ["zai", "webchat:deepseek", "webchat:qwen", "local"])

    # ── 8b. llm.exclude: персона убирает провайдер из СВОЕЙ автоматической
    # цепочки (в отличие от exclude_provider — разового параметра вызова,
    # см. секцию 10) ──
    r.set_persona_llm("zai", ["webchat:deepseek", "webchat:qwen", "local"],
                      exclude=["webchat:qwen"])
    check("llm.exclude: токен нормализован в webchat:<сайт>",
          r.excluded == {"webchat:qwen"})
    check("llm.exclude: исключённый сайт выпал из цепочки, остальное на месте",
          r._get_full_order() == ["zai", "webchat:deepseek", "local"])

    r.set_persona_llm("zai", ["webchat:deepseek", "webchat:qwen", "local"],
                      exclude=["zai"])
    check("llm.exclude: собственный primary исключить нельзя — цепочка как без исключения",
          r._get_full_order() == ["zai", "webchat:deepseek", "webchat:qwen", "local"])

    r.set_persona_llm("zai", ["webchat:deepseek", "webchat:qwen", "local"],
                      exclude=["local"])
    check("llm.exclude: local (не primary) исключён из хвоста цепочки",
          r._get_full_order() == ["zai", "webchat:deepseek", "webchat:qwen"])

    r.set_persona_llm("zai", ["webchat:deepseek", "webchat:qwen", "local"],
                      exclude=["webchat"])
    check("llm.exclude: голый webchat разворачивается в текущие сайты персоны",
          r.excluded == {"webchat:deepseek", "webchat:qwen"})
    check("llm.exclude: все веб-чаты выпали из цепочки",
          r._get_full_order() == ["zai", "local"])

    r.set_persona_llm("zai", ["webchat:deepseek", "webchat:qwen", "local"])
    check("llm.exclude: не передан (None) — исключения сброшены",
          r.excluded == set()
          and r._get_full_order() == ["zai", "webchat:deepseek", "webchat:qwen", "local"])

    r8 = _stub_router(["qwen", "deepseek"])
    r8.active_provider = "webchat"
    r8.excluded = {"webchat:qwen"}
    check("llm.exclude: _filter_excluded_sites убирает сайт даже под общим primary=webchat",
          r8._filter_excluded_sites(r8.webchat_sites) == ["deepseek"])
    r8.active_provider = "webchat:qwen"
    check("llm.exclude: _filter_excluded_sites не трогает сайт-primary (webchat:qwen)",
          r8._filter_excluded_sites(["qwen"]) == ["qwen"])

    # ── 9. get_response через webchat (стабы вместо живого WebChatLLM) ──
    class _StubWebchat:
        def __init__(self, site, answer):
            self.site = site
            self._answer = answer
            self.calls = 0

        def get_response(self, messages, **kw):
            self.calls += 1
            return self._answer

    r._webchats = {"deepseek": _StubWebchat("deepseek", "ответ из веб-чата")}
    r.webchat_sites = ["deepseek"]
    r.active_provider = "webchat"
    ans = r.get_response([{"role": "user", "content": "привет"}])
    check("router: get_response основным webchat — ответ, провайдер помечен",
          ans == "ответ из веб-чата" and r._last_provider == "webchat:deepseek")

    # ── 9b. Перебор сайтов: первый молчит — отвечает второй ──
    r._webchats = {"qwen": _StubWebchat("qwen", None),
                   "deepseek": _StubWebchat("deepseek", "второй ответил")}
    r.webchat_sites = ["qwen", "deepseek"]
    ans = r.get_response([{"role": "user", "content": "привет"}])
    check("router: fallback между веб-чатами (qwen→deepseek)",
          ans == "второй ответил" and r._last_provider == "webchat:deepseek")

    # ── 9c. Стриминг: webchat отдаёт ответ одним куском ──
    tokens = []
    ans = r.get_response_stream([{"role": "user", "content": "привет"}],
                                tokens.append)
    check("router: stream — webchat одним куском через on_token",
          ans == "второй ответил" and tokens == ["второй ответил"])

    # ── 10. exclude_provider: пропускает и основную ветку (LTM-путь) ──
    r = _stub_router(["qwen"])
    r.available = {"groq": {"model": "m", "api_keys": ["k"]}}
    r.active_provider = "local"
    r.pinned_provider = "local"
    local_calls = []
    r._try_local = lambda *a, **kw: local_calls.append(1) or None
    r._webchats = {"qwen": _StubWebchat("qwen", "ltm ответ")}
    ans = r.get_response([{"role": "user", "content": "x"}], exclude_provider="local")
    check("router: exclude local — основная ветка пропущена, ответил fallback webchat",
          ans == "ltm ответ" and not local_calls and r._last_provider == "webchat:qwen")
    r.set_persona_llm("local", ["webchat:qwen", "groq"])
    check("router: при основном local персональный fb-порядок соблюдается в цепочке",
          r._get_full_order() == ["webchat:qwen", "groq"])

    # ── 10b. exclude голый webchat — все веб-чаты разом ──
    r2 = _stub_router(["qwen", "deepseek"])
    r2.active_provider = "webchat"
    r2.pinned_provider = "webchat"
    q_stub, d_stub = _StubWebchat("qwen", "ответ"), _StubWebchat("deepseek", "ответ")
    r2._webchats = {"qwen": q_stub, "deepseek": d_stub}
    r2._try_local = lambda *a, **kw: None  # не ходим в реальную Ollama
    ans = r2.get_response([{"role": "user", "content": "x"}], exclude_provider="webchat")
    check("router: exclude webchat — веб-чаты пропущены полностью",
          ans is None and q_stub.calls == 0 and d_stub.calls == 0)

    # ── 10c. exclude webchat:<сайт> — пропущен только он ──
    ans = r2.get_response([{"role": "user", "content": "x"}],
                          exclude_provider="webchat:qwen")
    check("router: exclude webchat:qwen — qwen пропущен, deepseek ответил",
          ans == "ответ" and q_stub.calls == 0 and d_stub.calls == 1
          and r2._last_provider == "webchat:deepseek")

    # ── 10d. llm.exclude через get_response: исключённый сайт персона
    # никогда не пробует сама (в отличие от 10b/10c — те про разовый
    # exclude_provider вызова, не про постоянную настройку персоны) ──
    r7 = _stub_router(["qwen", "deepseek"])
    r7.active_provider = "webchat"
    r7.pinned_provider = "webchat"
    q7, d7 = _StubWebchat("qwen", "ответ qwen"), _StubWebchat("deepseek", "ответ deepseek")
    r7._webchats = {"qwen": q7, "deepseek": d7}
    r7._try_local = lambda *a, **kw: None
    r7.set_persona_llm(None, exclude=["webchat:qwen"])
    ans = r7.get_response([{"role": "user", "content": "x"}])
    check("llm.exclude: исключённый сайт не пробуется даже как часть общего primary=webchat",
          ans == "ответ deepseek" and q7.calls == 0 and d7.calls == 1)

    # Исключён сайт, который сам — закреплённый primary: исключение не действует
    r7b = _stub_router(["qwen", "deepseek"])
    q7b = _StubWebchat("qwen", "ответ от закреплённого")
    r7b._webchats = {"qwen": q7b}
    r7b._try_local = lambda *a, **kw: None
    r7b.set_persona_llm("webchat:qwen", exclude=["webchat:qwen"])
    ans = r7b.get_response([{"role": "user", "content": "x"}])
    check("llm.exclude: primary=webchat:qwen игнорирует своё же исключение — отвечает",
          ans == "ответ от закреплённого" and q7b.calls == 1)

    # exclude=[] снимает исключения
    r7.set_persona_llm(None, exclude=[])
    check("llm.exclude: пустой список снимает исключения", r7.excluded == set())

    # Офлайн-шорткат (get_response): исключённый local (не primary) не трогается
    r7c = _stub_router([])
    r7c.set_persona_llm(None, exclude=["local"])
    r7c.active_provider = "не-local-и-не-облако"  # точно не primary=local
    local_calls_c = []
    r7c._try_local = lambda *a, **kw: local_calls_c.append(1) or None
    _net_router.internet_available = lambda: False
    try:
        ans = r7c.get_response([{"role": "user", "content": "x"}])
    finally:
        _net_router.internet_available = lambda: True
    check("llm.exclude: офлайн-шорткат не трогает исключённый local (не primary)",
          not local_calls_c)

    # Но local — сам primary: своё же исключение не действует, шорткат пробует его
    r7d = _stub_router([])
    r7d.set_persona_llm("local", exclude=["local"])
    local_calls_d = []
    r7d._try_local = lambda *a, **kw: local_calls_d.append(1) or "офлайн-ответ"
    _net_router.internet_available = lambda: False
    try:
        ans = r7d.get_response([{"role": "user", "content": "x"}])
    finally:
        _net_router.internet_available = lambda: True
    check("llm.exclude: primary=local — исключение самого себя не действует",
          ans == "офлайн-ответ" and local_calls_d == [1])

    # ── 11. Каналы: side-чат изолирован от main ──
    llm_m = wl.WebChatLLM("qwen", base_dir=tmp / "ch")
    llm_s = wl.WebChatLLM("qwen", base_dir=tmp / "ch", channel="side")
    llm_m._save_state({"chat_url": "https://chat.qwen.ai/c/main",
                       "date": today, "count": 7})
    check("channel: у side свой ключ состояния и пустое состояние",
          llm_s._state_key == "qwen#side" and llm_s._load_state() == {}
          and llm_m._chat_url() == "https://chat.qwen.ai/c/main")
    llm_s._save_state({"chat_url": "https://chat.qwen.ai/c/side"})
    check("channel: main и side не пересекаются (чаты и квоты раздельны)",
          llm_s._chat_url() == "https://chat.qwen.ai/c/side"
          and llm_m._chat_url() == "https://chat.qwen.ai/c/main")

    # ── 11a. Канал «cc»: свой ключ состояния; свежий тред на каждый вызов
    #        — только на поисковике (google); на чат-сайтах постоянный чат
    #        канала (01.10: свежие чаты cc — 60 за ~50 минут, аккаунт
    #        deepseek заблокировали на 3 дня) ──
    llm_cc = wl.WebChatLLM("qwen", base_dir=tmp / "ch", channel="cc")
    check("cc: свой ключ состояния; на чат-сайте не stateless, на google — "
          "stateless; main/side не тронуты",
          llm_cc.stateless is False and llm_cc._state_key == "qwen#cc"
          and wl.WebChatLLM("google", base_dir=tmp / "ch",
                            channel="cc").stateless is True
          and llm_m.stateless is False and llm_s.stateless is False)
    llm_dc = wl.WebChatLLM("deepseek", base_dir=tmp / "dcc", channel="cc")
    flow_c = {"open": [], "nav": [], "sent": 0}
    _saved_c = (ba.open_new_tab, ba.navigate_tab, ba.chat_fill_send,
                ba.last_block_text, ba.count_blocks, ba.tab_url)
    ba.open_new_tab = lambda url, **kw: (flow_c["open"].append(url), 42)[1]
    ba.navigate_tab = lambda url, tab_id=None: flow_c["nav"].append(url)
    ba.tab_url = lambda *a, **kw: ("https://chat.deepseek.com/a/chat/s/cc1"
                                   if flow_c["sent"] else
                                   wl.ADAPTERS["deepseek"]["home"])
    ba.chat_fill_send = lambda *a, **kw: (
        flow_c.__setitem__("sent", flow_c["sent"] + 1), "sent")[1]
    ba.count_blocks = lambda *a, **kw: 1  # deepseek: один обмен в DOM

    def _lbt_c(host, tid, sels=None, **kw):
        if sels and list(sels) == (llm_dc.adapter.get("user") or []):
            return "User: hi"
        return f"ответ {flow_c['sent']}" if flow_c["sent"] else "старый"
    ba.last_block_text = _lbt_c
    try:
        a1 = llm_dc.get_response([{"role": "user", "content": "hi"}])
        a2 = llm_dc.get_response([{"role": "user", "content": "hi"}])
        check("cc на deepseek: один чат на канал — второй вызов в "
              "сохранённый чат (без перехода на home), адрес запомнен",
              a1 == "ответ 1" and a2 == "ответ 2"
              and flow_c["open"] == [wl.ADAPTERS["deepseek"]["home"]]
              and flow_c["nav"] == []
              and llm_dc._chat_url()
              == "https://chat.deepseek.com/a/chat/s/cc1")
    finally:
        (ba.open_new_tab, ba.navigate_tab, ba.chat_fill_send,
         ba.last_block_text, ba.count_blocks, ba.tab_url) = _saved_c

    # ── 11a'. duck.ai (01.10): постоянный чат, модель Gemma 4 31B, промпт
    #         длиннее 16 тыс. — сразу следующему провайдеру; «временно
    #         недоступен» (анти-бот) — карантин, не сброс чата ──
    llm_dk = wl.WebChatLLM("duckai", base_dir=tmp / "dk", channel="cc")
    check("duck.ai: cc — постоянный чат; модель в JS — gemma4-31b",
          llm_dk.stateless is False
          and '"tinfoil/gemma4-31b"' in (llm_dk._mode_js() or ""))
    touched = []
    _saved_dk = (ba.open_new_tab, ba.chat_fill_send, ba.tab_url)
    ba.open_new_tab = lambda *a, **kw: touched.append("open") or 42
    ba.chat_fill_send = lambda *a, **kw: touched.append("send") or "sent"
    ba.tab_url = lambda *a, **kw: touched.append("url") or ""
    try:
        long_ans = llm_dk.get_response([{"role": "user", "content": "я" * 16001}])
        check("duck.ai: промпт > 16 тыс. — None без вкладки и отправки",
              long_ans is None and touched == [])
    finally:
        ba.open_new_tab, ba.chat_fill_send, ba.tab_url = _saved_dk
    seen_kw = []
    _cfs_dk = ba.chat_fill_send
    ba.chat_fill_send = lambda *a, **kw: seen_kw.append(kw) or "sent"
    try:
        llm_dk._fill_send(ba, "duck.ai", 42, "привет")
        wl.WebChatLLM("qwen", base_dir=tmp / "dk")._fill_send(
            ba, "chat.qwen.ai", 42, "привет")
        check("duck.ai: Enter с символом «\\r» (enter_text) — только ему; "
              "qwen — вызов как раньше",
              seen_kw == [{"enter_text": True}, {}])
    finally:
        ba.chat_fill_send = _cfs_dk
    dk_err = ("Упс... Сервис Duck.ai временно недоступен. Если ошибка "
              "повторится, отправьте код 02f8 на адрес aichat-error@duckduckgo.com.")
    check("duck.ai: «временно недоступен» — ошибка сайта и признак отказа "
          "(карантин), обычный ответ со словом «недоступен» — нет",
          any(rx.search(dk_err) for rx in wl._CHAT_ERROR_RES)
          and wl._OVERLOAD_RE.search(dk_err) is not None
          and not any(rx.search("Сайт банка временно недоступен, попробуй "
                                "позже") for rx in wl._CHAT_ERROR_RES))

    # ── 11b. Роутер: webchat_channel="side" — отдельный экземпляр ──
    r3 = _stub_router(["qwen"])
    r3.active_provider = "webchat"
    r3.pinned_provider = "webchat"
    side_stub = _StubWebchat("qwen", "side ответ")
    r3._webchats = {"qwen#side": side_stub}
    ans = r3.get_response([{"role": "user", "content": "x"}], webchat_channel="side")
    check("router: webchat_channel=side — отдельный side-чат, main не создан",
          ans == "side ответ" and side_stub.calls == 1
          and "qwen" not in r3._webchats)

    # ── 11c. force_provider: назначенный провайдер вне цепочки ──
    r5 = _stub_router(["qwen", "deepseek"])
    r5.active_provider = "webchat"  # forced-ветка раньше основного webchat
    r5._webchats = {"deepseek": _StubWebchat("deepseek", "назначенный")}
    ans = r5.get_response([{"role": "user", "content": "x"}],
                          force_provider="webchat:deepseek")
    check("force_provider: назначенный сайт отвечает первым, вне позиции",
          ans == "назначенный" and r5._webchats["deepseek"].calls == 1)
    r5._webchats["deepseek"]._answer = None  # назначенный молчит
    r5._webchats["qwen"] = _StubWebchat("qwen", "цепочка")
    ans = r5.get_response([{"role": "user", "content": "x"}],
                          force_provider="webchat:deepseek")
    check("force_provider: молчание назначенного — fallback по цепочке",
          ans == "цепочка")
    r6 = _stub_router(["qwen"])
    r6.set_persona_llm(None, answer_provider="webchat:qwen",
                       cc_provider="local", vision_provider="неттакого")
    check("router: answer/cc/vision_provider из llm-секции (мусор — в цепочку)",
          r6.answer_provider == "webchat:qwen" and r6.cc_provider == "local"
          and r6.vision_provider is None)

    # ── 12. Лимиты веб-чатов персоны (llm.webchat_limits) ──
    r4 = _stub_router(["qwen"])
    check("limits: без записи — дефолт QUOTA_PER_HOUR",
          r4._webchat_quota_for("qwen") == wl.QUOTA_PER_HOUR)
    r4.set_persona_llm(None, webchat_limits={
        "qwen": {"enabled": True, "per_hour": 10},
        "deepseek": {"enabled": False},
        "unknown": {"enabled": True, "per_hour": 5},  # нет адаптера — мимо
        "qwen2": "мусор",
    })
    check("limits: override 10/ч, снятый — None (бесконечно), мусор отброшен",
          r4._webchat_quota_for("qwen") == 10
          and r4._webchat_quota_for("deepseek") is None
          and r4._webchat_quota_for("claude") == wl.QUOTA_PER_HOUR
          and "unknown" not in r4.webchat_limits
          and "qwen2" not in r4.webchat_limits)

    # ── 13. Адаптеры: обязательные поля у всех сайтов ──
    req = ("host", "home", "input", "answer", "user")
    check("adapters: у всех сайтов host/home/input/answer/user; zai+chatgpt есть",
          all(all(k in a for k in req) for a in wl.ADAPTERS.values())
          and {"deepseek", "qwen", "claude", "zai", "chatgpt"} <= set(wl.ADAPTERS))

    # ── 14. Vision через веб-чат (get_response_with_image) ──
    # 14a. Сайт без adapter["images"] — честный None, браузер не трогаем
    llm_d = wl.WebChatLLM("chatgpt", base_dir=tmp / "vd")
    check("vision-webchat: адаптер без images → None до всякого браузера",
          llm_d.get_response_with_image("что на картинке?", b"png") is None)

    # 14a2. deepseek: images включены; модель чата по каналу — vision-канал
    # (картинки) → «Vision», текстовые → «Instant» (пилюли задаются при
    # создании чата — в существующем их нет)
    check("deepseek: images включён, режимы по каналам заданы",
          wl.ADAPTERS["deepseek"].get("images") is True
          and wl.ADAPTERS["deepseek"]["mode_by_channel"] == {"vision": "Vision"}
          and wl.ADAPTERS["deepseek"]["mode_default"] == "Instant")
    llm_mv = wl.WebChatLLM("deepseek", base_dir=tmp / "vmv", channel="vision")
    llm_mt = wl.WebChatLLM("deepseek", base_dir=tmp / "vmt")
    llm_mq = wl.WebChatLLM("qwen", base_dir=tmp / "vmq")
    _mv, _mt, _mq = llm_mv._mode_js(), llm_mt._mode_js(), llm_mq._mode_js()
    check("deepseek: vision-канал → пилюля Vision, текстовый → Instant",
          _mv is not None and '"Vision"' in _mv
          and _mt is not None and '"Instant"' in _mt)
    check("qwen: mode_js без плейсхолдера — как было (Fast)",
          _mq is not None and "%s" not in _mq and "fast" in _mq.lower())

    # 14b. Happy path: paste картинки → ожидание аплоада → отправка/ожидание
    llm_v = wl.WebChatLLM("qwen", base_dir=tmp / "vq")
    vcalls = {"open": [], "send": [], "paste": [], "wait": []}
    _open, _nav = ba.open_new_tab, ba.navigate_tab
    _send, _read = ba.chat_fill_send, ba.last_block_text
    _cnt, _url, _paste = ba.count_blocks, ba.tab_url, ba.chat_paste_image
    _waitu = ba.chat_wait_uploaded
    ba.open_new_tab = lambda url, **kw: (vcalls["open"].append(url), 42)[1]
    ba.navigate_tab = lambda url, tab_id=None: None
    ba.tab_url = lambda *a, **kw: "https://chat.qwen.ai/c/vision-1"
    ba.chat_fill_send = lambda host, tab_id, sel, text: (
        vcalls["send"].append(text), "sent")[1]
    ba.chat_paste_image = lambda host, tab_id, sel, img, mime="image/png": (
        vcalls["paste"].append((sel, bytes(img), mime)), True)[1]
    ba.chat_wait_uploaded = lambda host, tab_id, sel, **kw: (
        vcalls["wait"].append(sel), True)[1]
    counts = iter([0, 1, 1, 1, 1])
    texts = iter(["", "42", "42", "42", "42"])
    ba.count_blocks = lambda *a, **kw: next(counts)

    def _lbt_vis(host, tid, sels=None, **kw):
        if sels and list(sels) == (llm_v.adapter.get("user") or []):
            return vcalls["send"][-1] if vcalls["send"] else ""
        return next(texts)
    ba.last_block_text = _lbt_vis
    try:
        res_v = llm_v.get_response_with_image("что на картинке?", b"\x89PNG...")
        check("vision-webchat: картинка вставлена paste'ом ДО отправки текста",
              res_v == "42" and len(vcalls["paste"]) == 1
              and vcalls["paste"][0][0] == llm_v.adapter["input"]
              and vcalls["paste"][0][1] == b"\x89PNG..."
              and len(vcalls["send"]) == 1)
        check("vision-webchat: конец аплоада ждали между paste и отправкой",
              vcalls["wait"] == [llm_v.adapter["input"]])
        # 14c. Paste не подтвердился сайтом → None, текст не шлём, квоту не тратим
        ba.chat_paste_image = lambda *a, **kw: False
        counts = iter([0])
        texts = iter([""])
        res_v2 = llm_v.get_response_with_image("ещё раз", b"\x89PNG...")
        check("vision-webchat: paste не сработал → None, текст не отправлен",
              res_v2 is None and len(vcalls["send"]) == 1)
        check("vision-webchat: paste не сработал → аплоад не ждали",
              len(vcalls["wait"]) == 1)
    finally:
        ba.open_new_tab, ba.navigate_tab = _open, _nav
        ba.chat_fill_send, ba.last_block_text = _send, _read
        ba.count_blocks, ba.tab_url, ba.chat_paste_image = _cnt, _url, _paste
        ba.chat_wait_uploaded = _waitu

    # 14g. _send_verified(wait_upload=True): перед повтором ждёт конца
    # аплоада (первая отправка могла упереться в тост «files still uploading»)
    _sv9 = wl.SEND_VERIFY_SEC
    wl.SEND_VERIFY_SEC = 0.3
    _cfs9, _lbt9, _cwu9 = ba.chat_fill_send, ba.last_block_text, \
        ba.chat_wait_uploaded
    llm10 = wl.WebChatLLM("qwen", base_dir=tmp / "q10")
    seq10 = []
    done10 = {"v": False}

    def _send10(*a, **kw):
        seq10.append("send")
        if seq10.count("send") >= 2:
            done10["v"] = True
        return "sent"

    ba.chat_fill_send = _send10
    ba.chat_wait_uploaded = lambda *a, **kw: (seq10.append("wait"), True)[1]
    ba.last_block_text = lambda host, tid, sels=None, **kw: (
        "привет" if done10["v"] else "чужое")
    try:
        mk10 = llm10._send_verified(ba, "chat.qwen.ai", 42, "привет",
                                    wait_upload=True)
        check("send-verify: с картинкой перед повтором — ожидание аплоада",
              mk10 == "привет" and seq10 == ["send", "wait", "send"])
    finally:
        wl.SEND_VERIFY_SEC = _sv9
        ba.chat_fill_send, ba.last_block_text = _cfs9, _lbt9
        ba.chat_wait_uploaded = _cwu9

    # 14d. Router: облака мертвы → vision уходит в веб-чат с images-флагом
    class _StubWebchatV:
        def __init__(self, site, answer):
            self.site = site
            self._answer = answer
            self.calls = 0

        def get_response_with_image(self, prompt, image_bytes, **kw):
            self.calls += 1
            return self._answer

    rv = _stub_router(["qwen", "deepseek"])
    rv._vision_verdict = {}
    rv.available = {"kimi": {"model": "m", "api_keys": ["k"],
                             "base_url": "x", "vision": "true"}}
    rv.active_provider = "kimi"
    cloud_calls = []
    rv._call_with_keys = lambda *a, **kw: (cloud_calls.append(1), None)[1]
    stub_v = _StubWebchatV("qwen", "3")
    rv._webchats = {"qwen#vision": stub_v}
    ans_v = rv.get_response_with_image("номер?", b"img", image_mime="image/png")
    check("vision-router: облако молчит → ответил веб-чат, провайдер помечен",
          ans_v == "3" and stub_v.calls == 1
          and rv._last_provider == "webchat:qwen" and cloud_calls)
    # 14e. Основной — веб-чат: он первый, облако не дёргается
    rv2 = _stub_router(["qwen"])
    rv2._vision_verdict = {}
    rv2.available = {"kimi": {"model": "m", "api_keys": ["k"],
                              "base_url": "x", "vision": "true"}}
    rv2.active_provider = "webchat"
    rv2.pinned_provider = "webchat"
    cloud2 = []
    rv2._call_with_keys = lambda *a, **kw: (cloud2.append(1), "облако")[1]
    stub_v2 = _StubWebchatV("qwen", "веб")
    rv2._webchats = {"qwen#vision": stub_v2}
    ans_v2 = rv2.get_response_with_image("номер?", b"img")
    check("vision-router: основной webchat — он первым, облако не тронуто",
          ans_v2 == "веб" and stub_v2.calls == 1 and not cloud2)
    # 14j. Персональный fallback соблюдается и для vision: webchat:qwen в
    # fallback-списке РАНЬШЕ groq → после молчащего kimi отвечает именно он
    rv8 = _stub_router(["qwen"])
    rv8._vision_verdict = {}
    rv8.available = {"kimi": {"model": "m", "api_keys": ["k"],
                              "base_url": "x", "vision": "true"},
                     "groq": {"model": "m", "api_keys": ["k"],
                              "base_url": "x", "vision": "true"}}
    rv8.active_provider = "kimi"
    rv8.pinned_provider = "kimi"
    rv8.fallback_order = ["webchat:qwen"]
    calls8 = []

    def _cw8(provider, *a, **kw):
        calls8.append(provider)
        return None  # все облака молчат
    rv8._call_with_keys = _cw8
    stub8 = _StubWebchatV("qwen", "веб по приоритету персоны")
    rv8._webchats = {"qwen#vision": stub8}
    ans8 = rv8.get_response_with_image("номер?", b"img")
    check("vision-router: fallback персоны соблюдён (webchat раньше groq)",
          ans8 == "веб по приоритету персоны" and stub8.calls == 1
          and calls8 == ["kimi"])
    # 14f. Сайт без флага images — None
    rv3 = _stub_router(["kimi"])
    rv3._vision_verdict = {}
    check("vision-router: веб-чаты без images — честный None",
          rv3.get_response_with_image("номер?", b"img") is None)
    # 14g. supports_vision: без облаков, но с картиночным веб-чатом — True
    rv4 = _stub_router(["qwen"])
    rv4._vision_verdict = {}
    rv5 = _stub_router(["kimi"])
    rv5._vision_verdict = {}
    check("vision-router: supports_vision учитывает webchat-флаг images",
          rv4.supports_vision() is True and rv5.supports_vision() is False)
    # 14h. Проба vision: ошибка сети НЕ кешируется (транзиент ≠ слепая модель)
    rv6 = _stub_router([])
    rv6._vision_verdict = {}
    probes = []
    def _boom_probe(*a, **kw):
        probes.append(1)
        raise RuntimeError("429")
    rv6._call_with_keys = _boom_probe
    v1 = rv6._probe_vision("kimi", {"model": "m", "api_keys": ["k"]})
    v2 = rv6._probe_vision("kimi", {"model": "m", "api_keys": ["k"]})
    check("vision-router: ошибка пробы не кешируется (повторная проба)",
          v1 is False and v2 is False and len(probes) == 2
          and "kimi" not in rv6._vision_verdict)
    # 14i. Полный отвал ключей (ответ None) — тоже транзиент, не кешируем
    rv7 = _stub_router([])
    rv7._vision_verdict = {}
    probes7 = []
    rv7._call_with_keys = lambda *a, **kw: (probes7.append(1), None)[1]
    w1 = rv7._probe_vision("zai", {"model": "m", "api_keys": ["k"]})
    w2 = rv7._probe_vision("zai", {"model": "m", "api_keys": ["k"]})
    check("vision-router: None от всех ключей не кешируется (повторная проба)",
          w1 is False and w2 is False and len(probes7) == 2
          and "zai" not in rv7._vision_verdict)

    # ── 15. Поле ввода по цели (input_goal): селектор протух → снапшот ──
    _cfs15 = ba.chat_fill_send
    _cft15 = ba.chat_fill_send_tagged
    _snap15 = ba.snapshot_elements
    sent_sel = []
    sent_tagged = []

    def _cfs_boom(host, tab_id, sel, text):
        sent_sel.append(sel)
        raise ba.BrowserUnavailable("поле чата не приняло ввод: не нашлось")

    ba.chat_fill_send = _cfs_boom
    ba.chat_fill_send_tagged = lambda host, tab_id, idx, text: (
        sent_tagged.append(idx), "sent")[1]
    llm15 = wl.WebChatLLM("qwen", base_dir=tmp / "q15")
    try:
        # Одно видимое поле — берётся без скоринга
        ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://chat.qwen.ai", "chat.qwen.ai",
            [{"idx": 7, "tag": "textarea", "ed": True, "text": "",
              "aria": ""}])
        llm15._fill_send(ba, "chat.qwen.ai", 42, "привет")
        check("goal-фолбэк: селектор протух → ввод по метке единственного поля",
              sent_sel and sent_tagged == [7])
        # Поле находится скорингом по подписи среди нескольких
        sent_tagged.clear()
        ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://chat.qwen.ai", "chat.qwen.ai",
            [{"idx": 3, "tag": "input", "ed": True, "text": "Поиск по чатам",
              "aria": ""},
             {"idx": 8, "tag": "textarea", "ed": True, "text": "",
              "aria": "Send a Message"}])
        llm15._fill_send(ba, "chat.qwen.ai", 42, "привет")
        check("goal-фолбэк: поле выбрано скорингом по подписи",
              sent_tagged == [8])
        # Поля нет вообще — честная ошибка исходного селектора
        ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://chat.qwen.ai", "chat.qwen.ai", [])
        try:
            llm15._fill_send(ba, "chat.qwen.ai", 42, "привет")
            _raised15 = False
        except Exception:
            _raised15 = True
        check("goal-фолбэк: полей нет — исходная ошибка, а не тихий промах",
              _raised15)
    finally:
        ba.chat_fill_send = _cfs15
        ba.chat_fill_send_tagged = _cft15
        ba.snapshot_elements = _snap15

    # ── 16. Rate-limit: паттерны, парсинг TTL, карантин с таймером ──
    check("ratelimit-res: «out of free messages», «message limit», русский лимит",
          any(rx.search("You're out of free messages. Try again in 5 hours.")
              for rx in wl._RATE_LIMIT_RES)
          and any(rx.search("You've reached your message limit")
                  for rx in wl._RATE_LIMIT_RES)
          and any(rx.search("Your message limit will reset at 3:00 PM")
                  for rx in wl._RATE_LIMIT_RES)
          and any(rx.search("Лимит сообщений исчерпан, восстановится через 3 часа")
                  for rx in wl._RATE_LIMIT_RES))
    check("ratelimit-res: обычный ответ не ловится",
          not any(rx.search("Открыл ютуб, как просили") for rx in wl._RATE_LIMIT_RES)
          and not any(rx.search("Попробуй снова через минуту")
                      for rx in wl._RATE_LIMIT_RES))
    check("reset-ttl: «через N часов/минут», «in N hours»",
          wl._parse_reset_ttl("лимит восстановится через 5 часов") == 18000.0
          and wl._parse_reset_ttl("try again in 2 hours") == 7200.0
          and wl._parse_reset_ttl("через 45 минут") == 2700.0)
    _ttl_t = wl._parse_reset_ttl("Your message limit will reset at 3:00 PM")
    check("reset-ttl: «resets at 3:00 PM» — секунды до сегодня/завтра",
          _ttl_t is not None and 0 < _ttl_t <= 86400)
    check("reset-ttl: время не указано — None (дефолт у caller'а)",
          wl._parse_reset_ttl("out of free messages") is None)
    # Карантин с кастомным TTL и kind
    wl.quarantine_site("testsite", "лимит сообщений", ttl=7200, kind="ratelimit")
    _q = wl._SITE_QUARANTINE.get("testsite")
    _alerts = wl.pop_quarantine_alerts()
    check("quarantine: TTL из лимита + kind=ratelimit + алерт с until",
          bool(_q) and abs(_q["until"] - (wl.time.time() + 7200)) < 5
          and _q["kind"] == "ratelimit"
          and any(a.get("site") == "testsite" and a.get("kind") == "ratelimit"
                  and a.get("until") for a in _alerts))
    check("quarantine: сайт мгновенно пропускается цепочкой",
          wl.site_quarantined("testsite"))
    wl.clear_quarantine("testsite")
    check("quarantine: снятие", not wl.site_quarantined("testsite"))

    # ── 17. Vision: max_images и переполнение (trim/followup) ──
    # google: max_images=1 — второй кадр не прикрепляется вовсе
    g17 = wl.WebChatLLM("google", base_dir=tmp / "g17")
    _seen17 = []
    g17._get_response_locked = lambda messages, t, mt, tp, to, **kw: (
        _seen17.append(kw.get("extra_image_bytes")), "ответ")[1]
    _r17 = g17.get_response_with_image("что тут?", b"img", extra_image=b"extra")
    check("max_images=1 (google): второй кадр не уходит, ответ получен",
          _r17 == "ответ" and _seen17 == [None])
    # trim: сайт отверг число кадров вопреки конфигу — повтор с одним
    d17 = wl.WebChatLLM("deepseek", base_dir=tmp / "d17")
    _calls17 = []

    def _locked_trim(messages, t, mt, tp, to, **kw):
        _calls17.append(kw.get("extra_image_bytes"))
        if kw.get("extra_image_bytes") is not None:
            raise wl._TooManyImages("too many images")
        return "ответ"

    d17._get_response_locked = _locked_trim
    _r17b = d17.get_response_with_image("что тут?", b"img", extra_image=b"extra")
    check("overflow trim: переполнение → повтор с одним кадром",
          _r17b == "ответ" and _calls17 == [b"extra", None])
    # followup: оставшийся кадр — вторым сообщением, финал — уточнённый ответ
    q17 = wl.WebChatLLM("qwen", base_dir=tmp / "q17")
    q17.adapter = {**q17.adapter, "max_images": 1, "image_overflow": "followup"}
    _calls17b = []

    def _locked_followup(messages, t, mt, tp, to, **kw):
        _calls17b.append((messages[0]["content"], kw.get("image_bytes"),
                          kw.get("extra_image_bytes")))
        return "ответ-2" if len(_calls17b) == 2 else "ответ-1"

    q17._get_response_locked = _locked_followup
    _r17c = q17.get_response_with_image("что тут?", b"img", extra_image=b"extra")
    check("overflow followup: второй кадр отдельным сообщением, финал уточнённый",
          _r17c == "ответ-2" and len(_calls17b) == 2
          and _calls17b[0][2] is None and _calls17b[1][1] == b"extra")

    # ── 18. lock_timeout: пользовательский путь не ждёт занятый инстанс ──
    c18 = wl.WebChatLLM("google", base_dir=tmp / "c18", channel="cc")
    c18._lock.acquire()  # занят долгой фоновой задачей (генерация банка)
    _t0 = wl.time.time()
    _r18 = c18.get_response([{"role": "user", "content": "x"}],
                            lock_timeout=0.2)
    _dt18 = wl.time.time() - _t0
    check("lock_timeout: занятый лок — быстрый None (фолбэк вызывающего)",
          _r18 is None and _dt18 < 5)
    c18._lock.release()
    c18._get_response_locked = lambda *a, **kw: "ответ"
    check("lock_timeout: свободный лок — обычный ответ",
          c18.get_response([{"role": "user", "content": "x"}],
                           lock_timeout=0.2) == "ответ")
    c18b = wl.WebChatLLM("google", base_dir=tmp / "c18b", channel="main")
    c18b._get_response_locked = lambda *a, **kw: "ок"
    check("без lock_timeout — семантика прежняя (блокирующий лок)",
          c18b.get_response([{"role": "user", "content": "x"}]) == "ок")

    # ── 19. Сигнал сайта (rate limit/картинки/ошибка) берётся только из
    #       баннера/error_scope сайта, НЕ из текста ответа модели ──
    _cnt19, _lbt19, _ev19 = ba.count_blocks, ba.last_block_text, ba.eval_js
    try:
        # 19a. Длинный реалистичный ответ, где МОДЕЛЬ САМА обсуждает лимиты
        # сообщений/картинок чужих сервисов (пользователь спросил про
        # лимиты): фразы вроде «message limit»/«usage limit»/«too many
        # images» в такой прозе не должны резать готовый ответ и отправлять
        # ЭТОТ сайт в карантин — _RATE_LIMIT_RES/_TOO_MANY_IMAGES_RES не
        # сверяются с cur_norm (текстом самого ответа).
        long_answer = (
            "Лимиты бесплатных тарифов у чат-сервисов разные: у одних "
            "message limit считается числом сообщений в сутки, у других — "
            "usage limit по токенам. При загрузке нескольких фотографий "
            "сайт может показать «too many images» или предупредить про "
            "лимит сообщений — это тема моего ответа, а не отказ этого "
            "чата прямо сейчас. Free-тарифы обычно ограничивают и число "
            "картинок за раз, и суточный лимит запросов — уточняйте в "
            "настройках аккаунта конкретного сервиса, который вас "
            "интересует, сроки сброса там же."
        )
        check("19a: тестовый ответ действительно содержит слова из "
              "паттернов rate-limit/too-many-images (тест не тривиален)",
              len(long_answer) > wl._SITE_SIGNAL_MAX_LEN
              and any(rx.search(long_answer) for rx in wl._RATE_LIMIT_RES)
              and any(rx.search(long_answer) for rx in wl._TOO_MANY_IMAGES_RES))
        ba.eval_js = lambda *a, **kw: ""       # ни баннера, ни error_scope
        ba.count_blocks = lambda *a, **kw: 1   # новый блок (baseline=0)
        ba.last_block_text = lambda host, tid, sels=None, **kw: long_answer
        llm19a = wl.WebChatLLM("claude", base_dir=tmp / "rl19a")
        res19a = llm19a._wait_answer("claude.ai", 1, timeout=5, had_image=True)
        check("19a: проза про лимиты чужих сервисов — не rate-limit, не "
              "картиночный отказ, ответ доходит целиком, карантина нет",
              res19a == long_answer and not wl.site_quarantined("claude"))

        # 19b. Настоящий баннер сайта (сканер страницы, НЕ текст ответа —
        # сайт ничего не ответил, cur_norm пуст) — карантин ДЕЙСТВИТЕЛЬНО
        # ставится, TTL парсится из текста баннера. Заодно проверяет частоту
        # пробника: banner_js сканит раз в BANNER_PROBE_EVERY тиков, а порогу
        # нужны 2 замера ПОДРЯД — без форсирования непрерывного пробника
        # после первого совпадения (rl_seen/im_seen в условии пробника) два
        # подряд замера никогда бы не набрались.
        banner_real = "You've reached your message limit. Try again in 2 hours."
        ba.eval_js = lambda *a, **kw: banner_real
        ba.count_blocks = lambda *a, **kw: 0   # сайт вообще не ответил
        ba.last_block_text = lambda *a, **kw: ""
        llm19b = wl.WebChatLLM("claude", base_dir=tmp / "rl19b")
        raised19b = None
        try:
            llm19b._wait_answer("claude.ai", 1, timeout=5)
        except wl._ChatRateLimited as e:
            raised19b = e
        check("19b: настоящий баннер лимита сообщений — рейзит "
              "_ChatRateLimited с распарсенным TTL (2 часа)",
              raised19b is not None and raised19b.ttl is not None
              and abs(raised19b.ttl - 7200) < 5)

        # 19c. Сайты БЕЗ error_scope, у которых баннер рендерится ВНУТРИ
        # answer-блока (deepseek «Length limit reached…» — без content-
        # классов): короткий блок, целиком похожий на баннер, всё равно
        # детектится (allow_answer_text), в отличие от 19a, где похожий
        # текст тонет в длинном настоящем ответе.
        short_refusal = "Length limit reached. Please start a new chat."
        ba.eval_js = lambda *a, **kw: ""
        ba.count_blocks = lambda *a, **kw: 1
        ba.last_block_text = lambda host, tid, sels=None, **kw: short_refusal
        llm19c = wl.WebChatLLM("deepseek", base_dir=tmp / "rl19c")
        raised19c = None
        try:
            llm19c._wait_answer("chat.deepseek.com", 1, timeout=5)
        except wl._ChatBroken as e:
            raised19c = e
        check("19c: короткий баннер сайта внутри answer-блока (deepseek) — "
              "всё ещё детектится (структурно короткий, не длинный ответ)",
              raised19c is not None and "length limit" in str(raised19c).lower())
    finally:
        ba.count_blocks, ba.last_block_text, ba.eval_js = _cnt19, _lbt19, _ev19

    # 19d. web_llm_state.json — общий на контекст файл: два РАЗНЫХ инстанса
    # WebChatLLM (например, бот и фоновая память со своими ModelRouter, см.
    # комментарий у _STATE_FILE_LOCKS) конкурентно пишут РАЗНЫЕ каналы одного
    # сайта — read-modify-write под разными per-instance локами не должен
    # терять ключ (chat_url/счётчик квоты) того канала, что сохранился не
    # последним.
    base19d = tmp / "state19d"
    a19d = wl.WebChatLLM("qwen", base_dir=base19d, channel="main")
    b19d = wl.WebChatLLM("qwen", base_dir=base19d, channel="side")
    barrier19d = threading.Barrier(2)

    def _writer19d(inst, url):
        barrier19d.wait()
        for _ in range(25):
            inst._save_state({"chat_url": url})
            inst._quota_bump()

    t1 = threading.Thread(target=_writer19d, args=(a19d, "https://x/main"))
    t2 = threading.Thread(target=_writer19d, args=(b19d, "https://x/side"))
    t1.start(); t2.start(); t1.join(); t2.join()
    st19d = json.loads((base19d / "web_llm_state.json").read_text(encoding="utf-8"))
    sites19d = st19d.get("sites", {})
    check("19d: конкурентная запись двух каналов одного сайта не теряет "
          "chat_url ни одного из них",
          sites19d.get("qwen", {}).get("chat_url") == "https://x/main"
          and sites19d.get("qwen#side", {}).get("chat_url") == "https://x/side")
    check("19d: конкурентный инкремент счётчика квоты не теряет апдейты "
          "(25+25, а не меньше из-за гонки read-modify-write)",
          sites19d.get("qwen", {}).get("count") == 25
          and sites19d.get("qwen#side", {}).get("count") == 25)

    # ── 19e. Межпроцессный file_lock ──
    # Два НЕЗАВИСИМЫХ ПРОЦЕССА (multiprocessing, не потока — threading.Lock
    # процесса A не виден процессу B) параллельно инкрементируют квоту ОДНОГО
    # и того же сайта+канала в общем web_llm_state.json. Без file_lock
    # (atomic_io, fcntl/msvcrt) часть из 2×N инкрементов терялась бы —
    # та же гонка read-modify-write, что и в 19d, но между процессами, где
    # _STATE_FILE_LOCKS (threading, только для своего процесса) не спасает.
    base19e = tmp / "state19e"
    n19e = 30
    p1 = multiprocessing.Process(target=_mp_quota_worker,
                                 args=(str(base19e), "qwen", "main", n19e))
    p2 = multiprocessing.Process(target=_mp_quota_worker,
                                 args=(str(base19e), "qwen", "main", n19e))
    p1.start(); p2.start()
    p1.join(60); p2.join(60)
    st19e = json.loads((base19e / "web_llm_state.json").read_text(encoding="utf-8"))
    count19e = st19e.get("sites", {}).get("qwen", {}).get("count")
    check("19e: file_lock — два ПРОЦЕССА инкрементируют квоту одного канала "
          "без потерь (2×30, а не меньше)",
          p1.exitcode == 0 and p2.exitcode == 0 and count19e == 2 * n19e)

    # ── 19f. Время сайта → timeutil (TIMEZONE), не системный пояс ──
    # _parse_reset_ttl («resets at HH:MM») считает через timeutil.now()/
    # to_ts(): часы САЙТА (открытого в браузере пользователя) — это часы
    # ПОЛЬЗОВАТЕЛЯ из TIMEZONE, а не системный пояс машины бота. Проверяем на
    # двух РАЗНЫХ поясах: TTL совпадает с прямым расчётом через zoneinfo для
    # КАЖДОГО из них — то есть функция действительно читает TIMEZONE, а не
    # игнорирует его.
    _orig_timezone_env = os.environ.get("TIMEZONE")
    try:
        # Строка «resets at HH:MM» несёт только час:минуту (без секунд) —
        # «ожидаемое» TTL тоже считаем от цели с обнулёнными секундами,
        # иначе секунды/микросекунды момента запуска теста сами дают
        # расхождение с функцией (она их из HH:MM получить не может).
        os.environ["TIMEZONE"] = "UTC"
        now_utc = datetime.now(ZoneInfo("UTC"))
        target_utc = (now_utc + timedelta(minutes=12)).replace(
            second=0, microsecond=0)
        ttl_utc = wl._parse_reset_ttl(
            f"resets at {target_utc.hour:02d}:{target_utc.minute:02d}")
        expected_utc = (target_utc - now_utc).total_seconds()
        check("19f: TIMEZONE=UTC — TTL «resets at HH:MM» совпадает с "
              "прямым расчётом по zoneinfo (±5с)",
              ttl_utc is not None and abs(ttl_utc - expected_utc) < 5)

        os.environ["TIMEZONE"] = "Etc/GMT-3"  # UTC+3 (знак у Etc/GMT инвертирован)
        now_msk = datetime.now(ZoneInfo("Etc/GMT-3"))
        target_msk = (now_msk + timedelta(minutes=12)).replace(
            second=0, microsecond=0)
        ttl_msk = wl._parse_reset_ttl(
            f"resets at {target_msk.hour:02d}:{target_msk.minute:02d}")
        expected_msk = (target_msk - now_msk).total_seconds()
        check("19f: TIMEZONE=Etc/GMT-3 — тот же разбор пересчитан по ДРУГОМУ "
              "поясу (±5с), не системным временем машины",
              ttl_msk is not None and abs(ttl_msk - expected_msk) < 5)
    finally:
        if _orig_timezone_env is None:
            os.environ.pop("TIMEZONE", None)
        else:
            os.environ["TIMEZONE"] = _orig_timezone_env

    ba.answer_blocks_after = _aba_orig
    ba.restart_browser = _rb_orig
    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


def _with_temp_browser_locks(fn):
    """Лок-файлы Chrome пулов (<профиль>.bot-lifecycle.lock/.bot-users.lock)
    и общий срок rescue (<профиль>.bot-rescue) — во временный каталог на весь
    прогон: тест ходит в сырой пул V (и воркер) с конфигом по умолчанию и
    иначе создавал бы/держал лок-файлы рядом с НАСТОЯЩИМИ профилями — теми
    же, что у живого бота, а снятие карантина (_challenge_check) удаляло бы
    файл rescue живого бота, завершая его rescue."""
    import app.features.browser_actions as _ba_locks
    d = tempfile.mkdtemp(prefix="browser_locks_")
    saved = (_ba_locks._pool_h_life_path, _ba_locks._pool_v_life_path,
             _ba_locks._pool_h_rescue_path)
    _ba_locks._pool_h_life_path = lambda: os.path.join(
        d, "h" + _ba_locks._LIFE_SUFFIX)
    _ba_locks._pool_v_life_path = lambda: os.path.join(
        d, "v" + _ba_locks._LIFE_SUFFIX)
    _ba_locks._pool_h_rescue_path = lambda: os.path.join(
        d, "h" + _ba_locks._RESCUE_SUFFIX)
    try:
        return fn()
    finally:
        (_ba_locks._pool_h_life_path, _ba_locks._pool_v_life_path,
         _ba_locks._pool_h_rescue_path) = saved
        for pool in ("h", "v"):
            _ba_locks._pool_user_release(pool)


if __name__ == "__main__":
    sys.exit(_with_temp_browser_locks(main))

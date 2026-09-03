"""Smoke-тесты web_llm (веб-чат как LLM-провайдер): склейка промпта,
квота/пейсинг (per-site состояние), постоянный чат (один URL на сайт,
ожидание НОВОГО блока ответа), восстановление при сломанном чате,
extract_json, интеграция webchat-токенов в ModelRouter.
Браузерные вызовы (browser_actions) — моки.
Запуск: python -m scripts.test_web_llm"""

import json
import sys
import tempfile
from pathlib import Path


def main():
    tmp = Path(tempfile.mkdtemp(prefix="webllm_data_"))
    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

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
          joined.startswith("Инструкции") and "Ты — Коннор." in joined
          and "Пользователь: привет" in joined
          and "Ассистент: здорово" in joined
          and joined.endswith("Пользователь: как дела?")
          and "image" not in joined)

    # ── 2. extract_json ──
    check("extract_json: чистый/ограждённый/в прозе/массив/мусор",
          wl.extract_json('{"a": 1}') == {"a": 1}
          and wl.extract_json('Вот:\n```json\n{"a": 2}\n```') == {"a": 2}
          and wl.extract_json('ответ: [1, 2]!') == [1, 2]
          and wl.extract_json("никакого json") is None
          and wl.extract_json("") is None)

    # Пейсинг/опрос в тестах глушим глобально
    wl.MIN_INTERVAL_SEC = 0
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

    # ── 3. Состояние per-site: квота (окно per_hour × QUOTA_WINDOW_HOURS),
    #       изоляция сайтов, миграция формата ──
    today = wl.time.strftime("%Y-%m-%d")
    llm = wl.WebChatLLM("qwen", base_dir=tmp / "q1")
    (tmp / "q1" / "web_llm_state.json").write_text(json.dumps(
        {"sites": {"qwen": {"window_start": wl.time.time(),
                            "count": wl.QUOTA_PER_HOUR * wl.QUOTA_WINDOW_HOURS,
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
    # Снятый лимит (None): счётчик не мешает, остаётся только пейсинг
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
        check("happy: текст ответа читается с сохранением markdown-разметки",
              bool(md_flags) and all(f is True for f in md_flags))
    finally:
        ba.open_new_tab, ba.navigate_tab = _open, _nav
        ba.chat_fill_send, ba.last_block_text = _send, _read
        ba.count_blocks, ba.tab_url = _cnt, _url

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
        check("stuck: сообщение не появилось в ленте → перезапуск браузера",
              len(restarts) == 1
              and "не появилось в ленте" in restarts[0])
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
            return "Пользователь: hi"
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
        check("send fail: BrowserUnavailable → None",
              llm4.get_response([{"role": "user", "content": "hi"}],
                                timeout=0.01) is None)
        check("stuck: поле не очистилось → перезапуск браузера, вкладка забыта",
              len(restarts) == 2
              and "поле чата не приняло ввод" in restarts[1]
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
    cnts = iter([0, 1, 2, 2, 2])  # baseline попыток: 0 (битая), 1 (свежая с
    txts = iter(["ок"] * 5)  # baseline_text попыток ×2 + 3 замера; прошлым
    # блоком в DOM) → ждём блок > 1
    ba.count_blocks = lambda *a, **kw: next(cnts)
    def _lbt_rec(host, tid, sels=None, **kw):
        if sels and list(sels) == (llm5.adapter.get("user") or []):
            return "Пользователь: hi"
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
            return "Пользователь: hi"
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

    # ── 6d. Кейс 22.08: баннер ошибки рендерится в assistant-контейнере БЕЗ
    #       content-классов — answer-селекторы его не видят (last_block_text
    #       всегда отдаёт старый ответ), ловим только через error_scope ──
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
            return "Пользователь: hi"
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

    # ── 6e. Hydration-гонка (кейс 22.08): closed-loop «поле очистилось»
    #       сработал ложно, сообщение не попало в ленту — _send_verified
    #       обязан заметить по user-блоку и повторить отправку ──
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
            return "Пользователь: hi" if flow8["delivered"] else "старый чужой"
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
        check("stuck: двойной промах ленты (кейс 26.08) → перезапуск браузера",
              len(restarts) == 3
              and "не появилось в ленте" in restarts[2])
    finally:
        wl.SEND_VERIFY_SEC, wl.POLL_SEC = _sv, _pl
        ba.open_new_tab = _open8
        ba.chat_fill_send, ba.last_block_text = _cfs8, _lbt8
        ba.count_blocks, ba.tab_url, ba.eval_js = _cnt8, _url8, _ev8

    # ── 6f. Кейс 22.08: «реформулировка вместо ответа». Страница чата
    #       непрогрета: baseline=0, хотя в ленте уже лежит СТАРЫЙ завершённый
    #       ответ (реплика coref) — baseline-путь вернул бы её. Якорный путь
    #       ждёт блок ПОСЛЕ нашего сообщения ──
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
            return "Пользователь: привет"   # подтверждение отправки
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
            return "Пользователь: привет"
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
        r.model_overrides = {}
        r.webchat_sites = list(sites)
        r._webchats = {}
        r.webchat_limits = {}
        r._last_key_index = {}
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
    llm_d = wl.WebChatLLM("deepseek", base_dir=tmp / "vd")
    check("vision-webchat: адаптер без images → None до всякого браузера",
          llm_d.get_response_with_image("что на картинке?", b"png") is None)

    # 14b. Happy path: paste картинки → обычная отправка/ожидание ответа
    llm_v = wl.WebChatLLM("qwen", base_dir=tmp / "vq")
    vcalls = {"open": [], "send": [], "paste": []}
    _open, _nav = ba.open_new_tab, ba.navigate_tab
    _send, _read = ba.chat_fill_send, ba.last_block_text
    _cnt, _url, _paste = ba.count_blocks, ba.tab_url, ba.chat_paste_image
    ba.open_new_tab = lambda url, **kw: (vcalls["open"].append(url), 42)[1]
    ba.navigate_tab = lambda url, tab_id=None: None
    ba.tab_url = lambda *a, **kw: "https://chat.qwen.ai/c/vision-1"
    ba.chat_fill_send = lambda host, tab_id, sel, text: (
        vcalls["send"].append(text), "sent")[1]
    ba.chat_paste_image = lambda host, tab_id, sel, img, mime="image/png": (
        vcalls["paste"].append((sel, bytes(img), mime)), True)[1]
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
        # 14c. Paste не подтвердился сайтом → None, текст не шлём, квоту не тратим
        ba.chat_paste_image = lambda *a, **kw: False
        counts = iter([0])
        texts = iter([""])
        res_v2 = llm_v.get_response_with_image("ещё раз", b"\x89PNG...")
        check("vision-webchat: paste не сработал → None, текст не отправлен",
              res_v2 is None and len(vcalls["send"]) == 1)
    finally:
        ba.open_new_tab, ba.navigate_tab = _open, _nav
        ba.chat_fill_send, ba.last_block_text = _send, _read
        ba.count_blocks, ba.tab_url, ba.chat_paste_image = _cnt, _url, _paste

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
    # 14f. Ни у кого нет images — None
    rv3 = _stub_router(["deepseek"])
    rv3._vision_verdict = {}
    check("vision-router: веб-чаты без images — честный None",
          rv3.get_response_with_image("номер?", b"img") is None)
    # 14g. supports_vision: без облаков, но с картиночным веб-чатом — True
    rv4 = _stub_router(["qwen"])
    rv4._vision_verdict = {}
    rv5 = _stub_router(["deepseek"])
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

    ba.answer_blocks_after = _aba_orig
    ba.restart_browser = _rb_orig
    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

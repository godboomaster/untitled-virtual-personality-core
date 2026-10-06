"""Тест трафика веб-чатов: темп отправок и повторяемые блоки промпта.

Проверяет:
* pace_reserve — автоматический канал ждёт паузу от прошлой отправки на сайт
  (любого канала), часовой потолок автоматики → None, ответы человеку не
  ждут и в потолок не упираются, сайты независимы, битый файл темпа не
  блокирует отправку;
* повторяемые блоки (app/core/sticky_scope) в WebChatLLM: блок, недавно
  ушедший в тред целиком, уходит ссылкой; окно в сообщениях (чужие вызовы
  канала тоже считаются), навигация/новая вкладка — снова целиком, срок
  STICKY_MAX_AGE_SEC, поисковик (stateless) — всегда целиком;
* жёсткий лимит поля (duck.ai, 16 тыс.): длинные правила уходят отдельным
  сообщением перед шагом, шаг — со ссылкой; не влезает и так — пропуск без
  отправки;
* агент задач: ранние вопросы и шаги истории короче, старая выдача поиска —
  одной строкой, блоки шага — точные куски промпта, счёт вызовов и итог в
  лог.

Браузер не нужен: вкладка, отправка и ответ подменены на инстансе.

Запуск: PYTHONPATH=. python3 scripts/test_webchat_traffic.py
"""

import logging
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    tmp = Path(tempfile.mkdtemp(prefix="webchat_traffic_"))
    from app.features import web_llm as wl
    from app.core.sticky_scope import StickyBlock, sticky_blocks
    wl._pace_path = lambda: tmp / "pacing.json"
    env_keys = ("WEBCHAT_AUTO_GAP_SEC", "WEBCHAT_AUTO_JITTER_SEC",
                "WEBCHAT_AUTO_PER_HOUR")
    env_old = {k: os.environ.get(k) for k in env_keys}

    def pacing(gap, jitter, cap):
        os.environ["WEBCHAT_AUTO_GAP_SEC"] = str(gap)
        os.environ["WEBCHAT_AUTO_JITTER_SEC"] = str(jitter)
        os.environ["WEBCHAT_AUTO_PER_HOUR"] = str(cap)

    # ── 1. Темп отправок ──
    print("темп отправок:")
    from app.core.pace_scope import automated_calls

    def auto(site, channel="cc"):
        with automated_calls():
            return wl.pace_reserve(site, channel)

    pacing(10, 0, 3)
    check("ответ человеку (main) — без ожидания",
          wl.pace_reserve("deepseek", "main") == 0.0)
    w1 = auto("deepseek")
    check(f"шаг агента сразу после main — пауза ~10 с ({w1:.1f})",
          9.5 <= w1 <= 10.5)
    w2 = wl.pace_reserve("deepseek", "side")
    check(f"фон (side) — автоматика и без области, после слота прошлого "
          f"(~20 с, {w2:.1f})", 19.5 <= w2 <= 20.5)
    w3 = auto("deepseek")
    check(f"третий — ~30 с ({w3:.1f})", 29.5 <= w3 <= 30.5)
    check("четвёртый за час — потолок 3 → None",
          auto("deepseek") is None)
    check("команда человека (cc вне цикла агента) — без ожидания и потолка",
          wl.pace_reserve("deepseek", "cc") == 0.0)
    check("ответ человеку при исчерпанном потолке — проходит без ожидания",
          wl.pace_reserve("deepseek", "main") == 0.0)
    check("другой сайт — свой счёт", auto("qwen") == 0.0)
    pacing(0, 5, 0)
    waits = [auto("kimi") for _ in range(6)]
    check("разброс: пауза в пределах jitter от прошлого слота",
          all(w is not None and w >= 0 for w in waits)
          and max(waits) <= 5 * 6 + 0.5)
    pacing(0, 0, 0)
    check("потолок 0 — без потолка, пауза 0 — без ожидания",
          all(auto("zai") == 0.0 for _ in range(100)))
    (tmp / "pacing.json").write_text("{битый json")
    check("битый файл темпа — отправка не блокируется",
          auto("deepseek") == 0.0)
    os.environ["WEBCHAT_AUTO_GAP_SEC"] = "abc"
    check("нечисловая настройка — значение по умолчанию, без падения",
          auto("claude") == 0.0)
    pacing(0, 0, 0)

    # ── 2. Повторяемые блоки в постоянном треде ──
    print("повторяемые блоки:")
    from app.features import browser_actions as ba
    _cnt, _lbt = ba.count_blocks, ba.last_block_text
    ba.count_blocks = lambda *a, **kw: 0
    ba.last_block_text = lambda *a, **kw: ""

    class FakeChat(wl.WebChatLLM):
        """Вкладка, отправка и ответ — в памяти. nav_next — следующий
        _ensure_chat «навигирует» (новый тред)."""

        def __init__(self, site, channel="cc"):
            super().__init__(site, base_dir=tmp / site, channel=channel)
            self.sent = []
            self.nav_next = True

        def _ensure_chat(self, fresh=False):
            if fresh or self.nav_next:
                self._new_thread()
                self.nav_next = False
            self._tab_id = 1
            return 1

        def _send_verified(self, ba_, host, tab_id, prompt,
                           wait_upload=False):
            self.sent.append(prompt)
            return None

        def _wait_answer(self, *a, **kw):
            return "OK" if "Standing instructions" in self.sent[-1][:40] \
                else '{"action":"scroll"}'

        def _capture_chat_url(self, *a, **kw):
            pass

        def _quarantine_skip(self, ba_=None):
            return False

        def _snap_for(self, ba_, tab_id):
            return None

    rules = "RULES:" + "r" * 3000
    page = "PAGE:" + "p" * 2000
    rb = StickyBlock(rules, "[rules as before]", window=2, primable=True)
    pb = StickyBlock(page, "[page as before]", window=1)

    def step(chat, extra="step", blocks=(rb, pb)):
        with sticky_blocks(*blocks):
            return chat.get_response([{"role": "user", "content":
                                       f"{extra}\n{page}\n{rules}"}])

    c = FakeChat("deepseek")
    step(c)
    check("1-е сообщение — правила и страница целиком",
          rules in c.sent[-1] and page in c.sent[-1])
    step(c)
    check("2-е — оба блока ссылкой",
          "[rules as before]" in c.sent[-1] and "[page as before]" in c.sent[-1]
          and rules not in c.sent[-1] and page not in c.sent[-1])
    step(c)
    check("3-е — страница вне окна (1) целиком, правила (окно 2) ссылкой",
          page in c.sent[-1] and "[rules as before]" in c.sent[-1])
    step(c)
    check("4-е — правила вне окна (2) целиком снова",
          rules in c.sent[-1] and "[page as before]" in c.sent[-1])
    c.get_response([{"role": "user", "content": "unrelated call"}])
    c.get_response([{"role": "user", "content": "another call"}])
    step(c)
    check("чужие вызовы канала тоже считаются в окно — правила целиком",
          rules in c.sent[-1])
    c.nav_next = True
    step(c)
    check("навигация (новый тред) — оба блока целиком",
          rules in c.sent[-1] and page in c.sent[-1])
    step(c, extra="other step",
         blocks=(rb, StickyBlock("PAGE:other", "[page as before]", 1)))
    check("блока нет в промпте — не мешает, правила ссылкой",
          "[rules as before]" in c.sent[-1])
    old_age = wl.STICKY_MAX_AGE_SEC
    wl.STICKY_MAX_AGE_SEC = 0.0
    step(c)
    wl.STICKY_MAX_AGE_SEC = old_age
    check("старше STICKY_MAX_AGE_SEC — целиком",
          rules in c.sent[-1] and page in c.sent[-1])
    n0 = len(c.sent)
    c.get_response([{"role": "user", "content": f"x\n{rules}"}])
    check("без области sticky — промпт как есть",
          c.sent[-1] == f"User: x\n{rules}" or rules in c.sent[-1])
    check("ровно одна отправка на вызов", len(c.sent) == n0 + 1)

    g = FakeChat("google", channel="cc")
    check("поисковик на канале cc — stateless", g.stateless)
    step(g)
    step(g)
    check("поисковик (свежий тред на вызов) — всегда целиком",
          all(rules in s and page in s for s in g.sent))

    # ── 3. Жёсткий лимит поля (duck.ai) ──
    print("жёсткий лимит поля:")
    big_rules = "RULES:" + "r" * 7000
    big_page = "PAGE:" + "p" * 10000
    brb = StickyBlock(big_rules, "[rules as before]", window=5,
                      primable=True)
    bpb = StickyBlock(big_page, "[page as before]", window=1)
    d = FakeChat("duckai")
    cap = wl.ADAPTERS["duckai"]["max_input"]
    with sticky_blocks(brb, bpb):
        r = d.get_response([{"role": "user",
                             "content": f"s\n{big_page}\n{big_rules}"}])
    check("полный шаг > лимита: сначала правила отдельным сообщением",
          len(d.sent) == 2 and "Standing instructions" in d.sent[0][:40]
          and big_rules in d.sent[0])
    check("затем шаг со ссылкой на правила, в лимите",
          "[rules as before]" in d.sent[1] and big_page in d.sent[1]
          and len(d.sent[1]) <= cap)
    check("ответ — на шаг, не «OK» праймера", r == '{"action":"scroll"}')
    with sticky_blocks(brb, bpb):
        d.get_response([{"role": "user",
                         "content": f"s2\n{big_page}\n{big_rules}"}])
    check("следующий шаг — без праймера", len(d.sent) == 3
          and "Standing instructions" not in d.sent[2])
    huge = StickyBlock("RULES:" + "r" * 9000, "[r]", window=5, primable=True)
    n0 = len(d.sent)
    with sticky_blocks(huge):
        r = d.get_response([{"role": "user", "content":
                             "q" * 17000 + "RULES:" + "r" * 9000}])
    check("не влезает даже со ссылкой — пропуск без отправки",
          r is None and len(d.sent) == n0)
    ba.count_blocks, ba.last_block_text = _cnt, _lbt

    # ── 4. Агент задач: промпт и счёт ──
    print("агент задач:")
    from app.features import task_agent as ta
    from app.core.sticky_scope import current_sticky
    from app.core.pace_scope import is_automated

    class CC:
        base_dir = tmp / "cc"
        site_search = "google"

    agent = ta.TaskAgent(CC())
    qs = [(f"Вопрос {i}? Варианты:\n- Альфа {i}\n- Бета {i}\n- Гамма {i}",
           f"бета {i}") for i in range(10)]
    lines = ta._qa_lines(qs)
    text = "\n".join(lines)
    check("10 пар → первые 2 только числом",
          lines[0].startswith("- (2 earlier questions"))
    check("последние 3 — со списком вариантов",
          text.count("options you offered") == 3)
    check("ранние — выбранный вариант словами",
          "→ the user picked: Бета 2" in text and "Альфа 2" not in text)
    check("приватная пара — заглушкой",
          ta._PRIVATE_QUESTION.split()[0] in "\n".join(
              ta._qa_lines(qs[:2], {0})))
    long_note = 'click "A | B" → ok | after it: ' + "x" * 500
    short = ta._hist_short(long_note)
    check("ранний шаг: действие и итог целы, заметка обрезана",
          short.startswith('click "A | B" → ok | after it: ')
          and short.endswith("…")
          and len(short) <= len('click "A | B" → ok | ') + ta.HIST_NOTE_SHORT
          + 1)
    check("короткая заметка не трогается",
          ta._hist_short('click "x" → ok | after it: y') ==
          'click "x" → ok | after it: y')
    check("строка без заметки не трогается",
          ta._hist_short('click "a | b" → ok') == 'click "a | b" → ok')

    items = [{"idx": i, "tag": "button", "role": "button",
              "text": f"Товар {i}", "vp": True} for i in range(10)]
    run = {"goal": "закажи пиццу", "lang": "ru", "qa": qs, "steps": 20,
           "history": [f'click "Товар {i}" → ok | after it: '
                       + "z" * 400 for i in range(12)],
           "search": {"query": "пицца", "results": [
               {"title": "Пиццерия", "url": "https://pizza.test/",
                "snippet": "доставка"}]},
           "search_at": 2, "chat_id": "1"}
    obs = {"url": "https://pizza.test/", "host": "pizza.test", "tab_id": 1,
           "shown": items, "note": None, "text": None, "error": None,
           "search": run["search"]}
    p = agent._prompt(run, obs)
    check("выдача поиска 18 шагов назад — одной строкой",
          "Earlier web search \"пицца\"" in p and "pizza.test/ " not in p
          and "Пиццерия" not in p)
    run["search_at"] = 15
    p = agent._prompt(run, obs)
    check("свежая выдача — списком", "Пиццерия" in p)
    check("в истории полных заметок — 4 последних",
          p.count("z" * 400) == ta.HISTORY_FULL)
    blocks = agent._sticky_for(run)
    check("блоки шага — точные куски промпта (правила и страница)",
          len(blocks) == 2 and all(b.text in p for b in blocks)
          and blocks[0].primable and "Товар 3" in blocks[1].text)
    check("правила — общий блок для всех шагов",
          blocks[0].text == ta._STEP_RULES)

    seen_sticky = []

    class FakeLLM:
        cc_provider = "webchat:duckai"
        _last_provider = "webchat:duckai"

        def get_response(self, messages, **kw):
            seen_sticky.append((current_sticky(), kw.get("webchat_channel"),
                                kw.get("force_provider"), is_automated()))
            return '{"action":"scroll"}'

    r2 = {"goal": "g", "lang": "ru"}
    agent._ask_llm(r2, "step", FakeLLM(), "abc", sticky=blocks,
                   temperature=0.0)
    agent._ask_llm(r2, "brief", FakeLLM(), "de")
    check("вызов модели: блоки в области, канал cc, провайдер режима, "
          "автоматика",
          seen_sticky[0][0] == tuple(blocks)
          and seen_sticky[0][1:] == ("cc", "webchat:duckai", True)
          and not is_automated()
          and seen_sticky[1][0] == ())
    check("счёт вызовов и символов по видам",
          r2["traffic"] == {"step": [1, 3], "brief": [1, 2]}
          and r2["traffic_by"] == {"webchat:duckai": 2})
    records = []

    class H(logging.Handler):
        def emit(self, rec):
            records.append(rec.getMessage())

    h = H()
    logging.getLogger("app.features.task_agent").addHandler(h)
    logging.getLogger("app.features.task_agent").setLevel(logging.INFO)
    agent._remember("1", dict(r2, qa=[], sites=[]), "done")
    check("итог трафика — в лог по концу прогона",
          any("трафик задачи: 2 вызовов" in m for m in records))

    for k, v in env_old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    print(f"\n{ok - failures}/{ok} OK")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    t0 = time.time()
    main()

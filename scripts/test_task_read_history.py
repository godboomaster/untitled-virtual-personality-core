"""Тест честной строки истории после find/read агента задач (task_agent:
_hist_gone, obs_extra hist). Ошибку нашёл четвёртый прогон симуляции (run4,
21.10, req-0040): после вопроса человеку в истории шагов осталось «read the
page → ok — the text is shown below», а текста страницы в промпте уже не
было — модель брала корзину из раннего сообщения треда.

Проверяет:
* шаг сразу после read: текст страницы в промпте, строка — «shown below»;
* шаг после вопроса человеку: текста нет, строка — «its text was shown at
  the next step, not repeated now», «shown below» в промпте нет;
* два чтения подряд с тем же текстом: старое — «was shown», свежее —
  «the SAME text … (shown below)»; после — без «(shown below)»;
* find: «listed below» — пока найденное в промпте, потом — «were listed
  at the next step».

Браузер — FakeCC (test_task_agent), модель — ScriptedRouter: живых
браузеров, веб-чатов и LLM нет.

Запуск: PYTHONPATH=. python3 scripts/test_task_read_history.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.test_task_agent import FakeCC, ScriptedRouter  # noqa: E402

FAILS = 0
OK = 0


def check(name, cond, detail=""):
    global FAILS, OK
    OK += 1
    if cond:
        print(f"  [OK] {name}")
    else:
        FAILS += 1
        print(f"  [FAIL] {name} {detail}")


def hist_part(prompt: str) -> str:
    # Блок «Steps done so far» промпта шага
    i = prompt.find("Steps done so far")
    j = prompt.find("\n\n", i)
    return prompt[i:j] if i >= 0 else ""


def main():
    from app.features import browser_actions as _ba
    from app.features.task_agent import TaskAgent

    _ba.wait_dom_idle = lambda *a, **k: None
    _ba.section_items = lambda *a, **k: None
    _ba.section_names = lambda *a, **k: None
    tmp = Path(tempfile.mkdtemp(prefix="read_history_"))
    FakeCC.base_dir = tmp
    page_text = "Меню: Пепперони 408 ₽, Маргарита 399 ₽"

    class ReadCC(FakeCC):
        def execute(self, act, chat_id="", router=None):
            if act["kind"] == "read":
                self.executed.append(dict(act))
                return True, page_text
            return super().execute(act, chat_id, router)

    print("read и вопрос человеку:")
    cc = ReadCC()
    cc.url = "https://pizza.test/menu"
    rt = ScriptedRouter([{"action": "read"},
                         {"action": "ask", "question": "Какую пиццу взять?"},
                         {"action": "fail", "message": "стоп"}])
    ag = TaskAgent(cc, memory_path=tmp / "r1.json")
    q = ag.start("r1", "найди пиццу подешевле на pizza.test", rt,
                 user_id="A")
    check("модель спросила человека после чтения",
          "Какую пиццу взять?" in str(q), q)
    p1 = rt.prompts[1] if len(rt.prompts) > 1 else ""
    check("шаг сразу после read: текст в промпте, строка — «shown below»",
          "Page text:" in p1 and page_text in p1
          and "read the page → ok — the text is shown below" in hist_part(p1),
          hist_part(p1))
    ag.feed("r1", "маргариту", rt, user_id="A")
    p2 = rt.prompts[2] if len(rt.prompts) > 2 else ""
    h2 = hist_part(p2)
    check("после ответа человека: текста нет, строка — «was shown at the "
          "next step, not repeated now»",
          "Page text:" not in p2 and page_text not in p2
          and "read the page → ok — its text was shown at the next step, not "
          "repeated now" in h2 and "shown below" not in p2, h2)
    run = ag.__dict__.get("_finished", {}).get("r1", {}).get("run") \
        or ag._runs.get("r1") or {}
    check("сама история прогона не переписана (строка — в промпте)",
          any(h.endswith("the text is shown below")
              for h in run.get("history") or ()), run.get("history"))

    print("тот же текст два раза:")
    cc = ReadCC()
    cc.url = "https://pizza.test/menu"
    rt = ScriptedRouter([{"action": "read"}, {"action": "read"},
                         {"action": "ask", "question": "Какую пиццу взять?"},
                         {"action": "fail", "message": "стоп"}])
    ag = TaskAgent(cc, memory_path=tmp / "r2.json")
    ag.start("r2", "найди пиццу подешевле на pizza.test", rt, user_id="A")
    h = hist_part(rt.prompts[2]) if len(rt.prompts) > 2 else ""
    check("старое чтение — «was shown», свежее — «the SAME text … (shown "
          "below)»",
          "1. read the page → ok — its text was shown at the next step" in h
          and "2. read the page → ok — the SAME text as the previous read "
          "(shown below)" in h, h)
    ag.feed("r2", "маргариту", rt, user_id="A")
    p = rt.prompts[3] if len(rt.prompts) > 3 else ""
    check("после ответа: «the SAME text as the previous read:» без «(shown "
          "below)», текста нет",
          "the SAME text as the previous read: what you look for" in p
          and "shown below" not in p and "Page text:" not in p,
          hist_part(p))

    print("find:")
    saved = _ba.snapshot_for_goal
    _ba.snapshot_for_goal = lambda host, text, tab_id=None: (
        "https://pizza.test/menu", [{"idx": 700, "tag": "button",
                                     "role": "button",
                                     "text": "Добавить соус"}])
    try:
        cc = FakeCC()
        cc.url = "https://pizza.test/menu"
        rt = ScriptedRouter([{"action": "find", "text": "соус"},
                             {"action": "ask", "question": "Какой соус?"},
                             {"action": "fail", "message": "стоп"}])
        ag = TaskAgent(cc, memory_path=tmp / "f1.json")
        ag.start("f1", "найди соус на pizza.test", rt, user_id="A")
        p1 = rt.prompts[1] if len(rt.prompts) > 1 else ""
        check("шаг сразу после find: найденное в промпте, «listed below»",
              'Elements around "соус"' in p1
              and "elements around it are listed below" in hist_part(p1),
              hist_part(p1))
        ag.feed("f1", "сырный", rt, user_id="A")
        p2 = rt.prompts[2] if len(rt.prompts) > 2 else ""
        check("после ответа: найденного нет, «were listed at the next step»",
              'Elements around "соус"' not in p2
              and "elements around it were listed at the next step, not "
              "repeated now" in hist_part(p2) and "listed below" not in p2,
              hist_part(p2))
    finally:
        _ba.snapshot_for_goal = saved

    print(f"\nИтого: {OK} проверок, {FAILS} провалов")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

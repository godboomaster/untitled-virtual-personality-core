"""Тест карточки задачи агента для веба (TaskAgent.task_card).

Проверяет:
* журнал хода: строки те же, что ушли человеку (notify/ответ) — фронт по
  ним прячет дубли из пузырей; неудача метится ok=False;
* статус: working / ask / confirm / paused; после финала — итог (payment,
  done, cancelled) ещё CARD_TTL_SEC, потом карточки нет;
* план заказа человеку: магазин → позиции → «что-нибудь ещё?» → корзина →
  оформление, текущий шаг один; не заказ — без плана; отмена — без текущего;
* цель без секретов (пароль из ответа на вопрос прогона);
* очистка диалога снимает карточку; чтение протухшего прогона его не снимает.

Запуск: PYTHONPATH=. python3 scripts/test_task_card.py
"""

import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.features import task_agent as ta_mod  # noqa: E402
from app.features.task_agent import CARD_TTL_SEC, RUN_TTL_SEC, TaskAgent  # noqa: E402
from scripts.test_task_agent import FakeCC, ScriptedRouter  # noqa: E402

FAILS = 0


def check(name, cond, detail=""):
    global FAILS
    if cond:
        print(f"  [OK] {name}")
    else:
        FAILS += 1
        print(f"  [FAIL] {name} {detail}")


def bare_agent():
    return TaskAgent(SimpleNamespace(base_dir=tempfile.mkdtemp()))


def run_dict(goal="закажи пиццу", **kw):
    r = {"goal": goal, "lang": "ru", "qa": [], "history": [], "steps": 3,
         "awaiting": None, "busy": False, "cancel": False,
         "touched": time.time(), "started": time.time() - 30, "ui_log": [],
         "id": "r1", "turn_user": "web_user"}
    r.update(kw)
    return r


print("Прогон на фейковом браузере")
# Память задач — во временную папку (FakeCC сам её не задаёт: это делает
# main() test_task_agent), иначе прогон писал бы в data/default
FakeCC.base_dir = Path(tempfile.mkdtemp(prefix="task_card_"))
cc = FakeCC()
agent = TaskAgent(cc)
router = ScriptedRouter([
    {"action": "open", "target": "pizza.test"},
    {"action": "click", "n": 1},                     # Меню
    {"action": "ask", "question": "Какую пиццу: Пепперони или Маргариту?"},
    {"action": "click", "n": 1},                     # Пепперони
    {"action": "click", "n": 4},                     # Оплатить картой
])
notes = []
reply = agent.start("c1", "закажи пиццу на pizza.test", router, notify=notes.append)
card = agent.task_card("c1")
sent = "\n".join(notes + [reply])
check("карточка есть у живого прогона", card is not None)
check("статус — ждёт ответа", card and card["status"] == "ask", card and card["status"])
check("журнал хода не пуст", card and len(card["log"]) >= 2, card and card["log"])
check("каждая строка журнала дословно ушла человеку",
      card and all(x["text"] in sent.splitlines() for x in card["log"]),
      [x["text"] for x in card["log"] if x["text"] not in sent.splitlines()] if card else "")
check("шаги без неудач — ok", card and all(x["ok"] for x in card["log"]))
check("заказ — с планом", card and len(card["plan"]) >= 4, card and card["plan"])
check("в плане ровно один текущий шаг",
      card and sum(1 for p in card["plan"] if p["state"] == "current") == 1)
check("цель в карточке", card and card["goal"] == "закажи пиццу на pizza.test")
n_log = len(card["log"]) if card else 0
reply = agent.feed("c1", "пепперони", router, notify=notes.append)
card = agent.task_card("c1")
check("прогон закончен, карточка осталась", not agent.has_run("c1") and card is not None)
check("итог — передача оплаты", card and card["status"] == "payment", card and card["status"])
check("журнал дописан ходом после ответа", card and len(card["log"]) > n_log)
agent._cards["c1"]["ts"] = time.time() - CARD_TTL_SEC - 1
check("после CARD_TTL_SEC карточки нет", agent.task_card("c1") is None)

print("Статусы и план")
agent = bare_agent()
agent._runs["c"] = run_dict(busy=True)
check("занят → working", agent.task_card("c")["status"] == "working")
for kind, want in (("ask", "ask"), ("confirm", "confirm"), ("switch", "confirm"), ("continue", "paused")):
    agent._runs["c"] = run_dict(awaiting={"kind": kind})
    check(f"{kind} → {want}", agent.task_card("c")["status"] == want)

agent._runs["c"] = run_dict()
plan = agent.task_card("c")["plan"]
check("без магазина — текущий шаг «выбрать магазин»",
      plan[0] == {"text": "выбрать магазин", "state": "current"}, plan[0])
agent._runs["c"] = run_dict(site_ok="pizza.test",
                            brief={"items": [{"name": "Пепперони", "size": "30 см", "qty": 1}]},
                            cart_adds=[{"key": "Пепперони"}])
plan = agent.task_card("c")["plan"]
texts = [p["text"] for p in plan]
check("магазин отмечен", plan[0] == {"text": "магазин: pizza.test", "state": "done"}, plan[0])
check("позиция в корзине отмечена",
      {"text": "положить «Пепперони 30 см» в корзину", "state": "done"} in plan, texts)
check("текущий — «что-нибудь ещё?»",
      next(p for p in plan if p["state"] == "current")["text"] == "спросить «что-нибудь ещё?»")
check("последний шаг — оформление", texts[-1].startswith("оформить заказ"))
agent._runs["c"] = run_dict(goal="найди расписание электричек")
check("не заказ — без плана", agent.task_card("c")["plan"] == [])
agent._runs["c"] = run_dict(goal="order a pizza", lang="en")
check("план по-английски", agent.task_card("c")["plan"][0]["text"] == "choose the shop")

print("Журнал, секреты, отмена, очистка")
r = run_dict()
agent._ui_note(r, "Открыл pizza.test.\nНажал «Меню».")
agent._ui_note(r, "Не удалось нажать «Оформить»: кнопка пропала.")
check("строки разбиты по одной", [x["text"] for x in r["ui_log"]][:2] == ["Открыл pizza.test.", "Нажал «Меню»."])
check("неудача — ok=False", r["ui_log"][-1]["ok"] is False and r["ui_log"][0]["ok"] is True)
for i in range(ta_mod.CARD_LOG_MAX + 5):
    agent._ui_note(r, f"Шаг {i}.")
check("журнал ограничен CARD_LOG_MAX", len(r["ui_log"]) == ta_mod.CARD_LOG_MAX)

agent._runs["c"] = run_dict(goal="войди на сайт, пароль Kotik2019!",
                            qa=[("Какой пароль?", "Kotik2019!")])
check("пароль не попал в цель карточки", "Kotik2019" not in agent.task_card("c")["goal"],
      agent.task_card("c")["goal"])

agent._runs["c"] = run_dict()
agent.cancel("c")
card = agent.task_card("c")
check("отмена вне хода → карточка «cancelled»", card and card["status"] == "cancelled")
check("у отменённой нет текущего шага",
      card and not any(p["state"] == "current" for p in card["plan"]))

agent._runs["c"] = run_dict()
agent._keep_card("c", agent._runs["c"], "done")
agent.forget_chat("c")
check("очистка диалога снимает карточку", agent.task_card("c") is None)

agent._runs["c"] = run_dict(touched=time.time() - RUN_TTL_SEC - 5)
agent.task_card("c")
check("чтение протухшего прогона его не снимает", agent.has_run("c"))

print(f"\n{'ALL OK' if FAILS == 0 else f'{FAILS} FAIL'}")
sys.exit(1 if FAILS else 0)

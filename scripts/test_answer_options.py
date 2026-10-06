"""Тест кнопок ответа режима управления (веб): что показать под вопросом.

Проверяет:
* TaskAgent.answer_options — вопрос с вариантами → кнопки с номерами,
  которые _chosen_option разбирает точно; несколько вопросов одним
  сообщением и вопрос без вариантов → без кнопок; confirm/switch → да/нет,
  continue → да/отмена; занятый и протухший прогон → без кнопок, причём
  протухший не снимается (только чтение);
* ComputerControlManager.pending_answer_options — выбор сайта → номера
  (parse_choice разбирает) и «нет»; обычное подтверждение → да/нет;
  протухший pending не снимается и флаг «истекло» не ставится; чужой
  владелец — без кнопок;
* BotInstance.cc_answer_options — живой прогон агента важнее подтверждения
  команды, вне режима управления кнопок нет.

Запуск: PYTHONPATH=. python3 scripts/test_answer_options.py
"""

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.bot_instance import BotInstance  # noqa: E402
from app.features.computer_control import (  # noqa: E402
    ComputerControlManager, parse_choice)
from app.features.task_agent import RUN_TTL_SEC, TaskAgent, _chosen_option  # noqa: E402

FAILS = 0


def check(name, cond, detail=""):
    global FAILS
    if cond:
        print(f"  [OK] {name}")
    else:
        FAILS += 1
        print(f"  [FAIL] {name} {detail}")


def make_agent():
    agent = TaskAgent.__new__(TaskAgent)
    agent._lock = threading.RLock()
    agent._runs = {}
    return agent


def run_with(awaiting, busy=False, touched=None):
    return {"goal": "закажи пиццу", "awaiting": awaiting, "busy": busy,
            "touched": time.time() if touched is None else touched,
            "turn_user": "web_user"}


print("TaskAgent.answer_options")
agent = make_agent()
q = "Какую пиццу взять?\n- Пепперони — 549 ₽\n- Маргарита — 449 ₽\n- Четыре сыра — 599 ₽"
agent._runs["c"] = run_with({"kind": "ask", "question": q})
opts = agent.answer_options("c")
check("вопрос с вариантами → options", opts and opts["kind"] == "options", opts)
check("три кнопки по порядку", [o["label"] for o in opts["options"]]
      == ["Пепперони — 549 ₽", "Маргарита — 449 ₽", "Четыре сыра — 599 ₽"], opts)
check("кнопки шлют номера", [o["send"] for o in opts["options"]] == ["1", "2", "3"])
check("номер кнопки разбирается точно",
      all(_chosen_option(q, o["send"]) == (i, True)
          for i, o in enumerate(opts["options"], 1)))

multi = ("Перед тем как положить «Пепперони» в корзину:\nКакой размер?\n- 25 см\n- 30 см\n"
         "Какое тесто?\n- Традиционное\n- Тонкое\nОтветь одним сообщением.")
agent._runs["c"] = run_with({"kind": "ask", "question": multi})
check("несколько вопросов одним сообщением → без кнопок", agent.answer_options("c") is None)
agent._runs["c"] = run_with({"kind": "ask", "question": "Какой адрес доставки?"})
check("вопрос без вариантов → без кнопок", agent.answer_options("c") is None)
agent._runs["c"] = run_with({"kind": "ask", "question": "Заказываем здесь?\n- Да, на dodopizza.ru"})
check("один вариант → без кнопок (это не выбор)", agent.answer_options("c") is None)

for kind in ("confirm", "switch"):
    agent._runs["c"] = run_with({"kind": kind, "question": "Делаю? (да/нет)"})
    o = agent.answer_options("c")
    check(f"{kind} → да/нет", o == {"kind": "yesno", "options": [{"role": "yes"}, {"role": "no"}]}, o)
agent._runs["c"] = run_with({"kind": "continue"})
o = agent.answer_options("c")
check("continue → да/отмена", o and [x.get("role") for x in o["options"]] == ["yes", "cancel"], o)

agent._runs["c"] = run_with({"kind": "confirm"}, busy=True)
check("агент занят → без кнопок", agent.answer_options("c") is None)
agent._runs["c"] = run_with({"kind": "confirm"}, touched=time.time() - RUN_TTL_SEC - 5)
check("протухший прогон → без кнопок", agent.answer_options("c") is None)
check("протухший прогон не снят (только чтение)", agent.has_run("c"))
check("нет прогона → None", agent.answer_options("other") is None)

print("ComputerControlManager.pending_answer_options")
cc = ComputerControlManager.__new__(ComputerControlManager)
cc._lock = threading.RLock()
cc._pending = {}
choices = {"kind": "open_site", "value": "кинопоиск", "choices": [
    {"url": "https://www.kinopoisk.ru/", "title": "Кинопоиск — фильмы"},
    {"url": "https://hd.kinopoisk.ru/film/123?token=SECRET", "title": "Кинопоиск HD"},
]}
cc._pending["c"] = {"action": choices, "expires_at": time.time() + 60, "user_id": "web_user"}
o = cc.pending_answer_options("c", "web_user")
check("выбор сайта → options", o and o["kind"] == "options", o)
labels = [x.get("label") for x in o["options"] if "label" in x]
check("подписи как в вопросе (без схемы)", labels[0] == "Кинопоиск — фильмы — kinopoisk.ru", labels)
check("секрет из адреса не попал в подпись", "SECRET" not in " ".join(labels), labels)
check("последняя кнопка — «нет»", o["options"][-1] == {"role": "no"})
check("номер кнопки разбирает parse_choice",
      all(parse_choice(x["send"]) == i for i, x in enumerate(o["options"][:-1], 1)))
check("вопрос и кнопки из одних подписей",
      all(f"{i}. {s}" in ComputerControlManager._choices_question(choices, "ru")
          for i, s in enumerate(labels, 1)))

cc._pending["c"] = {"action": {"kind": "open_site", "value": "youtube.com"},
                    "expires_at": time.time() + 60, "user_id": "web_user"}
o = cc.pending_answer_options("c", "web_user")
check("обычное подтверждение → да/нет", o and o["kind"] == "yesno", o)
check("чужой владелец → без кнопок", cc.pending_answer_options("c", "someone") is None)
cc._pending["c"]["expires_at"] = time.time() - 1
check("протухший pending → без кнопок", cc.pending_answer_options("c", "web_user") is None)
check("протухший pending не снят", "c" in cc._pending)
check("флаг «истекло» не поставлен", "_pending_expired" not in cc.__dict__)

print("BotInstance.cc_answer_options")
agent2 = make_agent()
cc2 = SimpleNamespace(pending_answer_options=lambda c, u: {"kind": "yesno", "options": []})
bot = SimpleNamespace(computer_control=cc2, task_agent=agent2, control_mode_on=lambda c: True)
check("нет прогона → подтверждение команды",
      BotInstance.cc_answer_options(bot, "c", "web_user") == {"kind": "yesno", "options": []})
agent2._runs["c"] = run_with({"kind": "ask", "question": q})
o = BotInstance.cc_answer_options(bot, "c", "web_user")
check("живой прогон важнее подтверждения", o and o["kind"] == "options", o)
agent2._runs["c"] = run_with({"kind": "ask", "question": "Какой адрес?"})
check("прогон со свободным ответом → без кнопок",
      BotInstance.cc_answer_options(bot, "c", "web_user") is None)
bot.control_mode_on = lambda c: False
check("вне режима управления → без кнопок",
      BotInstance.cc_answer_options(bot, "c", "web_user") is None)
bot.control_mode_on = lambda c: True
bot.computer_control = None
check("computer_control выключен → без кнопок",
      BotInstance.cc_answer_options(bot, "c", "web_user") is None)

print(f"\n{'ALL OK' if FAILS == 0 else f'{FAILS} FAIL'}")
sys.exit(1 if FAILS else 0)

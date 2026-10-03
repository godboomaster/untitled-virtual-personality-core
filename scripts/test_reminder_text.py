"""Напоминания текстом в обычном сообщении (BotInstance._reminder_turn):

  1. «отмени напоминание (про X / 2 / все)» и «какие у меня напоминания?»
     не создают новое напоминание: отмена — по словам/номеру/id, при
     нескольких подходящих — вопрос «какое?» и ответ номером; список —
     контекст со списком;
  2. вопрос «когда напомнить?» живёт ограниченное время, принадлежит тому,
     кого спросили (в группе чужая реплика его не трогает), «не надо»
     снимает его, посторонняя реплика отпускает, похожее на время, но
     непонятое — переспрашивается;
  3. новая полная просьба во время «когда?» создаёт напоминание со СВОИМ
     текстом, а не со старым.

Бот — фейк с настоящим ReminderManager на временных данных; LLM не зовётся
(_reformulate_task — тождество). Запуск: python3 -m scripts.test_reminder_text
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from types import MethodType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="reminder_text_")
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")

from app.bot_instance import BotInstance  # noqa: E402
from app.features import reminder_manager as rmod  # noqa: E402
from app.features.reminder_manager import ReminderManager  # noqa: E402

ok = 0
failures = 0
CHAT = "c1"


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


_n = 0


def make_bot():
    global _n
    _n += 1
    bot = SimpleNamespace(
        reminder_manager=ReminderManager(context=f"t{_n}"),
        learning_manager=None,
        _reformulate_task=lambda t: t,
        get_chat_topic=lambda c: None,
    )
    bot._postpone_handled_context = MethodType(BotInstance._postpone_handled_context, bot)
    return bot


def say(bot, text, user="u1", name="Аня"):
    return BotInstance._reminder_turn(bot, text, CHAT, user, name, None)


def tasks(bot):
    return [r.get("task") for r in bot.reminder_manager.get_active(CHAT)]


def seed(bot, *items):
    for t in items:
        bot.reminder_manager.add_reminder(CHAT, "Аня", t, 3600, user_id="u1")


def main():
    section("1. отмена и список не создают напоминание")
    bot = make_bot()
    seed(bot, "выпить воду", "купить хлеб")
    ctx, is_rem = say(bot, "Отмени напоминание про воду")
    check("«отмени напоминание про воду» — отменено именно оно", tasks(bot) == ["купить хлеб"])
    check("…контекст: отменено, с названием", is_rem and "ALREADY" in ctx and "выпить воду" in ctx)
    check("…вопроса «когда?» нет", bot.reminder_manager.get_pending_remind(CHAT) is None)
    ctx, is_rem = say(bot, "какие у меня напоминания?")
    check("«какие у меня напоминания?» — список, без нового", tasks(bot) == ["купить хлеб"]
          and is_rem and "купить хлеб" in ctx and "Active reminders" in ctx)
    check("…вопроса «когда?» нет", bot.reminder_manager.get_pending_remind(CHAT) is None)
    ctx, _ = say(bot, "отмени напоминание")
    check("одно активное и без уточнения — отменено оно", tasks(bot) == [] and "ALREADY" in ctx)
    ctx, _ = say(bot, "какие у меня напоминания?")
    check("пустой список — честно «нет»", "NO active reminders" in ctx and tasks(bot) == [])

    bot = make_bot()
    seed(bot, "позвонить маме", "позвонить в банк", "купить хлеб")
    ctx, _ = say(bot, "отмени напоминание про позвонить")
    check("подходят два — ничего не отменено, вопрос «какое?»",
          len(tasks(bot)) == 3 and "NOTHING was cancelled" in ctx
          and bot.reminder_manager.get_pending_cancel_choice(CHAT))
    ctx, _ = say(bot, "2")
    check("ответ «2» — отменено второе из показанных", tasks(bot) == ["позвонить маме", "купить хлеб"]
          and not bot.reminder_manager.get_pending_cancel_choice(CHAT))
    ctx, _ = say(bot, "удали напоминание 7")
    check("несуществующий номер — ничего не отменено, список показан",
          len(tasks(bot)) == 2 and "none of the active reminders" in ctx)
    ctx, _ = say(bot, "отмени все напоминания")
    check("«отмени все напоминания» — все отменены", tasks(bot) == [])

    bot = make_bot()
    seed(bot, "water the plants")
    ctx, _ = say(bot, "what reminders do I have?")
    check("EN: список", "water the plants" in ctx and tasks(bot) == ["water the plants"])
    ctx, _ = say(bot, "cancel the reminder about water")
    check("EN: отмена по словам", tasks(bot) == [])

    section("2. вопрос «когда напомнить?»")
    bot = make_bot()
    ctx, is_rem = say(bot, "напомни купить молоко")
    check("без времени — вопрос «когда?» и задача запомнена",
          bot.reminder_manager.get_pending_remind(CHAT) == "напомни купить молоко" or
          "купить молоко" in (bot.reminder_manager.get_pending_remind(CHAT) or ""))
    ctx, is_rem = say(bot, "через 10 минут")
    check("ответ «через 10 минут» — создано со старой задачей",
          len(tasks(bot)) == 1 and "купить молоко" in tasks(bot)[0]
          and bot.reminder_manager.get_pending_remind(CHAT) is None)

    bot = make_bot()
    say(bot, "напомни купить молоко")
    ctx, is_rem = say(bot, "как дела?")
    check("посторонняя реплика — вопрос снят, сообщение обычное",
          not is_rem and ctx is None and bot.reminder_manager.get_pending_remind(CHAT) is None
          and tasks(bot) == [])

    bot = make_bot()
    say(bot, "напомни купить молоко")
    ctx, is_rem = say(bot, "в какое-нибудь время")
    check("похоже на время, но не понято — переспрос, вопрос остаётся",
          is_rem and "could not be understood" in ctx
          and bot.reminder_manager.get_pending_remind(CHAT) is not None)
    ctx, is_rem = say(bot, "не надо")
    check("«не надо» — вопрос снят, ничего не создано",
          is_rem and "decided not to set" in ctx and tasks(bot) == []
          and bot.reminder_manager.get_pending_remind(CHAT) is None)

    bot = make_bot()
    seed(bot, "старое дело")
    say(bot, "напомни купить молоко")
    ctx, _ = say(bot, "отмени напоминание")
    check("«отмени напоминание» на «когда?» — отказ от создаваемого, старое цело",
          "decided not to set" in ctx and tasks(bot) == ["старое дело"]
          and bot.reminder_manager.get_pending_remind(CHAT) is None)

    bot = make_bot()
    say(bot, "напомни купить молоко")
    with bot.reminder_manager._lock:
        bot.reminder_manager._pending_remind[CHAT]["asked_at"] = time.time() - rmod.PENDING_REMIND_TTL_SEC - 5
    ctx, is_rem = say(bot, "через 10 минут")
    check("вопрос старше срока — ответ уже не к нему, ничего не создано",
          tasks(bot) == [] and bot.reminder_manager.get_pending_remind(CHAT) is None)

    bot = make_bot()
    say(bot, "напомни купить молоко", user="u1")
    ctx, is_rem = say(bot, "через 10 минут", user="u2", name="Боря")
    check("группа: чужой ответ не забирает вопрос", tasks(bot) == []
          and bot.reminder_manager.get_pending_remind(CHAT, "u1") is not None)
    say(bot, "через 10 минут", user="u1")
    check("…а ответ спрошенного — создаёт", len(tasks(bot)) == 1
          and bot.reminder_manager.get_active(CHAT)[0].get("user_id") == "u1")

    section("3. новая полная просьба во время «когда?»")
    bot = make_bot()
    say(bot, "напомни купить молоко")
    ctx, is_rem = say(bot, "напомни через 5 минут купить хлеб")
    check("создано напоминание со СВОЕЙ задачей", len(tasks(bot)) == 1
          and "купить хлеб" in tasks(bot)[0] and "молоко" not in tasks(bot)[0])
    check("…старый вопрос «когда?» снят", bot.reminder_manager.get_pending_remind(CHAT) is None)

    bot = make_bot()
    say(bot, "напомни купить молоко")
    say(bot, "напомни через 5 минут")
    check("«напомни через 5 минут» без своей задачи — ответ на «когда?» (старая задача)",
          len(tasks(bot)) == 1 and "купить молоко" in tasks(bot)[0])

    section("регрессии")
    bot = make_bot()
    ctx, is_rem = say(bot, "напомни через 5 минут выпить воды")
    check("обычная просьба со временем — создана", is_rem and len(tasks(bot)) == 1
          and "выпить воды" in tasks(bot)[0])
    ctx, is_rem = say(bot, "напоминание пришло вовремя, спасибо")
    check("разговор о напоминании — не просьба, «когда?» не задан",
          not is_rem and ctx is None and bot.reminder_manager.get_pending_remind(CHAT) is None)
    ctx, is_rem = say(bot, "перенеси напоминание на 10 минут")
    check("перенос по-прежнему работает", is_rem and "ALREADY applied" in ctx)

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

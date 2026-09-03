"""Smoke-тест живого включения/выключения фич reminder/todo/inventory.

Проверяет: BotInstance.sync_feature_managers (создание/остановка менеджеров),
обвязку свежесозданного reminder-менеджера к веб-inbox (wire_reminder_for_api:
sender/память/LLP/заморозка + запуск фонового цикла), идемпотентность
ReminderManager.start и семантику restart_required в save_persona_yaml.

Запуск: python -m scripts.test_live_feature_toggle
"""

import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    import os
    tmp = tempfile.mkdtemp(prefix="live_toggle_smoke_")
    os.environ["DATA_DIR"] = tmp

    import importlib
    import app.core.config as config_mod
    importlib.reload(config_mod)

    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    # ── 1. sync_feature_managers: выключено → включено → выключено ──
    import app.bot_instance as bi
    from app.features.reminder_manager import ReminderManager
    from app.features.todo_manager import TodoManager
    from app.features.inventory_manager import InventoryManager

    ctx = "live_toggle_test_ctx"
    bot = SimpleNamespace(
        features={"reminder": False, "todo": False, "inventory": False},
        context=ctx, persona_name="live_toggle_test",
        intellect=SimpleNamespace(tier="primitive"),
        reminder_manager=None, todo_manager=None, inventory_manager=None,
        memory=SimpleNamespace(), router=SimpleNamespace(
            set_persona_llm=lambda *a, **k: None), persona=SimpleNamespace(),
    )

    res = bi.BotInstance.sync_feature_managers(bot)
    check("все выключены: менеджеров нет",
          res == {"reminder": False, "todo": False, "inventory": False}
          and bot.reminder_manager is None and bot.todo_manager is None
          and bot.inventory_manager is None)

    bot.features = {"reminder": True, "todo": True, "inventory": True}
    res = bi.BotInstance.sync_feature_managers(bot)
    check("включение: все три менеджера созданы",
          res == {"reminder": True, "todo": True, "inventory": True}
          and isinstance(bot.reminder_manager, ReminderManager)
          and isinstance(bot.todo_manager, TodoManager)
          and isinstance(bot.inventory_manager, InventoryManager))
    check("primitive-tier прокинут в reminder-менеджер", bot.reminder_manager._primitive)

    stopped = bot.reminder_manager
    bot.features = {"reminder": False, "todo": False, "inventory": False}
    res = bi.BotInstance.sync_feature_managers(bot)
    check("выключение: менеджеры остановлены и убраны",
          res == {"reminder": False, "todo": False, "inventory": False}
          and bot.reminder_manager is None and bot.todo_manager is None
          and bot.inventory_manager is None
          and stopped._running is False)

    # Повторный sync без изменений — идемпотентен, объекты не пересоздаются
    bot.features = {"reminder": True, "todo": True, "inventory": True}
    bi.BotInstance.sync_feature_managers(bot)
    rm = bot.reminder_manager
    bi.BotInstance.sync_feature_managers(bot)
    check("идемпотентность: менеджер не пересоздаётся", bot.reminder_manager is rm)

    # ── 2. wire_reminder_for_api: обвязка + запуск фонового цикла ──
    from app.api.inbox import wire_reminder_for_api, WebInboxSender
    bot.features = {"reminder": True}
    bot.reminder_manager = ReminderManager(context=ctx)
    wire_reminder_for_api("live_toggle_test", bot)
    rm = bot.reminder_manager
    check("обвязка: sender/memory/router/persona/muted_check",
          isinstance(rm._sender, WebInboxSender) and rm._memory is bot.memory
          and rm._router is bot.router and rm._persona is bot.persona
          and callable(rm._muted_check))
    bot.features["muted"] = True
    muted_now = rm._muted_check()
    bot.features.pop("muted", None)
    check("заморозка читается живьём из features",
          muted_now is True and rm._muted_check() is False)
    deadline = time.time() + 5
    while not rm._running and time.time() < deadline:
        time.sleep(0.05)
    task_after_start = rm._task
    check("фоновый цикл запущен", rm._running and task_after_start is not None)
    rm.start()  # идемпотентность: повторный старт не плодит задачи
    check("повторный start — no-op (задача не пересоздана)", rm._task is task_after_start)
    rm.stop()

    # ── 3. save_persona_yaml: reminder-переключатель больше не требует рестарта ──
    import app.api.settings_api as sa
    sa._PERSONAS_DIR = Path(tmp)
    from app.api import runtime
    bot.sync_feature_managers = lambda: bi.BotInstance.sync_feature_managers(bot)
    runtime.registry._bots["live_toggle_test"] = bot

    yaml_all_off = (
        "id: live_toggle_test\nname: t\nsystem_prompt: ping\n"
        "features:\n  reminder: false\n  todo: false\n  inventory: false\n"
    )
    (Path(tmp) / "live_toggle_test.yaml").write_text(yaml_all_off, encoding="utf-8")
    sa.save_persona_yaml("live_toggle_test", yaml_all_off)
    check("все выключены: менеджеров нет",
          bot.reminder_manager is None and bot.todo_manager is None
          and bot.inventory_manager is None)

    yaml_reminder_on = yaml_all_off.replace("reminder: false", "reminder: true")
    r = sa.save_persona_yaml("live_toggle_test", yaml_reminder_on)
    check("reminder: true через YAML-редактор — рестарт не нужен",
          r["ok"] and r["restart_required"] is False)
    deadline = time.time() + 5
    while (bot.reminder_manager is None or not bot.reminder_manager._running) \
            and time.time() < deadline:
        time.sleep(0.05)
    check("reminder-менеджер создан и запущен на живом боте",
          isinstance(bot.reminder_manager, ReminderManager)
          and bot.reminder_manager._running)
    check("todo/inventory остались выключены",
          bot.todo_manager is None and bot.inventory_manager is None)

    yaml_all_on = yaml_reminder_on.replace("todo: false", "todo: true") \
                                 .replace("inventory: false", "inventory: true")
    r = sa.save_persona_yaml("live_toggle_test", yaml_all_on)
    check("todo/inventory: true — рестарт не нужен",
          r["restart_required"] is False and isinstance(bot.todo_manager, TodoManager)
          and isinstance(bot.inventory_manager, InventoryManager))

    r = sa.save_persona_yaml("live_toggle_test", yaml_all_off)
    check("выключение через YAML: менеджеры убраны, рестарт не нужен",
          r["restart_required"] is False and bot.reminder_manager is None
          and bot.todo_manager is None and bot.inventory_manager is None)

    yaml_needs_restart = yaml_all_off + "  self_memory: true\n"
    r = sa.save_persona_yaml("live_toggle_test", yaml_needs_restart)
    check("прочие фичи по-прежнему требуют рестарта", r["restart_required"] is True)

    # ── 4. update_persona_config: путь тумблеров в настройках персоны ──
    r = sa.update_persona_config("live_toggle_test", None, None,
                                 features={"reminder": True})
    deadline = time.time() + 5
    while (bot.reminder_manager is None or not bot.reminder_manager._running) \
            and time.time() < deadline:
        time.sleep(0.05)
    check("тумблер reminder в настройках — live, цикл запущен",
          r["restart_required"] is False
          and isinstance(bot.reminder_manager, ReminderManager)
          and bot.reminder_manager._running)
    r = sa.update_persona_config("live_toggle_test", None, None,
                                 features={"reminder": False})
    check("тумблер reminder off — менеджер убран, рестарт не нужен",
          r["restart_required"] is False and bot.reminder_manager is None)
    r = sa.update_persona_config("live_toggle_test", None, None,
                                 features={"web_search": True})
    check("веб-поиск через настройки — по-прежнему нужен рестарт",
          r["restart_required"] is True)

    runtime.registry._bots.pop("live_toggle_test", None)

    print("\nOK" if ok > 0 else "\nFAILURES")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

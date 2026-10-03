"""Замороженная персона (features.muted) в Telegram.

  1. Заморозка из веба (API-процесс пишет YAML) доходит до Telegram-процесса
     без рестарта: PersonaLayer.is_muted перечитывает YAML по mtime (не чаще
     раза в _MUTED_RECHECK_SEC), BotInstance.is_muted переносит флаг в features.
  2. Входящие (текст/фото/документ/команды — общий _gate_update): замороженная
     персона не отвечает, pre_check и обработчик не вызываются; владельцу —
     уведомление не чаще раза в час на чат, остальным — тишина.
  3. Напоминания: muted_check подключается в BotInstance (любой канал), _fire
     при заморозке ничего не шлёт.
  4. Фоновые проверки proactive и learning берут свежий флаг через is_muted.

Без сети и Telegram; YAML — только временный.
Запуск: python3 -m scripts.test_telegram_muted
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
_TMP = Path(tempfile.mkdtemp(prefix="tg_muted_"))
os.environ["VPC_DATA_DIR"] = str(_TMP / "data")
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")
os.environ.pop("OWNER_USER_ID", None)

import app.core.persona as persona_mod  # noqa: E402
from app.core.persona import PersonaLayer  # noqa: E402

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


def write_yaml(path: Path, muted: bool, bump: float = 0.0):
    path.write_text(
        "system_prompt: test persona\n"
        "features:\n"
        "  reminder: true\n"
        f"  muted: {'true' if muted else 'false'}\n",
        encoding="utf-8",
    )
    if bump:
        st = path.stat()
        os.utime(path, (st.st_atime, st.st_mtime + bump))


class FakeMessage:
    def __init__(self, text, chat_id):
        self.text = text
        self.caption = None
        self.chat_id = chat_id
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def main():
    yaml_path = _TMP / "frozen_test.yaml"
    write_yaml(yaml_path, muted=False)
    real_find = persona_mod.find_persona_file
    persona_mod.find_persona_file = lambda name: yaml_path if name == "frozen_test" else None
    try:
        section("1. живой подхват заморозки из YAML")
        p = PersonaLayer("frozen_test")
        check("при старте не заморожена", p.is_muted() is False)
        write_yaml(yaml_path, muted=True, bump=2)
        check("сразу после правки — ещё старое значение (сверка не чаще раза в N с)",
              p.is_muted() is False)
        p._muted_checked_at = float("-inf")
        check("после интервала — заморожена без рестарта", p.is_muted() is True)
        write_yaml(yaml_path, muted=False, bump=4)
        p._muted_checked_at = float("-inf")
        check("разморозка тоже подхватывается", p.is_muted() is False)

        from app.bot_instance import BotInstance
        write_yaml(yaml_path, muted=True, bump=6)
        p._muted_checked_at = float("-inf")
        fake = SimpleNamespace(persona=p, features={"reminder": True})  # отдельный dict
        check("BotInstance.is_muted — True", BotInstance.is_muted(fake) is True)
        check("…и переносит флаг в bot.features", fake.features.get("muted") is True)

        section("2. входящие Telegram")
        try:
            import telegram  # noqa: F401
        except ImportError:
            print("  [SKIP] нет python-telegram-bot")
        else:
            import app.telegram_bot as tb
            tb._muted_noticed.clear()
            calls = []
            state = {"muted": True}
            bot = SimpleNamespace(
                persona_name="frozen_test", owner="111", features={},
                is_muted=lambda: state["muted"],
                pre_check=lambda u, t, priv: calls.append(u),
                chat_user_language=lambda chat_id: None,
            )
            m = FakeMessage("привет", 111)
            blocked = asyncio.run(tb._gate_update(bot, m, "111", True, "привет"))
            check("заморожена: апдейт заблокирован", blocked is True)
            check("pre_check не вызывался", calls == [])
            check("владельцу — уведомление о заморозке", len(m.replies) == 1 and "заморожена" in m.replies[0])
            m2 = FakeMessage("ещё", 111)
            asyncio.run(tb._gate_update(bot, m2, "111", True, "ещё"))
            check("второе уведомление в течение часа — нет", m2.replies == [])
            tb._muted_noticed[("frozen_test", "111")] = time.monotonic() - tb._MUTED_NOTICE_SEC - 1
            m3 = FakeMessage("hello there", 111)
            asyncio.run(tb._gate_update(bot, m3, "111", True, "hello there"))
            check("через час — снова, на языке сообщения (en)",
                  len(m3.replies) == 1 and "frozen" in m3.replies[0])
            m4 = FakeMessage("привет", 222)
            blocked = asyncio.run(tb._gate_update(bot, m4, "222", True, "привет"))
            check("не владелец: заблокирован и без ответа", blocked is True and m4.replies == [])
            os.environ["OWNER_USER_ID"] = "333"
            m5 = FakeMessage("привет", 333)
            asyncio.run(tb._gate_update(bot, m5, "333", True, "привет"))
            check("владелец из OWNER_USER_ID тоже получает уведомление", len(m5.replies) == 1)
            os.environ.pop("OWNER_USER_ID", None)
            state["muted"] = False
            m6 = FakeMessage("привет", 111)
            blocked = asyncio.run(tb._gate_update(bot, m6, "111", True, "привет"))
            check("разморожена: гейт пропускает, pre_check вызван", blocked is False and calls == ["111"])
            # Текст, фото, документ: заморозка проверяется ДО записи активности
            # (on_user_message), иначе замороженной персоне фото/файл двигали
            # ритм и порог молчания
            import inspect
            src = inspect.getsource(tb.create_handlers)
            for name in ("handle_message", "handle_document", "handle_photo"):
                body = src[src.index(f"async def {name}("):]
                body = body[:body.index("\n    async def ", 10)] if "\n    async def " in body[10:] else body
                i_mute, i_act = body.find("_muted_block("), body.find("bot.on_user_message(")
                check(f"{name}: заморозка до записи активности", 0 <= i_mute < i_act)
            legacy = SimpleNamespace(persona_name="x", owner="1", features={"muted": True},
                                     pre_check=lambda u, t, priv: None,
                                     chat_user_language=lambda c: None)
            check("бот без is_muted — флаг из features",
                  asyncio.run(tb._gate_update(legacy, FakeMessage("a", 5), "5", True, "a")) is True)

        section("3. напоминания")
        from app.features.reminder_manager import ReminderManager
        sent = []

        class Sender:
            async def send_message(self, *a, **kw):
                sent.append((a, kw))

        rm = ReminderManager(context="muted_test")
        rm.set_sender(Sender())
        rm.set_muted_check(lambda: True)
        res = asyncio.run(rm._fire({"chat_id": "1", "task": "выпить воды", "user_name": "U"}))
        check("_fire при заморозке: ничего не отправлено", sent == [] and res is True)

        bot2 = SimpleNamespace(
            features={"reminder": True}, reminder_manager=None, todo_manager=None,
            inventory_manager=None, context="muted_test2", persona_name="frozen_test",
            intellect=SimpleNamespace(tier="full"), turn_gate=None,
        )
        bot2.is_muted = lambda: True
        BotInstance.sync_feature_managers(bot2)
        check("живое включение reminder: muted_check подключён",
              bot2.reminder_manager is not None and bot2.reminder_manager._muted_check is bot2.is_muted)
        src = Path(__file__).parent.parent.joinpath("app/bot_instance.py").read_text(encoding="utf-8")
        check("при создании бота reminder получает muted_check=self.is_muted",
              "self.reminder_manager.set_muted_check(self.is_muted)" in src)
        check("rhythm получает muted_check=self.is_muted", "muted_check=self.is_muted," in src)

        section("4. proactive и learning — свежий флаг")
        from app.features.proactive_messaging import ProactiveMessaging
        from app.features.learning_manager import LearningManager
        live = SimpleNamespace(persona_data={"features": {"muted": False}}, is_muted=lambda: True)
        check("proactive: берёт is_muted, а не устаревший persona_data",
              ProactiveMessaging._persona_muted(SimpleNamespace(persona=live)) is True)
        check("learning: берёт is_muted, а не устаревший persona_data",
              LearningManager._is_muted(SimpleNamespace(_persona=live)) is True)
        plain = SimpleNamespace(persona_data={"features": {"muted": True}})
        check("заглушка персоны без is_muted — по persona_data",
              ProactiveMessaging._persona_muted(SimpleNamespace(persona=plain)) is True)
    finally:
        persona_mod.find_persona_file = real_find
        shutil.rmtree(_TMP, ignore_errors=True)

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

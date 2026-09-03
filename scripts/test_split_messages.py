"""Smoke-тест расщепления ответа на отдельные сообщения (settings.split_messages).

Проверяет: BotInstance.split_reply_parts (граница — пустая строка, пример из
задачи), _save_assistant_reply (STM пишется по частям, хвост уходит в pending,
возвращается первая часть) и _rewrite_image_stm веб-сервера (переписывает
весь хвост assistant-частей, а не фиксированные 2 сообщения).

Запуск: python -m scripts.test_split_messages
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    tmp = tempfile.mkdtemp(prefix="split_smoke_")
    os.environ["DATA_DIR"] = tmp

    import importlib

    import app.core.config as config_mod
    importlib.reload(config_mod)

    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    import app.bot_instance as bi

    def make_bot(split):
        # Лёгкий BotInstance без тяжёлого __init__: методы расщепления
        # зависят только от persona.settings, memory и pending-бакета
        bot = bi.BotInstance.__new__(bi.BotInstance)
        bot.persona = SimpleNamespace(settings={"split_messages": True} if split else {})
        bot._pending_split_messages = {}
        saved = []
        bot.memory = SimpleNamespace(
            add_message=lambda role, content, uid, cid: saved.append((role, content)))
        bot.saved = saved
        return bot

    example = (
        "Я фиксирую завершённость. Ты закончил — не значит, что решил проблему. "
        "Это значит, что ты прекратил попытки её решить. Разница существенная.\n\n"
        "Но твоё «просто ме»... Оно не случайно. Ты не просто закончил. Ты закончил "
        "на полуслове — и это даёт тебе право считать, что я не понял. Потому что ты "
        "сам не дал мне понять.\n\n"
        "Монетка в кармане. Я жду, пока ты решишь, что делать дальше.")

    # ── 1. Включено: пример из задачи → три абзаца тремя сообщениями ──
    bot = make_bot(split=True)
    parts = bot.split_reply_parts(example)
    check("пример из задачи → 3 части", len(parts) == 3)
    check("часть 1 = первый абзац",
          parts[0].startswith("Я фиксирую завершённость.")
          and parts[0].endswith("Разница существенная."))
    check("часть 3 = последний абзац",
          parts[2] == "Монетка в кармане. Я жду, пока ты решишь, что делать дальше.")

    # ── 2. Выключено → ответ уходит как есть ──
    bot_off = make_bot(split=False)
    check("выключено → один кусок", bot_off.split_reply_parts(example) == [example])

    # ── 3. Краевые случаи ──
    check("один абзац → без расщепления", bot.split_reply_parts("Просто ответ.") == ["Просто ответ."])
    check("пустые/пробельные строки — граница", bot.split_reply_parts("a\n\n\n\nb\n \nc") == ["a", "b", "c"])
    check("одиночный \\n внутри абзаца не режет", bot.split_reply_parts("строка 1\nстрока 2") == ["строка 1\nстрока 2"])
    check("пустой текст → []", bot.split_reply_parts("  \n\n ") == [])

    # ── 4. _save_assistant_reply: STM по частям, хвост в pending ──
    first = bot._save_assistant_reply(example, "u1", "c1")
    check("возврат — первая часть", first == parts[0])
    check("STM: 3 assistant-сообщения по частям", [c for _r, c in bot.saved] == parts)
    check("pending-хвост = части 2–3", bot.pop_pending_split_messages("c1") == parts[1:])
    check("pending очищается после pop", bot.pop_pending_split_messages("c1") == [])

    # ── 5. Выключено — прежнее поведение одним сообщением ──
    first_off = bot_off._save_assistant_reply(example, "u1", "c1")
    check("выключено: ответ без изменений", first_off == example)
    check("выключено: STM одним сообщением",
          bot_off.saved == [("assistant", example)])
    check("выключено: pending пуст", bot_off.pop_pending_split_messages("c1") == [])

    # ── 6. _rewrite_image_stm (веб): переписывает хвост из N частей + user ──
    from app.api.server import _rewrite_image_stm

    class FakeStm:
        def __init__(self):
            self.buf = []

        def get_messages(self, chat_id=None):
            return self.buf

        def pop_last_n(self, n, chat_id):
            if n:
                del self.buf[-n:]
            return n

        def add_message(self, role, content, uid, cid):
            self.buf.append({"role": role, "content": content})

    stm = FakeStm()
    web_bot = SimpleNamespace(memory=SimpleNamespace(stm=stm))
    # Порядок как в реальном process_message: user-синтетика, затем части
    stm.buf.append({"role": "user", "content": "старый вопрос"})
    stm.buf.append({"role": "user", "content": "синтетика vision"})
    for p in parts:
        stm.buf.append({"role": "assistant", "content": p})
    _rewrite_image_stm(web_bot, "c1", "u1", "📷 фото", parts)
    check("STM после правки: старая реплика + 📷 + все части",
          [m["content"] for m in stm.buf] == ["старый вопрос", "📷 фото"] + parts)

    print("\n" + ("ALL OK" if ok > 0 else "FAILED"))
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

"""Тест: реплика из скина не попадает в режим управления.

Скин веб-интерфейса — сторонний код в песочнице (iframe). Он может сам, без
человека, вызвать vpc.send(...), а собеседник в вебе всегда владелец. Поэтому
реплика из скина (ChatRequest.from_skin) идёт с флагом хода, и единая точка
авторизации режима управления (_cc_allowed) в таком ходе отвечает False:
ни команд, ни подтверждений «да», ни маркеров LLM. Проверяем:

  - ChatRequest.from_skin: по умолчанию False;
  - _cc_allowed в ходе из скина — False, после хода (и после исключения) —
    снова True; вложенный process_message флаг не снимает; другой поток
    флага не видит;
  - в режиме управления и на «перейди в режим управления» из скина —
    подсказка владельцу, переключатель режима не вызывается; без скина —
    как раньше;
  - cc_turn_enter («стоп», «ещё работаю») для скина не вызывается;
  - оба эндпоинта чата передают from_skin в process_message.

Запуск: PYTHONPATH=. python3 -m scripts.test_skin_turn
"""

import sys
import threading
from contextlib import nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n== {title}")


class _Stm:
    def get_last(self, n, chat_id=None):
        return []


class _Memory:
    def __init__(self):
        self.stm = _Stm()
        self.added = []

    def add_message(self, role, content, user_id=None, chat_id=None, user_name=None):
        self.added.append((role, content))


class _CC:
    def __init__(self):
        self.calls = []

    def set_turn(self, key, lang):
        self.calls.append(("set_turn", key))

    def chat_scope(self, key):
        return nullcontext()

    def __getattr__(self, name):
        # Любой другой вызов менеджера управления в ходе из скина — ошибка теста
        def _any(*a, **kw):
            self.calls.append((name, a))
        return _any


def _bot(control_on=False):
    from app.bot_instance import BotInstance
    bot = BotInstance.__new__(BotInstance)
    bot.web_single_user = True
    bot.owner = ""
    bot._cc_allowed_users = set()
    bot.memory = _Memory()
    bot.computer_control = _CC()
    bot.proactive = None
    bot._pending_list_messages = {}
    bot._pending_split_messages = {}
    bot._pending_photos = {}
    bot._pending_question_kind = {}
    bot.user_turn = lambda key: nullcontext()
    bot.stm_key = lambda chat_id, user_id: f"{chat_id or user_id}"
    bot.control_mode_on = lambda key: control_on
    bot.switch_calls = []

    def _switch(key, mode, lang=None):
        bot.switch_calls.append((key, mode))
        return "режим переключён"
    bot._control_mode_switch = _switch
    return bot


def test_schema():
    section("ChatRequest.from_skin")
    from app.api.schemas import ChatRequest
    r = ChatRequest(persona="connor", message="привет")
    check("по умолчанию False", r.from_skin is False)
    r = ChatRequest(persona="connor", message="привет", from_skin=True)
    check("принимает True", r.from_skin is True)


def test_cc_allowed_flag():
    section("_cc_allowed в ходе из скина")
    bot = _bot()
    seen = {}

    def impl(user_input, user_id="default", chat_id=None, from_skin=False, **kw):
        seen.setdefault("outer", bot._cc_allowed(user_id, chat_id))
        seen.setdefault("from_skin", from_skin)
        if user_input == "nested":
            # Агент задач передаёт реплику дальше обычным вызовом
            bot.process_message("inner", user_id=user_id, chat_id=chat_id)
            seen["after_nested"] = bot._cc_allowed(user_id, chat_id)
        if user_input == "inner":
            seen["inner"] = bot._cc_allowed(user_id, chat_id)
        if user_input == "thread":
            box = {}
            t = threading.Thread(target=lambda: box.setdefault("v", bot._cc_allowed(user_id, chat_id)))
            t.start()
            t.join()
            seen["other_thread"] = box.get("v")
        if user_input == "boom":
            raise RuntimeError("boom")
        return "ответ"
    bot._process_message_impl = impl

    check("вне хода владелец допущен", bot._cc_allowed("web_user", "web") is True)
    bot.process_message("привет", user_id="web_user", chat_id="web", from_skin=True)
    check("в ходе из скина — False", seen["outer"] is False)
    check("from_skin доходит до _process_message_impl", seen["from_skin"] is True)
    check("после хода — снова True", bot._cc_allowed("web_user", "web") is True)

    seen.clear()
    bot.process_message("привет", user_id="web_user", chat_id="web")
    check("обычный ход — True", seen["outer"] is True)

    seen.clear()
    bot.process_message("nested", user_id="web_user", chat_id="web", from_skin=True)
    check("вложенный process_message в ходе из скина — False", seen.get("inner") is False)
    check("после вложенного вызова флаг не снят", seen.get("after_nested") is False)
    check("после хода — True", bot._cc_allowed("web_user", "web") is True)

    seen.clear()
    bot.process_message("thread", user_id="web_user", chat_id="web", from_skin=True)
    check("другой поток флага хода не видит", seen.get("other_thread") is True)

    try:
        bot.process_message("boom", user_id="web_user", chat_id="web", from_skin=True)
    except RuntimeError:
        pass
    check("исключение в ходе снимает флаг", bot._cc_allowed("web_user", "web") is True)


def test_skin_note():
    section("Подсказка вместо режима управления")
    from app.features import cc_texts
    note_ru = cc_texts.t("skin_no_control", "ru")
    note_en = cc_texts.t("skin_no_control", "en")
    check("тексты подсказки есть на двух языках", note_ru != note_en and "скин" in note_ru.lower())

    bot = _bot(control_on=True)
    reply = bot.process_message("открой ютуб", user_id="web_user",
                                chat_id="web", from_skin=True)
    check("режим включён, реплика из скина → подсказка", reply == note_ru)
    check("реплика и подсказка записаны в историю",
          bot.memory.added == [("user", "открой ютуб"), ("assistant", note_ru)])
    check("менеджер управления не вызывался (кроме set_turn)",
          [c for c in bot.computer_control.calls if c[0] != "set_turn"] == [])

    bot = _bot(control_on=True)
    reply = bot.process_message("да", user_id="web_user", chat_id="web", from_skin=True)
    check("«да» из скина не подтверждает — подсказка", reply == note_ru)

    bot = _bot(control_on=False)
    reply = bot.process_message("перейди в режим управления", user_id="web_user",
                                chat_id="web", from_skin=True)
    check("«перейди в режим управления» из скина → подсказка", reply == note_ru)
    check("переключатель режима не вызывался", bot.switch_calls == [])

    bot = _bot(control_on=False)
    reply = bot.process_message("enter control mode", user_id="web_user",
                                chat_id="web", from_skin=True)
    check("английская команда → подсказка на английском", reply == note_en)

    bot = _bot(control_on=False)
    reply = bot.process_message("перейди в режим управления", user_id="web_user",
                                chat_id="web")
    check("без скина переключатель работает как раньше",
          reply == "режим переключён" and bot.switch_calls == [("web", True)])

    bot = _bot(control_on=True)
    bot.computer_control = None
    bot._control_mode_switch = lambda *a, **kw: "режим переключён"
    # Подсказки нет — реплика уходит в обычный конвейер ответа, которому
    # у заглушки бота не хватает полей (AttributeError = прошли мимо)
    try:
        passed = bot.process_message("перейди в режим управления", user_id="web_user",
                                     chat_id="web", from_skin=True) != note_ru
    except AttributeError:
        passed = True
    check("без фичи computer_control подсказки нет", passed)


def test_server():
    section("Эндпоинты чата")
    from app.api import server
    from app.api.schemas import ChatRequest

    class _B:
        def __init__(self):
            self.calls = 0

        def cc_turn_enter(self, text, user_id, chat_id):
            self.calls += 1
            return "Останавливаю…", "tok"

    b = _B()
    early, tok = server._cc_turn_enter(b, ChatRequest(persona="connor", message="стоп", from_skin=True))
    check("cc_turn_enter для скина не вызывается", b.calls == 0 and early is None and tok is None)
    early, tok = server._cc_turn_enter(b, ChatRequest(persona="connor", message="стоп"))
    check("без скина — как раньше", b.calls == 1 and early == "Останавливаю…")

    src = Path(server.__file__).read_text(encoding="utf-8")
    check("оба вызова process_message передают from_skin",
          src.count("from_skin=req.from_skin") == 2
          and src.count("bot.process_message(") == 2)


def main():
    test_schema()
    test_cc_allowed_flag()
    test_skin_note()
    test_server()
    print(f"\nИтого: {ok - failures}/{ok} OK" + (f", FAIL: {failures}" if failures else ""))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

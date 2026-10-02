"""Смок-тест приватности истории режима управления (bot_instance:
_cc_hist_* — обёртка memory.add_message, маски кадра хода).

Проверяет: секрет из команды ввода в чувствительное поле не пишется в
историю (ни реплика пользователя, ни вопрос-подтверждение, ни «Готово,
ввёл …» после «да»), обычный ввод не трогается; текст, прочитанный с
приватной страницы, и отчёт «что на странице» приватной страницы — в
историю заглушкой, облачной модели не уходят; URL отчёта — без токенов;
вне хода и в следующем ходе маски не действуют.
Браузер, LLM и сеть подменены — живой Chrome/интернет не нужны.

Запуск: python -m scripts.test_cc_history_privacy
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="cc_hist_priv_"))
    os.environ["DATA_DIR"] = str(tmp / "data")
    os.environ["VPC_DATA_DIR"] = str(tmp / "data")
    ok = 0
    fails = []

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        if cond:
            ok += 1
        else:
            fails.append(name)

    from app.features import flavor_text
    from app.features.computer_control import (ComputerControlManager,
                                               normalize_command)
    from app.bot_instance import BotInstance

    # flavor-реплика без банка ушла бы в живой google — шаблон
    flavor_text.cc_reply = lambda *a, **kw: None

    SECRET = "Kotik2019!"

    class _CC(ComputerControlManager):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.executed = []
            self.type_act = None
            self.read_text = ""
            self.pv = None

        def resolve_type(self, body, site_word, router=None, chat_id=""):
            return dict(self.type_act), None

        def execute(self, action, chat_id="", router=None):
            self.executed.append(dict(action))
            if action["kind"] == "read":
                return True, self.read_text
            return True, ""

        def page_view_report(self, site_word=None, chat_id="", full_page=False):
            return dict(self.pv), None

    class _Mem:
        def __init__(self):
            self.log = []
            self.kw = []
            self.stm = SimpleNamespace(get_last=lambda *a, **kw: [],
                                       get_messages=lambda *a, **kw: [])

        def add_message(self, role, content, *a, **kw):
            self.log.append((role, content))
            self.kw.append((a, kw))

    def mkbot(confirm=True):
        b = BotInstance.__new__(BotInstance)
        b.persona_name = "connor"
        b.trigger_words = ["коннор"]
        b.persona = SimpleNamespace(
            persona_data={"name": "Connor"}, system_prompt="You are Connor.",
            get_settings=lambda: {"max_tokens": 400, "top_p": 0.9})
        b.context = f"hist_{os.urandom(3).hex()}"
        b.router = None
        b.proactive = None
        b.task_agent = None
        b.memory = _Mem()
        b._pending_photos = {}
        b._pending_more_photos = {}
        b.computer_control = _CC(
            context=b.context,
            config={"confirm": confirm, "click": True,
                    "sites": {"сбер": "online.sberbank.ru"}},
            base_dir=tmp / f"b{os.urandom(3).hex()}")
        b._cc_hist_install()
        return b

    def turn(b, text, chat="c"):
        with b.user_turn(chat):
            return b._cc_fast_path(normalize_command(text, b._address_names()),
                                   text, "u", chat, "U", "ru")

    def hist(b):
        return "\n".join(c for _r, c in b.memory.log)

    # ── 1. Ввод пароля: команда уходит в pending ──
    print("история: секрет ввода")
    b = mkbot()
    b.computer_control.type_act = {
        "kind": "type", "idx": 3, "text": SECRET, "element": "Пароль",
        "host": "example.com", "field_sensitive": True}
    cmd = f"введи {SECRET} в поле пароль"
    r = turn(b, cmd)
    check("pending: человеку вопрос с введённым текстом", SECRET in (r or ""))
    check("pending: в историю записаны user + assistant",
          [x[0] for x in b.memory.log] == ["user", "assistant"])
    check("pending: секрета нет ни в реплике, ни в вопросе",
          SECRET not in hist(b))
    check("pending: реплика пользователя — маской, остальное как было",
          b.memory.log[0][1] == f"введи ***({len(SECRET)}) в поле пароль")
    check("pending: вопрос-подтверждение — маской",
          f"***({len(SECRET)})" in b.memory.log[1][1])

    # «да»: pending-блок исполняет через execute + _cc_reply
    b.memory.log.clear()
    pend = b.computer_control.get_pending("c")
    with b.user_turn("c"):
        b.computer_control.clear_pending("c")
        b.computer_control.execute(pend, "c")
        rep = b._cc_reply(pend, True, "", "Готово, " +
                          b.computer_control.describe_done(pend))
        b.memory.add_message("user", "да", "u", "c", "U")
        b.memory.add_message("assistant", rep, "u", "c")
    check("«да»: человеку «Готово, ввёл …» с текстом", SECRET in rep)
    check("«да»: в истории «Готово» без секрета",
          SECRET not in hist(b) and "Готово" in hist(b))

    # Следующий ход: маски кадра не переживают ход
    b.memory.log.clear()
    with b.user_turn("c"):
        b.memory.add_message("user", "а kotik — хорошее имя", "u", "c")
    check("следующий ход: маски прошлого хода не действуют",
          b.memory.log[-1][1] == "а kotik — хорошее имя")

    # ── 2. Ввод без confirm по подписи «Пароль» (флага поля нет) ──
    b = mkbot(confirm=False)
    b.computer_control.type_act = {
        "kind": "type", "idx": 3, "text": SECRET, "element": "Пароль",
        "host": "example.com"}
    r = turn(b, f"введи «{SECRET}» в поле пароль")
    check("сразу исполнено: ввод ушёл в браузер",
          any(a.get("kind") == "type" for a in b.computer_control.executed))
    check("сразу исполнено: секрет не в истории", SECRET not in hist(b)
          and "«***(10)»" in b.memory.log[0][1])

    # Секретоподобное значение в обычном поле (email) — тоже маской
    b = mkbot(confirm=False)
    b.computer_control.type_act = {
        "kind": "type", "idx": 1, "text": "ivan.petrov@mail.ru",
        "element": "Поле", "host": "example.com"}
    turn(b, "введи Ivan.Petrov@mail.ru в поле")
    check("email в поле: маской без учёта регистра",
          "petrov" not in hist(b).lower())

    # Обычный ввод (поиск) — как есть
    b = mkbot(confirm=False)
    b.computer_control.type_act = {
        "kind": "type", "idx": 1, "text": "котики", "element": "Поиск",
        "host": "example.com", "field_safe": True}
    turn(b, "введи котики в поиск")
    check("поиск: обычный текст в истории не тронут",
          b.memory.log[0][1] == "введи котики в поиск"
          and "котики" in b.memory.log[1][1])

    # ── 3. Чтение приватной страницы ──
    print("история: приватные страницы")
    page = "Баланс 1 234 567 ₽. Последний перевод: Иван П., 5 000 ₽"
    b = mkbot(confirm=False)
    b.computer_control.read_text = page
    b._cc_ladder = lambda *a, **kw: {"action": {
        "kind": "read", "host": "online.sberbank.ru", "mode": "last"},
        "direct": True}
    r = turn(b, "прочитай последнее сообщение")
    check("приватное чтение: человек получил текст", r == page)
    check("приватное чтение: текста нет в истории", "Баланс" not in hist(b)
          and "Иван" not in hist(b))
    check("приватное чтение: заглушка с длиной и хостом",
          b.memory.log[1][1] == f"[прочитано {len(page)} символов с "
                                f"приватной страницы online.sberbank.ru]")

    # Та же вкладка ушла на /login обычного хоста — приватно по адресу
    b = mkbot(confirm=False)
    b.computer_control.read_text = page
    b.computer_control._last_url = "https://mail.example.com/login?next=/"
    b._cc_ladder = lambda *a, **kw: {"action": {
        "kind": "read", "host": "mail.example.com", "mode": "page"},
        "direct": True}
    turn(b, "прочитай страницу")
    check("приватное чтение по пути вкладки (/login): заглушка",
          "Баланс" not in hist(b) and "приватной страницы" in hist(b))

    # Обычная страница — прочитанное в истории как есть
    b = mkbot(confirm=False)
    b.computer_control.read_text = "Привет, как дела?"
    b.computer_control._last_url = "https://bank.example.org/"  # другая вкладка
    b._cc_ladder = lambda *a, **kw: {"action": {
        "kind": "read", "host": "news.example.com", "mode": "last"},
        "direct": True}
    turn(b, "прочитай последнее сообщение")
    check("обычное чтение: текст в истории (чужая вкладка не влияет)",
          b.memory.log[1][1] == "Привет, как дела?")

    # Чтение по «да» (pending-блок → _cc_reply)
    b = mkbot()
    act = {"kind": "read", "host": "online.sberbank.ru", "mode": "page"}
    with b.user_turn("c"):
        rep = b._cc_reply(act, True, page, "Готово.\n\n" + page)
        b.memory.add_message("user", "да", "u", "c")
        b.memory.add_message("assistant", rep, "u", "c")
    check("чтение по «да»: в истории заглушка", "Баланс" not in hist(b)
          and "приватной страницы online.sberbank.ru" in hist(b))

    # ── 4. «что на странице?» ──
    items = [{"idx": 1, "text": "Войти", "tag": "button"},
             {"idx": 2, "text": "Номер карты", "tag": "input"}]
    llm_calls = []

    class _Router:
        answer_provider = None

        def get_response(self, messages, **kw):
            llm_calls.append(messages)
            return "Вижу форму входа и кнопку."

    b = mkbot(confirm=False)
    b.router = _Router()
    b.computer_control.pv = {
        "url": "https://online.sberbank.ru/login", "host": "online.sberbank.ru",
        "items": items, "shot": None}
    b._cc_ladder = lambda *a, **kw: b._cc_page_view_reply(
        None, False, False, "c", "ru", "что на странице?")
    r = turn(b, "что на странице?")
    check("приватный обзор: облачная модель не вызывалась", not llm_calls)
    check("приватный обзор: человеку список элементов", "Номер карты" in (r or ""))
    check("приватный обзор: в истории заглушка",
          "Номер карты" not in hist(b)
          and "обзор приватной страницы online.sberbank.ru" in hist(b))

    # Обычная страница с токеном в адресе: ни в LLM, ни в истории токена нет
    b = mkbot(confirm=False)
    b.router = _Router()
    llm_calls.clear()
    b.computer_control.pv = {
        "url": "https://example.com/reset?token=SECRETTOKEN&code=123456",
        "host": "example.com",
        "items": [{"idx": 1, "text": "Пишите на ivan.petrov@mail.ru",
                   "tag": "a"}], "shot": None}
    b._cc_ladder = lambda *a, **kw: b._cc_page_view_reply(
        None, False, False, "c", "ru", "что на странице?")
    r = turn(b, "что на странице?")
    sent = str(llm_calls)
    check("обычный обзор: ответ персоны через LLM", len(llm_calls) == 1
          and r == "Вижу форму входа и кнопку.")
    check("обычный обзор: токен/код адреса не ушли в LLM",
          "SECRETTOKEN" not in sent and "123456" not in sent)
    check("обычный обзор: email из подписи в LLM — маской",
          "ivan.petrov@mail.ru" not in sent)

    # LLM молчит — шаблон с адресом без токена попадает в ответ и историю
    b = mkbot(confirm=False)
    b.computer_control.pv = dict(b.computer_control.pv or {}, **{
        "url": "https://example.com/reset?token=SECRETTOKEN&code=123456",
        "host": "example.com", "items": items, "shot": None})
    b._persona_page_view_reply = lambda *a, **kw: None
    b._cc_ladder = lambda *a, **kw: b._cc_page_view_reply(
        None, False, False, "c", "ru", "что на странице?")
    r = turn(b, "что на странице?")
    check("шаблон обзора: токена нет ни в ответе, ни в истории",
          "SECRETTOKEN" not in (r or "") and "SECRETTOKEN" not in hist(b)
          and "Номер карты" in hist(b))

    # ── 4б. Агент задач: ответ-секрет и ввод агента ──
    print("история: агент задач и маркеры")
    import json as _json
    from app.features import browser_actions as _ba
    from app.features.task_agent import TaskAgent
    _ba.wait_dom_idle = lambda *a, **k: None

    class _FormCC(_CC):
        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            return "https://shop.test/form", "shop.test", [
                {"idx": 5, "tag": "input", "role": "textbox", "text": "Пароль",
                 "ed": True, "sn": True},
                {"idx": 6, "tag": "input", "role": "textbox", "text": "Имя",
                 "ed": True}], 1, None

    class _AgentRouter:
        def __init__(self):
            self.replies = []

        def get_response(self, messages, **kw):
            if str(messages[-1]["content"]).startswith("You keep the brief"):
                # Разбор ответа по слотам брифа (отдельный вызов агента) —
                # без изменений, скрипт шагов не трогаем
                self.slot_prompts = getattr(self, "slot_prompts", []) + [
                    messages[-1]["content"]]
                return "{}"
            r = (self.replies.pop(0) if self.replies
                 else {"action": "done", "message": "ок"})
            return _json.dumps(r, ensure_ascii=False)

    def mkagent_bot():
        b = mkbot(confirm=False)
        b.reminder_manager = None
        b.computer_control = _FormCC(
            context=b.context, config={"confirm": False, "click": True},
            base_dir=tmp / f"a{os.urandom(3).hex()}")
        b.task_agent = TaskAgent(b.computer_control)
        b.router = _AgentRouter()
        return b

    def ta_turn(b, text, replies, goal=None):
        b.router.replies = list(replies)
        with b.user_turn("c"):
            return b._task_agent_turn(text, "u", "c", "U", "ru", goal=goal,
                                      feed_text=text)

    b = mkagent_bot()
    ta_turn(b, "задача: войди", [{"action": "ask",
                                  "question": "Какой пароль от аккаунта?"}],
            goal="войди")
    ta_turn(b, SECRET, [{"action": "type", "n": 1, "text": SECRET}])
    r = ta_turn(b, "да", [])
    check("агент: пароль введён в поле настоящим текстом",
          any(a.get("text") == SECRET for a in b.computer_control.executed))
    check("агент: человеку «Ввёл …» с текстом", SECRET in r)
    check("агент: ответ на вопрос о пароле в истории — маской",
          b.memory.log[2] == ("user", f"***({len(SECRET)})"))
    check("агент: ни вопрос-подтверждение, ни «Ввёл …» не несут пароль",
          SECRET not in hist(b) and "Ввёл «***(10)»" in hist(b))
    check("агент: разбор ответа по слотам (облако) — пароль плейсхолдером",
          getattr(b.router, "slot_prompts", None)
          and all(SECRET not in p for p in b.router.slot_prompts)
          and any("{{secret1}}" in p for p in b.router.slot_prompts))

    # H4: «бросить задачу?» — команда с паролем в STM маской
    b = mkagent_bot()
    b._control_mode = {"c"}
    b.computer_control.sites = {"почта": "https://mail.test/"}
    b.router.replies = [{"action": "ask", "question": "Какую пиццу?"}]
    with b.user_turn("c"):
        b._task_agent_turn("задача: закажи", "u", "c", "U", "ru",
                           goal="закажи", feed_text="задача: закажи")
    cmd_h4 = f"задача: войди в почту, пароль {SECRET}"
    with b.user_turn("c"):
        sw_h4 = b.task_agent.ask_switch("c", cmd_h4, user_id="u")
        b._cc_hist_note_user_text(cmd_h4, contacts=False)
        b.memory.add_message("user", cmd_h4, "u", "c", "U")
        b.memory.add_message("assistant", sw_h4, "u", "c")
    check("H4: «бросить задачу?» — пароль команды не в STM и не в вопросе",
          SECRET not in hist(b) and SECRET not in sw_h4)

    # E8: веб-клиент API без chat_id — прогон и память по user_id, два
    # клиента не делят один прогон «None»
    b = mkagent_bot()
    b.router.replies = [{"action": "ask", "question": "Какой сайт?"}]
    with b.user_turn("u1"):
        b._task_agent_turn("задача: закажи", "u1", None, "U", "ru",
                           goal="закажи", feed_text="задача: закажи")
    b.router.replies = [{"action": "ask", "question": "Какой город?"}]
    with b.user_turn("u2"):
        b._task_agent_turn("задача: найди", "u2", None, "U", "ru",
                           goal="найди", feed_text="задача: найди")
    check("E8: без chat_id — у каждого клиента свой прогон (ключ user_id)",
          set(b.task_agent._runs) >= {"u1", "u2"}
          and "None" not in b.task_agent._runs
          and b.task_agent._runs["u1"]["goal"] == "закажи")

    # Вопрос не о секрете, ответ — обычное имя: как есть; отмена — как есть
    b = mkagent_bot()
    ta_turn(b, "задача: запиши", [{"action": "ask",
                                   "question": "Какую пиццу взять?"}],
            goal="запиши")
    ta_turn(b, "Маргариту", [{"action": "type", "n": 2, "text": "Маргарита"}])
    check("агент: обычный ответ и обычный ввод — как есть",
          ("user", "Маргариту") in b.memory.log
          and "Маргарита" in hist(b).replace("Маргариту", ""))
    b = mkagent_bot()
    ta_turn(b, "задача: войди", [{"action": "ask",
                                  "question": "Какой пароль?"}], goal="войди")
    ta_turn(b, "отмена", [])
    check("агент: «отмена» на вопрос о пароле — не маскируется",
          ("user", "отмена") in b.memory.log)
    # email/телефон в ответе на несекретный вопрос — словом
    b = mkagent_bot()
    ta_turn(b, "задача: закажи", [{"action": "ask",
                                   "question": "Куда прислать чек?"}],
            goal="закажи")
    ta_turn(b, "на ivan.petrov@mail.ru или +7 913 123-45-67", [])
    check("агент: email и телефон в ответе — маской, остальное как есть",
          "petrov" not in hist(b) and "123-45" not in hist(b)
          and "на ***(" in hist(b) and " или ***(" in hist(b))

    # Путь маркеров: реплика пишется ДО pending ввода (process_markers)
    b = mkbot()
    cmd = f"зайди на сайт и введи пароль {SECRET} в поле пароль"
    with b.user_turn("c"):
        b._cc_hist_note_user_text(cmd)
        b.memory.add_message("user", cmd, "u", "c")
        b.computer_control.set_pending("c", {
            "kind": "type", "idx": 5, "text": SECRET, "element": "Пароль",
            "field_sensitive": True, "host": "example.com"})
        b.memory.add_message("assistant", f"Ввести «{SECRET}» в поле «Пароль»?",
                             "u", "c")
    check("маркер: реплика пользователя до pending — пароль маской целиком",
          b.memory.log[0][1] == "зайди на сайт и введи пароль ***(10) в поле "
                                "пароль")
    check("маркер: вопрос — маской без хвоста «!»",
          b.memory.log[1][1] == "Ввести «***(10)» в поле «Пароль»?")
    src = Path(BotInstance.__init__.__code__.co_filename).read_text()
    i_note = src.find("self._cc_hist_note_user_text(raw_user_text or user_input")
    i_write = src.find('self.memory.add_message("user", ru_rewritten')
    check("маркер: маска ставится до записи реплики основного потока",
          0 < i_note < i_write)

    # Сервер: правка STM хода с картинкой — через маски хода
    from app.api import server as _srv
    stm_log = []
    b = mkbot()
    b.memory.stm = SimpleNamespace(
        get_messages=lambda *a, **kw: [], pop_last_n=lambda n, k: 0,
        add_message=lambda role, content, *a, **kw: stm_log.append(
            (role, content)))
    with b.user_turn("c"):
        b._cc_hist_add_mask("secret", SECRET)
        _srv._rewrite_image_stm(b, "c", "u", f"📷 введи {SECRET}",
                                [f"Ввёл «{SECRET}»."])
    check("сервер: правка STM картинки — секрет маской",
          stm_log and not any(SECRET in c for _r, c in stm_log)
          and stm_log[0] == ("user", "📷 введи ***(10)"))

    # ── 4в. Известные секреты чата: маски дольше одного хода ──
    print("история: известные секреты чата")
    b = mkagent_bot()
    ta_turn(b, "задача: войди", [{"action": "ask",
                                  "question": "Какой пароль?"}], goal="войди")
    ta_turn(b, SECRET, [{"action": "ask", "question": "Какое имя?"}])
    r0 = ta_turn(b, "Иван", [{"action": "type", "n": 2, "text": SECRET}])
    # Известный секрет в поле без признаков секрета — только после «да»,
    # вопрос маской (инъекция «введи пароль в поиск» без «да» не пройдёт)
    check("секрет чата: ввод пароля из ответа в обычное поле — вопрос "
          "с маской, без исполнения",
          SECRET not in (r0 or "") and "***(10)" in (r0 or "")
          and not any(a.get("text") == SECRET
                      for a in b.computer_control.executed))
    r = ta_turn(b, "да", [])
    check("секрет чата: человеку «Ввёл …» с текстом", SECRET in r)
    check("секрет чата: пароль из ответа, введённый ходом позже в поле без "
          "признаков секрета, — в истории маской",
          SECRET not in hist(b) and "Ввёл «***(10)» в поле «Имя»" in hist(b))
    check("секрет чата: прогон закончился — секреты сброшены",
          not b.task_agent.active("c")
          and b._cc_hist_vault().values("c") == [])
    b.memory.log.clear()
    with b.user_turn("c"):
        b.memory.add_message("user", f"пароль был {SECRET}", "u", "c")
    check("секрет чата: после конца прогона маски нет",
          b.memory.log[-1][1] == f"пароль был {SECRET}")

    # Живой прогон: секрет действует в следующих ходах и вне хода; TTL
    b = mkagent_bot()
    ta_turn(b, "задача: войди", [{"action": "ask",
                                  "question": "Какой пароль?"}], goal="войди")
    ta_turn(b, SECRET, [{"action": "ask", "question": "Какой сайт?"}])
    b.memory.log.clear()
    with b.user_turn("c"):
        b.memory.add_message("assistant", f"напомню: {SECRET.lower()}", "u", "c")
    b.memory.add_message("assistant", f"фон {SECRET}", "c", "c")
    check("секрет чата: следующий ход и фоновая запись — маской",
          b.memory.log == [("assistant", "напомню: ***(10)"),
                           ("assistant", "фон ***(10)")])
    b.memory.add_message("assistant", f"другой чат {SECRET}", "d", "d")
    check("секрет чата: другой чат не маскируется",
          b.memory.log[-1][1] == f"другой чат {SECRET}")
    vault = b._cc_hist_vault()
    vault._by_chat["c"] = {k: 0.0 for k in vault._by_chat.get("c", {})}
    b.memory.add_message("assistant", f"фон {SECRET}", "c", "c")
    check("секрет чата: по TTL снимается",
          b.memory.log[-1][1] == f"фон {SECRET}" and vault.values("c") == [])

    # «да»/«отмена» на вопрос о пароле — в секреты чата не попадают
    b = mkagent_bot()
    ta_turn(b, "задача: войди", [{"action": "ask",
                                  "question": "Пароль вводить?"}], goal="войди")
    ta_turn(b, "да", [{"action": "ask", "question": "Какой сайт?"}])
    check("секрет чата: «да» на вопрос о пароле — не секрет чата",
          b._cc_hist_vault().values("c") == [])

    # Путь маркеров: логин без слова «логин/пароль» в реплике → после
    # маркера ввода в поле «Логин» реплика хода переписывается маской
    class _StmMem:
        def __init__(self):
            self.buf = []
            self.log = self.buf
            self.pops = []
            mem = self

            class _Stm:
                def get_messages(self, chat_id=None, **kw):
                    return list(mem.buf)

                def get_last(self, n, **kw):
                    return list(mem.buf[-n:])

                def pop_last_n(self, n, key):
                    mem.pops.append((n, key))
                    for _ in range(min(n, len(mem.buf))):
                        mem.buf.pop()
                    return n

                def add_message(self, role, content, user_id="default",
                                chat_id=None, user_name=None):
                    mem.buf.append({"role": role, "content": content,
                                    "sender_id": user_id,
                                    "user_name": user_name})
            self.stm = _Stm()

        def add_message(self, role, content, user_id="default", chat_id=None,
                        user_name=None, **kw):
            self.stm.add_message(role, content, user_id, chat_id, user_name)

    def mk_stm_bot():
        b = mkbot()
        b.memory = _StmMem()
        b._cc_hist_install()
        return b

    b = mk_stm_bot()
    b.memory.add_message("user", "меня зовут ghost_user", "u", "c", "U")
    say = "зайди и впиши ghost_user, почта ivan@mail.ru"
    with b.user_turn("c"):
        b._cc_hist_note_user_text(say, contacts=False)
        b.memory.add_message("user", say, "u", "c", "U")
        written = b.memory.buf[-1]["content"]
        b.computer_control.set_pending("c", {
            "kind": "type", "idx": 3, "text": "ghost_user", "element": "Логин",
            "host": "x.test"})
        b._cc_hist_after_markers("c", "u")
        b.memory.add_message("assistant", "Ввести «ghost_user» в поле «Логин»?",
                             "u", "c")
    cont = [m["content"] for m in b.memory.buf]
    check("маркер-логин: до маркера реплика записана как есть", written == say)
    check("маркер-логин: реплика хода переписана маской, прошлый ход не тронут",
          cont == ["меня зовут ghost_user",
                   "зайди и впиши ***(10), почта ivan@mail.ru",
                   "Ввести «***(10)» в поле «Логин»?"]
          and b.memory.pops == [(1, "c")])
    check("маркер-логин: автор и имя в переписанной записи сохранены",
          b.memory.buf[1]["sender_id"] == "u"
          and b.memory.buf[1]["user_name"] == "U")
    # Маркер ввёл email в поле почты — email в реплике маской
    b = mk_stm_bot()
    with b.user_turn("c"):
        b._cc_hist_note_user_text(say, contacts=False)
        b.memory.add_message("user", say, "u", "c", "U")
        b.computer_control.set_pending("c", {
            "kind": "type", "idx": 4, "text": "ivan@mail.ru", "element": "Поле",
            "host": "x.test"})
        b._cc_hist_after_markers("c", "u")
    check("маркер-email: email, введённый в поле, — маской в реплике хода",
          b.memory.buf[-1]["content"] == "зайди и впиши ghost_user, почта ***(12)")
    # Маркер без ввода — реплика не переписывается
    b = mk_stm_bot()
    with b.user_turn("c"):
        b.memory.add_message("user", say, "u", "c", "U")
        b.computer_control.set_pending("c", {
            "kind": "click", "idx": 4, "element": "Войти", "host": "x.test"})
        b._cc_hist_after_markers("c", "u")
    check("маркер-клик: реплика не переписывается", b.memory.pops == []
          and b.memory.buf[-1]["content"] == say)
    i_after = src.find("self._cc_hist_after_markers(chat_id, user_id)")
    i_markers = src.find("answer, cc_notices = self.computer_control.process_markers(")
    check("маркер: правка реплики стоит сразу после process_markers",
          0 < i_markers < i_after < i_markers + 600)

    # Пережим: email/телефон обычной реплики в режиме управления — как есть
    b = mkbot()
    chat_say = "мой email ivan@mail.ru, телефон +7 913 123-45-67, запомни"
    with b.user_turn("c"):
        b._cc_hist_note_user_text(chat_say, contacts=False)
        b.memory.add_message("user", chat_say, "u", "c")
        b.memory.add_message("assistant", "Запомнил ivan@mail.ru", "u", "c")
    check("обычная реплика: email/телефон в истории как есть",
          b.memory.log == [("user", chat_say),
                           ("assistant", "Запомнил ivan@mail.ru")])
    b = mkbot()
    tok_say = f"пароль {SECRET}, карта 4111 1111 1111 1111"
    with b.user_turn("c"):
        b._cc_hist_note_user_text(tok_say, contacts=False)
        b.memory.add_message("user", tok_say, "u", "c")
    check("обычная реплика: пароль и карта — по-прежнему маской",
          SECRET not in hist(b) and "4111" not in hist(b))

    # ── 5. Обёртка и маска ──
    print("обёртка memory.add_message")
    b = mkbot()
    before = b.memory.add_message
    b._cc_hist_install()
    check("установка идемпотентна", b.memory.add_message is before)
    b.memory.add_message("assistant", f"фон {SECRET}", user_id="c",
                         chat_id="c")
    check("вне хода: запись как есть, kwargs переданы",
          b.memory.log[-1][1] == f"фон {SECRET}"
          and b.memory.kw[-1][1] == {} and b.memory.kw[-1][0][:2] == ("c", "c"))
    with b.user_turn("c"):
        b._cc_hist_add_mask("secret", "1234")
        b.memory.add_message("user", "пин 1234, заказ 12345", "u", "c", "U",
                             light_mode=True)
    check("короткий секрет — только отдельным словом",
          b.memory.log[-1][1] == "пин ***(4), заказ 12345")
    check("user_name/light_mode доходят до памяти",
          b.memory.kw[-1] == (("u", "c", "U"), {"light_mode": True}))
    long_s = "A1b2C3d4" * 8  # 64 символа
    m = BotInstance._cc_mask_secret(
        f"ввёл «{long_s[:40]}» / «{long_s[:199]}…» / {long_s.lower()}", long_s)
    check("длинный секрет: голова 40 и полный — маской",
          "A1b2C3d4" not in m and "a1b2c3d4" not in m and m.count("***(64)") == 3)
    check("в __init__ обёртка ставится",
          "self._cc_hist_install()" in Path(
              BotInstance.__init__.__code__.co_filename).read_text())

    print(f"\n{ok} OK, {len(fails)} FAIL")
    for f in fails:
        print(f"  [FAIL] {f}")


if __name__ == "__main__":
    main()

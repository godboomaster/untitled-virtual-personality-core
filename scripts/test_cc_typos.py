"""Опечатки в командах режима управления и честный ответ вне режима.

Проверяет: правку слова (cc_typos: Дамерау, допуск по длине, «другая форма
глагола», ничья, раскладка, латиница в русском слове); фразу режима с
опечаткой и в чужой раскладке (parse_control_mode) и то, что посторонние
фразы режимом не становятся; команды в режиме с опечаткой в глаголе — без
LLM-яруса, безопасные сразу, остальные с «да», «открой» после правки — только
к известной цели; вне режима — сразу «режим выключен, включить и выполнить?»,
«да» включает и исполняет, «нет» / другая реплика / чужой участник / скин /
команда телефону — как раньше; отметка «ждём да/нет» для группы Telegram и
кнопки веба. Полный ход — через process_message; браузер и LLM подменены.

Запуск: python -m scripts.test_cc_typos
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cc_typos_")
    os.environ["VPC_DATA_DIR"] = os.environ["DATA_DIR"]  # одна папка данных на всё
    tmp = Path(tempfile.mkdtemp(prefix="cc_typos_data_"))
    import app.core.router as _net_router
    _net_router.internet_available = lambda: True
    _net_router._net_ok, _net_router._net_checked = True, float("inf")

    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        if cond:
            ok += 1
        else:
            fail += 1

    from app.features import cc_typos as ct
    from app.features.computer_control import ComputerControlManager, parse_control_mode

    # ── A. Правка слова ──
    print("A. правка слова")
    check("перестановка соседних букв — одна ошибка",
          ct._osa("урпавления", "управления", 2) == 1 and ct._osa("opne", "open", 1) == 1)
    check("отсечка: дальше допуска — допуск + 1", ct._osa("привет", "открой", 1) == 2)
    check("короткое слово: замена буквы не правится (fine ≠ find, plan ≠ play)",
          ct.nearest("fine", ct.CMD_VERBS) is None and ct.nearest("plan", ct.CMD_VERBS) is None)
    check("короткое слово: пропуск и перестановка правятся (clik, opne)",
          ct.nearest("clik", ct.CMD_VERBS) == "click" and ct.nearest("opne", ct.CMD_VERBS) == "open")
    check("до 3 букв не правим", ct.nearest("ткн", ct.CMD_VERBS) is None)
    check("другая форма глагола — не опечатка (найду, открою, включил, пришла, saved)",
          all(ct.nearest(w, ct.CMD_VERBS) is None
              for w in ("найду", "открою", "включил", "пришла", "saved", "typed")))
    check("ничья между разным смыслом — не правим (вылючи: включи/выключи)",
          ct.nearest("вылючи", ct.CMD_VERBS, ct._CMD_GROUPS) is None)
    check("раскладка: латиница → кириллица и обратно",
          ct.switch_layout("jnrhjq .ne,") == "открой ютуб"
          and ct.switch_layout("щзут нщгегиу") == "open youtube"
          and ct.switch_layout("open ютуб") is None)
    check("раскладка целиком: только если начинается с глагола-команды",
          ct.layout_command("jnrhjq .ne,") == "открой ютуб"
          and ct.layout_command("open youtube") is None
          and ct.layout_command("ghbdtn") is None
          and ct.layout_command("привет") is None)

    fixes = {
        "откой ютуб": "открой ютуб",
        "нажим на кнопку войти": "нажми на кнопку войти",
        "пролистй вниз": "пролистай вниз",
        "прокрутт вниз": "прокрути вниз",
        "навеид на меню": "наведи на меню",
        "закрй вкладку": "закрой вкладку",
        "opne youtube": "open youtube",
        "clik login": "click login",
        "jnrhjq .ne,": "открой ютуб",
        "щзут нщгегиу": "open youtube",
        "оtкрой ютуб": "открой ютуб",
    }
    for src, want in fixes.items():
        check(f"«{src}» → «{want}»", ct.fix_command_verb(src) == want)
    for src in ("открой ютуб", "найду сам", "открою позже", "включил свет", "fine thanks",
                "plan a trip", "привет", "как дела", "смотри какая погода", "верно",
                "вылючи звук", "пришла домой", "typed it", "lunch time", ""):
        check(f"«{src}» — без правки", ct.fix_command_verb(src) is None)

    # ── B. Фраза режима ──
    print("\nB. фраза режима управления")
    for src, want in (("включи режим урпавления", True), ("режим упраления", True),
                      ("Перейди в режим управлния!", True), ("рeжим управления", True),
                      ("выйди из режима упрвления", False), ("выключи режим упраления", False),
                      ("drk.xb ht;bv eghfdktybz", True), ("ht;bv eghfdktybz", True),
                      ("enter contorl mode", True), ("exit control mdoe", False),
                      ("режим управления", True), ("выйди из режима управления", False)):
        check(f"«{src}» → {want}", parse_control_mode(src) is want)
    for src in ("вылючи режим управления", "режим сна", "включи режим экономии",
                "режим уравнения", "управление", "какой режим управления у тебя?",
                "включи режим управления и открой ютуб пожалуйста прямо сейчас быстро"):
        check(f"«{src}» — не про режим", parse_control_mode(src) is None)

    # ── C. Бот: полный ход через process_message ──
    print("\nC. бот: опечатки в режиме и переспрос вне режима")
    from app.bot_instance import BotInstance
    from app.features import browser_actions as ba
    from app.features import cc_texts
    from app.features.conversation_style import ConversationStyleConfig
    from app.features.list_offers import ListOffers
    import app.features.flavor_text as _flavor
    from types import SimpleNamespace
    ba.set_control_mode = lambda *a, **kw: None
    _flavor.cc_reply = lambda *a, **kw: None

    class _CC(ComputerControlManager):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.executed, self.clicks, self.llm_calls = [], [], []

        def _first_result_url(self, *a, **kw):
            return None

        def execute(self, action, chat_id="", router=None):
            self.executed.append(dict(action))
            return True, "ok"

        def resolve_click(self, goal, site_word, router=None, chat_id=""):
            self.clicks.append(goal)
            return {"kind": "click", "goal": goal, "element": goal, "host": "x.ru"}, None

        def resolve_hover(self, goal, site_word, router=None, chat_id=""):
            return {"kind": "hover", "goal": goal, "element": goal, "host": "x.ru"}, None

        def resolve_intent_llm(self, text, router, chat_id=""):
            self.llm_calls.append(text)
            return None, None

    class _Persona:
        def __init__(self):
            self.settings, self.persona_data = {}, {"name": "Connor"}

        def prepare_messages(self, *a, **kw):
            return [{"role": "system", "content": "SYS"}]

        def get_settings(self):
            return {"max_tokens": 500}

    class _Memory:
        class _STM:
            def get_last(self, *a, **kw):
                return []

        class _LTM:
            def get_facts_by_category(self, *a, **kw):
                return []

            def save_facts(self, *a, **kw):
                pass

        def __init__(self):
            self.stm, self.ltm = self._STM(), self._LTM()
            self.log = []

        def add_message(self, role, content, *a, **kw):
            self.log.append((role, content))

        def get_context(self, *a, **kw):
            return [], [], []

        def get_chat_facts_block(self, *a, **kw):
            return None

    class _Router:
        def __init__(self):
            self.calls = 0
            self.answer_provider, self._last_provider = None, "fake"

        def is_local_primary(self):
            return False

        def supports_vision(self):
            return False

        def get_response(self, messages, **kw):
            self.calls += 1
            return "Обычный ответ."

    CHAT = "g1"
    CFG = {"confirm": False, "click": True,
           "sites": {"ютуб": "youtube.com", "додо": "dodopizza.ru"},
           "search": {"ютуб": {"url": "https://www.youtube.com/results?search_query={q}"}}}

    def bot(mode_on=False):
        b = BotInstance.__new__(BotInstance)
        b.persona_name = "typos"
        b.context = f"typos_{time.time_ns()}"
        b.owner = "A"
        b.web_single_user = False
        b._cc_allowed_users = set()
        b.features = {"file_access": False}
        b.trigger_words = ["коннор"]
        b.intellect = SimpleNamespace(active=False)
        b.conversation_style = ConversationStyleConfig(None)
        b._control_mode = {CHAT} if mode_on else set()
        b.computer_control = _CC(context=b.context, config=dict(CFG),
                                 base_dir=tmp / b.context)
        b.scenario_manager = None
        b.task_agent = None
        b.proactive = None
        b.addons = []
        b.self_memory = None
        b.living = None
        b.todo_manager = b.inventory_manager = b.reminder_manager = None
        b.learning_manager = None
        b.file_db = None
        b._punish_enabled = b._moderation_enabled = False
        b._web_search_enabled = False
        b._web_search_disabled_chats = set()
        b._pending_list_messages, b._pending_split_messages = {}, {}
        b._pending_photos, b._pending_question_kind = {}, {}
        b._pending_more_photos = {}
        b.persona = _Persona()
        b.memory = _Memory()
        b.router = _Router()
        b.list_offers = ListOffers()
        return b

    def say(b, text, user="A", **kw):
        return b.process_message(text, user_id=user, chat_id=CHAT, **kw) or ""

    # Фраза режима с опечаткой — переключение без модели
    b = bot()
    r = say(b, "включи режим урпавления")
    check("«включи режим урпавления» — режим включён, модель не звали",
          b.control_mode_on(CHAT) and b.router.calls == 0 and "Режим управления" in r)
    r = say(b, "выйди из режима упраления")
    check("«выйди из режима упраления» — выключен", not b.control_mode_on(CHAT)
          and b.router.calls == 0)
    r = say(b, "ht;bv eghfdktybz")
    check("«ht;bv eghfdktybz» (раскладка) — режим включён, ответ по-русски",
          b.control_mode_on(CHAT) and "Режим управления" in r)

    # В режиме: опечатка в «открой» — исполнено без LLM-яруса
    b = bot(mode_on=True)
    r = say(b, "Коннор, откой ютуб")
    ex = b.computer_control.executed
    check("в режиме: «откой ютуб» — YouTube открыт сразу, без модели и LLM-яруса",
          len(ex) == 1 and "youtube.com" in str(ex[0].get("value"))
          and not b.computer_control.llm_calls and b.router.calls == 0
          and r.startswith("Готово"))
    b = bot(mode_on=True)
    say(b, "jnrhjq .ne,")
    check("в режиме: «jnrhjq .ne,» (раскладка) — YouTube открыт",
          len(b.computer_control.executed) == 1
          and "youtube.com" in str(b.computer_control.executed[0].get("value")))
    b = bot(mode_on=True)
    say(b, "навеид на меню")
    ex = b.computer_control.executed
    check("в режиме: «навеид на меню» — наведение сразу (безопасное)",
          len(ex) == 1 and ex[0].get("kind") == "hover")

    # Клик после правки — только с «да»
    b = bot(mode_on=True)
    r = say(b, "нажим на кнопку войти")
    p = b.computer_control.get_pending(CHAT)
    check("в режиме: «нажим на кнопку войти» — клик не исполнен, спрошено «да»",
          not b.computer_control.executed and p and p.get("kind") == "click"
          and "?" in r and not b.computer_control.llm_calls)
    say(b, "да")
    check("…«да» — клик исполнен", len(b.computer_control.executed) == 1
          and b.computer_control.executed[0].get("kind") == "click")

    # «Открой» после правки — только известная цель
    b = bot(mode_on=True)
    check("правка «откой котиков» не принята: цель неизвестна, поисковик не трогаем",
          b._cc_typo_candidate("откой котиков") is None)
    check("правка «launhc youtube» принята: youtube — известный сайт",
          b._cc_typo_candidate("launhc youtube") == "launch youtube")
    check("«найди котиков на ютубе» с опечаткой — поиск на известном сайте",
          b._cc_typo_candidate("найид котиков на ютубе") == "найди котиков на ютубе")
    say(b, "найду сам")
    check("в режиме: «найду сам» — ничего не исполнено", not b.computer_control.executed)

    # Вне режима: «открой ютуб» — сразу честный вопрос
    b = bot()
    stamp0 = b.cc_pending_stamp(CHAT)
    r = say(b, "открой ютуб")
    check("вне режима: «открой ютуб» — «режим выключен, включить и выполнить?», без модели",
          r == cc_texts.t("cc_mode_offer", "ru", cmd="открой ютуб")
          and b.router.calls == 0 and not b.computer_control.executed
          and not b.control_mode_on(CHAT))
    stamp1 = b.cc_pending_stamp(CHAT)
    check("отметка «ждём да/нет» появилась (подсказка reply-ем в группе Telegram)",
          stamp0 is None and stamp1 and stamp1[0] == "offer")
    check("веб: кнопки «да/нет» у вопроса",
          (b.cc_answer_options(CHAT, "A") or {}).get("kind") == "yesno"
          and b.cc_answer_options(CHAT, "B") is None)
    r = say(b, "да")
    ex = b.computer_control.executed
    check("«да» — режим включён и YouTube открыт этим же ходом",
          b.control_mode_on(CHAT) and len(ex) == 1
          and "youtube.com" in str(ex[0].get("value"))
          and r.startswith(cc_texts.t("cc_mode_on_short", "ru")) and "Готово" in r
          and b.router.calls == 0)
    check("история: вопрос и ответ записаны по разу",
          [x[0] for x in b.memory.log] == ["user", "assistant", "user", "assistant"]
          and b.memory.log[-1][1] == r)
    check("после «да» вопроса больше нет", b.cc_pending_stamp(CHAT) is None)

    # Опечатка вне режима — вопрос уже с исправленной командой
    b = bot()
    r = say(b, "откой ютуб")
    check("вне режима: «откой ютуб» — вопрос про «открой ютуб»",
          "«открой ютуб»" in r and b.router.calls == 0)
    b2 = bot()
    r2 = say(b2, "jnrhjq .ne,")
    check("вне режима: «jnrhjq .ne,» — вопрос про «открой ютуб»",
          "«открой ютуб»" in r2 and b2.router.calls == 0)
    r = say(b, "не надо")
    check("«не надо» — режим не включён, короткий отказ",
          not b.control_mode_on(CHAT) and not b.computer_control.executed
          and r == cc_texts.t("cc_mode_offer_declined", "ru"))

    b = bot()
    say(b, "нажми на кнопку войти")
    r = say(b, "расскажи анекдот")
    check("другая реплика после вопроса — обычный ответ, вопрос снят",
          r == "Обычный ответ." and not b.control_mode_on(CHAT)
          and b.cc_pending_stamp(CHAT) is None)
    say(b, "да")
    check("…и позднее «да» уже ничего не включает", not b.control_mode_on(CHAT)
          and not b.computer_control.executed)

    b = bot()
    say(b, "открой ютуб")
    b.list_offers._offers[CHAT]["asked_at"] -= b._CC_OFFER_TTL_SEC + 1
    say(b, "да")
    check("«да» через 3+ минуты — не ответ, режим не включён",
          not b.control_mode_on(CHAT) and not b.computer_control.executed)

    # Не перехватываем: обычные фразы, чужой участник, скин, команда телефону
    for text in ("открой глаза", "стоп", "найди рецепт борща", "привет", "включил свет"):
        b = bot()
        r = say(b, text)
        check(f"вне режима: «{text}» — обычный ответ", r == "Обычный ответ."
              and b.list_offers.peek(CHAT) is None)
    b = bot()
    r = say(b, "открой ютуб", user="B")
    check("чужой участник (не владелец): «открой ютуб» — обычный ответ",
          r == "Обычный ответ." and b.list_offers.peek(CHAT) is None)
    b = bot()
    r = say(b, "открой ютуб", from_skin=True)
    check("реплика из скина: вопроса нет", b.list_offers.peek(CHAT) is None)
    b = bot()
    r = say(b, "открой ютуб", turn_note="The phone has just opened YouTube.")
    check("команда телефону (телефон уже открыл): вопроса нет",
          b.list_offers.peek(CHAT) is None and not b.computer_control.executed)
    b = bot()
    b.computer_control = None
    r = say(b, "открой ютуб")
    check("управление компьютером выключено — обычный ответ", r == "Обычный ответ.")

    # Обычный переспрос про дела не путается с этим вопросом
    b = bot()
    check("_list_offer_turn не берёт вопрос режима",
          b._list_offer_turn({"kind": "cc_mode", "value": "открой ютуб",
                              "asked_at": time.time()}, "да", CHAT, CHAT, "ru") is None)

    print(f"\nИтого: {ok} OK, {fail} FAIL")
    return fail == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

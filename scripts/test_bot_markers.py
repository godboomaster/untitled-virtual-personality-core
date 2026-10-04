"""Тест: маркеры дел/инвентаря, переспрос «Записать «X»…?» и «запомни
сценарий» вне режима управления.

  - маркеры ответа LLM: разбираются ВСЕ вхождения каждого типа (TODO_ADD,
    TODO_DONE, INVENTORY_ADD/REMOVE/USE, PUNISH:FACT), несколько TODO_DONE
    удаляют нужные пункты (номера — по списку, который видела модель,
    только чистым списком/диапазоном; цифры в тексте пункта — не номера),
    TODO_DONE не мешает TODO_ADD в том же ответе, нераспознанный TODO_DONE
    не отключает запасной путь, маркеры вырезаются, прочий текст не трогается;
  - при пустом инвентаре инструкция маркеров инвентаря есть в промпте, в
    учебных ходах её нет (там маркеры не разбираются);
  - переспрос без локальной модели: «да» тем же пользователем на следующем
    ходу — действие и короткое подтверждение без LLM, «нет»/«не надо» —
    отказ, посторонняя реплика и отказ с хвостом — вопрос снят молча, чужой
    пользователь вопрос не трогает, истёкший не исполняется, ход с ранним
    возвратом (переключатель режима) вопрос снимает, reply на другое
    сообщение бота и вопрос обучения/инициатива после него — не ответ;
    «Отметить пункт №N?» сверяет текст пункта; ru и en; вопрос задаётся
    только на явную просьбу («как сделать торт?», «держи меня в курсе» — нет);
  - «запомни сценарий …» вне режима управления: правило не извлекается,
    допущенному к режиму — подсказка, недопущенному (в том числе в чате с
    включённым режимом) и ходу из скина — обычный путь; «запиши сценарий
    ролика», «remember the scenario where…» — обычные просьбы; в режиме
    управления — как раньше (сценарии).

LLM, Ollama и сеть не вызываются: бот — BotInstance.__new__ с заглушками,
локальной модели нет (_local_router = None), data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_bot_markers
"""

import logging
import os
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_TMP = tempfile.mkdtemp(prefix="bot_markers_")
os.environ["VPC_DATA_DIR"] = _TMP
# Обычный путь хода тест обрывает исключением _Stop сразу после извлечения
# правил — traceback «сбоя пайплайна» в выводе не нужен
logging.getLogger("app.bot_instance").setLevel(logging.CRITICAL)

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


class _Stop(Exception):
    """Конвейер дошёл до точки после извлечения правил — дальше не нужно."""


class _Stm:
    def get_last(self, n, chat_id=None):
        return []


class _Ltm:
    def __init__(self):
        self.saved = []

    def save_facts(self, text, user_id, **kw):
        self.saved.append(text)

    def get_facts_by_category(self, *a, **kw):
        raise _Stop()


class _Memory:
    def __init__(self):
        self.stm = _Stm()
        self.ltm = _Ltm()
        self.added = []

    def add_message(self, role, content, *a, **kw):
        self.added.append((role, content))

    def get_context(self, *a, **kw):
        return [], [], []

    def get_chat_facts_block(self, *a, **kw):
        return None


class _Router:
    def is_local_primary(self):
        return False

    def __getattr__(self, name):
        # Любой вызов основной модели в этом тесте — ошибка
        raise AssertionError(f"LLM вызвана: router.{name}")


class _Persona:
    persona_data = {}
    system_prompt = "SYSTEM."


class _Intellect:
    active = False


class _CC:
    """Заглушка менеджера управления: методы, которые ход дёргает в try."""

    def set_turn(self, *a, **kw):
        pass

    def stop_clear(self, *a, **kw):
        pass

    def note_requester(self, *a, **kw):
        pass


class _SM:
    """Заглушка сценариев: разбор — настоящий (ScenarioManager), запись —
    в список."""

    def __init__(self):
        from app.features.scenario_manager import ScenarioManager
        self.parse_start_record = ScenarioManager.parse_start_record
        self.parse_stop_record = ScenarioManager.parse_stop_record
        self.parse_save_request = ScenarioManager.parse_save_request
        self.saved = []

    def active(self, chat_id):
        return False

    def match_scenario(self, *a, **kw):
        return None

    def record_reply(self, chat_id, name, router):
        self.saved.append(name)
        return f"Сценарий «{name}» сохранён."


class _Learning:
    """Заглушка обучения: сессии с вопросом «продолжаем?»/тестом."""

    def __init__(self, sessions=(), setup=None):
        self.sessions = list(sessions)
        self.setup = setup

    def get_sessions(self, chat_id):
        return self.sessions

    def get_setup_state(self, chat_id, user_id=None):
        return self.setup


class _Proactive:
    def __init__(self, last=0.0):
        self.last = last

    def last_initiative_at(self, chat_id):
        return self.last

    def record_user_response(self, chat_id):
        pass


_ctx_n = 0


def _bot(todo=True, inventory=True, control_on=False, single_user=True):
    global _ctx_n
    from app.bot_instance import BotInstance
    from app.features.inventory_manager import InventoryManager
    from app.features.list_offers import ListOffers
    from app.features.todo_manager import TodoManager
    _ctx_n += 1
    ctx = f"t{_ctx_n}"
    bot = BotInstance.__new__(BotInstance)
    bot.persona_name = "tester"
    bot.context = ctx
    bot.trigger_words = {"коннор"}
    bot.persona = _Persona()
    bot.features = {}
    bot.intellect = _Intellect()
    bot.router = _Router()
    bot._local_router = None  # локальной модели нет → переспрос
    bot.todo_manager = TodoManager(context=ctx) if todo else None
    bot.inventory_manager = InventoryManager(context=ctx) if inventory else None
    bot.list_offers = ListOffers()
    bot.memory = _Memory()
    bot.proactive = None
    bot.living = None
    bot.file_db = None
    bot.self_memory = None
    bot.task_agent = None
    bot.computer_control = None
    bot.scenario_manager = None
    bot._web_search_enabled = False
    bot._web_search_disabled_chats = set()
    bot.web_single_user = single_user
    bot.owner = "owner"
    bot._cc_allowed_users = set()
    bot._pending_list_messages = {}
    bot._pending_split_messages = {}
    bot._pending_photos = {}
    bot._pending_question_kind = {}
    bot.user_turn = lambda key: nullcontext()
    bot.control_mode_on = lambda key: control_on
    bot._cc_mode_touch = lambda key: None
    bot._cc_hist_hook_cc = lambda: None
    bot._cc_hist_note_user_text = lambda *a, **kw: None
    bot.facts = []
    bot.inject_fact = lambda text, user_id="default": bot.facts.append(text)
    bot.rule_calls = []

    def _rule(text):
        bot.rule_calls.append(text)
        return "Remember the scenario"
    bot._extract_rule_from_correction = _rule
    bot._pipeline_failure_reply = lambda user_id, chat_id: "STOP"
    return bot


def _todo_items(bot, chat):
    return bot.todo_manager.get_tasks(chat)


def _inv_names(bot):
    return [i.name for i in bot.inventory_manager.get_items()]


def _turn(bot, text, user_id="u1", chat_id="c1", reply_to=None):
    # Ход целиком: ответ на переспрос не должен звать LLM (router падает),
    # обычный путь обрывается после правил — «STOP»
    bot._pending_list_messages[str(chat_id)] = []
    return bot._process_message_impl(text, user_id=user_id, chat_id=chat_id,
                                     user_name="Аня",
                                     reply_to_bot_message_id=reply_to)


def _answer(bot, text, user_id="u1", lang="ru"):
    # Решение по переспросу так же, как в ходе: забрать в начале, решить
    return bot._list_offer_turn(bot._take_list_offer("c1", user_id), text,
                                "c1", "c1", lang)


# ════════════ 1. Маркеры: все вхождения ════════════

def test_markers():
    section("1. Маркеры ответа LLM: все вхождения каждого типа")
    bot = _bot()
    for t in ("первое", "второе", "третье", "четвёртое"):
        bot.todo_manager.add_item("c1", "Аня", t)
    ans = bot._process_todo_marker(
        "Готово! [TODO_DONE:1] [TODO_DONE:3]\nЕщё записала [TODO_ADD:купить хлеб] "
        "и [TODO_ADD:позвонить маме] — всё.",
        "c1", "Аня", user_text="вычеркни 1 и 3, запиши купить хлеб и позвонить маме",
        user_id="u1", lang="ru")
    check("TODO_DONE ×2: удалены именно 1 и 3 (по убыванию, без сдвига)",
          _todo_items(bot, "c1")[:2] == ["второе", "четвёртое"])
    check("TODO_ADD ×2: оба записаны",
          _todo_items(bot, "c1") == ["второе", "четвёртое", "купить хлеб", "позвонить маме"])
    check("в тексте ни одного маркера", "[TODO_" not in ans)
    check("текст ответа цел", ans == "Готово!\nЕщё записала и — всё.")
    lists = bot._pending_list_messages.get("c1", [])
    check("список досылается один раз, итоговый",
          len(lists) == 1 and "позвонить маме" in lists[0] and "первое" not in lists[0])

    bot = _bot()
    for t in ("а", "б", "в"):
        bot.todo_manager.add_item("c1", "Аня", t)
    ans = bot._process_todo_marker("Ок [TODO_DONE:2][TODO_DONE:2]", "c1", "Аня",
                                   user_text="вычеркни 2", user_id="u1")
    check("одинаковый TODO_DONE дважды — один пункт", _todo_items(bot, "c1") == ["а", "в"])
    check("маркеры вырезаны", ans == "Ок")

    # Цифры в тексте пункта — не номера
    bot = _bot()
    for t in ("позвонить маме", "купить 3 яблока", "сдать отчёт", "погулять"):
        bot.todo_manager.add_item("c1", "Аня", t)
    ans = bot._process_todo_marker("Вычеркнула! [TODO_DONE:2. купить 3 яблока]", "c1", "Аня",
                                   user_text="вычеркни яблоки", user_id="u1", lang="ru")
    check("[TODO_DONE:2. купить 3 яблока] — только пункт 2, третий цел",
          _todo_items(bot, "c1") == ["позвонить маме", "сдать отчёт", "погулять"]
          and ans == "Вычеркнула!")
    bot = _bot()
    for t in ("позвонить маме", "купить хлеб", "сдать отчёт"):
        bot.todo_manager.add_item("c1", "Аня", t)
    bot._process_todo_marker("Ок [TODO_DONE:сдать отчёт]", "c1", "Аня",
                             user_text="отчёт сдал", user_id="u1")
    check("[TODO_DONE:текст] — пункт найден по тексту",
          _todo_items(bot, "c1") == ["позвонить маме", "купить хлеб"])
    bot = _bot()
    for t in "abcde":
        bot.todo_manager.add_item("c1", "Аня", t)
    bot._process_todo_marker("Ок [TODO_DONE:1-3]", "c1", "Аня",
                             user_text="вычеркни с 1 по 3", user_id="u1")
    check("[TODO_DONE:1-3] — диапазон раскрыт", _todo_items(bot, "c1") == ["d", "e"])
    bot = _bot()
    for t in "abcde":
        bot.todo_manager.add_item("c1", "Аня", t)
    bot._process_todo_marker("Ок [TODO_DONE:#2, #4]", "c1", "Аня",
                             user_text="вычеркни 2 и 4", user_id="u1")
    check("[TODO_DONE:#2, #4] — список номеров", _todo_items(bot, "c1") == ["a", "c", "e"])
    bot = _bot()
    for t in ("а", "б"):
        bot.todo_manager.add_item("c1", "Аня", t)
    ans = bot._process_todo_marker("Ок [TODO_DONE:что-то про 1 и 2]", "c1", "Аня",
                                   user_text="ну ок", user_id="u1")
    check("нераспознанный TODO_DONE — ничего не удалено, маркер вырезан",
          _todo_items(bot, "c1") == ["а", "б"] and ans == "Ок")

    # TODO_DONE больше не делает ранний return: TODO_ADD того же ответа
    # обрабатывается, а не остаётся видимым текстом
    bot = _bot()
    bot.todo_manager.add_item("c1", "Аня", "старое")
    ans = bot._process_todo_marker("Сделано [TODO_DONE:1] [TODO_ADD:новое]", "c1", "Аня",
                                   user_text="вычеркни 1 и запиши новое", user_id="u1")
    check("TODO_DONE + TODO_ADD: оба применены", _todo_items(bot, "c1") == ["новое"])
    check("TODO_DONE + TODO_ADD: оба вырезаны", ans == "Сделано")

    # Эвристика нашла «вычеркни 2», модель ответила только TODO_ADD —
    # раньше маркер оставался в тексте (ранний return ветки удаления)
    bot = _bot()
    for t in ("а", "б"):
        bot.todo_manager.add_item("c1", "Аня", t)
    ans = bot._process_todo_marker("Записала [TODO_ADD:в]", "c1", "Аня",
                                   fallback_done_index=2, user_text="вычеркни 2",
                                   user_id="u1", lang="ru")
    check("fallback удаления + маркер TODO_ADD: маркер обработан и вырезан",
          ans == "Записала" and "в" in _todo_items(bot, "c1"))

    # Нераспознанный TODO_DONE запасной путь не отключает
    bot = _bot()
    for t in ("а", "б", "в"):
        bot.todo_manager.add_item("c1", "Аня", t)
    bot._pending_list_messages["c1"] = []
    ans = bot._process_todo_marker("Готово [TODO_DONE:молоко]", "c1", "Аня",
                                   fallback_done_index=2, user_text="вычеркни 2",
                                   user_id="u1", lang="ru")
    check("нечисловой TODO_DONE + «вычеркни 2» → переспрос про пункт 2",
          bot._pending_list_messages["c1"] == ["Отметить пункт №2 «б» как выполненный?"]
          and _todo_items(bot, "c1") == ["а", "б", "в"] and ans == "Готово")

    # Прочий текст ответа не трогается: пробелы, пустые строки
    bot = _bot()
    txt = "Стих:  \nстрока один  \nстрока два\n\n\n\nконец [TODO_ADD:y]"
    ans = bot._process_todo_marker(txt, "c1", "Аня", user_text="запиши y", user_id="u1")
    check("текст вокруг маркера не переформатирован",
          ans == "Стих:  \nстрока один  \nстрока два\n\n\n\nконец")
    ans = bot._process_todo_marker("Ок.\n[TODO_ADD:x]\nЕщё строка", "c1", "Аня",
                                   user_text="запиши x", user_id="u1")
    check("маркер на своей строке — вместе со строкой", ans == "Ок.\nЕщё строка")

    bot = _bot()
    bot.inventory_manager.add_item("Старый ключ", "ржавый")
    bot.inventory_manager.add_item("Монета", "медная")
    bot.inventory_manager.add_item("Хлеб", "свежий")
    ans = bot._process_inventory_markers(
        "Спасибо! [INVENTORY_ADD:Яблоко:спелое: красное:2099-01-01] "
        "[INVENTORY_ADD:Мяч:резиновый мяч]\n[INVENTORY_REMOVE:Старый ключ] "
        "[INVENTORY_REMOVE:Монета] [INVENTORY_USE:Хлеб] Ням.",
        giver_name="Аня", user_text="держи яблоко и мяч", chat_id="c1", user_id="u1")
    names = _inv_names(bot)
    check("INVENTORY_ADD ×2: оба добавлены", "Яблоко" in names and "Мяч" in names)
    apple = [i for i in bot.inventory_manager.get_items() if i.name == "Яблоко"]
    check("двоеточие в описании не ломает разбор, срок разобран",
          apple and apple[0].description == "спелое: красное"
          and apple[0].expires == "2099-01-01")
    check("INVENTORY_REMOVE ×2: оба убраны",
          "Старый ключ" not in names and "Монета" not in names)
    check("INVENTORY_USE: использован", "Хлеб" not in names)
    check("в тексте ни одного маркера", "[INVENTORY_" not in ans)
    check("текст ответа цел", ans == "Спасибо!\nНям.")
    check("инвентарь досылается один раз",
          len(bot._pending_list_messages.get("c1", [])) == 1)

    bot = _bot()
    bot._punish_enabled = True
    ans = bot._parse_punishment("Ладно. [PUNISH:FACT:любит лук] [PUNISH:FACT:боится мышей]", "u1")
    check("PUNISH:FACT ×2: оба факта", bot.facts == ["любит лук", "боится мышей"])
    check("PUNISH:FACT ×2: вырезаны", ans == "Ладно.")
    bot.facts.clear()
    ans = bot._parse_punishment("Ха.  [PUNISH:FACT:Привычка: [секрет] грызёт ногти]", "u1")
    check("PUNISH:FACT со скобками внутри — факт целиком, хвоста в тексте нет",
          bot.facts == ["Привычка: [секрет] грызёт ногти"] and ans == "Ха.")


# ════════════ 2. Пустой инвентарь: инструкция маркеров в промпте ════════════

def test_empty_inventory_prompt():
    section("2. Пустой инвентарь: инструкция маркеров есть, в учебных ходах — нет")
    from app.core.persona import PersonaLayer
    from app.features.inventory_manager import InventoryManager
    inv = InventoryManager(context="empty_inv")
    block = inv.get_context_block()
    check("get_context_block пустого инвентаря — не None, «empty»",
          bool(block) and "empty" in block.lower())
    p = PersonaLayer.__new__(PersonaLayer)
    p.system_prompt = "SYSTEM."
    sys_text = p.prepare_messages("держи ключ", inventory_context=block)[0]["content"]
    check("в промпте инструкция [INVENTORY_ADD] и строка «пуст»",
          "[INVENTORY_ADD:" in sys_text and block in sys_text)
    check("текст инструкции — английский",
          not any("а" <= ch.lower() <= "я" for ch in block))
    inv.add_item("Ключ", "медный")
    check("непустой — список предметов, как раньше",
          "Your inventory:" in inv.get_context_block()
          and "Ключ" in inv.get_context_block())

    # Блок инвентаря (с инструкцией маркеров) — при тех же условиях, что и
    # разбор маркеров: в учебно-административном ходе маркеры не разбираются
    src = (Path(__file__).parent.parent / "app" / "bot_instance.py").read_text(encoding="utf-8")
    check("контекст инвентаря и разбор маркеров — одно условие (не напоминание, не учёба)",
          "inventory_markers_on = not is_reminder_request and not is_learning_request" in src
          and src.count("if inv_block and inventory_markers_on:") == 2
          and "if inventory_markers_on and self.inventory_manager:" in src)


# ════════════ 3. Переспрос без локальной модели ════════════

def _ask_todo(bot, lang="ru", user_id="u1", task="купить молоко",
              text="запиши купить молоко"):
    bot._pending_list_messages["c1"] = []
    bot._process_todo_marker("Хорошо!", "c1", "Аня", fallback_task=task,
                             user_text=text, user_id=user_id, lang=lang)
    return bot._pending_list_messages.get("c1", [])


def _ask_inv(bot, lang="ru", user_id="u1", item="ключ", text="держи ключ"):
    bot._pending_list_messages["c1"] = []
    bot._process_inventory_markers("Ох!", fallback_add=item, giver_name="Аня",
                                   user_text=text, chat_id="c1", user_id=user_id,
                                   lang=lang)
    return bot._pending_list_messages.get("c1", [])


def test_offer_todo():
    section("3a. «Записать «X» в список дел?»: да / нет / другое / чужой / срок")
    from app.features import list_offers

    bot = _bot()
    q = _ask_todo(bot)
    check("вопрос задан (ru)", q == ["Записать «купить молоко» в список дел?"])
    reply = _turn(bot, "да")
    check("«да» → подтверждение без LLM",
          reply == "Готово — «купить молоко» в списке дел.")
    check("«да» → дело записано", _todo_items(bot, "c1") == ["купить молоко"])
    check("«да» → список досылается",
          any("купить молоко" in m for m in bot._pending_list_messages.get("c1", [])))
    check("«да» → реплика и ответ в истории",
          bot.memory.added[-2:] == [("user", "да"), ("assistant", reply)])
    check("вопрос снят", bot.list_offers.peek("c1") is None)

    bot = _bot()
    _ask_todo(bot)
    check("«да, запиши» — тоже согласие",
          _turn(bot, "Да, запиши!") == "Готово — «купить молоко» в списке дел.")
    for yes in ("нет проблем", "no problem"):
        bot = _bot()
        _ask_todo(bot)
        check(f"«{yes}» — согласие", _answer(bot, yes) is not None
              and _todo_items(bot, "c1") == ["купить молоко"])

    for no in ("нет", "не надо", "не записывай", "нет, спасибо", "ну нет"):
        bot = _bot()
        _ask_todo(bot)
        reply = _turn(bot, no)
        check(f"«{no}» → отказ, ничего не записано",
              reply == "Хорошо, не буду." and _todo_items(bot, "c1") == []
              and bot.list_offers.peek("c1") is None)

    # Отказ с содержательным хвостом и «да ладно» — другая реплика: вопрос
    # снят, реплика уходит обычным путём (хвост не теряется)
    for other in ("нет, расскажи анекдот", "нет, а зачем?", "да ладно", "не знаю",
                  "а какая завтра погода?"):
        bot = _bot()
        _ask_todo(bot)
        reply = _turn(bot, other)
        check(f"«{other}» → обычный путь, вопрос снят молча",
              reply == "STOP" and _todo_items(bot, "c1") == []
              and bot.list_offers.peek("c1") is None)
    check("после неё «да» уже ничего не делает",
          _turn(bot, "да") == "STOP" and _todo_items(bot, "c1") == [])

    bot = _bot()
    _ask_todo(bot, user_id="u1")
    check("«да» другого участника → не ответ (обычный путь)",
          _turn(bot, "да", user_id="u2") == "STOP")
    check("вопрос спросившего жив", bot.list_offers.peek("c1") is not None)
    check("ничего не записано чужим «да»", _todo_items(bot, "c1") == [])
    check("а «да» спросившего — записывает",
          _turn(bot, "да") == "Готово — «купить молоко» в списке дел."
          and _todo_items(bot, "c1") == ["купить молоко"])

    bot = _bot()
    _ask_todo(bot)
    bot.list_offers._offers["c1"]["asked_at"] = time.time() - list_offers.LIST_OFFER_TTL_SEC - 1
    check("истёкший вопрос: «да» → обычный путь, ничего не записано",
          _turn(bot, "да") == "STOP" and _todo_items(bot, "c1") == [])
    check("истёкший вопрос снят", bot.list_offers.peek("c1") is None)

    bot = _bot(control_on=True)
    _ask_todo(bot)
    check("в режиме управления вопрос снимается без действия",
          _answer(bot, "да") is None and bot.list_offers.peek("c1") is None
          and _todo_items(bot, "c1") == [])

    # Ход с ранним возвратом (переключатель режима) — тоже ход: вопрос снят
    bot = _bot()
    bot.computer_control = _CC()
    bot._cc_pop_idle_notice = lambda key, lang=None: None
    state = {"on": False}
    bot.control_mode_on = lambda key: state["on"]

    def _switch(key, mode, lang=None):
        state["on"] = bool(mode)
        return "режим переключён"
    bot._control_mode_switch = _switch
    _ask_todo(bot)
    _turn(bot, "перейди в режим управления")
    _turn(bot, "выйди из режима управления")
    check("переключатель режима (ранний возврат) снимает вопрос",
          bot.list_offers.peek("c1") is None)
    check("«ок» потом — обычный путь, старое не записано",
          _turn(bot, "ок") == "STOP" and _todo_items(bot, "c1") == [])

    # English
    bot = _bot()
    q = _ask_todo(bot, lang="en", task="buy milk", text="add buy milk to my todo list")
    check("вопрос задан (en)", q == ['Add "buy milk" to the todo list?'])
    reply = _turn(bot, "yes please")
    check("«yes please» → подтверждение (en) без LLM",
          reply == 'Done — "buy milk" is on the todo list.'
          and _todo_items(bot, "c1") == ["buy milk"])
    bot = _bot()
    _ask_todo(bot, lang="en", task="buy milk", text="add buy milk to my todo list")
    check("«no» → отказ (en)", _turn(bot, "no") == "Okay, I won't."
          and _todo_items(bot, "c1") == [])
    bot = _bot()
    _ask_todo(bot, lang="en", task="buy milk", text="add buy milk to my todo list")
    check("«don't add it» → отказ (en)", _turn(bot, "don't add it") == "Okay, I won't.")
    bot = _bot()
    _ask_todo(bot, lang="en", task="buy milk", text="add buy milk to my todo list")
    check("«no, tell me a joke» → обычный путь (en)",
          _turn(bot, "no, tell me a joke") == "STOP" and _todo_items(bot, "c1") == [])


def test_offer_not_ours():
    section("3b. «Да» не нам: reply на другое сообщение, вопрос обучения, инициатива")
    bot = _bot()
    q = _ask_todo(bot)
    bot.note_list_message("c1", "Список дел:\n1. x", [500])  # не вопрос — не запоминается
    bot.note_list_message("c1", q[0], [501])
    check("message_id вопроса запомнен", bot.list_offers.peek("c1")["message_ids"] == [501])
    check("reply на сам вопрос — ответ",
          _turn(bot, "да", reply_to=501) == "Готово — «купить молоко» в списке дел.")

    bot = _bot()
    q = _ask_todo(bot)
    bot.note_list_message("c1", q[0], [501])
    check("reply на другое сообщение бота (вопрос обучения) — не ответ",
          _turn(bot, "да", reply_to=777) == "STOP" and _todo_items(bot, "c1") == [])

    bot = _bot()
    _ask_todo(bot)
    bot.learning_manager = _Learning(sessions=[{"continue_asked_at": time.time() + 1}])
    check("после вопроса обучение спросило «продолжаем?» — «да» не нам",
          _turn(bot, "да") == "STOP" and _todo_items(bot, "c1") == [])
    bot = _bot()
    _ask_todo(bot)
    bot.learning_manager = _Learning(sessions=[{"quiz_set_at": time.time() + 1}])
    check("после вопроса пришёл тест — «да» не нам",
          _turn(bot, "да") == "STOP" and _todo_items(bot, "c1") == [])
    bot = _bot()
    _ask_todo(bot)
    bot.learning_manager = _Learning(sessions=[{"continue_asked_at": time.time() - 60}])
    check("вопрос обучения ДО переспроса — «да» наше",
          _turn(bot, "да") == "Готово — «купить молоко» в списке дел.")
    bot = _bot()
    _ask_todo(bot)
    bot.proactive = _Proactive(last=time.time() + 1)
    check("после вопроса ушла инициатива — «да» не нам",
          _turn(bot, "да") == "STOP" and _todo_items(bot, "c1") == [])


def test_offer_inventory():
    section("3c. «Добавить «X» в инвентарь?»: да / нет / другое / чужой / срок")
    from app.features import list_offers

    bot = _bot()
    q = _ask_inv(bot)
    check("вопрос задан (ru)", q == ["Добавить «ключ» в инвентарь?"])
    reply = _turn(bot, "давай")
    check("«давай» → подтверждение без LLM", reply == "Готово — кладу «ключ» в инвентарь.")
    check("предмет добавлен с заглавной", _inv_names(bot) == ["Ключ"])
    check("инвентарь досылается",
          any("Ключ" in m for m in bot._pending_list_messages.get("c1", [])))

    bot = _bot()
    q = _ask_inv(bot, item="шоколадку", text="на, держи шоколадку")
    check("«на, держи шоколадку» → вопрос в винительном",
          q == ["Добавить «шоколадку» в инвентарь?"])
    check("«да» → «кладу «шоколадку» в инвентарь», имя с заглавной",
          _turn(bot, "да") == "Готово — кладу «шоколадку» в инвентарь."
          and _inv_names(bot) == ["Шоколадку"])

    bot = _bot()
    bot.inventory_manager.add_item("Шоколадка", "молочная")
    check("предмет уже есть («Шоколадка» ~ «шоколадку») — вопроса нет",
          _ask_inv(bot, item="шоколадку", text="держи шоколадку") == [])
    bot = _bot()
    _ask_inv(bot, item="шоколадку", text="держи шоколадку")
    bot.inventory_manager.add_item("Шоколадка", "от маркера")
    check("появился, пока висел вопрос → честно говорим, дубля нет",
          _turn(bot, "да") == "«Шоколадка» уже есть в инвентаре."
          and _inv_names(bot) == ["Шоколадка"])

    bot = _bot()
    _ask_inv(bot)
    check("«не надо» → отказ, не добавлено",
          _turn(bot, "не надо") == "Хорошо, не буду." and _inv_names(bot) == [])

    bot = _bot()
    _ask_inv(bot)
    check("посторонняя реплика → обычный путь, вопрос снят",
          _turn(bot, "расскажи сказку") == "STOP"
          and bot.list_offers.peek("c1") is None and _inv_names(bot) == [])

    bot = _bot()
    _ask_inv(bot, user_id="u1")
    check("«да» другого участника → не ответ, вопрос жив",
          _turn(bot, "да", user_id="u2") == "STOP"
          and bot.list_offers.peek("c1") is not None and _inv_names(bot) == [])

    bot = _bot()
    _ask_inv(bot)
    bot.list_offers._offers["c1"]["asked_at"] = time.time() - list_offers.LIST_OFFER_TTL_SEC - 1
    check("истёкший вопрос: «да» → обычный путь, не добавлено",
          _turn(bot, "да") == "STOP" and _inv_names(bot) == [])

    # English
    bot = _bot()
    q = _ask_inv(bot, lang="en")
    check("вопрос задан (en)", q == ['Add "ключ" to the inventory?'])
    check("«yes» → подтверждение (en)",
          _turn(bot, "yes") == 'Done — putting "ключ" in the inventory.'
          and _inv_names(bot) == ["Ключ"])
    bot = _bot()
    _ask_inv(bot, lang="en")
    check("«nope» → отказ (en)", _turn(bot, "nope") == "Okay, I won't." and _inv_names(bot) == [])


def test_offer_remove_kinds():
    section("3d. Переспрос вычеркнуть пункт / убрать предмет")
    bot = _bot()
    for t in ("а", "б", "в"):
        bot.todo_manager.add_item("c1", "Аня", t)
    bot._pending_list_messages["c1"] = []
    bot._process_todo_marker("Ок", "c1", "Аня", fallback_done_index=2,
                             user_text="вычеркни 2", user_id="u1", lang="ru")
    check("вопрос «Отметить пункт №2 «б»…?» — с текстом пункта",
          bot._pending_list_messages["c1"] == ["Отметить пункт №2 «б» как выполненный?"])
    check("«да» → пункт 2 вычеркнут",
          _turn(bot, "да") == "Готово — пункт №2 вычеркнут." and _todo_items(bot, "c1") == ["а", "в"])

    # Список изменился, пока висел вопрос: под №2 уже другое — не трогаем
    bot = _bot()
    for t in ("а", "б", "в"):
        bot.todo_manager.add_item("c1", "Аня", t)
    bot._pending_list_messages["c1"] = []
    bot._process_todo_marker("Ок", "c1", "Аня", fallback_done_index=2,
                             user_text="вычеркни 2", user_id="u1", lang="ru")
    bot.todo_manager.remove_item("c1", 1)  # другой участник/веб удалил пункт 1
    check("номер съехал → честный ответ, ничего не вычеркнуто",
          _turn(bot, "да") == "Пункта №2 «б» в списке уже нет — ничего не вычёркиваю."
          and _todo_items(bot, "c1") == ["б", "в"])

    bot = _bot()
    bot.todo_manager.add_item("c1", "Аня", "а")
    bot._pending_list_messages["c1"] = []
    bot._process_todo_marker("Молодец", "c1", "Аня", fallback_done_index=3,
                             user_text="готово, прочитал 3 главы", user_id="u1")
    check("«готово, прочитал 3 главы» — без вопроса (число из рассказа)",
          bot._pending_list_messages["c1"] == [] and bot.list_offers.peek("c1") is None)
    bot._process_todo_marker("Ок", "c1", "Аня", fallback_done_index=5,
                             user_text="вычеркни 5", user_id="u1")
    check("«вычеркни 5» при одном пункте — без вопроса (такого номера нет)",
          bot._pending_list_messages["c1"] == [] and bot.list_offers.peek("c1") is None)

    bot = _bot()
    bot.inventory_manager.add_item("Яблоко", "красное")
    bot._pending_list_messages["c1"] = []
    bot._process_inventory_markers("Ладно", fallback_remove="яблоко", giver_name="Аня",
                                   user_text="выбрось яблоко", chat_id="c1", user_id="u1",
                                   lang="ru")
    check("вопрос «Убрать «Яблоко» из инвентаря?» (имя из инвентаря)",
          bot._pending_list_messages["c1"] == ["Убрать «Яблоко» из инвентаря?"])
    check("«да, выбрось» → убран",
          _turn(bot, "да, выбрось") == "Готово — «Яблоко» больше нет в инвентаре."
          and _inv_names(bot) == [])

    bot = _bot()
    bot.inventory_manager.add_item("Негорючий плащ", "")
    for text, item in (("сними видео", "видео"), ("убери его", "его")):
        bot._pending_list_messages["c1"] = []
        bot._process_inventory_markers("Ок", fallback_remove=item, user_text=text,
                                       chat_id="c1", user_id="u1")
        check(f"«{text}» — без вопроса (такого предмета нет / местоимение)",
              bot._pending_list_messages["c1"] == [] and bot.list_offers.peek("c1") is None)


def test_offer_only_explicit():
    section("3e. Вопрос — только на явную просьбу")
    from app.features.todo_manager import extract_task, is_todo_request
    from app.features.inventory_manager import extract_inventory_item

    for text in ("как сделать скриншот на маке", "что мне сделать на ужин?",
                 "добавь деталей в рассказ", "надо сделать отчёт",
                 "можешь сделать мне комплимент?"):
        bot = _bot()
        check(f"эвристика срабатывает: «{text}»", is_todo_request(text))
        q = _ask_todo(bot, task=extract_task(text), text=text)
        check(f"…но вопроса нет: «{text}»", q == [] and bot.list_offers.peek("c1") is None)

    for text in ("запиши купить молоко", "добавь в список дел купить хлеб",
                 "Коннор, запиши: позвонить маме", "todo: buy milk"):
        bot = _bot()
        q = _ask_todo(bot, task=extract_task(text, ["коннор"]), text=text)
        check(f"явная просьба → вопрос: «{text}» {q}", len(q) == 1)
    check("«добавь в список дел купить хлеб» → задача без «дел»",
          extract_task("добавь в список дел купить хлеб") == "купить хлеб")

    for text in ("держи меня в курсе", "возьми паузу", "подбери мне фильм",
                 "передаю привет маме", "забери свои слова назад", "держи ключ?",
                 "держи пять", "держи кулачки", "держи ухо востро",
                 "возьми ответственность", "возьми с собой зонт"):
        bot = _bot()
        q = _ask_inv(bot, item=extract_inventory_item(text), text=text)
        check(f"передачи предмета нет — вопроса нет: «{text}»", q == [])

    for text, item in (("держи ключ", "ключ"), ("вот тебе красный мяч", "красный мяч"),
                       ("добавь в инвентарь яблоко", "яблоко"),
                       ("возьми ключ, он пригодится", "ключ")):
        bot = _bot()
        q = _ask_inv(bot, item=extract_inventory_item(text), text=text)
        check(f"передача предмета → вопрос про «{item}»",
              q == [f"Добавить «{item}» в инвентарь?"])

    bot = _bot()
    bot._pending_list_messages["c1"] = []
    bot._process_todo_marker("Ок", "c1", "Аня", fallback_task="яблоко",
                             user_text="запиши яблоко", user_id="u1", lang="ru")
    bot._process_inventory_markers("Ок", fallback_add="яблоко", user_text="держи яблоко",
                                   chat_id="c1", user_id="u1", lang="ru")
    check("два вопроса в одном ходе не задаются — только первый",
          bot._pending_list_messages["c1"] == ["Записать «яблоко» в список дел?"]
          and bot.list_offers.peek("c1")["kind"] == "todo_add")


# ════════════ 4. «Запомни сценарий» вне режима управления ════════════

def _sc_bot(**kw):
    bot = _bot(**kw)
    bot.computer_control = _CC()
    bot.scenario_manager = _SM()
    return bot


def test_scenario_outside_mode():
    section("4. «Запомни сценарий …» вне режима управления")
    from app.features import cc_texts
    import app.bot_instance as bi

    bot = _bot()
    for text in ("запомни сценарий заказ пиццы", "Коннор, запомни сценарий заказ пиццы",
                 "Запомни, пожалуйста, сценарий заказа пиццы",
                 "запомни сценарий:\nшаг 1\nшаг 2", "сохрани сценарий как утро",
                 "save the scenario pizza order"):
        check(f"команда сценария: {text!r}", bot._is_scenario_save_command(text))
    for text in ("запомни, я не люблю кофе", "запомни мой день рождения",
                 "какой сценарий у фильма?"):
        check(f"не команда сценария: «{text}»", not bot._is_scenario_save_command(text))

    hint_ru = cc_texts.t("scenario_outside_mode", "ru")
    hint_en = cc_texts.t("scenario_outside_mode", "en")
    check("подсказка на двух языках", hint_ru != hint_en
          and "режим управления" in hint_ru and "control mode" in hint_en)

    # Допущен (владелец), у персоны есть режим управления и сценарии
    for text in ("запомни сценарий заказ пиццы", "Запомни, пожалуйста, сценарий заказа пиццы",
                 "запомни сценарий:\nоткрой додо\nвыбери пиццу"):
        bot = _sc_bot()
        reply = _turn(bot, text)
        check(f"допущенному — подсказка: {text!r}", reply == hint_ru)
        check(f"правило не извлекалось: {text!r}",
              bot.rule_calls == [] and bot.memory.ltm.saved == [])
    check("сценарий не записан, в истории реплика и подсказка",
          bot.scenario_manager.saved == []
          and bot.memory.added[-1] == ("assistant", hint_ru))

    # Обычные просьбы со словом «сценарий» — не перехватываются (раньше
    # правилом не становились: в эвристике исправлений только «запомни»)
    for text in ("запиши сценарий ролика про котов для тиктока",
                 "сохрани сценарий нашей игры",
                 "remember the scenario where you are a pirate",
                 "save the scenario pizza order"):
        bot = _sc_bot()
        check(f"обычный ответ, без подсказки: «{text}»", _turn(bot, text) == "STOP")

    # Не допущен: режима «не существует» — обычный путь, но без правила
    bot = _sc_bot(single_user=False)
    reply = _turn(bot, "запомни сценарий заказ пиццы", user_id="guest")
    check("недопущенному — без подсказки (обычный путь)", reply == "STOP")
    check("недопущенному — правило не извлекалось", bot.rule_calls == []
          and bot.memory.ltm.saved == [])
    bot = _sc_bot(single_user=False)
    _turn(bot, "Запомни, пожалуйста, сценарий заказа пиццы", user_id="guest")
    check("недопущенному, «пожалуйста» — правило не извлекалось", bot.rule_calls == [])

    # Недопущенный в чате с включённым режимом блок режима не проходит —
    # правило создаваться всё равно не должно
    bot = _sc_bot(single_user=False, control_on=True)
    reply = _turn(bot, "запомни сценарий заказ пиццы", user_id="guest")
    check("недопущенный в чате с режимом — без правила, без записи сценария",
          reply == "STOP" and bot.rule_calls == [] and bot.scenario_manager.saved == [])

    # Ход из скина: _cc_allowed — False, подсказки нет, правила тоже
    bot = _sc_bot()
    reply = bot.process_message("запомни сценарий заказ пиццы", user_id="u1",
                                chat_id="c1", from_skin=True)
    check("из скина — без подсказки и без правила",
          reply == "STOP" and bot.rule_calls == [])
    check("флаг хода из скина снят", not getattr(bi._SKIN_TURN, "on", False))

    # Нет режима управления у персоны — подсказки нет, правила нет
    bot = _bot()
    reply = _turn(bot, "запомни сценарий заказ пиццы")
    check("без computer_control — без подсказки и без правила",
          reply == "STOP" and bot.rule_calls == [])

    # Обычное «запомни …» — правило, как раньше
    bot = _bot()
    _turn(bot, "запомни, я не люблю кофе")
    check("«запомни, я не люблю кофе» — правило извлекается",
          bot.rule_calls == ["запомни, я не люблю кофе"]
          and bot.memory.ltm.saved == ["Rule: Remember the scenario"])

    # В режиме управления — как раньше: команду забирают сценарии
    bot = _sc_bot(control_on=True)
    reply = _turn(bot, "запомни сценарий заказ пиццы")
    check("в режиме управления — запись сценария, как раньше",
          reply == "Сценарий «заказ пиццы» сохранён."
          and bot.scenario_manager.saved == ["заказ пиццы"] and bot.rule_calls == [])


def main():
    test_markers()
    test_empty_inventory_prompt()
    test_offer_todo()
    test_offer_not_ours()
    test_offer_inventory()
    test_offer_remove_kinds()
    test_offer_only_explicit()
    test_scenario_outside_mode()
    print(f"\nИтого: {ok - failures}/{ok} OK" + (f", FAIL: {failures}" if failures else ""))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

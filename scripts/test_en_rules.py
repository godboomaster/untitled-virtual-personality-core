"""Тест: английские поправки/просьбы запомнить правило распознаются так же,
как русские, русское поведение не меняется.

  A. эвристика _CORRECTION_HINT_EN_RE / _looks_like_correction: английские
     поправки («that's wrong», «I meant…», «don't call me…», «you did it
     again», «remember: …», «keep in mind…») срабатывают; обычная речь —
     нет: вопросы и воспоминания с «remember», напоминалки («remember to buy
     milk», «remind me…»), рассказы с «wrong», «again», звонки («don't call
     me tomorrow»), команды сценария («remember/save the scenario …»);
  B. русский шаблон не тронут (тот же текст и флаги), и для каждой русской
     фразы из строк scripts/test_*.py и примеров ниже помощник отвечает так
     же, как раньше _CORRECTION_HINT_RE.search;
  C. английская эвристика смотрит только на то, что написал человек
     (подпись), а не на текст файла/OCR; русская — как раньше, по всему вводу;
  D. ход целиком (_process_message_impl с заглушками из test_bot_markers):
     английская поправка — извлечение правила, обычная речь — нет; команды
     сценария на обоих языках вне режима управления правилом не становятся;
     для команд сценария и русских фраз исход хода (ответ, вызовы извлечения
     правила, записанные сценарии) совпадает со старой эвристикой — вне
     режима, у недопущенного и в режиме управления;
  E. длинный ввод (серии пробелов и пустых строк, сотни поправок в одной
     фразе) проверяется за линейное время, без квадратичного перебора;
  F. обращение в начале без запятой (веб-чат и скин имя не срезают):
     «connor call me max», «hey Connor don't call me buddy» — правило/имя;
     имя — целым словом («Connors …», «Don't …» у персоны Don, «Call me …»
     у персоны Cal), имя-модальный глагол без знака («will remember that!»
     у персоны Will) и имя-подлежащее («Connor called me Max») — не
     обращение; русские итоги не меняются;
  G. команды сценария в режиме управления с обращением и «пожалуйста»
     («Коннор, запомни сценарий утро», «Запомни, пожалуйста, сценарий утро»,
     «save the scenario as X please») — запись сценария, а не правило; тот
     же разбор, что у подсказки вне режима.

LLM, Ollama и сеть не вызываются: извлечение правила — заглушка, локальной
модели нет, data/ — временный каталог.
Запуск: python3 -m scripts.test_en_rules
"""

import ast
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")

# Заготовка бота и ход целиком — из test_bot_markers (там же VPC_DATA_DIR
# во временный каталог — до импорта app)
from scripts import test_bot_markers as tbm  # noqa: E402
import app.bot_instance as bi  # noqa: E402

ROOT = Path(__file__).parent.parent

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    if cond:
        ok += 1
        print(f"  [OK] {name}")
    else:
        failures += 1
        print(f"  [FAIL] {name}")


def section(title):
    print(f"\n── {title} ──")


# Русский шаблон до английской пары — дословно
OLD_RU_PATTERN = (
    r"\b(?:не\s+так|неправильно|неверно|я\s+име[лл]\s+в\s+виду|запомни|не\s+называй|"
    r"не\s+надо\s+так|не\s+говори\s+так|поправ\w*|исправь|ты\s+опять|ты\s+снова)\b")
OLD_RU_RE = re.compile(OLD_RU_PATTERN, re.IGNORECASE)


def old_hint(text, typed=None):
    # Как было до английской пары: только русский шаблон по всему вводу
    return bool(OLD_RU_RE.search(text))


EN_POSITIVE = [
    # просьбы запомнить
    "remember: I don't eat meat",
    "Remember, I'm vegetarian.",
    "remember that I hate onions",
    "Remember that my name is Alex",
    "please remember that I'm allergic to nuts",
    "Please remember my birthday is May 5",
    "remember please, no emoji",
    "remember I'm vegan",
    "remember I don't drink coffee",
    "remember my name is Alex",
    "remember my dog's name is Rex",
    "remember this: no emoji",
    "Remember this!",
    "remember to never use emoji",
    "remember not to call me buddy",
    "Connor, remember that I work nights",
    "and remember, no smileys",
    "keep in mind that I'm a student",
    "Please keep in mind I live in Berlin",
    "bear that in mind",
    "don't forget that I'm left-handed",
    "For future reference, I prefer short answers",
    "From now on, always answer briefly",
    "from now on don't use emoji",
    "from now on I want you to answer in English",
    # поправки
    "that's wrong",
    "No, that's not right.",
    "That's not what I meant",
    "this is incorrect",
    "that’s not it",
    "not what I asked",
    "you're wrong",
    "You are wrong about the date",
    "you got it wrong",
    "you misunderstood me",
    "No, not like that",
    "Not like that!",
    "not that way",
    "I meant the other one",
    "what I meant was Tuesday",
    "Sorry, I meant to say Paris",
    "don't call me buddy",
    "Don't call me that",
    "please don't call me sweetie",
    "do not call me by my full name",
    "stop calling me buddy",
    "Never call me that again",
    "don't say it like that",
    "don't say that",
    "don't talk to me like that",
    "don't do that",
    "Don't ever do that again",
    "don't use emoji",
    "dont use emojis",
    "don’t apologize so much",
    "don't repeat yourself",
    "don't ask me about work",
    "stop using emoji",
    "Stop saying sorry",
    "stop asking questions at the end",
    "you did it again",
    "you're doing it again",
    "you called me buddy again",
    "you used emoji again",
    "you keep forgetting my name",
    "you keep asking me that",
    "Again you forgot my name",
    "Once again, you're wrong",
    "correct yourself",
    "Fix that.",
    "fix it please",
    "how many times do I have to tell you",
    "I told you not to call me that",
    "I said don't use emoji",
    # найдены при ревью
    "you got my name wrong",
    "you mixed up my sisters",
    "don't call me 'buddy' please",
    "don't call me “sweetie”",
    "That's wrong.\nMy sister is Kate, not Anna",
]

EN_NEGATIVE = [
    # вопросы и воспоминания с «remember»
    "do you remember what I told you yesterday?",
    "Do you remember me?",
    "I remember when we went to Paris",
    "remember when we went to the zoo?",
    "Remember when we went to the zoo",
    "I can't remember his name",
    "remember that time we got lost?",
    "remember that day at the beach",
    "Remember this place?",
    "remember my name?",
    "remember me?",
    "remember, when we were kids?",
    "we'll always remember him",
    "what do you remember about me?",
    "The movie was about a man who couldn't remember anything",
    # напоминалки и дела
    "remember to buy milk",
    "please remember to call mom",
    "remind me to buy milk tomorrow",
    "remind me in 10 minutes",
    "don't forget to call mom",
    "I'll keep it in mind",
    "I will keep in mind your advice",
    # «wrong», «again» в обычной речи
    "The answer was wrong, so the teacher was angry",
    "it's wrong to steal",
    "he was wrong about everything",
    "something went wrong with my laptop",
    "what's wrong with you?",
    "is that wrong?",
    "is that not true?",
    "that's wrong of me",
    "I did it again!",
    "let's do it again",
    "see you again",
    "thanks again, you're the best",
    "again and again",
    "you made me laugh again",
    # «I mean», звонки, идиомы
    "I mean, it's fine",
    "I meant to call you yesterday",
    "I meant it",
    "I meant no offense to her",
    "I meant what I said about moving to Spain",
    "I meant to tell you, I got the job",
    "I meant to ask, how was your weekend?",
    "Connor, I meant to ask, how's your day?",
    "don't call me, I'll call you",
    "Don't call me.",
    "don't call me so late next time",
    "I told you not to worry, everything worked out fine.",
    "don't call me tomorrow, I'm busy",
    "don't call me after 10pm",
    "please don't call me at work",
    "if you don't call me I'll be sad",
    "why don't you call me later?",
    "don't worry",
    "don't mention it",
    "don't be sad",
    "never mind",
    "I need to stop using my phone at night",
    "I can't stop thinking about you",
    "it's not like that between us",
    "how do I fix it?",
    "can you fix my code?",
    "fix this bug",
    "I told you about my sister",
    "I said goodbye",
    "you look great",
    "that's right",
    "that's not bad",
    "hi! how are you?",
    "tell me a joke",
    "what's the weather like in London?",
    # команды сценария (вне режима управления — не правило)
    "remember the scenario where you are a pirate",
    "remember this scenario as order pizza",
    "remember the scenario pizza order",
    "please remember the scenario as order pizza",
    "Please, remember this scenario as morning",
    "remember please the scenario pizza",
    "save the scenario as order pizza",
    "save this scenario as morning routine",
    "record the scenario morning",
]

RU_EXAMPLES = [
    "не так", "неправильно", "это неверно", "я имел в виду другое", "запомни, я не люблю кофе",
    "запомни мой день рождения", "не называй его Сашей", "не надо так", "не говори так",
    "поправь", "поправка: не Анна, а Катя", "исправь", "ты опять за своё", "ты снова забыл",
    "Запомни: я не ем мясо", "Коннор, не так!", "я поправился на два кило",
    "запомни сценарий заказ пиццы", "Запомни, пожалуйста, сценарий заказа пиццы",
    "запомни сценарий:\nоткрой додо\nвыбери пиццу", "сохрани сценарий как утро",
    "запиши сценарий ролика про котов", "привет, как дела?", "помнишь, как мы ездили на море?",
    "напомни купить молоко", "что ты помнишь обо мне?", "опять дождь", "снова понедельник",
    "это так неправильно звучит, но мне нравится", "не знаю", "так-то да",
]


def ru_corpus():
    """Русские строки из scripts/test_*.py (все строковые литералы с
    кириллицей) и примеры выше."""
    cyr = re.compile("[А-Яа-яЁё]")
    out = set(RU_EXAMPLES)
    for path in sorted((ROOT / "scripts").glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and cyr.search(node.value) and len(node.value) < 2000):
                out.add(node.value)
    return sorted(out)


# ════════════ A. Английская эвристика ════════════

def test_en_heuristic():
    section("A. Английские поправки срабатывают, обычная речь — нет")
    for text in EN_POSITIVE:
        check(f"поправка: {text!r}", bi._looks_like_correction(text))
    for text in EN_NEGATIVE:
        m = bi._CORRECTION_HINT_EN_RE.search(text)
        check(f"не поправка: {text!r}" + (f" (сработало на {m.group(0)!r})" if m else ""),
              not bi._looks_like_correction(text))
    check("пустой и None — не поправка",
          not bi._looks_like_correction("") and not bi._looks_like_correction(None))
    # Обе «английские» половины помощника видят одно и то же, если подписи нет
    check("typed=None — смотрим на сам текст",
          bi._looks_like_correction("don't call me buddy", None))


# ════════════ B. Русское поведение не меняется ════════════

def test_ru_unchanged():
    section("B. Русский шаблон и русские фразы — как раньше")
    check("русский шаблон дословно тот же",
          bi._CORRECTION_HINT_RE.pattern == OLD_RU_PATTERN
          and bi._CORRECTION_HINT_RE.flags == OLD_RU_RE.flags)

    corpus = ru_corpus()
    lat = re.compile("[A-Za-z]")
    pure = [t for t in corpus if not lat.search(t)]
    mixed = [t for t in corpus if lat.search(t)]
    hits = sum(old_hint(t) for t in pure)
    check(f"корпус русских фраз собран ({len(pure)} без латиницы, {hits} — поправки)",
          len(pure) > 1000 and hits >= 15)

    # Без латиницы английский шаблон сработать не может — итог строго прежний,
    # с подписью и без (фото/файл: typed — подпись, может быть пустой)
    diff = [t for t in pure for typed in (None, t, "")
            if bi._looks_like_correction(t, typed) != old_hint(t)]
    check(f"русские фразы: помощник = старый _CORRECTION_HINT_RE.search "
          f"({len(pure)} × 3 варианта подписи)", not diff)
    for t in diff[:10]:
        print(f"      расхождение: {t[:100]!r}")

    # Смешанные строки (кириллица + латиница): прежнее срабатывание не теряется,
    # новое — только там, где есть английская поправка
    lost = [t for t in mixed if old_hint(t) and not bi._looks_like_correction(t)]
    extra = [t for t in mixed if bi._looks_like_correction(t) and not old_hint(t)]
    check(f"смешанные строки ({len(mixed)}): прежние срабатывания на месте", not lost)
    check("смешанные строки: новые срабатывания — только из-за английской поправки",
          all(bi._CORRECTION_HINT_EN_RE.search(t) for t in extra))
    for t in extra:
        print(f"      англ. поправка в смешанной строке: {t[:80]!r}")


# ════════════ C. Подпись против текста файла / OCR ════════════

def test_typed_vs_composite():
    section("C. Английское — только по подписи, русское — по всему вводу")
    doc = ("The user sent a file 'notes.txt'. Files loaded: 1/5:\n\n"
           "Remember: never commit secrets. Don't use global variables. "
           "That's wrong, said the reviewer.")
    check("английский текст файла без подписи — не поправка",
          not bi._looks_like_correction(doc, ""))
    check("английский текст файла с нейтральной подписью — не поправка",
          not bi._looks_like_correction("summarize this\n\n" + doc, "summarize this"))
    check("английская поправка в подписи — поправка",
          bi._looks_like_correction("don't call me buddy\n\n" + doc, "don't call me buddy"))
    ocr = ("The user sent an image. Its contents according to the vision model:\n"
           "A sign: keep in mind that the shop closes at 6")
    check("английский OCR без подписи — не поправка", not bi._looks_like_correction(ocr, ""))
    check("служебные обёртки фото/файла сами не срабатывают",
          not bi._looks_like_correction(
              "The user sent an image. Its contents according to the vision model:\nA cat", None)
          and not bi._looks_like_correction(
              "The user sent a file 'a.txt'. Files loaded: 1/5:\n\nhello", None))
    ru_doc = "The user sent a file 'заметки.txt'. Files loaded: 1/5:\n\nЗапомни: не так!"
    check("русский текст файла — как раньше (по всему вводу)",
          bi._looks_like_correction(ru_doc, "") == old_hint(ru_doc) is True)


# ════════════ D. Ход целиком ════════════

class _CCAny(tbm._CC):
    """Заглушка режима управления для хода В режиме: любой метод — None
    (ни pending, ни быстрой команды) — реплика, которую не забрали
    сценарии, идёт дальше обычным путём, до извлечения правила."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **kw: None


def _sc_bot(**kw):
    bot = tbm._sc_bot(**kw)
    bot.computer_control = _CCAny()
    bot._pending_more_photos = {}
    bot.trigger_words = {"коннор", "connor"}
    return bot


def _outcome(bot, text, user_id="u1", raw=None, **kw):
    bot._pending_list_messages["c1"] = []
    reply = bot._process_message_impl(text, user_id=user_id, chat_id="c1", user_name="Ann",
                                      raw_user_text=raw, **kw)
    saved = bot.scenario_manager.saved if bot.scenario_manager else None
    return reply, list(bot.rule_calls), list(bot.memory.ltm.saved), saved


def _with_old_hint(fn):
    real = bi._looks_like_correction
    bi._looks_like_correction = old_hint
    try:
        return fn()
    finally:
        bi._looks_like_correction = real


def test_turns():
    section("D. Ход целиком: правило, сценарии, режим управления")
    from app.features import cc_texts

    for text in ("don't call me buddy", "remember that I'm vegan", "that's not what I meant",
                 "you did it again"):
        r = _outcome(tbm._bot(), text)
        check(f"англ. поправка → извлечение правила: {text!r}",
              r[0] == "STOP" and r[1] == [text] and r[2] == ["Rule: Remember the scenario"])
    for text in ("do you remember what I told you yesterday?", "remember to buy milk",
                 "I remember when we went to Paris", "something went wrong with my laptop"):
        r = _outcome(tbm._bot(), text)
        check(f"обычная речь → без правила: {text!r}", r[0] == "STOP" and r[1] == [])

    # Фото/файл: англ. текст документа правило не запускает, подпись — запускает
    doc = ("The user sent a file 'notes.txt'. Files loaded: 1/5:\n\n"
           "Remember: never commit secrets. Don't use global variables.")
    r = _outcome(tbm._bot(), doc, raw="")
    check("файл без подписи, англ. «Remember:/Don't use» внутри — без правила",
          r[0] == "STOP" and r[1] == [])
    composite = "don't call me buddy\n\n" + doc
    r = _outcome(tbm._bot(), composite, raw="don't call me buddy")
    check("англ. поправка в подписи к файлу — правило (весь ввод — в извлечение)",
          r[1] == [composite])
    ru_doc = "The user sent a file 'z.txt'. Files loaded: 1/5:\n\nЗапомни: я не ем мясо"
    r = _outcome(tbm._bot(), ru_doc, raw="")
    check("файл без подписи, русское «Запомни» внутри — правило, как раньше", r[1] == [ru_doc])

    # Команды сценария вне режима управления — не правило (оба языка)
    hint_ru = cc_texts.t("scenario_outside_mode", "ru")
    en_sc = ("remember this scenario as order pizza", "remember the scenario pizza order",
             "please remember the scenario as order pizza",
             "Please, remember this scenario as morning", "remember please the scenario pizza",
             "Connor, please remember the scenario as order pizza",
             "save the scenario as order pizza", "save this scenario as morning routine",
             "remember the scenario where you are a pirate")
    ru_sc = ("запомни сценарий заказ пиццы", "Запомни, пожалуйста, сценарий заказа пиццы",
             "Коннор, запомни сценарий заказ пиццы", "запомни сценарий:\nоткрой додо\nвыбери пиццу",
             "сохрани сценарий как утро")
    for text in en_sc:
        bot = _sc_bot()
        check(f"команда сценария: {text!r}", bot._is_scenario_save_command(text))
        r = _outcome(bot, text)
        check(f"вне режима, допущен — правило не извлекалось: {text!r}",
              r[1] == [] and r[2] == [] and r[3] == [])
        r = _outcome(_sc_bot(single_user=False), text, user_id="guest")
        check(f"вне режима, недопущен — правило не извлекалось: {text!r}", r[1] == [])
    r = _outcome(_sc_bot(), "запомни сценарий заказ пиццы")
    check("русская команда вне режима — подсказка, как раньше", r[0] == hint_ru and r[1] == [])

    # Исход хода для команд сценария и русских фраз — тот же, что со старой
    # эвристикой: вне режима (допущен/недопущен) и в режиме управления
    kinds = (("вне режима, допущен", dict(), "u1"),
             ("вне режима, недопущен", dict(single_user=False), "guest"),
             ("в режиме управления", dict(control_on=True), "u1"),
             ("недопущен в чате с режимом", dict(single_user=False, control_on=True), "guest"))
    texts = en_sc + ru_sc + ("запомни, я не люблю кофе", "не так, я имел в виду вторник",
                             "ты опять забыл", "привет, как дела?")
    for label, kw, uid in kinds:
        diff = []
        for text in texts:
            new = _outcome(_sc_bot(**kw), text, user_id=uid)
            old = _with_old_hint(lambda: _outcome(_sc_bot(**kw), text, user_id=uid))
            if new != old:
                diff.append((text, old, new))
        check(f"{label}: исход как со старой эвристикой ({len(texts)} фраз)", not diff)
        for text, old, new in diff:
            print(f"      {text!r}: было {old}, стало {new}")

    r = _outcome(_sc_bot(control_on=True), "remember this scenario as order pizza")
    check("в режиме управления англ. команда — запись сценария, без правила",
          r[0] == "Сценарий «order pizza» сохранён." and r[3] == ["order pizza"] and r[1] == [])
    r = _outcome(_sc_bot(control_on=True), "please remember the scenario as order pizza")
    check("в режиме управления вежливая англ. команда — запись сценария, без правила",
          r[1] == [] and r[3] == ["order pizza"])
    r = _outcome(_sc_bot(control_on=True), "don't call me buddy")
    check("в режиме управления англ. поправка — правило, как русская «запомни, …»",
          r[1] == ["don't call me buddy"]
          and _outcome(_sc_bot(control_on=True), "запомни, я не люблю кофе")[1]
          == ["запомни, я не люблю кофе"])


# ════════════ E. Длинный ввод ════════════

def test_long_input():
    import time
    section("E. Длинный ввод — без квадратичного перебора")
    n = 50_000
    cases = {
        "пустые строки": "\n" * n + "x",
        "пустые строки с пробелами": "\n " * (n // 2) + "x",
        "пробелы после «from now on»": "from now on" + " " * n + "x",
        "пробелы после «please»": "please" + " " * n + "x",
        "пробелы после «fix it»": "fix it" + " " * n + "x",
        "сотни «that's wrong» до «?»": "that's wrong " * (n // 13) + "?",
        "сотни «remember,» до «?»": "remember, " * (n // 10) + "?",
    }
    for label, text in cases.items():
        t0 = time.perf_counter()
        bi._looks_like_correction(text, text)
        dt = time.perf_counter() - t0
        check(f"{label} ({len(text)} симв.): {dt * 1000:.0f} мс", dt < 1.0)
    # Пробельные серии в один символ — исход тот же, что у короткой фразы
    check("пустые строки между фразами — граница фразы сохраняется",
          bi._looks_like_correction("remember that I'm vegan\n\n\n\nwhat's for dinner?", None)
          and not bi._looks_like_correction("remember that I'm vegan    what's for dinner?",
                                            None))


# ════════════ F. Обращение в начале без запятой ════════════

def _named_bot(*names, **kw):
    bot = tbm._bot(**kw)
    bot.trigger_words = set(names)
    return bot


def test_address_prefix():
    section("F. Обращение в начале без запятой: правило и имя")
    bot = _named_bot("коннор", "connor")
    for text, want in (("connor call me max", "call me max"),
                       ("Connor remember that I hate emojis", "remember that I hate emojis"),
                       ("hey Connor don't call me buddy", "don't call me buddy"),
                       ("ok connor, dont use emojis", "dont use emojis"),
                       ("Connor - stop using emoji", "stop using emoji"),
                       ("CONNOR!!! you did it again", "you did it again"),
                       ("эй Коннор запомни, я не ем мясо", "запомни, я не ем мясо")):
        check(f"обращение срезано: {text!r} → {want!r}", bot._strip_address(text) == want)
    for text in ("Connors don't use emojis", "Connor's friends call me Max",
                 "Connor-chan call me Max", "hey there, how are you?", "Connor", "hey Connor!",
                 "Коннорище, не так", "my name is Connor"):
        check(f"не обращение — без изменений: {text!r}", bot._strip_address(text) == text)
    check("None и пустая строка — как есть",
          bot._strip_address(None) is None and bot._strip_address("") == "")

    # Ход целиком: правило / имя из фразы с обращением без запятой
    for text in ("Connor remember that I hate emojis", "connor dont use emojis",
                 "hey Connor don't call me buddy", "connor stop calling me buddy",
                 "Connor that's not what I meant"):
        r = _outcome(_named_bot("коннор", "connor"), text)
        check(f"правило: {text!r}", r[1] == [text] and r[2] == ["Rule: Remember the scenario"])
    for text, name in (("connor call me max", "max"), ("Connor call me Max", "Max"),
                       ("hey connor you can call me Alex", "Alex"),
                       ("эй Коннор call me Max", "Max")):
        r = _outcome(_named_bot("коннор", "connor"), text)
        check(f"имя: {text!r} → {name!r}", r[1] == [] and r[2] == [f"Name: {name}"])
    # Имя персоны — подлежащее, не обращение: правила и имени нет
    for text in ("Connor called me Max yesterday", "Connor calls me buddy all the time",
                 "Connor never calls me by my name", "Connor remembers that I'm vegan",
                 "Connors don't use emojis", "Connor's friends call me Max"):
        r = _outcome(_named_bot("коннор", "connor"), text)
        check(f"не обращение — без правила и имени: {text!r}", r[1] == [] and r[2] == [])

    # Имя — целым словом: начало другого слова не срезается
    r = _outcome(_named_bot("don"), "Don't call me buddy")
    check("персона Don: «Don't call me buddy» — правило (имя не срезано из Don't)",
          r[1] == ["Don't call me buddy"])
    r = _outcome(_named_bot("don"), "don call me Max")
    check("персона Don: обращение «don» без запятой — имя Max", r[2] == ["Name: Max"])
    r = _outcome(_named_bot("rem"), "Remember that I'm vegan")
    check("персона Rem: «Remember that I'm vegan» — правило", r[1] == ["Remember that I'm vegan"])
    r = _outcome(_named_bot("cal"), "Call me Max")
    check("персона Cal: имя не срезано из «Call …» — имя Max", r[2] == ["Name: Max"])
    check("персона Al: «Always remember …» — «Al» из «Always» не срезан",
          _named_bot("al")._strip_address("Always remember that I'm vegan")
          == "Always remember that I'm vegan")

    # Имя — обычное слово: модальный глагол без знака — часть фразы
    r = _outcome(_named_bot("will"), "will remember that!")
    check("персона Will: «will remember that!» (= я запомню) — без правила", r[1] == [])
    r = _outcome(_named_bot("will"), "Will never call me buddy again")
    check("персона Will: «Will never call me buddy again» — без правила", r[1] == [])
    r = _outcome(_named_bot("will"), "Will, remember that I'm vegan")
    check("персона Will: «Will, remember that I'm vegan» — правило",
          r[1] == ["Will, remember that I'm vegan"])
    r = _outcome(_named_bot("max"), "max remembers that I'm vegan")
    check("персона Max: «max remembers that …» — без правила", r[1] == [])
    r = _outcome(_named_bot("hope"), "hope you remember that I'm vegan")
    check("персона Hope: «hope you remember that …» — без правила", r[1] == [])

    # Подпись к файлу: обращение срезается только с написанного
    doc = "The user sent a file 'n.txt'. Files loaded: 1/5:\n\nRemember: never commit secrets."
    r = _outcome(_named_bot("connor"), "connor summarize this\n\n" + doc,
                 raw="connor summarize this")
    check("подпись «connor summarize this» к англ. файлу — без правила", r[1] == [])

    # Русские итоги не меняются: без латиницы — то же, что без среза
    corpus = ru_corpus()
    lat = re.compile("[A-Za-z]")
    bot = _named_bot("коннор", "connor", "жабка")
    diff = [t for t in corpus if not lat.search(t)
            and (bi._looks_like_correction(t, bot._strip_address(t))
                 != bi._looks_like_correction(t, t)
                 or bi._extract_alias(t, bot._strip_address(t)) != bi._extract_alias(t, t))]
    check("русские фразы корпуса: правило и имя — как без среза обращения", not diff)
    for t in diff[:10]:
        print(f"      расхождение: {t[:100]!r}")
    # Смешанные строки: расхождение — только там, где фраза начата обращением
    names_re = re.compile(r"\s*(?:(?:hey|hi|ok|okay|so|эй)\b\s*,?\s*)?(?:коннор|connor|жабка)\b",
                          re.IGNORECASE)
    extra = [t for t in corpus if lat.search(t)
             and (bi._looks_like_correction(t, bot._strip_address(t))
                  != bi._looks_like_correction(t, t)
                  or bi._extract_alias(t, bot._strip_address(t)) != bi._extract_alias(t, t))]
    check("смешанные строки: расхождения — только у фраз с обращением в начале",
          all(names_re.match(t) for t in extra))
    for t in extra:
        print(f"      обращение в смешанной строке: {t[:80]!r}")


# ════════════ G. Сценарий в режиме управления: обращение, «пожалуйста» ════════════

class _SMRec(tbm._SM):
    """Заглушка сценариев с записью «начни записывать / отмени запись»."""

    def __init__(self):
        super().__init__()
        self.started = []
        self.stopped = 0

    def record_start(self, chat_id, name=""):
        self.started.append(name)
        return "REC"

    def record_stop(self, chat_id):
        self.stopped += 1
        return "REC_STOP"

    def cancel(self, chat_id):
        return None


def test_scenario_in_mode():
    section("G. Сценарий в режиме управления: обращение и «пожалуйста»")
    from app.features import cc_texts
    hint_ru = cc_texts.t("scenario_outside_mode", "ru")
    cases = (("Коннор, запомни сценарий утро", "утро"),
             ("Коннор запомни сценарий утро", "утро"),
             ("эй Коннор, сохрани сценарий как утро", "утро"),
             ("Запомни, пожалуйста, сценарий утро", "утро"),
             ("Пожалуйста, запомни сценарий утро", "утро"),
             ("Коннор, запомни, пожалуйста, сценарий утро", "утро"),
             ("запомни сценарий утро, пожалуйста", "утро"),
             ("Connor, please remember this scenario as morning", "morning"),
             ("connor save the scenario as morning", "morning"),
             ("save the scenario as morning please", "morning"),
             ("Save this scenario as morning routine, please!", "morning routine"),
             ("hey Connor, remember please the scenario morning", "morning"),
             ("запомни сценарий утро", "утро"),
             ("save the scenario as morning", "morning"))
    for text, name in cases:
        bot = _sc_bot(control_on=True)
        r = _outcome(bot, text)
        check(f"в режиме — сценарий «{name}», без правила: {text!r}",
              r[3] == [name] and r[1] == [] and r[2] == [])
        # Тот же разбор у подсказки вне режима — она не отправит туда, где
        # фраза сценарием не считается
        check(f"вне режима — тоже команда сценария: {text!r}",
              _sc_bot()._is_scenario_save_command(text))
    # Вне режима русская команда с обращением/«пожалуйста» — подсказка, как раньше
    for text in ("Коннор, запомни сценарий утро", "Коннор запомни сценарий утро",
                 "запомни сценарий утро, пожалуйста"):
        r = _outcome(_sc_bot(), text)
        check(f"вне режима — подсказка, без правила: {text!r}", r[0] == hint_ru and r[1] == [])

    # Скобки записи с обращением / «пожалуйста»
    bot = _sc_bot(control_on=True)
    bot.scenario_manager = _SMRec()
    r = _outcome(bot, "Коннор, начни записывать сценарий заказ пиццы")
    check("«Коннор, начни записывать сценарий X» — запись начата",
          r[0] == "REC" and bot.scenario_manager.started == ["заказ пиццы"])
    r = _outcome(bot, "connor, start recording a scenario please")
    check("«connor, start recording a scenario please» — запись без имени",
          r[0] == "REC" and bot.scenario_manager.started[-1] == "")
    r = _outcome(bot, "Коннор, отмени запись, пожалуйста")
    check("«Коннор, отмени запись, пожалуйста» — запись снята",
          r[0] == "REC_STOP" and bot.scenario_manager.stopped == 1)

    # Не команды сценария: просьбы со словом «запомни», имя как начало слова
    r = _outcome(_sc_bot(control_on=True), "Коннор, запомни, я не люблю кофе")
    check("в режиме «Коннор, запомни, я не люблю кофе» — правило, не сценарий",
          r[3] == [] and r[1] == ["Коннор, запомни, я не люблю кофе"])
    r = _outcome(_sc_bot(control_on=True), "Коннорище, запомни сценарий утро")
    check("в режиме «Коннорище, …» — имя не срезано, сценарий не записан", r[3] == [])
    r = _outcome(_sc_bot(control_on=True), "Connors save the scenario as morning")
    check("в режиме «Connors save …» — имя не срезано, сценарий не записан", r[3] == [])


def main():
    test_en_heuristic()
    test_ru_unchanged()
    test_typed_vs_composite()
    test_turns()
    test_long_input()
    test_address_prefix()
    test_scenario_in_mode()
    print(f"\nИтого: {ok + failures} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

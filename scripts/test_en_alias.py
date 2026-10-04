"""Тест «зови меня X» / «call me X» — предпочитаемое имя пользователя
(_extract_alias в app/bot_instance.py).

  A. английские просьбы → одно слово-имя (с титулом: «Dr. Smith»);
  B. английские «не просьбы» → None: отрицание, звонок («call me back /
     tomorrow / a taxi»), местоимения, идиомы «call me crazy», вопросы и
     третьи лица («why do you call me X», «my friends call me X»), «I go by»
     не про имя, обычные реплики;
  C. русский: на примерах результат тот же, что у прежнего _ALIAS_RE, кроме
     явного списка намеренных правок (RU_CHANGED); на корпусе (строки из
     scripts/test_*.py) каждое расхождение — одна из намеренных правок
     (граница слова, стоп-слово, мягкое слово, творительный, отрицание),
     весь список печатается;
  D. русский с отрицанием («не называй меня малышом») → не имя;
  E. английские строки из scripts/test_*.py имени не задают — кроме
     простых просьб («call me Sam», «Please, you can call me Sam.»): другой
     тест может просить имя законно;
  F. место вызова — через _extract_alias(user_input, typed_h), typed_h —
     написанное без обращения к персоне;
  G. английское имя — только из написанного (подписи), не из текста
     файла/OCR; русское — как раньше, по всему вводу;
  H. длинный ввод с тысячами «call me …» / «не зови меня …» — за линейное
     время;
  I. русский глагол — целым словом: «позови / обзови меня …» → None;
     «назови меня», «зовите / называйте меня» — просьба об имени;
  J. русские стоп-слова: «зови меня так / завтра / когда …», «называй меня
     как хочешь / на ты», «зови меня гулять» → None; «просто Саша» → «Саша»;
     имена, похожие на служебные слова (Ник, Люба, Ли), — имена;
  K. творительный → именительный: «называй меня Сашей» → «Саша»,
     «Александром» → «Александр», «Игорем» → «Игорь»; именительный
     («Алексей», «Артём», «Ной») и латиница — как есть, регистр — как написан.

LLM и сеть не зовутся. Запуск: python -m scripts.test_en_alias
"""

import ast
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")

from app.bot_instance import (  # noqa: E402
    _ALIAS_RU_STOP, _alias_ru_name, _alias_ru_nominative, _extract_alias)

ROOT = Path(__file__).parent.parent

# Прежний regex — эталон для русского (копия, чтобы правка _ALIAS_RE не
# сравнивала сама с собой)
OLD_ALIAS_RE = re.compile(r"(?:зови|называй)\s+меня\s+([А-Яа-яЁёA-Za-z\-]{2,30})", re.IGNORECASE)

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


def old_alias(text):
    m = OLD_ALIAS_RE.search(text)
    return m.group(1).strip() if m else None


EN_POSITIVE = [
    ("call me Alex", "Alex"),
    ("Call me Alex.", "Alex"),
    ("Hi! Call me Alex", "Alex"),
    ("please call me Alex", "Alex"),
    ("Please, call me Alex", "Alex"),
    ("pls call me al", "al"),
    ("you can call me Al", "Al"),
    ("You can just call me Al", "Al"),
    ("u can call me jojo", "jojo"),
    ("just call me Sam", "Sam"),
    ("I'd like you to call me Max", "Max"),
    ("I want you to call me Max", "Max"),
    ("I would prefer you to call me Max", "Max"),
    ("I'd prefer it if you'd call me Kate", "Kate"),
    ("I'd rather you call me Kate", "Kate"),
    ("Can you call me Alex?", "Alex"),
    ("Could you please call me Sam from now on?", "Sam"),
    ("Would you please call me Sam?", "Sam"),
    ("from now on, call me Alex", "Alex"),
    ("From now on call me Alex", "Alex"),
    ("Hi, I'm Alexander, but call me Sasha", "Sasha"),
    ("My name is Robert but you can call me Bob", "Bob"),
    ("Everyone calls me Al, so call me Al too", "Al"),
    ("Don't call me Bob, call me Robert", "Robert"),
    ("Not Bob — call me Robert", "Robert"),
    ("call me Alex, but not Al", "Alex"),
    ("Feel free to call me Jo", "Jo"),
    ("how about you call me Max", "Max"),
    ("you'll call me Master", "Master"),
    ("ok so call me Alex", "Alex"),
    ("Hey, call me Kate :)", "Kate"),
    ("call me as Alex", "Alex"),
    ("call me just Alex", "Alex"),
    ("call me O'Neil", "O'Neil"),
    ("call me Mary-Jane", "Mary-Jane"),
    ("call me 'Bob'", "Bob"),
    ('call me "Bob"', "Bob"),
    ("call me Dr. Smith", "Dr. Smith"),
    ("call me Mr  Smith", "Mr Smith"),
    ("call me boss", "boss"),
    ("call me José", "José"),
    ("call me Zoë", "Zoë"),
    ("Call me Imran.", "Imran"),
    ("address me as Sir", "Sir"),
    ("please refer to me as Kim", "Kim"),
    # Похожие на прилагательные имена не режем суффиксом
    ("call me Manish", "Manish"),
    ("call me Danish", "Danish"),
    ("call me Eric", "Eric"),
    ("call me Emily", "Emily"),
    ("call me Clive", "Clive"),
    ("call me Mable", "Mable"),
    ("call me Ahmed", "Ahmed"),
    ("call me May", "May"),
    # «I go by X»
    ("I go by Bob", "Bob"),
    ("Hi, I'm Robert but I go by Bob.", "Bob"),
    ("I go by the name of Bob", "Bob"),
    ("i usually go by Rob these days", "Rob"),
    # Смесь языков: русская просьба впереди английской
    ("don't call me Bob, зови меня Боб", "Боб"),
]

EN_NEGATIVE = {
    "отрицание": [
        "don't call me Bob", "dont call me Bob", "do not call me Bob", "Please don't call me Bob",
        "never call me Bob", "Never, ever call me Bob", "Don't ever call me Bob",
        "stop calling me Bob", "you can't call me Bob", "you shouldn't call me Bob",
        "You will never call me Bob", "Can you not call me Bob", "I don't want you to call me Bob",
        "I'd hate for you to call me Bob", "please do not call me Bob ever again",
    ],
    "звонок": [
        "call me tomorrow", "call me later", "call me back", "call me when you're free",
        "call me if you need anything", "call me at 5", "call me in an hour", "call me on Monday",
        "call me Monday", "call me tonight", "call me soon", "call me now", "call me right now",
        "call me again", "call me sometime", "call me anytime", "call me asap", "call me ASAP",
        "call me maybe", "Call me, maybe?", "call me once you land", "call me tmrw",
        "call me as soon as you can", "call me real quick", "call me first thing",
        "can you call me?", "Can you call me back?", "can you call me up", "call me up later",
        "Can you remind me to call mom", "I'll call me a cab", "remind me to call me",
        "call me taxi", "call me cab please",
    ],
    "артикли и местоимения": [
        "call me a taxi", "call me an ambulance", "call me a cab", "call me the moment it's done",
        "call me that", "call me this", "call me it", "call me whatever you want",
        "call me by my name", "call me names", "call me A", "call me X", "call me Mr",
        "call me Dr. who knows",
    ],
    "идиомы": [
        "call me crazy", "call me crazy, but I like it", "call me old-fashioned",
        "call me old fashioned but I like letters", "call me stupid", "call me paranoid",
        "call me biased", "call me lazy", "call me a pessimist", "call me a nerd",
        "call me naive", "call me superstitious", "call me careful", "call me clueless",
        "call me sensitive", "call me irresponsible", "call me a perfectionist", "call me sexist",
        "call me squeamish, but I hate spiders", "you might call me lucky", "you could call me lucky",
        "Would you call me handsome?",
    ],
    "вопросы и третьи лица": [
        "what should I call you?", "what do you call me?", "What do you call me now?",
        "why do you call me Bob?", "Did you just call me Bob?", "My friends call me Al",
        "They call me the Wolf", "my mom used to call me Bunny", "she would always call me Bunny",
        "Tell him to call me Sam", "I asked her to call me Sam",
        "If you call me Bob again I'll leave", "I like it when you call me Bob",
        "Why don't you call me Bob", "Call me, Alex", "Call me Alex2",
    ],
    "I go by — не имя": [
        "I go by bus", "I go by feel", "I go by the book", "I go by Walmart every day",
        "When I go by Alex's house", "I go by Mike's place", "I go by Starbucks on my way",
        "I go by he/him", "I go by they/them", "i go by bob",
        # транспорт с заглавной (найдено при ревью)
        "I go by Uber.", "i mostly go by Lyft", "I go by Metro.",
    ],
    "обычные реплики": [
        "", "Hello world", "How are you?", "what's your name?", "My name is Alex",
        "I'm Alex", "call me", "I'm not Bob, call me", "open youtube and play music",
        "remind me to call the dentist tomorrow", "I missed your call",
    ],
}

# Русские просьбы без отрицания: результат как у прежнего _ALIAS_RE
RU_EXAMPLES = [
    "зови меня Саша", "зови меня Александр Петрович",
    "Пожалуйста, зови меня Шурик", "называй меня Анна-Мария", "зови меня Max",
    "можешь звать меня Саша", "меня зовут Саша", "да не, зови меня Саша",
    "Мне нравится, когда зови меня Саша", "(зови меня Саша)", "Ладно, зови меня Алексей.",
    "называй меня Алексей", "ЗОВИ МЕНЯ САША", "зови меня Ник", "зови меня Ли",
    "зови меня, пожалуйста, Саша", "называй меня, как хочешь",
    "зови меня Мир", "зови меня Радость", "Зови меня Тёма",
]

# Намеренные правки: (фраза, было, стало) — всё остальное как у прежнего regex
RU_CHANGED = [
    # граница слова: «позови» содержит «зови»
    ("позови меня когда будет готово", "когда", None),
    ("Назови меня Сашей", "Сашей", "Саша"),
    ("Зовите меня Саша", None, "Саша"),
    ("обзови меня дураком", "дураком", None),
    ("незови меня так", "так", None),
    # стоп-слова и инфинитив
    ("зови меня так", "так", None),
    ("зови меня завтра", "завтра", None),
    ("называй меня как хочешь", "как", None),
    ("зови меня потом", "потом", None),
    ("называй меня на ты", "на", None),
    ("зови меня гулять", "гулять", None),
    # мягкое слово перед именем
    ("зови меня просто Саша", "просто", "Саша"),
    ("называй меня лучше Сашей", "лучше", "Саша"),
    # творительный → именительный
    ("Называй меня Сашей", "Сашей", "Саша"),
    ("не надо, называй меня Сашей", "Сашей", "Саша"),
    ("зови меня Александром", "Александром", "Александр"),
    ("называй меня Игорем", "Игорем", "Игорь"),
]

# Русские с отрицанием: прежний regex брал имя — теперь нет
# (или следующую просьбу без отрицания)
RU_NEGATED = [
    ("не называй меня малышом", None),
    ("Не зови меня так", None),
    ("никогда не называй меня малышом", None),
    ("больше не зови меня Сашей", None),
    ("Пожалуйста, не называй меня малыш", None),
    ("НЕ НАЗЫВАЙ МЕНЯ ТАК", None),
    ("незови меня так", None),
    ("Не зови меня Саша, зови меня Шурик", "Шурик"),
]

# I. Глагол целым словом
RU_BOUNDARY_NONE = [
    "позови меня когда будет готово", "Позови меня, когда будет готово", "позови меня Сашей",
    "назови меня как-нибудь", "назови меня по имени", "созови меня всех",
    "обзови меня дураком", "вызови меня завтра", "подзови меня Сашей", "отзови меня",
    "призови меня на помощь", "перезови меня позже", "незови меня так",
]
RU_BOUNDARY_OK = [
    ("Зови меня Саша", "Саша"), ("— зови меня Саша", "Саша"), ("«зови меня Саша»", "Саша"),
    ("ладно,зови меня Саша", "Саша"), ("ок. Называй меня Сашей!", "Саша"),
    ("Привет!\nзови меня Саша", "Саша"), ("позови Олю, а меня зови Саша", None),
    ("позови маму, а потом зови меня Сашей", "Саша"),
    ("Назови меня Сашей", "Саша"), ("зовите меня Саша", "Саша"),
    ("называйте меня Алексеем", "Алексей"), ("назовите меня Олей", "Оля"),
    ("не называйте меня малышом", None), ("не назови меня так", None),
]

# J. Стоп-слова: после «меня» не имя
RU_STOP_NONE = [
    "зови меня так", "Зови меня так же", "зови меня вот так", "зови меня завтра",
    "зови меня сегодня", "зови меня потом", "зови меня позже", "зови меня сейчас же",
    "зови меня всегда", "зови меня когда угодно", "зови меня когда будешь готов",
    "зови меня если что", "зови меня если понадоблюсь", "зови меня чуть что",
    "называй меня как хочешь", "называй меня как угодно", "называй меня иначе",
    "называй меня по-другому", "называй меня по-своему", "называй меня кем хочешь",
    "называй меня кем-нибудь", "называй меня как-нибудь", "называй меня на ты",
    "называй меня на вы", "называй меня Вы", "называй меня ты", "зови меня по имени",
    "зови меня по фамилии", "зови меня тем же именем", "зови меня этим именем",
    "зови меня новым именем",
    "зови меня его именем", "называй меня своим именем", "зови меня полным именем",
    "зови меня именем Саша", "зови меня это", "зови меня тоже", "зови меня ещё раз",
    "зови меня еще раз", "зови меня опять", "зови меня снова", "зови меня обратно",
    "зови меня сюда", "зови меня туда", "зови меня вместе с ними", "зови меня за собой",
    "зови меня с собой", "зови меня в гости", "зови меня к себе", "зови меня на помощь",
    "зови меня при случае", "зови меня из кухни", "зови меня до обеда", "зови меня после обеда",
    "зови меня вечером", "зови меня утром", "зови меня ночью", "зови меня пожалуйста",
    "зови меня лучше потом", "зови меня просто", "зови меня просто так", "зови меня уже",
    "зови меня всё-таки", "зови меня тогда, когда будет время", "зови меня и его",
    "зови меня или его", "зови меня но не его", "зови меня срочно", "зови меня ласково",
    "зови меня нормально", "зови меня гулять", "зови меня обедать", "зови меня кататься",
    "зови меня играть", "зови меня помочь", "зови меня смотреть кино",
]
RU_STOP_OK = [
    # мягкие слова пропускаем, как «just»
    ("зови меня просто Саша", "Саша"), ("называй меня лучше Сашей", "Саша"),
    ("зови меня теперь Алексей", "Алексей"), ("называй меня отныне Шуриком", "Шурик"),
    ("называй меня пожалуйста Машей", "Маша"), ("зови меня всегда Сашей", "Саша"),
    ("зови меня только Саша", "Саша"), ("называй меня уже Сашей", "Саша"),
    ("зови меня тогда Саша", "Саша"), ("называй меня своей девочкой", "девочка"),
    ("зови меня своим котиком", "котик"), ("называй меня просто-напросто Сашкой", "Сашка"),
    ("зови меня теперь просто Саша", "Саша"), ("зови меня так, а лучше зови меня Сашей", "Саша"),
    # имена, похожие на служебные слова, — имена
    ("зови меня Ник", "Ник"), ("зови меня Ником", "Ник"), ("зови меня Любой", "Люба"),
    ("зови меня Ли", "Ли"), ("зови меня Ян", "Ян"), ("зови меня Мир", "Мир"),
    ("зови меня Тата", "Тата"), ("зови меня Ника", "Ника"), ("зови меня Вера", "Вера"),
    ("называй меня Надеждой", "Надежда"), ("называй меня Любовью", "Любовь"),
    ("зови меня Мила", "Мила"), ("зови меня Кира", "Кира"), ("зови меня Ия", "Ия"),
    ("зови меня Тёма", "Тёма"), ("зови меня Радость", "Радость"),
    ("называй меня прелестью", "прелесть"), ("зови меня Хозяин", "Хозяин"),
    ("зови меня малыш", "малыш"), ("зови меня Alex", "Alex"),
]

# K. Творительный → именительный (проверяем с обоими глаголами)
RU_INSTR = [
    # жен. склонение, основа на шипящий/ц → «-а»
    ("Сашей", "Саша"), ("Машей", "Маша"), ("Наташей", "Наташа"), ("Дашей", "Даша"),
    ("Пашей", "Паша"), ("Мишей", "Миша"), ("Гришей", "Гриша"), ("Ксюшей", "Ксюша"),
    ("Гошей", "Гоша"), ("Лёшей", "Лёша"), ("Серёжей", "Серёжа"), ("Сережей", "Сережа"),
    ("Красавицей", "Красавица"), ("Умницей", "Умница"),
    # остальные «-ей / -ёй» → «-я»
    ("Олей", "Оля"), ("Катей", "Катя"), ("Петей", "Петя"), ("Ваней", "Ваня"), ("Женей", "Женя"),
    ("Настей", "Настя"), ("Костей", "Костя"), ("Таней", "Таня"), ("Юлей", "Юля"),
    ("Зоей", "Зоя"), ("Майей", "Майя"), ("Марией", "Мария"), ("Юлией", "Юлия"),
    ("Натальей", "Наталья"), ("Софьей", "Софья"), ("Ильёй", "Илья"), ("Ильей", "Илья"),
    ("Надей", "Надя"), ("Бабулей", "Бабуля"),
    # «-ой» → «-а»
    ("Мариной", "Марина"), ("Анной", "Анна"), ("Леной", "Лена"), ("Ольгой", "Ольга"),
    ("Любой", "Люба"), ("Верой", "Вера"), ("Лерой", "Лера"), ("Никитой", "Никита"),
    ("Фомой", "Фома"), ("Кузьмой", "Кузьма"), ("Лукой", "Лука"), ("Дашенькой", "Дашенька"),
    ("Сашкой", "Сашка"), ("зайкой", "зайка"), ("малышкой", "малышка"),
    ("Госпожой", "Госпожа"), ("Королевой", "Королева"), ("Принцессой", "Принцесса"),
    # «-ом» → без окончания (и беглая гласная)
    ("Александром", "Александр"), ("Шуриком", "Шурик"), ("Максимом", "Максим"),
    ("Иваном", "Иван"), ("Кириллом", "Кирилл"), ("Марком", "Марк"), ("Максом", "Макс"),
    ("Артёмом", "Артём"), ("Артемом", "Артем"), ("Томом", "Том"), ("Петром", "Петр"),
    ("Димоном", "Димон"), ("Павлом", "Павел"), ("Львом", "Лев"), ("Сашком", "Сашок"),
    ("Саньком", "Санёк"), ("Ваньком", "Ванёк"), ("малышом", "малыш"), ("котиком", "котик"),
    ("котёнком", "котёнок"), ("дружком", "дружок"), ("ангелочком", "ангелочок"),
    ("солнышком", "солнышко"), ("Хозяином", "Хозяин"), ("Боссом", "Босс"),
    ("Кузьмичом", "Кузьмич"), ("отцом", "отец"),
    # «-ем / -ём»: основа на гласную → «-й», на шипящий → без окончания, иначе → «-ь»
    ("Алексеем", "Алексей"), ("Андреем", "Андрей"), ("Сергеем", "Сергей"),
    ("Матвеем", "Матвей"), ("Юрием", "Юрий"), ("Дмитрием", "Дмитрий"),
    ("Евгением", "Евгений"), ("Николаем", "Николай"), ("Игорем", "Игорь"),
    ("Тёмычем", "Тёмыч"), ("Ильичём", "Ильич"), ("Королём", "Король"), ("Царём", "Царь"),
    ("Князем", "Князь"), ("Повелителем", "Повелитель"), ("героем", "герой"),
    ("Принцем", "Принц"), ("Красавцем", "Красавец"), ("солнцем", "солнце"),
    ("счастьем", "счастье"), ("дружищем", "дружище"),
    # «-ью» → «-ь»
    ("Любовью", "Любовь"), ("Аделью", "Адель"), ("прелестью", "прелесть"),
    # ласковые прилагательные
    ("любимой", "любимая"), ("любимым", "любимый"), ("единственной", "единственная"),
    ("хорошей", "хорошая"), ("милым", "милый"), ("родным", "родной"), ("дорогим", "дорогой"),
    # двойные имена, регистр
    ("Анной-Марией", "Анна-Мария"), ("Жан-Полем", "Жан-Поль"),
    ("сашей", "саша"), ("САШЕЙ", "САША"), ("ПАВЛОМ", "ПАВЕЛ"), ("алексеем", "алексей"),
]
# Уже именительный, хотя кончается как творительный, — как есть
RU_NOM_LOOKALIKE = [
    "Алексей", "Андрей", "Сергей", "Матвей", "Тимофей", "Елисей", "Гордей", "Евсей",
    "Корней", "Моисей", "Фаддей", "Авдей", "Ерофей", "Макей", "Еремей", "Пантелей",
    "Артём", "Артем", "Ефрем", "Рустем", "Ной", "Рой", "Трой", "Том", "Ром", "Джей", "Грей",
    "Рей", "Кей", "Бахром", "Герой", "Ковбой", "Малой", "Большой", "Дорогой", "Родной",
    "алексей", "АНДРЕЙ",
]
# Именительный — как есть
RU_NOMINATIVE = """
Александр Анатолий Антон Аркадий Арсений Артур Богдан Борис Вадим Валентин Валерий Василий
Виктор Виталий Владимир Владислав Всеволод Вячеслав Геннадий Георгий Глеб Григорий Давид
Даниил Денис Дмитрий Евгений Егор Иван Игорь Илья Кирилл Константин Лев Леонид Максим Марк
Михаил Никита Николай Олег Павел Пётр Петр Платон Роман Руслан Савелий Семён Станислав
Степан Тимур Фёдор Филипп Эдуард Юрий Ярослав Мирон Ефим Захар Тихон Макар Святослав Лука
Фома Кузьма Савва Ким Саша Маша Даша Наташа Паша Миша Гриша Лёша Серёжа Женя Оля Катя Петя
Ваня Таня Аня Настя Костя Соня Юля Анна Мария Елена Ольга Наталья Татьяна Ирина Светлана
Екатерина Анастасия Юлия Дарья Ксения Полина Алина Виктория Вера Надежда Любовь Люба Тата
Ника Вероника Мила Людмила Лера Зоя Майя Ия Лия Яна Жанна Арина Алиса Ева Злата Ульяна
Варвара Софья Кира Нинель Адель Ассоль Шурик Сашок Санёк котик малыш Max Alex
""".split()


def script_literals():
    """Строковые литералы из scripts/test_*.py (кроме этого файла)."""
    ru, en = set(), set()
    cyr = re.compile(r"[А-Яа-яЁё]")
    for p in sorted((ROOT / "scripts").glob("test_*.py")):
        if p.name == Path(__file__).name:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip():
                (ru if cyr.search(node.value) else en).add(node.value)
    return ru, en


def en_case():
    section("A. английские просьбы → имя")
    for text, expected in EN_POSITIVE:
        got = _extract_alias(text)
        check(f"{text!r} → {expected!r} (получено {got!r})", got == expected)

    section("B. английские «не просьбы» → None")
    for group, texts in EN_NEGATIVE.items():
        print(f"  · {group}")
        for text in texts:
            got = _extract_alias(text)
            check(f"{text!r} → None (получено {got!r})", got is None)


# Мягкие слова перед именем (копия списка из _ALIAS_RE)
RU_SOFT = frozenset("просто просто-напросто лучше теперь отныне впредь всегда только уже уж "
                    "тогда пожалуйста плиз плз пж пжл пжлст своей своим".split())


def ru_change_reason(text, old, new):
    """Почему результат разошёлся с прежним regex; None — непредусмотренно."""
    m = OLD_ALIAS_RE.search(text)
    if not m:
        # прежний regex вежливое «зовите / называйте / назовите» не знал
        if new and re.search(r"(?<![^\W\d_])(?:зови|называй|назови)те\s+меня", text, re.IGNORECASE):
            return "вежливая форма"
        return None
    if m.start() and text[m.start() - 1].isalpha():
        return "граница слова"
    if re.search(r"(?<![^\W\d_])не\s*$", text[:m.start()], re.IGNORECASE):
        return "отрицание"
    if old.lower() in RU_SOFT:
        return "мягкое слово"
    if _alias_ru_name(old) is None:
        return "стоп-слово"
    if new != old and new == _alias_ru_nominative(old):
        return "творительный"
    return None


def ru_case(ru_literals):
    section("C. русский: как прежний _ALIAS_RE, кроме намеренных правок")
    for text in RU_EXAMPLES:
        got, old = _extract_alias(text), old_alias(text)
        check(f"{text!r} → {old!r} (получено {got!r})", got == old)
    print("  · намеренные правки")
    for text, was, expected in RU_CHANGED:
        old, got = old_alias(text), _extract_alias(text)
        reason = ru_change_reason(text, old, got)
        check(f"{text!r}: было {old!r}, стало {got!r} ({reason})",
              old == was and got == expected and reason)
    corpus = sorted(ru_literals)
    diffs = [(t, old_alias(t), _extract_alias(t)) for t in corpus
             if old_alias(t) != _extract_alias(t)]
    if not diffs:
        print("    расхождений с прежним regex в корпусе нет")
    unexplained = []
    for t, old, got in diffs:
        reason = ru_change_reason(t, old, got) if old else None
        print(f"    расхождение: {t[:80]!r}: было {old!r}, стало {got!r} — {reason or '???'}")
        if not reason:
            unexplained.append(t)
    check(f"корпус из {len(corpus)} русских строк scripts/test_*.py: расхождений {len(diffs)}, "
          f"все — намеренные правки", not unexplained)

    section("D. русский с отрицанием → не имя")
    for text, expected in RU_NEGATED:
        old, got = old_alias(text), _extract_alias(text)
        check(f"{text!r}: было {old!r}, стало {got!r} (ждём {expected!r})",
              old is not None and got == expected)


# Простая просьба целиком: «call me Sam», «Hi! Please, you can call me Sam.».
# Такая строка в другом тесте — законная проверка имени, а не ложное
# срабатывание; имя из любой другой (длинной, составной) строки — подозрительно
_PLAIN_REQUEST_RE = re.compile(
    r"\s*(?:(?:hi|hey|hello|ok|okay|so|well|please|pls|just|you\s+can|you\s+may|"
    r"from\s+now\s+on)\W+)*(?:call\s+me|i\s+go\s+by|refer\s+to\s+me\s+as|address\s+me\s+as)\s+"
    r"(?:just\s+)?[\"'“‘]?"
    r"(?P<name>(?:(?:mr|mrs|ms|mx|dr|prof)\.?\s+)?[^\W\d_]+(?:['’\-][^\W\d_]+)*)[\"'”’]?"
    r"(?:\s*,?\s*(?:please|from\s+now\s+on))?[\s.!:)]*",
    re.IGNORECASE)


def en_corpus_case(en_literals):
    section("E. английские строки scripts/test_*.py: имя — только из простой просьбы")
    hits = [(t, _extract_alias(t)) for t in sorted(en_literals)]
    hits = [(t, a) for t, a in hits if a]
    plain = [(t, a) for t, a in hits
             if (m := _PLAIN_REQUEST_RE.fullmatch(t)) and " ".join(m.group("name").split()) == a]
    bad = [(t, a) for t, a in hits if (t, a) not in plain]
    for t, a in plain[:10]:
        print(f"    простая просьба в другом тесте: {t[:80]!r} → {a!r}")
    for t, a in bad[:10]:
        print(f"    срабатывание: {t[:80]!r} → {a!r}")
    check(f"корпус из {len(en_literals)} английских строк — имени нет нигде, кроме "
          f"простых просьб ({len(plain)})", not bad)
    check("простая просьба распознаётся, составная строка — нет",
          _PLAIN_REQUEST_RE.fullmatch("Hi! Please, you can call me Sam.").group("name") == "Sam"
          and _PLAIN_REQUEST_RE.fullmatch("call me Dr. Smith").group("name") == "Dr. Smith"
          and _PLAIN_REQUEST_RE.fullmatch("call me O'Neil please").group("name") == "O'Neil"
          and not _PLAIN_REQUEST_RE.fullmatch("Everyone calls me Al, so call me Al too")
          and not _PLAIN_REQUEST_RE.fullmatch("help me write a bio: you can call me Davey"))


def wiring_case():
    section("F. место вызова")
    src = (ROOT / "app" / "bot_instance.py").read_text(encoding="utf-8")
    check("имя берётся через _extract_alias(user_input, typed_h)",
          "alias = _extract_alias(user_input, typed_h)" in src)
    check("typed_h — написанное без обращения к персоне",
          "typed_h = self._strip_address(raw_user_text)" in src)
    check("прямого _ALIAS_RE.search в обработке нет", "_ALIAS_RE.search(" not in src)


def typed_case():
    section("G. английское имя — только из написанного, не из файла/OCR")
    doc = ("The user sent a file 'letter.txt'. Files loaded: 1/5:\n\n"
           "Hi team, I'm the new manager. You can call me Dave.")
    check("англ. «call me Dave» в тексте файла без подписи — не имя",
          _extract_alias(doc, "") is None)
    check("нейтральная подпись + англ. текст файла — не имя",
          _extract_alias("summarize this\n\n" + doc, "summarize this") is None)
    check("англ. просьба в подписи к файлу — имя из подписи",
          _extract_alias("call me Sam\n\n" + doc, "call me Sam") == "Sam")
    ocr = ("The user sent an image. Its contents according to the vision model:\n"
           "A chat screenshot: just call me Jess")
    check("англ. OCR без подписи — не имя", _extract_alias(ocr, "") is None)
    ru_doc = "The user sent a file 'z.txt'. Files loaded: 1/5:\n\nзови меня Саша"
    check("русское в тексте файла — как раньше (по всему вводу)",
          _extract_alias(ru_doc, "") == old_alias(ru_doc) == "Саша")
    check("typed=None — смотрим на сам текст", _extract_alias("call me Sam", None) == "Sam")


def long_input_case():
    import time
    section("H. длинный ввод — без квадратичного перебора")
    n = 50_000
    cases = {
        "«call me x» × тысячи": "call me x " * (n // 10),
        "«my friends call me Al» × тысячи": "my friends call me Al " * (n // 22),
        "«I go by bus» × тысячи": "I go by bus " * (n // 12),
        "«не зови меня Саша» × тысячи": "не зови меня Саша " * (n // 18),
        "«зови меня так» × тысячи": "зови меня так " * (n // 14),
        "«зови меня просто …» × тысячи": "зови меня " + "просто " * (n // 7),
        "«позови меня Сашей» × тысячи": "позови меня Сашей " * (n // 18),
        "пробелы после имени": "call me Bob" + " " * n + "x",
    }
    for label, text in cases.items():
        t0 = time.perf_counter()
        _extract_alias(text)
        dt = time.perf_counter() - t0
        check(f"{label} ({len(text)} симв.): {dt * 1000:.0f} мс", dt < 1.0)
    check("вводная часть длиннее окна — не просьба",
          _extract_alias("please " * 100 + "call me Sam") is None
          and _extract_alias("x. " + "please " * 3 + "call me Sam") == "Sam")


def ru_boundary_case():
    section("I. русский глагол — целым словом")
    for text in RU_BOUNDARY_NONE:
        got = _extract_alias(text)
        check(f"{text!r} → None (получено {got!r})", got is None)
    for text, expected in RU_BOUNDARY_OK:
        got = _extract_alias(text)
        check(f"{text!r} → {expected!r} (получено {got!r})", got == expected)


def ru_stop_case():
    section("J. русские стоп-слова и мягкие слова")
    for text in RU_STOP_NONE:
        got = _extract_alias(text)
        check(f"{text!r} → None (получено {got!r})", got is None)
    for text, expected in RU_STOP_OK:
        got = _extract_alias(text)
        check(f"{text!r} → {expected!r} (получено {got!r})", got == expected)
    check("мягкое слово без имени после него — тоже не имя",
          all(_alias_ru_name(w) is None for w in RU_SOFT))
    names = {n.lower() for n in RU_NOMINATIVE} | {n.lower() for n, _ in RU_INSTR}
    clash = sorted(names & _ALIAS_RU_STOP)
    check(f"ни одно имя из тестов не стоп-слово ({clash})", not clash)


def ru_instr_case():
    section("K. русский творительный → именительный")
    for verb in ("называй", "зови"):
        print(f"  · «{verb} меня …»")
        for word, expected in RU_INSTR:
            got = _extract_alias(f"{verb} меня {word}")
            check(f"{word!r} → {expected!r} (получено {got!r})", got == expected)
    print("  · именительный, похожий на творительный, — как есть")
    for word in RU_NOM_LOOKALIKE:
        got = (_extract_alias(f"зови меня {word}"), _extract_alias(f"называй меня {word}"))
        check(f"{word!r} (получено {got!r})", got == (word, word))
    bad = [(w, _extract_alias(f"зови меня {w}"), _extract_alias(f"называй меня {w}"))
           for w in RU_NOMINATIVE]
    bad = [b for b in bad if b[1:] != (b[0], b[0])]
    for b in bad:
        print(f"    изменилось: {b}")
    check(f"{len(RU_NOMINATIVE)} имён в именительном — без изменений, с обоими глаголами",
          not bad)
    # Именительный из таблицы сам не меняется (повторное применение — без порчи)
    again = [(w, n, _alias_ru_nominative(n)) for w, n in RU_INSTR if _alias_ru_nominative(n) != n]
    for b in again:
        print(f"    повторно: {b}")
    check("результат перевода в именительный устойчив", not again)


def main():
    ru_literals, en_literals = script_literals()
    en_case()
    ru_case(ru_literals)
    en_corpus_case(en_literals)
    wiring_case()
    typed_case()
    long_input_case()
    ru_boundary_case()
    ru_stop_case()
    ru_instr_case()
    print(f"\nИтого: {ok + failures} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

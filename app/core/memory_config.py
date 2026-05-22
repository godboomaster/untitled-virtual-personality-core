import re


# Категории важных фактов
# Используются только как документация и подсказка в промпте.
# Логику распознавания ведёт LLM через примеры — не ключевые слова.

FACT_CATEGORIES = {
    # Личная информация
    "Name": ["my name is", "i'm called", "меня зовут", "моё имя", "name is"],
    "Age": ["age", "лет", "years old", "мне ", "возраст", "born in"],
    "Gender": ["пол", "male", "female", "мужской", "женский", "I'm a man", "I'm a woman"],
    
    # География (UPDATE — заменяется)
    "City": ["from ", "live in", "из ", "живу в", "город", "city", "Moscow", "London"],
    "Country": ["country", "страна", "from Russia", "from Ukraine", "from Kazakhstan"],
    
    # Работа и учёба (UPDATE — заменяется)
    "Profession": [
        "работаю", "work as", "engineer", "developer", "programmer",
        "designer", "manager", "врач", "учитель", "студент"
    ],
    "Company": ["works at", "работаю в", "company", "study at", "учусь в"],
    "Workplace": ["офис", "office", "remote", "удалённо", "home office"],
    
    # Хобби — дробные подкатегории (APPEND — накапливаются)
    "Hobby_music": ["играю на", "play guitar", "play piano", "гитара", "пианино", "барабаны"],
    "Hobby_reading": ["читаю", "reading", "книги", "books", "манга", "manga"],
    "Hobby_creative": ["рисую", "drawing", "фото", "photography", "writing", "пишу"],
    "Hobby_outdoors": ["hiking", "кемпинг", "fishing", "рыбалка", "походы"],
    "Hobby_tech": ["программирую", "coding", "электроника", "electronics", "Arduino"],
    "Hobby_gaming": ["играю в", "gaming", "videogames", "настолки", "board games"],
    "Hobby_cooking": ["готовлю", "cooking", "кулинария", "baking", "выпекаю"],
    "Hobby_fitness": ["йога", "yoga", "медитация", "meditation", "stretching"],
    
    # Навыки — дробные подкатегории (APPEND — накапливаются)
    "Skills_tech": ["Python", "JavaScript", "умею программировать", "владею", "Docker", "SQL"],
    "Skills_languages": ["говорю на", "speak", "язык", "language", "английский", "японский"],
    "Skills_creative": ["дизайн", "design", "монтаж", "video editing", "иллюстрация"],
    
    # Остальные предпочтения (APPEND — накапливаются)
    "Food": ["love food", "люблю еду", "favorite food", "обожаю", "пицца", "pizza", "кушать"],
    "Music": ["love music", "люблю музыку", "favorite band", "слушаю", "music genre"],
    "Movies": ["love movies", "люблю фильмы", "favorite movie", "кино", "film"],
    "Books": ["love books", "люблю книги", "favorite book"],
    "Games": ["люблю игры", "gaming", "играю в игры", "videogames"],
    "Sports": ["спорт", "sport", "fitness", "gym", "бег", "football", "swimming"],
    
    # Отношения
    "Family": ["семья", "family", "wife", "husband", "children", "жена", "муж", "дети", "есть сын", "есть дочь"],
    "Pets": ["питомец", "pet", "cat", "dog", "кот", "собака", "есть кот", "есть собака"],
    
    # Планы и цели
    "Goals": ["хочу", "want to", "planning to", "планирую", "цель", "goal", "мечтаю"],
    "Dreams": ["мечта", "dream", "wish", "желаю"],
    
    # Финансы
    "Income": ["зарплата", "salary", "income", "доход", "earn"],
    "Expenses": ["расходы", "expenses", "spend on", "трачу на"],
    
    # Здоровье
    "Health": ["здоровье", "health", "allergy", "аллергия", "diabetes", "диабет"],
}

# =============================================================================
# ПРИМЕРЫ ДЛЯ ПРОМПТА
# =============================================================================

POSITIVE_EXAMPLES = {
    # Прямые факты
    "Меня зовут Александр": "Name: Alexander",
    "Мне 25 лет": "Age: 25",
    "Я программист из Москвы": "Profession: programmer, City: Moscow",
    "Работаю в Google инженером": "Company: Google, Profession: engineer",
    "Люблю пиццу и пасту": "Food: pizza, pasta",
    "Обожаю играть на гитаре": "Hobby_music: guitar",
    "У меня есть кот и собака": "Pets: cat, dog",
    "Женат, двое детей": "Family: married, 2 children",
    "Хочу выучить Python": "Goals: learn Python",
    "Занимаюсь бегом по утрам": "Sports: running",

    # Косвенные формулировки
    "Переехал в Берлин год назад": "City: Berlin",
    "Работаю на себя, фриланс": "Profession: freelancer",
    "Снял квартиру в Питере": "City: Saint Petersburg",
    "Только что устроился в Яндекс": "Company: Yandex",

    # Факты смешанные с болтовнёй
    "Спасибо! Кстати, я вегетарианец": "Food: vegetarian",
    "Ладно, вернёмся к теме — у меня двое котов": "Pets: 2 cats",

    # Неявные факты
    "Сегодня снова не выспался, дети орут": "Family: has children",
    "Гитара лежит без дела уже месяц": "Hobby_music: guitar",

    # Подкатегории хобби — каждое в свою
    "Недавно начал рисовать и ещё увлёкся фотографией": "Hobby_creative: drawing, photography",
    "Читаю фантастику и иногда мангу": "Hobby_reading: sci-fi, manga",
    "Кодю по вечерам и паяю Arduino": "Hobby_tech: coding, Arduino",
    "Хожу в походы и на рыбалку": "Hobby_outdoors: hiking, fishing",
    "Играю на пианино и барабанах": "Hobby_music: piano, drums",

    # Подкатегории навыков
    "Знаю Python и Docker": "Skills_tech: Python, Docker",
    "Говорю на английском и японском": "Skills_languages: English, Japanese",
    "Делаю монтаж видео и дизайн": "Skills_creative: video editing, design",
}

NEGATIVE_EXAMPLES = {
    "Привет, как дела?": "[NO_FACTS]",
    "Какая сегодня погода?": "[NO_FACTS]",
    "Спасибо за помощь!": "[NO_FACTS]",
    "До свидания!": "[NO_FACTS]",
    "Который сейчас час?": "[NO_FACTS]",
    "Ты кто?": "[NO_FACTS]",
    "Как тебя зовут?": "[NO_FACTS]",
    # Контекстные сообщения без личных фактов
    "Меня прислал сюда начальник, чтобы разобраться с задачей": "[NO_FACTS]",
    "Это странная ситуация, не знаю что делать": "[NO_FACTS]",
    # Имена без прямого самообъявления — НЕ извлекать
    "Илья, помоги мне с этим": "[NO_FACTS]",
    "Спроси у Дениса, он знает": "[NO_FACTS]",
    "Вчера общался с Катей по телефону": "[NO_FACTS]",
    "Антон сказал что будет позже": "[NO_FACTS]",
    "А вот Макс считает иначе": "[NO_FACTS]",
    "Передай Лене что я звонил": "[NO_FACTS]",
    # Частичные факты — только то что известно, остальное не упоминается
    "Меня зовут Иван, живу в Казани": "Name: Ivan, City: Kazan",
    # (НЕ добавляй Music: none, Pets: none и т.д. — просто пропусти их)
    
}

# =============================================================================
# НАСТРОЙКИ ГЕНЕРАЦИИ ПРОМПТА
# =============================================================================

PROMPT_SETTINGS = {
    "temperature": 0.1,
    "max_tokens": 150,
    "answer_format": "Category: value, Category: value",
    "no_facts_marker": "[NO_FACTS]",
    "response_language": "English",
}

# Значения которые считаются «пустыми» и не сохраняются
EMPTY_VALUES = {
    "unknown", "none", "n/a", "not mentioned", "no", "not",
    "not specified", "not stated", "not provided", "not known",
    "нет", "не указано", "неизвестно", "не знаю",
    "", "-", "—", "?", "null", "nil", "na", "n/a",
    "[no_facts]", "no_facts", "nofacts",
}

# =============================================================================
# ФУНКЦИЯ ГЕНЕРАЦИИ ПРОМПТА
# =============================================================================

def build_extraction_prompt(user_message: str, stm_context: str = None) -> str:
    """
    Строит промпт для извлечения фактов.

    Args:
        user_message: Сообщение пользователя для анализа
        stm_context: Контекст из краткосрочной памяти (последние сообщения)

    Ключевые принципы:
    - Категории передаются одной строкой-подсказкой, не списком
    - Явный запрет писать "unknown" и заполнять пустые категории
    - Примеры включают контекстные сообщения без фактов (NEGATIVE)
    """
    pos_examples = "\n".join([
        f'  "{msg}" → {fact}'
        for msg, fact in POSITIVE_EXAMPLES.items()
    ])
    neg_examples = "\n".join([
        f'  "{msg}" → {fact}'
        for msg, fact in NEGATIVE_EXAMPLES.items()
    ])

    # Категории — только подсказка, не шаблон для заполнения
    categories_hint = ", ".join(FACT_CATEGORIES.keys())

    # Добавляем контекст STM если есть
    context_section = ""
    if stm_context:
        context_section = f"""RECENT CONVERSATION CONTEXT (helps understand the full dialogue):
{stm_context}

"""

    prompt = f"""You are a fact extractor for long-term memory of an AI assistant.

TASK: Find ONLY personal facts that are EXPLICITLY present in the LAST MESSAGE from the user.

POSSIBLE CATEGORIES (use only relevant ones): {categories_hint}

{context_section}STRICT RULES:
- Extract facts ONLY from the last user message — NOT from the conversation context!
- The conversation context is ONLY for understanding pronouns and references
- Include a category ONLY if the fact is clearly stated or strongly implied
- !! NEVER write "none", "no", "unknown", "not mentioned" — SKIP the category entirely !!
- !! Output ONLY categories that have real values — nothing else !!
- Each category must appear AT MOST ONCE
- If NO facts found → write ONLY: [NO_FACTS]
- Ignore opinions about external things, questions, weather, time
- Extract facts even if phrased indirectly ("moved to Berlin" → City: Berlin)
- Extract facts even if mixed with small talk ("thanks, btw I'm a doctor" → Profession: doctor)
- Use the conversation context ONLY to understand pronouns and implied facts — do NOT extract facts from it

SUBCATEGORY RULE (IMPORTANT):
- Use SPECIFIC subcategories instead of broad ones:
  Hobby_music, Hobby_reading, Hobby_creative, Hobby_outdoors, Hobby_tech, Hobby_gaming, Hobby_cooking, Hobby_fitness
  Skills_tech, Skills_languages, Skills_creative
- NEVER use plain "Hobby" or plain "Skills" — always pick the specific subcategory
- Examples: "играю на гитаре" → Hobby_music, "читаю книги" → Hobby_reading, "знаю Python" → Skills_tech

NAME RULE (CRITICAL):
- Extract Name ONLY when the user EXPLICITLY states their OWN name or someone else's name as a fact
- Valid: "меня зовут X", "моё имя X", "my name is X", "I'm called X", "that person's name is X", "вот этого человека зовут X"
- DO NOT extract Name when a name is just mentioned in conversation:
  "Илья, помоги" → [NO_FACTS], NOT Name: Илья
  "Спроси Дениса" → [NO_FACTS], NOT Name: Денис
  "Антон сказал что будет" → [NO_FACTS], NOT Name: Антон
- When in doubt about a name → do NOT extract it

"WRONG output (never do this):\n"
"  Name: Ivan, Pets: No_pets, Music: not mentioned, Goals: unknown\n"
"  Hobby: guitar, reading, yoga\n\n"

CORRECT output for same input (only real facts, specific subcategories):
  Name: Ivan, Hobby_music: guitar, Hobby_reading: reading, Hobby_fitness: yoga

FORMAT: Category: value, Category: value

EXAMPLES — extract facts:
{pos_examples}

EXAMPLES — no facts:
{neg_examples}

Message: "{user_message}"
Answer:"""

    return prompt


# =============================================================================
# ФУНКЦИЯ ПАРСИНГА И ФИЛЬТРАЦИИ ФАКТОВ
# =============================================================================

def parse_and_filter_facts(raw: str) -> dict:
    """
    Парсит строку фактов от LLM, выбрасывает пустышки, мусор, дубли и паттерны No_*.
    """
    if not raw:
        return {}
    
    stripped = raw.strip()
    if stripped == PROMPT_SETTINGS["no_facts_marker"]:
        return {}
    
    facts = {}
    for part in stripped.split(","):
        part = part.strip().rstrip(",")
        if ":" not in part:
            continue
        
        key, _, value = part.partition(":")
        key = key.strip()
        value = value.strip()
        
        if not key or not value:
            continue
            
        # 1. Точные совпадения с маркерами пустоты
        if value.lower() in EMPTY_VALUES:
            continue

        # 2. Безопасная проверка на префиксы отсутствия факта
        # Ловит: "No_pets", "not mentioned", "unknown", "n/a", "нет", "не указано"
        # НЕ ловит: "North", "Noah", "Norway" (нет пробела/подчёркивания после "no")
        neg_prefixes = ("no ", "no_", "not ", "not_", "unknown", "n/a", "none", 
                        "нет", "не ", "неизвестно", "не указано", "null")
        if value.lower().startswith(neg_prefixes):
            continue
            
        # 3. Если маркер отсутствия встречается внутри значения (на всякий случай)
        if re.search(r'\bno_\w+', value, re.IGNORECASE) or re.search(r'\bnot_\w+', value, re.IGNORECASE):
            continue
            
        # 4. Старые проверки для совместимости
        if any(marker in value.lower() for marker in ["no_facts", "not mentioned", "not known"]):
            continue
            
        # Дедупликация: первое вхождение побеждает
        if key not in facts:
            facts[key] = value
            
    return facts


# =============================================================================
# ФУНКЦИЯ ПРОВЕРКИ НА ИГНОРИРОВАНИЕ
# =============================================================================

# =============================================================================
# КАТЕГОРИИ: ОБНОВЛЕНИЕ vs НАКОПЛЕНИЕ
# =============================================================================

# Факты, которые ЗАМЕНЯЮТСЯ при повторе (одно актуальное значение)
UPDATE_CATEGORIES = {
    "City", "Profession", "Company", "Age", "Workplace", "Country",
    "Gender", "Name", "Income", "Expenses",
}

# Факты, которые НАКАПЛИВАЮТСЯ (может быть несколько значений)
APPEND_CATEGORIES = {
    # Хобби — подкатегории
    "Hobby_music", "Hobby_reading", "Hobby_creative", "Hobby_outdoors",
    "Hobby_tech", "Hobby_gaming", "Hobby_cooking", "Hobby_fitness",
    # Навыки — подкатегории
    "Skills_tech", "Skills_languages", "Skills_creative",
    # Остальные
    "Pets", "Food", "Music", "Movies", "Books", "Games", "Sports",
    "Family", "Goals", "Dreams", "Health",
}

MERGE_SETTINGS = {
    "temperature": 0.1,
    "max_tokens": 100,
}


def build_merge_prompt(category: str, existing: str, new_value: str) -> str:
    """
    Промпт для умного слияния фактов категории APPEND.

    LLM получает старое и новое значение и решает:
    - Добавить новое к старому
    - Убрать часть старого (пользователь передумал)
    - Заменить полностью (если противоречие)
    """
    return f"""You are merging values for the "{category}" category in an AI assistant's long-term memory.

Existing value: {existing}
New information: {new_value}

Rules:
- Combine existing and new into a single concise value
- Remove duplicates
- If the user explicitly says they NO LONGER like/have/do something — REMOVE it
- If the new information contradicts part of the existing value, trust the NEW information
- Keep the result short and comma-separated

Output ONLY the final merged value. No category prefix, no explanation, no quotes.

Examples:
  Existing: pizza, pasta | New: sushi → pizza, pasta, sushi
  Existing: cat, dog | New: dog died → cat
  Existing: guitar, piano | New: piano, drums → guitar, piano, drums
  Existing: football | New: don't like football anymore, now into swimming → swimming

Merged value:"""


# =============================================================================
# SUMMARIZATION — ПЕРИОДИЧЕСКАЯ ОЧИСТКА LTM
# =============================================================================

SUMMARY_SETTINGS = {
    "temperature": 0.2,
    "max_tokens": 300,
    "trigger_every": 20,     # каждые N сообщений пользователя
}

UPDATE_CATEGORIES_FOR_SUMMARY = sorted(UPDATE_CATEGORIES)
APPEND_CATEGORIES_FOR_SUMMARY = sorted(APPEND_CATEGORIES)


def build_summary_prompt(raw_facts: str) -> str:
    """
    Промпт для периодической консолидации LTM.

    LLM получает ВСЕ факты пользователя и:
    - Убирает противоречия (старый город + новый город)
    - Объединяет дубликаты
    - Формирует чистый консистентный набор фактов
    """
    update_cats = ", ".join(UPDATE_CATEGORIES_FOR_SUMMARY)
    append_cats = ", ".join(APPEND_CATEGORIES_FOR_SUMMARY)

    return f"""You are consolidating long-term memory facts about a user.

RAW FACTS (may contain duplicates, contradictions, outdated info):
{raw_facts}

YOUR TASK:
1. Remove contradictions — keep only the LATEST/MOST RECENT value for: {update_cats}
2. Merge duplicates for accumulating categories: {append_cats}
3. Remove clearly wrong or garbage entries
4. Keep ALL valid facts — do NOT lose information

RULES:
- Output one fact per line in format: Category: value
- For update categories (e.g. City, Age) keep ONLY the most recent value
- For append categories (e.g. Hobby_music, Food) merge into single entries with comma-separated values
- If two facts contradict each other, keep the NEWER one
- Do NOT invent new facts — only restructure what's given
- Do NOT add "none", "unknown", "not mentioned"
- If a fact looks outdated (e.g. old city after user moved), remove the old one

EXAMPLES of consolidation:
  City: Moscow, City: Berlin → City: Berlin  (kept newer)
  Food: pizza, Food: sushi → Food: pizza, sushi  (merged)
  Hobby_music: guitar, Hobby_music: piano, drums → Hobby_music: guitar, piano, drums
  Age: 24, Age: 25 → Age: 25  (kept newer)

Consolidated facts:"""


def should_ignore_message(message: str) -> bool:
    """
    Игнорируем слишком короткие сообщения и чистый шум.
    """
    stripped = message.strip()
    if len(stripped) < 8:
        return True
    words = set(stripped.lower().split())
    pure_noise = {"привет", "hello", "hi", "пока", "bye"}
    if words.issubset(pure_noise):
        return True
    return False



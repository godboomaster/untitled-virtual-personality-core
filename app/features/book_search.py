"""
Поиск по книге Lord of the Mysteries для Арродеса.
Использует ChromaDB коллекцию с эмбеддингами.

Улучшения v2:
  - Трансляция русских имён → английские (с учётом падежей)
  - Мультизапрос: оригинальный + с переведёнными именами
  - Фильтр по дистанции
  - Дедупликация
  - n_results=12 по умолчанию
"""

import re
import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

logger = logging.getLogger(__name__)


def _tokenize(text: str) -> List[str]:
    """
    Простая токенизация для BM25: слова в нижнем регистре.
    Цифры и дефисные номера сохраняем как единый токен («2-049», «0-08») —
    иначе запечатанные артефакты по номеру не находятся вообще.
    """
    return re.findall(r'[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*', text.lower())


# Ключи авто-сгенерированных суффиксных алиасов (см. _expand_suffix_aliases).
# IntentRouter использует их, чтобы общеупотребимые однословные алиасы
# («Башня», «Бабочка») не переключали обычный разговор в book-режим.
_SUFFIX_ALIAS_KEYS: set = set()


def get_suffix_alias_keys() -> set:
    """Множество ключей-алиасов, добавленных автоматически (не из глоссария)."""
    return _SUFFIX_ALIAS_KEYS


def _load_ru_to_en(glossary_path: str) -> Dict[str, str]:
    """
    Парсит glossary-файл → словарь {ru_name: en_name}.
    Формат строк: English Name = Русское Имя
    """
    mapping: Dict[str, str] = {}
    try:
        with open(glossary_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                en_part, _, ru_part = line.partition("=")
                en_name = en_part.strip()
                # Чистим "(Pathway)" — это нотация глоссария, не часть имени.
                # Иначе "Тьма" заменится на "Darkness (Pathway)" буквально со скобками.
                en_name = re.sub(r'\s*\(Pathway\)\s*$', '', en_name)
                ru_name = ru_part.strip()
                if not en_name or not ru_name:
                    # Если нет русской части — возможно, это сокращение: "seq = Sequence"
                    # Тогда обе части английские, используем левую как сокращение
                    if en_name and not ru_name:
                        # Проверим: если en_name содержит пробел, это не сокращение
                        if " " not in en_name:
                            continue
                    continue
                # Убираем пояснения в скобках: "Сьюзи (собака)" → "Сьюзи"
                ru_clean = ru_name.split("(")[0].strip()
                # Несколько вариантов через "/"
                for variant in re.split(r'\s*/\s*', ru_clean):
                    v = variant.strip()
                    if v:
                        mapping[v] = en_name
    except FileNotFoundError:
        logger.warning(f"[BookSearch] Glossary not found: {glossary_path}")
    except Exception as e:
        logger.warning(f"[BookSearch] Glossary load error: {e}")

    # Хардкод сокращений
    ABBREVIATIONS = {
        "seq": "Sequence",
        "seq.": "Sequence",
    }
    for abbr, full in ABBREVIATIONS.items():
        mapping[abbr] = full

    # Авто-алиасы: суффиксы длинных имён («Морской Король Ян Коттман» →
    # «Ян Коттман», «Коттман») и первые имена («Розель Гюстав» → «Розель»),
    # чтобы пользователь мог писать имя без титула/фамилии.
    aliases = _expand_suffix_aliases(mapping)
    for k, v in _expand_first_name_aliases(mapping).items():
        aliases.setdefault(k, v)
    mapping.update(aliases)
    _SUFFIX_ALIAS_KEYS.update(aliases)
    if aliases:
        logger.info(
            f"[BookSearch] Auto aliases added: {len(aliases)} "
            f"(suffix + first-name)"
        )

    return mapping


def _normalize_glossary_key(key: str) -> str:
    """«Последовательность N - Имя» → «Имя»."""
    m = re.match(r'^Последовательность\s+\S+\s*-\s*(.+)$', key)
    return m.group(1).strip() if m else key


def _base_en_name(en: str) -> str:
    """«Sequence N - Name» → «Name»."""
    return re.sub(r'^Sequence\s+\d+\s*-\s*', '', en).strip()


def _ru_name_stem(word: str) -> str:
    """
    Стем для матчинга падежей имени: полное слово, с исключениями —
      - окончание на мягкий знак: без него (Розель → Розел: «Розеля», «Розелю»);
      - прилагательные окончания (ий/ый/ой/ие/ые/ая/яя) от 6 букв: минус 2
        (Потусторонний → Потусторонн: «потусторонних», «потустороннего»);
      - имена на гласную: минус последняя гласная от 5 букв
        (Каттлея → Каттле: «Каттлею», «Каттлеи» — винительный/родительный
        падежи меняют саму гласную). Порог 5 — чтобы «Роза» не ловила «Розель».
    Укороченные стемы дальше этого не используем: «Бит» из «Битч» матчил
    «битву», «Мона» из «Монарх» — «Амона», «Роз» из «Роза» — «Розель».
    """
    if word.endswith("ь"):
        return word[:-1]
    if len(word) >= 6 and word[-2:].lower() in (
            "ий", "ый", "ой", "ие", "ые", "ая", "яя"):
        return word[:-2]
    if len(word) >= 5 and word[-1].lower() in "аеиоыя":
        return word[:-1]
    return word


def _expand_suffix_aliases(ru_to_en: Dict[str, str]) -> Dict[str, str]:
    """
    Строит алиасы из суффиксов многословных имён глоссария.
    Для «Морской Король Ян Коттман» порождает «Ян Коттман» и «Коттман».

    Защита от ложных срабатываний:
      - суффикс не должен уже быть отдельной записью глоссария;
      - минимум 2 слова ИЛИ 1 слово от 5 букв («Ян» не станет алиасом);
      - коллизия: один суффикс у нескольких имён («Гюстав» у Розель,
        Бернадетты, Сиель) — отбрасывается. EN-формы, вложенные друг в друга
        («Jahn Kottman» ⊂ «Sea King Jahn Kottman»), коллизией не считаются —
        берётся самая короткая (она же самая частотная в тексте);
      - захват имени: однословный суффикс, чей стем совпадает со стемом
        первого слова другого имени («Розелля» из «выставки Розелля» имеет
        стем «Розел», как и «Розель» из «Розель Гюстав») — отбрасывается,
        иначе алиас перехватывает обычное имя персонажа.
    """
    def stem_of(word: str) -> str:
        # Стем по тем же правилам, что в _build_patterns
        return word[:max(4, len(word) - 2)].lower()

    # suffix -> множество нормализованных полных ключей
    suffix_map: Dict[str, set] = {}
    # стемы первых слов имён — ими пользователи называют персонажей
    first_stems: set = set()
    for key in ru_to_en:
        nk = _normalize_glossary_key(key)
        words = nk.split()
        if len(words) < 2:
            continue
        first_stems.add(stem_of(words[0]))
        for start in range(1, len(words)):
            suffix = " ".join(words[start:])
            suffix_map.setdefault(suffix, set()).add(nk)

    def ok_suffix(s: str) -> bool:
        if not s or s.startswith("-") or s[0].isdigit():
            return False
        # Однословные суффиксы с прилагательными окончаниями — обычно это
        # родительный падеж прилагательного, а не имя: «Гаданий» из «Клуб
        # Гаданий» давал стем «Гадан», который матчил обычное «гадание» и
        # срывал контроль целостности перевода. Цена — редкие имена на -ий
        # (Валерий, Григорий), их полные формы остаются в словаре.
        if len(s.split()) == 1 and s[-2:].lower() in ("ий", "ый", "ой"):
            return False
        return len(s.split()) >= 2 or len(s) >= 5

    def hijacks_first_name(s: str) -> bool:
        """Однословный суффикс перехватывает чужое первое имя (по стемам)."""
        if len(s.split()) != 1:
            return False
        ss = stem_of(s)
        return any(ss.startswith(fs) or fs.startswith(ss) for fs in first_stems)

    # en-форма нормализованного ключа (base_en существующей записи)
    norm_en: Dict[str, str] = {}
    for k, v in ru_to_en.items():
        norm_en.setdefault(_normalize_glossary_key(k), _base_en_name(v))

    aliases: Dict[str, str] = {}
    for suffix, keys in suffix_map.items():
        if not ok_suffix(suffix) or suffix in ru_to_en:
            continue
        if hijacks_first_name(suffix):
            continue
        ens = {norm_en.get(k, k) for k in keys}
        # Совместимость: все en-формы образуют цепочку по вложенности
        low = sorted({e.lower() for e in ens}, key=len)
        if all(low[i] in low[i + 1] for i in range(len(low) - 1)):
            aliases[suffix] = min(ens, key=len)
    return aliases


# Транслитерация RU → LAT для подбора EN-формы имени (без внешних зависимостей)
_RU_LAT_TABLE = str.maketrans({
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
    'ы': 'y', 'ь': '', 'ъ': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
})


def _translit_ru(word: str) -> str:
    return word.lower().translate(_RU_LAT_TABLE)


def _expand_first_name_aliases(ru_to_en: Dict[str, str]) -> Dict[str, str]:
    """
    Алиасы по первым словам многословных имён («Розель Гюстав» → «Розель»).
    Пользователь часто называет персонажа только именем — без алиаса
    regex-перевод его пропускает, а Ollama транслитерирует по-своему («Rozel»).

    EN-слово выбирается НЕ позиционно (порядок слов в RU/EN различается:
    «Алукард Хилберт» = «Hilbert Alucard»), а по схожести транслитерации
    (SequenceMatcher >= 0.65 с отрывом от второго кандидата).

    Защита: первое слово уникально среди первых слов имён глоссария
    (отсекает титулы «Лорд»/«Император» и общие имена), длина >= 4,
    не должно быть существующей записью.
    """
    from difflib import SequenceMatcher

    STOP = {"the", "of", "de", "van", "von", "der", "and", "a", "an"}

    # Частота первых слов многословных имён
    fw_counts: Dict[str, int] = {}
    for key in ru_to_en:
        words = _normalize_glossary_key(key).split()
        if len(words) >= 2:
            fw_counts[words[0]] = fw_counts.get(words[0], 0) + 1

    def best_en_word(ru_word: str, en_form: str) -> Optional[str]:
        t = _translit_ru(ru_word)
        cands = []
        for ew in en_form.split():
            ew_c = ew.strip(".,'\"()")
            if len(ew_c) < 3 or not ew_c[0].isupper() or ew_c.lower() in STOP:
                continue
            r = SequenceMatcher(None, t, ew_c.lower()).ratio()
            cands.append((r, ew_c))
        if not cands:
            return None
        cands.sort(reverse=True)
        best_r, best_w = cands[0]
        second_r = cands[1][0] if len(cands) > 1 else 0.0
        if best_r >= 0.65 and (best_r - second_r >= 0.15 or best_r >= 0.8):
            return best_w
        return None

    aliases: Dict[str, str] = {}
    for key in ru_to_en:
        words = _normalize_glossary_key(key).split()
        if len(words) < 2:
            continue
        w = words[0]
        if len(w) < 4 or w in ru_to_en or fw_counts.get(w, 0) != 1 or w in aliases:
            continue
        en = best_en_word(w, _base_en_name(ru_to_en[key]))
        if en:
            aliases[w] = en
    return aliases


def _build_patterns(ru_to_en: Dict[str, str]) -> List[Tuple[re.Pattern, str]]:
    """
    Строит список (compiled_regex, en_replacement) для трансляции запросов.
    Обрабатывает русские падежи через стем-матчинг: берём первые N символов
    как стем и разрешаем любое русское окончание.
    Паттерны пре-компилируются: их ~1.4k, а кеш re вмещает лишь 512 —
    без компиляции заранее каждый _translate_query тратил бы ~250 мс.
    Однословные алиасы из _CASE_SENSITIVE_ALIASES компилируются БЕЗ
    re.IGNORECASE: подстановка срабатывает только на форму с заглавной
    буквы (имя собственное), общеупотребительное слово не трогаем.
    """
    RU_SUFFIX = r'[а-яёА-ЯЁ]*'
    patterns: List[Tuple[str, str]] = []

    for ru, en in sorted(ru_to_en.items(), key=lambda x: -len(x[0])):
        if ru.isascii():
            # Латинские сокращения («seq»): только точное совпадение.
            # Без концевого \b «seq» матчится внутри слова «Sequence»
            # и каскад замен раздувает его в «Sequenceuence».
            pattern = re.escape(ru) + r'\b'
            patterns.append((re.compile(r'\b' + pattern, re.IGNORECASE), en))
            continue
        words = ru.split()
        if len(words) >= 2:
            # Многословное имя: каждое слово → стем (см. _ru_name_stem) +
            # суффикс. Соседние слова — якорь, поэтому для слов на гласную
            # стем срезается и при длине < 5 («Роз» из «Роза Искупления»),
            # чего нельзя делать в однословных паттернах («Роз» ⊂ «Розель»).
            def stem_multi(w: str) -> str:
                s = _ru_name_stem(w)
                if s == w and len(w) >= 3 and w[-1].lower() in "аеиоыя":
                    return w[:-1]
                return s

            word_pats = []
            for w in words:
                stem = re.escape(stem_multi(w))
                word_pats.append(stem + RU_SUFFIX)
            pattern = r'\s+'.join(word_pats)
        elif len(ru) >= 3:
            # Однословное имя: стем + суффикс для падежей (см. _ru_name_stem)
            pattern = re.escape(_ru_name_stem(ru)) + RU_SUFFIX
        else:
            # Очень короткое: точное совпадение
            pattern = re.escape(ru) + r'\b'

        # \b в начале обязателен: стем без границы слова матчится ВНУТРИ
        # других слов («Мона» из «Монарх» ловилась внутри «Амона»).
        flags = 0 if (len(words) == 1 and ru.lower() in _CASE_SENSITIVE_ALIASES) \
            else re.IGNORECASE
        patterns.append((re.compile(r'\b' + pattern, flags), en))

    return patterns


# Однословные алиасы, совпадающие с общеупотребимыми существительными:
# «Мир» = The World, но «мир» = world; «Солнце» = The Sun / путь Солнца,
# но «солнце» = sun. IGNORECASE-подстановка превращала «перемещение в
# другой мир» в «shift to another The World» и отравляла весь поиск —
# поэтому эти слова заменяем только в форме с заглавной буквы (имя
# собственное по-русски пишется с заглавной). Сюда попадают и прямые
# записи глоссария, и авто-алиасы («Господин Звезда» → «Звезда»).
_CASE_SENSITIVE_ALIASES = {
    "мир", "солнце", "луна", "звезда", "судья",
    "маг", "справедливость", "повешенный", "отшельник", "шут",
}


def _translate_query(query: str,
                     patterns: List[Tuple[re.Pattern, str]]) -> str:
    """
    Заменяет русские имена/термины в запросе на английские.
    Паттерны отсортированы по длине (длинные первыми), пре-компилированы.
    """
    result = query
    for pattern, en in patterns:
        result = pattern.sub(en, result)
    return result


def _translate_query_tracked(query: str,
                             patterns: List[Tuple[re.Pattern, str]]
                             ) -> Tuple[str, List[str]]:
    """
    Как _translate_query, но дополнительно возвращает список en-форм,
    чья замена РЕАЛЬНО применилась. Нужно для _entities_survived:
    паттерны пересекаются (авто-алиас «Гаданий» матчит то же «гадание»,
    что и запись «Гадание»), и проверять надо только применённые замены —
    иначе неприменившийся паттерн даёт ложное «перевод потерял сущность».
    """
    result = query
    used: List[str] = []
    for pattern, en in patterns:
        new = pattern.sub(en, result)
        if new != result:
            used.append(en)
            result = new
    return result, used


# Базовые переводы русских запросов → английские для cross-encoder reranker.
# Cross-encoder (ms-marco) англоязычный, поэтому нужен полный перевод смысла.
_RU_EN_QUERY_MAP: List[Tuple[str, str]] = [
    # (русский паттерн, английская замена) — длинные первыми
    ("расскажи про", "tell me about"),
    ("расскажи о", "tell me about"),
    ("расскажи", "tell me about"),
    ("что произошло с", "what happened to"),
    ("что произошло в", "what happened in"),
    ("что случилось с", "what happened to"),
    ("что случилось", "what happened"),
    ("что произошло", "what happened"),
    ("что ты знаешь о", "what do you know about"),
    ("что ты знаешь про", "what do you know about"),
    ("кто такой", "who is"),
    ("кто такая", "who is"),
    ("кто такие", "who are"),
    ("кто это", "who is"),
    ("кто был", "who was"),
    ("кто", "who"),
    ("где находится", "where is"),
    ("где", "where"),
    ("когда", "when"),
    ("зачем", "why"),
    ("почему", "why"),
    ("как", "how"),
    ("сколько", "how many"),
    ("какие", "what are"),
    ("какая", "what is"),
    ("какой", "what is"),
    ("какое", "what is"),
    ("опиши", "describe"),
    ("найди", "find"),
    ("покажи", "show"),
    ("почётное имя", "honorific name"),
    ("почетное имя", "honorific name"),
    ("почётные имена", "honorific names"),
    ("почетные имена", "honorific names"),
    # Глаголы-связки
    ("был", "was"),
    ("была", "was"),
    ("было", "was"),
    ("были", "were"),
    ("есть", "is"),
    ("стал", "became"),
    ("стала", "became"),
    # Предлоги / служебные
    ("в", "in"),
    ("во", "in"),
    ("у", ""),
    ("с", "with"),
    ("на", "on"),
    ("из", "from"),
    ("от", "from"),
    ("про", "about"),
    ("о", "about"),
    ("для", "for"),
    ("по", "by"),
    ("к", "to"),
    ("не", "not"),
    # Существительные (в т.ч. LotM-специфичные)
    ("номер", "number"),
    ("запечатанный артефакт", "sealed artifact"),
    ("артефакт", "artifact"),
    ("сущность", "entity"),
    ("последовательность", "sequence"),
    ("путь", "pathway"),
    ("сражение", "battle"),
    ("битва", "battle"),
    ("бой", "fight"),
    ("дворец", "palace"),
    ("короля", "king"),
    ("король", "king"),
    ("произошло", "happened"),
    ("случилось", "happened"),
    ("сведения", "information"),
    ("подробности", "details"),
    ("история", "history"),
    ("прошлое", "past"),
    ("сила", "power"),
    ("способность", "ability"),
    ("способности", "abilities"),
    # Том/книга (числительные)
    ("первый", "first"),
    ("первая", "first"),
    ("второй", "second"),
    ("вторая", "second"),
    ("третий", "third"),
    ("третья", "third"),
    ("четвёртый", "fourth"),
    ("четвертый", "fourth"),
    ("пятый", "fifth"),
    ("шестой", "sixth"),
    ("седьмой", "seventh"),
    ("восьмой", "eighth"),
    ("том", "volume"),
    ("томе", "volume"),
    ("книга", "book"),
    ("книге", "book"),
    ("главе", "chapter"),
    ("глава", "chapter"),
]


_ollama_available: Optional[bool] = None

# Модель Ollama для всех LLM-шагов переформулирования запроса
# (подстановка имён, дистилляция, резолюция местоимений).
# gemma3:4b стабильнее на классификации/переформулировании, чем qwen2.5:3b
# (последняя флапает даже при temperature=0, давая ложные решения).
OLLAMA_MODEL = "gemma3:4b"

# Фича-флаг для query distillation. Позволяет отключать шаг для A/B-замеров
# (recall до/после) и для фолбэка, если дистилляция нестабильна.
DISTILL_ENABLED: bool = True

# Сколько символов чанка видит cross-encoder при rerank. 400 было мало:
# у чанков с префиксом [SUMMARY:] описание предмета часто лежит глубже
# (напр. внешность 2-049 после ~950-го символа) и rerank его «не видел».
# ~1400 символов ≈ 380 токенов — влезает в лимит ms-marco (512).
RERANK_EXCERPT_CHARS: int = 1400

# Доля окна rerank, отводимая [SUMMARY:]-префиксу. Без ограничения длинный
# префикс (~1000 символов) съедал почти всё окно, и сцена с ответом в теле
# чанка (напр. карта «Шут» на ~1475-й позиции) оказывалась за пределами
# видимости cross-encoder'а. Префикс важен: в нём курируемые связки
# («Antigonus family notebook»), которых нет в теле чанка.
_RERANK_PREFIX_SHARE = 0.4


def _rerank_excerpt(text: str) -> str:
    """
    Готовит окно чанка для cross-encoder rerank:
    [SUMMARY:]-префикс (урезанный до доли окна) + начало тела чанка.
    """
    m = re.match(r'^\[SUMMARY:.*?\]\n\n', text, flags=re.DOTALL)
    if not m:
        return text[:RERANK_EXCERPT_CHARS]
    prefix = m.group(0)[:int(RERANK_EXCERPT_CHARS * _RERANK_PREFIX_SHARE)]
    body = text[m.end():m.end() + (RERANK_EXCERPT_CHARS - len(prefix))]
    return prefix + body


def _find_glossary_entries(query: str, ru_to_en: Dict[str, str],
                           limit: int = 30,
                           full_only: bool = False) -> Dict[str, str]:
    """
    Находит релевантные записи глоссария в запросе.
    Использует границы слов (\\b) — НЕ матчит подстроки внутри слов.
    Возвращает словарь {ru_name: en_name} только для совпавших записей.

    Для многословных имён ("Форс Уолл"): если совпали все слова — полное имя.
    Если совпало только первое слово (имя) и оно уникально в глоссарии — тоже матч.
    Титулы ("Господин", "Мисс") не уникальны — частичный матч для них отключён.

    full_only=True — только полные совпадения (для контроля целостности
    перевода: частичные матчи по имени нельзя проверить по полной en-форме —
    «Клейна» переведут как «Klein», а не «Klein Moretti»).
    """
    # Считаем частоту первых слов — не уникальные = титулы, не имена
    first_word_counts: Dict[str, int] = {}
    for ru in ru_to_en:
        words = ru.split()
        if len(words) >= 2:
            fw = words[0].lower()
            first_word_counts[fw] = first_word_counts.get(fw, 0) + 1

    result: Dict[str, str] = {}
    query_lower = query.lower()
    for ru, en in ru_to_en.items():
        words = ru.split()
        # Полное совпадение — все слова
        all_matched = True
        for w in words:
            stem_len = max(3, len(w) - 1)
            stem = re.escape(w[:stem_len].lower())
            if not re.search(r'\b' + stem + r'[а-яё]*', query_lower):
                all_matched = False
                break
        if all_matched:
            result[ru] = en
            if len(result) >= limit:
                break
            continue
        # Частичное — первое слово, только если уникально и достаточно длинное.
        # Стем строгий (_ru_name_stem): матч незаякоренный, мягкий стем даёт
        # ложные срабатывания («Роз» из «Роза Искупления» ловился в «Розель»).
        if full_only:
            continue
        if len(words) >= 2 and len(words[0]) >= 4:
            if first_word_counts.get(words[0].lower(), 0) > 1:
                continue  # титул — пропускаем
            w = words[0]
            stem = re.escape(_ru_name_stem(w).lower())
            if re.search(r'\b' + stem + r'[а-яё]*', query_lower):
                result[ru] = en
                if len(result) >= limit:
                    break
    return result


def _translate_via_ollama(text: str) -> Optional[str]:
    """
    Полный перевод запроса на английский через локальную Ollama.
    Имена собственные в тексте уже подставлены regex-глоссарием — модели
    остаётся только перевести связной текст, с чем она справляется заметно
    лучше, чем с хирургией отдельных слов. None при ошибке/недоступности.
    """
    global _ollama_available
    if _ollama_available is False:
        return None
    prompt = (
        "Translate the following Russian search query into English. "
        "Keep any English names and terms already in the text unchanged. "
        "Output ONLY the translation. No quotes, no explanation.\n\n"
        f"Query: {text}\n"
        "Translation:"
    )
    try:
        import requests

        resp = requests.post("http://localhost:11434/api/generate", json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 128},
        }, timeout=8)
        text_out = resp.json()["response"].strip().strip('"').strip()
        _ollama_available = True
        return text_out if text_out else None
    except Exception as e:
        _ollama_available = False
        logger.warning(f"[BookSearch] ollama translate failed: {e}")
        return None


def _distill_query_via_ollama(fully_translated: str) -> Optional[str]:
    """
    Сжимает переведённый (английский) запрос в компактный поисковый запрос,
    убирая разговорный шум, но сохраняя все сущности и связи между ними.
    Использует ту же Ollama-инстанс/флаг доступности, что и подстановка имён.
    Возвращает None при ошибке или недоступности (для фолбэка).
    """
    global _ollama_available
    if _ollama_available is False:
        return None
    prompt = (
        "You are a search query optimizer for a vector database "
        'containing narrative text from the novel "Lord of the Mysteries".\n\n'
        "Rewrite the user's question into a concise search query for semantic "
        "similarity search against book passages. Remove conversational filler "
        "(tell me about, what is, can you explain, I want to know, please) but "
        "KEEP all named entities and the relationship/action connecting them "
        "if the question involves more than one entity or an event.\n\n"
        "Output ONLY the rewritten query. No quotes, no explanation, "
        "no punctuation at the end.\n\n"
        'Examples:\n'
        'Q: "tell me about tingen"\n'
        "A: Tingen\n\n"
        'Q: "tell me about the relationship between Klein and Tingen"\n'
        "A: Klein relationship to Tingen\n\n"
        'Q: "what happened when Klein first became a Beyonder"\n'
        "A: Klein becomes Beyonder first time\n\n"
        'Q: "can you explain how the Fool Pathway sequences work"\n'
        "A: Fool Pathway sequences\n\n"
        f"Question: {fully_translated}\n"
        "Query:"
    )
    try:
        import requests

        resp = requests.post("http://localhost:11434/api/generate", json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 64},
        }, timeout=5)
        text = resp.json()["response"].strip()
        _ollama_available = True
        return text if text else None
    except Exception as e:
        _ollama_available = False
        logger.warning(f"[BookSearch] distill_query failed: {e}")
        return None


def _split_aspects_via_ollama(translated: str) -> List[str]:
    """
    Многоаспектный запрос → независимые подзапросы через Ollama.

    Один векторный запрос покрывает лишь доминирующий аспект вопроса
    («как добил Стива И ЧТО досталось с трофеев» — чанки про трофеи тонут).
    Возвращает до 3 коротких подзапросов; для одноаспектного вопроса —
    пустой список. Фолбэк как у distill: недоступность Ollama = без сплита.
    """
    global _ollama_available
    if _ollama_available is False:
        return []
    prompt = (
        "You are a search query analyzer for a book Q&A system.\n"
        "If the user's question asks about TWO OR MORE distinct things "
        "(e.g. 'how did X happen and what did Y get for it'), split it into "
        "independent short search queries, ONE PER ASPECT, one per line. "
        "Keep all named entities in each line.\n"
        "If the question is about a SINGLE thing, output exactly: SINGLE\n"
        "Output ONLY the queries (one per line) or SINGLE. "
        "No numbering, no quotes, no explanation.\n\n"
        "Examples:\n"
        'Q: "How did Klein defeat Steve in that fight and what did he get from the spoils?"\n'
        "A:\nKlein defeats Steve fight\nKlein spoils loot after Steve fight\n\n"
        'Q: "Who is Audrey Hall?"\n'
        "A:\nSINGLE\n\n"
        'Q: "What is the Tarot Club and who founded it?"\n'
        "A:\nTarot Club\nTarot Club founder\n\n"
        f'Q: "{translated}"\nA:'
    )
    try:
        import requests

        resp = requests.post("http://localhost:11434/api/generate", json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 96},
        }, timeout=5)
        text = resp.json().get("response", "").strip()
        _ollama_available = True
    except Exception as e:
        _ollama_available = False
        logger.warning(f"[BookSearch] aspect_split failed: {e}")
        return []

    if not text or "single" in text.lower():
        return []
    aspects = []
    for line in text.splitlines():
        a = line.strip().lstrip("-•*0123456789.): ").strip().strip('"\'.,! ')
        if len(a) >= 3 and not re.search(r"[а-яёА-ЯЁ]", a):
            aspects.append(a)
    # защита от копирования исходника и мусора
    aspects = [a for a in aspects if a.lower() != translated.lower()][:3]
    return aspects


def _clean_distilled(distilled: str, original: str) -> Optional[str]:
    """
    Пост-обработка distilled-запроса.
    Отсекает три проблемных случая: пустой ответ, ленивое копирование
    исходника (>= 80% длины) и точный дубль (без учёта регистра).
    """
    if not distilled:
        return None
    # снять случайные кавычки/точки, если модель их всё же добавила
    cleaned = distilled.strip('"\'.,! ').strip()
    if not cleaned:
        return None
    # не добавлять, если модель просто скопировала вопрос почти целиком
    if len(cleaned) >= len(original) * 0.8:
        return None
    # не добавлять дубликат (без учёта регистра)
    if cleaned.lower() == original.lower():
        return None
    return cleaned


# ---------------------------------------------------------------------------
# Coreference resolution (Шаг 0): разрешаем местоимения 3-го лица в запросе
# по истории диалога, ДО перевода/глоссария — на сыром русском тексте.
# Иначе местоимение «он/его» пойдёт в retrieval и сломает поиск.
# ---------------------------------------------------------------------------

# Русские местоимения 3-го лица (ед. + мн. число, все падежи)
# и указательные «это»/«этот» в анафорическом употреблении
# («в какой главе это произошло?», «как называется этот город?»).
# «Этот» здесь тоже: resolver (GLM) сам решает — анафора это
# («этот город» = последняя тема) или просто определитель.
_PRONOUN_PATTERN = re.compile(
    r'\b(он|она|оно|его|её|ему|ей|им|ею|него|неё|нему|ней|'
    r'они|их|ими|них|ним|ними|'
    r'это|этого|этому|об этом|про это|'
    r'этот|эта|эти|этим|этой|этих|этими)\b',
    re.IGNORECASE,
)


def has_pronoun_reference(query: str) -> bool:
    """True, если в запросе есть местоимение 3-го лица, требующее резолюции."""
    return bool(_PRONOUN_PATTERN.search(query))


def _is_person_name(ru_key: str) -> bool:
    """
    Приближённая фильтрация «имя персонажа» vs «служебный термин».
    Критерии ИСТИНА:
      - русское имя (кириллица, а не английская аббревиатура вроде seq)
      - достаточно длинное (>= 4 символов) — отбрасывает служебные
        «Шут»/«Путь»/«Маг», оставляя имена персонажей.
    """
    if not ru_key:
        return False
    # Английские ключи (seq/seq.) — не имена, это служебные аббревиатуры
    if ru_key.isascii():
        return False
    # Только кириллица + пробелы
    if not re.fullmatch(r'[а-яёА-ЯЁ ]+', ru_key):
        return False
    return len(ru_key) >= 4


def _get_last_mentioned_entity(history: List[str],
                               ru_to_en: Dict[str, str],
                               n_messages: int = 6) -> Optional[str]:
    """
    Эвристический фолбэк: последнее упомянутое в истории имя персонажа.
    Идёт от свежих сообщений к старым, пропуская служебные термины
    (Pathway/Sequence/титулы) — берёт только имена (см. _is_person_name).
    Приоритет: многословные имена (полные) выше коротких.
    """
    # Идём от свежих к старым, собираем всех кандидатов
    for msg in reversed(history[-n_messages:]):
        if not msg:
            continue
        entries = _find_glossary_entries(msg, ru_to_en)
        # Сортируем совпадения в этом сообщении: длинные (полные имена) первыми
        person_keys = [k for k in entries.keys() if _is_person_name(k)]
        person_keys.sort(key=len, reverse=True)
        if person_keys:
            return person_keys[0]
    return None


# Промпт резолюции местоимений: подстановка СУЩНОСТИ из истории
# (персонаж, артефакт, место, организация), а не только персонажа.
_COREF_PROMPT = """You resolve pronoun references in a Russian conversation about the novel "Lord of the Mysteries". Replace 3rd-person pronouns (он/она/его/её/ему/ей/им/них) and demonstratives «это» / «этот + generic noun» (when they point to a previously discussed subject or event) with the ENTITY they refer to — the most recently discussed subject from the history: a character, an artifact, a place, an organization, or an event. Substitute its exact name or number as it appeared in the history («Леонард», «артефакт 2-049», «Тинген», «наказание Каттлеи Шутом», «марионеточный город»). Keep everything else in Russian, unchanged. Prefer the subject of the LAST exchange over earlier topics. Only answer UNCLEAR when no entity from the history fits.

Conversation history:
{context}

Current message: {query}

Examples:
History: Кто такой Клейн?  /  Клейн — Sequence 9 Seer.
Current: "а что с его способностями?"
Rewritten: а что со способностями Клейна?

History: Расскажи про запечатанный артефакт 2-049.  /  2-049 — марионетка семьи Антигонус.
Current: "как он выглядит?"
Rewritten: как выглядит артефакт 2-049?

History: Какой город создал Клейн для продвижения?  /  Клейн создал марионеточный город.
Current: "как называется этот город?"
Rewritten: как называется марионеточный город?

History: Как Шут наказал Каттлею?  /  Шут приготовил три плана наказания.
Current: "в какой главе это произошло?"
Rewritten: в какой главе произошло наказание Каттлеи Шутом?

History: Расскажи про Амона.  /  Амон — ангел пути Error.
Current: "где он появился?"
Rewritten: где появился Амон?

History: Кто такая Богиня Ночи?
Current: "расскажи про Тинген"
Rewritten: UNCLEAR

Output ONLY the rewritten message or UNCLEAR, nothing else.

Rewritten:"""


def _resolve_pronoun_via_router(query: str, history: List[str],
                                router) -> Tuple[Optional[str], bool]:
    """
    Резолюция местоимений через основную LLM (router). Заметно надёжнее
    локальной gemma3:4b, которая на этой задаче флапает и копирует сущности
    из few-shot примеров («Как он выглядит?» → «как выглядит Тинген?»).
    Returns: (rewritten, answered) — семантика как у _resolve_pronoun_via_ollama.
    """
    context = "\n".join(history[-6:])
    if not context.strip():
        return None, False
    prompt = _COREF_PROMPT.format(context=context, query=query)
    try:
        result = router.get_response(
            [{"role": "user", "content": prompt}],
            temperature=0.0, max_tokens=128,
        ).strip()
        if not result or result.upper().startswith("UNCLEAR"):
            return None, True
        return result, True
    except Exception as e:
        logger.warning(f"[BookSearch] pronoun resolution via router failed: {e}")
        return None, False


def _resolve_pronoun_via_ollama(query: str,
                                history: List[str]) -> Tuple[Optional[str], bool]:
    """
    LLM-резолюция местоимений через локальную Ollama (фолбэк к router-варианту).
    Returns: (rewritten, answered) — см. _resolve_pronoun_via_router.
    """
    global _ollama_available
    if _ollama_available is False:
        return None, False
    # Берём последние сообщения диалога (пользователь + ответы бота).
    context = "\n".join(history[-6:])
    if not context.strip():
        return None, False
    prompt = _COREF_PROMPT.format(context=context, query=query)
    try:
        import requests

        resp = requests.post("http://localhost:11434/api/generate", json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 128},
        }, timeout=6)
        result = resp.json()["response"].strip()
        _ollama_available = True
        if not result or result.upper().startswith("UNCLEAR"):
            # LLM ответил, но считает антецедент неоднозначным — НЕ фолбэчим.
            return None, True
        return result, True
    except Exception as e:
        _ollama_available = False
        logger.warning(f"[BookSearch] pronoun resolution failed: {e}")
        return None, False


def resolve_query_coref(query: str,
                        history: Optional[List[str]],
                        ru_to_en: Dict[str, str],
                        router=None) -> str:
    """
    Разрешает кореферентность местоимений в запросе, используя историю диалога.
    Пайплайн:
      1. Нет местоимения / нет истории → отдаём как есть.
      2. Router (основная LLM) пробует разрешить по контексту — надёжнее всего.
      3. Фолбэк: локальная Ollama (gemma3:4b), затем regex-эвристика
         на последнее упомянутое имя.
         - Если LLM ответил UNCLEAR — антецедент неоднозначен, НЕ фолбэчим
           (лучше оставить местоимение, чем подставить случайное имя).
    Логирует каждый шаг для отладки (неверная резолвенция молча ломает retrieval).
    """
    if not history or not has_pronoun_reference(query):
        return query

    if router is not None:
        resolved, answered = _resolve_pronoun_via_router(query, history, router)
        if resolved and resolved != query:
            logger.info(f"[BookSearch] [coref] router: '{query}' -> '{resolved}'")
            return resolved
        if answered:
            logger.info(f"[BookSearch] [coref] router UNCLEAR: '{query}'")
            return query

    resolved, llm_answered = _resolve_pronoun_via_ollama(query, history)
    if resolved and resolved != query:
        logger.info(f"[BookSearch] [coref] LLM: '{query}' -> '{resolved}'")
        return resolved
    if llm_answered:
        # LLM ответил, но не дал замены (UNCLEAR) — честнее оставить как есть.
        logger.info(f"[BookSearch] [coref] LLM UNCLEAR: '{query}'")
        return query

    # Фолбэк (только при недоступности LLM): regex-замена на последнее имя.
    last_entity = _get_last_mentioned_entity(history, ru_to_en)
    if last_entity:
        replaced = _PRONOUN_PATTERN.sub(last_entity, query)
        if replaced != query:
            logger.info(
                f"[BookSearch] [coref] heuristic: '{query}' -> '{replaced}' "
                f"(entity={last_entity!r})"
            )
            return replaced

    logger.info(f"[BookSearch] [coref] unresolved: '{query}'")
    return query


def _google_translate(text: str) -> Optional[str]:
    """Перевод через deep_translator (Google Translate API)."""
    try:
        from deep_translator import GoogleTranslator
        translator = GoogleTranslator(source="auto", target="en")
        result = translator.translate(text)
        if result and result.lower() != text.lower():
            return result
    except Exception as e:
        logger.debug(f"[BookSearch] Google Translate недоступен: {e}")
    return None


def _build_pathway_map(glossary_path: str) -> Tuple[Dict[str, List[str]], Dict[str, str], Dict[Tuple[str, int], str]]:
    """
    Парсит глоссарий → маппинги:
      pathway_name → [seq_name_1, seq_name_2, ...]
      seq_name → pathway_name
      (pathway_name, номер) → seq_name   — для «N последовательность» запросов
    Pathway names: 'Red Priest', 'Twilight Giant', 'Fool' и т.д.
    Sequence names: 'Conqueror', 'Iron-Blood Knight', 'Solar High Priest' и т.д.
    """
    pathway_to_seqs: Dict[str, List[str]] = {}
    seq_to_pathway: Dict[str, str] = {}
    seq_by_num: Dict[Tuple[str, int], str] = {}
    try:
        with open(glossary_path, encoding="utf-8") as f:
            current_pathway: Optional[str] = None
            for line in f:
                raw = line.strip()
                # Заголовок секции пути: "ПУТЬ КРАСНОГО ЖРЕЦА (Red Priest Pathway):"
                m = re.match(r'^ПУТЬ\s+.*?\((.+?)\s+Pathway\)\s*:', raw)
                if m:
                    current_pathway = m.group(1).strip()
                    pathway_to_seqs.setdefault(current_pathway, [])
                    continue
                # Если в не-секциях путей — пропускаем
                if current_pathway is None:
                    continue
                # Конец секции путей: любой другой заголовок («ОРГАНИЗАЦИИ:»,
                # «ЛОКАЦИИ:», «СПРАВОЧНИК...») или комментарий-разделитель.
                # Без этого ВСЕ записи до конца файла (Тинген, Клуб Таро,
                # само слово Sequence) записывались в последний путь —
                # и pathway_exp добавлял Black Emperor почти в каждый запрос.
                if raw.startswith("#") or (raw.endswith(":") and "=" not in raw):
                    current_pathway = None
                    continue
                if raw.startswith("ОБЩИЕ") or "ОБЩИЕ ТЕРМИНЫ" in raw:
                    current_pathway = None
                    continue
                if not raw:
                    continue
                if "=" not in raw:
                    continue
                en_part, _, _ = raw.partition("=")
                en_name = en_part.strip()
                # Убираем "Sequence N - " префикс
                m2 = re.match(r'^Sequence\s+(\d+)\s*-\s*(.+)$', en_name)
                if m2:
                    seq_num = int(m2.group(1))
                    seq_name = m2.group(2).strip()
                    if seq_name and seq_name != current_pathway:
                        pathway_to_seqs[current_pathway].append(seq_name)
                        seq_to_pathway[seq_name] = current_pathway
                        seq_by_num.setdefault((current_pathway, seq_num), seq_name)
                elif en_name and en_name != current_pathway:
                    pathway_to_seqs[current_pathway].append(en_name)
                    seq_to_pathway[en_name] = current_pathway
    except Exception as e:
        logger.warning(f"[BookSearch] Pathway map parse error: {e}")
    # Дедупликация внутри каждого пути с сохранением порядка
    for p, seqs in pathway_to_seqs.items():
        seen = set()
        deduped = []
        for s in seqs:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        pathway_to_seqs[p] = deduped
    return pathway_to_seqs, seq_to_pathway, seq_by_num


def _build_character_pathways(glossary_path: str) -> Dict[str, str]:
    """
    Парсит секцию «СПРАВОЧНИК ПЕРСОНАЖЕЙ» глоссария:
      «Клейн Моретти → Шут (Fool) | Путь Шута» → {'Клейн Моретти': 'Fool'}
    EN-имя пути берётся из скобок в самой строке, либо из заголовков
    секций «ПУТЬ X (EN Pathway):» по совпадению русского названия.
    """
    # RU-название пути (род. падеж из заголовков) → EN
    pathway_ru_to_en: Dict[str, str] = {}
    char_pathways: Dict[str, str] = {}
    try:
        with open(glossary_path, encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                m = re.match(r'^ПУТЬ\s+(.+?)\s*\((.+?)\s+Pathway\)\s*:', raw)
                if m:
                    pathway_ru_to_en["путь " + m.group(1).strip().lower()] = m.group(2).strip()
        with open(glossary_path, encoding="utf-8") as f:
            in_ref = False
            for line in f:
                raw = line.strip()
                if raw.startswith("СПРАВОЧНИК ПЕРСОНАЖЕЙ"):
                    in_ref = True
                    continue
                if not in_ref:
                    continue
                if raw.endswith(":") or raw.startswith("#"):
                    if char_pathways:
                        break  # секция закончилась
                    continue
                m = re.match(r'^(.+?)\s*→\s*(.+?)\s*\|\s*Путь\s+(.+?)\s*$', raw)
                if not m:
                    continue
                char_ru, _, path_part = m.group(1).strip(), m.group(2), m.group(3).strip()
                en_inline = re.search(r'\((.+?)\)', path_part)
                if en_inline:
                    pathway_en = en_inline.group(1).strip()
                else:
                    pathway_en = pathway_ru_to_en.get("путь " + path_part.lower())
                if pathway_en:
                    char_pathways[char_ru] = pathway_en
    except Exception as e:
        logger.warning(f"[BookSearch] Character pathways parse error: {e}")
    return char_pathways


def _expand_query_with_pathway(query: str,
                               pathway_to_seqs: Dict[str, List[str]],
                               seq_to_pathway: Dict[str, str]) -> Optional[str]:
    """
    Если в запросе есть pathway name → возвращает строку из названий его последовательностей.
    Если есть sequence name → добавляет pathway name.
    Эти термы добавляются в отдельный поисковый запрос — НЕ подмешиваются в reranker query.
    """
    q_lower = query.lower()
    has_pathway_word = "pathway" in q_lower
    extra_terms: List[str] = []

    # Прямой поиск: pathway name → sequences (только если "Pathway" есть в запросе)
    if has_pathway_word:
        for pathway, seqs in pathway_to_seqs.items():
            p_lower = pathway.lower()
            if " " in p_lower:
                # Многословный путь — точная подстрока
                if p_lower in q_lower:
                    extra_terms.extend(seqs)
                    logger.info(f"[BookSearch] Pathway expand: '{pathway}' -> +{len(seqs)} seqs")
            else:
                # Однословный путь (Sun, Fool, Moon) — проверяем "X Pathway" / "X's Pathway"
                pat = r'\b' + re.escape(p_lower) + r"(?:'s)?\s+pathway\b"
                if re.search(pat, q_lower):
                    extra_terms.extend(seqs)
                    logger.info(f"[BookSearch] Pathway expand: '{pathway}' -> +{len(seqs)} seqs")

    # Обратный поиск: sequence name → pathway (без условия "Pathway" в запросе).
    # Все имена — только по границам слов: подстрочный матч ловил «queen»
    # внутри «Sequence(uence)» и добавлял мусорные пути в fan-out.
    for seq, path in seq_to_pathway.items():
        s_lower = seq.lower()
        if re.search(r'\b' + re.escape(s_lower) + r'\b', q_lower):
            extra_terms.append(path)

    if not extra_terms:
        return None
    # Дедупликация с сохранением порядка
    seen = set()
    result = []
    for t in extra_terms:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return " ".join(result)


# «N последовательность» в вопросе: и числом перед словом, и после
_SEQNUM_RU = re.compile(
    r'(\d+)\s*(?:-\s*\w+)?\s*последовательност|последовательност\w*\s*(?:номер\s*)?(\d+)',
    re.IGNORECASE,
)
_SEQNUM_EN = re.compile(r'\bsequence\s+(\d+)\b|\b(\d+)\s+sequence\b', re.IGNORECASE)


def _detect_seq_number(raw_query: str, translated: str) -> Optional[int]:
    """Номер последовательности в вопросе (0-9) или None."""
    m = _SEQNUM_RU.search(raw_query)
    if m:
        n = int(m.group(1) or m.group(2))
    else:
        m = _SEQNUM_EN.search(translated or "")
        n = int(m.group(1) or m.group(2)) if m else None
    return n if n is not None and 0 <= n <= 9 else None


# Ритуальные ключевые слова продвижения по путям (по тексту книги).
# Книга о ритуале НЕ пишет номер последовательности — только её название
# и содержание ритуала, поэтому «N последовательность» без этих терминов
# не находит нужные главы. Только проверенные формулировки из текста.
_PATHWAY_RITUAL_TERMS: Dict[Tuple[str, int], str] = {
    ("Fool", 4): "grand performance",   # Bizarro Sorcerer: представление перед зрителями
    ("Fool", 1): "marionette town",     # Attendant of Mysteries: марионеточный город
}


def _expand_seq_number(raw_query: str,
                       translated: str,
                       char_pathways: Dict[str, str],
                       seq_by_num: Dict[Tuple[str, int], str],
                       ru_to_en: Dict[str, str]) -> Optional[str]:
    """
    «Как Клейн перешёл на 4 последовательность?» → «Klein Moretti Bizarro Sorcerer
    advancement». Текст книги о переходе редко содержит номер последовательности —
    только её имя, поэтому резолвим номер через справочник персонаж→путь и
    карту (путь, номер) → название последовательности (+ термины ритуала
    из _PATHWAY_RITUAL_TERMS, если известны).
    Работает только для персонажей из справочника (иначе путь неизвестен).
    """
    n = _detect_seq_number(raw_query, translated)
    if n is None:
        return None

    q_lower = raw_query.lower()
    for char_ru, pathway in char_pathways.items():
        # Имя персонажа: полное имя или первое слово (имя) со стемом
        stems = [_ru_name_stem(char_ru).lower(),
                 _ru_name_stem(char_ru.split()[0]).lower()]
        if not any(re.search(r'\b' + re.escape(s) + r'[а-яё]*', q_lower)
                   for s in stems):
            continue
        seq_name = seq_by_num.get((pathway, n))
        if not seq_name:
            return None
        char_en = ru_to_en.get(char_ru, char_ru)
        ritual = _PATHWAY_RITUAL_TERMS.get((pathway, n), "")
        expansion = f"{char_en} {seq_name} advancement"
        if ritual:
            expansion += f" {ritual}"
        logger.info(
            f"[BookSearch] Seq number expand: {char_ru} + seq {n} "
            f"({pathway}) -> {expansion!r}"
        )
        return expansion
    return None


def _entities_survived(translated: str, used_en: List[str]) -> bool:
    """
    Контроль целостности перевода: все en-формы, которые regex-подстановка
    РЕАЛЬНО вставила в запрос (см. _translate_query_tracked), должны
    присутствовать и в переводе — иначе переводчик потерял сущность.
    Проверка только по применённым заменам: паттерны пересекаются
    (авто-алиас «Гаданий» и запись «Гадание» матчат одно слово), и проверка
    всех паттернов подряд давала ложные отказы хорошего перевода.
    """
    t = translated.lower()
    return all(en.lower() in t for en in used_en)


def _translate_full_query(query: str,
                          ru_to_en: Dict[str, str],
                          patterns: Optional[List[Tuple[re.Pattern, str]]] = None) -> str:
    """
    Полный перевод русского запроса на английский.
    Пайплайн:
      1. Regex-глоссарий: детерминированная подстановка имён/терминов
         (стем-матчинг с падежами + авто-алиасы суффиксов).
      2. Полный перевод оставшегося русского текста:
         Ollama (локально) → Google Translate → словарь _RU_EN_QUERY_MAP.
      3. Контроль целостности: если перевод потерял сущности глоссария
         из исходного запроса — откат на regex-вариант шага 1 (смешанный
         RU/EN запрос: имена на английском, остальное на русском —
         мультиязычному эмбеддеру этого достаточно).
    """
    # Если в запросе нет русского — нечего переводить
    if not re.search(r'[а-яёА-ЯЁ]', query):
        return query

    # --- Этап 1: подстановка имён regex-глоссарием (детерминированно) ---
    working, used_en = _translate_query_tracked(query, patterns) \
        if patterns else (query, [])
    if working != query:
        logger.info(f"[BookSearch] Names (regex): '{query}' -> '{working}'")

    # Если после подстановки не осталось русского — готово
    if not re.search(r'[а-яёА-ЯЁ]', working):
        return working

    # --- Этап 2: полный перевод ---
    translated = _translate_via_ollama(working)
    if not translated:
        translated = _google_translate(working)
    if translated:
        # Постобработка pathway-названий к каноническому виду "X Pathway"
        translated = re.sub(r'\bpath(?:way)?\s+of\s+(?:the\s+)?([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)\b', r'\1 Pathway', translated, flags=re.IGNORECASE)
        translated = re.sub(r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)'s\s+(?:path|pathway)\b", r'\1 Pathway', translated, flags=re.IGNORECASE)
        translated = re.sub(r'\bPathway\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)\b', r'\1 Pathway', translated)
        logger.info(f"[BookSearch] Translate: '{working}' -> '{translated}'")
    else:
        # Fallback: словарная подстановка. Строго по границам слов, кириллицу
        # НЕ удаляем: мультиязычный эмбеддер её понимает, а BM25-токенизатор
        # сам отбросит — так сущность запроса переживает любой сбой перевода.
        logger.info(f"[BookSearch] Ollama/Google недоступны, using dictionary fallback")
        translated = working
        for ru, en in _RU_EN_QUERY_MAP:
            translated = re.sub(r'\b' + re.escape(ru) + r'\b', en,
                                translated, flags=re.IGNORECASE)
        translated = re.sub(r'\s+', ' ', translated).strip()

    # --- Этап 3: контроль целостности сущностей ---
    if not _entities_survived(translated, used_en):
        logger.warning(
            f"[BookSearch] Translate lost entities: '{translated}', "
            f"fallback to regex version: '{working}'"
        )
        return working
    return translated


# Маппинг названий томов → номер тома
VOLUME_NAMES: Dict[str, int] = {
    # Русские (цифры)
    "том 1": 1, "книга 1": 1, "первый том": 1, "первая книга": 1,
    "том 2": 2, "книга 2": 2, "второй том": 2, "вторая книга": 2,
    "том 3": 3, "книга 3": 3, "третий том": 3, "третья книга": 3,
    "том 4": 4, "книга 4": 4, "четвёртый том": 4, "четвертый том": 4, "четвёртая книга": 4,
    "том 5": 5, "книга 5": 5, "пятый том": 5, "пятая книга": 5,
    "том 6": 6, "книга 6": 6, "шестой том": 6, "шестая книга": 6,
    "том 7": 7, "книга 7": 7, "седьмой том": 7, "седьмая книга": 7,
    "том 8": 8, "книга 8": 8, "восьмой том": 8, "восьмая книга": 8,
    "том 0": 0, "книга 0": 0, "нулевой том": 0,
    # Русские (словами — без "том"/"книга")
    "в первом": 1, "в первой": 1, "первый": 1, "первая": 1, "один": 1, "одна": 1,
    "во втором": 2, "во второй": 2, "второй": 2, "вторая": 2, "два": 2, "две": 2,
    "в третьем": 3, "в третьей": 3, "третий": 3, "третья": 3, "три": 3,
    "в четвёртом": 4, "в четвертом": 4, "в четвёртой": 4, "четвёртый": 4, "четвертый": 4, "четвёртая": 4, "четыре": 4,
    "в пятом": 5, "в пятой": 5, "пятый": 5, "пятая": 5, "пять": 5,
    "в шестом": 6, "в шестой": 6, "шестой": 6, "шестая": 6, "шесть": 6,
    "в седьмом": 7, "в седьмой": 7, "седьмой": 7, "седьмая": 7, "семь": 7,
    "в восьмом": 8, "в восьмой": 8, "восьмой": 8, "восьмая": 8, "восемь": 8,
    "в нулевом": 0, "нулевой": 0, "ноль": 0, "нуль": 0,
    # Русские (падежи)
    "первую книгу": 1, "вторую книгу": 2, "третью книгу": 3,
    "четвёртую книгу": 4, "четвертую книгу": 4,
    "пятую книгу": 5, "шестую книгу": 6, "седьмую книгу": 7, "восьмую книгу": 8,
    "первый том": 1, "второй том": 2, "третий том": 3,
    "четвёртый том": 4, "четвертый том": 4,
    "пятый том": 5, "шестой том": 6, "седьмой том": 7, "восьмой том": 8,
    # Русские (падежи — родительный)
    "первого тома": 1, "второго тома": 2, "третьего тома": 3,
    "четвёртого тома": 4, "четвертого тома": 4,
    "пятого тома": 5, "шестого тома": 6, "седьмого тома": 7, "восьмого тома": 8,
    "первой книги": 1, "второй книги": 2, "третьей книги": 3,
    "четвёртой книги": 4, "четвертой книги": 4,
    "пятой книги": 5, "шестой книги": 6, "седьмой книги": 7, "восьмой книги": 8,
    # Русские (падежи — предложный)
    "первом томе": 1, "втором томе": 2, "третьем томе": 3,
    "четвёртом томе": 4, "четвертом томе": 4,
    "пятом томе": 5, "шестом томе": 6, "седьмом томе": 7, "восьмом томе": 8,
    "первой книге": 1, "второй книге": 2, "третьей книге": 3,
    "четвёртой книге": 4, "четвертой книге": 4,
    "пятой книге": 5, "шестой книге": 6, "седьмой книге": 7, "восьмой книге": 8,
    # Английские
    "volume 1": 1, "book 1": 1, "first volume": 1, "first book": 1,
    "volume 2": 2, "book 2": 2, "second volume": 2, "second book": 2,
    "volume 3": 3, "book 3": 3, "third volume": 3, "third book": 3,
    "volume 4": 4, "book 4": 4, "fourth volume": 4, "fourth book": 4,
    "volume 5": 5, "book 5": 5, "fifth volume": 5, "fifth book": 5,
    "volume 6": 6, "book 6": 6, "sixth volume": 6, "sixth book": 6,
    "volume 7": 7, "book 7": 7, "seventh volume": 7, "seventh book": 7,
    "volume 8": 8, "book 8": 8, "eighth volume": 8, "eighth book": 8,
    "volume 0": 0, "book 0": 0, "zeroth volume": 0, "zeroth book": 0,
    "in the first volume": 1, "in the first book": 1,
    "in the second volume": 2, "in the second book": 2,
    "in the third volume": 3, "in the third book": 3,
    "in the fourth volume": 4, "in the fourth book": 4,
    "in the fifth volume": 5, "in the fifth book": 5,
    "in the sixth volume": 6, "in the sixth book": 6,
    "in the seventh volume": 7, "in the seventh book": 7,
    "in the eighth volume": 8, "in the eighth book": 8,
    "in first volume": 1, "in first book": 1,
    "in second volume": 2, "in second book": 2,
    "in third volume": 3, "in third book": 3,
    "in fourth volume": 4, "in fourth book": 4,
    "in fifth volume": 5, "in fifth book": 5,
    "in sixth volume": 6, "in sixth book": 6,
    "in seventh volume": 7, "in seventh book": 7,
    "in eighth volume": 8, "in eighth book": 8,
    "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8,
    # Названия арок (рус) — только с явным указанием "арка"/"arc"/"volume"
    "арка клоун": 1, "clown arc": 1,
    "арка безликий": 2, "faceless arc": 2,
    "арка путешественник": 3, "traveler arc": 3, "traveller arc": 3,
    "арка бессмертный": 4, "undying arc": 4,
    "арка красный жрец": 5, "red priest arc": 5,
    "арка искатель света": 6, "lightseeker arc": 6,
    "арка повешенный": 7, "hanged man arc": 7,
    "арка шут": 8, "fool arc": 8,
    "истории": 0, "side stories": 0, "сторис": 0,
}


def detect_volume(query: str) -> Optional[int]:
    """
    Распознаёт упоминание конкретного тома/арки в запросе пользователя.
    Возвращает номер тома (0-8) или None.
    Использует границы слов, чтобы 'one' не совпало с 'Simone'.
    """
    q_lower = query.lower()
    for name, vol in sorted(VOLUME_NAMES.items(), key=lambda x: -len(x[0])):
        pattern = r'\b' + re.escape(name) + r'\b'
        if re.search(pattern, q_lower):
            return vol
    return None


# Числительные словами без «том/книга» («один», «два», «три»...) — слишком
# шумные для мультитомного детекта: почти любой текст их содержит.
_VOL_GENERIC_WORDS = {
    "один", "одна", "два", "две", "три", "четыре",
    "пять", "шесть", "семь", "восемь", "ноль", "нуль",
}
_VOL_NUM_RE = re.compile(
    r"(?:том|книга|volume|book|vol\.?)\s*(\d+)"
    r"|\b(\d+)\s*-\s*(?:й|ый|ой|th|st|nd|rd)\s*(?:том|книга|volume|book)",
    re.IGNORECASE,
)


def detect_volumes(query: str) -> List[int]:
    """
    Все тома, упомянутые в запросе (для мультитомных вопросов вида
    «год в первом томе и год в 8-м»). Без числительных словами —
    они ловятся в обычных фразах («два дня»).
    """
    q_lower = query.lower()
    found = set()
    for name, vol in sorted(VOLUME_NAMES.items(), key=lambda x: -len(x[0])):
        if name in _VOL_GENERIC_WORDS:
            continue
        if re.search(r'\b' + re.escape(name) + r'\b', q_lower):
            found.add(vol)
    for m in _VOL_NUM_RE.finditer(q_lower):
        for g in m.groups():
            if g and g.isdigit() and 0 <= int(g) <= 8:
                found.add(int(g))
    # «в 8» без слова «том» — только если «том/книга» уже есть в запросе
    if re.search(r"том|книг|volume|book", q_lower):
        for m in re.finditer(r"\bв\s*(\d)\b|\bin\s+(\d)\b", q_lower):
            v = int(m.group(1) or m.group(2))
            if 1 <= v <= 8:
                found.add(v)
    return sorted(found)


# ---------------------------------------------------------------------------
# Recency-детект: запрос про «последнее/финальное» событие.
# Для таких вопросов фрагменты из разных томов описывают РАЗНЫЕ события
# (напр. битвы Клейна и Амона в Vol.6 и Vol.8) и модель склеивает их в одно.
# ---------------------------------------------------------------------------
_RECENCY_RU = re.compile(r'\b(последн\w+|финальн\w+)\b', re.IGNORECASE)
_RECENCY_EN = re.compile(r'\b(last|final|latest|finale)\b', re.IGNORECASE)

# Минимальное число кандидатов из максимального тома, чтобы recency-фильтр
# сработал (защита от шума: один случайный чанк из Vol.8 не должен
# отфильтровать релевантные результаты из Vol.6).
RECENCY_MIN_CANDIDATES = 5


def detect_recency(raw_query: str, translated_query: str) -> bool:
    """True, если запрос про «последнее/финальное» событие книги."""
    return bool(
        _RECENCY_RU.search(raw_query) or _RECENCY_EN.search(translated_query)
    )


# ---------------------------------------------------------------------------
# Детект обзорного вопроса («расскажи про X», «кто такой X»).
# Такие вопросы про сущность в целом — chunk-поиск даёт случайные обрывки,
# поэтому они уходят в поиск по саммари глав (коллекция lotm_summaries).
# Детект детерминированный (regex по сырому русскому запросу): перевод и
# дистилляция через Ollama/Google нестабильны и могут потерять сущность.
# ---------------------------------------------------------------------------
_OVERVIEW_RU_PAT = re.compile(
    r'\b(расскажи про|расскажи обо?|кто такой|кто такая|кто такие|кто был|'
    r'кто была|что такое|что ты знаешь о|что ты знаешь про|опиши)\b',
    re.IGNORECASE,
)
_OVERVIEW_EN_PAT = re.compile(
    r'\b(tell me about|who is|who was|who are|what is|what are|describe)\b',
    re.IGNORECASE,
)
# Слова-события: вопрос про конкретное событие, а не про сущность — обзорный
# режим не нужен (такие вопросы обслуживает обычный chunk-поиск + recency).
_EVENT_WORDS_RU = (
    "битв", "сражен", "бой", "войн", "смерт", "убий", "ритуал",
    "финал", "последн", "когда", "встреч", "погиб", "умер",
)
_EVENT_WORDS_EN = (
    "battle", "fight", "war", "death", "kill", "ritual", "vs", "versus",
    "finale", "final", "last", "first time", "happened", "when",
)
# Незначимые слова для остатка обзорного вопроса (после вырезания
# вопросной фразы и сущностей не должно остаться ничего, кроме них).
_OVERVIEW_STOPWORDS = {
    "и", "в", "во", "на", "о", "об", "обо", "про", "у", "с", "со", "к", "ко",
    "из", "по", "за", "от", "до", "для", "или", "а", "но", "не", "же", "ли",
    "что", "как", "где", "кто", "кем", "чем", "это", "этот", "эта", "его",
    "её", "их", "им", "ей", "все", "всё", "так", "там", "тут", "ещё", "уже",
    "мне", "тебе", "знаешь", "можешь", "очень",
}


def _is_overview_query(raw_query: str, translated: str,
                       distilled: Optional[str],
                       patterns: Optional[List[Tuple[re.Pattern, str]]] = None) -> bool:
    """
    True для широких вопросов про сущность В ЦЕЛОМ:
      1. вопросный паттерн («расскажи про»/«кто такой»/«tell me about»/...);
      2. ни одного слова-события в сыром запросе, переводе или дистилляте;
      3. после вырезания вопросной фразы и всех терминов глоссария не
         остаётся значимых слов — иначе это предметный вопрос («остров,
         где запечатан Розель», «кто дети Розеля»), и ему место
         в chunk-поиске, а не в саммари всей сущности.
    Примеры: 'расскажи про тинген' → True;
    'расскажи про остров, где запечатан Розель' → False (остров/запечатан).
    """
    q = raw_query.lower()
    if any(w in q for w in _EVENT_WORDS_RU):
        return False
    for text in ((translated or "").lower(), (distilled or "").lower()):
        if any(w in text for w in _EVENT_WORDS_EN):
            return False
    if not (_OVERVIEW_RU_PAT.search(raw_query)
            or _OVERVIEW_EN_PAT.search(translated or "")):
        return False
    # Проверка «чистоты» вопроса: вырезаем вопросную фразу и заменяем
    # термины глоссария (regex-паттерны уже учитывают падежи); если
    # кириллическое слово >= 3 букв не из стоп-листа осталось — вопрос
    # предметный, а не обзорный.
    if patterns:
        remainder = _OVERVIEW_RU_PAT.sub(" ", raw_query)
        remainder_en = _translate_query(remainder, patterns)
        for w in re.findall(r'[а-яёА-ЯЁ]{3,}', remainder_en):
            if w.lower() not in _OVERVIEW_STOPWORDS:
                return False
    return True


# Токен-идентификатор артефакта: «2-049», «0-08», «3-0271»
_ARTIFACT_TOKEN_RE = re.compile(r'\b\d-\d{2,}\b')

# Вопрос про имя/название
_NAME_Q_RU = re.compile(r'\b(называ\w*|названи\w*|зовут)\b', re.IGNORECASE)
_NAME_Q_EN = re.compile(r'\b(name of|called|what.{0,20}\bname\b)', re.IGNORECASE)
# Просьба процитировать текст (почётное имя, заклинание, цитата)
_RECITE_Q_RU = re.compile(
    r'\b(почётн\w+\s+им\w*|заклинани\w*|процитируй|прочти\b|дословно|'
    r'произнеси|напиши\s+(?:мне\s+)?текст)\b', re.IGNORECASE)
_RECITE_Q_EN = re.compile(r'\b(honorific name|incantation|recite|quote)\b', re.IGNORECASE)


def _asks_name(raw_ru: str, translated: str) -> bool:
    """True, если вопрос — про имя/название («как назывался?», «what is it called?»)."""
    return bool(_NAME_Q_RU.search(raw_ru) or _NAME_Q_EN.search(translated or ""))


def _asks_recite(raw_ru: str, translated: str) -> bool:
    """True, если просьба процитировать текст («почётное имя?», «recite», «quote»)."""
    return bool(_RECITE_Q_RU.search(raw_ru) or _RECITE_Q_EN.search(translated or ""))


# ---------------------------------------------------------------------------
# Дословная цитата из главы (для recite-запросов): топ-N глав целиком отдаём
# в LLM, она выписывает точный фрагмент дословно. Полные главы склеиваются
# из чанков (overlap при загрузке — CHUNK_OVERLAP символов, стыки точные).
# ---------------------------------------------------------------------------

def _merge_chunks(chunks: List[str]) -> str:
    """
    Склеивает перекрывающиеся чанки главы в сплошной текст.
    Перекрытие при загрузке книги — точное повторение хвоста предыдущего
    чанка в начале следующего, поэтому ищем максимальное совпадение
    «хвост склеенного == голова следующего» и срезаем его.
    """
    if not chunks:
        return ""
    merged = chunks[0]
    for nxt in chunks[1:]:
        overlap = 0
        for k in range(min(600, len(merged), len(nxt)), 19, -1):
            if merged.endswith(nxt[:k]):
                overlap = k
                break
        merged += nxt[overlap:] if overlap else "\n\n" + nxt
    return merged


# Промпт извлечения цитаты: модель копирует фрагмент главы дословно.
# Английский — текст глав английский, экстракция надёжнее на одном языке.
_EXTRACT_QUOTE_PROMPT = """In the chapter text below, find the exact passage that answers the user's question and quote it VERBATIM (copy the original English text, up to 5 sentences). Output ONLY the quoted passage — no translation, no commentary. If the chapter contains no passage answering the question, output exactly NONE.

Question: {query}
Chapter: {chapter}

Chapter text:
{text}

Passage:"""

# Максимум символов полной главы, отправляемых в LLM (защита от гигантских глав)
_QUOTE_CHAPTER_MAX_CHARS = 40000


# ---------------------------------------------------------------------------
# Алиасы одного персонажа. Вопрос «про Клейна в начале книги» — это про
# Чжоу Минжуя: cross-encoder не связывает «Klein» ↔ «Zhou Mingrui»
# (скор -6 на правильном фрагменте), поэтому добавляем варианты запроса
# с заменой на алиасы и для retrieval, и для rerank (max по запросам).
# ---------------------------------------------------------------------------
_IDENTITY_ALIASES: List[List[str]] = [
    # Длинные формы первыми — при совпадении нескольких базой берётся
    # самая длинная («Klein Moretti», а не «Klein» внутри неё).
    ["Klein Moretti", "Klein", "Zhou Mingrui", "Sherlock Moriarty",
     "Gehrman Sparrow", "Dwayne Dantès", "Merlin Hermes",
     "Honorable Mister Fool", "Mister Fool", "Mr. Fool", "The Fool"],
]


def _expand_with_aliases(translated: str, limit: int = 3) -> List[str]:
    """
    Варианты запроса с заменой имени персонажа на его алиасы.
    Срабатывает, только если в переведённом запросе есть персонаж
    из _IDENTITY_ALIASES. Не срабатывает для вопросов про путь
    («Fool pathway» — про путь, а не про Мистера Шута).
    """
    if "pathway" in translated.lower():
        return []
    out: List[str] = []
    for group in _IDENTITY_ALIASES:
        present = [a for a in group
                   if re.search(r'\b' + re.escape(a) + r'\b', translated, re.IGNORECASE)]
        if not present:
            continue
        present.sort(key=len, reverse=True)
        base = present[0]
        for alias in group:
            if alias == base:
                continue
            variant = re.sub(r'\b' + re.escape(base) + r'\b', alias,
                             translated, flags=re.IGNORECASE)
            if variant not in out:
                out.append(variant)
    return out[:limit]


# ---------------------------------------------------------------------------
# Концепт-синонимы: вопрос пользователя и текст книги называют одно и то же
# разными словами («почётное имя» vs «incantation», «все ангелы» vs
# «six angels»). Добавляем книжные термины в fan-out — retrieval их ловит,
# без хардкода фактов в словарь.
# ---------------------------------------------------------------------------
_CONCEPT_EXPANSIONS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r'почётн\w+\s+им\w*', re.IGNORECASE), "incantation"),
    (re.compile(r'\bзаклинани\w*', re.IGNORECASE), "incantation"),
    (re.compile(r'\bвсе\s+ангел\w*', re.IGNORECASE), "six angels"),
    (re.compile(r'\bсписок\s+ангел\w*', re.IGNORECASE), "six angels"),
    # Перемещение в другой мир: в тексте книги это «transmigrate» —
    # без термина cross-encoder не связывает «shift to another world»
    # со сценой трансмиграции Чжоу Минжуя (скор -8 на правильном чанке).
    (re.compile(
        r'трансмиграц\w*|перерод\w+|переселени\w+\s+душ|'
        r'(?:перемещ\w*|перен[её]с\w*|попа\w+)\s+в\s+(?:другой|иной|новый)\s+мир',
        re.IGNORECASE), "transmigrate transmigration another world"),
    (re.compile(r'усилени\w*\s+удач\w*', re.IGNORECASE), "luck enhancement ritual"),
    # «Трофеи/добыча»: в тексте книги это «spoils (of war)» — перевод
    # «trophies» не матчится ни векторно, ни по BM25 (кейс qa_eval:
    # делёжка добычи после боя со Стивом терялась полностью).
    (re.compile(r'\bтрофе\w+|\bдобыч\w*|\bнаграблен\w+|\bтрофейн\w+',
                re.IGNORECASE), "spoils spoils of war loot"),
    # «Отговорка/оправдание»: книжная лексика — excuse/pretext/alibi
    # (кейс qa_eval: отговорка Клейна для Бенсона и Мелиссы).
    (re.compile(r'\bотговорк\w*|\bоправдани\w*|\bотмазк\w*|\bприкрыти\w+\s+для\s+лжи',
                re.IGNORECASE), "excuse pretext alibi"),
]


def _expand_concepts(raw_query: str, translated: str) -> Optional[str]:
    """
    Дополнительный fan-out запрос: переведённый запрос + книжные термины
    из _CONCEPT_EXPANSIONS (если концепт найден в сыром русском запросе).
    """
    extra = [terms for pat, terms in _CONCEPT_EXPANSIONS if pat.search(raw_query)]
    if not extra:
        return None
    return f"{translated} {' '.join(extra)}"


class BookSearch:
    """Поиск релевантных фрагментов книги: гибрид векторный + BM25 + cross-encoder rerank."""

    def __init__(self,
                 context: str = "arrodes",
                 collection_name: str = "lord_of_mysteries",
                 model_name: str = "intfloat/multilingual-e5-base",
                 max_distance: float = 0.50,
                 alpha: float = 0.6,
                 rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
                 n_candidates: int = 100,
                 router=None):
        """
        Args:
            max_distance: порог дистанции (0.45 строго, 0.50 умеренно, 0.55 мягко).
            alpha: вес векторного скора в гибридном поиске (0=только BM25, 1=только вектор).
                Устарело: слияние вариантов теперь через RRF (параметр
                оставлен для совместимости, ни на что не влияет).
            rerank_model: cross-encoder для reranking (None = выключить).
            n_candidates: сколько кандидатов собирать перед rerank.
        """
        self._db_path = f"data/{context}/book"
        self._collection_name = collection_name
        self._model_name = model_name
        # e5-модели обучены с префиксами: документы — 'passage: ', запросы — 'query: '.
        # (Предыдущая модель, paraphrase-multilingual-MiniLM-L12-v2, работала с
        # 128 токенами и обрезала чанки по [SUMMARY:]-префиксу.)
        _is_e5 = "e5" in model_name.lower()
        self._query_prefix = "query: " if _is_e5 else ""
        self._doc_prefix = "passage: " if _is_e5 else ""
        self._client = None
        self._collection = None
        self._summaries_collection = None
        self._embedder = None
        self._router = router
        self._sum_docs: Optional[List[str]] = None
        self._sum_meta: List[Dict] = []
        self._sum_ids: List[str] = []
        self._max_distance = max_distance
        self._alpha = alpha
        self._rerank_model_name = rerank_model
        self._n_candidates = n_candidates
        self._reranker = None

        # BM25 state
        self._bm25 = None
        self._bm25_docs: List[str] = []
        self._bm25_meta: List[Dict] = []
        self._bm25_ids: List[str] = []

        glossary_path = Path(__file__).parent.parent / "personas" / "arrodes_glossary.yaml"
        ru_to_en = _load_ru_to_en(str(glossary_path))
        self._ru_to_en = ru_to_en
        self._patterns = _build_patterns(ru_to_en)
        self._pathway_to_seqs, self._seq_to_pathway, self._seq_by_num = \
            _build_pathway_map(str(glossary_path))
        self._char_pathways = _build_character_pathways(str(glossary_path))
        if ru_to_en:
            logger.info(
                f"[BookSearch] Loaded {len(ru_to_en)} RU->EN mappings, "
                f"{len(self._patterns)} patterns, "
                f"{len(self._pathway_to_seqs)} pathways, "
                f"{len(self._seq_to_pathway)} sequences, "
                f"{len(self._char_pathways)} character pathways"
            )

    @staticmethod
    def _is_proper_noun_in(ru_key: str, raw_query: str) -> bool:
        """
        Проверяет, что термин глоссария встречается в запросе как имя
        собственное (с заглавной буквы). Иначе служебные слова вроде
        «последовательность» (кириллица, длина >= 4) принимаются за
        персонажа и дизъюнктивное расширение строится от них.
        """
        stem = re.escape(_ru_name_stem(ru_key))
        m = re.search(r'\b' + stem + r'[а-яёА-ЯЁ]*', raw_query)
        return bool(m and m.group(0)[0].isupper())

    def _expand_seqnum_disjunctive(self, n: int, raw_query: str) -> Optional[str]:
        """
        Персонаж ВНЕ справочника: путь неизвестен, поэтому добавляем
        названия seq N всех путей одним запросом — имя персонажа в запросе
        само заякорит нужное при ранжировании (глава о переходе содержит
        и персонажа, и название его последовательности).
        В rerank-запрос не добавляется (21 термин размывает cross-encoder).
        """
        entries = _find_glossary_entries(raw_query, self._ru_to_en)
        person_keys = sorted(
            (k for k in entries
             if _is_person_name(k) and self._is_proper_noun_in(k, raw_query)),
            key=len, reverse=True,
        )
        if not person_keys:
            return None
        char_en = entries[person_keys[0]]
        names: List[str] = []
        seen = set()
        for (pathway, num), name in self._seq_by_num.items():
            if num == n and name not in seen:
                seen.add(name)
                names.append(name)
        if not names:
            return None
        expansion = f"{char_en} {' '.join(names)} advancement"
        logger.info(
            f"[BookSearch] Seq number expand (all pathways): "
            f"{person_keys[0]} + seq {n} -> {len(names)} names"
        )
        return expansion

    def _load_reranker(self):
        """Ленивая загрузка cross-encoder."""
        if self._reranker is not None or self._rerank_model_name is None:
            return
        try:
            from sentence_transformers import CrossEncoder
            logger.info(f"[BookSearch] Loading reranker: {self._rerank_model_name}")
            self._reranker = CrossEncoder(self._rerank_model_name)
            logger.info("[BookSearch] Reranker ready")
        except Exception as e:
            logger.warning(f"[BookSearch] Reranker load failed: {e}")
            self._rerank_model_name = None

    def _load_bm25(self) -> bool:
        """Загружает все чанки из ChromaDB и строит BM25 индекс."""
        if self._bm25 is not None:
            return True
        if not self._ensure_connection():
            return False
        try:
            logger.info("[BookSearch] Building BM25 index...")
            data = self._collection.get(include=["documents", "metadatas"])
            docs = data.get("documents") or []
            metas = data.get("metadatas") or []
            ids = data.get("ids") or []
            if not docs:
                logger.warning("[BookSearch] No documents for BM25")
                return False

            tokenized = [_tokenize(d) for d in docs]
            from rank_bm25 import BM25Okapi
            self._bm25 = BM25Okapi(tokenized)
            self._bm25_docs = docs
            self._bm25_meta = metas
            self._bm25_ids = ids
            logger.info(f"[BookSearch] BM25 index ready: {len(docs)} docs")
            return True
        except Exception as e:
            logger.warning(f"[BookSearch] BM25 init error: {e}")
            return False

    def _bm25_search(self, query: str, top_k: int = 100,
                     volume: Optional[int] = None) -> List[Dict]:
        """BM25 поиск по чанкам."""
        if not self._load_bm25():
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        # Собираем индексы с учётом volume filter
        indexed = []
        for idx, score in enumerate(scores):
            if score <= 0:
                continue
            meta = self._bm25_meta[idx] if idx < len(self._bm25_meta) else {}
            if volume is not None and meta.get("volume") != volume:
                continue
            indexed.append((idx, score))
        indexed.sort(key=lambda x: x[1], reverse=True)
        results = []
        for idx, score in indexed[:top_k]:
            meta = self._bm25_meta[idx] if idx < len(self._bm25_meta) else {}
            results.append({
                "text": self._bm25_docs[idx],
                "bm25_score": float(score),
                "_chunk_id": self._bm25_ids[idx],
                "volume": meta.get("volume", "?"),
                "volume_name": meta.get("volume_name", "?"),
                "chapter": meta.get("chapter", "?"),
            })
        return results

    def _ensure_connection(self) -> bool:
        if self._collection is not None:
            return True
        try:
            self._client = chromadb.PersistentClient(path=self._db_path)
            self._embedder = SentenceTransformerEmbeddingFunction(
                model_name=self._model_name
            )
            # e5-base нативно работает с 512 токенами — [SUMMARY:]-префикс
            # и тело чанка помещаются целиком. (У прежней MiniLM лимит 128
            # обрезал чанк по префиксу; поднятие лимита до 512 у неё —
            # мусор: позиционных эмбеддингов за 128 нет.)
            self._embedder._model.max_seq_length = 512
            self._collection = self._client.get_collection(
                self._collection_name,
                embedding_function=self._embedder
            )
            return True
        except Exception as e:
            logger.error(f"[BookSearch] Connection error: {e}")
            return False

    @staticmethod
    def _to_list(embs):
        """np.ndarray | list → list для chromadb."""
        return [e.tolist() if hasattr(e, "tolist") else e for e in embs]

    def _embed_docs(self, texts: List[str]):
        """Эмбеддинги документов/окон; для e5 — с префиксом 'passage: '."""
        if self._doc_prefix:
            return self._embedder._model.encode(
                [self._doc_prefix + t for t in texts])
        return self._embedder(texts)

    def _embed_queries(self, texts: List[str]):
        """Эмбеддинги запросов; для e5 — с префиксом 'query: '."""
        if self._query_prefix:
            return self._embedder._model.encode(
                [self._query_prefix + t for t in texts])
        return self._embedder(texts)

    def _ensure_summaries(self) -> bool:
        """Ленивое подключение к коллекции саммари глав (lotm_summaries)."""
        if self._summaries_collection is not None:
            return True
        if not self._ensure_connection():
            return False
        try:
            self._summaries_collection = self._client.get_collection(
                "lotm_summaries",
                embedding_function=self._embedder
            )
            return True
        except Exception:
            # Коллекция не построена (scripts/build_summaries_index.py) —
            # обзорный режим молча отключён, обычный поиск продолжает работать.
            return False

    def _load_summaries_cache(self) -> bool:
        """Загружает все саммари в память для литерального поиска по сущности."""
        if self._sum_docs is not None:
            return True
        if not self._ensure_summaries():
            return False
        try:
            data = self._summaries_collection.get(include=["documents", "metadatas"])
            self._sum_docs = data.get("documents") or []
            self._sum_meta = data.get("metadatas") or []
            self._sum_ids = data.get("ids") or []
            return bool(self._sum_docs)
        except Exception as e:
            logger.warning(f"[BookSearch] summaries cache error: {e}")
            return False

    def _summaries_by_entity(self, entity_term: str,
                             min_hits: int = 3) -> List[Dict]:
        """
        Детерминированный отбор саммари, где сущность упоминается буквально.
        Для редких имён собственных это надёжнее векторного поиска: эмбеддер
        и cross-encoder слабо различают «Tingen», а подстрока — точно.
        Пусто, если совпадений меньше min_hits (тогда срабатывает векторный путь).
        """
        if not entity_term or re.search(r'[а-яёА-ЯЁ]', entity_term):
            return []
        if not self._load_summaries_cache():
            return []
        term = entity_term.lower()
        out: List[Dict] = []
        for i, doc in enumerate(self._sum_docs):
            if term not in doc.lower():
                continue
            meta = self._sum_meta[i] if i < len(self._sum_meta) else {}
            out.append({
                "text": doc,
                "distance": 0.0,  # литеральное совпадение
                "volume": meta.get("volume", "?"),
                "volume_name": meta.get("volume_name", "?"),
                "chapter": meta.get("chapter", "?"),
                "chapter_num": meta.get("chapter_num", 0),
                "kind": "summary",
            })
        if len(out) < min_hits:
            return []
        logger.info(
            f"[BookSearch] Overview entity match: {len(out)} summaries "
            f"contain {entity_term!r}"
        )
        return out

    def _summaries_by_distance(self, queries: List[str],
                               n_results: int = 3) -> List[Dict]:
        """
        Векторный поиск по саммари глав БЕЗ rerank'а. Для name-вопросов
        («как назывался?») cross-encoder топит саммари с явным ответом
        (гл. 1328 с «creates the town of Utopia» уходил ниже случайных
        саммари про города), а дистанция по всем вариантам запроса
        стабильно ставит его первым.
        """
        if not self._ensure_summaries():
            return []
        seen: Dict[str, Dict] = {}
        for q in queries:
            try:
                res = self._summaries_collection.query(
                    query_embeddings=self._to_list(self._embed_queries([q])),
                    n_results=15
                )
            except Exception as e:
                logger.warning(f"[BookSearch] summaries distance query error: {e}")
                continue
            for i in range(len(res["ids"][0])):
                cid = res["ids"][0][i]
                dist = res["distances"][0][i] if "distances" in res else 1.0
                if cid in seen and seen[cid]["distance"] <= dist:
                    continue
                meta = res["metadatas"][0][i] if res["metadatas"] else {}
                seen[cid] = {
                    "text": res["documents"][0][i],
                    "distance": dist,
                    "volume": meta.get("volume", "?"),
                    "volume_name": meta.get("volume_name", "?"),
                    "chapter": meta.get("chapter", "?"),
                    "chapter_num": meta.get("chapter_num", 0),
                    "kind": "summary",
                    "rerank_score": float((1.0 - dist) * 10),
                }
        top = sorted(seen.values(), key=lambda x: x["distance"])[:n_results]
        top.sort(key=lambda x: x.get("chapter_num", 0))
        return top

    def _search_summaries(self, queries: List[str], rerank_query: str,
                          n_results: int = 10,
                          entity_terms: Optional[List[str]] = None) -> List[Dict]:
        """
        Поиск по саммари глав для обзорных вопросов («расскажи про X»).
        Если сущности известны (entity_terms) — сначала литеральный отбор
        саммари с упоминанием термина (перебор от длинных к коротким);
        иначе/если мало совпадений — векторный fan-out теми же запросами,
        что и chunk-поиск. Далее rerank cross-encoder'ом → топ-n_results
        глав в порядке следования.
        """
        if not self._ensure_summaries():
            return []

        # Литеральный путь: все саммари, где сущность упомянута буквально.
        # Пулы ВСЕХ терминов объединяются (дедуп по главе): полное имя
        # («Leonard Mitchell») даёт 10 саммари из ранних томов, короткая
        # форма («Leonard») — ещё 80, включая поздние тома. Поодиночке
        # беря только самый длинный термин, теряем актуальное состояние
        # персонажа. Пул больше MAX_LITERAL_POOL пропускаем (недискриминативен).
        MAX_LITERAL_POOL = 150
        candidates: List[Dict] = []
        used_term: Optional[str] = None
        merged: Dict[int, Dict] = {}
        for term in (entity_terms or []):
            pool = self._summaries_by_entity(term)
            if not pool:
                continue
            if len(pool) > MAX_LITERAL_POOL:
                logger.info(
                    f"[BookSearch] Overview entity {term!r}: pool {len(pool)} "
                    f"> {MAX_LITERAL_POOL}, skipping literal match"
                )
                continue
            for c in pool:
                merged.setdefault(c.get("chapter_num", 0), c)
            used_term = used_term or term
        if merged:
            candidates = list(merged.values())
            rerank_query = f"Tell me about {used_term}"

        # Векторный путь (фолбэк)
        if not candidates:
            seen: Dict[str, Dict] = {}
            for q in queries:
                try:
                    res = self._summaries_collection.query(
                        query_embeddings=self._to_list(self._embed_queries([q])),
                        n_results=20
                    )
                except Exception as e:
                    logger.warning(f"[BookSearch] summaries query error: {e}")
                    continue
                for i in range(len(res["ids"][0])):
                    cid = res["ids"][0][i]
                    dist = res["distances"][0][i] if "distances" in res else 1.0
                    if cid in seen and seen[cid]["distance"] <= dist:
                        continue
                    meta = res["metadatas"][0][i] if res["metadatas"] else {}
                    seen[cid] = {
                        "text": res["documents"][0][i],
                        "distance": dist,
                        "volume": meta.get("volume", "?"),
                        "volume_name": meta.get("volume_name", "?"),
                        "chapter": meta.get("chapter", "?"),
                        "chapter_num": meta.get("chapter_num", 0),
                        "kind": "summary",
                    }
            candidates = sorted(seen.values(), key=lambda x: x["distance"])[:20]
        if not candidates:
            return []

        self._load_reranker()
        if self._reranker is not None:
            pairs = [(rerank_query, _rerank_excerpt(c["text"]))
                     for c in candidates]
            try:
                scores = self._reranker.predict(pairs, batch_size=32)
                for c, s in zip(candidates, scores):
                    c["rerank_score"] = float(s)
                candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
            except Exception as e:
                logger.warning(f"[BookSearch] summaries rerank failed: {e}")
                for c in candidates:
                    c["rerank_score"] = 1.0 - c["distance"]
        else:
            for c in candidates:
                c["rerank_score"] = 1.0 - c["distance"]

        # Отдаём главы в хронологическом порядке — модель собирает связный рассказ.
        # По одному самому позднему саммари С КАЖДОГО ТОМА — принудительно:
        # состояние персонажа меняется от тома к тому (Ночные Ястребы →
        # Красные Перчатки), а rerank часто выбирает только ранние главы,
        # и ответ описывает персонажа по первым томам, а не актуальным.
        by_vol_latest: Dict[int, Dict] = {}
        for c in candidates:
            v = c.get("volume")
            if isinstance(v, int):
                cur = by_vol_latest.get(v)
                if cur is None or c.get("chapter_num", 0) > cur.get("chapter_num", 0):
                    by_vol_latest[v] = c
        must_nums = {c.get("chapter_num", 0) for c in by_vol_latest.values()}
        top = [c for c in candidates if c.get("chapter_num", 0) in must_nums]
        for c in candidates:
            if len(top) >= n_results:
                break
            cn = c.get("chapter_num", 0)
            if cn not in must_nums:
                must_nums.add(cn)
                top.append(c)
        top.sort(key=lambda x: x.get("chapter_num", 0))
        return top

    def _search_windows(self, queries: List[str], token: str,
                        max_windows: int = 1200) -> List[Dict]:
        """
        Оконный поиск по чанкам с точным токеном (номер артефакта).
        Целый чанк размывает ответ (описание — ~200 символов внутри ~1300),
        поэтому кандидаты — окна ±250 символов вокруг вхождений токена.
        Ранжирование: max cosine по всем вариантам запроса + 0.5 * BM25
        по окнам (дистиллят несёт термины вроде «appearance», которых нет
        в разговорной формулировке запроса) — иначе окно с описанием
        проигрывает тематически близким окнам других артефактов.
        Возвращает до 3 лучших окон как фрагменты (дедуп по чанку).
        """
        if not self._load_bm25():
            return []
        # Окна центрируются на вхождениях токена (±250 символов) — иначе
        # равномерная нарезка берёт окна вообще без номера артефакта,
        # и они выигрывают у окна с описанием за счёт общей близости темы.
        windows = []  # (chunk_idx, meta, window_text)
        for i, doc in enumerate(self._bm25_docs):
            if token not in doc:
                continue
            meta = self._bm25_meta[i] if i < len(self._bm25_meta) else {}
            last_pos = -500
            for m in re.finditer(re.escape(token), doc):
                if m.start() - last_pos < 300:
                    continue  # перекрытие с предыдущим окном
                last_pos = m.start()
                lo = max(0, m.start() - 250)
                windows.append((i, meta, doc[lo:m.start() + 250]))
            if len(windows) > max_windows:
                break
        if not windows:
            return []
        out = self._rank_windows(windows, queries)
        if out:
            logger.info(
                f"[BookSearch] Window search '{token}': "
                f"{[(w['chapter'], round(w['rerank_score'], 2)) for w in out]}"
            )
        return out

    def _rank_windows(self, windows: List[Tuple], queries: List[str],
                      top_n: int = 3) -> List[Dict]:
        """
        Ранжирует готовые окна (chunk_id, meta, text) по
        max cosine по всем запросам + 0.5 * BM25 по окнам (нормализация
        BM25 ПО КАЖДОМУ запросу отдельно — дистиллят дискриминативнее
        разговорной формы). Возвращает до top_n лучших окон (дедуп по chunk_id).
        """
        if not windows:
            return []
        try:
            import numpy as np
            from rank_bm25 import BM25Okapi
            texts = [w[2] for w in windows]
            queries = [q for q in queries if q]
            w_embs = self._embed_docs(texts)
            q_embs = self._embed_queries(queries)
            cos_scores = np.zeros(len(texts))
            for q_emb in q_embs:
                q_n = q_emb / (np.linalg.norm(q_emb) + 1e-9)
                w_n = w_embs / (np.linalg.norm(w_embs, axis=1, keepdims=True) + 1e-9)
                cos_scores = np.maximum(cos_scores, w_n @ q_n)
            bm = BM25Okapi([_tokenize(t) for t in texts])
            bm_norm_max = np.zeros(len(texts))
            for q in queries:
                s = np.array(bm.get_scores(_tokenize(q)))
                if s.max() > 0:
                    bm_norm_max = np.maximum(bm_norm_max, s / s.max())
            final = cos_scores + 0.5 * bm_norm_max
        except Exception as e:
            logger.warning(f"[BookSearch] window ranking failed: {e}")
            return []

        out: List[Dict] = []
        seen = set()
        for idx in final.argsort()[::-1]:
            ci, meta, wtext = windows[idx]
            if ci in seen:
                continue
            seen.add(ci)
            out.append({
                "text": wtext,
                "distance": float(1.0 - cos_scores[idx]),
                "volume": meta.get("volume", "?"),
                "volume_name": meta.get("volume_name", "?"),
                "chapter": meta.get("chapter", "?"),
                "rerank_score": float(final[idx] * 5),
            })
            if len(out) >= top_n:
                break
        return out

    def _search_windows_for_name(self, queries: List[str],
                                 candidates: List[Dict],
                                 max_chunks: int = 100) -> List[Dict]:
        """
        Оконный проход по топ-кандидатам для вопросов про имя/название
        («как назывался?»). Точный ответ — короткая сцена называния внутри
        чанка, которую rerank по целому чанку топит (напр. запись «Utopia»
        на доске в гл. 1328 получала -9.2).
        """
        windows = []
        for pos, c in enumerate(candidates[:max_chunks]):
            doc = c["text"]
            for start in range(0, max(len(doc) - 300, 1), 400):
                windows.append((pos, c, doc[start:start + 500]))
        out = self._rank_windows(windows, queries, top_n=2)
        if out:
            logger.info(
                f"[BookSearch] Name-window search: "
                f"{[(w['chapter'], round(w['rerank_score'], 2)) for w in out]}"
            )
        return out

    def _resolve_chapter_title(self, chapter: str) -> str:
        """
        Резолвит название главы к каноническому виду из метаданных БД.
        summaries.json и метаданные чанков расходятся в написании
        («A Bestowal or A Curse» vs «A Bestowment Or A Curse») — точный
        where по строке такие главы не находил, поэтому матчим по номеру.
        """
        if not hasattr(self, "_chapter_by_num"):
            self._chapter_by_num = {}
            try:
                data = self._collection.get(include=["metadatas"])
                for meta in data["metadatas"]:
                    ch = meta.get("chapter") if isinstance(meta, dict) else None
                    if not ch:
                        continue
                    m = re.match(r"Chapter (\d+):", ch)
                    if m:
                        self._chapter_by_num[m.group(1)] = ch
            except Exception:
                pass
        m = re.match(r"Chapter (\d+):", chapter)
        if m and m.group(1) in self._chapter_by_num:
            return self._chapter_by_num[m.group(1)]
        return chapter

    def get_full_chapter(self, chapter: str) -> Optional[str]:
        """
        Собирает полный текст главы из её чанков (порядок — по числовому
        суффиксу id чанка: lotm_594, lotm_595, ...). Префиксы [SUMMARY: ...]
        срезаются, перекрытия склеиваются (см. _merge_chunks).
        """
        if not self._ensure_connection():
            return None
        chapter = self._resolve_chapter_title(chapter)
        try:
            res = self._collection.get(where={"chapter": chapter},
                                       include=["documents"])
        except Exception as e:
            logger.warning(f"[BookSearch] get_full_chapter error: {e}")
            return None
        if not res["ids"]:
            return None

        def _num(cid: str) -> int:
            m = re.search(r'(\d+)$', cid)
            return int(m.group(1)) if m else 0

        pairs = sorted(zip(res["ids"], res["documents"]), key=lambda p: _num(p[0]))
        chunks = [re.sub(r'^\[SUMMARY:.*?\]\n\n', '', doc, flags=re.DOTALL)
                  for _, doc in pairs]
        return _merge_chunks(chunks)

    def _extract_quotes(self, raw_query: str, candidates: List[Dict],
                        max_chapters: int = 2) -> List[Dict]:
        """
        Для recite-запросов («процитируй», «дословно», «honorific name»):
        берёт топ-max_chapters разных глав по rerank_score, склеивает их
        полный текст и просит основную LLM выписать точный фрагмент дословно.
        Возвращает список псевдофрагментов kind="quote" (обычно 0-2).
        """
        if self._router is None:
            return []
        # Топ-N разных глав (кандидаты уже отсортированы по rerank_score)
        chosen: List[Dict] = []
        seen_ch = set()
        for c in candidates:
            key = (c.get("volume", "?"), c.get("chapter", "?"))
            if key in seen_ch:
                continue
            seen_ch.add(key)
            chosen.append(c)
            if len(chosen) >= max_chapters:
                break

        out: List[Dict] = []
        for c in chosen:
            chapter = c.get("chapter", "?")
            full_text = self.get_full_chapter(chapter)
            if not full_text:
                continue
            prompt = _EXTRACT_QUOTE_PROMPT.format(
                query=raw_query, chapter=chapter,
                text=full_text[:_QUOTE_CHAPTER_MAX_CHARS],
            )
            try:
                quote = self._router.get_response(
                    [{"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=400,
                ).strip()
            except Exception as e:
                logger.warning(f"[BookSearch] quote extraction failed: {e}")
                continue
            if not quote or quote.upper().startswith("NONE") or len(quote) < 20:
                logger.info(f"[BookSearch] quote extraction: no quote in {chapter}")
                continue
            logger.info(
                f"[BookSearch] quote extracted from {chapter}: "
                f"{quote[:80]!r}..."
            )
            out.append({
                "text": (
                    f"[ДОСЛОВНАЯ ЦИТАТА из {chapter} — англ. оригинал; "
                    f"передай её пользователю по-русски, точно по смыслу]\n{quote}"
                ),
                "distance": 0.0,
                "volume": c.get("volume", "?"),
                "volume_name": c.get("volume_name", "?"),
                "chapter": chapter,
                "kind": "quote",
                "rerank_score": 999.0,  # цитата всегда первая в контексте
            })
        return out

    def _search_single(self, query: str, n_results: int,
                       volume: Optional[int]) -> List[Dict]:
        kwargs: Dict = {
            "query_embeddings": self._to_list(self._embed_queries([query])),
            "n_results": n_results,
        }
        if volume is not None:
            kwargs["where"] = {"volume": volume}
        try:
            results = self._collection.query(**kwargs)
        except Exception as e:
            logger.error(f"[BookSearch] Query error: {e}")
            return []

        fragments = []
        for i in range(len(results["ids"][0])):
            frag: Dict = {
                "text": results["documents"][0][i],
                "distance": results["distances"][0][i] if "distances" in results else 0,
                "_chunk_id": results["ids"][0][i],
            }
            if results["metadatas"] and results["metadatas"][0]:
                meta = results["metadatas"][0][i]
                frag["volume"] = meta.get("volume", "?")
                frag["volume_name"] = meta.get("volume_name", "?")
                frag["chapter"] = meta.get("chapter", "?")
            fragments.append(frag)
        return fragments

    def _expand_with_neighbors(self, top: List[Dict], n_results: int,
                               budget: int = 18) -> List[Dict]:
        """Small-to-big: подтягивает соседние чанки глав из выдачи.

        Сцена размазана по соседним чанкам главы, и нужная деталь часто в куске
        рядом с найденным (кейсы qa_eval: делёжка добычи — два чанка после
        последнего найденного; отрицательный ответ гадания — предыдущий).
        Политика:
          - глава с ≥2 фрагментами в топе (сцена уже собирается) →
            два следующих чанка после её последнего фрагмента
            (сцены текут вперёд, продолжение может быть обрывком);
          - глава с 1 фрагментом (изолированный обрывок) → предыдущий и
            следующий чанки этого фрагмента.
        Главы обходим в порядке их лучшего фрагмента, всего не более budget
        добавлений. Id чанков — «prefixN», соседи в главе — N±k (совпадение
        главы проверяем по метаданным).
        """
        if not top or not self._ensure_connection():
            return top

        present = {f.get("_chunk_id") for f in top}
        # главы в порядке их лучшего фрагмента
        chapters: List[tuple] = []
        for f in top:
            key = (f.get("volume"), f.get("chapter"))
            if key not in chapters:
                chapters.append(key)

        before: Dict[str, Dict] = {}
        after: Dict[str, Dict] = {}
        added = 0

        def _fetch_neighbors(anchor: Dict, want_prev: bool, want_next: bool,
                             next_count: int = 1) -> None:
            nonlocal added
            cid = str(anchor.get("_chunk_id") or "")
            m = re.search(r"^(.*?)(\d+)$", cid)
            if not m:
                return
            prefix, num = m.group(1), int(m.group(2))
            ids = []
            if want_prev:
                ids.append(f"{prefix}{num - 1}")
            if want_next:
                ids.extend(f"{prefix}{num + k}" for k in range(1, next_count + 1))
            try:
                res = self._collection.get(ids=ids,
                                           include=["documents", "metadatas"])
            except Exception as e:
                logger.debug(f"[BookSearch] neighbor fetch failed: {e}")
                return
            for nid, doc, meta in zip(res.get("ids") or [],
                                      res.get("documents") or [],
                                      res.get("metadatas") or []):
                if added >= budget:
                    return
                if not nid or nid in present or not doc:
                    continue
                if meta.get("chapter") != anchor.get("chapter") or \
                        meta.get("volume") != anchor.get("volume"):
                    continue
                frag = {
                    "text": doc,
                    "distance": anchor.get("distance", 1.0),
                    "rerank_score": anchor.get("rerank_score", 0.0),
                    "volume": meta.get("volume", "?"),
                    "volume_name": meta.get("volume_name", "?"),
                    "chapter": meta.get("chapter", "?"),
                    "_chunk_id": nid,
                    "_expanded": True,
                }
                n_num = int(re.search(r"(\d+)$", nid).group(1))
                if n_num < num:
                    before.setdefault(cid, []).append(frag)
                    added += 1
                elif n_num > num:
                    after.setdefault(cid, []).append(frag)
                    added += 1

        for key in chapters:
            if added >= budget:
                break
            frags = [f for f in top if (f.get("volume"), f.get("chapter")) == key]
            if len(frags) >= 2:
                # Сцена собирается из разбросанных фрагментов — продолжение
                # за КАЖДЫМ из них (не более 3 на главу): нужная деталь
                # обычно в чанке сразу после найденного.
                per_ch = 0
                for f in frags:
                    if added >= budget or per_ch >= 3:
                        break
                    before_n = added
                    _fetch_neighbors(f, want_prev=False, want_next=True)
                    if added > before_n:
                        per_ch += 1
            else:
                # изолированный обрывок — контекст с обеих сторон
                _fetch_neighbors(frags[0], want_prev=True, want_next=True)

        if added:
            logger.info(f"[BookSearch] Small-to-big: +{added} соседних чанков")

        out: List[Dict] = []
        for f in top:
            cid = str(f.get("_chunk_id") or "")
            out.extend(sorted(before.get(cid, []),
                              key=lambda x: str(x.get("_chunk_id", ""))))
            out.append(f)
            out.extend(sorted(after.get(cid, []),
                              key=lambda x: str(x.get("_chunk_id", ""))))
        # Rescue-чанки (aspect rescue/guarantee) не должны вылетать из-за
        # общего cap — их принудительно добавляли, срез сведёт это на нет.
        capped = out[:n_results + budget]
        capped_ids = {id(f) for f in capped}
        capped.extend(f for f in out[n_results + budget:]
                      if f.get("_rescued") and id(f) not in capped_ids)
        return capped

    def search(self, query: str, n_results: int = 25,
               volume: Optional[int] = None,
               max_per_chapter: int = 3,
               history: Optional[List[str]] = None,
               on_candidates=None) -> List[Dict]:
        """
        Двухэтапный поиск:
          0. Coreference resolution: разрешаем местоимения по истории диалога
          1. Retrieval: гибридный векторный + BM25 → 100 кандидатов
          2. Rerank: cross-encoder оценивает пары (запрос, чанк)
          3. Дедупликация: не более max_per_chapter из одной главы
          4. Возвращаем топ-n_results по rerank_score

        Args:
            history: последние сообщения диалога (строки) — нужны, чтобы
                     разрешать местоимения («он/его» → имя персонажа).
                     None = контекста нет, шаг 0 пропускается.
        """
        if not self._ensure_connection():
            return []

        # --- Шаг 0: coreference resolution на сыром русском запросе ---
        # До перевода/глоссария — пока есть диалоговый контекст в оригинале.
        raw_query = query  # исходный запрос до любых преобразований (для логов)
        coref_resolved = False
        if history:
            resolved = resolve_query_coref(query, history, self._ru_to_en,
                                           router=self._router)
            if resolved != query:
                query = resolved
                coref_resolved = True

        # Полный перевод через Ollama (подстановка имён из глоссария + перевод)
        fully_translated = _translate_full_query(query, self._ru_to_en, self._patterns)

        # Дополнительный вариант перевода от Google Translate: Ollama (gemma3:4b)
        # периодически инвертирует порядок слов («Which city created Klein»),
        # Google — заметно реже. Добавляется просто как ещё один вариант
        # в fan-out, основной перевод не заменяет. Пропускается, если Google
        # недоступен или дал тот же результат.
        google_variant: Optional[str] = None
        if re.search(r'[а-яёА-ЯЁ]', query):
            _gw = _translate_query(query, self._patterns)
            _g = _google_translate(_gw)
            if _g and _g.lower() != fully_translated.lower():
                google_variant = _g
                logger.info(f"[BookSearch] Translate (google variant): '{_gw}' -> '{_g}'")

        # Дистилляция: сжимаем переведённый запрос в компактный поисковый.
        # Добавляется как ДОПОЛНЕНИЕ к вееру запросов, не заменяя existing.
        distilled_query: Optional[str] = None
        distilled_raw: Optional[str] = None
        if DISTILL_ENABLED:
            distilled_raw = _distill_query_via_ollama(fully_translated)
            distilled_query = _clean_distilled(distilled_raw, fully_translated)

        # Авто-определение тома — на переведённом (английском) запросе
        # Мультитомный вопрос («год в первом томе и в 8-м»): ищем по каждому
        # тому отдельно, иначе второй том отрезается фильтром на входе.
        detected_vols = detect_volumes(fully_translated)
        search_volumes: Optional[List[int]] = None
        if volume is None and len(detected_vols) >= 2:
            search_volumes = detected_vols
            logger.info(f"[BookSearch] Multi-volume question: {search_volumes}")
        else:
            detected_vol = detect_volume(fully_translated)
            if detected_vol is not None and volume is None:
                volume = detected_vol
                logger.info(f"[BookSearch] Volume filter: {volume} (from '{fully_translated}')")

        # --- Этап 1: Retrieval (векторный + BM25) ---
        # Используем все варианты запроса для максимального покрытия
        queries = []
        if fully_translated != query:
            queries.append(fully_translated)
        queries.append(query)

        # Distilled query: компактный поисковый запрос без разговорного шума.
        # Добавляется как доп. вариант — recall-страховка, не заменяет existing.
        if distilled_query:
            queries.append(distilled_query)

        # Google-вариант перевода (страховка от инверсий Ollama).
        if google_variant:
            queries.append(google_variant)

        # Концепт-синонимы: «почётное имя»→incantation, «все ангелы»→six angels.
        # Триггеры ищем по обоим вариантам запроса: сырому И после coref —
        # «этого ритуала» превращается в «ритуала усиления удачи» только
        # на резолюции, и без неё концепт «luck enhancement ritual» теряется.
        _concept_src = raw_query if raw_query == query else f"{raw_query}\n{query}"
        concept_q = _expand_concepts(_concept_src, fully_translated)
        if concept_q:
            queries.append(concept_q)
            logger.info(f"[BookSearch] [rewrite] concept_exp : {concept_q!r}")

        # Pathway expansion: если в запросе упомянут путь — добавляем названия
        # его последовательностей как отдельный поисковый запрос.
        # Если упомянута последовательность — добавляем название пути.
        pathway_extra = _expand_query_with_pathway(
            fully_translated, self._pathway_to_seqs, self._seq_to_pathway
        )
        if pathway_extra:
            queries.append(pathway_extra)

        # Seq-number expansion: «перешёл на 4 последовательность» + персонаж
        # из справочника → название последовательности («Bizarro Sorcerer»).
        seqnum_extra = _expand_seq_number(
            raw_query, fully_translated, self._char_pathways,
            self._seq_by_num, self._ru_to_en,
        )
        seqnum_precise = seqnum_extra is not None
        if seqnum_extra is None:
            # Персонаж вне справочника — дизъюнктивное расширение по всем
            # путям; имя персонажа якорит нужное название при ранжировании.
            n = _detect_seq_number(raw_query, fully_translated)
            if n is not None:
                seqnum_extra = self._expand_seqnum_disjunctive(n, raw_query)
        if seqnum_extra:
            queries.append(seqnum_extra)

        # Alias expansion: «Клейн» в начале книги — это Чжоу Минжуй;
        # без варианта с алиасом rerank не связывает их и топит правильную главу.
        alias_queries = _expand_with_aliases(fully_translated)
        if alias_queries:
            queries.extend(alias_queries)

        # Аспектный сплит: многоаспектный вопрос («как добил И ЧТО досталось») —
        # один запрос покрывает лишь доминирующий аспект, чанки второго тонут.
        # Подзапросы идут в fan-out, в BM25, в rerank (max) и в rescue-слоты.
        aspect_queries: List[str] = []
        if DISTILL_ENABLED:
            aspect_queries = _split_aspects_via_ollama(fully_translated)
            if aspect_queries:
                queries.extend(aspect_queries)

        # --- Сводка перефразирования: наглядная цепочка преобразований запроса ---
        # Показывает весь путь: оригинал → coref → перевод → дистилляция → pathway → fan-out.
        logger.info(f"[BookSearch] ===== REWRITE =====")
        logger.info(f"[BookSearch] [rewrite] original    : {raw_query!r}")
        if coref_resolved:
            logger.info(f"[BookSearch] [rewrite] coref       : {query!r}")
        if fully_translated != query:
            logger.info(f"[BookSearch] [rewrite] translated  : {fully_translated!r}")
        else:
            logger.info(f"[BookSearch] [rewrite] translated  : (unchanged)")
        if DISTILL_ENABLED:
            logger.info(
                f"[BookSearch] [rewrite] distilled    : raw={distilled_raw!r} -> "
                f"cleaned={distilled_query!r} (used={bool(distilled_query)})"
            )
        if pathway_extra:
            logger.info(f"[BookSearch] [rewrite] pathway_exp : {pathway_extra!r}")
        if seqnum_extra:
            logger.info(f"[BookSearch] [rewrite] seqnum_exp  : {seqnum_extra!r}")
        if alias_queries:
            logger.info(f"[BookSearch] [rewrite] alias_exp   : {alias_queries}")
        if aspect_queries:
            logger.info(f"[BookSearch] [rewrite] aspect_exp  : {aspect_queries}")
        logger.info(f"[BookSearch] [rewrite] fan-out ({len(queries)}): {queries}")

        # --- Обзорный режим: широкий вопрос про сущность → саммари глав ---
        # Chunk-поиск на таких вопросах возвращает случайные обрывки, и модель
        # домысливает ответ из собственных (ошибочных) знаний. Саммари глав
        # дают связное покрытие темы. Только без явного фильтра тома.
        if volume is None and _is_overview_query(raw_query, fully_translated,
                                                 distilled_query, self._patterns):
            overview_queries = list(queries)
            # Детерминированный entity-запрос: срезаем вопросную фразу и
            # переводим имена regex-глоссарием — страховка на случай, когда
            # Ollama/Google исказили перевод и сущность потерялась.
            entity_ru = _OVERVIEW_RU_PAT.sub("", raw_query).strip(" ?.!,")
            entity_q = _translate_query(entity_ru, self._patterns).strip()
            if entity_q and entity_q.lower() not in {q.lower() for q in overview_queries}:
                overview_queries.append(entity_q)
            # Термины глоссария в entity-части — кандидаты для литерального
            # substring-матча по саммари (длинные термины первыми, они точнее).
            glossary_hits = _find_glossary_entries(entity_ru, self._ru_to_en)
            entity_terms = sorted(set(glossary_hits.values()),
                                  key=len, reverse=True)
            # Reranker англоязычный: entity-запрос без кириллицы или перевод.
            entity_lit = re.sub(r'\b[а-яёА-ЯЁ]+\b', ' ', entity_q)
            entity_lit = re.sub(r'\s+', ' ', entity_lit).strip(' ?.,!-')
            rerank_q = entity_lit if entity_lit else fully_translated
            overview = self._search_summaries(
                overview_queries, rerank_q, entity_terms=entity_terms,
                n_results=min(n_results, 10),
            )
            if overview:
                logger.info(
                    f"[BookSearch] Overview mode: '{query}' -> "
                    f"{len(overview)} chapter summaries (terms={entity_terms!r})"
                )
                for i, frag in enumerate(overview, 1):
                    logger.info(
                        f"  [{i}] Vol.{frag.get('volume', '?')} "
                        f"{frag.get('volume_name', '?')} / {frag.get('chapter', '?')} "
                        f"(d={frag.get('distance', 0):.3f}, "
                        f"rerank={frag.get('rerank_score', 0):.3f})"
                    )
                return overview
            logger.info("[BookSearch] Overview mode: no summaries, fallback to chunks")

        # per_query > n_candidates намеренно: Chroma HNSW — приближённый поиск,
        # ef растёт вместе с n_results. При per_query=n_candidates=100 (ef=100)
        # ANN теряет настоящих близких соседей (кейс: чанк с dist=0.154 не
        # попадал в выдачу вовсе). 3x запас почти не стоит времени (мс),
        # а candidate recall заметно выше.
        per_query = max(self._n_candidates * 3, 100)
        seen_ids: set = set()
        vector_results: List[Dict] = []
        # Позиции каждого чанка в каждом варианте запроса — для RRF-слияния
        rank_lists: Dict[str, List[int]] = {}

        for vol_q in (search_volumes if search_volumes else [volume]):
            for q in queries:
                for rank, frag in enumerate(self._search_single(q, per_query, vol_q), 1):
                    cid = frag.get("_chunk_id", frag["text"][:40])
                    rank_lists.setdefault(cid, []).append(rank)
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        vector_results.append(frag)

        # Нормализуем векторные скоры
        if vector_results:
            min_dist = min(f.get("distance", 1.0) for f in vector_results)
            max_dist = max(f.get("distance", 0.0) for f in vector_results)
            dist_range = max(max_dist - min_dist, 0.001)
            for f in vector_results:
                dist = f.get("distance", 1.0)
                f["_vector_score"] = 1.0 - (dist - min_dist) / dist_range
        else:
            min_dist = 1.0

        # BM25 — по каждому английскому варианту запроса (перевод + дистиллят),
        # слияние по max. Дистиллят несёт термины, которых нет в переводе
        # («appearance» против «look like») — без него точные главы
        # отрезались до rerank'а.
        bm25_queries: List[str] = []
        _bq = fully_translated if fully_translated != query else query
        bm25_queries.append(_bq)
        if distilled_query and distilled_query.lower() not in {q.lower() for q in bm25_queries}:
            bm25_queries.append(distilled_query)
        # Концепт-запрос несёт книжную лексику («transmigrate», «incantation») —
        # именно она матчится текстом глав, поэтому добавляем его и в BM25.
        if concept_q and concept_q.lower() not in {q.lower() for q in bm25_queries}:
            bm25_queries.append(concept_q)
        # Аспектные подзапросы — лексика второстепенных аспектов («spoils, loot»),
        # которой нет в основной формулировке.
        for aq in aspect_queries:
            if aq.lower() not in {q.lower() for q in bm25_queries}:
                bm25_queries.append(aq)
        bm25_by_id: Dict[str, Dict] = {}
        for vol_q in (search_volumes if search_volumes else [volume]):
            for bq in bm25_queries:
                for rank, f in enumerate(self._bm25_search(bq, top_k=200, volume=vol_q), 1):
                    cid = f.get("_chunk_id", f["text"][:40])
                    rank_lists.setdefault(cid, []).append(rank)
                    if cid not in bm25_by_id or f["bm25_score"] > bm25_by_id[cid]["bm25_score"]:
                        bm25_by_id[cid] = f
        bm25_results = list(bm25_by_id.values())

        if bm25_results:
            max_bm25 = max(f.get("bm25_score", 0.0) for f in bm25_results)
            if max_bm25 > 0:
                for f in bm25_results:
                    f["_bm25_score_norm"] = f.get("bm25_score", 0.0) / max_bm25
            else:
                for f in bm25_results:
                    f["_bm25_score_norm"] = 0.0

        # Гибридное объединение
        all_ids: Dict[str, Dict] = {}
        for f in vector_results:
            cid = f.pop("_chunk_id", f["text"][:40])
            all_ids[cid] = {
                "text": f["text"],
                "distance": f.get("distance", 1.0),
                "volume": f.get("volume", "?"),
                "volume_name": f.get("volume_name", "?"),
                "chapter": f.get("chapter", "?"),
                "_vector_score": f.get("_vector_score", 0.0),
                "_bm25_score_norm": 0.0,
                "bm25_score": 0.0,
            }
        for f in bm25_results:
            cid = f.pop("_chunk_id", f["text"][:40])
            if cid in all_ids:
                all_ids[cid]["_bm25_score_norm"] = f.get("_bm25_score_norm", 0.0)
                all_ids[cid]["bm25_score"] = f.get("bm25_score", 0.0)
            else:
                all_ids[cid] = {
                    "text": f["text"],
                    "distance": 1.0,
                    "volume": f.get("volume", "?"),
                    "volume_name": f.get("volume_name", "?"),
                    "chapter": f.get("chapter", "?"),
                    "_vector_score": 0.0,
                    "_bm25_score_norm": f.get("_bm25_score_norm", 0.0),
                    "bm25_score": f.get("bm25_score", 0.0),
                }

        # Фильтр по дистанции + RRF-слияние (Reciprocal Rank Fusion).
        # Раньше: alpha*vector_score + (1-alpha)*bm25_norm — у чанка,
        # отсутствующего в векторной выдаче, vector_score=0 обнулял половину
        # скора, и аспектные чанки с отличным BM25 («трофеи», rank 3 по
        # concept-запросу) тонули в топ-100. RRF: вклад каждого варианта
        # запроса = 1/(k+rank); отсутствие в варианте не штрафуется, и
        # «высоко в одном источнике» честно конкурирует с «средне во всех».
        RRF_K = 60
        candidates = []
        for cid, data in all_ids.items():
            data["_chunk_id"] = cid
            if data["distance"] <= self._max_distance or data["_bm25_score_norm"] > 0.3:
                data["hybrid_score"] = sum(
                    1.0 / (RRF_K + r) for r in rank_lists.get(cid, [])
                )
                candidates.append(data)

        if not candidates:
            logger.info(f"[BookSearch] '{query}' -> 0 candidates")
            return []

        # Сортируем по гибридному скору и берём топ-k для rerank
        candidates.sort(key=lambda x: x["hybrid_score"], reverse=True)
        pool = candidates
        candidates = pool[:self._n_candidates]

        # Аспектный rescue: топ-2 попадания каждого подзапроса гарантированно
        # доходят до rerank. Иначе чанки второстепенного аспекта («трофеи»)
        # отрезаются топ-100 доминирующего («бой»). Смотрим ОБЕ ветки
        # (вектор и BM25) и concept_q — именно он несёт книжную лексику
        # аспекта («spoils» вместо переводного «trophies»).
        rescue_queries = list(aspect_queries)
        if concept_q and concept_q not in rescue_queries:
            rescue_queries.append(concept_q)
        if rescue_queries:
            have = {c.get("_chunk_id") for c in candidates}
            by_id = {c.get("_chunk_id"): c for c in pool}
            for aq in rescue_queries:
                # до 2 из каждой ветки: вектор и BM25 покрывают разные
                # типы несовпадения, давать вектору съедать оба слота нельзя
                for source in (self._search_single(aq, 20, volume),
                               self._bm25_search(aq, top_k=20, volume=volume)):
                    rescued = 0
                    for frag in source:
                        if rescued >= 2:
                            break
                        cid = frag.get("_chunk_id")
                        if not cid or cid in have:
                            continue
                        if cid in by_id:
                            rescued_frag = dict(by_id[cid])
                            rescued_frag["_rescued"] = True
                            candidates.append(rescued_frag)
                            have.add(cid)
                            rescued += 1
                    if rescued:
                        logger.info(f"[BookSearch] aspect rescue: +{rescued} for '{aq[:50]}'")

        # --- Этап 2: Rerank через cross-encoder ---
        self._load_reranker()
        if self._reranker is not None:
            # Готовим пары (query, doc) для reranker
            # Используем полностью переведённый запрос — cross-encoder англоязычный
            rerank_query = fully_translated if fully_translated != query else query
            # Концепт-запрос содержит перевод + книжную лексику («transmigrate»,
            # «incantation»): cross-encoder ранжирует по пересечению терминов,
            # и без неё правильная глава тонет (её слов нет в формулировке юзера).
            if concept_q:
                rerank_query = concept_q
            # Страховка: если запрос для rerank остался с кириллицей (перевод
            # откатился на смешанный RU/EN), а google-вариант чисто английский —
            # rerank по нему: cross-encoder англоязычный, кириллица в запросе
            # обнуляет скоры правильных глав.
            if re.search(r'[а-яёА-ЯЁ]', rerank_query) and google_variant \
                    and not re.search(r'[а-яёА-ЯЁ]', google_variant):
                suffix = concept_q[len(fully_translated):] if concept_q else ""
                rerank_query = google_variant + suffix
                logger.info(f"[BookSearch] rerank via google variant: {rerank_query!r}")
            # Seq-number expansion (точное, из справочника): без названия
            # последовательности в rerank-запросе cross-encoder давит нужные
            # главы (они не содержат номера). Дизъюнктивное не добавляем —
            # 21 название размывает запрос.
            if seqnum_extra and seqnum_precise:
                rerank_query = f"{rerank_query} {seqnum_extra}"
            pairs = [(rerank_query, _rerank_excerpt(c["text"]))
                     for c in candidates]
            try:
                rerank_scores = list(self._reranker.predict(pairs, batch_size=32))
                # Дистиллят как дополнительный rerank-запрос (max по скорам):
                # составные вопросы («какую карту..., и чем прерывается...»)
                # cross-encoder'у трудны в полной формулировке, а сжатый
                # запрос без разговорного шума ранжирует их заметно точнее.
                if distilled_query and distilled_query.lower() != rerank_query.lower():
                    import numpy as np
                    dq_pairs = [(distilled_query, _rerank_excerpt(c["text"]))
                                for c in candidates]
                    dq_scores = self._reranker.predict(dq_pairs, batch_size=32)
                    rerank_scores = np.maximum(rerank_scores, dq_scores).tolist()
                # Алиасы: rerank берёт max по основному запросу и вариантам
                # («Клейн» ↔ «Zhou Mingrui»), иначе правильная глава с
                # алиасом получает отрицательный скор и тонет.
                if alias_queries:
                    import numpy as np
                    for aq in alias_queries:
                        aq_pairs = [(aq, _rerank_excerpt(c["text"]))
                                    for c in candidates]
                        aq_scores = self._reranker.predict(aq_pairs, batch_size=32)
                        rerank_scores = np.maximum(rerank_scores, aq_scores).tolist()
                # Аспекты: rerank берёт max по основному запросу и подзапросам —
                # чанк второстепенного аспекта («трофеи») получает свой шанс
                # даже если основная формулировка его не покрывает.
                if aspect_queries:
                    import numpy as np
                    for aq in aspect_queries:
                        aq_pairs = [(aq, _rerank_excerpt(c["text"]))
                                    for c in candidates]
                        aq_scores = self._reranker.predict(aq_pairs, batch_size=32)
                        rerank_scores = np.maximum(rerank_scores, aq_scores).tolist()
                for i, score in enumerate(rerank_scores):
                    candidates[i]["rerank_score"] = float(score)
                # Сортируем по rerank_score
                candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
                logger.info(
                    f"[BookSearch] '{query}' reranked {len(candidates)} candidates"
                )
            except Exception as e:
                logger.warning(f"[BookSearch] Rerank failed: {e}")
                # Fallback: оставляем гибридную сортировку
                for c in candidates:
                    c["rerank_score"] = c["hybrid_score"]
        else:
            for c in candidates:
                c["rerank_score"] = c["hybrid_score"]

        # Отладочный хук: отдать полный список кандидатов после rerank
        # (для qa_eval-диагностики; в проде не используется).
        if on_candidates is not None:
            try:
                on_candidates(list(candidates))
            except Exception:
                pass

        # --- Recency-фильтр: «последняя/финальная» → только самый поздний том ---
        # Иначе фрагменты разных томов (напр. битвы Клейна и Амона в Vol.6 и
        # Vol.8) попадают в контекст вместе, и модель склеивает два разных
        # события в одно. Не применяется при явном фильтре тома.
        if volume is None and not search_volumes and detect_recency(raw_query, fully_translated):
            vol_counts: Dict[int, int] = {}
            for c in candidates:
                v = c.get("volume")
                if isinstance(v, int):
                    vol_counts[v] = vol_counts.get(v, 0) + 1
            if vol_counts:
                target = max(vol_counts)
                if vol_counts[target] >= RECENCY_MIN_CANDIDATES:
                    before = len(candidates)
                    candidates = [c for c in candidates
                                  if c.get("volume") == target]
                    logger.info(
                        f"[BookSearch] Recency filter: kept vol {target} "
                        f"({before} -> {len(candidates)} candidates)"
                    )
                else:
                    logger.info(
                        f"[BookSearch] Recency filter skipped: vol {target} "
                        f"has only {vol_counts[target]} candidates"
                    )

        # Дедупликация: не более max_per_chapter фрагментов из одной главы.
        # Исключение: для главы топ-1 кандидата лимит +2 — сцена часто
        # размазана по 4-5 подряд идущим чанкам одной главы (карта «Шут» и
        # прерывание гадания в гл. 5), и жёсткий лимит обрезал её хвост.
        hot_key = None
        if candidates:
            hot_key = (f"{candidates[0].get('volume', '?')}_"
                       f"{candidates[0].get('chapter', '?')}")
        chapter_counts: Dict[str, int] = {}
        top: List[Dict] = []
        kept = 0
        for c in candidates:
            ch_key = f"{c.get('volume', '?')}_{c.get('chapter', '?')}"
            limit = max_per_chapter + 2 if ch_key == hot_key else max_per_chapter
            # Аспектные rescue-чанки не считаем ни в лимит главы, ни в
            # n_results: их и так мало (≤2 на аспект), а отрезание сводит
            # rescue на нет (кейс qa_eval: чанк делёжки добычи был бы 4-м
            # из своей главы и 101-м в общем списке).
            rescued = bool(c.get("_rescued"))
            if not rescued and chapter_counts.get(ch_key, 0) >= limit:
                continue
            chapter_counts[ch_key] = chapter_counts.get(ch_key, 0) + 1
            top.append(c)
            if not rescued:
                kept += 1
            if kept >= n_results:
                break

        # Rescue-чанки, оставшиеся за срезом по kept (rerank поставил их ниже
        # 25-го обычного), добираем принудительно — но только из глав, уже
        # присутствующих в выдаче. Якорь на сцену отсекает keyword-шум из
        # нерелевантных томов, который rescue набрал по лексике аспекта.
        in_top = {id(t) for t in top}
        top_chapters = {(t.get("volume"), t.get("chapter")) for t in top}
        for c in candidates:
            if (c.get("_rescued") and id(c) not in in_top
                    and (c.get("volume"), c.get("chapter")) in top_chapters):
                top.append(c)

        # Гарантия томов при мультитомном вопросе: у каждого упомянутого тома
        # должно быть ≥2 фрагментов в выдаче — иначе вторая половина вопроса
        # («а какой год был в 8-м?») остаётся без контекста.
        if search_volumes:
            in_top_ids = {t.get("_chunk_id") for t in top}
            for v in search_volumes:
                if sum(1 for t in top if t.get("volume") == v) >= 2:
                    continue
                added_v = 0
                for c in candidates:  # отсортированы по rerank desc
                    if added_v >= 2:
                        break
                    cid = c.get("_chunk_id")
                    if c.get("volume") == v and cid not in in_top_ids:
                        frag = dict(c)
                        frag["_rescued"] = True
                        top.append(frag)
                        in_top_ids.add(cid)
                        added_v += 1
                if added_v:
                    logger.info(f"[BookSearch] multi-volume guarantee: +{added_v} for vol {v}")

        # Гарантия аспекта в финале: если ни один чанк аспектного подзапроса
        # не дошёл до top (проиграл rerank/лимит главы доминирующему аспекту),
        # добираем лучший по rerank чанк этого аспекта — но только из глав,
        # УЖЕ присутствующих в выдаче. Якорь на сцену отсекает keyword-шум
        # (сцены делёжки добычи из других томов, которые BM25 любит не меньше).
        if rescue_queries:
            in_top_ids = {t.get("_chunk_id") for t in top}
            top_chapters = {(t.get("volume"), t.get("chapter")) for t in top}
            for aq in rescue_queries:
                hit_ids = {f.get("_chunk_id")
                           for f in self._bm25_search(aq, top_k=10, volume=volume)}
                hit_ids |= {f.get("_chunk_id")
                            for f in self._search_single(aq, 10, volume)}
                if hit_ids & in_top_ids:
                    continue  # аспект уже представлен в выдаче
                for c in candidates:  # отсортированы по rerank desc
                    cid = c.get("_chunk_id")
                    if (cid in hit_ids and cid not in in_top_ids
                            and (c.get("volume"), c.get("chapter")) in top_chapters):
                        frag = dict(c)
                        frag["_rescued"] = True
                        top.append(frag)
                        in_top_ids.add(cid)
                        logger.info(f"[BookSearch] aspect guarantee: +1 for '{aq[:50]}'")
                        break

        skipped = len(candidates) - len(top)
        if skipped > 0:
            logger.info(f"[BookSearch] Dedup: skipped {skipped} frags (max {max_per_chapter}/chapter)")

        # Оконное расширение (small-to-big): сцена часто размазана по соседним
        # чанкам одной главы (кейс из qa_eval: делёжка добычи шла сразу после
        # чанка боя, но в топ не попала — модель достроила вывод неверно).
        top = self._expand_with_neighbors(top, n_results)

        # Оконный поиск по точному номеру артефакта («2-049», «0-08»):
        # описание предмета — маленькое окно внутри большого чанка,
        # обычный retrieval (и rerank) его размывает. Окна ставим первыми.
        m_art = _ARTIFACT_TOKEN_RE.search(raw_query)
        if m_art:
            _win_queries = [fully_translated]
            if distilled_query:
                _win_queries.append(distilled_query)
            win = self._search_windows(_win_queries, m_art.group(0))
            if win:
                win_keys = {(w["volume"], w["chapter"]) for w in win}
                top = win + [f for f in top
                             if (f.get("volume", "?"), f.get("chapter", "?")) not in win_keys]
                top = top[:n_results]
        elif _asks_name(raw_query, fully_translated) or \
                _asks_recite(raw_query, fully_translated):
            # Вопрос про имя/название: два прохода —
            # 1) окна по BM25-пулу (сцена называния внутри чанка);
            # 2) саммари глав — ответ часто записан там прямо
            #    («creates the town of Utopia» в саммари гл. 1328).
            _name_queries = [fully_translated]
            if distilled_query:
                _name_queries.append(distilled_query)
            win = self._search_windows_for_name(_name_queries,
                                                list(bm25_by_id.values()))
            sum_frags = self._summaries_by_distance(_name_queries, n_results=3)
            win = (win or []) + sum_frags
            if win:
                win_keys = {(w["volume"], w["chapter"]) for w in win}
                top = win + [f for f in top
                             if (f.get("volume", "?"), f.get("chapter", "?")) not in win_keys]
                top = top[:n_results]

        # Дословная цитата (recite-запросы): топ-2 главы целиком уходят в LLM,
        # она выписывает точный фрагмент. Цитата ставится первым фрагментом,
        # её глава из обычных чанков убирается (её роль играет цитата).
        if _asks_recite(raw_query, fully_translated) and top:
            quote_frags = self._extract_quotes(raw_query, candidates)
            if quote_frags:
                q_keys = {(q["volume"], q["chapter"]) for q in quote_frags}
                top = quote_frags + [
                    f for f in top
                    if (f.get("volume", "?"), f.get("chapter", "?")) not in q_keys
                ]
                top = top[:n_results]

        logger.info(
            f"[BookSearch] '{query}' -> {len(top)} frags "
            f"(candidates={len(all_ids)}, fusion=rrf)"
        )
        for i, frag in enumerate(top, 1):
            vol = frag.get("volume", "?")
            vol_name = frag.get("volume_name", "?")
            ch = frag.get("chapter", "?")
            dist = frag.get("distance", 0)
            bm25 = frag.get("bm25_score", 0)
            hybrid = frag.get("hybrid_score", 0)
            rerank = frag.get("rerank_score", 0)
            preview = frag["text"][:60].replace("\n", " ")
            logger.info(
                f"  [{i}] Vol.{vol} {vol_name} / {ch} "
                f"(d={dist:.3f}, bm25={bm25:.1f}, hybrid={hybrid:.3f}, rerank={rerank:.3f}): {preview}..."
            )

        # Чистим внутренние поля
        for frag in top:
            frag.pop("_vector_score", None)
            frag.pop("_bm25_score_norm", None)

        return top

    def translate_query(self, query: str) -> str:
        """Возвращает полностью переведённый (английский) вариант запроса."""
        return _translate_full_query(query, self._ru_to_en, self._patterns)

    def format_fragments(self, fragments: List[Dict],
                         max_chars_per_fragment: int = 800) -> str:
        """Форматирует фрагменты в текстовый блок для промпта."""
        if not fragments:
            return ""
        lines = []
        for frag in fragments:
            vol = frag.get("volume", "?")
            vol_name = frag.get("volume_name", "?")
            chapter = frag.get("chapter", "?")
            text = frag["text"][:max_chars_per_fragment]
            lines.append(f"[Vol.{vol} {vol_name} / {chapter}]\n{text}")
        return "\n\n".join(lines)

    def get_stats(self) -> Dict:
        if not self._ensure_connection():
            return {"error": "not connected"}
        return {
            "collection": self._collection_name,
            "total_chunks": self._collection.count(),
            "db_path": self._db_path,
            "max_distance": self._max_distance,
            "patterns": len(self._patterns),
        }

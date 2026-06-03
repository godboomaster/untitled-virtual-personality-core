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
    """Простая токенизация для BM25: слова в нижнем регистре."""
    return re.findall(r'[a-zA-Z]+', text.lower())


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

    return mapping


def _build_patterns(ru_to_en: Dict[str, str]) -> List[Tuple[str, str]]:
    """
    Строит список (regex_pattern, en_replacement) для трансляции запросов.
    Обрабатывает русские падежи через стем-матчинг: берём первые N символов
    как стем и разрешаем любое русское окончание.
    """
    RU_SUFFIX = r'[а-яёА-ЯЁ]*'
    patterns: List[Tuple[str, str]] = []

    for ru, en in sorted(ru_to_en.items(), key=lambda x: -len(x[0])):
        words = ru.split()
        if len(words) >= 2:
            # Многословное имя: каждое слово → стем + суффикс
            word_pats = []
            for w in words:
                stem_len = max(4, len(w) - 2)
                stem = re.escape(w[:stem_len])
                word_pats.append(stem + RU_SUFFIX)
            pattern = r'\s+'.join(word_pats)
        elif len(ru) >= 5:
            # Длинное однословное: стем + суффикс
            stem = re.escape(ru[:max(4, len(ru) - 2)])
            pattern = stem + RU_SUFFIX
        elif len(ru) >= 3:
            # Короткое русское слово (3-4 символа): стем + суффикс для падежей
            # "Шут" → "Шут[а-яёА-ЯЁ]*" — поймает "Шута", "Шуту" и т.д.
            stem_len = max(3, len(ru) - 1)
            stem = re.escape(ru[:stem_len])
            pattern = stem + RU_SUFFIX
        else:
            # Очень короткое: точное совпадение
            pattern = r'\b' + re.escape(ru) + r'\b'

        patterns.append((pattern, en))

    return patterns


def _translate_query(query: str,
                     patterns: List[Tuple[str, str]]) -> str:
    """
    Заменяет русские имена/термины в запросе на английские.
    Паттерны отсортированы по длине (длинные первыми).
    """
    result = query
    for pattern, en in patterns:
        result = re.sub(pattern, en, result, flags=re.IGNORECASE)
    return result


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


def _translate_via_ollama(query: str) -> Optional[str]:
    """Перевод запроса через локальную Ollama (qwen2.5:3b). ~0.3-0.5с."""
    global _ollama_available
    if _ollama_available is False:
        return None  # Уже проверяли, не работает
    try:
        import requests
        resp = requests.post("http://localhost:11434/api/generate", json={
            "model": "qwen2.5:3b",
            "prompt": (
                "Translate Russian to English. Context: Lord of the Mysteries novel. "
                "Beyonder pathways with sequences. "
                "Russian 'путь' = 'Pathway' (e.g. 'какого пути' = 'which pathway'). "
                "Russian 'последовательность' = 'Sequence'. "
                "Output ONLY the translation, nothing else.\n\n"
                f"{query}"
            ),
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 80},
        }, timeout=5)
        text = resp.json()["response"].strip()
        # Если модель вернула русский — что-то пошло не так
        if re.search(r'[а-яёА-ЯЁ]', text):
            return None
        _ollama_available = True
        return text
    except Exception:
        _ollama_available = False
        return None


def _build_pathway_map(glossary_path: str) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """
    Парсит глоссарий → маппинги:
      pathway_name → [seq_name_1, seq_name_2, ...]
      seq_name → pathway_name
    Pathway names: 'Red Priest', 'Twilight Giant', 'Fool' и т.д.
    Sequence names: 'Conqueror', 'Iron-Blood Knight', 'Solar High Priest' и т.д.
    """
    pathway_to_seqs: Dict[str, List[str]] = {}
    seq_to_pathway: Dict[str, str] = {}
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
                # Конец секции путей — общий раздел
                if raw.startswith("ОБЩИЕ") or "ОБЩИЕ ТЕРМИНЫ" in raw:
                    current_pathway = None
                    continue
                if not raw or raw.startswith("#"):
                    continue
                if "=" not in raw:
                    continue
                en_part, _, _ = raw.partition("=")
                en_name = en_part.strip()
                # Убираем "Sequence N - " префикс
                m2 = re.match(r'^Sequence\s+\d+\s*-\s*(.+)$', en_name)
                if m2:
                    seq_name = m2.group(1).strip()
                    if seq_name and seq_name != current_pathway:
                        pathway_to_seqs[current_pathway].append(seq_name)
                        seq_to_pathway[seq_name] = current_pathway
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
    return pathway_to_seqs, seq_to_pathway


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

    # Обратный поиск: sequence name → pathway (без условия "Pathway" в запросе)
    for seq, path in seq_to_pathway.items():
        s_lower = seq.lower()
        # Короткие/общие — только как отдельное слово
        if len(s_lower) <= 4:
            if re.search(r'\b' + re.escape(s_lower) + r'\b', q_lower):
                extra_terms.append(path)
        else:
            if s_lower in q_lower:
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


def _translate_full_query(query: str,
                          patterns: List[Tuple[str, str]]) -> str:
    """
    Полный перевод русского запроса на английский для cross-encoder.
    Приоритет: Ollama (LLM) -> словарь -> исходный запрос.
    """
    # Если в запросе нет русского — нечего переводить
    if not re.search(r'[а-яёА-ЯЁ]', query):
        return query

    # Сначала заменяем имена через глоссарий (Сасрир → Sasrir, Арродес → Arrodes)
    name_translated = _translate_query(query, patterns)
    if name_translated != query:
        logger.info(f"[BookSearch] Names: '{query}' -> '{name_translated}'")

    # Если в запросе нет русского — имена заменили, больше нечего делать
    if not re.search(r'[а-яёА-ЯЁ]', name_translated):
        return name_translated

    # Пробуем Ollama на запросе с уже английскими именами
    llm = _translate_via_ollama(name_translated)
    if llm:
        # Постобработка pathway-названий к каноническому виду "X Pathway"
        # "path/pathway of [the] X" → "X Pathway"
        # "X's path[way]" → "X Pathway"
        # "Pathway X" → "X Pathway"  (Ollama часто ставит Pathway перед именем)
        llm = re.sub(r'\bpath(?:way)?\s+of\s+(?:the\s+)?([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)\b', r'\1 Pathway', llm, flags=re.IGNORECASE)
        llm = re.sub(r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)'s\s+(?:path|pathway)\b", r'\1 Pathway', llm, flags=re.IGNORECASE)
        llm = re.sub(r'\bPathway\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)\b', r'\1 Pathway', llm)
        logger.info(f"[BookSearch] Ollama: '{name_translated}' -> '{llm}'")
        return llm

    # Fallback: словарная подстановка
    logger.info(f"[BookSearch] Ollama unavailable, using dictionary fallback")
    result = name_translated
    for ru, en in _RU_EN_QUERY_MAP:
        result = re.sub(re.escape(ru), en, result, flags=re.IGNORECASE)
    result = re.sub(r'\b[а-яёА-ЯЁ]+\b', '', result)
    result = re.sub(r'\s+', ' ', result).strip()
    return result


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


class BookSearch:
    """Поиск релевантных фрагментов книги: гибрид векторный + BM25 + cross-encoder rerank."""

    def __init__(self,
                 context: str = "arrodes",
                 collection_name: str = "lord_of_mysteries",
                 model_name: str = "paraphrase-multilingual-MiniLM-L12-v2",
                 max_distance: float = 0.50,
                 alpha: float = 0.6,
                 rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
                 n_candidates: int = 100):
        """
        Args:
            max_distance: порог дистанции (0.45 строго, 0.50 умеренно, 0.55 мягко).
            alpha: вес векторного скора в гибридном поиске (0=только BM25, 1=только вектор).
            rerank_model: cross-encoder для reranking (None = выключить).
            n_candidates: сколько кандидатов собирать перед rerank.
        """
        self._db_path = f"data/{context}/book"
        self._collection_name = collection_name
        self._model_name = model_name
        self._client = None
        self._collection = None
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
        self._patterns = _build_patterns(ru_to_en)
        self._pathway_to_seqs, self._seq_to_pathway = _build_pathway_map(str(glossary_path))
        if ru_to_en:
            logger.info(
                f"[BookSearch] Loaded {len(ru_to_en)} RU->EN mappings, "
                f"{len(self._patterns)} patterns, "
                f"{len(self._pathway_to_seqs)} pathways, "
                f"{len(self._seq_to_pathway)} sequences"
            )

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
            embedder = SentenceTransformerEmbeddingFunction(
                model_name=self._model_name
            )
            self._collection = self._client.get_collection(
                self._collection_name,
                embedding_function=embedder
            )
            return True
        except Exception as e:
            logger.error(f"[BookSearch] Connection error: {e}")
            return False

    def _search_single(self, query: str, n_results: int,
                       volume: Optional[int]) -> List[Dict]:
        kwargs: Dict = {"query_texts": [query], "n_results": n_results}
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

    def search(self, query: str, n_results: int = 25,
               volume: Optional[int] = None,
               max_per_chapter: int = 3) -> List[Dict]:
        """
        Двухэтапный поиск:
          1. Retrieval: гибридный векторный + BM25 → 100 кандидатов
          2. Rerank: cross-encoder оценивает пары (запрос, чанк)
          3. Дедупликация: не более max_per_chapter из одной главы
          4. Возвращаем топ-n_results по rerank_score
        """
        if not self._ensure_connection():
            return []

        # Транслируем запрос (имена) и полностью (для reranker)
        translated = _translate_query(query, self._patterns)
        fully_translated = _translate_full_query(query, self._patterns)
        if translated != query:
            logger.info(f"[BookSearch] Translated: '{query}' -> '{translated}'")
        if fully_translated != translated:
            logger.info(f"[BookSearch] Full translate: '{query}' -> '{fully_translated}'")

        # Авто-определение тома — на переведённом (английском) запросе
        detected_vol = detect_volume(fully_translated)
        if detected_vol is not None and volume is None:
            volume = detected_vol
            logger.info(f"[BookSearch] Volume filter: {volume} (from '{fully_translated}')")

        # --- Этап 1: Retrieval (векторный + BM25) ---
        # Используем все варианты запроса для максимального покрытия
        queries = []
        if fully_translated != query:
            queries.append(fully_translated)
        if translated != query and translated != fully_translated:
            queries.append(translated)
        queries.append(query)

        # Pathway expansion: если в запросе упомянут путь — добавляем названия
        # его последовательностей как отдельный поисковый запрос.
        # Если упомянута последовательность — добавляем название пути.
        pathway_extra = _expand_query_with_pathway(
            fully_translated, self._pathway_to_seqs, self._seq_to_pathway
        )
        if pathway_extra:
            queries.append(pathway_extra)
            logger.info(f"[BookSearch] Pathway query added: '{pathway_extra[:80]}...'")

        per_query = max(self._n_candidates, 10)
        seen_ids: set = set()
        vector_results: List[Dict] = []

        for q in queries:
            for frag in self._search_single(q, per_query, volume):
                cid = frag.get("_chunk_id", frag["text"][:40])
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

        # BM25 — используем полностью переведённый запрос для лучшего match'а
        bm25_query = fully_translated if fully_translated != query else query
        bm25_results = self._bm25_search(bm25_query, top_k=200, volume=volume)

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

        # Фильтр по дистанции
        candidates = []
        for cid, data in all_ids.items():
            if data["distance"] <= self._max_distance or data["_bm25_score_norm"] > 0.3:
                data["hybrid_score"] = (
                    self._alpha * data["_vector_score"] +
                    (1 - self._alpha) * data["_bm25_score_norm"]
                )
                candidates.append(data)

        if not candidates:
            logger.info(f"[BookSearch] '{query}' -> 0 candidates")
            return []

        # Сортируем по гибридному скору и берём топ-k для rerank
        candidates.sort(key=lambda x: x["hybrid_score"], reverse=True)
        candidates = candidates[:self._n_candidates]

        # --- Этап 2: Rerank через cross-encoder ---
        self._load_reranker()
        if self._reranker is not None:
            # Готовим пары (query, doc) для reranker
            # Используем полностью переведённый запрос — cross-encoder англоязычный
            rerank_query = fully_translated if fully_translated != query else query
            pairs = [(rerank_query, c["text"][:400]) for c in candidates]
            try:
                rerank_scores = self._reranker.predict(pairs, batch_size=32)
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

        # Дедупликация: не более max_per_chapter фрагментов из одной главы
        chapter_counts: Dict[str, int] = {}
        top: List[Dict] = []
        for c in candidates:
            ch_key = f"{c.get('volume', '?')}_{c.get('chapter', '?')}"
            if chapter_counts.get(ch_key, 0) >= max_per_chapter:
                continue
            chapter_counts[ch_key] = chapter_counts.get(ch_key, 0) + 1
            top.append(c)
            if len(top) >= n_results:
                break

        skipped = len(candidates) - len(top)
        if skipped > 0:
            logger.info(f"[BookSearch] Dedup: skipped {skipped} frags (max {max_per_chapter}/chapter)")

        logger.info(
            f"[BookSearch] '{query}' -> {len(top)} frags "
            f"(candidates={len(all_ids)}, alpha={self._alpha})"
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
        return _translate_full_query(query, self._patterns)

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

"""Динамическая (retrieval-based) подгрузка глоссария Арродеса в промпт.

Раньше весь глоссарий (~1180 строк, ~17k токенов) добавлялся в системный
промпт каждого сообщения. Теперь в промпт идут только записи, релевантные
вопросу: совпавшие словарные термины и секции путей для упомянутых
путей/последовательностей/персонажей со справочным путём.

Использование:
    from app.features.glossary_context import build_glossary_block
    block = build_glossary_block([user_input, translated_query])
    book_context = (block + "\n\n" + book_context) if block else book_context
"""
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.features.book_search import (
    _load_ru_to_en,
    _find_glossary_entries,
    _build_pathway_map,
    _build_character_pathways,
)

logger = logging.getLogger(__name__)

GLOSSARY_PATH = Path(__file__).parent.parent / "personas" / "arrodes_glossary.yaml"
# Статичное ядро самых частых терминов книги (считается офлайн по всем
# чанкам): ~150 имён ≈ 1.5 КБ. Всегда в промпте — страховка от дрейфа
# нейминга для терминов, которых нет ни в вопросе, ни во фрагментах
# (модель иначе переводит их «по памяти»: «Антигона» вместо Антигонус).
CORE_PATH = Path(__file__).parent.parent / "personas" / "arrodes_glossary_core.json"
# Таймлайн последовательностей персонажей по томам (канон) — лечит класс
# ошибок «не та ступень персонажа» (Одри названа Гипнотистом вместо
# Психиатра: модель не знала, на каком томе вопрос).
TIMELINE_PATH = Path(__file__).parent.parent / "personas" / "arrodes_seq_timeline.yaml"
# Принадлежность персонажей к организациям по томам (канон) — кейс:
# бот считал Леонарда вечным Ночным Ястребом, а он после т.1 — Красные Перчатки.
AFFILIATIONS_PATH = Path(__file__).parent.parent / "personas" / "arrodes_affiliations.yaml"

# Лимиты блока: ~50 словарных записей + до 2 секций путей ≈ 4-5k символов
MAX_DICT_ENTRIES = 50
MAX_PATHWAY_SECTIONS = 2

_cache: Optional[Tuple[List[Tuple[str, List[Tuple[str, str]]]],
                       List[Tuple[str, List[Tuple[str, str]]]],
                       Dict[str, str],
                       Dict[str, str],
                       Dict[str, str],
                       Dict[str, List[str]]]] = None


def _parse_glossary():
    """Парсит глоссарий. Возвращает кортеж:
    (dict_sections, pathway_sections, ru_to_en, seq_to_pathway,
     char_pathways, pathway_to_seqs)

    dict_sections/pathway_sections: [(header, [(en, ru), ...])]
    """
    global _cache
    if _cache is not None:
        return _cache

    dict_sections: List[Tuple[str, List[Tuple[str, str]]]] = []
    pathway_sections: List[Tuple[str, List[Tuple[str, str]]]] = []
    notes: List[str] = []  # справочные заметки «Имя → пояснение» из хвоста файла
    current_header: Optional[str] = None
    current_entries: List[Tuple[str, str]] = []

    def _flush():
        if current_header is None:
            return
        target = pathway_sections if current_header.startswith("ПУТЬ ") else dict_sections
        target.append((current_header, current_entries))

    with open(GLOSSARY_PATH, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            if line.endswith(":") and "=" not in line:
                _flush()
                current_header, current_entries = line, []
            elif "=" in line:
                en, _, ru = line.partition("=")
                current_entries.append((en.strip(), ru.strip()))
            elif "→" in line:
                notes.append(line.strip())
    _flush()

    ru_to_en = _load_ru_to_en(str(GLOSSARY_PATH))
    pathway_map = _build_pathway_map(str(GLOSSARY_PATH))
    pathway_to_seqs, seq_to_pathway = pathway_map[0], pathway_map[1]
    char_pathways = _build_character_pathways(str(GLOSSARY_PATH))

    _cache = (dict_sections, pathway_sections, notes, ru_to_en,
              seq_to_pathway, char_pathways, pathway_to_seqs)
    return _cache


def _load_core_terms() -> List[str]:
    """Статичное ядро частых терминов (json, считается офлайн). Кэш."""
    global _core_terms
    if _core_terms is None:
        try:
            import json
            _core_terms = json.load(open(CORE_PATH, encoding="utf-8"))
        except Exception:
            _core_terms = []
    return _core_terms


_core_terms: Optional[List[str]] = None
_timeline: Optional[List[Dict]] = None
_affiliations: Optional[Dict] = None


def _load_timeline() -> List[Dict]:
    """Таймлайн последовательностей персонажей (yaml). Кэш."""
    global _timeline
    if _timeline is None:
        try:
            import yaml
            data = yaml.safe_load(open(TIMELINE_PATH, encoding="utf-8"))
            _timeline = (data or {}).get("characters", [])
        except Exception:
            _timeline = []
    return _timeline


def _load_affiliations() -> Dict:
    """Принадлежность к организациям по томам (yaml). Кэш."""
    global _affiliations
    if _affiliations is None:
        try:
            import yaml
            data = yaml.safe_load(open(AFFILIATIONS_PATH, encoding="utf-8"))
            _affiliations = data or {}
        except Exception:
            _affiliations = {}
    return _affiliations


def _match_character(names: List[str], joined_lower: str) -> Optional[str]:
    """Первое имя персонажа, встретившееся в тексте (границы слов)."""
    for n in names:
        if len(str(n)) >= 3 and re.search(r"\b" + re.escape(str(n).lower()) + r"\b",
                                          joined_lower):
            return str(n)
    return None


def build_glossary_block(texts: List[str],
                         fragments: Optional[List[Dict]] = None,
                         max_entries: int = MAX_DICT_ENTRIES,
                         max_fragment_entries: int = 30,
                         max_pathways: int = MAX_PATHWAY_SECTIONS) -> str:
    """Собирает релевантный блок глоссария для текстов вопроса.

    Args:
        texts: варианты текста вопроса (оригинал RU, перевод EN, ...).
        fragments: найденные фрагменты книги — термины из них тоже включаем
            (до max_fragment_entries): имена, которых нет в вопросе, но есть
            в контексте, иначе модель переводит их не по словарю
            («Антигона» вместо Антигонус, «Секретный Орден» вместо Тайного).
        max_entries: потолок словарных записей из вопроса.
        max_fragment_entries: потолок записей из фрагментов.
        max_pathways: потолок секций путей.

    Returns:
        Отформатированный блок (словарь + секции путей) или "" если
        совпадений нет — тогда в промпт не добавляем ничего.
    """
    (dict_sections, pathway_sections, notes, ru_to_en,
     seq_to_pathway, char_pathways, pathway_to_seqs) = _parse_glossary()

    joined = "\n".join(t for t in texts if t)
    if not joined.strip():
        return ""
    joined_lower = joined.lower()

    # --- 1. Совпавшие словарные термины (RU-сторона через _find_glossary_entries,
    #         EN-сторона прямым матчем с границами слов) ---
    matched_ru: Dict[str, str] = {}
    for t in texts:
        if t and re.search(r"[а-яёА-ЯЁ]", t):
            matched_ru.update(_find_glossary_entries(t, ru_to_en, limit=max_entries))
    matched_en_terms = set(matched_ru.values())
    en_to_ru = {en: ru for ru, en in ru_to_en.items()}
    for en, ru in en_to_ru.items():
        if len(en) < 3:
            continue
        if re.search(r"\b" + re.escape(en.lower()) + r"\b", joined_lower):
            matched_en_terms.add(en)
            matched_ru.setdefault(ru, en)

    # Термины из найденных фрагментов: имена, которых нет в вопросе, но
    # которые модель будет переводить в ответе. Без них уходит нейминг
    # («Антигона» вместо Антигонус). Только словарные термины с матчем,
    # до max_fragment_entries (сначала многословные — они точнее).
    fragment_terms: List[str] = []
    if fragments:
        frag_text = " ".join(f.get("text", "") for f in fragments)[:60000].lower()
        candidates = sorted(
            (en for en in en_to_ru if len(en) >= 4
             and re.search(r"\b" + re.escape(en.lower()) + r"\b", frag_text)),
            key=lambda e: -len(e),
        )
        for en in candidates:
            if len(fragment_terms) >= max_fragment_entries:
                break
            if en not in matched_en_terms:
                fragment_terms.append(en)

    # --- 2. Словарные секции с совпадениями ---
    out_sections: List[Tuple[str, List[Tuple[str, str]]]] = []
    total = 0
    for header, entries in dict_sections:
        hits = [(en, ru) for en, ru in entries
                if en in matched_en_terms or ru in matched_ru]
        if hits and total < max_entries:
            hits = hits[:max_entries - total]
            out_sections.append((header, hits))
            total += len(hits)

    # Термины из фрагментов — отдельной секцией (это имена, которые модель
    # будет переводить в ответе, даже если их не было в вопросе)
    if fragment_terms:
        frag_hits = [(en, en_to_ru[en]) for en in fragment_terms]
        out_sections.append(("ИМЕНА ИЗ КОНТЕКСТА:", frag_hits))

    # Статичное ядро частых терминов книги — всегда, если блок вообще
    # формируется (иначе при отсутствии совпадений в вопросе модель
    # остаётся без базового словаря)
    core_terms = _load_core_terms()
    if core_terms:
        core_hits = [(en, en_to_ru[en]) for en in core_terms if en in en_to_ru]
        if core_hits:
            out_sections.append(("ОСНОВНЫЕ ИМЕНА:", core_hits))

    # --- 3. Секции путей: упоминание пути/последовательности/персонажа ---
    wanted_pathways: List[str] = []  # EN-имена путей

    def _add_pathway(en_pathway: str):
        if en_pathway and en_pathway not in wanted_pathways:
            wanted_pathways.append(en_pathway)

    # (a) имя последовательности в тексте → её путь
    for seq, pathway in seq_to_pathway.items():
        if re.search(r"\b" + re.escape(seq.lower()) + r"\b", joined_lower):
            _add_pathway(pathway)
    # (b) RU-имя пути («путь Демонессы», «путь Шута») в тексте
    for header, entries in pathway_sections:
        m = re.match(r"^ПУТЬ\s+(.+?)\s*\((.+?)\s+Pathway\)\s*:", header)
        if not m:
            continue
        ru_name, en_name = m.group(1).strip().lower(), m.group(2).strip()
        if f"путь {ru_name}" in joined_lower or \
                re.search(r"\b" + re.escape(en_name.lower()) + r"\b", joined_lower):
            _add_pathway(en_name)
    # (c) персонаж со справочным путём упомянут → его путь
    for char, pathway in char_pathways.items():
        if re.search(r"\b" + re.escape(char.lower()) + r"\b", joined_lower):
            _add_pathway(pathway)

    out_pathways: List[Tuple[str, List[Tuple[str, str]]]] = []
    for header, entries in pathway_sections:
        if len(out_pathways) >= max_pathways:
            break
        m = re.match(r"^ПУТЬ\s+.+?\s*\((.+?)\s+Pathway\)\s*:", header)
        if m and m.group(1).strip() in wanted_pathways:
            out_pathways.append((header, entries))

    # Справочные заметки «Имя → пояснение»: включаем те, чей подтекст
    # упомянут в вопросе (по имени до стрелки), до 8 штук.
    matched_notes: List[str] = []
    for note in notes:
        if len(matched_notes) >= 8:
            break
        name = note.split("→", 1)[0].strip().lower()
        if len(name) >= 3 and name in joined_lower:
            matched_notes.append(note)

    # Таймлайн последовательностей: для упомянутых в вопросе персонажей
    # даём их уровень по томам; том(а) из фрагментов подсвечиваем —
    # иначе модель путает ступени из разных периодов (кейс: Одри названа
    # Гипнотистом вместо Психиатра).
    timeline_lines: List[str] = []
    # Основной том сцены подсвечиваем только при ЯВНОМ доминировании одного
    # тома в топ-5 (иначе аннотация сама становится источником ошибки:
    # топ-1 реранка может быть соседней сценой из другого тома).
    top_vols = [f.get("volume") for f in (fragments or [])[:5]
                if isinstance(f.get("volume"), int)]
    main_vol = None
    if top_vols:
        cand = max(set(top_vols), key=top_vols.count)
        if top_vols.count(cand) >= 3:
            main_vol = cand

    def _display_name(names):
        return next((str(n) for n in names if re.search(r"[а-яёА-ЯЁ]", str(n))
                     and " " in str(n)), str(names[0]))

    def _timeline_parts(tl):
        return "; ".join(f"т{vol} — {lvl}" for vol, lvl in
                         sorted(tl.items(), key=lambda x: int(x[0])))

    for char in _load_timeline():
        if not _match_character(char.get("names", []), joined_lower):
            continue
        line = (f"  {_display_name(char.get('names', []))} "
                f"(путь {char.get('pathway', '?')}): "
                + _timeline_parts(char.get("timeline", {})))
        current = char.get("timeline", {}).get(str(main_vol)) if main_vol else None
        if current:
            line += f"  → основной том фрагментов: {main_vol}, актуальная ступень: {current}"
        timeline_lines.append(line)
        if len(timeline_lines) >= 5:
            break

    # Принадлежность к организациям по томам (для упомянутых персонажей)
    affiliation_lines: List[str] = []
    aff = _load_affiliations()
    for char in aff.get("characters", []):
        if not _match_character(char.get("names", []), joined_lower):
            continue
        affiliation_lines.append(
            f"  {_display_name(char.get('names', []))}: "
            + _timeline_parts(char.get("timeline", {})))
        if len(affiliation_lines) >= 5:
            break

    if not out_sections and not out_pathways and not matched_notes \
            and not timeline_lines and not affiliation_lines:
        return ""

    # --- 4. Форматирование (как старый _load_glossary, чтобы правила
    #         промпта про СПРАВОЧНИК продолжали работать) ---
    lines = [
        "",
        "══════════════════════",
        " СЛОВАРЬ И СПРАВОЧНИК — только релевантное к вопросу",
        " (если нужного персонажа или пути здесь нет — опирайся на фрагменты)",
        "══════════════════════",
    ]
    for header, hits in out_sections:
        lines.append(f"\n{header}")
        for en, ru in hits:
            lines.append(f"  {en} = {ru}")
    for header, entries in out_pathways:
        lines.append(f"\n{header}")
        for en, ru in entries:
            lines.append(f"  {en} = {ru}")
    if matched_notes:
        lines.append("\nСПРАВОЧНЫЕ ЗАМЕТКИ (ИСТИНА):")
        for note in matched_notes:
            lines.append(f"  {note}")
    if timeline_lines:
        lines.append("\nТАЙМЛАЙН ПОСЛЕДОВАТЕЛЬНОСТЕЙ (канон, по томам):")
        lines.extend(timeline_lines)
        lines.append("  Если вопрос о повышении/способностях без указания периода, "
                     "а во фрагментах подходят несколько томов — перечисли "
                     "ступени по томам или уточни, о каком спрашивают, "
                     "не выбирай молча одну.")
    if affiliation_lines:
        lines.append("\nПРИНАДЛЕЖНОСТЬ К ОРГАНИЗАЦИЯМ (по томам — "
                     "название организации называй по тому спрошенного события):")
        lines.extend(affiliation_lines)

    block = "\n".join(lines)
    logger.info(
        f"[Glossary] dynamic: {total} dict entries, "
        f"{len(out_pathways)} pathway sections, {len(block)} chars"
    )
    return block

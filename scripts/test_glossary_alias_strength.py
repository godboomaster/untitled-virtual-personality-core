"""Тест силы однословных авто-алиасов глоссария (weak vs strong).

«Митчелл» — транслитерация фамилии (Mitchell): сильный book-сигнал,
сам поднимает chat_only -> mixed. «Башня»/«Бабочка» — переводы
общеупотребимых слов (Tower, Jodeson-эпитет): остаются слабыми.

Запуск: python -m scripts.test_glossary_alias_strength
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.features.book_search import (  # noqa: E402
    _find_glossary_entries,
    _load_ru_to_en,
    get_suffix_alias_keys,
    get_weak_single_aliases,
)


def main() -> int:
    glossary = str(Path(__file__).parent.parent / "app" / "personas"
                   / "arrodes_glossary.yaml")
    ru_to_en = _load_ru_to_en(glossary)
    weak = get_weak_single_aliases()

    failures = []

    # 1. Матчинг голой фамилии и имени
    m_mitchell = _find_glossary_entries("митчелл", ru_to_en)
    m_leonard = _find_glossary_entries("леонард", ru_to_en)
    if m_mitchell.get("Митчелл") != "Leonard Mitchell":
        failures.append(f"митчелл: ожидался алиас Митчелл -> Leonard "
                        f"Mitchell, получено {m_mitchell}")
    if m_leonard.get("Леонард Митчелл") != "Leonard Mitchell":
        failures.append(f"леонард: ожидалась полная запись Леонард Митчелл, "
                        f"получено {m_leonard}")

    # 2. Сила сигнала: имена — сильные, переводы — слабые
    if "Митчелл" in weak:
        failures.append("Митчелл не должен быть слабым алиасом (это фамилия)")
    for common_noun in ("Башня", "Бабочка"):
        if common_noun not in weak:
            failures.append(f"{common_noun} должен быть слабым алиасом "
                            f"(перевод общеупотребимого слова)")

    # 3. Симуляция override из classify_intent: chat_only + сильный
    #    словарный термин -> mixed
    def strong_terms(query: str) -> dict:
        matched = _find_glossary_entries(query, ru_to_en)
        return {ru: en for ru, en in matched.items() if ru not in weak}

    for query in ("митчелл", "леонард", "что известно о митчелле?"):
        if not strong_terms(query):
            failures.append(f"«{query}»: нет сильных терминов — "
                            f"override chat_only -> mixed не сработает")

    # 4. Обычная фраза без book-терминов не должна получать сильный сигнал
    for query in ("привет, как дела?", "посоветуй фильм"):
        if strong_terms(query):
            failures.append(f"«{query}»: ложные сильные термины "
                            f"{strong_terms(query)}")

    # Для eyeball-контроля: все однословные алиасы с разметкой силы
    single = sorted(k for k in get_suffix_alias_keys()
                    if len(k.split()) == 1)
    print(f"Однословных авто-алиасов: {len(single)}, из них слабых: "
          f"{len(weak)}")
    print("Сильные (имена/фамилии):")
    for k in single:
        if k not in weak:
            print(f"  {k} -> {ru_to_en[k]}")
    print("Слабые (переводы):")
    for k in sorted(weak):
        print(f"  {k} -> {ru_to_en[k]}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

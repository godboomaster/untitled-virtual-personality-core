"""
book_context.py — формирование блока [КОНТЕКСТ] для промпта с сигналом качества поиска.

Использование:
    from book_search import BookSearch
    from book_context import build_context_block

    frags = book_search.search(user_query)
    context_block = build_context_block(frags)
    # → вставить context_block в системный промпт перед вопросом пользователя
"""

from typing import List, Dict
import re


def build_context_block(fragments: List[Dict],
                        original_query: str = "",
                        translated_query: str = "",
                        max_chars_per_fragment: int = 1000,
                        high_quality_rerank: float = 2.0,
                        low_quality_rerank: float = 0.0,
                        high_quality_distance: float = 0.38,
                        low_quality_distance: float = 0.46,
                        mode: str = "book") -> str:
    """
    Формирует блок [КОНТЕКСТ] с сигналом качества для LLM.

    Качество определяется по rerank_score (если есть) — это лучший сигнал,
    чем raw cosine distance. Если rerank_score отсутствует, fallback на distance.

    Сигнал качества помогает модели понять степень уверенности, но НЕ заставляет
    отказываться от ответа — модель должна пытаться отвечать по фрагментам.

    Пороги rerank_score (cross-encoder/ms-marco-MiniLM-L-6-v2):
        > 2.0 — высокое качество, фрагмент очень релевантен
        0.0–2.0 — среднее качество
        < 0.0 — низкое качество, фрагменты слабо связаны с вопросом

    Пороги дистанций (fallback, ChromaDB cosine):
        < 0.38 — высокое качество
        0.38–0.46 — среднее качество
        > 0.46 — низкое качество
    """
    mode_tag = "BOOK" if mode == "book" else "MIXED"
    # Формируем блок вопросов
    query_block = ""
    if original_query or translated_query:
        query_block = "ВОПРОС ПОЛЬЗОВАТЕЛЯ:\n"
        if original_query:
            query_block += f"  [RU] {original_query}\n"
        if translated_query and translated_query != original_query:
            query_block += f"  [EN] {translated_query}\n"
        query_block += f"{'─' * 60}\n"

    if not fragments:
        return (
            f"[КОНТЕКСТ:{mode_tag} — ПУСТО]\n"
            f"{query_block}"
            "Поиск не нашёл релевантных фрагментов.\n"
            "ИНСТРУКЦИЯ: Признайся в незнании В ХАРАКТЕРЕ персонажа. "
            "Не используй фразу «В хрониках нет сведений». "
            "Задай собеседнику встречный вопрос."
        )

    # Определяем качество: предпочитаем rerank_score, fallback на distance
    best_rerank = min(f.get("rerank_score", 999) for f in fragments if "rerank_score" in f)
    has_rerank = any("rerank_score" in f for f in fragments)
    best_dist = min(f.get("distance", 1.0) for f in fragments)

    if has_rerank and best_rerank != 999:
        if best_rerank >= high_quality_rerank:
            quality_label = "ВЫСОКОЕ"
            quality_note = "Фрагменты хорошо отвечают на вопрос — отвечай уверенно."
        elif best_rerank >= low_quality_rerank:
            quality_label = "СРЕДНЕЕ"
            quality_note = (
                "Фрагменты связаны с вопросом. "
                "Отвечай по содержимому фрагментов, не додумывай."
            )
        else:
            quality_label = "НИЗКОЕ"
            quality_note = (
                "Фрагменты слабо связаны с вопросом. "
                "Расскажи то немногое что удалось найти — начни с осторожной "
                "оговорки в своём стиле. "
                "Если в фрагментах действительно ничего полезного — честно признайся "
                "В ХАРАКТЕРЕ персонажа и переведи тему вопросом собеседнику. "
                "НЕ используй фразу «В хрониках нет сведений»."
            )
        signal = f"лучший rerank = {best_rerank:.2f}"
    else:
        # Fallback на distance
        if best_dist < high_quality_distance:
            quality_label = "ВЫСОКОЕ"
            quality_note = "Фрагменты хорошо отвечают на вопрос — отвечай уверенно."
        elif best_dist < low_quality_distance:
            quality_label = "СРЕДНЕЕ"
            quality_note = (
                "Фрагменты связаны с вопросом. "
                "Отвечай по содержимому фрагментов, не додумывай."
            )
        else:
            quality_label = "НИЗКОЕ"
            quality_note = (
                "Фрагменты слабо связаны с вопросом. "
                "Расскажи то немногое что удалось найти — начни с осторожной "
                "оговорки в своём стиле. "
                "Если в фрагментах действительно ничего полезного — честно признайся "
                "В ХАРАКТЕРЕ персонажа и переведи тему вопросом собеседнику. "
                "НЕ используй фразу «В хрониках нет сведений»."
            )
        signal = f"лучшая дистанция = {best_dist:.3f}"

    header = (
        f"[КОНТЕКСТ:{mode_tag} — качество поиска: {quality_label} ({signal})]\n"
        f"{quality_note}\n"
        f"{'─' * 60}"
    )

    # Фрагменты из разных томов = из разных периодов истории. Предупреждаем,
    # иначе модель переносит события/состав поздних томов на ранние сцены
    # (кейс qa_eval: «Мистер Мир» на первом собрании Форс).
    vols = sorted({f.get("volume") for f in fragments
                   if isinstance(f.get("volume"), int)})
    if len(vols) > 1:
        vol_list = ", ".join(str(v) for v in vols)
        header += (
            f"\nВНИМАНИЕ: фрагменты из РАЗНЫХ томов ({vol_list}) — это разные "
            "периоды истории. Не переноси события и состояние мира одного "
            "периода на другой."
        )

    # Обзорный режим: фрагменты — это саммари глав, а не сырые чанки.
    # Свой заголовок и инструкция (собрать цельный обзор, не домысливать).
    if fragments and all(f.get("kind") == "summary" for f in fragments):
        header = (
            f"[КОНТЕКСТ:{mode_tag} — ОБЗОР ПО САММАРИ ГЛАВ]\n"
            "Ниже — краткие пересказы глав книги, релевантные вопросу, "
            "в хронологическом порядке. Собери из них цельный обзор по существу "
            "вопроса. Для вопросов «кто такой/что такое» описывай сущность по её "
            "САМОМУ ПОЗДНЕМУ состоянию (последние пересказы: актуальная организация, "
            "последовательность, роль), а более ранние подавай как прошлое "
            "(«раньше был…, позже…»). Не выдумывай фактов, которых нет в пересказах.\n"
            f"{'─' * 60}"
        )

    lines = [header]
    if query_block:
        lines.append(query_block)

    # Фрагменты нумеруем (Ф1..ФN): модель обязана помечать каждую фактическую
    # фразу ответа скрытым маркером [ФN] — номер фрагмента-источника. Маркеры
    # срезаются кодом перед отправкой; фразы со ссылкой на несуществующий
    # номер удаляются (это сигнал выдумки).
    lines.append(
        "Каждую фразу с фактами из фрагментов завершай скрытым маркером "
        "[ФN] — номером фрагмента-источника (например, [Ф2]). Маркер "
        "скрыт от пользователя. Не помечай строку о взаимности и свой "
        "встречный вопрос. Не ставь маркер на фразы без фактов из фрагментов."
    )

    # Префикс [SUMMARY: ...] оставляем на первом фрагменте каждой главы:
    # саммари несёт ключевые факты главы (и курируется вручную), а раньше
    # он срезался на ВСЕХ чанках — модель видела только тела и теряла
    # главное (напр. четыре хлеба ритуала удачи). На повторных чанках той
    # же главы префикс по-прежнему срезается, чтобы не дублировать.
    seen_chapters = set()
    for idx, frag in enumerate(fragments, 1):
        vol = frag.get("volume", "?")
        vol_name = frag.get("volume_name", "?")
        chapter = frag.get("chapter", "?")
        full = frag["text"]
        ch_key = (vol, chapter)
        m = re.match(r'^\[SUMMARY:.*?\]\n\n', full, flags=re.DOTALL)
        if m and ch_key not in seen_chapters:
            seen_chapters.add(ch_key)
            # Полный префикс + тело в пределах бюджета (префикс не съедает тело)
            text = m.group(0) + full[m.end():m.end() + max_chars_per_fragment]
        else:
            # Убираем SUMMARY префикс из текста для LLM
            text = re.sub(r'^\[SUMMARY:.*?\]\n\n', '', full, flags=re.DOTALL)
            text = text[:max_chars_per_fragment]
        lines.append(f"\n[Ф{idx}] [Vol.{vol} {vol_name} / {chapter}]\n{text}")

    return "\n".join(lines)

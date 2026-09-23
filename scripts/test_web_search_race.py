"""Тест политики гонки поиска (app/features/web_search_race.py) на подставных
источниках: AI Mode в приоритете до дедлайна, DDG — подстраховка.

Запуск: PYTHONPATH=. python3 scripts/test_web_search_race.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


DDG = [{"title": "ddg", "body": "сниппет", "href": "https://x"}]


def leg(delay, value):
    def fn(_q):
        time.sleep(delay)
        if isinstance(value, Exception):
            raise value
        return value
    return fn


def run(ai_delay, ai_val, ddg_delay, ddg_val, deadline=0.6, total=1.2):
    from app.features.web_search_race import race_search
    t = time.monotonic()
    res = race_search("q", context="test", ai_ask=leg(ai_delay, ai_val),
                      ddg_search=leg(ddg_delay, ddg_val),
                      ai_deadline=deadline, total_timeout=total)
    src = res[0].get("_source", "ddg") if res else None
    return src, time.monotonic() - t


def main():
    print("\n── web_search_race: политика ──")
    src, dt = run(0.2, "сводка", 0.05, DDG)
    check("DDG быстрее, AI до дедлайна — берём AI", src == "google_ai")
    check("  и не ждём дедлайна", dt < 0.4)

    src, dt = run(0.05, "сводка", 0.3, DDG)
    check("AI первым — берём AI сразу, DDG не ждём", src == "google_ai" and dt < 0.2)

    src, dt = run(0.05, None, 0.3, DDG)
    check("AI отказал — берём DDG, как только готов", src == "ddg" and dt < 0.45)

    src, dt = run(0.05, RuntimeError("boom"), 0.1, DDG)
    check("AI упал исключением — DDG", src == "ddg")

    src, dt = run(2.0, "поздно", 0.1, DDG)
    check("AI не успел к дедлайну — DDG на дедлайне", src == "ddg" and 0.55 < dt < 0.8)

    src, dt = run(0.9, "сводка", 2.0, DDG)
    check("после дедлайна DDG ещё нет — берём первого (AI)", src == "google_ai" and dt < 1.05)

    src, dt = run(0.9, "сводка", 0.05, [])
    check("DDG пусто — ждём AI и после дедлайна", src == "google_ai")

    src, dt = run(3.0, None, 3.0, DDG)
    check("оба не успели к общему потолку — пусто", src is None and dt < 1.35)

    src, dt = run(0.05, None, 0.05, [])
    check("оба пусто — пусто", src is None)

    from app.features.web_search_race import ai_result, AI_MAX_CHARS
    r = ai_result("слово " * 2000)
    check("сводка AI обрезается до лимита", len(r["full_text"]) <= AI_MAX_CHARS + 4)

    print("\n── web_llm google: чистка служебного UI из ответа ──")
    from app.features import web_llm as W
    llm = W.WebChatLLM.__new__(W.WebChatLLM)
    llm.adapter = W.ADAPTERS["google"]
    got = llm._cut_tail("Столица — **Париж**.\n\nSomething went wrong.\n\n"
                        "Map data ©2026 GoogleTerms\n\nExpand map\n\nZoom in\n\n"
                        "Zoom out\n\nГород стоит на Сене.")
    check("виджет карты вырезан, текст вокруг цел",
          got == "Столица — **Париж**.\n\nГород стоит на Сене.")
    check("тост копирования и подвал отрезаны",
          llm._cut_tail("Курс 84,07.\n\nСкопировано в буфер обменаНе удалось "
                        "скопировать\n\nПоделиться") == "Курс 84,07.")
    check("английский тост — тоже",
          llm._cut_tail("Paris.\n\nCopied to clipboardFailed to copy") == "Paris.")
    plain = "Обычный ответ. Something went wrong — часть текста."
    check("обычный текст не трогается", llm._cut_tail(plain) == plain)

    print("\n── web_llm claude: метки инструментов в начале ответа ──")
    cl = W.WebChatLLM.__new__(W.WebChatLLM)
    cl.adapter = W.ADAPTERS["claude"]
    for raw, want in [
        ("Updated memorycodewords.md\n\nПринято.", "Принято."),
        ("Recalled memorycodewords.md\n\nДа.", "Да."),
        ("Read and updated memory\n\nDeleted.", "Deleted."),
        ("PonderingPonderingДа", "Да"),
        ("Pondering\n\nОтвет.", "Ответ."),
        ("Pondering over options is fine.", "Pondering over options is fine."),
        ("Обычный ответ про memory.", "Обычный ответ про memory."),
    ]:
        check(f"claude: {raw[:30]!r}", cl._cut_tail(raw) == want)

    print(f"\nИтого: {ok - failures}/{ok} OK")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

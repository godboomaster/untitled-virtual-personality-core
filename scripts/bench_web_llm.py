"""
Бенчмарк скорости веб-чатов LLM (провайдер webchat).

Каждому сайту последовательно отправляется 7 сообщений: 2 системных
(инструкция через role=system + реплика — как реальные вызовы классификаторов
бота: «ответь одним словом»), 2 лёгких (обычная живая речь), 2 средних
(короткий рассказ по вопросу), 1 сложное (структура + рассуждение).
Замеряется время ответа на каждое. Первая отправка на сайте помечается
«cold» — в неё входят запуск браузера и открытие чата.

Запуск:
    python -m scripts.bench_web_llm                  # все сайты из WEBCHAT_SITES
    python -m scripts.bench_web_llm qwen deepseek    # только перечисленные
    python -m scripts.bench_web_llm --fast           # только лёгкие сообщения
    python -m scripts.bench_web_llm --allow-headed   # + chatgpt/claude (пул V,
                                                     # видимое окно Chrome)

Состояние чатов — в отдельном контексте data/bench_web/, чаты персон не
затрагиваются. ВНИМАНИЕ: браузерный пул общий с запущенным ботом — во время
прогона не пользуйтесь ботом с провайдером webchat (вкладки рассинхронируются).
"""

import argparse
import logging
import sys
import time

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.WARNING)

# (ярлык сложности, messages в формате role/content). Системные — реалистичные
# задачи-классификаторы бота (инициатива/тональность): system+user.
MESSAGES = [
    ("системное", [
        {"role": "system",
         "content": "Ты классификатор инициативы чат-бота. По реплике "
                    "пользователя реши, уместно ли боту позже написать "
                    "первым. Ответь строго одним словом: «нужна» или «нет»."},
        {"role": "user", "content": "Устал сегодня, весь день совещания."}]),
    ("системное", [
        {"role": "system",
         "content": "Ты определяешь тональность сообщения пользователя. "
                    "Ответь строго одним словом: «позитив», «негатив» или "
                    "«нейтрально»."},
        {"role": "user", "content": "Наконец-то сдал отчёт, свобода!"}]),
    ("лёгкое", [{"role": "user", "content": "Привет! Как дела?"}]),
    ("лёгкое", [{"role": "user", "content": "Спасибо тебе за помощь вчера!"}]),
    ("среднее", [{"role": "user",
                  "content": "Объясни простыми словами, чем акция отличается "
                             "от облигации. Три-четыре предложения."}]),
    ("среднее", [{"role": "user",
                  "content": "Расскажи коротко, как правильно заваривать "
                             "зелёный чай: температура воды и время."}]),
    ("сложное", [{"role": "user",
                  "content": "Составь таблицу из пяти крупнейших стран мира "
                             "по площади: страна, столица, площадь в км² "
                             "(приблизительно). После таблицы одним "
                             "предложением скажи, у какой из них наименьшая "
                             "плотность населения и почему."}]),
]

TIERS = ["системное", "лёгкое", "среднее", "сложное"]


def _configured_sites() -> list:
    # Сайты из WEBCHAT_SITES/WEBCHAT_SITE (.env), отфильтрованные по ADAPTERS
    from app.core.router import _parse_webchat_sites
    from app.features.web_llm import ADAPTERS
    return [s for s in _parse_webchat_sites() if s in ADAPTERS]


def bench_site(site: str, messages: list, pause: float = 6.0) -> list:
    """Прогон всех сообщений по одному сайту. Возвращает список
    (ярлык, секунды | None, длина ответа)."""
    from app.features.web_llm import WebChatLLM
    llm = WebChatLLM(site, context="bench_web")
    results = []
    for i, (tier, msgs) in enumerate(messages):
        cold = " [cold]" if i == 0 else ""
        # Пауза между отправками: UI чата после ответа ещё «отходит»
        # (стриминг закрылся, но поле ввода не готово) — иначе send-verify
        # ловит «сообщение не появилось в ленте» на быстрых сериях
        if i > 0 and pause > 0:
            time.sleep(pause)
        t0 = time.monotonic()
        try:
            resp = llm.get_response(msgs)
        except Exception as e:
            resp, err = None, f"{type(e).__name__}: {e}"
        else:
            err = "" if resp else "None (фолбэк)"
        dt = time.monotonic() - t0
        results.append((tier, dt if resp else None, len(resp or "")))
        status = f"{dt:6.1f} с, {len(resp)} симв." if resp else f"СБОЙ ({err})"
        print(f"    {i + 1}. {tier}{cold}: {status}", flush=True)
    return results


def main():
    ap = argparse.ArgumentParser(description="Бенчмарк скорости веб-чатов LLM")
    ap.add_argument("sites", nargs="*", help="сайты (по умолчанию — все из WEBCHAT_SITES)")
    ap.add_argument("--fast", action="store_true", help="только лёгкие сообщения")
    ap.add_argument("--pause", type=float, default=6.0,
                    help="пауза между отправками на одном сайте, сек (дефолт 6)")
    ap.add_argument("--allow-headed", action="store_true",
                    help="разрешить headed-пул V (chatgpt/claude) без режима "
                         "управления — на машине появится видимое окно Chrome")
    args = ap.parse_args()

    if args.allow_headed:
        from app.features import browser_actions as ba
        ba._BCFG["headed_fallback_without_control"] = True

    sites = args.sites or _configured_sites()
    if not sites:
        print("Не настроено ни одного сайта (WEBCHAT_SITES пуст)")
        sys.exit(1)
    messages = [m for m in MESSAGES if m[0] == "лёгкое"] if args.fast else MESSAGES

    print(f"Сайты: {', '.join(sites)}")
    counts = {t: sum(1 for x, _ in messages if x == t) for t in TIERS}
    print(f"Сообщений на сайт: {len(messages)} ("
          + ", ".join(f"{t}×{n}" for t, n in counts.items() if n) + ")")

    all_results = {}
    for site in sites:
        print(f"\n=== {site} ===", flush=True)
        all_results[site] = bench_site(site, messages, pause=args.pause)

    # ── сводка ──
    print("\n=== СВОДКА ===")
    header = f"{'сайт':<10}" + "".join(f"{t:^16}" for t in TIERS if counts.get(t)) \
        + f"{'всего':^12}{'сбоев':^7}"
    print(header)
    print("-" * len(header))
    best_site, best_total = None, None
    for site, rows in all_results.items():
        cells, total, fails = [], 0.0, 0
        for tier in TIERS:
            times = [dt for t, dt, _ in rows if t == tier and dt is not None]
            need = counts.get(tier, 0)
            fails += sum(1 for t, dt, _ in rows if t == tier and dt is None)
            total += sum(times)
            cells.append(f"{sum(times) / len(times):^5.1f} с ({len(times)}/{need})"
                         if times else f"{'—':^10}")
        if fails == 0 and (best_total is None or total < best_total):
            best_site, best_total = site, total
        print(f"{site:<10}" + "".join(f"{c:^16}" for c in cells)
              + f"{total:^10.1f} с{fails:^7}")

    print()
    if best_site:
        print(f"Быстрее всех без сбоев: {best_site} "
              f"(суммарно {best_total:.1f} с на {len(messages)} сообщений)")
    else:
        print("Ни один сайт не ответил на все сообщения без сбоев.")


if __name__ == "__main__":
    main()

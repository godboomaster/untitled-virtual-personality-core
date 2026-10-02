"""Сравнение поисковиков резолва «открой X»: веб-Google (пул H) против DDG.

Для каждого запроса с эталонным адресом одна выдача на движок, по ней:
  hit@1/3/5 — эталон среди первых k ссылок (годится ли выдача для списка
              «выберите ссылку»);
  resolve   — find_site_url по этой же выдаче: верно / мимо / отказ.
Эталон — префикс «хост/путь» (www. и поддомены хоста засчитываются).

Нужен живой пул H (бот с веб-чатом запущен) — иначе Google-нога уходит в
DDG, и такие строки помечены. Между запросами к Google — пауза: серия
запросов подряд быстрее ловит капчу; капча — нога Google останавливается.

  python -m scripts.eval_site_search [--only google|ddg] [--pause 3]
"""

import argparse
import logging
import random
import sys
import time
from urllib.parse import urlparse

from app.features import web_search as ws

# (запрос, эталонные префиксы). Группы: навигационные бренды (кириллица и
# латиница), узкие страницы внутри сайта, документация
CASES = [
    # бренды, кириллица
    ("мгу", ["msu.ru"]),
    ("госуслуги", ["gosuslugi.ru"]),
    ("кинопоиск", ["kinopoisk.ru"]),
    ("хабр", ["habr.com"]),
    ("авито", ["avito.ru"]),
    ("ржд", ["rzd.ru"]),
    ("почта россии", ["pochta.ru"]),
    ("озон", ["ozon.ru"]),
    ("вшэ", ["hse.ru"]),
    ("мфти", ["mipt.ru"]),
    ("третьяковская галерея", ["tretyakovgallery.ru"]),
    ("эрмитаж", ["hermitagemuseum.org"]),
    # бренды, латиница
    ("github", ["github.com"]),
    ("stack overflow", ["stackoverflow.com"]),
    ("hugging face", ["huggingface.co"]),
    ("arxiv", ["arxiv.org"]),
    # узкие страницы
    ("python pathlib documentation", ["docs.python.org/3/library/pathlib"]),
    ("pypi requests", ["pypi.org/project/requests"]),
    ("библиотека мгу", ["nbmgu.ru"]),
    ("википедия python", ["ru.wikipedia.org/wiki/Python"]),
    ("mdn fetch api", ["developer.mozilla.org"]),
    ("яндекс карты", ["yandex.ru/maps", "yandex.com/maps"]),
    ("telegram bot api", ["core.telegram.org/bots/api"]),
    ("chrome devtools protocol", ["chromedevtools.github.io/devtools-protocol"]),
    ("ddgs pypi", ["pypi.org/project/ddgs"]),
    # документация
    ("python asyncio documentation", ["docs.python.org/3/library/asyncio"]),
    ("playwright python documentation", ["playwright.dev/python"]),
    ("pydantic docs", ["pydantic.dev/docs", "docs.pydantic.dev"]),
    ("httpx documentation", ["python-httpx.org"]),
    ("postgresql документация на русском", ["postgrespro.ru/docs"]),
]


def _match(url: str, prefixes) -> bool:
    p = urlparse(url or "")
    host = (p.hostname or "").lower()
    path = p.path or "/"
    for pref in prefixes:
        ph, _, pp = pref.partition("/")
        if (host == ph or host.endswith("." + ph)) and path.startswith("/" + pp):
            return True
    return False


def _rank(links, prefixes):
    for i, r in enumerate(links, 1):
        if _match(r.get("href", ""), prefixes):
            return i
    return None


def run_engine(engine: str, pause: float) -> list:
    rows = []
    real_search = ws.search_links
    for n, (q, exp) in enumerate(CASES):
        if engine == "google" and n:
            time.sleep(pause + random.uniform(0, pause))
        t0 = time.time()
        links, used = real_search(q, 10, engine=engine)
        dt = time.time() - t0
        # Резолв — по той же выдаче: второй запрос к поисковику не шлём
        ws.search_links = lambda *_a, **_k: (links, used)
        try:
            got = ws.find_site_url(q, engine=engine)
        finally:
            ws.search_links = real_search
        rows.append({"q": q, "rank": _rank(links, exp), "n": len(links),
                     "resolved": got,
                     "verdict": ("ok" if got and _match(got, exp)
                                 else "wrong" if got else "none"),
                     "sec": dt, "used": used,
                     "top": [r.get("href", "") for r in links[:3]]})
        mark = "" if used == engine else f"  [{used}!]"
        print(f"  {engine:6} {q[:34]:34} rank={rows[-1]['rank'] or '-':>2} "
              f"resolve={rows[-1]['verdict']:5} {dt:4.1f}s{mark}", flush=True)
        if engine == "google" and used != "google":
            print("  Google недоступен (карантин/капча/пул H) — нога остановлена")
            break
    return rows


def summary(engine: str, rows: list):
    k = len(rows)
    if not k:
        return
    hit = lambda m: sum(1 for r in rows if r["rank"] and r["rank"] <= m)
    ok = sum(r["verdict"] == "ok" for r in rows)
    wrong = sum(r["verdict"] == "wrong" for r in rows)
    lat = sorted(r["sec"] for r in rows)
    print(f"{engine:6} n={k:2}  hit@1 {hit(1):2}/{k}  hit@3 {hit(3):2}/{k}  "
          f"hit@5 {hit(5):2}/{k}  resolve ok {ok:2} / wrong {wrong:2} / "
          f"none {k - ok - wrong:2}  median {lat[k // 2]:.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("google", "ddg"))
    ap.add_argument("--pause", type=float, default=3.0,
                    help="базовая пауза между запросами к Google, с")
    ap.add_argument("-v", action="store_true", help="логи поиска")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.v else logging.WARNING,
                        format="%(message)s")
    results = {}
    for eng in ("ddg", "google"):
        if a.only and a.only != eng:
            continue
        print(f"— {eng}")
        results[eng] = run_engine(eng, a.pause)
    print()
    for eng, rows in results.items():
        summary(eng, rows)
    if len(results) == 2:
        g = {r["q"]: r for r in results["google"]}
        diff = [(r["q"], r["verdict"], g[r["q"]]["verdict"])
                for r in results["ddg"] if r["q"] in g
                and r["verdict"] != g[r["q"]]["verdict"]]
        if diff:
            print("\nРасхождения резолва (ddg → google):")
            for q, d, gg in diff:
                print(f"  {q[:40]:40} {d:5} → {gg}")
    miss = [(e, r) for e, rows in results.items() for r in rows
            if r["verdict"] != "ok"]
    if miss:
        print("\nПромахи (топ-3 выдачи):")
        for e, r in miss:
            print(f"  [{e}] {r['q']} → {r['resolved']}")
            for u in r["top"]:
                print(f"        {u[:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Точечный дебаг резолва элементов: почему бот находит/не находит цель.

Примеры:
  python3 scripts/debug_resolve.py "корзина" --url https://x.ru
  python3 scripts/debug_resolve.py "тест в поле поиск" --type --url https://ru.wikipedia.org
  python3 scripts/debug_resolve.py "корзина" --smoke        # синтетическая смоук-страница
  python3 scripts/debug_resolve.py "войти"                  # активная вкладка запущенного
                                                            # браузера бота (attach)

Показывает: снапшот (что вообще видно на странице), скоринг без LLM (top с
баллами), результат резолва (путь: score/llm/goal_snapshot/llm_wide/vision/…),
сырой ответ LLM. По умолчанию НИЧЕГО не нажимает — только резолв; --execute
выполняет найденное действие по-настоящему (closed-loop).

Флаги:
  --type       режим ввода (resolve_type вместо resolve_click)
  --no-router  не звать LLM вовсе (чистый детерминированный скоринг)
  --execute    выполнить найденное действие (клик/ввод)
  -v           DEBUG-логи
"""

import argparse
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="Дебаг резолва элементов бота")
    ap.add_argument("goal", help="цель клика («корзина») или, с --type, "
                                 "тело ввода («тест в поле поиск»)")
    ap.add_argument("--url", help="открыть URL в новой вкладке браузера бота")
    ap.add_argument("--tab-url", dest="tab_url", help="подстрока URL уже "
                    "открытой вкладки (без новой): «loyaltyprogram»")
    ap.add_argument("--smoke", action="store_true",
                    help="поднять синтетическую смоук-страницу "
                         "(как в scripts/smoke_browser_live.py)")
    ap.add_argument("--type", dest="type_mode", action="store_true",
                    help="режим ввода (resolve_type)")
    ap.add_argument("--no-router", dest="no_router", action="store_true",
                    help="не звать LLM (чистый скоринг)")
    ap.add_argument("--execute", action="store_true",
                    help="выполнить найденное действие (по умолчанию — "
                         "только резолв, ничего не нажимается)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from app.features import browser_actions as ba
    from app.features.computer_control import ComputerControlManager

    router = None
    if not args.no_router:
        try:
            from app.core.router import ModelRouter
            router = ModelRouter()
            print(f"router: vision="
                  f"{'да' if router.supports_vision() else 'нет'}")
        except Exception as e:
            print(f"router недоступен ({e}) — резолв без LLM")

    mgr = ComputerControlManager(context="debug", config={"click": True},
                                 base_dir=Path(tempfile.mkdtemp(
                                     prefix="vpc_dbg_")))

    # Браузер уже запущен (профиль бота) — не наш, за собой не убиваем
    we_started = not ba._cdp_available()
    tab_id = None
    try:
        if args.smoke:
            from scripts.smoke_browser_live import _write_pages
            args.url = _write_pages()
        if args.tab_url:
            # Живая вкладка по подстроке URL — без открытия новой
            tab_id = ba.find_tab_id(args.tab_url)
            if tab_id is None:
                print(f"вкладка «{args.tab_url}» не найдена")
                return 1
            mgr._last_tab_id = tab_id
        if args.url:
            tab_id = ba.open_new_tab(args.url)
            mgr._last_tab_id = tab_id
            time.sleep(1.0)
        try:
            url, host, items = ba.snapshot_elements(None, tab_id=tab_id)
        except Exception as e:
            print(f"снапшот не удался: {e}")
            return 1
        if args.url is None:
            # attach: резолв целим в ту же вкладку, что снапшотнули
            tab_id = ba.find_tab_id(url)
            mgr._last_tab_id = tab_id
            mgr._last_host = host
        print(f"\n== Снапшот {host} ({url[:70]}): {len(items)} элементов ==")
        for it in items[:15]:
            label = it.get("text") or it.get("aria") or ""
            print(f"  [{it['idx']:>3}] {it['tag']}/{it.get('role') or '-'}"
                  f"{' ed' if it.get('ed') else ''} {label[:60]}")
        if len(items) > 15:
            print(f"  … и ещё {len(items) - 15}")

        if not args.type_mode:
            scored = mgr._score_candidates(items, args.goal) \
                or mgr._score_scoped(items, args.goal)
            print("\n== Скоринг без LLM ==")
            for s, it in scored[:8]:
                print(f"  {s:6.1f} [{it['idx']:>3}] "
                      f"{str(it.get('text') or '')[:60]}")
            if not scored:
                print("  кандидатов нет (zero-match → дальше только "
                      "LLM/vision)")

        print("\n== Резолв ==")
        if args.type_mode:
            act, err = mgr.resolve_type(args.goal, None, router,
                                        chat_id="debug")
        else:
            act, err = mgr.resolve_click(args.goal, None, router,
                                         chat_id="debug")
        if act is None:
            print(f"  отказ: {err}")
            return 1
        choose = act.get("choose") or {}
        print(f"  действие: {act['kind']} idx={act['idx']} "
              f"«{str(act.get('element') or '')[:50]}»")
        print(f"  путь: {choose.get('path')}, llm_response: "
              f"{str(choose.get('llm_response'))[:80]!r}")
        for c in (choose.get("candidates") or [])[:5]:
            print(f"    кандидат: [{c['idx']}] {c['score']:6.1f} "
                  f"{str(c.get('text') or '')[:50]}")
        if args.execute:
            ok, detail = mgr.execute(act, "debug", router=router)
            print(f"  execute: {'OK' if ok else 'FAIL'} — {detail}")
        else:
            print("  (только резолв; --execute — выполнить по-настоящему)")
        return 0
    finally:
        if we_started:
            try:
                ba._WORKER.submit(
                    lambda w: w._kill_browser(ba._resolve_user_data_dir()))
            except Exception:
                pass
        elif tab_id is not None:
            print("(вкладку оставил открытой — браузер был запущен не нами)")


if __name__ == "__main__":
    sys.exit(main())

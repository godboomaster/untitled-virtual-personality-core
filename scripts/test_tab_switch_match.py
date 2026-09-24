"""Регрессия: переключение/операции вкладок по названию.

Кейс 24.09: «переключись на страницу попов» → «Вкладка «страница попов» не
найдена», хотя открыта «вуза - ПОПОВ А. А. - Общая инф…». Слово-носитель
«страницу» (любой падеж) оставалось в цели, а матч требует совпадения
каждого слова цели. Плюс «..» в конце ответа: «Причина: {detail}.» + detail
с точкой.

Браузер — моки (browser_actions.list_tabs). Запуск:
python -m scripts.test_tab_switch_match
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="tabmatch_"))
    tmp = Path(tempfile.mkdtemp(prefix="tabmatch_data_"))
    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    from app.features.computer_control import (
        ComputerControlManager, _strip_tab_filler, parse_tab_op,
        parse_tab_switch)
    from app.features import browser_actions as ba
    from app.features import browser_history as bh
    from app.features import web_search as ws

    # ── 1. Разбор фразы: носители срезаются в любом падеже ──
    check("parse: «переключись на страницу попов» → («попов», мягкая)",
          parse_tab_switch("переключись на страницу попов")
          == ("попов", False))
    check("parse: «перейди на страничку попова» → «попова»",
          parse_tab_switch("перейди на страничку попова")
          == ("попова", False))
    check("parse: «переключись на сайт гитхаба» → «гитхаба»",
          parse_tab_switch("переключись на сайт гитхаба")
          == ("гитхаба", False))
    check("parse: «перейди на вкладку страницы попова» → явная «попова»",
          parse_tab_switch("перейди на вкладку страницы попова")
          == ("попова", True))
    check("parse: старое поведение сохранено",
          parse_tab_switch("перейди на вкладку ютуб") == ("ютуб", True)
          and parse_tab_switch("переключись на гитхаб") == ("гитхаб", False)
          and parse_tab_switch("покажи вкладку почта") == ("почта", True)
          and parse_tab_switch("нажми кнопку войти") is None)
    check("parse: одни носители/«эту» — не команда",
          parse_tab_switch("перейди на эту страницу") is None
          and parse_tab_switch("перейди на текущую вкладку") is None
          and parse_tab_switch("перейди на страницу") is None
          and parse_tab_switch("перейди на сайт") is None
          and parse_tab_switch("переключись на окно") is None)
    check("parse_tab_op: второй носитель срезается",
          parse_tab_op("закрой вкладку сайта попов") == ("close", "попов")
          and parse_tab_op("обнови страницу попова") == ("reload", "попова")
          and parse_tab_op("закрой вкладку с ютубом") == ("close", "ютубом")
          and parse_tab_op("закрой страницу вконтакте")
          == ("close", "вконтакте"))
    check("_strip_tab_filler: падежи и латиница",
          _strip_tab_filler("страница попов") == "попов"
          and _strip_tab_filler("Страницу ПОПОВА") == "ПОПОВА"
          and _strip_tab_filler("вкладке сайта вуза") == "вуза"
          and _strip_tab_filler("tab github") == "github"
          and _strip_tab_filler("страница") == ""
          # «Странный» — не носитель (граница слова)
          and _strip_tab_filler("странный сайт") == "странный")

    # ── 2. Матч вкладок ──
    orig = (ba.list_tabs, ws.find_site_url, bh.find_in_history)
    ws.find_site_url = lambda name, **kw: None
    bh.find_in_history = lambda name: None
    tabs = [
        (1, "https://example.edu/student", "example.edu",
         "Личный кабинет обучающегося"),
        (2, "https://example.edu/kaf/persons/123", "example.edu",
         "вуза - ПОПОВ А. А. - Общая информация"),
        (3, "https://github.com/x", "github.com", "vpc · GitHub"),
    ]
    ba.list_tabs = lambda: list(tabs)
    try:
        m = ComputerControlManager(
            context="tabmatch", base_dir=tmp,
            config={"confirm": True, "allow_domains": [],
                    "sites": {"гитхаб": "github.com"}})

        def switch(goal, explicit=True):
            return m.resolve_tab_switch(goal, explicit)

        a, e = switch("попов", False)
        check("switch: «попов» (мягкая) → вкладка вуза - ПОПОВ",
              a is not None and a["kind"] == "tab_switch"
              and a["tab_id"] == 2)
        # Цель LLM-разбора приходит с носителем в им. падеже
        a, e = switch("страница попов")
        check("switch: «страница попов» (цель LLM) → вкладка ПОПОВ",
              a is not None and a.get("tab_id") == 2)
        a, e = switch("страницу попов", False)
        check("switch: «страницу попов» → вкладка ПОПОВ",
              a is not None and a.get("tab_id") == 2)
        for goal in ("попова", "попову", "поповым", "Попов", "ПОПОВ"):
            a, e = switch(goal)
            check(f"switch: падеж/регистр «{goal}» → вкладка ПОПОВ",
                  a is not None and a.get("tab_id") == 2)
        a, e = switch("личный кабинет")
        check("switch: «личный кабинет» → вкладка 1",
              a is not None and a.get("tab_id") == 1)
        a, e = switch("кабинета обучающегося")
        check("switch: «кабинета обучающегося» (падежи) → вкладка 1",
              a is not None and a.get("tab_id") == 1)
        a, e = switch("сайт гитхаба")
        check("switch: «сайт гитхаба» → алиас → github",
              a is not None and a.get("tab_id") == 3)
        a, e = switch("попкорн")
        check("switch: «попкорн» не цепляет «ПОПОВ» → отказ",
              a is None and e and "не найдена" in e)
        a, e = switch("страница")
        check("switch: цель из одного носителя — без ложного матча",
              a is None and e and "не найдена" in e)
        a, e = switch("квакушка", False)
        check("switch: мягкая без совпадений → (None, None)",
              a is None and e is None)

        # Два одинаково подходящих — не выбираем наугад
        tabs.append((4, "https://example.edu/kaf/persons/456", "example.edu",
                     "вуза - ПОПОВ Б. В. - Кафедра"))
        a, e = switch("страницу попов")
        check("switch: два «ПОПОВ» → уточнение, а не случайная",
              a is None and e and "несколько" in e.lower())
        # Уточнение словом из заголовка — однозначно
        a, e = switch("попов кафедра")
        check("switch: «попов кафедра» → вкладка 4",
              a is not None and a.get("tab_id") == 4)
        tabs.pop()

        # Точный префикс сильнее совпадения лишь по основе
        tabs.append((5, "https://example.org/", "example.org",
                     "Попова М. — блог"))
        a, e = switch("попов")
        check("switch: «попов» — «ПОПОВ» и «Попова» равны → "
              "уточнение",
              a is None and e and "несколько" in e.lower())
        tabs.pop()

        a, e = m.resolve_tab_op("страницу попов", "close", None)
        check("tab_op: «закрой страницу попов» → tab_id вкладки ПОПОВ",
              a is not None and a["kind"] == "tab_op" and a["tab_id"] == 2)
    finally:
        ba.list_tabs, ws.find_site_url, bh.find_in_history = orig

    # ── 3. «..» в ответе: «Причина: {detail}.» + detail с точкой ──
    from app.features import flavor_text as ft
    ft._bank_path = lambda context: tmp / f"{context}_bank.json"
    ft._save_bank("tabmatch", {
        "_meta": {"prompt_hash": "x", "generated_at": time.time()},
        "kinds": {"generic": {"err": [
            "Операция отклонена. Причина: {detail}."]}}})
    txt = ft._from_bank("tabmatch", "generic", "err", None,
                        "Вкладка «x» не найдена. Скажи «открой …», "
                        "если нужна новая.")
    check("flavor: без «..» в конце",
          txt == "Операция отклонена. Причина: Вкладка «x» не найдена. "
                 "Скажи «открой …», если нужна новая.")
    txt2 = ft._from_bank("tabmatch", "generic", "err", None, "нет сети")
    check("flavor: detail без точки — точка фразы на месте",
          txt2 == "Операция отклонена. Причина: нет сети.")

    print(f"\nИтог: {ok} проверок")
    sys.exit(0 if ok > 0 else 1)


if __name__ == "__main__":
    main()

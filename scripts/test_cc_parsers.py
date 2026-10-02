"""Тест парсеров команд режима управления (computer_control: parse_* и
resolve/resolve_many/resolve_tab_switch): бытовые фразы не становятся
командами, вежливость/кавычки/предлоги срезаются, англ. формы, составные
команды, поисковый резолв только для бренд-подобных имён.

Браузер, поисковик и история подменены — живой Chrome и сеть не трогаются.

Запуск: python -m scripts.test_cc_parsers
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cc_parsers_")
    tmp = Path(tempfile.mkdtemp(prefix="cc_parsers_data_"))

    ok = 0
    fail = 0

    def check(name, cond):
        nonlocal ok, fail
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        if cond:
            ok += 1
        else:
            fail += 1

    import app.features.computer_control as cc
    from app.features.computer_control import (
        ComputerControlManager, parse_cart_request, parse_click_request,
        parse_close_request, parse_control_mode, parse_download_request,
        parse_media_request, parse_open_many, parse_open_request,
        parse_open_with_url, parse_read_request, parse_scroll_request,
        parse_scroll_to_goal, parse_search_on_site, parse_slider_request,
        parse_tab_op, parse_tab_switch, parse_type_request,
        split_compound_command)

    # ── 1. «открой душу/свет» — не поиск сайта в fast-path ──
    for t, want in [("открой душу", ["душу"]), ("включи свет", ["свет"]),
                    ("открой мне секрет", ["секрет"])]:
        check(f"open_many: {t!r} разбирается как цель (резолв решит)",
              parse_open_many(t) == want)
    check("tab_op: «открой новую вкладку» → new",
          parse_tab_op("открой новую вкладку") == ("new", None)
          and parse_open_many("открой новую вкладку") is None
          and parse_tab_op("open a new tab") == ("new", None))

    import app.features.web_search as ws
    import app.features.browser_history as bh
    searched = []
    orig_find, orig_hist = ws.find_site_url, bh.find_in_history
    ws.find_site_url = lambda name, **kw: (searched.append(name),
                                     "https://random-domain.example/")[1]
    bh.find_in_history = lambda name: None
    try:
        m = ComputerControlManager(
            context="t", base_dir=tmp,
            config={"confirm": False, "sites": {"ютуб": "https://youtube.com"}})
        searched.clear()
        check("resolve auto: кириллические обычные слова — без поисковика",
              m.resolve_many(["душу"]) is None
              and m.resolve_many(["свет"]) is None
              and m.resolve_many(["мне секрет"]) is None
              and m.resolve_many(["the light"]) is None and searched == [])
        a = m.resolve_many(["figma"])
        check("resolve auto: бренд-подобное латинское имя — поиск + via_search",
              a is not None and a.get("via_search") is True
              and searched == ["figma"])
        check("needs_confirm: via_search требует подтверждения даже при "
              "confirm:false",
              m.needs_confirm(a) is True
              and m.needs_confirm({"kind": "url",
                                   "value": "https://youtube.com"}) is False)
        searched.clear()
        a2 = m.resolve_many(["ютуб", "figma"])
        check("resolve_many: multi с поисковым пунктом помечен via_search",
              a2 and a2["kind"] == "multi" and a2.get("via_search") is True)
        a3 = m.resolve("ютуб", web_search=False)
        check("resolve: алиас — без via_search",
              a3 == {"kind": "url", "value": "https://youtube.com"})
        searched.clear()
        check("resolve web_search=False — поисковик не зовём",
              m.resolve("figma", web_search=False) is None and searched == [])
        check("resolve web_search=True (LLM-ярус) — поиск и для кириллицы",
              (m.resolve("вуза", web_search=True) or {}).get("via_search")
              and searched == ["вуза"])
        check("resolve: кавычки/запятая по краям цели срезаются",
              m.resolve("«ютуб»,", web_search=False)
              == {"kind": "url", "value": "https://youtube.com"})

        # ── 6. resolve_tab_switch: мягкий фолбэк — только алиас/история ──
        m.list_open_tabs = lambda: []
        searched.clear()
        act, err = m.resolve_tab_switch("какой-то сайт", explicit=False)
        check("resolve_tab_switch мягкая: промах — без поисковика, в диалог",
              act is None and err is None and searched == [])
        act, err = m.resolve_tab_switch("ютуб", explicit=False)
        check("resolve_tab_switch мягкая: алиас — открытие сайта",
              act == {"kind": "url", "value": "https://youtube.com"})
        act, err = m.resolve_tab_switch("почтой", explicit=True)
        check("resolve_tab_switch явная: промах — отказ с подсказкой",
              act is None and "не найдена" in (err or "") and searched == [])
        act, err = m.resolve_tab_op(None, "close_all")
        check("resolve_tab_op close_all — отказ с подсказкой",
              act is None and "по одной" in (err or ""))
        act, err = m.resolve_tab_op(None, "new")
        check("resolve_tab_op new — подсказка, не пустая вкладка",
              act is None and "открой" in (err or ""))
    finally:
        ws.find_site_url, bh.find_in_history = orig_find, orig_hist

    # ── 2. «открой вкладку с почтой» — переключение ──
    for t in ("открой вкладку с почтой", "перейди на вкладку с почтой",
              "открой вкладку почта", "зайди во вкладку с почтой"):
        check(f"tab_switch: {t!r} → «почт…», явная",
              (parse_tab_switch(t) or ("", False))[0].startswith("почт")
              and parse_tab_switch(t)[1] is True
              and parse_open_many(t) is None)
    check("tab_switch en: «switch to the youtube tab»",
          parse_tab_switch("switch to the youtube tab") == ("youtube", True))

    # ── 3. Элементы интерфейса — не поиск ролика ──
    for t, goal in [("открой комментарии на ютубе", "комментарии"),
                    ("открой настройки на ютубе", "настройки")]:
        check(f"UI-элемент: {t!r} — клик, не поиск на сайте",
              parse_search_on_site(t) is None
              and parse_click_request(t) == (goal, "ютубе"))
    check("«включи звук на ютубе» → unmute (с сайтом), не поиск",
          parse_search_on_site("включи звук на ютубе") is None
          and parse_media_request("включи звук на ютубе") == ("m", 1, "unmute")
          and parse_media_request("включи звук на ютубе", with_site=True)
          == (("m", 1, "unmute"), "ютубе"))
    check("поиск на сайте по-прежнему: «включи матрицу на кинопоиске»",
          parse_search_on_site("включи матрицу на кинопоиске")
          == ("матрицу", "кинопоиске", True))
    check("«поставь громкость на 50» — ползунок, не поиск на сайте «50»",
          parse_search_on_site("поставь громкость на 50") is None
          and parse_slider_request("поставь громкость на 50") is not None)
    check("«открой комментарии» без сайта — не клик (может быть алиас)",
          parse_click_request("открой комментарии") is None
          and parse_click_request("включи субтитры") == ("субтитры", None))

    # ── 4. «напиши рассказ» — не ввод ──
    for t in ("напиши рассказ", "напиши привет", "напиши стих про осень",
              "заполни анкету"):
        check(f"type: {t!r} — не команда ввода", parse_type_request(t) is None)
    check("type: «напиши привет в чат» / «введи привет» — ввод",
          parse_type_request("напиши привет в чат") == "привет в чат"
          and parse_type_request("введи привет") == "привет"
          and parse_type_request("набери 123") == "123")
    check("type: «enter control mode» — не ввод",
          parse_type_request("enter control mode") is None)

    # ── 5. Пунктуация/кавычки/вежливость ──
    for t in ("открой ютуб, пожалуйста", "открой «ютуб»", "открой \"ютуб\"",
              "пожалуйста, открой ютуб", "открой ютуб please",
              "открой ютуб!"):
        check(f"open: {t!r} → «ютуб»", parse_open_request(t) == "ютуб")
    check("open: «please open youtube» / «open youtube and github»",
          parse_open_request("please open youtube") == "youtube"
          and parse_open_many("open youtube and github",
                              known={"youtube", "github"}.__contains__)
          == ["youtube", "github"])

    # ── 7. Листание: мера/частицы не контейнер, англ. «scroll to X» ──
    for t, d in [("прокрути немного вниз", None),
                 ("прокрути чуть ниже", None),
                 ("прокрути страницу вниз пожалуйста", None),
                 ("прокрути немного вверх", "up"),
                 ("scroll down a bit", None)]:
        r = parse_scroll_request(t)
        check(f"scroll: {t!r} — без контейнера",
              r is not None and r[0] == "start" and r[4] is None
              and r[3] == d)
    check("scroll: «прокрути комментарии» — контейнер сохраняется",
          (parse_scroll_request("прокрути комментарии") or [None] * 5)[4]
          == "комментарии")
    check("scroll to: «scroll to comments» → доскролл, не контейнер",
          parse_scroll_request("scroll to comments") is None
          and parse_scroll_to_goal("scroll to comments") == "comments"
          and parse_scroll_to_goal("scroll down to the comments")
          == "comments")

    # ── 8. Корзина/ползунок: общие глаголы без слов шкалы/корзины ──
    for t in ("убавь пыл", "прибавь шагу", "увеличь зарплату",
              "убавь аппетиты", "уменьши нагрузку"):
        check(f"cart: {t!r} — не корзина", parse_cart_request(t) is None)
    check("cart: «убавь колу», «увеличь количество колы в корзине»",
          parse_cart_request("убавь колу") == ("decrease", "колу")
          and parse_cart_request("увеличь количество колы в корзине")
          == ("increase", "колы"))
    for t in ("установи будильник на 7", "передвинь встречу на 15 минут",
              "поставь чайник на 5 минут"):
        check(f"slider: {t!r} — не ползунок", parse_slider_request(t) is None)
    check("slider: громкость/перетащи — ползунок",
          parse_slider_request("поставь громкость на 50") is not None
          and parse_slider_request("перетащи цену на 500") is not None)

    # ── 9. Закрытие: только элементы интерфейса ──
    for t in ("закрой рот", "закрой глаза", "закрой тему",
              "скрой свои эмоции", "сверни разговор"):
        check(f"close: {t!r} — не команда", parse_close_request(t) is None)
    check("close: окно/рекламу/попап на сайте",
          parse_close_request("закрой окно") == ("закрой окно", None)
          and parse_close_request("закрой рекламу") == ("закрой рекламу", None)
          and parse_close_request("скрой попап на ютубе")
          == ("скрой попап", "ютубе"))

    # ── 10. Скачивание: «сохрани» — только файл/сайт ──
    for t in ("сохрани это в памяти", "сохрани мой номер",
              "сохрани это на потом"):
        check(f"download: {t!r} — не скачивание",
              parse_download_request(t) is None)
    check("download: «скачай мне музыку» / «сохрани картинку» / сайт",
          parse_download_request("скачай мне музыку") == ("музыку", None)
          and parse_download_request("сохрани картинку") == ("картинку", None)
          and parse_download_request("сохрани отчёт на гитхабе")
          == ("отчёт", "гитхабе"))

    # ── 11. Кнопки браузера ──
    for t, op in [("нажми назад", "back"), ("нажми обновить", "reload"),
                  ("кликни вперёд", "forward"),
                  ("нажми на кнопку назад", "back")]:
        check(f"tab_op: {t!r} → {op}", parse_tab_op(t) == (op, None))

    # ── 12. Цель клика: предлоги/носители/вежливость/сайт в начале ──
    for t, want in [("нажми на войти", ("войти", None)),
                    ("кликни по кнопке войти", ("войти", None)),
                    ("нажми на кнопку «Войти»", ("Войти", None)),
                    ("нажми войти пожалуйста", ("войти", None)),
                    ("нажми на ютубе подписаться", ("подписаться", "ютубе")),
                    ("click on the login button", ("login", None)),
                    ("нажми подписаться на ютубе", ("подписаться", "ютубе")),
                    ("нажми на кнопке меню", ("меню", None))]:
        check(f"click: {t!r} → {want}", parse_click_request(t) == want)

    # ── 13. Англ. формы ──
    check("en media: pause / volume up / volume down",
          parse_media_request("pause") == ("Space", 1, "toggle")
          and parse_media_request("volume up") == ("ArrowUp", 2, "vol_up")
          and parse_media_request("volume down")
          == ("ArrowDown", 2, "vol_down"))
    check("en tab_op: go back / refresh the page / close the tab",
          parse_tab_op("go back") == ("back", None)
          and parse_tab_op("refresh the page") == ("reload", None)
          and parse_tab_op("close the tab") == ("close", None)
          and parse_tab_op("close the youtube tab") == ("close", "youtube"))
    check("control mode en: enter/exit/on/off",
          parse_control_mode("enter control mode") is True
          and parse_control_mode("exit control mode") is False
          and parse_control_mode("control mode on") is True
          and parse_control_mode("control mode off") is False
          and parse_control_mode("выйди из режима управления, пожалуйста")
          is False
          and parse_control_mode("what is control mode") is None)

    # ── 14. Чтение / «закрой все вкладки» / «следующее видео» ──
    check("read: «прочитай текст песни Yesterday» — не чтение страницы",
          parse_read_request("прочитай текст песни Yesterday") != ("page", None))
    check("read: «прочитай страницу/текст» — чтение страницы",
          parse_read_request("прочитай страницу") == ("page", None)
          and parse_read_request("прочитай текст на этой странице")
          == ("page", None))
    check("read: «что ответила мама» — не команда; бот/сайт — чтение",
          parse_read_request("что ответила мама") is None
          and parse_read_request("что ответил бот") == ("last", "бот")
          and parse_read_request("что ответила мама в телеграме")
          == ("last", "телеграме"))
    check("tab_op: «закрой все вкладки» → close_all (резолв откажет)",
          parse_tab_op("закрой все вкладки") == ("close_all", None)
          and parse_close_request("закрой все вкладки") is None)
    check("«следующее видео» без глагола — рецепт youtube_next",
          parse_open_many("следующее видео") == ["следующее видео"]
          and parse_open_many("открой следующее видео") == ["следующее видео"]
          and cc.next_video_recipe("следующее видео"))
    check("«душа» без глагола — не команда",
          parse_open_many("душа") is None)

    # ── 15. Хвост составной команды — не шаги nav ──
    r = parse_open_with_url("открой dodo.ru и нажми на пепперони фреш",
                            with_rest=True)
    check("open_with_url: «…и нажми на X» — хвост командой, не шаг пути",
          r == ("dodo.ru", [], ["нажми на пепперони фреш"]))
    r = parse_open_with_url(
        "открой dodo.ru, введи в поле search додо пицца и отправь",
        with_rest=True)
    check("open_with_url: «, введи … и отправь» — одна команда-хвост",
          r == ("dodo.ru", [],
                ["введи в поле search додо пицца и отправь"]))
    check("open_with_url: без with_rest — прежняя пара без хвоста",
          parse_open_with_url("открой dodo.ru и нажми на пепперони фреш")
          == ("dodo.ru", []))
    check("open_with_url: «сайт» после адреса — не шаг",
          parse_open_with_url("открой dodo.ru сайт") == ("dodo.ru", []))
    check("open_with_url: путь через « - » сохраняется",
          parse_open_with_url("открой на example.com/827 студентам - "
                              "Технологии баз данных")
          == ("example.com/827", ["студентам", "Технологии баз данных"]))
    check("open_many: часть с глаголом-командой — не второй сайт",
          parse_open_many("открой dodo.ru и нажми на пепперони фреш") is None
          and parse_open_many("открой ютуб и нажми подписаться") is None
          and parse_open_many("open youtube and play music") is None
          and parse_open_many("открой ютуб и запусти телеграм")
          == ["ютуб", "телеграм"])
    for t, want in [
            ("открой додо и нажми на пепперони фреш",
             ["открой додо", "нажми на пепперони фреш"]),
            ("открой ютуб, потом включи музыку",
             ["открой ютуб", "включи музыку"]),
            ("open youtube then click subscribe",
             ["open youtube", "click subscribe"]),
            ("открой Тома и Джерри", ["открой Тома и Джерри"]),
            ("открой ютуб и гитхаб", ["открой ютуб и гитхаб"]),
            ("введи привет и отправь", ["введи привет и отправь"])]:
        check(f"split_compound_command: {t!r}",
              split_compound_command(t) == want)

    # ── Волна 3, парсеры: «and» в названиях, сайт в начале клика, слайдер ──
    known = {"youtube", "github", "ютуб"}.__contains__
    for t, want in [("open Barnes and Noble", ["Barnes and Noble"]),
                    ("open Marks and Spencer", ["Marks and Spencer"]),
                    ("open Crate and Barrel", ["Crate and Barrel"]),
                    ("включи Tom and Jerry", ["Tom and Jerry"]),
                    ("открой Rock and Roll", ["Rock and Roll"]),
                    ("open youtube and github", ["youtube", "github"]),
                    ("open youtube and open github", ["youtube", "github"]),
                    ("open youtube and Noble", ["youtube and Noble"])]:
        got = parse_open_many(t, known=known)
        check(f"open_many: {t!r} → {want} (got {got})", got == want)
    check("open_many: без known «and» делит только явные домены",
          parse_open_many("open Barnes and Noble") == ["Barnes and Noble"]
          and parse_open_many("open example.com and github.com")
          == ["example.com", "github.com"])
    check("open_many: «and <глагол>» — составная, не второй сайт",
          parse_open_many("open youtube and play music", known=known) is None)
    m_kn = ComputerControlManager(
        context="kn", config={"sites": {"ютуб": "https://www.youtube.com",
                                        "gh": "github.com"},
                              "apps": {"музыка": "Music"}},
        base_dir=tmp / "kn")
    check("is_known_target: алиас/хост алиаса/приложение/домен — да",
          m_kn.is_known_target("ютуб") and m_kn.is_known_target("YouTube")
          and m_kn.is_known_target("github") and m_kn.is_known_target("музыка")
          and m_kn.is_known_target("example.org"))
    check("is_known_target: «Barnes»/«Noble»/«www» — нет",
          not m_kn.is_known_target("Barnes") and not m_kn.is_known_target("Noble")
          and not m_kn.is_known_target("www") and not m_kn.is_known_target(""))
    for t, want in [("нажми на красное платье", ("красное платье", None)),
                    ("нажми на последнее сообщение", ("последнее сообщение", None)),
                    ("нажми на третье видео", ("третье видео", None)),
                    ("нажми на мое имя", ("мое имя", None)),
                    ("нажми на ютубе подписаться", ("подписаться", "ютубе")),
                    ("нажми на почте входящие", ("входящие", "почте"))]:
        got = parse_click_request(t)
        check(f"click: {t!r} → {want} (got {got})", got == want)
    for t, want in [("слайдер громкости на 70", ("громкости", 70, "")),
                    ("сделай звук 20%", ("звук", 20, "pct")),
                    ("громкость 50 процентов", ("громкость", 50, "pct")),
                    ("поставь громкость на 50", ("громкость", 50, "")),
                    ("перемотай ползунок на 2 минуты", ("перемотка", 2, "min")),
                    ("перемотай слайдер на 30 секунд", ("перемотка", 30, "sec"))]:
        got = parse_slider_request(t)
        check(f"slider: {t!r} → {want} (got {got})",
              got is not None and got[0] == want)
    for t in ("перемотай на 2 минуты", "сделай звук", "звук 2000"):
        check(f"slider: {t!r} — не ползунок", parse_slider_request(t) is None)

    print(f"\nИтог: {ok} OK / {fail} FAIL")
    return 0


if __name__ == "__main__":
    sys.exit(main())

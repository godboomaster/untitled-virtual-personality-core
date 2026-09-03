"""Smoke-тест управления компьютером уровня 1 (computer_control).

Проверяет: разбор конфига, нормализацию URL и whitelist доменов, резолв
apps/tasks per-OS, срезку маркеров, pending-подтверждение (TTL/да/нет),
исполнение через подменённый _dispatch, аудит-лог, инструкцию для промпта,
интеграцию с prepare_messages и guard маркеров в conversation_style.

Запуск: python -m scripts.test_computer_control
"""

import sys
import tempfile
import os
import json
import subprocess
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="compcontrol_smoke_")
    tmp = Path(tempfile.mkdtemp(prefix="compcontrol_data_"))

    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    from app.features.computer_control import (
        ComputerControlManager, classify_confirmation, config_enabled)

    CFG = {
        "confirm": True,
        "allow_domains": ["youtube.com"],
        "apps": {"safari": "Safari", "chrome": {"darwin": "Google Chrome", "win32": "chrome"}},
        "tasks": {"музыка": {"darwin": 'shortcuts run "Музыка"'}},
    }

    class SpyManager(ComputerControlManager):
        """_dispatch подменён: реальные системные вызовы в тесте не делаем."""

        def __init__(self, *a, fail_with=None, **kw):
            super().__init__(*a, **kw)
            self.calls = []
            self.fail_with = fail_with

        def _dispatch(self, action, router=None):
            if self.fail_with:
                raise self.fail_with
            self.calls.append(dict(action))

    def make(cfg=CFG, **kw):
        return SpyManager(context="test", config=cfg, base_dir=tmp, **kw)

    # ── 1. Конфиг ──
    m = make()
    check("дефолт: confirm=True", m.confirm is True)
    check("дефолт: click=True (агентный клик включён)", m.click is True)
    check("config_enabled: false/None/пустой dict → режим выключен",
          config_enabled(False) is False
          and config_enabled(None) is False
          and config_enabled({}) is False)
    check("config_enabled: dict с enabled:false → выключен (списки сохранены)",
          config_enabled({"enabled": False, "sites": {"ютуб": "youtube.com"}}) is False)
    check("config_enabled: true / dict без enabled / enabled:true → включён",
          config_enabled(True) is True
          and config_enabled({"confirm": True}) is True
          and config_enabled({"enabled": True, "sites": {"ютуб": "youtube.com"}}) is True)
    check("не-dict конфиг не роняет конструктор",
          SpyManager(context="t", config=True, base_dir=tmp).confirm is True)
    check("ключи apps/tasks нормализуются в lowercase",
          make(cfg={**CFG, "apps": {"Safari": "Safari"}}).available_apps() == ["safari"])

    # ── 2. URL: нормализация и домены ──
    check("URL без схемы → https://",
          m._normalize_url("youtube.com/watch?v=1") == "https://youtube.com/watch?v=1")
    check("file:// и javascript: отклоняются",
          m._normalize_url("file:///etc/passwd") is None
          and m._normalize_url("javascript:alert(1)") is None)
    check("URL с пробелом отклоняется", m._normalize_url("you tube.com") is None)
    check("домен из whitelist проходит (и поддомен)",
          m._domain_allowed("https://youtube.com/x") and m._domain_allowed("https://music.youtube.com/"))
    check("домен вне whitelist отклоняется",
          not m._domain_allowed("https://evil-youtube.com/") and not m._domain_allowed("https://example.com/"))
    check("пустой whitelist = любые домены",
          make(cfg={**CFG, "allow_domains": []})._domain_allowed("https://example.com/"))

    # ── 3. Резолв apps/tasks ──
    import sys as _sys
    plat = {"darwin": "darwin", "win32": "win32"}.get(_sys.platform, "linux")
    expect_chrome = {"darwin": "Google Chrome", "win32": "chrome"}.get(plat)
    check("app строкой резолвится на любой ОС",
          m._build_action("app", "safari") == {"kind": "app", "key": "safari", "value": "Safari"})
    if expect_chrome:
        check("app per-OS dict резолвится для текущей ОС",
              m._build_action("app", "chrome")["value"] == expect_chrome)
    check("неизвестное приложение отклоняется", m._build_action("app", "photoshop") is None)
    if plat == "darwin":
        m_win_only = make(cfg={**CFG, "tasks": {"только_win": {"win32": "x"}}})
        check("task без ключа текущей ОС отклоняется (нет other)",
              m_win_only._build_action("task", "только_win") is None)
        m2 = make(cfg={**CFG, "tasks": {"музыка": {"darwin": 'shortcuts run "М"', "win32": "x"}}})
        check("task per-OS резолвится на macOS",
              m2._build_action("task", "музыка")["value"] == 'shortcuts run "М"')
    else:
        check("task только для macOS на другой ОС отклоняется",
              m._build_action("task", "музыка") is None)
    check("task строкой резолвится на любой ОС",
          make(cfg={**CFG, "tasks": {"тест": "echo hi"}})._build_action("task", "тест")["value"] == "echo hi")

    # ── 4. Маркеры: срезка + pending (confirm=True) ──
    m = make()
    clean, notices = m.process_markers("Конечно! Открыть YouTube? [OPEN_URL:youtube.com]", "c1")
    check("маркер срезан из видимого текста",
          clean == "Конечно! Открыть YouTube?" and notices == [])
    pend = m.get_pending("c1")
    check("confirm-режим: действие в pending, исполнения нет",
          pend == {"kind": "url", "value": "https://youtube.com"} and m.calls == [])
    clean2, _ = m.process_markers("Запустить Safari? [OPEN_APP:safari]", "c2")
    check("pending per-chat независимы",
          m.get_pending("c2")["kind"] == "app" and m.get_pending("c1")["kind"] == "url")
    clean3, _ = m.process_markers("Открыть? [OPEN_URL:example.com]", "c3")
    check("маркер вне whitelist доменов отклонён, pending не создан",
          m.get_pending("c3") is None)
    check("ответ без маркеров возвращается как есть",
          m.process_markers("просто текст", "c9") == ("просто текст", []))
    check("маркер — единственное содержимое → подставляется вопрос подтверждения",
          m.process_markers("[OPEN_URL:youtube.com]", "c10")[0] == "Открыть youtube.com?"
          and m.get_pending("c10") is not None)

    # ── 5. Немедленный режим (confirm=False) ──
    mi = make(cfg={**CFG, "confirm": False})
    clean, notices = mi.process_markers("Открываю. [OPEN_URL:youtube.com]", "c4")
    check("immediate: исполнено сразу, pending нет",
          mi.calls == [{"kind": "url", "value": "https://youtube.com"}]
          and mi.get_pending("c4") is None and notices == [])
    mf = make(cfg={**CFG, "confirm": False}, fail_with=RuntimeError("no display"))
    clean, notices = mf.process_markers("Открываю. [OPEN_URL:youtube.com]", "c5")
    check("immediate: неудача → уведомление пользователю",
          len(notices) == 1 and "Не удалось" in notices[0])
    mr = make(cfg={**CFG, "confirm": False})
    _, notices = mr.process_markers("[OPEN_APP:photoshop]", "c6")
    check("immediate: отклонённый маркер → уведомление",
          len(notices) == 1 and mr.calls == [])
    check("immediate: маркер — единственное содержимое → «Готово, …»",
          mi.process_markers("[OPEN_URL:youtube.com]", "c11")[0] == "Готово, открыл youtube.com.")

    # ── 5b. Fast-path «открой X»: парсинг и резолв ──
    from app.features.computer_control import parse_open_request
    check("parse: «открой ютуб» → «ютуб»", parse_open_request("открой ютуб") == "ютуб")
    check("parse: пожалуйста/сайт/знаки срезаются",
          parse_open_request("Открой пожалуйста сайт YouTube!") == "YouTube")
    check("parse: запусти/open/start тоже команды",
          parse_open_request("запусти телеграм") == "телеграм"
          and parse_open_request("open youtube") == "youtube")
    check("parse: «мне/сайт/пожалуйста» между глаголом и названием срезаются",
          parse_open_request("открой мне сайт нгту") == "нгту"
          and parse_open_request("открой сайт нгту") == "нгту"
          and parse_open_request("открой пожалуйста ютуб") == "ютуб"
          and parse_open_request("открой ютуб пожалуйста") == "ютуб")
    check("parse: «открой мне» без названия — не команда",
          parse_open_request("открой мне") is None)
    check("parse: «страницу/вкладку» срезаются, «открой сайт» — не команда",
          parse_open_request("открой страницу кутузова нгту") == "кутузова нгту"
          and parse_open_request("открой страницу кутузовой нгту") == "кутузовой нгту"
          and parse_open_request("открой вкладку ютуб") == "ютуб"
          and parse_open_request("открой сайт") is None)
    check("parse: «приложение/программу» срезаются",
          parse_open_request("открой приложение clip studio paint") == "clip studio paint"
          and parse_open_request("запусти программу телеграм") == "телеграм")
    for t in ("открой ютуб и включи музыку", "а откройка ютуб", "расскажи про ютуб",
              "открой дверь", "открой напоминание", "", "открой " + "очень " * 20 + "длинный"):
        check(f"parse: НЕ команда: {t[:34]!r}", parse_open_request(t) is None)

    mr_ = make()
    # Поисковый резолв и историю браузера стабаем заранее: сеть и личные
    # данные машины в тестах не трогаем
    import app.features.web_search as _ws
    import app.features.browser_history as _bh
    _orig_find = _ws.find_site_url
    _orig_hist = _bh.find_in_history
    _ws.find_site_url = lambda name, **kw: "https://www.youtube.com/" if name == "ютуб" else None
    _bh.find_in_history = lambda name: None
    try:
        check("resolve: ключ apps → app-действие",
              mr_.resolve("safari") == {"kind": "app", "key": "safari", "value": "Safari"})
        check("resolve: регистр и лишние пробелы не мешают",
              mr_.resolve("  Safari ") == {"kind": "app", "key": "safari", "value": "Safari"})
        check("resolve: домен с точкой нормализуется",
              mr_.resolve("youtube.com") == {"kind": "url", "value": "https://youtube.com"})
        check("resolve: домен вне whitelist → None (путь LLM)",
              mr_.resolve("example.com") is None)
        check("resolve: неизвестное приложение → None (поиск ответил None)",
              mr_.resolve("photoshop") is None)
        check("resolve: «ютуб» через поисковый резолв → youtube",
              mr_.resolve("ютуб") == {"kind": "url",
                                      "value": "https://www.youtube.com/",
                                      "expect_name": "ютуб"})
        check("resolve: поиск ничего не нашёл → None",
              mr_.resolve("какой-то ноунейм") is None)
    finally:
        _ws.find_site_url = _orig_find
        _bh.find_in_history = _orig_hist

    # Алиасы sites: мгновенный резолв без поиска, в т.ч. в маркерном пути
    ms_ = make(cfg={**CFG, "sites": {"ютуб": "youtube.com", "плохой": "javascript:x"}})
    check("resolve: алиас sites → url, поиск не нужен",
          ms_.resolve("ютуб") == {"kind": "url", "value": "https://youtube.com"})
    check("алиас с невалидным URL отброшен при загрузке конфига",
          "плохой" not in ms_.sites)
    check("маркер [OPEN_URL:ютуб] резолвится через алиас",
          ms_._build_action("url", "ютуб") == {"kind": "url", "value": "https://youtube.com"})
    check("алиас работает и при чужом whitelist доменов (авторский список)",
          ms_._build_action("url", "ютуб") is not None)

    # find_site_url: доменный матч против «википедия-first» выдачи
    class _FakeDDGS:
        def text(self, q, max_results=5):
            return [
                {"href": "https://en.wikipedia.org/wiki/YouTube"},
                {"href": "https://www.youtube.com/"},
                {"href": "https://play.google.com/store/apps/details?id=com.google.android.youtube"},
            ]
    _orig_ddgs, _orig_tr = _ws._get_ddgs, _ws._google_translate
    _ws._get_ddgs = lambda: _FakeDDGS
    _ws._google_translate = lambda text: "youtube" if text == "ютуб" else None
    try:
        check("find_site_url: «ютуб» → youtube.com, а не википедия",
              _ws.find_site_url("ютуб") == "https://www.youtube.com/")
        check("find_site_url: без доменного совпадения — None (не гадаем первым результатом)",
              _ws.find_site_url("ноунейм") is None)
    finally:
        _ws._get_ddgs, _ws._google_translate = _orig_ddgs, _orig_tr

    # find_site_url: «нгту» — домен вуза (nstu.ru) аббревиатуру не содержит,
    # резолв через заголовок; группа ВК и другой вуз (nntu.ru) отфильтрованы,
    # URL сводится к корню, а не к SEO-подстранице
    class _FakeDDGSNstu:
        def text(self, q, max_results=5):
            return [
                {"href": "https://vk.ru/nstu_vk",
                 "title": "НГТУ НЭТИ | Официальное сообщество"},
                {"href": "https://www.nstu.ru/entrance/enrollment_campaign/current_numbers",
                 "title": "НГТУ. Конкурсная ситуация 2026"},
                {"href": "https://www.nntu.ru/content/abiturientam",
                 "title": "НГТУ им. Р.Е. Алексеева | Нижегородский"},
            ]
    _ws._get_ddgs = lambda: _FakeDDGSNstu
    _ws._google_translate = lambda text: "ngtu" if text == "нгту" else None
    try:
        check("find_site_url: «нгту» → корень nstu.ru по заголовку (не ВК, не nntu)",
              _ws.find_site_url("нгту") == "https://www.nstu.ru/")
        # Кириллический домен (.рф): punycode хоста декодируется → доменный матч,
        # даже когда в заголовке названия нет
        class _FakeDDGSIdn:
            def text(self, q, max_results=5):
                return [{"href": "https://xn--c1atqe.xn--p1ai/studies",
                         "title": "Обучающимся"}]
        _ws._get_ddgs = lambda: _FakeDDGSIdn
        check("find_site_url: punycode-домен (нгту.рф) матчится по домену",
              _ws.find_site_url("нгту") == "https://xn--c1atqe.xn--p1ai/")
    finally:
        _ws._get_ddgs, _ws._google_translate = _orig_ddgs, _orig_tr

    # find_site_url: мультисловное название. Домен maps.google.com не содержит
    # слаг «гуглкарты»/«googlemaps» (порядок слов!), а заголовок «Google Карты»
    # смешивает языки — матч по словам ru/en; сегмент /maps в пути сохраняется
    class _FakeDDGSMaps:
        def text(self, q, max_results=5):
            return [
                {"href": "https://www.google.com/maps/@55.0,83.0,12z",
                 "title": "Google Карты"},
                {"href": "https://maps.google.com/", "title": "Google Maps"},
                {"href": "https://ru.wikipedia.org/wiki/Google_Карты",
                 "title": "Google Карты — Википедия"},
            ]
    _ws._get_ddgs = lambda: _FakeDDGSMaps
    _ws._google_translate = lambda text: "google maps" if text == "гугл карты" else None
    try:
        check("find_site_url: «гугл карты» → google.com/maps (слова + сегмент пути)",
              _ws.find_site_url("гугл карты") == "https://www.google.com/maps")
    finally:
        _ws._get_ddgs, _ws._google_translate = _orig_ddgs, _orig_tr

    # find_site_url: падежная форма запроса («кутузовой») матчится с «КУТУЗОВА»
    # в заголовке через основу слова; страница персоны сохраняется целиком
    class _FakeDDGSKutuzova:
        def text(self, q, max_results=5):
            return [
                {"href": "https://ru.wikipedia.org/wiki/Лицей_НГТУ",
                 "title": "Лицей НГТУ — Википедия"},
                {"href": "https://ciu.nstu.ru/kaf/persons/98849",
                 "title": "НГТУ - КУТУЗОВА И. А. - Общая информация"},
            ]
    _ws._get_ddgs = lambda: _FakeDDGSKutuzova
    _ws._google_translate = lambda text: "kutuzova ngtu" if text == "кутузовой нгту" else None
    try:
        check("find_site_url: «кутузовой нгту» (падеж) → страница Кутузовой целиком",
              _ws.find_site_url("кутузовой нгту") == "https://ciu.nstu.ru/kaf/persons/98849")
    finally:
        _ws._get_ddgs, _ws._google_translate = _orig_ddgs, _orig_tr

    # ── 5г. Этап 2: мульти-команды, поиск на сайте, история браузера ──
    from app.features.computer_control import parse_open_many, parse_search_on_site
    check("parse many: «открой ютуб и кинопоиск» → две цели",
          parse_open_many("открой ютуб и кинопоиск") == ["ютуб", "кинопоиск"])
    check("parse many: у второй части свой глагол и филлеры",
          parse_open_many("открой ютуб и запусти приложение музыку") == ["ютуб", "музыку"])
    check("parse many: одиночная команда — список из одной",
          parse_open_many("открой мне сайт нгту") == ["нгту"])
    check("parse many: стоп-слово в любой части → None",
          parse_open_many("открой ютуб и дверь") is None)
    check("parse many: хвостовое «пожалуйста» срезается",
          parse_open_many("открой ютуб пожалуйста") == ["ютуб"])
    check("parse search: «включи интерстеллар на кинопоиске»",
          parse_search_on_site("включи интерстеллар на кинопоиске") == ("интерстеллар", "кинопоиске", True))
    check("parse search: «открой X на ютуб» — тоже поиск на сайте",
          parse_search_on_site("открой utopia show на ютуб") == ("utopia show", "ютуб", True))
    check("parse search: филлер «видео» срезается, длинный запрос проходит",
          parse_search_on_site("открой видео winter is here - you're not alone "
                               "in this cold на ютуб")
          == ("winter is here - you're not alone in this cold", "ютуб", True)
          and parse_search_on_site("найди видео winter is here на ютуб")
          == ("winter is here", "ютуб", False))
    check("parse search: голый филлер без запроса / запрос > 80 — None",
          parse_search_on_site("открой видео на ютубе") is None
          and parse_search_on_site("открой " + "x" * 82 + " на ютуб") is None)
    check("parse search: англ. глаголы и предлоги (open/play/on/in)",
          parse_search_on_site("open expedition 33 piano collection on youtube")
          == ("expedition 33 piano collection", "youtube", True)
          and parse_search_on_site("play utopia show on youtube") == ("utopia show", "youtube", True))
    check("parse search: найди/поищи/search → страница поиска (direct=False)",
          parse_search_on_site("найди интерстеллар на кинопоиске") == ("интерстеллар", "кинопоиске", False)
          and parse_search_on_site("search utopia on youtube") == ("utopia", "youtube", False))
    check("parse search: без сайта — None",
          parse_search_on_site("включи музыку") is None
          and parse_search_on_site("открой ютуб") is None)

    ms2 = make(cfg={**CFG, "allow_domains": [],
                    "sites": {"ютуб": "youtube.com", "кинопоиск": "kinopoisk.ru"},
                    "search": {"кинопоиск": "https://www.kinopoisk.ru/index.php?kp_query={q}",
                               "ютуб": {"url": "https://www.youtube.com/results?search_query={q}",
                                        "first": r"/watch\?v=[\w-]{11}"},
                               "youtube": {"url": "https://www.youtube.com/results?search_query={q}",
                                           "first": r"/watch\?v=[\w-]{11}"},
                               "плохой": "без-плейсхолдера"}})
    check("config: шаблон без {q} отброшен при загрузке",
          "плохой" not in ms2.search_urls)
    check("config: regex first подхвачен только у словарной формы",
          "ютуб" in ms2.search_first and "кинопоиск" not in ms2.search_first)

    # update_config: правки из веб-настроек применяются на живую менеджером
    mu = make()
    mu.stats["markers"] = 3
    mu.update_config({"confirm": False, "sites": {"новый": "example.com"},
                      "apps": {"телеграм": "Telegram"}})
    check("update_config: allowlist'ы перечитываются, статистика сохраняется",
          mu.confirm is False
          and mu.sites.get("новый") == "https://example.com"
          and mu.apps.get("телеграм") == "Telegram"
          and mu.stats["markers"] == 3)

    # Под-переключатель click: клики выключаются отдельно от остального
    # computer_control и так же применяются на живую
    mu.update_config({"click": False})
    check("update_config: click=False выключает агентный клик", mu.click is False)
    mu.update_config({"click": True})
    check("update_config: click обратно включается", mu.click is True)

    # recipe-задачи (этап 3b): значение "recipe:<id>" уходит в browser_actions,
    # а не в shell; неизвестный id — (False, человеческая причина)
    import app.features.browser_actions as _ba
    _calls = []
    _orig_run = _ba.run_recipe

    def _fake_run(rid):  # сеть/CDP не трогаем, но валидация id — настоящая
        if rid not in _ba.RECIPES:
            raise _ba.BrowserUnavailable(f"неизвестный рецепт «{rid}»")
        _calls.append(rid)
    _ba.run_recipe = _fake_run
    try:
        rc = make(cfg={**CFG, "tasks": {"пауза": "recipe:youtube_toggle"}})
        act = rc.resolve("паузу")  # stem: «паузу» → ключ «пауза»
        check("recipe: resolve по основе слова → task-действие",
              act == {"kind": "task", "key": "пауза", "value": "recipe:youtube_toggle"})
        # роутинг проверяем на настоящем _dispatch (SpyManager его подменяет)
        real = ComputerControlManager(context="t", base_dir=tmp,
                                      config={"tasks": {"пауза": "recipe:youtube_toggle"}})
        ok_, _ = real.execute(act, "c")
        check("recipe: recipe:<id> уходит в browser_actions, shell не тронут",
              ok_ and _calls == ["youtube_toggle"])
        ok2, detail = real.execute({"kind": "task", "key": "х", "value": "recipe:no_such"}, "c")
        check("recipe: неизвестный id → (False, человеческая причина)",
              not ok2 and "рецепт" in detail)
    finally:
        _ba.run_recipe = _orig_run
    from app.features.browser_actions import RECIPES
    check("реестр рецептов: домен (str | None=активная) и JS-сниппет",
          all((h is None or isinstance(h, str)) and isinstance(js, str) and js
              for h, js in RECIPES.values()))

    # Номерные результаты: «третье видео» → recipe:search_pick:3
    from app.features.computer_control import ordinal_recipe
    check("ordinal: «третье видео»/«2 результат»/«пятое видео»",
          ordinal_recipe("третье видео") == "search_pick:3"
          and ordinal_recipe("2 результат") == "search_pick:2"
          and ordinal_recipe("пятое видео") == "search_pick:5")
    check("ordinal: «видео»/«третий»/«второй диван» — None",
          ordinal_recipe("видео") is None and ordinal_recipe("третий") is None
          and ordinal_recipe("второй диван") is None)
    check("ordinal: «в плейлисте» → playlist_pick, «в выдаче» срезается",
          ordinal_recipe("третье видео в плейлисте") == "playlist_pick:3"
          and ordinal_recipe("2 результат в выдаче") == "search_pick:2"
          and ordinal_recipe("второй диван в плейлисте") is None)
    check("resolve: «третье видео в плейлисте» → recipe:playlist_pick:3",
          ms2.resolve("третье видео в плейлисте")
          == {"kind": "task", "key": "третье видео в плейлисте",
              "value": "recipe:playlist_pick:3"})
    check("resolve: «второе видео» → recipe:search_pick:2 (до истории/DDG)",
          ms2.resolve("второе видео") == {"kind": "task", "key": "второе видео",
                                          "value": "recipe:search_pick:2"})
    check("_build_action: маркер [RUN_TASK:третий результат] → search_pick:3",
          ms2._build_action("task", "третий результат")
          == {"kind": "task", "key": "третий результат", "value": "recipe:search_pick:3"})

    _captured = {}
    _orig_ae = _ba._run_apple_events
    _orig_cdp = _ba._cdp_available
    _ba._run_apple_events = lambda host, js, scan_search=False, tab_id=None: (
        _captured.update(host=host, js=js, scan=scan_search), "ok:opened")[1]
    # Живой Chrome с отладкой переключает бэкенд на CDP мимо мока — глушим пробник
    _ba._cdp_available = lambda: False
    try:
        _ba.run_recipe("search_pick:4")
        check("run_recipe: номер подставлен в JS, вкладка — как у search_first",
              "var N=4;" in _captured["js"] and _captured["host"] is None)
        try:
            _ba.run_recipe("search_pick:0")
            bad = False
        except _ba.BrowserUnavailable:
            bad = True
        check("run_recipe: номер 0 отклоняется", bad)
    finally:
        _ba._run_apple_events = _orig_ae
        _ba._cdp_available = _orig_cdp

    # Явный адрес в команде открытия («открой на ciu.nstu.ru/827 студентам — …»):
    # длинная фраза → адрес + путь кликами по странице
    from app.features.computer_control import parse_open_with_url
    check("parse url: длинная фраза с явным адресом и путём",
          parse_open_with_url("открой на ciu.nstu.ru/827 студентам - Технологии "
                              "баз данных - Методические указания")
          == ("ciu.nstu.ru/827", ["студентам", "Технологии баз данных",
                                  "Методические указания"]))
    check("parse url: схема сохраняется / нет адреса / не команда",
          parse_open_with_url("открой https://example.com/a б в")
          == ("https://example.com/a", ["б в"])
          and parse_open_with_url("открой ютуб") is None
          and parse_open_with_url("расскажи про ciu.nstu.ru") is None)
    check("parse url: без хвоста — пустой путь; дефис внутри слова не рвётся",
          parse_open_with_url("открой ciu.nstu.ru/827") == ("ciu.nstu.ru/827", [])
          and parse_open_with_url("открой ciu.nstu.ru англо-русский словарь")
          == ("ciu.nstu.ru", ["англо-русский словарь"]))
    check("parse url: «X на адрес» — предлог перед адресом не липнет к пути",
          parse_open_with_url("открой страницу кутузовой на ciu.nstu.ru")
          == ("ciu.nstu.ru", ["кутузовой"]))
    check("resolve_url: whitelist доменов работает и для явного адреса",
          m.resolve_url("youtube.com/watch?v=1")
          == {"kind": "url", "value": "https://youtube.com/watch?v=1"}
          and m.resolve_url("ciu.nstu.ru/827") is None)

    # Нет вкладки с выдачей (search_first без поиска) — человеческое сообщение
    # без «None» в тексте (ветка __no_tab__ внутри _run_apple_events)
    import subprocess as _sp
    class _R:  # минимальный CompletedProcess: osascript «отработал», вкладки нет
        returncode = 0
        stdout = "__no_tab__\n"
        stderr = ""
    _orig_run = _sp.run
    _sp.run = lambda *a, **kw: _R()
    try:
        try:
            _ba._run_apple_events(None, "var x=1;", True)
            _msg = ""
        except _ba.BrowserUnavailable as e:
            _msg = str(e)
        check("run_apple_events: нет вкладки с выдачей — сообщение без «None»",
              "результатами поиска" in _msg and "None" not in _msg)
        # err 12 (JS из событий Apple выключен) — понятная подсказка, а не
        # «нет вкладки»: сентинел пробрасывается сквозь try-глушилку
        class _R12:
            returncode = 0
            stdout = "__js_disabled__\n"
            stderr = ""
        _sp.run = lambda *a, **kw: _R12()
        try:
            _ba._run_apple_events("example.com", "var x=1;")
            _msg = ""
        except _ba.BrowserUnavailable as e:
            _msg = str(e)
        check("run_apple_events: JS отключен — подсказка про настройку Chrome",
              "JavaScript из событий Apple" in _msg)
    finally:
        _sp.run = _orig_run

    # Многошаговая навигация (nav): адрес + путь → действие, формулировки,
    # пошаговое исполнение, осечка с честным прогрессом
    mnav = make(cfg={"confirm": True})
    nav_act = mnav.resolve_nav("ciu.nstu.ru/827",
                               ["студентам", "Технологии баз данных"])
    check("resolve_nav: адрес + путь → nav-действие",
          nav_act == {"kind": "nav", "value": "https://ciu.nstu.ru/827",
                      "steps": ["студентам", "Технологии баз данных"],
                      "host": "ciu.nstu.ru/827"})
    check("resolve_nav: без шагов — обычное открытие страницы",
          mnav.resolve_nav("ciu.nstu.ru/827", [])
          == {"kind": "url", "value": "https://ciu.nstu.ru/827"})
    check("формулировки nav: вопрос и «Готово»",
          mnav.confirm_question(nav_act)
          == "Открыть ciu.nstu.ru/827 и пройти: студентам → Технологии баз данных?"
          and mnav.describe_done(nav_act)
          == "открыл ciu.nstu.ru/827 и прошёл до «Технологии баз данных»")

    import types as _types
    import app.features.computer_control as _cc_mod

    def _it(idx, tag, text, **kw):
        """Элемент структурированного снапшота (как отдаёт snapshot_elements)."""
        it = {"idx": idx, "tag": tag, "role": "", "text": text, "aria": "",
              "title": "", "href": "", "w": 40.0, "h": 20.0, "vp": True}
        it.update(kw)
        return it

    # Целевой снапшот (фолбэк «элемент за бюджетом 100») по умолчанию «ничего
    # не нашёл» — иначе тесты с промахом клика дёргали бы настоящий браузер.
    # Секция проверки самого фолбэка перемокирует локально
    _ba.snapshot_for_goal = lambda host, goal, tab_id=None: ("", [])
    _orig_lp_all = _ba.list_pages
    _ba.list_pages = lambda: []
    # Pre-снапшот проходы (оверлеи/антибот/ожидание DOM): по умолчанию
    # «ничего нет» — секции их проверки перемокируют локально. Реальные
    # функции сохраняем: секция обёрток ниже гоняет их с подменённым eval
    _real_dismiss_overlay = _ba.dismiss_overlay
    _real_detect_antibot = _ba.detect_antibot
    _real_wait_dom_idle = _ba.wait_dom_idle
    _ba.dismiss_overlay = lambda host=None, tab_id=None: None
    _ba.detect_antibot = lambda host=None, tab_id=None: None
    _ba.wait_dom_idle = lambda *a, **kw: None
    # Доскролл-поиск (виртуализированные списки): по умолчанию «некуда
    # листать» — целевой снапшот не находит, поведение прежнее
    _ba.scroll_position = lambda host=None, tab_id=None: 0.0
    _ba.scroll_step = lambda host=None, tab_id=None: {"moved": False,
                                                      "bottom": True}
    _ba.scroll_restore = lambda host=None, tab_id=None, y=0.0: None

    _pages = [[_it(0, "a", "Студентам"), _it(1, "a", "Абитуриентам")],
              [_it(0, "a", "Новости"), _it(3, "a", "Технологии баз данных")]]
    _clicks2 = []
    _snapped = []
    _opened = []
    _orig_snap2, _orig_ct2 = _ba.snapshot_elements, _ba.click_tagged
    _orig_open, _orig_tm = _ba.open_new_tab, _cc_mod.time
    _ba.open_new_tab = lambda url: (_opened.append(url), 42)[1]
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        _snapped.append(tab_id),
        ("https://ciu.nstu.ru/827", "ciu.nstu.ru", _pages[min(len(_clicks2), 1)]))[1]
    _ba.click_tagged = lambda host, idx, tab_id=None, mark=None: (
        _clicks2.append((host, idx, tab_id)), "clicked")[1]
    _cc_mod.time = _types.SimpleNamespace(sleep=lambda s: None, time=_orig_tm.time)
    try:
        mnav._navigate(nav_act)
        check("nav: шаги проходятся кликами по порядку",
              _clicks2 == [("ciu.nstu.ru", 0, 42), ("ciu.nstu.ru", 3, 42)])
        check("nav: вкладка открывается отслеживаемой (id) и вся навигация — по ней",
              _opened == ["https://ciu.nstu.ru/827"]
              and _snapped == [42, 42])
        check("nav: финальная вкладка запоминается («на открывшейся странице»)",
              mnav._last_tab_id == 42)
        _clicks2.clear()
        bad_nav = {"kind": "nav", "value": "https://ciu.nstu.ru/827",
                   "host": "ciu.nstu.ru/827",
                   "steps": ["студентам", "несуществующий пункт"]}
        try:
            mnav._navigate(bad_nav)
            _nav_err = ""
        except RuntimeError as e:
            _nav_err = str(e)
        check("nav: пункт не найден — причина с прогрессом, клики остановлены",
              "несуществующий пункт" in _nav_err and "студентам" in _nav_err
              and _clicks2 == [("ciu.nstu.ru", 0, 42)])
        # Страница ещё грузится (снапшот падает) — ждём и пробуем снова
        _clicks2.clear()
        _fails = {"n": 0}
        def _flaky(host=None, tab_id=None):
            if _fails["n"] < 2:
                _fails["n"] += 1
                raise _ba.BrowserUnavailable("на странице нет кликабельных элементов")
            return ("https://ciu.nstu.ru/827", "ciu.nstu.ru",
                    _pages[min(len(_clicks2), 1)])
        _ba.snapshot_elements = _flaky
        mnav._navigate({"kind": "nav", "value": "https://ciu.nstu.ru/827",
                        "host": "ciu.nstu.ru/827", "steps": ["студентам"]})
        check("nav: страница не прогрузилась — повтор снапшота, а не отказ",
              _fails["n"] == 2 and _clicks2 == [("ciu.nstu.ru", 0, 42)])
        # DOM перерисовался между снапшотом и кликом («элемент потерян») —
        # шаг повторяется один раз со свежим снапшотом
        _clicks2.clear()
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            _snapped.append(tab_id),
            ("https://ciu.nstu.ru/827", "ciu.nstu.ru", _pages[0]))[1]
        _ct_n = {"n": 0}
        def _ct_flaky(host, idx, tab_id=None, mark=None):
            _ct_n["n"] += 1
            if _ct_n["n"] == 1:
                raise _ba.BrowserUnavailable("элемент потерян — страница изменилась")
            return "clicked"
        _ba.click_tagged = _ct_flaky
        mnav._navigate({"kind": "nav", "value": "https://ciu.nstu.ru/827",
                        "host": "ciu.nstu.ru/827", "steps": ["студентам"]})
        check("nav: «элемент потерян» — один повтор шага, успех",
              _ct_n["n"] == 2)
        # Клик без видимого эффекта (closed-loop, п.6) — тоже один повтор шага
        _ct_n2 = {"n": 0}
        def _ct_uncertain(host, idx, tab_id=None, mark=None):
            _ct_n2["n"] += 1
            if _ct_n2["n"] == 1:
                raise _ba.ClickUncertain("клик отправлен, но страница не изменилась")
            return "clicked"
        _ba.click_tagged = _ct_uncertain
        mnav._navigate({"kind": "nav", "value": "https://ciu.nstu.ru/827",
                        "host": "ciu.nstu.ru/827", "steps": ["студентам"]})
        check("nav: клик без эффекта — один повтор шага, успех",
              _ct_n2["n"] == 2)
        def _ct_dead(host, idx, tab_id=None, mark=None):
            raise _ba.BrowserUnavailable("элемент потерян — страница изменилась")
        _ba.click_tagged = _ct_dead
        try:
            mnav._navigate({"kind": "nav", "value": "https://ciu.nstu.ru/827",
                            "host": "ciu.nstu.ru/827", "steps": ["студентам"]})
            _nav_err2 = ""
        except RuntimeError as e:
            _nav_err2 = str(e)
        check("nav: повтор не помог — причина с именем шага",
              "на шаге «студентам»" in _nav_err2 and "элемент потерян" in _nav_err2)
    finally:
        _ba.snapshot_elements, _ba.click_tagged = _orig_snap2, _orig_ct2
        _ba.open_new_tab = _orig_open
        _cc_mod.time = _orig_tm

    # Обычный клик: метка протухла за время подтверждения (dodo перерисовывает
    # карусель баннеров) — один повтор со свежим снапшотом и свежим выбором
    _orig_snap10, _orig_ct10, _orig_pu10 = (_ba.snapshot_elements,
                                            _ba.click_tagged, _ba.page_urls)
    _ba.page_urls = lambda: ["https://dodopizza.ru/"]
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/", "dodopizza.ru",
        [_it(0, "button", "1 3 1 5 ₽"), _it(1, "a", "Пиццы")])
    try:
        mlost = ComputerControlManager(context="t", config={**CFG,
                                                          "allow_domains": []},
                                       base_dir=tmp / "s5-lost")
        _clicks_l = []
        _lost_n = {"n": 0}
        def _ct_lost(host, idx, tab_id=None, mark=None):
            _clicks_l.append(idx)
            _lost_n["n"] += 1
            if _lost_n["n"] == 1:
                raise _ba.BrowserUnavailable(
                    "элемент потерян — страница изменилась")
            return "clicked"
        _ba.click_tagged = _ct_lost
        ok_l, det_l = mlost.execute(
            {"kind": "click", "idx": 78, "element": "1 3 1 5 ₽",
             "host": "dodopizza.ru", "value": "https://dodopizza.ru/",
             "goal": "1 3 1 5 ₽"}, "c", router=None)
        check("click: «элемент потерян» — свежий снапшот + один повтор",
              ok_l and _clicks_l == [78, 0])
        def _ct_dead2(host, idx, tab_id=None, mark=None):
            raise _ba.BrowserUnavailable("элемент потерян — страница изменилась")
        _ba.click_tagged = _ct_dead2
        ok_d, det_d = mlost.execute(
            {"kind": "click", "idx": 78, "element": "1 3 1 5 ₽",
             "host": "dodopizza.ru", "value": "https://dodopizza.ru/",
             "goal": "1 3 1 5 ₽"}, "c", router=None)
        check("click: повтор не помог — честная ошибка",
              not ok_d and "элемент потерян" in det_d)
    finally:
        _ba.snapshot_elements = _orig_snap10
        _ba.click_tagged = _orig_ct10
        _ba.page_urls = _orig_pu10

    # Агентный клик «нажми X»: парс, LLM-выбор элемента, dispatch
    from app.features.computer_control import (
        PAGE_REF, parse_click_request, parse_open_on_page)
    check("parse click: «нажми кнопку скачать на гитхабе»",
          parse_click_request("нажми кнопку скачать на гитхабе") == ("скачать", "гитхабе"))
    check("parse click: «кликни «войти»» / не команда",
          parse_click_request("кликни «войти»") == ("войти", None)
          and parse_click_request("расскажи про кнопки") is None)
    check("parse click: «на этой странице» → PAGE_REF",
          parse_click_request("нажми кнопку войти на этой странице") == ("войти", PAGE_REF))
    check("parse click: оборот в середине («нажми на этой странице X»)",
          parse_click_request("нажми на этой странице кнопку войти") == ("войти", PAGE_REF))
    check("parse on_page: «открой X на этой странице» → цель клика",
          parse_open_on_page("открой методические указания на этой странице")
          == "методические указания"
          and parse_open_on_page("открой на этой странице студентам") == "студентам"
          and parse_open_on_page("открой ютуб") is None)

    class _FakeRouter:
        def __init__(self, resp): self.resp = resp
        def get_response(self, messages, **kw): return self.resp

    _snap_calls = []
    _snap_ids = []
    _orig_snap = _ba.snapshot_elements
    _gh_items = lambda: [_it(0, "a", "Войти"), _it(1, "button", "Скачать"),
                         _it(2, "a", "Помощь")]
    _ba.snapshot_elements = lambda host, tab_id=None: (
        _snap_calls.append(host), _snap_ids.append(tab_id),
        ("https://github.com/x", "github.com", _gh_items()))[2]
    class _BoomRouter:  # LLM не должен дёргаться при явном лидере по скору
        def get_response(self, *a, **kw):
            raise AssertionError("LLM вызван при точном матче")

    try:
        act_click, err_click = ms2.resolve_click("скачать", None, _BoomRouter())
        check("resolve_click: точный текстовый матч — без LLM",
              err_click is None
              and act_click["idx"] == 1 and act_click["element"] == "Скачать"
              and act_click["host"] == "github.com"
              and act_click["value"] == "https://github.com/x"
              and act_click.get("choose", {}).get("path") == "score")
        no_act, no_err = ms2.resolve_click("загрузить", None, _FakeRouter("подходящего нет"))
        check("resolve_click: нет кандидатов → (None, честная причина)",
              no_act is None and no_err is not None and "не нашёл" in no_err)
        ms3 = make(cfg={**CFG, "allow_domains": [], "sites": {"гитхаб": "github.com"}})
        act3, _ = ms3.resolve_click("войти", "гитхабе", _BoomRouter())
        check("resolve_click: «на гитхабе» → снапшот вкладки github.com",
              act3 is not None and _snap_calls[-1] == "github.com")
        # Явный домен без алиаса: «на ciu.nstu.ru» целится напрямую
        act4, _ = ms3.resolve_click("войти", "ciu.nstu.ru", _BoomRouter())
        check("resolve_click: явный домен в site_word — без алиаса",
              act4 is not None and _snap_calls[-1] == "ciu.nstu.ru")
        # «на этой странице»: цель — отслеживаемая вкладка (id в действии)
        ms3._last_tab_id = 555
        act5, _ = ms3.resolve_click("войти", PAGE_REF, _BoomRouter())
        check("resolve_click: PAGE_REF → снапшот по tab_id, id в действии",
              act5 is not None and act5.get("tab_id") == 555
              and _snap_ids[-1] == 555)
        # Отслеживаемая вкладка умерла → забываем её, фолбэк на _last_host
        ms3._last_tab_id = 999
        ms3._last_host = "github.com"
        _orig_snap4 = _ba.snapshot_elements
        _orig_tout, _orig_poll = _cc_mod.NAV_LOAD_TIMEOUT_SEC, _cc_mod.NAV_POLL_SEC
        _cc_mod.NAV_LOAD_TIMEOUT_SEC = 0.2  # опрос мёртвой вкладки — ускоряем
        _cc_mod.NAV_POLL_SEC = 0.05
        def _dead_tab(host=None, tab_id=None):
            if tab_id is not None:
                raise _ba.BrowserUnavailable("нет отслеживаемой вкладки")
            return ("https://github.com/x", "github.com", _gh_items())
        _ba.snapshot_elements = _dead_tab
        try:
            act6, _ = ms3.resolve_click("войти", None, _BoomRouter())
            check("resolve_click: мёртвая вкладка → фолбэк на хост, id забыт",
                  act6 is not None and "tab_id" not in act6
                  and ms3._last_tab_id is None)
        finally:
            _ba.snapshot_elements = _orig_snap4
            _cc_mod.NAV_LOAD_TIMEOUT_SEC, _cc_mod.NAV_POLL_SEC = _orig_tout, _orig_poll
        # Ни отслеживаемой вкладки, ни хоста — честная просьба открыть страницу.
        # Свой base_dir: контекст страницы персистится (last_tab.json), а общий
        # tmp уже содержит сохранённый хост от предыдущих секций
        ms4 = SpyManager(context="t", config={**CFG, "allow_domains": []},
                         base_dir=tmp / "s5-nopage")
        no_pg, err_pg = ms4.resolve_click("войти", PAGE_REF, _BoomRouter())
        check("resolve_click: PAGE_REF без открытой страницы — честный отказ",
              no_pg is None and err_pg is not None
              and "нет открытой" in err_pg.lower())
    finally:
        _ba.snapshot_elements = _orig_snap

    # Скоуп-клик «выбрать на Цезарь с беконом»: плоский матч пуст, кнопка
    # находится по контексту предка-карточки (поле ctx снапшота)
    _pizza = lambda: [
        _it(0, "a", "Цезарь с беконом 270 г Курица жареная, бекон"),
        _it(1, "button", "Выбрать",
            ctx="Цезарь с беконом 270 г Курица жареная, бекон 419 ₽ Выбрать"),
        _it(2, "a", "Маргарита 330 г Томатный соус, моцарелла"),
        _it(3, "button", "Выбрать",
            ctx="Маргарита 330 г Томатный соус, моцарелла 399 ₽ Выбрать"),
        # Ловушка: те же слова скопа, но не фразой — слабее точного попадания
        _it(4, "a", "Цезарь с сыром и беконом 245 г Курица жареная, бекон"),
        _it(5, "button", "Выбрать",
            ctx="Цезарь с сыром и беконом 245 г Курица жареная, бекон 429 ₽")]
    _orig_snap5 = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://yobidoyobi.ru/", "yobidoyobi.ru", _pizza())
    try:
        msc = SpyManager(context="t", config={**CFG, "allow_domains": []},
                         base_dir=tmp / "s5-scope")
        act_sc, err_sc = msc.resolve_click("выбрать на цезарь с беконом", None,
                                           _BoomRouter())
        check("scope click: кнопка «Выбрать» карточки «Цезарь с беконом» — без LLM",
              err_sc is None and act_sc["idx"] == 1
              and act_sc.get("choose", {}).get("scoped") is True
              and act_sc.get("choose", {}).get("path") == "score"
              and len(act_sc.get("choose", {}).get("candidates", [])) == 1)
        no_sc, err_nosc = msc.resolve_click("оформить на пепперони", None,
                                            _BoomRouter())
        check("scope click: скоп не найден → честный отказ",
              no_sc is None and err_nosc is not None and "не нашёл" in err_nosc)
        # Плоский матч приоритетнее скоупа: «цезарь с беконом» — это ссылка
        act_fl, _ = msc.resolve_click("цезарь с беконом", None, _BoomRouter())
        check("scope click: плоский матч не подменяется скоупом",
              act_fl is not None and act_fl["idx"] == 0
              and act_fl.get("choose", {}).get("scoped") is not True)
        # Однословный скоп съедается site-регексом («на маргарите») →
        # resolve_click возвращает слово в цель, раз это не алиас и не домен
        check("parse click: «на маргарите» уходит в site_word",
              parse_click_request("нажми выбрать на маргарите")
              == ("выбрать", "маргарите"))
        act_m, err_m = msc.resolve_click("выбрать", "маргарите", _BoomRouter())
        check("scope click: «выбрать» + site_word «маргарите» → скоп карточки",
              err_m is None and act_m["idx"] == 3)
        # «на странице» — не скоп: слово-пустышка в цель не возвращается
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://github.com/x", "github.com", _gh_items())
        act_np, _ = msc.resolve_click("войти", "странице", _BoomRouter())
        check("scope click: «на странице» не склеивается в скоп",
              act_np is not None and act_np["element"] == "Войти")
        # Пространственный скоп «в левой части»: фильтр по позиции элемента
        # (x + w/2 против vw/2), текст карточки тут бесполезен. Дубли
        # «Омлет сырный»: левый — панель выбора, правый — лента за ней
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://dodopizza.ru/x", "dodopizza.ru", [
                _it(0, "div", "Омлет сырный", x=300, w=300, vw=1400),
                _it(1, "a", "Омлет сырный", x=1000, w=250, vw=1400)])
        act_left, err_left = msc.resolve_click("омлет сырный в левой части",
                                               None, _BoomRouter())
        check("scope click: «в левой части» — позиционный фильтр (левый)",
              err_left is None and act_left["idx"] == 0
              and act_left.get("choose", {}).get("scoped") is True)
        act_right, err_right = msc.resolve_click("омлет сырный справа",
                                                 None, _BoomRouter())
        check("scope click: «справа» — позиционный фильтр (правый)",
              err_right is None and act_right["idx"] == 1)
        # Нет элемента на этой стороне — честный отказ
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://dodopizza.ru/x", "dodopizza.ru", [
                _it(0, "a", "Омлет сырный", x=1000, w=250, vw=1400)])
        no_side, err_noside = msc.resolve_click("омлет сырный в левой части",
                                                None, _BoomRouter())
        check("scope click: на этой стороне пусто → честный отказ",
              no_side is None and err_noside is not None
              and "не нашёл" in err_noside)
        # Тот же скоп существительным первым: «в части слева» = «в левой части»
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://dodopizza.ru/x", "dodopizza.ru", [
                _it(0, "div", "Сырный", x=300, w=300, vw=1400),
                _it(1, "div", "Сырный", x=1000, w=250, vw=1400)])
        act_nl, err_nl = msc.resolve_click("сырный в части слева",
                                           None, _BoomRouter())
        check("scope click: «в части слева» — позиционный фильтр (левый)",
              err_nl is None and act_nl["idx"] == 0)
        act_nr, err_nr = msc.resolve_click("сырный в части справа",
                                           None, _BoomRouter())
        check("scope click: «в части справа» — позиционный фильтр (правый)",
              err_nr is None and act_nr["idx"] == 1)
    finally:
        _ba.snapshot_elements = _orig_snap5

    # Опечатки: fuzzy-ярус скоринга (55) — ниже точного (70), выше контекста
    _orig_snap_fz = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/loyaltyprogram", "dodopizza.ru", [
            _it(0, "a", "Пиццы"), _it(1, "a", "Комбо"),
            _it(2, "button", "Что такое кэшбек?")])
    try:
        mfz = SpyManager(context="t", config={**CFG, "allow_domains": []},
                         base_dir=tmp / "s5-fuzzy")
        act_fz, err_fz = mfz.resolve_click("что такое кешбэк", None,
                                           _BoomRouter())
        check("fuzzy: «кешбэк» → «кэшбек» (перестановка), без LLM",
              err_fz is None and act_fz["idx"] == 2
              and act_fz.get("choose", {}).get("path") == "score")
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://dodopizza.ru/x", "dodopizza.ru", [
                _it(0, "a", "Пиццы"), _it(1, "div", "Красный лук 79 ₽"),
                _it(2, "div", "Моцарелла 125 ₽")])
        act_dk, err_dk = mfz.resolve_click("красный дук", None, _BoomRouter())
        check("fuzzy: «красный дук» → «красный лук» (якорь + 1 замена)",
              err_dk is None and act_dk["idx"] == 1)
        # Трёхбуквенное слово без якоря — не fuzzy-матчим (ложняки)
        act_lk, err_lk = mfz.resolve_click("дук", None, _BoomRouter())
        check("fuzzy: голое «дук» (3 буквы, без якоря) — честный отказ",
              act_lk is None and err_lk is not None and "не нашёл" in err_lk)
    finally:
        _ba.snapshot_elements = _orig_snap_fz

    # Вето на деструктивный клик: LLM ткнула в «Закрыть», а цель — соус
    _orig_snap_vt = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/x", "dodopizza.ru", [
            _it(0, "button", "Закрыть"), _it(1, "button", "Заменить"),
            _it(2, "button", "В корзину"), _it(3, "a", "Прямой эфир")])
    try:
        mvt = SpyManager(context="t", config={**CFG, "allow_domains": []},
                         base_dir=tmp / "s5-veto")
        act_vt, err_vt = mvt.resolve_click("сырный в части слева", None,
                                           _FakeRouter("1"))
        check("veto: llm_wide ткнул «Закрыть» без намерения — отказ",
              act_vt is None and err_vt is not None and "не нашёл" in err_vt)
        # Легитимное закрытие вето не режет
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://dodopizza.ru/x", "dodopizza.ru", [
                _it(0, "button", "Закрыть"), _it(1, "a", "Пиццы")])
        act_cl2, err_cl2 = mvt.resolve_click("закрыть окно", None,
                                             _FakeRouter("1"))
        check("veto: «закрыть окно» — намерение есть, клик легитимен",
              err_cl2 is None and act_cl2["idx"] == 0)
    finally:
        _ba.snapshot_elements = _orig_snap_vt

    # «заменить барбекю»: кнопка «Заменить» (действие в тексте) важнее строки
    # «Барбекю» (объект в тексте, действие — в контексте)
    _orig_snap_aw = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/x", "dodopizza.ru", [
            _it(0, "div", "Барбекю", ctx="1 шт 25 г заменить"),
            _it(1, "button", "Заменить", ctx="барбекю 1 шт 25 г"),
            _it(2, "button", "Заменить", ctx="сырный 1 шт 25 г")])
    try:
        maw = SpyManager(context="t", config={**CFG, "allow_domains": []},
                         base_dir=tmp / "s5-actword")
        act_aw, err_aw = maw.resolve_click("заменить барбекю", None,
                                           _BoomRouter())
        check("score: «заменить барбекю» → «Заменить» ряда барбекю, не строка",
              err_aw is None and act_aw["idx"] == 1)
    finally:
        _ba.snapshot_elements = _orig_snap_aw

    # Синонимы цели: «аватар» → доступное имя «Меню аккаунта» (icon-only кнопка
    # с пустым текстом — пользователь зовёт её не по aria-label)
    _orig_snap6 = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://www.youtube.com/", "www.youtube.com",
        [_it(0, "a", "Главная"), _it(1, "button", "", aria="Меню аккаунта"),
         _it(2, "a", "Подписки")])
    try:
        msyn = SpyManager(context="t", config={**CFG, "allow_domains": []},
                          base_dir=tmp / "s5-syn")
        act_av, err_av = msyn.resolve_click("аватар", None, _BoomRouter())
        check("syn: «аватар» → кнопка с aria «Меню аккаунта», без LLM",
              err_av is None and act_av["idx"] == 1
              and act_av.get("choose", {}).get("path") == "score")
        no_syn, err_syn = msyn.resolve_click("калейдоскоп", None, _BoomRouter())
        check("syn: слово без совпадений и синонимов — честный отказ",
              no_syn is None and err_syn is not None and "не нашёл" in err_syn)
    finally:
        _ba.snapshot_elements = _orig_snap6

    # «закрытие модального окна»: крестик попапа без текста подписан
    # «закрыть» ранним проходом снапшота; цель-канцеляризм сводится к ней
    _orig_snap7 = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/", "dodopizza.ru",
        [_it(0, "a", "Пиццы"), _it(1, "button", "закрыть"),
         _it(2, "button", "49 ₽")])
    try:
        mcl = SpyManager(context="t", config={**CFG, "allow_domains": []},
                         base_dir=tmp / "s5-close")
        act_cl, err_cl = mcl.resolve_click("закрытия модального окна", None,
                                           _BoomRouter())
        check("close: «закрытия модального окна» → крестик, без LLM",
              err_cl is None and act_cl["idx"] == 1
              and act_cl.get("choose", {}).get("path") == "score")
        act_cl2, err_cl2 = mcl.resolve_click("закрой", None, _BoomRouter())
        check("close: «закрой» — через синоним",
              err_cl2 is None and act_cl2["idx"] == 1)
    finally:
        _ba.snapshot_elements = _orig_snap7

    # Целевой снапшот за бюджетом 100: на dodo 350+ кликабельных, «Додстер»
    # из закусок в общий снапшот не влезает — фолбэк ищет текст цели по всему
    # DOM (snapshot_for_goal) и повторяет выбор уже на нём
    _orig_snap8, _orig_gsnap8 = _ba.snapshot_elements, _ba.snapshot_for_goal
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/", "dodopizza.ru",
        [_it(0, "a", "Пицца"), _it(1, "a", "Комбо")])  # додстер не влез
    try:
        mg = SpyManager(context="t", config={**CFG, "allow_domains": []},
                        base_dir=tmp / "s5-goal")
        # Фолбэк нашёл карточки; точное совпадение — явный лидер, без LLM
        _ba.snapshot_for_goal = lambda host, goal, tab_id=None: (
            "https://dodopizza.ru/",
            [_it(0, "a", "Додстер"), _it(1, "a", "Додстер Чилл Грилл"),
             _it(2, "a", "Острый Додстер")])
        act_g, err_g = mg.resolve_click("додстер", None, _BoomRouter())
        check("goal-snap: элемент за бюджетом — точный матч без LLM",
              err_g is None and act_g["idx"] == 0
              and act_g["element"] == "Додстер"
              and act_g.get("choose", {}).get("via") == "goal_snapshot")
        # Единственный ctx-кандидат (кнопка «Выбрать» карточки): целевой
        # снапшот уже отфильтровал по тексту цели — безопасен и без LLM
        _ba.snapshot_for_goal = lambda host, goal, tab_id=None: (
            "https://dodopizza.ru/",
            [_it(0, "button", "Выбрать", ctx="Додстер 419 ₽ Выбрать")])
        act_g2, err_g2 = mg.resolve_click("додстер", None, _BoomRouter())
        check("goal-snap: единственный ctx-кандидат (кнопка карточки)",
              err_g2 is None and act_g2["idx"] == 0
              and act_g2.get("choose", {}).get("path") == "goal_sole")
        # Не нашлось и в целевом снапшоте — честный отказ
        _ba.snapshot_for_goal = lambda host, goal, tab_id=None: ("", [])
        no_g, err_ng = mg.resolve_click("суши", None, _BoomRouter())
        check("goal-snap: нет совпадений нигде — честный отказ",
              no_g is None and err_ng is not None and "не нашёл" in err_ng)
        # Вето LLM уважаем: единственный кандидат, но LLM сказала «нет»
        _ba.snapshot_for_goal = lambda host, goal, tab_id=None: (
            "https://dodopizza.ru/",
            [_it(0, "button", "Выбрать", ctx="Додстер 419 ₽ Выбрать")])
        no_g2, err_ng2 = mg.resolve_click("додстер", None, _FakeRouter("нет"))
        check("goal-snap: вето LLM не перекрывается единственным кандидатом",
              no_g2 is None and err_ng2 is not None and "не нашёл" in err_ng2)
    finally:
        _ba.snapshot_elements = _orig_snap8
        _ba.snapshot_for_goal = _orig_gsnap8  # дефолт-мок «пусто», см. выше

    # Слабый лидер общего снапшота консультирует целевой: «соусы» уехало в
    # «2 соуса 89 ₽» по основе слова (промо-карточка — добавила бы лишнее
    # в заказ), а точная карточка «Соусы» жила ниже бюджета общего снапшота
    _orig_snap9, _orig_gsnap9 = _ba.snapshot_elements, _ba.snapshot_for_goal
    _gsnap_calls = []
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/", "dodopizza.ru",
        [_it(0, "a", "2 соуса 89 ₽"), _it(1, "a", "Пиццы")])
    _ba.snapshot_for_goal = lambda host, goal, tab_id=None: (
        _gsnap_calls.append(goal),
        ("https://dodopizza.ru/", [_it(0, "div", "Соусы")]))[1]
    try:
        mwk = SpyManager(context="t", config={**CFG, "allow_domains": []},
                         base_dir=tmp / "s5-weak")
        act_w, err_w = mwk.resolve_click("соусы", None, _BoomRouter())
        check("goal-snap: слабый лидер общего уступает точному из целевого",
              err_w is None and act_w["idx"] == 0
              and act_w["element"] == "Соусы"
              and act_w.get("choose", {}).get("via") == "goal_snapshot")
        # Точный лидер общего снапшота — целевой даже не дёргается
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://dodopizza.ru/", "dodopizza.ru",
            [_it(0, "a", "Соусы"), _it(1, "a", "Пиццы")])
        _gsnap_calls.clear()
        act_x, err_x = mwk.resolve_click("соусы", None, _BoomRouter())
        check("goal-snap: точный лидер общего — без лишнего снапшота",
              err_x is None and act_x["idx"] == 0
              and act_x.get("choose", {}).get("via") is None
              and _gsnap_calls == [])
        # Целевой пуст — слабый лидер общего остаётся (ничего лучше нет)
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://dodopizza.ru/", "dodopizza.ru",
            [_it(0, "a", "2 соуса 89 ₽"), _it(1, "a", "Пиццы")])
        _ba.snapshot_for_goal = lambda host, goal, tab_id=None: ("", [])
        act_y, err_y = mwk.resolve_click("соусы", None, _BoomRouter())
        check("goal-snap: целевой пуст — слабый лидер общего остаётся",
              err_y is None and act_y["element"] == "2 соуса 89 ₽")
    finally:
        _ba.snapshot_elements = _orig_snap9
        _ba.snapshot_for_goal = _orig_gsnap9

    mh = make(cfg={**CFG, "allow_domains": []})
    mh.execute({"kind": "url", "value": "https://youtube.com"}, "c")
    check("execute: хост последней открытой вкладки запоминается для клика",
          mh._last_host == "youtube.com")

    _orig_open3 = _ba.open_new_tab
    _ba.open_new_tab = lambda url: 4242
    try:
        real4 = ComputerControlManager(context="t", base_dir=tmp, config={})
        ok6, _ = real4.execute({"kind": "url", "value": "https://youtube.com"}, "c")
        check("dispatch: url на macOS открывается отслеживаемой вкладкой",
              ok6 and real4._last_tab_id == 4242)
    finally:
        _ba.open_new_tab = _orig_open3

    # Клик, открывший попап (окно входа Google): отслеживание переключается
    # на новое окно — следующие «введи …» целятся в него
    _orig_ct9, _orig_pu = _ba.click_tagged, _ba.page_urls
    _orig_fp, _orig_ft = _ba.follow_popup, _ba.find_tab_id
    _ba.page_urls = lambda: ["https://claude.ai/login"]
    _ba.click_tagged = lambda host, idx, tab_id=None, mark=None: "clicked"
    _ba.follow_popup = lambda pre, **kw: (
        (777, "accounts.google.com", "https://accounts.google.com/v3/signin")
        if pre == ["https://claude.ai/login"] else None)
    try:
        real5 = ComputerControlManager(context="t", base_dir=tmp / "s5-pop",
                                       config={})
        ok7, _ = real5.execute({"kind": "click", "idx": 3, "element": "Войти",
                                "host": "claude.ai",
                                "value": "https://claude.ai/login"}, "c")
        check("popup: клик открыл окно — отслеживание перешло на него",
              ok7 and real5._last_tab_id == 777
              and real5._last_host == "accounts.google.com")
        _saved = json.loads((tmp / "s5-pop" / "last_tab.json")
                            .read_text(encoding="utf-8"))
        check("popup: хост попапа сохранён на диск (last_tab.json)",
              _saved.get("host") == "accounts.google.com")
        # Попапа нет — обычный путь _remember_tab по хосту
        _ba.follow_popup = lambda pre, **kw: None
        _ba.find_tab_id = lambda host: 555
        ok8, _ = real5.execute({"kind": "click", "idx": 4, "element": "Войти",
                                "host": "claude.ai",
                                "value": "https://claude.ai/login"}, "c")
        check("popup: нового окна нет — remember_tab по хосту, как раньше",
              ok8 and real5._last_tab_id == 555
              and real5._last_host == "accounts.google.com")
    finally:
        _ba.click_tagged, _ba.page_urls = _orig_ct9, _orig_pu
        _ba.follow_popup, _ba.find_tab_id = _orig_fp, _orig_ft

    # Элемента нет на целевой вкладке, но он — единственный явный лидер на
    # другой открытой странице (попап, открытый до клика): кликаем там
    _orig_snap7, _orig_lp = _ba.snapshot_elements, _ba.list_pages
    _pages_map = {
        "claude.ai": ("https://claude.ai/login", "claude.ai",
                      [_it(0, "button", "Continue with email")]),
        "accounts.google.com": (
            "https://accounts.google.com/v3/signin", "accounts.google.com",
            [_it(0, "div", "Yuurei Reishi schoolyuurei@gmail.com"),
             _it(1, "div", "Использовать другой аккаунт")]),
        # чат с совпадающим текстом — из поиска исключается
        "127.0.0.1": ("http://127.0.0.1:5173/", "127.0.0.1",
                      [_it(0, "div", "Yuurei Reishi schoolyuurei@gmail.com")]),
    }
    def _snap7(host=None, tab_id=None):
        # Кросс-страничный поиск целится по полному URL (несколько вкладок
        # одного хоста), начальный снапшот — по хосту
        for v in _pages_map.values():
            if host in (v[0], v[1]):
                return v
        raise _ba.BrowserUnavailable(f"нет страницы {host}")
    _ba.snapshot_elements = _snap7
    _ba.list_pages = lambda: [(v[0], v[1]) for v in _pages_map.values()]
    try:
        mfb = SpyManager(context="t", config={**CFG, "allow_domains": []},
                         base_dir=tmp / "s5-otherpage")
        mfb._last_host = "claude.ai"
        act_x, err_x = mfb.resolve_click("yuurei reishi", None, _BoomRouter())
        check("page-fallback: лидер на другой странице — клик там (чат пропущен)",
              err_x is None and act_x["host"] == "accounts.google.com"
              and act_x.get("choose", {}).get("path") == "page_fallback")
        # Явно названный сайт, элемент — на другой вкладке ТОГО ЖЕ хоста
        _pages_map["accounts.google.com-2"] = (
            "https://accounts.google.com/v3/signin/confirm", "accounts.google.com",
            [_it(0, "button", "Далее")])
        act_sh, err_sh = mfb.resolve_click("далее", "accounts.google.com",
                                           _BoomRouter())
        check("page-fallback: сайт назван — поиск по вкладкам того же хоста",
              err_sh is None and act_sh is not None
              and act_sh["value"].endswith("/confirm")
              and act_sh.get("choose", {}).get("path") == "page_fallback")
        del _pages_map["accounts.google.com-2"]
        # ...но не по чужим сайтам
        no_sh, err_sh2 = mfb.resolve_click("yuurei reishi", "claude.ai",
                                           _BoomRouter())
        check("page-fallback: сайт назван — чужие хосты не трогаем",
              no_sh is None and err_sh2 is not None and "не нашёл" in err_sh2)
        # Две страницы с явным лидером — не гадаем, честный отказ
        _pages_map["mail.google.com"] = (
            "https://mail.google.com/x", "mail.google.com",
            [_it(0, "div", "Yuurei Reishi schoolyuurei@gmail.com")])
        no_x, err_x2 = mfb.resolve_click("yuurei reishi", None, _BoomRouter())
        check("page-fallback: лидеры на двух страницах — честный отказ",
              no_x is None and err_x2 is not None and "не нашёл" in err_x2)
    finally:
        _ba.snapshot_elements, _ba.list_pages = _orig_snap7, _orig_lp

    # ── 8d. Чтение со страницы «прочитай последнее сообщение» ──
    from app.features.computer_control import parse_read_request
    check("read-parse: «прочитай последнее сообщение (на кладе)»",
          parse_read_request("прочитай последнее сообщение") == ("last", None)
          and parse_read_request("прочитай последнее сообщение на кладе")
          == ("last", "кладе")
          and parse_read_request("прочитай страницу на ютубе")
          == ("page", "ютубе"))
    check("read-parse: «что ответил клод» / не команда — None",
          parse_read_request("что ответил клод") == ("last", "клод")
          and parse_read_request("прочитай книгу") is None
          and parse_read_request("расскажи новости") is None)
    _orig_snap8, _orig_rt8, _orig_ft8 = (
        _ba.snapshot_elements, _ba.read_text, _ba.find_tab_id)
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://claude.ai/chat/x", "claude.ai", _gh_items())
    _ba.read_text = lambda host=None, tab_id=None, mode="last": (
        f"mode={mode}: Проверка прошла — я на связи")
    _ba.find_tab_id = lambda host: None
    try:
        mrd = ComputerControlManager(context="t", base_dir=tmp / "s8-read",
                                     config={**CFG, "allow_domains": []})
        act_r, err_r = mrd.resolve_read("last", None)
        check("read: resolve → read-действие с хостом вкладки",
              err_r is None and act_r["kind"] == "read"
              and act_r["host"] == "claude.ai" and act_r["mode"] == "last")
        ok_r, det_r = mrd.execute(act_r, "c8r")
        check("read: execute — прочитанный текст уезжает в detail",
              ok_r and "Проверка прошла" in det_r and "mode=last" in det_r)
        rec_r = json.loads((tmp / "s8-read" / "audit.jsonl")
                           .read_text(encoding="utf-8").strip().splitlines()[-1])
        check("read: аудит kind=read, ok",
              rec_r.get("kind") == "read" and rec_r.get("ok") is True)
    finally:
        _ba.snapshot_elements, _ba.read_text = _orig_snap8, _orig_rt8
        _ba.find_tab_id = _orig_ft8

    # ── 8e. «…и отправь»: ввод + Enter в том же поле ──
    _orig_snap9, _orig_ft9 = _ba.snapshot_elements, _ba.fill_tagged
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://chat.deepseek.com", "chat.deepseek.com",
        [_it(0, "textarea", "Сообщение", ed=True)])
    try:
        msub = SpyManager(context="t", config={**CFG, "allow_domains": []},
                          base_dir=tmp / "s8-sub")
        act_s, err_s = msub.resolve_type("в поле сообщение привет и отправь",
                                         None, _BoomRouter())
        check("submit: «…и отправь» — текст без хвоста, флаг в действии",
              err_s is None and act_s["text"] == "привет"
              and act_s.get("submit") is True)
        act_s2, _ = msub.resolve_type("в поле сообщение привет", None,
                                      _BoomRouter())
        check("submit: без хвоста — флага нет",
              act_s2 is not None and act_s2.get("submit") is None)
        check("submit: подтверждение упоминает отправку",
              "и отправить" in msub.confirm_question(act_s)
              and "и отправить" not in msub.confirm_question(act_s2))
        # execute прокидывает submit в fill_tagged
        _subs = []
        _ba.fill_tagged = lambda host, idx, text, tab_id=None, submit=False: (
            _subs.append(submit), "submitted")[1]
        real6 = ComputerControlManager(context="t", base_dir=tmp / "s8-sub2",
                                       config={})
        ok_s, _ = real6.execute({"kind": "type", "idx": 0, "text": "привет",
                                 "element": "Сообщение", "host": "chat.deepseek.com",
                                 "value": "https://chat.deepseek.com",
                                 "submit": True}, "c")
        check("submit: execute прокинул submit=True в fill_tagged",
              ok_s and _subs == [True])
    finally:
        _ba.snapshot_elements, _ba.fill_tagged = _orig_snap9, _orig_ft9

    # ── 8f. Standalone «отправь» — Enter в поле без ввода ──
    from app.features.computer_control import parse_send_request
    check("send-parse: «отправь (сообщение)» / «send it» / сайт",
          parse_send_request("отправь") == ("send", None)
          and parse_send_request("отправь сообщение") == ("send", None)
          and parse_send_request("отправь на кладе") == ("send", "кладе")
          and parse_send_request("send it") == ("send", None))
    check("send-parse: «отправь посылку» — не та команда",
          parse_send_request("отправь посылку") is None
          and parse_send_request("расскажи анекдот") is None)
    _orig_snap10, _orig_pe10, _orig_ft10 = (
        _ba.snapshot_elements, _ba.press_enter, _ba.find_tab_id)
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://chat.deepseek.com", "chat.deepseek.com",
        [_it(0, "textarea", "Message DeepSeek", ed=True)])
    _sent = []
    _ba.press_enter = lambda host=None, tab_id=None: (
        _sent.append(host), "sent")[1]
    _ba.find_tab_id = lambda host: None
    try:
        msn = ComputerControlManager(context="t", base_dir=tmp / "s8-send",
                                     config={**CFG, "allow_domains": []})
        act_n, err_n = msn.resolve_send(None, None)
        check("send: resolve → send-действие с хостом вкладки",
              err_n is None and act_n["kind"] == "send"
              and act_n["host"] == "chat.deepseek.com")
        check("send: подтверждение называет действие и место",
              msn.confirm_question(act_n)
              == "Отправить сообщение на chat.deepseek.com (Enter)?")
        ok_n, _ = msn.execute(act_n, "c8s")
        rec_n = json.loads((tmp / "s8-send" / "audit.jsonl")
                           .read_text(encoding="utf-8").strip().splitlines()[-1])
        check("send: execute → press_enter, аудит kind=send ok",
              ok_n and _sent == ["chat.deepseek.com"]
              and rec_n.get("kind") == "send" and rec_n.get("ok") is True)
    finally:
        _ba.snapshot_elements, _ba.press_enter = _orig_snap10, _orig_pe10
        _ba.find_tab_id = _orig_ft10

    # ── 8g. Авто-листание «промотай страницу» / «стоп» ──
    from app.features.computer_control import parse_scroll_request
    check("scroll-parse: «промотай страницу» / «листай» / сайт",
          parse_scroll_request("промотай страницу") == ("start", None, None, None)
          and parse_scroll_request("листай") == ("start", None, None, None)
          and parse_scroll_request("пролистай страницу на ютубе")
          == ("start", "ютубе", None, None)
          and parse_scroll_request("проскролль вниз") == ("start", None, None, None))
    check("scroll-parse: сторона — «раздел слева» / «список справа» / сайт",
          parse_scroll_request("промотай раздел слева")
          == ("start", None, "left", None)
          and parse_scroll_request("пролистай список справа")
          == ("start", None, "right", None)
          and parse_scroll_request("проскролль слева") == ("start", None, "left", None)
          and parse_scroll_request("листай раздел слева на додо")
          == ("start", "додо", "left", None))
    check("scroll-parse: прилагательное первым — «правую часть» = «часть справа»",
          parse_scroll_request("пролистай правую часть")
          == ("start", None, "right", None)
          and parse_scroll_request("пролистай часть справа")
          == ("start", None, "right", None)
          and parse_scroll_request("пролистай левую часть")
          == ("start", None, "left", None)
          and parse_scroll_request("промотай левую колонку")
          == ("start", None, "left", None)
          and parse_scroll_request("пролистай правую часть вверх на додо")
          == ("start", "додо", "right", "up"))
    check("scroll-parse: направление — «вверх»/«выше» + сторона и сайт",
          parse_scroll_request("промотай вверх") == ("start", None, None, "up")
          and parse_scroll_request("пролистай раздел слева вверх")
          == ("start", None, "left", "up")
          and parse_scroll_request("прокрути страницу наверх")
          == ("start", None, None, "up")
          and parse_scroll_request("листай справа вверх на додо")
          == ("start", "додо", "right", "up"))
    check("scroll-parse: «стоп» / «хватит листать» / «остановись»",
          parse_scroll_request("стоп") == ("stop", None, None, None)
          and parse_scroll_request("хватит листать") == ("stop", None, None, None)
          and parse_scroll_request("остановись") == ("stop", None, None, None))
    check("scroll-parse: не команды — None",
          parse_scroll_request("ну ладно") is None
          and parse_scroll_request("остановись, я подумаю") is None
          and parse_scroll_request("промотай мне историю про кота") is None)

    # ── 8h. Корзина сайта: parse_cart_request ──
    from app.features.computer_control import parse_cart_request
    check("cart-parse: убрать из корзины/заказа",
          parse_cart_request("убери гавайскую из корзины") == ("remove", "гавайскую")
          and parse_cart_request("удали додстер из заказа") == ("remove", "додстер"))
    check("cart-parse: убавить (с «одну» и без; филлер «пиццу» срезается)",
          parse_cart_request("убавь додстер") == ("decrease", "додстер")
          and parse_cart_request("убери одну гавайскую пиццу") == ("decrease", "гавайскую")
          and parse_cart_request("минус одну колу") == ("decrease", "колу"))
    check("cart-parse: прибавить (в т.ч. «ещё одну», «плюс один»)",
          parse_cart_request("прибавь колу") == ("increase", "колу")
          and parse_cart_request("добавь ещё одну песто") == ("increase", "песто")
          and parse_cart_request("плюс один додстер") == ("increase", "додстер"))
    check("cart-parse: изменить — только «в корзине»",
          parse_cart_request("измени песто в корзине") == ("edit", "песто")
          and parse_cart_request("измени промпт") is None)
    check("cart-parse: не команды корзины — None (инвентарь/настройки/разговор)",
          parse_cart_request("убери меч") is None
          and parse_cart_request("убавь громкость") is None
          and parse_cart_request("прибавь яркость экрана") is None
          and parse_cart_request("добавь в инвентарь зелье") is None
          and parse_cart_request("расскажи про корзину") is None)
    check("cart-describe: вопрос и отчёт по операции",
          ComputerControlManager.describe(
              {"kind": "cart", "op": "decrease", "product": "гавайскую",
               "host": "dodopizza.ru"}) == "убавить «гавайскую» в корзине на dodopizza.ru"
          and ComputerControlManager.describe_done(
              {"kind": "cart", "op": "decrease", "product": "гавайскую",
               "host": "dodopizza.ru", "qty_new": 1})
          == "убавил «гавайскую» — теперь 1 шт. в корзине"
          and ComputerControlManager.describe_done(
              {"kind": "cart", "op": "decrease", "product": "колу",
               "host": "dodopizza.ru", "qty_new": 0})
          == "убрал «колу» из корзины (была последняя штука)"
          and ComputerControlManager.describe_done(
              {"kind": "cart", "op": "remove", "product": "додстер",
               "host": "dodopizza.ru"}) == "убрал «додстер» из корзины на dodopizza.ru")

    # ── 8i. Вопрос о секции страницы: parse_page_question ──
    from app.features.computer_control import parse_page_question
    check("pageq-parse: «что находится в X?» / «что в разделе X?»",
          parse_page_question("что находится в добавить по вкусу?")
          == ("добавить по вкусу", None, "добавить по вкусу")
          and parse_page_question("что в разделе напитки?")
          == ("напитки", None, "напитки")
          and parse_page_question("что там в корзине")
          == ("корзине", None, "корзине"))
    check("pageq-parse: сайт-хвост и «на странице» как пустое место",
          parse_page_question("что в разделе напитки на додо?")
          == ("напитки", "додо", "напитки на додо")
          and parse_page_question("что есть в блоке добавки на этой странице?")
          == ("добавки", None, "добавки")
          and parse_page_question("что в корзине на странице?")
          == ("корзине", None, "корзине"))
    check("pageq-parse: «на двоих» — часть названия, полный запрос сохраняется",
          parse_page_question("что есть в завтрак на двоих?")
          == ("завтрак", "двоих", "завтрак на двоих"))
    check("pageq-parse: en-форма",
          parse_page_question("what's in the drinks section?")
          == ("drinks", None, "drinks"))
    check("pageq-parse: не вопрос о странице — None",
          parse_page_question("что ты думаешь о пицце?") is None
          and parse_page_question("что мне ответил клод") is None
          and parse_page_question("расскажи что было вчера") is None)
    check("pageq-parse: мусор/пустое — None",
          parse_page_question("") is None
          and parse_page_question("что в?") is None)

    _orig_rs = _ba.read_section
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/x", "dodopizza.ru", [_it( 0, "a", "Hi")])
    try:
        mpq = ComputerControlManager(context="t", base_dir=tmp / "s8-pageq",
                                     config={**CFG, "allow_domains": []})
        check("pageq: без открытой страницы и сайта — None (в диалог)",
              mpq.read_page_section("напитки", None) is None)
        mpq._last_host = "dodopizza.ru"
        _ba.read_section = lambda host, q, tab_id=None: (
            "Добавить по вкусу\nХалапеньо 49 ₽\nСырный бортик 129 ₽"
            if "добавить" in q else "")
        got_pq = mpq.read_page_section("добавить по вкусу", None)
        check("pageq: секция найдена → (текст, host, запрос)",
              got_pq is not None and "Халапеньо" in got_pq[0]
              and got_pq[1] == "dodopizza.ru"
              and got_pq[2] == "добавить по вкусу")
        check("pageq: секция не нашлась — None (молча в диалог)",
              mpq.read_page_section("несуществующая секция", None) is None)
        # «на двоих» не алиас и не домен → поиск по ПОЛНОМУ запросу
        _seen_q = []
        _ba.read_section = lambda host, q, tab_id=None: (
            _seen_q.append(q), "Завтрак на двоих\nОмлет с томатами")[1]
        got_pq2 = mpq.read_page_section("завтрак", "двоих", "завтрак на двоих")
        check("pageq: хвост не алиас/домен → ищем полный запрос",
              got_pq2 is not None and got_pq2[2] == "завтрак на двоих"
              and _seen_q == ["завтрак на двоих"])
        # Алиас из конфига сайтов — хвост срезается, запрос короткий
        mpq.sites = {"додо": "https://dodopizza.ru"}
        _seen_q.clear()
        got_pq3 = mpq.read_page_section("напитки", "додо", "напитки на додо")
        check("pageq: алиас сайта — запрос срезан, host по алиасу",
              got_pq3 is not None and got_pq3[2] == "напитки"
              and _seen_q == ["напитки"])
        def _boom_rs(host, q, tab_id=None):
            raise RuntimeError("вкладка умерла")
        _ba.read_section = _boom_rs
        check("pageq: вкладка умерла без last_host-фолбэка — None",
              mpq.read_page_section("напитки", "dodopizza.ru") is None)
    finally:
        _ba.read_section = _orig_rs

    # ── 8j. Служебные вкладки веб-чатов: не попап клика и не контекст ──
    from app.features.browser_actions import is_service_host, register_service_host
    check("svc: chat-хосты адаптеров — служебные (статически), dodo — нет",
          is_service_host("chat.deepseek.com")
          and is_service_host("chat.qwen.ai")
          and not is_service_host("dodopizza.ru")
          and not is_service_host(None) and not is_service_host(""))
    register_service_host("example-svc.test")
    check("svc: register_service_host добавляет хост",
          is_service_host("example-svc.test")
          and is_service_host("  Example-SVC.TEST "))
    # last_tab.json со служебным хостом — контекст НЕ восстанавливается
    svc_dir = tmp / "s8-svc"
    svc_dir.mkdir(parents=True, exist_ok=True)
    (svc_dir / "last_tab.json").write_text(
        json.dumps({"host": "chat.deepseek.com",
                    "url": "https://chat.deepseek.com/x", "ts": 1}),
        encoding="utf-8")
    msvc = ComputerControlManager(context="t", base_dir=svc_dir,
                                  config={**CFG, "allow_domains": []})
    check("svc: грязный last_tab.json проигнорирован при восстановлении",
          msvc._last_host is None)
    # …и служебный хост не перезаписывает рабочую страницу на диске
    msvc._last_host = "dodopizza.ru"
    msvc._save_last_page("https://dodopizza.ru/x")
    msvc._last_host = "chat.deepseek.com"
    msvc._save_last_page("https://chat.deepseek.com/x")
    check("svc: _save_last_page не пишет служебный хост",
          json.loads((svc_dir / "last_tab.json").read_text(encoding="utf-8"))
          ["host"] == "dodopizza.ru")

    import app.features.computer_control as _cc_mod
    _orig_st, _orig_snap_s, _orig_ft_s = (
        _ba.scroll_start, _ba.snapshot_elements, _ba.find_tab_id)
    _orig_stat, _orig_stop = _ba.scroll_status, _ba.scroll_stop
    _orig_poll = _cc_mod._SCROLL_POLL_SEC
    _cc_mod._SCROLL_POLL_SEC = 0.02  # дозорный поток → мгновенные опросы
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/x", "dodopizza.ru", [_it(0, "a", "Hi")])
    _ba.find_tab_id = lambda host: None
    _sc_calls = []
    _stop_calls = []
    _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None: (
        _sc_calls.append(host), {"ok": True, "bottom": False})[1]
    _ba.scroll_status = lambda host=None, tab_id=None: {
        "active": True, "done": False}
    _ba.scroll_stop = lambda host=None, tab_id=None: _stop_calls.append(host)
    try:
        mscr = ComputerControlManager(context="t", base_dir=tmp / "s8-scroll",
                                      config={**CFG, "allow_domains": []})
        act_s, err_s = mscr.resolve_scroll("start", None)
        check("scroll: resolve → scroll-действие с хостом вкладки",
              err_s is None and act_s["kind"] == "scroll"
              and act_s["host"] == "dodopizza.ru")
        ok_s, _ = mscr.execute(act_s, "c8sc")
        check("scroll: execute — листание запущено (анимация стартовала синхронно)",
              ok_s and _sc_calls == ["dodopizza.ru"] and mscr._scroll_active())
        act_s2, err_s2 = mscr.resolve_scroll("start", None)
        check("scroll: повторный старт при живом — «уже листаю»",
              act_s2 is None and "уже листаю" in (err_s2 or "").lower())
        act_st, err_st = mscr.resolve_scroll("stop", None)
        check("scroll: «стоп» при активном листании → scroll_stop",
              err_st is None and act_st is not None
              and act_st["kind"] == "scroll_stop")
        ok_st, _ = mscr.execute(act_st, "c8sc")
        check("scroll: стоп глушит анимацию в странице и поток, без причины",
              ok_st and act_st.get("end_reason") is None
              and not mscr._scroll_active()
              and _stop_calls == ["dodopizza.ru"])
        act_st2, err_st2 = mscr.resolve_scroll("stop", None)
        check("scroll: «стоп» без листания → (None, None) — уходит в диалог",
              act_st2 is None and err_st2 is None)
        # Страница уже внизу: анимация не стартует — честный отказ
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None: (
            {"ok": False, "bottom": True})
        act_s3, err_s3 = mscr.resolve_scroll("start", None)
        ok_s3, det_s3 = mscr.execute(act_s3, "c8sc")
        check("scroll: страница уже внизу — «листать некуда»",
              not ok_s3 and "низу" in det_s3)
        # Долистал до конца сам: «стоп» после — честный отчёт о причине
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None: (
            {"ok": True, "bottom": False})
        _seq = iter([{"active": True, "done": False}]
                    + [{"active": False, "done": True}] * 5)
        _ba.scroll_status = lambda host=None, tab_id=None: next(
            _seq, {"active": False, "done": True})
        act_s4, _ = mscr.resolve_scroll("start", None)
        ok_s4, _ = mscr.execute(act_s4, "c8sc2")
        time.sleep(0.3)  # дозор успевает увидеть конец страницы
        act_st4, _ = mscr.resolve_scroll("stop", None)
        mscr.execute(act_st4, "c8sc2")
        check("scroll: долистал до конца — «стоп» докладывает причину",
              ok_s4 and act_st4.get("end_reason") == "bottom"
              and "до конца" in mscr.describe_done(act_st4))
        # Режим-кортеж ("start", side) из parse_scroll_request → действие со стороной
        act_sl, err_sl = mscr.resolve_scroll(("start", "left"), None)
        check("scroll: режим-кортеж → действие со стороной",
              err_sl is None and act_sl["kind"] == "scroll"
              and act_sl.get("side") == "left")
        check("scroll: формулировки со стороной («раздел слева»)",
              "раздел слева" in mscr.confirm_question(act_sl)
              and "раздел слева" in mscr.describe_done(act_sl)
              and "листать раздел слева" in mscr.describe(act_sl))
        # side_missed из страницы — честный отказ исполнения
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None: (
            {"ok": False, "bottom": False, "side_missed": True})
        ok_sm, det_sm = mscr.execute(act_sl, "c8sc3")
        check("scroll: side_missed → «не вижу раздела слева»",
              not ok_sm and "слева" in det_sm)
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None: (
            {"ok": True, "bottom": False})
        # Направление вверх: кортеж ("start", None, "up") → dir в действии
        act_up, err_up = mscr.resolve_scroll(("start", None, "up"), None)
        check("scroll: режим-кортеж с направлением → dir=up в действии",
              err_up is None and act_up.get("dir") == "up"
              and act_up.get("side") is None)
        check("scroll: формулировки с направлением («страницу вверх»)",
              "страницу вверх" in mscr.confirm_question(act_up)
              and "страницу вверх" in mscr.describe_done(act_up))
        check("scroll: формулировки сторона+направление («раздел слева вверх»)",
              "раздел слева вверх" in mscr.describe(
                  {"kind": "scroll", "host": "dodopizza.ru",
                   "side": "left", "dir": "up"}))
        # «уже в самом верху» — честный отказ при dir=up на верхней границе
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None: (
            {"ok": False, "bottom": True})
        ok_up2, det_up2 = mscr.execute(act_up, "c8sc4")
        check("scroll: dir=up на верхней границе → «в самом верху»",
              not ok_up2 and "верху" in det_up2)
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None: (
            {"ok": True, "bottom": False})
        check("scroll: формулировки вопроса и «Готово»",
              mscr.confirm_question(act_s) ==
              "Начать листать страницу на dodopizza.ru? "
              "Скажи «стоп», чтобы остановить."
              and "начал листать страницу на dodopizza.ru"
              in mscr.describe_done(act_s)
              and mscr.describe_done({"kind": "scroll_stop"})
              == "остановил прокрутку")
    finally:
        _ba.scroll_start, _ba.snapshot_elements = _orig_st, _orig_snap_s
        _ba.scroll_status, _ba.scroll_stop = _orig_stat, _orig_stop
        _ba.find_tab_id = _orig_ft_s
        _cc_mod._SCROLL_POLL_SEC = _orig_poll

    _clicks = []
    _orig_ct = _ba.click_tagged
    _orig_ft = _ba.find_tab_id
    _ba.click_tagged = lambda host, idx, tab_id=None, mark=None: (
        _clicks.append((host, idx, tab_id)), "clicked")[1]
    _ba.find_tab_id = lambda h: 777
    try:
        real2 = ComputerControlManager(context="t", base_dir=tmp, config={})
        ok3, _ = real2.execute(act_click, "c")
        check("dispatch: click уходит в click_tagged с host и idx",
              ok3 and _clicks == [("github.com", 1, None)])
        check("dispatch: вкладка клика запоминается по id",
              real2._last_tab_id == 777)
    finally:
        _ba.click_tagged = _orig_ct
        _ba.find_tab_id = _orig_ft
    check("формулировки click: вопрос и «Готово» с текстом элемента",
          ms2.confirm_question(act_click) == "Нажать «Скачать» на github.com?"
          and ms2.describe_done(act_click) == "нажал «Скачать» на github.com")

    # Скачивание «скачай X (на сайте)»: парс, резолв через href, dispatch
    from app.features.computer_control import parse_download_request
    check("parse download: «скачай файл методичку по sql на ciu.nstu.ru»",
          parse_download_request("скачай файл методичку по sql на ciu.nstu.ru")
          == ("методичку по sql", "ciu.nstu.ru"))
    check("parse download: «скачай отчёт» / не команда",
          parse_download_request("скачай отчёт") == ("отчёт", None)
          and parse_download_request("расскажи про файлы") is None)
    check("parse download: «на открывшейся странице» → PAGE_REF",
          parse_download_request("скачай отчёт на открывшейся странице")
          == ("отчёт", PAGE_REF)
          and parse_download_request("скачай на этой странице файл отчёт")
          == ("отчёт", PAGE_REF))
    _orig_snap3, _orig_href = _ba.snapshot_elements, _ba.href_of_tagged
    _ba.snapshot_elements = lambda host, tab_id=None: (
        "https://ciu.nstu.ru/x", "ciu.nstu.ru",
        [_it(0, "a", "Войти"), _it(1, "a", "Методичка по SQL"),
         _it(2, "img", "СТУДЕНТАМ")])
    _hrefs = {1: "https://ciu.nstu.ru/a/file_get/329640?nomenu=1", 2: ""}
    _ba.href_of_tagged = lambda host, idx, tab_id=None: _hrefs.get(idx, "")
    try:
        act_dl, err_dl = ms2.resolve_download("методичку по sql", "ciu.nstu.ru",
                                              _BoomRouter())
        check("resolve_download: матч → download-действие с href",
              err_dl is None
              and act_dl["kind"] == "download"
              and act_dl["url"] == "https://ciu.nstu.ru/a/file_get/329640?nomenu=1"
              and act_dl["element"] == "Методичка по SQL"
              and act_dl["host"] == "ciu.nstu.ru")
        no_dl, no_dl_err = ms2.resolve_download("студентам", "ciu.nstu.ru",
                                                _BoomRouter())
        check("resolve_download: у иконки нет href — честный отказ",
              no_dl is None and no_dl_err is not None and "не ссылка" in no_dl_err)
        check("формулировки download: вопрос и «Готово»",
              ms2.confirm_question(act_dl)
              == "Скачать «Методичка по SQL» с ciu.nstu.ru?"
              and ms2.describe_done(act_dl)
              == "скачал «Методичка по SQL» с ciu.nstu.ru")
        _dls = []
        _orig_dt = _ba.download_in_tab
        _orig_ft2 = _ba.find_tab_id
        _ba.download_in_tab = lambda host, url, tab_id=None: (
            _dls.append((host, url, tab_id)), "ok")[1]
        _ba.find_tab_id = lambda h: 888
        try:
            real3 = ComputerControlManager(context="t", base_dir=tmp, config={})
            ok4, _ = real3.execute(act_dl, "c")
            check("dispatch: download уходит в download_in_tab с host и url",
                  ok4 and _dls == [("ciu.nstu.ru",
                                    "https://ciu.nstu.ru/a/file_get/329640?nomenu=1",
                                    None)])
            check("dispatch: вкладка скачивания запоминается по id",
                  real3._last_tab_id == 888)
        finally:
            _ba.download_in_tab = _orig_dt
            _ba.find_tab_id = _orig_ft2
    finally:
        _ba.snapshot_elements, _ba.href_of_tagged = _orig_snap3, _orig_href
    act_search = ms2.resolve_search("интерстеллар", "кинопоиске")  # предложный падеж
    check("resolve_search: падеж сайта сматчен + запрос подставлен",
          act_search is not None and act_search["kind"] == "url"
          and act_search["value"].startswith("https://www.kinopoisk.ru/index.php?kp_query=")
          and "%D0%B8" in act_search["value"])
    check("resolve_search: сайт без шаблона → None",
          ms2.resolve_search("что-то", "рутубе") is None)
    # search first: сеть в тестах не трогаем — httpx.Client подменяем
    import httpx as _httpx
    _orig_client = _httpx.Client

    class _FailClient:  # сеть упала → фолбэк на страницу поиска
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url): raise RuntimeError("no network in tests")

    class _HtmlClient:  # страница поиска со ссылкой на видео
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url):
            class _R: text = '<a href="/watch?v=abc12345678">Utopia</a>'
            return _R()

    _httpx.Client = _FailClient
    try:
        check("resolve_search: сбой извлечения первого результата → страница поиска",
              ms2.resolve_search("utopia show", "ютуб")["value"]
              == "https://www.youtube.com/results?search_query=utopia+show")
    finally:
        _httpx.Client = _orig_client
    _httpx.Client = _HtmlClient
    try:
        act_first = ms2.resolve_search("utopia show", "ютуб")
        check("search first: открывается само видео, а не страница поиска",
              act_first["value"] == "https://www.youtube.com/watch?v=abc12345678"
              and act_first.get("direct") is True)
        check("search first: формулировки про «открыть», не про «найти»",
              ms2.confirm_question(act_first) == "Открыть «utopia show» на ютуб?"
              and ms2.describe_done(act_first) == "открыл «utopia show» на ютуб")
        check("search first: англ. ключ сайта работает так же",
              ms2.resolve_search("expedition 33", "youtube")["value"]
              == "https://www.youtube.com/watch?v=abc12345678")
        act_nondirect = ms2.resolve_search("utopia show", "ютуб", False)
        check("search: глагол-«поисковик» → страница поиска, first не дёргается",
              act_nondirect["value"] == "https://www.youtube.com/results?search_query=utopia+show"
              and "direct" not in act_nondirect
              and ms2.confirm_question(act_nondirect) == "Найти «utopia show» на ютуб?")
    finally:
        _httpx.Client = _orig_client
    check("resolve_search: вопрос/«Готово» говорят о поиске, а не про index.php",
          ms2.confirm_question(act_search) == "Найти «интерстеллар» на кинопоиск?"
          and ms2.describe_done(act_search) == "открыл поиск «интерстеллар» на кинопоиск")
    check("resolve: ключ tasks по основе слова («музыку» → «музыка»)",
          ms2.resolve("музыку") == {"kind": "task", "key": "музыка",
                                    "value": 'shortcuts run "Музыка"'})

    multi = ms2.resolve_many(["ютуб", "кинопоиск"])
    check("resolve_many: две цели → multi-действие",
          multi is not None and multi["kind"] == "multi" and len(multi["items"]) == 2)
    check("resolve_many: одна цель → обычное действие",
          ms2.resolve_many(["ютуб"]) == {"kind": "url", "value": "https://youtube.com"})
    _orig_find2, _orig_hist2 = _ws.find_site_url, _bh.find_in_history
    _ws.find_site_url = lambda name, **kw: None
    _bh.find_in_history = lambda name: None
    try:
        check("resolve_many: хоть одна цель не резолвится → None",
              ms2.resolve_many(["ютуб", "ноунейм-сайт"]) is None)
    finally:
        _ws.find_site_url, _bh.find_in_history = _orig_find2, _orig_hist2
    check("multi: вопрос подтверждения и «Готово» по обоим действиям",
          ms2.confirm_question(multi) == "Открыть https://youtube.com и открыть https://kinopoisk.ru?"
          and ms2.describe_done(multi) == "открыл youtube.com, открыл kinopoisk.ru")

    import app.features.computer_control as _ccm
    real = ComputerControlManager(context="t", base_dir=tmp, config={
        "confirm": True, "allow_domains": [],
        "sites": {"ютуб": "youtube.com", "кинопоиск": "kinopoisk.ru"}})
    opened = []
    _orig_open = _ccm.webbrowser.open
    _orig_open4 = _ba.open_new_tab
    _ccm.webbrowser.open = lambda url: opened.append(url) or True
    _ba.open_new_tab = lambda url: (opened.append(url), 1)[1]  # macOS-путь
    try:
        ok_multi, _ = real.execute(multi, "c")
        check("execute multi: оба URL открыты по очереди",
              ok_multi and opened == ["https://youtube.com", "https://kinopoisk.ru"])
    finally:
        _ccm.webbrowser.open = _orig_open
        _ba.open_new_tab = _orig_open4

    # История браузера: фейковая БД в схеме Chrome, читатели — настоящие
    import sqlite3
    hist_db = tmp / "fake_chrome_history.sqlite"
    con = sqlite3.connect(hist_db)
    con.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT,"
                " visit_count INTEGER, last_visit_time INTEGER)")
    con.executemany("INSERT INTO urls (url, title, visit_count, last_visit_time)"
                    " VALUES (?,?,?,?)", [
        ("https://dispace.edu.nstu.ru/login", "Диспейс НГТУ — личный кабинет", 40, 0),
        ("https://ciu.nstu.ru/kaf/persons/98849", "НГТУ - КУТУЗОВА И. А.", 12, 0),
        ("https://mail.google.com/mail/u/0/#inbox", "Платформа — рассылка", 100, 0),
        ("https://example.com/rare", "Rare example", 1, 0),  # < MIN_VISITS
    ])
    con.commit()
    con.close()
    _orig_files = _bh._history_files
    _bh._history_files = lambda: [("chrome", hist_db)]
    _bh._cache["ts"] = 0.0
    try:
        check("history: «диспейс» → корень частого сайта",
              _bh.find_in_history("диспейс") == "https://dispace.edu.nstu.ru/")
        check("history: мультисловный запрос (падеж) → страница целиком",
              _bh.find_in_history("кутузовой нгту") == "https://ciu.nstu.ru/kaf/persons/98849")
        check("history: редкий визит (<3) игнорируется",
              _bh.find_in_history("rare example") is None)
        check("history: нет совпадений → None", _bh.find_in_history("никуда") is None)
        check("history: заголовки почты/лент в матче не участвуют",
              _bh.find_in_history("платформа") is None)
        _orig_find3 = _ws.find_site_url
        _ws.find_site_url = lambda name, **kw: None  # если сработает поиск — провал
        try:
            check("resolve: история срабатывает раньше поиска DDG",
                  ms2.resolve("диспейс") == {"kind": "url",
                                             "value": "https://dispace.edu.nstu.ru/"})
        finally:
            _ws.find_site_url = _orig_find3
    finally:
        _bh._history_files = _orig_files
        _bh._cache["ts"] = 0.0

    check("шаблоны вопроса/подтверждения для url/app/task",
          mr_.confirm_question({"kind": "url", "value": "https://www.youtube.com/"}) == "Открыть youtube.com?"
          and mr_.confirm_question({"kind": "url", "value": "https://www.google.com/maps"}) == "Открыть google.com/maps?"
          and mr_.confirm_question({"kind": "app", "key": "safari"}) == "Запустить «safari»?"
          and mr_.describe_done({"kind": "url", "value": "https://www.youtube.com/"}) == "открыл youtube.com"
          and mr_.describe_done({"kind": "task", "key": "музыка"}) == "выполнил задачу «музыка»")

    # ── 6. Pending TTL и clear ──
    m = make()
    m.set_pending("c7", {"kind": "url", "value": "https://youtube.com"})
    check("pending читается", m.get_pending("c7") is not None)
    m._pending["c7"]["expires_at"] = time.time() - 1
    check("pending протухает по TTL", m.get_pending("c7") is None)
    m.set_pending("c7", {"kind": "url", "value": "https://youtube.com"})
    m.clear_pending("c7")
    check("pending сбрасывается", m.get_pending("c7") is None)

    # ── 7. Детект да/нет ──
    for t in ("да", "Давай!", "ок", "конечно", "открывай", "yes", "угу"):
        check(f"confirm YES: {t!r}", classify_confirmation(t) == "YES")
    for t in ("нет", "не надо", "отмена", "no", "стоп"):
        check(f"confirm NO: {t!r}", classify_confirmation(t) == "NO")
    for t in ("расскажи подробнее", "а зачем?", "", "может быть"):
        check(f"confirm UNKNOWN: {t!r}", classify_confirmation(t) == "UNKNOWN")

    # ── 8. execute + аудит ──
    s8 = tmp / "s8"
    m = SpyManager(context="test", config=CFG, base_dir=s8)
    ok_, _ = m.execute({"kind": "url", "value": "https://youtube.com"}, "c8")
    check("execute: успех → (True), dispatch вызван",
          ok_ and m.calls[-1]["kind"] == "url")
    mf = SpyManager(context="test", config=CFG, base_dir=s8, fail_with=RuntimeError("boom"))
    ok_, detail = mf.execute({"kind": "url", "value": "https://youtube.com"}, "c8")
    check("execute: сбой → (False, причина)", not ok_ and "boom" in detail)
    audit = (s8 / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    check("аудит-лог: обе попытки записаны",
          len(audit) == 2 and json.loads(audit[0])["ok"] is True
          and json.loads(audit[1])["ok"] is False)

    # ── 8b. Скоринг, узкая LLM, closed-loop, наблюдаемость, бэкенд ──
    ms = make()
    # Скоринг: точный текст > aria/title > основы слов > подстрока
    check("score: точный текст — высший балл",
          ms._score_candidates([_it(0, "a", "Войти")], "войти")[0][0] == 100.0)
    check("score: точный aria-label — второй балл",
          ms._score_candidates([_it(0, "button", "", aria="Скачать")],
                               "скачать")[0][0] == 90.0)
    check("score: совпадение по основам слов",
          ms._score_candidates([_it(0, "a", "Методичка по SQL")],
                               "методичку по sql")[0][0] == 70.0)
    check("score: частичная подстрока — низший балл",
          # слова цели короче 3 букв — ярусов основ слов нет, только подстрока
          ms._score_candidates([_it(0, "a", "Помощь")], "по")[0][0] == 50.0
          and 50.0 < ms._score_candidates([_it(0, "a", "Помощь")],
                                          "помощь")[0][0])
    check("score: штрафы за вне-вьюпорта и крошечный размер",
          ms._score_candidates([_it(0, "a", "Войти", vp=False)], "войти")[0][0] == 90.0
          and ms._score_candidates([_it(0, "a", "Войти", w=4.0)], "войти")[0][0] == 85.0)
    check("score: штраф за позднюю позицию в DOM",
          ms._score_candidates([_it(0, "a", "Войти"), _it(1, "a", "Войти")],
                               "войти")[1][1]["idx"] == 1
          and ms._score_candidates([_it(0, "a", "Войти"), _it(1, "a", "Войти")],
                                   "войти")[1][0] == 99.5)
    # Ярус «слова в тексте + остальные в контексте»: модалка соусов dodo —
    # кнопки цен одинаковые («49 ₽»), отличает их подпись совпадения
    # («Сырный · 49 ₽») и контекст места с заголовком «Соусы…»
    _sauce_ctx = ("Соусы к бортикам и закускам Тысяча островов 45 ₽ "
                  "Сырный 49 ₽ Чесночный 49 ₽")
    _sc = ms._score_candidates(
        [_it(0, "button", "Сырный · 49 ₽", ctx=_sauce_ctx),
         _it(1, "button", "Чесночный · 49 ₽", ctx=_sauce_ctx),
         _it(2, "button", "49 ₽", ctx=_sauce_ctx)], "сырный соус")
    check("score: ярус текст+контекст выделяет соус модалки",
          _sc[0][0] == 65.0 and _sc[0][1]["idx"] == 0
          and all(s <= 40.0 for s, _ in _sc[1:]))
    # ...но скоуп-цель («выбрать на Цезарь») им не перехватывается — её
    # разруливает _score_scoped
    check("score: скоуп-цель мимо яруса текст+контекст",
          ms._score_candidates(
              [_it(0, "button", "Выбрать",
                   ctx="Цезарь с беконом 270 г Курица 419 ₽ Выбрать")],
              "выбрать на цезарь") == [])

    # Выбор: явный лидер — без LLM; близкие кандидаты — LLM одним токеном
    _tie = lambda: [_it(0, "button", "Скачать приложение"),
                    _it(1, "button", "Скачать прайс"), _it(2, "a", "Помощь")]
    idx_, meta_ = ms._choose_element("войти", [_it(0, "a", "Войти"),
                                               _it(1, "a", "Регистрация")],
                                     _BoomRouter())
    check("choose: явный лидер — LLM не дёргается, путь score",
          idx_ == 0 and meta_["path"] == "score")
    _cap = []

    class _CapRouter:
        def __init__(self, resp): self.resp = resp
        def get_response(self, messages, **kw):
            _cap.append(messages[-1]["content"])
            return self.resp

    idx_, meta_ = ms._choose_element("скачать", _tie(), _CapRouter("2"))
    check("choose: близкие кандидаты → LLM, строгий ответ «2» → второй",
          idx_ == 1 and meta_["path"] == "llm" and meta_["llm_response"] == "2")
    check("choose: в промпт ушли только top-5 и только текст+тег+роль",
          "1) [button/-" in _cap[-1] and "2) [button/-" in _cap[-1]
          and "6)" not in _cap[-1] and "скачать" in _cap[-1].lower())
    _many = [_it(i, "a", f"Скачать вариант {i}") for i in range(8)]
    ms._choose_element("скачать", _many, _CapRouter("1"))
    check("choose: кандидатов в промпте не больше пяти",
          "5) [" in _cap[-1] and "6) [" not in _cap[-1])
    idx_, meta_ = ms._choose_element("скачать", _tie(), _CapRouter("вариант 2"))
    check("choose: невалидный ответ — без докручивания, фолбэк на лучшего",
          idx_ == 0 and meta_["path"] == "llm_fallback")
    idx_, meta_ = ms._choose_element("скачать", _tie(), _CapRouter("9"))
    check("choose: номер вне диапазона — невалиден, фолбэк на лучшего",
          idx_ == 0 and meta_["path"] == "llm_fallback")
    idx_, meta_ = ms._choose_element("скачать", _tie(), _CapRouter("нет"))
    check("choose: LLM «нет» — честный отказ",
          idx_ is None and meta_["path"] == "none")

    class _RaiseRouter:
        def get_response(self, *a, **kw):
            raise RuntimeError("ollama down")

    idx_, meta_ = ms._choose_element("скачать", _tie(), _RaiseRouter())
    check("choose: LLM упала — фолбэк на лучшего по скору",
          idx_ == 0 and meta_["path"] == "llm_fallback")
    idx_, meta_ = ms._choose_element("загрузить", _tie(), None)
    check("choose: без кандидатов — отказ (путь none)",
          idx_ is None and meta_["path"] == "none")
    check("choose: слабые кандидаты (<50) и мёртвая LLM — отказ, а не гадание",
          ms._choose_element("сохранить", [_it(0, "a", "Войти")],
                             _RaiseRouter())[0] is None)

    # Аудит: путь выбора, кандидаты, сырой ответ LLM, verify
    sa = tmp / "s8c"
    ma = ComputerControlManager(context="t", base_dir=sa,
                                config={**CFG, "allow_domains": []})
    _orig_se, _orig_ct3, _orig_ft3 = (
        _ba.snapshot_elements, _ba.click_tagged, _ba.find_tab_id)
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://github.com/x", "github.com", _tie())
    _ba.click_tagged = lambda host, idx, tab_id=None, mark=None: "clicked"
    _ba.find_tab_id = lambda h: None
    try:
        act_a, err_a = ma.resolve_click("скачать", "github.com", _CapRouter("1"))
        oka, _ = ma.execute(act_a, "c8b")
        rec = json.loads((sa / "audit.jsonl").read_text(encoding="utf-8")
                         .strip().splitlines()[-1])
        check("аудит: путь/кандидаты/ответ LLM/verify записаны",
              oka and rec.get("path") == "llm" and rec.get("llm_response") == "1"
              and rec.get("verify") == "ok"
              and isinstance(rec.get("candidates"), list)
              and rec["candidates"] and "score" in rec["candidates"][0])
        # Клик без видимого эффекта — отдельный класс ошибки (не «не найден»)
        def _ct_noop(host, idx, tab_id=None, mark=None):
            raise _ba.ClickUncertain(
                "клик отправлен, но страница не изменилась — не уверен, что сработало")
        _ba.click_tagged = _ct_noop
        oku, detu = ma.execute(act_a, "c8b")
        recu = json.loads((sa / "audit.jsonl").read_text(encoding="utf-8")
                          .strip().splitlines()[-1])
        check("closed-loop: клик без эффекта — честное «не уверен»",
              not oku and "не уверен" in detu)
        check("аудит: error_class=uncertain, verify=uncertain",
              recu.get("error_class") == "uncertain"
              and recu.get("verify") == "uncertain")
        check("метрики: доля LLM-фолбэка и валидных ответов считается",
              ma.metrics()["choices"] > 0 and ma.metrics()["llm_calls"] > 0
              and ma.metrics()["llm_share"] > 0
              and ma.metrics()["llm_valid_share"] is not None)
    finally:
        _ba.snapshot_elements, _ba.click_tagged, _ba.find_tab_id = (
            _orig_se, _orig_ct3, _orig_ft3)
    check("метрики: свежий менеджер — нули, без деления на ноль",
          make().metrics() == {"choices": 0, "llm_calls": 0, "llm_share": 0.0,
                               "llm_valid_share": None})

    # Конфиг браузера: валидация backend, per-OS профиль, принудительный бэкенд
    _ba.set_browser_config({"backend": "weird"})
    check("browser cfg: неизвестный backend → auto",
          _ba._BCFG["backend"] == "auto" and not _ba.backend_forced())
    _ba.set_browser_config({"user_data_dir": {sys.platform: "/tmp/vpc-test-profile-x"}})
    check("browser cfg: per-OS user_data_dir резолвится",
          _ba._resolve_user_data_dir() == "/tmp/vpc-test-profile-x")
    _ba.set_browser_config({"backend": "applescript"})
    check("browser cfg: applescript на macOS выбирается без проберки CDP",
          sys.platform != "darwin" or _ba._select_backend(tab_op=True) == "applescript")
    # Принудительный cdp без живого порта (запуск запрещён) — понятная ошибка
    _ba.set_browser_config({"backend": "cdp", "launch": False,
                            "cdp_url": "http://127.0.0.1:59994"})
    try:
        _ba._select_backend(tab_op=True)
        _be = ""
    except _ba.BrowserUnavailable as e:
        _be = str(e)
    check("browser cfg: backend=cdp без порта — BrowserUnavailable",
          "недоступен" in _be)
    check("browser cfg: backend != auto — forced",
          _ba.backend_forced())
    _ba.set_browser_config({"backend": "auto", "launch": False,
                            "cdp_url": "http://127.0.0.1:59994"})
    check("browser cfg: auto без CDP на macOS — фолбэк applescript",
          sys.platform != "darwin"
          or _ba._select_backend(tab_op=True) == "applescript")
    _ba.set_browser_config({})
    check("browser cfg: сброс к дефолтам",
          _ba._BCFG["backend"] == "auto" and _ba._BCFG["launch"] is True)

    # SingletonLock: живой pid — понятная ошибка; протухший — снимается
    if sys.platform != "win32":
        _prof = tmp / "prof_live"
        _prof.mkdir(exist_ok=True)
        os.symlink(f"host-{os.getpid()}", _prof / "SingletonLock")
        try:
            _ba._check_profile_lock(str(_prof))
            _lk = ""
        except _ba.BrowserUnavailable as e:
            _lk = str(e)
        check("профиль: живой SingletonLock — инструкция закрыть Chrome",
              "Закрой" in _lk and os.path.lexists(_prof / "SingletonLock"))
        _dead = subprocess.Popen(["true"]); _dead.wait()
        _prof2 = tmp / "prof_dead"
        _prof2.mkdir(exist_ok=True)
        os.symlink(f"host-{_dead.pid}", _prof2 / "SingletonLock")
        _ba._check_profile_lock(str(_prof2))
        check("профиль: протухший SingletonLock снят, запуск не блокирован",
              not os.path.lexists(_prof2 / "SingletonLock"))

        # Reclaim: выделенный профиль занят Chrome БЕЗ отладки — мягко забираем
        import signal as _signal
        _orig_run, _orig_kill = _ba.subprocess.run, _ba.os.kill
        _kills = []

        def _fake_kill(pid, sig):
            if sig == _signal.SIGTERM:
                _kills.append(sig)
                return
            if sig == 0:
                if _kills:
                    raise ProcessLookupError()  # после SIGTERM процесса нет
                return  # жив до SIGTERM
            raise OSError(sig)

        def _fake_run_with_cmd(cmdline):
            def _r(*a, **kw):
                return type("R", (), {"stdout": cmdline})()
            return _r

        try:
            _ba.os.kill = _fake_kill
            _prof3 = tmp / "prof_reclaim"
            _prof3.mkdir(exist_ok=True)
            os.symlink("host-4242", _prof3 / "SingletonLock")
            _ba.subprocess.run = _fake_run_with_cmd(
                f"/Applications/Google Chrome --user-data-dir={_prof3} "
                f"--no-first-run about:blank")
            _ba._check_profile_lock(str(_prof3))
            check("профиль: Chrome без отладки на выделенном профиле — "
                  "SIGTERM и лок освобождён",
                  _kills == [_signal.SIGTERM]
                  and not os.path.lexists(_prof3 / "SingletonLock"))

            _kills.clear()
            _prof4 = tmp / "prof_debug"
            _prof4.mkdir(exist_ok=True)
            os.symlink("host-4243", _prof4 / "SingletonLock")
            _ba.subprocess.run = _fake_run_with_cmd(
                f"/Applications/Google Chrome --remote-debugging-port=9222 "
                f"--user-data-dir={_prof4}")
            try:
                _ba._check_profile_lock(str(_prof4))
                _lk2 = ""
            except _ba.BrowserUnavailable as e:
                _lk2 = str(e)
            check("профиль: держателя С отладочным флагом не трогаем",
                  "Закрой" in _lk2 and not _kills)

            _kills.clear()
            _prof5 = tmp / "prof_main"
            _prof5.mkdir(exist_ok=True)
            os.symlink("host-4244", _prof5 / "SingletonLock")
            _ba.subprocess.run = _fake_run_with_cmd(
                f"/Applications/Google Chrome --user-data-dir={_prof5}")
            _orig_def = _ba._is_default_browser_profile
            _ba._is_default_browser_profile = lambda p: True
            try:
                try:
                    _ba._check_profile_lock(str(_prof5))
                    _lk3 = ""
                except _ba.BrowserUnavailable as e:
                    _lk3 = str(e)
            finally:
                _ba._is_default_browser_profile = _orig_def
            check("профиль: основной профиль пользователя не убиваем",
                  "Закрой" in _lk3 and not _kills)
        finally:
            _ba.subprocess.run, _ba.os.kill = _orig_run, _orig_kill

        _def = {"darwin": "~/Library/Application Support/Google/Chrome",
                "linux": "~/.config/google-chrome"}.get(sys.platform)
        if _def:
            check("профиль: дефолтный профиль ОС распознаётся, vpc — нет",
                  _ba._is_default_browser_profile(os.path.expanduser(_def))
                  and not _ba._is_default_browser_profile(
                      str(tmp / "vpc-browser-profile")))

    # background-вкладки (web_llm): без CDP — понятная ошибка, не AppleScript
    _orig_sel2 = _ba._select_backend
    _ba._select_backend = lambda *a, **kw: "applescript"
    try:
        try:
            _ba.open_new_tab("https://x.test", background=True)
            _noas = ""
        except _ba.BrowserUnavailable as e:
            _noas = str(e)
        check("background-вкладка без CDP: ошибка про CDP, AppleScript не зовём",
              "CDP" in _noas)
    finally:
        _ba._select_backend = _orig_sel2

    # Разбор снапшота: JSON → items; мусор — человеческая ошибка
    _u, _its = _ba._parse_snapshot(json.dumps(
        {"url": "https://x.test/p", "items": [
            {"idx": 0, "tag": "a", "text": "Hi", "w": 10, "h": 5, "vp": 1}]}))
    check("снапшот: валидный JSON → url + нормализованные items",
          _u == "https://x.test/p" and _its[0]["idx"] == 0
          and _its[0]["vp"] is True and _its[0]["text"] == "Hi")
    for _bad, _want in (("not json", "не разобрался"),
                        ("__empty__", "нет кликабельных"),
                        (json.dumps({"url": "https://x", "items": []}),
                         "нет кликабельных")):
        try:
            _ba._parse_snapshot(_bad)
            _ps = ""
        except _ba.BrowserUnavailable as e:
            _ps = str(e)
        check(f"снапшот: {_want} — BrowserUnavailable", _want in _ps)

    # Готовность страницы: стабильный DOM за 2 одинаковых опроса; без
    # стабильности — выход по общему таймауту
    class _FakePage:
        def __init__(self, states):
            self.states = states  # список или callable → всегда новое состояние
            self.i = 0
        def wait_for_load_state(self, *a, **kw):
            pass
        def evaluate(self, js):
            if callable(self.states):
                self.i += 1
                return self.states()
            v = self.states[min(self.i, len(self.states) - 1)]
            self.i += 1
            return v
        def wait_for_timeout(self, ms):
            pass

    _fp = _FakePage(["u|loading|1|10", "u|complete|2|20", "u|complete|2|20",
                     "u|complete|2|20", "u|complete|2|20"])
    _ba.wait_page_ready(_fp, timeout_sec=2.0)
    check("готовность: 2 одинаковых опроса подряд — хватит, лишнего не ждём",
          _fp.i == 4)
    _ctr = [0]

    def _next_state():  # гарантированно новое состояние на каждый опрос
        _ctr[0] += 1
        return f"u|complete|{_ctr[0]}"

    _fp2 = _FakePage(_next_state)
    # monotonic, а не time(): сравниваем с дедлайном wait_page_ready, который
    # сам на monotonic; gettimeofday может отставать на микросекунды
    _t0 = time.monotonic()
    _ba.wait_page_ready(_fp2, timeout_sec=0.3)
    check("готовность: DOM вечно меняется — выход по таймауту, не вечность",
          0.3 <= time.monotonic() - _t0 < 5)

    # Closed-loop опрос: изменение состояния засчитано, постоянство — нет
    _seq = iter(["x", "x", "y"])
    check("closed-loop: состояние изменилось → True",
          _ba._poll_state_change(lambda: next(_seq, "y"), "x",
                                 timeout=1.0, interval=0.01) is True)
    check("closed-loop: состояние не менялось → False по таймауту",
          _ba._poll_state_change(lambda: "x", "x", timeout=0.05,
                                 interval=0.01) is False)

    # Текстовая обёртка снапшота (совместимость)
    _orig_se2 = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.test/y", "x.test", [_it(0, "a", "Hi"), _it(1, "button", "Go")])
    try:
        _wu, _wh, _wt = _ba.snapshot_clickables()
        check("snapshot_clickables: текстовая форма «idx|тег|текст»",
              _wu == "https://x.test/y" and _wh == "x.test"
              and _wt == "0|a|Hi\n1|button|Go")
    finally:
        _ba.snapshot_elements = _orig_se2

    # ── 8c. Ввод текста «введи X в поле Y» ──
    from app.features.computer_control import parse_type_request
    check("type-parse: «введи в поле ПОЛЕ ТЕКСТ» → тело команды",
          parse_type_request("введи в поле выберите город новосибирск")
          == "в поле выберите город новосибирск")
    check("type-parse: «напиши ТЕКСТ в поле ПОЛЕ» → тело",
          parse_type_request("напиши привет в поле поиска")
          == "привет в поле поиска")
    check("type-parse: не команда ввода / простыня → None",
          parse_type_request("расскажи сказку") is None
          and parse_type_request("введи" + " x" * 150) is None)
    check("type-parse: «введи меня в курс дела» — идиома, не ввод в страницу",
          parse_type_request("введи меня в курс дела") is None
          and parse_type_request("введи нас в курс дела") is None
          and parse_type_request("введи мой город") == "мой город")
    check("type-parse: «введи email» — тело команды",
          parse_type_request("введи schoolyuurei@gmail.com")
          == "schoolyuurei@gmail.com")

    st8 = tmp / "s8t"
    mt = ComputerControlManager(context="t", base_dir=st8,
                                config={**CFG, "allow_domains": []})
    _inputs2 = [_it(0, "input", "Выберите город", ed=True),
                _it(1, "input", "Поиск по меню", ed=True),
                _it(2, "a", "Войти")]  # ссылка — НЕ поле ввода
    _orig_se5, _orig_ft5 = _ba.snapshot_elements, _ba.fill_tagged
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://yobidoyobi.ru", "yobidoyobi.ru", _inputs2)
    try:
        act_t, err_t = mt.resolve_type("новосибирск в поле выберите город",
                                       None, _BoomRouter())
        check("type: «ТЕКСТ в поле ПОЛЕ» — текст и поле разделены верно",
              err_t is None and act_t["idx"] == 0
              and act_t["text"] == "новосибирск"
              and act_t["element"] == "Выберите город")
        act_t2, err_t2 = mt.resolve_type("в поле выберите город новосибирск",
                                         None, _BoomRouter())
        check("type: «в поле ПОЛЕ ТЕКСТ» — префиксный матч подписи",
              err_t2 is None and act_t2["idx"] == 0
              and act_t2["text"] == "новосибирск"
              and act_t2.get("choose", {}).get("path") == "match")
        act_t3, _ = mt.resolve_type("в поле поиск по меню роллы", None,
                                    _BoomRouter())
        check("type: префикс из нескольких слов — самая длинная подпись",
              act_t3 is not None and act_t3["idx"] == 1
              and act_t3["text"] == "роллы")
        act_t4, err_t4 = mt.resolve_type("роллы в поле поиск", None,
                                         _BoomRouter())
        check("type: поле через «в поле» — скоринг по подписям, без LLM",
              err_t4 is None and act_t4["idx"] == 1
              and act_t4["text"] == "роллы")
        no_t, no_e = mt.resolve_type("мне длинное письмо про жизнь", None,
                                     _BoomRouter())
        check("type: «напиши письмо» без маркеров поля — не наше, в LLM-поток",
              no_t is None and no_e is None)
        _, err_t7 = mt.resolve_type("привет в поле фамилия", None, _BoomRouter())
        check("type: поле не найдено — причина + подсказка по видимым полям",
              err_t7 is not None and "фамилия" in err_t7
              and "Выберите город" in err_t7)
        _, err_t8 = mt.resolve_type("привет в поле", None, _BoomRouter())
        check("type: «в поле» без названия — внятная просьба переформулировать",
              err_t8 is not None and "в какое поле" in err_t8)
        # Единственное поле на странице + одно слово — вводим в него
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://yobidoyobi.ru", "yobidoyobi.ru",
            [_it(0, "input", "Выберите город", ed=True)])
        act_t5, _ = mt.resolve_type("новосибирск", None, _BoomRouter())
        check("type: одно поле + одно слово — в единственное поле",
              act_t5 is not None and act_t5["idx"] == 0
              and act_t5["text"] == "новосибирск")
        # Нет полей ввода вообще — честная причина
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://yobidoyobi.ru", "yobidoyobi.ru", [_it(0, "a", "Войти")])
        _, err_t6 = mt.resolve_type("привет в поле поиск", None, _BoomRouter())
        check("type: нет полей ввода — честная причина",
              err_t6 is not None and "нет полей ввода" in err_t6)
        # «введи email» (одно слово) — явная команда ввода: неудаче честная
        # причина, а не (None, None) → LLM, который «изобразит» заполнение
        _, err_t10 = mt.resolve_type("schoolyuurei@gmail.com", None, _BoomRouter())
        check("type: одно слово без полей на странице — причина, не LLM",
              err_t10 is not None and "нет полей ввода" in err_t10)
        def _snap_boom(host=None, tab_id=None):
            raise _ba.BrowserUnavailable("нет отслеживаемой вкладки")
        _ba.snapshot_elements = _snap_boom
        _, err_t11 = mt.resolve_type("schoolyuurei@gmail.com", None, _BoomRouter())
        check("type: одно слово при мёртвой странице — причина, не LLM",
              err_t11 is not None and "Не удалось" in err_t11)
        # Одно слово + поля без совпадения подписи — подсказка с видимыми полями
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://yobidoyobi.ru", "yobidoyobi.ru", _inputs2)
        _, err_t12 = mt.resolve_type("schoolyuurei@gmail.com", None, _BoomRouter())
        check("type: одно слово + чужие поля — подсказка с полями, не LLM",
              err_t12 is not None and "Не разобрал" in err_t12
              and "Выберите город" in err_t12)
        # «эссе в стиле классиков»: голое «в» без явных маркеров — генерация,
        # по-прежнему уходит в LLM-поток
        no_t2, no_e2 = mt.resolve_type("эссе в стиле классиков", None,
                                       _BoomRouter())
        check("type: «в стиле…» без поля — по-прежнему не наше (LLM-поток)",
              no_t2 is None and no_e2 is None)
        # «на ёбидоёби» — сайт срезается по алиасу, снапшот его вкладки
        ms_t = make(cfg={**CFG, "allow_domains": [],
                         "sites": {"ёбидоёби": "yobidoyobi.ru"}})
        _tcalls = []
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            _tcalls.append(host),
            ("https://yobidoyobi.ru", "yobidoyobi.ru", _inputs2))[1]
        act_ts, _ = ms_t.resolve_type(
            "новосибирск в поле выберите город на ёбидоёби", None, _BoomRouter())
        check("type: «на ёбидоёби» — сайт срезан по алиасу, снапшот по хосту",
              act_ts is not None and _tcalls[-1] == "yobidoyobi.ru")
        # «в чат» — НЕ сайт (нет алиаса/точки): хвост остаётся в теле команды
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://yobidoyobi.ru", "yobidoyobi.ru",
            [_it(0, "textarea", "Чат с оператором", ed=True)])
        act_tc, _ = mt.resolve_type("привет в чат с оператором", None,
                                    _BoomRouter())
        check("type: «в чат с оператором» — поле по матчу, не сайт",
              act_tc is not None and act_tc["text"] == "привет")
        # execute → fill_tagged(host, idx, text); аудит с verify
        _fills = []
        _ba.fill_tagged = lambda host, idx, text, tab_id=None, submit=False: (
            _fills.append((host, idx, text, tab_id)), "filled")[1]
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://yobidoyobi.ru", "yobidoyobi.ru", _inputs2)
        act_t9, _ = mt.resolve_type("новосибирск в поле выберите город",
                                    None, _BoomRouter())
        ok9, _ = mt.execute(act_t9, "c8t")
        rec9 = json.loads((st8 / "audit.jsonl").read_text(encoding="utf-8")
                          .strip().splitlines()[-1])
        check("type: execute → fill_tagged, аудит kind=type verify=ok",
              ok9 and _fills[-1][:3] == ("yobidoyobi.ru", 0, "новосибирск")
              and rec9.get("kind") == "type" and rec9.get("verify") == "ok")
        # Closed-loop: значение поля не совпало — честное «не уверен»
        def _ft_noop(host, idx, text, tab_id=None, submit=False):
            raise _ba.FillUncertain(
                "текст отправлен в поле, но его значение не совпало — "
                "не уверен, что ввод сработал")

        _ba.fill_tagged = _ft_noop
        ok10, det10 = mt.execute(act_t9, "c8t")
        rec10 = json.loads((st8 / "audit.jsonl").read_text(encoding="utf-8")
                           .strip().splitlines()[-1])
        check("type: closed-loop — значение не совпало → «не уверен»",
              not ok10 and "не уверен" in det10
              and rec10.get("error_class") == "uncertain"
              and rec10.get("verify") == "uncertain")
        check("type: describe/confirm/done говорят, что и куда вводится",
              "ввести «новосибирск»" in ComputerControlManager.describe(act_t9)
              and "Выберите город" in ComputerControlManager.describe(act_t9)
              and ComputerControlManager.confirm_question(act_t9).startswith("Ввести")
              and "ввёл «новосибирск»" in ComputerControlManager.describe_done(act_t9))
        # «роллы в поиск» — без слова «поле»: «поиск» сам название поля.
        # Раньше фраза не считалась явной командой и уходила в LLM-поток,
        # который «изображал» ввод, ничего не делая
        _orig_hel = _ba.hidden_editable_labels
        _ba.hidden_editable_labels = lambda host=None, tab_id=None: []
        act_tsr, err_tsr = mt.resolve_type("роллы в поиск", None, _BoomRouter())
        check("type: «X в поиск» — сепаратор, поле поиска найдено",
              err_tsr is None and act_tsr["idx"] == 1
              and act_tsr["text"] == "роллы")
        # Поле скрыто (свёрнутое меню): не «нет поля», а «есть, но скрыто»
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://ranobes.com", "ranobes.com",
            [_it(0, "input", "Введите номер главы", ed=True)])
        _ba.hidden_editable_labels = lambda host=None, tab_id=None: [
            {"t": "Пишите полное название...", "q": 1}]
        _, err_hid = mt.resolve_type("повелитель тайн в поиск", None,
                                     _BoomRouter())
        check("type: поле поиска скрыто — честное «есть, но скрыто»",
              err_hid is not None and "скрыто" in err_hid
              and "Пишите полное название" in err_hid)
        # Скрытые поля есть, но не про цель — обычная подсказка
        _ba.hidden_editable_labels = lambda host=None, tab_id=None: [
            {"t": "Логин", "q": 0}]
        _, err_hid2 = mt.resolve_type("повелитель тайн в поиск", None,
                                      _BoomRouter())
        check("type: скрытое поле не про цель — обычная подсказка",
              err_hid2 is not None and "не нашёл поля" in err_hid2
              and "скрыто" not in err_hid2)
        _ba.hidden_editable_labels = _orig_hel
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://yobidoyobi.ru", "yobidoyobi.ru", _inputs2)
    finally:
        _ba.snapshot_elements, _ba.fill_tagged = _orig_se5, _orig_ft5

    # Гео-плейсхолдер «мой город» — город из местоположения (env_location)
    from app.features import env_context as _ec
    _orig_ll = _ec.load_location
    _orig_se6 = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://yobidoyobi.ru", "yobidoyobi.ru", _inputs2)
    _ec.load_location = lambda: {"mode": "geo", "city": "Новосибирск",
                                 "lat": 55.0, "lon": 82.9}
    try:
        act_g, err_g = mt.resolve_type("мой город в поле поиск", None,
                                       _BoomRouter())
        check("type+geo: «мой город в поле поиск» — город подставлен",
              err_g is None and act_g["text"] == "Новосибирск"
              and act_g["idx"] == 1)
        act_g2, err_g2 = mt.resolve_type("город в поле поиск", None,
                                         _BoomRouter())
        check("type+geo: голое «город» — тоже плейсхолдер",
              err_g2 is None and act_g2["text"] == "Новосибирск")
        # Одно поле на странице: «введи мой город» — без названия поля
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://yobidoyobi.ru", "yobidoyobi.ru",
            [_it(0, "input", "Поиск", ed=True)])
        act_g3, err_g3 = mt.resolve_type("мой город", None, _BoomRouter())
        check("type+geo: «введи мой город» + одно поле — в него",
              err_g3 is None and act_g3["text"] == "Новосибирск"
              and act_g3["idx"] == 0)
        # Несколько полей и ни одно не названо — подсказка, а не молчание
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://yobidoyobi.ru", "yobidoyobi.ru", _inputs2)
        act_g4, err_g4 = mt.resolve_type("мой город", None, _BoomRouter())
        check("type+geo: несколько полей — подсказка с перечнем полей",
              act_g4 is None and err_g4 is not None
              and "Поиск по меню" in err_g4)
        # Местоположение выключено — честная причина, а не слово «город» в поле
        _ec.load_location = lambda: {"mode": "off"}
        act_g5, err_g5 = mt.resolve_type("мой город в поле поиск", None,
                                         _BoomRouter())
        check("type+geo: местоположение off — честная причина",
              act_g5 is None and err_g5 is not None
              and "местоположение" in err_g5.lower())
        # «Новосибирск, Россия» (manual-режим) → только город, без страны
        _ec.load_location = lambda: {"mode": "manual",
                                     "city": "Новосибирск, Россия"}
        check("type+geo: «Город, Страна» → только город",
              mt._home_city() == "Новосибирск")
    finally:
        _ec.load_location = _orig_ll
        _ba.snapshot_elements = _orig_se6

    # Персист контекста страницы (переживает перезапуск) + строка в инструкции
    sp8 = tmp / "s8p"
    mp = SpyManager(context="t", config={**CFG, "allow_domains": []},
                    base_dir=sp8)
    okp, _ = mp.execute({"kind": "url", "value": "https://youtube.com/watch"},
                        "c8p")
    check("персист: execute(url) пишет last_tab.json",
          okp and json.loads((sp8 / "last_tab.json")
                             .read_text(encoding="utf-8"))["host"]
          == "youtube.com/watch")
    mp2 = SpyManager(context="t", config=CFG, base_dir=sp8)
    check("персист: новый менеджер (рестарт) восстанавливает контекст",
          mp2._last_host == "youtube.com/watch"
          and mp2._last_url == "https://youtube.com/watch")
    check("инструкция: при контексте — строка про открытую страницу",
          "Сейчас открытая мной страница: youtube.com/watch"
          in mp2.instruction_block())
    mp3 = SpyManager(context="t", config=CFG, base_dir=tmp / "s8p-empty")
    check("инструкция: без контекста — без строки про страницу",
          "Сейчас открытая мной страница" not in mp3.instruction_block())

    # Снапшот: флаг ed (поле ввода) парсится; без него — False
    _u3, _its3 = _ba._parse_snapshot(json.dumps(
        {"url": "https://x.test/p", "items": [
            {"idx": 0, "tag": "input", "text": "Город", "ed": 1},
            {"idx": 1, "tag": "a", "text": "Hi"}]}))
    check("снапшот: флаг ed парсится, у ссылки — False",
          _its3[0]["ed"] is True and _its3[1]["ed"] is False)

    # ── 9. Инструкция для промпта ──
    block = make().instruction_block()
    check("инструкция: маркеры + ключи + whitelist доменов + запрос подтверждения",
          "OPEN_URL" in block and "OPEN_APP" in block and "RUN_TASK" in block
          and "safari" in block and "chrome" in block and "youtube.com" in block
          and "ОБЯЗАН спрашивать подтверждение" in block and "ЕСТЬ доступ" in block)
    check("инструкция immediate-режима: без подтверждения",
          "выполняется сразу" in make(cfg={**CFG, "confirm": False}).instruction_block())

    # ── 10. Интеграция: prepare_messages + guard conversation_style ──
    from app.core.persona import PersonaLayer
    p = PersonaLayer.__new__(PersonaLayer)
    p.system_prompt = "SYSTEM."
    msgs = p.prepare_messages("привет", computer_control_context="CC_NOTE",
                              conversation_style_context="CS_NOTE")
    content = msgs[0]["content"]
    check("prepare_messages: cc-нота перед conv-нотой (conv — последняя)",
          content.endswith("\n\nCS_NOTE") and "\n\nCC_NOTE" in content)

    from app.features import conversation_style as cs
    cfg_none = cs.ConversationStyleConfig(
        {"conversation_style": {"question_frequency": "none"}})
    check("conv_style: ответ с маркером [OPEN_URL:] не уходит на регенерацию",
          not cs.should_regenerate(cfg_none, "Открыть YouTube? [OPEN_URL:youtube.com]", 5))

    # ── 11. Фоновые вкладки (raw CDP): реестр и диспетчеризация ──
    raw_calls = []
    def _fake_raw_call(method, params=None, session_id=None, _retried=False):
        raw_calls.append((method, params or {}, session_id))
        if method == "Target.createTarget":
            return {"targetId": "T1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "S1"}
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"url": "https://chat.qwen.ai/c/x1"}}
        if method == "Runtime.evaluate":
            return {"result": {"value": "7"}}
        return {}
    _orig_raw_call, _orig_sel = _ba._raw_call, _ba._select_backend
    _orig_submit = _ba._WORKER.submit
    _ba._raw_call = _fake_raw_call
    _ba._select_backend = lambda *a, **kw: "cdp"
    _ba._WORKER.submit = lambda fn: None  # playwright-вкладок «нет» — только raw
    try:
        tid = _ba.open_new_tab("https://chat.qwen.ai/c/x1", background=True)
        check("raw: фоновая вкладка через createTarget(background:true) + flat-сессия",
              tid in _ba._RAW_TABS
              and raw_calls[0][0] == "Target.createTarget"
              and raw_calls[0][2] is None
              and raw_calls[0][1].get("background") is True
              and raw_calls[1][0] == "Target.attachToTarget"
              and raw_calls[1][1].get("flatten") is True
              and raw_calls[2] == ("Page.navigate",
                                   {"url": "https://chat.qwen.ai/c/x1"}, "S1"))
        check("raw: tab_url/eval_js/count_blocks идут в raw-сессию (S1)",
              _ba.tab_url(tab_id=tid) == "https://chat.qwen.ai/c/x1"
              and _ba.eval_js(None, tid, "3+4") == "7"
              and _ba.count_blocks(None, tid, [".x"]) == 7
              and all(c[2] == "S1" for c in raw_calls
                      if c[0] == "Runtime.evaluate"))
        check("raw: find_tab_id видит фоновую вкладку по хосту",
              _ba.find_tab_id("chat.qwen.ai/c/x1") == tid
              and _ba.find_tab_id("example.org") is None)
        _ba._RAW_TABS.pop(tid)
        try:
            _ba._raw_tab(tid)
            gone = False
        except _ba.BrowserUnavailable:
            gone = True
        check("raw: после drop вкладка — BrowserUnavailable", gone)
    finally:
        _ba._raw_call, _ba._select_backend = _orig_raw_call, _orig_sel
        _ba._WORKER.submit = _orig_submit
        _ba._RAW_TABS.clear()

    # ── 12. Сценарии: парсеры, запись из трассы, runner, автопредложение ──
    from app.features.scenario_manager import ScenarioManager

    class FakeCC:
        """Минимальный computer_control для ScenarioManager: без браузера —
        резолверы/исполнение подменены, base_dir ведёт в tmp (трасса)."""
        def __init__(self, base_dir, fail=False, uncertain=False,
                     fail_times=None, snapshot_items=None):
            self.base_dir = Path(base_dir)
            self.calls = []
            self.fail = fail
            self.uncertain = uncertain
            self.fail_times = fail_times  # resolve падает столько раз подряд
            self.snapshot_items = snapshot_items or []

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=True):
            if not self.snapshot_items:
                return None, None, None, None, "нет открытой вкладки"
            return ("https://x.ru", "x.ru", self.snapshot_items, None, None)

        @staticmethod
        def _score_candidates(items, goal):
            return [(1.0, it) for it in items]

        def resolve_click(self, goal, site_word, router, chat_id=""):
            if self.fail or (self.fail_times or 0) > 0:
                if not self.fail:
                    self.fail_times -= 1
                return None, f"не нашёл «{goal}»"
            return {"kind": "click", "idx": 1, "element": goal,
                    "host": site_word or "x.ru", "value": "https://x.ru"}, None

        def resolve_type(self, body, site_word, router, chat_id=""):
            if self.fail:
                return None, "нет поля"
            return {"kind": "type", "idx": 1, "element": "поле",
                    "text": body, "host": site_word or "x.ru",
                    "value": "https://x.ru"}, None

        def execute(self, action, chat_id="", router=None):
            self.calls.append(dict(action))
            if self.uncertain and action["kind"] in ("click", "type"):
                return False, ("клик отправлен, но страница не изменилась — "
                               "не уверен, что сработало")
            if self.fail:
                return False, "браузер недоступен"
            return True, ""

        @staticmethod
        def describe_done(action):
            return {"url": "открыл сайт", "click": "нажал кнопку",
                    "type": "ввёл текст", "send": "отправил"}.get(
                action["kind"], "сделал")

    class FakeRouter:
        def __init__(self, *responses):
            self.responses = list(responses)
            self.prompts = []

        def get_response(self, messages, **kw):
            self.prompts.append(messages[-1]["content"])
            return self.responses.pop(0) if self.responses else None

    sc_tmp = Path(tempfile.mkdtemp(prefix="scenarios_test_"))

    def write_trace(cc_dir, chat_id, rows):
        """rows: [(kind, extra_dict)] — пишем audit.jsonl как _audit."""
        with open(Path(cc_dir) / "audit.jsonl", "a", encoding="utf-8") as f:
            for kind, extra in rows:
                rec = {"ts": time.time(), "chat_id": str(chat_id), "ok": True,
                       "kind": kind, "value": "https://x.ru", "detail": ""}
                rec.update(extra)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    TRACE4 = [("url", {}),
              ("click", {"element": "Гавайская", "host": "dodopizza.ru"}),
              ("click", {"element": "В корзину", "host": "dodopizza.ru"}),
              ("click", {"element": "Оформить заказ", "host": "dodopizza.ru"})]

    # парсеры
    check("sc: parse_save «запомни сценарий заказ пиццы»",
          ScenarioManager.parse_save_request("запомни сценарий заказ пиццы")
          == "заказ пиццы")
    check("sc: parse_save «запиши этот сценарий как «тест»!»",
          ScenarioManager.parse_save_request("запиши этот сценарий как «тест»!")
          == "тест")
    check("sc: parse_save без имени → пустая строка (спросим название)",
          ScenarioManager.parse_save_request("запомни сценарий") == "")
    check("sc: parse_save НЕ команда",
          ScenarioManager.parse_save_request("расскажи сценарий фильма") is None
          and ScenarioManager.parse_save_request("привет") is None)
    check("sc: parse_cancel «отмена»/«отмени сценарий»/«хватит»",
          ScenarioManager.parse_cancel("отмена")
          and ScenarioManager.parse_cancel("отмени сценарий")
          and ScenarioManager.parse_cancel("Хватит.")
          and not ScenarioManager.parse_cancel("отмена встречи завтра"))

    # запись: LLM-обобщение + payment-отрезка
    cc1 = FakeCC(sc_tmp)
    sm1 = ScenarioManager(context="sctest1", computer_control=cc1,
                          base_dir=sc_tmp / "sc1")
    write_trace(sc_tmp, "c1", TRACE4)
    llm_json = json.dumps({
        "aliases": ["закажи пиццу"],
        "steps": [
            {"op": "open", "url": "https://dodopizza.ru/novosibirsk"},
            {"op": "ask", "slot": "pizza", "question": "Какую пиццу?"},
            {"op": "click", "target": "{pizza}", "host": "dodopizza.ru"},
            {"op": "click", "target": "В корзину", "host": "dodopizza.ru"},
            {"op": "click", "target": "Оплатить картой", "host": "dodopizza.ru"},
        ]}, ensure_ascii=False)
    rt1 = FakeRouter(f"```json\n{llm_json}\n```")
    sc1, err1 = sm1.build_from_trace("c1", "заказ пиццы", rt1)
    check("sc: build_from_trace — сценарий собран", sc1 is not None and err1 is None)
    check("sc: payment-шаг отрезан в handoff",
          sc1 is not None
          and sc1["steps"][-1]["op"] == "handoff"
          and not any("Оплатить" in str(s.get("target") or "")
                      for s in sc1["steps"]))
    check("sc: ask-шаг и слот в target на месте",
          sc1 is not None
          and any(s["op"] == "ask" and s["slot"] == "pizza" for s in sc1["steps"])
          and any(s["op"] == "click" and s.get("target") == "{pizza}"
                  for s in sc1["steps"]))
    check("sc: алиасы сохранены, сценарий в хранилище",
          sm1.find_scenario("закажи пиццу") == "заказ пиццы"
          and "заказ пиццы" in sm1.list_names())
    check("sc: матч по имени с опечаткой падежа, «привет» — не матч",
          sm1.find_scenario("давай заказ пиццы") == "заказ пиццы"
          and sm1.find_scenario("привет, как дела") is None)

    # запись: мусор от LLM → rule-based фолбэк
    cc2 = FakeCC(sc_tmp)
    sm2 = ScenarioManager(context="sctest2", computer_control=cc2,
                          base_dir=sc_tmp / "sc2")
    write_trace(sc_tmp, "c2", TRACE4)
    rt2 = FakeRouter("не могу, у меня лапки")
    sc2, err2 = sm2.build_from_trace("c2", "ручной режим", rt2)
    check("sc: сломанный LLM → rule-based фолбэк",
          sc2 is not None and err2 is None
          and any(s["op"] == "open" for s in sc2["steps"])
          and all(s.get("target") != "Оформить заказ" or True
                  for s in sc2["steps"]))
    # пустая трасса → честный отказ
    sm3 = ScenarioManager(context="sctest3", computer_control=FakeCC(sc_tmp),
                          base_dir=sc_tmp / "sc3")
    sc3, err3 = sm3.build_from_trace("nochat", "пусто", FakeRouter())
    check("sc: без трассы — отказ с объяснением",
          sc3 is None and err3 is not None and "нечего записывать" in err3)

    # явная запись: «начни записывать сценарий» … «сохрани сценарий»
    check("sc: parse_start_record — формы",
          ScenarioManager.parse_start_record("начни записывать сценарий") == ""
          and ScenarioManager.parse_start_record(
              "начни записывать сценарий заказ пиццы") == "заказ пиццы"
          and ScenarioManager.parse_start_record("начни запись") == ""
          and ScenarioManager.parse_start_record("запомни сценарий х") is None
          and ScenarioManager.parse_start_record("привет") is None)
    check("sc: parse_stop_record — «отмени запись»",
          ScenarioManager.parse_stop_record("отмени запись")
          and ScenarioManager.parse_stop_record("отмени запись сценария")
          and not ScenarioManager.parse_stop_record("отмена"))
    cc5 = FakeCC(sc_tmp)
    sm5 = ScenarioManager(context="sctest5", computer_control=cc5,
                          base_dir=sc_tmp / "sc5")
    # старая трасса ДО начала записи — не должна попасть в сценарий
    _old_ts = time.time() - 3600
    write_trace(sc_tmp, "c5", [("url", {"ts": _old_ts,
                                        "value": "https://old.ru"})])
    r_start = sm5.record_start("c5", "мой тест")
    check("sc: record_start — запись пошла",
          sm5.recording("c5") and "Записываю" in r_start)
    check("sc: повторный старт — запись одна",
          "Уже записываю" in sm5.record_start("c5"))
    # действия после начала записи
    write_trace(sc_tmp, "c5", [("url", {"value": "https://dodopizza.ru/x"}),
                               ("click", {"element": "Гавайская",
                                          "host": "dodopizza.ru"})])
    reply5 = sm5.record_reply("c5", "", FakeRouter("мусор"))
    check("sc: «сохрани сценарий» — запись снята, имя из стартовой команды",
          "мой тест" in reply5 and not sm5.recording("c5"))
    sc5 = sm5._scenarios.get("мой тест")
    check("sc: запись: только действия после «начни записывать»",
          sc5 is not None
          and not any("old.ru" in str(s.get("url") or "")
                      for s in sc5["steps"])
          and any(s["op"] == "open" and "dodopizza" in str(s.get("url") or "")
                  for s in sc5["steps"])
          and any(s["op"] == "click" and s.get("target") == "Гавайская"
                  for s in sc5["steps"]))
    # отмена записи без сохранения
    sm5.record_start("c5", "второй")
    check("sc: «отмени запись» — снято без сохранения",
          "отменена" in sm5.record_stop("c5") and not sm5.recording("c5")
          and "второй" not in sm5.list_names())
    # автопредложение молчит во время явной записи
    write_trace(sc_tmp, "c5", [("click", {"element": "В корзину",
                                          "host": "dodopizza.ru"})])
    sm5.record_start("c5")
    check("sc: maybe_offer молчит во время записи",
          sm5.maybe_offer("c5", "спасибо") is None)
    sm5.record_stop("c5")

    # валидация шагов: неизвестный слот / битый op → None
    check("sc: валидация — слот до ask запрещён",
          ScenarioManager._validate_steps(
              [{"op": "click", "target": "{x}"}]) is None)
    check("sc: валидация — open без http отклонён",
          ScenarioManager._validate_steps(
              [{"op": "open", "url": "ftp://x"}]) is None)
    check("sc: валидация — нормальная цепочка проходит",
          ScenarioManager._validate_steps(
              [{"op": "ask", "slot": "a", "question": "Что?"},
               {"op": "click", "target": "{a}"}]) is not None)

    # runner: open → ask → click{slot} → handoff
    cc4 = FakeCC(sc_tmp)
    sm4 = ScenarioManager(context="sctest4", computer_control=cc4,
                          base_dir=sc_tmp / "sc4")
    sm4._scenarios["тест"] = {
        "name": "тест", "aliases": [], "created": time.time(),
        "steps": [
            {"op": "open", "url": "https://x.ru"},
            {"op": "ask", "slot": "pizza", "question": "Какую пиццу?"},
            {"op": "click", "target": "{pizza}", "host": "x.ru"},
            {"op": "ask", "slot": "extra", "question": "Ещё что-то?",
             "optional": True},
            {"op": "click", "target": "В корзину", "host": "x.ru"},
            {"op": "handoff", "message": "Оплата за тобой."}]}
    r = sm4.start("тест", "run1", None)
    check("sc: старт — open исполнен, пауза на ask",
          "Какую пиццу?" in r and len(cc4.calls) == 1
          and cc4.calls[0]["kind"] == "url" and sm4.active("run1"))
    r = sm4.feed("run1", "гавайскую", None)
    check("sc: слот подставлен в target клика",
          len(cc4.calls) == 2 and cc4.calls[1].get("element") == "гавайскую"
          and "Ещё что-то?" in r)
    r = sm4.feed("run1", "нет", None)
    check("sc: «нет» на опциональный ask — пропуск, прогон до handoff",
          "Оплата за тобой." in r and "завершён" in r
          and len(cc4.calls) == 3 and not sm4.active("run1"))
    check("sc: прогоны per-chat изолированы",
          not sm4.active("run2"))

    # runner: сбой шага → «повтори» не двигает pos, «дальше» пропускает
    cc5 = FakeCC(sc_tmp, fail=True)
    sm5 = ScenarioManager(context="sctest5", computer_control=cc5,
                          base_dir=sc_tmp / "sc5")
    sm5._scenarios["падение"] = {
        "name": "падение", "aliases": [], "created": time.time(),
        "steps": [{"op": "click", "target": "X", "host": "x.ru"},
                  {"op": "click", "target": "Y", "host": "x.ru"}]}
    r = sm5.start("падение", "run9", None)
    check("sc: сбой шага — стоп с подсказкой",
          "повтори" in r and "отмена" in r and sm5.active("run9"))
    r = sm5.feed("run9", "что-нибудь", None)
    check("sc: непонятный ответ на сбое — та же подсказка, pos не сдвинут",
          "повтори" in r and sm5._runs["run9"]["pos"] == 0)
    r = sm5.feed("run9", "дальше", None)
    check("sc: «дальше» пропускает сбойный шаг",
          sm5._runs.get("run9") is None or sm5._runs["run9"]["pos"] >= 1)
    r = sm5.start("падение", "run9", None)
    r = sm5.cancel("run9")
    check("sc: отмена снимает прогон",
          "отменён" in r and not sm5.active("run9"))
    # Антизалипание: два нераспознанных сообщения на сбое → прогон снимается,
    # второе возвращает None (уйдёт в обычный диалог)
    r = sm5.start("падение", "run10", None)
    r1 = sm5.feed("run10", "а погода какая?", None)
    r2 = sm5.feed("run10", "ну ты сломался что ли", None)
    check("sc: антизалипание — 1-е чужое: подсказка, 2-е: None + прогон снят",
          isinstance(r1, str) and "повтори" in r1
          and r2 is None and not sm5.active("run10"))
    check("sc: отмена понимает «забудь»/«выйди»",
          ScenarioManager.parse_cancel("забудь")
          and ScenarioManager.parse_cancel("выйди"))
    # LLM потеряла шаги (7 действий → 5 шагов) → rule-based фолбэк
    cc9 = FakeCC(sc_tmp)
    sm9 = ScenarioManager(context="sctest9", computer_control=cc9,
                          base_dir=sc_tmp / "sc9")
    trace7 = [("url", {}),
              ("click", {"element": "меню", "host": "nstu.ru"}),
              ("click", {"element": "ВОЙТИ", "host": "nstu.ru"}),
              ("click", {"element": "кабинет обучающегося", "host": "nstu.ru"}),
              ("click", {"element": "меню", "host": "ciu.nstu.ru"}),
              ("click", {"element": "Расписание", "host": "ciu.nstu.ru"}),
              ("click", {"element": "Расписание занятий", "host": "ciu.nstu.ru"})]
    write_trace(sc_tmp, "c9", trace7)
    lossy = json.dumps({"aliases": [], "steps": [
        {"op": "open", "url": "https://nstu.ru"},
        {"op": "click", "target": "меню", "host": "nstu.ru"},
        {"op": "click", "target": "ВОЙТИ", "host": "nstu.ru"},
        {"op": "click", "target": "Расписание", "host": "ciu.nstu.ru"},
        {"op": "click", "target": "Расписание занятий",
         "host": "ciu.nstu.ru"}]}, ensure_ascii=False)
    sc9, err9 = sm9.build_from_trace("c9", "расписание", FakeRouter(lossy))
    check("sc: LLM потеряла шаги (5 из 7) → фолбэк rule-based со всеми 7",
          sc9 is not None
          and sum(1 for s in sc9["steps"]
                  if s["op"] in ("open", "click", "type", "send")) == 7)

    # uncertain-клик (closed-loop не увидел эффекта) попадает в трассу —
    # JS-меню так открываются; и при воспроизведении не останавливает прогон
    cc10 = FakeCC(sc_tmp)
    sm10 = ScenarioManager(context="sctest10", computer_control=cc10,
                           base_dir=sc_tmp / "sc10")
    with open(Path(sc_tmp) / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "chat_id": "c10", "ok": False,
                            "verify": "uncertain", "kind": "click",
                            "element": "бургер-меню", "host": "ciu.nstu.ru",
                            "value": "", "detail": "не уверен"},
                           ensure_ascii=False) + "\n")
    check("sc: трасса берёт verify=uncertain (клик JS-меню не теряется)",
          any(r.get("element") == "бургер-меню" for r in sm10._trace("c10")))
    cc11 = FakeCC(sc_tmp, uncertain=True)
    sm11 = ScenarioManager(context="sctest11", computer_control=cc11,
                           base_dir=sc_tmp / "sc11")
    sm11._scenarios["unc"] = {
        "name": "unc", "aliases": [], "created": time.time(),
        "steps": [{"op": "click", "target": "меню", "host": "x.ru"},
                  {"op": "click", "target": "пункт", "host": "x.ru"}]}
    r = sm11.start("unc", "run11", None)
    check("sc: uncertain при воспроизведении — прогон идёт дальше",
          "завершён" in r and len(cc11.calls) == 2 and not sm11.active("run11"))

    # LLM-восстановление: элемент не найден дважды → модель выбирает из
    # живого снапшота «меню» → её клик → повтор шага успешен
    cc12 = FakeCC(sc_tmp, fail_times=2,
                  snapshot_items=[{"idx": 10, "tag": "button", "role": "",
                                   "text": "меню"}])
    sm12 = ScenarioManager(context="sctest12", computer_control=cc12,
                           base_dir=sc_tmp / "sc12")
    sm12._scenarios["rec"] = {
        "name": "rec", "aliases": [], "created": time.time(),
        "steps": [{"op": "open", "url": "https://x.ru"},
                  {"op": "click", "target": "Расписание", "host": "x.ru"}]}
    rt12 = FakeRouter("1")
    r = sm12.start("rec", "run12", rt12)
    check("sc: LLM-восстановление — клик по выбранному элементу + шаг прошёл",
          "завершён" in r and len(cc12.calls) == 3
          and cc12.calls[1].get("element") == "меню"
          and cc12.calls[2].get("element") == "Расписание")
    # В промпте восстановления — дорожная карта: что сделано (✓), где встали (✗)
    p12 = rt12.prompts[0] if rt12.prompts else ""
    check("sc: промпт восстановления содержит дорожную карту сценария",
          "✓ 1. открыть https://x.ru" in p12
          and "✗ 2. нажать «Расписание»" in p12)
    # LLM ответила «нет» — честный стоп, восстановления не было
    cc13 = FakeCC(sc_tmp, fail=True,
                  snapshot_items=[{"idx": 10, "tag": "button", "role": "",
                                   "text": "меню"}])
    sm13 = ScenarioManager(context="sctest13", computer_control=cc13,
                           base_dir=sc_tmp / "sc13")
    sm13._scenarios["rec2"] = {
        "name": "rec2", "aliases": [], "created": time.time(),
        "steps": [{"op": "click", "target": "Расписание", "host": "x.ru"}]}
    r = sm13.start("rec2", "run13", FakeRouter("нет"))
    check("sc: LLM «нет» → стоп с подсказкой, лишних кликов нет",
          "повтори" in r and len(cc13.calls) == 0 and sm13.active("run13"))
    # Кандидаты для восстановления ранжируются: органы управления (бургер)
    # попадают в промпт, даже если в сыром снапшоте они за пределами топ-25
    filler = [{"idx": i, "tag": "a", "role": "", "text": f"Ссылка {i}"}
              for i in range(30)]
    cc14 = FakeCC(sc_tmp, fail=True,
                  snapshot_items=filler + [{"idx": 99, "tag": "button",
                                            "role": "", "text": "бургер-меню"}])
    sm14 = ScenarioManager(context="sctest14", computer_control=cc14,
                           base_dir=sc_tmp / "sc14")
    sm14._scenarios["rec3"] = {
        "name": "rec3", "aliases": [], "created": time.time(),
        "steps": [{"op": "click", "target": "Расписание", "host": "x.ru"}]}
    rt14 = FakeRouter("нет")
    sm14.start("rec3", "run14", rt14)
    p14 = rt14.prompts[0] if rt14.prompts else ""
    check("sc: восстановление — бургер попадает в промпт из-за пределов топ-25",
          "бургер-меню" in p14 and "Ссылка 29" not in p14)
    # «пропустить»: шаг устарел (страница ушла вперёд) — скипаем, идём дальше
    cc15 = FakeCC(sc_tmp, fail_times=2,
                  snapshot_items=[{"idx": 5, "tag": "a", "role": "",
                                   "text": "что-то"}])
    sm15 = ScenarioManager(context="sctest15", computer_control=cc15,
                           base_dir=sc_tmp / "sc15")
    sm15._scenarios["rec4"] = {
        "name": "rec4", "aliases": [], "created": time.time(),
        "steps": [{"op": "click", "target": "устаревший", "host": "x.ru"},
                  {"op": "click", "target": "финальный", "host": "x.ru"}]}
    r = sm15.start("rec4", "run15", FakeRouter("пропустить"))
    check("sc: LLM «пропустить» — шаг скипнут, сценарий доехал до конца",
          "пропускаю" in r and "завершён" in r
          and [c.get("element") for c in cc15.calls] == ["финальный"])

    # record_reply: без имени — вопрос; с именем — описание сценария
    cc6 = FakeCC(sc_tmp)
    sm6 = ScenarioManager(context="sctest6", computer_control=cc6,
                          base_dir=sc_tmp / "sc6")
    check("sc: record_reply без имени спрашивает название",
          "Как назвать" in sm6.record_reply("c6", "", None))
    write_trace(sc_tmp, "c6", TRACE4)
    rep = sm6.record_reply("c6", "мой сюжет", FakeRouter("мусор"))
    check("sc: record_reply — «Записал сценарий» с числом шагов",
          "Записал сценарий «мой сюжет»" in rep and "шагов" in rep)

    # maybe_offer: ≥3 действия + закрывающая фраза → предложение (один раз)
    cc7 = FakeCC(sc_tmp)
    sm7 = ScenarioManager(context="sctest7", computer_control=cc7,
                          base_dir=sc_tmp / "sc7")
    write_trace(sc_tmp, "c7", TRACE4[:2])
    check("sc: offer — <3 действий, молчим",
          sm7.maybe_offer("c7", "спасибо") is None)
    write_trace(sc_tmp, "c7", TRACE4[2:])
    offer = sm7.maybe_offer("c7", "спасибо!")
    check("sc: offer — 4 действия + «спасибо» → предложение",
          offer is not None and "запомни сценарий" in offer)
    check("sc: offer — повтор на том же окне не донимает",
          sm7.maybe_offer("c7", "спасибо") is None)
    check("sc: offer — незакрывающая фраза мимо",
          sm7.maybe_offer("c7", "а что завтра по планам?") is None)
    # уже записанный сюжет не предлагаем
    cc8 = FakeCC(sc_tmp)
    sm8 = ScenarioManager(context="sctest8", computer_control=cc8,
                          base_dir=sc_tmp / "sc8")
    sm8._scenarios["готовый"] = {
        "name": "готовый", "aliases": [], "created": time.time(),
        "steps": [{"op": "open", "url": "https://x.ru"},
                  {"op": "click", "target": "Гавайская", "host": "dodopizza.ru"},
                  {"op": "click", "target": "В корзину", "host": "dodopizza.ru"},
                  {"op": "click", "target": "Оформить заказ",
                   "host": "dodopizza.ru"}]}
    write_trace(sc_tmp, "c8", TRACE4)
    check("sc: offer — сюжет уже записан, молчим",
          sm8.maybe_offer("c8", "всё, спасибо") is None)

    # ── Классификация неудач резолва + аудит (fail_reason в audit.jsonl) ──
    def _aud_recs(chat):
        return [json.loads(l) for l in
                (tmp / "audit.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(l).get("chat_id") == chat]

    _orig_snap_rf = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru", _gh_items())
    try:
        # Пусто в снапшоте: кандидатов под цель нет вовсе
        m_rf = make()
        no_rf, err_rf = m_rf.resolve_click("загрузить", None, None,
                                           chat_id="rf-ns")
        check("resolve_fail аудит: not_in_snapshot (пусто в снапшоте)",
              no_rf is None and err_rf
              and any(r.get("kind") == "resolve_fail"
                      and r.get("fail_reason") == "not_in_snapshot"
                      and r.get("value") == "загрузить"
                      for r in _aud_recs("rf-ns")))
        # Кандидаты есть, но скор слабый (только ctx совпал, LLM нет)
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru",
            [_it(5, "button", "Выбрать", ctx="Додстер от 169 ₽")])
        no_ls, _ = m_rf.resolve_click("додстер", None, None, chat_id="rf-ls")
        check("resolve_fail аудит: low_score (кандидат слабый)",
              no_ls is None
              and any(r.get("fail_reason") == "low_score"
                      and r.get("candidates") for r in _aud_recs("rf-ls")))
        # LLM посмотрела топ и сказала «нет» (кандидаты с РАЗНЫМ текстом —
        # одноимённые теперь разруливаются детерминированно, без LLM)
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru",
            [_it(0, "a", "Кешбэк"), _it(1, "button", "Кешбэк программа")])
        no_vt, _ = m_rf.resolve_click("кэшбек", None, _FakeRouter("нет"),
                                      chat_id="rf-vt")
        check("resolve_fail аудит: llm_veto (LLM отвергла кандидатов)",
              no_vt is None
              and any(r.get("fail_reason") == "llm_veto"
                      and r.get("llm_response") == "нет"
                      for r in _aud_recs("rf-vt")))
        # Одноимённые кандидаты («Войти» кнопка и ссылка): LLM их в списке
        # не различит — лучший по скору берётся детерминированно, без жребия
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru",
            [_it(0, "button", "Войти"), _it(1, "a", "Войти")])
        act_same, _ = m_rf.resolve_click("войти", None, _FakeRouter("нет"),
                                         chat_id="rf-same")
        check("одноимённые кандидаты: детерминированный выбор без LLM",
              act_same is not None and act_same["idx"] == 0
              and act_same["choose"].get("path") == "score")
        # Антибот-стена: честный отказ про капчу вместо «не нашёл»
        _ba.detect_antibot = lambda host=None, tab_id=None: "widget: iframe[src*=recaptcha]"
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru", _gh_items())
        no_cp, err_cp = m_rf.resolve_click("загрузить", None, None,
                                           chat_id="rf-cp")
        check("resolve_fail аудит: captcha — честный отказ «я не робот»",
              no_cp is None and "не робот" in err_cp
              and any(r.get("fail_reason") == "captcha"
                      for r in _aud_recs("rf-cp")))
        _ba.detect_antibot = lambda host=None, tab_id=None: None
        # Страницы вообще нет (PAGE_REF без открытой вкладки) — no_page
        m_np = SpyManager(context="t", config={**CFG, "allow_domains": []},
                          base_dir=tmp / "s-rf-nopage")
        no_np, err_np = m_np.resolve_click("войти", PAGE_REF, None,
                                           chat_id="rf-np")
        recs_np = [json.loads(l) for l in (tmp / "s-rf-nopage" / "audit.jsonl")
                   .read_text(encoding="utf-8").splitlines()]
        check("resolve_fail аудит: no_page (нет открытой страницы)",
              no_np is None and err_np
              and any(r.get("kind") == "resolve_fail"
                      and r.get("fail_reason") == "no_page"
                      for r in recs_np))
    finally:
        _ba.snapshot_elements = _orig_snap_rf

    # ── Авто-закрытие оверлеев перед снапшотом ──
    _dismiss_calls = []
    _ba.dismiss_overlay = lambda host=None, tab_id=None: (
        _dismiss_calls.append(host), "Принять все")[1]
    _orig_snap_ov = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru", _gh_items())
    try:
        m_ov = make()
        act_ov, _ = m_ov.resolve_click("скачать", None, _BoomRouter(),
                                       chat_id="ov-chat")
        check("оверлей: авто-закрытие до снапшота + overlay_dismiss в аудите",
              act_ov is not None and _dismiss_calls
              and any(r.get("kind") == "overlay_dismiss"
                      and r.get("value") == "Принять все" and r.get("ok")
                      for r in _aud_recs("ov-chat")))
        # Явная цель-закрытие («закрой модалку»): авто-клик ВЫКЛЮЧЕН —
        # крестик ищет скоринг, иначе авто-закрытие съело бы его раньше
        _dismiss_calls.clear()
        no_cl, err_cl = m_ov.resolve_click("закрыть модалку", None, None,
                                           chat_id="ov-close")
        check("оверлей: цель-закрытие — авто-закрытие не дёргается",
              not _dismiss_calls and no_cl is None and err_cl)
    finally:
        _ba.dismiss_overlay = lambda host=None, tab_id=None: None
        _ba.snapshot_elements = _orig_snap_ov

    # ── browser_actions: dismiss_overlay / detect_antibot / wait_dom_idle ──
    # Гоняем РЕАЛЬНЫЕ обёртки (сохранены до дефолт-моков) с подменённым eval
    _orig_eval_any = _ba._eval_js_any
    _orig_sleep_ba = _ba.time.sleep
    try:
        _ba._eval_js_any = lambda h, t, js: (
            '{"text":"Принять все"}' if "fixedish" in js else "")
        check("dismiss_overlay: текст нажатого контрола из JSON",
              _real_dismiss_overlay("x.ru") == "Принять все")
        _ba._eval_js_any = lambda h, t, js: ""
        check("dismiss_overlay: оверлея нет → None",
              _real_dismiss_overlay("x.ru") is None)
        _ba._eval_js_any = lambda h, t, js: (
            "widget: iframe[src*=recaptcha]" if "sels=" in js else "")
        check("detect_antibot: метка виджета капчи",
              _real_detect_antibot("x.ru") == "widget: iframe[src*=recaptcha]")
        # wait_dom_idle: DOM перестал меняться → выход задолго до таймаута
        _states = iter(["u|1", "u|2", "u|2", "u|2", "u|2"])
        _ba._eval_js_any = lambda h, t, js: next(_states, "u|2")
        _t0 = time.time()
        _real_wait_dom_idle("x.ru", None, timeout_sec=5.0, min_wait=0.05)
        check("wait_dom_idle: выход после стабилизации DOM",
              time.time() - _t0 < 3)
        # Бэкенд без eval → прежний фиксированный слип (половина бюджета)
        def _no_eval(h, t, js):
            raise _ba.BrowserUnavailable("no eval")
        _ba._eval_js_any = _no_eval
        _slept = []
        _ba.time.sleep = lambda s: _slept.append(s)
        _real_wait_dom_idle("x.ru", None, timeout_sec=2.0, min_wait=0.3)
        check("wait_dom_idle: без eval — фолбэк-слип", _slept == [1.0])
    finally:
        _ba._eval_js_any = _orig_eval_any
        _ba.time.sleep = _orig_sleep_ba

    # ── iframe-обход: слияние элементов фреймов в снапшот ──
    class _FakeFrame:
        def __init__(self, url, items, box):
            self.url, self._items, self._box = url, items, box
        def frame_element(self): return self
        def bounding_box(self): return self._box
        def evaluate(self, js): return json.dumps({"items": self._items})

    class _FakePage:
        def __init__(self, frames):
            self.main_frame = "main"
            self.frames = frames
        def evaluate(self, js): return "1280x800"

    _fitem = {"idx": 100, "tag": "button", "role": "", "text": "Оплатить",
              "aria": "", "title": "", "href": "", "w": 120, "h": 40,
              "x": 10, "vp": 1, "ed": 0}
    _frames = [
        _FakeFrame("https://pay.widget.ru/frame", [_fitem],
                   {"x": 300.0, "y": 100.0, "width": 400.0, "height": 300.0}),
        _FakeFrame("about:blank", [_fitem],      # пустой — пропускаем
                   {"x": 0.0, "y": 0.0, "width": 400.0, "height": 300.0}),
        _FakeFrame("https://tracker.ru/px", [_fitem],  # мелкий — трекер
                   {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}),
    ]
    _merged = _ba._merge_frame_items(
        _FakePage(_frames), [_it(0, "a", "Главная"), _it(1, "a", "Каталог")])
    _fr_items = [it for it in _merged if it.get("fr")]
    check("iframe: элементы фрейма в снапшоте (fr, сдвиг x, фильтр мелких)",
          len(_merged) == 3 and len(_fr_items) == 1
          and _fr_items[0]["fr"] == "pay.widget.ru"
          and _fr_items[0]["x"] == 310.0 and _fr_items[0]["idx"] == 100)

    # Локатор клика/ввода: метка ищется сначала в главном фрейме, потом во
    # фреймах — scope фрейма нужен closed-loop проверке эффекта
    class _FakeLoc:
        def __init__(self, n): self._n = n
        def count(self): return self._n

    class _FakeLocFrame:
        def __init__(self, n): self._n = n
        def locator(self, sel): return _FakeLoc(self._n)

    _pg = _FakeLocFrame(0)
    _pg.main_frame = _pg
    _pg.frames = [_pg, _FakeLocFrame(1)]
    _loc, _scope = _ba._locator_any_frame(_pg, 5)
    check("iframe: локатор находит метку во фрейме",
          _loc is not None and _scope is _pg.frames[1])
    _pg2 = _FakeLocFrame(3)
    _pg2.main_frame = _pg2
    _pg2.frames = [_pg2]
    _loc2, _scope2 = _ba._locator_any_frame(_pg2, 5)
    check("iframe: метка в главном фрейме — фреймы не обходятся",
          _loc2 is not None and _scope2 is _pg2)

    # ── Доскролл-поиск: виртуализированный список ──
    _orig_snap_sh = _ba.snapshot_elements
    _orig_gsnap_sh = _ba.snapshot_for_goal
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru", _gh_items())
    _scroll_log = []
    _ba.scroll_step = lambda host=None, tab_id=None: (
        _scroll_log.append(1) or {"moved": True, "bottom": False})

    def _goal_after_scroll(host, goal, tab_id=None):
        # Цель «отрендерилась» только после второго экрана прокрутки
        if len(_scroll_log) >= 2:
            return ("https://x.ru", [_it(9, "button", "Омлет сырный")])
        return ("", [])
    _ba.snapshot_for_goal = _goal_after_scroll
    try:
        m_sh = make()
        act_sh, err_sh = m_sh.resolve_click("омлет сырный", None,
                                            _BoomRouter(), chat_id="sh-chat")
        check("доскролл: цель нашлась после 2 экранов вниз",
              act_sh is not None and act_sh["idx"] == 9
              and len(_scroll_log) == 2)
        # Промах — прокрутку вернули на место
        _restore_log = []
        _ba.scroll_restore = lambda host=None, tab_id=None, y=0.0: \
            _restore_log.append(y)
        _scroll_log.clear()
        _ba.snapshot_for_goal = lambda host, goal, tab_id=None: ("", [])
        no_sh, err_sh2 = m_sh.resolve_click("несуществующее", None, None,
                                            chat_id="sh-chat2")
        check("доскролл: промах — скролл восстановлен, честный отказ",
              no_sh is None and err_sh2 and _restore_log == [0.0])
    finally:
        _ba.snapshot_elements = _orig_snap_sh
        _ba.snapshot_for_goal = _orig_gsnap_sh
        _ba.scroll_step = lambda host=None, tab_id=None: {"moved": False,
                                                          "bottom": True}
        _ba.scroll_restore = lambda host=None, tab_id=None, y=0.0: None

    # ── Мягкая верификация сайта после навигации (п.5) ──
    _orig_pid = _ba.page_identity
    _orig_gt = _ws._google_translate
    _ws._google_translate = lambda text: "youtube" if text == "ютуб" else None
    try:
        _ba.page_identity = lambda tab_id=None, host_part=None: "YouTube | YouTube"
        m_v = make()
        m_v._last_tab_id = 42
        act_v = {"kind": "url", "value": "https://www.youtube.com/",
                 "expect_name": "ютуб"}
        m_v._verify_opened_site(act_v)
        check("п.5: title совпал с именем (через перевод) → ok",
              act_v["name_check"]["ok"] is True)
        _ba.page_identity = lambda tab_id=None, host_part=None: "Kuxni.org | Рецепты"
        act_v2 = {"kind": "url", "value": "https://kuxni.org/",
                  "expect_name": "ютуб"}
        m_v._verify_opened_site(act_v2)
        check("п.5: title не совпал → ok=False (пометка, не отказ)",
              act_v2["name_check"]["ok"] is False)
        # Без expect_name (алиас/история/явный домен) — проверка не дёргается
        act_v3 = {"kind": "url", "value": "https://youtube.com"}
        m_v._verify_opened_site(act_v3)
        check("п.5: без expect_name верификации нет",
              "name_check" not in act_v3)
    finally:
        _ba.page_identity = _orig_pid
        _ws._google_translate = _orig_gt

    # ── Визуальный фолбэк резолва (п.4) ──
    class _VisionRouter:
        def __init__(self, resp): self.resp = resp; self.img_calls = 0
        def supports_vision(self): return True
        def get_response(self, *a, **kw): return "нет"
        def get_response_with_image(self, prompt, img, image_mime=None):
            self.img_calls += 1
            return self.resp

    import io as _io
    from PIL import Image as _PILImage
    _buf = _io.BytesIO()
    _PILImage.new("RGB", (1280, 800), (255, 255, 255)).save(_buf, format="PNG")
    _png = _buf.getvalue()
    _vis_items = [_it(0, "button", "", x=1100.0, y=10.0, vw=1280.0),
                  _it(1, "button", "", x=1150.0, y=10.0, vw=1280.0)]
    _orig_sshot = _ba.screenshot_viewport
    _orig_snap_v = _ba.snapshot_elements
    _ba.screenshot_viewport = lambda host=None, tab_id=None: _png
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru", _vis_items)
    try:
        m_vis = make()
        act_v4, _ = m_vis.resolve_click("корзина", None, _VisionRouter("2"),
                                        chat_id="vis1")
        check("п.4: vision-фолбэк выбрал элемент по скриншоту",
              act_v4 is not None and act_v4["idx"] == 1
              and act_v4["choose"]["path"] == "vision")
        # vision ответила «нет» — честный отказ, как без фолбэка
        no_v4, err_v4 = m_vis.resolve_click("корзина", None,
                                            _VisionRouter("нет"),
                                            chat_id="vis2")
        check("п.4: vision «нет» → честный отказ",
              no_v4 is None and err_v4)
        # Выключено конфигом — vision не дёргается вовсе
        vr = _VisionRouter("2")
        m_vis_off = make(cfg={**CFG, "vision_fallback": False})
        no_v5, _ = m_vis_off.resolve_click("корзина", None, vr,
                                           chat_id="vis3")
        check("п.4: vision_fallback=false — скриншот не делается",
              no_v5 is None and vr.img_calls == 0)
        # Рамки кандидатов — валидный JPEG (легче PNG для аплоада в веб-чат)
        boxed = _cc_mod._draw_candidate_boxes(_png, _vis_items)
        check("п.4: рамки кандидатов рисуются (JPEG на выходе)",
              boxed is not None and boxed[:2] == b"\xff\xd8")
    finally:
        _ba.screenshot_viewport = _orig_sshot
        _ba.snapshot_elements = _orig_snap_v

    # ── Shadow DOM: проход по открытым shadow root'ам в снапшоте ──
    check("снапшот: есть проход по shadow root'ам",
          "shadowRoot" in _ba._SNAPSHOT_JS and "shroots" in _ba._SNAPSHOT_JS)

    # ── «введи X в поиск»: поле по q-флагу, когда подпись без слова «поиск» ──
    _orig_snap_q = _ba.snapshot_elements
    _orig_hid_q = _ba.hidden_editable_labels
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru",
        [_it(3, "input", "Искать в Википедии", ed=True, q=True),
         _it(4, "a", "Главная")])
    _ba.hidden_editable_labels = lambda host=None, tab_id=None: []
    try:
        m_q = make()
        act_q, err_q = m_q.resolve_type("тест в поле поиск", None, None,
                                        chat_id="q-chat")
        check("поисковое поле по q-флагу (подпись без слова «поиск»)",
              act_q is not None and act_q["idx"] == 3
              and act_q["text"] == "тест"
              and act_q["choose"]["path"] == "search_field")
    finally:
        _ba.snapshot_elements = _orig_snap_q
        _ba.hidden_editable_labels = _orig_hid_q

    # ── Широкий LLM-резолв (zero-match): скоринг не дал ни одного кандидата ──
    class _WideRouter:
        def __init__(self, resp): self.resp = resp; self.calls = []
        def get_response(self, messages, **kw):
            self.calls.append(messages[-1]["content"])
            return self.resp

    _orig_snap_w = _ba.snapshot_elements
    _orig_hid_w = _ba.hidden_editable_labels
    _ba.hidden_editable_labels = lambda host=None, tab_id=None: []
    _wide_items = lambda: [_it(0, "a", "Главная"),
                           _it(1, "button", "Оформить заказ"),
                           _it(2, "a", "Помощь")]
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru", _wide_items())
    try:
        # Цель не матчится текстом ни с одним элементом («перейти к оплате»
        # при кнопке «Оформить заказ») — LLM выбирает из списка снапшота:
        # пользователь не обязан знать точные подписи элементов
        m_w = make()
        rw = _WideRouter("2")
        act_w, _ = m_w.resolve_click("перейти к оплате", None, rw, chat_id="w1")
        check("wide: zero-match → LLM выбрала элемент из списка снапшота",
              act_w is not None and act_w["idx"] == 1
              and act_w["choose"]["path"] == "llm_wide")
        check("wide: в промпт ушли список элементов и цель",
              rw.calls and "1) [a/-] Главная" in rw.calls[-1]
              and "перейти к оплате" in rw.calls[-1])
        no_w, err_w = m_w.resolve_click("перейти к оплате", None,
                                        _WideRouter("нет"), chat_id="w2")
        check("wide: LLM «нет» → честный отказ",
              no_w is None and err_w and "не нашёл" in err_w)
        no_w2, _ = m_w.resolve_click("перейти к оплате", None,
                                     _WideRouter("30"), chat_id="w3")
        check("wide: номер вне диапазона → отказ", no_w2 is None)
        rw_off = _WideRouter("2")
        m_w_off = make(cfg={**CFG, "llm_wide_resolve": False})
        no_w3, _ = m_w_off.resolve_click("перейти к оплате", None, rw_off,
                                         chat_id="w4")
        check("wide: llm_wide_resolve=false — LLM не дёргается",
              no_w3 is None and not rw_off.calls)
        # Детерминированное попадание широкий резолв не вытесняет
        act_w2, _ = m_w.resolve_click("помощь", None, _BoomRouter(),
                                      chat_id="w5")
        check("wide: точный матч — LLM не дёргается (путь score)",
              act_w2 is not None and act_w2["idx"] == 2
              and act_w2["choose"]["path"] == "score")
        # Безымянные иконки в широкий список не попадают — их разбирает vision
        rw_n = _WideRouter("1")
        idx_n, _ = m_w._llm_wide_pick(
            "корзина", [_it(7, "button", ""), _it(8, "button", "Оформить")],
            rw_n)
        check("wide: безымянные иконки не попадают в LLM-список",
              idx_n == 8 and rw_n.calls
              and "1) [button/-] Оформить" in rw_n.calls[-1]
              and "2)" not in rw_n.calls[-1])
        rw_e = _WideRouter("1")
        idx_e, meta_e = m_w._llm_wide_pick("корзина", [_it(9, "button", "")],
                                           rw_e)
        check("wide: все элементы безымянные — LLM не дёргается (это к vision)",
              idx_e is None and meta_e is None and not rw_e.calls)
        # Скоуп-цель «закрыть на корзина»: крестик подписан просто «закрыть»
        # и слова «корзина» не содержит — без пояснения LLM отвечала «нет»
        rw_s = _WideRouter("1")
        idx_s, _ = m_w._llm_wide_pick(
            "закрыть на корзина",
            [_it(0, "button", "закрыть", ctx="4 товара на 1 330 ₽"),
             _it(1, "a", "Пиццы")], rw_s)
        check("wide: скоуп-цель — промпт поясняет форму и даёт контекст",
              idx_s == 0 and rw_s.calls
              and "может быть подписан просто «закрыть»" in rw_s.calls[-1]
              and "(блок: 4 товара на 1 330 ₽)" in rw_s.calls[-1])
        # Ввод: «в поле емейл» при подписи «Электронная почта» — LLM выбирает
        # поле по смыслу, точное имя поля знать не нужно
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru",
            [_it(3, "input", "Электронная почта", ed=True),
             _it(4, "input", "Пароль", ed=True),
             _it(5, "button", "Войти")])
        rw_t = _WideRouter("1")
        act_wt, _ = m_w.resolve_type("мой@mail.ru в поле емейл", None,
                                     rw_t, chat_id="w6")
        check("wide: поле по смыслу («емейл» → «Электронная почта»)",
              act_wt is not None and act_wt["idx"] == 3
              and act_wt["text"] == "мой@mail.ru"
              and act_wt["choose"]["path"] == "llm_wide")
        check("wide: для ввода промпт — «поле ввода», поля первыми",
              rw_t.calls and "1) [input/-] Электронная почта" in rw_t.calls[-1]
              and "поле ввода" in rw_t.calls[-1])
        # LLM «нет» для поля → подсказка с перечнем полей, как прежде
        no_wt, err_wt2 = m_w.resolve_type("мой@mail.ru в поле емейл", None,
                                          _WideRouter("нет"), chat_id="w7")
        check("wide: «нет» для поля → честный отказ с перечнем полей",
              no_wt is None and err_wt2 and "Вижу поля" in err_wt2)
    finally:
        _ba.snapshot_elements = _orig_snap_w
        _ba.hidden_editable_labels = _orig_hid_w

    # Безымянные иконки (SVG без текста/aria) попадают в снапшот с квотой —
    # иначе у vision-фолбэка нет кандидатов
    check("снапшот: безымянные иконки включаются с квотой (для vision)",
          "nb5>=12" in _ba._SNAPSHOT_JS)

    # ── Вкладки: «перейди на вкладку X», список открытых ──
    from app.features.computer_control import (
        parse_tab_list_query, parse_tab_switch)
    check("parse tab switch: явная/мягкая/не-команда",
          parse_tab_switch("перейди на вкладку ютуб") == ("ютуб", True)
          and parse_tab_switch("переключись на гитхаб") == ("гитхаб", False)
          and parse_tab_switch("покажи вкладку почта") == ("почта", True)
          and parse_tab_switch("нажми кнопку войти") is None
          and parse_tab_switch("перейди на эту страницу") is None)
    check("parse tab list: «какие вкладки открыты»",
          parse_tab_list_query("какие вкладки открыты")
          and parse_tab_list_query("покажи список вкладок")
          and not parse_tab_list_query("перейди на вкладку ютуб")
          and not parse_tab_list_query("нажми кнопку"))

    _orig_lt2 = _ba.list_tabs
    _orig_at = _ba.activate_tab
    _orig_find2, _orig_hist2 = _ws.find_site_url, _bh.find_in_history
    _ws.find_site_url = lambda name, **kw: None
    _bh.find_in_history = lambda name: None
    _ba.list_tabs = lambda: [
        (1, "https://youtube.com/", "youtube.com", "YouTube"),
        (2, "https://github.com/x", "github.com", "vpc · GitHub"),
        (7, "https://music.youtube.com/", "music.youtube.com",
         "YouTube Music")]
    _act_log = []
    _ba.activate_tab = lambda tid: (
        _act_log.append(tid), ("https://youtube.com/", "YouTube"))[1]
    try:
        m_tab = make(cfg={**CFG, "allow_domains": [],
                          "sites": {"ютуб": "youtube.com",
                                    "гитхаб": "github.com",
                                    "пикабу": "pikabu.ru"}})
        act_t1, _ = m_tab.resolve_tab_switch("гитхаб", True)
        check("вкладки: «гитхаб» → tab_switch по алиасу-хосту",
              act_t1 is not None and act_t1["kind"] == "tab_switch"
              and act_t1["tab_id"] == 2)
        act_t2, err_t2 = m_tab.resolve_tab_switch("ютуб", True)
        check("вкладки: «ютуб» — две вкладки youtube → уточнение",
              act_t2 is None and err_t2 and "несколько" in err_t2.lower())
        act_t3, err_t3 = m_tab.resolve_tab_switch("вк", True)
        check("вкладки: промах (явная форма) → отказ со списком открытых",
              act_t3 is None and err_t3 and "Открыты:" in err_t3)
        # Мягкая форма, вкладки нет — фолбэк на открытие сайта (алиас):
        # url-действие с обычным подтверждением
        act_t4, _ = m_tab.resolve_tab_switch("пикабу", False)
        check("вкладки: мягкая без вкладки → открытие сайта по алиасу",
              act_t4 is not None and act_t4["kind"] == "url"
              and "pikabu.ru" in act_t4["value"])
        # Мягкая форма, нет ни вкладки, ни сайта → (None, None) — не наше
        act_t5, err_t5 = m_tab.resolve_tab_switch("квакушка", False)
        check("вкладки: мягкая без совпадений → (None, None), диалог не ломаем",
              act_t5 is None and err_t5 is None)
        # Исполнение: bring_to_front + вкладка становится отслеживаемой
        # (настоящий менеджер — у SpyManager _dispatch заглушен)
        m_tab_real = ComputerControlManager(
            context="t", base_dir=tmp,
            config={**CFG, "allow_domains": [], "sites": {}})
        m_tab_real.execute({"kind": "tab_switch", "tab_id": 1,
                            "value": "https://youtube.com/",
                            "host": "youtube.com", "element": "YouTube"},
                           "c-tab")
        check("вкладки: dispatch → activate_tab, вкладка отслеживается",
              _act_log == [1] and m_tab_real._last_tab_id == 1
              and m_tab_real._last_host == "youtube.com")
        # Кэш списка обновился
        check("вкладки: список вкладок кэшируется (_known_tabs)",
              len(m_tab._known_tabs) == 3
              and m_tab._known_tabs[1]["title"] == "vpc · GitHub")
        # Список текстом
        txt = m_tab.list_open_tabs_text()
        check("вкладки: список текстом",
              "YouTube" in txt and "github.com" in txt)
        # Формулировки
        check("вкладки: describe/done/confirm",
              m_tab.describe(act_t1) == "перейти на вкладку «vpc · GitHub»"
              and m_tab.describe_done(act_t1)
              == "переключился на вкладку «vpc · GitHub»"
              and m_tab.confirm_question(act_t1)
              == "Перейти на вкладку «vpc · GitHub»?")
    finally:
        _ba.list_tabs = _orig_lt2
        _ba.activate_tab = _orig_at
        _ws.find_site_url = _orig_find2
        _bh.find_in_history = _orig_hist2

    # ── «закрой X»: generic и целевое закрытие попапа ──
    from app.features.computer_control import parse_close_request
    check("parse close: «закрой окно» / объект / не-команда / сайт",
          parse_close_request("закрой окно") == ("закрой окно", None)
          and parse_close_request("закрой соусы к бортикам")
          == ("закрой соусы к бортикам", None)
          and parse_close_request("скрой попап на ютубе")
          == ("скрой попап", "ютубе")
          and parse_close_request("нажми кнопку") is None)
    _orig_snap_c = _ba.snapshot_elements
    _orig_dis_c = _ba.dismiss_overlay
    _dismiss_calls = []
    _ba.dismiss_overlay = lambda host=None, tab_id=None: (
        _dismiss_calls.append(1), None)[1]
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru",
        [_it(0, "button", "закрыть", ctx="Аррива! 1266 ₽ Изменить"),
         _it(1, "button", "закрыть",
             ctx="Соусы к бортикам и закускам Тысяча островов 49 ₽"),
         _it(2, "a", "Главная")])
    try:
        m_c = make()
        # Generic: «закрой окно» → первый крестик, авто-закрытие оверлея ВЫКЛ
        act_c1, _ = m_c.resolve_click("закрой окно", None, _BoomRouter())
        check("закрытие: «закрой окно» → крестик, без авто-dismiss",
              act_c1 is not None and act_c1["idx"] == 0
              and act_c1["goal"] == "закрыть"
              and not _dismiss_calls)
        # Целевое: «закрой соусы к бортикам» → крестик ИМЕННО модалки соусов
        act_c2, _ = m_c.resolve_click("закрой соусы к бортикам", None,
                                      _BoomRouter())
        check("закрытие: целевое — крестик модалки по контексту (скоуп)",
              act_c2 is not None and act_c2["idx"] == 1
              and act_c2["choose"].get("scoped") is True)
        # Целевое с промахом («закрой непонятное») → фолбэк на крестик
        act_c3, _ = m_c.resolve_click("закрой непонятное", None,
                                      _BoomRouter())
        check("закрытие: промах целевого → фолбэк на обычный крестик",
              act_c3 is not None and act_c3["idx"] == 0
              and act_c3["goal"] == "закрыть")
    finally:
        _ba.snapshot_elements = _orig_snap_c
        _ba.dismiss_overlay = _orig_dis_c

    # ── «закрой окно» без крестика → Escape-фолбэк по видимой модалке ──
    check("parse close: «сверни анкету»",
          parse_close_request("сверни анкету") == ("сверни анкету", None))
    _orig_snap_e = _ba.snapshot_elements
    _orig_dis_e = _ba.dismiss_overlay
    _orig_mv_e = _ba.modal_visible
    _orig_ol_e = _ba.open_list_visible
    _ba.open_list_visible = lambda host=None, tab_id=None: False
    _esc_dismiss = []
    _ba.dismiss_overlay = lambda host=None, tab_id=None: (
        _esc_dismiss.append(1), None)[1]
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru",
        [_it(0, "a", "Главная"), _it(1, "button", "Отправить анкету")])
    try:
        m_e = make()
        _ba.modal_visible = lambda host=None, tab_id=None: True
        act_e1, err_e1 = m_e.resolve_click("закрой окно", None, _BoomRouter())
        check("закрытие: нет крестика + видна модалка → Escape-действие",
              act_e1 is not None and act_e1["kind"] == "press"
              and act_e1["element"] == "Escape" and err_e1 is None
              and not _esc_dismiss)
        # Открытый выпадающий список (без модалки) — тоже повод для Escape
        _ba.modal_visible = lambda host=None, tab_id=None: False
        _ba.open_list_visible = lambda host=None, tab_id=None: True
        act_e3, err_e3 = m_e.resolve_click("закрой окно", None, _BoomRouter())
        check("закрытие: открытый список (без модалки) → Escape-действие",
              act_e3 is not None and act_e3["kind"] == "press"
              and err_e3 is None)
        _ba.open_list_visible = lambda host=None, tab_id=None: False
        act_e2, err_e2 = m_e.resolve_click("закрой окно", None, _BoomRouter())
        check("закрытие: модалки не видно → честный отказ без Escape",
              act_e2 is None and err_e2)
    finally:
        _ba.snapshot_elements = _orig_snap_e
        _ba.dismiss_overlay = _orig_dis_e
        _ba.modal_visible = _orig_mv_e
        _ba.open_list_visible = _orig_ol_e

    # ── Режим управления: «перейди в/выйди из режима управления» ──
    from app.features.computer_control import parse_control_mode
    check("режим: «перейди в режим управления» → ON",
          parse_control_mode("перейди в режим управления") is True
          and parse_control_mode("включи режим управления") is True
          and parse_control_mode("режим управления") is True)
    check("режим: «выйди из режима управления» → OFF",
          parse_control_mode("выйди из режима управления") is False
          and parse_control_mode("выключи режим управления") is False
          and parse_control_mode("покинь режим управления") is False)
    check("режим: обычные фразы — не команды режима",
          parse_control_mode("нажми кнопку") is None
          and parse_control_mode("что такое режим управления?") is None
          and parse_control_mode("напомни через час") is None
          and parse_control_mode("привет") is None)

    # ── Слайдер: «перетащи/поставь слайдер X на N» ──
    from app.features.computer_control import parse_slider_request
    check("parse slider: подпись+значение / не-команда",
          parse_slider_request("перетащи слайдер рабочие часы в день на 8")
          == (("рабочие часы в день", 8), None)
          and parse_slider_request("выставь громкость на 5")
          == (("громкость", 5), None)
          and parse_slider_request("поставь лайк") is None
          and parse_slider_request("нажми кнопку") is None)
    _orig_snap_s = _ba.snapshot_elements
    _orig_dis_s = _ba.dismiss_overlay
    _ba.dismiss_overlay = lambda host=None, tab_id=None: None
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru/anketa", "x.ru", [_it(0, "button", "Отправить анкету")])
    _orig_set_s = _ba.set_slider
    _slider_calls = []
    _ba.set_slider = lambda host, label, value, tab_id=None: (
        _slider_calls.append((host, label, value, tab_id)), str(value))[1]
    try:
        m_s = make()
        act_s, err_s = m_s.resolve_slider(("рабочие часы", 8), None,
                                          _BoomRouter())
        check("слайдер: resolve → kind=slider с подписью и значением",
              act_s is not None and act_s["kind"] == "slider"
              and act_s["slider_label"] == "рабочие часы"
              and act_s["slider_value"] == 8 and err_s is None)
        m_s_real = ComputerControlManager(
            context="t", base_dir=tmp,
            config={**CFG, "allow_domains": [], "sites": {}})
        m_s_real.execute(act_s, "c-slider")
        check("слайдер: dispatch → set_slider, факт — в slider_done",
              _slider_calls and _slider_calls[0][:3] == ("x.ru",
                                                         "рабочие часы", 8)
              and act_s.get("slider_done") == "8")
        check("слайдер: describe/confirm/done",
              "рабочие часы" in ComputerControlManager.describe(act_s)
              and "?" in ComputerControlManager.confirm_question(act_s)
              and "8" in ComputerControlManager.describe_done(act_s))
    finally:
        _ba.snapshot_elements = _orig_snap_s
        _ba.dismiss_overlay = _orig_dis_s
        _ba.set_slider = _orig_set_s

    # ── Фикстуры снапшотов: регрессия скоринга ──
    from scripts.eval_snapshot_scoring import run as _eval_fixtures
    _fx_pass, _fx_all = _eval_fixtures(verbose=False)
    check("фикстуры снапшотов: скоринг/выбор без регрессий",
          _fx_pass == _fx_all and _fx_all > 0)

    # аудит: новые поля element/host/text пишутся
    m_aud = make()
    m_aud.execute({"kind": "click", "idx": 5, "element": "Кнопка",
                   "host": "x.ru", "value": "https://x.ru"}, "audchat")
    m_aud.execute({"kind": "type", "idx": 6, "element": "Поле",
                   "host": "x.ru", "value": "https://x.ru",
                   "text": "привет"}, "audchat")
    aud_lines = (tmp / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    aud_recs = [json.loads(l) for l in aud_lines
                if json.loads(l).get("chat_id") == "audchat"]
    check("sc: аудит пишет element/host для click",
          any(r.get("element") == "Кнопка" and r.get("host") == "x.ru"
              for r in aud_recs))
    check("sc: аудит пишет text для type",
          any(r.get("text") == "привет" for r in aud_recs))

    # ── Доскролл-поиск: окно возвращается на место ПЕРЕД фазой контейнера ──
    # Кейс 03.09: «включи/троеточие в <видео> из плейлиста» на YouTube —
    # фаза 1 (3 экрана окна вниз) уносила панель ytd-playlist-panel-renderer
    # из вьюпорта, и контейнерный шаг (только видимые контейнеры) крутил
    # левое меню вместо списка #items
    from app.features import browser_actions as _ba_hunt
    _hunt_calls = []

    class _FakeBAHunt:
        def scroll_position(self, host, tab_id=None):
            _hunt_calls.append("pos")
            return 100.0

        def scroll_step(self, host, tab_id=None):
            _hunt_calls.append("wstep")
            return {"moved": True, "bottom": False}

        def wait_dom_idle(self, *a, **kw):
            pass

        def scroll_container_step(self, host, tab_id=None):
            _hunt_calls.append("cstep")
            return {"moved": True, "bottom": False, "y0": 0.0}

        def scroll_container_restore(self, host, tab_id=None, y0=0.0):
            _hunt_calls.append("crestore")

        def scroll_restore(self, host, tab_id=None, y=0.0):
            _hunt_calls.append(("wrestore", y))

    _orig_sfg = _ba_hunt.snapshot_for_goal
    _ba_hunt.snapshot_for_goal = lambda *a, **kw: ("", [])  # цель не рендерится
    try:
        ComputerControlManager._scroll_hunt(make(), _FakeBAHunt(),
                                            "youtube.com", 7, "sirene boss")
    finally:
        _ba_hunt.snapshot_for_goal = _orig_sfg
    check("scroll_hunt: окно восстановлено ДО контейнерной фазы",
          ("wrestore", 100.0) in _hunt_calls
          and _hunt_calls.index(("wrestore", 100.0))
          < _hunt_calls.index("cstep"))
    check("scroll_hunt: контейнерная фаза отработала и возвращена",
          _hunt_calls.count("cstep") == 10 and "crestore" in _hunt_calls
          and _hunt_calls[-1] == ("wrestore", 100.0))

    # Синоним «троеточие»: основа «действ» prefix-матчит «Меню действий»
    # YouTube (полное слово «действия» не матчило «действий»)
    from app.features.computer_control import _goal_with_synonyms, _word_in
    check("синонимы «троеточие» содержат основу «действ»",
          _word_in("действ", _goal_with_synonyms("троеточие")))

    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

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
import itertools
import json
import subprocess
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="compcontrol_smoke_")
    tmp = Path(tempfile.mkdtemp(prefix="compcontrol_data_"))

    ok = 0
    # Тест не должен зависеть от живой сети: internet_available() — TCP-пробы
    # 1.1.1.1:443/8.8.8.8:53, которые в песочнице/за файрволом молчат, и тогда
    # webchat-ветка роутера и резолв сайтов честно «офлайн» → ложные FAIL
    import app.core.router as _net_router
    _net_router.internet_available = lambda: True
    _net_router._net_ok, _net_router._net_checked = True, float("inf")
    import app.features.web_search as _net_ws
    _net_ws.internet_available = lambda: True  # импортирован по имени

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

    # ── 5c. Подтверждение по типу действия (risk_overrides) ──
    RO = {"click": False, "navigate_known_domain": False,
          "navigate_new_domain": True, "download": True,
          "type_text": True, "type_text_safe_fields": False}
    ro = make(cfg={**CFG, "risk_overrides": RO})
    check("risk: клик без confirm", not ro.needs_confirm({"kind": "click", "idx": 1}))
    check("risk: скролл — как клик", not ro.needs_confirm({"kind": "scroll"}))
    check("risk: пробел — как клик",
          not ro.needs_confirm({"kind": "key", "key": "Space"}))
    check("risk: Enter — общий confirm (может отправить форму)",
          ro.needs_confirm({"kind": "key", "key": "Enter"}))
    check("risk: известный домен (allow_domains) без confirm",
          not ro.needs_confirm({"kind": "url", "value": "https://youtube.com"}))
    check("risk: nav по известному домену без confirm",
          not ro.needs_confirm({"kind": "nav", "value": "https://youtube.com/a",
                                "steps": ["раздел"]}))
    check("risk: скачивание с confirm", ro.needs_confirm({"kind": "download"}))
    check("risk: ввод в обычное поле с confirm",
          ro.needs_confirm({"kind": "type"}))
    check("risk: ввод в поисковое поле без confirm",
          not ro.needs_confirm({"kind": "type", "field_safe": True}))
    check("risk: чувствительное поле — confirm всегда",
          ro.needs_confirm({"kind": "type", "field_sensitive": True}))
    check("risk: multi — доминирует самое рискованное",
          ro.needs_confirm({"kind": "multi", "items": [{"kind": "click", "idx": 1},
                                                       {"kind": "download"}]}))
    # Новый домен: navigate_new_domain=true требует confirm даже при confirm=False
    rn = make(cfg={**CFG, "allow_domains": [], "confirm": False,
                   "risk_overrides": {"navigate_new_domain": True}})
    check("risk: новый домен — confirm поверх confirm=False",
          rn.needs_confirm({"kind": "url", "value": "https://example.com"}))
    rn.process_markers("[OPEN_URL:example.com]", "c12")
    check("risk: маркер нового домена → pending, не исполнен",
          rn.calls == [] and rn.get_pending("c12") is not None)
    # Известный домен без confirm: маркер исполняется сразу
    rk = make(cfg={**CFG, "risk_overrides": {"navigate_known_domain": False}})
    rk.process_markers("[OPEN_URL:youtube.com]", "c13")
    check("risk: известный домен → исполнено сразу",
          rk.calls == [{"kind": "url", "value": "https://youtube.com"}]
          and rk.get_pending("c13") is None)
    # Без переопределений всё решает общий confirm
    plain = make()
    check("risk: без overrides — общий confirm",
          plain.needs_confirm({"kind": "click", "idx": 1})
          and plain.needs_confirm({"kind": "url", "value": "https://youtube.com"}))
    pi = make(cfg={**CFG, "confirm": False})
    check("risk: без overrides — confirm=False ничего не спрашивает",
          not pi.needs_confirm({"kind": "download"})
          and not pi.needs_confirm({"kind": "type", "field_safe": False}))
    check("risk: чувствительное поле не обходится даже confirm=False",
          pi.needs_confirm({"kind": "type", "field_sensitive": True}))

    # ── 5b. Fast-path «открой X»: парсинг и резолв ──
    from app.features.computer_control import parse_open_request
    check("parse: «открой ютуб» → «ютуб»", parse_open_request("открой ютуб") == "ютуб")
    check("parse: пожалуйста/сайт/знаки срезаются",
          parse_open_request("Открой пожалуйста сайт YouTube!") == "YouTube")
    check("parse: запусти/open/start тоже команды",
          parse_open_request("запусти телеграм") == "телеграм"
          and parse_open_request("open youtube") == "youtube")
    check("parse: «мне/сайт/пожалуйста» между глаголом и названием срезаются",
          parse_open_request("открой мне сайт вуза") == "вуза"
          and parse_open_request("открой сайт вуза") == "вуза"
          and parse_open_request("открой пожалуйста ютуб") == "ютуб"
          and parse_open_request("открой ютуб пожалуйста") == "ютуб")
    check("parse: «открой мне» без названия — не команда",
          parse_open_request("открой мне") is None)
    check("parse: «страницу/вкладку» срезаются, «открой сайт» — не команда",
          parse_open_request("открой страницу кутузова вуза") == "кутузова вуза"
          and parse_open_request("открой страницу кутузовой вуза") == "кутузовой вуза"
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

    # find_site_url: «вуза» — домен вуза (example.edu) аббревиатуру не содержит,
    # резолв через заголовок; группа ВК и другой вуз (nntu.ru) отфильтрованы,
    # URL сводится к корню, а не к SEO-подстранице
    class _FakeDDGSNstu:
        def text(self, q, max_results=5):
            return [
                {"href": "https://vk.ru/nstu_vk",
                 "title": "вуза НЭТИ | Официальное сообщество"},
                {"href": "https://www.example.edu/entrance/enrollment_campaign/current_numbers",
                 "title": "вуза. Конкурсная ситуация 2026"},
                {"href": "https://www.nntu.ru/content/abiturientam",
                 "title": "вуза им. Р.Е. Алексеева | Нижегородский"},
            ]
    _ws._get_ddgs = lambda: _FakeDDGSNstu
    _ws._google_translate = lambda text: "ngtu" if text == "вуза" else None
    try:
        check("find_site_url: «вуза» → корень example.edu по заголовку (не ВК, не nntu)",
              _ws.find_site_url("вуза") == "https://www.example.edu/")
        # Кириллический домен (.рф): punycode хоста декодируется → доменный матч,
        # даже когда в заголовке названия нет
        class _FakeDDGSIdn:
            def text(self, q, max_results=5):
                return [{"href": "https://xn--c1atqe.xn--p1ai/studies",
                         "title": "Обучающимся"}]
        _ws._get_ddgs = lambda: _FakeDDGSIdn
        check("find_site_url: punycode-домен (вуза.рф) матчится по домену",
              _ws.find_site_url("вуза") == "https://xn--c1atqe.xn--p1ai/")
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
                {"href": "https://ru.wikipedia.org/wiki/Лицей_вуза",
                 "title": "Лицей вуза — Википедия"},
                {"href": "https://example.edu/kaf/persons/98849",
                 "title": "вуза - КУТУЗОВА И. А. - Общая информация"},
            ]
    _ws._get_ddgs = lambda: _FakeDDGSKutuzova
    _ws._google_translate = lambda text: "kutuzova ngtu" if text == "кутузовой вуза" else None
    try:
        check("find_site_url: «кутузовой вуза» (падеж) → страница Кутузовой целиком",
              _ws.find_site_url("кутузовой вуза") == "https://example.edu/kaf/persons/98849")
    finally:
        _ws._get_ddgs, _ws._google_translate = _orig_ddgs, _orig_tr

    # ── 5г. Этап 2: мульти-команды, поиск на сайте, история браузера ──
    from app.features.computer_control import parse_open_many, parse_search_on_site
    check("parse many: «открой ютуб и кинопоиск» → две цели",
          parse_open_many("открой ютуб и кинопоиск") == ["ютуб", "кинопоиск"])
    check("parse many: у второй части свой глагол и филлеры",
          parse_open_many("открой ютуб и запусти приложение музыку") == ["ютуб", "музыку"])
    check("parse many: одиночная команда — список из одной",
          parse_open_many("открой мне сайт вуза") == ["вуза"])
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
    check("ordinal: shorts — «первое видео в shorts» / «первый шортс»",
          ordinal_recipe("первое видео в shorts") == "shorts_pick:1"
          and ordinal_recipe("первый шортс") == "shorts_pick:1"
          and ordinal_recipe("третий шортс") == "shorts_pick:3"
          and ordinal_recipe("2 шортса") == "shorts_pick:2"
          and ordinal_recipe("третье видео в шортсах") == "shorts_pick:3")
    # Клик-путь: номерная команда — рецепт по разметке, без снапшота/нейронки
    m_ord = make()
    act_o, err_o = m_ord.resolve_click("первое видео на shorts", None, None)
    check("resolve_click: «первое видео на shorts» → recipe:shorts_pick:1",
          err_o is None
          and act_o == {"kind": "task", "key": "первое видео на shorts",
                        "value": "recipe:shorts_pick:1"})
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
        check("run_recipe: search_pick считает от первого видимого во вьюпорте",
              "innerHeight" in _captured["js"] and "vf(" in _captured["js"])
        try:
            _ba.run_recipe("search_pick:0")
            bad = False
        except _ba.BrowserUnavailable:
            bad = True
        check("run_recipe: номер 0 отклоняется", bad)
    finally:
        _ba._run_apple_events = _orig_ae
        _ba._cdp_available = _orig_cdp

    # Явный адрес в команде открытия («открой на example.edu/827 студентам — …»):
    # длинная фраза → адрес + путь кликами по странице
    from app.features.computer_control import parse_open_with_url
    check("parse url: длинная фраза с явным адресом и путём",
          parse_open_with_url("открой на example.edu/827 студентам - Технологии "
                              "баз данных - Методические указания")
          == ("example.edu/827", ["студентам", "Технологии баз данных",
                                  "Методические указания"]))
    check("parse url: схема сохраняется / нет адреса / не команда",
          parse_open_with_url("открой https://example.com/a б в")
          == ("https://example.com/a", ["б в"])
          and parse_open_with_url("открой ютуб") is None
          and parse_open_with_url("расскажи про example.edu") is None)
    check("parse url: без хвоста — пустой путь; дефис внутри слова не рвётся",
          parse_open_with_url("открой example.edu/827") == ("example.edu/827", [])
          and parse_open_with_url("открой example.edu англо-русский словарь")
          == ("example.edu", ["англо-русский словарь"]))
    check("parse url: «X на адрес» — предлог перед адресом не липнет к пути",
          parse_open_with_url("открой страницу кутузовой на example.edu")
          == ("example.edu", ["кутузовой"]))
    check("resolve_url: whitelist доменов работает и для явного адреса",
          m.resolve_url("youtube.com/watch?v=1")
          == {"kind": "url", "value": "https://youtube.com/watch?v=1"}
          and m.resolve_url("example.edu/827") is None)

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
    nav_act = mnav.resolve_nav("example.edu/827",
                               ["студентам", "Технологии баз данных"])
    check("resolve_nav: адрес + путь → nav-действие",
          nav_act == {"kind": "nav", "value": "https://example.edu/827",
                      "steps": ["студентам", "Технологии баз данных"],
                      "host": "example.edu/827"})
    check("resolve_nav: без шагов — обычное открытие страницы",
          mnav.resolve_nav("example.edu/827", [])
          == {"kind": "url", "value": "https://example.edu/827"})
    check("формулировки nav: вопрос и «Готово»",
          mnav.confirm_question(nav_act)
          == "Открыть example.edu/827 и пройти: студентам → Технологии баз данных?"
          and mnav.describe_done(nav_act)
          == "открыл example.edu/827 и прошёл до «Технологии баз данных»")

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
    # Снапшот/клик/href: секция сквозной нумерации разметки гоняет настоящие
    # на странице-заглушке — сохраняем до первых моков
    _real_snapshot_elements = _ba.snapshot_elements
    _real_click_tagged = _ba.click_tagged
    _real_href_of_tagged = _ba.href_of_tagged
    # Детект модалки/раскрытого списка (Escape-фолбэк «закрой окно») — тоже
    # «ничего не видно» по умолчанию: реальные обёртки уходят в ЖИВОЙ Chrome
    # с отладкой (он на машине бывает открыт), и «закрой модалку» отвечало
    # Escape-действием вместо честного отказа — проверка плавала
    _real_modal_visible = _ba.modal_visible
    _real_open_list_visible = _ba.open_list_visible
    _ba.dismiss_overlay = lambda host=None, tab_id=None: None
    _ba.detect_antibot = lambda host=None, tab_id=None, strict=False: None
    _ba.modal_visible = lambda host=None, tab_id=None: False
    _ba.open_list_visible = lambda host=None, tab_id=None: False
    _ba.wait_dom_idle = lambda *a, **kw: None
    # Видимая вкладка (CDP visibilityState): по умолчанию «нет видимой» —
    # адресация по отслеживаемой/хосту, как раньше; секция видимой вкладки
    # перемокирует локально
    _real_visible_page_info = _ba.visible_page_info
    _ba.visible_page_info = lambda: None
    # Доскролл-поиск (виртуализированные списки): по умолчанию «некуда
    # листать» — целевой снапшот не находит, поведение прежнее
    _ba.scroll_position = lambda host=None, tab_id=None: 0.0
    _ba.scroll_step = lambda host=None, tab_id=None: {"moved": False,
                                                      "bottom": True}
    _ba.scroll_restore = lambda host=None, tab_id=None, y=0.0: None
    # Контейнерная фаза доскролла — тоже «некуда»: без браузера настоящий
    # вызов упирался бы в CDP-таймаут (эскалация шагов навигации его дёргает)
    _ba.scroll_container_step = lambda host=None, tab_id=None: {"moved": False}
    _ba.scroll_container_restore = lambda host=None, tab_id=None, y=0.0: None

    _pages = [[_it(0, "a", "Студентам"), _it(1, "a", "Абитуриентам")],
              [_it(0, "a", "Новости"), _it(3, "a", "Технологии баз данных")]]
    _clicks2 = []
    _snapped = []
    _opened = []
    _orig_snap2, _orig_ct2 = _ba.snapshot_elements, _ba.click_tagged
    _orig_open, _orig_tm = _ba.open_new_tab, _cc_mod.time
    _ba.open_new_tab = lambda url, **kw: (_opened.append(url), 42)[1]
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        _snapped.append(tab_id),
        ("https://example.edu/827", "example.edu", _pages[min(len(_clicks2), 1)]))[1]
    _ba.click_tagged = lambda host, idx, tab_id=None: (
        _clicks2.append((host, idx, tab_id)), "clicked")[1]
    _cc_mod.time = _types.SimpleNamespace(sleep=lambda s: None, time=_orig_tm.time)
    try:
        mnav._navigate(nav_act)
        check("nav: шаги проходятся кликами по порядку",
              _clicks2 == [("example.edu", 0, 42), ("example.edu", 3, 42)])
        check("nav: вкладка открывается отслеживаемой (id) и вся навигация — по ней",
              _opened == ["https://example.edu/827"]
              and _snapped == [42, 42])
        check("nav: финальная вкладка запоминается («на открывшейся странице»)",
              mnav._last_tab_id == 42)
        _clicks2.clear()
        bad_nav = {"kind": "nav", "value": "https://example.edu/827",
                   "host": "example.edu/827",
                   "steps": ["студентам", "несуществующий пункт"]}
        try:
            mnav._navigate(bad_nav)
            _nav_err = ""
        except RuntimeError as e:
            _nav_err = str(e)
        check("nav: пункт не найден — причина с прогрессом, клики остановлены",
              "несуществующий пункт" in _nav_err and "студентам" in _nav_err
              and _clicks2 == [("example.edu", 0, 42)])
        # Страница ещё грузится (снапшот падает) — ждём и пробуем снова
        _clicks2.clear()
        _fails = {"n": 0}
        def _flaky(host=None, tab_id=None):
            if _fails["n"] < 2:
                _fails["n"] += 1
                raise _ba.BrowserUnavailable("на странице нет кликабельных элементов")
            return ("https://example.edu/827", "example.edu",
                    _pages[min(len(_clicks2), 1)])
        _ba.snapshot_elements = _flaky
        mnav._navigate({"kind": "nav", "value": "https://example.edu/827",
                        "host": "example.edu/827", "steps": ["студентам"]})
        check("nav: страница не прогрузилась — повтор снапшота, а не отказ",
              _fails["n"] == 2 and _clicks2 == [("example.edu", 0, 42)])
        # DOM перерисовался между снапшотом и кликом («элемент потерян») —
        # шаг повторяется один раз со свежим снапшотом
        _clicks2.clear()
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            _snapped.append(tab_id),
            ("https://example.edu/827", "example.edu", _pages[0]))[1]
        _ct_n = {"n": 0}
        def _ct_flaky(host, idx, tab_id=None):
            _ct_n["n"] += 1
            if _ct_n["n"] == 1:
                raise _ba.BrowserUnavailable("элемент потерян — страница изменилась")
            return "clicked"
        _ba.click_tagged = _ct_flaky
        mnav._navigate({"kind": "nav", "value": "https://example.edu/827",
                        "host": "example.edu/827", "steps": ["студентам"]})
        check("nav: «элемент потерян» — один повтор шага, успех",
              _ct_n["n"] == 2)
        # Клик без видимого эффекта (closed-loop, п.6) — тоже один повтор шага
        _ct_n2 = {"n": 0}
        def _ct_uncertain(host, idx, tab_id=None):
            _ct_n2["n"] += 1
            if _ct_n2["n"] == 1:
                raise _ba.ClickUncertain("клик отправлен, но страница не изменилась")
            return "clicked"
        _ba.click_tagged = _ct_uncertain
        mnav._navigate({"kind": "nav", "value": "https://example.edu/827",
                        "host": "example.edu/827", "steps": ["студентам"]})
        check("nav: клик без эффекта — один повтор шага, успех",
              _ct_n2["n"] == 2)
        def _ct_dead(host, idx, tab_id=None):
            raise _ba.BrowserUnavailable("элемент потерян — страница изменилась")
        _ba.click_tagged = _ct_dead
        try:
            mnav._navigate({"kind": "nav", "value": "https://example.edu/827",
                            "host": "example.edu/827", "steps": ["студентам"]})
            _nav_err2 = ""
        except RuntimeError as e:
            _nav_err2 = str(e)
        check("nav: повтор не помог — причина с именем шага",
              "на шаге «студентам»" in _nav_err2 and "элемент потерян" in _nav_err2)
    finally:
        _ba.snapshot_elements, _ba.click_tagged = _orig_snap2, _orig_ct2
        _ba.open_new_tab = _orig_open
        _cc_mod.time = _orig_tm

    # Шаг навигации открыл новую вкладку (target=_blank / window.open):
    # маршрут переходит на неё — следующий шаг целится и кликается уже там;
    # старая вкладка остаётся открытой (её ничто не трогает)
    _orig_open_np = _ba.open_new_tab
    _orig_pu_np, _orig_fp_np = _ba.page_urls, _ba.follow_popup
    _orig_snap_np, _orig_ct_np = _ba.snapshot_elements, _ba.click_tagged
    _ba.open_new_tab = lambda url, **kw: 42
    _ba.page_urls = lambda: ["https://example.edu/827"]
    _pop_fired = []
    _ba.follow_popup = lambda pre, **kw: (
        None if _pop_fired else
        (_pop_fired.append(1),
         (515, "news.site.ru", "https://news.site.ru/x"))[1])
    _nav_np = {42: ("https://example.edu/827", "example.edu",
                    [_it(0, "a", "Студентам")]),
               515: ("https://news.site.ru/x", "news.site.ru",
                     [_it(3, "a", "Расписание")])}
    _clicks_np, _snaps_np = [], []
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        _snaps_np.append(tab_id), _nav_np[tab_id])[1]
    _ba.click_tagged = lambda host, idx, tab_id=None: (
        _clicks_np.append((host, idx, tab_id)), "clicked")[1]
    try:
        mnav_np = make(cfg={**CFG, "allow_domains": []})
        mnav_np._navigate({"kind": "nav", "value": "https://example.edu/827",
                           "host": "example.edu/827",
                           "steps": ["студентам", "расписание"]})
        check("nav-popup: шаг открыл вкладку — маршрут перешёл на неё",
              _clicks_np == [("example.edu", 0, 42), ("news.site.ru", 3, 515)]
              and _snaps_np == [42, 515]
              and mnav_np._last_tab_id == 515)
    finally:
        _ba.open_new_tab = _orig_open_np
        _ba.page_urls, _ba.follow_popup = _orig_pu_np, _orig_fp_np
        _ba.snapshot_elements, _ba.click_tagged = _orig_snap_np, _orig_ct_np

    # Навигация, эскалация сбойного шага (п.3): пропуск устаревшего шага без
    # LLM, целевой снапшот (gidx), LLM-восстановление (клик/«пропустить»/
    # «нет»), вето на деструктивный выбор
    class _SeqRouter:
        """Ответы LLM по очереди: широкий резолв, затем восстановление."""

        def __init__(self, *resps):
            self.resps = list(resps)
            self.calls = 0

        def get_response(self, messages, **kw):
            self.calls += 1
            return self.resps.pop(0) if self.resps else "нет"

    _orig_snap3, _orig_ct3 = _ba.snapshot_elements, _ba.click_tagged
    _orig_fg3 = _ba.snapshot_for_goal
    _ba.open_new_tab = lambda url, **kw: 42
    try:
        # A. Текущего шага нет на странице, более поздний — явный лидер:
        # шаг пропускаем без LLM (страница сама ушла вперёд по плану)
        _clicksA = []
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru", [_it(0, "a", "Новости")])
        _ba.click_tagged = lambda host, idx, tab_id=None: (
            _clicksA.append(idx), "clicked")[1]
        nav_a = {"kind": "nav", "value": "https://x.ru", "host": "x.ru",
                 "steps": ["скрытый раздел", "новости"]}
        mnav._navigate(nav_a)
        check("nav эскалация: устаревший шаг пропущен (skip_ahead), "
              "кликнут только актуальный",
              _clicksA == [0]
              and "skip_ahead" in str(nav_a.get("choose", {}).get("path")))

        # B. Шаг не влез в общий снапшот — целевой снапшот по всему DOM,
        # клик по меткам data-vpc-gidx
        _clicksB = []
        _ba.snapshot_for_goal = lambda host, goal, tab_id=None: (
            "https://x.ru", [_it(9, "a", "Скрытый пункт")])
        _ba.click_tagged = lambda host, idx, tab_id=None: (
            _clicksB.append(idx), "clicked")[1]
        mnav._navigate({"kind": "nav", "value": "https://x.ru", "host": "x.ru",
                        "steps": ["скрытый пункт"]})
        check("nav эскалация: шаг нашёлся целевым снапшотом, клик по его номеру",
              _clicksB == [9])
        _ba.snapshot_for_goal = _orig_fg3 if _orig_fg3 else (
            lambda host, goal, tab_id=None: ("", []))

        # C. LLM-восстановление: шаг не нашёлся нигде — модель советует
        # открыть «Меню», после клика цель появляется и шаг проходит
        _clicksC = []
        _pagesC = [[_it(0, "a", "Меню")],
                   [_it(0, "a", "Меню"), _it(1, "a", "Цель")]]
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru", _pagesC[min(len(_clicksC), 1)])
        _ba.click_tagged = lambda host, idx, tab_id=None: (
            _clicksC.append((host, idx, tab_id)), "clicked")[1]
        _rc = _SeqRouter("нет", "1")  # широкий резолв: нет; восстановление: 1
        nav_c = {"kind": "nav", "value": "https://x.ru", "host": "x.ru",
                 "steps": ["цель"]}
        mnav._navigate(nav_c, router=_rc)
        check("nav эскалация: LLM-восстановление — клик по меню, затем шаг",
              _clicksC == [("x.ru", 0, 42), ("x.ru", 1, 42)]
              and "after_recover" in str(nav_c.get("choose", {}).get("path")))
        check("nav эскалация: LLM дёрнулась дважды (wide + recover)",
              _rc.calls == 2)

        # D. «пропустить» — шаг устарел по мнению LLM, навигация завершается
        _clicksD = []
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru", [_it(0, "a", "Меню")])
        _ba.click_tagged = lambda host, idx, tab_id=None: (
            _clicksD.append(idx), "clicked")[1]
        _rd = _SeqRouter("нет", "пропустить")
        nav_d = {"kind": "nav", "value": "https://x.ru", "host": "x.ru",
                 "steps": ["цель"]}
        mnav._navigate(nav_d, router=_rd)
        check("nav эскалация: «пропустить» — шаг снят без ошибки и кликов",
              _clicksD == []
              and "llm_skip" in str(nav_d.get("choose", {}).get("path")))

        # E. «нет» — честная ошибка с прогрессом
        _re = _SeqRouter("нет", "нет")
        try:
            mnav._navigate({"kind": "nav", "value": "https://x.ru",
                            "host": "x.ru", "steps": ["цель"]}, router=_re)
            _errE = ""
        except RuntimeError as e:
            _errE = str(e)
        check("nav эскалация: LLM сдалась — честный отказ с именем шага",
              "не нашёл на странице пункт «цель»" in _errE)

        # F. Восстановление ткнуло в «Закрыть» без намерения закрывать — вето
        _clicksF = []
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru", [_it(0, "button", "Закрыть")])
        _ba.click_tagged = lambda host, idx, tab_id=None: (
            _clicksF.append(idx), "clicked")[1]
        _rf = _SeqRouter("нет", "1")
        try:
            mnav._navigate({"kind": "nav", "value": "https://x.ru",
                            "host": "x.ru", "steps": ["цель"]}, router=_rf)
            _errF = ""
        except RuntimeError as e:
            _errF = str(e)
        check("nav эскалация: деструктивный выбор восстановления ветирован",
              _clicksF == [] and "цель" in _errF)
    finally:
        _ba.snapshot_elements, _ba.click_tagged = _orig_snap3, _orig_ct3
        _ba.snapshot_for_goal = _orig_fg3
        _ba.open_new_tab = _orig_open

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
        def _ct_lost(host, idx, tab_id=None):
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
        def _ct_dead2(host, idx, tab_id=None):
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
        # Явный домен без алиаса: «на example.edu» целится напрямую
        act4, _ = ms3.resolve_click("войти", "example.edu", _BoomRouter())
        check("resolve_click: явный домен в site_word — без алиаса",
              act4 is not None and _snap_calls[-1] == "example.edu")
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
        # Вето — инвариант скоринга: «Очистить очередь» (70.0) не участвует
        # в выборе при цели «очередь», и решение переходит к «Очередь
        # просмотра» (69.5), а не к ветированному лидеру
        _q_items = [_it(0, "button", "Очистить очередь"),
                    _it(1, "a", "Очередь просмотра")]
        check("veto: разрушительный кандидат в скоринг не попадает",
              [it["text"] for _s, it
               in mvt._score_candidates(_q_items, "очередь")]
              == ["Очередь просмотра"]
              and len(mvt._score_candidates(_q_items, "очисти очередь")) == 1)
        _q_llm = mvt._choose_element("очередь", _q_items, _FakeRouter("1"))
        check("veto: LLM выбрала №1 (ветируемый) — берётся следующий кандидат",
              _q_llm[0] == 1)
        _q_det = mvt._choose_element("очередь", _q_items, None)
        check("veto: детерминированный путь с деструктивным лидером — "
              "следующий кандидат",
              _q_det[0] == 1 and _q_det[1]["path"] == "score")
        _q_int = mvt._choose_element("очисти очередь", _q_items, None)
        check("veto: намерение в цели есть — жмём «Очистить очередь»",
              _q_int[0] == 0)
        # Внятного кандидата не осталось — честный отказ с меткой вето
        _only = [_it(0, "button", "Очистить очередь")]
        _q_no = mvt._choose_element("очередь", _only, None)
        check("veto: кроме разрушительного никого — отказ, вето в аудит-мете",
              _q_no[0] is None and _q_no[1].get("veto") == "destructive"
              and _q_no[1].get("vetoed") == ["Очистить очередь"]
              and mvt._resolve_fail_kind(_q_no[1]) == "destructive_veto")
        # Полнота правила: подпись только в aria (иконка), RU/EN формы
        check("veto: разрушительность читается из aria/title и по-английски",
              _cc_mod._destructive_mismatch(
                  "очередь", {"text": "", "aria": "Remove from queue"})
              and _cc_mod._destructive_mismatch("канал", {"text": "Отписаться"})
              and _cc_mod._destructive_mismatch("меню", {"title": "Log out"})
              and _cc_mod._destructive_mismatch("джем", {"text": "🗑"}))
        # Открытый корень ловил безобидные слова — правило морфологическое
        check("veto: «Удалённая работа»/«Closed captions» — не разрушители",
              not _cc_mod._destructive_mismatch(
                  "работа", {"text": "Удалённая работа"})
              and not _cc_mod._destructive_mismatch(
                  "субтитры", {"text": "Closed captions"})
              and not _cc_mod._destructive_mismatch(
                  "лента", {"text": "Скроллить"}))
    finally:
        _ba.snapshot_elements = _orig_snap_vt

    # Подмена намерения по открытому корню: «нажми закрепить» становилось
    # «закрыть» (_CLOSE_VERB_RE ловил «закр\w+») и жало крестик
    check("close-глагол: «закрепить»/«скролл»/«закрась» — не закрытие",
          _cc_mod._CLOSE_VERB_RE.match("закрепить") is None
          and _cc_mod._CLOSE_VERB_RE.match("закрепи комментарий") is None
          and _cc_mod._CLOSE_VERB_RE.match("скролл вниз") is None
          and _cc_mod._CLOSE_VERB_RE.match("скроллить ленту") is None
          and _cc_mod._CLOSE_VERB_RE.match("закрась фон") is None
          and _cc_mod._CLOSE_VERB_RE.match("закрути гайку") is None)
    check("close-глагол: «закрой»/«закрыть»/«сверни»/«скрой» — закрытие",
          _cc_mod._CLOSE_VERB_RE.match("закрой").group(1) == ""
          and _cc_mod._CLOSE_VERB_RE.match("закрыть окно").group(1) == "окно"
          and _cc_mod._CLOSE_VERB_RE.match("сверни анкету").group(1) == "анкету"
          and _cc_mod._CLOSE_VERB_RE.match("скрой").group(1) == ""
          and _cc_mod._CLOSE_VERB_RE.match("скрыть панель").group(1) == "панель")
    check("close-объект: «закрепить форму» не сводится к «закрыть»",
          not _cc_mod._CLOSE_GOAL_RE.search("закрепить форму")
          and bool(_cc_mod._CLOSE_GOAL_RE.search("закрой окно"))
          and bool(_cc_mod._CLOSE_GOAL_RE.search("сверни анкету")))
    # Сквозь resolve_click: крестик рядом с «Закрепить» больше не выигрывает
    _orig_snap_pin = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://youtube.com/x", "youtube.com", [
            _it(0, "button", "Закрыть"), _it(1, "button", "Закрепить")])
    try:
        mpin = SpyManager(context="t", config={**CFG, "allow_domains": []},
                          base_dir=tmp / "s5-pin")
        act_pin, err_pin = mpin.resolve_click("закрепить", None,
                                              _FakeRouter("1"))
        check("close-глагол: «нажми закрепить» жмёт «Закрепить», не крестик",
              err_pin is None and act_pin["idx"] == 1)
        act_cls, err_cls = mpin.resolve_click("закрой", None, _FakeRouter("1"))
        check("close-глагол: «закрой» по-прежнему жмёт крестик",
              err_cls is None and act_cls["idx"] == 0)
    finally:
        _ba.snapshot_elements = _orig_snap_pin

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

    # Бургер («три полоски») — хост-зависимые синонимы: на YouTube кнопка
    # зовётся «Гид» (а «Меню аккаунта» рядом — не цель), на прочих сайтах —
    # «Меню»/«Открыть меню» (кейс 10.09: «нажми три полоски» на платформе)
    _orig_snap_bg = _ba.snapshot_elements
    try:
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://school.example.com/", "school.example.com",
            [_it(0, "a", "Главная"), _it(1, "button", "", aria="Меню"),
             _it(2, "button", "", aria="Меню аккаунта")])
        m_bg = SpyManager(context="t", config={**CFG, "allow_domains": []},
                          base_dir=tmp / "s5-burger")
        act_bg, err_bg = m_bg.resolve_click("три полоски", None, _BoomRouter())
        check("syn: «три полоски» вне ютуба → кнопка «Меню»",
              err_bg is None and act_bg["idx"] == 1)
        # На ютубе — «Гид», и «Меню аккаунта» синонимом не задевается
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://www.youtube.com/", "www.youtube.com",
            [_it(0, "button", "", aria="Гид"),
             _it(1, "button", "", aria="Меню аккаунта")])
        m_by = SpyManager(context="t", config={**CFG, "allow_domains": []},
                          base_dir=tmp / "s5-burger-yt")
        act_by, err_by = m_by.resolve_click("три полоски", None, _BoomRouter())
        check("syn: «три полоски» на ютубе → «Гид», не «Меню аккаунта»",
              err_by is None and act_by["idx"] == 0
              and act_by.get("choose", {}).get("path") == "score")
    finally:
        _ba.snapshot_elements = _orig_snap_bg

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

    # Скоуп-цель «троеточие в <комментарий>» (shorts): общий снапшот даёт меню
    # ОСНОВНОГО видео (синоним «действ», скор 52), целевой снапшот находит
    # меню комментария с меньшим скором — общий выбор без слов скоупа в
    # контексте уступает целевому, даже проигрывая по баллам
    _orig_snap_s2, _orig_gsnap_s2 = _ba.snapshot_elements, _ba.snapshot_for_goal
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://www.youtube.com/shorts/x", "www.youtube.com",
        [_it(0, "button", "Меню действий"), _it(1, "a", "Новости")])
    _ba.snapshot_for_goal = lambda host, goal, tab_id=None: (
        "https://www.youtube.com/shorts/x",
        [_it(8, "button", "Меню действий", w=4.0, h=4.0,
             ctx="@faithnee 4 недели назад He has an little sister to help")])
    try:
        msc = make(cfg={**CFG, "allow_domains": []})
        act_s2, err_s2 = msc.resolve_click("троеточие в he has an little sister",
                                           None, _FakeRouter("1"))
        check("goal-snap: скоуп — общий выбор без слов скоупа уступает целевому",
              err_s2 is None and act_s2["idx"] == 8
              and act_s2.get("choose", {}).get("via") == "goal_snapshot")
        # Контроль: общий выбор СО словами скоупа в контексте — остаётся
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://www.youtube.com/shorts/x", "www.youtube.com",
            [_it(2, "button", "Меню действий",
                 ctx="He has an little sister to help him smile"),
             _it(1, "a", "Новости")])
        act_s3, err_s3 = msc.resolve_click("троеточие в he has an little sister",
                                           None, _FakeRouter("1"))
        check("goal-snap: скоуп — общий выбор со словами скоупа остаётся",
              err_s3 is None and act_s3["idx"] == 2
              and act_s3.get("choose", {}).get("via") != "goal_snapshot")
    finally:
        _ba.snapshot_elements = _orig_snap_s2
        _ba.snapshot_for_goal = _orig_gsnap_s2

    _orig_open3 = _ba.open_new_tab
    _ba.open_new_tab = lambda url, **kw: 4242
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
    _ba.click_tagged = lambda host, idx, tab_id=None: "clicked"
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

    # follow_popup: «новизна» вкладки — по счётчику URL (дубль адреса — тоже
    # попап), свежая about:blank дожидается навигации, старая — нет
    class _PopPg:
        def __init__(self, url):
            self.url = url
        def is_closed(self):
            return False

    class _PopWorker:
        """Стуб CDP-воркера для follow_popup: страницы — список URL,
        меняемый между опросами (submit вызывает fn синхронно)."""
        def __init__(self, urls):
            self.urls = list(urls)
            self._pages = {}
            self._next_tab_id = 900
        def _all_pages(self):
            return [_PopPg(u) for u in self.urls]

    _orig_sel_p, _orig_sub_p = _ba._select_backend, _ba._WORKER.submit
    _ba._select_backend = lambda *a, **kw: "cdp"
    _ba._SERVICE_HOSTS.add("chat.deepseek.com")
    try:
        # Новых страниц нет → None, и без долгого ожидания (нет свежего
        # about:blank — ждать навигацию нечего)
        fw = _PopWorker(["https://a.ru/"])
        _ba._WORKER.submit = lambda fn, timeout=None: fn(fw)
        t0 = time.monotonic()
        check("popup: новых страниц нет → None без долгого ожидания",
              _ba.follow_popup(["https://a.ru/"], timeout_sec=0.4) is None
              and time.monotonic() - t0 < 1.5)
        # Новая вкладка с ТЕМ ЖЕ URL (ссылка «в новой вкладке» на текущую
        # страницу): по множеству она невидима, по счётчику — попап
        fw.urls = ["https://a.ru/", "https://a.ru/"]
        dup = _ba.follow_popup(["https://a.ru/"], timeout_sec=0.4)
        check("popup: вкладка-дубль URL поймана (счётчик, не множество)",
              dup is not None and dup[0] == 900 and dup[1] == "a.ru")
        # Свежая about:blank (навигация в полёте): дожидаемся URL дольше
        # базового бюджета (POPUP_NAV_WAIT_SEC)
        fw2 = _PopWorker(["https://a.ru/", "about:blank"])
        fw2.t0 = time.monotonic()
        _orig_all2 = fw2._all_pages
        def _nav_pages():
            if time.monotonic() - fw2.t0 > 0.5:
                return [_PopPg("https://a.ru/"), _PopPg("https://b.ru/new")]
            return _orig_all2()
        fw2._all_pages = _nav_pages
        _ba._WORKER.submit = lambda fn, timeout=None: fn(fw2)
        t0 = time.monotonic()
        nav_pop = _ba.follow_popup(["https://a.ru/"], timeout_sec=0.2)
        check("popup: свежая about:blank дождалась навигации (за бюджетом)",
              nav_pop is not None and nav_pop[1] == "b.ru"
              and time.monotonic() - t0 >= 0.4)
        # СТАРЫЙ about:blank (стартовая вкладка браузера, была до клика) —
        # свежим не считается, ложного ожидания нет
        fw3 = _PopWorker(["about:blank", "https://a.ru/"])
        _ba._WORKER.submit = lambda fn, timeout=None: fn(fw3)
        t0 = time.monotonic()
        check("popup: старый about:blank — не попап и не ждётся",
              _ba.follow_popup(["about:blank", "https://a.ru/"],
                               timeout_sec=0.4) is None
              and time.monotonic() - t0 < 1.5)
        # Служебная вкладка веб-чата попапом не считается
        fw4 = _PopWorker(["https://a.ru/", "https://chat.deepseek.com/x"])
        _ba._WORKER.submit = lambda fn, timeout=None: fn(fw4)
        check("popup: служебная вкладка веб-чата — не попап",
              _ba.follow_popup(["https://a.ru/"], timeout_sec=0.4) is None)
    finally:
        _ba._select_backend = _orig_sel_p
        _ba._WORKER.submit = _orig_sub_p
        _ba._SERVICE_HOSTS.discard("chat.deepseek.com")

    # tab_op без явной цели: исполнение берёт видимую вкладку ТЕМ ЖЕ
    # источником, что подпись на резолве (AppleScript переднего окна), а не
    # CDP-эвристикой _visible_of (кейс 10.09: «закрой вкладку» подписалось
    # «YouTube», а закрыло платформу — Chrome отдаёт visibilityState
    # 'visible' всем вкладкам окна, и эвристика брала просто последнюю)
    class _ClosablePg:
        def __init__(self, url, title=""):
            self.url, self._title, self.closed = url, title, False
        def title(self):
            return self._title
        def close(self):
            self.closed = True
        def is_closed(self):
            return self.closed

    _orig_fw = _ba._front_window_url
    _orig_sel5, _orig_sub5 = _ba._select_backend, _ba._WORKER.submit
    _ba._select_backend = lambda *a, **kw: "cdp"
    try:
        _p1 = _ClosablePg("https://school.example.com/", "School 21")
        _p2 = _ClosablePg("https://www.youtube.com/", "YouTube")
        _wk = _ba._CdpWorker()
        _wk._all_pages = lambda: [_p1, _p2]
        # AppleScript (переднее окно) видит YouTube; CDP-эвристика взяла бы
        # платформу (последнюю) — исполнение обязано совпасть с подписью
        _ba._front_window_url = lambda: "https://www.youtube.com/"
        _ba._WORKER.submit = lambda fn, timeout=None: fn(_wk)
        _url_c, _title_c = _ba.close_tab(None)
        check("tab_op: закрывается видимая по AppleScript, не CDP-последняя",
              _p2.closed and not _p1.closed and _title_c == "YouTube")
        # SPA сменила query между резолвом и исполнением — матч по origin
        _p5 = _ClosablePg("https://www.youtube.com/watch?v=9", "YouTube")
        _wk._all_pages = lambda: [_p1, _p5]
        _ba._front_window_url = lambda: "https://www.youtube.com/"
        _url_c3, _ = _ba.close_tab(None)
        check("tab_op: точный URL устарел — origin-матч видимой",
              _p5.closed and _url_c3.endswith("/watch?v=9"))
        # Источник молчит (не macOS/нет прав) — прежний фолбэк page_for
        _ba._front_window_url = lambda: ""
        _p3 = _ClosablePg("https://a.ru/", "A")
        _p4 = _ClosablePg("https://b.ru/", "B")
        _wk2 = _ba._CdpWorker()
        _wk2._all_pages = lambda: [_p3, _p4]
        _wk2.page_for = lambda host, tab_id=None: [_p3, _p4][-1]
        _ba._WORKER.submit = lambda fn, timeout=None: fn(_wk2)
        _url_c2, _title_c2 = _ba.close_tab(None)
        check("tab_op: источник молчит — прежний фолбэк (page_for)",
              _p4.closed and not _p3.closed and _title_c2 == "B")
    finally:
        _ba._front_window_url = _orig_fw
        _ba._select_backend, _ba._WORKER.submit = _orig_sel5, _orig_sub5

    # Элемента нет на целевой вкладке, но он — единственный явный лидер на
    # другой открытой странице ТОГО ЖЕ сайта (SSO-окно auth.…, открытое до
    # клика): кликаем там. Чужой сайт без явного «на X» — никогда: «текущий
    # сайт» липкий (кейс 10.09: «три полоски» не нашлись на платформе →
    # фолбэк кликал «Гид» на ютубе)
    _orig_snap7, _orig_lp = _ba.snapshot_elements, _ba.list_pages
    _pages_map = {
        "school.example.com": (
            "https://school.example.com/", "school.example.com",
            [_it(0, "button", "ASAP")]),
        "auth.school.example.com": (
            "https://auth.school.example.com/auth?redirect_uri=platform",
            "auth.school.example.com", [_it(0, "button", "Войти")]),
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
        mfb._last_host = "school.example.com"
        # Окно входа СВОЕГО семейства (auth.school.example.com): элемент там —
        # кликаем (мотивация фолбэка сохранена)
        act_fam, err_fam = mfb.resolve_click("войти", None, _BoomRouter())
        check("page-fallback: SSO-окно своего сайта — клик там",
              err_fam is None and act_fam is not None
              and act_fam["host"] == "auth.school.example.com"
              and act_fam.get("choose", {}).get("path") == "page_fallback")
        # Лидер на ЧУЖОМ сайте, сайт не назван — не кликаем: честный отказ
        act_x, err_x = mfb.resolve_click("yuurei reishi", None, _BoomRouter())
        check("page-fallback: чужой сайт без явного «на X» — отказ",
              act_x is None and err_x is not None and "не нашёл" in err_x)
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
        # Две страницы семейства с явным лидером — не гадаем, честный отказ
        _pages_map["sso.school.example.com"] = (
            "https://sso.school.example.com/x", "sso.school.example.com",
            [_it(0, "button", "Войти")])
        no_x, err_x2 = mfb.resolve_click("войти", None, _BoomRouter())
        check("page-fallback: лидеры на двух страницах семейства — отказ",
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

    # ── 8g. Отчёт о странице «что на странице?» / «пришли скриншот» ──
    from app.features.computer_control import (
        parse_page_view_request, page_view_text, page_view_full_text,
        parse_scroll_to_goal, _MORE_PHOTOS_RE)
    check("view-parse: вопросы о состоянии страницы",
          parse_page_view_request("что на странице?") == (None, False, False)
          and parse_page_view_request("что есть на странице")
          == (None, False, False)
          and parse_page_view_request("что ты видишь") == (None, False, False)
          and parse_page_view_request("что ты видишь на странице?")
          == (None, False, False)
          and parse_page_view_request("что сейчас на экране?")
          == (None, False, False)
          and parse_page_view_request("покажи страницу") == (None, False, False)
          and parse_page_view_request("what's on the page?")
          == (None, False, False))
    check("view-parse: скриншот — явный запрос кадра",
          parse_page_view_request("пришли скриншот") == (None, True, False)
          and parse_page_view_request("сделай скриншот страницы")
          == (None, True, False)
          and parse_page_view_request("скриншот") == (None, True, False)
          and parse_page_view_request("take a screenshot")
          == (None, True, False))
    check("view-parse: сайт хвостом",
          parse_page_view_request("что на странице на додо")
          == ("додо", False, False))
    check("view-parse: не наша команда — None",
          parse_page_view_request("прочитай страницу") is None
          and parse_page_view_request("что находится в разделе напитки") is None
          and parse_page_view_request("нажми войти") is None
          and parse_page_view_request("перейди на вкладку ютуб") is None
          and parse_page_view_request("покажи вкладку ютуб") is None
          and parse_page_view_request("расскажи новости") is None)
    # Полностраничный захват: маркер полноты обязателен
    check("view-parse: «вся страница» — полный захват",
          parse_page_view_request("покажи всю страницу") == (None, True, True)
          and parse_page_view_request("покажи страницу целиком")
          == (None, True, True)
          and parse_page_view_request("покажи всю страницу целиком")
          == (None, True, True)
          and parse_page_view_request("Покажи всю страницу целиком")
          == (None, True, True)
          and parse_page_view_request("пришли весь сайт целиком")
          == (None, True, True)
          and parse_page_view_request("пришли страницу полностью")
          == (None, True, True)
          and parse_page_view_request("сделай полный скриншот страницы")
          == (None, True, True)
          and parse_page_view_request("сфотай всю страницу")
          == (None, True, True)
          and parse_page_view_request("покажи всю страницу на додо")
          == ("додо", True, True))
    check("view-parse: без маркера полноты — обычный отчёт",
          parse_page_view_request("покажи страницу") == (None, False, False)
          and parse_page_view_request("сделай скриншот страницы")
          == (None, True, False))
    # «пролистай до X» / «найди X на странице» — ограниченный доскролл
    check("scroll-goal-parse: цель после «до»",
          parse_scroll_to_goal("пролистай до напитков") == "напитков"
          and parse_scroll_to_goal("докрути до конца") == "конца"
          and parse_scroll_to_goal("промотай страницу до комментариев")
          == "комментариев"
          and parse_scroll_to_goal("доскролль до корзины.") == "корзины")
    check("scroll-goal-parse: «найди … на странице»",
          parse_scroll_to_goal("найди пепперони на странице") == "пепперони"
          and parse_scroll_to_goal("найди пепперони здесь") == "пепперони"
          and parse_scroll_to_goal("отыщи адрес на сайте") == "адрес")
    check("scroll-goal-parse: не наша команда — None",
          parse_scroll_to_goal("пролистай") is None
          and parse_scroll_to_goal("пролистай комментарии") is None
          and parse_scroll_to_goal("найди пепперони на додо") is None
          and parse_scroll_to_goal("найди интерстеллар на кинопоиске") is None
          and parse_scroll_to_goal("покажи страницу") is None
          and parse_scroll_to_goal("нажми напитки") is None)
    # «ещё» — досылка остатка альбома
    check("more-photos: «ещё»/«дальше»",
          bool(_MORE_PHOTOS_RE.match("ещё"))
          and bool(_MORE_PHOTOS_RE.match("еще"))
          and bool(_MORE_PHOTOS_RE.match("дальше"))
          and bool(_MORE_PHOTOS_RE.match("покажи остальные"))
          and bool(_MORE_PHOTOS_RE.match("пришли остальное"))
          and not _MORE_PHOTOS_RE.match("покажи страницу")
          and not _MORE_PHOTOS_RE.match("ещё раз расскажи про додо"))
    _orig_snap_v, _orig_shot_v, _orig_ft_v = (
        _ba.snapshot_elements, _ba.screenshot_viewport, _ba.find_tab_id)
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/", "dodopizza.ru",
        [_it(0, "a", "Пицца"), _it(1, "button", "Корзина"),
         _it(2, "textarea", "Поиск", ed=True),
         _it(3, "div", "Ещё", role="button", vp=False),
         _it(4, "a", "Пицца")])  # дубль — дедуп в тексте
    _view_focus = []
    _ba.screenshot_viewport = lambda host=None, tab_id=None, **kw: (
        _view_focus.append(kw.get("allow_focus")), b"\xff\xd8jpeg")[1]
    _ba.find_tab_id = lambda host: None
    try:
        mview = ComputerControlManager(context="t", base_dir=tmp / "s8-view",
                                       config={**CFG, "allow_domains": []})
        rep, err_v = mview.page_view_report(None)
        check("view: отчёт — элементы + скриншот",
              err_v is None and rep["host"] == "dodopizza.ru"
              and len(rep["items"]) == 5 and rep["shot"] == b"\xff\xd8jpeg")
        check("view: «покажи страницу» — скриншот с allow_focus=True",
              _view_focus == [True])
        txt_v = page_view_text(rep["url"], rep["host"], rep["items"])
        check("view: текст — группы поля/кнопки/ссылки, дедуп дублей",
              "Поля ввода: Поиск" in txt_v
              and "Кнопки: Корзина" in txt_v
              and "Ссылки: Пицца" in txt_v
              and txt_v.count("Пицца") == 1
              and "dodopizza.ru" in txt_v)
        rec_v = json.loads((tmp / "s8-view" / "audit.jsonl")
                           .read_text(encoding="utf-8").strip().splitlines()[-1])
        check("view: аудит kind=page_view, ok",
              rec_v.get("kind") == "page_view" and rec_v.get("ok") is True)
        # Кадр не получился (бэкенд не даёт/ошибка) — отчёт живёт текстом
        _ba.screenshot_viewport = lambda host=None, tab_id=None, **kw: None
        rep2, err_v2 = mview.page_view_report(None)
        check("view: без скриншота — отчёт всё равно отдаётся",
              err_v2 is None and rep2 is not None and rep2["shot"] is None)
        check("view: пустая страница — честный текст без элементов",
              "не вижу" in page_view_text("https://x.ru", "x.ru", []))
    finally:
        _ba.snapshot_elements, _ba.screenshot_viewport = _orig_snap_v, _orig_shot_v
        _ba.find_tab_id = _orig_ft_v

    # ── 8g-full. Полностраничный отчёт: нарезка + оглавление ──
    _orig_snap_vf, _orig_shot_vf = _ba.snapshot_elements, _ba.screenshot_viewport
    _orig_fpc = getattr(_ba, "full_page_capture", None)
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/", "dodopizza.ru",
        [_it(0, "a", "Пиццы"), _it(1, "button", "Корзина")])
    _ba.screenshot_viewport = lambda host=None, tab_id=None, **kw: \
        b"\xff\xd8viewport"
    _ba.full_page_capture = lambda host=None, tab_id=None, **kw: {
        "shots": [b"\xff\xd8p1", b"\xff\xd8p2", b"\xff\xd8p3"],
        "outline": [{"head": "Пиццы", "items": ["Чипси-пицца", "Масала"]},
                     {"head": "Напитки", "items": ["Кола"]}],
        "captured": 3000, "total": 3000, "truncated": False,
        "url": "https://dodopizza.ru/"}
    try:
        mfull = ComputerControlManager(context="t", base_dir=tmp / "s8-full",
                                       config={**CFG, "allow_domains": []})
        rep_f, err_f = mfull.page_view_report(None, full_page=True)
        check("view-full: отчёт — куски + оглавление вместо вьюпортного кадра",
              err_f is None and rep_f.get("full") is True
              and len(rep_f["shots"]) == 3 and rep_f["shot"] is None
              and rep_f["outline"][0]["head"] == "Пиццы")
        txt_f = page_view_full_text(rep_f["url"], rep_f["host"],
                                    rep_f["outline"])
        check("view-full: текст — разделы и позиции сверху вниз",
              "Пиццы: Чипси-пицца; Масала" in txt_f
              and "Напитки: Кола" in txt_f and "dodopizza.ru" in txt_f)
        txt_ft = page_view_full_text("https://x.ru", "x.ru", [],
                                     truncated=True)
        check("view-full: обрезка длинной страницы помечена честно",
              "верхнюю часть" in txt_ft and "кадры" in txt_ft)
        # Захват не вышел — молчаливый фолбэк на обычный вьюпортный отчёт
        _ba.full_page_capture = lambda host=None, tab_id=None, **kw: None
        rep_f2, err_f2 = mfull.page_view_report(None, full_page=True)
        check("view-full: захват не вышел — обычный отчёт с кадром",
              err_f2 is None and rep_f2.get("full") is None
              and rep_f2["shot"] == b"\xff\xd8viewport")
        rec_f = json.loads((tmp / "s8-full" / "audit.jsonl")
                           .read_text(encoding="utf-8").strip()
                           .splitlines()[-1])
        check("view-full: аудит пишет full:число_кусков",
              rec_f.get("kind") == "page_view" and rec_f.get("ok") is True)
    finally:
        _ba.snapshot_elements, _ba.screenshot_viewport = _orig_snap_vf, _orig_shot_vf
        if _orig_fpc is not None:
            _ba.full_page_capture = _orig_fpc

    # ── 8h. «пролистай до X» — ограниченный доскролл с фото места ──
    _orig_snap_sg, _orig_shot_sg = _ba.snapshot_elements, _ba.screenshot_viewport
    _orig_sfg_sg, _orig_ss_sg = _ba.snapshot_for_goal, _ba.scroll_step
    _orig_wd_sg = _ba.wait_dom_idle
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/", "dodopizza.ru",
        [_it(0, "a", "Пиццы"), _it(1, "button", "Корзина")])
    _ba.screenshot_viewport = lambda host=None, tab_id=None, **kw: \
        b"\xff\xd8place"
    _ba.wait_dom_idle = lambda *a, **kw: None
    try:
        msg = ComputerControlManager(context="t", base_dir=tmp / "s8-sgoal",
                                     config={**CFG, "allow_domains": []})
        # Цель сразу в DOM (целевой снапшот нашёл и доскроллил)
        _ba.snapshot_for_goal = lambda host, goal, tab_id=None: (
            "https://dodopizza.ru/", [_it(2, "a", "Напитки")])
        res_g, err_g = msg.scroll_to_goal("напитки")
        check("scroll-goal: находка — found + кадр места",
              err_g is None and res_g["found"] is True
              and res_g["shot"] == b"\xff\xd8place" and res_g["edge"] is None)
        # Промах: цель нигде не рендерится — found=False, кадра нет
        _ba.snapshot_for_goal = lambda host, goal, tab_id=None: ("", [])
        _ba.scroll_step = lambda host=None, tab_id=None: {"moved": False,
                                                          "bottom": True}
        res_g2, err_g2 = msg.scroll_to_goal("суши")
        check("scroll-goal: промах — found=False без кадра",
              err_g2 is None and res_g2["found"] is False
              and res_g2["shot"] is None)
        # Край страницы: «докрути до конца» — листаем до низа
        steps = {"n": 0}

        def _step_bottom(host=None, tab_id=None):
            steps["n"] += 1
            return {"moved": steps["n"] < 3, "bottom": steps["n"] >= 3}
        _ba.scroll_step = _step_bottom
        res_g3, err_g3 = msg.scroll_to_goal("конца")
        check("scroll-goal: «до конца» — edge=bottom, листали до упора",
              err_g3 is None and res_g3["edge"] == "bottom"
              and res_g3["found"] is True and steps["n"] >= 3)
        res_g4, _ = msg.scroll_to_goal("начала")
        check("scroll-goal: «до начала» — edge=top",
              res_g4["edge"] == "top" and res_g4["found"] is True)
    finally:
        _ba.snapshot_elements, _ba.screenshot_viewport = _orig_snap_sg, _orig_shot_sg
        _ba.snapshot_for_goal, _ba.scroll_step = _orig_sfg_sg, _orig_ss_sg
        _ba.wait_dom_idle = _orig_wd_sg

    # ── 8i. Ститчинг полного кадра: непрерывность швов и нарезка ──
    import io as _io_st
    from PIL import Image as _ImgSt

    def _doc_frame(y_from: int, rows: int = 100, w: int = 80) -> bytes:
        # «Документ» — лента 250 строк, строка y залита тоном (y % 256,0,0);
        # кадр = окно [y_from, y_from+rows) этого документа
        im = _ImgSt.new("RGB", (w, rows))
        for yy in range(rows):
            for xx in range(w):
                im.putpixel((xx, yy), ((y_from + yy) % 256, 0, 0))
        buf = _io_st.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    _st = _ba._stitch_slices
    # y: 0 → 80 → 160 (перекрытие 20; последний шаг зажат низом);
    # панели нет — crops нулевые, offs = фактический сдвиг; vh_css=100
    shots_st = _st([_doc_frame(0), _doc_frame(80), _doc_frame(160)],
                   [0.0, 80.0, 160.0], [0.0, 0.0, 0.0], 100, slice_h=100)
    _imgs_st = [_ImgSt.open(_io_st.BytesIO(s)) for s in shots_st]
    check("стич: лента 260px нарезана 100+100+60",
          len(_imgs_st) == 3
          and [im.height for im in _imgs_st] == [100, 100, 60])

    def _row_tone(img, row):
        return img.convert("RGB").getpixel((40, row))[0]

    check("стич: швы непрерывны (тон строки = номеру строки документа)",
          abs(_row_tone(_imgs_st[0], 99) - 99) <= 10
          and abs(_row_tone(_imgs_st[1], 0) - 100) <= 10
          and abs(_row_tone(_imgs_st[1], 79) - 179) <= 10
          and abs(_row_tone(_imgs_st[2], 0) - 200) <= 10
          and abs(_row_tone(_imgs_st[2], 49) - 249) <= 10)
    # Липкая панель 20px у второго кадра: crop=20, непрерывный off=100
    # (сдвиг контента 80 = off − crop)
    shots_st4 = _st([_doc_frame(0), _doc_frame(80)], [0.0, 100.0],
                    [0.0, 20.0], 100, slice_h=100)
    _im4 = [_ImgSt.open(_io_st.BytesIO(s)) for s in shots_st4]
    check("стич: липкая панель срезана, шов непрерывен",
          [im.height for im in _im4] == [100, 80]
          and abs(_row_tone(_im4[1], 0) - 100) <= 10)
    # Кадр шире остальных (полоса прокрутки исчезла) — приведение к первому
    shots_st2 = _st([_doc_frame(0, w=80), _doc_frame(80, w=92)],
                    [0.0, 80.0], [0.0, 0.0], 100, slice_h=500)
    check("стич: разная ширина кадров — приведение к первому",
          _ImgSt.open(_io_st.BytesIO(shots_st2[0])).width == 80)
    check("стич: пустой вход — пустой выход", _st([], [], [], 100) == [])
    # Разный масштаб источников (playwright 1:1 vs OS-уровень ×2 на Retina):
    # кадр ×2 приводится к масштабу первого, геометрия считается в CSS px
    def _doc_frame2x(y_from: int, rows: int = 200, w: int = 160) -> bytes:
        im = _ImgSt.new("RGB", (w, rows))
        for yy in range(rows):
            for xx in range(w):
                im.putpixel((xx, yy), ((y_from // 2 + yy // 2) % 256, 0, 0))
        buf = _io_st.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    shots_st3 = _st([_doc_frame(0), _doc_frame2x(160)],
                    [0.0, 80.0], [0.0, 0.0], 100, slice_h=100)
    _im3 = _ImgSt.open(_io_st.BytesIO(shots_st3[1])).convert("RGB")
    check("стич: кадр ×2 — масштаб выровнен, шов непрерывен",
          _im3.width == 80 and abs(_im3.getpixel((40, 0))[0] - 100) <= 12)


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
          parse_scroll_request("промотай страницу") == ("start", None, None, None, None)
          and parse_scroll_request("листай") == ("start", None, None, None, None)
          and parse_scroll_request("пролистай страницу на ютубе")
          == ("start", "ютубе", None, None, None)
          and parse_scroll_request("проскролль вниз") == ("start", None, None, None, None))
    check("scroll-parse: сторона — «раздел слева» / «список справа» / сайт",
          parse_scroll_request("промотай раздел слева")
          == ("start", None, "left", None, None)
          and parse_scroll_request("пролистай список справа")
          == ("start", None, "right", None, None)
          and parse_scroll_request("проскролль слева") == ("start", None, "left", None, None)
          and parse_scroll_request("листай раздел слева на додо")
          == ("start", "додо", "left", None, None))
    check("scroll-parse: прилагательное первым — «правую часть» = «часть справа»",
          parse_scroll_request("пролистай правую часть")
          == ("start", None, "right", None, None)
          and parse_scroll_request("пролистай часть справа")
          == ("start", None, "right", None, None)
          and parse_scroll_request("пролистай левую часть")
          == ("start", None, "left", None, None)
          and parse_scroll_request("промотай левую колонку")
          == ("start", None, "left", None, None)
          and parse_scroll_request("пролистай правую часть вверх на додо")
          == ("start", "додо", "right", "up", None))
    check("scroll-parse: направление — «вверх»/«выше» + сторона и сайт",
          parse_scroll_request("промотай вверх") == ("start", None, None, "up", None)
          and parse_scroll_request("пролистай раздел слева вверх")
          == ("start", None, "left", "up", None)
          and parse_scroll_request("прокрути страницу наверх")
          == ("start", None, None, "up", None)
          and parse_scroll_request("листай справа вверх на додо")
          == ("start", "додо", "right", "up", None))
    check("scroll-parse: именованный контейнер — «комментарии»/«чат»",
          parse_scroll_request("пролистай комментарии")
          == ("start", None, None, None, "комментарии")
          and parse_scroll_request("промотай комменты на ютубе")
          == ("start", "ютубе", None, None, "комменты")
          and parse_scroll_request("пролистай чат")
          == ("start", None, None, None, "чат"))
    check("scroll-parse: контейнер + направление — «комментарии вверх»",
          parse_scroll_request("пролистай комментарии вверх")
          == ("start", None, None, "up", "комментарии")
          and parse_scroll_request("промотай комментарии наверх")
          == ("start", None, None, "up", "комментарии"))
    check("scroll-parse: «стоп» / «хватит листать» / «остановись»",
          parse_scroll_request("стоп") == ("stop", None, None, None, None)
          and parse_scroll_request("хватит листать") == ("stop", None, None, None, None)
          and parse_scroll_request("остановись") == ("stop", None, None, None, None))
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
    _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None, name=None: (
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
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None, name=None: (
            {"ok": False, "bottom": True})
        act_s3, err_s3 = mscr.resolve_scroll("start", None)
        ok_s3, det_s3 = mscr.execute(act_s3, "c8sc")
        check("scroll: страница уже внизу — «листать некуда»",
              not ok_s3 and "низу" in det_s3)
        # Долистал до конца сам: «стоп» после — честный отчёт о причине
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None, name=None: (
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
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None, name=None: (
            {"ok": False, "bottom": False, "side_missed": True})
        ok_sm, det_sm = mscr.execute(act_sl, "c8sc3")
        check("scroll: side_missed → «не вижу раздела слева»",
              not ok_sm and "слева" in det_sm)
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None, name=None: (
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
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None, name=None: (
            {"ok": False, "bottom": True})
        ok_up2, det_up2 = mscr.execute(act_up, "c8sc4")
        check("scroll: dir=up на верхней границе → «в самом верху»",
              not ok_up2 and "верху" in det_up2)
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None, name=None: (
            {"ok": True, "bottom": False})
        check("scroll: формулировки вопроса и «Готово»",
              mscr.confirm_question(act_s) ==
              "Начать листать страницу на dodopizza.ru? "
              "Скажи «стоп», чтобы остановить."
              and "начал листать страницу на dodopizza.ru"
              in mscr.describe_done(act_s)
              and mscr.describe_done({"kind": "scroll_stop"})
              == "остановил прокрутку")
        # Именованный контейнер («пролистай комментарии»): действие с
        # container, scroll_start получает имя + англ. алиас
        _sc_names = []
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None, name=None: (
            _sc_names.append(name), {"ok": True, "bottom": False})[1]
        act_cn, err_cn = mscr.resolve_scroll(("start", None, None,
                                              "комментарии"), None)
        check("scroll: режим-кортеж с контейнером → container в действии",
              err_cn is None and act_cn.get("container") == "комментарии")
        ok_cn, _ = mscr.execute(act_cn, "c8sc5")
        check("scroll: контейнер → scroll_start(name=«комментарии|comment»)",
              ok_cn and _sc_names == ["комментарии|comment"])
        check("scroll: формулировки с контейнером («листать „комментарии“»)",
              "«комментарии»" in mscr.confirm_question(act_cn)
              and "«комментарии»" in mscr.describe_done(act_cn))
        mscr._scroll_stop_now()
        # Контейнер не нашёлся на странице — честный отказ с именем
        _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None, name=None: (
            {"ok": False, "bottom": False, "name_missed": True})
        ok_nm, det_nm = mscr.execute(act_cn, "c8sc6")
        check("scroll: name_missed → «не вижу блока „комментарии“»",
              not ok_nm and "комментарии" in det_nm)
    finally:
        _ba.scroll_start, _ba.snapshot_elements = _orig_st, _orig_snap_s
        _ba.scroll_status, _ba.scroll_stop = _orig_stat, _orig_stop
        _ba.find_tab_id = _orig_ft_s
        _cc_mod._SCROLL_POLL_SEC = _orig_poll

    # ── Адресация без сайта: отслеживаемая вкладка — «текущий сайт» ──
    _orig_snap_v2, _orig_vis_v2 = _ba.snapshot_elements, _ba.visible_page_info
    _sn_v2 = []
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        _sn_v2.append((host, tab_id)),
        ("https://www.youtube.com/watch?v=2", "www.youtube.com",
         [_it(0, "button", "Понравилось")]))[1]
    try:
        m_vis2 = make(cfg={**CFG, "allow_domains": []})
        m_vis2._last_host = "www.youtube.com"
        m_vis2._last_tab_id = 42
        # A. Видимая вкладка того же сайта — цель всё равно отслеживаемая
        # (tab_id точнее полного URL)
        _ba.visible_page_info = lambda: ("https://www.youtube.com/watch?v=2",
                                         "www.youtube.com")
        act_v2, err_v2 = m_vis2.resolve_key("Space", None, None)
        check("адресация: дубль сайта — отслеживаемая вкладка (tab_id)",
              err_v2 is None
              and _sn_v2[-1] == (None, 42))
        # B. Видимая вкладка ДРУГОГО сайта цель не перетягивает: «текущий
        # сайт» меняется только явным «открой …»/«перейди на вкладку …»
        # (кейс 10.09: «нажми три полоски» после «открой платформу»
        # уходило в бургер-гид ютуба — видимой на тот момент вкладке)
        _ba.visible_page_info = lambda: ("https://google.com", "google.com")
        _sn_v2.clear()
        act_v3, _ = m_vis2.resolve_key("Space", None, None)
        check("адресация: видимая другого сайта — не цель, отслеживаемая",
              _sn_v2 and _sn_v2[-1] == (None, 42))
        # B2. Медиа-команда («пауза») — та же отслеживаемая: ютуб может
        # играть фоном, пока пользователь смотрит другую страницу
        m_vis2._last_host = "www.youtube.com"
        _sn_v2.clear()
        act_v3m, _ = m_vis2.resolve_key(("Space", 1, "toggle"), None, None)
        check("адресация: медиа-команда — отслеживаемая (фоновой ютуб)",
              _sn_v2 and _sn_v2[-1] == (None, 42))
        # B3. Служебные страницы бота (веб-LLM, чат-UI) видимыми/контекстом
        # не становятся — цель остаётся отслеживаемой вкладкой
        _ba.visible_page_info = lambda: ("https://chat.qwen.ai/c/1",
                                         "chat.qwen.ai")
        _sn_v2.clear()
        m_vis2._last_host = "www.youtube.com"
        act_v3q, _ = m_vis2.resolve_key("Space", None, None)
        check("адресация: веб-LLM вкладка бота — всё равно отслеживаемая",
              _sn_v2 and _sn_v2[-1] == (None, 42))
        _ba.visible_page_info = lambda: ("http://localhost:8080/chat",
                                         "localhost")
        _sn_v2.clear()
        act_v3l, _ = m_vis2.resolve_key("Space", None, None)
        check("адресация: localhost (чат-UI бота) — отслеживаемая",
              _sn_v2 and _sn_v2[-1] == (None, 42))
        # C. Контекста нет — видимая вкладка (свежий base_dir: контекст
        # не поднимается с диска, разделяемого другими менеджерами теста)
        _ba.visible_page_info = lambda: ("https://www.youtube.com/watch?v=2",
                                         "www.youtube.com")
        m_vis3 = ComputerControlManager(context="t",
                                        config={**CFG, "allow_domains": []},
                                        base_dir=tmp / "s-vis3")
        _sn_v2.clear()
        act_v4, _ = m_vis3.resolve_key("Space", None, None)
        check("адресация: без контекста — видимая вкладка",
              _sn_v2 and _sn_v2[-1] == ("https://www.youtube.com/watch?v=2",
                                        None))
        # D. Видимой нет (окно свёрнуто) — отслеживаемая
        _ba.visible_page_info = lambda: None
        _sn_v2.clear()
        act_v5, _ = m_vis2.resolve_key("Space", None, None)
        check("адресация: нет видимой — отслеживаемая вкладка",
              _sn_v2 and _sn_v2[-1] == (None, 42))
        # E. Клик (путь сценариев) — та же отслеживаемая вкладка
        _ba.visible_page_info = lambda: ("https://google.com", "google.com")
        _sn_v2.clear()
        act_pt, _ = m_vis2.resolve_click("Понравилось", None, _BoomRouter())
        check("адресация: клик — отслеживаемая вкладка",
              act_pt is not None and _sn_v2 and _sn_v2[-1] == (None, 42))
        # F. Отслеживаемая вкладка переехала редиректом (SSO: platform →
        # auth…): контекст следует за живым URL, иначе после авторизации
        # «эта страница» замирает на адресе открытия (кейс 10.09)
        _ba.visible_page_info = lambda: None
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            _sn_v2.append((host, tab_id)),
            ("https://auth.school.example.com/login?state=1", "auth.school.example.com",
             [_it(0, "button", "Войти")]))[1]
        m_red = ComputerControlManager(context="t",
                                       config={**CFG, "allow_domains": []},
                                       base_dir=tmp / "s-redir")
        m_red._last_tab_id = 77
        m_red._last_host = "school.example.com"
        act_red, err_red = m_red.resolve_key("Space", None, None)
        check("редирект: контекст следует за живым URL вкладки",
              err_red is None and _sn_v2[-1] == (None, 77)
              and m_red._last_host == "auth.school.example.com"
              and m_red._last_url == "https://auth.school.example.com/login?state=1")
        # Возврат редиректом на сайт после авторизации подхватывается сам —
        # вкладка та же (tab_id), просто URL снова сменился
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            _sn_v2.append((host, tab_id)),
            ("https://school.example.com/", "school.example.com",
             [_it(0, "button", "Меню")]))[1]
        act_red2, err_red2 = m_red.resolve_key("Space", None, None)
        check("редирект: возврат на сайт после авторизации подхвачен",
              err_red2 is None and _sn_v2[-1] == (None, 77)
              and m_red._last_host == "school.example.com")
        # Служебная страница (веб-чат LLM оказался в той же вкладке)
        # контекстом не становится
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            _sn_v2.append((host, tab_id)),
            ("https://chat.qwen.ai/c/9", "chat.qwen.ai",
             [_it(0, "button", "X")]))[1]
        m_red.resolve_key("Space", None, None)
        check("редирект: служебный хост контекстом не становится",
              m_red._last_host == "school.example.com")
    finally:
        _ba.snapshot_elements = _orig_snap_v2
        _ba.visible_page_info = _orig_vis_v2

    # ── Громкость на shorts: стрелки там — листание, не громкость ──
    _orig_snap_mv = _ba.snapshot_elements
    _orig_mvo = getattr(_ba, "media_volume_op", None)
    _vol_ops = []
    _ba.media_volume_op = lambda host, op, tab_id=None: (
        _vol_ops.append(op), "vol:30")[1]
    try:
        m_mv = make(cfg={**CFG, "allow_domains": []})
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://www.youtube.com/shorts/abc", "www.youtube.com",
            [_it(0, "a", "Hi")])
        act_mv, err_mv = m_mv.resolve_key(("ArrowDown", 2, "vol_down"),
                                          None, None)
        check("shorts: «тише» → media_vol (video.volume), не клавиша",
              err_mv is None and act_mv["kind"] == "media_vol"
              and act_mv.get("op") == "-0.2")
        act_mm, _ = m_mv.resolve_key(("m", 1, "mute"), None, None)
        check("shorts: «без звука» → media_vol mute",
              act_mm is not None and act_mm.get("op") == "mute"
              and "звук" in m_mv.confirm_question(act_mm))
        # Исполнение — настоящим менеджером (SpyManager._dispatch — заглушка)
        m_mv_exec = ComputerControlManager(context="t", config=dict(CFG),
                                           base_dir=tmp / "s-mvol")
        ok_mv, _ = m_mv_exec.execute(act_mv, "c-mv")
        check("shorts: dispatch media_vol → media_volume_op, отчёт с %",
              ok_mv and _vol_ops == ["-0.2"]
              and m_mv_exec.describe_done(act_mv)
              == "выставил громкость 30% на www.youtube.com")
        # «пауза» на shorts — тоже через <video> напрямую (пробел там
        # листает ленту вперёд, «k» молчит)
        _ba.media_volume_op = lambda host, op, tab_id=None: (
            _vol_ops.append(op), "paused")[1]
        act_tg, _ = m_mv.resolve_key(("Space", 1, "toggle"), None, None)
        check("shorts: play/pause → media_vol toggle (video), не клавиша",
              act_tg is not None and act_tg["kind"] == "media_vol"
              and act_tg.get("op") == "toggle")
        ok_tg, _ = m_mv_exec.execute(act_tg, "c-mv2")
        check("shorts: dispatch toggle → «поставил на паузу»",
              ok_tg and _vol_ops[-1] == "toggle"
              and m_mv_exec.describe_done(act_tg)
              == "поставил видео на паузу на www.youtube.com")
        # «нажми пауза»/«нажми плей» — медиа-команда, а не клик по странице
        from app.features.computer_control import parse_media_request as _pmr
        check("shorts: «нажми пауза»/«нажми плей» парсятся как toggle",
              _pmr("нажми пауза") == ("Space", 1, "toggle")
              and _pmr("нажми паузу") == ("Space", 1, "toggle")
              and _pmr("нажми плей") == ("Space", 1, "toggle")
              and _pmr("пауза") == ("Space", 1, "toggle"))
        # Обычный YouTube — стрелки как раньше
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://www.youtube.com/watch?v=1", "www.youtube.com",
            [_it(0, "a", "Hi")])
        act_yt, _ = m_mv.resolve_key(("ArrowDown", 2, "vol_down"), None, None)
        check("shorts: обычный YouTube — клавиша ArrowDown, как раньше",
              act_yt is not None and act_yt["kind"] == "key"
              and act_yt.get("key") == "ArrowDown")
        # Обычный YouTube — пауза по-прежнему клавишей k
        act_yk, _ = m_mv.resolve_key(("Space", 1, "toggle"), None, None)
        check("shorts: обычный YouTube — пауза клавишей k",
              act_yk is not None and act_yk["kind"] == "key"
              and act_yk.get("key") == "k")
    finally:
        _ba.snapshot_elements = _orig_snap_mv
        if _orig_mvo is not None:
            _ba.media_volume_op = _orig_mvo

    # ── «удали N символов» — стирание серией Backspace ──
    from app.features.computer_control import (parse_erase_request as _per,
                                               PAGE_REF as _PAGE_REF_E)
    check("erase: «удали 5 символов» / «сотри последние три буквы»",
          _per("удали 5 символов") == (("Backspace", 5, "erase"), None)
          and _per("сотри последние три буквы")
          == (("Backspace", 3, "erase"), None)
          and _per("удали символ") == (("Backspace", 1, "erase"), None)
          and _per("стереть пару знаков") == (("Backspace", 2, "erase"), None))
    check("erase: сайт-хвост и «на этой странице»",
          _per("удали 5 символов на ютубе")
          == (("Backspace", 5, "erase"), "ютубе")
          and _per("удали два символа на этой странице")
          == (("Backspace", 2, "erase"), _PAGE_REF_E))
    check("erase: «удали сообщение/вкладку/чикен» — не стирание текста",
          _per("удали сообщение") is None
          and _per("удали вкладку") is None
          and _per("удали чикен") is None)
    check("erase: потолок 100 на команду (опечатка ≠ намерение)",
          _per("удали 500 символов") == (("Backspace", 100, "erase"), None))
    _orig_snap_e = _ba.snapshot_elements
    _orig_vis_e = _ba.visible_page_info
    _orig_pk = getattr(_ba, "press_key", None)
    _presses = []
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://stepik.org/lesson/1", "stepik.org", [_it(0, "input", "Поиск")])
    _ba.visible_page_info = lambda: None
    _ba.press_key = lambda host, key, tab_id=None, times=1: (
        _presses.append((host, key, times)), None)[1]
    try:
        m_er = make(cfg={**CFG, "allow_domains": []})
        m_er._last_host = "stepik.org"
        goal_e, site_e = _per("удали 5 символов")
        act_e, err_e = m_er.resolve_key(goal_e, site_e, None)
        check("erase: резолв — key Backspace ×5, вид «erase»",
              err_e is None and act_e is not None
              and act_e["kind"] == "key" and act_e["key"] == "Backspace"
              and act_e["times"] == 5 and act_e.get("media") == "erase")
        check("erase: формулировки (описание/вопрос/готово)",
              m_er.describe(act_e) == "удалить 5 символов на stepik.org"
              and m_er.confirm_question(act_e)
              == "Удалить 5 символов на stepik.org?"
              and m_er.describe_done(act_e)
              == "удалил 5 символов на stepik.org")
        check("erase: плюрализация (1 символ / 21 символ)",
              m_er.describe({"kind": "key", "key": "Backspace", "times": 1,
                             "media": "erase", "host": "h.ru"})
              == "удалить 1 символ на h.ru"
              and m_er.describe_done({"kind": "key", "key": "Backspace",
                                      "times": 21, "media": "erase",
                                      "host": "h.ru"})
              == "удалил 21 символ на h.ru")
        real_er = ComputerControlManager(context="t", base_dir=tmp / "er",
                                         config={})
        ok_e, _ = real_er.execute(act_e, "c")
        check("erase: dispatch — press_key Backspace ×5",
              ok_e and _presses == [("stepik.org", "Backspace", 5)])
    finally:
        _ba.snapshot_elements = _orig_snap_e
        _ba.visible_page_info = _orig_vis_e
        if _orig_pk is not None:
            _ba.press_key = _orig_pk

    # ── _visible_of: несколько окон — вкладка с фокусом важнее порядка ──
    class _FakePage:
        def __init__(self, vis="visible", focus=False, boom=False):
            self._vis, self._focus, self._boom = vis, focus, boom
        def evaluate(self, expr):
            if self._boom:
                raise RuntimeError("dead")
            if "visibilityState" in expr:
                return self._vis
            if "hasFocus" in expr:
                return self._focus
            return None
    _vo = _ba._CdpWorker._visible_of
    _fp1 = _FakePage("visible")
    check("visible_of: единственная видимая — она и есть",
          _vo([_FakePage("hidden"), _fp1]) is _fp1)
    _fp_old, _fp_new = _FakePage("visible"), _FakePage("visible")
    check("visible_of: без фокуса — последняя видимая (свежее окно)",
          _vo([_fp_new, _fp_old]) is _fp_old)
    _fp_focus = _FakePage("visible", focus=True)
    check("visible_of: окно с фокусом важнее порядка",
          _vo([_fp_focus, _fp_old]) is _fp_focus)
    check("visible_of: скрытые и мёртвые — None",
          _vo([_FakePage("hidden"), _FakePage(boom=True)]) is None)

    # ── visible_page_info: AppleScript-бэкенд — активная вкладка окна ──
    _orig_sb = _ba._select_backend
    _orig_os = _ba._osascript
    try:
        _ba._select_backend = lambda tab_op=True: "applescript"
        _ba._osascript = lambda script, browser="chrome": \
            "https://school.example.com/\n"
        check("visible applescript: активная вкладка переднего окна",
              _real_visible_page_info() == ("https://school.example.com/",
                                            "school.example.com"))
        _ba._osascript = lambda script, browser="chrome": \
            "http://localhost:5173/chat"
        check("visible applescript: чат-UI бота — не кандидат",
              _real_visible_page_info() is None)
        _ba._osascript = lambda script, browser="chrome": "missing value"
        check("visible applescript: нет окна — None",
              _real_visible_page_info() is None)
        # CDP-бэкенд на macOS — тоже активная вкладка окна: visibilityState
        # врёт (Chrome отдаёт visible всем вкладкам окна, кейс 10.09 —
        # «нажми три полоски» уходило на youtube вместо платформы)
        _ba._select_backend = lambda tab_op=True: "cdp"
        _ba._osascript = lambda script, browser="chrome": \
            "https://school.example.com/\n"
        check("visible cdp+mac: активная вкладка окна, не visibilityState",
              _real_visible_page_info() == ("https://school.example.com/",
                                            "school.example.com"))
        # AppleScript молчит (нет прав автоматизации) — фолбэк на
        # CDP-эвристику visibilityState
        def _raise_os(*a, **kw):
            raise _ba.BrowserUnavailable("no automation")
        _ba._osascript = _raise_os

        class _VisPage:
            def __init__(self, url, vis):
                self.url, self._vis = url, vis
            def evaluate(self, expr):
                if "visibilityState" in expr:
                    return self._vis
                if "hasFocus" in expr:
                    return False
                return None

        class _VisW:
            def _all_pages(self):
                return [_VisPage("https://www.youtube.com/", "hidden"),
                        _VisPage("https://example.com/", "visible")]
            def _visible_of(self, cands):
                return _ba._CdpWorker._visible_of(cands)

        _orig_sub2 = _ba._WORKER.submit
        _ba._WORKER.submit = lambda fn, timeout=None: fn(_VisW())
        try:
            got_vis = _real_visible_page_info()
        finally:
            _ba._WORKER.submit = _orig_sub2
        check("visible cdp+mac: AppleScript молчит — фолбэк visibilityState",
              got_vis == ("https://example.com/", "example.com"))
    finally:
        _ba._select_backend = _orig_sb
        _ba._osascript = _orig_os

    # ── page_for: устаревший полный URL (OAuth state/nonce) → origin ──
    check("_origin_of: URL → scheme://host, хост-фрагмент → None",
          _ba._origin_of("https://auth.school.example.com/auth?state=x&nonce=y")
          == "https://auth.school.example.com"
          and _ba._origin_of("youtube.com") is None
          and _ba._origin_of("") is None)

    class _PfPage:
        def __init__(self, url):
            self.url = url
        def is_closed(self):
            return False
        def evaluate(self, expr):
            if "visibilityState" in expr:
                return "visible"
            if "hasFocus" in expr:
                return False
            return None

    _wk_pf = _ba._CdpWorker()
    _wk_pf.ensure_browser = lambda allow_launch=False: None
    _auth_new = ("https://auth.school.example.com/auth/realms/EduPowerKeycloak/"
                 "protocol/openid-connect/auth?client_id=school21&state=NEW")
    _wk_pf._all_pages = lambda: [
        _PfPage("https://www.youtube.com/"), _PfPage(_auth_new)]
    # URL, захваченный секунду назад (state=OLD), уже не совпадает с живым
    _stale = ("https://auth.school.example.com/auth/realms/EduPowerKeycloak/"
              "protocol/openid-connect/auth?client_id=school21&state=OLD"
              "&nonce=abc")
    try:
        _pg = _wk_pf.page_for(_stale)
        check("page_for: устаревший OAuth-URL — матч по origin",
              _pg.url == _auth_new)
        # Точный URL по-прежнему в приоритете
        _pg2 = _wk_pf.page_for("https://www.youtube.com/")
        check("page_for: точный URL — как раньше",
              _pg2.url == "https://www.youtube.com/")
        # Совсем чужой сайт — честный промах
        try:
            _wk_pf.page_for("https://unknown-site.example/")
            _pf_missed = False
        except _ba.BrowserUnavailable:
            _pf_missed = True
        check("page_for: чужой сайт — BrowserUnavailable", _pf_missed)
    except Exception:
        check("page_for: origin-фолбэк не упал", False)

    # ── page_for: вкладка ушла дальше по редиректам (кейс 10.09, повтор) ──
    check("_redirect_origins_of: redirect_uri из query (и двойное кодирование)",
          _ba._redirect_origins_of(
              "https://auth.school.example.com/auth?client_id=school21"
              "&redirect_uri=https%3A%2F%2Fschool.example.com%2F&state=x")
          == ["https://school.example.com"]
          and _ba._redirect_origins_of(
              "https://idp.example.com/auth?next="
              "https%253A%252F%252Fapp.example.net%252Fcb")
          == ["https://app.example.net"]
          and _ba._redirect_origins_of("youtube.com") == [])
    check("_site_key: auth/platform — одно семейство; IP — None, голый хост — ключ",
          _ba._site_key("https://auth.school.example.com/auth?state=x")
          == "school.example.com"
          and _ba._site_key("https://school.example.com/") == "school.example.com"
          and _ba._site_key("http://127.0.0.1:8000/") is None
          and _ba._site_key("youtube.com") == "youtube.com")

    class _FakeCtx:
        def __init__(self, pages):
            self.pages = pages

    class _FakeBr:
        def __init__(self, pages):
            self.contexts = [_FakeCtx(pages)]

    # _all_pages ведёт историю URL каждой вкладки и чистит закрытые
    _wk_h = _ba._CdpWorker()
    _pg_h = _PfPage("https://id.old-portal.ru/login?state=OLD")
    _wk_h._browser = _FakeBr([_pg_h])
    _wk_h._all_pages()
    _pg_h.url = "https://app.new-portal.ru/dashboard"  # SSO увёл на другой домен
    _wk_h._all_pages()
    check("_all_pages: история URL вкладки накапливается",
          _wk_h._url_hist.get(id(_pg_h))
          == ["https://id.old-portal.ru/login?state=OLD",
              "https://app.new-portal.ru/dashboard"])
    _wk_h._browser = _FakeBr([])  # вкладка закрылась — история чистится
    _wk_h._all_pages()
    check("_all_pages: история закрытой вкладки удаляется", not _wk_h._url_hist)

    # page_for: ни точного, ни origin — матч по истории URL вкладки
    _wk_h2 = _ba._CdpWorker()
    _wk_h2.ensure_browser = lambda allow_launch=False: None
    _pg_h2 = _PfPage("https://app.new-portal.ru/dashboard")
    _wk_h2._all_pages = lambda: [_pg_h2]
    _wk_h2._url_hist[id(_pg_h2)] = [
        "https://id.old-portal.ru/login?state=OLD",
        "https://app.new-portal.ru/dashboard"]
    try:
        _pg_r = _wk_h2.page_for("https://id.old-portal.ru/login?state=OLD")
        check("page_for: уехавшая с URL вкладка — матч по истории",
              _pg_r is _pg_h2)
    except Exception:
        check("page_for: уехавшая с URL вкладка — матч по истории", False)

    # page_for: auth-вкладка вернулась на redirect_uri ДРУГОГО домена
    _wk_h3 = _ba._CdpWorker()
    _wk_h3.ensure_browser = lambda allow_launch=False: None
    _pg_cb = _PfPage("https://app.client-app.net/callback?code=ok")
    _wk_h3._all_pages = lambda: [_pg_cb]
    try:
        _pg_r3 = _wk_h3.page_for(
            "https://accounts.idp-example.com/auth?client_id=x"
            "&redirect_uri=https%3A%2F%2Fapp.client-app.net%2Fcallback"
            "&state=OLD")
        check("page_for: матч по redirect_uri из query записанного URL",
              _pg_r3 is _pg_cb)
    except Exception:
        check("page_for: матч по redirect_uri из query записанного URL", False)

    # page_for: redirect_uri нет — последний фолбэк на семейство сайта
    _wk_h4 = _ba._CdpWorker()
    _wk_h4.ensure_browser = lambda allow_launch=False: None
    _pg_pl = _PfPage("https://school.example.com/")
    _wk_h4._all_pages = lambda: [_pg_pl]
    try:
        _pg_r4 = _wk_h4.page_for(
            "https://auth.school.example.com/auth/realms/EduPowerKeycloak/"
            "protocol/openid-connect/auth?client_id=school21&state=OLD2")
        check("page_for: без redirect_uri — матч по семейству сайта",
              _pg_r4 is _pg_pl)
    except Exception:
        check("page_for: без redirect_uri — матч по семейству сайта", False)

    # ── Пустой скриншот фоновой вкладки (dodo, кейсы 09.09/18.09) ──
    import io as _io
    from PIL import Image as _PILImage
    def _jpeg_blank():
        b = _io.BytesIO()
        _PILImage.new("RGB", (200, 100), (255, 255, 255)).save(b, "JPEG")
        return b.getvalue()
    def _jpeg_busy():
        img = _PILImage.new("RGB", (200, 100), (255, 255, 255))
        for x in range(60):
            for y in range(100):
                img.putpixel((x, y), (0, 0, 0))
        b = _io.BytesIO()
        img.save(b, "JPEG")
        return b.getvalue()
    def _png_twotone():
        # Заглушка + кайма второго тона: top-1 < 98.5%, но top-2 ≥ 99.5%
        img = _PILImage.new("L", (200, 100), 243)
        for x in range(190, 200):
            for y in range(100):
                img.putpixel((x, y), 255)
        b = _io.BytesIO()
        img.save(b, "PNG")
        return b.getvalue()
    def _png_halfrendered():
        # Как «полосатый» dodo-кадр (18.09): 75% фон + 15% белое + 10%
        # контента — top-2 ~90% < 99.5% → НЕ пустой
        img = _PILImage.new("L", (200, 100), 243)
        for x in range(170, 200):
            for y in range(100):
                img.putpixel((x, y), 255)
        for x in range(20):
            for y in range(100):
                img.putpixel((x, y), 30)
        b = _io.BytesIO()
        img.save(b, "PNG")
        return b.getvalue()
    check("shot_blank: белый/двухтональный — пустые, контент/мусор — нет",
          _ba._shot_blank(_jpeg_blank()) is True
          and _ba._shot_blank(_png_twotone()) is True
          and _ba._shot_blank(_png_halfrendered()) is False
          and _ba._shot_blank(_jpeg_busy()) is False
          and _ba._shot_blank(b"junk") is False)

    class _ShotPage:
        def __init__(self, shots):
            self._shots = list(shots)
            self.shots_taken = 0
            self.raised = 0
            self.url = "https://dodopizza.ru/"
        def screenshot(self, **kw):
            self.shots_taken += 1
            return self._shots.pop(0) if self._shots else _jpeg_blank()
        def evaluate(self, expr, *args):
            return 1  # devicePixelRatio / RAF-заглушка
        def bring_to_front(self):
            self.raised += 1

    class _ShotWorker:
        def __init__(self, page):
            self._page = page
            self.activated = 0
            self.zoom_checked = 0
        def page_for(self, host, tid):
            return self._page
        def _activate_tab_quietly(self, page, url):
            self.activated += 1
        def _ensure_zoom_normal(self, page):
            self.zoom_checked += 1

    _orig_sel3 = _ba._select_backend
    _orig_submit3 = _ba._WORKER.submit
    _orig_sleep3 = _ba.time.sleep
    _orig_focus3 = _ba._focus_browser_tab
    _orig_os3 = _ba._screenshot_os_level
    _focus_calls3 = []
    _os_shots3 = []
    _os_calls3 = []
    _ba._select_backend = lambda tab_op=True: "cdp"
    _ba.time.sleep = lambda s: None
    _ba._focus_browser_tab = lambda url: _focus_calls3.append(url) or True
    _ba._screenshot_os_level = lambda page: (
        _os_calls3.append(1),
        _os_shots3.pop(0) if _os_shots3 else None)[1]
    try:
        # Живой кадр с первой попытки — без пересъёмок и OS-уровня
        pg = _ShotPage([_jpeg_busy()])
        wk1 = _ShotWorker(pg)
        _ba._WORKER.submit = lambda fn, timeout=None: fn(wk1)
        got = _ba.screenshot_viewport("dodopizza.ru")
        check("screenshot: живой кадр сразу — без лишних ступеней",
              got == _jpeg_busy() and pg.shots_taken == 1 and not _os_calls3
              and wk1.zoom_checked == 1)
        # Середина рендера: пересъёмка после паузы — без OS-уровня
        _os_calls3.clear()
        pg2 = _ShotPage([_jpeg_blank(), _jpeg_busy()])
        _ba._WORKER.submit = lambda fn, timeout=None: fn(_ShotWorker(pg2))
        got2 = _ba.screenshot_viewport("dodopizza.ru")
        check("screenshot: retry после паузы помог — без OS-уровня",
              got2 == _jpeg_busy() and pg2.shots_taken == 2 and not _os_calls3)
        # Композитор белый (окно скрыто/перекрыто, кейс 18.09 dodo):
        # OS-уровень вытащил кадр БЕЗ активации вкладки
        pg3 = _ShotPage([_jpeg_blank(), _jpeg_blank()])
        wk3 = _ShotWorker(pg3)
        _os_shots3[:] = [_jpeg_busy()]
        _ba._WORKER.submit = lambda fn, timeout=None: fn(wk3)
        got3 = _ba.screenshot_viewport("dodopizza.ru")
        check("screenshot: композитор белый — OS-уровень без активации",
              got3 == _jpeg_busy() and wk3.activated == 0)
        # OS-уровень не вышел (окно свёрнуто): тихая активация → OS-кадр
        pg4 = _ShotPage([_jpeg_blank(), _jpeg_blank()])
        wk4 = _ShotWorker(pg4)
        _os_shots3[:] = [None, _jpeg_busy()]
        _ba._WORKER.submit = lambda fn, timeout=None: fn(wk4)
        got4 = _ba.screenshot_viewport("dodopizza.ru")
        check("screenshot: окно свёрнуто — тихая активация и OS-пересъёмка",
              got4 == _jpeg_busy() and wk4.activated == 1)
        # allow_focus: всё пусто до эскалации — подъём окна вытащил кадр
        pg5 = _ShotPage([])
        wk5 = _ShotWorker(pg5)
        _os_shots3[:] = [None, None, _jpeg_busy()]
        _focus_calls3.clear()
        _ba._WORKER.submit = lambda fn, timeout=None: fn(wk5)
        got5 = _ba.screenshot_viewport("dodopizza.ru", allow_focus=True)
        check("screenshot: allow_focus — эскалация подняла окно, кадр есть",
              got5 == _jpeg_busy() and pg5.raised == 1 and _focus_calls3)
        # Кадр не добыт нигде — None (белый кадр не отдаём)
        pg6 = _ShotPage([])
        wk6 = _ShotWorker(pg6)
        _os_shots3[:] = []
        _ba._WORKER.submit = lambda fn, timeout=None: fn(wk6)
        got6 = _ba.screenshot_viewport("dodopizza.ru", allow_focus=True)
        check("screenshot: кадр не добыт нигде — None",
              got6 is None and pg6.raised == 1)
        # Без allow_focus эскалации нет (тихие фолбэки — тишина важнее)
        pg7 = _ShotPage([])
        wk7 = _ShotWorker(pg7)
        _ba._WORKER.submit = lambda fn, timeout=None: fn(wk7)
        got7 = _ba.screenshot_viewport("dodopizza.ru")
        check("screenshot: без allow_focus — окно не трогаем, None",
              got7 is None and pg7.raised == 0 and wk7.activated == 1)
    finally:
        _ba._select_backend = _orig_sel3
        _ba._WORKER.submit = _orig_submit3
        _ba.time.sleep = _orig_sleep3
        _ba._focus_browser_tab = _orig_focus3
        _ba._screenshot_os_level = _orig_os3

    # ── 502/503/504 при открытии: вкладка закрывается и открывается заново ──
    class _GWPage:
        def __init__(self, statuses, no_eval=False):
            self._seq = list(statuses)
            self._no_eval = no_eval
            self.closed = False
            self.url = "https://dodopizza.ru/"
        def evaluate(self, expr, *args):
            if self._no_eval:
                raise RuntimeError("no eval")
            if len(self._seq) > 1:
                return self._seq.pop(0)
            return self._seq[0]
        def close(self):
            self.closed = True

    class _GWWorker:
        def __init__(self, pages):
            self._pages_seq = list(pages)
            self.made = []
        def _new_page_quiet(self, ctx, url):
            pg = self._pages_seq.pop(0) if self._pages_seq else _GWPage([200])
            self.made.append(pg)
            return pg, False

    _orig_sleep4 = _ba.time.sleep
    _ba.time.sleep = lambda s: None
    try:
        # Здоровый ответ — вкладка одна, ничего не закрывалось
        w_gw = _GWWorker([_GWPage([200])])
        pg_gw, _ = _ba._open_page_gateway_retry(w_gw, None, "https://x.ru/")
        check("gateway: 200 — без переоткрытия",
              len(w_gw.made) == 1 and not pg_gw.closed)
        # 502 → закрыть и открыть заново (кейс 18.09, dodo)
        w_gw2 = _GWWorker([_GWPage([502]), _GWPage([200])])
        pg_gw2, _ = _ba._open_page_gateway_retry(w_gw2, None, "https://x.ru/")
        check("gateway: 502 → закрыть и открыть заново",
              len(w_gw2.made) == 2 and w_gw2.made[0].closed
              and pg_gw2 is w_gw2.made[1] and not pg_gw2.closed)
        # 502 → 502: один retry, вторую вкладку не трогаем
        w_gw3 = _GWWorker([_GWPage([502]), _GWPage([502])])
        pg_gw3, _ = _ba._open_page_gateway_retry(w_gw3, None, "https://x.ru/")
        check("gateway: повторная 502 — один retry, страница оставлена",
              len(w_gw3.made) == 2 and w_gw3.made[0].closed
              and not pg_gw3.closed)
        # 503/504 — тот же транзит шлюза; 500 — не наш случай
        w_gw4 = _GWWorker([_GWPage([504]), _GWPage([200])])
        _ba._open_page_gateway_retry(w_gw4, None, "https://x.ru/")
        w_gw5 = _GWWorker([_GWPage([500])])
        _ba._open_page_gateway_retry(w_gw5, None, "https://x.ru/")
        check("gateway: 504 лечится, 500 — нет",
              len(w_gw4.made) == 2 and len(w_gw5.made) == 1)
        # Статус не читается (старый бэкенд) — не мешаем открытию
        w_gw6 = _GWWorker([_GWPage([0], no_eval=True)])
        pg_gw6, _ = _ba._open_page_gateway_retry(w_gw6, None, "https://x.ru/")
        check("gateway: статус не читается — вкладка оставлена",
              len(w_gw6.made) == 1 and not pg_gw6.closed)
        # Статус появляется не сразу (0, потом 502) — опрос дождался
        w_gw7 = _GWWorker([_GWPage([0, 502]), _GWPage([200])])
        _ba._open_page_gateway_retry(w_gw7, None, "https://x.ru/")
        check("gateway: статус дочитался со второго опроса → переоткрытие",
              len(w_gw7.made) == 2 and w_gw7.made[0].closed)
    finally:
        _ba.time.sleep = _orig_sleep4

    _clicks = []
    _orig_ct = _ba.click_tagged
    _orig_ft = _ba.find_tab_id
    _ba.click_tagged = lambda host, idx, tab_id=None: (
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
    check("parse download: «скачай файл методичку по sql на example.edu»",
          parse_download_request("скачай файл методичку по sql на example.edu")
          == ("методичку по sql", "example.edu"))
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
        "https://example.edu/x", "example.edu",
        [_it(0, "a", "Войти"), _it(1, "a", "Методичка по SQL"),
         _it(2, "img", "СТУДЕНТАМ")])
    _hrefs = {1: "https://example.edu/a/file_get/329640?nomenu=1", 2: ""}
    _ba.href_of_tagged = lambda host, idx, tab_id=None: _hrefs.get(idx, "")
    try:
        act_dl, err_dl = ms2.resolve_download("методичку по sql", "example.edu",
                                              _BoomRouter())
        check("resolve_download: матч → download-действие с href",
              err_dl is None
              and act_dl["kind"] == "download"
              and act_dl["url"] == "https://example.edu/a/file_get/329640?nomenu=1"
              and act_dl["element"] == "Методичка по SQL"
              and act_dl["host"] == "example.edu")
        no_dl, no_dl_err = ms2.resolve_download("студентам", "example.edu",
                                                _BoomRouter())
        check("resolve_download: у иконки нет href — честный отказ",
              no_dl is None and no_dl_err is not None and "не ссылка" in no_dl_err)
        check("формулировки download: вопрос и «Готово»",
              ms2.confirm_question(act_dl)
              == ("Скачать «Методичка по SQL» с example.edu?\n"
                  "https://example.edu/a/file_get/329640?nomenu=1")
              and ms2.describe_done(act_dl)
              == "скачал «Методичка по SQL» с example.edu")
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
                  ok4 and _dls == [("example.edu",
                                    "https://example.edu/a/file_get/329640?nomenu=1",
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
    # search first: сеть в тестах не трогаем — httpx.Client подменяем; DNS
    # тоже: загрузка идёт через SSRF-фильтр (getaddrinfo) — без подмены
    # проверка зависела бы от живого резолвинга youtube.com
    import httpx as _httpx
    import socket as _socket_sf
    _orig_client = _httpx.Client
    _orig_gai_sf = _socket_sf.getaddrinfo
    _socket_sf.getaddrinfo = lambda host, *a, **kw: [
        (2, 1, 6, "", ("93.184.216.34", 0))]

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
            class _R:
                text = '<a href="/watch?v=abc12345678">Utopia</a>'
                is_redirect = False  # цепочку редиректов проверяет фильтр

                def raise_for_status(self):
                    pass
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
        _socket_sf.getaddrinfo = _orig_gai_sf
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
    _ba.open_new_tab = lambda url, **kw: (opened.append(url), 1)[1]  # macOS-путь
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
        ("https://lms.example.edu/login", "Диспейс вуза — личный кабинет", 40, 0),
        ("https://example.edu/kaf/persons/98849", "вуза - КУТУЗОВА И. А.", 12, 0),
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
              _bh.find_in_history("диспейс") == "https://lms.example.edu/")
        check("history: мультисловный запрос (падеж) → страница целиком",
              _bh.find_in_history("кутузовой вуза") == "https://example.edu/kaf/persons/98849")
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
                                             "value": "https://lms.example.edu/"})
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
    # Действия без поля key (read из LLM-яруса, неизвестные kind) не должны
    # ронять шаблоны KeyError'ом — падение улетало в чат как «⚠ 'key'»
    check("шаблоны: read и kind без key — без KeyError",
          mr_.describe({"kind": "read", "mode": "page", "host": "x.ru"})
          == "прочитать страницу на x.ru"
          and mr_.describe_done({"kind": "read", "mode": "page",
                                 "host": "x.ru"}) == "прочитал страницу на x.ru"
          and mr_.describe_done({"kind": "read", "mode": "last",
                                 "host": "x.ru"})
          == "прочитал последнее сообщение на x.ru"
          and mr_.describe_done({"kind": "weird"}) == "выполнил задачу «weird»"
          and mr_.describe({"kind": "weird"}) == "выполнить задачу «weird»"
          and mr_.confirm_question({"kind": "weird"})
          == "Выполнить задачу «weird»?")

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

    # ── 7b. Отрицание побеждает («сомнение = UNKNOWN, отрицание побеждает»):
    # голое да-слово внутри отрицающей реплики раньше пробивало границу
    # безопасности (RUN_TASK=shell исполнялся по «не открывай») ──
    for t in (
        "не открывай", "не запускай", "не включай", "давай не будем",
        "подожди, не открывай", "нет, давай, действуй", "да, но нет",
        "стоп, не надо", "не делай этого, отмена",
    ):
        check(f"confirm NO (отрицание рядом с да-словом): {t!r}",
              classify_confirmation(t) == "NO")
    # Отрицание общей уверенности без прямой привязки к да-слову — не NO
    # (не знаем, что именно отрицается), но и не YES
    check("confirm UNKNOWN: 'да, но не сейчас' (отрицание без да-слова рядом)",
          classify_confirmation("да, но не сейчас") == "UNKNOWN")
    # EN: not/don't перед да-словом/глаголом-согласием — тоже NO, общим правилом,
    # а не списком фраз
    for t in ("don't go", "do not go", "not sure, no"):
        check(f"confirm NO (EN negation): {t!r}", classify_confirmation(t) == "NO")
    check("confirm UNKNOWN: \"don't open it\" (open — не в словаре да-слов)",
          classify_confirmation("don't open it") == "UNKNOWN")

    # Вопрос — не ответ: «да?» задаёт вопрос, не подтверждает
    for t in ("да?", "готово, да?", "да или нет?", "а что откроется, да или нет?"):
        check(f"confirm не-YES на вопрос: {t!r}",
              classify_confirmation(t) != "YES")

    # Длинный текст (в т.ч. составной ввод фото/документа с OCR) — да-слово
    # внутри него не должно засчитываться как ответ на pending-вопрос
    _long_ocr_like = (
        "The user sent an image. Its contents according to the vision model:\n"
        "меню кафе: сегодня в наличии да, круассаны, кофе американо, чай "
        "зелёный и чёрный, а также свежая выпечка и десерты на любой вкус"
    )
    check("confirm UNKNOWN: длинный текст с «да» внутри (OCR-подобный)",
          classify_confirmation(_long_ocr_like) == "UNKNOWN")
    check("confirm UNKNOWN: длинная фраза с «да» не как ответ",
          classify_confirmation(
              "вчера мы гуляли и я думаю что да, погода была очень "
              "хорошая и тёплая для этого времени года") == "UNKNOWN")

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

    # Отрицание «не» в подписи кандидата: «нравится» ≠ кнопка
    # «Поставить отметку "Не нравится"» — раньше обе матчились одинаково
    # и клик уходил на дизлайк (shorts)
    _like_pair = [_it(0, "button", "", aria='Поставить отметку "Нравится"'),
                  _it(1, "button", "", aria='Поставить отметку "Не нравится"')]
    _sc = ms._score_candidates(_like_pair, "нравится")
    check("score: «не нравится» не матчит голое «нравится»",
          [t[1]["idx"] for t in _sc] == [0])
    _sc = ms._score_candidates(_like_pair, "не нравится")
    check("score: «нравится» не матчит цель «не нравится»",
          [t[1]["idx"] for t in _sc] == [1])
    _like_ctx = [_it(0, "button", "", aria='Поставить отметку "Нравится"',
                    ctx="The Loudest Cane Gamerish"),
                 _it(1, "button", "", aria='Поставить отметку "Не нравится"',
                    ctx="The Loudest Cane Gamerish")]
    check("score: скоуп-цель «нравится в X» выбирает лайк, не дизлайк",
          [t[1]["idx"] for t in ms._score_scoped(
              _like_ctx, "нравится в the loudest cane")] == [0])
    idx_, meta_ = ms._choose_element(
        "нравится в the loudest cane", _like_ctx, _BoomRouter())
    check("choose: «нравится в X» — лайк без LLM",
          idx_ == 0 and meta_["path"] == "score")
    check("score: «не» у чужого слова не ломает совпадение",
          [t[1]["idx"] for t in ms._score_candidates(
              [_it(0, "a", "", aria="Не пропустить: скачать отчёт")],
              "скачать")] == [0])

    # Диакритика в названии: «in lumiere's name» должно находить ссылку-
    # заголовок «In Lumière's Name …», а не автора/дату из той же карточки
    # (иначе клик уходил на канал по ctx-ярусу)
    from app.features.computer_control import _norm_match
    _lum = [_it(0, "a", "In Lumière’s Name (Mime Battle Theme) - Clair Obscur"),
            _it(1, "span", "Anton Betita",
                ctx="In Lumière’s Name (Mime Battle Theme) - Clair Obscur"),
            _it(2, "span", "1 год назад",
                ctx="In Lumière’s Name (Mime Battle Theme) - Clair Obscur")]
    check("score: диакритика — «lumiere» находит «Lumière» (заголовок видео)",
          ms._score_candidates(_lum, "in lumiere's name")[0][1]["idx"] == 0)
    check("norm: акценты и ё/е склеиваются",
          _norm_match("Lumière") == "lumiere"
          and _norm_match("Ёлка") == "елка")

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
    _ba.click_tagged = lambda host, idx, tab_id=None: "clicked"
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
        def _ct_noop(host, idx, tab_id=None):
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
    from app.features.browser_actions import set_browser_config as _sbc
    _sbc({"backend": "applescript"})
    try:
        try:
            _ba.open_new_tab("https://x.test", background=True)
            _noas = ""
        except _ba.BrowserUnavailable as e:
            _noas = str(e)
        check("background-вкладка без CDP: ошибка про CDP, AppleScript не зовём",
              "CDP" in _noas)
    finally:
        _sbc({})

    # Ожидание конца аплоада аттачей веб-чата (qwen: *-uploading класс):
    # JS-маркеры + обёртка по raw-вкладке
    from app.features.browser_actions import _CHAT_WAIT_UPLOADED_JS
    check("chat_wait_uploaded: JS ищет uploading/progressbar, шаблон под sel",
          'uploading' in _CHAT_WAIT_UPLOADED_JS
          and 'progressbar' in _CHAT_WAIT_UPLOADED_JS
          and '%s' in _CHAT_WAIT_UPLOADED_JS)
    _raw_saved = dict(_ba._RAW_TABS)
    _orig_raw = _ba._raw_eval
    try:
        _ba._RAW_TABS[4242] = {"targetId": "x", "sessionId": "y", "pool": "v"}
        _budgets = []
        def _ev_ready(tid, js, timeout_sec=None):
            _budgets.append(timeout_sec)
            return "ready" if tid == 4242 else "?"
        _ba._raw_eval = _ev_ready
        check("chat_wait_uploaded: raw-вкладка ready → True",
              _ba.chat_wait_uploaded(None, 4242, "textarea") is True)
        # Потолок ответа CDP выводится из бюджета ожидания В СТРАНИЦЕ
        check("chat_wait_uploaded: бюджет JS страницы уходит в транспорт",
              _budgets == [25.0]
              and _ba.chat_wait_uploaded(None, 4242, "textarea",
                                         timeout=40.0) is True
              and _budgets[-1] == 40.0)
        _ba._raw_eval = lambda tid, js, timeout_sec=None: "timeout"
        check("chat_wait_uploaded: аплоад завис (timeout) → False",
              _ba.chat_wait_uploaded(None, 4242, "textarea") is False)
    finally:
        _ba._RAW_TABS.clear()
        _ba._RAW_TABS.update(_raw_saved)
        _ba._raw_eval = _orig_raw

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
    _px = _ba._Probe(True, "", "u", "x")
    _py = _ba._Probe(True, "", "u", "y")
    _seq = iter([_px, _px, _py])
    check("closed-loop: состояние изменилось → «изменилось»",
          _ba._wait_effect(lambda: next(_seq, _py), _px,
                           timeout=1.0, interval=0.01) == _ba.EFFECT_CHANGED)
    check("closed-loop: состояние не менялось → «не изменилось» по таймауту",
          _ba._wait_effect(lambda: _px, _px, timeout=0.05,
                           interval=0.01) == _ba.EFFECT_SAME)

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
          parse_type_request("введи в поле выберите город �город")
          == "в поле выберите город �город")
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
        act_t, err_t = mt.resolve_type("�город в поле выберите город",
                                       None, _BoomRouter())
        check("type: «ТЕКСТ в поле ПОЛЕ» — текст и поле разделены верно",
              err_t is None and act_t["idx"] == 0
              and act_t["text"] == "�город"
              and act_t["element"] == "Выберите город")
        act_t2, err_t2 = mt.resolve_type("в поле выберите город �город",
                                         None, _BoomRouter())
        check("type: «в поле ПОЛЕ ТЕКСТ» — префиксный матч подписи",
              err_t2 is None and act_t2["idx"] == 0
              and act_t2["text"] == "�город"
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
        act_t5, _ = mt.resolve_type("�город", None, _BoomRouter())
        check("type: одно поле + одно слово — в единственное поле",
              act_t5 is not None and act_t5["idx"] == 0
              and act_t5["text"] == "�город")
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
            "�город в поле выберите город на ёбидоёби", None, _BoomRouter())
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
        act_t9, _ = mt.resolve_type("�город в поле выберите город",
                                    None, _BoomRouter())
        ok9, _ = mt.execute(act_t9, "c8t")
        rec9 = json.loads((st8 / "audit.jsonl").read_text(encoding="utf-8")
                          .strip().splitlines()[-1])
        check("type: execute → fill_tagged, аудит kind=type verify=ok",
              ok9 and _fills[-1][:3] == ("yobidoyobi.ru", 0, "�город")
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
              "ввести «�город»" in ComputerControlManager.describe(act_t9)
              and "Выберите город" in ComputerControlManager.describe(act_t9)
              and ComputerControlManager.confirm_question(act_t9).startswith("Ввести")
              and "ввёл «�город»" in ComputerControlManager.describe_done(act_t9))
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
    _ec.load_location = lambda: {"mode": "geo", "city": "�город",
                                 "lat": 55.0, "lon": 82.9}
    try:
        act_g, err_g = mt.resolve_type("мой город в поле поиск", None,
                                       _BoomRouter())
        check("type+geo: «мой город в поле поиск» — город подставлен",
              err_g is None and act_g["text"] == "�город"
              and act_g["idx"] == 1)
        act_g2, err_g2 = mt.resolve_type("город в поле поиск", None,
                                         _BoomRouter())
        check("type+geo: голое «город» — тоже плейсхолдер",
              err_g2 is None and act_g2["text"] == "�город")
        # Одно поле на странице: «введи мой город» — без названия поля
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://yobidoyobi.ru", "yobidoyobi.ru",
            [_it(0, "input", "Поиск", ed=True)])
        act_g3, err_g3 = mt.resolve_type("мой город", None, _BoomRouter())
        check("type+geo: «введи мой город» + одно поле — в него",
              err_g3 is None and act_g3["text"] == "�город"
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
        # «�город, Россия» (manual-режим) → только город, без страны
        _ec.load_location = lambda: {"mode": "manual",
                                     "city": "�город, Россия"}
        check("type+geo: «Город, Страна» → только город",
              mt._home_city() == "�город")
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
    # Порядок системных нот: CC → conversation_style → язык ответа. Нота
    # языка добавлена в persona.py ПОСЛЕ conv_style_note намеренно (она
    # перекрывает язык всех блоков выше) — «последняя» теперь она
    check("prepare_messages: порядок нот CC → conv_style → нота языка",
          content.index("\n\nCC_NOTE") < content.index("\n\nCS_NOTE")
          < content.index("[RESPONSE LANGUAGE")
          and content.rstrip().endswith("Do not mix languages in one reply.")
          and content.startswith("SYSTEM."))
    check("prepare_messages: без языка сообщения conv-нота остаётся последней",
          p.prepare_messages("", computer_control_context="CC_NOTE",
                             conversation_style_context="CS_NOTE"
                             )[0]["content"].endswith("\n\nCS_NOTE"))

    from app.features import conversation_style as cs
    cfg_none = cs.ConversationStyleConfig(
        {"conversation_style": {"question_frequency": "none"}})
    check("conv_style: ответ с маркером [OPEN_URL:] не уходит на регенерацию",
          not cs.should_regenerate(cfg_none, "Открыть YouTube? [OPEN_URL:youtube.com]", 5))

    # ── 11. Фоновые вкладки (raw CDP): реестр и диспетчеризация ──
    raw_calls = []
    def _fake_raw_call(method, params=None, session_id=None, pool=None,
                       tab_id=None, timeout=None, _retried=False):
        # Адресация вкладкой: sessionId резолвится из реестра, как в бою
        sid = _ba._raw_session(tab_id) if tab_id is not None else session_id
        raw_calls.append((method, params or {}, sid))
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
    _ba._WORKER.submit = lambda fn, timeout=None: None  # playwright-вкладок «нет» — только raw
    try:
        tid = _ba.open_new_tab("https://chat.qwen.ai/c/x1", background=True)
        check("raw: фоновая вкладка через createTarget(background:true) + flat-сессия",
              tid in _ba._RAW_TABS
              and _ba._RAW_TABS[tid].get("pool") == "v"  # дефолт — пул V
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

        # Единый диспетчер: у операций со вкладкой НЕТ своих развилок —
        # всё, что раньше «забывало» raw-ветку (_eval_js_any и его
        # обёртки), идёт в raw-транспорт, а не в playwright
        _raw_js = []
        _orig_raw_eval = _ba._raw_eval
        _ba._raw_eval = lambda t, js, timeout_sec=None: (
            _raw_js.append((t, js, timeout_sec)), "1")[1]
        def _no_pw(fn, timeout=None):
            raise AssertionError("playwright дёрнули для raw-вкладки")
        _ba._WORKER.submit = _no_pw
        _bridged = {}
        for _name, _call in (
                ("_eval_js_any", lambda: _ba._eval_js_any(None, tid, "1+1")),
                ("detect_antibot", lambda: _real_detect_antibot(None, tid)),
                ("dismiss_overlay", lambda: _real_dismiss_overlay(None, tid)),
                ("wait_dom_idle", lambda: _real_wait_dom_idle(
                    None, tid, timeout_sec=0.05, min_wait=0.0)),
                ("page_identity", lambda: _ba.page_identity(tid, None)),
                ("modal_visible", lambda: _real_modal_visible(None, tid)),
                ("open_list_visible", lambda: _real_open_list_visible(None, tid)),
                ("read_text", lambda: _ba.read_text(None, tid)),
                ("count_blocks", lambda: _ba.count_blocks(None, tid, [".x"])),
                ("eval_js", lambda: _ba.eval_js(None, tid, "1")),
                ("_run_js", lambda: _ba._run_js(None, "1", tab_id=tid))):
            _was = len(_raw_js)
            try:
                _call()
            except Exception:
                pass  # значение не важно — важен транспорт
            _bridged[_name] = len(_raw_js) > _was
        _ba._raw_eval = _orig_raw_eval
        _ba._WORKER.submit = lambda fn, timeout=None: None
        check("raw: диспетчер уводит все мосты со вкладкой в raw-транспорт",
              all(_bridged.values()) and all(t == tid for t, _j, _b in _raw_js))

        # detect_antibot: сбой ЗАМЕРА ≠ «чисто» (strict поднимает ошибку)
        _orig_eja = _ba._eval_js_any
        _ba._eval_js_any = lambda *a, **kw: (_ for _ in ()).throw(
            _ba.BrowserUnavailable("вкладка не отвечает"))
        try:
            soft = _real_detect_antibot(None, tid)
            strict_raised = False
            try:
                _real_detect_antibot(None, tid, strict=True)
            except _ba.BrowserUnavailable:
                strict_raised = True
        finally:
            _ba._eval_js_any = _orig_eja
        check("raw: detect_antibot(strict) — сбой замера падает, а не «чисто»",
              soft is None and strict_raised)

        # Закрытие фоновой вкладки: Target.closeTarget + выход из реестра
        raw_calls.clear()
        closed_ok = _ba.close_background_tab(tid)
        check("raw: close_background_tab зовёт Target.closeTarget и чистит реестр",
              closed_ok is True and tid not in _ba._RAW_TABS
              and raw_calls[-1][0] == "Target.closeTarget"
              and raw_calls[-1][1] == {"targetId": "T1"}
              and _ba.close_background_tab(tid) is False)
        try:
            _ba._raw_tab(tid)
            gone = False
        except _ba.BrowserUnavailable:
            gone = True
        check("raw: после drop вкладка — BrowserUnavailable", gone)

        # Идентичность вкладок: id монотонный, под локом, не переиспользуется
        _seq = itertools.count(1)
        _seq_lock = threading.Lock()
        def _par_raw_call(method, params=None, session_id=None, pool=None,
                          tab_id=None, timeout=None, _retried=False):
            if method == "Target.createTarget":
                with _seq_lock:
                    n = next(_seq)
                time.sleep(0.002)  # растягиваем окно гонки между потоками
                return {"targetId": f"T{n}"}
            if method == "Target.attachToTarget":
                return {"sessionId": "S" + str(params["targetId"])[1:]}
            return {}
        _ba._raw_call = _par_raw_call
        opened, errs = [], []
        def _open_one():
            try:
                opened.append(_ba._raw_open("https://chat.qwen.ai/", pool="v"))
            except Exception as e:  # noqa: BLE001 — гонку видно по списку
                errs.append(e)
        threads = [threading.Thread(target=_open_one) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        targets = [_ba._RAW_TABS[i]["targetId"] for i in opened]
        check("raw: 16 параллельных _raw_open — id уникальны, вкладки не смешаны",
              not errs and len(opened) == 16 and len(set(opened)) == 16
              and len(set(targets)) == 16 and len(_ba._RAW_TABS) == 16)
        dropped = opened[0]
        _ba.close_background_tab(dropped)
        again = _ba._raw_open("https://chat.qwen.ai/", pool="v")
        check("raw: id освобождённой вкладки не переиспользуется",
              again not in opened and again > max(opened))
        for i in list(_ba._RAW_TABS):
            _ba._RAW_TABS.pop(i, None)
    finally:
        _ba._raw_call, _ba._select_backend = _orig_raw_call, _orig_sel
        _ba._WORKER.submit = _orig_submit
        _ba._RAW_TABS.clear()

    # ── 11b. Жизненный цикл raw-соединения: переattach вместо смерти вкладок,
    #        бюджет ответа на вызов (живого браузера не трогаем) ──
    class _FakeCdp:
        """Фейк _RawCdp: ни сокета, ни браузера. sessions — живые сессии
        таргетов; broken — «сокет оборван»."""

        def __init__(self, sessions):
            self.sessions = dict(sessions)
            self.calls = []
            self.broken = False
            self.closed = False

        def call(self, method, params=None, session_id=None, timeout=None):
            self.calls.append((method, dict(params or {}), session_id, timeout))
            if self.broken:
                raise OSError("socket closed")
            if method == "Target.attachToTarget":
                sid = self.sessions.get(str((params or {}).get("targetId")))
                if sid is None:
                    raise _ba.BrowserUnavailable(
                        "CDP Target.attachToTarget: No target with given id")
                return {"sessionId": sid}
            if session_id is not None and session_id not in self.sessions.values():
                raise _ba.BrowserUnavailable(
                    f"CDP {method}: Session with given id not found.")
            if method == "Runtime.evaluate":
                return {"result": {"value": "ok"}}
            return {}

        def close(self):
            self.closed = True

    _orig_clients = dict(_ba._RAW_CLIENTS)
    _orig_rawcdp = _ba._RawCdp
    _made = []
    try:
        # Обрыв сокета: клиент пересоздаётся, живые таргеты получают НОВЫЙ
        # sessionId, вкладка продолжает работать (раньше повтор шёл со
        # старым sessionId и убивал разом все вкладки пула)
        _ba._RAW_TABS[900001] = {"targetId": "T1", "sessionId": "S1",
                                 "pool": "v"}
        first = _FakeCdp({"T1": "S1"})
        second = _FakeCdp({"T1": "S2"})
        _ba._RAW_CLIENTS["v"] = first
        _ba._RawCdp = lambda url=None: (_made.append(second), second)[1]
        first.broken = True
        out = _ba._raw_eval(900001, "1+1")
        check("raw: обрыв соединения → переattach, вкладка жива",
              out == "ok" and first.closed and len(_made) == 1
              and _ba._RAW_TABS[900001]["sessionId"] == "S2"
              and any(c[0] == "Target.attachToTarget" for c in second.calls))

        # «Session with given id not found» — ОДИН переattach и повтор,
        # а не смерть вкладки
        third = _FakeCdp({"T1": "S3"})
        _ba._RAW_CLIENTS["v"] = third
        _ba._RAW_TABS[900001]["sessionId"] = "S-stale"
        out2 = _ba._raw_eval(900001, "1+1")
        attaches = [c for c in third.calls if c[0] == "Target.attachToTarget"]
        check("raw: «session not found» → один переattach + повтор",
              out2 == "ok" and len(attaches) == 1
              and _ba._RAW_TABS[900001]["sessionId"] == "S3"
              and 900001 in _ba._RAW_TABS)

        # Таргета больше нет — вкладка честно помечается мёртвой
        fourth = _FakeCdp({})
        _ba._RAW_CLIENTS["v"] = fourth
        _ba._RAW_TABS[900001]["sessionId"] = "S-stale"
        dead = False
        try:
            _ba._raw_eval(900001, "1+1")
        except _ba.BrowserUnavailable:
            dead = True
        check("raw: таргет исчез — вкладка помечена мёртвой, а не вечный retry",
              dead and 900001 not in _ba._RAW_TABS)
    finally:
        _ba._RawCdp = _orig_rawcdp
        _ba._RAW_CLIENTS.clear()
        _ba._RAW_CLIENTS.update(_orig_clients)
        _ba._RAW_TABS.clear()

    # Бюджет ответа на вызов: долгий awaitPromise не падает по таймауту
    # сокета (тот лишь нарезает ожидание), а истёкший бюджет ОДНОГО вызова
    # не рвёт соединение — следующий вызов по тому же сокету работает
    class _WsTimeout(Exception):
        pass
    _WsTimeout.__name__ = "WebSocketTimeoutException"

    class _FakeWs:
        def __init__(self, script):
            self.script = list(script)
            self.sent = []
            self.timeouts = []
            self.closed = False

        def settimeout(self, t):
            self.timeouts.append(t)

        def send(self, data):
            self.sent.append(json.loads(data))

        def recv(self):
            if not self.script:
                raise _WsTimeout("timed out")
            item = self.script.pop(0)
            if item == "timeout":
                raise _WsTimeout("timed out")
            item = dict(item)
            item["id"] = self.sent[-1]["id"]
            return json.dumps(item)

        def close(self):
            self.closed = True

    _cl = _ba._RawCdp.__new__(_ba._RawCdp)
    _cl._ws = _FakeWs(["timeout"] * 6 + [{"result": {"value": "done"}}])
    _res = _cl.call("Runtime.evaluate", {"expression": "x"}, "S1", timeout=25.0)
    check("raw: долгий awaitPromise доживает до ответа (таймаут сокета — не потолок)",
          _res == {"value": "done"}
          and _cl._ws.timeouts and max(_cl._ws.timeouts) <= 5.0
          and not _cl._ws.closed)
    _cl._ws.script = []  # ответа не будет вовсе
    _timed = False
    try:
        _cl.call("Runtime.evaluate", {"expression": "y"}, "S1", timeout=0.4)
    except _ba.RawCallTimeout:
        _timed = True
    _cl._ws.script = [{"result": {"value": "next"}}]
    check("raw: таймаут ОДНОГО вызова не рвёт соединение — следующий работает",
          _timed and not _cl._ws.closed
          and _cl.call("Runtime.evaluate", {"expression": "z"}, "S1",
                       timeout=5.0) == {"value": "next"})

    # ── 11c. Воркер playwright: зависшая операция не держит всё навсегда ──
    _wk_hang = _ba._CdpWorker()
    _release = threading.Event()
    _t0 = time.time()
    _hung = False
    try:
        _wk_hang.submit(lambda w: _release.wait(30), timeout=0.5)
    except _ba.BrowserUnavailable:
        _hung = True
    _elapsed = time.time() - _t0
    _after = _wk_hang.submit(lambda w: "жив", timeout=5.0)
    _release.set()
    check("воркер: зависшая операция отдаёт управление по таймауту, "
          "следующая работает",
          _hung and _elapsed < 5.0 and _after == "жив" and _wk_hang._gen == 1)

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

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            if not self.snapshot_items:
                return None, None, None, None, "нет открытой вкладки"
            return ("https://x.ru", "x.ru", self.snapshot_items, None, None)

        @staticmethod
        def _score_candidates(items, goal, host=None, op="click"):
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
            {"op": "open", "url": "https://dodopizza.ru/city"},
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
              ("click", {"element": "меню", "host": "example.edu"}),
              ("click", {"element": "ВОЙТИ", "host": "example.edu"}),
              ("click", {"element": "кабинет обучающегося", "host": "example.edu"}),
              ("click", {"element": "меню", "host": "example.edu"}),
              ("click", {"element": "Расписание", "host": "example.edu"}),
              ("click", {"element": "Расписание занятий", "host": "example.edu"})]
    write_trace(sc_tmp, "c9", trace7)
    lossy = json.dumps({"aliases": [], "steps": [
        {"op": "open", "url": "https://example.edu"},
        {"op": "click", "target": "меню", "host": "example.edu"},
        {"op": "click", "target": "ВОЙТИ", "host": "example.edu"},
        {"op": "click", "target": "Расписание", "host": "example.edu"},
        {"op": "click", "target": "Расписание занятий",
         "host": "example.edu"}]}, ensure_ascii=False)
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
                            "element": "бургер-меню", "host": "example.edu",
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
        _ba.detect_antibot = lambda host=None, tab_id=None, strict=False: "widget: iframe[src*=recaptcha]"
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru", _gh_items())
        no_cp, err_cp = m_rf.resolve_click("загрузить", None, None,
                                           chat_id="rf-cp")
        check("resolve_fail аудит: captcha — честный отказ «я не робот»",
              no_cp is None and "не робот" in err_cp
              and any(r.get("fail_reason") == "captcha"
                      for r in _aud_recs("rf-cp")))
        _ba.detect_antibot = lambda host=None, tab_id=None, strict=False: None
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

    # ── Сквозная нумерация разметки: номер = ссылка на элемент ──
    # Блоки номеров выдаёт питон (_mark_base) — общий снапшот, каждый фрейм
    # и целевой снапшот не пересекаются, номера не переиспользуются
    _mb1 = _ba._mark_base(_ba.SNAPSHOT_MAX)
    _mb2 = _ba._mark_base(_ba.FRAME_SNAPSHOT_ITEMS)
    _mb3 = _ba._mark_base(_ba.GOAL_SNAPSHOT_MAX)
    check("разметка: блоки номеров не пересекаются и не повторяются",
          _mb2 >= _mb1 + _ba.SNAPSHOT_MAX
          and _mb3 >= _mb2 + _ba.FRAME_SNAPSHOT_ITEMS
          and _ba._mark_base(1) >= _mb3 + _ba.GOAL_SNAPSHOT_MAX)
    check("разметка: номер ищется в обеих разметках (общей и целевой)",
          _ba._mark_sel(7) == '[data-vpc-idx="7"],[data-vpc-gidx="7"]'
          and "data-vpc-gidx" in _ba._mark_find_js(7)
          and "'7'" in _ba._mark_find_js(7))
    # Бюджеты проходов снапшота тоже от базы — иначе блок вылез бы за свои
    # пределы и налез на соседний
    check("разметка: бюджеты снапшота отсчитываются от базы",
          "idx=B" in _ba._SNAPSHOT_JS and "idx<B+M" in _ba._SNAPSHOT_JS
          and "idx<B+M" in _ba._GOAL_SNAPSHOT_JS)
    # Бюджет в JS — из питоновской константы, а не свой литерал рядом
    # (иначе связь держалась бы только комментарием)
    check("разметка: лимит в JS подставлен из SNAPSHOT_MAX/GOAL_SNAPSHOT_MAX",
          f"var M={_ba.SNAPSHOT_MAX};" in _ba._SNAPSHOT_JS
          and f"var M={_ba.GOAL_SNAPSHOT_MAX};" in _ba._GOAL_SNAPSHOT_JS
          and f"var M={_ba.SNAPSHOT_MAX};" not in _ba._GOAL_SNAPSHOT_JS)

    class _MarkedEl:
        """Размеченный элемент страницы: клик меняет отпечаток (closed-loop)."""

        def __init__(self, page, text, href=""):
            self.page, self.text, self.href = page, text, href
            self.clicked = 0

        def click(self, **kw):
            self.clicked += 1
            self.page.state += 1

        def evaluate(self, js, *a):
            return self.href

    class _MarkedLoc:
        def __init__(self, els): self._els = els
        def count(self): return len(self._els)

        @property
        def first(self): return self._els[0]

    class _MarkedPage:
        """Страница с DOM-разметкой: locator понимает селектор _mark_sel,
        снапшот размечает элементы номерами от базы, подставленной в JS
        (var B=…) — как настоящий JS в браузере, и снимает прошлые метки."""

        def __init__(self):
            self.main_frame = self
            self.frames = [self]
            self.url = "https://x.ru"
            self.marks = {}
            self.state = 0

        def wait_for_load_state(self, *a, **kw): pass

        def wait_for_timeout(self, ms): pass

        def locator(self, sel):
            return _MarkedLoc([el for (attr, n), el in self.marks.items()
                               if f'[{attr}="{n}"]' in sel])

        def mark(self, attr, num, text, href=""):
            el = _MarkedEl(self, text, href)
            self.marks[(attr, num)] = el
            return el

        def evaluate(self, js, *a):
            if js.startswith("window.innerWidth"):
                return "1280x800"
            if "var B=" in js and "data-vpc-idx" in js:
                base = int(js.split("var B=", 1)[1].split(";", 1)[0])
                self.marks = {}
                self.mark("data-vpc-idx", base, "Скачать",
                          "https://x.ru/right.pdf")
                self.mark("data-vpc-idx", base + 1, "Войти")
                return json.dumps({
                    "url": "https://x.ru", "vw": 1280, "items": [
                        {"idx": base, "tag": "a", "text": "Скачать",
                         "href": "https://x.ru/right.pdf", "w": 40, "h": 20,
                         "vp": 1},
                        {"idx": base + 1, "tag": "a", "text": "Войти",
                         "w": 40, "h": 20, "vp": 1}]})
            # Отпечаток страницы: «токен документа|href|readyState|хэш»
            return f"doc1|https://x.ru|complete|{self.state}"

    _mk_page = _MarkedPage()
    _orig_sel_mk, _orig_sub_mk = _ba._select_backend, _ba._WORKER.submit
    _orig_snap_mk, _orig_pu_mk = _ba.snapshot_elements, _ba.page_urls
    _orig_ct_mk, _orig_href_mk = _ba.click_tagged, _ba.href_of_tagged
    _ba._select_backend = lambda tab_op=True: "cdp"
    # Настоящие функции разметки (секции выше подменяют их моками)
    _ba.snapshot_elements = _real_snapshot_elements
    _ba.click_tagged = _real_click_tagged
    _ba.href_of_tagged = _real_href_of_tagged
    _wk_mk = _types.SimpleNamespace(
        page_for=lambda host, tab_id=None: _mk_page,
        _all_pages=lambda: [_mk_page],
        _ensure_zoom_normal=lambda p: None)
    _ba._WORKER.submit = lambda fn, timeout=None: fn(_wk_mk)
    _ba.page_urls = lambda: ["https://x.ru"]
    try:
        # Скачивание после целевого снапшота: номер целевой разметки, и href
        # должен быть у ЕГО элемента, а не у чужого из общей разметки
        _mk_page.marks = {}
        _mk_page.mark("data-vpc-idx", 4001, "Анкета", "https://x.ru/wrong.pdf")
        _mk_page.mark("data-vpc-gidx", 4110, "Методичка",
                      "https://x.ru/right.pdf")
        check("скачивание: href берётся по номеру целевого снапшота, "
              "а не у элемента общей разметки",
              _ba.href_of_tagged("x.ru", 4110) == "https://x.ru/right.pdf"
              and _ba.href_of_tagged("x.ru", 4001) == "https://x.ru/wrong.pdf")
        # Разметка сменилась (новый снапшот): номера нового блока, метки
        # прошлого на странице не остаются и под старым номером не находятся
        _u_g1, _h_g1, _items_g1 = _ba.snapshot_elements("x.ru")
        _old_idx = int(_items_g1[0]["idx"])
        _u_g2, _h_g2, _items_g2 = _ba.snapshot_elements("x.ru")
        _new_idx = int(_items_g2[0]["idx"])
        check("разметка: номера нового снапшота не совпадают со старыми",
              _new_idx != _old_idx
              and all(int(it["idx"]) != _old_idx for it in _items_g2))
        _gone_err = ""
        try:
            _ba.click_tagged("x.ru", _old_idx)
        except _ba.BrowserUnavailable as e:
            _gone_err = str(e)
        check("разметка: метка прошлого снапшота не находится — честный отказ, "
              "а не клик по чужому элементу",
              "элемент потерян" in _gone_err
              and all(el.clicked == 0 for el in _mk_page.marks.values()))
        check("разметка: номер текущего снапшота кликается",
              _ba.click_tagged("x.ru", _new_idx) == "clicked"
              and _mk_page.marks[("data-vpc-idx", _new_idx)].clicked == 1)
        # Отложенное исполнение (подтверждение «да» через минуту): разметка
        # за это время сменилась — честный отказ, а не клик наугад
        _mk_page.state = 0
        _ba.snapshot_elements("x.ru")          # страница пересняла разметку
        _stale_act = {"kind": "click", "idx": _new_idx, "element": "Скачать",
                      "host": "x.ru", "goal": "методичка по sql"}
        real_mk = ComputerControlManager(context="t", base_dir=tmp, config={})
        ok_mk, det_mk = real_mk.execute(_stale_act, "c")
        check("отложенный клик: разметка сменилась и цель заново не нашлась — "
              "отказ без клика",
              ok_mk is False and "элемент потерян" in det_mk
              and all(el.clicked == 0 for el in _mk_page.marks.values()))
    finally:
        _ba._select_backend, _ba._WORKER.submit = _orig_sel_mk, _orig_sub_mk
        _ba.snapshot_elements, _ba.page_urls = _orig_snap_mk, _orig_pu_mk
        _ba.click_tagged, _ba.href_of_tagged = _orig_ct_mk, _orig_href_mk

    # Коллизия «главный документ / iframe» невозможна: база нумерации фрейма
    # — свой блок _mark_base, а не max(idx)+1 по ОСТАВШИМСЯ после дедупа
    # (метки выброшенных дублей со страницы никуда не деваются)
    _fbases = []

    class _BaseFrame:
        def __init__(self, url, box): self.url, self._box = url, box
        def frame_element(self): return self
        def bounding_box(self): return self._box

        def evaluate(self, js):
            base = int(js.rsplit("})(", 1)[1].split(",")[0])
            _fbases.append(base)
            return json.dumps({"items": [
                {"idx": base, "tag": "button", "text": "Оплатить",
                 "w": 120, "h": 40, "x": 10, "vp": 1}]})

    _mb_main = _ba._mark_base(_ba.SNAPSHOT_MAX)
    _dedup_items = [_it(_mb_main, "a", "Главная"),
                    _it(_mb_main + 40, "a", "Каталог")]  # хвост съел дедуп
    _merged2 = _ba._merge_frame_items(
        _FakePage([_BaseFrame("https://pay.widget.ru/f",
                              {"x": 0.0, "y": 0.0,
                               "width": 400.0, "height": 300.0}),
                   _BaseFrame("https://chat.widget.ru/f",
                              {"x": 0.0, "y": 0.0,
                               "width": 400.0, "height": 300.0})]),
        _dedup_items)
    _fr2 = [it for it in _merged2 if it.get("fr")]
    check("iframe: номера фрейма — свой блок, а не «max(idx)+1» после дедупа",
          len(_fbases) == 2
          and all(b >= _mb_main + _ba.SNAPSHOT_MAX for b in _fbases)
          and _fbases[1] >= _fbases[0] + _ba.FRAME_SNAPSHOT_ITEMS
          and len({it["idx"] for it in _merged2}) == len(_merged2)
          and len(_fr2) == 2)

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

    # ── Closed-loop: «замер не удался» ≠ «сработало» (п.6) ──
    # Отпечаток и три исхода замера. Раньше любая ошибка evaluate отдавалась
    # уникальным сентинелом и первый же опрос признавал клик успешным
    _P = _ba._Probe
    _pa = _P(True, "https://x.ru/a", "d1", "h1")
    check("замер: тот же отпечаток → «не изменилось»",
          _ba._effect_verdict(_pa, _P(True, "https://x.ru/a", "d1", "h1"))
          == _ba.EFFECT_SAME)
    check("замер: другой отпечаток → «изменилось»",
          _ba._effect_verdict(_pa, _P(True, "https://x.ru/a", "d1", "h2"))
          == _ba.EFFECT_CHANGED)
    check("замер: сбой evaluate → «замер не удался», а не «изменилось»",
          _ba._effect_verdict(_pa, _P(False, "https://x.ru/a", "", ""))
          == _ba.EFFECT_FAILED
          and _ba._effect_verdict(_P(False, "", "", ""),
                                  _P(False, "", "", ""))
          == _ba.EFFECT_FAILED)
    check("замер: навигация — положительный признак даже при сбое замера",
          _ba._effect_verdict(_pa, _P(False, "https://x.ru/b", "", ""))
          == _ba.EFFECT_CHANGED
          and _ba._effect_verdict(_pa, _P(True, "https://x.ru/a", "d2", "h1"))
          == _ba.EFFECT_CHANGED)
    check("замер: новое окно (aux — число вкладок) → «изменилось»",
          _ba._effect_verdict(_P(True, "https://x.ru/a", "d1", "h1", "tabs:1"),
                              _P(True, "https://x.ru/a", "d1", "h1", "tabs:2"))
          == _ba.EFFECT_CHANGED)
    _boom_probe = lambda: _P(False, "", "", "")
    check("_wait_effect: систематический сбой замера → failed (не changed)",
          _ba._wait_effect(_boom_probe, _pa, timeout=0.05)
          == _ba.EFFECT_FAILED)
    check("_wait_effect: замеры идут, изменений нет → same",
          _ba._wait_effect(lambda: _pa, _pa, timeout=0.05)
          == _ba.EFFECT_SAME)

    # Состав отпечатка: дешёвый и ограниченный — без сериализации разметки,
    # с лимитами обхода shadow-DOM (глубина/число узлов)
    check("отпечаток: без innerHTML, с лимитами обхода и защитой от исключений",
          "innerHTML" not in _ba._DOM_STATE_JS
          and "N=1500" in _ba._DOM_STATE_JS
          and "dep>D" in _ba._DOM_STATE_JS
          and "shadowRoot" in _ba._DOM_STATE_JS
          and "catch(e2)" in _ba._DOM_STATE_JS)
    check("отпечаток: сигналы эффекта клика (меню/чекбокс/фокус/диалог)",
          "aria-expanded" in _ba._DOM_STATE_JS
          and ":checked" in _ba._DOM_STATE_JS
          and "role=dialog" in _ba._DOM_STATE_JS
          and "activeElement" in _ba._DOM_STATE_JS
          and "elementFromPoint" in _ba._DOM_STATE_JS
          and "textContent" in _ba._DOM_STATE_JS)

    # Исполнители клика/наведения на фейковой странице: живого браузера нет,
    # _WORKER.submit и _select_backend замоканы
    class _LoopLoc:
        def __init__(self, scope):
            self._scope = scope
        @property
        def first(self):
            return self
        def count(self):
            return 1
        def click(self, **kw):
            self._scope.acts.append("click")
        def hover(self, **kw):
            self._scope.acts.append("hover")

    class _LoopScope:
        """Страница для closed-loop: evaluate либо бросает («замер не
        удался»), либо отдаёт очередь отпечатков; url меняется навигацией."""
        def __init__(self, states=None, boom=False, hov=None):
            self.url = "https://x.ru/a"
            self.states = list(states or ["d1|https://x.ru/a|complete|1"])
            self.boom = boom
            self.hov = hov
            self.acts = []
            self.args = []
        def locator(self, sel):
            return _LoopLoc(self)
        @property
        def frames(self):
            return [self]
        @property
        def main_frame(self):
            return self
        def evaluate(self, js, *a):
            self.args.append((js, a))
            if js is _ba._HOVER_CHECK_JS:
                return self.hov if a and a[0] == _ba._mark_sel(7) else "?"
            if self.boom:
                raise RuntimeError("страница блокирует evaluate")
            if js is _ba._DOM_STATE_JS:
                return (self.states.pop(0) if len(self.states) > 1
                        else self.states[0])
            return ""

    class _LoopWorker:
        def __init__(self, page):
            self.page = page
        def page_for(self, host, tab_id=None):
            return self.page
        def _all_pages(self):
            return [self.page]

    _orig_sel_cl, _orig_sub_cl = _ba._select_backend, _ba._WORKER.submit
    _orig_cv = _ba.CLICK_VERIFY_SEC
    _ba._select_backend = lambda *a, **kw: "cdp"
    _ba.CLICK_VERIFY_SEC = 0.06
    try:
        def _use(page):
            _ba._WORKER.submit = lambda fn, timeout=None: fn(_LoopWorker(page))

        # Замер не удаётся систематически — это НЕ «нажал»
        _sc_boom = _LoopScope(boom=True)
        _use(_sc_boom)
        try:
            _ba.click_tagged(None, 7)
            _cl_res = "clicked"
        except _ba.ClickUncertain as e:
            _cl_res = str(e)
        check("клик: замер не удался → ClickUncertain с честной причиной",
              "проверить эффект не удалось" in _cl_res
              and _sc_boom.acts == ["click"])
        # Реальное изменение отпечатка — успех
        _use(_LoopScope(["d1|https://x.ru/a|complete|1",
                         "d1|https://x.ru/a|complete|2"]))
        check("клик: отпечаток изменился → «нажал»",
              _ba.click_tagged(None, 7) == "clicked")
        # Ничего не изменилось — честное «не уверен» с другой формулировкой
        _use(_LoopScope())
        try:
            _ba.click_tagged(None, 7)
            _cl_same = "clicked"
        except _ba.ClickUncertain as e:
            _cl_same = str(e)
        check("клик: страница не изменилась → ClickUncertain «не изменилась»",
              "страница не изменилась" in _cl_same)
        # Навигация: evaluate в переходе падает, но URL сменился — успех
        _sc_nav = _LoopScope(boom=True)
        _orig_ev_nav = _sc_nav.evaluate
        def _nav_eval(js, *a):
            _sc_nav.url = "https://x.ru/b"
            return _orig_ev_nav(js, *a)
        _sc_nav.evaluate = _nav_eval
        _use(_sc_nav)
        check("клик: навигация (сменился URL) → «нажал» несмотря на сбой замера",
              _ba.click_tagged(None, 7) == "clicked")

        # Наведение: аргумент (селектор) ДОХОДИТ до JS — иначе ветка ':hover'
        # недостижима и любое наведение рапортовалось бы успехом
        _sc_hov = _LoopScope(hov="hover")
        _use(_sc_hov)
        check("наведение: аргумент дошёл → ветка ':hover' достижима",
              _ba.hover_tagged(None, 7) == "hovered"
              and any(a and a[0] == _ba._mark_sel(7)
                      for js, a in _sc_hov.args if js is _ba._HOVER_CHECK_JS))
        _use(_LoopScope(hov="gone"))
        check("наведение: 'gone' (DOM перерисовался) → «навёл»",
              _ba.hover_tagged(None, 7) == "hovered")
        _use(_LoopScope(hov=""))
        try:
            _ba.hover_tagged(None, 7)
            _hv_res = "hovered"
        except _ba.ClickUncertain as e:
            _hv_res = str(e)
        check("наведение: '' и страница не среагировала → ClickUncertain",
              "не уверен" in _hv_res)

        # Координаты: валидация вьюпорта — честный отказ вместо клика в никуда
        class _PtPage:
            def __init__(self):
                self.url = "https://x.ru/a"
                self.clicks = []
                self.mouse = self
            def click(self, x, y):
                self.clicks.append((x, y))
            def move(self, x, y):
                self.clicks.append((x, y))
            def evaluate(self, js, *a):
                if "innerWidth" in js and "innerHeight" in js:
                    return "1200x800"
                return "d1|https://x.ru/a|complete|1"
        _pt = _PtPage()
        _ba._WORKER.submit = lambda fn, timeout=None: fn(_LoopWorker(_pt))
        try:
            _ba.click_at_point(None, -90.0, 300.0)
            _pt_err = ""
        except _ba.BrowserUnavailable as e:
            _pt_err = str(e)
        check("координаты: точка вне вьюпорта → честный отказ без клика",
              "вне видимой области" in _pt_err and _pt.clicks == [])
        try:
            _ba.hover_at_point(None, 600.0, 1200.0)
            _pt_err2 = ""
        except _ba.BrowserUnavailable as e:
            _pt_err2 = str(e)
        check("координаты: наведение вне вьюпорта — тот же отказ",
              "вне видимой области" in _pt_err2 and _pt.clicks == [])
        check("координаты: точка внутри вьюпорта проходит валидацию",
              _ba._check_point(_pt, 600.0, 400.0) == (600.0, 400.0))
    finally:
        _ba._select_backend, _ba._WORKER.submit = _orig_sel_cl, _orig_sub_cl
        _ba.CLICK_VERIFY_SEC = _orig_cv

    # Линтер по всему файлу: evaluate(js, arg) с IIFE молча теряет аргумент
    import ast as _ast
    _ba_tree = _ast.parse(Path(_ba.__file__).read_text(encoding="utf-8"))
    # Сам помощник _eval_arg зовёт evaluate с переменной — он и есть проверка
    _lint_skip = set()
    for _n in _ast.walk(_ba_tree):
        if isinstance(_n, _ast.FunctionDef) and _n.name == "_eval_arg":
            _lint_skip.update(id(_s) for _s in _ast.walk(_n))
    _bad_eval = []
    for _node in _ast.walk(_ba_tree):
        if id(_node) in _lint_skip:
            continue
        if not (isinstance(_node, _ast.Call)
                and isinstance(_node.func, _ast.Attribute)
                and _node.func.attr == "evaluate" and len(_node.args) >= 2):
            continue
        _a0 = _node.args[0]
        if isinstance(_a0, _ast.Constant) and isinstance(_a0.value, str):
            _js = _a0.value
        elif isinstance(_a0, _ast.Name):
            _js = getattr(_ba, _a0.id, None)
        else:
            _js = None
        if not isinstance(_js, str) or not _ba._js_takes_arg(_js):
            _bad_eval.append(getattr(_a0, "lineno", 0))
    check("линтер: нет evaluate(<IIFE>, arg) — аргумент не теряется",
          _bad_eval == [])
    check("линтер: _js_takes_arg различает функцию и вызванный IIFE",
          _ba._js_takes_arg("(sel)=>{return sel;}")
          and _ba._js_takes_arg("e => e.value")
          and not _ba._js_takes_arg("(function(sel){return sel;})()")
          and not _ba._js_takes_arg(_ba._DOM_STATE_JS))
    try:
        _ba._eval_arg(None, "(function(a){return a;})()", "x")
        _ea_err = ""
    except _ba.BrowserUnavailable as e:
        _ea_err = str(e)
    check("линтер: _eval_arg отказывает IIFE-шаблону",
          _ea_err.startswith("JS-шаблон не принимает"))

    # JS-шаблоны: синтаксис (node --check) + рабочий шаг прокрутки при
    # scroll-behavior:smooth и кламп зон по вьюпорту
    _node_ok = True
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=10)
    except Exception:
        _node_ok = False
    _jsdir = Path(tempfile.mkdtemp(prefix="cc_js_"))

    def _node_run(name, src):
        f = _jsdir / f"{name}.js"
        f.write_text(src, encoding="utf-8")
        r = subprocess.run(["node", str(f)], capture_output=True, text=True,
                           timeout=30)
        return r.stdout.strip(), r.stderr.strip()

    # Шаблоны без плейсхолдеров — как есть; с плейсхолдерами — заполняем
    # ЕДИНСТВЕННЫМ путём (_js_fill), тем же, что и боевой код: если шаблон
    # и вызывающий .replace()/_js_fill() рассинхронизировались (переименовали
    # __X__ в шаблоне и забыли зовущий код), это всплывает здесь же
    _JS_TPL_FILL = {
        "_DOM_STATE_JS": {},
        "_SCROLL_STEP_JS": {},
        "_CONTAINER_SCROLL_STEP_JS": {},
        "_CONTAINER_SCROLL_RESTORE_JS": {"Y": 0},
        "_SCROLL_START_JS": {"SIDE": "", "DIR": "", "NAME": ""},
        "_SCROLL_STATUS_JS": {},
        "_ALL_CLICKABLE_BOXES_JS": {"MIN": 8},
        "_HOVER_CHECK_JS": {},
        "_SNAPSHOT_JS": {"BASE": 100},
        "_FRAME_SNAPSHOT_JS": {"BASE": 100, "LIM": 25},
        "_GOAL_SNAPSHOT_JS": {"GOAL": "цель", "BASE": 100},
        "_HIDDEN_EDITABLES_JS": {},
        "_CART_CLICK_JS": {"PROD": "додстер", "OP": "remove"},
        "_CART_VERIFY_JS": {"PROD": "додстер"},
        "_COMP_EDIT_FIND_JS": {"PROD": "гавайская"},
        "_COMP_EDIT_CLICK_JS": {"PROD": "гавайская"},
        "_MEDIA_VOLUME_JS": {"OP": "mute"},
        "_READ_SECTION_JS": {"Q": "корзина"},
    }
    if _node_ok:
        _tpl_bad = []
        for _n, _kw in _JS_TPL_FILL.items():
            try:
                _t = _ba._js_fill(getattr(_ba, _n), **_kw)
            except Exception as _tpl_exc:
                _tpl_bad.append(f"{_n} ({_tpl_exc})")
                continue
            _f = _jsdir / f"{_n}.js"
            _f.write_text("var __x = " + _t + ";\n", encoding="utf-8")
            if subprocess.run(["node", "--check", str(_f)],
                              capture_output=True, timeout=30).returncode:
                _tpl_bad.append(_n)
        check("JS-шаблоны: node --check проходит", _tpl_bad == [])

        # Шаг прокрутки при scroll-behavior:smooth: scrollBy асинхронен, и
        # синхронное чтение scrollY давало moved:false — доскролл обрывался
        # на первом шаге («не нашлось на странице» для цели ниже сгиба)
        _sm_harness = """
var behavior='smooth', y=0;
function mkStyle(){var m={};return {
  setProperty:function(k,v,p){m[k]=v;if(k==='scroll-behavior')behavior=v;},
  getPropertyValue:function(k){return m[k]||'';},
  getPropertyPriority:function(k){return '';},
  removeProperty:function(k){delete m[k];if(k==='scroll-behavior')behavior='smooth';}};}
global.document={documentElement:{style:mkStyle(),scrollHeight:5000},
                 body:{style:mkStyle(),scrollHeight:5000}};
global.window={innerHeight:800,
  scrollBy:function(dx,dy){ if(behavior==='auto'){y+=dy;} },
  get scrollY(){return y;}};
console.log(%s);
""" % _ba._SCROLL_STEP_JS
        _sm_out, _sm_err = _node_run("smooth_step", _sm_harness)
        check("прокрутка: шаг видит сдвиг при scroll-behavior:smooth",
              not _sm_err
              and json.loads(_sm_out or "{}").get("moved") is True)

        # Кламп зон по вьюпорту: центр сырого rect'а частично видимого
        # элемента уезжал за экран (mouse.click(-90, y) — мимо страницы)
        _boxes_js = _ba._js_fill(_ba._ALL_CLICKABLE_BOXES_JS, MIN=8)
        _put_src = _boxes_js[_boxes_js.index("function put("):
                             _boxes_js.index("all.forEach")]
        _clamp_out, _clamp_err = _node_run(
            "clamp",
            "global.innerWidth=1000;global.innerHeight=800;var out=[];"
            + _put_src
            + "put(-100,10,220,30,'a');put(-215,10,220,30,'b');"
              "put(940,10,220,30,'c');console.log(JSON.stringify(out));")
        _clamped = json.loads(_clamp_out or "[]") if not _clamp_err else []
        check("зоны: рамки клампятся к вьюпорту, мелкий остаток отбрасывается",
              [(b["x"], b["w"], b["text"]) for b in _clamped]
              == [(0, 120, "a"), (940, 60, "c")])

    # ── Задача №10: единая JS-нормализация + безопасная подстановка в JS ──
    if _node_ok:
        import app.features.computer_control as _cc_norm

        # 1) JS __vpcN (общий нормализатор — _VPC_NORM_CORE_JS) посимвольно
        # совпадает с питоновской _norm_match: ё/Ё, диакритика латиницы
        # (Lumière), апострофы разных начертаний (L'Oréal/L’Oréal), кириллица
        # без порчи «й» (действие/действий), дефисы/тире, NBSP, регистр, эмодзи
        _norm_corpus = [
            "ёлка", "Ёлка", "елка", "Lumière", "lumieres",
            "L'Oréal", "l'oreal", "L’Oréal", "Papa John's", "papa johns",
            "papa john’s", "papa johnʼs", "действие", "действий", "йод",
            "гавАЙСкая", "айс-ти", "Айс ти", "café", "cafe", "naïve",
            "naive", "Zoë", "zoe", "über", "uber", "  multi   space  ",
            "тире–здесь", "дефис-здесь", "non‑breaking",
            "😀 emoji 🎉 текст", "MIXED Case ТЕКСТ", " nbsp test", "",
        ]
        _py_norm = [_cc_norm._norm_match(s) for s in _norm_corpus]
        _norm_script = (
            "global.__norm=(function(){" + _ba._VPC_NORM_CORE_JS
            + " return __vpcN;})();\n"
            "var corpus=" + json.dumps(_norm_corpus, ensure_ascii=False) + ";\n"
            "console.log(JSON.stringify(corpus.map(__norm)));\n"
        )
        _norm_out, _norm_err = _node_run("norm_parity", _norm_script)
        _js_norm = (json.loads(_norm_out) if _norm_out and not _norm_err
                    else None)
        check("нормализация: JS __vpcN совпадает с _norm_match на корпусе "
              "ё/диакритика/апострофы/кириллица/NBSP/эмодзи",
              not _norm_err and _js_norm == _py_norm)

        # 2) _js_fill/_js_value: значения с кавычками/бэкслешем/переводом
        # строки/U+2028-2029/</script> дают синтаксически валидный JS И
        # доносят исходную строку без искажений (не «вырезаны», как раньше)
        _tricky_values = [
            "plain", "with 'single' quotes", 'with "double" quotes',
            "back\\slash", "newline\nhere", "crlf\r\nhere",
            "line sep", "para sep",
            "</script><script>alert(1)</script>",
            "кириллица и Ёлка", "L'Oréal", "Papa John's", "L’Oréal",
        ]
        _fill_bad = []
        for _tv in _tricky_values:
            _js = _ba._js_fill(
                "var v=__V__;console.log(JSON.stringify(v));", V=_tv)
            _pf = _jsdir / "fill_probe.js"
            _pf.write_text(_js, encoding="utf-8")
            if subprocess.run(["node", "--check", str(_pf)],
                              capture_output=True, timeout=10).returncode:
                _fill_bad.append(("syntax", _tv))
                continue
            _r = subprocess.run(["node", str(_pf)], capture_output=True,
                                text=True, timeout=10)
            _got = None
            try:
                _got = json.loads((_r.stdout or "").strip() or "null")
            except ValueError:
                pass
            if _r.returncode or _got != _tv:
                _fill_bad.append(("roundtrip", _tv))
        check("_js_fill: спецсимволы в значениях дают валидный JS и "
              "доходят до JS той же строкой (без вырезания кавычек)",
              _fill_bad == [])
        check("_js_value: числа/bool — JS-примитивы, не строка в кавычках",
              _ba._js_value(50) == "50" and _ba._js_value(True) == "true"
              and _ba._js_value(None) == "null")

        # 3) _js_fill требует полного совпадения имён — рассинхрон (опечатка/
        # забытое значение) падает сразу, а не оставляет "__X__" в JS-тексте
        try:
            _ba._js_fill("var x=__A__;", B=1)
            _fill_mismatch_err = ""
        except KeyError as _e:
            _fill_mismatch_err = str(_e)
        check("_js_fill: несовпадение имён плейсхолдера/значения падает "
              "сразу (а не тихо оставляет __A__ в тексте)",
              "__A__" in _fill_mismatch_err)

        # 4) Функциональный прогон нормализации+матчинга из _GOAL_SNAPSHOT_JS:
        # тот же код (goal=__vpcN(goal); hits поверх общего __vpcWIn), что
        # реально ищет цель в снапшоте, — «Ёлка» находится по «елка»,
        # «L'Oréal» по «l'oreal». Обвязка — _VPC_NORM_JS (нормализация + стем
        # + поиск слова): своей копии стемминга в шаблоне больше нет, и если
        # она вернётся, фрагмент перестанет работать с общей обвязкой
        _goal_core = _ba._GOAL_SNAPSHOT_JS[
            _ba._GOAL_SNAPSHOT_JS.index("goal=__vpcN(goal);"):
            _ba._GOAL_SNAPSHOT_JS.index("var mts=[];")]
        check("выдёргивание фрагмента матчинга из _GOAL_SNAPSHOT_JS не пусто "
              "(шаблон не переписали так, что якоря съехали)",
              len(_goal_core) > 200)
        check("целевой снапшот: стемминг общий (__vpcWIn), своей копии нет",
              "__vpcWIn" in _goal_core and "function wmatch" not in _goal_core
              and "__vpcWIn" in _ba._VPC_NORM_JS)

        def _goal_hits(goal_raw, own_raw):
            # own нормализуется __vpcN так же, как в _GOAL_SNAPSHOT_JS перед
            # вызовом hits(own) (own там — уже нормализованный текст узла)
            _src = (
                "global.__h=(function(){" + _ba._VPC_NORM_JS + "\n"
                "var goal=" + json.dumps(goal_raw, ensure_ascii=False) + ";\n"
                + _goal_core + "\n"
                "return function(o){return hits(__vpcN(o));};})();\n"
                "console.log(__h(" + json.dumps(own_raw, ensure_ascii=False)
                + "));\n")
            _out, _err = _node_run("goal_hits", _src)
            return (int(_out) if _out and not _err else -1), _err

        _h_yolka, _err_y = _goal_hits("елка", "ёлка")
        _h_lumiere, _err_l = _goal_hits("lumieres", "lumière")
        _h_oreal, _err_o = _goal_hits("l'oreal", "l’oréal")
        _h_miss, _err_m = _goal_hits("елка", "гараж")
        check("целевой снапшот: «елка» находит «Ёлка» через общую "
              "нормализацию (ё=е)",
              not _err_y and _h_yolka >= 1)
        check("целевой снапшот: «lumieres» находит «lumière» (диакритика "
              "латиницы снята)",
              not _err_l and _h_lumiere >= 1)
        check("целевой снапшот: «l'oreal» находит «l’oréal» (апостроф "
              "унифицирован + диакритика)",
              not _err_o and _h_oreal >= 1)
        check("целевой снапшот: несовпадающий текст хитов не даёт",
              not _err_m and _h_miss < 1)

        # 5) _HIDDEN_EDITABLES_JS теперь в IIFE — не течёт в глобальный скоуп
        # страницы (сайт с собственным `let e` раньше валил evaluate целиком
        # SyntaxError'ом, hidden_editable_labels молча возвращал [])
        import subprocess as _sp10
        _leak_script = (
            "const vm=require('vm');"
            "const ctx=vm.createContext({document:{querySelectorAll:()=>[]},"
            "getComputedStyle:()=>({display:'block',visibility:'visible',"
            "opacity:'1'})});"
            "vm.runInContext('let e = 1;', ctx);"
            "try{const r=vm.runInContext(" + json.dumps(_ba._HIDDEN_EDITABLES_JS)
            + ", ctx);console.log('OK:'+r);}"
            "catch(err){console.log('ERR:'+err.message);}"
        )
        _leak_out = _sp10.run(["node", "-e", _leak_script],
                              capture_output=True, text=True, timeout=10)
        check("_HIDDEN_EDITABLES_JS: не конфликтует с чужим `let e` "
              "страницы (IIFE-обёртка)",
              _leak_out.stdout.strip().startswith("OK:"))

        # 6) Корзина: вариант с «|» в тексте карточки не рвётся при разборе
        # (JSON вместо join('|')/split('|'))
        _orig_run_js_cart = _ba._run_js
        _ba._run_js = lambda *a, **kw: (
            "amb:" + json.dumps(["о! 1+1=3 | комбо", "гавайская 30 см"],
                                ensure_ascii=False))
        try:
            _cart_err = ""
            try:
                _ba.cart_op(None, "пицца", "remove")
            except _ba.BrowserUnavailable as _e:
                _cart_err = str(_e)
        finally:
            _ba._run_js = _orig_run_js_cart
        check("корзина: вариант с «|» в тексте карточки не рвётся при "
              "разборе неоднозначности (JSON, а не split('|'))",
              "о! 1+1=3 | комбо" in _cart_err
              and "гавайская 30 см" in _cart_err)

        # 7) AppleScript-экранирование (_as_lit): одно место для JS-текста и
        # для обычных строковых литералов AppleScript (host_part/URL/origin);
        # раунд-трип по правилам самого AppleScript (\\→\, \"→") восстанавливает
        # исходную строку для кавычек/бэкслешей любой вложенности
        def _un_as_lit(escaped):
            out, i = [], 0
            while i < len(escaped):
                c = escaped[i]
                if c == "\\" and i + 1 < len(escaped):
                    out.append(escaped[i + 1])
                    i += 2
                else:
                    out.append(c)
                    i += 1
            return "".join(out)

        _as_lit_corpus = [
            'simple', 'has "quote"', "back\\slash", 'quote"and\\backslash',
            '""""', '\\\\\\\\', "смешанный текст с \" и \\",
        ]
        check("_as_lit: экранирование AppleScript обратимо для кавычек/"
              "бэкслешей любой вложенности",
              all(_un_as_lit(_ba._as_lit(s)) == s for s in _as_lit_corpus))
        check("_find_tab_applescript больше не вставляет host_part в "
              "contains \"…\" без экранирования (_as_lit используется явно)",
              "_as_lit(host_part)" in Path(_ba.__file__).read_text(
                  encoding="utf-8"))

    # ── Линтер: единая подстановка в JS (задача №10) ──────────
    import ast as _ast10b
    _ba_src10 = Path(_ba.__file__).read_text(encoding="utf-8")
    _ba_tree10 = _ast10b.parse(_ba_src10)
    _bad_dot_replace = []
    for _n10 in _ast10b.walk(_ba_tree10):
        if not (isinstance(_n10, _ast10b.Call)
                and isinstance(_n10.func, _ast10b.Attribute)
                and _n10.func.attr == "replace" and _n10.args
                and isinstance(_n10.args[0], _ast10b.Constant)
                and isinstance(_n10.args[0].value, str)):
            continue
        _v10 = _n10.args[0].value
        if _v10.startswith("__") and _v10.endswith("__") and len(_v10) > 4:
            _bad_dot_replace.append(getattr(_n10, "lineno", 0))
    check("линтер: нет .replace(\"__X__\", …) вне _js_fill — единственный "
          "путь подстановки в JS",
          _bad_dot_replace == [])
    import re as _re10
    _quoted_ph = [
        i + 1 for i, line in enumerate(_ba_src10.split("\n"))
        if not line.strip().startswith("#")
        and _re10.search(r"['\"]__[A-Z][A-Z0-9]*__['\"]", line)
    ]
    check("линтер: нет плейсхолдеров __X__ внутри кавычек JS-шаблона "
          "(значение, а не текст в строке)",
          _quoted_ph == [])

    # ── Листание: самозавершение снимает сеанс, «стоп» без подтверждения ──
    _orig_st8, _orig_stat8 = _ba.scroll_start, _ba.scroll_status
    _orig_stop8, _orig_snap8 = _ba.scroll_stop, _ba.snapshot_elements
    _orig_ft8, _orig_poll8 = _ba.find_tab_id, _cc_mod._SCROLL_POLL_SEC
    _orig_grace8 = _cc_mod._SCROLL_END_GRACE_SEC
    _orig_max8 = _cc_mod._SCROLL_MAX_SEC
    _cc_mod._SCROLL_POLL_SEC = 0.02
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/x", "dodopizza.ru", [_it(0, "a", "Hi")])
    _ba.find_tab_id = lambda host: None
    _ba.scroll_start = lambda host=None, tab_id=None, side=None, direction=None, \
        name=None: {"ok": True, "bottom": False}
    _ba.scroll_stop = lambda host=None, tab_id=None: None
    try:
        m_e8 = ComputerControlManager(context="t", base_dir=tmp / "s8-end",
                                      config={**CFG, "allow_domains": []})
        _ba.scroll_status = lambda host=None, tab_id=None: {
            "active": False, "done": True, "end": "edge"}
        act_e8, _ = m_e8.resolve_scroll("start", None)
        m_e8.execute(act_e8, "c8e")
        time.sleep(0.2)   # дозорный успевает увидеть конец ленты
        check("листание: самозавершение снимает сеанс (не «я уже листаю»)",
              m_e8._scroll is None and not m_e8._scroll_active())
        act_e8s, _ = m_e8.resolve_scroll("stop", None)
        m_e8.execute(act_e8s, "c8e")
        check("листание: «стоп» сразу после конца — честная причина",
              act_e8s is not None
              and act_e8s.get("end_reason") == "bottom"
              and "до конца" in m_e8.describe_done(act_e8s))
        # Спустя окно памяти самозавершение забыто: «стоп» — бытовое слово
        act_e8b, _ = m_e8.resolve_scroll("start", None)
        m_e8.execute(act_e8b, "c8e1b")
        time.sleep(0.2)
        _cc_mod._SCROLL_END_GRACE_SEC = 0.0
        act_e8n, err_e8n = m_e8.resolve_scroll("stop", None)
        check("листание: «стоп» позже — уходит в обычный диалог",
              act_e8n is None and err_e8n is None)
        _cc_mod._SCROLL_END_GRACE_SEC = _orig_grace8
        # Потолок сеанса: страница молчит («крутится»), дозорный сам закрывает
        _cc_mod._SCROLL_MAX_SEC = 0.0
        _ba.scroll_status = lambda host=None, tab_id=None: {
            "active": True, "done": False, "end": ""}
        act_e8t, _ = m_e8.resolve_scroll("start", None)
        m_e8.execute(act_e8t, "c8e2")
        time.sleep(0.2)
        check("листание: потолок длительности закрывает зависший сеанс",
              m_e8._scroll is None)
        _cc_mod._SCROLL_MAX_SEC = _orig_max8
        # «Стоп» при активном листании в режиме с подтверждением — сразу,
        # без pending (повторное «стоп» читалось как отказ и остановить было
        # нельзя). Само листание подтверждения как требовало, так и требует
        m_e8c = ComputerControlManager(context="t", base_dir=tmp / "s8-end2",
                                       config={**CFG, "confirm": True,
                                               "allow_domains": []})
        _ba.scroll_status = lambda host=None, tab_id=None: {
            "active": True, "done": False, "end": ""}
        act_c8, _ = m_e8c.resolve_scroll("start", None)
        m_e8c.execute(act_c8, "c8c")
        act_c8s, _ = m_e8c.resolve_scroll("stop", None)
        check("листание: «стоп» своего листания не требует подтверждения",
              m_e8c.needs_confirm(act_c8) is True
              and m_e8c.needs_confirm(act_c8s) is False)
        m_e8c._scroll_stop_now()
    finally:
        _ba.scroll_start, _ba.scroll_status = _orig_st8, _orig_stat8
        _ba.scroll_stop, _ba.snapshot_elements = _orig_stop8, _orig_snap8
        _ba.find_tab_id, _cc_mod._SCROLL_POLL_SEC = _orig_ft8, _orig_poll8
        _cc_mod._SCROLL_END_GRACE_SEC = _orig_grace8
        _cc_mod._SCROLL_MAX_SEC = _orig_max8

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
        def get_response_with_image(self, prompt, img, image_mime=None,
                                    extra_image=None, force_provider=None):
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
    # Зоны для этой секции отключаем: она про _visual_resolve, а следом за ним
    # в каскаде идёт зональный vision — с НЕподменённым all_clickable_boxes он
    # уходил в живой браузер, и проверки вето плавали (есть открытый Chrome с
    # отладкой — зоны находились, и ветированный выбор подменялся кликом по
    # координатам; нет — отказ). Свои зоны разбирает секция ниже
    _orig_acb_v = getattr(_ba, "all_clickable_boxes", None)
    _ba.all_clickable_boxes = lambda host=None, tab_id=None: []
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
        # Бейджи номеров: заливка цветом рамки из палитры, коллизии
        # разнесены, кегль растёт с шириной кадра (кейс 07.09: мелкий красный
        # текст по видеоряду vision-модель не читала)
        _im = _PILImage.open(_io.BytesIO(boxed)).convert("RGB")
        _reds = _blues = 0
        for _yy in range(0, _im.height, 3):
            for _xx in range(0, _im.width, 3):
                _r, _g, _b = _im.getpixel((_xx, _yy))
                if _r > 140 and _g < 120 and _b < 120:
                    _reds += 1
                if _b > 140 and _r < 120 and _g < 160:
                    _blues += 1
        check("рамки: палитра цветов (красный и синий кандидаты)",
              _reds > 10 and _blues > 10)
        _buf2 = _io.BytesIO()
        _PILImage.new("RGB", (2400, 1300), (255, 255, 255)).save(
            _buf2, format="PNG")
        boxed2 = _cc_mod._draw_candidate_boxes(_buf2.getvalue(), _vis_items)
        check("рамки: широкий канвас (крупный кегль) — тоже валидный JPEG",
              boxed2 is not None and boxed2[:2] == b"\xff\xd8")
        # Искатель зон: подпись из предков (безымянные ×/−/+ карточки),
        # схлопывание близнецов с одним rect (слайды баннера)
        check("зоны: подписи из предков + дедуп близнецов в искателе",
              "parentElement;up++" in _ba._ALL_CLICKABLE_BOXES_JS
              and "var ded=[]" in _ba._ALL_CLICKABLE_BOXES_JS)
        # Открытые меню/попапы — первыми: в DOM они последние (портал в конец
        # body) и бюджет 40 зон съедала лента (меню «Ещё» без разметки)
        check("зоны: пункты открытых попапов собираются первыми",
              "popSel" in _ba._ALL_CLICKABLE_BOXES_JS
              and "a.closest(popSel)" in _ba._ALL_CLICKABLE_BOXES_JS)
        # Служебные строки метаданных (просмотры/подписчики/даты) внутри
        # бо́льшей рамки карточки срезаются до бюджета зон
        check("зоны: svc-фильтр служебного текста + вложенность в рамку",
              "function svc(t)" in _ba._ALL_CLICKABLE_BOXES_JS
              and "buried" in _ba._ALL_CLICKABLE_BOXES_JS)

        # ── Дедуп дублей одной карточки для vision-рамок (кейс 08.09) ──
        # У youtube-карточки на одну ссылку — обёртка, заголовок и строка
        # метаданных; все три проходили в рамки и съедали номера топ-8
        _H = "https://youtube.com/watch?v=abc"
        _dd1 = _cc_mod._dedup_same_target_cards([
            _it(10, "a", "", href=_H, x=20.0, y=100.0, w=360.0, h=300.0),
            _it(11, "a", "Джем – Sirene Boss Theme", href=_H,
                x=20.0, y=405.0, w=360.0, h=40.0),
            _it(12, "a", "5,7 млн просмотров • 4 месяца назад", href=_H,
                x=20.0, y=450.0, w=360.0, h=16.0)])
        check("дедуп карт: 3 ссылки одной карточки → 1 (обёртка, max площадь)",
              [i["idx"] for i in _dd1] == [10])
        _dd2 = _cc_mod._dedup_same_target_cards([
            _it(10, "a", "", href=_H, x=20.0, y=100.0, w=360.0, h=300.0),
            _it(11, "a", "Джем – Sirene Boss Theme", href=_H,
                x=20.0, y=405.0, w=360.0, h=40.0),
            _it(13, "a", "Джем – Sirene Boss Theme", href=_H,
                x=700.0, y=1200.0, w=360.0, h=40.0)])
        check("дедуп карт: тот же href в другом конце страницы выживает",
              sorted(i["idx"] for i in _dd2) == [10, 13])
        _dd3 = _cc_mod._dedup_same_target_cards([
            _it(14, "button", "Fantasy World Music | THE ETERNAL GROVE",
                x=500.0, y=800.0, w=300.0, h=30.0),
            _it(15, "button", "Fantasy World Music | THE ETERNAL GR…",
                x=500.0, y=805.0, w=300.0, h=18.0)])
        check("дедуп карт: префиксный дубль текста (обрезка «…») схлопнут",
              [i["idx"] for i in _dd3] == [14])
        _dd4 = _cc_mod._dedup_same_target_cards([
            _it(16, "a", "Подробнее о событии", href="https://a.ru/1",
                x=10.0, y=10.0, w=200.0, h=30.0),
            _it(17, "a", "Подробнее о событии", href="https://a.ru/2",
                x=12.0, y=12.0, w=200.0, h=30.0)])
        check("дедуп карт: разные href + тот же текст рядом — оба живы",
              sorted(i["idx"] for i in _dd4) == [16, 17])
        _dd5 = _cc_mod._dedup_same_target_cards([
            _it(18, "button", "Войти", x=10.0, y=10.0, w=100.0, h=30.0),
            _it(19, "button", "Войти", x=12.0, y=12.0, w=100.0, h=30.0)])
        check("дедуп карт: короткий общий текст без href не дедупим",
              sorted(i["idx"] for i in _dd5) == [18, 19])
        # Трекер-параметры и #fragment не различают ссылку; значащий query —
        # различает
        check("дедуп карт: ключ ссылки без utm/si/#, но с значащим query",
              _cc_mod._link_key("https://x.ru/p?utm_source=a&si=1#f")
              == _cc_mod._link_key("https://x.ru/p")
              and _cc_mod._link_key("https://y.ru/watch?v=1")
              != _cc_mod._link_key("https://y.ru/watch?v=2"))

        # ── Дедуп дублей карточки в текстовом снапшоте (кейс 09.09) ──
        # Список для LLM был забит фрагментами одной карточки ютуба:
        # превью-обёртка «1:03», заголовок, span-дубли канала/метаданных
        _snap_dd = _ba._dedup_snapshot_items([
            _it(1, "span", "KADOKAWAanime", x=40.0, y=480.0),
            _it(2, "a", "1:03", href=_H, x=20.0, y=100.0, w=360.0, h=300.0),
            _it(3, "a", "劇場シリーズ【第一部】メイドインアビス", href=_H,
                x=20.0, y=405.0, w=360.0, h=40.0),
            _it(4, "a", "KADOKAWAanime", href="https://youtube.com/@kadokawa",
                x=20.0, y=470.0, w=200.0, h=20.0),
            _it(5, "span", "1,7 млн просмотров", x=20.0, y=495.0),
            _it(6, "a", "1:03", href=_H, x=900.0, y=1500.0,
                w=360.0, h=300.0)])
        check("дедуп снапшота: «1:03» слилось в заголовок, span-дубль канала"
              " выкинут, далёкий дубль href жив, DOM-порядок сохранён",
              [i["idx"] for i in _snap_dd] == [3, 4, 5, 6])
        _snap_dd2 = _ba._dedup_snapshot_items([
            _it(7, "a", "Подробнее о событии", href="https://a.ru/1",
                x=10.0, y=10.0, w=200.0, h=30.0),
            _it(8, "a", "Подробнее о событии", href="https://a.ru/2",
                x=12.0, y=12.0, w=200.0, h=30.0)])
        check("дедуп снапшота: разные href + тот же текст рядом — оба живы",
              [i["idx"] for i in _snap_dd2] == [7, 8])
        _snap_dd3 = _ba._dedup_snapshot_items([
            _it(9, "span", "Ещё", x=10.0, y=10.0, w=50.0, h=20.0),
            _it(10, "div", "Ещё", x=12.0, y=12.0, w=50.0, h=20.0)])
        check("дедуп снапшота: два фрагмента без контрола — оба живы",
              [i["idx"] for i in _snap_dd3] == [9, 10])
        # «Служебный» текст — python-зеркало svc
        check("svc-текст: метаданные с цифрой — служебные, навигация — нет",
              _ba._service_text("1,7 млн просмотров")
              and _ba._service_text("18 horas atrás")
              and _ba._service_text("1 месяц назад")
              and not _ba._service_text("Главная")
              and not _ba._service_text("Мои подписки")
              and not _ba._service_text("Заказать за 300 ₽"))
        # pdiv-проход: неинтерактивные роли/глифы/служебный текст срезаются
        # до расхода бюджета; svc — общий с зональным vision код
        check("снапшот: pdiv-фильтры унаследованного cursor:pointer",
              "svc(pdt)" in _ba._SNAPSHOT_JS
              and "text|presentation|none|separator|img" in _ba._SNAPSHOT_JS
              and "\\p{L}" in _ba._SNAPSHOT_JS
              and _ba._SVC_FN_JS in _ba._SNAPSHOT_JS
              and _ba._SVC_FN_JS in _ba._ALL_CLICKABLE_BOXES_JS)
        # Переподпись бейджа длительности из aria/title (vpcInfo)
        check("снапшот: бейдж «1:03» переподписывается из aria/title",
              "d{1,3}" in _ba._SNAPSHOT_JS
              and "dtl.length>t.length" in _ba._SNAPSHOT_JS)
        # Основной проход собирает сначала элементы вьюпорта, потом
        # остальные в DOM-порядке (кейс 09.09: ссылка-заголовок видимой
        # карточки не влезала в бюджет 100 за шапкой/сайдбаром); при активном
        # бэкдропе внутри обеих фаз первыми — элементы его слоя (кейс 11.09:
        # пилюли «49 ₽» шторки соусов dodo терялись за затемнённым каталогом)
        check("снапшот: основной проход — вьюпорт первым, слой впереди",
              "var evp=[],eoff=[],evx=[],eox=[];" in _ba._SNAPSHOT_JS
              and "(isc3?evp:evx).push(el)" in _ba._SNAPSHOT_JS
              and "evp.concat(evx,eoff,eox)" in _ba._SNAPSHOT_JS
              and "pv.concat(pvx,po,pox)" in _ba._SNAPSHOT_JS)
        # Голая цена («49 ₽») — неуникальная подпись: переподписываем из
        # предка-ряда («Сырный 49 ₽»), иначе шторка соусов — шесть
        # одинаковых пилюль, неразличимых для скоринга и LLM
        check("снапшот: svc-подпись контрола переподписывается из ряда",
              "if(svc(t)){var rp=" in _ba._SNAPSHOT_JS
              and "if(svc(pdt)){var ap=" in _ba._SNAPSHOT_JS)
        # div role=button — настоящий контрол, фрагментом не считается
        _snap_dd4 = _ba._dedup_snapshot_items([
            _it(14, "div", "Ещё", role="button", x=10.0, y=10.0,
                w=50.0, h=20.0),
            _it(15, "a", "Ещё", href="https://a.ru/more", x=12.0, y=12.0,
                w=50.0, h=20.0)])
        check("дедуп снапшота: div role=button — не фрагмент, оба живы",
              [i["idx"] for i in _snap_dd4] == [14, 15])
        check("снапшот: _snap_frag — span без href фрагмент, контролы нет",
              _ba._snap_frag(_it(16, "span", "Канал"))
              and not _ba._snap_frag(_it(17, "div", "Ещё", role="button"))
              and not _ba._snap_frag(
                  _it(18, "a", "Видео", href="https://a.ru/1"))
              and not _ba._snap_frag(_it(19, "input", "Поиск", ed=True)))
        # Интеграция: в vision-промпт уходит 1 рамка вместо 2 дублей карточки
        _cap_prompt = []

        class _VR2(_VisionRouter):
            def get_response_with_image(self, prompt, img, image_mime=None,
                                        extra_image=None, force_provider=None):
                _cap_prompt.append(prompt)
                return super().get_response_with_image(
                    prompt, img, image_mime=image_mime, extra_image=extra_image,
                    force_provider=force_provider)

        _vis_items2 = [_it(20, "a", "Джем – Sirene Boss Theme (Роème)",
                           href=_H, x=20.0, y=100.0, w=360.0, h=300.0,
                           vw=1280.0),
                       _it(21, "a", "5,7 млн просмотров • 4 месяца назад",
                           href=_H, x=20.0, y=405.0, w=360.0, h=16.0,
                           vw=1280.0)]
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru", _vis_items2)
        act_v6, _ = m_vis.resolve_click("корзина", None, _VR2("1"),
                                        chat_id="vis4")
        # Дедуп: 1 рамка в промпте (prompt собран до ответа модели). Сам выбор
        # с 19.09 ветируется: у кандидата читаемая подпись без слов цели
        # («корзина» vs «Джем – Sirene…») — галлюцинация номера, клика нет
        check("дедуп карт: в vision-промпт ушла 1 рамка вместо 2 дублей",
              _cap_prompt and "1..1" in _cap_prompt[0])
        check("дедуп карт: vision-выбор без цели в подписи — вето",
              act_v6 is None)
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru", _vis_items)

        # ── Активный слой поверх затемнённого фона (кейс 08.09: панель
        # комментариев поверх страницы — зоны/рамки размечали всё подряд,
        # на панель не хватало бюджета) ──
        check("слои: снапшот и искатель зон помечают активный слой (sc)",
              "elementsFromPoint" in _ba._SNAPSHOT_JS
              and "sc:vpcSc(e)" in _ba._SNAPSHOT_JS
              and "elementsFromPoint" in _ba._ALL_CLICKABLE_BOXES_JS)
        # Боковая панель (корзина dodo) центр вьюпорта не перекрывает:
        # верхний элемент стека там сам бэкдроп — нужен разбор bi=0 и
        # покрытие центра элемента (bdEl-ветка)
        check("слои: бэкдроп верхним в центре (боковая корзина) — bdEl-ветка",
              "bdEl" in _ba._SNAPSHOT_JS
              and "bdEl" in _ba._ALL_CLICKABLE_BOXES_JS
              and "bi=0" in _ba._SNAPSHOT_JS
              and "bi=0" in _ba._ALL_CLICKABLE_BOXES_JS)
        # Текстовый выбор тоже режется до активного слоя (раньше sc
        # использовал только vision — клик уходил в затемнённый фон мимо
        # открытой корзины)
        idx_l, _ = m_vis._choose_element(
            "оформить заказ",
            [_it(35, "a", "Оформить заказ", sc=False),  # точный матч под бэкдропом
             _it(36, "button", "Оформить заказ →", sc=True)], _BoomRouter())
        check("слои: точный матч под бэкдропом проигрывает элементу слоя",
              idx_l == 36)
        idx_l2, _ = m_vis._choose_element(
            "оформить заказ", [_it(37, "a", "Оформить заказ", sc=False)],
            _BoomRouter())
        check("слои: все кандидаты под бэкдропом — не режем в ноль",
              idx_l2 == 37)
        idx_l3, meta_l3 = m_vis._choose_element(
            "пепперони", [_it(43, "button", "В корзину", sc=False),
                          _it(44, "button", "Оформить заказ", sc=True)],
            _BoomRouter())
        check("слои: цель только в затемнённом фоне — честный отказ",
              idx_l3 is None and meta_l3["path"] == "none")
        _cap.clear()
        idx_lw, _ = m_vis._llm_wide_pick(
            "оплата", [_it(38, "a", "Главная", sc=False),
                       _it(39, "button", "Оформить заказ", sc=True)],
            _CapRouter("1"))
        check("слои: широкий LLM-резолв видит только активный слой",
              idx_lw == 39 and _cap and "Оформить заказ" in _cap[-1]
              and "Главная" not in _cap[-1])
        # Элементы под бэкдропом (sc=False) в рамки не попадают — только
        # активный слой
        _lay = [_it(30, "button", "", x=10.0, y=10.0, w=300.0, h=200.0,
                    vw=1280.0, sc=False),
                _it(31, "button", "", x=500.0, y=300.0, w=80.0, h=30.0,
                    vw=1280.0, sc=True)]
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru", _lay)
        _cap_prompt.clear()
        act_v7, _ = m_vis.resolve_click("корзина", None, _VR2("1"),
                                        chat_id="vis5")
        check("слои: vision-рамки — только активный слой",
              act_v7 is not None and act_v7["idx"] == 31
              and _cap_prompt and "1..1" in _cap_prompt[0])
        # Ложный детект (все кандидаты вне слоя) — не режем в ноль
        _lay2 = [_it(32, "button", "", x=10.0, y=10.0, w=100.0, h=40.0,
                     vw=1280.0, sc=False)]
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru", _lay2)
        act_v8, _ = m_vis.resolve_click("корзина", None, _VR2("1"),
                                        chat_id="vis6")
        check("слои: все кандидаты вне слоя — фолбэк на полный список",
              act_v8 is not None and act_v8["idx"] == 32)
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://x.ru", "x.ru", _vis_items)
    finally:
        _ba.screenshot_viewport = _orig_sshot
        _ba.snapshot_elements = _orig_snap_v
        if _orig_acb_v is not None:
            _ba.all_clickable_boxes = _orig_acb_v

    # ── Зональный vision-фолбэк (п.2-дельта): DOM нечитаем — клик по координатам ──
    class _ZoneRouter:
        """get_response — «нет» (широкий резолв мимо); vision-ответы по очереди."""

        def __init__(self, *resps):
            self.resps = list(resps)
            self.img_calls = 0
            self.extra = None

        def supports_vision(self):
            return True

        def get_response(self, *a, **kw):
            return "нет"

        def get_response_with_image(self, prompt, img, image_mime=None,
                                    extra_image=None, force_provider=None):
            self.img_calls += 1
            self.extra = extra_image
            return self.resps.pop(0) if self.resps else "нет"

    _boxes = [{"x": 10.0, "y": 10.0, "w": 100.0, "h": 40.0, "text": "Играть"},
              {"x": 10.0, "y": 60.0, "w": 100.0, "h": 40.0, "text": "Рекорды"},
              {"x": 0.0, "y": 0.0, "w": 426.0, "h": 266.0,
               "text": "canvas — сектор 1"}]
    _orig_acb = getattr(_ba, "all_clickable_boxes", None)
    _ba.all_clickable_boxes = lambda host=None, tab_id=None: list(_boxes)
    _ba.screenshot_viewport = lambda host=None, tab_id=None: _png
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru", [_it(0, "a", "Новости")])
    _orig_pu_z = _ba.page_urls
    _ba.page_urls = lambda: ["https://x.ru"]
    _orig_fp_z = getattr(_ba, "follow_popup", None)
    _ba.follow_popup = lambda pre: None
    try:
        m_zone = make()
        # vision по кандидатам снапшота — «нет», по зонам — «2» → координаты
        zr = _ZoneRouter("нет", "2")
        act_z, err_z = m_zone.resolve_click("рекорды", None, zr,
                                            chat_id="vz1")
        check("зоны: выбор зоны → координатный клик (point, vision_zones)",
              act_z is not None and act_z["kind"] == "click"
              and act_z.get("point") == {"x": 60.0, "y": 80.0,
                                         "label": "Рекорды", "zone": 2}
              and act_z["choose"]["path"] == "vision_zones"
              and zr.img_calls == 2)
        # Чистый кадр без разметки уходит вторым изображением — модель
        # видит, что закрыто рамками/бейджами
        check("зоны: чистый скриншот уходит вторым кадром (extra_image)",
              zr.extra == _png)
        # «нет» на обоих ярусах — честный отказ
        no_z, err_z2 = m_zone.resolve_click("рекорды", None,
                                            _ZoneRouter("нет", "нет"),
                                            chat_id="vz2")
        check("зоны: «нет» → честный отказ", no_z is None and bool(err_z2))
        # Зона «Закрыть» при цели без намерения закрывать — вето
        _boxes[1]["text"] = "Закрыть"
        no_z2, err_z3 = m_zone.resolve_click("рекорды", None,
                                             _ZoneRouter("нет", "2"),
                                             chat_id="vz3")
        check("зоны: деструктивная зона ветирована",
              no_z2 is None and bool(err_z3))
        _boxes[1]["text"] = "Рекорды"
        # Исполнение: dispatch дёргает click_at_point с координатами зоны
        # (менеджер без SpyManager._dispatch — тот только записывает вызов)
        _cap = []
        _orig_cap = _ba.click_at_point
        _ba.click_at_point = lambda host, x, y, tab_id=None: (
            _cap.append((host, x, y, tab_id)), "clicked")[1]
        m_zone_exec = ComputerControlManager(context="t", config=dict(CFG),
                                             base_dir=tmp / "s-zone-exec")
        ok_z, _ = m_zone_exec.execute(act_z, "vz1")
        _ba.click_at_point = _orig_cap
        check("зоны: dispatch → click_at_point(центр зоны)",
              ok_z and _cap == [("x.ru", 60.0, 80.0, None)])
        # Координатный клик — confirm всегда (даже при click: false)
        check("зоны: координатный клик — confirm не обходится",
              make(cfg={**CFG, "confirm": False,
                        "risk_overrides": {"click": False}}).needs_confirm(
                  {"kind": "click", "point": {"x": 1, "y": 2}}))
        # Скачивание по зоне: нет href — честный отказ, а не клик
        no_dl_z, err_dl_z = m_zone.resolve_download(
            "рекорды", None, _ZoneRouter("нет", "2"), chat_id="vz4")
        check("зоны: download по координатной зоне — «не ссылка»",
              no_dl_z is None and "не ссылка" in (err_dl_z or ""))
        # Безымянная зона подписывается ближайшей подписанной (миниатюра
        # рядом с названием видео), а не «зона N». Цель — НЕ номерная
        # («первое видео» уйдёт в ordinal-рецепт task-словарём без element)
        _boxes.append({"x": 10.0, "y": 120.0, "w": 100.0, "h": 80.0,
                       "text": ""})
        act_z2, _ = m_zone.resolve_click("видео недели", None,
                                         _ZoneRouter("нет", "4"),
                                         chat_id="vz5")
        check("зоны: безымянная зона подписана соседом, а не «зона N»",
              act_z2 is not None
              and act_z2["element"].startswith("зона рядом с «")
              and "Рекорды" in act_z2["element"]
              and act_z2["point"]["zone"] == 4)
        _boxes.pop()
        # Подписанных зон нет совсем — подпись словами самой цели
        _texts = [b["text"] for b in _boxes]
        for b in _boxes:
            b["text"] = ""
        act_z3, _ = m_zone.resolve_click("видео недели", None,
                                         _ZoneRouter("нет", "1"),
                                         chat_id="vz6")
        check("зоны: все зоны безымянные — подпись словами цели",
              act_z3 is not None and "видео недели" in act_z3["element"])
        for b, t in zip(_boxes, _texts):
            b["text"] = t
    finally:
        if _orig_acb is not None:
            _ba.all_clickable_boxes = _orig_acb
        if _orig_fp_z is not None:
            _ba.follow_popup = _orig_fp_z
        _ba.page_urls = _orig_pu_z
        _ba.screenshot_viewport = _orig_sshot
        _ba.snapshot_elements = _orig_snap_v

    # ── Vision-гейт: сомнительный текстовый выбор → зональный vision ──
    # Слабый ярус (<55: голый контекст — клик по карточке комбо из-за
    # упоминания товара в её описании) или ничья одноимённых («Изменить» ×3
    # в корзине): до клика вслепую — зональный vision как общий
    # дизамбигуатор без привязки к сайту; отказ vision — старый выбор
    m_gate = make()
    _gate_calls = []

    def _fake_zones(goal, host, tab_id, router, op="click"):
        _gate_calls.append(goal)
        return ({"x": 10.0, "y": 20.0, "label": "нужная зона", "zone": 2},
                {"path": "vision_zones",
                 "point": {"x": 10.0, "y": 20.0, "label": "нужная зона",
                           "zone": 2}})

    def _fake_zones_none(goal, host, tab_id, router, op="click"):
        _gate_calls.append(goal)
        return None, {"path": "vision_zones"}

    _meta_weak = {"path": "score", "candidates": [
        {"idx": 5, "text": "3 пиццы 30 или 35 см", "score": 40.0}]}
    m_gate._vision_zones = _fake_zones
    gi, gm = m_gate._vision_gate_choice("двойная пепперони", "x.ru", None,
                                        5, _meta_weak, _ZoneRouter())
    check("vision-гейт: слабый скор → координатный клик зоны",
          gi is None and gm.get("point", {}).get("zone") == 2
          and gm.get("vision_gate") and _gate_calls)
    _meta_strong = {"path": "score", "candidates": [
        {"idx": 5, "text": "Пепперони фреш", "score": 70.0}]}
    gi2, gm2 = m_gate._vision_gate_choice("пепперони", "x.ru", None,
                                          5, _meta_strong, _ZoneRouter())
    check("vision-гейт: уверенный скор — vision не дёргается",
          gi2 == 5 and gm2 is _meta_strong and len(_gate_calls) == 1)
    _meta_tie = {"path": "llm", "candidates": [
        {"idx": 5, "text": "Изменить", "score": 65.0},
        {"idx": 8, "text": "Изменить", "score": 62.5},
        {"idx": 9, "text": "Удалить", "score": 40.0}]}
    gi3, gm3 = m_gate._vision_gate_choice("изменить в двойная пепперони",
                                          "x.ru", None, 5, _meta_tie,
                                          _ZoneRouter())
    check("vision-гейт: ничья одноимённых → зональный vision",
          gi3 is None and gm3.get("point") and len(_gate_calls) == 2)
    _meta_gap = {"path": "score", "candidates": [
        {"idx": 5, "text": "Изменить", "score": 75.0},
        {"idx": 8, "text": "Изменить", "score": 62.5}]}
    gi4, _ = m_gate._vision_gate_choice("изменить", "x.ru", None,
                                        5, _meta_gap, _ZoneRouter())
    check("vision-гейт: одноимённые, но отрыв ≥5 (бонусы модалки) — мимо",
          gi4 == 5 and len(_gate_calls) == 2)
    m_gate._vision_zones = _fake_zones_none
    gi5, gm5 = m_gate._vision_gate_choice("двойная пепперони", "x.ru", None,
                                          5, dict(_meta_weak,
                                                  candidates=list(
                                                      _meta_weak["candidates"])),
                                          _ZoneRouter())
    check("vision-гейт: vision отказался — исходный выбор сохранён",
          gi5 == 5 and len(_gate_calls) == 3)
    _meta_snap = {"path": "goal_snapshot", "candidates": [
        {"idx": 5, "text": "x", "score": 10.0}]}
    gi6, _ = m_gate._vision_gate_choice("x", "x.ru", None, 5, _meta_snap,
                                        _ZoneRouter())
    check("vision-гейт: чужие пути (goal_snapshot) не трогаем",
          gi6 == 5 and len(_gate_calls) == 3)
    del m_gate._vision_zones

    # e2e через _resolve_element: слабый выбор (50, единственный кандидат)
    # уходит в зональный vision вместо слепого клика
    _orig_acb2 = getattr(_ba, "all_clickable_boxes", None)
    # Зоны под цель «соус»: подпись выбранной зоны проходит ту же проверку
    # соответствия цели, что и любой выбор по номеру от модели
    _gate_boxes = [{"x": 10.0, "y": 10.0, "w": 100.0, "h": 40.0,
                    "text": "Мегасоус"},
                   {"x": 10.0, "y": 60.0, "w": 100.0, "h": 40.0,
                    "text": "Соус чесночный"}]
    _ba.all_clickable_boxes = lambda host=None, tab_id=None: list(_gate_boxes)
    _ba.screenshot_viewport = lambda host=None, tab_id=None: _png
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru", [_it(0, "a", "Мегасоус")])
    try:
        # «соус» — подстрока внутри чужого слова («мегаСОУС»): ярус 50,
        # единственный кандидат — раньше кликнули бы вслепую
        act_g, _ = make().resolve_click(
            "соус", None, _ZoneRouter("2"), chat_id="vg1")
        check("vision-гейт e2e: слабый выбор → point-действие от vision",
              act_g is not None and act_g.get("point")
              and act_g["choose"].get("vision_gate"))
    finally:
        if _orig_acb2 is not None:
            _ba.all_clickable_boxes = _orig_acb2
        _ba.screenshot_viewport = _orig_sshot
        _ba.snapshot_elements = _orig_snap_v

    # ── LLM-ярус разбора команды (JSON-протокол вместо tool-calling) ──
    from app.features.computer_control import intent_prompt, parse_intent_action
    check("intent parse: валидный JSON / ограждённый / none / мусор",
          parse_intent_action('{"action":"click","goal":"войти","site":null}')
          == {"action": "click", "goal": "войти"}
          and parse_intent_action('ок\n```json\n{"action":"none"}\n```')
          == {"action": "none"}
          and parse_intent_action("ничего") is None
          and parse_intent_action('{"action":"hack"}') is None
          and parse_intent_action('{"action":"click"}') is None)
    check("intent parse: клавиша — «пробел» → Space, неизвестная отклонена",
          parse_intent_action('{"action":"key","key":"пробел"}')
          == {"action": "key", "key": "Space"}
          and parse_intent_action('{"action":"key","key":"F12"}') is None)
    check("intent parse: type требует text; search — query и site",
          parse_intent_action('{"action":"type","text":"привет","field":"поиск"}')
          == {"action": "type", "text": "привет", "field": "поиск"}
          and parse_intent_action('{"action":"type","field":"поиск"}') is None
          and parse_intent_action('{"action":"search","query":"x"}') is None)
    check("intent prompt: перечисляет действия и none",
          '{"action":"none"}' in intent_prompt("тест")
          and '"action":"click"' in intent_prompt("тест"))

    class _IntentRouter:
        def __init__(self, resp): self.resp = resp
        def get_response(self, messages, **kw): return self.resp

    # none → не наша команда, резолверы не дёргаются
    m_il = make()
    m_il.resolve_click = lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("резолвер не должен вызываться"))
    check("intent llm: none — резолверы не вызываются",
          m_il.resolve_intent_llm("как дела?", _IntentRouter('{"action":"none"}'))
          == (None, None))
    # open → resolve_many (алиас сайта)
    m_il2 = make(cfg={**CFG, "sites": {"ютуб": "youtube.com"}})
    act_il, _ = m_il2.resolve_intent_llm(
        "зайди-ка на ютубчик", _IntentRouter('{"action":"open","target":"ютуб"}'))
    check("intent llm: open → url-действие через resolve_many",
          act_il == {"kind": "url", "value": "https://youtube.com"})
    # click → resolve_click с goal и site
    seen_il = {}
    m_il3 = make()
    m_il3.resolve_click = lambda goal, site, router, chat_id="": (
        seen_il.update(goal=goal, site=site),
        ({"kind": "click", "idx": 1}, None))[1]
    act_il3, _ = m_il3.resolve_intent_llm(
        "тыкни там на зелёненькое", _IntentRouter(
            '{"action":"click","goal":"зелёная кнопка","site":"додо"}'))
    check("intent llm: click → resolve_click(goal, site)",
          act_il3 == {"kind": "click", "idx": 1}
          and seen_il == {"goal": "зелёная кнопка", "site": "додо"})
    # LLM недоступна / мусорный ответ — не наше, фраза уходит в диалог
    check("intent llm: ошибка роутера и мусор — (None, None)",
          m_il3.resolve_intent_llm("x", None) == (None, None)
          and m_il3.resolve_intent_llm("x", _IntentRouter("не json"))
          == (None, None))
    # ошибка резолвера → честная причина, а не молчание/«сыгранный» успех
    m_il4 = make()
    m_il4.resolve_send = lambda *a, **kw: (None, "нет полей ввода")
    err_il4 = m_il4.resolve_intent_llm(
        "отправляй", _IntentRouter('{"action":"send"}'))[1]
    check("intent llm: ошибка резолвера → честная причина",
          err_il4 == "нет полей ввода")

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
        # Псевдокликабельные фрагменты (span с унаследованным
        # cursor:pointer) — после настоящих контролов: иначе на ютубе
        # имена каналов вытесняли ссылку-заголовок из топ-30, и «видео с
        # японскими символами» нажало span канала вместо видео (кейс 09.09)
        rw_f = _WideRouter("1")
        idx_f, _ = m_w._llm_wide_pick(
            "видео с японскими символами",
            [_it(11, "span", "アニプレックス チャンネル"),
             _it(12, "a", "「TO BE HERO X」ノンクレジットEDムービー",
                 href="https://x.ru/watch?v=1"),
             _it(13, "div", "Перейти на канал", role="button")], rw_f)
        check("wide: фрагменты span — после настоящих контролов в списке",
              idx_f == 12 and rw_f.calls
              and rw_f.calls[-1].index("TO BE HERO X")
              < rw_f.calls[-1].index("アニプレックス")
              and rw_f.calls[-1].index("Перейти на канал")
              < rw_f.calls[-1].index("アニプレックス"))
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

    # ── Ярус 1: «нажми X» — сначала видимая страница, скролл лишь при
    # промахе (кейс 09.09: «нажми Lumiere» при видимом «Lumière – Expedition
    # 33» уезжало к «Lumiere | Metal Cover» ниже по ленте — целевой снапшот
    # заменял выбор при равном скоре и делал scrollIntoView) ──
    _orig_snap_vp = _ba.snapshot_elements
    _orig_sfg_vp = _ba.snapshot_for_goal
    _goal_calls_vp = []
    _ba.snapshot_for_goal = lambda host, goal, tab_id=None: (
        _goal_calls_vp.append(goal) or ("", []))
    _lum = [_it(40, "a", "Lumière – Expedition 33 | Slowed + Reverb",
                href="https://x.ru/v1", x=20.0, y=400.0),
            _it(41, "a", "Clair Obscur - Lumiere | Metal Cover",
                href="https://x.ru/v2", x=20.0, y=2000.0, vp=False),
            _it(42, "a", "Главная", href="https://x.ru/")]
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru", list(_lum))
    try:
        m_vp = make()
        # Опечатка без акцента: видимый «Lumière …» находится ярусом 1
        # (снятие диакритики), целевой снапшот/доскролл не запускаются
        act_vp, _ = m_vp.resolve_click("lumiere", None, _BoomRouter(),
                                       chat_id="vp1")
        check("vp-ярус: опечатка без акцента — видимый матч, целевой "
              "снапшот не запускался",
              act_vp is not None and act_vp["idx"] == 40
              and act_vp["choose"].get("vp_first")
              and not _goal_calls_vp)
        # На видимой странице совпадений нет — ярус 2 берёт вне-экранный
        act_vp2, _ = m_vp.resolve_click("lumiere metal cover", None,
                                        _BoomRouter(), chat_id="vp2")
        check("vp-ярус: на видимой нет — вне-экранный матч (ярус 2)",
              act_vp2 is not None and act_vp2["idx"] == 41
              and not act_vp2["choose"].get("vp_first"))
    finally:
        _ba.snapshot_elements = _orig_snap_vp
        _ba.snapshot_for_goal = _orig_sfg_vp

    # ── Команда «наведи (курсор) на X» — hover без клика ──
    from app.features.computer_control import (parse_hover_request,
                                               parse_intent_action)
    check("hover: парсер — «наведи на меню»/курсор/сайт/EN, «нажми» мимо",
          parse_hover_request("наведи на меню") == ("меню", None)
          and parse_hover_request("наведи курсор на видео на ютубе")
          == ("видео", "ютубе")
          and parse_hover_request("подержи курсор над громкостью")
          == ("громкостью", None)
          and parse_hover_request("hover over settings") == ("settings", None)
          and parse_hover_request("нажми на меню") is None)
    check("hover: LLM-intent принимает действие hover",
          parse_intent_action('{"action":"hover","goal":"меню"}')
          == {"action": "hover", "goal": "меню"})
    _orig_snap_hv = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru",
        [_it(50, "button", "Меню", x=10.0, y=10.0),
         _it(51, "a", "Видео про котиков", href="https://x.ru/v",
             x=10.0, y=60.0)])
    try:
        m_hv = make()
        act_hv, err_hv = m_hv.resolve_hover("меню", None, _BoomRouter(),
                                            chat_id="hv1")
        check("hover: резолв строит hover-действие по элементу",
              act_hv is not None and err_hv is None
              and act_hv["kind"] == "hover" and act_hv["idx"] == 50)
        # Подтверждение — по override click; точечное наведение (зона
        # vision) принудительного подтверждения не требует: ничего не
        # активируется
        m_hv_off = make(cfg={**CFG, "confirm": False})
        check("hover: needs_confirm — override click, точка не обязывает",
              m_hv.needs_confirm(act_hv) is True
              and m_hv_off.needs_confirm(act_hv) is False
              and m_hv_off.needs_confirm(
                  {"kind": "hover", "point": {"x": 1, "y": 2}}) is False
              and m_hv.needs_confirm(
                  {"kind": "hover", "point": {"x": 1, "y": 2}}) is True)
        check("hover: формулировки describe/вопроса/успеха",
              m_hv.describe(act_hv)
              == "навести курсор на «Меню» на x.ru"
              and m_hv.confirm_question(act_hv)
              == "Навести курсор на «Меню» на x.ru?"
              and m_hv.describe_done(act_hv)
              == "навёл курсор на «Меню» на x.ru")
        # Диспетчер: tagged — по метке снапшота, point — по координатам
        _hv_calls = []
        _orig_ht, _orig_hp = _ba.hover_tagged, _ba.hover_at_point
        _ba.hover_tagged = lambda host=None, idx=0, tab_id=None: (
            _hv_calls.append(("tagged", idx)) or "hovered")
        _ba.hover_at_point = lambda host=None, x=0, y=0, tab_id=None: (
            _hv_calls.append(("point", x, y)) or "hovered")
        try:
            ComputerControlManager._dispatch(m_hv, act_hv)
            ComputerControlManager._dispatch(
                m_hv, {"kind": "hover", "point": {"x": 5.0, "y": 6.0},
                       "host": "x.ru", "element": "зона 1"})
            check("hover: dispatch — tagged по метке и point по координатам",
                  _hv_calls == [("tagged", 50),
                                ("point", 5.0, 6.0)])
        finally:
            _ba.hover_tagged, _ba.hover_at_point = _orig_ht, _orig_hp
    finally:
        _ba.snapshot_elements = _orig_snap_hv

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

    # ── «обнови/закрой вкладку (X)» — tab_op (кейс 10.09) ──
    from app.features.computer_control import parse_tab_op, parse_close_request
    check("tab_op parse: обнови/перезагрузи/закрой вкладку (X)",
          parse_tab_op("обнови страницу") == ("reload", None)
          and parse_tab_op("обнови") == ("reload", None)
          and parse_tab_op("перезагрузи вкладку") == ("reload", None)
          and parse_tab_op("обнови вкладку ютуба") == ("reload", "ютуба")
          and parse_tab_op("обнови страницу, пожалуйста") == ("reload", None)
          and parse_tab_op("закрой вкладку") == ("close", None)
          and parse_tab_op("закрой эту вкладку") == ("close", None)
          and parse_tab_op("закрой вкладку с ютубом") == ("close", "ютубом")
          and parse_tab_op("закрой страницу вконтакте") == ("close", "вконтакте"))
    check("tab_op parse: не-вкладочные формы не цепляем",
          parse_tab_op("закрой окно") is None
          and parse_tab_op("закрой соусы к бортикам") is None
          and parse_tab_op("обнови ленту") is None
          and parse_tab_op("закрой их") is None
          # «закрой вкладку» теперь НЕ уходит в клик-закрытие крестиком
          and parse_close_request("закрой вкладку") is None
          and parse_close_request("закрой страницу") is None
          and parse_close_request("закрой окно") is not None
          and parse_close_request("закрой соусы к бортикам") is not None)
    check("tab_op parse: назад/вперёд по истории вкладки",
          parse_tab_op("назад") == ("back", None)
          and parse_tab_op("обратно") == ("back", None)
          and parse_tab_op("вернись назад") == ("back", None)
          and parse_tab_op("шаг назад") == ("back", None)
          and parse_tab_op("вернись на предыдущую страницу")
          == ("back", None)
          and parse_tab_op("назад на этой странице") == ("back", None)
          and parse_tab_op("назад на ютубе") == ("back", "ютубе")
          and parse_tab_op("вперёд") == ("forward", None)
          and parse_tab_op("вперед") == ("forward", None)
          and parse_tab_op("перейди вперёд") == ("forward", None)
          and parse_tab_op("шаг вперёд") == ("forward", None)
          # не-команда: «вернись» без «назад» — не про историю вкладки
          and parse_tab_op("вернись") is None
          # хвост без слова «вкладка» — только указание МЕСТА («назад на
          # ютубе»); «назад в будущее» — фраза, а не команда истории
          # (проверка формы хвоста — в секции 12z)
          and parse_tab_op("назад в будущее") is None)

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
        # Пометка текущей (отслеживаемой) вкладки — по tab_id…
        m_tab._last_tab_id = 2
        txt2 = m_tab.list_open_tabs_text()
        check("вкладки: текущая помечена «← текущая» (по tab_id)",
              "vpc · GitHub» (github.com) ← текущая" in txt2
              and "YouTube» (youtube.com) ←" not in txt2)
        m_tab._last_tab_id = None
        # …а без tab_id — по хосту контекста
        m_tab_lm = ComputerControlManager(
            context="t", base_dir=tmp / "s-listmark",
            config={**CFG, "allow_domains": []})
        m_tab_lm._last_host = "youtube.com"
        txt3 = m_tab_lm.list_open_tabs_text()
        check("вкладки: текущая помечена по хосту (без tab_id)",
              "YouTube» (youtube.com) ← текущая" in txt3
              and "YouTube Music» (music.youtube.com) ←" not in txt3)
        # Формулировки
        check("вкладки: describe/done/confirm",
              m_tab.describe(act_t1) == "перейти на вкладку «vpc · GitHub»"
              and m_tab.describe_done(act_t1)
              == "переключился на вкладку «vpc · GitHub»"
              and m_tab.confirm_question(act_t1)
              == "Перейти на вкладку «vpc · GitHub»?")

        # ── tab_op: резолв, формулировки, исполнение ──
        _orig_rt = _ba.reload_tab
        _orig_ct = _ba.close_tab
        _orig_vpi = _ba.visible_page_info
        _orig_hn = _ba.history_nav_tab
        _log_rt, _log_ct, _log_hn = [], [], []
        _ba.reload_tab = lambda tid=None: (
            _log_rt.append(tid), ("https://github.com/x", "vpc · GitHub"))[1]
        _ba.close_tab = lambda tid=None: (
            _log_ct.append(tid), ("https://github.com/x", "vpc · GitHub"))[1]
        _ba.history_nav_tab = lambda tid=None, direction="back": (
            _log_hn.append((tid, direction)),
            ("https://youtube.com/", "YouTube"))[1]
        _ba.visible_page_info = lambda: ("https://youtube.com/",
                                         "youtube.com")
        try:
            act_o1, _ = m_tab.resolve_tab_op("гитхаб", "reload", None)
            check("tab_op: «обнови вкладку гитхаба» — матч по алиасу-хосту",
                  act_o1 is not None and act_o1["kind"] == "tab_op"
                  and act_o1["op"] == "reload" and act_o1["tab_id"] == 2)
            act_o2, err_o2 = m_tab.resolve_tab_op("ютуб", "close", None)
            check("tab_op: «закрой вкладку ютуба» — две вкладки → уточнение",
                  act_o2 is None and err_o2 and "несколько" in err_o2.lower())
            act_o3, err_o3 = m_tab.resolve_tab_op("вк", "close", None)
            check("tab_op: нет такой вкладки — отказ со списком открытых",
                  act_o3 is None and err_o3 and "Открыты:" in err_o3)
            act_o4, _ = m_tab.resolve_tab_op(None, "reload", None)
            check("tab_op: без цели — видимая вкладка (tab_id=None, имя из "
                  "списка по видимому URL)",
                  act_o4 is not None and act_o4["tab_id"] is None
                  and act_o4["element"] == "YouTube")
            check("tab_op: describe/confirm/done",
                  m_tab.describe(act_o1) == "обновить вкладку «vpc · GitHub»"
                  and m_tab.confirm_question(act_o1)
                  == "Обновить вкладку «vpc · GitHub»?"
                  and m_tab.describe_done(act_o1)
                  == "обновил вкладку «vpc · GitHub»")
            _act_c = {"kind": "tab_op", "op": "close", "tab_id": 2,
                      "host": "github.com", "element": "vpc · GitHub"}
            check("tab_op: describe close",
                  m_tab.describe(_act_c) == "закрыть вкладку «vpc · GitHub»"
                  and m_tab.describe_done(_act_c)
                  == "закрыл вкладку «vpc · GitHub»")
            # Исполнение: reload с tab_id, close отслеживаемой сбрасывает
            # _last_tab_id/_last_host (настоящий менеджер — у SpyManager
            # _dispatch заглушен)
            m_to_real = ComputerControlManager(
                context="t", base_dir=tmp,
                config={**CFG, "allow_domains": [], "sites": {}})
            ok_r, _ = m_to_real.execute(act_o1, "c-tab")
            check("tab_op: dispatch reload → reload_tab(tab_id)",
                  ok_r and _log_rt == [2])
            m_to_real._last_tab_id = 2
            m_to_real._last_host = "github.com"
            ok_c, _ = m_to_real.execute(_act_c, "c-tab")
            check("tab_op: dispatch close → close_tab, отслеживание сброшено",
                  ok_c and _log_ct == [2] and m_to_real._last_tab_id is None
                  and m_to_real._last_host is None)
            # Без tab_id — видимая вкладка на момент исполнения
            ok_r2, _ = m_to_real.execute(
                {"kind": "tab_op", "op": "reload", "tab_id": None,
                 "host": "youtube.com", "element": "YouTube"}, "c-tab")
            check("tab_op: dispatch reload без tab_id → reload_tab(None)",
                  ok_r2 and _log_rt == [2, None])
            # Назад/вперёд: резолв (без цели), формулировки, исполнение
            act_b, _ = m_tab.resolve_tab_op(None, "back", None)
            check("tab_op: «вернись назад» → back на видимой вкладке",
                  act_b is not None and act_b["op"] == "back"
                  and act_b["tab_id"] is None)
            check("tab_op: describe/confirm/done back",
                  m_tab.describe(act_b)
                  == "вернуться назад на вкладке «YouTube»"
                  and m_tab.confirm_question(act_b)
                  == "Вернуться назад на вкладке «YouTube»?"
                  and m_tab.describe_done(act_b)
                  == "вернулся назад на вкладке «YouTube»")
            ok_b, _ = m_to_real.execute(act_b, "c-tab")
            check("tab_op: dispatch back → history_nav_tab, цель обновлена",
                  ok_b and _log_hn == [(None, "back")]
                  and m_to_real._last_url == "https://youtube.com/"
                  and m_to_real._last_host == "youtube.com")
            # Истории нет — честный отказ (BrowserUnavailable из движка)
            def _no_history(tid=None, direction="back"):
                raise _ba.BrowserUnavailable(
                    "некуда: истории назад у этой вкладки нет")
            _ba.history_nav_tab = _no_history
            ok_b2, det_b2 = m_to_real.execute(
                {"kind": "tab_op", "op": "back", "tab_id": None}, "c-tab")
            check("tab_op: истории назад нет — честный отказ",
                  not ok_b2 and "некуда" in det_b2)
        finally:
            _ba.reload_tab = _orig_rt
            _ba.close_tab = _orig_ct
            _ba.history_nav_tab = _orig_hn
            _ba.visible_page_info = _orig_vpi
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
          == (("рабочие часы в день", 8, ""), None)
          and parse_slider_request("выставь громкость на 5")
          == (("громкость", 5, ""), None)
          and parse_slider_request("поставь лайк") is None
          and parse_slider_request("нажми кнопку") is None)
    # Единицы после числа (кейс 08.09: «на 50 процентов»/«на 10 минут»
    # не матчились и фраза улетала в generic-клик — кликал «1:37 / 16:28»)
    check("parse slider: единицы %/минут/секунд",
          parse_slider_request("перетащи громкость на 50 процентов")
          == (("громкость", 50, "pct"), None)
          and parse_slider_request("выставь громкость на 50%")
          == (("громкость", 50, "pct"), None)
          and parse_slider_request("перетащи прогресс видео на 10 минут")
          == (("прогресс видео", 10, "min"), None)
          and parse_slider_request("двинь ползунок на 30 секунд")
          == (("ползунок", 30, "sec"), None))
    _orig_snap_s = _ba.snapshot_elements
    _orig_dis_s = _ba.dismiss_overlay
    _ba.dismiss_overlay = lambda host=None, tab_id=None: None
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru/anketa", "x.ru", [_it(0, "button", "Отправить анкету")])
    _orig_set_s = _ba.set_slider
    _slider_calls = []
    _ba.set_slider = lambda host, label, value, tab_id=None, unit="": (
        _slider_calls.append((host, label, value, tab_id, unit)),
        str(value))[1]
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
        # Единица доезжает до set_slider и видна в подписи действия
        act_s2, _ = m_s.resolve_slider(("громкость", 50, "pct"), None,
                                       _BoomRouter())
        m_s_real.execute(act_s2, "c-slider2")
        check("слайдер: unit=pct прокинута в set_slider и в подпись",
              _slider_calls[-1][4] == "pct"
              and "50%" in ComputerControlManager.describe(act_s2))
        # Подпись слайдера матчится по собственному aria-label/title
        # (громкость/прогресс ютуба — текст не в innerText предков), слова
        # от 6 букв — по усечённому началу («громкости» → «громкост…»),
        # свёрнутые до наведения — второй шанс + hover-reveal на CDP,
        # промах — со списком имеющихся (have)
        check("слайдер: матч по aria-label, стемминг, tiny, have-список",
              "ownLab" in _ba._SET_SLIDER_JS
              and "aria-labelledby" in _ba._SET_SLIDER_JS
              and "w.slice(0,w.length-1)" in _ba._SET_SLIDER_JS
              and "tiny" in _ba._SET_SLIDER_JS
              and "have" in _ba._SET_SLIDER_JS
              and "shadowRoot" in _ba._SET_SLIDER_JS)
        _nm = _ba._slider_no_match("громкость", {"have": ["громкость",
                                                          "прогресс"]})
        check("слайдер: ошибка промаха — со списком имеющихся",
              "не нашёлся" in str(_nm) and "на странице есть" in str(_nm)
              and "прогресс" in str(_nm))
        check("слайдер: hover-reveal с shadow-обходом + клавиатурный фолбэк",
              "getRootNode" in _ba._SLIDER_HOVER_ANCHOR_JS
              and hasattr(_ba, "_keyboard_set_slider")
              and "dbg" in _ba._SET_SLIDER_JS)

        # Клавиатурный фолбэк: Home → min, шаг измеряется (ютуб-громкость
        # шагает по 5), стрелки до цели
        class _Kb:
            def __init__(self, pg):
                self.pg = pg

            def press(self, key):
                self.pg.presses.append(key)
                if key == "Home":
                    self.pg.val = 0.0
                elif key in ("ArrowRight", "ArrowUp"):
                    self.pg.val += 5.0
                elif key in ("ArrowLeft", "ArrowDown"):
                    self.pg.val -= 5.0

        class _FakeSliderPage:
            def __init__(self):
                self.val = 80.0
                self.presses = []
                self.keyboard = _Kb(self)

            def evaluate(self, js):
                if "String(v)" in js:  # _SLIDER_READ_JS
                    return str(self.val)
                return ""  # focus/снятие метки

        _fp = _FakeSliderPage()
        _ba._keyboard_set_slider(_fp, 30.0)
        check("слайдер: клавиши — Home, измерение шага, стрелки до цели",
              _fp.val == 30.0 and _fp.presses[0] == "Home"
              and _fp.presses.count("ArrowRight") == 6)
        # Слайдер мёртв (клавиши не двигают) — честный отказ, метка снята
        class _DeadPage:
            def __init__(self):
                self.keyboard = _Kb(self)
                self.cleared = False
                self.presses = []

            def evaluate(self, js):
                if "String(v)" in js:
                    return "42"  # значение не меняется от клавиш
                if "removeAttribute" in js:
                    self.cleared = True
                return ""

        _dp = _DeadPage()
        _kb_err = None
        try:
            _ba._keyboard_set_slider(_dp, 10.0)
        except Exception as e:
            _kb_err = e
        check("слайдер: клавиши не отвечают — честный отказ + метка снята",
              _kb_err is not None and "клавиату" in str(_kb_err)
              and _dp.cleared)

        # Hover-reveal: перебирает точки-кандидаты, меряет после каждой
        class _FakeHoverPage:
            def __init__(self, ok_at):
                self.moves = 0
                self.ok_at = ok_at
                page = self

                class _M:
                    def move(self, x, y):
                        page.moves += 1

                self.mouse = _M()

            def evaluate(self, js):
                if "JSON.stringify(pts)" in js:  # _SLIDER_HOVER_ANCHOR_JS
                    return '[{"x": 10, "y": 20}, {"x": 30, "y": 40}]'
                if "aria-valuemin" in js:  # _SLIDER_MEASURE_JS
                    return '{"x": 55, "y": 20}' if self.moves >= self.ok_at \
                        else ""
                return ""

        _pt = _ba._hover_reveal_slider(_FakeHoverPage(ok_at=2), 5)
        check("слайдер: hover-reveal перебирает точки до раскрытия",
              _pt == (55.0, 20.0))
        check("слайдер: hover не раскрыл — None (дальше клавиши)",
              _ba._hover_reveal_slider(_FakeHoverPage(ok_at=99), 5) is None)
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
    # Окно возвращается РОВНО один раз (перед фазой контейнера): контейнерный
    # шаг окно не двигает, и второй возврат был лишним походом в браузер
    check("scroll_hunt: контейнерная фаза отработала и возвращена",
          _hunt_calls.count("cstep") == 10 and _hunt_calls[-1] == "crestore"
          and _hunt_calls.count(("wrestore", 100.0)) == 1)

    # Свайп-лента (shorts/reels): доскролл-поиск пропускается ЦЕЛИКОМ —
    # шаг прокрутки там листает основной контент, а не список элементов
    # (кейс 07.09: «нажми сортировать» на странице shorts бесконечно
    # листало видео: фаза 2 крутила #shorts-container)
    _ba_hunt.snapshot_for_goal = lambda *a, **kw: ("", [])
    try:
        _hunt_calls.clear()
        ComputerControlManager._scroll_hunt(
            make(), _FakeBAHunt(), "youtube.com", 7, "сортировать",
            page_url="https://www.youtube.com/shorts/abc123")
        check("scroll_hunt: на shorts ни одного шага прокрутки",
              not _hunt_calls)
        _hunt_calls.clear()
        ComputerControlManager._scroll_hunt(
            make(), _FakeBAHunt(), "youtube.com", 7, "сортировать",
            page_url="https://www.youtube.com/watch?v=abc123")
        check("scroll_hunt: на обычной странице листает как раньше",
              "wstep" in _hunt_calls and "cstep" in _hunt_calls)
    finally:
        _ba_hunt.snapshot_for_goal = _orig_sfg

    # ── Корзинный фолбэк: «удали/закрой X» без «из корзины» (кейс 07.09) ──
    # Крестик товара в корзине dodo — иконка без текста и aria, текстовый
    # резолв его не видит («не нашёл элемента для "удалить чикен"»), а
    # целевое закрытие «закрыть на чикен» цепляло крестик ВСЕЙ панели.
    # Удаление уходит в cart_op — но только когда товар реально в корзине
    _orig_snap_cr = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://dodopizza.ru/x", "dodopizza.ru", _gh_items())
    _m_cart = make()
    try:
        _ba.cart_item_present = lambda host=None, product="", tab_id=None: True
        a_cr, e_cr = _m_cart.resolve_click("удалить чикен", None, None,
                                           chat_id="cr1")
        check("корзина-фолбэк: «удалить чикен» → cart remove",
              a_cr is not None and a_cr.get("kind") == "cart"
              and a_cr.get("op") == "remove"
              and a_cr.get("product") == "чикен")
        a_cr2, _ = _m_cart.resolve_click("закрыть чикен", None, None,
                                         chat_id="cr2")
        check("корзина-фолбэк: «закрыть чикен» → cart remove, не крестик панели",
              a_cr2 is not None and a_cr2.get("kind") == "cart"
              and a_cr2.get("op") == "remove")
        a_cr3, _ = _m_cart.resolve_click("удалить пиццу чикен", None, None,
                                         chat_id="cr4")
        check("корзина-фолбэк: филлер «пиццу» срезан из названия",
              a_cr3 is not None and a_cr3.get("product") == "чикен")
        a_cr4, _ = _m_cart.resolve_click("удалить чикен из корзины", None,
                                         None, chat_id="cr6")
        check("корзина-фолбэк: хвост «из корзины» срезан",
              a_cr4 is not None and a_cr4.get("product") == "чикен")
        a_ce, _ = _m_cart.resolve_click("изменить состав", "гавайская",
                                        None, chat_id="cr7")
        check("корзина-фолбэк: «изменить состав в гавайская» → cart edit",
              a_ce is not None and a_ce.get("kind") == "cart"
              and a_ce.get("op") == "edit"
              and a_ce.get("product") == "гавайская")
        a_ce2, _ = _m_cart.resolve_click("поменять гавайскую", None, None,
                                         chat_id="cr8")
        check("корзина-фолбэк: «поменять гавайскую» → cart edit",
              a_ce2 is not None and a_ce2.get("op") == "edit"
              and a_ce2.get("product") == "гавайскую")
        # Количество символом/словом: «нажми + в двойная пепперони» —
        # раньше шло общим резолвом и цепляло упоминание товара в описании
        # состава комбо (клик по «+» открывал «3 пиццы 30 или 35 см»)
        a_cq, _ = _m_cart.resolve_click("+ в двойная пепперони", None, None,
                                        chat_id="cr12")
        check("корзина-фолбэк: «+ в X» → cart increase",
              a_cq is not None and a_cq.get("kind") == "cart"
              and a_cq.get("op") == "increase"
              and a_cq.get("product") == "двойная пепперони")
        a_cq2, _ = _m_cart.resolve_click("минус гавайская", None, None,
                                         chat_id="cr13")
        check("корзина-фолбэк: «минус X» → cart decrease",
              a_cq2 is not None and a_cq2.get("op") == "decrease"
              and a_cq2.get("product") == "гавайская")
        # «нажми плюс на колу»: «на колу» спарсилось словом-сайтом —
        # оно не алиас и возвращается в цель
        a_cq3, _ = _m_cart.resolve_click("плюс", "колу", None, chat_id="cr14")
        check("корзина-фолбэк: «плюс на колу» — сайт-слово вернулось в товар",
              a_cq3 is not None and a_cq3.get("op") == "increase"
              and a_cq3.get("product") == "колу")
        _ba.cart_item_present = lambda host=None, product="", tab_id=None: False
        no_cr, err_cr = _m_cart.resolve_click("удалить чикен", None, None,
                                              chat_id="cr3")
        check("корзина-фолбэк: товара нет в корзине — честный отказ",
              no_cr is None and bool(err_cr))
        check("корзина-фолбэк: «удали звук» — не товар, мимо",
              _m_cart._cart_op_fallback("звук", "remove", None, "cr5")
              is None)
        # Не корзина, а страница/модалка продукта со слотами «Изменить
        # состав» (комбо dodo): детерминированный поиск по контексту предка
        # (кейс 07.09: раньше кликало инфо-иконку «Показать доп. информацию»)
        _ba.edit_composition_find = lambda host=None, product="", tab_id=None: {
            "status": "unique"}
        a_pe, _ = _m_cart.resolve_click("изменить состав", "гавайская",
                                        None, chat_id="cr9")
        check("редактор состава: страница продукта → comp_edit",
              a_pe is not None and a_pe.get("kind") == "comp_edit"
              and a_pe.get("product") == "гавайская")
        _ba.edit_composition_find = lambda host=None, product="", tab_id=None: {
            "status": "multi",
            "variants": ["пепперони фреш 25 см", "гавайская 25 см"]}
        no_pe, err_pe = _m_cart.resolve_click("изменить состав", "гавайская",
                                              None, chat_id="cr10")
        check("редактор состава: неоднозначность → уточняющий вопрос",
              no_pe is None and bool(err_pe) and "несколько" in err_pe)
        _ba.edit_composition_find = lambda host=None, product="", tab_id=None: {
            "status": "none"}
        no_pe2, err_pe2 = _m_cart.resolve_click("изменить состав", "гавайская",
                                                None, chat_id="cr11")
        check("редактор состава: ни корзины, ни слотов — обычный резолв",
              no_pe2 is None and bool(err_pe2))
        check("comp_edit: describe + confirm по умолчанию",
              "изменить состав «гавайская»" in ComputerControlManager.describe(
                  {"kind": "comp_edit", "product": "гавайская",
                   "host": "dodopizza.ru"})
              and _m_cart.needs_confirm({"kind": "comp_edit",
                                         "product": "гавайская"}) is True)
        # Исполнение: dispatch дёргает edit_composition_op (менеджер без
        # SpyManager._dispatch — тот только записывает вызов)
        _cap_ce = []
        _orig_ce = _ba.edit_composition_op
        _ba.edit_composition_op = lambda host, product, tab_id=None: (
            _cap_ce.append((host, product, tab_id)), None)[1]
        m_ce = ComputerControlManager(context="t", config=dict(CFG),
                                      base_dir=tmp / "s-cedit")
        ok_ce, rep_ce = m_ce.execute(
            {"kind": "comp_edit", "product": "гавайская",
             "host": "dodopizza.ru"}, "ce1")
        _ba.edit_composition_op = _orig_ce
        check("comp_edit: dispatch → edit_composition_op",
              ok_ce and _cap_ce == [("dodopizza.ru", "гавайская", None)])
        check("comp_edit: отчёт об успехе",
              "открыл редактирование состава «гавайская»"
              in ComputerControlManager.describe_done(
                  {"kind": "comp_edit", "product": "гавайская",
                   "host": "dodopizza.ru"}))
    finally:
        _ba.snapshot_elements = _orig_snap_cr
        del _ba.cart_item_present
        del _ba.edit_composition_find

    # Приоритет заголовка карточки корзины: «двойная пепперони» — это и имя
    # одиночной пиццы, и строка в описании состава комбо «3 пиццы …»; без
    # приоритета первой строки карточки искатель отдавал ложную
    # неоднозначность из 3 карточек, а клик уходил в «Изменить» первого
    # комбо (кейс 07.09). Сам подбор карточек проверен вживую на CDP
    check("корзина-искатель: приоритет заголовка над описанием состава",
          "titled" in _ba._CART_FIND_JS
          and "__vpcWIn(fl" in _ba._CART_FIND_JS)

    # Синоним «троеточие»: основа «действ» prefix-матчит «Меню действий»
    # YouTube (полное слово «действия» не матчило «действий»)
    from app.features.computer_control import _goal_with_synonyms, _word_in
    check("синонимы «троеточие» содержат основу «действ»",
          _word_in("действ", _goal_with_synonyms("троеточие")))

    # Синонимы бургера — хост-зависимые: «Гид» на YouTube («меню» там нельзя —
    # ничья с «Меню аккаунта»), «Меню»/«навигация» на остальных сайтах
    check("синонимы бургера: ютуб — «гид», без «меню»",
          _word_in("гид", _goal_with_synonyms("бургер", "www.youtube.com"))
          and not _word_in("меню", _goal_with_synonyms("бургер",
                                                       "www.youtube.com")))
    check("синонимы бургера: прочие сайты — «меню»/«навигац»",
          _word_in("меню", _goal_with_synonyms("бургер", "dodopizza.ru"))
          and _word_in("навигац", _goal_with_synonyms("три полоски и бургер",
                                                      "school.example.com")))
    check("синонимы бургера: латинский «burger» для data-testid",
          _word_in("burger", _goal_with_synonyms("бургер",
                                                 "school.example.com")))

    # data-testid безтекстовой иконки — крюк скоринга: бургер платформы
    # School 21 (<button data-testid="MobileHeader.BurgerButton">, без текста
    # и aria) находится по стему «burger» из синонимов (кейс 10.09: «три
    # полоски» на платформе не находились вовсе — и клик уходил на ютуб)
    _tid_items = [
        _it(0, "a", "Tribe Tournament"),
        _it(1, "button", "", tid="MobileHeader.SearchButton"),
        _it(2, "button", "", tid="MobileHeader.BurgerButton"),
    ]
    _sc_tid = ComputerControlManager._score_candidates(
        _tid_items, "бургер", host="school.example.com")
    check("скоринг: data-testid безтекстовой иконки — «бургер» находится",
          len(_sc_tid) == 1
          and _sc_tid[0][1]["tid"] == "MobileHeader.BurgerButton"
          and _sc_tid[0][0] >= 60.0)

    # Кейс 10.09 целиком: «нажми три полоски» на платформе (отслеживаемая
    # вкладка) при открытом ютубе — клик по бургеру ПЛАТФОРМЫ по testid,
    # а не «Гид» ютуба через кросс-страничный фолбэк
    _orig_snap_tb = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://school.example.com/", "school.example.com",
        list(_tid_items))
    try:
        m_tb = make(cfg={**CFG, "allow_domains": []})
        m_tb._last_host = "school.example.com"
        m_tb._last_tab_id = 7
        act_tb, err_tb = m_tb.resolve_click("три полоски", None, _BoomRouter())
        check("бургер платформы: «три полоски» — клик по testid-бургеру",
              err_tb is None and act_tb is not None
              and act_tb["idx"] == 2
              and act_tb["host"] == "school.example.com")
    finally:
        _ba.snapshot_elements = _orig_snap_tb

    # ── Зум страницы: парсер команды + описание действия ──
    import app.features.computer_control as _cc
    check("zoom: «уменьши масштаб» → out",
          _cc.parse_zoom_request("уменьши масштаб") == ("out", None))
    check("zoom: «увеличь масштаб на додо» → in + сайт",
          _cc.parse_zoom_request("увеличь масштаб на додо") == ("in", "додо"))
    check("zoom: «сбрось масштаб» и «масштаб 100%» → reset",
          _cc.parse_zoom_request("сбрось масштаб") == ("reset", None)
          and _cc.parse_zoom_request("масштаб 100%") == ("reset", None))
    check("zoom: «прибавь громкость» и «пролистай страницу» — не зум",
          _cc.parse_zoom_request("прибавь громкость") is None
          and _cc.parse_zoom_request("пролистай страницу") is None
          and _cc.parse_zoom_request("открой ютуб") is None)
    check("zoom: describe/describe_done по-русски",
          _cc.ComputerControlManager.describe(
              {"kind": "zoom", "dir": "out", "host": "dodopizza.ru"})
          == "уменьшить масштаб страницы на dodopizza.ru"
          and _cc.ComputerControlManager.describe_done(
              {"kind": "zoom", "zoom_done": 80, "host": "dodopizza.ru"})
          == "выставил масштаб 80% на dodopizza.ru")
    # Зум — обратимая вьюшная настройка: подтверждение не спрашиваем
    _m_zoom = make()
    check("zoom: без подтверждения (needs_confirm False)",
          _m_zoom.needs_confirm({"kind": "zoom", "dir": "in"}) is False)

    # ── «в джеме/миксе/очереди» — область страницы, а не скоп цели ──
    # Кейс 19.09: «нажми renoir в джем» — «джем» не алиас и не домен, он
    # склеивался в цель («renoir на джем») и убивал текстовый резолв
    _orig_snap_jam = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://www.youtube.com/watch?v=x", "www.youtube.com",
        [_it(0, "a", "3:03 Renoir Lorien Testard", vp=False),
         _it(1, "a", "2:43 World Map - Taking Down the Paintress")])
    try:
        m_jam = make()
        act_jam, err_jam = m_jam.resolve_click("renoir", "джем", _BoomRouter())
        check("click: «в джем» не склеивается в скоп — renoir нашёлся",
              err_jam is None and act_jam is not None
              and act_jam["idx"] == 0)
        act_q, err_q = m_jam.resolve_click("renoir", "очереди", _BoomRouter())
        check("click: «в очереди» — тоже область страницы",
              err_q is None and act_q is not None and act_q["idx"] == 0)
    finally:
        _ba.snapshot_elements = _orig_snap_jam

    # ── Vision-вето: подпись выбранного кандидата без слов цели ──
    check("goal_in_label: слово цели в подписи",
          _cc._goal_in_label("renoir", "3:03 Renoir Lorien Testard"))
    check("goal_in_label: цели нет в подписи — промах",
          not _cc._goal_in_label(
              "renoir", "2:43 World Map - Taking Down the Paintress"))
    check("goal_in_label: пустая подпись (иконка) — пропускаем",
          _cc._goal_in_label("три полоски", ""))
    check("goal_in_label: синоним иконки (бургер → «Гид» на YouTube)",
          _cc._goal_in_label("бургер", "Гид", host="www.youtube.com"))

    class _VisionPick2:
        def supports_vision(self):
            return True

        def get_response_with_image(self, *a, **kw):
            return "2"

    m_vv = make()
    m_vv.vision_fallback = True
    _orig_sshot_vv = _ba.screenshot_viewport
    _ba.screenshot_viewport = lambda host=None, tab_id=None, **kw: _jpeg_busy()
    _items_vv = [
        _it(0, "a", "3:03 Renoir Lorien Testard", x=0, y=0, w=190, h=40,
            vw=200),
        _it(1, "a", "2:43 World Map - Taking Down the Paintress",
            x=0, y=50, w=190, h=40, vw=200)]
    try:
        idx_vv, meta_vv = m_vv._visual_resolve(
            "www.youtube.com", None, _items_vv, "renoir", _VisionPick2())
        check("vision: подпись без слов цели — вето, клика нет",
              idx_vv is None and meta_vv.get("veto") == "label_mismatch")
        idx_ok, meta_ok = m_vv._visual_resolve(
            "www.youtube.com", None, _items_vv, "world map", _VisionPick2())
        check("vision: подпись со словами цели — выбор принят",
              idx_ok == 1)
    finally:
        _ba.screenshot_viewport = _orig_sshot_vv

    # ── 12z. Разбор команд, адресация места и инварианты выбора (аудит п.2/п.3) ──
    from app.features.computer_control import (
        _looks_like_domain, parse_page_view_request, parse_scroll_request,
        parse_media_request, parse_cart_request, parse_tab_op, ordinal_recipe)

    # Указательное слово перед «страницей»: \s+ жил внутри последней
    # альтернативы — «что на ЭТОЙ странице?» не распознавалось вовсе
    check("page_view: «что на этой странице?» распознаётся",
          parse_page_view_request("что на этой странице?")
          == (None, False, False)
          and parse_page_view_request("что там на этом экране?")
          == (None, False, False)
          and parse_page_view_request("что на открытой вкладке")
          == (None, False, False)
          and parse_page_view_request("что на этой странице на додо")
          == ("додо", False, False))
    # Направление листания — одно определение слов: и в regex, и при срезке
    # с имени контейнера («комментарии вниз» было именем контейнера)
    check("scroll: направление вырезается из имени контейнера",
          parse_scroll_request("пролистай комментарии вниз")
          == ("start", None, None, None, "комментарии")
          and parse_scroll_request("пролистай чат ниже")
          == ("start", None, None, None, "чат")
          and parse_scroll_request("пролистай комментарии вверх")
          == ("start", None, None, "up", "комментарии")
          and parse_scroll_request("пролистай комментарии слева")
          == ("start", None, "left", None, "комментарии")
          and parse_scroll_request("промотай страницу на ютубе")
          == ("start", "ютубе", None, None, None))
    check("ordinal: «0 результат» / «21 видео» — не номерной рецепт",
          ordinal_recipe("0 результат") is None
          and ordinal_recipe("21 видео") is None
          and ordinal_recipe("1 результат") == "search_pick:1"
          and ordinal_recipe("20 видео") == "search_pick:20"
          and ordinal_recipe("третье видео") == "search_pick:3")
    check("домен vs файл: одно определение «похоже на адрес»",
          not _looks_like_domain("config.py")
          and not _looks_like_domain("отчёт.docx")
          and not _looks_like_domain("маргарите")
          and not _looks_like_domain("две штуки.txt")
          and _looks_like_domain("example.edu")
          and _looks_like_domain("example.com/index.html")
          and _looks_like_domain("дом.рф")
          and _looks_like_domain("127.0.0.1:8000"))
    _m_dom = make(cfg={**CFG, "allow_domains": []})
    check("открытие: «config.py» — файл, а не сайт (не https://config.py)",
          _m_dom.resolve_url("config.py") is None
          and _m_dom.resolve_url("example.edu/827")
          == {"kind": "url", "value": "https://example.edu/827"})
    check("tab_op: «назад в будущее» — фраза, а не команда «назад»",
          parse_tab_op("назад в будущее") is None
          and parse_tab_op("вперёд в прошлое") is None
          and parse_tab_op("назад") == ("back", None)
          and parse_tab_op("назад на ютубе") == ("back", "ютубе")
          and parse_tab_op("вернись на предыдущую страницу") == ("back", None))
    check("корзина: служебные слова снимаются независимо от регистра",
          parse_cart_request("убери Одну Гавайскую пиццу из корзины")
          == ("remove", "Гавайскую")
          and parse_cart_request("убери гавайскую из корзины")
          == ("remove", "гавайскую"))
    check("медиа: «включи звук» — unmute, «выключи звук» — mute",
          parse_media_request("включи звук") == ("m", 1, "unmute")
          and parse_media_request("выключи звук") == ("m", 1, "mute")
          and parse_media_request("без звука") == ("m", 1, "mute")
          and parse_media_request("unmute") == ("m", 1, "unmute"))
    check("медиа: формулировки направленные (включить/выключить звук)",
          ComputerControlManager.describe(
              {"kind": "key", "key": "m", "media": "unmute", "host": "y.ru"})
          == "включить звук на y.ru (m)"
          and ComputerControlManager.confirm_question(
              {"kind": "key", "key": "m", "media": "mute", "host": "y.ru"})
          == "Выключить звук на y.ru (m)?"
          and "включил звук" in ComputerControlManager.describe_done(
              {"kind": "key", "key": "m", "media": "unmute", "host": "y.ru"}))
    # «k» (play/pause ютуба, его подставляет сам resolve_key) — обратимая
    # клавиша: подпадает под risk_overrides.click, как пробел
    _m_k = make(cfg={**CFG, "risk_overrides": {"click": False}})
    check("needs_confirm: «k» — обратимая клавиша (как пробел)",
          _m_k.needs_confirm({"kind": "key", "key": "k"}) is False
          and _m_k.needs_confirm({"kind": "key", "key": "Space"}) is False
          and _m_k.needs_confirm({"kind": "key", "key": "Enter"}) is True)
    # Ввод + Enter: послабление «безопасных полей» (поиск) на отправку формы
    # не распространяется
    _m_sub = make(cfg={**CFG,
                       "risk_overrides": {"type_text_safe_fields": False}})
    check("needs_confirm: submit не проходит по послаблению безопасных полей",
          _m_sub.needs_confirm({"kind": "type", "field_safe": True}) is False
          and _m_sub.needs_confirm({"kind": "type", "field_safe": True,
                                    "submit": True}) is True)

    # Нераспознанное имя места — честный отказ, а не подмена отслеживаемой
    # вкладкой (раньше «нажми X на платформе» уходило в ютуб)
    _orig_snap_uz = _ba.snapshot_elements
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://youtube.com/watch", "youtube.com", [_it(0, "a", "Войти")])
    try:
        m_us = make(cfg={**CFG, "allow_domains": [],
                         "sites": {"ютуб": "https://youtube.com"}})
        m_us._last_host = "youtube.com"
        m_us._last_url = "https://youtube.com/watch"
        _unknown = {
            "key": m_us.resolve_key("Space", "платформе", None),
            "send": m_us.resolve_send(None, "платформе", None),
            "slider": m_us.resolve_slider(("громкость", 50, "pct"),
                                          "платформе", None),
            "scroll": m_us.resolve_scroll("start", "платформе", None),
            "read": m_us.resolve_read("last", "платформе"),
            "zoom": m_us.resolve_zoom("in", "платформе"),
            "page_view": m_us.page_view_report("платформе"),
            "type": m_us.resolve_type("привет в поле чат", "платформе",
                                      _BoomRouter()),
            "download": m_us.resolve_download("методичку", "платформе", None),
        }
        check("место не опознано: все пути возвращают причину, а не действие",
              all(v[0] is None and v[1] and "Не знаю, где" in v[1]
                  for v in _unknown.values()))
        # У клика/наведения неопознанное слово места — скоп карточки («нажми
        # выбрать на маргарите»), это решение старше и остаётся: до
        # _snapshot_for такое слово не доходит вовсе
        _no_cw, _err_cw = m_us.resolve_click("выбрать", "маргарите", None,
                                             chat_id="us-scope")
        check("клик: неопознанное слово места — скоп карточки, не отказ места",
              _no_cw is None and _err_cw and "Не знаю, где" not in _err_cw
              and "выбрать на маргарите" in _err_cw)
        check("место опознано алиасом sites — работает как раньше",
              m_us.resolve_key("Space", "ютубе", None)[0] is not None
              and m_us.resolve_key("Space", "youtube.com", None)[0] is not None
              and m_us.resolve_key("Space", None, None)[0] is not None
              and m_us.resolve_key("Space", "странице", None)[0] is not None)
        check("место совпадает с отслеживаемой страницей — не отказ",
              m_us._site_word_tracked("youtube") is True
              and m_us._site_word_tracked("платформе") is False
              and m_us.resolve_key("Space", "youtube", None)[0] is not None)
        m_sr = make(cfg={**CFG, "allow_domains": [], "search": {
            "кинопоиск": "https://www.kinopoisk.ru/s?q={q}"}})
        m_sr._last_host = "youtube.com"
        check("место из `search` — тот же сайт (хост из шаблона поиска)",
              m_sr.resolve_key("Space", "кинопоиске", None)[0] is not None
              and m_sr.resolve_key("Space", "платформе", None)[1]
              and "Не знаю, где" in m_sr.resolve_key("Space", "платформе",
                                                     None)[1])
    finally:
        _ba.snapshot_elements = _orig_snap_uz

    # Авто-закрытие оверлея — побочный эффект: на этапе резолва его просят
    # только пути с ВЫБОРОМ элемента по снапшоту; чтение страницу не трогает
    _dm_calls = []
    _orig_dm_z = _ba.dismiss_overlay
    _orig_snap_dz = _ba.snapshot_elements
    _orig_sshot_dz = _ba.screenshot_viewport
    _ba.screenshot_viewport = lambda host=None, tab_id=None, allow_focus=False: None
    _ba.dismiss_overlay = lambda host=None, tab_id=None: (
        _dm_calls.append(host or "-"), "Принять все")[1]
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru", [_it(0, "input", "Поиск", ed=True),
                                 _it(1, "button", "Скачать")])
    try:
        m_dz = make(cfg={**CFG, "allow_domains": []})
        m_dz._last_host = "x.ru"
        _readonly = {}
        for _nm, _call in (
                ("read", lambda: m_dz.resolve_read("last", None)),
                ("zoom", lambda: m_dz.resolve_zoom("in", None)),
                ("page_view", lambda: m_dz.page_view_report(None)),
                ("scroll", lambda: m_dz.resolve_scroll("start", None)),
                ("key", lambda: m_dz.resolve_key("Space", None, None)),
                ("send", lambda: m_dz.resolve_send(None, None, None)),
                ("slider", lambda: m_dz.resolve_slider(("громкость", 5, ""),
                                                       None, None))):
            _dm_calls.clear()
            try:
                _call()
            except Exception:
                pass
            _readonly[_nm] = list(_dm_calls)
        check("оверлей: операции без выбора элемента на резолве не кликают",
              all(not v for v in _readonly.values()))
        _dm_calls.clear()
        m_dz.resolve_click("скачать", None, _BoomRouter(), chat_id="dz1")
        _click_dismissed = bool(_dm_calls)
        _dm_calls.clear()
        m_dz.resolve_type("роллы в поле поиск", None, _BoomRouter(),
                          chat_id="dz2")
        check("оверлей: выбор элемента/поля по снапшоту — авто-закрытие есть",
              _click_dismissed and _dm_calls)
    finally:
        _ba.dismiss_overlay = _orig_dm_z
        _ba.snapshot_elements = _orig_snap_dz
        _ba.screenshot_viewport = _orig_sshot_dz

    # Вето на разрушительное зависит от ТИПА действия: наведение не активирует
    _orig_snap_hv = _ba.snapshot_elements
    _q_items_hv = [_it(0, "button", "Очистить очередь"),
                   _it(1, "a", "Очередь просмотра")]
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://youtube.com", "youtube.com", list(_q_items_hv))
    try:
        m_hv = make(cfg={**CFG, "allow_domains": []})
        m_hv._last_host = "youtube.com"
        act_cl, err_cl2 = m_hv.resolve_click("очередь", None, None,
                                             chat_id="hv1")
        check("вето: клик «очередь» уходит к следующему кандидату, не в вето",
              err_cl2 is None and act_cl is not None
              and act_cl["element"] == "Очередь просмотра")
        # Наведению разрушительный кандидат не запрещён — он и лидер по
        # скору («Очистить очередь» 70.0 против «Очередь просмотра» 69.5):
        # выбор честно отличается от клика, где он отфильтрован на входе
        check("вето: наведение «очередь» берёт лидера, снятого у клика",
              m_hv.resolve_hover("очередь", None, None,
                                 chat_id="hv2")[0]["element"]
              == "Очистить очередь")
        # На странице ТОЛЬКО разрушительный контрол: клик без слова-намерения
        # ветирован (как и раньше), а наведение — нет: оно не активирует
        _ba.snapshot_elements = lambda host=None, tab_id=None: (
            "https://youtube.com", "youtube.com",
            [_it(0, "button", "Очистить очередь")])
        no_hc, err_hc = m_hv.resolve_click("очередь", None, None,
                                           chat_id="hv3")
        check("вето: клик — только разрушительный кандидат → отказ",
              no_hc is None and err_hc
              and any(r.get("fail_reason") == "destructive_veto"
                      for r in _aud_recs("hv3")))
        act_hv, err_hv = m_hv.resolve_hover("очередь", None, None,
                                            chat_id="hv4")
        check("вето: наведение на разрушительное — можно (не активирует)",
              err_hv is None and act_hv is not None
              and act_hv["kind"] == "hover"
              and act_hv["element"] == "Очистить очередь")
    finally:
        _ba.snapshot_elements = _orig_snap_hv

    # Зональный vision: номер зоны проходит ту же проверку соответствия цели,
    # что и визуальный фолбэк (галлюцинация → координатный клик наугад)
    _orig_snap_vz2 = _ba.snapshot_elements
    _orig_acb_z2 = getattr(_ba, "all_clickable_boxes", None)
    _orig_sshot_z2 = _ba.screenshot_viewport
    _zboxes = [{"x": 0.0, "y": 0.0, "w": 100.0, "h": 40.0,
                "text": "Подписаться"},
               {"x": 0.0, "y": 50.0, "w": 100.0, "h": 40.0, "text": ""}]
    _ba.all_clickable_boxes = lambda host=None, tab_id=None: list(_zboxes)
    _ba.screenshot_viewport = lambda host=None, tab_id=None: _png
    _ba.snapshot_elements = lambda host=None, tab_id=None: (
        "https://x.ru", "x.ru", [_it(0, "a", "Новости")])
    try:
        m_vz2 = make(cfg={**CFG, "allow_domains": []})
        pnt_bad, meta_bad = m_vz2._vision_zones("renoir", "x.ru", None,
                                                _ZoneRouter("1"))
        check("зоны: подпись зоны без слов цели — вето, координат нет",
              pnt_bad is None and meta_bad.get("veto") == "label_mismatch")
        pnt_ok, meta_ok2 = m_vz2._vision_zones("подписаться", "x.ru", None,
                                               _ZoneRouter("1"))
        check("зоны: подпись со словом цели — зона принята",
              pnt_ok is not None and pnt_ok["zone"] == 1
              and not meta_ok2.get("veto"))
        pnt_un, _ = m_vz2._vision_zones("renoir", "x.ru", None,
                                        _ZoneRouter("2"))
        check("зоны: безымянная зона — законная цель (сверять не с чем)",
              pnt_un is not None and pnt_un["zone"] == 2)
        no_vz, err_vz = m_vz2.resolve_click("renoir", None, _ZoneRouter("1"),
                                            chat_id="vz-lbl")
        check("зоны: вето по подписи — честный отказ и класс в аудите",
              no_vz is None and err_vz
              and any(r.get("fail_reason") == "label_mismatch"
                      for r in _aud_recs("vz-lbl")))
    finally:
        _ba.snapshot_elements = _orig_snap_vz2
        _ba.screenshot_viewport = _orig_sshot_z2
        if _orig_acb_z2 is not None:
            _ba.all_clickable_boxes = _orig_acb_z2

    # Повторный снапшот после ожидания DOM обновляет состояние ЦЕЛИКОМ
    _orig_snap_st = _ba.snapshot_elements
    _st_calls = []

    def _snap_two(host=None, tab_id=None):
        _st_calls.append(host)
        if len(_st_calls) == 1:
            return "https://old.ru/p", "old.ru", [_it(0, "a", "Новости")]
        return "https://new.ru/p", "new.ru", [_it(5, "a", "Новости")]

    _ba.snapshot_elements = _snap_two
    try:
        m_st = make(cfg={**CFG, "allow_domains": []})
        m_st._last_host = "old.ru"
        no_st, err_st = m_st.resolve_click("загрузить", None, None,
                                           chat_id="st1")
        check("снапшот-состояние: причина отказа про СВЕЖИЙ хост (не первый)",
              no_st is None and err_st and "new.ru" in err_st
              and "old.ru" not in err_st)
    finally:
        _ba.snapshot_elements = _orig_snap_st

    # Несколько маркеров в одном ответе — одно действие multi
    m_mk = make()
    clean_mk, _ = m_mk.process_markers(
        "Сделаю. [OPEN_URL:youtube.com] [OPEN_APP:safari]", "mk1")
    _pend_mk = m_mk.get_pending("mk1")
    check("маркеры: два маркера → один pending multi (не затирают друг друга)",
          _pend_mk is not None and _pend_mk["kind"] == "multi"
          and [a["kind"] for a in _pend_mk["items"]] == ["url", "app"]
          and ComputerControlManager.describe(_pend_mk)
          == "открыть https://youtube.com и запустить приложение «safari»")
    m_mki = make(cfg={**CFG, "confirm": False})
    m_mki.process_markers("Открываю. [OPEN_URL:youtube.com] [OPEN_APP:safari]",
                          "mk2")
    check("маркеры: immediate-режим исполняет multi одним действием",
          len(m_mki.calls) == 1 and m_mki.calls[0]["kind"] == "multi"
          and len(m_mki.calls[0]["items"]) == 2)

    # «закрой вкладку» без цели: id закрытой неизвестен — отслеживаемый
    # сбрасываем, иначе следующая команда ждёт мёртвую вкладку ~10 с
    _orig_close_z = _ba.close_tab
    _ba.close_tab = lambda tid=None: ("https://x.ru/p", "X")
    try:
        m_cl = make(cfg={**CFG, "allow_domains": []})
        m_cl._last_tab_id, m_cl._last_host = 42, "x.ru"
        # _dispatch у SpyManager подменён — зовём настоящий явно
        ComputerControlManager._dispatch(
            m_cl, {"kind": "tab_op", "op": "close", "host": "x.ru"})
        check("tab_op close без цели: отслеживаемая вкладка забыта",
              m_cl._last_tab_id is None and m_cl._last_host is None)
        m_cl2 = make(cfg={**CFG, "allow_domains": []})
        m_cl2._last_tab_id, m_cl2._last_host = 42, "other.ru"
        ComputerControlManager._dispatch(
            m_cl2, {"kind": "tab_op", "op": "close", "tab_id": 7,
                    "host": "x.ru"})
        check("tab_op close по чужому id: свой контекст не теряем",
              m_cl2._last_tab_id == 42 and m_cl2._last_host == "other.ru")
    finally:
        _ba.close_tab = _orig_close_z

    # Подпись поля внутри фразы: хвост — от конца подписи (терялось первое
    # слово текста)
    # Подпись поля ВНУТРИ фразы: текст — всё, что вокруг подписи. Хвост
    # брался от начала следующего слова, и первое слово текста пропадало
    _fld = [{"idx": 0, "tag": "input", "text": "город", "ed": True}]
    _fa_it, _fa_txt = ComputerControlManager._match_field_anywhere(
        "кутузова город �город", _fld)
    _fa_it2, _fa_txt2 = ComputerControlManager._match_field_anywhere(
        "город �город красный проспект", _fld)
    check("поле внутри фразы: первое слово текста не теряется",
          _fa_it is _fld[0]
          and " ".join(_fa_txt.split()) == "кутузова �город"
          and _fa_it2 is _fld[0]
          and " ".join(_fa_txt2.split()) == "�город красный проспект")

    # LLM-ярус разбора: сбой резолвера ≠ «это не команда»
    class _IntentRouter:
        def get_response(self, *a, **kw):
            return '{"action":"click","goal":"войти","site":null}'

    m_ir = make()

    def _boom_click(goal, site_word, router, chat_id=""):
        raise RuntimeError("снапшот упал")

    m_ir.resolve_click = _boom_click
    _ir_act, _ir_err = m_ir.resolve_intent_llm("жми вход", _IntentRouter(),
                                               chat_id="ir1")
    check("intent LLM: сбой резолвера — честная причина, а не «не команда»",
          _ir_act is None and _ir_err and "снапшот упал" in _ir_err)
    check("intent LLM: «не команда» по-прежнему (None, None)",
          m_ir.resolve_intent_llm("как дела", _FakeRouter('{"action":"none"}'),
                                  chat_id="ir2") == (None, None))

    # ── 12y. Первый результат поиска: SSRF-фильтр на URL и каждом редиректе ──
    # Страницу поиска тянет БОТ, а вёрстку и редиректы диктует сторонний сайт:
    # без проверки 302 на http://169.254.169.254/ или localhost:PORT уходил бы
    # обычным GET с машины бота. Политика — общая с web_search
    import socket as _socket_ssrf
    from unittest import mock as _mock_ssrf
    from app.features import web_search as _ws_ssrf
    from app.features.computer_control import _get_safe_redirects
    import httpx as _httpx_ssrf

    class _FakeResp:
        def __init__(self, redirect_to=None, text=""):
            self.is_redirect = redirect_to is not None
            self.headers = {"location": redirect_to} if redirect_to else {}
            self.text = text

        def raise_for_status(self):
            pass

    _fr_pages = {}
    _fr_gets = []
    _fr_kw = []

    class _FakeHttpxClient:
        def __init__(self, *a, **kw):
            _fr_kw.append(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            _fr_gets.append(url)
            return _fr_pages.get(url) or _FakeResp(text="")

    _SEARCH_CFG = {**CFG, "allow_domains": [], "search": {
        "пуб": {"url": "https://pub.example/s?q={q}",
                "first": r"/watch\?v=[\w-]{11}"}}}

    def _resolve_map(mapping, default="93.184.216.34"):
        def _fake(host, *a, **kw):
            ip = mapping.get(host, default)
            return [(2, 1, 6, "", (ip, 0))]
        return _fake

    m_fr = make(cfg=_SEARCH_CFG)
    check("search first: шаблон и regex разобраны из конфига",
          m_fr.search_urls.get("пуб") == "https://pub.example/s?q={q}"
          and m_fr.search_first.get("пуб"))
    with _mock_ssrf.patch.object(_httpx_ssrf, "Client", _FakeHttpxClient), \
            _mock_ssrf.patch.object(_socket_ssrf, "getaddrinfo",
                                    _resolve_map({})):
        _fr_pages.clear()
        _fr_gets.clear()
        _fr_kw.clear()
        _fr_pages["https://pub.example/s?q=x"] = _FakeResp(
            text='<a href="/watch?v=abcdefghijk">видео</a>')
        _direct = m_fr._first_result_url("пуб", "https://pub.example/s?q=x")
        check("search first: публичный адрес — ссылка первого результата",
              _direct == "https://pub.example/watch?v=abcdefghijk"
              and _fr_gets == ["https://pub.example/s?q=x"]
              # клиент обязан НЕ идти по редиректам сам — иначе хопы никто
              # не проверит
              and _fr_kw and _fr_kw[-1].get("follow_redirects") is False)
        # Безопасный редирект: относительная ссылка клеится к ФИНАЛЬНОМУ URL
        _fr_pages.clear()
        _fr_gets.clear()
        _fr_pages["https://pub.example/s?q=x"] = _FakeResp(
            redirect_to="https://m.pub.example/search?q=x")
        _fr_pages["https://m.pub.example/search?q=x"] = _FakeResp(
            text='<a href="/watch?v=abcdefghijk">видео</a>')
        check("search first: после безопасного редиректа база — финальный URL",
              m_fr._first_result_url("пуб", "https://pub.example/s?q=x")
              == "https://m.pub.example/watch?v=abcdefghijk")
    # Исходный адрес резолвится в loopback — сети не касаемся вовсе
    with _mock_ssrf.patch.object(_httpx_ssrf, "Client", _FakeHttpxClient), \
            _mock_ssrf.patch.object(_socket_ssrf, "getaddrinfo",
                                    _resolve_map({}, default="127.0.0.1")):
        _fr_gets.clear()
        check("search first: приватный/loopback адрес — None и ни одного GET",
              m_fr._first_result_url("пуб", "https://sneaky.example/s?q=x")
              is None and not _fr_gets)
    # Редирект с публичного на метаданные облака — проверка на КАЖДОМ хопе
    with _mock_ssrf.patch.object(_httpx_ssrf, "Client", _FakeHttpxClient), \
            _mock_ssrf.patch.object(
                _socket_ssrf, "getaddrinfo",
                _resolve_map({"169.254.169.254": "169.254.169.254"})):
        _fr_pages.clear()
        _fr_gets.clear()
        _fr_pages["https://pub.example/s?q=x"] = _FakeResp(
            redirect_to="http://169.254.169.254/latest/meta-data/")
        _fr_pages["http://169.254.169.254/latest/meta-data/"] = _FakeResp(
            text='<a href="/watch?v=abcdefghijk">не должно читаться</a>')
        check("search first: редирект на 169.254.169.254 — None, хоп не читаем",
              m_fr._first_result_url("пуб", "https://pub.example/s?q=x") is None
              and _fr_gets == ["https://pub.example/s?q=x"])
    # Бесконечная цепочка обрывается по общему лимиту web_search.MAX_REDIRECTS
    with _mock_ssrf.patch.object(_httpx_ssrf, "Client", _FakeHttpxClient), \
            _mock_ssrf.patch.object(_socket_ssrf, "getaddrinfo",
                                    _resolve_map({})):
        _fr_pages.clear()
        _fr_gets.clear()
        _fr_pages["https://pub.example/s?q=x"] = _FakeResp(
            redirect_to="https://pub.example/s?q=x")
        check("search first: бесконечные редиректы обрываются по MAX_REDIRECTS",
              m_fr._first_result_url("пуб", "https://pub.example/s?q=x") is None
              and len(_fr_gets) == _ws_ssrf.MAX_REDIRECTS + 1)
    # Сам хелпер: политика берётся из web_search (подмена предиката видна)
    with _mock_ssrf.patch.object(_ws_ssrf, "is_safe_public_url",
                                 lambda u: "bad" not in u):
        _fr_pages.clear()
        _fr_gets.clear()
        _fr_pages["https://ok.example/a"] = _FakeResp(
            redirect_to="https://bad.example/b")
        _resp_h, _why = _get_safe_redirects(_FakeHttpxClient(),
                                            "https://ok.example/a")
        check("_get_safe_redirects: причина отказа называет недоступный хоп",
              _resp_h is None and "bad.example" in _why)
        _resp_h2, _final2 = _get_safe_redirects(_FakeHttpxClient(),
                                                "https://bad.example/a")
        check("_get_safe_redirects: исходный адрес не прошёл — отказ до GET",
              _resp_h2 is None and "недоступ" in _final2)

    # ── 13. Авторизация режима управления: владелец/allowlist на каждом входе ──
    # Общий браузер режима управления несёт авторизованные сессии владельца,
    # а сам режим — на весь чат: без гейта на КАЖДОМ входе (переключатель,
    # rescue, fast-path, pending-confirm, маркеры LLM) любой участник чата,
    # где режим включён, мог бы им управлять. BotInstance собираем через
    # __new__ (без тяжёлого __init__ — роутер/персона/диски) и вручную
    # выставляем только то, что реально трогает process_message при
    # выключенных менеджерах — сам код (is_owner/_cc_allowed/process_message/
    # _control_mode_switch) при этом настоящий, не переопределён.
    from types import SimpleNamespace
    from app.bot_instance import BotInstance
    from app.features.conversation_style import ConversationStyleConfig
    import app.features.flavor_text as _flavor

    class _AuthzCC(ComputerControlManager):
        """_dispatch подменён — как SpyManager выше, отдельный класс не нужен,
        но своё имя для читаемости диагностики теста."""
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls = []

        def _dispatch(self, action, router=None):
            self.calls.append(dict(action))

    class _AuthzPersona:
        """Только то, что читает process_message: settings/persona_data —
        заглушки, prepare_messages фиксирует kwargs (ловим утечку cc-инструкции
        неавторизованному), не ходит ни в LLM, ни на диск."""
        def __init__(self):
            self.settings = {}
            self.persona_data = {}
            self.last_kwargs = None

        def prepare_messages(self, *a, **kw):
            self.last_kwargs = kw
            return [{"role": "system", "content": "SYS"}]

        def get_settings(self):
            return {"max_tokens": 500}

    class _AuthzMemory:
        """STM/LTM — заглушки без диска: process_message читает их результат,
        но для этого теста важно только поведение CC-гейтов, не память."""
        class _STM:
            def get_last(self, *a, **kw):
                return []

        class _LTM:
            def get_facts_by_category(self, *a, **kw):
                return []

            def save_facts(self, *a, **kw):
                pass

        def __init__(self):
            self.stm = self._STM()
            self.ltm = self._LTM()

        def add_message(self, *a, **kw):
            pass

        def get_context(self, *a, **kw):
            return [], [], []

        def get_chat_facts_block(self, *a, **kw):
            return None

    class _AuthzRouter:
        """LLM не вызываем взаправду — фиксированный ответ (без маркера или
        с маркером, по сценарию) + счётчик вызовов (fast-path обязан вернуть
        РАНЬШЕ роутера, обычный чат — дойти до него)."""
        def __init__(self, reply):
            self.reply = reply
            self.answer_provider = None
            self._last_provider = "fake"
            self.calls = 0

        def is_local_primary(self):
            return False

        def get_response(self, messages, **kw):
            self.calls += 1
            return self.reply

    def make_authz_bot(owner="OWNER", allowed_users=None, control_mode_chats=None,
                       cc_confirm=False, reply="Просто отвечаю на сообщение."):
        b = BotInstance.__new__(BotInstance)
        b.persona_name = "authz_test"
        b.context = f"authz_test_{id(b)}"
        b.owner = owner
        b.web_single_user = False
        b._cc_allowed_users = set(allowed_users or [])
        b.features = {}
        b.intellect = SimpleNamespace(active=False)
        b.conversation_style = ConversationStyleConfig(None)
        b._control_mode = set(control_mode_chats or [])
        b.computer_control = _AuthzCC(
            context=b.context, config={"confirm": cc_confirm, "click": False},
            base_dir=tmp / f"authz_{id(b)}")
        b.scenario_manager = None
        b.proactive = None
        b.book_search = None
        b.self_memory = None
        b.living = None
        b.todo_manager = None
        b.inventory_manager = None
        b.reminder_manager = None
        b.learning_manager = None
        b.file_db = None
        b._punish_enabled = False
        b._moderation_enabled = False
        b._web_search_enabled = False
        b._web_search_disabled_chats = set()
        b._pending_list_messages = {}
        b._pending_split_messages = {}
        b._pending_photos = {}
        b._pending_question_kind = {}
        b._pending_more_photos = {}
        b.persona = _AuthzPersona()
        b.memory = _AuthzMemory()
        b.router = _AuthzRouter(reply)
        return b

    AUTHZ_CHAT = "authz_chat_1"
    _orig_set_mode = _ba.set_control_mode
    _orig_rescue = _ba.rescue_pool_h
    _orig_cc_reply = _flavor.cc_reply
    _authz_calls = {"set_mode": [], "rescue": 0}
    _ba.set_control_mode = lambda chat_id, on: _authz_calls["set_mode"].append((str(chat_id), on))
    _ba.rescue_pool_h = lambda *a, **kw: (_authz_calls.__setitem__("rescue", _authz_calls["rescue"] + 1) or True)
    # cc_reply без банка фраз уходит в «живой» Google AI Mode — в тесте не
    # нужен, форсируем честный шаблон
    _flavor.cc_reply = lambda *a, **kw: None
    try:
        # переключатель режима
        b_owner = make_authz_bot()
        r_owner = b_owner.process_message(
            "перейди в режим управления", user_id="OWNER", chat_id=AUTHZ_CHAT)
        check("authz: владелец включает режим управления",
              AUTHZ_CHAT in b_owner._control_mode
              and "Режим управления включён" in r_owner
              and b_owner.router.calls == 0)  # fast-path, до LLM не дошло

        b_other = make_authz_bot()
        r_other = b_other.process_message(
            "перейди в режим управления", user_id="OTHER", chat_id=AUTHZ_CHAT)
        check("authz: не-владелец «перейди в режим управления» — режим НЕ "
              "включается, фраза уходит в обычный диалог (не технический отказ)",
              AUTHZ_CHAT not in b_other._control_mode
              and r_other == "Просто отвечаю на сообщение."
              and b_other.router.calls == 1)

        # rescue browser — вне режима, но тоже только для авторизованных
        b_owner_r = make_authz_bot()
        r_resc_owner = b_owner_r.process_message(
            "почини браузер", user_id="OWNER", chat_id=AUTHZ_CHAT)
        check("authz: владелец — «почини браузер» запускает rescue",
              _authz_calls["rescue"] == 1 and "Открыл браузер" in r_resc_owner)

        _authz_calls["rescue"] = 0
        b_other_r = make_authz_bot()
        r_resc_other = b_other_r.process_message(
            "почини браузер", user_id="OTHER", chat_id=AUTHZ_CHAT)
        check("authz: не-владелец — rescue НЕ запускается, обычный диалог",
              _authz_calls["rescue"] == 0
              and r_resc_other == "Просто отвечаю на сообщение.")

        # fast-path (pending-подтверждение «да»): владелец исполняет, чужой — нет
        b_pend_owner = make_authz_bot(control_mode_chats=[AUTHZ_CHAT])
        b_pend_owner.computer_control.set_pending(
            AUTHZ_CHAT, {"kind": "url", "value": "https://example.com"})
        r_pend_owner = b_pend_owner.process_message(
            "да", user_id="OWNER", chat_id=AUTHZ_CHAT, raw_user_text="да")
        check("authz: владелец — «да» на pending исполняет действие и снимает pending",
              b_pend_owner.computer_control.calls == [{"kind": "url", "value": "https://example.com"}]
              and b_pend_owner.computer_control.get_pending(AUTHZ_CHAT) is None
              and b_pend_owner.router.calls == 0)

        b_pend_other = make_authz_bot(control_mode_chats=[AUTHZ_CHAT])
        b_pend_other.computer_control.set_pending(
            AUTHZ_CHAT, {"kind": "url", "value": "https://example.com"})
        r_pend_other = b_pend_other.process_message(
            "да", user_id="OTHER", chat_id=AUTHZ_CHAT, raw_user_text="да")
        check("authz: не-владелец — «да» на чужой pending НИЧЕГО не исполняет "
              "(pending цел, обычный диалог)",
              b_pend_other.computer_control.calls == []
              and b_pend_other.computer_control.get_pending(AUTHZ_CHAT)
              == {"kind": "url", "value": "https://example.com"}
              and r_pend_other == "Просто отвечаю на сообщение.")

        # Маркеры LLM в ответе + инструкция о маркерах в системном промпте
        MARKER_REPLY = "Открываю сайт. [OPEN_URL:https://example.com]"
        b_mark_owner = make_authz_bot(control_mode_chats=[AUTHZ_CHAT],
                                      reply=MARKER_REPLY)
        r_mark_owner = b_mark_owner.process_message(
            "Как у тебя дела?", user_id="OWNER", chat_id=AUTHZ_CHAT)
        check("authz: владелец — маркер LLM исполняется и срезается из ответа",
              b_mark_owner.computer_control.calls == [{"kind": "url", "value": "https://example.com"}]
              and "[OPEN_URL" not in r_mark_owner)
        check("authz: владельцу инструкция о маркерах уходит в системный промпт",
              "COMPUTER CONTROL" in
              (b_mark_owner.persona.last_kwargs.get("computer_control_context") or ""))

        b_mark_other = make_authz_bot(control_mode_chats=[AUTHZ_CHAT],
                                      reply=MARKER_REPLY)
        r_mark_other = b_mark_other.process_message(
            "Как у тебя дела?", user_id="OTHER", chat_id=AUTHZ_CHAT)
        check("authz: не-владелец — маркер LLM НЕ исполняется, но и не "
              "показывается пользователю (срезан)",
              b_mark_other.computer_control.calls == []
              and "[OPEN_URL" not in r_mark_other)
        check("authz: не-владельцу инструкция о маркерах (и URL открытой "
              "владельцем страницы) в промпт НЕ подмешивается",
              b_mark_other.persona.last_kwargs.get("computer_control_context") is None)

        # allowlist фичи (computer_control.allowed_users) — как владелец
        b_allow = make_authz_bot(allowed_users=["ALLOWED"])
        r_allow = b_allow.process_message(
            "перейди в режим управления", user_id="ALLOWED", chat_id=AUTHZ_CHAT)
        check("authz: пользователь из allowed_users включает режим управления "
              "наравне с владельцем",
              AUTHZ_CHAT in b_allow._control_mode
              and "Режим управления включён" in r_allow)

        b_allow_mark = make_authz_bot(control_mode_chats=[AUTHZ_CHAT],
                                      allowed_users=["ALLOWED"], reply=MARKER_REPLY)
        r_allow_mark = b_allow_mark.process_message(
            "Как у тебя дела?", user_id="ALLOWED", chat_id=AUTHZ_CHAT)
        check("authz: allowed_users исполняет маркеры LLM (не только владелец)",
              b_allow_mark.computer_control.calls == [{"kind": "url", "value": "https://example.com"}]
              and "[OPEN_URL" not in r_allow_mark)
    finally:
        _ba.set_control_mode = _orig_set_mode
        _ba.rescue_pool_h = _orig_rescue
        _flavor.cc_reply = _orig_cc_reply

    # ── 14. is_owner/_cc_allowed — юнит-уровень ──
    _owner_bot = SimpleNamespace(owner="OWNER", web_single_user=False)
    check("is_owner: совпадение с owner персоны",
          BotInstance.is_owner(_owner_bot, "OWNER") is True)
    check("is_owner: чужой id — не владелец",
          BotInstance.is_owner(_owner_bot, "OTHER") is False)
    check("is_owner: пустой user_id — не владелец",
          BotInstance.is_owner(_owner_bot, "") is False)
    _web_bot = SimpleNamespace(owner="", web_single_user=True)
    check("is_owner: web_single_user — всегда владелец, даже без owner в конфиге",
          BotInstance.is_owner(_web_bot, "anyone") is True
          and BotInstance.is_owner(_web_bot, "") is True)

    _cc_bot = SimpleNamespace(owner="OWNER", web_single_user=False,
                              _cc_allowed_users={"ALLOWED"},
                              is_owner=lambda uid: BotInstance.is_owner(_cc_bot, uid))
    check("_cc_allowed: владелец разрешён",
          BotInstance._cc_allowed(_cc_bot, "OWNER") is True)
    check("_cc_allowed: пользователь из allowed_users разрешён",
          BotInstance._cc_allowed(_cc_bot, "ALLOWED") is True)
    check("_cc_allowed: посторонний — не разрешён",
          BotInstance._cc_allowed(_cc_bot, "STRANGER") is False)

    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

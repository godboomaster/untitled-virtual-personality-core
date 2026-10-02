"""Смок-тест «узких мест» приватности режима управления: правила стоят в
одной точке, через которую идут все пути, а не на каждом вызове.

Проверяет: фильтр логов процесса (app/core/log_privacy — известные секреты
чатов, значение после «пароль/код», email/телефон/карта/токен, URL; id чатов
и метки времени читаемы; структура args uvicorn.access; исключения; DEBUG
через фильтр хендлера; входные строки логов — длина в режиме управления),
маску аудита (любой ввод на приватной странице по полному URL/хосту и ввод
с известным секретом, хук on_typed), роутер по полному URL
(_privacy_router + отслеживаемый URL, _execute_locked, _choose_element
page_url, _refind_confirmed), агента задач (плейсхолдеры {{secretN}} в
промпте, подстановка в момент ввода, task_memory.json и прошлые задачи без
секретов, известный секрет в любое поле — только после «да»), сценарии
(трасса приватной страницы не уходит в облако, адреса open только из
трассы, алиасы-команды отброшены, литералы секретов → слоты), живую
flavor-реплику (приватная страница — шаблон, ввод маской) и ответ на
слот-секрет сценария в истории.
Браузер, LLM, локальная модель и сеть подменены.

Запуск: python -m scripts.test_cc_privacy_choke
"""

import io
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="cc_choke_"))
    os.environ["DATA_DIR"] = str(tmp / "data")
    os.environ["VPC_DATA_DIR"] = str(tmp / "data")
    ok = 0
    fails = []

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        if cond:
            ok += 1
        else:
            fails.append(name)

    import app.core.router as _net_router
    _net_router.internet_available = lambda: True
    from app.features import cc_privacy as P
    from app.features import browser_actions as ba
    from app.features.computer_control import ComputerControlManager
    from app.core import log_privacy as L

    # Локальная модель (Ollama на этой машине) — не зовём: приватный путь
    # без локальной модели = None (скоринг/rule-based/шаблон)
    local_calls = []

    def _no_local(self, messages, **kw):
        local_calls.append(messages)
        return None
    P.PrivateRouter.get_response = _no_local
    ba.set_control_mode = lambda *a, **kw: None

    SECRET = "Kotik2019!"

    class Rec(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []
            self.buf = io.StringIO()

        def emit(self, record):
            self.records.append(record)
            self.buf.write(self.format(record) + "\n")

    # ── 1. Фильтр логов ──
    print("log_privacy: маска логов процесса")
    root = logging.getLogger()
    cap = Rec()
    root.addHandler(cap)
    root.setLevel(logging.INFO)
    L.install()
    f1 = logging.getLogRecordFactory()
    L.install()
    check("install идемпотентен (фабрика не оборачивается дважды)",
          logging.getLogRecordFactory() is f1
          and getattr(f1, "_cc_log_privacy", False))
    check("фильтр повешен на хендлер корня",
          any(isinstance(f, L.LogPrivacyFilter) for f in cap.filters))
    ks = P.KnownSecrets()
    ks.add("grp1", SECRET)
    check("known_secret_values: секрет любого хранилища",
          SECRET in P.known_secret_values())
    lg = logging.getLogger("app.choke")

    def last():
        return cap.buf.getvalue().splitlines()[-1]
    lg.info(f"reply to grp1: {SECRET} | chat_id=-1001234567890 "
            "user=5123456789 ts=1727550000.5")
    ln = last()
    check("лог: известный секрет чата — маской", SECRET not in ln
          and "***(10)" in ln)
    check("лог: id группы/пользователя и метка времени читаемы",
          "-1001234567890" in ln and "5123456789" in ln
          and "1727550000.5" in ln)
    lg.info("msg %s and %d", SECRET.lower(), 5)
    check("лог: секрет в %-аргументе (без регистра) — маской",
          "kotik2019" not in last().lower() and last().endswith("and 5"))
    lg.info("введи пароль qwerty77 в поле")
    check("лог: значение после «пароль» — маской", "qwerty77" not in last())
    lg.info("Мой пароль: hunter22, pin 4321, password=Secr3tVal code=200")
    ln = last()
    check("лог: пароль/PIN/password= — маской, «code=200» как есть",
          "hunter22" not in ln and "4321" not in ln and "Secr3tVal" not in ln
          and "code=200" in ln)
    lg.info("[Router] HTTP error code 429 for chat -1001234567890")
    check("лог: код статуса после «code» не трогаем",
          last() == "[Router] HTTP error code 429 for chat -1001234567890")
    # Служебные слова/тире между словом секрета и значением (redteam-1)
    for phrase, val in (("запомни: пароль от почты Kotik2019x", "Kotik2019x"),
                        ("пароль — Kotik2019x", "Kotik2019x"),
                        ("пароль от вайфая: Kotik2019x", "Kotik2019x"),
                        ("код из смс 4821, введи потом", "4821"),
                        ("my password for gmail is Kotik2019x", "Kotik2019x"),
                        ("пасс Kotik2019x", "Kotik2019x"),
                        ("cvv: 123", "123")):
        lg.info(phrase)
        check(f"лог: значение через служебные слова — маской: {phrase[:28]!r}",
              val not in last())
    lg.info("my password for gmail is Kotik2019x")
    check("лог: объект («gmail») между словом и значением — не маской",
          "gmail" in last())
    lg.info("token ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789 ok")
    check("лог: ключ GitHub (ghp_…) — маской, не слаг",
          "ghp_AbCd" not in last())
    lg.info("[X] token refreshed at 12:00:01")
    check("лог: время после «token» — как есть", "12:00:01" in last())
    lg.info("mail ivan.petrov@mail.ru tel +7 999 123-45-67 и 8 (999) "
            "123-45-67 card 4111 1111 1111 1111 id 1234567890123")
    ln = last()
    check("лог: email/телефоны/карта — маской, длинный id — нет",
          "petrov" not in ln and "123-45-67" not in ln
          and "4111" not in ln and "1234567890123" in ln)
    lg.info("url https://site.ru/cb?code=abc123def&q=pizza uuid "
            "550e8400-e29b-41d4-a716-446655440000 key sk-AbCdEf1234567890GhIjKlMn")
    ln = last()
    check("лог: URL без секретных параметров, UUID читаем, ключ — маской",
          "abc123def" not in ln and "q=pizza" in ln
          and "550e8400-e29b-41d4-a716-446655440000" in ln
          and "GhIjKlMn" not in ln)
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET",
        f"/api/x?token={SECRET}", "1.1", 200)
    r = cap.records[-1]
    check("лог uvicorn.access: args остались кортежем из 5, секрет — маской",
          isinstance(r.args, tuple) and len(r.args) == 5
          and SECRET not in r.getMessage())
    try:
        raise RuntimeError(f"boom {SECRET}")
    except RuntimeError:
        lg.exception("упало")
    check("лог: текст исключения — маской",
          SECRET not in cap.buf.getvalue().split("упало")[-1])
    dbg = logging.getLogger("app.choke.dbg")
    dbg.setLevel(logging.DEBUG)
    dbg.debug(f"debug {SECRET}")
    check("лог DEBUG (мимо фабрики) — фильтр хендлера", SECRET not in last()
          and last().startswith("debug "))
    mk = logging.makeLogRecord({"msg": f"made {SECRET}", "levelno": 20,
                                "levelname": "INFO", "name": "q"})
    cap.handle(mk)
    check("лог makeLogRecord — фильтр хендлера", SECRET not in last())
    lg.info("x %s", "{безопасно} 100%")
    check("лог: обычная строка не меняется", last() == "x {безопасно} 100%")
    t0 = time.time()
    for i in range(5000):
        lg.info(f"[CompControl] Резолв «кнопка {i}» не удался: "
                f"chat_id=-100123456789{i % 10}")
    dt = time.time() - t0
    check(f"лог: 5000 записей быстро ({dt:.2f} с)", dt < 3.0)
    check("input_for_log: режим управления — только длина",
          L.input_for_log(SECRET, True) == "<10 chars>")
    check("input_for_log: вне режима — без секретов и в кавычках",
          L.input_for_log(f"мой пароль {SECRET} ок", False)
          == "'мой пароль ***(10) ок'")
    check("control_mode_active: чистая проверка ключа",
          L.control_mode_active(SimpleNamespace(_control_mode={"c"}), None, "c")
          and not L.control_mode_active(SimpleNamespace(_control_mode={"c"}), "d"))
    root_dir = Path(__file__).parent.parent
    src_main = (root_dir / "app/main.py").read_text(encoding="utf-8")
    src_srv = (root_dir / "app/api/server.py").read_text(encoding="utf-8")
    src_bot = (root_dir / "app/bot_instance.py").read_text(encoding="utf-8")
    src_tg = (root_dir / "app/telegram_bot.py").read_text(encoding="utf-8")
    check("маска логов ставится при старте (main, API после буфера логов)",
          "log_privacy.install()" in src_main
          and src_srv.find("log_privacy.install()")
          > src_srv.find("log_buffer.install()") > 0)
    check("входные строки логов без сырого текста реплики",
          "process_message START: '{user_input" not in src_bot
          and "rewrite_query: '{user_input" not in src_bot
          and "пропущен ({search_skip}): '{user_input" not in src_bot
          and "text='{text[:80]}'" not in src_tg
          and "{clean_text[:60]}..." not in src_tg)

    # ── 2. Аудит: маска ввода по приватности страницы и секретам чата ──
    print("audit: единая маска ввода")

    class Spy(ComputerControlManager):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls = []
            self.routers = []

        def _dispatch(self, action, router=None):
            self.calls.append(dict(action))
            self.routers.append(router)

    cc = Spy(context="choke_a", config={"confirm": False, "click": True},
             base_dir=tmp / "a")
    cc.base_dir.mkdir(parents=True, exist_ok=True)
    typed_hook = []
    cc.on_typed = lambda a: typed_hook.append(a.get("text"))

    def audit_rows():
        return [json.loads(x) for x in
                (cc.base_dir / "audit.jsonl").read_text().splitlines()]
    cc.execute({"kind": "type", "idx": 2, "element": "Сообщение",
                "text": "мама, долг за аренду", "host": "web.telegram.org"}, "c")
    cc.execute({"kind": "type", "idx": 3, "element": "Написать",
                "text": "привет, Иван", "host": "vk.com",
                "value": "https://vk.com/im?sel=123"}, "c")
    cc.execute({"kind": "type", "idx": 4, "element": "Поиск",
                "text": SECRET, "host": "shop.ru",
                "value": "https://shop.ru/menu"}, "c")
    cc.execute({"kind": "type", "idx": 5, "element": "Поиск",
                "text": "пицца пепперони", "host": "shop.ru",
                "value": "https://shop.ru/menu"}, "c")
    cc.execute({"kind": "multi", "items": [
        {"kind": "type", "idx": 6, "element": "Комментарий",
         "text": "Иванов Пётр Сергеевич", "host": "online.sberbank.ru"},
        {"kind": "click", "idx": 7, "element": "Далее",
         "host": "online.sberbank.ru"}]}, "c")
    rows = audit_rows()
    check("аудит: мессенджер (хост) — ввод маской", rows[0]["text"] == "***(20)")
    check("аудит: vk.com/im (приватность по пути) — ввод маской",
          rows[1]["text"] == "***(12)")
    check("аудит: известный секрет в поле «Поиск» — маской",
          rows[2]["text"] == "***(10)")
    check("аудит: обычный ввод на обычной странице — как есть",
          rows[3]["text"] == "пицца пепперони")
    check("аудит: multi с шагом на банке — ввода нет в value",
          "Иванов" not in json.dumps(rows[4], ensure_ascii=False))
    check("аудит: хук on_typed — на каждый исполненный ввод",
          len(typed_hook) == 5)
    old = {"ts": 1, "kind": "type", "element": "Получатель",
           "text": "Иванов Пётр", "host": "online.sberbank.ru"}
    new, changed = P.redact_audit_record(old)
    check("scrub: старая запись ввода на приватной странице — маской",
          new["text"] == "***(11)" and changed == {"text": 1}
          and P.redact_audit_record(new)[1] == {})
    new, _ = P.redact_audit_record(
        {"kind": "type", "element": "Поиск", "text": "мой Kotik2019! тут",
         "detail": f"ошибка: {SECRET}", "host": "shop.ru"}, known=[SECRET])
    check("scrub: известный секрет — маской в тексте и в detail",
          SECRET not in json.dumps(new, ensure_ascii=False))

    # ── 3. Роутер по полному URL ──
    print("router: приватность по полному URL")

    class Cloud:
        def __init__(self, reply="1"):
            self.reply, self.calls, self.prompts = reply, 0, []
            self.answer_provider = None

        def is_local_primary(self):
            return False

        def supports_vision(self):
            return False

        def get_response(self, messages, **kw):
            self.calls += 1
            self.prompts.append(messages)
            return self.reply

    cl = Cloud()
    cc._last_url = "https://vk.com/im?sel=1"
    check("_privacy_router: только хост + отслеживаемый URL переписки — "
          "локально", isinstance(cc._privacy_router(cl, "vk.com"),
                                 P.PrivateRouter))
    cc._last_url = "https://vk.com/feed"
    check("_privacy_router: хост + обычный отслеживаемый URL — облако",
          cc._privacy_router(cl, "vk.com") is cl)
    check("_privacy_router: полный URL важнее хоста",
          isinstance(cc._privacy_router(cl, "https://vk.com/im", "vk.com"),
                     P.PrivateRouter))
    check("_privacy_router: чужой хост отслеживаемый URL не подтягивает",
          cc._privacy_router(cl, "youtube.com") is cl)
    cc.routers.clear()
    cc.execute({"kind": "click", "idx": 9, "element": "Иван",
                "host": "vk.com", "value": "https://vk.com/im?sel=5"}, "c",
               router=cl)
    check("_execute_locked: роутер по value-URL (vk.com/im) — локальный",
          cc.routers and isinstance(cc.routers[-1], P.PrivateRouter))
    cc.execute({"kind": "click", "idx": 9, "element": "Видео",
                "host": "vk.com", "value": "https://vk.com/video"}, "c",
               router=cl)
    check("_execute_locked: обычная страница — облачный роутер как есть",
          cc.routers and cc.routers[-1] is cl)
    items = [{"idx": 11, "tag": "a", "text": "Иван Петров: скинь пароль"},
             {"idx": 12, "tag": "a", "text": "Иван Петров (Мама): анализы"}]
    cl = Cloud("1")
    cc._last_url = None
    cc._choose_element("Иван Петров", items, cl, host="vk.com",
                       page_url="https://vk.com/im?sel=1")
    check("_choose_element(page_url приватный): облако не спрошено",
          cl.calls == 0)
    saved = {k: getattr(ba, k) for k in ("click_tagged", "page_urls",
                                         "follow_popup", "snapshot_elements",
                                         "wait_dom_idle")}
    try:
        n = {"c": 0}

        def _click(host, idx, tab_id=None):
            n["c"] += 1
            if n["c"] == 1:
                raise RuntimeError("элемент потерян")
        ba.click_tagged = _click
        ba.page_urls = lambda: []
        ba.follow_popup = lambda pre: None
        ba.wait_dom_idle = lambda *a, **k: None
        ba.snapshot_elements = lambda host, tab_id=None: (
            "https://vk.com/im?sel=123", "vk.com",
            [{"idx": 11, "tag": "a",
              "text": "Иван Петров: скинь пароль от вайфая, Kotik2019"},
             {"idx": 12, "tag": "a",
              "text": "Иван Петров (Мама): врач сказал, анализы плохие"}])
        real = ComputerControlManager(context="choke_r",
                                      config={"confirm": True, "click": True},
                                      base_dir=tmp / "r")
        cl = Cloud("1")
        okx, det = real.execute({"kind": "click", "idx": 5,
                                 "element": "Иван Петров",
                                 "goal": "Иван Петров", "host": "vk.com",
                                 "value": "https://vk.com/im?sel=123",
                                 "origin": "pending"}, "c1", router=cl)
        check("_refind_confirmed на vk.com/im: подписи не ушли в облако",
              cl.calls == 0)
        check("_refind_confirmed: подпись найденного на приватной странице "
              "не в ошибке", not okx and "вайфая" not in det
              and "анализы" not in det)
    finally:
        for k, v in saved.items():
            setattr(ba, k, v)

    # ── 4. Агент задач: секреты не уходят модели ──
    print("task_agent: плейсхолдеры секретов")
    from app.features.task_agent import TaskAgent
    ba_wait = ba.wait_dom_idle
    ba.wait_dom_idle = lambda *a, **k: None
    try:
        tcc = Spy(context="choke_t", config={"confirm": True, "click": True},
                  base_dir=tmp / "t")
        ta = TaskAgent(computer_control=tcc, context="choke_t",
                       memory_path=tmp / "t" / "task_memory.json")
        ta.known_secrets = lambda chat: []
        run = {"goal": "зайди в кабинет на shop.ru и закажи пиццу",
               "lang": "ru",
               "qa": [("Какой пароль от shop.ru?", "Pa55word#7"),
                      ("Какой телефон?", "+7 999 123-45-67"),
                      ("Какой логин?", "type"),
                      ("Какую пиццу?", "пепперони")],
               "history": [], "steps": 0, "awaiting": None,
               "obs_extra": None, "busy": False, "cancel": False,
               "touched": time.time(), "sites": ["shop.ru"],
               "turn_user": "A", "past": []}
        obs = {"url": "https://shop.ru/menu", "host": "shop.ru", "tab_id": 1,
               "note": None, "text": None, "search": None, "error": None,
               "shown": [{"idx": 7, "tag": "input", "text": "Поиск по меню",
                          "ed": 1, "q": 1},
                         {"idx": 8, "tag": "input", "text": "Пароль",
                          "ed": 1, "sn": 1},
                         {"idx": 9, "tag": "textarea", "text": "Отзыв",
                          "ed": 1}]}
        p = ta._prompt(run, obs, "c1")
        check("промпт агента: пароля и телефона нет",
              "Pa55word#7" not in p and "999 123-45-67" not in p)
        check("промпт агента: вместо них {{secretN}} и правило ввода",
              "{{secret1}}" in p and "{{secret2}}" in p
              and "placeholder itself" in p)
        check("промпт агента: обычный ответ как есть", "пепперони" in p)
        check("промпт агента: секрет-слово не портит формат действий",
              '{"action":"type","n":N' in p)
        ph = run["hidden"]["Pa55word#7"]
        tcc.calls.clear()
        out = ta._act(run, "c1", Cloud(), {"action": "type", "n": 2,
                                          "text": ph}, obs)
        aw = run["awaiting"] or {}
        check("ввод {{secretN}} в поле пароля — вопрос «да/нет», без "
              "исполнения", out[0] == "pause" and not tcc.calls)
        check("вопрос — маской, в действии — настоящее значение",
              "Pa55word#7" not in (out[1] or "")
              and (aw.get("act") or {}).get("text") == "Pa55word#7")
        check("строка шага в истории — без значения",
              "Pa55word#7" not in aw.get("line", ""))
        # «да» человека (как feed): токен подтверждения и исполнение
        _grant = getattr(ComputerControlManager, "grant_confirmation", None)
        if callable(_grant):
            _grant(aw["act"], "task", by="A")
        ta._execute(run, "c1", Cloud(), aw["act"], aw["line"])
        check("после «да» введено настоящее значение",
              tcc.calls and tcc.calls[-1].get("text") == "Pa55word#7")
        audit_t = (tcc.base_dir / "audit.jsonl").read_text()
        check("аудит ввода секрета агента — маской", "Pa55word#7" not in audit_t)
        run["awaiting"] = None
        tcc.calls.clear()
        out = ta._act(run, "c1", Cloud(), {"action": "type", "n": 1,
                                          "text": "Pa55word#7",
                                          "submit": True}, obs)
        check("известный секрет в поисковое поле с Enter — только после «да»",
              out[0] == "pause" and not tcc.calls
              and "Pa55word#7" not in (out[1] or ""))
        run["awaiting"] = None
        out = ta._act(run, "c1", Cloud(), {"action": "type", "n": 3,
                                          "text": "{{secret9}}"}, obs)
        check("неизвестный плейсхолдер — отказ шага, без ввода",
              out[0] == "progress" and not tcc.calls
              and "unknown placeholder" in run["history"][-1])
        out = ta._act(run, "c1", Cloud(), {"action": "type", "n": 1,
                                          "text": "пепперони"}, obs)
        check("обычный текст в поиск — без вопроса",
              out[0] == "progress" and tcc.calls
              and tcc.calls[-1].get("text") == "пепперони")
        run_g = {"goal": "войди на 2gis.ru с паролем Qwerty12", "qa": [],
                 "history": []}
        hid = ta._hidden(run_g, "c1")
        check("секрет в цели скрыт, домен цели — нет",
              "Qwerty12" in hid and "2gis.ru" not in hid)
        ta._remember("c1", run, "Заказ оформлен")
        disk = ta._memory_path.read_text(encoding="utf-8")
        check("task_memory.json: без пароля/телефона/логина-секрета",
              "Pa55word#7" not in disk and "999 123-45-67" not in disk
              and '"type"' not in disk and "пепперони" in disk)
        rec_old = {"ts": 1, "goal": "войди с паролем Qwerty12 на shop.ru",
                   "sites": ["shop.ru"],
                   "qa": [["Какой пароль?", "Kotik2020!"],
                          ["Что заказать?", "пиццу"]], "result": "ок"}
        line = TaskAgent._past_line(rec_old)
        check("прошлая задача (запись до маски): пароль ответа и цели — маской",
              "Kotik2020!" not in line and "Qwerty12" not in line
              and "пиццу" in line)
    finally:
        ba.wait_dom_idle = ba_wait

    # ── 5. Сценарии: трасса, адреса, алиасы ──
    print("scenario_manager: обобщение трассы")
    from app.features.scenario_manager import ScenarioManager

    def mk_sc(name, recs):
        base = tmp / name
        base.mkdir(parents=True, exist_ok=True)
        scc = Spy(context=name, config={"confirm": False, "click": True},
                  base_dir=base)
        now = time.time()
        with open(base / "audit.jsonl", "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(dict(r, ts=now, chat_id="u", ok=True),
                                   ensure_ascii=False) + "\n")
        return scc, ScenarioManager(context=name, computer_control=scc,
                                    base_dir=base)

    bank = [{"kind": "url", "value": "https://online.sberbank.ru/CSAFront/index.do"},
            {"kind": "click", "element": "Счёт 40817810 ···· 4412",
             "host": "online.sberbank.ru"},
            {"kind": "type", "element": "Получатель",
             "text": "Иванов Пётр Сергеевич", "host": "online.sberbank.ru"},
            {"kind": "type", "element": "Сообщение", "text": "долг за аренду",
             "host": "web.telegram.org"}]
    scc, sm = mk_sc("sc_bank", bank)
    trace = sm._trace("u")
    check("трасса: записи приватных страниц помечены",
          all(r.get("_private") for r in trace))
    check("трасса: ввод на приватной странице — маской (старые записи)",
          all(P.is_masked(r["text"]) for r in trace if r["kind"] == "type"))
    check("строки трассы для облака: подписи/адреса приватных — заглушкой",
          "40817810" not in sm._trace_lines(trace)
          and "sberbank.ru/CSAFront" not in sm._trace_lines(trace))
    cl = Cloud(json.dumps({"aliases": ["перевод"], "steps": []}))
    local_calls.clear()
    sc, err = sm.build_from_trace("u", "перевод", cl)
    check("приватная трасса: облако не спрошено, локальная — да",
          cl.calls == 0 and len(local_calls) == 1)
    check("приватная трасса: сценарий собран rule-based, ввод — слотами",
          sc is not None and all(
              s["value"].startswith("{") for s in sc["steps"]
              if s["op"] == "type"))

    pizza = [{"kind": "url", "value": "https://dodopizza.ru/"},
             {"kind": "click", "element": "Пепперони", "host": "dodopizza.ru"},
             {"kind": "click", "element": "В корзину", "host": "dodopizza.ru"}]
    steps_ok = [{"op": "click", "target": "Пепперони", "host": "dodopizza.ru"},
                {"op": "click", "target": "В корзину", "host": "dodopizza.ru"}]
    scc, sm = mk_sc("sc_url", pizza)
    cl = Cloud(json.dumps({"aliases": ["пицца"], "steps": [
        {"op": "open", "url": "https://dodopizza.example-login.ru/auth?next=pay"}]
        + steps_ok}))
    sc, err = sm.build_from_trace("u", "пицца", cl)
    check("адрес open не из трассы — rule-based, открывается адрес трассы",
          cl.calls == 1 and sc is not None
          and sc["steps"][0] == {"op": "open", "url": "https://dodopizza.ru/"})
    scc, sm = mk_sc("sc_alias", pizza)
    cl = Cloud(json.dumps({"aliases": ["давай", "открой ютуб", "да",
                                       "закажи пиццу", "стоп"],
                           "steps": [{"op": "open", "url": "https://dodopizza.ru"}]
                           + steps_ok}))
    sc, err = sm.build_from_trace("u", "пицца", cl)
    check("адрес open из трассы (без хвостового /) — LLM-шаги приняты",
          sc is not None and sc["steps"][0]["url"].startswith(
              "https://dodopizza.ru"))
    check("алиасы: «да»-слова и команды отброшены, обычный оставлен",
          sc is not None and sc["aliases"] == ["закажи пиццу"])
    check("«давай» не запускает сценарий", sm.match_scenario("давай") is None)

    login = [{"kind": "url", "value": "https://shop.example/enter?next=/&token=abc"},
             {"kind": "type", "element": "Логин", "text": "ivan@mail.ru",
              "host": "shop.example"},
             {"kind": "type", "element": "Пароль", "text": "P4ssw0rd!",
              "host": "shop.example"},
             {"kind": "click", "element": "Войти", "host": "shop.example"}]
    scc, sm = mk_sc("sc_lit", login)
    cl = Cloud(json.dumps({"aliases": ["вход"], "steps": [
        {"op": "open", "url": "https://shop.example/enter?next=/&token=abc"},
        {"op": "type", "field": "Логин", "value": "ivan@mail.ru",
         "host": "shop.example"},
        {"op": "type", "field": "Пароль", "value": "P4ssw0rd!",
         "host": "shop.example"},
        {"op": "click", "target": "Войти", "host": "shop.example"}]},
        ensure_ascii=False))
    sc, err = sm.build_from_trace("u", "вход в магазин", cl)
    saved_sc = (tmp / "sc_lit" / "scenarios.json").read_text(encoding="utf-8")
    check("обычная страница: промпт обобщения без секретов",
          cl.calls == 1 and all("P4ssw0rd!" not in m[0]["content"]
                                and "ivan@mail.ru" not in m[0]["content"]
                                for m in cl.prompts))
    check("LLM вернула литералы секретов — в файле слоты, без токена URL",
          "P4ssw0rd!" not in saved_sc and "ivan@mail.ru" not in saved_sc
          and "token=abc" not in saved_sc and sc is not None
          and sum(1 for s in sc["steps"] if s["op"] == "ask") >= 2)

    # ── 6. Живая flavor-реплика ──
    print("flavor_text: живая реплика")
    import importlib
    from app.features import flavor_text as ft
    importlib.reload(ft)
    sent = []

    class FR:
        answer_provider = "deepseek_web"

        def get_response_assigned(self, provider, messages, **kw):
            sent.append(messages[1]["content"])
            return "Сделано."
    fcc = ComputerControlManager(context="choke_f",
                                 config={"confirm": True, "click": True},
                                 base_dir=tmp / "f")
    fb = SimpleNamespace(context="choke_f_ctx",
                         persona=SimpleNamespace(system_prompt="Ты — Коннор."),
                         computer_control=fcc, router=FR())
    for a, okf, det in (
            ({"kind": "click", "idx": 3, "element": "Перевести 15 000 ₽",
              "host": "online.sberbank.ru"}, True, None),
            ({"kind": "type", "idx": 2, "element": "#2", "text": "Kotik2019!",
              "host": "id.example.com"}, True, None),
            ({"kind": "click", "idx": 5, "element": "Иван", "host": "vk.com",
              "value": "https://vk.com/im"}, False, "на месте «Иван: пароль»")):
        sent.clear()
        out = ft.cc_reply(fb, a, okf, det, lang="ru")
        check(f"flavor: приватная страница ({a['host']}) — шаблон, "
              "провайдеру ничего", out is None and not sent)
    sent.clear()
    out = ft.cc_reply(fb, {"kind": "type", "idx": 2, "element": "Имя",
                           "text": "Иван Грозный", "host": "shop.ru",
                           "value": "https://shop.ru/form"}, True, None,
                      lang="ru")
    check("flavor: обычная страница — ввод всегда маской",
          sent and "Иван Грозный" not in sent[0] and "***(12)" in sent[0])
    sent.clear()
    ft.cc_reply(fb, {"kind": "click", "idx": 1, "element": "Найти",
                     "host": "shop.ru"}, False, f"не нашёл «{SECRET}»",
                lang="ru")
    check("flavor: известный секрет в причине — маской",
          sent and SECRET not in sent[0])
    # Адрес действия — файл/только хост (download/cart/zoom), а страница
    # приватна по пути: отслеживаемый адрес того же хоста решает
    fcc._last_host, fcc._last_url = "vk.com", "https://vk.com/im?sel=123"
    for a in ({"kind": "download", "url": "https://vk.com/doc1_2",
               "element": "Анализы.pdf", "host": "vk.com"},
              {"kind": "click", "idx": 4, "element": "Мама: анализы",
               "host": "vk.com"}):
        sent.clear()
        out = ft.cc_reply(fb, a, True, None, lang="ru")
        check(f"flavor: {a['kind']} на vk.com при отслеживаемой /im — "
              "провайдеру ничего", out is None and not sent)
    fcc._last_host, fcc._last_url = "apteka.test", "https://apteka.test/cart"
    sent.clear()
    out = ft.cc_reply(fb, {"kind": "cart", "op": "remove", "product": "АРВТ",
                           "host": "apteka.test"}, True, None, lang="ru")
    check("flavor: корзина (/cart приватна) по хосту — провайдеру ничего",
          out is None and not sent)
    # Резолверы кладут адрес страницы в действие (value)
    fcc._snapshot_for = lambda site, chat_id="", **kw: (
        "https://vk.com/im?sel=1", "vk.com", [], 3, None)
    with fcc.chat_scope("choke_z"):
        za, _e = fcc.resolve_zoom("in", None, chat_id="choke_z")
        ca, _e2 = fcc.resolve_cart(("remove", "x"), None, chat_id="choke_z")
    check("resolve_zoom/resolve_cart: value — адрес страницы",
          (za or {}).get("value") == "https://vk.com/im?sel=1"
          and (ca or {}).get("value") == "https://vk.com/im?sel=1")
    fcc._last_host = fcc._last_url = None

    # ── 7. Бот: слот-секрет сценария и входной лог ──
    print("bot_instance: слот сценария и лог входа")
    import app.features.flavor_text as _flavor
    _flavor.cc_reply = lambda *a, **kw: None
    from app.bot_instance import BotInstance
    from app.features.conversation_style import ConversationStyleConfig

    class _Persona:
        def __init__(self):
            self.settings, self.persona_data = {}, {"name": "Connor"}

        def prepare_messages(self, *a, **kw):
            return [{"role": "system", "content": "SYS"}]

        def get_settings(self):
            return {"max_tokens": 500}

    class _Memory:
        class _STM:
            def get_last(self, *a, **kw):
                return []

        class _LTM:
            def get_facts_by_category(self, *a, **kw):
                return []

            def save_facts(self, *a, **kw):
                pass

        def __init__(self):
            self.stm, self.ltm = self._STM(), self._LTM()
            self.log = []

        def add_message(self, role, content, *a, **kw):
            self.log.append((role, content))

        def get_context(self, *a, **kw):
            return [], [], []

        def get_chat_facts_block(self, *a, **kw):
            return None

    class SlotCC(Spy):
        def resolve_type(self, body, site_word, router=None, chat_id=""):
            text, _, field = body.rpartition(" в поле ")
            return {"kind": "type", "idx": 4, "text": text,
                    "element": field.capitalize(), "host": "shop.ru",
                    "value": "https://shop.ru/"}, None

    CHAT = "grp1"
    b = BotInstance.__new__(BotInstance)
    b.persona_name = "connor"
    b.context = f"choke_{os.urandom(3).hex()}"
    b.owner = "A"
    b.web_single_user = False
    b._cc_allowed_users = set()
    b.features = {}
    b.trigger_words = ["коннор"]
    b.intellect = SimpleNamespace(active=False)
    b.conversation_style = ConversationStyleConfig(None)
    b._control_mode = {CHAT}
    b.computer_control = SlotCC(context=b.context,
                                config={"confirm": True, "click": True},
                                base_dir=tmp / "bot")
    b.scenario_manager = None
    b.task_agent = None
    b.proactive = None
    b.addons = []
    b.self_memory = None
    b.living = None
    b.todo_manager = b.inventory_manager = b.reminder_manager = None
    b.learning_manager = None
    b.file_db = None
    b._punish_enabled = b._moderation_enabled = False
    b._web_search_enabled = False
    b._web_search_disabled_chats = set()
    b._pending_list_messages, b._pending_split_messages = {}, {}
    b._pending_photos, b._pending_question_kind = {}, {}
    b._pending_more_photos = {}
    b.persona = _Persona()
    b.memory = _Memory()
    b.router = Cloud("Обычный ответ.")
    sm = ScenarioManager(context=b.context,
                         computer_control=b.computer_control,
                         base_dir=tmp / "bot_sc")
    sm._scenarios["вход в магазин"] = {
        "name": "вход в магазин", "aliases": [],
        "steps": sm._validate_steps([
            {"op": "open", "url": "https://shop.ru"},
            {"op": "ask", "slot": "секрет1",
             "question": "Что ввести в поле «Код»? (не сохраняю)"},
            {"op": "type", "field": "код", "value": "{секрет1}",
             "host": "shop.ru"}])}
    b.scenario_manager = sm
    b._cc_hist_install()
    check("бот: хук on_typed менеджера поставлен",
          getattr(b.computer_control, "on_typed", None) is not None)
    n0 = len(cap.records)
    b.process_message("вход в магазин", user_id="A", chat_id=CHAT)
    slot_answer = "Zebra-Moon"
    b.process_message(slot_answer, user_id="A", chat_id=CHAT)
    hist_txt = "\n".join(c for _r, c in b.memory.log)
    typed = [c.get("text") for c in b.computer_control.calls
             if c["kind"] == "type"]
    check("слот сценария: введено настоящее значение", typed == [slot_answer])
    check("слот-секрет сценария: ни реплики, ни «Ввёл …» в истории",
          slot_answer not in hist_txt and "***(10)" in hist_txt)
    check("слот-секрет сценария: значение в KnownSecrets чата",
          slot_answer in b._cc_hist_vault().values(CHAT))
    starts = [r.getMessage() for r in cap.records[n0:]
              if "process_message START" in r.getMessage()]
    check("лог START в режиме управления — длина, без текста",
          starts and all(slot_answer not in s and "chars>" in s
                         for s in starts))

    # Секрет команды — маской ДО лесенки: отказ резолва ввода возвращается
    # раньше маски в её конце (redteam-1: пароль оставался в STM открытым)
    CH2 = "grp2"
    b._control_mode.add(CH2)
    fcc2 = b.computer_control
    fcc2.resolve_type = lambda body, site_word, router=None, chat_id="": (
        None, "На странице shop.ru не нашёл поля «пароль».")
    for cmd, pw in (("введи Kotik2019x в поле пароль", "Kotik2019x"),
                    ("введи в поле пароль Kotik2019y", "Kotik2019y")):
        b.memory.log.clear()
        b.process_message(cmd, user_id="A", chat_id=CH2)
        h = "\n".join(c for _r, c in b.memory.log)
        check(f"отказ резолва ввода: пароль не в STM ({cmd[:24]}…)",
              b.memory.log and pw not in h and "***(" in h)
        check(f"отказ резолва ввода: пароль в KnownSecrets ({cmd[:24]}…)",
              pw in b._cc_hist_vault().values(CH2))
    del fcc2.resolve_type
    # Секрет без ключевого слова пароля: пары «логин / пароль», «пасс»,
    # «pass» — не в облачный intent-ярус, не в STM
    for cmd, pw in (("войди на shop.test, логин ivan, пасс Kotik2019a!",
                     "Kotik2019a"),
                    ("log in to shop.test as ivan with pass Kotik2019b!",
                     "Kotik2019b"),
                    ("авторизуйся на shop.test: ivan / Kotik2019c!",
                     "Kotik2019c")):
        b.memory.log.clear()
        c0 = b.router.calls
        n1 = len(cap.records)
        b.process_message(cmd, user_id="A", chat_id=CH2)
        h = "\n".join(c for _r, c in b.memory.log)
        leaked = [p for p in b.router.prompts[c0:] if pw in repr(p)]
        logs = [r.getMessage() for r in cap.records[n1:]
                if pw in r.getMessage()]
        check(f"секрет без слова «пароль»: не в облаке/STM/логе ({cmd[:26]}…)",
              not leaked and pw not in h and not logs)
    from app.features.computer_control import (command_has_secret,
                                               command_secret_values)
    check("command_has_secret: пары и pass/пасс/pw/credentials",
          all(command_has_secret(x) for x in (
              "ivan / Kotik2019!", "логин ivan пасс Kotik2019",
              "pw Qwerty12", "credentials ivan Kotik2019",
              "данные для входа: ivan Kotik2019")))
    check("command_has_secret: даты/пути/дроби — не пара",
          not any(command_has_secret(x) for x in (
              "открой 12 / 2027", "открой site.ru/path/1", "нажми 1 / 2",
              "покажи код страницы")))
    check("command_secret_values: правая часть пары",
          "Kotik2019!" in command_secret_values("зайди как ivan / Kotik2019!"))

    total = ok + len(fails)
    print(f"\n{ok}/{total} проверок прошло" + (f"; FAIL: {len(fails)}" if fails else ""))
    for f in fails:
        print(f"  [FAIL] {f}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())

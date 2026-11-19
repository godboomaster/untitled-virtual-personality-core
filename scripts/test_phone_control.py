"""Управление экраном телефона (app/features/phone_control.py, ход в
BotInstance, служба PhoneControlService на Java).

Проверяет:
  A. разбор фраз: RU/EN-команды с явным обращением к телефону, вежливая
     форма, обращение к персоне; без «на телефоне», отрицания, вопросы,
     «напиши Каре…» (цель для агента), будильник/фонарик — не команды;
  B. поиск элемента: точная подпись, часть, опечатка, текст внутри строки
     жмёт строку, одинаковые подписи — вопрос, поля — по подсказке;
  C. приватность: банки/пароли/коды — по пакету и имени; заметка «что на
     экране» — без кодов из уведомлений и без текста паролей;
  D. ход через поддельный телефон (настоящий реестр app/core/devices, телефон
     в потоке берёт команды и отвечает): нажатие, «да/нет» на необратимое,
     оплата не нажимается, приватное приложение не трогается, выбор номером,
     ввод в поле (по имени, в фокусе, пароль после «да»), листание и «дальше
     некуда», назад/домой, открыть приложение (выбор, не найдено), экран
     выключен/заблокирован, элемент пропал, телефон не настроен/не на связи/
     не ответил, срок вопроса, чужой ответ, «да» режиму управления компьютером;
  E. полный ход process_message: владелец — ответ без модели и в истории;
     не владелец, группа, скин, ход команды телефону — обычный ответ без
     команд телефону; «что на экране» — заметка персоне, без веб-поиска;
     наш вопрос гасит ожидающее «да» режима управления компьютером;
  F. веб и Telegram: «экран телефона» — не снимок экрана компьютера;
  G. контракт Java ↔ Python: возможности, коды отказов, служба в манифесте.

Браузеры, модели и телефон подменены.
Запуск: python -m scripts.test_phone_control
"""

import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="phone_control_")
os.environ["VPC_DATA_DIR"] = os.environ["DATA_DIR"]
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

ok = 0
fail = 0


def check(name, cond, detail=""):
    global ok, fail
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if cond:
        ok += 1
    else:
        fail += 1


def section(title):
    print(f"\n{title}")


from app.core import devices  # noqa: E402
from app.features import phone_control as pc  # noqa: E402

pc.REREAD_DELAY_SEC = 0.05

CAPS = ["screen.read", "screen.tap", "screen.type", "screen.scroll", "screen.global",
        "apps.list", "app.open"]
NAMES = ["Коннор", "connor"]


def el(i, k, t="", **kw):
    e = {"i": i, "k": k, "t": t, "b": kw.pop("b", [0, 100 * i, 1080, 100 * i + 90])}
    if k in ("button", "item"):
        e["c"] = 1
    if k == "field":
        e["ed"] = 1
        e["c"] = 1
    e.update(kw)
    return e


def telegram():
    return {"snap": 1, "pkg": "org.telegram.messenger", "app": "Telegram", "w": 1080, "h": 2400,
            "elements": [
                el(0, "button", "Назад", b=[0, 100, 120, 220]),
                el(1, "text", "Кара", b=[150, 100, 600, 160]),
                el(2, "field", "", h="Сообщение", f=1, b=[100, 2200, 900, 2300]),
                el(3, "button", "Отправить", b=[920, 2200, 1060, 2300]),
                el(4, "item", "Хэнк · привет", b=[0, 400, 1080, 560]),
                el(5, "text", "Хэнк", p=4, b=[150, 420, 500, 470]),
                el(6, "text", "привет", p=4, b=[150, 480, 500, 540]),
                {"i": 7, "k": "list", "t": "", "sc": 1, "b": [0, 300, 1080, 2150]},
            ]}


def chats():
    return {"snap": 2, "pkg": "org.telegram.messenger", "app": "Telegram", "w": 1080, "h": 2400,
            "elements": [
                el(0, "item", "Кара · буду в 8", b=[0, 300, 1080, 450]),
                el(1, "text", "Кара", p=0, b=[150, 310, 500, 360]),
                el(2, "item", "Кара Ч. · ок", b=[0, 1800, 1080, 1950]),
                el(3, "text", "Кара", p=2, b=[150, 1810, 500, 1860]),
                el(4, "button", "Поиск", b=[900, 100, 1060, 200]),
            ]}


def shop():
    return {"snap": 3, "pkg": "ru.ozon.app.android", "app": "Ozon", "w": 1080, "h": 2400,
            "elements": [el(0, "button", "Оплатить 1 299 ₽"), el(1, "button", "Удалить из корзины"),
                         el(2, "button", "Купить"), el(3, "button", "В корзину", off=1)]}


def bank():
    return {"snap": 4, "pkg": "ru.sberbankmobile", "app": "СберБанк", "w": 1080, "h": 2400,
            "elements": [el(0, "button", "Перевести"), el(1, "text", "Баланс 12 000 ₽")]}


def login():
    return {"snap": 5, "pkg": "com.example.app", "app": "Example", "w": 1080, "h": 2400,
            "elements": [el(0, "field", "", h="Логин"), el(1, "field", "", h="Пароль", pw=1),
                         el(2, "button", "Войти")]}


def shade():
    return {"snap": 6, "pkg": "com.android.systemui", "app": "System UI", "w": 1080, "h": 2400,
            "elements": [el(0, "text", "Банк: Ваш код: 4821. Никому не сообщайте"),
                         el(1, "text", "Your verification code is 912 334"),
                         el(2, "text", "Кара: буду в 8"),
                         el(3, "switch", "Wi-Fi", on=1)]}


class FakePhone:
    """Телефон в потоке: берёт команды из очереди реестра, как PhoneControlLink."""

    def __init__(self, screen=None, dev_id="phone-test", apps=None):
        self.id = dev_id
        self.screen = screen if screen is not None else telegram()
        self.cmds = []
        self.after_tap = None    # args → новый экран
        self.fail = {}           # action → «код: текст»
        self.silent = set()      # action → не отвечать (таймаут)
        self.scroll_moves = True
        self.apps = apps if apps is not None else [
            {"label": "Telegram", "pkg": "org.telegram.messenger"},
            {"label": "YouTube", "pkg": "com.google.android.youtube"},
            {"label": "Заметки", "pkg": "com.example.notes"},
            {"label": "Заметки", "pkg": "com.samsung.notes"},
            {"label": "Заметки Pro", "pkg": "com.example.notespro"},
        ]
        self.stop = False
        devices.register(dev_id, "Test Phone", ["телефон", "phone"], CAPS)
        self.t = threading.Thread(target=self.loop, daemon=True)
        self.t.start()

    def loop(self):
        while not self.stop:
            devices._note(self.id, waiting=True)
            for c in devices._take(self.id):
                self.cmds.append((c["action"], dict(c["args"])))
                if c["action"] in self.silent:
                    continue
                if c["action"] in self.fail:
                    devices.complete(self.id, c["id"], False, error=self.fail[c["action"]])
                    continue
                devices.complete(self.id, c["id"], True, data=self.handle(c["action"], c["args"]))
            time.sleep(0.003)

    def handle(self, action, args):
        if action == "screen.read":
            return {"screen": self.screen}
        if action == "apps.list":
            return {"apps": self.apps}
        if action == "screen.tap":
            if self.after_tap:
                self.screen = self.after_tap(args)
            return {"method": "click", "screen": self.screen}
        if action == "screen.type":
            for e in self.screen["elements"]:
                if e.get("i") == args.get("i"):
                    e["t"] = args.get("text")
            return {"method": "set_text", "screen": self.screen}
        if action == "screen.scroll":
            if self.scroll_moves:
                self.screen = dict(self.screen, elements=list(reversed(self.screen["elements"])))
            return {"method": "scroll", "screen": self.screen}
        if action == "app.open":
            self.screen = dict(telegram(), pkg=args["pkg"], app=args["pkg"])
            return {"method": "launch", "screen": self.screen}
        return {"method": args.get("name"), "screen": self.screen}

    def actions(self):
        return [a for a, _ in self.cmds]

    def close(self):
        self.stop = True
        self.t.join(1)
        devices.forget(self.id)


def reset_registry():
    with devices._lock:
        devices._devices = {}
        devices._seen.clear()
        devices._queues.clear()
        devices._cmds.clear()
    devices._save()
    pc._apps_cache.clear()
    with pc._pending._lock:
        pc._pending._by_chat.clear()


def turn(text, key="k1", user="A", lang=None, foreign=False):
    return pc.turn(text, key, user, lang, names=NAMES, foreign_pending=foreign)


# ── A ──────────────────────────────────────────────────────────────

def test_parse():
    section("A. разбор фраз")
    cases = {
        "на телефоне нажми «Отправить»": {"kind": "tap", "target": "Отправить"},
        "нажми на телефоне кнопку Отправить": {"kind": "tap", "target": "Отправить"},
        "Коннор, на телефоне нажми на Кару": {"kind": "tap", "target": "Кару"},
        "на моём телефоне тапни Хэнк": {"kind": "tap", "target": "Хэнк"},
        "можешь на телефоне нажать «Далее»?": {"kind": "tap", "target": "Далее"},
        "на телефоне нажми назад": {"kind": "back"},
        "телефон, назад": {"kind": "back"},
        "можешь на телефоне нажать назад?": {"kind": "back"},
        "на телефоне вернись назад": {"kind": "back"},
        "на телефоне домой": {"kind": "home"},
        "выйди на телефоне на главный экран": {"kind": "home"},
        "на телефоне недавние приложения": {"kind": "recents"},
        "на телефоне открой шторку": {"kind": "notifications"},
        "покажи на телефоне уведомления": {"kind": "notifications"},
        "на телефоне быстрые настройки": {"kind": "quick_settings"},
        "на телефоне листай вниз": {"kind": "scroll", "dir": "down"},
        "пролистай на телефоне вверх": {"kind": "scroll", "dir": "up"},
        "на телефоне листай": {"kind": "scroll", "dir": "down"},
        "телефон, ниже": {"kind": "scroll", "dir": "down"},
        "на телефоне прокрути ленту дальше": {"kind": "scroll", "dir": "down"},
        "что на экране телефона?": {"kind": "read"},
        "что у меня на телефоне открыто?": {"kind": "read"},
        "прочитай экран телефона": {"kind": "read"},
        "Коннор, что сейчас на экране телефона?": {"kind": "read"},
        "посмотри на телефоне, что там": {"kind": "read"},
        "опиши экран телефона": {"kind": "read"},
        "на телефоне введи привет в поле поиска": {"kind": "type", "text": "привет", "field": "поиск"},
        "введи на телефоне «котики» в поиск": {"kind": "type", "text": "котики", "field": "поиск"},
        "на телефоне напиши «буду в 8»": {"kind": "type", "text": "буду в 8", "field": ""},
        "на телефоне введи ghost в поле логин": {"kind": "type", "text": "ghost", "field": "логин"},
        "открой на телефоне телеграм": {"kind": "open", "app": "телеграм"},
        "на телефоне запусти YouTube": {"kind": "open", "app": "YouTube"},
        "tap Send on my phone": {"kind": "tap", "target": "Send"},
        "on my phone, tap the Next button": {"kind": "tap", "target": "Next"},
        "could you tap Back on my phone?": {"kind": "back"},
        "phone, go back": {"kind": "back"},
        "on my phone go home": {"kind": "home"},
        "on my phone, scroll down": {"kind": "scroll", "dir": "down"},
        "scroll up on my phone": {"kind": "scroll", "dir": "up"},
        "what's on my phone screen?": {"kind": "read"},
        "read the screen on my phone": {"kind": "read"},
        "open Telegram on my phone": {"kind": "open", "app": "Telegram"},
        "type hello into the search field on my phone": {"kind": "type", "text": "hello",
                                                          "field": "поиск"},
        "on my phone open the notifications": {"kind": "notifications"},
    }
    for text, want in cases.items():
        got = pc.parse(text, NAMES)
        check(f"«{text}» → {want}", got == want, f"получили {got}")
    for text in ("нажми «Отправить»", "у меня в телефоне сел аккумулятор",
                 "не нажимай на телефоне ничего", "на телефоне нажать назад?",
                 "напиши Каре на телефоне что опоздаю", "на компьютере нажми назад",
                 "на телефоне включи фонарик", "что на экране?", "почему на телефоне всё тормозит?",
                 "открой на телефоне будильник", "что на телефоне нового?", "don't tap anything on my phone",
                 "what's up on my phone?", "телефон сломался", "я на телефоне нажал не то",
                 "листай вниз", "назад", "", "на телефоне " + "а" * 400):
        check(f"«{text[:50]}» — не команда", pc.parse(text, NAMES) is None)
    check("mentions_phone: «на телефоне», «экран телефона», «on my phone»",
          pc.mentions_phone("на телефоне") and pc.mentions_phone("экран телефона")
          and pc.mentions_phone("on my phone") and not pc.mentions_phone("телефонный звонок"))


# ── B ──────────────────────────────────────────────────────────────

def test_match():
    section("B. поиск элемента")
    s = telegram()
    m = pc.match(s, "Отправить")
    check("точная подпись — один кандидат", len(m) == 1 and m[0]["target"]["i"] == 3)
    m = pc.match(s, "хэнк")
    check("текст внутри строки — жмём строку (p)", len(m) == 1 and m[0]["target"]["i"] == 4)
    m = pc.match(s, "Отправ")
    check("начало слова — тот же элемент", len(m) == 1 and m[0]["target"]["i"] == 3)
    m = pc.match(s, "Хэнка")
    check("другой падеж («нажми на Хэнка») — та же строка", len(m) == 1 and m[0]["target"]["i"] == 4)
    m = pc.match(s, "Отправтиь")
    check("опечатка — тот же элемент", len(m) == 1 and m[0]["target"]["i"] == 3)
    check("нет такого — пусто", pc.match(s, "Удалить") == [])
    m = pc.match(chats(), "Кара")
    check("две строки «Кара» — два кандидата (по строкам, без дублей текста)",
          len(m) == 2 and {c["target"]["i"] for c in m} == {0, 2})
    bar = {"snap": 8, "pkg": "com.android.settings", "app": "Настройки", "h": 2400, "elements": [
        el(0, "item", "Поиск", b=[40, 200, 1040, 330]),
        el(1, "button", "Поиск", b=[60, 220, 160, 310]),
        el(2, "item", "Поиск", b=[160, 210, 1020, 320]),
        el(3, "field", "", h="Поиск", b=[170, 215, 1000, 315]),
        el(4, "button", "Поиск", b=[60, 2000, 300, 2100])]}
    m = pc.match(bar, "поиск")
    check("вложенные «Поиск» (Honor: контейнер, кнопка, поле) — один элемент, поле; "
          "отдельная кнопка внизу — второй", len(m) == 2
          and {c["target"]["i"] for c in m} == {3, 4}, str([c["target"]["i"] for c in m]))
    # Telegram (Honor, 30.10): в поиск уже введено «РабИнфСем», ниже — найденный чат;
    # строки — без текстов внутри, подпись дал зонд (описание из события доступности)
    tg = {"snap": 9, "pkg": "org.telegram.messenger", "app": "Telegram", "h": 2664, "elements": [
        el(0, "field", "РабИнфСем", h="Search Chats", f=1, b=[180, 130, 900, 300]),
        el(1, "item", "Группа. РабИнфСем (РИС) Ьl (Сообщество). 8 участников", b=[0, 400, 1200, 600]),
        el(2, "item", "Тема. General. Кара: буду в 8", b=[0, 600, 1200, 800]),
        el(3, "item", "Тема. Расписание. 2 новых сообщения", b=[0, 800, 1200, 1000])]}
    m = pc.match(tg, "рабинфсем")
    check("набранное в поле не перебивает чат: «нажми рабинфсем» — строка чата",
          len(m) == 1 and m[0]["target"]["i"] == 1, str([c["target"]["i"] for c in m]))
    m = pc.match(tg, "general")
    check("тема по подписи из события доступности («Тема. General. …»)",
          len(m) == 1 and m[0]["target"]["i"] == 2)
    m = pc.match(tg, "search chats")
    check("поле по названию находится", len(m) == 1 and m[0]["target"]["i"] == 0)
    note = pc.read_note(tg)
    check("«что на экране»: строки без текстов внутри — в заметке ([row])",
          "[row] Тема. General" in note and "[row] Группа. РабИнфСем" in note, note)
    m = pc.match(s, "сообщение", fields_only=True)
    check("поле — по подсказке", len(m) == 1 and m[0]["target"]["i"] == 2)
    check("подпись поля — подсказка, а не пустое содержимое", pc._label(s["elements"][2]) == "Сообщение")
    check("место: вверху / внизу", pc._place(s, s["elements"][1]) == "top"
          and pc._place(s, s["elements"][3]) == "bottom")


# ── C ──────────────────────────────────────────────────────────────

def test_privacy():
    section("C. приватность")
    for pkg, app in (("ru.sberbankmobile", "СберБанк"), ("com.idamob.tinkoff.android", "Т-Банк"),
                     ("com.x8bit.bitwarden", "Bitwarden"),
                     ("com.google.android.apps.authenticator2", "Authenticator"),
                     ("ru.rostel", "Госуслуги"), ("com.google.android.apps.walletnfcrel", "Wallet"),
                     ("ru.vtb24.mobilebanking.android", "ВТБ Онлайн")):
        check(f"{app} — приватное", pc.is_private({"pkg": pkg, "app": app}))
    for pkg, app in (("org.telegram.messenger", "Telegram"), ("com.google.android.youtube", "YouTube"),
                     ("ru.ozon.app.android", "Ozon"), ("com.android.settings", "Настройки")):
        check(f"{app} — не приватное", not pc.is_private({"pkg": pkg, "app": app}))
    note = pc.read_note(shade())
    check("заметка: код из уведомления скрыт", "4821" not in note and "912 334" not in note
          and "[hidden]" in note)
    check("заметка: обычный текст и переключатель есть", "Кара: буду в 8" in note
          and "[switch on] Wi-Fi" in note)
    lg = login()
    lg["elements"][1]["t"] = "секрет"
    note = pc.read_note(lg)
    check("заметка: текст пароля не попадает", "секрет" not in note and "password field" in note)
    tiles = {"snap": 7, "pkg": "com.android.systemui", "app": "com.android.systemui", "elements": [
        el(0, "switch", "Internet, AndroidWifi", on=0, c=1),
        el(1, "text", "Internet", p=0), el(2, "text", "AndroidWifi", p=0),
        el(3, "text", "Кара: буду в 8")]}
    check("где: «в Telegram», «в шторке уведомлений», «на главном экране», EN",
          pc._where(telegram(), "ru") == "в Telegram" and pc._where(tiles, "ru") == "в шторке уведомлений"
          and pc._where({"home": True, "pkg": "x"}, "ru") == "на главном экране"
          and pc._where(tiles, "en") == "in the notification shade")
    note = pc.read_note(tiles)
    check("шторка: подпись плитки не дублируется её текстами, имя — шторка",
          note.count("Internet") == 1 and "AndroidWifi" in note
          and "App on screen: the notification shade" in note, note)
    note = pc.read_note(telegram())
    check("заметка: строки списка не дублируются, поле пустое помечено",
          note.count("Хэнк") == 1 and "[text field \"Сообщение\": empty]" in note
          and "App on screen: Telegram" in note)


# ── D ──────────────────────────────────────────────────────────────

def test_turns():
    section("D. ход через поддельный телефон")
    reset_registry()
    r = turn("на телефоне нажми назад")
    check("телефона нет — подсказка, где включить", "не включено" in r["reply"]
          and "спец. возможности" in r["reply"])
    check("не команда — None", turn("привет, как дела?") is None)

    ph = FakePhone()
    try:
        r = turn("на телефоне нажми «Отправить»")
        check("необратимое — сначала вопрос, нажатия нет",
              r.get("confirm") and "Нажать «Отправить» в Telegram" in r["reply"]
              and "screen.tap" not in ph.actions())
        r = turn("да")
        taps = [a for a in ph.cmds if a[0] == "screen.tap"]
        check("«да» — нажато, ответ «Нажал»", r["reply"].startswith("Нажал «Отправить»")
              and len(taps) == 1 and taps[0][1]["i"] == 3 and taps[0][1]["t"] == "Отправить"
              and taps[0][1]["snap"] == 1)
        check("после «да» вопрос снят: второе «да» — не наше", turn("да") is None)

        ph.cmds.clear()
        turn("на телефоне нажми «Отправить»")
        r = turn("нет")
        check("«нет» — не нажато", r["reply"] == "Хорошо, не нажимаю." and "screen.tap" not in ph.actions())
        turn("на телефоне нажми «Отправить»")
        check("другая реплика — вопрос снят, реплика не наша", turn("а какая погода?") is None)
        check("…и «да» после неё уже не нажимает", turn("да") is None and "screen.tap" not in ph.actions())

        turn("на телефоне нажми «Отправить»", user="A")
        check("«да» другого участника — не ответ", turn("да", user="B") is None)
        r = turn("да", user="A")
        check("…а автора — ответ", r and r["reply"].startswith("Нажал"))

        turn("на телефоне нажми «Отправить»")
        check("ждёт «да» режим управления компьютером — наше «да» не берём",
              turn("да", foreign=True) is None)

        ph.cmds.clear()
        turn("на телефоне нажми «Отправить»")
        pc._pending._by_chat["k1"]["at"] -= pc.PENDING_TTL_SEC + 1
        check("срок вопроса вышел — «да» не нажимает", turn("да") is None and "screen.tap" not in ph.actions())

        ph.cmds.clear()
        r = turn("на телефоне нажми Хэнк")
        tap = [a for a in ph.cmds if a[0] == "screen.tap"]
        check("обычное — сразу, жмётся строка", r["reply"] == "Нажал «Хэнк»."
              and tap and tap[0][1]["i"] == 4)

        ph.cmds.clear()
        r = turn("на телефоне нажми Удалить")
        check("нет на экране — честно и что видно", "нет «Удалить»" in r["reply"]
              and "«Отправить»" in r["reply"])
        check("…экран перечитан один раз (переход мог ещё идти)",
              ph.actions().count("screen.read") == 2)

        # Экран ещё открывался: на первом снимке кнопки нет, на втором — есть
        later = telegram()
        ph.screen = dict(telegram(), elements=telegram()["elements"][:3])
        reads = {"n": 0}
        old_handle = ph.handle

        def handle_late(action, args):
            if action == "screen.read":
                reads["n"] += 1
                if reads["n"] == 2:
                    ph.screen = later
            return old_handle(action, args)
        ph.handle = handle_late
        r = turn("на телефоне нажми Хэнк")
        ph.handle = old_handle
        check("кнопка появилась при повторном чтении — нажата", r["reply"] == "Нажал «Хэнк».")
        ph.screen = dict(telegram(), elements=[e for e in telegram()["elements"] if not e.get("ed")])
        reads["n"] = 0
        later = telegram()
        ph.handle = handle_late
        r = turn("на телефоне введи «привет»")
        ph.handle = old_handle
        check("поле появилось при повторном чтении — введено", r["reply"].startswith("Ввёл «привет»"))

        ph.screen = chats()
        ph.cmds.clear()
        r = turn("на телефоне нажми Кара")
        check("две «Кара» — вопрос с номерами", "какое нажать" in r["reply"]
              and "1. «Кара · буду в 8» — строка, вверху" in r["reply"]
              and "2. «Кара Ч. · ок» — строка, внизу" in r["reply"] and "screen.tap" not in ph.actions())
        r = turn("7")
        check("номер вне списка — переспрос", r["reply"] == "Назовите номер от 1 до 2.")
        r = turn("второй")
        tap = [a for a in ph.cmds if a[0] == "screen.tap"]
        check("«второй» — нажата вторая строка", r["reply"].startswith("Нажал «Кара»")
              and tap and tap[0][1]["i"] == 2)

        ph.screen = shop()
        ph.cmds.clear()
        r = turn("на телефоне нажми оплатить")
        check("оплата — не нажимается и не спрашивается", "похоже на оплату" in r["reply"]
              and "screen.tap" not in ph.actions() and not pc.has_pending("k1"))
        r = turn("на телефоне нажми купить")
        check("голое «Купить» на телефоне — тоже оплата", "похоже на оплату" in r["reply"]
              and "screen.tap" not in ph.actions())
        r = turn("на телефоне нажми удалить из корзины")
        check("удаление — с вопросом", r.get("confirm") and "screen.tap" not in ph.actions())
        turn("нет")
        r = turn("на телефоне нажми в корзину")
        check("неактивная кнопка — честно", "неактивна" in r["reply"] and "screen.tap" not in ph.actions())

        ph.screen = bank()
        ph.cmds.clear()
        r = turn("на телефоне нажми Перевести")
        check("банк — не нажимается", "не читаю и не нажимаю" in r["reply"]
              and "screen.tap" not in ph.actions())
        r = turn("что на экране телефона?")
        check("банк — не читается (ни заметки, ни текста)", "reply" in r and "12 000" not in r["reply"])
        r = turn("на телефоне назад")
        check("банк — «назад» работает", r["reply"].startswith("Нажал «Назад»")
              and ("screen.global", {"name": "back"}) in ph.cmds)

        ph.screen = telegram()
        ph.cmds.clear()
        r = turn("на телефоне введи «буду в 8»")
        typ = [a for a in ph.cmds if a[0] == "screen.type"]
        check("ввод без поля — в поле с фокусом", r["reply"] == "Ввёл «буду в 8» в поле «Сообщение»."
              and typ and typ[0][1]["i"] == 2 and typ[0][1]["text"] == "буду в 8")

        ph.screen = login()
        ph.cmds.clear()
        r = turn("на телефоне введи ghost в поле логин")
        check("ввод в поле по имени", r["reply"] == "Ввёл «ghost» в поле «Логин»."
              and [a for a in ph.cmds if a[0] == "screen.type"][0][1]["i"] == 0)
        r = turn("на телефоне введи «hunter2» в поле пароль")
        check("пароль — сначала вопрос", r.get("confirm") and "поле пароля" in r["reply"]
              and len([a for a in ph.cmds if a[0] == "screen.type"]) == 1)
        r = turn("да")
        check("«да» — введено, пароль в ответе не повторяется",
              r["reply"] == "Ввёл пароль в поле «Пароль»." and "hunter2" not in r["reply"]
              and [a for a in ph.cmds if a[0] == "screen.type"][-1][1]["i"] == 1)
        ph.screen = login()
        ph.screen["elements"] = [e for e in ph.screen["elements"] if not e.get("f")]
        r = turn("на телефоне введи ghost")
        check("два поля без фокуса — вопрос, в какое", "В какое поле" in r["reply"])
        r = turn("1")
        check("…«1» — в первое", r["reply"] == "Ввёл «ghost» в поле «Логин».")
        r = turn("на телефоне введи ghost в поле адрес")
        check("нет такого поля — честно", "нет поля «адрес»" in r["reply"])
        ph.screen = bank()
        ph.screen["pkg"], ph.screen["app"] = "com.example", "Example"
        r = turn("на телефоне введи ghost")
        check("полей нет — честно", r["reply"] == "На экране телефона нет поля ввода.")

        ph.screen = telegram()
        r = turn("на телефоне листай вниз")
        check("листание", r["reply"] == "Пролистал вниз."
              and ("screen.scroll", {"dir": "down"}) in ph.cmds)
        ph.scroll_moves = False
        r = turn("на телефоне листай вверх")
        check("экран не сдвинулся — «дальше некуда»", "дальше некуда" in r["reply"])

        r = turn("телефон, домой")
        check("домой", r["reply"] == "Вышел на главный экран.")
        ph.screen = {"snap": 20, "pkg": "com.google.android.apps.nexuslauncher",
                     "app": "com.google.android.apps.nexuslauncher", "home": True,
                     "elements": [el(0, "button", "Chrome")]}
        r = turn("телефон, назад")
        check("рабочий стол называется рабочим столом, а не пакетом",
              r["reply"] == "Нажал «Назад». Теперь на экране: главный экран.", r["reply"])
        r = turn("на телефоне нажми Telegram")
        check("на рабочем столе нет — «на экране телефона (главный экран)»",
              "(главный экран)" in r["reply"], r["reply"])
        r = turn("on my phone, open the notifications", lang="en")
        check("шторка по-английски", r["reply"] == "Opened the notification shade.")

        ph.cmds.clear()
        r = turn("открой на телефоне телеграм")
        check("открыть приложение по имени (кириллицей)", r["reply"] == "Открыл Telegram на телефоне.",
              r["reply"])
        r = turn("открой на телефоне telegram")
        opens = [a for a in ph.cmds if a[0] == "app.open"]
        check("латиницей — открыт Telegram, список приложений один раз (кэш)",
              opens and opens[-1][1]["pkg"] == "org.telegram.messenger"
              and ph.actions().count("apps.list") == 1)
        r = turn("открой на телефоне заметки")
        check("две «Заметки» — вопрос, различаются пакетом", "какое открыть" in r["reply"]
              and "2. Заметки (com.samsung.notes)" in r["reply"], r["reply"])
        r = turn("2")
        check("…«2» — вторые", r["reply"] == "Открыл Заметки на телефоне."
              and [a for a in ph.cmds if a[0] == "app.open"][-1][1]["pkg"] == "com.samsung.notes")
        r = turn("открой на телефоне заметки про")
        check("«заметки про» — Заметки Pro", r["reply"] == "Открыл Заметки Pro на телефоне.")
        r = turn("открой на телефоне фотошоп")
        check("нет приложения — честно", "не нашёл" in r["reply"])

        ph.screen = {"snap": 9, "screen_off": True, "elements": []}
        r = turn("на телефоне нажми Хэнк")
        check("экран выключен", r["reply"].startswith("Экран телефона выключен"))
        ph.screen = dict(telegram(), locked=True)
        r = turn("что на экране телефона?")
        check("заблокирован", r["reply"].startswith("Телефон заблокирован"))

        ph.screen = telegram()
        ph.fail["screen.tap"] = "gone: элемента больше нет на экране"
        r = turn("на телефоне нажми Хэнк")
        check("элемент пропал — понятный ответ", "экран телефона сменился" in r["reply"])
        ph.fail["screen.tap"] = "failed: нажатие не прошло"
        r = turn("на телефоне нажми Хэнк")
        check("сбой — текст телефона в ответе", "нажатие не прошло" in r["reply"])
        ph.fail.clear()

        r = turn("что на экране телефона?")
        check("«что на экране» — заметка персоне", "note" in r and "Telegram" in r["note"]
              and "[button] Отправить" in r["note"])

        ph.cmds.clear()
        for goal in ("на телефоне напиши Каре что опоздаю", "закажи на телефоне пиццу",
                     "Коннор, на телефоне поставь будильник на 7", "on my phone, text Kara I'm late"):
            r = turn(goal)
            check(f"цель «{goal}» — честная заметка, телефону ничего",
                  r == {"note": pc.UNSUPPORTED_NOTE} and not ph.cmds)
        for other in ("у меня в телефоне сел аккумулятор", "на телефоне поставь будильник?",
                      "не пиши ничего на телефоне"):
            check(f"«{other}» — не цель", turn(other) is None)

        ph.silent.add("screen.read")
        t0 = time.time()
        old = pc.READ_TIMEOUT_SEC
        pc.READ_TIMEOUT_SEC = 0.3
        r = turn("на телефоне нажми Хэнк")
        pc.READ_TIMEOUT_SEC = old
        check("телефон молчит — «не ответил вовремя»", r["reply"].startswith("Телефон не ответил")
              and time.time() - t0 < 3)
        ph.silent.clear()
    finally:
        ph.close()

    reset_registry()
    devices.register("phone-off", "Honor", ["телефон"], CAPS)
    with devices._lock:
        devices._seen["phone-off"] = {"seen": time.time() - 3600, "waiting": False}
    r = turn("на телефоне назад")
    check("телефон есть, но не на связи — имя и что проверить", "«Honor» сейчас не на связи" in r["reply"])
    reset_registry()

    # Служба выключена — телефон сам говорит «ухожу», ядро не ждёт таймаута
    ph = FakePhone()
    try:
        ph.stop = True
        ph.t.join(1)
        t0 = time.time()
        check("leave: известное устройство — True", devices.leave(ph.id))
        r = turn("на телефоне назад")
        check("после «ухожу» — сразу «не на связи», без ожидания",
              "не на связи" in r["reply"] and time.time() - t0 < 1)
        check("leave: неизвестное — False", devices.leave("nope") is False)
    finally:
        ph.close()
        reset_registry()


# ── E ──────────────────────────────────────────────────────────────

def make_bot(owner="A", features=None, web_single=False):
    from app.bot_instance import BotInstance
    from app.features.computer_control import ComputerControlManager
    from app.features.conversation_style import ConversationStyleConfig
    from app.features.list_offers import ListOffers
    import app.core.router as _net_router
    _net_router.internet_available = lambda: True
    _net_router._net_ok, _net_router._net_checked = True, float("inf")

    class _Persona:
        def __init__(self):
            self.settings, self.persona_data = {}, {"name": "Connor"}
            self.addons = []

        def prepare_messages(self, *a, **kw):
            self.addons = list(kw.get("addon_blocks") or [])
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

    class _Router:
        def __init__(self):
            self.calls = 0
            self.answer_provider, self._last_provider = None, "fake"

        def is_local_primary(self):
            return False

        def supports_vision(self):
            return False

        def get_response(self, messages, **kw):
            self.calls += 1
            return "Обычный ответ."

    tmp = Path(tempfile.mkdtemp(prefix="phone_bot_"))
    b = BotInstance.__new__(BotInstance)
    b.persona_name = "phonectl"
    b.context = f"phonectl_{time.time_ns()}"
    b.owner = owner
    b.web_single_user = web_single
    b._cc_allowed_users = set()
    b.features = dict(features or {"file_access": False})
    b.trigger_words = ["коннор"]
    b.intellect = SimpleNamespace(active=False)
    b.conversation_style = ConversationStyleConfig(None)
    b._control_mode = set()
    b.computer_control = ComputerControlManager(context=b.context, config={"confirm": True},
                                                base_dir=tmp / b.context)
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
    b.router = _Router()
    b.list_offers = ListOffers()
    return b


def test_bot():
    section("E. полный ход process_message")
    reset_registry()
    ph = FakePhone()
    try:
        b = make_bot()
        r = b.process_message("Коннор, на телефоне нажми Хэнк", user_id="A", chat_id="c1")
        check("владелец: нажато без модели", r == "Нажал «Хэнк»." and b.router.calls == 0
              and "screen.tap" in ph.actions())
        check("…реплика и ответ — в истории", b.memory.log[-2:] == [
            ("user", "Коннор, на телефоне нажми Хэнк"), ("assistant", "Нажал «Хэнк».")])

        ph.cmds.clear()
        r = b.process_message("на телефоне нажми Хэнк", user_id="B", chat_id="c1")
        check("не владелец — обычный ответ, телефону ничего", r == "Обычный ответ." and not ph.cmds)
        r = b.process_message("на телефоне нажми Хэнк", user_id="A", chat_id="-100500")
        check("группа Telegram — обычный ответ, телефону ничего", r == "Обычный ответ." and not ph.cmds)
        r = b.process_message("на телефоне нажми Хэнк", user_id="A", chat_id="c1", from_skin=True)
        check("из скина — телефону ничего", not ph.cmds)
        r = b.process_message("на телефоне нажми Хэнк", user_id="A", chat_id="c1",
                              turn_note="The app on the user's phone has just opened \"X\".")
        check("ход команды, уже исполненной телефоном, — телефону ничего", not ph.cmds)
        b2 = make_bot(features={"file_access": False, "phone_control": False})
        b2.process_message("на телефоне нажми Хэнк", user_id="A", chat_id="c1")
        check("features.phone_control: false — выключено", not ph.cmds)
        b3 = make_bot(owner="", web_single=True)
        r = b3.process_message("на телефоне нажми Хэнк", user_id="web_user", chat_id=None)
        check("веб без chat_id (один пользователь) — работает по user_id", r == "Нажал «Хэнк»."
              and "screen.tap" in ph.actions())

        ph.cmds.clear()
        b.process_message("нажми «Далее»", user_id="A", chat_id="c1")
        check("«нажми «Далее»» без «на телефоне» — не телефону", not ph.cmds)

        b = make_bot()
        r = b.process_message("на телефоне напиши Каре что опоздаю", user_id="A", chat_id="c1")
        check("цель телефону — ответ модели с заметкой «не умею, ничего не сделано»",
              r == "Обычный ответ." and any("You cannot do that yet" in x for x in b.persona.addons))

        b = make_bot()
        r = b.process_message("что на экране телефона?", user_id="A", chat_id="c1")
        check("«что на экране» — ответ модели с заметкой", r == "Обычный ответ." and b.router.calls == 1
              and any("phone's screen-control service" in x for x in b.persona.addons)
              and any("[button] Отправить" in x for x in b.persona.addons))

        # Наш вопрос гасит ожидающее «да» режима управления компьютером
        b = make_bot()
        cc = b.computer_control
        cc.set_pending("c1", {"kind": "click", "element": "Купить"}, user_id="A")
        had = cc.get_pending("c1", user_id="A") is not None
        r = b.process_message("на телефоне нажми «Отправить»", user_id="A", chat_id="c1")
        check("вопрос телефона — «да» компьютера снято",
              "Нажать «Отправить»" in r and cc.get_pending("c1", user_id="A") is None,
              f"pending компьютера был: {had}")
        r = b.process_message("да", user_id="A", chat_id="c1")
        check("«да» — телефону", r.startswith("Нажал «Отправить»") and b.router.calls == 0)
    finally:
        ph.close()
        reset_registry()


# ── F ──────────────────────────────────────────────────────────────

def test_screen_routes():
    section("F. веб и Telegram: экран телефона — не экран компьютера")
    from app.api import server
    from app.features import screen_look
    bot = SimpleNamespace(control_mode_on=lambda key: False)
    req = SimpleNamespace(from_skin=False, message="посмотри, что на экране телефона",
                          chat_id="c1", user_id="A")
    check("«посмотри, что на экране телефона» — не снимок компьютера",
          not server._wants_screen(bot, req))
    req.message = "посмотри на экран"
    check("«посмотри на экран» — по-прежнему снимок компьютера", server._wants_screen(bot, req)
          and screen_look.is_screen_request(req.message))
    src = (ROOT / "app" / "telegram_bot.py").read_text(encoding="utf-8")
    check("Telegram: снимок экрана компьютера не берёт фразы про телефон",
          "not phone_control.mentions_phone(clean_text)" in src)


# ── G ──────────────────────────────────────────────────────────────

def test_contract():
    section("G. контракт Java ↔ Python")
    jdir = ROOT / "web" / "android" / "app" / "src" / "main" / "java" / "io" / "vpcore" / "app"
    link = (jdir / "PhoneControlLink.java").read_text(encoding="utf-8")
    screen = (jdir / "PhoneScreen.java").read_text(encoding="utf-8")
    m = re.search(r"CAPABILITIES\s*=\s*\{(.*?)\};", link, re.S)
    java_caps = set(re.findall(r'"([\w.]+)"', m.group(1))) if m else set()
    py_caps = {pc.READ, pc.TAP, pc.TYPE, pc.SCROLL, pc.GLOBAL, pc.APPS, pc.OPEN}
    check("возможности Java = константы ядра", java_caps == py_caps, f"{java_caps ^ py_caps}")
    for action in py_caps:
        check(f"PhoneControlLink исполняет {action}", f'case "{action}"' in link)
    codes = set(re.findall(r'new Failure\("(\w+)"', screen)) | set(
        re.findall(r'PhoneScreen\.Failure\("(\w+)"', link))
    handled = {"gone", "disabled", "no_window", "no_field", "not_field"}
    generic = {"failed", "bad_args", "unsupported", "not_found", "no_handler"}
    check("коды отказов Java известны ядру", codes <= handled | generic, f"{codes - handled - generic}")
    server_src = (ROOT / "app" / "api" / "server.py").read_text(encoding="utf-8")
    check("служба при остановке говорит ядру «ухожу» — маршрут есть с обеих сторон",
          '"/bye"' in link and '@app.post("/api/devices/{device_id}/bye"' in server_src)
    for name in ("back", "home", "recents", "notifications", "quick_settings"):
        check(f"глобальное действие «{name}» есть в Java", f'case "{name}"' in screen)
    manifest = (ROOT / "web" / "android" / "app" / "src" / "main" / "AndroidManifest.xml").read_text(
        encoding="utf-8")
    check("служба в манифесте — только для системы (BIND_ACCESSIBILITY_SERVICE)",
          'android:name=".PhoneControlService"' in manifest
          and "android.permission.BIND_ACCESSIBILITY_SERVICE" in manifest
          and "@xml/phone_control_service" in manifest)
    xml = (ROOT / "web" / "android" / "app" / "src" / "main" / "res" / "xml"
           / "phone_control_service.xml").read_text(encoding="utf-8")
    check("служба читает окна и делает жесты", 'canRetrieveWindowContent="true"' in xml
          and 'canPerformGestures="true"' in xml and "flagRetrieveInteractiveWindows" in xml)
    main = (jdir / "MainActivity.java").read_text(encoding="utf-8")
    check("плагин PhoneControl зарегистрирован", "registerPlugin(PhoneControlPlugin.class)" in main)
    for lang in ("values", "values-ru"):
        strings = (ROOT / "web" / "android" / "app" / "src" / "main" / "res" / lang
                   / "strings.xml").read_text(encoding="utf-8")
        check(f"{lang}: название и описание службы", 'name="phone_control_label"' in strings
              and 'name="phone_control_desc"' in strings)


# ── H ──────────────────────────────────────────────────────────────

APPS = ["Telegram", "YouTube", "YouTube Music", "Камера", "Галерея", "Chrome", "Firefox", "СберБанк", "Яндекс",
        "Яндекс Музыка", "Яндекс Карты", "Карты", "Google", "Google Play Маркет", "Часы", "Музыка", "Spotify",
        "VK", "WhatsApp", "Настройки", "Калькулятор", "Календарь", "Gmail", "Погода", "Telegram"]
APP_NAMES = ["телеграмм", "ютуб", "ютуб музыку", "камеру", "camera", "галерею", "сбер", "яндекс",
             "яндекс музыку", "карты", "chrome", "гугл", "плей маркет", "вк", "вотсап", "спотифай",
             "settings", "браузер", "telegram", "тикток", "кал", "Firefox", "часы", "погоду", "гмейл",
             "телеграм", "youtube music", "яндекс карты"]


def test_match_app_ts():
    section("H. подбор приложения = matchApp из web/src/phone/parse.ts")
    import json
    import subprocess
    parse_ts = (ROOT / "web" / "src" / "phone" / "parse.ts").as_uri()
    js = f"""
const p = await import({json.dumps(parse_ts)});
const apps = INPUT.apps.map((label, i) => ({{ label, package: 'pkg' + i }}));
console.log(JSON.stringify(INPUT.names.map((name) => {{
  const r = p.matchApp(name, apps, false);
  return {{ status: r.status, pkgs: r.app ? [r.app.package] : (r.apps || []).map((a) => a.package) }};
}})));
"""
    payload = {"apps": APPS, "names": APP_NAMES}
    script = f"globalThis.INPUT = {json.dumps(payload, ensure_ascii=False)};\n{js}"
    try:
        res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True,
                             text=True, timeout=60, cwd=str(ROOT / "web"))
    except FileNotFoundError:
        check("Node не найден — сверка с TS пропущена", True)
        return
    if res.returncode != 0:
        check(f"Node: {res.stderr.strip()[:400]}", False)
        return
    want = json.loads(res.stdout.strip().splitlines()[-1])
    apps = [{"label": a, "pkg": f"pkg{i}"} for i, a in enumerate(APPS)]
    for name, w in zip(APP_NAMES, want):
        status, found = pc.match_app(name, apps)
        pkgs = [found["pkg"]] if status == "ok" else [a["pkg"] for a in (found or [])]
        check(f"«{name}» → {w['status']} {w['pkgs']}", status == w["status"] and pkgs == w["pkgs"],
              f"ядро: {status} {pkgs}")


def main():
    test_parse()
    test_match()
    test_privacy()
    test_turns()
    test_bot()
    test_screen_routes()
    test_contract()
    test_match_app_ts()
    print(f"\nИтого: {ok} OK, {fail} FAIL")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()

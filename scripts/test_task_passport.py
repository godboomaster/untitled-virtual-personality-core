"""Тест: паспорт и СНИЛС не уходят моделям и не пишутся на диск.

Правило пользователя: паспорт и СНИЛС никогда не храним и не отдаём
никаким моделям — модели только название переменной. Модель агента задач
видит {{secretN}}, настоящее значение код подставляет только в момент
ввода в поле страницы.

Проверяет:
* распознавание (cc_privacy.id_doc_values): паспорт РФ (серия и номер в
  разных записях, раздельно, код подразделения, дата выдачи),
  загранпаспорт, СНИЛС, ИНН; телефон, сумма, номер заказа, год, серийный
  номер — не документ;
* общие правила: секрет в команде (LLM-ярус разбора пропускается), маска
  истории (command_secret_values), redact_inline, ввод в поле «Серия»,
  аудит, фильтр логов;
* агент задач: паспорт в цели, ответ «серия … номер …» на вопрос без слова
  «паспорт», ответ на вопрос о паспорте, СНИЛС в цели — ни в одном промпте
  (облако: шаг и разбор ответа), ни в памяти задач, ни в аудите, ни в логе,
  ни в карточке нет цифр; ввод в поле — настоящее значение;
* последний рубеж: номер в подписи приватной страницы — заглушкой и в
  промпте локальной модели (ввод по заглушке — настоящее значение); поиск
  с номером документа не уходит поисковику даже после «да»;
* обычные числа в цели (сумма, номер заказа, телефон) модель видит;
* бот: цель и ответ с паспортом в STM — маской; фраза режима управления с
  паспортом не уходит LLM-ярусу разбора.

Номера вымышленные. Без браузера, веб-чатов и настоящих LLM.

Запуск: python3 -m scripts.test_task_passport
"""

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="ta_passport_"))
os.environ["DATA_DIR"] = str(TMP / "data")
os.environ["VPC_DATA_DIR"] = str(TMP / "data")

from scripts.test_task_agent import ScriptedRouter, real_cc  # noqa: E402

FAILS = 0
OK = 0

PASS = "4510 123456"
SNILS = "123-456-789 01"
# Куски, которых не должно быть нигде: серия, номер, код подразделения,
# дата выдачи, хвост СНИЛС
LEAKS = ("4510", "123456", "123-456", "789 01", "01.02.2015")


def check(name, cond, detail=""):
    global FAILS, OK
    OK += 1
    if cond:
        print(f"  [OK] {name}")
    else:
        FAILS += 1
        print(f"  [FAIL] {name} {detail}")


def leaked(*texts) -> list:
    out = []
    for t in texts:
        t = str(t or "")
        out += [x for x in LEAKS if x in t]
    return out


class Cap(logging.Handler):
    """Все записи логгеров app.* — текстом, как их отформатирует хендлер."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass


class SlotRouter:
    """slot_router: разбор ответа по слотам брифа (облако) — запоминает."""

    def __init__(self):
        self.prompts = []

    def get_response(self, messages, **kw):
        self.prompts.append(messages[-1]["content"])
        return "{}"


FORM = "https://forms.test/anketa"
FORM_ITEMS = [
    {"idx": 1, "tag": "input", "role": "textbox",
     "text": "Серия и номер паспорта", "ed": True, "vp": True},
    {"idx": 2, "tag": "input", "role": "textbox", "text": "Серия",
     "ed": True, "vp": True},
    {"idx": 3, "tag": "input", "role": "textbox", "text": "Номер",
     "ed": True, "vp": True},
    {"idx": 4, "tag": "input", "role": "textbox", "text": "СНИЛС",
     "ed": True, "vp": True},
    {"idx": 5, "tag": "button", "role": "button", "text": "Далее",
     "vp": True},
]
# Профиль на госуслугах — приватная страница (только локальная модель):
# сама страница показывает паспорт
PROFILE = "https://gosuslugi.test/profile"
PROFILE_ITEMS = [
    {"idx": 7, "tag": "div", "text": f"Паспорт: серия 4510 номер 123456",
     "vp": True},
    {"idx": 8, "tag": "input", "role": "textbox", "text": "Серия",
     "ed": True, "vp": True},
]


def typed(cc) -> list:
    return [(a.get("element"), a.get("text")) for a in cc.dispatched
            if a.get("kind") == "type"]


def audit_text(cc) -> str:
    p = Path(cc.base_dir) / "audit.jsonl"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def run_agent(goal, replies, answers, items=None, url=FORM, chat="c1"):
    """Прогон агента на настоящем ComputerControlManager (гейт, аудит) с
    подменёнными браузером и моделью. answers — реплики человека по
    очереди. → (agent, cc, router, slot, ответы агента)."""
    from app.features.task_agent import TaskAgent
    base = Path(tempfile.mkdtemp(prefix="cc_", dir=TMP))
    cc = real_cc({url: [dict(x) for x in (items or FORM_ITEMS)]}, url=url,
                 base_dir=base)
    agent = TaskAgent(cc, memory_path=base / "task_memory.json")
    slot = SlotRouter()
    agent.slot_router = slot
    router = ScriptedRouter(list(replies))
    out = [agent.start(chat, goal, router, user_id="A")]
    for a in answers:
        out.append(agent.feed(chat, a, router, user_id="A"))
    return agent, cc, router, slot, out


def mem_text(agent) -> str:
    p = agent._memory_path
    return p.read_text(encoding="utf-8") if p.exists() else ""


def main():
    from app.features import browser_actions as _ba
    _ba.wait_dom_idle = lambda *a, **k: None
    _ba.section_items = lambda *a, **k: None
    _ba.section_names = lambda *a, **k: None
    from app.features import cc_privacy as P
    from app.features import task_agent as ta
    # Локальная модель — по умолчанию «нет» (тест, которому нужна, подменит)
    orig_local = P.PrivateRouter.get_response
    P.PrivateRouter.get_response = lambda self, msgs, **kw: None
    cap = Cap()
    app_log = logging.getLogger("app")
    app_log.addHandler(cap)
    app_log.setLevel(logging.DEBUG)

    # ── 1. Распознавание ──
    print("распознавание документов")
    f = P.id_doc_values
    cases = [
        ("впиши паспорт 4510 123456 в форму", None, ["4510 123456"]),
        ("паспорт 45 10 123456", None, ["45 10 123456"]),
        ("паспорт 4510123456", None, ["4510123456"]),
        ("Паспорт: 4510 № 123456", None, ["4510 № 123456"]),
        ("серия 4510 номер 123456", None, ["4510", "123456"]),
        ("Серия 4510, номер 123456", None, ["4510", "123456"]),
        ("4510 123456 — мой паспорт", None, ["4510 123456"]),
        ("введи 4510 в поле серия", None, ["4510"]),
        ("введи 4510 123456 в поле серия и номер паспорта", None,
         ["4510 123456"]),
        ("4510 123456", "Серия и номер?", ["4510 123456"]),
        ("4510", "Серия", ["4510"]),
        ("номер 123456", "Серия и номер паспорта?", ["123456"]),
        ("паспорт 4510 123456, выдан ОВД Тестовый 01.02.2015, код "
         "подразделения 123-456", None,
         ["4510 123456", "123-456", "01.02.2015"]),
        ("01.02.2015", "Дата выдачи паспорта?", ["01.02.2015"]),
        ("загранпаспорт 75 1234567", None, ["75 1234567"]),
        ("passport number 123456789", None, ["123456789"]),
        ("СНИЛС 123-456-789 01", None, [SNILS]),
        (SNILS, None, [SNILS]),
        ("мой снилс 12345678901", None, ["12345678901"]),
        ("ИНН 123456789012", None, ["123456789012"]),
        ("инн 1234567890", None, ["1234567890"]),
    ]
    for text, ctx, want in cases:
        got = f(text, ctx)
        check(f"документ: «{text}»" + (f" на «{ctx}»" if ctx else ""),
              sorted(got) == sorted(want), f"{got}")
    for text, ctx in [("позвони 4510123456", None),
                      ("телефон 89991234567", None),
                      ("+7 999 123-45-67", None),
                      ("паспорт есть, телефон 9991234567", None),
                      ("номер заказа 4510123456", None),
                      ("заказ №123456 оплачен", None),
                      ("сумма 4510 руб", None),
                      ("4510 ₽ за две пиццы", None),
                      ("открой сайт паспортного стола 2024", None),
                      ("серийный номер 123456", None),
                      ("код из смс 123456, паспорт ввёл", None),
                      ("пункт выдачи 123456", None),
                      ("123456", "Номер?"),
                      ("позвони 4510123456 в пятницу насчёт паспорта", None),
                      ("4510123456", None)]:
        got = f(text, ctx)
        check(f"не документ: «{text}»" + (f" на «{ctx}»" if ctx else ""),
              got == [], f"{got}")

    # ── 2. Общие правила маски ──
    print("общие правила")
    cmd = "впиши паспорт 4510 123456 в анкету на госуслугах"
    check("секрет команды: паспорт — LLM-ярус разбора пропускается",
          P.command_has_secret(cmd)
          and P.command_has_secret("мой снилс 123-456-789 01"))
    check("маска истории: значения паспорта из команды",
          "4510 123456" in P.command_secret_values(cmd)
          and set(P.command_secret_values("серия 4510 номер 123456"))
          >= {"4510", "123456"})
    check("обычная команда с телефоном/суммой — не секрет",
          not P.command_has_secret("закажи пиццу, телефон 89991234567")
          and not P.command_has_secret("положи товаров на 4510 рублей"))
    red = P.redact_inline("Профиль. Паспорт: серия 4510 номер 123456, СНИЛС "
                          "123-456-789 01")
    check("redact_inline: серия, номер и СНИЛС — маской",
          not leaked(red) and "***(4)" in red, red)
    check("ввод в поле «Серия» — чувствительный",
          P.typed_is_sensitive("4510", "Серия")
          and P.redact_typed("4510", "Серия") == "***(4)"
          and not P.typed_is_sensitive("Пепперони", "Поиск"))
    rec, _ch = P.redact_audit_record({"kind": "type", "text": "4510",
                                      "element": "Серия"})
    check("аудит: ввод серии — маской", rec["text"] == "***(4)", f"{rec}")
    from app.core.log_privacy import redact_log
    line = redact_log("[TaskAgent] старт: «впиши серия 4510 номер 123456»")
    check("фильтр логов: серия и номер — маской", not leaked(line), line)
    check("ответ «4510» на «Серия?» — секрет целиком",
          ta._secret_answer("Серия?", "4510"))
    check("ответ «серия 4510 номер 123456» — по кускам",
          not ta._secret_answer("Какие данные документа?",
                                "серия 4510 номер 123456")
          and set(ta._answer_secrets("серия 4510 номер 123456"))
          >= {"4510", "123456"})
    check("память задач: _redact прячет серию и номер",
          not leaked(ta._redact("впиши серия 4510 номер 123456")))

    # ── 3. Паспорт в цели ──
    print("паспорт в цели")
    cap.lines.clear()
    goal = f"впиши паспорт {PASS} в анкету на forms.test"
    agent, cc, router, slot, out = run_agent(
        goal, [{"action": "type", "n": 1, "text": "{{secret1}}"},
               {"action": "done", "message": "Готово, вписал."}], ["да"])
    check("ввод в поле — настоящее значение",
          typed(cc) == [("Серия и номер паспорта", PASS)], f"{typed(cc)}")
    check("перед вводом — «да» (ввод секрета)",
          "Делаю?" in out[0] or "да/нет" in out[0], out[0][:200])
    check("облачный промпт шага — только {{secret1}}",
          router.prompts and not leaked(*router.prompts)
          and "{{secret1}}" in router.prompts[0],
          f"{leaked(*router.prompts)}")
    check("разбор цели по слотам (облако) — без номера",
          slot.prompts and not leaked(*slot.prompts),
          f"{leaked(*slot.prompts)}")
    check("ответы агента в чат — без номера", not leaked(*out), f"{out}")
    check("память задач — без номера",
          mem_text(agent) and not leaked(mem_text(agent)), mem_text(agent))
    check("аудит — без номера",
          audit_text(cc) and not leaked(audit_text(cc)), audit_text(cc)[:300])
    check("лог — без номера", cap.lines and not leaked(*cap.lines),
          f"{[x for x in cap.lines if leaked(x)][:3]}")
    card = agent.task_card("c1") or {}
    check("карточка задачи — без номера",
          card and not leaked(json.dumps(card, ensure_ascii=False)),
          json.dumps(card, ensure_ascii=False)[:300])

    # ── 4. Ответ «серия … номер …» на вопрос без слова «паспорт» ──
    print("ответ без слова «паспорт»")
    cap.lines.clear()
    agent, cc, router, slot, out = run_agent(
        "заполни анкету на forms.test",
        [{"action": "ask", "question": "Какие данные документа вписать?"},
         {"action": "type", "n": 2, "text": "{{secret1}}"},
         {"action": "type", "n": 3, "text": "{{secret2}}"},
         {"action": "done", "message": "Готово."}],
        ["серия 4510 номер 123456", "да", "да"], chat="c2")
    check("серия и номер — в свои поля настоящими значениями",
          typed(cc) == [("Серия", "4510"), ("Номер", "123456")],
          f"{typed(cc)}")
    p_ans = next((p for p in router.prompts if "A: " in p), "")
    check("промпт после ответа: подписи видны, номера — заглушками",
          "серия {{secret1}} номер {{secret2}}" in p_ans, p_ans[-800:])
    check("облачные промпты шагов и разбора — без номеров",
          not leaked(*router.prompts, *slot.prompts),
          f"{leaked(*router.prompts, *slot.prompts)}")
    check("память, аудит, лог, ответы — без номеров",
          not leaked(mem_text(agent), audit_text(cc), *cap.lines, *out),
          f"{leaked(mem_text(agent), audit_text(cc), *cap.lines, *out)}")
    check("ответ — секрет для маски истории бота (по кускам)",
          set(P.command_secret_values("серия 4510 номер 123456"))
          >= {"4510", "123456"})

    # ── 5. Ответ на вопрос о паспорте ──
    print("ответ на вопрос о паспорте")
    cap.lines.clear()
    agent, cc, router, slot, out = run_agent(
        "заполни анкету на forms.test",
        [{"action": "ask", "question": "Серия и номер паспорта?"},
         {"action": "type", "n": 1, "text": "{{secret1}}"},
         {"action": "done", "message": "Готово."}],
        [PASS, "да"], chat="c3")
    check("ввод — настоящее значение",
          typed(cc) == [("Серия и номер паспорта", PASS)], f"{typed(cc)}")
    check("ответ — секрет целиком (маска STM бота)",
          agent.awaiting_question("c3") is None
          and ta._secret_answer("Серия и номер паспорта?", PASS))
    every = [*router.prompts, *slot.prompts, mem_text(agent), audit_text(cc),
             *cap.lines, *out]
    check("промпты, память, аудит, лог, ответы — без номера",
          not leaked(*every), f"{leaked(*every)}")

    # ── 6. СНИЛС в цели ──
    print("СНИЛС в цели")
    cap.lines.clear()
    agent, cc, router, slot, out = run_agent(
        f"впиши СНИЛС {SNILS} в анкету на forms.test",
        [{"action": "type", "n": 4, "text": "{{secret1}}"},
         {"action": "done", "message": "Готово."}], ["да"], chat="c4")
    check("СНИЛС введён настоящим значением",
          typed(cc) == [("СНИЛС", SNILS)], f"{typed(cc)}")
    every = [*router.prompts, *slot.prompts, mem_text(agent), audit_text(cc),
             *cap.lines, *out]
    check("промпты, память, аудит, лог, ответы — без СНИЛС",
          not leaked(*every), f"{leaked(*every)}")

    # Следующая задача той же темы: прошлая запись в промпте — без номеров
    from app.features.task_agent import TaskAgent
    a2 = TaskAgent(cc, memory_path=agent._memory_path)
    r2 = ScriptedRouter([{"action": "fail", "message": "стоп"}])
    a2.start("c4", "впиши снилс в анкету", r2, user_id="A")
    check("прошлые задачи в промпте — без СНИЛС",
          r2.prompts and "earlier tasks" in r2.prompts[0]
          and not leaked(*r2.prompts), f"{leaked(*r2.prompts)}")

    # Записи памяти, легшие до распознавания документов (и другого чата), —
    # маской при следующей записи файла
    mem_p = agent._memory_path
    data = json.loads(mem_p.read_text(encoding="utf-8"))
    data["old"] = [{"ts": 1, "goal": f"впиши паспорт {PASS}", "sites": [],
                    "ok": False, "qa": [["Какие данные документа?",
                                         "серия 4510 номер 123456"],
                                        ["Серия?", "4510"]],
                    "result": f"вписал СНИЛС {SNILS}"}]
    mem_p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    a3 = TaskAgent(cc, memory_path=mem_p)
    r3 = ScriptedRouter([{"action": "ask", "question": "Какой адрес?"},
                         {"action": "fail", "message": "стоп"}])
    a3.start("c4b", "впиши адрес в анкету", r3, user_id="A")
    a3.feed("c4b", "Ленина 5", r3, user_id="A")
    old = json.loads(mem_p.read_text(encoding="utf-8")).get("old") or [{}]
    check("старые записи памяти — без номеров после следующей записи",
          "c4b" in json.loads(mem_p.read_text(encoding="utf-8"))
          and not leaked(json.dumps(old, ensure_ascii=False))
          and old[0].get("qa", [[0, 0]])[0][1] == "серия ***(4) номер ***(6)",
          json.dumps(old, ensure_ascii=False))

    # ── 7. Приватная страница: номер на самой странице, локальная модель ──
    print("локальная модель приватной страницы")
    local_prompts = []
    local_replies = [{"action": "type", "n": 2, "text": "{{secret1}}"},
                     {"action": "done", "message": "Готово."}]

    def _local(self, messages, **kw):
        p = messages[-1]["content"]
        local_prompts.append(p)
        if "autopilot" not in p or not local_replies:
            return "{}"
        return json.dumps(local_replies.pop(0), ensure_ascii=False)
    P.PrivateRouter.get_response = _local
    cap.lines.clear()
    try:
        agent, cc, router, slot, out = run_agent(
            "проверь серию в профиле на gosuslugi.test", [], ["да"],
            items=PROFILE_ITEMS, url=PROFILE, chat="c5")
    finally:
        P.PrivateRouter.get_response = lambda self, msgs, **kw: None
    steps = [p for p in local_prompts if p.startswith("You are an autopilot")]
    check("шаг ушёл локальной модели, облаку — нет",
          steps and not router.prompts, f"{len(steps)} {len(router.prompts)}")
    check("локальный промпт: номер со страницы — заглушкой",
          steps and not leaked(*local_prompts)
          and "серия {{secret1}} номер {{secret2}}" in steps[0],
          (steps[0][:900] if steps else ""))
    check("ввод по заглушке со страницы — настоящее значение",
          typed(cc) == [("Серия", "4510")], f"{typed(cc)}")
    check("память, аудит, лог — без номера",
          not leaked(mem_text(agent), audit_text(cc), *cap.lines),
          f"{leaked(mem_text(agent), audit_text(cc), *cap.lines)}")

    # Последний рубеж напрямую: номер в тексте промпта → заглушка прогона
    run = {"hidden": {}}
    p, _st = TaskAgent._hide_docs(run, "Page text:\nСНИЛС 123-456-789 01")
    check("последний рубеж: СНИЛС в тексте страницы — {{secretN}}",
          not leaked(p) and "{{secret1}}" in p
          and run["hidden"].get(SNILS) == "{{secret1}}", p)

    # ── 8. Поиск с номером документа ──
    print("поиск")
    searched = []
    orig_search = ta.web_search_links
    ta.web_search_links = lambda q, engine="google": (
        searched.append(q), ([], None))[1]
    try:
        agent = TaskAgent(real_cc({FORM: FORM_ITEMS}, url=FORM),
                          memory_path=TMP / "m8.json")
        run = {"goal": f"проверь паспорт {PASS}", "qa": [], "history": [],
               "steps": 0, "turn_user": "A"}
        agent._hidden(run, "c8")
        kind, _t = agent._do_search(run, f"проверка паспорта {PASS}",
                                    chat_id="c8")
        kind2, _t2 = agent._do_search(run, f"{PASS} действителен",
                                      confirmed=True, chat_id="c8")
        check("номер в запросе — поиск не делается, и с «да» тоже",
              searched == [] and kind == kind2 == "progress"
              and (run.get("awaiting") or {}).get("kind") != "confirm")
        check("строка истории о поиске — без номера",
              run["history"] and not leaked(*run["history"]),
              f"{run['history']}")
        agent._do_search(run, "проверка паспорта онлайн", chat_id="c8")
        check("запрос без номера — ищется", searched == [
            "проверка паспорта онлайн"])
    finally:
        ta.web_search_links = orig_search

    # ── 9. Обычные числа модель видит ──
    print("не документы")
    agent, cc, router, slot, out = run_agent(
        "закажи на forms.test товаров на сумму 4510 рублей, номер заказа "
        "123456, телефон 89991234567",
        [{"action": "fail", "message": "стоп"}], [], chat="c9")
    check("сумма и номер заказа в промпте — как есть",
          router.prompts and "4510" in router.prompts[0]
          and "123456" in router.prompts[0], router.prompts[0][:300]
          if router.prompts else "")
    check("номера заказа и суммы нет среди скрытых",
          not {"4510", "123456"} & set((agent.__dict__.get("_finished") or {})
                                       .get("c9", {}).get("run", {})
                                       .get("hidden", {})))

    # ── 10. Бот: STM и разбор фразы режима управления ──
    print("бот: история и LLM-ярус")
    bot_checks(check)

    P.PrivateRouter.get_response = orig_local
    app_log.removeHandler(cap)
    print(f"\nOK: {OK - FAILS}/{OK}, FAIL: {FAILS}")
    return FAILS


def bot_checks(check):
    """Ход бота в режиме управления: цель «задача: …» и ответы агенту с
    паспортом — в STM маской; фраза с паспортом, которую regex не разобрал,
    — не в LLM-ярус (просьба сказать по шаблону)."""
    from app.bot_instance import BotInstance
    from app.features import flavor_text
    from app.features.computer_control import (ComputerControlManager,
                                               normalize_command)
    from app.features.task_agent import TaskAgent
    flavor_text.cc_reply = lambda *a, **kw: None

    class _CC(ComputerControlManager):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.executed = []

        def _snapshot_for(self, site_word, chat_id="", auto_dismiss=False):
            return FORM, "forms.test", [dict(x) for x in FORM_ITEMS], 1, None

        def execute(self, action, chat_id="", router=None):
            self.executed.append(dict(action))
            return True, ""

    class _Mem:
        def __init__(self):
            self.log = []
            self.stm = SimpleNamespace(get_last=lambda *a, **kw: [],
                                       get_messages=lambda *a, **kw: [])

        def add_message(self, role, content, *a, **kw):
            self.log.append((role, content))

    class _Router:
        def __init__(self):
            self.replies = []
            self.prompts = []

        def get_response(self, messages, **kw):
            p = str(messages[-1]["content"])
            self.prompts.append(p)
            if p.startswith("You keep the brief"):
                return "{}"
            r = (self.replies.pop(0) if self.replies
                 else {"action": "done", "message": "ок"})
            return json.dumps(r, ensure_ascii=False)

    def mkbot():
        b = BotInstance.__new__(BotInstance)
        b.persona_name = "connor"
        b.trigger_words = ["коннор"]
        b.persona = SimpleNamespace(
            persona_data={"name": "Connor"}, system_prompt="You are Connor.",
            get_settings=lambda: {"max_tokens": 400, "top_p": 0.9})
        b.context = f"pass_{os.urandom(3).hex()}"
        b.proactive = None
        b.reminder_manager = None
        b.memory = _Mem()
        b._pending_photos = {}
        b._pending_more_photos = {}
        b.computer_control = _CC(
            context=b.context, config={"confirm": False, "click": True},
            base_dir=TMP / f"b{os.urandom(3).hex()}")
        b.task_agent = TaskAgent(b.computer_control)
        b.router = _Router()
        b._cc_hist_install()
        return b

    def hist(b):
        return "\n".join(c for _r, c in b.memory.log)

    def ta_turn(b, text, replies, goal=None):
        b.router.replies = list(replies)
        with b.user_turn("c"):
            return b._task_agent_turn(text, "u", "c", "U", "ru", goal=goal,
                                      feed_text=text)

    b = mkbot()
    goal = f"впиши паспорт {PASS} в анкету на forms.test"
    r1 = ta_turn(b, f"задача: {goal}",
                 [{"action": "type", "n": 1, "text": "{{secret1}}"}],
                 goal=goal)
    r2 = ta_turn(b, "да", [{"action": "done", "message": "Готово."}])
    check("бот: паспорт из цели введён настоящим значением",
          any(a.get("text") == PASS for a in b.computer_control.executed))
    check("бот: цель с паспортом в STM — маской, ответы агента — тоже",
          not leaked(hist(b)) and "***(11)" in hist(b), hist(b)[:400])
    check("бот: модели (шаг и бриф) — без номера",
          b.router.prompts and not leaked(*b.router.prompts))
    check("бот: человеку — без номера", not leaked(r1, r2), f"{r1} | {r2}")

    b = mkbot()
    ta_turn(b, "задача: заполни анкету на forms.test",
            [{"action": "ask", "question": "Какие данные документа вписать?"}],
            goal="заполни анкету на forms.test")
    ta_turn(b, "серия 4510 номер 123456",
            [{"action": "fail", "message": "стоп"}])
    check("бот: ответ «серия … номер …» в STM — маской",
          not leaked(hist(b)) and "серия ***(4) номер ***(6)" in hist(b),
          hist(b)[:400])
    check("бот: модели — без номеров",
          b.router.prompts and not leaked(*b.router.prompts))

    # Фраза режима управления, которую regex не разобрал: в LLM-ярус не
    # уходит (иначе текст с паспортом ушёл бы облаку на разбор)
    b = mkbot()
    b.task_agent = None
    text = f"заполни паспорт {PASS} на госуслугах"
    with b.user_turn("c"):
        r = b._cc_fast_path(normalize_command(text, b._address_names()),
                            text, "u", "c", "U", "ru")
    check("бот: фраза с паспортом — без LLM-яруса разбора",
          not b.router.prompts, f"{b.router.prompts[:1]}")
    check("бот: ответ — просьба сказать по шаблону, без номера",
          r and "введи" in str(r) and not leaked(r), f"{r}")
    check("бот: фраза в STM — маской", not leaked(hist(b)), hist(b)[:300])

    # Единая точка записи STM — и вне хода (фоновая запись без кадра
    # маскировки): номер документа маской, остальной текст как был
    b = mkbot()
    rec = b._cc_hist_redact("user", f"мой паспорт {PASS}, запомни", "u", "c")
    check("бот: запись истории вне хода — паспорт маской",
          not leaked(rec) and rec.startswith("мой паспорт ***(")
          and rec.endswith(", запомни"), f"{rec}")

    # Обычный чат (не режим управления): ввод маскируется до заметок,
    # переписчика, поиска и модели персоны — после веток режима управления
    # (там своё: command_has_secret), до первой эвристики обычного чата
    import inspect
    src = inspect.getsource(BotInstance._process_message_impl)
    at_mask = src.find("user_input = mask_id_docs(user_input)")
    at_raw = src.find("raw_user_text = mask_id_docs(raw_user_text)")
    at_chat = src.find("typed_h = self._strip_address(raw_user_text)")
    at_cc = src.find("if (self.computer_control and cc_mode_key")
    check("бот: обычный чат — ввод маской до эвристик и модели персоны",
          0 <= at_cc < at_mask < at_chat and 0 <= at_raw < at_chat,
          f"cc={at_cc} mask={at_mask} raw={at_raw} chat={at_chat}")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

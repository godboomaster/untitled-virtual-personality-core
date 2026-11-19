"""Тест ввода личных данных агентом задач (task_agent: _type_gate,
_PERSONAL_FIELD_RE, _value_in, _elem_line, отбой «Продолжить» неактивна,
_ask_confirm после «нет»).

Живой прогон 29.10 (dodopizza, «закажи пиццу», оформление): модель сама
вписала в «Номер телефона» номер, которого человек не давал, — «да» на
«ввести «***(12)»» он давал вслепую (маска скрывала, что номер не его).
Потом номер человека «1111111111» сайт не принял: «Продолжить» осталась
неактивной, а поле в снимке — без признака «заполнено», и модель снова
предлагала ввести тот же номер; «нет» — тот же вопрос — «нет» — стоп
«хожу по кругу».

Проверяет:
* номер, которого человек не давал, — не вводится и без вопроса «да/нет»,
  модели — «спроси человека»;
* номер из ответа человека — вводится после «да»;
* неактивная «Продолжить» после ввода — «сайт не принял значение, скажи
  человеку»; поле в промпте — «filled in, the site marks it invalid»;
* тот же номер в заполненное поле — не вводится и не спрашивается;
* другой номер от человека — вводится;
* шаг, на который человек только что ответил «нет», — второй раз не
  спрашивается; после новых слов человека — снова можно;
* подписи личных полей, сравнение значения по цифрам.

Браузер — настоящий гейт ComputerControlManager с заглушкой _dispatch
(real_cc), модель — ScriptedRouter (test_task_agent): живых браузеров,
веб-чатов и LLM нет.

Запуск: PYTHONPATH=. python3 scripts/test_task_phone.py
"""

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.test_task_agent import ScriptedRouter, real_cc  # noqa: E402

FAILS = 0
OK = 0


def check(name, cond, detail=""):
    global FAILS, OK
    OK += 1
    if cond:
        print(f"  [OK] {name}")
    else:
        FAILS += 1
        print(f"  [FAIL] {name} {detail}")


SITE = "dodo.test"
LOGIN = f"https://{SITE}/nsk"  # окно входа поверх меню, как у Додо
GOOD = "9991234567"


def login_cc(base_dir):
    """Окно входа на оформлении: «Номер телефона» и «Продолжить»
    (неактивна, пока сайт не принял номер). «1111111111» сайт не принимает
    (поле заполнено, помечено неверным), 9991234567 — принимает: окно кода
    из SMS."""
    cc = real_cc({}, url=LOGIN, base_dir=base_dir)
    cc.phone = ""

    def _snap(site_word, chat_id="", auto_dismiss=False):
        digits = re.sub(r"\D", "", cc.phone)[-10:]
        ok = digits == GOOD
        if ok:
            page = [{"idx": 5, "tag": "input", "role": "", "ed": 1, "sn": 1,
                     "md": 1, "text": "Код из SMS"},
                    {"idx": 6, "tag": "button", "role": "button", "md": 1,
                     "text": "Войти", "dis": 1}]
        else:
            bad = bool(digits)
            ph = {"idx": 1, "tag": "input", "role": "", "ed": 1, "sn": 1,
                  "md": 1, "fm": 1, "text": "Номер телефона",
                  "ctx": "Вход на сайт Номер телефона Продолжить"}
            if bad:
                ph.update(fv=1, iv=1, er="Неверный номер телефона")
            page = [ph, {"idx": 3, "tag": "button", "role": "button",
                         "md": 1, "text": "Продолжить", "dis": 1}]
        cc.cur = LOGIN
        return LOGIN, SITE, [dict(x) for x in page], 1, None

    def _disp(action, router=None):
        cc.dispatched.append(dict(action))
        if action["kind"] == "type" and action.get("idx") == 1:
            cc.phone = str(action.get("text") or "")
    cc._snapshot_for, cc._dispatch = _snap, _disp
    return cc


def main():
    from app.features import browser_actions as _ba
    from app.features import cc_privacy as _cp
    from app.features import task_agent as ta
    from app.features.task_agent import TaskAgent

    _ba.wait_dom_idle = lambda *a, **k: None
    _ba.section_items = lambda *a, **k: None
    _ba.section_names = lambda *a, **k: None
    _ba.snapshot_for_goal = lambda host, text, tab_id=None: ("", [])
    _cp.PrivateRouter.get_response = lambda self, msgs, **kw: None
    tmp = Path(tempfile.mkdtemp(prefix="task_phone_"))
    slot = type("S", (), {"get_response": lambda self, m, **k: "{}"})()

    def typed(cc):
        return [d.get("text") for d in cc.dispatched
                if d.get("kind") == "type"]

    # ── 1. Живой прогон 29.10 ──
    print("номер телефона на оформлении (dodopizza 29.10):")
    cc = login_cc(tmp)
    ag = TaskAgent(cc, memory_path=tmp / "m1.json")
    ag.slot_router = slot
    rt = ScriptedRouter([
        # номер, которого человек не давал
        {"action": "type", "n": 1, "text": "+79001234567"},
        {"action": "ask", "question": "Какой номер телефона указать для "
                                      "входа на dodo.test?"},
        # ответ человека
        {"action": "type", "n": 1, "text": "1111111111"},
        # «да» → введено, сайт не принял
        {"action": "click", "n": 2, "label": "Продолжить"},
        {"action": "type", "n": 1, "text": "1111111111"},
        {"action": "ask", "question": "Сайт пишет «Неверный номер "
                                      "телефона». Какой номер указать?"},
        # другой номер
        {"action": "type", "n": 1, "text": GOOD},
        {"action": "fail", "message": "стоп"}])
    q = ag.start("p1", "войди на dodo.test по номеру телефона", rt,
                 user_id="A") or ""
    run = ag._runs.get("p1") or {}
    hist = run.get("history") or []
    check("номер, которого человек не давал, — не введён, без «да/нет», "
          "модели — «спроси человека»",
          not typed(cc)
          and any("the user never gave this value" in h for h in hist)
          and "Какой номер телефона" in q
          and (run.get("awaiting") or {}).get("kind") == "ask",
          f"{q!r} {hist[-2:]}")
    p2 = rt.prompts[1] if len(rt.prompts) > 1 else ""
    check("в промпте — отбой без самого номера",
          "never make up their personal data" in p2 and "9001234567" not in p2,
          p2[-1500:])

    q = ag.feed("p1", "1111111111", rt, user_id="A") or ""
    check("номер из ответа — вопрос «да/нет» перед вводом",
          "Номер телефона" in q and not typed(cc)
          and (ag._runs["p1"].get("awaiting") or {}).get("kind")
          == "confirm", q)
    q = ag.feed("p1", "да", rt, user_id="A") or ""
    run = ag._runs.get("p1") or {}
    hist = run.get("history") or []
    check("«да» — введён ровно один раз",
          typed(cc) == ["1111111111"], f"{typed(cc)}")
    p4 = rt.prompts[3] if len(rt.prompts) > 3 else ""
    check("поле в промпте — «filled in, the site marks it invalid» с "
          "текстом ошибки",
          'filled in, the site marks it invalid: "Неверный номер '
          'телефона"' in p4, p4[-2500:])
    check("неактивная «Продолжить» после ввода — «сайт не принял значение, "
          "скажи человеку»",
          any(h.startswith('click "Продолжить" → NOT performed — this '
                           'element is disabled') and "does not accept a "
              "value already entered" in h for h in hist), f"{hist[-4:]}")
    check("тот же номер в заполненное поле — не введён и не спрошен",
          any("this field already holds this value" in h for h in hist)
          and typed(cc) == ["1111111111"]
          and "Неверный номер" in q
          and (run.get("awaiting") or {}).get("kind") == "ask",
          f"{q!r} {hist[-3:]}")

    q = ag.feed("p1", GOOD, rt, user_id="A") or ""
    check("другой номер от человека — вопрос «да/нет»",
          (ag._runs["p1"].get("awaiting") or {}).get("kind") == "confirm",
          q)
    ag.feed("p1", "да", rt, user_id="A")
    check("и ввод", typed(cc) == ["1111111111", GOOD], f"{typed(cc)}")

    # ── 2. «нет» на шаг — второй раз тот же вопрос не задаётся ──
    print("повтор шага после «нет»:")
    page = {LOGIN: [
        {"idx": 1, "tag": "button", "role": "button", "text": "Удалить заказ"},
        {"idx": 2, "tag": "button", "role": "button", "text": "Оставить"}]}
    cc = real_cc(page, url=LOGIN, base_dir=tmp)
    ag = TaskAgent(cc, memory_path=tmp / "m2.json")
    ag.slot_router = slot
    rt = ScriptedRouter([
        {"action": "click", "n": 1, "label": "Удалить заказ"},
        {"action": "click", "n": 1, "label": "Удалить заказ"},
        {"action": "ask", "question": "Не удалять — а что сделать?"},
        {"action": "click", "n": 1, "label": "Удалить заказ"},
        {"action": "fail", "message": "стоп"}])
    q = ag.start("p2", "разберись с заказом на dodo.test", rt,
                 user_id="A") or ""
    check("удаление — вопрос «да/нет»",
          "Удалить заказ" in q
          and (ag._runs["p2"].get("awaiting") or {}).get("kind")
          == "confirm", q)
    q = ag.feed("p2", "нет", rt, user_id="A") or ""
    run = ag._runs.get("p2") or {}
    hist = run.get("history") or []
    check("тот же шаг сразу после «нет» — не вопрос, назад модели; она "
          "спрашивает человека",
          any("the user has just said no to exactly this step" in h
              for h in hist)
          and q.startswith("Не удалять")
          and (run.get("awaiting") or {}).get("kind") == "ask"
          and not [d for d in cc.dispatched if d.get("kind") == "click"],
          f"{q!r} {hist[-3:]}")
    q = ag.feed("p2", "всё-таки удали", rt, user_id="A") or ""
    check("после новых слов человека — тот же шаг снова с вопросом",
          (ag._runs["p2"].get("awaiting") or {}).get("kind") == "confirm"
          and "Удалить заказ" in q, q)

    # ── 3. Подписи и сравнение ──
    print("подписи личных полей и сравнение значения:")
    yes = ["Номер телефона", "Телефон", "E-mail", "Электронная почта", "Имя",
           "Фамилия", "Пароль", "Код из SMS", "Код подтверждения",
           "Phone number", "First name", "Verification code", "Email"]
    no = ["Промокод", "Комментарий к заказу", "Улица", "Почтовый индекс",
          "Поиск", "Название компании", "Street name", "Telegram-канал",
          "Hotel"]
    check("личные поля — да",
          all(ta._PERSONAL_FIELD_RE.search(x) for x in yes),
          [x for x in yes if not ta._PERSONAL_FIELD_RE.search(x)])
    check("промокод, адрес, поиск — нет",
          not any(ta._PERSONAL_FIELD_RE.search(x) for x in no),
          [x for x in no if ta._PERSONAL_FIELD_RE.search(x)])
    check("телефон — по цифрам: «+7 999 123-45-67» = «89991234567»",
          ta._value_in("+7 999 123-45-67", ["мой 89991234567"])
          and ta._value_in("9991234567", ["+7 (999) 123-45-67"])
          and not ta._value_in("+79001234567", ["Додо пицца", "1"]))
    check("имя — каждым словом: «Иван» есть, «Иван Петров» — нет; падеж "
          "не мешает («Маргариту» → «Маргарита»)",
          ta._value_in("иван", ["меня зовут Иван"])
          and not ta._value_in("Иван Петров", ["Иван"])
          and ta._value_in("Маргарита", ["запиши на Маргариту"])
          and not ta._value_in("Марина", ["Маргариту"]))
    check("пароль и почта — только точно",
          ta._value_in("Kotik2019!", ["пароль Kotik2019!"])
          and not ta._value_in("Kotik2020!", ["пароль Kotik2019!"])
          and not ta._value_in("ivan@mail.ru", ["ivan@mail.com"]))
    line = TaskAgent._elem_line(1, {
        "idx": 1, "tag": "input", "text": "Номер телефона", "ed": 1,
        "fv": 1, "iv": 1, "er": "Номер +7 999 123-45-67 не обслуживается"})
    check("строка поля: заполнено, неверно, номер в тексте ошибки — маской",
          "input field, filled in, the site marks it invalid" in line
          and "123-45" not in line and "не обслуживается" in line, line)
    line = TaskAgent._elem_line(1, {"idx": 1, "tag": "input",
                                    "text": "Имя", "ed": 1})
    check("пустое поле — без «filled in»", "filled in" not in line
          and "invalid" not in line, line)

    print(f"\nИтого: {OK} проверок, {FAILS} провалов")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

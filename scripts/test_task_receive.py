"""Тест окна способа получения после «В корзину» и сверки корзины
(task_agent: _receive_options, _note_cart, _receive_step, _receive_close,
_receive_pick, _repeat_click, _plan_lines, _cart_empty_note).

Живой прогон 29.10 (dodopizza, «закажи пиццу»): после «В корзину» сайт
открывал окно «Указать адрес доставки / Забрать из пиццерии / Войти /
закрыть» — товар ложится только после выбора. Модель жала «закрыть»
(товар не добавлен), страж повтора принял крестик окна товара за тот же
«закрыть» и спросил человека «уже нажата — отменит выбор?», план до конца
писал «✓ it is in the cart» при пустой корзине. 34 шага, потолок вызовов,
корзина пуста.

Проверяет:
* окно способа получения после «В корзину» — не добавлено, вопрос человеку
  кодом до хода модели (варианты — кнопки окна), голый выбор — без модели;
* Escape и крестик этого окна — сначала назад модели, потом «да» человека;
* пиццерию из списка и «Выбрать» модель сама не жмёт (29.10 взяла первую —
  «Геодезическая, 4/1 Закрыто»): назад ей, она спрашивает человека; пункт,
  названный в ответе, — без вопроса; адрес в окне — только его;
* выбор человека кликами в окне — рост корзины засчитан товару, окно
  позади, план ✓, «что-нибудь ещё?» кодом;
* шторка корзины («Бесконтактная доставка», «К оформлению») и выросшая
  корзина — не окно способа получения;
* два разных «закрыть» — не повтор переключателя; опция — по-прежнему он;
* план: нажатие без подтверждения корзиной — не «it is in the cart»;
* прочитана пустая корзина («Пока тут пусто») — неподтверждённые
  добавления сняты, сверенные и лежавшие до задачи — нет;
* английские реплики.

Браузер — настоящий гейт ComputerControlManager с заглушкой _dispatch
(real_cc), модель — ScriptedRouter (test_task_agent): живых браузеров,
веб-чатов и LLM нет.

Запуск: PYTHONPATH=. python3 scripts/test_task_receive.py
"""

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
MENU = f"https://{SITE}/nsk"
PROD = f"https://{SITE}/nsk/product/dodster"


def hdr(text, **kw):
    # Кнопка корзины в шапке: «Корзина» без чисел — пуста
    return dict({"idx": 7, "tag": "button", "role": "button", "text": text,
                 "aria": "Корзина"}, **kw)


CARD = {"idx": 20, "tag": "a", "role": "link",
        "text": "Острый Додстер от 239 ₽"}
PROD_ITEMS = [
    {"idx": 1, "tag": "button", "role": "button", "text": "Закрыть",
     "md": 1, "ctx": "Острый Додстер Горячая закуска"},
    {"idx": 6, "tag": "button", "role": "button",
     "text": "В корзину за 239 ₽", "md": 1, "ctx": "Острый Додстер"}]
# Окно способа получения поверх окна товара (окно товара — под ним)
RECV_ITEMS = [
    {"idx": 31, "tag": "button", "role": "button",
     "text": "Указать адрес доставки", "md": 1},
    {"idx": 32, "tag": "button", "role": "button",
     "text": "Забрать из пиццерии", "md": 1},
    {"idx": 33, "tag": "button", "role": "button", "text": "Войти", "md": 1},
    {"idx": 34, "tag": "button", "role": "button", "text": "закрыть",
     "md": 1, "ctx": "Острый Додстер Горячая закуска"}]
# Список пиццерий после «Забрать из пиццерии» (живой прогон 29.10: модель
# сама взяла первую — «Геодезическая, 4/1 Закрыто» — и нажала «Выбрать»)
PICK_ITEMS = [
    {"idx": 41, "tag": "label", "role": "", "md": 1, "on": 0,
     "text": "Новосибирск улица Геодезическая, 4/1 Закрыто"},
    {"idx": 42, "tag": "label", "role": "", "md": 1, "on": 0,
     "text": "Новосибирск Красный проспект, 101 Открыто до 23:00"},
    {"idx": 43, "tag": "button", "role": "button", "text": "Выбрать",
     "md": 1}]


def dodo_cc(base_dir):
    """Додо из живого прогона: меню → окно товара → «В корзину» → окно
    способа получения → «Забрать из пиццерии» → пиццерия → «Выбрать» →
    товар в корзине («Корзина 239 ₽ 1»). Шапка под окнами — перекрыта
    (cov)."""
    cc = real_cc({}, url=MENU, base_dir=base_dir)
    cc.state, cc.point = "menu", None

    def _snap(site_word, chat_id="", auto_dismiss=False):
        st = cc.state
        if st == "menu":
            page = [hdr("Корзина"), CARD]
        elif st == "added":
            page = [hdr("Корзина 239 ₽ 1"), CARD]
        else:
            under = [dict(x, cov=1) for x in PROD_ITEMS]
            pick = [dict(x, on=int(x["idx"] == cc.point))
                    if x.get("on") is not None else x for x in PICK_ITEMS]
            page = [hdr("Корзина", cov=1)] + (
                PROD_ITEMS if st == "prod" else under + (
                    RECV_ITEMS if st == "recv" else pick))
        url = MENU if st in ("menu", "added") else PROD
        cc.cur = url
        return url, SITE, [dict(x) for x in page], 1, None

    def _disp(action, router=None):
        cc.dispatched.append(dict(action))
        idx = action.get("idx")
        if action["kind"] == "key" and action.get("key") == "Escape":
            cc.state = {"recv": "prod", "pick": "prod"}.get(cc.state,
                                                            "menu")
        elif action["kind"] != "click":
            return
        elif idx == 20:
            cc.state = "prod"
        elif idx == 6:
            cc.state = "recv"
        elif idx == 34:
            cc.state = "prod"
        elif idx == 32:
            cc.state = "pick"
        elif idx in (41, 42):
            cc.point = idx
        elif idx == 43 and cc.point:
            cc.state = "added"
        elif idx == 1:
            cc.state = "menu"
    cc._snapshot_for, cc._dispatch = _snap, _disp
    return cc


def main():
    from app.features import browser_actions as _ba
    from app.features import cc_privacy as _cp
    from app.features import cc_texts
    from app.features import task_agent as ta
    from app.features.task_agent import TaskAgent

    _ba.wait_dom_idle = lambda *a, **k: None
    _ba.section_items = lambda *a, **k: None
    _ba.section_names = lambda *a, **k: None
    _ba.snapshot_for_goal = lambda host, text, tab_id=None: ("", [])
    _cp.PrivateRouter.get_response = lambda self, msgs, **kw: None
    tmp = Path(tempfile.mkdtemp(prefix="task_receive_"))

    def clicks(cc):
        return [d.get("idx") for d in cc.dispatched
                if d.get("kind") == "click"]

    # ── 1. Живой прогон 29.10 ──
    print("окно способа получения (dodopizza 29.10):")
    cc = dodo_cc(tmp)
    ag = TaskAgent(cc, memory_path=tmp / "m1.json")
    rt = ScriptedRouter([
        {"action": "click", "n": 2, "label": "Острый Додстер от 239 ₽"},
        {"action": "click", "n": 2, "label": "В корзину за 239 ₽"},
        # после ответа человека модель всё равно закрывает окно
        {"action": "key", "key": "Escape"},
        {"action": "click", "n": 4, "label": "закрыть"},
        # «нет» на «закрыть?» — выбор человека
        {"action": "click", "n": 2, "label": "Забрать из пиццерии"},
        # пиццерию модель берёт сама — первую в списке, закрытую
        {"action": "click", "n": 1,
         "label": "Новосибирск улица Геодезическая, 4/1 Закрыто"},
        {"action": "click", "n": 3, "label": "Выбрать"},
        {"action": "ask", "question": "Из какой пиццерии забрать?\n"
                                      "- Геодезическая, 4/1 — закрыто\n"
                                      "- Красный проспект, 101 — открыто "
                                      "до 23:00"},
        # ответ человека
        {"action": "click", "n": 2,
         "label": "Новосибирск Красный проспект, 101 Открыто до 23:00"},
        {"action": "click", "n": 3, "label": "Выбрать"},
        {"action": "fail", "message": "стоп"}])
    q = ag.start("r1", "закажи острый додстер на dodo.test", rt,
                 user_id="A") or ""
    run = ag._runs.get("r1") or {}
    hist = " ".join(run.get("history") or ())
    check("«В корзину» → окно способа получения: не добавлено, в истории "
          "«NOT added yet», корзина задачи пуста",
          "NOT added yet" in hist and not run.get("cart_adds")
          and (run.get("receive") or {}).get("key") == "Острый Додстер",
          f"{run.get('cart_adds')} {run.get('receive')}")
    check("вопрос человеку кодом до хода модели — варианты окна, «Войти» и "
          "«закрыть» не в них",
          len(rt.prompts) == 2
          and "как получить заказ" in q
          and "- Указать адрес доставки\n- Забрать из пиццерии\n" in q
          and "Острый Додстер" in q and "Войти" not in q
          and "закрыть" not in q, q)
    check("окно не закрыто, «В корзину» нажат один раз",
          clicks(cc) == [20, 6], f"{clicks(cc)}")

    slot_calls = []
    ag.slot_router = type("S", (), {"get_response": lambda self, m, **k: (
        slot_calls.append(1) or "{}")})()
    q2 = ag.feed("r1", "2", rt, user_id="A") or ""
    run = ag._runs.get("r1") or {}
    hist = run.get("history") or []
    check("«2» — голый выбор: разобран без модели",
          not slot_calls, f"{len(slot_calls)}")
    p3 = rt.prompts[2] if len(rt.prompts) > 2 else ""
    check("модели: выбор человека в ответах, план — «not in the cart yet», "
          "правило «never close it»",
          "the user picked option (2) Забрать из пиццерии" in p3
          and "not in the cart yet: the site asked how to get the order" in p3
          and "never close it" in p3, p3[-3000:])
    check("Escape на окне способа получения — не нажат, назад модели",
          any(h.startswith("press Escape → NOT performed — this dialog asks "
                           "how to get the order") for h in hist)
          and not [d for d in cc.dispatched if d.get("kind") == "key"],
          f"{hist[-3:]}")
    check("крестик после отбоя — вопрос человеку, не клик",
          "Тогда товар в корзину не ляжет" in q2
          and (run.get("awaiting") or {}).get("kind") == "confirm"
          and 34 not in clicks(cc), q2)
    q3 = ag.feed("r1", "нет", rt, user_id="A") or ""
    run = ag._runs.get("r1") or {}
    hist = run.get("history") or []
    check("«нет» → «Забрать из пиццерии»; пиццерию и «Выбрать» модель сама "
          "не жмёт — назад ей, она спрашивает человека",
          clicks(cc) == [20, 6, 32]
          and sum("the pickup point or the delivery address is the user's "
                  "choice" in h for h in hist) == 2
          and "\nИз какой пиццерии забрать?" in q3
          and (run.get("awaiting") or {}).get("kind") == "ask",
          f"{clicks(cc)} {q3!r} {hist[-3:]}")
    p_pick = rt.prompts[5] if len(rt.prompts) > 5 else ""
    check("пиццерии в промпте модели — с их статусом",
          "Геодезическая, 4/1 Закрыто" in p_pick
          and "Открыто до 23:00" in p_pick, p_pick[-2000:])
    q4 = ag.feed("r1", "на Красном проспекте", rt, user_id="A") or ""
    run = ag._runs.get("r1") or {}
    hist = " ".join(run.get("history") or ())
    adds = run.get("cart_adds") or []
    check("ответ человека → его пиццерия и «Выбрать»: рост корзины засчитан "
          "товару (сверено), окно позади",
          clicks(cc) == [20, 6, 32, 42, 43]
          and [(x["key"], x.get("verified")) for x in adds]
          == [("Острый Додстер", True)]
          and "added: the cart went from empty to 1 item(s), 239" in hist
          and not run.get("receive"), f"{clicks(cc)} {adds} {hist[-400:]}")
    check("всё в корзине — «что-нибудь ещё?» кодом (модель не звалась)",
          "Добавить что-нибудь ещё?" in q4 and len(rt.prompts) == 10
          and (run.get("awaiting") or {}).get("more"),
          f"{q4!r} {len(rt.prompts)} {(run.get('history') or [])[-3:]}")
    check("план: ✓ «Острый Додстер» в корзине, строки об окне нет",
          any(ln.startswith("✓") and "Острый Додстер\" is in the cart" in ln
              for ln in ag._plan_lines(run))
          and not any("how to get the order" in ln
                      for ln in ag._plan_lines(run)),
          f"{ag._plan_lines(run)}")

    # ── 2. Повтор вопроса и закрытие окна человеком ──
    print("второе окно и «да» на закрытие:")
    cc = dodo_cc(tmp)
    ag = TaskAgent(cc, memory_path=tmp / "m2.json")
    rt = ScriptedRouter([
        {"action": "click", "n": 2, "label": "Острый Додстер от 239 ₽"},
        {"action": "click", "n": 2, "label": "В корзину за 239 ₽"},
        {"action": "click", "n": 4, "label": "закрыть"},
        {"action": "click", "n": 4, "label": "закрыть"},
        {"action": "fail", "message": "стоп"}])
    ag.start("r2", "закажи острый додстер на dodo.test", rt, user_id="A")
    q = ag.feed("r2", "самовывоз", rt, user_id="A") or ""
    check("ответ не голым вариантом — дальше модель; крестик — сначала "
          "назад ей, потом вопрос",
          "Тогда товар в корзину не ляжет" in q and 34 not in clicks(cc), q)
    ag.feed("r2", "да", rt, user_id="A")
    run = (ag.__dict__.get("_finished", {}).get("r2") or {}).get("run") \
        or ag._runs.get("r2") or {}
    check("«да» — окно закрыто, план к выбору в нём больше не ведёт; вопрос "
          "о способе в прогоне не повторяется",
          34 in clicks(cc) and run.get("receive_asked")
          and not run.get("receive")
          and sum("как получить заказ" in str(qq)
                  for qq, _a in run.get("qa") or ()) == 1,
          f"{clicks(cc)} {run.get('receive')} {run.get('qa')}")

    # ── 3. Не окно способа получения ──
    print("не окно способа получения:")
    drawer = [
        {"idx": 51, "tag": "label", "role": "", "text": "Бесконтактная доставка",
         "md": 1, "on": 0},
        {"idx": 52, "tag": "button", "role": "button",
         "text": "К оформлению заказа", "md": 1}]
    new = ["Бесконтактная доставка", "К оформлению заказа"]
    check("шторка корзины («Бесконтактная доставка», «К оформлению») — "
          "не оно", ta._receive_options(drawer, new) == [])
    recv = [dict(x) for x in RECV_ITEMS]
    check("кнопки окна, которых не было до клика, — только они",
          ta._receive_options(recv, ["Указать адрес доставки",
                                     "Забрать из пиццерии"])
          == ["Указать адрес доставки", "Забрать из пиццерии"]
          and ta._receive_options(recv, ["Войти"]) == []
          and ta._receive_options([dict(x, md=0) for x in recv],
                                  [x["text"] for x in recv]) == [])
    run = {"history": ["click \"В корзину за 239 ₽\" → ok"], "qa": []}
    ag._note_cart(run, {"label": "В корзину за 239 ₽", "key": "Додстер",
                        "hist_idx": 0},
                  {"cart": {"found": True, "count": 0}},
                  {"cart": {"found": True, "count": 1}}, [],
                  receive=["Указать адрес доставки"])
    check("корзина выросла — добавлено, окно с «доставкой» не держит",
          not run.get("receive")
          and [x["key"] for x in run.get("cart_adds") or ()] == ["Додстер"])

    # ── 4. Два «закрыть» — не повтор переключателя ──
    print("страж повтора:")
    url = PROD
    close_a = {"idx": 34, "text": "закрыть", "md": 1}
    close_b = {"idx": 1, "text": "Закрыть", "md": 1}
    run = {"history": ["click \"закрыть\" → ok | after it: the open dialog "
                       "closed"],
           "clicks": [{"lab": "закрыть", "url": url, "ctx": "", "hist": 0}]}
    obs = {"url": url, "shown": [close_a, close_b]}
    check("29.10: «Закрыть» после «закрыть» по тому же адресу — не повтор",
          TaskAgent._repeat_click(run, close_b, obs) is None)
    check("«× (Закрыть корзину)» — крестик; «Закрытые акции» — нет",
          ta._CLOSE_LABEL_RE.search("× (закрыть корзину)")
          and not ta._CLOSE_LABEL_RE.search("закрытые акции"))
    opt = {"idx": 9, "text": "Халапеньо", "md": 1, "on": 1}
    run = {"history": ["click \"Халапеньо\" → ok"],
           "clicks": [{"lab": "халапеньо", "url": url, "ctx": "", "hist": 0}]}
    check("опция, уже выбранная, — по-прежнему повтор",
          TaskAgent._repeat_click(run, opt, {"url": url, "shown": [opt]})
          == 0)

    # ── 5. План и пустая корзина ──
    print("план и пустая корзина:")
    base = {"goal": "закажи пиццу", "qa": [], "history": [],
            "brief": dict(ta._new_brief(), items=[
                {"name": "Пепперони", "qty": 1, "src": "user"},
                {"name": "Кола", "qty": 1, "src": "user"}])}
    run = dict(base, cart_adds=[
        {"key": "Пепперони", "verified": False, "hist": 1},
        {"key": "Кола", "verified": True, "hist": 2}])
    lines = ag._plan_lines(run)
    check("нажатие без подтверждения — ✓, но «the cart did not confirm it»; "
          "сверенное — «it is in the cart»",
          any("Пепперони" in ln and "did not confirm it" in ln for ln in lines)
          and not any("Пепперони" in ln and "it is in the cart" in ln
                      for ln in lines)
          and any("Кола" in ln and "it is in the cart" in ln for ln in lines),
          f"{lines}")
    run = dict(base, cart_adds=[
        {"key": "Пепперони", "verified": False, "hist": 1},
        {"key": "Кола", "verified": True, "hist": 2},
        {"key": "Соус", "verified": True, "pre": True, "hist": 3}])
    note = ag._cart_empty_note(run, "закрыть\nПока тут пусто\nДобавьте "
                                    "пиццу. Или две!")
    check("«Пока тут пусто» — неподтверждённое снято, сверенное и лежавшее "
          "до задачи — нет",
          [x["key"] for x in run["cart_adds"]] == ["Кола", "Соус"]
          and '"Пепперони" did NOT get into it' in note, note)
    lines = ag._plan_lines(run)
    check("план снова: Пепперони не ✓",
          any("Пепперони" in ln and not ln.startswith("✓") for ln in lines)
          and not any("Пепперони" in ln and ln.startswith("✓")
                      for ln in lines), f"{lines}")
    run = dict(base, cart_adds=[{"key": "Пепперони", "verified": False}])
    check("текст без пустой корзины — ничего не снято",
          ag._cart_empty_note(run, "Меню\nПепперони от 459 ₽") == ""
          and len(run["cart_adds"]) == 1
          and ag._cart_empty_note(run, "Your cart is empty") != "")

    # ── 6. Пункт самовывоза и адрес — выбор человека ──
    print("пункт самовывоза и адрес:")
    places = ["Новосибирск улица Геодезическая, 4/1 Закрыто",
              "Красный проспект, 101", "Открыто до 23:00", "Main St 5",
              "ТЦ Галерея, Гоголя 13"]
    not_places = ["Пепперони 25 см", "В корзину за 239 ₽", "Закрыть",
                  "Открыть карту", "Кофе 0,3 л", "Забрать из пиццерии",
                  "Показать списком"]
    check("подписи пунктов (адрес, статус) — да",
          all(ta._PLACE_RE.search(x) for x in places),
          [x for x in places if not ta._PLACE_RE.search(x)])
    check("размер, цена, крестик, «Открыть карту» — нет",
          not any(ta._PLACE_RE.search(x) for x in not_places),
          [x for x in not_places if ta._PLACE_RE.search(x)])
    shown = [dict(x) for x in PICK_ITEMS]
    obs = {"url": PROD, "host": SITE, "shown": shown}

    def pick_run(answer):
        return {"goal": "закажи пиццу", "history": [], "receive_asked": True,
                "qa": [("Как получить заказ?", answer)],
                "receive": {"key": "Додстер", "label": "В корзину",
                            "opts": ["Забрать из пиццерии"], "qa0": 0}}
    run = pick_run("самовывоз с Красного проспекта")
    check("пиццерия названа в ответе о способе — её клик без вопроса, "
          "«Выбрать» после него — тоже",
          ag._receive_pick(run, shown[1], obs, "click") is None
          and ag._receive_pick(run, shown[2], obs, "click") is None)
    run = pick_run("самовывоз, я в Новосибирске")
    check("город есть у всех пиццерий — не выбор: клик — назад модели",
          ag._receive_pick(run, shown[0], obs, "click") == ("progress", None)
          and "user's choice" in run["history"][-1])
    check("кнопка способа получения и клик вне окна — не пункт",
          ag._receive_pick(pick_run("2"), dict(RECV_ITEMS[1]), obs, "c")
          is None
          and ag._receive_pick(pick_run("2"), dict(shown[0], md=0), obs,
                               "c") is None)
    field = {"idx": 50, "tag": "input", "text": "Улица и дом", "ed": 1,
             "md": 1}
    run = pick_run("1")
    out = ag._type_gate(run, "c", field, obs, "Улица и дом", "type",
                        "Ленина 1", "Ленина 1")
    check("адрес, которого человек не давал, — не вводится",
          out == ("progress", None)
          and "the user never gave this value" in run["history"][-1])
    run = pick_run("доставка на Ленина 1")
    check("его адрес — вводится",
          ag._type_gate(run, "c", field, obs, "Улица и дом", "type",
                        "Ленина 1", "Ленина 1") is None)

    # ── 7. Английский ход ──
    print("английский:")
    en = cc_texts.t("task_receive", "en", site=SITE, item="Spicy Dodster",
                    options="- Delivery\n- Pickup")
    check("en: вопрос о способе получения",
          en.startswith("To put \"Spicy Dodster\" into the cart, dodo.test "
                        "asks how you'll get the order:\n- Delivery"), en)
    check("en: «закрыть окно?» — английский шаблон",
          "won't go into the cart" in cc_texts.phrase(
              "x", "task_receive_close", "ru", "en"))

    print(f"\nИтого: {OK} проверок, {FAILS} провалов")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

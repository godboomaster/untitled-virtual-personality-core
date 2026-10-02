"""Тест сценариев (scenario_manager): граница оплаты, валидация шагов, локи.

Проверяет:
* _is_payment — явные словоформы: «оплатить/карта/pay/Мир Pay» ловятся,
  «карточка товара», «картошка», «мир» (обычное слово), «платье», «платформа»,
  географическая «карта города/сайта/метро» — нет; _strip_payment режет
  сценарий на шаге оплаты и не режет на «карточке товара»;
* _validate_steps — единственный валидатор шага: ввод без подписи поля не
  проходит ни из LLM-вывода, ни из rule-based фолбэка, ни при чтении файла;
* время в реплике «уже записываю (с HH:MM)» берётся из app.core.timeutil;
* локи: одновременный «начни записывать» заводит запись один раз;
  автопредложение на одно окно трассы выдаётся один раз; параллельные
  start/cancel/feed не рушат словарь прогонов.

Запуск: PYTHONPATH=. python3 scripts/test_scenario.py
"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    tmp = Path(tempfile.mkdtemp(prefix="scenario_smoke_"))
    os.chdir(tmp)

    from app.features.scenario_manager import ScenarioManager, _is_payment

    # ── 1. Граница оплаты: явные словоформы ──
    for text in ("Оплатить заказ", "оплата онлайн", "перейти к оплате",
                 "картой онлайн", "ввести номер карты", "Visa/Mastercard",
                 "Google Pay", "Мир Pay", "карта «Мир»", "CVV",
                 "Номер карты", "Данные карты", "привязать карту",
                 "кредитная карта", "Карта",  # одиночное «карта» — в спорном
                 # случае лучше отрезать шаг, чем подпустить бота к деньгам
                 "оформить платёж", "Сбербанк Онлайн", "checkout",
                 # английские подписи / шаги, обобщённые LLM по-английски
                 "Pay now", "Proceed to payment", "Card number",
                 "Billing details", "pay by card"):
        check(f"оплата: «{text}» → шаг отрезается", _is_payment(text))
    for text in ("карточка товара", "добавить карточку", "карточка заказа",
                 "картошка фри", "Мир новостей", "мир", "платье летнее",
                 "платформа 3", "картинка профиля", "карта сайта",
                 "карта города", "карта мира", "открыть карту метро",
                 "Add to cart", "Postcards", "Display"):
        check(f"не оплата: «{text}» → шаг остаётся", not _is_payment(text))
    check("оплата: «карта города» рядом с оплатой всё равно оплата",
          _is_payment("карта города и оплата картой"))

    steps = [
        {"op": "open", "url": "https://example.com"},
        {"op": "click", "target": "карточка товара"},
        {"op": "click", "target": "в корзину"},
        {"op": "click", "target": "оплатить картой"},
        {"op": "click", "target": "подтвердить"},
    ]
    cut = ScenarioManager._strip_payment(steps)
    check("_strip_payment: режет на шаге оплаты, а не на «карточке товара»",
          len(cut) == 4 and cut[1]["target"] == "карточка товара"
          and cut[-1]["op"] == "handoff")

    # ── 2. Валидатор шага ──
    v = ScenarioManager._validate_steps
    check("validate: type без field → None (шаг неисполним)",
          v([{"op": "type", "field": "", "value": "москва"}]) is None)
    check("validate: type с пробелами вместо field → None",
          v([{"op": "type", "field": "   ", "value": "москва"}]) is None)
    good = v([{"op": "ask", "slot": "city", "question": "Куда?"},
              {"op": "type", "field": "адрес", "value": "{city}"}])
    check("validate: type с подписью поля и известным слотом → ок",
          good is not None and len(good) == 2)
    check("validate: неизвестный слот → None",
          v([{"op": "type", "field": "адрес", "value": "{city}"}]) is None)
    check("validate: пустой список → None", v([]) is None)

    # ── 3. Тот же валидатор — на чтении файла ──
    base = tmp / "scen_ctx"
    base.mkdir(parents=True, exist_ok=True)
    (base / "scenarios.json").write_text(json.dumps({
        "битый": {"name": "битый", "aliases": [], "created": time.time(),
                  "steps": [{"op": "open", "url": "https://a.test"},
                            {"op": "type", "field": "", "value": "x"}]},
        "целый": {"name": "целый", "aliases": ["закажи пиццу"],
                  "created": time.time(),
                  "steps": [{"op": "open", "url": "https://b.test"},
                            {"op": "click", "target": "в корзину"}]},
    }), encoding="utf-8")
    mgr = ScenarioManager(context="scen_ctx", base_dir=base)
    check("load: сценарий с неисполнимым шагом не поднимается",
          mgr.list_names() == ["целый"])
    check("load: валидный сценарий читается как раньше",
          mgr.find_scenario("закажи пиццу") == "целый")

    # ── 3b. Rule-based фолбэк проходит тот же валидатор ──
    # Без этого его вывод шёл бы в файл без проверок — шаги вида «ввести в
    # поле «»» ломали бы прогон уже у пользователя.
    rb_dir = tmp / "rb_cc"
    rb_dir.mkdir(parents=True, exist_ok=True)
    ts0 = time.time() - 300
    with (rb_dir / "audit.jsonl").open("w", encoding="utf-8") as f:
        for i, rec in enumerate((
            {"kind": "url", "value": "https://d.test"},
            {"kind": "type", "element": "", "text": "москва"},  # поле без подписи
            {"kind": "send"},
        )):
            rec.update({"chat_id": "chat1", "ok": True, "ts": ts0 + i,
                        "host": "d.test"})
            f.write(json.dumps(rec) + "\n")
    rb = ScenarioManager(context="rb_ctx", base_dir=tmp / "rb_ctx",
                         computer_control=SimpleNamespace(base_dir=rb_dir))
    scenario, err = rb.build_from_trace("chat1", "тест", None)
    check("rule-based: ввод в поле без подписи → честный отказ, а не битый файл",
          scenario is None and err is not None and "без подписи" in err)
    check("rule-based: битый сценарий не сохранён", rb.list_names() == [])

    with (rb_dir / "audit.jsonl").open("w", encoding="utf-8") as f:
        for i, rec in enumerate((
            {"kind": "url", "value": "https://d.test"},
            {"kind": "type", "element": "город", "text": "москва"},
            {"kind": "send"},
        )):
            rec.update({"chat_id": "chat1", "ok": True, "ts": ts0 + i,
                        "host": "d.test"})
            f.write(json.dumps(rec) + "\n")
    scenario, err = rb.build_from_trace("chat1", "тест", None)
    check("rule-based: с подписью поля сценарий собирается (ask + type)",
          err is None and scenario is not None
          and [st["op"] for st in scenario["steps"]]
          == ["open", "ask", "type", "send"])

    # ── 4. Локи in-memory состояния ──
    mgr2 = ScenarioManager(context="lock_ctx", base_dir=tmp / "lock_ctx")
    barrier = threading.Barrier(8)
    starts = []

    def _start():
        barrier.wait()
        starts.append(mgr2.record_start("chat1", "тест"))

    threads = [threading.Thread(target=_start) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    fresh = [s for s in starts if "Записываю сценарий" in s]
    check("lock: 8 одновременных «начни записывать» → запись началась один раз",
          len(fresh) == 1 and len(starts) == 8)
    check("recording(): запись видна", mgr2.recording("chat1") is True)
    again = mgr2.record_start("chat1")
    check("record_start: повторный вызов сообщает время начала (timeutil)",
          "Уже записываю (с " in again)
    check("record_stop: запись снимается",
          "отменена" in mgr2.record_stop("chat1")
          and mgr2.recording("chat1") is False)

    # Автопредложение: check-and-set _offered под локом — на одно окно трассы
    # предложение уходит один раз, даже если «спасибо» пришло из двух потоков
    audit_dir = tmp / "cc"
    audit_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with (audit_dir / "audit.jsonl").open("w", encoding="utf-8") as f:
        for i, (kind, val) in enumerate((("url", "https://c.test"),
                                         ("click", "меню"),
                                         ("click", "в корзину"))):
            f.write(json.dumps({"chat_id": "chat1", "kind": kind, "ok": True,
                                "ts": now - 60 + i, "element": val,
                                "value": val, "host": "c.test"}) + "\n")
    mgr3 = ScenarioManager(context="offer_ctx", base_dir=tmp / "offer_ctx",
                           computer_control=SimpleNamespace(base_dir=audit_dir))
    offers = []
    barrier2 = threading.Barrier(8)

    def _offer():
        barrier2.wait()
        offers.append(mgr3.maybe_offer("chat1", "спасибо"))

    threads = [threading.Thread(target=_offer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("lock: 8 одновременных «спасибо» → одно предложение записать сценарий",
          len([o for o in offers if o]) == 1)
    check("maybe_offer: повторный вызов на том же окне → None",
          mgr3.maybe_offer("chat1", "спасибо") is None)

    # Прогон: словарь _runs под локом — параллельные active/cancel не падают
    mgr2._scenarios["тест"] = {"name": "тест", "aliases": [], "created": now,
                               "steps": [{"op": "ask", "slot": "s",
                                          "question": "Что?"},
                                         {"op": "handoff", "message": "всё"}]}
    reply = mgr2.start("тест", "chat1", None)
    check("start: прогон встал на вопросе", "Что?" in reply
          and mgr2.active("chat1") is True)
    errors = []

    def _hammer(fn):
        try:
            for _ in range(50):
                fn()
        except Exception as e:  # KeyError/RuntimeError на словаре прогонов
            errors.append(repr(e))

    threads = [
        threading.Thread(target=_hammer, args=(lambda: mgr2.active("chat1"),)),
        threading.Thread(target=_hammer, args=(lambda: mgr2.cancel("chat1"),)),
        threading.Thread(target=_hammer,
                         args=(lambda: mgr2.feed("chat1", "ответ", None),)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("lock: параллельные active/cancel/feed — без исключений", not errors)

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

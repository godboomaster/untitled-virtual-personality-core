"""Тест гибридного zero-match яруса ComputerControlManager._hybrid_pick
(wide_mode: hybrid) — один vision-вызов: рамки видимых кандидатов на
скриншоте + текстовый список элементов вне экрана, сквозная нумерация.

Покрывает: разбор ответа (_parse_pick_answer), построение кандидатов и
промпта (бюджеты рамок/строк/символов, ранжирование, активный слой, дедуп,
скоуп, hover), договор возврата ((None, None) / (None, meta) / (idx, meta)),
вето label_mismatch и разрушительного, интеграцию в каскад клика
(_resolve_element_pick) и эскалацию шага навигации (_resolve_nav_step),
аудит (conf / wide_mode / offscreen) и счётчики stats.

Запуск: python -m scripts.test_hybrid_pick
(код выхода ненадёжен — грепать вывод на [FAIL])
"""

import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="hybrid_pick_smoke_")

    counts = {"ok": 0, "fail": 0}

    def check(name, cond):
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        counts["ok" if cond else "fail"] += 1

    # Сеть не трогаем (как в test_computer_control)
    import app.core.router as _net_router
    _net_router.internet_available = lambda: True
    _net_router._net_ok, _net_router._net_checked = True, float("inf")
    import app.features.web_search as _net_ws
    _net_ws.internet_available = lambda: True

    import app.features.computer_control as cc
    import app.features.browser_actions as _ba
    from app.features.computer_control import (
        ComputerControlManager, _parse_pick_answer, _cand_line,
        HYBRID_BOX_MAX, HYBRID_TEXT_MAX, HYBRID_PROMPT_MAX)
    from PIL import Image

    CFG = {"confirm": True}

    def make(cfg=None):
        # Свой tmp base_dir на каждый менеджер — аудит не смешивается.
        base = Path(tempfile.mkdtemp(prefix="hybrid_pick_data_"))
        return ComputerControlManager(context="test", config=dict(cfg or CFG),
                                      base_dir=base)

    def _it(idx, tag, text, **kw):
        # Элемент структурированного снапшота (как отдаёт snapshot_elements).
        it = {"idx": idx, "tag": tag, "role": "", "text": text, "aria": "",
              "title": "", "href": "", "w": 40.0, "h": 20.0, "vp": True,
              "vw": 1280.0}
        it.update(kw)
        return it

    # Настоящий JPEG-скриншот — _draw_candidate_boxes реально рисует рамки
    _buf = io.BytesIO()
    Image.new("RGB", (1280, 800), (255, 255, 255)).save(_buf, format="JPEG",
                                                        quality=90)
    JPEG = _buf.getvalue()

    # ── Фейковый роутер: ответ по виду промпта, все вызовы пишутся ──
    def _kind(prompt: str) -> str:
        if "C=<confidence from 0 to 1>" in prompt:
            return "hybrid"
        if "Which of them is" in prompt:
            return "visual"
        if "clickable zones" in prompt:
            return "zones"
        if "Page elements:" in prompt:
            return "wide"
        return "other"

    class FakeRouter:
        def __init__(self, answers=None, vision=True, default="нет"):
            # answers: {kind: str | callable(prompt) -> str}
            self.answers = dict(answers or {})
            self.vision = vision
            self.default = default
            self.img_calls = []   # dict(kind, prompt, img, extra, mime)
            self.text_calls = []  # dict(kind, prompt)

        def supports_vision(self):
            if self.vision == "raise":
                raise RuntimeError("probe failed")
            return self.vision

        def _answer(self, kind, prompt):
            a = self.answers.get(kind, self.default)
            if isinstance(a, BaseException):
                raise a
            return a(prompt) if callable(a) else a

        def get_response_with_image(self, prompt, img, image_mime=None,
                                    extra_image=None, force_provider=None):
            k = _kind(prompt)
            self.img_calls.append({"kind": k, "prompt": prompt, "img": img,
                                   "extra": extra_image, "mime": image_mime})
            return self._answer(k, prompt)

        def get_response(self, messages, **kw):
            prompt = (messages[-1]["content"] if isinstance(messages, list)
                      else str(messages))
            k = _kind(prompt)
            self.text_calls.append({"kind": k, "prompt": prompt})
            return self._answer(k, prompt)

        def kinds(self):
            return [c["kind"] for c in self.img_calls]

        def wide_calls(self):
            return [c for c in self.text_calls if c["kind"] == "wide"]

    _LINE_RE = re.compile(r"^(\d+)\) \[[^\]]*\] (.*)$", re.M)

    def lines_of(prompt):
        # [(номер, подпись-строка)] из промпта.
        return [(int(n), lab) for n, lab in _LINE_RE.findall(prompt)]

    def num_of(prompt, label):
        # Номер строки кандидата, чья подпись начинается с label.
        for n, lab in lines_of(prompt):
            if lab.startswith(label):
                return n
        return None

    # ── Подмена browser_actions (сохраняем оригиналы, в finally — назад) ──
    _saved = {}

    def patch(name, fn):
        if name not in _saved:
            _saved[name] = getattr(_ba, name, None)
        setattr(_ba, name, fn)

    state = {"items": [], "shot": JPEG, "zones": []}
    patch("screenshot_viewport", lambda host=None, tab_id=None: state["shot"])
    patch("snapshot_elements", lambda host=None, tab_id=None: (
        "https://x.ru/", "x.ru", [dict(i) for i in state["items"]]))
    patch("snapshot_for_goal", lambda host, goal, tab_id=None: ("", []))
    patch("list_pages", lambda: [])
    patch("dismiss_overlay", lambda host=None, tab_id=None: None)
    patch("detect_antibot", lambda host=None, tab_id=None, strict=False: None)
    patch("modal_visible", lambda host=None, tab_id=None: False)
    patch("open_list_visible", lambda host=None, tab_id=None: False)
    patch("wait_dom_idle", lambda *a, **kw: None)
    patch("visible_page_info", lambda: None)
    patch("reveal_player_controls", lambda host=None, tab_id=None: None)
    patch("scroll_position", lambda host=None, tab_id=None: 0.0)
    patch("scroll_step", lambda host=None, tab_id=None: {"moved": False,
                                                         "bottom": True})
    patch("scroll_restore", lambda host=None, tab_id=None, y=0.0: None)
    patch("scroll_container_step", lambda host=None, tab_id=None: {"moved": False})
    patch("scroll_container_restore", lambda host=None, tab_id=None, y=0.0: None)
    patch("all_clickable_boxes",
          lambda host=None, tab_id=None: [dict(z) for z in state["zones"]])
    patch("find_tab_id", lambda url: None)
    patch("click_tagged", lambda host, idx, tab_id=None: "clicked")

    def aud(m, chat=None):
        p = m.base_dir / "audit.jsonl"
        if not p.exists():
            return []
        recs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
        return [r for r in recs if chat is None or r.get("chat_id") == chat]

    # Базовая zero-match страница под цель «корзина»: две иконки, «Cart»
    # («другое название»), «Помощь», и две строки вне экрана
    def base_items():
        return [
            _it(0, "button", "", x=1100.0, y=10.0, w=40.0, h=40.0),
            _it(1, "button", "", x=1150.0, y=10.0, w=40.0, h=40.0),
            _it(2, "a", "Cart", x=100.0, y=100.0, w=80.0, h=30.0),
            _it(3, "a", "Помощь", x=300.0, y=100.0, w=80.0, h=30.0),
            _it(4, "a", "Оформление заказа", vp=False, y=2400.0),
            _it(5, "a", "Моя корзина покупок", vp=False, y=2500.0,
                x=10.0),
        ]

    def zero_items():
        # Zero-match под «корзина»: без строки со словом цели.
        return [i for i in base_items() if i["idx"] != 5]

    try:
        # ════════════════ 1. Разбор ответа _parse_pick_answer ════════════════
        print("\n── 1. _parse_pick_answer ──")
        cases = [
            ("7", (7, None, False)),
            ("T=7", (7, None, False)),
            ("t = 7", (7, None, False)),
            ("`7`", (7, None, False)),
            ("**7**", (7, None, False)),
            ("_7_", (7, None, False)),
            ("7.", (7, None, False)),
            (" 7 ", (7, None, False)),
            ("07", (7, None, False)),
            ("7 C=0.8", (7, 0.8, False)),
            ("7, C=0.8", (7, 0.8, False)),
            ("7C=0.8", (7, 0.8, False)),
            ("7 (C=0,8)", (7, 0.8, False)),
            ("7 (C=0.8).", (7, 0.8, False)),
            ("7 c:0.8", (7, 0.8, False)),
            ("7 С=0.8", (7, 0.8, False)),     # кириллическая С
            ("7 с=0,8", (7, 0.8, False)),     # кириллическая строчная
            ("7\nC=0.8", (7, 0.8, False)),
            ("T=7 C=1", (7, 1.0, False)),
            ("7 C=1.0", (7, 1.0, False)),
            ("7 C=0", (7, 0.0, False)),
            ("нет", (None, None, True)),
            ("Нет.", (None, None, True)),
            ("НЕТ", (None, None, True)),
            ("**Нет**", (None, None, True)),
            ("мусор", (None, None, False)),
            ("", (None, None, False)),
            (None, (None, None, False)),
            ("12345", (None, None, False)),
            ("C=0.8", (None, None, False)),
            # Пояснение после номера, «.8», уверенность вне [0, 1] —
            # номер валиден (вне диапазона C — просто без уверенности)
            ("3 — это кнопка корзины", (3, None, False)),
            ("7 C=0.8 кнопка", (7, 0.8, False)),
            ("7 C=1.5", (7, None, False)),
            ("7 C=.8", (7, 0.8, False)),
            ("3 — кнопка «Оплата»", (3, None, False)),
            ("Ответ: 3", (3, None, False)),
            ("answer: 3 C=0.9", (3, 0.9, False)),
            ("3, 0.8", (3, 0.8, False)),
            ("3 C=80%", (3, 0.8, False)),
            ("C=0.8 3", (3, 0.8, False)),
            ("3 (кнопка 3)", (3, None, False)),
            ("3 C=0.", (3, None, False)),      # оборванное число — без C
            ("3 или 5", (None, None, False)),  # два разных номера
            ("3 — 2:43 World Map", (None, None, False)),
            ("3,8", (None, None, False)),
            ("Подходящего нет, возможно 3", (None, None, False)),
            ("Я не вижу подходящего элемента", (None, None, True)),
            # Сомнение или отрицание при номере — невалидно всегда
            ("Не вижу подходящего, возможно 4", (None, None, False)),
            ("Не уверен, но 3", (None, None, False)),
            ("Возможно, 3", (None, None, False)),
            ("I think 3", (None, None, False)),
            ("maybe 3", (None, None, False)),
            ("Наверное 3", (None, None, False)),
            ("Кажется, 2", (None, None, False)),
            # Фраза-отказ без номера — трактуется как «нет»
            ("Подходящего элемента нет", (None, None, True)),
            ("ничего не подходит", (None, None, True)),
            ("Nothing matches", (None, None, True)),
            # Эхо подписи — нестрого номер есть, строго — нет (см. ниже)
            ("Тариф 5 ГБ", (5, None, False)),
            ("Кнопка Войти в 2 клика", (2, None, False)),
            ("Нетфликс", (None, None, False)),  # «нет» — только целым словом
            ("no", (None, None, True)),
            ("None.", (None, None, True)),
            ("0", (0, None, False)),           # разбор даёт 0; диапазон — ярус
        ]
        for s, want in cases:
            got = _parse_pick_answer(s)
            check(f"parse {s!r} → {want}", got == want)
        # Отказ «none»/«No»/«нет, …», префикс «Ответ:», кириллическая «Т=», «нету»
        check("parse: «none» — отказ",
              _parse_pick_answer("none") == (None, None, True))
        check("parse: «No» — отказ",
              _parse_pick_answer("No") == (None, None, True))
        check("parse: «Ответ: 7» — префикс снят, номер 7",
              _parse_pick_answer("Ответ: 7") == (7, None, False))
        check("parse: кириллическая «Т=7» — номер 7",
              _parse_pick_answer("Т=7") == (7, None, False))
        check("parse (наблюдение): «нет, но 3» — отказ («нет» первым словом)",
              _parse_pick_answer("нет, но 3") == (None, None, True))
        check("parse: «нету» — не отказ и не номер (невалидно)",
              _parse_pick_answer("нету") == (None, None, False))
        # Строгая грамматика: «[Ответ:] [T=]N [C=…][.]» — и ничего больше
        strict_cases = [
            ("7", (7, None, False)), ("T=7", (7, None, False)),
            ("**7**", (7, None, False)), ("7.", (7, None, False)),
            ("Ответ: 7", (7, None, False)), ("7 C=0.8", (7, 0.8, False)),
            ("7, C=0,8", (7, 0.8, False)), ("7 (C=0.8).", (7, 0.8, False)),
            ("7C=0.8", (7, 0.8, False)), ("7 C=80%", (7, 0.8, False)),
            ("нет", (None, None, True)),
            ("Подходящего элемента нет", (None, None, True)),
            ("3 — кнопка «Оплата»", (None, None, False)),
            ("Тариф 5 ГБ", (None, None, False)),
            ("Кнопка Войти в 2 клика", (None, None, False)),
            ("3, 0.8", (None, None, False)),
            ("C=0.8 3", (None, None, False)),
            ("Не вижу подходящего, возможно 4", (None, None, False)),
            ("Возможно, 3", (None, None, False)),
            ("I think 3", (None, None, False)),
        ]
        for s_, want in strict_cases:
            check(f"parse strict {s_!r} → {want}",
                  _parse_pick_answer(s_, strict=True) == want)

        # ════════════════ 1b. _cand_line ════════════════
        print("\n── 1b. _cand_line ──")
        check("_cand_line: безымянный → «(без подписи)»",
              _cand_line(3, _it(0, "button", "")) == "3) [button/-] (no label)")
        check("_cand_line: короткая подпись + ctx → «(блок: …)»",
              _cand_line(1, _it(0, "button", "×", ctx="Корзина"))
              == "1) [button/-] × (block: Корзина)")
        check("_cand_line: обрезка подписи lab_max",
              _cand_line(2, _it(0, "a", "я" * 300), 40)
              == "2) [a/-] " + "я" * 40)
        check("_cand_line: пометка слоя (открытое окно) и роль",
              _cand_line(4, _it(0, "div", "Подтвердить заказ", role="button",
                                md=True))
              == "4) [div/button] Подтвердить заказ — in an open dialog")

        # ════════════════ 2. Построение кандидатов и промпта ════════════════
        print("\n── 2. Кандидаты и промпт ──")
        m = make()
        r = FakeRouter({"hybrid": "нет"})
        m._hybrid_pick("корзина", base_items(), "x.ru", 7, r)
        p0 = r.img_calls[0]["prompt"] if r.img_calls else ""
        c0 = r.img_calls[0] if r.img_calls else {}
        check("промпт: ровно один vision-вызов", len(r.img_calls) == 1
              and r.kinds() == ["hybrid"] and not r.text_calls)
        check("промпт: картинка с рамками — JPEG, отличается от скриншота; "
              "второй кадр — чистый скриншот",
              bool(c0) and c0["img"][:2] == b"\xff\xd8" and c0["img"] != JPEG
              and c0["extra"] == JPEG and c0["mime"] == "image/jpeg")
        ln = lines_of(p0)
        check("промпт: нумерация сквозная 1..N без пропусков",
              [n for n, _ in ln] == list(range(1, len(ln) + 1)) and len(ln) == 6)
        check("промпт: рамки 1..4, строки 5..6 «вне экрана»",
              "elements 1..4" in p0 and "(1-6)" in p0
              and "Elements without a box (mostly below the screen):" in p0
              and num_of(p0, "Оформление заказа") in (5, 6)
              and num_of(p0, "Моя корзина покупок") in (5, 6))
        check("промпт: безымянные иконки получили рамки первыми (1, 2)",
              [lab for n, lab in ln[:2]] == ["(no label)", "(no label)"])
        check("промпт: строка вне экрана без пометки «на экране»",
              "Оформление заказа — on screen" not in p0)
        check("промпт: релевантная строка (со словом цели) первой среди строк",
              num_of(p0, "Моя корзина покупок") == 5)

        # Номер → правильный idx (перебираем каждый номер с уверенностью)
        items_map = base_items()
        want_by_label = {"Cart": 2, "Помощь": 3, "Оформление заказа": 4,
                         "Моя корзина покупок": 5}
        icon_nums = [n for n, lab in ln if lab == "(no label)"]
        ok_map = True
        for n, lab in ln:
            mm = make()
            # Проверяется только соответствие номер → idx: сверка подписи
            # (рамки «Cart»/«Помощь» для «корзина» ветируются) отключена
            mm._veto_model_pick = lambda *a, **kw: False
            rr = FakeRouter({"hybrid": f"{n} C=0.9"})
            idx, meta = mm._hybrid_pick("корзина", items_map, "x.ru", 7, rr)
            if lab == "(no label)":
                want = {1: 0, 2: 1}[icon_nums.index(n) + 1]
            else:
                want = want_by_label[lab]
            if idx != want:
                ok_map = False
                print(f"     номер {n} ({lab}) → idx {idx}, ждали {want}")
        check("номер ответа → DOM-idx своего кандидата (все номера)", ok_map)

        # Бюджеты: >12 видимых, >20 вне экрана
        many = [_it(i, "a", f"Раздел {chr(0x410 + i)}", x=float(10 + (i % 12) * 100),
                    y=float(50 + (i // 12) * 60), w=90.0, h=40.0)
                for i in range(20)]
        many += [_it(100 + i, "a", f"Внизу пункт {i}", vp=False,
                     y=float(3000 + i * 50)) for i in range(40)]
        m = make()
        r = FakeRouter({"hybrid": "нет"})
        _, meta = m._hybrid_pick("корзина", many, "x.ru", None, r)
        p = r.img_calls[0]["prompt"]
        n_on_screen_rows = p.count(" — on screen, no box")
        check(f"бюджет: рамок ≤ {HYBRID_BOX_MAX}, строк ≤ {HYBRID_TEXT_MAX}",
              meta["n_boxes"] == HYBRID_BOX_MAX
              and meta["n_text"] == HYBRID_TEXT_MAX
              and f"elements 1..{HYBRID_BOX_MAX}" in p
              and f"(1-{HYBRID_BOX_MAX + HYBRID_TEXT_MAX})" in p
              and len(lines_of(p)) == HYBRID_BOX_MAX + HYBRID_TEXT_MAX)
        check("бюджет: видимые без рамки идут строками с пометкой «на экране»",
              n_on_screen_rows == 20 - HYBRID_BOX_MAX)
        check("мета: candidates — первые 10, флаг off у всех False (12 рамок)",
              len(meta["candidates"]) == 10
              and all(c["off"] is False for c in meta["candidates"]))

        # Ранжирование: частичный матч (ctx) и безымянная иконка — в рамки
        # раньше крупных посторонних
        big = [_it(i, "div", f"Большой баннер {i}", x=float(i * 90),
                   y=200.0, w=85.0, h=300.0) for i in range(14)]
        partial = _it(50, "button", "Оформить", ctx="Корзина товаров",
                      x=10.0, y=600.0, w=40.0, h=20.0)
        icon = _it(51, "button", "", x=60.0, y=600.0, w=20.0, h=20.0)
        sc = ComputerControlManager._score_candidates(big + [partial, icon],
                                                      "корзина", host="x.ru")
        check("ранжирование (предусловие): частичный матч имеет балл, "
              "крупные — нет",
              [int(i["idx"]) for _, i in sc] == [50])
        m = make()
        r = FakeRouter({"hybrid": "нет"})
        m._hybrid_pick("корзина", big + [partial, icon], "x.ru", None, r)
        p = r.img_calls[0]["prompt"]
        check("ранжирование: частичный матч — рамка 1, иконка — рамка 2",
              num_of(p, "Оформить (block:") == 1
              and num_of(p, "(no label)") == 2)
        check("ранжирование: не влезшие в рамки крупные — строками",
              p.count(" — on screen, no box") == 14 - (HYBRID_BOX_MAX - 2))

        # Активный слой: под бэкдропом (sc=False) и перекрытые (cov) — вон
        layer = [_it(0, "button", "Закрыть окно", md=True, x=500.0, y=300.0),
                 _it(1, "a", "Корзина в модалке нет", md=True, x=600.0, y=300.0),
                 _it(2, "a", "Под бэкдропом", sc=False, x=10.0, y=10.0),
                 _it(3, "a", "Перекрыт слоем", cov=True, x=10.0, y=100.0),
                 _it(4, "a", "Под бэкдропом вне экрана", sc=False, vp=False,
                     y=3000.0)]
        m = make()
        r = FakeRouter({"hybrid": "нет"})
        m._hybrid_pick("оплата", layer, "x.ru", None, r)
        p = r.img_calls[0]["prompt"]
        check("_active_layer: sc=False и cov=True не попадают ни в рамки, "
              "ни в строки",
              "Под бэкдропом" not in p and "Перекрыт слоем" not in p
              and "Закрыть окно" in p and len(lines_of(p)) == 2)

        # Дедуп дублей одной карточки: дубль не повторяется строкой
        dup = [_it(0, "a", "Карточка товара длинная", href="https://x.ru/p/1",
                   x=100.0, y=100.0, w=300.0, h=200.0),
               _it(1, "a", "Карточка товара длинная", href="https://x.ru/p/1",
                   x=110.0, y=250.0, w=200.0, h=20.0),
               _it(2, "a", "Другая карточка товара", href="https://x.ru/p/2",
                   x=600.0, y=100.0, w=300.0, h=200.0)]
        m = make()
        r = FakeRouter({"hybrid": "нет"})
        _, meta = m._hybrid_pick("корзина", dup, "x.ru", None, r)
        p = r.img_calls[0]["prompt"]
        check("дедуп: дубль карточки — одна рамка, строкой не повторён",
              p.count("Карточка товара длинная") == 1
              and meta["n_boxes"] == 2 and meta["n_text"] == 0
              and {c["idx"] for c in meta["candidates"]} == {0, 2})

        # Лимит промпта: 12 рамок + полный бюджет строк с подписями по 300
        # символов — подписи обрезаны (рамки 60, строки 120), строки все влезли
        longl = [_it(i, "a", f"{i:02d}" + "ж" * 298, x=float(10 + i * 100),
                     y=50.0, w=90.0, h=40.0) for i in range(12)]
        longl += [_it(100 + i, "a", f"{i:02d}" + "щ" * 298, vp=False,
                      y=float(3000 + i * 40))
                  for i in range(HYBRID_TEXT_MAX + 5)]
        m = make()
        r = FakeRouter({"hybrid": "нет"})
        _, meta = m._hybrid_pick("корзина", longl, "x.ru", None, r)
        p = r.img_calls[0]["prompt"]
        check(f"лимит: 12 рамок + {HYBRID_TEXT_MAX} строк по 300 символов → "
              f"промпт ≤ {HYBRID_PROMPT_MAX} ({len(p)})",
              len(p) <= HYBRID_PROMPT_MAX and meta["n_text"] == HYBRID_TEXT_MAX
              and meta["n_boxes"] == HYBRID_BOX_MAX
              and "ж" * 61 not in p and "щ" * 121 not in p)
        # Худший случай (длинные custom-теги/роли, пометки слоя, длинная
        # цель): подписи ужаты, хвост строк отброшен, нумерация сквозная
        LW = "Ж" * 200

        def _heavy(i, vp):
            return _it(i, "ytd-thumbnail-overlay-toggle-button-renderer", LW,
                       role="menuitemcheckbox", href=f"/h{i}", dd=True,
                       w=100.0, h=40.0, x=float(i % 10) * 120,
                       y=float(i // 10) * 60, vp=vp)
        heavy = [_heavy(i, True) for i in range(40)] \
            + [_heavy(100 + i, False) for i in range(40)]
        for goal in ("кнопка", "закрыть на " + "x" * 300, "ы" * 7000):
            m = make()
            r = FakeRouter({"hybrid": "нет"})
            _, meta = m._hybrid_pick(goal, heavy, "x.ru", None, r)
            p = r.img_calls[0]["prompt"]
            nums = [n for n, _ in lines_of(p)]
            check(f"лимит (худший случай, цель {len(goal)} симв.): ≤ "
                  f"{HYBRID_PROMPT_MAX} ({len(p)}, строк {meta['n_text']})",
                  len(p) <= HYBRID_PROMPT_MAX
                  and 0 < meta["n_text"] < HYBRID_TEXT_MAX
                  and nums == list(range(1, HYBRID_BOX_MAX + meta["n_text"] + 1))
                  and f"(1-{HYBRID_BOX_MAX + meta['n_text']})" in p)
        # Цель в промпте — не длиннее 200 символов (бюджет жёсткий)
        m = make()
        r = FakeRouter({"hybrid": "нет"})
        m._hybrid_pick("ы" * 7000, longl, "x.ru", None, r)
        p = r.img_calls[0]["prompt"]
        check("лимит: цель обрезана до 200 символов с «…»",
              "ы" * 200 + "…" in p and "ы" * 201 not in p
              and len(p) <= HYBRID_PROMPT_MAX)

        # Скоуп-подсказка
        m = make()
        r = FakeRouter({"hybrid": "нет"})
        m._hybrid_pick("закрыть на корзина",
                       [_it(0, "button", "×", ctx="Корзина", x=10.0, y=10.0,
                            w=20.0, h=20.0),
                        _it(1, "a", "Каталог", x=100.0, y=10.0)],
                       "x.ru", None, r)
        p = r.img_calls[0]["prompt"]
        check("скоуп: «закрыть на корзина» — подсказка про элемент «закрыть» "
              "и контекст блока у «×»",
              'Task: click "закрыть на корзина" (the element "закрыть" '
              'belonging to "корзина"; it may be labelled just "закрыть")'
              in p and "× (block: Корзина)" in p)

        # op=hover — формулировка задачи
        m = make()
        r = FakeRouter({"hybrid": "нет"})
        m._hybrid_pick("корзина", base_items(), "x.ru", None, r, op="hover")
        p = r.img_calls[0]["prompt"]
        check("hover: «Задача: навести курсор на «корзина»»",
              p.startswith('Task: hover the cursor over "корзина".')
              and "click" not in p.split("\n")[0])

        # llm_wide_resolve=false — только рамки, без текстового списка
        m = make({**CFG, "llm_wide_resolve": False})
        r = FakeRouter({"hybrid": "нет"})
        _, meta = m._hybrid_pick("корзина", base_items(), "x.ru", None, r)
        check("llm_wide_resolve=false: строк нет, только рамки",
              meta is not None and meta["n_text"] == 0 and meta["n_boxes"] == 4
              and "Elements without a box" not in r.img_calls[0]["prompt"])

        # Палитра 12 цветов: бейджи 12 рамок — 12 разных цветов палитры
        pal = [(220, 38, 38), (37, 99, 235), (5, 150, 105), (217, 119, 6),
               (147, 51, 234), (219, 39, 119), (8, 145, 178), (234, 88, 12),
               (101, 163, 13), (120, 72, 30), (23, 23, 23), (107, 114, 128)]
        pcands = [{"x": 20.0 + 100 * i, "y": 200.0, "w": 60.0, "h": 60.0,
                   "vw": 1280.0} for i in range(12)]
        drawn = cc._draw_candidate_boxes(JPEG, pcands)
        pal_ok = drawn is not None
        if drawn is not None:
            img = Image.open(io.BytesIO(drawn)).convert("RGB")
            for i, c in enumerate(pcands):
                # Середина левой стороны рамки (толщина ≥ 3 px), усреднение
                xs, ys = int(c["x"]) + 1, int(c["y"] + c["h"] / 2)
                px = [img.getpixel((xs + dx, ys + dy))
                      for dx in (0, 1) for dy in (-2, -1, 0, 1, 2)]
                avg = tuple(sum(p_[k] for p_ in px) / len(px) for k in range(3))
                near = min(range(12), key=lambda j: sum(
                    (avg[k] - pal[j][k]) ** 2 for k in range(3)))
                if near != i:
                    pal_ok = False
                    print(f"     рамка {i + 1}: цвет {avg} ближе к палитре "
                          f"#{near + 1}")
        check("палитра: 12 рамок — 12 разных цветов по порядку", pal_ok)

        # ════════════════ 3. Договор возврата ════════════════
        print("\n── 3. Договор возврата ──")

        def run(goal, items, answers=None, cfg=None, router="default",
                op="click", shot=JPEG):
            mm = make(cfg)
            rr = FakeRouter(answers or {}) if router == "default" else router
            state["shot"] = shot
            try:
                res = mm._hybrid_pick(goal, items, "x.ru", None, rr, op=op)
            finally:
                state["shot"] = JPEG
            return res, rr, mm

        (i_, m_), rr, _ = run("корзина", base_items(), {"hybrid": "2"},
                              cfg={**CFG, "vision_fallback": False})
        check("(None, None): vision_fallback=false, vision не зовётся",
              (i_, m_) == (None, None) and not rr.img_calls)
        (i_, m_), _, _ = run("корзина", base_items(), router=None)
        check("(None, None): router=None", (i_, m_) == (None, None))
        (i_, m_), rr, _ = run("корзина", base_items(),
                              router=FakeRouter({"hybrid": "2"}, vision=False))
        check("(None, None): supports_vision() → False",
              (i_, m_) == (None, None) and not rr.img_calls)
        (i_, m_), rr, _ = run("корзина", base_items(),
                              router=FakeRouter({"hybrid": "2"}, vision="raise"))
        check("(None, None): supports_vision() бросает",
              (i_, m_) == (None, None) and not rr.img_calls)
        (i_, m_), rr, _ = run("корзина", base_items(), {"hybrid": "2"},
                              shot=None)
        check("(None, None): скриншот None", (i_, m_) == (None, None)
              and not rr.img_calls)

        def _boom(host=None, tab_id=None):
            raise RuntimeError("cdp down")
        patch("screenshot_viewport", _boom)
        (i_, m_), rr, _ = run("корзина", base_items(), {"hybrid": "2"})
        patch("screenshot_viewport",
              lambda host=None, tab_id=None: state["shot"])
        check("(None, None): скриншот бросает", (i_, m_) == (None, None)
              and not rr.img_calls)
        (i_, m_), rr, _ = run("корзина", base_items(), {"hybrid": "2"},
                              shot=b"not an image")
        check("(None, None): Pillow не разобрал скриншот",
              (i_, m_) == (None, None) and not rr.img_calls)
        offonly = [_it(0, "a", "Оформление", vp=False, y=3000.0),
                   _it(1, "button", "", w=5.0, h=5.0),
                   _it(2, "a", "Пиксель", w=8.0, h=30.0)]
        (i_, m_), rr, _ = run("корзина", offonly, {"hybrid": "1"})
        check("(None, None): нет видимых кандидатов под рамку "
              "(вне экрана/мелкие)", (i_, m_) == (None, None)
              and not rr.img_calls)
        (i_, m_), rr, mm = run("корзина", base_items(),
                               {"hybrid": RuntimeError("vision 500")})
        check("(None, None): ошибка vision-вызова; vision_calls не растёт",
              (i_, m_) == (None, None) and len(rr.img_calls) == 1
              and mm.stats["vision_calls"] == 0)

        (i_, m_), _, _ = run("корзина", base_items(), {"hybrid": "нет"})
        check("(None, meta): «нет» — meta vision_hybrid, conf=None, без вето",
              i_ is None and m_ is not None and m_["path"] == "vision_hybrid"
              and m_["conf"] is None and m_["wide_mode"] == "hybrid"
              and m_["llm_response"] == "нет" and not m_.get("veto"))
        (i_, m_), _, _ = run("корзина", base_items(), {"hybrid": "мусор"})
        check("(None, meta): невалидный ответ",
              i_ is None and m_ is not None and m_["path"] == "vision_hybrid"
              and m_["llm_response"] == "мусор")
        (i_, m_), _, _ = run("корзина", base_items(), {"hybrid": "9 C=0.9"})
        check("(None, meta): номер вне диапазона (9 при 6)",
              i_ is None and m_ is not None and m_["path"] == "vision_hybrid")
        (i_, m_), _, _ = run("корзина", base_items(), {"hybrid": "0"})
        check("(None, meta): номер 0", i_ is None and m_ is not None)

        destr = [_it(0, "button", "Удалить", x=10.0, y=10.0),
                 _it(1, "a", "Каталог", x=100.0, y=10.0)]
        (i_, m_), rr, _ = run("открыть", destr, {
            "hybrid": lambda p: f"{num_of(p, 'Удалить')} C=0.95"})
        check("(None, meta): вето разрушительного («Удалить» для «открыть»)",
              i_ is None and m_ is not None and m_.get("veto") == "destructive"
              and m_.get("wide_mode") == "hybrid")
        check("(наблюдение): destructive-вето переписывает path на «none»",
              m_ is not None and m_.get("path") == "none")
        (i_, m_), _, _ = run("очистить очередь",
                             [_it(0, "button", "Очистить очередь", x=10.0,
                                  y=10.0), _it(1, "a", "Каталог", x=100.0)],
                             {"hybrid": lambda p: f"{num_of(p, 'Очистить')}"},
                             op="hover")
        check("hover: разрушительный контрол не ветируется",
              i_ == 0 and m_ is not None and not m_.get("veto"))

        (i_, m_), _, _ = run("корзина", base_items(), {"hybrid": "2 C=0.9"})
        check("(idx, meta): иконка из рамки, C=0.9 — принята, мета полная",
              i_ == 1 and m_["path"] == "vision_hybrid" and m_["conf"] == 0.9
              and m_["wide_mode"] == "hybrid" and m_["n_boxes"] == 4
              and m_["n_text"] == 2 and not m_.get("offscreen")
              and not m_.get("veto"))
        check("мета: candidates с флагом off (4 рамки + 2 строки)",
              [c["off"] for c in m_["candidates"]]
              == [False] * 4 + [True] * 2
              and all({"idx", "text", "score", "off"} <= set(c)
                      for c in m_["candidates"]))
        (i_, m_), _, _ = run("корзина", base_items(), {"hybrid": "2"})
        check("(idx, meta): безымянная иконка без C — принята",
              i_ == 1 and m_["conf"] is None and not m_.get("veto"))
        (i_, m_), _, _ = run("оплата", base_items(), {
            "hybrid": lambda p: f"{num_of(p, 'Оформление заказа')} C=0.8"})
        check("(idx, meta): строка вне экрана — offscreen=True",
              i_ == 4 and m_.get("offscreen") is True and m_["conf"] == 0.8)

        (i_, m_), _, _ = run("корзина", base_items(), {
            "hybrid": lambda p: f"{num_of(p, 'Помощь')}"})
        check("label_mismatch: чужая подпись без C — вето",
              i_ is None and m_ is not None
              and m_.get("veto") == "label_mismatch"
              and m_["path"] == "vision_hybrid")
        (i_, m_), _, _ = run("корзина", base_items(), {
            "hybrid": lambda p: f"{num_of(p, 'Помощь')} C=0.3"})
        check("label_mismatch: чужая подпись с C=0.3 — вето, conf в мете",
              i_ is None and m_.get("veto") == "label_mismatch"
              and m_["conf"] == 0.3)
        # Уверенность не освобождает от сверки подписи — ни C=0.95, ни «другое
        # название» рамкой («Cart» для «корзина» — это дело широкого резолва)
        (i_, m_), _, _ = run("корзина", base_items(), {
            "hybrid": lambda p: f"{num_of(p, 'Помощь')} C=0.95"})
        check("N2: рамка с чужой подписью, C=0.95 — всё равно вето",
              i_ is None and m_.get("veto") == "label_mismatch"
              and m_["conf"] == 0.95)
        (i_, m_), _, _ = run("корзина", base_items(), {
            "hybrid": lambda p: f"{num_of(p, 'Cart')} C=0.9"})
        check("N2: «Cart» рамкой для «корзина» с C=0.9 — вето (дальше широкий)",
              i_ is None and m_.get("veto") == "label_mismatch")
        (i_, m_), _, _ = run("корзина", base_items(), {
            "hybrid": lambda p: f"{num_of(p, 'Моя корзина')}"})
        check("без вето: подпись со словом цели, без C (и вне экрана)",
              i_ == 5 and not m_.get("veto") and m_.get("offscreen") is True)
        (i_, m_), _, _ = run("корзину", base_items(), {
            "hybrid": lambda p: f"{num_of(p, 'Моя корзина')} C=0.1"})
        check("без вето: слово цели по стему («корзину»), C=0.1",
              i_ == 5 and not m_.get("veto"))

        # ════════════════ 7. stats ════════════════
        print("\n── 7. stats ──")
        m = make()
        s0 = dict(m.stats)
        seq = [("2", "valid"), ("нет", "valid"), ("мусор", "invalid"),
               ("42", "invalid"),
               (lambda p: f"{num_of(p, 'Помощь')}", "valid")]  # вето — valid
        st_ok = True
        for ans, kind in seq:
            before = dict(m.stats)
            m._hybrid_pick("корзина", base_items(), "x.ru", None,
                           FakeRouter({"hybrid": ans}))
            d = {k: m.stats[k] - before[k]
                 for k in ("vision_calls", "vision_valid", "vision_invalid")}
            want = {"vision_calls": 1, "vision_valid": int(kind == "valid"),
                    "vision_invalid": int(kind == "invalid")}
            if d != want:
                st_ok = False
                print(f"     ответ {ans!r}: {d}, ждали {want}")
        check("stats: vision_calls/vision_valid/vision_invalid по каждому "
              "исходу (m8: отдельно от текстовых llm_*)",
              st_ok and m.stats["llm_calls"] == s0["llm_calls"])
        before = dict(m.stats)
        m._hybrid_pick("корзина", base_items(), "x.ru", None,
                       FakeRouter({"hybrid": "2"}, vision=False))
        m._hybrid_pick("корзина", base_items(), "x.ru", None,
                       FakeRouter({"hybrid": RuntimeError("x")}))
        check("stats: ярус не запускался / ошибка вызова — счётчики стоят",
              all(m.stats[k] == before[k]
                  for k in ("vision_calls", "vision_valid", "vision_invalid")))
        check("stats: итог 5 вызовов (3 valid, 2 invalid)",
              m.stats["vision_calls"] - s0["vision_calls"] == 5
              and m.stats["vision_valid"] - s0["vision_valid"] == 3
              and m.stats["vision_invalid"] - s0["vision_invalid"] == 2
              and m.metrics()["vision_calls"] == m.stats["vision_calls"])

        # ════════════════ 4. Каскад _resolve_element_pick ════════════════
        print("\n── 4. Каскад клика ──")
        state["items"] = zero_items()
        state["zones"] = []
        check("каскад (предусловие): zero-match — скоринг пуст для «корзина»",
              ComputerControlManager._score_candidates(
                  zero_items(), "корзина", host="x.ru") == []
              and ComputerControlManager._score_scoped(
                  zero_items(), "корзина") == [])

        def cascade(goal, answers, cfg=None, vision=True, zones=None,
                    items=None, chat="c"):
            state["items"] = items if items is not None else zero_items()
            state["zones"] = zones or []
            mm = make(cfg)
            rr = FakeRouter(answers, vision=vision)
            res = mm._resolve_element_pick(goal, None, rr, chat_id=chat)
            return res, rr, mm

        res, rr, mm = cascade("корзина", {
            "hybrid": "2 C=0.9", "wide": "1", "visual": "1", "zones": "1"},
            chat="k1")
        check("hybrid: выбор гибридом — иконка, путь vision_hybrid",
              res[3] == 1 and res[5]["path"] == "vision_hybrid"
              and res[6] is None)
        check("hybrid: ровно ОДИН vision-вызов (гибрид), широкий текстовый "
              "и _visual_resolve не звались",
              rr.kinds() == ["hybrid"] and not rr.wide_calls()
              and not rr.text_calls)

        zone = [{"x": 10.0, "y": 10.0, "w": 60.0, "h": 30.0, "text": "Зона"}]
        res, rr, mm = cascade("корзина", {"hybrid": "нет", "zones": "нет",
                                          "wide": "1", "visual": "1"},
                              zones=zone, chat="k2")
        check("hybrid «нет»: дальше только зоны — vision-вызовов ровно 2 "
              "(гибрид + зоны), текстовых 0",
              rr.kinds() == ["hybrid", "zones"] and not rr.text_calls
              and res[3] is None and res[6])
        check("hybrid «нет» + зоны «нет»: итоговая мета — от зон "
              "(наблюдение: вердикт гибрида в аудите затёрт)",
              res[5] is not None and res[5].get("path") == "vision_zones")
        # Безымянная зона: подписанная чужим словом («Зона») ветировалась
        # бы label_mismatch уже в _vision_zones
        res, rr, mm = cascade("корзина", {"hybrid": "нет", "zones": "1",
                                          "wide": "1", "visual": "1"},
                              zones=[dict(zone[0], text="")], chat="k2b")
        check("hybrid «нет» → зона выбрана (координатный клик)",
              res[3] is None and res[6] is None
              and res[5].get("point", {}).get("zone") == 1
              and rr.kinds() == ["hybrid", "zones"])
        res, rr, mm = cascade("корзина", {"hybrid": "нет"}, chat="k2c")
        check("hybrid «нет», зон нет: мета гибрида доходит до отказа",
              res[3] is None and res[5]["path"] == "vision_hybrid"
              and rr.kinds() == ["hybrid"] and not rr.text_calls)

        res, rr, mm = cascade("корзина", {
            "hybrid": "1", "wide": lambda p: f"{num_of(p, 'Cart')}",
            "visual": "1"}, vision=False, chat="k3")
        check("supports_vision=False: прежний путь — широкий текстовый "
              "выбрал, vision не звался",
              res[3] == 2 and res[5]["path"] == "llm_wide"
              and len(rr.wide_calls()) == 1 and not rr.img_calls)
        state["shot"] = None
        res, rr, mm = cascade("корзина", {
            "wide": "нет", "visual": "1", "hybrid": "1"}, chat="k3b")
        state["shot"] = JPEG
        check("скриншота нет: гибрид не запускался → широкий текстовый "
              "(и vision-ярусы без скриншота молчат)",
              len(rr.wide_calls()) == 1 and not rr.img_calls
              and res[3] is None)

        res, rr, mm = cascade("корзина", {
            "hybrid": "1", "wide": "нет", "visual": "нет", "zones": "нет"},
            cfg={**CFG, "wide_mode": "text"}, zones=zone, chat="k4")
        check("wide_mode=text: прежний путь — широкий текстовый, затем "
              "_visual_resolve, затем зоны; гибрида нет",
              [c["kind"] for c in rr.text_calls] == ["wide"]
              and rr.kinds() == ["visual", "zones"] and res[3] is None)
        res, rr, mm = cascade("корзина", {
            "wide": lambda p: f"{num_of(p, 'Cart')}"},
            cfg={**CFG, "wide_mode": "text"}, chat="k4b")
        check("wide_mode=text: широкий выбрал — vision не зовётся вовсе",
              res[3] == 2 and res[5]["path"] == "llm_wide"
              and not rr.img_calls)
        res, rr, mm = cascade("корзина", {"wide": "нет", "visual": "2"},
                              cfg={**CFG, "wide_mode": "text"}, chat="k4c")
        check("wide_mode=text: _visual_resolve выбрал иконку",
              res[3] is not None and res[5]["path"] == "vision"
              and rr.kinds() == ["visual"])

        # Более конкретное вето прошлых ярусов не затирается гибридом
        vitems = zero_items() + [_it(9, "button", "Удалить всё", x=600.0,
                                     y=100.0, w=90.0, h=30.0)]
        res, rr, mm = cascade("корзина", {"hybrid": "нет"}, items=vitems,
                              chat="k5")
        check("вето прошлого яруса (destructive из _choose_element) не "
              "затёрто мета гибрида",
              res[3] is None and res[5].get("veto") == "destructive"
              and res[5].get("path") != "vision_hybrid"
              and rr.kinds() == ["hybrid"])
        recs = aud(mm, "k5")
        check("вето прошлого яруса: в аудите fail_reason=destructive_veto",
              any(r.get("fail_reason") == "destructive_veto" for r in recs))

        # ════════════════ 5. Шаг навигации _resolve_nav_step ════════════════
        print("\n── 5. Шаг навигации ──")

        def nav(step, answers, cfg=None, vision=True, items=None):
            mm = make(cfg)
            rr = FakeRouter(answers, vision=vision)
            res = mm._resolve_nav_step(step, "x.ru",
                                       items if items is not None
                                       else zero_items(), 42, rr,
                                       page_url="https://x.ru/")
            return res, rr

        (n_idx, n_meta), rr = nav("корзина", {
            "hybrid": "2 C=0.9", "wide": "1", "visual": "1"})
        check("nav hybrid: выбор гибридом, один vision-вызов, без текстового",
              n_idx == 1 and n_meta["path"] == "vision_hybrid"
              and rr.kinds() == ["hybrid"] and not rr.text_calls)
        (n_idx, n_meta), rr = nav("корзина", {"hybrid": "нет", "wide": "1",
                                              "visual": "1"})
        check("nav hybrid «нет»: (None, мета гибрида), широкий и visual "
              "не звались",
              n_idx is None and n_meta.get("path") == "vision_hybrid"
              and rr.kinds() == ["hybrid"] and not rr.text_calls)
        (n_idx, n_meta), rr = nav("корзина", {
            "hybrid": "1", "wide": lambda p: f"{num_of(p, 'Cart')}",
            "visual": "1"}, vision=False)
        check("nav supports_vision=False: широкий текстовый выбрал",
              n_idx == 2 and n_meta["path"] == "llm_wide"
              and len(rr.wide_calls()) == 1 and not rr.img_calls)
        (n_idx, n_meta), rr = nav("корзина", {
            "hybrid": "1", "wide": "нет", "visual": "2"},
            cfg={**CFG, "wide_mode": "text"})
        check("nav wide_mode=text: широкий «нет» → _visual_resolve выбрал",
              n_idx is not None and n_meta["path"] == "vision"
              and [c["kind"] for c in rr.text_calls] == ["wide"]
              and rr.kinds() == ["visual"])
        (n_idx, n_meta), rr = nav("открыть", {
            "hybrid": lambda p: f"{num_of(p, 'Удалить')} C=0.9"},
            items=destr)
        check("nav hybrid: разрушительный выбор ветирован",
              n_idx is None and n_meta.get("veto") == "destructive"
              and rr.kinds() == ["hybrid"] and not rr.text_calls)

        # ════════════════ 6. Аудит ════════════════
        print("\n── 6. Аудит ──")
        state["items"] = zero_items()
        state["zones"] = []
        m = make()
        act, err = m.resolve_click("оплата", None, FakeRouter({
            "hybrid": lambda p: f"{num_of(p, 'Оформление заказа')} C=0.8"}),
            chat_id="a1")
        check("аудит (предусловие): resolve_click вернул клик гибридом",
              err is None and act is not None and act["idx"] == 4
              and act["choose"]["path"] == "vision_hybrid")
        if act is not None:
            m._audit("a1", act, True, "clicked")
        rec = next((r for r in aud(m, "a1") if r.get("kind") == "click"), {})
        check("аудит клика: wide_mode=hybrid, conf=0.8, offscreen=True, "
              "path=vision_hybrid",
              rec.get("wide_mode") == "hybrid" and rec.get("conf") == 0.8
              and rec.get("offscreen") is True
              and rec.get("path") == "vision_hybrid")

        # Строка текстового списка не проходит сверку подписи (как широкий
        # резолв) — вето по подписи проверяем на РАМКЕ. После вето каскад
        # спрашивает широкий (тут «нет») — итоговая причина от него, а
        # вердикт гибрида с уверенностью — в следе ярусов. Слов цели в
        # zero_items нет вовсе — «нет» модели это not_in_snapshot, а не
        # llm_veto (_resolve_fail_kind, goal_absent)
        m = make()
        act, err = m.resolve_click("оплата", None, FakeRouter({
            "hybrid": lambda p: f"{num_of(p, 'Помощь')} C=0.3"}),
            chat_id="a2")
        rec = next((r for r in aud(m, "a2")
                    if r.get("kind") == "resolve_fail"), {})
        t0 = (rec.get("tiers") or [{}])[0]
        check("аудит отказа (_audit_resolve): рамка с чужой подписью, C=0.3 — "
              "вето в следе (conf, picked_n, бюджеты), затем широкий «нет»",
              act is None and rec.get("fail_reason") == "not_in_snapshot"
              and rec.get("wide_mode") == "hybrid"
              and rec.get("path") == "llm_wide"
              and [t.get("path") for t in rec.get("tiers") or []]
              == ["vision_hybrid", "llm_wide"]
              and t0.get("veto") == "label_mismatch" and t0.get("conf") == 0.3
              and t0.get("picked_n") == 4 and t0.get("n_boxes") == 4
              and t0.get("n_text") == 1)

        m = make()
        act, err = m.resolve_click("корзина", None,
                                   FakeRouter({"hybrid": "нет"}),
                                   chat_id="a3")
        rec = next((r for r in aud(m, "a3")
                    if r.get("kind") == "resolve_fail"), {})
        check("аудит отказа: гибрид «нет» при цели вне снапшота — "
              "not_in_snapshot, wide_mode, без conf",
              act is None and rec.get("fail_reason") == "not_in_snapshot"
              and rec.get("wide_mode") == "hybrid" and "conf" not in rec
              and rec.get("llm_response") == "нет")

        m = make({**CFG, "wide_mode": "text"})
        act, err = m.resolve_click("корзина", None, FakeRouter({
            "wide": "нет", "visual": "нет"}), chat_id="a4")
        rec = next((r for r in aud(m, "a4")
                    if r.get("kind") == "resolve_fail"), {})
        check("аудит (text): _visual_resolve «нет» → path=vision, "
              "llm_response=«нет», цели нет в снапшоте → not_in_snapshot",
              act is None and rec.get("path") == "vision"
              and rec.get("llm_response") == "нет"
              and rec.get("fail_reason") == "not_in_snapshot"
              and rec.get("wide_mode") == "text")  # режим взят из конфига

        m = make({**CFG, "wide_mode": "text"})
        act, err = m.resolve_click("корзина", None, FakeRouter({
            "wide": "нет", "visual": "мусор"}), chat_id="a5")
        rec = next((r for r in aud(m, "a5")
                    if r.get("kind") == "resolve_fail"), {})
        check("аудит (text): _visual_resolve невалидно → path=vision, "
              "ответ сохранён",
              act is None and rec.get("path") == "vision"
              and rec.get("llm_response") == "мусор")

        # _visual_resolve напрямую: «нет» / невалидно → (None, meta)
        m = make({**CFG, "wide_mode": "text"})
        v_idx, v_meta = m._visual_resolve("x.ru", None, base_items(),
                                          "корзина", FakeRouter({"visual": "нет"}))
        check("_visual_resolve «нет» → (None, meta path=vision)",
              v_idx is None and v_meta is not None
              and v_meta["path"] == "vision" and v_meta["llm_response"] == "нет")
        v_idx, v_meta = m._visual_resolve("x.ru", None, base_items(),
                                          "корзина", FakeRouter({"visual": "99"}))
        check("_visual_resolve вне диапазона → (None, meta)",
              v_idx is None and v_meta is not None)
        v_idx, v_meta = m._visual_resolve("x.ru", None, base_items(),
                                          "корзина",
                                          FakeRouter({"visual": "1"},
                                                     vision=False))
        check("_visual_resolve без vision → (None, None)",
              (v_idx, v_meta) == (None, None))

        # _llm_wide_pick: в мете нет метки wide_mode — режим берётся из
        # конфига в _audit
        m = make()
        w_idx, w_meta = m._llm_wide_pick("корзина", base_items(),
                                         FakeRouter({"wide": "нет"}))
        check("_llm_wide_pick: без метки wide_mode в мете",
              w_idx is None and "wide_mode" not in w_meta)
        w_idx, w_meta = m._llm_wide_pick("почта", base_items(),
                                         FakeRouter({"wide": "нет"}),
                                         for_field=True)
        check("_llm_wide_pick(for_field): без метки wide_mode",
              w_meta is not None and "wide_mode" not in w_meta)

        # ════════════════ 9. Крайние случаи и устойчивость каскада ════════════════
        print("\n── 9. Правки ревью ──")
        state["items"] = zero_items()
        state["zones"] = []
        zone1 = [{"x": 10.0, "y": 10.0, "w": 60.0, "h": 30.0, "text": ""}]

        # Vision-цепочка недоступна — get_response_with_image вернул None/""
        for dead in (None, ""):
            mm = make()
            (i_, m_) = mm._hybrid_pick("корзина", base_items(), "x.ru", None,
                                       FakeRouter({"hybrid": dead}))
            check(f"M1: ответ {dead!r} → (None, None), vision_invalid не растёт",
                  (i_, m_) == (None, None) and mm.stats["vision_invalid"] == 0
                  and mm.stats["vision_calls"] == 0)
        vis = {}
        make()._hybrid_pick("корзина", base_items(), "x.ru", None,
                            FakeRouter({"hybrid": None}), vis=vis)
        check("M1: лежащий vision помечен в vis (down) и в следе ярусов",
              vis.get("down") is True
              and vis.get("tiers") == [{"path": "vision_hybrid",
                                        "fail": "vision_down"}])
        res, rr, mm = cascade("корзина", {
            "hybrid": None, "wide": lambda p: f"{num_of(p, 'Cart')}",
            "visual": "1", "zones": "1"}, zones=zone1, chat="r1")
        check("M1 каскад: vision лежит → текстовый широкий выбрал; "
              "_visual_resolve и зоны не звались",
              res[3] == 2 and res[5]["path"] == "llm_wide"
              and rr.kinds() == ["hybrid"] and len(rr.wide_calls()) == 1
              and [t["path"] for t in res[5].get("tiers") or []]
              == ["vision_hybrid", "llm_wide"])
        res, rr, mm = cascade("корзина", {
            "hybrid": None, "wide": "нет", "visual": "1", "zones": "1"},
            zones=zone1, chat="r1b")
        check("M1 каскад: vision лежит, широкий «нет» → отказ без зон",
              res[3] is None and res[6] and rr.kinds() == ["hybrid"]
              and len(rr.wide_calls()) == 1)
        res, rr, mm = cascade("корзина", {
            "wide": "нет", "visual": None, "zones": "1"},
            cfg={**CFG, "wide_mode": "text"}, zones=zone1, chat="r1c")
        check("M1 text: _visual_resolve с лежащим vision — зоны не зовутся",
              res[3] is None and rr.kinds() == ["visual"])
        v_idx, v_meta = make({**CFG, "wide_mode": "text"})._visual_resolve(
            "x.ru", None, base_items(), "корзина", FakeRouter({"visual": None}))
        check("M1: _visual_resolve на None-ответ → (None, None), не «нет»",
              (v_idx, v_meta) == (None, None))
        (n_idx, n_meta), rr = nav("корзина", {
            "hybrid": None, "wide": lambda p: f"{num_of(p, 'Cart')}",
            "visual": "1"})
        check("M1 nav: vision лежит → широкий выбрал, visual не звался",
              n_idx == 2 and n_meta["path"] == "llm_wide"
              and rr.kinds() == ["hybrid"])

        # Невалидный ответ гибрида — дальше широкий, но не _visual_resolve, затем зоны
        res, rr, mm = cascade("корзина", {
            "hybrid": "Выбираю между 1 и 2", "wide": "нет", "visual": "1",
            "zones": "нет"}, zones=zone1, chat="r2")
        check("M2 каскад: невалидный гибрид → широкий, без _visual_resolve, "
              "затем зоны",
              res[3] is None and rr.kinds() == ["hybrid", "zones"]
              and len(rr.wide_calls()) == 1
              and [t["path"] for t in res[5].get("tiers") or []]
              == ["vision_hybrid", "llm_wide", "vision_zones"]
              and res[5]["tiers"][0].get("fail") == "invalid")
        res, rr, mm = cascade("корзина", {
            "hybrid": "мусор", "wide": lambda p: f"{num_of(p, 'Cart')}"},
            chat="r2b")
        check("M2 каскад: невалидный гибрид → широкий текстовый выбрал",
              res[3] == 2 and res[5]["path"] == "llm_wide")
        (n_idx, n_meta), rr = nav("корзина", {
            "hybrid": "мусор", "wide": lambda p: f"{num_of(p, 'Cart')}",
            "visual": "1"})
        check("M2 nav: невалидный гибрид → широкий выбрал, visual не звался",
              n_idx == 2 and n_meta["path"] == "llm_wide"
              and rr.kinds() == ["hybrid"])
        (i_, m_), _, _ = run("корзина", base_items(), {"hybrid": "мусор"})
        check("M2: невалидный ответ помечен fail=invalid",
              i_ is None and m_.get("fail") == "invalid")

        # Политика сверки подписи: рамка с чужой подписью ветируется при любой
        # уверенности, строка с чужой подписью принимается (как в широком резолве)
        renoir = [_it(0, "a", "2:43 World Map - Taking Down the Paintress",
                      x=10.0, y=10.0, w=300.0, h=40.0),
                  _it(1, "a", "Главная", x=10.0, y=100.0, w=200.0, h=40.0)]
        (i_, m_), _, _ = run("renoir", renoir, {"hybrid": "1 C=0.8"})
        check("M3: «renoir» → рамка «2:43 World Map…» с C=0.8 — вето",
              i_ is None and m_.get("veto") == "label_mismatch")
        (i_, m_), _, _ = run("renoir", renoir, {"hybrid": "1 C=0.9"})
        check("N2 (repro G): та же рамка с C=0.9 — тоже вето",
              i_ is None and m_.get("veto") == "label_mismatch"
              and m_["conf"] == 0.9)
        (i_, m_), _, _ = run("renoir", renoir, {"hybrid": "1"})
        check("M3: рамка с чужой подписью без C — вето",
              i_ is None and m_.get("veto") == "label_mismatch")
        (i_, m_), _, _ = run("корзина", base_items(), {
            "hybrid": lambda p: f"{num_of(p, 'Оформление заказа')}"})
        check("M3: строка с чужой подписью без C — принята (как широкий)",
              i_ == 4 and m_.get("row") is True and not m_.get("veto"))
        (i_, m_), rr, _ = run("корзина", base_items(), {"hybrid": "нет"})
        check("M3: формат уверенности в промпте — без числа-образца",
              "C=<confidence from 0 to 1>" in rr.img_calls[0]["prompt"]
              and not re.search(r"C=0[.,]\d", rr.img_calls[0]["prompt"]))
        check("N2: порога уверенности больше нет (C — только данные)",
              not hasattr(cc, "HYBRID_MIN_CONF"))

        # Видимая подписанная цель, поздно встречающаяся в DOM, не выпадает из строк
        page = [_it(i, "button", "", w=30.0, h=30.0, x=float(i * 40), y=10.0)
                for i in range(10)]
        page += [_it(10 + i, "a", f"Раздел номер {i}", w=80.0, h=20.0,
                     x=float(i * 40), y=100.0) for i in range(25)]
        page += [_it(90 + i, "a", f"Внизу {i}", vp=False, y=3000.0)
                 for i in range(10)]
        page += [_it(40, "a", "Электронная почта", w=60.0, h=18.0, x=10.0,
                     y=300.0)]
        (i_, m_), rr, _ = run("почта ящик", page, {"hybrid": "нет"})
        p = rr.img_calls[0]["prompt"]
        n_t = num_of(p, "Электронная почта")
        n_off = num_of(p, "Внизу 0")
        check("M4: видимая цель без рамки — в строках, раньше строк вне экрана",
              n_t is not None and (n_off is None or n_t < n_off))
        check("M4: HYBRID_TEXT_MAX = 30", HYBRID_TEXT_MAX == 30)

        # Докрутка элемента во вьюпорт выполняется в том же evaluate, что и
        # замер «до» (CDP), и в том же Apple Events-вызове (AppleScript);
        # только если элемент целиком вне экрана, behavior:'instant'
        order = []
        evals = []

        class _First:
            def scroll_into_view_if_needed(self, timeout=None):
                order.append("scroll_api")  # не должен вызываться

            def click(self, **kw):
                order.append("click")

            def hover(self, **kw):
                order.append("hover")

        class _Loc:
            first = _First()

        class _Scope:
            url = "https://x.ru/"

            def evaluate(self, js, *a):
                evals.append(js)
                order.append("state")
                return "d|https://x.ru/|complete|123"

        class _W:
            def page_for(self, host, tab_id):
                return object()

            def _all_pages(self):
                return []

        patch("_locator_any_frame", lambda page, idx: (_Loc(), _Scope()))
        patch("_wait_effect", lambda st, pre: _ba.EFFECT_CHANGED)
        patch("_eval_arg", lambda scope, js, arg: "hover")
        _ba._click_cdp(_W(), "x.ru", 5, None)
        pre_js = evals[0] if evals else ""
        check("N3 CDP-клик: без scroll_into_view_if_needed, один evaluate до "
              "клика (замер со встроенной докруткой)",
              "scroll_api" not in order and order[:2] == ["state", "click"])
        check("N3/N4: докрутка условная (элемент целиком вне вьюпорта) и "
              "мгновенная, внутри замера «до»",
              "getBoundingClientRect" in pre_js
              and "vpcR.bottom<=0||vpcR.top>=innerHeight" in pre_js
              and "behavior:'instant'" in pre_js
              and _ba._DOM_STATE_JS in pre_js
              and pre_js.index("scrollIntoView") < pre_js.index(_ba._DOM_STATE_JS))
        check("N3: для видимого элемента прокрутки нет — scrollIntoView только "
              "под условием «вне экрана»",
              pre_js.count("scrollIntoView") == 1
              and re.search(r"if\(vpcR\.bottom<=0[^{]*\)\{vpcSe\.scrollIntoView",
                            pre_js) is not None)
        check("N3: замер без scroll_idx — прежний JS (опрос эффекта не крутит)",
              _ba._state_js() == _ba._DOM_STATE_JS)
        order.clear()
        evals.clear()
        _ba._hover_cdp(_W(), "x.ru", 5, None)
        check("N3 CDP-наведение: докрутка в замере «до», без scroll API",
              "scroll_api" not in order and order[:2] == ["state", "hover"]
              and evals and "behavior:'instant'" in evals[0])
        ae = []

        def _fake_ae(host, js, tab_id=None):
            ae.append(js)
            if "el.click()" in js:
                return "ok:clicked"
            return "d|https://x.ru/|complete|123"
        patch("_run_apple_events", _fake_ae)
        _ba._click_applescript("x.ru", 5, None)
        check("N3 AppleScript-клик: ноль лишних Apple Events (замер+клик = 2)",
              len(ae) == 2 and _ba._DOM_STATE_JS in ae[0]
              and "behavior:'instant'" in ae[0] and "el.click()" in ae[1])
        check("N4: клик-JS AppleScript больше не крутит страницу после замера",
              "scrollIntoView" not in ae[1])

        # Условная докрутка в настоящем JS-движке (node, если есть): видимый
        # элемент — ни одного scrollIntoView, целиком вне экрана — ровно
        # один, мгновенный; полный JS замера — синтаксически валиден
        import shutil
        import subprocess
        if shutil.which("node"):
            sj = _ba._scroll_if_off_js(5)
            harness = (
                "var scrolled=[];var innerHeight=800,innerWidth=1280;"
                "function mk(r){return {getAttribute:function(a){"
                "return a==='data-vpc-idx'?'5':null;},"
                "getBoundingClientRect:function(){return r;},"
                "scrollIntoView:function(o){scrolled.push(o);}};}"
                "var els=[];var document={querySelectorAll:function(){"
                "return els;}};"
                "els=[mk({top:100,bottom:140,left:10,right:200})];" + sj +
                "var onScreen=scrolled.length;"
                "els=[mk({top:790,bottom:830,left:10,right:200})];" + sj +
                "var partial=scrolled.length-onScreen;"
                "els=[mk({top:3000,bottom:3040,left:10,right:200})];" + sj +
                "console.log(onScreen+','+partial+','+"
                "(scrolled.length-onScreen-partial)+','+"
                "(scrolled.length?scrolled[scrolled.length-1].behavior:''));"
                "var full=function(){return " + _ba._state_js(5) + ";};")
            try:
                out = subprocess.run(["node", "-e", harness],
                                     capture_output=True, text=True,
                                     timeout=30)
                res_js = out.stdout.strip()
            except Exception as e:
                res_js = f"error {e}"
            check("N3 (node): видимый и частично видимый — без прокрутки; "
                  "вне экрана — одна, behavior=instant",
                  res_js == "0,0,1,instant")
        else:
            print("  [SKIP] node не найден — JS-проверка докрутки пропущена")

        # Сомнение и эхо подписи не становятся кликом
        rowitems = [_it(0, "a", "Главная", x=10.0, y=10.0, w=200.0, h=40.0),
                    _it(1, "a", "Настройки", vp=False, y=3000.0)]
        (i_, m_), _, _ = run("renoir", rowitems,
                             {"hybrid": "Не вижу подходящего, возможно 2"})
        check("N1 (repro I): сомнение с номером строки — невалидно, не клик",
              i_ is None and m_.get("fail") == "invalid")
        hedge_ok = True
        for hedge in ("Не вижу подходящего, возможно 2", "Не уверен, но 2",
                      "Возможно, 2", "I think 2", "maybe 2", "Кажется, 2"):
            (i_, m_), _, _ = run("renoir", rowitems, {"hybrid": hedge})
            if i_ is not None or m_.get("fail") != "invalid":
                hedge_ok = False
                print(f"     {hedge!r} → {i_}, {m_ and m_.get('fail')}")
        check("N1: все формы сомнения — невалидно (дальше широкий)", hedge_ok)
        for ans, want in (("2 — Настройки", None), ("2.", 1),
                          ("**2** C=0.9", 1), ("Ответ: 2", 1),
                          ("Тариф 2 ГБ", None)):
            (i_, m_), _, _ = run("настройки профиля", rowitems,
                                 {"hybrid": ans})
            check(f"N1a: строка — только строгая грамматика: {ans!r} → {want}",
                  i_ == want)
        # Безымянная рамка — тоже только строго
        (i_, m_), _, _ = run("корзина", base_items(),
                             {"hybrid": "2 — это иконка корзины"})
        check("N1a: безымянная рамка с пояснением — невалидно",
              i_ is None and m_.get("fail") == "invalid")
        # Рамка с подписью: нестрогий разбор допустим, но сверка строгая
        tariff = [_it(0, "a", "Тариф 5 ГБ", x=10.0, y=10.0, w=200.0, h=40.0),
                  _it(1, "a", "Главная", x=10.0, y=100.0, w=200.0, h=40.0)]
        (i_, m_), _, _ = run("тариф", tariff, {"hybrid": "1 — «Тариф 5 ГБ»"})
        check("N1b: рамка с подписью, нестрогий ответ + подпись совпала — выбор",
              i_ == 0 and m_.get("loose_parse") is True)
        (i_, m_), _, _ = run("renoir", tariff, {"hybrid": "Тариф 5 ГБ"})
        check("N1b: эхо подписи («Тариф 5 ГБ» → 5) — вне диапазона/невалидно",
              i_ is None)
        (i_, m_), _, _ = run("renoir", renoir,
                             {"hybrid": "Не вижу подходящего, возможно 1"})
        check("N1 (repro H): сомнение с номером рамки — невалидно",
              i_ is None and m_.get("fail") == "invalid")

        # Вето label_mismatch рамки → текстовый широкий (0 → 1 вызов),
        # _visual_resolve не зовётся
        res, rr, mm = cascade("корзина", {
            "hybrid": lambda p: f"{num_of(p, 'Cart')} C=0.95",
            "wide": lambda p: f"{num_of(p, 'Cart')}", "visual": "1",
            "zones": "1"}, chat="n2")
        check("N2 каскад: «Cart» рамкой ветирован → широкий выбрал «Cart»",
              res[3] == 2 and res[5]["path"] == "llm_wide"
              and rr.kinds() == ["hybrid"] and len(rr.wide_calls()) == 1
              and [t.get("veto") for t in res[5].get("tiers") or []][:1]
              == ["label_mismatch"])
        res, rr, mm = cascade("корзина", {"hybrid": "2", "wide": "1"},
                              chat="n2b")
        check("N2 каскад: принятый выбор гибрида — текстовых вызовов 0",
              res[3] == 1 and not rr.text_calls)
        (n_idx, n_meta), rr = nav("корзина", {
            "hybrid": lambda p: f"{num_of(p, 'Cart')} C=0.95",
            "wide": lambda p: f"{num_of(p, 'Cart')}", "visual": "1"})
        check("N2 nav: вето рамки → широкий выбрал, visual не звался",
              n_idx == 2 and n_meta["path"] == "llm_wide"
              and rr.kinds() == ["hybrid"] and len(rr.wide_calls()) == 1)

        # Удачный скриншот — один на резолв (гибрид «нет» → зоны)
        shots_ok = []

        def _count_ok(host=None, tab_id=None):
            shots_ok.append(1)
            return state["shot"]
        patch("screenshot_viewport", _count_ok)
        res, rr, mm = cascade("корзина", {"hybrid": "нет", "zones": "нет"},
                              zones=zone1, chat="m5b")
        patch("screenshot_viewport",
              lambda host=None, tab_id=None: state["shot"])
        check("m5: гибрид → зоны — один снимок, кадр переиспользован",
              len(shots_ok) == 1 and rr.kinds() == ["hybrid", "zones"])

        # Строка на экране (не влезла в рамки) — row без offscreen
        many_v = [_it(i, "a", f"Раздел {chr(0x410 + i)}",
                      x=float(10 + (i % 12) * 100), y=float(50 + (i // 12) * 60),
                      w=90.0, h=40.0) for i in range(14)]
        (i_, m_), _, _ = run("корзина", many_v, {"hybrid": "14 C=0.9"})
        check("m1: видимая строка без рамки — row=True, offscreen нет",
              i_ is not None and m_.get("row") is True
              and "offscreen" not in m_ and m_.get("picked_n") == 14)

        # След ярусов и wide_mode на записи клика (режим text)
        m = make({**CFG, "wide_mode": "text"})
        act, err = m.resolve_click("корзина", None, FakeRouter({
            "wide": lambda p: f"{num_of(p, 'Cart')}"}), chat_id="t1")
        if act is not None:
            m._audit("t1", act, True, "clicked")
        rec = next((r for r in aud(m, "t1") if r.get("kind") == "click"), {})
        check("m2: запись клика — wide_mode из конфига (text), след ярусов, "
              "picked_n",
              rec.get("wide_mode") == "text" and rec.get("picked_n") == 1
              and [t.get("path") for t in rec.get("tiers") or []]
              == ["llm_wide"])
        res, rr, mm = cascade("корзина", {"hybrid": "нет", "zones": "нет"},
                              zones=zone1, chat="t2")
        recs = [r for r in aud(mm, "t2") if r.get("kind") == "resolve_fail"]
        check("m2: «нет» гибрида не теряется, когда зоны тоже «нет» (tiers)",
              recs and [(t.get("path"), t.get("resp"))
                        for t in recs[0].get("tiers") or []]
              == [("vision_hybrid", "нет"), ("vision_zones", "нет")])

        # Цель-иконка — только рамки
        (i_, m_), rr, _ = run("шестерёнка", base_items(), {"hybrid": "нет"})
        check("m3: цель-иконка — строк нет, только рамки",
              m_ is not None and m_["n_text"] == 0
              and "Elements without a box" not in rr.img_calls[0]["prompt"])

        # Скриншот не снялся — в том же резолве повторно не снимаем
        shots = []

        def _count_shot(host=None, tab_id=None):
            shots.append(1)
            return None
        patch("screenshot_viewport", _count_shot)
        res, rr, mm = cascade("корзина", {"wide": "нет"}, zones=zone1,
                              chat="t3")
        patch("screenshot_viewport",
              lambda host=None, tab_id=None: state["shot"])
        check("m5: скриншот упал — один снимок на резолв (гибрид/рамки/зоны "
              "не переснимают)",
              len(shots) == 1 and not rr.img_calls
              and len(rr.wide_calls()) == 1)

        # Центр рамки за кадром (iframe: vp=True, но ниже картинки)
        frame = [_it(0, "button", "", x=10.0, y=10.0, w=40.0, h=40.0),
                 _it(1, "a", "Кнопка во фрейме", x=10.0, y=900.0, w=200.0,
                     h=40.0)]
        (i_, m_), rr, _ = run("корзина", frame, {"hybrid": "нет"})
        p = rr.img_calls[0]["prompt"]
        check("m6: элемент с центром ниже кадра — без рамки",
              m_["n_boxes"] == 1 and num_of(p, "Кнопка во фрейме") == 2)
        v_idx, v_meta = make({**CFG, "wide_mode": "text"})._visual_resolve(
            "x.ru", None, frame, "корзина", FakeRouter({"visual": "нет"}))
        check("m6: _visual_resolve тоже рисует только рамки внутри кадра",
              v_meta is not None and [c["idx"] for c in v_meta["candidates"]]
              == [0])

        # Vision-вызовы рамок и зон учитываются в vision_*, не в текстовых llm_*
        m = make({**CFG, "wide_mode": "text"})
        m._visual_resolve("x.ru", None, base_items(), "корзина",
                          FakeRouter({"visual": "нет"}))
        state["zones"] = zone1
        m._vision_zones("корзина", "x.ru", None, FakeRouter({"zones": "мусор"}))
        state["zones"] = []
        check("m8: _visual_resolve и _vision_zones — в vision_calls/valid/"
              "invalid; llm_calls и llm_share не тронуты",
              m.stats["vision_calls"] == 2 and m.stats["vision_valid"] == 1
              and m.stats["vision_invalid"] == 1
              and m.stats["llm_calls"] == 0
              and m.metrics()["llm_share"] == 0.0
              and m.metrics()["vision_calls"] == 2)

        # Форматирование _cand_line: крайние случаи
        check("нит: безымянный с контекстом — «(no label, block: …)»",
              _cand_line(1, _it(0, "button", "", ctx="Корзина"))
              == "1) [button/-] (no label, block: Корзина)")
        check("нит: длинный тег/роль обрезаны до 24 символов",
              _cand_line(1, _it(0, "t" * 50, "Ок", role="r" * 50))
              == f"1) [{'t' * 24}/{'r' * 24}] Ок")

        # ════════════════ Конфиг wide_mode ════════════════
        print("\n── Конфиг ──")
        check("wide_mode: дефолт hybrid", make().wide_mode == "hybrid")
        check("wide_mode: «text» / « TEXT » → text",
              make({**CFG, "wide_mode": "text"}).wide_mode == "text"
              and make({**CFG, "wide_mode": " TEXT "}).wide_mode == "text")
        check("wide_mode: неизвестное / None → hybrid",
              make({**CFG, "wide_mode": "vision"}).wide_mode == "hybrid"
              and make({**CFG, "wide_mode": None}).wide_mode == "hybrid")

        # Кросс-платформенность: ярус сам на диск не пишет, аудит — через
        # pathlib (base_dir / "audit.jsonl") — проверять нечего, пропущено
    finally:
        for name, fn in _saved.items():
            if fn is None:
                try:
                    delattr(_ba, name)
                except AttributeError:
                    pass
            else:
                setattr(_ba, name, fn)

    print(f"\nИтог: {counts['ok']} OK, {counts['fail']} FAIL")
    return 0 if counts["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

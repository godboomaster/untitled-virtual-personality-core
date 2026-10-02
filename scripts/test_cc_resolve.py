"""Каскад резолва элемента режима управления (computer_control).

Проверяет: отсев не-клик целей на входе resolve_click (клавиша/листание/
сайт/звукоподражание) и сдвоенный предлог; общий бюджет каскада
(resolve_budget_sec, fail_reason «budget», resolve_ms); общий снапшот попыток
«закрой X»; целевой снапшот/доскролл не трогают уверенный дешёвый выбор;
пул широкого LLM-резолва без служебных обрывков (n_pool) и честный класс
отказа (not_in_snapshot vs llm_veto); сверку подписи vision-выбора
(порядковые/иконочные слова, контекст), согласие гибрида и зон → клик с
подтверждением; хост в аудите сбоя снапшота и фолбэк SSO по хосту; следование
за видимой вкладкой; клик в невидимой вкладке — только с подтверждением;
choose/origin у кликов агента задач. Браузер и LLM подменены.

Запуск: python -m scripts.test_cc_resolve
"""

import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cc_resolve_smoke_")
    tmp = Path(tempfile.mkdtemp(prefix="cc_resolve_data_"))
    import app.core.router as _net_router
    _net_router.internet_available = lambda: True
    _net_router._net_ok, _net_router._net_checked = True, float("inf")

    fails = 0
    total = 0

    def check(name, cond):
        nonlocal fails, total
        total += 1
        if not cond:
            fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")

    import app.features.computer_control as ccm
    from app.features import browser_actions as ba
    from app.features.computer_control import ComputerControlManager

    # ── Браузер подменён целиком: ни одного вызова в живой Chrome ──
    state = {"items": [], "url": "https://x.ru", "host": "x.ru",
             "snap_calls": 0, "goal_snap": 0, "snap_delay": 0.0,
             "snap_raise": None, "visible": None, "pages": [],
             "boxes": [], "page_items": {}}

    def _snap(host=None, tab_id=None):
        state["snap_calls"] += 1
        if state["snap_delay"]:
            time.sleep(state["snap_delay"])
        if state["snap_raise"] is not None:
            r = state["snap_raise"](host)
            if r is not None:
                raise r
        if host in state["page_items"]:
            u, h, its = state["page_items"][host]
            return u, h, list(its)
        return state["url"], state["host"], list(state["items"])

    def _goal_snap(host, goal, tab_id=None):
        state["goal_snap"] += 1
        return "", []

    saved = {}
    mocks = {
        "snapshot_elements": _snap,
        "snapshot_for_goal": _goal_snap,
        "list_pages": lambda: list(state["pages"]),
        "dismiss_overlay": lambda host=None, tab_id=None: None,
        "detect_antibot": lambda host=None, tab_id=None, strict=False: None,
        "modal_visible": lambda host=None, tab_id=None: False,
        "open_list_visible": lambda host=None, tab_id=None: False,
        "wait_dom_idle": lambda *a, **kw: None,
        "visible_page_info": lambda: state["visible"],
        "reveal_player_controls": lambda *a, **kw: None,
        "scroll_position": lambda host=None, tab_id=None: 0.0,
        "scroll_step": lambda host=None, tab_id=None: {"moved": False},
        "scroll_restore": lambda host=None, tab_id=None, y=0.0: None,
        "scroll_container_step": lambda host=None, tab_id=None: {"moved": False},
        "scroll_container_restore": lambda host=None, tab_id=None, y=0.0: None,
        "screenshot_viewport": lambda host=None, tab_id=None: None,
        "all_clickable_boxes": lambda host=None, tab_id=None: list(state["boxes"]),
        "find_tab_id": lambda url: 7,
        "click_tagged": lambda *a, **kw: "clicked",
        "href_of_tagged": lambda *a, **kw: "",
    }
    for k, v in mocks.items():
        saved[k] = getattr(ba, k, None)
        setattr(ba, k, v)

    CFG = {"confirm": False, "allow_domains": [], "vision_fallback": True}

    class Spy(ComputerControlManager):
        def _dispatch(self, action, router=None):
            pass

    def make(cfg=CFG):
        return Spy(context="cres", config=cfg,
                   base_dir=tmp / f"m{time.time_ns()}")

    def _it(idx, tag, text, **kw):
        it = {"idx": idx, "tag": tag, "role": "", "text": text, "aria": "",
              "title": "", "href": "", "w": 40.0, "h": 20.0, "vp": True}
        it.update(kw)
        return it

    def _recs(m, chat):
        p = m.base_dir / "audit.jsonl"
        if not p.exists():
            return []
        out = []
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("chat_id") == chat or chat is None:
                out.append(r)
        return out

    class _Router:
        # Текстовые ответы — по очереди из texts, vision — из imgs
        def __init__(self, texts=(), imgs=(), vision=True):
            self.texts = list(texts)
            self.imgs = list(imgs)
            self.vision = vision
            self.prompts = []
            self.img_calls = 0

        def supports_vision(self):
            return self.vision

        def get_response(self, messages, **kw):
            self.prompts.append(messages[-1]["content"])
            return self.texts.pop(0) if self.texts else "no"

        def get_response_with_image(self, prompt, img, image_mime=None,
                                    extra_image=None, force_provider=None):
            self.img_calls += 1
            return self.imgs.pop(0) if self.imgs else "no"

    try:
        # ── 1. Не-клик цели на входе resolve_click ──
        hint = ComputerControlManager._non_click_hint
        for g in ("ArrowDown", "arrowdown", "PageDown", "пробел", "Escape",
                  "пролситать", "прокрутить вниз", "листать", "scroll down",
                  "сайт додо пицца", "тук-тук", "кап кап"):
            check(f"не-клик: «{g}» → подсказка", bool(hint(g)))
        for g in ("Home", "стрелку вправо", "Подписаться", "закрыть",
                  "третье на новости", "крестик", "Контакты", "2"):
            check(f"клик: «{g}» — обычная цель", hint(g) is None)
        check("не-клик: подсказка по-русски называет форму команды",
              "клавиш" in hint("ArrowDown") and "открой" in hint("сайт додо пицца")
              and "прокрути" in hint("пролситать"))

        m1 = make()
        state.update(items=[_it(0, "a", "Новости")], snap_calls=0)
        a1, e1 = m1.resolve_click("ArrowDown", None, None, chat_id="nc")
        check("не-клик: resolve_click отказывает без каскада (снапшотов 0)",
              a1 is None and e1 and state["snap_calls"] == 0)
        check("не-клик: класс not_a_click в аудите",
              any(r.get("fail_reason") == "not_a_click" for r in _recs(m1, "nc")))

        # «закрыть на на джем» — сдвоенный предлог уходит до резолва
        seen_goals = []
        orig_re = Spy._resolve_element

        def _spy_re(self, goal, *a, **kw):
            seen_goals.append(goal)
            return orig_re(self, goal, *a, **kw)

        Spy._resolve_element = _spy_re
        try:
            state.update(items=[_it(0, "a", "Новости")])
            m1.resolve_click("нажми на на новости", None, None, chat_id="dp")
            m1.resolve_click("закрыть на на джем", None, None, chat_id="dp2")
        finally:
            Spy._resolve_element = orig_re
        check("сдвоенный предлог: «на на» схлопнут",
              seen_goals and all(" на на " not in f" {g} " for g in seen_goals)
              and any("на джем" in g for g in seen_goals))
        check("_DUP_PREP_RE: «в в корзине» → «в корзине», «на нас» не трогаем",
              ccm._DUP_PREP_RE.sub(r"\1", "добавить в в корзину")
              == "добавить в корзину"
              and ccm._DUP_PREP_RE.sub(r"\1", "на нас") == "на нас")

        # ── 2. Бюджет каскада ──
        check("бюджет: дефолт resolve_budget_sec = 25", make().resolve_budget_sec == 25.0)
        check("бюджет: из конфига, не меньше 1 с",
              make({**CFG, "resolve_budget_sec": 7}).resolve_budget_sec == 7.0
              and make({**CFG, "resolve_budget_sec": 0}).resolve_budget_sec == 1.0)
        mb = make({**CFG, "resolve_budget_sec": 1})
        rb = _Router(texts=["no"] * 5, imgs=["no"] * 5)
        state.update(items=[_it(0, "a", "Новости"), _it(1, "a", "О нас")],
                     snap_delay=1.05, snap_calls=0, goal_snap=0)
        ab, eb = mb.resolve_click("renoir impressionist", None, rb, chat_id="bud")
        state["snap_delay"] = 0.0
        recs_b = _recs(mb, "bud")
        check("бюджет: за дедлайном ярусы не зовутся (ни LLM, ни vision)",
              ab is None and not rb.prompts and rb.img_calls == 0)
        check("бюджет: один снапшот, без повторного и целевого",
              state["snap_calls"] == 1 and state["goal_snap"] == 0)
        check("бюджет: честный отказ называет бюджет, класс budget в аудите",
              eb and "не успел" in eb
              and any(r.get("fail_reason") == "budget" for r in recs_b))
        check("бюджет: resolve_ms в аудите",
              any(int(r.get("resolve_ms") or 0) >= 1000 for r in recs_b))

        # resolve_ms у успешного выбора
        mo = make()
        state.update(items=[_it(0, "a", "Контакты"), _it(1, "a", "О нас")])
        ao, eo = mo.resolve_click("контакты", None, None, chat_id="ok")
        check("resolve_ms: у успешного клика в choose",
              eo is None and isinstance(ao["choose"].get("resolve_ms"), int))

        # ── 3. Попытки «закрой X» делят один снапшот ──
        mc = make()
        calls = {"n": 0}
        orig_ss = Spy._snapshot_state

        def _cnt_ss(self, *a, **kw):
            calls["n"] += 1
            return orig_ss(self, *a, **kw)

        Spy._snapshot_state = _cnt_ss
        try:
            state.update(items=[_it(0, "a", "Новости"), _it(1, "a", "О нас")])
            ac, ec = mc.resolve_click("сверни джем", None, None, chat_id="cl")
        finally:
            Spy._snapshot_state = orig_ss
        check("закрой X: три попытки — снапшот + один повтор, не шесть",
              ac is None and calls["n"] <= 2)

        # ── 4. Уверенный дешёвый выбор не оспаривается целевым снапшотом ──
        mg = make()
        state.update(items=[_it(0, "a", "Последние новости сайта"),
                            _it(1, "a", "О нас")], goal_snap=0)
        hunts = {"n": 0}
        orig_hunt = Spy._scroll_hunt

        def _cnt_hunt(self, *a, **kw):
            hunts["n"] += 1
            return orig_hunt(self, *a, **kw)

        Spy._scroll_hunt = _cnt_hunt
        try:
            ag, eg = mg.resolve_click("новости", None, None, chat_id="lead")
            check("явный лидер (скор < 90): целевой снапшот не снимается",
                  eg is None and ag["idx"] == 0 and state["goal_snap"] == 0)
            # Слабый выбор (основа слова) — целевой снапшот можно, доскролл нет
            state.update(items=[_it(0, "a", "Контакты"), _it(1, "a", "О нас")],
                         goal_snap=0)
            ag2, eg2 = mg.resolve_click("контакт", None, None, chat_id="lead2")
            check("найденный выбор: доскролл-поиск не запускается",
                  eg2 is None and ag2["idx"] == 0 and hunts["n"] == 0)
            state.update(items=[_it(0, "a", "О нас")], goal_snap=0)
            mg.resolve_click("renoir", None, None, chat_id="lead3")
            check("промах: доскролл-поиск запускается",
                  hunts["n"] == 1 and state["goal_snap"] >= 1)
        finally:
            Spy._scroll_hunt = orig_hunt

        # ── 5. Пул широкого LLM-резолва и класс отказа ──
        mw = make({**CFG, "vision_fallback": False})
        junk = [_it(i, "span", t) for i, t in enumerate(
            ["0:13", "/", "1:30", "•", "0:48 / 8:13", "33 тыс. • 1 г. назад"])]
        real = [_it(100 + i, "a", f"Раздел {i}") for i in range(10)]
        rw = _Router(texts=["no"])
        widx, wmeta = mw._llm_wide_pick("почта", junk + real, rw, host="x.ru")
        prompt = rw.prompts[0] if rw.prompts else ""
        check("широкий пул: служебные обрывки не в промпте",
              "0:13" not in prompt and "1:30" not in prompt
              and "•" not in prompt and "Раздел 3" in prompt)
        check("широкий пул: n_pool — сколько реально ушло в промпт",
              wmeta and wmeta.get("n_pool") == 10)
        check("широкий пул: промпт на английском + языковая строка",
              prompt.startswith("Task: click") and "Reply" in prompt)
        check("_wide_label_ok: «2», «×», «Войти» — да; «/», «0:13» — нет",
              ccm._wide_label_ok(_it(0, "a", "2"))
              and ccm._wide_label_ok(_it(0, "a", "×"))
              and ccm._wide_label_ok(_it(0, "a", "Войти"))
              and not ccm._wide_label_ok(_it(0, "a", "/"))
              and not ccm._wide_label_ok(_it(0, "a", "0:13")))

        fk = ComputerControlManager._resolve_fail_kind
        check("класс: «нет» при цели вне снапшота → not_in_snapshot",
              fk({"candidates": [{"idx": 1}], "llm_response": "no",
                  "goal_absent": True}) == "not_in_snapshot")
        check("класс: «нет» при цели на странице → llm_veto",
              fk({"candidates": [{"idx": 1}], "llm_response": "no",
                  "goal_absent": False}) == "llm_veto")
        check("класс: бюджет → budget; вето подписи → label_mismatch",
              fk({"budget_hit": True, "candidates": []}) == "budget"
              and fk({"veto": "label_mismatch"}) == "label_mismatch")

        # Сквозь каскад: цели нет на странице → not_in_snapshot + n_pool
        state.update(items=[_it(i, "a", f"Раздел {i}") for i in range(5)])
        mw.resolve_click("почтовый ящик", None, _Router(texts=["no"] * 3),
                         chat_id="nis")
        r_nis = [r for r in _recs(mw, "nis") if r.get("kind") == "resolve_fail"]
        check("каскад: цели нет в снапшоте → not_in_snapshot, n_pool в аудите",
              r_nis and r_nis[-1].get("fail_reason") == "not_in_snapshot"
              and r_nis[-1].get("n_pool") == 5)
        # Скоринг нашёл кандидатов (fuzzy «кэшбек»≈«Кешбэк»), LLM сказала «нет»
        state.update(items=[_it(0, "a", "Кешбэк"),
                            _it(1, "button", "Кешбэк программа")])
        mw.resolve_click("кэшбек", None, _Router(texts=["нет"] * 3),
                         chat_id="vt")
        r_vt = [r for r in _recs(mw, "vt") if r.get("kind") == "resolve_fail"]
        check("каскад: цель на странице, LLM «нет» → llm_veto",
              r_vt and r_vt[-1].get("fail_reason") == "llm_veto")

        # ── 6. Сверка подписи vision-выбора ──
        lc = ccm._label_goal_check
        check("подпись: «третье на новости» vs «Новости» → match",
              lc("третье на новости", "Новости") == "match")
        check("подпись: «крестик» vs «Меню» (открытый бургер) → unverified",
              lc("крестик", "Меню") == "unverified")
        check("подпись: «закрыть» vs «бургер-меню» → unverified",
              lc("закрыть", "бургер-меню") == "unverified")
        check("подпись: слово цели только в ctx → unverified",
              lc("третье на новости", "Читать далее", None,
                 "Новости дня") == "unverified")
        check("подпись: чужое слово → mismatch",
              lc("renoir", "Новости") == "mismatch")
        check("_goal_in_label: порядковое/иконочное слово не требует совпадения",
              ccm._goal_in_label("третье видео", "Как испечь хлеб")
              and not ccm._goal_in_label("renoir", "Новости"))
        mv = make()
        meta_v = {}
        vetoed = mv._veto_model_pick("крестик", _it(0, "button", "Меню"),
                                     meta_v, "x.ru", "гибрид")
        check("_veto_model_pick: unverified — не вето, но force_confirm",
              vetoed is False and meta_v.get("force_confirm")
              and meta_v.get("label_unverified"))

        # Гибрид ветирует «Новости» для «renoir», зоны независимо указывают на
        # тот же элемент с той же подписью → клик по DOM-метке с подтверждением
        state.update(items=[_it(0, "a", "Новости", x=0.0, y=0.0,
                                w=100.0, h=40.0)],
                     boxes=[{"x": 0.0, "y": 0.0, "w": 100.0, "h": 40.0,
                             "text": "Новости"}])
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        ba.screenshot_viewport = lambda host=None, tab_id=None: png
        orig_draw = ccm._draw_candidate_boxes
        ccm._draw_candidate_boxes = lambda shot, cands: shot
        try:
            mz = make()
            rz = _Router(texts=["no"] * 4, imgs=["1", "1"])
            az, ez = mz.resolve_click("renoir", None, rz, chat_id="agree")
            check("согласие гибрида и зон: клик по элементу гибрида",
                  ez is None and az and az.get("idx") == 0
                  and not az.get("point"))
            check("согласие гибрида и зон: только с подтверждением",
                  az and az.get("force_confirm") and mz.needs_confirm(az))
            check("согласие гибрида и зон: vision-вызовов ровно два",
                  rz.img_calls == 2)
            # Зона с ДРУГОЙ подписью поверх того же места — не согласие
            state["boxes"] = [{"x": 0.0, "y": 0.0, "w": 100.0, "h": 40.0,
                               "text": "Подписаться"}]
            mz2 = make()
            az2, ez2 = mz2.resolve_click("renoir", None,
                                         _Router(texts=["no"] * 4,
                                                 imgs=["1", "1"]),
                                         chat_id="noagree")
            check("зона с чужой подписью: честный отказ, label_mismatch",
                  az2 is None and ez2
                  and any(r.get("fail_reason") == "label_mismatch"
                          for r in _recs(mz2, "noagree")))
            check("_zone_same_label: пустая подпись — не противоречие",
                  ccm._zone_same_label({"text": ""}, {"text": "Меню"})
                  and not ccm._zone_same_label({"text": "Подписаться"},
                                               {"text": "Новости"}))
        finally:
            ccm._draw_candidate_boxes = orig_draw
            ba.screenshot_viewport = mocks["screenshot_viewport"]
            state["boxes"] = []

        # ── 7. Хост в аудите сбоя снапшота; SSO-фолбэк по хосту ──
        ms = make()
        state["snap_raise"] = lambda host: RuntimeError("no tab")
        a_se, e_se = ms.resolve_click("новости", "example.edu", None,
                                      chat_id="se")
        state["snap_raise"] = None
        r_se = [r for r in _recs(ms, "se") if r.get("kind") == "resolve_fail"]
        check("snapshot_error: хост в записи аудита",
              a_se is None and r_se
              and r_se[-1].get("fail_reason") == "snapshot_error"
              and r_se[-1].get("host"))
        ms2 = make()
        state["visible"] = ("https://auth.school.example.com/login?state=abc",
                            "auth.school.example.com")
        state["snap_raise"] = (lambda host: RuntimeError("stale url")
                               if host and "://" in str(host) else None)
        state.update(url="https://auth.school.example.com/", host="auth.school.example.com",
                     items=[_it(0, "button", "Войти")])
        a_sso, e_sso = ms2.resolve_click("войти", None, None, chat_id="sso")
        state["snap_raise"] = None
        state["visible"] = None
        state.update(url="https://x.ru", host="x.ru")
        check("SSO: устаревший полный URL → снапшот по хосту",
              e_sso is None and a_sso and a_sso["idx"] == 0
              and a_sso["host"] == "auth.school.example.com")

        # ── 8. Видимая вкладка и клик в невидимой ──
        mt = make()
        mt._last_host, mt._last_tab_id = "a.ru", None
        state["visible"] = ("https://a.ru/", "a.ru")
        mt._follow_visible_tab()          # база: видна отслеживаемая
        check("видимая вкладка: без переключения цель — отслеживаемая",
              mt._last_host == "a.ru")
        state["visible"] = ("https://b.ru/page", "b.ru")
        mt._follow_visible_tab()
        check("видимая вкладка: пользователь переключился → цель видимая",
              mt._last_host == "b.ru" and mt._last_tab_id is None)
        mt._last_host, mt._last_tab_id = "c.ru", 5
        state["visible"] = ("https://b.ru/page", "b.ru")
        mt._follow_visible_tab()
        check("видимая вкладка: бот открыл другой сайт без подъёма окна — "
              "видимое не менялось, цель прежняя", mt._last_tab_id == 5
              and mt._last_host == "c.ru")
        state["visible"] = None

        mp = make()
        state["pages"] = [("https://x.ru/a", "x.ru"), ("https://x.ru/b", "x.ru")]
        state["page_items"] = {"https://x.ru/b": (
            "https://x.ru/b", "x.ru", [_it(3, "a", "Технологии баз данных")])}
        state["visible"] = ("https://x.ru/a", "x.ru")
        found = mp._element_on_other_pages("технологии баз данных",
                                           "https://x.ru/a", only_host="x.ru")
        check("другая вкладка: найдено в невидимой → other_tab + force_confirm",
              found is not None and found[5].get("other_tab")
              and found[5].get("force_confirm"))
        act_ot = ComputerControlManager._act_from_meta(
            {"kind": "click", "idx": 3, "element": "Т", "host": "x.ru"},
            found[5] if found else {}, 7)
        check("другая вкладка: действие требует подтверждения при confirm:false",
              act_ot.get("force_confirm") and mp.needs_confirm(act_ot)
              and act_ot.get("tab_id") == 7)
        check("подтверждение называет хост вкладки",
              "x.ru" in mp.describe(act_ot))
        found_dl = mp._element_on_other_pages(
            "технологии баз данных", "https://x.ru/a", only_host="x.ru",
            deadline=time.monotonic() - 1)
        check("другая вкладка: за дедлайном вкладки не снимаются",
              found_dl is None)
        state["visible"] = ("https://x.ru/b", "x.ru")
        found_v = mp._element_on_other_pages("технологии баз данных",
                                             "https://x.ru/a", only_host="x.ru")
        check("другая вкладка: найдено в видимой — без принудительного вопроса",
              found_v is not None and not found_v[5].get("other_tab"))
        state.update(pages=[], page_items={}, visible=None)
        check("_act_from_meta: retried переносится в действие",
              ComputerControlManager._act_from_meta(
                  {"kind": "click"}, {"retried": True}, None).get("retried"))

        # ── 9. Клик агента задач: choose/origin ──
        from app.features.task_agent import TaskAgent
        got = []
        fake = types.SimpleNamespace(
            _execute=lambda run, chat, router, a, line: (got.append(a),
                                                         ("progress", None))[1],
            _ask_confirm=lambda run, a, line: (got.append(a),
                                               ("await", None))[1],
            _phrase=lambda key, text, **kw: text,
            _is_cart_add=TaskAgent._is_cart_add,
            _is_cart_add_item=TaskAgent._is_cart_add_item,
            _checkout_phase=TaskAgent._checkout_phase,
            _option_unchosen=lambda run, item, obs: TaskAgent._option_unchosen(
                fake, run, item, obs),
            _early_item=lambda run, item, obs: TaskAgent._early_item(
                fake, run, item, obs),
            _done_item=lambda run, item, obs: TaskAgent._done_item(
                fake, run, item, obs),
            _repeat_click=lambda run, item, obs: None)
        TaskAgent._do_element(fake, {"history": []}, "c", None, {"action": "click"},
                              _it(4, "a", "Далее"),
                              {"host": "x.ru", "url": "https://x.ru",
                               "tab_id": None})
        check("агент задач: клик с choose.path=task_agent и origin=task",
              got and got[0].get("choose") == {"path": "task_agent"}
              and got[0].get("origin") == "task")

        # ── 10. Бюджет: сомнительный выбор за дедлайном; резерв под vision ──
        cd = ComputerControlManager._choice_doubt
        c_tie = {"path": "score", "candidates": [
            {"idx": 0, "text": "Изменить", "score": 100.0},
            {"idx": 1, "text": "Изменить", "score": 99.5}]}
        check("_choice_doubt: ничья одноимённых / слабый скор / уверенный",
              cd(0, c_tie) == "ничья одноимённых"
              and (cd(0, {"path": "score", "candidates": [
                  {"idx": 0, "text": "А", "score": 40.0}]}) or "")
              .startswith("слабый")
              and cd(0, {"path": "score", "candidates": [
                  {"idx": 0, "text": "Контакты", "score": 100.0}]}) is None
              and cd(0, {**c_tie, "path": "goal_sole"}) is None)

        gates = {"n": 0}
        orig_gate = Spy._vision_gate_choice

        def _cnt_gate(self, *a, **kw):
            gates["n"] += 1
            return orig_gate(self, *a, **kw)

        Spy._vision_gate_choice = _cnt_gate
        try:
            mbd = make({**CFG, "resolve_budget_sec": 1})
            rbd = _Router(imgs=["1"] * 3)
            state.update(items=[_it(0, "button", "Изменить", ctx="Маргарита"),
                                _it(1, "button", "Изменить", ctx="Кола")],
                         snap_delay=1.05)
            a_bd, e_bd = mbd.resolve_click("изменить", None, rbd,
                                           chat_id="bdoubt")
            check("бюджет + ничья: vision-проверки нет, клик — только с "
                  "подтверждением",
                  e_bd is None and a_bd and gates["n"] == 0
                  and rbd.img_calls == 0 and a_bd.get("force_confirm")
                  and mbd.needs_confirm(a_bd)
                  and a_bd["choose"].get("budget_doubt"))
            state.update(items=[_it(0, "a", "Контакты"), _it(1, "a", "О нас")])
            a_bs, e_bs = mbd.resolve_click("контакты", None, rbd,
                                           chat_id="bsure")
            check("бюджет + уверенный выбор: без лишнего вопроса",
                  e_bs is None and a_bs and not a_bs.get("force_confirm")
                  and not mbd.needs_confirm(a_bs))
            state["snap_delay"] = 0.0
            # Выбор после повторного снапшота тоже идёт через vision-проверку
            seq = {"n": 0}

            def _snap_seq(host=None, tab_id=None):
                seq["n"] += 1
                its = [] if seq["n"] == 1 else [
                    _it(0, "button", "Изменить", ctx="Маргарита"),
                    _it(1, "button", "Изменить", ctx="Кола")]
                return state["url"], state["host"], its

            ba.snapshot_elements = _snap_seq
            try:
                gates["n"] = 0
                a_rt, e_rt = make().resolve_click("изменить", None, _Router(),
                                                  chat_id="retry_gate")
            finally:
                ba.snapshot_elements = mocks["snapshot_elements"]
            check("повторный снапшот: ничья после повтора — через "
                  "vision-проверку, не вслепую",
                  e_rt is None and a_rt and a_rt.get("retried")
                  and gates["n"] == 1)
            # Приватная корзина: vision-проверки нет — ничья только с «да»
            u0, h0 = state["url"], state["host"]
            state.update(url="https://shop.example/cart", host="shop.example",
                         items=[_it(0, "button", "Изменить", ctx="Маргарита"),
                                _it(1, "button", "Изменить", ctx="Кола")])
            try:
                mpc = make()
                a_pc, e_pc = mpc.resolve_click("изменить", None, _Router(),
                                               chat_id="priv_tie")
            finally:
                state.update(url=u0, host=h0)
            check("приватная страница + ничья: клик только с подтверждением",
                  e_pc is None and a_pc and a_pc.get("force_confirm")
                  and mpc.needs_confirm(a_pc)
                  and a_pc["choose"].get("private_doubt"))
        finally:
            Spy._vision_gate_choice = orig_gate
            state["snap_delay"] = 0.0

        # Резерв: доскролл получает дедлайн без хвоста под vision
        order = []
        orig_hyb, orig_hunt2 = Spy._hybrid_pick, Spy._scroll_hunt

        def _spy_hyb(self, *a, **kw):
            order.append(("hybrid", time.monotonic()))
            return None, None

        def _spy_hunt(self, *a, **kw):
            order.append(("hunt", time.monotonic(), kw.get("deadline")))
            return orig_hunt2(self, *a, **kw)

        Spy._hybrid_pick, Spy._scroll_hunt = _spy_hyb, _spy_hunt
        try:
            state.update(items=[_it(0, "a", "Подписаться"),
                                _it(1, "button", "")])
            m25 = make({**CFG, "wide_mode": "hybrid"})
            t_s = time.monotonic()
            m25.resolve_click("renoir", None, _Router(), chat_id="res25")
            h25 = [o for o in order if o[0] == "hunt"]
            check("резерв: при 25 с доскролл кончается за ~9 с до дедлайна",
                  h25 and abs((t_s + 25.0 - h25[0][2]) - 9.0) < 0.5)
            order.clear()
            t_s = time.monotonic()
            make({**CFG, "wide_mode": "hybrid"}).resolve_click(
                "renoir", None, None, chat_id="res_novis")
            h0 = [o for o in order if o[0] == "hunt"]
            check("резерв: без vision доскролл получает весь бюджет",
                  h0 and abs((t_s + 25.0) - h0[0][2]) < 0.5)
            # Живая лента: шаги двигаются, DOM не затихает (0.5 с на шаг) —
            # доскролл упирается в резерв, гибрид всё равно успевает
            order.clear()
            ba.scroll_step = lambda host=None, tab_id=None: {"moved": True}
            ba.scroll_container_step = lambda host=None, tab_id=None: {
                "moved": True, "y0": 0.0}
            ba.wait_dom_idle = lambda *a, **kw: time.sleep(0.5)
            try:
                m4 = make({**CFG, "wide_mode": "hybrid",
                           "resolve_budget_sec": 4})
                t_s = time.monotonic()
                a4, e4 = m4.resolve_click("лайк", None, _Router(),
                                          chat_id="feed")
            finally:
                for k in ("scroll_step", "scroll_container_step",
                          "wait_dom_idle"):
                    setattr(ba, k, mocks[k])
            kinds = [o[0] for o in order]
            check("лента: цель без текстовых совпадений — гибрид ДО "
                  "доскролла, один раз",
                  kinds[:2] == ["hybrid", "hunt"]
                  and kinds.count("hybrid") == 1)
            check("лента: отказ честный, а не «не успел за бюджет»",
                  a4 is None and e4 and "не успел" not in e4
                  and time.monotonic() - t_s < 4.5)
            # Текст цели есть на странице (кандидаты скоринга) — гибрид не
            # раньше доскролла
            order.clear()
            state.update(items=[_it(0, "a", "О компании", vp=False),
                                _it(1, "a", "Новости")])
            make({**CFG, "wide_mode": "hybrid"}).resolve_click(
                "компании отчёт", None, _Router(texts=["no"] * 3),
                chat_id="txt")
            kinds = [o[0] for o in order]
            check("цель с текстом на странице: гибрид не раньше доскролла",
                  "hybrid" not in kinds[:1])
        finally:
            Spy._hybrid_pick, Spy._scroll_hunt = orig_hyb, orig_hunt2
    finally:
        for k, v in saved.items():
            if v is not None:
                setattr(ba, k, v)

    print(f"\nИтого: {total - fails}/{total} OK, FAIL: {fails}")


if __name__ == "__main__":
    main()

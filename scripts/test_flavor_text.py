"""Тесты flavor_text: банк реплик CC (плейсхолдеры, err обязана хранить
{detail}), парсинг ответа генератора, hash-инвалидация банка при смене
system_prompt, фоновое пополнение с дедупликацией.
LLM/браузер — моки. Запуск: python -m scripts.test_flavor_text"""

import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


def main():
    tmp = Path(tempfile.mkdtemp(prefix="flavor_data_"))
    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    from app.features import flavor_text as ft

    # Банк — во временный каталог; живые вызовы управляемо мокаем
    ft._bank_path = lambda context: tmp / f"{context}_bank.json"
    _from_live_real = ft._from_live
    _generate_safe_real = ft._generate_safe
    _generate_kinds_real = ft._generate_kinds
    _google_cc_chat_real = ft._google_cc_chat

    SP = "Ты — Коннор, андроид RK800."
    bot = SimpleNamespace(context="t1",
                          persona=SimpleNamespace(system_prompt=SP),
                          computer_control=None,
                          router=None)

    # ── 1. Банк: ok/err, плейсхолдеры, суть ошибки ──
    ft._save_bank("t1", {
        "_meta": {"prompt_hash": ft._prompt_hash(SP), "generated_at": time.time()},
        "kinds": {
            "open": {"ok": ["*Кручу монетку.* Открыл {host}.", "Готово — {host}."],
                     "err": ["Не вышло: {detail}.", "*Монетка замирает.* {detail}"]},
            "generic": {"ok": ["Сделано."], "err": ["Сбой: {detail}."]}}})

    r = ft.cc_reply(bot, {"kind": "url", "host": "youtube.com"}, True)
    check("bank ok: плейсхолдер {host} подставлен, живой вызов не нужен",
          r is not None and "youtube.com" in r)

    r = ft.cc_reply(bot, {"kind": "url", "host": "youtube.com"}, False,
                    "сайт не отвечает")
    check("bank err: суть ошибки сохранена ({detail})",
          r is not None and "сайт не отвечает" in r)

    r = ft.cc_reply(bot, {"kind": "unknown_kind"}, True)
    check("bank: неизвестный kind → generic", r == "Сделано.")

    ft._from_live = lambda *a, **kw: None
    ft._save_bank("t1", {"kinds": {"generic": {"ok": [],
                                               "err": ["Простите, не вышло."]}}})
    check("bank err без {detail} — отбракована (промах банка → None)",
          ft.cc_reply(bot, None, False, "x") is None)
    # Кейс 19.09, «пауза»: key-действие без element — фраза «Выполнено:
    # {element}.» подставила бы пустоту («Выполнено: .») — отбраковываем,
    # берём вариант без плейсхолдеров
    ft._save_bank("t1", {"kinds": {"key": {"ok": ["Выполнено: {element}."],
                                           "err": ["Сбой: {detail}."]},
                                   "generic": {"ok": ["Сделано."],
                                               "err": ["Сбой: {detail}."]}}})
    r = ft.cc_reply(bot, {"kind": "key", "media": "toggle"}, True)
    check("bank ok: плейсхолдер с пустым значением — вариант пропущен",
          r == "Сделано.")
    r = ft.cc_reply(bot, {"kind": "key", "media": "toggle",
                          "element": "пробел"}, True)
    check("bank ok: непустой element — плейсхолдерная фраза годится",
          r == "Выполнено: пробел.")
    ft._from_live = _from_live_real

    # ── 1б. _pick: перебор ВСЕХ кандидатов, а не sample(3) (задача №9
    # аудита) — с 5 кандидатами, из которых годен только 1, sample(3)
    # промахивался мимо него в ~40% вызовов (C(4,3)/C(5,3)); полный перебор
    # в случайном порядке находит его каждый раз.
    ft._save_bank("t1c", {"kinds": {"generic": {
        "ok": [],
        "err": ["Без детали 1.", "Без детали 2.", "Без детали 3.",
               "Без детали 4.", "Годная: {detail}"],
    }}})
    ft._from_live = lambda *a, **kw: None  # не должен понадобиться — подстраховка
    bot1c = SimpleNamespace(context="t1c", computer_control=None, router=None)
    try:
        misses = sum(
            1 for _ in range(30)
            if ft.cc_reply(bot1c, None, False, "причина") is None
        )
    finally:
        ft._from_live = _from_live_real
    check("_pick: единственный годный err-вариант находится всегда (30/30), "
          "не иногда", misses == 0)

    # ── 2. Промах банка → живой google, чистка ответа ──
    ft._save_bank("t1", {"kinds": {}})
    ft._google_cc_chat = lambda context: SimpleNamespace(
        get_response=lambda messages, **kw: "  «*Киваю.* Готово.»  ")
    r = ft.cc_reply(bot, {"kind": "click", "element": "Play", "host": "x.com"},
                    True)
    check("live: промах банка → google, обрамляющие кавычки срезаны",
          r == "*Киваю.* Готово.")

    # ── 2б. Живой вызов не блокируется генерацией/локом (кейс 18.09) ──
    _live_calls = []
    ft._google_cc_chat = lambda context: SimpleNamespace(
        get_response=lambda messages, **kw: _live_calls.append(kw) or "ок")
    ft._GEN_STARTED.add("t1")
    try:
        check("live: генерация банка идёт — живой вызов пропущен (шаблон)",
              ft.cc_reply(bot, {"kind": "click"}, True) is None
              and not _live_calls)
    finally:
        ft._GEN_STARTED.discard("t1")
    r = ft.cc_reply(bot, {"kind": "click"}, True)
    check("live: лок ждётся не дольше _LIVE_LOCK_TIMEOUT_SEC",
          r == "ок" and _live_calls
          and _live_calls[0].get("lock_timeout") == ft._LIVE_LOCK_TIMEOUT_SEC)

    # ── 2в. Генерация — отдельный канал cc_gen (свой инстанс/лок) ──
    _gen_channels = []
    ft._google_cc_chat = lambda context, channel="cc": (
        _gen_channels.append(channel),
        SimpleNamespace(get_response=lambda messages, **kw:
                        '{"open": {"ok": ["Открыл {host}."], '
                        '"err": ["Не вышло: {detail}."]}}'))[1]
    gen = ft._generate_kinds("t1", SP, None, 1, 1)
    check("generate: канал cc_gen, ответ распарсен",
          _gen_channels == ["cc_gen"] and gen.get("kinds", {}).get(
              "open", {}).get("ok") == ["Открыл {host}."])
    # Генерация служебных фраз — ОТДЕЛЬНЫМ вызовом (кейс 19.09: объединённый
    # промпт не укладывался в таймаут google AI Mode)
    ft._google_cc_chat = lambda context, channel="cc": SimpleNamespace(
        get_response=lambda messages, **kw:
        '{"cc_mode_on": ["*Киваю.* Режим управления включён."], '
        '"scenario_saved": ["Записал «{name}» — {steps} шагов."], '
        '"scenario_not_found": ["нет такого"], "junk": ["x"]}')
    genp = ft._generate_phrases("t1", SP, None)
    check("generate phrases: валидные приняты, невалидная выкинута",
          genp.get("phrases", {}).get("cc_mode_on")
          == ["*Киваю.* Режим управления включён."]
          and genp["phrases"].get("scenario_saved")
          == ["Записал «{name}» — {steps} шагов."]
          and "scenario_not_found" not in genp["phrases"]
          and "junk" not in genp["phrases"])
    ft._google_cc_chat = _google_cc_chat_real  # восстановить настоящую

    # Кэш инстансов — по (контекст, канал)
    import app.features.web_llm as _wl_mod
    _wc_orig = _wl_mod.WebChatLLM
    _made = []
    _wl_mod.WebChatLLM = lambda *a, **kw: _made.append(
        (a, kw)) or SimpleNamespace(args=a, kwargs=kw)
    try:
        ft._WC.clear()
        c1 = ft._google_cc_chat("tx")
        c2 = ft._google_cc_chat("tx", channel="cc_gen")
        c3 = ft._google_cc_chat("tx")
        check("google_cc_chat: каналы — разные инстансы, повтор — из кэша",
              c1 is not c2 and c1 is c3 and len(_made) == 2
              and _made[1][1].get("channel") == "cc_gen")
    finally:
        _wl_mod.WebChatLLM = _wc_orig
        ft._WC.clear()

    # ── 3. Парсинг ответа генератора ──
    raw = ('Вот JSON:\n```json\n{"open": {"ok": ["Открыл {host}."], '
           '"err": ["Не вышло: {detail}.", "Сломалось."]}, '
           '"click": {"ok": ["Нажал {element} {evil}"], "err": []}, '
           '"unknown_kind": {"ok": ["x"], "err": []}}}```')
    parsed = ft._parse_bank_json(raw)
    p_kinds = parsed.get("kinds", {})
    check("parse: валидные ok/err приняты; err без {detail} выкинута",
          p_kinds.get("open", {}).get("ok") == ["Открыл {host}."]
          and p_kinds.get("open", {}).get("err") == ["Не вышло: {detail}."])
    check("parse: неизвестный плейсхолдер и неизвестный kind выкинуты",
          "click" not in p_kinds and "unknown_kind" not in p_kinds)
    check("parse: мусор → {}", ft._parse_bank_json("никакого json") == {})

    # ── 3б. Парсинг phrases (служебные фразы голосом персоны) ──
    raw_ph = ('{"open": {"ok": ["Открыл {host}."], '
              '"err": ["Провал: {detail}."]}, '
              '"phrases": {'
              '"cc_mode_on": ["*Кручу монетку.* Режим управления включён.", '
              '"Второй вариант."], '
              '"scenario_saved": ["Записал «{name}» — {steps} шагов."], '
              '"scenario_not_found": ["Без плейсхолдера имени."], '
              '"cc_mode_off": ["Вышел {evil}."], '
              '"hacker_key": ["x"]}}')
    p_ph = ft._parse_bank_json(raw_ph).get("phrases", {})
    check("parse phrases: валидные приняты, чужой ключ выкинут",
          len(p_ph.get("cc_mode_on", [])) == 2 and "hacker_key" not in p_ph)
    check("parse phrases: без обязательного {name} и с чужим плейсхолдером — "
          "выкинуты",
          "scenario_not_found" not in p_ph and "cc_mode_off" not in p_ph
          and p_ph.get("scenario_saved") == ["Записал «{name}» — {steps} шагов."])

    # ── 3в. phrase(): выдача из банка, подстановка, честный шаблон ──
    ft._save_bank("t1ph", {"phrases": {
        "cc_mode_on": ["*Кручу монетку.* Режим управления включён."],
        "scenario_saved": ["Запомнил «{name}»: {steps} шагов."]}})
    check("phrase: банковская фраза вместо шаблона",
          "Кручу монетку" in ft.phrase("t1ph", "cc_mode_on", "ШАБЛОН"))
    check("phrase: плейсхолдеры подставлены",
          ft.phrase("t1ph", "scenario_saved", "ШАБЛОН",
                    name="заказ пиццы", steps=4)
          == "Запомнил «заказ пиццы»: 4 шагов.")
    check("phrase: нет ключа — честный шаблон",
          ft.phrase("t1ph", "cc_mode_off", "ШАБЛОН") == "ШАБЛОН")
    check("phrase: нет банка — честный шаблон",
          ft.phrase("t_none", "cc_mode_on", "ШАБЛОН") == "ШАБЛОН")

    # ── 4. ensure_flavor_bank: hash-инвалидация ──
    ft._save_bank("t2", {"_meta": {"prompt_hash": ft._prompt_hash(SP)},
                         "kinds": {"generic": {"ok": ["Сделано."],
                                               "err": ["Сбой: {detail}."]}},
                         "phrases": {"cc_mode_on": ["Режим включён."]}})
    called = []
    ft._generate_safe = lambda *a, **kw: called.append(1)
    try:
        ft.ensure_flavor_bank(context="t2", system_prompt=SP, background=False)
        check("ensure: хэш совпал — генерация не запускалась", not called)
        # Банк старой схемы (kinds есть, phrases нет) — на догенерацию
        ft._save_bank("t2", {"_meta": {"prompt_hash": ft._prompt_hash(SP)},
                             "kinds": {"generic": {"ok": ["Сделано."],
                                                   "err": ["Сбой: {detail}."]}}})
        ft.ensure_flavor_bank(context="t2", system_prompt=SP, background=False)
        check("ensure: нет секции phrases — генерация запущена", called == [1])
        # мок _generate_safe не чистит _GEN_STARTED — снимаем вручную
        ft._GEN_STARTED.discard("t2")
        ft._save_bank("t2", {"_meta": {"prompt_hash": ft._prompt_hash(SP)},
                             "kinds": {"generic": {"ok": ["Сделано."],
                                                   "err": ["Сбой: {detail}."]}},
                             "phrases": {"cc_mode_on": ["Режим включён."]}})
        ft.ensure_flavor_bank(context="t2", system_prompt="Другой промпт.",
                              background=False)
        check("ensure: хэш изменился — генерация запущена", called == [1, 1])
    finally:
        ft._generate_safe = _generate_safe_real
        ft._GEN_STARTED.discard("t2")
    check("ensure: пустой system_prompt — тихий выход",
          ft.ensure_flavor_bank(context="t9", system_prompt="  ",
                                background=False) is None)

    # ── 4б. ensure_flavor_bank: конкурентный запуск не плодит две генерации
    # (задача №9 аудита — "context in _GEN_STARTED" и .add(context) были
    # двумя отдельными шагами без лока между ними: два потока могли оба
    # пройти проверку до того, как любой из них успевал добавить context) ──
    import threading as _threading
    calls_concurrent = []
    calls_lock = _threading.Lock()

    def _counted_generate(*a, **kw):
        with calls_lock:
            calls_concurrent.append(1)

    ft._save_bank("t_race", {})  # банк «устарел» — генерация должна запуститься
    ft._generate_safe = _counted_generate
    try:
        threads = [
            _threading.Thread(target=ft.ensure_flavor_bank,
                              kwargs=dict(context="t_race", system_prompt=SP,
                                          background=True))
            for _ in range(16)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        check("ensure: 16 параллельных вызовов — ровно одна фоновая генерация",
              len(calls_concurrent) == 1)
    finally:
        ft._generate_safe = _generate_safe_real
        ft._GEN_STARTED.discard("t_race")

    # ── 5. Пополнение: возраст + простой, merge с дедупом ──
    old = time.time() - (ft.FLAVOR_BANK_REFRESH_DAYS + 1) * 86400
    ft._save_bank("t3", {"_meta": {"prompt_hash": ft._prompt_hash(SP),
                                   "generated_at": old},
                         "kinds": {"open": {"ok": ["Открыл {host}."],
                                            "err": ["Сбой: {detail}."]}}})
    ft._generate_kinds = lambda *a, **kw: {
        "kinds": {"open": {"ok": ["Открыл {host}.", "Готово, {host} on-line."],
                           "err": ["Не смог: {detail}."]}}}
    try:
        done = ft.maybe_topup_flavor_bank("t3", SP, None, idle_ok=True)
        bank3 = ft._load_bank("t3")
        check("topup: merge с дедупом — дубль не добавлен, новые добавлены",
              done
              and bank3["kinds"]["open"]["ok"] == ["Открыл {host}.",
                                                   "Готово, {host} on-line."]
              and bank3["kinds"]["open"]["err"] == ["Сбой: {detail}.",
                                                    "Не смог: {detail}."])
        ft._generate_kinds = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("не должен вызываться"))
        check("topup: свежий банк — пропуск",
              ft.maybe_topup_flavor_bank("t3", SP, None, idle_ok=True) is False)
        check("topup: пользователь активен — пропуск",
              ft.maybe_topup_flavor_bank("t3", SP, None, idle_ok=False) is False)
    finally:
        ft._generate_kinds = _generate_kinds_real

    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

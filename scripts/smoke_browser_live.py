"""Живой смоук-тест браузерного контура (НЕ офлайн-сьюта — поднимает браузер
бота на выделенном automation-профиле и гоняет реальные страницы).

Проверяет на синтетической странице (пишется во временный каталог):
  * авто-закрытие куки-оверлея перед снапшотом (+ audit overlay_dismiss);
  * обычный клик end-to-end (resolve → execute, closed-loop);
  * элемент внутри iframe (снапшот-обход фреймов + клик);
  * элемент внутри open shadow root;
  * элемент, появляющийся в DOM только после прокрутки (доскролл-поиск);
  * detect_antibot на нормальной странице (нет ложного срабатывания);
  * целевое закрытие попапа («закрой акцию дня» → крестик промо);
  * широкий LLM-резолв (цель без текстового совпадения с подписями) —
    если роутер доступен;
  * vision-фолбэк (иконка корзины-SVG без accessible name) — если роутер
    с vision;
и на реальных сайтах:
  * снапшот youtube.com (много элементов, без сбоев);
  * ввод текста в поиск wikipedia.org (без submit);
  * «перейди на вкладку X» (активация по названию) и список открытых вкладок.

Запуск: python -m scripts.smoke_browser_live
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_MAIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><title>VPC Smoke</title></head>
<body>
<h1>Тестовая страница</h1>
<button onclick="document.body.insertAdjacentHTML('beforeend','<p>нажато-обычное</p>')">Обычная кнопка</button>
<iframe src="frame.html" style="width:500px;height:300px"></iframe>
<div id="host"></div>
<script>
  const root = document.getElementById('host').attachShadow({mode: 'open'});
  const b = document.createElement('button');
  b.textContent = 'В тени';
  b.onclick = () => {
    const s = document.createElement('span');
    s.textContent = 'нажато-тень';
    root.appendChild(s);
  };
  root.appendChild(b);
</script>
<div style="height:3000px"></div>
<div id="lazy"></div>
<script>
  window.addEventListener('scroll', () => {
    if (window.scrollY > 100 && !document.getElementById('latebtn')) {
      const b = document.createElement('button');
      b.id = 'latebtn';
      b.textContent = 'Поздний элемент';
      b.onclick = () => document.body.insertAdjacentHTML('beforeend', '<p>нажато-позднее</p>');
      document.getElementById('lazy').appendChild(b);
    }
  });
</script>
<button style="position:fixed;top:8px;right:8px;width:56px;height:56px"
        onclick="document.body.insertAdjacentHTML('beforeend','<p>нажато-корзина</p>')"><svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/></svg></button>
<button onclick="document.body.insertAdjacentHTML('beforeend','<p>нажато-продолжить</p>')">Продолжить</button>
<div id="promo" style="position:fixed;right:100px;top:130px;background:#fff;border:1px solid #888;padding:18px;z-index:9998">
  <b>Акция дня</b> — скидка 20% на соусы
  <button class="promo-close" style="width:28px;height:28px;margin-left:10px"
          onclick="document.getElementById('promo').style.display='none'"></button>
</div>
<div id="prod" role="dialog" aria-modal="true"
     style="display:none;position:fixed;left:300px;top:200px;background:#fff;border:1px solid #888;padding:18px;z-index:9997">
  Карточка товара
  <button aria-label="Закрыть" style="width:28px;height:28px;margin-left:10px"
          onclick="document.getElementById('prod').style.display='none'"></button>
  <button onclick="document.body.insertAdjacentHTML('beforeend','<p>нажато-добавка</p>')">Моцарелла 125 ₽</button>
</div>
<style>.faq input:checked ~ .panel{display:block !important}</style>
<!-- FAQ-аккордеон как на dodo: label+checkbox, панель открывается CSS
     :checked (DOM не меняется), заголовок глушит клики (у dodo playwright-
     клик по div.title не активирует label) -->
<label class="faq" style="display:block;margin:12px 0;cursor:pointer">
  <input type="checkbox" style="position:absolute;opacity:0;width:1px;height:1px">
  <div class="title" onclick="event.preventDefault();event.stopPropagation()"
       style="padding:6px">Что такое кешбэк?</div>
  <div class="panel" style="display:none;padding:6px">Кешбэк — это додокоины</div>
</label>
<!-- Поля с плавающей подписью-соседом (анкета dodocontrol): без placeholder,
     без label[for] — подпись в span перед полем -->
<div class="ffield" style="margin:6px 0"><span>Имя</span><input type="text" id="fname"></div>
<div class="ffield" style="margin:6px 0"><span>Телефон</span><input type="text" id="fphone"></div>
<div class="cookie-consent" style="position:fixed;left:0;right:0;bottom:0;background:#222;color:#fff;padding:18px;z-index:9999">
  Мы используем куки.
  <button style="margin-left:12px;padding:8px 16px"
          onclick="this.parentElement.style.display='none'">Принять все</button>
</div>
</body>
</html>
"""

_FRAME_HTML = """<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><title>VPC Smoke Frame</title></head>
<body>
<button onclick="document.body.insertAdjacentHTML('beforeend','<p>нажато-фрейм</p>')">Из фрейма</button>
</body>
</html>
"""


def _write_pages() -> str:
    d = Path(tempfile.mkdtemp(prefix="vpc_smoke_page_"))
    (d / "main.html").write_text(_MAIN_HTML, encoding="utf-8")
    (d / "frame.html").write_text(_FRAME_HTML, encoding="utf-8")
    return (d / "main.html").as_uri()

ok_n = 0


def check(name, cond):
    global ok_n
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok_n = ok_n + 1 if cond else ok_n - 100


def main() -> int:
    global ok_n
    import logging
    logging.basicConfig(level=logging.WARNING)
    tmp = Path(tempfile.mkdtemp(prefix="vpc_smoke_"))
    from app.features import browser_actions as ba
    from app.features.computer_control import ComputerControlManager

    router = None
    has_vision = False
    try:
        from app.core.router import ModelRouter
        router = ModelRouter()
        has_vision = bool(router.supports_vision())
        if not has_vision:
            print("  [..] router без vision — vision-фолбэк пропустим")
    except Exception as e:
        print(f"  [..] router недоступен ({e}) — LLM-фолбэки пропустим")
        router = None

    mgr = ComputerControlManager(
        context="smoke", config={"click": True, "vision_fallback": True},
        base_dir=tmp)

    def run(goal, **kw):
        """resolve_click + execute как в проде. → (ok, detail, action)"""
        act, err = mgr.resolve_click(goal, None, router,
                                     chat_id=kw.get("chat", "smoke"))
        if act is None:
            return False, err, None
        ok, detail = mgr.execute(act, "smoke", router=router)
        return ok, detail, act

    tab_id = None
    # Браузер уже жив (профиль бота, вкладки пользователя/веб-чатов) — не наш,
    # за собой НЕ убиваем: kill в finally — только если запускали сами
    we_started = not ba._cdp_available()
    try:
        tab_id = ba.open_new_tab(_write_pages())
        mgr._last_tab_id = tab_id
        time.sleep(1.0)

        # Изоляция фикстуры: чужие вкладки (dodo и т.п. из ручных тестов)
        # ловят zero-match цели через page_fallback — «корзина» уходила в
        # хедер dodo вместо vision-фолбэка. Служебные веб-чаты и localhost/
        # file-фикстуру list_tabs не отдаёт — они не трогаются
        def _close_tabs(tids):
            def _op(w):
                n = 0
                for t in tids:
                    pg = w._pages.get(t)
                    if pg is not None and not pg.is_closed():
                        try:
                            pg.close()
                            n += 1
                        except Exception:
                            pass
                return n
            return ba._WORKER.submit(_op)
        _close_tabs([t[0] for t in ba.list_tabs()])

        # Баннер на месте до всякого резолва
        banner = ba.eval_js(None, tab_id,
                            "getComputedStyle(document.querySelector"
                            "('.cookie-consent')).display")
        check("подготовка: куки-баннер виден изначально", banner != "none")

        # 1. Первый резолв: авто-закрытие баннера + обычный клик end-to-end
        ok1, d1, _ = run("Обычная кнопка")
        check("клик end-to-end: «Обычная кнопка»", ok1)
        banner2 = ba.eval_js(None, tab_id,
                             "getComputedStyle(document.querySelector"
                             "('.cookie-consent')).display")
        check("оверлей: куки-баннер авто-закрыт до снапшота", banner2 == "none")
        audit = [json.loads(l) for l in
                 (tmp / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        check("оверлей: overlay_dismiss записан в аудит",
              any(r.get("kind") == "overlay_dismiss"
                  and r.get("value") == "Принять все" for r in audit))

        # 1.5. Целевое закрытие попапа: «закрой акцию дня» — крестик промо
        # по контексту, а не первый попавшийся
        ok1c, d1c, _ = run("закрой акцию дня")
        promo_gone = ba.eval_js(None, tab_id,
                                "document.getElementById('promo')"
                                ".style.display === 'none'")
        check("закрытие: «закрой акцию дня» — попап закрыт"
              f"{'' if ok1c else ' — ' + str(d1c)}",
              ok1c and str(promo_gone).lower() == "true")

        # 1.6. Регрессия: контентная модалка (role=dialog карточки товара,
        # крестик с aria-label «Закрыть») НЕ снимается авто-дисмиссом —
        # иначе «нажми добавку» на странице товара закрывало саму страницу
        ba.eval_js(None, tab_id,
                   "document.getElementById('prod').style.display='block'")
        ok1d, d1d, _ = run("Моцарелла")
        prod_alive = ba.eval_js(None, tab_id,
                                "getComputedStyle(document.getElementById"
                                "('prod')).display !== 'none'")
        check("оверлей: контентная модалка пережила авто-дисмисс",
              str(prod_alive).lower() == "true")
        check(f"оверлей: клик внутри контентной модалки («Моцарелла»)"
              f"{'' if ok1d else ' — ' + str(d1d)}", ok1d)
        ba.eval_js(None, tab_id,
                   "document.getElementById('prod').style.display='none'")

        # 1.7. FAQ-аккордеон label+checkbox (dodo): заголовок глушит клики —
        # основной клик не меняет DOM, контрол перещёлкивает фолбэк; цель с
        # опечаткой «кэшбек» (на странице «кешбэк») — fuzzy-ярус скоринга
        ok1e, d1e, _ = run("что такое кэшбек")
        acc = ba.eval_js(None, tab_id,
                         "document.querySelector('.faq input').checked")
        check(f"аккордеон: «что такое кэшбек» (опечатка) — открыт"
              f"{'' if ok1e else ' — ' + str(d1e)}",
              ok1e and str(acc).lower() == "true")
        ok1f, d1f, _ = run("что такое кешбэк")
        acc2 = ba.eval_js(None, tab_id,
                          "document.querySelector('.faq input').checked")
        check(f"аккордеон: повторный клик не закрывает (уже открыт)"
              f"{'' if ok1f else ' — ' + str(d1f)}",
              ok1f and str(acc2).lower() == "true")

        # 1.8. Поле без placeholder — плавающая подпись-сосед («Имя» в span)
        act_t, err_t = mgr.resolve_type("Иван в поле имя", None, router,
                                        chat_id="smoke")
        ok_t = False
        if act_t is not None:
            ok_t, _ = mgr.execute(act_t, "smoke", router=router)
        fname = ba.eval_js(None, tab_id,
                           "document.getElementById('fname').value")
        check(f"ввод: плавающая подпись — поле «Имя» заполнено"
              f"{'' if ok_t else ' — ' + str(err_t)}",
              ok_t and fname == "Иван")

        # 2. Элемент внутри iframe
        ok2, d2, act2 = run("Из фрейма")
        fr = ba.eval_js(None, tab_id,
                        "document.querySelector('iframe')"
                        ".getBoundingClientRect().width > 0")
        check("iframe: кнопка «Из фрейма» нажата"
              f" ({d2 if not ok2 else 'closed-loop ok'})",
              ok2 and str(fr).lower() == "true")

        # 3. Элемент внутри open shadow root
        ok3, d3, _ = run("В тени")
        shadow_hit = ba.eval_js(
            None, tab_id,
            "document.getElementById('host').shadowRoot.innerHTML"
            ".indexOf('нажато-тень') >= 0")
        check("shadow DOM: кнопка «В тени» нажата",
              ok3 and str(shadow_hit).lower() == "true")

        # 4. Элемент, появляющийся только после прокрутки (доскролл-поиск)
        ok4, d4, _ = run("Поздний элемент")
        check("доскролл: «Поздний элемент» найден и нажат", ok4)

        # 5. detect_antibot на нормальной странице — не срабатывает
        check("antibot: нормальная страница — без ложного срабатывания",
              ba.detect_antibot(None, tab_id) is None)

        # 6. Vision-фолбэк: иконка корзины (SVG) без accessible name.
        # Отдельный менеджер с выключенным wide-резолвом: иначе текстовая
        # LLM может угадать безымянную кнопку раньше скриншота — связку
        # wide→vision покрывают юнит-тесты, здесь проверяем сам vision
        if router is not None and has_vision:
            mgr_vis = ComputerControlManager(
                context="smoke",
                config={"click": True, "vision_fallback": True,
                        "llm_wide_resolve": False},
                base_dir=tmp)
            mgr_vis._last_tab_id = tab_id
            act6, err6 = mgr_vis.resolve_click("корзина", None, router,
                                               chat_id="smoke")
            if act6 is None:
                # Vision-вызов — один сетевой запрос к внешнему провайдеру,
                # флапает; один повтор честнее, чем красный смоук на пустом месте
                act6, err6 = mgr_vis.resolve_click("корзина", None, router,
                                                   chat_id="smoke")
            ok6, d6 = False, err6
            if act6 is not None:
                ok6, d6 = mgr_vis.execute(act6, "smoke", router=router)
            check("vision: иконка корзины найдена по скриншоту",
                  ok6 and act6 is not None
                  and act6.get("choose", {}).get("path") == "vision")

        # 7. Широкий LLM-резолв: цель без текстового совпадения с подписями
        # («следующий шаг оформления» → кнопка «Продолжить»)
        if router is not None:
            ok7, d7, act7 = run("следующий шаг оформления")
            check("wide: «Продолжить» выбрано LLM без текстового совпадения"
                  f"{'' if ok7 else ' — ' + str(d7)}",
                  ok7 and act7 is not None
                  and act7.get("choose", {}).get("path") == "llm_wide"
                  and act7.get("element") == "Продолжить")

        # ── Реальные сайты ──
        # Идемпотентность: вкладки прошлых прогонов (смоук за собой не
        # закрывает) делают «перейди на youtube» неоднозначным — чистим
        def _close_host(needle):
            def _op(w):
                closed = 0
                for pg in list(w._all_pages()):
                    if needle in (pg.url or ""):
                        try:
                            pg.close()
                            closed += 1
                        except Exception:
                            pass
                return closed
            return ba._WORKER.submit(_op)
        _close_host("youtube.com")
        _close_host("wikipedia.org")

        tid2 = ba.open_new_tab("https://www.youtube.com/")
        url, host, items = ba.snapshot_elements(None, tab_id=tid2)
        check("youtube.com: снапшот живой страницы (>20 элементов)",
              len(items) > 20)

        tid3 = ba.open_new_tab("https://ru.wikipedia.org/wiki/Заглавная_страница")
        mgr._last_tab_id = tid3  # резолв целится в вкладку википедии
        # Окно автоматизационного браузера узкое (715px): Vector-2022 прячет
        # поле поиска за иконкой. Десктопный вьюпорт — как у пользователя
        ba._WORKER.submit(lambda w: w.page_for(None, tid3)
                          .set_viewport_size({"width": 1400, "height": 900}))
        time.sleep(0.5)
        act_t, err_t = mgr.resolve_type("тест в поле поиск", None, router,
                                        chat_id="smoke")
        if act_t is None:
            act_t, err_t = mgr.resolve_type("тест в поиск", None, router,
                                            chat_id="smoke")
        if act_t is None:
            check(f"wikipedia: ввод в поиск — {err_t}", False)
        else:
            ok_t, d_t = mgr.execute(act_t, "smoke", router=router)
            got = ba.eval_js(None, tid3,
                             "(document.activeElement||{}).value || ''")
            check(f"wikipedia: ввод в поле поиска (closed-loop)"
                  f"{'' if ok_t else ' — ' + str(d_t)}",
                  ok_t and "тест" in got)

        # 8. Переключение вкладок по названию + список открытых
        # (youtube и wikipedia уже открыты выше). Активность вкладки под CDP
        # in-page не проверить: присоединённый отладчик снимает троттлинг —
        # visibilityState/hasFocus у всех вкладок «visible» (проверено
        # вживую). Проверяем резолв, исполнение и перенос отслеживания
        act_s1, err_s1 = mgr.resolve_tab_switch("youtube", True)
        ok_s1 = act_s1 is not None and act_s1.get("kind") == "tab_switch"
        ok_sw = False
        if ok_s1:
            ok_sw, _ = mgr.execute(act_s1, "smoke", router=router)
        check("вкладки: «перейди на youtube» — резолв+активация, "
              "вкладка отслеживается"
              f"{'' if ok_s1 else ' — ' + str(err_s1)}",
              ok_s1 and ok_sw
              and mgr._last_tab_id == act_s1.get("tab_id"))
        tabs_text = mgr.list_open_tabs_text()
        check("вкладки: список открытых вкладок текстом",
              "youtube.com" in tabs_text and "wikipedia.org" in tabs_text)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"  [FAIL] смоук упал целиком: {e}")
        ok_n -= 100
    finally:
        # Браузер бота (выделенный профиль) закрываем за собой — но только
        # если его поднял ЭТОТ прогон; чужой живой браузер не трогаем
        if we_started:
            try:
                ba._WORKER.submit(
                    lambda w: w._kill_browser(ba._resolve_user_data_dir()))
            except Exception:
                pass
        elif tab_id is not None:
            print("  (браузер был запущен не нами — оставлен живым)")
    print(f"\nИтог: {ok_n}")
    return 0 if ok_n > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

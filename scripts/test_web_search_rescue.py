"""Проба капчи поиска Google в rescue пула H (web_search._google_rescue_probe).

Капча поиска ставит карантин «google» (вид challenge, пул H), и rescue пула H
ждёт его снятия. Раньше снять его было некому: поиск в карантине Google
пропускал, open_headless_tab в rescue отказывал, а веб-чата google нет у
многих персон — rescue держался весь срок, окна веб-чатов выскакивали, а
окна с капчей поиска у человека не было. Теперь в rescue поиск держит ОДНО
окно-пробу и только смотрит на него (без навигации):
  а. вне rescue в карантине — None, вкладка не открывается, браузер не трогаем;
  б. rescue + карантин — открыта ровно одна проба (с rescue_probe=True);
     повторный поиск вторую не открывает и не навигирует;
  в. капча в пробе пройдена — карантин снят, проба закрыта,
     _finish_rescue_if_done(cleared="google"); дальше обычный путь (в rescue —
     отказ → None); проба на ЭТОМ же запросе — выдача прямо из неё, без
     второго запроса к Google; rescue окончен — обычная вкладка;
  г. сбой замера (detect_antibot бросил) — «неизвестно»: карантин остаётся,
     проба цела;
  д. проба мертва (Chrome перезапущен; вызов выбросил вкладку) — забыта,
     открыта следующая; проба ушла с Google — закрыта, открыта новая;
  е. open_headless_tab в rescue без флага пробы по-прежнему отказывает,
     с флагом — открывает в пуле H;
  ж. rescue кончился / карантин снят не пробой / карантин не капча — проба
     закрыта, новая не открывается;
  з. два поиска одновременно — одна проба;
  и. адрес пробы — из того же замера (about:blank — «неизвестно»), заголовок
     выдачи с «captcha» при отрисованной выдаче — не капча.

Браузер — заглушки ba.*; настоящий Chrome и Google не трогаются, файлы
рядом с профилем бота — во временной папке.
Запуск: python3 -m scripts.test_web_search_rescue
"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


class FakeBrowser:
    """Пул H на заглушках: реестр фоновых вкладок, состояние Google по
    вкладке («sorry» — капча, «clean» — выдача, «other» — чужой сайт,
    «blank» — навигация ещё не дошла, «title» — выдача, чей заголовок
    ловит detect_antibot)."""

    def __init__(self, ba):
        self.ba = ba
        self.tabs = {}
        self.next_id = 1_000_001
        self.rescue = True
        self.google = "sorry"         # что увидит СВЕЖАЯ вкладка
        self.antibot_raises = False
        self.eval_drops = None         # tab_id: eval выбросит её из реестра
        self.open_delay = 0.0
        self.attempts = []             # (url, rescue_probe) — все попытки
        self.opened = []               # (tab_id, url, rescue_probe) — удачные
        self.closed = []
        self.navigated = []
        self.evals = 0
        self.tab_urls = 0
        self.lock = threading.Lock()

    # ── ba.* ──
    def pool_h_rescue_active(self):
        return self.rescue

    def open_headless_tab(self, url, rescue_probe=False):
        with self.lock:
            self.attempts.append((url, rescue_probe))
        if self.open_delay:
            time.sleep(self.open_delay)
        if self.rescue and not rescue_probe:
            raise self.ba.BrowserUnavailable("браузер бота в видимом режиме (rescue)")
        with self.lock:
            tid = self.next_id
            self.next_id += 1
            self.tabs[tid] = {"url": url, "state": self.google}
            self.opened.append((tid, url, rescue_probe))
        return tid

    def is_raw_tab(self, tab_id):
        return tab_id in self.tabs

    def _href(self, t):
        if t["state"] == "sorry":
            return "https://www.google.com/sorry/index?continue=x&q=y"
        if t["state"] == "other":
            return "https://example.org/page"
        if t["state"] == "blank":
            return "about:blank"
        return t["url"]  # после капчи Google возвращает на исходный адрес

    def eval_js(self, host, tab_id, js, timeout_sec=None):
        self.evals += 1
        if tab_id not in self.tabs:
            raise self.ba.BrowserUnavailable("фоновая вкладка закрыта")
        if tab_id == self.eval_drops:
            self.tabs.pop(tab_id, None)   # как _raw_eval: таргета нет → _raw_drop
            raise self.ba.BrowserUnavailable("Session with given id not found")
        t = self.tabs[tab_id]
        st = t["state"]
        if st == "sorry":
            r = {"sorry": True, "rso": False, "links": []}
        elif st in ("other", "blank"):
            r = {"sorry": False, "rso": False, "links": []}
        else:
            r = {"sorry": False, "rso": True, "links": [
                ["Кафедра", "https://example.edu/staff/ivanov", "сниппет"],
                ["Картинки", "https://www.google.com/imgres?x=1", ""]]}
        # Адрес — только если замер его просит (как настоящий JS пробы):
        # замер обычного поиска адреса не отдаёт
        if "location.href" in js:
            r["href"] = self._href(t)
        return json.dumps(r)

    def detect_antibot(self, host=None, tab_id=None, strict=False):
        if self.antibot_raises:
            raise self.ba.BrowserUnavailable("антибот-проверка не выполнена: таймаут")
        if tab_id not in self.tabs:
            raise self.ba.BrowserUnavailable("фоновая вкладка закрыта")
        st = self.tabs[tab_id]["state"]
        if st == "sorry":
            return "widget: iframe[src*=recaptcha]"
        if st == "title":  # выдача по запросу «captcha …» — заголовок ловится
            return "title: captcha не проходит - Поиск в Google"
        return None

    def tab_url(self, host_part=None, tab_id=None):
        self.tab_urls += 1
        if tab_id not in self.tabs:
            raise self.ba.BrowserUnavailable("фоновая вкладка закрыта")
        return self._href(self.tabs[tab_id])

    def close_background_tab(self, tab_id):
        self.closed.append(tab_id)
        return self.tabs.pop(tab_id, None) is not None

    def navigate_tab(self, url, *a, **kw):
        self.navigated.append(url)

    # ── сценарии ──
    def solve(self, tab_id):
        self.tabs[tab_id]["state"] = "clean"

    def restart_chrome(self):
        self.tabs.clear()  # _forget_raw_pool_tabs: реестр пула сброшен


STUBBED = ("pool_h_rescue_active", "open_headless_tab", "is_raw_tab",
           "eval_js", "detect_antibot", "tab_url", "close_background_tab",
           "navigate_tab")


def main():
    from app.features import browser_actions as ba
    from app.features import web_llm as wl
    from app.features import web_search as ws

    G = ws.GOOGLE_QUARANTINE_SITE
    tmp = tempfile.mkdtemp(prefix="ws_rescue_")
    saved_paths = (ba._pool_h_rescue_path, ba._pool_h_life_path,
                   ba._pool_v_life_path)
    saved_ba = {n: getattr(ba, n) for n in STUBBED}
    real_open = ba.open_headless_tab
    saved_finish = wl._finish_rescue_if_done
    saved_net = ws.internet_available
    finish_calls = []

    def fake_finish(ba_, cleared=None, pool="h"):
        finish_calls.append((cleared, pool))
        return False

    def fresh(**kw):
        fb = FakeBrowser(ba)
        for k, v in kw.items():
            setattr(fb, k, v)
        for n in STUBBED:
            setattr(ba, n, getattr(fb, n))
        ws._GOOGLE_PROBE_TAB = None
        wl.clear_quarantine(G)
        wl.pop_quarantine_alerts()
        finish_calls.clear()
        return fb

    def quarantine(kind="challenge"):
        wl.quarantine_site(G, "капча в поиске Google", kind=kind,
                           ttl=600 if kind != "challenge" else None)

    try:
        # Файлы-соседи профиля (rescue, лок жизненного цикла) — во временной
        # папке: ни один вызов не должен дотянуться до настоящего профиля
        ba._pool_h_rescue_path = lambda: os.path.join(tmp, "h" + ba._RESCUE_SUFFIX)
        ba._pool_h_life_path = lambda: os.path.join(tmp, "h" + ba._LIFE_SUFFIX)
        ba._pool_v_life_path = lambda: os.path.join(tmp, "v" + ba._LIFE_SUFFIX)
        ws.internet_available = lambda: True
        wl._finish_rescue_if_done = fake_finish

        print("\n── а. вне rescue: карантин — сразу None, браузер не трогаем ──")
        fb = fresh(rescue=False)
        quarantine()
        res = ws.google_web_links("иванов кафедра")
        check("None", res is None)
        check("вкладка не открывалась, страница не читалась",
              not fb.attempts and fb.evals == 0 and not fb.closed)
        check("карантин на месте, rescue не трогали",
              wl.site_quarantined(G) and not finish_calls)

        print("\n── б. rescue + карантин: одна проба, без навигации ──")
        fb = fresh()
        quarantine()
        res = ws.google_web_links("иванов кафедра")
        check("капча в пробе — None (поиск уйдёт в DDG)", res is None)
        check("открыта ровно одна проба, с rescue_probe=True",
              len(fb.opened) == 1 and fb.opened[0][2] is True
              and "udm=14" in fb.opened[0][1])
        probe = fb.opened[0][0]
        check("проба запомнена и не закрыта",
              ws._GOOGLE_PROBE_TAB == probe and probe in fb.tabs
              and not fb.closed)
        ev = fb.evals
        res2 = ws.google_web_links("петров лаборатория")
        res3 = ws.google_web_links("сидоров")
        check("повторные поиски — None, вторая проба не открыта",
              res2 is None and res3 is None and len(fb.attempts) == 1)
        check("повторные поиски пробу не навигируют, а только смотрят",
              not fb.navigated and fb.tabs[probe]["url"] == fb.opened[0][1]
              and fb.evals > ev)
        check("карантин держится, rescue не завершали",
              wl.site_quarantined(G) and not finish_calls)

        print("\n── в. капча в пробе пройдена ──")
        fb.solve(probe)
        res = ws.google_web_links("петров лаборатория")
        check("карантин снят", not wl.site_quarantined(G))
        check("проба закрыта и забыта",
              probe in fb.closed and ws._GOOGLE_PROBE_TAB is None)
        check("_finish_rescue_if_done(cleared='google')",
              finish_calls == [("google", "h")])
        check("другой запрос, rescue идёт — обычный путь: вкладка без флага, "
              "отказ → None (DDG)",
              res is None and len(fb.attempts) == 2
              and fb.attempts[1][1] is False and len(fb.opened) == 1)
        res = ws.google_web_links("петров лаборатория")
        check("после снятия проба больше не открывается",
              res is None and len(fb.opened) == 1)

        # проба стоит на ЭТОМ же запросе — выдача из неё, второго запроса нет
        fb = fresh()
        quarantine()
        ws.google_web_links("иванов кафедра")
        probe = fb.opened[0][0]
        fb.solve(probe)
        res = ws.google_web_links("иванов кафедра")
        check("тот же запрос — выдача прямо из пробы (служебные ссылки "
              "Google отброшены)",
              res == [{"href": "https://example.edu/staff/ivanov",
                       "title": "Кафедра", "body": "сниппет"}])
        check("  и ни одной новой вкладки (второго запроса к Google нет)",
              len(fb.attempts) == 1 and probe in fb.closed
              and not wl.site_quarantined(G)
              and finish_calls == [("google", "h")])

        # rescue завершился на снятии — обычная вкладка, обычная выдача
        fb = fresh()
        quarantine()
        ws.google_web_links("иванов кафедра")
        probe = fb.opened[0][0]
        fb.solve(probe)

        def finish_ends(ba_, cleared=None, pool="h"):
            finish_calls.append((cleared, pool))
            fb.rescue = False
            return True
        wl._finish_rescue_if_done = finish_ends
        fb.google = "clean"
        res = ws.google_web_links("петров лаборатория")
        wl._finish_rescue_if_done = fake_finish
        check("rescue окончен снятием — поиск по обычной вкладке",
              res and res[0]["href"] == "https://example.edu/staff/ivanov"
              and len(fb.opened) == 2 and fb.opened[1][2] is False
              and fb.opened[1][0] in fb.closed)

        # чистая СВЕЖАЯ проба (Google сам снял блок) — сразу выдача
        fb = fresh(google="clean")
        quarantine()
        res = ws.google_web_links("иванов кафедра")
        check("свежая проба чиста — карантин снят, выдача из неё, окно "
              "закрыто, одна вкладка",
              res and len(fb.attempts) == 1 and not wl.site_quarantined(G)
              and fb.opened[0][0] in fb.closed
              and finish_calls == [("google", "h")])

        print("\n── г. сбой замера — «неизвестно», карантин не снят ──")
        fb = fresh()
        quarantine()
        ws.google_web_links("иванов кафедра")
        probe = fb.opened[0][0]
        fb.solve(probe)
        fb.antibot_raises = True
        res = ws.google_web_links("иванов кафедра")
        check("None", res is None)
        check("карантин остался, rescue не завершали",
              wl.site_quarantined(G) and not finish_calls)
        check("проба цела, новая не открыта",
              ws._GOOGLE_PROBE_TAB == probe and probe in fb.tabs
              and not fb.closed and len(fb.attempts) == 1)
        fb.antibot_raises = False
        res = ws.google_web_links("иванов кафедра")
        check("замер снова работает — капча пройдена, карантин снят",
              res and not wl.site_quarantined(G))

        print("\n── д. проба мертва — забыта, открывается следующая ──")
        fb = fresh()
        quarantine()
        ws.google_web_links("иванов кафедра")
        probe = fb.opened[0][0]
        fb.restart_chrome()  # «почини браузер» ещё раз / конец rescue
        ev = fb.evals
        res = ws.google_web_links("петров")
        check("Chrome перезапущен: мёртвую пробу не читаем (вызовы в чужой "
              "транспорт не уходят)", fb.evals == ev + 1)
        check("старая забыта, открыта новая (одна)",
              res is None and len(fb.opened) == 2
              and ws._GOOGLE_PROBE_TAB == fb.opened[1][0] != probe)
        check("карантин держится", wl.site_quarantined(G) and not finish_calls)

        # вызов на вкладке упал и выбросил её из реестра (таргета нет)
        probe = ws._GOOGLE_PROBE_TAB
        fb.eval_drops = probe
        res = ws.google_web_links("сидоров")
        check("вызов на пробе упал (вкладки нет) — забыта, открыта новая",
              res is None and len(fb.opened) == 3
              and ws._GOOGLE_PROBE_TAB == fb.opened[2][0] != probe
              and wl.site_quarantined(G))

        # человек увёл окно пробы на другой сайт — капчу поиска там не решить
        probe = ws._GOOGLE_PROBE_TAB
        fb.tabs[probe]["state"] = "other"
        res = ws.google_web_links("сидоров")
        check("проба ушла с Google — закрыта, открыта новая, карантин не "
              "снят по чужой чистой странице",
              res is None and probe in fb.closed and len(fb.opened) == 4
              and ws._GOOGLE_PROBE_TAB == fb.opened[3][0]
              and wl.site_quarantined(G) and not finish_calls)

        # свежая проба сразу не на Google — без повтора в том же вызове
        fb = fresh(google="other")
        quarantine()
        res = ws.google_web_links("сидоров")
        check("свежая проба не на Google — закрыта, второй попытки в том же "
              "вызове нет", res is None and len(fb.attempts) == 1
              and ws._GOOGLE_PROBE_TAB is None and wl.site_quarantined(G))

        print("\n── и. замер пробы: адрес и заголовок ──")
        # свежая проба прочитана ещё на about:blank — «неизвестно», не «чисто»
        fb = fresh(google="blank")
        quarantine()
        res = ws.google_web_links("иванов")
        check("проба на about:blank — карантин не снят, проба цела",
              res is None and wl.site_quarantined(G)
              and ws._GOOGLE_PROBE_TAB == fb.opened[0][0]
              and not fb.closed and not finish_calls)
        check("адрес — из того же замера, отдельного tab_url нет",
              fb.tab_urls == 0)
        fb.tabs[ws._GOOGLE_PROBE_TAB]["state"] = "clean"
        res = ws.google_web_links("иванов")
        check("навигация дошла, капчи нет — карантин снят, выдача из пробы",
              res and not wl.site_quarantined(G) and len(fb.attempts) == 1)

        # выдача по запросу «captcha …»: detect_antibot ловит заголовок
        fb = fresh()
        quarantine()
        ws.google_web_links("captcha не проходит")
        probe = fb.opened[0][0]
        fb.tabs[probe]["state"] = "title"
        res = ws.google_web_links("captcha не проходит")
        check("заголовок выдачи с «captcha» при #rso — не капча: карантин снят",
              res and not wl.site_quarantined(G) and probe in fb.closed
              and finish_calls == [("google", "h")])

        print("\n── ж. rescue кончился / карантин не тот — пробу не держим ──")
        fb = fresh()
        quarantine()
        ws.google_web_links("иванов")
        probe = fb.opened[0][0]
        fb.rescue = False
        res = ws.google_web_links("иванов")
        check("rescue окончен: проба закрыта, новая не открыта, None",
              res is None and probe in fb.closed
              and ws._GOOGLE_PROBE_TAB is None and len(fb.attempts) == 1
              and wl.site_quarantined(G))

        fb = fresh()
        quarantine()
        ws.google_web_links("иванов")
        probe = fb.opened[0][0]
        wl.clear_quarantine(G)  # снял веб-чат google (_challenge_check)
        fb.google = "clean"
        res = ws.google_web_links("иванов")
        check("карантин снят не пробой: проба закрыта, дальше обычный путь "
              "(в rescue — отказ)",
              res is None and probe in fb.closed
              and ws._GOOGLE_PROBE_TAB is None
              and fb.attempts[-1][1] is False and len(fb.opened) == 1)

        fb = fresh()
        quarantine(kind="ratelimit")
        res = ws.google_web_links("иванов")
        check("карантин-лимит в rescue: пробы нет (руками не снять)",
              res is None and not fb.attempts and wl.site_quarantined(G))

        print("\n── з. два поиска одновременно — одна проба ──")
        fb = fresh(open_delay=0.3)
        quarantine()
        out = []
        ths = [threading.Thread(
            target=lambda q=q: out.append(ws.google_web_links(q)))
            for q in ("иванов", "петров", "сидоров")]
        for t in ths:
            t.start()
        for t in ths:
            t.join(5)
        check("три потока — открыта одна проба, все None",
              len(fb.attempts) == 1 and len(out) == 3
              and all(r is None for r in out))

        print("\n── е. open_headless_tab в rescue ──")
        for n in STUBBED:
            setattr(ba, n, saved_ba[n])
        opened = []
        saved_raw = (ba._raw_open, ba._pool_h_alive, ba.pool_h_rescue_active)
        ba._raw_open = lambda url, pool=None: (opened.append((url, pool)), 7)[1]
        ba._pool_h_alive = lambda: True
        ba.pool_h_rescue_active = lambda: True
        try:
            try:
                real_open("https://www.google.com/search?q=x")
                refused = False
            except ba.BrowserUnavailable:
                refused = True
            check("без флага пробы — отказ, окно не открыто",
                  refused and not opened)
            tid = real_open("https://www.google.com/search?q=x",
                            rescue_probe=True)
            check("с rescue_probe=True — вкладка в пуле H",
                  tid == 7 and opened == [("https://www.google.com/search?q=x",
                                           ba._POOL_H)])
            ba.pool_h_rescue_active = lambda: False
            real_open("https://www.google.com/search?q=y")
            check("вне rescue — открывается как раньше", len(opened) == 2)
        finally:
            ba._raw_open, ba._pool_h_alive, ba.pool_h_rescue_active = saved_raw
    finally:
        # Карантин снимаем ДО возврата путей: снятие публикует ожидание
        # rescue (web_llm._publish_rescue_wait) — по временному пути, а не
        # рядом с настоящим профилем
        ws._GOOGLE_PROBE_TAB = None
        wl.clear_quarantine(G)
        wl.pop_quarantine_alerts()
        for n, v in saved_ba.items():
            setattr(ba, n, v)
        (ba._pool_h_rescue_path, ba._pool_h_life_path,
         ba._pool_v_life_path) = saved_paths
        wl._finish_rescue_if_done = saved_finish
        ws.internet_available = saved_net
        leftovers = os.listdir(tmp)
        for f in leftovers:
            try:
                os.unlink(os.path.join(tmp, f))
            except OSError:
                pass
        os.rmdir(tmp)

    print(f"\nИтого: {ok - failures}/{ok} OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

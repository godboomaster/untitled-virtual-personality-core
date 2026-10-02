"""Smoke-тест браузерного слоя (browser_actions).

Живого браузера здесь нет: страницы, процессы, osascript и raw-CDP —
моки/фейки (как в разделе «Фоновые вкладки (raw CDP)» test_computer_control).
Один блок проверок на тему:

  1. «текущая пользовательская вкладка» — одно понятие (scan_search, tab_op);
  2. сбой запуска браузера добивает осиротевший процесс (пулы V и H);
  3. AppleScript: имя приложения и адресация по pid — одной функцией;
  4. Preferences профиля и платформенная проверка живости процесса;
  5. служебный хост — одно определение (list_tabs_detailed/list_pages);
  6. учёт страниц воркера: один реестр, чистка, история URL без id-реюза;
  7. полный захват не затирает инлайновый display; дедуп текстового правила;
  8. слайдер: один компаратор и гарантированное снятие метки;
  9. явные бюджеты опроса шлюза, мёртвый код убран;
 10. брошенное поколение воркера освобождает своё CDP-соединение;
 11. scripts/chrome_debug.sh берёт флаги из кода, а не копирует их;
 12. «активный бэкенд не умеет» — тип исключения, а не подстрока текста;
 13. бюджеты снапшотов в JS подставляются из SNAPSHOT_MAX/GOAL_SNAPSHOT_MAX;
 14. стем слова (__vpcStem/__vpcWIn) — одно определение на все шаблоны;
 15. press_key: один потолок серии (PRESS_TIMES_MAX) и добивание остатка;
 16. звук направленно (mute/unmute), отчёт по факту, а не «громкость N%»;
 17. AppleScript при нескольких экземплярах браузера: сверка URL вкладок;
 18. Windows: профиль и завершение процесса;
 19. реестр страниц воркера — по поколениям;
 20. антибот: валидные CSS-селекторы, причина ошибки JS в сообщении.

Запуск: PYTHONPATH=. python3 scripts/test_browser_actions.py
"""

import gc
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    import app.features.browser_actions as ba

    SRC = io.open(ba.__file__, encoding="utf-8").read()

    class _Pg:
        # Минимальная страница playwright: url + evaluate + закрытость.

        def __init__(self, url, res=None, title=""):
            self.url = url
            self._res = res
            self._title = title
            self.closed = False
            self.evals = []

        def evaluate(self, js, *a):
            self.evals.append(js)
            if callable(self._res):
                return self._res(js)
            if isinstance(self._res, dict):
                return self._res.get(js, "")
            return self._res or ""

        def is_closed(self):
            return self.closed

        def title(self):
            return self._title

    class _FakeCtx:
        def __init__(self, pages):
            self.pages = pages

    class _FakeBr:
        def __init__(self, pages):
            self.contexts = [_FakeCtx(pages)]

        def is_connected(self):
            return True

    def _it(idx, tag, text="", **kw):
        d = {"idx": idx, "tag": tag, "text": text, "x": 0.0, "y": 0.0,
             "w": 100.0, "h": 30.0}
        d.update(kw)
        return d

    print("\n── 1. Текущая пользовательская вкладка (один источник) ──")

    _orig_fw = ba._front_window_url
    try:
        wk = ba._CdpWorker()
        wk.ensure_browser = lambda allow_launch=False: None
        p_bg = _Pg("https://www.youtube.com/results?q=a", res="ok:background")
        p_vis = _Pg("https://www.youtube.com/results?q=b", res="ok:visible")
        wk._all_pages = lambda: [p_bg, p_vis]
        ba._front_window_url = lambda: "https://www.youtube.com/results?q=b"
        order = wk._scan_order()
        check("scan_search: обход начинается с видимой вкладки, без дублей",
              order and order[0] is p_vis and len(order) == 2
              and order[1] is p_bg)
        check("scan_search: рецепт выполняется в видимой вкладке, "
              "а не в первой из списка",
              wk.eval_js(None, "JS", scan_search=True) == "ok:visible")
        # Источник видимой молчит — фолбэк на первую подходящую вкладку
        ba._front_window_url = lambda: ""
        wk2 = ba._CdpWorker()
        wk2.ensure_browser = lambda allow_launch=False: None
        wk2._all_pages = lambda: [p_bg, p_vis]
        wk2.page_for = lambda host, tab_id=None: p_bg
        check("scan_search: источник видимой молчит — прежний порядок",
              wk2.eval_js(None, "JS", scan_search=True) == "ok:background")
        # current_user_page — то же понятие для команд без явной цели
        wk3 = ba._CdpWorker()
        wk3.ensure_browser = lambda allow_launch=False: None
        wk3._all_pages = lambda: [p_bg, p_vis]
        ba._front_window_url = lambda: "https://www.youtube.com/results?q=b"
        check("current_user_page: видимая вкладка важнее CDP-эвристики",
              wk3.current_user_page() is p_vis)
        ba._front_window_url = lambda: ""
        wk3.page_for = lambda host, tab_id=None: p_bg
        check("current_user_page: фолбэк на page_for(None, None)",
              wk3.current_user_page() is p_bg)
        check("tab_op без цели: reload/close/history идут в current_user_page",
              SRC.count("pg = w.current_user_page() if tab_id is None") == 3
              and "w.page_for_user_visible() or w.page_for(None, None)" not in SRC)
    finally:
        ba._front_window_url = _orig_fw

    print("\n── 2. Сбой запуска браузера: осиротевший процесс добивается ──")

    class _Proc:
        def __init__(self, alive=True):
            self.pid = 424242
            self._alive = alive

        def poll(self):
            return None if self._alive else 1

    class _FakeSub:
        # Подмена модуля subprocess для путей запуска (Popen/run/DEVNULL).
        DEVNULL = -3
        TimeoutExpired = subprocess.TimeoutExpired

        def __init__(self, proc):
            self.proc = proc
            self.cmds = []

        def Popen(self, cmd, **kw):
            self.cmds.append(cmd)
            return self.proc

        def run(self, *a, **kw):
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()

    tmp = Path(tempfile.mkdtemp(prefix="ba_smoke_"))
    kills = []
    _orig = dict(sub=ba.subprocess, kill=ba._kill_chrome_on_profile,
                 lock=ba._check_profile_lock, copy=ba._ensure_v_profile_copy,
                 ms=ba._enable_memory_saver, zoom=ba._wipe_saved_zoom_levels,
                 tmo=ba.BROWSER_LAUNCH_TIMEOUT_SEC, hide=ba._hide_pool_window,
                 halive=ba._pool_h_alive, hmask=ba._headless_mask_flags,
                 life=ba._pool_h_life_path)
    try:
        ba._kill_chrome_on_profile = lambda proc, udd, grace_sec=10.0: (
            kills.append((getattr(proc, "pid", None), udd, grace_sec)), True)[1]
        ba._check_profile_lock = lambda udd: None
        ba._ensure_v_profile_copy = lambda: None
        ba._enable_memory_saver = lambda udd: None
        ba._wipe_saved_zoom_levels = lambda udd: None
        ba._hide_pool_window = lambda pid=None: None
        ba.BROWSER_LAUNCH_TIMEOUT_SEC = 0.4
        ba.set_browser_config({"executable": "/bin/echo",
                               "visible_user_data_dir": str(tmp / "v"),
                               "user_data_dir": str(tmp / "h")})
        # (а) отладочный порт так и не поднялся
        proc = _Proc()
        ba.subprocess = _FakeSub(proc)
        wkl = ba._CdpWorker()
        wkl._connect = lambda timeout_ms=0: (_ for _ in ()).throw(
            ba.BrowserUnavailable("порт закрыт"))
        raised = ""
        try:
            wkl._launch_chrome()
        except ba.BrowserUnavailable as e:
            raised = str(e)
        check("запуск пула V: таймаут порта — процесс добит, ссылка очищена",
              "не дождался" in raised and kills
              and kills[-1][0] == proc.pid and kills[-1][2] <= 3.0
              and wkl._proc is None)
        # (б) любой другой сбой на пути запуска — та же уборка
        kills.clear()
        proc2 = _Proc()
        ba.subprocess = _FakeSub(proc2)
        wkl2 = ba._CdpWorker()
        wkl2._connect = lambda timeout_ms=0: (_ for _ in ()).throw(
            RuntimeError("странный сбой playwright"))
        boom = False
        try:
            wkl2._launch_chrome()
        except RuntimeError:
            boom = True
        check("запуск пула V: любое исключение — тоже добиваем (общий cleanup)",
              boom and kills and kills[-1][0] == proc2.pid
              and wkl2._proc is None)
        # (в) процесс умер сам — убивать нечего, ссылка всё равно очищена
        kills.clear()
        dead = _Proc(alive=False)
        ba.subprocess = _FakeSub(dead)
        wkl3 = ba._CdpWorker()
        wkl3._connect = lambda timeout_ms=0: (_ for _ in ()).throw(
            ba.BrowserUnavailable("порт закрыт"))
        try:
            wkl3._launch_chrome()
        except ba.BrowserUnavailable:
            pass
        check("запуск пула V: мёртвый процесс не «добивается» зря",
              not kills and wkl3._proc is None)
        # (в2) основной профиль пользователя: гасим только СВОЙ процесс,
        # чужой Chrome на этом профиле не трогаем никогда
        kills.clear()
        _oid = ba._is_default_browser_profile

        class _TProc(_Proc):
            def __init__(self):
                super().__init__()
                self.terminated = False

            def terminate(self):
                self.terminated = True

        try:
            ba._is_default_browser_profile = lambda path: True
            tproc = _TProc()
            ba.subprocess = _FakeSub(tproc)
            wkl4 = ba._CdpWorker()
            wkl4._connect = lambda timeout_ms=0: (_ for _ in ()).throw(
                ba.BrowserUnavailable("порт закрыт"))
            try:
                wkl4._launch_chrome()
            except ba.BrowserUnavailable:
                pass
            check("сбой запуска на основном профиле: гасим только свой процесс",
                  tproc.terminated and not kills)
        finally:
            ba._is_default_browser_profile = _oid

        # (г) пул H — тот же общий cleanup
        kills.clear()
        proc4 = _Proc()
        ba.subprocess = _FakeSub(proc4)
        ba._pool_h_alive = lambda: False
        ba._headless_mask_flags = lambda exe: ["--headless=new"]
        ba._POOL_H_PROC = None
        # Не реальный лок-файл профиля: иначе тест делит его с живым ботом
        _life = tempfile.NamedTemporaryFile(suffix=".bot-lifecycle.lock",
                                            delete=False).name
        ba._pool_h_life_path = lambda: _life
        h_raised = ""
        try:
            ba._launch_pool_h_chrome()
        except ba.BrowserUnavailable as e:
            h_raised = str(e)
        check("запуск пула H: таймаут порта — процесс добит, ссылка очищена",
              "не поднял порт" in h_raised and kills
              and kills[-1][0] == proc4.pid and ba._POOL_H_PROC is None)
    finally:
        ba.subprocess = _orig["sub"]
        ba._kill_chrome_on_profile = _orig["kill"]
        ba._check_profile_lock = _orig["lock"]
        ba._ensure_v_profile_copy = _orig["copy"]
        ba._enable_memory_saver = _orig["ms"]
        ba._wipe_saved_zoom_levels = _orig["zoom"]
        ba.BROWSER_LAUNCH_TIMEOUT_SEC = _orig["tmo"]
        ba._pool_h_life_path = _orig["life"]
        ba._hide_pool_window = _orig["hide"]
        ba._pool_h_alive = _orig["halive"]
        ba._headless_mask_flags = _orig["hmask"]
        ba.set_browser_config({})

    print("\n── 3. AppleScript: единая адресация приложения ──")

    try:
        ba.set_browser_config({"channel": "brave"})
        check("_as_app_name: имя приложения из канала конфига",
              ba._as_app_name() == "Brave Browser")
        ba.set_browser_config(
            {"executable": "/Applications/Yandex.app/Contents/MacOS/Yandex"})
        check("_as_app_name: имя приложения из пути бинаря (.app)",
              ba._as_app_name() == "Yandex")
        ba.set_browser_config({})
        check("_as_app_name: дефолт — Chrome",
              ba._as_app_name() == "Google Chrome")
        tell = ba._as_tell("  return 1\n", app='Brow"ser\\x')
        check("_as_tell: экранирование имени идёт через _as_lit",
              'tell application "Brow\\"ser\\\\x"' in tell)
        check("_as_tell: guard не даёт Apple Events ЗАПУСТИТЬ браузер",
              tell.startswith('if application "Brow\\"ser\\\\x" is not running '
                              'then return "__no_app__"')
              and tell.rstrip().endswith("end tell"))
        check("_as_tell: guard=False — запуск разрешён (команда «открой сайт»)",
              not ba._as_tell("  x\n", guard=False).startswith("if application"))
        check("_as_proc_ref: адресация процесса по pid (System Events)",
              ba._as_proc_ref(77) == "(first process whose unix id is 77)")
        check("_as_tell_to: однострочная форма",
              ba._as_tell_to("activate", app="Safari")
              == 'tell application "Safari" to activate\n')
        # Ни одного зашитого «Google Chrome» в скриптах — только через хелперы
        check("в модуле нет зашитого tell application \"Google Chrome\"",
              'tell application "Google Chrome"' not in SRC
              and SRC.count('tell application "{name}"') == 1
              and SRC.count('to {cmd}') == 1)
        check("«unix id is» собирается только в _as_proc_ref",
              SRC.count("unix id is") == 1
              and SRC.count("first process whose unix id is {int(pid)}") == 1)

        grabbed = []
        _o = dict(osa=ba._osascript, single=ba._as_single_target,
                  run=ba._as_run)
        try:
            ba.set_browser_config({"channel": "brave"})
            ba._as_single_target = lambda app=None: True
            ba._osascript = lambda script, browser="chrome": (
                grabbed.append(script), "out")[1]
            ba._run_apple_events("youtube.com", "1+1")
            ba._front_window_url()
            try:
                ba._open_tab_applescript("https://a.ru/x")
            except ba.BrowserUnavailable:
                pass  # мок отдаёт не число — важен только сам скрипт
            check("мосты адресуют браузер из конфига, а не Chrome",
                  len(grabbed) == 3
                  and all('tell application "Brave Browser"' in g
                          for g in grabbed)
                  and all("Google Chrome" not in g for g in grabbed))
            check("мост «открой сайт» браузер запустить может, "
                  "а мосты чтения — нет",
                  grabbed[0].startswith("if application")
                  and grabbed[1].startswith("if application")
                  and not grabbed[2].startswith("if application"))
            # Экземпляров несколько и ответ не из вкладок пула — мосты
            # молчат/отказывают (подробно — в секции 17)
            ba._as_single_target = lambda app=None: False
            _ofi = ba._as_foreign_instance
            ba._as_foreign_instance = lambda url: True
            check("несколько экземпляров браузера: переднее окно не читаем",
                  ba._front_window_url() == "")
            ba._as_foreign_instance = _ofi
            amb = ""
            try:
                ba._run_apple_events("youtube.com", "1+1")
            except ba.BrowserUnavailable as e:
                amb = str(e)
            check("несколько экземпляров браузера: JS во вкладке — отказ",
                  "несколько экземпляров" in amb)
            check("несколько экземпляров браузера: тихий выбор вкладки — нет",
                  ba._select_browser_tab_quietly("https://a.ru/x") is False
                  and ba._focus_browser_tab("https://a.ru/x") is False
                  and ba._find_tab_applescript("a.ru") is None)
        finally:
            ba._osascript, ba._as_single_target = _o["osa"], _o["single"]
            ba._as_run = _o["run"]
            ba.set_browser_config({})
        # Сентинел «приложение не запущено» — общий разбор
        _o2 = ba._osascript
        try:
            ba._osascript = lambda script, browser="chrome": ba.AS_NO_APP
            no_app = ""
            try:
                ba._as_run("x", app="Safari")
            except ba.BrowserUnavailable as e:
                no_app = str(e)
            check("_as_run: сентинел «не запущено» → BrowserUnavailable",
                  "Safari не запущен" in no_app)
        finally:
            ba._osascript = _o2
        check("_as_browser_pids: незапущенное приложение — пустой список",
              ba._as_browser_pids("VpcNoSuchApp") in ([], None)
              and ba._as_single_target("VpcNoSuchApp") is True)
    finally:
        ba.set_browser_config({})

    print("\n── 4. Профиль браузера: Preferences и живость процесса ──")

    check("_pid_alive: свой процесс жив, мусорные pid — нет",
          ba._pid_alive(os.getpid()) is True
          and ba._pid_alive(0) is False and ba._pid_alive(-7) is False
          and ba._pid_alive("nope") is False
          and ba._pid_alive(4_000_000) is False)
    if sys.platform != "win32":
        # Зомби (01.10): завершённый Chrome пула H, которого родитель ещё не
        # забрал, держал SingletonLock «живым» — профиль «занят» навсегда
        import subprocess as _sp
        _z = _sp.Popen(["sleep", "0.05"])
        time.sleep(0.4)
        zombie_dead = ba._pid_alive(_z.pid) is False
        _z.wait()
        check("_pid_alive: зомби (завершён, не забран родителем) — не жив",
              zombie_dead)
    check("_pid_alive: os.kill(pid, 0) больше нигде не зовётся "
          "(на Windows это terminate)",
          # один вызов в самом _pid_alive + упоминание в его докстринге
          SRC.count("os.kill(pid, 0)") == 2
          and "ctypes" in SRC and "WaitForSingleObject" in SRC)

    prof = tmp / "prefs-profile"
    (prof / "Default").mkdir(parents=True, exist_ok=True)
    pp = prof / "Default" / "Preferences"
    check("_load_prefs: файла нет — пустые настройки (не отказ)",
          ba._load_prefs(str(prof)) == {})
    pp.write_text("{битый json", encoding="utf-8")
    check("_load_prefs: нечитаемый файл — None (правку отменяем)",
          ba._load_prefs(str(prof)) is None)
    ba._enable_memory_saver(str(prof))
    check("memory saver: нечитаемые Preferences НЕ перезаписываются пустыми",
          pp.read_text(encoding="utf-8") == "{битый json")
    pp.write_text(json.dumps({"profile": {"name": "мой"},
                              "partition": {"per_host_zoom_levels":
                                            {"x": {"dodopizza.ru": 1.5}}}}),
                  encoding="utf-8")
    ba._enable_memory_saver(str(prof))
    got = json.loads(pp.read_text(encoding="utf-8"))
    check("memory saver: включён, чужие ключи профиля сохранены",
          got.get("high_efficiency_mode", {}).get("enabled") is True
          and got.get("profile", {}).get("name") == "мой")
    ba._wipe_saved_zoom_levels(str(prof))
    got2 = json.loads(pp.read_text(encoding="utf-8"))
    check("сброс зумов: карта очищена, остальное на месте",
          got2["partition"]["per_host_zoom_levels"]["x"] == {}
          and got2.get("profile", {}).get("name") == "мой")
    if sys.platform != "win32":
        lock = prof / "SingletonLock"
        if lock.is_symlink() or lock.exists():
            lock.unlink()
        os.symlink(f"host-{os.getpid()}", lock)
        check("Preferences живого профиля не правим (лок держит живой Chrome)",
              ba._prefs_editable(str(prof)) is False)
        lock.unlink()
        os.symlink("host-4000000", lock)
        check("протухший лок правку не блокирует",
              ba._prefs_editable(str(prof)) is True)
        lock.unlink()
    check("на Windows закрытость профиля решает отдельная проверка "
          "(подробно — в секции 18)",
          'if sys.platform == "win32":\n        return _win_profile_closed(udd)'
          in SRC and "msvcrt" in SRC and "lockfile" in SRC)

    print("\n── 5. Служебный хост — одно определение ──")

    _o3 = ba._WORKER.submit
    try:
        ba.register_service_host("svc.example")
        wks = ba._CdpWorker()
        wks.ensure_browser = lambda allow_launch=False: None
        p_svc = _Pg("https://svc.example/chat", title="Служебка")
        p_ds = _Pg("https://chat.deepseek.com/a", title="DeepSeek")
        p_chat = _Pg("http://localhost:5173/", title="Чат бота")
        p_user = _Pg("https://www.youtube.com/", title="YouTube")
        wks._all_pages = lambda: [p_svc, p_ds, p_chat, p_user]
        tabs = wks.list_tabs_detailed()
        check("list_tabs_detailed: служебные (реестр + адаптеры web_llm) "
              "и чат бота отфильтрованы одним правилом",
              [t[3] for t in tabs] == ["YouTube"]
              and ba.is_service_host("chat.deepseek.com"))
        check("list_tabs_detailed: id вкладки стабилен между вызовами",
              wks.list_tabs_detailed()[0][0] == tabs[0][0])
        ba._WORKER.submit = lambda fn, timeout=None: fn(wks)
        _osel = ba._select_backend
        try:
            ba._select_backend = lambda *a, **kw: "cdp"
            check("list_pages: тот же фильтр служебных/чатовых URL",
                  [u for u, _h in ba.list_pages()]
                  == ["https://www.youtube.com/"])
        finally:
            ba._select_backend = _osel
        check("в файле не осталось своих копий фильтра _SERVICE_HOSTS",
              "in _SERVICE_HOSTS" not in SRC.split("def register_service_host")[0]
              )
    finally:
        ba._WORKER.submit = _o3
        ba._SERVICE_HOSTS.discard("svc.example")

    print("\n── 6. Учёт страниц воркера: один реестр ──")

    wkp = ba._CdpWorker()
    ens = []
    p_a, p_b = _Pg("https://a.ru/"), _Pg("https://b.ru/")

    def _ens(allow_launch=False):
        ens.append(allow_launch)
        wkp._browser = _FakeBr([p_a, p_b])

    wkp.ensure_browser = _ens
    pages = wkp._all_pages()
    check("_all_pages: соединение поднимается тут же (одна точка учёта)",
          ens == [False] and pages == [p_a, p_b])
    tid_a = ba._register_page(wkp, p_a)
    check("register_page: повторная регистрация отдаёт тот же id",
          ba._register_page(wkp, p_a) == tid_a
          and ba._register_page(wkp, p_b) != tid_a)
    p_a.closed = True

    class _BrokenPg(_Pg):
        def is_closed(self):
            raise RuntimeError("страница брошенного поколения")

    broken = _BrokenPg("https://c.ru/")
    wkp._pages[999] = broken
    wkp._purge_pages()
    check("_purge_pages: закрытые и чужие страницы уходят из реестра",
          tid_a not in wkp._pages and 999 not in wkp._pages
          and len(wkp._pages) == 1)
    # История URL: чужая запись под тем же id не наследуется
    wkh = ba._CdpWorker()
    p_new = _Pg("https://fresh.ru/start")
    wkh._url_hist[id(p_new)] = ["https://alien.ru/от-другой-вкладки"]
    wkh.ensure_browser = lambda allow_launch=False: None
    wkh._browser = _FakeBr([p_new])
    wkh._all_pages()
    check("история URL: запись прошлой вкладки с тем же id не наследуется",
          wkh._url_hist[id(p_new)] == ["https://fresh.ru/start"])
    p_new.url = "https://fresh.ru/next"
    wkh._all_pages()
    check("история URL: своя история накапливается",
          wkh._url_hist[id(p_new)] == ["https://fresh.ru/start",
                                       "https://fresh.ru/next"])
    wkh._browser = _FakeBr([])
    wkh._all_pages()
    gc.collect()
    check("история URL: закрытая вкладка выброшена (и владелец тоже)",
          not wkh._url_hist and not dict(wkh._hist_owner))
    check("page_for_user_visible идёт через _all_pages (соединение гарантировано)",
          "def page_for_user_visible" in SRC
          and "pages = [p for p in self._all_pages()" in SRC)

    print("\n── 7. Полный захват и дедуп снапшота ──")

    check("захват: исходный инлайновый display запоминается в метке",
          "els[i].setAttribute('data-vpc-fp-hide',els[i].style.display||'')"
          in ba._FULLPAGE_HIDE_FIXED_JS)
    check("захват: восстановление возвращает исходный display, "
          "а не затирает его",
          "e.style.removeProperty('display')" in ba._FULLPAGE_UNHIDE_JS
          and "e.style.display=d" in ba._FULLPAGE_UNHIDE_JS
          and "e.style.display=''" not in ba._FULLPAGE_UNHIDE_JS)
    if shutil.which("node"):
        node_js = """
        var els = [
          {m:'flex', st:{display:'none'}, gone:false},
          {m:'',     st:{display:'none'}, gone:false}];
        function mk(e){ return {
          style: {set display(v){e.st.display=v;},
                  get display(){return e.st.display;},
                  removeProperty:function(k){e.st[k]=null;e.gone=true;}},
          getAttribute:function(){return e.m;},
          removeAttribute:function(){}};}
        var nodes = els.map(mk);
        global.document = {querySelectorAll:function(sel){
          return sel.indexOf('fp-hide') >= 0 ? nodes : [];}};
        var f = %s;
        f();
        if (els[0].st.display !== 'flex') throw new Error('display не восстановлен');
        if (!els[1].gone) throw new Error('инлайновый display не снят');
        console.log('ok');
        """ % ba._FULLPAGE_UNHIDE_JS
        r = subprocess.run(["node", "-e", node_js], capture_output=True,
                           text=True, timeout=30)
        check("захват (node): display:flex восстановлен, пустой — снят",
              r.returncode == 0 and "ok" in r.stdout)
    _dd = ba._dedup_snapshot_items([
        _it(1, "a", "Очень длинный заголовок карточки",
            href="https://a.ru/1", x=10.0, y=10.0, w=300.0, h=40.0),
        _it(2, "span", "Очень длинный заголовок кар…",
            x=12.0, y=14.0, w=300.0, h=20.0)])
    check("дедуп снапшота: обрезанный «…» фрагмент схлопывается "
          "(общее правило текста _texts_dup)",
          [i["idx"] for i in _dd] == [1])
    _dd2 = ba._dedup_snapshot_items([
        _it(3, "a", "Заголовок первой карточки", href="https://a.ru/1",
            x=10.0, y=10.0, w=300.0, h=40.0),
        _it(4, "a", "Заголовок второй карточки", href="https://a.ru/2",
            x=12.0, y=14.0, w=300.0, h=40.0)])
    check("дедуп снапшота: разные href и разный текст — оба живы",
          [i["idx"] for i in _dd2] == [3, 4])
    check("дедуп снапшота: оба правила считаются для каждой пары",
          "same_link or same_text" in SRC)

    print("\n── 8. Слайдер: один компаратор, метка всегда снимается ──")

    check("_slider_accepted: допуск 0.51, нечисловой ответ — «не принял»",
          ba._slider_accepted("8", 8) and ba._slider_accepted(" 8.4 ", 8)
          and not ba._slider_accepted("9", 8)
          and not ba._slider_accepted("", 8)
          and not ba._slider_accepted("on", 8)
          and not ba._slider_accepted(None, 8))

    _o4 = dict(sel=ba._select_backend, sx=ba._safari_exec,
               sub=ba._WORKER.submit)
    try:
        ba._select_backend = lambda *a, **kw: "safari"
        calls = []

        def _sx_nonnum(host, js, tab_id=None):
            calls.append(js)
            if js.startswith("(function(label"):
                return json.dumps({"st": "range", "v": 8})
            if js == ba._SLIDER_VERIFY_JS:
                return "не-число"
            return "ok"

        ba._safari_exec = _sx_nonnum
        err = ""
        try:
            ba.set_slider("a.ru", "громкость", 8)
        except ba.BrowserUnavailable as e:
            err = str(e)
        except ValueError as e:  # так проявился бы голый float(got)
            err = f"ValueError: {e}"
        check("слайдер Safari: нечисловое значение — честный отказ, "
              "а не ValueError",
              "не принял значение" in err
              and ba._SLIDER_UNMARK_JS in calls)
        # Отказ «нет слайдеров» тоже снимает метку
        calls.clear()
        ba._safari_exec = lambda host, js, tab_id=None: (
            calls.append(js), "мусор не json")[1]
        err2 = ""
        try:
            ba.set_slider("a.ru", "громкость", 8)
        except ba.BrowserUnavailable as e:
            err2 = str(e)
        check("слайдер Safari: нечитаемый ответ JS — метка всё равно снята",
              "нет слайдеров" in err2 and ba._SLIDER_UNMARK_JS in calls)
        # CDP-ветка: та же гарантия
        ba._select_backend = lambda *a, **kw: "cdp"
        pg_sl = _Pg("https://a.ru/", res="мусор не json")
        wksl = ba._CdpWorker()
        wksl.page_for = lambda host, tab_id=None: pg_sl
        ba._WORKER.submit = lambda fn, timeout=None: fn(wksl)
        err3 = ""
        try:
            ba.set_slider("a.ru", "громкость", 8)
        except ba.BrowserUnavailable as e:
            err3 = str(e)
        check("слайдер CDP: метка снимается на отказе (finally)",
              "нет слайдеров" in err3
              and ba._SLIDER_UNMARK_JS in pg_sl.evals)
        check("снятие метки слайдера — одно место (_SLIDER_UNMARK_JS "
              "плюс чтение-со-снятием в _SLIDER_VERIFY_JS)",
              SRC.count("removeAttribute('data-vpc-slider')") == 2
              and "removeAttribute('data-vpc-slider')" in ba._SLIDER_UNMARK_JS
              and "removeAttribute('data-vpc-slider')" in ba._SLIDER_VERIFY_JS)
    finally:
        ba._select_backend, ba._safari_exec = _o4["sel"], _o4["sx"]
        ba._WORKER.submit = _o4["sub"]

    print("\n── 9. Бюджеты опроса шлюза и мёртвый код ──")

    pg_g = _Pg("https://a.ru/", res=lambda js: 0)
    t0 = time.monotonic()
    st = ba._gateway_status(pg_g, budget_sec=0.3)
    check("_gateway_status: бюджет явный и соблюдается",
          st is None and time.monotonic() - t0 < 1.5)
    budgets = []
    _o5 = ba._gateway_status
    try:
        def _gs(page, budget_sec=ba.GATEWAY_PROBE_SEC):
            budgets.append(budget_sec)
            return 502 if len(budgets) == 1 else None

        ba._gateway_status = _gs

        class _W:
            def _new_page_quiet(self, ctx, url):
                return _Pg(url), True

        ba._open_page_gateway_retry(_W(), None, "https://a.ru/")
        check("_open_page_gateway_retry: перепроверка — короткий бюджет "
              "(воркер не держится 30с)",
              budgets == [ba.GATEWAY_PROBE_SEC, ba.GATEWAY_RECHECK_SEC]
              and ba.GATEWAY_RECHECK_SEC <= 5.0)
    finally:
        ba._gateway_status = _o5
    check("бюджет submit'а навигации считается из шагов внутри",
          ba.SUBMIT_NAV_TIMEOUT_SEC == (2 * ba.NAV_GOTO_TIMEOUT_SEC
                                        + ba.GATEWAY_PROBE_SEC
                                        + ba.GATEWAY_RECHECK_SEC + 20.0))
    check("set_tab_frozen убран (мёртвый код, никем не вызывался)",
          not hasattr(ba, "set_tab_frozen")
          and "setWebLifecycleState" not in SRC)

    print("\n── 10. Брошенное поколение воркера отпускает соединение ──")

    class _FakePw:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    wkg = ba._CdpWorker()
    fake_pw = _FakePw()
    release = threading.Event()

    def _hang(w):
        w._pw = fake_pw
        w._browser = object()
        release.wait(10)
        return "поздно"

    hung = False
    try:
        wkg.submit(_hang, timeout=0.5)
    except ba.BrowserUnavailable:
        hung = True
    check("воркер: зависшая операция брошена, соединение — в _abandoned",
          hung and wkg._gen == 1 and 0 in wkg._abandoned
          and wkg._abandoned[0][0] is fake_pw and not fake_pw.stopped)
    check("воркер: новое поколение работает своим соединением",
          wkg.submit(lambda w: "жив", timeout=5.0) == "жив")
    release.set()
    for _ in range(50):
        if fake_pw.stopped:
            break
        time.sleep(0.1)
    check("воркер: вернувшийся зависший вызов закрыл СВОЁ соединение и умер",
          fake_pw.stopped and not wkg._abandoned)
    _rel_body = SRC.split("def _release_abandoned")[1].split("\n    def ")[0]
    check("воркер: брошенное поколение не гасит Chrome (browser.close не зовём)",
          "pw.stop()" in _rel_body
          and "browser.close()\n" not in _rel_body)

    print("\n── 11. chrome_debug.sh: флаги из кода, без копии ──")

    sh_path = Path(ba.__file__).parents[2] / "scripts" / "chrome_debug.sh"
    sh = io.open(sh_path, encoding="utf-8").read()
    sh_code = "\n".join(ln for ln in sh.splitlines()
                        if not ln.lstrip().startswith("#"))
    check("chrome_debug.sh: --mute-audio убран (расходился с кодом)",
          "--mute-audio" not in sh_code)
    check("chrome_debug.sh: своей копии списка флагов нет",
          "--disable-sync" not in sh and "--disable-component-update" not in sh
          and "CHROME_THRIFT_FLAGS" in sh
          and "CHROME_NO_THROTTLE_FLAGS" in sh)
    body = sh.split("<<'PYEOF'\n", 1)[1].split("\nPYEOF\n", 1)[0]
    r = subprocess.run([sys.executable, "-c", body], capture_output=True,
                       text=True, timeout=60,
                       env={"PYTHONPATH": str(Path(ba.__file__).parents[2]),
                            "PATH": os.environ.get("PATH", ""),
                            "HOME": os.environ.get("HOME", "")})
    argv = [ln for ln in (r.stdout or "").splitlines() if ln]
    check("chrome_debug.sh: argv собирается из тех же констант, что у бота",
          r.returncode == 0
          and all(f in argv for f in ba.CHROME_THRIFT_FLAGS)
          and all(f in argv for f in ba.CHROME_NO_THROTTLE_FLAGS)
          and any(a.startswith("--remote-debugging-port=") for a in argv)
          and not any(a.startswith("--headless") for a in argv))

    print("\n── 12. «Бэкенд не умеет» — тип, а не подстрока ──")

    check("BackendUnsupported — подвид BrowserUnavailable со своим классом",
          issubclass(ba.BackendUnsupported, ba.BrowserUnavailable)
          and ba.BackendUnsupported.error_class == "backend")
    _o6 = ba._select_backend
    try:
        ba._select_backend = lambda *a, **kw: "applescript"
        kind = ""
        try:
            ba._eval_in_tab(None, None, "1+1", backends=("cdp",))
        except ba.BackendUnsupported:
            kind = "typed"
        except ba.BrowserUnavailable:
            kind = "plain"
        check("_eval_in_tab: отказ по бэкенду — BackendUnsupported",
              kind == "typed")
        check("chat_wait_uploaded: нет транспорта проверки — отправку "
              "не блокируем (по типу, не по тексту)",
              ba.chat_wait_uploaded(None, None, "textarea") is True)
    finally:
        ba._select_backend = _o6
    check("формулировки «требует бэкенд»/«только с CDP-бэкендом» "
          "больше не разбираются по подстроке",
          SRC.count("требует бэкенд") == 1  # только в докстринге исключения
          and "только с CDP-бэкендом" not in SRC
          and "только на CDP-бэкенде" not in SRC
          and SRC.count("raise _no_backend(") >= 12)


    print("\n── 13. Лимиты снапшотов: JS берёт их из констант ──")

    import re as _re
    _snap_tpl, _goal_tpl = ba._SNAPSHOT_JS, ba._GOAL_SNAPSHOT_JS
    check("снапшот: общий бюджет в JS — из SNAPSHOT_MAX",
          f"var M={ba.SNAPSHOT_MAX};" in _snap_tpl
          and f"var M={ba.GOAL_SNAPSHOT_MAX};" in _goal_tpl)
    check("снапшот: этапные бюджеты — доли общего, а не числа",
          "var M25=Math.round(M*0.25)" in _snap_tpl
          and "M50=Math.round(M*0.5)" in _snap_tpl
          and "M60=Math.round(M*0.6)" in _snap_tpl
          and "M70=Math.round(M*0.7)" in _snap_tpl)
    check("снапшот: в шаблонах не осталось литералов B+100/B+25/B+50…",
          not _re.search(r"B\+\d", _snap_tpl)
          and not _re.search(r"B\+\d", _goal_tpl)
          and _snap_tpl.count("B+M") >= 7 and _goal_tpl.count("B+M") >= 5)
    # Шаблон заполняется одним BASE, без лишних плейсхолдеров (иначе
    # разъехались бы вызывающие и шаблонный линтер)
    _fill_ok = True
    try:
        _sf = ba._js_fill(_snap_tpl, BASE=100)
        _gf = ba._js_fill(_goal_tpl, GOAL="цель", BASE=100)
    except Exception:
        _fill_ok = False
        _sf = _gf = ""
    check("снапшот: заполнение шаблона не потребовало новых плейсхолдеров",
          _fill_ok and "__MAX__" not in _snap_tpl and "__MAX__" not in _goal_tpl
          and not _re.search(r"B\+\d", _sf) and not _re.search(r"B\+\d", _gf))
    if shutil.which("node"):
        _bad_tpl = []
        for _n, _t in (("_SNAPSHOT_JS", _sf), ("_GOAL_SNAPSHOT_JS", _gf)):
            _f = tmp / f"{_n}.js"
            _f.write_text("var __x = " + _t + ";\n", encoding="utf-8")
            if subprocess.run(["node", "--check", str(_f)],
                              capture_output=True, timeout=30).returncode:
                _bad_tpl.append(_n)
        check("снапшот (node): шаблоны с M синтаксически валидны",
              _bad_tpl == [])

    # Признак перекрытия cov (элемент под попапом, когда бэкдроп не пойман
    # детектом) — одно определение vpcCov в обоих шаблонах, эмиссия из
    # vpcInfo/info, парсер отдаёт bool с дефолтом «не перекрыт»
    _cov_defs = [_re.search(r"function vpcCov\(e\)\{.*?return 0;\}", _t)
                 for _t in (_snap_tpl, _goal_tpl)]
    check("cov: vpcCov определён в обоих шаблонах и копии идентичны",
          all(_cov_defs)
          and _cov_defs[0].group(0) == _cov_defs[1].group(0)
          and _snap_tpl.count("function vpcCov(") == 1
          and _goal_tpl.count("function vpcCov(") == 1)
    check("cov: поле эмитится из vpcInfo общего и info целевого снапшота",
          "cov:vpcCov(e)" in _snap_tpl and "cov:vpcCov(e)" in _goal_tpl
          and "if(tp&&tp!==e&&!e.contains(tp)&&!tp.contains(e))cov=1;}" in _goal_tpl)
    _cov_items = [{"idx": 7, "tag": "a", "text": "Под попапом", "w": 100,
                   "h": 20, "vp": 1, "cov": 1},
                  {"idx": 8, "tag": "label", "text": "Пункт попапа", "w": 100,
                   "h": 20, "vp": 1, "md": 1}]
    _, _cov_parsed = ba._parse_snapshot(json.dumps(
        {"url": "https://x.ru/", "items": _cov_items}))
    check("cov: парсер — bool, без ключа считается не перекрытым",
          [it.get("cov") for it in _cov_parsed] == [True, False])

    print("\n── 14. Стем слова — одно определение на проект ──")

    check("стем: целевой снапшот подключает общий _VPC_NORM_JS",
          ba._VPC_NORM_JS in ba._GOAL_SNAPSHOT_JS
          and ba._VPC_NORM_CORE_JS in ba._VPC_NORM_JS)
    check("стем: своей копии wmatch в целевом снапшоте нет",
          "wmatch" not in ba._GOAL_SNAPSHOT_JS
          and "__vpcWIn(own,words[wi])" in ba._GOAL_SNAPSHOT_JS
          and ba._GOAL_SNAPSHOT_JS.count("function __vpcStem") == 1)
    check("стем: корзина и состав продукта — тот же хелпер",
          ba._VPC_NORM_JS in ba._CART_FIND_JS
          and ba._VPC_NORM_JS in ba._COMP_EDIT_FIND_JS)
    if shutil.which("node"):
        # Стем питона и JS — ОДНА таблица падежных окончаний
        # (web_search._WORD_ENDINGS) и один алгоритм: посимвольное совпадение
        # на корпусе, как у __vpcN против _norm_match
        from app.features.web_search import _WORD_ENDINGS, _stem

        check("стем: JS собран из питоновской таблицы окончаний",
              tuple(ba._STEM_ENDINGS) == tuple(_WORD_ENDINGS)
              and len(ba._STEM_ENDINGS) >= 30
              and "var __vpcEnds=" in ba._VPC_NORM_JS
              # усечение окончаний по длине не дублируется своей формулой
              and "w.length>=7?w.length-3" not in SRC
              and SRC.count("function __vpcStem") == 1
              # _READ_SECTION_JS использует тот же общий хелпер, без своей копии
              and ba._VPC_NORM_JS in ba._READ_SECTION_JS
              and "function stem(w)" not in SRC)
        _stem_corpus = [
            "додстера", "додстер", "айс", "гавайская", "пепперони",
            "мороженое", "кола", "коктейль", "поиск", "подписки", "сыр",
            "lumieres", "lumiere", "елка", "настройки", "настройка", "видео",
            "канал", "канала", "каналы", "орловой", "орлова", "корзину",
            "корзина", "очередь", "очереди", "сообщение", "сообщения",
            "cafe", "the", "button", "buttons", "settings", "setting",
            "пиццы", "пиццу", "троеточие", "плейлиста", "открыто",
            "a", "ab", "abc", "abcd", "abcde", "ой", "ью", "ого", "и", "",
        ]
        _win_cases = [["додстера", "додстер большой"],   # окончание усечено
                      ["айс", "гавайская пицца"],        # не внутри слова
                      ["айс", "айс ти"],                 # с начала слова
                      ["мороженое", "морковь корейская"],  # общий префикс мал
                      ["кола", "кока кола"],             # второе слово текста
                      ["сыр", "сырный соус"],            # короткое не режем
                      ["настройки", "настройка профиля"],
                      ["каналы", "канал новостей"]]
        _stem_script = (
            "global.__x=(function(){" + ba._VPC_NORM_JS
            + " return {stem:__vpcStem, win:__vpcWIn};})();\n"
            "var corpus=" + json.dumps(_stem_corpus, ensure_ascii=False) + ";\n"
            "var pairs=" + json.dumps(_win_cases, ensure_ascii=False) + ";\n"
            "console.log(JSON.stringify({s:corpus.map(__x.stem),"
            "w:pairs.map(function(p){return __x.win(p[1],p[0]);})}));\n")
        _sf2 = tmp / "stem_parity.js"
        _sf2.write_text(_stem_script, encoding="utf-8")
        _r = subprocess.run(["node", str(_sf2)], capture_output=True,
                            text=True, timeout=30)
        _got = json.loads(_r.stdout) if _r.returncode == 0 and _r.stdout else {}
        check(f"стем (node): __vpcStem посимвольно совпадает с web_search._stem "
              f"на корпусе {len(_stem_corpus)} слов",
              len(_stem_corpus) >= 30
              and _got.get("s") == [_stem(w) for w in _stem_corpus])
        check("стем (node): матчинг слов не изменился после смены формулы",
              _got.get("w") == [True, False, True, False, True, True,
                                True, True])
        from app.core import word_stem as _word_stem

        check("стем: таблица — общий stdlib-модуль app.core.word_stem, "
              "ast в browser_actions для неё больше не используется",
              "import ast" not in SRC
              and "ast.literal_eval" not in SRC
              and "ast.parse" not in SRC
              and "_py_word_endings" not in SRC
              and "from app.core.word_stem import" in SRC
              and tuple(ba._STEM_ENDINGS) == tuple(_word_stem.WORD_ENDINGS)
              and tuple(_WORD_ENDINGS) == tuple(_word_stem.WORD_ENDINGS))

    print("\n── 15. press_key: один потолок и добивание остатка ──")

    import app.features.computer_control as _cc
    check("потолок нажатий — одна константа на проект",
          ba.PRESS_TIMES_MAX == _cc._ERASE_MAX == 100)
    check("«удали N символов»: обещание разбора = возможности press_key",
          _cc.parse_erase_request("удали 50 символов")
          == (("Backspace", 50, "erase"), None)
          and _cc.parse_erase_request("удали 5000 символов")
          == (("Backspace", ba.PRESS_TIMES_MAX, "erase"), None))

    class _Kb:
        def __init__(self):
            self.presses = []

        def press(self, key):
            self.presses.append(key)

    class _Loc:
        # locator('video').first: press падает на N-м нажатии серии.

        def __init__(self, fail_at=None):
            self.presses = []
            self.fail_at = fail_at

        @property
        def first(self):
            return self

        def press(self, key, timeout=None):
            self.presses.append(key)
            if self.fail_at is not None and len(self.presses) == self.fail_at:
                raise RuntimeError("плеер не принял нажатие")

    class _KeyPg(_Pg):
        def __init__(self, loc):
            super().__init__("https://www.youtube.com/watch?v=1")
            self.keyboard = _Kb()
            self._loc = loc

        def locator(self, sel):
            return self._loc

    _o7 = dict(sel=ba._select_backend, sub=ba._WORKER.submit)
    try:
        ba._select_backend = lambda *a, **kw: "cdp"
        _loc = _Loc(fail_at=3)
        _kpg = _KeyPg(_loc)
        _wkk = ba._CdpWorker()
        _wkk.page_for = lambda host, tab_id=None: _kpg
        ba._WORKER.submit = lambda fn, timeout=None: fn(_wkk)
        ba.press_key("youtube.com", "ArrowUp", times=5)
        check("press_key: серия оборвалась на 3-м — добит только остаток "
              "(было 5 сверху ещё 5)",
              len(_loc.presses) == 3 and len(_kpg.keyboard.presses) == 3
              and len(_loc.presses) - 1 + len(_kpg.keyboard.presses) == 5)
        # Серия прошла целиком — клавиатура в документ не дублирует
        _loc2 = _Loc()
        _kpg2 = _KeyPg(_loc2)
        _wkk.page_for = lambda host, tab_id=None: _kpg2
        ba.press_key("youtube.com", "ArrowUp", times=4)
        check("press_key: успешная серия не дублируется в документ",
              len(_loc2.presses) == 4 and _kpg2.keyboard.presses == [])
        # Потолок серии — PRESS_TIMES_MAX, а не 10
        _loc3 = _Loc(fail_at=1)
        _kpg3 = _KeyPg(_loc3)
        _wkk.page_for = lambda host, tab_id=None: _kpg3
        ba.press_key("youtube.com", "Backspace", times=50)
        check("press_key: 50 нажатий действительно уходят (потолок 100)",
              len(_kpg3.keyboard.presses) == 50)
        _loc4 = _Loc(fail_at=1)
        _kpg4 = _KeyPg(_loc4)
        _wkk.page_for = lambda host, tab_id=None: _kpg4
        ba.press_key("youtube.com", "Backspace", times=500)
        check("press_key: выше потолка клампится (одним числом с разбором)",
              len(_kpg4.keyboard.presses) == ba.PRESS_TIMES_MAX)
    finally:
        ba._select_backend, ba._WORKER.submit = _o7["sel"], _o7["sub"]

    print("\n── 16. Звук направленно (mute/unmute), отчёт по факту ──")

    check("медиа: явные mute/unmute/toggle_mute заведены в шаблоне",
          "if(op==='mute'){v.muted=true;return 'muted';}" in ba._MEDIA_VOLUME_JS
          and "if(op==='unmute'){v.muted=false;return 'unmuted';}"
          in ba._MEDIA_VOLUME_JS
          and "op==='toggle_mute'" in ba._MEDIA_VOLUME_JS
          and "v.muted=!v.muted;return v.muted?'muted':'unmuted';}"
          in ba._MEDIA_VOLUME_JS)
    if shutil.which("node"):
        _vid_harness = (
            "function mkVid(m,vol){return {muted:m,volume:vol,paused:false,"
            "play:function(){this.paused=false;},"
            "pause:function(){this.paused=true;},"
            "getBoundingClientRect:function(){return {width:640,height:360};}};}"
            "\nglobal.document={querySelectorAll:function(){return [V];}};\n")
        _vid_cases = [("mute", False), ("mute", True), ("unmute", True),
                      ("unmute", False), ("toggle_mute", False),
                      ("toggle_mute", True), ("-0.2", True), ("вздор", False)]
        _vid_got = []
        for _op, _muted in _vid_cases:
            _js = ba._js_fill(ba._MEDIA_VOLUME_JS, OP=_op)
            _f3 = tmp / "vid.js"
            _f3.write_text(
                _vid_harness + f"var V=mkVid({str(_muted).lower()},0.5);\n"
                f"var r={_js};\n"
                "console.log(JSON.stringify([r,V.muted,V.volume]));\n",
                encoding="utf-8")
            _r3 = subprocess.run(["node", str(_f3)], capture_output=True,
                                 text=True, timeout=30)
            _vid_got.append(json.loads(_r3.stdout) if _r3.returncode == 0
                            and _r3.stdout else None)
        check("медиа (node): «выключи звук» глушит независимо от текущего "
              "состояния, «включи звук» — включает",
              _vid_got[0] == ["muted", True, 0.5]
              and _vid_got[1] == ["muted", True, 0.5]
              and _vid_got[2] == ["unmuted", False, 0.5]
              and _vid_got[3] == ["unmuted", False, 0.5])
        check("медиа (node): toggle_mute сохранил прежнее переключение",
              _vid_got[4] == ["muted", True, 0.5]
              and _vid_got[5] == ["unmuted", False, 0.5])
        check("медиа (node): шаг громкости снимает мьют и отдаёт vol:NN, "
              "неизвестная операция — честная причина",
              _vid_got[6] == ["vol:30", False, 0.3]
              and isinstance(_vid_got[7], list)
              and _vid_got[7][0].startswith("неизвестная операция со звуком")
              and _vid_got[7][1] is False and _vid_got[7][2] == 0.5)
    # Отчёт: по 'muted'/'unmuted' — про звук, а не про громкость
    _dd_mgr = _cc.ComputerControlManager.describe_done
    check("медиа: отчёт говорит «включил/выключил звук», а не «громкость N%»",
          "включил звук" in _dd_mgr({"kind": "media_vol", "host": "y.ru",
                                     "vol_done": "unmuted"})
          and "выключил звук" in _dd_mgr({"kind": "media_vol", "host": "y.ru",
                                          "vol_done": "muted"})
          and "громкость 30%" in _dd_mgr({"kind": "media_vol", "host": "y.ru",
                                          "vol_done": "vol:30"}))
    # Причина отказа из JS — исключение, а не строка-«успех» в отчёте
    _orig_eja = ba._eval_js_any
    try:
        _mv_res = {}
        def _mv_run(answer):
            ba._eval_js_any = lambda h, t, js: answer
            try:
                return ba.media_volume_op("y.ru", "unmute")
            except ba.BrowserUnavailable as e:
                return e
        _mv_res["ok"] = [_mv_run(a) for a in ("vol:30", "muted", "unmuted",
                                             "paused", "playing")]
        _mv_res["bad"] = [_mv_run(a) for a in ("нет видео на странице",
                                              "неизвестная операция со звуком: x", "")]
        check("media_volume_op: успешные ответы возвращаются как есть",
              _mv_res["ok"] == ["vol:30", "muted", "unmuted", "paused", "playing"])
        check("media_volume_op: причина из JS → BrowserUnavailable с её текстом",
              all(isinstance(e, ba.BrowserUnavailable) for e in _mv_res["bad"])
              and "нет видео" in str(_mv_res["bad"][0])
              and "неизвестная операция" in str(_mv_res["bad"][1]))
    finally:
        ba._eval_js_any = _orig_eja


    print("\n── 17. AppleScript при нескольких экземплярах браузера ──")

    check("_page_key: query/#fragment/www/хвостовой слеш не различают страницу",
          ba._page_key("https://www.YouTube.com/results?q=a#x")
          == ba._page_key("https://youtube.com/results/")
          and ba._page_key("https://y.ru/watch") != ba._page_key("https://y.ru/results")
          and ba._page_key("не url") == "")

    _o8 = dict(pids=ba._as_browser_pids, urls=ba._bot_page_urls,
               run=ba._as_run)
    try:
        _as_url = ["https://www.youtube.com/watch?v=1"]
        ba._as_run = lambda script, browser="chrome", app=None: _as_url[0]
        # (а) один экземпляр — сверка не нужна
        ba._as_browser_pids = lambda app=None: [111]
        ba._bot_page_urls = lambda: ()
        check("один экземпляр: мост работает, сверка не нужна",
              ba._as_single_target() is True
              and ba._as_foreign_instance(_as_url[0]) is False)
        if sys.platform == "darwin":
            check("один экземпляр: переднее окно отдаётся как есть",
                  ba._front_window_url() == _as_url[0])
        # (б) два экземпляра + URL совпал со вкладкой пула — работает
        ba._as_browser_pids = lambda app=None: [111, 222]
        ba._bot_page_urls = lambda: ("https://www.youtube.com/watch?v=1",
                                     "about:blank")
        check("два экземпляра + URL из вкладок пула: мост допущен",
              ba._as_foreign_instance(_as_url[0]) is False
              and ba._as_url_is_bot_page(_as_url[0]) is True)
        # тот же URL с другим query — та же страница (SPA перепишет query)
        check("два экземпляра: сверка по scheme://host/path, не по query",
              ba._as_url_is_bot_page("https://youtube.com/watch?v=1&t=30")
              is True)
        if sys.platform == "darwin":
            check("два экземпляра + совпало: переднее окно используется",
                  ba._front_window_url() == _as_url[0]
                  and ba._as_single_target() is True)
        # (в) два экземпляра + URL чужой — отказ
        ba._bot_page_urls = lambda: ("https://www.youtube.com/results?q=b",)
        check("два экземпляра + чужой URL: ответ признан личным браузером",
              ba._as_foreign_instance(_as_url[0]) is True)
        if sys.platform == "darwin":
            check("два экземпляра + чужой URL: переднее окно не используется, "
                  "мосты молчат",
                  ba._front_window_url() == ""
                  and ba._as_single_target() is False)
        # (г) отладочного порта нет вовсе (чистый applescript-бэкенд):
        # сверять не с чем — мосту не мешаем
        ba._bot_page_urls = lambda: ()
        check("два экземпляра без отладочного порта: сверять не с чем — "
              "мост работает",
              ba._as_foreign_instance(_as_url[0]) is False
              and ba._as_single_target() is True)
        # (д) чужое имя приложения (перебор браузеров) — подтвердить нечем
        check("чужой браузер с несколькими экземплярами — отказ",
              ba._as_single_target("Opera") is False)
        # (е) мосты без URL в ответе спрашивают ту же точку
        if sys.platform == "darwin":
            ba._bot_page_urls = lambda: ("https://www.youtube.com/results?q=b",)
            _calls = []
            _o_run = ba._as_run
            ba._as_run = lambda script, browser="chrome", app=None: (
                _calls.append(script), _as_url[0])[1]
            _amb = ""
            try:
                ba._run_apple_events("youtube.com", "1+1")
            except ba.BrowserUnavailable as e:
                _amb = str(e)
            check("JS во вкладке: отказ идёт через ту же проверку",
                  "несколько экземпляров" in _amb
                  and ba._find_tab_applescript("youtube.com") is None)
            ba._as_run = _o_run
    finally:
        ba._as_browser_pids, ba._bot_page_urls = _o8["pids"], _o8["urls"]
        ba._as_run = _o8["run"]

    print("\n── 18. Windows: профиль и завершение процесса ──")

    _win_prof = tmp / "win-profile"
    (_win_prof / "Default").mkdir(parents=True, exist_ok=True)
    (_win_prof / "Default" / "Preferences").write_text(
        json.dumps({"profile": {"name": "win"}}), encoding="utf-8")
    check("win32: без lockfile профиль считается закрытым",
          ba._win_profile_closed(str(_win_prof)) is True)
    (_win_prof / "lockfile").write_text("", encoding="utf-8")

    class _FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self, busy):
            self.busy = busy
            self.calls = []

        def locking(self, fd, mode, nbytes):
            self.calls.append(mode)
            if self.busy and mode == self.LK_NBLCK:
                raise OSError(36, "уже заблокирован другим процессом")

    _real_msvcrt = sys.modules.get("msvcrt")
    _real_platform = sys.platform
    try:
        sys.modules["msvcrt"] = _FakeMsvcrt(busy=True)
        check("win32: занятый lockfile — профиль не правим",
              ba._win_profile_closed(str(_win_prof)) is False)
        _free = _FakeMsvcrt(busy=False)
        sys.modules["msvcrt"] = _free
        check("win32: lockfile свободен — профиль закрыт, править можно",
              ba._win_profile_closed(str(_win_prof)) is True
              and _free.calls == [_free.LK_NBLCK, _free.LK_UNLCK])
        # Memory Saver на win32 включается — но только при закрытом профиле
        sys.platform = "win32"
        check("win32: закрытость профиля решает _prefs_editable",
              ba._prefs_editable(str(_win_prof)) is True)
        ba._enable_memory_saver(str(_win_prof))
        _wp = json.loads((_win_prof / "Default" / "Preferences")
                         .read_text(encoding="utf-8"))
        check("win32: Memory Saver включается при закрытом профиле",
              _wp.get("high_efficiency_mode", {}).get("enabled") is True
              and _wp.get("profile", {}).get("name") == "win")
        sys.modules["msvcrt"] = _FakeMsvcrt(busy=True)
        (_win_prof / "Default" / "Preferences").write_text(
            json.dumps({"profile": {"name": "живой"}}), encoding="utf-8")
        ba._enable_memory_saver(str(_win_prof))
        _wp2 = json.loads((_win_prof / "Default" / "Preferences")
                          .read_text(encoding="utf-8"))
        check("win32: живой профиль не правится (правка потерялась бы)",
              "high_efficiency_mode" not in _wp2
              and _wp2.get("profile", {}).get("name") == "живой")
        # Завершение процесса: taskkill вежливо, затем /F — os.kill не зовём
        _sub_calls = []

        class _KillSub:
            DEVNULL = -3
            TimeoutExpired = subprocess.TimeoutExpired

            def run(self, cmd, **kw):
                _sub_calls.append(list(cmd))

                class _R:
                    returncode = 0
                    stdout = ""
                    stderr = ""
                return _R()

        _o9 = dict(sub=ba.subprocess, alive=ba._pid_alive)
        try:
            ba.subprocess = _KillSub()
            ba._proc_terminate(4242)
            ba._proc_kill(4242)
            check("win32: вежливое завершение — taskkill /PID, жёсткое — /F /T",
                  _sub_calls == [["taskkill", "/PID", "4242"],
                                 ["taskkill", "/F", "/T", "/PID", "4242"]])
            _sub_calls.clear()
            ba._pid_alive = lambda pid: True  # процесс не уходит по WM_CLOSE
            _kp = _Proc()
            ba._kill_chrome_on_profile(_kp, str(_win_prof), grace_sec=0.2)
            check("win32: сначала вежливо, потом принудительно (одна лестница)",
                  [c[:2] for c in _sub_calls]
                  == [["taskkill", "/PID"], ["taskkill", "/F"]])
        finally:
            ba.subprocess, ba._pid_alive = _o9["sub"], _o9["alive"]
    finally:
        sys.platform = _real_platform
        if _real_msvcrt is None:
            sys.modules.pop("msvcrt", None)
        else:
            sys.modules["msvcrt"] = _real_msvcrt
    check("os.kill(SIGTERM/SIGKILL) остался только в posix-ветках хелперов",
          SRC.count("os.kill(pid, signal.SIGTERM)") == 1
          and SRC.count("os.kill(pid, signal.SIGKILL)") == 1
          and "taskkill" in SRC)

    print("\n── 19. Реестр страниц — по поколениям воркера ──")

    wkg2 = ba._CdpWorker()
    _stale_pg = _Pg("https://stale-generation.example/")
    _fresh_pg = _Pg("https://fresh.example/")
    _rel2 = threading.Event()
    _stale_done = threading.Event()

    def _hang_register(w):
        _rel2.wait(10)
        # Зависший вызов вернулся уже после _poison — и регистрирует страницу
        ba._register_page(w, _stale_pg)
        _stale_done.set()
        return "поздно"

    _hung2 = False
    try:
        wkg2.submit(_hang_register, timeout=0.5)
    except ba.BrowserUnavailable:
        _hung2 = True
    _tid_fresh = wkg2.submit(lambda w: ba._register_page(w, _fresh_pg),
                             timeout=5.0)
    _rel2.set()
    _stale_done.wait(5)
    time.sleep(0.4)  # поток успевает освободить своё поколение и умереть
    _pages_now = wkg2.submit(lambda w: dict(w._pages), timeout=5.0)
    check("поколения: страница, зарегистрированная зависшим потоком, "
          "новому поколению не видна",
          _hung2 and wkg2._gen == 1
          and _pages_now.get(_tid_fresh) is _fresh_pg
          and all(pg is not _stale_pg for pg in _pages_now.values()))
    check("поколения: реестр брошенного поколения освобождён вместе с потоком",
          0 not in wkg2._pages_by_gen and 0 not in wkg2._hist_by_gen
          and list(wkg2._pages_by_gen) == [1])
    check("поколения: доступ к _pages/_url_hist снаружи прежний (свойства)",
          isinstance(wkg2._pages, dict) and isinstance(wkg2._url_hist, dict)
          and wkg2._pages is wkg2._pages_by_gen[1])
    check("поколения: id вкладок общие на все поколения (не переиспользуются)",
          wkg2._next_tab_id > _tid_fresh
          and "self._next_tab_id = 1  # общий на все поколения" in SRC)

    # ── 20. Точка в незакавыченном значении атрибута
    #    (iframe[src*=challenges.cloudflare]) — невалидный CSS: querySelectorAll
    #    кидает SyntaxError, и детект антибота не работает нигде — strict-режим
    #    падает с ошибкой замера на любой странице, best-effort
    #    (computer_control) молча отвечает «чисто». Ошибка JS обязана нести
    #    текст причины, а не голое «JS во вкладке упал» ──
    import re as _re
    _css_ident = _re.compile(r"^-?[A-Za-z_][\w-]*$")
    _attr_sel = _re.compile(r"\[([\w-]+)(?:[*^$~|]?=)([^\]]+)\]")

    def _bad_attr_values(js):
        bad = []
        for m in _attr_sel.finditer(js):
            v = m.group(2).strip()
            if v[:1] in "\"'" or _css_ident.match(v):
                continue
            bad.append(m.group(0))
        return bad

    check("антибот: регресс-детектор ловит незакавыченную точку в селекторе",
          _bad_attr_values("iframe[src*=challenges.cloudflare]")
          == ["[src*=challenges.cloudflare]"]
          and _bad_attr_values("iframe[src*=\"challenges.cloudflare\"]"
                               "[class*=CheckboxCaptcha]") == [])
    for _jsname in ("_ANTIBOT_JS", "_CHALLENGE_BOX_JS"):
        check(f"антибот: {_jsname} — все значения атрибутов в селекторах "
              "валидны (точка только в кавычках)",
              _bad_attr_values(getattr(ba, _jsname)) == []
              and 'challenges.cloudflare' in getattr(ba, _jsname))

    _raw_saved17 = dict(ba._RAW_TABS)
    _orig_call17 = ba._raw_tab_call
    try:
        ba._RAW_TABS[171717] = {"targetId": "T17", "sessionId": "S17",
                                "pool": "h"}
        _calls17 = []

        def _boom17(tid, method, params=None, timeout=None):
            _calls17.append(method)
            return {"result": {"type": "undefined"},
                    "exceptionDetails": {
                        "text": "Uncaught", "exception": {
                            "description": "SyntaxError: Failed to execute "
                            "'querySelectorAll' on 'Document': "
                            "'iframe[src*=challenges.cloudflare]' is not a "
                            "valid selector.\n    at <anonymous>:1:10"}}}
        ba._raw_tab_call = _boom17
        try:
            ba._raw_eval(171717, "1+1")
            _msg17 = ""
        except ba.BrowserUnavailable as e:
            _msg17 = str(e)
        check("raw_eval: текст исключения страницы попадает в ошибку "
              "(первая строка description, без стека)",
              _msg17.startswith("JS во вкладке упал: SyntaxError")
              and "not a valid selector" in _msg17
              and "\n" not in _msg17 and "at <anonymous>" not in _msg17
              and len(_calls17) == 2)
        check("raw_eval: вкладка после исключения страницы не выброшена",
              171717 in ba._RAW_TABS)
        try:
            ba.detect_antibot(None, 171717, strict=True)
            _msg17b = ""
        except ba.BrowserUnavailable as e:
            _msg17b = str(e)
        check("detect_antibot strict: причина сбоя замера видна в сообщении",
              _msg17b.startswith("антибот-проверка не выполнена: JS во вкладке "
                                 "упал: SyntaxError")
              and "not a valid selector" in _msg17b)
        check("cdp_exception_text: без description берётся text протокола, "
              "пустые details не роняют",
              ba._cdp_exception_text({"text": "Uncaught"}) == "Uncaught"
              and ba._cdp_exception_text({}) == "без текста"
              and ba._cdp_exception_text(None) == "без текста")
    finally:
        ba._raw_tab_call = _orig_call17
        ba._RAW_TABS.clear()
        ba._RAW_TABS.update(_raw_saved17)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

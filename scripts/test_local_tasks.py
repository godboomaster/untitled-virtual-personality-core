"""Тест движков служебных задач на персону (local_router + settings_api).

  - дефолт по роду задачи: на пути ответа — Ollama, фоновые — веб-чат:
    первый веб-чат fallback-цепочки персоны после основного, запасной —
    основной веб-чат (основной не веб-чат — следующий веб-чат цепочки);
  - bg_site: primary — основной первым; имя сайта — он первым;
  - явный выбор задачи (ollama / webchat + сайт) перекрывает дефолт; OCR
    всегда Ollama; мусор в YAML отбрасывается;
  - без привязки персоны — LOCAL_LLM_BACKEND (ollama);
  - get_response перебирает сайты по порядку, затем откат на Ollama;
    вид роутера персоны передаёт её context;
  - update_persona_local_tasks пишет llm.local_tasks в YAML, default снимает
    выбор, мусорные значения — отказ; живой бот перепривязывается.

Запуск: /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m scripts.test_local_tasks
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


class StubModelRouter:
    # Минимум ModelRouter, который читает local_router: основной провайдер и
    # полный порядок перебора (без основного webchat — как _get_full_order).
    # order уже считается ОТФИЛЬТРОВАННЫМ по excluded — как настоящий
    # _get_full_order() (исключение сайтов там же, где и в реальном роутере);
    # webchat_sites — сырой список персоны (не фильтруется сам по себе, как и
    # в ModelRouter — фильтрует только производные токены).

    def __init__(self, active, order, webchat_sites=(), excluded=None):
        self.active_provider = active
        self._order = list(order)
        self.webchat_sites = list(webchat_sites)
        self.excluded = set(excluded or ())

    def _get_full_order(self):
        return list(self._order)


def fresh_router():
    import app.core.local_router as lrm
    r = lrm.LocalLLMRouter.__new__(lrm.LocalLLMRouter)
    r._personas = {}
    r._webchats = {}
    r._available = True
    r._last_check = 0.0
    import threading
    r._rot_lock = threading.Lock()  # как в __init__ (очередь «rotate»)
    return r


def test_resolution():
    section("Дефолты по роду задачи и bg_site")
    import app.core.local_router as lrm
    r = fresh_router()
    deepseek_main = StubModelRouter(
        "webchat:deepseek", ["groq", "webchat:qwen", "local", "webchat:kimi"])
    r.bind_persona("p", deepseek_main, None)
    check("фоновая задача: первый fallback-веб-чат, запасной — основной",
          r._resolve_task("state_engine", "p") == ("webchat", ["qwen", "deepseek"]))
    check("задача на пути ответа: Ollama",
          r._resolve_task("intent_router", "p") == ("ollama", []))
    check("OCR: Ollama", r._resolve_task("ocr", "p") == ("ollama", []))
    check("уроки: на пути ответа — Ollama, плановый урок (тема/словарь) — веб-чат",
          r._resolve_task("learning", "p") == ("ollama", [])
          and r._resolve_task("learning_lesson", "p") == ("webchat", ["qwen", "deepseek"]))
    check("сжатие офлайн-дневника — фоновое (веб-чат)",
          r._resolve_task("offline_summary", "p") == ("webchat", ["qwen", "deepseek"]))

    r.bind_persona("p", deepseek_main, {"bg_site": "primary"})
    check("bg_site=primary: основной первым, запасной — первый fallback",
          r._resolve_task("dialogue_harvest", "p") == ("webchat", ["deepseek", "qwen"]))
    r.bind_persona("p", deepseek_main, {"bg_site": "kimi"})
    check("bg_site=сайт: он первым, запасной — основной",
          r._resolve_task("dialogue_harvest", "p") == ("webchat", ["kimi", "deepseek"]))

    cloud_main = StubModelRouter("groq", ["zai", "webchat:qwen", "webchat:deepseek"])
    r.bind_persona("c", cloud_main, None)
    check("основной не веб-чат: запасной — следующий веб-чат цепочки",
          r._resolve_task("state_engine", "c") == ("webchat", ["qwen", "deepseek"]))

    bare = StubModelRouter("webchat", ["webchat:qwen", "webchat:deepseek"],
                           webchat_sites=["qwen", "deepseek"])
    r.bind_persona("b", bare, None)
    check("голый webchat основным: основной — первый сайт, в запасных не дублируется",
          r._resolve_task("state_engine", "b") == ("webchat", ["deepseek", "qwen"]))

    # llm.exclude: персона исключила первый сайт из своей цепочки — он не
    # должен быть ни основным, ни запасным веб-чатом фоновых задач (order уже
    # отфильтрован по excluded, как настоящий router._get_full_order()).
    excl = StubModelRouter("webchat", ["webchat:deepseek"],
                           webchat_sites=["qwen", "deepseek"],
                           excluded={"webchat:qwen"})
    r.bind_persona("e", excl, None)
    check("llm.exclude: исключённый первый сайт пропущен — основной для "
          "локальных задач следующий по списку",
          r._resolve_task("state_engine", "e") == ("webchat", ["deepseek"]))

    no_wc = StubModelRouter("groq", ["zai", "local"])
    r.bind_persona("n", no_wc, None)
    check("веб-чатов в цепочке нет: фоновая задача остаётся на Ollama",
          r._resolve_task("state_engine", "n") == ("ollama", []))

    section("Явный выбор и мусор")
    r.bind_persona("p", deepseek_main, {"tasks": {
        "intent_router": {"backend": "webchat", "site": "kimi"},
        "state_engine": {"backend": "ollama"},
        "self_memory": {"backend": "webchat"},
        "ocr": {"backend": "webchat"},
        "world_engine": {"backend": "gpt"},
        "relationship": "webchat",
        "help_detect": {"backend": "webchat", "site": "nosuchsite"},
    }})
    check("задача на пути ответа → webchat kimi, запасной — основной",
          r._resolve_task("intent_router", "p") == ("webchat", ["kimi", "deepseek"]))
    check("фоновая → ollama", r._resolve_task("state_engine", "p") == ("ollama", []))
    check("webchat без сайта → веб-чат фоновых задач",
          r._resolve_task("self_memory", "p") == ("webchat", ["qwen", "deepseek"]))
    check("OCR даже с явным webchat → Ollama", r._resolve_task("ocr", "p") == ("ollama", []))
    check("неизвестный движок отброшен → дефолт (веб-чат)",
          r._resolve_task("world_engine", "p") == ("webchat", ["qwen", "deepseek"]))
    check("запись не dict отброшена → дефолт",
          r._resolve_task("relationship", "p") == ("webchat", ["qwen", "deepseek"]))
    check("неизвестный сайт отброшен → веб-чат фоновых задач",
          r._resolve_task("help_detect", "p") == ("webchat", ["qwen", "deepseek"]))

    section("Без привязки персоны")
    with mock.patch.dict(os.environ, {"LOCAL_LLM_BACKEND": ""}):
        check("не привязана: всё на Ollama",
              r._resolve_task("state_engine", "nobody") == ("ollama", [])
              and r._resolve_task("state_engine", None) == ("ollama", []))

    section("Снимок для UI")
    r.bind_persona("p", deepseek_main, {"tasks": {"state_engine": {"backend": "ollama"}}})
    snap = r.task_snapshot("p")
    by_id = {t["id"]: t for t in snap["tasks"]}
    check("основной/первый fallback/сайты персоны",
          snap["primary_site"] == "deepseek" and snap["fallback_site"] == "qwen"
          and snap["sites"] == ["deepseek", "qwen", "kimi"])
    check("explicit только у задач с выбором",
          by_id["state_engine"]["explicit"] and not by_id["world_engine"]["explicit"])
    check("background/ollama_only в снимке",
          by_id["world_engine"]["background"] and not by_id["intent_router"]["background"]
          and by_id["ocr"]["ollama_only"])
    check("все задачи LOCAL_TASKS в снимке", set(by_id) == set(lrm.LOCAL_TASKS))

    snap_e = r.task_snapshot("e")
    check("llm.exclude: снимок для UI тоже не предлагает исключённый сайт "
          "как основной/запасной фон",
          snap_e["primary_site"] == "deepseek" and snap_e["fallback_site"] is None
          and snap_e["sites"] == ["deepseek"])


def test_get_response_order():
    section("get_response: сайты по очереди, затем Ollama; вид персоны")
    import app.core.local_router as lrm
    r = fresh_router()
    r.bind_persona("p", StubModelRouter("webchat:deepseek", ["webchat:qwen"]), None)
    calls = []

    timeouts = []

    class Chat:
        def __init__(self, site, answer):
            self.site, self.answer = site, answer

        def get_response(self, messages, **kw):
            calls.append(self.site)
            timeouts.append(kw.get("timeout"))
            return self.answer

    answers = {"qwen": None, "deepseek": "ответ deepseek"}
    r._get_webchat = lambda site=None: Chat(site, answers.get(site))
    out = r.get_response([{"role": "user", "content": "x"}], task="state_engine",
                         persona="p")
    check("qwen промолчал → ответил deepseek", out == "ответ deepseek"
          and calls == ["qwen", "deepseek"])
    check("таймаут веб-чата по умолчанию — пол 150 с", timeouts == [150.0, 150.0])
    calls.clear()
    timeouts.clear()
    r.get_response([{"role": "user", "content": "x"}], task="offline_summary",
                   persona="p", webchat_timeout=40.0)
    check("webchat_timeout задаёт свой потолок на сайт", timeouts == [40.0, 40.0])

    answers["deepseek"] = None
    calls.clear()
    ollama_hits = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "ответ ollama"}}
    r._client = SimpleNamespace(post=lambda *a, **kw: ollama_hits.append(1) or Resp())
    r.model, r.base_url, r.timeout = "m", "http://x", 1.0
    out = r.get_response([{"role": "user", "content": "x"}], task="state_engine",
                         persona="p")
    check("оба веб-чата промолчали → откат на Ollama",
          out == "ответ ollama" and calls == ["qwen", "deepseek"] and ollama_hits)

    calls.clear()
    view = lrm.PersonaLocalRouter(r, "p")
    view.get_response([{"role": "user", "content": "x"}], task="dialogue_harvest")
    check("вид персоны передаёт её context (фоновая задача ушла в веб-чаты)",
          calls == ["qwen", "deepseek"])
    calls.clear()
    view.get_response([{"role": "user", "content": "x"}], task="intent_router")
    check("вид персоны: задача на пути ответа — сразу Ollama", calls == [])
    check("вид персоны читает атрибуты общего роутера",
          view.model == "m" and view._available is True)
    check("вид персоны: is_available фоновой задачи",
          view.is_available(task="state_engine") is True)


def test_rotation():
    section("bg_site=rotate: сайты по очереди (duck.ai и Google AI Mode, 01.10)")
    import app.core.local_router as lrm
    r = fresh_router()
    main = StubModelRouter("webchat:deepseek", ["webchat:qwen"])
    cfg = {"bg_site": "rotate", "rotate": {"duckai": 2, "google": 1},
           "tasks": {"intent_router": {"backend": "webchat"}}}
    r.bind_persona("p", main, cfg)
    check("снимок для настроек очередь не сдвигает",
          r._resolve_task("state_engine", "p") == ("webchat", ["duckai", "google"])
          and r._resolve_task("state_engine", "p") == ("webchat", ["duckai", "google"]))
    calls = []

    class Chat:
        def __init__(self, site):
            self.site = site

        def get_response(self, messages, **kw):
            calls.append(self.site)
            return None if self.site in dead else f"ответ {self.site}"

    dead = set()
    r._get_webchat = lambda site=None: Chat(site)
    outs = [r.get_response([{"role": "user", "content": "x"}], task="state_engine",
                           persona="p") for _ in range(6)]
    check("2:1 — два вопроса duck.ai, один Google, и снова",
          calls == ["duckai", "duckai", "google"] * 2
          and outs[2] == "ответ google")
    calls.clear()
    r.get_response([{"role": "user", "content": "x"}], task="intent_router",
                   persona="p")
    check("задача на пути ответа с backend: webchat — та же очередь",
          calls == ["duckai"])
    dead.add("duckai")
    calls.clear()
    out = r.get_response([{"role": "user", "content": "x"}], task="state_engine",
                         persona="p")
    check("очередной сайт промолчал (лимит, карантин) — ответил второй",
          out == "ответ google" and calls == ["duckai", "google"])
    check("мусор в rotate отброшен (чужой сайт, 0, не число, повтор)",
          lrm.normalize_rotate_cfg({"rotate": {"nosuch": 1, "google": 0,
                                               "duckai": "x", "qwen": 3}})
          == [("qwen", 3)])
    r.bind_persona("p", main, {"bg_site": "rotate"})
    check("rotate без очереди — как по умолчанию (fallback), не сайт «rotate»",
          r._resolve_task("state_engine", "p") == ("webchat", ["qwen", "deepseek"]))


def test_settings_api():
    section("settings_api.update_persona_local_tasks: YAML + живой бот")
    import app.api.settings_api as sa
    import app.core.local_router as lrm
    tmp = Path(tempfile.mkdtemp(prefix="vpc_local_tasks_"))
    path = tmp / "p.yaml"
    path.write_text(yaml.safe_dump({"system_prompt": "x", "llm": {"primary": "webchat:deepseek"}},
                                   allow_unicode=True), encoding="utf-8")
    r = fresh_router()
    bot = SimpleNamespace(context="api_p", persona=SimpleNamespace(persona_data={}),
                          router=StubModelRouter("webchat:deepseek", ["webchat:qwen"]))
    registry = SimpleNamespace(get=lambda name: bot if name == "p" else None, _bots={"p": bot})
    with mock.patch.object(sa, "_persona_yaml_path", lambda name: path if name == "p" else None), \
            mock.patch("app.api.runtime.registry", registry), \
            mock.patch.object(lrm, "_local_router", r):
        res = sa.update_persona_local_tasks("p", task="state_engine", backend="ollama")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        check("выбор задачи записан в llm.local_tasks",
              data["llm"]["local_tasks"] == {"tasks": {"state_engine": {"backend": "ollama"}}}
              and data["llm"]["primary"] == "webchat:deepseek")
        check("живой бот перепривязан: задача на Ollama",
              res["ok"] and r._resolve_task("state_engine", "api_p") == ("ollama", []))

        res = sa.update_persona_local_tasks("p", bg_site="primary")
        check("bg_site записан и применён",
              yaml.safe_load(path.read_text())["llm"]["local_tasks"]["bg_site"] == "primary"
              and r._resolve_task("world_engine", "api_p") == ("webchat", ["deepseek", "qwen"])
              and res["bg_site"] == "primary")

        sa.update_persona_local_tasks("p", task="intent_router", backend="webchat", site="kimi")
        check("webchat с сайтом записан",
              yaml.safe_load(path.read_text())["llm"]["local_tasks"]["tasks"]["intent_router"]
              == {"backend": "webchat", "site": "kimi"})

        sa.update_persona_local_tasks("p", task="state_engine", backend="default")
        sa.update_persona_local_tasks("p", task="intent_router", backend="default")
        sa.update_persona_local_tasks("p", bg_site="fallback")
        data = yaml.safe_load(path.read_text())
        check("default и bg_site=fallback снимают записи — секция убрана целиком",
              "local_tasks" not in data["llm"] and data["llm"]["primary"] == "webchat:deepseek")

        check("неизвестная задача — отказ",
              sa.update_persona_local_tasks("p", task="nope", backend="ollama")["ok"] is False)
        check("OCR в веб-чат — отказ",
              sa.update_persona_local_tasks("p", task="ocr", backend="webchat")["ok"] is False)
        check("мусорный движок — отказ",
              sa.update_persona_local_tasks("p", task="state_engine", backend="gpt")["ok"] is False)
        check("мусорный bg_site — отказ",
              sa.update_persona_local_tasks("p", bg_site="nosuchsite")["ok"] is False)
        check("нет персоны — None",
              sa.update_persona_local_tasks("zzz", task="state_engine", backend="ollama") is None)
        snap = sa.get_persona_local_tasks("p")
        check("get_persona_local_tasks — снимок персоны",
              snap["primary_site"] == "deepseek" and snap["fallback_site"] == "qwen")


def main():
    test_resolution()
    test_get_response_order()
    test_rotation()
    test_settings_api()
    print(f"\nИтого: {ok - failures}/{ok} OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

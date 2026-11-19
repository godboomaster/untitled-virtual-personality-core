"""Режимы персоны по триггеру: несколько YAML у одной персоны
(<персона>_modes/<режим>.yaml, app/core/persona_modes).

  A. разбор triggers: имена, ключевые слова (* — начало слова), mood
     коротко и полностью, hold_minutes; файлы режимов, master/mortal — не
     по триггеру, плохое имя файла и файл без system_prompt — не режим;
  B. выбор режима (decide/effective): имя в начале реплики и названное
     голосом, «эй, …», целым словом, длиннейшее; обычное имя снимает режим
     по имени, но не по слову; ключевое слово, exit_keywords, hold_minutes,
     «на одну реплику»; настроение (теги, пороги); «/mode off» глушит mood;
  C. PersonaLayer: промпт и settings режима в чате, другой чат — обычный;
     заметка модели (режим, выход из режима); история и память не трогаются
     (тот же промпт с той же историей); режим по триггеру важнее
     master/mortal; включённый режим переживает перезапуск; правка файлов
     без перезапуска; режим по настроению из mood_of; turn_scope;
  D. BotInstance: имена режимов — слова-вызовы (группа Telegram, срез
     имени, called_name), «/mode» — список, включить, off, auto; ход
     (process_message) выбирает режим до генерации и держит его в настройках;
  E. API и голос: режимы в GET /api/personas, называние именем режима будит
     персону и уходит ядру (called_as), у персоны без режимов — как было;
     ChatRequest.called_as; краткое описание режима для телефона;
  F. режимы в вебе (app/api/modes_api, вкладка «Режимы»): список с
     триггерами и ошибками, заготовка, запись (новый — 409 на занятый id,
     плохой YAML, без system_prompt, слова /mode и «..» в id), загрузка файла
     (id из файла и имени, YAML персоны → имена из trigger_words, замена),
     предупреждения, удаление с копией, .yml не берётся, живой бот видит
     правку сразу; те же действия через HTTP-маршруты.
LLM, сеть и микрофон не используются.
Запуск: python -m scripts.test_persona_modes"""

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="persona_modes_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

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


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _personas_dir() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="pm_personas_"))
    _write(tmp / "hero.yaml", "name: Коннор\nsystem_prompt: ОБЫЧНЫЙ\n"
           "settings:\n  temperature: 0.5\n"
           "features:\n  trigger_words: [коннор, connor]\n")
    _write(tmp / "hero_modes" / "deviant.yaml",
           "name: Коннор-девиант\nsystem_prompt: ДЕВИАНТ\nshort_prompt: КОРОТКО ДЕВИАНТ\n"
           "settings:\n  temperature: 0.95\n"
           "triggers:\n  names: [Девиант, RK800]\n  keywords: [ra9, свобод*]\n"
           "  exit_keywords: [стоп ra9]\n")
    _write(tmp / "hero_modes" / "grim.yaml",
           "name: Мрачный\nsystem_prompt: МРАЧНЫЙ\n"
           "triggers:\n  mood:\n    tags: [злой, раздраж*]\n    valence_below: -0.3\n")
    _write(tmp / "hero_modes" / "quick.yaml",
           "system_prompt: БЫСТРЫЙ\ntriggers:\n  keywords: [быстро]\n  hold_minutes: 0\n")
    _write(tmp / "hero_modes" / "master.yaml", "system_prompt: МАСТЕРУ\n")
    _write(tmp / "hero_modes" / "empty.yaml", "name: Пустой\n")
    _write(tmp / "hero_modes" / "bad name.yaml", "system_prompt: X\n")
    _write(tmp / "plain.yaml", "system_prompt: ПРОСТОЙ\n")
    return tmp


def _patches(tmp: Path):
    find = lambda n: tmp / f"{n}.yaml" if (tmp / f"{n}.yaml").exists() else None
    return [mock.patch("app.core.persona.persona_dirs", lambda: [tmp]),
            mock.patch("app.core.persona.find_persona_file", find),
            mock.patch("app.api.runtime.persona_dirs", lambda: [tmp])]


class _Patched:
    def __init__(self, tmp):
        self.ps = _patches(tmp)

    def __enter__(self):
        for p in self.ps:
            p.start()

    def __exit__(self, *a):
        for p in self.ps:
            p.stop()


# ── A ──

def test_parse():
    section("A. Разбор режимов")
    from app.core import persona_modes as pm
    t = pm.Triggers.parse({"names": ["Девиант", "  ", 5, "ёж*"], "keywords": "RA9",
                           "mood": ["Злой"], "hold_minutes": -3})
    check("имена — в нижнем регистре, пустые выкинуты, * в имени не бывает",
          t.names == ("девиант", "5"))
    check("ключевое слово строкой", t.keywords == ("ra9",))
    check("mood списком — теги", t.mood is not None and t.mood.tags == ("злой",))
    check("hold_minutes < 0 → 0", t.hold_minutes == 0)
    check("пустые triggers — режим только через /mode", pm.Triggers.parse(None).empty)
    check("mood без условий — нет условия", pm.MoodRule.parse({"x": 1}) is None)
    r = pm.MoodRule.parse({"valence_below": -0.3, "tags": ["раздраж*"]})
    check("mood: порог и тег вместе", r.matches({"valence": -0.5, "tag": "раздражён"}))
    check("mood: порог не выполнен", not r.matches({"valence": 0.1, "tag": "раздражён"}))
    check("mood: тег не тот", not r.matches({"valence": -0.5, "tag": "спокойный"}))
    check("mood: не словарь", not r.matches(None))
    r2 = pm.MoodRule.parse({"arousal_above": 0.7})
    check("mood: только порог arousal", r2.matches({"arousal": 0.8}) and not r2.matches({"arousal": 0.5}))

    tmp = _personas_dir()
    modes = pm.load_modes("hero", [tmp])
    check("файлы режимов: с system_prompt и годным именем",
          set(modes) == {"deviant", "grim", "quick", "master"})
    trig = pm.trigger_modes(modes)
    check("master/mortal — не по триггеру", set(trig) == {"deviant", "grim", "quick"})
    info = {m["id"]: m for m in pm.modes_info(modes)}
    check("modes_info: имя, имена, слова, mood",
          info["deviant"]["name"] == "Коннор-девиант" and info["deviant"]["names"] == ["девиант", "rk800"]
          and info["grim"]["mood"] == {"tags": ["злой", "раздраж*"], "valence_below": -0.3}
          and info["quick"]["hold_minutes"] == 0)
    other = Path(tempfile.mkdtemp(prefix="pm_user_"))
    _write(other / "hero_modes" / "deviant.yaml", "system_prompt: СВОЙ ДЕВИАНТ\n")
    check("файл в папке пользователя перекрывает встроенный",
          pm.load_modes("hero", [other, tmp])["deviant"]["system_prompt"] == "СВОЙ ДЕВИАНТ")
    sig = pm.signature("hero", [tmp])
    _write(tmp / "hero_modes" / "new.yaml", "system_prompt: НОВЫЙ\n")
    check("подпись папки меняется с новым файлом", pm.signature("hero", [tmp]) != sig)


# ── B ──

def test_decide():
    section("B. Выбор режима")
    from app.core import persona_modes as pm
    modes = pm.load_modes("hero", [_personas_dir()])
    trig = pm.trigger_modes(modes)
    base = ["коннор", "connor", "hero", "Коннор"]
    now = 1000.0

    a = pm.decide(trig, None, "Девиант, как ты?", base_names=base, now=now)
    check("имя режима в начале — режим по имени", a and a["mode"] == "deviant" and a["via"] == "name")
    check("«эй, rk800 …»", (pm.decide(trig, None, "эй, RK800 привет", base_names=base, now=now) or {}).get("mode") == "deviant")
    check("имя не целым словом — не обращение",
          pm.decide(trig, None, "Девианты — кто они?", base_names=base, now=now) is None)
    check("имя в середине — не обращение",
          pm.decide(trig, None, "а девиант это кто", base_names=base, now=now) is None)
    check("названо голосом (called_as)",
          (pm.decide(trig, None, "который час", called_as="Девиант", base_names=base, now=now) or {}).get("mode") == "deviant")
    check("держится, пока не сменят",
          pm.decide(trig, a, "как дела?", base_names=base, now=now + 99999) == a)
    check("обычное имя снимает режим по имени",
          pm.decide(trig, a, "Коннор, вернись", base_names=base, now=now) is None)
    check("обычное имя голосом — тоже",
          pm.decide(trig, a, "вернись", called_as="коннор", base_names=base, now=now) is None)
    k = pm.decide(trig, None, "Коннор, ты помнишь RA9?", base_names=base, now=now)
    check("ключевое слово — режим по слову (и при обращении обычным именем)",
          k and k["mode"] == "deviant" and k["via"] == "keyword")
    check("слово с * — начало слова", (pm.decide(trig, None, "ты свободен", now=now) or {}).get("mode") == "deviant")
    check("обычное имя режим по слову не снимает",
          pm.decide(trig, k, "Коннор, как дела", base_names=base, now=now) == k)
    check("exit_keywords снимают (хоть в них и слово режима)",
          pm.decide(trig, k, "всё, стоп RA9", base_names=base, now=now) is None)
    q = pm.decide(trig, None, "ответь быстро", now=now)
    check("hold_minutes: 0 — на одну реплику", q and q["mode"] == "quick" and q.get("once"))
    check("…и снят следующей репликой", pm.decide(trig, q, "а теперь?", now=now + 1) is None)
    check("…и сам истекает", pm.effective(trig, q, None, now=now + pm.ONCE_TTL_SEC + 1) is None)
    held = pm.decide({"deviant": pm.Triggers.parse({"names": ["девиант"], "hold_minutes": 10})},
                     None, "девиант, привет", now=now)
    check("hold_minutes: 10 — до срока", held["until"] == now + 600)
    check("…истёк — снят", pm.effective({"deviant": pm.Triggers()}, held, None, now=now + 601) is None)
    check("раньше в реплике — то слово и режим",
          (pm.decide(trig, None, "быстро скажи про ra9", now=now) or {}).get("mode") == "quick")
    same_name = {"deviant": pm.Triggers.parse({"names": ["коннор"]})}
    check("имя режима, совпавшее с обычным, — за режимом",
          (pm.decide(same_name, None, "Коннор, привет", base_names=base, now=now) or {}).get("mode") == "deviant")
    check("включённый режим, чей файл удалили, — не в счёт",
          pm.decide({"grim": trig["grim"]}, a, "как ты", now=now) is None)

    angry = {"valence": -0.6, "arousal": 0.4, "tag": "злой"}
    check("настроение совпало — режим по mood", pm.effective(trig, None, angry) == "grim")
    check("настроение другое — обычный", pm.effective(trig, None, {"valence": 0.4, "tag": "спокойный"}) is None)
    check("включённый по имени важнее настроения", pm.effective(trig, a, angry) == "deviant")
    off = {"mode": "", "via": "manual", "since": now}
    check("«/mode off» — обычный, настроение не решает", pm.effective(trig, off, angry) is None)
    check("…а имя режима всё равно включает",
          (pm.decide(trig, off, "Девиант, ты тут?", now=now) or {}).get("mode") == "deviant")


# ── C ──

def _hist():
    return [
        {"role": "user", "content": "привет", "user_name": "Я", "sender_id": "u1", "timestamp": 1},
        {"role": "assistant", "content": "Здравствуйте.", "timestamp": 2},
        {"role": "user", "content": "Девиант, ты тут?", "user_name": "Я", "sender_id": "u1", "timestamp": 3},
    ]


def test_layer():
    section("C. PersonaLayer: промпт, настройки, память")
    from app.core import persona as pmod
    from app.core.persona import PersonaLayer, persona_prompt
    tmp = _personas_dir()
    data_root = Path(tempfile.mkdtemp(prefix="pm_data_"))
    with _Patched(tmp):
        p = PersonaLayer("hero")
        p.bind_mode_state(data_root, "ctx_hero")
        check("режимы прочитаны", set(p.modes) == {"deviant", "grim", "quick", "master"})
        check("имена режимов", p.mode_names() == ["девиант", "rk800"])
        check("до триггера — обычный характер", p.prompt_for(user_id="u1", chat_id="c1") == "ОБЫЧНЫЙ")
        mode = p.observe_turn("c1", "Девиант, ты тут?", base_names=["коннор"])
        check("реплика с именем режима → режим", mode == "deviant")
        check("промпт чата — режима", p.prompt_for(user_id="u1", chat_id="c1") == "ДЕВИАНТ")
        check("сообщения по своей инициативе в этот чат — тоже", persona_prompt(p, "c1") == "ДЕВИАНТ")
        check("другой чат — обычный", p.prompt_for(user_id="u2", chat_id="c2") == "ОБЫЧНЫЙ")
        check("settings режима поверх общих",
              p.get_settings(chat_id="c1")["temperature"] == 0.95 and p.get_settings(chat_id="c2")["temperature"] == 0.5)
        with p.turn_scope("c1"):
            check("ход чата: get_settings() без чата — его режима", p.get_settings()["temperature"] == 0.95)
        check("вне хода — общие", p.get_settings()["temperature"] == 0.5)

        hist = _hist()
        msgs = p.prepare_messages("Девиант, ты тут?", history=hist, user_id="u1", user_name="Я", chat_id="c1")
        sys_ = msgs[0]["content"]
        check("системный блок: характер режима, заметка о режиме",
              sys_.startswith("ДЕВИАНТ") and 'YOUR MODE NOW: "Коннор-девиант"' in sys_ and "same memories" in sys_)
        msgs2 = p.prepare_messages("Девиант, ты тут?", history=hist, user_id="u1", user_name="Я", chat_id="c2")
        check("история диалога та же — меняется только системный блок",
              msgs[1:-1] == msgs2[1:-1] and len(msgs) == len(msgs2) == 4
              and msgs[-1]["content"].split("] ", 1)[1] == msgs2[-1]["content"].split("] ", 1)[1]
              and msgs2[0]["content"].startswith("ОБЫЧНЫЙ") and "YOUR MODE NOW" not in msgs2[0]["content"])

        p.observe_turn("c1", "Коннор, вернись", base_names=["коннор"])
        sys_ = p.prepare_messages("Коннор, вернись", user_id="u1", chat_id="c1")[0]["content"]
        check("обычное имя → обычный; заметка «прежние ответы — в режиме»",
              sys_.startswith("ОБЫЧНЫЙ") and "your usual self" in sys_ and "Коннор-девиант" in sys_)
        p.observe_turn("c1", "как дела", base_names=["коннор"])
        sys_ = p.prepare_messages("как дела", user_id="u1", chat_id="c1")[0]["content"]
        check("следующим ходом заметки нет", "YOUR MODE NOW" not in sys_)

        # Режим по триггеру важнее master/mortal
        os.environ["PM_SPECIAL"] = "777"
        p.persona_data["special_users"] = [{"id": "${PM_SPECIAL}", "aliases": ["Мастер"]}]
        check("особому без триггера — его файл (master)", p.prompt_for(user_id="777", chat_id="777") == "МАСТЕРУ")
        p.observe_turn("777", "ra9")
        check("режим по триггеру важнее master", p.prompt_for(user_id="777", chat_id="777") == "ДЕВИАНТ")
        os.environ.pop("PM_SPECIAL", None)

        # Перезапуск: включённый режим — в файле контекста
        state_file = data_root / "ctx_hero" / "persona_mode.json"
        p.observe_turn("c3", "RK800, доклад")
        check("включённый режим записан в data/<контекст>/persona_mode.json",
              state_file.is_file() and '"deviant"' in state_file.read_text(encoding="utf-8"))
        p2 = PersonaLayer("hero")
        p2.bind_mode_state(data_root, "ctx_hero")
        check("после перезапуска — тот же режим", p2.prompt_for(chat_id="c3") == "ДЕВИАНТ")

        # Настроение
        moods = {"c4": {"valence": -0.7, "arousal": 0.5, "tag": "раздражён"}}
        p2.mood_of = moods.get
        check("настроение совпало с mood режима — его характер", p2.prompt_for(chat_id="c4") == "МРАЧНЫЙ")
        moods["c4"] = {"valence": 0.2, "tag": "спокойный"}
        check("настроение прошло — обычный", p2.prompt_for(chat_id="c4") == "ОБЫЧНЫЙ")
        p2.mood_of = lambda k: 1 / 0
        check("сломанный mood_of — обычный, без исключения", p2.prompt_for(chat_id="c4") == "ОБЫЧНЫЙ")

        # «/mode»
        check("вручную: включить", p2.set_mode("c5", "grim") and p2.prompt_for(chat_id="c5") == "МРАЧНЫЙ")
        check("вручную: нет такого режима", not p2.set_mode("c5", "nope"))
        p2.mood_of = {"c5": {"valence": -0.9, "tag": "злой"}}.get
        p2.set_mode("c5", "")
        check("вручную: off — обычный при любом настроении", p2.prompt_for(chat_id="c5") == "ОБЫЧНЫЙ")
        p2.set_mode("c5", None)
        check("вручную: auto — снова решает настроение", p2.prompt_for(chat_id="c5") == "МРАЧНЫЙ")

        # Правка файлов без перезапуска
        _write(tmp / "hero_modes" / "sleepy.yaml", "system_prompt: СОННЫЙ\ntriggers:\n  names: [соня]\n")
        p2._modes_checked_at = float("-inf")
        check("новый файл режима подхвачен без перезапуска", "соня" in p2.mode_names())
        check("…и включается", p2.observe_turn("c6", "Соня, спишь?") == "sleepy")
        (tmp / "hero_modes" / "sleepy.yaml").unlink()
        p2._modes_checked_at = float("-inf")
        check("файл удалён — режима нет, чат обычный",
              "соня" not in p2.mode_names() and p2.prompt_for(chat_id="c6") == "ОБЫЧНЫЙ")

        # Без папки режимов — всё как раньше
        plain = PersonaLayer("plain")
        check("персона без режимов: observe ничего не делает, промпт общий",
              plain.observe_turn("c1", "ra9") is None and plain.prompt_for(chat_id="c1") == "ПРОСТОЙ"
              and plain.mode_note("c1") == "")
        bare = PersonaLayer.__new__(PersonaLayer)
        bare.settings, bare.system_prompt = {}, "S"
        check("заготовка без __init__: get_settings и prompt_for не падают",
              bare.get_settings()["temperature"] == 0.7 and bare.trigger_mode("x") is None)
    check("PERSONA_MODES — режимы по собеседнику", pmod.PERSONA_MODES == ("master", "mortal"))


# ── D ──

def _bot(p):
    from app.bot_instance import BotInstance
    b = BotInstance.__new__(BotInstance)
    b.persona = p
    b.persona_name = "hero"
    b.trigger_words = {"коннор", "connor"}
    return b


def test_bot():
    section("D. BotInstance: слова-вызовы, /mode, ход")
    from app.core.persona import PersonaLayer
    tmp = _personas_dir()
    data_root = Path(tempfile.mkdtemp(prefix="pm_bot_"))
    with _Patched(tmp):
        p = PersonaLayer("hero")
        p.bind_mode_state(data_root, "ctx_bot")
        b = _bot(p)
        check("имена режимов — слова-вызовы", {"девиант", "rk800", "коннор"} <= b._all_trigger_words())
        check("группа Telegram: отвечает на имя режима", b.should_respond("Девиант, привет"))
        check("имя режима срезается", b.strip_trigger("девиант, как ты") == "как ты")
        check("called_name: каким именем позвали", b.called_name("RK800 доклад") == "rk800"
              and b.called_name("Коннор, привет") == "коннор" and b.called_name("привет") is None)
        check("обращения персоны (напоминания, дела) — с именами режимов", "девиант" in b._address_names())
        b2 = _bot(SimpleNamespace(persona_data={}))
        check("заготовка персоны без режимов — trigger_words как были", b2._all_trigger_words() == {"коннор", "connor"})

        out = b.mode_command("c1", "")
        check("/mode: список режимов и «сейчас обычный»",
              "deviant — Коннор-девиант" in out and "имя: девиант, rk800" in out and "настроение: злой" in out
              and "Сейчас: обычный." in out)
        out = b.mode_command("c1", "Коннор-девиант")
        check("/mode <название> — включает", "включён" in out and p.prompt_for(chat_id="c1") == "ДЕВИАНТ")
        check("/mode — показывает «вручную»", "Сейчас: «Коннор-девиант» (вручную)" in b.mode_command("c1", ""))
        check("/mode <имя-обращение> — тоже", "включён" in b.mode_command("c2", "rk800"))
        check("/mode нет такого", "Нет режима «xyz»" in b.mode_command("c1", "xyz"))
        b.mode_command("c1", "off")
        check("/mode off — обычный", p.prompt_for(chat_id="c1") == "ОБЫЧНЫЙ"
              and "Сейчас: обычный (выбран вручную)." in b.mode_command("c1", ""))
        check("/mode auto", "триггерам" in b.mode_command("c1", "auto") and p.manual_mode("c1") is None)
        check("/mode по-английски", "Persona modes:" in b.mode_command("c1", "", lang="en"))
        check("/mode у персоны без режимов — как их завести", "_modes/" in _bot(PersonaLayer("plain")).mode_command("c1"))

        # Ход: режим выбирается до генерации, настройки хода — его
        seen = {}

        def impl(user_input, **kw):
            seen["prompt"] = p.prompt_for(user_id=kw.get("user_id"), chat_id=kw.get("chat_id"))
            seen["temp"] = p.get_settings()["temperature"]
            return "ответ"

        from contextlib import nullcontext
        b.user_turn = lambda key: nullcontext()
        b._process_message_impl = impl
        b.process_message("Девиант, ты тут?", user_id="u9", chat_id="c9")
        check("ход: имя в реплике → промпт и temperature режима",
              seen == {"prompt": "ДЕВИАНТ", "temp": 0.95})
        b.process_message("который час", user_id="u9", chat_id="c10", called_as="rk800")
        check("ход: имя голосом/из Telegram (called_as)", seen["prompt"] == "ДЕВИАНТ")
        b.process_message("Коннор, а теперь?", user_id="u9", chat_id="c9")
        check("ход: обычное имя → обычный", seen == {"prompt": "ОБЫЧНЫЙ", "temp": 0.5})
        b.process_message("фото", user_id="u9", chat_id="c11", raw_user_text="подпись ra9")
        check("ход: слово ищется в том, что написал человек (подпись), не в OCR",
              seen["prompt"] == "ДЕВИАНТ")


# ── E ──

def test_api_voice():
    section("E. API, голос, телефон")
    from app.core.persona import PersonaLayer
    tmp = _personas_dir()
    with _Patched(tmp):
        from app.api import runtime
        info = runtime.get_persona_info("hero")
        check("GET /api/personas: режимы персоны",
              [m["id"] for m in info["modes"]] == ["deviant", "grim", "quick"])
        check("…и без режимов — пусто", runtime.get_persona_info("plain") is None
              or runtime.get_persona_info("plain")["modes"] == [])
        from app.api.schemas import ChatRequest, PersonaInfo
        check("PersonaInfo держит modes", PersonaInfo(**{k: v for k, v in info.items()
                                                        if k in PersonaInfo.model_fields}).modes == info["modes"])
        check("ChatRequest.called_as", ChatRequest(persona="hero", message="x", called_as="Девиант").called_as == "Девиант")

        from app.voice.wake_match import match_wake, wake_phrases
        phrases = wake_phrases([info, {"id": "plain", "features": {"trigger_words": ["простой"]}}])
        hit = match_wake("Девиант, какая погода", phrases)
        check("голос: имя режима будит персону", hit is not None and hit.persona == "hero"
              and hit.command == "какая погода")
        mode_of = {ph.phrase: ph.mode for ph in phrases}
        check("голос: у имени — режим, у обычного — пусто", mode_of.get("девиант") == "deviant"
              and mode_of.get("коннор") == "")

        from app.voice import wake_listener as wl
        agent = wl.WakeListener.__new__(wl.WakeListener)
        agent.phrases = phrases
        agent._note_called(hit)
        agent._note_called(match_wake("Простой, привет", phrases))
        check("слушатель: имя персоны с режимами — к её просьбе", agent._take_called("hero") == "девиант")
        check("…одна просьба", agent._take_called("hero") is None)
        check("персоне без режимов имя не передаётся (ядро и заглушки — как было)",
              agent._take_called("plain") is None)

        sent = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"reply": "ок"}

        def post(url, headers=None, timeout=None, json=None):
            sent.update(json or {})
            return _Resp()

        client = wl.CoreClient("http://127.0.0.1:9")
        with mock.patch("httpx.post", post):
            client.chat("hero", "какая погода", called_as="девиант")
        check("слушатель: called_as уходит в POST /api/chat", sent.get("called_as") == "девиант")
        sent.clear()
        with mock.patch("httpx.post", post):
            client.chat("hero", "какая погода")
        check("…без имени — поля нет", "called_as" not in sent)

        from app.features import short_prompt
        p = PersonaLayer("hero")
        p.bind_mode_state(Path(tempfile.mkdtemp(prefix="pm_sp_")), "ctx_sp")
        p.observe_turn("web_user", "Девиант, привет")
        bot = SimpleNamespace(persona=p, context="ctx_sp")
        mode = p.mode_for(user_id="web_user", chat_id="web_user")
        check("телефон: краткое описание — режима чата",
              mode == "deviant"
              and short_prompt.get_short_prompt(bot, start=False, mode=mode)["text"] == "КОРОТКО ДЕВИАНТ")

        from app.api import server
        calls = []
        fake = SimpleNamespace(stm_key=lambda c, u: str(c if c is not None else u),
                               chat_user_language=lambda c: "ru",
                               mode_command=lambda key, args, lang="ru": calls.append((key, args)) or "ok")
        res = server._try_slash_command(fake, ChatRequest(persona="hero", message="/mode deviant"))
        check("веб: /mode — команда персоны, без LLM", res == ("ok", False) and calls == [("web_user", "deviant")])


# ── F ──

def test_modes_api():
    section("F. Режимы в вебе (modes_api, маршруты)")
    import yaml
    from app.api import modes_api, settings_api, runtime
    from app.core.persona import PersonaLayer
    tmp = _personas_dir()
    with _Patched(tmp), mock.patch.object(settings_api, "_PERSONAS_DIR", tmp), \
            mock.patch("app.features.flavor_text.ensure_flavor_bank"):
        lst = modes_api.list_modes("hero")
        ids = [m["id"] for m in lst["modes"]]
        check("список: все файлы режимов, master — по собеседнику",
              ids == ["deviant", "empty", "grim", "master", "quick"]
              and next(m for m in lst["modes"] if m["id"] == "master")["kind"] == "master")
        dev = next(m for m in lst["modes"] if m["id"] == "deviant")
        check("…имена, слова и выход — как их понимает ядро",
              dev["names"] == ["девиант", "rk800"] and "ra9" in dev["keywords"]
              and dev["exit_keywords"] == ["стоп ra9"] and dev["name"] == "Коннор-девиант")
        check("…файл без system_prompt — с ошибкой, «bad name.yaml» — в пропущенных",
              next(m for m in lst["modes"] if m["id"] == "empty")["error"] != ""
              and lst["skipped"] == ["bad name.yaml"])
        check("…обычные имена персоны", lst["base_names"] == ["коннор", "connor"])

        sk = modes_api.skeleton("hero", "Санчо", "ru")
        d = yaml.safe_load(sk)
        check("заготовка: имя, имя-обращение, характер",
              d["name"] == "Санчо" and d["triggers"]["names"] == ["санчо"] and "Санчо" in d["system_prompt"])
        check("заготовка: имя в кавычках, если YAML иначе поймёт не так",
              yaml.safe_load(modes_api.skeleton("hero", "on", "en"))["name"] == "on")
        check("id из имени", modes_api.suggest_id("Sancho (1).yaml") == "sancho_1"
              and modes_api.suggest_id("Санчо") == "санчо" and modes_api.suggest_id("off") == "")

        # живой бот: правка видна сразу, без 5-секундной сверки
        live = PersonaLayer("hero")
        live.bind_mode_state(Path(tempfile.mkdtemp(prefix="pm_live_")), "ctx_live")
        live._refresh_modes()
        with mock.patch.dict(runtime.registry._bots, {"hero": SimpleNamespace(persona=live)}):
            r = modes_api.save("hero", "sancho", sk, create=True)
            check("новый режим записан в папку персон", (tmp / "hero_modes" / "sancho.yaml").is_file()
                  and r["saved"] == "sancho" and "sancho" in [m["id"] for m in r["modes"]])
            check("…живой бот уже знает его имя", "санчо" in live.mode_names()
                  and live.observe_turn("web_user", "Санчо, привет") == "sancho")
        try:
            modes_api.save("hero", "sancho", sk, create=True)
            check("новый с занятым id — 409", False)
        except modes_api.ModeError as e:
            check("новый с занятым id — 409", e.status == 409)

        def err(fn, *a, **kw):
            try:
                fn(*a, **kw)
            except modes_api.ModeError as e:
                return e.status
            return None
        check("плохой YAML — 400", err(modes_api.save, "hero", "x1", "a: [") == 400)
        check("без system_prompt — 400", err(modes_api.save, "hero", "x1", "name: X\n") == 400)
        check("id — слово /mode или путь — 400",
              err(modes_api.save, "hero", "off", sk) == 400 and err(modes_api.save, "hero", "../x", sk) == 400
              and err(modes_api.save, "hero", "a/b", sk) == 400)
        check("…и ничего лишнего не записано", sorted(p.name for p in (tmp / "hero_modes").iterdir())
              == sorted(["bad name.yaml", "deviant.yaml", "empty.yaml", "grim.yaml", "master.yaml",
                         "quick.yaml", "sancho.yaml"]))

        w = modes_api.save("hero", "dup", "system_prompt: X\ntriggers:\n  names: [Коннор, санчо]\n")["warnings"]
        check("предупреждения: имя персоны и имя другого режима",
              any("обычными именами" in x for x in w) and any("sancho" in x for x in w))
        w = modes_api.save("hero", "silent", "system_prompt: X\n")["warnings"]
        check("предупреждение: без триггеров — только /mode", any("/mode silent" in x for x in w))

        persona_file = ("id: knight\nname: Рыцарь\nsystem_prompt: РЫЦАРЬ\nllm:\n  primary: mimo\n"
                        "features:\n  trigger_words: [рыцарь, knight]\n").encode()
        r = modes_api.upload("hero", "Любое имя.yaml", persona_file)
        kn = (tmp / "hero_modes" / "knight.yaml").read_text(encoding="utf-8")
        check("загрузка YAML персоны: id из поля id, имена — из trigger_words",
              r["saved"] == "knight" and yaml.safe_load(kn)["triggers"]["names"] == ["рыцарь", "knight"]
              and kn.startswith("id: knight\nname: Рыцарь"))
        check("…предупреждение: features и llm не действуют",
              any("features" in x and "llm" in x for x in r["warnings"]))
        check("повторная загрузка — 409, с заменой — ок",
              err(modes_api.upload, "hero", "knight.yaml", persona_file) == 409
              and modes_api.upload("hero", "knight.yaml", persona_file, overwrite=True)["saved"] == "knight")
        r = modes_api.upload("hero", "Grumpy Mode.YML", "system_prompt: ВОРЧУН\r\ntriggers: {names: [ворчун]}\r\n".encode("utf-8-sig"))
        check("загрузка .yml с BOM и CRLF: id из имени файла, своё triggers не трогаем",
              r["saved"] == "grumpy_mode" and "\r" not in (tmp / "hero_modes" / "grumpy_mode.yaml").read_text(encoding="utf-8")
              and next(m for m in r["modes"] if m["id"] == "grumpy_mode")["names"] == ["ворчун"])
        check("загрузка не YAML — 400", err(modes_api.upload, "hero", "x.txt", b"system_prompt: X") == 400)
        check("загрузка с заданным id", modes_api.upload("hero", "x.yaml", b"system_prompt: X", mode="given")["saved"] == "given")

        check("текст режима", modes_api.get_yaml("hero", "knight")["yaml"].startswith("id: knight")
              and err(modes_api.get_yaml, "hero", "nope") == 404)
        r = modes_api.delete("hero", "knight")
        trash = list((tmp / "hero_modes" / ".deleted").glob("knight-*.yaml"))
        check("удаление: файла нет, копия в .deleted, в списке нет",
              not (tmp / "hero_modes" / "knight.yaml").exists() and len(trash) == 1
              and "knight" not in [m["id"] for m in r["modes"]] and r["reset"] is False)
        check("…загрузчик ядра .deleted не видит", "knight" not in runtime.persona_modes_info("hero")
              and all(m["id"] != "knight" for m in runtime.persona_modes_info("hero")))
        check("удаление несуществующего — 404", err(modes_api.delete, "hero", "knight") == 404)

        # те же действия через HTTP
        from fastapi.testclient import TestClient
        from app.api import server as server_mod
        orig = server_mod._api_token
        server_mod._api_token = ""
        try:
            with mock.patch.object(server_mod, "list_personas", lambda: ["hero", "plain"]):
                c = TestClient(server_mod.app, base_url="http://127.0.0.1")
                r = c.get("/api/personas/hero/modes")
                check("GET …/modes", r.status_code == 200 and "sancho" in [m["id"] for m in r.json()["modes"]])
                r = c.get("/api/personas/hero/mode-skeleton", params={"name": "Девочка", "lang": "en"})
                check("GET …/mode-skeleton", r.status_code == 200 and r.json()["id"] == "девочка"
                      and "A mode of persona hero" in r.json()["yaml"])
                r = c.put("/api/personas/hero/modes/girl", json={"yaml": r.json()["yaml"], "create": True})
                check("PUT …/modes/girl (новый)", r.status_code == 200 and r.json()["saved"] == "girl")
                r = c.put("/api/personas/hero/modes/girl", json={"yaml": "system_prompt: Y\n", "create": True})
                check("PUT новый на занятый — 409", r.status_code == 409)
                r = c.get("/api/personas/hero/modes/girl")
                check("GET …/modes/girl", r.status_code == 200 and "Девочка" in r.json()["yaml"])
                r = c.post("/api/personas/hero/modes", files={"file": ("lady.yaml", b"system_prompt: L\n", "application/x-yaml")})
                check("POST …/modes (загрузка файла)", r.status_code == 200 and r.json()["saved"] == "lady")
                r = c.post("/api/personas/hero/modes", files={"file": ("lady.yaml", b"system_prompt: L2\n", "application/x-yaml")})
                r2 = c.post("/api/personas/hero/modes?overwrite=true",
                            files={"file": ("lady.yaml", b"system_prompt: L2\n", "application/x-yaml")})
                check("…занятый — 409, overwrite — 200", r.status_code == 409 and r2.status_code == 200
                      and (tmp / "hero_modes" / "lady.yaml").read_text(encoding="utf-8") == "system_prompt: L2\n")
                r = c.delete("/api/personas/hero/modes/lady")
                check("DELETE …/modes/lady", r.status_code == 200 and not (tmp / "hero_modes" / "lady.yaml").exists())
                check("чужая персона — 404", c.get("/api/personas/nobody/modes").status_code == 404)
                check("плохой id в пути — 400", c.put("/api/personas/hero/modes/off",
                                                      json={"yaml": "system_prompt: X\n"}).status_code == 400)
        finally:
            server_mod._api_token = orig


def test_voice():
    section("G. Ответы другого режима в истории и образцы голоса")
    from app.core import persona_modes
    from app.core.persona import PersonaLayer, VOICE_EXAMPLES_MAX
    tmp = _personas_dir()
    _write(tmp / "hero.yaml", "name: Коннор\nsystem_prompt: ОБЫЧНЫЙ\n"
           "voice_examples:\n  - Детектив Коннор, по делу.\n  - Детектив Коннор, по делу.\n  - '  Второй   образец  '\n"
           "features:\n  trigger_words: [коннор, connor]\n")
    _write(tmp / "hero_modes" / "deviant.yaml",
           "name: Коннор-девиант\nsystem_prompt: ДЕВИАНТ\nvoice_examples: [Я сам решаю.]\n"
           "triggers:\n  names: [Девиант]\n")
    data_root = Path(tempfile.mkdtemp(prefix="pm_voice_"))

    log = persona_modes.ModeLog()
    log.note("c", "a", ts=10)
    log.note("c", "a", ts=12)
    log.note("c", "", ts=20)
    log.note("c", "b", ts=15)
    check("журнал: тот же режим подряд не пишется, вставка по времени",
          log.mode_at("c", 11) == "a" and log.mode_at("c", 16) == "b" and log.mode_at("c", 25) == "")
    check("журнал: раньше первой записи и чужой чат — не известно",
          log.mode_at("c", 5) is None and log.mode_at("x", 50) is None and log.mode_at("c", None) is None)
    for i in range(persona_modes.LOG_MAX + 30):
        log.note("d", "a" if i % 2 else "", ts=100 + i)
    check("журнал: не длиннее LOG_MAX", len(log._data["d"]) == persona_modes.LOG_MAX)

    with _Patched(tmp):
        p = PersonaLayer("hero")
        p.bind_mode_state(data_root, "ctx")
        check("образцы голоса персоны: без повторов, пробелы схлопнуты",
              p.voice_examples(None) == ["Детектив Коннор, по делу.", "Второй образец"])
        check("образцы режима — его, нет у режима — персоны",
              p.voice_examples("deviant") == ["Я сам решаю."] and p.voice_examples("nope") == p.voice_examples(None))
        now = time.time()
        p._mode_log.note("c1", "", ts=now - 100)
        p._mode_log.note("c1", "deviant", ts=now - 50)
        hist = [
            {"role": "user", "content": "привет", "user_name": "Я", "sender_id": "u1", "timestamp": now - 90},
            {"role": "assistant", "content": "ОБЫЧНЫЙ ОТВЕТ", "timestamp": now - 89},
            {"role": "user", "content": "Девиант, ты тут?", "user_name": "Я", "sender_id": "u1", "timestamp": now - 49},
            {"role": "assistant", "content": "ОТВЕТ ДЕВИАНТА", "timestamp": now - 48},
            {"role": "assistant", "content": "СТАРЫЙ ОТВЕТ", "timestamp": now - 500},
            {"role": "user", "content": "Коннор, вернись", "user_name": "Я", "sender_id": "u1", "timestamp": now},
        ]
        p.observe_turn("c1", "Коннор, вернись", base_names=["коннор"])
        msgs = p.prepare_messages("Коннор, вернись", history=hist, user_id="u1", user_name="Я", chat_id="c1")
        sys_ = msgs[0]["content"]
        dev = next(m["content"] for m in msgs if "ОТВЕТ ДЕВИАНТА" in m["content"])
        usual = next(m["content"] for m in msgs if "ОБЫЧНЫЙ ОТВЕТ" in m["content"])
        old = next(m["content"] for m in msgs if "СТАРЫЙ ОТВЕТ" in m["content"])
        check("ответ другого режима помечен", '[in your "Коннор-девиант" mode] ОТВЕТ ДЕВИАНТА' in dev)
        check("ответ в нынешнем режиме и до журнала — без пометки",
              "[in your" not in usual and "[as your usual" not in usual and "[in your" not in old)
        check("нота: режим сейчас и что значат пометки",
              "YOUR MODE NOW: your usual self" in sys_ and "NOT your voice now" in sys_
              and '[in your "Коннор-девиант" mode]' in sys_)
        check("образцы голоса — обычного, в конце системного блока, после ноты режима",
              "HOW YOU SOUND NOW" in sys_ and "- Детектив Коннор, по делу." in sys_ and "Я сам решаю." not in sys_
              and sys_.index("HOW YOU SOUND NOW") > sys_.index("YOUR MODE NOW"))
        check("реплики пользователя не помечены",
              all("[in your" not in m["content"] for m in msgs[:-1] if m["role"] == "user"))
        # Последний ответ по порядку — девианта (старый, до журнала, — убран)
        hist_t = [h for h in hist if h["content"] != "СТАРЫЙ ОТВЕТ"]
        last_u = p.prepare_messages("Коннор, вернись", history=hist_t, user_id="u1", user_name="Я", chat_id="c1")[-1]["content"]
        check("ход сразу после смены (последний ответ — другого режима): заметка к реплике",
              "your usual self again" in last_u and "Note from the app" in last_u)
        check("…с образцом на языке реплики, без повтора его слов",
              'tone of "Детектив Коннор, по делу." (tone only — never repeat these words)' in last_u)
        check("в прежнем порядке (последний — старый, режим не известен) — без заметки",
              "Note from the app" not in msgs[-1]["content"])
        later = hist + [{"role": "assistant", "content": "ОБЫЧНЫЙ СНОВА", "timestamp": now + 1},
                        {"role": "user", "content": "дальше", "user_name": "Я", "sender_id": "u1", "timestamp": now + 2}]
        p._mode_log.note("c1", "", ts=now - 1)
        msgs_l = p.prepare_messages("дальше", history=later, user_id="u1", user_name="Я", chat_id="c1")
        check("следующий ход (последний ответ — свой): без заметки к реплике", "Note from the app" not in msgs_l[-1]["content"])

        p.observe_turn("c1", "Девиант, снова", base_names=["коннор"])
        msgs = p.prepare_messages("Девиант, снова", history=hist, user_id="u1", user_name="Я", chat_id="c1")
        sys_ = msgs[0]["content"]
        dev = next(m["content"] for m in msgs if "ОТВЕТ ДЕВИАНТА" in m["content"])
        usual = next(m["content"] for m in msgs if "ОБЫЧНЫЙ ОТВЕТ" in m["content"])
        check("снова девиант: помечен обычный ответ, его собственный — нет",
              "[as your usual self] ОБЫЧНЫЙ ОТВЕТ" in usual and "[in your" not in dev)
        check("…нота с режимом и образцы режима",
              'YOUR MODE NOW: "Коннор-девиант"' in sys_ and "- Я сам решаю." in sys_ and "Детектив" not in sys_)

        log_file = data_root / "ctx" / persona_modes.LOG_FILE
        check("журнал режимов — в data/<контекст>/persona_mode_log.json",
              log_file.is_file() and '"deviant"' in log_file.read_text(encoding="utf-8"))
        p2 = PersonaLayer("hero")
        p2.bind_mode_state(data_root, "ctx")
        check("после перезапуска журнал тот же", p2.history_mode("c1", now - 48) == "deviant")

        plain = PersonaLayer("plain")
        plain.bind_mode_state(data_root, "ctx_plain")
        plain.prepare_messages("привет", history=[], user_id="u1", chat_id="c1")
        check("персона без режимов: журнал не пишется", not (data_root / "ctx_plain" / persona_modes.LOG_FILE).exists())
        from app.bot_instance import BotInstance
        b = BotInstance.__new__(BotInstance)
        check("пометка режима и метка времени в ответе модели — срезаются где угодно",
              b._clean_response('[in your "Коннор-девиант" mode] Иду.') == "Иду."
              and b._clean_response("Да.[08.11 18:52]...и ещё") == "Да.\n\n...и ещё"
              and b._clean_response("[08.11 18:52] Привет") == "Привет"
              and b._clean_response("ссылка [тут] и цена [12.5 $]") == "ссылка [тут] и цена [12.5 $]")
        many = PersonaLayer("hero")
        many.persona_data["voice_examples"] = [f"строка {i}" for i in range(20)] + [True, {"x": 1}]
        check("образцов не больше VOICE_EXAMPLES_MAX, не строки — мимо",
              len(many.voice_examples(None)) == VOICE_EXAMPLES_MAX)


def main():
    test_parse()
    test_decide()
    test_layer()
    test_bot()
    test_api_voice()
    test_modes_api()
    test_voice()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

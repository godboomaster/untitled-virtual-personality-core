"""Режимы персоны на телефоне без ядра: пометки ответов другого режима и
образцы голоса — как в промпте ядра.

  A. Кит (offline_api.build_kit): режим чата, журнал режимов, пометки по
     режимам, строка «режим сейчас», правило к пометкам, заметка без них,
     образцы голоса режима; у персоны без режимов — modes null.
  B. Телефон (web/src/offline/modeTags.ts под Node, кит — настоящий, от
     ядра): какие ответы помечены и какими пометками — ровно как в
     PersonaLayer.prepare_messages; заметка и образцы (не больше voiceMax);
     без режимов — ничего.
  C. Офлайн-переписка обратно (import_messages): режим, которым ответил
     телефон, — в журнал режимов ядра; схема принимает mode, плохой — нет.

LLM и сеть не вызываются, data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_offline_modes
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="offline_modes_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

OFFLINE = Path(__file__).parent.parent / "web" / "src" / "offline"
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


PERSONAS = Path(tempfile.mkdtemp(prefix="om_personas_"))
_write(PERSONAS / "hero.yaml", "name: Дон\nsystem_prompt: ОБЫЧНЫЙ\nvoice_examples: ['Внемли, добрый друг!']\n"
       "features:\n  trigger_words: [дон]\n")
_write(PERSONAS / "hero_modes" / "squire.yaml",
       "name: Санчо\nsystem_prompt: САНЧО\nvoice_examples: [Ладно., Без речей., Почти., Четыре века.]\n"
       "triggers:\n  names: [санчо]\n")
_write(PERSONAS / "plain.yaml", "name: Простой\nsystem_prompt: ПРОСТОЙ\n")


def _find(n):
    p = PERSONAS / f"{n}.yaml"
    return p if p.exists() else None


for _p in (mock.patch("app.core.persona.persona_dirs", lambda: [PERSONAS]),
           mock.patch("app.core.persona.find_persona_file", _find),
           mock.patch("app.api.runtime.persona_dirs", lambda: [PERSONAS])):
    _p.start()

NOW = time.time()
STM = [
    {"role": "user", "content": "привет", "timestamp": NOW - 400},
    {"role": "assistant", "content": "Внемли же!", "timestamp": NOW - 390},
    {"role": "user", "content": "Санчо?", "timestamp": NOW - 300},
    {"role": "assistant", "content": "Здесь.", "timestamp": NOW - 290},
    {"role": "user", "content": "как дела", "timestamp": NOW - 200},
    {"role": "assistant", "content": "Терпимо.", "timestamp": NOW - 190},
    {"role": "assistant", "content": "До журнала.", "timestamp": NOW - 9000},
]


def fake_bot(layer, context="api_hero"):
    calls = []

    def add_message(role, content, user_id="default", chat_id=None, user_name=None, timestamp=None, bulk=False):
        calls.append((role, content, timestamp))

    bot = SimpleNamespace(
        context=context, persona=layer, calls=calls,
        conversation_style=SimpleNamespace(frequency="rare"),
        memory=SimpleNamespace(ltm=SimpleNamespace(get_all_facts=lambda **k: []),
                               stm=SimpleNamespace(get_messages=lambda chat_id=None: list(STM)),
                               add_message=add_message),
        reminder_manager=None, self_memory=None,
        stm_key=lambda chat_id, user_id: str(chat_id if chat_id is not None else user_id),
        chat_user_language=lambda key: "ru",
        _get_dossier_context_line=lambda chat_id, user_id: "",
    )

    class Turn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    bot.user_turn = lambda key: Turn()
    return bot


def layer(name="hero", ctx="api_hero"):
    from app.core.paths import data_dir
    from app.core.persona import PersonaLayer
    p = PersonaLayer(name)
    p.bind_mode_state(data_dir(), ctx)
    return p


def test_kit():
    section("A. Кит для телефона")
    from app.api import offline_api
    p = layer()
    p._mode_log.note("web_user", "", ts=NOW - 500)
    p._mode_log.note("web_user", "squire", ts=NOW - 295)
    p._mode_log.note("web_user", "", ts=NOW - 195)
    kit = offline_api.build_kit(fake_bot(p), "hero", "web_user", "web_user")
    m = kit.get("modes") or {}
    check("режим чата сейчас — обычный", m.get("current") == "")
    check("журнал режимов — по времени", [r[1] for r in m.get("log", [])] == ["", "squire", ""])
    check("пометки по режимам", m.get("tags", {}).get("") == "[as your usual self]"
          and m.get("tags", {}).get("squire") == '[in your "Санчо" mode]')
    check("строка «режим сейчас» и правило к пометкам",
          m.get("now", "").startswith("YOUR MODE NOW: your usual self") and "{tags}" in m.get("tagged", ""))
    check("образцы голоса обычного и заметка к ним",
          kit.get("voice", {}).get("lines") == ["Внемли, добрый друг!"] and "{lines}" in kit["voice"]["note"])
    p.set_mode("web_user", "squire")
    kit2 = offline_api.build_kit(fake_bot(p), "hero", "web_user", "web_user")
    check("режим включён — в ките он, его образцы и запись в журнале",
          kit2["modes"]["current"] == "squire" and kit2["voice"]["lines"][0] == "Ладно."
          and kit2["modes"]["log"][-1][1] == "squire" and 'YOUR MODE NOW: "Санчо"' in kit2["modes"]["note"])
    p.set_mode("web_user", "")
    plain = offline_api.build_kit(fake_bot(layer("plain", "api_plain"), "api_plain"), "plain", "web_user", "web_user")
    check("персона без режимов — modes null, без образцов — voice null",
          plain.get("modes") is None and plain.get("voice") is None)
    return p


NODE_CODE = """
import { registerHooks } from 'node:module';
registerHooks({
  resolve(spec, ctx, next) {
    try { return next(spec, ctx); }
    catch (e) {
      if (spec.startsWith('.') && !spec.endsWith('.ts')) return next(spec + '.ts', ctx);
      throw e;
    }
  },
});
const mt = await import(%s);
const input = JSON.parse(%s);
const out = input.cases.map((c) => {
  const r = mt.tagHistory(c.kit, c.lines);
  return { lines: r.lines.map((l) => l.text), tags: r.tags, note: mt.modeNote(c.kit, r.tags, c.voiceMax ?? Infinity),
           at: (c.at || []).map((t) => mt.modeAt(c.kit, t)), last: r.lastTagged,
           turn: (c.asks || []).map((q) => mt.withTurnNote(c.kit, r.lastTagged, q)) };
});
console.log(JSON.stringify(out));
"""


def run_node(payload):
    code = NODE_CODE % (json.dumps((OFFLINE / "modeTags.ts").as_uri()), json.dumps(json.dumps(payload, ensure_ascii=False)))
    res = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:600])
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_phone(p):
    section("B. Телефон: пометки как у ядра")
    if not shutil.which("node"):
        print("  (пропущено — нет Node)")
        return
    from app.api import offline_api
    kit = offline_api.build_kit(fake_bot(p), "hero", "web_user", "web_user")
    lines = [{"role": m["role"], "text": m["content"], "ts": m["timestamp"]} for m in STM]
    lines.append({"role": "assistant", "text": "Офлайн, без времени"})
    # Ядро на той же истории: что помечает prepare_messages
    hist = [dict(m) for m in STM] + [{"role": "user", "content": "дальше", "timestamp": NOW}]
    msgs = p.prepare_messages("дальше", history=hist, user_id="web_user", chat_id="web_user")
    core_tagged = sorted(m["content"].split("] ", 1)[1] for m in msgs[1:-1] if "[in your" in m["content"] or "[as your usual" in m["content"])
    no_modes = dict(kit, modes=None, voice=None)
    out = run_node({"cases": [
        {"kit": kit, "lines": lines, "at": [NOW - 600, NOW - 290, NOW - 100, None]},
        {"kit": kit, "lines": lines, "voiceMax": 0},
        {"kit": no_modes, "lines": lines},
    ]})
    a, b, c = out
    phone_tagged = sorted(t for t in a["lines"] if t.startswith("[in your") or t.startswith("[as your usual"))
    check("помечен ответ Санчо, ответы обычного и без журнала — нет",
          a["lines"][3] == '[in your "Санчо" mode] Здесь.' and a["lines"][1] == "Внемли же!"
          and a["lines"][5] == "Терпимо." and a["lines"][6] == "До журнала." and a["lines"][7] == "Офлайн, без времени")
    check("пометки телефона = пометки ядра на той же истории", phone_tagged == core_tagged and len(core_tagged) == 1)
    check("реплики пользователя не помечены", a["lines"][2] == "Санчо?")
    check("режим по времени: до журнала — null", a["at"] == [None, "squire", "", None])
    check("заметка: режим сейчас, правило с пометками, образцы",
          a["note"].startswith("YOUR MODE NOW: your usual self") and '[in your "Санчо" mode]' in a["note"]
          and "NOT your voice now" in a["note"] and "- Внемли, добрый друг!" in a["note"] and "{tags}" not in a["note"])
    check("voiceMax 0 — без образцов", "HOW YOU SOUND NOW" not in b["note"] and "NOT your voice now" in b["note"])
    check("без режимов в ките — ни пометок, ни заметки", c["tags"] == [] and c["note"] == "" and c["lines"][3] == "Здесь.")
    check("последний ответ — свой: заметки к реплике нет", a["last"] is False)
    after = lines[:4]  # кончается ответом Санчо, а чат уже в обычном режиме
    out = run_node({"cases": [{"kit": kit, "lines": after, "asks": ["как ты?", "how are you?"]}]})[0]
    check("последний ответ — другого режима: заметка к реплике с образцом на её языке",
          out["last"] is True and "your usual self again" in out["turn"][0] and "{like}" not in out["turn"][0]
          and 'tone of "Внемли, добрый друг!" (tone only' in out["turn"][0] and "tone of" not in out["turn"][1]
          and out["turn"][1].startswith("how are you?"))
    p.set_mode("web_user", "squire")
    kit_s = offline_api.build_kit(fake_bot(p), "hero", "web_user", "web_user")
    out = run_node({"cases": [{"kit": kit_s, "lines": lines, "voiceMax": 3}]})[0]
    check("в режиме Санчо помечены ответы обычного, его — нет; образцов не больше 3",
          out["lines"][1] == "[as your usual self] Внемли же!" and out["lines"][3] == "Здесь."
          and out["note"].count("\n- ") == 3 and 'YOUR MODE NOW: "Санчо"' in out["note"])
    p.set_mode("web_user", "")


def test_import(p):
    section("C. Офлайн-переписка обратно на ядро")
    from app.api import offline_api
    from app.api.schemas import OfflineImportRequest
    bot = fake_bot(p)
    # Позже всех записей журнала прошлых шагов (они — по настоящим часам),
    # но не из будущего: такое время импорт сдвигает на «сейчас»
    base = time.time()
    t1, t2 = base + 0.2, base + 0.4
    p._mode_log.note("web_user", "", ts=base)  # ядро было в обычном режиме
    time.sleep(0.6)
    offline_api.import_messages(bot, "web_user", "web_user", [
        {"id": "u1", "role": "user", "text": "Санчо, ты тут?", "ts": t1 - 1, "mode": "squire"},
        {"id": "a1", "role": "assistant", "text": "Тут.", "ts": t1, "mode": "squire"},
        {"id": "a2", "role": "assistant", "text": "Без режима.", "ts": t2, "mode": None},
    ])
    check("ответ телефона в режиме Санчо — в журнале", p.history_mode("web_user", t1) == "squire")
    check("реплика пользователя журнал не трогает", p.history_mode("web_user", t1 - 1) == "")
    check("ответ без режима (старое приложение) — журнал как был", p.history_mode("web_user", t2) == "squire")
    check("сообщения записаны", [c[1] for c in bot.calls] == ["Санчо, ты тут?", "Тут.", "Без режима."])
    ok_req = OfflineImportRequest(messages=[{"id": "x", "role": "assistant", "text": "a", "ts": 1, "mode": "squire"}])
    check("схема: mode принят", ok_req.messages[0].mode == "squire")
    try:
        OfflineImportRequest(messages=[{"id": "x", "role": "assistant", "text": "a", "ts": 1, "mode": "../x"}])
        check("схема: плохой mode — отказ", False)
    except Exception:
        check("схема: плохой mode — отказ", True)
    old = OfflineImportRequest(messages=[{"id": "x", "role": "assistant", "text": "a", "ts": 1}])
    check("схема: без mode (старое приложение) — как было", old.messages[0].mode is None)


if __name__ == "__main__":
    p = test_kit()
    test_phone(p)
    test_import(p)
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    sys.exit(1 if failures else 0)

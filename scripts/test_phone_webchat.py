"""Веб-чаты без ключей на телефоне (web/src/offline/webchatSites.ts,
WebChatPlugin.java, вкладка «Провайдеры»).

Без ядра персоны отвечают через сайты чатов невидимым WebView: облако по
ключу → веб-чаты → Gemma (запасной путь; цепочка персоны — test_phone_chain).
Проверяется:
  1. склейка промпта телефона совпадает с веб-чатами ядра
     (WebChatLLM._join_messages) — прогоном webchatSites.ts под Node;
  2. под лимит поля сайта урезается старая история, а не инструкции и вопрос;
  3. порядок сайтов, по умолчанию всё выключено;
  4. сайты в Java и в TS — одни и те же; у каждого кода отказа есть текст
     на обоих языках; по умолчанию звено веб-чатов — между облаком и Gemma.
Сами сайты (WebView) проверяются вживую на телефоне.

Запуск: PYTHONPATH=. python3 -m scripts.test_phone_webchat
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="phone_webchat_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

SRC = ROOT / "web" / "src"
JAVA = ROOT / "web" / "android" / "app" / "src" / "main" / "java" / "io" / "vpcore" / "app" / "WebChatPlugin.java"

results = {"ok": 0, "fail": 0}


def check(name, cond):
    results["ok" if cond else "fail"] += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


SYSTEM = "You are Connor.\nStay in character."
CASES = [
    [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "Привет"}],
    [{"role": "system", "content": SYSTEM}, {"role": "system", "content": "Facts: likes tea"},
     {"role": "user", "content": "Как дела?"}, {"role": "assistant", "content": "Нормально."},
     {"role": "user", "content": "  А погода?  "}],
    [{"role": "user", "content": "без инструкций"}],
    [{"role": "system", "content": "   "}, {"role": "user", "content": "пустой system"}, {"role": "assistant", "content": ""}],
]

NODE_CODE = r"""
const m = await import(%s);
const cases = JSON.parse(%s);
const out = { join: cases.map((c) => m.joinMessages(c)) };
const long = [
  { role: 'system', content: 'S'.repeat(100) },
  { role: 'user', content: 'old '.repeat(50) },
  { role: 'assistant', content: 'mid '.repeat(50) },
  { role: 'user', content: 'вопрос' },
];
out.fitAll = m.fitPrompt(long, 100000);
out.fitCut = m.fitPrompt(long, 450);
out.fitQuestion = m.fitPrompt(long, 200);
out.fitNone = m.fitPrompt(long, 50);
const empty = { order: [], on: {} };
out.defaultOn = m.enabledSites(empty).map((s) => s.id);
out.defaultOrder = m.orderedSites(empty).map((s) => s.id);
const prefs = { order: ['google', 'bogus', 'google'], on: { chatgpt: true, deepseek: true, google: false } };
out.order = m.orderedSites(prefs).map((s) => s.id);
out.enabled = m.enabledSites(prefs).map((s) => s.id);
out.sites = m.WEBCHAT_SITES.map((s) => ({ id: s.id, account: s.account, login: !!s.login, maxInput: s.maxInput }));
console.log(JSON.stringify(out));
"""


def node_run():
    src = SRC / "offline" / "webchatSites.ts"
    code = NODE_CODE % (json.dumps(src.as_uri()), json.dumps(json.dumps(CASES, ensure_ascii=False)))
    res = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        check(f"webchatSites.ts запускается под Node: {res.stderr.strip()[:600]}", False)
        return None
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_join(got):
    print("\n1. Склейка промпта — как у веб-чатов ядра")
    from app.features.web_llm import WebChatLLM
    for i, (case, have) in enumerate(zip(CASES, got["join"]), 1):
        want = WebChatLLM._join_messages(case)
        check(f"случай {i}: {want[:50]!r}…" + ("" if want == have else f"  (телефон: {have[:80]!r})"), want == have)


def test_fit(got):
    print("\n2. Под поле сайта")
    check("всё влезает — целиком", got["fitAll"] and "old" in got["fitAll"] and got["fitAll"].endswith("User: вопрос"))
    cut = got["fitCut"] or ""
    check("не влезает — старая реплика ушла, средняя и вопрос остались",
          "old" not in cut and "mid" in cut and cut.endswith("User: вопрос") and len(cut) <= 450)
    q = got["fitQuestion"] or ""
    check("влезают только инструкции и вопрос", q.startswith("Instructions") and "mid" not in q
          and q.endswith("User: вопрос") and len(q) <= 200)
    check("инструкции длиннее поля — сайт пропускается (null)", got["fitNone"] is None)


def test_prefs(got):
    print("\n3. Порядок и выбор сайтов")
    check("по умолчанию ни один сайт не включён", got["defaultOn"] == [])
    check("порядок по умолчанию — duck.ai, ChatGPT, Google, DeepSeek, потом сайты ядра",
          got["defaultOrder"] == ["duckai", "chatgpt", "google", "deepseek", "qwen", "kimi", "zai", "claude"])
    check("войти можно в ChatGPT (не обязательно)", [s["id"] for s in got["sites"] if s["login"]] == ["chatgpt"])
    check("сохранённый порядок, чужие id и повторы — мимо, новые — в конец",
          got["order"] == ["google", "duckai", "chatgpt", "deepseek", "qwen", "kimi", "zai", "claude"])
    check("перебор — только включённые, по порядку", got["enabled"] == ["chatgpt", "deepseek"])
    acc = {s["id"]: s["account"] for s in got["sites"]}
    check("без входа — duck.ai, ChatGPT, Google; со входом — DeepSeek и сайты ядра",
          acc == {"duckai": False, "chatgpt": False, "google": False, "deepseek": True,
                  "qwen": True, "kimi": True, "zai": True, "claude": True})
    lim = {s["id"]: s["maxInput"] for s in got["sites"]}
    check("лимит duck.ai — 16 тыс. (как ADAPTERS ядра)", lim["duckai"] == 16000)


def test_static(got):
    print("\n4. Java, тексты, порядок звеньев")
    java = JAVA.read_text(encoding="utf-8")
    cases = re.findall(r'case "(\w+)":', java.split("static JSONObject site(")[1].split("private static final String[] SITES")[0])
    arr = re.search(r'String\[\] SITES = \{([^}]*)\}', java).group(1)
    arr_ids = re.findall(r'"(\w+)"', arr)
    ts_ids = [s["id"] for s in got["sites"]]
    check(f"сайты Java (site) = TS: {cases}", sorted(cases) == sorted(ts_ids))
    check("SITES в Java = TS", sorted(arr_ids) == sorted(ts_ids))
    check("свой профиль WebView «webchat»", 'PROFILE = "webchat"' in java and "WebViewCompat.setProfile" in java)
    check("мостов из страниц в приложение нет", "addJavascriptInterface" not in java)
    check("сайты со входом: лимит новых чатов в день", "NEW_CHATS_PER_DAY" in java and '"budget"' in java)
    # Вкладка без окна не рисует кадров — DeepSeek (виртуальный список) не
    # выводил ответ, ход ждал 90 с и падал в no_answer (31.10)
    check("вкладке без окна подкачиваются кадры, пока идёт ход",
          "t.web.draw(pumpCanvas);" in java and "pumpFor(x, budget);" in java and "pumpFor(x, READY_WAIT_MS + 5000);" in java)
    check("после хода вкладка засыпает (иначе 60 кадров в секунду до закрытия)",
          "t.web.onPause();" in java and "sleep(t);" in java and "t.web.onResume();" in java)
    ds = java.split('case "deepseek":')[1].split("break;")[0]
    check("DeepSeek: конец ответа — кнопки под своим сообщением, не у старых",
          '".ds-message + .ds-flex [role=button]"' in ds and 's.put("doneUp", 2);' in ds and "c.doneUp" in java)
    check("свой чат на персону — у сайтов с адресом чата (ChatGPT — после входа)",
          'String thread = cfg.has("thread") && !fresh' in java and 'if (cfg.has("thread")) {' in java
          and 's.put("thread", "/c/");' in java.split('case "chatgpt":')[1].split("break;")[0])
    check("рассуждения модели в ответ не попадают (exclude)", "function txt(x)" in java and ".map(txt)" in java)
    check("DeepSeek: поиск и глубокое мышление сайта выключаются",
          '".ds-toggle-button[aria-pressed=true]"' in ds and '"modes"' in java)

    wc = (SRC / "offline" / "webchat.ts").read_text(encoding="utf-8")
    reasons = re.findall(r"'(\w+)'", re.search(r"const REASONS = \[([^\]]*)\]", wc).group(1)) + ["failed"]
    java_codes = set(re.findall(r'new Fail\("(\w+)"', java))
    check(f"у каждого отказа Java есть текст: {sorted(java_codes)}", java_codes <= set(reasons))
    for lang in ("ru", "en"):
        text = (SRC / "i18n" / f"{lang}.ts").read_text(encoding="utf-8")
        missing = [r for r in reasons if f"'webchat.state.{r}':" not in text]
        check(f"{lang}: тексты всех состояний сайта", not missing)
        check(f"{lang}: вкладка «Провайдеры» и сообщение «веб-чаты не ответили»",
              f"'nav.providers':" in text and "'offline.webFailed':" in text and "'offline.webFailed.standalone':" in text)

    eng = (SRC / "offline" / "engine.ts").read_text(encoding="utf-8")
    chain = (SRC / "offline" / "personaChain.ts").read_text(encoding="utf-8")
    i_web = eng.find("askWebchats(persona, [cloudMessages(), compactMessages()], false, step.sites)")
    check("запасной путь: облако → веб-чаты → Gemma",
          "[{ kind: 'cloud', only: null }, { kind: 'web', sites: enabledSites(webchatPrefs())" in chain
          and "{ kind: 'gemma' }]" in chain)
    check("веб-чаты — только включённые, и в своём порядке", "prefs.on[s.id] === true" in wc and "if (!step.sites.length) continue;" in eng)
    check("не влез полный промпт облака — сайту короткий промпт Gemma", 0 < i_web and "const local = localParts();" in eng)
    side = (SRC / "components" / "Sidebar.tsx").read_text(encoding="utf-8")
    check("пункт «Провайдеры» в меню — только без связи с ядром", "item.id !== 'providers' || offline" in side)


def main():
    print("Веб-чаты без ключей на телефоне")
    if not shutil.which("node"):
        print("  (пропущено — нет Node)")
        return 0
    got = node_run()
    if got is None:
        return 1
    test_join(got)
    test_fit(got)
    test_prefs(got)
    test_static(got)
    print(f"\nИтого: {results['ok']} OK, {results['fail']} FAIL")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())

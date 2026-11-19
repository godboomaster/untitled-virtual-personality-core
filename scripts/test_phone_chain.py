"""Цепочка провайдеров персоны без ядра (web/src/offline/chainOrder.ts,
personaChain.ts, PhoneChainCard.tsx, OfflineLlmPlugin.java).

Без ядра за персону отвечают облачные провайдеры (ключи с ядра и с
телефона), веб-чаты телефона и Gemma — по цепочке персоны, как на
компьютере: у каждой своя, в досье → «Настройки». Пока на телефоне её не
меняли — она с компьютера (копия llm персоны). Проверяется:
  1. звенья ядра → звенья телефона: веб-чаты и локальная модель ядра —
     звенья ПК (pc:…), не сайты и Gemma телефона; чего нет — мимо;
  2. порядок персоны: основной (её или общий, убрать нельзя), её порядок,
     новые — перед Gemma, если та последняя; убранные — мимо; звенья ПК без
     ядра — всегда в убранных, основным не бывают, не возвращаются;
  3. порядок → шаги ответа (подряд идущие ключи — одним запросом к плагину);
  4. правки в досье: выше/ниже, основной, убрать, вернуть, запись;
  5. статика: плагин берёт только переданных и в их порядке, ключи ядра
     других персон — для исключённых у этой; вебу — маски; движок идёт по
     цепочке персоны; правка цепочки на ядре снимает телефонную; тексты.
Сам ответ (облако, сайты, Gemma) проверяется на телефоне.

Запуск: PYTHONPATH=. python3 -m scripts.test_phone_chain
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "web" / "src"
JAVA = ROOT / "web" / "android" / "app" / "src" / "main" / "java" / "io" / "vpcore" / "app" / "OfflineLlmPlugin.java"

results = {"ok": 0, "fail": 0}


def check(name, cond):
    results["ok" if cond else "fail"] += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


NODE_CODE = r"""
const m = await import(%s);
const out = {};
// Как у Коннора на компьютере (копия на Honor 31.10)
const items = ['mimo', 'kimi', 'zai', 'groq', 'hf', 'webchat:deepseek', 'gemma',
  'pc:webchat:deepseek', 'pc:webchat:qwen', 'pc:webchat:chatgpt', 'pc:webchat:google', 'pc:local'];
const connor = { primary: 'mimo', fallback: ['webchat:deepseek', 'webchat:qwen', 'local', 'openai', 'deepseek', 'webchat:google'],
  exclude: ['webchat:chatgpt', 'kimi', 'zai', 'groq', 'hf', 'webchat:kimi'] };
out.tokens = ['local', 'webchat:deepseek', 'webchat:google_anon', 'mimo', 'openai'].map((t) => m.phoneToken(t, items));
// Основной на ПК — веб-чат ПК (как у Арродеса): без ядра основным не бывает
const arr = m.chainFromCore({ primary: 'webchat:deepseek', fallback: ['webchat:qwen', 'kimi', 'mimo', 'local'], exclude: [] }, 'kimi', items);
out.arrodes = m.normalizeChain(items, arr.active, arr.state);
out.pcActive = m.normalizeChain(['mimo', 'gemma', 'pc:local'], 'pc:local', null);
out.savedPc = m.normalizeChain(items, 'kimi', { primary: 'pc:webchat:qwen', fallback: ['pc:local', 'mimo'], exclude: [] });
out.restorePc = m.restoreIn(['mimo', 'gemma'], ['pc:local', 'kimi'], 'pc:local');
out.toChainPc = m.toChain(['mimo', 'gemma'], ['kimi', 'pc:webchat:qwen', 'pc:local']);
const core = m.chainFromCore(connor, 'kimi', items);
out.core = core;
out.connor = m.normalizeChain(items, core.active, core.state);
const glob = m.chainFromCore(null, 'kimi', items);
out.global = m.normalizeChain(items, glob.active, glob.state);
out.none = m.normalizeChain(['mimo', 'webchat:duckai', 'gemma'], null, null);
out.activeExcluded = m.normalizeChain(items, 'kimi', { primary: null, fallback: [], exclude: ['kimi', 'zai'] });
out.primaryGone = m.normalizeChain(['mimo', 'gemma'], 'mimo', { primary: 'webchat:duckai', fallback: ['gemma'], exclude: [] });
out.newSite = m.normalizeChain(['mimo', 'webchat:duckai', 'webchat:deepseek', 'gemma'], null,
  { primary: 'webchat:deepseek', fallback: ['mimo', 'gemma'], exclude: [] });
out.gemmaFirst = m.normalizeChain(['mimo', 'webchat:duckai', 'gemma'], null, { primary: 'gemma', fallback: ['mimo'], exclude: [] });
out.onlyGemma = m.placeNew(['gemma'], 'mimo');
out.steps = m.chainSteps(['mimo', 'kimi', 'webchat:deepseek', 'gemma']);
out.stepsMixed = m.chainSteps(['webchat:deepseek', 'mimo', 'gemma', 'webchat:google', 'kimi']);
out.stepsJunk = m.chainSteps(['bad token', 'webchat:', 'gemma', 'gemma', 'mimo']);
const o = ['mimo', 'webchat:deepseek', 'kimi', 'gemma'];
out.up = m.moveIn(o, 'kimi', -1);
out.upToMain = m.moveIn(o, 'webchat:deepseek', -1);
out.mainDown = m.moveIn(o, 'mimo', 1);
out.down = m.moveIn(o, 'webchat:deepseek', 1);
out.first = m.makeFirst(o, 'webchat:deepseek');
out.ex = m.excludeIn(o, ['zai'], 'kimi');
out.exMain = m.excludeIn(o, [], 'mimo');
out.back = m.restoreIn(['mimo', 'webchat:deepseek', 'gemma'], ['kimi', 'zai'], 'kimi');
out.chain = m.toChain(['webchat:deepseek', 'mimo', 'gemma'], ['kimi', 'webchat:deepseek']);
out.kinds = ['mimo', 'webchat:duckai', 'gemma', 'pc:webchat:qwen', 'pc:local', 'pc:x', 'webchat:', 'a b'].map((x) => m.kindOf(x));
out.orig = o;
console.log(JSON.stringify(out));
"""


def node_run():
    src = SRC / "offline" / "chainOrder.ts"
    res = subprocess.run(["node", "--input-type=module", "-e", NODE_CODE % json.dumps(src.as_uri())],
                         capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        check(f"chainOrder.ts запускается под Node: {res.stderr.strip()[:600]}", False)
        return None
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_tokens(got):
    print("\n1. Цепочка с компьютера → звенья телефона")
    check("local и веб-чаты ядра — звенья ПК, а не Gemma и сайты телефона; чего нет — мимо",
          got["tokens"] == ["pc:local", "pc:webchat:deepseek", None, "mimo", None])
    check("Коннор: основной Mimo, порядок и убранные — звеньями телефона и ПК", got["core"] == {
        "state": {"primary": "mimo", "fallback": ["pc:webchat:deepseek", "pc:webchat:qwen", "pc:local", "pc:webchat:google"],
                  "exclude": ["pc:webchat:chatgpt", "kimi", "zai", "groq", "hf"]},
        "active": "kimi"})
    check("род звена", got["kinds"] == ["api", "webchat", "gemma", "pc", "pc", None, None, None])


def test_normalize(got):
    print("\n2. Порядок персоны")
    pc_all = ["pc:webchat:deepseek", "pc:webchat:qwen", "pc:webchat:chatgpt", "pc:webchat:google", "pc:local"]
    check("Коннор без ядра: Mimo → DeepSeek телефона → Gemma; Kimi/ZAI/Groq/HF и всё ПК — в убранных",
          got["connor"]["order"] == ["mimo", "webchat:deepseek", "gemma"]
          and sorted(got["connor"]["exclude"]) == sorted(["kimi", "zai", "groq", "hf"] + pc_all))
    check("персона без своего llm — общий основной, дальше по умолчанию, ПК — в убранных",
          got["global"]["order"] == ["kimi", "mimo", "zai", "groq", "hf", "webchat:deepseek", "gemma"]
          and sorted(got["global"]["exclude"]) == sorted(pc_all))
    check("основной на ПК — веб-чат ПК: без ядра основной — общий, сайт ПК в убранных",
          got["arrodes"]["order"][:3] == ["kimi", "mimo", "zai"] and "pc:webchat:deepseek" in got["arrodes"]["exclude"]
          and "webchat:deepseek" in got["arrodes"]["order"])
    check("общий основной — локальная модель ПК: основной — первый телефонный", got["pcActive"] == {
        "order": ["mimo", "gemma"], "exclude": ["pc:local"]})
    check("звено ПК в сохранённой цепочке — всё равно в убранных",
          got["savedPc"]["order"][0] == "kimi" and "pc:local" not in got["savedPc"]["order"]
          and "pc:webchat:qwen" in got["savedPc"]["exclude"])
    check("ни своего, ни общего — первый по умолчанию", got["none"]["order"] == ["mimo", "webchat:duckai", "gemma"])
    check("общий основной не убирается (как на ядре)", got["activeExcluded"] == {
        "order": ["kimi", "mimo", "groq", "hf", "webchat:deepseek", "gemma"], "exclude": ["zai"] + pc_all})
    check("основного больше нет (сайт выключили) — общий", got["primaryGone"]["order"] == ["mimo", "gemma"])
    check("включили сайт — встаёт перед Gemma, а не за ней",
          got["newSite"]["order"] == ["webchat:deepseek", "mimo", "webchat:duckai", "gemma"])
    check("Gemma основной — новое не становится основным, а в конец",
          got["gemmaFirst"]["order"] == ["gemma", "mimo", "webchat:duckai"])
    check("одна Gemma — новое после неё (основным не становится)", got["onlyGemma"] == ["gemma", "mimo"])


def test_steps(got):
    print("\n3. Порядок → шаги ответа")
    check("ключи подряд — один запрос, потом сайт, потом Gemma", got["steps"] == [
        {"kind": "cloud", "only": ["mimo", "kimi"]},
        {"kind": "web", "sites": ["deepseek"]},
        {"kind": "gemma"},
    ])
    check("веб-чат основным, Gemma в середине", got["stepsMixed"] == [
        {"kind": "web", "sites": ["deepseek"]},
        {"kind": "cloud", "only": ["mimo"]},
        {"kind": "gemma"},
        {"kind": "web", "sites": ["google"]},
        {"kind": "cloud", "only": ["kimi"]},
    ])
    check("чужое мимо, Gemma один раз", got["stepsJunk"] == [{"kind": "gemma"}, {"kind": "cloud", "only": ["mimo"]}])


def test_actions(got):
    print("\n4. Правки в досье")
    check("выше", got["up"] == ["mimo", "kimi", "webchat:deepseek", "gemma"])
    check("выше основного — нельзя (только «сделать основным»)", got["upToMain"] == got["orig"])
    check("основной стрелками не двигается", got["mainDown"] == got["orig"])
    check("звено ПК без ядра не возвращается", got["restorePc"] == {"order": ["mimo", "gemma"], "exclude": ["pc:local", "kimi"]})
    check("звенья ПК в запись не идут", got["toChainPc"]["exclude"] == ["kimi"])
    check("ниже", got["down"] == ["mimo", "kimi", "webchat:deepseek", "gemma"])
    check("сделать основным — старый основной следом", got["first"] == ["webchat:deepseek", "mimo", "kimi", "gemma"])
    check("убрать — из цепочки в убранные", got["ex"] == {"order": ["mimo", "webchat:deepseek", "gemma"], "exclude": ["zai", "kimi"]})
    check("основного не убрать", got["exMain"] == {"order": got["orig"], "exclude": []})
    check("вернуть — перед Gemma", got["back"] == {"order": ["mimo", "webchat:deepseek", "kimi", "gemma"], "exclude": ["zai"]})
    check("запись: основной, порядок, убранные без основного", got["chain"] == {
        "primary": "webchat:deepseek", "fallback": ["mimo", "gemma"], "exclude": ["kimi"]})
    check("исходный порядок не испорчен", got["orig"] == ["mimo", "webchat:deepseek", "kimi", "gemma"])


def body(src, start):
    i = src.find(start)
    if i < 0:
        return ""
    j = src.find("@PluginMethod", i + len(start))
    return src[i:j if j > 0 else len(src)]


def test_static():
    print("\n5. Плагин, движок, досье")
    java = JAVA.read_text(encoding="utf-8")
    cc = body(java, "public void cloudChat(")
    check("cloudChat: only — только эти и в их порядке, без него — как раньше",
          "only != null ? pick(allProvidersFor(persona), only) : providersFor(persona)" in cc)
    allp = java[java.find("private JSONArray allProvidersFor("):java.find("private static void addProviders(")]
    i_own, i_core, i_phone = allp.find('"own"'), allp.find('"core"'), allp.find('"phone"')
    check("ключи персоны → ключи ядра других персон → ключи телефона", 0 < i_own < i_core < i_phone)
    add = java[java.find("private static void addProviders("):java.find("@PluginMethod", java.find("private static void addProviders("))]
    check("ключ ядра у провайдера уже есть — чужой не подменяет; телефон — дополняет",
          'if (!"phone".equals(src)) continue;' in add)
    pp = body(java, "public void personaProviders(")
    check("personaProviders: вебу — маски; без модели или ключа — мимо",
          "mask(keys.optString(k))" in pp and 'o.put("keys", masks)' in pp and "api_keys" not in pp.split("JSONArray masks")[1]
          and 'p.optString("model").isEmpty()' in pp)

    eng = (SRC / "offline" / "engine.ts").read_text(encoding="utf-8")
    check("движок идёт по цепочке персоны",
          "const steps = await answerSteps(persona).catch(() => defaultSteps());" in eng and "for (const step of steps)" in eng)
    check("облаку — only шага", "OfflineLlm.cloudChat({ persona, messages: cloudMessages(), only })" in eng)
    check("Gemma не последняя: нет модели — к следующему шагу",
          "if (!status?.downloaded) continue;" in eng and "else if (code !== 'model_missing') throw e;" in eng)
    wc = (SRC / "offline" / "webchat.ts").read_text(encoding="utf-8")
    check("выключенный сайт не спрашивается и из цепочки", "prefs.on[s.id] === true" in wc)
    pc = (SRC / "offline" / "personaChain.ts").read_text(encoding="utf-8")
    check("в цепочку — только включённые сайты", "for (const s of enabledSites(webchatPrefs()))" in pc)
    check("цепочка — своя на персону", "const PREFIX = 'vpc-phone-chain:';" in pc and "PREFIX + persona" in pc)
    check("не меняли — с компьютера (копия llm персоны и общий основной)",
          "chainFromCore(copy?.config.llm ?? null, copy?.providers.find((p) => p.active)?.id ?? null, ids)" in pc)
    st = (SRC / "sections" / "Settings.tsx").read_text(encoding="utf-8")
    check("досье без ядра — редактор цепочки на телефоне",
          "{offlineMode && personaId ? (\n        <PhoneChainCard personaId={personaId} />" in st)
    check("и без копии настроек", "{personaId && <PhoneChainCard personaId={personaId} />}" in st)
    check("цепочку сохранили или сбросили на ядре — телефонная снимается",
          st.count("saveChain(personaId, null);") + st.count("saveChain(personaId!, null);") == 2)
    prov = (SRC / "sections" / "Providers.tsx").read_text(encoding="utf-8")
    card = (SRC / "offline" / "PhoneChainCard.tsx").read_text(encoding="utf-8")
    check("«вернуть» звено ПК — ошибка «ядро не запущено», а не возврат",
          "if (i.kind === 'pc') setNoCore(i.id);" in card and "phoneChain.noCoreWeb" in card)
    check("подписи: веб-чат ПК / веб-чат телефона",
          "phoneChain.webPc" in card and "phoneChain.webPhone" in card)
    check("звенья ПК — веб-чаты ядра и сайты из llm персоны, локальная модель",
          "copy.webchat.sites" in pc and "id: `pc:webchat:${site}`" in pc and "id: 'pc:local'" in pc)
    check("во вкладке «Провайдеры» — ключи провайдеров, общего порядка нет",
          "<PhoneKeysCard" in prov and "ChainCard" not in prov.replace("PhoneChainCard", ""))

    # Правка ключей с телефона: поверх ключей ядра, только на телефоне
    check("правки ключей — и в ответе по порядку персоны, и в запасном пути",
          "return applyEdits(mergedFor(persona));" in java and "return applyEdits(out);" in java)
    ap = java[java.find("private JSONArray applyEdits("):java.find("@PluginMethod", java.find("private JSONArray applyEdits("))]
    check("правка: своя модель и без убранных ключей ядра (по отпечатку)",
          'c.put("model", model);' in ap and "contains(hide, keyId(keys.optString(k)))" in ap)
    kr = body(java, "public void keyring(")
    check("keyring: вебу — маски и отпечатки, не ключи",
          'put("mask", mask(keys.optString(k)))' in kr and '.put("api_keys"' not in kr)
    ed = body(java, "public void editProvider(")
    check("editProvider: хранит модель и отпечатки, ключ ядра не трогает",
          "storeEdit(name, e);" in ed and "storeProviders" not in ed and "encrypt" not in ed)
    cl = body(java, "public void clearProviders(")
    check("сброс ключей ядра правки телефона не стирает", "EDIT_PREFIX" not in cl)
    kc = (SRC / "offline" / "PhoneKeysCard.tsx").read_text(encoding="utf-8")
    check("карточка: ключ ПК — убрать/вернуть, ключ телефона — удалить, модель ПК — правкой",
          "hideKey: k.id" in kc and "showKey: k.id" in kc and "removeKey: k.index" in kc
          and "editProvider({ name: id, model: v === r.core_model ? '' : v })" in kc)

    used = set()
    for f in ("offline/PhoneChainCard.tsx", "offline/PhoneKeysCard.tsx",
              "offline/WebChatsCard.tsx", "sections/Providers.tsx"):
        text = (SRC / f).read_text(encoding="utf-8")
        used |= set(re.findall(r"t\('([\w.]+)'", text))
        for pair in re.findall(r"t\([\w.?]+ \? '([\w.]+)' : '([\w.]+)'\)", text):
            used |= set(pair)
    for lang in ("ru", "en"):
        text = (SRC / "i18n" / f"{lang}.ts").read_text(encoding="utf-8")
        missing = sorted(k for k in used if f"'{k}':" not in text)
        check(f"{lang}: тексты ({len(used)} ключей){'' if not missing else ' — нет: ' + ', '.join(missing)}", not missing)


def main():
    print("Цепочка провайдеров персоны без ядра")
    if not shutil.which("node"):
        print("  (пропущено — нет Node)")
        return 0
    got = node_run()
    if got is None:
        return 1
    test_tokens(got)
    test_normalize(got)
    test_steps(got)
    test_actions(got)
    test_static()
    print(f"\nИтого: {results['ok']} OK, {results['fail']} FAIL")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())

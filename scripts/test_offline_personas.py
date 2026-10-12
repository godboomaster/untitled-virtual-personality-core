"""Персона, созданная на телефоне без ядра (web/src/offline/localPersonas.ts,
personaSync.ts, state.ts).

Без связи с ядром новая персона сохраняется на телефоне: проверки — как у
create_persona ядра, кит для ответов без ядра — из её YAML (полный
характер; правила, заметки напоминаний, гейт поиска — из кита другой
персоны с ядра), в списках персон она видна и после перезаписи снимка
списка. Ядро на связи — персона уходит на него раньше переписки: 409 —
новый id «_2» и перенос всего, что телефон хранит под старым, 400 — ошибка
на карточке, нет сети — ждёт. Переписка уходит частями по 200 (лимит
ядра), персоны с телефона в ней и в напоминаниях пропускаются, краткое
описание дозапрашивается, пока ядро его составляет.

Модули гоняются под Node (снятие типов, Node ≥ 23.6) с поддельными
@capacitor/core и ../api (ядро — в памяти скрипта); ядро, LLM и сеть не
вызываются.

Запуск: python3 -m scripts.test_offline_personas
"""

import json
import os
import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SRC = WEB / "src"

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    ok += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


def url(rel: str) -> str:
    return (SRC / rel).as_uri()


MODULES = {
    "localPersonas": url("offline/localPersonas.ts"),
    "kit": url("offline/kit.ts"),
    "prompt": url("offline/prompt.ts"),
    "apiData": url("apiData.ts"),
    "state": url("offline/state.ts"),
    "store": url("offline/store.ts"),
    "reminders": url("offline/reminders.ts"),
    "personaSync": url("offline/personaSync.ts"),
}

# Общая часть сценария: заглушки модулей, localStorage, поддельное ядро
PRELUDE = r"""
import { registerHooks } from 'node:module';
const API_STUB = `
export class ApiError extends Error {
  constructor(status, detail, data) { super(detail); this.status = status; this.data = data; }
}
export const api = new Proxy({}, { get: (_, k) => (...a) => globalThis.__api(String(k), ...a) });
export function isNativeApp() { return true; }
export function getApiUrl() { return 'http://core:8000'; }
`;
const stubs = {
  '@capacitor/core': `export const Capacitor = { isPluginAvailable: () => false, isNativePlatform: () => true };
    export function registerPlugin() {
      return new Proxy({}, { get: (_, k) => (k === 'then' ? undefined : (...a) => globalThis.__plugin(String(k), ...a)) });
    }`,
  '../api': API_STUB,
  './api': API_STUB,
  '../link/client': 'export function getLinkConfig() { return null; }',
};
registerHooks({
  resolve(spec, ctx, next) {
    if (spec in stubs) return { url: 'data:text/javascript,' + encodeURIComponent(stubs[spec]), shortCircuit: true };
    if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(spec + '.ts', ctx);
    return next(spec, ctx);
  },
});
const lsMap = new Map();
globalThis.localStorage = {
  getItem: (k) => (lsMap.has(k) ? lsMap.get(k) : null),
  setItem: (k, v) => { lsMap.set(k, String(v)); },
  removeItem: (k) => { lsMap.delete(k); },
  key: (i) => [...lsMap.keys()][i] ?? null,
  get length() { return lsMap.size; },
};
globalThis.window = Object.assign(new EventTarget(), { setTimeout, clearTimeout, setInterval, clearInterval });
globalThis.__plugin = async () => ({});
localStorage.setItem('vpc-lang', 'ru');

const { ApiError } = await import('data:text/javascript,' + encodeURIComponent(API_STUB));
const M = {};
for (const [k, v] of Object.entries(INPUT.modules)) M[k] = v;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Ядро в памяти: персоны (id → YAML), память удалённых, режимы сбоев
const core = {
  personas: new Map(), leftover: new Set(), createMode: 'ok', listMode: 'ok',
  importFail: 0, imports: 0, kitShort: [], calls: [], notFound: [],
};
const DONOR = INPUT.donor;
function personaApi(id) {
  return { id, name: id, description: '', color: null, features: {}, settings: {} };
}
function notFound(method, id) {
  core.notFound.push(`${method}:${id}`);
  return new ApiError(404, `Персона '${id}' не найдена`);
}
globalThis.__api = async (method, ...a) => {
  core.calls.push([method, ...a]);
  switch (method) {
    case 'getPersonas':
      if (core.listMode === 'net') throw new TypeError('Failed to fetch');
      return [...core.personas.keys()].map(personaApi);
    case 'createPersona': {
      const [yaml, memory] = a;
      if (core.createMode === 'net') throw new TypeError('Failed to fetch');
      if (core.createMode === '400') throw new ApiError(400, 'YAML не парсится: плохой отступ');
      const id = /^id:\s*(\S+)/m.exec(yaml)?.[1];
      if (core.personas.has(id)) throw new ApiError(409, `Персона '${id}' уже существует`);
      if (core.leftover.has(id) && !memory) {
        throw new ApiError(409, 'осталась память', { memory_exists: true, persona: id, can_keep: true });
      }
      core.personas.set(id, yaml);
      return { ok: true, persona: id };
    }
    case 'getPersonaYaml': {
      const [id] = a;
      if (!core.personas.has(id)) throw notFound(method, id);
      return { persona: id, yaml: core.personas.get(id) };
    }
    case 'offlineImport': {
      const [id, msgs] = a;
      if (!core.personas.has(id)) throw notFound(method, id);
      if (msgs.length > 200) throw new ApiError(422, 'too many messages');
      core.imports += 1;
      if (core.importFail === core.imports) throw new TypeError('Failed to fetch');
      return { imported: msgs.length, skipped: 0 };
    }
    case 'getOfflineKit': {
      const [id] = a;
      if (!core.personas.has(id)) throw notFound(method, id);
      const st = core.kitShort.length ? core.kitShort.shift() : 'ready';
      if (st === 'net') throw new TypeError('Failed to fetch');
      return {
        ...DONOR, persona: id, name: id,
        parts: { ...DONOR.parts, short: st === 'ready' ? 'кратко с ядра' : '' },
        short_prompt: { source: st === 'ready' ? 'auto' : '', status: st, at: 0 },
      };
    }
    case 'offlineReminders': {
      const [id, list] = a;
      if (!core.personas.has(id)) throw notFound(method, id);
      return { created: Object.fromEntries(list.map((r) => [r.id, 'srv'])), past: [], enabled: true };
    }
    case 'getOfflineMemory':
    case 'getOfflineProviders':
    case 'getPersonaConfig':
    case 'getProviders':
      throw new ApiError(404, 'нет в поддельном ядре');
  }
  throw new Error('unexpected api.' + method);
};
const callNames = () => core.calls.map((c) => (typeof c[1] === 'string' && c[0] !== 'createPersona' ? `${c[0]}:${c[1]}` : c[0]));
const out = {};
"""

EPILOGUE = "\nconsole.log(JSON.stringify(out));\nprocess.exit(0);\n"

NOVA = """id: nova
name: Нова
version: "1.0"
description: Бортовой ИИ
# комментарий в YAML переживает смену id
features:
  reminder: true
  web_search: true
system_prompt: |
  ТЫ — НОВА, бортовой ИИ исследовательского судна.
  Говоришь коротко и по делу.
settings:
  temperature: 0.8
"""

DONOR = {
    "version": 3, "persona": "connor", "name": "Коннор", "generated_at": 1, "system": "CONNOR SYSTEM",
    "parts": {"character": "Я Коннор, андроид-детектив.", "rules": "CORE OFFLINE RULES", "style": "STYLE NOTE",
              "language": "The user speaks Russian in the dialogue: write all free-text output in Russian.",
              "short": "Коннор кратко", "rules_local": "RULES LOCAL"},
    "short_prompt": {"source": "auto", "status": "ready", "at": 1},
    "lang": "ru", "facts": ["Пользователя зовут Хэнк"], "history": [{"role": "user", "text": "привет", "ts": 1}],
    "history_first_ts": 1, "history_total": 1, "dossier": "DOSSIER CONNOR", "self_memory": "SELF MEMORY CONNOR",
    "env": {"city": "Новосибирск", "lat": 55.0, "lon": 82.9, "codes": {}},
    "reminders": [{"id": "r1", "ref": "x", "task": "t", "at": 9e9, "recurring": False}], "reminders_enabled": True,
    "rules": {"web": "WEB RULE", "env": "ENV RULE", "self_memory": "SM RULE", "recall_head": "RH", "recall": "RR"},
    "notes": {"set": "SET{task} {when}", "ask_time": "ASK{task}", "past": "PAST", "recurring": "REC",
              "scheduled": "SCHED {list}", "extract": "EXTRACT {now}"},
    "search_gate": {"address_words": ["ты"], "smalltalk": ["привет"], "self_patterns": ["p"]},
    "fetched_at": 1000,
}


def node(code: str, extra=None) -> dict | None:
    payload = {"modules": MODULES, "donor": DONOR, "nova": NOVA, **(extra or {})}
    script = f"globalThis.INPUT = {json.dumps(payload, ensure_ascii=False)};\n{PRELUDE}\n{code}\n{EPILOGUE}"
    res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True,
                         timeout=60, cwd=WEB, env={**os.environ, "TZ": "Europe/Moscow"})
    if res.returncode != 0:
        check(f"Node: {res.stderr.strip()[:1500]}", False)
        return None
    return json.loads(res.stdout.strip().splitlines()[-1])


def yaml_with(id_=None, **fields):
    lines = []
    if id_ is not None:
        lines.append(f"id: {id_}")
    lines += [f"{k}: {v}" for k, v in fields.items()]
    return "\n".join(lines) + "\n"


# ─── A. Проверки при создании ───

def test_validation():
    section("A. Проверки при создании (как create_persona ядра)")
    prompt = "system_prompt: Ты тестовая персона."
    cases = [
        ("новая персона", NOVA, [], {"ok": True}),
        ("id уже есть на телефоне", NOVA, [], {"code": "exists", "id": "nova"}),
        ("id есть в списке с ядра (регистр не важен)", f"id: Connor\n{prompt}\n", ["connor"], {"code": "exists"}),
        ("служебный tg", f"id: tg\n{prompt}\n", [], {"code": "reserved"}),
        ("служебный TG (регистр)", f"id: TG\n{prompt}\n", [], {"code": "reserved"}),
        ("префикс api_", f"id: api_nova\n{prompt}\n", [], {"code": "reserved"}),
        ("префикс API_ (регистр)", f"id: API_x\n{prompt}\n", [], {"code": "reserved"}),
        ("служебный persona_drafts", f"id: persona_drafts\n{prompt}\n", [], {"code": "reserved"}),
        ("id с пробелом", f"id: bad id\n{prompt}\n", [], {"code": "bad_id"}),
        ("без id", f"name: X\n{prompt}\n", [], {"code": "bad_id"}),
        ("id длиннее 64", f"id: {'a' * 65}\n{prompt}\n", [], {"code": "bad_id"}),
        ("id ровно 64", f"id: {'b' * 64}\n{prompt}\n", [], {"ok": True}),
        ("без system_prompt", "id: empty1\nname: X\n", [], {"code": "no_prompt"}),
        ("system_prompt из пробелов", "id: empty2\nsystem_prompt: '   '\n", [], {"code": "no_prompt"}),
        ("YAML с ошибкой", "id: [\n", [], {"code": "yaml"}),
        ("YAML — список", "- a\n- b\n", [], {"code": "yaml"}),
        ("без name — имя = id", f"id: noname\n{prompt}\n", [], {"ok": True, "name": "noname"}),
    ]
    code = r"""
const lp = await import(M.localPersonas);
out.cases = INPUT.cases.map(([yaml, ids]) => {
  try { const p = lp.addLocalPersona(yaml, ids, 1000); return { ok: true, id: p.id, name: p.name, status: p.status }; }
  catch (e) { return { ok: false, code: e.code, id: e.id }; }
});
out.list = lp.localPersonas().map((p) => p.id);
out.colors = INPUT.colorIds.map((id) => lp.personaColor(id, {}));
out.explicit = lp.personaColor('x', { color: ' #123456 ' });
out.novaColor = lp.localPersona('nova').color;
out.withId = lp.withYamlId(INPUT.nova, 'nova_2');
out.withIdFlow = lp.withYamlId('{id: nova, name: N, system_prompt: hi}', 'nova_3');
out.parsedAfter = lp.parsePersonaYaml(out.withId).id;
out.parsedFlow = lp.parsePersonaYaml(out.withIdFlow).id;
out.api = lp.localApiPersona(lp.localPersona('nova'));
out.re = lp.LOCAL_ID_RE.source;
out.reserved = INPUT.reservedProbe.map((id) => lp.isReservedId(id));
"""
    color_ids = ["nova", "connor", "arrodes", "x", "Персона", "a" * 64]
    server = (ROOT / "app/api/settings_api.py").read_text(encoding="utf-8")
    shared = set(re.findall(r'"([a-z_]+)"', re.search(r"_SHARED_DATA_NAMES = \{([^}]*)\}", server).group(1)))
    probe = sorted(shared) + [x.upper() for x in shared] + ["api_x", "Api_Y", "apix", "connor"]
    r = node(code, {"cases": [[c[1], c[2]] for c in cases], "colorIds": color_ids, "reservedProbe": probe})
    if r is None:
        return
    for (name, _, _, want), got in zip(cases, r["cases"]):
        if want.get("ok"):
            good = got["ok"] and all(got.get(k) == v for k, v in want.items() if k != "ok")
        else:
            good = not got["ok"] and all(got.get(k) == v for k, v in want.items())
        check(f"{name} → {want}" + ("" if good else f"  (получено {got})"), good)
    check("сохранены только прошедшие проверку", r["list"] == ["nova", "b" * 64, "noname"])
    # Палитра и формат id — копия ядра
    runtime = (ROOT / "app/api/runtime.py").read_text(encoding="utf-8")
    palette = re.findall(r'"(#[0-9a-f]{6})"', re.search(r"_COLOR_PALETTE = \[([^\]]*)\]", runtime).group(1))
    want_colors = [palette[zlib.crc32(i.encode("utf-8")) % len(palette)] for i in color_ids]
    check("цвет по умолчанию = persona_color ядра (crc32 id)", r["colors"] == want_colors)
    check("явный color из YAML", r["explicit"] == "#123456")
    check("цвет записан при создании", r["novaColor"] == want_colors[0])
    security = (ROOT / "app/api/security.py").read_text(encoding="utf-8")
    check("формат id = SAFE_ID_RE ядра", re.search(r'SAFE_ID_RE = re.compile\(r"([^"]+)"\)', security).group(1) == r["re"])
    want_res = [True] * (2 * len(shared)) + [True, True, False, False]
    check(f"служебные id = _SHARED_DATA_NAMES ядра ({len(shared)}) и api_*", r["reserved"] == want_res)
    check("смена id: строка id, комментарий и блок | на месте",
          "id: nova_2\n" in r["withId"] and "# комментарий в YAML" in r["withId"]
          and "system_prompt: |" in r["withId"] and r["parsedAfter"] == "nova_2")
    check("смена id во flow-YAML — через разбор документа", r["parsedFlow"] == "nova_3")
    api = r["api"]
    check("персона для списка: фичи и настройки из YAML, пометка local",
          api["features"] == {"reminder": True, "web_search": True} and api["settings"] == {"temperature": 0.8}
          and api["local"] == {"status": "pending"} and api["name"] == "Нова" and not api["builtin"])


# ─── B. Кит из YAML ───

def test_local_kit():
    section("B. Кит персоны с телефона")
    code = r"""
const kit = await import(M.kit);
const lp = await import(M.localPersonas);
const prompt = await import(M.prompt);
lp.addLocalPersona(INPUT.nova, []);
out.bare = kit.getKit('nova');
kit.storeKit('connor', INPUT.donor);
out.full = kit.getKit('nova');
out.memo = kit.getKit('nova') === kit.getKit('nova');
lp.addLocalPersona(INPUT.nova.replace('id: nova', 'id: lyra').replace('reminder: true', 'reminder: false'), []);
out.norem = kit.getKit('lyra').reminders_enabled;
lp.addLocalPersona('id: vega\nshort_prompt: Вега, кратко.\nsystem_prompt: Ты Вега.\n', []);
out.short = kit.getKit('vega').parts.short;
out.shortStatus = kit.getKit('vega').short_prompt;
out.gate = out.full.version >= 2 && !!out.full.rules;
out.prompt = prompt.localPrompt(out.full, { recall: [], facts: [] }, [], [], 'привет').system;
out.unknown = kit.getKit('nobody');
out.fallback = kit.fallbackKit('nova', 'привет')?.name ?? null;
const before = core.calls.length;
out.refreshed = (await kit.refreshKit('nova'))?.persona ?? null;
out.refreshCalls = core.calls.length - before;
out.kitPersonas = kit.kitPersonas();
"""
    r = node(code)
    if r is None:
        return
    bare, full = r["bare"], r["full"]
    character = "ТЫ — НОВА, бортовой ИИ исследовательского судна.\nГоворишь коротко и по делу."
    check("характер — весь system_prompt", full["parts"]["character"] == character and full["system"].startswith(character))
    check("имя и id из YAML, local без fallback", full["name"] == "Нова" and full["persona"] == "nova"
          and full.get("local") is True and not full.get("fallback"))
    check("без кита с ядра: версия 1, свои правила, без напоминаний и погоды",
          bare["version"] == 1 and "unavailable" in bare["parts"]["rules"] and bare["reminders_enabled"] is False
          and bare["env"] is None and bare["notes"]["set"] == "")
    check("язык — интерфейса (ru)", full["lang"] == "ru" and "Russian" in full["parts"]["language"])
    check("с китом с ядра: правила, стиль, заметки, гейт поиска, погода — из него",
          full["version"] == 3 and full["parts"]["rules"] == "CORE OFFLINE RULES" and full["parts"]["style"] == "STYLE NOTE"
          and full["parts"].get("rules_local") == "RULES LOCAL" and full["rules"] == DONOR["rules"]
          and full["notes"] == DONOR["notes"] and full["search_gate"] == DONOR["search_gate"] and full["env"] == DONOR["env"])
    check("чужое не попадает: факты, история, досье, личная память, напоминания, краткое описание",
          full["facts"] == [] and full["history"] == [] and full["dossier"] == "" and full["self_memory"] == ""
          and full["reminders"] == [] and full["parts"]["short"] == "" and "short_prompt" not in full
          and "Коннор" not in full["system"])
    check("напоминания — по features.reminder", full["reminders_enabled"] is True and r["norem"] is False)
    check("напоминания, поиск, погода включены (full в engine.ts)", r["gate"] is True)
    check("short_prompt из YAML — краткое описание сразу",
          r["short"] == "Вега, кратко." and r["shortStatus"]["source"] == "yaml" and r["shortStatus"]["status"] == "ready")
    check("кит собирается один раз (тот же объект)", r["memo"] is True)
    check("промпт модели телефона — с характером персоны", "бортовой ИИ" in r["prompt"])
    check("нет персоны — нет кита", r["unknown"] is None)
    check("запасной кит по списку тоже видит персону с телефона", r["fallback"] == "Нова")
    check("refreshKit персоны с телефона — без запроса к ядру", r["refreshed"] == "nova" and r["refreshCalls"] == 0)
    check("кит персоны с телефона не пишется в хранилище китов", r["kitPersonas"] == ["connor"])


# ─── C. Списки персон ───

def test_lists():
    section("C. Персона с телефона в списках")
    code = r"""
const React = await import('react');
const { renderToString } = await import('react-dom/server');
const apiData = await import(M.apiData);
const lp = await import(M.localPersonas);
const ids = (list) => (list ? list.map((p) => [p.id, p.local ? p.local.status : null]) : null);
const seen = [];
function C() { seen.push(apiData.useApiPersonas()); return null; }
const render = () => { renderToString(React.createElement(C)); return seen[seen.length - 1]; };
lp.addLocalPersona(INPUT.nova, []);
out.hasOnlyLocal = apiData.hasOfflinePersonas();
localStorage.setItem('vpc-offline-personas', JSON.stringify([{ id: 'connor', name: 'Коннор', description: 'андроид', features: {}, settings: {} }]));
out.seed = apiData.seedOfflinePersonas();
const a = render(), b = render();
out.ids1 = ids(a);
out.stable = a === b;
out.color = a.find((p) => p.id === 'nova').color;
out.features = a.find((p) => p.id === 'nova').features;
out.list1 = apiData.offlinePersonaList().map((p) => [p.id, p.name, p.description]);
core.personas.set('connor', 'x');
core.personas.set('ada', 'x');
await apiData.refetchPersonas();
out.ids2 = ids(render());
out.snapshot = JSON.parse(localStorage.getItem('vpc-offline-personas')).map((p) => p.id);
out.list2 = apiData.offlinePersonaList().map((p) => p.id);
lp.setLocalPersonaError('nova', 'плохой YAML');
out.ids3 = ids(render());
core.personas.set('nova', 'x');
await apiData.refetchPersonas();
out.ids4 = ids(render());
core.listMode = 'net';
const err = await apiData.refetchPersonas();
out.afterNet = ids(render());
out.errStatus = err ? err.status : null;
"""
    r = node(code)
    if r is None:
        return
    check("только персона с телефона, снимка нет — запуск без ядра доступен", r["hasOnlyLocal"] is True)
    check("снимок + персона с телефона: в списке обе, у новой пометка", r["seed"] and r["ids1"] == [["connor", None], ["nova", "pending"]])
    check("список — тот же массив, пока ничего не сменилось", r["stable"] is True)
    check("цвет и фичи персоны с телефона — из YAML", r["color"].startswith("#") and "reminder" in r["features"])
    check("offlinePersonaList — и снимок, и персона с телефона",
          r["list1"] == [["connor", "Коннор", "андроид"], ["nova", "Нова", "Бортовой ИИ"]])
    check("снимок перезаписан списком с ядра — персона с телефона на месте",
          r["ids2"] == [["connor", None], ["ada", None], ["nova", "pending"]] and "nova" not in r["snapshot"]
          and r["list2"] == ["connor", "ada", "nova"])
    check("ошибка ядра — в пометке", r["ids3"][-1] == ["nova", "error"])
    check("появилась на ядре — в списке одна, с ядра", r["ids4"] == [["connor", None], ["ada", None], ["nova", None]])
    check("перечитка без связи — прежний список, не демо-данные", r["afterNet"] == r["ids4"] and r["errStatus"] == 0)


# ─── D. Перенос на ядро ───

SETUP = r"""
const state = await import(M.state);
const lp = await import(M.localPersonas);
const store = await import(M.store);
const kit = await import(M.kit);
const rem = await import(M.reminders);
const apiData = await import(M.apiData);
const sync = await import(M.personaSync);
localStorage.setItem('vpc-offline-personas', JSON.stringify([{ id: 'connor', name: 'Коннор', description: '', features: {}, settings: {} }]));
core.personas.set('connor', 'id: connor\nsystem_prompt: x\n');
apiData.seedOfflinePersonas();
kit.storeKit('connor', INPUT.donor);
const renames = [];
lp.onLocalPersonaRenamed((f, t) => renames.push([f, t]));
function addNova(yaml = INPUT.nova) {
  lp.addLocalPersona(yaml, ['connor']);
  const id = /^id:\s*(\S+)/m.exec(yaml)[1];
  store.addOfflineMessage(id, { role: 'user', text: 'привет', source: 'local' });
  store.addOfflineMessage(id, { role: 'assistant', text: 'Нова на связи', source: 'local' });
  store.addOfflineMessage(id, { role: 'user', text: 'как дела?', source: 'queued' });
  rem.addLocalReminder(id, 'полить цветы', Date.now() / 1000 + 3600);
}
const creates = () => core.calls.filter((c) => c[0] === 'createPersona').map((c) => ({ id: /^id:\s*(\S+)/m.exec(c[1])[1], memory: c[2] ?? null }));
const imports = () => core.calls.filter((c) => c[0] === 'offlineImport').map((c) => [c[1], c[2].length]);
"""


def test_sync_ok():
    section("D1. Перенос: ядро приняло")
    code = SETUP + r"""
addNova();
core.kitShort = ['ready'];
await state.syncWithCore();
await sleep(50);
out.calls = callNames();
out.creates = creates();
out.imports = imports();
out.local = lp.localPersonas().length;
out.msgs = store.offlineMessages('nova').map((m) => [m.text, m.source]);
const k = kit.getKit('nova');
out.kit = { local: !!k.local, status: k.short_prompt?.status, short: k.parts.short, fetched: k.fetched_at > 0 };
out.snapshot = JSON.parse(localStorage.getItem('vpc-offline-personas')).map((p) => p.id);
out.notFound = core.notFound;
out.reminders = core.calls.filter((c) => c[0] === 'offlineReminders').map((c) => c[1]);
out.localReminders = rem.localReminders().length;
out.posted = core.personas.get('nova') === INPUT.nova;
out.list = apiData.offlinePersonaList().map((p) => p.id);
"""
    r = node(code)
    if r is None:
        return
    calls = r["calls"]
    i_create = calls.index("createPersona")
    i_import = calls.index("offlineImport:nova")
    i_kit = calls.index("getOfflineKit:nova")
    check("порядок: список с ядра → создание → перечитка → переписка → кит",
          calls[0] == "getPersonas" and i_create < calls.index("getPersonas", i_create) < i_import < i_kit)
    check("YAML ушёл как есть, без выбора памяти", r["posted"] and r["creates"] == [{"id": "nova", "memory": None}])
    check("переписка до хвоста без ответа — на ядро (2 сообщения)", r["imports"] == [["nova", 2]])
    check("хвост без ответа ждёт чата (отправится обычным путём)", r["msgs"] == [["как дела?", "queued"]])
    check("из персон телефона — прочь, в снимке списка — есть", r["local"] == 0 and "nova" in r["snapshot"]
          and r["list"] == ["connor", "nova"])
    check("кит с ядра сразу после переноса (краткое описание готово)",
          r["kit"] == {"local": False, "status": "ready", "short": "кратко с ядра", "fetched": True})
    check("напоминание с телефона — на ядро после переноса", "nova" in r["reminders"] and r["localReminders"] == 0)
    check("ни одного 404 по персоне", r["notFound"] == [])


def test_sync_conflict():
    section("D2. Перенос: id занят (409) — новый id и перенос ключей")
    code = SETUP + r"""
core.personas.set('nova', 'id: nova\nsystem_prompt: другая\n');
core.personas.set('nova_2', 'id: nova_2\nsystem_prompt: другая\n');
addNova();
// Вторая персона телефона уже заняла nova_3 — её id свободный выбор обходит
addNova(INPUT.nova.replace('id: nova', 'id: nova_3').replace('Нова', 'Нова-3'));
kit.storeKit('nova', { ...INPUT.donor, persona: 'nova', local: true });  // ключ кита под старым id
localStorage.setItem('vpc-offline-kit-error:nova', JSON.stringify({ message: 'x', at: 1 }));
rem.askTimePlan({ persona: 'nova', notes: { ask_time: 'ASK{task}' } }, 'купить хлеб');
// По шагам syncWithCore: ключи под старым id — до обновления китов (кит
// чужой «nova» с ядра потом ляжет под своим id законно)
await sync.syncLocalPersonas();
out.oldKeys = ['vpc-offline-kit:nova', 'vpc-offline-kit-error:nova'].map((k) => localStorage.getItem(k) !== null);
out.newKit = kit.getKit('nova_4')?.persona ?? null;
await state.syncOffline();
await rem.syncLocalReminders();
out.creates = creates();
out.renames = renames;
out.yaml = core.personas.get('nova_4');
out.imports = imports();
out.oldMsgs = store.offlineMessages('nova').length;
out.newMsgs = store.offlineMessages('nova_4').map((m) => m.text);
out.pending = Object.keys(JSON.parse(localStorage.getItem('vpc-offline-remind-pending') || '{}'));
out.reminders = core.calls.filter((c) => c[0] === 'offlineReminders').map((c) => c[1]);
out.local = lp.localPersonas().map((p) => p.id);
out.list = apiData.offlinePersonaList().map((p) => p.id);
out.notFound = core.notFound;
"""
    r = node(code)
    if r is None:
        return
    check("занятый id → свободный nova_4 (nova_2 на ядре, nova_3 — у другой персоны телефона)",
          r["creates"] == [{"id": "nova_4", "memory": None}, {"id": "nova_3", "memory": None}])
    check("событие смены id для чата", r["renames"] == [["nova", "nova_4"]])
    check("в YAML новый id, остальное на месте", r["yaml"] is not None and "id: nova_4\n" in r["yaml"]
          and "# комментарий в YAML" in r["yaml"] and "name: Нова\n" in r["yaml"])
    check("переписка — под новым id и на ядро под ним", r["oldMsgs"] == 0 and r["newMsgs"] == ["как дела?"]
          and ["nova_4", 2] in r["imports"] and not any(i[0] == "nova" for i in r["imports"]))
    check("кит и ошибка кита — не под старым id", r["oldKeys"] == [False, False] and r["newKit"] is not None)
    check("вопрос «когда напомнить?» — под новым id", r["pending"] == ["nova_4"])
    check("напоминания — на ядро под новым id", "nova_4" in r["reminders"] and "nova" not in r["reminders"])
    check("обе перенесены, в списке — новые id (nova, nova_2 — чужие с ядра)",
          r["local"] == [] and r["list"] == ["connor", "nova", "nova_2", "nova_4", "nova_3"])
    check("ни одного 404", r["notFound"] == [])


def test_sync_memory_conflict():
    section("D3. Перенос: под id осталась память удалённой персоны")
    code = SETUP + r"""
core.leftover.add('nova');
addNova();
await state.syncWithCore();
out.creates = creates();
out.calls = callNames();
out.on = [...core.personas.keys()];
"""
    r = node(code)
    if r is None:
        return
    check("409 memory_exists → новый id, память на ядре не тронута (memory не отправлен)",
          r["creates"] == [{"id": "nova", "memory": None}, {"id": "nova_2", "memory": None}])
    check("перед новым id — проверка, не наша ли это персона (GET yaml)", "getPersonaYaml:nova" in r["calls"])
    check("на ядре — nova_2", r["on"] == ["connor", "nova_2"])


def test_sync_ours():
    section("D4. Перенос: прошлый дошёл, а ответ потерялся")
    code = SETUP + r"""
addNova();
core.personas.set('nova', INPUT.nova);
await state.syncWithCore();
out.creates = creates();
out.local = lp.localPersonas().length;
out.renames = renames;
out.imports = imports();
"""
    r = node(code)
    if r is None:
        return
    check("та же персона уже на ядре — без повторного создания и без смены id",
          r["creates"] == [] and r["renames"] == [] and r["local"] == 0 and r["imports"] == [["nova", 2]])


def test_sync_errors():
    section("D5. Перенос: 400 и нет сети")
    code = SETUP + r"""
addNova();
core.createMode = '400';
await state.syncWithCore();
out.err = lp.localPersonas().map((p) => [p.id, p.status, p.error ?? null]);
out.errCalls = callNames();
out.msgs = store.offlineMessages('nova').length;
out.notFound = core.notFound;
out.listed = apiData.offlinePersonaList().map((p) => p.id);
// Ядро починили — следующая связь переносит и её
core.createMode = 'ok';
core.calls.length = 0;
await state.syncWithCore();
out.retry = lp.localPersonas().length;
out.retryImports = imports();
// Нет сети при создании — ждёт, без ошибки
addNova(INPUT.nova.replace('id: nova', 'id: lyra'));
core.createMode = 'net';
core.calls.length = 0;
await state.syncWithCore();
out.net = lp.localPersonas().map((p) => [p.id, p.status]);
out.netImports = imports();
// Нет сети уже на списке персон — даже не пробует создать
core.createMode = 'ok';
core.listMode = 'net';
core.calls.length = 0;
await sync.syncLocalPersonas();
out.netList = callNames();
out.netList2 = lp.localPersonas().map((p) => [p.id, p.status]);
"""
    r = node(code)
    if r is None:
        return
    check("400 → ошибка на карточке, персона остаётся на телефоне",
          r["err"] == [["nova", "error", "YAML не парсится: плохой отступ"]] and "nova" in r["listed"])
    check("переписка ждёт, ни импорта, ни кита, ни напоминаний — ни одного 404",
          r["msgs"] == 3 and r["notFound"] == [] and not any(c.endswith(":nova") for c in r["errCalls"]))
    check("после правки на ядре — перенос при следующей связи", r["retry"] == 0 and r["retryImports"] == [["nova", 2]])
    check("нет сети при создании — ждёт, без ошибки", r["net"] == [["lyra", "pending"]] and r["netImports"] == [])
    check("нет сети на списке — создание не пробует", r["netList"] == ["getPersonas"] and r["netList2"] == [["lyra", "pending"]])


def test_sync_chunks():
    section("D6. Переписка частями по 200; персоны с телефона пропускаются")
    code = SETUP + r"""
const big = [];
for (let i = 0; i < 450; i++) big.push({ id: `m${i}`, num: i, role: i % 2 ? 'assistant' : 'user', text: `сообщение ${i}`, ts: 1000 + i, source: 'local' });
big.push({ id: 'tail', num: 999, role: 'user', text: 'без ответа', ts: 2000, source: 'queued' });
localStorage.setItem('vpc-offline-msgs:connor', JSON.stringify(big));
addNova();
rem.addLocalReminder('connor', 'позвонить', Date.now() / 1000 + 7200);
out.done = await state.syncOffline();
out.imports = imports();
out.left = store.offlineMessages('connor').map((m) => m.id);
out.nova = store.offlineMessages('nova').length;
await rem.syncLocalReminders();
out.reminders = core.calls.filter((c) => c[0] === 'offlineReminders').map((c) => c[1]);
out.novaReminders = rem.localReminders().filter((r) => r.persona === 'nova').length;
out.notFound = core.notFound;
// Сбой на второй части: первая ушла, остальное ждёт
localStorage.setItem('vpc-offline-msgs:connor', JSON.stringify(big));
core.calls.length = 0;
core.imports = 0;
core.importFail = 2;
out.done2 = await state.syncOffline();
out.imports2 = imports();
out.left2 = store.offlineMessages('connor').length;
out.first2 = store.offlineMessages('connor')[0].id;
"""
    r = node(code)
    if r is None:
        return
    check("450 сообщений → части 200, 200, 50", r["imports"] == [["connor", 200], ["connor", 200], ["connor", 50]])
    check("ушло всё, кроме хвоста без ответа", r["left"] == ["tail"] and r["done"] == ["connor"])
    check("персона с телефона — без импорта (её на ядре нет)", r["nova"] == 3)
    check("напоминания: персоны с ядра — да, с телефона — ждут", r["reminders"] == ["connor"] and r["novaReminders"] == 1)
    check("ни одного 404", r["notFound"] == [])
    check("сбой второй части — первая ушла, остальное ждёт",
          len(r["imports2"]) == 2 and r["left2"] == 251 and r["first2"] == "m200" and r["done2"] == ["connor"])


def test_short_prompt():
    section("D7. Краткое описание: дозапрос, пока составляется")
    code = r"""
const sync = await import(M.personaSync);
const kit = await import(M.kit);
core.personas.set('nova', 'x');
core.personas.set('ada', 'x');
core.personas.set('vega', 'x');
const kitCalls = (id) => core.calls.filter((c) => c[0] === 'getOfflineKit' && c[1] === id).length;
core.kitShort = ['pending', 'pending', 'ready'];
let t = Date.now();
const k = await sync.followShortPrompt('nova', { pollMs: 20, maxMs: 5000 });
out.ready = { status: k.short_prompt.status, short: k.parts.short, calls: kitCalls('nova'), ms: Date.now() - t };
core.kitShort = Array(100).fill('pending');
t = Date.now();
const k2 = await sync.followShortPrompt('ada', { pollMs: 20, maxMs: 70 });
out.timeout = { status: k2.short_prompt.status, calls: kitCalls('ada'), ms: Date.now() - t };
core.kitShort = ['net', 'pending', 'ready'];
const k3 = await sync.followShortPrompt('vega', { pollMs: 20, maxMs: 5000 });
out.flaky = { status: k3.short_prompt.status, calls: kitCalls('vega') };
core.kitShort = Array(100).fill('pending');
const p1 = sync.followShortPrompt('nova', { pollMs: 20, maxMs: 60 });
const p2 = sync.followShortPrompt('nova', { pollMs: 20, maxMs: 60 });
await Promise.all([p1, p2]);
out.dedupe = kitCalls('nova') - 3;
"""
    r = node(code)
    if r is None:
        return
    check("pending → дозапрос, готово — стоп (3 запроса кита)",
          r["ready"]["status"] == "ready" and r["ready"]["short"] == "кратко с ядра" and r["ready"]["calls"] == 3)
    check("не готово до срока — стоп по сроку", r["timeout"]["status"] == "pending" and 2 <= r["timeout"]["calls"] <= 5
          and r["timeout"]["ms"] < 1000)
    check("сбой сети не принимается за «готово» — дозапрос дальше", r["flaky"] == {"status": "ready", "calls": 3})
    check("второй дозапрос той же персоны не запускается параллельно", 2 <= r["dedupe"] <= 5)


def test_forget():
    section("E. «Удалить с телефона»")
    code = SETUP + r"""
addNova();
localStorage.setItem('vpc-offline-kit-error:nova', JSON.stringify({ message: 'x', at: 1 }));
rem.askTimePlan({ persona: 'nova', notes: { ask_time: 'ASK{task}' } }, 'купить хлеб');
sync.forgetLocalPersona('nova');
out.local = lp.localPersonas().length;
out.keys = [...Array(localStorage.length).keys()].map((i) => localStorage.key(i)).filter((k) => k.includes('nova'));
out.reminders = rem.localReminders().filter((r) => r.persona === 'nova').length;
out.pending = Object.keys(JSON.parse(localStorage.getItem('vpc-offline-remind-pending') || '{}'));
out.connor = kit.getKit('connor') !== null;
"""
    r = node(code)
    if r is None:
        return
    check("персона, переписка, ошибка кита, напоминания и вопрос — убраны",
          r["local"] == 0 and r["keys"] == [] and r["reminders"] == 0 and r["pending"] == [])
    check("чужое не тронуто", r["connor"] is True)


def main():
    print("Персона, созданная на телефоне без ядра")
    if not shutil.which("node"):
        print("  (пропущено — нет Node)")
        return 0
    test_validation()
    test_local_kit()
    test_lists()
    test_sync_ok()
    test_sync_conflict()
    test_sync_memory_conflict()
    test_sync_ours()
    test_sync_errors()
    test_sync_chunks()
    test_short_prompt()
    test_forget()
    print(f"\n{ok - failures}/{ok} OK" + (f", FAIL: {failures}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

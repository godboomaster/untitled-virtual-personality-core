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
    "reply": url("offline/reply.ts"),
    "localNotes": url("offline/localNotes.ts"),
    "avatarStore": url("avatarStore.ts"),
    "shortPrompt": url("offline/shortPrompt.ts"),
    "shortCompose": url("offline/shortCompose.ts"),
    "engine": url("offline/engine.ts"),
    "memory": url("offline/memory.ts"),
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
export function isLinkMode() { return false; }
`;
const stubs = {
  '@capacitor/core': `export const Capacitor = { isPluginAvailable: () => false, isNativePlatform: () => true };
    export function registerPlugin() {
      return new Proxy({}, { get: (_, k) => (k === 'then' ? undefined : (...a) => globalThis.__plugin(String(k), ...a)) });
    }`,
  '../api': API_STUB,
  './api': API_STUB,
  '../link/client': 'export function getLinkConfig() { return null; }',
  // Модель векторов (onnxruntime-web, ?url вайта) Node не грузит — поиск только по словам
  './embed': 'export async function queryVector() { return null; } export async function embedTexts() { return null; } export function releaseEmbedder() {} export function warmEmbedder() {}',
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
  importFail: 0, imports: 0, kitShort: [], calls: [], notFound: [], avatars: new Map(),
  shortMode: 'ok', shorts: [],
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
    case 'getPersonaAvatars':
      if (core.listMode === 'net') throw new TypeError('Failed to fetch');
      return { avatars: Object.fromEntries(core.avatars) };
    case 'setPersonaAvatar': {
      const [id, url] = a;
      if (!core.personas.has(id)) throw notFound(method, id);
      core.avatars.set(id, url);
      return { ok: true };
    }
    case 'deletePersonaAvatar': {
      const [id] = a;
      if (!core.avatars.has(id)) throw notFound(method, id);
      core.avatars.delete(id);
      return { ok: true };
    }
    case 'setShortPrompt': {
      const [id, text, source, crc] = a;
      if (core.shortMode === 'net') throw new TypeError('Failed to fetch');
      if (!core.personas.has(id)) throw notFound(method, id);
      if (core.shortMode === '409') throw new ApiError(409, 'Характер персоны на ядре другой');
      if (core.shortMode === '500') throw new ApiError(500, 'ядро не готово');
      core.shorts.push([id, text, source ?? 'yaml', crc ?? null]);
      const own = (source ?? 'yaml') === 'yaml';
      return { text, source: own && text ? 'yaml' : 'auto', status: text ? 'ready' : 'pending', at: 5, chars: 10, composing: false };
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


LORE_NODE = r"""
// Сети нет: веб-поиск и погода телефона в сценарии не участвуют
globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
// IndexedDB в памяти: приложение хранит там лор
const idbData = new Map();
const idbReq = (fn) => { const r = {}; queueMicrotask(() => { r.result = fn(); r.onsuccess?.(); }); return r; };
globalThis.indexedDB = {
  open() {
    const r = {};
    queueMicrotask(() => {
      r.result = {
        transaction: () => ({ objectStore: () => ({
          get: (k) => idbReq(() => idbData.get(k)),
          put: (v, k) => idbReq(() => { idbData.set(k, v); }),
          delete: (k) => idbReq(() => { idbData.delete(k); }),
        }) }),
      };
      r.onsuccess?.();
    });
    return r;
  },
};
const lore = {
  persona: 'kaya', stamp: 's1', vectors: null,
  chunks: [
    { f: 'a.md', s: 'Любовь', t: 'Кая носит компас, подаренный Лукасом.' },
    { f: 'a.md', s: 'Еда', t: 'Кая терпеть не может рыбу.' },
    { f: 'a.md', s: 'Страх', t: 'Кая боится штиля в открытом море.' },
  ],
};
const rules = { ...INPUT.donor.rules, lore_head: 'LORE HEAD', lore_weak: 'LORE WEAK', lore: 'LORE RULE', lore_gap: 'LORE GAP' };
const kit = { ...INPUT.donor, persona: 'kaya', name: 'Кая', lore_stamp: 's1', rules, reminders: [], reminders_enabled: false, fetched_at: Date.now() };
let loreCalls = 0;
const apiBefore = globalThis.__api;
globalThis.__api = async (method, ...a) => {
  if (method === 'getOfflineLore') { loreCalls += 1; return lore; }
  return apiBefore(method, ...a);
};
const sent = [];
const gemma = [];
let cloudUp = true;
globalThis.__plugin = async (name, arg) => {
  if (name === 'cloudChat') {
    if (!cloudUp) throw Object.assign(new Error('offline'), { code: 'offline' });
    sent.push(arg.messages);
    return { text: 'Ответ.', provider: 'test' };
  }
  if (name === 'modelStatus') return { downloaded: true };
  if (name === 'localChat') { gemma.push(arg); return { text: 'Ответ Gemma.' }; }
  if (name === 'addListener') return { remove() {} };
  return {};
};
const engine = await import(INPUT.modules.engine);
const memory = await import(INPUT.modules.memory);
const kits = await import(INPUT.modules.kit);
kits.storeKit('kaya', kit);
await memory.refreshLore('kaya', kit);
await memory.refreshLore('kaya', kit);
const stored = [...idbData.keys()];
const ask = async (text) => {
  const before = sent.length;
  await engine.answerOffline('kaya', text, []);
  return sent.length > before ? sent[sent.length - 1][0].content : '';
};
const hit = await ask('что за компас у тебя?');
const small = await ask('привет');
const off = await ask('как приготовить борщ?');
cloudUp = false;
await engine.answerOffline('kaya', 'что за компас у тебя?', []);
const local = gemma[gemma.length - 1];
kit.lore_stamp = '';
await memory.refreshLore('kaya', kit);
const gone = [...idbData.keys()];
cloudUp = true;
const after = await ask('что за компас у тебя?');
Object.assign(out, { loreCalls, stored, hit, small, off, localText: local?.text ?? '', localSystem: local?.system ?? '', gone, after });
"""


def test_lore_offline():
    section("Лор на телефоне: от ядра до запроса к модели")
    out = node(LORE_NODE)
    if out is None:
        return
    r = out
    check("лор забран один раз: метка та же — повторно не качается", r["loreCalls"] == 1)
    check("лор лежит в хранилище телефона", "lore:kaya" in r["stored"])
    check("облако: кусок лора в системном тексте", "компас, подаренный Лукасом" in r["hit"]
          and "LORE HEAD" in r["hit"] and "LORE RULE" in r["hit"])
    check("облако: лишнего лора нет", "терпеть не может рыбу" not in r["hit"])
    check("облако: правило против выдумки", "LORE GAP" in r["hit"])
    check("болтовня: только правило, без кусков", "LORE GAP" in r["small"] and "компас" not in r["small"])
    check("посторонняя реплика: только правило, без кусков", "LORE GAP" in r["off"] and "компас" not in r["off"])
    check("Gemma: кусок в реплике пользователя", "компас, подаренный Лукасом" in r["localText"])
    check("Gemma: правило в системном тексте", "LORE GAP" in r["localSystem"])
    check("лор удалён на ядре: удалён и с телефона", "lore:kaya" not in r["gone"])
    check("без лора в запросе ни кусков, ни правила", bool(r["after"]) and "Лукасом" not in r["after"]
          and "LORE GAP" not in r["after"])
    check("сценарий не ходил в сеть", "WEB SEARCH RESULTS" not in r["hit"] and "WEB SEARCH RESULTS" not in r["after"])


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
    # Ни одного кита с ядра (скачан только APK) — общее из базового кита
    # приложения (baseKit.ts из offline_api.build_base_kit): правила без
    # компьютера, заметки напоминаний, гейт поиска; напоминания — по флагу
    # персоны, погода — только с городом телефона
    check("без кита с ядра: базовый кит приложения (правила без компьютера, напоминания, гейт)",
          bare["version"] == 3 and bare.get("standalone") is True and "phone app" in bare["parts"]["rules"]
          and bare["reminders_enabled"] is True and bare["env"] is None and "{when}" in bare["notes"]["set"]
          and bare["notes"]["recurring"].find("phone app") >= 0 and len(bare["search_gate"]["smalltalk"]) > 0
          and bare["rules"]["web"] != "")
    check("с китом с ядра — не standalone", not full.get("standalone"))
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


def test_standalone_bundled():
    section("D8. Телефон без компьютера: встроенный Коннор → в Коннора ядра")
    code = r"""
const state = await import(M.state);
const lp = await import(M.localPersonas);
const store = await import(M.store);
const kit = await import(M.kit);
const rem = await import(M.reminders);
const apiData = await import(M.apiData);
const bundled = { connor: 'id: connor\nname: Коннор\nfeatures:\n  reminder: true\nsystem_prompt: |\n  Я Коннор с телефона.\n' };
// Скачан только APK: снимка с ядра нет, встроенные — один раз
out.added = lp.ensureBundledPersonas(bundled, []);
out.seeded = apiData.seedOfflinePersonas(true);
out.kitLocal = kit.getKit('connor')?.local === true && kit.getKit('connor')?.standalone === true;
store.addOfflineMessage('connor', { role: 'user', text: 'привет', source: 'local' });
store.addOfflineMessage('connor', { role: 'assistant', text: 'Здравствуйте.', source: 'local' });
for (let i = 0; i < 120; i++) rem.addLocalReminder('connor', `дело ${i}`, Date.now() / 1000 + 3600 + i);
// На ядре свой Коннор (другой YAML — встроенный или его копия пользователя)
core.personas.set('connor', 'id: connor\nsystem_prompt: Коннор ядра\n');
await state.syncWithCore();
out.creates = creates();
out.renames = renames;
out.local = lp.localPersonas().length;
out.imports = imports();
out.reminderChunks = core.calls.filter((c) => c[0] === 'offlineReminders').map((c) => c[2].length);
out.remLeft = rem.localReminders().length;
// Удалил встроенного — второй раз не появится
lp.removeLocalPersona('connor');
out.again = lp.ensureBundledPersonas(bundled, []);
"""
    setup = r"""
const renames = [];
const creates = () => core.calls.filter((c) => c[0] === 'createPersona').map((c) => /^id:\s*(\S+)/m.exec(c[1])[1]);
const imports = () => core.calls.filter((c) => c[0] === 'offlineImport').map((c) => [c[1], c[2].length]);
"""
    r = node(setup + code)
    if r is None:
        return
    check("встроенный Коннор добавлен один раз, список без снимка ядра", r["added"] == ["connor"] and r["seeded"] is True)
    check("его кит — с телефона, из базового кита приложения", r["kitLocal"] is True)
    check("на ядре Коннор уже есть — не создаётся заново и не становится connor_2",
          r["creates"] == [] and r["renames"] == [] and r["local"] == 0)
    check("переписка ушла в Коннора ядра", r["imports"] == [["connor", 2]])
    check("120 напоминаний — частями 50, 50, 20 (лимит ядра)", r["reminderChunks"] == [50, 50, 20] and r["remLeft"] == 0)
    check("удалённый встроенный не возвращается", r["again"] == [])


def test_notes_offline():
    section("D9. «Запиши в заметки» без ядра: код записывает, модель — итог")
    code = r"""
const lp = await import(M.localPersonas);
const kit = await import(M.kit);
const reply = await import(M.reply);
const notes = await import(M.localNotes);
const rem = await import(M.reminders);
lp.ensureBundledPersonas({ connor: 'id: connor\nname: Коннор\nfeatures:\n  reminder: true\nsystem_prompt: Я Коннор.\n' }, []);
let cloud = null;
const sent = [];
globalThis.__plugin = async (method, opts) => {
  if (method === 'cloudChat') {
    sent.push(opts.messages[0].content);
    if (!cloud) { const e = new Error('no'); e.code = 'no_providers'; throw e; }
    return { text: cloud, provider: 'p', model: 'm' };
  }
  if (method === 'modelStatus') return { downloaded: false };
  return {};
};
// Ни облака, ни модели: итог говорит приложение, заметка записана
const r1 = await reply.replyOffline('connor', 'Коннор, запиши в заметки купить хлеб', []);
out.r1 = r1.text;
out.n1 = notes.localNotes('connor').map((n) => [n.text, n.source]);
const r2 = await reply.replyOffline('connor', 'что у меня в заметках?', []);
out.r2 = r2.text;
// С облаком: модели — итог (prompt_saved ядра), напоминание ход не берёт
cloud = 'Записал.';
const r3 = await reply.replyOffline('connor', 'запиши в заметки напомнить маме через час про лекарства', []);
out.r3 = r3.text;
out.sys3 = sent[sent.length - 1];
out.rem = rem.localReminders().length;
out.n3 = notes.localNotes('connor').length;
const r4 = await reply.replyOffline('connor', 'удали заметку 1', []);
out.sys4 = sent[sent.length - 1];
out.n4 = notes.localNotes('connor').map((n) => n.text);
// «Покажи заметки» с моделью: ответ модели + список отдельным сообщением
cloud = 'Вот они.';
await reply.replyOffline('connor', 'покажи заметки', []);
const store = await import(M.store);
out.tail = store.offlineMessages('connor').slice(-2).map((m) => [m.role, m.text]);
"""
    r = node(code)
    if r is None:
        return
    check("без модели: заметка записана кодом, ответ — приложения",
          r["n1"] == [["Купить хлеб", "chat"]] and r["r1"].startswith("Записано в заметки: «Купить хлеб»"))
    check("без модели: «что у меня в заметках?» — список", r["r2"].startswith("📝 Заметки:\n1. Купить хлеб"))
    check("с облаком: модели — итог записи (как prompt_saved ядра)",
          r["r3"] == "Записал." and "You have just written this into the user's notes" in r["sys3"])
    check("«запиши в заметки напомнить … через час» — не напоминание", r["rem"] == 0 and r["n3"] == 2)
    check("«удали заметку 1» — удалена старейшая, модели — итог",
          r["n4"] == ["Напомнить маме через час про лекарства"] and "You have just deleted" in r["sys4"])
    check("«покажи заметки» — ответ модели, потом список отдельным сообщением",
          r["tail"][0] == ["assistant", "Вот они."] and r["tail"][1][1].startswith("📝 Заметки:"))


def test_avatars_offline():
    section("D10. Аватар персоны с телефона: на телефоне, на ядро — с ней")
    code = SETUP + r"""
const av = await import(M.avatarStore);
localStorage.setItem('vpc-persona-avatars-migrated', '1');
core.listMode = 'net';
addNova();
await av.setPersonaAvatar('nova', 'data:image/png;base64,NOVA');
out.local = av.getPersonaAvatar('nova');
out.pending = JSON.parse(localStorage.getItem('vpc-persona-avatars-pending') || '{}');
// Аватар персоны ядра, заданный без связи (снимок списка), — тоже ждёт
await av.setPersonaAvatar('connor', 'data:image/png;base64,CONNOR');
// Ядро вернулось, а nova там занята чужой — переезжает в nova_2 с аватаром
core.listMode = 'ok';
core.personas.set('nova', 'id: nova\nsystem_prompt: чужая\n');
await state.syncWithCore();
out.renamed = av.getPersonaAvatar('nova_2');
out.gone = av.getPersonaAvatar('nova');
out.core = Object.fromEntries(core.avatars);
out.pendingAfter = localStorage.getItem('vpc-persona-avatars-pending');
// Удалённая с телефона персона уносит аватар
lp.addLocalPersona('id: vega\nsystem_prompt: hi\n', ['connor']);
await av.setPersonaAvatar('vega', 'data:image/png;base64,VEGA');
sync.forgetLocalPersona('vega');
out.vega = av.getPersonaAvatar('vega');
out.pendingVega = localStorage.getItem('vpc-persona-avatars-pending');
"""
    r = node(code)
    if r is None:
        return
    check("без ядра: аватар персоны с телефона — на телефоне и ждёт отправки",
          r["local"] == "data:image/png;base64,NOVA" and r["pending"] == {"nova": "set"})
    check("перенос под новым id — аватар переехал за персоной",
          r["renamed"] == "data:image/png;base64,NOVA" and r["gone"] is None)
    check("при связи оба аватара ушли на ядро (nova_2 и connor), очередь пуста",
          r["core"].get("nova_2") == "data:image/png;base64,NOVA"
          and r["core"].get("connor") == "data:image/png;base64,CONNOR" and r["pendingAfter"] is None)
    check("удалённая с телефона персона — без аватара и без отправки",
          r["vega"] is None and r["pendingVega"] is None)


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


SHORT_CHARACTER = (
    "Ты — Вега, штурман дальнего корабля. Говоришь коротко и по делу, с лёгкой иронией.\n\n"
    "Нельзя: грубить, выдумывать факты о собеседнике."
)
SHORT_CARD = (
    "Ты — Вега, штурман дальнего корабля, спокойная и ироничная.\n\n"
    "Как ты говоришь:\n- коротко и по делу\n- с морскими словечками\n- с лёгкой иронией\n\n"
    "Никогда:\n- не грубишь\n- не выдумываешь факты о собеседнике\n\n"
    "Примеры ответов:\n(привет) Свистать всех наверх — здравствуй!\n(как дела?) Курс ровный, ветер попутный."
)
CLEAN_CASES = [
    SHORT_CARD,
    "<think>сначала подумаю</think>```text\n" + SHORT_CARD + "\n```",
    "────\n" + SHORT_CARD + "\n\n\n\n====\n",
    "коротко",
    ("Абзац номер один, довольно длинный. " * 30 + "\n\n") * 4,
    "строка\r\n" * 120,
]


def test_short_offline():
    section("D11. Краткое описание без ядра: своё, составленное телефоном, перенос")
    from app.features import short_prompt as sp
    code = SETUP + r"""
const sp = await import(M.shortPrompt);
const comp = await import(M.shortCompose);
out.clean = INPUT.cleanCases.map((c) => sp.cleanCard(c));
out.manual = sp.manualPrompt(INPUT.character);
addNova();
sp.setPendingShort('nova', { text: 'Нова: своё описание', source: 'yaml', at: 1000 });
let k = kit.getKit('nova');
out.own = { short: k.parts.short, src: k.short_prompt?.source, st: k.short_prompt?.status, local: !!k.local };
sp.setPendingShort('connor', { text: 'АВТО', source: 'auto', at: 2000, crc: 1 });
out.autoStale = kit.getKit('connor').parts.short;
const pluginCalls = [];
globalThis.__plugin = async (m, o) => {
  pluginCalls.push([m, o]);
  if (m === 'cloudChat') return { text: '```\n' + INPUT.card + '\n```', provider: 'groq', model: 'x' };
  return {};
};
const res = await comp.composeOnPhone('connor');
const kc = kit.getKit('connor');
const cc = pluginCalls.find((c) => c[0] === 'cloudChat')?.[1];
out.compose = {
  provider: res.provider, same: res.text === INPUT.card, short: kc.parts.short === INPUT.card,
  src: kc.short_prompt.source, sys: cc?.messages[0].content.startsWith(INPUT.cardPrompt),
  user: cc?.messages[1].content === INPUT.donor.parts.character, max: cc?.maxTokens,
  gemma: pluginCalls.some((c) => c[0] === 'localChat'),
};
globalThis.__plugin = async (m) => {
  if (m === 'cloudChat') throw Object.assign(new Error('нет ключей'), { code: 'no_providers' });
  return {};
};
try {
  await comp.composeOnPhone('connor');
  out.fail = 'не упало';
} catch (e) {
  out.fail = [e.constructor.name, e.reason, e.cloud];
}
globalThis.__plugin = async () => ({});
core.kitShort = ['ready', 'ready', 'ready'];
await state.syncWithCore();
await sleep(50);
out.shorts = core.shorts;
out.pendingAfter = localStorage.getItem('vpc-short-prompt-pending');
const order = callNames();
out.order = [order.indexOf('createPersona'), order.indexOf('setShortPrompt:nova'), order.lastIndexOf('getOfflineKit:nova')];
"""
    extra = {"cleanCases": CLEAN_CASES, "character": SHORT_CHARACTER, "card": SHORT_CARD,
             "cardPrompt": sp.CARD_PROMPT}
    r = node(code, extra)
    if r is None:
        return
    check("cleanCard — как clean_card ядра (6 случаев)", r["clean"] == [sp.clean_card(c) for c in CLEAN_CASES])
    check("промпт для своей нейросети — как manual_prompt ядра", r["manual"] == sp.manual_prompt(SHORT_CHARACTER))
    check("своё без ядра — сразу в ките персоны с телефона",
          r["own"] == {"short": "Нова: своё описание", "src": "yaml", "st": "ready", "local": True})
    check("составленное по другому характеру — не накладывается", r["autoStale"] == "Коннор кратко")
    c = r["compose"]
    check("составлено облаком персоны: тот же запрос, без Gemma, в ките сразу",
          c["provider"] == "groq" and c["same"] and c["short"] and c["src"] == "auto" and c["sys"] and c["user"]
          and c["max"] == 1200 and not c["gemma"])
    check("составить некому — ComposeFailed no_route с кодом облака",
          r["fail"] == ["ComposeFailed", "no_route", "no_providers"])
    crc = zlib.crc32(DONOR["parts"]["character"].encode("utf-8"))
    check("при связи: составленное — ядру с crc характера, своё перенесённой — после её создания",
          sorted(map(tuple, r["shorts"])) == sorted([("connor", SHORT_CARD, "auto", crc),
                                                     ("nova", "Нова: своё описание", "yaml", None)]))
    i_create, i_short, i_kit = r["order"]
    check("порядок: создание персоны → её описание → кит", 0 <= i_create < i_short < i_kit)
    check("очередь пуста", r["pendingAfter"] is None)

    code = SETUP + r"""
const sp = await import(M.shortPrompt);
core.personas.set('nova', 'id: nova\nsystem_prompt: другая\n');
addNova();
sp.setPendingShort('nova', { text: 'своё', source: 'yaml', at: 1 });
sp.setPendingShort('connor', { text: 'своё К', source: 'yaml', at: 2 });
core.shortMode = 'net';
await state.syncWithCore();
out.afterNet = Object.keys(JSON.parse(localStorage.getItem('vpc-short-prompt-pending') || '{}')).sort();
core.shortMode = '409';
await sync.syncShortPrompts();
out.after409 = localStorage.getItem('vpc-short-prompt-pending');
"""
    r = node(code)
    if r is None:
        return
    check("id занят — правка переехала на nova_2; нет сети — ждёт", r["afterNet"] == ["connor", "nova_2"])
    check("ядро отклонило (409) — правка снята", r["after409"] is None)


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
    test_standalone_bundled()
    test_notes_offline()
    test_avatars_offline()
    test_short_prompt()
    test_short_offline()
    test_forget()
    test_lore_offline()
    print(f"\n{ok - failures}/{ok} OK" + (f", FAIL: {failures}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Библиотека готовых скинов и промпт для своей нейросети.

  A. Тексты промпта генерации выгружены из ядра без расхождений
     (scripts/gen_skin_prompts.py --check → web/src/skins/skinGenPrompts.ts).
  B. Сборка промпта на клиенте (web/src/skins/manualPrompt.ts, под Node)
     совпадает с ядром (app/api/skin_gen_api.py) на одних и тех же входах:
     промпт экрана (prepare → build_messages: шаблон и скин-основа, ru/en,
     с направлением и без, исправление), промпт направлений, нормализация и
     блок направления (включая округление смеси цветов как у round() Python),
     разбор ответа с направлениями, strip_base/strip_assets.
  C. Встроенные скины в хранилище (skinStore.ts под Node): шесть в
     библиотеке в локальном и серверном режимах, файлы грузятся из чанков
     приложения (не с сервера), персонам сами не назначаются, «удаление»
     только скрывает (вернуть — restore), правка цветов — копия, встроенный
     не меняется; id принимает сервер; имена и описания есть в ru/en.
  D. Каждый экран каждого встроенного скина в браузере (vite dev + headless
     Chrome, свой порт, мёртвый адрес ядра): validateSkin, все hook-точки
     шаблона на месте, runtime-проверка smokeTest (ru/светлая и en/тёмная),
     одинаковая shell-тема у трёх экранов, плоскость CSS (ни размытых теней,
     ни blur/свечений), узкий экран 390 px без горизонтальной прокрутки:
     поле ввода и отправка видны, боковые панели — выдвижные по кнопкам,
     вкладки досье переключаются, аватар комнаты на сцене.
  E. Раздел «Скины» в приложении (мок-режим): встроенные в библиотеке, никому
     не назначены; назначение персоне — чат рисуется скином; «Скрыть» и
     «Восстановить встроенные»; «Промпт для своей нейросети» копирует промпт
     экрана, принимает ответ (файл встроенного скина) и передаёт его в
     загрузку, где runtime-проверка проходит.

Ядро и LLM не вызываются. Vite запускается на свободном порту и
останавливается по PID.

Запуск: OLLAMA_URL=http://127.0.0.1:9 python3 -m scripts.test_skin_library
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("VPC_DATA_DIR", tempfile.mkdtemp(prefix="skin_library_"))
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")

WEB = ROOT / "web"
SRC = WEB / "src"
PRESETS = ["ink", "form", "pixel", "netrunner", "retro", "metro"]
SCREENS = ["chat", "dossier", "room"]
_BROWSER_CACHE = Path.home() / "Library/Caches/ms-playwright"
_CHROME_CANDIDATES = sorted(
    _BROWSER_CACHE.glob("chromium-*/chrome-mac*/*.app/Contents/MacOS/*"), reverse=True)
CHROME = next((p for p in _CHROME_CANDIDATES if p.is_file() and os.access(p, os.X_OK)),
              None)

ok = 0
failures = 0


def check(name, cond, detail=""):
    global ok, failures
    ok += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}{'' if cond or not detail else ' — ' + detail}")
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


def preset_file(pid: str, screen: str) -> str:
    return (SRC / "skins" / "presets" / f"{pid}.{screen}.html").read_text(encoding="utf-8")


def template(screen: str) -> str:
    return (SRC / "skins" / f"skin-template.{screen}.html").read_text(encoding="utf-8")


# Модули под Node: «.ts» без расширения, заглушки api/apiData, файлы
# «?raw» — строкой (как у vite)
NODE_PRELUDE = r"""
import { registerHooks } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const API_STUB = `
export class ApiError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
}
export const api = new Proxy({}, { get: (_, k) => (...a) => globalThis.__api(String(k), ...a) });
`;
const stubs = {
  '../api': API_STUB,
  '../apiData': 'export function useApiOnline() { return false; }',
};
registerHooks({
  resolve(spec, ctx, next) {
    if (spec in stubs) return { url: 'data:text/javascript,' + encodeURIComponent(stubs[spec]), shortCircuit: true };
    if (spec.startsWith('.') && !/\.[a-z]+(\?raw)?$/.test(spec)) return next(spec + '.ts', ctx);
    return next(spec, ctx);
  },
  load(url, ctx, next) {
    if (url.endsWith('?raw')) {
      const text = readFileSync(fileURLToPath(url.slice(0, -4)), 'utf8');
      return { format: 'module', source: 'export default ' + JSON.stringify(text), shortCircuit: true };
    }
    return next(url, ctx);
  },
});
const lsMap = new Map();
globalThis.localStorage = {
  getItem: (k) => (lsMap.has(k) ? lsMap.get(k) : null),
  setItem: (k, v) => { lsMap.set(k, String(v)); },
  removeItem: (k) => { lsMap.delete(k); },
  key: (i) => [...lsMap.keys()][i] ?? null,
  clear: () => lsMap.clear(),
  get length() { return lsMap.size; },
};
const INPUT = JSON.parse(readFileSync(process.env.VPC_TEST_INPUT, 'utf8'));
const OUT = {};
"""
NODE_EPILOGUE = "\nwriteFileSync(process.env.VPC_TEST_OUTPUT, JSON.stringify(OUT));\n"


def node(code: str, payload: dict) -> dict | None:
    with tempfile.TemporaryDirectory(prefix="skinlib_node_") as tmp:
        inp, out = Path(tmp) / "in.json", Path(tmp) / "out.json"
        inp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        res = subprocess.run(["node", "--input-type=module", "-e", NODE_PRELUDE + code + NODE_EPILOGUE],
                             capture_output=True, text=True, timeout=180, cwd=WEB,
                             env={**os.environ, "VPC_TEST_INPUT": str(inp), "VPC_TEST_OUTPUT": str(out)})
        if res.returncode != 0:
            check(f"Node: {res.stderr.strip()[:1500]}", False)
            return None
        return json.loads(out.read_text(encoding="utf-8"))


def first_diff(a: str, b: str) -> str:
    i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
    return f"@{i}: ядро …{a[max(0, i - 60):i + 60]!r}… / клиент …{b[max(0, i - 60):i + 60]!r}…"


# ─── A. Выгрузка текстов промпта ───

def test_export():
    section("A. Тексты промпта выгружены из ядра (gen_skin_prompts --check)")
    res = subprocess.run([sys.executable, "-m", "scripts.gen_skin_prompts", "--check"],
                         capture_output=True, text=True, cwd=ROOT, env=os.environ)
    check("--check: skinGenPrompts.ts совпадает с ядром", res.returncode == 0)
    from app.api import skin_gen_api as sg
    text = (SRC / "skins" / "skinGenPrompts.ts").read_text(encoding="utf-8")
    body = json.loads(text[text.index("{"):text.rindex("}") + 1])
    check("системный промпт генерации — как SYSTEM_PROMPT", body["system"] == sg.SYSTEM_PROMPT)
    check("системный промпт направлений — как DIRECTION_SYSTEM_PROMPT",
          body["directionSystem"] == sg.DIRECTION_SYSTEM_PROMPT)
    check("функциональный CSS — как FUNCTIONAL_CSS", body["functionalCss"] == sg.FUNCTIONAL_CSS)


# ─── B. Сборка промпта на клиенте = ядро ───

D1 = {"name": "Ночной гроссбух", "subject": "Журнал смотрителя маяка во время шторма", "mood": "тихо, сосредоточенно",
      "palette": [{"role": "background", "hex": "#1b1a17", "reason": "сажа на стекле лампы"},
                  {"role": "surface", "hex": "#26241f"}, {"role": "ink", "hex": "#ece4d4"},
                  {"role": "muted", "hex": "#9a917f"}, {"role": "accent", "hex": "#d9822b", "reason": "огонь"},
                  {"role": "accent2", "hex": "#4f7c8a"}],
      "fonts": {"display": "Didot, 'Bodoni 72', serif", "text": "'Iowan Old Style', Palatino, Georgia, serif",
                "mono": "", "why": "печатный журнал"},
      "layout": "персоны — корешок, реплики — записи", "signature": "штормовая линейка",
      "texture": "волосяные линии", "motion": "запись появляется", "avoid": ["неон", "стекло", "эмодзи"]}
D2 = {"name": "  Очень   длинное имя направления, которое точно не влезает в лимит шестидесяти символов  ",
      "subject": "Касса  кинотеатра\nв 1974 году",
      "palette": {"bg": "#FFF", "text": {"hex": "цвет #2d2e29 — чернила"}, "primary": "#5e7449",
                  "Second accent": "#a4544b", "фон": "#000000", "lol": "nothex"},
      "fonts": "Optima, Candara, sans-serif,", "avoid": "клише; ещё клише\nтретье", "note": "  светлее  , без анимаций "}
D3 = {"name": "Округление", "subject": "проверка смеси цветов",
      "palette": [{"role": "background", "hex": "#000000"}, {"role": "ink", "hex": "#191919"},
                  {"role": "accent", "hex": "#abc"}, {"role": "ink", "hex": "#ffffff"}],
      "fonts": {"display": "Futura, sans-serif"}}
D4 = {"name": "Без акцента", "subject": "x", "palette": [{"role": "background", "hex": "#000"}, {"role": "ink", "hex": "#fff"}],
      "fonts": {"text": "Georgia"}}
D5 = {"name": "Плохой стек", "subject": "s", "palette": [{"role": "bg", "hex": "#101010"}, {"role": "ink", "hex": "#fafafa"},
                                                          {"role": "accent", "hex": "#ff0000"}],
      "fonts": {"display": "url(http://x) serif", "text": "Menlo, monospace", "mono": "\"Unclosed, serif"}}
D6 = {"name": "Длинный стек", "subject": "s", "palette": [{"role": "background", "hex": "#101010"},
                                                           {"role": "ink", "hex": "#fafafa"}, {"role": "accent", "hex": "#00ff00"}],
      "fonts": {"display": ", ".join(f"Font Number {i}" for i in range(30)), "text": "Georgia"}}
D7 = ["не объект"]
D8 = {"name": "🌙" * 70, "subject": "луна " * 120, "palette": [{"role": "background", "hex": "#0a0a0a"},
                                                             {"role": "ink", "hex": "#f0f0f0"}, {"role": "accent", "hex": "#e0b000"}],
      "fonts": {"text": "Georgia"}, "avoid": ["x" * 300] * 12}


def parse_samples():
    j = lambda o: json.dumps(o, ensure_ascii=False)  # noqa: E731
    p3 = ('{"directions": [{"name": "Кавычки", "subject": "B", "palette": [{"role": "background", "hex": "#111111"}, '
          '{"role": "ink", "hex": "#eeeeee"}, {"role": "accent", "hex": "#ff0000"}], "fonts": {\n'
          '  "display": ""Iowan Old Style", Palatino, serif",\n  "text": "Georgia, serif"\n}}]}')
    return [
        "```json\n" + j({"directions": [D1, D2]}) + "\n```",
        "Вот идеи:\n" + j({"directions": [D1]})[:-2] + ",]}\nУдачи",
        p3,
        "Одно направление: " + j(D1),
        j([D3, D4, D5]),
        "тут нет json, только слово palette",
        "{битый {\"directions\": [" + j(D6) + "]}",
        j({"directions": [D4]}),
        "",
    ]


ASSET = "data:image/png;base64," + "A" * 320


def screen_cases() -> list[dict]:
    """Входы промпта экрана: каждый экран × основа (шаблон / скин) × язык,
    направление по кругу (нет, D1, D2 с заметкой), с описанием и без,
    обычная генерация и исправление."""
    cases = []
    dirs = [None, D1, D2]
    contract = "  # Контракт\nправила скина\n  "
    k = 0
    for screen in SCREENS:
        for kind in ("template", "skin"):
            for loc in ("ru", "en"):
                for fix in (False, True):
                    base = template(screen) if kind == "template" else preset_file(PRESETS[k % len(PRESETS)], screen)
                    if kind == "skin" and k % 3 == 0:
                        # Inline-ассеты основы — метками vpc-asset:N (и повтор той же картинки)
                        base = base.replace("</body>", f'<img src="{ASSET}" alt=""><img src="{ASSET}" alt=""></body>')
                    prev = None
                    if fix:
                        prev = preset_file(PRESETS[(k + 1) % len(PRESETS)], screen).replace(
                            "</body>", f'<i style="background:url({ASSET})"></i></body>')
                    cases.append({
                        "screen": screen, "description": "" if k % 2 else "тёмная библиотека, свечи",
                        "base_html": base, "base_kind": kind, "contract_doc": contract, "locale": loc,
                        "previous_html": prev,
                        "errors": ["Не найдена обязательная точка: поле ввода.", "Ошибка JS: x is not defined"] if fix else [],
                        "direction": dirs[k % 3],
                    })
                    k += 1
    return cases


NODE_PROMPTS = r"""
const M = await import(new URL('./src/skins/manualPrompt.ts', 'file://' + process.cwd() + '/').href);
OUT.screens = INPUT.screens.map((c) => {
  const assets = [];
  const base = M.prepareBaseHtml(c.base_html, c.base_kind, assets);
  const prev = c.previous_html == null ? null : M.stripAssets(c.previous_html, assets);
  const direction = c.direction == null ? null : M.normalizeDirection(c.direction);
  const messages = M.buildScreenMessages({
    screen: c.screen, description: c.description, baseHtml: base, baseKind: c.base_kind,
    contractDoc: c.contract_doc, locale: c.locale, previousHtml: prev, errors: c.errors, direction,
  });
  return { messages, assets, restored: M.restoreAssets(base, assets) };
});
OUT.dirMessages = INPUT.dirRequests.map((r) => M.buildDirectionMessages(r.description, r.locale, r.count, r.exclude));
OUT.normalized = INPUT.rawDirections.map((d) => M.normalizeDirection(d));
OUT.formatted = INPUT.rawDirections.map((d) => { const n = M.normalizeDirection(d); return n ? M.formatDirection(n) : null; });
OUT.parsed = INPUT.parseTexts.map((t) => M.parseDirections(t));
OUT.parsedLimit = M.parseDirections(INPUT.parseTexts[0], 1);
OUT.stripped = INPUT.strip.map((s) => M.stripBase(s.html, s.kind));
OUT.joined = M.chatbotPrompt([{ role: 'system', content: 'S' }, { role: 'user', content: 'U' }]);
OUT.extract = INPUT.answers.map((a) => M.extractHtmlDocument(a));
"""


def test_prompt_parity():
    section("B. Промпт на клиенте = промпт ядра (manualPrompt.ts под Node)")
    from app.api import skin_gen_api as sg
    cases = screen_cases()
    dir_reqs = [
        {"description": "", "locale": "ru", "count": 3, "exclude": []},
        {"description": "уютная мастерская часовщика", "locale": "en", "count": 1, "exclude": []},
        {"description": "  тёмный  ", "locale": "ru", "count": 2, "exclude": ["Ночной гроссбух", "  ", "x" * 120]},
    ]
    raws = [D1, D2, D3, D4, D5, D6, D7, D8]
    texts = parse_samples()
    strip = [{"html": template(s), "kind": k} for s in SCREENS for k in ("template", "skin")]
    strip += [{"html": preset_file(p, s), "kind": "skin"} for p in PRESETS for s in SCREENS]
    answers = ["Вот файл:\n```html\n<!DOCTYPE html><html><body>x</body></html>\n```\nГотово.",
               "<html><body>оборвано", "нет документа"]
    r = node(NODE_PROMPTS, {"screens": cases, "dirRequests": dir_reqs, "rawDirections": raws,
                            "parseTexts": texts, "strip": strip, "answers": answers})
    if r is None:
        return

    bad = []
    for i, (c, got) in enumerate(zip(cases, r["screens"])):
        req = sg.SkinGenRequest(**c)
        msgs, assets = sg.prepare(req)
        if got["messages"] != msgs or got["assets"] != assets:
            a = "\n".join(m["content"] for m in msgs)
            b = "\n".join(m["content"] for m in got["messages"])
            bad.append(f"#{i} {c['screen']}/{c['base_kind']}/{c['locale']}: {first_diff(a, b)}")
    check(f"промпт экрана: {len(cases)} случаев (экраны × основа × язык × направление × исправление) — "
          f"совпадают с prepare/build_messages ядра", not bad)
    for b in bad[:3]:
        print("     ", b[:400])
    with_asset = [(c, g) for c, g in zip(cases, r["screens"]) if ASSET in c["base_html"]]
    check("inline-ассеты основы: две одинаковые картинки — одна метка vpc-asset:0, restoreAssets возвращает их",
          with_asset and all(g["assets"] == [ASSET] and "vpc-asset:0" in g["messages"][1]["content"]
                             and ASSET not in g["messages"][1]["content"] and g["restored"].count(ASSET) == 2
                             for c, g in with_asset))

    exp_dir = [sg.build_direction_messages(sg.SkinDirectionRequest(**q)) for q in dir_reqs]
    check("промпт направлений (build_direction_messages) совпадает", r["dirMessages"] == exp_dir)

    exp_norm = [sg.normalize_direction(d) for d in raws]
    check("нормализация направлений (алиасы ролей, hex, стеки шрифтов, лимиты, заметка) совпадает",
          r["normalized"] == exp_norm)
    check("…негодные направления отбрасываются и там и там",
          [x is None for x in r["normalized"]] == [x is None for x in exp_norm] and exp_norm.count(None) >= 2)
    exp_fmt = [sg.format_direction(n) if n else None for n in exp_norm]
    check("блок направления в промпте (format_direction, shell-переменные) совпадает", r["formatted"] == exp_fmt)
    n3 = sg.normalize_direction(D3)
    check("смесь цветов округляется как round() Python (25 × 0,18 = 4,5 → 4)",
          dict(sg.shell_vars(n3))["--vpc-shell-border"] == "#040404" and "#040404" in r["formatted"][2])

    exp_parsed = [sg.parse_directions(t) for t in texts]
    check(f"разбор ответа с направлениями: {len(texts)} образцов (ограды, висячие запятые, кавычки в шрифтах, "
          "одно направление, массив, мусор)", r["parsed"] == exp_parsed)
    check("…лимит числа направлений", r["parsedLimit"] == sg.parse_directions(texts[0], 1))

    exp_strip = [sg.strip_base(s["html"], s["kind"]) for s in strip]
    check(f"strip_base: шаблоны и {len(PRESETS) * 3} файлов встроенных скинов — совпадает", r["stripped"] == exp_strip)
    check("системная и пользовательская части склеены одним текстом", r["joined"] == "S\n\n---\n\nU")
    ex = r["extract"]
    check("ответ чат-бота: документ из текста с оградой, оборванный, без документа",
          ex[0]["html"] == "<!DOCTYPE html><html><body>x</body></html>" and not ex[0]["truncated"]
          and ex[1]["truncated"] and ex[2]["html"] is None)


# ─── C. Встроенные скины в хранилище ───

NODE_STORE = r"""
const base = 'file://' + process.cwd() + '/src/skins/skinStore.ts';
const calls = [];
globalThis.__api = async (name, ...args) => {
  calls.push([name, ...args]);
  throw new Error('core down');
};
const S = await import(base);
await S.restoreBuiltinSkins();
const list = S.listSkins();
OUT.local = {
  ids: list.map((e) => e.id),
  builtin: list.map((e) => !!e.builtin),
  keys: list.map((e) => [e.nameKey, e.descKey]),
  screens: list.map((e) => Object.keys(e.screens).sort()),
  assignments: JSON.parse(localStorage.getItem('vpc-skin-library') || '{"assignments":{}}').assignments,
};
const files = await S.loadSkinFiles('builtin-ink');
OUT.local.files = files;
OUT.local.sizes = S.findEntry('builtin-ink').sizes;
OUT.local.getSkinCalls = calls.filter((c) => c[0] === 'getSkin').length;
await S.assignSkin('connor', 'builtin-ink');
OUT.local.assigned = JSON.parse(localStorage.getItem('vpc-skin-library')).assignments;
await S.deleteSkin('builtin-ink');
OUT.local.afterHide = {
  listed: S.listSkins().some((e) => e.id === 'builtin-ink'),
  stillKnown: !!S.findEntry('builtin-ink'),
  assignments: JSON.parse(localStorage.getItem('vpc-skin-library')).assignments,
  hidden: JSON.parse(localStorage.getItem('vpc-skin-hidden-builtins') || '[]'),
};
await S.restoreBuiltinSkins();
OUT.local.restored = S.listSkins().some((e) => e.id === 'builtin-ink');
const fork = await S.updateSkin('builtin-form', { colors: { '--accent': '#ff0000' } });
const formFiles = await S.loadSkinFiles('builtin-form');
const forkFiles = await S.loadSkinFiles(fork.id);
OUT.local.fork = {
  builtin: !!fork.builtin, id: fork.id, colors: fork.colors,
  sameFiles: ['chat', 'dossier', 'room'].every((s) => forkFiles[s] === formFiles[s] && !!forkFiles[s]),
  originalColors: S.findEntry('builtin-form').colors,
  originalListed: S.listSkins().some((e) => e.id === 'builtin-form'),
};

// Серверный режим: отдельный экземпляр модуля
localStorage.clear();
const server = { calls: [], hidden: [] };
globalThis.__api = async (name, ...args) => {
  server.calls.push([name, ...args]);
  if (name === 'getSkins') return { skins: [], hidden_builtins: server.hidden };
  if (name === 'getSkinAssignments') return { assignments: {} };
  if (name === 'deleteSkin') { server.hidden.push(args[0]); return { ok: true }; }
  if (name === 'restoreBuiltinSkins') { server.hidden = []; return { restored: [] }; }
  if (name === 'setPersonaSkin') return { ok: true };
  throw new Error('unexpected ' + name);
};
const T = await import(base + '?server');
await T.restoreBuiltinSkins();
OUT.server = {
  ids: T.listSkins().map((e) => e.id),
  setCallsBefore: server.calls.filter((c) => c[0] === 'setPersonaSkin').length,
};
const pix = await T.loadSkinFiles('builtin-pixel');
OUT.server.pixelLoaded = ['chat', 'dossier', 'room'].every((s) => typeof pix[s] === 'string' && pix[s].includes('data-vpc-screen="' + s + '"'));
OUT.server.getSkinCalls = server.calls.filter((c) => c[0] === 'getSkin').length;
await T.assignSkin('verso', 'builtin-retro');
OUT.server.setCall = server.calls.find((c) => c[0] === 'setPersonaSkin');
await T.deleteSkin('builtin-retro');
OUT.server.deleteCall = server.calls.find((c) => c[0] === 'deleteSkin');
OUT.server.listedAfterHide = T.listSkins().some((e) => e.id === 'builtin-retro');
"""


def test_store():
    section("C. Встроенные скины в хранилище (skinStore.ts под Node)")
    r = node(NODE_STORE, {})
    if r is None:
        return
    loc, srv = r["local"], r["server"]
    ids = [f"builtin-{p}" for p in PRESETS]
    check("локальный режим: шесть встроенных в библиотеке", loc["ids"][:6] == ids and all(loc["builtin"][:6]))
    check("…у каждого i18n-ключи имени и описания",
          loc["keys"][:6] == [[f"skin.preset.{p}", f"skin.preset.{p}.desc"] for p in PRESETS])
    check("…у каждого все три экрана", all(s == ["chat", "dossier", "room"] for s in loc["screens"][:6]))
    check("…никому не назначены", loc["assignments"] == {})
    disk = {s: preset_file("ink", s) for s in SCREENS}
    check("файлы встроенного грузятся из чанков приложения — те же, что на диске",
          loc["files"] == disk and loc["getSkinCalls"] == 0)
    check("…размеры экранов известны после загрузки", all(loc["sizes"].get(s, 0) > 1000 for s in SCREENS))
    check("назначение встроенного персоне сохраняется", loc["assigned"] == {"connor": "builtin-ink"})
    h = loc["afterHide"]
    check("«удаление» встроенного только скрывает: из списка ушёл, сам скин остался, с персоны снят",
          not h["listed"] and h["stillKnown"] and h["assignments"] == {} and h["hidden"] == ["builtin-ink"])
    check("«Восстановить встроенные» возвращает его", loc["restored"])
    f = loc["fork"]
    check("правка цветов встроенного создаёт пользовательскую копию с теми же файлами",
          not f["builtin"] and not f["id"].startswith("builtin-") and f["colors"] == {"--accent": "#ff0000"} and f["sameFiles"])
    check("…встроенный не изменился", f["originalColors"] == {} and f["originalListed"])
    check("серверный режим: шесть встроенных в библиотеке, никому не назначены",
          srv["ids"][:6] == ids and srv["setCallsBefore"] == 0)
    check("…файлы встроенного — из чанков, без запроса к серверу", srv["pixelLoaded"] and srv["getSkinCalls"] == 0)
    check("…назначение уходит на сервер с id builtin-*", srv["setCall"] == ["setPersonaSkin", "verso", "builtin-retro"])
    check("…скрытие — DELETE встроенного на сервере", srv["deleteCall"] == ["deleteSkin", "builtin-retro"]
          and not srv["listedAfterHide"])

    from app.api import skins_api
    check("сервер принимает id всех встроенных", all(skins_api._BUILTIN_ID_RE.match(i) for i in ids))
    ru = (SRC / "i18n" / "ru.ts").read_text(encoding="utf-8")
    en = (SRC / "i18n" / "en.ts").read_text(encoding="utf-8")
    check("имена и описания встроенных — в ru и en",
          all(f"'skin.preset.{p}':" in t and f"'skin.preset.{p}.desc':" in t for p in PRESETS for t in (ru, en)))


# ─── D. Встроенные скины в браузере ───

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Vite:
    """vite dev на свободном порту с мёртвым адресом ядра; стоп — по PID."""

    def __init__(self):
        self.port = free_port()
        self.dead = free_port()
        self.proc = None

    def __enter__(self):
        env = {**os.environ, "VITE_API_URL": f"http://127.0.0.1:{self.dead}"}
        self.proc = subprocess.Popen(
            ["node", "node_modules/vite/bin/vite.js", "--port", str(self.port), "--strictPort", "--host", "127.0.0.1"],
            cwd=WEB, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=1)
                return self
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("vite не поднялся")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)


BROWSER_CHECK = r"""
async ({ ids, screens }) => {
  const store = await import('/src/skins/skinStore.ts');
  const engine = await import('/src/skins/engine.ts');
  const smoke = await import('/src/skins/smokeTest.ts');
  const prev = await import('/src/components/skins/usePreviewStates.ts');
  const ui = await import('/src/components/skins/skinUi.ts');
  const mock = await import('/src/mockData.ts');
  const ru = (await import('/src/i18n/ru.ts')).default;
  const en = (await import('/src/i18n/en.ts')).default;
  const payloads = await import('/src/skins/payloads.ts');
  const mk = (dict) => (k, v) => {
    let s = dict[k] ?? ru[k] ?? k;
    if (v) for (const [n, x] of Object.entries(v)) s = s.replaceAll('{' + n + '}', String(x));
    return s;
  };
  const out = {};
  for (const id of ids) {
    const files = await store.loadSkinFiles('builtin-' + id);
    for (const s of screens) {
      const html = files[s];
      const r = { present: typeof html === 'string' };
      out[id + '.' + s] = r;
      if (!r.present) continue;
      const v = engine.validateSkin(html);
      Object.assign(r, { valid: v.ok, errors: v.errors, screens: v.screens, contract: v.meta.contract, name: v.meta.name });
      r.hooks = ui.checkSkinMarkup(html, s, ui.TEMPLATES[s], 'ru', mk(ru));
      r.shell = { light: engine.extractShellTheme(html, 'light'), dark: engine.extractShellTheme(html, 'dark') };
      r.smoke = {};
      for (const [lang, theme, dict, data] of [['ru', 'light', ru, mock.mockRu], ['en', 'dark', en, mock.mockEn]]) {
        const t = mk(dict);
        const persona = data.personas[0];
        const env = payloads.buildSkinEnv({ theme, locale: lang, t, personaName: persona.name });
        const states = prev.buildPreviewStates(persona, data, t, env);
        const res = await smoke.runSkinSmokeTest(html, s, states[s], { lang });
        r.smoke[lang + '-' + theme] = res.errors;
      }
    }
  }
  return out;
}
"""

# Рендер экрана в iframe заданного размера тем же путём, что SkinFrame
RENDER = r"""
async ({ id, screen, theme, w, h }) => {
  const engine = await import('/src/skins/engine.ts');
  const prev = await import('/src/components/skins/usePreviewStates.ts');
  const mock = await import('/src/mockData.ts');
  const ru = (await import('/src/i18n/ru.ts')).default;
  const payloads = await import('/src/skins/payloads.ts');
  const html = (await import(`/src/skins/presets/${id}.${screen}.html?raw`)).default;
  const t = (k, v) => { let s = ru[k] ?? k; if (v) for (const [n, x] of Object.entries(v)) s = s.replaceAll('{' + n + '}', String(x)); return s; };
  const persona = mock.mockRu.personas[0];
  const env = payloads.buildSkinEnv({ theme, locale: 'ru', t, personaName: persona.name });
  let state = prev.buildPreviewStates(persona, mock.mockRu, t, env)[screen];
  if (screen === 'chat') state = prev.withBusyChat(state);
  document.querySelectorAll('#t-host').forEach((n) => n.remove());
  const host = document.createElement('div');
  host.id = 't-host';
  host.style.cssText = `position:fixed;left:0;top:0;width:${w}px;height:${h}px;z-index:2147483647;`;
  const iframe = document.createElement('iframe');
  iframe.setAttribute('sandbox', 'allow-scripts');
  iframe.name = 'tskin';
  iframe.style.cssText = `width:${w}px;height:${h}px;border:0;display:block;`;
  host.appendChild(iframe);
  document.body.appendChild(host);
  const ready = new Promise((resolve) => {
    const on = (e) => {
      if (e.source !== iframe.contentWindow || !e.data || e.data.vpc !== 'skin' || e.data.type !== 'ready') return;
      iframe.contentWindow.postMessage({ vpc: 'host', type: 'state', payload: state }, '*');
      window.removeEventListener('message', on);
      setTimeout(() => resolve(true), 400);
    };
    window.addEventListener('message', on);
    setTimeout(() => resolve(false), 5000);
  });
  iframe.srcdoc = engine.prepareSkin(html, screen, {});
  return await ready;
}
"""

# Внутри документа скина: переполнение по ширине и видимость элементов
LAYOUT = r"""
(sels) => {
  const W = document.documentElement.clientWidth;
  const res = { overflow: document.documentElement.scrollWidth > W + 1 || document.body.scrollWidth > W + 1, seen: {} };
  for (const sel of sels) {
    const el = document.querySelector(sel);
    if (!el) { res.seen[sel] = 'missing'; continue; }
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const shown = cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0;
    res.seen[sel] = shown ? (r.left >= -1 && r.right <= W + 1 ? 'ok' : 'offscreen') : 'hidden';
  }
  return res;
}
"""

BLUR_SHADOW = re.compile(r"(?:box|text)-shadow\s*:\s*([^;}]+)", re.I)


def blurred_shadows(css: str) -> list[str]:
    """Тени с ненулевым размытием (третья длина в каждой тени списка)."""
    bad = []
    for m in BLUR_SHADOW.finditer(css):
        val = m.group(1)
        if val.strip() in ("none",) or "var(--win)" in val:
            continue
        for part in re.split(r",(?![^(]*\))", val):
            lens = re.findall(r"(?<![\w-])(-?\d*\.?\d+)(?:px|em|rem)?(?![\w%])", re.sub(r"var\([^)]*\)|#[0-9a-fA-F]+", "", part))
            if len(lens) >= 3 and float(lens[2]) != 0:
                bad.append(part.strip())
    return bad


def test_browser(pw_ok: bool):
    section("D. Каждый экран встроенных скинов в браузере (vite + headless Chrome)")
    if not pw_ok:
        check("headless Chrome не найден — браузерные проверки не выполнены", False)
        return
    from playwright.sync_api import sync_playwright

    # Плоскость — по файлам: ни размытых теней, ни blur/свечений/стекла
    for pid in PRESETS:
        css = "".join(preset_file(pid, s) for s in SCREENS)
        flat_bad = blurred_shadows(css) + re.findall(r"blur\(|backdrop-filter|drop-shadow", css)
        check(f"{pid}: плоский CSS — без размытых теней, blur, drop-shadow, backdrop-filter", not flat_bad)
        if flat_bad:
            print("     ", flat_bad[:4])

    with Vite() as vite, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(CHROME))
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.route(f"http://127.0.0.1:{vite.dead}/**", lambda r: r.abort())
        page.goto(f"http://127.0.0.1:{vite.port}/")
        page.wait_for_timeout(800)
        res = page.evaluate(BROWSER_CHECK, {"ids": PRESETS, "screens": SCREENS})
        for pid in PRESETS:
            rs = [res[f"{pid}.{s}"] for s in SCREENS]
            check(f"{pid}: три экрана, validateSkin без ошибок, контракт v3",
                  all(r["present"] and r["valid"] and r["screens"] == [s] and r["contract"] == 3
                      for r, s in zip(rs, SCREENS)))
            hooks = [h for r in rs for h in r.get("hooks", [])]
            check(f"{pid}: все hook-точки шаблонов на месте", not hooks)
            if hooks:
                print("     ", hooks[:2])
            smoke = {f"{s}/{k}": v for r, s in zip(rs, SCREENS) for k, v in r.get("smoke", {}).items() if v}
            check(f"{pid}: runtime-проверка (smokeTest) чата, досье, комнаты — ru/светлая и en/тёмная", not smoke)
            if smoke:
                print("     ", json.dumps(smoke, ensure_ascii=False)[:500])
            shells = [json.dumps(r.get("shell"), sort_keys=True) for r in rs]
            check(f"{pid}: shell-тема каркаса одинакова у трёх экранов (обе темы)",
                  len(set(shells)) == 1 and json.loads(shells[0])["light"].get("--bg"))
            names = {r.get("name") for r in rs}
            check(f"{pid}: имя скина в <meta> одно на три экрана", len(names) == 1 and None not in names)

        # Узкий экран (телефон) и широкий: раскладка внутри iframe
        def frame():
            return next(f for f in page.frames if f.name == "tskin")

        for pid in PRESETS:
            problems = []
            for theme in ("light", "dark"):
                for w, h in ((390, 700), (1180, 760)):
                    for screen in SCREENS:
                        if not page.evaluate(RENDER, {"id": pid, "screen": screen, "theme": theme, "w": w, "h": h}):
                            problems.append(f"{screen} {w}: нет ready")
                            continue
                        fr = frame()
                        sels = {
                            "chat": ['[data-vpc="input"]', '[data-vpc="send"]', '[data-vpc="messages"]', '[data-vpc="open-dossier"]'],
                            "dossier": ['.tabs [data-tab="memory"]', '.tabs [data-tab="settings"]', '[data-vpc="close-dossier"]'],
                            "room": ['[data-vpc="room-avatar"]', '[data-vpc="room-pastime"]', '[data-vpc="room-mood"]'],
                        }[screen]
                        lay = fr.evaluate(LAYOUT, sels)
                        if lay["overflow"]:
                            problems.append(f"{screen} {w} {theme}: горизонтальная прокрутка")
                        bad = {k: v for k, v in lay["seen"].items() if v != "ok"}
                        if bad:
                            problems.append(f"{screen} {w} {theme}: {bad}")
                        if screen == "chat":
                            side = fr.evaluate(LAYOUT, ['.side--left [data-vpc="persona-list"]', '[data-vpc="ctx-mood"]'])["seen"]
                            if w > 1000 and set(side.values()) != {"ok"}:
                                problems.append(f"chat {w} {theme}: боковые панели не видны {side}")
                            if w < 720:
                                btns = fr.evaluate(LAYOUT, ['[data-panel="left"]', '[data-panel="right"]'])["seen"]
                                if set(btns.values()) != {"ok"} or set(side.values()) != {"hidden"}:
                                    problems.append(f"chat {w} {theme}: кнопки панелей {btns}, панели {side}")
                                for btn, sel in (('[data-panel="left"]', '[data-vpc="persona-list"]'),
                                                 ('[data-panel="right"]', '[data-vpc="ctx-mood"]')):
                                    fr.click(btn)
                                    opened = fr.evaluate(LAYOUT, [sel])
                                    if opened["seen"][sel] != "ok" or opened["overflow"]:
                                        problems.append(f"chat {w} {theme}: панель {btn} не выдвигается {opened}")
                                    fr.press("body", "Escape")
                                    if fr.evaluate(LAYOUT, [sel])["seen"][sel] != "hidden":
                                        problems.append(f"chat {w} {theme}: панель {btn} не закрывается Escape")
                        if screen == "dossier":
                            for tab, probe in (("tasks", '[data-vpc="todo-list"]'), ("initiative", '[data-vpc-setting="iniBayes"]'),
                                               ("learning", '[data-vpc-action="add-course"]'), ("files", '[data-vpc="file-list"]'),
                                               ("settings", '[data-vpc="clear-chat"]'), ("memory", '[data-vpc="stm-list"]')):
                                fr.click(f'.tabs:not(.tabs--sub) [data-tab="{tab}"]')
                                fr.evaluate("(sel) => document.querySelector(sel)?.scrollIntoView()", probe)
                                st = fr.evaluate(LAYOUT, [probe])
                                if st["seen"][probe] != "ok" or st["overflow"]:
                                    problems.append(f"dossier {w} {theme}: вкладка {tab} — {st}")
            check(f"{pid}: 390 и 1180 px, светлая и тёмная — без горизонтальной прокрутки, ввод/отправка, "
                  f"выдвижные панели, вкладки досье, аватар на сцене", not problems)
            for pr in problems[:4]:
                print("     ", pr)
        browser.close()


# ─── E. Раздел «Скины» в приложении ───

def test_app(pw_ok: bool):
    section("E. Раздел «Скины» в приложении (мок-режим): библиотека, назначение, промпт для своей нейросети")
    if not pw_ok:
        check("headless Chrome не найден — проверки раздела не выполнены", False)
        return
    from playwright.sync_api import sync_playwright
    from app.api import skin_gen_api as sg
    names = ["Тушь", "Бланк", "Пиксель", "Нетраннер", "Ретрофутуризм", "Метро"]
    with Vite() as vite, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(CHROME))
        ctx = browser.new_context(viewport={"width": 1440, "height": 4200},
                                  permissions=["clipboard-read", "clipboard-write"])
        page = ctx.new_page()
        page.route(f"http://127.0.0.1:{vite.dead}/**", lambda r: r.abort())
        # Скрипт идёт и в песочницы скинов (там localStorage недоступен) — только верхнее окно
        page.add_init_script("if (window === window.top) { localStorage.setItem('vpc-lang', 'ru'); "
                             "localStorage.setItem('vpc-theme', 'light'); }")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{vite.port}/")
        page.locator("button", has_text="API").first.click(timeout=15000)
        page.wait_for_timeout(800)
        page.locator("nav button, aside button").filter(has_text="Скины").first.click()
        page.wait_for_selector(".skin-card", timeout=10000)
        cards = page.locator(".skin-card-name").all_inner_texts()
        check("в библиотеке шесть встроенных скинов с русскими именами", cards[:6] == names)
        check("…с описаниями", page.locator(".skin-card-desc").count() >= 6)
        lib = page.evaluate("() => localStorage.getItem('vpc-skin-library')")
        check("…никому не назначены", not lib or json.loads(lib).get("assignments") == {})
        check("…у встроенных «Скрыть», а не «Удалить»",
              page.locator(".skin-card").filter(has_text="Тушь").locator("button", has_text="Скрыть").count() == 1
              and page.locator(".skin-card").filter(has_text="Тушь").locator("button", has_text="Удалить").count() == 0)

        # Скрыть встроенный и вернуть
        page.locator(".skin-card").filter(has_text="Метро").locator("button", has_text="Скрыть").click()
        page.locator(".dialog-actions button", has_text="Скрыть").click()
        page.wait_for_timeout(300)
        hidden = page.locator(".skin-card-name").all_inner_texts()
        page.locator("button", has_text="Восстановить встроенные").click()
        page.wait_for_timeout(300)
        check("«Скрыть» убирает встроенный из библиотеки, «Восстановить встроенные» возвращает",
              "Метро" not in hidden and "Метро" in page.locator(".skin-card-name").all_inner_texts())

        # Назначить персоне — чат рисуется скином
        page.locator(".skin-card").filter(has_text="Тушь").locator("button", has_text="Применить к").click()
        page.wait_for_timeout(400)
        lib = json.loads(page.evaluate("() => localStorage.getItem('vpc-skin-library')"))
        check("назначение встроенного персоне", list(lib["assignments"].values()) == ["builtin-ink"])
        page.locator("nav button, aside button").filter(has_text="Чат").first.click()
        # «Все чаты» → чат персоны, которой назначен скин
        page.get_by_text("Коннор", exact=True).first.click()
        page.wait_for_selector("iframe.skin-frame", timeout=10000)
        page.wait_for_timeout(1200)
        skin_frame = page.frame_locator("iframe.skin-frame")
        skin_name = skin_frame.locator('meta[name="vpc-skin-name"]').get_attribute("content")
        msgs = skin_frame.locator('[data-vpc="messages"] > .msg').count()
        check("чат персоны рисуется встроенным скином «Тушь» с лентой",
              skin_name == "Тушь" and msgs > 0,
              f"skin={skin_name!r}, msgs={msgs}")

        # Промпт для своей нейросети
        page.locator("nav button, aside button").filter(has_text="Скины").first.click()
        page.wait_for_selector(".skin-card", timeout=10000)
        page.get_by_role("button", name="Промпт для своей нейросети").click()
        kit = page.locator(".skin-kit-body")
        kit.locator("textarea").first.fill("ночная прачечная, неоновые вывески отражаются в мокром кафеле")
        kit.get_by_role("button", name="Скопировать промпт: Чат").click()
        page.wait_for_timeout(300)
        clip = page.evaluate("() => navigator.clipboard.readText()")
        check("промпт экрана копируется целиком: системная часть ядра, задача, бриф, контракт, основа",
              clip.startswith(sg.SYSTEM_PROMPT + "\n\n---\n\n# Task\nCreate the \"chat\" screen")
              and "# Client's brief\nночная прачечная" in clip and "# Skin contract\n" in clip
              and "# Base file (chat) — functional reference\n" in clip and sg.STYLES_REMOVED in clip)
        kit.get_by_role("button", name="Скопировать промпт идей").click()
        page.wait_for_timeout(300)
        clip = page.evaluate("() => navigator.clipboard.readText()")
        check("промпт идей — системная часть арт-директора ядра + описание",
              clip.startswith(sg.DIRECTION_SYSTEM_PROMPT + "\n\n---\n\n# Client's description\nночная прачечная"))
        answer = json.dumps({"directions": [D1]}, ensure_ascii=False)
        kit.get_by_placeholder(re.compile("directions")).fill("Вот направление:\n```json\n" + answer + "\n```")
        page.wait_for_timeout(200)
        check("вставленный ответ с направлениями — карточка направления", kit.locator(".skin-dir-card").count() == 1)
        kit.get_by_role("button", name="Скопировать промпт: Досье").click()
        page.wait_for_timeout(300)
        clip = page.evaluate("() => navigator.clipboard.readText()")
        check("выбранное направление входит в промпт экрана (с shell-переменными ядра)",
              sg.format_direction(sg.normalize_direction(D1)) in clip)
        kit.get_by_placeholder(re.compile("HTML-документ")).fill("Готово!\n```html\n" + preset_file("ink", "chat") + "\n```")
        kit.get_by_role("button", name="Проверить ответ").click()
        page.wait_for_timeout(500)
        check("ответ чат-бота (файл экрана) проходит проверку разметки", kit.locator(".skin-gen-summary").count() == 1)
        kit.get_by_placeholder(re.compile("HTML-документ")).fill(preset_file("ink", "room").replace('data-vpc="room-pastime"', 'data-x="1"'))
        kit.get_by_role("button", name="Проверить ответ").click()
        page.wait_for_timeout(500)
        fix_btn = kit.get_by_role("button", name="Скопировать промпт исправления")
        check("сломанный ответ — ошибки и промпт исправления", kit.locator(".skin-report li").count() > 0 and fix_btn.count() == 1)
        fix_btn.click()
        page.wait_for_timeout(300)
        clip = page.evaluate("() => navigator.clipboard.readText()")
        check("промпт исправления — как итерация исправления ядра",
              "Your previous skin file for the \"room\" screen failed the app checks" in clip
              and "# Previous file (room) — fix this one\n" in clip and "room-pastime" in clip)
        kit.get_by_role("button", name=re.compile(r"Открыть в загрузке")).click()
        page.wait_for_selector(".skin-smoke--ok, .skin-smoke--failed", timeout=20000)
        check("готовый экран уходит в загрузку, runtime-проверка там проходит", page.locator(".skin-smoke--ok").count() == 1)
        check("без ошибок JS на странице", not errors)
        if errors:
            print("     ", errors[:3])
        browser.close()


def main():
    print("Библиотека готовых скинов и промпт для своей нейросети")
    test_export()
    if shutil.which("node"):
        test_prompt_parity()
        test_store()
    else:
        check("Node не найден — проверки B и C не выполнены", False)
    try:
        import playwright  # noqa: F401
        pw_ok = CHROME is not None
    except ImportError:
        pw_ok = False
    test_browser(pw_ok)
    test_app(pw_ok)
    print(f"\n{ok - failures}/{ok} OK" + (f", FAIL: {failures}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

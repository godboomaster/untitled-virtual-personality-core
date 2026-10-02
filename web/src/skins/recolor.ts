/* Перекраска скина без правки его файла: цветовые CSS-переменные из
   <style>-блоков скина переопределяются дописанным в самый конец документа
   блоком <style data-vpc-recolor>. Переопределение повторяет селекторы (и
   @media-обёртки, в т.ч. из атрибута media у <style>), где переменная
   объявлена, с !important — поэтому побеждает и внутри экранов, которые
   переобъявляют переменную у себя, а стоя последним — и !important самого
   скина с тем же селектором (<style> в <body> тоже).

   Сдвиг тона (hueShift, градусы) поворачивает в HSL все цветовые
   переменные-литералы; явные переопределения цветов важнее сдвига.
   Переменные-ссылки (--vpc-shell-bg: var(--bg)) не трогаются — они
   следуют за своей базовой переменной сами.

   Shell-тема каркаса (engine.extractShellTheme) читает переменные
   regex'ом по тексту файла и берёт ПОСЛЕДНЕЕ объявление. В правилах с
   !important значение захватилось бы вместе с флагом, поэтому в конце
   блока — правило-носитель чистых значений с никогда не совпадающим
   селектором: регекс видит их последними, на рендер оно не влияет.

   Разбор regex'ами и ручным сканером (без DOM): быстро и на 3 МБ файлах. */

export interface Rgba {
  r: number; // 0..255
  g: number;
  b: number;
  a: number; // 0..1
}

export type ColorGroup = 'surface' | 'text' | 'line' | 'accent';

export interface ColorVar {
  name: string; // --bg
  value: string; // исходное значение (литерал или var(...))
  color: string; // разрешённый цвет-литерал
  alias?: string; // значение — ссылка на другую переменную (цвет наследуется)
  contexts: string[]; // где объявлена: селекторы (с @media-обёртками)
  uses: number; // сколько раз переменная используется через var()
  group: ColorGroup;
}

// Объявление переменной в стилях скина
interface VarDecl {
  name: string;
  value: string;
  atRules: string[]; // обёртки @media/@supports снаружи внутрь
  selector: string;
}

const RECOLOR_ATTR = 'data-vpc-recolor';
const RECOLOR_BLOCK_RE = /<style\b[^>]*\bdata-vpc-recolor\b[^>]*>[\s\S]*?<\/style>\s*/gi;
const STYLE_RE = /<style\b([^>]*)>([\s\S]*?)<\/style>/gi;
const DECL_RE = /^\s*(--[A-Za-z0-9_-]+)\s*:\s*([\s\S]*?)\s*$/;
const ALIAS_RE = /^var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*([\s\S]*))?\)$/;
// Контексты, где объявления — не свойства элементов
const SKIP_AT_RE = /^@(keyframes|-webkit-keyframes|font-face|page|property|counter-style)/i;

// ── Цвета ──

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

function channel(s: string, max: number): number {
  const t = s.trim();
  if (t.endsWith('%')) return (parseFloat(t) / 100) * max;
  return parseFloat(t);
}

// Разбор цвета: hex, rgb()/rgba(), hsl()/hsla() (запятые или пробелы + «/»)
export function parseColor(value: string): Rgba | null {
  const v = value.trim().toLowerCase();
  const hex = /^#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/.exec(v);
  if (hex) {
    let h = hex[1];
    if (h.length <= 4) h = h.split('').map((c) => c + c).join('');
    const n = (i: number) => parseInt(h.slice(i, i + 2), 16);
    return { r: n(0), g: n(2), b: n(4), a: h.length === 8 ? n(6) / 255 : 1 };
  }
  const fn = /^(rgba?|hsla?)\(\s*([^)]*)\)$/.exec(v);
  if (!fn) return null;
  const parts = fn[2].split(/\s*[,/]\s*|\s+/).filter(Boolean);
  if (parts.length !== 3 && parts.length !== 4) return null;
  const alpha = parts.length === 4 ? channel(parts[3], 1) : 1;
  if (fn[1].startsWith('rgb')) {
    const [r, g, b] = parts.slice(0, 3).map((p) => channel(p, 255));
    if ([r, g, b, alpha].some((x) => !Number.isFinite(x))) return null;
    return { r: clamp(r, 0, 255), g: clamp(g, 0, 255), b: clamp(b, 0, 255), a: clamp(alpha, 0, 1) };
  }
  const h = parseFloat(parts[0].replace(/deg$/, ''));
  const s = channel(parts[1], 1);
  const l = channel(parts[2], 1);
  if ([h, s, l, alpha].some((x) => !Number.isFinite(x))) return null;
  return { ...hslToRgb(h, clamp(s, 0, 1), clamp(l, 0, 1)), a: clamp(alpha, 0, 1) };
}

export function isColorValue(value: string): boolean {
  return parseColor(value) !== null;
}

function hslToRgb(h: number, s: number, l: number): { r: number; g: number; b: number } {
  const hue = (((h % 360) + 360) % 360) / 360;
  if (s === 0) return { r: l * 255, g: l * 255, b: l * 255 };
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const f = (t: number) => {
    const x = t < 0 ? t + 1 : t > 1 ? t - 1 : t;
    if (x < 1 / 6) return p + (q - p) * 6 * x;
    if (x < 1 / 2) return q;
    if (x < 2 / 3) return p + (q - p) * (2 / 3 - x) * 6;
    return p;
  };
  return { r: f(hue + 1 / 3) * 255, g: f(hue) * 255, b: f(hue - 1 / 3) * 255 };
}

function rgbToHsl({ r, g, b }: Rgba): { h: number; s: number; l: number } {
  const rn = r / 255;
  const gn = g / 255;
  const bn = b / 255;
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const l = (max + min) / 2;
  if (max === min) return { h: 0, s: 0, l };
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h = max === rn ? (gn - bn) / d + (gn < bn ? 6 : 0) : max === gn ? (bn - rn) / d + 2 : (rn - gn) / d + 4;
  h *= 60;
  return { h, s, l };
}

const hex2 = (n: number) => Math.round(clamp(n, 0, 255)).toString(16).padStart(2, '0');

// #rrggbb без альфы — для <input type="color">
export function toHex(c: Rgba): string {
  return '#' + hex2(c.r) + hex2(c.g) + hex2(c.b);
}

// Литерал цвета: непрозрачный — #rrggbb, полупрозрачный — rgba()
export function formatColor(c: Rgba): string {
  if (c.a >= 0.999) return toHex(c);
  return `rgba(${Math.round(c.r)}, ${Math.round(c.g)}, ${Math.round(c.b)}, ${Math.round(c.a * 1000) / 1000})`;
}

// Поворот тона цвета на deg градусов (насыщенность, светлота, альфа — как были)
export function rotateHue(c: Rgba, deg: number): Rgba {
  const { h, s, l } = rgbToHsl(c);
  return { ...hslToRgb(h + deg, s, l), a: c.a };
}

// Цвет из пикера (#rrggbb) с альфой исходного значения переменной
export function pickerToColor(hex: string, original: string): string {
  const picked = parseColor(hex);
  if (!picked) return original;
  const orig = parseColor(original);
  return formatColor({ ...picked, a: orig ? orig.a : 1 });
}

// ── Разбор стилей скина ──

// Разбор CSS в объявления кастомных свойств с контекстом (селектор + @-обёртки).
// Скобки и кавычки учитываются: «;» и «{» внутри url(data:...) или строк
// не ломают разбор.
function parseDecls(css: string): VarDecl[] {
  const out: VarDecl[] = [];
  const stack: string[] = [];
  let start = 0;
  let depth = 0; // вложенность круглых скобок
  let quote = '';

  const flush = (end: number) => {
    const text = css.slice(start, end);
    if (!stack.length || !text.includes('--')) return;
    const m = DECL_RE.exec(text);
    if (!m) return;
    const selector = stack[stack.length - 1];
    if (selector.startsWith('@')) return; // объявление прямо в @media — не свойство
    const atRules = stack.slice(0, -1);
    if (atRules.some((a) => SKIP_AT_RE.test(a)) || !atRules.every((a) => a.startsWith('@'))) return;
    const value = m[2].replace(/\s*!important\s*$/i, '').trim();
    if (value) out.push({ name: m[1], value, atRules, selector });
  };

  for (let i = 0; i < css.length; i++) {
    const ch = css[i];
    if (quote) {
      if (ch === '\\') i++;
      else if (ch === quote) quote = '';
      continue;
    }
    if (ch === '"' || ch === "'") quote = ch;
    else if (ch === '(') depth++;
    else if (ch === ')') depth = Math.max(0, depth - 1);
    else if (depth > 0) continue;
    else if (ch === '{') {
      stack.push(css.slice(start, i).trim().replace(/\s+/g, ' '));
      start = i + 1;
    } else if (ch === ';') {
      flush(i);
      start = i + 1;
    } else if (ch === '}') {
      flush(i);
      stack.pop();
      start = i + 1;
    }
  }
  return out;
}

// Условие атрибута media у <style> («(prefers-color-scheme: dark)») как
// @media-обёртка; «all»/пусто/мусор — без обёртки
const MEDIA_ATTR_RE = /\bmedia\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+))/i;
function styleMedia(attrs: string): string | null {
  const m = MEDIA_ATTR_RE.exec(attrs);
  const media = (m?.[1] ?? m?.[2] ?? m?.[3] ?? '').trim().replace(/\s+/g, ' ');
  if (!media || /^all$/i.test(media) || /[{};<>]/.test(media)) return null;
  return `@media ${media}`;
}

// Все объявления кастомных свойств из <style>-блоков файла (кроме нашего)
function collectDecls(html: string): VarDecl[] {
  const out: VarDecl[] = [];
  STYLE_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = STYLE_RE.exec(html))) {
    if (m[1].includes(RECOLOR_ATTR)) continue;
    const decls = parseDecls(m[2].replace(/\/\*[\s\S]*?\*\//g, ''));
    const media = styleMedia(m[1]);
    // <style media="…">: условие блока — внешняя обёртка каждого объявления,
    // иначе переопределение действовало бы всегда
    out.push(...(media ? decls.map((d) => ({ ...d, atRules: [media, ...d.atRules] })) : decls));
  }
  return out;
}

// Контекст объявления строкой — для группировки и показа
function contextKey(d: VarDecl): string {
  return [...d.atRules, d.selector].join(' › ');
}

// Корневые контексты (значения по умолчанию для всего документа)
const isRootSelector = (d: VarDecl) => !d.atRules.length && /^(:root|html)\b/.test(d.selector);

// Значения переменных по имени: корневое объявление важнее экранных,
// среди равных — последнее (как в каскаде)
function baseValues(decls: VarDecl[]): Map<string, VarDecl> {
  const map = new Map<string, VarDecl>();
  for (const d of decls) {
    const cur = map.get(d.name);
    if (!cur || isRootSelector(d) || !isRootSelector(cur)) map.set(d.name, d);
  }
  return map;
}

// Значение → цвет: литерал или цепочка var() (до 6 шагов, с fallback)
function resolveColor(value: string, base: Map<string, VarDecl>): { color: Rgba; alias?: string } | null {
  let v = value.trim();
  let alias: string | undefined;
  for (let i = 0; i < 6; i++) {
    const m = ALIAS_RE.exec(v);
    if (!m) break;
    alias = alias ?? m[1];
    const next = base.get(m[1]);
    if (next) v = next.value.trim();
    else if (m[2] != null) v = m[2].trim();
    else return null;
  }
  const color = parseColor(v);
  return color ? { color, alias } : null;
}

export function colorGroup(name: string): ColorGroup {
  const n = name.replace(/^--(vpc-shell-)?/, '').toLowerCase();
  if (/(^|[-_])(bg|back|background|panel|surface|card|paper|bubble|base|canvas|sheet|layer|topbar|shade|sidebar)/.test(n)) return 'surface';
  if (/(^|[-_])(text|ink|fg|foreground|dim|muted|title|label|heading|caption)/.test(n)) return 'text';
  if (/(^|[-_])(line|border|rule|stroke|divider|outline|sep|grid)/.test(n)) return 'line';
  return 'accent';
}

// «--persona-bubble-2» → «Persona bubble 2», «--vpc-shell-bg» → «Shell · bg»
export function prettyVarName(name: string): string {
  const shell = name.startsWith('--vpc-shell-');
  const words = name.replace(/^--(vpc-shell-)?/, '').split(/[-_]+/).filter(Boolean).join(' ');
  const text = words.charAt(0).toUpperCase() + words.slice(1);
  return shell ? `Shell · ${words}` : text;
}

// Счётчик использований var(--name) по всему файлу — одним проходом
function countUses(html: string): Map<string, number> {
  const counts = new Map<string, number>();
  const re = /var\(\s*(--[A-Za-z0-9_-]+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html))) counts.set(m[1], (counts.get(m[1]) ?? 0) + 1);
  return counts;
}

// Цветовые CSS-переменные скина (дедуп по имени): литералы и ссылки на
// цвета, с местами объявления и числом использований; порядок — самые
// используемые первыми
export function extractColorVars(html: string): ColorVar[] {
  const decls = collectDecls(html.replace(RECOLOR_BLOCK_RE, ''));
  const base = baseValues(decls);
  const uses = countUses(html);
  const out: ColorVar[] = [];
  for (const [name, d] of base) {
    const resolved = resolveColor(d.value, base);
    if (!resolved) continue;
    const contexts = [...new Set(decls.filter((x) => x.name === name).map(contextKey))];
    out.push({
      name,
      value: d.value,
      color: formatColor(resolved.color),
      alias: resolved.alias,
      contexts,
      uses: uses.get(name) ?? 0,
      group: colorGroup(name),
    });
  }
  return out.sort((a, b) => b.uses - a.uses || a.name.localeCompare(b.name));
}

// Цветовые переменные нескольких файлов (экранов одного скина) одним списком
export function extractSkinColorVars(files: string[]): ColorVar[] {
  const byName = new Map<string, ColorVar>();
  for (const html of new Set(files)) {
    for (const v of extractColorVars(html)) {
      const cur = byName.get(v.name);
      if (!cur) byName.set(v.name, v);
      else {
        cur.uses += v.uses;
        cur.contexts = [...new Set([...cur.contexts, ...v.contexts])];
      }
    }
  }
  return [...byName.values()].sort((a, b) => b.uses - a.uses || a.name.localeCompare(b.name));
}

// Убрать ранее вставленный блок перекраски
export function stripRecolor(html: string): string {
  return html.replace(RECOLOR_BLOCK_RE, '');
}

// Вставить перекраску: явные цвета (имя → литерал) + сдвиг тона остальных
// литералов. Без переопределений возвращается исходный файл.
export function applyColorOverrides(html: string, colors: Record<string, string>, hueShift = 0): string {
  const src = stripRecolor(html);
  const explicit = Object.entries(colors).filter(([name, v]) => /^--[A-Za-z0-9_-]+$/.test(name) && parseColor(v));
  const shift = Number.isFinite(hueShift) ? hueShift % 360 : 0;
  if (!explicit.length && !shift) return src;

  const decls = collectDecls(src);
  const base = baseValues(decls);
  const overrides = new Map(explicit);

  // Правила по контекстам: ключ → {обёртки, селектор, строки}
  const rules = new Map<string, { atRules: string[]; selector: string; lines: string[] }>();
  // Итоговое значение каждой переопределённой переменной (для shell-темы)
  const finals = new Map<string, string>();
  const add = (d: { atRules: string[]; selector: string }, name: string, value: string) => {
    const key = [...d.atRules, d.selector].join('\u0000');
    let rule = rules.get(key);
    if (!rule) rules.set(key, (rule = { atRules: d.atRules, selector: d.selector, lines: [] }));
    rule.lines.push(`  ${name}: ${value} !important;`);
  };

  for (const d of decls) {
    const explicitValue = overrides.get(d.name);
    if (explicitValue != null) {
      add(d, d.name, explicitValue);
      finals.set(d.name, explicitValue);
      continue;
    }
    if (!shift) continue;
    const c = parseColor(d.value); // только литералы: ссылки следуют за базой сами
    if (!c) continue;
    const value = formatColor(rotateHue(c, shift));
    add(d, d.name, value);
    if (base.get(d.name) === d) finals.set(d.name, value);
  }
  // Явный цвет переменной, которой в файле нет (другой экран скина) — на :root
  for (const [name, value] of overrides) {
    if (!decls.some((d) => d.name === name)) {
      add({ atRules: [], selector: ':root' }, name, value);
      finals.set(name, value);
    }
  }
  if (!rules.size) return src;

  let css = '';
  for (const rule of rules.values()) {
    let block = `${rule.selector} {\n${rule.lines.join('\n')}\n}`;
    for (const at of [...rule.atRules].reverse()) block = `${at} {\n${block}\n}`;
    css += block + '\n';
  }
  // Носитель чистых значений для extractShellTheme (см. шапку файла)
  css += `:root:not(:root) {\n${[...finals].map(([n, v]) => `  ${n}: ${v};`).join('\n')}\n}\n`;
  const style = `<style ${RECOLOR_ATTR}>\n${css}</style>\n`;

  // В самый конец документа (парсер перенесёт его в <body>): и последним в
  // каскаде, и последним для regex extractShellTheme — даже если у скина
  // есть <style> после </head>
  return src.replace(/\s*$/, '\n') + style;
}

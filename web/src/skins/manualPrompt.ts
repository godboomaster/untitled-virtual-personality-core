/* Промпт генерации скина для своей нейросети (ChatGPT, Claude и т.п.):
   ровно то, что генератор ядра отправляет модели (app/api/skin_gen_api.py —
   build_messages, build_direction_messages, format_direction, shell_vars,
   check_direction, parse_directions, strip_base, strip_assets). Тексты
   промптов — из skinGenPrompts.ts (выгрузка из ядра,
   scripts/gen_skin_prompts.py), здесь только сборка; совпадение с ядром
   проверяет scripts/test_skin_library.py (одни и те же входы через Python
   и через этот модуль под Node).

   Модуль без DOM и без обращений к ядру: промпт собирается и на телефоне
   без компьютера. Системная и пользовательская части склеиваются в один
   текст (chatbotPrompt) — его вставляют в чат-бот одним сообщением. */

import type { ApiSkinDirection } from '../api';
import { SKIN_GEN_PROMPTS as P } from './skinGenPrompts';

export type PromptScreen = 'chat' | 'dossier' | 'room';
export type PromptLocale = 'ru' | 'en';
export type PromptBaseKind = 'template' | 'skin';

export interface PromptMessage {
  role: 'system' | 'user';
  content: string;
}

export const PROMPT_MAX_DESCRIPTION = P.maxDescription;
export const PROMPT_MAX_HTML = P.maxHtmlForLlm;
export const PROMPT_DIRECTION_COUNT = P.directionMaxCount;

// Разделитель системной и пользовательской частей в тексте для чат-бота
export const PROMPT_JOINER = '\n\n---\n\n';

// Системная и пользовательская части — одним сообщением для чат-бота
export function chatbotPrompt(messages: PromptMessage[]): string {
  return messages.map((m) => m.content).join(PROMPT_JOINER);
}

// ── Мелочи Python-строк ──

// Длина в кодовых точках (как len() у str в Python)
const cpLen = (s: string) => Array.from(s).length;
const cpSlice = (s: string, n: number) => Array.from(s).slice(0, n).join('');

// round() Python: половина — к чётному
function roundHalfEven(v: number): number {
  const f = Math.floor(v);
  const diff = v - f;
  if (diff > 0.5) return f + 1;
  if (diff < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

// ── Базовый файл для модели (strip_base / strip_assets) ──

const BASE_TOKEN_RE =
  /(?<comment>(?:^[ \t]*)?<!--.*?-->(?:[ \t]*\n)?)|(?<script><script\b[^>]*>.*?<\/script\s*>)|(?<style>(?<open><style\b[^>]*>)(?<css>.*?)(?<close><\/style\s*>))/gims;
const CSS_COMMENT_RE = /(?:^[ \t]*)?\/\*.*?\*\/(?:[ \t]*\n)?/gms;
const BLANK_LINES_RE = /\n[ \t]*(?:\n[ \t]*){2,}/g;
const BRIDGE_MARKER_RE = /^<!--\s*=+\s*VPC-BRIDGE:(?:START|END)\b/;
const ASSET_RE = /data:[a-zA-Z0-9.+-]+\/[a-zA-Z0-9.+-]+(?:;[a-zA-Z0-9=.+-]+)*,[A-Za-z0-9+/=%._-]{200,}/g;
const ASSET_TOKEN_RE = /vpc-asset:(\d+)/g;

function isLongComment(text: string): boolean {
  const t = text.trim();
  return cpLen(t) > P.longComment && !BRIDGE_MARKER_RE.test(t);
}

// Замена длинного комментария: на своей строке уходит целиком, после
// текста той же строки оставляет перевод строки (как _drop_comment)
function dropComment(whole: string, offset: number, source: string): string {
  if (!isLongComment(whole)) return whole;
  const endsLine = whole.endsWith('\n');
  const ownLine = offset === 0 || source[offset - 1] === '\n';
  return endsLine && !ownLine ? '\n' : '';
}

/** Базовый файл для модели: длинные комментарии-инструкции вырезаны, у
 *  шаблона ('template') — ещё и содержимое <style> (strip_base ядра). */
export function stripBase(html: string, kind: PromptBaseKind): string {
  const out = html.replace(BASE_TOKEN_RE, (...args: unknown[]) => {
    const whole = args[0] as string;
    const offset = args[args.length - 3] as number;
    const source = args[args.length - 2] as string;
    const g = args[args.length - 1] as Record<string, string | undefined>;
    if (g.comment !== undefined) return dropComment(whole, offset, source);
    if (g.script !== undefined) return whole;
    if (kind === 'template') return g.open + P.stylesRemoved + g.close;
    const css = (g.css ?? '').replace(CSS_COMMENT_RE, (m: string, off: number, src: string) => dropComment(m, off, src));
    return g.open + css + g.close;
  });
  return out.replace(BLANK_LINES_RE, '\n\n');
}

/** Длинные data-URI → метки vpc-asset:N (N — индекс в assets). */
export function stripAssets(html: string, assets: string[]): string {
  return html.replace(ASSET_RE, (uri) => {
    let idx = assets.indexOf(uri);
    if (idx === -1) idx = assets.push(uri) - 1;
    return `vpc-asset:${idx}`;
  });
}

/** Метки vpc-asset:N → исходные data-URI. */
export function restoreAssets(html: string, assets: string[]): string {
  if (!assets.length) return html;
  return html.replace(ASSET_TOKEN_RE, (whole, n: string) => {
    const idx = Number(n);
    return idx < assets.length ? assets[idx] : whole;
  });
}

// ── Арт-направление (check_direction, shell_vars, format_direction) ──

const HEX_RE = /^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$/;
const HEX_IN_TEXT_RE = /#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z])/;
const STACK_BAD_RE = /[;{}<>()\\]|url|https?:|@import/i;
const ROLE_ALIASES: Record<string, string> = P.roleAliases;
const ALIAS_ROLES = new Set(Object.values(ROLE_ALIASES));
const TEXT_LIMITS: Record<string, number> = P.textLimits;

function cleanText(v: unknown, limit: number): string {
  if (typeof v !== 'string') return '';
  const s = v.replace(/\s+/g, ' ').trim();
  return cpLen(s) <= limit ? s : cpSlice(s, limit - 1).trimEnd() + '…';
}

function normHex(v: unknown): string | null {
  if (typeof v !== 'string') return null;
  const m = HEX_RE.exec(v.trim()) ?? HEX_IN_TEXT_RE.exec(v);
  if (!m) return null;
  let h = m[1].toLowerCase();
  if (h.length === 3) h = [...h].map((c) => c + c).join('');
  return '#' + h;
}

function normStack(v: unknown): string {
  if (typeof v !== 'string') return '';
  let s = v.replace(/\s+/g, ' ').trim().replace(/,+$/, '').trim();
  if (cpLen(s) > P.maxStack) {
    const cut = s.lastIndexOf(',', P.maxStack);
    s = cut > 0 ? s.slice(0, cut).trim() : '';
  }
  const quotes = (q: string) => s.split(q).length - 1;
  if (!s || STACK_BAD_RE.test(s) || quotes('"') % 2 || quotes("'") % 2) return '';
  return s;
}

// Палитра словарём {"background": "#…"} или {"background": {"hex": …}} → список
function paletteItems(items: unknown): unknown[] {
  if (isObj(items)) {
    return Object.entries(items).map(([role, v]) =>
      isObj(v) ? { ...v, role: v.role || role } : { role, hex: v },
    );
  }
  return Array.isArray(items) ? items : [];
}

/** Проверенное направление или null (как normalize_direction ядра). */
export function normalizeDirection(raw: unknown): ApiSkinDirection | null {
  if (!isObj(raw)) return null;
  const text = (k: string) => cleanText(raw[k], TEXT_LIMITS[k]);
  const name = text('name');
  const subject = text('subject');
  if (!name || !subject) return null;

  const palette: ApiSkinDirection['palette'] = [];
  const seen = new Set<string>();
  for (const item of paletteItems(raw.palette)) {
    if (!isObj(item)) continue;
    const hex = normHex(item.hex);
    if (!hex) continue;
    let role = cleanText(item.role, 24).toLowerCase();
    role = (Object.hasOwn(ROLE_ALIASES, role) ? ROLE_ALIASES[role] : role) || 'extra';
    if (seen.has(role) && ALIAS_ROLES.has(role)) continue;
    seen.add(role);
    palette.push({ role, hex, reason: cleanText(item.reason, 160) });
    if (palette.length >= P.maxPalette) break;
  }
  if (!['background', 'ink', 'accent'].every((r) => seen.has(r))) return null;

  let f = raw.fonts;
  if (typeof f === 'string') f = { text: f };
  const fonts = isObj(f) ? f : {};
  const display = normStack(fonts.display);
  const body = normStack(fonts.text);
  if (!display && !body) return null;

  let avoid = raw.avoid;
  if (typeof avoid === 'string') avoid = avoid.split(/[;\n]+/);
  const avoidList = (Array.isArray(avoid) ? avoid : [])
    .map((x) => cleanText(x, 200))
    .filter(Boolean)
    .slice(0, P.maxAvoid);
  const note = cleanText(raw.note, P.directionMaxNote);

  return {
    name,
    subject,
    mood: text('mood'),
    layout: text('layout'),
    signature: text('signature'),
    texture: text('texture'),
    motion: text('motion'),
    palette,
    fonts: { display: display || body, text: body || display, mono: normStack(fonts.mono), why: cleanText(fonts.why, 300) },
    avoid: avoidList,
    ...(note ? { note } : {}),
  };
}

function color(d: ApiSkinDirection, role: string): string | undefined {
  return d.palette.find((c) => c.role === role)?.hex;
}

// Смесь двух #rrggbb: t — доля b (как _mix ядра, с его округлением)
function mix(a: string, b: string, t: number): string {
  const ch = (h: string, i: number) => parseInt(h.slice(i, i + 2), 16);
  return (
    '#' +
    [1, 3, 5]
      .map((i) => {
        const x = ch(a, i);
        const y = ch(b, i);
        return roundHalfEven(x + (y - x) * t).toString(16).padStart(2, '0');
      })
      .join('')
  );
}

/** --vpc-shell-* из палитры и шрифтов направления (shell_vars ядра). */
export function shellVars(d: ApiSkinDirection): [string, string][] {
  const bg = color(d, 'background')!;
  const ink = color(d, 'ink')!;
  const panel = color(d, 'surface') ?? bg;
  return [
    ['--vpc-shell-bg', bg],
    ['--vpc-shell-panel', panel],
    ['--vpc-shell-text', ink],
    ['--vpc-shell-dim', color(d, 'muted') ?? mix(bg, ink, 0.6)],
    ['--vpc-shell-border', mix(panel, ink, 0.18)],
    ['--vpc-shell-accent', color(d, 'accent')!],
    ['--vpc-shell-font', d.fonts.text],
    ['--vpc-shell-font-disp', d.fonts.display],
    ['--vpc-shell-font-mono', d.fonts.mono || P.defaultMono],
  ];
}

/** Блок направления в промпте экрана (format_direction ядра). */
export function formatDirection(d: ApiSkinDirection): string {
  const f = d.fonts;
  const lines = ['# Art direction (binding)', `Name: ${d.name}`, `Subject: ${d.subject}`];
  if (d.mood) lines.push(`Mood: ${d.mood}`);
  lines.push('Palette:');
  for (const c of d.palette) lines.push(`- ${c.role}: ${c.hex}` + (c.reason ? ` — ${c.reason}` : ''));
  lines.push(`Display type: ${f.display}`);
  lines.push(`Text type: ${f.text}`);
  if (f.mono) lines.push(`Mono: ${f.mono}`);
  if (f.why) lines.push(`Why these fonts: ${f.why}`);
  const extra: [keyof ApiSkinDirection, string][] = [
    ['layout', 'Layout'],
    ['signature', 'Signature element (the one bold thing)'],
    ['texture', 'Texture'],
    ['motion', 'Motion'],
  ];
  for (const [key, label] of extra) if (d[key]) lines.push(`${label}: ${d[key] as string}`);
  if (d.avoid.length) lines.push('Avoid: ' + d.avoid.join('; '));
  if (d.note) lines.push(`Client's note on this direction (takes priority): ${d.note}`);
  lines.push(
    'All three screens of this skin (chat, dossier, room) are built from this direction and must feel ' +
      'like one product. Declare these shell variables in :root exactly as written — identical in all ' +
      'three files:',
  );
  for (const [k, v] of shellVars(d)) lines.push(`  ${k}: ${v};`);
  return lines.join('\n');
}

// ── Разбор ответа с направлениями (parse_directions ядра) ──

const FENCE_RE = /^\s*```[a-zA-Z]*\s*$/gm;
const JSON_START_RE = /[[{]/g;
const TRAILING_COMMA_RE = /,(\s*[}\]])/g;
const FONT_LINE_RE = /^(\s*"(?:display|text|mono)"\s*:\s*")(.*)("\s*,?\s*)$/gm;
const NEXT_KEY_RE = /\s*["\u201c][A-Za-z_][A-Za-z0-9_ -]{0,40}["\u201d]\s*:/y;

function stringCloses(s: string, j: number, inArray: boolean): boolean {
  const n = s.length;
  while (j < n && ' \t\r\n'.includes(s[j])) j++;
  if (j >= n || ':}]'.includes(s[j])) return true;
  if (s[j] !== ',') return false;
  if (!inArray) {
    NEXT_KEY_RE.lastIndex = j + 1;
    return NEXT_KEY_RE.test(s);
  }
  j++;
  while (j < n && ' \t\r\n'.includes(s[j])) j++;
  return j >= n || '"{[]\u201c'.includes(s[j]);
}

/** Типовые поломки JSON от чат-ботов: кавычки внутри строк (имена шрифтов),
 *  типографские кавычки-разделители, переводы строки в строках, висячие запятые. */
export function repairJson(text: string): string {
  text = text.replace(FONT_LINE_RE, (_m, a: string, b: string, c: string) => a + b.replaceAll('"', "'") + c);
  const out: string[] = [];
  const stack: string[] = [];
  let inStr = false;
  let i = 0;
  const n = text.length;
  while (i < n) {
    let c = text[i];
    if (!inStr) {
      if (c === '\u201c' || c === '\u201d') c = '"';
      if (c === '"') inStr = true;
      else if (c === '{' || c === '[') stack.push(c);
      else if ((c === '}' || c === ']') && stack.length) stack.pop();
      out.push(c);
      i++;
      continue;
    }
    if (c === '\\' && i + 1 < n) {
      out.push(text.slice(i, i + 2));
      i += 2;
      continue;
    }
    if (c === '\n') out.push('\\n');
    else if ((c === '"' || c === '\u201d') && stringCloses(text, i + 1, stack.length > 0 && stack[stack.length - 1] === '[')) {
      inStr = false;
      out.push('"');
    } else if (c === '"') out.push('\\"');
    else out.push(c);
    i++;
  }
  return out.join('').replace(TRAILING_COMMA_RE, '$1');
}

// JSON-значение с начала строки s (как raw_decode): кратчайший разбираемый
// префикс, кончающийся на } или ]; null — не разобрать
function decodePrefix(s: string): { data: unknown; end: number } | null {
  for (let end = 0; end < s.length; end++) {
    const c = s[end];
    if (c !== '}' && c !== ']') continue;
    try {
      return { data: JSON.parse(s.slice(0, end + 1)), end: end + 1 };
    } catch {
      /* дальше */
    }
  }
  return null;
}

function decodeAt(text: string, start: number): { data: unknown; end: number | null } | null {
  const direct = decodePrefix(text.slice(start));
  if (direct) return { data: direct.data, end: start + direct.end };
  const rest = text.slice(start);
  for (const fixed of [rest.replace(TRAILING_COMMA_RE, '$1'), repairJson(rest)]) {
    const r = decodePrefix(fixed);
    if (r) return { data: r.data, end: null };
  }
  return null;
}

/** Направления из ответа чат-бота: ограды markdown и текст вокруг
 *  отбрасываются, берётся первый разбираемый JSON ({"directions": […]},
 *  […] или одно направление); [] — ничего не нашлось. */
export function parseDirections(text: string, limit: number = P.directionMaxCount): ApiSkinDirection[] {
  // Без ключа palette направления не бывает (normalizeDirection) — и разбирать
  // нечего: не перебирать попусту каждую «{» вставленного по ошибке HTML
  if (!text || !text.includes('palette')) return [];
  text = text.replace(FENCE_RE, '');
  const singles: ApiSkinDirection[] = [];
  JSON_START_RE.lastIndex = 0;
  let pos = 0;
  for (;;) {
    JSON_START_RE.lastIndex = pos;
    const m = JSON_START_RE.exec(text);
    if (!m) break;
    const start = m.index;
    pos = start + 1;
    const r = decodeAt(text, start);
    if (!r) continue;
    const data = r.data;
    let items: unknown[];
    if (isObj(data) && Array.isArray(data.directions)) items = data.directions;
    else if (Array.isArray(data)) items = data;
    else if (isObj(data) && 'palette' in data) {
      const d = normalizeDirection(data);
      if (d) {
        singles.push(d);
        if (r.end !== null) pos = r.end;
      }
      continue;
    } else continue;
    const out = items.map(normalizeDirection).filter((d): d is ApiSkinDirection => d !== null);
    if (out.length) return out.slice(0, limit);
  }
  return singles.slice(0, limit);
}

// ── Сообщения (build_messages / build_direction_messages) ──

export interface ScreenPromptInput {
  screen: PromptScreen;
  description: string;
  // База уже подготовлена: stripBase + stripAssets (prepareBaseHtml)
  baseHtml: string;
  baseKind: PromptBaseKind;
  contractDoc: string;
  locale: PromptLocale;
  // Исправление: прошлый ответ (без ассетов) и ошибки проверки
  previousHtml?: string | null;
  errors?: string[];
  direction?: ApiSkinDirection | null; // нормализованное
}

/** База для модели: как prepare() ядра (стили шаблона и длинные комментарии
 *  вырезаны, inline-ассеты — метками vpc-asset:N в assets). */
export function prepareBaseHtml(html: string, kind: PromptBaseKind, assets: string[]): string {
  return stripAssets(stripBase(html, kind), assets);
}

function functionalCss(screen: PromptScreen, fixing: boolean): string {
  const fc = P.functionalCss as Record<string, string[]>;
  const rules = [...fc.common.map((r) => r.replaceAll('{screen}', screen)), ...(fc[screen] ?? [])];
  const intro = fixing
    ? 'Whatever the file looks like, its CSS must keep these behaviours'
    : 'The base file comes without its styles — you write all CSS. Whatever it looks like, ' +
      'it must keep these behaviours';
  return (
    '# Functional CSS requirements\n' + intro + ' (the app and its checks rely on them):\n' +
    rules.map((r) => `- ${r}`).join('\n')
  );
}

/** Сообщения запроса экрана скина — как build_messages ядра. */
export function buildScreenMessages(inp: ScreenPromptInput): PromptMessage[] {
  const direction = inp.direction ?? null;
  const errors = inp.errors ?? [];
  const previous = inp.previousHtml ?? null;
  const screen = inp.screen;
  const brief =
    inp.description.trim() ||
    (direction
      ? '(no brief — the art direction is all there is)'
      : '(no brief — invent a fitting, concrete design yourself)');
  const parts: string[] = [];
  if (errors.length) {
    parts.push(
      `# Task\nYour previous skin file for the "${screen}" screen failed the app checks. ` +
        'Fix every problem listed below and return the complete corrected file. ' +
        (direction
          ? 'Keep the design and stay inside the art direction; change only what is needed.'
          : 'Keep the design; change only what is needed.'),
    );
    parts.push('# Problems\n' + errors.map((e) => `- ${e}`).join('\n'));
    if (direction) parts.push(formatDirection(direction));
    parts.push('# Original brief\n' + brief);
  } else {
    const what =
      inp.baseKind === 'skin'
        ? 'an existing skin of this screen: redesign it into the art direction — its CSS is ' +
          'yours to rewrite; keep its hook points and behaviour'
        : 'the default template of this screen with its styles removed: a functional reference ' +
          '(hook points, forms, tabs, scripts), not a look to restyle';
    parts.push(
      `# Task\nCreate the "${screen}" screen of a skin from the art direction below. ` +
        `The base file at the end is ${what}.`,
    );
    parts.push(
      direction
        ? formatDirection(direction)
        : '# Art direction\n(none given — before writing code, derive one concrete direction from the brief ' +
            'yourself: a subject that is a concrete thing, not a genre; a palette with roles; system font ' +
            'stacks; a layout idea; one signature element)',
    );
    parts.push("# Client's brief\n" + brief);
  }
  parts.push(P.langLine[inp.locale]);
  if (inp.baseKind === 'template') parts.push(functionalCss(screen, previous !== null));
  if (inp.contractDoc.trim()) parts.push('# Skin contract\n' + inp.contractDoc.trim());
  if (previous !== null) parts.push(`# Previous file (${screen}) — fix this one\n` + previous);
  else parts.push(`# Base file (${screen}) — functional reference\n` + inp.baseHtml);
  return [
    { role: 'system', content: P.system },
    { role: 'user', content: parts.join('\n\n') },
  ];
}

/** Сообщения запроса арт-направлений — как build_direction_messages ядра. */
export function buildDirectionMessages(
  description: string,
  locale: PromptLocale,
  count: number = P.directionMaxCount,
  exclude: string[] = [],
): PromptMessage[] {
  const brief = description.trim() || '(no description — surprise the client with concrete, distinct ideas)';
  const lang = P.langName[locale] ?? 'Russian';
  const parts = [
    "# Client's description\n" + brief,
    `Give exactly ${count} direction${count > 1 ? 's' : ''}. ` +
      `Write all text values in ${lang}; keep hex codes and font names as they are.`,
  ];
  const names = exclude.filter((n) => n.trim()).map((n) => cpSlice(n.trim(), 80));
  if (names.length) parts.push('Already proposed — do not repeat these ideas or their subjects: ' + names.join('; '));
  parts.push(P.userLanguageLine[locale]);
  return [
    { role: 'system', content: P.directionSystem },
    { role: 'user', content: parts.join('\n\n') },
  ];
}

// ── Ответ чат-бота с HTML ──

const DOC_END_RE = /<\/html\s*>/gi;

/** HTML-документ из ответа чат-бота (как extract_html ядра): текст и
 *  markdown-ограды вокруг отбрасываются; truncated — документ не закрыт
 *  </html> (ответ оборван). */
export function extractHtmlDocument(text: string): { html: string | null; truncated: boolean } {
  const doctype = /<!doctype/i.exec(text) ?? /<html[\s>]/i.exec(text);
  if (!doctype) return { html: null, truncated: false };
  const start = doctype.index;
  let end = -1;
  for (const m of text.matchAll(DOC_END_RE)) end = m.index + m[0].length;
  if (end <= start) return { html: text.slice(start).replace(FENCE_RE, '').trimEnd(), truncated: true };
  return { html: text.slice(start, end), truncated: false };
}

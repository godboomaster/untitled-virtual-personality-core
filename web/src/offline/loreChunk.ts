/* Нарезка лора на телефоне — то же, что app/core/lore.py (clean_text,
   chunk_text, outline): лор, загруженный без ядра, режется так же, как его
   нарежет ядро. Длины — в символах Юникода, как len() в Python. Без
   зависимостей: scripts/test_lore.py сверяет с ядром под Node. */

export const CHUNK_CHARS = 350; // кусок вместе с заголовком раздела
export const SECTION_CHARS = 60; // заголовок в векторе и в промпте
const PLAIN_HEAD_WORDS = 5; // заголовок без разметки — не длиннее

export interface Chunk {
  s: string; // раздел
  t: string; // текст
}

export interface OutlineBlock {
  h?: number; // заголовок этого уровня; нет — абзац
  t: string;
}

const MD_HEAD_RE = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/u;
const BOLD_HEAD_RE = /^\s*(?:\*\*|__)(.+?)(?:\*\*|__)\s*:?\s*$/u;
const IMAGE_RE = /!\[[^\]]*\]\([^)]*\)/gu;
const SENTENCE_RE = /(?<=[.!?…])\s+/u;
const CLAUSE_RE = /(?<=[,;:—])\s+/u;
const INVISIBLE_RE = /[​-‍⁠﻿­]/gu;
const PLAIN_END = new Set('.!?…,;»"');

const chars = (s: string) => Array.from(s);
const len = (s: string) => chars(s).length;
const cut = (s: string, n: number) => chars(s).slice(0, n).join('');

// str.isupper() одного символа: буква с заглавной формой
const isUpper = (ch: string) => ch !== ch.toLowerCase() && ch === ch.toUpperCase();
const isDigit = (ch: string) => /^\p{Nd}$/u.test(ch);

// str.strip(chars) Python
function stripChars(s: string, set: string): string {
  const a = chars(s);
  let i = 0;
  let j = a.length;
  while (i < j && set.includes(a[i])) i++;
  while (j > i && set.includes(a[j - 1])) j--;
  return a.slice(i, j).join('');
}

const rstripChars = (s: string, set: string) => {
  const a = chars(s);
  let j = a.length;
  while (j > 0 && set.includes(a[j - 1])) j--;
  return a.slice(0, j).join('');
};

/** Единый вид текста: нормальная форма Юникода, переводы строк, пробелы. */
export function cleanText(text: string): string {
  let s = (text || '').normalize('NFC');
  s = s.replace(INVISIBLE_RE, '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  s = s.replace(IMAGE_RE, '').replace(/ /g, ' ').replace(/\t/g, ' ');
  return s
    .split('\n')
    .map((ln) => ln.replace(/ {2,}/g, ' ').trim())
    .join('\n')
    .trim();
}

// Заголовок без разметки (txt): короткая строка без знаков конца фразы,
// отделённая от текста
function plainHeading(lines: string[], i: number): boolean {
  const line = lines[i];
  const a = chars(line);
  const words = line.split(/\s+/u).filter(Boolean).length;
  if (!line || a.length > SECTION_CHARS || words > PLAIN_HEAD_WORDS) return false;
  if (PLAIN_END.has(a[a.length - 1]) || !(isUpper(a[0]) || isDigit(a[0]))) return false;
  if (a[a.length - 1] === ':' && words > 2) return false;
  if (/:\s+\S/u.test(line)) return false;
  const prevBlank = i === 0 || !lines[i - 1];
  if (i + 1 >= lines.length) return false;
  const nxt = lines[i + 1];
  return !nxt || (prevBlank && len(nxt) > 80);
}

function heading(lines: string[], i: number): [number, string] | null {
  const line = lines[i];
  let m = MD_HEAD_RE.exec(line);
  if (m) return [m[1].length, stripChars(m[2], ' *_:')];
  m = BOLD_HEAD_RE.exec(line);
  if (m && len(m[1]) <= SECTION_CHARS) return [4, stripChars(m[1], ' *_:')];
  if (plainHeading(lines, i)) return [4, rstripChars(line, ' :')];
  return null;
}

// Части подряд — в куски не длиннее limit
function pack(parts: string[], limit: number): string[] {
  const out: string[] = [];
  let cur = '';
  for (const part of parts) {
    if (cur && len(cur) + 1 + len(part) > limit) {
      out.push(cur);
      cur = part;
    } else {
      cur = cur ? `${cur} ${part}` : part;
    }
  }
  if (cur) out.push(cur);
  return out;
}

// Абзац длиннее куска: по предложениям, длинное предложение — по частям
// фразы, слово длиннее куска — как есть по границе
function splitLong(text: string, limit: number): string[] {
  if (len(text) <= limit) return [text];
  for (const re of [SENTENCE_RE, CLAUSE_RE]) {
    const parts = text.split(re);
    if (parts.length > 1) return pack(parts.flatMap((p) => splitLong(p, limit)), limit);
  }
  const words = text.split(' ');
  if (words.length > 1) return pack(words.flatMap((w) => splitLong(w, limit)), limit);
  const a = chars(text);
  const out: string[] = [];
  for (let i = 0; i < a.length; i += limit) out.push(a.slice(i, i + limit).join(''));
  return out;
}

/** Текст → куски. Заголовки вложенные: в разделе — два нижних уровня
 * («Биография / Детство»). */
export function chunkText(text: string, limit = CHUNK_CHARS): Chunk[] {
  const lines = cleanText(text).split('\n');
  const stack: [number, string][] = [];
  let section = '';
  const paragraphs: string[] = [];
  const out: Chunk[] = [];
  const flush = () => {
    if (!paragraphs.length) return;
    const room = Math.max(120, limit - len(section) - 2);
    const parts = paragraphs.flatMap((p) => splitLong(p, room));
    for (const piece of pack(parts, room)) out.push({ s: section, t: piece });
    paragraphs.length = 0;
  };
  lines.forEach((line, i) => {
    if (!line) return;
    const head = heading(lines, i);
    if (head) {
      flush();
      const [level, title] = head;
      while (stack.length && stack[stack.length - 1][0] >= level) stack.pop();
      stack.push([level, title]);
      section = cut(
        stack
          .slice(-2)
          .map(([, t]) => t)
          .join(' / '),
        SECTION_CHARS,
      );
      return;
    }
    paragraphs.push(line);
  });
  flush();
  return out;
}

/** Текст для просмотра: заголовки — как их увидела нарезка, остальное — абзацы. */
export function outline(text: string): OutlineBlock[] {
  const lines = cleanText(text).split('\n');
  const out: OutlineBlock[] = [];
  lines.forEach((line, i) => {
    if (!line) return;
    const head = heading(lines, i);
    out.push(head ? { h: head[0], t: head[1] } : { t: line });
  });
  return out;
}

/** Только имя, без пути; форма NFC — как clean_filename ядра. */
export function cleanFilename(name: string): string {
  const s = String(name || '')
    .normalize('NFC')
    .replace(/\\/g, '/');
  return cut(s.split('/').pop()!.trim(), 200) || 'lore.txt';
}

export function fileExt(name: string): string {
  const i = name.lastIndexOf('.');
  return i >= 0 ? name.slice(i + 1).toLowerCase() : '';
}

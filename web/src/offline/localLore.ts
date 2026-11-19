import { api, ApiError, isNativeApp } from '../api';
import { DocxError, docxText } from './docx';
import { embedTexts } from './embed';
import { isLocalPersona } from './localPersonas';
import { chunkText, cleanFilename, cleanText, fileExt } from './loreChunk';
import type { LoreChunk } from './lore';
import { getLocalLore, getLoreCopy, getLoreOriginal, localLorePersonas, putLocalLore } from './memory';
import type { LocalLore, LocalLoreFile } from './memory';
import { packVec } from './vectors';

/* Лор, загруженный на телефоне без ядра: txt, md и docx читаются и режутся
   здесь же (loreChunk.ts — так же, как ядро), векторы кусков считает модель
   телефона (если скачана; нет — поиск по словам). Файл и куски лежат в
   IndexedDB рядом с копией лора с ядра (memory.ts), поиск видит их вместе.
   Ядро на связи — исходный файл уходит на него обычной загрузкой
   (POST …/lore): ядро разберёт его своей моделью, новая копия придёт по
   метке в ките, а запись телефона снимается. Ядро файл не приняло (400,
   413, 422) — он остаётся с ошибкой, пока его не удалят; нет связи, 5xx —
   ждёт следующей. PDF — только через ядро (разбор слишком тяжёл). */

export const PHONE_LORE_EXT = ['txt', 'md', 'docx'];
export const PHONE_LORE_MAX_BYTES = 10 * 1024 * 1024; // как MAX_FILE_BYTES ядра
// Все файлы персоны, загруженные на телефоне: векторы считает телефон —
// больше этого он считает минуты (на ядре лимит 2 млн знаков)
export const PHONE_LORE_MAX_CHARS = 300_000;
const EMBED_BATCH = 16;

export type PhoneLoreErrorCode = 'ext' | 'size' | 'empty' | 'docx' | 'total';

export class PhoneLoreError extends Error {
  code: PhoneLoreErrorCode;
  constructor(code: PhoneLoreErrorCode, detail = '') {
    super(detail || code);
    this.code = code;
  }
}

// Текстовый файл: BOM, затем UTF-8, иначе cp1251 (старые русские txt) — как
// _decode ядра (без угадывания кодировки)
function decodeText(buf: ArrayBuffer): string {
  const b = new Uint8Array(buf);
  if (b[0] === 0xef && b[1] === 0xbb && b[2] === 0xbf) return new TextDecoder('utf-8').decode(b.subarray(3));
  if (b[0] === 0xff && b[1] === 0xfe) return new TextDecoder('utf-16le').decode(b.subarray(2));
  if (b[0] === 0xfe && b[1] === 0xff) return new TextDecoder('utf-16be').decode(b.subarray(2));
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(b);
  } catch {
    return new TextDecoder('windows-1251').decode(b);
  }
}

/** Текст файла лора на телефоне. */
export async function readPhoneLore(name: string, buf: ArrayBuffer): Promise<string> {
  const ext = fileExt(name);
  if (!PHONE_LORE_EXT.includes(ext)) throw new PhoneLoreError('ext');
  if (buf.byteLength > PHONE_LORE_MAX_BYTES) throw new PhoneLoreError('size');
  if (ext !== 'docx') return decodeText(buf);
  try {
    return await docxText(buf);
  } catch (e) {
    throw new PhoneLoreError('docx', e instanceof DocxError ? e.message : String(e));
  }
}

/** Загрузить файл лора на телефоне. progress(сделано, всего) — векторы кусков. */
export async function addPhoneLore(
  persona: string,
  file: File,
  progress?: (done: number, total: number) => void,
): Promise<LocalLoreFile> {
  const name = cleanFilename(file.name);
  const buf = await file.arrayBuffer();
  const body = cleanText(await readPhoneLore(name, buf));
  const pieces = chunkText(body);
  if (!pieces.length) throw new PhoneLoreError('empty');
  const have = (await getLocalLore(persona)) ?? { files: [], chunks: [] };
  const other = have.files.filter((f) => f.name !== name).reduce((n, f) => n + f.chars, 0);
  if (other + Array.from(body).length > PHONE_LORE_MAX_CHARS) throw new PhoneLoreError('total');

  // Векторы — пачками: модель на телефоне считает их по одному-два в секунду
  const chunks: LoreChunk[] = pieces.map((p) => ({ f: name, s: p.s, t: p.t }));
  let vectors = true;
  for (let i = 0; i < chunks.length && vectors; i += EMBED_BATCH) {
    progress?.(i, chunks.length);
    const part = chunks.slice(i, i + EMBED_BATCH);
    const vecs = await embedTexts(part.map((c) => (c.s ? `${c.s}: ${c.t}` : c.t)));
    if (!vecs || vecs.length !== part.length) {
      vectors = false;
      break;
    }
    part.forEach((c, k) => {
      c.v = packVec(vecs[k]) || undefined;
    });
  }
  if (!vectors) chunks.forEach((c) => delete c.v);
  progress?.(chunks.length, chunks.length);

  const entry: LocalLoreFile = {
    name,
    ext: fileExt(name),
    chars: Array.from(body).length,
    chunks: chunks.length,
    size: buf.byteLength,
    ts: Math.floor(Date.now() / 1000),
    text: body,
    vectors,
  };
  const next: LocalLore = {
    files: [...have.files.filter((f) => f.name !== name), entry],
    chunks: [...have.chunks.filter((c) => c.f !== name), ...chunks],
  };
  await putLocalLore(persona, next, { [name]: buf });
  return entry;
}

/** Убрать файл, загруженный на телефоне (и его исходник). */
export async function removePhoneLore(persona: string, name: string): Promise<void> {
  const have = await getLocalLore(persona);
  if (!have) return;
  const next: LocalLore = {
    files: have.files.filter((f) => f.name !== name),
    chunks: have.chunks.filter((c) => c.f !== name),
  };
  await putLocalLore(persona, next, { [name]: null });
}

/** Персона сменила id (перенос с телефона на ядро под свободным id). */
export async function moveLocalLore(from: string, to: string): Promise<void> {
  const have = await getLocalLore(from);
  if (!have) return;
  const originals: Record<string, ArrayBuffer | null> = {};
  for (const f of have.files) {
    const buf = await getLoreOriginal(from, f.name);
    if (buf) originals[f.name] = buf;
  }
  await putLocalLore(to, have, originals);
  await putLocalLore(from, null, Object.fromEntries(have.files.map((f) => [f.name, null])));
}

/** «Удалить с телефона» персону, которой нет на ядре, — и её лор. */
export async function forgetLocalLore(persona: string): Promise<void> {
  const have = await getLocalLore(persona);
  if (have) await putLocalLore(persona, null, Object.fromEntries(have.files.map((f) => [f.name, null])));
}

/** Файлы лора персоны для списка без связи: с ядра (копия на телефоне) и
 * загруженные здесь. */
export async function phoneLoreFiles(persona: string): Promise<{
  core: { name: string; chunks: number; sections: number }[];
  local: LocalLoreFile[];
}> {
  const [copy, local] = await Promise.all([getLoreCopy(persona), getLocalLore(persona)]);
  const mine = new Set(local?.files.map((f) => f.name));
  const byFile = new Map<string, { chunks: number; sections: Set<string> }>();
  for (const c of copy?.chunks ?? []) {
    if (mine.has(c.f)) continue;
    const e = byFile.get(c.f) ?? { chunks: 0, sections: new Set<string>() };
    e.chunks += 1;
    if (c.s) e.sections.add(c.s);
    byFile.set(c.f, e);
  }
  return {
    core: [...byFile].map(([name, e]) => ({ name, chunks: e.chunks, sections: e.sections.size })),
    local: local?.files ?? [],
  };
}

/** Куски одного файла (для просмотра без связи). */
export async function phoneLoreChunks(persona: string, name: string): Promise<LoreChunk[]> {
  const local = await getLocalLore(persona);
  if (local?.files.some((f) => f.name === name)) return local.chunks.filter((c) => c.f === name);
  return ((await getLoreCopy(persona))?.chunks ?? []).filter((c) => c.f === name);
}

let syncing: Promise<string[]> | null = null;

/** Ядро на связи: файлы лора с телефона — на него. Персона с телефона ждёт
 * своего переноса (personaSync.ts). → персоны, чей лор ушёл. */
export function syncLocalLore(): Promise<string[]> {
  if (syncing) return syncing;
  syncing = (async () => {
    const done: string[] = [];
    if (!isNativeApp()) return done;
    for (const persona of await localLorePersonas()) {
      if (isLocalPersona(persona)) continue;
      const have = await getLocalLore(persona);
      if (!have) continue;
      let sent = false;
      for (const f of have.files) {
        if (f.error) continue;
        const buf = await getLoreOriginal(persona, f.name);
        // Исходника нет (запись повреждена) — уходит прочитанный текст
        const body = buf ?? new TextEncoder().encode(f.text).buffer;
        const name = buf || f.ext !== 'docx' ? f.name : `${f.name.replace(/\.docx$/i, '')}.md`;
        try {
          await api.uploadLore(persona, new File([body], name));
          await removePhoneLore(persona, f.name);
          sent = true;
        } catch (e) {
          const status = e instanceof ApiError ? e.status : 0;
          if (status === 400 || status === 413 || status === 422) {
            const cur = await getLocalLore(persona);
            if (cur) {
              const files = cur.files.map((x) => (x.name === f.name ? { ...x, error: e instanceof Error ? e.message : String(e) } : x));
              await putLocalLore(persona, { ...cur, files });
            }
            continue;
          }
          // 404 — персоны на ядре нет (ещё не перенеслась); сеть, 401, 5xx —
          // при следующей связи
          break;
        }
      }
      if (sent) done.push(persona);
    }
    return done;
  })().finally(() => {
    syncing = null;
  });
  return syncing;
}

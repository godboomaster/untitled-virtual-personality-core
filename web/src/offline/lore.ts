import { stemWord, terms } from './recall';
import { cosine, unpackVec } from './vectors';
import type { Vec } from './vectors';

/* Лор персоны на телефоне без ядра — тот же поиск, что app/core/lore.py: куски
   и их векторы приходят с ядра (GET …/offline/lore), вектор реплики телефон
   считает той же моделью. Модели векторов нет — ищет только по словам. Без
   зависимостей приложения: scripts/test_lore.py сверяет с ядром под Node. */

export const LORE_LIMIT = 10;
export const LORE_WEAK_LIMIT = 5;
const RRF_K = 20;
const CONTEXT_WEIGHT = 0.6;
const STRONG_SIM = 0.5;
const WORD_SIM = 0.35;
const SPREAD_SIM = 0.15;
const RARE_SHARE = 0.015;
const RARE_MIN = 3;
const RARE_CAP = 15;
const TAIL_CHARS = 200;
const K1 = 1.2;
const B = 0.75;

export interface LoreChunk {
  f: string; // файл
  s: string; // раздел
  t: string; // текст
  v?: string; // вектор (offline_api.pack_vector)
}

export interface LoreData {
  persona: string;
  stamp: string;
  chunks: LoreChunk[];
  vectors: { model: string; dim: number } | null;
}

export interface LoreIndex {
  chunks: LoreChunk[];
  vecs: (Vec | null)[];
  postings: Map<string, [number, number][]>; // основа → [(кусок, сколько раз)]
  docLen: Float32Array;
  avgLen: number;
}

export interface LoreHit {
  index: number;
  section: string;
  text: string;
  similarity: number;
  words: boolean;
}

export interface LoreResult {
  hits: LoreHit[];
  weak: boolean;
  skipped: 'smalltalk' | 'off_topic' | 'no_lore' | null;
}

export interface LoreRules {
  lore_head?: string;
  lore_weak?: string;
  lore?: string;
  lore_gap?: string;
}

const EMPTY: LoreResult = { hits: [], weak: false, skipped: 'no_lore' };

// \b в JS не знает кириллицы — границы слова явно
const L = '[\\p{L}\\p{N}_]';
const wordRe = (body: string) => new RegExp(`(?<!${L})(?:${body})(?!${L})`, 'giu');

const YOU =
  'у\\s+тебя|с\\s+тобой|ты|тебя|тебе|тобой|твой|твоя|твоё|твое|твои|твою|твоего|твоей|твоих|твоим|you|your|yours';
const YOU_VERB = '[а-яё]{2,}(?:ешь|ёшь|ишь)(?:ся)?|о\\s+себе|про\\s+себя';
const THEM = 'он|она|они|оно|его|её|ее|их|ему|ей|им|ним|ней|них|it|he|she|they|him|her|them';
const FOLLOW_RE = /^(?:а|и|но|and|but|so)\s/iu;
const SENTENCE_RE = /(?<=[.!?…])\s+/u;
const WORD_RE = /[\p{L}\p{N}]+(?:-[\p{L}\p{N}]+)*/gu;

const wordsOf = (text: string) => text.match(WORD_RE) ?? [];
const maxOf = (values: number[]) => values.reduce((a, b) => Math.max(a, b), -Infinity);
const embedText = (c: LoreChunk) => (c.s ? `${c.s}: ${c.t}` : c.t);
const rareMax = (n: number) => Math.min(RARE_CAP, Math.max(RARE_MIN, Math.floor(n * RARE_SHARE)));

/** Индекс лора с ядра: векторы и список слов — один раз на загрузку. */
export function buildLoreIndex(data: LoreData, vectorsOk: boolean): LoreIndex {
  const chunks = data.chunks;
  const postings = new Map<string, [number, number][]>();
  const docLen = new Float32Array(chunks.length);
  chunks.forEach((c, i) => {
    const ts = terms(embedText(c));
    docLen[i] = ts.length;
    const tf = new Map<string, number>();
    for (const t of ts) tf.set(t, (tf.get(t) ?? 0) + 1);
    for (const [t, n] of tf) {
      const list = postings.get(t);
      if (list) list.push([i, n]);
      else postings.set(t, [[i, n]]);
    }
  });
  const total = docLen.reduce((a, b) => a + b, 0);
  return {
    chunks,
    vecs: chunks.map((c) => (vectorsOk ? unpackVec(c.v) : null)),
    postings,
    docLen,
    avgLen: chunks.length && total ? total / chunks.length : 1,
  };
}

function bm25(index: LoreIndex, queryTerms: string[]): { scores: Float64Array; rare: boolean[] } {
  const n = index.chunks.length;
  const scores = new Float64Array(n);
  const rare = new Array<boolean>(n).fill(false);
  const max = rareMax(n);
  for (const t of new Set(queryTerms)) {
    const posting = index.postings.get(t);
    if (!posting) continue;
    const df = posting.length;
    const idf = Math.log(1 + (n - df + 0.5) / (df + 0.5));
    for (const [i, tf] of posting) {
      const norm = 1 - B + (B * index.docLen[i]) / index.avgLen;
      scores[i] += (idf * tf * (K1 + 1)) / (tf + K1 * norm);
      if (df <= max) rare[i] = true;
    }
  }
  return { scores, rare };
}

/** Запрос для модели векторов: «у тебя» → имя, иначе имя приставкой. */
export function vectorQuery(query: string, name: string): string {
  if (!name) return query;
  const replaced = query.replace(wordRe(YOU), () => name);
  return replaced !== query ? replaced : `${name}: ${query}`;
}

/** Тексты для векторов реплики: сама реплика, с именем, с контекстом. */
export function loreQueryTexts(query: string, previous: string, name: string): string[] {
  const out = [query, vectorQuery(query, name)];
  if (previous.trim()) out.push(vectorQuery(`${previous.trim()} ${query}`, name));
  return out;
}

function aimedAtCharacter(query: string, names: string[]): boolean {
  if (wordRe(YOU).test(query) || wordRe(YOU_VERB).test(query)) return true;
  const own = new Set(names.flatMap((n) => wordsOf(n.toLowerCase()).map(stemWord)));
  return wordsOf(query.toLowerCase().replace(/ё/g, 'е')).some((w) => own.has(stemWord(w)));
}

function namesInLore(query: string, index: LoreIndex, names: string[]): boolean {
  const own = new Set(names.flatMap((n) => wordsOf(n.toLowerCase()).map(stemWord)));
  for (const sentence of query.split(SENTENCE_RE)) {
    for (const word of wordsOf(sentence).slice(1)) {
      if (word[0] !== word[0].toLowerCase() && word !== word.toUpperCase()) {
        const stem = stemWord(word);
        if (!own.has(stem) && index.postings.has(stem)) return true;
      }
    }
  }
  return false;
}

// Места элементов: значения по убыванию, равные — по порядку
function ranks(values: ArrayLike<number>): Int32Array {
  const order = Array.from(values, (_, i) => i).sort((a, b) => values[b] - values[a] || a - b);
  const out = new Int32Array(order.length);
  order.forEach((i, r) => {
    out[i] = r;
  });
  return out;
}

export interface LoreQuery {
  query: string;
  previous?: string;
  name?: string;
  names?: string[];
  limit?: number;
  smalltalk?: boolean; // решает гейт (gate.ts)
  vectors?: (Vec | null)[] | null; // векторы loreQueryTexts; нет — только по словам
}

/** Куски к реплике: лучшие сверху. Как LoreStore.search ядра. */
export function searchLore(index: LoreIndex | null, q: LoreQuery): LoreResult {
  if (!index?.chunks.length) return EMPTY;
  const n = index.chunks.length;
  const query = q.query.trim();
  const previous = (q.previous ?? '').trim();
  const name = q.name ?? '';
  const names = (q.names ?? []).filter(Boolean);
  if (!names.length && name) names.push(name);
  const limit = q.limit ?? LORE_LIMIT;
  const queryTerms = terms(query);
  const aimed = aimedAtCharacter(query, names);
  if (q.smalltalk || !(queryTerms.length || aimed)) return { hits: [], weak: false, skipped: 'smalltalk' };

  const forms: [string, number][] = [[query, 1]];
  if (previous) forms.push([`${previous} ${query}`, CONTEXT_WEIGHT]);
  const vectors = q.vectors && q.vectors[0] && index.vecs.some(Boolean) ? q.vectors : null;
  const plain = vectors ? index.vecs.map((v) => (v ? cosine(vectors[0]!, v) : -1)) : null;

  const rareNow = bm25(index, queryTerms).rare;
  const rareAny = rareNow.some(Boolean);
  // Без векторов смысл не сверить: слово считается темой только у вопроса к
  // персонажу или рядом с именем из лора
  const closeWord = plain ? rareAny && maxOf(plain.filter((_, i) => rareNow[i])) >= WORD_SIM : false;
  const named = namesInLore(query, index, names);
  const followUp = !!previous && (wordRe(THEM).test(query) || FOLLOW_RE.test(query));
  if (!(aimed || closeWord || named || followUp)) return { hits: [], weak: false, skipped: 'off_topic' };

  const total = new Float64Array(n);
  const simsBest = new Float64Array(n).fill(-1);
  const rareBoth = new Array<boolean>(n).fill(false);
  forms.forEach(([text, weight], f) => {
    const { scores, rare } = bm25(index, terms(text));
    const qvec = vectors?.[f + 1] ?? null;
    if (qvec) {
      const sims = index.vecs.map((v) => (v ? cosine(qvec, v) : -1));
      const place = ranks(sims);
      sims.forEach((s, i) => {
        total[i] += weight / (RRF_K + place[i]);
        simsBest[i] = Math.max(simsBest[i], s);
      });
    }
    if (scores.some((s) => s > 0)) {
      const place = ranks(scores);
      scores.forEach((s, i) => {
        if (s > 0) total[i] += weight / (RRF_K + place[i]);
      });
    }
    rare.forEach((r, i) => {
      if (r) rareBoth[i] = true;
    });
  });

  let order = Array.from(total, (_, i) => i).sort((a, b) => total[b] - total[a] || a - b);
  if (!vectors) order = order.filter((i) => total[i] > 0);
  order = order.slice(0, limit);
  if (!order.length) return { hits: [], weak: false, skipped: null };
  const strong = closeWord || named || (aimed && rareAny) || (plain ? maxOf(plain) >= STRONG_SIM : false);
  let top = order.slice(0, strong ? limit : Math.min(limit, LORE_WEAK_LIMIT));
  if (vectors) {
    const best = maxOf(top.map((i) => simsBest[i]));
    top = top.filter((i) => rareBoth[i] || simsBest[i] >= best - SPREAD_SIM);
  }
  return {
    hits: top.map((i) => ({
      index: i,
      section: index.chunks[i].s ?? '',
      text: index.chunks[i].t,
      similarity: simsBest[i],
      words: rareBoth[i],
    })),
    weak: !strong,
    skipped: null,
  };
}

/** Контекст запроса: прошлая реплика и конец ответа персоны. */
export function previousTurn(history: { role: string; text: string }[]): string {
  const last = (role: string) => [...history].reverse().find((h) => h.role === role)?.text ?? '';
  return `${last('user')} ${last('assistant').slice(-TAIL_CHARS)}`.trim();
}

function groupLines(hits: LoreHit[]): string[] {
  const groups: { section: string; parts: string[]; last: number }[] = [];
  for (const h of [...hits].sort((a, b) => a.index - b.index)) {
    const g = groups[groups.length - 1];
    if (g && g.section === h.section && g.last === h.index - 1) {
      g.parts.push(h.text);
      g.last = h.index;
    } else groups.push({ section: h.section, parts: [h.text], last: h.index });
  }
  return groups.map((g) => (g.section ? `[${g.section}] ${g.parts.join(' ')}` : g.parts.join(' ')));
}

/** Блок лора для системного текста — как build_block ядра. */
export function loreBlock(rules: LoreRules, result: LoreResult): string {
  const gap = rules.lore_gap ?? '';
  if (result.skipped === 'no_lore') return '';
  if (!result.hits.length) return gap;
  const head = (result.weak ? rules.lore_weak : rules.lore_head) ?? '';
  return [head, ...groupLines(result.hits), '', rules.lore ?? '', gap].join('\n').trim();
}

/** Лор для модели телефона — в реплику пользователя, как найденная память:
 * из конца длинного системного текста маленькая модель его не берёт. */
export function loreNote(result: LoreResult | null | undefined, max: number): string {
  if (!result?.hits.length || max <= 0) return '';
  const lines = groupLines(result.hits.slice(0, max)).map((l) => `- ${l}`);
  return `(Note from the app, not from the user — your own memories, use them only if they fit:\n${lines.join('\n')})`;
}

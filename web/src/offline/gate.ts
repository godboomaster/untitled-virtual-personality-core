/* Гейт веб-поиска без ноутбука — порт app/features/search_gate.py: не искать
   для одного обращения по имени, реплик-связок и вопросов о самой персоне,
   а по белому списку («Экономия нейронки») — искать только по явной просьбе
   или вопросу о внешнем/свежем. Шаблоны и списки приходят в ките
   (search_gate) — те же, что у ядра. Модуль без зависимостей: его сверяет с
   ядром scripts/test_offline_gate.py. */

// Белый список: «*» в конце — префикс, пробел — фраза из слов подряд
export interface GateNeed {
  search: string[];
  search_start: string[];
  topic: string[];
  fresh: string[];
  wh: string[];
  ask: string[];
  you: string[];
  me: string[];
  you_suffix: string[];
  who: string[];
  pronoun: string[];
  latin_stop: string[];
}

export interface GateRules {
  address_words: string[];
  smalltalk: string[];
  self_patterns: string[];
  // Нет — ядро без белого списка: ищем всё, кроме связок (как раньше)
  need?: GateNeed;
  need_limits?: { fresh_min_words: number; topic_short_words: number };
  // Пункт «Экономии нейронки» search_every_message: белый список выключен
  every_message?: boolean;
}

export interface GateKit {
  name: string;
  persona: string;
  search_gate?: GateRules;
}

interface Gate {
  address: Set<string>;
  smalltalk: Set<string>;
  self: RegExp[];
  // Служебные слова — не сущность, даже с заглавной
  stop: Set<string>;
}

const gateCache = new WeakMap<object, Gate>();

function compileGate(raw: GateRules): Gate {
  const have = gateCache.get(raw);
  if (have) return have;
  const self: RegExp[] = [];
  for (const p of raw.self_patterns) {
    try {
      // \w в Python — буквы Unicode; в JS даже с флагом u — только латиница
      self.push(new RegExp(`^(?:${p.replace(/\\w/g, '[\\p{L}\\p{N}_]')})$`, 'u'));
    } catch {
      /* шаблон, который JS не понял, — без него */
    }
  }
  const stop = new Set<string>(['i', ...raw.address_words, ...(raw.need?.latin_stop ?? [])]);
  if (raw.need) {
    for (const list of [raw.need.wh, raw.need.you, raw.need.ask, raw.need.pronoun]) {
      for (const p of list) for (const w of p.split(' ')) stop.add(w);
    }
  }
  const gate = { address: new Set(raw.address_words), smalltalk: new Set(raw.smalltalk), self, stop };
  gateCache.set(raw, gate);
  return gate;
}

function normalize(text: string): string {
  return text
    .toLowerCase()
    .replace(/ё/g, 'е')
    .replace(/[^\p{L}\p{N}_\s-]+/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

const wordHit = (w: string, p: string) => (p.endsWith('*') ? w.startsWith(p.slice(0, -1)) : w === p);

// Длина совпадения фразы pattern со слов words[i..] (0 — нет)
function matchAt(words: string[], i: number, pattern: string): number {
  const parts = pattern.split(' ');
  if (i + parts.length > words.length) return 0;
  return parts.every((p, j) => wordHit(words[i + j], p)) ? parts.length : 0;
}

// Номера слов, покрытых совпадениями любого из шаблонов
function spans(words: string[], patterns: string[]): Set<number> {
  const hit = new Set<number>();
  for (const p of patterns) {
    for (let i = 0; i < words.length; i++) {
      const n = matchAt(words, i, p);
      for (let k = 0; k < n; k++) hit.add(i + k);
    }
  }
  return hit;
}

const TOKEN_RE = /[.!?…]+(?=\s|$)|\n|\p{L}+|[0-9]+/gu;
const QUOTED_RE = /«[^»]{2,}»|"[^"]{2,}"|“[^”]{2,}”/;
const CYR_RE = /[а-яё]/i;
const LATIN_RE = /^[a-zA-Z]{2,}$/;

/* Сущность в сыром тексте: кавычки, год, латиница в русском тексте, слово с
   заглавной не в начале предложения (обращение и служебные слова — нет). */
function hasEntity(gate: Gate, text: string, nameWords: Set<string>): boolean {
  if (QUOTED_RE.test(text)) return true;
  const latinOk = CYR_RE.test(text);
  // Всё капсом — заглавные ничего не значат
  const capsOk = /\p{Ll}/u.test(text);
  let start = true;
  for (const m of text.matchAll(TOKEN_RE)) {
    const tok = m[0];
    if (tok === '\n' || '.!?…'.includes(tok[0])) {
      start = true;
      continue;
    }
    if (/^[0-9]+$/.test(tok)) {
      const n = Number(tok);
      if (tok.length === 4 && n >= 1000 && n <= 2099) return true;
      start = false;
      continue;
    }
    const low = tok.toLowerCase().replace(/ё/g, 'е');
    if (nameWords.has(low)) continue; // обращение прозрачно
    const first = start;
    start = false;
    if (gate.stop.has(low)) continue;
    if (latinOk && LATIN_RE.test(tok)) return true;
    if (capsOk && !first && /^\p{Lu}/u.test(tok)) return true;
  }
  return false;
}

/* Почему поиск нужен ('explicit' | 'topic' | 'fact' | 'fresh') или null —
   порт search_need_reason ядра. */
function needReason(gate: Gate, rules: GateRules, need: GateNeed, words: string[], text: string,
  nameWords: Set<string>): string | null {
  if (!words.length) return null;
  if (spans(words, need.search).size || need.search_start.some((p) => matchAt(words, 0, p) > 0)) return 'explicit';
  const ask = spans(words, need.ask);
  const question = text.includes('?') || text.includes('？') || ask.size > 0
    || need.wh.some((p) => matchAt(words, 0, p) > 0);
  const lim = rules.need_limits ?? { fresh_min_words: 4, topic_short_words: 5 };
  if (spans(words, need.topic).size && (question || words.length <= lim.topic_short_words)) return 'topic';
  if (!question) return null;
  // Вопрос к самой персоне («как тебе…», «что думаешь…») — из её памяти
  const rest = words.filter((_, i) => !ask.has(i));
  const you = new Set(need.you);
  if (rest.some((w) => you.has(w) || need.you_suffix.some((s) => w.length >= s.length + 2 && w.endsWith(s)))) {
    return null;
  }
  const pronoun = new Set(need.pronoun);
  for (const p of need.who) {
    for (let i = 0; i < words.length; i++) {
      const n = matchAt(words, i, p);
      if (n && i + n < words.length && !pronoun.has(words[i + n])) return 'fact';
    }
  }
  if (hasEntity(gate, text, nameWords)) return 'fact';
  const me = new Set(need.me);
  if (spans(words, need.fresh).size && words.length >= lim.fresh_min_words && !rest.some((w) => me.has(w))) {
    return 'fresh';
  }
  return null;
}

// Слова реплики без обращения по имени и междометий с краёв
function coreWords(gate: Gate, names: string[], text: string): string[] {
  let words = normalize(text).split(' ').filter(Boolean);
  const startsWith = (w: string[], n: string[]) => n.every((x, i) => w[i] === x);
  for (let changed = true; words.length && changed; ) {
    changed = false;
    for (const n of names) {
      const nw = n.split(' ');
      if (startsWith(words, nw)) {
        words = words.slice(nw.length);
        changed = true;
        break;
      }
      if (words.length >= nw.length && startsWith(words.slice(-nw.length), nw)) {
        words = words.slice(0, -nw.length);
        changed = true;
        break;
      }
    }
    if (!changed && words.length && gate.address.has(words[0])) {
      words = words.slice(1);
      changed = true;
    }
  }
  return words;
}

const gateNames = (kit: GateKit) =>
  [kit.name, kit.persona].map(normalize).filter(Boolean).sort((a, b) => b.length - a.length);

// Обращение, связка или вопрос о дне самой персоны: внешние данные не нужны
function isChatter(gate: Gate, core: string): boolean {
  return !core || gate.smalltalk.has(core) || gate.self.some((r) => r.test(core));
}

/** Болтовня без темы: вспоминать нечего (search_skip_reason ядра). */
export function isSmalltalk(kit: GateKit, text: string): boolean {
  if (!kit.search_gate) return false;
  const gate = compileGate(kit.search_gate);
  return isChatter(gate, coreWords(gate, gateNames(kit), text).join(' '));
}

/** Нужен ли веб-поиск: не нужен для обращений, связок и вопросов о самой
    персоне; с белым списком — только по явной просьбе или вопросу о внешнем. */
export function wantsSearch(kit: GateKit, text: string): boolean {
  if (!kit.search_gate) return false;
  const rules = kit.search_gate;
  const gate = compileGate(rules);
  const names = gateNames(kit);
  const words = coreWords(gate, names, text);
  if (isChatter(gate, words.join(' '))) return false;
  if (!rules.need || rules.every_message) return true;
  const nameWords = new Set(names.flatMap((n) => n.split(' ')));
  return needReason(gate, rules, rules.need, words, text, nameWords) !== null;
}

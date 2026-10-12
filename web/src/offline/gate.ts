/* Гейт веб-поиска без ноутбука — порт app/features/search_gate.py: не искать
   для одного обращения по имени, реплик-связок и вопросов о самой персоне.
   Шаблоны приходят в ките (search_gate) — те же, что у ядра. Модуль без
   зависимостей: его сверяет с ядром scripts/test_offline_gate.py. */

export interface GateRules {
  address_words: string[];
  smalltalk: string[];
  self_patterns: string[];
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
  const gate = { address: new Set(raw.address_words), smalltalk: new Set(raw.smalltalk), self };
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

/** Нужен ли веб-поиск: не нужен для обращений, связок и вопросов о самой персоне. */
export function wantsSearch(kit: GateKit, text: string): boolean {
  if (!kit.search_gate) return false;
  const gate = compileGate(kit.search_gate);
  const names = [kit.name, kit.persona].map(normalize).filter(Boolean).sort((a, b) => b.length - a.length);
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
  const core = words.join(' ');
  if (!core || gate.smalltalk.has(core)) return false;
  return !gate.self.some((r) => r.test(core));
}

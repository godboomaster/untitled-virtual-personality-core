/* Цепочка ответа персоны без ядра — чистая логика (без импортов: сверяется
   под Node, scripts/test_phone_chain.py). Устроена как цепочка персоны на
   ядре (llm.primary / fallback / exclude), только звенья — телефона:
     «<провайдер>»     — облако по ключу (ключи с ядра и/или телефона),
     «webchat:<сайт>»  — веб-чат телефона без ключа (включённый на телефоне),
     «gemma»           — модель на телефоне,
     «pc:…»            — веб-чаты ПК (pc:webchat:<сайт>) и локальная модель ПК
                         (pc:local): живут на компьютере, без ядра не отвечают —
                         всегда в убранных, вернуть их нельзя.
   Пока на телефоне цепочку персоны не меняли, она берётся с компьютера
   (копия настроек персоны). */

export type ChainKind = 'api' | 'webchat' | 'gemma' | 'pc';

export const GEMMA = 'gemma';

// Как llm персоны на ядре: основной, порядок после него, убранные
export interface PersonaChain {
  primary: string | null;
  fallback: string[];
  exclude: string[];
}

// Шаг ответа: звенья одного рода подряд — одним шагом. only: null — все
// ключи персоны с ядра и телефона в их порядке (запасной путь)
export type ChainStep = { kind: 'cloud'; only: string[] | null } | { kind: 'web'; sites: string[] } | { kind: 'gemma' };

export function kindOf(id: string): ChainKind | null {
  if (id === GEMMA) return 'gemma';
  if (id.startsWith('pc:')) return id === 'pc:local' || (id.startsWith('pc:webchat:') && id.length > 11) ? 'pc' : null;
  if (id.startsWith('webchat:')) return id.length > 8 ? 'webchat' : null;
  return /^[\w.-]+$/.test(id) ? 'api' : null;
}

/** Звено цепочки ядра → звено телефона; null — на телефоне такого нет.
 * Веб-чаты и локальная модель ядра — звенья ПК (pc:…), а не сайты и Gemma
 * телефона: это другие собеседники, хоть сайт и тот же. */
export function phoneToken(token: string, items: string[]): string | null {
  const id = token === 'local' || token.startsWith('webchat:') ? `pc:${token}` : token;
  return items.includes(id) ? id : null;
}

/** Звено ПК: без ядра не отвечает. */
export function isPc(id: string): boolean {
  return kindOf(id) === 'pc';
}

function uniq(list: (string | null)[]): string[] {
  const out: string[] = [];
  for (const x of list) if (x && !out.includes(x)) out.push(x);
  return out;
}

/** Цепочка с компьютера (llm персоны и общий основной ядра) — на звенья телефона. */
export function chainFromCore(
  llm: { primary?: string | null; fallback?: string[]; exclude?: string[] } | null,
  globalActive: string | null,
  items: string[],
): { state: PersonaChain; active: string | null } {
  const map = (t: string | null | undefined) => (t ? phoneToken(t, items) : null);
  return {
    state: {
      primary: map(llm?.primary),
      fallback: uniq((llm?.fallback ?? []).map(map)),
      exclude: uniq((llm?.exclude ?? []).map(map)),
    },
    active: map(globalActive),
  };
}

/** Звено, которого в порядке ещё нет (новое или возвращённое): перед Gemma,
 * если она последняя (дальше Gemma очередь почти не доходит — она отвечает
 * и без интернета), иначе в конец. Основным не становится. */
export function placeNew(order: string[], id: string): string[] {
  const out = order.filter((x) => x !== id);
  const g = out.indexOf(GEMMA);
  if (g > 0 && g === out.length - 1) out.splice(g, 0, id);
  else out.push(id);
  return out;
}

/** Порядок персоны: основной (её или общий с ядра; убрать его нельзя, как
 * на ядре), дальше её порядок, остальные — по placeNew; убранные — мимо.
 * items — кто есть на телефоне, в порядке по умолчанию. */
export function normalizeChain(
  items: string[],
  active: string | null,
  state: PersonaChain | null,
): { order: string[]; exclude: string[] } {
  // Звенья ПК без ядра — только в убранных
  const ex = new Set([...(state?.exclude ?? []), ...items.filter(isPc)]);
  const has = (x: string | null | undefined): x is string => !!x && items.includes(x) && !isPc(x);
  const primary = state?.primary ?? null;
  let main: string | null = null;
  if (has(primary)) main = primary;
  else if (has(active)) main = active;
  else main = items.find((x) => !ex.has(x)) ?? null;
  const exclude = [...ex].filter((x) => x !== main);
  let order = main ? [main] : [];
  for (const x of state?.fallback ?? []) if (has(x) && !ex.has(x) && !order.includes(x)) order.push(x);
  for (const x of items) if (!ex.has(x) && !order.includes(x)) order = placeNew(order, x);
  return { order, exclude };
}

/** Порядок → шаги ответа: подряд идущие ключи — один запрос к плагину
 * (он перебирает их ключи), подряд идущие веб-чаты — один перебор сайтов. */
export function chainSteps(order: string[]): ChainStep[] {
  const steps: ChainStep[] = [];
  for (const id of order) {
    const kind = kindOf(id);
    const last = steps[steps.length - 1];
    if (kind === 'api') {
      if (last?.kind === 'cloud' && last.only) last.only.push(id);
      else steps.push({ kind: 'cloud', only: [id] });
    } else if (kind === 'webchat') {
      const site = id.slice(8);
      if (last?.kind === 'web') last.sites.push(site);
      else steps.push({ kind: 'web', sites: [site] });
    } else if (kind === 'gemma' && !steps.some((s) => s.kind === 'gemma')) {
      steps.push({ kind: 'gemma' });
    }
  }
  return steps;
}

// ── Правки в досье (order[0] — основной) ──────────────────────────────

/** Выше (-1) или ниже (1) среди тех, кто после основного. */
export function moveIn(order: string[], id: string, dir: -1 | 1): string[] {
  const out = [...order];
  const i = out.indexOf(id);
  const j = i + dir;
  if (i < 1 || j < 1 || j >= out.length) return out;
  [out[i], out[j]] = [out[j], out[i]];
  return out;
}

/** Сделать основным: старый основной — первым после него (как на ядре). */
export function makeFirst(order: string[], id: string): string[] {
  return order.includes(id) ? [id, ...order.filter((x) => x !== id)] : [...order];
}

/** Убрать из цепочки (основного — нельзя). */
export function excludeIn(order: string[], exclude: string[], id: string): { order: string[]; exclude: string[] } {
  if (order[0] === id || !order.includes(id)) return { order: [...order], exclude: [...exclude] };
  return { order: order.filter((x) => x !== id), exclude: exclude.includes(id) ? [...exclude] : [...exclude, id] };
}

/** Вернуть убранного — по placeNew; звено ПК без ядра не возвращается. */
export function restoreIn(order: string[], exclude: string[], id: string): { order: string[]; exclude: string[] } {
  if (isPc(id)) return { order: [...order], exclude: [...exclude] };
  return { order: placeNew(order, id), exclude: exclude.filter((x) => x !== id) };
}

/** Порядок → запись цепочки персоны (звенья ПК не пишутся: они убраны всегда). */
export function toChain(order: string[], exclude: string[]): PersonaChain {
  return { primary: order[0] ?? null, fallback: order.slice(1), exclude: exclude.filter((x) => x !== order[0] && !isPc(x)) };
}

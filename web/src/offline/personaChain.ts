import { useEffect, useReducer } from 'react';
import { OfflineLlm } from './plugin';
import type { PersonaProvider } from './plugin';
import { PHONE_CATALOG } from './phoneKeys';
import { getSettingsCopy } from './settingsCopy';
import { webchatPrefs } from './webchat';
import { enabledSites } from './webchatSites';
import type { WebchatSite } from './webchatSites';
import { chainFromCore, chainSteps, GEMMA, normalizeChain } from './chainOrder';
import type { ChainKind, ChainStep, PersonaChain } from './chainOrder';

/* Цепочка провайдеров персоны без ядра (досье → «Настройки» → «LLM-
   провайдеры», PhoneChainCard): у каждой персоны своя, как на компьютере.
   Пока её на телефоне не меняли — берётся с компьютера (копия настроек
   персоны, settingsCopy.ts) и следует за ним; веб-чаты и локальная модель
   ПК без ядра не отвечают — они в убранных (chainOrder.ts); поменяли — хранится на
   телефоне (localStorage) и действует, пока нет связи с ядром. На ядро не
   уходит: с ядром на связи отвечает ядро по своей цепочке. Цепочку персоны
   поменяли на ядре — телефонная правка снимается (Settings.tsx), и телефон
   снова следует за компьютером. */

const PREFIX = 'vpc-phone-chain:';
const listeners = new Set<() => void>();

export function savedChain(persona: string): PersonaChain | null {
  try {
    const raw = JSON.parse(localStorage.getItem(PREFIX + persona) || 'null') as Partial<PersonaChain> | null;
    if (!raw || !Array.isArray(raw.fallback)) return null;
    const strs = (a: unknown) => (Array.isArray(a) ? a.filter((x): x is string => typeof x === 'string') : []);
    return { primary: typeof raw.primary === 'string' ? raw.primary : null, fallback: strs(raw.fallback), exclude: strs(raw.exclude) };
  } catch {
    return null;
  }
}

/** Сохранить цепочку персоны; null — снова как на компьютере. */
export function saveChain(persona: string, chain: PersonaChain | null) {
  try {
    if (chain) localStorage.setItem(PREFIX + persona, JSON.stringify(chain));
    else localStorage.removeItem(PREFIX + persona);
  } catch {
    // нет хранилища — цепочка живёт до перезапуска
  }
  for (const l of listeners) l();
}

export function onChainChange(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

export function useChainVersion(): number {
  const [n, bump] = useReducer((x: number) => x + 1, 0);
  useEffect(() => onChainChange(bump), []);
  return n;
}

export interface ChainItem {
  id: string;
  kind: ChainKind;
  name: string;
  provider?: PersonaProvider; // облако: модель, маски, откуда ключи
  site?: WebchatSite; // веб-чат телефона
  pc?: 'webchat' | 'local'; // звено ПК: веб-чат в браузере ядра или его локальная модель
}

function providerName(id: string): string {
  return PHONE_CATALOG.find((c) => c.id === id)?.name ?? id.charAt(0).toUpperCase() + id.slice(1);
}

// Сайты веб-чатов ядра (app/features/web_llm.py ADAPTERS)
const PC_SITE_NAMES: Record<string, string> = {
  deepseek: 'DeepSeek',
  chatgpt: 'ChatGPT',
  google: 'Google AI Mode',
  google_anon: 'Google AI Mode (anon)',
  duckai: 'Duck.ai',
  kimi: 'Kimi',
  qwen: 'Qwen',
  zai: 'Z.ai',
  claude: 'Claude',
};

/** Звенья ПК персоны — как строки её цепочки на компьютере: включённые на
 * ядре веб-чаты и сайты из её llm, локальная модель ядра. */
function pcItems(persona: string): ChainItem[] {
  const copy = getSettingsCopy(persona);
  if (!copy) return [];
  const llm = copy.config.llm;
  const sites = [...copy.webchat.sites];
  for (const tok of [llm?.primary, ...(llm?.fallback ?? []), ...(llm?.exclude ?? [])]) {
    const site = tok?.startsWith('webchat:') ? tok.slice(8) : '';
    if (site && !sites.includes(site)) sites.push(site);
  }
  const out: ChainItem[] = sites.map((site) => ({
    id: `pc:webchat:${site}`,
    kind: 'pc' as const,
    name: PC_SITE_NAMES[site] ?? site.charAt(0).toUpperCase() + site.slice(1),
    pc: 'webchat' as const,
  }));
  const local = copy.providers.find((p) => p.local);
  if (local) out.push({ id: 'pc:local', kind: 'pc', name: local.name, pc: 'local' });
  return out;
}

/** Кем персона может отвечать на телефоне — в порядке по умолчанию: ключи
 * (её с ядра, другие ключи ядра, ключи телефона), включённые веб-чаты
 * телефона, Gemma; в конце — звенья ПК (без ядра всегда в убранных). */
export async function chainItems(persona: string): Promise<ChainItem[]> {
  const providers = await OfflineLlm.personaProviders({ persona })
    .then((r) => r.providers)
    .catch(() => [] as PersonaProvider[]);
  const out: ChainItem[] = providers.map((p) => ({ id: p.name, kind: 'api' as const, name: providerName(p.name), provider: p }));
  for (const s of enabledSites(webchatPrefs())) out.push({ id: `webchat:${s.id}`, kind: 'webchat', name: s.name, site: s });
  out.push({ id: GEMMA, kind: 'gemma', name: 'Gemma' });
  return [...out, ...pcItems(persona)];
}

export interface LoadedChain {
  items: ChainItem[];
  order: string[]; // [0] — основной
  exclude: string[];
  saved: boolean; // меняли на телефоне
}

/** Цепочка персоны сейчас: своя телефонная или с компьютера. */
export async function loadChain(persona: string): Promise<LoadedChain> {
  const items = await chainItems(persona);
  const ids = items.map((i) => i.id);
  const copy = getSettingsCopy(persona);
  const core = chainFromCore(copy?.config.llm ?? null, copy?.providers.find((p) => p.active)?.id ?? null, ids);
  const saved = savedChain(persona);
  const { order, exclude } = normalizeChain(ids, core.active, saved ?? core.state);
  return { items, order, exclude, saved: !!saved };
}

/** Шаги ответа персоны без ядра — по её цепочке. */
export async function answerSteps(persona: string): Promise<ChainStep[]> {
  return chainSteps((await loadChain(persona)).order);
}

/** Запасной путь (цепочку собрать не вышло): ключи, веб-чаты, Gemma. */
export function defaultSteps(): ChainStep[] {
  return [{ kind: 'cloud', only: null }, { kind: 'web', sites: enabledSites(webchatPrefs()).map((s) => s.id) }, { kind: 'gemma' }];
}

import { api, ApiError, isNativeApp } from '../api';
import { offlinePersonaList } from '../apiData';
import type { OfflineKitResponse } from '../api';
import { OfflineLlm } from './plugin';
import { refreshMemory } from './memory';
import { refreshSettingsCopy } from './settingsCopy';
import { buildLocalKit, isLocalPersona, localPersona } from './localPersonas';
import type { LocalPersona } from './localPersonas';

/* Персона-кит на телефоне: характер, правила офлайна, факты, хвост
   переписки — всё, чтобы персона ответила без ноутбука. Чат обновляет его
   при открытии и после каждого ответа, пока ноутбук на связи; вместе с ним
   (не чаще KEYS_TTL_MS) — ключи облачных провайдеров персоны: они уходят
   сразу в нативное хранилище (OfflineLlm.setProviders, шифрование ключом
   Android Keystore). Вся переписка и факты — отдельно и реже (memory.ts). */

// fallback — запасной: кита с ноутбука нет, персона по имени и описанию;
// local — собран на телефоне из YAML персоны, созданной без ядра (localPersonas.ts)
export type OfflineKit = OfflineKitResponse & { fetched_at: number; fallback?: boolean; local?: boolean };

const PREFIX = 'vpc-offline-kit:';
const KEYS_TTL_MS = 10 * 60_000;
const ALL_TTL_MS = 10 * 60_000; // киты всех персон — не чаще
const ERROR_PREFIX = 'vpc-offline-kit-error:';
let allAt = 0;
const inflight = new Map<string, Promise<OfflineKit | null>>();
const keysAt = new Map<string, number>();
const refreshed = new Set<(persona: string) => void>();

/** Кит персоны обновился (reminders.ts: будильник телефона — заново). */
export function onKitRefreshed(fn: (persona: string) => void): () => void {
  refreshed.add(fn);
  return () => {
    refreshed.delete(fn);
  };
}

/** Персоны, чей кит есть на телефоне. */
export function kitPersonas(): string[] {
  const out: string[] = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k?.startsWith(PREFIX)) out.push(k.slice(PREFIX.length));
    }
  } catch {
    /* хранилище недоступно */
  }
  return out;
}

function storedKit(persona: string): OfflineKit | null {
  try {
    const raw = localStorage.getItem(PREFIX + persona);
    return raw ? (JSON.parse(raw) as OfflineKit) : null;
  } catch {
    return null;
  }
}

/** Кит персоны: с ядра, а у созданной на телефоне — из её YAML. */
export function getKit(persona: string): OfflineKit | null {
  const kit = storedKit(persona);
  if (kit) return kit;
  const local = localPersona(persona);
  return local ? localKit(local) : null;
}

/** Записать кит персоны (с ядра или замену ему на время переноса). */
export function storeKit(persona: string, kit: OfflineKit) {
  try {
    localStorage.setItem(PREFIX + persona, JSON.stringify(kit));
  } catch {
    /* хранилище переполнено — кит проживёт в памяти до перезапуска */
  }
  donor = undefined;
}

/** Убрать кит и ошибку его загрузки (персона удалена с телефона). */
export function forgetKit(persona: string) {
  try {
    localStorage.removeItem(PREFIX + persona);
    localStorage.removeItem(ERROR_PREFIX + persona);
  } catch {
    /* хранилище недоступно */
  }
  donor = undefined;
  localMemo.delete(persona);
}

/** Кит и ошибка — под новым id (id персоны сменился при переносе на ядро). */
export function renameKit(from: string, to: string) {
  for (const prefix of [PREFIX, ERROR_PREFIX]) {
    try {
      const raw = localStorage.getItem(prefix + from);
      if (raw === null) continue;
      localStorage.setItem(prefix + to, raw);
      localStorage.removeItem(prefix + from);
    } catch {
      /* хранилище недоступно */
    }
  }
  donor = undefined;
  localMemo.delete(from);
}

// Общее для всех персон (правила, заметки напоминаний, гейт поиска, погода)
// — из самого свежего кита с ядра; undefined — ещё не искали (сброс при записи)
let donor: OfflineKit | null | undefined;
const localMemo = new Map<string, { key: string; kit: OfflineKit }>();

function donorKit(): OfflineKit | null {
  if (donor !== undefined) return donor;
  let best: OfflineKit | null = null;
  for (const id of kitPersonas()) {
    const k = storedKit(id);
    if (!k || k.local || k.fallback || k.version < 2 || !k.rules) continue;
    if (!best || k.fetched_at > best.fetched_at) best = k;
  }
  donor = best;
  return best;
}

function uiLanguage(): string | null {
  try {
    const v = localStorage.getItem('vpc-lang');
    return v === 'ru' || v === 'en' ? v : null;
  } catch {
    return null;
  }
}

// Кит из YAML собирается заново, только когда сменились YAML, кит-источник
// или язык: getKit зовут на каждом рендере списков (lastMessage.ts)
function localKit(p: LocalPersona): OfflineKit {
  const from = donorKit();
  const lang = uiLanguage();
  const key = `${p.yaml}\u0000${from?.persona ?? ''}:${from?.fetched_at ?? 0}\u0000${lang ?? ''}`;
  const memo = localMemo.get(p.id);
  if (memo && memo.key === key) return memo.kit;
  const kit = buildLocalKit(p, from, lang);
  localMemo.set(p.id, { key, kit });
  return kit;
}

const FALLBACK_RULES =
  "You are answering from the user's phone: the computer you normally run on is unreachable, " +
  "and your full profile, memory and tools haven't reached the phone yet. Stay in character as " +
  'best you can from the short description above, keep replies short and conversational, and ' +
  "don't invent facts about the user. Don't claim to set reminders, add to-dos, search the web " +
  "or control the computer — say you'll do it once you're back on the computer.";

/** Кита нет (телефон ни разу не застал ноутбук с этой персоной) — запасной
 * по имени и описанию из списка персон: ответит, хоть и проще. */
export function fallbackKit(persona: string, sample: string): OfflineKit | null {
  const p = offlinePersonaList().find((x) => x.id === persona);
  if (!p) return null;
  const character = `You are ${p.name}.${p.description ? ` ${p.description}` : ''}`;
  const language = /[а-яё]/i.test(sample)
    ? 'The user speaks Russian in the dialogue: write all free-text output in Russian.'
    : '';
  const empty = { set: '', ask_time: '', past: '', recurring: '', scheduled: '', extract: '' };
  return {
    version: 0,
    persona,
    name: p.name,
    generated_at: 0,
    fetched_at: 0,
    fallback: true,
    system: [character, FALLBACK_RULES, language].filter(Boolean).join('\n\n'),
    parts: { character, rules: FALLBACK_RULES, style: '', language },
    facts: [],
    history: [],
    env: null,
    reminders: [],
    reminders_enabled: false,
    rules: { web: '', env: '' },
    notes: empty,
    search_gate: { address_words: [], smalltalk: [], self_patterns: [] },
  };
}

/** Последняя ошибка загрузки кита — для «Проверить» в настройках. */
export function kitError(persona: string): { message: string; at: number } | null {
  try {
    const raw = localStorage.getItem(ERROR_PREFIX + persona);
    return raw ? (JSON.parse(raw) as { message: string; at: number }) : null;
  } catch {
    return null;
  }
}

function setKitError(persona: string, e: unknown) {
  try {
    if (e === null) {
      localStorage.removeItem(ERROR_PREFIX + persona);
      return;
    }
    const message = e instanceof ApiError ? `HTTP ${e.status}: ${e.message}` : e instanceof Error ? e.message : String(e);
    localStorage.setItem(ERROR_PREFIX + persona, JSON.stringify({ message, at: Date.now() }));
  } catch {
    /* хранилище недоступно */
  }
}

/** Ноутбук на связи — киты (и ключи) всех персон, а не только открытых чатов:
 * без ноутбука ответит любая. force — без учёта ALL_TTL_MS. */
export async function refreshAllKits(force = false): Promise<void> {
  if (!isNativeApp() || (!force && Date.now() - allAt < ALL_TTL_MS)) return;
  const list = offlinePersonaList();
  if (!list.length) return; // список персон ещё не пришёл — попытка не в счёт
  allAt = Date.now();
  for (const p of list) await refreshKit(p.id);
}

/** Обновить кит (и, если пора, ключи) с ноутбука. */
export function refreshKit(persona: string): Promise<OfflineKit | null> {
  if (!isNativeApp()) return Promise.resolve(null);
  const have = getKit(persona);
  // Персоны с телефона на ядре ещё нет — запрос дал бы только 404
  if (isLocalPersona(persona)) return Promise.resolve(have);
  const running = inflight.get(persona);
  if (running) return running;
  const job = (async () => {
    try {
      const kit: OfflineKit = { ...(await api.getOfflineKit(persona)), fetched_at: Date.now() };
      storeKit(persona, kit);
      setKitError(persona, null);
      refreshed.forEach((fn) => fn(persona));
      await refreshMemory(persona, kit);
      if (Date.now() - (keysAt.get(persona) ?? 0) >= KEYS_TTL_MS) {
        try {
          const { providers } = await api.getOfflineProviders(persona);
          await OfflineLlm.setProviders({ persona, providers });
          keysAt.set(persona, Date.now());
        } catch {
          /* ключи не обновились — остаются прежние */
        }
        // Копия настроек для досье без связи (settingsCopy.ts) — с той же частотой
        await refreshSettingsCopy(persona).catch(() => {});
      }
      return kit;
    } catch (e) {
      setKitError(persona, e);
      return have;
    } finally {
      inflight.delete(persona);
    }
  })();
  inflight.set(persona, job);
  return job;
}

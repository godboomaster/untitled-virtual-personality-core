import { api, isNativeApp } from '../api';
import type { OfflineHistoryLine, OfflineMemoryResponse } from '../api';
import type { OfflineKit } from './kit';
import { EMBED_DIM, EMBED_MODEL } from './vectors';
import { buildLoreIndex } from './lore';
import type { LoreChunk, LoreData, LoreIndex } from './lore';

/* Память персоны на телефоне: вся переписка чата (до 500 сообщений STM) и
   все факты о собеседнике — с ноутбука (GET …/offline/memory), не чаще
   MEMORY_TTL_MS: свежий хвост переписки приходит с китом. Лежит в IndexedDB
   — в localStorage столько не влезет. С векторами из базы ядра — для
   поиска по смыслу (около 0,5 КБ на сообщение).

   Без ноутбука модель видит хвост переписки, а реплики постарше к теме
   вопроса находит поиск по словам и по смыслу (recall.ts, embed.ts). */

export interface OfflineMemory extends OfflineMemoryResponse {
  fetched_at: number;
}

const DB_NAME = 'vpc-offline';
const STORE = 'memory';
const MEMORY_TTL_MS = 10 * 60_000;

let dbPromise: Promise<IDBDatabase> | null = null;
const cache = new Map<string, OfflineMemory | null>();
const inflight = new Map<string, Promise<void>>();

function openDb(): Promise<IDBDatabase> {
  if (!dbPromise) {
    dbPromise = new Promise<IDBDatabase>((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    dbPromise.catch(() => {
      dbPromise = null;
    });
  }
  return dbPromise;
}

async function idb<T>(mode: IDBTransactionMode, run: (s: IDBObjectStore) => IDBRequest): Promise<T> {
  const db = await openDb();
  return new Promise<T>((resolve, reject) => {
    const req = run(db.transaction(STORE, mode).objectStore(STORE));
    req.onsuccess = () => resolve(req.result as T);
    req.onerror = () => reject(req.error);
  });
}

/** Память персоны с телефона; null — её ещё нет (или хранилище недоступно). */
export async function getMemory(persona: string): Promise<OfflineMemory | null> {
  if (cache.has(persona)) return cache.get(persona) ?? null;
  try {
    const mem = (await idb<OfflineMemory | undefined>('readonly', (s) => s.get(persona))) ?? null;
    cache.set(persona, mem);
    return mem;
  } catch {
    return null;
  }
}

/** Ноутбук на связи: память персоны — если её нет или пора освежить. */
export function refreshMemory(persona: string, kit: OfflineKit, force = false): Promise<void> {
  if (!isNativeApp() || kit.version < 3) return Promise.resolve();
  const running = inflight.get(persona);
  if (running) return running;
  const job = (async () => {
    const have = await getMemory(persona);
    // Память разошлась с ноутбуком — забрать сразу, не дожидаясь срока:
    // между её концом и хвостом кита разрыв или сообщений стало не столько
    // (дописаны в прошлое, стёрты из середины)
    const last = have?.history[have.history.length - 1]?.ts ?? 0;
    const stale =
      !!have &&
      ((kit.history.length > 0 && kit.history[0].ts > last + 0.001) ||
        (kit.history_total !== undefined && kit.history_total !== have.history.length));
    if (!force && !stale && have && Date.now() - have.fetched_at < MEMORY_TTL_MS) return;
    try {
      const mem: OfflineMemory = { ...(await api.getOfflineMemory(persona)), fetched_at: Date.now() };
      cache.set(persona, mem);
      await idb('readwrite', (s) => s.put(mem, persona));
    } catch {
      /* не пришла или не записалась — прежняя (в памяти процесса — свежая) */
    }
  })().finally(() => inflight.delete(persona));
  inflight.set(persona, job);
  return job;
}

// ── Лор персоны: куски и векторы с ядра, по метке в ките, и файлы,
// загруженные на телефоне без ядра (localLore.ts) — до переноса на ядро ──

/** Файл лора, загруженный на телефоне: текст и куски — здесь, сам файл —
 * отдельной записью (уходит на ядро как есть). */
export interface LocalLoreFile {
  name: string;
  ext: string;
  chars: number;
  chunks: number;
  size: number; // байт исходного файла
  ts: number; // секунды
  text: string; // прочитанный текст (после разбора docx)
  vectors: boolean; // куски с векторами модели телефона (нет — поиск по словам)
  error?: string; // ядро не приняло файл — ждёт, пока его удалят
}

export interface LocalLore {
  files: LocalLoreFile[];
  chunks: LoreChunk[];
}

const loreKey = (persona: string) => `lore:${persona}`;
const localLoreKey = (persona: string) => `lore-local:${persona}`;
const originalKey = (persona: string, name: string) => `lore-orig:${persona}:${name}`;
const LOCAL_PREFIX = 'lore-local:';
const loreCache = new Map<string, { stamp: string; index: LoreIndex } | null>();
const loreInflight = new Map<string, Promise<void>>();

// Копия с ядра и файлы телефона — в один индекс: файл телефона заменяет
// одноимённый файл ядра (загрузили новую версию без связи)
function indexLore(core: LoreData | null, local: LocalLore | null): { stamp: string; index: LoreIndex } | null {
  const coreOk = !!core?.vectors && core.vectors.model === EMBED_MODEL && core.vectors.dim === EMBED_DIM;
  const mine = new Set(local?.files.map((f) => f.name));
  const chunks: LoreChunk[] = [
    ...(core?.chunks ?? []).filter((c) => !mine.has(c.f)).map((c) => (coreOk ? c : { ...c, v: undefined })),
    ...(local?.chunks ?? []),
  ];
  if (!chunks.length) return null;
  const stamp = core?.stamp ?? '';
  const data: LoreData = { persona: core?.persona ?? '', stamp, chunks, vectors: { model: EMBED_MODEL, dim: EMBED_DIM } };
  return { stamp, index: buildLoreIndex(data, true) };
}

/** Копия лора с ядра (для списка файлов без связи); null — её нет. */
export async function getLoreCopy(persona: string): Promise<LoreData | null> {
  try {
    return (await idb<LoreData | undefined>('readonly', (s) => s.get(loreKey(persona)))) ?? null;
  } catch {
    return null;
  }
}

/** Файлы лора, загруженные на телефоне и ещё не ушедшие на ядро. */
export async function getLocalLore(persona: string): Promise<LocalLore | null> {
  try {
    return (await idb<LocalLore | undefined>('readonly', (s) => s.get(localLoreKey(persona)))) ?? null;
  } catch {
    return null;
  }
}

/** Записать файлы телефона (пусто — убрать запись); originals — исходники:
 * ArrayBuffer — записать, null — удалить. */
export async function putLocalLore(
  persona: string,
  data: LocalLore | null,
  originals: Record<string, ArrayBuffer | null> = {},
): Promise<void> {
  for (const [name, buf] of Object.entries(originals)) {
    await idb('readwrite', (s) => (buf ? s.put(buf, originalKey(persona, name)) : s.delete(originalKey(persona, name))));
  }
  await idb('readwrite', (s) => (data?.files.length ? s.put(data, localLoreKey(persona)) : s.delete(localLoreKey(persona))));
  loreCache.delete(persona);
}

export async function getLoreOriginal(persona: string, name: string): Promise<ArrayBuffer | null> {
  try {
    return (await idb<ArrayBuffer | undefined>('readonly', (s) => s.get(originalKey(persona, name)))) ?? null;
  } catch {
    return null;
  }
}

/** Персоны, у которых на телефоне есть свои файлы лора. */
export async function localLorePersonas(): Promise<string[]> {
  try {
    const keys = await idb<IDBValidKey[]>('readonly', (s) => s.getAllKeys());
    return keys.map(String).filter((k) => k.startsWith(LOCAL_PREFIX)).map((k) => k.slice(LOCAL_PREFIX.length));
  } catch {
    return [];
  }
}

/** Лор персоны на телефоне; null — его нет. */
export async function getLore(persona: string): Promise<LoreIndex | null> {
  if (loreCache.has(persona)) return loreCache.get(persona)?.index ?? null;
  try {
    const [core, local] = await Promise.all([getLoreCopy(persona), getLocalLore(persona)]);
    const entry = indexLore(core, local);
    loreCache.set(persona, entry);
    return entry?.index ?? null;
  } catch {
    return null;
  }
}

/** Ядро на связи: лор — если метка разошлась с копией на телефоне. */
export function refreshLore(persona: string, kit: OfflineKit): Promise<void> {
  if (!isNativeApp() || kit.lore_stamp === undefined) return Promise.resolve();
  const running = loreInflight.get(persona);
  if (running) return running;
  const job = (async () => {
    const have = await getLoreCopy(persona);
    if ((have?.stamp ?? '') === kit.lore_stamp) return;
    try {
      if (!kit.lore_stamp) {
        await idb('readwrite', (s) => s.delete(loreKey(persona)));
      } else {
        const data = await api.getOfflineLore(persona);
        await idb('readwrite', (s) => s.put(data, loreKey(persona)));
      }
      loreCache.delete(persona);
    } catch {
      /* не пришёл или не записался — прежний */
    }
  })().finally(() => loreInflight.delete(persona));
  loreInflight.set(persona, job);
  return job;
}

/** Переписка с телефона без стёртого на ноутбуке: старше начала истории в
 * ките — /clear или срезка STM, этого персона уже не помнит. */
export function memoryHistory(kit: OfflineKit, mem: OfflineMemory | null): OfflineHistoryLine[] {
  if (!mem || kit.version < 3) return [];
  const first = kit.history_first_ts ?? 0;
  if (!first) return [];
  return mem.history.filter((m) => m.ts >= first - 0.001);
}

/** Векторы памяти с ноутбука годятся для поиска по смыслу: посчитаны той же
 * моделью, что есть на телефоне. */
export function memoryHasVectors(mem: OfflineMemory | null): boolean {
  return !!mem?.vectors && mem.vectors.model === EMBED_MODEL && mem.vectors.dim === EMBED_DIM;
}

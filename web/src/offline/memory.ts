import { api, isNativeApp } from '../api';
import type { OfflineHistoryLine, OfflineMemoryResponse } from '../api';
import type { OfflineKit } from './kit';

/* Память персоны на телефоне: вся переписка чата (до 500 сообщений STM) и
   все факты о собеседнике — с ноутбука (GET …/offline/memory), не чаще
   MEMORY_TTL_MS: свежий хвост переписки приходит с китом. Лежит в IndexedDB
   — в localStorage столько не влезет.

   Без ноутбука модель видит хвост переписки, а реплики постарше к теме
   вопроса находит поиск по словам (recall.ts). */

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

/** Переписка с телефона без стёртого на ноутбуке: старше начала истории в
 * ките — /clear или срезка STM, этого персона уже не помнит. */
export function memoryHistory(kit: OfflineKit, mem: OfflineMemory | null): OfflineHistoryLine[] {
  if (!mem || kit.version < 3) return [];
  const first = kit.history_first_ts ?? 0;
  if (!first) return [];
  return mem.history.filter((m) => m.ts >= first - 0.001);
}

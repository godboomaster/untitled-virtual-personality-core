import { api, isNativeApp } from '../api';
import type { OfflineKitResponse } from '../api';
import { OfflineLlm } from './plugin';

/* Персона-кит на телефоне: характер, правила офлайна, факты, хвост
   переписки — всё, чтобы персона ответила без ноутбука. Чат обновляет его
   при открытии и после каждого ответа, пока ноутбук на связи; вместе с ним
   (не чаще KEYS_TTL_MS) — ключи облачных провайдеров персоны: они уходят
   сразу в нативное хранилище (OfflineLlm.setProviders, шифрование ключом
   Android Keystore). */

export type OfflineKit = OfflineKitResponse & { fetched_at: number };

const PREFIX = 'vpc-offline-kit:';
const KEYS_TTL_MS = 10 * 60_000;
const inflight = new Map<string, Promise<OfflineKit | null>>();
const keysAt = new Map<string, number>();

export function getKit(persona: string): OfflineKit | null {
  try {
    const raw = localStorage.getItem(PREFIX + persona);
    return raw ? (JSON.parse(raw) as OfflineKit) : null;
  } catch {
    return null;
  }
}

/** Обновить кит (и, если пора, ключи) с ноутбука. */
export function refreshKit(persona: string): Promise<OfflineKit | null> {
  if (!isNativeApp()) return Promise.resolve(null);
  const have = getKit(persona);
  const running = inflight.get(persona);
  if (running) return running;
  const job = (async () => {
    try {
      const kit: OfflineKit = { ...(await api.getOfflineKit(persona)), fetched_at: Date.now() };
      try {
        localStorage.setItem(PREFIX + persona, JSON.stringify(kit));
      } catch {
        /* хранилище переполнено — кит проживёт в памяти до перезапуска */
      }
      if (Date.now() - (keysAt.get(persona) ?? 0) >= KEYS_TTL_MS) {
        try {
          const { providers } = await api.getOfflineProviders(persona);
          await OfflineLlm.setProviders({ persona, providers });
          keysAt.set(persona, Date.now());
        } catch {
          /* ключи не обновились — остаются прежние */
        }
      }
      return kit;
    } catch {
      return have;
    } finally {
      inflight.delete(persona);
    }
  })();
  inflight.set(persona, job);
  return job;
}

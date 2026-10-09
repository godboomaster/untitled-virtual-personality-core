import { useEffect, useReducer } from 'react';
import { api, isNativeApp } from '../api';
import { refetchPersonas, useCoreHealth } from '../apiData';
import { offlineMessages, personasWithOffline, removeOfflineMessages, unansweredTail } from './store';

/* Режим «без ноутбука» в приложении на телефоне и возврат к ноутбуку.

   Режим включён, когда ядро не отвечает (опрос /api/health), или приложение
   запущено без ноутбука кнопкой на экране запуска (offlineBoot) — пока ядро
   не ответит. Ядро снова на связи — переписка уходит на него
   (syncOffline): всё до последнего ответа — в историю и память
   (…/offline/import), а хвост реплик без ответа остаётся в очереди — чат
   отправит его обычным путём, чтобы на него ответила полная персона. */

export const OFFLINE_SYNCED_EVENT = 'vpc-offline-synced';

let offlineBoot = false;
const listeners = new Set<() => void>();

export function setOfflineBoot(on: boolean) {
  if (offlineBoot === on) return;
  offlineBoot = on;
  listeners.forEach((l) => l());
}

export function useOfflineMode(): boolean {
  const health = useCoreHealth();
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    listeners.add(force);
    return () => {
      listeners.delete(force);
    };
  }, []);
  return isNativeApp() && (health === 'down' || (offlineBoot && health !== 'ok'));
}

let syncing: Promise<string[]> | null = null;

/** Отдать ноутбуку переписку без него. → персоны, чья переписка ушла. */
export function syncOffline(): Promise<string[]> {
  if (syncing) return syncing;
  syncing = (async () => {
    const done: string[] = [];
    for (const persona of personasWithOffline()) {
      const list = offlineMessages(persona);
      const tail = unansweredTail(list);
      const send = list.slice(0, list.length - tail.length);
      if (!send.length) continue;
      try {
        await api.offlineImport(
          persona,
          send.map((m) => ({ id: m.id, role: m.role, text: m.text, ts: m.ts, source: m.source })),
        );
        removeOfflineMessages(persona, send.map((m) => m.id));
        done.push(persona);
      } catch {
        /* не ушло — попробуем при следующей связи */
      }
    }
    if (done.length) window.dispatchEvent(new CustomEvent(OFFLINE_SYNCED_EVENT, { detail: done }));
    return done;
  })().finally(() => {
    syncing = null;
  });
  return syncing;
}

/** App: ядро на связи — выйти из запуска без ноутбука и отдать ему переписку. */
export function useOfflineSync() {
  const health = useCoreHealth();
  useEffect(() => {
    if (!isNativeApp() || health !== 'ok') return;
    if (offlineBoot) {
      setOfflineBoot(false);
      void refetchPersonas();
    }
    void syncOffline();
  }, [health]);
}

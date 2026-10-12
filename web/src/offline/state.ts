import { useEffect, useReducer } from 'react';
import { api, isNativeApp } from '../api';
import { refetchPersonas, useApiPersonas, useCoreHealth } from '../apiData';
import { offlineMessages, personasWithOffline, removeOfflineMessages, unansweredTail } from './store';
import { pushAlarms, syncLocalReminders } from './reminders';
import { refreshAllKits } from './kit';
import { isLocalPersona, localPersonas } from './localPersonas';
import { followShortPrompt, syncLocalPersonas } from './personaSync';

/* Режим «без ноутбука» в приложении на телефоне и возврат к ноутбуку.

   Режим включён, когда ядро не отвечает (опрос /api/health), или приложение
   запущено без ноутбука кнопкой на экране запуска (offlineBoot) — пока ядро
   не ответит. Ядро снова на связи — переписка уходит на него
   (syncOffline): всё до последнего ответа — в историю и память
   (…/offline/import), а хвост реплик без ответа остаётся в очереди — чат
   отправит его обычным путём, чтобы на него ответила полная персона.
   Персоны, созданные на телефоне без ядра, уходят на него раньше переписки
   (personaSync.ts): без них ей некуда лечь. */

export const OFFLINE_SYNCED_EVENT = 'vpc-offline-synced';
// Сообщений за один приём — как MAX_IMPORT ядра (app/api/offline_api.py):
// больше — 422, и переписка не ушла бы никогда
const IMPORT_CHUNK = 200;
// Персона с телефона не перенеслась (ядро отвечает, а запрос оборвался) —
// повтор, пока связь есть
const LOCAL_RETRY_MS = 60_000;

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
      // Персоны с телефона на ядре ещё нет (404) — переписка ждёт её переноса
      if (isLocalPersona(persona)) continue;
      const list = offlineMessages(persona);
      const tail = unansweredTail(list);
      const send = list.slice(0, list.length - tail.length);
      let sent = 0;
      for (let i = 0; i < send.length; i += IMPORT_CHUNK) {
        const part = send.slice(i, i + IMPORT_CHUNK);
        try {
          await api.offlineImport(
            persona,
            part.map((m) => ({ id: m.id, role: m.role, text: m.text, ts: m.ts, source: m.source })),
          );
          removeOfflineMessages(persona, part.map((m) => m.id));
          sent += part.length;
        } catch {
          break; /* не ушло — остальное при следующей связи (повтор по id ядро не запишет дважды) */
        }
      }
      if (sent) done.push(persona);
    }
    if (done.length) window.dispatchEvent(new CustomEvent(OFFLINE_SYNCED_EVENT, { detail: done }));
    return done;
  })().finally(() => {
    syncing = null;
  });
  return syncing;
}

/** Ядро на связи: персоны с телефона, переписка, напоминания, киты — по
 * порядку (переписка персоны с телефона ложится на ядро после неё самой). */
export async function syncWithCore(): Promise<void> {
  const moved = await syncLocalPersonas();
  await syncOffline();
  // Кит перенесённой — сразу и, пока ядро составляет краткое описание, ещё
  for (const id of moved) void followShortPrompt(id);
  await syncLocalReminders();
  await refreshAllKits();
}

/** App: ядро на связи — выйти из запуска без ноутбука и отдать ему переписку
 * и поставленные без него напоминания. Запуск — будильник телефона заново
 * по сохранённым китам (на случай, если его сбросила система). */
export function useOfflineSync() {
  const health = useCoreHealth();
  // Список персон пришёл позже связи — киты всех персон тогда же
  const personaCount = useApiPersonas()?.length ?? 0;
  useEffect(() => {
    if (isNativeApp()) void pushAlarms();
  }, []);
  useEffect(() => {
    if (!isNativeApp() || health !== 'ok') return;
    if (offlineBoot) {
      setOfflineBoot(false);
      void refetchPersonas();
    }
    void syncWithCore();
    const timer = window.setInterval(() => {
      if (localPersonas().some((p) => p.status === 'pending')) void syncWithCore();
    }, LOCAL_RETRY_MS);
    return () => window.clearInterval(timer);
  }, [health, personaCount]);
}

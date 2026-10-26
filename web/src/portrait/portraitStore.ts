// Живые портреты персон: загрузка с ядра (GET /portrait) один раз на
// персону, общий кеш для голосового режима, арт-мастерской и PiP-окна.
// Атласы — data-URL по сотне-другой КБ, поэтому в localStorage не кладём:
// только в памяти. Здесь же — что персона сейчас делает в голосовом режиме
// (говорит, слушает, думает): портрет в PiP-окне живёт вместе с ним.
import { useEffect, useSyncExternalStore } from 'react';
import { api } from '../api';
import type { PortraitData } from './portraitTypes';

type Entry = { state: 'loading' | 'ready' | 'error'; data: PortraitData | null };

const cache = new Map<string, Entry>();
const listeners = new Set<() => void>();
// Ядро не ответило — повторить не раньше, чем через столько
const RETRY_MS = 30_000;

function emit() {
  listeners.forEach((l) => l());
}

function load(personaId: string) {
  const cur = cache.get(personaId);
  if (cur && cur.state !== 'error') return;
  cache.set(personaId, { state: 'loading', data: cur?.data ?? null });
  api
    .getPortrait(personaId)
    .then((data) => cache.set(personaId, { state: 'ready', data }))
    .catch(() => {
      cache.set(personaId, { state: 'error', data: null });
      setTimeout(() => {
        if (cache.get(personaId)?.state === 'error') cache.delete(personaId);
      }, RETRY_MS);
    })
    .finally(emit);
}

/** Портрет персоны (null — нет или ещё грузится). */
export function usePortrait(personaId: string): PortraitData | null {
  useEffect(() => load(personaId), [personaId]);
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => cache.get(personaId)?.data ?? null,
  );
}

/** Ответ ядра после загрузки или удаления листа — сразу всем. */
export function setPortrait(personaId: string, data: PortraitData) {
  cache.set(personaId, { state: 'ready', data });
  emit();
}

/** Есть ли что показать: без основного листа портрета нет. */
export function hasPortrait(data: PortraitData | null): data is PortraitData {
  return !!data?.sheets.A;
}

export interface PortraitActivity {
  speaking: boolean;
  listening: boolean;
  thinking: boolean;
}

const IDLE: PortraitActivity = { speaking: false, listening: false, thinking: false };
const activity = new Map<string, PortraitActivity>();

/** Голосовой режим сообщает, что делает персона; null — режим закрыт. */
export function setPortraitActivity(personaId: string, next: PortraitActivity | null) {
  const cur = activity.get(personaId);
  if (!next) {
    if (!cur) return;
    activity.delete(personaId);
  } else {
    if (cur && cur.speaking === next.speaking && cur.listening === next.listening && cur.thinking === next.thinking) return;
    activity.set(personaId, next);
  }
  emit();
}

/** Что персона делает в голосовом режиме (режим закрыт — ничего). */
export function usePortraitActivity(personaId: string): PortraitActivity {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => activity.get(personaId) ?? IDLE,
  );
}

// Погладили портрет — сказать ядру (до персоны дойдёт, только если включены
// «тычки» и не чаще раза в 15 мин). Подряд гладят часто: запрос — не чаще PET_POKE_MS
const PET_POKE_MS = 60_000;
const petPokedAt = new Map<string, number>();

export function pokePet(personaId: string) {
  const now = Date.now();
  if (now - (petPokedAt.get(personaId) ?? 0) < PET_POKE_MS) return;
  petPokedAt.set(personaId, now);
  api.pokeRoom(personaId, 'auto', 'pet').catch(() => {});
}

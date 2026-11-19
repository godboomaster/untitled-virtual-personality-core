// Живые портреты персон: загрузка с ядра (GET /portrait) один раз на
// аватар, общий кеш для голосового режима, раздела «Аватар» и PiP-окна.
// Атласы — data-URL по сотне-другой КБ, поэтому в localStorage не кладём:
// только в памяти. Здесь же — что персона сейчас делает в голосовом режиме
// (говорит, слушает, думает): портрет в PiP-окне живёт вместе с ним.
//
// Аватар — id персоны или «персона@режим» (api.splitAvatar): у режима свои
// листы (раздел «Аватар»). Голосовой режим и PiP показывают живой аватар
// (useLivePortrait): того режима, что включён в чате (app/api/avatar_modes).
// Режим чата приходит с опросом входящих (noteAvatarMode): сменился —
// живой аватар перечитывается.
import { useEffect, useSyncExternalStore } from 'react';
import { api, splitAvatar } from '../api';
import type { PortraitData } from './portraitTypes';

type Entry = { state: 'loading' | 'ready' | 'error'; data: PortraitData | null };

const cache = new Map<string, Entry>();
const listeners = new Set<() => void>();
// Ядро не ответило — повторить не раньше, чем через столько
const RETRY_MS = 30_000;
// Ключ живого аватара персоны в кеше
const LIVE = '#live';

function emit() {
  listeners.forEach((l) => l());
}

function fetchOf(key: string): Promise<PortraitData> {
  return key.endsWith(LIVE) ? api.getLivePortrait(key.slice(0, -LIVE.length)) : api.getPortrait(key);
}

function load(key: string, force = false) {
  const cur = cache.get(key);
  if (cur && cur.state !== 'error' && !force) return;
  if (cur?.state === 'loading') return;
  cache.set(key, { state: 'loading', data: cur?.data ?? null });
  fetchOf(key)
    .then((data) => cache.set(key, { state: 'ready', data }))
    .catch(() => {
      // Перечитать не вышло — остаётся прежнее (живой аватар не пропадает)
      if (force && cur?.data) {
        cache.set(key, { state: 'ready', data: cur.data });
        return;
      }
      cache.set(key, { state: 'error', data: null });
      setTimeout(() => {
        if (cache.get(key)?.state === 'error') cache.delete(key);
      }, RETRY_MS);
    })
    .finally(emit);
}

function useEntry(key: string): PortraitData | null {
  useEffect(() => load(key), [key]);
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => cache.get(key)?.data ?? null,
  );
}

/** Портрет аватара (id персоны или «персона@режим»; null — нет или ещё грузится). */
export function usePortrait(avatar: string): PortraitData | null {
  return useEntry(avatar);
}

/** Живой аватар персоны — режима, что включён в чате (голосовой режим,
 * PiP); mode в данных — какой это режим. */
export function useLivePortrait(personaId: string): PortraitData | null {
  return useEntry(personaId + LIVE);
}

/** Ответ ядра после загрузки или удаления листа — сразу всем; живой аватар
 * этой персоны — перечитать (листы могли быть его). */
export function setPortrait(avatar: string, data: PortraitData) {
  cache.set(avatar, { state: 'ready', data });
  const live = splitAvatar(avatar).persona + LIVE;
  if (cache.has(live)) load(live, true);
  emit();
}

/** Режим персоны в чате (опрос входящих): живой аватар другого режима —
 * перечитать (не вышло — попробует следующий опрос). */
export function noteAvatarMode(personaId: string, mode: string) {
  const live = cache.get(personaId + LIVE);
  if (!live?.data || (live.data.mode ?? '') === mode) return;
  load(personaId + LIVE, true);
}

/** Есть ли что показать: без основного листа портрета нет. */
export function hasPortrait(data: PortraitData | null): data is PortraitData {
  return !!data?.sheets.A;
}

export interface PortraitActivity {
  speaking: boolean;
  listening: boolean;
  thinking: boolean;
  /** Счётчик кивков: сменился — портрет кивает (услышал реплику) */
  nod: number;
  /** Что говорит и с какого знака звучащее слово — для рта по гласным */
  speech?: string;
  speechAt?: number;
}

const IDLE: PortraitActivity = { speaking: false, listening: false, thinking: false, nod: 0 };
const activity = new Map<string, PortraitActivity>();

/** Голосовой режим сообщает, что делает персона; null — режим закрыт. */
export function setPortraitActivity(personaId: string, next: PortraitActivity | null) {
  const cur = activity.get(personaId);
  if (!next) {
    if (!cur) return;
    activity.delete(personaId);
  } else {
    if (
      cur &&
      cur.speaking === next.speaking &&
      cur.listening === next.listening &&
      cur.thinking === next.thinking &&
      cur.nod === next.nod &&
      cur.speech === next.speech &&
      cur.speechAt === next.speechAt
    )
      return;
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

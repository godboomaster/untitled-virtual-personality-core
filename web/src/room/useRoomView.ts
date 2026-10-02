/* Поллинг комнаты: GET /api/personas/{p}/room раз в 60 с. Кеш и таймер —
   на уровне модуля (по образцу apiData): сколько бы потребителей ни было
   (раздел «Комната», PiP-окно, редактор размещения), на персону — один
   запрос в минуту. Пока все документы-владельцы скрыты (вкладка в фоне) —
   поллинг стоит; keepAlive (открытое PiP-окно) держит его живым. При
   возврате на вкладку — немедленное обновление, если данные устарели.

   Режимы: live — новый бэкенд с /room; legacy — старый бэкенд без /room
   (404): состояние берём из /state, конфиг и инвентарь — локальные;
   demo — бэкенд недоступен, комната живёт по демо-расписанию. */

import { useCallback, useEffect, useSyncExternalStore } from 'react';
import { ApiError, WEB_CHAT_ID, api } from '../api';
import type { LivingStateData } from '../api';
import type { RoomView } from './roomTypes';

export type RoomViewMode = 'live' | 'legacy' | 'demo';

export interface RoomViewEntry {
  view: RoomView | null;
  mode: RoomViewMode;
  loaded: boolean; // была хотя бы одна попытка
}

const POLL_MS = 60_000;
// Возврат на вкладку/новый потребитель: не перезапрашиваем чаще
const MIN_REFETCH_MS = 20_000;
// /room ответил 404 (старый бэкенд) — следующая попытка /room не раньше
const ROOM_RETRY_MS = 10 * 60_000;
// Краткий сбой сети не роняет комнату в демо: держим прошлые данные
const KEEP_STALE_MS = 3 * 60_000;

interface Meta {
  attemptAt: number;
  okAt: number;
  roomMissingAt: number;
  json: string;
}

interface Sub {
  doc: Document | null;
  keepAlive: boolean;
}

const EMPTY: RoomViewEntry = { view: null, mode: 'demo', loaded: false };
const entries = new Map<string, RoomViewEntry>();
const metas = new Map<string, Meta>();
const inflight = new Map<string, Promise<void>>();
const listeners = new Map<string, Set<() => void>>();
const subs = new Map<string, Set<Sub>>();
const timers = new Map<string, ReturnType<typeof setTimeout>>();

function meta(persona: string): Meta {
  let m = metas.get(persona);
  if (!m) {
    m = { attemptAt: 0, okAt: 0, roomMissingAt: 0, json: '' };
    metas.set(persona, m);
  }
  return m;
}

function emit(persona: string) {
  listeners.get(persona)?.forEach((l) => l());
}

// Старый бэкенд: /state → RoomView без конфига (конфиг берётся из моков)
function fromLegacy(s: LivingStateData): RoomView {
  const on = s.enabled && s.ui_sync && s.state;
  return {
    source: { context: '', chat_id: WEB_CHAT_ID, kind: 'web', last_activity: null },
    config: { props: [], pet: 'none', spots: [], is_default: true },
    living: on
      ? {
          enabled: true,
          ui_sync: true,
          state: s.state,
          recent_events: s.recent_events?.length ? s.recent_events : (s.last_events ?? []),
          plans: [],
        }
      : null,
    inventory: [],
    placements: {},
    focus: { active: false, started_at: null, minutes: null },
  };
}

async function load(persona: string): Promise<{ view: RoomView; mode: RoomViewMode }> {
  const m = meta(persona);
  if (!m.roomMissingAt || Date.now() - m.roomMissingAt > ROOM_RETRY_MS) {
    try {
      const view = await api.getRoom(persona);
      m.roomMissingAt = 0;
      return { view, mode: 'live' };
    } catch (e) {
      if (!(e instanceof ApiError && (e.status === 404 || e.status === 405))) throw e;
      m.roomMissingAt = Date.now();
    }
  }
  return { view: fromLegacy(await api.getLivingState(persona)), mode: 'legacy' };
}

function fetchRoom(persona: string): Promise<void> {
  const running = inflight.get(persona);
  if (running) return running;
  const m = meta(persona);
  m.attemptAt = Date.now();
  const p = load(persona)
    .then(({ view, mode }) => {
      m.okAt = Date.now();
      const json = JSON.stringify(view);
      const prev = entries.get(persona);
      // Структурное совпадение — тот же объект: подписчики не перерисовываются
      if (prev && prev.loaded && prev.mode === mode && json === m.json) return;
      m.json = json;
      entries.set(persona, { view, mode, loaded: true });
      emit(persona);
    })
    .catch(() => {
      const prev = entries.get(persona);
      if (prev?.loaded && prev.view && Date.now() - m.okAt < KEEP_STALE_MS) return;
      m.json = '';
      if (prev?.loaded && prev.mode === 'demo') return;
      entries.set(persona, { view: null, mode: 'demo', loaded: true });
      emit(persona);
    })
    .finally(() => {
      inflight.delete(persona);
    });
  inflight.set(persona, p);
  return p;
}

// Нужен ли поллинг: хоть один подписчик виден или держит keepAlive
function isActive(persona: string): boolean {
  const set = subs.get(persona);
  if (!set?.size) return false;
  for (const s of set) {
    if (s.keepAlive || !s.doc || !s.doc.hidden) return true;
  }
  return false;
}

// Один setTimeout-цикл на персону
function schedule(persona: string) {
  const prev = timers.get(persona);
  if (prev) clearTimeout(prev);
  timers.delete(persona);
  if (!isActive(persona)) return;
  const age = Date.now() - meta(persona).attemptAt;
  const delay = Math.max(1000, POLL_MS - age);
  timers.set(
    persona,
    setTimeout(() => {
      timers.delete(persona);
      if (!isActive(persona)) return;
      void fetchRoom(persona).finally(() => schedule(persona));
    }, delay),
  );
}

// Обновить сейчас, если данные старше порога (или force)
function refreshIfStale(persona: string, force = false) {
  const age = Date.now() - meta(persona).attemptAt;
  if (force || age > MIN_REFETCH_MS) {
    void fetchRoom(persona).finally(() => schedule(persona));
  } else {
    schedule(persona);
  }
}

// Принудительное обновление (после записи: добавили предмет, poke и т.п.)
export function refreshRoomView(persona: string): Promise<void> {
  return fetchRoom(persona).finally(() => schedule(persona));
}

export interface UseRoomViewOptions {
  // Документ, чья видимость управляет поллингом (PiP-окно передаёт свой)
  ownerDocument?: Document | null;
  // Не останавливать поллинг, пока документ скрыт (открыто PiP-окно)
  keepAlive?: boolean;
  enabled?: boolean;
}

export function useRoomView(
  persona: string,
  opts: UseRoomViewOptions = {},
): RoomViewEntry & { online: boolean; refresh: () => Promise<void> } {
  const { ownerDocument, keepAlive = false, enabled = true } = opts;

  const entry = useSyncExternalStore(
    useCallback(
      (cb: () => void) => {
        let set = listeners.get(persona);
        if (!set) {
          set = new Set();
          listeners.set(persona, set);
        }
        set.add(cb);
        return () => {
          set.delete(cb);
        };
      },
      [persona],
    ),
    () => entries.get(persona) ?? EMPTY,
  );

  useEffect(() => {
    if (!enabled) return;
    const doc = ownerDocument ?? (typeof document !== 'undefined' ? document : null);
    const sub: Sub = { doc, keepAlive };
    let set = subs.get(persona);
    if (!set) {
      set = new Set();
      subs.set(persona, set);
    }
    set.add(sub);
    refreshIfStale(persona);
    const onVisibility = () => {
      if (doc && !doc.hidden) refreshIfStale(persona);
      else schedule(persona); // все скрыты — таймер снимается
    };
    doc?.addEventListener('visibilitychange', onVisibility);
    return () => {
      doc?.removeEventListener('visibilitychange', onVisibility);
      set.delete(sub);
      schedule(persona);
    };
  }, [persona, ownerDocument, keepAlive, enabled]);

  const refresh = useCallback(() => refreshRoomView(persona), [persona]);
  return { ...entry, online: entry.mode === 'live', refresh };
}

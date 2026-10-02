/* «Поработать вместе» (body doubling): сессия фокуса на 25/50 минут.
   Состояние — на уровне модуля (один источник для раздела «Комната» и
   PiP-окна) и в localStorage на персону: перезагрузка и другие вкладки
   видят ту же сессию (событие storage). Онлайн сессия регистрируется на
   бэкенде (POST /room/focus start/end) и сверяется с view.focus из GET /room;
   офлайн/старый бэкенд — таймер чисто локальный, без запросов.

   Конец сессии — один setTimeout до времени окончания (пересчитывается при
   смене видимости документа), никаких посекундных тиков. Завершение
   (end) — ровно один раз на сессию даже при нескольких вкладках: Web Locks
   (если есть) + флаг в localStorage с меткой начала сессии. */

import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { api } from '../api';
import type { RoomFocus } from './roomTypes';
import { refreshRoomView } from './useRoomView';
import type { RoomViewMode } from './useRoomView';

export const FOCUS_OPTIONS = [25, 50] as const;

// Интервал обновления прогресса/остатка в интерфейсе
const CLOCK_MS = 30_000;
// Свежая сессия: «неактивно» в ответе поллинга может быть снятым до start
const FRESH_START_MS = 90_000;
// Допуск сравнения меток начала (локальная ↔ серверная, сек → мс)
const SAME_START_MS = 1500;

export interface FocusSession {
  startedAt: number; // epoch, мс
  minutes: number;
  live: boolean; // зарегистрирована на бэкенде (end тоже пойдёт туда)
}

export interface FocusResult {
  startedAt: number;
  line: string | null; // null — общая реплика из i18n
  early: boolean; // остановлено раньше срока
  pending: boolean; // ждём реплику с бэкенда
}

interface FocusEntry {
  session: FocusSession | null;
  result: FocusResult | null;
}

const EMPTY: FocusEntry = { session: null, result: null };
const entryKey = (persona: string) => `vpc-room-focus:${persona}`;
const endKey = (persona: string) => `vpc-room-focus-end:${persona}`;

const entries = new Map<string, FocusEntry>();
const listeners = new Set<() => void>();
const timers = new Map<string, ReturnType<typeof setTimeout>>();

function emit() {
  listeners.forEach((l) => l());
}

function parseEntry(raw: string | null): FocusEntry {
  if (!raw) return EMPTY;
  try {
    const v = JSON.parse(raw) as Partial<FocusEntry>;
    const s = v.session;
    const session = s && Number.isFinite(s.startedAt) && Number.isFinite(s.minutes) && s.minutes > 0
      ? { startedAt: s.startedAt, minutes: s.minutes, live: !!s.live }
      : null;
    const r = v.result;
    const result = r && Number.isFinite(r.startedAt)
      ? { startedAt: r.startedAt, line: typeof r.line === 'string' ? r.line : null, early: !!r.early, pending: !!r.pending }
      : null;
    return session || result ? { session, result } : EMPTY;
  } catch {
    return EMPTY;
  }
}

// null — localStorage недоступен (приватный режим и т.п.)
function readStorage(persona: string): FocusEntry | null {
  try {
    return parseEntry(localStorage.getItem(entryKey(persona)));
  } catch {
    return null;
  }
}

// Снимок персоны (кеш; первый доступ — из localStorage)
function read(persona: string): FocusEntry {
  let e = entries.get(persona);
  if (!e) {
    e = readStorage(persona) ?? EMPTY;
    entries.set(persona, e);
  }
  return e;
}

function write(persona: string, next: FocusEntry) {
  entries.set(persona, next);
  try {
    if (next.session || next.result) localStorage.setItem(entryKey(persona), JSON.stringify(next));
    else localStorage.removeItem(entryKey(persona));
  } catch {
    // Квота/приватный режим — сессия живёт в рамках вкладки
  }
  schedule(persona);
  emit();
}

function readEndFlag(persona: string): number {
  try {
    return Number(localStorage.getItem(endKey(persona)) ?? 0) || 0;
  } catch {
    return 0;
  }
}

function writeEndFlag(persona: string, startedAt: number) {
  try {
    localStorage.setItem(endKey(persona), String(startedAt));
  } catch {
    // без localStorage защита от двойного end — только в пределах вкладки
  }
}

const sameStart = (a: number, b: number) => Math.abs(a - b) < SAME_START_MS;

// ── Таймер окончания: один setTimeout на персону ──

let visibilityHooked = false;

function schedule(persona: string) {
  const prev = timers.get(persona);
  if (prev) clearTimeout(prev);
  timers.delete(persona);
  const s = entries.get(persona)?.session;
  if (!s) return;
  hookVisibility();
  const left = s.startedAt + s.minutes * 60_000 - Date.now();
  if (left <= 0) {
    void finishFocus(persona, false);
    return;
  }
  timers.set(
    persona,
    setTimeout(() => {
      timers.delete(persona);
      void finishFocus(persona, false);
    }, left + 250),
  );
}

// Пересчитать таймеры всех активных сессий (возврат на вкладку/в PiP:
// таймеры скрытых вкладок браузер придерживает)
export function recheckFocusTimers() {
  for (const [persona, e] of entries) if (e.session) schedule(persona);
}

// Слушатели уровня модуля ставятся один раз на жизнь приложения:
// видимость главного документа и синхронизация с другими вкладками
function hookVisibility() {
  if (visibilityHooked || typeof document === 'undefined') return;
  visibilityHooked = true;
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) recheckFocusTimers();
  });
  window.addEventListener('storage', (e) => {
    if (!e.key?.startsWith('vpc-room-focus:')) return;
    const persona = e.key.slice('vpc-room-focus:'.length);
    entries.set(persona, parseEntry(e.newValue));
    schedule(persona);
    emit();
  });
}

// Подгрузить сессии персон при старте приложения — сессия, начатая до
// перезагрузки, завершится вовремя, даже если «Комната» не открыта
export function primeFocusSessions(personas: string[]) {
  hookVisibility();
  for (const p of personas) {
    if (!entries.has(p)) read(p);
    if (entries.get(p)?.session && !timers.has(p)) schedule(p);
  }
}

// ── Действия ──

export function startFocus(persona: string, minutes: number, live: boolean) {
  const startedAt = Date.now();
  write(persona, { session: { startedAt, minutes, live }, result: null });
  if (!live) return;
  api
    .roomFocus(persona, 'start', minutes)
    .then((res) => {
      // Метка начала — серверная: по ней сверяемся с view.focus
      const sec = res.focus?.started_at;
      const cur = read(persona).session;
      if (sec && cur && cur.startedAt === startedAt) {
        write(persona, { ...read(persona), session: { ...cur, startedAt: Math.round(sec * 1000) } });
      }
      void refreshRoomView(persona);
    })
    .catch(() => {
      // Бэкенд не принял — таймер остаётся, но завершение будет локальным
      const cur = read(persona).session;
      if (cur && cur.startedAt === startedAt) write(persona, { ...read(persona), session: { ...cur, live: false } });
    });
}

// Завершение: по таймеру (early=false) или кнопкой «Закончить» (early=true).
// Захват под Web Lock: сессию снимает ровно одна вкладка
export async function finishFocus(persona: string, early: boolean): Promise<void> {
  const s = read(persona).session;
  if (!s) return;
  const claim = (): boolean => {
    // Свежее состояние из localStorage: другая вкладка могла уже завершить
    const fresh = (readStorage(persona) ?? entries.get(persona) ?? EMPTY).session;
    if (!fresh || !sameStart(fresh.startedAt, s.startedAt)) return false;
    if (sameStart(readEndFlag(persona), s.startedAt)) return false;
    writeEndFlag(persona, s.startedAt);
    write(persona, { session: null, result: { startedAt: s.startedAt, line: null, early, pending: s.live } });
    return true;
  };
  const locks = typeof navigator !== 'undefined' ? navigator.locks : undefined;
  let claimed = false;
  if (locks?.request) {
    try {
      claimed = await locks.request(`vpc-room-focus:${persona}`, () => claim());
    } catch {
      claimed = claim();
    }
  } else {
    claimed = claim();
  }
  if (!claimed) {
    // Завершила другая вкладка — подхватываем её результат из storage
    const fresh = readStorage(persona) ?? read(persona);
    if (!fresh.session || !sameStart(fresh.session.startedAt, s.startedAt)) {
      entries.set(persona, fresh);
      schedule(persona);
      emit();
    }
    return;
  }
  if (!s.live) return;
  let line: string | null = null;
  try {
    line = (await api.roomFocus(persona, 'end')).line ?? null;
  } catch {
    line = null;
  }
  const cur = read(persona);
  // Пока ждали, пользователь мог закрыть пузырь или начать новую сессию
  if (cur.result && cur.result.startedAt === s.startedAt) {
    write(persona, { ...cur, result: { ...cur.result, line, pending: false } });
  }
  void refreshRoomView(persona);
}

export function dismissFocusResult(persona: string) {
  const cur = read(persona);
  if (cur.result) write(persona, { ...cur, result: null });
}

// Сверка с сервером (view.focus из GET /room, только live):
// активная серверная сессия, которой нет локально, — начата в другом
// браузере, подхватываем; локальная live-сессия, которой сервер не знает
// (и она не только что начата), — завершена где-то ещё, тихо снимаем
function reconcileFocus(persona: string, focus: RoomFocus | null | undefined) {
  if (!focus) return;
  const cur = read(persona);
  if (focus.active && focus.started_at) {
    const start = Math.round(focus.started_at * 1000);
    if (sameStart(readEndFlag(persona), start)) return; // уже завершили, сервер ещё не обновился
    if (cur.session && sameStart(cur.session.startedAt, start)) return;
    write(persona, { ...cur, session: { startedAt: start, minutes: focus.minutes || FOCUS_OPTIONS[0], live: true } });
    return;
  }
  if (!focus.active && cur.session?.live && Date.now() - cur.session.startedAt > FRESH_START_MS) {
    write(persona, { ...cur, session: null });
  }
}

// ── Хук ──

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

// Часы сессии: тик раз в 30 с, пока сессия идёт и документ виден
function useFocusClock(active: boolean, doc: Document | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const d = doc ?? document;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = () => {
      if (timer) clearTimeout(timer);
      timer = undefined;
      if (d.hidden) return;
      setNow(Date.now());
      timer = setTimeout(tick, CLOCK_MS);
    };
    tick();
    d.addEventListener('visibilitychange', tick);
    return () => {
      if (timer) clearTimeout(timer);
      d.removeEventListener('visibilitychange', tick);
    };
  }, [active, doc]);
  return now;
}

export interface FocusApi {
  session: FocusSession | null;
  result: FocusResult | null;
  active: boolean;
  remainingMin: number; // округлено вверх
  progress: number; // 0..1
  start: (minutes: number) => void;
  stop: () => void;
  dismiss: () => void;
}

export function useFocusSession(
  persona: string,
  args: { focus: RoomFocus | null | undefined; mode: RoomViewMode; loaded: boolean; doc?: Document | null },
): FocusApi {
  const { focus, mode, loaded, doc = null } = args;
  const entry = useSyncExternalStore(subscribe, () => read(persona));
  const live = mode === 'live';

  // Сессия из localStorage — таймер окончания (идемпотентно)
  useEffect(() => {
    primeFocusSessions([persona]);
  }, [persona]);

  // Документ-владелец (PiP) стал видим — пересчитать таймеры
  useEffect(() => {
    if (!doc) return;
    const onVis = () => {
      if (!doc.hidden) recheckFocusTimers();
    };
    doc.addEventListener('visibilitychange', onVis);
    return () => doc.removeEventListener('visibilitychange', onVis);
  }, [doc]);

  const focusKey = focus ? `${focus.active}:${focus.started_at ?? ''}:${focus.minutes ?? ''}` : '';
  useEffect(() => {
    if (live && loaded) reconcileFocus(persona, focus);
    // focusKey — содержательная зависимость вместо объекта focus
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persona, live, loaded, focusKey]);

  const s = entry.session;
  const now = useFocusClock(!!s, doc);
  const total = s ? s.minutes * 60_000 : 1;
  const elapsed = s ? Math.min(total, Math.max(0, now - s.startedAt)) : 0;

  const start = useCallback((minutes: number) => startFocus(persona, minutes, live), [persona, live]);
  const stop = useCallback(() => {
    void finishFocus(persona, true);
  }, [persona]);
  const dismiss = useCallback(() => dismissFocusResult(persona), [persona]);

  const remainingMin = s ? Math.max(1, Math.ceil((total - elapsed) / 60_000)) : 0;
  const progress = s ? Math.round((elapsed / total) * 1000) / 1000 : 0;
  const result = entry.result;
  // Стабильный объект: memo-компоненты кнопок/оверлея не перерисовываются зря
  return useMemo(
    () => ({ session: s, result, active: !!s, remainingMin, progress, start, stop, dismiss }),
    [s, result, remainingMin, progress, start, stop, dismiss],
  );
}

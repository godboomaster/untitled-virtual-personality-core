/* Клиентские сигналы присутствия поверх серверного состояния:
   — with_you: пользователь прямо сейчас в чате с персоной (веб: открыт чат
     этой персоны и вкладка видима; Telegram: последняя активность < 3 мин);
   — glance: короткий взгляд на зрителя, когда тот вернулся после ≥10 мин;
   — «пока тебя не было»: разница с последним увиденным снимком (localStorage
     на персону) — смена занятия/места, новые вещи, новые события.
   Комната присутствие НЕ отправляет (это делает только чат, presence.ts). */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import type { RefObject } from 'react';
import { getFocusedPersona, onFocusedPersonaChange } from '../presence';
import type { RoomSource } from './roomTypes';

const AWAY_MIN_MS = 10 * 60_000;
const TELEGRAM_ACTIVE_MS = 3 * 60_000;
const GLANCE_MS = 4000;

// Снимок того, что пользователь видел в комнате
export interface PresenceSnapshot {
  pastime: string;
  place: string;
  items: string[];
  eventIds: number[];
}

interface StoredSnapshot extends PresenceSnapshot {
  at: number;
}

export interface AwaySummary {
  minutes: number;
  pastime?: { from: string; to: string };
  place?: { from: string; to: string };
  newItems: string[];
  newEvents: number;
}

const snapKey = (persona: string) => `vpc-room-last-seen:${persona}`;

function readSnapshot(persona: string): StoredSnapshot | null {
  try {
    const raw = localStorage.getItem(snapKey(persona));
    return raw ? (JSON.parse(raw) as StoredSnapshot) : null;
  } catch {
    return null;
  }
}

function writeSnapshot(persona: string, snap: PresenceSnapshot) {
  try {
    localStorage.setItem(snapKey(persona), JSON.stringify({ ...snap, at: Date.now() }));
  } catch {
    // Квота/приватный режим — сводка просто не покажется
  }
}

function diffSnapshots(prev: StoredSnapshot, cur: PresenceSnapshot, now: number): AwaySummary | null {
  const lastSeenEvent = prev.eventIds.length ? Math.max(...prev.eventIds) : -Infinity;
  const newEvents = cur.eventIds.filter((id) => id > lastSeenEvent).length;
  const newItems = cur.items.filter((n) => !prev.items.includes(n));
  const pastime = prev.pastime && cur.pastime && prev.pastime !== cur.pastime ? { from: prev.pastime, to: cur.pastime } : undefined;
  const place = prev.place && cur.place && prev.place !== cur.place ? { from: prev.place, to: cur.place } : undefined;
  if (!pastime && !place && !newItems.length && !newEvents) return null;
  return {
    minutes: Math.max(1, Math.round((now - prev.at) / 60_000)),
    ...(pastime ? { pastime } : {}),
    ...(place ? { place } : {}),
    newItems,
    newEvents,
  };
}

export interface PresenceCues {
  cue: 'with_you' | 'glance' | null;
  away: AwaySummary | null;
  dismissAway: () => void;
}

export function usePresenceCues(args: {
  personaId: string;
  source: RoomSource | null; // null — веб/демо
  snapshot: PresenceSnapshot | null; // null — данные ещё не готовы
  rootRef?: RefObject<Element | null>; // узел в документе-владельце (PiP)
  ownerDocument?: Document | null;
  enabled?: boolean;
}): PresenceCues {
  const { personaId, source, snapshot, rootRef, ownerDocument, enabled = true } = args;

  // ── with_you: веб-чат этой персоны открыт и вкладка видима ──
  const webFocused = useSyncExternalStore(
    useCallback((cb: () => void) => {
      const off = onFocusedPersonaChange(cb);
      // Чат живёт в главном документе приложения — его видимость и важна
      const main = typeof document !== 'undefined' ? document : null;
      main?.addEventListener('visibilitychange', cb);
      return () => {
        off();
        main?.removeEventListener('visibilitychange', cb);
      };
    }, []),
    () => getFocusedPersona() === personaId && typeof document !== 'undefined' && !document.hidden,
  );

  // ── with_you: Telegram-чат активен последние 3 минуты (разовый таймер на истечение) ──
  const [tgTick, setTgTick] = useState(0);
  const lastActivity = source?.kind === 'telegram' ? source.last_activity : null;
  const tgActive = useMemo(
    () => lastActivity != null && Date.now() - lastActivity * 1000 < TELEGRAM_ACTIVE_MS,
    // tgTick — пересчёт по таймеру истечения
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [lastActivity, tgTick],
  );
  useEffect(() => {
    if (!tgActive || lastActivity == null) return;
    const left = lastActivity * 1000 + TELEGRAM_ACTIVE_MS - Date.now();
    const timer = setTimeout(() => setTgTick((n) => n + 1), Math.max(1000, left + 500));
    return () => clearTimeout(timer);
  }, [tgActive, lastActivity]);

  // ── glance и сводка «пока тебя не было» ──
  const [glance, setGlance] = useState(false);
  const [away, setAway] = useState<AwaySummary | null>(null);
  const baseline = useRef<StoredSnapshot | null>(null);
  const snapRef = useRef(snapshot);
  useEffect(() => {
    snapRef.current = snapshot;
  });
  const snapJson = snapshot ? JSON.stringify(snapshot) : '';

  useEffect(() => {
    if (!glance) return;
    const timer = setTimeout(() => setGlance(false), GLANCE_MS);
    return () => clearTimeout(timer);
  }, [glance]);

  // Смена персоны — сводка и база сравнения сбрасываются
  useEffect(() => {
    baseline.current = null;
    setAway(null);
  }, [personaId]);

  // Снимок готов/обновился: первый раз — сравнение с сохранённым (если ушли
  // ≥10 мин назад), дальше — пересчёт сводки против базы и запись снимка
  const primed = useRef<string | null>(null);
  useEffect(() => {
    if (!enabled || !snapshot) return;
    const doc = ownerDocument ?? rootRef?.current?.ownerDocument ?? null;
    if (primed.current !== personaId) {
      primed.current = personaId;
      const prev = readSnapshot(personaId);
      if (prev && Date.now() - prev.at >= AWAY_MIN_MS) {
        baseline.current = prev;
        setGlance(true);
      }
    }
    if (baseline.current) {
      const summary = diffSnapshots(baseline.current, snapshot, Date.now());
      if (summary) setAway(summary);
    }
    if (!doc?.hidden) writeSnapshot(personaId, snapshot);
    // snapJson — содержательная зависимость вместо объекта snapshot
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, personaId, snapJson, ownerDocument]);

  // Уход из раздела / закрытие PiP / смена персоны — тоже «ушёл»: метка
  // времени снимка — момент ухода, а не последнего изменения (иначе после
  // получаса в комнате и минуты в чате сработали бы взгляд и сводка)
  useEffect(() => {
    if (!enabled) return;
    const doc = ownerDocument ?? rootRef?.current?.ownerDocument ?? (typeof document !== 'undefined' ? document : null);
    return () => {
      if (snapRef.current && !doc?.hidden) writeSnapshot(personaId, snapRef.current);
    };
  }, [enabled, personaId, ownerDocument, rootRef]);

  // Видимость документа-владельца: уход — запоминаем время и снимок;
  // возврат через ≥10 мин — взгляд и база для сводки
  useEffect(() => {
    if (!enabled) return;
    const doc = ownerDocument ?? rootRef?.current?.ownerDocument ?? (typeof document !== 'undefined' ? document : null);
    if (!doc) return;
    let hiddenAt = doc.hidden ? Date.now() : 0;
    const onVis = () => {
      const snap = snapRef.current;
      if (doc.hidden) {
        hiddenAt = Date.now();
        if (snap) writeSnapshot(personaId, snap);
        return;
      }
      if (hiddenAt && Date.now() - hiddenAt >= AWAY_MIN_MS) {
        const prev = readSnapshot(personaId);
        if (prev) {
          baseline.current = prev;
          setGlance(true);
          if (snap) {
            const summary = diffSnapshots(prev, snap, Date.now());
            if (summary) setAway(summary);
          }
        }
      }
      hiddenAt = 0;
    };
    doc.addEventListener('visibilitychange', onVis);
    return () => doc.removeEventListener('visibilitychange', onVis);
  }, [enabled, personaId, ownerDocument, rootRef]);

  const dismissAway = useCallback(() => {
    baseline.current = null;
    setAway(null);
  }, []);

  const cue: PresenceCues['cue'] = glance ? 'glance' : webFocused || tgActive ? 'with_you' : null;
  return { cue, away, dismissAway };
}

// ── Записка на столе: последнее непрочитанное событие ──

const noteKey = (persona: string) => `vpc-room-note-read:${persona}`;
const noteCache = new Map<string, number>();
const noteListeners = new Set<() => void>();

function readNoteId(persona: string): number {
  const cached = noteCache.get(persona);
  if (cached != null) return cached;
  let id = 0;
  try {
    id = Number(localStorage.getItem(noteKey(persona)) ?? 0) || 0;
  } catch {
    id = 0;
  }
  noteCache.set(persona, id);
  return id;
}

function subscribeNote(cb: () => void) {
  noteListeners.add(cb);
  return () => {
    noteListeners.delete(cb);
  };
}

export interface NoteEvent {
  id: number;
  text: string;
  time: string;
  dim?: boolean; // уже рассказано пользователю (consumed)
}

export function useUnreadNote(personaId: string, events: NoteEvent[]) {
  const readId = useSyncExternalStore(subscribeNote, () => readNoteId(personaId));
  const note = useMemo(() => {
    let best: NoteEvent | null = null;
    for (const e of events) {
      if (e.dim || e.id <= readId || !e.text) continue;
      if (!best || e.id > best.id) best = e;
    }
    return best;
  }, [events, readId]);
  const markRead = useCallback(
    (id: number) => {
      if (id <= readNoteId(personaId)) return;
      noteCache.set(personaId, id);
      try {
        localStorage.setItem(noteKey(personaId), String(id));
      } catch {
        // без localStorage записка просто снова появится после перезагрузки
      }
      noteListeners.forEach((l) => l());
    },
    [personaId],
  );
  return { note, markRead };
}

import { useEffect, useSyncExternalStore } from 'react';
import { ApiError, api } from '../api';
import { MOOD_POLL_MS, moodEmotion, useEmotionHold } from './emotionHold';
import type { EmotionEvent } from './emotionHold';
import type { PersonaMood, PortraitData } from './portraitTypes';

/* Эмоции живого лица: персона выбрала выражение маркером [EMO:имя] в ответе
   (app/api/portrait_api) — ядро шлёт его в ленту реплик (/api/said, kind
   emo, текст — id эмоции) перед самой репликой. Опрос — один на вкладку и
   только пока открыт живой портрет с эмоциями (голосовой режим, PiP): пока
   ленту так слушают, ядро и подсказывает персоне её эмоции.

   Сколько держится — emotionHold.ts (там же, что и у портрета на рабочем
   столе, desktop/src/portrait).

   В остальное время лицо — по настроению персоны: у эмоций с условием «по
   настроению» настроение спрашивается раз в MOOD_POLL_MS (один опрос на
   персону, пока такой портрет на виду). */

export type { EmotionEvent };

const WAIT_SEC = 25;
const RETRY_MS = 5000;

const last = new Map<string, EmotionEvent>();
const listeners = new Set<() => void>();
const wanted = new Map<string, number>(); // персона → сколько портретов ждут
let seq = 0;
let loop: AbortController | null = null;

function emit() {
  listeners.forEach((l) => l());
}

function start() {
  if (loop) return;
  const abort = new AbortController();
  loop = abort;
  void (async () => {
    let since: number | null = null;
    let epoch = '';
    while (!abort.signal.aborted) {
      try {
        const r = await api.waitSaid(since, epoch, WAIT_SEC, abort.signal, true);
        if (abort.signal.aborted) return;
        // Первый ответ — только номер: прошлые эмоции уже не к месту
        if (since != null) {
          let changed = false;
          for (const m of r.messages) {
            if (m.kind !== 'emo' || !wanted.has(m.persona)) continue;
            last.set(m.persona, { id: m.text.trim(), seq: ++seq });
            changed = true;
          }
          if (changed) emit();
        }
        since = r.cursor;
        epoch = r.epoch;
      } catch (e) {
        if (abort.signal.aborted || (e instanceof ApiError && e.status === 404)) return;
        await new Promise((res) => setTimeout(res, RETRY_MS));
      }
    }
  })();
}

function want(persona: string, on: boolean) {
  const n = (wanted.get(persona) ?? 0) + (on ? 1 : -1);
  if (n > 0) wanted.set(persona, n);
  else wanted.delete(persona);
  if (wanted.size) start();
  else {
    loop?.abort();
    loop = null;
  }
}

/** Последняя эмоция персоны из ленты; enabled=false — не слушать. */
export function useEmotionFeed(personaId: string, enabled: boolean): EmotionEvent | null {
  useEffect(() => {
    if (!enabled) return;
    want(personaId, true);
    return () => want(personaId, false);
  }, [personaId, enabled]);
  const ev = useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => last.get(personaId) ?? null,
  );
  return enabled ? ev : null;
}

// Настроение персон: персона → последнее (null — живого состояния нет)
const moods = new Map<string, PersonaMood | null>();
const moodWanted = new Map<string, number>();
const moodTimers = new Map<string, number>();

async function fetchMood(persona: string) {
  if (document.visibilityState === 'hidden' && moods.has(persona)) return;
  try {
    const { mood } = await api.getPortraitMood(persona);
    if (!moodWanted.has(persona) || JSON.stringify(mood) === JSON.stringify(moods.get(persona))) return;
    moods.set(persona, mood);
    emit();
  } catch {
    // Ядро не ответило — лицо остаётся каким было, спросим в следующий раз
  }
}

function wantMood(persona: string, on: boolean) {
  const n = (moodWanted.get(persona) ?? 0) + (on ? 1 : -1);
  if (n > 0) {
    moodWanted.set(persona, n);
    if (!moodTimers.has(persona)) {
      void fetchMood(persona);
      moodTimers.set(persona, window.setInterval(() => void fetchMood(persona), MOOD_POLL_MS));
    }
    return;
  }
  moodWanted.delete(persona);
  window.clearInterval(moodTimers.get(persona));
  moodTimers.delete(persona);
}

/** Настроение персоны сейчас; enabled=false — не спрашивать. */
export function usePersonaMood(personaId: string, enabled: boolean): PersonaMood | null {
  useEffect(() => {
    if (!enabled) return;
    wantMood(personaId, true);
    return () => wantMood(personaId, false);
  }, [personaId, enabled]);
  const mood = useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => moods.get(personaId) ?? null,
  );
  return enabled ? mood : null;
}

/** Есть ли у портрета эмоции, которые можно показать (со своим основным листом). */
export const hasEmotions = (data: PortraitData | null | undefined) => !!data?.emotions?.some((e) => e.sheets.A);

/** …и такие, что встают на лицо по настроению. */
export const hasMoodEmotions = (data: PortraitData | null | undefined) =>
  !!data?.emotions?.some((e) => e.mood && e.sheets.A);

/** Эмоция живого портрета персоны: к реплике (лента + удержание), иначе по настроению. */
export function useLiveEmotion(personaId: string, data: PortraitData | null | undefined, speaking: boolean): string | null {
  const ev = useEmotionFeed(personaId, hasEmotions(data));
  const held = useEmotionHold(ev, speaking);
  const mood = usePersonaMood(personaId, hasMoodEmotions(data));
  return held ?? moodEmotion(data, mood);
}

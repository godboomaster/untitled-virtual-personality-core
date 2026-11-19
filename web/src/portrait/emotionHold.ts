import { useEffect, useRef, useState } from 'react';
import type { EmotionMood, PersonaMood, PortraitData, PortraitEmotion } from './portraitTypes';

/* Эмоция на живом лице: какую показывать и сколько держать. Без сети —
   модуль берут и LivePortrait, и портрет на рабочем столе
   (desktop/src/portrait); событие эмоции веб получает из ленты реплик
   (portraitEmotion.ts), панель — своей лентой.

   Эмоция к реплике (маркер [EMO:имя]) держится, пока персона говорит, и ещё
   AFTER_MS после; не начала говорить — не меньше MIN_MS. В остальное время
   лицо — по настроению персоны (moodEmotion): эмоция, чьё условие «по
   настроению» подходит; никакая — обычное. */

export interface EmotionEvent {
  id: string;
  seq: number;
}

export const EMOTION_MIN_MS = 5000;
export const EMOTION_AFTER_MS = 4000;

/** Эмоция на лице сейчас: с события и пока говорит (+AFTER_MS), не меньше
 *  MIN_MS. События до появления портрета не показываются. */
export function useEmotionHold(ev: EmotionEvent | null, speaking: boolean): string | null {
  const [shown, setShown] = useState<string | null>(null);
  const seen = useRef(ev?.seq ?? 0);
  const from = useRef(0);
  useEffect(() => {
    if (!ev || ev.seq === seen.current) return;
    seen.current = ev.seq;
    from.current = Date.now();
    setShown(ev.id);
  }, [ev]);
  useEffect(() => {
    if (!shown || speaking) return;
    const wait = Math.max(EMOTION_AFTER_MS, from.current + EMOTION_MIN_MS - Date.now());
    const timer = window.setTimeout(() => setShown(null), wait);
    return () => window.clearTimeout(timer);
  }, [shown, speaking]);
  return shown;
}

/** Набор листов эмоции, если он есть и с основным листом. */
export function emotionOf(data: PortraitData, id: string | null | undefined): PortraitEmotion | null {
  if (!id) return null;
  const e = data.emotions?.find((x) => x.id === id);
  return e?.sheets.A ? e : null;
}

// Настроение персоны (GET …/portrait/mood) — раз в столько, пока лицо на виду
export const MOOD_POLL_MS = 45_000;
// Зоны настроения: valence −1…1, arousal 0…1
export const MOOD_GOOD = 0.3;
export const MOOD_BAD = -0.3;
export const MOOD_CALM = 0.4;
export const MOOD_EXCITED = 0.55;

const words = (s: string) =>
  s
    .toLowerCase()
    .replace(/ё/g, 'е')
    .split(/[^\p{L}\p{N}*]+/u)
    .filter((w) => w && w !== '*');

// Слово условия и слово тега — одно: «груст*» — начало слова; иначе равны или
// с общим началом без окончаний («грусть» ~ «грустный», «радость» ~ «радостно»)
function sameWord(want: string, got: string): boolean {
  if (want.endsWith('*')) return got.startsWith(want.slice(0, -1));
  if (want === got) return true;
  const need = Math.max(4, Math.min(want.length, got.length) - 2);
  let k = 0;
  while (k < want.length && want[k] === got[k]) k++;
  return k >= need;
}

/** Фраза условия целыми словами в теге настроения. */
export function inMoodTag(tag: string, phrase: string): boolean {
  const have = words(tag);
  const want = words(phrase);
  if (!want.length) return false;
  for (let i = 0; i + want.length <= have.length; i++) {
    if (want.every((w, k) => sameWord(w, have[i + k]))) return true;
  }
  return false;
}

/** Насколько условие эмоции подходит к настроению: 0 — нет; слово (имя
 *  эмоции или слово условия) в теге — 3; зона — по числу заданных осей. */
export function moodScore(cond: EmotionMood, name: string, mood: PersonaMood): number {
  if ([name, ...(cond.words ?? [])].some((w) => inMoodTag(mood.tag, w))) return 3;
  const axes = (cond.valence ? 1 : 0) + (cond.arousal ? 1 : 0);
  if (!axes) return 0;
  const v = !cond.valence || (cond.valence === 'good' ? mood.valence > MOOD_GOOD : mood.valence < MOOD_BAD);
  const a = !cond.arousal || (cond.arousal === 'excited' ? mood.arousal > MOOD_EXCITED : mood.arousal < MOOD_CALM);
  return v && a ? axes : 0;
}

/** Эмоция лица по настроению: лучшая из подходящих (со своим основным
 *  листом), при равных — первая по списку; никакая — null. */
export function moodEmotion(data: PortraitData | null | undefined, mood: PersonaMood | null | undefined): string | null {
  if (!mood || !data?.emotions) return null;
  let best: string | null = null;
  let top = 0;
  for (const e of data.emotions) {
    if (!e.mood || !e.sheets.A) continue;
    const score = moodScore(e.mood, e.name, mood);
    if (score > top) {
      best = e.id;
      top = score;
    }
  }
  return best;
}

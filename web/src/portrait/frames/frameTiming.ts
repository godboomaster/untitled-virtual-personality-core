import type { FrameTiming } from './frameTypes';

/* Время кадров анимации (app/api/portrait_anim.py): fps — шаг всей анимации,
   holds — сколько шагов стоит каждый кадр. На полоске всё в мс, поэтому
   длительность кадра здесь переводится в шаги и обратно. Кадр при сдвиге
   упирается в соседей: порядок кадров полоска не меняет. */

const HOLD_MIN = 0.05; // как в portrait_anim.py
const HOLD_MAX = 300;
const FRAME_MS_MIN = 20; // короче кадр глазом не увидеть

export const stepOf = (fps: number) => 1000 / Math.max(1, Math.min(30, fps));

export const frameMs = (t: FrameTiming, i: number) => stepOf(t.fps) * (t.holds?.[i] ?? 1);

export function frameLimits(fps: number): { min: number; max: number } {
  const step = stepOf(fps);
  return { min: Math.max(FRAME_MS_MIN, step * HOLD_MIN), max: step * HOLD_MAX };
}

/** Начало каждого из n кадров и последним — конец анимации, мс */
export function frameStarts(t: FrameTiming, n: number): number[] {
  const out = [0];
  for (let i = 0; i < n; i++) out.push(out[i] + frameMs(t, i));
  return out;
}

function withHolds(t: FrameTiming, n: number, ms: Record<number, number>): FrameTiming {
  const step = stepOf(t.fps);
  const holds = Array.from({ length: Math.max(n, t.holds?.length ?? 0) }, (_, i) => t.holds?.[i] ?? 1);
  for (const [i, v] of Object.entries(ms)) holds[Number(i)] = Math.round((v / step) * 1000) / 1000;
  return { ...t, holds: holds.every((v) => v === 1) ? null : holds };
}

/** Кадр i идёт ms: следующие сдвигаются, длина анимации меняется */
export function withFrameMs(t: FrameTiming, n: number, i: number, ms: number): FrameTiming {
  const { min, max } = frameLimits(t.fps);
  return withHolds(t, n, { [i]: Math.max(min, Math.min(max, ms)) });
}

/** Кадр i начинается в at: следующий кадр остаётся на месте, длина та же */
export function withFrameStart(t: FrameTiming, n: number, i: number, at: number): FrameTiming {
  if (i <= 0 || i >= n) return t;
  const { min, max } = frameLimits(t.fps);
  const s = frameStarts(t, n);
  const lo = Math.max(s[i - 1] + min, s[i + 1] - max);
  const hi = Math.min(s[i + 1] - min, s[i - 1] + max);
  const x = Math.max(lo, Math.min(hi, at));
  return withHolds(t, n, { [i - 1]: x - s[i - 1], [i]: s[i + 1] - x });
}

import { animMask, type AnimMask } from './animMask';
import { animGrid, type PortraitAnim } from './animTypes';

/* Фигура идёт внутри кадров: шаги нарисованы так, что на каждом кадре она
   стоит дальше, чем на прежнем. По кругу после последнего кадра снова
   первый, и фигура прыгала назад, к месту первого кадра. Здесь — где фигура
   на каждом кадре и сколько она проходит за кадр, чтобы следующий круг шёл
   с места последнего кадра:
   - на рабочем столе фигура стоит в окне на месте, а окно идёт её шагами:
     сменился кадр — окно на шаг (AnimSprite travel steady, portrait_gait);
   - в превью и в редакторе кадров фигура уходит дальше, круг за кругом.
   Шаг от последнего кадра к первому следующего круга нигде не нарисован —
   берётся средний шаг круга. */

/** Доля ширины кадра: прошла меньше за круг — стоит на месте (покачивается) */
export const TRAVEL_MIN = 0.06;
const SOLID = 40; // как в animMask

export interface AnimTravel {
  /** Середина фигуры в кадре i — px кадра от середины кадра (+ вправо) */
  at: number[];
  /** Средняя середина: на столе фигура стоит в окне здесь */
  mid: number;
  /** Шаг от кадра i к следующему; последний — к первому кадру следующего круга */
  steps: number[];
  /** Проходит за круг, px кадра, со знаком */
  cycle: number;
}

/** Шаги по серединам фигуры (null — кадр пуст: стоит, где была); frameW — ширина кадра */
export function travelOf(at: readonly (number | null)[], frameW: number): AnimTravel | null {
  const n = at.length;
  const first = at.findIndex((v) => v != null);
  if (n < 2 || first < 0) return null;
  const filled: number[] = [];
  let last = at[first] as number;
  for (const v of at) {
    if (v != null) last = v;
    filled.push(last);
  }
  const span = filled[n - 1] - filled[0];
  if (!(Math.abs(span) >= TRAVEL_MIN * frameW)) return null;
  const wrap = span / (n - 1);
  const steps = filled.map((v, i) => (i < n - 1 ? filled[i + 1] - v : wrap));
  return { at: filled, mid: filled.reduce((s, v) => s + v, 0) / n, steps, cycle: span + wrap };
}

/** Середины фигуры по маске альфы (кадры по порядку чтения), px кадра fw от середины */
export function centersOf(m: AnimMask, frames: number, fw: number): (number | null)[] {
  const out: (number | null)[] = [];
  const rowW = m.cols * m.cw;
  for (let i = 0; i < frames; i++) {
    const c = i % m.cols;
    const r = Math.floor(i / m.cols);
    if (r >= m.rows) {
      out.push(null);
      continue;
    }
    let mass = 0;
    let sx = 0;
    for (let y = 0; y < m.ch; y++) {
      const row = (r * m.ch + y) * rowW + c * m.cw;
      for (let x = 0; x < m.cw; x++) {
        const a = m.alpha[row + x];
        if (a < SOLID) continue;
        mass += a;
        sx += (x + 0.5) * a;
      }
    }
    out.push(mass ? (sx / mass / m.cw - 0.5) * fw : null);
  }
  return out;
}

const cache = new WeakMap<AnimMask, Map<number, AnimTravel | null>>();

/** Шаги фигуры в кадрах анимации — по её атласу (маска из кэша animMask) */
export async function animTravel(anim: PortraitAnim): Promise<AnimTravel | null> {
  const m = await animMask(anim);
  if (!m) return null;
  const grid = animGrid(anim);
  const frames = Math.max(1, Math.min(anim.frames, grid.rows * grid.cols));
  let byFrames = cache.get(m);
  if (!byFrames) {
    byFrames = new Map();
    cache.set(m, byFrames);
  }
  if (!byFrames.has(frames)) byFrames.set(frames, travelOf(centersOf(m, frames, anim.fw), anim.fw));
  return byFrames.get(frames) ?? null;
}

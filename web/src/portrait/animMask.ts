import { animGrid, type PortraitAnim } from './animTypes';

/* Где у кадра анимации фигура, а где прозрачно — по альфе атласа,
   уменьшенного до LONG px по длинной стороне кадра: портрет на рабочем
   столе пропускает клик мимо фигуры окну под ним и пока играет анимация
   (desktop/src/portrait/). Маска строится один раз на версию анимации. */

const LONG = 64;
const SOLID = 40;

export interface AnimMask {
  rows: number;
  cols: number;
  cw: number;
  ch: number;
  alpha: Uint8Array;
}

const cache = new Map<string, Promise<AnimMask | null>>();

async function build(anim: PortraitAnim): Promise<AnimMask | null> {
  if (!anim.atlas) return null;
  const img = new Image();
  img.src = anim.atlas;
  await img.decode();
  const k = LONG / Math.max(anim.fw, anim.fh, 1);
  const cw = Math.max(1, Math.round(anim.fw * k));
  const ch = Math.max(1, Math.round(anim.fh * k));
  const canvas = document.createElement('canvas');
  const grid = animGrid(anim); // атлас клиентов: кадров бывает больше, чем клеток листа
  canvas.width = grid.cols * cw;
  canvas.height = grid.rows * ch;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) return null;
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  const px = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  const alpha = new Uint8Array(canvas.width * canvas.height);
  for (let i = 0; i < alpha.length; i++) alpha[i] = px[i * 4 + 3];
  return { rows: grid.rows, cols: grid.cols, cw, ch, alpha };
}

/** Маска анимации (из кэша по id и времени правки) */
export function animMask(anim: PortraitAnim): Promise<AnimMask | null> {
  const key = `${anim.id}:${anim.updated_at ?? ''}`;
  let m = cache.get(key);
  if (!m) {
    m = build(anim).catch(() => null);
    cache.set(key, m);
    // Старые версии этой анимации больше не нужны
    for (const k of cache.keys()) if (k !== key && k.startsWith(`${anim.id}:`)) cache.delete(k);
  }
  return m;
}

/** Точка (u, v) — доли кадра frame от его левого верхнего угла — на фигуре? */
export function animHit(m: AnimMask, frame: number, u: number, v: number): boolean {
  if (!(u >= 0 && u < 1 && v >= 0 && v < 1)) return false;
  const c = frame % m.cols;
  const r = Math.floor(frame / m.cols);
  if (r >= m.rows) return false;
  const x = Math.floor((c + u) * m.cw);
  const y = Math.floor((r + v) * m.ch);
  return m.alpha[y * m.cols * m.cw + x] >= SOLID;
}

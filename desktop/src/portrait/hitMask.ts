import type { PortraitData } from '../../../web/src/portrait/portraitTypes';

/* Где у портрета фигура, а где прозрачно — по альфе основного листа (A),
   уменьшенного до CELL px на клетку: этого хватает, чтобы клик мимо головы
   ушёл окну под портретом. Остальные листы — те же ракурсы с другими глазами
   и ртом, силуэт у них тот же. */

const CELL = 96;
// Альфа, с которой пиксель — уже фигура: края волос полупрозрачные
const SOLID = 40;

export interface HitMask {
  rows: number;
  cols: number;
  alpha: Uint8Array;
}

export async function buildMask(data: PortraitData): Promise<HitMask | null> {
  const src = data.sheets.A;
  if (!src) return null;
  const img = new Image();
  img.src = src;
  await img.decode();
  const { rows, cols } = data;
  const canvas = document.createElement('canvas');
  canvas.width = cols * CELL;
  canvas.height = rows * CELL;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) return null;
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  const px = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  const alpha = new Uint8Array(canvas.width * canvas.height);
  for (let i = 0; i < alpha.length; i++) alpha[i] = px[i * 4 + 3];
  return { rows, cols, alpha };
}

/** Точка (u, v) — доли стороны клетки (r, c) от её левого верхнего угла — на фигуре? */
export function maskHit(m: HitMask, r: number, c: number, u: number, v: number): boolean {
  if (!(u >= 0 && u < 1 && v >= 0 && v < 1) || r < 0 || r >= m.rows || c < 0 || c >= m.cols) return false;
  const x = Math.floor((c + u) * CELL);
  const y = Math.floor((r + v) * CELL);
  return m.alpha[y * m.cols * CELL + x] >= SOLID;
}

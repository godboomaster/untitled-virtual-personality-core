import { useEffect, useRef } from 'react';
import type { PortraitData } from './portraitTypes';

/* Где у портрета фигура, а где прозрачно — по альфе основного листа (A),
   уменьшенного до CELL px на клетку: этого хватает, чтобы клик мимо головы
   ушёл окну под портретом (рабочий стол) и не считался поглаживанием
   (голосовой режим, PiP). Остальные листы — те же ракурсы с другими глазами
   и ртом, силуэт у них тот же. */

const CELL = 96;
// Альфа, с которой пиксель — уже фигура: края волос полупрозрачные
const SOLID = 40;

export interface HitMask {
  rows: number;
  cols: number;
  alpha: Uint8Array;
  /** Макушка: самая высокая строка фигуры по всем ракурсам — доля стороны
      клетки от её верха (над ней в кадре прозрачно; облачко портрета на
      рабочем столе встаёт над ней, а не над верхом кадра) */
  top: number;
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
  return { rows, cols, alpha, top: crown(alpha, rows, cols) };
}

// Самая высокая строка фигуры в клетках листа, доля стороны клетки
function crown(alpha: Uint8Array, rows: number, cols: number): number {
  const width = cols * CELL;
  let best = CELL;
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      for (let y = 0; y < best; y++) {
        const row = (r * CELL + y) * width + c * CELL;
        let solid = false;
        for (let x = 0; x < CELL && !solid; x++) solid = alpha[row + x] >= SOLID;
        if (solid) {
          best = y;
          break;
        }
      }
    }
  }
  return best >= CELL ? 0 : best / CELL;
}

/** Точка (u, v) — доли стороны клетки (r, c) от её левого верхнего угла — на фигуре? */
export function maskHit(m: HitMask, r: number, c: number, u: number, v: number): boolean {
  if (!(u >= 0 && u < 1 && v >= 0 && v < 1) || r < 0 || r >= m.rows || c < 0 || c >= m.cols) return false;
  const x = Math.floor((c + u) * CELL);
  const y = Math.floor((r + v) * CELL);
  return m.alpha[y * m.cols * CELL + x] >= SOLID;
}

/** Точка (x, y) экрана — на фигуре? body — .live-portrait-body, клетка — та, что сейчас на экране (onCell). */
export function hitOnBody(m: HitMask | null, cell: { r: number; c: number }, body: Element | null | undefined, x: number, y: number): boolean {
  if (!m || !body) return false;
  const r = body.getBoundingClientRect();
  return maskHit(m, cell.r, cell.c, (x - r.left) / r.width, (y - r.top) / r.height);
}

/** Маска портрета: строится заново, когда меняются атласы. */
export function useHitMask(data: PortraitData | null) {
  const mask = useRef<HitMask | null>(null);
  useEffect(() => {
    mask.current = null;
    if (!data) return;
    let alive = true;
    buildMask(data)
      .then((m) => {
        if (alive) mask.current = m;
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [data]);
  return mask;
}

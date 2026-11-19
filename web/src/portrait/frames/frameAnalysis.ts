import { cellForTotal, mapPoint, totalAdjust } from './frameMath';
import type { FrameAdjust, FrameAlign, FrameFix } from './frameTypes';

/* Что видно по кадрам без ядра: где стоит фигура и насколько кадр совпадает
   с другим. Кадр сводится к плоскости яркости (Plane): прозрачное — 0,
   фигура — 96 и выше, поэтому несовпавший контур стоит дороже, чем разница
   внутри фигуры. Подгонка «по рисунку» ищет лучший сдвиг и масштаб сверху
   вниз по пирамиде: грубо на 1/8 размера, потом уточнение на каждом шаге.
   Повёрнутый кадр меряется по краю фигуры (edge): у повёрнутой фигуры
   крайние точки — среди крайних точек её строк. */

export interface FrameInfo {
  empty: boolean;
  x0: number; // рамка фигуры в кадре
  y0: number;
  x1: number;
  y1: number;
  cx: number; // центр масс
  cy: number;
  edge: Float32Array; // углы крайних пикселей каждой строки: x, y, x, y…
}

interface Plane {
  w: number;
  h: number;
  v: Uint8Array;
}

export interface FrameData {
  fw: number;
  fh: number;
  info: FrameInfo[];
  planes: Plane[]; // кадры в полный размер
  pyramid: Map<number, Plane[]>; // уменьшенные копии — по мере надобности
}

const SOLID = 16; // альфа, с которой пиксель — уже фигура (как на ядре)
const LEVELS = [8, 4, 2, 1];
const SEARCH = 10; // на 1/8 размера: ±80 px кадра
const SCALE_STEPS = [0.92, 0.94, 0.96, 0.98, 1, 1.02, 1.04, 1.06, 1.08];

export function analyzeAtlas(img: CanvasImageSource, rows: number, cols: number, fw: number, fh: number): FrameData {
  const canvas = document.createElement('canvas');
  canvas.width = cols * fw;
  canvas.height = rows * fh;
  const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
  ctx.drawImage(img, 0, 0);
  const px = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  const info: FrameInfo[] = [];
  const planes: Plane[] = [];
  for (let i = 0; i < rows * cols; i++) {
    const ox = (i % cols) * fw;
    const oy = Math.floor(i / cols) * fh;
    const v = new Uint8Array(fw * fh);
    let x0 = fw;
    let y0 = fh;
    let x1 = 0;
    let y1 = 0;
    let mass = 0;
    let sx = 0;
    let sy = 0;
    const edge: number[] = [];
    for (let y = 0; y < fh; y++) {
      let at = ((oy + y) * canvas.width + ox) * 4;
      let left = -1;
      let right = -1;
      for (let x = 0; x < fw; x++, at += 4) {
        const a = px[at + 3];
        if (!a) continue;
        const luma = (px[at] * 77 + px[at + 1] * 151 + px[at + 2] * 28) >> 8;
        v[y * fw + x] = ((96 + (luma * 159) / 255) * a) / 255;
        if (a > SOLID) {
          if (x < x0) x0 = x;
          if (x >= x1) x1 = x + 1;
          if (y < y0) y0 = y;
          if (y >= y1) y1 = y + 1;
          if (left < 0) left = x;
          right = x + 1;
          mass += a;
          sx += x * a;
          sy += y * a;
        }
      }
      if (left >= 0) edge.push(left, y, left, y + 1, right, y, right, y + 1);
    }
    info.push({
      empty: !mass,
      x0,
      y0,
      x1,
      y1,
      cx: mass ? sx / mass : fw / 2,
      cy: mass ? sy / mass : fh / 2,
      edge: new Float32Array(edge),
    });
    planes.push({ w: fw, h: fh, v });
  }
  return { fw, fh, info, planes, pyramid: new Map([[1, planes]]) };
}

const slottedCache = new WeakMap<FrameData, Map<string, FrameData>>();

/** Кадры анимации в её порядке: кадр j — клетка order[j] атласа нарезки.
 * Те же плоскости и сведения, только по кадрам (пирамида — по мере надобности) */
export function slotted(data: FrameData, order: readonly number[] | null): FrameData {
  if (!order) return data;
  const key = order.join(',');
  let byOrder = slottedCache.get(data);
  if (!byOrder) {
    byOrder = new Map();
    slottedCache.set(data, byOrder);
  }
  const have = byOrder.get(key);
  if (have) return have;
  const pick = <T,>(list: T[]) => order.map((k) => list[k]).filter((v): v is T => v != null);
  const planes = pick(data.planes);
  const made: FrameData = { fw: data.fw, fh: data.fh, info: pick(data.info), planes, pyramid: new Map([[1, planes]]) };
  byOrder.set(key, made);
  return made;
}

function shrink(p: Plane): Plane {
  const w = Math.max(1, p.w >> 1);
  const h = Math.max(1, p.h >> 1);
  const v = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const a = y * 2 * p.w + x * 2;
      const b = (y * 2 + 1) * p.w + x * 2;
      v[y * w + x] = (p.v[a] + p.v[a + 1] + p.v[b] + p.v[b + 1]) >> 2;
    }
  }
  return { w, h, v };
}

function planesAt(data: FrameData, level: number): Plane[] {
  const have = data.pyramid.get(level);
  if (have) return have;
  const made = planesAt(data, level / 2).map(shrink);
  data.pyramid.set(level, made);
  return made;
}

/** Кадр с правкой в плоскости w×h, совмещённой с кадром по низу-середине */
function render(src: Plane, a: FrameAdjust, level: number, w: number, h: number): Plane {
  const v = new Uint8Array(w * h);
  const k = 1 / a.s;
  const dx = a.dx / level;
  const dy = a.dy / level;
  if (a.r) {
    // Обратно: сдвиг, масштаб, поворот назад вокруг центра
    const t = (a.r * Math.PI) / 180;
    const c = Math.cos(t);
    const s = Math.sin(t);
    const px = (a.px ?? 0) / level;
    const py = (a.py ?? 0) / level;
    for (let y = 0; y < h; y++) {
      const uy = (y - h - dy) * k - py;
      for (let x = 0; x < w; x++) {
        const ux = (x - w / 2 - dx) * k - px;
        const sx = Math.floor(src.w / 2 + c * ux + s * uy + px);
        const sy = Math.floor(src.h - s * ux + c * uy + py);
        if (sx >= 0 && sx < src.w && sy >= 0 && sy < src.h) v[y * w + x] = src.v[sy * src.w + sx];
      }
    }
    return { w, h, v };
  }
  for (let y = 0; y < h; y++) {
    const sy = Math.floor(src.h + (y - h - dy) * k);
    if (sy < 0 || sy >= src.h) continue;
    for (let x = 0; x < w; x++) {
      const sx = Math.floor(src.w / 2 + (x - w / 2 - dx) * k);
      if (sx >= 0 && sx < src.w) v[y * w + x] = src.v[sy * src.w + sx];
    }
  }
  return { w, h, v };
}

/** Края фигуры кадра с правкой: px кадра от низа-середины */
function bounds(f: FrameInfo, fw: number, fh: number, a: FrameAdjust): { left: number; right: number; top: number; bottom: number } {
  if (!a.r) {
    const [left, top] = mapPoint(a, f.x0 - fw / 2, f.y0 - fh);
    const [right, bottom] = mapPoint(a, f.x1 - fw / 2, f.y1 - fh);
    return { left, right, top, bottom };
  }
  let left = Infinity;
  let right = -Infinity;
  let top = Infinity;
  let bottom = -Infinity;
  for (let i = 0; i < f.edge.length; i += 2) {
    const [x, y] = mapPoint(a, f.edge[i] - fw / 2, f.edge[i + 1] - fh);
    if (x < left) left = x;
    if (x > right) right = x;
    if (y < top) top = y;
    if (y > bottom) bottom = y;
  }
  return { left, right, top, bottom };
}

function cost(a: Plane, b: Plane, dx: number, dy: number, limit: number): number {
  let sum = 0;
  const { w, h } = a;
  for (let y = 0; y < h; y++) {
    const by = y - dy;
    const inRow = by >= 0 && by < h;
    for (let x = 0; x < w; x++) {
      const bx = x - dx;
      const other = inRow && bx >= 0 && bx < w ? b.v[by * w + bx] : 0;
      const d = a.v[y * w + x] - other;
      sum += d < 0 ? -d : d;
    }
    if (sum >= limit) return sum;
  }
  return sum;
}

/** Насколько кадры различаются, % площади фигур (на 1/4 размера: дёшево). */
export function frameDiff(
  a: FrameData,
  ai: number,
  aAdj: FrameAdjust,
  b: FrameData,
  bi: number,
  bAdj: FrameAdjust,
): number {
  const level = 4;
  const pa = planesAt(a, level)[ai];
  const pb = planesAt(b, level)[bi];
  if (!pa || !pb) return 0;
  const ra = render(pa, aAdj, level, pa.w, pa.h);
  const rb = render(pb, bAdj, level, pa.w, pa.h);
  let sum = 0;
  let area = 0;
  for (let i = 0; i < ra.v.length; i++) {
    const x = ra.v[i];
    const y = rb.v[i];
    if (!x && !y) continue;
    area++;
    sum += Math.abs(x - y);
  }
  return area ? (sum / (area * 255)) * 100 : 0;
}

/** Кадр с правками: фигурам тесно — растёт вверх и вбок до limit, низ стоит
 * полом (portrait_api.grown_frame). cut — что-то всё равно не влезает. */
/** Рамка кадра атласа: ширина, высота, sink — пол на столько выше низа,
 * ax — середина пола от левого края. Своя рамка (fix.box) — она и есть,
 * over — фигура за неё выходит (обрежется); иначе подбирается (autoBox) */
export function frameBox(
  data: FrameData,
  fix: FrameFix,
  limit: number,
): { w: number; h: number; sink: number; ax: number; floor: number; cut: boolean; own: boolean; over: boolean } {
  const box = fix.box;
  // Низ экрана (fix.floor): пол кадра — на столько выше низа кадра нарезки
  const floor = fix.floor ?? 0;
  if (!box) return { ...autoBox(data, fix, limit), floor, own: false, over: false };
  let { l, r, t, b } = box;
  // Как grown_frame ядра: больше предела — ужать, доли те же
  if (l + r > limit) {
    l = Math.max(1, Math.round((l * limit) / (l + r)));
    r = Math.max(1, limit - l);
  }
  if (t + b > limit) {
    b = Math.round((b * limit) / (t + b));
    t = Math.max(1, limit - b);
  }
  const { fw, fh } = data;
  let over = false;
  data.info.forEach((f, i) => {
    if (over || f.empty) return;
    const e = bounds(f, fw, fh, totalAdjust(fix, i));
    if (e.left < -l - 1 || e.right > r + 1 || e.top + floor < -t - 1 || e.bottom + floor > b + 1) over = true;
  });
  return { w: l + r, h: t + b, sink: b, ax: l, floor, cut: false, own: true, over };
}

/** Рамка, в которую влезут фигуры с правками (grown_frame ядра): растёт
 * вверх и вбок поровну, под пол — вниз на sink */
export function autoBox(data: FrameData, fix: FrameFix, limit: number): { w: number; h: number; sink: number; ax: number; cut: boolean } {
  const { fw, fh } = data;
  // Низ экрана поднят на lift: кадр нарезки тот же, пол в нём выше
  // (grown_frame ядра) — над полом fh − lift, под полом lift
  const lift = fix.floor ?? 0;
  let half = fw / 2;
  let top = Math.max(1, fh - lift);
  let low = lift;
  data.info.forEach((f, i) => {
    if (f.empty) return;
    const b = bounds(f, fw, fh, totalAdjust(fix, i));
    half = Math.max(half, -b.left, b.right);
    top = Math.max(top, -(b.top + lift));
    low = Math.max(low, b.bottom + lift);
  });
  const w = Math.min(limit, fw + 2 * Math.ceil(half - fw / 2));
  const up = Math.min(limit, Math.ceil(top));
  // Под полом — кадр растёт вниз на sink, пол остаётся (grown_frame ядра);
  // пиксель-другой — сглаживание края, не повод
  const below = low > 1 ? Math.ceil(low) : 0;
  const sink = Math.min(below, limit - up);
  return { w, h: up + sink, sink, ax: w / 2, cut: fw + 2 * Math.ceil(half - fw / 2) > limit || Math.ceil(top) > limit || below > sink };
}

/** Где нижний край и центр масс кадра с учётом правки */
export function placed(data: FrameData, cell: number, a: FrameAdjust): { bottom: number; center: number; top: number } | null {
  const f = data.info[cell];
  if (!f || f.empty) return null;
  const { fw, fh } = data;
  const b = bounds(f, fw, fh, a);
  return { bottom: b.bottom, top: b.top, center: mapPoint(a, f.cx - fw / 2, f.cy - fh)[0] };
}

/** Середина фигуры кадра в px кадра от низа-середины, до правки: центр поворота
 * по умолчанию (по рамке фигуры — так её и видно); пустой кадр — null */
export function figureCenter(data: FrameData, cell: number): [number, number] | null {
  const f = data.info[cell];
  if (!f || f.empty) return null;
  return [Math.round((f.x0 + f.x1) / 2 - data.fw / 2), Math.round((f.y0 + f.y1) / 2 - data.fh)];
}

function byImage(target: FrameData, cell: number, ref: FrameData, refCell: number, refAdj: FrameAdjust, fit: boolean, cur: FrameAdjust): FrameAdjust {
  const startS = cur.s;
  let best: FrameAdjust = { ...cur, dx: 0, dy: 0 };
  let done = false;
  for (const level of LEVELS) {
    const src = planesAt(target, level)[cell];
    const base = planesAt(ref, level)[refCell];
    if (!src || !base) continue;
    const a = render(base, refAdj, level, src.w, src.h);
    const coarse = !done;
    const radius = coarse ? SEARCH : 2;
    const ks = !fit
      ? [startS]
      : coarse
        ? SCALE_STEPS.map((k) => k * startS)
        : [best.s * 0.995, best.s, best.s * 1.005];
    const cx = coarse ? 0 : Math.round(best.dx / level);
    const cy = coarse ? 0 : Math.round(best.dy / level);
    let bestCost = Infinity;
    let next = best;
    for (const k of ks) {
      // Поворот кадра остаётся каким был: ищем только сдвиг и масштаб
      const b = render(src, { ...cur, dx: 0, dy: 0, s: k }, level, src.w, src.h);
      for (let dy = cy - radius; dy <= cy + radius; dy++) {
        for (let dx = cx - radius; dx <= cx + radius; dx++) {
          const c = cost(a, b, dx, dy, bestCost);
          if (c < bestCost) {
            bestCost = c;
            next = { ...cur, dx: dx * level, dy: dy * level, s: k };
          }
        }
      }
    }
    best = next;
    done = true;
  }
  return best;
}

/** Какая правка кадра нужна, чтобы он совпал с опорным; null — нечего совмещать. */
export function alignCell(
  how: FrameAlign,
  target: FrameData,
  fix: FrameFix,
  cell: number,
  ref: FrameData,
  refCell: number,
  refAdj: FrameAdjust,
  fitScale: boolean,
): FrameAdjust | null {
  const cur = totalAdjust(fix, cell);
  const a = placed(target, cell, cur);
  const b = placed(ref, refCell, refAdj);
  if (!a || !b) return null;
  if (how === 'bottom') return cellForTotal(fix, { ...cur, dy: cur.dy + Math.round(b.bottom - a.bottom) });
  if (how === 'center') return cellForTotal(fix, { ...cur, dx: cur.dx + Math.round(b.center - a.center) });
  return cellForTotal(fix, byImage(target, cell, ref, refCell, refAdj, fitScale, cur));
}

import type { FrameAdjust, FrameArea, FrameCut, FrameFix, FrameLayer, FrameScope, FrameTiming, ScreenEdge } from './frameTypes';

/* Чистые расчёты редактора кадров. Формула правки — та же, что у ядра
   (portrait_api.compose, turn_points): поворот вокруг центра кадра (px, py
   от низа-середины), масштаб вокруг низа-середины кадра, потом сдвиг. */

export const NO_ADJUST: FrameAdjust = { dx: 0, dy: 0, s: 1 };
export const SHIFT_MAX = 512;
export const SCALE_MIN = 0.25;
export const SCALE_MAX = 4;
export const PIVOT_MAX = 1024; // как FIX_PIVOT_MAX на ядре
const LINE_GAP = 8; // как LINE_GAP_MIN на ядре

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
const roundS = (s: number) => Math.round(clamp(s, SCALE_MIN, SCALE_MAX) * 1000) / 1000;
const round1 = (v: number) => Math.round(v * 10) / 10 || 0; // без «-0»

/** Угол в −180..180, до десятых, как на ядре */
export function normDeg(r: number): number {
  return round1(((((r + 180) % 360) + 360) % 360) - 180);
}

export function isPlain(a: FrameAdjust | undefined): boolean {
  return !a || (a.dx === 0 && a.dy === 0 && a.s === 1 && !a.r && !a.px && !a.py);
}

/** Итог для кадра: сдвиги складываются, масштабы перемножаются, поворот — свой у кадра */
export function totalAdjust(fix: FrameFix, cell: number): Required<FrameAdjust> {
  const a = fix.all ?? NO_ADJUST;
  const c = fix.cells?.[cell] ?? NO_ADJUST;
  return { dx: a.dx + c.dx, dy: a.dy + c.dy, s: a.s * c.s, r: c.r ?? 0, px: c.px ?? 0, py: c.py ?? 0 };
}

export function cellAdjust(fix: FrameFix, cell: number): FrameAdjust {
  return fix.cells?.[cell] ?? NO_ADJUST;
}

/** Точка кадра от низа-середины (px кадра нарезки) → куда её ставит правка */
export function mapPoint(a: FrameAdjust, x: number, y: number): [number, number] {
  const r = a.r ?? 0;
  if (!r) return [x * a.s + a.dx, y * a.s + a.dy];
  const px = a.px ?? 0;
  const py = a.py ?? 0;
  const t = (r * Math.PI) / 180;
  const c = Math.cos(t);
  const s = Math.sin(t);
  const ux = x - px;
  const uy = y - py;
  return [(c * ux - s * uy + px) * a.s + a.dx, (s * ux + c * uy + py) * a.s + a.dy];
}

/** Где центр поворота кадра на холсте: px кадра от низа-середины */
export function pivotAt(a: FrameAdjust): [number, number] {
  return [(a.px ?? 0) * a.s + a.dx, (a.py ?? 0) * a.s + a.dy];
}

/** Правка в пределах и в том же виде, что у ядра (clean_fix): поля по порядку,
 * поворот и центр — только если есть; у листа целиком (whole) их нет */
function tidy(a: FrameAdjust, whole = false): FrameAdjust {
  const out: FrameAdjust = { dx: clamp(Math.round(a.dx), -SHIFT_MAX, SHIFT_MAX), dy: clamp(Math.round(a.dy), -SHIFT_MAX, SHIFT_MAX), s: roundS(a.s) };
  if (whole) return out;
  const r = normDeg(a.r ?? 0);
  const px = round1(clamp(a.px ?? 0, -PIVOT_MAX, PIVOT_MAX));
  const py = round1(clamp(a.py ?? 0, -PIVOT_MAX, PIVOT_MAX));
  if (r) out.r = r;
  if (px || py) {
    out.px = px;
    out.py = py;
  }
  return out;
}

export function withCell(fix: FrameFix, cell: number, next: FrameAdjust): FrameFix {
  const cells = { ...fix.cells };
  const clean = tidy(next);
  if (isPlain(clean)) delete cells[cell];
  else cells[cell] = clean;
  const out: FrameFix = {};
  if (fix.all) out.all = fix.all;
  if (Object.keys(cells).length) out.cells = cells;
  if (fix.box) out.box = fix.box;
  if (fix.floor) out.floor = fix.floor;
  if (fix.edge) out.edge = fix.edge;
  return out;
}

export function withAll(fix: FrameFix, next: FrameAdjust): FrameFix {
  const clean = tidy(next, true);
  const out: FrameFix = {};
  if (!isPlain(clean)) out.all = clean;
  if (fix.cells) out.cells = fix.cells;
  if (fix.box) out.box = fix.box;
  if (fix.floor) out.floor = fix.floor;
  if (fix.edge) out.edge = fix.edge;
  return out;
}

/** Своя рамка кадра (null — подбирать самой); правки кадров те же */
export function withBox(fix: FrameFix, box: FrameArea | null): FrameFix {
  const out: FrameFix = {};
  if (fix.all) out.all = fix.all;
  if (fix.cells) out.cells = fix.cells;
  if (box) out.box = box;
  if (fix.floor) out.floor = fix.floor;
  if (fix.edge) out.edge = fix.edge;
  return out;
}

export const FLOOR_MAX = 1024; // как BOX_MAX на ядре

/** Край экрана анимации: низ — линия на at px кадра нарезки выше низа
 * фигуры (0 — по низу, fix.floor), другой — на at px от этого края кадра
 * (fix.edge). Одно из двух: подглядывает сбоку — низ по фигуре */
export function withScreenEdge(fix: FrameFix, side: ScreenEdge, at: number): FrameFix {
  const out: FrameFix = {};
  if (fix.all) out.all = fix.all;
  if (fix.cells) out.cells = fix.cells;
  if (fix.box) out.box = fix.box;
  const v = Math.round(clamp(at, 0, FLOOR_MAX));
  if (side === 'bottom') {
    if (v) out.floor = v;
  } else out.edge = { side, at: v };
  return out;
}

/** Какой край и линия (см. withScreenEdge) */
export function screenEdgeOf(fix: FrameFix): { side: ScreenEdge; at: number } {
  return fix.edge ? { side: fix.edge.side, at: fix.edge.at } : { side: 'bottom', at: fix.floor ?? 0 };
}

/** Линия края на холсте (начало — низ-середина кадра нарезки fw×fh): у
 * низа и верха — y, у боков — x */
export function edgeLinePos(side: ScreenEdge, at: number, fw: number, fh: number): number {
  return side === 'bottom' ? -at : side === 'top' ? -fh + at : side === 'left' ? -fw / 2 + at : fw / 2 - at;
}

/** Обратно: линия на холсте в pos → at (не меньше 0) */
export function edgeLineAt(side: ScreenEdge, pos: number, fw: number, fh: number): number {
  const at = side === 'bottom' ? -pos : side === 'top' ? pos + fh : side === 'left' ? pos + fw / 2 : fw / 2 - pos;
  return Math.max(0, Math.round(at));
}

/** Правка кадра, чтобы вместе с правкой листа вышло total */
export function cellForTotal(fix: FrameFix, total: FrameAdjust): FrameAdjust {
  const a = fix.all ?? NO_ADJUST;
  return { dx: total.dx - a.dx, dy: total.dy - a.dy, s: total.s / a.s, r: total.r, px: total.px, py: total.py };
}

/** Кадры, которые правим: этот, его ряд, его столбец или все; total — сколько кадров */
export function scopeCells(scope: FrameScope, cell: number, total: number, cols: number): number[] {
  const out: number[] = [];
  for (let i = 0; i < total; i++) {
    if (
      scope === 'sheet' ||
      (scope === 'frame' && i === cell) ||
      (scope === 'row' && Math.floor(i / cols) === Math.floor(cell / cols)) ||
      (scope === 'col' && i % cols === cell % cols)
    )
      out.push(i);
  }
  return out;
}

/** Добавить сдвиг и масштаб ко всему, что в scope; лист — общей правкой */
export function nudge(
  fix: FrameFix,
  scope: FrameScope,
  cell: number,
  total: number,
  cols: number,
  ddx: number,
  ddy: number,
  ds = 1,
): FrameFix {
  const add = (a: FrameAdjust): FrameAdjust => ({ ...a, dx: a.dx + ddx, dy: a.dy + ddy, s: a.s * ds });
  if (scope === 'sheet') return withAll(fix, add(fix.all ?? NO_ADJUST));
  return scopeCells(scope, cell, total, cols).reduce((f, i) => withCell(f, i, add(cellAdjust(f, i))), fix);
}

/** Повернуть кадры scope на dr градусов. Поворот у каждого кадра свой, и «Лист»
 * здесь — все кадры по одному. Кадр без центра поворота получает center(i) —
 * обычно середину своей фигуры, а не низ кадра */
export function turn(
  fix: FrameFix,
  scope: FrameScope,
  cell: number,
  total: number,
  cols: number,
  dr: number,
  center: (i: number) => [number, number] | null,
  step = 0, // шаг угла (Shift — 15°): итог кратен ему
): FrameFix {
  return scopeCells(scope, cell, total, cols).reduce((f, i) => {
    const a = cellAdjust(f, i);
    const has = a.px != null || a.py != null;
    const c = has ? null : center(i);
    const r = (a.r ?? 0) + dr;
    return withCell(f, i, { ...a, r: step ? Math.round(r / step) * step : r, ...(c ? { px: c[0], py: c[1] } : {}) });
  }, fix);
}

/** Центр поворота кадров scope — в точку (x, y) холста, px кадра от низа-середины.
 * Центр едет с масштабом и сдвигом кадра, поэтому у каждого кадра — свой */
export function placePivot(fix: FrameFix, scope: FrameScope, cell: number, total: number, cols: number, x: number, y: number): FrameFix {
  return scopeCells(scope, cell, total, cols).reduce((f, i) => {
    const t = totalAdjust(f, i);
    return withCell(f, i, { ...cellAdjust(f, i), px: (x - t.dx) / t.s, py: (y - t.dy) / t.s });
  }, fix);
}

/** Кадр по кругу: листаем дальше последнего — снова первый */
export function wrapCell(cell: number, count: number): number {
  const n = Math.max(1, count);
  return ((cell % n) + n) % n;
}

/** Кадр слоя; лист перерезали и кадров стало меньше — последний */
export function layerCell(layer: Pick<FrameLayer, 'cell'>, count: number): number {
  return Math.min(layer.cell, Math.max(0, count - 1));
}

// ── Порядок кадров анимации ──────────────────────────────────────────

/** Клетка листа, что идёт кадром j */
export const cutOf = (order: readonly number[] | null, j: number) => (order ? (order[j] ?? j) : j);

export interface Reordered {
  order: number[] | null;
  fix: FrameFix;
  holds: number[] | null;
}

/** Кадры в новом порядке: perm — какой прежний кадр теперь на месте n
 * (кого нет — удалён). Правка и время кадра едут с ним; cells — клеток в листе */
export function permute(order: number[] | null, fix: FrameFix, holds: number[] | null, perm: number[], cells: number): Reordered {
  const next = perm.map((j) => cutOf(order, j));
  const same = next.length === cells && next.every((v, i) => v === i);
  const moved: Record<string, FrameAdjust> = {};
  perm.forEach((j, n) => {
    const a = fix.cells?.[j];
    if (a) moved[n] = a;
  });
  const out: FrameFix = {};
  if (fix.all) out.all = fix.all;
  if (Object.keys(moved).length) out.cells = moved;
  if (fix.box) out.box = fix.box;
  if (fix.floor) out.floor = fix.floor;
  if (fix.edge) out.edge = fix.edge;
  const h = holds ? perm.map((j) => holds[j] ?? 1) : null;
  return { order: same ? null : next, fix: out, holds: h && h.some((v) => v !== 1) ? h : null };
}

export const SLOTS_MAX = 144; // кадров в анимации, как MAX_SLOTS на ядре

/** Клетку листа cut — новым кадром на место at (кадры с at сдвигаются).
 * Клетка уже есть в анимации — новый кадр берёт правку и время у первого
 * такого: выглядит и идёт так же, как он */
export function insertSlot(order: number[] | null, fix: FrameFix, holds: number[] | null, cells: number, cut: number, at: number): Reordered {
  const cur = order ?? Array.from({ length: cells }, (_, i) => i);
  const twin = cur.indexOf(cut);
  const next = [...cur.slice(0, at), cut, ...cur.slice(at)];
  const moved: Record<string, FrameAdjust> = {};
  for (const [k, a] of Object.entries(fix.cells ?? {})) moved[Number(k) >= at ? Number(k) + 1 : Number(k)] = a;
  const same = twin >= 0 ? fix.cells?.[twin] : undefined;
  if (same) moved[at] = same;
  const out: FrameFix = {};
  if (fix.all) out.all = fix.all;
  if (Object.keys(moved).length) out.cells = moved;
  if (fix.box) out.box = fix.box;
  if (fix.floor) out.floor = fix.floor;
  if (fix.edge) out.edge = fix.edge;
  const base = Array.from({ length: cur.length }, (_, i) => holds?.[i] ?? 1);
  const h = [...base.slice(0, at), twin >= 0 ? base[twin] : 1, ...base.slice(at)];
  const plain = next.length === cells && next.every((v, i) => v === i);
  return { order: plain ? null : next, fix: out, holds: h.some((v) => v !== 1) ? h : null };
}

/** Кадр from — перед кадром to (to = n — в конец); null — на месте и так */
export function movePerm(n: number, from: number, to: number): number[] | null {
  if (to === from || to === from + 1) return null;
  const perm = Array.from({ length: n }, (_, i) => i);
  perm.splice(from, 1);
  perm.splice(to > from ? to - 1 : to, 0, from);
  return perm;
}

/** Без кадра j */
export function dropPerm(n: number, j: number): number[] {
  return Array.from({ length: n }, (_, i) => i).filter((i) => i !== j);
}

/** Время с другим числом играемых кадров */
export const withFrames = (t: FrameTiming | null, frames: number): FrameTiming | null => (t ? { ...t, frames } : t);

// ── Линии сетки ──────────────────────────────────────────────────────

export function evenLines(from: number, to: number, n: number): number[] {
  return Array.from({ length: n + 1 }, (_, i) => Math.round(from + ((to - from) * i) / n));
}

/** Сохранить доли: поля сменились — внутренние линии едут вместе с ними */
export function stretchLines(lines: number[], from: number, to: number): number[] {
  const a = lines[0];
  const span = lines[lines.length - 1] - a || 1;
  return lines.map((v, i) => (i === 0 ? from : i === lines.length - 1 ? to : Math.round(from + ((v - a) * (to - from)) / span)));
}

/** Подвинуть линию i, не заходя за соседние и край листа */
export function moveLine(lines: number[], i: number, value: number, size: number): number[] {
  const lo = i > 0 ? lines[i - 1] + LINE_GAP : 0;
  const hi = i < lines.length - 1 ? lines[i + 1] - LINE_GAP : size;
  const next = [...lines];
  next[i] = clamp(Math.round(value), lo, Math.max(lo, hi));
  return next;
}

/** Другая сетка: те же внешние края, линии ровные */
export function regrid(cut: FrameCut, rows: number, cols: number, w: number, h: number): FrameCut {
  const xs = cut.xs ?? evenLines(cut.offset_x, w - cut.offset_x, cut.cols);
  const ys = cut.ys ?? evenLines(cut.offset_y, h - cut.offset_y, cut.rows);
  return {
    ...cut,
    rows,
    cols,
    xs: cols === cut.cols ? xs : evenLines(xs[0], xs[xs.length - 1], cols),
    ys: rows === cut.rows ? ys : evenLines(ys[0], ys[ys.length - 1], rows),
  };
}

/** Правка без кадров: номера другой сетки не подходят (так же и на ядре);
 * своя рамка — в px старого кадра нарезки, тоже долой */
export function wholeOnly(fix: FrameFix): FrameFix {
  return fix.all ? { all: fix.all } : {};
}

export function signed(v: number): string {
  return v > 0 ? `+${v}` : `${v}`.replace('-', '−');
}

import type { FrameAdjust, FrameArea, FrameBg, FrameBlend, ScreenEdge } from './frameTypes';

/* Рисование холста редактора кадров. Единицы слоёв — px кадра, начало —
   низ-середина кадра: и ядро ставит фигуры по низу, поэтому кадры разных
   размеров совмещаются сами. */

export interface StageItem {
  img: CanvasImageSource;
  sx: number; // кадр в атласе
  sy: number;
  fw: number;
  fh: number;
  boxW: number; // кадр атласа с правками: что снаружи, срежет и ядро
  boxH: number;
  boxSink?: number; // из них под полом (низ-серединой слоёв): фигура ушла ниже — кадр растёт вниз
  boxAx?: number; // середина пола от левого края рамки; нет — посередине (своя рамка бывает несимметричной)
  boxFloor?: number; // пол рамки на столько выше низа кадра нарезки (низ экрана анимации, fix.floor)
  adjust: FrameAdjust;
  opacity: number;
  tint: string | null; // свой цвет слоя
  moves?: boolean; // идёт по кругу дальше (шаги при проигрывании): сдвиг OverlayScene.carry
}

export interface StageView {
  z: number;
  x: number;
  y: number;
  h: number; // высота кадра при выборе вида: кадр растёт, а вид стоит
}

export interface Guides {
  on: boolean;
  x: number; // от середины кадра
  y: number; // от низа кадра (вверх — минус)
}

export interface OverlayScene {
  w: number;
  h: number;
  dpr: number;
  view: StageView;
  items: StageItem[];
  blend: FrameBlend;
  bg: FrameBg;
  flick: number;
  guides: Guides;
  /** px кадра вправо для слоёв moves — уже в пределах холста (carryShift) */
  carry?: number;
  /** Центр поворота выбранного кадра (px кадра от низа-середины) — метка; null — не показывать */
  pivot?: [number, number] | null;
  /** Своя рамка выбранной анимации — контур с ручками на краях (тянуть); hot — край под рукой */
  area?: FrameArea | null;
  areaHot?: AreaEdge | null;
  /** Край экрана выбранной анимации: линия (её тянут) — у низа и верха на y
      pos, у боков на x pos (px кадра от низа-середины); что за ней — притенено:
      уйдёт за край экрана; null — не показывать */
  edgeLine?: { side: ScreenEdge; pos: number; hot: boolean; label: string } | null;
}

export type AreaEdge = 'l' | 'r' | 't' | 'b';
export const AREA = '#ffb020';
export const AREA_HANDLE = 5; // px экрана: полуразмер ручки края рамки
export const FLOOR = '#1aa6c4';
export const FLOOR_TAB = { w: 92, h: 18 }; // ярлык линии края экрана: у горизонтальной — слева, у вертикальной — сверху

/** Где на холсте линия края экрана и её ярлык (px экрана) */
export function edgeLineScreen(s: Pick<OverlayScene, 'w' | 'h' | 'view'>, side: ScreenEdge, pos: number) {
  const a = overlayAnchor(s);
  const z = s.view.z;
  if (side === 'bottom' || side === 'top') {
    const y = a.y + pos * z;
    // Ярлык — с той стороны, что остаётся на экране
    const ty = side === 'bottom' ? y - FLOOR_TAB.h : y;
    return { horizontal: true, at: y, tab: { x: 0, y: ty, w: FLOOR_TAB.w, h: FLOOR_TAB.h } };
  }
  const x = a.x + pos * z;
  const tx = side === 'left' ? x : x - FLOOR_TAB.w;
  return { horizontal: false, at: x, tab: { x: tx, y: 0, w: FLOOR_TAB.w, h: FLOOR_TAB.h } };
}

export interface SliceScene {
  w: number;
  h: number;
  dpr: number;
  view: StageView;
  sheet: CanvasImageSource | null;
  sheetW: number;
  sheetH: number;
  xs: number[];
  ys: number[];
  cell: number; // клетка листа, что сейчас открыта
  labels?: (string | null)[]; // подпись клетки: номер кадра анимации; null — кадр удалён
  bg: FrameBg;
  grab: { axis: 'x' | 'y'; i: number } | null;
}

export const GUIDE = '#ff2d6f';
const PIVOT = '#18c7ff';
export const PIVOT_R = 7; // px экрана: метка центра поворота
let tile: HTMLCanvasElement | null = null;

function checker(ctx: CanvasRenderingContext2D, w: number, h: number) {
  if (!tile) {
    tile = document.createElement('canvas');
    tile.width = tile.height = 24;
    const t = tile.getContext('2d')!;
    t.fillStyle = '#b4b4b4';
    t.fillRect(0, 0, 24, 24);
    t.fillStyle = '#d2d2d2';
    t.fillRect(0, 0, 12, 12);
    t.fillRect(12, 12, 12, 12);
  }
  ctx.fillStyle = ctx.createPattern(tile, 'repeat')!;
  ctx.fillRect(0, 0, w, h);
}

function paintBg(ctx: CanvasRenderingContext2D, bg: FrameBg, w: number, h: number) {
  if (bg === 'checker') checker(ctx, w, h);
  else {
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, w, h);
  }
}

/** Вписать содержимое boxW×boxH в холст */
export function fitZoom(w: number, h: number, boxW: number, boxH: number): number {
  return Math.max(0.05, Math.min((w - 40) / boxW, (h - 40) / boxH));
}

/** Шаги ушли на carry px кадра: сдвиг слоёв в пределах холста — кадр целиком
 * ушёл за край, входит с другой стороны */
export function carryShift(carry: number, w: number, view: StageView, boxW: number): number {
  if (!carry) return 0;
  const x0 = w / 2 + view.x; // где начало координат слоёв без сдвига
  const box = boxW * view.z;
  const period = w + box;
  const x = x0 + carry * view.z + box / 2;
  return ((((x % period) + period) % period) - box / 2 - x0) / view.z;
}

/** Экранная точка начала координат слоёв (низ-середина кадра) */
export function overlayAnchor(s: Pick<OverlayScene, 'w' | 'h' | 'view'>) {
  return { x: s.w / 2 + s.view.x, y: s.h / 2 + s.view.y + (s.view.h * s.view.z) / 2 };
}

export function sliceOrigin(s: Pick<SliceScene, 'w' | 'h' | 'view' | 'sheetW' | 'sheetH'>) {
  return { x: s.w / 2 + s.view.x - (s.sheetW * s.view.z) / 2, y: s.h / 2 + s.view.y - (s.sheetH * s.view.z) / 2 };
}

export function drawOverlay(ctx: CanvasRenderingContext2D, scratch: HTMLCanvasElement, s: OverlayScene) {
  const { w, h, dpr, view, items, blend, bg } = s;
  const { z } = view;
  const anchor = overlayAnchor(s);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  paintBg(ctx, bg, w, h);

  const shown = blend === 'flicker' && items.length ? [items[s.flick % items.length]] : items;
  const sc = scratch.getContext('2d')!;
  const ox = (item: StageItem) => anchor.x + (item.moves ? (s.carry ?? 0) * z : 0);
  for (const item of shown) {
    const { img, sx, sy, fw, fh, boxW, boxH, adjust: a } = item;
    // Низ рамки — от её пола, а он поднят над низом кадра нарезки на boxFloor
    const sink = (item.boxSink ?? 0) - (item.boxFloor ?? 0);
    const left = -(item.boxAx ?? boxW / 2);
    sc.setTransform(1, 0, 0, 1, 0, 0);
    sc.globalCompositeOperation = 'source-over';
    sc.clearRect(0, 0, scratch.width, scratch.height);
    sc.setTransform(dpr * z, 0, 0, dpr * z, dpr * ox(item), dpr * anchor.y);
    // Сильно приближено — видны пиксели: так точнее совмещать
    sc.imageSmoothingEnabled = z < 3;
    sc.save();
    sc.beginPath();
    sc.rect(left, sink - boxH, boxW, boxH);
    sc.clip();
    const put = () => {
      if (!a.r) return sc.drawImage(img, sx, sy, fw, fh, (-fw / 2) * a.s + a.dx, -fh * a.s + a.dy, fw * a.s, fh * a.s);
      // Поворот вокруг центра, который уже на месте после масштаба и сдвига
      const cx = (a.px ?? 0) * a.s + a.dx;
      const cy = (a.py ?? 0) * a.s + a.dy;
      sc.save();
      sc.translate(cx, cy);
      sc.rotate((a.r * Math.PI) / 180);
      sc.translate(-cx, -cy);
      sc.drawImage(img, sx, sy, fw, fh, (-fw / 2) * a.s + a.dx, -fh * a.s + a.dy, fw * a.s, fh * a.s);
      sc.restore();
    };
    put();
    if (item.tint) {
      // Сначала серое, потом цвет: свои цвета кадра не мешают видеть слой
      sc.globalCompositeOperation = 'saturation';
      sc.fillStyle = '#808080';
      sc.fillRect(left, sink - boxH, boxW, boxH);
      sc.globalCompositeOperation = 'multiply';
      sc.fillStyle = item.tint;
      sc.fillRect(left, sink - boxH, boxW, boxH);
      sc.globalCompositeOperation = 'destination-in';
      put();
    }
    sc.restore();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = blend === 'normal' ? item.opacity : 1;
    ctx.drawImage(scratch, 0, 0);
  }

  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  // Рамки кадров: что снаружи, в атлас не попадёт
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = 'rgba(128, 128, 128, 0.9)';
  const seen = new Set<string>();
  for (const item of items) {
    const sink = (item.boxSink ?? 0) - (item.boxFloor ?? 0);
    const ax = item.boxAx ?? item.boxW / 2;
    const k = `${item.boxW}x${item.boxH}x${sink}x${ax}:${item.moves ? 1 : 0}`;
    if (seen.has(k)) continue;
    seen.add(k);
    ctx.strokeRect(
      Math.round(ox(item) - ax * z) + 0.5,
      Math.round(anchor.y - (item.boxH - sink) * z) + 0.5,
      Math.round(item.boxW * z),
      Math.round(item.boxH * z),
    );
  }
  ctx.setLineDash([]);
  if (s.guides.on) {
    const gx = Math.round(anchor.x + s.guides.x * z) + 0.5;
    const gy = Math.round(anchor.y + s.guides.y * z) + 0.5;
    ctx.strokeStyle = GUIDE;
    ctx.beginPath();
    ctx.moveTo(gx, 0);
    ctx.lineTo(gx, h);
    ctx.moveTo(0, gy);
    ctx.lineTo(w, gy);
    ctx.stroke();
  }
  // Пол анимации поднят над низом кадра нарезки (низ экрана): от него и рамка
  const el = s.edgeLine;
  const floorY = anchor.y + (el?.side === 'bottom' ? el.pos : 0) * z;
  if (el) {
    // Что за линией — за краем экрана: притенить
    const g = edgeLineScreen(s, el.side, el.pos);
    const v = Math.round(g.at) + 0.5;
    ctx.fillStyle = 'rgba(16, 32, 40, 0.16)';
    if (el.side === 'bottom') ctx.fillRect(0, v, w, h - v);
    else if (el.side === 'top') ctx.fillRect(0, 0, w, v);
    else if (el.side === 'left') ctx.fillRect(0, 0, v, h);
    else ctx.fillRect(v, 0, w - v, h);
    ctx.strokeStyle = FLOOR;
    ctx.lineWidth = el.hot ? 2.5 : 1.5;
    ctx.beginPath();
    if (g.horizontal) {
      ctx.moveTo(0, v);
      ctx.lineTo(w, v);
    } else {
      ctx.moveTo(v, 0);
      ctx.lineTo(v, h);
    }
    ctx.stroke();
    ctx.lineWidth = 1;
    // Ярлык — за него линию берут всегда, даже на краю рамки
    ctx.fillStyle = FLOOR;
    ctx.fillRect(g.tab.x, g.tab.y, g.tab.w, g.tab.h);
    ctx.fillStyle = '#ffffff';
    ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
    ctx.textBaseline = 'middle';
    ctx.fillText(el.label, g.tab.x + 6, g.tab.y + g.tab.h / 2, g.tab.w - 10);
  }
  if (s.area) {
    // Своя рамка: сплошной контур и ручки посреди краёв — их тянут
    const ar = s.area;
    const l = Math.round(anchor.x - ar.l * z) + 0.5;
    const r = Math.round(anchor.x + ar.r * z) + 0.5;
    const t = Math.round(floorY - ar.t * z) + 0.5;
    const b = Math.round(floorY + ar.b * z) + 0.5;
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = AREA;
    ctx.strokeRect(l, t, r - l, b - t);
    // Пол — тонкой линией, если рамка уходит под него
    if (ar.b > 0) {
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(l, Math.round(floorY) + 0.5);
      ctx.lineTo(r, Math.round(floorY) + 0.5);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    const handle = (x: number, y: number, edge: AreaEdge) => {
      const hs = AREA_HANDLE + (s.areaHot === edge ? 2 : 0);
      ctx.fillStyle = s.areaHot === edge ? AREA : '#ffffff';
      ctx.fillRect(x - hs, y - hs, hs * 2, hs * 2);
      ctx.strokeRect(x - hs, y - hs, hs * 2, hs * 2);
    };
    handle(l, (t + b) / 2, 'l');
    handle(r, (t + b) / 2, 'r');
    handle((l + r) / 2, t, 't');
    handle((l + r) / 2, b, 'b');
    ctx.lineWidth = 1;
  }
  if (s.pivot) {
    // Центр поворота: кольцо с крестом, с тёмной обводкой — виден на любом фоне
    const px = Math.round(anchor.x + s.pivot[0] * z) + 0.5;
    const py = Math.round(anchor.y + s.pivot[1] * z) + 0.5;
    const mark = () => {
      ctx.beginPath();
      ctx.arc(px, py, PIVOT_R, 0, Math.PI * 2);
      ctx.moveTo(px - PIVOT_R - 4, py);
      ctx.lineTo(px + PIVOT_R + 4, py);
      ctx.moveTo(px, py - PIVOT_R - 4);
      ctx.lineTo(px, py + PIVOT_R + 4);
      ctx.stroke();
    };
    ctx.lineWidth = 3;
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.75)';
    mark();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = PIVOT;
    mark();
    ctx.lineWidth = 1;
  }
}

export function drawSlice(ctx: CanvasRenderingContext2D, s: SliceScene) {
  const { w, h, dpr, view, xs, ys, cell, bg } = s;
  const { z } = view;
  const o = sliceOrigin(s);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  paintBg(ctx, bg, w, h);
  if (s.sheet) {
    ctx.imageSmoothingEnabled = z < 3;
    ctx.drawImage(s.sheet, o.x, o.y, s.sheetW * z, s.sheetH * z);
  }
  const cols = xs.length - 1;
  const rows = ys.length - 1;
  const sx = (v: number) => Math.round(o.x + v * z) + 0.5;
  const sy = (v: number) => Math.round(o.y + v * z) + 0.5;
  // Текущий кадр — заливка, остальное не закрывает лист
  const c = cell % cols;
  const r = Math.floor(cell / cols);
  if (r < rows) {
    ctx.fillStyle = 'rgba(255, 45, 111, 0.22)';
    ctx.fillRect(sx(xs[c]), sy(ys[r]), sx(xs[c + 1]) - sx(xs[c]), sy(ys[r + 1]) - sy(ys[r]));
  }
  // Удалённые из анимации клетки — притушены
  s.labels?.forEach((label, i) => {
    if (label != null) return;
    const cc = i % cols;
    const rr = Math.floor(i / cols);
    ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';
    ctx.fillRect(sx(xs[cc]), sy(ys[rr]), sx(xs[cc + 1]) - sx(xs[cc]), sy(ys[rr + 1]) - sy(ys[rr]));
  });
  xs.forEach((v, i) => {
    const hot = s.grab?.axis === 'x' && s.grab.i === i;
    ctx.lineWidth = hot || i === 0 || i === cols ? 2 : 1;
    ctx.strokeStyle = GUIDE;
    ctx.beginPath();
    ctx.moveTo(sx(v), sy(ys[0]));
    ctx.lineTo(sx(v), sy(ys[rows]));
    ctx.stroke();
  });
  ys.forEach((v, i) => {
    const hot = s.grab?.axis === 'y' && s.grab.i === i;
    ctx.lineWidth = hot || i === 0 || i === rows ? 2 : 1;
    ctx.strokeStyle = GUIDE;
    ctx.beginPath();
    ctx.moveTo(sx(xs[0]), sy(v));
    ctx.lineTo(sx(xs[cols]), sy(v));
    ctx.stroke();
  });
  // Номера кадров, если клетки не совсем мелкие
  if ((xs[1] - xs[0]) * z > 30) {
    ctx.font = '600 10px ui-monospace, Menlo, monospace';
    ctx.textBaseline = 'top';
    for (let i = 0; i < rows * cols; i++) {
      const tx = sx(xs[i % cols]) + 4;
      const ty = sy(ys[Math.floor(i / cols)]) + 4;
      const own = s.labels && i < s.labels.length ? s.labels[i] : String(i + 1);
      const label = own ?? '×';
      ctx.fillStyle = own == null ? GUIDE : '#000000';
      ctx.fillRect(tx - 2, ty - 2, ctx.measureText(label).width + 4, 14);
      ctx.fillStyle = '#ffffff';
      ctx.fillText(label, tx, ty);
    }
  }
}

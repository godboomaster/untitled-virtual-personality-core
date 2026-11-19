import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { useI18n } from '../../i18n';
import { carryShift, drawOverlay, drawSlice, edgeLineScreen, fitZoom, overlayAnchor, PIVOT_R, sliceOrigin } from './frameDraw';
import type { AreaEdge, Guides, StageItem, StageView } from './frameDraw';
import type { FrameArea, FrameBg, FrameBlend, ScreenEdge } from './frameTypes';

/* Холст редактора кадров. «Наложение» — кадры слоёв друг на друге, тянем
   кадр или направляющие; «Нарезка» — исходный лист с сеткой, тянем линии,
   клик по клетке выбирает кадр. Колесо — приближение к курсору, «Рука» или
   средняя кнопка мыши — двигать вид (у каждого режима свой вид). «Поворот»
   в наложении: тянем по холсту — кадр поворачивается вокруг центра (метка),
   метку можно тащить, щелчок ставит её в эту точку. Своя рамка кадра
   (area) — края тянут за контур или ручки; с Shift — левый и правый
   вместе. Край экрана анимации (edgeLine) — линия поперёк холста: у низа и
   верха — горизонтальная, у боков — вертикальная; тянут её саму или ярлык;
   рамка стоит на линии низа. */

const HIT = 7; // px: насколько близко к линии сетки считается «взял»
const GUIDE_HIT = 4; // направляющие тоньше: они идут прямо через фигуру
const ZOOM_MIN = 0.1;
const ZOOM_MAX = 16;

type Grab =
  | { kind: 'frame'; x: number; y: number; moved: boolean }
  | { kind: 'turn'; x: number; y: number; a0: number; moved: boolean }
  | { kind: 'pivot'; tag: string }
  | { kind: 'edge'; edge: AreaEdge; tag: string }
  | { kind: 'floor'; tag: string; off: number } // off — от курсора до линии, px кадра: линия не прыгает к курсору
  | { kind: 'pan'; x: number; y: number; vx: number; vy: number; moved: boolean; pick: number | null }
  | { kind: 'guide'; axis: 'x' | 'y' }
  | { kind: 'line'; axis: 'x' | 'y'; i: number; tag: string };

interface Props {
  mode: 'overlay' | 'slice';
  hand: boolean;
  bg: FrameBg;
  // Наложение
  items: StageItem[];
  carry?: number; // шаги при проигрывании: слои moves ушли на столько px кадра
  blend: FrameBlend;
  flick: number;
  guides: Guides;
  onGuides: (g: Guides) => void;
  onDrag: (dx: number, dy: number, phase: 'start' | 'move') => void;
  // Поворот: инструмент включён, где центр выбранного кадра (px кадра от
  // низа-середины); deg — на сколько повернули с начала движения
  turn: boolean;
  pivot: [number, number] | null;
  onTurn: (deg: number, phase: 'start' | 'move', snap: boolean) => void;
  onPivot: (x: number, y: number, tag: string) => void;
  // Своя рамка выбранной анимации (px от середины пола): края тянут
  area?: FrameArea | null;
  onArea?: (next: FrameArea, tag: string) => void;
  // Край экрана выбранной анимации: линия на pos (y у низа и верха, x у
  // боков; px кадра от низа-середины); null — не анимация
  edgeLine?: { side: ScreenEdge; pos: number } | null;
  onEdgeLine?: (pos: number, tag: string) => void;
  // Нарезка
  sheet: CanvasImageSource | null;
  sheetW: number;
  sheetH: number;
  xs: number[] | null;
  ys: number[] | null;
  cell: number;
  labels?: (string | null)[];
  onLine: (axis: 'x' | 'y', i: number, value: number, tag: string) => void;
  onPickCell: (i: number) => void;
}

export default function FrameStage(p: Props) {
  const { t } = useI18n();
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const scratch = useRef<HTMLCanvasElement | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [views, setViews] = useState<{ overlay: StageView | null; slice: StageView | null }>({ overlay: null, slice: null });
  const grab = useRef<Grab | null>(null);
  const [hot, setHot] = useState<{ axis: 'x' | 'y'; i: number } | null>(null);
  const [edgeHot, setEdgeHot] = useState<AreaEdge | null>(null);
  const [floorHot, setFloorHot] = useState(false);
  const eline = p.mode === 'overlay' && p.edgeLine ? p.edgeLine : null;
  // Рамка стоит на полу анимации — он поднят линией низа экрана
  const lift = eline?.side === 'bottom' ? -eline.pos : 0;

  const boxW = Math.max(1, ...p.items.map((i) => i.boxW));
  const boxH = Math.max(1, ...p.items.map((i) => i.boxH));
  const contentW = p.mode === 'overlay' ? boxW : Math.max(1, p.sheetW);
  const contentH = p.mode === 'overlay' ? boxH : Math.max(1, p.sheetH);
  const fit = size.w ? fitZoom(size.w, size.h, contentW, contentH) : 1;
  const view: StageView = views[p.mode] ?? { z: fit, x: 0, y: 0, h: boxH };

  const setView = (v: StageView | null) => setViews((cur) => ({ ...cur, [p.mode]: v }));

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Рисуем на каждую смену: правки приходят сюда же, отдельного цикла нет
  useLayoutEffect(() => {
    const c = canvasRef.current;
    if (!c || !size.w || !size.h) return;
    const dpr = window.devicePixelRatio || 1;
    const bw = Math.round(size.w * dpr);
    const bh = Math.round(size.h * dpr);
    if (c.width !== bw || c.height !== bh) {
      c.width = bw;
      c.height = bh;
    }
    const ctx = c.getContext('2d');
    if (!ctx) return;
    if (p.mode === 'overlay') {
      if (!scratch.current) scratch.current = document.createElement('canvas');
      const sc = scratch.current;
      if (sc.width !== bw || sc.height !== bh) {
        sc.width = bw;
        sc.height = bh;
      }
      drawOverlay(ctx, sc, {
        w: size.w,
        h: size.h,
        dpr,
        view,
        items: p.items,
        blend: p.blend,
        bg: p.bg,
        flick: p.flick,
        guides: p.guides,
        pivot: p.turn ? p.pivot : null,
        area: p.area ?? null,
        areaHot: edgeHot,
        edgeLine: eline ? { ...eline, hot: floorHot || grab.current?.kind === 'floor', label: t('frames.edge.line') } : null,
        carry: carryShift(p.carry ?? 0, size.w, view, Math.max(1, ...p.items.filter((i) => i.moves).map((i) => i.boxW))),
      });
    } else if (p.xs && p.ys) {
      drawSlice(ctx, {
        w: size.w,
        h: size.h,
        dpr,
        view,
        sheet: p.sheet,
        sheetW: p.sheetW,
        sheetH: p.sheetH,
        xs: p.xs,
        ys: p.ys,
        cell: p.cell,
        labels: p.labels,
        bg: p.bg,
        grab: hot,
      });
    }
  });

  // Колесо: приближение к курсору (нужен не-пассивный слушатель)
  const zoomAt = (z: number, px: number, py: number) => {
    const next = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z));
    const k = next / view.z;
    const cx = px - size.w / 2;
    const cy = py - size.h / 2;
    setView({ z: next, x: cx - (cx - view.x) * k, y: cy - (cy - view.y) * k, h: view.h });
  };
  const zoomRef = useRef(zoomAt);
  zoomRef.current = zoomAt;
  const viewRef = useRef(view);
  viewRef.current = view;
  useEffect(() => {
    const c = canvasRef.current;
    if (!c) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = c.getBoundingClientRect();
      zoomRef.current(viewRef.current.z * Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
    };
    c.addEventListener('wheel', onWheel, { passive: false });
    return () => c.removeEventListener('wheel', onWheel);
  }, []);

  const local = (e: ReactPointerEvent) => {
    const r = canvasRef.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  const scene = { w: size.w, h: size.h, view, sheetW: p.sheetW, sheetH: p.sheetH };

  // Линия сетки или направляющая под курсором
  const lineAt = (x: number, y: number): { axis: 'x' | 'y'; i: number } | null => {
    if (!p.xs || !p.ys) return null;
    const o = sliceOrigin(scene);
    const z = view.z;
    const top = o.y + p.ys[0] * z - HIT;
    const bottom = o.y + p.ys[p.ys.length - 1] * z + HIT;
    const left = o.x + p.xs[0] * z - HIT;
    const right = o.x + p.xs[p.xs.length - 1] * z + HIT;
    if (x < left || x > right || y < top || y > bottom) return null;
    let best: { axis: 'x' | 'y'; i: number } | null = null;
    let dist = HIT;
    p.xs.forEach((v, i) => {
      const d = Math.abs(o.x + v * z - x);
      if (d < dist) {
        dist = d;
        best = { axis: 'x', i };
      }
    });
    p.ys.forEach((v, i) => {
      const d = Math.abs(o.y + v * z - y);
      if (d < dist) {
        dist = d;
        best = { axis: 'y', i };
      }
    });
    return best;
  };

  const guideAt = (x: number, y: number): 'x' | 'y' | null => {
    if (!p.guides.on) return null;
    const a = overlayAnchor(scene);
    if (Math.abs(a.y + p.guides.y * view.z - y) <= GUIDE_HIT) return 'y';
    if (Math.abs(a.x + p.guides.x * view.z - x) <= GUIDE_HIT) return 'x';
    return null;
  };

  // Центр поворота на экране и угол от него до точки, градусы
  const pivotScreen = (): [number, number] | null => {
    if (!p.pivot) return null;
    const a = overlayAnchor(scene);
    return [a.x + p.pivot[0] * view.z, a.y + p.pivot[1] * view.z];
  };
  const onPivotMark = (x: number, y: number) => {
    const m = pivotScreen();
    return !!m && Math.hypot(x - m[0], y - m[1]) <= PIVOT_R + 4;
  };
  const angleAt = (x: number, y: number) => {
    const m = pivotScreen() ?? [overlayAnchor(scene).x, overlayAnchor(scene).y];
    return (Math.atan2(y - m[1], x - m[0]) * 180) / Math.PI;
  };
  // Край своей рамки под курсором: у контура или на ручке
  const edgeAt = (x: number, y: number): AreaEdge | null => {
    const ar = p.area;
    if (!ar || p.mode !== 'overlay') return null;
    const a = overlayAnchor(scene);
    const z = view.z;
    // Рамка стоит на полу анимации — он поднят на низ экрана
    const fy = a.y - lift * z;
    const l = a.x - ar.l * z;
    const r = a.x + ar.r * z;
    const t = fy - ar.t * z;
    const b = fy + ar.b * z;
    const inY = y >= t - HIT && y <= b + HIT;
    const inX = x >= l - HIT && x <= r + HIT;
    const near: [AreaEdge, number][] = [];
    if (inY) near.push(['l', Math.abs(x - l)], ['r', Math.abs(x - r)]);
    if (inX) near.push(['t', Math.abs(y - t)], ['b', Math.abs(y - b)]);
    const best = near.filter(([, d]) => d <= HIT).sort((m, n) => m[1] - n[1])[0];
    return best ? best[0] : null;
  };

  // Линия края экрана: ярлык (tab) — всегда её, сама линия — если там не
  // край своей рамки
  const floorAt = (x: number, y: number): 'tab' | 'line' | null => {
    if (!eline) return null;
    const g = edgeLineScreen(scene, eline.side, eline.pos);
    if (x >= g.tab.x - 2 && x <= g.tab.x + g.tab.w + 2 && y >= g.tab.y - 2 && y <= g.tab.y + g.tab.h + 2) return 'tab';
    return Math.abs((g.horizontal ? y : x) - g.at) <= HIT ? 'line' : null;
  };

  const toFrame = (x: number, y: number): [number, number] => {
    const a = overlayAnchor(scene);
    return [(x - a.x) / view.z, (y - a.y) / view.z];
  };

  const cellAt = (x: number, y: number): number | null => {
    if (!p.xs || !p.ys) return null;
    const o = sliceOrigin(scene);
    const sx = (x - o.x) / view.z;
    const sy = (y - o.y) / view.z;
    const c = p.xs.findIndex((v, i) => i < p.xs!.length - 1 && sx >= v && sx < p.xs![i + 1]);
    const r = p.ys.findIndex((v, i) => i < p.ys!.length - 1 && sy >= v && sy < p.ys![i + 1]);
    return c < 0 || r < 0 ? null : r * (p.xs.length - 1) + c;
  };

  const down = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    const { x, y } = local(e);
    e.currentTarget.setPointerCapture(e.pointerId);
    const panning = p.hand || e.button === 1;
    if (!panning && p.mode === 'slice') {
      const line = lineAt(x, y);
      if (line) {
        grab.current = { kind: 'line', ...line, tag: `line:${Date.now()}` };
        setHot(line);
        return;
      }
    }
    if (!panning && p.mode === 'overlay') {
      if (p.turn && onPivotMark(x, y)) {
        grab.current = { kind: 'pivot', tag: `pivot:${Date.now()}` };
        return;
      }
      const fl = floorAt(x, y);
      const edge = fl === 'tab' ? null : edgeAt(x, y);
      if (edge) {
        // Рамка меняется под рукой: вид стоит на месте
        if (!views.overlay) setView(view);
        grab.current = { kind: 'edge', edge, tag: `area:${Date.now()}` };
        setEdgeHot(edge);
        return;
      }
      if (fl) {
        if (!views.overlay) setView(view);
        const f = toFrame(x, y);
        grab.current = { kind: 'floor', tag: `edge-line:${Date.now()}`, off: (eline?.pos ?? 0) - (eline && (eline.side === 'left' || eline.side === 'right') ? f[0] : f[1]) };
        setFloorHot(true);
        return;
      }
      const g = guideAt(x, y);
      if (g) {
        grab.current = { kind: 'guide', axis: g };
        return;
      }
      // Кадр может вырасти под рукой: вид на время правки стоит на месте
      if (!views.overlay) setView(view);
      grab.current = p.turn ? { kind: 'turn', x, y, a0: angleAt(x, y), moved: false } : { kind: 'frame', x, y, moved: false };
      return;
    }
    grab.current = {
      kind: 'pan',
      x,
      y,
      vx: view.x,
      vy: view.y,
      moved: false,
      pick: p.mode === 'slice' && !panning ? cellAt(x, y) : null,
    };
  };

  const move = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    const { x, y } = local(e);
    const g = grab.current;
    if (!g) {
      // Какой курсор здесь
      const c = e.currentTarget;
      if (p.hand) c.style.cursor = 'grab';
      else if (p.mode === 'slice') {
        const l = lineAt(x, y);
        c.style.cursor = l ? (l.axis === 'x' ? 'col-resize' : 'row-resize') : 'pointer';
      } else if (p.turn && onPivotMark(x, y)) c.style.cursor = 'move';
      else if (floorAt(x, y) === 'tab' || (!edgeAt(x, y) && floorAt(x, y))) {
        c.style.cursor = eline && (eline.side === 'left' || eline.side === 'right') ? 'ew-resize' : 'ns-resize';
        if (!floorHot) setFloorHot(true);
        if (edgeHot) setEdgeHot(null);
        return;
      } else if (edgeAt(x, y)) {
        const edge = edgeAt(x, y);
        c.style.cursor = edge === 'l' || edge === 'r' ? 'ew-resize' : 'ns-resize';
        if (edge !== edgeHot) setEdgeHot(edge);
        if (floorHot) setFloorHot(false);
        return;
      } else {
        const guide = guideAt(x, y);
        c.style.cursor = guide === 'x' ? 'col-resize' : guide === 'y' ? 'row-resize' : p.turn ? 'crosshair' : 'move';
      }
      if (edgeHot) setEdgeHot(null);
      if (floorHot) setFloorHot(false);
      return;
    }
    if (g.kind === 'pan') {
      if (Math.abs(x - g.x) + Math.abs(y - g.y) > 3) g.moved = true;
      setView({ z: view.z, x: g.vx + x - g.x, y: g.vy + y - g.y, h: view.h });
    } else if (g.kind === 'frame') {
      const dx = Math.round((x - g.x) / view.z);
      const dy = Math.round((y - g.y) / view.z);
      if (!g.moved && !dx && !dy) return;
      p.onDrag(dx, dy, g.moved ? 'move' : 'start');
      g.moved = true;
    } else if (g.kind === 'turn') {
      if (!g.moved && Math.abs(x - g.x) + Math.abs(y - g.y) < 4) return;
      const d = angleAt(x, y) - g.a0;
      p.onTurn(((((d + 180) % 360) + 360) % 360) - 180, g.moved ? 'move' : 'start', e.shiftKey);
      g.moved = true;
    } else if (g.kind === 'pivot') {
      const [fx, fy] = toFrame(x, y);
      p.onPivot(fx, fy, g.tag);
    } else if (g.kind === 'floor') {
      const [fx, fy] = toFrame(x, y);
      const side = eline?.side;
      p.onEdgeLine?.(g.off + (side === 'left' || side === 'right' ? fx : fy), g.tag);
    } else if (g.kind === 'edge') {
      if (!p.area || !p.onArea) return;
      const [fx, fy0] = toFrame(x, y);
      // Верх и низ рамки — от пола анимации (низа экрана), а не от низа кадра
      const fy = fy0 + lift;
      const v = Math.round(g.edge === 'l' ? -fx : g.edge === 'r' ? fx : g.edge === 't' ? -fy : fy);
      const next = { ...p.area, [g.edge]: Math.max(g.edge === 'b' ? 0 : 1, v) };
      // Shift — левый и правый край вместе: рамка симметрична
      if (e.shiftKey && (g.edge === 'l' || g.edge === 'r')) next.l = next.r = next[g.edge];
      p.onArea(next, g.tag);
    } else if (g.kind === 'guide') {
      const a = overlayAnchor(scene);
      if (g.axis === 'x') p.onGuides({ ...p.guides, x: Math.round((x - a.x) / view.z) });
      else p.onGuides({ ...p.guides, y: Math.round((y - a.y) / view.z) });
    } else {
      const o = sliceOrigin(scene);
      p.onLine(g.axis, g.i, g.axis === 'x' ? (x - o.x) / view.z : (y - o.y) / view.z, g.tag);
    }
  };

  const up = () => {
    const g = grab.current;
    grab.current = null;
    setHot(null);
    if (g?.kind === 'edge') setEdgeHot(null);
    if (g?.kind === 'floor') setFloorHot(false);
    if (g?.kind === 'pan' && !g.moved && g.pick != null) p.onPickCell(g.pick);
    // Щелчок с «Поворотом» — центр сюда
    if (g?.kind === 'turn' && !g.moved) {
      const [fx, fy] = toFrame(g.x, g.y);
      p.onPivot(fx, fy, `pivot:${Date.now()}`);
    }
  };

  return (
    <div className="fe-stage" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        role="img"
        aria-label={t(p.mode === 'overlay' ? 'frames.stage.overlay' : 'frames.stage.slice')}
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={() => {
          grab.current = null;
          setHot(null);
        }}
      />
      <div className="fe-zoom" hidden={p.mode === 'overlay' && !p.items.length}>
        <button type="button" className="fe-zoom-btn" aria-label={t('frames.zoomOut')} onClick={() => zoomAt(view.z / 1.25, size.w / 2, size.h / 2)}>
          −
        </button>
        <span className="fe-zoom-value">{Math.round(view.z * 100)}%</span>
        <button type="button" className="fe-zoom-btn" aria-label={t('frames.zoomIn')} onClick={() => zoomAt(view.z * 1.25, size.w / 2, size.h / 2)}>
          +
        </button>
        <button type="button" className="fe-zoom-btn fe-zoom-btn--wide" onClick={() => setView(null)}>
          {t('frames.fit')}
        </button>
      </div>
    </div>
  );
}

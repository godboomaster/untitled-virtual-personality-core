// Редактор кадров (app/api/portrait_frames.py): источник — лист портрета или
// анимация, слои — кадры таких источников друг на друге

export type FrameKind = 'sheet' | 'anim';

/** Правка: сдвиг в px кадра, масштаб вокруг низа-середины кадра и поворот.
    Поворот — только у отдельного кадра: r — градусы по часовой стрелке,
    px/py — центр поворота в px кадра от низа-середины (вверх — минус); он
    едет вместе с масштабом и сдвигом. Нет центра — низ-середина */
export interface FrameAdjust {
  dx: number;
  dy: number;
  s: number;
  r?: number;
  px?: number;
  py?: number;
}

/** Правка всего листа и правки отдельных кадров (ключ — номер кадра с нуля:
    у анимации — кадра по её порядку, а не клетки листа) */
/** Своя рамка кадра анимации: px кадра нарезки от середины его пола влево,
    вправо, вверх и вниз (под пол). Нет — рамка подбирается сама (frameBox) */
export interface FrameArea {
  l: number;
  r: number;
  t: number;
  b: number;
}

export interface FrameFix {
  all?: FrameAdjust;
  cells?: Record<string, FrameAdjust>;
  box?: FrameArea;
  /** Низ экрана анимации: линия на столько px кадра нарезки выше низа
      фигуры стоит на нижнем крае экрана, что ниже — под ним. Нет — по низу */
  floor?: number;
  /** Подглядывает из-за другого края: линия на at px кадра нарезки от этого
      его края встаёт на край экрана (портрет на столе переходит к нему) */
  edge?: { side: EdgeSide; at: number };
}

/** Край экрана, на котором стоит или из-за которого подглядывает анимация */
export type EdgeSide = 'left' | 'right' | 'top';
export type ScreenEdge = 'bottom' | EdgeSide;

/** Время кадров анимации: fps — шаг, holds — сколько шагов стоит каждый кадр
    (null — по одному), loops — раз за раз, loop — по кругу, frames — сколько
    кадров играть (null — все занятые) */
export interface FrameTiming {
  fps: number;
  loops: number;
  loop: boolean;
  holds: number[] | null;
  frames?: number | null;
}

/** Как лист разрезан: линии сетки xs (cols+1) и ys (rows+1), px листа */
export interface FrameCut {
  rows: number;
  cols: number;
  offset_x: number;
  offset_y: number;
  bg: 'auto' | 'keep';
  tolerance: number;
  align?: 'grid' | 'feet';
  xs: number[] | null;
  ys: number[] | null;
}

// POST …/portrait/frames/{kind}/{id}/cut — пробная нарезка, на диск не идёт
export interface FrameAtlas {
  rows: number;
  cols: number;
  fw: number;
  fh: number;
  count: number; // сколько кадров в деле (у анимации — сколько играется)
  atlas: string; // data-URL webp без правок
}

// GET и PUT …/portrait/frames/{kind}/{id}
export interface FrameSourceData extends FrameAtlas {
  kind: FrameKind;
  id: string;
  name: string;
  fps: number;
  max_grid: number;
  frame_max: number; // кадр растёт под правки до frame_max×frame_max
  saved_w?: number; // кадр атласа, что на ядре (только у анимации)
  saved_h?: number;
  cut: FrameCut;
  fix: FrameFix;
  timing: FrameTiming | null; // у листа портрета нет
  order: number[] | null; // порядок кадров анимации: кадр j — клетка листа order[j]; null — как на листе
  sheet_w: number; // 0 — исходника нет, нарезку не поправить
  sheet_h: number;
  updated_at?: number | null;
}

export interface FrameLayer {
  id: string;
  source: string; // ключ источника: sheet:A, anim:1a2b3c4d
  cell: number; // какой кадр показывает слой, с нуля
  linked: boolean; // листается вместе с другими такими слоями
  opacity: number; // 0..1
  color: string | null; // свой цвет слоя; null — как на листе
  visible: boolean;
}

export type FrameBlend = 'normal' | 'flicker';
/** Фон холста: 'checker' — прозрачный (шахматка), иначе свой цвет #rrggbb */
export type FrameBg = string;

/** Что двигаем: кадр, его ряд, его столбец или лист целиком */
export type FrameScope = 'frame' | 'row' | 'col' | 'sheet';
export const FRAME_SCOPES: readonly FrameScope[] = ['frame', 'row', 'col', 'sheet'];

export type FrameAlign = 'image' | 'bottom' | 'center';

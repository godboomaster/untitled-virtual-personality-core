import { createElement } from 'react';
import type { ReactElement } from 'react';
import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';
import type { RoomAvatarPreset } from '../mockData';
import { NEUTRAL_ACCENT } from '../room/avatarOptions';
import type { HeadLook } from './headLook';
import { AvatarHeadSvg } from './LiveHead';
import type { Mouth } from './motion';
import type { FaceSheet, PortraitData } from './portraitTypes';

/* Голова фигуры из «Комнаты» картинкой. Листы — в формате живого портрета
   (app/api/portrait_api.py): сетка ракурсов 5×5, клетка (r, c) — взгляд
   ((c − 2) / 2, (r − 2) / 2), как у LivePortrait; A — глаза открыты, B и C —
   рот приоткрыт и открыт, D — глаза закрыты. По листу A портрет на рабочем
   столе ловит клик по фигуре, из листов виджет телефона режет кадры
   (widgetHeads). Лист рисуется одной SVG-картинкой: 25 вложенных <svg>, каждый
   обрезает плечи по своей клетке. */

const GRID = 5;
const BOX = 64; // бокс головы (LiveHead)

const SHEET_FACE: Record<FaceSheet, { closed: boolean; mouth: Mouth }> = {
  A: { closed: false, mouth: 0 },
  B: { closed: false, mouth: 1 },
  C: { closed: false, mouth: 2 },
  D: { closed: true, mouth: 0 },
  E: { closed: true, mouth: 1 },
  F: { closed: true, mouth: 2 },
};

/** Листы головы; cell — сторона клетки в px, look — рисовка скина (нет — голова фигуры). */
export async function headAtlas(
  preset: RoomAvatarPreset,
  {
    cell = 256,
    sheets = ['A', 'B', 'C', 'D'],
    look = null,
  }: { cell?: number; sheets?: FaceSheet[]; look?: HeadLook | null } = {},
): Promise<PortraitData> {
  const out: Partial<Record<FaceSheet, string>> = {};
  for (const sheet of sheets) {
    const cells: ReactElement[] = [];
    for (let r = 0; r < GRID; r++) {
      for (let c = 0; c < GRID; c++) {
        cells.push(
          createElement(AvatarHeadSvg, {
            key: `${r}-${c}`,
            preset,
            ...SHEET_FACE[sheet],
            gaze: { x: (c - 2) / 2, y: (r - 2) / 2 },
            accent: NEUTRAL_ACCENT,
            look,
            label: '',
            x: c * BOX,
            y: r * BOX,
            width: BOX,
            height: BOX,
          }),
        );
      }
    }
    // Размер картинки — сразу в пикселях: SVG растрируется чётко, без растяжения
    const side = GRID * BOX;
    const px = GRID * cell;
    const markup = await svgMarkup(createElement('svg', { viewBox: `0 0 ${side} ${side}`, width: px, height: px }, cells));
    out[sheet] = await rasterize(markup, px, null);
  }
  return { rows: GRID, cols: GRID, cell, sheets: out, settings: {} };
}

/** Одна голова, смотрит прямо, на фоне (иконка уведомления) — PNG data-URL. */
export async function headIcon(
  preset: RoomAvatarPreset,
  size: number,
  background: string,
  accent: string,
  look: HeadLook | null = null,
): Promise<string> {
  const el = createElement(AvatarHeadSvg, { preset, gaze: { x: 0, y: 0 }, accent, look, label: '', width: size, height: size });
  return rasterize(await svgMarkup(el), size, background);
}

// Разметка SVG из React-элемента: отрисовать вне страницы и снять. Своей
// задачей: из эффекта React (портрет на рабочем столе) flushSync не рисует
async function svgMarkup(el: ReactElement): Promise<string> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  const box = document.createElement('div');
  const root = createRoot(box);
  flushSync(() => root.render(el));
  const svg = box.querySelector('svg');
  const markup = svg ? new XMLSerializer().serializeToString(svg) : '';
  root.unmount();
  if (!markup) throw new Error('голова не нарисовалась');
  return markup;
}

// SVG → квадрат size px: WebP, где браузер его кодирует, иначе PNG
async function rasterize(markup: string, size: number, background: string | null): Promise<string> {
  const img = new Image();
  img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(markup)}`;
  await img.decode();
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('нет canvas');
  if (background) {
    ctx.fillStyle = background;
    ctx.fillRect(0, 0, size, size);
  }
  ctx.drawImage(img, 0, 0, size, size);
  return background ? canvas.toDataURL('image/png') : canvas.toDataURL('image/webp', 0.9);
}

/* Опции конструктора аватара (общие для фигуры в сцене и конструктора в
   арт-мастерской). Индексы eyes/accessory/shade в RoomAvatarPreset — индексы
   в этих массивах. */

import type { RoomAvatarPreset } from '../mockData';

export const headShapes: { id: RoomAvatarPreset['head']; glyph: string }[] = [
  { id: 'circle', glyph: '●' },
  { id: 'square', glyph: '■' },
  { id: 'hex', glyph: '⬡' },
  { id: 'diamond', glyph: '◆' },
];

export const eyeOptions = ['◉ ◉', '▪ ▪', '— —', '◠ ◠', '✦ ✦'];

export const accessoryOptions = ['antenna', 'ears', 'halo', 'none'] as const;

// Оттенки серого для корпуса аватара
export const avatarShades = ['#1c1c1c', '#4a4a4a', '#8f8f8f', '#c9c9c9'];

// Точка «ступней» SVG-фигуры в долях её бокса (стоя и сидя)
export const FIGURE_ANCHOR = { x: 0.5, y: 0.97 };

// Лёжа (sleep): точка — середина верха матраса (бокс 104×52, матрас — y=40)
export const SLEEP_ANCHOR = { x: 0.5, y: 40 / 52 };

// На чём сидит фигура: стул за столом / сиденье без стола / пол; null — стоит
export type AvatarSeat = 'desk' | 'chair' | 'floor' | null;

export function figureAnchor(pose: string): { x: number; y: number } {
  return pose === 'sleep' ? SLEEP_ANCHOR : FIGURE_ANCHOR;
}

export interface SpriteLike {
  dataUrl: string;
  anchor: { x: number; y: number };
}

// Спрайт позы: свой (art.sprites[pose]) или базовый; own=false — базовый
// спрайт, позу изображают CSS-модификаторы (наклон, затемнение во сне)
export function pickSprite(
  pose: string,
  sprite: SpriteLike | null | undefined,
  sprites: Partial<Record<string, SpriteLike>> | null | undefined,
): { asset: SpriteLike; own: boolean } | null {
  const own = sprites?.[pose];
  if (own) return { asset: own, own: true };
  return sprite ? { asset: sprite, own: false } : null;
}

/* Опции конструктора аватара (общие для фигуры в сцене и конструктора в
   арт-мастерской). Индексы eyes/accessory/shade в RoomAvatarPreset — индексы
   в этих массивах. */

import type { RoomAvatarPreset } from '../mockData';

// Формы головы в порядке кнопок конструктора (кнопка — силуэт формы,
// room/avatarHead HeadShapeIcon); порядок исходных четырёх не менять —
// по нему же выбирается форма по умолчанию (roomModel.defaultAvatarFor)
export const headShapes: { id: RoomAvatarPreset['head'] }[] = [
  { id: 'circle' },
  { id: 'square' },
  { id: 'hex' },
  { id: 'diamond' },
  { id: 'squircle' },
  { id: 'hexv' },
  { id: 'octagon' },
  { id: 'pent' },
  { id: 'capsule' },
  { id: 'screen' },
  { id: 'can' },
];

// Аксессуары: индекс — RoomAvatarPreset.accessory; первые четыре — исходные
// (по ним выбирается облик по умолчанию), остальные добавлены 04.11.2026.
// Рисунок — room/avatarHead accessoryDraw
export const accessoryOptions = ['antenna', 'ears', 'halo', 'none', 'twin', 'spring', 'headphones', 'tophat', 'bow', 'sprout'] as const;

// Оттенки серого для корпуса аватара
export const avatarShades = ['#1c1c1c', '#4a4a4a', '#8f8f8f', '#c9c9c9'];

// Антенна и нимб там, где нет CSS-переменных темы (картинки, окно портрета
// на рабочем столе): лежат и на светлом, и на тёмном
export const NEUTRAL_ACCENT = '#8a8a8a';

// Цвета фигуры по оттенку корпуса
export function avatarTone(config: RoomAvatarPreset) {
  return {
    shade: avatarShades[config.shade] ?? avatarShades[1],
    // На светлых оттенках глаза тёмные, на тёмных — светлые
    eyeColor: config.shade >= 2 ? '#1c1c1c' : '#eae8e1',
    stroke: config.shade >= 2 ? '#5a5a5a' : '#8f8f8f',
  };
}

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

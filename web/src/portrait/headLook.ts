import type { ClothKey } from '../room/avatarBody';
import { createContext } from 'react';
import type { ComponentType, SVGProps } from 'react';
import type { RoomAvatarPreset } from '../mockData';
import type { Mouth } from './motion';

/* Рисовка головы персоны. По умолчанию голова фигуры из «Комнаты» — та, что
   в AvatarHeadSvg; скин может подставить свою (web/skins/<id>/avatar.tsx).
   Облик (форма, глаза, аксессуар, оттенок) и движение (взгляд слоями,
   моргание, рот) — общие: рисовка получает их готовыми и только рисует.
   Модуль без зависимостей от скинов — его берёт и портрет на рабочем столе
   (desktop/), где скинов нет и контекст пуст. */

export interface HeadDrawProps {
  preset: RoomAvatarPreset;
  /** Вид глаз сейчас — своя эмоция поверх preset.eyes (гладят, думает…);
   *  нет — preset.eyes. Облик (наклон и т. п.) считать по preset, не по нему */
  eyes?: number;
  scheme: 'light' | 'dark';
  closed: boolean;
  mouth: Mouth;
  /** Слои взгляда — повесить на свои <g>: голова, лицо на ней (внутри головы),
   *  аксессуар (внутри головы, антенна и нимб чуть отстают) */
  layers: {
    head: SVGProps<SVGGElement>;
    face: SVGProps<SVGGElement>;
    accessory: SVGProps<SVGGElement>;
    /** Только глаза (внутри лица): фигура в комнате моргает их CSS-анимацией */
    eyes?: SVGProps<SVGGElement>;
  };
  /** Только голова, без плеч — у фигуры в комнате своё тело */
  headOnly?: boolean;
  /** Улыбка: фигура в комнате смотрит на вас */
  smile?: boolean;
}

export type HeadDraw = ComponentType<HeadDrawProps>;

/** Как красить тело фигуры в комнате (торс, руки, ноги): заливка, обводка и
 *  подложка со сдвигом без размытия (back — её цвет, shift — сдвиг вправо-вниз) */
export interface FigurePaint {
  fill: string;
  /** Цвета ткани одежды (room/avatarBody ClothKey); нет — серые */
  cloth?: Partial<Record<ClothKey, string>>;
  stroke?: string;
  strokeWidth?: number;
  back?: string;
  shift?: number;
}

export type FigurePaintFn = (preset: RoomAvatarPreset, scheme: 'light' | 'dark') => FigurePaint;

export interface HeadLook {
  draw: HeadDraw;
  /** Тело фигуры в комнате; нет — тело стандартное */
  paint?: FigurePaintFn;
  scheme: 'light' | 'dark';
  /** Скин и схема — для кэшей картинок (иконки уведомлений, листы виджета) */
  key: string;
}

// Рисовка применённого скина; null — голова по умолчанию
export const HeadLookContext = createContext<HeadLook | null>(null);

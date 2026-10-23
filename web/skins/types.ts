import type { ComponentType } from 'react';

/* Тема оформления (скин) — обёртка над обычным интерфейсом: свои цвета,
   шрифты, фактуры и декор поверх тех же разделов, кнопок и полей. Папка
   скина — web/skins/<id>/, правила оформления — web/skins/README.md. */

export type SkinScheme = 'light' | 'dark';

export interface SkinManifest {
  id: string; // совпадает с именем папки
  order: number; // место в галерее
  name: { ru: string; en: string };
  tagline: { ru: string; en: string }; // одна строка под названием в галерее
  // Какие схемы скин умеет; первая — если выбранной в шапке у скина нет
  schemes: SkinScheme[];
}

// Необязательный живой фон скина (ambient.tsx): слой под интерфейсом
export interface SkinAmbientProps {
  scheme: SkinScheme;
  reducedMotion: boolean;
}

export type SkinAmbient = ComponentType<SkinAmbientProps>;

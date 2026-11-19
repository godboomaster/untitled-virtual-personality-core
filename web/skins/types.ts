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

// Необязательный голос скина (texts.ts): свои формулировки строк интерфейса
// по ключам из src/i18n/ru.ts и en.ts. Действует, пока скин применён;
// ключа нет — обычная строка. Плейсхолдеры {name} — те же, что в исходной строке.
export type SkinTexts = Partial<Record<'ru' | 'en', Record<string, string>>>;

// Необязательная своя рисовка головы персоны (avatar.tsx, default export):
// облик и движение — общие (src/portrait/headLook.ts), скин только рисует
// Необязательные свои части интерфейса (parts.tsx, именованные экспорты)
export type {
  SkinBootPhase,
  SkinBootScreenProps,
  SkinBrowserStateProps,
  SkinParts,
  SkinTaskCardProps,
} from '../src/skinParts';

// и необязательный named export figurePaint — краска тела фигуры в комнате
export type {
  HeadDraw as SkinAvatar,
  HeadDrawProps as SkinAvatarProps,
  FigurePaintFn as SkinFigurePaint,
} from '../src/portrait/headLook';

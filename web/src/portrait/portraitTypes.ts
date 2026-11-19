// Живой портрет персоны (app/api/portrait_api.py): листы ракурсов головы
// rows×cols, по листу на сочетание глаз и рта; VA…VO — рот на гласную
// (по желанию: есть — рот в речи идёт по гласным текста)
export type FaceSheet = 'A' | 'B' | 'C' | 'D' | 'E' | 'F';
export type VowelSheet = 'VA' | 'VI' | 'VU' | 'VE' | 'VO';
export type PortraitSheet = FaceSheet | VowelSheet;
export const FACE_SHEETS: readonly FaceSheet[] = ['A', 'B', 'C', 'D', 'E', 'F'];
export const VOWEL_SHEETS: readonly VowelSheet[] = ['VA', 'VI', 'VU', 'VE', 'VO'];
export const PORTRAIT_SHEETS: readonly PortraitSheet[] = [...FACE_SHEETS, ...VOWEL_SHEETS];

export type Vowel = 'a' | 'i' | 'u' | 'e' | 'o';
export interface InitiativeBadge {
  kind: 'symbol' | 'image';
  symbol: string;
  image: string | null;
  /** Размер плашки в CSS-пикселях. Старые ядра могут не вернуть поле. */
  size?: number;
}
export const VOWEL_SHEET: Record<Vowel, VowelSheet> = { a: 'VA', i: 'VI', u: 'VU', e: 'VE', o: 'VO' };

export interface PortraitSheetSettings {
  offset_x: number;
  offset_y: number;
  bg: 'auto' | 'keep';
  tolerance: number;
}

export type PortraitSheets = Partial<Record<PortraitSheet, string>>; // атлас листа, data-URL

// Когда эмоция на лице сама — по настроению персоны (state engine): зона и/или
// слова тега настроения; имя эмоции — тоже такое слово. Всё необязательно
export interface EmotionMood {
  valence?: 'good' | 'bad';
  arousal?: 'calm' | 'excited';
  words?: string[];
}

// Настроение персоны сейчас: GET /api/personas/{p}/portrait/mood
export interface PersonaMood {
  /** −1 (плохо) … 1 (хорошо) */
  valence: number;
  /** 0 (спокойно) … 1 (взбудоражено) */
  arousal: number;
  /** 1–3 слова: «спокойствие», «радость» */
  tag: string;
}

// Эмоция: свой набор тех же листов (без своего A её нет); персона выбирает
// сама маркером [EMO:имя] в ответе, а с mood — лицо в ней, пока такое
// настроение. Ключ листа на ядре — «id.лист»
export interface PortraitEmotion {
  id: string;
  name: string;
  /** Когда уместна — подсказка персоне */
  hint: string;
  /** Нет — только по маркеру */
  mood?: EmotionMood;
  sheets: PortraitSheets;
  settings: Partial<Record<PortraitSheet, PortraitSheetSettings>>;
}

// GET /api/personas/{p}/portrait
export interface PortraitData {
  rows: number;
  cols: number;
  cell: number;
  sheets: PortraitSheets;
  settings: Partial<Record<PortraitSheet, PortraitSheetSettings>>;
  /** Нет у старого ядра и у голов, нарисованных вебом (headAtlas) */
  emotions?: PortraitEmotion[];
  updated_at?: number | null;
  // Только у ?live=1 (живой аватар, app/api/avatar_modes): режим основного
  // чата и чьи это листы ("" — обычные, иначе id режима)
  mode?: string;
  avatar?: string;
  /** PUT emotions/new: id новой эмоции */
  created?: string;
}

/** Ключ листа на ядре: обычный — «A», эмоции — «id.A». */
export const sheetKey = (sheet: PortraitSheet, emotion = '') => (emotion ? `${emotion}.${sheet}` : sheet);

// PUT /api/personas/{p}/portrait/{лист}: без data_url — переразметить загруженный
export interface PortraitSheetUpdate {
  data_url?: string;
  rows: number;
  cols: number;
  offset_x?: number;
  offset_y?: number;
  bg?: 'auto' | 'keep';
  tolerance?: number;
}

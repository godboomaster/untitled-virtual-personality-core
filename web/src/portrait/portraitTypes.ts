// Живой портрет персоны (app/api/portrait_api.py): листы ракурсов головы
// rows×cols, по листу на сочетание глаз и рта
export type PortraitSheet = 'A' | 'B' | 'C' | 'D' | 'E' | 'F';
export const PORTRAIT_SHEETS: readonly PortraitSheet[] = ['A', 'B', 'C', 'D', 'E', 'F'];

export interface PortraitSheetSettings {
  offset_x: number;
  offset_y: number;
  bg: 'auto' | 'keep';
  tolerance: number;
}

// GET /api/personas/{p}/portrait
export interface PortraitData {
  rows: number;
  cols: number;
  cell: number;
  sheets: Partial<Record<PortraitSheet, string>>; // атлас листа, data-URL
  settings: Partial<Record<PortraitSheet, PortraitSheetSettings>>;
  updated_at?: number | null;
}

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

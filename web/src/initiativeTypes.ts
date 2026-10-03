import type { InitiativeEvent } from './mockData';

// Тип самоинициативы бэкенда → тип события в интерфейсе (раздел
// «Самоинициатива» и досье скина)
export const INIT_TYPE_MAP: Record<string, InitiativeEvent['type']> = {
  continuation: 'continuation',
  memory_recall: 'observation',
  self_reflection: 'thought',
  user_reflection: 'thought',
  todo_reflection: 'observation',
  inventory_reflection: 'observation',
};

type T = (key: string, vars?: Record<string, string | number>) => string;

// Порог молчания самоинициативы (минуты) — коротко: «45 мин», «3 ч», «1,5 ч»
export function formatSilence(min: number, t: T, lang: string): string {
  if (min < 60) return t('room.dur.minShort', { n: Math.max(1, Math.round(min)) });
  const h = Math.round((min / 60) * 2) / 2;
  return t('room.dur.hShort', { h: h.toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US') });
}

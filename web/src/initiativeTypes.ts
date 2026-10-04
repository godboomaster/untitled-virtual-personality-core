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

// Молчание пользователя против действующего порога: минуты с последней
// реплики (lastUserTs — секунды), доля шкалы и подпись «{n} из порога {max}».
// Нет порога — прочерк, нет реплики — прочерк вместо минут и пустая шкала
export function silenceProgress(
  lastUserTs: number | null,
  thresholdMin: number | null | undefined,
  nowMs: number,
  t: T,
  lang: string,
): { pct: number; text: string } {
  if (thresholdMin == null) return { pct: 0, text: '—' };
  const min = lastUserTs ? Math.max(0, Math.floor((nowMs / 1000 - lastUserTs) / 60)) : null;
  const n = min == null ? '—' : min < 1 ? t('room.dur.minShort', { n: 0 }) : formatSilence(min, t, lang);
  return {
    pct: min != null && thresholdMin ? Math.min(100, Math.round((min / thresholdMin) * 100)) : 0,
    text: t('init.silenceProgressLive', { n, max: formatSilence(thresholdMin, t, lang) }),
  };
}

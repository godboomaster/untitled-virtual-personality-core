import { ApiError } from '../api';

/* Общее раздела «Сервисы» без разметки (servicesParts.tsx — компоненты). */

export type T = (key: string, vars?: Record<string, string | number>) => string;

export function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return e instanceof Error ? e.message : String(e);
}

export function timeLabel(ts: number | null | undefined, locale: string): string {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay
    ? d.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleString(locale, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

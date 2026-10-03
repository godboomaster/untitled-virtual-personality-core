import type { ReminderEntry, ReminderRecurrenceInput } from './api';
import en from './i18n/en';
import ru from './i18n/ru';

/* Повтор напоминания: варианты формы (модалка «Дела», форма скина) ↔
   расписание бэкенда (reminder_manager: daily / weekly с днями недели).
   Порядок вариантов совпадает с 'tasks.repeatOptions', дни — с
   'tasks.weekdays' (0 — пн … 6 — вс, как weekday() на бэкенде). */

type T = (key: string, vars?: Record<string, string | number>) => string;

export type RepeatKind = 'once' | 'daily' | 'weekdays' | 'weekends' | 'days';
export const REPEAT_KINDS: RepeatKind[] = ['once', 'daily', 'weekdays', 'weekends', 'days'];

const WORK_DAYS = [0, 1, 2, 3, 4];
const WEEKEND = [5, 6];

const sameDays = (a: number[], b: number[]) => a.length === b.length && a.every((d, i) => d === b[i]);

// Дни расписания (0 — пн) или null — каждый день (как schedule_days на бэкенде)
export function recurrenceDays(rec: ReminderEntry['recurrence']): number[] | null {
  if (!rec || rec.type !== 'weekly') return null;
  const days = (rec.weekdays ?? []).filter((d) => d >= 0 && d <= 6);
  if (days.length) return [...new Set(days)].sort((a, b) => a - b);
  return rec.weekday != null ? [rec.weekday] : null;
}

// Вариант формы по расписанию сервера (предзаполнение при правке)
export function formFromRecurrence(rec: ReminderEntry['recurrence']): { kind: RepeatKind; days: number[] } {
  if (!rec) return { kind: 'once', days: [] };
  const days = recurrenceDays(rec);
  if (!days) return { kind: 'daily', days: [] };
  if (sameDays(days, WORK_DAYS)) return { kind: 'weekdays', days: [] };
  if (sameDays(days, WEEKEND)) return { kind: 'weekends', days: [] };
  return { kind: 'days', days };
}

// Расписание для API по варианту формы; null — разовое; 'no-days' — «по дням
// недели» без выбранных дней. Час и минуту не шлём: сервер берёт их из срока
// напоминания по часовому поясу пользователя (настройки), а часы браузера
// могут быть в другом поясе — повтор срабатывал бы в чужое время
export function recurrenceFromForm(kind: RepeatKind, days: number[]): ReminderRecurrenceInput | null | 'no-days' {
  if (kind === 'once') return null;
  if (kind === 'daily') return { type: 'daily' };
  const picked = kind === 'weekdays' ? WORK_DAYS : kind === 'weekends' ? WEEKEND : days;
  if (!picked.length) return 'no-days';
  return { type: 'weekly', weekdays: [...new Set(picked)].sort((a, b) => a - b) };
}

// Подписи на обоих языках: шаблон скина и сам скин могут быть на языке,
// отличном от языка интерфейса
const lower = (v: string) => v.split('|').map((x) => x.trim().toLowerCase());
const OPTION_LABELS = [lower(ru['tasks.repeatOptions']), lower(en['tasks.repeatOptions'])];
const DAY_NAMES = [lower(ru['tasks.weekdays']), lower(en['tasks.weekdays'])];

// Свободный текст повтора из формы скина: подпись варианта («по будням»,
// «on weekdays») или список дней («пн, ср, пт», «Mon, Wed»); пусто — null
// (поля нет / не выбрано); не распознан — 'unknown'
export function formFromRepeatText(text: string): { kind: RepeatKind; days: number[] } | null | 'unknown' {
  const s = text.trim().toLowerCase();
  if (!s) return null;
  for (const labels of OPTION_LABELS) {
    const idx = labels.indexOf(s);
    if (idx >= 0 && idx < REPEAT_KINDS.length && REPEAT_KINDS[idx] !== 'days') return { kind: REPEAT_KINDS[idx], days: [] };
  }
  const parts = s.split(/[\s,;]+/).filter(Boolean);
  for (const names of DAY_NAMES) {
    const days = parts.map((p) => names.indexOf(p));
    if (parts.length && days.every((d) => d >= 0)) return { kind: 'days', days: [...new Set(days)].sort((a, b) => a - b) };
  }
  return 'unknown';
}

// Повтор для списка: «разовое» / «каждый день · 09:00» / «пн, ср, пт · 09:00»
export function fmtRecurrence(rec: ReminderEntry['recurrence'], t: T): string {
  const labels = t('tasks.repeatOptions').split('|');
  const { kind, days } = formFromRecurrence(rec);
  if (!rec) return labels[0];
  const hh = `${String(rec.hour).padStart(2, '0')}:${String(rec.minute).padStart(2, '0')}`;
  const names = t('tasks.weekdays').split('|');
  const what = kind === 'days' ? days.map((d) => names[d] ?? '').join(', ') : labels[REPEAT_KINDS.indexOf(kind)];
  return `↻ ${what} · ${hh}`;
}

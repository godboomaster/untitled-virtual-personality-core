// Разбор «когда» напоминания из формы (скин-досье, вкладка задач): дата и
// время, как их вводит человек или как их показывает список напоминаний
// (toLocaleString: ru «01.10, 14:30», en «10/01, 02:30 PM»).
//
// new Date(строка) для этого не годится: «01.10» он читает как 2001 год,
// «завтра» — как Invalid Date, а ISO «2026-10-01» — как полночь по UTC
// (западнее Гринвича это предыдущий день). Всё считаем в локальном времени
// браузера — в нём же список показывает срок напоминания.

export type WhenLocale = 'ru' | 'en';

export type WhenError = 'date' | 'time' | 'past';

export type WhenResult = { ok: true; at: Date } | { ok: false; error: WhenError };

// Пустое поле: ничего не введено или прочерк из списка («—»)
const isBlank = (s: string | undefined) => !s || /^[\s—–-]*$/.test(s);

// Слова-дни: смещение от сегодняшнего дня (оба языка — независимо от локали)
const DAY_WORDS: [RegExp, number][] = [
  [/^(послезавтра|после\s+завтра|day\s+after\s+tomorrow)$/i, 2],
  [/^(завтра|tomorrow)$/i, 1],
  [/^(сегодня|today)$/i, 0],
];

// Собрать локальную дату и проверить, что день/месяц реальные (31.02 — нет)
function localDate(y: number, m: number, d: number): Date | null {
  if (m < 1 || m > 12 || d < 1 || d > 31) return null;
  const dt = new Date(y, m - 1, d);
  return dt.getFullYear() === y && dt.getMonth() === m - 1 && dt.getDate() === d ? dt : null;
}

/** Дата напоминания (полночь по локальному времени) или null — не разобрать.
 * ISO YYYY-MM-DD; DD.MM[.YYYY]; через «/» — MM/DD[/YYYY] для en и
 * DD/MM[/YYYY] для ru; «сегодня/завтра/послезавтра», today/tomorrow.
 * Без года — ближайшая такая дата не раньше сегодняшней. */
export function parseReminderDate(input: string, locale: WhenLocale, now: Date = new Date()): Date | null {
  const s = input.trim().replace(/\.$/, '');
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  for (const [re, offset] of DAY_WORDS) {
    if (re.test(s)) return new Date(today.getFullYear(), today.getMonth(), today.getDate() + offset);
  }
  let m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(s);
  if (m) return localDate(Number(m[1]), Number(m[2]), Number(m[3]));
  let day: number;
  let month: number;
  let year: string | undefined;
  m = /^(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?$/.exec(s);
  if (m) {
    [day, month, year] = [Number(m[1]), Number(m[2]), m[3]];
  } else {
    m = /^(\d{1,2})\/(\d{1,2})(?:\/(\d{2}|\d{4}))?$/.exec(s);
    if (!m) return null;
    [day, month] = locale === 'en' ? [Number(m[2]), Number(m[1])] : [Number(m[1]), Number(m[2])];
    year = m[3];
  }
  if (year) return localDate(year.length === 2 ? 2000 + Number(year) : Number(year), month, day);
  const thisYear = localDate(today.getFullYear(), month, day);
  if (thisYear && thisYear >= today) return thisYear;
  return localDate(today.getFullYear() + 1, month, day) ?? thisYear;
}

/** Время «ЧЧ:ММ» (также «Ч.ММ», «9», «2:30 PM», «9 am») → [часы, минуты] или null. */
export function parseReminderTime(input: string): [number, number] | null {
  const m = /^(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?\s?m\.?|p\.?\s?m\.?)?$/i.exec(input.trim());
  if (!m) return null;
  let h = Number(m[1]);
  const min = m[2] ? Number(m[2]) : 0;
  if (m[3]) {
    if (h < 1 || h > 12) return null;
    const pm = /^p/i.test(m[3]);
    h = (h % 12) + (pm ? 12 : 0);
  }
  if (h > 23 || min > 59) return null;
  return [h, min];
}

/** Момент срабатывания по полям формы.
 * Нет ни даты, ни времени — через час (как у бэкенда по умолчанию);
 * только дата — в 09:00 этого дня; только время — сегодня, а если оно
 * уже прошло — завтра. Дата+время в прошлом — ошибка 'past' (молча
 * переносить нельзя: человек явно назвал момент). */
export function parseReminderWhen(
  dateInput: string | undefined,
  timeInput: string | undefined,
  locale: WhenLocale,
  now: Date = new Date(),
): WhenResult {
  const hasDate = !isBlank(dateInput);
  const hasTime = !isBlank(timeInput);
  if (!hasDate && !hasTime) return { ok: true, at: new Date(now.getTime() + 3600 * 1000) };
  const time = hasTime ? parseReminderTime(timeInput!) : [9, 0] as [number, number];
  if (!time) return { ok: false, error: 'time' };
  if (!hasDate) {
    const at = new Date(now.getFullYear(), now.getMonth(), now.getDate(), time[0], time[1]);
    if (at.getTime() <= now.getTime()) at.setDate(at.getDate() + 1);
    return { ok: true, at };
  }
  const day = parseReminderDate(dateInput!, locale, now);
  if (!day) return { ok: false, error: 'date' };
  const at = new Date(day.getFullYear(), day.getMonth(), day.getDate(), time[0], time[1]);
  if (at.getTime() <= now.getTime()) return { ok: false, error: 'past' };
  return { ok: true, at };
}

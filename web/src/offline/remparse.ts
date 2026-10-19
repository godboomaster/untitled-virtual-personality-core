/* Разбор просьбы о напоминании на телефоне — перенос parse_reminder ядра
   (app/features/reminder_manager.py) без модели: «через 2 минуты», «завтра
   в 8», «в 8 вечера», «remind me in 30 minutes». Мгновенно и так же, как у
   ядра; scripts/test_offline_remparse.py сверяет с ним на одних фразах.
   Модель разбирает только то, что здесь не понято (engine.ts).

   В Python \b и \w — юникодные, в JS — только латиница: шаблоны пишутся
   как у ядра и проходят через rx(), который подставляет юникодные границы. */

const W = '[\\p{L}\\p{N}_]';
const B = `(?:(?<=${W})(?!${W})|(?<!${W})(?=${W}))`;

/** Шаблон в синтаксисе ядра → RegExp с юникодными \b и \w. */
function rx(src: string, flags = 'iu'): RegExp {
  return new RegExp(src.replace(/\[\\w/g, '[\\p{L}\\p{N}_').replace(/\\w/g, W).replace(/\\b/g, B), flags);
}

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// Обращение в начале фразы срезается по именам персоны (names); их нет — (?!), не срезается

const namesAlt = (names?: string[]) =>
  [...new Set((names ?? []).filter(Boolean))].sort((a, b) => b.length - a.length).map(esc).join('|') || '(?!)';

const RU_WORD_NUMBERS: Record<string, number> = {
  ноль: 0, одну: 1, один: 1, одно: 1, две: 2, два: 2,
  три: 3, четыре: 4, пять: 5, шесть: 6, семь: 7, восемь: 8,
  девять: 9, десять: 10, одиннадцать: 11, двенадцать: 12,
  тринадцать: 13, четырнадцать: 14, пятнадцать: 15, шестнадцать: 16,
  семнадцать: 17, восемнадцать: 18, девятнадцать: 19, двадцать: 20,
  тридцать: 30, сорок: 40, пятьдесят: 50, шестьдесят: 60,
  полтора: 1.5,
};

const EN_WORD_NUMBERS: Record<string, number> = {
  zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5,
  six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11,
  twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15,
  sixteen: 16, seventeen: 17, eighteen: 18, nineteen: 19,
  twenty: 20, thirty: 30, forty: 40, fifty: 50, sixty: 60,
  a: 1, an: 1,
};

const has = (o: Record<string, number>, k: string) => Object.prototype.hasOwnProperty.call(o, k);

const REMIND_EN = rx(String.raw`\bremind`);

/** Маркер просьбы: «напом…» или «remind…». */
export function hasRemindWord(text: string): boolean {
  const lower = text.toLowerCase();
  return lower.includes('напом') || REMIND_EN.test(lower);
}

const EN_UNITS = 'hours?|hrs?|h|minutes?|mins?|min|seconds?|secs?|sec|days?|d';

function enUnitMultiplier(unit: string): number {
  const u = unit.toLowerCase();
  if (u.startsWith('s')) return 1;
  if (u.startsWith('m')) return 60;
  if (u.startsWith('h')) return 3600;
  return 86400;
}

const NUM = String.raw`(\d+(?:[.,]\d+)?)`;

const TIME_UNIT_PATTERNS: [RegExp, number][] = [
  [rx(String.raw`через\s+${NUM}\s*(?:минуту|минуты|минут|мин|м)\b`), 60],
  [rx(String.raw`через\s+${NUM}\s*(?:час|часа|часов|ч)\b`), 3600],
  [rx(String.raw`через\s+${NUM}\s*(?:секунду|секунды|секунд|сек|с)\b`), 1],
  [rx(String.raw`через\s+${NUM}\s*(?:день|дня|дней|д)\b`), 86400],
];

const TIME_UNIT_FIRST_PATTERNS: [RegExp, number][] = [
  [rx(String.raw`через\s+(?:минуту|минуты|минут|мин)\s+${NUM}\b`), 60],
  [rx(String.raw`через\s+(?:час|часа|часов)\s+${NUM}\b`), 3600],
  [rx(String.raw`через\s+(?:секунду|секунды|секунд|сек)\s+${NUM}\b`), 1],
  [rx(String.raw`через\s+(?:день|дня|дней)\s+${NUM}\b`), 86400],
];

const TIME_WORD_PATTERNS: [RegExp, number][] = [
  [rx(String.raw`через\s+(\w+)\s+(?:минуту|минуты|минут|мин)\b`), 60],
  [rx(String.raw`через\s+(\w+)\s+(?:час|часа|часов)\b`), 3600],
  [rx(String.raw`через\s+(\w+)\s+(?:секунду|секунды|секунд|сек)\b`), 1],
  [rx(String.raw`через\s+(\w+)\s+(?:день|дня|дней)\b`), 86400],
];

const WORD_TIME: [RegExp, number][] = (
  [
    ['полчаса', 1800],
    ['полтора часа', 5400],
    ['час', 3600],
    ['два часа', 7200],
    ['минутку', 60],
    ['минутку-другую', 120],
  ] as [string, number][]
).map(([w, s]) => [rx(String.raw`через\s+${esc(w)}\b`), s]);

const EN_REL_NUM_RE = rx(String.raw`\b(?:in|after)\s+${NUM}\s*(${EN_UNITS})\b`);
const EN_REL_NUM_FROMNOW_RE = rx(String.raw`\b${NUM}\s*(${EN_UNITS})\s+from\s+now\b`);
const EN_REL_WORD_RE = rx(String.raw`\b(?:in|after)\s+(${Object.keys(EN_WORD_NUMBERS).join('|')})\s+(${EN_UNITS})\b`);

const EN_WORD_TIME: [RegExp, number][] = (
  [
    ['half an hour', 1800],
    ['a half hour', 1800],
    ['an hour', 3600],
    ['an hour and a half', 5400],
    ['a minute', 60],
    ['a couple of minutes', 120],
    ['a couple minutes', 120],
  ] as [string, number][]
).map(([p, s]) => [rx(String.raw`\bin\s+${esc(p)}\b`), s]);

// ─── Абсолютное время: «до 12», «в 11:30», «к полудню», «at 5 pm» ───

const ABS_PREP = '(?:до|к|в|at|by|until|till)';
const AMPM = String.raw`(?:a\.?m\.?|p\.?m\.?)?`;
const ABS_HM_RE = rx(String.raw`\b${ABS_PREP}\s+(\d{1,2})[:.\s](\d{2})\s*(${AMPM})`);
const ABS_HOUR_RE = rx(String.raw`\b${ABS_PREP}\s+(\d{1,2})\s*(${AMPM})\b(?!\s*:\s*\d)`);

const ABS_WORD_TIME: [RegExp, number][] = [
  ...Object.entries({ полдень: 12, полудня: 12, полудню: 12, полночь: 0, полуночи: 0 }).map(
    ([w, h]) => [rx(String.raw`\b(?:до|к|в)\s+${w}\b`), h] as [RegExp, number],
  ),
  ...Object.entries({ noon: 12, midnight: 0 }).map(([w, h]) => [rx(String.raw`\b${ABS_PREP}\s+${w}\b`), h] as [RegExp, number]),
];

const normalizeHour = (hour: number) => (((Math.trunc(hour) % 24) + 24) % 24);

function applyAmpm(hour: number, ampm: string | undefined): number {
  if (!ampm) return hour;
  if (/^p/i.test(ampm.replace(/^[.\s]+|[.\s]+$/g, ''))) return hour < 12 ? hour + 12 : hour;
  return hour % 12;
}

interface Span {
  start: number;
  end: number;
}

const spanOf = (m: RegExpExecArray): Span => ({ start: m.index, end: m.index + m[0].length });

function parseAbsoluteTime(text: string): { hour: number; minute: number; span: Span } | null {
  const lower = text.toLowerCase();
  for (const [r, hour] of ABS_WORD_TIME) {
    const m = r.exec(lower);
    if (m) return { hour, minute: 0, span: spanOf(m) };
  }
  let m = ABS_HM_RE.exec(lower);
  if (m) {
    const hour = applyAmpm(parseInt(m[1], 10), m[3]);
    const minute = parseInt(m[2], 10);
    if (hour >= 0 && hour <= 24 && minute >= 0 && minute < 60) return { hour, minute, span: spanOf(m) };
  }
  m = ABS_HOUR_RE.exec(lower);
  if (m) {
    const hour = applyAmpm(parseInt(m[1], 10), m[2]);
    if (hour >= 0 && hour <= 24) return { hour, minute: 0, span: spanOf(m) };
  }
  return null;
}

/** Секунды до ближайших hour:minute (сегодня или завтра); не дальше 7 дней. */
function absoluteToDelay(hour: number, minute: number, now: Date): number | null {
  const nowTotal = now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();
  let delay = normalizeHour(hour) * 3600 + minute * 60 - nowTotal;
  if (delay <= 0) delay += 86400;
  if (delay < 10 || delay > 7 * 86400) return null;
  return delay;
}

// ─── День и часть суток: «завтра», «утром», «в 8 вечера», «tomorrow morning at 8» ───

const DAY_OFFSET_RES: [RegExp, number][] = [
  [rx(String.raw`\bпослезавтра\b|\bпосле\s+завтра\b|\bday\s+after\s+tomorrow\b`), 2],
  [rx(String.raw`\bзавтра\b|\btomorrow\b`), 1],
  [rx(String.raw`\bсегодня\b|\btoday\b`), 0],
];

type DayPart = 'am' | 'day' | 'evening' | 'night';

const DAYPART_RES: [RegExp, DayPart][] = [
  [rx(String.raw`\bутром\b`), 'am'],
  [rx(String.raw`\bднём\b`), 'day'],
  [rx(String.raw`\bвечером\b`), 'evening'],
  [rx(String.raw`\bночью\b`), 'night'],
  [rx(String.raw`\b(?:in\s+the\s+|at\s+)?morning\b`), 'am'],
  [rx(String.raw`\b(?:in\s+the\s+|at\s+)?afternoon\b`), 'day'],
  [rx(String.raw`\b(?:in\s+the\s+|at\s+)?evening\b`), 'evening'],
  [rx(String.raw`\b(?:at\s+)?night\b`), 'night'],
  [rx(String.raw`\btonight\b`), 'night'],
];

const DAYPART_PREP = String.raw`(?:\b(?:в|к|до|около)\s+)?`;
const HOUR_MIN_DAYPART_RE = rx(DAYPART_PREP + String.raw`\b(\d{1,2})[:.](\d{2})\s*(утра|дня|вечера|ночи)\b`);
const HOUR_DAYPART_RE = rx(DAYPART_PREP + String.raw`(?<![:.\d])\b(\d{1,2})\s*(утра|дня|вечера|ночи)\b`);

const DAYPART_DEFAULT_HOUR: Record<DayPart, number> = { am: 9, day: 15, evening: 19, night: 23 };
const HOUR_GEN_TO_KIND: Record<string, DayPart> = { утра: 'am', дня: 'day', вечера: 'evening', ночи: 'night' };
const GREETING_PREFIXES = ['good ', 'добр'];

function adjustHourByDaypart(hour: number, kind: DayPart): number {
  const h = normalizeHour(hour);
  if (kind === 'day' || kind === 'evening') return h < 12 ? h + 12 : h;
  if (kind === 'night') {
    if (h === 12) return 0;
    if (h >= 6 && h < 12) return h + 12;
  }
  return h;
}

const collapse = (s: string) => s.split(/\s+/u).filter(Boolean).join(' ');

function removeSpans(text: string, spans: Span[]): string {
  const out: string[] = [];
  let prev = 0;
  for (const { start, end } of [...spans].sort((a, b) => a.start - b.start || a.end - b.end)) {
    const s = Math.max(start, prev);
    out.push(text.slice(prev, s));
    prev = Math.max(prev, end);
  }
  out.push(text.slice(prev));
  return collapse(out.join(''));
}

const REMIND_VERBS_RE = rx(String.raw`\b(?:напомни|напомнить|напоминание|напомните|напомню)\b`, 'giu');
const REMIND_EN_TO_RE = rx(String.raw`\bremind\w*(?:\s+(?:me|us))?(?:\s+to)?\b`, 'giu');

function cleanTaskFragment(fragment: string, names?: string[]): string {
  let s = fragment.trim();
  s = s.replace(rx(String.raw`^(?:${namesAlt(names)})[,\s]+`), '');
  s = s.replace(REMIND_VERBS_RE, '').trim();
  s = s.replace(REMIND_EN_TO_RE, '').trim();
  s = s.replace(rx(String.raw`^(?:мне|нам|ему|ей|им)\b\s*`), '').trim();
  s = s.replace(rx(String.raw`^(?:please)\s+`), '').trim();
  s = s.replace(rx(String.raw`^to\s+`), '').trim();
  s = s.replace(/^[,:\-\s]+/u, '').trim();
  s = s.replace(/[,:\-\s]+$/u, '').trim();
  s = s.replace(/[.!?]+$/u, '').trim();
  return s;
}

/** Секунды до (сегодня + dayOffset) hour:minute; offset 0 — как absoluteToDelay. */
function delayUntil(dayOffset: number, hour: number, minute: number, now: Date): number | null {
  if (dayOffset <= 0) return absoluteToDelay(hour, minute, now);
  const target = new Date(now.getFullYear(), now.getMonth(), now.getDate() + dayOffset, normalizeHour(hour), minute);
  const delay = (target.getTime() - now.getTime()) / 1000;
  if (delay < 10 || delay > 30 * 86400) return null;
  return delay;
}

function parseDayDaypart(text: string, now: Date): { delay: number; spans: Span[] } | null {
  const lower = text.toLowerCase();
  const spans: Span[] = [];
  let dayOffset: number | null = null;
  for (const [r, off] of DAY_OFFSET_RES) {
    const m = r.exec(lower);
    if (m) {
      dayOffset = off;
      spans.push(spanOf(m));
      break;
    }
  }

  let kind: DayPart | null = null;
  let explicitHour: number | null = null;
  let explicitMinute = 0;

  let hg = HOUR_MIN_DAYPART_RE.exec(lower);
  if (hg) {
    explicitHour = parseInt(hg[1], 10);
    explicitMinute = parseInt(hg[2], 10);
    kind = HOUR_GEN_TO_KIND[hg[3].toLowerCase()];
    spans.push(spanOf(hg));
  } else {
    hg = HOUR_DAYPART_RE.exec(lower);
    if (hg) {
      explicitHour = parseInt(hg[1], 10);
      kind = HOUR_GEN_TO_KIND[hg[2].toLowerCase()];
      spans.push(spanOf(hg));
    }
  }

  if (kind === null) {
    for (const [r, k] of DAYPART_RES) {
      const m = r.exec(lower);
      if (!m) continue;
      // «good morning» / «добрым утром» — приветствие, не время
      const prefix = lower.slice(Math.max(0, m.index - 8), m.index);
      if (GREETING_PREFIXES.some((g) => prefix.includes(g))) continue;
      kind = k;
      spans.push(spanOf(m));
      break;
    }
  }

  if (dayOffset === null && kind === null) return null;

  if (explicitHour === null) {
    const abs = parseAbsoluteTime(text);
    if (abs) {
      explicitHour = abs.hour;
      explicitMinute = abs.minute;
      spans.push(abs.span);
    }
  }

  let hour: number;
  if (explicitHour !== null && kind !== null) hour = adjustHourByDaypart(explicitHour, kind);
  else if (explicitHour !== null) hour = Math.trunc(explicitHour) % 24;
  else if (kind !== null) hour = DAYPART_DEFAULT_HOUR[kind];
  else hour = 9; // «завтра» без уточнений — утро
  const minute = explicitHour !== null ? explicitMinute : 0;

  const delay = delayUntil(dayOffset ?? 0, hour, minute, now);
  return delay === null ? null : { delay, spans };
}

export interface ParsedReminder {
  task: string | null;
  delay: number; // секунды от now
}

/**
 * Просьба о напоминании → задача и через сколько секунд; null — не понято
 * (нет «напом…/remind» или времени). names — обращения к персоне в начале.
 */
export function parseReminder(text: string, names?: string[], now: Date = new Date()): ParsedReminder | null {
  const lower = text.toLowerCase();
  if (!hasRemindWord(text)) return null;

  // Найденное время: через сколько секунд и где оно во фразе
  const found: { delay: number | null; span: Span | null } = { delay: null, span: null };
  const take = (m: RegExpExecArray, secs: number) => {
    found.delay = secs;
    found.span = spanOf(m);
  };

  // 1. Относительное время: «через N …»
  if (lower.includes('через')) {
    for (const [r, secs] of WORD_TIME) {
      const m = r.exec(lower);
      if (m) {
        take(m, secs);
        break;
      }
    }
    if (found.delay === null) {
      for (const [r, mult] of [...TIME_UNIT_PATTERNS, ...TIME_UNIT_FIRST_PATTERNS]) {
        const m = r.exec(lower);
        if (m) {
          take(m, parseFloat(m[1].replace(/,/g, '.')) * mult);
          break;
        }
      }
    }
    if (found.delay === null) {
      for (const [r, mult] of TIME_WORD_PATTERNS) {
        const m = r.exec(lower);
        if (m && has(RU_WORD_NUMBERS, m[1].toLowerCase())) {
          take(m, RU_WORD_NUMBERS[m[1].toLowerCase()] * mult);
          break;
        }
      }
    }
  }

  // 1b. Относительное время (англ.): «in 30 minutes», «after 2 hours»
  if (found.delay === null) {
    const m = EN_REL_NUM_RE.exec(lower) ?? EN_REL_NUM_FROMNOW_RE.exec(lower);
    if (m) take(m, parseFloat(m[1].replace(/,/g, '.')) * enUnitMultiplier(m[2]));
  }
  if (found.delay === null) {
    for (const [r, secs] of EN_WORD_TIME) {
      const m = r.exec(lower);
      if (m) {
        take(m, secs);
        break;
      }
    }
  }
  if (found.delay === null) {
    const m = EN_REL_WORD_RE.exec(lower);
    if (m && has(EN_WORD_NUMBERS, m[1].toLowerCase())) take(m, EN_WORD_NUMBERS[m[1].toLowerCase()] * enUnitMultiplier(m[2]));
  }

  // 1c. День и часть суток — до общего абсолютного разбора: иначе «завтра
  // в 8» съедалось бы как «в 8», а «завтра» оставалось бы в задаче
  if (found.delay === null) {
    const day = parseDayDaypart(text, now);
    if (day) {
      const task = cleanTaskFragment(removeSpans(text, day.spans), names);
      return { task: task || null, delay: day.delay };
    }
  }

  // 2. Абсолютное время: «до 12», «в 11:30», «к полудню»
  if (found.delay === null) {
    const abs = parseAbsoluteTime(text);
    if (abs) {
      const secs = absoluteToDelay(abs.hour, abs.minute, now);
      if (secs) {
        found.delay = secs;
        found.span = abs.span;
      }
    }
  }

  const d = found.delay;
  const span = found.span;
  if (d === null || span === null || d < 10 || d > 30 * 86400) return null;

  // Задача — текст после времени, а если там пусто — до него
  let after = text.slice(span.end).trim();
  after = after.replace(/^[,:\-\s]+/u, '').trim();
  after = after.replace(rx(String.raw`^(?:напомни|напомнить)(?:\s+мне)?\s*`), '').trim();
  after = after.replace(rx(String.raw`^remind(?:er|ers)?(?:\s+(?:me|us))?(?:\s+to)?\s*`), '').trim();
  after = after.replace(rx(String.raw`^to\s+`), '').trim();
  after = after.replace(/^[,:\-\s]+/u, '').trim();
  after = after.replace(/[.!?]+$/u, '').trim();

  if (!after) {
    let before = text.slice(0, span.start).trim();
    before = before.replace(rx(String.raw`^(?:${namesAlt(names)})[,\s]+`), '');
    before = before.replace(REMIND_VERBS_RE, '').trim();
    before = before.replace(REMIND_EN_TO_RE, '').trim();
    before = before.replace(rx(String.raw`^(?:please|please,)\s+`), '').trim();
    before = before.replace(rx(String.raw`^(?:can|could|would|will)\s+you\s+`), '').trim();
    before = before.replace(rx(String.raw`\b(?:сделай|поставь|создай)\b`, 'giu'), '').trim();
    before = before.replace(rx(String.raw`\b(?:set|make|create)\s+(?:a\s+)?remind\w*\b`, 'giu'), '').trim();
    before = before.replace(rx(String.raw`\b(?:с\s+таким\s+содержимым|с\s+содержимым)\b[:\s]*`, 'giu'), '').trim();
    before = before.replace(rx(String.raw`^(?:мне|мне\s+про|мне\s+о)\b`), '').trim();
    before = before.replace(/^[,:\-\s]+/u, '').trim();
    after = before;
  }
  return { task: after || null, delay: d };
}

// ─── Повтор, просьба, отказ, попытка назвать время — как у ядра ───

const RECURRING_DAILY_RE = rx(String.raw`\b(?:каждый\s+день|ежедневно|every\s+day|each\s+day|daily)\b`);
const RECURRING_WEEKLY_RE = rx(
  String.raw`\b(?:каждый\s+(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)` +
    String.raw`|по\s+(понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям)` +
    String.raw`|(?:every|each)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?` +
    String.raw`|on\s+(mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays))\b`,
);

/** Просьба о повторе («каждый день», «по пятницам») — их ставит только ядро. */
export function isRecurringAsk(text: string): boolean {
  return hasRemindWord(text) && (RECURRING_DAILY_RE.test(text) || RECURRING_WEEKLY_RE.test(text));
}

const REMIND_CREATE_RE = rx(
  String.raw`(?<![\wё])(?:напомни(?:те)?|напомнить|напоминай(?:те)?|` +
    String.raw`(?:поставь|создай|сделай|добавь|заведи|установи)(?:те)?\s+(?:мне\s+)?напоминани\w*)` +
    String.raw`(?![\wё])|\bremind\s+(?:me|us)\b|` +
    String.raw`\b(?:set|create|add|make)\s+(?:me\s+)?(?:a|an|the)?\s*reminder\b`,
);

/** Просьба СОЗДАТЬ напоминание, а не любое упоминание («ты мне напомнил?»). */
export function isReminderCreateRequest(text: string): boolean {
  return !!text && REMIND_CREATE_RE.test(text);
}

const PENDING_DECLINE_RE = rx(
  String.raw`^\s*(?:не\s+надо|не\s+нужно|нет|отмена|отмени|забудь|неважно|не\s+важно|` +
    String.raw`передумал[аи]?|no|nope|cancel|never\s*mind|forget\s+it|don[’']?t)` +
    String.raw`(?:[\s,]+(?:спасибо|thanks|thank\s+you))?\s*[.!…]*\s*$`,
);

/** «не надо» / «отмена» в ответ на «когда напомнить?». */
export function isPendingDecline(text: string): boolean {
  return !!text && PENDING_DECLINE_RE.test(text);
}

const TIME_ATTEMPT_RE = rx(
  String.raw`\d|(?<![\wё])(?:через|в|во|к|на|завтра|послезавтра|сегодня|утр\w*|вечер\w*|` +
    String.raw`ноч\w*|дн[её]м|полдень|полночь|час\w*|минут\w*|секунд\w*|сутки|недел\w*|` +
    String.raw`кажд\w*|ежедневно|понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|` +
    String.raw`суббот\w*|воскресень\w*|полчаса|полтора|` +
    String.raw`in|at|on|tomorrow|today|tonight|morning|evening|noon|midnight|hours?|` +
    String.raw`minutes?|mins?|seconds?|every|daily|week|monday|tuesday|wednesday|thursday|` +
    String.raw`friday|saturday|sunday|half)(?![\wё])`,
);

/** Похоже на попытку назвать время («в 8», «завтра утром», «через час»). */
export function looksLikeTimeAttempt(text: string): boolean {
  return !!text && TIME_ATTEMPT_RE.test(text);
}
